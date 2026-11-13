import unittest

from portfolio_risk.rules import evaluate_snapshot


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.holding = {"symbol": "SIM", "stop_loss": "9.99", "take_profit": "10.01"}
        self.portfolio = {"id": "demo", "holdings": [self.holding]}
        self.snapshot = {"timestamp": "2026-11-07T09:30:00+08:00", "prices": {"SIM": "10"}}

    def test_levels_inside_and_at_bounds(self):
        for price, expected in [
            ("9", "stop_loss"), ("9.99", "stop_loss"),
            ("9.99000000000000000000000000001", None), ("10", None),
            ("10.00999999999999999999999999999", None),
            ("10.01", "take_profit"), ("11", "take_profit"),
        ]:
            with self.subTest(price=price):
                self.snapshot["prices"]["SIM"] = price
                signals = evaluate_snapshot(self.portfolio, self.snapshot)
                if expected is None:
                    self.assertEqual(signals, [])
                    continue
                self.assertEqual(len(signals), 1)
                signal = signals[0]
                self.assertEqual(signal["rule"], expected)
                self.assertEqual(signal["price"], price)
                self.assertEqual(signal["threshold"], self.holding[expected])
                self.assertEqual(signal["portfolio_id"], "demo")
                self.assertEqual(signal["symbol"], "SIM")

    def test_identity_is_stable_for_same_instant_and_distinct_between_events(self):
        self.snapshot["prices"]["SIM"] = "9"

        def event_id():
            return evaluate_snapshot(self.portfolio, self.snapshot)[0]["event_id"]

        initial = event_id()
        self.snapshot["timestamp"] = "2026-11-07T01:30:00.000Z"
        self.assertEqual(event_id(), initial)
        self.snapshot["timestamp"] = "2026-11-07T01:30:00.000000001Z"
        later = event_id()
        self.assertNotEqual(later, initial)
        self.snapshot["prices"]["SIM"] = "12"
        self.assertNotEqual(event_id(), later)

    def test_rejects_bad_prices_and_thresholds(self):
        for bad in (None, True, 9.99, "", "NaN", "Infinity", "0", "-1", "9__9", "unknown"):
            with self.subTest(bad=bad):
                self.snapshot["prices"]["SIM"] = bad
                with self.assertRaises(ValueError):
                    evaluate_snapshot(self.portfolio, self.snapshot)
        self.snapshot["prices"]["SIM"] = "10"
        for threshold in ("0", "10.01", "11"):
            with self.subTest(threshold=threshold):
                self.holding["stop_loss"] = threshold
                with self.assertRaises(ValueError):
                    evaluate_snapshot(self.portfolio, self.snapshot)

    def test_missing_quote_and_duplicate_holding(self):
        self.snapshot["prices"] = {}
        with self.assertRaisesRegex(ValueError, "缺少标的行情"):
            evaluate_snapshot(self.portfolio, self.snapshot)
        self.snapshot["prices"] = {"SIM": "8"}
        self.portfolio["holdings"].append(dict(self.holding))
        with self.assertRaisesRegex(ValueError, "持仓标的重复"):
            evaluate_snapshot(self.portfolio, self.snapshot)

    def test_rejects_missing_timezone_and_invalid_date(self):
        for timestamp in ("2026-11-07", "2026-11-07T09:30:00", "2026-02-30T00:00:00Z"):
            with self.subTest(timestamp=timestamp):
                self.snapshot["timestamp"] = timestamp
                with self.assertRaises(ValueError):
                    evaluate_snapshot(self.portfolio, self.snapshot)
