"""SQLite 持久化层：把本次 Nexent 运行实际交付的结构化工具结果落库。

约束（评审可据此逐条核对）：
- 本模块**不计算规则、不生成信号、不补信号、不去重、不改写数值**；
  传入的 signals 必须是平台工具返回的原始结构化载荷（十进制字符串原样保留）。
- 不导入被测规则包，不读取冻结标注文件，不读取离线回放结果文件。
- 同一次 run_id 内 event_id 唯一；重试遇到相同 event_id 且内容一致 -> 幂等返回；
  内容冲突 -> 报错，绝不覆盖原始依据。
- 任务执行记录与最终信号记录分开，零命中任务也必须留下成功完成记录。
- **原子性**：一个任务的 completed 状态、全部信号、全部尝试日志在同一个事务里
  成功提交；中途任何冲突或异常都整批回滚，不留半条信号。失败/冲突原因写入
  独立的 task_failures 表，不受业务回滚影响。
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA_PATH = HERE / "schema.sql"
SIGNAL_FIELDS = (
    "event_id", "portfolio_id", "timestamp", "symbol",
    "rule", "price", "threshold",
)


class SignalConflictError(Exception):
    """同一 event_id 被以不同内容重投，拒绝覆盖原始依据。"""


class PayloadMismatchError(Exception):
    """重试任务引用的工具载荷与首次记录不一致。"""


class PayloadValidationError(Exception):
    """平台返回的载荷结构不合法，事务未开始即拒绝。"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        if not db_path.exists():
            raise FileNotFoundError(f"数据库不存在：{db_path}")
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # 关闭 sqlite3 的隐式事务：所有写入都由本模块显式 BEGIN/COMMIT/ROLLBACK，
    # 这样 SAVEPOINT、外键检查与整批回滚的行为才可预测。
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def start_run(conn: sqlite3.Connection, *, run_id: str, platform_label: str,
              platform_version: str | None, agent_id: str | None,
              source_input_sha256: str, task_manifest_sha256: str,
              note: str = "") -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO runs (run_id, platform_label, platform_version, agent_id,"
            " source_input_sha256, task_manifest_sha256, started_at, status, note)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)",
            (run_id, platform_label, platform_version, agent_id,
             source_input_sha256, task_manifest_sha256, utc_now(), note),
        )


def finish_run(conn: sqlite3.Connection, run_id: str, status: str) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE runs SET status = ?, finished_at = ? WHERE run_id = ?",
            (status, utc_now(), run_id),
        )


def _write_task_row(conn: sqlite3.Connection, task: dict) -> None:
    """写一个任务执行行。**不提交事务**，由调用方包在同一个事务里。"""
    existing = conn.execute(
        "SELECT tool_response_sha256, attempt_count FROM task_runs"
        " WHERE run_id = ? AND task_key = ?",
        (task["run_id"], task["task_key"]),
    ).fetchone()
    if existing is not None:
        if existing["tool_response_sha256"] != task["tool_response_sha256"]:
            raise PayloadMismatchError(
                f"任务 {task['task_key']} 重试引用的载荷哈希与首次记录不一致："
                f"{existing['tool_response_sha256']} != {task['tool_response_sha256']}"
            )
        conn.execute(
            "UPDATE task_runs SET attempt_count = attempt_count + 1 WHERE run_id = ? AND task_key = ?",
            (task["run_id"], task["task_key"]),
        )
        return

    conn.execute(
        "INSERT INTO task_runs (run_id, task_key, task_index, portfolio_id,"
        " snapshot_timestamp, input_symbols, cell_count, platform_session_id,"
        " platform_request_id, platform_tool_call_id, platform_trace_note,"
        " tool_response_ref, tool_response_sha256, returned_signal_count,"
        " status, attempt_count, last_error, started_at, finished_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task["run_id"], task["task_key"], task["task_index"],
            task["portfolio_id"], task["snapshot_timestamp"],
            json.dumps(task["input_symbols"], ensure_ascii=False),
            task["cell_count"], task.get("platform_session_id"),
            task.get("platform_request_id"), task.get("platform_tool_call_id"),
            task.get("platform_trace_note"), task["tool_response_ref"],
            task["tool_response_sha256"], task["returned_signal_count"],
            task["status"], task.get("attempt_count", 1), task.get("last_error"),
            task.get("started_at", utc_now()), task.get("finished_at"),
        ),
    )


