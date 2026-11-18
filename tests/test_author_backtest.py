"""Screening backtest: features match the engine, exits and costs follow the stated model, unsupported recipes are refused."""
import contextlib
import csv
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest

try:
    import numpy as np
except ModuleNotFoundError:  # the tool is optional
    np = None

from trading_lab.arena_strategies import catalogue_defaults
from trading_lab.strategy_contract import v2_template

if np is not None:
    from tools import author_backtest as ab

T0 = int(datetime(2026, 10, 3, 13, 0, tzinfo=timezone.utc).timestamp() * 1000)  # a Saturday, 13:00 UTC
HEAD = ["ts_ms", "utc", "open", "high", "low", "close", "vol_contracts", "vol_btc", "vol_quote_usdt", "confirm"]


def synthetic(n=2880, seed=5, start=T0):
    rng = np.random.default_rng(seed)
    close = 80000 * np.exp(np.cumsum(rng.normal(0, 6e-4, n)))
    open_ = np.concatenate([[80000.0], close[:-1]])
    wick = np.abs(rng.normal(0, 4e-4, (2, n))) * close
    return ab.Series((start + np.arange(n) * 60000).astype(np.int64), open_, np.maximum(open_, close) + wick[0],
                     np.minimum(open_, close) - wick[1], close, rng.lognormal(5, .6, n))


def flat(n=40, price=100.0):
    arr = np.full(n, price)
    return ab.Series((T0 + np.arange(n) * 60000).astype(np.int64), arr.copy(), arr.copy(), arr.copy(), arr.copy(), np.full(n, 10.0))


def bar(S, k, o, h, l, c):
    S.o[k], S.h[k], S.l[k], S.c[k] = o, h, l, c


def plan(n, long_at=(), short_at=(), sl=50.0, tp=100.0, hold=600.0, cooldown=0.0, long_exit=None, size=10.0):
    def mask(at):
        m = np.zeros(n, dtype=bool)
        m[list(at)] = True
        return m
    return ab.Plan("t", 1, mask(long_at) if long_at else None, mask(short_at) if short_at else None, long_exit, None,
                   sl, tp, hold, cooldown, size, [])


def write_csv(path, S, confirm="1"):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEAD)
        for k in range(S.n):
            writer.writerow([int(S.ts[k]), "", S.o[k], S.h[k], S.l[k], S.c[k], S.v[k], 0, 0, confirm])


def write_recipe(path, record):
    keys = ("name", "author", "description", "original_text", "rules", "confirmed", "strategy_schema_version")
    Path(path).write_text(json.dumps({k: record[k] for k in keys if k in record}, ensure_ascii=False), encoding="utf-8")


def v1_recipe(**rules):
    record = json.loads(json.dumps(catalogue_defaults()[0]))
    record["rules"].update(rules)
    return record


def v2_recipe(**rules):
    record = v2_template()
    record["rules"].update(exit=None, max_hold_seconds=3600)
    record["rules"].update(rules)
    return record


@unittest.skipUnless(np is not None, "numpy is not installed")
class FeatureTests(unittest.TestCase):
    def test_features_match_the_engines_own_functions(self):
        S = synthetic(900)
        F, valid = ab.features(S)
        worst = ab.check_features(S, F, valid, samples=250)
        self.assertEqual(set(worst), set(ab.MARKET_FEATURES) - {"spread_bps"} | set(ab.CONTEXT_FEATURES))
        self.assertLess(max(worst.values()), 1e-6, worst)

    def test_context_features_follow_the_closing_time_of_the_completed_bar(self):
        S = flat(60)
        F, valid = ab.features(S)
        close = datetime(2026, 10, 3, 14, 0, tzinfo=timezone.utc)  # bar 59 opens 13:59 and closes 14:00
        self.assertEqual((F["utc_hour"][59], F["utc_weekday"][59]), (14, close.weekday()))
        self.assertEqual(F["utc_hour"][40], 13)
        self.assertTrue(np.isnan(F["utc_hour"][10]), "no clean 30-bar history yet: unknown, not zero")

    def test_gaps_make_features_unknown_until_thirty_clean_bars_exist(self):
        S = synthetic(200)
        S.ts[100:] += 60000  # one missing minute
        F, valid = ab.features(S)
        self.assertFalse(valid[100:129].any())
        self.assertTrue(valid[129])
        self.assertTrue(np.isnan(F["rsi_14"][100]))

    def test_unknown_never_passes_a_condition(self):
        F = {"rsi_14": np.array([np.nan, 20.0, 80.0, np.nan]), "return_5m_bps": np.array([-50.0, np.nan, -50.0, np.nan])}
        group = {"mode": "all", "conditions": [{"feature": "rsi_14", "op": "lt", "value": 30}, {"feature": "return_5m_bps", "op": "lt", "value": 0}]}
        self.assertEqual(ab.group_mask(F, group).tolist(), [False, False, False, False])
        either = {"mode": "any", "conditions": group["conditions"]}
        self.assertEqual(ab.group_mask(F, either).tolist(), [False, False, True, False], "UNKNOWN in any branch blocks the root permission")
        self.assertEqual(ab.group_mask(F, {"mode": "all", "conditions": []}).tolist(), [False, False, False, False])


