"""Isolated evidence-projection acceptance tests, with no live transports.

Run from project root with py -3.12 -X utf8 <this-file>.
Existing ExchangeTests fixture uses temporary fake keys, injected transports and
background=False. Production roots/controllers/ledgers are never opened.
"""
from copy import deepcopy
from decimal import Decimal
import unittest
from unittest.mock import patch

from trading_lab import cycle_diagnostics as draft



def state_for(names=("Sol",), max_entries=1, probe=False):
    return {"status": "RUNNING", "bots": [{"id": name, "name": name, "status": "WAITING",
            "position_btc": 0, "ai_requests": 0, "ai_cost_usd": 0, "ai_cost_complete": True,
            "accounting_reconciled": False, "funding_reconciled": False, "_orders": []}
            for name in names] + [{"id": "cash", "name": "Cash"}],
            "strategies": [{"id": name, "rules": {"max_entries": max_entries},
                **({"test_plan": "demo_cycle_v1"} if probe else {})} for name in names], "events": []}


def event(state, bot, code, data=None, step=1, message=""):
    row = {"seq": len(state["events"]) + 1, "at": "2026-09-30T12:00:%02dZ" % (len(state["events"]) % 60),
           "bot_id": bot["id"] if bot else None, "code": code, "step": step,
           "message": message, "data": data or {}}
    state["events"].append(row)
    return row


def filled(state, bot, key, qty=".02", side="buy", reduce=False, fee="-.01", gross="0"):
    request = {"clOrdId": key, "reduceOnly": reduce, "side": side}
    result = {"ordId": "order_" + key, "clOrdId": key, "contracts": qty, "state": "filled",
              "signed_fee_usdt": fee, "gross_pnl_usdt": gross, "average_price": "84000"}
    order = {"request": request, "result": result}
    bot["_orders"].append(order)
    event(state, bot, "exchange_order_intent", {"request": request}, 6 if reduce else 3)
    evidence = event(state, bot, "exchange_close_fill" if reduce else "exchange_entry_fill", {"result": result}, 6 if reduce else 3)
    sign = 1 if side == "buy" else -1
    bot["position_btc"] = float(Decimal(str(bot["position_btc"])) + Decimal(qty) * Decimal(".01") * sign)
    return order, evidence


def checked(state, bot, opened=False, passed=True, kind=None):
    return event(state, bot, "position_checks" if opened else "entry_checks", {
        "kind": kind or ("add" if opened else "entry"), "local_checks_passed": passed,
        "checks": [{"feature": "return_5m_bps", "actual": 10 if passed is not None else None,
                    "expected": 20, "op": "gte", "passed": passed}]}, 4 if opened else 1)


def jev(state, bot, kind="entry", allowed=True, cost=".0001", confidence=.99, choice="proceed"):
    bot["ai_requests"] += 1
    bot["ai_cost_usd"] += float(cost or 0)
    return event(state, bot, "jev_decision", {"kind": kind, "ai_used": True,
        "reason": "typed_answer", "answer": {"choice": choice, "confidence": confidence},
        "jev_allowed": allowed, "blocked_reason": "low_confidence" if confidence < .7 else "choice_hold" if choice == "hold" else None,
        "confidence_threshold": .7, "reported_batch_cost_usd": float(cost) if cost is not None else None}, 2 if kind == "entry" else 5)


def settled(state, bot, gross="0", fees="-.02", funding="0"):
    cash = Decimal(gross) + Decimal(fees) + Decimal(funding)
    bot.update(status="STOPPED", accounting_reconciled=True, funding_reconciled=True,
               accounting={"gross_pnl_usdt": gross, "signed_fees_usdt": fees,
                           "signed_funding_usdt": funding, "cash_delta_usdt": str(cash),
                           "cash_delta_matches": True})
    return event(state, bot, "accounting_confirmed", bot["accounting"], 6)


