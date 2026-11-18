"""Fast screening backtest for single-position arena recipes on public 1-minute history (B-15).

NOT the production engine and NOT a market recording. Features reproduce the engine's formulas (checked against
trading_lab.arena_market by `check-features`); fills are a labelled model: the decision is taken at the close of a
completed bar plus a declared delay (<60s); UNKNOWN uses the existing V1/V2 kernels, null-hold is refused. The price walks a piecewise-linear O-L-H-C / O-H-L-C path inside each minute, fees are
charged per side and slippage is adverse. Threshold crossings inspect only post-fill path segments. OHLC path order is MODEL/INTRABAR_PATH_AMBIGUOUS; reversed-extreme sensitivity is reported separately. Use it to screen supported ideas; long-series performance is not qualified by the P0 patch. Every finalist still has
to be re-run on the production engine (tools/strategy_round.py, /workshop). A recipe that needs something this model
cannot reproduce (setup -> confirm, adds, partial reduces, position features, funding) is refused, never approximated.

Reads local CSV files only (the public-history format of tools/public_history.py). No network, no account, no orders.

  py -3.12 -X utf8 -B tools/author_backtest.py run --data okx_1m.csv --recipe recipe.json --out runtime/my_backtest
  py -3.12 -X utf8 -B tools/author_backtest.py run --data okx_1m.csv --recipe recipe.json --split 2025-07-01 --split 2026-07-18
  py -3.12 -X utf8 -B tools/author_backtest.py check-features --data okx_1m.csv --samples 500
"""
import argparse
import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys

try:
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view as swv
except ModuleNotFoundError:  # the tool is optional; tests skip without numpy
    np = None

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MINUTE_MS, DAY_MS = 60_000, 86_400_000
CSV_HEAD = ["ts_ms", "utc", "open", "high", "low", "close"]
RECIPE_KEYS = ("name", "author", "description", "original_text", "rules", "confirmed", "strategy_schema_version")
MARKET_FEATURES = ("sma_gap_bps", "return_5m_bps", "return_1m_bps", "rsi_14", "range_position_20", "volume_ratio_5", "atr_14_bps",
                   "body_bps", "upper_wick_bps", "lower_wick_bps", "bollinger_z_20", "ema_gap_bps", "volume_ratio_1", "spread_bps")
EXECUTION_MODEL_VERSION = "screening-ohlc-causal-path-v2"
PATH_SCENARIOS = ("close_direction", "reverse_extremes")

CONTEXT_FEATURES = ("utc_hour", "utc_weekday")  # from the closing time of the completed bar (PR-O2)
COSTS = {"live": (5.0, 0.5), "stress": (5.0, 2.0)}  # (fee bps per side, slippage bps per side): 11 and 14 bps per round trip


class Unsupported(Exception):
    """The recipe needs something the screening model does not reproduce."""


def need_numpy():
    if np is None:
        raise SystemExit("Потрібен numpy: py -3.12 -m pip install numpy")


# ----------------------------------------------------------------------------------------------------------- data
@dataclass
class Series:
    ts: "np.ndarray"  # int64 ms, bar open time
    o: "np.ndarray"
    h: "np.ndarray"
    l: "np.ndarray"
    c: "np.ndarray"
    v: "np.ndarray"   # volume in contracts

    @property
    def n(self):
        return len(self.ts)


def load_bars(paths):
    """Read one or more public-history CSV files; later files win on duplicate minutes; unconfirmed candles are dropped."""
    need_numpy()
    rows = {}
    for path in paths:
        with open(path, newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
            if header is None or header[:6] != CSV_HEAD:
                raise ValueError(f"{path}: очікується заголовок {','.join(CSV_HEAD)},…")
            confirm = header.index("confirm") if "confirm" in header else None
            for row in reader:
                if confirm is not None and row[confirm] != "1":
                    continue
                rows[int(row[0])] = (float(row[2]), float(row[3]), float(row[4]), float(row[5]), float(row[6]))
    if not rows:
        raise ValueError("У файлах немає підтверджених свічок.")
    ts = np.array(sorted(rows), dtype=np.int64)
    arr = np.array([rows[t] for t in ts], dtype=np.float64)
    return Series(ts, arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3], arr[:, 4])


def contiguous_valid(S, lookback=30):
    """valid[i]: the `lookback` bars ending at i exist without gaps."""
    gap = np.zeros(S.n, dtype=np.int64)
    gap[1:] = (np.diff(S.ts) != MINUTE_MS).astype(np.int64)
    cum = np.cumsum(gap)
    valid = np.zeros(S.n, dtype=bool)
    if S.n >= lookback:
        valid[lookback - 1:] = cum[lookback - 1:] == cum[:S.n - lookback + 1]
    return valid


