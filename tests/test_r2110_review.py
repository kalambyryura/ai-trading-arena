"""R2.1.10 review findings (team/reviews/R2110_REVIEW_UA.md, D138) on the fake exchange: the two medium ones,
R2110-INCOMPLETE and R2110-STUCK, test gaps of R2110-TESTS, and (R2.1.11) DUP-EXIT, WINDOW-END and TIMING-DONE."""
from datetime import datetime
from decimal import Decimal
import hashlib
import io
import json
import unittest
import urllib.error
import zipfile
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import test_order_outcomes as outcomes
from trading_lab import exchange_engine as engine
from trading_lab import exchange_protection as protection
from trading_lab.arena_strategies import ArenaError
from trading_lab.battle_review import safe_data
from trading_lab.exchange_api import send, venue_error
from trading_lab.okx_readonly import PreflightError

PRICE = outcomes.PRICE
POSITIONS = "/api/v5/account/positions"
LOW = (PRICE * Decimal(".994"), PRICE * Decimal(".994") + Decimal(".01"))  # below the recipe's 50 bps stop
CHILD = "100099"  # outcomes.fire_stop's child ordId


def at(event):
    return datetime.fromisoformat(event["at"]).timestamp()


def unavailable(request):
    raise venue_error("http_503", http_status=503, okx_code="50001")


