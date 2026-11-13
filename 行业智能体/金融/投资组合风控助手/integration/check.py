"""Portable config/asset/schema check. Never asserts actual platform import success."""
import argparse
import hashlib
from importlib.metadata import version
import json
import os
import re
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
from knowledge.client import KnowledgeClient,KnowledgeError,load_assets
from eval.nexent.nexent_client import PlatformConfig,probe

ROOT=Path(__file__).resolve().parents[1]
SCHEMAS=['mcps/portfolio_rules/schema.json','mcps/news_risk/schema.json','mcps/knowledge/schema.json']
WECHAT_SCHEMA='mcps/wechat/schema.json'


def tool_catalog(*, wechat=False):
    schemas = [*SCHEMAS, WECHAT_SCHEMA] if wechat else SCHEMAS
    return [dict(tool=t,schema_file=p,schema_sha256=hashlib.sha256((ROOT/p).read_bytes()).hexdigest())
            for p in schemas for t in json.loads((ROOT/p).read_text(encoding='utf-8'))['tools']]


def check_inventory(inventory, *, wechat=False):
    expected={r['tool']['name']:r['tool']['inputSchema'] for r in tool_catalog(wechat=wechat) if r['tool']['name']!='assess_news_risk'}
    actual={t['name']:t['inputSchema'] for t in inventory.get('tools',[])}
    return {'exact_tools':actual==expected and len(actual)==len(inventory.get('tools',[])),
            'published_declared':inventory.get('published') is True,
            'platform_version':inventory.get('platform_version')=='v2.8.3',
            'knowledge_permission_declared':inventory.get('knowledge',{}).get('permission_verified') is True,
            'native_inventory_provenance_declared':inventory.get('provenance',{}).get('kind') in ('platform-export','platform-published-config') and
                bool(re.fullmatch('[0-9a-f]{64}',inventory.get('provenance',{}).get('sha256','')))}


def readiness(*,mode,db,ports,inventory=None,live_probe=False,wechat=False):
    checks={'python':sys.version_info>=(3,11)};errors={}
    try:load_assets();checks['asset_hashes']=True
    except Exception:checks['asset_hashes']=False
    for name in ('mcp','mcp-types'):
        try:checks[name]=version(name)=='2.3.0'
        except Exception:checks[name]=False
    if mode=='local-rules':checks.pop('mcp');checks.pop('mcp-types')
    for port in ports:
        try:
            with socket.socket() as s:
                if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):s.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
                s.bind(('127.0.0.1',port))
            checks['port:'+str(port)]=True
        except OSError:checks['port:'+str(port)]=False
    checks['clean_business_state']=True
    if Path(db).exists():
        try:
            conn=sqlite3.connect(Path(db).resolve().as_uri()+'?mode=ro',uri=True)
            try:
                for name in ('monitor_runs','signals','push_records'):
                    if conn.execute('SELECT 1 FROM sqlite_master WHERE name=?',(name,)).fetchone():
                        if conn.execute('SELECT count(*) FROM '+name).fetchone()[0]:checks['clean_business_state']=False
            finally:conn.close()
        except sqlite3.Error:checks['clean_business_state']=False
    platform_summary=None
    if mode=='nexent-integrated':
        cfg=PlatformConfig.from_env();checks['native_config']=not cfg.missing() and bool(cfg.execution_records)
        checks['receipt_source_exists']=bool(cfg.execution_records and Path(cfg.execution_records).is_file())
        checks['shared_db']=bool(os.environ.get('TOPIC16_KB_DB')) and Path(os.environ['TOPIC16_KB_DB']).resolve()==Path(db).resolve()
        checks.update(check_inventory(inventory or {}, wechat=wechat))
        checks['live_platform_probe']=False;checks['live_knowledge_permission']=False
        if live_probe and checks['native_config']:
            platform_summary=probe(cfg);checks['live_platform_probe']=platform_summary.get('status')=='reachable'
            try:
                client=KnowledgeClient.from_env(None)
                if client.api is None or not client.index or client.role!='risk_reader':raise KnowledgeError('configuration_error')
                r=client.api.request('POST','/nb/v1/knowledge/indices/'+client.index+'/chunks?page=1&page_size=1')
                checks['live_knowledge_permission']=r.get('status')=='success' and bool(r.get('chunks'))
            except Exception as exc:errors['knowledge_probe']=getattr(exc,'status',type(exc).__name__)
    try:sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    except Exception:sha=None
    return {'schema':'topic16-config-check-v1','commit':sha,'mode':mode,'checks':checks,'errors':errors,
            'ready_for_verification':all(checks.values()),'platform_import_proven':False,
            'permission_inventory_is_a_declaration':True,'platform_probe':platform_summary}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--mode',choices=['local-rules','nexent-integrated'],required=True)
    p.add_argument('--db',required=True);p.add_argument('--ports',default='8765,8768,8770,8771')
    p.add_argument('--inventory');p.add_argument('--probe-platform',action='store_true');p.add_argument('--wechat',action='store_true');p.add_argument('--output',required=True)
    a=p.parse_args();out=Path(a.output)
    if out.exists():p.error('output exists')
    result=readiness(mode=a.mode,db=a.db,ports=[int(v) for v in a.ports.split(',') if v],
        inventory=json.loads(Path(a.inventory).read_text(encoding='utf-8')) if a.inventory else None,live_probe=a.probe_platform,wechat=a.wechat)
    out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'ready_for_verification':result['ready_for_verification'],'failed':[k for k,v in result['checks'].items() if not v]}))
    return 0 if result['ready_for_verification'] else 2


if __name__=='__main__':raise SystemExit(main())
