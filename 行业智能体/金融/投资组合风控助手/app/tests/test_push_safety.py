"""No external traffic: mock transport and real SQLite/HTTP concurrency."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.gateway import LocalMockGateway, WebhookGateway
from app.service import Service
from app.store import Store
from app.server import build_server


class WebhookTests(unittest.TestCase):
    def reply(self, code, body):
        from io import BytesIO
        class Response(BytesIO):
            status = code
        with patch('app.gateway.urllib.request.urlopen', return_value=Response(body)):
            return WebhookGateway(url='https://gateway.invalid').send('SYNTHETIC', 'test')[0]

    def test_configuration_is_not_delivery(self):
        self.assertFalse(WebhookGateway(url='https://gateway.invalid').describe()['real_delivery'])

    def test_business_errors_not_delivered(self):
        for body in ({'errcode':40013}, {'status':'failed'}, {'success':False}):
            self.assertEqual(self.reply(200,json.dumps(body).encode()), 'failed')

    def test_acceptance_is_not_delivery(self):
        for code, body in ((202,{'status':'accepted'}),(200,{'errcode':0}),
                           (200,{'status':'delivered'})):
            self.assertEqual(self.reply(code,json.dumps(body).encode()), 'accepted')

    def test_invalid_or_unrecognized_body_is_unknown(self):
        for body in (b'not json', b'{}', b'[]'):
            self.assertEqual(self.reply(200,body),'unknown')

    def test_timeout_or_lost_response_is_unknown(self):
        with patch('app.gateway.urllib.request.urlopen', side_effect=TimeoutError('secret')):
            status, reason=WebhookGateway(url='https://gateway.invalid').send('SYNTHETIC','test')
        self.assertEqual(status,'unknown');self.assertNotIn('secret',reason)


class PushOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'test.db')
        self.gateway=LocalMockGateway();self.service=Service(self.store,self.gateway,max_per_window=99)
        self.store.push_begin(event_id='E',recipient_id=self.service.recipient_id,
            message='synthetic',gateway_id='local-mock',simulated=True,max_per_window=99)

    def claim(self, **kwargs):
        return self.store.push_retry('E',self.service.recipient_id,max_per_window=99,**kwargs)

    def test_two_concurrent_http_retries_only_send_once(self):
        first=self.claim();self.store.push_finish(first['id'],'failed',lease_token=first['lease_token'])
        entered=threading.Event();release=threading.Event();calls=[]
        def send(*args):
            calls.append(args);entered.set();self.assertTrue(release.wait(5));return 'delivered',None
        server=build_server('127.0.0.1',0,self.service)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request():
            import urllib.request
            req=urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/retry',
                data=b'{"event_id":"E"}',headers={'Content-Type':'application/json'})
            with urllib.request.urlopen(req,timeout=6) as r:return json.load(r)['result']
        try:
            with patch.object(self.gateway,'send',side_effect=send),ThreadPoolExecutor(2) as pool:
                a=pool.submit(request);self.assertTrue(entered.wait(3))
                try:
                    b=pool.submit(request);self.assertFalse(b.result(timeout=3)['retried'])
                finally:release.set()
                self.assertTrue(a.result(timeout=3)['retried'])
            self.assertEqual(len(calls),1);self.assertEqual(len(self.store.list_push_attempts()),2)
        finally:
            release.set();server.shutdown();server.server_close();thread.join(2)

    def test_pending_reentry_and_wrong_owner_cannot_finish(self):
        claim=self.claim();self.assertIsNone(self.claim())
        self.assertIsNone(self.store.push_finish(claim['id'],'delivered',lease_token='wrong'))
        self.assertEqual(self.store.list_pushes()[0]['status'],'sending')

    def test_expired_send_after_restart_is_unknown_and_not_retried(self):
        claim=self.claim(lease_seconds=-1)
        restarted=Store(self.store.path);self.assertEqual(restarted.recover_pushes(),1)
        self.assertIsNone(self.claim())
        self.assertIsNone(restarted.push_finish(claim['id'],'delivered',lease_token=claim['lease_token']))
        self.assertEqual(restarted.list_push_attempts()[0]['status'],'unknown')

    def test_response_loss_does_not_trigger_second_send(self):
        with patch.object(self.gateway,'send',side_effect=TimeoutError) as send:
            self.assertEqual(self.service.retry_push('E')['status'],'unknown')
            self.assertFalse(self.service.retry_push('E')['retried']);self.assertEqual(send.call_count,1)

    def test_retries_count_towards_rate_limit(self):
        self.service.max_per_window=1;self.gateway.failure_mode=True
        self.assertTrue(self.service.retry_push('E')['retried'])
        self.assertFalse(self.service.retry_push('E')['retried'])
        self.assertEqual(len(self.store.list_push_attempts()),1)

    def test_gateway_mode_cannot_change_existing_receipt(self):
        self.assertIsNone(self.claim(gateway_id='webhook',simulated=False))

    def test_new_event_after_retry_quota_is_suppressed_without_send(self):
        self.service.max_per_window=2;self.gateway.failure_mode=True
        self.assertTrue(self.service.retry_push('E')['retried'])
        self.assertTrue(self.service.retry_push('E')['retried'])
        signal={'event_id':'E2','portfolio_name':'synthetic','portfolio_type':'stock',
                'risk_type_name':'stop_loss','trigger_time':'2026-11-10T00:00:00+08:00',
                'symbol':'SIM','level':'test','evidence':'synthetic','review_action':'review'}
        with patch.object(self.gateway,'send') as send:
            result=self.service._push(signal)
        self.assertEqual(result['status'],'suppressed');self.assertTrue(result['suppressed'])
        self.assertFalse(result['attempted']);send.assert_not_called()
        self.assertEqual(len(self.store.list_push_attempts()),2)


if __name__=='__main__':unittest.main()
