"""Create a new immutable 24-hour plan from the unchanged frozen input manifest."""
import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
from app.scheduler import validate
from price_worker import PROFILE


def make_plan(manifest, start, window_id):
    begin=datetime.fromisoformat(start)
    if begin.tzinfo is None or begin.utcoffset()!=timedelta(hours=8):
        raise ValueError('Start must use an explicit +08:00 offset')
    ids=list(dict.fromkeys(t['portfolio_id'] for t in manifest['tasks']))
    if len(ids)!=3 or len(manifest['tasks'])!=24:
        raise ValueError('Requires the frozen 3 by 8 task manifest')
    at=lambda seconds:(begin+timedelta(seconds=seconds)).isoformat()
    slots=[]
    for cycle in range(48):
        for i,pid in enumerate(ids):
            matches=[t for t in manifest['tasks'] if t['portfolio_id']==pid and t['snapshot_index']==cycle%8+1]
            if len(matches)!=1:raise ValueError('Frozen task is missing or ambiguous')
            slots.append({'slot_id':'slot-'+str(len(slots)+1).zfill(6),
                'scheduled_at':at(cycle*1800+i*180),'tool_arguments':matches[0]['tool_arguments']})
    plan={'window_id':window_id,'timezone':'Asia/Shanghai','mode':'nexent-price',
        'execution_profile':PROFILE,'gateway':'local-mock','start':at(0),'end':at(86400),
        'cutoff':at(86700),'tolerance_seconds':150,'retry_delay_seconds':60,
        'lease_seconds':300,'max_attempts':2,'window_seconds':60,'max_per_window':3,'slots':slots,
        'data_scope':'frozen-synthetic-eight-snapshots-cycled-six-times',
        'note':'Only explicitly failed simulated pushes retry from the original receipt; unknown tool outcomes never replay.'}
    validate(plan)
    return plan


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest',default='eval/nexent/task_manifest.json')
    p.add_argument('--start',required=True);p.add_argument('--window-id',required=True)
    p.add_argument('--output',required=True)
    a=p.parse_args();plan=make_plan(json.loads(Path(a.manifest).read_text(encoding='utf-8')),a.start,a.window_id)
    with Path(a.output).open('x',encoding='utf-8') as stream:json.dump(plan,stream,ensure_ascii=False,indent=2)
