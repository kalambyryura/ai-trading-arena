"""R2.1.5: a refused or never created order is terminal, never a season frozen in PAUSED; a local stop-loss that meets an
owned stop already fired takes the venue's child; the recipe's cooldown follows an exit by the owned stop. R2.1.6 (the
independent review of R2.1.5, team/reviews/R215_REVIEW_UA.md): late facts of a sent order are no foreign position (F1),
only listed codes are refusals and an order recorded as not created that shows up is reopened (F2), the settlement does
not re-read cancelled unfilled orders OKX has dropped (F3), and a refused close waits a growing pause and does not count
against the limits (F4). OKX Live season (RULES_ONLY, ETH long, one lot) on the fake exchange and temp roots only: no
network, key, Jev call or real order."""
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import test_exchange_lab as fixture  # noqa: E402
from test_post_only import RestingFake  # noqa: E402
from trading_lab import exchange_engine as engine, exchange_execution as policy  # noqa: E402
from trading_lab.exchange_api import CancelFailed, CancelRefused, OrderAbsent, OrderRejected  # noqa: E402
from trading_lab.exchange_lab import ABSENT_READS, ABSENT_SECONDS  # noqa: E402
from trading_lab.okx_readonly import PreflightError  # noqa: E402

RECIPE = ROOT / "03_For_Client_Deliverables" / "R2_DEMO_ETH_POST_ONLY_LONG.json"  # long, post-only, SL 50, cooldown 600
PRICE = Decimal(2250)


class OutcomeFake(RestingFake):
    """RestingFake plus OKX's answers this release handles: a refused POST (code "1", a 51xxx sCode for our client ID
    and an empty ordId, no order created), a reduce-only order with no position refused, a GET of a client ID that
    never became an order (51603), and a POST whose answer is lost on the way."""
    reject_entry, lose_entry, lose_entry_after_create, before_close = None, False, False, None
    # R2.1.6 review scenarios: a POST created on the venue but answered with a code (refuse_but_create), or whose answer
    # is lost (lose_created: "entry" or "close") and whose GET then answers 51603 for lag reads, or that GET and
    # orders-pending hide for hide seconds; closes refused with refuse_close while set; cancelled unfilled orders dropped
    # from GET after purge_after seconds (OKX keeps them about 2 hours).
    refuse_but_create, lose_created, lag, hide, refuse_close, purge_after = None, None, 0, 0, None, None

    def transport(self, request):
        route, method = urlsplit(request.full_url), request.get_method()
        if not hasattr(self, "lagging"):
            self.lagging, self.hidden = {}, {}
        if route.path == "/api/v5/trade/order" and method == "POST":
            body = json.loads(request.data)
            if body["reduceOnly"] and self.refuse_close:
                self.requests.append(request)
                self.refused.append((body["clOrdId"], self.refuse_close))
                return {"code": "1", "data": [{"sCode": self.refuse_close, "sMsg": "", "clOrdId": body["clOrdId"], "ordId": ""}]}
            if not body["reduceOnly"] and self.refuse_but_create:
                code, self.refuse_but_create = self.refuse_but_create, None
                super().transport(request)  # the venue did create it
                return {"code": "1", "data": [{"sCode": code, "sMsg": "", "clOrdId": body["clOrdId"], "ordId": ""}]}
            if self.lose_created == ("close" if body["reduceOnly"] else "entry"):
                self.lose_created = None
                super().transport(request)
                self.lagging[body["clOrdId"]] = self.lag
                if self.hide:
                    self.hidden[body["clOrdId"]] = self.clock() + self.hide
                raise PreflightError("network_error")
            if body["reduceOnly"] and self.before_close:
                hook, self.before_close = self.before_close, None
                hook()  # e.g. the owned stop fires while our close is on its way
            refused = None
            if not body["reduceOnly"] and self.reject_entry:
                refused, self.reject_entry = self.reject_entry, None
            elif body["reduceOnly"] and not self.units:
                refused = "51169"
            if refused:
                self.requests.append(request)
                self.refused.append((body["clOrdId"], refused))
                return {"code": "1", "msg": "All operations failed",
                        "data": [{"sCode": refused, "sMsg": "", "clOrdId": body["clOrdId"], "ordId": ""}]}
            if not body["reduceOnly"] and self.lose_entry:
                self.lose_entry = False
                self.requests.append(request)
                raise PreflightError("network_error")  # lost before the venue: no order
            if not body["reduceOnly"] and self.lose_entry_after_create:
                self.lose_entry_after_create = False
                super().transport(request)  # the venue created it; its answer is lost
                raise PreflightError("network_error")
        if route.path == "/api/v5/trade/order" and method == "GET":
            query = parse_qs(route.query)
            ident = query.get("clOrdId", [None])[0]
            row = self.orders.get(ident)
            if ident is not None and self.lagging.get(ident, 0) > 0:
                self.lagging[ident] -= 1
                row = None
            if row is not None and self.hidden.get(ident, 0) > self.clock():
                row = None
            if (row is not None and self.purge_after and row["state"] == "canceled" and not Decimal(row["accFillSz"] or "0")
                    and self.clock() - int(row["uTime"]) / 1000 > self.purge_after):
                row = None
            if ident is not None and row is None:
                self.requests.append(request)
                return {"code": "51603", "msg": "Order does not exist", "data": []}
        if route.path == "/api/v5/trade/orders-pending":
            payload = super().transport(request)
            payload["data"] = [r for r in payload["data"] if self.hidden.get(r["clOrdId"], 0) <= self.clock()]
            return payload
        if route.path == "/api/v5/account/positions" and getattr(self, "position_lag", 0):
            self.position_lag -= 1  # R2.1.9: OKX's position view not yet showing a fill
            units, self.units = self.units, self.lagged_units
            try:
                return super().transport(request)
            finally:
                self.units = units
        return super().transport(request)


