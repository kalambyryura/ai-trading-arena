"""Feature book for schema-4 recipes (R1 full F2): closed 1m bars -> strategy_v4 feature values, built incrementally.

One book serves the model tapes and Live alike. Like the evaluator (tools/team_eval_v4.load_minutes), exactly one
missing minute is filled flat at the previous close without trades; a longer hole stays a hole (features UNKNOWN). The caller adds each closed swap minute once it is visible under D106
(close + 5 s), and for positioning features the index and mark closes of minutes and the settled funding as they
become known. Bars of every timeframe and the lagged spread bars are running groups, so a new minute costs O(n) per
feature, and the values come from the reference's own code (strategy_v4.price_feature, lagged_feature,
funding_value) over the same bars the reference builds from the whole history; nothing is rebuilt on every quote.

A secondary minute is read once, when a decision first may read it (one minute after the swap minute closes): an
index or mark close that arrives later never rewrites a value that a decision may already have used. The values of
one bar close are computed once and then served from the cache.
"""
from collections import deque
import math

from . import strategy_v4 as v4

SPREADS = {"premium_bps": "premium", "premium_change_bps": "premium", "basis_mark_bps": "basis"}
SPREAD_NAMES = {"premium": "premium_bps", "basis": "basis_mark_bps"}


class BookError(ValueError):
    pass


def warmup_ms(spec):
    """History a value needs at any close: the feature's reach plus one bar of alignment (and two settlements more
    for funding, whose schedule is confirmed by the steps before the rates it averages)."""
    width, n = v4.TIMEFRAMES[spec["tf"]], spec["n"]
    if spec["name"] in v4.CLOCK:
        return v4.MINUTE
    if spec["name"] in v4.ANCHORS:
        return (8 if spec["name"] == "return_since_utc_week_open_bps" else 1) * v4.DAY + 2 * v4.MINUTE
    if spec["name"] == "funding_settled_bps":
        return (n + 2) * v4.FUNDING_STEP_MS + width + v4.FUNDING_DELAY_MS
    return (n + 3) * width + (v4.SECONDARY_LAG_MS if spec["name"] in v4.POSITIONING else 0)


