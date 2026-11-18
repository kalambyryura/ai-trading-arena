"""Feature book (R1 full F2): incremental values equal the strategy_v4 reference at every close, causality (POISON),
enough warm-up (TRUNCATION) and late inputs. Synthetic data only."""
import math
from pathlib import Path
import random
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trading_lab import strategy_v4 as v4, strategy_v4_book as fb

T0 = 1_735_689_600_000  # 2025-01-01T00:00:00Z
MIN = 60_000
SPECS = [{"name": "return_bps", "tf": "1m", "n": 60}, {"name": "return_bps", "tf": "15m", "n": 3},
         {"name": "rms_bar_bps", "tf": "1h", "n": 6}, {"name": "rms_bar_bps", "tf": "5m", "n": 12},
         {"name": "sma_gap_bps", "tf": "1d", "n": 2}, {"name": "sma_gap_bps", "tf": "4h", "n": 3},
         {"name": "premium_bps", "tf": "1m", "n": 30}, {"name": "premium_bps", "tf": "1h", "n": 2},
         {"name": "premium_change_bps", "tf": "15m", "n": 2}, {"name": "basis_mark_bps", "tf": "5m", "n": 3},
         {"name": "funding_settled_bps", "tf": "1h", "n": 2}, {"name": "funding_settled_bps", "tf": "1m", "n": 1}]


def market(days, seed=11):
    """Swap minutes with holes (single minutes and a whole hour), minutes without trades, index and mark closes with their
    own holes, and settlements every 8 h that switch to 4 h on day 2 and have one unknown rate."""
    rng = random.Random(seed)
    price, rows, index, mark = 100.0, [], {}, {}
    for k in range(days * 1440):
        o = price
        price *= math.exp(rng.gauss(0, .0015))
        if k in (700, 701) or 2000 <= k < 2060 or rng.random() < .002:
            continue
        rows.append((T0 + k * MIN, o, max(o, price) * 1.0003, min(o, price) * .9997, price, 0.0 if rng.random() < .01 else 5.0))
        if rng.random() > .003:
            index[T0 + k * MIN] = price * (1 - rng.gauss(0, .0004))
        if rng.random() > .003:
            mark[T0 + k * MIN] = price * (1 + rng.gauss(0, .0002))
    funding, at = [], T0 - 3 * 8 * 3_600_000
    while at < T0 + days * 86_400_000:
        funding.append((at, None if at == T0 + 86_400_000 else f"{rng.gauss(1e-4, 5e-5):.8f}"))
        at += (8 if at < T0 + 2 * 86_400_000 else 4) * 3_600_000
    return rows, index, mark, funding


def filled(rows):
    """The evaluator's minutes: exactly one missing minute filled flat at the previous close, without trades."""
    out = []
    for row in rows:
        if out and row[0] == out[-1][0] + 2 * MIN:
            c = out[-1][4]
            out.append((out[-1][0] + MIN, c, c, c, c, 0.0))
        out.append(row)
    return out


def feed(book, row, index, mark):
    if row[0] in index:
        book.add_secondary("index", row[0], index[row[0]])
    if row[0] in mark:
        book.add_secondary("mark", row[0], mark[row[0]])
    book.add_minute(row)


class FeatureBookTests(unittest.TestCase):
    def test_values_equal_the_reference_at_every_sampled_close(self):
        rows, index, mark, funding = market(4)
        secondary = {"index": index, "mark": mark, "funding": funding}
        reference = filled(rows)
        book = fb.FeatureBook(SPECS)
        for at, rate in funding:
            book.add_settlement(at, rate)
        rng = random.Random(5)
        picks = set(rng.sample(range(len(rows)), 150)) | {59, 60, 699, 702, 1439, 1440, 1998, 2001, 2002, 2880, len(rows) - 1}
        compared, unknown = 0, 0
        for i, row in enumerate(rows):
            feed(book, row, index, mark)
            if i not in picks:
                continue
            got = book.values()
            for spec in SPECS:
                ref = v4.feature_value(spec, reference[:reference.index(row) + 1], secondary)
                key = v4.feature_key(spec)
                if ref is None:
                    unknown += 1
                    self.assertIsNone(got[key], (key, i))
                else:
                    compared += 1
                    self.assertEqual(got[key], ref, (key, i))  # the same code over the same bars: bit for bit
        self.assertGreater(compared, 1000)
        self.assertGreater(unknown, 50)  # holes, zero volume and missing index/mark minutes do occur
        self.assertGreater(book.filled, 3)

    def test_future_inputs_never_change_a_value(self):
        rows, index, mark, funding = market(2, seed=3)
        cut = 1700
        clean = fb.FeatureBook(SPECS)
        poisoned = fb.FeatureBook(SPECS)
        for at, rate in funding:
            clean.add_settlement(at, rate)
            poisoned.add_settlement(at, rate if at + v4.FUNDING_DELAY_MS <= rows[cut][0] + MIN else "0.5")
        for row in rows[:cut + 1]:
            feed(clean, row, index, mark)
        poison_index = {k: (v if k <= rows[cut][0] - MIN else v * 3) for k, v in index.items()}
        poison_mark = {k: (v if k <= rows[cut][0] - MIN else v * 3) for k, v in mark.items()}
        for k, v in poison_index.items():
            if k > rows[cut][0] - MIN:
                poisoned.add_secondary("index", k, v)  # future closes known early must stay unread
        for k, v in poison_mark.items():
            if k > rows[cut][0] - MIN:
                poisoned.add_secondary("mark", k, v)
        for row in rows[:cut + 1]:
            feed(poisoned, row, poison_index, poison_mark)
        self.assertEqual(clean.values(), poisoned.values())

    def test_a_book_primed_with_only_its_warm_up_equals_one_with_the_whole_history(self):
        rows, index, mark, funding = market(4, seed=9)
        full, short = fb.FeatureBook(SPECS), fb.FeatureBook(SPECS)
        for at, rate in funding:
            full.add_settlement(at, rate)
            short.add_settlement(at, rate)
        end = len(rows) - 1
        start = rows[end][0] + MIN - short.warmup_ms
        for row in rows:
            feed(full, row, index, mark)
            if row[0] >= start:
                feed(short, row, index, mark)
        self.assertEqual(full.values(), short.values())
        self.assertGreater(sum(v is not None for v in full.values().values()), 6)

    def test_repeats_and_late_closes_never_rewrite_history(self):
        rows, index, mark, _ = market(1, seed=4)
        book = fb.FeatureBook([{"name": "premium_bps", "tf": "1m", "n": 1}, {"name": "return_bps", "tf": "1m", "n": 1}])
        for row in rows[:200]:
            book.add_minute(row)  # no index at all: premium UNKNOWN
        before = book.values()
        self.assertIsNone(before["premium_bps@1m:1"])
        self.assertFalse(book.add_minute(rows[150]))
        self.assertFalse(book.add_minute((rows[199][0], 1.0, 1.0, 1.0, 1.0, 1.0)))
        book.add_secondary("index", rows[197][0], index.get(rows[197][0], 100.0))  # read already: too late
        self.assertEqual(book.values(), before)
        self.assertEqual(book.ignored, 2)
        self.assertEqual(book.book()["bar_close_ms"], rows[199][0] + MIN)
        with self.assertRaises(fb.BookError):
            book.add_minute((rows[199][0] + 30_000, 1.0, 1.0, 1.0, 1.0, 1.0))


if __name__ == "__main__":
    unittest.main()