class Window(outcomes.SeasonCase):
    """An open position under the verified owned stop; positions GETs answer 50001/503 while self.failing is set."""
    TAKER = True

    def setUp(self):
        super().setUp()
        self.run_season(until=lambda: self.bot["position_btc"] != 0 and self.bot.get("protection", {}).get("status") == "ARMED"
                        and self.matched() is not None, limit=100)
        self.failing, self.lagged, self.down = False, {}, set()
        api = self.fx.lab.resources[self.state["id"]]["apis"][0]
        original = api.transport

        def transport(request):
            route = urlsplit(request.full_url)
            path = route.path
            if path == POSITIONS and request.get_method() == "GET" and self.failing:
                return unavailable(request)
            if request.get_method() == "GET" and (path == "/api/v5/trade/order-algo" and "algo" in self.down
                                                  or path == "/api/v5/trade/order" and "child" in self.down
                                                  and parse_qs(route.query).get("ordId") == [CHILD]):
                return unavailable(request)  # R2111-RECHECK-FIRED: the stop's own GET or its child's
            answer = original(request)
            if path == "/api/v5/trade/order" and request.get_method() == "POST" and self.lagged:
                body = json.loads(request.data)
                if body["reduceOnly"]:
                    self.fake.lagging[body["clOrdId"]] = self.lagged["reads"]
                    if self.lagged.get("cancel_stop"):
                        for algo in self.fake.algos.values():
                            algo["state"] = "canceled"
            return answer
        api.transport = transport

    def matched(self):
        return self.fx.lab.resources[self.state["id"]].get("stop_matched_mono", {}).get(self.bot["id"])

    def steps(self, n, dt=5):
        for _ in range(n):
            self.fx.lab.step(self.state)
            if self.state["status"] in {"DONE", "PAUSED"}:
                return
            self.fx.now += dt

    def first(self, code):
        found = self.events(code)
        return found[0] if found else None

    def assert_flat_in_cooldown(self):
        """Flat, no recovery, and the recipe's cooldown from the close before a new entry, as on R2.1.10."""
        self.assertEqual(self.bot["position_btc"], 0)
        self.assertFalse(self.events("protective_recovery"))
        self.assertEqual((self.bot["status"], self.bot["protection"]["status"]), ("WAITING", "FLAT"))
        cooldown = next(r["rules"]["cooldown_seconds"] for r in self.state["strategies"] if r["id"] == self.bot["id"])
        self.assertGreaterEqual(self.bot["_next_decision"], at(self.events("exchange_close_fill")[-1]) + cooldown)
        entries = [e for e in self.events("exchange_order_intent") if not e["data"]["request"]["reduceOnly"]]
        self.assertEqual(len(entries), 1)  # no entry inside the cooldown

    def test_incomplete_three_transient_failures_then_success_set_incomplete_without_recovery(self):
        self.failing = True
        self.steps(3)
        self.failing = False
        self.steps(1)
        self.assertTrue(self.state["incomplete"])  # as R2.1.9 sets it on the third failure
        self.assertFalse(self.events("protective_recovery"))
        self.assertNotEqual(self.bot["position_btc"], 0)  # the position stayed under its stop
        self.assertEqual(self.first("degraded_window_ended")["data"]["outcome"], "recovered")

    def test_incomplete_two_failures_leave_the_round_complete(self):
        self.failing = True
        self.steps(2)
        self.failing = False
        self.steps(1)
        self.assertFalse(self.state["incomplete"])

    def test_flat_a_close_inside_the_window_ends_the_extension(self):
        self.failing = True
        self.steps(1)
        self.fake.bid, self.fake.ask = LOW
        self.steps(3)
        self.assertEqual(self.bot["position_btc"], 0)
        self.assertEqual(self.first("degraded_extension_ended")["data"]["reason"], "flat")

    def test_resume_writes_the_execution_profile_again(self):
        lab = self.fx.lab
        lab.resources.pop(self.state["id"])["stack"].close()
        lab.resume(lab.data["generation"], self.state["id"])
        state = lab.sessions[self.state["id"]]
        lab.prepare(state, lab.chosen_profiles(["arena-a"]), True)
        profiles = [e for e in state["events"] if e["code"] == "execution_profile"]
        self.assertEqual([e["data"]["resumed"] for e in profiles], [False, True])

    def test_resume_after_an_owner_stop_is_a_renewed_stop(self):
        lab = self.fx.lab
        lab.stop(lab.data["generation"], self.state["id"])
        lab.resources.pop(self.state["id"])["stack"].close()
        lab.resume(lab.data["generation"], self.state["id"])
        renewed = [e for e in lab.sessions[self.state["id"]]["events"] if e["code"] == "stop_renewed"]
        self.assertEqual([(e["data"]["origin"], e["data"]["reason_code"]) for e in renewed], [("recovery", "recovery")])

    def test_dup_exit_one_trigger_for_one_exit_while_the_close_is_unread(self):
        self.lagged = {"reads": 2}
        self.fake.bid, self.fake.ask = LOW
        self.steps(8)
        self.assertEqual(len(self.events("exit_trigger")), 1)
        self.assertEqual(len(self.events("rules_close")), 1)

    def test_dup_exit_a_failed_close_inside_the_window_is_one_failure_and_no_second_trigger(self):
        self.failing = True
        self.steps(1)
        before = len(self.events("exchange_check_failed"))
        self.fake.bid, self.fake.ask = LOW
        from trading_lab import strategy_runtime
        failed = venue_error("http_503", http_status=503, okx_code="50001")
        failed.method, failed.endpoint = "GET", POSITIONS  # a transient failure: the extension goes on
        with patch.object(self.fx.lab, "closing", side_effect=failed), \
                patch.object(strategy_runtime, "advance", wraps=strategy_runtime.advance) as advance:
            self.steps(1)
        self.assertEqual(advance.call_count, 1)  # the exit rules ran once in this step, not again for the failed close
        self.assertEqual(len(self.events("exit_trigger")), 1)
        self.assertEqual(len(self.events("exchange_check_failed")) - before, 2)  # the check, and the close: one each
        self.steps(1)
        self.assertEqual(len(self.events("exit_trigger")), 1)

    def test_window_end_a_season_that_settles_inside_a_window_closes_it_first(self):
        settle = engine.settle

        def settle_in_a_window(state, bot, *args):  # a window still open when the bot settles (RC1: closing() in a failed step)
            bot["_degraded"] = {"start": self.fx.now - 10, "failures": 1, "codes": ["50001"], "endpoints": [POSITIONS],
                                "http_statuses": [503], "skipped_decisions": 0, "extension": "active"}
            bot["admission_blocked"] = "degraded_window"
            return settle(state, bot, *args)
        self.fake.bid, self.fake.ask = LOW
        with patch.object(engine, "settle", side_effect=settle_in_a_window):
            self.steps(200)
        self.assertEqual(self.state["status"], "DONE")
        ended, done = self.first("degraded_window_ended"), self.first("season_done")
        self.assertIsNotNone(ended)
        self.assertLess(ended["seq"], done["seq"])
        self.assertNotEqual(self.bot.get("admission_blocked"), "degraded_window")

    def test_timing_done_the_final_step_timing_comes_before_season_done(self):
        self.fake.bid, self.fake.ask = LOW
        self.steps(200)
        self.assertEqual(self.state["status"], "DONE")
        timing, done = self.events("step_timing")[-1], self.first("season_done")
        self.assertLess(timing["seq"], done["seq"])
        self.assertFalse([e for e in self.events("step_timing") if e["seq"] > done["seq"]])

    def test_window_end_a_resume_closes_the_saved_window_as_restarted(self):
        self.failing = True
        self.steps(2)
        self.assertIsNotNone(self.bot.get("_degraded"))
        lab = self.fx.lab
        lab.resources.pop(self.state["id"])["stack"].close()
        self.fx.now += 600
        lab.resume(lab.data["generation"], self.state["id"])
        self.state = lab.sessions[self.state["id"]]
        self.bot = self.state["bots"][0]
        lab.prepare(self.state, lab.chosen_profiles(["arena-a"]), True)
        ended = [e for e in self.state["events"] if e["code"] == "degraded_window_ended"]
        self.assertEqual(ended[-1]["data"]["outcome"], "restarted")
        self.assertIsNone(self.bot.get("_degraded"))
        profile = [e for e in self.state["events"] if e["code"] == "execution_profile"]
        self.assertTrue(profile[-1]["data"]["resumed"])  # R2110-TESTS: the profile on Resume
        renewed = [e for e in self.state["events"] if e["code"] == "stop_renewed"]
        self.assertFalse(renewed)  # the Resume's own Stop is the first one here: stop_requested, origin recovery
        self.assertEqual([e for e in self.state["events"] if e["code"] == "stop_requested"][-1]["data"]["origin"], "recovery")

    def test_recheck_a_close_in_the_window_whose_stop_okx_took_off_is_no_recovery(self):
        # R2.1.11 (R2110-RECHECK, check p. 3): our close is on its way, OKX cancels the stop at it (cxlOnClosePos), the
        # positions GET fails: the re-check must not end the extension (the R219-CLOSE-LATE class)
        self.failing = True
        self.steps(1)
        self.lagged = {"reads": 2}
        self.fake.bid, self.fake.ask = LOW
        self.steps(4)
        ended = [e["data"]["reason"] for e in self.events("degraded_extension_ended")]
        self.assertNotIn("stop_unmatched", ended)
        self.assertFalse(self.events("protective_recovery"))
        self.failing = False
        self.steps(6)
        self.assert_flat_in_cooldown()

    def test_recheck_a_stop_okx_took_off_without_our_close_is_a_veto_and_a_recovery(self):
        # R2.1.11 (check p. 3, RC2): no close of ours on its way, the stop is gone on the venue: the extension ends at once
        self.failing = True
        self.steps(1)
        self.assertTrue(self.first("degraded_window_started")["data"]["extended"])
        for algo in self.fake.algos.values():
            algo["state"] = "canceled"
        self.steps(1)
        # the stop's GET reads canceled before the positions GET fails: the recovery's system stop comes first, so the
        # extension ends at that same step as "stopped" (stop_unmatched would follow it in the veto order)
        ended, recovery = self.first("degraded_extension_ended"), self.first("protective_recovery")
        self.assertEqual(ended["data"]["reason"], "stopped")
        self.assertIsNotNone(recovery)
        self.assertEqual(at(ended), at(recovery))
        self.assertNotEqual(self.bot["position_btc"], 0)  # left without a stop: the recovery closes it next

    def fire_in_the_window(self, *downs):
        """The A3 window opens under the verified stop, the stop fires (its child filled on the venue), then each step
        fails the positions GET and the GETs in downs[i]; then every GET answers."""
        self.failing = True
        self.steps(1)
        self.assertTrue(self.first("degraded_window_started")["data"]["extended"])
        outcomes.fire_stop(self.fx, self.fake)
        for down in downs:
            self.down = set(down)
            self.steps(1)
        self.failing, self.down = False, set()
        self.steps(6)

    def test_recheck_fired_the_child_unread_twice_is_no_recovery(self):
        # R2111-RECHECK-FIRED (variant B): an effective stop whose child is unread is on its way to terminal, not a mismatch
        self.fire_in_the_window({"child"}, {"child"})
        self.assertNotIn("stop_unmatched", [e["data"]["reason"] for e in self.events("degraded_extension_ended")])
        self.assertTrue(self.state["incomplete"])  # the third failure, as before (D138 p. 1)
        self.assert_flat_in_cooldown()

    def test_recheck_fired_the_algo_get_fails_then_the_childs(self):
        self.fire_in_the_window({"algo"}, {"child"})
        self.assertNotIn("stop_unmatched", [e["data"]["reason"] for e in self.events("degraded_extension_ended")])
        self.assert_flat_in_cooldown()

    def test_recheck_fired_effective_without_a_child_is_a_veto(self):
        # the hole the variant must not open: OKX says effective but names no child, the position still open
        self.failing = True
        self.steps(1)
        algo = next(a for a in self.fake.algos.values() if a["state"] == "live")
        algo.update(state="effective", ordIdList=[])
        self.steps(1)
        self.assertEqual(self.first("degraded_extension_ended")["data"]["reason"], "stop_unmatched")

    def test_recheck_a_stop_record_that_stops_matching_ends_the_extension(self):
        self.failing = True
        self.steps(1)
        self.assertTrue(self.first("degraded_window_started")["data"]["extended"])
        stop = next(p for p in self.bot["_protections"] if not p.get("terminal"))
        stop["pending_amend"] = {**stop["body"], "sz": str(Decimal(stop["body"]["sz"]) * 2)}  # sent, not confirmed by OKX
        self.steps(1)
        self.assertEqual(self.first("degraded_extension_ended")["data"]["reason"], "stop_unmatched")

    def test_veto_an_unresolved_own_order_ends_the_extension(self):
        # R2110-TESTS: unreachable in the natural run (no decision inside a window), so a recorded entry is put in
        self.failing = True
        self.steps(1)
        self.assertTrue(self.first("degraded_window_started")["data"]["extended"])
        self.bot["_orders"].append({"request": {"instId": "ETH-USDT-SWAP", "side": "buy", "ordType": "post_only", "sz": "0.01",
                                                "reduceOnly": False, "clOrdId": "J5" + "e" * 16 + "0E9998"},
                                    "at": self.fx.now, "send_started": True, "result": None})
        self.steps(1)
        self.assertEqual(self.first("degraded_extension_ended")["data"]["reason"], "unresolved_order")

    def test_vetoes_a_stale_quote_at_the_opening_and_inside_the_window(self):
        resource = self.fx.lab.resources[self.state["id"]]
        snapshot = resource["feed"].snapshot
        resource["feed"].snapshot = lambda: (_ for _ in ()).throw(PreflightError("market_arrival_stale"))
        self.failing = True
        self.steps(1)
        self.assertFalse(self.first("degraded_window_started")["data"]["extended"])  # no fresh quote at the opening
        self.tearDown(); self.setUp()
        resource = self.fx.lab.resources[self.state["id"]]
        snapshot = resource["feed"].snapshot
        self.failing = True
        self.steps(2)
        resource["feed"].snapshot = lambda: (_ for _ in ()).throw(PreflightError("market_arrival_stale"))
        self.steps(1)
        self.assertEqual(self.first("degraded_extension_ended")["data"]["reason"], "market_not_fresh")
        resource["feed"].snapshot = snapshot

    def test_boundaries_the_extension_ends_at_exactly_120_s(self):
        self.failing = True
        start = self.fx.now
        self.steps(30)
        ended = self.first("degraded_extension_ended")
        self.assertEqual(ended["data"]["reason"], "limit_120s")
        self.assertEqual(at(ended) - start, 120)

    def test_boundaries_a_stop_matched_exactly_60_s_before_extends(self):
        api = self.fx.lab.resources[self.state["id"]]["apis"][0]
        original = api.transport

        def no_receipt(request):  # the stop's own GET fails too, so the mark stays as set
            if urlsplit(request.full_url).path == "/api/v5/trade/order-algo" and request.get_method() == "GET":
                return unavailable(request)
            return original(request)
        api.transport = no_receipt
        self.fx.lab.resources[self.state["id"]]["stop_matched_mono"][self.bot["id"]] = self.fx.now - 60
        self.steps(1)
        self.assertTrue(self.first("degraded_window_started")["data"]["extended"])

    def test_stop_mark_matches_only_the_live_actual_size_and_a_trigger_no_wider(self):
        stop = next(p for p in self.bot["_protections"] if not p.get("terminal"))
        self.assertTrue(protection.stop_matches(self.state, self.bot, stop))
        for change in ("pending_amend", "size", "wider", "state"):
            changed = json.loads(json.dumps(stop))
            if change == "pending_amend":
                changed["pending_amend"] = dict(changed["body"])
            elif change == "size":
                changed["body"]["sz"] = str(Decimal(changed["body"]["sz"]) * 2)
            elif change == "wider":
                changed["body"]["slTriggerPx"] = str(Decimal(changed["body"]["slTriggerPx"]) * Decimal(".99"))  # a long: lower
            else:
                changed["proof"]["state"], changed["proof"]["live_verified"] = "canceled", False
            self.assertFalse(protection.stop_matches(self.state, self.bot, changed), change)


