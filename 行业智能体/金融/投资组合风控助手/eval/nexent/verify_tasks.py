"""任务对账：防止「任务没执行 / 库被改过 / 载荷对不上，最终报告照样通过」。

必须同时成立：
① 清单自身的 SHA256 锁有效，24 个预定逻辑任务全部完成、48 格都处理过；
② 每个任务引用的原始载荷文件真实存在，哈希、任务、工具名、实际入参、
   返回信号都能逐项验真；零命中也必须有成功调用记录和显式空列表；
③ **从 SQLite 重新读出的每一行**与导出文件的七个业务字段逐行一致；
④ 比对报告 TP=20/FP=0/FN=0、无重复/偏差，且可追溯到此次导出与冻结标注；
⑤ scope=end-to-end 时，数据必须来自真实平台，本地替身不得冒充。

本脚本不读取预期标注内容；只读 .sha256 锁的哈希值。预期答案只交给
最后的独立比对进程。
"""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import persistence

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
RESULTS = ROOT / "eval" / "results"
DEFAULT_DB = HERE / "state" / "nexent_eval.db"
DEFAULT_MANIFEST = HERE / "task_manifest.json"
DEFAULT_MANIFEST_LOCK = HERE / "task_manifest.json.sha256"
PLATFORM_ID_FIELDS = ("platform_session_id", "platform_request_id",
                       "platform_tool_call_id")
SIGNAL_FIELDS = persistence.SIGNAL_FIELDS
TOOL_NAME = "evaluate_portfolio_snapshot"
RECORD_SCHEMA = "topic16-platform-tool-record-v3"
FIXTURE_RECORD_SCHEMA = "topic16-platform-tool-record-v2"
REAL_SOURCES = ("nexent-northbound", "mcp-server-log")
ACTUAL_ARGUMENT_SOURCES = ("sse-tool-call", "mcp-server-log", "execution-record")
PLATFORM_ID_KEYS = ("conversation_id", "x_request_id", "request_id",
                    "session_id", "tool_call_id")


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def portable(path: Path) -> str:
    """证据里只写仓库相对路径，不带本机绝对路径。"""
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return "<outside-repo>/" + path.name


def add(checks: list, name: str, ok: bool, detail: str) -> None:
    checks.append({"check": name, "ok": bool(ok), "detail": detail})


def load_json(path: Path):
    return json.loads(path.read_bytes().decode("utf-8-sig")) if path.exists() else None


def canonical(signals) -> list[tuple]:
    """把信号列表规范化为七字段字符串元组，排序后用于内容等价判断。"""
    rows = []
    for signal in signals or []:
        rows.append(tuple(str(signal.get(f)) for f in SIGNAL_FIELDS))
    return sorted(rows)