def fire_stop(fx, fake):
    """Our live stop on the venue triggers: a filled reduce-only market child closes the whole position."""
    contracts = Decimal(fake.units)
    parent = next(a for a in fake.algos.values() if a["state"] == "live")
    px, value = fake.bid, Decimal(fake.spec["ctVal"])
    gross, fee = contracts * value * (px - fake.basis), -contracts * value * px * Decimal(".0005")
    parent.update(state="effective", ordIdList=["100099"])
    fake.orders["stop-child"] = {
        "instId": fake.spec["instId"], "tdMode": "isolated", "posSide": "net", "side": "sell", "ordType": "market",
        "reduceOnly": True, "sz": str(contracts), "clOrdId": "", "ordId": "100099", "algoId": parent["algoId"],
        "state": "filled", "accFillSz": str(contracts), "avgPx": str(px), "fee": str(fee), "feeCcy": "USDT",
        "pnl": str(gross), "rebate": "0", "uTime": str(int(fx.now * 1000))}
    fake.units -= contracts
    fake.cash += gross + fee
    fake.bills.append({"billId": "100099", "ts": str(int(fx.now * 1000)), "type": "2", "subType": "3", "ccy": "USDT",
                       "instId": fake.spec["instId"], "ordId": "100099", "pnl": str(gross), "fee": str(fee),
                       "balChg": str(gross + fee), "posBalChg": "0"})


class SeasonCase(unittest.TestCase):
    TAKER, DURATION = False, 300

    def setUp(self):
        self.fx = fixture.ExchangeTests(methodName="runTest")
        self.fx.setUp()
        for fake in self.fx.fakes.values():
            fake.__class__ = OutcomeFake
            fake.use(fixture.ETH_SPEC, PRICE, 0)
            fake.refused = []
        self.fx.lab.jev_factory = Mock(side_effect=AssertionError("No Jev for RULES_ONLY"))
        shop = self.fx.workshop
        recipe = json.loads(RECIPE.read_text(encoding="utf-8"))
        if self.TAKER:  # an IOC entry instead of the resting post-only one
            recipe["rules"]["order"] = {"type": "taker"}
        version = shop.add_version(shop.data["generation"], recipe)["id"]
        shop.train(shop.data["generation"], version, "synthetic-suite-v2", 100)
        shop.freeze(shop.data["generation"], version)
        self.promoted = shop.promote(shop.data["generation"], version)["id"]
        lab = self.fx.lab
        self.state = lab.load(lab.start(lab.data["generation"], "arena_live", [self.promoted], ["arena-a"], 25, self.DURATION,
                                        execution_mode=policy.RULES_ONLY)["id"])
        lab.prepare(self.state, lab.chosen_profiles(["arena-a"]), False)
        lab.resources[self.state["id"]]["wakeup"].wait = lambda seconds: False
        self.bot, self.fake, self.start = self.state["bots"][0], self.fx.fakes["arena-a"], self.fx.now

    def tearDown(self):
        resource = self.fx.lab.resources.pop(self.state["id"], None)
        if resource:
            resource["stack"].close()
        self.fx.tearDown()

    def run_season(self, act=lambda t: None, until=None, limit=400, dt=5):
        for _ in range(limit):
            act(self.fx.now - self.start)
            self.fx.lab.step(self.state)
            if self.state["status"] in {"DONE", "PAUSED"} or (until and until()):
                break
            self.fx.now += dt

    def events(self, code):
        return [e for e in self.state["events"] if e["code"] == code]

    def fill_resting(self, t, marks):
        live = [o for o in self.fake.orders.values() if o.get("ordType") == "post_only" and o["state"] == "live"]
        if live and "fill" not in marks:
            self.fake.fill(live[0]["clOrdId"], Decimal(live[0]["sz"]))
            marks["fill"] = t

    def assert_never_frozen(self):
        self.assertEqual(self.state["status"], "DONE")
        self.assertFalse(self.events("closing_paused"))
        self.assertFalse(self.events("exchange_check_failed"))
        self.assertEqual(engine.unresolved(self.bot), [])
        # The next Live season (another subaccount) is accepted: no unfinished run holds the arena.
        lab = self.fx.lab
        lab.start(lab.data["generation"], "arena_live", [self.promoted], ["arena-b"], 25, 300, execution_mode=policy.RULES_ONLY)


