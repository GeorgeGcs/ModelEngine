"""驱动：按 run_id 把逻辑任务提交给真实 Nexent 北向 API 并落库。

严格边界：
- 只读取冻结行情输入与任务清单（纯输入），**不读取冻结标注文件**；
- 实跑入口同时校验输入锁与 **task_manifest.json 自身的锁**；
- 不导入被测规则包、不导入离线回放结果文件、不绕过 Nexent 直接算规则；
- 写库信号来自 SSE 流中带显式工具名 evaluate_portfolio_snapshot、
  **按同一次调用标识配对的已识别成功工具结果**，且实际入参与冻结清单一致；
  通过本地保存的原始载荷引用传递，不做数字抄写；
- 只有「工具成功 + 显式返回 []」才计零命中；tool_error / 超时 / 推理事件 /
  缺少结果 / 无法核实实际入参一律判为证据不足；
- 平台不可用时立即退出（码 2），不写库、不生成任何端到端证据文件；
- 平台流缺少结构化工具结果时退出（码 3），留下缺口报告，不猜、不补。

模式：
- `--mode smoke`（默认）：按清单顺序调用，直到拿到「一条命中 + 一条零命中」，
  交付两条能分别追到原始工具结果与数据库行的真实平台记录，然后停下。
  这是第一轮平台联调的目标，不是 20/20 通过报告。
- `--mode full`：跑完 24 个任务；通过报告仍由后续的导出 / 比对 / 对账命令链生成。
"""

import argparse
import hashlib
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
RESULTS = ROOT / "eval" / "results"
DEFAULT_DB = HERE / "state" / "nexent_eval.db"
DEFAULT_RECORDS = HERE / "platform_records"

from nexent_client import (  # noqa: E402
    PlatformConfig, PlatformUnavailable, PlatformResponseError,
    TOOL_NAME, invoke_task, probe,
)
import persistence  # noqa: E402
from task_manifest import (  # noqa: E402
    MANIFEST, verify_lock, verify_manifest_lock,
)


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"RUN-{stamp}-{uuid.uuid4().hex[:8]}"


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def trace_note(run_id: str, task_key: str, ids: dict) -> str:
    present = {k: v for k, v in ids.items() if v}
    if not any(present.get(k) for k in ("session_id", "conversation_id",
                                        "request_id", "x_request_id",
                                        "tool_call_id")):
        return (
            f"平台未返回任何可核对标识；run_id={run_id} task_key={task_key} "
            "须在平台侧补充会话/请求标识映射，否则该任务不计为具备平台证据"
        )
    return f"run_id={run_id} task_key={task_key} 平台标识={present}"


def task_row_from(task: dict, ref: str, digest: str, ids: dict,
                  count: int, run_id: str) -> dict:
    return {
        "task_index": task["task_index"],
        "portfolio_id": task["portfolio_id"],
        "snapshot_timestamp": task["timestamp"],
        "input_symbols": task["input_symbols"],
        "cell_count": len(task["cell_keys"]),
        "platform_session_id": ids.get("session_id"),
        "platform_request_id": ids.get("request_id"),
        "platform_tool_call_id": ids.get("tool_call_id") or ids.get("trace_id"),
        "platform_trace_note": trace_note(run_id, task["task_key"], ids),
        "tool_response_ref": ref,
        "tool_response_sha256": digest,
        "returned_signal_count": count,
        "status": "completed",
        "finished_at": persistence.utc_now(),
    }


