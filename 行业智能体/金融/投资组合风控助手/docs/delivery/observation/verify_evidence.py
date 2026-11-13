"""Read-only cross-check of a signed scheduler database and original receipts."""
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from app.scheduler import instant
from eval.nexent.nexent_client import collect_tool_call_ids


def verify(directory, receipt_file):
    directory = Path(directory)
    plan = json.loads((directory/'plan.json').read_bytes())
    with sqlite3.connect((directory/'schedule.sqlite').resolve().as_uri()+'?mode=ro', uri=True) as c:
        c.row_factory = sqlite3.Row
        c.execute('BEGIN')
        attempts = [dict(r) for r in c.execute('SELECT * FROM schedule_attempts ORDER BY started,attempt_id')]
        signals = {r['event_id']:json.loads(r['detail_json'])['raw_signal'] for r in c.execute('SELECT * FROM signals')}
        runs = {r['run_id']:dict(r) for r in c.execute('SELECT * FROM monitor_runs')}
        pushes = [dict(r) for r in c.execute('SELECT status,simulated,attempts FROM push_records')]
        push_attempts = c.execute('SELECT count(*) FROM push_attempts').fetchone()[0]
        beats = [r[0] for r in c.execute('SELECT observed_at FROM schedule_heartbeats ORDER BY observed_at')]
        db_plan = c.execute('SELECT plan_json FROM schedule_windows WHERE window_id=?',(plan['window_id'],)).fetchone()[0]
    receipts = [json.loads(line) for line in Path(receipt_file).read_bytes().splitlines() if line.strip()]
    counts = dict(on_time=0,late=0,failed=0,missing=0,pending=0)
    checks=[];native_ids=[];now=time.time();closed=now>instant(plan['cutoff'])
    retries=manual=unknown=zero=0
    for slot in plan['slots']:
        all_rows=[a for a in attempts if a['slot_id']==slot['slot_id']]
        auto=[a for a in all_rows if a['source']=='automatic' and a['started']<=instant(plan['cutoff'])]
        success=[a for a in auto if a['status']=='completed' and a['finished']<=instant(plan['cutoff'])]
        outcome=('on_time' if min(a['finished'] for a in success)<=instant(slot['scheduled_at'])+plan['tolerance_seconds'] else 'late') if success else ('failed' if auto else 'missing') if closed else 'pending'
        counts[outcome]+=1;retries+=max(0,len(auto)-1)
        manual+=sum(a['source']=='manual' for a in all_rows);unknown+=sum(a['status']=='unknown' for a in all_rows)
        if not success:continue
        attempt=success[0];problems=[];call_id=None;raw_result=None
        try:
            result=json.loads(attempt['result_json']);ref=result['evidence']
            file=directory/'evidence'/ref['file'];raw=file.read_bytes();payload=json.loads(raw)
            assert hashlib.sha256(raw).hexdigest()==ref['sha256'],'payload_hash'
            assert payload['window_id']==plan['window_id'] and payload['run_id']==result['run_id'],'ownership'
            assert payload['planned_arguments']==slot['tool_arguments'],'planned_args'
            assert payload['evidence_status']=='verified' and payload['dispatch_mode']=='published-agent-workflow','signed_dispatch'
            call_id=payload['tool_evidence']['call_id'];native_ids.append(call_id)
            assert call_id in collect_tool_call_ids(payload['events']),'trusted_target_id'
            match=[r for r in receipts if r.get('call_id')==call_id]
            assert len(match)==1,'unique_receipt'
            row=match[0];wire=row['mcp_raw_result'];raw_result=wire['structuredContent']['result']
            assert row['tool_name']=='evaluate_portfolio_snapshot' and row['arguments']==slot['tool_arguments'],'actual_args'
            assert row['outer_result_accepted'] is True and row['tool_success'] is True and wire['isError'] is False,'native_success'
            assert row.get('rpc_count')==1 and len(row.get('workflow_action_sha256',''))==64,'rpc_action_stamp'
            assert isinstance(raw_result,list) and json.loads(wire['content'][0]['text'])==raw_result==payload['result'],'dual_channels'
            assert result['signal_count']==len(raw_result),'signal_count'
            assert all(signals.get(s['event_id'])==s for s in raw_result),'database_raw_signals'
            assert runs[result['run_id']]['status']=='completed' and runs[result['run_id']]['signal_count']==len(raw_result),'run_record'
            zero+=not raw_result
        except Exception as exc:
            problems.append(str(exc) if isinstance(exc,AssertionError) else type(exc).__name__)
        checks.append({'slot_id':slot['slot_id'],'pass':not problems,'problems':problems,
                       'native_id_sha256_12':hashlib.sha256(call_id.encode()).hexdigest()[:12] if call_id else None,
                       'signals':len(raw_result) if isinstance(raw_result,list) else None})
    return {'window_id':plan['window_id'],'start':plan['start'],'end':plan['end'],'cutoff':plan['cutoff'],
        'planned':len(plan['slots']),**counts,'attempts':len(attempts),'retries':retries,'manual':manual,'unknown':unknown,
        'verified_slots':sum(r['pass'] for r in checks),'native_ids_unique':len(native_ids)==len(set(native_ids)),
        'successful_zero_hit_slots':zero,'native_checks':checks,'persisted_unique_signals':len(signals),
        'push_attempts':push_attempts,'push_statuses':{s:sum(r['status']==s for r in pushes) for s in sorted({r['status'] for r in pushes})},
        'all_pushes_simulated':all(r['simulated']==1 for r in pushes),'plan_matches_database':json.loads(db_plan)==plan,
        'largest_worker_heartbeat_gap_seconds':max((b-a for a,b in zip(beats,beats[1:])),default=None),
        'collection_closed':closed,'counts_balance':sum(counts.values())==len(plan['slots']),
        'real_wechat_delivery_proven':False,'first_evidence_failure':next((r for r in checks if not r['pass']),None)}
