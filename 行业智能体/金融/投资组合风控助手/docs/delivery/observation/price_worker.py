"""A-owned deployment adapter for the existing persistent price scheduler.

Requires the released signed published-Agent dispatch, independent DB and receipt
stream. Failed pushes retry persisted evidence; unknown calls never replay.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time

from app.scheduler import Executor, Scheduler, canonical, instant
from app.store import Store
from integration.platform import invoke

PROFILE = 'published-agent-signed-price-v1'


class SignedPriceExecutor(Executor):
    def __init__(self, store, plan, evidence_dir, *, native_invoke=invoke):
        if (plan.get('execution_profile') != PROFILE or plan['mode'] != 'nexent-price'
                or plan['gateway'] != 'local-mock'):
            raise ValueError('This deployment only admits signed native price and simulated pushes')
        key = Path(os.environ.get('TOPIC16_DISPATCH_KEY_FILE', ''))
        if not key.is_file() or key.stat().st_size != 32:
            raise ValueError('Private signed-dispatch key is required')
        super().__init__(store, plan, evidence_dir)
        self.native_invoke = native_invoke
        fingerprint = canonical({'profile': PROFILE,
            'adapter_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'dispatch_sha256': hashlib.sha256(key.read_bytes()).hexdigest()})
        with store.connection() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS schedule_adapter_environment('
                         'window_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)')
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute('SELECT fingerprint FROM schedule_adapter_environment WHERE window_id=?',
                               (plan['window_id'],)).fetchone()
            if old and old[0] != fingerprint:
                raise ValueError('Frozen adapter environment changed')
            conn.execute('INSERT OR IGNORE INTO schedule_adapter_environment VALUES(?,?)',
                         (plan['window_id'], fingerprint))
            conn.execute('COMMIT')

    def __call__(self, slot, token):
        args = slot['tool_arguments']
        portfolio, snapshot = args['portfolio'], args['snapshot']
        # Slot cache is written exclusively before any business push. A crash is
        # recorded unknown by the scheduler and cannot automatically enter here again.
        saved = self.directory / (slot['slot_id'] + '.json')
        reused = saved.exists()
        if reused:
            raw = saved.read_bytes()
            payload = json.loads(raw)
            if payload['planned_arguments'] != args or payload['window_id'] != self.plan['window_id']:
                raise ValueError('Cached request belongs to a different slot or plan')
        else:
            run_id = self.plan['window_id'] + '-' + slot['slot_id'] + '-' + token
            response = self.native_invoke(self.config, tool='evaluate_portfolio_snapshot',
                arguments=args, run_id=run_id,
                task_key=portfolio['id'] + '|' + snapshot['timestamp'])
            payload = {'window_id': self.plan['window_id'], 'run_id': run_id,
                       'planned_arguments': args, **response}
            raw = canonical(payload).encode()
            fd = os.open(saved, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        ref = {'file': saved.name, 'sha256': hashlib.sha256(raw).hexdigest()}
        run_id = payload['run_id']
        common = {'run_id': run_id, 'mode': 'nexent-price', 'execution_profile': PROFILE,
                  'evidence': ref, 'reused_native_evidence': reused}
        if (payload.get('evidence_status') != 'verified'
                or payload.get('dispatch_mode') != 'published-agent-workflow'
                or payload.get('scope') != 'nexent-native-agent'):
            return dict(common, execution_status='unknown')
        signals, tool = payload.get('result'), payload.get('tool_evidence')
        if not isinstance(signals, list) or not isinstance(tool, dict) or not tool.get('call_id'):
            raise ValueError('Explicit array and native call ID required')
        symbols = {h['symbol'] for h in portfolio['holdings']}
        seen = set()
        for signal in signals:
            if (not isinstance(signal, dict)
                    or not {'event_id','portfolio_id','timestamp','symbol','rule','price','threshold'} <= signal.keys()
                    or signal['portfolio_id'] != portfolio['id'] or signal['symbol'] not in symbols
                    or signal['rule'] not in ('stop_loss','take_profit')
                    or instant(signal['timestamp']) != instant(snapshot['timestamp'])
                    or signal['event_id'] in seen):
                raise ValueError('Invalid native signal or duplicate')
            seen.add(signal['event_id'])
            old = self.store.get_signal(signal['event_id'])
            if old and old['detail'].get('raw_signal') != signal:
                raise ValueError('Native event ID conflict')
        if not reused:
            self.store.open_run(run_id, {'mode': 'nexent-price', 'evidence': ref, 'tool_evidence': tool})
        pushes = []
        for signal in signals:
            row = {'event_id': signal['event_id'], 'run_id': run_id,
                'portfolio_id': portfolio['id'], 'portfolio_name': portfolio.get('name',portfolio['id']),
                'portfolio_type': portfolio.get('type',''), 'symbol': signal['symbol'],
                'risk_type': signal['rule'], 'risk_type_name': signal['rule'],
                'trigger_time': signal['timestamp'], 'evidence': canonical(signal),
                'level': '待复核', 'review_action': '人工核对工具返回与行情依据',
                'detail': {'mode': 'nexent-price', 'raw_signal': signal,
                           'tool_evidence': tool, 'evidence': ref}}
            self.store.insert_signal(row)
            pushes.append(self.service._push(row))
        status = self.push_status(pushes)
        self.store.finish_run(run_id, status, len(signals), sum(bool(p['attempted']) for p in pushes))
        return dict(common, execution_status=status, tool_evidence=tool,
                    signal_count=len(signals), pushes=pushes)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--plan', required=True)
    p.add_argument('--db', required=True)
    p.add_argument('--evidence-dir', required=True)
    p.add_argument('--poll', type=float, default=2)
    args = p.parse_args()
    if not 0.1 <= args.poll <= 30:
        p.error('poll must be between 0.1 and 30 seconds')
    plan = json.loads(Path(args.plan).read_text(encoding='utf-8'))
    store = Store(args.db)
    scheduler = Scheduler(store, plan)
    executor = SignedPriceExecutor(store, plan, args.evidence_dir)
    while time.time() <= instant(plan['cutoff']):
        scheduler.tick(executor)
        time.sleep(args.poll)
    # The final report is produced by a separate, read-only verifier.
    scheduler.recover()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
