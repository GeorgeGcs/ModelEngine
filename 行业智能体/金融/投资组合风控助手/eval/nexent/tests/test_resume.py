"""Exercise the smoke-to-full driver transition with local fixture responses."""

import json
import shutil
import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import run_nexent_eval as driver


class ResumeTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = self.root / "run.db"
        self.report = self.root / "preflight.json"
        shutil.copytree(driver.ROOT / "data", self.root / "data")
        fixture = json.loads((HERE / "fixtures/platform_responses.json").read_bytes())
        self.responses = {t["task_key"]: t for t in fixture["tasks"]}
        self.calls = []

    def invoke(self, config, *, task, run_id):
        key = task["task_key"]
        self.calls.append(key)
        row = self.responses[key]
        ids, signals = row["platform_ids"], row["tool_result"]
        return dict(
            signals=signals, http_status=200, evidence_status="verified",
            tool_evidence=dict(
                call_id=ids["tool_call_id"], pairing="sse-call-id", tool_success=True,
                zero_hit=not signals, actual_tool_arguments=task["tool_arguments"],
                actual_arguments_source="sse-tool-call",
            ),
            platform_ids=ids, response_header_ids={}, execution_keys=[],
            tool_call_ids=[ids["tool_call_id"]], raw_response=row["raw_response"],
        )

    def drive(self, mode, db=None):
        argv = ["driver", "--run-id", "LOCAL-DRIVER-TEST", "--mode", mode,
                "--db", str(db or self.db), "--records-dir", str(self.root / "records"),
                "--status-output", str(self.root / "status.json"),
                "--preflight-output", str(self.report)]
        config = driver.PlatformConfig("http://mock.invalid", "fixture-token", "mock-agent")
        with (patch.object(sys, "argv", argv), patch.object(driver, "ROOT", self.root),
              patch.object(driver.PlatformConfig, "from_env", return_value=config),
              patch.object(driver, "probe", return_value={"status": "reachable", "checks": []}),
              patch.object(driver, "invoke_task", side_effect=self.invoke)):
            return driver.main()

    def test_full_preserves_smoke_tasks_and_calls_only_the_remainder(self):
        self.assertEqual(self.drive("smoke"), 0)
        rows = json.loads(self.report.read_bytes())["records"]
        originals = {self.root / r["record_ref"]: (self.root / r["record_ref"]).read_bytes()
                     for r in rows}
        self.assertEqual(len(originals), 4)
        self.assertEqual(self.drive("full"), 0)
        self.assertCountEqual(self.calls, self.responses.keys())
        self.assertTrue(all(path.read_bytes() == raw for path, raw in originals.items()))
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0], 24)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM final_signals").fetchone()[0], 20)

    def test_full_refuses_a_different_db_or_altered_smoke_evidence_before_calling(self):
        self.assertEqual(self.drive("smoke"), 0)
        calls = self.calls.copy()
        self.assertEqual(self.drive("full", self.root / "other.db"), 4)
        first = json.loads(self.report.read_bytes())["records"][0]
        path = self.root / first["record_ref"]
        path.write_bytes(path.read_bytes() + b" ")
        self.assertEqual(self.drive("full"), 4)
        self.assertEqual(self.calls, calls)
