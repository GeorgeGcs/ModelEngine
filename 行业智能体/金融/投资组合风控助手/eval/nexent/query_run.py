"""按 run_id 查库：任务执行统计 + 最终信号明细。"""

import argparse
import json
import sys
from pathlib import Path

import persistence

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DEFAULT_DB = HERE / "state" / "nexent_eval.db"


def main() -> int:
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=None,
                        help="可选：把完整查询结果写成 JSON")
    args = parser.parse_args()

    conn = persistence.connect(args.db, read_only=True)
    try:
        summary = persistence.query_run(conn, args.run_id)
    finally:
        conn.close()

    print(f"run_id          : {args.run_id}")
    print(f"平台            : {summary['run']['platform_label']} "
          f"版本={summary['run']['platform_version']}")
    print(f"运行状态        : {summary['run']['status']}")
    print(f"输入/清单哈希   : {summary['run']['source_input_sha256']} / "
          f"{summary['run']['task_manifest_sha256']}")
    print(f"任务数          : {summary['task_count']}  状态分布="
          f"{summary['task_status_counts']}")
    print(f"零命中任务数    : {summary['zero_hit_task_count']}")
    print(f"持仓格          : {summary['cell_count']}")
    print(f"有平台标识任务  : {summary['platform_identified_task_count']}/{summary['task_count']}")
    print(f"库内原始信号行  : {summary['raw_signal_row_count']}")
    print(f"尝试记录        : {len(summary['attempts'])}")
    print("零命中任务明细：")
    for task in summary["tasks"]:
        if task["returned_signal_count"] == 0:
            print(f"  [{task['task_index']:02d}] {task['task_key']} "
                  f"status={task['status']} ref={task['tool_response_ref']}")
    print("信号明细：")
    for signal in summary["signals"]:
        print(f"  {signal['timestamp']} {signal['portfolio_id']} {signal['symbol']} "
              f"{signal['rule']} price={signal['price']} threshold={signal['threshold']} "
              f"event_id={signal['event_id'][:12]}…")

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"完整结果：{args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
