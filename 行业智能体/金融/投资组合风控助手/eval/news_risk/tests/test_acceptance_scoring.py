"""舆情正式评分：独立预登记、有效性/达标分离、旧报告失效的显式合成夹具测试。

⚠️ 本文件所有样本、人工表、封存记录均为临时合成夹具，
不是人工金标、不是 50 条评测答案、不作任何验收证据；文件不写入仓库。

运行：python -m unittest discover -s eval/news_risk/tests -p "test_acceptance_scoring.py" -v

覆盖服务器侧复核指令：
  - 固定独立预登记的样本与实体映射摘要；
  - 禁止用自供清单把已公开答案的开发集转为正式集；
  - 人工表逐行核对标的；
  - 区分「正式评测是否有效」与「是否达到 43/50」：真实 42/50 记为正式未达标；
  - 修复开发评分异常后旧报告仍显示有效的问题（两个评分器同口径）；
  - 正式候选用 NR 集，AI 参考的 50/50 只保留为开发结果。
"""
from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
SCORE = ROOT / "eval/news_risk/score.py"
DEV_SCORER = ROOT / "eval/news_risk/score_dev_eval.py"
PREREG = ROOT / "data/news_risk/acceptance_preregistration.json"
DEV_SAMPLES = ROOT / "data/news_risk/dev_eval_samples.json"
sys.path.insert(0, str(ROOT / "eval/news_risk"))
import score as score_module  # noqa: E402

SYMBOLS = ("SIM-A", "SIM-B", "SIM-C", "SIM-D")
DENOMINATOR = 50
THRESHOLD = 43


def sample_row(i: int) -> dict:
    return {"sample_id": f"NR-{i:03d}",
            "symbol": SYMBOLS[(i - 1) % 4],
            "published_at": "2026-11-05T10:00:00+08:00",
            "source": "合成夹具模拟来源",
            "text": f"合成夹具舆情原文第 {i} 条，仅用于评分脚本机制验证。"}


def system_row(i: int, verdict: str = "no_risk") -> dict:
    row = sample_row(i)
    return {"sample_id": row["sample_id"], "symbol": row["symbol"],
            "risk_verdict": verdict, "evidence": row["text"][:12],
            "level": "无", "review_action": "无需预警；按常规归档。",
            "profile": "v2"}


class AcceptanceCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.samples = self.dir / "samples_50.json"
        self.lock = self.dir / "samples_50.json.sha256"
        self.human = self.dir / "human_labels.csv"
        self.system = self.dir / "system_output.json"
        self.report = self.dir / "consistency_report.json"
        self.registration = self.dir / "annotation_registration.json"
        self.registry = self.dir / "sample_registry.json"

    # ---------- 夹具构造 ----------
    def write_samples(self, count: int = DENOMINATOR) -> str:
        items = [sample_row(i) for i in range(1, count + 1)]
        raw = (json.dumps({"items": items}, ensure_ascii=False, indent=2)
               + "\n").encode("utf-8")
        self.samples.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        self.lock.write_bytes(f"{digest}  {self.samples.name}\n".encode("utf-8"))
        return digest

    def write_human(self, *, consistent: int = DENOMINATOR,
                    with_symbol: bool = True) -> None:
        with self.human.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["sample_id", "human_verdict"]
                            + (["symbol"] if with_symbol else []))
            for i in range(1, DENOMINATOR + 1):
                row = sample_row(i)
                row_values = [row["sample_id"], "no_risk"]
                if with_symbol:
                    row_values.append(row["symbol"])
                writer.writerow(row_values)
        # 让前 (50-consistent) 条系统输出与人工表相反
        self.system_rows = [
            system_row(i, "risk" if i <= DENOMINATOR - consistent
                       else "no_risk")
            for i in range(1, DENOMINATOR + 1)]

    def write_system(self) -> None:
        self.system.write_bytes((json.dumps({
            "schema": "topic16-news-risk-output-v1",
            "scope": "system-output", "profile": "v2",
            "count": len(self.system_rows), "items": self.system_rows,
        }, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    def write_evidence_chain(self, samples_sha: str, *,
                             set_id: str = "NR-2026-11-09",
                             profile: str = "v2",
                             not_recommended: bool = False,
                             registry_sha: str | None = None,
                             entities_sha: str | None = None) -> None:
        human_sha = hashlib.sha256(self.human.read_bytes()).hexdigest()
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
                "sha256": registry_sha or samples_sha,
                "not_recommended_for_blind_acceptance": not_recommended,
                **({"entities_sha256": entities_sha}
                   if entities_sha else {}),
            }],
        }, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    def formal_args(self, *, fixture: bool = True) -> list[str]:
        return [
            *self.base_args(),
            *(["--fixture-mode", "synthetic-fixture"] if fixture else []),
            "--label-source", "human-annotation",
            "--registration", str(self.registration),
            "--sample-registry", str(self.registry),
            "--profile", "v2",
        ]

    def base_args(self) -> list[str]:
        return ["--samples", str(self.samples),
                "--human", str(self.human),
                "--system", str(self.system),
                "--report", str(self.report)]

    def run_score(self, extra: list[str]) -> tuple[subprocess.CompletedProcess, dict]:
        completed = subprocess.run(
            [sys.executable, str(SCORE), *extra],
            cwd=str(ROOT), capture_output=True)
        payload = json.loads(completed.stdout.decode("utf-8"))
        return completed, payload

    def full_fixture(self, *, consistent: int = DENOMINATOR,
                     with_symbol: bool = True) -> str:
        sha = self.write_samples()
        self.write_human(consistent=consistent, with_symbol=with_symbol)
        self.write_system()
        self.write_evidence_chain(sha)
        return sha

    # ---------- 独立预登记 ----------
    def test_01_preregistration_matches_builtin_constants(self):
        summary, problems = score_module.load_preregistration()
        self.assertEqual(problems, [])
        candidate = summary["candidate"]
        for key, expected in score_module.BUILTIN_PREREGISTRATION.items():
            self.assertEqual(candidate.get(key), expected, key)
        self.assertIn(
            hashlib.sha256(DEV_SAMPLES.read_bytes()).hexdigest(),
            summary["forbidden"])
        self.assertEqual(
            summary["sha256"],
            hashlib.sha256(PREREG.read_bytes()).hexdigest())

    def test_02_tampered_preregistration_is_detected(self):
        """只改登记文件不改代码，内置常量交叉核对必须报错。"""
        tampered = json.loads(PREREG.read_text(encoding="utf-8"))
        tampered["formal_candidate"]["samples_sha256"] = "0" * 64
        fake = self.dir / "tampered_preregistration.json"
        fake.write_bytes((json.dumps(tampered, ensure_ascii=False, indent=2)
                          + "\n").encode("utf-8"))
        with mock.patch.object(score_module, "PREREGISTRATION_PATH", fake):
            _summary, problems = score_module.load_preregistration()
        self.assertTrue(any("samples_sha256" in p for p in problems))

    # ---------- 有效性 / 达标分离 ----------
    def test_03_real_42_of_50_is_formal_but_not_met(self):
        """指令核心：42/50 必须记为「正式评测有效但未达标」，不是「无效」。"""
        self.full_fixture(consistent=42)
        completed, report = self.run_score(self.formal_args())
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(report["consistent_count"], 42)
        self.assertTrue(report["machine_chain_valid"], "证据链齐全时 formal 不受分数影响")
        self.assertTrue(report["machine_chain_valid"])
        self.assertFalse(report["meets_85_percent"])
        self.assertFalse(report["pass"])
        self.assertEqual(report["status"], "unverified-source")
        self.assertNotIn("未达到", " ".join(report["formal_requirements_missing"]))
        self.assertTrue(report["synthetic_fixture"])
        self.assertTrue(report["not_acceptance_evidence"])

    def test_04_43_of_50_passes_and_42_does_not(self):
        self.full_fixture(consistent=43)
        completed, report = self.run_score(self.formal_args())
        self.assertEqual(completed.returncode, 1)
        self.assertTrue(report["machine_chain_valid"])
        self.assertTrue(report["meets_85_percent"])
        self.assertTrue(report["engineering_would_pass"])
        self.assertEqual(report["status"], "unverified-source")

        self.full_fixture(consistent=42)
        completed2, report2 = self.run_score(self.formal_args())
        self.assertEqual(completed2.returncode, 1)
        self.assertTrue(report2["machine_chain_valid"])
        self.assertFalse(report2["pass"])

    def test_05_fixture_mode_is_explicitly_marked_not_evidence(self):
        self.full_fixture()
        _completed, report = self.run_score(self.formal_args())
        self.assertTrue(report["synthetic_fixture"])
        self.assertTrue(report["not_acceptance_evidence"])
        self.assertIsNotNone(report["fixture_warning"])
        self.assertEqual(report["preregistration"]["file"],
                         "data/news_risk/acceptance_preregistration.json")

    # ---------- 自供清单不能洗白开发集 ----------
    def test_06_self_supplied_registry_cannot_whitelist_dev_set(self):
        """自供清单把 NEWS-DEV 标为可用也没用：内置禁用指纹硬性拒绝。"""
        samples_sha = self.write_samples()
        self.write_human()
        self.write_system()
        dev_digest = hashlib.sha256(DEV_SAMPLES.read_bytes()).hexdigest()
        # 自供清单：把开发集伪装成「未公开答案、允许盲评」
        self.write_evidence_chain(
            dev_digest, set_id="NEWS-DEV-2026-11-09",
            not_recommended=False, registry_sha=dev_digest)
        prereg, prereg_problems = score_module.load_preregistration()
        ok, missing, _ = score_module.verify_provenance(
            label_source="human-annotation",
            registration_path=self.registration,
            registry_path=self.registry,
            samples_path=DEV_SAMPLES, human_path=self.human,
            system_path=self.system,
            samples_sha=dev_digest,
            human_sha=hashlib.sha256(self.human.read_bytes()).hexdigest(),
            lock_verified=True, expected_profile="v2",
            system_header={"profile": "v2"}, structural_errors=[],
            consistent=50, preregistration=prereg,
            preregistration_problems=prereg_problems,
            fixture_mode=False, human_symbol_rows=DENOMINATOR)
        self.assertFalse(ok)
        self.assertTrue(any("禁用指纹" in m for m in missing))
        # 即便直接把开发集喂给 CLI 正式模式，也拿不到 formal=true
        formal_args = self.formal_args(fixture=False)
        formal_args[formal_args.index("--samples") + 1] = str(DEV_SAMPLES)
        _completed, report = self.run_score(formal_args)
        self.assertFalse(report["formal"])
        self.assertTrue(any("禁用指纹" in m
                            for m in report["formal_requirements_missing"]))

    def test_07_formal_mode_rejects_non_preregistered_samples(self):
        """正式模式不认自供清单指定的其他样本集（合成样本未预登记）。"""
        self.full_fixture()
        _completed, report = self.run_score(self.formal_args(fixture=False))
        self.assertFalse(report["formal"])
        self.assertTrue(any("独立预登记的正式候选" in m
                            for m in report["formal_requirements_missing"]))

    # ---------- 人工表逐行核对标的 ----------
    def test_08_formal_mode_requires_symbol_on_every_human_row(self):
        self.full_fixture(with_symbol=False)
        _completed, report = self.run_score(self.formal_args(fixture=False))
        self.assertFalse(report["formal"])
        self.assertTrue(any("逐行" in m
                            for m in report["formal_requirements_missing"]))
        self.assertEqual(
            report["formal_annotation_requirements"]["human_symbol_rows"], 0)

        # 补上 50 行 symbol 后，这一条缺口必须消失（其余正式模式缺口仍在）
        self.full_fixture(with_symbol=True)
        _completed2, report2 = self.run_score(self.formal_args(fixture=False))
        self.assertFalse(any("逐行" in m
                             for m in report2["formal_requirements_missing"]))
        self.assertEqual(
            report2["formal_annotation_requirements"]["human_symbol_rows"],
            DENOMINATOR)

    def test_09_wrong_symbol_row_is_a_structural_error(self):
        """人工表某行 symbol 写错，逐行核对必须报结构错误。"""
        self.full_fixture()
        rows = list(csv.reader(self.human.read_text(
            encoding="utf-8-sig").splitlines()))
        rows[3][2] = "SIM-WRONG"
        with self.human.open("w", encoding="utf-8-sig", newline="") as handle:
            csv.writer(handle).writerows(rows)
        _completed, report = self.run_score(self.formal_args())
        self.assertFalse(report["formal"])
        self.assertTrue(any("人工表标的与样本不一致" in e
                            for e in report["structural_errors"]))

    # ---------- AI 参考 50/50 只保留为开发结果 ----------
    def test_10_dev_eval_report_is_not_acceptance_evidence(self):
        dev_report = ROOT / "eval/news_risk/results/dev_eval_consistency_report.json"
        document = json.loads(dev_report.read_text(encoding="utf-8"))
        self.assertEqual(document["ai_reference_match_count"], 50)
        self.assertTrue(document["not_human_judgment_agreement"])
        self.assertTrue(document["not_acceptance_evidence"])
        self.assertTrue(document["not_an_acceptance_threshold"])
        self.assertNotIn("pass", document)
        self.assertNotIn("meets_85_percent_vs_ai_reference", document)

    # ---------- 与开发评分器同类的「旧报告仍显示有效」缺口 ----------
    def read_report(self) -> dict:
        return json.loads(self.report.read_text(encoding="utf-8"))

    def test_11_failed_run_invalidates_previous_passing_report(self):
        """正式评分器也不能留一份 pass=true 的旧报告给只读报告的人。"""
        self.full_fixture()
        ok, first = self.run_score(self.formal_args())
        self.assertEqual(ok.returncode, 1)
        self.assertTrue(first["engineering_would_pass"])
        self.assertTrue(self.read_report()["engineering_would_pass"])

        self.human.unlink()  # 模拟人工表丢失 / 本次评分异常
        failed, pending = self.run_score(self.formal_args())
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(pending["status"], "pending-human-annotation")
        invalidated = self.read_report()
        self.assertEqual(invalidated["schema"],
                         "topic16-formal-report-invalidated-v1")
        self.assertFalse(invalidated["report_valid"])
        self.assertFalse(invalidated["pass"])
        self.assertFalse(invalidated["formal"])
        self.assertNotIn("consistent_count", invalidated)
        self.assertEqual(invalidated["invalidated_by_status"],
                         "pending-human-annotation")
        self.assertTrue(invalidated["stale_report"]["engineering_would_pass"])
        self.assertTrue(invalidated["not_acceptance_evidence"])

    def test_12_failed_run_does_not_create_a_report_when_none_existed(self):
        """旧报告本来不存在时，失败运行仍只输出 stdout，不凭空造报告。"""
        self.full_fixture()
        self.human.unlink()
        completed, _ = self.run_score(self.formal_args())
        self.assertEqual(completed.returncode, 2)
        self.assertFalse(self.report.exists())

    def test_13_invalidation_is_idempotent_and_reversible_by_successful_rerun(self):
        self.full_fixture()
        self.run_score(self.formal_args())
        self.human.unlink()
        self.run_score(self.formal_args())
        self.run_score(self.formal_args())
        second = self.read_report()
        self.assertEqual(second["schema"], "topic16-formal-report-invalidated-v1")
        self.assertEqual(second["stale_report"]["schema"],
                         "topic16-news-risk-consistency-v5")

        self.full_fixture()  # 修好输入后重跑
        ok, restored = self.run_score(self.formal_args())
        self.assertEqual(ok.returncode, 1)
        self.assertTrue(restored["engineering_would_pass"])
        self.assertEqual(self.read_report()["schema"],
                         "topic16-news-risk-consistency-v5")


if __name__ == "__main__":
    unittest.main(verbosity=2)