def write_gaps(path: Path, run_id: str, task: dict, outcome: dict) -> None:
    payload = {
        "schema": "topic16-evidence-gap-v1",
        "run_id": run_id,
        "task_key": task["task_key"],
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "insufficient-tool-evidence",
        "http_status": outcome["http_status"],
        "content_type": outcome["content_type"],
        "event_count": len(outcome["events"]),
        "evidence_status": outcome["evidence_status"],
        "response_header_ids": outcome["response_header_ids"],
        "execution_keys": outcome["execution_keys"],
        "tool_call_ids": outcome["tool_call_ids"],
        "platform_ids": outcome["platform_ids"],
        "evidence_gaps": outcome["evidence_gaps"],
        "events": outcome["events"],
        "note": "SSE 流没有可核对的工具调用/结果配对与实际入参；本次不写库、"
                "不生成端到端通过报告。需要真实工具事件样例、可关联的 MCP 服务端"
                "调用记录，或若 SSE 只给 execution_key 时该键唯一对应的执行记录"
                "（也可用与 SSE 工具调用事件相同 tool_call_id/call_id 的 MCP 回执）："
                "工具名、实际入参、返回信号、成功标记、时间戳。"
                "无法唯一关联时保持证据不足，不误报成功。",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def check_traceability(db: Path, run_id: str, picks: list[dict]) -> dict:
    """重开只读 DB，确认命中/零命中各一条真实记录都能追到平台与库行。

    每条记录必须串起：平台请求/会话 ID -> 实际工具入参 -> 成功返回值 ->
    SQLite 查询结果。执行记录补证时还要求关联键（execution_key，或工具调用
    事件的 tool_call_id/call_id）确实来自本次 SSE。
    """
    conn = persistence.connect(db, read_only=True)
    try:
        rows = []
        for pick in picks:
            path = ROOT / pick["record_ref"]
            exists = path.is_file()
            raw = path.read_bytes() if exists else b""
            record = json.loads(raw.decode("utf-8-sig")) if exists else {}
            db_rows = persistence.list_signals_for_task(
                conn, run_id, pick["task_key"])
            comparable = sorted(
                tuple(str(s.get(f)) for f in persistence.SIGNAL_FIELDS)
                for s in (record.get("tool_result") or []))
            db_comparable = sorted(
                tuple(str(s.get(f)) for f in persistence.SIGNAL_FIELDS)
                for s in db_rows)
            planned = record.get("planned_tool_arguments")
            actual = record.get("actual_tool_arguments")
            pair = record.get("tool_call_pair") or {}
            evidence = record.get("tool_evidence") or {}
            manifest_args = pick["planned_tool_arguments"]
            ids = record.get("platform_ids") or {}
            platform_id_present = bool(
                ids.get("request_id") or ids.get("x_request_id")
                or ids.get("conversation_id") or ids.get("session_id"))
            exec_key = pair.get("execution_key")
            sse_keys = record.get("execution_keys") or []
            sse_calls = record.get("tool_call_ids") or []
            if record.get("actual_arguments_source") == "execution-record":
                if pair.get("pairing") == "tool-call-id":
                    # 回执按工具调用标识关联：该标识必须来自本次 SSE 调用事件，
                    # 且不得再挂一个未出现在 SSE 里的 execution_key。
                    execution_key_ok = (not exec_key
                                        and pair.get("call_id") in sse_calls)
                else:
                    execution_key_ok = bool(exec_key) and exec_key in sse_keys
            else:
                execution_key_ok = True
            pair_ok = bool(pair.get("call_id") or exec_key)
            result_matches_db = comparable == db_comparable
            args_ok = (planned == manifest_args and isinstance(actual, dict)
                       and actual == planned)
            success_ok = (record.get("tool_success") is True
                          and evidence.get("tool_success") is True)
            traceable = (
                exists and sha256_bytes(raw) == pick["record_sha256"]
                and record.get("tool_name") == TOOL_NAME
                and result_matches_db and args_ok and success_ok
                and pair_ok and execution_key_ok and platform_id_present
            )
            rows.append({
                "task_key": pick["task_key"],
                "record_ref": pick["record_ref"],
                "record_exists": exists,
                "record_sha256": sha256_bytes(raw) if exists else None,
                "hash_matches_task_row": (
                    sha256_bytes(raw) == pick["record_sha256"] if exists else False),
                "tool_name": record.get("tool_name"),
                "platform_ids": record.get("platform_ids"),
                "platform_request_or_session_id_present": platform_id_present,
                "tool_result_count": len(record.get("tool_result") or []),
                "db_rows_for_task": len(db_rows),
                "tool_result_matches_db": result_matches_db,
                "planned_args_match_manifest": planned == manifest_args,
                "actual_args_match_planned": args_ok,
                "actual_arguments_source": record.get("actual_arguments_source"),
                "tool_success": record.get("tool_success"),
                "pair_call_id": pair.get("call_id"),
                "pair_execution_key": exec_key,
                "pair_evidence": pair,
                "pair_evidence_ok": pair_ok,
                "execution_key_from_sse_ok": execution_key_ok,
                "zero_hit_evidence_ok": (
                    bool(record.get("tool_result")) or (
                        record.get("tool_result") == []
                        and success_ok and pair_ok
                    )
                ),
                "traceable": traceable,
            })
        hit = [r for r in rows if r["tool_result_count"] > 0 and r["traceable"]]
        zero = [r for r in rows if r["tool_result_count"] == 0 and r["traceable"]]
        checks = [
            {"check": "命中记录存在且可追溯", "ok": bool(hit),
             "detail": f"命中记录 {len(hit)} 条"},
            {"check": "零命中记录存在且可追溯", "ok": bool(zero),
             "detail": f"零命中记录 {len(zero)} 条"},
            {"check": "每条记录含平台请求/会话 ID", "ok": bool(rows) and all(
                r["platform_request_or_session_id_present"] for r in rows),
             "detail": f"{sum(1 for r in rows if r['platform_request_or_session_id_present'])}/{len(rows)}"},
            {"check": "每条记录含实际工具入参与同次调用配对", "ok": bool(rows) and all(
                r["actual_args_match_planned"] and r["pair_evidence_ok"]
                for r in rows),
             "detail": f"{sum(1 for r in rows if r['actual_args_match_planned'] and r['pair_evidence_ok'])}/{len(rows)}"},
            {"check": "每条记录的成功返回值与 SQLite 查询结果一致",
             "ok": bool(rows) and all(
                 r["tool_result_matches_db"] and r["tool_success"] is True
                 for r in rows),
             "detail": f"{sum(1 for r in rows if r['tool_result_matches_db'] and r['tool_success'] is True)}/{len(rows)}"},
            {"check": "执行记录补证键来自本次 SSE 且可唯一关联",
             "ok": bool(rows) and all(r["execution_key_from_sse_ok"] for r in rows),
             "detail": f"{sum(1 for r in rows if r['execution_key_from_sse_ok'])}/{len(rows)}"},
            {"check": "零命中由工具成功、显式 [] 与配对共同证明",
             "ok": bool(rows) and all(r["zero_hit_evidence_ok"] for r in rows),
             "detail": f"{sum(1 for r in rows if r['zero_hit_evidence_ok'])}/{len(rows)}"},
        ]
        passed = bool(rows) and all(r["traceable"] for r in rows) \
            and all(c["ok"] for c in checks)
        return {
            "schema": "topic16-northbound-preflight-v2",
            "run_id": run_id,
            "scope": "northbound-preflight",
            "platform_evidence": True,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "pass": passed,
            "hit_task_found": bool(hit),
            "zero_hit_task_found": bool(zero),
            "checks": checks,
            "records": rows,
            "note": "第一轮平台联调交付物：一条命中、一条零命中，且每条都能从平台"
                    "请求/会话 ID 追到实际工具入参、成功返回值与 SQLite 查询结果。"
                    "这不是 20/20 端到端通过报告；全量 24 任务须在本报告通过后另行运行。",
        }
    finally:
        conn.close()


def smoke_preflight_gate(preflight_path: Path, run_id: str) -> tuple[bool, list[str]]:
    """全量门禁：同一 run_id 必须已有命中+零命中各一条可追溯的 smoke 预检。"""
    problems: list[str] = []
    if not preflight_path.is_file():
        return False, [f"预检报告不存在：{preflight_path.name}"]
    try:
        pre = json.loads(preflight_path.read_bytes().decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError) as exc:
        return False, [f"预检报告无法解析：{type(exc).__name__}"]
    if not isinstance(pre, dict):
        return False, ["预检报告顶层不是对象"]
    if pre.get("run_id") != run_id:
        problems.append(f"预检 run_id={pre.get('run_id')!r} 与本次 {run_id!r} 不一致")
    if pre.get("platform_evidence") is not True:
        problems.append("预检不是真实平台证据")
    if pre.get("pass") is not True:
        problems.append("预检 pass 不为 true")
    if pre.get("hit_task_found") is not True:
        problems.append("预检缺少可追溯的命中记录")
    if pre.get("zero_hit_task_found") is not True:
        problems.append("预检缺少可追溯的零命中记录")
    return not problems, problems


def resume_preflight(conn, db: Path, report_path: Path, run_id: str,
                     identity: dict, tasks: list[dict]) -> set[str]:
    """Claim the verified smoke run and return the tasks already committed to it."""
    report = json.loads(report_path.read_bytes())
    records = report.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("预检报告没有已验证的任务记录")
    with persistence.transaction(conn):
        run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not run or run["status"] != "completed":
            raise ValueError("当前数据库中找不到已完成且可接续的预检运行")
        if any(run[field] != value for field, value in identity.items()):
            raise ValueError("预检的平台、Agent、版本或冻结数据与本次全量运行不同")
        rows = conn.execute("SELECT * FROM task_runs WHERE run_id=?", (run_id,)).fetchall()
        saved = {row["task_key"]: row for row in rows}
        planned = {task["task_key"]: task for task in tasks}
        keys = {record["task_key"] for record in records}
        if keys != set(saved) or len(records) != len(saved) or not keys <= planned.keys():
            raise ValueError("预检报告、当前数据库和冻结清单中的预检任务不匹配")
        picks = []
        for record in records:
            key = record["task_key"]
            row = saved[key]
            if (row["status"] != "completed" or record["record_ref"] != row["tool_response_ref"]
                    or record["record_sha256"] != row["tool_response_sha256"]):
                raise ValueError(f"预检任务记录不一致：{key}")
            picks.append({"task_key": key, "record_ref": row["tool_response_ref"],
                          "record_sha256": row["tool_response_sha256"],
                          "planned_tool_arguments": planned[key]["tool_arguments"]})
        if not check_traceability(db, run_id, picks)["pass"]:
            raise ValueError("预检原始载荷或持久化信号复核失败")
        conn.execute("UPDATE runs SET status='running', finished_at=NULL WHERE run_id=?", (run_id,))
    return keys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--mode", choices=("smoke", "full"), default="smoke")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--records-dir", type=Path, default=DEFAULT_RECORDS)
    parser.add_argument("--status-output", type=Path,
                        default=RESULTS / "platform_status.json")
    parser.add_argument("--gaps-output", type=Path,
                        default=RESULTS / "nexent_evidence_gaps.json")
    parser.add_argument("--preflight-output", type=Path,
                        default=RESULTS / "nexent_preflight.json")
    parser.add_argument("--mcp-call-log", default=None,
                        help="可选：MCP 服务端调用记录（JSON/JSONL），用于 SSE 未暴露实际入参时补证")
    parser.add_argument("--execution-records", default=None,
                        help="可选：按 SSE execution_key 或工具调用标识查询的"
                             "平台执行记录 / MCP 回执（JSON/JSONL）")
    args = parser.parse_args()

    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

    config = PlatformConfig.from_env()
    if args.mcp_call_log:
        config.mcp_call_log = args.mcp_call_log
    if args.execution_records:
        config.execution_records = args.execution_records
    status = probe(config)
    status.update({
        "schema": "topic16-platform-status-v3",
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "needed_from_user": [
            "可访问的 Nexent 实例地址（NEXENT_BASE_URL）与 Bearer API Key（NEXENT_API_TOKEN，仅本机环境变量）",
            "在该实例已发布、且绑定 evaluate_portfolio_snapshot 的 Agent 名称（NEXENT_AGENT_NAME）",
            "若网关前缀不是 /nb/v1，用 NEXENT_API_PREFIX 指定",
            "真实工具事件样例（含工具调用事件的实际入参与同一次调用标识），"
            "或可关联的 MCP 服务端调用记录（NEXENT_MCP_CALL_LOG）",
            "若 SSE 只返回工具调用与 execution_key：该键唯一对应的平台执行记录"
            "或 MCP 服务端回执（NEXENT_EXECUTION_RECORDS），须带同一执行键（或与 SSE "
            "工具调用事件相同的 tool_call_id/call_id）、显式成功标记、实际入参与"
            "返回信号；无法唯一关联则保持证据不足",
            "可选：NEXENT_VERSION、NEXENT_MODEL、NEXENT_MCP_URL、"
            "NEXENT_HEALTH_STATUS、NEXENT_HEALTH_SERVICE、NEXENT_TOOL_EVENT_TYPES",
        ],
        "artifacts_not_generated": [
            "eval/results/nexent_final_signals.json",
            "eval/results/compare_report_end_to_end.json",
            "eval/results/nexent_task_reconciliation.json",
        ],
        "note": "平台不可用时只完成数据库、导出器与本地测试；不生成端到端通过报告。",
    })
    args.status_output.parent.mkdir(parents=True, exist_ok=True)
    args.status_output.write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    if status["status"] != "reachable":
        print("平台不可用，本次不发起回放、不写库、不生成端到端报告。")
        print(f"  卡点：{status.get('blocked_at')}")
        for check in status["checks"]:
            print(f"  [{check['check']}] ok={check['ok']} {check['detail']}")
        print("  需要提供：")
        for item in status["needed_from_user"]:
            print(f"    - {item}")
        print(f"  状态记录：{args.status_output}")
        return 2

    # 冻结输入锁 + 清单自身锁：改清单而保留旧锁时，实跑必须拒绝。
    try:
        input_digest = verify_lock(ROOT / "data" / "eval_20.json")
        manifest_digest = verify_manifest_lock()
    except ValueError as exc:
        print(f"冻结校验失败，拒绝运行：{exc}", file=sys.stderr)
        return 2
    manifest_raw = MANIFEST.read_bytes()
    manifest = json.loads(manifest_raw.decode("utf-8-sig"))
    if manifest["source_sha256"] != input_digest:
        print("任务清单与冻结输入不一致，拒绝运行。", file=sys.stderr)
        return 2
    if manifest_digest != sha256_bytes(manifest_raw):
        print("任务清单哈希与锁不一致，拒绝运行。", file=sys.stderr)
        return 2

    run_id = args.run_id or new_run_id()

    # 暂缓全量：full 必须先有同一 run_id、且命中/零命中两条记录均可追溯的 smoke 预检。
    if args.mode == "full":
        allowed, problems = smoke_preflight_gate(args.preflight_output, run_id)
        if not allowed:
            print("拒绝 --mode full：暂缓全量 24 任务。请先用同一 run_id 跑 --mode smoke，"
                  "取得一条命中、一条零命中且每条都能追到平台会话/实际入参/"
                  "成功返回值/SQLite 查询结果的预检报告。")
            for problem in problems:
                print(f"  - {problem}")
            print(f"  预检报告：{args.preflight_output}")
            return 4

    if args.mode == "full" and not args.db.is_file():
        print("拒绝全量运行：请使用 smoke 阶段的同一个数据库。")
        return 4
    conn = persistence.connect(args.db)
    reported = status.get("health", {}).get("reported_version")
    identity = dict(platform_label=config.label(), platform_version=config.version or reported,
                    agent_id=config.agent_name, source_input_sha256=input_digest,
                    task_manifest_sha256=manifest_digest)
    completed_tasks = set()
    if args.mode == "full":
        try:
            completed_tasks = resume_preflight(conn, args.db, args.preflight_output,
                                               run_id, identity, manifest["tasks"])
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            conn.close()
            print(f"拒绝全量运行：{exc}")
            return 4
    else:
        persistence.init_db(conn)
        persistence.start_run(conn, run_id=run_id, **identity,
                              note="真实 Nexent 北向 API 编排，预检运行。")
    print(f"run_id={run_id} 平台={config.label()} "
          f"版本={config.version or reported} agent={config.agent_name} "
          f"模式={args.mode}")

    picks: list[dict] = []
    failures: list[tuple[str, str]] = []
    has_hit = has_zero = False
    try:
        for task in manifest["tasks"]:
            if args.mode == "smoke" and has_hit and has_zero:
                break
            task_key = task["task_key"]
            if task_key in completed_tasks:
                continue
            try:
                outcome = invoke_task(config, run_id=run_id, task=task)
            except (PlatformUnavailable, PlatformResponseError) as exc:
                failures.append((task_key, str(exc)))
                persistence.record_task(conn, {
                    "run_id": run_id, "task_key": task_key,
                    "task_index": task["task_index"],
                    "portfolio_id": task["portfolio_id"],
                    "snapshot_timestamp": task["timestamp"],
                    "input_symbols": task["input_symbols"],
                    "cell_count": len(task["cell_keys"]),
                    "tool_response_ref": "(none)",
                    "tool_response_sha256": sha256_bytes(b""),
                    "returned_signal_count": 0,
                    "status": "failed", "last_error": str(exc)[:500],
                    "finished_at": persistence.utc_now(),
                })
                continue

            if outcome["signals"] is None:
                write_gaps(args.gaps_output, run_id, task, outcome)
                print(f"证据不足：{task_key} 的 SSE 流没有可核对的工具调用/结果配对与实际入参。")
                print(f"  缺口报告：{args.gaps_output}")
                print("  本次不写该任务、不生成端到端通过报告。")
                persistence.finish_run(conn, run_id, "failed")
                return 3

            signals = outcome["signals"]
            evidence = outcome["tool_evidence"] or {}
            pair = {
                "call_id": evidence.get("call_id"),
                "execution_key": evidence.get("execution_key"),
                "call_event_index": evidence.get("call_event_index"),
                "result_event_index": evidence.get("result_event_index"),
                "result_field": evidence.get("result_field"),
                "pairing": evidence.get("pairing"),
            }
            record_dir = args.records_dir / run_id
            record_dir.mkdir(parents=True, exist_ok=True)
            record_path = record_dir / f"task_{task['task_index']:02d}.json"
            record = {
                "schema": "topic16-platform-tool-record-v3",
                "run_id": run_id,
                "task_key": task_key,
                "task_index": task["task_index"],
                "http_status": outcome["http_status"],
                "call_success": 200 <= outcome["http_status"] < 300,
                "evidence_status": outcome["evidence_status"],
                "tool_name": TOOL_NAME,
                "tool_arguments": task["tool_arguments"],
                "planned_tool_arguments": task["tool_arguments"],
                "actual_tool_arguments": evidence.get("actual_tool_arguments"),
                "actual_arguments_source": evidence.get("actual_arguments_source"),
                "tool_call_pair": pair,
                "tool_success": evidence.get("tool_success") is True,
                "zero_hit": bool(evidence.get("zero_hit")) and not signals,
                "platform_ids": outcome["platform_ids"],
                "response_header_ids": outcome["response_header_ids"],
                "execution_keys": outcome["execution_keys"],
                "tool_call_ids": outcome["tool_call_ids"],
                "raw_response": outcome["raw_response"],
                "tool_result": signals,
                "tool_evidence": evidence,
                "source": "nexent-northbound",
                "platform_evidence": True,
                "api_path": config.api_prefix + "/chat/run",
                "agent_name": config.agent_name,
                "notice": "原始 SSE 响应仅存本机 platform_records/，默认不提交仓库；"
                          "仓库只保留本记录的哈希与脱敏摘要。",
            }
            record_raw = (json.dumps(record, ensure_ascii=False, indent=2)
                          + "\n").encode("utf-8")
            record_path.write_bytes(record_raw)
            ref = record_path.relative_to(ROOT).as_posix()
            digest = sha256_bytes(record_raw)

            # 通过引用重新读取原始载荷，避免任何数字抄写。
            delivered = json.loads(
                record_path.read_text(encoding="utf-8"))["tool_result"]
            persistence.commit_signals(
                conn, run_id=run_id, task_key=task_key,
                signals=delivered, attempt=1,
                task=task_row_from(task, ref, digest, outcome["platform_ids"],
                                   len(delivered), run_id),
            )
            print(f"  [{task['task_index']:02d}/24] {task_key} -> "
                  f"{len(delivered)} 条  {ref}")
            picks.append({
                "task_key": task_key, "record_ref": ref,
                "record_sha256": digest, "signal_count": len(delivered),
                "tool_evidence": evidence,
                "tool_call_pair": pair,
                "planned_tool_arguments": task["tool_arguments"],
                "actual_tool_arguments": evidence.get("actual_tool_arguments"),
                "platform_ids": outcome["platform_ids"],
            })
            if delivered:
                has_hit = True
            else:
                has_zero = True
    finally:
        conn.close()

    conn2 = persistence.connect(args.db)
    persistence.finish_run(conn2, run_id, "failed" if failures else "completed")
    summary = persistence.query_run(conn2, run_id)
    print(f"完成：任务 {summary['task_count']}，状态 {summary['task_status_counts']}，"
          f"库内信号行 {summary['raw_signal_row_count']}，失败 {len(failures)}")
    conn2.close()

    if args.mode == "smoke":
        preflight = check_traceability(args.db, run_id, picks)
        # 只用可追溯记录判定命中/零命中，不用循环里的弱标记覆盖。
        preflight["loop_hit_seen"] = has_hit
        preflight["loop_zero_hit_seen"] = has_zero
        preflight["task_count_this_run"] = summary["task_count"]
        args.preflight_output.parent.mkdir(parents=True, exist_ok=True)
        args.preflight_output.write_text(
            json.dumps(preflight, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        print(f"预检可追溯性：{'PASS' if preflight['pass'] else 'FAIL'}  "
              f"报告：{args.preflight_output}")
        if not preflight["pass"]:
            return 3
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