class LiveSeasonOutcomes(SeasonCase):
    def test_a_refused_entry_is_terminal_spends_its_attempt_and_waits_the_cooldown(self):
        self.fake.reject_entry = "51008"  # insufficient balance or margin
        self.run_season()
        self.assert_never_frozen()
        refused = self.events("exchange_order_rejected")
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]["data"]["rejected_code"], "51008")
        posted = self.events("exchange_order_posted")[0]["data"]
        self.assertEqual((posted["outcome"], posted["rejected_code"]), ("rejected", "51008"))
        order = self.bot["_orders"][0]
        self.assertEqual(order["not_created"]["kind"], "rejected")
        self.assertEqual(self.bot["_entry_attempts"], 1)
        self.assertGreaterEqual(self.bot["_next_decision"], order["at"] + 600)  # no new entry before the recipe's cooldown
        self.assertEqual(len([o for o in self.fake.orders.values() if o.get("ordType") == "post_only"]), 0)
        self.assertTrue(self.bot["accounting_reconciled"])

    def test_a_local_stop_loss_on_a_fired_owned_stop_takes_its_child_and_sends_no_close(self):
        marks = {}

        def act(t):
            self.fill_resting(t, marks)
            if ("fill" in marks and "race" not in marks and self.bot.get("protection", {}).get("status") == "ARMED"
                    and t - marks["fill"] >= 20):
                def fire():  # after this step's account check, before its market snapshot
                    self.fake.bid, self.fake.ask = PRICE * Decimal(".994"), PRICE * Decimal(".994") + Decimal(".01")
                    fire_stop(self.fx, self.fake)
                self.fake.before_ticker = fire
                marks["race"] = t
        self.run_season(act)
        self.assert_never_frozen()
        self.assertEqual([e["data"]["exit_reason"] for e in self.events("exit_trigger")], ["stop_loss"])
        self.assertTrue(self.events("protective_child_adopted"))
        self.assertEqual(self.fake.refused, [])  # closing() read the fired stop first: no reduce-only order for nothing
        self.assertEqual([o for o in self.fake.orders.values() if o.get("reduceOnly") is True and o.get("clOrdId")], [])
        self.assertEqual(self.bot["trades"], 1)
        self.assertTrue(self.bot["accounting_reconciled"])

    def test_a_close_refused_because_the_stop_fired_on_its_way_takes_the_actual_state(self):
        marks = {}

        def act(t):
            self.fill_resting(t, marks)
            if ("fill" in marks and "drop" not in marks and self.bot.get("protection", {}).get("status") == "ARMED"
                    and t - marks["fill"] >= 20):
                self.fake.bid, self.fake.ask = PRICE * Decimal(".994"), PRICE * Decimal(".994") + Decimal(".01")
                self.fake.before_close = lambda: fire_stop(self.fx, self.fake)  # fires after closing() read the stop
                marks["drop"] = t
        self.run_season(act)
        self.assert_never_frozen()
        self.assertEqual([code for _, code in self.fake.refused], ["51169"])
        refused = self.events("exchange_order_rejected")
        self.assertEqual(refused[0]["step"], 6)
        self.assertEqual(refused[0]["data"]["rejected_code"], "51169")
        self.assertTrue(self.events("protective_child_adopted"))
        self.assertEqual((self.bot["trades"], self.bot["position_btc"]), (1, 0.0))
        self.assertTrue(self.bot["accounting_reconciled"])

    def test_an_exit_by_the_owned_stop_starts_the_recipes_cooldown(self):
        marks = {}

        def act(t):
            self.fill_resting(t, marks)
            if ("fill" in marks and "fired" not in marks and self.bot.get("protection", {}).get("status") == "ARMED"
                    and t - marks["fill"] >= 20):
                self.fake.bid, self.fake.ask = PRICE * Decimal(".994"), PRICE * Decimal(".994") + Decimal(".01")
                fire_stop(self.fx, self.fake)
                marks["fired"] = self.fx.now
        self.run_season(act)
        self.assert_never_frozen()
        self.assertFalse(self.events("exit_trigger"))  # the venue closed it: no local exit
        self.assertGreaterEqual(self.bot["_next_decision"], marks["fired"] + 600)
        entries = [e for e in self.events("exchange_order_intent") if not e["data"]["request"]["reduceOnly"]]
        self.assertEqual(len(entries), 1)  # the admission window stayed open, yet no entry before the cooldown

    def test_an_uncertain_send_the_venue_never_shows_is_concluded_absent_after_reads_over_time(self):
        self.fake.lose_entry = True
        self.run_season(until=lambda: bool(self.events("exchange_order_absent")))
        absent = self.events("exchange_order_absent")
        self.assertEqual(len(absent), 1)
        self.assertGreaterEqual(absent[0]["data"]["absent_reads"], ABSENT_READS)
        self.assertGreaterEqual(absent[0]["data"]["absent_seconds"], ABSENT_SECONDS)
        self.assertEqual(len(self.events("exchange_order_not_visible")), 1)
        self.assertEqual(self.events("order_ack_uncertain")[0]["bot_id"], self.bot["id"])
        first = self.bot["_orders"][0]
        self.assertEqual(first["not_created"]["kind"], "absent")
        self.assertNotIn(first, engine.unresolved(self.bot))  # a later entry of the same step may rest; this one is done
        self.assertNotEqual(self.state["status"], "PAUSED")
        self.run_season()
        self.assert_never_frozen()

    def test_an_uncertain_send_the_venue_did_create_is_read_as_usual(self):
        self.fake.lose_entry_after_create = True
        marks = {}
        self.run_season(lambda t: self.fill_resting(t, marks), until=lambda: self.bot["position_btc"] != 0)
        self.assertTrue(self.events("order_ack_uncertain"))
        self.assertFalse(self.events("exchange_order_not_visible"))
        self.assertFalse(self.events("exchange_order_absent"))
        self.assertGreater(self.bot["position_btc"], 0)


