"""本地 fixture 测试：验证持久化、导出、对账、重试机制。

⚠️ 这些测试使用 LOCAL FIXTURE（模拟平台响应），不是真实 Nexent 运行。
所有产物 scope 标为 local-fixture，不写入正式的端到端证据路径，
也绝不生成 end-to-end 通过报告。

最关键的一项：某个零命中任务没执行 / 没平台证据时，即使最终信号仍是
那 20 条、比对报告照样 PASS，任务对账也必须 FAIL。
"""

import ast
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent
NEXENT_DIR = HERE.parents[0]
ROOT = NEXENT_DIR.parents[1]
sys.path.insert(0, str(NEXENT_DIR))
import persistence  # noqa: E402

RUN_ID = "LOCAL-FIXTURE-RUN-0001"
SCOPE = "local-fixture"
MANIFEST = NEXENT_DIR / "task_manifest.json"
FIXTURE = HERE / "fixtures" / "platform_responses.json"
EXPECTED = ROOT / "eval" / "expected_20.json"
EXPECTED_LOCK = ROOT / "eval" / "expected_20.json.sha256"


def run_script(relative: str, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / relative), *[str(a) for a in args]],
        cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def read_json(path: Path):
    return json.loads(path.read_bytes().decode("utf-8-sig")) if path.exists() else None


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        holder = TemporaryDirectory()
        cls.addClassCleanup(holder.cleanup)
        cls.dir = Path(holder.name)
        cls.db = cls.dir / "baseline.db"
        cls.fixture = read_json(FIXTURE)
        cls.manifest = read_json(MANIFEST)
        cls.by_key = {t["task_key"]: t for t in cls.manifest["tasks"]}

        conn = persistence.connect(cls.db)
        persistence.init_db(conn)
        persistence.start_run(
            conn, run_id=RUN_ID, platform_label="LOCAL-FIXTURE",
            platform_version=None, agent_id=None,
            source_input_sha256=cls.manifest["source_sha256"],
            task_manifest_sha256="fixture-manifest",
            note="LOCAL FIXTURE — 非平台证据，仅用于机制自检",
        )
        for record in cls.fixture["tasks"]:
            task = cls.by_key[record["task_key"]]
            rel = f"platform_records/task_{record['task_index']:02d}.json"
            path = cls.dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            raw = (json.dumps({
                "schema": "topic16-platform-tool-record-v2",
                "run_id": RUN_ID, "task_key": record["task_key"],
                "task_index": record["task_index"],
                "http_status": 200,
                "call_success": True,
                "tool_name": "evaluate_portfolio_snapshot",
                "tool_arguments": task["tool_arguments"],
                "platform_ids": record["platform_ids"],
                "raw_response": record["raw_response"],
                "tool_result": record["tool_result"],
                "source": "local-fixture",
                "platform_evidence": False,
                "notice": "LOCAL FIXTURE — 非真实 Nexent 平台记录，不得作为验收证据。",
            }, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            path.write_bytes(raw)
            import hashlib
            digest = hashlib.sha256(raw).hexdigest()
            ids = record["platform_ids"]
            task_row = {
                "task_index": record["task_index"],
                "portfolio_id": task["portfolio_id"],
                "snapshot_timestamp": task["timestamp"],
                "input_symbols": task["input_symbols"],
                "cell_count": len(task["cell_keys"]),
                "platform_session_id": ids["session_id"],
                "platform_request_id": ids["request_id"],
                "platform_tool_call_id": ids["tool_call_id"],
                "platform_trace_note": f"LOCAL FIXTURE run_id={RUN_ID} task={record['task_key']}",
                "tool_response_ref": rel,
                "tool_response_sha256": digest,
                "returned_signal_count": len(record["tool_result"]),
                "status": "completed",
                "finished_at": persistence.utc_now(),
            }
            persistence.commit_signals(
                conn, run_id=RUN_ID, task_key=record["task_key"],
                signals=record["tool_result"], attempt=1, task=task_row,
            )
        persistence.finish_run(conn, RUN_ID, "completed")
        cls.task_with_signal = next(
            r["task_key"] for r in cls.fixture["tasks"] if r["tool_result"]
        )
        cls.zero_hit_task = next(
            r["task_key"] for r in cls.fixture["tasks"] if not r["tool_result"]
        )
        conn.close()

    def copy_db(self, tag: str) -> Path:
        target = self.dir / f"{tag}.db"
        shutil.copyfile(self.db, target)
        return target

    def pipeline(self, tag: str, db: Path) -> dict:
        exported = self.dir / f"{tag}_exported.json"
        trace = self.dir / f"{tag}_trace.json"
        compared = self.dir / f"{tag}_compare.json"
        reconciled = self.dir / f"{tag}_reconciliation.json"
        export = run_script(
            "eval/nexent/export_final_signals.py",
            "--run-id", RUN_ID, "--db", db,
            "--output", exported, "--trace-output", trace,
        )
        compare = run_script(
            "eval/compare.py", "--expected", EXPECTED, "--actual", exported,
            "--report", compared, "--scope", SCOPE,
        )
        verify = run_script(
            "eval/nexent/verify_tasks.py",
            "--run-id", RUN_ID, "--db", db,
            "--exported", exported, "--compare-report", compared,
            "--expected-lock", EXPECTED_LOCK,
            "--report", reconciled, "--scope", SCOPE,
            "--records-root", self.dir,
            "--manifest-lock", MANIFEST.with_name(MANIFEST.name + ".sha256"),
        )
        return {
            "export": export, "compare": compare, "verify": verify,
            "exported": read_json(exported),
            "compare_report": read_json(compared),
            "reconciliation": read_json(reconciled),
            "trace": read_json(trace),
        }

    def test_01_baseline_pipeline_passes(self):
        result = self.pipeline("baseline", self.copy_db("t01"))
        self.assertEqual(result["export"].returncode, 0, result["export"].stderr)
        self.assertEqual(result["compare"].returncode, 0,
                         result["compare"].stdout + result["compare"].stderr)
        self.assertEqual(result["verify"].returncode, 0,
                         result["verify"].stdout + result["verify"].stderr)
        self.assertEqual(result["exported"]["signal_count"], 20)
        self.assertEqual(result["compare_report"]["metrics"]["TP"], 20)
        self.assertEqual(result["compare_report"]["scope"], SCOPE)
        self.assertTrue(result["reconciliation"]["pass"])
        stats = run_script("eval/nexent/query_run.py", "--run-id", RUN_ID,
                           "--db", self.copy_db("t01_query"))
        self.assertEqual(stats.returncode, 0, stats.stderr)
        self.assertIn("任务数          : 24", stats.stdout)
        self.assertIn("持仓格          : 48", stats.stdout)
        self.assertIn("库内原始信号行  : 20", stats.stdout)

    def test_02_missing_zero_hit_task_is_caught_even_when_signals_are_correct(self):
        """最关键的漏洞防护：零命中任务没执行，最终 20 条也不能算通过。"""
        db = self.copy_db("t02")
        conn = persistence.connect(db)
        conn.execute(
            "UPDATE task_runs SET status = 'not_executed', platform_session_id = NULL,"
            " platform_request_id = NULL, platform_tool_call_id = NULL"
            " WHERE run_id = ? AND task_key = ?",
            (RUN_ID, self.zero_hit_task),
        )
        conn.commit()
        conn.close()

        result = self.pipeline("t02", db)
        # 信号本身没变，独立比对仍应 PASS —— 这正是只看报告会漏掉的情况。
        self.assertEqual(result["compare"].returncode, 0,
                         result["compare"].stdout + result["compare"].stderr)
        self.assertEqual(result["compare_report"]["metrics"]["TP"], 20)
        self.assertEqual(result["exported"]["signal_count"], 20)
        # 但任务对账必须失败。
        self.assertNotEqual(result["verify"].returncode, 0)
        self.assertFalse(result["reconciliation"]["pass"])
        failed = [c["check"] for c in result["reconciliation"]["checks"]
                 if not c["ok"]]
        self.assertIn("24 个任务状态全部 completed", failed)
        self.assertIn("零命中任务具备平台成功完成证据", failed)
        self.assertIn("无失败 / 超时 / 未执行任务", failed)

    def test_03_retry_is_idempotent_and_keeps_20_rows(self):
        db = self.copy_db("t03")
        evidence_path = self.dir / "t03_retry.json"
        result = run_script(
            "eval/nexent/verify_retry.py",
            "--run-id", RUN_ID, "--task-key", self.task_with_signal,
            "--db", db, "--records-root", self.dir,
            "--output", evidence_path,
        )
        self.assertEqual(result.returncode, 0,
                         result.stdout + result.stderr)
        evidence = read_json(evidence_path)
        self.assertTrue(evidence["pass"])
        self.assertEqual(evidence["before"]["raw_signal_row_count"], 20)
        self.assertEqual(evidence["after"]["raw_signal_row_count"], 20)
        self.assertEqual(evidence["before"]["signals_sha256"],
                         evidence["after"]["signals_sha256"])
        self.assertGreater(len(evidence["after"]["attempt_log"]),
                           len(evidence["before"]["attempt_log"]))

    def test_04_conflicting_event_id_is_rejected(self):
        db = self.copy_db("t04")
        conn = persistence.connect(db)
        original = persistence.list_signals(conn, RUN_ID)[0]
        tampered = dict(original)
        tampered["price"] = "999.00"
        with self.assertRaises(persistence.SignalConflictError):
            persistence.commit_signals(
                conn, run_id=RUN_ID, task_key=self.task_with_signal,
                signals=[tampered], attempt=2,
            )
        unchanged = persistence.list_signals(conn, RUN_ID)[0]
        self.assertEqual(unchanged["price"], original["price"])
        self.assertEqual(persistence.count_signals(conn, RUN_ID), 20)
        conn.close()

    def test_05_exporter_does_not_scrub_abnormal_rows(self):
        """导出器不得去重或过滤；异常行必须原样导出，由比对器报错。"""
        db = self.copy_db("t05")
        conn = persistence.connect(db)
        original = persistence.list_signals(conn, RUN_ID)[0]
        duplicate = dict(original)
        duplicate["event_id"] = "f" * 64  # 新 event_id，同一业务键 -> 重复信号
        conn.execute(
            "INSERT INTO final_signals (run_id, task_key, event_id, portfolio_id,"
            " timestamp, symbol, rule, price, threshold, source_attempt, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (RUN_ID, self.task_with_signal, duplicate["event_id"],
             duplicate["portfolio_id"], duplicate["timestamp"],
             duplicate["symbol"], duplicate["rule"], duplicate["price"],
             duplicate["threshold"], 2, persistence.utc_now()),
        )
        conn.commit()
        conn.close()

        exported = self.dir / "t05_exported.json"
        compared = self.dir / "t05_compare.json"
        export = run_script(
            "eval/nexent/export_final_signals.py",
            "--run-id", RUN_ID, "--db", db, "--output", exported,
            "--trace-output", self.dir / "t05_trace.json",
        )
        self.assertEqual(export.returncode, 0, export.stderr)
        self.assertEqual(read_json(exported)["signal_count"], 21)
        compare = run_script(
            "eval/compare.py", "--expected", EXPECTED, "--actual", exported,
            "--report", compared, "--scope", SCOPE,
        )
        self.assertNotEqual(compare.returncode, 0)
        report = read_json(compared)
        self.assertIsNotNone(report, compare.stdout + compare.stderr)
        self.assertFalse(report["pass"])
        self.assertEqual(report["metrics"]["FP"], 1)
        self.assertEqual(report["metrics"]["duplicates"], 1)

    def test_06_pipeline_code_never_touches_expected_annotations(self):
        """预期标注只能进最后的独立比对进程，不能进运行/写库/导出链路。"""
        strict = (
            "eval/nexent/persistence.py",
            "eval/nexent/run_nexent_eval.py",
            "eval/nexent/export_final_signals.py",
            "eval/nexent/verify_retry.py",
            "eval/nexent/nexent_client.py",
            "eval/nexent/task_manifest.py",
        )
        forbidden = ("expected_20", "replay_actual", "triggers",
                     "non_trigger_cells", "annotation_id",
                     "evaluate_snapshot", "import portfolio_risk",
                     "from portfolio_risk")
        for relative in strict:
            source = (ROOT / relative).read_text(encoding="utf-8")
            for word in forbidden:
                self.assertNotIn(word, source, f"{relative} 不应出现 {word}")
            roots = set()
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Import):
                    roots.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    roots.add(node.module.split(".")[0])
            self.assertNotIn("portfolio_risk", roots, relative)
        # verify_tasks 只读锁的哈希字符串，不读取标注内容。
        verify_source = (ROOT / "eval/nexent/verify_tasks.py").read_text(encoding="utf-8")
        for word in ("triggers", "non_trigger_cells", "annotation_id",
                     "evaluate_snapshot", "replay_actual"):
            self.assertNotIn(word, verify_source)
        self.assertIn("expected_20.json.sha256", verify_source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