def verify_record(task: dict, *, run_id: str, records_root: Path,
                  manifest_by_key: dict, scope: str,
                  db_rows_by_task: dict) -> list[str]:
    """打开任务实际引用的载荷文件，逐项验真，不做任何数值重算。"""
    key = task["task_key"]
    problems: list[str] = []
    ref = task.get("tool_response_ref") or ""
    path = Path(ref)
    if not path.is_absolute():
        path = records_root / path
    if not path.is_file():
        return [f"{key} ## 原始载荷文件缺失或不可读（{ref}）"]
    raw = path.read_bytes()
    if sha256_bytes(raw) != task.get("tool_response_sha256"):
        problems.append(f"{key} ## 载荷 SHA256 与库内记录不一致")
    try:
        record = json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:  # noqa: BLE001 - 载荷损坏必须记录原因
        return problems + [f"{key} ## 载荷不是合法 JSON（{type(exc).__name__}）"]
    if not isinstance(record, dict):
        return problems + [f"{key} ## 载荷顶层不是对象"]

    expected_schema = (RECORD_SCHEMA if scope == "end-to-end"
                       else FIXTURE_RECORD_SCHEMA)
    if record.get("schema") != expected_schema:
        problems.append(f"{key} ## 载荷 schema 不是 {expected_schema}")
    if record.get("run_id") != run_id:
        problems.append(f"{key} ## 载荷 run_id 与本次运行不一致")
    if record.get("task_key") != key:
        problems.append(f"{key} ## 载荷 task_key 与库内任务不一致")
    if record.get("tool_name") != TOOL_NAME:
        problems.append(f"{key} ## 载荷未记录 {TOOL_NAME} 工具调用")
    expected_args = (manifest_by_key.get(key) or {}).get("tool_arguments")
    if expected_args is None:
        problems.append(f"{key} ## 任务不在冻结清单中，无法核对入参")
    elif record.get("tool_arguments") != expected_args:
        problems.append(f"{key} ## 载荷中的实际工具入参与冻结清单不一致")

    status = record.get("http_status")
    if not isinstance(status, int) or not 200 <= status < 300:
        problems.append(f"{key} ## 缺少 2xx 成功调用记录（http_status={status!r}）")
    if record.get("call_success") is not True:
        problems.append(f"{key} ## 缺少显式 call_success=true")
    ids = record.get("platform_ids") or {}
    if not isinstance(ids, dict) or not any(ids.values()):
        problems.append(f"{key} ## 载荷没有可核对的平台调用标识")

    if scope == "end-to-end":
        planned = record.get("planned_tool_arguments")
        actual = record.get("actual_tool_arguments")
        pair = record.get("tool_call_pair")
        if planned != expected_args:
            problems.append(f"{key} ## 端到端：计划入参与冻结清单不一致")
        if not isinstance(actual, dict) or actual != planned:
            problems.append(f"{key} ## 端到端：实际工具入参缺失或与计划入参不一致")
        if record.get("actual_arguments_source") not in ACTUAL_ARGUMENT_SOURCES:
            problems.append(f"{key} ## 端到端：实际入参来源不可核实")
        if not isinstance(pair, dict) or not (
                pair.get("call_id") or pair.get("execution_key")) \
                or not pair.get("pairing"):
            problems.append(f"{key} ## 端到端：缺少同一次调用标识的配对证据")
        if record.get("actual_arguments_source") == "execution-record":
            exec_key = pair.get("execution_key") if isinstance(pair, dict) else None
            sse_keys = record.get("execution_keys") or []
            sse_calls = record.get("tool_call_ids") or []
            if isinstance(pair, dict) and pair.get("pairing") == "tool-call-id":
                if (exec_key or not pair.get("call_id")
                        or pair.get("call_id") not in sse_calls):
                    problems.append(
                        f"{key} ## 端到端：工具调用标识补证键必须来自本次 SSE 调用事件")
            elif not exec_key or exec_key not in sse_keys:
                problems.append(
                    f"{key} ## 端到端：execution_key 补证键必须来自本次 SSE 事件")
        if record.get("tool_success") is not True \
                or record.get("evidence_status") != "verified":
            problems.append(f"{key} ## 端到端：工具结果不是已识别成功调用")
        if not isinstance(ids, dict) or not any(
                ids.get(name) for name in PLATFORM_ID_KEYS):
            problems.append(f"{key} ## 端到端：缺少响应头/会话关联标识")

    result = record.get("tool_result")
    if not isinstance(result, list):
        problems.append(f"{key} ## tool_result 不是显式数组")
    else:
        if len(result) != task.get("returned_signal_count"):
            problems.append(
                f"{key} ## 载荷返回 {len(result)} 条，库内任务记录 "
                f"{task.get('returned_signal_count')} 条")
        if canonical(result) != canonical(db_rows_by_task.get(key, [])):
            problems.append(f"{key} ## 载荷返回信号与库中该任务的原始行不一致")
        if task.get("returned_signal_count") == 0 and result != []:
            problems.append(f"{key} ## 零命中任务的工具结果必须是显式空列表 []")
        if scope == "end-to-end" and task.get("returned_signal_count") == 0:
            pair = record.get("tool_call_pair") or {}
            if (record.get("tool_success") is not True
                    or not (pair.get("call_id") or pair.get("execution_key"))
                    or record.get("zero_hit") is not True):
                problems.append(f"{key} ## 端到端：零命中必须由工具成功、"
                                "显式 [] 与同次调用配对共同证明")

    if scope == "end-to-end":
        if record.get("platform_evidence") is not True:
            problems.append(f"{key} ## 端到端证据不允许 platform_evidence=false")
        if record.get("source") not in REAL_SOURCES:
            problems.append(f"{key} ## 端到端载荷来源不是真实平台（{record.get('source')!r}）")
        if any("FIXTURE" in str(v).upper() for v in ids.values() if v):
            problems.append(f"{key} ## 端到端载荷仍带本地替身标识")
        if "FIXTURE" in str(run_id).upper():
            problems.append(f"{key} ## 端到端 run_id 带本地替身标记")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--manifest-lock", type=Path, default=DEFAULT_MANIFEST_LOCK)
    parser.add_argument("--records-root", type=Path, default=ROOT,
                        help="tool_response_ref 的解析根目录")
    parser.add_argument("--exported", type=Path,
                        default=RESULTS / "nexent_final_signals.json")
    parser.add_argument("--compare-report", type=Path,
                        default=RESULTS / "compare_report_end_to_end.json")
    parser.add_argument("--expected-lock", type=Path,
                        default=ROOT / "eval" / "expected_20.json.sha256")
    parser.add_argument("--report", type=Path,
                        default=RESULTS / "nexent_task_reconciliation.json")
    parser.add_argument("--scope", default="end-to-end",
                        choices=("end-to-end", "local-fixture"))
    args = parser.parse_args()

    manifest = load_json(args.manifest)
    manifest_raw = args.manifest.read_bytes() if args.manifest.exists() else None
    manifest_digest = sha256_bytes(manifest_raw) if manifest_raw else None
    manifest_lock = (args.manifest_lock.read_text(encoding="utf-8").split() or [""])[0] \
        if args.manifest_lock.exists() else None
    exported = load_json(args.exported)
    compare = load_json(args.compare_report)
    exported_raw = args.exported.read_bytes() if args.exported.exists() else None
    lock = (args.expected_lock.read_text(encoding="utf-8").split() or [""])[0] \
        if args.expected_lock.exists() else None

    checks: list[dict] = []
    summary, tasks, db_all, db_rows_by_task = None, [], [], {}
    try:
        conn = persistence.connect(args.db, read_only=True)
        summary = persistence.query_run(conn, args.run_id)
        tasks = summary["tasks"]
        conn.close()
        # 第二个独立只读连接：重新读取全部原始信号行，不复用上面的汇总结果。
        conn2 = persistence.connect(args.db, read_only=True)
        db_all = persistence.list_signals(conn2, args.run_id)
        db_rows_by_task = {
            t["task_key"]: persistence.list_signals_for_task(
                conn2, args.run_id, t["task_key"])
            for t in tasks
        }
        conn2.close()
        add(checks, "数据库可查询且可独立重读原始信号", True,
            f"重读 {len(db_all)} 行，覆盖 {len(db_rows_by_task)} 个任务")
    except Exception as exc:  # noqa: BLE001
        add(checks, "数据库可查询且可独立重读原始信号", False,
            f"{type(exc).__name__}: {exc}")

    # ① 清单与冻结锁
    add(checks, "清单 SHA256 与冻结锁一致",
        bool(manifest) and manifest_raw is not None
        and manifest_digest == manifest_lock and manifest_lock is not None,
        f"清单={manifest_digest} 锁={manifest_lock}")
    manifest_keys = {t["task_key"] for t in manifest["tasks"]} if manifest else set()
    manifest_by_key = {t["task_key"]: t for t in manifest["tasks"]} if manifest else {}
    actual_keys = {t["task_key"] for t in tasks}
    add(checks, "任务清单为 24 个任务 / 48 个持仓格",
        bool(manifest) and manifest.get("task_count") == 24
        and manifest.get("cell_count") == 48,
        f"清单: {manifest.get('task_count') if manifest else '缺失'} 任务 / "
        f"{manifest.get('cell_count') if manifest else '缺失'} 格")
    add(checks, "24 个预定逻辑任务全部有执行记录",
        len(actual_keys) == 24 and actual_keys == manifest_keys,
        f"库内 {len(actual_keys)} 个，缺失 {sorted(manifest_keys - actual_keys)[:3]}")
    completed = [t for t in tasks if t["status"] == "completed"]
    add(checks, "24 个任务状态全部 completed", len(completed) == 24,
        f"completed={len(completed)}；状态分布={summary['task_status_counts'] if summary else {}}")
    add(checks, "48 个持仓格都经过处理",
        bool(summary) and summary["cell_count"] == 48
        and all(t["cell_count"] == len(json.loads(t["input_symbols"])) for t in tasks),
        f"合计 cell_count={summary['cell_count'] if summary else 'n/a'}")
    with_ids = [
        t for t in tasks if any(t.get(field) for field in PLATFORM_ID_FIELDS)
        and t.get("tool_response_ref") not in (None, "", "(none)")
        and t.get("tool_response_sha256")
    ]
    add(checks, "每个任务都有可核对的平台标识与原始载荷引用",
        len(with_ids) == 24, f"具备={len(with_ids)}/24")

    # ② 载荷逐个验真
    record_problems: list[str] = []
    for task in tasks:
        record_problems.extend(verify_record(
            task, run_id=args.run_id, records_root=args.records_root,
            manifest_by_key=manifest_by_key, scope=args.scope,
            db_rows_by_task=db_rows_by_task,
        ))
    add(checks, "24 个任务的原始载荷文件均通过验真",
        len(tasks) == 24 and not record_problems,
        f"问题数={len(record_problems)}；样例={record_problems[:3]}")
    pairing_problems = [p for p in record_problems if "## 端到端" in p]
    add(checks, "端到端载荷含计划/实际入参与同次调用配对证据",
        args.scope != "end-to-end" or (len(tasks) == 24 and not pairing_problems),
        f"scope={args.scope}；问题数={len(pairing_problems)}；"
        f"样例={pairing_problems[:3]}")
    zero_hit = [t for t in tasks if t["returned_signal_count"] == 0]
    zero_problem_keys = {p.split(" ## ", 1)[0] for p in record_problems}
    zero_ok = bool(zero_hit) and all(
        t["status"] == "completed" and any(t.get(f) for f in PLATFORM_ID_FIELDS)
        and t.get("tool_response_ref") not in (None, "", "(none)")
        and t["task_key"] not in zero_problem_keys
        for t in zero_hit
    )
    add(checks, "零命中任务具备平台成功完成证据",
        zero_ok, f"零命中任务={len(zero_hit)}，具备证据="
        f"{sum(1 for t in zero_hit if t['status'] == 'completed' and any(t.get(f) for f in PLATFORM_ID_FIELDS) and t['task_key'] not in zero_problem_keys)}")
    add(checks, "无失败 / 超时 / 未执行任务",
        bool(summary) and all(
            status == "completed" for status in summary["task_status_counts"]
        ), f"状态分布={summary['task_status_counts'] if summary else {}}")
    add(checks, "run 状态为 completed",
        bool(summary) and summary["run"]["status"] == "completed",
        str(summary["run"]["status"]) if summary else "n/a")

    # ③ 逐行内容比对（不看条数了事）
    exported_signals = exported.get("signals") if isinstance(exported, dict) else None
    content_problems: list[str] = []
    if not isinstance(exported_signals, list):
        content_problems.append("导出文件缺少 signals 数组")
    elif len(db_all) != len(exported_signals):
        content_problems.append(
            f"重读行数 {len(db_all)} != 导出条数 {len(exported_signals)}")
    else:
        for index, (db_row, ex_row) in enumerate(zip(db_all, exported_signals), 1):
            if not isinstance(ex_row, dict):
                content_problems.append(f"第 {index} 行导出项不是对象")
                continue
            for field in SIGNAL_FIELDS:
                if str(db_row[field]) != str(ex_row.get(field)):
                    content_problems.append(
                        f"第 {index} 行 {field}: 库={db_row[field]!r} "
                        f"导出={ex_row.get(field)!r}")
                    break
    add(checks, "数据库原始行与导出信号逐行七字段一致",
        not content_problems,
        f"逐行比对 {len(db_all)} 行；问题={content_problems[:3]}")
    add(checks, "导出文件 signal_count 与数组长度一致",
        isinstance(exported, dict) and isinstance(exported_signals, list)
        and exported.get("signal_count") == len(exported_signals),
        f"signal_count={exported.get('signal_count') if exported else 'n/a'} "
        f"数组={len(exported_signals) if isinstance(exported_signals, list) else 'n/a'}")

    # ④ 行数、比对报告与可追溯性
    row_count = summary["raw_signal_row_count"] if summary else -1
    exported_count = exported.get("signal_count") if exported else None
    compare_count = compare.get("metrics", {}).get("actual_signals") if compare else None
    add(checks, "数据库原始行数 = 导出条数",
        exported_count is not None and row_count == exported_count,
        f"库={row_count} 导出={exported_count}")
    add(checks, "导出条数 = 比对器读到的条数",
        compare_count is not None and exported_count == compare_count,
        f"导出={exported_count} 比对器={compare_count}")
    metrics = compare.get("metrics", {}) if compare else {}
    add(checks, f"比对报告存在且 scope 为 {args.scope}",
        bool(compare) and compare.get("scope") == args.scope,
        str(compare.get("scope")) if compare else "报告缺失")
    add(checks, "比对报告使用冻结标注（哈希与锁一致）",
        bool(compare) and bool(lock) and compare.get("expected_sha256") == lock,
        f"报告={compare.get('expected_sha256') if compare else 'n/a'} 锁={lock}")
    add(checks, "比对报告可追溯到本次导出文件",
        bool(compare) and exported_raw is not None
        and compare.get("actual_sha256") == sha256_bytes(exported_raw),
        f"报告={compare.get('actual_sha256') if compare else 'n/a'} "
        f"文件={sha256_bytes(exported_raw) if exported_raw else 'n/a'}")
    add(checks, "TP=20 FP=0 FN=0 且无重复/字段偏差",
        metrics.get("TP") == 20 and metrics.get("FP") == 0
        and metrics.get("FN") == 0 and metrics.get("duplicates") == 0
        and metrics.get("mismatches") == 0,
        json.dumps(metrics, ensure_ascii=False))
    add(checks, "比对报告 pass=true 且无结构错误",
        bool(compare) and compare.get("pass") is True
        and not compare.get("structural_errors")
        and not compare.get("differences"),
        f"pass={compare.get('pass') if compare else 'n/a'} "
        f"结构错误={len(compare.get('structural_errors', [])) if compare else 'n/a'}")
    add(checks, "平台运行记录可追溯（run 含输入与清单哈希）",
        bool(summary) and bool(summary["run"]["source_input_sha256"])
        and bool(summary["run"]["task_manifest_sha256"]),
        f"输入={summary['run']['source_input_sha256'] if summary else 'n/a'} "
        f"清单={summary['run']['task_manifest_sha256'] if summary else 'n/a'}")

    # ⑤ 禁止本地替身冒充端到端
    if args.scope == "end-to-end":
        run_meta = summary["run"] if summary else {}
        real_run = (
            bool(summary)
            and "FIXTURE" not in str(run_meta.get("platform_label") or "").upper()
            and "FIXTURE" not in str(run_meta.get("note") or "").upper()
            and run_meta.get("task_manifest_sha256") == manifest_digest
            and bool(run_meta.get("platform_version"))
        )
        add(checks, "scope=end-to-end 时数据来自真实平台（禁止 fixture）",
            real_run and not record_problems,
            f"platform_label={run_meta.get('platform_label')!r} "
            f"version={run_meta.get('platform_version')!r} "
            f"清单哈希匹配={run_meta.get('task_manifest_sha256') == manifest_digest}")

    passed = all(check["ok"] for check in checks)
    report = {
        "schema": "topic16-task-reconciliation-v2",
        "scope": args.scope,
        "run_id": args.run_id,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pass": passed,
        "checks": checks,
        "evidence": {
            "database": portable(args.db),
            "task_manifest": portable(args.manifest),
            "task_manifest_sha256": manifest_digest,
            "task_manifest_lock": manifest_lock,
            "records_root": portable(args.records_root),
            "exported_signals": portable(args.exported),
            "compare_report": portable(args.compare_report),
            "exported_sha256": sha256_bytes(exported_raw) if exported_raw else None,
            "task_status_counts": summary["task_status_counts"] if summary else {},
            "zero_hit_task_count": len(zero_hit),
            "raw_signal_row_count": row_count,
            "reread_signal_row_count": len(db_all),
            "verified_record_count": len(tasks) - len(
                {p.split(" ## ", 1)[0] for p in record_problems}),
            "content_mismatches": content_problems[:20],
            "record_problems": record_problems[:20],
            "task_failures": summary["failures"] if summary else [],
        },
        "note": "逐行核对库中原始信号与导出内容，并打开每个任务的原始载荷验真；"
                "仅凭条数或最终比对报告无法证明任务执行与数据未被改写。",
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for check in checks:
        print(f"  [{'OK' if check['ok'] else 'FAIL'}] {check['check']} — {check['detail'][:160]}")
    print(f"对账结论：{'PASS' if passed else 'FAIL'}  报告：{args.report}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