class EvidenceTests(unittest.TestCase):
    def test_second_bot_execution_is_visible_and_noadd_applicability_is_six(self):
        state = state_for(("Sol", "Astra")); sol, astra = state["bots"][:2]
        checked(state, sol, passed=False)
        checked(state, astra); jev(state, astra)
        filled(state, astra, "E"); checked(state, astra, opened=True, passed=False)
        filled(state, astra, "C", side="sell", reduce=True, gross=".01624", fee="-.00843482")
        settled(state, astra, gross=".01624", fees="-.01686152")
        settled(state, sol, fees="0")
        state["status"] = "DONE"
        evidence = draft.lifecycle(state)
        self.assertEqual(len(evidence["bots"]), 2)
        self.assertEqual([b["required_count"] for b in evidence["bots"]], [6, 6])
        self.assertEqual([b["observed_required_count"] for b in evidence["bots"]], [2, 6])
        self.assertFalse(evidence["all_stages_observed"])
        self.assertTrue(evidence["bots"][1]["complete"])
        self.assertEqual(evidence["bots"][1]["costs"]["net_usdt"], "-0.00062152")
        self.assertEqual(evidence["total"], 12); self.assertEqual(evidence["completed"], 8)
        self.assertTrue(all("bot_id" in r for r in evidence["rows"]))
        for bot in evidence["bots"]:
            for row in bot["rows"]:
                if row["id"] in {"jev_hold", "jev_add", "add"}:
                    self.assertEqual(row["status"], "not_applicable")

    def test_common_accounting_event_requires_each_bots_own_flags(self):
        state = state_for(("One", "Two")); first, second = state["bots"][:2]
        first.update(accounting_reconciled=True, funding_reconciled=True)
        state["status"] = "DONE"; event(state, None, "season_done", step=6)
        data = draft.lifecycle(state)["bots"]
        self.assertEqual(next(r for r in data[0]["rows"] if r["id"] == "accounting")["status"], "observed")
        self.assertEqual(next(r for r in data[1]["rows"] if r["id"] == "accounting")["status"], "not_observed")

    def test_repeated_cumulative_partial_fills_do_not_become_adds_or_duplicate_fees(self):
        state = state_for(max_entries=2); bot = state["bots"][0]
        order, evidence = filled(state, bot, "E", qty=".01", fee="-.005")
        event(state, bot, "exchange_entry_fill", {"result": deepcopy(order["result"])}, 3)
        order["result"] = {**order["result"], "contracts": ".02", "signed_fee_usdt": "-.01"}
        event(state, bot, "exchange_entry_fill", {"result": deepcopy(order["result"])}, 3)
        bot["position_btc"] = .0002
        filled(state, bot, "C", side="sell", reduce=True, fee="-.01")
        bot["_next_funding_read"] = 10; bot["_funding"] = "0"
        data = draft.lifecycle(state)["bots"][0]
        self.assertEqual(data["funnel"]["entry_filled_orders"], 1)
        self.assertEqual(data["funnel"]["add_filled_orders"], 0)
        self.assertEqual(data["funnel"]["round_trips"], 1)
        self.assertEqual(data["costs"]["signed_fees_usdt"], "-0.02")
        self.assertEqual(next(r for r in data["rows"] if r["id"] == "entry")["event_seq"], evidence["seq"])

    def test_two_short_roundtrips_are_two_entries_and_buy_is_close(self):
        state = state_for(max_entries=2); bot = state["bots"][0]
        filled(state, bot, "E1", side="sell")
        filled(state, bot, "C1", side="buy", reduce=True)
        filled(state, bot, "E2", side="sell")
        filled(state, bot, "C2", side="buy", reduce=True)
        data = draft.lifecycle(state)["bots"][0]
        self.assertEqual(data["funnel"]["entry_filled_orders"], 2)
        self.assertEqual(data["funnel"]["add_filled_orders"], 0)
        self.assertEqual(data["funnel"]["round_trips"], 2)
        self.assertEqual(next(r for r in data["rows"] if r["id"] == "close")["status"], "observed")

    def test_separate_fill_inside_same_short_position_is_add(self):
        state = state_for(max_entries=2); bot = state["bots"][0]
        filled(state, bot, "E", side="sell"); filled(state, bot, "A", side="sell")
        filled(state, bot, "C", qty=".04", side="buy", reduce=True)
        data = draft.lifecycle(state)["bots"][0]
        self.assertEqual(data["funnel"]["entry_filled_orders"], 1)
        self.assertEqual(data["funnel"]["add_filled_orders"], 1)
        self.assertEqual(next(r for r in data["rows"] if r["id"] == "add")["status"], "observed")

    def test_repeat_of_old_close_after_flat_owner_stop_does_not_prove_stop_close(self):
        state = state_for(); bot = state["bots"][0]
        filled(state, bot, "E"); close, _ = filled(state, bot, "C", side="sell", reduce=True)
        event(state, bot, "stop_requested", step=6, message="Зупинка власником.")
        close["result"]["signed_fee_usdt"] = "-.0101"
        event(state, bot, "exchange_close_fill", {"result": deepcopy(close["result"])}, 6)
        state["status"] = "DONE"
        rows = draft.lifecycle(state)["bots"][0]["rows"]
        self.assertEqual(next(r for r in rows if r["id"] == "close")["status"], "observed")
        self.assertEqual(next(r for r in rows if r["id"] == "owner_stop")["status"], "not_observed")

    def test_owner_stop_inside_open_short_requires_actual_owned_close(self):
        state = state_for(); bot = state["bots"][0]
        filled(state, bot, "E", side="sell")
        event(state, bot, "stop_requested", step=6, message="Зупинка власником.")
        _, close = filled(state, bot, "C", side="buy", reduce=True)
        rows = draft.lifecycle(state)["bots"][0]["rows"]
        self.assertEqual(next(r for r in rows if r["id"] == "owner_stop")["event_seq"], close["seq"])

    def test_owner_stop_while_flat_then_inflight_fill_is_not_open_position_stop_coverage(self):
        state = state_for(); bot = state["bots"][0]
        event(state, bot, "stop_requested", step=6, message="Зупинка власником.")
        filled(state, bot, "E"); filled(state, bot, "C", side="sell", reduce=True)
        state["status"] = "DONE"
        rows = draft.lifecycle(state)["bots"][0]["rows"]
        self.assertEqual(next(r for r in rows if r["id"] == "owner_stop")["status"], "not_observed")

    def test_service_timeout_is_not_owner_stop(self):
        state = state_for(); bot = state["bots"][0]
        filled(state, bot, "E")
        event(state, bot, "stop_requested", step=6, message="Час сезону завершено.")
        filled(state, bot, "C", side="sell", reduce=True)
        state["status"] = "DONE"
        rows = draft.lifecycle(state)["bots"][0]["rows"]
        self.assertEqual(next(r for r in rows if r["id"] == "owner_stop")["status"], "not_observed")

    def test_flat_waits_position_holds_jev_veto_and_unknown_are_not_mixed(self):
        state = state_for(max_entries=2); bot = state["bots"][0]
        checked(state, bot, passed=False); event(state, bot, "no_signal")
        checked(state, bot); jev(state, bot, allowed=False, confidence=.36)
        before = draft.lifecycle(state)["bots"][0]
        self.assertEqual(before["wait_reason"]["code"], "low_confidence")
        filled(state, bot, "E")
        checked(state, bot, opened=True, passed=False); event(state, bot, "no_signal", step=5)
        checked(state, bot, opened=True, passed=None)
        jev(state, bot, kind="add", allowed=False, choice="hold")
        funnel = draft.lifecycle(state)["bots"][0]["funnel"]
        self.assertEqual(funnel["entry"]["checks"], 2)
        self.assertEqual(funnel["add"]["checks"], 2)
        self.assertEqual(funnel["entry"]["signals"], 1)
        self.assertEqual(funnel["entry"]["jev_vetoes"], 1)
        self.assertEqual(funnel["add"]["unknown_checks"], 1)
        self.assertEqual(funnel["add"]["jev_holds"], 1)
        self.assertEqual(funnel["flat_waits"], 1); self.assertEqual(funnel["position_holds"], 1)

    def test_signed_rebates_and_funding_do_not_subtract_positive_income_or_jev_usd(self):
        state = state_for(); bot = state["bots"][0]
        settled(state, bot, gross="1", fees=".02", funding="-.03")
        bot["ai_cost_usd"] = .005
        data = draft.lifecycle(state)["bots"][0]["costs"]
        self.assertEqual(data["net_usdt"], "0.99")
        self.assertEqual(data["signed_fees_usdt"], "0.02")
        self.assertEqual(data["jev"]["known_cost_usd"], "0.005")
        self.assertTrue(data["complete"])

    def test_shared_batch_known_cost_is_perbot_estimate_and_unknown_remains_unknown(self):
        state = state_for(("One", "Two"))
        for bot in state["bots"][:2]:
            jev(state, bot, cost=".001"); bot["ai_cost_usd"] = .0005
        data = draft.lifecycle(state)["bots"]
        self.assertEqual(sum(Decimal(b["costs"]["jev"]["known_cost_usd"]) for b in data), Decimal(".001"))
        first = state["bots"][0]; jev(state, first, cost=None)
        first["ai_cost_complete"] = False
        cost = draft.lifecycle(state)["bots"][0]["costs"]["jev"]
        self.assertEqual(cost["unknown_billing_requests"], 1)
        self.assertFalse(cost["complete"])

    def test_old_log_missing_requests_is_not_upgraded_and_funding_zero_is_not_guessed(self):
        state = state_for(); bot = state["bots"][0]
        event(state, bot, "exchange_entry_fill", {"result": {"ordId": "O", "contracts": ".02",
                    "state": "filled", "gross_pnl_usdt": "0", "signed_fee_usdt": "-.01"}}, 3)
        bot.pop("_orders"); bot["_funding"] = "0"
        original = deepcopy(state)
        data = draft.lifecycle(state)["bots"][0]
        self.assertEqual(state, original)
        self.assertEqual(data["funnel"]["entry_filled_orders"], 0)
        self.assertIsNone(data["costs"]["signed_funding_usdt"])
        self.assertIsNone(data["costs"]["net_usdt"])
        self.assertFalse(data["complete"])
        self.assertTrue(any("Старий fill" in x for x in data["limitations"]))

    def test_partial_old_public_log_can_use_own_intents_but_not_another_bots_fill(self):
        state = state_for(("One", "Two")); first, second = state["bots"][:2]
        filled(state, first, "E"); filled(state, first, "C", side="sell", reduce=True)
        first.pop("_orders"); second.pop("_orders")
        data = draft.lifecycle(state)["bots"]
        self.assertEqual(data[0]["funnel"]["round_trips"], 1)
        self.assertEqual(data[1]["funnel"]["entry_filled_orders"], 0)
        self.assertEqual(data[1]["funnel"]["round_trips"], 0)

    def test_drain_phase_does_not_claim_new_entry_and_unknown_recipe_never_complete(self):
        state = state_for(); bot = state["bots"][0]
        filled(state, bot, "E"); state["status"] = "DRAINING"
        self.assertEqual(draft.lifecycle(state)["bots"][0]["wait_reason"]["code"], "entry_window_closed_position_managed")
        state["strategies"] = []
        self.assertFalse(draft.lifecycle(state)["bots"][0]["complete"])

    def test_drain_notice_does_not_hide_actual_close_or_market_failure(self):
        state = state_for(); bot = state["bots"][0]
        filled(state, bot, "E"); state["status"] = "DRAINING"
        bot["status"] = "CLOSING"; bot["_close_reason"] = "owner_stop"
        result = draft.lifecycle(state)["bots"][0]
        self.assertEqual(result["current_phase"]["code"], "closing")
        self.assertEqual(result["wait_reason"]["code"], "closing")
        bot["status"] = "HOLDING"
        event(state, bot, "market_unavailable", {"reason": "quote_stale"}, message="Немає свіжих даних.")
        result = draft.lifecycle(state)["bots"][0]
        self.assertEqual(result["wait_reason"]["code"], "quote_stale")
        self.assertEqual(result["wait_reason"]["message"], "Немає свіжих даних.")

    def test_uncertain_post_has_unknown_costs_until_cumulative_result_arrives(self):
        state = state_for(); bot = state["bots"][0]
        request = {"clOrdId": "maybe_filled", "side": "buy", "reduceOnly": False}
        bot["_orders"].append({"request": request, "result": None})
        event(state, bot, "exchange_order_intent", {"request": request}, 3)
        event(state, bot, "order_ack_uncertain", step=3)
        data = draft.lifecycle(state)["bots"][0]
        self.assertIsNone(data["costs"]["gross_pnl_usdt"])
        self.assertIsNone(data["costs"]["signed_fees_usdt"])
        self.assertFalse(data["costs"]["complete"])

    def test_old_missing_gate_boolean_is_unknown_signal_not_false_signal(self):
        state = state_for(); bot = state["bots"][0]
        event(state, bot, "entry_checks", {"checks": [{"feature": "spread_bps", "passed": True}]})
        result = draft.lifecycle(state)["bots"][0]["funnel"]["entry"]
        self.assertEqual(result["signals"], 0)
        self.assertEqual(result["signals_unknown"], 1)

    def test_zero_fill_terminal_ioc_is_not_entry_or_add(self):
        state = state_for(max_entries=2); bot = state["bots"][0]
        order, _ = filled(state, bot, "NOFILL", qty="0", fee="0")
        order["result"]["state"] = "canceled"; state["status"] = "DONE"
        data = draft.lifecycle(state)["bots"][0]
        self.assertEqual(data["funnel"]["entry_filled_orders"], 0)
        self.assertEqual(next(r for r in data["rows"] if r["id"] == "entry")["status"], "not_observed")
        self.assertEqual(next(r for r in data["rows"] if r["id"] == "add")["status"], "not_observed")


