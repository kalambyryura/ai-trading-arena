import copy
import json
import unittest
from trading_lab.battle_review import build_review, verify_bundle
from trading_lab.arena_strategies import ArenaError
from tests.test_cycle_diagnostics import state_for, event, filled, checked, jev, settled


class ReviewTests(unittest.TestCase):
    def fixture(self):
        state = state_for(("Sol", "Astra"))
        state.update(id="a" * 32, mode="OKX_LIVE", budget_usdt=100, frames=[])
        sol, astra = state["bots"][:2]
        checked(state, sol, passed=False)
        checked(state, astra); jev(state, astra)
        order, _ = filled(state, astra, "PRIVATE_CLIENT", qty=".01", fee="-.005")
        event(state, astra, "exchange_entry_fill", {"result": copy.deepcopy(order["result"])}, 3)
        order["result"] = {**order["result"], "contracts": ".02", "signed_fee_usdt": "-.01"}
        event(state, astra, "exchange_entry_fill", {"result": copy.deepcopy(order["result"])}, 3)
        astra["position_btc"] = .0002
        filled(state, astra, "PRIVATE_CLOSE", side="sell", reduce=True, gross=".014", fee="-.01")
        settled(state, astra, gross=".014", fees="-.02", funding="-.001")
        settled(state, sol, fees="0")
        state["status"] = "DONE"
        state["strategies"][0]["author"] = {"prompt": "PRIVATE_PROMPT", "uid": "PRIVATE_UID"}
        event(state, astra, "private_future_field", {"uid": "PRIVATE_UID", "authorization": "PRIVATE_KEY", "nested": {"prompt": "PRIVATE_PROMPT"}})
        return state

    def test_equal_bundle_is_exact_and_private_identifiers_never_escape(self):
        state = self.fixture(); before = copy.deepcopy(state)
        review = build_review(state)
        self.assertTrue(verify_bundle(review)["verified"])
        self.assertEqual(review["participants"][1]["costs"]["net_usdt"], "-0.007")
        self.assertEqual(review["participants"][1]["funnel"]["round_trips"], 1)
        self.assertEqual(len(review["participants"][1]["trade_ledger"]["round_trips"]), 1)
        serialized = json.dumps(review)
        for private in ("PRIVATE_CLIENT", "PRIVATE_CLOSE", "PRIVATE_UID", "PRIVATE_KEY", "PRIVATE_PROMPT"):
            self.assertNotIn(private, serialized)
        self.assertEqual(state, before)

    def test_tamper_and_missing_reference_rejected(self):
        bundle = build_review(self.fixture())
        bundle["participants"][1]["costs"]["net_usdt"] = "1"
        with self.assertRaises(ArenaError): verify_bundle(bundle)

    def test_unknown_results_are_unknown_not_cash(self):
        state = state_for(); state.update(mode="OKX_DEMO", budget_usdt=100)
        bot = state["bots"][0]
        bot["_orders"] = [{"request": {"clOrdId": "pending", "reduceOnly": False, "side": "buy"}, "result": None}]
        result = build_review(state)["participants"][0]
        self.assertIsNone(result["costs"]["net_usdt"])
        self.assertFalse(result["costs"]["complete"])
        self.assertFalse(result["quality"]["exact_exit_trigger_recorded"])

    def test_duplicate_partial_updates_are_one_entry(self):
        bundle = build_review(self.fixture()); bot = bundle["participants"][1]
        self.assertEqual(bot["funnel"]["entry_filled_orders"], 1)
        self.assertEqual(bot["funnel"]["add_filled_orders"], 0)
        entries = [r for r in bot["trade_ledger"]["rows"] if r["role"] == "entry"]
        self.assertEqual([r["quantity"] for r in entries], ["0.01", "0.01"])
        self.assertEqual([r["signed_fees_usdt"] for r in entries], ["-0.005", "-0.005"])

    def test_non_contiguous_history_rejected(self):
        state = self.fixture(); state["events"].pop(1)
        with self.assertRaises(ArenaError): build_review(state)


if __name__ == "__main__": unittest.main()
