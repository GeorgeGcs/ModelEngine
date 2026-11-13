import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from integration import dispatch


KEY = bytes(range(32))
AGENT = 'published-unit-agent'
TOOL = 'evaluate_portfolio_snapshot'
ARGS = {'portfolio': {'id': 'P'}, 'snapshot': {'prices': {'X': '0.90'}}}


def load_runtime():
    path = Path(__file__).resolve().parents[2] / 'eval/nexent/deployment/startup/topic16_workflow_dispatch.py'
    spec = importlib.util.spec_from_file_location('unit_workflow_runtime', path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'topic16_dispatch_protocol': dispatch}):
        spec.loader.exec_module(module)
    return module


class DispatchTests(unittest.TestCase):
    def test_bound_tool_and_exact_arguments_roundtrip(self):
        args = {'query': '分析</code>\n', 'run_id': 'R', 'task_key': 'K'}
        query, nonce = dispatch.encode_request(KEY, AGENT, 'search_portfolio_knowledge', args, now=100)
        action = dispatch.decode_request(KEY, AGENT, '[Current time]\n\n' + query, now=101)
        self.assertEqual(action['arguments'], args)
        self.assertEqual(action['nonce'], nonce)

    def test_rejects_wrong_agent_key_tamper_expired_or_multiple(self):
        query, _ = dispatch.encode_request(KEY, AGENT, TOOL, ARGS, now=100)
        for key, agent, text, now in [
                (KEY, 'other-agent', query, 101),
                (b'x' * 32, AGENT, query, 101),
                (KEY, AGENT, query[:-1] + ('1' if query[-1] == '0' else '0'), 101),
                (KEY, AGENT, query, 161),
                (KEY, AGENT, query + '\n' + query, 101)]:
            with self.subTest(now=now, agent=agent), self.assertRaises(ValueError):
                dispatch.decode_request(key, agent, text, now=now)
        self.assertIsNone(dispatch.decode_request(KEY, AGENT, 'an ordinary chat request', now=101))

    def test_allowlist_rejects_unknown_tools_and_extra_arguments(self):
        for tool, args in [('arbitrary_tool', ARGS), (TOOL, {**ARGS, 'sanitize_inputs_outputs': True})]:
            with self.subTest(tool=tool), self.assertRaises(ValueError):
                dispatch.encode_request(KEY, AGENT, tool, args)

    def test_nonce_claim_and_scope_of_receipt_stamp(self):
        runtime = load_runtime()
        query, nonce = dispatch.encode_request(KEY, AGENT, TOOL, ARGS)
        action = dispatch.decode_request(KEY, AGENT, query)
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'claims.sqlite'
            runtime.claim_action(db, action)
            with self.assertRaisesRegex(ValueError, 'already used'):
                runtime.claim_action(db, action)
            with patch.object(runtime.time,'time',return_value=action['expires']+1):
                with self.assertRaisesRegex(ValueError, 'expired before dispatch'):
                    runtime.claim_action(db, action)
        output = []
        sink = runtime.stamp_receipts(output.append)
        token = runtime.active_action.set(action)
        try:
            sink({'tool_name': TOOL})
            sink({'tool_name': 'another_tool'})
        finally:
            runtime.active_action.reset(token)
        sink({'tool_name': TOOL})
        self.assertEqual(output[0]['workflow_action_sha256'], dispatch.action_digest(nonce))
        self.assertNotIn('workflow_action_sha256', output[1])
        self.assertNotIn('workflow_action_sha256', output[2])
