"""Schema v4 (SDK v3 early subset): validation, reference features vs the evaluator's vectorised arrays, predicate
semantics, the Live/PAPER refusal and EOL-independent core hashes (D098). Synthetic data only."""
import json
import math
from pathlib import Path
import random
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import numpy as np
import team_eval_v4
from trading_lab import arena, exchange_lab, strategy_training, strategy_v4 as v4
from trading_lab.arena_strategies import ArenaError, validate_strategy

DAY = 86_400_000
T0 = 1_735_689_600_000  # 2025-01-01T00:00:00Z


def recipe(**rules):
    base = {"side": "long",
            "entry": {"when": {"all": [
                {"feature": {"name": "return_bps", "tf": "1m", "n": 60}, "op": "gte", "ref": {"name": "rms_bar_bps", "tf": "1h", "n": 24}, "mult": 3.0},
                {"feature": {"name": "sma_gap_bps", "tf": "1d", "n": 2}, "op": "gt", "value": 0}]}},
            "order": {"type": "post_only", "offset": {"mult": .25, "of": {"name": "rms_bar_bps", "tf": "1h", "n": 24}}, "timeout_seconds": 7200},
            "stop_loss_bps": 500, "take_profit_bps": 1000, "max_hold_seconds": 86400, "cooldown_seconds": 600, "size_pct": 11}
    base.update(rules)
    return {"strategy_schema_version": 4, "name": "fixture", "author": {"name": "platform", "kind": "model"}, "description": "d",
            "original_text": "t", "confirmed": True, "instrument": "ETH-USDT-SWAP", "rules": base}


def minutes(days, seed=7, start=T0):
    rng = random.Random(seed)
    price, rows = 2000.0, []
    for k in range(days * 1440):
        o = price
        price *= math.exp(rng.gauss(0, .0012))
        rows.append((start + k * 60_000, o, max(o, price) * (1 + abs(rng.gauss(0, .0004))), min(o, price) * (1 - abs(rng.gauss(0, .0004))), price))
    return rows


def series_of(rows):
    arr = np.array([r[1:] for r in rows])
    return {"ts": np.array([r[0] for r in rows], dtype=np.int64), "o": arr[:, 0], "h": arr[:, 1], "l": arr[:, 2], "c": arr[:, 3],
            "filled_minutes": [], "gaps": []}


class ValidationTests(unittest.TestCase):
    def test_valid_recipe_hashes_stably_and_lists_its_features(self):
        clean = v4.validate_payload(recipe())
        self.assertEqual(clean, v4.validate_payload(json.loads(json.dumps(recipe()))))
        self.assertEqual(set(v4.required_features(clean["rules"])), {"return_bps@1m:60", "rms_bar_bps@1h:24", "sma_gap_bps@1d:2"})

    def test_bad_shapes_are_refused(self):
        bad = [dict(side="both"), dict(size_pct=40), dict(order={"type": "post_only", "offset": {"bps": 5}, "timeout_seconds": 90}),
               dict(order={"type": "limit"}),
               dict(entry={"when": {"feature": {"name": "rsi", "tf": "1h", "n": 14}, "op": "gt", "value": 1}}),
               dict(entry={"when": {"feature": {"name": "sma_gap_bps", "tf": "1d", "n": 120}, "op": "gt", "value": 0}}),
               dict(entry={"when": {"feature": {"name": "return_bps", "tf": "1m", "n": 5}, "op": "gt",
                                    "ref": {"name": "return_bps", "tf": "1m", "n": 1}, "mult": 0}})]
        for change in bad:
            with self.assertRaises(v4.ContractError, msg=str(change)):
                v4.validate_payload(recipe(**change))
        with self.assertRaises(v4.ContractError):
            v4.validate_payload({**recipe(), "instrument": "DOGE-USDT-SWAP"})

    def test_live_and_paper_engines_refuse_v4(self):
        with self.assertRaises(ArenaError):
            validate_strategy(recipe())

    def test_eval_only_recipes_cannot_enter_the_workshop_or_the_arena(self):
        from trading_lab.workshop import Workshop
        clean = v4.validate_payload(recipe())
        self.assertEqual((clean["eval_only"], clean["execution_model_id"]), (True, v4.EXECUTION_MODEL_ID))

        def no_feed(*_args, **_kwargs):
            raise AssertionError("no market feed in this test")
        with tempfile.TemporaryDirectory() as folder:
            board = arena.Arena(folder, feed_factory=no_feed, background=False)
            try:
                shop = Workshop(folder, board)
                for payload in (recipe(), clean):
                    with self.assertRaises(ArenaError):
                        shop.add_version(0, payload)
                    with self.assertRaises(ArenaError):
                        board.add_strategy(board.data["generation"], payload)
                self.assertEqual(shop.data["versions"], [])
            finally:
                board.close()


