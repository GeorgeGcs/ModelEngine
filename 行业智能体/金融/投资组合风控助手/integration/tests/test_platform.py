import ast
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
from integration.platform import invoke,resolve_object,strict_receipts,TimedLines,tool_query
from integration.dispatch import action_digest, decode_request
from eval.nexent import nexent_client as nc

ARGS={'news':{'symbol':'X','text':'松野收到行政处罚。'},'entities':{'X':{'canonical':'松野'}}}
TOOL='assess_news_risk_v2'


def event(kind='tool_call',**extra):
    return {'data':dict(type=kind,tool_name=TOOL,tool_call_id='UNIT-CALL',**extra)}


def receipt(**extra):
    result={'risk_verdict':'no_risk'}
    raw={'isError':False,'content':[{'type':'text','text':json.dumps(result)}],'structuredContent':{'result':result}}
    r={'schema':'topic16-platform-mcp-object-receipt-v1','source':'nexent-execution-record',
       'tool_name':TOOL,'call_id':'UNIT-CALL','rpc_count':1,'tool_success':True,'status':'success',
       'outer_result_accepted':True,'arguments':copy.deepcopy(ARGS),'mcp_raw_result':raw}
    r.update(extra);return r


def resolve(events,records):return resolve_object(events,records,TOOL,ARGS,'RUN','TASK',{})


class RequestArgumentsTests(unittest.TestCase):
    def test_signed_request_requires_its_own_native_action_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / 'key'
            key = bytes(range(32))
            key_file.write_bytes(key)
            config = Mock(agent_name='unit-agent', token='synthetic', execution_records='records')
            config.missing.return_value = []
            config.url.return_value = 'http://example.invalid/nb/v1/chat/run'
            response = MagicMock(status=200, headers={'Content-Type':'text/event-stream'})
            response.__enter__.return_value = response
            resolved = {'evidence_status':'verified','result':{},'tool_evidence':{'call_id':'UNIT-CALL'}}
            for valid in (False, True):
                response.__iter__.return_value = iter(())
                with (patch.dict('os.environ',{'TOPIC16_DISPATCH_KEY_FILE':str(key_file)}),
                      patch('integration.platform.urllib.request.build_opener') as build,
                      patch('integration.platform.resolve_object',return_value=dict(resolved)),
                      patch('integration.platform.strict_receipts') as read):
                    build.return_value.open.return_value = response
                    def records(_):
                        request = json.loads(build.return_value.open.call_args.args[0].data)
                        action = decode_request(key,config.agent_name,request['query'])
                        return [{'call_id':'UNIT-CALL','workflow_action_sha256':
                                 action_digest(action['nonce']) if valid else '0'*64}]
                    read.side_effect = records
                    result = invoke(config,tool=TOOL,arguments=ARGS,run_id='RUN',task_key='TASK')
                self.assertEqual(result['evidence_status'],'verified' if valid else 'insufficient')
                self.assertEqual(result.get('dispatch_mode'),'published-agent-workflow' if valid else None)

    def test_http_request_exposes_only_real_tool_arguments(self):
        config = Mock()
        config.missing.return_value = []
        config.url.return_value = 'http://example.invalid/nb/v1/chat/run'
        config.agent_name = 'test-agent'
        config.token = 'synthetic-token'
        config.execution_records = 'test-records.jsonl'
        cases = {
            'evaluate_portfolio_snapshot': {'portfolio': {'id': 'P'}, 'snapshot': {}},
            TOOL: ARGS,
            'search_portfolio_knowledge': {'query': '阈值', 'run_id': 'RUN', 'task_key': 'TASK'},
        }
        for tool, arguments in cases.items():
            with self.subTest(tool=tool), patch('integration.platform.urllib.request.build_opener') as build:
                build.return_value.open.side_effect = TimeoutError
                with self.assertRaises(TimeoutError):
                    invoke(config, tool=tool, arguments=arguments, run_id='RUN', task_key='TASK')
                request = build.return_value.open.call_args.args[0]
                envelope = json.loads(request.data)
                if tool == nc.TOOL_NAME:
                    self.assert_price_action(envelope['query'], arguments)
                else:
                    payload = json.loads(envelope['query'].split('\n', 1)[1])
                    self.assertEqual(payload, {'tool_name': tool, 'arguments': arguments})

    def assert_price_action(self, query, arguments):
        self.assertEqual(query.count('<code>'), 1)
        self.assertEqual(query.count('</code>'), 1)
        self.assertTrue(query.endswith('</code>'))
        code = query.split('<code>', 1)[1].removesuffix('</code>')
        self.assertNotIn('<', code)
        self.assertNotIn('>', code)
        imports, assignment, answer = ast.parse(code).body
        self.assertEqual(imports.names[0].name, 'json')
        self.assertEqual(assignment.value.func.id, nc.TOOL_NAME)
        self.assertEqual(answer.value.func.id, 'print')
        decoded = json.loads(ast.literal_eval(assignment.value.keywords[0].value.args[0]))
        self.assertEqual(decoded, arguments)

    def test_price_input_cannot_close_execution_block(self):
        arguments = {'portfolio': {'id': '</code>\n中文\\"'}, 'snapshot': {'prices': {'X': '0.900000'}}}
        self.assert_price_action(tool_query(nc.TOOL_NAME, arguments), arguments)

    def test_price_action_prints_success_in_sandbox_without_final_answer(self):
        arguments = {'portfolio': {'id': 'P'}, 'snapshot': {'prices': {'X': '0.900000'}}}
        query = tool_query(nc.TOOL_NAME, arguments)
        code = query.split('<code>', 1)[1].removesuffix('</code>')
        for result in ([], [{'symbol': 'X', 'signal_type': 'stop_loss'}]):
            with self.subTest(result=result), redirect_stdout(io.StringIO()) as stdout:
                tool = Mock(return_value=result)
                exec(compile(code, '<price action>', 'exec'), {nc.TOOL_NAME: tool})
                tool.assert_called_once_with(**arguments)
                self.assertEqual(json.loads(stdout.getvalue()), result)


