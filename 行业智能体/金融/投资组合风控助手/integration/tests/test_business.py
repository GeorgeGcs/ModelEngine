import copy
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
from app.store import Store
from app.service import Service
from integration.runner import execute
from integration.rehearsal import make_plan,run_plan
from integration.check import readiness,tool_catalog,check_inventory
from knowledge.client import KnowledgeClient,load_assets
from knowledge.tests.test_knowledge import NativeDouble
from portfolio_risk.rules import evaluate_snapshot
from news_risk.rules_v2 import assess_news_v2


class BusinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=Store(self.root/'state.db')
        self.case=copy.deepcopy(make_plan(42)['selected'][0]['case'])
    def test_metadata_and_no_risk_persist(self):
        self.case['knowledge_queries']=[]
        self.case['news_items'][0]['text']='松野材料未收到行政处罚。'
        self.case['news_items'][0]['name']='松野材料'
        execute(self.store,self.case,'local','local-rules',self.root/'evidence')
        record=Store(self.store.path).business_record('local')
        news=next(c['payload'] for c in record['components'] if c['key'].startswith('news:'))[0]
        self.assertEqual(news['result']['risk_verdict'],'no_risk')
        self.assertEqual(news['news']['published_at'],self.case['news_items'][0]['published_at'])
        self.assertEqual(news['news']['source'],'project-synthetic-demo')
    def test_reused_run_rejected_without_overwrite(self):
        self.case['knowledge_queries']=[]
        execute(self.store,self.case,'once','local-rules',self.root/'one')
        before=self.store.business_record('once')
        with self.assertRaises(ValueError):execute(self.store,self.case,'once','local-rules',self.root/'two')
        self.assertEqual(self.store.business_record('once'),before)
    def test_duplicate_event_has_new_business_link_without_new_send(self):
        self.case['knowledge_queries']=[]
        execute(self.store,self.case,'first','local-rules',self.root/'one')
        second=execute(self.store,self.case,'second','local-rules',self.root/'two')
        self.assertTrue(self.store.business_record('second')['signals'])
        self.assertTrue(all(not p['attempted'] for p in second['pushes']))
    def test_invalid_news_keeps_error_not_no_risk(self):
        self.case['knowledge_queries']=[];self.case['news_items'][0]['published_at']='yesterday'
        r=execute(self.store,self.case,'bad','local-rules',self.root/'bad')
        self.assertTrue(r['news_review_required']);self.assertIsNone(r['news'][0]['result'])
        self.assertEqual(self.store.business_record('bad')['run']['status'],'review_required')
    def test_native_missing_config_does_not_fallback(self):
        with patch.dict(os.environ,{},clear=True),self.assertRaises(ValueError):
            execute(self.store,self.case,'bad','nexent-integrated',self.root/'bad')
        self.assertEqual(self.store.list_runs(),[])
    def test_native_contract_roundtrip_then_sqlite_reopen(self):
        _,docs=load_assets();doc=next(iter(docs.values()));api=NativeDouble(doc)
        client=KnowledgeClient(self.store,api=api,index_id='kb-test',role='asset_admin');client.import_asset(doc)
        api.rows=[dict(index='kb-test',path_or_url='object/policy',id='UNIT-CHUNK',content='持仓价格小于或等于止损阈值时生成止损信号，大于或等于止盈阈值时生成止盈信号。',score=.8)]
        def mock_invoke(config,*,tool,arguments,run_id,task_key):
            if tool=='evaluate_portfolio_snapshot':result=evaluate_snapshot(**arguments)
            elif tool=='assess_news_risk_v2':result=assess_news_v2(arguments['news'],arguments['entities'])
            else:result=client.retrieve(**arguments)
            return {'evidence_status':'verified','scope':'mock-native-contract','result':result,
                    'tool_evidence':{'call_id':'UNIT-'+tool},'timing':{'first_visible_ms':None,'end_to_end_ms':1}}
        env={'PATH':os.environ.get('PATH',''),'NEXENT_BASE_URL':'http://example.invalid','NEXENT_API_TOKEN':'synthetic','NEXENT_AGENT_NAME':'unit',
             'NEXENT_EXECUTION_RECORDS':str(self.root/'unused'),'TOPIC16_KB_DB':str(self.store.path)}
        events=[]
        def early_price(event):
            self.assertEqual(event['type'],'price_verified')
            record=self.store.business_record('native')
            self.assertTrue(any(c['key'].startswith('price:') for c in record['components']))
            self.assertFalse(any(c['key'].startswith('news:') for c in record['components']))
            events.append(event)
        with patch.dict(os.environ,env,clear=True):
            result=execute(self.store,self.case,'native','nexent-integrated',self.root/'native',
                           native_invoke=mock_invoke,on_verified=early_price)
        self.assertEqual(len(events),1)
        self.assertTrue(result['evidence_complete'],result['failures'])
        record=Store(self.store.path).business_record('native')
        self.assertTrue(record['knowledge']);self.assertTrue(record['signals']);self.assertTrue(record['pushes'])
        self.assertTrue(all(p['simulated'] for p in record['pushes']))
        self.assertTrue(all(s['detail']['knowledge_retrieval_ids'] for s in record['signals']))
        with patch.dict(os.environ,env,clear=True):
            r=run_plan(make_plan(42),self.store,'nexent-integrated',self.root/'rehearsal',
                run=lambda *args:execute(*args,native_invoke=mock_invoke))
        self.assertEqual(r['runs'],9);self.assertTrue(r['all_consistent'],r)
        self.assertTrue(r['development_run']);self.assertEqual(r['scopes'],['mixed-or-mock'])
    def test_fixed_seed_stratified_three_cases(self):
        p=make_plan(100);self.assertEqual(p,make_plan(100))
        self.assertEqual({c['case']['portfolios'][0]['type'] for c in p['selected']},{'股票','基金','混合'})
        self.assertEqual(p['repetitions'],3)
    def test_modified_plan_rejected(self):
        p=make_plan(1);p['selected'][0]['case']['snapshot']['prices']={}
        with self.assertRaises(ValueError):run_plan(p,self.store,'local-rules',self.root/'runs')
    def test_clean_check_rejects_old_run_and_busy_port(self):
        self.store.open_run('old',{})
        with socket.socket() as s:
            s.bind(('127.0.0.1',0));s.listen();port=s.getsockname()[1]
            result=readiness(mode='local-rules',db=self.store.path,ports=[port])
        self.assertFalse(result['checks']['clean_business_state']);self.assertFalse(result['checks']['port:'+str(port)])
    def test_missing_tool_permission_and_template_cannot_pass_inventory(self):
        i={'tools':[],'published':True,'platform_version':'v2.8.3','provenance':{'kind':'template_not_platform_export'}}
        r=check_inventory(i);self.assertFalse(r['exact_tools']);self.assertFalse(r['knowledge_permission_declared']);self.assertFalse(r['native_inventory_provenance_declared'])
    def test_readiness_uses_reachable_probe_without_claiming_import(self):
        receipts=self.root/'receipts.json';receipts.write_text('[]')
        env={'NEXENT_BASE_URL':'http://example.invalid','NEXENT_API_TOKEN':'synthetic','NEXENT_AGENT_NAME':'unit',
             'NEXENT_EXECUTION_RECORDS':str(receipts),'TOPIC16_KB_DB':str(self.store.path)}
        inventory={'tools':[{'name':r['tool']['name'],'inputSchema':r['tool']['inputSchema']} for r in tool_catalog()
                            if r['tool']['name']!='assess_news_risk'],
            'published':True,'platform_version':'v2.8.3','knowledge':{'permission_verified':True},
            'provenance':{'kind':'platform-published-config','sha256':'f'*64}}
        with patch.dict(os.environ,env,clear=True),patch('integration.check.probe',return_value={'status':'reachable'}),patch('integration.check.version',return_value='2.3.0'),patch('integration.check.KnowledgeClient.from_env') as client:
            client.return_value.index='unit';client.return_value.role='risk_reader'
            client.return_value.api.request.return_value={'status':'success','chunks':[{'id':'UNIT'}]}
            r=readiness(mode='nexent-integrated',db=self.store.path,ports=[],inventory=inventory,live_probe=True)
        self.assertTrue(r['ready_for_verification'],r['checks']);self.assertFalse(r['platform_import_proven'])