@unittest.skipUnless(np is not None, "numpy is not installed")
class SimulatorTests(unittest.TestCase):
    NO_COST = None

    def setUp(self):
        self.free = ab.Cost(fee_bps=0.0, slip_bps=0.0)

    def run_one(self, S, p, cost=None, **kwargs):
        return ab.simulate(S, p, cost or ab.Cost(fee_bps=5.0, slip_bps=0.0), 20.0, **kwargs)

    def test_take_profit_fills_at_the_target_and_pays_two_fees(self):
        S = flat()
        bar(S, 8, 100.0, 101.5, 100.0, 101.0)
        t = self.run_one(S, plan(S.n, long_at=[5]))
        self.assertEqual(list(t["reason"]), ["take_profit"])
        self.assertAlmostEqual(float(t["exit_px"][0]), 101.0)
        self.assertAlmostEqual(float(t["gross_bps"][0]), 100.0, places=6)
        self.assertAlmostEqual(float(t["net_bps"][0]), 90.0, places=6)
        self.assertAlmostEqual(float(t["hold_s"][0]), 130.0)

    def test_a_gap_through_the_stop_fills_at_the_open_not_at_the_stop(self):
        S = flat()
        bar(S, 7, 98.0, 98.5, 97.5, 98.0)
        t = self.run_one(S, plan(S.n, long_at=[5]))
        self.assertEqual(list(t["reason"]), ["stop_loss"])
        self.assertAlmostEqual(float(t["exit_px"][0]), 98.0)
        self.assertAlmostEqual(float(t["net_bps"][0]), -210.0, places=6)

    def test_stop_and_target_in_one_bar_follow_the_path_order(self):
        up = flat(); bar(up, 7, 100.0, 101.5, 99.0, 101.0)      # close >= open: low first
        down = flat(); bar(down, 7, 100.0, 101.5, 99.0, 99.2)   # close < open: high first
        self.assertEqual(list(self.run_one(up, plan(up.n, long_at=[5]))["reason"]), ["stop_loss"])
        self.assertEqual(list(self.run_one(down, plan(down.n, long_at=[5]))["reason"]), ["take_profit"])
        shorts = flat(); bar(shorts, 7, 100.0, 100.8, 98.8, 99.0)  # close < open: high first, which hurts a short
        self.assertEqual(list(self.run_one(shorts, plan(shorts.n, short_at=[5]))["reason"]), ["stop_loss"])

    def test_a_short_gains_when_the_price_falls(self):
        S = flat()
        bar(S, 8, 100.0, 100.0, 98.9, 99.0)
        t = self.run_one(S, plan(S.n, short_at=[5]))
        self.assertEqual((list(t["reason"]), int(t["side"][0])), (["take_profit"], -1))
        self.assertAlmostEqual(float(t["gross_bps"][0]), 100.0, places=6)

    def test_max_hold_closes_a_flat_position_and_the_costs_are_all_that_is_left(self):
        S = flat()
        t = self.run_one(S, plan(S.n, long_at=[5], sl=500.0, tp=500.0, hold=120.0))
        self.assertEqual(list(t["reason"]), ["max_hold"])
        self.assertAlmostEqual(float(t["hold_s"][0]), 120.0)
        self.assertAlmostEqual(float(t["net_bps"][0]), -10.0, places=6)

    def test_slippage_is_adverse_on_both_sides(self):
        S = flat()
        t = self.run_one(S, plan(S.n, long_at=[5], sl=500.0, tp=500.0, hold=120.0), ab.Cost(fee_bps=5.0, slip_bps=1.0))
        self.assertAlmostEqual(float(t["entry_px"][0]), 100.01)
        self.assertAlmostEqual(float(t["exit_px"][0]), 99.99)
        self.assertAlmostEqual(float(t["net_bps"][0]), -2.0 - 10.0, places=2)

    def test_formal_exit_closes_one_bar_after_the_signal(self):
        S = flat()
        exit_mask = np.zeros(S.n, dtype=bool); exit_mask[7] = True
        t = self.run_one(S, plan(S.n, long_at=[5], sl=500.0, tp=500.0, hold=3600.0, long_exit=exit_mask))
        self.assertEqual(list(t["reason"]), ["formal_exit"])
        self.assertEqual(int(t["j"][0]), 8)

    def test_cooldown_blocks_the_next_entry_until_it_has_passed(self):
        S = flat(60)
        quick = self.run_one(S, plan(S.n, long_at=[5, 8], sl=500.0, tp=500.0, hold=120.0, cooldown=0.0))
        slow = self.run_one(S, plan(S.n, long_at=[5, 8], sl=500.0, tp=500.0, hold=120.0, cooldown=600.0))
        self.assertEqual((len(quick["i"]), len(slow["i"])), (2, 1))

    def test_conflicting_directions_on_one_bar_mean_no_trade(self):
        S = flat()
        self.assertEqual(len(self.run_one(S, plan(S.n, long_at=[5], short_at=[5]))["i"]), 0)

    def test_every_window_starts_flat_and_decisions_after_its_end_are_dropped(self):
        S = flat(60)
        p = plan(S.n, long_at=[5, 9, 30], sl=500.0, tp=500.0, hold=120.0)
        window = [(int(S.ts[0]), int(S.ts[10]))]       # bar 9's decision falls after the window closes
        t = self.run_one(S, p, windows=window)
        self.assertEqual(list(t["i"]), [5])
        two = self.run_one(S, p, windows=[(int(S.ts[0]), int(S.ts[10])), (int(S.ts[25]), int(S.ts[40]))])
        self.assertEqual((list(two["i"]), list(two["window"])), ([5, 30], [0, 1]))

    def test_usdt_result_uses_whole_lots_of_the_participants_budget(self):
        S = flat()
        bar(S, 8, 100.0, 101.5, 100.0, 101.0)
        t = self.run_one(S, plan(S.n, long_at=[5]))
        usdt, lots = ab.usdt_net(t, size_pct=30.0, budget=100.0)
        self.assertEqual(float(lots[0]), np.floor(100 * 0.30 / (100.0 * 1e-4)))
        self.assertAlmostEqual(float(usdt[0]), 90.0 / 1e4 * lots[0] * 1e-4 * 100.0, places=9)
        dear = {"entry_px": np.array([70000.0]), "net_bps": np.array([10.0])}   # 11 % of 100 USDT buys 1.57 lots: one, never two
        usdt, lots = ab.usdt_net(dear, size_pct=11.0)
        self.assertEqual(float(lots[0]), 1.0)
        self.assertAlmostEqual(float(usdt[0]), 10.0 / 1e4 * 1e-4 * 70000.0, places=9)


