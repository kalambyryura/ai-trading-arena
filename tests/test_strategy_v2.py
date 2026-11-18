"""Offline mechanics; tests neither import executors nor call an API."""
from copy import deepcopy
import json
from pathlib import Path
import random
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trading_lab import strategy_v2 as v2
from trading_lab.arena_strategies import evaluate as evaluate_v1


def leaf(feature, op="gt", value=0):
    return {"feature": feature, "op": op, "value": value}


def recipe(temporal=True, both=False):
    plan = {"setup": leaf("return_5m_bps", "gte", 20) if temporal else None,
            "confirm": {"all": [leaf("return_1m_bps", "gte", -20), leaf("return_1m_bps", "lte", -2)]}
                       if temporal else leaf("sma_gap_bps"),
            "invalidate": leaf("sma_gap_bps", "lte", 0) if temporal else None,
            "expiry_bars": 2 if temporal else None}
    rules = {"size_pct": 20, "max_entries": 3, "add_after_bps": 20, "stop_loss_bps": 40,
             "take_profit_bps": 90, "cooldown_seconds": 180, "max_spread_bps": 1.5,
             "max_hold_seconds": None, "side": "both" if both else "long",
             "entry": {"long": plan}, "exit": None, "add_when": leaf("gross_gain_bps", "gte", 20),
             "cost_checkpoint": {"after_seconds": 180,
                                 "when": {"feature": "gross_gain_bps", "op": "lt", "ref": "entry_cost_hurdle_bps"}}}
    if both:
        rules["entry"]["short"] = deepcopy(plan)
    return v2.validate_rules(rules)


def observation(seconds, **features):
    data = {"sma_gap_bps": 15, "return_5m_bps": 25, "return_1m_bps": 5,
            "rsi_14": 50, "range_position_20": 70, "volume_ratio_5": 1.2, "spread_bps": 1}
    data.update(features)
    return v2.packet(seconds * 1000, "a" * 64, seconds + 1, seconds + 1, data)


class PredicateTests(unittest.TestCase):
    def test_nested_truth_and_not(self):
        expr = {"all": [{"any": [leaf("return_1m_bps"), leaf("return_5m_bps")]},
                        {"not": leaf("sma_gap_bps", "lt", 0)}]}
        self.assertTrue(v2.evaluate_predicate(expr, observation(60)["features"])[0])

    def test_unknown_overrides_successful_any_and_not(self):
        for expr in ({"any": [leaf("sma_gap_bps"), leaf("volume_ratio_5")]},
                     {"not": leaf("volume_ratio_5")}):
            self.assertIsNone(v2.evaluate_predicate(expr, {"sma_gap_bps": 15, "volume_ratio_5": None})[0])

    def test_flat_legacy_evaluator_parity_2000_cases(self):
        rng = random.Random(821)
        for _ in range(2000):
            rows = [leaf(feature, rng.choice(list(v2.OPS)), rng.randint(-30, 30))
                    for feature in rng.sample(sorted(v2.MARKET_FEATURES), rng.randint(1, 6))]
            mode = rng.choice(["all", "any"])
            values = {feature: rng.choice([None, rng.uniform(-40, 40)]) for feature in v2.MARKET_FEATURES}
            old_result, old_checks = evaluate_v1({"mode": mode, "conditions": rows}, values)
            new_result, new_checks = v2.evaluate_predicate({mode: rows}, values)
            self.assertEqual(old_result, new_result is True)
            self.assertEqual(old_checks, [{key: value for key, value in check.items() if key != "path"} for check in new_checks])

    def test_scope_depth_and_field_rejection(self):
        rule = recipe()
        rule["entry"]["long"]["setup"] = leaf("position_age_seconds")
        with self.assertRaises(v2.ContractError):
            v2.validate_rules(rule)
        expr = leaf("sma_gap_bps")
        for _ in range(6):
            expr = {"not": expr}
        with self.assertRaises(v2.ContractError):
            v2.validate_predicate(expr, v2.MARKET_FEATURES)
        with self.assertRaises(v2.ContractError):
            v2.validate_predicate({"function": "anything"}, v2.MARKET_FEATURES)


