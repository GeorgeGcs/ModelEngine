"""Atomic ownership of outbound attempts. Uncertain sends are never auto-replayed."""
import uuid
from datetime import datetime, timedelta, timezone


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class PushState:
    def initialize_push_state(self, conn):
        conn.execute("BEGIN IMMEDIATE")
        columns = {r[1] for r in conn.execute("PRAGMA table_info(push_records)")}
        if "lease_token" not in columns:
            conn.execute("ALTER TABLE push_records ADD COLUMN lease_token TEXT")
            conn.execute("ALTER TABLE push_records ADD COLUMN lease_until TEXT")
            # Old pending rows lack an owner and may already have reached a gateway.
            conn.execute("UPDATE push_records SET status='unknown', failure_reason=? "
                         "WHERE status='pending'", ("旧版本未完成发送，需人工核对",))
        conn.execute("CREATE TABLE IF NOT EXISTS push_attempts ("
                     "attempt_id TEXT PRIMARY KEY,push_id INTEGER NOT NULL,"
                     "started_at TEXT NOT NULL,finished_at TEXT,status TEXT NOT NULL,"
                     "source TEXT NOT NULL,failure_reason TEXT)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_push_attempt_time ON push_attempts(started_at)")
        attempts = {r[1] for r in conn.execute("PRAGMA table_info(push_attempts)")}
        for column in ("provider_message_id", "phone_confirmed_at", "phone_observer"):
            if column not in attempts:
                conn.execute(f"ALTER TABLE push_attempts ADD COLUMN {column} TEXT")
        conn.execute("COMMIT")

    def _claim_push(self, conn, row, *, window_seconds, max_per_window, source,
                    lease_seconds, gateway_id, simulated):
        if not row or row["status"] not in ("failed", "pending"):
            return None
        if gateway_id is not None and (row["gateway_id"] != gateway_id
                                      or bool(row["simulated"]) != simulated):
            return None
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(seconds=window_seconds)).isoformat()
        used = conn.execute(
            "SELECT COUNT(*) FROM push_attempts a JOIN push_records p ON p.id=a.push_id "
            "WHERE p.recipient_id=? AND julianday(a.started_at)>=julianday(?)",
            (row["recipient_id"], cutoff)).fetchone()[0]
        # Preserve known legacy attempts without inventing per-attempt timestamps.
        used += conn.execute("SELECT COALESCE(SUM(p.attempts),0) FROM push_records p "
                             "WHERE p.recipient_id=? AND julianday(p.updated_at)>=julianday(?) "
                             "AND NOT EXISTS(SELECT 1 FROM push_attempts a WHERE a.push_id=p.id)",
                             (row["recipient_id"], cutoff)).fetchone()[0]
        if used >= max_per_window:
            status = 'suppressed' if row['status'] == 'pending' and row['attempts'] == 0 else row['status']
            conn.execute('UPDATE push_records SET status=?,failure_reason=? WHERE id=?',
                         (status, '实际发送尝试已达窗口上限，未领取发送权', row['id']))
            return None
        token = uuid.uuid4().hex
        conn.execute("UPDATE push_records SET status='sending',lease_token=?,lease_until=?,"
                     "attempts=attempts+1,updated_at=? WHERE id=?",
                     (token, (now + timedelta(seconds=lease_seconds)).isoformat(),
                      now.isoformat(), row["id"]))
        conn.execute("INSERT INTO push_attempts(attempt_id,push_id,started_at,status,source) VALUES(?,?,?,'sending',?)",
                     (token, row["id"], now.isoformat(), source))
        return {**dict(row), "lease_token": token, "attempt_id": token}

    def push_retry(self, event_id, recipient_id, *, window_seconds=60, max_per_window=3,
                   source="manual", lease_seconds=120, gateway_id=None, simulated=None):
        self.recover_pushes()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM push_records WHERE event_id=? AND recipient_id=?",
                               (event_id, recipient_id)).fetchone()
            claimed = self._claim_push(conn, row, window_seconds=window_seconds,
                                       max_per_window=max_per_window, source=source,
                                       lease_seconds=lease_seconds, gateway_id=gateway_id,
                                       simulated=simulated)
            conn.execute("COMMIT")
            return claimed

    def push_finish(self, record_id, status, failure_reason=None, *, lease_token, provider_message_id=None):
        if status not in ("delivered", "accepted", "unknown", "failed"):
            status, failure_reason = "unknown", "未识别的网关状态"
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT simulated FROM push_records WHERE id=?", (record_id,)).fetchone()
            if row and not row["simulated"] and status == "delivered":
                status, failure_reason = "accepted", "未实现终端送达核验，不认领真实送达"
            changed = conn.execute("UPDATE push_records SET status=?,failure_reason=?,updated_at=?,"
                                   "lease_token=NULL,lease_until=NULL WHERE id=? "
                                   "AND status='sending' AND lease_token=?",
                                   (status, failure_reason, stamp(), record_id, lease_token)).rowcount
            if changed:
                conn.execute("UPDATE push_attempts SET status=?,failure_reason=?,finished_at=?,"
                             "provider_message_id=? WHERE attempt_id=?",
                             (status, failure_reason, stamp(), provider_message_id, lease_token))
            conn.execute("COMMIT")
            return status if changed else None

    def recover_pushes(self):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT id,lease_token FROM push_records WHERE status='sending' "
                                "AND julianday(lease_until)<=julianday(?)", (stamp(),)).fetchall()
            for row in rows:
                conn.execute("UPDATE push_attempts SET status='unknown',finished_at=?,failure_reason=? "
                             "WHERE attempt_id=?", (stamp(), "发送租约过期；不得自动重发", row["lease_token"]))
                conn.execute("UPDATE push_records SET status='unknown',failure_reason=?,"
                             "lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                             ("发送租约过期，需人工核对", stamp(), row["id"]))
            conn.execute("COMMIT")
            return len(rows)

    def list_push_attempts(self):
        with self.connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM push_attempts ORDER BY started_at,attempt_id")]

    def confirm_clawbot_phone(self, phone_code, observer):
        if not observer.strip() or len(observer) > 80:
            raise ValueError("须提供观察者代号")
        with self.connection() as conn:
            at = stamp()
            row = conn.execute(
                "UPDATE push_attempts SET phone_confirmed_at=?,phone_observer=? "
                "WHERE attempt_id=? AND status IN ('accepted','unknown') AND phone_confirmed_at IS NULL "
                "AND push_id IN (SELECT id FROM push_records WHERE gateway_id='clawbot' AND simulated=0) "
                "RETURNING attempt_id,push_id,provider_message_id,phone_confirmed_at",
                (at, observer, phone_code)).fetchone()
            return dict(row) if row else None
