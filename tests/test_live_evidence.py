"""R2.1.10 (C, D136 p. 2 g and p. 6): Live evidence under new keys and in new events; the old keys stay unexported."""
from decimal import Decimal
import unittest
from unittest.mock import patch

import test_order_outcomes as outcomes
from trading_lab import exchange_diagnostics, exchange_lab
from trading_lab.battle_review import safe_data

PRICE = outcomes.PRICE


class LiveEvidence(outcomes.SeasonCase):
    TAKER = True

    def exported(self, event):
        return safe_data(event["data"], event_code=event["code"])

    def stop_loss_round_trip(self):
        def act(t):
            if self.bot["position_btc"] and self.bot.get("protection", {}).get("status") == "ARMED":
                self.fake.bid, self.fake.ask = PRICE * Decimal(".994"), PRICE * Decimal(".994") + Decimal(".01")  # below the SL
        self.run_season(act, until=lambda: bool(self.events("accounting_confirmed")) or bool(self.events("exchange_close_fill")) and not self.bot["position_btc"],
                        limit=200)

    def test_c_execution_profile_and_leverage_are_written_at_start(self):
        profile = self.events("execution_profile")
        self.assertEqual(len(profile), 1)
        view = self.exported(profile[0])
        self.assertEqual(view, {"exit_price_basis": "touch", "hold_clock": "venue_fill_time", "hold_backup_clock": "monotonic",
                                "hosted_stop_trigger": "last", "hosted_stop_order": "market", "model_exit_slippage_bps": "2",
                                "account_checks_per_step": "1", "poll_seconds": "5", "test_live_max_lots": "1",
                                "hosted_stop": True, "hosted_stop_cancel_on_close": True, "bounded_extension": True,
                                "resumed": False, "transient_codes_checked_at": exchange_diagnostics.OKX_ERROR_CHECKED_AT,
                                "code_sha256": exchange_lab.LOADED_HASH})
        leverage = self.events("leverage_confirmed")
        self.assertEqual(len(leverage), 1)
        self.assertEqual(self.exported(leverage[0]), {"lever": "1", "mgn_mode": "isolated"})

    def test_c_an_exit_carries_what_it_compared_and_the_close_its_venue_time(self):
        self.stop_loss_round_trip()
        intent = self.events("exchange_order_intent")[0]
        body = self.exported(intent)["order_body"]
        self.assertEqual({k: body[k] for k in ("instId", "side", "ordType", "tdMode", "reduceOnly")},
                         {"instId": "ETH-USDT-SWAP", "side": "buy", "ordType": "ioc", "tdMode": "isolated", "reduceOnly": False})
        self.assertEqual(body["clOrdId"], intent["data"]["request"]["clOrdId"])
        self.assertNotIn("request", self.exported(intent))  # the old key stays out: older bundles keep their hashes
        trigger = self.events("exit_trigger")[0]
        view = self.exported(trigger)
        self.assertEqual(view["exit_reason"], "stop_loss")
        self.assertEqual(view["price_basis"], "bid")
        self.assertEqual(Decimal(view["compare_price"]), Decimal(str(trigger["data"]["compare_price"])))
        self.assertLessEqual(Decimal(view["gain_bps"]), -Decimal(view["stop_loss_bps"]))
        self.assertIn("quote_age_seconds", view)
        self.assertIn("hold_overrun_seconds", view)
        self.assertNotIn("hold_clock", view)  # only a max_hold exit names its clock
        protected = self.exported(self.events("fill_protected")[0])
        self.assertIsInstance(protected["venue_stop_after_fill_ms"], int)  # on the venue's clocks alone, a record only
        close = [e for e in self.events("exchange_close_fill") if self.exported(e).get("order_state") == "filled"]
        self.assertTrue(close)
        self.assertIn("venue_updated_ms", self.exported(close[0]))

    def test_c_stop_codes_and_a_renewed_stop(self):
        self.run_season(until=lambda: self.bot["position_btc"] != 0, limit=60)
        lab = self.fx.lab
        lab.stop(lab.data["generation"], self.state["id"])
        lab.stop(lab.data["generation"], self.state["id"])  # the owner presses Stop again
        first, again = self.events("stop_requested"), self.events("stop_renewed")
        self.assertEqual([self.exported(e)["reason_code"] for e in first], ["owner_stop"])
        self.assertEqual([(self.exported(e)["reason_code"], self.exported(e)["origin"]) for e in again], [("owner_stop", "owner")])

    def test_c_a_deadline_exit_in_check_exits_is_deadline(self):
        self.run_season(until=lambda: self.bot["position_btc"] != 0, limit=60)
        lab, resource = self.fx.lab, self.fx.lab.resources[self.state["id"]]
        self.state["_end"] = self.fx.now - 1  # the season ended between the step's own check and this bot's exits
        with patch("trading_lab.exchange_lab.draining_policy", return_value=False),                 patch("trading_lab.exchange_engine.draining_policy", return_value=False), lab.lock:
            self.assertTrue(lab.check_exits(self.state, resource, self.bot, self.state["strategies"][0], self.state["_last"]))
        trigger = self.events("exit_trigger")[-1]
        self.assertEqual(self.exported(trigger)["exit_reason"], "deadline")
        self.assertEqual(self.exported(self.events("stop_requested")[-1])["reason_code"], "deadline")

    def test_c_a_system_deadline_exit_is_no_longer_called_owner_stop(self):
        self.run_season(until=lambda: self.bot["position_btc"] != 0, limit=60)
        self.state["_end"] = self.fx.now - 1
        with patch("trading_lab.exchange_lab.draining_policy", return_value=False),                 patch("trading_lab.exchange_engine.draining_policy", return_value=False):  # a season without draining
            self.run_season(limit=5)
        reasons = [self.exported(e).get("exit_reason") for e in self.events("exit_trigger")]
        codes = [self.exported(e).get("reason_code") for e in self.events("stop_requested")]
        self.assertNotIn("owner_stop", reasons + codes)
        self.assertIn("deadline", reasons + codes)

    def test_c_accounting_and_step_timing(self):
        self.stop_loss_round_trip()
        self.run_season(limit=200)
        confirmed = self.events("accounting_confirmed")
        self.assertTrue(confirmed, self.state["status"])
        view = self.exported(confirmed[0])
        self.assertEqual((view["bills_reconciled"], view["cash_delta_reconciled"]), (confirmed[0]["data"]["bill_count"], True))
        self.assertNotIn("bill_count", view)
        timing = self.events("step_timing")
        self.assertTrue(timing)  # the final one once the season is DONE or PAUSED
        view = self.exported(timing[-1])
        self.assertEqual(len(view["account_requests"]), 1)
        self.assertGreater(int(view["account_requests"][0]), 0)
        self.assertEqual(len(view["calibration_rtt_ms"]), 1)
        self.assertTrue(view["calibration_rtt_ms"][0])  # every calibration's round trip since the last report

    def test_c_protective_terminal_exports_the_cancel_source_under_its_new_key(self):
        data = {"venue_cancel_source": "1", "cancel_source_code": "1", "cancelled_by": "venue"}
        view = safe_data(data, event_code="protective_terminal")
        self.assertEqual(view.get("cancel_source_code"), "1")
        self.assertNotIn("venue_cancel_source", view)
        self.assertNotIn("cancel_source_code", safe_data({"cancel_source_code": "x1"}, event_code="protective_terminal"))


if __name__ == "__main__":
    unittest.main()
