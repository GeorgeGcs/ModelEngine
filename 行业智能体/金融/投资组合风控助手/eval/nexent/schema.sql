-- 课题 16 端到端评测的落库结构。
-- 写入方：run_nexent_eval.py（只依据 Nexent 本次运行返回的结构化工具结果）。
-- 读取方：query_run.py / export_final_signals.py / verify_tasks.py / verify_retry.py。
-- 本层不计算规则、不生成信号、不补信号、不去重。规则 MCP 保持只读。

CREATE TABLE IF NOT EXISTS runs (
    run_id                 TEXT PRIMARY KEY,
    platform_label        TEXT,                 -- 脱敏后的实例标识，绝不含凭据
    platform_version       TEXT,
    agent_id               TEXT,
    source_input_sha256    TEXT NOT NULL,
    task_manifest_sha256   TEXT NOT NULL,
    started_at             TEXT NOT NULL,
    finished_at            TEXT,
    status                 TEXT NOT NULL DEFAULT 'running',
    note                   TEXT
);

CREATE TABLE IF NOT EXISTS task_runs (
    run_id                  TEXT NOT NULL,
    task_key                TEXT NOT NULL,      -- 组合 | 快照原始时间
    task_index              INTEGER NOT NULL,
    portfolio_id            TEXT NOT NULL,
    snapshot_timestamp      TEXT NOT NULL,      -- 原始输入时间，未归一化
    input_symbols           TEXT NOT NULL,      -- JSON 数组
    cell_count              INTEGER NOT NULL,   -- 本任务应处理的持仓格数
    platform_session_id     TEXT,
    platform_request_id     TEXT,
    platform_tool_call_id   TEXT,
    platform_trace_note     TEXT,               -- 无统一 trace_id 时的真实标识与 run_id 映射说明
    tool_response_ref       TEXT NOT NULL,      -- 平台原始结构化工具载荷的引用（文件/URL）
    tool_response_sha256    TEXT NOT NULL,      -- 原始载荷的哈希，供核对未被抄写改写
    returned_signal_count   INTEGER NOT NULL,
    status                 TEXT NOT NULL,       -- completed / failed / timeout / not_executed
    attempt_count           INTEGER NOT NULL DEFAULT 1,
    last_error              TEXT,
    started_at              TEXT NOT NULL,
    finished_at             TEXT,
    PRIMARY KEY (run_id, task_key),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS final_signals (
    run_id          TEXT NOT NULL,
    task_key        TEXT NOT NULL,
    event_id        TEXT NOT NULL,
    portfolio_id    TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    rule            TEXT NOT NULL,
    price           TEXT NOT NULL,              -- 十进制字符串，原样保留
    threshold       TEXT NOT NULL,             -- 十进制字符串，原样保留
    source_attempt  INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (run_id, event_id),            -- 同次运行内事件唯一
    FOREIGN KEY (run_id, task_key) REFERENCES task_runs(run_id, task_key)
);

CREATE INDEX IF NOT EXISTS idx_task_runs_run ON task_runs(run_id, status);
CREATE INDEX IF NOT EXISTS idx_signals_run ON final_signals(run_id);

CREATE TABLE IF NOT EXISTS attempt_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    task_key    TEXT NOT NULL,
    attempt     INTEGER NOT NULL,
    outcome     TEXT NOT NULL,   -- inserted / idempotent_hit / conflict / error
    event_id    TEXT,
    detail      TEXT,
    created_at  TEXT NOT NULL
);

-- 失败/冲突单独留证。它不参与业务事务回滚：整批信号回滚后，
-- 仍能查到「这个任务在第几次尝试、因为什么被拒绝」。
CREATE TABLE IF NOT EXISTS task_failures (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    task_key    TEXT NOT NULL,
    attempt     INTEGER NOT NULL,
    outcome     TEXT NOT NULL,   -- SignalConflictError / ValidationError / ...
    event_id    TEXT,
    detail      TEXT,
    created_at  TEXT NOT NULL
);
