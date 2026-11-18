"""Tiny synthetic causal funding/SDK regressions; no network or history downloads."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from tools.okx_tape_model import CandleStore
from trading_lab import arena_engine, strategy_v2
from trading_lab.arena_market import build_snapshot, richer_features
from trading_lab.strategy_contract import author_contract as contract
from trading_lab.strategy_training import read_recorded
from trading_lab.arena_strategies import ArenaError


def store_fixture(rate):
    t = 1_800_000_000
    store = CandleStore.__new__(CandleStore)
    store.rows = {(t + 60 * i) * 1000: [str((t + 60 * i) * 1000),
                  "80000", "80000", "80000", "80000", "100", "1", "80000", "1"]
                  for i in range(-30, 4)}
    store.funding_times = [(t + 60) * 1000, (t + 3600) * 1000]
    store.funding_rates = [str(rate), ".0001"]
    store.instrument = [{"instId": "BTC-USDT-SWAP", "state": "live", "ctValCcy": "BTC",
                         "settleCcy": "USDT", "ctVal": ".01", "ctMult": "1",
                         "lotSz": ".01", "minSz": ".01", "tickSz": ".1"}]
    store.tick, store.decimals, store._paths = .1, 1, {}
    return t, store


class CausalFundingTests(unittest.TestCase):
    def test_future_settlement_rate_cannot_change_past_raw_or_decision_view(self):
        t, plus = store_fixture(.0009)
        _, minus = store_fixture(-.0009)
        self.assertEqual(plus.raw_at(t), minus.raw_at(t))
        a, b = plus.tape(t, t+55), minus.tape(t, t+55)
        self.assertEqual(a, b)
        for snapshot in a:
            self.assertIsNone(snapshot["features"]["funding_rate_bps"])
            self.assertIsNone(snapshot["features"]["minutes_to_funding"])
            self.assertIsNone(snapshot["funding_rate"])
            verdict, _ = strategy_v2.evaluate_predicate(
                {"feature": "funding_rate_bps", "op": "gt", "value": 0}, snapshot["features"])
            self.assertIsNone(verdict)

    def test_realized_rate_applies_once_at_settlement_without_forecast(self):
        t, store = store_fixture(.0009)
        tape = store.tape(t, t + 70)
        bot = {"id": "synthetic", "position_btc": .001, "funding_usdt": 0., "_bank": 100.}
        state = {"_last": tape[0], "_funding_applied": [], "bots": [bot], "events": []}
        for snapshot in tape[1:12]:
            arena_engine.apply_funding(state, snapshot)
            state["_last"] = snapshot
        self.assertEqual(bot["funding_usdt"], 0.)
        self.assertIsNone(state["_last"]["funding_rate"])
        arena_engine.apply_funding(state, tape[12])
        expected = -.001 * tape[11]["mid"] * .0009
        self.assertAlmostEqual(bot["funding_usdt"], expected, places=10)
        self.assertAlmostEqual(bot["_bank"], 100 + expected, places=10)
        arena_engine.apply_funding(state, tape[12])
        self.assertEqual(len(state["_funding_applied"]), 1)
        self.assertAlmostEqual(bot["funding_usdt"], expected, places=10)

    def test_sdk_wick_description_matches_unchanged_close_formula(self):
        for o, h, l, c in ((100., 112., 98., 110.), (110., 112., 98., 100.)):
            rows = [[i*60000, 100., 100., 100., 100., 1.] for i in range(30)]
            rows[-1] = [29*60000, o, h, l, c, 1.]
            actual = richer_features(rows)
            self.assertAlmostEqual(actual["upper_wick_bps"], (h-max(o,c))/c*10000)
            self.assertAlmostEqual(actual["lower_wick_bps"], (min(o,c)-l)/c*10000)
        for name in ("upper_wick_bps", "lower_wick_bps"):
            self.assertIn("/close", contract()["features"][name])

    def test_model_requires_explicit_replay_and_cannot_carry_future_event(self):
        t, store = store_fixture(.0009)
        raw = store.raw_at(t)
        with self.assertRaises(ArenaError):
            build_snapshot(raw, t)
        raw["funding"][0]["settlements"] = [{"fundingTime": str((t+60)*1000), "realizedRate": ".0009"}]
        with self.assertRaises(ArenaError):
            build_snapshot(raw, t, allow_model_funding=True)
        raw["funding"][0]["source"] = "MODEL:settled-rate-of-next-fundingTime"
        with self.assertRaisesRegex(ArenaError, "v1"):
            build_snapshot(raw, t, allow_model_funding=True)

    def test_current_asof_quote_remains_known_with_original_semantics(self):
        t, store = store_fixture(.0009)
        raw = store.raw_at(t)
        raw["funding"] = [{"instId": "BTC-USDT-SWAP", "ts": str(t*1000-1000),
                           "fundingTime": str((t+60)*1000), "fundingRate": ".0002"}]
        snapshot = build_snapshot(raw, t)
        self.assertEqual(snapshot["features"]["funding_rate_bps"], 2)
        self.assertEqual(snapshot["features"]["minutes_to_funding"], 1)
        self.assertNotIn("funding_events", snapshot)

    def test_new_model_recording_roundtrip_preserves_unknown_and_event(self):
        t, store = store_fixture(.0009)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "model.snapshots.jsonl"
            store.write_recording(t, t+120, target)
            self.assertEqual(read_recorded(target), store.tape(t, t+120))

    def test_missing_realized_rate_is_incomplete_never_zero_substitution(self):
        t, store = store_fixture(.0009)
        store.funding_rates[0] = None
        tape = store.tape(t, t+60)
        bot = {"id": "synthetic", "position_btc": -.001, "funding_usdt": 0., "_bank": 100.}
        state = {"_last": tape[-2], "_funding_applied": [], "bots": [bot], "events": []}
        arena_engine.apply_funding(state, tape[-1])
        self.assertTrue(state["incomplete"])
        self.assertEqual(bot["_bank"], 100)
        self.assertEqual(state["events"][-1]["code"], "funding_unknown")


if __name__ == "__main__":
    unittest.main()
