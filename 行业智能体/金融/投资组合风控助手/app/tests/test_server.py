"""公开 HTTP 接口回归：页面、录入、监控、查询、去重、限频、故障恢复。

用临时端口与临时数据库，全部经 urllib 调真实接口，不直接调 Service。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.gateway import LocalMockGateway  # noqa: E402
from app.server import build_server       # noqa: E402
from app.service import Service           # noqa: E402
from app.store import Store               # noqa: E402

PRICES = {"SIM-STOCK-A": "94.00", "SIM-FUND-B": "1.0900",
          "SIM-STOCK-C": "47.00", "SIM-FUND-D": "2.1300"}


class ServerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        service = Service(Store(Path(self._tmp.name) / "web.db"),
                          LocalMockGateway(), window_seconds=3600,
                          max_per_window=50)
        self.httpd = build_server("127.0.0.1", 0, service)
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown)

    def _shutdown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def call(self, method: str, path: str,
             body: dict | None = None) -> tuple[int, dict | str]:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") \
            if body is not None else None
        request = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read().decode("utf-8")
                ctype = response.headers.get("Content-Type", "")
                return response.status, (json.loads(raw) if "json" in ctype else raw)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_01_index_has_prominent_disclaimer(self):
        code, body = self.call("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn("风险提示仅供参考，不构成投资建议", body)
        self.assertIn("立即监控", body)

    def test_02_health_reports_simulated_gateway(self):
        code, health = self.call("GET", "/api/health")
        self.assertEqual(code, 200)
        self.assertTrue(health["gateway"]["simulated"])
        self.assertFalse(health["gateway"]["real_delivery"])

    def test_08_business_query_and_knowledge_failure_keep_price_signals(self):
        self.call('POST','/api/import',{'source':'demo'})
        from unittest.mock import patch
        with patch.dict('os.environ',{},clear=True):
            code,r=self.call('POST','/api/monitor',{'prices':PRICES,'run_id':'http-evidence',
                'knowledge_queries':[{'portfolio_id':'stock-demo','query':'止损阈值'}]})
        self.assertEqual(code,200);self.assertGreater(r['result']['signal_count'],0)
        code,record=self.call('GET','/api/business-run?run_id=http-evidence')
        self.assertEqual(code,200);self.assertEqual(record['record']['run']['status'],'review_required')
        self.assertTrue(record['record']['components']);self.assertTrue(record['record']['knowledge'])
        self.assertTrue(record['record']['signals']);self.assertTrue(record['record']['pushes'])

    def test_09_integrated_http_local_mode_and_native_config_gate(self):
        from integration.rehearsal import make_plan
        from unittest.mock import patch
        case=make_plan(1)['selected'][0]['case'];case['knowledge_queries']=[]
        with patch.dict('os.environ',{'TOPIC16_INTEGRATION_EVIDENCE_DIR':self._tmp.name},clear=True):
            code,r=self.call('POST','/api/integrated-monitor',{'case':case,'run_id':'integrated-http','mode':'local-rules'})
            self.assertEqual(code,200);self.assertTrue(r['result']['evidence_complete'])
            code,r=self.call('POST','/api/integrated-monitor',{'case':case,'run_id':'native-http','mode':'nexent-integrated'})
            self.assertEqual(code,400)

    def test_03_import_monitor_query_full_loop(self):
        code, imported = self.call("POST", "/api/import", {"source": "demo"})
        self.assertEqual(code, 200)
        self.assertEqual(imported["imported"], 3)
        code, monitored = self.call("POST", "/api/monitor", {"prices": PRICES})
        self.assertEqual(code, 200)
        self.assertGreater(monitored["result"]["signal_count"], 0)
        code, records = self.call("GET", "/api/records")
        self.assertEqual(code, 200)
        self.assertEqual(len({s["portfolio_id"] for s in records["signals"]}), 3)
        self.assertTrue(records["pushes"])
        for push in records["pushes"]:
            self.assertIn("不构成投资建议", push["message"])
            self.assertNotIn("SIM-REVIEWER-01", push["recipient_masked"])

    def test_04_repeated_monitor_is_idempotent_over_http(self):
        self.call("POST", "/api/import", {"source": "demo"})
        _, first = self.call("POST", "/api/monitor", {
            "prices": PRICES, "timestamp": "2026-11-10T09:30:00+08:00"})
        before = self.call("GET", "/api/records")[1]["counts"]
        _, second = self.call("POST", "/api/monitor", {
            "prices": PRICES, "timestamp": "2026-11-10T09:30:00+08:00"})
        after = self.call("GET", "/api/records")[1]["counts"]
        self.assertTrue(all(p["duplicate"] for p in second["result"]["pushes"]))
        self.assertEqual(before["push_records"], after["push_records"])

    def test_05_rate_limit_and_failure_recovery_over_http(self):
        self.call("POST", "/api/import", {"source": "demo"})
        self.call("POST", "/api/config", {"max_per_window": 2})
        _, first = self.call("POST", "/api/monitor", {"prices": PRICES})
        self.assertTrue(any(p["suppressed"] for p in first["result"]["pushes"]))

        self.call("POST", "/api/config", {"max_per_window": 50})
        self.call("POST", "/api/gateway",
                  {"name": "local-mock", "failure_mode": True})
        _, failed = self.call("POST", "/api/monitor", {
            "prices": PRICES, "timestamp": "2026-11-10T12:00:00+08:00"})
        rows = [p for p in failed["result"]["pushes"] if p["status"] == "failed"]
        self.assertTrue(rows)
        self.call("POST", "/api/gateway",
                  {"name": "local-mock", "failure_mode": False})
        event_id = rows[0]["event_id"]
        _, retried = self.call("POST", "/api/retry", {"event_id": event_id})
        self.assertTrue(retried["result"]["retried"])
        self.assertEqual(retried["result"]["status"], "delivered")
        _, again = self.call("POST", "/api/retry", {"event_id": event_id})
        self.assertFalse(again["result"]["retried"])

    def test_06_invalid_input_returns_400_and_unknown_path_404(self):
        code, error = self.call("POST", "/api/portfolios", {"id": "x"})
        self.assertEqual(code, 400)
        self.assertIn("error", error)
        code, _ = self.call("GET", "/api/does-not-exist")
        self.assertEqual(code, 404)

    def test_07_risk_catalog_and_holdings_api(self):
        code, catalog = self.call("GET", "/api/risk-catalog")
        self.assertEqual(code, 200)
        self.assertEqual(len(catalog["risk_catalog"]), 6)
        self.call("POST", "/api/import", {"source": "demo"})
        code, changed = self.call("PUT", "/api/holdings", {
            "portfolio_id": "stock-demo",
            "holding": {"symbol": "SIM-STOCK-A", "cost": "101.00",
                        "quantity": 25, "holding_date": "2026-11-03",
                        "stop_loss": "96.00", "take_profit": "108.00"}})
        self.assertEqual(code, 200)
        stock = next(p for p in changed["portfolio"]["holdings"]
                     if p["symbol"] == "SIM-STOCK-A") \
            if "holdings" in changed["portfolio"] else None
        self.assertIsNotNone(stock)


if __name__ == "__main__":
    unittest.main(verbosity=2)