class FiredChildUnread(unittest.TestCase):
    """R2111-RECHECK-FIRED: the contract of exchange_protection.fired_child_unread on plain records."""

    def test_only_a_fired_stop_with_a_listed_child_not_yet_resolved(self):
        def bot(child_state=None):
            orders = [] if child_state is None else [{"_protective_parent": "S1", "_child_id": CHILD, "result": {"state": child_state}}]
            return {"_orders": orders}

        def record(state, children=(CHILD,)):
            return {"body": {"algoClOrdId": "S1"}, "proof": {"state": state, "child_order_ids": list(children)}}
        for state in ("effective", "partially_effective"):
            self.assertTrue(protection.fired_child_unread(bot(), record(state)), state)  # the child not read yet
            self.assertTrue(protection.fired_child_unread(bot("live"), record(state)), state)  # read, not finished
            for done in ("filled", "canceled", "mmp_canceled"):
                self.assertFalse(protection.fired_child_unread(bot(done), record(state)), (state, done))
            self.assertFalse(protection.fired_child_unread(bot(), record(state, ())), state)  # no child listed
        for state in ("live", "canceled", "order_failed", None):
            self.assertFalse(protection.fired_child_unread(bot(), record(state)), state)  # not a fire: the re-check decides
        other = {"_orders": [{"_protective_parent": "S2", "_child_id": CHILD, "result": {"state": "filled"}}]}
        self.assertTrue(protection.fired_child_unread(other, record("effective")))  # another stop's child is not this one's