class TemporalTests(unittest.TestCase):
    def setUp(self):
        self.rules = recipe()
        self.state = v2.new_state(self.rules, "b" * 64)

    def arm(self):
        self.state, _ = v2.advance_bar(self.rules, self.state, observation(60))

    def test_setup_does_not_confirm_on_same_bar(self):
        self.state, trace = v2.advance_bar(self.rules, self.state, observation(60, return_1m_bps=-5))
        self.assertEqual(self.state["branches"]["long"]["phase"], "ARMED")
        self.assertFalse(v2.entry_view(self.rules, self.state, observation(60, return_1m_bps=-5))["allowed"])

    def test_confirmation_and_fresh_recheck_are_pure(self):
        self.arm()
        obs = observation(120, return_1m_bps=-5, return_5m_bps=8)
        self.state, _ = v2.advance_bar(self.rules, self.state, obs)
        serialized = json.dumps(self.state, sort_keys=True)
        for _ in range(4):
            self.assertTrue(v2.entry_view(self.rules, self.state, obs)["allowed"])
        self.assertEqual(serialized, json.dumps(self.state, sort_keys=True))

    def test_duplicate_poll_with_new_spread_does_not_advance(self):
        self.arm()
        new_state, trace = v2.advance_bar(self.rules, self.state, observation(60, spread_bps=2))
        self.assertEqual(self.state, new_state)
        self.assertEqual(trace, [])

    def test_bar_revision_regression_and_future_reject(self):
        self.arm()
        for obs in (observation(60, return_5m_bps=99), {**observation(60), "bar_ms": 0}):
            with self.assertRaises(v2.ContractError):
                v2.advance_bar(self.rules, self.state, obs)
        with self.assertRaises(v2.ContractError):
            v2.packet(120000, "a" * 64, 60, 60, {})

    def test_last_expiry_bar_allowed_then_reset_without_rearm(self):
        self.arm()
        self.state, _ = v2.advance_bar(self.rules, self.state, observation(120))
        self.state, _ = v2.advance_bar(self.rules, self.state, observation(180, return_1m_bps=-5))
        self.assertTrue(v2.entry_view(self.rules, self.state, observation(180, return_1m_bps=-5))["allowed"])
        self.state, trace = v2.advance_bar(self.rules, self.state, observation(240))
        self.assertEqual(self.state["branches"]["long"]["phase"], "IDLE")
        self.assertIn("confirmation_expired", [row["code"] for row in trace])

    def test_expiry_without_confirmation(self):
        self.arm()
        for second in (120, 180, 240):
            self.state, trace = v2.advance_bar(self.rules, self.state, observation(second))
        self.assertEqual(self.state["branches"]["long"]["phase"], "IDLE")
        self.assertIn("setup_expired", [row["code"] for row in trace])

    def test_unknown_and_invalidation_precede_confirmation(self):
        for changes in ({"sma_gap_bps": 0}, {"sma_gap_bps": None}, {"return_1m_bps": None}):
            self.state = v2.new_state(self.rules, "b" * 64)
            self.arm()
            self.state, _ = v2.advance_bar(self.rules, self.state, observation(120, **{"return_1m_bps": -5, **changes}))
            self.assertEqual(self.state["branches"]["long"]["phase"], "IDLE")

    def test_gap_drops_pending_setup_without_historical_catchup(self):
        self.arm()
        self.state, trace = v2.advance_bar(self.rules, self.state, observation(180, return_1m_bps=-5))
        self.assertFalse(v2.entry_view(self.rules, self.state, observation(180, return_1m_bps=-5))["allowed"])
        self.assertIn("setup_gap_reset", [row["code"] for row in trace])

    def test_restart_json_state_preserves_exact_transition(self):
        self.arm()
        restored = json.loads(json.dumps(self.state))
        obs = observation(120, return_1m_bps=-5)
        self.assertEqual(v2.advance_bar(self.rules, self.state, obs), v2.advance_bar(self.rules, restored, obs))

    def test_long_short_conflict_holds(self):
        rules = recipe(temporal=False, both=True)
        state, _ = v2.advance_bar(rules, v2.new_state(rules, "b" * 64), observation(60))
        self.assertEqual(v2.entry_view(rules, state, observation(60))["reason"], "long_short_conflict")

    def test_reserved_episode_not_reused_and_does_not_start_hold(self):
        self.arm()
        obs = observation(120, return_1m_bps=-5)
        self.state, _ = v2.advance_bar(self.rules, self.state, obs)
        candidate = v2.entry_view(self.rules, self.state, obs)
        self.state = v2.reserve_entry(self.rules, self.state, candidate, "intent-1")
        self.assertIsNone(self.state["position"])
        self.assertFalse(v2.entry_view(self.rules, self.state, obs)["allowed"])
        self.assertTrue(v2.entry_view(self.rules, self.state, obs, intent_id="intent-1")["allowed"])
        self.assertFalse(v2.entry_view(self.rules, self.state, observation(180, return_1m_bps=-5), intent_id="intent-1")["allowed"])
        self.state = v2.finish_unfilled_intent(self.state, "intent-1")
        self.assertFalse(v2.entry_view(self.rules, self.state, obs)["allowed"])


