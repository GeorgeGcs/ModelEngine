"""本地 fixture 机制自检演示：产出可复现的任务统计/查库/复算/重试证据。

⚠️ 全部产物 scope = local-fixture，platform_evidence = false。
这些只是「数据库、导出器、对账脚本、重试逻辑」的自检证据，
不是 Nexent 平台运行证据，也不生成任何 end-to-end 通过报告。
真实平台实跑请用 run_nexent_eval.py。
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
RESULTS = ROOT / "eval" / "results"
STATE = HERE / "state"
DB = STATE / "fixture_demo.db"
RETRY_DB = STATE / "fixture_retry.db"
RECORDS = STATE / "fixture_records"

RUN_ID = "LOCAL-FIXTURE-RUN-0001"
SCOPE = "local-fixture"
EXPECTED = ROOT / "eval" / "expected_20.json"
LOCK = ROOT / "eval" / "expected_20.json.sha256"

import persistence  # noqa: E402


def run_script(relative: str, *args) -> int:
    completed = subprocess.run(
        [sys.executable, str(ROOT / relative), *[str(a) for a in args]],
        cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if completed.stdout.strip():
        print(completed.stdout.strip()[:1200])
    if completed.returncode != 0:
        print(f"  [退出码 {completed.returncode}] {completed.stderr.strip()[:600]}")
    return completed.returncode


def build_fixture_db() -> tuple[str, str]:
    fixture = json.loads(
        (HERE / "tests/fixtures/platform_responses.json").read_text(encoding="utf-8")
    )
    manifest = json.loads((HERE / "task_manifest.json").read_text(encoding="utf-8"))
    by_key = {task["task_key"]: task for task in manifest["tasks"]}

    STATE.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        DB.unlink()
    conn = persistence.connect(DB)
    persistence.init_db(conn)
    persistence.start_run(
        conn, run_id=RUN_ID, platform_label="LOCAL-FIXTURE",
        platform_version=None, agent_id=None,
        source_input_sha256=manifest["source_sha256"],
        task_manifest_sha256="fixture-manifest",
        note="LOCAL FIXTURE — 非平台证据，仅用于机制自检",
    )
    for record in fixture["tasks"]:
        task = by_key[record["task_key"]]
        rel = f"fixture_records/task_{record['task_index']:02d}.json"
        path = STATE / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = (json.dumps({
            "schema": "topic16-platform-tool-record-v2",
            "run_id": RUN_ID, "task_key": record["task_key"],
            "task_index": record["task_index"],
            "http_status": 200,
            "call_success": True,
            "tool_name": "evaluate_portfolio_snapshot",
            "tool_arguments": task["tool_arguments"],
            "platform_ids": record["platform_ids"],
            "raw_response": record["raw_response"],
            "tool_result": record["tool_result"],
            "source": "local-fixture",
            "platform_evidence": False,
            "notice": "LOCAL FIXTURE — 非真实 Nexent 平台记录，不得作为验收证据。",
        }, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        path.write_bytes(raw)
        ids = record["platform_ids"]
        task_row = {
            "task_index": record["task_index"],
            "portfolio_id": task["portfolio_id"],
            "snapshot_timestamp": task["timestamp"],
            "input_symbols": task["input_symbols"],
            "cell_count": len(task["cell_keys"]),
            "platform_session_id": ids["session_id"],
            "platform_request_id": ids["request_id"],
            "platform_tool_call_id": ids["tool_call_id"],
            "platform_trace_note": f"LOCAL FIXTURE run_id={RUN_ID} task={record['task_key']}",
            "tool_response_ref": rel,
            "tool_response_sha256": hashlib.sha256(raw).hexdigest(),
            "returned_signal_count": len(record["tool_result"]),
            "status": "completed",
            "finished_at": persistence.utc_now(),
        }
        # completed 任务行与全部信号在同一事务提交，避免留下半成品。
        persistence.commit_signals(
            conn, run_id=RUN_ID, task_key=record["task_key"],
            signals=record["tool_result"], attempt=1, task=task_row,
        )
    persistence.finish_run(conn, RUN_ID, "completed")
    conn.close()
    with_signal = next(r["task_key"] for r in fixture["tasks"] if r["tool_result"])
    zero_hit = next(r["task_key"] for r in fixture["tasks"] if not r["tool_result"])
    return with_signal, zero_hit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-retry", action="store_true")
    args = parser.parse_args()

    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    RESULTS.mkdir(parents=True, exist_ok=True)
    with_signal, zero_hit = build_fixture_db()
    print(f"[0/5] 构造本地 fixture DB：{DB.relative_to(ROOT).as_posix()} "
          f"run_id={RUN_ID}（非平台证据）")

    query_out = RESULTS / "local_fixture_query_run.json"
    exported = RESULTS / "local_fixture_final_signals.json"
    trace = RESULTS / "local_fixture_export_trace.json"
    compare = RESULTS / "local_fixture_compare_report.json"
    recon = RESULTS / "local_fixture_task_reconciliation.json"
    retry = RESULTS / "local_fixture_retry_evidence.json"

    print("[1/5] 按 run_id 查库")
    codes = {"query": run_script(
        "eval/nexent/query_run.py", "--run-id", RUN_ID, "--db", DB,
        "--output", query_out)}
    print("[2/5] 只读重开 DB 并导出")
    codes["export"] = run_script(
        "eval/nexent/export_final_signals.py", "--run-id", RUN_ID, "--db", DB,
        "--output", exported, "--trace-output", trace)
    print("[3/5] 独立比对（scope=local-fixture）")
    codes["compare"] = run_script(
        "eval/compare.py", "--expected", EXPECTED, "--actual", exported,
        "--report", compare, "--scope", SCOPE)
    print("[4/5] 24 任务对账（scope=local-fixture）")
    codes["verify"] = run_script(
        "eval/nexent/verify_tasks.py", "--run-id", RUN_ID, "--db", DB,
        "--exported", exported, "--compare-report", compare,
        "--expected-lock", LOCK, "--report", recon, "--scope", SCOPE,
        "--records-root", STATE, "--manifest-lock",
        HERE / "task_manifest.json.sha256")
    print("[5/5] 幂等重试对照")
    if RETRY_DB.exists():
        RETRY_DB.unlink()
    shutil.copyfile(DB, RETRY_DB)
    codes["retry"] = run_script(
        "eval/nexent/verify_retry.py", "--run-id", RUN_ID,
        "--task-key", with_signal, "--db", RETRY_DB,
        "--records-root", STATE, "--output", retry)

    def load(path):
        return json.loads(path.read_bytes().decode("utf-8-sig")) if path.exists() else None

    summary = {
        "schema": "topic16-local-fixture-summary-v1",
        "scope": SCOPE,
        "platform_evidence": False,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_id": RUN_ID,
        "notice": "本地替身自检，不是 Nexent 平台运行证据；未生成 end-to-end 通过报告。",
        "exit_codes": codes,
        "task_stats": {
            "task_count": load(query_out)["task_count"] if load(query_out) else None,
            "task_status_counts": load(query_out)["task_status_counts"] if load(query_out) else None,
            "zero_hit_task_count": load(query_out)["zero_hit_task_count"] if load(query_out) else None,
            "cell_count": load(query_out)["cell_count"] if load(query_out) else None,
            "platform_identified_task_count": load(query_out)["platform_identified_task_count"] if load(query_out) else None,
            "raw_signal_row_count": load(query_out)["raw_signal_row_count"] if load(query_out) else None,
        },
        "compare_metrics": load(compare)["metrics"] if load(compare) else None,
        "reconciliation_pass": load(recon)["pass"] if load(recon) else None,
        "retry_pass": load(retry)["pass"] if load(retry) else None,
        "retry_before_rows": load(retry)["before"]["raw_signal_row_count"] if load(retry) else None,
        "retry_after_rows": load(retry)["after"]["raw_signal_row_count"] if load(retry) else None,
        "zero_hit_task_example": zero_hit,
        "retried_task": with_signal,
        "artifacts": [p.relative_to(ROOT).as_posix() for p in
                      (query_out, exported, trace, compare, recon, retry)],
        "artifacts_not_generated": [
            "eval/results/nexent_final_signals.json",
            "eval/results/compare_report_end_to_end.json",
            "eval/results/nexent_task_reconciliation.json",
        ],
    }
    out = RESULTS / "local_fixture_summary.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"汇总：{out.relative_to(ROOT).as_posix()}")
    print(f"  退出码：{codes}")
    print("  注意：以上全部是 local-fixture 自检，真实平台证据仍为「待平台实跑」。")
    return 0 if all(v == 0 for v in codes.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
