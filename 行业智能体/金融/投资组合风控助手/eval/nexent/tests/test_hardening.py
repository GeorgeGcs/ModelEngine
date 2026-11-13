"""加固回归测试：把复核发现的两个缺陷与三项新要求固化为反向测试。

⚠️ 全部使用 LOCAL FIXTURE，不是 Nexent 平台证据；不生成 end-to-end 通过报告。

覆盖：
- 只改库中一条价格、保留原导出与原报告，对账必须 FAIL；
- 移走某个零命中任务的原始载荷，对账必须 FAIL；
- 改任务清单但保留旧锁，实跑入口与对账都必须 FAIL；
- 本地替身数据不得以 end-to-end 身份通过；
- 冲突时整批回滚（含 completed 任务行），失败单独留证；
- 相同内容重投成功且不增加信号行。
"""

import hashlib
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent
NEXENT_DIR = HERE.parent
ROOT = NEXENT_DIR.parents[1]
sys.path.insert(0, str(NEXENT_DIR))
import persistence  # noqa: E402
import task_manifest  # noqa: E402
import verify_tasks  # noqa: E402

RUN_ID = "LOCAL-FIXTURE-RUN-0001"
E2E_RUN_ID = "RUN-REAL-0001"
MANIFEST = NEXENT_DIR / "task_manifest.json"
MANIFEST_LOCK = NEXENT_DIR / "task_manifest.json.sha256"
FIXTURE = HERE / "fixtures" / "platform_responses.json"
EXPECTED = ROOT / "eval" / "expected_20.json"
EXPECTED_LOCK = ROOT / "eval" / "expected_20.json.sha256"
TOOL_NAME = "evaluate_portfolio_snapshot"


def read_json(path: Path):
    return json.loads(path.read_bytes().decode("utf-8-sig")) if path.exists() else None


def run_script(relative: str, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / relative), *[str(a) for a in args]],
        cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


