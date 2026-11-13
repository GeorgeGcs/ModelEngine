"""独立评分器回归测试。

所有人工/系统数据都是本测试临时构造的 synthetic fixture，
**不是人工金标、不是 50 条评测样本的答案**，也不写入仓库。
"""

from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCORE = ROOT / "eval/news_risk/score.py"
SYMBOLS = ("SIM-A", "SIM-B", "SIM-C", "SIM-D")


def sample_row(i: int) -> dict:
    return {
        "sample_id": f"NR-{i:03d}",
        "symbol": SYMBOLS[(i - 1) % 4],
        "published_at": "2026-11-05T10:00:00+08:00",
        "source": "工程自测模拟来源",
        "text": f"模拟舆情原文第 {i} 条，用于评分脚本机制验证。",
    }


def system_row(i: int, verdict: str = "no_risk", **overrides) -> dict:
    row = {
        "sample_id": f"NR-{i:03d}",
        "symbol": SYMBOLS[(i - 1) % 4],
        "risk_verdict": verdict,
        # 证据必须是该条原文的子串（正式模式会核对）
        "evidence": sample_row(i)["text"][:12],
        "level": "无",
        "review_action": "无需预警；按常规归档。",
        "profile": "v2",
    }
    row.update(overrides)
    return row


class ScoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.samples = self.dir / "samples_50.json"
        self.lock = self.dir / "samples_50.json.sha256"
        self.human = self.dir / "human_labels.csv"
        self.system = self.dir / "system_output.json"
        self.report = self.dir / "consistency_report.json"
        self.registration = self.dir / "annotation_registration.json"
        self.registry = self.dir / "sample_registry.json"

    def tearDown(self):
        self._tmp.cleanup()

    def build_formal_fixtures(self, *, profile: str = "v2",
                              set_id: str = "TEST-SET") -> None:
        """按当前文件字节生成封存记录与登记清单，供正式模式测试使用。"""
        samples_sha = (hashlib.sha256(self.samples.read_bytes()).hexdigest()
                       if self.samples.exists() else "")
        human_sha = (hashlib.sha256(self.human.read_bytes()).hexdigest()
                     if self.human.exists() else "")
        self.registration.write_bytes((json.dumps({
            "schema": "topic16-human-annotation-registration-v1",
            "sample_set_id": set_id,
            "samples_sha256": samples_sha,
            "human_labels_sha256": human_sha,
            "annotator": "独立复核者（合成测试固定值）",
            "independent_review": True,
            "sealed_at": "2026-11-01T09:00:00+08:00",
            "profile": profile,
        }, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        self.registry.write_bytes((json.dumps({
            "schema": "topic16-news-sample-registry-v1",
            "registered_sets": [{
                "sample_set_id": set_id,
                "file": self.samples.name,
                "sha256": samples_sha,
                "not_recommended_for_blind_acceptance": False,
            }],
        }, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    def write_samples(self, count: int = 50) -> None:
        items = [sample_row(i) for i in range(1, count + 1)]
        raw = (json.dumps({"items": items}, ensure_ascii=False, indent=2)
               + "\n").encode("utf-8")
        self.samples.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        self.lock.write_bytes(f"{digest}  {self.samples.name}\n".encode("utf-8"))

    def write_human(self, verdicts: list[str] | dict[int, str] | None = None) -> None:
        if verdicts is None:
            verdicts = ["no_risk"] * 50
        with self.human.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["sample_id", "human_verdict"])
            if isinstance(verdicts, dict):
                for i, verdict in verdicts.items():
                    writer.writerow([f"NR-{i:03d}", verdict])
            else:
                for i, verdict in enumerate(verdicts, start=1):
                    writer.writerow([f"NR-{i:03d}", verdict])

    def write_system(self, rows: list[dict]) -> None:
        self.system.write_bytes(
            (json.dumps({"schema": "topic16-news-risk-output-v1",
                         "scope": "system-output", "profile": "v2",
                         "count": len(rows), "items": rows},
                        ensure_ascii=False, indent=2)
             + "\n").encode("utf-8"))

    def baseline(self, *, human=None, system=None, count: int = 50):
        self.write_samples(count)
        self.write_human(human)
        self.write_system(system if system is not None
                          else [system_row(i) for i in range(1, count + 1)])

    def run_score(self, *extra: str) -> tuple[subprocess.CompletedProcess, dict]:
        """默认走完整正式证据链；extra 里可覆盖 --label-source 等参数（argparse 后者生效）。"""
        self.build_formal_fixtures()
        return self.run_raw([
            "--label-source", "human-annotation",
            "--registration", str(self.registration),
            "--sample-registry", str(self.registry),
            "--profile", "v2",
            *extra,
        ])

    def run_raw(self, extra: list[str], *, fixture: bool = True
                ) -> tuple[subprocess.CompletedProcess, dict]:
        """默认显式声明合成夹具：本文件的人工表/样本都是临时构造的测试数据。

        fixture=False 用于验证「正式模式」下的独立预登记门禁。
        """
        command = [
            sys.executable, str(SCORE),
            "--samples", str(self.samples),
            "--human", str(self.human),
            "--system", str(self.system),
            "--report", str(self.report),
            *(["--fixture-mode", "synthetic-fixture"] if fixture else []),
            *extra,
        ]
        completed = subprocess.run(command, cwd=str(ROOT), capture_output=True)
        payload = json.loads(completed.stdout.decode("utf-8"))
        return completed, payload

    # ---------- 门槛与计分 ----------
    def test_01_all_50_consistent_passes(self):
        self.baseline()
        completed, report = self.run_score()
        self.assertEqual(completed.returncode, 1)
        self.assertTrue(report["engineering_would_pass"])
        self.assertTrue(report["machine_chain_valid"])
        self.assertEqual(report["scope"], "engineering-selftest")
        self.assertEqual(report["denominator"], 50)
        self.assertEqual(report["threshold"], 43)
        self.assertEqual(report["consistent_count"], 50)
        self.assertEqual(report["consistency_rate"], 1.0)
        self.assertEqual(report["structural_errors"], [])

    def test_02_43_consistent_is_the_passing_boundary(self):
        system = [system_row(i, "risk" if i > 43 else "no_risk")
                  for i in range(1, 51)]
        self.baseline(system=system)
        completed, report = self.run_score()
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(report["consistent_count"], 43)
        self.assertTrue(report["engineering_would_pass"])
        self.assertEqual(report["false_positive_count"], 7)

    def test_03_42_consistent_fails(self):
        system = [system_row(i, "risk" if i > 42 else "no_risk")
                  for i in range(1, 51)]
        self.baseline(system=system)
        completed, report = self.run_score()
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(report["consistent_count"], 42)
        self.assertFalse(report["pass"])

    def test_04_false_negatives_and_per_sample_differences(self):
        human = ["risk"] * 50
        system = [system_row(i, "no_risk" if i <= 3 else "risk")
                  for i in range(1, 51)]
        self.baseline(human=human, system=system)
        _, report = self.run_score()
        self.assertEqual(report["false_negative_count"], 3)
        self.assertEqual(report["false_negatives"], ["NR-001", "NR-002", "NR-003"])
        self.assertEqual(report["consistent_count"], 47)
        self.assertEqual(len(report["differences"]), 3)
        self.assertEqual(report["differences"][0]["kind"], "false_negative")

    # ---------- 系统输出结构错误 ----------
    def test_05_missing_system_item_is_caught(self):
        self.baseline(system=[system_row(i) for i in range(1, 50)])
        completed, report = self.run_score()
        self.assertNotEqual(completed.returncode, 0)
        self.assertTrue(any("系统输出缺" in e for e in report["structural_errors"]))

    def test_06_duplicate_system_item_is_caught(self):
        rows = [system_row(i) for i in range(1, 51)] + [system_row(7)]
        self.baseline(system=rows)
        _, report = self.run_score()
        self.assertTrue(any("系统输出重复" in e for e in report["structural_errors"]))
        self.assertFalse(report["pass"])

    def test_07_extra_id_is_caught(self):
        rows = [system_row(i) for i in range(1, 51)]
        rows.append(system_row(1, sample_id="NR-999"))
        self.baseline(system=rows)
        _, report = self.run_score()
        self.assertTrue(any("评测集以外" in e for e in report["structural_errors"]))

    def test_08_invalid_verdict_is_caught(self):
        rows = [system_row(i) for i in range(1, 51)]
        rows[0]["risk_verdict"] = "maybe"
        self.baseline(system=rows)
        _, report = self.run_score()
        self.assertTrue(any("risk_verdict 非法" in e for e in report["structural_errors"]))

    def test_09_missing_required_field_is_caught(self):
        rows = [system_row(i) for i in range(1, 51)]
        del rows[4]["review_action"]
        self.baseline(system=rows)
        _, report = self.run_score()
        self.assertTrue(any("review_action" in e for e in report["structural_errors"]))

    def test_10_invalid_level_is_caught(self):
        rows = [system_row(i) for i in range(1, 51)]
        rows[2]["level"] = "极高"
        self.baseline(system=rows)
        _, report = self.run_score()
        self.assertTrue(any("level 非法" in e for e in report["structural_errors"]))

    def test_11_symbol_mismatch_is_caught(self):
        rows = [system_row(i) for i in range(1, 51)]
        rows[3]["symbol"] = "SIM-Z"
        self.baseline(system=rows)
        _, report = self.run_score()
        self.assertTrue(any("标的与样本不一致" in e for e in report["structural_errors"]))

    def test_12_empty_evidence_is_caught(self):
        rows = [system_row(i) for i in range(1, 51)]
        rows[5]["evidence"] = "  "
        self.baseline(system=rows)
        _, report = self.run_score()
        self.assertTrue(any("缺失或为空字段" in e for e in report["structural_errors"]))

    def test_13_structural_error_fails_even_with_50_consistent(self):
        rows = [system_row(i) for i in range(1, 51)]
        rows[9]["level"] = "未知"
        self.baseline(system=rows)
        _, report = self.run_score()
        self.assertEqual(report["consistent_count"], 50)
        self.assertFalse(report["pass"])

    # ---------- 人工侧与未就绪状态 ----------
    def test_14_missing_human_file_reports_pending_and_writes_no_report(self):
        self.write_samples()
        self.write_system([system_row(i) for i in range(1, 51)])
        completed, report = self.run_score()
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(report["status"], "pending-human-annotation")
        self.assertFalse(report["formal"])
        self.assertTrue(report["not_acceptance_evidence"])
        self.assertFalse(self.report.exists())

    def test_15_missing_system_output_reports_pending(self):
        self.write_samples()
        self.write_human()
        completed, report = self.run_score()
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(report["status"], "missing-system-output")
        self.assertFalse(self.report.exists())

    def test_16_blank_human_value_is_structural_error(self):
        verdicts = ["no_risk"] * 50
        verdicts[11] = ""
        self.baseline(human=verdicts)
        completed, report = self.run_score()
        self.assertEqual(completed.returncode, 1)
        self.assertTrue(any("人工判定为空或非法" in e
                            for e in report["structural_errors"]))

    def test_17_duplicate_human_row_is_caught(self):
        verdicts = ["no_risk"] * 51
        self.baseline(human=verdicts)
        _, report = self.run_score()
        self.assertTrue(any("评测集以外" in e or "人工判定重复" in e
                            for e in report["structural_errors"]))

    # ---------- 冻结评测集与证据属性 ----------
    def test_18_non_50_samples_are_rejected(self):
        self.baseline(count=49)
        completed, report = self.run_score()
        self.assertEqual(completed.returncode, 3)
        self.assertEqual(report["status"], "invalid-samples")

    def test_19_tampered_lock_is_rejected(self):
        self.baseline()
        self.samples.write_bytes(self.samples.read_bytes() + b" ")
        completed, report = self.run_score()
        self.assertEqual(completed.returncode, 3)
        self.assertTrue(any("SHA256" in p for p in report["problems"]))

    def test_20_answer_fields_in_samples_are_rejected(self):
        self.write_samples()
        document = json.loads(self.samples.read_text(encoding="utf-8"))
        document["items"][0]["human_verdict"] = "risk"
        raw = (json.dumps(document, ensure_ascii=False, indent=2)
               + "\n").encode("utf-8")
        self.samples.write_bytes(raw)
        self.lock.write_bytes(
            f"{hashlib.sha256(raw).hexdigest()}  {self.samples.name}\n".encode("utf-8"))
        self.write_human()
        self.write_system([system_row(i) for i in range(1, 51)])
        completed, report = self.run_score()
        self.assertEqual(completed.returncode, 3)
        self.assertTrue(any("疑似答案字段" in p for p in report["problems"]))

    def test_21_synthetic_label_source_is_never_acceptance_evidence(self):
        self.baseline()
        completed, report = self.run_score("--label-source", "synthetic-test-fixture")
        self.assertEqual(completed.returncode, 1)
        self.assertFalse(report["formal"])
        self.assertTrue(report["not_acceptance_evidence"])
        self.assertEqual(report["scope"], "engineering-selftest")
        self.assertEqual(report["consistent_count"], 50)

    def test_22_report_contains_disclaimer_and_relative_evidence_paths(self):
        self.baseline()
        _, report = self.run_score()
        self.assertIn("不构成投资建议", report["disclaimer"])
        self.assertFalse(report["evidence"]["samples"].startswith("D:"))
        self.assertFalse(report["evidence"]["samples"].startswith("C:"))


    def test_23_alternative_id_namespace_is_supported(self):
        """正式验收可沿用用户提供的 N001..N050，不受 NR- 前缀限制。"""
        items = [dict(sample_row(i), sample_id=f"N{i:03d}") for i in range(1, 51)]
        raw = (json.dumps({"items": items}, ensure_ascii=False, indent=2)
               + "\n").encode("utf-8")
        self.samples.write_bytes(raw)
        self.lock.write_bytes(
            f"{hashlib.sha256(raw).hexdigest()}  {self.samples.name}\n".encode("utf-8"))
        with self.human.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["sample_id", "human_verdict"])
            for i in range(1, 51):
                writer.writerow([f"N{i:03d}", "no_risk"])
        self.write_system([dict(system_row(i), sample_id=f"N{i:03d}")
                           for i in range(1, 51)])
        completed, report = self.run_score()
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(report["consistent_count"], 50)

    def test_24_dev_eval_sample_set_is_compatible(self):
        """用真实开发评测样本文件跑结构兼容性；标签全为 synthetic，绝非人工金标。"""
        source = ROOT / "data/news_risk/dev_eval_samples.json"
        raw = source.read_bytes()
        self.samples.write_bytes(raw)
        self.lock.write_bytes(
            f"{hashlib.sha256(raw).hexdigest()}  {self.samples.name}\n".encode("utf-8"))
        items = json.loads(raw.decode("utf-8-sig"))["items"]
        rows = []
        with self.human.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["sample_id", "human_verdict"])
            for item in items:
                writer.writerow([item["sample_id"], "no_risk"])
                rows.append({
                    "sample_id": item["sample_id"],
                    "symbol": item["symbol"],
                    "risk_verdict": "no_risk",
                    "evidence": item['text'][:20],
                    "profile": "v2",
                    "level": "无",
                    "review_action": "无需预警；按常规归档。",
                })
        self.write_system(rows)
        # 审计 P0：合成标签必须显式走测试模式，且断言不能出具正式通过结论。
        completed, report = self.run_score("--label-source", "synthetic-test-fixture")
        self.assertEqual(completed.returncode, 1)
        self.assertFalse(report["formal"])
        self.assertFalse(report["pass"])
        self.assertTrue(report["not_acceptance_evidence"])
        self.assertEqual(report["scope"], "engineering-selftest")
        self.assertEqual(report["structural_errors"], [])
        self.assertEqual(report["consistent_count"], 50)
        self.assertEqual(report["denominator"], 50)

    # ---------- 审计 P0 复现与门禁 ----------
    def test_25_default_source_never_grants_formal_pass(self):
        """还原审计反例：两列 CSV + 默认参数，即使 50/50 也不能出正式结论。"""
        self.baseline()
        completed, report = self.run_raw([])
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(report["consistent_count"], 50)
        self.assertFalse(report["formal"])
        self.assertFalse(report["pass"])
        self.assertTrue(report["not_acceptance_evidence"])
        self.assertEqual(report["status"], "unverified-source")
        self.assertEqual(report["human_labels_source"], "unverified")
        self.assertTrue(any("标签来源" in m
                            for m in report["formal_requirements_missing"]))

    def test_26_declared_human_source_without_registration_still_unverified(self):
        """只把 --label-source 写成人工、没有封存记录，同样不给 formal。"""
        self.baseline()
        completed, report = self.run_raw(["--label-source", "human-annotation"])
        self.assertEqual(completed.returncode, 1)
        self.assertFalse(report["formal"])
        self.assertTrue(any("封存记录" in m
                            for m in report["formal_requirements_missing"]))

    def test_27_wrong_symbol_in_human_table_is_caught(self):
        """人工表整列写错 symbol 也能通过 —— 该漏洞必须被堵住。"""
        self.baseline()
        with self.human.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["sample_id", "human_verdict", "symbol"])
            for i in range(1, 51):
                writer.writerow([f"NR-{i:03d}", "no_risk", "WRONG-SYMBOL"])
        completed, report = self.run_score()
        self.assertEqual(completed.returncode, 1)
        self.assertTrue(any("人工表标的与样本不一致" in e
                            for e in report["structural_errors"]))
        self.assertFalse(report["formal"])

    def test_28_registration_hash_mismatch_blocks_formal(self):
        """封存记录里的人工表哈希对不上，不能出正式结论。"""
        self.baseline()
        self.build_formal_fixtures()
        document = json.loads(self.registration.read_text(encoding="utf-8"))
        document["human_labels_sha256"] = "0" * 64
        self.registration.write_bytes((json.dumps(document, ensure_ascii=False,
                                                 indent=2) + "\n").encode("utf-8"))
        completed, report = self.run_raw([
            "--label-source", "human-annotation",
            "--registration", str(self.registration),
            "--sample-registry", str(self.registry), "--profile", "v2",
        ])
        self.assertEqual(completed.returncode, 1)
        self.assertFalse(report["formal"])
        self.assertTrue(any("人工表 SHA256" in m
                            for m in report["formal_requirements_missing"]))

    def test_29_dev_eval_set_is_refused_for_formal_blind_review(self):
        """登记清单标明「已公开开发标签」的样本集，不得用于正式盲评。"""
        sys.path.insert(0, str(ROOT / "eval/news_risk"))
        import score as score_module
        samples_path = ROOT / "data/news_risk/dev_eval_samples.json"
        raw = samples_path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        self.registration.write_bytes((json.dumps({
            "schema": "topic16-human-annotation-registration-v1",
            "sample_set_id": "NEWS-DEV-2026-11-09",
            "samples_sha256": digest, "human_labels_sha256": "0" * 64,
            "annotator": "独立复核者（合成测试）", "independent_review": True,
            "sealed_at": "2026-11-01T09:00:00+08:00", "profile": "v2",
        }, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        prereg, prereg_problems = score_module.load_preregistration()
        ok, missing, _ = score_module.verify_provenance(
            label_source="human-annotation",
            registration_path=self.registration,
            registry_path=ROOT / "data/news_risk/sample_registry.json",
            samples_path=samples_path, human_path=samples_path,
            system_path=samples_path,
            samples_sha=digest, human_sha="0" * 64, lock_verified=True,
            expected_profile="v2", system_header={"profile": "v2"},
            structural_errors=[], consistent=50,
            preregistration=prereg,
            preregistration_problems=prereg_problems,
            fixture_mode=False, human_symbol_rows=50)
        self.assertFalse(ok)
        self.assertTrue(any("已用于规则开发" in m for m in missing))
        self.assertTrue(any("禁用指纹" in m for m in missing),
                        "独立预登记的禁用指纹也应硬性拒绝，不能只靠自供清单")

    def test_30_incomplete_human_table_is_fail_not_pending(self):
        """人工表只有 49 条：返回码 1 / fail，并列出缺项，不再误报 pending。"""
        self.baseline()
        with self.human.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["sample_id", "human_verdict"])
            for i in range(1, 50):
                writer.writerow([f"NR-{i:03d}", "no_risk"])
        completed, report = self.run_score()
        self.assertEqual(completed.returncode, 1)
        self.assertNotEqual(report["status"], "pending-human-annotation")
        self.assertTrue(any("缺 1 条" in e for e in report["structural_errors"]))

    def test_31_ai_reference_converted_to_two_column_csv_never_forms(self):
        """逐字还原审计 P0 反例：把 AI 参考判断转成两列 CSV，仍不得出正式结论。"""
        self.baseline()
        ai_reference = {
            "label_origin": "AI 起草的参考判断",
            "human_review_status": "未经独立人工判定",
            "references": [{"id": f"NR-{i:03d}", "label": "无风险信号"}
                           for i in range(1, 51)],
        }
        labels = {"有风险信号": "risk", "无风险信号": "no_risk"}
        with self.human.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["sample_id", "human_verdict"])
            for entry in ai_reference["references"]:
                writer.writerow([entry["id"], labels[entry["label"]]])
        self.write_system([system_row(i) for i in range(1, 51)])
        completed, report = self.run_raw([])
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(report["consistent_count"], 50)
        self.assertFalse(report["formal"])
        self.assertFalse(report["pass"])
        self.assertTrue(report["not_acceptance_evidence"])
        self.assertEqual(report["scope"], "engineering-selftest")

    def test_32_cli_v1_output_carries_profile_for_strict_scoring(self):
        """回归：v1 规则不带 profile；CLI 不补齐会让严格模式把 50 条全判结构错误。"""
        self.baseline()
        cli_output = self.dir / "cli_v1_output.json"
        completed = subprocess.run(
            [sys.executable, "-m", "news_risk", str(self.samples),
             "--profile", "v1", "--output", str(cli_output)],
            cwd=str(ROOT), capture_output=True,
        )
        self.assertEqual(completed.returncode, 0,
                         completed.stderr.decode("utf-8", "replace"))
        payload = json.loads(cli_output.read_text(encoding="utf-8"))
        self.assertEqual(payload["profile"], "v1")
        self.assertEqual(len(payload["items"]), 50)
        self.assertEqual({row.get("profile") for row in payload["items"]}, {"v1"})

        # 用真实 v1 CLI 输出走一遍完整正式证据链，不得出现 profile 相关结构错误。
        self.system.write_bytes(cli_output.read_bytes())
        self.build_formal_fixtures(profile="v1")
        completed, report = self.run_raw([
            "--label-source", "human-annotation",
            "--registration", str(self.registration),
            "--sample-registry", str(self.registry),
            "--profile", "v1",
        ])
        profile_errors = [e for e in report.get("structural_errors", [])
                          if "profile" in e]
        self.assertEqual(profile_errors, [])
        self.assertNotIn("预测 profile 与封存记录不一致",
                         report.get("formal_requirements_missing", []))

    # ---------- 审计 P2-4：封存时间必须带时区且不得在未来 ----------
    def test_33_sealed_at_without_timezone_or_in_future_never_forms(self):
        self.baseline()
        self.build_formal_fixtures(profile="v2")
        registration = json.loads(self.registration.read_text(encoding="utf-8"))
        formal_args = [
            "--label-source", "human-annotation",
            "--registration", str(self.registration),
            "--sample-registry", str(self.registry),
            "--profile", "v2",
        ]
        for sealed in ("2099-01-01T00:00:00", "2099-01-01",
                       "2099-01-01T00:00:00+08:00", "2026-11-01T09:00:00"):
            with self.subTest(sealed=sealed):
                registration["sealed_at"] = sealed
                self.registration.write_bytes(
                    (json.dumps(registration, ensure_ascii=False, indent=2)
                     + "\n").encode("utf-8"))
                completed, report = self.run_raw(formal_args)
                self.assertEqual(completed.returncode, 1)
                self.assertFalse(report["formal"])
        # 控制组：带显式时区的过去时间仍可出具正式结论
        registration["sealed_at"] = "2026-11-01T09:00:00+08:00"
        self.registration.write_bytes(
            (json.dumps(registration, ensure_ascii=False, indent=2)
             + "\n").encode("utf-8"))
        completed, report = self.run_raw(formal_args)
        self.assertEqual(completed.returncode, 1)
        self.assertTrue(report["machine_chain_valid"])

    def test_34_entities_sha256_must_match_registry_and_system_header(self):
        """登记清单声明主体映射时，系统输出与封存记录必须带同一哈希。"""
        self.baseline()
        self.build_formal_fixtures(profile="v2")
        registry = json.loads(self.registry.read_text(encoding="utf-8"))
        digest = "a" * 64
        registry["registered_sets"][0]["entities_sha256"] = digest
        self.registry.write_bytes(
            (json.dumps(registry, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        formal_args = [
            "--label-source", "human-annotation",
            "--registration", str(self.registration),
            "--sample-registry", str(self.registry),
            "--profile", "v2",
        ]
        completed, report = self.run_raw(formal_args)
        self.assertFalse(report["formal"])
        self.assertTrue(any("主体映射 SHA256" in m
                            for m in report["formal_requirements_missing"]))

        document = json.loads(self.system.read_text(encoding="utf-8"))
        document["entities_sha256"] = digest
        self.system.write_bytes(
            (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        completed, report = self.run_raw(formal_args)
        self.assertEqual(completed.returncode, 1)
        self.assertTrue(report["machine_chain_valid"])

        # 封存记录里的哈希若与系统输出不符，同样拒绝
        registration = json.loads(self.registration.read_text(encoding="utf-8"))
        registration["entities_sha256"] = "b" * 64
        self.registration.write_bytes(
            (json.dumps(registration, ensure_ascii=False, indent=2)
             + "\n").encode("utf-8"))
        completed, report = self.run_raw(formal_args)
        self.assertFalse(report["formal"])

    def test_35_registry_entities_hash_does_not_block_v1_formal_run(self):
        """v1 用内置主体表；登记清单为 v2 预留的哈希不能反过来卡死 v1。"""
        self.baseline()
        cli_output = self.dir / "cli_v1_output.json"
        completed = subprocess.run(
            [sys.executable, "-m", "news_risk", str(self.samples),
             "--profile", "v1", "--output", str(cli_output)],
            cwd=str(ROOT), capture_output=True)
        self.assertEqual(completed.returncode, 0,
                         completed.stderr.decode("utf-8", "replace"))
        self.system.write_bytes(cli_output.read_bytes())
        self.build_formal_fixtures(profile="v1")
        registry = json.loads(self.registry.read_text(encoding="utf-8"))
        # 模拟真实 NR-2026-11-09 登记项：为 v2 写定了主体映射哈希
        registry["registered_sets"][0]["entities_sha256"] = "a" * 64
        self.registry.write_bytes(
            (json.dumps(registry, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        completed, report = self.run_raw([
            "--label-source", "human-annotation",
            "--registration", str(self.registration),
            "--sample-registry", str(self.registry),
            "--profile", "v1",
        ])
        self.assertEqual(completed.returncode, 1)
        self.assertTrue(report["machine_chain_valid"])
        self.assertNotIn(
            "主体映射 SHA256 与登记清单不一致：None",
            report.get("formal_requirements_missing", []))
        self.assertEqual(
            report["provenance"]["entities_profile_gate"], "not-required")

        # 控制组：同一登记清单换回 v2，仍必须要求系统输出带匹配哈希。
        completed, report = self.run_raw([
            "--label-source", "human-annotation",
            "--registration", str(self.registration),
            "--sample-registry", str(self.registry),
            "--profile", "v2",
        ])
        self.assertFalse(report["formal"])


if __name__ == "__main__":
    unittest.main()
