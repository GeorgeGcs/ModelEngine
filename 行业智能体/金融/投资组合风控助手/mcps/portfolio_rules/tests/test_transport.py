"""Rule MCP wire regressions; run with the pinned MCP 2.3 project environment."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.func_metadata import func_metadata
from mcp_types import CallToolResult

from mcps.portfolio_rules import server as rules_server


class MCPTransportTests(unittest.TestCase):
    def setUp(self):
        self.portfolio = {
            "id": "transport-demo",
            "holdings": [
                {"symbol": "SIM", "stop_loss": "9.99", "take_profit": "10.01"}
            ],
        }
        self.snapshot = {
            "timestamp": "2026-11-07T09:30:00+08:00",
            "prices": {"SIM": "9.99"},
        }
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.audit = Path(temporary) / "calls.jsonl"
        self.enterContext(patch.object(rules_server, "AUDIT_LOG", self.audit))
        self.enterContext(patch.object(rules_server, "CORRELATION", {}))

    def assert_successful_wire(self, result):
        self.assertIsInstance(result, CallToolResult)
        self.assertIn("is_error", result.model_fields_set)
        wire = result.model_dump(mode="json", by_alias=True, exclude_none=True)
        self.assertIn("isError", wire)
        self.assertIs(wire["isError"], False)
        self.assertEqual(len(wire["content"]), 1)
        self.assertEqual(wire["content"][0]["type"], "text")
        self.assertIn("structuredContent", wire)
        structured = wire["structuredContent"]
        self.assertIn("result", structured)
        self.assertIsInstance(structured["result"], list)
        self.assertEqual(json.loads(wire["content"][0]["text"]), structured["result"])
        return wire

    def assert_successful_audit(self, signals):
        records = [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(records), 1)
        self.assertIs(records[0]["tool_success"], True)
        self.assertEqual(records[0]["tool_name"], "evaluate_portfolio_snapshot")
        self.assertEqual(records[0]["arguments"], {
            "portfolio": self.portfolio, "snapshot": self.snapshot,
        })
        self.assertEqual(records[0]["result"], signals)

    def test_hit_has_matching_text_and_structured_signal(self):
        before = copy.deepcopy((self.portfolio, self.snapshot))
        result = rules_server.evaluate_portfolio_snapshot(self.portfolio, self.snapshot)
        wire = self.assert_successful_wire(result)
        signals = wire["structuredContent"]["result"]
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["portfolio_id"], "transport-demo")
        self.assertEqual(signals[0]["symbol"], "SIM")
        self.assertEqual(signals[0]["rule"], "stop_loss")
        self.assertEqual(signals[0]["price"], "9.99")
        self.assertEqual(signals[0]["threshold"], "9.99")
        self.assertTrue(signals[0]["event_id"])
        self.assertEqual((self.portfolio, self.snapshot), before)
        self.assert_successful_audit(signals)

    def test_successful_zero_hit_keeps_explicit_empty_text_and_array(self):
        self.snapshot["prices"]["SIM"] = "10.00"
        result = rules_server.evaluate_portfolio_snapshot(self.portfolio, self.snapshot)
        wire = self.assert_successful_wire(result)
        self.assertEqual(wire["content"][0]["text"], "[]")
        self.assertEqual(wire["structuredContent"]["result"], [])
        self.assert_successful_audit([])

    def test_rule_errors_raise_and_do_not_write_successful_zero_hit(self):
        for prices in ({}, {"SIM": "NaN"}):
            with self.subTest(prices=prices):
                self.snapshot["prices"] = prices
                with self.assertRaises(ToolError):
                    rules_server.evaluate_portfolio_snapshot(self.portfolio, self.snapshot)
                self.assertFalse(self.audit.exists(), "Rejected calls must not be logged as successful zero hits")

    def test_explicit_result_preserves_original_input_and_output_schema(self):
        def original_shape(portfolio: rules_server.Portfolio, snapshot: rules_server.Snapshot) -> list[dict]:
            raise AssertionError("Schema inspection must not call the baseline-shaped function")

        original_shape.__name__ = rules_server.evaluate_portfolio_snapshot.__name__
        original = func_metadata(original_shape)
        explicit = func_metadata(rules_server.evaluate_portfolio_snapshot)
        self.assertEqual(explicit.arg_model.model_json_schema(), original.arg_model.model_json_schema())
        self.assertEqual(explicit.output_schema, original.output_schema)
        result = rules_server.evaluate_portfolio_snapshot(self.portfolio, self.snapshot)
        self.assertIs(explicit.convert_result(result), result)


if __name__ == "__main__":
    unittest.main()
