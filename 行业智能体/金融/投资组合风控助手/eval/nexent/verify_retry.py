"""重试验证：模拟「写库已成功、调用方未收到确认」后用相同 run_id / 任务标识重投。

要求：允许调用次数增加，库中最终信号仍应为 20 条，事件内容与原始依据不被覆盖；
同价不同时刻的合法信号（如 T04/T18）分别保留。
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import persistence

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
RESULTS = ROOT / "eval" / "results"
DEFAULT_DB = HERE / "state" / "nexent_eval.db"


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def state(conn, run_id: str, task_key: str) -> dict:
    signals = persistence.list_signals(conn, run_id)
    content = sha256_bytes(json.dumps(signals, ensure_ascii=False,
                                       sort_keys=True).encode("utf-8"))
    attempts = [dict(row) for row in conn.execute(
        "SELECT * FROM attempt_log WHERE run_id = ? AND task_key = ?"
        " ORDER BY attempt", (run_id, task_key)
    ).fetchall()]
    task = conn.execute(
        "SELECT * FROM task_runs WHERE run_id = ? AND task_key = ?",
        (run_id, task_key),
    ).fetchone()
    return {
        "raw_signal_row_count": persistence.count_signals(conn, run_id),
        "signals_sha256": content,
        "event_ids": [s["event_id"] for s in signals],
        "signals": signals,
        "attempt_log": attempts,
        "task_attempt_count": task["attempt_count"] if task else None,
        "tool_response_sha256": task["tool_response_sha256"] if task else None,
        "tool_response_ref": task["tool_response_ref"] if task else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--task-key", required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--records-root", type=Path, default=ROOT,
                        help="tool_response_ref 的解析根目录")
    parser.add_argument("--output", type=Path,
                        default=RESULTS / "nexent_retry_evidence.json")
    args = parser.parse_args()

    conn = persistence.connect(args.db)
    before = state(conn, args.run_id, args.task_key)
    ref = before["tool_response_ref"]
    if not ref or ref == "(none)":
        print("该任务没有原始载荷引用，无法重投。", file=sys.stderr)
        return 2
    ref_path = Path(ref)
    record_path = ref_path if ref_path.is_absolute() else args.records_root / ref
    record = json.loads(record_path.read_text(encoding="utf-8"))
    signals = record["tool_result"]
    attempt = (before["task_attempt_count"] or 0) + 1

    persistence.record_task(conn, {
        "run_id": args.run_id, "task_key": args.task_key,
        "tool_response_sha256": before["tool_response_sha256"],
    })
    outcomes = persistence.commit_signals(
        conn, run_id=args.run_id, task_key=args.task_key,
        signals=signals, attempt=attempt,
    )
    after = state(conn, args.run_id, args.task_key)
    conn.close()

    same_price_diff_time = [
        s for s in after["signals"] if s["price"] == "1.8999"
    ]
    checks = [
        {"check": "库内原始信号行数不变", "ok": before["raw_signal_row_count"]
         == after["raw_signal_row_count"] == 20,
         "detail": f"{before['raw_signal_row_count']} -> {after['raw_signal_row_count']}"},
        {"check": "事件内容未被改写（哈希一致）", "ok": before["signals_sha256"]
         == after["signals_sha256"],
         "detail": f"{before['signals_sha256']} -> {after['signals_sha256']}"},
        {"check": "event_id 集合不变", "ok": before["event_ids"] == after["event_ids"],
         "detail": f"{len(before['event_ids'])} -> {len(after['event_ids'])}"},
        {"check": "调用次数增加（任务 attempt_count）", "ok": after["task_attempt_count"]
         == before["task_attempt_count"] + 1,
         "detail": f"{before['task_attempt_count']} -> {after['task_attempt_count']}"},
        {"check": "尝试记录条数增加", "ok": len(after["attempt_log"])
         > len(before["attempt_log"]),
         "detail": f"{len(before['attempt_log'])} -> {len(after['attempt_log'])}"},
        {"check": "重投全部幂等返回，无新增、无冲突",
         "ok": outcomes["inserted"] == 0 and outcomes["conflict"] == 0
         and outcomes["idempotent_hit"] == len(signals),
         "detail": json.dumps(outcomes, ensure_ascii=False)},
        {"check": "原始载荷引用哈希未被覆盖",
         "ok": before["tool_response_sha256"] == after["tool_response_sha256"],
         "detail": f"{before['tool_response_sha256']} -> {after['tool_response_sha256']}"},
        {"check": "同价不同时刻的合法信号分别保留（1.8999）",
         "ok": len(same_price_diff_time) == 2
         and len({s["timestamp"] for s in same_price_diff_time}) == 2
         and len({s["event_id"] for s in same_price_diff_time}) == 2,
         "detail": json.dumps(same_price_diff_time, ensure_ascii=False)},
    ]
    passed = all(check["ok"] for check in checks)
    evidence = {
        "schema": "topic16-retry-evidence-v1",
        "run_id": args.run_id,
        "task_key": args.task_key,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": "retry-verification",
        "pass": passed,
        "checks": checks,
        "before": {
            "raw_signal_row_count": before["raw_signal_row_count"],
            "signals_sha256": before["signals_sha256"],
            "task_attempt_count": before["task_attempt_count"],
            "attempt_log": before["attempt_log"],
            "signals": before["signals"],
        },
        "after": {
            "raw_signal_row_count": after["raw_signal_row_count"],
            "signals_sha256": after["signals_sha256"],
            "task_attempt_count": after["task_attempt_count"],
            "attempt_log": after["attempt_log"],
            "signals": after["signals"],
        },
        "replayed_tool_response_ref": ref,
        "replayed_attempt": attempt,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for check in checks:
        print(f"  [{'OK' if check['ok'] else 'FAIL'}] {check['check']} — {check['detail'][:160]}")
    print(f"重试验证：{'PASS' if passed else 'FAIL'}  证据：{args.output}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
