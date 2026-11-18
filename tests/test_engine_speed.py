"""R1 full F4: the per-minute candle feature cache and the frame look-back change speed only, never a value or an error.
Synthetic candles only."""
import math
from pathlib import Path
import random
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trading_lab import arena_engine, arena_market
from trading_lab.arena_strategies import ArenaError

START = 1_800_000_000_000


def candles(minutes=31, seed=1):
    rng, price, rows = random.Random(seed), 80000.0, []
    for k in range(minutes):
        o = price
        price *= math.exp(rng.gauss(0, .001))
        rows.append([str(START + k * 60_000), f"{o:.1f}", f"{max(o, price) + 3:.1f}", f"{min(o, price) - 3:.1f}", f"{price:.1f}",
                     f"{rng.uniform(1, 9):.2f}", "0", "0", "1"])
    return rows


def raw(rows, live_price="1"):
    return [[str(int(rows[-1][0]) + 60_000), live_price, live_price, live_price, live_price, "1", "0", "0", "0"]] + list(reversed(rows))


def outcome(function, *args):
    try:
        return function(*args)
    except ArenaError as error:
        return "error: " + str(error)


class CandleCacheTests(unittest.TestCase):
    def setUp(self):
        arena_market._CANDLES.clear()
        arena_market._RICHER.clear()

    def test_cached_values_and_errors_equal_the_full_computation(self):
        rows = candles()
        now = int(rows[-1][0]) / 1000 + 60
        cases = [(raw(rows), now + 5), (raw(rows, "2"), now + 30),  # the minute in progress changes, completed rows do not
                 (raw(rows), now - 1),  # the newest completed candle would be in the future
                 (raw(rows), now + 91),  # stale completed candles
                 (raw(rows)[:1] + [["bad"]] + raw(rows)[1:], now + 5)]  # a malformed row
        arena_market.candle_features(raw(rows), now + 5)  # fill the cache
        for rows_in, server_time in cases:
            self.assertEqual(outcome(arena_market.candle_features, rows_in, server_time),
                             outcome(arena_market._candle_features, rows_in, server_time), server_time)
        self.assertTrue(arena_market._CANDLES)

    def test_odd_rows_give_the_same_value_or_error_as_the_full_computation(self):
        rows = candles(seed=6)
        now = int(rows[-1][0]) / 1000 + 61
        arena_market.candle_features(raw(rows), now)  # warm the cache with the clean rows
        unhashable = [list(r) for r in rows]
        unhashable[3][6] = {"x": 1}  # an unused field the full computation never reads
        boolean = [list(r) for r in rows]
        boolean[5][5] = False  # equal to 0 as a key, but not a number to the full computation
        cases = [(raw(unhashable), now), (raw(boolean), now),
                 (raw(rows) + [["x"]], now - 30)]  # a future completed candle comes before a malformed row
        for rows_in, server_time in cases:
            self.assertEqual(outcome(arena_market.candle_features, rows_in, server_time),
                             outcome(arena_market._candle_features, rows_in, server_time))
        self.assertEqual(outcome(arena_market.candle_features, raw(rows) + [["x"]], now - 30), "error: Нечинна або майбутня свічка.")

    def test_a_returned_dict_can_be_changed_without_touching_the_cache(self):
        rows = candles(seed=4)
        now = int(rows[-1][0]) / 1000 + 65
        first = arena_market.candle_features(raw(rows), now)
        first["sma_gap_bps"] = 1e9
        self.assertNotEqual(arena_market.candle_features(raw(rows), now)["sma_gap_bps"], 1e9)
        richer = arena_market.richer_features(rows[-30:])
        richer.clear()
        self.assertEqual(arena_market.richer_features(rows[-30:]), arena_market._richer_features(rows[-30:]))


class FrameTests(unittest.TestCase):
    def test_the_look_back_finds_the_same_transitions_as_a_full_scan(self):
        state = arena_engine.new_season([], 100, 3600, 1_800_000_000, "f" * 64)
        state["rules"]["chart_interval_seconds"] = 60
        stamp = 1_800_000_000
        codes = ["entry_checks", "rules_decision", "paper_entry_fill", "position_checks", "paper_close_fill", "no_signal"]
        rng = random.Random(9)
        for step in range(400):
            stamp += 5
            for _ in range(rng.randint(0, 3)):
                arena_engine.emit(state, stamp, None, 1, rng.choice(codes), "x")
            previous = state["frames"][-1]
            transitions = {"paper_entry_fill", "paper_close_fill", "exchange_entry_fill", "exchange_close_fill", "stop_requested",
                           "admission_closed", "paused", "season_done", "season_finished", "closing_paused"}
            expected_changed = any(e["code"] in transitions for e in state["events"] if e["seq"] > previous["event_seq"])
            expected_frame = not (stamp - state.get("_frame_stamp", 0) < 60 and not expected_changed)
            before = len(state["frames"])
            arena_engine.frame(state, stamp, None)
            self.assertEqual(len(state["frames"]) > before, expected_frame, step)


if __name__ == "__main__":
    unittest.main()
