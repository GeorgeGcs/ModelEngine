"""「与 AI 参考答案一致率」评分器回归测试。

所有参考判断均为临时构造的 synthetic fixture，不是人工金标，也不是用户提供的
AI 参考答案文件；测试不写入仓库。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCORE = ROOT / "eval/news_risk/score_dev_eval.py"
AI_ORIGIN = "AI 起草的参考判断"
UNREVIEWED = "未经独立人工判定"


def sample_row(i: int) -> dict:
    return {
        "sample_id": f"N{i:03d}",
        "symbol": "NEWS-A",
        "published_at": "2026-11-05T10:00:00+08:00",
        "source": "工程自测模拟来源",
        "text": f"模拟舆情原文第 {i} 条，用于评分器机制验证。",
    }


def prediction_row(i: int, verdict: str = "no_risk", **overrides) -> dict:
    row = {
        "sample_id": f"N{i:03d}",
        "symbol": "NEWS-A",
        "risk_verdict": verdict,
        # 证据必须是该条原文的子串
        "evidence": sample_row(i)["text"][:12],
        "level": "无",
        "review_action": "无需预警；按常规归档。",
        "profile": "v2",
        "evidence_status": "explanatory",
        "reason_codes": ["工程自测"],
        "disclaimer": "风险提示仅供参考，不构成投资建议。",
    }
    row.update(overrides)
    return row


class DevEvalScoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.samples = self.dir / "samples.json"
        self.lock = Path(str(self.samples) + ".sha256")
        self.predictions = self.dir / "predictions.json"
        self.reference = self.dir / "ai_reference.json"
        self.report = self.dir / "report.json"

    def tearDown(self):
        self._tmp.cleanup()

    def write_samples(self, count: int = 50) -> None:
        items = [sample_row(i) for i in range(1, count + 1)]
        raw = (json.dumps({"schema": "test", "items": items},
                          ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        self.samples.write_bytes(raw)
        self.lock.write_bytes(
            f"{hashlib.sha256(raw).hexdigest()}  {self.samples.name}\n".encode("utf-8"))

    def write_predictions(self, rows: list[dict]) -> None:
        self.predictions.write_bytes(
            (json.dumps({"schema": "topic16-news-risk-output-v1",
                         "scope": "system-output", "profile": "v2",
                         "count": len(rows), "items": rows},
                        ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    def write_reference(self, labels: list[str] | dict[int, str] | None = None,
                        *, origin: str = AI_ORIGIN,
                        reviewed: str = UNREVIEWED) -> None:
        if labels is None:
            labels = ["无风险信号"] * 50
        entries = []
        if isinstance(labels, dict):
            for i, label in labels.items():
                entries.append({"id": f"N{i:03d}", "label": label,
                               "evidence_quote": "模拟依据", "reason": "模拟理由"})
        else:
            for i, label in enumerate(labels, start=1):
                entries.append({"id": f"N{i:03d}", "label": label,
                               "evidence_quote": "模拟依据", "reason": "模拟理由"})
        self.reference.write_bytes(
            (json.dumps({
                "label_origin": origin, "human_review_status": reviewed,
                "references": entries,
            }, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    def baseline(self, *, labels=None, predictions=None, count: int = 50):
        self.write_samples(count)
        self.write_reference(labels)
        self.write_predictions(predictions if predictions is not None
                               else [prediction_row(i) for i in range(1, count + 1)])

    def run_score(self, *extra: str) -> tuple[subprocess.CompletedProcess, dict]:
        command = [
            sys.executable, str(SCORE),
            "--samples", str(self.samples),
            "--predictions", str(self.predictions),
            "--reference", str(self.reference),
            "--report", str(self.report),
            *extra,
        ]
        completed = subprocess.run(command, cwd=str(ROOT), capture_output=True)
        payload = json.loads(completed.stdout.decode("utf-8"))
        return completed, payload

    # ---------- 正常计分与命名 ----------
    def test_01_all_50_agree_and_metric_is_named_correctly(self):
        self.baseline()
        completed, report = self.run_score()
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(report["metric_name"], "与 AI 参考答案的一致率")
        self.assertEqual(report["ai_reference_sample_count"], 50)
        self.assertEqual(report["ai_reference_match_count"], 50)
        self.assertEqual(report["ai_reference_match_rate"], 1.0)
        # 审计 P0/中：开发报告不得出现任何验收门槛布尔值。
        for key in ("meets_85_percent_vs_ai_reference", "pass", "formal",
                    "threshold_for_85_percent", "agreement_rate", "agreement_count"):
            self.assertNotIn(key, report)
        self.assertTrue(report["not_an_acceptance_threshold"])
        self.assertTrue(report["not_human_judgment_agreement"])
        self.assertTrue(report["not_acceptance_evidence"])
        self.assertTrue(report["reference_read_after_prediction_validation"])
        self.assertTrue(report["generalization_caveat"])
        self.assertEqual(report["label_origin"], AI_ORIGIN)
        self.assertEqual(report["human_review_status"], UNREVIEWED)

    def test_02_43_matches_is_reported_without_acceptance_boolean(self):
        """43/50 是正式验收门槛，但开发评分器只报数字、不判达标。"""
        predictions = [prediction_row(i, "risk" if i > 43 else "no_risk")
                       for i in range(1, 51)]
        self.baseline(predictions=predictions)
        completed, report = self.run_score()
        self.assertEqual(report["ai_reference_match_count"], 43)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(report["false_positive_count"], 7)
        self.assertNotIn("meets_85_percent_vs_ai_reference", report)

    def test_03_42_matches_is_not_a_failure_of_this_script(self):
        """42/50 也只是数字更低；退出码 0 表示结构合法，不代表达标。"""
        predictions = [prediction_row(i, "risk" if i > 42 else "no_risk")
                       for i in range(1, 51)]
        self.baseline(predictions=predictions)
        completed, report = self.run_score()
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(report["ai_reference_match_count"], 42)
        self.assertTrue(report["not_acceptance_evidence"])
        self.assertNotIn("pass", report)

    def test_04_false_positives_and_negatives_include_original_text(self):
        labels = ["无风险信号"] * 50
        labels[0] = "有风险信号"   # 参考 risk，预测 no_risk -> 漏报
        predictions = [prediction_row(i) for i in range(1, 51)]
        predictions[1]["risk_verdict"] = "risk"  # 参考 no_risk -> 误报
        self.baseline(labels=labels, predictions=predictions)
        _, report = self.run_score()
        self.assertEqual(report["false_negative_count"], 1)
        self.assertEqual(report["false_positive_count"], 1)
        self.assertEqual(report["false_negatives"][0]["sample_id"], "N001")
        self.assertEqual(report["false_positives"][0]["sample_id"], "N002")
        self.assertIn("模拟舆情原文", report["false_negatives"][0]["text"])
        self.assertIn("模拟依据", report["false_positives"][0]["reference_evidence_quote"])

    def test_05_baseline_variant_is_recorded(self):
        self.baseline()
        _, report = self.run_score("--profile", "v2-baseline",
                                   "--variant", "baseline")
        self.assertEqual(report["variant"], "baseline")
        self.assertEqual(report["profile"], "v2-baseline")

    # ---------- 参考文件身份校验（防止把人工判定误当 AI 参考） ----------
    def test_06_human_origin_reference_is_rejected(self):
        self.baseline()
        self.write_reference(origin="人工判定")
        _, report = self.run_score()
        self.assertTrue(any("label_origin" in e for e in report["structural_errors"]))

    def test_07_reviewed_reference_is_rejected(self):
        self.baseline()
        self.write_reference(reviewed="已独立人工复核")
        _, report = self.run_score()
        self.assertTrue(any("human_review_status" in e
                            for e in report["structural_errors"]))

    def test_08_misaligned_reference_ids_are_rejected(self):
        self.baseline()
        self.write_reference(labels={i: "无风险信号" for i in range(1, 50)})
        _, report = self.run_score()
        self.assertTrue(any("ID 与评测输入未完全对齐" in e
                            for e in report["structural_errors"]))

    # ---------- 预测结构错误 ----------
    def test_09_missing_prediction_is_caught(self):
        self.baseline(predictions=[prediction_row(i) for i in range(1, 50)])
        _, report = self.run_score()
        self.assertTrue(any("预测须覆盖" in e for e in report["structural_errors"]))

    def test_10_duplicate_prediction_is_caught(self):
        rows = [prediction_row(i) for i in range(1, 51)] + [prediction_row(3)]
        self.baseline(predictions=rows)
        _, report = self.run_score()
        self.assertTrue(any("预测重复" in e for e in report["structural_errors"]))

    def test_11_extra_prediction_id_is_caught(self):
        rows = [prediction_row(i) for i in range(1, 51)]
        rows.append(prediction_row(1, sample_id="N999"))
        self.baseline(predictions=rows)
        _, report = self.run_score()
        self.assertTrue(any("评测集以外" in e for e in report["structural_errors"]))

    def test_12_invalid_prediction_verdict_is_caught(self):
        rows = [prediction_row(i) for i in range(1, 51)]
        rows[0]["risk_verdict"] = "maybe"
        self.baseline(predictions=rows)
        _, report = self.run_score()
        self.assertTrue(any("risk_verdict 非法" in e
                            for e in report["structural_errors"]))

    def test_13_answer_field_in_prediction_is_caught(self):
        rows = [prediction_row(i) for i in range(1, 51)]
        rows[0]["label"] = "有风险信号"
        self.baseline(predictions=rows)
        _, report = self.run_score()
        self.assertTrue(any("疑似答案字段" in e for e in report["structural_errors"]))

    # ---------- 缺文件与输入非法 ----------
    def test_14_missing_predictions_returns_2_without_report(self):
        self.write_samples()
        self.write_reference()
        completed, report = self.run_score()
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(report["status"], "missing-predictions")
        self.assertFalse(self.report.exists())

    def test_15_missing_reference_returns_2_without_report(self):
        self.write_samples()
        self.write_predictions([prediction_row(i) for i in range(1, 51)])
        completed, report = self.run_score()
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(report["status"], "missing-ai-reference")
        self.assertFalse(self.report.exists())

    def test_16_tampered_sample_lock_is_rejected(self):
        self.baseline()
        self.samples.write_bytes(self.samples.read_bytes() + b" ")
        completed, report = self.run_score()
        self.assertEqual(completed.returncode, 3)
        self.assertEqual(report["status"], "invalid-samples")

    def test_17_report_paths_are_not_machine_absolute(self):
        self.baseline()
        _, report = self.run_score()
        for value in report["evidence"].values():
            self.assertFalse(value.startswith("D:"))
            self.assertFalse(value.startswith("C:"))

    def test_18_invalid_predictions_never_open_reference_file(self):
        """预测未通过结构校验时，参考判断文件必须连打开都不打开。"""
        self.write_samples()
        rows = [prediction_row(i) for i in range(1, 51)]
        rows[0]["risk_verdict"] = "maybe"
        self.write_predictions(rows)
        # 一旦被读取，就会因非法编码/非法 JSON 产生另一种状态。
        self.reference.write_bytes(b"\xff\xfe not-json \x00")
        completed, payload = self.run_score()
        self.assertEqual(completed.returncode, 3)
        self.assertEqual(payload["status"], "invalid-predictions")
        self.assertFalse(payload["reference_read"])
        self.assertTrue(any("risk_verdict 非法" in e
                            for e in payload["structural_errors"]))
        self.assertFalse(self.report.exists())

    # ---------- 审计 P1-6：结构校验绕过 ----------
    def test_19_nested_reference_labels_in_prediction_are_caught(self):
        rows = [prediction_row(i) for i in range(1, 51)]
        rows[0]["details"] = {"label": "有风险信号",
                              "items": [{"evidence_quote": "书面解约通知"}]}
        self.baseline(predictions=rows)
        completed, payload = self.run_score()
        self.assertEqual(completed.returncode, 3)
        self.assertEqual(payload["status"], "invalid-predictions")
        self.assertTrue(any("疑似答案字段" in e
                            for e in payload["structural_errors"]))

    def test_20_fabricated_evidence_is_caught(self):
        rows = [prediction_row(i) for i in range(1, 51)]
        rows[0]["evidence"] = "与原文无关的伪造证据片段"
        self.baseline(predictions=rows)
        completed, payload = self.run_score()
        self.assertEqual(completed.returncode, 3)
        self.assertTrue(any("证据片段不是该条原文的子串" in e
                            for e in payload["structural_errors"]))

    def test_21_expect_profile_accepts_matching_output(self):
        self.baseline()
        completed, payload = self.run_score("--expect-profile", "v2")
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(payload["ai_reference_match_count"], 50)

    def test_22_expect_profile_rejects_mismatched_output(self):
        self.baseline()
        completed, payload = self.run_score("--expect-profile", "v1")
        self.assertEqual(completed.returncode, 3)
        self.assertTrue(any("profile 与期望不一致" in e
                            for e in payload["structural_errors"]))

    def test_23_failure_marks_stale_report_invalid_in_status_sidecar(self):
        """失败不写新报告；状态文件标 report_valid=false，且旧报告就地失效。"""
        self.write_samples()
        rows = [prediction_row(i) for i in range(1, 51)]
        rows[0]["risk_verdict"] = "maybe"
        self.write_predictions(rows)
        self.write_reference()
        self.report.write_bytes(
            (json.dumps({"schema": "topic16-ai-reference-agreement-v2",
                         "ai_reference_match_count": 50}, ensure_ascii=False)
             + "\n").encode("utf-8"))
        completed, _ = self.run_score("--run-id", "AUDIT-RUN-1")
        self.assertEqual(completed.returncode, 3)
        sidecar = Path(str(self.report) + ".status.json")
        self.assertTrue(sidecar.exists())
        status = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(status["run_id"], "AUDIT-RUN-1")
        self.assertEqual(status["exit_code"], 3)
        self.assertFalse(status["report_valid"])
        self.assertFalse(status["reference_read"])
        self.assertTrue(status["previous_report_invalidated"])
        invalidated = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(invalidated["schema"],
                         "topic16-dev-eval-report-invalidated-v1")
        self.assertFalse(invalidated["report_valid"])
        self.assertNotIn("ai_reference_match_count", invalidated)
        self.assertEqual(invalidated["invalidated_by_run_id"], "AUDIT-RUN-1")
        self.assertEqual(invalidated["stale_report"]["ai_reference_match_count"], 50)

    def test_24_directly_monitors_reference_file_open(self):
        """直接监听 Path.read_text：预测失败时参考文件打开次数必须为 0。"""
        self.write_samples()
        rows = [prediction_row(i) for i in range(1, 51)]
        rows[0]["risk_verdict"] = "maybe"
        self.write_predictions(rows)
        self.write_reference()
        probe = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, r"{root}")
import score_dev_eval as m
opened = []
original = Path.read_text
def spy(self, *args, **kwargs):
    opened.append(str(self))
    return original(self, *args, **kwargs)
Path.read_text = spy
sys.argv = ["score_dev_eval.py", "--samples", r"{samples}",
            "--predictions", r"{predictions}", "--reference", r"{reference}",
            "--report", r"{report}", "--run-id", "PROBE"]
code = m.main()
print("RESULT=" + json.dumps({{"code": code, "opened": opened}}), file=sys.stderr)
'''.format(root=str(ROOT / "eval/news_risk"), samples=str(self.samples),
           predictions=str(self.predictions), reference=str(self.reference),
           report=str(self.report))
        completed = subprocess.run([sys.executable, "-c", probe], cwd=str(ROOT),
                                   capture_output=True)
        marker = [line for line in completed.stderr.decode("utf-8").splitlines()
                  if line.startswith("RESULT=")]
        self.assertEqual(len(marker), 1, completed.stderr.decode("utf-8"))
        result = json.loads(marker[0][len("RESULT="):])
        self.assertEqual(result["code"], 3)
        self.assertNotIn(str(self.reference), result["opened"])

    # ---------- 提前交付：失败运行不得让磁盘上留下「有效」的旧报告 ----------
    def broken_predictions(self) -> None:
        rows = [prediction_row(i) for i in range(1, 51)]
        rows[0]["risk_verdict"] = "maybe"
        self.write_predictions(rows)

    def read_report(self) -> dict:
        return json.loads(self.report.read_text(encoding="utf-8"))

    def test_25_successful_report_is_invalidated_by_later_failed_run(self):
        """核心反例：先 50/50 成功，后一次预测异常；旧一致条数不得继续有效。"""
        self.baseline()
        ok, first = self.run_score("--run-id", "DEV-OK-1")
        self.assertEqual(ok.returncode, 0)
        self.assertEqual(first["ai_reference_match_count"], 50)
        self.assertEqual(self.read_report()["ai_reference_match_count"], 50)

        self.broken_predictions()
        failed, _ = self.run_score("--run-id", "DEV-FAIL-1")
        self.assertEqual(failed.returncode, 3)
        invalidated = self.read_report()
        self.assertFalse(invalidated["report_valid"])
        self.assertNotIn("ai_reference_match_count", invalidated)
        self.assertNotIn("pass", invalidated)
        self.assertTrue(invalidated["not_acceptance_evidence"])
        self.assertEqual(invalidated["invalidated_by_run_id"], "DEV-FAIL-1")
        self.assertEqual(invalidated["stale_report"]["ai_reference_match_count"], 50)
        status = json.loads(Path(str(self.report) + ".status.json")
                            .read_text(encoding="utf-8"))
        self.assertFalse(status["report_valid"])
        self.assertTrue(status["previous_report_invalidated"])

    def test_26_repeated_failures_do_not_nest_invalidated_reports(self):
        """连续失败幂等：stale_report 里不应再套一层 invalidated schema。"""
        self.baseline()
        self.assertEqual(self.run_score("--run-id", "DEV-OK-2")[0].returncode, 0)
        self.broken_predictions()
        self.run_score("--run-id", "DEV-FAIL-2A")
        self.run_score("--run-id", "DEV-FAIL-2B")
        final = self.read_report()
        self.assertEqual(final["schema"], "topic16-dev-eval-report-invalidated-v1")
        self.assertEqual(final["invalidated_by_run_id"], "DEV-FAIL-2B")
        self.assertEqual(final["stale_report"]["schema"],
                         "topic16-ai-reference-agreement-v2")
        self.assertEqual(final["stale_report"]["ai_reference_match_count"], 50)

    def test_27_structural_errors_also_invalidate_written_report(self):
        """退出码 1（结构错误）时本次报告也不能作为有效一致率留在磁盘上。"""
        self.baseline()
        self.write_reference(origin="人工判定")
        completed, stdout_report = self.run_score("--run-id", "DEV-BAD-REF")
        self.assertEqual(completed.returncode, 1)
        self.assertTrue(stdout_report["structural_errors"])
        invalidated = self.read_report()
        self.assertFalse(invalidated["report_valid"])
        self.assertEqual(invalidated["invalidation_state"],
                         "scored-with-structural-errors")
        self.assertNotIn("ai_reference_match_count", invalidated)

    def test_28_successful_rerun_restores_a_valid_report(self):
        """失效是一次运行的状态，不是永久封禁：修好后重跑可写回有效报告。"""
        self.baseline()
        self.run_score("--run-id", "DEV-OK-3")
        self.broken_predictions()
        self.run_score("--run-id", "DEV-FAIL-3")
        self.assertFalse(self.read_report()["report_valid"])
        self.write_predictions([prediction_row(i) for i in range(1, 51)])
        ok, _ = self.run_score("--run-id", "DEV-OK-4")
        self.assertEqual(ok.returncode, 0)
        restored = self.read_report()
        self.assertEqual(restored["schema"], "topic16-ai-reference-agreement-v2")
        self.assertEqual(restored["ai_reference_match_count"], 50)
        status = json.loads(Path(str(self.report) + ".status.json")
                            .read_text(encoding="utf-8"))
        self.assertTrue(status["report_valid"])
        self.assertFalse(status["previous_report_invalidated"])


if __name__ == "__main__":
    unittest.main()
