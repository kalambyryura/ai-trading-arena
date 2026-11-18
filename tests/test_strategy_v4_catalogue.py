"""R1 full F5 catalogue: realized_vol_bps, dist_high/low_bps, sma_gap n=1 and the block-B anchors. The evaluator's
vectors and the feature book equal the strategy_v4 reference; validation and the US session clock. Synthetic data only."""
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
import random
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
for _path in (str(ROOT), str(ROOT / "tools"), str(ROOT / "tests")):
    if _path not in sys.path:
        sys.path.insert(0, _path)
import numpy as np
import team_eval_v4
from test_strategy_v4 import minutes, series_of
from test_strategy_v4_book import feed, filled, market
from trading_lab import strategy_v4 as v4, strategy_v4_book as fb

NEW = [{"name": "realized_vol_bps", "tf": "1h", "n": 24}, {"name": "realized_vol_bps", "tf": "5m", "n": 12},
       {"name": "dist_high_bps", "tf": "1d", "n": 1}, {"name": "dist_low_bps", "tf": "1h", "n": 6},
       {"name": "dist_high_bps", "tf": "1m", "n": 30}, {"name": "sma_gap_bps", "tf": "1d", "n": 1},
       {"name": "utc_minute_of_day", "tf": "1m", "n": 1}, {"name": "utc_weekday", "tf": "1d", "n": 1},
       {"name": "us_session_minute", "tf": "1m", "n": 1}, {"name": "return_since_utc_day_open_bps", "tf": "1d", "n": 1},
       {"name": "return_since_utc_week_open_bps", "tf": "1d", "n": 7}, {"name": "return_since_utc_hour_bps", "tf": "1h", "n": 13}]


class CatalogueTests(unittest.TestCase):
    def test_vectors_equal_the_reference(self):
        rows = [r + (5.0,) for r in minutes(9, seed=5)]
        del rows[3000:3002]  # a two-minute hole: bars around it are UNKNOWN in both definitions
        series = series_of(rows)
        series["v"] = np.array([r[5] for r in rows])
        cache = {}
        arrays = {v4.feature_key(s): team_eval_v4.feature_array(v4.validate_feature(s), series, cache) for s in NEW}
        rng = random.Random(2)
        compared = unknown = 0
        for i in sorted(rng.sample(range(len(rows)), 80)) + [2999, 3000, 3001, 7 * 1440]:
            for spec in NEW:
                ref, got = v4.feature_value(spec, rows[:i + 1]), arrays[v4.feature_key(spec)][i]
                if ref is None:
                    unknown += 1
                    self.assertTrue(math.isnan(got), (spec, i))
                else:
                    compared += 1
                    self.assertAlmostEqual(got, ref, delta=1e-7 * max(1, abs(ref)), msg=(spec, i))
        self.assertGreater(compared, 800)
        self.assertGreater(unknown, 20)

    def test_the_book_equals_the_reference_bit_for_bit(self):
        rows, index, mark, _ = market(9, seed=31)
        reference = filled(rows)
        book = fb.FeatureBook(NEW)
        picks = set(random.Random(4).sample(range(len(rows)), 60)) | {len(rows) - 1}
        compared = 0
        for i, row in enumerate(rows):
            feed(book, row, index, mark)
            if i in picks:
                got, upto = book.values(), reference[:reference.index(row) + 1]
                for spec in NEW:
                    ref = v4.feature_value(spec, upto)
                    compared += ref is not None
                    self.assertEqual(got[v4.feature_key(spec)], ref, (spec, i))
        self.assertGreater(compared, 500)

    def test_validation_of_the_new_shapes(self):
        for good in NEW + [{"name": "return_since_utc_hour_bps", "tf": "1h", "n": 0}, {"name": "realized_vol_bps", "tf": "1h", "n": 720}]:
            self.assertEqual(v4.validate_feature(good)["name"], good["name"])
        for bad in ({"name": "utc_weekday", "tf": "1h", "n": 1}, {"name": "return_since_utc_hour_bps", "tf": "1h", "n": 24},
                    {"name": "return_since_utc_week_open_bps", "tf": "1d", "n": 1}, {"name": "realized_vol_bps", "tf": "1h", "n": 1},
                    {"name": "realized_vol_bps", "tf": "1d", "n": 100}, {"name": "dist_low_bps", "tf": "1m", "n": 0}):
            with self.assertRaises(v4.ContractError, msg=bad):
                v4.validate_feature(bad)
        self.assertEqual(v4.CATALOGUE, "v4-closed-bar-2")

    def test_the_us_session_clock_follows_new_york_time(self):
        try:
            from zoneinfo import ZoneInfo
            york = ZoneInfo("America/New_York")
        except Exception:  # noqa: BLE001 - no tz database on this machine: the fixed cases below still run
            york = None
        cases = {"2025-03-07 14:30": 0, "2025-03-10 13:30": 0, "2025-03-10 13:29": -1, "2025-11-03 14:30": 0,
                 "2025-11-03 20:59": 389, "2025-11-03 21:00": -1, "2025-03-08 15:00": -1, "2025-10-31 19:59": 389}
        for text, expected in cases.items():
            close = int(datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp() * 1000)
            self.assertEqual(v4.us_session_minute(close), expected, text)
        dst = {2020: (8, 1), 2021: (14, 7), 2022: (13, 6), 2023: (12, 5), 2024: (10, 3), 2025: (9, 2), 2026: (8, 1), 2027: (14, 7)}
        for year, (march, november) in dst.items():  # published US dates: 2nd Sunday of March, 1st Sunday of November
            start, end = v4.us_summer_time(year)
            self.assertEqual((datetime.fromtimestamp(start / 1000, timezone.utc), datetime.fromtimestamp(end / 1000, timezone.utc)),
                             (datetime(year, 3, march, 7, tzinfo=timezone.utc), datetime(year, 11, november, 6, tzinfo=timezone.utc)), year)
        if york is None:
            return
        moment = datetime(2020, 1, 1, tzinfo=timezone.utc)
        while moment.year < 2028:
            local = moment.astimezone(york)
            minute = local.hour * 60 + local.minute
            expected = minute - 570 if local.weekday() < 5 and 570 <= minute < 960 else -1
            self.assertEqual(v4.us_session_minute(int(moment.timestamp() * 1000)), expected, moment)
            moment += timedelta(minutes=97)