class CloseLateNegative(outcomes.SeasonCase):
    """R2110-TESTS: the deferral itself is exercised, then a close that resolves with contracts left gets the recovery."""
    TAKER = True

    def test_close_late_deferred_then_contracts_left_is_a_recovery(self):
        self.run_season(until=lambda: self.bot["position_btc"] != 0 and self.bot.get("protection", {}).get("status") == "ARMED",
                        limit=100)
        api = self.fx.lab.resources[self.state["id"]]["apis"][0]
        original = api.transport

        def transport(request):
            answer = original(request)
            if urlsplit(request.full_url).path == "/api/v5/trade/order" and request.get_method() == "POST":
                body = json.loads(request.data)
                if body["reduceOnly"]:
                    self.fake.lagging[body["clOrdId"]] = 1  # its facts a read later
                    for algo in self.fake.algos.values():
                        algo["state"] = "canceled"  # and the stop gone at once
            return answer
        api.transport = transport
        self.fake.close_fraction = Decimal(0)  # the close ends with no fill: the contracts stay
        self.fake.bid, self.fake.ask = LOW
        self.run_season(until=lambda: bool(self.events("protective_recovery")), limit=30)
        pending, recovery = self.events("protective_close_pending"), self.events("protective_recovery")
        self.assertTrue(pending)
        self.assertTrue(recovery)
        self.assertLess(pending[0]["seq"], recovery[0]["seq"])