def record_task(conn: sqlite3.Connection, task: dict) -> None:
    """单独记录一个任务行（用于失败任务或纯计数递增）。

    正常成功路径请用 commit_signals(..., task=...)，把 completed 状态与信号
    放进同一个事务；本函数单独提交，不承担信号写入。
    """
    with transaction(conn):
        _write_task_row(conn, task)


def _log_attempt(conn: sqlite3.Connection, run_id: str, task_key: str,
                  attempt: int, outcome: str, event_id: str | None,
                  detail: str) -> None:
    # 每条信号的每次尝试都单独留一行，不能用 (run_id, task_key, attempt)
    # 做主键，否则同一任务内多条信号会互相覆盖首次写入记录。
    conn.execute(
        "INSERT INTO attempt_log"
        " (run_id, task_key, attempt, outcome, event_id, detail, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_id, task_key, attempt, outcome, event_id, detail, utc_now()),
    )


def _record_failure(conn: sqlite3.Connection, *, run_id: str, task_key: str,
                     attempt: int, outcome: str, event_id: str | None,
                     detail: str) -> None:
    """失败/冲突单独保存：业务事务回滚后这条仍在，便于追查。

    留证失败不得掩盖原始异常，因此这里吞掉留证自身的异常。
    """
    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO task_failures"
                " (run_id, task_key, attempt, outcome, event_id, detail, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, task_key, attempt, outcome, event_id,
                 detail[:1000], utc_now()),
            )
    except sqlite3.Error:
        pass


def commit_signals(conn: sqlite3.Connection, *, run_id: str, task_key: str,
                   signals: list[dict], attempt: int = 1,
                   task: dict | None = None) -> dict:
    """原子提交一个任务的最终信号，返回本次各 outcome 的计数。

    - 事务**开始前**完成结构校验，校验失败不会碰数据库；
    - 事务内依次写：任务行（可选，status=completed）→ 全部信号 → 全部尝试日志；
    - 任意一步冲突或异常：ROLLBACK 整批，任务行也不会留下 completed 半成品；
    - 回滚后把失败原因写进 task_failures，单独留证。

    `task` 给定时，必须包含 record_task 所需的完整字段，且状态应为 completed。
    调用方需保证调用本函数时连接上没有挂起的写事务。
    """
    if not isinstance(signals, list):
        raise PayloadValidationError("工具结果不是数组，拒绝写入")
    for signal in signals:
        if not isinstance(signal, dict):
            raise PayloadValidationError(f"信号不是对象：{signal!r}")
        missing = [f for f in SIGNAL_FIELDS if f not in signal]
        if missing:
            raise PayloadValidationError(f"平台信号缺字段 {missing}：{signal}")
    if task is not None and task.get("status") != "completed":
        raise PayloadValidationError("同事务写入的任务行状态必须是 completed")

    counts = {"inserted": 0, "idempotent_hit": 0, "conflict": 0}
    conn.execute("BEGIN IMMEDIATE")
    try:
        if task is not None:
            _write_task_row(conn, {**task, "run_id": run_id, "task_key": task_key})
        for signal in signals:
            event_id = signal["event_id"]
            existing = conn.execute(
                "SELECT * FROM final_signals WHERE run_id = ? AND event_id = ?",
                (run_id, event_id),
            ).fetchone()
            if existing is not None:
                same = all(str(existing[f]) == str(signal[f]) for f in SIGNAL_FIELDS[1:])
                same = same and existing["task_key"] == task_key
                if same:
                    counts["idempotent_hit"] += 1
                    _log_attempt(conn, run_id, task_key, attempt,
                                  "idempotent_hit", event_id,
                                  "内容一致，幂等返回，不覆盖原始依据")
                    continue
                counts["conflict"] += 1
                _log_attempt(conn, run_id, task_key, attempt,
                              "conflict", event_id,
                              "同一 event_id 内容冲突，拒绝覆盖")
                raise SignalConflictError(
                    f"event_id={event_id} 已存在但内容不同，拒绝覆盖。"
                    f" 库中={dict(existing)} 重投={signal}"
                )
            conn.execute(
                "INSERT INTO final_signals (run_id, task_key, event_id, portfolio_id,"
                " timestamp, symbol, rule, price, threshold, source_attempt, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, task_key, event_id, signal["portfolio_id"],
                 signal["timestamp"], signal["symbol"], signal["rule"],
                 signal["price"], signal["threshold"], attempt, utc_now()),
            )
            counts["inserted"] += 1
            _log_attempt(conn, run_id, task_key, attempt,
                          "inserted", event_id, "首次写入")
        conn.execute("COMMIT")
    except BaseException as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        _record_failure(
            conn, run_id=run_id, task_key=task_key, attempt=attempt,
            outcome=type(exc).__name__, event_id=None, detail=str(exc),
        )
        raise
    return counts