class ReviewFindings(SeasonCase):
    """The R2.1.5 review scenarios (team/reviews/R215_REVIEW_UA.md, probe_outcomes.py of the lead's review)."""

    def stop_when_protected(self, marks, arm=None, after=20):
        def act(t):
            self.fill_resting(t, marks)
            if ("fill" in marks and "stop" not in marks and self.bot.get("protection", {}).get("status") == "ARMED"
                    and t - marks["fill"] >= after):
                if arm:
                    arm()
                self.fx.lab.stop(self.fx.lab.data["generation"], self.state["id"])
                marks["stop"] = t
        return act

    def test_f1_a_market_close_whose_answer_is_lost_and_whose_facts_are_late_is_no_foreign_position(self):
        marks = {}

        def arm():
            self.fake.lose_created, self.fake.lag = "close", 2
        self.run_season(self.stop_when_protected(marks, arm), dt=1, limit=600)
        self.assert_never_frozen()
        self.assertFalse(self.bot["_manual_review"])
        self.assertTrue(self.events("exchange_order_not_visible"))
        self.assertTrue(self.events("exchange_order_fill_not_visible"))  # the position moved before the order showed up
        self.assertEqual((self.bot["trades"], self.bot["position_btc"]), (1, 0.0))
        self.assertTrue(self.bot["accounting_reconciled"])

    def test_f2_an_answer_that_is_no_listed_refusal_stays_uncertain_and_the_created_order_is_read_and_cancelled(self):
        self.fake.refuse_but_create = "51149"  # "Order timed out ... does not mean that the request was successful or failed"
        self.run_season(limit=200)
        self.assertFalse(self.events("exchange_order_rejected"))
        first = self.bot["_orders"][0]
        self.assertNotIn("not_created", first)
        self.assertEqual(first["result"]["state"], "canceled")  # read, then cancelled at its post-only timeout
        cancels = [json.loads(r.data)["clOrdId"] for r in self.fake.requests if urlsplit(r.full_url).path == "/api/v5/trade/cancel-order"]
        self.assertIn(first["request"]["clOrdId"], cancels)
        self.assertEqual([o["clOrdId"] for o in self.fake.orders.values() if o.get("ordType") == "post_only" and o["state"] == "live"],
                         [o["request"]["clOrdId"] for o in engine.unresolved(self.bot)])  # nothing rests unwatched

    def live_own_entries(self):
        return [k for k, v in self.fake.orders.items() if v.get("ordType") == "post_only" and v["state"] in {"live", "partially_filled"}]

    def test_f2_r1_a_hidden_resting_entry_is_found_by_its_cancel_and_gets_no_second_entry_beside_it(self):
        self.fake.lose_created, self.fake.hide = "entry", 120  # GET and orders-pending hide it for 120 s; it rests
        most = []
        self.run_season(lambda t: most.append(len(self.live_own_entries())), limit=60)
        first = self.bot["_orders"][0]
        self.assertFalse(self.events("exchange_order_absent"))  # the cancel was accepted: it exists
        self.assertTrue(self.events("exchange_order_found"))
        self.assertEqual(first["result"]["state"], "canceled")  # read once visible, as usual
        self.assertNotIn("not_created", first)
        self.assertLessEqual(max(most), 1)  # never a second own entry resting beside it

    def test_f2_r1_an_owner_stop_while_the_entry_hides_is_no_done_with_our_order_resting(self):
        self.fake.lose_created, self.fake.hide = "entry", 120
        stop = lambda t: t >= 105 and not self.bot["stop_requested"] and self.fx.lab.stop(self.fx.lab.data["generation"], self.state["id"])
        resting_at_done = []

        def act(t):
            stop(t)
            if self.state["status"] == "DONE":
                resting_at_done.append(self.live_own_entries())
        self.run_season(act, limit=200)
        self.assertEqual(self.state["status"], "DONE")
        self.assertEqual(self.live_own_entries(), [])
        self.assertFalse(self.bot["_manual_review"])
        self.assertTrue(self.bot["accounting_reconciled"])

    def test_f2_r1_a_settling_bot_cancels_entries_recorded_as_not_created(self):
        self.fake.lose_entry = True  # lost before the venue: truly not created, concluded absent after a refused cancel
        stop = lambda t: t >= 150 and not self.bot["stop_requested"] and self.fx.lab.stop(self.fx.lab.data["generation"], self.state["id"])
        self.run_season(stop, limit=200)
        self.assert_never_frozen()
        first = self.bot["_orders"][0]
        self.assertEqual(first["not_created"]["kind"], "absent")
        self.assertTrue(first["not_created"]["cancel_checked"])  # the settlement's cancel answered "nothing to cancel"
        outcomes = [e["data"]["outcome"] for e in self.events("exchange_cancel_posted")
                    if e["data"].get("client_order_id") == first["request"]["clOrdId"]]
        self.assertEqual(outcomes, ["nothing_to_cancel", "nothing_to_cancel"])  # before the verdict and before the settlement

    def test_f4_a_refused_close_waits_a_short_flat_pause_and_does_not_spend_the_limits(self):
        marks = {}

        def arm():
            self.fake.refuse_close = "51135"

        def act(t):
            self.stop_when_protected(marks, arm)(t)
            if "stop" in marks and t - marks["stop"] >= 30:
                self.fake.refuse_close = None
        self.run_season(act, dt=2, limit=2000)
        self.assert_never_frozen()
        refused = self.events("exchange_order_rejected")
        times = [datetime.fromisoformat(e["at"]).timestamp() for e in refused]
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertTrue(4 <= len(refused) <= 8, len(refused))  # about one each 5 s over 30 s, not one each 2 s step
        self.assertTrue(all(5 <= gap <= 6 for gap in gaps), gaps)
        self.assertEqual(refused[0]["data"]["retry_after_seconds"], 5)
        self.assertEqual(engine.intent_count(self.bot), len(self.bot["_orders"]) - len(refused))
        self.assertNotIn("_close_retry_at", self.bot)  # cleared once the position is flat

    def test_f4_an_owner_stop_after_a_refused_rules_exit_closes_at_once(self):
        marks = {}

        def act(t):
            self.fill_resting(t, marks)
            if "fill" in marks and "drop" not in marks and self.bot.get("protection", {}).get("status") == "ARMED" and t - marks["fill"] >= 20:
                self.fake.bid, self.fake.ask = PRICE * Decimal(".994"), PRICE * Decimal(".994") + Decimal(".01")  # below the SL
                self.fake.refuse_close = "51136"
                marks["drop"] = t
            if self.fake.refused and "refused" not in marks:
                marks["refused"] = t
                self.fake.refuse_close = None  # the venue accepts again
            if "refused" in marks and "owner" not in marks and t > marks["refused"]:
                self.fx.lab.stop(self.fx.lab.data["generation"], self.state["id"])
                marks["owner"] = t
        self.run_season(act, dt=2, limit=600)
        self.assert_never_frozen()
        closes = [o for o in self.bot["_orders"] if o["request"]["reduceOnly"] and not o.get("not_created") and not o.get("_protective_parent")]
        self.assertEqual(len(closes), 1)
        self.assertLess(closes[0]["at"] - (self.start + marks["refused"]), 5)  # the Stop did not wait out the pause
        self.assertEqual(marks["owner"], marks["refused"] + 2)

    def test_f4_without_an_armed_stop_a_refused_close_is_sent_again_at_the_next_step(self):
        marks = {}

        def act(t):
            self.fill_resting(t, marks)
            if "fill" in marks and "fail" not in marks and self.bot.get("protection", {}).get("status") == "ARMED" and t - marks["fill"] >= 20:
                for algo in self.fake.algos.values():
                    if algo["state"] == "live":
                        algo.update(state="order_failed", failCode="51136")  # the owned stop failed on the venue
                self.fake.refuse_close = "51136"
                marks["fail"] = t
            if "fail" in marks and t - marks["fail"] >= 12:
                self.fake.refuse_close = None
        self.run_season(act, dt=2, limit=600)
        refused = self.events("exchange_order_rejected")
        times = [datetime.fromisoformat(e["at"]).timestamp() for e in refused]
        self.assertTrue(all(b - a <= 2 for a, b in zip(times, times[1:])), times)  # no pause with nothing on the venue
        self.assertEqual(self.bot["position_btc"], 0.0)


    def test_f3r_an_acknowledged_order_the_venue_dropped_unread_is_concluded_unseen_without_a_pause(self):
        marks = {}

        def act(t):
            live = [o for o in self.fake.orders.values() if o.get("ordType") == "post_only" and o["state"] == "live"]
            first = self.bot["_orders"][0] if self.bot["_orders"] else None
            if live and "drop" not in marks and first and (first.get("result") or {}).get("state") == "live":
                live[0].update(state="canceled", uTime=str(int(self.fx.now * 1000) - 1000))  # cancelled unfilled on the venue...
                self.fake.purge_after = 0.5  # ...and dropped from GET at once (OKX: after about 2 hours)
                marks["drop"] = t
            if "drop" in marks and t - marks["drop"] >= 120 and not self.bot["stop_requested"]:
                self.fx.lab.stop(self.fx.lab.data["generation"], self.state["id"])
        self.run_season(act, limit=200)
        self.assert_never_frozen()
        first = self.bot["_orders"][0]
        self.assertEqual(first["not_created"]["kind"], "unseen")  # it existed: not "never created"
        self.assertEqual(self.events("exchange_order_absent")[0]["data"]["kind"], "unseen")
        self.assertLess(self.bot["_next_decision"], first["not_created"]["at"] + 600)  # no refusal cooldown for it

    def test_f4c_accepted_closes_with_no_fill_wait_a_growing_pause(self):
        marks = {}

        def act(t):
            self.stop_when_protected(marks, lambda: setattr(self.fake, "close_fraction", Decimal(0)))(t)
            if "stop" in marks and t - marks["stop"] >= 50:
                self.fake.close_fraction = Decimal(1)
        self.run_season(act, dt=1, limit=2000)
        self.assert_never_frozen()
        empty = [o for o in self.bot["_orders"] if o["request"]["reduceOnly"] and not o.get("_protective_parent")
                 and o["result"]["state"] == "canceled" and not Decimal(o["result"]["contracts"])]
        times = [o["at"] for o in empty]
        gaps = [round(b - a) for a, b in zip(times, times[1:])]
        self.assertTrue(len(empty) <= 4, len(empty))  # 0, 5, 15, 35 s over 50 s, not one each 1 s step
        self.assertEqual(gaps, sorted(gaps))
        self.assertGreaterEqual(gaps[0], 5)
        self.assertNotIn("_close_empty", self.bot)  # cleared once flat