class Stuck(outcomes.SeasonCase):
    """R2110-STUCK: the reviewer's chain n1 (a lost entry POST, then an order placed by hand) and the grace's ceiling."""
    TAKER, DURATION = False, 1800

    def marked(self):
        return [o for o in engine.unresolved(self.bot) if (o.get("_absent") or {}).get("nothing_at")]

    def test_stuck_manual_review_during_the_grace_pauses_as_r219(self):
        self.fake.lose_entry = True
        self.run_season(until=lambda: bool(self.marked()), limit=200)
        self.assertTrue(self.marked())
        self.fake.orders["manual-1"] = {"instId": self.fake.spec["instId"], "clOrdId": "", "ordId": "999001", "side": "buy",
                                        "posSide": "net", "tdMode": "isolated", "ordType": "limit", "sz": "1", "reduceOnly": False,
                                        "px": "1000", "state": "live", "accFillSz": "0", "avgPx": "", "fee": "0", "feeCcy": "USDT",
                                        "pnl": "0", "rebate": "0", "uTime": "1"}
        self.run_season(until=lambda: self.bot["_manual_review"], limit=5)
        self.assertTrue(self.bot["_manual_review"])
        began = self.fx.now
        self.run_season(limit=60)
        self.assertEqual(self.state["status"], "PAUSED")  # R2.1.9 did the same within 120 s; RC1 stayed CLOSING
        self.assertLessEqual(self.fx.now - began, 130)
        paused = self.events("closing_paused")[-1]  # R2.1.11 (check p. 2): the final step_timing for PAUSED too
        self.assertTrue([e for e in self.events("step_timing") if e["seq"] > paused["seq"]])
        lab = self.fx.lab  # and the owner's Resume works
        lab.resources.pop(self.state["id"])["stack"].close()
        lab.resume(lab.data["generation"], self.state["id"])
        self.assertEqual(lab.sessions[self.state["id"]]["status"], "CLOSING")

    def test_stuck_no_grace_in_a_recovery(self):
        self.run_season(until=lambda: any(o["request"]["ordType"] == "post_only" for o in self.bot["_orders"]), limit=40)
        order = next(o for o in self.bot["_orders"] if o["request"]["ordType"] == "post_only")
        self.fake.hidden[order["request"]["clOrdId"]] = self.fx.now + 10_000
        order["_absent"] = {"reads": 1, "first": self.fx.now, "last": self.fx.now, "nothing_at": self.fx.now}
        self.fx.lab.stop(self.fx.lab.data["generation"], self.state["id"])
        self.state["_recovery"] = True  # a Resume's close-only attempt (D138: no grace in a recovery)
        self.bot["_closing_since"] = self.fx.now - 125
        self.fx.lab.resources[self.state["id"]]["closing_deadlines"].clear()
        self.fx.lab.step(self.state)
        self.assertEqual(self.state["status"], "PAUSED")

    def test_stuck_the_grace_is_at_most_60_s(self):
        self.run_season(until=lambda: any(o["request"]["ordType"] == "post_only" for o in self.bot["_orders"]), limit=40)
        order = next(o for o in self.bot["_orders"] if o["request"]["ordType"] == "post_only")
        self.fake.hidden[order["request"]["clOrdId"]] = self.fx.now + 10_000
        order["_absent"] = {"reads": 1, "first": self.fx.now, "last": self.fx.now, "nothing_at": self.fx.now}
        self.fx.lab.stop(self.fx.lab.data["generation"], self.state["id"])
        self.bot["_closing_since"] = self.fx.now - 125
        self.fx.lab.resources[self.state["id"]]["closing_deadlines"].clear()
        with patch.object(self.fx.lab, "timed_account_check", side_effect=lambda state, bot, api: self.fx.now):
            began = self.fx.now
            self.run_season(limit=30)  # checks that never finish (and never fail): the grace ends on its own ceiling
        self.assertEqual(self.state["status"], "PAUSED")
        self.assertLessEqual(self.fx.now - began, 65)
        self.assertGreaterEqual(self.fx.now - began, 60)


    def grace_start(self):
        """A flat bot past its close deadline whose one unresolved order waits only for its two-phase verdict."""
        self.run_season(until=lambda: any(o["request"]["ordType"] == "post_only" for o in self.bot["_orders"]), limit=40)
        order = next(o for o in self.bot["_orders"] if o["request"]["ordType"] == "post_only")
        self.fake.hidden[order["request"]["clOrdId"]] = self.fx.now + 10_000
        order["_absent"] = {"reads": 1, "first": self.fx.now, "last": self.fx.now, "nothing_at": self.fx.now}
        self.fx.lab.stop(self.fx.lab.data["generation"], self.state["id"])
        self.bot["_closing_since"] = self.fx.now - 125
        self.fx.lab.resources[self.state["id"]]["closing_deadlines"].clear()

    def test_rc2_no_grace_in_the_protection_recovery(self):
        self.grace_start()
        self.bot["_protection_recovery"] = {"policy": "test", "at": self.fx.now}  # R2110RC2-TEST-RECOVERY
        self.fx.lab.step(self.state)
        self.assertEqual(self.state["status"], "PAUSED")

    def test_rc2_the_ceiling_is_monotonic_whatever_the_wall_clock_does(self):
        self.grace_start()
        lab = self.fx.lab
        jump, began, mono0 = {"seconds": 0}, self.fx.now, lab.monotonic()
        # R2.1.11 (correction 15, RC2): separate clocks as in test_okx_timebase.py: the wall one the code could read (the
        # timebase's own and the lab's attribute) jumps back, the monotonic one only goes on
        wall = lambda: self.fx.now + jump["seconds"]
        lab.wall_clock = lab.timebase.wall = wall
        lab.monotonic = lambda: mono0 + (self.fx.now - began)
        with patch.object(lab, "timed_account_check", side_effect=lambda state, bot, api: self.fx.now):
            for _ in range(30):
                lab.step(self.state)
                if self.state["status"] == "PAUSED":
                    break
                if self.fx.now - began == 10:
                    jump["seconds"] = -1000  # the Windows clock set back 1000 s inside the grace
                self.fx.now += 5
        self.assertEqual(self.state["status"], "PAUSED")  # R2110RC2-TEST-MONO: on the monotonic ceiling, not the wall
        self.assertLessEqual(self.fx.now - began, 65)

