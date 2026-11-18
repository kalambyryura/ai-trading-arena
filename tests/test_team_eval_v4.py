"""Candle engine for v4 recipes on the 5-second model path: exact-engine conformance, the D106 post-only state
machine, exit order, causality of features and decisions, data holes and the CLI. Synthetic data, no network."""
from contextlib import contextmanager, redirect_stdout
import csv
import hashlib
import inspect
import io
import json
import math
from pathlib import Path
import random
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
import numpy as np
import arena_eval
import okx_tape_model
import research_limits
import team_data
import team_eval
import team_eval_v4 as engine
from trading_lab import strategy_v4 as v4

T0 = 1_735_689_600_000  # 2025-01-01T00:00:00Z
ETH = {"instId": "ETH-USDT-SWAP", "tickSz": "0.01", "lotSz": "0.01", "ctVal": "0.1", "minSz": "0.01"}
BTC = json.loads((ROOT / "tools" / "okx_instrument_btc_usdt_swap.json").read_text(encoding="utf-8"))[0]
JUMP = {"feature": {"name": "return_bps", "tf": "1m", "n": 1}, "op": "gte", "value": 50}
ENGINE_PINS = {"v4-path5s-2": "31458a1a1320ab0b79bc5b649f30a8e9cf4359acd19ffb379658c1a099bc3539"}  # execution model id -> sha256 of the engine's rule code


def strategy(order=None, instrument="ETH-USDT-SWAP", **rules):
    base = {"side": "long", "entry": {"when": JUMP}, "order": order or {"type": "post_only", "offset": {"bps": 50}, "timeout_seconds": 600},
            "stop_loss_bps": 500, "take_profit_bps": 1000, "max_hold_seconds": 3600, "cooldown_seconds": 0, "size_pct": 11}
    base.update(rules)
    return v4.validate_payload({"strategy_schema_version": 4, "name": "t", "author": {"name": "p"}, "description": "d",
                                "original_text": "t", "confirmed": True, "instrument": instrument, "rules": base})


def flat(count=200, price=100.0):
    """Flat candles with a +100 bps jump closing minute 10; minutes after it sit at 101 unless a test edits them."""
    o, h, l, c = (np.full(count, price) for _ in range(4))
    c[10] = h[10] = price * 1.01
    o[11:], h[11:], l[11:], c[11:] = price * 1.01, price * 1.01, price * 1.01, price * 1.01
    return {"ts": T0 + np.arange(count, dtype=np.int64) * 60_000, "o": o, "h": h, "l": l, "c": c, "filled_minutes": [], "gaps": []}


def run(series, recipe, spec=ETH, funding=(), **kw):
    return engine.simulate(recipe, series, spec, list(funding), int(series["ts"][0]), int(series["ts"][-1]) + 60_000, **kw)


S11 = T0 // 1000 + 11 * 60  # second 0 of minute 11: bar 10 closes here; decision at +5 s, order in the book at +10 s


def team_data_funding(path):
    with path.open(encoding="utf-8", newline="") as handle:
        return [(int(r["funding_time_ms"]), r["realized_rate"] or None) for r in csv.DictReader(handle)]


class PathTests(unittest.TestCase):
    def test_path_equals_the_tape_model(self):
        rng = random.Random(4)
        rows = []
        for _ in range(500):
            a, b = rng.uniform(90, 110), rng.uniform(90, 110)
            rows.append((a, max(a, b) + rng.uniform(0, 2), min(a, b) - rng.uniform(0, 2), b))
        rows.append((100.0, 100.0, 100.0, 100.0))
        arr = np.array(rows)
        grid = engine.path_prices(arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3], np.arange(0, 60, 5))
        for i, row in enumerate(rows):
            price = okx_tape_model.minute_path(*row)[0]
            for j, second in enumerate(range(0, 60, 5)):
                self.assertAlmostEqual(grid[i, j], price(second), places=9)