class R217Findings(SeasonCase):
    """The R2.1.7 review (team/reviews/R217_REVIEW_UA.md): low findings fixed in R2.1.8."""

    def test_a1_a_hidden_entry_that_fills_before_our_cancel_is_no_absence_and_gets_no_second_entry(self):
        marks = {}

        def act(t):
            first = self.bot["_orders"][0] if self.bot["_orders"] else None
            if first and "hide" not in marks and (first.get("result") or {}).get("state") == "live":
                ident = first["request"]["clOrdId"]
                self.fake.hidden[ident] = self.clock_now() + 100  # GET and orders-pending hide it for 100 s
                self.fake.on_cancel = lambda: self.fake.fill(ident, Decimal(self.fake.orders[ident]["sz"]))  # it fills first
                marks["hide"] = t
            if "hide" in marks:
                marks["entries"] = max(marks.get("entries", 0), sum(1 for v in self.fake.orders.values()
                                                                     if v.get("ordType") == "post_only" and v["state"] in {"live", "filled"}))
        self.fake.lagging, self.fake.hidden = {}, {}
        self.run_season(act, limit=60)
        first = self.bot["_orders"][0]
        self.assertNotIn("not_created", first)  # 51400 came with a fill: no "unseen" verdict
        self.assertFalse(self.events("exchange_order_absent"))
        self.assertEqual(marks["entries"], 1)  # no second entry beside it
        self.assertFalse([e for e in self.events("exchange_order_intent") if not e["data"]["request"]["reduceOnly"]][1:])
        self.assertTrue(self.events("exchange_order_fill_not_visible"))  # its fill was late facts, not foreign
        self.assertFalse(self.bot["_manual_review"])
        self.assertEqual(self.bot["position_btc"], 0.001)  # one lot, under our stop once the order showed up again
        self.assertEqual([a["state"] for a in self.fake.algos.values()], ["live"])

    def clock_now(self):
        return self.fx.now

    def test_r218_a_late_position_after_nothing_to_cancel_gets_the_entry_its_stop_not_a_manual_review(self):
        self.fake.lose_created, self.fake.hide = "entry", 100  # created, hidden from GET and orders-pending for 100 s
        marks = {}

        def act(t):
            entry = self.bot["_orders"][0] if self.bot["_orders"] else None
            if entry and "armed" not in marks:
                ident = entry["request"]["clOrdId"]

                def fill_first():  # it fills just before our cancel, and the position view lags that fill by one read
                    self.fake.lagged_units = self.fake.units
                    self.fake.fill(ident, Decimal(self.fake.orders[ident]["sz"]))
                    self.fake.position_lag = 1
                self.fake.on_cancel = fill_first
                marks["armed"] = t
        self.run_season(act, limit=60)
        first = self.bot["_orders"][0]
        self.assertNotIn("not_created", first)  # the verdict waited for the next check, which saw the lot
        self.assertFalse(self.events("exchange_order_absent"))
        self.assertFalse(self.bot["_manual_review"])
        self.assertEqual(first["result"]["state"], "filled")
        self.assertEqual([a["state"] for a in self.fake.algos.values()][:1], ["live"])  # under our stop once it showed
        self.assertEqual(len([o for o in self.bot["_orders"] if not o["request"]["reduceOnly"]]), 1)  # one lot, no second entry

    def test_a3_a_foreign_position_after_an_absent_ioc_entry_is_a_manual_review_at_once(self):
        self.fake.lose_entry = True  # lost before the venue: concluded absent after a refused cancel
        self.run_season(until=lambda: bool(self.events("exchange_order_absent")), limit=100)
        self.assertTrue(self.events("exchange_order_absent"))
        self.fake.units += Decimal(".01")  # someone else's lot on this account
        self.run_season(limit=3)
        self.assertTrue(self.bot["_manual_review"])  # no window that takes it for the absent order's late fill
        self.assertFalse(self.events("exchange_order_fill_not_visible"))

    def test_f4cr_a_resume_gets_a_close_reserve_above_the_hard_cap(self):
        marks = {}

        def act(t):
            self.fill_resting(t, marks)
            if "fill" in marks and "fail" not in marks and self.bot.get("protection", {}).get("status") == "ARMED" and t - marks["fill"] >= 20:
                for algo in self.fake.algos.values():
                    if algo["state"] == "live":
                        algo.update(state="order_failed", failCode="51136")  # the owned stop failed on the venue
                self.fake.close_fraction = Decimal(0)  # closes accepted, cancelled with no fill
                marks["fail"] = t
        self.run_season(act, dt=2, limit=2000)
        self.assertEqual(self.state["status"], "PAUSED")  # the P0 hard cap of 60 holds within the run
        self.assertEqual(engine.intent_count(self.bot), engine.LIMITS["max_order_intents_per_bot"])
        self.assertTrue([e for e in self.events("exchange_check_failed") if e["data"].get("reason") == "order_intent_limit"])
        # The owner resumes once the market matches again: the Resume's reserve closes the position.
        self.fake.close_fraction = Decimal(1)
        lab = self.fx.lab
        resource = lab.resources.pop(self.state["id"], None)
        if resource:
            resource["stack"].close()
        lab.resume(lab.data["generation"], self.state["id"])
        self.state = lab.sessions[self.state["id"]]
        self.bot = self.state["bots"][0]
        lab.prepare(self.state, lab.chosen_profiles(["arena-a"]), True)
        lab.resources[self.state["id"]]["wakeup"].wait = lambda seconds: False
        self.run_season(dt=2, limit=100)
        self.assertEqual(self.state["status"], "DONE")
        self.assertEqual(self.bot["position_btc"], 0.0)
        self.assertLessEqual(engine.intent_count(self.bot), engine.LIMITS["max_order_intents_per_bot"] + engine.RESUME_CLOSE_RESERVE)

    def resume(self):
        lab = self.fx.lab
        resource = lab.resources.pop(self.state["id"], None)
        if resource:
            resource["stack"].close()
        lab.resume(lab.data["generation"], self.state["id"])
        self.state = lab.sessions[self.state["id"]]
        self.bot = self.state["bots"][0]
        lab.prepare(self.state, lab.chosen_profiles(["arena-a"]), True)
        lab.resources[self.state["id"]]["wakeup"].wait = lambda seconds: False

    def test_r218_each_resume_adds_its_reserve(self):
        marks = {}

        def act(t):
            self.fill_resting(t, marks)
            if "fill" in marks and "fail" not in marks and self.bot.get("protection", {}).get("status") == "ARMED" and t - marks["fill"] >= 20:
                for algo in self.fake.algos.values():
                    if algo["state"] == "live":
                        algo.update(state="order_failed", failCode="51136")
                self.fake.close_fraction = Decimal(0)
                marks["fail"] = t
        self.run_season(act, dt=2, limit=2000)
        self.assertEqual(self.state["status"], "PAUSED")
        self.resume()  # the market still matches nothing: the first reserve is spent too
        self.run_season(dt=2, limit=200)
        self.assertEqual(self.state["status"], "PAUSED")
        cap, reserve = engine.LIMITS["max_order_intents_per_bot"], engine.RESUME_CLOSE_RESERVE
        self.assertEqual(engine.intent_count(self.bot), cap + reserve)
        self.fake.close_fraction = Decimal(1)
        self.resume()  # a second Resume adds another reserve, not the same one again
        self.assertEqual(self.bot["_resume_closes"], 2 * reserve)
        self.run_season(dt=2, limit=100)
        self.assertEqual(self.state["status"], "DONE")
        self.assertEqual(self.bot["position_btc"], 0.0)
        self.assertTrue(cap + reserve < engine.intent_count(self.bot) <= cap + 2 * reserve)

    def test_f4_an_owner_stop_clears_the_refusal_pause_at_once(self):
        marks = {}

        def act(t):
            self.fill_resting(t, marks)
            if "fill" in marks and "drop" not in marks and self.bot.get("protection", {}).get("status") == "ARMED" and t - marks["fill"] >= 20:
                self.fake.bid, self.fake.ask = PRICE * Decimal(".994"), PRICE * Decimal(".994") + Decimal(".01")  # below the SL
                self.fake.refuse_close = "51136"
                marks["drop"] = t
            refused = self.events("exchange_order_rejected")
            if refused and "owner" not in marks:
                self.fake.refuse_close = None
                self.fx.lab.stop(self.fx.lab.data["generation"], self.state["id"])  # one second after the refusal
                marks["owner"] = self.fx.now
        self.run_season(act, dt=1, limit=600)
        self.assert_never_frozen()
        refused_at = datetime.fromisoformat(self.events("exchange_order_rejected")[0]["at"]).timestamp()
        close = next(o for o in self.bot["_orders"] if o["request"]["reduceOnly"] and not o.get("not_created") and not o.get("_protective_parent"))
        self.assertLessEqual(close["at"] - refused_at, 2)  # sent at the step of the Stop, not 5 s after the refusal

    def test_f4_resets_by_owner_stop_resume_and_a_flat_position(self):
        bot = self.bot
        for origin, first, cleared in (("owner", False, True), ("recovery", False, True), ("system", False, False), ("system", True, True)):
            bot.update(stop_requested=not first, _close_retry_at=self.fx.now + 99, _close_refusals=2, _close_empty=3)
            engine.stop(self.state, self.fx.now, bot["id"], origin=origin)
            self.assertEqual("_close_retry_at" not in bot, cleared, (origin, first))
            self.assertEqual("_close_empty" not in bot, cleared, (origin, first))
        bot.update(_close_retry_at=self.fx.now + 99, _close_refusals=2, _close_empty=3)
        engine.rebuild(bot)  # no fills: a flat position
        self.assertFalse({"_close_retry_at", "_close_refusals", "_close_empty"} & set(bot))