@unittest.skipUnless(np is not None, "numpy is not installed")
class StatisticsTests(unittest.TestCase):
    def test_few_trades_give_no_interval_instead_of_a_made_up_one(self):
        out = ab.day_bootstrap_mean(np.array([1.0, -2.0, 3.0]), np.array([1, 1, 2]))
        self.assertTrue(all(np.isnan(x) for x in out))

    def test_the_bootstrap_is_deterministic_and_brackets_the_mean(self):
        values = np.array([(-1.0) ** i * 2.0 - 1.0 for i in range(60)])
        days = np.arange(60) // 3
        first, second = ab.day_bootstrap_mean(values, days), ab.day_bootstrap_mean(values, days)
        self.assertEqual(first, second)
        mean, low, high, _ = first
        self.assertAlmostEqual(mean, float(values.mean()))
        self.assertLess(low, mean)
        self.assertGreater(high, mean)

    def test_an_empty_period_reports_zero_trades_not_a_mean(self):
        empty = {k: np.array([]) for k in ("i", "j", "side", "entry_px", "exit_px", "reason", "gross_bps", "net_bps", "hold_s", "t_entry_ms", "window")}
        out = ab.summarize(empty, (T0, T0 + 86_400_000), plan(10))
        self.assertEqual((out["trades"], out["mean_net_bps"], out["net_usdt"]), (0, None, 0.0))