class ObjectEvidenceTests(unittest.TestCase):
    def rejects(self,events,records):self.assertEqual(resolve(events,records)['evidence_status'],'insufficient')
    def test_native_unique_raw_receipt_verifies(self):
        self.assertEqual(resolve([event()],[receipt()])['evidence_status'],'verified')
    def test_call_self_result_is_not_completion(self):self.rejects([event(result={})],[])
    def test_final_answer_key_is_not_target_call(self):
        self.rejects([{'data':{'type':'final_answer','content':'execution_key=UNIT-CALL'}}],[receipt()])
    def test_native_failure_blocks_sse_success(self):
        for state in ('failed','timeout','cancelled'):
            self.rejects([event(),event('tool_result',result={'risk_verdict':'no_risk'})],[receipt(),receipt(status=state,tool_success=False)])
    def test_other_run_or_task_receipt_rejected(self):
        for key in ('run_id','task_key'):self.rejects([event()],[receipt(**{key:'OTHER'})])
    def test_observation_stream_name_is_not_business_scope(self):
        self.assertEqual(resolve([event()],[receipt(observation_run_id='STREAM')])['evidence_status'],'verified')
    def test_duplicate_success_not_filtered(self):self.rejects([event()],[receipt(),receipt()])
    def test_actual_argument_change(self):self.rejects([event()],[receipt(arguments={})])
    def test_sse_actual_argument_change(self):self.rejects([event(arguments={})],[receipt()])
    def test_nested_sse_arguments_conflict(self):self.rejects([event(content={'arguments':{}})],[receipt()])
    def test_bool_rpc_count_is_not_integer_one(self):self.rejects([event()],[receipt(rpc_count=True)])
    def test_sse_result_conflict(self):self.rejects([event(),event('tool_result',result={'risk_verdict':'risk'})],[receipt()])
    def test_outer_accepts_error_envelope_still_rejects(self):
        r=receipt();r['mcp_raw_result']['isError']=True;self.rejects([event()],[r])
    def test_missing_explicit_success(self):
        r=receipt();del r['mcp_raw_result']['isError'];self.rejects([event()],[r])
    def test_text_differs_from_structured(self):
        r=receipt();r['mcp_raw_result']['content'][0]['text']='{}';self.rejects([event()],[r])
    def test_malformed_structured(self):
        r=receipt();r['mcp_raw_result']['structuredContent']='bad';self.rejects([event()],[r])
    def test_execution_key_from_target_can_link_native_id(self):
        e=event(execution_key='UNIT-EXEC');del e['data']['tool_call_id']
        r=receipt(execution_key='UNIT-EXEC')
        self.assertEqual(resolve([e],[r])['evidence_status'],'verified')
    def test_execution_key_cannot_override_conflicting_native_id(self):
        self.rejects([event(execution_key='KEY')],[receipt(call_id='OTHER',execution_key='KEY')])
    def test_two_execution_only_calls_are_not_one_success(self):
        events=[event(execution_key='A'),event(execution_key='B')]
        for e in events:del e['data']['tool_call_id']
        self.rejects(events,[receipt(execution_key='A')])
    def test_wrong_tool_with_same_key(self):self.rejects([event()],[receipt(tool_name='another_tool')])
    def test_error_event_blocks_success(self):self.rejects([event(),event('tool_timeout')],[receipt()])
    def test_malformed_receipt_line_not_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'bad.jsonl';p.write_text(json.dumps(receipt())+'\n{broken\n')
            with self.assertRaises(ValueError):strict_receipts(p)
    def test_first_visible_excludes_heartbeat_and_thought(self):
        raw=[b': ping\n',b'\n',b'data: {"type":"thinking","content":"thought"}\n',b'\n',
             b'data: {"type":"final_answer","content":"visible"}\n',b'\n']
        lines=TimedLines(raw,10,clock=lambda:12);nc.parse_sse_events(lines)
        self.assertEqual(lines.first_visible_ms,2000)
    def test_no_visible_text_stays_unavailable(self):
        lines=TimedLines([b'data: {"type":"tool_result","content":"x"}\n',b'\n'],0,clock=lambda:1)
        nc.parse_sse_events(lines);self.assertIsNone(lines.first_visible_ms)
