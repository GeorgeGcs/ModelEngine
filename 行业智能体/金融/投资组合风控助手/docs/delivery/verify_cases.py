"""Exercise the shipped synthetic acceptance cases against a loopback Nexent app."""
import argparse
import hashlib
import json
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4


HERE = Path(__file__).resolve().parent


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def call(origin, route, body=None):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    request = Request(origin + route, data=data, headers={'Content-Type': 'application/json'})
    try:
        with urlopen(request, timeout=65) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.load(exc)


def run_case(origin, definition, prefix):
    fixture = load(HERE.parents[1] / 'integration/cases' / (definition['base'] + '.json'))
    case_id = definition['id']
    run_id = prefix + '-' + case_id
    portfolio, = fixture['portfolios']
    portfolio['id'] = run_id
    fixture['case_id'] = case_id
    for item in fixture['knowledge_queries']:
        item['portfolio_id'] = portfolio['id']
    if 'prices' in definition:
        fixture['snapshot']['prices'] = definition['prices']
    serialized = json.dumps(fixture, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    started = time.perf_counter()
    status, envelope = call(origin, '/api/integrated-monitor',
                            {'case': fixture, 'mode': 'nexent-integrated', 'run_id': run_id})
    result = {'case_id': case_id, 'run_id': run_id, 'http': status,
              'input_sha256': hashlib.sha256(serialized).hexdigest(),
              'elapsed_ms': round((time.perf_counter() - started) * 1000, 3)}
    status_query = '/api/business-run?' + urlencode({'run_id': run_id})
    if 'http_error' in definition:
        absent, lookup = call(origin, status_query)
        result['rejected_before_persistence'] = (status == definition['http_error']
                                                 and lookup.get('ok') is False
                                                 and lookup.get('error') == 'run_id 不存在')
        result['absent_lookup_http'] = absent
        result['passed'] = result['rejected_before_persistence']
        return result
    if status != 200 or envelope.get('ok') is not True:
        result['passed'] = False
        return result
    record_status, saved = call(origin, status_query)
    if record_status != 200 or saved.get('ok') is not True:
        result['passed'] = False
        return result
    record = saved['record']
    components = [item['payload'] for item in record['components'] if 'tool_name' in item['payload']]
    outputs = {item['tool_name']: item['response'] for item in components}
    price = outputs.get('evaluate_portfolio_snapshot', {}).get('result')
    news = outputs.get('assess_news_risk_v2', {}).get('result') or {}
    knowledge = outputs.get('search_portfolio_knowledge', {}).get('result') or {}
    result.update(price_rules={row['symbol']: row['rule'] for row in price} if isinstance(price, list) else None,
                  news=news.get('risk_verdict'), knowledge=knowledge.get('status'),
                  push_count=len(record['pushes']),
                  simulated_pushes=all(p['simulated'] and p['gateway_id']=='local-mock' for p in record['pushes']),
                  native_receipts_verified=(len(components)==3 and len(outputs)==3
                                            and all(item['evidence_status']=='verified'
                                                    and item.get('dispatch_mode')=='published-agent-workflow'
                                                    for item in outputs.values())))
    result['passed'] = (envelope['result']['evidence_complete'] and result['native_receipts_verified']
                        and result['simulated_pushes']
                        and all(result[key] == definition[key]
                                for key in ['price_rules','news','knowledge','push_count']))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8765')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    origin = args.base_url.rstrip('/')
    url = urlsplit(origin)
    if url.scheme != 'http' or url.hostname != '127.0.0.1' or url.path or url.query or url.fragment or url.username:
        parser.error('use a loopback-only app origin')
    if args.output.exists():
        parser.error('output already exists')
    prefix = 'acceptance-' + uuid4().hex[:12]
    definitions = load(HERE / 'cases.json')['cases']
    rows = [run_case(origin, item, prefix) for item in definitions]
    report = {'scope':'native Nexent and simulated push', 'passed':sum(row['passed'] for row in rows),
              'total':len(rows), 'cases':rows}
    with args.output.open('x', encoding='utf-8') as handle:
        json.dump(report,handle,ensure_ascii=False,indent=2)
        handle.write('\n')
    print(json.dumps({key:report[key] for key in ['scope','passed','total']},ensure_ascii=False))
    return 0 if report['passed'] == report['total'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
