"""Controller edge regressions. PAPER temp snapshots / injected FakeExchange only."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from urllib.parse import urlsplit

PATH = Path(__file__).resolve().parent
ROOT = PATH.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(PATH))
from test_management_integration import recipe, market, FictionalExchangeRun
from trading_lab import arena_engine as paper, exchange_engine as exchange, exchange_protection as protection
from trading_lab.arena import Arena
from trading_lab.match_protocol import close_admission
from trading_lab.okx_readonly import PreflightError


def entry_reserved():
    item = recipe(reduce=False)
    state = paper.new_season([item], 100, 300, 60, "f" * 64)
    paper.tick(state, market(61))
    bot = state["bots"][0]
    assert bot["_pending"] and bot["_strategy_v2"]["reservation"]
    return state, bot, item


class FakePaperGate:
    def __init__(self, callback=None):
        self.callback, self.candidates = callback, []
        self.ledger = SimpleNamespace(summary=lambda season_id: {"unknown_billing": 0})

    def decide(self, season_id, candidates):
        self.candidates.append(deepcopy(candidates))
        if self.callback:
            self.callback()
        return {"attempted": False, "reported_cost_usd": 0, "allowed": [], "answers": {},
                "elapsed_ms": 1, "model": "fictional-gate", "reason": "fictional_hold"}


def pending_transport(run, *, override=None, algo_override=None):
    """Expose live fake rows; optionally inject a contradictory ownership receipt."""
    original = run.fake.transport
    def wrapper(request):
        response = original(request)
        path = urlsplit(request.full_url).path
        if request.get_method() == "GET" and path == "/api/v5/trade/orders-pending":
            rows = override if override is not None else [o for o in run.fake.orders.values()
                if o.get("state") in {"live", "partially_filled"}]
            return {"code": "0", "data": deepcopy(rows)}
        if request.get_method() == "GET" and path == "/api/v5/trade/orders-algo-pending" and algo_override is not None:
            return {"code": "0", "data": deepcopy(algo_override)}
        if request.get_method() == "POST" and path == "/api/v5/trade/cancel-order":
            body = json.loads(request.data)
            found = next((o for o in run.fake.orders.values() if
                (body.get("ordId") and o["ordId"] == body["ordId"])
                or (body.get("clOrdId") and o["clOrdId"] == body["clOrdId"])), None)
            if found is None:
                raise AssertionError("Attempt to cancel a fictional unowned row")
            found["state"] = "canceled"
            return {"code": "0", "data": [{"sCode": "0", "ordId": found["ordId"], "clOrdId": found["clOrdId"]}]}
        return response
    run.api.transport = wrapper


def partial_protective_child(run):
    run.step()  # Establish the known owned parent while no reduction is enabled.
    parent = run.fake.algos[run.bot["_protections"][-1]["body"]["algoClOrdId"]]
    parent.update(state="partially_effective", ordIdList=["100001"])
    child = {"instId": "BTC-USDT-SWAP", "tdMode": "isolated", "posSide": "net",
        "side": "sell", "ordType": "market", "reduceOnly": True, "sz": ".04", "clOrdId": "",
        "ordId": "100001", "algoId": parent["algoId"], "state": "partially_filled",
        "accFillSz": ".01", "avgPx": "84000", "fee": "-.0042", "feeCcy": "USDT",
        "pnl": "-.00001", "rebate": "0", "uTime": str(int(run.now * 1000))}
    run.fake.orders["fictional-protective-child"] = child
    run.fake.units = Decimal(".03")
    return parent, child


def terminal_zero_fill_padding(run, target_count):
    while len(run.bot["_orders"]) < target_count:
        index = len(run.bot["_orders"])
        body = {"instId": "BTC-USDT-SWAP", "tdMode": "isolated", "posSide": "net",
                "side": "buy", "ordType": "ioc", "reduceOnly": False, "stpMode": "cancel_taker",
                "clOrdId": "J5" + run.state["id"][:16] + "0E" + str(index).zfill(4), "sz": ".01", "px": "84000.1"}
        row = {**body, "ordId": str(100000 + index), "state": "canceled", "accFillSz": "0",
               "avgPx": "", "fee": "0", "feeCcy": "USDT", "pnl": "0", "rebate": "0", "uTime": str(int(run.now * 1000))}
        run.fake.orders[body["clOrdId"]] = row
        run.bot["_orders"].append({"at": run.now, "request": body, "result": run.api.parse_order_result(row, body)})


class ControllerEdgeTests(unittest.TestCase):
    def fake_run(self, **kwargs):
        run = FictionalExchangeRun(recipe(reduce=kwargs.pop("reduce", False)), **kwargs)
        self.addCleanup(run.close)
        return run

    def paper_controller(self, *, side="long", callback=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        arena = Arena.__new__(Arena)
        arena.path = Path(temp.name)
        arena.session = paper.new_season([recipe(side, reduce=False)], 100, 300, 60, "f" * 64)
        arena.session["execution_mode"] = "shared_jev"
        arena.lock = threading.RLock()
        arena.now = 61
        arena.clock = lambda: arena.now
        arena.admission_deadline, arena.background = None, False
        arena.save = lambda **kwargs: None
        arena.release = lambda: None
        arena.jev_gate = FakePaperGate(callback)
        return arena

    def test_paper_stop_cancels_the_unsubmitted_reservation_as_well_as_pending(self):
        state, bot, _ = entry_reserved()
        paper.request_stop(state, 62, bot["id"], origin="fictional-owner")
        self.assertIsNone(bot["_pending"])
        self.assertIsNone(bot["_strategy_v2"]["reservation"])

    def test_paper_admission_cancels_the_unsubmitted_reservation(self):
        state, bot, _ = entry_reserved()
        close_admission(state, 360, paper.emit)
        self.assertIsNone(bot["_pending"])
        self.assertIsNone(bot["_strategy_v2"]["reservation"])

    def test_paper_outage_cancels_reservation_and_a_new_bar_can_qualify_again(self):
        state, bot, _ = entry_reserved()
        paper.failed_snapshot(state, 62)
        self.assertIsNone(bot["_strategy_v2"]["reservation"])
        paper.tick(state, market(120))
        self.assertEqual(bot["_pending"]["kind"], "entry")

    def test_paper_gap_cancels_an_unapproved_shared_jev_entry_even_before_any_reply(self):
        state, bot, _ = entry_reserved()
        state["execution_mode"] = "shared_jev"
        bot["_pending"]["jev_requested"] = True
        paper.tick(state, market(181))  # Observed120s gap; no entry POST/fill ever occurred.
        self.assertIsNone(bot["_pending"])
        self.assertIsNone(bot["_strategy_v2"]["reservation"])
        self.assertEqual(bot["position_btc"], 0)

    def test_paper_jev_hold_releases_reservation_before_a_later_free_retry(self):
        arena = self.paper_controller()
        arena.advance(market(61))
        bot = arena.session["bots"][0]
        self.assertIsNone(bot["_pending"])
        self.assertIsNone(bot["_strategy_v2"]["reservation"])
        arena.now = 120
        arena.advance(market(120))
        self.assertEqual(len(arena.jev_gate.candidates), 2)

    def test_paper_expired_jev_response_releases_the_never_submitted_reservation(self):
        arena = self.paper_controller()
        arena.jev_gate.callback = lambda: setattr(arena, "now", 71)
        arena.advance(market(61))
        self.assertIsNone(arena.session["bots"][0]["_pending"])
        self.assertIsNone(arena.session["bots"][0]["_strategy_v2"]["reservation"])

    def test_paper_both_gate_receives_the_selected_direction_from_the_reserved_signal(self):
        arena = self.paper_controller(side="both")
        arena.advance(market(61))
        self.assertEqual(arena.jev_gate.candidates[0][0]["side"], "long")

    def test_pending_ordinary_order_cannot_reuse_a_terminal_client_id_with_another_server_id(self):
        run = self.fake_run()
        run.step()
        owned = deepcopy(next(iter(run.fake.orders.values())))
        owned.update(ordId="999999", state="live", accFillSz="0")
        pending_transport(run, override=[owned])
        with self.assertRaises(PreflightError):
            run.lab.checked_account(run.state, run.bot, run.api)

    def test_pending_known_algo_id_cannot_mask_a_different_parent_client_id(self):
        run = self.fake_run()
        run.step()
        changed = deepcopy(next(iter(run.fake.algos.values())))
        changed["algoClOrdId"] = "fictional-foreign-parent"
        pending_transport(run, algo_override=[changed])
        with self.assertRaises(PreflightError):
            run.lab.checked_account(run.state, run.bot, run.api)

    def test_pending_linked_partial_child_is_owned_without_an_empty_client_id_wildcard(self):
        run = self.fake_run()
        _, child = partial_protective_child(run)
        pending_transport(run)
        observed_error = None
        try:
            run.lab.checked_account(run.state, run.bot, run.api)
        except PreflightError as error:
            observed_error = str(error)
        self.assertFalse(observed_error and "foreign" in observed_error, observed_error)
        self.assertEqual(Decimal(run.bot["_contracts"]), Decimal(".03"))
        self.assertEqual(run.bot["_orders"][-1]["_child_id"], child["ordId"])

    def test_a_linked_child_cannot_be_cancelled_by_an_empty_client_id(self):
        run = self.fake_run()
        partial_protective_child(run)
        # Ownership was separately validated by parent + exact child GET above.
        run.api.pending = lambda *args, **kwargs: []
        cancelled = []
        run.api.cancel = lambda identity: cancelled.append(identity)
        try:
            run.lab.checked_account(run.state, run.bot, run.api)
        except PreflightError:
            pass  # Waiting for terminal child/protection is allowed.
        self.assertNotIn("", cancelled)

    def test_unlinked_empty_client_id_pending_row_is_never_adopted_or_cancelled(self):
        run = self.fake_run()
        parent, child = partial_protective_child(run)
        child["state"] = "filled"
        child["sz"] = child["accFillSz"] = ".01"
        # Simulate a separate pending row whose ordId is absent from this parent's list.
        foreign = {**deepcopy(child), "ordId": "999999", "state": "live", "accFillSz": "0"}
        pending_transport(run, override=[foreign])
        with self.assertRaises(PreflightError):
            run.lab.checked_account(run.state, run.bot, run.api)
        self.assertEqual(run.fake.post_count("/api/v5/trade/cancel-order"), 0)

    def test_discretionary_partial_preserves_slots_for_a_later_entire_owner_close(self):
        run = self.fake_run(reduce=True)
        terminal_zero_fill_padding(run, exchange.LIMITS["max_order_intents_per_bot"] - 1)
        run.step()
        # A discretionary management action must not spend the final close reserve.
        self.assertEqual(run.fake.units, Decimal(".04"))
        self.assertEqual(run.fake.post_count("/api/v5/trade/order"), 0)
        self.assertIsNone(run.bot["_strategy_v2"]["position"].get("reduction_ticket"))
        exchange.stop(run.state, run.now, reason="Fictional owner Stop")
        run.step(1)
        self.assertEqual(run.fake.units, 0)
        self.assertEqual(run.fake.post_count("/api/v5/trade/order"), 1)

    def test_reduction_capacity_after_partial_fill_never_leaves_an_irrecoverable_closing_plan(self):
        run = self.fake_run(reduce=True, contracts=".08")
        terminal_zero_fill_padding(run, exchange.LIMITS["max_order_intents_per_bot"] - 21)
        run.fake.close_fraction = Decimal(".5")
        run.step()  # Last discretionary slot: target.02, actual fill.01, still owned.07.
        self.assertEqual(run.fake.units, Decimal(".07"))
        self.assertEqual(Decimal(run.bot["_reduce_remaining"]), Decimal(".01"))
        run.fake.close_fraction = Decimal(1)
        run.step(1)
        run.step(1)
        # Either finish the already-started target, or explicitly veto its unsubmitted
        # remainder and stay holding. An order-slot gate must not become a full exit.
        self.assertGreater(run.fake.units, 0)
        self.assertFalse(run.bot["stop_requested"])
        self.assertFalse(exchange.unresolved(run.bot))
        self.assertFalse(run.bot.get("_exit_requested"))

    def test_financial_observation_preserves_last_meaningful_action_reason(self):
        state, bot, _ = entry_reserved()
        message = "Fictional cause: entry cancelled because spread widened."
        paper.emit(state, 120, bot, 2, "entry_cancelled", message)
        paper.frame(state, 120, market(120))
        self.assertEqual(bot["last_reason"], message)
        self.assertTrue(any(e["code"] == "financial_observation" for e in state["events"]))


if __name__ == "__main__":
    unittest.main()
