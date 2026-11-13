"""Seal the checked-out commit, exact schemas and assets into a portable manifest."""
import argparse
import asyncio
import hashlib
import importlib
from importlib.metadata import version
import json
from pathlib import Path
import subprocess
import sys
from .check import ROOT,SCHEMAS,tool_catalog
from knowledge.client import load_assets

FROZEN=('data/eval_20.json','eval/expected_20.json','eval/nexent/task_manifest.json',
        'data/news_risk/samples_50.json','data/news_risk/dev_eval_samples.json')


async def verify_sdk():
    checks={}
    for module,path in zip(('mcps.portfolio_rules.server','mcps.news_risk.server','mcps.knowledge.server'),SCHEMAS):
        actual=[t.model_dump(mode='json',by_alias=True,exclude_none=True) for t in await importlib.import_module(module).server.list_tools()]
        expected=json.loads((ROOT/path).read_text(encoding='utf-8'))['tools']
        checks[module]=actual==expected
    return checks


def build(verify=False):
    sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    status=subprocess.check_output(['git','status','--porcelain','--untracked-files=all','--','.'],cwd=ROOT,text=True)
    tracked=subprocess.check_output(['git','ls-files','-z','--','.'],cwd=ROOT).decode().split('\0')
    hashes={p:hashlib.sha256((ROOT/p).read_bytes().replace(b'\r\n',b'\n')).hexdigest()
            for p in tracked if p.endswith('.py')}
    assets,_=load_assets()
    return {'schema':'topic16-portable-release-v1','commit':sha,'clean_topic_tree':not status,
            'python':sys.version.split()[0],'python_supported':'>=3.11','sdk_pins':{'mcp':'2.3.0','mcp-types':'2.3.0'},
            'platform':'Nexent v2.8.3','platform_commit':'046d77c8c479d30ccd1ed44cd37f727547798d0f',
            'tools':tool_catalog(),'assets':assets,'source_lf_sha256':hashes,
            'frozen_sha256':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in FROZEN},
            'database_version':4,'migration':'additive only; see integration/README.md',
            'observation':json.loads((ROOT/'eval/nexent/deployment/observation-manifest.json').read_text(encoding='utf-8')),
            'sdk_schema_check':asyncio.run(verify_sdk()) if verify else None,
            'platform_import_proven':False,'formal_acceptance':False}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--verify-sdk',action='store_true');p.add_argument('--output',required=True)
    a=p.parse_args();out=Path(a.output)
    if out.exists():p.error('output exists')
    result=build(a.verify_sdk);out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return 0 if result['clean_topic_tree'] and all((result['sdk_schema_check'] or {}).values()) else 2


if __name__=='__main__':raise SystemExit(main())
