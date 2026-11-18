"""Optional authored reductions, priority and durable one-per-bar tickets."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trading_lab import strategy_v2 as v2
from test_strategy_v2 import recipe, observation, leaf


class ReductionTests(unittest.TestCase):
    def setUp(self):
        rule = recipe()
        rule.update(reduce_when=leaf("gross_gain_bps", "gte", 20), reduce_pct=25, cost_checkpoint=None)
        self.rules = v2.validate_rules(rule)
        self.state, _ = v2.advance_bar(self.rules, v2.new_state(self.rules, "b" * 64), observation(60))
        self.state = v2.sync_position(self.state, "owned-first-fill", 65, "long", 11)
        self.state, _ = v2.advance_bar(self.rules, self.state, observation(120), {"gross_gain_bps": 25})

    def test_exit_then_reduce_then_add_priority(self):
        values = {"gross_gain_bps": 25, "position_age_seconds": 55}
        reduced = v2.position_view(self.rules, self.state, {}, values)
        self.assertTrue(reduced["reduce"])
        self.assertFalse(reduced["add"])
        self.assertEqual(reduced["reduce_pct"], 25)
        rule = deepcopy(self.rules)
        rule["exit"] = leaf("gross_gain_bps", "gte", 20)
        state = v2.new_state(rule, "b" * 64)
        state = v2.sync_position(state, "owned-first-fill", 65, "long", 11)
        exited = v2.position_view(rule, state, {}, values)
        self.assertTrue(exited["exit"])
        self.assertFalse(exited["reduce"])
        self.assertFalse(exited["add"])

    def test_unknown_reduction_does_not_become_add_permission(self):
        managed = v2.position_view(self.rules, self.state, {}, {"gross_gain_bps": None})
        self.assertFalse(managed["reduce"])
        self.assertFalse(managed["add"])
        self.assertFalse(managed["exit"])

    def test_exactly_once_ticket_survives_restart_and_partial_remains_open(self):
        self.state = v2.consume_reduction(self.rules, self.state, observation(120), "partial-intent-1")
        restored = deepcopy(self.state)
        self.assertEqual(self.state, v2.consume_reduction(self.rules, restored, observation(120), "partial-intent-1"))
        with self.assertRaises(v2.ContractError):
            v2.consume_reduction(self.rules, self.state, observation(120), "partial-intent-2")
        self.assertFalse(v2.position_view(self.rules, self.state, {}, {"gross_gain_bps": 25})["reduce"])
        self.assertFalse(v2.position_view(self.rules, self.state, {}, {"gross_gain_bps": 25})["add"])
        synced = v2.sync_position(self.state, "owned-first-fill", 65, "long", 11)
        self.assertEqual(synced["position"]["first_fill_at"], 65)
        self.assertEqual(synced["position"]["reduction_ticket"], self.state["position"]["reduction_ticket"])
        self.state, _ = v2.advance_bar(self.rules, self.state, observation(180), {"gross_gain_bps": 25})
        self.assertTrue(v2.position_view(self.rules, self.state, {}, {"gross_gain_bps": 25})["reduce"])

    def test_invalid_fraction_and_lone_keys_rejected(self):
        for value in (0, 100, None, True):
            with self.assertRaises(v2.ContractError):
                v2.validate_rules({**self.rules, "reduce_pct": value})
        bad = deepcopy(self.rules)
        del bad["reduce_pct"]
        with self.assertRaises(v2.ContractError):
            v2.validate_rules(bad)


if __name__ == "__main__":
    unittest.main()