class LongSeasonReviewFindings(SeasonCase):
    DURATION = 1800

    def test_f3_the_settlement_does_not_read_cancelled_unfilled_orders_that_okx_dropped(self):
        self.fake.purge_after = 30  # stands for OKX's ~2 hours
        stop = lambda t: t >= 700 and not self.bot["stop_requested"] and self.fx.lab.stop(self.fx.lab.data["generation"], self.state["id"])
        self.run_season(stop, limit=600)  # the post-only entry times out at 600 s and is cancelled unfilled; Stop at 700 s
        self.assert_never_frozen()
        dropped = [o for o in self.bot["_orders"] if o["result"]["state"] == "canceled" and o["result"]["contracts"] == "0"]
        self.assertTrue(dropped)
        self.assertTrue(self.bot["accounting_reconciled"])
        # tools/check_order_retention.py (GET only) finds the dropped order from the local journal and says so.
        sys.path.insert(0, str(ROOT / "tools"))
        import check_order_retention as tool
        self.assertEqual(self.fx.lab.path, Path(self.fx.root) / "runtime" / "exchange_v1")
        before = len(self.fake.requests)
        report = tool.check(self.fx.root, "arena-a", self.state["id"], older_hours=30 / 3600,
                            transport=self.fake.transport, now=self.fx.now)
        self.assertEqual(report["orders"][0]["client_order_id"], dropped[0]["request"]["clOrdId"])
        self.assertEqual(report["orders"][0]["answer"], "51603 Order does not exist")
        self.assertEqual((report["writes"], report["requests"]["blocked"]), (0, 0))
        self.assertTrue(all(r.get_method() == "GET" for r in self.fake.requests[before:]))
        # R2.1.7: a --max of 8 or more meets the 8-request cap: it is reported per order, never raised
        many = [("J5" + "c" * 16 + "0E%04d" % k, "ETH-USDT-SWAP", self.fx.now - 3 * 3600) for k in range(10)]
        original = tool.candidates
        tool.candidates = lambda journal, older, now: many
        try:
            capped = tool.check(self.fx.root, "arena-a", self.state["id"], maximum=10, transport=self.fake.transport, now=self.fx.now)
        finally:
            tool.candidates = original
        self.assertLessEqual(capped["requests"]["get"], 8)
        self.assertTrue(capped["orders"][-1]["answer"].startswith("not read: "))
        self.assertTrue(all(row["answer"] == "51603 Order does not exist" for row in capped["orders"][:-1]))


