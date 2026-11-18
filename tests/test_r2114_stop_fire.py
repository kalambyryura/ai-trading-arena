"""R2.1.14 (R2113-STOP-FIRED-LAG, D160, D161; plan team/platform/R2114_PLAN_UA.md, its check team/reviews/R2114_PLAN_CHECK_UA.md).
Our own stop on the venue fires between its GET at the start of an account check and the position GET at its end (season
a037504b, Opus: 15:39:52.476 fired, 15:39:57.206 the position read flat, the whole season paused). The check now reads the stop
again; a venue that does not show it fired yet gets a bounded wait. Strict fakes, temp roots: no network, key, Jev or order."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch
from urllib.parse import urlsplit
import unittest

import test_order_outcomes as outcomes
import test_r2112_mix as mix
from trading_lab import exchange_engine as engine
from trading_lab.battle_review import build_review
from trading_lab.exchange_api import venue_error
from trading_lab.exchange_api import ExchangeFeed
from trading_lab.exchange_lab import STOP_FIRE_WAIT_CHECKS, STOP_FIRE_WAIT_SECONDS, ExchangeLab
from trading_lab.match_journal import restore_events
from trading_lab.okx_readonly import PreflightError

PRICE = outcomes.PRICE
POSITIONS, ALGO, ORDER = "/api/v5/account/positions", "/api/v5/trade/order-algo", "/api/v5/trade/order"
CONFIG, PENDING = "/api/v5/account/config", "/api/v5/trade/orders-pending"


def fire_own_stop(fx, fake):
    """The venue triggers our live stop: the algo turns effective, its market child closes the whole position (long or short)."""
    contracts = Decimal(fake.units)
    parent = next(a for a in fake.algos.values() if a["state"] == "live")
    px, value = (fake.bid if contracts > 0 else fake.ask), Decimal(fake.spec["ctVal"])
    gross, fee = contracts * value * (px - fake.basis), -abs(contracts) * value * px * Decimal(".0005")
    parent.update(state="effective", ordIdList=["100099"], uTime=str(int(fx.now * 1000)), triggerTime=str(int(fx.now * 1000) - 2))
    fake.orders["stop-child"] = {
        "instId": fake.spec["instId"], "tdMode": "isolated", "posSide": "net", "side": "sell" if contracts > 0 else "buy",
        "ordType": "market", "reduceOnly": True, "sz": str(abs(contracts)), "clOrdId": "", "ordId": "100099", "algoId": parent["algoId"],
        "state": "filled", "accFillSz": str(abs(contracts)), "avgPx": str(px), "fee": str(fee), "feeCcy": "USDT",
        "pnl": str(gross), "rebate": "0", "uTime": str(int(fx.now * 1000))}
    fake.units -= contracts
    fake.cash += gross + fee
    fake.bills.append({"billId": "100099", "ts": str(int(fx.now * 1000)), "type": "2", "subType": "3", "ccy": "USDT",
                       "instId": fake.spec["instId"], "ordId": "100099", "pnl": str(gross), "fee": str(fee),
                       "balChg": str(gross + fee), "posBalChg": "0"})


class StopFiredDuringCheck(outcomes.SeasonCase):
    TAKER, DURATION, SIDE = True, 900, "long"

    def setUp(self):
        recipe = json.loads(outcomes.RECIPE.read_text(encoding="utf-8"))
        recipe["rules"]["side"] = self.SIDE
        self.recipes = tempfile.TemporaryDirectory()
        self.addCleanup(self.recipes.cleanup)
        path = Path(self.recipes.name) / "recipe.json"
        path.write_text(json.dumps(recipe), encoding="utf-8")
        with patch.object(outcomes, "RECIPE", path):
            super().setUp()
        self.fired_at, self.hide, self.before_position, self.live_row = None, 0, None, None
        self.after_position, self.hide_child = None, 0
        self.fail_get, self.lag_until, self.lag_units, self.foreign_pending = {}, None, None, None  # RC2 probes
        self.partial_child = 0  # D169: the child shown half filled for so many seconds after the fire
        self.install(self.fx.lab.resources[self.state["id"]]["apis"][0])
        self.run_season(until=lambda: self.bot["position_btc"] != 0 and (self.bot.get("protection") or {}).get("status") == "ARMED", limit=100)
        self.assertEqual((self.bot.get("protection") or {}).get("status"), "ARMED")
        self.fx.now += 5

    def install(self, api):
        """The bot's own client: a hook before the position GET; the venue showing the fired stop live for self.hide seconds."""
        original = api.transport

        def transport(request):
            path = urlsplit(request.full_url).path
            if request.get_method() == "GET" and self.fail_get.get(path):
                self.fail_get[path] -= 1
                raise venue_error("http_503", http_status=503, okx_code="50001")  # transient, A3-eligible
            if path == POSITIONS and self.before_position:
                hook, self.before_position = self.before_position, None
                hook()
            if path == POSITIONS and self.lag_until is not None and self.fx.now < self.lag_until:
                units, self.fake.units = self.fake.units, self.lag_units  # OKX's position view a moment late (R218-A1A3-LAG)
                try:
                    answer = original(request)
                finally:
                    self.fake.units = units
            else:
                answer = original(request)
            if path == PENDING and self.foreign_pending is not None:
                answer = {**answer, "data": list(answer["data"]) + [dict(self.foreign_pending)]}
            if path == POSITIONS and self.after_position:
                hook, self.after_position = self.after_position, None
                hook()  # the venue answered the position GET before the fire
            if (path == ALGO and request.get_method() == "GET" and self.fired_at is not None
                    and self.fx.now < self.fired_at + self.hide):
                answer = {**answer, "data": [deepcopy(self.live_row)]}
            if (path == ORDER and request.get_method() == "GET" and "ordId=100099" in request.full_url
                    and self.fired_at is not None and self.fx.now < self.fired_at + self.hide_child):
                row = {**answer["data"][0], "state": "live", "accFillSz": "0", "avgPx": "", "fee": "0", "pnl": "0"}
                answer = {**answer, "data": [row]}  # the stop's child, its fill not shown yet
            elif (path == ORDER and request.get_method() == "GET" and "ordId=100099" in request.full_url
                    and self.fired_at is not None and self.fx.now < self.fired_at + self.partial_child):
                full = answer["data"][0]
                row = {**full, "state": "partially_filled", "accFillSz": str(Decimal(full["accFillSz"]) / 2),
                       "fee": str(Decimal(full["fee"]) / 2), "pnl": str(Decimal(full["pnl"]) / 2)}
                answer = {**answer, "data": [row]}  # the stop's child half filled so far
            return answer
        api.transport = transport

    def fire(self):
        self.live_row = deepcopy(next(a for a in self.fake.algos.values() if a["state"] == "live"))
        move = Decimal(".994") if self.SIDE == "long" else Decimal("1.006")
        self.fake.bid, self.fake.ask = PRICE * move, PRICE * move + Decimal(".01")
        fire_own_stop(self.fx, self.fake)
        self.fired_at = self.fx.now

    def step(self, n=1, dt=5):
        for _ in range(n):
            self.fx.lab.step(self.state)
            self.fx.now += dt

    def codes(self, *names):
        return [e["code"] for e in self.state["events"] if e["code"] in names]

    def posts(self):
        return [r for r in self.fake.requests if r.get_method() == "POST"]

    def assert_not_paused(self):
        self.assertFalse(self.bot["_manual_review"])
        self.assertFalse(self.events("exchange_check_failed"))
        self.assertFalse(self.state["incomplete"])
        self.assertNotIn(self.state["status"], {"PAUSED"})

    def finish_and_reconcile(self):
        self.run_season()
        self.assertEqual(self.state["status"], "DONE")
        self.assertFalse(self.state["incomplete"])
        self.assertTrue(self.bot["accounting_reconciled"])  # the stop's child and its bill reconcile like any close
        self.assertGreaterEqual(self.bot["trades"], 1)  # the recipe may enter again after its cooldown
        self.assertEqual(self.bot["position_btc"], 0.0)
        self.assertFalse(self.events("closing_paused"))

    # ---- plan test 1: the season a037504b sequence ----
    def test_a037504b_the_stop_fires_between_its_get_and_the_position_get(self):
        self.before_position = self.fire
        self.step()
        self.assert_not_paused()
        order = self.codes("protective_terminal", "protective_child_adopted", "exchange_close_fill", "protective_fired_during_check")
        self.assertEqual(order, ["protective_terminal", "protective_child_adopted", "exchange_close_fill", "protective_fired_during_check"])
        self.assertEqual(self.events("exchange_close_fill")[-1]["data"]["seen_by"], "protection")
        self.assertFalse(self.codes("protective_fire_pending"))
        self.assertEqual(self.events("protective_fired_during_check")[-1]["data"]["seconds_from_venue_fire"], 0.002)  # triggerTime
        self.assertGreaterEqual(self.bot["_next_decision"], self.fired_at + 600)  # the recipe's cooldown from the child's adoption
        self.finish_and_reconcile()
        full = deepcopy(self.fx.lab.load(self.state["id"]))
        restore_events(self.fx.lab.path / (full["id"] + ".events.jsonl"), full, full=True)
        review = build_review(full)
        fired = next(e for e in review["events"] if e["code"] == "protective_fired_during_check")
        self.assertIn("seconds_from_venue_fire", fired["data"])  # exported (plan check p. 8)
        # the trade ledger has no exit_reason; the venue stop's close is the fill seen by the protection read, and it ends trip 1
        close = next(e for e in review["events"] if e["code"] == "exchange_close_fill" and e["data"].get("seen_by") == "protection")
        first = review["participants"][0]["trade_ledger"]["round_trips"][0]
        self.assertEqual((first["close_seq"], first["closed"]), (close["seq"], True))

    # ---- plan test 2: effective shown 1, 2 or 3 checks late ----
    def wait_resolves(self, checks):
        self.hide = checks * 5 - 1
        self.before_position = self.fire
        posts = len(self.posts())
        self.step(checks)
        self.assertEqual(len(self.posts()), posts)  # no close, no new stop, no entry while waiting
        self.assertEqual(self.codes("protective_fire_pending"), ["protective_fire_pending"])
        self.assertFalse(self.codes("protective_fire_resolved"))
        self.assertEqual(self.bot["admission_blocked"], "stop_fire_pending")
        self.step()
        resolved = self.events("protective_fire_resolved")
        self.assertEqual(len(resolved), 1, self.codes("protective_fire_pending", "protective_fire_expired", "exchange_check_failed"))
        self.assertEqual(resolved[0]["data"]["stop_fire_wait_checks"], checks)
        self.assertNotIn("admission_blocked", self.bot)
        self.assertNotIn("_stop_fire_wait", self.bot)
        self.assert_not_paused()
        self.assertFalse(self.events("degraded_window_started"))  # a wait is no account failure (plan check p. 5)
        self.assertGreaterEqual(self.bot["_next_decision"], self.fired_at + 600)
        self.finish_and_reconcile()
        full = deepcopy(self.fx.lab.load(self.state["id"]))
        restore_events(self.fx.lab.path / (full["id"] + ".events.jsonl"), full, full=True)
        exported = {e["code"]: e["data"] for e in build_review(full)["events"]}
        self.assertEqual(exported["protective_fire_resolved"]["stop_fire_wait_checks"], str(checks))  # plan test 11; numbers as text
        self.assertIn("stop_fire_wait_seconds", exported["protective_fire_resolved"])
        self.assertIn("contracts", exported["protective_fire_pending"])

    def test_effective_one_check_late(self):
        self.wait_resolves(1)

    def test_effective_two_checks_late(self):
        self.wait_resolves(2)

    def test_effective_three_checks_late(self):
        self.wait_resolves(3)

    # ---- D164 (plan check p. 6): the second window, the stop fires after the position GET, before ensure's GET ----
    def assert_no_recovery(self):
        self.assertFalse(self.events("protective_recovery"))
        self.assertFalse(self.bot["stop_requested"])
        self.assertNotIn("_protection_recovery", self.bot)
        self.assert_not_paused()

    def test_the_second_window_the_stop_fires_after_the_position_get(self):
        self.after_position = self.fire
        self.step()
        self.assert_no_recovery()
        order = self.codes("protective_terminal", "protective_child_adopted", "exchange_close_fill", "protective_fired_during_check")
        self.assertEqual(order[-3:], ["protective_child_adopted", "exchange_close_fill", "protective_fired_during_check"])
        self.assertEqual(self.events("exchange_close_fill")[-1]["data"]["seen_by"], "protection")
        self.assertEqual(self.bot["protection"]["status"], "FLAT")
        self.assertGreaterEqual(self.bot["_next_decision"], self.fired_at + 600)  # the recipe's cooldown from the child
        self.finish_and_reconcile()
        self.assertFalse(self.events("protective_recovery"))

    def test_the_second_window_with_the_child_fill_not_shown_yet_waits(self):
        self.hide_child = 9
        self.after_position = self.fire
        self.step()
        self.assert_no_recovery()
        self.assertEqual(self.codes("protective_fire_pending"), ["protective_fire_pending"])
        self.step(2)
        self.assertEqual(len(self.events("protective_fire_resolved")), 1)
        self.assert_no_recovery()
        self.finish_and_reconcile()

    def test_the_venue_shows_the_stop_fired_before_its_child_fill_and_the_bot_waits(self):
        """The check's first read already shows the stop effective, its child's fill not yet; the position is flat."""
        self.hide_child = 9
        self.fire()
        self.step()
        self.assertEqual(self.codes("protective_fire_pending"), ["protective_fire_pending"])
        self.assertFalse(self.bot["_manual_review"])
        self.step(2)
        self.assertEqual(len(self.events("protective_fire_resolved")), 1)
        self.assert_no_recovery()
        self.finish_and_reconcile()

    def test_the_second_window_with_an_own_open_order_keeps_the_recovery(self):
        """An open order of ours besides the stop (here an entry the venue shows live): the recovery, as before D164."""
        self.open_entry()
        self.after_position = self.fire
        self.step()
        self.assertFalse(self.events("protective_fired_during_check"))
        self.assertTrue(self.events("protective_recovery"))
        self.assertTrue(self.bot["stop_requested"])

    # ---- RC2 (team/reviews/R2114_REVIEW_UA.md) ----
    def close_posts(self, since):
        return [r for r in self.posts()[since:] if urlsplit(r.full_url).path == ORDER]

    def assert_no_wait_left(self):
        self.assertNotIn("_stop_fire_wait", self.bot)
        self.assertNotEqual(self.bot.get("admission_blocked"), "stop_fire_pending")

    def by_hand_close_and_reopen(self, cancel):
        """P1a/P1b: a by-hand full close while our stop stays live, then a reopen of the same size by hand before the next
        check (and, P1b, OKX taking our stop off at that close: cxlOnClosePos)."""
        held = Decimal(self.fake.units)

        def by_hand_close():
            self.fake.units = Decimal(0)
        self.before_position = by_hand_close
        self.step()
        self.assertEqual(self.codes("protective_fire_pending"), ["protective_fire_pending"])
        self.fake.units = held
        if cancel:
            next(a for a in self.fake.algos.values() if a["state"] == "live")["state"] = "canceled"
        posts = len(self.posts())
        self.step(3)
        self.assertFalse(self.events("protective_fire_resolved"))
        self.assert_manual_review()
        self.assertEqual(self.events("protective_fire_expired")[-1]["data"]["outcome"], "stop_not_fired")
        self.assert_no_wait_left()
        self.assertEqual(self.close_posts(posts), [])  # never a close of the reopened (foreign) position

    def test_rc01_a_by_hand_close_and_reopen_while_our_stop_stays_live(self):
        self.by_hand_close_and_reopen(False)

    def test_rc01_a_by_hand_close_and_reopen_while_okx_takes_our_stop_off(self):
        self.by_hand_close_and_reopen(True)

    def test_rc01_a_position_read_glitch_is_no_fire_of_our_stop(self):
        def glitch():
            self.fake.position_lag, self.fake.lagged_units = 1, Decimal(0)  # one position read answers flat
        self.before_position = glitch
        self.step()
        self.assertFalse(self.codes("protective_fired_during_check", "protective_fire_pending", "protective_fire_resolved"))
        self.assert_manual_review()  # as on R2.1.13

    def lagged_fire(self):
        held = Decimal(self.fake.units)
        self.fire()
        self.lag_units, self.lag_until = held, self.fx.now + 1  # this step's later position reads still show the old size

    def assert_lag_waited(self):
        self.assertFalse(self.bot["_manual_review"], self.codes("protective_fire_pending", "protective_fire_expired", "exchange_check_failed"))
        self.assertEqual(self.codes("protective_fire_pending"), ["protective_fire_pending"])
        pending = self.events("protective_fire_pending")[0]["data"]
        self.assertEqual((Decimal(pending["stop_fire_own_contracts"]), Decimal(pending["stop_fire_venue_contracts"]) != 0), (0, True))
        self.step()
        self.assertEqual(len(self.events("protective_fire_resolved")), 1)
        self.assert_no_recovery()
        self.finish_and_reconcile()

    def test_rc02_the_second_window_with_a_lagging_position_view_waits(self):
        self.after_position = self.lagged_fire  # P3: the D164 window, OKX's position view 1 s behind the child's fill
        self.step()
        self.assert_lag_waited()

    def test_rc02_the_first_window_masked_by_a_lagging_position_view_waits(self):
        self.before_position = self.lagged_fire  # P3b: the a037504b race, the position view a moment late
        self.step()
        self.assert_lag_waited()

    def third_size_fire(self):
        """RC2-T1 (R2.1.14 RC2 review): the stop fires, its child is adopted, and OKX's position view then shows a third
        size: neither the bot's own (flat) nor the one before the child's fill."""
        held = Decimal(self.fake.units)
        self.fire()
        self.lag_units, self.lag_until = held * 2, self.fx.now + 1

    def assert_third_size_is_the_manual_review_at_once(self):
        posts = len(self.posts())
        self.step()
        self.assertFalse(self.codes("protective_fire_pending"))
        self.assert_manual_review()
        self.assertEqual(len(self.posts()), posts)  # no order, no stop, no cancel on that check

    def test_rc2_t1_a_third_size_after_a_proven_fire_in_the_second_window(self):
        self.after_position = self.third_size_fire
        self.assert_third_size_is_the_manual_review_at_once()

    def test_rc2_t1_a_third_size_after_a_proven_fire_in_the_first_window(self):
        self.before_position = self.third_size_fire
        self.assert_third_size_is_the_manual_review_at_once()

    def test_rc02_an_old_fired_stop_explains_no_later_position_of_its_size(self):
        """The venue-behind wait is only for a child adopted within STOP_FIRE_WAIT_SECONDS: a by-hand position of the same
        size two minutes after our stop's fire is the manual review at once."""
        held = Decimal(self.fake.units)
        self.before_position = self.fire
        self.step()
        self.assertTrue(self.events("protective_fired_during_check"))
        self.fx.now += 120
        self.fake.units = held  # opened by hand on the bot's subaccount
        self.step()
        self.assertFalse(self.codes("protective_fire_pending"))
        self.assert_manual_review()

    def test_rc02_the_second_window_with_the_child_and_the_position_view_both_late_waits(self):
        """Ensure sees the stop fired; its child reads unfilled and the position view still shows the old size."""
        held = Decimal(self.fake.units)
        self.hide_child = 9

        def late():
            self.fire()
            self.lag_units, self.lag_until = held, self.fx.now + 1
        self.after_position = late
        self.step()
        self.assert_no_recovery()
        self.assertEqual(self.codes("protective_fire_pending"), ["protective_fire_pending"])
        self.step(2)
        self.assertEqual(len(self.events("protective_fire_resolved")), 1)
        self.assert_no_recovery()
        self.finish_and_reconcile()

    def test_d169_a_child_filled_below_the_protected_size_is_no_proof_yet(self):
        """D169 p. 1: the proof is the child filled exactly on the size the stop protected; half of it, with the position view
        showing that half too, keeps the bot waiting, never protective_fired_during_check."""
        held = Decimal(self.fake.units)

        def half():
            self.fire()
            self.partial_child = 9
            self.lag_units, self.lag_until = held / 2, self.fx.now + 9
        self.after_position = half
        self.step()
        self.assertEqual(self.codes("protective_fire_pending"), ["protective_fire_pending"])
        self.assertFalse(self.codes("protective_fired_during_check", "protective_fire_resolved"))
        self.assert_no_recovery()
        self.step(2)
        self.assertEqual(len(self.events("protective_fire_resolved")), 1)
        self.assert_no_recovery()
        self.finish_and_reconcile()

    def test_d169_the_reread_shows_the_child_still_filling_and_the_bot_waits(self):
        """The first position read is flat; after the stop's re-read its child shows half filled and the view agrees on
        that half: no proof yet, the bot waits; the rest fills by the next check."""
        held = Decimal(self.fake.units)

        def half():
            self.fire()
            self.partial_child = 4
            self.fake.units = held / 2  # the venue: half of the child filled so far
            self.fake.position_lag, self.fake.lagged_units = 1, Decimal(0)  # this check's first position read: flat
        self.before_position = half
        self.step()
        self.assertEqual(self.codes("protective_fire_pending"), ["protective_fire_pending"])
        self.assertFalse(self.codes("protective_fired_during_check"))
        self.assertFalse(self.bot["_manual_review"])
        self.fake.units = Decimal(0)  # the rest of the child filled
        self.step()
        self.assertEqual(len(self.events("protective_fire_resolved")), 1)
        self.finish_and_reconcile()

    def test_d169_a_proven_fire_past_the_limit_stops_only_this_bot(self):
        """D169 p. 2: the child proven, OKX's position view still behind at the window's limit: only this bot stops (the
        recovery, as R2.1.13 for this fire), no manual review, no general Stop, no pause; it settles once the view agrees."""
        held = Decimal(self.fake.units)

        def lagged():
            self.fire()
            self.lag_units, self.lag_until = held, self.fx.now + 17  # behind for the four checks of the window
        self.after_position = lagged
        self.step(STOP_FIRE_WAIT_CHECKS)
        self.assertEqual(self.events("protective_fire_expired")[-1]["data"]["outcome"], "wait_limit")
        self.assertTrue(self.events("protective_recovery"))
        self.assertEqual([e["data"]["reason_code"] for e in self.events("stop_requested") if e.get("bot_id") == self.bot["id"]],
                         ["protection_unverified"])
        self.assertFalse(self.bot["_manual_review"])
        self.assertFalse(self.events("exchange_check_failed"))
        self.run_season()
        self.assertEqual(self.state["status"], "DONE")
        self.assertFalse(self.events("closing_paused"))
        self.assertFalse(self.bot["_manual_review"])
        self.assertTrue(self.bot["accounting_reconciled"])
        self.assertTrue(self.state["incomplete"])  # the recovery, as on R2.1.13

    def test_d169_a_position_view_still_behind_after_the_limit_is_the_manual_review(self):
        """The record whose wait reached its limit explains nothing more: a view still behind at the next check is the manual
        review, as on R2.1.13 (a lag that long is no lag)."""
        held = Decimal(self.fake.units)

        def lagged():
            self.fire()
            self.lag_units, self.lag_until = held, self.fx.now + 10 ** 6
        self.after_position = lagged
        self.step(STOP_FIRE_WAIT_CHECKS + 1)
        self.assertEqual(len(self.events("protective_fire_pending")), 1)
        self.assert_manual_review()

    def test_rc03_a_transient_failure_inside_the_wait_opens_no_extension_and_sends_nothing(self):
        self.hide = 10 ** 6
        self.before_position = self.fire
        self.step()
        posts = len(self.posts())
        self.fail_get[CONFIG] = 1  # P2: the next check's identity GET answers 503 / 50001
        self.step()
        self.assertEqual([e["data"]["extended"] for e in self.events("degraded_window_started")], [False])
        self.assertFalse(self.events("exit_trigger"))
        self.assertEqual(self.posts()[posts:], [])
        self.hide = 0
        self.step(2)
        self.assertEqual(len(self.events("protective_fire_resolved")), 1)
        self.assertFalse(self.bot["_manual_review"])

    def test_rc05_a_stop_canceled_inside_the_wait_leaves_no_wait_behind(self):
        def by_hand_close():
            self.fake.units = Decimal(0)
        self.before_position = by_hand_close
        self.step()
        self.assertEqual(self.codes("protective_fire_pending"), ["protective_fire_pending"])
        next(a for a in self.fake.algos.values() if a["state"] == "live")["state"] = "canceled"  # P5: OKX took our stop off
        self.step()
        self.assertTrue(self.bot["_manual_review"])
        self.assertEqual(self.events("protective_fire_expired")[-1]["data"]["outcome"], "other_facts")
        self.assert_no_wait_left()

    def test_rc05_a_manual_review_by_another_path_ends_the_wait(self):
        self.hide = 10 ** 6
        self.before_position = self.fire
        self.step()
        self.foreign_pending = {"instId": self.fake.spec["instId"], "clOrdId": "by-hand-1", "ordId": "900009", "state": "live"}
        self.step()
        self.assertTrue(self.bot["_manual_review"])
        self.assertEqual(self.events("exchange_check_failed")[-1]["data"]["reason"], "foreign_or_unbounded_pending_orders")
        self.assertEqual(self.events("protective_fire_expired")[-1]["data"]["outcome"], "manual_review")
        self.assert_no_wait_left()

    # ---- plan test 3: the limit ----
    def assert_manual_review(self):
        self.assertTrue(self.bot["_manual_review"])
        self.assertEqual(self.events("exchange_check_failed")[-1]["data"]["reason"], "actual_position_differs_from_owned_fills")
        self.assertTrue(self.state["incomplete"])

    def test_effective_never_shown_ends_in_the_manual_review_after_the_checks(self):
        self.hide = 10 ** 6
        self.before_position = self.fire
        self.step(STOP_FIRE_WAIT_CHECKS)
        expired = self.events("protective_fire_expired")
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0]["data"]["stop_fire_wait_checks"], STOP_FIRE_WAIT_CHECKS)
        self.assert_manual_review()
        self.assertNotIn("_stop_fire_wait", self.bot)
        self.assertNotEqual(self.bot.get("admission_blocked"), "stop_fire_pending")

    def test_effective_never_shown_ends_in_the_manual_review_after_the_seconds(self):
        self.hide = 10 ** 6
        self.before_position = self.fire
        self.step()
        self.fx.now += STOP_FIRE_WAIT_SECONDS
        self.step()
        expired = self.events("protective_fire_expired")
        self.assertEqual(expired[0]["data"]["stop_fire_wait_checks"], 2)
        self.assertGreaterEqual(expired[0]["data"]["stop_fire_wait_seconds"], STOP_FIRE_WAIT_SECONDS)
        self.assert_manual_review()

    def test_the_last_check_reads_the_stop_before_it_applies_the_limit(self):
        """Plan check p. 2: the venue shows the fired stop between the start of the last check and its position GET."""
        self.hide = 10 ** 6
        self.before_position = self.fire
        self.step(STOP_FIRE_WAIT_CHECKS - 1)
        self.fx.now += STOP_FIRE_WAIT_SECONDS  # the seconds are past as well

        def shown():
            self.hide = 0
        self.before_position = shown
        self.step()
        resolved = self.events("protective_fire_resolved")
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["data"]["stop_fire_wait_checks"], STOP_FIRE_WAIT_CHECKS - 1)
        self.assertFalse(self.events("protective_fire_expired"))
        self.assert_not_paused()

    def test_a_wait_spends_no_failure_and_keeps_an_earlier_failure_count(self):
        """Plan check p. 5: no account failure, no degraded window, the count neither grows nor resets."""
        self.bot["_api_failures"] = 1
        self.hide = 10 ** 6
        self.before_position = self.fire
        self.step(2)
        self.assertEqual(self.bot["_api_failures"], 1)
        self.assertFalse(self.events("exchange_check_failed"))
        self.assertFalse(self.events("degraded_window_started"))
        self.assertEqual(self.bot["admission_blocked"], "stop_fire_pending")

    def test_a_wait_belongs_to_its_own_stop_record(self):
        self.hide = 10 ** 6
        self.before_position = self.fire
        self.step()
        self.bot["_stop_fire_wait"]["algo_client_id"] = "J5-another-stop"  # the window was opened for another record
        self.step()
        self.assertEqual(len(self.events("protective_fire_pending")), 1)
        self.assert_manual_review()

    # ---- plan test 4: any other size is the manual review at once ----
    def test_a_foreign_partial_close_is_the_manual_review_at_once(self):
        def foreign():
            self.fake.units -= Decimal(self.fake.units) / 2
        self.before_position = foreign
        self.step()
        self.assertFalse(self.codes("protective_fire_pending", "protective_fired_during_check"))
        self.assert_manual_review()

    def open_entry(self):
        """An entry of ours the venue shows live (resting), besides the stop."""
        entry = deepcopy(next(o for o in self.bot["_orders"] if not o["request"]["reduceOnly"]))
        entry["request"]["clOrdId"] = entry["request"]["clOrdId"][:-1] + "9"
        entry["result"] = None
        venue = deepcopy(next(o for o in self.fake.orders.values() if o.get("clOrdId") == self.bot["_orders"][0]["request"]["clOrdId"]))
        venue.update(clOrdId=entry["request"]["clOrdId"], ordId="100077", state="live", accFillSz="0", avgPx="", fee="0", pnl="0")
        self.fake.orders[entry["request"]["clOrdId"]] = venue
        self.bot["_orders"].append(entry)

    def test_an_own_open_order_besides_the_stop_gets_no_window(self):
        """Plan p. 1.1: a resting order of ours (here an entry the venue shows live) may still fill: no wait, as before."""
        self.open_entry()
        self.hide = 10 ** 6
        self.before_position = self.fire
        self.step()
        self.assertFalse(self.codes("protective_fire_pending"))
        self.assertTrue(self.bot["_manual_review"])
        self.assertEqual(self.events("exchange_check_failed")[-1]["data"]["reason"], "actual_position_differs_from_owned_fills")

    def test_a_stop_with_a_pending_amendment_gets_no_window(self):
        stored = next(p for p in self.bot["_protections"] if not p.get("terminal"))
        stored["pending_amend"] = dict(stored["body"], slTriggerPx=str(Decimal(stored["body"]["slTriggerPx"]) + Decimal("0.5")))
        self.hide = 10 ** 6
        self.before_position = self.fire
        self.step()
        self.assertFalse(self.codes("protective_fire_pending"))
        self.assertTrue(self.bot["_manual_review"])

    # ---- plan test 5: a Resume hours later, the limit long past ----
    def test_a_resume_hours_later_resolves_the_open_window(self):
        self.hide = 10 ** 6
        self.before_position = self.fire
        self.step()
        self.assertIn("_stop_fire_wait", self.bot)
        fx, old = self.fx, self.fx.lab
        old.save(self.state)
        old.close()
        fx.lab = ExchangeLab(fx.root, fx.workshop, fx.arena, clock=fx.clock, monotonic=fx.clock, background=False,
                             api_factory=lambda p: fx.fakes[p["id"]].api(p["environment"]),
                             jev_factory=Mock(side_effect=AssertionError("No Jev in recovery")),
                             feed_factory=lambda api: ExchangeFeed(api, clock=fx.clock))
        fx.now += 3 * 3600
        self.hide = 0  # the venue shows the fired stop now
        fx.lab.resume(fx.lab.data["generation"], self.state["id"])
        self.state = fx.lab.sessions[self.state["id"]]
        self.bot = self.state["bots"][0]
        fx.lab.prepare(self.state, fx.lab.chosen_profiles(["arena-a"]), True)
        fx.lab.resources[self.state["id"]]["wakeup"].wait = lambda seconds: False
        self.install(fx.lab.resources[self.state["id"]]["apis"][0])
        self.run_season()
        resolved = self.events("protective_fire_resolved")
        self.assertEqual(len(resolved), 1)
        self.assertGreater(resolved[0]["data"]["stop_fire_wait_seconds"], STOP_FIRE_WAIT_SECONDS)  # read first, then the limit
        self.assertFalse(self.events("protective_fire_expired"))
        self.assertFalse(self.bot["_manual_review"])
        self.assertEqual(self.state["status"], "DONE")

    # ---- plan test 7: a stop read canceled or failed gets no window ----
    def test_a_stop_cancelled_by_a_manual_close_gets_no_window(self):
        def manual_close():  # closed by hand in the OKX app: OKX takes the stop off with the position (cxlOnClosePos)
            self.fake.units = Decimal(0)
            next(a for a in self.fake.algos.values() if a["state"] == "live")["state"] = "canceled"
        self.before_position = manual_close
        self.step()
        self.assertFalse(self.codes("protective_fire_pending"))
        self.assertTrue(self.events("protective_recovery"))
        self.assert_manual_review()

    # ---- plan test 8: the race in closing() ----
    def test_a_local_stop_whose_close_meets_the_fired_stop_waits_and_resolves(self):
        self.hide = 9
        self.fake.bid, self.fake.ask = PRICE * Decimal(".994"), PRICE * Decimal(".994") + Decimal(".01")  # the recipe's 50 bps stop

        def fire_on_close():
            self.fire()
        self.fake.before_close = fire_on_close  # the stop fires between closing()'s reconcile and its POST
        self.run_season(until=lambda: bool(self.events("protective_fire_resolved")) or self.bot["_manual_review"], limit=40)
        self.assertEqual([code for _, code in self.fake.refused], ["51169"])
        self.assertTrue(self.events("protective_fire_pending"))
        self.assertTrue(self.events("protective_fire_resolved"))
        self.assertFalse(self.events("closing_paused"))
        self.assert_not_paused()
        self.finish_and_reconcile()