class FakeIntegrationTests(unittest.TestCase):
    def setUp(self):
        from test_exchange_lab import ExchangeTests
        from trading_lab import exchange_engine, cycle_probe
        self.fixture = ExchangeTests("test_full_actual_fake_cycle_fees_bills_cash_and_report")
        self.fixture.setUp()
        self.patch_engine = patch.object(exchange_engine, "lifecycle", draft.lifecycle)
        self.patch_probe = patch.object(cycle_probe, "lifecycle", draft.lifecycle)
        self.patch_engine.start(); self.patch_probe.start()

    def tearDown(self):
        self.patch_engine.stop(); self.patch_probe.stop(); self.fixture.tearDown()

    def test_existing_full_demo_probe_and_stop_acceptance_still_pass_with_nine_required(self):
        self.fixture.test_probe_full_cycle_in_losing_market_has_all_nine_evidenced_stages()
        # A separate test has its own root and ledger; never reset a completed run.
        self.tearDown(); self.setUp()
        self.fixture.test_probe_stop_during_add_approval_closes_only_owned_fill()

    def test_one_waiting_bot_and_one_actual_fake_trade_remain_separate(self):
        from trading_lab.arena_strategies import catalogue_defaults
        from trading_lab.strategy_contract import payload_of
        f = self.fixture
        original = deepcopy(catalogue_defaults()[0]); first = payload_of(original)
        first["name"] = "Waiting fake contender"
        first["rules"]["entry"]["conditions"] = [{"feature": "spread_bps", "op": "gte", "value": 1000}]
        idle = f.arena.add_strategy(0, first)
        second = payload_of(original)
        second["name"] = "Actual fake contender"
        created = f.arena.add_strategy(f.arena.data["generation"], second)
        state = f.start("arena_live", [idle["id"], created["id"]], ["arena-a", "arena-b"])
        f.lab.step(state)
        self.assertEqual(f.fakes["arena-a"].units, 0)
        self.assertGreater(f.fakes["arena-b"].units, 0)
        f.now += 30; f.lab.step(state)
        f.lab.stop(f.lab.data["generation"], state["id"])
        f.now += 1; f.lab.step(state); f.now += 1; f.lab.step(state)
        data = f.lab.history(state["id"])["lifecycle"]["bots"]
        self.assertEqual([b["funnel"]["entry_filled_orders"] for b in data], [0, 1])
        self.assertEqual([b["complete"] for b in data], [False, True])
        self.assertTrue(data[1]["costs"]["complete"])
        self.assertEqual(data[1]["costs"]["source"], "actual_bills_and_cash_reconciled")
        self.assertEqual([next(r for r in b["rows"] if r["id"] == "owner_stop")["status"] for b in data], ["not_observed", "observed"])