class BothSidesTests(unittest.TestCase):
    LONG = {"feature": {"name": "return_bps", "tf": "1m", "n": 5}, "op": "lte", "value": -12}
    SHORT = {"feature": {"name": "return_bps", "tf": "1m", "n": 5}, "op": "gte", "value": 12}

    def rules(self, **entry):
        base = {"side": "both", "entry": entry or {"long": {"when": self.LONG}, "short": {"when": self.SHORT}}, "order": {"type": "taker"},
                "stop_loss_bps": 20, "take_profit_bps": 40, "max_hold_seconds": 900, "cooldown_seconds": 60, "size_pct": 11}
        return base

    def test_shapes_and_features(self):
        clean = v4.validate_rules(self.rules())
        self.assertEqual(set(v4.required_features(clean)), {"return_bps@1m:5"})
        for bad in ({"when": self.LONG}, {"long": {"when": self.LONG}}, {"long": {"when": self.LONG}, "short": self.SHORT}):
            with self.assertRaises(v4.ContractError):
                v4.validate_rules({**self.rules(), "entry": bad})

    def test_one_true_branch_enters_a_conflict_or_unknown_holds(self):
        clean = v4.validate_rules(self.rules())
        side = lambda value: v4.entry_side(clean, {"return_bps@1m:5": value})
        self.assertEqual(side(-20)[0], "long")
        self.assertEqual(side(20)[0], "short")
        self.assertEqual(side(0)[0], None)
        self.assertEqual(side(None)[0], None)
        both = v4.validate_rules(self.rules(long={"when": self.LONG}, short={"when": {**self.SHORT, "value": -50}}))
        self.assertEqual(v4.entry_side(both, {"return_bps@1m:5": -20})[:3:2], (None, True))  # both branches hold: conflict

    def test_a_recheck_never_turns_into_the_other_side(self):
        from trading_lab import strategy_v4_runtime as v4rt
        recipe = {"strategy_schema_version": 4, "rules": v4rt.engine_rules(v4.validate_rules(self.rules()))}
        snapshot = {"at": 1_800_000_065, "v4_features": {"bar_close_ms": 1_800_000_060_000, "values": {"return_bps@1m:5": 20.0}}}
        bot = {"position_btc": 0, "_candidate_side": "long"}
        self.assertEqual(v4rt.entry(recipe, bot, snapshot)["side"], "short")  # a fresh decision may pick either side
        recheck = v4rt.entry(recipe, bot, snapshot, intent_id="paper-7")
        self.assertEqual((recheck["allowed"], recheck["reason"]), (False, "side_changed_before_fill"))

    def test_engine_and_evaluator_trade_both_sides_alike(self):
        import json
        import tempfile
        import team_eval
        import v4_conformance
        from test_strategy_v4_runtime import payload, write_store
        with tempfile.TemporaryDirectory() as folder:
            store = write_store(Path(folder) / "BTC-USDT-SWAP", 6 * 60)
            recipe = Path(folder) / "both.json"
            item = payload()
            item["rules"].update(side="both", entry={"long": {"when": self.LONG}, "short": {"when": self.SHORT}})
            recipe.write_text(json.dumps(item), encoding="utf-8")
            report = v4_conformance.run(recipe, "2026-01-01T01:00:00Z", 2, store, team_eval.DEFAULT_INSTRUMENT, admission=7200)
        self.assertEqual(report["mismatches"], [], report["mismatches"][:3])
        sides = {row["side"] for row in report["matched"]}
        self.assertGreaterEqual(report["trades"]["engine"], 6)
        self.assertEqual(sides, {"long", "short"})


if __name__ == "__main__":
    unittest.main()