class Exports(unittest.TestCase):
    """R2110-TESTS: gap_flat, privacy filters, the receipt's size and schema, a nested error body."""

    def test_gap_flat_is_exported_as_a_new_key(self):
        self.assertEqual(safe_data({"gap_flat": 1, "gap_missing": 1}, event_code="feature_book"), {"gap_flat": 1, "gap_missing": "1"})
        self.assertNotIn("gap_flat", safe_data({"gap_flat": -1}, event_code="feature_book"))

    def test_privacy_order_body_keeps_only_its_allowlist(self):
        body = {"instId": "ETH-USDT-SWAP", "side": "buy", "ordType": "post_only", "tdMode": "isolated", "posSide": "net",
                "sz": "0.01", "px": "2250", "reduceOnly": False, "clOrdId": "J5" + "a" * 16 + "0E0001",
                "ordId": "123", "tag": "x", "uid": "42", "expTime": "1"}
        view = safe_data({"request": body, "order_body": body}, event_code="exchange_order_intent")
        self.assertEqual(set(view), {"order_body"})
        self.assertEqual(set(view["order_body"]), {"instId", "side", "ordType", "tdMode", "posSide", "sz", "px", "reduceOnly", "clOrdId"})
        bad = dict(body, instId="ETH-USDT-SWAP?x=1", clOrdId="not-ours", side="all")
        self.assertEqual(set(safe_data({"order_body": bad}, event_code="exchange_order_intent")["order_body"]) & {"instId", "clOrdId", "side"}, set())

    def receipt_zip(self, receipt):
        from tests.test_manifest_receipt import ManifestReceiptTests, rezip
        case = ManifestReceiptTests("test_h_manifest_names_the_code_and_the_receipt_pins_the_manifest")
        case.setUp()
        files = dict(case.files)
        files["manifest-receipt.json"] = receipt(files["manifest.json"])
        return rezip(files)

    def test_receipt_size_and_schema(self):
        from trading_lab.battle_export import verify_archive
        good = lambda m: json.dumps({"schema": "jev-arena-manifest-receipt/v1", "manifest_bytes": len(m),
                                     "manifest_sha256": hashlib.sha256(m).hexdigest()}).encode()
        self.assertTrue(verify_archive(self.receipt_zip(good))["verified"])
        for receipt in (lambda m: good(m)[:-1] + b" " * 5000 + b"}",  # over 4096 bytes
                        lambda m: good(m).replace(b"receipt/v1", b"receipt/v9")):
            with self.assertRaises(ArenaError):
                verify_archive(self.receipt_zip(receipt))

    def test_nested_error_body_under_64_kb_has_no_code(self):
        class Opener:
            def open(self, request, timeout):
                raise urllib.error.HTTPError(request.full_url, 500, "x", {}, io.BytesIO(b"[" * 5000 + b"]" * 5000))
        import urllib.request
        with patch.object(urllib.request, "build_opener", return_value=Opener()):
            with self.assertRaises(PreflightError) as caught:
                send(urllib.request.Request("https://openapi.okx.com/api/v5/account/positions", method="GET"))
        self.assertEqual((str(caught.exception), caught.exception.http_status, caught.exception.okx_code), ("http_500", 500, None))


if __name__ == "__main__":
    unittest.main()
