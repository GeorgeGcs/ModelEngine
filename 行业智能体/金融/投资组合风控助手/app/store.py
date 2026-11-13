"""本地演示闭环的 SQLite 持久化层（只用标准库）。

要点：
- 每次操作独立连接 + 显式事务，适配 ThreadingHTTPServer 的多线程请求。
- 推送记录以 (event_id, recipient_id) 为幂等键；发送前原子领取，最多一个有效发送所有者。
- 限频窗口按接收人统计实际尝试；被抑制也落一条 suppressed 记录便于查询。
- 明确失败可重试并追加尝试；受理/不确定结果不重发，不承诺外部网络 exactly-once。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .push_state import PushState
from .knowledge_state import KnowledgeState
from .business_state import BusinessState

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "local_app.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolios (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS holdings (
    portfolio_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    cost TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    holding_date TEXT NOT NULL,
    stop_loss TEXT NOT NULL,
    take_profit TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (portfolio_id, symbol)
);
CREATE TABLE IF NOT EXISTS snapshots (
    run_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    prices_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, timestamp)
);
CREATE TABLE IF NOT EXISTS monitor_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    signal_count INTEGER NOT NULL DEFAULT 0,
    push_count INTEGER NOT NULL DEFAULT 0,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS signals (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    portfolio_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    risk_type TEXT NOT NULL,
    trigger_time TEXT NOT NULL,
    evidence TEXT NOT NULL,
    level TEXT NOT NULL,
    review_action TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS push_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    recipient_id TEXT NOT NULL,
    recipient_masked TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    failure_reason TEXT,
    gateway_id TEXT NOT NULL,
    simulated INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    UNIQUE (event_id, recipient_id)
);
CREATE INDEX IF NOT EXISTS idx_push_recipient_time
    ON push_records (recipient_id, created_at);
CREATE INDEX IF NOT EXISTS idx_signals_portfolio
    ON signals (portfolio_id, trigger_time);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def mask_recipient(recipient_id: str) -> str:
    """接口展示用稳定摘要；原接收人仅在私有 DB 中供发送和幂等关联。"""
    digest = hashlib.sha256(recipient_id.encode("utf-8")).hexdigest()[:10]
    prefix = recipient_id[:2] if len(recipient_id) >= 2 else "*"
    return f"{prefix}***@{digest}"


class Store(PushState, KnowledgeState, BusinessState):
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else DEFAULT_DB
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connection(self):
        """每次操作独立连接，退出时必定关闭（Windows 上否则 DB 文件被占用）。

        事务仍由各方法显式 BEGIN IMMEDIATE / COMMIT / ROLLBACK 控制；
        本上下文管理器不代为提交，避免破坏原子性。
        """
        conn = sqlite3.connect(str(self.path), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        conn.execute("PRAGMA busy_timeout=15000")
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            self.initialize_push_state(conn)
            self.initialize_knowledge_state(conn)
            self.initialize_business_state(conn)

    # ---------- 组合与持仓 ----------
    def upsert_portfolio(self, portfolio: dict, *, replace_holdings: bool = True) -> dict:
        required = ("id", "name", "type", "holdings")
        missing = [k for k in required if k not in portfolio]
        if missing:
            raise ValueError(f"组合缺字段：{missing}")
        pid = portfolio["id"]
        if not isinstance(pid, str) or not pid.strip():
            raise ValueError("组合 id 须为非空字符串")
        if portfolio["type"] not in ("股票", "基金", "混合"):
            raise ValueError("组合 type 须为 股票 / 基金 / 混合")
        holdings = portfolio["holdings"]
        if not isinstance(holdings, list) or not holdings:
            raise ValueError("组合至少须有一项持仓")
        now = utc_now()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO portfolios(id,name,type,created_at,updated_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "name=excluded.name, type=excluded.type, updated_at=excluded.updated_at",
                    (pid, portfolio["name"], portfolio["type"], now, now))
                if replace_holdings:
                    conn.execute("DELETE FROM holdings WHERE portfolio_id=?", (pid,))
                for item in holdings:
                    self._write_holding(conn, pid, item, now)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return self.get_portfolio(pid)

    def _write_holding(self, conn: sqlite3.Connection, pid: str,
                       item: dict, now: str) -> None:
        fields = ("symbol", "cost", "quantity", "holding_date", "stop_loss", "take_profit")
        missing = [k for k in fields if k not in item or item[k] in ("", None)]
        if missing:
            raise ValueError(f"持仓缺字段：{missing}")
        symbol = item["symbol"]
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("持仓 symbol 须为非空字符串")
        try:
            quantity = int(item["quantity"])
        except (TypeError, ValueError) as exc:
            raise ValueError("持仓 quantity 须为正整数") from exc
        if quantity <= 0:
            raise ValueError("持仓 quantity 须为正整数")
        for key in ("cost", "stop_loss", "take_profit"):
            raw = str(item[key])
            try:
                value = float(raw)
            except ValueError as exc:
                raise ValueError(f"{symbol}.{key} 不是有效数值") from exc
            if value <= 0:
                raise ValueError(f"{symbol}.{key} 须为正数")
        if float(item["stop_loss"]) >= float(item["take_profit"]):
            raise ValueError(f"{symbol} 的止损线须低于止盈线")
        conn.execute(
            "INSERT INTO holdings(portfolio_id,symbol,cost,quantity,holding_date,"
            "stop_loss,take_profit,updated_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(portfolio_id,symbol) DO UPDATE SET cost=excluded.cost, "
            "quantity=excluded.quantity, holding_date=excluded.holding_date, "
            "stop_loss=excluded.stop_loss, take_profit=excluded.take_profit, "
            "updated_at=excluded.updated_at",
            (pid, symbol, str(item["cost"]), quantity, str(item["holding_date"]),
             str(item["stop_loss"]), str(item["take_profit"]), now))

    def set_holding(self, portfolio_id: str, holding: dict) -> dict:
        if self.get_portfolio(portfolio_id) is None:
            raise ValueError(f"组合不存在：{portfolio_id}")
        now = utc_now()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._write_holding(conn, portfolio_id, holding, now)
                conn.execute("UPDATE portfolios SET updated_at=? WHERE id=?",
                             (now, portfolio_id))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return self.get_portfolio(portfolio_id)

    def remove_holding(self, portfolio_id: str, symbol: str) -> None:
        with self.connection() as conn:
            conn.execute("DELETE FROM holdings WHERE portfolio_id=? AND symbol=?",
                         (portfolio_id, symbol))

    def list_portfolios(self) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM portfolios ORDER BY id").fetchall()
            result = []
            for row in rows:
                holdings = conn.execute(
                    "SELECT symbol,cost,quantity,holding_date,stop_loss,take_profit "
                    "FROM holdings WHERE portfolio_id=? ORDER BY symbol",
                    (row["id"],)).fetchall()
                result.append({
                    "id": row["id"], "name": row["name"], "type": row["type"],
                    "created_at": row["created_at"], "updated_at": row["updated_at"],
                    "holdings": [dict(h) for h in holdings],
                })
            return result

    def get_portfolio(self, portfolio_id: str) -> dict | None:
        for item in self.list_portfolios():
            if item["id"] == portfolio_id:
                return item
        return None

    # ---------- 快照与监控运行 ----------
    def save_snapshot(self, run_id: str, timestamp: str, prices: dict) -> None:
        with self.connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO snapshots(run_id,timestamp,prices_json,created_at) "
                "VALUES(?,?,?,?)",
                (run_id, timestamp, json.dumps(prices, ensure_ascii=False,
                                               sort_keys=True), utc_now()))

    def previous_prices(self, before_timestamp: str) -> dict:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT prices_json FROM snapshots WHERE timestamp < ? "
                "ORDER BY timestamp DESC LIMIT 1", (before_timestamp,)).fetchone()
        return json.loads(row["prices_json"]) if row else {}

    def open_run(self, run_id: str, detail: dict) -> None:
        with self.connection() as conn:
            try:
                conn.execute(
                    "INSERT INTO monitor_runs(run_id,started_at,status,detail_json) VALUES(?,?,?,?)",
                    (run_id, utc_now(), "running",json.dumps(detail, ensure_ascii=False, sort_keys=True)))
            except sqlite3.IntegrityError:
                raise ValueError('run_id 已存在；不得覆盖原运行，请使用新的独立 run_id') from None

    def finish_run(self, run_id: str, status: str, signal_count: int,
                   push_count: int) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE monitor_runs SET finished_at=?, status=?, "
                "signal_count=?, push_count=? WHERE run_id=?",
                (utc_now(), status, signal_count, push_count, run_id))

    def list_runs(self) -> list[dict]:
        with self.connection() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM monitor_runs ORDER BY started_at DESC LIMIT 50")]

    # ---------- 信号 ----------
    def insert_signal(self, signal: dict) -> bool:
        """返回 True 表示本次新写入；同 event_id 重复触发不覆盖原始依据。"""
        with self.connection() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO signals(event_id,run_id,portfolio_id,symbol,"
                "risk_type,trigger_time,evidence,level,review_action,detail_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (signal["event_id"], signal["run_id"], signal["portfolio_id"],
                 signal["symbol"], signal["risk_type"], signal["trigger_time"],
                 signal["evidence"], signal["level"], signal["review_action"],
                 json.dumps(signal.get("detail", {}), ensure_ascii=False,
                            sort_keys=True), utc_now()))
            created = cursor.rowcount > 0
            conn.execute('INSERT OR IGNORE INTO business_signals VALUES(?,?,?)',
                         (signal['run_id'],signal['event_id'],int(created)))
            return created

    def list_signals(self, *, portfolio_id: str | None = None,
                     run_id: str | None = None, limit: int = 200) -> list[dict]:
        query = "SELECT * FROM signals WHERE 1=1"
        params: list = []
        if portfolio_id:
            query += " AND portfolio_id=?"
            params.append(portfolio_id)
        if run_id:
            query += " AND run_id=?"
            params.append(run_id)
        query += " ORDER BY trigger_time DESC, event_id LIMIT ?"
        params.append(limit)
        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            result.append(item)
        return result

    def get_signal(self, event_id: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM signals WHERE event_id=?",
                               (event_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["detail"] = json.loads(item.pop("detail_json"))
        return item

    # ---------- 推送：幂等 / 限频 / 故障恢复 ----------
    def push_begin(self, *, event_id: str, recipient_id: str, message: str,
                   gateway_id: str, simulated: bool, window_seconds: int = 60,
                   max_per_window: int = 3) -> dict:
        """登记一次推送尝试。返回 created=False 表示同事件已处理，不得重复发送。"""
        now = utc_now()
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window_seconds)) \
            .isoformat(timespec="seconds").replace("+00:00", "Z")
        masked = mask_recipient(recipient_id)
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT * FROM push_records WHERE event_id=? AND recipient_id=?",
                    (event_id, recipient_id)).fetchone()
                if existing:
                    conn.execute("COMMIT")
                    return {"created": False, "duplicate": True,
                            **dict(existing)}
                used = conn.execute(
                    "SELECT COUNT(*) AS n FROM push_records WHERE recipient_id=? "
                    "AND created_at >= ? AND status IN ('pending','delivered','failed')",
                    (recipient_id, cutoff)).fetchone()["n"]
                status = "suppressed" if used >= max_per_window else "pending"
                reason = ("同一接收人限频窗口内已达上限，本次抑制"
                          if status == "suppressed" else None)
                cursor = conn.execute(
                    "INSERT INTO push_records(event_id,recipient_id,recipient_masked,"
                    "message,created_at,updated_at,status,failure_reason,gateway_id,"
                    "simulated,attempts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (event_id, recipient_id, masked, message, now, now, status,
                     reason, gateway_id, 1 if simulated else 0, 0))
                row = conn.execute("SELECT * FROM push_records WHERE id=?",
                                   (cursor.lastrowid,)).fetchone()
                conn.execute("COMMIT")
                return {"created": True, "duplicate": False, **dict(row)}
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def list_pushes(self, *, event_id: str | None = None,
                    limit: int = 200) -> list[dict]:
        query = (
            "SELECT p.*, (SELECT max(a.phone_confirmed_at) FROM push_attempts a "
            "WHERE a.push_id=p.id) AS phone_confirmed_at FROM push_records p WHERE 1=1")
        params: list = []
        if event_id:
            query += " AND event_id=?"
            params.append(event_id)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self.connection() as conn:
            return [{k: r[k] for k in r.keys() if k not in ('recipient_id', 'lease_token')}
                    for r in conn.execute(query, params).fetchall()]

    def counts(self) -> dict:
        with self.connection() as conn:
            def one(table: str) -> int:
                return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            return {
                "portfolios": one("portfolios"),
                "holdings": one("holdings"),
                "signals": one("signals"),
                "push_records": one("push_records"),
                "simulated_delivered": conn.execute("SELECT COUNT(*) FROM push_records WHERE status='delivered' AND simulated=1").fetchone()[0],
                "legacy_unverified_delivery": conn.execute("SELECT COUNT(*) FROM push_records WHERE status='delivered' AND simulated=0").fetchone()[0],
                "accepted": conn.execute("SELECT COUNT(*) FROM push_records WHERE status='accepted'").fetchone()[0],
                "unknown": conn.execute("SELECT COUNT(*) FROM push_records WHERE status='unknown'").fetchone()[0],
                "delivered": conn.execute(
                    "SELECT COUNT(*) AS n FROM push_records WHERE status='delivered'"
                ).fetchone()["n"],
                "suppressed": conn.execute(
                    "SELECT COUNT(*) AS n FROM push_records WHERE status='suppressed'"
                ).fetchone()["n"],
                "failed": conn.execute(
                    "SELECT COUNT(*) AS n FROM push_records WHERE status='failed'"
                ).fetchone()["n"],
            }
