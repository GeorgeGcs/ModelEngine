"""Read-only MCP interface for the shared portfolio rule engine."""

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import StrictInt, StrictStr
from typing_extensions import TypedDict

from portfolio_risk.rules import evaluate_snapshot


class Holding(TypedDict):
    symbol: str
    stop_loss: StrictStr | StrictInt
    take_profit: StrictStr | StrictInt


class Portfolio(TypedDict):
    id: str
    holdings: list[Holding]


class Snapshot(TypedDict):
    timestamp: str
    prices: dict[str, StrictStr | StrictInt]


server = MCPServer("Portfolio Risk Rules")

# 可选调用审计：默认关闭。仅在需要给平台联调补「工具实际入参」证据时启用。
# 它只在服务端本机追加一行 JSONL，不改动任何行情/持仓数据，也不改变工具只读语义。
AUDIT_LOG: Path | None = None
CORRELATION: dict = {}


def _write_audit(tool_name: str, arguments: dict, result: list[dict]) -> None:
    if AUDIT_LOG is None:
        return
    entry = {
        "schema": "topic16-mcp-call-log-v1",
        "source": "mcp-server-log",
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool_name": tool_name,
        # 本行只在 evaluate_snapshot 成功返回后写出，可作为执行回执的显式成功标记。
        "tool_success": True,
        "arguments": arguments,
        "result": result,
        "correlation": CORRELATION,
        **CORRELATION,
    }
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


@server.tool(
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
)
def evaluate_portfolio_snapshot(portfolio: Portfolio, snapshot: Snapshot) -> Annotated[CallToolResult, list[dict]]:
    """按行情快照检查止损与止盈，含等于阈值的情形。价格使用十进制字符串或整数。

    timestamp 使用带时区的 ISO 8601 时间。返回命中的信号列表，未命中时返回空列表。
    """
    try:
        result = evaluate_snapshot(portfolio, snapshot)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    _write_audit(
        "evaluate_portfolio_snapshot",
        {"portfolio": portfolio, "snapshot": snapshot},
        result,
    )
    return CallToolResult(
        content=[TextContent(text=json.dumps(result, ensure_ascii=False))],
        structured_content={"result": result},
        is_error=False,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--transport", choices=("stdio", "sse", "streamable-http"), default="stdio"
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP listen address")
    parser.add_argument("--port", type=int, default=8768, help="HTTP listen port")
    parser.add_argument(
        "--audit-log", default=os.environ.get("PORTFOLIO_RULES_AUDIT_LOG"),
        help="可选：把每次工具调用的实际入参与返回信号追加为 JSONL（默认关闭）",
    )
    parser.add_argument(
        "--correlation", default=os.environ.get("PORTFOLIO_RULES_CORRELATION", "{}"),
        help="可选：JSON 字符串，写入审计记录用于与平台会话/run_id/task_key 关联",
    )
    args = parser.parse_args()
    if args.audit_log:
        AUDIT_LOG = Path(args.audit_log)
    try:
        CORRELATION = json.loads(args.correlation)
    except json.JSONDecodeError:
        CORRELATION = {}
    if not isinstance(CORRELATION, dict):
        CORRELATION = {}
    if args.transport == "sse":
        server.run("sse", host=args.host, port=args.port, sse_path="/sse")
    elif args.transport == "streamable-http":
        # MCP SDK 2.3.0 的现代 HTTP transport；Nexent Remote URL 按实例版本选择。
        server.run(
            "streamable-http", host=args.host, port=args.port,
            streamable_http_path="/mcp",
        )
    else:
        server.run("stdio")
