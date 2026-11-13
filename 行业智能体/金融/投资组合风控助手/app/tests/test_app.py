"""本地演示闭环回归：录入/重启查询、去重、限频、故障恢复、扩展风险类别。

全部使用临时 SQLite 与虚构模拟数据，不写入仓库，不发起真实外部请求。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.gateway import LocalMockGateway, build_gateway  # noqa: E402
from app.rules_ext import RISK_CATALOG, build_signals     # noqa: E402
from app.service import Service                          # noqa: E402
from app.store import Store                              # noqa: E402

DEMO = ROOT / "data" / "demo.json"
PRICES = {"SIM-STOCK-A": "100.00", "SIM-FUND-B": "1.0000",
          "SIM-STOCK-C": "50.00", "SIM-FUND-D": "2.0000"}
T1 = "2026-11-10T09:30:00+08:00"
T2 = "2026-11-10T10:30:00+08:00"


class AppCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "local_app.db"
        self.gateway = LocalMockGateway()
        self.service = Service(Store(self.db), self.gateway)

    def seed(self):
        return self.service.import_demo_file(DEMO)

    def portfolio(self, portfolio_id: str) -> dict:
        return next(p for p in self.service.portfolios()
                    if p["id"] == portfolio_id)

    def test_01_import_three_portfolios_and_restart_query(self):
        rows = self.seed()
        self.assertEqual(len(rows), 3)
        self.assertEqual({r["type"] for r in rows}, {"股票", "基金", "混合"})
        self.assertEqual(sum(len(r["holdings"]) for r in rows), 4)
        # 进程重启：重新打开同一个 DB，数据仍可查。
        restarted = Service(Store(self.db), LocalMockGateway())
        self.assertEqual(len(restarted.portfolios()), 3)
        self.assertEqual(restarted.counts()["signals"], 0)
        self.assertEqual(restarted.counts()["push_records"], 0)

    def test_02_modify_holding_persists(self):
        self.seed()
        self.service.set_holding("stock-demo", {
            "symbol": "SIM-STOCK-A", "cost": "101.00", "quantity": 25,
            "holding_date": "2026-11-03", "stop_loss": "96.00",
            "take_profit": "108.00"})
        stock = next(p for p in self.service.portfolios() if p["id"] == "stock-demo")
        holding = stock["holdings"][0]
        self.assertEqual(holding["quantity"], 25)
        self.assertEqual(holding["take_profit"], "108.00")

    def test_03_invalid_holding_is_rejected(self):
        self.seed()
        with self.assertRaises(ValueError):
            self.service.set_holding("stock-demo", {
                "symbol": "SIM-STOCK-A", "cost": "100", "quantity": 10,
                "holding_date": "2026-11-02", "stop_loss": "110",
                "take_profit": "105"})

    def test_04_monitor_produces_signals_with_required_fields(self):
        self.seed()
        result = self.service.monitor(
            prices={"SIM-STOCK-A": "94.00", "SIM-FUND-B": "1.0900",
                    "SIM-STOCK-C": "47.00", "SIM-FUND-D": "2.1300"},
            timestamp=T1)
        self.assertGreater(result["signal_count"], 0)
        signals = self.service.signals(run_id=result["run_id"])
        for signal in signals:
            for key in ("portfolio_id", "risk_type", "trigger_time", "evidence",
                        "level", "review_action"):
                self.assertTrue(signal[key], key)
        self.assertTrue(all(s["disclaimer"] if "disclaimer" in s else True
                            for s in signals))

    def test_05_same_event_repeated_monitor_sends_only_once(self):
        self.seed()
        prices = {"SIM-STOCK-A": "94.00", "SIM-FUND-B": "1.0900",
                  "SIM-STOCK-C": "47.00", "SIM-FUND-D": "2.1300"}
        first = self.service.monitor(prices=prices, timestamp=T1)
        before = self.service.counts()
        second = self.service.monitor(prices=prices, timestamp=T1)
        self.assertEqual(second["signal_count"], first["signal_count"])
        self.assertTrue(all(not s["signal_created"] for s in second["signals"]))
        self.assertTrue(all(p["duplicate"] and not p["attempted"]
                            for p in second["pushes"]))
        self.assertEqual(self.service.counts()["signals"], before["signals"])
        self.assertEqual(self.service.counts()["push_records"],
                         before["push_records"])

    def test_06_different_events_are_rate_limited(self):
        service = Service(Store(self.db), LocalMockGateway(),
                          window_seconds=60, max_per_window=2)
        service.import_demo_file(DEMO)
        prices = {"SIM-STOCK-A": "94.00", "SIM-FUND-B": "1.0000",
                  "SIM-STOCK-C": "50.00", "SIM-FUND-D": "2.0000"}
        result = service.monitor(prices=prices, timestamp=T1)
        self.assertGreater(result["push_count"], 2)
        statuses = [p["status"] for p in result["pushes"]]
        self.assertIn("delivered", statuses)
        self.assertIn("suppressed", statuses)
        rows = service.pushes()
        self.assertTrue(all(r["recipient_masked"] != service.recipient_id
                            for r in rows))
        self.assertTrue(service.gateway.describe()["simulated"])
        self.assertFalse(service.gateway.describe()["real_delivery"])

    def test_07_failure_recovery_does_not_duplicate_delivery(self):
        gateway = LocalMockGateway(failure_mode=True)
        service = Service(Store(self.db), gateway, max_per_window=99)
        service.import_demo_file(DEMO)
        result = service.monitor(prices=PRICES, timestamp=T1,
                                 market={"daily_change_pct":
                                         {"SIM-STOCK-A": "8.0"}})
        failed = [p for p in result["pushes"] if p["status"] == "failed"]
        self.assertTrue(failed)
        before = service.counts()["push_records"]
        event_id = failed[0]["event_id"]

        gateway.set_failure_mode(False)
        retried = service.retry_push(event_id)
        self.assertTrue(retried["retried"])
        self.assertEqual(retried["status"], "delivered")
        self.assertEqual(service.counts()["push_records"], before)

        again = service.retry_push(event_id)
        self.assertFalse(again["retried"])
        self.assertEqual(service.counts()["delivered"], 1)

    def test_08_daily_move_concentration_liquidity_are_deterministic(self):
        self.seed()
        portfolio = self.portfolio("mixed-demo")  # 混合组合，两项持仓
        prices = {"SIM-STOCK-C": "50.00", "SIM-FUND-D": "2.0000"}
        signals = build_signals(
            portfolio, {"timestamp": T1, "prices": prices},
            previous_prices={"SIM-STOCK-C": "45.00", "SIM-FUND-D": "2.0000"},
            market={"volumes": {"SIM-STOCK-C": "2"}})
        kinds = {s["risk_type"] for s in signals}
        self.assertIn("daily_move", kinds)
        self.assertIn("liquidity", kinds)
        # 两项持仓市值 750 与 600，最大占比 55.6% < 60%，不应报集中度。
        self.assertNotIn("concentration", kinds)
        single = next(h for h in portfolio["holdings"]
                      if h["symbol"] == "SIM-STOCK-C")
        concentrated = build_signals(
            {**portfolio, "holdings": [single]},
            {"timestamp": T1, "prices": {"SIM-STOCK-C": "50.00"}})
        self.assertIn("concentration",
                      {s["risk_type"] for s in concentrated})

    def test_09_news_risk_reuses_v2_and_keeps_original_text(self):
        self.seed()
        result = self.service.monitor(
            prices=PRICES, timestamp=T1,
            news_items=[{"symbol": "SIM-STOCK-A", "name": "云岚科技股份有限公司",
                         "text": "云岚科技股份有限公司公告称全厂停产，尚无复产日期。"}])
        kinds = {s["risk_type"] for s in result["signals"]}
        self.assertIn("news_risk", kinds)
        stored = [s for s in self.service.signals(run_id=result["run_id"])
                  if s["risk_type"] == "news_risk"]
        self.assertTrue(stored)
        self.assertIn("全厂停产", stored[0]["evidence"])
        self.assertEqual(stored[0]["detail"]["profile"], "v2")

    def test_10_risk_catalog_is_explicit_about_implementation(self):
        self.assertEqual(len(RISK_CATALOG), 6)
        self.assertTrue(all(item["implemented"] for item in RISK_CATALOG))
        self.assertTrue(all(item["verification"] for item in RISK_CATALOG))

    def test_11_gateway_is_swappable_and_never_leaks_credentials(self):
        describe = self.service.switch_gateway("webhook")
        self.assertFalse(describe["simulated"])
        self.assertIn("url_configured", describe)
        self.assertNotIn("token", json.dumps(describe).lower().replace(
            "token_configured", ""))
        with self.assertRaises(ValueError):
            build_gateway("unknown")

    def test_12_push_records_carry_audit_fields(self):
        self.seed()
        result = self.service.monitor(prices=PRICES, timestamp=T1,
                                      market={"daily_change_pct":
                                              {"SIM-STOCK-A": "9.0"}})
        self.assertTrue(result["pushes"])
        row = self.service.pushes()[0]
        for key in ("event_id", "recipient_masked", "message", "created_at",
                    "updated_at", "status", "gateway_id", "simulated"):
            self.assertIn(key, row)
        self.assertEqual(row["simulated"], 1)
        self.assertIn("不构成投资建议", row["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
