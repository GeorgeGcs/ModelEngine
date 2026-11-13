"""MCP delivery of persisted scheduler signals to one configured recipient."""

import argparse
import json
import os
from pathlib import Path
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import StrictStr

from app.clawbot import ClawBotGateway
from app.gateway import LocalMockGateway
from app.service import Service
from app.store import Store
from integration.dispatch import read_key
from integration.wechat import verify_authorization


server = MCPServer("Topic16 WeChat Delivery")


@server.tool(annotations=ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
))
def send_wechat_risk_notification(
    event_id: StrictStr, run_id: StrictStr, authorization: StrictStr
) -> Annotated[CallToolResult, dict]:
    """Push one persisted risk event. The deployment supplies the recipient and channel."""
    try:
        key = read_key(os.environ["TOPIC16_WECHAT_MCP_KEY_FILE"])
        path = Path(os.environ["TOPIC16_WECHAT_MCP_DB"])
        mode = os.environ["TOPIC16_WECHAT_MCP_TRANSPORT"]
        recipient = os.environ["TOPIC16_RECIPIENT_ID"]
        window = int(os.environ.get("TOPIC16_WECHAT_WINDOW_SECONDS", "60"))
        maximum = int(os.environ.get("TOPIC16_WECHAT_MAX_PER_WINDOW", "3"))
        if not path.is_file() or not recipient.strip() or window <= 0 or maximum <= 0:
            raise ValueError("invalid delivery configuration")
        if mode not in ("clawbot", "local-mock"):
            raise ValueError("unknown transport")
        if not verify_authorization(key, event_id, run_id, authorization,
                                    [mode, recipient, window, maximum]):
            raise ValueError("invalid authorization")
        gateway = ClawBotGateway() if mode == "clawbot" else LocalMockGateway()
    except (KeyError, ValueError, OSError, TypeError) as exc:
        raise ToolError("微信推送授权或部署配置无效") from exc
    store = Store(path)
    signal = store.get_signal(event_id)
    if not signal or signal["run_id"] != run_id or signal["detail"].get("mode") != "nexent-price":
        raise ToolError("未找到匹配的已落库原生风险事件")
    with store.connection() as conn:
        previous = conn.execute(
            "SELECT gateway_id FROM push_records WHERE event_id=? AND recipient_id=?",
            (event_id, recipient)).fetchone()
    if previous and previous["gateway_id"] != gateway.gateway_id:
        raise ToolError("原推送记录与当前网关不符")
    service = Service(store, gateway, recipient_id=recipient,
                      window_seconds=window, max_per_window=maximum)
    signal.update(portfolio_name=signal["detail"].get("wechat_portfolio_name", signal["portfolio_id"]),
                  portfolio_type=signal["detail"].get("wechat_portfolio_type", ""),
                  risk_type_name=signal["risk_type"])
    pushed = service._push(signal)
    if pushed["duplicate"] and pushed["status"] == "failed":
        retry = service.retry_push(event_id, source="automatic")
        if retry["retried"]:
            pushed.update(retry, attempted=True)
    result = {k: pushed[k] for k in
              ("event_id", "push_id", "status", "duplicate", "recipient_masked", "attempted")}
    result.update(run_id=run_id, gateway_id=gateway.gateway_id, simulated=gateway.simulated,
                  attempt_id=pushed.get("attempt_id"))
    return CallToolResult(content=[TextContent(text=json.dumps(result, ensure_ascii=False))],
                          structured_content={"result": result}, is_error=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8772)
    args = parser.parse_args()
    if args.transport == "streamable-http":
        server.run("streamable-http", host=args.host, port=args.port, streamable_http_path="/mcp")
    else:
        server.run("stdio")
