"""Capture-only tests; not evidence of production scheduler ID propagation."""
import importlib.util
import json
from pathlib import Path
import unittest
import test_receipt_observation as baseline

spec=importlib.util.spec_from_file_location('object_observation',Path(__file__).resolve().parents[1]/'startup/topic16_object_observation.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
Capture=module.capture_type(baseline.Capture)

def receipt(result, *, accepted=True, error=None, call='UNIT-ID', explicit=True, mismatch=False):
    c=Capture(call);c.begin('assess_news_risk_v2',{'news':{'text':'synthetic'},'entities':{'X':{}}})
    raw={'structuredContent':{'result':result},'content':[{'type':'text','text':json.dumps({} if mismatch else result)}]}
    if explicit:raw['isError']=False
    c.returned(raw);return c.finish(accepted=accepted,error_type=error)

class ObjectObservationTests(unittest.TestCase):
    def test_wechat_acceptance_is_a_verified_tool_receipt(self):
        arguments = {'event_id':'e','run_id':'r','authorization':'synthetic'}
        result = {'status':'accepted','event_id':'e','run_id':'r'}
        for args, expected in ((arguments, True), ({'event_id':'e'}, False)):
            capture = Capture('UNIT-WECHAT-ID')
            capture.begin('send_wechat_risk_notification', args)
            capture.returned({'isError':False,'content':[{'type':'text','text':json.dumps(result)}],
                              'structuredContent':{'result':result}})
            record = capture.finish(accepted=True)
            self.assertEqual(record['tool_success'], expected)
            self.assertEqual(record['result']['status'], 'accepted')

    def test_knowledge_hit_no_match_and_error(self):
        for status,error in (('hit',False),('no_match',False),('timeout',True)):
            c=Capture('UNIT-KB-ID');c.begin('search_portfolio_knowledge',{'query':'规则','run_id':'r','task_key':'t'})
            result={'status':status,'citations':[]}
            c.returned({'isError':error,'content':[{'type':'text','text':json.dumps(result)}],
                        'structuredContent':{'result':result}})
            r=c.finish(accepted=True);self.assertEqual(r['tool_success'],not error)
    def test_risk_and_no_risk_objects(self):
        for verdict in ('risk','no_risk'):
            r=receipt({'risk_verdict':verdict});self.assertTrue(r['tool_success']);self.assertEqual(r['call_id'],'UNIT-ID')
    def test_array_is_not_news_no_risk(self):self.assertFalse(receipt([])['tool_success'])
    def test_missing_explicit_success_is_insufficient(self):self.assertEqual(receipt({},explicit=False)['status'],'insufficient')
    def test_text_disagreement_is_insufficient(self):self.assertFalse(receipt({'risk_verdict':'risk'},mismatch=True)['tool_success'])
    def test_timeout_and_cancel_not_success(self):
        for error in ('TimeoutError','CancelledError'):
            self.assertEqual(receipt({},accepted=False,error=error)['status'],'failed')
    def test_missing_call_id_not_success(self):self.assertFalse(receipt({},call=None)['tool_success'])
    def test_accepted_error_envelope_still_failed(self):
        c=Capture('UNIT-ID');c.begin('assess_news_risk_v2',{'news':{},'entities':{}})
        c.returned({'isError':True,'content':[{'type':'text','text':'invalid input'}]})
        r=c.finish(accepted=True);self.assertTrue(r['outer_result_accepted']);self.assertFalse(r['tool_success'])