class PaperEvidenceTests(unittest.TestCase):
    def test_free_rules_probe_uses_model_labels_and_five_applicable_stages(self):
        from test_arena import state, market
        from trading_lab.arena_engine import tick, request_stop
        run = state(); tick(run, market(1000)); tick(run, market(1001)); tick(run, market(1030))
        request_stop(run, 1031); tick(run, market(1032)); tick(run, market(1033))
        evidence = draft.lifecycle(run)["bots"][0]
        self.assertEqual(evidence["required_count"], 5)
        self.assertTrue(evidence["complete"])
        self.assertEqual(evidence["costs"]["source"], "paper_model_events")
        self.assertTrue(evidence["costs"]["complete"])
        self.assertEqual(evidence["costs"]["jev"]["source"], "not_used_rules_only")
        self.assertEqual(evidence["funnel"]["source"], "paper_model")
        self.assertFalse(any("OKX" in r["label"] for r in evidence["rows"]))
        self.assertTrue(all(r["status"] == "not_applicable" for r in evidence["rows"]
                            if r["id"] in {"jev_entry", "jev_hold", "jev_add", "add"}))
        # This old generic Stop event can also be emitted by the engine for risk.
        self.assertEqual(next(r for r in evidence["rows"] if r["id"] == "owner_stop")["status"], "not_observed")

    def test_paper_funding_uses_signed_payment_and_deduplicates_seq(self):
        from test_arena import state, market
        from trading_lab.arena_engine import tick, request_stop
        run = state(); tick(run, market(1000)); tick(run, market(1001)); tick(run, market(1030))
        request_stop(run, 1031); tick(run, market(1032)); tick(run, market(1033))
        bot = run["bots"][0]
        first = draft.lifecycle(run)["bots"][0]["costs"]
        bot["funding_usdt"] = -.03; bot["realized_pnl_usdt"] -= .03
        bot["pnl_usdt"] -= .03
        # Duplicating a sequence is not another simulated fill or fee.
        run["events"].append(deepcopy(next(e for e in run["events"] if e["code"] == "paper_entry_fill")))
        last = draft.lifecycle(run)["bots"][0]
        self.assertEqual(last["costs"]["signed_funding_usdt"], "-0.03")
        self.assertEqual(Decimal(last["costs"]["net_usdt"]) - Decimal(first["net_usdt"]), Decimal("-.03"))
        self.assertEqual(last["funnel"]["entry_filled_orders"], 1)
        self.assertEqual(last["costs"]["signed_fees_usdt"], first["signed_fees_usdt"])

    def test_paper_partial_short_closes_one_roundtrip_and_owner_origin_is_explicit(self):
        from test_arena import state, market
        from trading_lab.arena_engine import tick, request_stop
        run = state("short"); tick(run, market(1000)); tick(run, market(1001))
        request_stop(run, 1002)
        own_stop = next(e for e in run["events"] if e["code"] == "stop_requested" and e["bot_id"] != "cash")
        own_stop["data"]["origin"] = "owner"
        for when in range(1003, 1010):
            quote = market(when); quote["ask_btc"] = .5
            tick(run, quote)
            if run["status"] == "DONE":
                break
        evidence = draft.lifecycle(run)["bots"][0]
        self.assertEqual(evidence["funnel"]["entry_filled_orders"], 1)
        self.assertGreater(evidence["funnel"]["close_filled_orders"], 1)
        self.assertEqual(evidence["funnel"]["round_trips"], 1)
        self.assertEqual(next(r for r in evidence["rows"] if r["id"] == "owner_stop")["status"], "observed")
        self.assertEqual(next(r for r in evidence["rows"] if r["id"] == "close")["status"], "observed")

    def test_paper_cash_delta_does_not_pretend_to_be_actual_exchange_cash(self):
        from test_arena import state, market
        from trading_lab.arena_engine import tick, request_stop
        run = state(); tick(run, market(1000)); tick(run, market(1001))
        request_stop(run, 1002); tick(run, market(1003)); tick(run, market(1004))
        costs = draft.lifecycle(run)["bots"][0]["costs"]
        self.assertIsNone(costs["cash_delta_usdt"])
        self.assertIsNotNone(costs["model_cash_delta_usdt"])
        self.assertEqual(costs["funding_source"], "estimated_paper_funding")

    def test_paper_old_partial_close_without_entry_never_invents_owned_history(self):
        from test_arena import state
        run = state(); bot = run["bots"][0]
        event(run, bot, "paper_close_fill", {"quantity_btc": .19, "remaining_btc": 0,
                    "fee_usdt": .01, "gross_pnl_usdt": .02}, 6)
        run["status"] = "DONE"; bot["status"] = "STOPPED"
        result = draft.lifecycle(run)["bots"][0]
        self.assertEqual(result["funnel"]["round_trips"], 0)
        self.assertFalse(result["costs"]["complete"])
        self.assertIsNone(result["costs"]["net_usdt"])

    def test_paper_model_add_and_next_roundtrip_are_different(self):
        from test_arena import state
        run = state("short"); bot = run["bots"][0]
        bot["position_btc"] = 0
        for code, data in (
                ("paper_entry_fill", {"side": "short", "quantity_btc": .1, "fee_usdt": .005}),
                ("paper_entry_fill", {"side": "short", "quantity_btc": .1, "fee_usdt": .005}),
                ("paper_close_fill", {"quantity_btc": .2, "remaining_btc": 0, "fee_usdt": .01, "gross_pnl_usdt": .1}),
                ("paper_entry_fill", {"side": "short", "quantity_btc": .1, "fee_usdt": .005}),
                ("paper_close_fill", {"quantity_btc": .1, "remaining_btc": 0, "fee_usdt": .005, "gross_pnl_usdt": .1})):
            event(run, bot, code, data, 3 if code == "paper_entry_fill" else 6)
        result = draft.lifecycle(run)["bots"][0]["funnel"]
        self.assertEqual(result["entry_filled_orders"], 2)
        self.assertEqual(result["add_filled_orders"], 1)
        self.assertEqual(result["round_trips"], 2)

    def test_shared_jev_paper_gate_projects_true_attempt_allowed_and_separate_cost(self):
        from test_arena_jev import ArenaJevIntegrationTests
        f = ArenaJevIntegrationTests("test_common_gate_then_next_quote_fill_and_deterministic_close_without_jev")
        f.setUp()
        try:
            f.start(); f.advance(1000); f.advance(1005); f.advance(1030)
            f.arena.stop(f.arena.data["generation"], f.arena.session["id"])
            f.advance(1035); f.advance(1040)
            result = draft.lifecycle(f.arena.session)["bots"][0]
            self.assertEqual(result["required_count"], 6)
            self.assertTrue(result["complete"])
            self.assertEqual(result["funnel"]["entry"]["jev_calls"], 1)
            self.assertEqual(result["funnel"]["entry"]["jev_allows"], 1)
            self.assertEqual(result["costs"]["jev"]["known_cost_usd"], "0.00001")
            self.assertEqual(next(r for r in result["rows"] if r["id"] == "jev_hold")["status"], "not_applicable")
        finally:
            f.tearDown()

    def test_shared_jev_paper_hold_is_veto_not_a_position_hold(self):
        from test_arena_jev import ArenaJevIntegrationTests
        f = ArenaJevIntegrationTests("test_hold_and_local_failed_checks_never_reach_fill")
        f.setUp()
        try:
            f.result = "hold"; f.start(); f.advance(1000); f.advance(1005)
            result = draft.lifecycle(f.arena.session)["bots"][0]
            self.assertEqual(result["funnel"]["entry"]["jev_holds"], 1)
            self.assertEqual(result["funnel"]["entry"]["jev_vetoes"], 1)
            self.assertEqual(result["funnel"]["position_holds"], 0)
            self.assertEqual(result["funnel"]["entry_filled_orders"], 0)
            self.assertFalse(result["complete"])
        finally:
            f.tearDown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
