"""舆情研判规则回归测试（使用独立开发样例，不涉及 50 条评测样本）。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from news_risk.rules import (  # noqa: E402
    DISCLAIMER, VALID_LEVELS, VALID_VERDICTS, assess_batch, assess_news,
)

DEV = ROOT / "data/news_risk/dev_samples.json"
REQUIRED = ("symbol", "risk_verdict", "evidence", "level", "review_action")


def load_dev() -> list[dict]:
    return json.loads(DEV.read_text(encoding="utf-8"))["items"]


def strip_expect(item: dict) -> dict:
    return {key: value for key, value in item.items() if key != "expect"}


class NewsRiskRulesTests(unittest.TestCase):
    def test_01_all_dev_samples_match_engineering_expectations(self):
        for item in load_dev():
            with self.subTest(sample=item["sample_id"]):
                got = assess_news(strip_expect(item))
                for key, expected in item["expect"].items():
                    self.assertEqual(got[key], expected)

    def test_02_negation_reverses_negative_keywords(self):
        item = {"sample_id": "T-NEG", "symbol": "SIM-A",
                "text": "云岚科技回应：公司并未收到立案调查通知书，财务造假的说法不存在。"}
        result = assess_news(item)
        self.assertEqual(result["risk_verdict"], "no_risk")
        self.assertEqual(result["evidence_status"], "negated")
        self.assertIn("并未收到", result["evidence"])

    def test_03_entity_mismatch_is_not_a_risk_for_monitored_symbol(self):
        item = {"sample_id": "T-ENT", "symbol": "SIM-A",
                "text": "云岚科技（苏州）有限公司因环保问题被处以行政处罚，该主体为独立法人。"}
        result = assess_news(item)
        self.assertEqual(result["risk_verdict"], "no_risk")
        self.assertEqual(result["evidence_status"], "entity_mismatch")
        self.assertIn("云岚科技（苏州）有限公司", result["evidence"])

    def test_04_insufficient_evidence_is_downgraded_to_review(self):
        item = {"sample_id": "T-INS", "symbol": "SIM-C",
                "text": "仅有标题与沣泽消费服务控股有限公司相关字样，正文缺失，无法确认具体事项。"}
        result = assess_news(item)
        self.assertEqual(result["risk_verdict"], "no_risk")
        self.assertEqual(result["evidence_status"], "insufficient")
        self.assertEqual(result["level"], "待复核")
        self.assertIn("人工核实", result["review_action"])

    def test_05_clarification_wins_over_rumor_marker(self):
        item = {"symbol": "SIM-B",
                "text": "北辰智造发布澄清公告：此前传闻的产品召回系不实信息，原报道已撤回。"}
        self.assertEqual(assess_news(item)["evidence_status"], "clarified")

    def test_06_historical_item_without_new_development_is_not_risk(self):
        item = {"symbol": "SIM-D",
                "text": "历史资料：星渚生物医药曾在 2024 年收到监管问询函，本次为档案归档。"}
        result = assess_news(item)
        self.assertEqual(result["risk_verdict"], "no_risk")
        self.assertEqual(result["evidence_status"], "historical")

    def test_07_new_development_prevents_historical_exemption(self):
        item = {"symbol": "SIM-D",
                "text": "回顾 2024 年星渚生物医药曾收到监管问询函，本次披露该事项有最新进展并再度被立案调查。"}
        result = assess_news(item)
        self.assertEqual(result["risk_verdict"], "risk")
        self.assertEqual(result["level"], "高")

    def test_08_every_output_has_five_required_fields_and_valid_enums(self):
        for item in load_dev():
            result = assess_news(strip_expect(item))
            for key in REQUIRED:
                self.assertIn(key, result)
                self.assertTrue(str(result[key]).strip())
            self.assertIn(result["risk_verdict"], VALID_VERDICTS)
            self.assertIn(result["level"], VALID_LEVELS)
            self.assertEqual(result["disclaimer"], DISCLAIMER)

    def test_09_same_input_three_times_is_stable(self):
        item = strip_expect(load_dev()[6])
        results = [assess_news(item) for _ in range(3)]
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])

    def test_10_no_buy_or_sell_advice_in_any_output(self):
        forbidden = ("建议卖出", "建议买入", "立即抛售", "加仓", "清仓")
        for item in load_dev():
            result = assess_news(strip_expect(item))
            for word in forbidden:
                self.assertNotIn(word, result["review_action"])

    def test_11_invalid_input_is_rejected(self):
        with self.assertRaises(ValueError):
            assess_news({"symbol": "UNKNOWN", "text": "某公司治理正常"})
        with self.assertRaises(ValueError):
            assess_news({"symbol": "SIM-A", "text": "   "})
        with self.assertRaises(ValueError):
            assess_batch([{"symbol": "SIM-A", "text": "经营正常"},
                          {"symbol": "SIM-A", "text": ""}])

    def test_12_batch_keeps_sample_ids_and_order(self):
        items = [strip_expect(item) for item in load_dev()]
        results = assess_batch(items)
        self.assertEqual([r["sample_id"] for r in results],
                         [item["sample_id"] for item in items])

    def test_13_dev_samples_are_separate_from_evaluation_ids(self):
        doc = json.loads(DEV.read_text(encoding="utf-8"))
        self.assertTrue(doc["not_gold_standard"])
        self.assertTrue(doc["not_formal_evaluation"])
        self.assertTrue(all(item["sample_id"].startswith("DEV-")
                            for item in doc["items"]))

    # ---------- 审计 P2-1：主体归属必须早于风险词匹配 ----------
    def test_14_other_monitored_entitys_bad_news_is_not_this_symbols_risk(self):
        """「北辰智造集团股份有限公司收到行政处罚决定书」不能算到 SIM-A 头上。"""
        r = assess_news({"symbol": "SIM-A",
                         "text": "北辰智造集团股份有限公司收到行政处罚决定书。"})
        self.assertEqual(r["risk_verdict"], "no_risk")
        self.assertEqual(r["evidence_status"], "entity_mismatch")
        self.assertIn("其他监控主体", " ".join(r["reason_codes"]))
        self.assertIn("北辰智造集团股份有限公司", r["evidence"])

    def test_15_entity_attribution_keeps_existing_boundaries(self):
        cases = (
            ("SIM-A", "云岚科技股份有限公司收到监管部门立案调查通知书。", "risk", "sufficient"),
            ("SIM-A", "云岚科技（苏州）有限公司受到行政处罚。", "no_risk", "entity_mismatch"),
            ("SIM-A", "行业整体波动，多家企业调整排产。", "no_risk", "not_applicable"),
        )
        for symbol, text, verdict, status in cases:
            with self.subTest(text=text):
                r = assess_news({"symbol": symbol, "text": text})
                self.assertEqual(r["risk_verdict"], verdict)
                self.assertEqual(r["evidence_status"], status)


if __name__ == "__main__":
    unittest.main()
