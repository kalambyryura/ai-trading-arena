"""V2 engine acceptance: fictional PAPER and explicitly injected fake Exchange.

No real profiles, API, paid gate, worker, or runtime journal are opened.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest

PATH = Path(__file__).resolve().parent
ROOT = PATH.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(PATH))
from test_protection_integration import FaultExchange
from trading_lab import arena_engine as paper, exchange_engine as exchange, strategy_runtime as runtime, strategy_v2
from trading_lab.exchange_lab import ExchangeLab
from trading_lab.exchange_clock import ExchangeClock
from trading_lab.ops_policy import ClockGuard
from trading_lab.ops_storage import HealthState
from trading_lab.okx_readonly import PreflightError


def leaf(feature, op="gte", value=0):
    return {"feature": feature, "op": op, "value": value}


def recipe(side="long", *, reduce=True, conflict=False):
    item = json.loads((PATH / "fixtures" / "strategy_v2_temporal.json").read_text(encoding="utf-8"))
    item.update(id="fictional-v2", version=1, recipe_sha256="b" * 64)
    item["rules"].update(side=side, cost_checkpoint=None, max_entries=3,
                         add_when=leaf("position_age_seconds"),
                         reduce_when=leaf("position_age_seconds") if reduce else None,
                         reduce_pct=25 if reduce else None)
    item["rules"]["entry"] = {direction: {"setup": None, "invalidate": None, "expiry_bars": None,
        "confirm": leaf("sma_gap_bps", "gt" if direction == "long" or conflict else "lt", 0)}
        for direction in (("long", "short") if side == "both" else (side,))}
    item["rules"] = strategy_v2.validate_rules(item["rules"])
    return item


def market(at, *, short=False, bid=10000, ask=10001, lot=.0001, minimum=.0001, liquidity=1):
    return {"at": at, "server_time": at, "bar_close_ms": int(at // 60 * 60000),
            "quote_ts": int(at * 1000), "sha256": "a" * 64,
            "bid": bid, "ask": ask, "mid": (bid + ask) / 2,
            "bid_btc": liquidity, "ask_btc": liquidity, "lot_btc": lot, "min_btc": minimum,
            "funding_at": at + 3600, "funding_time_ms": int((at + 3600) * 1000), "funding_rate": 0,
            "features": {"spread_bps": (ask - bid) / ((ask + bid) / 2) * 10000,
                         "sma_gap_bps": -15 if short else 15, "rsi_14": 50,
                         "return_5m_bps": 20, "return_1m_bps": 5,
                         "range_position_20": 60, "volume_ratio_5": 1}}


def paper_owned(item, *, duration=300, quantity=.004):
    state = paper.new_season([item], 100, duration, 60, "f" * 64)
    bot = state["bots"][0]
    runtime.before_fill(bot)
    sign = -1 if item["rules"]["side"] == "short" else 1
    bot.update(position_btc=sign * quantity, entry_price=10000, entries=1, _total_entries=1,
               _hold_start=65, _entry_cost_hurdle_bps=14, fees_usdt=.02, _bank=99.98)
    runtime.entry_notional(bot, quantity * 10000)
    return state, bot


class FictionalExchangeRun:
    def __init__(self, item, *, contracts=".04"):
        self.item, self.now = item, 1800000030.
        self.fake = FaultExchange(lambda: self.now)
        self.api = self.fake.api()
        check = self.api.preflight(200)
        self.api.arm(200)
        self.state = exchange.create([item], ["fictional-account"], "workshop_demo",
                                      200, 300, self.now, "f" * 64)
        self.bot = self.state["bots"][0]
        exchange.initialize(self.bot, check)
        self.bot["_next_decision"] = self.now + 1000
        self.bot["_entry_cost_hurdle_bps"] = 10.012
        self.short = item["rules"]["side"] == "short"
        qty = Decimal(contracts)
        if qty:
            self.seed_entry(qty)
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.lab = ExchangeLab.__new__(ExchangeLab)
        self.journal_root = tempfile.TemporaryDirectory()
        self.lab.path = Path(self.journal_root.name)
        self.lab.root = self.lab.path  # Complete the ExchangeLab workspace contract.
        self.lab.lock = threading.RLock()
        self.lab.clock = self.lab.monotonic = lambda: self.now
        self.lab.timebase = ExchangeClock(self.lab.clock, self.lab.monotonic)
        self.lab.timebase.synchronize(self.now)
        self.lab.event_positions = {}
        self.saved = []
        self.lab.save = lambda s, **kw: self.saved.append(deepcopy(s))
        self.lab.record_snapshot = self.lab.note_market_quality = lambda *args: None
        self.lab.ledgers = {self.state["context"]: SimpleNamespace(summary=lambda identity: {"unknown_billing": 0})}
        self.lab.decide = lambda *args: self.unexpected_paid_gate()
        self.resource = {"apis": [self.api], "pool": self.pool,
            "feed": SimpleNamespace(calls=0, snapshot=lambda: self.snapshot()), "deadline": self.now + 300,
            "clock_guard": ClockGuard(), "health": HealthState(), "hosted_protection": True,
            "closing_deadlines": {}, "hold_deadlines": {}}
        self.lab.resources = {self.state["id"]: self.resource}

    def seed_entry(self, qty):
        sign = -1 if self.short else 1
        basis = self.fake.bid if self.short else self.fake.ask
        identity = "J5" + self.state["id"][:16] + "0E0000"
        body = {"instId": "BTC-USDT-SWAP", "tdMode": "isolated", "posSide": "net",
                "side": "sell" if self.short else "buy", "reduceOnly": False,
                "clOrdId": identity, "sz": str(qty), "px": str(basis), "ordType": "ioc", "stpMode": "cancel_taker"}
        signed_fee = -qty * Decimal(".01") * basis * Decimal(".0005")
        observed = self.now - 65
        row = {**body, "ordId": "100000", "state": "filled", "accFillSz": str(qty),
               "avgPx": str(basis), "fee": str(signed_fee), "feeCcy": "USDT", "pnl": "0",
               "rebate": "0", "uTime": str(int(observed * 1000))}
        runtime.before_fill(self.bot)
        self.bot["_orders"].append({"at": observed, "fill_observed_at": observed, "request": body,
            "result": self.api.parse_order_result(row, body)})
        runtime.entry_notional(self.bot, float(qty * Decimal(".01") * basis))
        exchange.rebuild(self.bot)
        self.fake.units, self.fake.basis = sign * qty, basis
        self.fake.cash += signed_fee
        self.fake.orders[identity] = row
        self.fake.bills.append({"billId": "1", "ts": str(int(self.now * 1000)), "type": "2", "subType": "3",
            "ccy": "USDT", "instId": "BTC-USDT-SWAP", "ordId": "100000", "pnl": "0", "fee": str(signed_fee),
            "balChg": str(signed_fee), "posBalChg": "0"})
        self.bot["_entry_attempts"] = 1

    def snapshot(self):
        return market(self.now, short=self.short, bid=float(self.fake.bid), ask=float(self.fake.ask))

    @staticmethod
    def unexpected_paid_gate():
        raise AssertionError("This acceptance fixture must never reach a model gate")

    def step(self, delta=0):
        self.now += delta
        self.lab.step(self.state)

    def close(self):
        self.pool.shutdown(wait=True)
        self.journal_root.cleanup()


class ManagementIntegrationTests(unittest.TestCase):
    def fake_run(self, item, **kwargs):
        run = FictionalExchangeRun(item, **kwargs)
        self.addCleanup(run.close)
        return run

    def test_paper_long_short_and_both_select_the_declared_direction_on_actual_fill(self):
        for side, expected_short in (("long", False), ("short", True), ("both", False), ("both", True)):
            with self.subTest(side=side, short=expected_short):
                item = recipe(side, reduce=False)
                state = paper.new_season([item], 100, 300, 60, "f" * 64)
                bot = state["bots"][0]
                paper.tick(state, market(61, short=expected_short))
                self.assertEqual(bot["_pending"]["kind"], "entry")
                paper.tick(state, market(62, short=expected_short))
                self.assertLess(bot["position_btc"], 0) if expected_short else self.assertGreater(bot["position_btc"], 0)

    def test_both_direction_conflict_holds_in_paper_and_exchange_before_any_order(self):
        item = recipe("both", reduce=False, conflict=True)
        state = paper.new_season([item], 100, 300, 60, "f" * 64)
        paper.tick(state, market(61))
        self.assertIsNone(state["bots"][0]["_pending"])
        run = self.fake_run(item, contracts="0")
        snap = run.snapshot()
        runtime.advance(run.state, run.bot, item, snap, paper.emit)
        cand = exchange.candidate(run.state, run.bot, item, snap, run.now)
        self.assertFalse(cand["local_checks_passed"])
        self.assertIsNone(cand["side"])
        self.assertEqual(run.fake.post_count("/api/v5/trade/order"), 0)

    def test_exchange_long_short_and_both_create_the_selected_ioc_side(self):
        for side, short in (("long", False), ("short", True), ("both", False), ("both", True)):
            with self.subTest(side=side, short=short):
                item = recipe(side, reduce=False)
                run = self.fake_run(item, contracts="0")
                run.short = short
                snap = run.snapshot()
                runtime.advance(run.state, run.bot, item, snap, paper.emit)
                cand = exchange.candidate(run.state, run.bot, item, snap, run.now)
                self.assertTrue(cand["local_checks_passed"])
                body = exchange.entry_body(run.state, run.bot, item, snap,
                                            run.lab.identity_for(run.state, run.bot, "E"))
                self.assertEqual(body["side"], "sell" if short else "buy")
                self.assertFalse(body["reduceOnly"])

    def test_paper_partial_preserves_position_age_cost_epoch_and_one_ticket_per_bar(self):
        for side in ("long", "short"):
            with self.subTest(side=side):
                item = recipe(side)
                state, bot = paper_owned(item)
                paper.tick(state, market(120, short=side == "short"))
                self.assertEqual(bot["_pending"]["kind"], "reduce")
                position = deepcopy(bot["_strategy_v2"]["position"])
                paper.tick(state, market(121, short=side == "short"))
                self.assertAlmostEqual(abs(bot["position_btc"]), .003)
                self.assertEqual(bot["_hold_start"], 65)
                self.assertEqual(bot["trades"], 0)
                self.assertEqual(bot["_cycle_invested_notional"], 40)
                self.assertEqual(bot["_strategy_v2"]["position"]["id"], position["id"])
                self.assertEqual(bot["_strategy_v2"]["position"]["first_fill_at"], 65)
                bot["_next_decision"] = 122
                paper.tick(state, market(122, short=side == "short"))
                self.assertIsNone(bot["_pending"])
                self.assertEqual(sum(e["code"] == "close_intent" for e in state["events"]), 1)

    def test_cutoff_blocks_add_and_keeps_partial_management_and_remainder_open(self):
        item = recipe()
        state, bot = paper_owned(item, duration=61)
        paper.tick(state, market(120))
        paper.tick(state, market(121))
        self.assertEqual(state["status"], "DRAINING")
        self.assertAlmostEqual(bot["position_btc"], .003)
        self.assertFalse(bot["stop_requested"])
        self.assertFalse(paper.entry_gates(state, bot, item, market(122)))
        run = self.fake_run(item)
        run.state["_end"] = run.now  # Exact cutoff while this owned position remains open.
        run.resource["deadline"] = run.now
        run.step()
        self.assertEqual(run.state["status"], "DRAINING")
        self.assertEqual(run.fake.units, Decimal(".03"))
        self.assertFalse(run.bot["stop_requested"])
        self.assertEqual(run.fake.post_count("/api/v5/trade/order"), 1)

    def test_paper_owner_stop_escalates_pending_reduction_to_entire_owned_volume(self):
        item = recipe()
        state, bot = paper_owned(item)
        paper.tick(state, market(120))
        paper.request_stop(state, 120.5, bot["id"], origin="fictional-owner")
        paper.tick(state, market(121))
        self.assertEqual(bot["position_btc"], 0)
        self.assertEqual(bot["trades"], 1)

    def test_paper_formal_full_exit_has_priority_over_a_pending_partial_target(self):
        item = recipe()
        item["rules"]["exit"] = leaf("position_age_seconds", "gte", 56)
        state, bot = paper_owned(item)
        paper.tick(state, market(120))  # Age55: partial target was durably reserved.
        paper.tick(state, market(121))  # Age56: formal full exit now applies.
        self.assertEqual(bot["position_btc"], 0)

    def test_partial_lot_veto_never_consumes_the_bar_ticket_in_either_engine(self):
        item = recipe()
        state, bot = paper_owned(item, quantity=.0001)
        paper.tick(state, market(120))
        self.assertIsNone(bot["_pending"])
        self.assertIsNone(bot["_strategy_v2"]["position"].get("reduction_ticket"))
        run = self.fake_run(item, contracts=".01")
        run.step()
        self.assertEqual(run.fake.units, Decimal(".01"))
        self.assertEqual(run.fake.post_count("/api/v5/trade/order"), 0)
        self.assertIsNone(run.bot["_strategy_v2"]["position"].get("reduction_ticket"))

    def test_unknown_reduce_cost_cannot_permit_a_reduce_or_add(self):
        item = recipe()
        item["rules"]["reduce_when"] = leaf("entry_cost_hurdle_bps")
        state, bot = paper_owned(item)
        bot["_entry_cost_hurdle_bps"] = None
        paper.tick(state, market(120, bid=10040, ask=10041))
        self.assertIsNone(bot["_pending"])
        run = self.fake_run(item)
        run.bot["_entry_cost_hurdle_bps"] = None
        run.fake.bid, run.fake.ask = Decimal("84300"), Decimal("84300.1")
        run.step()
        self.assertEqual(run.fake.post_count("/api/v5/trade/order"), 0)
        self.assertFalse(runtime.management(item, run.bot, run.snapshot())["add"])

    def test_exchange_partial_preserves_age_and_cost_epoch_and_resizes_actual_stop(self):
        for side in ("long", "short"):
            with self.subTest(side=side):
                run = self.fake_run(recipe(side))
                first, invested = run.bot["_hold_start"], run.bot["_cycle_invested_notional"]
                run.step()
                self.assertEqual(abs(run.fake.units), Decimal(".03"))
                self.assertEqual(run.bot["_hold_start"], first)
                self.assertEqual(run.bot["_cycle_invested_notional"], invested)
                self.assertEqual(run.bot["trades"], 0)
                self.assertEqual(Decimal(run.bot["protection"]["covered_contracts"]), Decimal(".03"))
                position_id = run.bot["_strategy_v2"]["position"]["id"]
                run.step(1)
                run.step(1)
                self.assertEqual(run.fake.post_count("/api/v5/trade/order"), 1)
                self.assertEqual(run.bot["_strategy_v2"]["position"]["id"], position_id)
                self.assertFalse(run.bot.get("_exit_requested"))

    def test_exchange_lost_ack_and_unknown_fill_reconcile_same_partial_without_second_post(self):
        run = self.fake_run(recipe())
        run.fake.lost_ack = True
        run.fake.lookup_broken = True
        run.step()
        self.assertEqual(run.fake.units, Decimal(".03"))
        self.assertEqual(Decimal(run.bot["_contracts"]), Decimal(".04"))  # Unknown fill retained honestly.
        self.assertEqual(len(exchange.unresolved(run.bot)), 1)
        run.step(1)
        self.assertEqual(run.fake.post_count("/api/v5/trade/order"), 1)
        run.fake.lookup_broken = False
        run.step(1)
        self.assertEqual(Decimal(run.bot["_contracts"]), Decimal(".03"))
        self.assertFalse(exchange.unresolved(run.bot))
        self.assertEqual(run.fake.post_count("/api/v5/trade/order"), 1)
        self.assertEqual(Decimal(run.bot["protection"]["covered_contracts"]), Decimal(".03"))

    def test_exchange_stop_while_partial_fill_unknown_reconciles_then_closes_only_remainder(self):
        run = self.fake_run(recipe())
        run.fake.lost_ack = True
        run.fake.lookup_broken = True
        run.step()
        exchange.stop(run.state, run.now, reason="Fictional owner Stop")
        run.fake.lookup_broken = False
        run.step(1)
        self.assertEqual(run.fake.units, 0)
        self.assertEqual(run.fake.post_count("/api/v5/trade/order"), 2)
        close_orders = [o for o in run.bot["_orders"] if o["request"]["reduceOnly"]]
        self.assertEqual([Decimal(o["request"]["sz"]) for o in close_orders], [Decimal(".01"), Decimal(".03")])

    def test_exchange_partial_executor_fills_continue_the_same_fixed_target(self):
        run = self.fake_run(recipe(), contracts=".08")
        first, invested = run.bot["_hold_start"], run.bot["_cycle_invested_notional"]
        run.fake.close_fraction = Decimal(".5")
        run.step()  # Fixed target .02 contracts; actual first close fill is only .01.
        self.assertEqual(run.fake.units, Decimal(".07"))
        self.assertEqual(Decimal(run.bot["_reduce_remaining"]), Decimal(".01"))
        ticket = deepcopy(run.bot["_strategy_v2"]["position"]["reduction_ticket"])
        run.fake.close_fraction = Decimal(1)
        run.step(1)
        run.step(1)
        self.assertEqual(run.fake.units, Decimal(".06"))
        self.assertEqual(run.bot["_hold_start"], first)
        self.assertEqual(run.bot["_cycle_invested_notional"], invested)
        self.assertEqual(run.bot["_strategy_v2"]["position"]["reduction_ticket"], ticket)
        self.assertEqual(sum(e["code"] == "reduction_trigger" for e in run.state["events"]), 1)
        self.assertEqual(run.fake.post_count("/api/v5/trade/order"), 2)
        self.assertFalse(run.bot.get("_exit_requested"))

    def test_owner_close_can_reduce_known_actual_volume_with_an_unapplied_pending_stop_amend(self):
        run = self.fake_run(recipe(reduce=False))
        run.step()  # Verify current .04 protection, no author management action.
        run.fake.entry_fraction = Decimal(".5")
        run.fake.reject_once.add(("POST", "/api/v5/trade/amend-algos"))
        snap = run.snapshot()
        body = exchange.entry_body(run.state, run.bot, run.item, snap,
                                   run.lab.identity_for(run.state, run.bot, "E"))
        try:
            run.lab.submit(run.state, run.bot, run.api, body, run.now)
        except PreflightError:
            pass  # Actual partial add is known; old confirmed SL covers .04, not .06.
        self.assertEqual(run.fake.units, Decimal(".06"))
        self.assertEqual(Decimal(run.bot["_contracts"]), Decimal(".06"))
        self.assertTrue(run.bot["_protections"][-1].get("pending_amend"))
        exchange.stop(run.state, run.now, reason="Fictional owner close")
        run.api.close_only = True
        run.step(1)
        self.assertEqual(run.fake.units, 0)
        self.assertEqual(run.fake.post_count("/api/v5/trade/order"), 2)  # One add, one close.
        self.assertEqual(run.fake.post_count("/api/v5/trade/amend-algos"), 1)  # No blind amend retry.


if __name__ == "__main__":
    unittest.main()