class PositionTests(unittest.TestCase):
    def setUp(self):
        self.rules = recipe()
        self.state, _ = v2.advance_bar(self.rules, v2.new_state(self.rules, "b" * 64), observation(60))
        self.state = v2.sync_position(self.state, "first-actual-fill", 65, "long", 11)

    def test_add_partial_and_restart_preserve_first_fill_age(self):
        restored = json.loads(json.dumps(self.state))
        self.assertEqual(self.state, v2.sync_position(restored, "first-actual-fill", 65, "long", 99))
        with self.assertRaises(v2.ContractError):
            v2.sync_position(self.state, "first-actual-fill", 100, "long", 11)

    def test_checkpoint_first_new_closed_bar_after_holding_age_exactly_once(self):
        values = {"position_age_seconds": 180, "gross_gain_bps": 5}
        for second in (120, 180, 240):
            self.state, _ = v2.advance_bar(self.rules, self.state, observation(second), values)
        self.assertEqual(self.state["position"]["checkpoint"]["status"], "PENDING")
        self.state, trace = v2.advance_bar(self.rules, self.state, observation(300), values)
        self.assertTrue(self.state["position"]["checkpoint"]["close"])
        state, trace2 = v2.advance_bar(self.rules, self.state, observation(300), {"gross_gain_bps": 50})
        self.assertEqual(self.state, state)
        self.assertEqual(trace2, [])
        self.assertEqual(sum(row["code"] == "cost_checkpoint_checked" for row in trace), 1)

    def test_unknown_cost_blocks_add_deferred_until_next_bar(self):
        self.state["position"]["entry_cost_hurdle_bps"] = None
        for second in (120, 180, 240, 300):
            self.state, trace = v2.advance_bar(self.rules, self.state, observation(second), {"gross_gain_bps": 25})
        self.assertEqual(self.state["position"]["checkpoint"]["status"], "PENDING")
        view = v2.position_view(self.rules, self.state, {}, {"position_age_seconds": 250, "gross_gain_bps": 25})
        self.assertFalse(view["exit"])
        self.assertFalse(view["add"])
        self.assertEqual(trace[-1]["code"], "cost_checkpoint_unknown")

    def test_admission_closes_only_entry_checkpoint_still_runs(self):
        for second in (120, 180, 240, 300):
            self.state, _ = v2.advance_bar(self.rules, self.state, observation(second), {"gross_gain_bps": 5}, entry_enabled=False)
        self.assertEqual(v2.entry_view(self.rules, self.state, observation(300), entry_enabled=False)["reason"], "admission_closed")
        self.assertTrue(self.state["position"]["checkpoint"]["close"])

    def test_optional_hold_does_not_impose_match_or_resource_duration(self):
        self.assertFalse(v2.hold_expired(self.rules, 100000000))
        self.rules["max_hold_seconds"] = 900
        self.assertFalse(v2.hold_expired(self.rules, 899))
        self.assertTrue(v2.hold_expired(self.rules, 900))

    def test_cost_helper_signed_funding_and_no_spread_double_charge(self):
        args = {"quantity_btc": .01, "weighted_entry_price": 10000, "close_price": 10002,
                "invested_notional_usdt": 100, "cycle_realized_gross_usdt": 0,
                "cycle_signed_fees_usdt": -.05, "cycle_signed_funding_usdt": -.01, "exit_fee_rate": .0005}
        self.assertAlmostEqual(v2.cycle_net_gain_bps(**args), -9.001)
        args.update(quantity_btc=-.01, close_price=9998, cycle_signed_funding_usdt=.02)
        self.assertAlmostEqual(v2.cycle_net_gain_bps(**args), -5.999)
        args["cycle_signed_funding_usdt"] = None
        self.assertIsNone(v2.cycle_net_gain_bps(**args))

    def test_actual_flat_resets_only_position_epoch_and_blocks_same_bar(self):
        reset = v2.sync_position(self.state, None)
        self.assertIsNone(reset["position"])
        self.assertEqual(reset["last_bar_ms"], self.state["last_bar_ms"])
        self.assertEqual(reset["branches"]["long"]["consumed_bar_ms"], self.state["last_bar_ms"])


if __name__ == "__main__":
    unittest.main()