class HardeningTests(unittest.TestCase):
    def setUp(self):
        holder = TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.dir = Path(holder.name)
        self._env = None

    # ---------- 构造一份完整的「导出 + 比对报告 + 对账报告」环境 ----------
    def env(self) -> dict:
        if self._env is not None:
            return self._env
        fixture = read_json(FIXTURE)
        manifest = read_json(MANIFEST)
        by_key = {t["task_key"]: t for t in manifest["tasks"]}
        db = self.dir / "env.db"
        records = self.dir / "records"
        records.mkdir(parents=True, exist_ok=True)

        conn = persistence.connect(db)
        persistence.init_db(conn)
        persistence.start_run(
            conn, run_id=RUN_ID, platform_label="LOCAL-FIXTURE",
            platform_version=None, agent_id=None,
            source_input_sha256=manifest["source_sha256"],
            task_manifest_sha256="fixture-manifest",
            note="LOCAL FIXTURE — 非平台证据，仅用于机制自检",
        )
        for record in fixture["tasks"]:
            task = by_key[record["task_key"]]
            rel = f"records/task_{record['task_index']:02d}.json"
            path = self.dir / rel
            raw = (json.dumps({
                "schema": "topic16-platform-tool-record-v2",
                "run_id": RUN_ID, "task_key": record["task_key"],
                "task_index": record["task_index"],
                "http_status": 200,
                "call_success": True,
                "tool_name": TOOL_NAME,
                "tool_arguments": task["tool_arguments"],
                "platform_ids": record["platform_ids"],
                "raw_response": record["raw_response"],
                "tool_result": record["tool_result"],
                "source": "local-fixture",
                "platform_evidence": False,
                "notice": "LOCAL FIXTURE — 非真实 Nexent 平台记录，不得作为验收证据。",
            }, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            path.write_bytes(raw)
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
                "platform_trace_note": f"LOCAL FIXTURE run_id={RUN_ID}",
                "tool_response_ref": rel,
                "tool_response_sha256": hashlib.sha256(raw).hexdigest(),
                "returned_signal_count": len(record["tool_result"]),
                "status": "completed",
                "finished_at": persistence.utc_now(),
            }
            persistence.commit_signals(
                conn, run_id=RUN_ID, task_key=record["task_key"],
                signals=record["tool_result"], attempt=1, task=task_row,
            )
        persistence.finish_run(conn, RUN_ID, "completed")
        conn.close()

        exported = self.dir / "exported.json"
        compared = self.dir / "compared.json"
        export = run_script("eval/nexent/export_final_signals.py",
                            "--run-id", RUN_ID, "--db", db,
                            "--output", exported,
                            "--trace-output", self.dir / "trace.json")
        self.assertEqual(export.returncode, 0, export.stderr)
        compare = run_script("eval/compare.py", "--expected", EXPECTED,
                             "--actual", exported, "--report", compared,
                             "--scope", "local-fixture")
        self.assertEqual(compare.returncode, 0, compare.stdout + compare.stderr)
        self._env = {
            "db": db, "records": records, "exported": exported,
            "compared": compared, "manifest": read_json(MANIFEST),
            "zero_hit_key": next(r["task_key"] for r in fixture["tasks"]
                                 if not r["tool_result"]),
        }
        return self._env

    def verify(self, *, db=None, manifest=None, manifest_lock=None,
               scope="local-fixture", tag="v") -> dict:
        env = self.env()
        report = self.dir / f"{tag}_recon.json"
        completed = run_script(
            "eval/nexent/verify_tasks.py",
            "--run-id", RUN_ID,
            "--db", db or env["db"],
            "--manifest", manifest or MANIFEST,
            "--manifest-lock", manifest_lock or MANIFEST_LOCK,
            "--records-root", self.dir,
            "--exported", env["exported"],
            "--compare-report", env["compared"],
            "--expected-lock", EXPECTED_LOCK,
            "--report", report, "--scope", scope,
        )
        data = read_json(report)
        return {
            "returncode": completed.returncode,
            "report": data,
            "failed": [c["check"] for c in (data or {}).get("checks", [])
                       if not c["ok"]],
            "stdout": completed.stdout + completed.stderr,
        }

    # ---------- 对账三项反例 ----------
    def test_01_price_change_in_database_is_caught(self):
        """保留原导出与原报告，只改库中一条价格 -> 必须 FAIL。"""
        baseline = self.verify(tag="h01_base")
        self.assertEqual(baseline["returncode"], 0, baseline["stdout"])
        db = self.dir / "h01_tampered.db"
        shutil.copyfile(self.env()["db"], db)
        conn = persistence.connect(db)
        conn.execute(
            "UPDATE final_signals SET price='123456.78' WHERE event_id ="
            " (SELECT event_id FROM final_signals WHERE run_id=? LIMIT 1)",
            (RUN_ID,),
        )
        conn.close()
        result = self.verify(db=db, tag="h01_tampered")
        self.assertNotEqual(result["returncode"], 0)
        self.assertIn("数据库原始行与导出信号逐行七字段一致", result["failed"])
        self.assertIn("24 个任务的原始载荷文件均通过验真", result["failed"])

    def test_02_missing_zero_hit_payload_is_caught(self):
        """移走零命中任务的载荷文件 -> 必须 FAIL。"""
        baseline = self.verify(tag="h02_base")
        self.assertEqual(baseline["returncode"], 0, baseline["stdout"])
        env = self.env()
        key = env["zero_hit_key"]
        conn = persistence.connect(env["db"])
        row = conn.execute(
            "SELECT tool_response_ref FROM task_runs WHERE run_id=? AND task_key=?",
            (RUN_ID, key),
        ).fetchone()
        conn.close()
        target = self.dir / row["tool_response_ref"]
        self.assertTrue(target.exists())
        target.unlink()
        result = self.verify(tag="h02_removed")
        self.assertNotEqual(result["returncode"], 0)
        self.assertIn("24 个任务的原始载荷文件均通过验真", result["failed"])
        self.assertIn("零命中任务具备平台成功完成证据", result["failed"])

    def test_03_manifest_changed_with_old_lock_is_caught(self):
        """改清单但保留旧锁：实跑入口校验与对账都必须 FAIL。"""
        original = MANIFEST.read_bytes()
        changed = json.loads(original.decode("utf-8-sig"))
        changed["note"] = "TAMPERED — 保留旧锁，必须被拒绝"
        tampered = self.dir / "task_manifest_tampered.json"
        tampered.write_bytes(
            (json.dumps(changed, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        )
        stale_lock = self.dir / "task_manifest_tampered.json.sha256"
        stale_lock.write_bytes(MANIFEST_LOCK.read_bytes())
        with self.assertRaises(ValueError):
            task_manifest.verify_manifest_lock(tampered, stale_lock)
        result = self.verify(manifest=tampered, manifest_lock=stale_lock,
                             tag="h03_stale_lock")
        self.assertNotEqual(result["returncode"], 0)
        self.assertIn("清单 SHA256 与冻结锁一致", result["failed"])

    def test_04_fixture_cannot_pass_as_end_to_end(self):
        """本地替身不得以 end-to-end 身份通过。"""
        result = self.verify(scope="end-to-end", tag="h04_fake_e2e")
        self.assertNotEqual(result["returncode"], 0)
        self.assertIn("scope=end-to-end 时数据来自真实平台（禁止 fixture）",
                      result["failed"])

    # ---------- 端到端载荷：实际入参与同次调用配对 ----------
    def e2e_probe(self, **overrides):
        """构造一份零命中的 v3 真实平台载荷，直接跑逐载荷验真。"""
        manifest = self.env()["manifest"]
        task = manifest["tasks"][0]
        key = task["task_key"]
        record = {
            "schema": "topic16-platform-tool-record-v3",
            "run_id": E2E_RUN_ID,
            "task_key": key,
            "task_index": task["task_index"],
            "http_status": 200,
            "call_success": True,
            "evidence_status": "verified",
            "tool_name": TOOL_NAME,
            "tool_arguments": task["tool_arguments"],
            "planned_tool_arguments": task["tool_arguments"],
            "actual_tool_arguments": task["tool_arguments"],
            "actual_arguments_source": "sse-tool-call",
            "tool_call_pair": {
                "call_id": "TC-1", "pairing": "sse-call-id",
                "call_event_index": 1, "result_event_index": 2,
            },
            "tool_success": True,
            "zero_hit": True,
            "platform_ids": {
                "conversation_id": "C-1", "x_request_id": "XR-1",
                "session_id": "C-1", "request_id": "XR-1",
                "tool_call_id": "TC-1",
            },
            "response_header_ids": {"x_request_id": "XR-1"},
            "raw_response": {},
            "tool_result": [],
            "tool_evidence": {"tool_success": True},
            "source": "nexent-northbound",
            "platform_evidence": True,
        }
        record.update(overrides)
        path = self.dir / "e2e_record.json"
        raw = (json.dumps(record, ensure_ascii=False, indent=2)
               + "\n").encode("utf-8")
        path.write_bytes(raw)
        db_task = {
            "task_key": key,
            "tool_response_ref": path.name,
            "tool_response_sha256": hashlib.sha256(raw).hexdigest(),
            "returned_signal_count": len(record["tool_result"]),
            "status": "completed",
            "platform_session_id": "C-1",
            "platform_request_id": "XR-1",
            "platform_tool_call_id": "TC-1",
        }
        return verify_tasks.verify_record(
            db_task, run_id=E2E_RUN_ID, records_root=self.dir,
            manifest_by_key={key: task}, scope="end-to-end",
            db_rows_by_task={key: []},
        )

    def test_08_valid_end_to_end_record_passes_record_verification(self):
        self.assertEqual(self.e2e_probe(), [])

    def test_09_end_to_end_rejects_old_schema_and_missing_pairing(self):
        problems = " ".join(self.e2e_probe(
            schema="topic16-platform-tool-record-v2"))
        self.assertIn("schema", problems)
        problems = " ".join(self.e2e_probe(tool_call_pair={}))
        self.assertIn("配对证据", problems)

    def test_10_end_to_end_rejects_wrong_actual_arguments(self):
        other = {"portfolio": {"id": "P-BOND"}, "snapshot": {"prices": {}}}
        problems = " ".join(self.e2e_probe(actual_tool_arguments=other))
        self.assertIn("实际工具入参", problems)

    def test_11_end_to_end_rejects_zero_hit_without_tool_success(self):
        problems = " ".join(self.e2e_probe(tool_success=False))
        self.assertIn("零命中必须由工具成功", problems)
        self.assertIn("已识别成功调用", problems)

    def test_12_end_to_end_accepts_execution_key_pairing(self):
        """SSE 只给 execution_key 时，v3 载荷须记录该键且配对方式可核实。"""
        problems = self.e2e_probe(
            actual_arguments_source="execution-record",
            execution_keys=["EX-7788"],
            tool_call_pair={"call_id": "EX-7788", "execution_key": "EX-7788",
                            "pairing": "execution-key"},
        )
        self.assertEqual(problems, [])

    def test_13_end_to_end_rejects_execution_key_not_from_sse(self):
        problems = " ".join(self.e2e_probe(
            actual_arguments_source="execution-record",
            execution_keys=[],
            tool_call_pair={"call_id": "EX-7788", "execution_key": "EX-7788",
                            "pairing": "execution-key"},
        ))
        self.assertIn("execution_key 补证键必须来自本次 SSE", problems)

    def test_14_end_to_end_accepts_tool_call_id_pairing(self):
        """MCP 服务端回执按同一次调用标识关联时，对账须认可且不得凭空造执行键。"""
        problems = self.e2e_probe(
            actual_arguments_source="execution-record",
            execution_keys=["EX-7788"],
            tool_call_ids=["TC-1"],
            tool_call_pair={"call_id": "TC-1", "execution_key": None,
                            "pairing": "tool-call-id"},
        )
        self.assertEqual(problems, [])

    def test_15_end_to_end_rejects_tool_call_id_not_from_sse(self):
        problems = " ".join(self.e2e_probe(
            actual_arguments_source="execution-record",
            tool_call_ids=[],
            tool_call_pair={"call_id": "TC-1", "execution_key": None,
                            "pairing": "tool-call-id"},
        ))
        self.assertIn("工具调用标识补证键必须来自本次 SSE", problems)

    def test_16_end_to_end_rejects_fabricated_key_on_call_id_pairing(self):
        problems = " ".join(self.e2e_probe(
            actual_arguments_source="execution-record",
            tool_call_ids=["TC-1"],
            tool_call_pair={"call_id": "TC-1", "execution_key": "EX-9999",
                            "pairing": "tool-call-id"},
        ))
        self.assertIn("工具调用标识补证键必须来自本次 SSE", problems)

    # ---------- 事务两项反例 ----------
    @staticmethod
    def signal(event_id: str, price: str) -> dict:
        return {
            "event_id": event_id, "portfolio_id": "P",
            "timestamp": "2026-11-09T02:00:00Z", "symbol": "S",
            "rule": "stop_loss", "price": price, "threshold": "95.00",
        }

    def make_atomic_db(self, tag: str, with_first_row: bool):
        db = self.dir / f"{tag}.db"
        conn = persistence.connect(db)
        persistence.init_db(conn)
        persistence.start_run(
            conn, run_id="R", platform_label="X", platform_version=None,
            agent_id=None, source_input_sha256="a", task_manifest_sha256="b",
        )
        self.addCleanup(conn.close)
        first = self.signal("a" * 64, "95.00")
        if with_first_row:
            persistence.record_task(conn, {
                "run_id": "R", "task_key": "T", "task_index": 1,
                "portfolio_id": "P", "snapshot_timestamp": "t",
                "input_symbols": ["S"], "cell_count": 1,
                "tool_response_ref": "x.json", "tool_response_sha256": "h",
                "returned_signal_count": 1, "status": "completed",
            })
            persistence.commit_signals(conn, run_id="R", task_key="T",
                                       signals=[first])
        return conn, first

    def test_05_conflict_rolls_back_entire_batch(self):
        """1 行 -> 提交「新事件 + 同 ID 不同价格」-> 抛错后仍是 1 行。"""
        conn, first = self.make_atomic_db("h05", with_first_row=True)
        self.assertEqual(persistence.count_signals(conn, "R"), 1)
        batch = [self.signal("b" * 64, "94.00"), self.signal("a" * 64, "999.00")]
        with self.assertRaises(persistence.SignalConflictError):
            persistence.commit_signals(conn, run_id="R", task_key="T",
                                       signals=batch, attempt=2)
        self.assertEqual(persistence.count_signals(conn, "R"), 1)
        self.assertEqual(persistence.list_signals(conn, "R")[0]["price"], "95.00")
        self.assertEqual(len(persistence.list_failures(conn, "R")), 1)
        conn.close()

    def test_06_idempotent_replay_succeeds_without_new_rows(self):
        conn, first = self.make_atomic_db("h06", with_first_row=True)
        outcomes = persistence.commit_signals(
            conn, run_id="R", task_key="T", signals=[first], attempt=2)
        self.assertEqual(outcomes,
                         {"inserted": 0, "idempotent_hit": 1, "conflict": 0})
        self.assertEqual(persistence.count_signals(conn, "R"), 1)
        conn.close()

    def test_07_completed_status_rolls_back_with_signals(self):
        """新任务提交「新事件 + 与已存在行冲突的事件」并带 completed 任务行。

        冲突必须让该任务的 completed 行、新插入的信号、尝试日志一起消失，
        只保留冲突前已经提交的那一行。
        """
        conn, first = self.make_atomic_db("h07", with_first_row=True)
        self.assertEqual(persistence.count_signals(conn, "R"), 1)
        task_row = {
            "task_index": 2, "portfolio_id": "P", "snapshot_timestamp": "t2",
            "input_symbols": ["S"], "cell_count": 1,
            "platform_session_id": "S2", "platform_request_id": "Q2",
            "platform_tool_call_id": "C2", "platform_trace_note": "",
            "tool_response_ref": "y.json", "tool_response_sha256": "h2",
            "returned_signal_count": 2, "status": "completed",
        }
        with self.assertRaises(persistence.SignalConflictError):
            persistence.commit_signals(
                conn, run_id="R", task_key="T2",
                signals=[self.signal("b" * 64, "94.00"),
                         self.signal("a" * 64, "999.00")],
                attempt=1, task=task_row,
            )
        self.assertEqual(persistence.count_signals(conn, "R"), 1)
        self.assertEqual(persistence.list_signals(conn, "R")[0]["price"], "95.00")
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE run_id='R' AND task_key='T2'"
        ).fetchone()[0], 0)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM attempt_log WHERE run_id='R' AND task_key='T2'"
        ).fetchone()[0], 0)
        self.assertEqual(len(persistence.list_failures(conn, "R")), 1)
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
