"""Read-only MCP interface for the deterministic news-risk assessor.

与 mcps/portfolio_rules 完全独立：不导入、不修改止盈止损链路的任何模块或运行态。
"""

from __future__ import annotations

import argparse
import json
from typing import Annotated, NotRequired

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import StrictStr
from typing_extensions import TypedDict

from news_risk.rules import assess_news
from news_risk.rules_v2 import assess_news_v2


def _wire(result: dict) -> CallToolResult:
    """同一份研判结果同时给文本块与结构化内容，并显式标记成功。

    2026-11-09 真实 Nexent v2.8.3 实跑表明 mcpadapt 0.1.19 会拒绝空 content，
    平台侧依赖 content[0].text 解析工具结果。只返回 dict / structuredContent
    无法保证被消费，故与规则 MCP 采用同一双通道写法：文本是可反序列化的完整 JSON，
    structuredContent.result 是同一对象，两者必须逐字一致。
    """
    return CallToolResult(
        content=[TextContent(text=json.dumps(result, ensure_ascii=False))],
        structured_content={"result": result},
        is_error=False,
    )


class NewsItem(TypedDict):
    symbol: StrictStr
    text: StrictStr
    sample_id: NotRequired[StrictStr]
    published_at: NotRequired[StrictStr]
    source: NotRequired[StrictStr]


server = MCPServer("Topic16 News Risk")


@server.tool(
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
)
def assess_news_risk(news: NewsItem) -> Annotated[CallToolResult, dict]:
    """研判一条虚构舆情，返回标的、风险判定、原文证据、等级与建议复核动作。

    symbol 为监控标的代码（SIM-A / SIM-B / SIM-C / SIM-D），text 为舆情原文。
    risk_verdict 只取 risk 或 no_risk；证据不足时降级为 no_risk 并给出待复核动作。
    本工具只做辅助研判，不输出买卖结论；不得把评测样本答案写入提示词或知识库。
    返回体同时含 content[0].text（完整 JSON 文本）与 structuredContent.result。
    """
    try:
        return _wire(assess_news(dict(news)))
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@server.tool(
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
)
def assess_news_risk_v2(news: NewsItem, entities: dict) -> Annotated[CallToolResult, dict]:
    """增强研判（动态主体）：entities 为 {标的代码: {"canonical": 主体全称}}。

    用于外部主体的舆情样本；规则覆盖部分否认仍触发、相似主体明确无关联不触发等情形。
    输出契约与 assess_news_risk 一致：content[0].text 与 structuredContent.result 同源。
    只读取传入原文，不读取任何参考判断或答案文件。
    """
    try:
        if not isinstance(entities, dict) or not entities:
            raise ValueError("entities 须为非空主体映射对象")
        return _wire(assess_news_v2(dict(news), entities))
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--transport", choices=("stdio", "sse", "streamable-http"), default="stdio"
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP listen address")
    parser.add_argument("--port", type=int, default=8770, help="HTTP listen port")
    args = parser.parse_args()
    if args.transport == "sse":
        server.run("sse", host=args.host, port=args.port, sse_path="/sse")
    elif args.transport == "streamable-http":
        server.run(
            "streamable-http", host=args.host, port=args.port,
            streamable_http_path="/mcp",
        )
    else:
        server.run("stdio")
