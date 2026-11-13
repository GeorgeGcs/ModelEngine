"""Preselect one case per portfolio type with a fixed seed, then execute each three times."""
import argparse
import hashlib
import json
from pathlib import Path
import random
import subprocess
import uuid
from app.store import Store
from .runner import execute,canonical

CASES=Path(__file__).with_name('demo_cases.json')


def make_plan(seed):
    pool=json.loads(CASES.read_text(encoding='utf-8'))['cases'];rng=random.Random(seed)
    selected=[rng.choice([c for c in pool if c['portfolios'][0]['type']==kind]) for kind in ('股票','基金','混合')]
    return {'schema':'topic16-rehearsal-plan-v1','scope':'synthetic-demo-not-formal-eval','seed':seed,
            'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=CASES.parent,text=True).strip(),
            'pool_sha256':hashlib.sha256(CASES.read_bytes()).hexdigest(),'repetitions':3,
            'selected':[{'case':c,'input_sha256':hashlib.sha256(canonical(c).encode()).hexdigest()} for c in selected]}


def directions(store,run_id):
    record=store.business_record(run_id)
    signals=sorted((s['portfolio_id'],s['symbol'],s['risk_type'],s['level']) for s in record['signals'])
    news=[]
    for c in record['components']:
        if not c['key'].startswith('news:'):continue
        payload=c['payload']
        if isinstance(payload,list):news.extend((r['news']['symbol'],(r.get('result') or {}).get('risk_verdict'),r['status']) for r in payload)
        else:
            r=payload.get('response',{});v=r.get('result') or {}
            news.append((v.get('symbol'),v.get('risk_verdict'),r.get('evidence_status')))
    return {'signals':signals,'news':sorted(news,key=str),
            'knowledge':sorted(r['status'] for r in record['knowledge'])}


def run_plan(plan,store,mode,directory,*,run=execute):
    if plan!=make_plan(plan['seed']):raise ValueError('plan changed after selection')
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=False)
    results=[];all_ok=True;scopes=set()
    for selected in plan['selected']:
        repeats=[]
        for n in range(plan['repetitions']):
            run_id='rehearsal-'+uuid.uuid4().hex
            try:
                summary=run(store,selected['case'],run_id,mode,directory/run_id)
                scopes.add(summary.get('scope',summary.get('execution_mode','unverified')))
                ok=summary.get('evidence_complete',not (summary.get('news_review_required') or summary.get('knowledge_review_required')))
                repeats.append({'run_id':run_id,'ok':ok,'directions':directions(store,run_id),
                                'timings':summary.get('timings',[]),'workflow_ms':summary.get('workflow_ms'),
                                'workflow_first_visible_ms':summary.get('workflow_first_visible_ms'),
                                'first_visible_policy':'native final_answer.content only; local mode unavailable'})
            except Exception as exc:repeats.append({'run_id':run_id,'ok':False,'error_type':type(exc).__name__})
        consistent=all(r['ok'] for r in repeats) and len({canonical(r.get('directions')) for r in repeats})==1
        all_ok=all_ok and consistent
        results.append({'case_id':selected['case']['case_id'],'input_sha256':selected['input_sha256'],
                        'direction_consistent':consistent,'repeats':repeats})
    return {'schema':'topic16-rehearsal-result-v1','mode':mode,'development_run':scopes!={'nexent-native-agent'},'scopes':sorted(scopes),
            'platform_import_proven':False,'formal_acceptance':False,'all_consistent':all_ok,
            'source_commit':plan['source_commit'],'plan_sha256':hashlib.sha256(canonical(plan).encode()).hexdigest(),
            'seed':plan['seed'],'cases':results,'runs':sum(len(x['repeats']) for x in results)}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['plan','run'])
    p.add_argument('--seed',type=int,default=161118);p.add_argument('--plan');p.add_argument('--db')
    p.add_argument('--mode',choices=['local-rules','nexent-integrated']);p.add_argument('--evidence-dir')
    p.add_argument('--output',required=True);a=p.parse_args();out=Path(a.output)
    if out.exists():p.error('output exists; preserve original evidence')
    if a.action=='plan':result=make_plan(a.seed)
    else:
        if not all((a.plan,a.db,a.mode,a.evidence_dir)):p.error('run needs plan/db/mode/evidence-dir')
        result=run_plan(json.loads(Path(a.plan).read_text(encoding='utf-8')),Store(a.db),a.mode,a.evidence_dir)
    out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return 0 if result.get('all_consistent',True) else 2


if __name__=='__main__':raise SystemExit(main())
