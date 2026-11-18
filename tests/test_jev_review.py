import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from trading_lab.jev import BudgetLedger
from trading_lab.jev_review import LEDGER_NAME, public_review_state, review_public_snapshot


def market_report():
    return {"status": "public_checks_passed", "public_data_verified": True,
            "instrument": {"instrument": "BTC-USDT-SWAP", "instrument_type": "SWAP",
                "size_unit": "contracts", "state": "live", "minimum_order_units": "0.01",
                "lot_step": "0.01", "contract_value": "0.01", "contract_multiplier": "1",
                "minimum_base_btc": "0.0001"},
            "quote": {"bid_usdt": "84059.9", "ask_usdt": "84060",
                      "age_seconds_using_server_clock": 1}}


class JevPublicReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = 1000.0
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def response(self, body, key):
        self.calls.append(json.loads(body))
        return {"answers": {"technical_consistency": {"choice": "consistent", "confidence": .9}},
                "usage": {"cost": .00003}, "model": "typesafe/jev-1.13-test"}

    def run_review(self, transport=None, report=None):
        with patch("trading_lab.jev_review.preflight", return_value=report or market_report()):
            return review_public_snapshot(self.root, object(), transport=transport or self.response,
                key_reader=lambda r: "unit-test-placeholder", clock=lambda: self.now, timer=lambda: self.now)

    def test_exact_units_costs_and_public_allowlist(self):
        report = market_report()
        report["account"] = {"private_sentinel": "do-not-send"}
        report["instrument"]["private_sentinel"] = "do-not-send"
        state = public_review_state(report)
        self.assertNotIn("do-not-send", json.dumps(state))
        self.assertEqual(state["proposed_test_quantity_btc"], "0.0001")
        self.assertEqual(state["reference_entry_notional_usdt"], "8.4060")
        self.assertEqual(state["estimated_round_trip_fees_at_snapshot_usdt"], "0.008405995")
        self.assertEqual(state["estimated_spread_cost_usdt"], "0.00001")
        self.assertFalse(state["live_execution_enabled"])

    def test_one_persistent_attempt_and_old_ledger_untouched(self):
        old = BudgetLedger(self.root / "runtime" / "jev_ledger.sqlite3")
        self.assertEqual(self.run_review()["status"], "review_completed")
        self.assertEqual(self.run_review()["reason"], "registered_attempt_already_consumed")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(old.summary()["attempts"], 0)
        self.assertTrue((self.root / "runtime" / LEDGER_NAME).exists())

    def test_bad_inputs_never_make_paid_request(self):
        for field, bad in [("minimum_base_btc", "0.1"), ("lot_step", "0.03"),
                           ("contract_value", "NaN"), ("instrument", "ETH-USDT-SWAP")]:
            with self.subTest(field=field):
                report = market_report()
                report["instrument"][field] = bad
                self.assertEqual(self.run_review(report=report)["status"], "blocked")
        report = market_report()
        report["quote"]["age_seconds_using_server_clock"] = 31
        self.assertEqual(self.run_review(report=report)["status"], "blocked")
        self.assertEqual(self.calls, [])

    def test_timeout_consumes_attempt_without_retry(self):
        def timeout(body, key):
            self.calls.append(body)
            raise TimeoutError()
        result = self.run_review(timeout)
        self.assertEqual(result["verdict"], "needs_review")
        self.assertEqual(result["budget"]["unresolved_billing_attempts"], 1)
        self.assertEqual(self.run_review()["reason"], "registered_attempt_already_consumed")
        self.assertEqual(len(self.calls), 1)

    def test_expired_response_cannot_pass(self):
        def slow(body, key):
            self.now += 31
            return self.response(body, key)
        result = self.run_review(slow)
        self.assertEqual(result["reason"], "review_snapshot_expired")
        self.assertEqual(result["verdict"], "needs_review")

    def test_invalid_answer_and_unknown_billing_fail_closed(self):
        bad_results = [None, {"answers": []},
            {"answers": {"technical_consistency": {"choice": "buy", "confidence": 1}}, "usage": {"cost": .001}},
            {"answers": {"technical_consistency": {"choice": "consistent", "confidence": True}}, "usage": {"cost": .001}},
            {"answers": {"technical_consistency": {"choice": "consistent", "confidence": .9}}, "usage": {"cost": True}}]
        for payload in bad_results:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as folder:
                with patch("trading_lab.jev_review.preflight", return_value=copy.deepcopy(market_report())):
                    result = review_public_snapshot(folder, object(), transport=lambda b, k: payload,
                        key_reader=lambda r: "unit-test-placeholder", timer=lambda: self.now)
                    self.assertEqual(result["status"], "blocked")
                    self.assertFalse(result["live_execution_enabled"])
                    self.assertEqual(result["orders_submitted"], 0)


if __name__ == "__main__":
    unittest.main()