def count_signals(conn: sqlite3.Connection, run_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM final_signals WHERE run_id = ?", (run_id,)
    ).fetchone()[0]


def list_signals(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    """只读查询 + 稳定排序。不使用 DISTINCT、不过滤、不补信号、不重算阈值。"""
    rows = conn.execute(
        "SELECT event_id, portfolio_id, timestamp, symbol, rule, price, threshold"
        " FROM final_signals WHERE run_id = ?"
        " ORDER BY timestamp, portfolio_id, symbol, rule, event_id",
        (run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def list_signals_for_task(conn: sqlite3.Connection, run_id: str,
                          task_key: str) -> list[dict]:
    """按任务读取原始信号行，供对账逐字段核对载荷与库的一致性。"""
    rows = conn.execute(
        "SELECT event_id, portfolio_id, timestamp, symbol, rule, price, threshold"
        " FROM final_signals WHERE run_id = ? AND task_key = ?"
        " ORDER BY timestamp, portfolio_id, symbol, rule, event_id",
        (run_id, task_key),
    ).fetchall()
    return [dict(row) for row in rows]


def list_failures(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    return [dict(row) for row in conn.execute(
        "SELECT * FROM task_failures WHERE run_id = ? ORDER BY id", (run_id,)
    ).fetchall()]


def query_run(conn: sqlite3.Connection, run_id: str) -> dict:
    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"库中找不到 run_id={run_id}")
    tasks = [dict(row) for row in conn.execute(
        "SELECT * FROM task_runs WHERE run_id = ? ORDER BY task_index", (run_id,)
    ).fetchall()]
    attempts = [dict(row) for row in conn.execute(
        "SELECT * FROM attempt_log WHERE run_id = ? ORDER BY task_key, attempt",
        (run_id,),
    ).fetchall()]
    status_counts: dict[str, int] = {}
    for task in tasks:
        status_counts[task["status"]] = status_counts.get(task["status"], 0) + 1
    return {
        "run": dict(run),
        "task_count": len(tasks),
        "task_status_counts": status_counts,
        "zero_hit_task_count": sum(
            1 for t in tasks if t["returned_signal_count"] == 0
        ),
        "cell_count": sum(t["cell_count"] for t in tasks),
        "platform_identified_task_count": sum(
            1 for t in tasks
            if any(t.get(k) for k in ("platform_session_id", "platform_request_id",
                                     "platform_tool_call_id"))
        ),
        "raw_signal_row_count": count_signals(conn, run_id),
        "signals": list_signals(conn, run_id),
        "tasks": tasks,
        "attempts": attempts,
        "failures": list_failures(conn, run_id),
    }
