"""R2.1.15 (D183; season review team/reviews/LIVE_MIX_6D794711_REVIEW_UA.md, ef9f00c8). QA-02: a failed second read of the
owned stop in the account check (protection.ensure), the stop just read live and matching in that check, goes the ordinary
path (A1, A3, three in a row) as a failed first read does, not straight to the recovery for the rest of the round. QA-03:
OKX's 51603 on the GET of our own stop inside the STOP-FIRED-LAG wait is facts not visible yet, never a foreign state.
Strict fakes, temp roots: no network, key, Jev or order."""
from decimal import Decimal
import json
from urllib.parse import urlsplit
import unittest

import test_bounded_extension as bounded
import test_r2114_stop_fire as fire
from trading_lab.exchange_api import venue_error

STOP_RECEIPT = bounded.STOP_RECEIPT


def only_own(cls, parent):
    """Run this class's own tests, not the parent's again."""
    for name in dir(parent):
        if name.startswith("test_") and name not in cls.__dict__:
            setattr(cls, name, None)
    return cls


class StopReadInEnsure(bounded.BoundedExtension):
    def stop_gets(self, failing, answer=None):
        """The n-th GET of the stop's receipt in each of the next steps (1: reconcile, 2: ensure) meets answer (503/50001)."""
        api = self.fx.lab.resources[self.state["id"]]["apis"][0]
        inner, seen = api.transport, {"n": 0}

        def transport(request):
            if urlsplit(request.full_url).path == STOP_RECEIPT and request.get_method() == "GET":
                seen["n"] += 1
                if seen["n"] in failing:
                    return (answer or bounded.http(503, "50001"))(request)
            return inner(request)
        api.transport = transport
        self.addCleanup(setattr, api, "transport", inner)
        return seen

    def step(self, n=1, dt=5):
        for _ in range(n):
            self.fx.lab.step(self.state)
            self.fx.now += dt

    def assert_ordinary_failure(self):
        self.assertFalse(self.events("protective_recovery"))
        self.assertFalse(self.bot["stop_requested"])
        self.assertNotIn("_protection_recovery", self.bot)
        started = self.first("degraded_window_started")
        self.assertTrue(started["data"]["extended"])  # A3: under a verified stop, as for the first read
        self.assertEqual((started["data"]["endpoint"], started["data"]["okx_code"]), (STOP_RECEIPT, "50001"))
        self.assertFalse(self.state["incomplete"])

    def test_qa02_the_first_read_failing_is_a3_as_before(self):
        self.stop_gets({1})
        self.step()
        self.assert_ordinary_failure()

    def test_qa02_the_second_read_failing_is_a3_too_and_the_bot_trades_on(self):
        seen = self.stop_gets({2})
        self.step()
        self.assertEqual(seen["n"], 2)  # the check read the stop twice, the second read failed
        self.assert_ordinary_failure()
        self.step()  # the next check reads the stop fine: the window ends recovered
        ended = self.first("degraded_window_ended")
        self.assertEqual(ended["data"]["outcome"], "recovered")
        self.assertFalse(self.events("protective_recovery"))
        self.assertEqual((self.bot.get("protection") or {}).get("status"), "ARMED")

    def test_qa02_only_the_account_checks_call_takes_the_ordinary_path(self):
        """The exemption is for ensure(own_fire=True), the account check's call right after its own reconcile. Any other
        call (after a fill, before a submit) with a failed read of the stop keeps the recovery, as before."""
        from trading_lab import exchange_protection as protection
        api = self.fx.lab.resources[self.state["id"]]["apis"][0]
        self.stop_gets({1})
        with self.assertRaises(Exception):
            protection.ensure(self.fx.lab, self.state, self.bot, api)
        self.assertEqual(len(self.events("protective_recovery")), 1)

    def test_qa02_the_second_read_failing_for_120_s_ends_in_the_recovery_at_the_a3_limit(self):
        self.stop_gets(set(range(2, 400, 2)))  # every check: the first read fine, the second fails
        began = self.fx.now
        self.step(30)
        recovery = self.seconds_to("protective_recovery", began)
        self.assertIsNotNone(recovery)
        self.assertGreaterEqual(recovery, 120)
        self.assertEqual(self.first("degraded_extension_ended")["data"]["reason"], "limit_120s")

    def test_qa02_a_receipt_that_is_not_ours_on_the_second_read_is_the_recovery_at_once(self):
        def mismatch(request):
            row = dict(next(a for a in self.fake.algos.values() if a["state"] == "live"))
            row["slTriggerPx"] = str(Decimal(row["slTriggerPx"]) * 2)  # another trigger: not our stop's receipt
            return {"code": "0", "data": [row]}
        self.stop_gets({2}, mismatch)
        self.step()
        self.assertEqual(len(self.events("protective_recovery")), 1)
        self.assertTrue(self.bot["stop_requested"])
        self.assertTrue(self.state["incomplete"])

    def test_qa02_a_failed_read_of_a_stop_that_did_not_match_keeps_the_recovery(self):
        live = self.live_stop()
        algo = next(a for a in self.fake.algos.values() if a["state"] == "live")
        wider = str((Decimal(live["body"]["slTriggerPx"]) * Decimal(".99")).quantize(Decimal(".01")))
        live["body"]["slTriggerPx"] = algo["slTriggerPx"] = wider
        self.stop_gets({2})  # ensure's read before any amendment, of a stop that did not match the recipe
        self.step()
        self.assertEqual(len(self.events("protective_recovery")), 1)

    def test_qa02_an_amendment_then_a_failed_read_keeps_the_recovery(self):
        """The stop's trigger is wider than the recipe's (it does not match): ensure amends it, and the read after that POST
        fails. That is no read of an unchanged stop: the recovery at once, as before."""
        live = self.live_stop()
        algo = next(a for a in self.fake.algos.values() if a["state"] == "live")
        wider = str((Decimal(live["body"]["slTriggerPx"]) * Decimal(".99")).quantize(Decimal(".01")))
        live["body"]["slTriggerPx"] = algo["slTriggerPx"] = wider  # the long's stop 1 % lower than the recipe's
        self.stop_gets({3})  # 1: reconcile, 2: ensure before the amendment, 3: ensure after it
        self.step()
        self.assertTrue([r for r in self.fake.requests if urlsplit(r.full_url).path == "/api/v5/trade/amend-algos"])
        self.assertEqual(len(self.events("protective_recovery")), 1)
        self.assertTrue(self.bot["stop_requested"])