class ShortStopFiredDuringCheck(StopFiredDuringCheck):
    """Plan test 9: a short position, a buy stop; the same sequences. The long-only cases stay with the parent."""
    SIDE = "short"


class StopFireProof(unittest.TestCase):
    """RC2 (RC-01, D169 p. 1): the proof of our own stop's fire is both its fired state and adopted children of it filled
    exactly on the size it protected."""

    def test_the_proof_needs_the_fired_state_and_a_filled_child_of_the_record(self):
        child = {"_protective_parent": "J8own", "result": {"contracts": "0.04"}}
        cases = (("effective", [child], True), ("partially_effective", [child], True),
                 ("live", [child], False), ("canceled", [child], False), ("order_failed", [child], False),
                 ("effective", [{**child, "result": {"contracts": "0"}}], False), ("effective", [{**child, "result": None}], False),
                 ("effective", [], False), ("effective", [{**child, "_protective_parent": "J8other"}], False),
                 ("effective", [{**child, "result": {"contracts": "0.02"}}], False),
                 ("effective", [{**child, "result": {"contracts": "0.02"}}, {**child, "result": {"contracts": "0.02"}}], True),
                 ("effective", [child, {**child, "result": {"contracts": "0.02"}}], False))
        for state, orders, proven in cases:
            record = {"body": {"algoClOrdId": "J8own", "sz": "0.04"}, "proof": {"state": state}}
            self.assertEqual(ExchangeLab.stop_fire_proof(None, {"_orders": orders}, record), proven, (state, orders))


