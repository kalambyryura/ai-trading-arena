"""Pure preview parity and paired report invariants on fictional fixtures."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_strategy_v2 import recipe
from trading_lab.workshop_tools import lint_rules, explain_predicate, lots_preview, compare_trials
from trading_lab.strategy_v2 import ContractError
from trading_lab.exchange_engine import entry_body


class WorkshopTests(unittest.TestCase):
    def test_lint_explains_unbounded_hold_and_disabled_add(self):
        rule = recipe()
        rule["max_entries"] = 1
        result = lint_rules(rule)
        self.assertEqual({row["code"] for row in result["warnings"]},
                         {"authored_hold_unbounded", "add_disabled_by_entry_count"})

    def test_explain_group_verdict_differs_from_false_leaf_count(self):
        expr = {"any": [{"feature": "sma_gap_bps", "op": "gte", "value": 12},
                        {"feature": "return_5m_bps", "op": "gte", "value": 20}]}
        view = explain_predicate(expr, {"sma_gap_bps": 15, "return_5m_bps": 5})
        self.assertEqual(view["root_verdict"], "PASS")
        self.assertEqual(view["counts"]["false"], 1)

    def test_lot_preview_exact_rounding_parity_with_offline_executor_body(self):
        snapshot = {"bid": 83999.3, "ask": 84000.7, "bid_btc": 10, "ask_btc": 10}
        spec = {"tick_size": ".1", "lot_step": ".01", "contract_value": ".01",
                "contract_multiplier": "1", "minimum_order_units": ".01"}
        for side in ("long", "short"):
            preview = lots_preview(budget_usdt=100, size_pct=30, side=side, snapshot=snapshot,
                                   instrument=spec, equity_usdt=100)
            frozen = {"id": "fixture", "rules": {"side": side, "size_pct": 30}}
            body = entry_body({"budget_usdt": 100, "strategies": [frozen]},
                              {"id": "fixture", "_instrument": spec, "_contracts": "0", "_fee_rate": "-.0005",
                               "equity_usdt": 100}, frozen, snapshot, "fake-do-not-send")
            self.assertTrue(preview["allowed"])
            self.assertEqual(float(preview["contracts"]), float(body["sz"]))
            self.assertEqual(preview["limit_price_usdt"], float(body["px"]))

    def test_lot_preview_explains_minimum_and_liquidity(self):
        kwargs = {"budget_usdt": 25, "size_pct": 5, "side": "long", "equity_usdt": 25,
                  "snapshot": {"bid": 83999, "ask": 84000, "bid_btc": 1, "ask_btc": 1},
                  "instrument": {"tick_size": ".1", "lot_step": ".01", "contract_value": ".01",
                                 "contract_multiplier": "1", "minimum_order_units": ".01"}}
        self.assertIn("minimum_order_units", lots_preview(**kwargs)["veto_reasons"])
        kwargs.update(budget_usdt=100, size_pct=30, equity_usdt=100)
        kwargs["snapshot"]["ask_btc"] = .000001
        self.assertIn("top_book_fraction", lots_preview(**kwargs)["veto_reasons"])

    def test_paired_trial_reports_preserve_missing_not_zero(self):
        report = {"data_sha256": "a" * 64, "code_sha256": "b" * 64, "dataset_id": "fictional",
                  "dataset_kind": "SYNTHETIC", "budget_usdt": 100, "runtime": "RULES_ONLY", "status": "COMPLETE",
                  "runs": [{"name": "fixture", "status": "COMPLETE", "admission_seconds": 300, "time_policy": "admission_then_drain", "metrics": {"pnl_usdt": 1, "trades": 1,
                            "fees_usdt": .1, "funding_usdt": None, "max_drawdown_pct": .1}}]}
        candidate = deepcopy(report)
        candidate["runs"][0]["metrics"]["pnl_usdt"] = 2
        paired = compare_trials(report, candidate)["paired_runs"][0]["delta_right_minus_left"]
        self.assertEqual(paired["pnl_usdt"], 1)
        self.assertIsNone(paired["funding_usdt"])
        candidate["data_sha256"] = "c" * 64
        with self.assertRaises(ContractError):
            compare_trials(report, candidate)

    def test_incomplete_or_cross_engine_trials_reject(self):
        report = {"data_sha256": "a", "code_sha256": "b", "dataset_id": "fixture", "dataset_kind": "SYNTHETIC",
                  "budget_usdt": 100, "runtime": "RULES_ONLY", "status": "COMPLETE", "runs": []}
        for changes in ({"status": "INCOMPLETE"}, {"code_sha256": "different"}, {"data_sha256": None}):
            with self.assertRaises(ContractError):
                compare_trials(report, {**report, **changes})


if __name__ == "__main__":
    unittest.main()
