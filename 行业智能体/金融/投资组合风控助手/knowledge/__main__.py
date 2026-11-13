"""Private native KB import/query command; never prints credentials or native paths."""
import argparse
import json
from pathlib import Path
from app.store import Store
from .client import KnowledgeClient, KnowledgeError, load_assets


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['assets','import','query','records'])
    p.add_argument('--db',required=True)
    p.add_argument('--run-id');p.add_argument('--task-key');p.add_argument('--query')
    p.add_argument('--output',required=True,help='Private output file; query text and quotes remain private')
    a=p.parse_args();result={};code=0
    out=Path(a.output)
    if out.exists():p.error('output already exists; use a new private filename')
    try:
        store=Store(a.db)
        if a.action=='assets':
            manifest,_=load_assets();result={'scope':'local-asset-check','manifest':manifest}
        elif a.action=='records':
            if not a.run_id:p.error('records requires --run-id')
            result={'scope':'persisted-retrievals','records':store.knowledge_records(a.run_id)}
        else:
            client=KnowledgeClient.from_env(store)
            if a.action=='import':
                _,assets=load_assets()
                result={'scope':'nexent-native-http','documents':[client.import_asset(d) for d in assets.values()]}
                code=0 if all(d['status']=='verified' for d in result['documents']) else 2
            else:
                result=client.retrieve(run_id=a.run_id,task_key=a.task_key,query=a.query)
                code=0 if result['status'] in ('hit','no_match') else 2
    except (KnowledgeError,ValueError,OSError) as exc:
        result={'status':getattr(exc,'status','configuration_error')};code=2
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'exit_code':code,'status':result.get('status','recorded')},ensure_ascii=False))
    return code


if __name__=='__main__':raise SystemExit(main())
