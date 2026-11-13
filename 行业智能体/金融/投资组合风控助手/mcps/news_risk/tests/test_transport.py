"""News-risk MCP wire regressions; run with the pinned MCP 2.3 project environment.

    .venv/Scripts/python.exe -m unittest discover -s mcps/news_risk/tests -v

背景：2026-11-09 真实 Nexent v2.8.3 实跑发现 mcpadapt 0.1.19 会拒绝空
content，平台侧依赖 content[0].text 取工具结果。只返回 dict 无法保证被消费。
本测试锁定舆情工具与规则 MCP 一致的「文本块 + 结构化结果 + 显式成功」契约。
"""

from __future__ import annotations

import json
import unittest

from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.func_metadata import func_metadata
from mcp_types import CallToolResult

from mcps.news_risk import server as news_server

REQUIRED_FIELDS = ("symbol", "risk_verdict", "evidence", "level", "review_action")
ENTITIES = {
    "NEWS-A": {"canonical": "示例储能股份有限公司", "aliases": ["示例储能"]},
    "NEWS-B": {"canonical": "另一家示例制造有限公司"},
}


class NewsRiskTransportTests(unittest.TestCase):
    def assert_successful_wire(self, result) -> dict:
        """返回解出的结构化结果，并保证线上 wire 三个必要条件都成立。"""
        self.assertIsInstance(result, CallToolResult)
        self.assertIn("is_error", result.model_fields_set)
        wire = result.model_dump(mode="json", by_alias=True, exclude_none=True)
        self.assertIn("isError", wire)
        self.assertIs(wire["isError"], False)
        self.assertEqual(len(wire["content"]), 1)
        self.assertEqual(wire["content"][0]["type"], "text")
        # 关键：即使判定为 no_risk，文本块也必须是完整 JSON，不能是空 content。
        self.assertTrue(wire["content"][0]["text"].strip())
        self.assertIn("structuredContent", wire)
        structured = wire["structuredContent"]
        self.assertIn("result", structured)
        self.assertIsInstance(structured["result"], dict)
        self.assertEqual(json.loads(wire["content"][0]["text"]), structured["result"])
        row = structured["result"]
        for key in REQUIRED_FIELDS:
            self.assertIn(key, row)
            self.assertTrue(str(row[key]).strip(), key)
        self.assertIn(row["risk_verdict"], ("risk", "no_risk"))
        return row

    def test_v1_risk_item_has_matching_text_and_structured_result(self):
        result = news_server.assess_news_risk({
            "symbol": "SIM-A",
            "sample_id": "WIRE-001",
            "text": "云岚科技股份有限公司公告，公司收到监管部门立案调查通知书。",
        })
        row = self.assert_successful_wire(result)
        self.assertEqual(row["symbol"], "SIM-A")
        self.assertEqual(row["risk_verdict"], "risk")
        self.assertEqual(row["sample_id"], "WIRE-001")

    def test_v1_no_risk_still_returns_non_empty_text(self):
        result = news_server.assess_news_risk({
            "symbol": "SIM-B",
            "text": "公司澄清，网传产品召回系不实信息，原报道已撤回。",
        })
        row = self.assert_successful_wire(result)
        self.assertEqual(row["risk_verdict"], "no_risk")

    def test_v2_dynamic_entity_has_matching_text_and_structured_result(self):
        result = news_server.assess_news_risk_v2(
            {"symbol": "NEWS-A", "sample_id": "WIRE-002",
             "text": "示例储能股份有限公司收到最大客户的书面解约通知，公司尚未找到替代订单。"},
            ENTITIES,
        )
        row = self.assert_successful_wire(result)
        self.assertEqual(row["symbol"], "NEWS-A")
        self.assertIn(row.get("profile"), ("v2", "v2-baseline"))

    def test_v2_entity_mismatch_does_not_alert_monitored_symbol(self):
        result = news_server.assess_news_risk_v2(
            {"symbol": "NEWS-A",
             "text": "另一家示例制造有限公司受到行政处罚，与示例储能股份有限公司没有股权或供货关系。"},
            ENTITIES,
        )
        row = self.assert_successful_wire(result)
        self.assertEqual(row["risk_verdict"], "no_risk")
        # 明确点出「与本公司无关联」走主体/关联排除；只点名他方走主体错配。
        # 两者都不应给本次监控标的预警，故都接受。
        self.assertIn(row["evidence_status"], ("entity_exclusion", "entity_mismatch"))

    def test_invalid_input_raises_without_fabricating_successful_result(self):
        for item in ({"symbol": "UNKNOWN", "text": "任何文本"},
                     {"symbol": "SIM-A", "text": "   "}):
            with self.subTest(item=item):
                with self.assertRaises(ToolError):
                    news_server.assess_news_risk(item)
        with self.assertRaises(ToolError):
            news_server.assess_news_risk_v2(
                {"symbol": "NEWS-A", "text": "任何文本"}, {})

    def test_explicit_result_preserves_original_output_schema(self):
        """Annotated[CallToolResult, dict] 不得改变对平台声明的输出 schema。"""
        def original_shape(news: news_server.NewsItem) -> dict:
            raise AssertionError("Schema inspection must not call the baseline-shaped function")

        original_shape.__name__ = news_server.assess_news_risk.__name__
        original = func_metadata(original_shape)
        explicit = func_metadata(news_server.assess_news_risk)
        self.assertEqual(explicit.arg_model.model_json_schema(),
                         original.arg_model.model_json_schema())
        self.assertEqual(explicit.output_schema, original.output_schema)
        result = news_server.assess_news_risk(
            {"symbol": "SIM-A", "text": "云岚科技股份有限公司收到监管立案调查通知书。"})
        self.assertIs(explicit.convert_result(result), result)


if __name__ == "__main__":
    unittest.main()
