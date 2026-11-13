"""Replay synthetic market snapshots against portfolio thresholds."""

import argparse
import json
import sys
from pathlib import Path

from .rules import evaluate_snapshot


def replay(document: dict) -> dict:
    if not isinstance(document, dict):
        raise ValueError("回放文件须为包含 portfolios 和 snapshots 的 JSON 对象")

    portfolios = document.get("portfolios")
    snapshots = document.get("snapshots")
    if not isinstance(portfolios, list) or not portfolios:
        raise ValueError("portfolios 须为非空数组")
    if not isinstance(snapshots, list) or not snapshots:
        raise ValueError("snapshots 须为非空数组")

    seen_portfolios = set()
    for portfolio in portfolios:
        portfolio_id = portfolio.get("id") if isinstance(portfolio, dict) else None
        if not isinstance(portfolio_id, str) or not portfolio_id.strip():
            raise ValueError("每个组合须有非空字符串 id")
        if portfolio_id in seen_portfolios:
            raise ValueError(f"组合 id 重复：{portfolio_id}")
        seen_portfolios.add(portfolio_id)

    signals = []
    for position, snapshot in enumerate(snapshots, start=1):
        for portfolio in portfolios:
            try:
                signals.extend(evaluate_snapshot(portfolio, snapshot))
            except ValueError as exc:
                raise ValueError(f"快照 {position}，组合 {portfolio['id']}：{exc}") from exc

    return {
        "portfolio_count": len(portfolios),
        "snapshot_count": len(snapshots),
        "signal_count": len(signals),
        "signals": signals,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="回放模拟行情，输出止损和止盈规则命中。")
    parser.add_argument("input", type=Path, help="包含 portfolios 和 snapshots 的 JSON 文件")
    args = parser.parse_args()
    try:
        document = json.loads(args.input.read_text(encoding="utf-8-sig"))
        result = replay(document)
    except (OSError, ValueError) as exc:
        print(f"回放失败：{exc}", file=sys.stderr)
        return 1
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
