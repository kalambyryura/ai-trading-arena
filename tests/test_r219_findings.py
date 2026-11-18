"""R2.1.10: the R2.1.9 review findings (team/reviews/R219_REVIEW_UA.md) on the fake exchange."""
from datetime import datetime
from decimal import Decimal
import json
import unittest
from urllib.parse import urlsplit

import test_order_outcomes as outcomes

PRICE = outcomes.PRICE


def at(event):
    return datetime.fromisoformat(event["at"]).timestamp()


class CloseLate(outcomes.SeasonCase):
    """R219-CLOSE-LATE: OKX takes our stop off at our own close at once, and the close's facts show a read later."""
    TAKER, DURATION = True, 1800

    def lag_closes(self, reads, lose=False):
        api = self.fx.lab.resources[self.state["id"]]["apis"][0]
        original = api.transport

        def transport(request):
            path = urlsplit(request.full_url).path
            close = path == "/api/v5/trade/order" and request.get_method() == "POST" and json.loads(request.data)["reduceOnly"]
            if close and lose:
                self.fake.lose_created, self.fake.lag = "close", reads  # the answer is lost too (E5)
                return original(request)
            answer = original(request)
            if close:
                self.fake.lagging[json.loads(request.data)["clOrdId"]] = reads  # its GET answers 51603 for these reads
            return answer
        api.transport = transport

    def round_trip(self, reads, lose=False, until=None):
        self.lag_closes(reads, lose)
        marks = {}

        def act(t):
            if self.bot["position_btc"] and self.bot.get("protection", {}).get("status") == "ARMED" and "low" not in marks:
                self.fake.bid, self.fake.ask = PRICE * Decimal(".994"), PRICE * Decimal(".994") + Decimal(".01")  # below the SL
                marks["low"] = t
            elif "low" in marks and not self.bot["position_btc"] and "back" not in marks:
                self.fake.bid, self.fake.ask = PRICE, PRICE + Decimal(".01")
                marks["back"] = t
        self.run_season(act, until=until, limit=500)

    def entries(self):
        return [e for e in self.events("exchange_order_intent") if e["data"]["request"]["reduceOnly"] is False]

    def assert_flat_without_recovery_then_a_next_entry(self):
        self.assertFalse(self.events("protective_recovery"))
        self.assertTrue(self.events("protective_close_pending"))
        closed = [e for e in self.events("exchange_close_fill") if e["data"].get("order_state") == "filled"]
        self.assertTrue(closed)
        self.assertEqual(len(self.entries()), 2)
        recipe = self.state["strategies"][0]
        self.assertGreaterEqual(at(self.entries()[1]) - at(closed[0]), recipe["rules"]["cooldown_seconds"])  # with the cooldown
        self.assertFalse(self.bot["_manual_review"])

    def test_close_late_e1_one_lagging_read(self):
        self.round_trip(1, until=lambda: len(self.entries()) >= 2)
        self.assert_flat_without_recovery_then_a_next_entry()

    def test_close_late_e1b_two_lagging_reads(self):
        self.round_trip(2, until=lambda: len(self.entries()) >= 2)
        self.assert_flat_without_recovery_then_a_next_entry()

    def test_close_late_e5_the_close_answer_lost_and_its_reads_lagging(self):
        self.round_trip(2, lose=True, until=lambda: len(self.entries()) >= 2)
        self.assert_flat_without_recovery_then_a_next_entry()

    def test_close_late_negative_a_close_that_resolves_with_contracts_left_gets_the_recovery(self):
        self.run_season(until=lambda: self.bot["position_btc"] != 0 and self.bot.get("protection", {}).get("status") == "ARMED",
                        limit=60)
        self.fake.close_fraction = Decimal(0)  # our close is accepted and ends with no fill
        for algo in self.fake.algos.values():
            algo["state"] = "canceled"  # and the stop is gone while the position stays
        self.fake.bid, self.fake.ask = PRICE * Decimal(".994"), PRICE * Decimal(".994") + Decimal(".01")
        self.run_season(until=lambda: bool(self.events("protective_recovery")), limit=20)
        self.assertTrue(self.events("protective_recovery"))


class TwoPhase(outcomes.SeasonCase):
    """R219-RELIST and R219-STOP-SLOW on a resting post-only entry whose verdict is two-phase (nothing_at)."""

    def resting(self):
        self.run_season(until=lambda: any(o["request"]["ordType"] == "post_only" for o in self.bot["_orders"]), limit=40)
        return next(o for o in self.bot["_orders"] if o["request"]["ordType"] == "post_only")

    def test_relist_a_deferred_entry_listed_again_needs_a_fresh_nothing_to_cancel(self):
        order = self.resting()
        order["_absent"] = {"reads": 3, "first": self.fx.now - 100, "last": self.fx.now, "nothing_at": self.fx.now - 5}
        self.fake.lagging[order["request"]["clOrdId"]] = 5  # its GET still answers 51603
        self.fx.lab.step(self.state)  # while the venue lists it in orders-pending
        self.assertNotIn("nothing_at", order.get("_absent", {}))
        self.assertFalse(self.events("exchange_order_absent"))  # no verdict without a new cancel

    def test_stop_slow_a_flat_bot_waiting_only_for_its_verdict_gets_one_more_check(self):
        order = self.resting()
        self.fake.hidden[order["request"]["clOrdId"]] = self.fx.now + 10_000  # not listed, its GET does not show it
        order["_absent"] = {"reads": 1, "first": self.fx.now, "last": self.fx.now, "nothing_at": self.fx.now}
        self.fx.lab.stop(self.fx.lab.data["generation"], self.state["id"])
        self.bot["_closing_since"] = self.fx.now - 125  # slow private requests put the marking step past the deadline
        self.fx.lab.resources[self.state["id"]]["closing_deadlines"].clear()
        self.fx.lab.step(self.state)
        self.assertNotEqual(self.state["status"], "PAUSED")  # R2.1.9: PAUSED here
        order["_absent"].update(reads=3, first=self.fx.now - 70)
        self.fake.orders[order["request"]["clOrdId"]]["state"] = "canceled"  # gone on the venue, no fill
        self.fx.now += 5
        self.run_season(limit=10)
        self.assertEqual(self.state["status"], "DONE")
        self.assertFalse(self.events("closing_paused"))

    def test_stop_slow_an_unresolved_order_without_its_verdict_mark_is_overdue_as_before(self):
        order = self.resting()
        self.fake.hidden[order["request"]["clOrdId"]] = self.fx.now + 10_000
        order["_absent"] = {"reads": 1, "first": self.fx.now, "last": self.fx.now}
        self.fx.lab.stop(self.fx.lab.data["generation"], self.state["id"])
        self.bot["_closing_since"] = self.fx.now - 125
        self.fx.lab.resources[self.state["id"]]["closing_deadlines"].clear()
        self.fx.lab.step(self.state)
        self.assertEqual(self.state["status"], "PAUSED")


if __name__ == "__main__":
    unittest.main()
