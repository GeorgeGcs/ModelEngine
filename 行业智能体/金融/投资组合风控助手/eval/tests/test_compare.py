"""反向验证复算脚本：基线必须过，删/添/重复一条必须都被抓到。"""

import ast
import hashlib
import json
import subprocess
import shutil
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

EVAL_DIR = Path(__file__).resolve().parents[1]
ROOT = EVAL_DIR.parent
COMPARE = EVAL_DIR / "compare.py"
EXPECTED = EVAL_DIR / "expected_20.json"


class FrozenEvalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        completed = subprocess.run(
            [sys.executable, "-m", "portfolio_risk", "data/eval_20.json"],
            cwd=ROOT, capture_output=True, check=True,
        )
        cls.baseline = json.loads(completed.stdout.decode("utf-8"))
        cls.expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
        holder = TemporaryDirectory()
        cls.addClassCleanup(holder.cleanup)
        cls.tmp = Path(holder.name)

    def run_compare(self, signals, name, expected_path=EXPECTED, expect_report=True):
        actual = self.tmp / f"{name}_actual.json"
        report = self.tmp / f"{name}_report.json"
        document = {
            "portfolio_count": self.baseline["portfolio_count"],
            "snapshot_count": self.baseline["snapshot_count"],
            "signal_count": len(signals),
            "signals": signals,
        }
        actual.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, str(COMPARE),
             "--expected", str(expected_path),
             "--actual", str(actual),
             "--report", str(report)],
            cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        if expect_report:
            self.assertTrue(report.exists(), f"比对未产出报告：{completed.stderr}")
        data = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
        return completed, data

    def test_baseline_passes_with_20_true_positives(self):
        completed, report = self.run_compare(self.baseline["signals"], "baseline")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertTrue(report["pass"])
        self.assertEqual(report["metrics"]["TP"], 20)
        self.assertEqual(report["metrics"]["FP"], 0)
        self.assertEqual(report["metrics"]["FN"], 0)
        self.assertEqual(report["metrics"]["duplicates"], 0)
        self.assertEqual(report["metrics"]["expected_cells"], 48)
        self.assertEqual(report["differences"], [])

    def test_deleting_one_signal_is_caught_as_false_negative(self):
        """人为删一条：必须报 FN=1，且退出码非 0。"""
        tampered = self.baseline["signals"][1:]
        self.assertEqual(len(tampered), 19)
        completed, report = self.run_compare(tampered, "drop_one")
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(report["metrics"]["FN"], 1)
        self.assertEqual(report["metrics"]["TP"], 19)
        self.assertFalse(report["pass"])
        missing = [d for d in report["differences"] if d["type"] == "MISSING"]
        self.assertEqual(len(missing), 1)
        self.assertIn("漏报", missing[0]["reason"])

    def test_adding_one_signal_is_caught_as_false_positive(self):
        """人为添一条：在非触发格塞入信号，必须报 FP=1（误触发）。"""
        cell = self.expected["non_trigger_cells"][0]
        injected = {
            "event_id": hashlib.sha256(b"injected-false-positive").hexdigest(),
            "portfolio_id": cell["portfolio_id"],
            "timestamp": cell["timestamp"],
            "symbol": cell["symbol"],
            "rule": "stop_loss",
            "price": cell["price"],
            "threshold": cell["stop_loss"],
        }
        completed, report = self.run_compare(
            self.baseline["signals"] + [injected], "add_one"
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(report["metrics"]["FP"], 1)
        self.assertEqual(report["metrics"]["TP"], 20)
        self.assertFalse(report["pass"])
        unexpected = [d for d in report["differences"] if d["type"] == "UNEXPECTED"]
        self.assertEqual(len(unexpected), 1)
        self.assertIn("非触发格", unexpected[0]["reason"])

    def test_duplicating_one_signal_is_caught_as_duplicate_and_fp(self):
        """人为重复一条：必须报 DUPLICATE，多出的一条同时计 FP。"""
        duplicated = self.baseline["signals"] + [self.baseline["signals"][0]]
        completed, report = self.run_compare(duplicated, "duplicate_one")
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(report["metrics"]["duplicates"], 1)
        self.assertEqual(report["metrics"]["FP"], 1)
        self.assertEqual(report["metrics"]["TP"], 20)
        self.assertFalse(report["pass"])
        kinds = {d["type"] for d in report["differences"]}
        self.assertIn("DUPLICATE", kinds)
        self.assertIn("EXTRA_DUPLICATE", kinds)

    def test_frozen_annotation_cannot_be_silently_changed(self):
        """改动标注但不重新冻结，比对必须拒绝运行。"""
        changed_dir = self.tmp / "changed_annotation"
        changed_dir.mkdir(exist_ok=True)
        changed = changed_dir / EXPECTED.name
        lock = changed_dir / (EXPECTED.name + ".sha256")
        document = json.loads(EXPECTED.read_text(encoding="utf-8"))
        document["triggers"][0]["price"] = "999.00"
        changed.write_text(
            json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        lock.write_text(
            (EXPECTED.with_name(EXPECTED.name + ".sha256")).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        completed, _ = self.run_compare(
            self.baseline["signals"], "tampered_annotation",
            expected_path=changed, expect_report=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("冻结标注已被改动", completed.stderr)

    def test_compare_script_is_independent_of_rule_engine(self):
        """比对脚本不得导入被测包；标注不得含引擎生成的 event_id。"""
        source = COMPARE.read_text(encoding="utf-8")
        roots = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        self.assertNotIn("portfolio_risk", roots)
        self.assertNotIn("evaluate_snapshot(", source)
        for trigger in self.expected["triggers"]:
            self.assertNotIn("event_id", trigger)
        self.assertEqual(self.expected["annotation_method"], "manual")
        self.assertEqual(self.expected["scope"], "offline-replay-layer")

    def test_empty_benchmark_cannot_be_refrozen(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / EXPECTED.name
            document = json.loads(EXPECTED.read_bytes())
            document.update(triggers=[], non_trigger_cells=[])
            document["source"].update(trigger_count=0, non_trigger_count=0, cells_total=0)
            path.write_text(json.dumps(document), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(COMPARE), "--expected", str(path), "--refresh-freeze"],
                capture_output=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertFalse(path.with_suffix(".json.sha256").exists())

    def test_changed_non_trigger_input_invalidates_previous_report(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("data", "eval", "portfolio_risk"):
                shutil.copytree(ROOT / name, root / name)
            command = [sys.executable, str(root / "eval/run_eval.py")]
            subprocess.run(command, cwd=root, capture_output=True, check=True)
            report = root / "eval/results/compare_report.json"
            self.assertTrue(json.loads(report.read_bytes())["pass"])
            input_path = root / "data/eval_20.json"
            document = json.loads(input_path.read_bytes())
            document["snapshots"][0]["prices"]["SIM-A"] = "95.005"
            input_path.write_text(json.dumps(document), encoding="utf-8")
            result = subprocess.run(command, cwd=root, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(report.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