@unittest.skipUnless(np is not None, "numpy is not installed")
class RecipeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.S = synthetic(400)
        self.F, _ = ab.features(self.S)

    def tearDown(self):
        self.temp.cleanup()

    def plan_of(self, record):
        path = self.dir / "r.json"
        write_recipe(path, record)
        return ab.compile_plan(ab.load_recipe(path), self.F)

    def test_a_v1_recipe_compiles_with_its_exit_parameters(self):
        p = self.plan_of(catalogue_defaults()[0])
        self.assertEqual((p.schema, p.sl_bps, p.tp_bps, p.hold_s, p.cooldown_s, p.size_pct), (1, 50.0, 100.0, 300.0, 60.0, 20.0))
        self.assertIsNotNone(p.long_entry)
        self.assertIsNone(p.short_entry)
        self.assertIsNotNone(p.long_exit)
        short = self.plan_of(catalogue_defaults()[2])
        self.assertIsNone(short.long_entry)
        self.assertIsNotNone(short.short_entry)

    def test_a_direct_v2_recipe_with_both_sides_compiles(self):
        p = self.plan_of(v2_recipe())
        self.assertEqual((p.schema, p.hold_s), (2, 3600.0))
        self.assertIsNotNone(p.long_entry)
        self.assertIsNotNone(p.short_entry)
        self.assertIsNone(p.long_exit)

    def test_a_null_hold_is_refused_without_a_24_hour_cap(self):
        with self.assertRaisesRegex(ab.Unsupported, "null"):
            self.plan_of(v2_recipe(max_hold_seconds=None))

    def test_the_model_spread_of_one_tick_never_blocks_an_entry(self):
        tight = self.plan_of(v1_recipe(max_spread_bps=0.1))   # the smallest cap the arena accepts
        wide = self.plan_of(v1_recipe(max_spread_bps=20))
        self.assertGreater(int(tight.long_entry.sum()), 0)
        self.assertEqual(tight.long_entry.tolist(), wide.long_entry.tolist())
        self.assertTrue(any("1 тік" in note for note in tight.notes), "the model says so out loud")

    def test_what_the_model_cannot_reproduce_is_refused_with_the_reason(self):
        position_exit = v2_template()  # exit on position_age_seconds
        with self.assertRaisesRegex(ab.Unsupported, "позиційна або квотна"):
            self.plan_of(position_exit)
        funding = v2_recipe()
        funding["rules"]["entry"]["long"]["confirm"] = {"all": [{"feature": "return_5m_bps", "op": "gt", "value": 20}, {"feature": "funding_rate_bps", "op": "lt", "value": 1}]}
        with self.assertRaisesRegex(ab.Unsupported, "funding"):
            self.plan_of(funding)
        temporal = v2_recipe()
        temporal["rules"]["entry"]["long"] = {"setup": {"feature": "return_5m_bps", "op": "lt", "value": -30},
                                              "confirm": {"feature": "return_1m_bps", "op": "gt", "value": 0}, "invalidate": None, "expiry_bars": 3}
        with self.assertRaisesRegex(ab.Unsupported, "setup"):
            self.plan_of(temporal)
        adds = v2_recipe(max_entries=2)
        with self.assertRaises(ab.Unsupported):
            self.plan_of(adds)

    def test_an_invalid_recipe_is_rejected_by_the_arenas_own_validator(self):
        bad = v1_recipe(stop_loss_bps=-5)
        path = self.dir / "bad.json"
        write_recipe(path, bad)
        with self.assertRaisesRegex(ValueError, "не проходить перевірку арени"):
            ab.load_recipe(path)


