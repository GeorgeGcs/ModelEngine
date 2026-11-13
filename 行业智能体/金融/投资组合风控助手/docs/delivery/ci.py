"""Project-scoped CI entry point. Requires the pinned project environment.

Exits nonzero for failure, zero tests, missing SDK, or skipped project tests.
Patched-platform observation runs separately inside its Linux runtime.
"""
import argparse
from datetime import datetime,timezone
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]
SUITES=['tests','app/tests','integration/tests','knowledge/tests','eval/nexent/tests',
        'eval/news_risk/tests','mcps/portfolio_rules/tests','mcps/news_risk/tests',
        'mcps/knowledge/tests','mcps/wechat/tests','docs/delivery/observation']


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True);a=p.parse_args()
    output=Path(a.output).resolve();output.mkdir(parents=True,exist_ok=False)
    sdk={name:version(name) for name in ['mcp','mcp-types']}
    if any(v!='2.3.0' for v in sdk.values()):raise RuntimeError('Requires mcp==2.3.0 and mcp-types==2.3.0')
    results=[]
    for i,suite in enumerate(SUITES):
        proc=subprocess.run([sys.executable,'-B','-m','unittest','discover','-s',suite,'-v'],cwd=ROOT,
            env=dict(os.environ,PYTHONPATH=str(ROOT),PYTHONUTF8='1',PYTHONIOENCODING='utf-8'),
            capture_output=True,text=True,encoding='utf-8',errors='replace')
        log=proc.stdout+'\n'+proc.stderr;(output/f'{i+1:02d}.log').write_text(log,encoding='utf-8')
        count=re.search(r'Ran (\d+) tests?',log);skip=re.search(r'skipped=(\d+)',log)
        row={'suite':suite,'tests':int(count[1]) if count else 0,'skip':int(skip[1]) if skip else 0,'exit':proc.returncode}
        row['pass']=row['tests']>0 and row['skip']==0 and row['exit']==0
        results.append(row);print(json.dumps(row),flush=True)
    report={'checked_at':datetime.now(timezone.utc).isoformat(),'sdk':sdk,'suites':results,
        'tests':sum(r['tests'] for r in results),'skip':sum(r['skip'] for r in results),'pass':all(r['pass'] for r in results),
        'patched_runtime_observation':'separate Linux job required; not counted here',
        'hosted_ci_job_executed':False}
    (output/'results.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return 0 if report['pass'] else 1


if __name__=='__main__':raise SystemExit(main())