# -------------------------------------------------------------------------------------------------------- features
def _roll_sum(x, k):
    out = np.full(len(x), np.nan)
    cs = np.cumsum(np.insert(x, 0, 0.0))
    out[k - 1:] = cs[k:] - cs[:-k]
    return out


def features(S):
    """The 14 market features of the V1/V2 catalogue plus utc_hour / utc_weekday. Unknown (not enough clean history) is NaN
    and never passes a condition. spread_bps is a model value: one tick (0.1 USDT) of spread."""
    n, o, h, l, c, v = S.n, S.o, S.h, S.l, S.c, S.v
    nan = np.full(n, np.nan)
    F = {}
    r1 = nan.copy(); r1[1:] = (c[1:] / c[:-1] - 1) * 1e4
    r5 = nan.copy(); r5[5:] = (c[5:] / c[:-5] - 1) * 1e4
    F["return_1m_bps"], F["return_5m_bps"] = r1, r5
    s5, s20 = nan.copy(), nan.copy()  # window means, not a running sum: a cumulative sum of 1e5-scale prices over years loses digits
    if n >= 20:
        s5[4:], s20[19:] = swv(c, 5).mean(axis=1), swv(c, 20).mean(axis=1)
    F["sma_gap_bps"] = (s5 / s20 - 1) * 1e4
    ch = nan.copy(); ch[1:] = np.diff(c)
    gain, loss = np.where(np.isnan(ch), 0.0, np.maximum(ch, 0)), np.where(np.isnan(ch), 0.0, np.maximum(-ch, 0))
    g14, l14 = _roll_sum(gain, 14), _roll_sum(loss, 14)
    with np.errstate(divide="ignore", invalid="ignore"):
        rsi = 100 - 100 / (1 + g14 / l14)
    rsi = np.where((g14 == 0) & (l14 == 0), 50.0, np.where(l14 == 0, 100.0, rsi))
    rsi[:14] = np.nan
    F["rsi_14"] = rsi
    hi20, lo20 = nan.copy(), nan.copy()
    if n >= 20:
        hi20[19:], lo20[19:] = swv(h, 20).max(axis=1), swv(l, 20).min(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        F["range_position_20"] = np.where(hi20 == lo20, 50.0, (c - lo20) / (hi20 - lo20) * 100)
    v5 = _roll_sum(v, 5)
    vr5 = nan.copy()
    if n > 5:
        with np.errstate(divide="ignore", invalid="ignore"):
            vr5[5:] = np.where(v5[:-5] > 0, v5[5:] / v5[:-5], np.nan)
    F["volume_ratio_5"] = vr5
    prev = nan.copy(); prev[1:] = c[:-1]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev), np.abs(l - prev)))
    tr[0] = np.nan
    F["atr_14_bps"] = _roll_sum(np.nan_to_num(tr), 14) / 14 / c * 1e4
    F["body_bps"] = (c / o - 1) * 1e4
    F["upper_wick_bps"] = (h - np.maximum(o, c)) / c * 1e4
    F["lower_wick_bps"] = (np.minimum(o, c) - l) / c * 1e4
    z = nan.copy()
    if n >= 20:
        W = swv(c, 20)
        var2, mean2 = nan.copy(), nan.copy()
        var2[19:], mean2[19:] = W.var(axis=1), W.mean(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            z = np.where(var2 > 0, (c - mean2) / np.sqrt(var2), 0.0)
    F["bollinger_z_20"] = z
    gap = nan.copy()
    if n >= 30:
        W30 = swv(c, 30)
        e5, e20 = W30[:, 0].copy(), W30[:, 0].copy()
        for k in range(1, 30):
            e5 = (2 / 6) * W30[:, k] + (1 - 2 / 6) * e5
            e20 = (2 / 21) * W30[:, k] + (1 - 2 / 21) * e20
        gap[29:] = (e5 / e20 - 1) * 1e4
    F["ema_gap_bps"] = gap
    vmean = nan.copy()
    if n > 5:
        vmean[5:] = _roll_sum(v, 5)[4:-1] / 5  # mean of the 5 bars before bar i
    with np.errstate(divide="ignore", invalid="ignore"):
        F["volume_ratio_1"] = np.where(vmean > 0, v / vmean, np.nan)
    valid = contiguous_valid(S, 30)
    for key in F:
        F[key] = np.where(valid, F[key], np.nan)
    F["spread_bps"] = np.where(valid, 0.1 / c * 1e4, np.nan)
    close_ms = S.ts + MINUTE_MS                      # the completed bar closes when the next one opens
    F["utc_hour"] = np.where(valid, (close_ms // 3_600_000 % 24).astype(float), np.nan)
    F["utc_weekday"] = np.where(valid, ((close_ms // DAY_MS + 3) % 7).astype(float), np.nan)  # 1970-01-01 was a Thursday; Monday = 0
    return F, valid


def check_features(S, F, valid, samples=400, seed=7):
    """Max relative difference to trading_lab.arena_market on random clean bars (the engine's own functions)."""
    from trading_lab.arena_market import candle_features, context_features, richer_features
    rng = np.random.default_rng(seed)
    clean = np.flatnonzero(valid)
    if not len(clean):
        raise ValueError("У даних немає жодного відрізка з 30 безперервних свічок.")
    worst = {}
    for i in rng.choice(clean, size=min(samples, len(clean)), replace=False):
        rows = [[str(int(S.ts[k])), S.o[k], S.h[k], S.l[k], S.c[k], S.v[k], 0.0, 0.0, "1"] for k in range(i - 29, i + 1)]
        close_ms = int(S.ts[i]) + MINUTE_MS
        reference = candle_features(rows, close_ms / 1000 + 5)
        reference.update(richer_features(rows))
        reference.update({k: v for k, v in context_features(close_ms, close_ms / 1000 + 5).items() if k in CONTEXT_FEATURES})
        for name, value in reference.items():
            mine = F[name][i]
            diff = (0.0 if np.isnan(mine) else float("inf")) if value is None else abs(mine - value) / max(1.0, abs(value))
            worst[name] = max(worst.get(name, 0.0), diff)
    return worst


# ---------------------------------------------------------------------------------------------------------- rules
def _ops():
    return {"gt": np.greater, "gte": np.greater_equal, "lt": np.less, "lte": np.less_equal}


def _known(F, name):
    if name not in F:
        raise Unsupported(f"ознака «{name}» недоступна в 1-хвилинному бектесті"
                          + (" (потрібен ряд funding)" if "funding" in name else " (позиційна або квотна)"))
    return F[name]


def _feature_names(node):
    """Collect inputs only; rule evaluation belongs to the shared kernel."""
    if node is None:
        return set()
    if "all" in node or "any" in node:
        key = "all" if "all" in node else "any"
        return set().union(*(_feature_names(child) for child in node[key]))
    if "not" in node:
        return _feature_names(node["not"])
    return {node["feature"]} | ({node["ref"]} if "ref" in node else set())


def _scalar(value):
    return value.item() if isinstance(value, np.generic) else value


def predicate_verdicts(F, node):
    """Authoritative True/False/None from the existing V2 kernel, per row.

    NaN, Inf, missing values within a supported feature and bool preserve UNKNOWN.
    An absent required feature remains an explicit Unsupported data/mechanics gap.
    """
    if node is None:
        return None
    from trading_lab import strategy_v2
    names = _feature_names(node)
    arrays = {name: _known(F, name) for name in names}
    n = len(next(iter(F.values())))
    return np.array([strategy_v2.evaluate_predicate(node, {name: _scalar(a[i]) for name, a in arrays.items()})[0]
                     for i in range(n)], dtype=object)


def group_verdicts(F, group):
    """Preserve V1 checks/UNKNOWN while reusing its original evaluator."""
    from trading_lab.arena_strategies import evaluate
    arrays = {item["feature"]: _known(F, item["feature"]) for item in group["conditions"]}
    values = []
    for i in range(len(next(iter(F.values())))):
        verdict, checks = evaluate(group, {name: _scalar(a[i]) for name, a in arrays.items()})
        values.append(None if any(c["passed"] is None for c in checks) else bool(verdict))
    return np.array(values, dtype=object)


def _permission(verdicts):
    # Conversion happens only at the root permission boundary. None is still
    # available through *_verdicts and compile_plan's explicit UNKNOWN counters.
    return np.array([value is True for value in verdicts], dtype=bool)


def group_mask(F, group):
    """V1 permission mask. ANY with an UNKNOWN child never authorizes an action."""
    return _permission(group_verdicts(F, group))


def predicate_mask(F, node):
    """V2 permission mask; never invert or short-circuit an UNKNOWN leaf."""
    return None if node is None else _permission(predicate_verdicts(F, node))


def load_recipe(path):
    """Read an author payload (or a stored record), validate it with the arena's own validator, return the normalized recipe."""
    from trading_lab.arena_strategies import ArenaError, validate_strategy
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    payload = {key: raw[key] for key in RECIPE_KEYS if key in raw}
    payload["confirmed"] = True  # the confirmation gate is for submissions; screening a draft is allowed
    try:
        return validate_strategy(payload)
    except ArenaError as error:
        raise ValueError(f"Рецепт не проходить перевірку арени: {error}") from None


@dataclass
class Plan:
    name: str
    schema: int
    long_entry: object
    short_entry: object
    long_exit: object
    short_exit: object
    sl_bps: float
    tp_bps: float
    hold_s: object
    cooldown_s: float
    size_pct: float
    notes: list
    unknown_counts: dict = field(default_factory=dict)


def compile_plan(recipe, F):
    """Turn a validated recipe into entry/exit masks and parameters, or refuse with the exact reason."""
    rules, name = recipe["rules"], recipe.get("name", "recipe")
    schema = 2 if recipe.get("strategy_schema_version") == 2 else 1
    if int(rules.get("max_entries", 1)) != 1:
        raise Unsupported("max_entries > 1 (докупівля) не моделюється")
    spread = np.isfinite(F["spread_bps"]) & (F["spread_bps"] <= rules["max_spread_bps"])
    unknown_counts = {"spread": int((~np.isfinite(F["spread_bps"])).sum())}

    def gate(node, label, v1=False):
        verdicts = group_verdicts(F, node) if v1 else predicate_verdicts(F, node)
        unknown_counts[label] = sum(value is None for value in verdicts)
        return _permission(verdicts)
    notes = ["max_spread_bps перевіряється за модельним спредом в 1 тік (0,1 USDT)"]
    long_entry = short_entry = long_exit = short_exit = None
    if schema == 1:
        entry = gate(rules["entry"], "entry", True) & spread
        exit_ = gate(rules["exit"], "exit", True) if rules["exit"]["conditions"] else None
        if rules["side"] == "long":
            long_entry, long_exit = entry, exit_
        else:
            short_entry, short_exit = entry, exit_
    else:
        for key in ("add_when", "reduce_when", "cost_checkpoint"):
            if rules.get(key) is not None:
                raise Unsupported(f"{key} (докупівля, часткове скорочення або контроль витрат) не моделюється")
        for side, node in rules["entry"].items():
            if node["setup"] is not None or node["invalidate"] is not None or node["expiry_bars"] is not None:
                raise Unsupported("двоетапний вхід setup → confirm не моделюється (потрібен стан між свічками)")
            mask = gate(node["confirm"], side + "_entry") & spread
            if side == "long":
                long_entry = mask
            else:
                short_entry = mask
        exit_ = gate(rules["exit"], "exit") if rules.get("exit") is not None else None
        long_exit = short_exit = exit_
        if exit_ is not None:
            notes.append("формальний вихід перевіряється за ознаками ринку на закритій свічці")
    hold = rules.get("max_hold_seconds")
    if hold is None:
        raise Unsupported("max_hold_seconds=null: null-tail потребує exact RULES_ONLY route; штучний 24h exit заборонений")
    return Plan(name, schema, long_entry, short_entry, long_exit, short_exit, float(rules["stop_loss_bps"]), float(rules["take_profit_bps"]),
                None if hold is None else float(hold), float(rules["cooldown_seconds"]), float(rules["size_pct"]), notes, unknown_counts)


# ------------------------------------------------------------------------------------------------------ simulator
@dataclass
class Cost:
    fee_bps: float = 5.0   # per side, taker
    slip_bps: float = 0.5  # adverse slippage per side
    sl_extra_bps: float = 0.0  # extra adverse slippage on stop fills


def _model_path(o, h, l, c, scenario):
    from tools.okx_tape_model import minute_path
    price, points, knots = minute_path(o, h, l, c)
    if scenario == "close_direction":
        return price, points, knots
    if scenario != "reverse_extremes":
        raise Unsupported("unknown OHLC path scenario")
    # A second declared OHLC-consistent scenario, not a reconstructed tick path.
    points = [points[0], points[2], points[1], points[3]]
    legs = [abs(b-a) for a, b in zip(points, points[1:])]
    total, knots = sum(legs), [0.]
    for length in legs:
        knots.append(knots[-1]+(60.*length/total if total else 0.))

    def price(second):
        if not total or second <= 0:
            return o
        if second >= 60:
            return c
        for i in range(3):
            if knots[i] <= second <= knots[i+1] and knots[i+1] > knots[i]:
                return points[i]+(points[i+1]-points[i])*(second-knots[i])/(knots[i+1]-knots[i])
        return c
    return price, points, knots


def minute_price(o, h, l, c, second):
    """Primary shared candle MODEL path; OHLC does not prove real tick order."""
    return _model_path(o, h, l, c, "close_direction")[0](second)


def _first_touch(price, knots, level, lower, start, end):
    """First crossing on the declared path AFTER start, not the full bar H/L."""
    hit = (lambda p: p <= level) if lower else (lambda p: p >= level)
    initial = price(start)
    if hit(initial):
        return start, initial  # actual current price; never the pre-fill open
    times = sorted(set([start, end]+[k for k in knots if start < k < end]))
    for a, b in zip(times, times[1:]):
        pa, pb = price(a), price(b)
        if hit(pb):
            crossing = a+(b-a)*(level-pa)/(pb-pa)
            return crossing, level
    return None


def simulate(S, plan, cost=Cost(), delay_s=20.0, windows=None, *, path_scenario="close_direction"):
    """Causal execution on a declared OHLC MODEL; never exact historical fills.

    Unsupported null holds, unavailable delayed fills or open data tails are
    refused explicitly rather than scored as Cash or silently closed at 24h.
    """
    if plan.hold_s is None:
        raise Unsupported("max_hold_seconds=null: use exact RULES_ONLY route, no implicit 24h close")
    if not math.isfinite(delay_s) or not 0 <= delay_s < 60:
        raise Unsupported("delay_s outside [0,60): delayed fill model unavailable")
    if not math.isfinite(plan.hold_s) or plan.hold_s <= 0:
        raise Unsupported("invalid finite hold")
    if path_scenario not in PATH_SCENARIOS:
        raise Unsupported("unknown OHLC path scenario")
    n = S.n
    ts, o, h, l, c = S.ts, S.o, S.h, S.l, S.c
    keys = ("i", "j", "side", "entry_px", "exit_px", "reason", "gross_bps", "net_bps", "hold_s", "t_entry_ms", "window",
            "intrabar_path_status")
    trades = {k: [] for k in keys}
    sig_l = np.zeros(n, dtype=bool) if plan.long_entry is None else plan.long_entry
    sig_s = np.zeros(n, dtype=bool) if plan.short_entry is None else plan.short_entry
    sig_any = (sig_l | sig_s) & ~(sig_l & sig_s)
    windows = windows or [(int(ts[0]), int(ts[-1])+MINUTE_MS)]
    for w_index, (w_start, w_end) in enumerate(windows):
        lo, hi = np.searchsorted(ts, w_start), np.searchsorted(ts, w_end)
        free_at = -1.
        for s in np.flatnonzero(sig_any[lo:hi])+lo:
            e = int(s)+1
            if e >= n:
                break
            t_entry = ts[s]/1000+60+delay_s
            if t_entry < free_at or t_entry >= w_end/1000:
                continue
            if ts[e] != ts[s]+MINUTE_MS:
                raise Unsupported("MODEL_DATA_GAP: next fill bar unavailable")
            side = 1 if sig_l[s] else -1
            entry_px = _model_path(o[e], h[e], l[e], c[e], path_scenario)[0](delay_s)*(1+side*cost.slip_bps/1e4)
            sl_px, tp_px = entry_px*(1-side*plan.sl_bps/1e4), entry_px*(1+side*plan.tp_bps/1e4)
            deadline, scheduled_reason = t_entry+plan.hold_s, "max_hold"
            exit_mask = plan.long_exit if side == 1 else plan.short_exit
            if exit_mask is not None:
                indices = np.flatnonzero(exit_mask[e:])+e
                if len(indices):
                    formal_at = ts[indices[0]]/1000+60+delay_s
                    if formal_at <= deadline:
                        deadline, scheduled_reason = formal_at, "formal_exit"
            ambiguous, outcome = False, None
            for j in range(e, n):
                opening_at = ts[j]/1000
                if opening_at > deadline:
                    break
                if j > e and ts[j] != ts[j-1]+MINUTE_MS:
                    raise Unsupported("MODEL_DATA_GAP: open position crosses missing bar")
                price, points, knots = _model_path(o[j], h[j], l[j], c[j], path_scenario)
                start = delay_s if j == e else 0.
                end = min(60., deadline-opening_at)
                if end < start:
                    break
                # A non-flat OHLC bar does not establish the historical tick path.
                ambiguous = ambiguous or h[j] != l[j]
                events = []
                for priority, (level, lower, reason) in enumerate(((sl_px, side==1, "stop_loss"), (tp_px, side==-1, "take_profit"))):
                    touch = _first_touch(price, knots, level, lower, start, end)
                    if touch is not None:
                        events.append((touch[0], priority, reason, touch[1]))
                if opening_at <= deadline <= opening_at+60:
                    events.append((deadline-opening_at, 2, scheduled_reason, price(deadline-opening_at)))
                if events:
                    offset, _, reason, reference_px = min(events)
                    extra = cost.sl_extra_bps if reason == "stop_loss" else 0.
                    px = reference_px*(1-side*(cost.slip_bps+extra)/1e4)
                    outcome = (j, reason, px, opening_at+offset)
                    break
            if outcome is None:
                raise Unsupported("INCOMPLETE_TAIL: own exit unobserved; use exact RULES_ONLY route")
            j, reason, px, t_exit = outcome
            gross = side*(px/entry_px-1)*1e4
            row = (int(s), int(j), side, entry_px, px, reason, gross, gross-2*cost.fee_bps,
                   t_exit-t_entry, int(t_entry*1000), w_index,
                   "INTRABAR_PATH_AMBIGUOUS" if ambiguous else "MODEL_FLAT_BAR")
            for key, value in zip(keys, row):
                trades[key].append(value)
            free_at = t_exit+plan.cooldown_s
    return {k: np.array(v) for k, v in trades.items()}


def usdt_net(trades, size_pct, budget=100.0, lot_btc=1e-4):
    """Net per trade in USDT for one participant: whole lots of 0.0001 BTC at size_pct of the budget (as the arena sizes)."""
    lots = np.floor(budget * size_pct / 100 / (trades["entry_px"] * lot_btc))
    return trades["net_bps"] / 1e4 * lots * lot_btc * trades["entry_px"], lots


# ----------------------------------------------------------------------------------------------------- statistics
def day_bootstrap_mean(values, day_ids, resamples=1000, seed=11):
    """Cluster-by-day bootstrap of the mean: (mean, low90, high90, z). Fewer than 5 trades: NaN, never a made-up interval."""
    ok = ~np.isnan(values)
    v, d = values[ok], day_ids[ok]
    if len(v) < 5:
        return (float("nan"),) * 4
    days = np.unique(d)
    sums = np.array([v[d == k].sum() for k in days])
    counts = np.array([int((d == k).sum()) for k in days])
    rng = np.random.default_rng(seed)
    means = np.empty(resamples)
    for b in range(resamples):
        pick = rng.integers(0, len(days), len(days))
        means[b] = sums[pick].sum() / max(counts[pick].sum(), 1)
    mean, se = float(v.mean()), float(means.std())
    return mean, float(np.percentile(means, 5)), float(np.percentile(means, 95)), (mean / se if se > 0 else float("nan"))


def summarize(trades, period, plan):
    """Plain numbers for one period (start_ms, end_ms): trades, rate, mean net with its day-clustered 90 % interval, USDT."""
    net = trades["net_bps"].astype(float)
    n = len(net)
    days = max((period[1] - period[0]) / DAY_MS, 1.0)
    out = {"trades": int(n), "per_day": n / days}
    if "intrabar_path_status" in trades:
        ambiguous = trades["intrabar_path_status"] == "INTRABAR_PATH_AMBIGUOUS"
        out["intrabar_path_ambiguous_trades"] = int(ambiguous.sum())
        out["intrabar_path_ambiguous_entry_ms"] = trades["t_entry_ms"][ambiguous].astype(int).tolist()
    if n == 0:
        out.update(mean_net_bps=None, low90=None, high90=None, z=None, win_rate=None, sum_net_bps=0.0, net_usdt=0.0, reasons={})
        return out
    day = trades["t_entry_ms"] // DAY_MS
    mean, low, high, z = day_bootstrap_mean(net, day)
    usdt, lots = usdt_net(trades, plan.size_pct)
    per_day_sum = {}
    for d, value in zip(day, net):
        per_day_sum[int(d)] = per_day_sum.get(int(d), 0.0) + float(value)
    clean = lambda x: None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)
    out.update(mean_net_bps=clean(mean), low90=clean(low), high90=clean(high), z=clean(z), win_rate=float((net > 0).mean()),
               sum_net_bps=float(net.sum()), net_usdt=float(usdt.sum()), lots_per_trade=float(np.mean(lots)),
               avg_hold_min=float(np.mean(trades["hold_s"]) / 60), worst_day_net_bps=float(min(per_day_sum.values())),
               reasons={str(r): int((trades["reason"] == r).sum()) for r in sorted(set(trades["reason"]))})
    return out


# ------------------------------------------------------------------------------------------------------------- CLI
def parse_day(text):
    return int(datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def periods_for(S, splits):
    edges = [int(S.ts[0])] + sorted(parse_day(x) for x in splits) + [int(S.ts[-1]) + MINUTE_MS]
    return [(a, b) for a, b in zip(edges, edges[1:]) if b > a]


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def load_windows(path):
    items = json.loads(Path(path).read_text(encoding="utf-8"))
    return [(int(w["start_ms"]), int(w["end_ms"])) if isinstance(w, dict) else (int(w[0]), int(w[1])) for w in items]


def command_run(args):
    S = load_bars(args.data)
    if args.start:
        keep = S.ts >= parse_day(args.start)
        S = Series(*(a[keep] for a in (S.ts, S.o, S.h, S.l, S.c, S.v)))
    if args.end:
        keep = S.ts < parse_day(args.end)
        S = Series(*(a[keep] for a in (S.ts, S.o, S.h, S.l, S.c, S.v)))
    F, valid = features(S)
    recipe = load_recipe(args.recipe)
    try:
        plan = compile_plan(recipe, F)
    except Unsupported as reason:
        print(f"Рецепт відхилено: {reason}.")
        print("Це не помилка рецепта: модель не відтворює цю механіку, тож краще не оцінювати його взагалі. Перевірте його в Майстерні.")
        persist_attempt(args, {"status": "UNSUPPORTED", "reason": str(reason), "recipe_sha256": recipe.get("recipe_sha256"),
                               "execution_model_version": EXECUTION_MODEL_VERSION, "periods": None})
        return 2
    fee, slip = COSTS[args.cost]
    fee = fee if args.fee_bps is None else args.fee_bps
    slip = slip if args.slip_bps is None else args.slip_bps
    cost = Cost(fee_bps=fee, slip_bps=slip)
    windows = load_windows(args.windows) if args.windows else None
    try:
        trades = simulate(S, plan, cost, args.delay_s, windows)
        reverse = simulate(S, plan, cost, args.delay_s, windows, path_scenario="reverse_extremes")
    except Unsupported as reason:
        print(f"Рецепт/модель відхилено: {reason}.")
        print("MODEL_LIMITATION: no fabricated exit or independent-market-fill claim; use exact RULES_ONLY route.")
        persist_attempt(args, {"status": "MODEL_LIMITATION", "reason": str(reason), "recipe_sha256": recipe.get("recipe_sha256"),
                               "execution_model_version": EXECUTION_MODEL_VERSION, "periods": None})
        return 2
    report = {"model": "screening; not the production engine and not a market recording", "recipe": plan.name, "schema": plan.schema,
              "recipe_sha256": recipe.get("recipe_sha256"), "data": {"bars": int(S.n), "from": iso(int(S.ts[0])), "to": iso(int(S.ts[-1]))},
              "cost": {"fee_bps_per_side": fee, "slip_bps_per_side": slip, "round_trip_bps": 2 * (fee + slip)}, "delay_s": args.delay_s,
              "notes": plan.notes, "periods": [],
              "decision_kernel_version": "shared-v1-v2-predicate-adapter-v1",
              "market_feature_version": "completed-close-wicks-v1;asof-funding-unsupported",
              "execution_model_version": EXECUTION_MODEL_VERSION,
              "accounting_version": "fill-price-slippage-once-fees-v1;funding-unsupported",
              "status": "COMPLETE_MODEL",
              "unknown_rows_by_predicate": plan.unknown_counts,
              "intrabar_evidence": "INTRABAR_PATH_AMBIGUOUS: OHLC gives no tick order; declared scenarios are sensitivity, not market bounds",
              "path_scenarios": {"primary": "close_direction", "sensitivity": "reverse_extremes", "periods": []}}
    for period in periods_for(S, args.split or []):
        mask = (trades["t_entry_ms"] >= period[0]) & (trades["t_entry_ms"] < period[1])
        part = {key: value[mask] for key, value in trades.items()}
        report["periods"].append({"from": iso(period[0]), "to": iso(period[1]), **summarize(part, period, plan)})
        other_mask = (reverse["t_entry_ms"] >= period[0]) & (reverse["t_entry_ms"] < period[1])
        other = {key: value[other_mask] for key, value in reverse.items()}
        report["path_scenarios"]["periods"].append({"from": iso(period[0]), "to": iso(period[1]),
                                                     **summarize(other, period, plan)})
    print_report(report)
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        persist_attempt(args, report)
        with open(out / "trades.csv", "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["entry_utc", "side", "entry_px", "exit_px", "reason", "gross_bps", "net_bps", "hold_s"])
            for k in range(len(trades["i"])):
                writer.writerow([datetime.fromtimestamp(trades["t_entry_ms"][k] / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                 "long" if trades["side"][k] == 1 else "short", f"{trades['entry_px'][k]:.2f}", f"{trades['exit_px'][k]:.2f}",
                                 trades["reason"][k], f"{trades['gross_bps'][k]:.3f}", f"{trades['net_bps'][k]:.3f}", f"{trades['hold_s'][k]:.0f}"])
        print(f"Записано: {out / 'summary.json'}, {out / 'trades.csv'}")
    return 0


def persist_attempt(args, report):
    """Keep refused/incomplete runs too; never serialize them as zero-profit Cash."""
    if not args.out:
        return
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "attempts.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, allow_nan=False) + "\n")
    (out / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=1, allow_nan=False) + "\n",
                                       encoding="utf-8", newline="\n")


def print_report(report):
    print("МОДЕЛЬ ДЛЯ ШВИДКОГО ВІДСІЮВАННЯ ІДЕЙ — не виробничий рушій і не запис ринку; фіналісти перевіряються в Майстерні.")
    c = report["cost"]
    print(f"Рецепт «{report['recipe']}» · дані {report['data']['from']} … {report['data']['to']} ({report['data']['bars']} свічок) · "
          f"витрати {c['round_trip_bps']:.1f} bps за коло · затримка {report['delay_s']:.0f} с")
    if "execution_model_version" in report:
        print(report["execution_model_version"], report["intrabar_evidence"])
        print("UNKNOWN counts:", report["unknown_rows_by_predicate"])
    for note in report["notes"]:
        print("  примітка:", note)
    print(f"{'період':<24}{'угод':>7}{'на добу':>9}{'сер. net, bps':>15}{'90 % інтервал':>22}{'win':>7}{'net USDT':>11}")
    for p in report["periods"]:
        mean = "—" if p["mean_net_bps"] is None else f"{p['mean_net_bps']:+.2f}"
        interval = "—" if p["low90"] is None else f"[{p['low90']:+.1f}; {p['high90']:+.1f}]"
        win = "—" if p.get("win_rate") is None else f"{p['win_rate']:.2f}"
        print(f"{p['from'] + ' … ' + p['to']:<24}{p['trades']:>7}{p['per_day']:>9.2f}{mean:>15}{interval:>22}{win:>7}{p['net_usdt']:>11.4f}")
    if "path_scenarios" in report:
        for row in report["path_scenarios"]["periods"]:
            print(f"MODEL sensitivity reverse_extremes {row['from']} … {row['to']}: {row['trades']} trades, net {row['net_usdt']:.6f} USDT")
    print("Інтервал — bootstrap за добами (угоди однієї доби не незалежні). Нуль угод означає «не торгував», а не «безпечно».")


def command_check(args):
    S = load_bars(args.data)
    F, valid = features(S)
    worst = check_features(S, F, valid, args.samples)
    bad = {k: v for k, v in worst.items() if v > args.tolerance}
    for name in sorted(worst):
        print(f"{name:<20}{worst[name]:.2e}")
    print("ЗБІГ З РУШІЄМ" if not bad else f"РОЗБІЖНІСТЬ: {sorted(bad)}")
    return 0 if not bad else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="screen one recipe on local public-history CSV files")
    run.add_argument("--data", nargs="+", required=True)
    run.add_argument("--recipe", required=True, help="author payload JSON (V1 or V2)")
    run.add_argument("--start", help="UTC date, inclusive")
    run.add_argument("--end", help="UTC date, exclusive")
    run.add_argument("--split", action="append", help="UTC date; repeat to cut the period (TRAIN/VALID/TEST)")
    run.add_argument("--cost", choices=sorted(COSTS), default="live", help="live: 11 bps per round trip; stress: 14 bps")
    run.add_argument("--fee-bps", type=float, help="override the fee per side")
    run.add_argument("--slip-bps", type=float, help="override the slippage per side")
    run.add_argument("--delay-s", type=float, default=20.0, help="seconds from the bar close to the fill")
    run.add_argument("--windows", help="JSON list of {start_ms, end_ms} admission windows, each starting flat")
    run.add_argument("--out", help="directory for summary.json and trades.csv")
    run.set_defaults(handler=command_run)
    check = sub.add_parser("check-features", help="compare the vectorized features with trading_lab.arena_market")
    check.add_argument("--data", nargs="+", required=True)
    check.add_argument("--samples", type=int, default=400)
    check.add_argument("--tolerance", type=float, default=1e-6, help="max relative difference allowed")
    check.set_defaults(handler=command_check)
    args = parser.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from research_limits import ResearchBusy, exclusive_research_slot
    try:
        with exclusive_research_slot(ROOT, "author_backtest"):
            need_numpy()
            return args.handler(args)
    except ResearchBusy as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    sys.exit(main())