only_own(StopReadInEnsure, bounded.BoundedExtension)


class UnseenStopReceipt(fire.StopFiredDuringCheck):
    def unseen(self, which):
        """The which-th GET of the stop's receipt from now answers OKX's 51603, "order does not exist" (HTTP 200)."""
        api = self.fx.lab.resources[self.state["id"]]["apis"][0]
        inner, seen = api.transport, {"n": 0}

        def transport(request):
            if urlsplit(request.full_url).path == fire.ALGO and request.get_method() == "GET":
                seen["n"] += 1
                if seen["n"] == which:
                    self.fake.requests.append(request)
                    return {"code": "51603", "msg": "fictional", "data": []}
            return inner(request)
        api.transport = transport
        return seen

    def test_qa03_51603_on_the_stop_inside_the_wait_is_one_more_check(self):
        self.hide = 7  # the venue shows our fired stop live for 7 s
        self.before_position = self.fire
        self.step()
        self.assertEqual(self.codes("protective_fire_pending"), ["protective_fire_pending"])
        self.unseen(1)  # the next check's first read of the stop: 51603
        posts = len(self.posts())
        self.step()
        self.assertFalse(self.events("exchange_check_failed"))
        self.assertFalse(self.bot["_manual_review"])
        self.assertEqual(self.bot["_stop_fire_wait"]["checks"], 2)
        self.assertEqual(len(self.posts()), posts)
        self.step()  # the facts are visible now
        self.assertEqual(len(self.events("protective_fire_resolved")), 1)
        self.assert_no_recovery()
        self.finish_and_reconcile()

    def test_qa03_51603_on_the_reread_that_explains_the_position_opens_the_wait(self):
        self.before_position = self.fire  # flat on the venue, our stop live on its last read
        self.unseen(2)  # the check's re-read of the stop (stop_fired): 51603
        self.step()
        self.assertEqual(self.codes("protective_fire_pending"), ["protective_fire_pending"])
        self.assertFalse(self.events("exchange_check_failed"))
        self.assertFalse(self.bot["_manual_review"])
        self.step()
        self.assertEqual(len(self.events("protective_fire_resolved")), 1)
        self.assert_no_recovery()
        self.finish_and_reconcile()

    def test_qa03_51603_until_the_wait_limit_is_the_manual_review_as_before(self):
        self.hide = 10 ** 6
        self.before_position = self.fire
        self.step()
        api = self.fx.lab.resources[self.state["id"]]["apis"][0]
        inner = api.transport

        def transport(request):
            if urlsplit(request.full_url).path == fire.ALGO and request.get_method() == "GET":
                return {"code": "51603", "msg": "fictional", "data": []}
            return inner(request)
        api.transport = transport
        self.step(fire.STOP_FIRE_WAIT_CHECKS)
        self.assertEqual(len(self.events("protective_fire_expired")), 1)
        self.assert_manual_review()

    def test_qa03_51603_with_no_wait_open_stays_an_ordinary_failure(self):
        """And the boundary with QR-01: this stop has receipts (OKX showed it live), so no verdict "not created" either."""
        self.unseen(1)
        self.step()
        failed = self.events("exchange_check_failed")
        self.assertEqual(failed[-1]["data"]["reason"], "okx_error_51603")
        self.assertFalse(self.bot["_manual_review"])
        self.assertFalse(self.codes("protective_fire_pending", "protective_absent", "protective_not_created"))

    def test_qa03_another_code_inside_the_wait_is_an_ordinary_failure(self):
        self.hide = 7
        self.before_position = self.fire
        self.step()
        self.assertEqual(self.codes("protective_fire_pending"), ["protective_fire_pending"])
        api = self.fx.lab.resources[self.state["id"]]["apis"][0]
        inner, seen = api.transport, {"n": 0}

        def transport(request):
            if urlsplit(request.full_url).path == fire.ALGO and request.get_method() == "GET" and not seen["n"]:
                seen["n"] += 1
                raise venue_error("http_503", http_status=503, okx_code="50001")
            return inner(request)
        api.transport = transport
        self.step()
        self.assertEqual(self.events("exchange_check_failed")[-1]["data"]["reason"], "http_503")  # not "facts not visible yet"
        self.assertEqual(self.bot["_stop_fire_wait"]["checks"], 1)


only_own(UnseenStopReceipt, fire.StopFiredDuringCheck)


if __name__ == "__main__":
    unittest.main()