class TakerReviewFindings(SeasonCase):
    TAKER = True

    def test_f1_an_ioc_entry_whose_answer_is_lost_and_whose_facts_are_late_gets_its_owned_stop(self):
        self.fake.lose_created, self.fake.lag = "entry", 2
        self.run_season(until=lambda: self.bot["position_btc"] != 0 and self.bot.get("protection", {}).get("status") == "ARMED",
                        limit=100)
        self.assertFalse(self.bot["_manual_review"])
        self.assertFalse(self.events("exchange_check_failed"))
        self.assertTrue(self.events("exchange_order_fill_not_visible"))
        self.assertEqual(self.bot["protection"]["covered_contracts"], self.bot["_contracts"])
        self.assertEqual([a["state"] for a in self.fake.algos.values()], ["live"])


class CancelAnswers(unittest.TestCase):
    """R2.1.8 (R217-A2): the cancel answers that say nothing is there to cancel, and the ones journalled as unknown."""

    def setUp(self):
        self.fake = fixture.FakeExchange(lambda: 1800000030.)
        self.api = self.fake.api()
        self.api.preflight(100)
        self.api.arm(100)
        self.ident = "J5" + "a" * 16 + "0E0000"

    def answer(self, payload):
        self.api.transport = lambda request: payload
        return self.api.cancel(self.ident)

    def test_nothing_to_cancel_in_every_shape(self):
        for payload in ({"code": "1", "data": [{"sCode": "51400", "clOrdId": self.ident}]},
                        {"code": "1", "data": [{"sCode": "51400", "clOrdId": "", "ordId": ""}]},  # no client ID echoed
                        {"code": "51400", "msg": "Order cancellation failed", "data": []},  # top-level, no rows
                        {"code": "1", "data": [{"sCode": "51405", "clOrdId": self.ident}]},  # "you don't have any pending orders"
                        {"code": "51603", "data": []}):
            with self.assertRaises(CancelRefused, msg=payload):
                self.answer(payload)

    def test_other_codes_are_unknown_and_keep_their_code(self):
        for code in ("51410", "51412", "51999"):
            with self.assertRaises(CancelFailed) as caught:
                self.answer({"code": "1", "data": [{"sCode": code, "clOrdId": self.ident}]})
            self.assertEqual(caught.exception.code, code)
            self.assertNotIsInstance(caught.exception, CancelRefused)
        with self.assertRaises(PreflightError) as caught:  # another order's answer is no answer about ours
            self.answer({"code": "1", "data": [{"sCode": "51400", "clOrdId": "J5" + "b" * 16 + "0E0000"}]})
        self.assertNotIsInstance(caught.exception, CancelRefused)


