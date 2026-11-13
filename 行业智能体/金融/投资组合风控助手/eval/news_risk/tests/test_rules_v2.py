"""增强研判 v2 回归测试：难例、修复前后对照与反过拟合检查。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from news_risk.rules_v2 import (  # noqa: E402
    VALID_LEVELS, VALID_VERDICTS, assess_batch_v2, assess_news_v2,
)

SAMPLES = ROOT / "data/news_risk/dev_eval_samples.json"
ENTITIES = json.loads(SAMPLES.read_text(encoding="utf-8"))["entities"]
CASES = {item["sample_id"]: item for item in
         json.loads(SAMPLES.read_text(encoding="utf-8"))["items"]}
REQUIRED = ("symbol", "risk_verdict", "evidence", "level", "review_action")


def case(sample_id: str) -> dict:
    return {key: value for key, value in CASES[sample_id].items()}


class RulesV2Tests(unittest.TestCase):
    def test_01_partial_denial_still_triggers(self):
        """否认『全厂停产』但确认四分之一产能暂停 —— 仍应触发。"""
        result = assess_news_v2(case("N038"), ENTITIES)
        self.assertEqual(result["risk_verdict"], "risk")
        self.assertEqual(result["evidence_status"], "partial_denial_confirmed")
        self.assertIn("仍须入队核查", " ".join(result["reason_codes"]))

    def test_02_same_partial_denial_is_missed_in_baseline(self):
        baseline = assess_news_v2(case("N038"), ENTITIES, baseline=True)
        self.assertEqual(baseline["risk_verdict"], "no_risk")
        self.assertEqual(baseline["evidence_status"], "negated")
        self.assertNotEqual(assess_news_v2(case("N038"), ENTITIES)["risk_verdict"],
                            baseline["risk_verdict"])

    def test_03_similar_entity_with_explicit_no_relation_is_not_risk(self):
        result = assess_news_v2(case("N003"), ENTITIES)
        self.assertEqual(result["risk_verdict"], "no_risk")
        self.assertEqual(result["evidence_status"], "entity_exclusion")

    def test_04_sold_out_position_is_not_risk(self):
        result = assess_news_v2(case("N024"), ENTITIES)
        self.assertEqual(result["risk_verdict"], "no_risk")
        self.assertEqual(result["evidence_status"], "entity_exclusion")

    def test_05_baseline_misreports_both_entity_cases(self):
        for sample_id in ("N003", "N024"):
            with self.subTest(sample=sample_id):
                self.assertEqual(
                    assess_news_v2(case(sample_id), ENTITIES,
                                   baseline=True)["risk_verdict"], "risk")

    def test_06_unseen_wording_still_excludes_other_entity(self):
        """未出现在 50 条里的句式，检验规则不是逐句硬编码。"""
        item = {
            "sample_id": "GEN-001", "symbol": "GEN-X",
            "text": "星海精密因排污被处罚，通报明确与持仓公司远川精密没有股权或供货关系。",
        }
        result = assess_news_v2(item, {"GEN-X": {"canonical": "远川精密"}})
        self.assertEqual(result["risk_verdict"], "no_risk")
        self.assertEqual(result["evidence_status"], "entity_exclusion")

    def test_07_unseen_wording_still_catches_partial_denial(self):
        item = {
            "sample_id": "GEN-002", "symbol": "GEN-Y",
            "text": "远川精密否认全线停产，同时确认一条产线暂停，尚无复产日期。",
        }
        result = assess_news_v2(item, {"GEN-Y": {"canonical": "远川精密"}})
        self.assertEqual(result["risk_verdict"], "risk")
        self.assertEqual(result["evidence_status"], "partial_denial_confirmed")

    def test_08_result_does_not_depend_on_sample_id(self):
        item = case("N038")
        other = dict(item, sample_id="TOTALLY-DIFFERENT-ID")
        first = assess_news_v2(item, ENTITIES)
        second = assess_news_v2(other, ENTITIES)
        self.assertEqual({k: v for k, v in first.items() if k != "sample_id"},
                         {k: v for k, v in second.items() if k != "sample_id"})

    def test_09_all_50_outputs_have_valid_structure(self):
        results = assess_batch_v2(list(CASES.values()), ENTITIES)
        self.assertEqual(len(results), 50)
        for result in results:
            for key in REQUIRED:
                self.assertIn(key, result)
                self.assertTrue(str(result[key]).strip())
            self.assertIn(result["risk_verdict"], VALID_VERDICTS)
            self.assertIn(result["level"], VALID_LEVELS)
            self.assertEqual(result["profile"], "v2")
            self.assertIn("不构成投资建议", result["disclaimer"])

    def test_10_same_input_three_times_is_stable(self):
        item = case("N038")
        results = [assess_news_v2(item, ENTITIES) for _ in range(3)]
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])

    def test_11_no_buy_or_sell_advice(self):
        forbidden = ("建议卖出", "建议买入", "抛售", "加仓", "清仓")
        for result in assess_batch_v2(list(CASES.values()), ENTITIES):
            for word in forbidden:
                self.assertNotIn(word, result["review_action"])

    def test_12_unknown_symbol_or_empty_text_is_rejected(self):
        with self.assertRaises(ValueError):
            assess_news_v2({"symbol": "UNKNOWN", "text": "经营正常"}, ENTITIES)
        with self.assertRaises(ValueError):
            assess_news_v2({"symbol": "NEWS-A", "text": "  "}, ENTITIES)
        with self.assertRaises(ValueError):
            assess_batch_v2([case("N001"), {"symbol": "NEWS-A", "text": ""}], ENTITIES)

    def test_13_input_file_has_no_answer_fields(self):
        document = json.loads(SAMPLES.read_text(encoding="utf-8"))
        forbidden = {"label", "answer", "gold", "risk_verdict", "human_verdict",
                     "expected", "expect", "reason", "evidence_quote"}
        for item in document["items"]:
            self.assertFalse(forbidden.intersection(item))

    def test_14_rules_have_no_sample_specific_literals(self):
        """词表不得写死样本公司名或样本 ID；否则等于把答案编进规则。"""
        source = (ROOT / "news_risk/rules_v2.py").read_text(encoding="utf-8")
        for literal in ("栖川", "澜汀", "青屿", "砾原", "N038", "N003", "N024"):
            with self.subTest(literal=literal):
                self.assertNotIn(literal, source)

    # ---------- 审计高危：主体归属 ----------
    def test_16_switching_monitored_entity_changes_risk_verdict(self):
        """旧版换标的 150/150 次判定不变；现在有风险的事件必须随标的改变。"""
        results = assess_batch_v2(list(CASES.values()), ENTITIES)
        risk_items = [item for item, result in zip(CASES.values(), results)
                      if result["risk_verdict"] == "risk"]
        self.assertEqual(len(risk_items), 25)
        checked = 0
        for item in risk_items:
            for other in ENTITIES:
                if other == item["symbol"]:
                    continue
                swapped = dict(item, symbol=other)
                result = assess_news_v2(swapped, ENTITIES)
                self.assertEqual(result["risk_verdict"], "no_risk")
                self.assertEqual(result["evidence_status"], "entity_mismatch")
                checked += 1
        self.assertEqual(checked, 75)

    def test_17_other_entitys_bad_news_does_not_alert_this_symbol(self):
        """别家公司的负面事件，不应给当前监控标的发预警。"""
        item = {
            "sample_id": "GEN-010", "symbol": "NEWS-A",
            "text": "澜汀精密因冷却系统故障停机，订单已通知客户延期，本金仍未支付。",
        }
        result = assess_news_v2(item, ENTITIES)
        self.assertEqual(result["risk_verdict"], "no_risk")
        self.assertEqual(result["evidence_status"], "entity_mismatch")

    def test_18_same_text_with_its_own_symbol_is_still_risk(self):
        """主体归属不是一刀切 no_risk：事件确实指向标的时仍要报警。"""
        text = "澜汀精密因冷却系统故障停机，订单已通知客户延期，本金仍未支付。"
        result = assess_news_v2({"symbol": "NEWS-B", "text": text}, ENTITIES)
        self.assertEqual(result["risk_verdict"], "risk")

    # ---------- 审计高危：部分否认 ----------
    def test_19_denial_confirming_recovery_is_no_risk(self):
        item = {"symbol": "GEN-Y",
                "text": "远川精密否认发生事故，同时确认三条产线全部运转正常。"}
        result = assess_news_v2(item, {"GEN-Y": {"canonical": "远川精密"}})
        self.assertEqual(result["risk_verdict"], "no_risk")
        self.assertEqual(result["evidence_status"], "denial_confirmed_recovery")

    def test_20_denial_confirming_specific_adverse_with_new_wording_is_risk(self):
        """把「四分之一」换成「三分之一」、改用其它措辞，仍应触发。"""
        item = {"symbol": "GEN-Y",
                "text": "远川精密否认全厂停工，但通报确认三分之一产能即日起闲置，整修至少持续两个月。"}
        result = assess_news_v2(item, {"GEN-Y": {"canonical": "远川精密"}})
        self.assertEqual(result["risk_verdict"], "risk")
        self.assertEqual(result["evidence_status"], "partial_denial_confirmed")

    def test_21_baseline_still_misses_the_partial_denial_case(self):
        """保留同版本开关对照：baseline 关闭新语义后 N038 仍被否定吞掉。"""
        baseline = assess_news_v2(case("N038"), ENTITIES, baseline=True)
        self.assertEqual(baseline["risk_verdict"], "no_risk")
        self.assertEqual(baseline["evidence_status"], "negated")

    def test_15_vocab_overlap_diagnostic_is_not_acceptance_evidence(self):
        """过拟合诊断可复算，且明确标注不是一致率指标、不是验收证据。"""
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "overlap.json"
            completed = subprocess.run(
                [sys.executable, str(ROOT / "eval/news_risk/vocab_overlap.py"),
                 "--report", str(report)],
                cwd=str(ROOT), capture_output=True)
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            self.assertTrue(report.exists())
            payload = json.loads(completed.stdout.decode("utf-8"))
        self.assertTrue(payload["diagnostic_only"])
        self.assertTrue(payload["not_acceptance_evidence"])
        self.assertTrue(payload["not_an_agreement_metric"])
        self.assertGreater(payload["total_markers"], 0)
        # 审计 P1-9：带类型注解的 ADVERSE_PATTERNS 必须被纳入，命中数不能恒为 0。
        self.assertTrue(payload["adverse_patterns_included"])
        self.assertGreater(payload["breakdown"]["ADVERSE_PATTERNS"]["markers"], 50)
        self.assertGreater(payload["verbatim_in_samples"], 0)


    # ---------- 审计 P1-6：正常预测与 --validate-only 共用输入校验 ----------
    def test_22_answer_field_in_input_is_rejected_by_both_cli_paths(self):
        import subprocess
        import tempfile
        document = {
            "entities": {"GEN-Z": {"canonical": "测试主体", "aliases": []}},
            "items": [{"sample_id": "X1", "symbol": "GEN-Z",
                       "text": "测试主体收到书面解约通知。",
                       "human_verdict": "risk"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "input.json"
            source.write_bytes((json.dumps(document, ensure_ascii=False, indent=2)
                                + "\n").encode("utf-8"))
            for flag in ("--validate-only", ""):
                argv = [sys.executable, "-m", "news_risk", str(source),
                        "--profile", "v2"]
                if flag:
                    argv.append(flag)
                completed = subprocess.run(argv, cwd=str(ROOT), capture_output=True)
                self.assertEqual(completed.returncode, 1,
                                 f"{flag or 'normal'}: {completed.stderr.decode()}")
                self.assertIn("疑似答案", completed.stderr.decode("utf-8"))

    def test_23_nested_reference_payload_in_input_is_rejected(self):
        import subprocess
        import tempfile
        document = {
            "entities": {"GEN-Z": {"canonical": "测试主体", "aliases": []}},
            "items": [{"sample_id": "X1", "symbol": "GEN-Z",
                       "text": "测试主体收到书面解约通知。"}],
            "references": [{"id": "X1", "label": "有风险信号"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "input.json"
            source.write_bytes((json.dumps(document, ensure_ascii=False, indent=2)
                                + "\n").encode("utf-8"))
            completed = subprocess.run(
                [sys.executable, "-m", "news_risk", str(source), "--profile", "v2"],
                cwd=str(ROOT), capture_output=True)
            self.assertEqual(completed.returncode, 1)
            self.assertIn("疑似答案", completed.stderr.decode("utf-8"))

    # ---------- 审计 P2-2：恢复词与残留不利事实并存 ----------
    def test_24_partial_recovery_does_not_cancel_residual_adverse_fact(self):
        """两条产线恢复、一条仍暂停 → 必须保留风险。"""
        text = ("公司否认全厂停产，同时确认两条产线运转正常，"
                "但一条产线暂停，尚无复产日期。")
        r = assess_news_v2({"symbol": "NEWS-A", "text": text}, ENTITIES)
        self.assertEqual(r["risk_verdict"], "risk")
        self.assertEqual(r["evidence_status"], "partial_denial_confirmed")

    def test_25_pure_recovery_and_negated_adverse_stay_no_risk(self):
        """只有恢复、或不利事实被明确否定时，不得误报。"""
        cases = (
            "公司否认全厂停产，同时确认三条产线全部运转正常。",
            "公司否认停产传闻，同时确认未发生停产，产线全部正常。",
        )
        for text in cases:
            with self.subTest(text=text):
                r = assess_news_v2({"symbol": "NEWS-A", "text": text}, ENTITIES)
                self.assertEqual(r["risk_verdict"], "no_risk")
                self.assertEqual(r["evidence_status"], "denial_confirmed_recovery")

    # ---------- 审计 P2-3：不改冻结样本即可写定 v2 ----------
    def test_26_entities_option_makes_frozen_samples_valid_for_v2(self):
        frozen = ROOT / "data/news_risk/samples_50.json"
        entity_file = ROOT / "data/news_risk/sim_entities.json"
        argv = [sys.executable, "-m", "news_risk", str(frozen),
                "--profile", "v2", "--entities", str(entity_file)]
        for flag in ("--validate-only", None):
            with self.subTest(flag=flag):
                command = list(argv)
                if flag:
                    command.append(flag)
                completed = subprocess.run(command, cwd=str(ROOT), capture_output=True)
                self.assertEqual(completed.returncode, 0,
                                 completed.stderr.decode("utf-8", "replace"))
                payload = json.loads(completed.stdout.decode("utf-8"))
                self.assertEqual(payload["entities_sha256"],
                                 hashlib.sha256(entity_file.read_bytes()).hexdigest())
                if flag:
                    self.assertEqual(payload["count"], 50)

    def test_27_entities_option_rejects_wrong_usage_and_answer_fields(self):
        frozen = ROOT / "data/news_risk/samples_50.json"
        entity_file = ROOT / "data/news_risk/sim_entities.json"
        bad_cases = [
            ([sys.executable, "-m", "news_risk", str(frozen), "--profile", "v2"],
             "需要输入文件提供 entities"),
            ([sys.executable, "-m", "news_risk", str(frozen), "--profile", "v1",
              "--entities", str(entity_file), "--validate-only"],
             "只对 --profile v2 有效"),
        ]
        for command, expected in bad_cases:
            with self.subTest(command=command[-3:]):
                completed = subprocess.run(command, cwd=str(ROOT), capture_output=True)
                self.assertEqual(completed.returncode, 1)
                self.assertIn(expected, completed.stderr.decode("utf-8", "replace"))
        with tempfile.TemporaryDirectory() as tmp:
            poisoned = Path(tmp) / "entities.json"
            poisoned.write_bytes((json.dumps({
                "entities": {"SIM-A": {"canonical": "云岚科技股份有限公司",
                                       "aliases": [], "human_verdict": "risk"}},
            }, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            completed = subprocess.run(
                [sys.executable, "-m", "news_risk", str(frozen), "--profile", "v2",
                 "--entities", str(poisoned), "--validate-only"],
                cwd=str(ROOT), capture_output=True)
            self.assertEqual(completed.returncode, 1)
            self.assertIn("疑似答案", completed.stderr.decode("utf-8", "replace"))

    def test_28_registered_entities_sha256_matches_checked_in_file(self):
        registry = json.loads((ROOT / "data/news_risk/sample_registry.json")
                             .read_text(encoding="utf-8"))
        entry = next(e for e in registry["registered_sets"]
                     if e["sample_set_id"] == "NR-2026-11-09")
        raw = (ROOT / entry["entities_file"]).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), entry["entities_sha256"])


if __name__ == "__main__":
    unittest.main()