class FeatureBook:
    def __init__(self, specs):
        self.specs = {}
        for spec in specs:
            clean = v4.validate_feature(spec)
            self.specs[v4.feature_key(clean)] = clean
        self.warmup_ms = max((warmup_ms(s) for s in self.specs.values()), default=0)
        self.depth, self.spread_depth = {}, {}
        for spec in self.specs.values():
            if spec["name"] in SPREADS:
                slot = (SPREADS[spec["name"]], spec["tf"])
                self.spread_depth[slot] = max(self.spread_depth.get(slot, 0), spec["n"] + 2)
            elif spec["name"] not in v4.ANCHORS and spec["name"] != "funding_settled_bps":
                self.depth[spec["tf"]] = max(self.depth.get(spec["tf"], 0), spec["n"] + 3)
        self.anchored = any(s["name"] in v4.ANCHORS and s["name"] not in v4.CLOCK for s in self.specs.values())
        self.opens = {}  # open of every hour-start minute of the last 8 days: the anchors' starting points
        self.groups = {tf: deque() for tf in self.depth}  # [open_ms, o, h, l, c, minutes]
        self.spreads = {slot: deque() for slot in self.spread_depth}  # [open_ms, minute spreads, mean once closed]
        self.pending = deque()  # swap minutes not yet readable by a lagged feature
        self.index, self.mark, self.settlements = {}, {}, {}
        self.last, self.cache, self.ignored, self.filled = None, None, 0, 0

    # ------------------------------------------------------------------------------------------------------- inputs
    def add_minute(self, row):
        """A closed swap 1m candle (open_ms, o, h, l, c, volume); False (and counted) for one not newer than the last."""
        open_ms = row[0]
        if isinstance(open_ms, bool) or not isinstance(open_ms, int) or open_ms % v4.MINUTE:
            raise BookError("minute_open_not_on_the_minute")
        if self.last is not None and open_ms <= self.last[0]:
            self.ignored += 1  # a repeated or revised candle never rewrites history a decision may have used
            return False
        values = [float(x) for x in row[1:6]]
        if not all(math.isfinite(x) and x > 0 for x in values[:4]) or not math.isfinite(values[4]) or values[4] < 0:
            raise BookError("minute_values")
        if self.last is not None and open_ms == self.last[0] + 2 * v4.MINUTE:
            close = self.last[4]
            self._add((self.last[0] + v4.MINUTE, close, close, close, close, 0.0))
            self.filled += 1
        self._add((open_ms, *values))
        return True

    def _add(self, row):
        open_ms = row[0]
        for tf, groups in self.groups.items():
            start = open_ms - open_ms % v4.TIMEFRAMES[tf]
            if groups and groups[-1][0] == start:
                group = groups[-1]
                group[2], group[3], group[4], group[5] = max(group[2], row[2]), min(group[3], row[3]), row[4], group[5] + 1
            else:
                groups.append([start, row[1], row[2], row[3], row[4], 1])
                while len(groups) > self.depth[tf] + 1:
                    groups.popleft()
        if self.spreads:
            self.pending.append(row)
        if self.anchored and open_ms % v4.HOUR == 0:
            self.opens[open_ms] = row[1]
            for stale in [k for k in self.opens if k < open_ms - 8 * v4.DAY]:
                del self.opens[stale]
        self.last = row

    def add_secondary(self, kind, open_ms, close):
        """Index or mark close of the minute that opens at open_ms."""
        target = {"index": self.index, "mark": self.mark}[kind]
        if open_ms not in target:
            target[open_ms] = float(close)

    def add_settlement(self, at_ms, rate):
        """A settled funding rate (None when the settlement happened with an unknown rate)."""
        self.settlements[int(at_ms)] = rate

    # ------------------------------------------------------------------------------------------------------- values
    def _read_spreads(self, until):
        while self.pending and self.pending[0][0] + v4.MINUTE <= until:
            row = self.pending.popleft()
            index, mark = self.index.pop(row[0], None), self.mark.pop(row[0], None)
            for (kind, tf), groups in self.spreads.items():
                value = v4.minute_spread(SPREAD_NAMES[kind], row, index, mark)
                start = row[0] - row[0] % v4.TIMEFRAMES[tf]
                if groups and groups[-1][0] == start:
                    groups[-1][1].append(value)
                else:
                    groups.append([start, [value], None])
                    while len(groups) > self.spread_depth[(kind, tf)] + 1:
                        groups.popleft()
            for target in (self.index, self.mark):  # closes of minutes the swap never had are never read
                for stale in [k for k in target if k < row[0]] if len(target) > 2880 else ():
                    del target[stale]

    def _closed_bars(self, tf, close, count):
        width, out = v4.TIMEFRAMES[tf], []
        for group in reversed(self.groups[tf]):
            if group[0] + width > close:
                continue
            out.append((group[0], tuple(group[1:5]) if group[5] == width // v4.MINUTE else None))
            if len(out) == count:
                break
        return out[::-1]

    def _closed_spreads(self, kind, tf, until, count):
        width, out = v4.TIMEFRAMES[tf], []
        for group in reversed(self.spreads[(kind, tf)]):
            if group[0] + width > until:
                continue
            if group[2] is None:  # a closed group never changes: its mean is computed once, with the reference's sum()
                whole = len(group[1]) == width // v4.MINUTE and all(v is not None for v in group[1])
                group[2] = (sum(group[1]) / len(group[1]),) if whole else (None,)
            out.append((group[0], group[2][0]))
            if len(out) == count:
                break
        return out[::-1]

    def values(self):
        """{feature key: value or None} at the close of the newest minute; None before the first minute."""
        if self.last is None:
            return None
        close = self.last[0] + v4.MINUTE
        if self.cache is not None and self.cache[0] == close:
            return dict(self.cache[1])
        if self.spreads:
            self._read_spreads(close - v4.SECONDARY_LAG_MS)
        settled = sorted(self.settlements.items()) if any(s["name"] == "funding_settled_bps" for s in self.specs.values()) else []
        out = {}
        for key, spec in self.specs.items():
            width, n = v4.TIMEFRAMES[spec["tf"]], spec["n"]
            if spec["name"] in v4.ANCHORS:
                out[key] = v4.anchor_feature(spec, close, self.last[4], self.opens.get)
            elif spec["name"] == "funding_settled_bps":
                out[key] = v4.funding_value(settled, close - close % width, n)
            elif spec["name"] in SPREADS:
                until = v4.lagged_until(spec, close)
                out[key] = None if until is None else v4.lagged_feature(
                    spec, self._closed_spreads(SPREADS[spec["name"]], spec["tf"], until, n + 1), until)
            else:
                out[key] = v4.price_feature(spec, self._closed_bars(spec["tf"], close, n + 1), close, self.last[4])
        self.cache = (close, out)
        return dict(out)

    def book(self):
        """The snapshot form: {"bar_close_ms": close of the newest minute, "values": {...}}."""
        values = self.values()
        return None if values is None else {"bar_close_ms": self.last[0] + v4.MINUTE, "values": values}


def attach(tape, book, minutes, secondary=None, settlements=()):
    """snapshot["v4_features"] for every snapshot of a model tape, from the book fed with each minute visible at that
    snapshot under D106 (close + 5 s) together with its index and mark closes. minutes: [(open_ms, o, h, l, c, volume)]
    in time order, from at least book.warmup_ms before the tape; secondary: {"index": {open_ms: close}, "mark": {...}}."""
    from .strategy_v4_runtime import VISIBILITY_SECONDS
    for at, rate in settlements:
        book.add_settlement(at, rate)
    secondary, k = secondary or {}, 0
    for snapshot in tape:
        newest = round(snapshot["at"] * 1000) - VISIBILITY_SECONDS * 1000 - v4.MINUTE  # open of the newest visible minute
        while k < len(minutes) and minutes[k][0] <= newest:
            row = minutes[k]
            for kind in ("index", "mark"):
                if row[0] in secondary.get(kind, {}):
                    book.add_secondary(kind, row[0], secondary[kind][row[0]])
            book.add_minute(row)
            k += 1
        current = book.book()
        if current is not None:
            snapshot["v4_features"] = current
    return tape


def attach_raw(tape, book):
    """snapshot["v4_features"] for recorded or model snapshots that carry their public candles (raw["candles"], OKX
    layout): each confirmed candle joins the book once visible under D106; the history is what the tape holds."""
    from .strategy_v4_runtime import VISIBILITY_SECONDS
    for snapshot in tape:
        now_ms = round(snapshot["at"] * 1000)
        rows = sorted((r for r in (snapshot.get("raw") or {}).get("candles") or ()
                       if r[8] == "1" and int(r[0]) + v4.MINUTE + VISIBILITY_SECONDS * 1000 <= now_ms), key=lambda r: int(r[0]))
        for row in rows:
            if book.last is None or int(row[0]) > book.last[0]:
                book.add_minute((int(row[0]), *row[1:6]))
        current = book.book()
        if current is not None:
            snapshot["v4_features"] = current
    return tape
