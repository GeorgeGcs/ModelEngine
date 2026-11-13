"""北向 API 客户端契约测试（本地 mock 网关，不接触真实 Nexent）。

以 v2.8.3 官方原样响应为准：
- health 精确返回 {"status":"healthy","service":"northbound-api"}；
- agents 使用官方 data 包装；
- 响应头带 X-Request-Id / conversation_id，conversation_created 事件
  的 content 可以是对象或 JSON 字符串。

工具证据的三条关键回归：
- 官方 health 响应必须通过；
- 超时 / tool_error + 空数组必须被拒绝，不得计为零命中；
- 计划组合 A、实际调用组合 B，即便返回空数组也必须被拒绝。

⚠️ 全部为本地 mock，不是 Nexent 平台证据。
"""

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent
NEXENT_DIR = HERE.parent
ROOT = NEXENT_DIR.parents[1]
sys.path.insert(0, str(NEXENT_DIR))
import nexent_client  # noqa: E402

TOKEN = "TEST-TOKEN-MUST-NOT-LEAK"
AGENT = "portfolio-risk-agent"
TOOL_NAME = nexent_client.TOOL_NAME

OFFICIAL_HEALTH = {"status": "healthy", "service": "northbound-api"}
OFFICIAL_AGENTS = {"data": [{"agent_name": AGENT}]}

PLANNED_ARGS = {
    "portfolio": {
        "id": "P-STOCK",
        "holdings": [{"symbol": "SIM-A", "stop_loss": "95.00",
                      "take_profit": "106.00"}],
    },
    "snapshot": {"timestamp": "2026-11-09T10:00:00+08:00",
                 "prices": {"SIM-A": "95.00"}},
}
ACTUAL_OTHER_ARGS = {
    "portfolio": {
        "id": "P-BOND",
        "holdings": [{"symbol": "SIM-C", "stop_loss": "1.80",
                      "take_profit": "2.10"}],
    },
    "snapshot": {"timestamp": "2026-11-09T11:00:00+08:00",
                 "prices": {"SIM-C": "1.90"}},
}

SIGNAL = {
    "event_id": "a" * 64,
    "portfolio_id": "P-STOCK",
    "timestamp": "2026-11-09T02:00:00Z",
    "symbol": "SIM-A",
    "rule": "stop_loss",
    "price": "95.00",
    "threshold": "95.00",
}


def sse(*events) -> str:
    chunks = []
    for name, payload in events:
        chunks.append(f"event: {name}\n")
        chunks.append("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n")
    chunks.append("data: [DONE]\n\n")
    return "".join(chunks)


def conversation_event():
    # 官方形态：conversation_created.content 里带 conversation_id，
    # 且 content 允许是 JSON 字符串。
    return ("conversation_created", {
        "type": "conversation_created",
        "content": json.dumps({"conversation_id": "C-1"}),
    })


def call_event(call_id="TC-1", arguments=None, **extra):
    return ("tool_call", {
        "type": "tool_call", "tool_name": TOOL_NAME,
        "tool_call_id": call_id,
        "arguments": PLANNED_ARGS if arguments is None else arguments,
        **extra,
    })


def result_event(signals, call_id="TC-1", field="structuredContent", **extra):
    payload = {"type": "tool_result", "tool_name": TOOL_NAME,
               "tool_call_id": call_id, **extra}
    if field == "structuredContent":
        payload["structuredContent"] = {"result": signals}
    else:
        payload[field] = signals
    return ("tool_result", payload)


