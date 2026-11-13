"""独立进程只读重开数据库，导出本次 run_id 下的全部最终信号。

只允许：字段映射（保持同名）与稳定排序。
禁止：DISTINCT、去重、过滤异常、补信号、重算阈值、修改任何数值。
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

QUERY = (
    "SELECT event_id, portfolio_id, timestamp, symbol, rule, price, threshold "
    "FROM final_signals WHERE run_id = ? "
    "ORDER BY timestamp, portfolio_id, symbol, rule, event_id"
)


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def portable(path: Path) -> str:
    """证据里只写仓库相对路径，不带本机绝对路径。"""
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return "<outside-repo>/" + path.name


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path,
                        default=RESULTS / "nexent_final_signals.json")
    parser.add_argument("--trace-output", type=Path,
                        default=RESULTS / "nexent_export_trace.json")
    args = parser.parse_args()

    conn = persistence.connect(args.db, read_only=True)
    try:
        row_count = persistence.count_signals(conn, args.run_id)
        signals = persistence.list_signals(conn, args.run_id)
    finally:
        conn.close()
    if len(signals) != row_count:
        raise RuntimeError(
            f"读取条数 {len(signals)} 与库内行数 {row_count} 不一致，导出中止"
        )

    document = {"signal_count": len(signals), "signals": signals}
    raw = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(raw)

    trace = {
        "schema": "topic16-export-trace-v1",
        "scope": "end-to-end-export",
        "run_id": args.run_id,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "database": portable(args.db),
        "database_sha256": sha256_bytes(args.db.read_bytes()) if args.db.exists() else None,
        "query": QUERY,
        "database_row_count": row_count,
        "exported_signal_count": len(signals),
        "output_file": portable(args.output),
        "output_sha256": sha256_bytes(raw),
        "transformations": ["字段同名映射", "按 (timestamp, portfolio_id, symbol, rule, event_id) 稳定排序"],
        "forbidden_operations_not_used": ["DISTINCT", "去重", "过滤异常", "补信号", "重算阈值", "改写数值"],
    }
    args.trace_output.parent.mkdir(parents=True, exist_ok=True)
    args.trace_output.write_text(
        json.dumps(trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"run_id={args.run_id} 库内原始行 {row_count} -> 导出 {len(signals)} 条")
    print(f"  结果：{args.output}")
    print(f"  追溯：{args.trace_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