@unittest.skipUnless(np is not None, "numpy is not installed")
class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.data = self.dir / "bars.csv"
        write_csv(self.data, synthetic(2880))
        self.windows = self.dir / "windows.json"
        # Reserve a real data tail for the authored 900s hold, instead of
        # silently dropping late trades whose exits were outside the CSV.
        self.windows.write_text(json.dumps([[T0, T0+2850*60000]]), encoding="utf-8")
        self.recipe = self.dir / "v1.json"
        write_recipe(self.recipe, v1_recipe(entry={"mode": "all", "conditions": [{"feature": "return_5m_bps", "op": "lte", "value": -15}]},
                                            exit={"mode": "any", "conditions": []}, stop_loss_bps=40, take_profit_bps=60, max_hold_seconds=900,
                                            cooldown_seconds=60, size_pct=20, side="long"))

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *argv):
        out = io.StringIO()
        if argv and argv[0] == "run":
            argv = (*argv, "--windows", str(self.windows))
        with contextlib.redirect_stdout(out):
            code = ab.main(list(argv))
        return code, out.getvalue()

    def test_run_writes_a_summary_and_a_trade_list_and_is_repeatable(self):
        first, second = self.dir / "a", self.dir / "b"
        code, text = self.run_cli("run", "--data", str(self.data), "--recipe", str(self.recipe), "--out", str(first))
        self.assertEqual(code, 0)
        self.assertIn("не виробничий рушій", text)
        self.assertEqual(self.run_cli("run", "--data", str(self.data), "--recipe", str(self.recipe), "--out", str(second))[0], 0)
        self.assertEqual((first / "summary.json").read_text(encoding="utf-8"), (second / "summary.json").read_text(encoding="utf-8"))
        summary = json.loads((first / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["cost"]["round_trip_bps"], 11.0)
        self.assertEqual(summary["data"]["bars"], 2880)
        with open(first / "trades.csv", encoding="utf-8") as handle:
            trades = list(csv.DictReader(handle))
        self.assertEqual(len(trades), summary["periods"][0]["trades"])
        self.assertGreater(len(trades), 5)
        self.assertEqual(set(trades[0]), {"entry_utc", "side", "entry_px", "exit_px", "reason", "gross_bps", "net_bps", "hold_s"})

    def test_regression_guard_for_the_simulator_on_a_frozen_series(self):
        out = self.dir / "golden"
        self.run_cli("run", "--data", str(self.data), "--recipe", str(self.recipe), "--out", str(out))
        period = json.loads((out / "summary.json").read_text(encoding="utf-8"))["periods"][0]
        self.assertEqual(period["trades"], GOLDEN["trades"])
        self.assertAlmostEqual(period["sum_net_bps"], GOLDEN["sum_net_bps"], places=3)
        self.assertEqual(period["reasons"], GOLDEN["reasons"])

    def test_stress_costs_and_splits_change_only_what_they_say(self):
        base = json.loads(self._summary(out="base"))
        stress = json.loads(self._summary("--cost", "stress", out="stress"))
        self.assertEqual(stress["cost"]["round_trip_bps"], 14.0)
        # more slippage moves the fills, so the trade list can differ a little; what must hold is that every trade got dearer
        gap = base["periods"][0]["mean_net_bps"] - stress["periods"][0]["mean_net_bps"]
        self.assertGreater(gap, 2.0)
        self.assertLess(gap, 4.5)
        split = json.loads(self._summary("--split", "2026-10-04", out="split"))
        self.assertEqual(len(split["periods"]), 2)
        self.assertEqual(sum(p["trades"] for p in split["periods"]), base["periods"][0]["trades"])

    def _summary(self, *extra, out):
        self.run_cli("run", "--data", str(self.data), "--recipe", str(self.recipe), *extra, "--out", str(self.dir / out))
        return (self.dir / out / "summary.json").read_text(encoding="utf-8")

    def test_a_recipe_the_model_cannot_reproduce_is_refused_not_approximated(self):
        refused = self.dir / "v2.json"
        write_recipe(refused, v2_template())
        code, text = self.run_cli("run", "--data", str(self.data), "--recipe", str(refused), "--out", str(self.dir / "never"))
        self.assertEqual(code, 2)
        self.assertIn("Рецепт відхилено", text)
        report = json.loads((self.dir / "never" / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "UNSUPPORTED")
        self.assertIsNone(report["periods"])
        self.assertTrue((self.dir / "never" / "attempts.jsonl").is_file())

    def test_check_features_reports_agreement_with_the_engine(self):
        code, text = self.run_cli("check-features", "--data", str(self.data), "--samples", "120")
        self.assertEqual(code, 0)
        self.assertIn("ЗБІГ З РУШІЄМ", text)

    def test_the_csv_reader_wants_the_public_history_format_and_skips_unconfirmed_candles(self):
        bad = self.dir / "bad.csv"
        bad.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            ab.load_bars([bad])
        half = self.dir / "half.csv"
        write_csv(half, synthetic(50), confirm="0")
        with self.assertRaises(ValueError):
            ab.load_bars([half])
        later = self.dir / "later.csv"
        S = synthetic(50)
        S.c[:] = 1.0
        write_csv(later, S)
        merged = ab.load_bars([self.data, later])
        self.assertEqual(float(merged.c[0]), 1.0, "a later file wins on a duplicate minute")


# v2 declared OHLC path: post-fill crossings and actual crossing times. Same
# seed5/2880 bars, now explicit 2850-bar admission + exit tail. The old defective
# result and comparable before/after are retained in SCREENING_GOLDEN_DELTA.json.
# This is a numerical model guard, not market evidence.
GOLDEN = {"trades": 99, "sum_net_bps": -856.1830209072531, "reasons": {"max_hold": 79, "stop_loss": 17, "take_profit": 3}}

if __name__ == "__main__":
    unittest.main()