class PlacementAnswers(unittest.TestCase):
    """Which answers to POST /api/v5/trade/order prove that no order exists (OrderRejected), and which stay uncertain."""

    def setUp(self):
        self.fake = fixture.FakeExchange(lambda: 1800000030.)
        self.api = self.fake.api()
        self.api.preflight(100)
        self.api.arm(100)
        self.body = {"instId": "BTC-USDT-SWAP", "tdMode": "isolated", "posSide": "net", "side": "buy", "ordType": "ioc",
                     "sz": ".01", "px": "84000.2", "reduceOnly": False, "clOrdId": "J5" + "a" * 16 + "0E0000", "stpMode": "cancel_taker"}

    def answer(self, payload):
        self.api.transport = lambda request: payload
        return self.api.order(self.body)

    def test_a_51xxx_code_for_our_client_id_without_an_order_id_is_a_rejection(self):
        for code in ("51008", "51169", "51000", "51121"):
            with self.assertRaises(OrderRejected) as caught:
                self.answer({"code": "1", "msg": "All operations failed",
                             "data": [{"sCode": code, "sMsg": "", "clOrdId": self.body["clOrdId"], "ordId": ""}]})
            self.assertEqual(caught.exception.code, code)

    def test_duplicates_system_codes_other_ids_and_order_ids_stay_uncertain(self):
        cases = [("51016", self.body["clOrdId"], ""), ("51011", self.body["clOrdId"], ""), ("50013", self.body["clOrdId"], ""),
                 ("50004", self.body["clOrdId"], ""), ("51008", "J5" + "b" * 16 + "0E0000", ""), ("51008", self.body["clOrdId"], "123"),
                 # R2.1.6: OKX's timeouts and any 51xxx not in the documented list stay uncertain
                 ("51149", self.body["clOrdId"], ""), ("51054", self.body["clOrdId"], ""), ("51999", self.body["clOrdId"], ""),
                 ("51400", self.body["clOrdId"], "")]
        for code, client, order in cases:
            with self.assertRaises(PreflightError) as caught:
                self.answer({"code": "1", "data": [{"sCode": code, "clOrdId": client, "ordId": order}]})
            self.assertNotIsInstance(caught.exception, OrderRejected, (code, client, order))
        with self.assertRaises(PreflightError) as caught:  # two rows: not one answer about our order
            self.answer({"code": "1", "data": [{"sCode": "51008", "clOrdId": self.body["clOrdId"], "ordId": ""}] * 2})
        self.assertNotIsInstance(caught.exception, OrderRejected)

    def test_a_get_of_a_client_id_that_never_became_an_order_is_order_absent(self):
        self.api.transport = lambda request: {"code": "51603", "msg": "Order does not exist", "data": []}
        with self.assertRaises(OrderAbsent) as caught:
            self.api.order_result(self.body)
        self.assertEqual(str(caught.exception), "okx_error_51603")  # the same diagnostic code as before


if __name__ == "__main__":
    unittest.main()