class WorkshopTests(unittest.TestCase):
    def test_workshop_validates_and_lints_v4_without_running_it(self):
        import io
        from contextlib import redirect_stdout
        import strategy_workshop
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "r.json"
            path.write_text(json.dumps(recipe()), encoding="utf-8")
            for command in ("validate", "lint"):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    self.assertEqual(strategy_workshop.main([command, str(path)]), 0)
                result = json.loads(buffer.getvalue())
                self.assertEqual((result["valid"], result["strategy_schema_version"]), (True, 4))
            path.write_text(json.dumps(recipe(size_pct=99)), encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(strategy_workshop.main(["validate", str(path)]), 1)


class FeatureTests(unittest.TestCase):
    def test_vectorised_arrays_equal_the_reference_definition(self):
        rows = minutes(4)
        series = series_of(rows)
        specs = [{"name": "return_bps", "tf": "1m", "n": 60}, {"name": "return_bps", "tf": "15m", "n": 3},
                 {"name": "rms_bar_bps", "tf": "1h", "n": 24}, {"name": "rms_bar_bps", "tf": "5m", "n": 12},
                 {"name": "sma_gap_bps", "tf": "1d", "n": 2}, {"name": "sma_gap_bps", "tf": "4h", "n": 6}]
        cache = {}
        arrays = {v4.feature_key(s): team_eval_v4.feature_array(s, series, cache) for s in specs}
        rng = random.Random(3)
        picks = sorted(rng.sample(range(len(rows)), 60)) + [59, 60, 1439, 1440, 2879, 2880, 2881, len(rows) - 1]
        compared = 0
        for i in picks:
            for spec in specs:
                ref = v4.feature_value(spec, rows[:i + 1])
                got = arrays[v4.feature_key(spec)][i]
                if ref is None:
                    self.assertTrue(math.isnan(got), (spec, i))
                else:
                    compared += 1
                    self.assertAlmostEqual(got, ref, delta=1e-7 * max(1, abs(ref)), msg=(spec, i))
        self.assertGreater(compared, 200)

    def test_bars_with_a_missing_minute_make_features_unknown(self):
        rows = minutes(3)
        del rows[2000]
        spec = {"name": "rms_bar_bps", "tf": "1h", "n": 3}
        self.assertIsNone(v4.feature_value(spec, rows[:2050]))
        self.assertIsNotNone(v4.feature_value(spec, rows[:2300]))

    def test_a_missing_latest_bar_is_unknown_in_both_definitions(self):
        rows = [r for k, r in enumerate(minutes(1)) if not 100 <= k <= 104]
        series = series_of(rows)
        for spec in ({"name": "return_bps", "tf": "5m", "n": 1}, {"name": "rms_bar_bps", "tf": "5m", "n": 2}):
            at = next(k for k, r in enumerate(rows) if r[0] == T0 + 105 * 60_000)
            self.assertIsNone(v4.feature_value(spec, rows[:at + 1]), spec)
            self.assertTrue(np.isnan(team_eval_v4.feature_array(spec, series, {})[at]), spec)

    def test_vectorised_predicate_keeps_unknown_semantics(self):
        clean = v4.validate_payload(recipe())["rules"]
        n = 6
        arrays = {"return_bps@1m:60": np.array([90, 90, np.nan, 10, 90, 90.]), "rms_bar_bps@1h:24": np.array([20, 20, 20, 20, np.nan, 20.]),
                  "sma_gap_bps@1d:2": np.array([5, -5, 5, 5, 5, np.nan])}
        true, known = team_eval_v4.predicate_mask(clean["entry"]["when"], arrays)
        for i in range(n):
            verdict, _ = v4.evaluate(clean["entry"]["when"], {k: (None if math.isnan(a[i]) else float(a[i])) for k, a in arrays.items()})
            self.assertEqual(bool(true[i]), verdict is True, i)
            self.assertEqual(bool(known[i]), verdict is not None, i)


class CoreHashTests(unittest.TestCase):
    def test_core_hashes_ignore_line_endings_but_not_content(self):
        for module, function in ((strategy_training, "source_hash"), (exchange_lab, "source_hash"), (arena, "arena_source_hash")):
            with tempfile.TemporaryDirectory() as folder:
                lf, crlf = Path(folder) / "lf", Path(folder) / "crlf"
                for target in (lf, crlf):
                    target.mkdir()
                for name in module.SOURCE_FILES:
                    data = (ROOT / "trading_lab" / name).read_bytes().replace(b"\r\n", b"\n")
                    (lf / name).write_bytes(data)
                    (crlf / name).write_bytes(data.replace(b"\n", b"\r\n"))
                hasher = getattr(module, function)
                self.assertEqual(hasher(lf), hasher(crlf), module.__name__)
                self.assertEqual(hasher(lf), hasher(), module.__name__)
                (crlf / module.SOURCE_FILES[0]).write_bytes((crlf / module.SOURCE_FILES[0]).read_bytes() + b"#")
                self.assertNotEqual(hasher(lf), hasher(crlf), module.__name__)


if __name__ == "__main__":
    unittest.main()