class ExactConformanceTests(unittest.TestCase):
    """mode='exact' must reproduce arena_eval (the production engine) trade by trade on taker recipes."""

    def test_taker_trades_equal_the_exact_engine(self):
        from test_arena_eval import write_data
        with tempfile.TemporaryDirectory() as folder:
            data = write_data(Path(folder) / "d", 720)
            v1 = {"name": "dump", "author": {"name": "t", "kind": "model", "model": "m", "submitted_at": "2025-01-01T00:00:00Z", "prompt": "p",
                                             "attempts": 1, "edited_by": ""}, "description": "d", "original_text": "t",
                  "confirmed": True, "rules": {"size_pct": 11, "max_entries": 1, "add_after_bps": 30, "stop_loss_bps": 40,
                                               "take_profit_bps": 60, "max_hold_seconds": 300, "cooldown_seconds": 60, "max_spread_bps": 2,
                                               "side": "long", "entry": {"mode": "all", "conditions": [{"feature": "return_5m_bps", "op": "lte", "value": -3}]},
                                               "exit": {"mode": "any", "conditions": []}}}
            twin = strategy(order={"type": "taker"}, instrument="BTC-USDT-SWAP", stop_loss_bps=40, take_profit_bps=60, max_hold_seconds=300,
                            cooldown_seconds=60, entry={"when": {"feature": {"name": "return_bps", "tf": "1m", "n": 5}, "op": "lte", "value": -3}})
            candles = next(data.glob("okx_*_1m_*.csv"))
            funding = next(data.glob("okx_*_funding_*.csv"))
            rows = list(csv.reader(candles.open(encoding="utf-8")))[1:]
            arr = np.array([[float(x) for x in r[2:6]] for r in rows])
            series = {"ts": np.array([int(r[0]) for r in rows], dtype=np.int64), "o": arr[:, 0], "h": arr[:, 1], "l": arr[:, 2], "c": arr[:, 3]}
            windows = [{"id": f"w{k}", "start_s": int(series["ts"][0]) // 1000 + 3600 * (k + 1), "admission_seconds": 3600} for k in range(8)]
            ours, _ = engine.simulate_exact_windows(twin, series, BTC, team_data_funding(funding), windows)
            raw = json.dumps(v1).encode()
            reference = []
            for w in windows:
                row = arena_eval.one_window((raw, {"id": w["id"], "start_utc": engine.utc(w["start_s"] * 1000), "admission_seconds": 3600},
                                             720, str(candles), str(funding), str(ROOT / "tools" / "okx_instrument_btc_usdt_swap.json")))
                reference += [(w["id"], c["entry_at"], c["exit_reason"], c["net_usdt"]) for c in row["cycles"]]
            self.assertGreater(len(reference), 5)
            mine = [(t["window_id"], t["entry_at"], t["exit_reason"], round(t["net_usdt"], 6)) for t in ours]
            self.assertEqual(mine, reference)


class MakerTests(unittest.TestCase):
    """D106 post-only state machine: book only after placement, max(1 bps, 2 ticks), no crossing, SL at once, TP later."""
    LIMIT = math.floor(101 * .995 / .01 + 1e-9) * .01  # 100.49

    def test_a_low_before_placement_does_not_fill(self):
        early = flat()
        # up minute O->L->H->C: the low (100.40) comes after 60*0.6/5.0 = 7.2 s, before the order exists at 10 s
        early["o"][11], early["l"][11], early["h"][11], early["c"][11] = 101.0, 100.4, 104.8, 104.8
        early["o"][12:], early["h"][12:], early["l"][12:], early["c"][12:] = 104.8, 104.8, 104.8, 104.8
        trades, counters, orders = run(early, strategy())
        self.assertEqual((trades, counters["maker_unfilled"]), ([], 1))
        self.assertEqual(orders[0]["placed_at"], engine.utc((S11 + 10) * 1000))

    def test_fill_needs_two_ticks_through_and_is_booked_at_the_limit_after_placement(self):
        for short_of in (.01, .015):  # 1 bps of 100.49 is 0.01005: only the 2-tick rule refuses 0.015
            touch = flat()
            touch["l"][12] = self.LIMIT - short_of
            self.assertEqual(run(touch, strategy())[1]["maker_unfilled"], 1, short_of)
        through = flat()
        through["l"][12] = self.LIMIT - .03
        trades, _, _ = run(through, strategy())
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual((t["entry_order"], t["executions"][0]["price_usdt"]), ("post_only", self.LIMIT))
        self.assertEqual(t["entry_at"][:16], engine.utc(T0 + 12 * 60_000)[:16])
        self.assertAlmostEqual(t["adverse_selection_usdt"], t["first_notional_usdt"] * .0002, places=8)

    def test_an_order_that_would_cross_is_rejected(self):
        crossing = flat()
        crossing["o"][11] = crossing["l"][11] = crossing["c"][11] = crossing["h"][11] = 100.40  # ask at placement below the limit
        trades, counters, orders = run(crossing, strategy())
        self.assertEqual((trades, counters["maker_rejected_crossing"], orders[0]["outcome"]), ([], 1, "rejected_crossing"))

    def test_stop_is_live_from_the_fill_and_the_target_only_from_the_next_minute(self):
        stop = flat()
        stop["o"][12], stop["h"][12], stop["l"][12], stop["c"][12] = 101.0, 101.0, 94.0, 94.0
        t = run(stop, strategy())[0][0]
        self.assertEqual((t["exit_reason"], t["exit_at"][:16]), ("stop_loss", engine.utc(T0 + 12 * 60_000)[:16]))
        target = flat()
        # up minute: the low 100.44 falls exactly on the 5-s sample (0.56 of a 6.72 path), the high passes the target at once
        target["o"][12], target["l"][12], target["h"][12], target["c"][12] = 101.0, 100.44, 106.6, 106.6
        target["o"][13:], target["h"][13:], target["l"][13:], target["c"][13:] = 106.6, 106.6, 106.6, 106.6
        t = run(target, strategy(take_profit_bps=300))[0][0]
        self.assertEqual(t["entry_at"], engine.utc(T0 + 12 * 60_000 + 5_000))
        self.assertEqual((t["exit_reason"], t["exit_at"][:16]), ("take_profit", engine.utc(T0 + 13 * 60_000)[:16]))

    def test_a_minute_where_stop_and_target_are_both_reachable_exits_at_the_stop(self):
        both = flat()
        both["l"][12] = self.LIMIT - .05
        both["o"][13], both["h"][13], both["l"][13], both["c"][13] = 101.0, 112.0, 94.0, 94.0  # red minute: the path visits the high first
        trades, counters, _ = run(both, strategy())
        t = trades[0]
        self.assertEqual((t["exit_reason"], t["ambiguous_minute"], counters["ambiguous_minute_stops"]), ("stop_loss", True, 1))
        self.assertAlmostEqual(t["executions"][1]["price_usdt"], self.LIMIT * .95, places=6)

    def test_on_btc_one_bps_is_wider_than_two_ticks(self):
        btc = {k: (v * 1000 if k in ("o", "h", "l", "c") else v) for k, v in flat().items()}  # price 100 000, tick 0.1
        limit = math.floor(101_000 * .995 / .1 + 1e-9) * .1
        btc["l"][12] = limit - .5  # five ticks, but less than 1 bps (10.05)
        self.assertEqual(run(btc, strategy(instrument="BTC-USDT-SWAP"), spec=BTC)[1]["maker_unfilled"], 1)
        btc["l"][12] = limit - 10.5
        self.assertEqual(len(run(btc, strategy(instrument="BTC-USDT-SWAP"), spec=BTC)[0]), 1)

    def test_short_post_only_mirrors_the_long_rules(self):
        drop = {"feature": {"name": "return_bps", "tf": "1m", "n": 1}, "op": "lte", "value": -50}
        down = flat()
        down["c"][10] = down["l"][10] = 99.0
        down["o"][11:], down["h"][11:], down["l"][11:], down["c"][11:] = 99.0, 99.0, 99.0, 99.0
        limit = math.ceil(99.0 * 1.005 / .01 - 1e-9) * .01  # 99.50
        recipe = strategy(side="short", entry={"when": drop})
        crossing = {k: (v.copy() if hasattr(v, "copy") else v) for k, v in down.items()}
        crossing["o"][11] = crossing["l"][11] = crossing["c"][11] = crossing["h"][11] = 99.6  # bid at placement above L
        self.assertEqual(run(crossing, recipe)[1]["maker_rejected_crossing"], 1)
        down["h"][12] = limit + .015
        self.assertEqual(run(down, recipe)[1]["maker_unfilled"], 1)
        down["h"][12] = limit + .05
        down["o"][13], down["l"][13], down["h"][13], down["c"][13] = 99.0, 85.0, 110.0, 110.0  # green: the low (target) first, then the stop
        trades, counters, _ = run(down, recipe)
        t = trades[0]
        self.assertEqual((t["side"], t["exit_reason"], t["ambiguous_minute"]), ("short", "stop_loss", True))
        self.assertAlmostEqual(t["executions"][1]["price_usdt"], limit * 1.05, places=6)

    def test_a_hole_over_the_exit_is_a_data_gap_exit_not_the_end_of_the_period(self):
        series = flat(400)
        keep = np.r_[0:60, 130:400]  # the 60-minute hold of an entry at minute 11 ends inside the hole
        holed = {k: (v[keep] if isinstance(v, np.ndarray) else v) for k, v in series.items()}
        trades, counters, _ = run(holed, strategy(order={"type": "taker"}))
        self.assertEqual((trades[0]["exit_reason"], counters["data_gap_exits"], counters["open_at_end"]), ("data_gap_exit", 1, 0))

    def test_an_unfilled_order_keeps_the_slot_until_its_timeout(self):
        series = flat(300)
        series["c"][15], series["h"][15] = 101 * 1.006, 101 * 1.006  # a second signal while the first order rests
        _, counters, orders = run(series, strategy())
        self.assertEqual((len(orders), counters["maker_unfilled"]), (1, 1))


class TakerAndTimeTests(unittest.TestCase):
    def test_taker_fills_at_the_next_quote_after_the_visible_bar_and_time_exit_follows_the_engine(self):
        t = run(flat(), strategy(order={"type": "taker"}, max_hold_seconds=3600))[0][0]
        self.assertEqual(t["entry_at"], engine.utc((S11 + 10) * 1000), "bar close + 5 s decision, next quote fill")
        self.assertAlmostEqual(t["executions"][0]["price_usdt"], (101.0 + .01) * 1.0002, places=8)
        self.assertEqual((t["exit_reason"], t["exit_at"]), ("max_hold", engine.utc((S11 + 10 + 3600 + 5) * 1000)))

    def test_funding_uses_the_previous_mid_and_cooldown_blocks_reentry(self):
        series = flat(400)
        series["c"][150], series["h"][150] = 101.0 * 1.006, 101.0 * 1.006
        settle = T0 + 40 * 60_000
        trades, _, _ = run(series, strategy(order={"type": "taker"}, cooldown_seconds=7200), funding=[(settle, "0.0001")])
        self.assertEqual(len(trades), 1)
        t = trades[0]
        mid = (101.0 + 101.01) / 2
        self.assertAlmostEqual(t["funding_usdt"], -t["executions"][0]["quantity"] * mid * .0001, places=8)  # ledger keeps 8 decimals
        self.assertEqual(len(run(series, strategy(order={"type": "taker"}), funding=[(settle, "0.0001")])[0]), 2)


T_DST = 1_742_947_200_000  # 2025-03-26T00:00Z: six days hold the EU DST switch (03-30 01:00 UTC) and a week start (03-31)
HOUR, DAY = 3_600_000, 86_400_000
ARRAYS = ("o", "h", "l", "c", "v", "index_c", "mark_c")


def rich_series(days=6, seed=5, start=T_DST):
    """Swap candles with volume (some minutes without trades), index and mark closes (a few missing) and an 8 h
    funding schedule with one missing settlement, one 4 h step and one unknown rate."""
    rng = random.Random(seed)
    price, rows = 2000.0, []
    for _ in range(days * 1440):
        o = price
        price *= math.exp(rng.gauss(0, .0015))
        rows.append((o, max(o, price) * 1.0003, min(o, price) * .9997, price))
    arr = np.array(rows)
    count = len(rows)
    series = {"ts": start + np.arange(count, dtype=np.int64) * 60_000, "o": arr[:, 0], "h": arr[:, 1], "l": arr[:, 2], "c": arr[:, 3],
              "v": np.array([0.0 if rng.random() < .002 else rng.uniform(1, 50) for _ in range(count)])}
    index = arr[:, 3] * np.array([1 - rng.gauss(0, 3e-4) for _ in range(count)])
    index[[rng.randrange(count) for _ in range(8)]] = np.nan
    series["index_c"] = index
    series["mark_c"] = index * np.array([1 + rng.gauss(0, 1e-4) for _ in range(count)])
    settle = list(range(start - 3 * DAY, start + days * DAY, 8 * HOUR))
    if days >= 5:
        settle.remove(start + 2 * DAY + 8 * HOUR)  # a missing settlement: a gap
        settle.append(start + 4 * DAY + 4 * HOUR)  # a 4 h step
    funding = [(t, f"{rng.gauss(1e-4, 6e-5):.8f}") for t in sorted(settle)]
    funding[5] = (funding[5][0], None)
    series["funding"] = funding
    return series


def cut(series, t):
    """Everything known at the close of minute t: candles up to t and settlements at or before its open."""
    out = {k: series[k][:t + 1].copy() for k in ("ts", *ARRAYS) if k in series}
    out["funding"] = [row for row in series.get("funding", []) if row[0] <= series["ts"][t]]
    return out


def shift(series, ms):
    out = {k: (v + ms if k == "ts" else v.copy()) for k, v in series.items() if k != "funding"}
    out["funding"] = [(t + ms, r) for t, r in series.get("funding", [])]
    return out


def reference(spec, series, t):
    """strategy_v4.feature_value at the close of minute t from a tail of minutes long enough for the spec."""
    width = v4.TIMEFRAMES[spec["tf"]] // 60_000
    lo = max(0, t + 1 - ((spec["n"] + 2) * width + 2))
    rows = range(lo, t + 1)
    minutes = [(int(series["ts"][k]), *(float(series[a][k]) for a in ("o", "h", "l", "c", "v"))) for k in rows]
    side = {name: {int(series["ts"][k]): float(series[name + "_c"][k]) for k in rows if math.isfinite(series[name + "_c"][k])}
            for name in ("index", "mark")}
    value = v4.feature_value(spec, minutes, {**side, "funding": series["funding"]})
    return math.nan if value is None else value


def same(a, b):
    return (math.isnan(a) and math.isnan(b)) or a == b


class CausalityTests(unittest.TestCase):
    """D106 p.3: POISON, TRUNCATION, WARM-UP INVARIANCE, SHIFT, AVAILABILITY, DST and golden values."""
    SPECS = [{"name": "return_bps", "tf": "1m", "n": 60}, {"name": "return_bps", "tf": "1h", "n": 3},
             {"name": "rms_bar_bps", "tf": "1h", "n": 6}, {"name": "rms_bar_bps", "tf": "15m", "n": 4},
             {"name": "sma_gap_bps", "tf": "1d", "n": 2}, {"name": "sma_gap_bps", "tf": "4h", "n": 3},
             {"name": "funding_settled_bps", "tf": "1m", "n": 3}, {"name": "funding_settled_bps", "tf": "1h", "n": 1},
             {"name": "premium_bps", "tf": "1m", "n": 60}, {"name": "premium_bps", "tf": "1h", "n": 2},
             {"name": "premium_change_bps", "tf": "15m", "n": 4}, {"name": "basis_mark_bps", "tf": "5m", "n": 6}]

    def features(self, series):
        cache = {}
        return {v4.feature_key(s): engine.feature_array(s, series, cache) for s in self.SPECS}

    def moments(self, series, count=500, seed=9):
        """Random closes plus day, week, settlement, hour and DST boundaries (the minute ending there and the next)."""
        ts = series["ts"]
        marks = {DAY: 0, 8 * HOUR: 0, HOUR: 0}
        edges = [k for k in range(1440, len(ts) - 1) if any((int(ts[k]) + 60_000 * d) % m == 0 for m in marks for d in (0, 1))]
        special = [k for k in range(len(ts)) if int(ts[k]) in (T_DST + 4 * DAY + HOUR - 60_000, T_DST + 4 * DAY + HOUR,  # DST
                                                                  T_DST + 5 * DAY - 60_000, T_DST + 5 * DAY)]  # Monday
        rng = random.Random(seed)
        return sorted(set(rng.sample(range(1440, len(ts) - 2), count) + rng.sample(edges, 60) + special))

    def test_poison_and_truncation_after_t_do_not_change_values_at_t(self):
        base = rich_series()
        full = self.features(base)
        rng = random.Random(11)
        moments = self.moments(base)
        self.assertGreaterEqual(len(moments), 500)
        for t in moments:
            poisoned = {k: (v.copy() if k != "funding" else v) for k, v in base.items()}
            for key in ARRAYS:
                poisoned[key][t + 1:] *= rng.uniform(.5, 2)
            poisoned["v"][t + 1::7] = 0.0
            poisoned["index_c"][t + 1::11] = np.nan
            poisoned["funding"] = [(at, r) if at <= base["ts"][t] else (at, None if rng.random() < .3 else "0.0075")
                                   for at, r in base["funding"]]
            after, truncated = self.features(poisoned), self.features(cut(base, t))
            for key in full:
                self.assertTrue(same(full[key][t], after[key][t]) and same(full[key][t], truncated[key][t]), (key, t))

    def test_warmup_start_and_shifts_do_not_change_values(self):
        long = rich_series()
        short = {k: (v[1440:].copy() if k != "funding" else v) for k, v in long.items()}  # starts a day later, longer than every L
        a, b = self.features(long), self.features(short)
        for t in (len(long["ts"]) - 1, len(long["ts"]) - 700, 6000):  # the short series has more than every L before t
            for key in a:
                self.assertTrue(same(a[key][t], b[key][t - 1440]), (key, t))
        whole_day = self.features(shift(long, DAY))
        for key in a:
            self.assertTrue(np.array_equal(a[key], whole_day[key], equal_nan=True), key)
        minute = self.features(shift(long, 60_000))  # bars of 1m features keep their content: values are identical
        for spec in self.SPECS:
            if spec["tf"] == "1m":
                key = v4.feature_key(spec)
                self.assertTrue(np.array_equal(a[key], minute[key], equal_nan=True), key)

    def test_a_bar_is_visible_only_five_seconds_after_its_close(self):
        quotes = engine.Quotes(rich_series(days=1), .01)
        close = int(quotes.close_s[100])
        self.assertEqual(quotes.visible(close + 4, 5), 99)
        self.assertEqual(quotes.visible(close + 5, 5), 100)
        self.assertEqual(quotes.visible(close, 0), 100)

    def test_vectorised_arrays_equal_the_reference_definition(self):
        series = rich_series()
        arrays = self.features(series)
        rng = random.Random(3)
        moments = sorted(set(rng.sample(range(1440, len(series["ts"])), 60) + self.moments(series, count=0)[:40]))
        seen = {key: 0 for key in arrays}
        for t in moments:
            for spec in self.SPECS:
                key = v4.feature_key(spec)
                expected, got = reference(spec, series, t), float(arrays[key][t])
                self.assertEqual(math.isnan(expected), math.isnan(got), (key, t))
                if not math.isnan(expected):
                    seen[key] += 1
                    self.assertAlmostEqual(got, expected, places=8, msg=(key, t))
        self.assertTrue(all(seen.values()), seen)

    def test_utc_days_ignore_dst(self):
        series = rich_series()
        t = int(np.searchsorted(series["ts"], T_DST + 5 * DAY))  # first minute of Monday 2025-03-31
        value = engine.feature_array({"name": "sma_gap_bps", "tf": "1d", "n": 2}, series, {})[t]
        closes = [series["c"][int(np.searchsorted(series["ts"], T_DST + d * DAY - 60_000))] for d in (4, 5)]  # 03-29, 03-30
        self.assertAlmostEqual(value, (series["c"][t] / (sum(closes) / 2) - 1) * 10000, places=9)

    def test_golden_values(self):
        k = np.arange(180)
        series = {"ts": T0 + k * 60_000, "o": 100.0 + k, "h": 100.5 + k, "l": 99.5 + k, "c": 100.25 + k}
        i = 179  # close of the third hour
        ret = engine.feature_array({"name": "return_bps", "tf": "1m", "n": 60}, series, {})[i]
        self.assertAlmostEqual(ret, (279.25 / 219.25 - 1) * 10000, places=9)
        rms = engine.feature_array({"name": "rms_bar_bps", "tf": "1h", "n": 2}, series, {})[i]
        hours = [(219.25 / 160.0 - 1) * 1e4, (279.25 / 220.0 - 1) * 1e4]  # close/open of hours 2 and 3
        self.assertAlmostEqual(rms, math.sqrt(sum(x * x for x in hours) / 2), places=9)
        gap = engine.feature_array({"name": "sma_gap_bps", "tf": "1h", "n": 3}, series, {})[i]
        self.assertAlmostEqual(gap, (279.25 / ((159.25 + 219.25 + 279.25) / 3) - 1) * 10000, places=9)


class PositioningTests(unittest.TestCase):
    """Positioning (D): golden values, availability of settlements and lagged index/mark bars, UNKNOWN rules."""

    def plain(self, minutes=600):
        k = np.arange(minutes)
        return {"ts": T0 + k * 60_000, "o": 100.0 + 0 * k, "h": 100.0 + 0 * k, "l": 100.0 + 0 * k, "c": 100.1 + .01 * k,
                "v": np.full(minutes, 5.0), "index_c": np.full(minutes, 100.0), "mark_c": np.full(minutes, 100.02)}

    def value(self, series, name, tf, n, at):
        return float(engine.feature_array({"name": name, "tf": tf, "n": n}, series, {})[at])

    def test_golden_premium_change_and_basis(self):
        s = self.plain()
        # minute m: premium (100.1 + 0.01 m) / 100 - 1 = 10 + m bps; decision after minute 130 sees minutes <= 129
        self.assertAlmostEqual(self.value(s, "premium_bps", "1m", 1, 130), 10 + 129, places=8)
        self.assertAlmostEqual(self.value(s, "premium_bps", "1m", 60, 130), 10 + (70 + 129) / 2, places=8)
        # hour bars 0 (minutes 0..59) and 1 (60..119) are visible from the close of minute 120
        self.assertAlmostEqual(self.value(s, "premium_bps", "1h", 1, 120), 10 + (60 + 119) / 2, places=8)
        self.assertTrue(math.isnan(self.value(s, "premium_bps", "1h", 2, 119)))
        self.assertAlmostEqual(self.value(s, "premium_bps", "1h", 2, 120), 10 + 59.5, places=8)
        self.assertAlmostEqual(self.value(s, "premium_change_bps", "15m", 2, 120), 30.0, places=8)
        self.assertAlmostEqual(self.value(s, "basis_mark_bps", "5m", 3, 120), 2.0, places=8)

    def test_golden_funding_with_schedule_rules(self):
        s = self.plain(3 * 1440)
        rows = [(T0 - 16 * HOUR, "0.0005"), (T0 - 8 * HOUR, "0.0004"), (T0, "0.0001"), (T0 + 8 * HOUR, "0.0002"),
                (T0 + 16 * HOUR, "0.0003"), (T0 + 20 * HOUR, "0.0001"), (T0 + 24 * HOUR, "0.0001")]
        s["funding"] = rows
        at = lambda ms: int((ms - T0) // 60_000)  # the minute that opens at ms
        self.assertAlmostEqual(self.value(s, "funding_settled_bps", "1m", 2, at(T0 + 16 * HOUR)), 2.5, places=9)
        self.assertAlmostEqual(self.value(s, "funding_settled_bps", "1m", 2, at(T0 + 16 * HOUR) - 1), 1.5, places=9)
        self.assertTrue(math.isnan(self.value(s, "funding_settled_bps", "1m", 1, at(T0 + 20 * HOUR))))  # a new 4 h step, unconfirmed
        self.assertAlmostEqual(self.value(s, "funding_settled_bps", "1m", 1, at(T0 + 24 * HOUR)), 2.0, places=9)  # confirmed: x2
        # tf 1h: the 16:00 settlement is known from the close of the 16:00 hour bar, not from 16:01
        self.assertAlmostEqual(self.value(s, "funding_settled_bps", "1h", 1, at(T0 + 16 * HOUR)), 2.0, places=9)
        self.assertAlmostEqual(self.value(s, "funding_settled_bps", "1h", 1, at(T0 + 17 * HOUR) - 1), 3.0, places=9)
        # on the 4 h schedule the 04:00 settlement is overdue from 04:01: UNKNOWN, as are windows without two earlier steps
        self.assertTrue(math.isnan(self.value(s, "funding_settled_bps", "1m", 1, at(T0 + 28 * HOUR))))
        self.assertFalse(math.isnan(self.value(s, "funding_settled_bps", "1m", 1, at(T0 + 28 * HOUR) - 1)))
        self.assertTrue(math.isnan(self.value(s, "funding_settled_bps", "1m", 4, at(T0 + 8 * HOUR))))
        self.assertAlmostEqual(self.value(s, "funding_settled_bps", "1m", 2, at(T0 + 8 * HOUR)), (1 + 2) / 2, places=9)
        s["funding"] = rows + [(T0 + 32 * HOUR, "0.0001")]  # the 28:00 settlement of the 4 h schedule is missing
        self.assertTrue(math.isnan(self.value(s, "funding_settled_bps", "1m", 1, at(T0 + 32 * HOUR))))
        s["funding"] = rows[:3] + [(T0 + 8 * HOUR, None)] + rows[4:]
        self.assertTrue(math.isnan(self.value(s, "funding_settled_bps", "1m", 2, at(T0 + 16 * HOUR))))
        self.assertAlmostEqual(self.value(s, "funding_settled_bps", "1m", 1, at(T0 + 16 * HOUR)), 3.0, places=9)
        s["funding"] = rows[:3] + rows[4:]  # 08:00 missing: the 16:00 step is 16 h, a gap
        self.assertTrue(math.isnan(self.value(s, "funding_settled_bps", "1m", 1, at(T0 + 16 * HOUR))))

    def test_no_rule_mixes_a_new_swap_bar_with_an_older_lagged_bar(self):
        s = self.plain()
        for name, tf, n in (("premium_bps", "1h", 1), ("premium_change_bps", "15m", 1), ("basis_mark_bps", "5m", 2)):
            values = engine.feature_array({"name": name, "tf": tf, "n": n}, s, {})
            width = v4.TIMEFRAMES[tf] // 60_000
            for edge in range(4 * width - 1, 500, width):  # past the warm-up: the minute whose close opens a new swap bar of tf
                self.assertTrue(math.isnan(values[edge]), (name, edge))
                self.assertTrue(math.isfinite(values[edge + 1]) and math.isfinite(values[edge - 1]), (name, edge))
        one_minute = engine.feature_array({"name": "premium_bps", "tf": "1m", "n": 1}, s, {})
        self.assertTrue(np.isfinite(one_minute[1:]).all())  # on 1m the lag is the rule itself

    def test_index_and_mark_are_read_one_minute_late(self):
        s = self.plain()
        base = engine.feature_array({"name": "premium_bps", "tf": "1m", "n": 1}, s, {})
        s["index_c"] = s["index_c"].copy()
        s["index_c"][200] = 90.0
        moved = engine.feature_array({"name": "premium_bps", "tf": "1m", "n": 1}, s, {})
        changed = ~((base == moved) | (np.isnan(base) & np.isnan(moved)))
        self.assertEqual(np.nonzero(changed)[0].tolist(), [201])

    def test_missing_series_zero_volume_and_holes_are_unknown(self):
        s = self.plain()
        for name in ("premium_bps", "premium_change_bps", "basis_mark_bps"):
            bare = {k: v for k, v in s.items() if k not in ("index_c", "mark_c")}
            self.assertTrue(np.isnan(engine.feature_array({"name": name, "tf": "1m", "n": 2}, bare, {})).all(), name)
        self.assertTrue(np.isnan(engine.feature_array({"name": "funding_settled_bps", "tf": "1m", "n": 1}, s, {})).all())
        s["v"] = s["v"].copy()
        s["v"][150] = 0.0
        s["mark_c"] = s["mark_c"].copy()
        s["mark_c"][300] = np.nan
        premium = engine.feature_array({"name": "premium_bps", "tf": "1h", "n": 1}, s, {})
        # hour 2 (minutes 120..179) has a minute without trades; it is the newest lagged hour from minute 180 to 239
        self.assertTrue(np.isnan(premium[180:240]).all() and np.isfinite(premium[178]) and np.isfinite(premium[240]))
        basis = engine.feature_array({"name": "basis_mark_bps", "tf": "1m", "n": 1}, s, {})
        self.assertTrue(math.isnan(basis[301]) and math.isfinite(basis[300]) and math.isnan(basis[151]))

    def test_every_feature_is_sometimes_known_and_varies(self):
        series = rich_series()
        lows = {name: (low, high) for name, (low, high) in v4.FEATURES.items()}
        for name, (low, high) in lows.items():  # anchors keep their own timeframe; n stays inside its range
            spec = v4.validate_feature({"name": name, "tf": v4.ANCHORS[name][0] if name in v4.ANCHORS else "1h", "n": min(max(low, 2), high)})
            values = engine.feature_array(spec, series, {})[1440:]
            known = values[np.isfinite(values)]
            self.assertTrue(len(known) > 0 and float(np.std(known)) > 0, name)

    def test_lookback_limits_cover_the_funding_window(self):
        self.assertEqual(v4.validate_feature({"name": "funding_settled_bps", "tf": "1d", "n": 90})["n"], 90)
        with self.assertRaises(v4.ContractError):
            v4.validate_feature({"name": "premium_bps", "tf": "1d", "n": 90})


def write_store(folder, skip=(), days=3):
    folder.mkdir(parents=True)
    name = f"okx_ETH-USDT-SWAP_1m_2025-01-01_2025-01-0{days}.csv"
    with (folder / name).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(team_data.COLUMNS)
        for k in range(days * 1440):
            if k in skip:
                continue
            ts = T0 + k * 60_000
            price = 100 + (k % 50) * .01
            writer.writerow([ts, team_data.iso(ts), price, price + .02, price - .02, price + .01, 10, 1, 100, "1"])
    scan = team_data.scan_candles(folder / name)
    (folder / "MANIFEST.json").write_text(json.dumps({"candles": {"file": name, "sha256": team_data.sha256_file(folder / name),
                                                                  "rows": scan["rows"], "gap_count": scan["gap_count"]}}), encoding="utf-8")
    assert team_data.catalog(folder, "ETH-USDT-SWAP")["problems"] == []
    return folder


def write_index_store(folder, days=3):
    """Index candles (price columns, no volume) 2 bps under the swap of write_store."""
    folder.mkdir(parents=True)
    name = f"okx_ETH-USDT_index-1m_2025-01-01_2025-01-0{days}.csv"
    with (folder / name).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(team_data.PRICE_COLUMNS)
        for k in range(days * 1440):
            ts = T0 + k * 60_000
            price = (100 + (k % 50) * .01 + .01) / 1.0002
            writer.writerow([ts, team_data.iso(ts), price, price, price, price, "1"])
    scan = team_data.scan_candles(folder / name)
    (folder / "MANIFEST.json").write_text(json.dumps({"candles": {"file": name, "sha256": team_data.sha256_file(folder / name),
                                                                  "rows": scan["rows"], "gap_count": scan["gap_count"]}}), encoding="utf-8")
    assert team_data.catalog(folder, "ETH-USDT")["problems"] == []
    return folder


def setUpModule():
    """Free RAM is the machine's, not the test's: every RAM floor reached here sees a fake 64 GB, so the suite passes
    beside other jobs; the tests of a floor set their own value on top (2 GB, 1 GB). Real runs keep their floors."""
    for target in (team_eval, research_limits):
        patcher = mock.patch.object(target, "free_ram_gb", return_value=64.0)
        patcher.start()
        unittest.addModuleCleanup(patcher.stop)


@contextmanager
def no_lease(*_args, **_kwargs):
    raise AssertionError("a v4 run takes no heavy slot (D111 p.4)")
    yield


class DataAndCliTests(unittest.TestCase):
    def test_only_single_minute_holes_are_filled_and_warmup_can_come_from_another_store(self):
        with tempfile.TemporaryDirectory() as folder:
            store = write_store(Path(folder) / "mixed", skip={100, *range(500, 530)})
            series = engine.load_minutes(store, T0, T0 + 3 * 86_400_000)
            self.assertEqual(len(series["ts"]), 4320 - 30)
            self.assertEqual(series["filled_minutes"], [[engine.utc(T0 + 100 * 60_000), 1]])
            self.assertEqual(series["gaps"], [[engine.utc(T0 + 500 * 60_000), 30]])
            self.assertEqual(series["o"][100], series["c"][99])
            values = engine.feature_array({"name": "rms_bar_bps", "tf": "1h", "n": 2}, series, {})
            at = int(np.searchsorted(series["ts"], T0 + 539 * 60_000))
            self.assertTrue(np.isnan(values[at]))
            early = write_store(Path(folder) / "early")
            joined = engine.load_minutes(store, T0, T0 + 3 * 86_400_000, warmup=(early, T0 + 86_400_000))
            self.assertIn(T0 + 510 * 60_000, set(joined["ts"].tolist()))

    def test_whole_minutes_and_store_instrument_are_enforced(self):
        for change in ({"max_hold_seconds": 119}, {"cooldown_seconds": 90}):
            with self.assertRaises(v4.ContractError):
                strategy(**change)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = write_store(root / "s")
            payload = {"strategy_schema_version": 4, "name": "t", "author": {"name": "p"}, "description": "d", "original_text": "t",
                       "confirmed": True, "instrument": "SOL-USDT-SWAP",
                       "rules": {"side": "long", "entry": {"when": JUMP}, "order": {"type": "taker"}, "stop_loss_bps": 100,
                                 "take_profit_bps": 200, "max_hold_seconds": 600, "cooldown_seconds": 60, "size_pct": 11}}
            (root / "r.json").write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(research_limits, "research_slot", no_lease), self.assertRaises(SystemExit):
                team_eval.main(["run", "--recipe", str(root / "r.json"), "--store", str(store), "--instrument",
                                str(ROOT / "tools" / "okx_instrument_sol_usdt_swap.json"), "--out", str(root / "o"), "--quiet"])

    def test_cli_reads_the_sibling_index_store_or_reports_premium_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            recipe = root / "r.json"
            recipe.write_text(json.dumps({"strategy_schema_version": 4, "name": "t", "author": {"name": "p"}, "description": "d",
                                          "original_text": "t", "confirmed": True, "instrument": "ETH-USDT-SWAP",
                                          "rules": {"side": "long", "entry": {"when": {"feature": {"name": "premium_bps", "tf": "1h", "n": 1},
                                                                                       "op": "gt", "value": 1}},
                                                    "order": {"type": "taker"}, "stop_loss_bps": 100, "take_profit_bps": 200,
                                                    "max_hold_seconds": 600, "cooldown_seconds": 60, "size_pct": 11}}), encoding="utf-8")
            store = write_store(root / "ETH-USDT-SWAP")
            reports = []
            for present in (False, True):
                if present:
                    write_index_store(root / "ETH-USDT_index")
                out = root / f"out_{present}"
                with mock.patch.object(research_limits, "research_slot", no_lease), redirect_stdout(io.StringIO()):
                    self.assertEqual(team_eval.main(["run", "--recipe", str(recipe), "--store", str(store), "--instrument",
                                                     str(ROOT / "tools" / "okx_instrument_eth_usdt_swap.json"), "--out", str(out),
                                                     "--registry", str(root / "reg.jsonl"), "--quiet"]), 0)
                reports.append(json.loads((out / "ledger.json").read_text(encoding="utf-8")))
            absent, found = reports
            self.assertEqual((absent["positioning_inputs"]["index"]["minutes"], absent["positioning_inputs"]["index"]["inputs"][0]["catalog_sha256"]),
                             (0, None))
            self.assertEqual((absent["trades"], absent["v4_report"]["quarters_mostly_unknown"]), ([], ["2025Q1"]))
            self.assertGreaterEqual(found["positioning_inputs"]["index"]["minutes"], 2 * 1440)  # every loaded swap minute
            self.assertIsNotNone(found["positioning_inputs"]["index"]["inputs"][0]["catalog_sha256"])
            self.assertGreater(len(found["trades"]), 0)
            self.assertEqual(found["v4_report"]["quarters_mostly_unknown"], [])

    def test_a_v4_run_takes_no_heavy_slot_but_refuses_when_memory_is_short(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = write_store(root / "ETH-USDT-SWAP")
            recipe = root / "r.json"
            recipe.write_text(json.dumps({k: v for k, v in strategy(order={"type": "taker"}).items() if k in v4.PAYLOAD_KEYS}), encoding="utf-8")
            argv = ["run", "--recipe", str(recipe), "--store", str(store), "--instrument",
                    str(ROOT / "tools" / "okx_instrument_eth_usdt_swap.json"), "--out", str(root / "o"), "--quiet"]
            with mock.patch.object(research_limits, "research_slot", no_lease),                     mock.patch.object(research_limits, "free_ram_gb", lambda: 1.0), self.assertRaises(SystemExit) as stop:
                team_eval.main(argv)
            self.assertIn("free RAM", str(stop.exception))
            with mock.patch.object(research_limits, "research_slot", no_lease), redirect_stdout(io.StringIO()):
                self.assertEqual(team_eval.main(argv + ["--registry", str(root / "reg.jsonl")]), 0)

    def test_a_sibling_store_with_problems_or_another_instrument_counts_as_absent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = write_store(root / "ETH-USDT-SWAP")
            index = write_index_store(root / "ETH-USDT_index")
            team_data.catalog(index, "SOL-USDT")
            series = engine.load_minutes(store, T0, T0 + 86_400_000)
            sources = engine.attach_positioning(series, store, "ETH-USDT-SWAP", [])
            self.assertEqual((sources["index"]["minutes"], sources["index"]["inputs"][0]["catalog_sha256"]), (0, None))
            self.assertIn("SOL-USDT", sources["index"]["inputs"][0]["rejected"])

    def test_sealed_gates_carry_no_per_quarter_rows_of_the_v4_report(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = write_store(root / "ETH-USDT-SWAP")
            recipe = root / "r.json"
            recipe.write_text(json.dumps({k: v for k, v in strategy().items() if k in v4.PAYLOAD_KEYS}), encoding="utf-8")
            with mock.patch.object(research_limits, "research_slot", no_lease), redirect_stdout(io.StringIO()):
                team_eval.main(["run", "--recipe", str(recipe), "--store", str(store), "--instrument",
                                str(ROOT / "tools" / "okx_instrument_eth_usdt_swap.json"), "--out", str(root / "o"),
                                "--registry", str(root / "reg.jsonl"), "--quiet"])
            ledger = json.loads((root / "o" / "ledger.json").read_text(encoding="utf-8"))
            self.assertIn("2025Q1", ledger["v4_report"]["entry_unknown_share_by_quarter"])
            gates = team_eval.build_gates({**ledger, "data_set": "SEALED"})
            text = json.dumps(gates["v4_report"]) + team_eval.gates_markdown(gates)
            self.assertNotIn("Q1", text)
            self.assertIn("entry_unknown_share_mean", gates["v4_report"])
            line = json.loads((root / "reg.jsonl").read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual((line["execution_model_id"], line["store_catalog_sha256"]), (v4.EXECUTION_MODEL_ID, ledger["store_catalog_sha256"]))

    def test_events_digest_ignores_random_bot_ids_but_not_behaviour(self):
        events = [{"seq": 1, "bot_id": None, "code": "season_started", "data": {"config_sha256": "a"}},
                  {"seq": 2, "bot_id": "x1", "code": "entry", "data": {"price": 1.0}}, {"seq": 3, "bot_id": "x1", "code": "exit", "data": {}}]
        renamed = [{**e, "bot_id": "y9" if e["bot_id"] else None, "data": {**e["data"], **({"config_sha256": "b"} if "config_sha256" in e["data"] else {})}}
                   for e in events]
        self.assertEqual(team_eval.events_digest(events), team_eval.events_digest(renamed))
        moved = [*events[:1], {**events[1], "data": {"price": 1.1}}, events[2]]
        self.assertNotEqual(team_eval.events_digest(events), team_eval.events_digest(moved))

    def test_the_execution_model_id_is_pinned_to_the_engine_source(self):
        # change the engine's rules -> bump strategy_v4.EXECUTION_MODEL_ID and record the new pair here (D106 p. 6)
        source = "".join(inspect.getsource(item) for item in (engine.path_prices, engine.Quotes, engine.quantity, engine.Run,
                                                               engine.simulate, engine.sensitivity))
        digest = hashlib.sha256(source.replace("\r\n", "\n").encode("utf-8")).hexdigest()
        self.assertEqual({v4.EXECUTION_MODEL_ID: digest}, ENGINE_PINS)

    def test_cli_reports_d106_lines_and_fails_on_a_data_hole(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            recipe = root / "r.json"
            recipe.write_text(json.dumps({"strategy_schema_version": 4, "name": "t", "author": {"name": "p"}, "description": "d",
                                          "original_text": "t", "confirmed": True, "instrument": "ETH-USDT-SWAP",
                                          "rules": {"side": "long", "entry": {"when": {"feature": {"name": "return_bps", "tf": "1m", "n": 1},
                                                                                       "op": "lte", "value": -40}},
                                                    "order": {"type": "post_only", "offset": {"bps": 5}, "timeout_seconds": 600},
                                                    "stop_loss_bps": 100, "take_profit_bps": 200,
                                                    "max_hold_seconds": 600, "cooldown_seconds": 60, "size_pct": 11}}), encoding="utf-8")
            for name, skip, errors in (("clean", (), 0), ("hole", {1740, 1741}, 1)):
                store = write_store(root / name, skip=skip)
                out = root / ("out_" + name)
                with mock.patch.object(research_limits, "research_slot", no_lease), redirect_stdout(io.StringIO()):
                    self.assertEqual(team_eval.main(["run", "--recipe", str(recipe), "--store", str(store), "--instrument",
                                                     str(ROOT / "tools" / "okx_instrument_eth_usdt_swap.json"), "--out", str(out),
                                                     "--registry", str(root / "reg.jsonl"), "--quiet"]), 0)
                gates = json.loads((out / "gates.json").read_text(encoding="utf-8"))
                ledger = json.loads((out / "ledger.json").read_text(encoding="utf-8"))
                self.assertEqual((ledger["execution_model_id"], ledger["eval_only"]), (v4.EXECUTION_MODEL_ID, True))
                self.assertEqual(len(gates["v4_report"]["maker_sensitivity"]), 5)
                self.assertIn("all maker as taker (floor)", (out / "gates.md").read_text(encoding="utf-8"))
                status = {r["check"]: r["status"] for r in gates["verdict"]["checks"]}
                self.assertEqual(status["no_engine_or_data_errors"], "FAIL" if errors else "PASS")


if __name__ == "__main__":
    unittest.main()
