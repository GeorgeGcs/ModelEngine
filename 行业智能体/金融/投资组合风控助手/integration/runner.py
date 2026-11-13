"""Reuse app persistence and push ownership for local or fully native business runs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
from app.service import Service
from app.store import Store
from app.scheduler import instant
from app.rules_ext import _signal,news_signals,RISK_CATALOG
from eval.nexent.nexent_client import PlatformConfig
from knowledge.client import load_assets,digest
from .platform import invoke


def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def validate_case(case):
    from datetime import datetime
    if not isinstance(case,dict) or not isinstance(case.get('portfolios'),list) or not case['portfolios']:
        raise ValueError('case requires portfolios')
    snapshot=case.get('snapshot',{})
    if datetime.fromisoformat(snapshot['timestamp'].replace('Z','+00:00')).tzinfo is None:
        raise ValueError('snapshot requires timezone')
    for portfolio in case['portfolios']:
        for h in portfolio['holdings']:
            if h['symbol'] not in snapshot['prices']:raise ValueError('missing holding price')
    if len({p['id'] for p in case['portfolios']})!=len(case['portfolios']):raise ValueError('duplicate portfolio')


def validate_news(result,news):
    if (not isinstance(result,dict) or result.get('symbol')!=news['symbol'] or
            result.get('risk_verdict') not in ('risk','no_risk') or
            result.get('level') not in ('高','中','低','无') or
            not isinstance(result.get('review_action'),str) or not result['review_action'] or
            not isinstance(result.get('evidence'),str) or
            result['evidence'] not in news['text'] or
            not {'profile','evidence_status','reason_codes'}<=result.keys()):
        raise ValueError('invalid news business result')


def validate_knowledge(record,store,run_id,task_key,query):
    if (not isinstance(record,dict) or record.get('status') not in ('hit','no_match') or
            record.get('run_id')!=run_id or record.get('task_key')!=task_key or record.get('query')!=query):
        raise ValueError('invalid knowledge ownership')
    saved=[r for r in store.knowledge_records(run_id) if r['retrieval_id']==record.get('retrieval_id')]
    if saved!=[record]:raise ValueError('knowledge result differs from shared persistent record')
    _,assets=load_assets()
    citations=record.get('citations')
    if not isinstance(citations,list) or (record['status']=='hit')!=bool(citations):raise ValueError('invalid citation status')
    for c in citations:
        d=assets[(c['doc_id'],c['version'])];q=c['quote']
        if (c['document_sha256']!=d['sha256'] or digest(q.encode())!=c['quote_sha256'] or
                type(c['start_char']) is not int or type(c['end_char']) is not int or
                d['text'][c['start_char']:c['end_char']]!=q):raise ValueError('invalid citation provenance')


def execute(store,case,run_id,mode,evidence_dir,*,native_invoke=invoke,on_verified=None):
    validate_case(case)
    with store.connection() as conn:
        if conn.execute('SELECT 1 FROM monitor_runs WHERE run_id=?',(run_id,)).fetchone():
            raise ValueError('run_id already exists; do not overwrite')
    if mode not in ('local-rules','nexent-integrated'):raise ValueError('unsupported execution mode')
    config=None
    if mode=='nexent-integrated':
        for p in case['portfolios']:
            if not any(n.get('symbol') in {h['symbol'] for h in p['holdings']} for n in case.get('news_items',[])) or not any(
                    q.get('portfolio_id')==p['id'] for q in case.get('knowledge_queries',[])):
                raise ValueError('integrated mode requires news and knowledge input for every portfolio')
        config=PlatformConfig.from_env()
        if config.missing() or not config.execution_records:raise ValueError('native mode requires platform and receipt configuration')
        if not os.environ.get('TOPIC16_KB_DB') or Path(os.environ['TOPIC16_KB_DB']).resolve()!=store.path.resolve():
            raise ValueError('native integration requires the shared private knowledge database')
    service=Service(store,max_per_window=100)
    # Demo runner always uses local-mock. Existing app/scheduler keep explicit webhook configuration.
    for p in case['portfolios']:service.upsert_portfolio(p)
    if mode=='local-rules':
        return service.monitor(prices=case['snapshot']['prices'],timestamp=case['snapshot']['timestamp'],
            frozen_portfolios=case['portfolios'],run_id=run_id,news_items=case.get('news_items'),
            knowledge_queries=case.get('knowledge_queries'),market=case.get('market'))
    directory=Path(evidence_dir)
    directory.mkdir(parents=True,exist_ok=False,mode=0o700)
    store.open_run(run_id,{'execution_mode':mode,'input_sha256':digest(canonical(case).encode()),'gateway':'local-mock'})
    store.save_component(run_id,'inputs','recorded',case)
    started=time.perf_counter();failures=[];signals=[];pushes=[];timings=[];news_records=[];verified_retrievals=set();scopes=set()

    def call(key,tool,args,task_key):
        offset_ms=(time.perf_counter()-started)*1000
        try:
            result=native_invoke(config,tool=tool,arguments=args,run_id=run_id,task_key=task_key)
        except Exception as exc:
            result={'scope':'nexent-native-agent','evidence_status':'insufficient','error_type':type(exc).__name__,'result':None}
        raw=canonical(result).encode();path=directory/(hashlib.sha256(key.encode()).hexdigest()+'.json')
        scopes.add(result.get('scope','unverified'))
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as f:f.write(raw)
        payload={'tool_name':tool,'planned_arguments':args,'response':result,
                 'artifact':{'file':path.name,'sha256':digest(raw)}}
        store.save_component(run_id,key,result['evidence_status'],payload)
        if result.get('timing'):timings.append(dict(component=key,start_offset_ms=round(offset_ms,3),**result['timing']))
        if result['evidence_status']!='verified':failures.append(key);return None
        return result

    for portfolio in case['portfolios']:
        snapshot=case['snapshot'];task=portfolio['id']+'|'+snapshot['timestamp']
        result=call('price:'+portfolio['id'],'evaluate_portfolio_snapshot',
                    {'portfolio':portfolio,'snapshot':snapshot},task)
        if result is not None:
            try:
                raw_signals=result['result']
                if not isinstance(raw_signals,list):raise ValueError('price result must be an explicit array')
                for s in raw_signals:
                    if (not isinstance(s,dict) or not {'event_id','portfolio_id','timestamp','symbol','rule','price','threshold'}<=s.keys()
                            or s['portfolio_id']!=portfolio['id'] or instant(s['timestamp'])!=instant(snapshot['timestamp'])
                            or s['symbol'] not in {h['symbol'] for h in portfolio['holdings']}
                            or s['rule'] not in ('stop_loss','take_profit')):raise ValueError('price signal scope')
                    old=store.get_signal(s['event_id'])
                    if old and old['detail'].get('raw_signal')!=s:raise ValueError('signal conflict')
                if len({s['event_id'] for s in raw_signals})!=len(raw_signals):raise ValueError('duplicate native signals')
                for s in raw_signals:
                    category=next(c for c in RISK_CATALOG if c['id']==s['rule'])
                    row=_signal(portfolio,s['timestamp'],s['symbol'],s['rule'],category['name'],canonical(s),
                        category['level'],category['review_action'],{'scope':'nexent-native-agent',
                        'raw_signal':s,'tool_evidence':result['tool_evidence']})
                    row['event_id']=s['event_id'];signals.append(row)
                if on_verified:
                    on_verified({'type':'price_verified','run_id':run_id,'portfolio_id':portfolio['id'],
                                 'artifact_sha256':digest(canonical(result).encode()),
                                 'signals':[{k:s[k] for k in ('symbol','rule','price','threshold')} for s in raw_signals]})
            except (KeyError,TypeError,ValueError):failures.append('price-business:'+portfolio['id'])
        for i,item in enumerate(case.get('news_items',[])):
            if item['symbol'] not in {h['symbol'] for h in portfolio['holdings']}:continue
            news={k:item[k] for k in ('symbol','text','sample_id','published_at','source') if k in item}
            entities=item.get('entity_profiles') or {item['symbol']:{'canonical':item['name']}}
            key='news:'+portfolio['id']+':'+str(i)
            result=call(key,'assess_news_risk_v2',{'news':news,'entities':entities},task+'|news:'+str(i))
            if result is None:continue
            try:validate_news(result['result'],news)
            except (ValueError,KeyError,TypeError):failures.append(key+':business');continue
            record={'scope':'nexent-native-agent','status':'success','news':news,'entities':entities,
                    'result':result['result'],'tool_evidence':result['tool_evidence']}
            news_records.append(record);signals.extend(news_signals(portfolio,snapshot['timestamp'],[record]))
        for i,q in enumerate(case.get('knowledge_queries',[])):
            if q['portfolio_id']!=portfolio['id']:continue
            key='knowledge:'+portfolio['id']+':'+str(i);kt=task+'|knowledge:'+str(i)
            result=call(key,'search_portfolio_knowledge',{'query':q['query'],'run_id':run_id,'task_key':kt},kt)
            if result is not None:
                try:
                    validate_knowledge(result['result'],store,run_id,kt,q['query'])
                    verified_retrievals.add(result['result']['retrieval_id'])
                except (ValueError,KeyError,TypeError):failures.append(key+':business')
    references=store.knowledge_records(run_id)
    for row in signals:
        row['run_id']=run_id
        selected=[r for r in references if r['retrieval_id'] in verified_retrievals and r['task_key'].startswith(row['portfolio_id']+'|')]
        row['detail']['knowledge_retrieval_ids']=[r['retrieval_id'] for r in selected]
        row['detail']['knowledge_statuses']=[r['status'] for r in selected]
        row['detail']['knowledge_role']='supporting-reference; does not override original input evidence'
        store.insert_signal(row);pushes.append(service._push(row))
    status='review_required' if failures or any(r['status']!='hit' for r in references) else 'completed'
    store.finish_run(run_id,status,len(signals),sum(p['attempted'] for p in pushes))
    summary={'run_id':run_id,'execution_mode':mode,'status':status,'evidence_complete':not failures,
             'scope':'nexent-native-agent' if scopes=={'nexent-native-agent'} else 'mixed-or-mock',
             'component_scopes':sorted(scopes),
             'failures':failures,'signal_count':len(signals),'pushes':pushes,'news':news_records,
             'knowledge':references,'timings':timings,'workflow_ms':round((time.perf_counter()-started)*1000,3),
             'real_wechat_delivery_proven':False}
    visible=[t['start_offset_ms']+t['first_visible_ms'] for t in timings if t.get('first_visible_ms') is not None]
    summary['workflow_first_visible_ms']=round(min(visible),3) if visible else None
    store.save_component(run_id,'summary',status,summary)
    return summary


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--case',required=True);p.add_argument('--db',required=True);p.add_argument('--run-id',required=True)
    p.add_argument('--mode',choices=['local-rules','nexent-integrated'],required=True)
    p.add_argument('--evidence-dir',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();out=Path(a.output)
    if out.exists():p.error('output exists; preserve original evidence')
    try:
        result=execute(Store(a.db),json.loads(Path(a.case).read_text(encoding='utf-8')),a.run_id,a.mode,a.evidence_dir)
        code=0 if result.get('evidence_complete',not result.get('knowledge_review_required',False)) else 2
    except Exception as exc:result={'status':'failed','error_type':type(exc).__name__};code=2
    out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'exit_code':code,'run_id':a.run_id,'status':result.get('status','local-completed')}));return code


if __name__=='__main__':raise SystemExit(main())
