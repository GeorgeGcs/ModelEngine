"""Measure a 1,000-holding bulk import through the app's public endpoint."""
import argparse
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import time
from urllib.request import Request, urlopen


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.server import build_server
from app.service import Service
from app.store import Store


def run():
    portfolios = [
        {'id': f'batch-{i:03}', 'name': f'模拟组合 {i:03}', 'type': '股票',
         'holdings': [{'symbol': f'SIM-{i:03}-{j:02}', 'cost': '100.00', 'quantity': 1,
                       'holding_date': '2026-11-02', 'stop_loss': '95.00', 'take_profit': '106.00'}
                      for j in range(10)]}
        for i in range(100)
    ]
    with TemporaryDirectory(prefix='topic16-batch-') as directory:
        db = Path(directory) / 'batch.sqlite'
        server = build_server('127.0.0.1', 0, Service(Store(db)))
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            origin = f'http://127.0.0.1:{server.server_port}'
            body = json.dumps({'portfolios': portfolios}, ensure_ascii=False).encode()
            request = Request(origin + '/api/import', data=body, headers={'Content-Type': 'application/json'})
            start = time.perf_counter()
            with urlopen(request, timeout=1800) as response:
                http = response.status
                result = json.load(response)
            imported_seconds = time.perf_counter() - start
            stored = Store(db).list_portfolios()
        finally:
            server.shutdown()
            server.server_close()
            worker.join()
    holdings = [item['symbol'] for portfolio in stored for item in portfolio['holdings']]
    passed = (http == 200 and result['ok'] and result['imported'] == 100
              and len(stored) == 100 and len(holdings) == len(set(holdings)) == 1000
              and imported_seconds <= 1800)
    return {'scope': 'application bulk holding import; no monitoring or push',
            'http': http, 'portfolios': len(stored), 'holdings': len(holdings),
            'unique_symbols': len(set(holdings)), 'elapsed_seconds': round(imported_seconds, 3),
            'limit_seconds': 1800, 'passed': passed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output already exists')
    result = run()
    with args.output.open('x', encoding='utf-8') as handle:
        json.dump(result,handle,ensure_ascii=False,indent=2)
        handle.write('\n')
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