GOOD_SSE = sse(
    conversation_event(),
    call_event(),
    result_event([SIGNAL]),
    ("final_answer", {"type": "final_answer", "result": [{"fake": 1}],
                      "content": "工具已执行"}),
)
ZERO_SSE = sse(
    conversation_event(),
    call_event(),
    result_event([]),
    ("final_answer", {"type": "final_answer", "content": "完成"}),
)
NO_TOOL_SSE = sse(
    conversation_event(),
    ("final_answer", {"type": "final_answer", "result": [SIGNAL]}),
    ("message", {"type": "message", "content": "完成", "result": []}),
)
TIMEOUT_SSE = sse(
    conversation_event(),
    call_event(),
    ("tool_result", {"type": "tool_result", "tool_name": TOOL_NAME,
                     "tool_call_id": "TC-1", "error": "timeout",
                     "tool_result": []}),
)
TOOL_ERROR_SSE = sse(
    conversation_event(),
    call_event(),
    ("tool_error", {"type": "tool_error", "tool_name": TOOL_NAME,
                    "tool_call_id": "TC-1", "tool_result": [],
                    "error": "MCP call failed"}),
)
REASONING_SSE = sse(
    conversation_event(),
    call_event(),
    ("reasoning", {"type": "reasoning", "tool_name": TOOL_NAME,
                   "tool_call_id": "TC-1", "tool_result": [],
                   "content": "我打算调用工具"}),
)
PLANNED_A_ACTUAL_B_SSE = sse(
    conversation_event(),
    call_event(arguments=ACTUAL_OTHER_ARGS),
    result_event([]),
)
MISMATCHED_CALL_ID_SSE = sse(
    conversation_event(),
    call_event(call_id="TC-1"),
    result_event([], call_id="TC-2"),
)
RESULT_ONLY_SSE = sse(
    conversation_event(),
    ("tool_result", {"type": "tool_result", "tool_name": TOOL_NAME,
                     "tool_call_id": "TC-1", "tool_result": []}),
)
# 平台只返回「工具调用 + execution_key」，SSE 里没有任何结构化工具结果。
EXECUTION_KEY_ONLY_SSE = sse(
    conversation_event(),
    call_event(call_id="TC-1", execution_key="EX-7788"),
    ("execution_started", {"type": "execution_started",
                           "execution_key": "EX-7788"}),
)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, content_type, body: str, extra=None):
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Request-Id", "XR-1")
        self.send_header("conversation_id", "C-1")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.server.captured.append({
            "method": "GET", "path": self.path,
            "authorization": self.headers.get("Authorization"),
        })
        if self.path.endswith("/health"):
            self._send(200, "application/json",
                       json.dumps(self.server.health_body))
        elif self.path.endswith("/agents"):
            self._send(200, "application/json",
                       json.dumps(self.server.agents_body))
        else:
            self._send(404, "application/json", "{}")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.captured.append({
            "method": "POST", "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "accept": self.headers.get("Accept"),
            "body": body,
        })
        self._send(200, "text/event-stream", self.server.sse_body)


