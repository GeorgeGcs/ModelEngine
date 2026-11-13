import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mcp_types import CallToolRequestParams, ListToolsRequest

from app.scheduler import Executor
from app.store import Store
from eval.nexent.nexent_client import PlatformConfig
from integration.dispatch import decode_request, encode_request
from integration.check import check_inventory, tool_catalog
from integration.platform import resolve_object
from integration.wechat import TOOL, authorize
from mcps.wechat.server import server


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = Store(self.root / "push.sqlite")
        key = self.root / "key"
        self.key = bytes(range(32))
        key.write_bytes(self.key)
        env = patch.dict(os.environ, {
            "TOPIC16_WECHAT_MCP_KEY_FILE": str(key),
            "TOPIC16_DISPATCH_KEY_FILE": str(key),
            "TOPIC16_WECHAT_MCP_DB": str(self.store.path),
            "TOPIC16_WECHAT_MCP_TRANSPORT": "local-mock",
            "TOPIC16_RECIPIENT_ID": "TEST-ONLY",
            "TOPIC16_WECHAT_MAX_PER_WINDOW": "1",
            "TOPIC16_CLAWBOT_ACCOUNT": "test-account",
            "TOPIC16_CLAWBOT_TARGET": "test-target",
            "TOPIC16_CLAWBOT_COMMAND_JSON": '["test-openclaw"]',
        })
        env.start()
        self.addCleanup(env.stop)

    def wire(self, args):
        params = CallToolRequestParams(name=TOOL, arguments=args)
        wire = asyncio.run(server._handle_call_tool(None, params)).model_dump(mode="json", by_alias=True)
        if wire["isError"] is False:
            self.assertEqual(json.loads(wire["content"][0]["text"]), wire["structuredContent"]["result"])
        return wire

    def executor(self):
        config = PlatformConfig(base_url="https://platform.invalid", token="TEST", agent_name="test-agent",
                                execution_records="fixture")
        plan = {"mode":"nexent-price","gateway":"nexent-wechat","window_id":"TEST",
                "window_seconds":60,"max_per_window":1,"lease_seconds":300}
        with patch.object(PlatformConfig, "from_env", return_value=config):
            return Executor(self.store, plan, self.root/"evidence")

    def run_slot(self, event, attempt):
        date = "2026-11-13T10:00:00+08:00"
        slot = {"slot_id":"slot-1","tool_arguments":{"portfolio":{"id":"P","holdings":[{"symbol":"X"}]},
                                                    "snapshot":{"timestamp":date,"prices":{"X":"90"}}}}
        signal = {"event_id":event,"portfolio_id":"P","timestamp":date,"symbol":"X",
                  "rule":"stop_loss","price":"90","threshold":"95"}

        def dispatch(config, *, tool, arguments, **kwargs):
            self.assertEqual(tool, TOOL)
            query, _ = encode_request(self.key, config.agent_name, tool, arguments)
            payload = decode_request(self.key, config.agent_name, query)
            wire = self.wire(payload["arguments"])
            call_id = "TEST-MCP-"+attempt
            events = [{"event":"tool_call","data":{"tool_name":tool,"call_id":call_id,"arguments":arguments}}]
            native = {"schema":"topic16-platform-mcp-object-receipt-v1","source":"nexent-execution-record",
                      "tool_name":tool,"call_id":call_id,"arguments":arguments,"rpc_count":1,
                      "tool_success":True,"outer_result_accepted":True,"status":"success","mcp_raw_result":wire}
            result = resolve_object(events, [native], tool, arguments, kwargs["run_id"], kwargs["task_key"], {})
            return dict(result, dispatch_mode="published-agent-workflow")

        with patch("eval.nexent.nexent_client.invoke_task", return_value={
            "evidence_status":"verified","signals":[signal],"tool_evidence":{"call_id":"TEST-PRICE"}
        }), patch("integration.platform.invoke", side_effect=dispatch):
            return self.executor()(slot, attempt)["pushes"][0]

    def test_scheduler_sdk_persistence_dedup_and_quota(self):
        initial = self.run_slot("EVENT-1", "first")
        duplicate = self.run_slot("EVENT-1", "second")
        suppressed = self.run_slot("EVENT-2", "third")
        self.assertEqual(initial["status"], "delivered")
        self.assertTrue(initial["simulated"])
        self.assertEqual(initial["mcp_call_id"], "TEST-MCP-first")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(suppressed["status"], "suppressed")
        self.assertEqual(len(self.store.list_push_attempts()), 1)
        self.assertIsNone(self.store.confirm_clawbot_phone(initial["attempt_id"], "test"))

    def test_clawbot_receipt_and_phone_confirmation(self):
        response = {"action":"send","channel":"openclaw-weixin","dryRun":False,
                    "payload":{"result":{"messageId":"synthetic-message"}}}
        with patch.dict(os.environ, {"TOPIC16_WECHAT_MCP_TRANSPORT":"clawbot"}), patch(
                "app.clawbot.subprocess.run",
                return_value=subprocess.CompletedProcess([],0,json.dumps(response).encode())) as cli:
            result = self.run_slot("EVENT-1", "first")
            self.run_slot("EVENT-1", "second")
        cli.assert_called_once()
        self.assertEqual(result["status"], "accepted")
        attempt = result["attempt_id"]
        command = cli.call_args.args[0]
        self.assertIn(attempt, command[command.index("--message") + 1])
        self.assertFalse(self.store.list_pushes(event_id="EVENT-1")[0]["phone_confirmed_at"])
        self.assertIsNone(self.store.confirm_clawbot_phone("wrong", "test"))
        confirmation = self.store.confirm_clawbot_phone(attempt, "test")
        self.assertEqual(confirmation["provider_message_id"], "synthetic-message")
        self.assertTrue(self.store.list_pushes(event_id="EVENT-1")[0]["phone_confirmed_at"])
        self.assertIsNone(self.store.confirm_clawbot_phone(attempt, "test"))

    def test_unconfirmed_cli_result_is_unknown_and_never_replayed(self):
        response = {"action":"send","channel":"openclaw-weixin","dryRun":False,"messageId":""}
        with patch.dict(os.environ, {"TOPIC16_WECHAT_MCP_TRANSPORT":"clawbot"}), patch(
                "app.clawbot.subprocess.run",
                return_value=subprocess.CompletedProcess([],0,json.dumps(response).encode())) as cli:
            self.assertEqual(self.run_slot("EVENT-1", "first")["status"], "unknown")
            self.assertEqual(self.run_slot("EVENT-1", "second")["status"], "unknown")
        cli.assert_called_once()

    def test_unauthorized_and_missing_events_produce_mcp_error(self):
        event, run = "absent", "run"
        for authorization in ("tampered", authorize(event, run, ["local-mock","TEST-ONLY",60,1])):
            result = self.wire({"event_id":event,"run_id":run,"authorization":authorization})
            self.assertIs(result["isError"], True)
        self.assertFalse(self.store.list_push_attempts())

    def test_manifest_matches_tool_list(self):
        actual = asyncio.run(server._handle_list_tools(None, ListToolsRequest()))
        actual = [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in actual.tools]
        expected = json.loads((Path(__file__).parents[1]/"schema.json").read_text(encoding="utf-8"))
        self.assertEqual(expected, {"tools":actual})
        inventory = {"tools":[entry["tool"] for entry in tool_catalog(wechat=True)
                              if entry["tool"]["name"] != "assess_news_risk"]}
        self.assertTrue(check_inventory(inventory, wechat=True)["exact_tools"])
        inventory["tools"].pop()
        self.assertFalse(check_inventory(inventory, wechat=True)["exact_tools"])
        self.assertTrue(check_inventory(inventory)["exact_tools"])

    def test_simulation_authorization_cannot_send_on_live_server(self):
        event, run = "absent", "run"
        auth = authorize(event, run, ["local-mock","TEST-ONLY",60,1])
        with patch.dict(os.environ, {"TOPIC16_WECHAT_MCP_TRANSPORT":"clawbot"}), patch(
                "app.clawbot.subprocess.run") as cli:
            result = self.wire({"event_id":event,"run_id":run,"authorization":auth})
            self.assertIs(result["isError"], True)
            cli.assert_not_called()
