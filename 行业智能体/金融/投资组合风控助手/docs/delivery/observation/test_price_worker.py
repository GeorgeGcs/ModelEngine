import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.scheduler import Scheduler
from app.store import Store
from price_worker import PROFILE, SignedPriceExecutor


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'key').write_bytes(b'x'*32)
        (self.root/'receipts').write_text('')
        self.env = patch.dict(os.environ, {
            'TOPIC16_DISPATCH_KEY_FILE':str(self.root/'key'), 'NEXENT_BASE_URL':'http://localhost:1',
            'NEXENT_AGENT_NAME':'test', 'NEXENT_API_TOKEN':'test-only', 'NEXENT_VERSION':'v2.8.3',
            'NEXENT_MODEL':'test', 'NEXENT_EXECUTION_RECORDS':str(self.root/'receipts')})
        self.env.start();self.addCleanup(self.env.stop)
        self.slot={'slot_id':'slot-1','scheduled_at':'2026-11-11T00:00:00+08:00',
            'tool_arguments':{'portfolio':{'id':'p','holdings':[{'symbol':'S'}]},
                              'snapshot':{'timestamp':'2026-11-09T11:00:00+08:00','prices':{'S':'2'}}}}
        self.plan={'window_id':'test','timezone':'Asia/Shanghai','mode':'nexent-price',
            'execution_profile':PROFILE,'gateway':'local-mock','start':'2026-11-11T00:00:00+08:00',
            'end':'2026-11-12T00:00:00+08:00','cutoff':'2026-11-12T00:05:00+08:00',
            'tolerance_seconds':150,'retry_delay_seconds':1,'lease_seconds':300,'max_attempts':2,
            'window_seconds':60,'max_per_window':100,'slots':[self.slot]}
        self.good={'evidence_status':'verified','scope':'nexent-native-agent',
            'dispatch_mode':'published-agent-workflow','result':[], 'tool_evidence':{'call_id':'native-1'}}
        self.invoke=Mock(side_effect=lambda *a,**k:copy.deepcopy(self.good))
        self.store=Store(self.root/'db.sqlite')

    def executor(self):
        return SignedPriceExecutor(self.store,self.plan,self.root/'evidence',native_invoke=self.invoke)

    def hit(self):
        self.good['result']=[{'event_id':'e1','portfolio_id':'p','timestamp':self.slot['tool_arguments']['snapshot']['timestamp'],
                             'symbol':'S','rule':'take_profit','price':'2','threshold':'1.5'}]

    def test_success_zero_explicit_array(self):
        r=self.executor()(self.slot,'a');self.assertEqual((r['execution_status'],r['signal_count']),('completed',0))
        self.assertEqual(self.invoke.call_args.kwargs['arguments'],self.slot['tool_arguments'])

    def test_hit_persisted_verbatim(self):
        self.hit();r=self.executor()(self.slot,'a')
        self.assertEqual(r['execution_status'],'completed')
        self.assertEqual(self.store.get_signal('e1')['detail']['raw_signal'],self.good['result'][0])
        self.assertTrue(self.store.list_pushes()[0]['simulated'])

    def test_failed_push_retries_without_second_rpc(self):
        self.hit();exe=self.executor();exe.service.gateway.set_failure_mode(True)
        first=exe(self.slot,'a');self.assertEqual(first['execution_status'],'failed')
        exe.service.gateway.set_failure_mode(False)
        second=exe(self.slot,'b');self.assertEqual(second['execution_status'],'completed')
        self.assertTrue(second['reused_native_evidence']);self.assertEqual(first['run_id'],second['run_id'])
        self.assertEqual(self.invoke.call_count,1)
        self.assertEqual(self.store.list_pushes()[0]['attempts'],2)

    def test_missing_signed_receipt_is_unknown(self):
        self.good.pop('dispatch_mode');self.assertEqual(self.executor()(self.slot,'a')['execution_status'],'unknown')

    def test_insufficient_never_zero(self):
        self.good.update(evidence_status='insufficient',result=None)
        self.assertEqual(self.executor()(self.slot,'a')['execution_status'],'unknown')

    def test_missing_array_rejected(self):
        self.good['result']=None
        with self.assertRaises(ValueError):self.executor()(self.slot,'a')

    def test_duplicate_signals_rejected(self):
        self.hit();self.good['result']*=2
        with self.assertRaises(ValueError):self.executor()(self.slot,'a')

    def test_foreign_signal_rejected(self):
        self.hit();self.good['result'][0]['portfolio_id']='other'
        with self.assertRaises(ValueError):self.executor()(self.slot,'a')

    def test_cache_scope_rejected(self):
        exe=self.executor();exe(self.slot,'a');self.slot['tool_arguments']['snapshot']['prices']['S']='3'
        with self.assertRaises(ValueError):exe(self.slot,'b')
        self.assertEqual(self.invoke.call_count,1)

    def test_dispatch_key_change_rejected(self):
        self.executor();(self.root/'key').write_bytes(b'y'*32)
        with self.assertRaises(ValueError):self.executor()

    def test_missing_dispatch_key_rejected(self):
        (self.root/'key').unlink()
        with self.assertRaises(ValueError):self.executor()

    def test_unknown_attempt_does_not_retry_after_restart(self):
        now=[1794326400.0]
        scheduler=Scheduler(self.store,self.plan,clock=lambda:now[0])
        self.invoke.side_effect=TimeoutError
        scheduler.tick(self.executor());now[0]+=400
        restarted=Scheduler(self.store,self.plan,clock=lambda:now[0]);restarted.tick(self.executor())
        self.assertEqual(self.invoke.call_count,1)
        with self.store.connection() as c:
            self.assertEqual(c.execute('SELECT status FROM schedule_attempts').fetchone()[0],'unknown')

    def test_completed_slot_does_not_replay_after_restart(self):
        now=1794326400.0
        Scheduler(self.store,self.plan,clock=lambda:now).tick(self.executor())
        Scheduler(self.store,self.plan,clock=lambda:now+400).tick(self.executor())
        self.assertEqual(self.invoke.call_count,1)


if __name__=='__main__':unittest.main()