class NorthboundTests(unittest.TestCase):
    def setUp(self):
        holder = TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.dir = Path(holder.name)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.captured = []
        server.health_body = dict(OFFICIAL_HEALTH)
        server.agents_body = dict(OFFICIAL_AGENTS)
        server.sse_body = GOOD_SSE
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.addCleanup(stop)
        self.server = server
        self.config = nexent_client.PlatformConfig(
            base_url=f"http://127.0.0.1:{server.server_address[1]}",
            token=TOKEN, agent_name=AGENT,
        )
        self.task = {
            "task_key": "P-STOCK|2026-11-09T10:00:00+08:00",
            "tool_arguments": PLANNED_ARGS,
        }

    def set_mcp_log(self, entries):
        path = self.dir / "mcp_calls.json"
        path.write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
            encoding="utf-8")
        self.config.mcp_call_log = str(path)

    def mcp_entry(self, **overrides):
        entry = {
            "schema": "topic16-mcp-call-log-v1",
            "source": "mcp-server-log",
            "tool_name": TOOL_NAME,
            "call_id": "TC-1",
            "conversation_id": "C-1",
            "arguments": PLANNED_ARGS,
            "result": [],
            "recorded_at": "2026-11-09T02:00:00Z",
        }
        entry.update(overrides)
        return entry

    def set_execution_records(self, entries):
        path = self.dir / "execution_records.json"
        path.write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
            encoding="utf-8")
        self.config.execution_records = str(path)

    def execution_receipt(self, **overrides):
        entry = {
            "schema": "topic16-execution-record-v1",
            "source": "mcp-server-receipt",
            "tool_name": TOOL_NAME,
            "execution_key": "EX-7788",
            "arguments": PLANNED_ARGS,
            "result": [SIGNAL],
            "tool_success": True,
            "recorded_at": "2026-11-09T02:00:00Z",
        }
        entry.update(overrides)
        return entry

    def mcp_receipt(self, **overrides):
        """MCP 服务端回执：不用 execution_key，只靠同一次工具调用标识关联。"""
        entry = {
            "schema": "topic16-mcp-call-log-v1",
            "source": "mcp-server-log",
            "tool_name": TOOL_NAME,
            "call_id": "TC-1",
            "conversation_id": "C-1",
            "arguments": PLANNED_ARGS,
            "result": [SIGNAL],
            "tool_success": True,
            "recorded_at": "2026-11-09T02:00:00Z",
        }
        entry.update(overrides)
        return entry

    # ---------- 官方契约 ----------
    def test_01_official_health_and_agents_contract_passes(self):
        """回归 1：官方原样 health 响应必须能通过，且 data 包装可枚举 Agent。"""
        result = nexent_client.probe(self.config)
        self.assertEqual(result["status"], "reachable")
        self.assertTrue(result["health"]["contract_matched"])
        self.assertTrue(result["health"]["identity_confirmed"])
        self.assertEqual(result["health"]["response_header_ids"]["x_request_id"],
                         "XR-1")
        self.assertEqual(result["agents"]["published_count"], 1)
        self.assertTrue(result["agents"]["target_found"])
        self.assertEqual(result["platform_ids"]["conversation_id"], "C-1")
        self.assertNotIn(TOKEN, json.dumps(result, ensure_ascii=False))

    def test_02_health_contract_mismatch_is_rejected(self):
        self.server.health_body = {"status": "ok"}
        result = nexent_client.probe(self.config)
        self.assertEqual(result["status"], "health-contract-mismatch")
        self.assertEqual(len([c for c in self.server.captured
                              if c["method"] == "GET"]), 1)
        self.server.health_body = dict(OFFICIAL_HEALTH)
        self.server.agents_body = {"data": [{"agent_name": "other"}]}
        result = nexent_client.probe(self.config)
        self.assertEqual(result["status"], "agent-not-published")

    def test_03_response_headers_and_conversation_created_are_captured(self):
        outcome = nexent_client.invoke_task(
            self.config, run_id="RUN-TEST", task=self.task)
        ids = outcome["platform_ids"]
        self.assertEqual(ids["x_request_id"], "XR-1")
        self.assertEqual(ids["request_id"], "XR-1")
        self.assertEqual(ids["conversation_id"], "C-1")
        self.assertEqual(ids["session_id"], "C-1")
        self.assertEqual(ids["tool_call_id"], "TC-1")
        self.assertEqual(outcome["response_header_ids"]["x_request_id"], "XR-1")
        self.assertEqual(
            outcome["raw_response"]["response_header_ids"]["conversation_id"],
            "C-1")

    def test_04_invoke_task_uses_agent_name_query_and_bearer(self):
        outcome = nexent_client.invoke_task(
            self.config, run_id="RUN-TEST", task=self.task)
        post = [c for c in self.server.captured if c["method"] == "POST"]
        self.assertEqual(len(post), 1)
        call = post[0]
        self.assertEqual(call["path"], "/nb/v1/chat/run")
        self.assertEqual(call["authorization"], f"Bearer {TOKEN}")
        self.assertEqual(call["accept"], "text/event-stream")
        self.assertEqual(set(call["body"]), {"agent_name", "query"})
        self.assertEqual(call["body"]["agent_name"], AGENT)
        self.assertIn(TOOL_NAME, call["body"]["query"])
        self.assertNotIn("run_id", call["body"])
        self.assertNotIn("task_key", call["body"])
        self.assertEqual(outcome["evidence_status"], "verified")
        self.assertEqual(outcome["signals"], [SIGNAL])
        self.assertEqual(outcome["tool_evidence"]["call_id"], "TC-1")
        self.assertEqual(outcome["tool_evidence"]["pairing"], "sse-call-id")
        self.assertEqual(outcome["tool_evidence"]["actual_tool_arguments"],
                         PLANNED_ARGS)
        self.assertEqual(outcome["tool_evidence"]["planned_tool_arguments"],
                         PLANNED_ARGS)
        self.assertEqual(outcome["evidence_gaps"], [])

    # ---------- 证据拒绝 ----------
    def test_05_final_answer_and_arbitrary_result_are_rejected(self):
        self.server.sse_body = NO_TOOL_SSE
        outcome = nexent_client.invoke_task(
            self.config, run_id="RUN-TEST", task=self.task)
        self.assertEqual(outcome["evidence_status"], "insufficient")
        self.assertIsNone(outcome["signals"])
        self.assertTrue(outcome["evidence_gaps"])
        self.assertIn("MCP 服务端调用记录",
                      " ".join(outcome["evidence_gaps"]))
        self.assertNotIn(TOKEN, json.dumps(outcome, ensure_ascii=False))

    def test_06_timeout_with_empty_array_is_rejected(self):
        """回归 2：超时 + 空数组不得被当成合法零命中。"""
        self.server.sse_body = TIMEOUT_SSE
        outcome = nexent_client.invoke_task(
            self.config, run_id="RUN-TEST", task=self.task)
        self.assertEqual(outcome["evidence_status"], "failed")
        self.assertIsNone(outcome["signals"])
        self.assertTrue(any("超时" in g or "错误" in g
                            for g in outcome["evidence_gaps"]))

    def test_07_tool_error_with_empty_array_is_rejected(self):
        self.server.sse_body = TOOL_ERROR_SSE
        outcome = nexent_client.invoke_task(
            self.config, run_id="RUN-TEST", task=self.task)
        self.assertEqual(outcome["evidence_status"], "failed")
        self.assertIsNone(outcome["signals"])

    def test_08_reasoning_event_with_tool_result_is_rejected(self):
        self.server.sse_body = REASONING_SSE
        outcome = nexent_client.invoke_task(
            self.config, run_id="RUN-TEST", task=self.task)
        self.assertEqual(outcome["evidence_status"], "insufficient")
        self.assertIsNone(outcome["signals"])

    def test_09_planned_A_actual_B_with_empty_array_is_rejected(self):
        """回归 3：计划组合 A、实际调用组合 B，即便返回 [] 也不得通过。"""
        self.server.sse_body = PLANNED_A_ACTUAL_B_SSE
        outcome = nexent_client.invoke_task(
            self.config, run_id="RUN-TEST", task=self.task)
        self.assertEqual(outcome["evidence_status"], "insufficient")
        self.assertIsNone(outcome["signals"])
        joined = " ".join(outcome["evidence_gaps"])
        self.assertIn("实际入参与冻结清单的计划入参不一致", joined)
        self.assertIn("P-BOND", joined)

    def test_10_mismatched_call_id_is_rejected(self):
        self.server.sse_body = MISMATCHED_CALL_ID_SSE
        outcome = nexent_client.invoke_task(
            self.config, run_id="RUN-TEST", task=self.task)
        self.assertEqual(outcome["evidence_status"], "insufficient")
        self.assertIsNone(outcome["signals"])

    # ---------- 零命中与 MCP 补证 ----------
    def test_11_zero_hit_requires_successful_paired_tool_call(self):
        self.server.sse_body = ZERO_SSE
        outcome = nexent_client.invoke_task(
            self.config, run_id="RUN-TEST", task=self.task)
        self.assertEqual(outcome["evidence_status"], "verified")
        self.assertEqual(outcome["signals"], [])
        self.assertTrue(outcome["tool_evidence"]["zero_hit"])
        self.assertTrue(outcome["tool_evidence"]["tool_success"])
        self.assertEqual(outcome["tool_evidence"]["actual_tool_arguments"],
                         PLANNED_ARGS)

    def test_12_mcp_server_log_can_supply_actual_arguments(self):
        self.server.sse_body = RESULT_ONLY_SSE
        outcome = nexent_client.invoke_task(
            self.config, run_id="RUN-TEST", task=self.task)
        self.assertEqual(outcome["evidence_status"], "insufficient")
        self.set_mcp_log([self.mcp_entry()])
        outcome = nexent_client.invoke_task(
            self.config, run_id="RUN-TEST", task=self.task)
        self.assertEqual(outcome["evidence_status"], "verified")
        self.assertEqual(outcome["signals"], [])
        self.assertEqual(
            outcome["tool_evidence"]["actual_arguments_source"],
            "mcp-server-log")
        self.assertEqual(outcome["tool_evidence"]["pairing"],
                         "mcp-server-log")

    def test_13_mcp_log_without_correlation_is_rejected(self):
        self.server.sse_body = RESULT_ONLY_SSE
        # 调用标识与会话标识都对不上本次平台请求，不能靠「结果相同」补证。
        self.set_mcp_log([self.mcp_entry(
            call_id="TC-OTHER", conversation_id="SOME-OTHER-SESSION")])
        outcome = nexent_client.invoke_task(
            self.config, run_id="RUN-TEST", task=self.task)
        self.assertEqual(outcome["evidence_status"], "insufficient")
        self.assertIsNone(outcome["signals"])

    def test_14_mcp_log_with_wrong_result_is_rejected(self):
        self.server.sse_body = RESULT_ONLY_SSE
        self.set_mcp_log([self.mcp_entry(result=[SIGNAL])])
        outcome = nexent_client.invoke_task(
            self.config, run_id="RUN-TEST", task=self.task)
        self.assertEqual(outcome["evidence_status"], "insufficient")

    def test_15_preflight_checks_planned_actual_and_pairing(self):
        """驱动的 smoke 预检必须验证 v3 记录的实际入参与同次调用配对。"""
        import hashlib
        import run_nexent_eval as driver
        import persistence

        db = self.dir / "preflight.db"
        conn = persistence.connect(db)
        persistence.init_db(conn)
        persistence.start_run(
            conn, run_id="RUN-TEST", platform_label="local-mock",
            platform_version="2.8.3", agent_id=AGENT,
            source_input_sha256="input-hash",
            task_manifest_sha256="manifest-hash",
        )

        def build(index, key, signals):
            record = {
                "schema": "topic16-platform-tool-record-v3",
                "run_id": "RUN-TEST", "task_key": key, "task_index": index,
                "http_status": 200, "call_success": True,
                "evidence_status": "verified",
                "tool_name": TOOL_NAME,
                "tool_arguments": PLANNED_ARGS,
                "planned_tool_arguments": PLANNED_ARGS,
                "actual_tool_arguments": PLANNED_ARGS,
                "actual_arguments_source": "sse-tool-call",
                "tool_call_pair": {"call_id": f"TC-{index}",
                                   "pairing": "sse-call-id"},
                "tool_success": True, "zero_hit": not signals,
                "platform_ids": {"conversation_id": "C-1",
                                 "x_request_id": "XR-1"},
                "response_header_ids": {"x_request_id": "XR-1"},
                "raw_response": {}, "tool_result": signals,
                "tool_evidence": {"tool_success": True},
                "source": "nexent-northbound", "platform_evidence": True,
            }
            path = self.dir / f"preflight_{index}.json"
            raw = (json.dumps(record, ensure_ascii=False, indent=2)
                   + "\n").encode("utf-8")
            path.write_bytes(raw)
            persistence.commit_signals(
                conn, run_id="RUN-TEST", task_key=key, signals=signals,
                attempt=1, task={
                    "task_index": index, "portfolio_id": "P-STOCK",
                    "snapshot_timestamp": "t", "input_symbols": ["SIM-A"],
                    "cell_count": 1, "platform_session_id": "C-1",
                    "platform_request_id": "XR-1",
                    "platform_tool_call_id": f"TC-{index}",
                    "platform_trace_note": "", "tool_response_ref": path.name,
                    "tool_response_sha256": hashlib.sha256(raw).hexdigest(),
                    "returned_signal_count": len(signals),
                    "status": "completed",
                },
            )
            return {
                "task_key": key, "record_ref": str(path),
                "record_sha256": hashlib.sha256(raw).hexdigest(),
                "signal_count": len(signals),
                "planned_tool_arguments": PLANNED_ARGS,
                "actual_tool_arguments": PLANNED_ARGS,
            }, path, record

        hit, hit_path, _ = build(1, "P-STOCK|t1", [SIGNAL])
        zero, zero_path, zero_record = build(2, "P-STOCK|t2", [])
        conn.close()
        result = driver.check_traceability(db, "RUN-TEST", [hit, zero])
        self.assertTrue(result["pass"], result["records"])
        row = next(r for r in result["records"] if r["task_key"] == zero["task_key"])
        self.assertTrue(row["actual_args_match_planned"])
        self.assertTrue(row["zero_hit_evidence_ok"])
        self.assertEqual(row["pair_call_id"], "TC-2")

        # 把零命中记录的实际入参改成另一个组合（同步更新哈希，排除哈希差异干扰）。
        zero_record["actual_tool_arguments"] = ACTUAL_OTHER_ARGS
        raw2 = (json.dumps(zero_record, ensure_ascii=False, indent=2)
                + "\n").encode("utf-8")
        zero_path.write_bytes(raw2)
        digest2 = hashlib.sha256(raw2).hexdigest()
        zero["record_sha256"] = digest2
        conn2 = persistence.connect(db)
        conn2.execute(
            "UPDATE task_runs SET tool_response_sha256 = ?"
            " WHERE run_id = 'RUN-TEST' AND task_key = ?",
            (digest2, zero["task_key"]),
        )
        conn2.close()
        result2 = driver.check_traceability(db, "RUN-TEST", [hit, zero])
        self.assertFalse(result2["pass"])
        row2 = next(r for r in result2["records"]
                    if r["task_key"] == zero["task_key"])
        self.assertTrue(row2["hash_matches_task_row"])
        self.assertFalse(row2["actual_args_match_planned"])

    def test_17_full_mode_gate_requires_smoke_preflight(self):
        """暂缓全量：没有同一 run_id 的两条预检记录时 --mode full 必须被拒绝。"""
        import run_nexent_eval as driver
        path = self.dir / "preflight.json"
        ok, _ = driver.smoke_preflight_gate(path, "RUN-1")
        self.assertFalse(ok)

        def write(**overrides):
            doc = {"schema": "topic16-northbound-preflight-v2",
                   "run_id": "RUN-1", "scope": "northbound-preflight",
                   "platform_evidence": True, "pass": True,
                   "hit_task_found": True, "zero_hit_task_found": True}
            doc.update(overrides)
            path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

        write()
        self.assertTrue(driver.smoke_preflight_gate(path, "RUN-1")[0])
        self.assertFalse(driver.smoke_preflight_gate(path, "RUN-OTHER")[0])
        write(hit_task_found=False)
        self.assertFalse(driver.smoke_preflight_gate(path, "RUN-1")[0])
        write(**{"pass": False})
        self.assertFalse(driver.smoke_preflight_gate(path, "RUN-1")[0])
        write(platform_evidence=False)
        self.assertFalse(driver.smoke_preflight_gate(path, "RUN-1")[0])

    # ---------- execution_key 补证（用户验证的联调边界） ----------
    def test_18_execution_key_only_without_records_is_insufficient(self):
        self.server.sse_body = EXECUTION_KEY_ONLY_SSE
        outcome = nexent_client.invoke_task(
            self.config, run_id="RUN-TEST", task=self.task)
        self.assertEqual(outcome["evidence_status"], "insufficient")
        self.assertIsNone(outcome["signals"])
        self.assertEqual(outcome["execution_keys"], ["EX-7788"])
        self.assertIn("execution_key", " ".join(outcome["evidence_gaps"]))

    def test_19_unrelated_mcp_log_does_not_rescue_execution_key(self):
        """MCP 日志里有结果但对不上执行键时，仍按指令保留 insufficient。"""
        self.server.sse_body = EXECUTION_KEY_ONLY_SSE
        self.set_mcp_log([self.mcp_entry(
            execution_key="EX-OTHER", result=[SIGNAL],
            conversation_id="C-1", call_id="TC-OTHER")])
        outcome = nexent_client.invoke_task(
            self.config, run_id="RUN-TEST", task=self.task)
        self.assertEqual(outcome["evidence_status"], "insufficient")
        self.assertIsNone(outcome["signals"])

    def test_20_unique_execution_receipt_hit_is_verified(self):
        self.server.sse_body = EXECUTION_KEY_ONLY_SSE
        self.set_execution_records([self.execution_receipt()])
        outcome = nexent_client.invoke_task(
            self.config, run_id="RUN-TEST", task=self.task)
        self.assertEqual(outcome["evidence_status"], "verified")
        self.assertEqual(outcome["signals"], [SIGNAL])
        self.assertEqual(outcome["tool_evidence"]["pairing"], "execution-key")
        self.assertEqual(outcome["tool_evidence"]["execution_key"], "EX-7788")
        self.assertEqual(outcome["tool_evidence"]["actual_tool_arguments"],
                         PLANNED_ARGS)

    def test_21_unique_execution_receipt_zero_hit_is_verified(self):
        self.server.sse_body = EXECUTION_KEY_ONLY_SSE
        self.set_execution_records([self.execution_receipt(result=[])])
        outcome = nexent_client.invoke_task(
            self.config, run_id="RUN-TEST", task=self.task)
        self.assertEqual(outcome["evidence_status"], "verified")
        self.assertEqual(outcome["signals"], [])
        self.assertTrue(outcome["tool_evidence"]["zero_hit"])

    def test_22_ambiguous_execution_records_are_rejected(self):
        self.server.sse_body = EXECUTION_KEY_ONLY_SSE
        self.set_execution_records([
            self.execution_receipt(),
            self.execution_receipt(recorded_at="2026-11-09T02:00:01Z"),
        ])
        outcome = nexent_client.invoke_task(
            self.config, run_id="RUN-TEST", task=self.task)
        self.assertEqual(outcome["evidence_status"], "insufficient")
        self.assertIn("无法唯一关联", " ".join(outcome["evidence_gaps"]))

    def test_23_execution_record_without_success_marker_is_rejected(self):
        self.server.sse_body = EXECUTION_KEY_ONLY_SSE
        self.set_execution_records([self.execution_receipt(tool_success=None)])
        outcome = nexent_client.invoke_task(
            self.config, run_id="RUN-TEST", task=self.task)
        self.assertEqual(outcome["evidence_status"], "insufficient")
        self.assertIsNone(outcome["signals"])

    def test_24_execution_record_with_error_marker_is_rejected(self):
        self.server.sse_body = EXECUTION_KEY_ONLY_SSE
        self.set_execution_records([self.execution_receipt(timeout="MCP timeout")])
        outcome = nexent_client.invoke_task(
            self.config, run_id="RUN-TEST", task=self.task)
        self.assertEqual(outcome["evidence_status"], "insufficient")

    def test_25_execution_record_wrong_arguments_is_rejected(self):
        self.server.sse_body = EXECUTION_KEY_ONLY_SSE
        self.set_execution_records([
            self.execution_receipt(arguments=ACTUAL_OTHER_ARGS)])
        outcome = nexent_client.invoke_task(
            self.config, run_id="RUN-TEST", task=self.task)
        self.assertEqual(outcome["evidence_status"], "insufficient")
        self.assertIn("P-BOND", " ".join(outcome["evidence_gaps"]))

    def test_26_execution_record_from_fixture_source_is_rejected(self):
        self.server.sse_body = EXECUTION_KEY_ONLY_SSE
        self.set_execution_records([
            self.execution_receipt(source="local-fixture")])
        outcome = nexent_client.invoke_task(
            self.config, run_id="RUN-TEST", task=self.task)
        self.assertEqual(outcome["evidence_status"], "insufficient")

    def test_27_mcp_receipt_linked_by_tool_call_id_is_verified(self):
        """复核指令：可唯一关联到该次调用的 MCP 服务端回执也可补证。"""
        self.server.sse_body = EXECUTION_KEY_ONLY_SSE
        self.set_execution_records([self.mcp_receipt()])
        outcome = nexent_client.invoke_task(
            self.config, run_id="RUN-TEST", task=self.task)
        self.assertEqual(outcome["evidence_status"], "verified")
        self.assertEqual(outcome["signals"], [SIGNAL])
        self.assertEqual(outcome["tool_evidence"]["pairing"], "tool-call-id")
        self.assertIsNone(outcome["tool_evidence"]["execution_key"])
        self.assertEqual(outcome["tool_call_ids"], ["TC-1"])

    def test_28_mcp_receipt_with_unrelated_call_id_is_rejected(self):
        self.server.sse_body = EXECUTION_KEY_ONLY_SSE
        self.set_execution_records([self.mcp_receipt(call_id="TC-OTHER")])
        outcome = nexent_client.invoke_task(
            self.config, run_id="RUN-TEST", task=self.task)
        self.assertEqual(outcome["evidence_status"], "insufficient")
        self.assertIsNone(outcome["signals"])

    def test_29_mcp_receipt_without_success_marker_is_rejected(self):
        self.server.sse_body = EXECUTION_KEY_ONLY_SSE
        self.set_execution_records([self.mcp_receipt(tool_success=None)])
        outcome = nexent_client.invoke_task(
            self.config, run_id="RUN-TEST", task=self.task)
        self.assertEqual(outcome["evidence_status"], "insufficient")

    def test_30_ambiguous_mcp_receipts_are_rejected(self):
        self.server.sse_body = EXECUTION_KEY_ONLY_SSE
        self.set_execution_records([
            self.mcp_receipt(),
            self.mcp_receipt(recorded_at="2026-11-09T02:00:01Z"),
        ])
        outcome = nexent_client.invoke_task(
            self.config, run_id="RUN-TEST", task=self.task)
        self.assertEqual(outcome["evidence_status"], "insufficient")
        self.assertIn("无法唯一关联", " ".join(outcome["evidence_gaps"]))

    def test_31_mcp_receipt_with_timeout_marker_is_rejected(self):
        self.server.sse_body = EXECUTION_KEY_ONLY_SSE
        self.set_execution_records([self.mcp_receipt(timeout="MCP timeout")])
        outcome = nexent_client.invoke_task(
            self.config, run_id="RUN-TEST", task=self.task)
        self.assertEqual(outcome["evidence_status"], "insufficient")

    def test_32_execution_key_association_takes_precedence(self):
        """同时带两种标识时，仍以 SSE execution_key 配对，不混入调用标识。"""
        self.server.sse_body = EXECUTION_KEY_ONLY_SSE
        self.set_execution_records([
            self.execution_receipt(call_id="TC-1")])
        outcome = nexent_client.invoke_task(
            self.config, run_id="RUN-TEST", task=self.task)
        self.assertEqual(outcome["evidence_status"], "verified")
        self.assertEqual(outcome["tool_evidence"]["pairing"], "execution-key")
        self.assertEqual(outcome["tool_evidence"]["execution_key"], "EX-7788")

    def test_33_full_mode_main_returns_4_without_preflight(self):
        """实跑入口级门禁：即使平台可达，没有预检也绝不开始 24 任务。"""
        import run_nexent_eval as driver
        from unittest import mock
        status = {"status": "reachable", "checks": [], "health": {}}
        status_path = self.dir / "status.json"
        preflight_path = self.dir / "missing_preflight.json"
        argv = ["run_nexent_eval.py", "--mode", "full", "--run-id", "RUN-1",
                "--status-output", str(status_path),
                "--preflight-output", str(preflight_path)]
        with mock.patch.object(driver, "probe", return_value=status), \
                mock.patch.object(sys, "argv", argv):
            code = driver.main()
        self.assertEqual(code, 4)
        self.assertFalse(preflight_path.exists())
        self.assertTrue(status_path.is_file())

    def test_16_credentials_are_redacted(self):
        redacted = nexent_client._redact({
            "api_token": TOKEN, "authorization": "Bearer " + TOKEN,
            "nested": {"api_key": TOKEN, "signals": [SIGNAL]},
        })
        self.assertEqual(redacted["api_token"], "<redacted>")
        self.assertEqual(redacted["authorization"], "<redacted>")
        self.assertEqual(redacted["nested"]["api_key"], "<redacted>")
        self.assertEqual(redacted["nested"]["signals"], [SIGNAL])
        self.assertNotIn(TOKEN, json.dumps(redacted, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
