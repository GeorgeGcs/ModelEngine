"""Completion and cross-source conflict regressions; synthetic evidence only."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nexent_client as nc

MANIFEST = json.loads((Path(__file__).resolve().parents[1] / "task_manifest.json").read_text(encoding="utf-8"))
TASK = next(t for t in MANIFEST["tasks"] if t["task_key"] == "P-FUND|2026-11-09T11:00:00+08:00")
ARGS = TASK["tool_arguments"]


def event(kind, **fields):
    return {"index": 0, "event": kind, "data": {
        "type": kind, "tool_name": nc.TOOL_NAME, "tool_call_id": "AUDIT-CALL", **fields}}


def receipt(**fields):
    return {"source": "nexent-execution-record", "tool_name": nc.TOOL_NAME,
            "call_id": "AUDIT-CALL", "arguments": ARGS, "tool_success": True,
            "result": [], **fields}


def pair():
    return [event("tool_call", arguments=ARGS), event("tool_result", result=[])]


def resolve(events, records=None, mcp=None):
    # Assign distinct positions, as the production SSE reader does.
    events = [dict(e, index=i) for i, e in enumerate(events)]
    return nc.resolve_tool_evidence(events, nc.DEFAULT_TOOL_EVENT_TYPES, ARGS,
                                   execution_records=records, mcp_call_log=mcp,
                                   run_id="AUDIT-RUN", task_key=TASK["task_key"])


class PriceCompletionTests(unittest.TestCase):
    def reject(self, result):
        self.assertNotEqual(result["status"], "verified")
        self.assertIsNone(result["evidence"])

    def verify(self, result, signals):
        self.assertEqual(result["status"], "verified", result["gaps"])
        self.assertEqual(result["evidence"]["signals"], signals)
        self.assertEqual(result["evidence"]["zero_hit"], not signals)

    def test_plain_call_with_inline_array_cannot_prove_completion(self):
        for kind in ("tool_call", "tool_use", "tool_request", "mcp_call"):
            for field in ({"result": []}, {"structuredContent": {"result": []}}):
                with self.subTest(kind=kind, field=field):
                    self.reject(resolve([event(kind, arguments=ARGS, **field)]))

    def test_generic_tool_without_success_or_with_pending_state_is_not_return(self):
        for kind in ("tool", "mcp_tool"):
            for fields in ({}, {"status": "running"}):
                with self.subTest(kind=kind, fields=fields):
                    self.reject(resolve([event(kind, arguments=ARGS, result=[], **fields)]))

    def test_explicit_completed_combined_event_is_accepted(self):
        for kind, fields in (("tool_completed", {}), ("tool_result", {}),
                             ("tool", {"status": "completed"}),
                             ("mcp_tool", {"tool_success": True})):
            with self.subTest(kind=kind):
                self.verify(resolve([event(kind, arguments=ARGS, result=[], **fields)]), [])

    def test_separate_call_and_result_preserve_hit_and_zero_hit(self):
        for signals in ([], [{"synthetic": "signal-passed-through-without-recalculation"}]):
            with self.subTest(signals=signals):
                self.verify(resolve([event("tool_call", arguments=ARGS),
                                     event("tool_result", result=signals)]), signals)

    def test_inline_call_array_can_be_confirmed_by_independent_receipt(self):
        self.verify(resolve([event("tool_call", arguments=ARGS, result=[])],
                            [receipt()]), [])

    def test_sse_success_cannot_override_native_failure_or_timeout(self):
        for fields in ({"tool_success": False}, {"status": "failed"},
                       {"error": "TimeoutError"}, {"status": "timeout"},
                       {"status": "cancelled"}, {"isError": True},
                       {"outer_result_accepted": False},
                       {"mcp_raw_result": {"isError": True}}):
            with self.subTest(fields=fields):
                self.reject(resolve(pair(), [receipt(**fields)]))

    def test_failure_not_discarded_for_missing_or_unrecognized_source(self):
        for source in (None, "unrecognized-export-format"):
            with self.subTest(source=source):
                self.reject(resolve(pair(), [receipt(source=source, tool_success=False)]))

    def test_sse_success_cannot_bypass_conflicting_receipts_in_either_order(self):
        entries = [receipt(), receipt(tool_success=False, error="TimeoutError")]
        self.reject(resolve(pair(), entries))
        self.reject(resolve(pair(), list(reversed(entries))))

    def test_mcp_log_failure_blocks_sse_and_execution_record_success(self):
        self.reject(resolve(pair(), [receipt()], [receipt(tool_success=False)]))

    def test_execution_key_failure_blocks_sse_success_even_without_native_call_id(self):
        events = pair()
        events[0]["data"]["execution_key"] = "AUDIT-EXEC"
        self.reject(resolve(events, [receipt(call_id=None, execution_key="AUDIT-EXEC",
                                              tool_success=False)]))

    def test_combined_return_failure_uses_its_call_id(self):
        self.reject(resolve([event("tool_completed", arguments=ARGS, result=[])],
                            [receipt(tool_success=False)]))

    def test_result_only_mcp_arguments_path_cannot_bypass_failure(self):
        self.reject(resolve([event("tool_result", result=[])],
                            [receipt(), receipt(tool_success=False)]))

    def test_unrelated_failure_does_not_block_valid_sse(self):
        self.verify(resolve(pair(), [receipt(call_id="UNRELATED", tool_success=False)]), [])

    def test_explicit_other_run_failure_is_not_this_calls_failure(self):
        self.verify(resolve(pair(), [receipt(run_id="OTHER-RUN", tool_success=False)]), [])

    def test_observation_stream_is_not_evaluation_run_scope(self):
        self.verify(resolve([event("tool_call", arguments=ARGS)],
                            [receipt(observation_run_id="OBSERVATION-STREAM")]), [])

    def test_wrong_actual_input_and_failed_combined_event_remain_rejected(self):
        wrong = copy.deepcopy(ARGS)
        wrong["portfolio"]["id"] = "OTHER-PORTFOLIO"
        self.reject(resolve([event("tool_completed", arguments=wrong, result=[])]))
        self.reject(resolve([event("tool_completed", arguments=ARGS, result=[], isError=True)]))


if __name__ == "__main__":
    unittest.main()