class StepOrderInAMixedSeason(unittest.TestCase):
    """Plan check p. 5: the wait goes first in the step's failure branch, before the A3 contract re-read of a mixed season
    (a held position whose quote is not fresh) and before account_failure. A control run proves the branch is reached.
    The mixed fixture of R2.1.12 by composition: its own tests are not run again here."""

    def setUp(self):
        self.mix = mix.MixedSeason("test_mix_a_snapshot_of_another_contract_is_refused")
        self.mix.setUp()
        self.addCleanup(self.mix.doCleanups)

    def one_step(self, error):
        lab = self.mix.fx.lab
        with patch.object(lab, "timed_account_check", side_effect=PreflightError(error)),                 patch("trading_lab.exchange_lab.fresh_market", return_value=False),                 patch.object(lab, "reread_contract", wraps=lab.reread_contract) as reread,                 patch.object(lab, "account_failure", wraps=lab.account_failure) as failure:
            lab.step(self.mix.state)
        self.mix.fx.now += 5
        return reread.call_count, failure.call_count

    def test_the_wait_goes_before_the_contract_reread_and_the_account_failure(self):
        m = self.mix
        m.start()
        m.steps(2)
        held = m.e["_contracts"]
        m.e["_contracts"] = "1"  # these steps only: the A3 re-read's case
        try:
            self.assertEqual(self.one_step("stop_fire_pending"), (0, 0))
            self.assertFalse(m.events("exchange_check_failed"))
            reread, failure = self.one_step("network_error")  # control: the same step with a transient code
            self.assertGreaterEqual(reread, 1)
            self.assertEqual(failure, 2)
        finally:
            m.e["_contracts"] = held


if __name__ == "__main__":
    unittest.main()
