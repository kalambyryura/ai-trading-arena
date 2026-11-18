"""Context features (clock of the completed bar, quote funding facts): values, scopes, gate payload identity."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import unittest

from trading_lab import strategy_v2 as v2
from trading_lab.arena_jev import GATE_FEATURES, make_payload
from trading_lab.arena_market import context_features
from trading_lab.arena_strategies import validate_strategy
from trading_lab.strategy_contract import author_contract, v2_template
from trading_lab.strategy_training import SYNTHETIC, run_trial, synthetic_tape

CONTEXT = ("utc_hour", "utc_weekday", "funding_rate_bps", "minutes_to_funding")


def legacy_features():
    return {"sma_gap_bps": -20.6, "return_5m_bps": -41.37, "return_1m_bps": 10.43, "rsi_14": 18.04,
            "range_position_20": 19.7, "volume_ratio_5": 1.2, "atr_14_bps": 8.4, "body_bps": 10.4,
            "upper_wick_bps": 0.0, "lower_wick_bps": 0.53, "bollinger_z_20": -2.39, "ema_gap_bps": -16.3,
            "volume_ratio_1": 0.55, "spread_bps": 0.0116}


def v1_candidate(features):
    rules = {"size_pct": 30, "max_entries": 1, "add_after_bps": 30, "stop_loss_bps": 100, "take_profit_bps": 150,
             "max_hold_seconds": 10800, "cooldown_seconds": 300, "max_spread_bps": 2, "side": "long",
             "entry": {"mode": "all", "conditions": [{"feature": "return_5m_bps", "op": "lt", "value": -40},
                                                     {"feature": "return_1m_bps", "op": "gt", "value": 0}]},
             "exit": {"mode": "any", "conditions": []}}
    checks = [{"feature": "return_5m_bps", "op": "lt", "expected": -40, "actual": features["return_5m_bps"], "passed": True},
              {"feature": "return_1m_bps", "op": "gt", "expected": 0, "actual": features["return_1m_bps"], "passed": True},
              {"feature": "spread_bps", "op": "lte", "expected": 2, "actual": features["spread_bps"], "passed": True},
              {"feature": "total_entries", "op": "lt", "expected": 20, "actual": 0, "passed": True},
              {"feature": "position_entries", "op": "lt", "expected": 1, "actual": 0, "passed": True}]
    return {"id": "probe", "recipe_sha256": "0" * 64, "features": features, "rules": rules, "checks": checks,
            "side": "long", "kind": "entry", "position_btc": 0.0, "local_checks_passed": True}


def wire(payload):
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


class ContextFeatureTests(unittest.TestCase):
    def test_clock_comes_from_completed_bar_and_funding_from_quote(self):
        close = datetime(2026, 10, 3, 14, 0, tzinfo=timezone.utc)
        server = close.timestamp() + 12
        out = context_features(int(close.timestamp() * 1000), server, .00012, server + 3600)
        self.assertEqual((out["utc_hour"], out["utc_weekday"]), (14, close.weekday()))
        self.assertAlmostEqual(out["funding_rate_bps"], 1.2)
        self.assertAlmostEqual(out["minutes_to_funding"], 60.0)
        late = context_features(int(close.timestamp() * 1000), server, .0001, server - 30)
        self.assertEqual(late["minutes_to_funding"], 0.0)

    def test_unknown_funding_stays_unknown_and_is_never_zero(self):
        for rate, when in ((None, None), (float("nan"), float("inf")), (True, False)):
            out = context_features(1790985600000, 1790985612, rate, when)
            self.assertIsNone(out["funding_rate_bps"])
            self.assertIsNone(out["minutes_to_funding"])

    def test_synthetic_tape_carries_context_features(self):
        tape = synthetic_tape("range", 120)
        for snapshot in tape[:: max(1, len(tape) // 10)]:
            features = snapshot["features"]
            self.assertTrue(set(CONTEXT) <= set(features))
            self.assertEqual(features["utc_hour"], datetime.fromtimestamp(snapshot["bar_close_ms"] / 1000, timezone.utc).hour)
            self.assertAlmostEqual(features["funding_rate_bps"], 1.0)
            self.assertAlmostEqual(features["minutes_to_funding"], (snapshot["funding_at"] - snapshot["server_time"]) / 60)

    def test_gate_payload_is_byte_identical_with_context_features(self):
        legacy = legacy_features()
        extended = {**legacy, "utc_hour": 14, "utc_weekday": 5, "funding_rate_bps": 1.2, "minutes_to_funding": 60.0}
        for mode in ("PAPER", "OKX_DEMO", "OKX_LIVE"):
            self.assertEqual(wire(make_payload([v1_candidate(legacy)], mode)),
                             wire(make_payload([v1_candidate(extended)], mode)))
        payload = make_payload([v1_candidate(extended)], "OKX_LIVE")
        self.assertEqual(set(payload["state"]["features"]), GATE_FEATURES)
        self.assertEqual(list(payload["state"]["features"]), list(legacy))  # key order preserved

    def test_live_battle_snapshots_gain_context_but_gate_bytes_stay_pinned(self):
        """8 recorded gate decisions (battle 8d53b2a8...): snapshots now carry the context features, while
        tests.test_jev_gate_probe.GOLDEN keeps their payload SHA256 unchanged, i.e. the gate filtering works."""
        import hashlib
        from pathlib import Path
        from tools import jev_gate_probe as probe
        from trading_lab.arena_market import build_snapshot
        from test_jev_gate_probe import GOLDEN
        fixture = probe.load_fixture(Path(__file__).parent / "fixtures" / "jev_gate_live5h.json")
        for decision in fixture["decisions"]:
            snapshot = build_snapshot(decision["raw"], decision["snapshot_at"])
            features = snapshot["features"]
            self.assertTrue(set(CONTEXT) <= set(features))
            self.assertEqual(features["utc_hour"], datetime.fromtimestamp(snapshot["bar_close_ms"] / 1000, timezone.utc).hour)
            self.assertIsNotNone(features["funding_rate_bps"])
            self.assertGreaterEqual(features["minutes_to_funding"], 0)
        bodies = {c["case_id"].rsplit(":r", 1)[0]: hashlib.sha256(c["body"]).hexdigest() for c in probe.replay_cases(fixture)}
        self.assertEqual(bodies, GOLDEN)

    def test_scopes_clock_everywhere_funding_only_direct(self):
        base = v2_template()["rules"]
        direct = deepcopy(base)
        direct["entry"]["long"]["confirm"] = {"all": [{"feature": "funding_rate_bps", "op": "gt", "value": 0},
                                                      {"feature": "utc_hour", "op": "gte", "value": 13}]}
        v2.validate_rules(direct)
        temporal_clock = deepcopy(base)
        temporal_clock["entry"]["long"].update(setup={"feature": "utc_hour", "op": "lt", "value": 6},
                                               confirm={"feature": "return_1m_bps", "op": "gt", "value": 0},
                                               invalidate=None, expiry_bars=3)
        v2.validate_rules(temporal_clock)
        for slot in ("setup", "confirm"):
            temporal_funding = deepcopy(temporal_clock)
            temporal_funding["entry"]["long"][slot] = {"feature": "minutes_to_funding", "op": "lt", "value": 30}
            with self.assertRaises(v2.ContractError):
                v2.validate_rules(temporal_funding)
        exit_rule = deepcopy(base)
        exit_rule["exit"] = {"feature": "minutes_to_funding", "op": "lt", "value": 5}
        v2.validate_rules(exit_rule)

    def test_author_contract_documents_every_market_feature(self):
        contract = author_contract()
        self.assertEqual(set(contract["features"]), v2.MARKET_FEATURES)
        for name in CONTEXT:
            self.assertIn(name, contract["prompt"])

    def test_engine_reads_clock_feature(self):
        def recipe(hour_low, hour_high):
            payload = v2_template()
            payload.update(confirmed=True, name="Clock probe")
            payload["author"].update(kind="example")
            payload["rules"]["side"] = "long"
            payload["rules"]["entry"] = {"long": {"setup": None, "confirm": {"all": [
                {"feature": "utc_hour", "op": "gte", "value": hour_low}, {"feature": "utc_hour", "op": "lte", "value": hour_high}]},
                "invalidate": None, "expiry_bars": None}}
            return validate_strategy(payload)
        hour = datetime.fromtimestamp(synthetic_tape("range", 120)[0]["bar_close_ms"] / 1000, timezone.utc).hour
        inside = run_trial(recipe(hour, hour), SYNTHETIC)
        outside = run_trial(recipe((hour + 12) % 24, (hour + 12) % 24), SYNTHETIC)
        self.assertGreater(inside["summary"]["trades"], 0)
        self.assertEqual(outside["summary"]["trades"], 0)


if __name__ == "__main__":
    unittest.main()
