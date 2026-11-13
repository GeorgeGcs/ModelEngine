import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from portfolio_risk.__main__ import replay


ROOT = Path(__file__).resolve().parents[1]


class ReplayTests(unittest.TestCase):
    def test_shipped_demo_runs_through_command_line(self):
        command = [sys.executable, "-m", "portfolio_risk", "data/demo.json"]
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, check=True)
        result = json.loads(completed.stdout)
        self.assertEqual(result["portfolio_count"], 3)
        self.assertEqual(result["snapshot_count"], 7)
        self.assertEqual(result["signal_count"], 4)
        events = [
            (signal["timestamp"], signal["portfolio_id"], signal["symbol"], signal["rule"])
            for signal in result["signals"]
        ]
        self.assertEqual(events, [
            ("2026-11-06T02:00:00Z", "stock-demo", "SIM-STOCK-A", "stop_loss"),
            ("2026-11-06T03:00:00Z", "fund-demo", "SIM-FUND-B", "take_profit"),
            ("2026-11-06T06:00:00Z", "mixed-demo", "SIM-STOCK-C", "stop_loss"),
            ("2026-11-06T06:00:00Z", "mixed-demo", "SIM-FUND-D", "take_profit"),
        ])

    def test_incomplete_last_snapshot_cannot_look_like_a_successful_replay(self):
        document = json.loads((ROOT / "data/demo.json").read_text(encoding="utf-8"))
        document["snapshots"][-1]["prices"] = {}
        with TemporaryDirectory() as directory:
            path = Path(directory) / "missing-price.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, "-m", "portfolio_risk", str(path)],
                cwd=ROOT,
                capture_output=True,
            )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b"")
        self.assertTrue(completed.stderr)

    def test_prevent_double_counting_duplicate_portfolios(self):
        document = json.loads((ROOT / "data/demo.json").read_text(encoding="utf-8"))
        document["portfolios"].append(document["portfolios"][0])
        with self.assertRaisesRegex(ValueError, "组合 id 重复"):
            replay(document)
