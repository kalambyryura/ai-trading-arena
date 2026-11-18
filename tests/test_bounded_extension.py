"""R2.1.10 A3 and A4 (BOARD 2def374 p. 5, D136 p. 8): a run of failed account checks on the fake exchange.

A3: a position under an owned stop verified on the venue runs on for at most 120 s of transient OKX codes, its exits
checked every step, no entry or add; everything else is as before (3 in a row -> recovery). A4: every run is a degraded
window in the journal."""
from decimal import Decimal
import unittest
from urllib.parse import urlsplit

import test_order_outcomes as outcomes
from trading_lab.battle_review import safe_data
from trading_lab.exchange_api import venue_error
from trading_lab.okx_readonly import PreflightError

PRICE = outcomes.PRICE
POSITIONS = "/api/v5/account/positions"
STOP_RECEIPT = "/api/v5/trade/order-algo"  # the GET receipt of the owned stop


def okx(code, status=200):
    def answer(request):
        request.http_status = status
        return {"code": code, "msg": "fictional", "data": []}
    return answer


def http(status, code):
    def answer(request):
        raise venue_error("http_%d" % status, http_status=status, okx_code=code)
    return answer


def local(reason):
    def answer(request):
        raise PreflightError(reason)
    return answer


class BoundedExtension(outcomes.SeasonCase):
    TAKER = True

    def setUp(self):
        super().setUp()
        self.run_season(until=lambda: self.bot["position_btc"] != 0 and self.bot.get("protection", {}).get("status") == "ARMED"
                        and self.matched() is not None, limit=100)
        self.assertIsNotNone(self.matched())
        self.answers, self.paths = [], {POSITIONS}
        api = self.fx.lab.resources[self.state["id"]]["apis"][0]
        original = api.transport

        def transport(request):
            if urlsplit(request.full_url).path in self.paths and request.get_method() == "GET" and self.answers:
                answer = self.answers[0] if len(self.answers) == 1 else self.answers.pop(0)
                return answer(request)
            return original(request)
        api.transport = transport
        self.entries = self.bot["entries"]

    def matched(self, value=None):
        """The monotonic time of the last matching stop receipt (the fixture's monotonic clock is its clock)."""
        matched = self.fx.lab.resources[self.state["id"]].setdefault("stop_matched_mono", {})
        if value is not None:
            matched[self.bot["id"]] = value
        return matched.get(self.bot["id"])

    def live_stop(self):
        return next(p for p in self.bot["_protections"] if not p.get("terminal"))

    def fail(self, *answers, steps, dt=5):
        """Each account check meets the next answer at its positions GET (the last one repeats)."""
        self.answers = list(answers)
        began = self.fx.now
        for _ in range(steps):
            self.fx.lab.step(self.state)
            self.fx.now += dt
        return began

    def first(self, code):
        found = self.events(code)
        return found[0] if found else None

    def seconds_to(self, code, began):
        event = self.first(code)
        from datetime import datetime
        return None if event is None else datetime.fromisoformat(event["at"]).timestamp() - began

    def assert_no_new_entry(self):
        intents = [e for e in self.events("exchange_order_intent") if e["data"]["request"]["reduceOnly"] is False]
        self.assertEqual(len(intents), 1)  # only the entry before the run

    def test_a3_transient_codes_under_a_verified_stop_run_on_120_s_then_recovery(self):
        began = self.fail(http(503, "50001"), steps=30)
        started = self.first("degraded_window_started")
        self.assertTrue(started["data"]["extended"])
        self.assertEqual((started["data"]["okx_code"], started["data"]["http_status"], started["data"]["endpoint"]),
                         ("50001", 503, POSITIONS))
        recovery = self.seconds_to("protective_recovery", began)
        self.assertIsNotNone(recovery)
        self.assertGreaterEqual(recovery, 120)  # not on the third failure (10 s), but at the 120 s limit
        self.assertLess(recovery, 130)
        ended = self.first("degraded_extension_ended")
        self.assertEqual(ended["data"]["reason"], "limit_120s")
        self.assertGreaterEqual(len(self.events("exchange_check_failed")), 20)
        self.assert_no_new_entry()
        self.assertEqual(self.bot["admission_blocked"], "account_unavailable")

    def test_a3_without_a_recently_verified_stop_recovery_comes_on_the_third_failure(self):
        self.matched(self.fx.now - 61)  # the last matching receipt is old, and the stop's GET fails too
        self.paths.add(STOP_RECEIPT)
        began = self.fail(http(503, "50001"), steps=6)
        self.assertFalse(self.first("degraded_window_started")["data"]["extended"])
        self.assertLess(self.seconds_to("protective_recovery", began), 15)  # the third failure, as before
        self.assertIsNone(self.first("degraded_extension_ended"))

    def test_a3_a_stop_verified_more_than_60_s_before_the_run_does_not_extend(self):
        self.paths.add(STOP_RECEIPT)
        self.matched(self.fx.now - 61)
        self.fail(http(503, "50001"), steps=1)
        self.assertFalse(self.first("degraded_window_started")["data"]["extended"])
        self.tearDown(); self.setUp()
        self.paths.add(STOP_RECEIPT)
        self.matched(self.fx.now - 59)  # within 60 s: the same run is extended
        self.fail(http(503, "50001"), steps=1)
        self.assertTrue(self.first("degraded_window_started")["data"]["extended"])

    def test_a3_a_stop_record_that_no_longer_matches_does_not_extend(self):
        for change in ("pending_amend", "size"):
            with self.subTest(change):
                self.tearDown(); self.setUp()
                self.paths.add(STOP_RECEIPT)  # no new receipt: the record as it stands decides
                stop = self.live_stop()
                if change == "pending_amend":
                    stop["pending_amend"] = dict(stop["body"])  # an amendment after an add or a reduce, not confirmed
                else:
                    stop["body"]["sz"] = str(Decimal(stop["body"]["sz"]) * 2)  # sized for another quantity
                self.fail(http(503, "50001"), steps=1)
                self.assertFalse(self.first("degraded_window_started")["data"]["extended"])

    def test_a3_an_owner_stop_inside_the_window_ends_the_extension(self):
        began = self.fail(http(503, "50001"), steps=2)
        self.assertTrue(self.first("degraded_window_started")["data"]["extended"])
        self.fx.lab.stop(self.fx.lab.data["generation"], self.state["id"])
        self.fail(http(503, "50001"), steps=3)
        self.assertEqual(self.first("degraded_extension_ended")["data"]["reason"], "stopped")
        self.assertLess(self.seconds_to("protective_recovery", began), 30)  # as before: the failures are already three

    def test_a3_51290_is_never_extended(self):
        began = self.fail(okx("51290"), steps=6)
        self.assertFalse(self.first("degraded_window_started")["data"]["extended"])
        self.assertLess(self.seconds_to("protective_recovery", began), 15)

    def test_a3_mixed_or_unknown_codes_are_as_before(self):
        began = self.fail(http(503, "50001"), okx("59999"), http(503, "50001"), steps=6)
        self.assertTrue(self.first("degraded_window_started")["data"]["extended"])
        self.assertEqual(self.first("degraded_extension_ended")["data"]["reason"], "not_transient")
        self.assertLess(self.seconds_to("protective_recovery", began), 15)  # three in a row
        for answer in (http(500, None), okx("51149", 500), okx("50017")):  # no OKX code; listed elsewhere; ADL
            with self.subTest():
                self.tearDown(); self.setUp()
                began = self.fail(answer, steps=6)
                self.assertFalse(self.first("degraded_window_started")["data"]["extended"])
                self.assertLess(self.seconds_to("protective_recovery", began), 15)

    def test_a3_a_manual_review_reason_in_the_window_is_as_before(self):
        self.fail(http(503, "50001"), local("foreign_or_unsupported_position"), steps=2)
        self.assertTrue(self.bot["_manual_review"])
        self.assertEqual(self.first("degraded_extension_ended")["data"]["reason"], "manual_review")
        self.assertEqual(self.first("degraded_window_ended")["data"]["outcome"], "manual_review")

    def test_a3_a_local_stop_in_the_window_sends_the_ordinary_close_at_once(self):
        self.fail(http(503, "50001"), steps=2)
        self.assertTrue(self.first("degraded_window_started")["data"]["extended"])
        self.fake.bid, self.fake.ask = PRICE * Decimal(".994"), PRICE * Decimal(".994") + Decimal(".01")  # below the SL
        self.fail(http(503, "50001"), steps=1)
        trigger = self.first("exit_trigger")
        self.assertEqual(trigger["data"]["exit_reason"], "stop_loss")
        closes = [e for e in self.events("exchange_order_intent") if e["data"]["request"]["reduceOnly"] is True]
        self.assertTrue(closes)
        self.assertEqual(closes[0]["seq"] > trigger["seq"], True)
        self.assertIsNone(self.first("protective_recovery"))
        self.assert_no_new_entry()

    def test_a4_a_window_that_recovers_is_recorded_with_its_skipped_decisions(self):
        self.fail(okx("51290"), steps=2)
        self.answers = []
        self.fx.lab.step(self.state)
        ended = self.first("degraded_window_ended")
        self.assertEqual(ended["data"]["outcome"], "recovered")
        self.assertEqual((ended["data"]["failures"], ended["data"]["codes"], ended["data"]["endpoints"]),
                         (2, ["51290"], [POSITIONS]))
        self.assertIn("skipped_decisions", ended["data"])
        self.assertFalse(self.state["incomplete"])  # two failures: incomplete as before
        view = safe_data(ended["data"], event_code="degraded_window_ended")
        self.assertEqual((view["outcome"], view["codes"], view["endpoints"], view["failures"]), ("recovered", ["51290"], [POSITIONS], "2"))
        started = safe_data(self.first("degraded_window_started")["data"], event_code="degraded_window_started")
        self.assertEqual((started["extended"], started["endpoint"], started["okx_code"]), (False, POSITIONS, "51290"))


if __name__ == "__main__":
    unittest.main()
