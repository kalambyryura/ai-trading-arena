"""Candle engine for schema-v4 recipes (SDK v3, schema 4, R1-early) inside the team evaluator.

One continuous 100 USDT account with one position, simulated on the same 5-second model path the exact engine's
tapes use (tools/okx_tape_model.py: piecewise O->L->H->C / O->H->L->C, bid floored to the tick, ask = bid + tick).
Feature definitions are trading_lab/strategy_v4.py, vectorised here and checked against the reference in tests.

Official v4 rules (D106; execution_model_id below binds verdicts):
  * a bar is visible from the first quote >= its close + 5 s; one rule decision per newly visible bar, at that
    quote; an order reaches the book at the next quote (5 s) after a recheck of the entry rule;
  * taker: ask x 1.0002 / bid x 0.9998, fee 5 bps - the exact engine's quote model and costs;
  * post-only (D096): limit L from the signal bar's close, rounded to the passive side; rejected if it would cross
    the book at placement; filled at L only when the path after placement passes L by max(1 bps, 2 ticks) within
    the timeout; fee 2 bps + 2 bps adverse selection; unfilled orders cancel without a trade;
  * exits on every quote with the engine's change test on the liquidation price: the stop is live from the fill,
    the target from the next minute, and a minute where both are reachable exits at the stop; age >= max_hold
    exits at the next quote; exit fee 5 bps; realized funding of the store's own series, valued at the previous mid;
  * mode="exact" drops the v4 rules (no visibility delay, target allowed at once, 24 h seasons, 20 entries,
    10 % risk stop) to reproduce arena_engine on taker recipes for the conformance proof.
Only a one-minute hole is filled (flat at the previous close, no volume); longer holes make features UNKNOWN and
quotes absent. Positioning features read the swap volume, the index and mark closes of the sibling stores
<BASE>-USDT_index and <instId>_mark (absent store or minute -> UNKNOWN) and the store's settled funding.
"""
from datetime import datetime, timezone
import math
from pathlib import Path
import sys

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _path in (str(ROOT), str(HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)
import team_data  # noqa: E402
from trading_lab import strategy_v4 as v4  # noqa: E402

MINUTE = v4.MINUTE
SLIP, TAKER_FEE, MAKER_FEE, ADVERSE, PASS_THROUGH = .0002, .0005, .0002, .0002, .0001
CAPITAL = 100.0


def utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------------------------------------------------- data
def read_arrays(store, start_ms, end_ms, columns=(2, 3, 4, 5, 6)):
    """Timestamps and the chosen CSV columns (default o, h, l, c, vol) as compact arrays straight from the catalogue
    segments (no per-row Python lists); columns=(5,) reads the close of an index or mark-price store."""
    from array import array
    import csv
    stamps, values = array("q"), array("d")
    for segment in team_data.read_catalog(store)["segments"]:
        if segment["last_ms"] < start_ms or segment["first_ms"] >= end_ms:
            continue
        with (Path(store) / segment["file"]).open(newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            next(reader)
            for row in reader:
                stamp = int(row[0])
                if stamp >= end_ms:
                    break
                if stamp >= start_ms:
                    stamps.append(stamp)
                    values.extend(float(row[k]) for k in columns)
    return np.frombuffer(stamps, dtype=np.int64).copy(), np.frombuffer(values, dtype=np.float64).reshape(-1, len(columns)).copy()


def load_minutes(store, start_ms, end_ms, warmup=None):
    """Arrays ts, o, h, l, c, v for start_ms <= ts < end_ms; only one-minute holes are filled (flat, previous close),
    longer holes stay holes (features UNKNOWN) and are reported. warmup=(store, end_ms) prepends history from
    another store, e.g. the DEV store before a SEALED window."""
    parts = []
    if warmup:
        parts.append(read_arrays(warmup[0], start_ms, warmup[1]))
    parts.append(read_arrays(store, warmup[1] if warmup else start_ms, end_ms))
    stamps = np.concatenate([p[0] for p in parts])
    values = np.concatenate([p[1] for p in parts])
    if not len(stamps):
        raise SystemExit("no candles in the requested range")
    if np.any(np.diff(stamps) <= 0):
        raise SystemExit("candles out of order or duplicated across stores")
    filled, errors = [], []
    steps = np.diff(stamps)
    singles = []
    for at in np.nonzero(steps != MINUTE)[0].tolist():
        missing = int(steps[at] // MINUTE) - 1
        (filled if missing == 1 else errors).append([utc(int(stamps[at]) + MINUTE), missing])
        if missing == 1:
            singles.append(at)
    if singles:  # insert exactly the missing minute of each one-minute hole
        at = np.array(singles)
        stamps = np.insert(stamps, at + 1, stamps[at] + MINUTE)
        flat = np.concatenate([np.repeat(values[at, 3:4], 4, axis=1), np.zeros((len(at), 1))], axis=1)  # no trades
        values = np.insert(values, at + 1, flat, axis=0)
    mask = np.zeros(len(stamps), dtype=bool)
    if singles:
        mask[np.array(singles) + 1 + np.arange(len(singles))] = True  # where each inserted minute landed
    return {"ts": stamps, "o": values[:, 0], "h": values[:, 1], "l": values[:, 2], "c": values[:, 3], "v": values[:, 4],
            "filled": mask, "filled_minutes": filled, "gaps": errors}


def attach_positioning(series, store, inst, funding, warmup=None):
    """What positioning features read: index and mark closes aligned to the swap minutes (NaN where a store or a minute
    is missing) from the sibling stores <BASE>-USDT_index and <instId>_mark, and the settled funding. Returns the
    provenance of each input; a store without a catalogue counts as absent."""
    names = {"index": inst.replace("-SWAP", "") + "_index", "mark": inst + "_mark"}
    begin, end = int(series["ts"][0]), int(series["ts"][-1]) + MINUTE
    ranges = ([(Path(warmup[0]), begin, warmup[1])] if warmup else []) + [(Path(store), warmup[1] if warmup else begin, end)]
    sources = {}
    for kind, name in names.items():
        stamps, closes, used = [], [], []
        for root, a, b in ranges:
            folder = root.parent / name
            catalogued = (folder / team_data.CATALOG).exists()
            rejected = None
            if catalogued:
                found = team_data.read_catalog(folder)
                if found.get("problems") or found.get("instrument") not in (name, name.rsplit("_", 1)[0]):
                    rejected = f"catalogue of {found.get('instrument')} with {len(found.get('problems') or [])} problems"
                    catalogued = False
            used.append({"store": str(folder).replace("\\", "/"), "rejected": rejected,
                         "catalog_sha256": team_data.sha256_file(folder / team_data.CATALOG) if catalogued else None})
            if catalogued and a < b:
                ts, vals = read_arrays(folder, a, b, columns=(5,))
                stamps.append(ts)
                closes.append(vals[:, 0])
        aligned = np.full(len(series["ts"]), np.nan)
        if stamps:
            ts, close = np.concatenate(stamps), np.concatenate(closes)
            pos = np.minimum(np.searchsorted(series["ts"], ts), len(series["ts"]) - 1)
            hit = series["ts"][pos] == ts
            aligned[pos[hit]] = close[hit]
        series[kind + "_c"] = aligned
        sources[kind] = {"inputs": used, "minutes": int(np.isfinite(aligned).sum())}
    series["funding"] = list(funding)
    sources["funding"] = {"settlements": len(series["funding"]),
                          "first": utc(series["funding"][0][0]) if series["funding"] else None,
                          "last": utc(series["funding"][-1][0]) if series["funding"] else None}
    return sources


def bars(series, tf):
    """Per-bar arrays of timeframe tf (index = bar number since epoch - first) and completeness."""
    width = v4.TIMEFRAMES[tf]
    ts = series["ts"]
    number = ts // width
    first = int(number[0])
    slot = (number - first).astype(np.int64)
    count = int(slot[-1]) + 1
    size = np.bincount(slot, minlength=count)
    o, h, l, c = (np.full(count, np.nan) for _ in range(4))
    present, first_index = np.unique(slot, return_index=True)  # slots ascend with time
    last_index = np.append(first_index[1:], len(slot)) - 1
    o[present] = series["o"][first_index]
    h[present] = np.maximum.reduceat(series["h"], first_index)
    l[present] = np.minimum.reduceat(series["l"], first_index)
    c[present] = series["c"][last_index]
    complete = size == width // MINUTE
    return {"first": first, "width": width, "o": o, "h": h, "l": l, "c": c, "complete": complete}


def rolling_sum(values, n):
    """Each window summed on its own, so a value never depends on where the series starts (WARM-UP INVARIANCE)."""
    out = np.full(len(values), np.nan)
    if len(values) >= n:
        out[n - 1:] = np.lib.stride_tricks.sliding_window_view(values, n).sum(axis=1)
    return out


def rolling_extreme(values, n, lowest=False):
    """Max (or min) of each window of n, O(N) by blocks (van Herk / Gil-Werman); exact, no arithmetic."""
    out = np.full(len(values), np.nan)
    if len(values) < n:
        return out
    pick = np.minimum if lowest else np.maximum
    pad = (-len(values)) % n
    blocks = np.concatenate([values, np.full(pad, np.inf if lowest else -np.inf)]).reshape(-1, n)
    prefix = pick.accumulate(blocks, axis=1).ravel()
    suffix = pick.accumulate(blocks[:, ::-1], axis=1)[:, ::-1].ravel()
    out[n - 1:] = pick(suffix[:len(values) - n + 1], prefix[n - 1:len(values)])
    return out


def anchor_array(spec, series):
    """Block-B anchors at every minute close, as strategy_v4.anchor_feature."""
    close = series["ts"] + MINUTE
    name = spec["name"]
    if name == "utc_minute_of_day":
        return (close // MINUTE % 1440).astype(float)
    if name == "utc_weekday":
        return ((close // v4.DAY + 3) % 7).astype(float)
    if name == "us_session_minute":
        years = close.astype("datetime64[ms]").astype("datetime64[Y]").astype(np.int64) + 1970
        bounds = {int(y): v4.us_summer_time(int(y)) for y in np.unique(years)}
        start = np.array([bounds[int(y)][0] for y in years])
        end = np.array([bounds[int(y)][1] for y in years])
        local = close - np.where((close >= start) & (close < end), 4, 5) * v4.HOUR
        minute = local // MINUTE % 1440
        session = ((local // v4.DAY + 3) % 7 < 5) & (minute >= 570) & (minute < 960)
        return np.where(session, minute - 570, -1).astype(float)
    ts = series["ts"]
    day = ts - ts % v4.DAY
    if name == "return_since_utc_day_open_bps":
        anchor = day
    elif name == "return_since_utc_week_open_bps":
        anchor = day - ((day // v4.DAY + 3) % 7) * v4.DAY
    else:
        start = day + spec["n"] * v4.HOUR
        anchor = np.where(start <= ts, start, start - v4.DAY)
    k = np.minimum(np.searchsorted(ts, anchor), len(ts) - 1)
    found = ts[k] == anchor
    out = np.full(len(ts), np.nan)
    out[found] = (series["c"][found] / series["o"][k[found]] - 1) * 10000
    return out


def funding_array(spec, series):
    """funding_settled_bps at every minute close, as strategy_v4.funding_value."""
    out = np.full(len(series["ts"]), np.nan)
    rows = series.get("funding") or []
    n, width = spec["n"], v4.TIMEFRAMES[spec["tf"]]
    if len(rows) <= n:
        return out
    at = np.array([t for t, _ in rows], dtype=np.int64)
    rate = np.array([float(r) if r is not None else np.nan for _, r in rows])
    step = np.concatenate([[0], np.diff(at)])
    before = np.concatenate([[0], step[:-1]])  # a new interval counts once two equal steps confirm it
    ok = np.isfinite(rate) & (step > 0) & (step <= v4.FUNDING_STEP_MS + MINUTE) & (before > 0) & (np.abs(step - before) <= MINUTE)
    factor = np.where(np.abs(step - v4.FUNDING_STEP_MS) <= MINUTE, 1.0, v4.FUNDING_STEP_MS / np.where(step > 0, step, 1))
    total = rolling_sum(np.where(ok, rate * factor, 0.0), n)
    broken = rolling_sum((~ok).astype(float), n)
    bar_close = (series["ts"] + MINUTE) // width * width
    last = np.searchsorted(at + v4.FUNDING_DELAY_MS, bar_close, side="right") - 1
    good = last >= n - 1
    i = np.nonzero(good)[0]
    j = last[good]
    fresh = (bar_close[good] - at[j] < step[j] + v4.FUNDING_DELAY_MS) & (broken[j] == 0)
    out[i[fresh]] = total[j[fresh]] / n * 10000
    return out


def secondary_array(spec, series, cache):
    """premium_bps, premium_change_bps and basis_mark_bps at every minute close, as strategy_v4.positioning_value."""
    out = np.full(len(series["ts"]), np.nan)
    name, tf, n = spec["name"], spec["tf"], spec["n"]
    index, volume = series.get("index_c"), series.get("v")
    top = series.get("mark_c") if name == "basis_mark_bps" else series["c"]
    if index is None or volume is None or top is None:
        return out
    key = ("secondary", name == "basis_mark_bps", tf)
    if key not in cache:
        width = v4.TIMEFRAMES[tf]
        with np.errstate(invalid="ignore", divide="ignore"):
            per_minute = (top / index - 1) * 10000
        valid = np.isfinite(per_minute) & (index > 0) & (volume > 0)
        first = int(series["ts"][0]) // width
        slot = series["ts"] // width - first
        count = int(slot[-1]) + 1
        total = np.bincount(slot, weights=np.where(valid, per_minute, 0.0), minlength=count)  # in time order, as the reference
        whole = np.bincount(slot, weights=valid.astype(float), minlength=count) == width // MINUTE
        cache[key] = (first, width, np.where(whole, total / (width // MINUTE), np.nan), whole)
    first, width, mean, whole = cache[key]
    last = (series["ts"] + MINUTE - v4.SECONDARY_LAG_MS) // width - 1 - first  # newest lagged bar at each minute close
    behind = (last < (series["ts"] + MINUTE) // width - 1 - first) if width > MINUTE else np.zeros(len(last), bool)
    if name == "premium_change_bps":
        ok = last - n >= 0
        i, j = last[ok], last[ok] - n
        good = whole[i] & whole[j] & ~behind[ok]
        out[np.nonzero(ok)[0][good]] = (mean[i] - mean[j])[good]
        return out
    stat = rolling_sum(np.where(whole, mean, 0.0), n) / n
    broken = rolling_sum((~whole).astype(float), n)
    ok = (last >= n - 1) & ~behind
    i = last[ok]
    good = broken[i] == 0
    out[np.nonzero(ok)[0][good]] = stat[i][good]
    return out


def feature_array(spec, series, cache):
    """Value of one feature at the close of every minute (NaN = UNKNOWN), as trading_lab.strategy_v4.feature_value."""
    if spec["name"] == "funding_settled_bps":
        return funding_array(spec, series)
    if spec["name"] in v4.POSITIONING:
        return secondary_array(spec, series, cache)
    if spec["name"] in v4.ANCHORS:
        return anchor_array(spec, series)
    tf, n = spec["tf"], spec["n"]
    if tf not in cache:
        cache[tf] = bars(series, tf)
    b = cache[tf]
    last = (series["ts"] + MINUTE) // b["width"] - 1 - b["first"]  # last closed bar at each minute close
    valid = last >= 0
    bad = (~b["complete"]).astype(float)
    out = np.full(len(series["ts"]), np.nan)
    if spec["name"] == "return_bps":
        ok = valid & (last - n >= 0)
        i, j = last[ok], last[ok] - n
        good = b["complete"][i] & b["complete"][j]
        value = (b["c"][i] / b["c"][j] - 1) * 10000
        out[np.nonzero(ok)[0][good]] = value[good]
        return out
    if spec["name"] == "realized_vol_bps":  # std (ddof 1) of the n log returns ending at the latest closed bar
        with np.errstate(invalid="ignore", divide="ignore"):
            logs = np.log(b["c"][1:] / b["c"][:-1]) * 10000
        good = b["complete"][1:] & b["complete"][:-1]
        centre = float(np.mean(logs[good])) if good.any() else 0.0  # shift-invariant variance, less cancellation
        logs = np.concatenate([[0.0], np.where(good, logs - centre, 0.0)])
        mean = rolling_sum(logs, n) / n
        var = (rolling_sum(logs * logs, n) - n * mean * mean) / (n - 1)
        broken = rolling_sum(bad, n + 1)
        ok = valid & (last >= n)
        i = last[ok]
        good = broken[i] == 0
        out[np.nonzero(ok)[0][good]] = np.sqrt(np.maximum(var[i][good], 0.0))
        return out
    broken = rolling_sum(bad, n)
    if spec["name"] == "rms_bar_bps":
        x = np.where(b["complete"], (b["c"] / b["o"] - 1) * 10000, 0.0)
        stat = np.sqrt(rolling_sum(x * x, n) / n)
    elif spec["name"] in ("dist_high_bps", "dist_low_bps"):
        lowest = spec["name"] == "dist_low_bps"
        stat = rolling_extreme(np.where(b["complete"], b["l" if lowest else "h"], np.inf if lowest else -np.inf), n, lowest)
    else:
        stat = rolling_sum(np.where(b["complete"], b["c"], 0.0), n) / n
    ok = valid & (last >= n - 1)
    i = last[ok]
    good = broken[i] == 0
    with np.errstate(divide="ignore", invalid="ignore"):  # windows with an incomplete bar are dropped just below
        value = stat[i] if spec["name"] == "rms_bar_bps" else (series["c"][ok] / stat[i] - 1) * 10000  # sma or extreme
    out[np.nonzero(ok)[0][good]] = value[good]
    return out


def predicate_mask(node, arrays):
    """(true, known) boolean arrays with V2/v4 semantics: UNKNOWN anywhere makes the whole verdict UNKNOWN."""
    if "all" in node or "any" in node:
        parts = [predicate_mask(child, arrays) for child in node.get("all", node.get("any"))]
        known = np.logical_and.reduce([k for _, k in parts])
        true = (np.logical_and if "all" in node else np.logical_or).reduce([t for t, _ in parts])
        return true & known, known
    if "not" in node:
        true, known = predicate_mask(node["not"], arrays)
        return ~true & known, known
    actual = arrays[v4.feature_key(node["feature"])]
    expected = arrays[v4.feature_key(node["ref"])] * node["mult"] if "ref" in node else np.full(len(actual), float(node["value"]))
    known = np.isfinite(actual) & np.isfinite(expected)
    with np.errstate(invalid="ignore"):
        true = v4.OPS[node["op"]](actual, expected)
    return true & known, known


# ----------------------------------------------------------------------------------------------------------- engine
POLL = 5  # seconds between quotes, as the exact engine's tapes
DECISION = 30  # seconds between rule decisions, as arena_engine RULES["decision_seconds"]
EXECUTION_MODEL_ID = v4.EXECUTION_MODEL_ID  # bump it in strategy_v4 on any rule change; a test pins it to this engine's source


def path_prices(o, h, l, c, seconds):
    """Model price at `seconds` into each minute: piecewise linear O->L->H->C (close >= open) or O->H->L->C,
    leg time proportional to its length - the same path as tools/okx_tape_model.minute_path."""
    o, h, l, c = (np.asarray(x, dtype=float)[:, None] for x in (o, h, l, c))
    s = np.asarray(seconds, dtype=float)[None, :]
    up = c >= o
    p0, p1, p2, p3 = o, np.where(up, l, h), np.where(up, h, l), c
    d1, d2, d3 = np.abs(p1 - p0), np.abs(p2 - p1), np.abs(p3 - p2)
    total = d1 + d2 + d3
    safe = np.where(total > 0, total, 1.0)
    k1 = 60.0 * d1 / safe
    k2 = k1 + 60.0 * d2 / safe
    k3 = k2 + 60.0 * d3 / safe
    zero = np.zeros_like(k1)
    out = np.broadcast_to(c, (o.shape[0], s.shape[1])).copy()
    with np.errstate(invalid="ignore", divide="ignore"):
        for a, b, pa, pb in ((k2, k3, p2, p3), (k1, k2, p1, p2), (zero, k1, p0, p1)):  # earliest leg wins
            take = (s <= b) & (b > a)
            out = np.where(take, pa + (pb - pa) * (s - a) / np.where(b > a, b - a, 1.0), out)
    out = np.where((total == 0) | (s <= 0), o, out)
    return np.where(s >= 60, c, out)


class Quotes:
    """Model quotes of the exact engine's tapes: bid = path floored to the tick, ask = bid + tick, every 5 s."""

    def __init__(self, series, tick):
        self.ts = series["ts"] // 1000
        self.o, self.h, self.l, self.c = series["o"], series["h"], series["l"], series["c"]
        self.tick = tick
        self.decimals = max(0, -int(math.floor(math.log10(tick) + 1e-9)))
        self.close_s = self.ts + 60

    def minute(self, t):
        k = int(np.searchsorted(self.ts, t - t % 60))
        return k if k < len(self.ts) and self.ts[k] == t - t % 60 else None

    def visible(self, t, delay):
        """Index of the newest bar visible at second t: closed and at least `delay` seconds old."""
        return int(np.searchsorted(self.close_s + delay, t, side="right")) - 1

    def prices(self, times):
        """Path prices at many quote times (array of seconds); NaN where the minute is missing."""
        times = np.asarray(times, dtype=np.int64)
        base = times - times % 60
        k = np.searchsorted(self.ts, base)
        k = np.minimum(k, len(self.ts) - 1)
        ok = self.ts[k] == base
        out = np.full(len(times), np.nan)
        if ok.any():
            kk, offs = k[ok], (times - base)[ok]
            grid = path_prices(self.o[kk], self.h[kk], self.l[kk], self.c[kk], np.arange(0, 60, POLL))
            out[ok] = grid[np.arange(len(kk)), offs // POLL]
        return out

    def book(self, times):
        price = self.prices(times)
        bid = np.round(np.floor(price / self.tick + 1e-9) * self.tick, self.decimals)
        return price, bid, np.round(bid + self.tick, self.decimals)


def quantity(notional, price, spec):
    """Base quantity rounded down to the lot, as arena_engine.quantity; 0 below the minimum size."""
    lot = float(spec["lotSz"]) * float(spec["ctVal"])
    qty = math.floor(notional / price / lot + 1e-9) * lot
    return qty if qty + 1e-15 >= float(spec["minSz"]) * float(spec["ctVal"]) else 0.0


class Run:
    """Counters and trades of one simulated account (or one exact-mode window)."""

    def __init__(self):
        self.trades, self.orders = [], []
        self.counters = {"signals": 0, "decisions": 0, "entry_cancelled_on_recheck": 0, "maker_orders": 0, "maker_unfilled": 0,
                         "maker_rejected_crossing": 0, "too_small": 0, "open_at_end": 0, "data_gap_exits": 0, "ambiguous_minute_stops": 0,
                         "entry_cap_reached": 0, "risk_stops": 0}


def simulate(strategy, series, instrument, funding, start_ms, end_ms, *, mode="official", maker_through_bps=1.0,
             maker_adverse_bps=2.0, lower_bound=False, admission_end_ms=None, entry_cap=None, risk_stop_pct=None,
             diagnostics=None):
    """One account on the 5-second model path.

    mode="official" (D106): a bar is visible 5 s after its close; one decision per newly visible bar; taker entries
    fill at the next quote after a recheck; post-only orders rest from the next quote, are rejected if they would
    cross, fill at L only when the path passes L by max(through bps, 2 ticks) after placement and within the timeout;
    the stop is live from the fill, the target from the next minute, and a minute where both are reachable exits at
    the stop. mode="exact" reproduces arena_engine for taker recipes (bar visible at its close, the engine's own
    target test) and is used only to prove conformance. lower_bound=True books every maker entry as a taker fill
    at L (5 bps fee + 2 bps slippage), the floor D106 asks for in every report.
    """
    rules = strategy["rules"]
    diagnostics = diagnostics or {}  # explanation runs only (never official): "no_cross_reject", "candle_fill_from_next_minute"
    branches = v4.entry_branches(rules)
    tick = float(instrument["tickSz"])
    quotes = Quotes(series, tick)
    delay = 5 if mode == "official" else 0
    arrays, cache = {}, {}
    for key, spec in v4.required_features(rules).items():
        arrays[key] = feature_array(spec, series, cache)
    masks = {s: predicate_mask(p, arrays) for s, p in branches.items()}
    # a side enters when its branch holds and every other branch is known and false (side both: one net position)
    holds = {s: masks[s][0] & np.logical_and.reduce([masks[o][1] & ~masks[o][0] for o in masks if o != s] or [np.ones_like(masks[s][0])])
             for s in masks}
    signal = np.logical_or.reduce(list(holds.values()))
    if "filled" in series:  # a minute filled flat here is missing in Live, where no decision can see it
        signal &= ~series["filled"]
    long_bar = holds.get("long", np.zeros_like(signal))
    run = Run()
    begin_s, end_s = start_ms // 1000, end_ms // 1000
    admission_s = (admission_end_ms // 1000) if admission_end_ms else end_s
    run.counters["signals"] = int(signal[(quotes.ts >= begin_s) & (quotes.ts < end_s)].sum())
    if len(masks) > 1:
        conflict = masks["long"][0] & masks["short"][0]
        run.counters["side_conflicts"] = int(conflict[(quotes.ts >= begin_s) & (quotes.ts < end_s)].sum())
    signal_bars = np.nonzero(signal)[0]
    funding_events = [(t // 1000, float(r)) for t, r in funding if r is not None and begin_s <= t // 1000 <= end_s + 86400 * 3]
    order, hold, cooldown = rules["order"], rules["max_hold_seconds"], rules["cooldown_seconds"]
    stop_bps, target_bps = rules["stop_loss_bps"], rules["take_profit_bps"]
    budget = CAPITAL * rules["size_pct"] / 100
    bank, peak, stopped, entries = CAPITAL, CAPITAL, False, 0
    next_decision = begin_s
    while True:
        # first decision tick >= next_decision whose newest visible bar carries a signal
        t = None
        if mode == "official":  # v4: one decision per newly visible bar, at its first quote (close + 5 s), if free
            at = int(np.searchsorted(signal_bars + 0, int(np.searchsorted(quotes.close_s + delay, next_decision))))
            if at < len(signal_bars):
                t = int(quotes.close_s[signal_bars[at]]) + delay
        else:  # exact engine: rule decisions on its own 30 s grid
            first_bar = quotes.visible(next_decision, delay)
            at = int(np.searchsorted(signal_bars, max(first_bar, 0)))
            while at < len(signal_bars):
                b = int(signal_bars[at])
                seen_from = int(quotes.close_s[b]) + delay
                candidate = next_decision + DECISION * max(0, -(-(seen_from - next_decision) // DECISION))
                if quotes.visible(candidate, delay) == b:
                    t = candidate
                    break
                at += 1
        if t is None or t >= admission_s or stopped:
            break
        if entry_cap is not None and entries >= entry_cap:
            run.counters["entry_cap_reached"] += 1
            break
        run.counters["decisions"] += 1
        bar = quotes.visible(t, delay)
        side_name = "long" if long_bar[bar] else "short" if "short" in holds else "long"
        side = 1 if side_name == "long" else -1
        place = t + POLL  # the next fresh quote, as the engine's minimum latency
        if not holds[side_name][quotes.visible(place, delay)] or place >= admission_s:  # recheck the same branch first
            run.counters["entry_cancelled_on_recheck"] += 1
            next_decision = t + (POLL if mode == "official" else DECISION)
            continue
        _, bid_p, ask_p = quotes.book([place])
        if not np.isfinite(bid_p[0]):
            next_decision = t + DECISION
            continue
        if order["type"] == "taker":
            price = float(ask_p[0]) * 1.0002 if side > 0 else float(bid_p[0]) * .9998
            fill_t, fee_rate, adverse, kind = place, TAKER_FEE, 0.0, "taker"
        else:
            b = quotes.visible(t, delay)
            values = {k: float(arrays[k][b]) for k in arrays}
            limit = v4.limit_price(rules, float(quotes.c[b]), values, tick, side_name)
            if limit is None:
                next_decision = t + (POLL if mode == "official" else DECISION)
                continue
            if not quantity(budget, limit, instrument):  # below the minimum lot: HOLD, no order reaches the book
                run.counters["too_small"] += 1
                next_decision = t + (POLL if mode == "official" else DECISION)
                continue
            run.counters["maker_orders"] += 1
            if not diagnostics.get("no_cross_reject") and ((side > 0 and ask_p[0] <= limit) or (side < 0 and bid_p[0] >= limit)):  # post-only never takes liquidity
                run.counters["maker_rejected_crossing"] += 1
                run.orders.append({"placed_at": utc(place * 1000), "limit_px": limit, "outcome": "rejected_crossing"})
                next_decision = t + (POLL if mode == "official" else DECISION)
                continue
            through = max(limit * maker_through_bps / 10000, 2 * tick)
            if diagnostics.get("candle_fill_from_next_minute"):  # the reference model: whole 1m lows from the signal's next minute
                through = limit * maker_through_bps / 10000
                first = quotes.minute(t - t % 60)
                mins = np.arange(first, min(first + order["timeout_seconds"] // 60, len(quotes.ts)))
                lows = quotes.l[mins] if side > 0 else quotes.h[mins]
                times = quotes.ts[mins]
                path = lows
            else:
                times = np.arange(place + POLL, place + order["timeout_seconds"] + 1, POLL)
                path = quotes.prices(times)
            hit = np.nonzero(path <= limit - through)[0] if side > 0 else np.nonzero(path >= limit + through)[0]
            if not len(hit):
                run.counters["maker_unfilled"] += 1
                run.orders.append({"placed_at": utc(place * 1000), "limit_px": limit, "outcome": "unfilled"})
                next_decision = place + order["timeout_seconds"]
                continue
            fill_t = int(times[hit[0]])
            run.orders.append({"placed_at": utc(place * 1000), "limit_px": limit, "outcome": "filled", "filled_at": utc(fill_t * 1000)})
            if lower_bound:
                price, fee_rate, adverse, kind = limit * (1 + side * SLIP), TAKER_FEE, 0.0, "post_only_as_taker"
            else:
                price, fee_rate, adverse, kind = limit, MAKER_FEE, maker_adverse_bps / 10000, "post_only"
        qty = quantity(budget, price, instrument)
        if not qty:
            run.counters["too_small"] += 1
            next_decision = t + (POLL if mode == "official" else DECISION)
            continue
        entries += 1
        entry_fee, adverse_cost = qty * price * fee_rate, qty * price * adverse
        bank -= entry_fee + adverse_cost
        # exits: every quote from the fill, as the engine; liquidation price = bid x 0.9998 (long) / ask x 1.0002 (short)
        horizon = np.arange(fill_t, min(fill_t + hold + 2 * POLL, end_s + 86400 * 2) + 1, POLL)
        _, bids, asks = quotes.book(horizon)
        liquidation = bids * .9998 if side > 0 else asks * 1.0002
        change = side * (liquidation / price - 1) * 10000
        age = horizon - fill_t
        stop_hit = change <= -stop_bps
        target_hit = change >= target_bps
        if mode == "official":
            target_hit &= (horizon - horizon % 60) > (fill_t - fill_t % 60)  # maker or taker: target from the next minute
        hold_hit = age >= hold
        valid = np.isfinite(liquidation)
        trigger = np.nonzero((stop_hit | target_hit | hold_hit) & valid & np.append(valid[1:], False))[0]
        reason, ambiguous = None, False
        if len(trigger):
            j = int(trigger[0])
            reason = "stop_loss" if stop_hit[j] else "take_profit" if target_hit[j] else "max_hold"
            exit_t = int(horizon[j + 1])
            exit_price = float(liquidation[j + 1])
            if mode == "official" and reason == "take_profit":
                k = quotes.minute(int(horizon[j]))
                worst = (quotes.l[k] if side > 0 else quotes.h[k])
                worst_mark = (math.floor(worst / tick + 1e-9) * tick * .9998) if side > 0 else ((math.floor(worst / tick + 1e-9) * tick + tick) * 1.0002)
                if side * (worst_mark / price - 1) * 10000 <= -stop_bps:  # both reachable inside one minute: the stop wins
                    reason, ambiguous = "stop_loss", True
                    exit_price = price * (1 - side * stop_bps / 10000)
                    run.counters["ambiguous_minute_stops"] += 1
        else:  # no quote to exit on: the data end (D097) or a hole inside the period, at the last mark
            at_end = int(horizon[-1]) >= int(quotes.close_s[-1])
            reason = "forced_close_end_of_period" if at_end else "data_gap_exit"
            finite = np.nonzero(np.isfinite(liquidation))[0]
            j = int(finite[-1]) if len(finite) else 0
            exit_t, exit_price = int(horizon[j]), float(liquidation[j])
            run.counters["open_at_end" if at_end else "data_gap_exits"] += 1
        gross = side * qty * (exit_price - price)
        exit_fee = qty * exit_price * TAKER_FEE
        paid = 0.0
        for at_s, rate in funding_events:
            if fill_t < at_s <= exit_t:  # settled while the position was open; valued at the previous quote's mid
                prev = at_s - at_s % POLL - (POLL if at_s % POLL == 0 else 0)
                _, pb, pa = quotes.book([prev])
                if np.isfinite(pb[0]):
                    paid -= side * qty * (pb[0] + pa[0]) / 2 * rate
        paid = float(paid)  # plain floats: numpy scalars must not leak into the reports
        net = float(gross - entry_fee - adverse_cost - exit_fee + paid)
        bank += gross - exit_fee + paid
        span = slice(0, max(int(np.searchsorted(horizon, exit_t)), 1))
        marks = side * qty * (liquidation[span] - price) - entry_fee - adverse_cost - qty * liquidation[span] * TAKER_FEE
        marks = np.append(marks[np.isfinite(marks)], net)
        trigger_minute = quotes.minute(int(exit_t - POLL)) if reason not in ("forced_close_end_of_period", "data_gap_exit") else None
        trigger_range = (float(quotes.h[trigger_minute] - quotes.l[trigger_minute]) / price * 10000) if trigger_minute is not None else None
        trades_peak = np.maximum.accumulate(np.maximum(marks, 0.0))
        run.trades.append({"window_id": utc(fill_t * 1000)[:10], "entry_at": utc(fill_t * 1000), "exit_at": utc(exit_t * 1000),
                           "seconds": float(exit_t - fill_t), "side": side_name, "adds": 0, "partial_reduces": 0,
                           "first_notional_usdt": round(qty * price, 6), "executed_notional_usdt": round(qty * (price + exit_price), 6),
                           "gross_usdt": round(gross, 8), "fees_usdt": round(entry_fee + exit_fee, 8),
                           "adverse_selection_usdt": round(adverse_cost, 8), "funding_usdt": round(paid, 8),
                           "net_usdt": round(net, 8), "exit_reason": reason, "ambiguous_minute": ambiguous,
                           "forced_close": reason in ("forced_close_end_of_period", "data_gap_exit"), "entry_order": kind,
                           "signal_at": utc(t * 1000), "limit_px": round(price if kind == "post_only" else 0.0, 10) or None,
                           "trigger_minute_range_bps": None if trigger_range is None else round(trigger_range, 4),
                           "mtm_best_usdt": round(float(max(marks.max(), 0.0)), 8), "mtm_worst_usdt": round(float(min(marks.min(), 0.0)), 8),
                           "mtm_drawdown_usdt": round(float((trades_peak - marks).max()), 8),
                           "executions": [{"at": utc(fill_t * 1000), "kind": "entry", "quantity": qty, "price_usdt": price,
                                           "notional_usdt": round(qty * price, 8), "fee_usdt": round(entry_fee, 10)},
                                          {"at": utc(exit_t * 1000), "kind": "close", "quantity": qty, "price_usdt": exit_price,
                                           "notional_usdt": round(qty * exit_price, 8), "fee_usdt": round(exit_fee, 10)}]})
        if risk_stop_pct is not None:  # exact mode: the engine's 10 % season drawdown stop, on realised equity
            peak = max(peak, bank)
            if (peak - bank) / peak * 100 >= risk_stop_pct:
                stopped = True
                run.counters["risk_stops"] += 1
        next_decision = exit_t + cooldown
    return run.trades, run.counters, run.orders


def simulate_exact_windows(strategy, series, instrument, funding, windows):
    """Conformance mode: the exact engine's 24 h seasons (fresh state each, 20 entries, 10 % risk stop, tail)."""
    trades, totals = [], {}
    tail = strategy["rules"]["max_hold_seconds"] + 420
    for window in windows:
        start = window["start_s"] * 1000
        rows, counters, _ = simulate(strategy, series, instrument, funding, start, start + (window["admission_seconds"] + tail) * 1000,
                                     mode="exact", admission_end_ms=start + window["admission_seconds"] * 1000,
                                     entry_cap=20, risk_stop_pct=10)
        for row in rows:
            row["window_id"] = window["id"]
        trades += rows
        for key, value in counters.items():
            totals[key] = totals.get(key, 0) + value
    return trades, totals


def sensitivity(strategy, series, instrument, funding, start_ms, end_ms):
    """D106 grid for post-only recipes: pass-through 1/2 bps x adverse selection 2/4 bps, plus the floor: the fills of
    the strictest cell (2 bps) booked as taker entries at the limit, 5 bps fee + 2 bps slippage."""
    if strategy["rules"]["order"]["type"] != "post_only":
        return None
    out = []
    for through in (1.0, 2.0):
        for adverse in (2.0, 4.0):
            rows, counters, _ = simulate(strategy, series, instrument, funding, start_ms, end_ms,
                                         maker_through_bps=through, maker_adverse_bps=adverse)
            out.append({"through_bps": through, "adverse_bps": adverse, "trades": len(rows),
                        "net_usdt": round(sum(r["net_usdt"] for r in rows), 6), "maker_unfilled": counters["maker_unfilled"]})
    rows, counters, _ = simulate(strategy, series, instrument, funding, start_ms, end_ms, maker_through_bps=2.0, lower_bound=True)
    out.append({"through_bps": 2.0, "adverse_bps": None, "lower_bound_all_maker_as_taker": True, "trades": len(rows),
                "net_usdt": round(sum(r["net_usdt"] for r in rows), 6), "maker_unfilled": counters["maker_unfilled"]})
    return out


def unknown_share(strategy, series, start_ms, end_ms):
    """Share of the period's minutes where the entry rule is UNKNOWN, by quarter (D106 report: in quarters where the
    recipe's features are mostly UNKNOWN the E1/E2 checks are only formal)."""
    arrays, cache = {}, {}
    for key, spec in v4.required_features(strategy["rules"]).items():
        arrays[key] = feature_array(spec, series, cache)
    known = np.logical_and.reduce([predicate_mask(p, arrays)[1] for p in v4.entry_branches(strategy["rules"]).values()])
    inside = (series["ts"] >= start_ms) & (series["ts"] < end_ms)
    stamps = series["ts"][inside].astype("datetime64[ms]")
    labels = (stamps.astype("datetime64[Y]").astype(int) + 1970) * 10 + (stamps.astype("datetime64[M]").astype(int) % 12) // 3 + 1
    unknown = ~known[inside]
    return {f"{label // 10}Q{label % 10}": round(float(unknown[labels == label].mean()), 4) for label in np.unique(labels).tolist()}


def day_windows(start_ms, end_ms):
    """Daily pseudo-windows for the gate arithmetic (span, coverage, quarters); the account itself is continuous."""
    out, day = [], start_ms - start_ms % 86_400_000
    while day < end_ms:
        out.append({"id": utc(day)[:10], "start_utc": utc(day), "admission_seconds": 86400, "status": "COMPLETE",
                    "pnl_usdt": 0.0, "trades": 0, "max_drawdown_pct": 0.0, "event_counts": {}, "errors": {}})
        day += 86_400_000
    return out
