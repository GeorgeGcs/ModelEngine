"""Virtual-clock scheduler tests, not a real 24-hour observation."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from app.scheduler import Scheduler, Executor, instant, validate
from app.store import Store


def iso(value):return datetime.fromtimestamp(value,timezone.utc).isoformat()


def plan(start, count=4):
    portfolio=json.loads((ROOT/'data/demo.json').read_text(encoding='utf-8'))['portfolios'][0]
    return {'window_id':'WINDOW-TEST','timezone':'Asia/Shanghai','mode':'local-rules','gateway':'local-mock',
            'start':iso(start),'end':iso(start+86400),'cutoff':iso(start+86700),
            'tolerance_seconds':10,'retry_delay_seconds':5,'lease_seconds':300,'max_attempts':2,
            'window_seconds':60,'max_per_window':99,
            'slots':[{'slot_id':f'slot-{i}','scheduled_at':iso(start+i*60),
                      'tool_arguments':{'portfolio':portfolio,
                          'snapshot':{'timestamp':iso(start),'prices':{'SIM-STOCK-A':'94.00'}}}}
                     for i in range(count)]}


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=Store(self.root/'test.db')
        self.now=[instant('2026-11-10T00:00:00+08:00')];self.plan=plan(self.now[0])
        self.scheduler=Scheduler(self.store,self.plan,clock=lambda:self.now[0])

    def test_plan_rejects_naive_timestamps_and_duplicate_slots(self):
        for key,value in [('start','2026-11-10T00:00:00'),('timezone','UTC'),('lease_seconds',10)]:
            p=copy.deepcopy(self.plan);p[key]=value
            with self.assertRaises(ValueError):validate(p)
        p=copy.deepcopy(self.plan);p['slots'].append(p['slots'][0])
        with self.assertRaises(ValueError):validate(p)

    def test_existing_plan_cannot_change(self):
        changed=copy.deepcopy(self.plan);changed['gateway']='webhook'
        with self.assertRaises(ValueError):Scheduler(self.store,changed)

    def test_environment_change_requires_new_window(self):
        Executor(self.store,self.plan,self.root/'evidence')
        with self.assertRaises(ValueError):Executor(self.store,self.plan,self.root/'different-evidence')

    def test_heartbeat_and_pending_are_not_completed_window(self):
        self.scheduler.tick(lambda s,t:{'execution_status':'completed'})
        self.now[0]+=30;self.scheduler.tick(lambda s,t:{'execution_status':'completed'})
        r=self.scheduler.report()
        self.assertEqual(r['worker_heartbeat']['largest_gap_seconds'],30)
        self.assertEqual(r['pending'],3);self.assertFalse(r['observation_closed'])

    def test_restart_skips_completed_and_resumes_due_slots(self):
        calls=[]
        def execute(s,t):calls.append(s['slot_id']);return {'execution_status':'completed'}
        self.assertEqual(self.scheduler.tick(execute),1)
        restarted=Scheduler(Store(self.store.path),self.plan,clock=lambda:self.now[0])
        self.assertEqual(restarted.tick(execute),0)
        self.now[0]+=60;self.assertEqual(restarted.tick(execute),1)
        self.assertEqual(calls,['slot-0','slot-1'])

    def test_concurrent_workers_claim_one_slot(self):
        other=Scheduler(Store(self.store.path),self.plan,clock=lambda:self.now[0])
        with ThreadPoolExecutor(2) as pool:
            tokens=list(pool.map(lambda w:w.claim(self.plan['slots'][0]),[self.scheduler,other]))
        self.assertEqual(sum(t is not None for t in tokens),1)

    def test_expired_attempt_unknown_no_replay_and_stale_owner_rejected(self):
        slot=self.plan['slots'][0];token=self.scheduler.claim(slot)
        self.now[0]+=301;self.scheduler.recover()
        self.assertIsNone(self.scheduler.claim(slot))
        self.assertFalse(self.scheduler.finish(slot,token,'completed'))
        self.assertEqual(self.scheduler.report()['unknown'],1)

    def test_retry_and_manual_are_not_added_to_slot_denominator(self):
        s=self.plan['slots'][0];token=self.scheduler.claim(s)
        self.scheduler.finish(s,token,'failed');self.now[0]+=6
        token=self.scheduler.claim(s);self.scheduler.finish(s,token,'completed')
        self.now[0]=instant(self.plan['cutoff'])+1
        self.scheduler.tick(lambda s,t:{'execution_status':'completed'},source='manual',slot_id='slot-1')
        r=self.scheduler.report()
        self.assertEqual((r['on_time'],r['missing'],r['retries'],r['manual']),(1,3,1,1))
        self.assertTrue(r['counts_balance']);self.assertEqual(r['planned'],4)

    def test_on_time_late_failed_missing_partition(self):
        for i,status,delay in [(0,'completed',0),(1,'completed',20),(2,'failed',0)]:
            s=self.plan['slots'][i];self.now[0]=instant(s['scheduled_at'])+delay
            token=self.scheduler.claim(s);self.scheduler.finish(s,token,status)
        self.now[0]=instant(self.plan['cutoff'])+1
        r=self.scheduler.report()
        self.assertEqual([r[k] for k in ('on_time','late','failed','missing')],[1,1,1,1])

    def test_no_early_dispatch_and_no_manual_repair_during_window(self):
        self.assertIsNone(self.scheduler.claim(self.plan['slots'][1]))
        with self.assertRaises(ValueError):self.scheduler.claim(self.plan['slots'][0],'manual')

    def test_executor_exception_is_unknown_without_retry(self):
        def error(*args):raise TimeoutError('private exception not stored')
        self.scheduler.tick(error);self.now[0]+=10
        self.assertEqual(self.scheduler.tick(error),0)
        report=self.scheduler.report();self.assertNotIn('private exception',json.dumps(report))

    def test_local_executor_marks_mode_and_persists_simulated_pushes(self):
        executor=Executor(self.store,self.plan,self.root/'evidence')
        self.scheduler.tick(executor)
        r=self.scheduler.report();self.assertEqual(r['on_time'],1)
        self.assertTrue(self.store.list_signals());self.assertTrue(self.store.list_push_attempts())
        detail=json.loads(self.store.list_runs()[0]['detail_json'])
        self.assertEqual(detail['execution_mode'],'local-rules')
        self.assertFalse(r['real_wechat_delivery_proven'])

    def test_real_mode_cannot_fallback_to_local_without_config(self):
        p=copy.deepcopy(self.plan);p['mode']='nexent-price'
        with patch.dict('os.environ',{},clear=True),self.assertRaises(ValueError):
            Executor(self.store,p,self.root/'evidence')

    def test_nexent_transport_fixture_keeps_raw_signal_and_zero_hit(self):
        from eval.nexent.nexent_client import PlatformConfig
        p=copy.deepcopy(self.plan);p['mode']='nexent-price'
        config=PlatformConfig(base_url='https://platform.invalid',token='SYNTHETIC',agent_name='test',execution_records='fixture')
        signal={'event_id':'SYNTHETIC-EVENT','portfolio_id':p['slots'][0]['tool_arguments']['portfolio']['id'],
                'timestamp':p['start'],'symbol':'SIM-STOCK-A','rule':'stop_loss','price':'94.00','threshold':'95.00'}
        with patch('eval.nexent.nexent_client.PlatformConfig.from_env',return_value=config):
            executor=Executor(self.store,p,self.root/'evidence')
        for token,signals in [('SYNTHETIC-1',[signal]),('SYNTHETIC-2',[])]:
            with patch('eval.nexent.nexent_client.invoke_task',return_value={
                'evidence_status':'verified','signals':signals,'tool_evidence':{'call_id':token}}):
                result=executor(p['slots'][0],token)
            self.assertEqual(result['execution_status'],'completed')
            self.assertEqual(result['signal_count'],len(signals))
        self.assertEqual(self.store.get_signal(signal['event_id'])['detail']['raw_signal'],signal)
        self.assertEqual(len(list((self.root/'evidence').glob('*.json'))),2)

    def test_real_mode_insufficient_never_persists_or_pushes(self):
        from eval.nexent.nexent_client import PlatformConfig
        p=copy.deepcopy(self.plan);p['mode']='nexent-price'
        config=PlatformConfig(base_url='https://platform.invalid',token='SYNTHETIC',agent_name='test',execution_records='fixture')
        with patch('eval.nexent.nexent_client.PlatformConfig.from_env',return_value=config):
            executor=Executor(self.store,p,self.root/'evidence')
        with patch('eval.nexent.nexent_client.invoke_task',return_value={'evidence_status':'insufficient'}):
            self.assertEqual(executor(p['slots'][0],'SYNTHETIC')['execution_status'],'unknown')
        self.assertEqual(self.store.counts()['signals'],0)

    def test_cli_restarts_without_replaying_completed_and_exports_report(self):
        p=plan(time.time()-2,1);file=self.root/'plan.json';file.write_text(json.dumps(p),encoding='utf-8')
        command=[sys.executable,'-B','-m','app.scheduler','run','--plan',str(file),'--db',str(self.root/'cli.db'),
                 '--evidence-dir',str(self.root/'cli-evidence'),'--once']
        for _ in range(2):subprocess.run(command,cwd=ROOT,check=True,capture_output=True)
        output=self.root/'report.json'
        subprocess.run([sys.executable,'-B','-m','app.scheduler','report','--plan',str(file),
                        '--db',str(self.root/'cli.db'),'--output',str(output)],cwd=ROOT,check=True,capture_output=True)
        r=json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual(len(r['slots'][0]['attempts']),1)


if __name__=='__main__':unittest.main()
