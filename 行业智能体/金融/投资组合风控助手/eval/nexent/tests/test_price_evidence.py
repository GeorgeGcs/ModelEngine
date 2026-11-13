"""价格工具证据：四个反例必须失败、正例必须成功的显式合成夹具回归。

对应服务器侧复核指令：
  ① 只有调用没有可信返回；
  ② execution_key 只来自模型正文；
  ③ 同键成功与失败回执冲突；
  ④ 回执明确属于其他 run。
四例均不得算成功。同时锁住正例：
  同一次可信调用 + 实际入参匹配 + 显式成功 + 返回 [] → 成功零命中；
  同一次调用的正常非空结果也必须照常通过。

全部为本地合成 SSE / 本地合成回执，不是任何真实 Nexent 平台证据。
运行：python -m unittest discover -s eval/nexent/tests -p "test_price_evidence.py" -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

HERE = Path(__file__).resolve().parent
NEXENT_DIR = HERE.parent
sys.path.insert(0, str(NEXENT_DIR))
import nexent_client as nc  # noqa: E402

TOOL = nc.TOOL_NAME
PLANNED = {
    "portfolio": {"id": "P-STOCK", "holdings": [
        {"symbol": "SIM-A", "stop_loss": "95.00", "take_profit": "106.00"}]},
    "snapshot": {"timestamp": "2026-11-09T10:00:00+08:00",
                 "prices": {"SIM-A": "95.00"}},
}
SIGNAL = {"event_id": "a" * 64, "portfolio_id": "P-STOCK",
          "symbol": "SIM-A", "rule": "stop_loss", "price": "95.00",
          "threshold": "95.00"}
RUN = "RUN-TEST"
TASK = "P-STOCK|2026-11-09T10:00:00+08:00"
IDS = {"conversation_id": "C-1", "request_id": "XR-1",
       "x_request_id": "XR-1", "tool_call_id": None,
       "run_id": None, "task_key": None}


def ev(etype: str, payload: dict, index: int) -> dict:
    return {"event": etype, "index": index,
            "data": {"type": etype, **payload}}


def conversation() -> dict:
    return ev("conversation_created",
              {"content": json.dumps({"conversation_id": "C-1"})}, 0)


def call(*, call_id="TC-1", execution_key=None) -> dict:
    payload = {"tool_name": TOOL, "tool_call_id": call_id,
               "arguments": PLANNED}
    if execution_key:
        payload["execution_key"] = execution_key
    return ev("tool_call", payload, 1)


def result(signals, *, call_id="TC-1", index=2) -> dict:
    return ev("tool_result", {
        "tool_name": TOOL, "tool_call_id": call_id,
        "structuredContent": {"result": signals}}, index)


def final_answer(payload: dict, index=9) -> dict:
    return ev("final_answer", payload, index)


def execution_started(key="EX-7788", index=2) -> dict:
    return ev("execution_started", {"execution_key": key}, index)


def receipt(**overrides) -> dict:
    entry = {"schema": "topic16-execution-record-v1",
             "source": "mcp-server-receipt", "tool_name": TOOL,
             "execution_key": "EX-7788", "arguments": PLANNED,
             "result": [SIGNAL], "tool_success": True,
             "recorded_at": "2026-11-09T02:00:00Z"}
    entry.update(overrides)
    return entry


def mcp_receipt(**overrides) -> dict:
    entry = {"schema": "topic16-mcp-call-log-v1",
             "source": "mcp-server-log", "tool_name": TOOL,
             "call_id": "TC-1", "conversation_id": "C-1",
             "arguments": PLANNED, "result": [SIGNAL],
             "tool_success": True, "recorded_at": "2026-11-09T02:00:00Z"}
    entry.update(overrides)
    return entry


def key_only_events() -> list[dict]:
    """平台只给「工具调用 + execution_key」，SSE 无结构化工具结果。"""
    return [conversation(), call(execution_key="EX-7788"),
            execution_started()]


def resolve(events, *, records=None, mcp=None) -> dict:
    return nc.resolve_tool_evidence(
        events, nc.DEFAULT_TOOL_EVENT_TYPES, PLANNED,
        mcp_call_log=mcp, execution_records=records,
        platform_ids=IDS, run_id=RUN, task_key=TASK)


class PriceEvidenceCase(unittest.TestCase):
    def assert_rejected(self, outcome: dict, hint: str = "") -> None:
        self.assertNotEqual(outcome["status"], "verified", hint)
        self.assertIsNone(outcome["evidence"], hint)
        self.assertIsNone(outcome.get("signals"), hint)

    def assert_verified(self, outcome: dict, signals: list) -> None:
        self.assertEqual(outcome["status"], "verified")
        self.assertIsNotNone(outcome["evidence"])
        self.assertEqual(outcome["evidence"]["signals"], signals)
        self.assertTrue(outcome["evidence"]["tool_success"])
        self.assertEqual(outcome["evidence"]["zero_hit"], not signals)
        self.assertEqual(outcome["evidence"]["actual_tool_arguments"], PLANNED)

    # ---------- 反例 ① 只有调用没有可信返回 ----------
    def test_01_call_without_any_trusted_return_is_rejected(self):
        self.assert_rejected(resolve([conversation(), call()]),
                             "只有工具调用、没有任何结果或回执，不得计成功")

    def test_02_model_prose_fabricating_empty_array_is_rejected(self):
        events = [conversation(), call(), final_answer({
            "tool_name": TOOL, "result": [],
            "structuredContent": {"result": []},
            "content": "工具返回 []，无风险信号"})]
        self.assert_rejected(resolve(events),
                             "final_answer 里的伪结果数组不能算可信返回")

    def test_03_call_only_with_unsigned_receipt_is_rejected(self):
        """有回执但缺显式成功标记，同样属于没有可信返回。"""
        self.assert_rejected(
            resolve(key_only_events(),
                    records=[receipt(result=[], tool_success=None)]),
            "回执缺显式成功标记不得计成功")

    # ---------- 反例 ② execution_key 只来自模型正文 ----------
    def test_04_execution_key_from_final_answer_is_rejected(self):
        events = [conversation(), call(), final_answer({
            "execution_key": "EX-7788",
            "content": "工具执行键为 EX-7788，返回 []"})]
        self.assert_rejected(
            resolve(events, records=[receipt(result=[])]),
            "execution_key 只能来自平台事件，不能来自 final_answer")
        self.assertNotIn("EX-7788", nc.collect_execution_keys(events))

    def test_05_execution_key_from_message_content_json_is_rejected(self):
        events = [conversation(), call(), ev("message", {
            "content": json.dumps({"execution_key": "EX-7788",
                                  "result": []})}, 8)]
        self.assert_rejected(
            resolve(events, records=[receipt(result=[])]),
            "message.content 里嵌 JSON 也不能提供执行键")
        self.assertEqual(nc.collect_execution_keys(events), [])

    def test_06_execution_key_from_reasoning_is_rejected(self):
        events = [conversation(), call(), ev("reasoning", {
            "executionKey": "EX-7788", "content": "我打算按 EX-7788 关联"}, 8)]
        self.assert_rejected(
            resolve(events, records=[receipt(result=[])]),
            "推理事件里的执行键不采信")
        self.assertEqual(nc.collect_execution_keys(events), [])

    def test_07_platform_execution_events_still_provide_key(self):
        """收紧不能误伤正例：平台显式执行事件 / 目标工具调用仍提供执行键。"""
        events = key_only_events()
        self.assertEqual(nc.collect_execution_keys(events), ["EX-7788"])
        self.assertEqual(nc.collect_tool_call_ids(events), ["TC-1"])

    # ---------- 反例 ③ 同键成功与失败回执冲突 ----------
    def test_08_conflicting_success_and_failure_receipts_are_rejected(self):
        records = [
            receipt(result=[], tool_success=True),
            receipt(result=[], tool_success=False, status="failed",
                    error="MCP timeout", recorded_at="2026-11-09T02:00:01Z"),
        ]
        outcome = resolve(key_only_events(), records=records)
        self.assert_rejected(outcome, "同键成功+失败冲突不得只过滤失败那条")
        self.assertTrue(any("同键冲突" in g for g in outcome["gaps"]))

    def test_09_conflict_by_tool_call_id_is_rejected_too(self):
        records = [mcp_receipt(result=[]),
                   mcp_receipt(result=[], tool_success=False,
                               status="timeout",
                               recorded_at="2026-11-09T02:00:01Z")]
        self.assert_rejected(resolve(key_only_events(), records=records),
                             "按调用标识关联的同键冲突同样不得算成功")

    def test_10_failure_receipt_alone_is_rejected(self):
        self.assert_rejected(resolve(key_only_events(), records=[
            receipt(result=[], tool_success=False, status="failed")]),
            "只有失败回执也不得算成功")

    # ---------- 反例 ④ 回执明确属于其他 run / 任务 ----------
    def test_11_receipt_of_another_run_is_rejected(self):
        outcome = resolve(key_only_events(), records=[
            receipt(result=[], run_id="RUN-OTHER")])
        self.assert_rejected(outcome, "回执 run_id 属于其他 run")
        self.assertTrue(any("其他 run" in g for g in outcome["gaps"]))

    def test_12_receipt_of_another_run_via_call_id_is_rejected(self):
        self.assert_rejected(resolve(key_only_events(), records=[
            mcp_receipt(result=[], run_id="RUN-OTHER")]),
            "按 call_id 关联的回执也须核对 run 归属")

    def test_13_receipt_of_another_task_is_rejected(self):
        self.assert_rejected(resolve(key_only_events(), records=[
            receipt(result=[], task_key="P-BOND|11:00")]),
            "回执 task_key 属于别的任务")

    def test_14_receipt_of_another_conversation_is_rejected(self):
        self.assert_rejected(resolve(key_only_events(), records=[
            receipt(result=[], conversation_id="C-OTHER")]),
            "回执会话标识冲突")

    # ---------- 正例 ----------
    def test_15_unique_success_receipt_with_empty_array_is_verified_zero_hit(self):
        """指令指定的正例：同一次可信调用 + 入参匹配 + 显式成功 + []。"""
        self.assert_verified(
            resolve(key_only_events(), records=[receipt(result=[])]), [])

    def test_16_unique_success_receipt_with_signals_is_verified(self):
        self.assert_verified(
            resolve(key_only_events(), records=[receipt(result=[SIGNAL])]),
            [SIGNAL])

    def test_17_sse_paired_call_with_empty_array_is_verified_zero_hit(self):
        events = [conversation(), call(), result([]),
                  final_answer({"content": "完成"})]
        self.assert_verified(resolve(events), [])

    def test_18_sse_paired_call_with_nonempty_result_is_verified(self):
        """同一次调用的正常非空结果照常通过，不能被收紧误伤。"""
        events = [conversation(), call(), result([SIGNAL]),
                  final_answer({"content": "完成"})]
        self.assert_verified(resolve(events), [SIGNAL])

    def test_19_mcp_receipt_linked_by_call_id_is_verified_zero_hit(self):
        self.assert_verified(resolve(key_only_events(), records=[
            mcp_receipt(result=[])]), [])

    def test_20_matching_run_scope_does_not_block_valid_receipt(self):
        """回执显式写出本次 run / 任务时应当放行（避免只靠缺失字段通过）。"""
        self.assert_verified(resolve(key_only_events(), records=[
            receipt(result=[], run_id=RUN, task_key=TASK,
                    conversation_id="C-1")]), [])

    def test_21_unrelated_bad_receipt_does_not_poison_sse_pairing(self):
        """更强的 SSE 直接配对证据不应被一条无关的冲突回执否定。"""
        events = [conversation(), call(), result([SIGNAL]),
                  final_answer({"content": "完成"})]
        self.assert_verified(resolve(events, records=[
            receipt(result=[], run_id="RUN-OTHER",
                    execution_key="EX-UNRELATED")]), [SIGNAL])

    # ---------- 驱动入口 invoke_task 端到端（本地 mock SSE） ----------
    def invoke_with(self, events: list[dict], records: list[dict]) -> dict:
        body = ["event: " + event["event"] + "\n"
                + "data: " + json.dumps(event["data"], ensure_ascii=False)
                + "\n\n" for event in events]
        body.append("data: [DONE]\n\n")
        raw = "".join(body)

        class FakeResponse:
            status = 200
            headers = {"Content-Type": "text/event-stream",
                       "X-Request-Id": "XR-1", "conversation_id": "C-1"}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def __iter__(self):
                return iter([line.encode("utf-8")
                             for line in raw.splitlines(keepends=True)])

        with TemporaryDirectory() as tmp:
            records_path = Path(tmp) / "execution_records.json"
            records_path.write_text(
                "\n".join(json.dumps(e, ensure_ascii=False) for e in records)
                + "\n", encoding="utf-8")
            config = nc.PlatformConfig(
                base_url="http://127.0.0.1:9", token="TEST",
                agent_name="portfolio-risk-agent",
                execution_records=str(records_path))
            task = {"task_key": TASK, "tool_arguments": PLANNED}
            with mock.patch.object(nc.urllib.request, "urlopen",
                                   return_value=FakeResponse()):
                return nc.invoke_task(config, run_id=RUN, task=task)

    def test_22_invoke_task_ignores_execution_key_from_model_prose(self):
        events = [conversation(), call(), final_answer({
            "execution_key": "EX-7788",
            "content": "工具执行键为 EX-7788，返回 []"})]
        outcome = self.invoke_with(events, [receipt(result=[])])
        self.assertNotEqual(outcome["evidence_status"], "verified")
        self.assertIsNone(outcome["signals"])
        self.assertEqual(outcome["execution_keys"], [])
        self.assertNotIn("EX-7788", outcome["raw_response"]["execution_keys"])

    def test_23_invoke_task_verifies_real_zero_hit_end_to_end(self):
        outcome = self.invoke_with(key_only_events(), [receipt(result=[])])
        self.assertEqual(outcome["evidence_status"], "verified")
        self.assertEqual(outcome["signals"], [])
        self.assertEqual(outcome["execution_keys"], ["EX-7788"])
        self.assertEqual(outcome["tool_evidence"]["pairing"], "execution-key")
        self.assertTrue(outcome["tool_evidence"]["zero_hit"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
