"""Strategy schema v4 ("SDK v3", early subset R1-early): pure validation, closed-bar features and decisions.

No transport, filesystem, model or orders. Schema 3 belongs to the G1 pilot, so SDK v3 recipes carry
strategy_schema_version 4. The official gates are the candle engine of tools/team_eval.py; since R1 full the exact
PAPER/Demo/Live engines also run BTC taker recipes (arena_strategies.validate_v4, strategy_v4_runtime, the feature book
of strategy_v4_book), all with the definitions below. Every value comes from COMPLETED bars; missing history or a missing
minute inside a bar makes a feature UNKNOWN, and UNKNOWN anywhere in a predicate forbids the entry.

Positioning features (D106 p. 2-3) read secondary inputs: settled funding is known from the first bar close at or
after the settlement + 60 s; index and mark bars are read one minute late (the bar that closed 60 s before the
swap bar), and on bars of 5m and longer a lagged bar older than the swap's newest bar of the same timeframe (the
first minute after a bar boundary) is UNKNOWN, so no rule mixes two bars; a swap minute without trades has no premium
or basis. While a series is absent from the data catalogue
these features stay UNKNOWN, so the arrival of data never changes this code or its hash.
"""
from bisect import bisect_left
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import operator

SEMANTICS = "strategy-v4-early-1"
CATALOGUE = "v4-closed-bar-2"
# The offline execution model (tools/team_eval_v4.py) these semantics are evaluated with; part of the semantic hash so
# that any change of fill, exit or timing rules forces requalification (D106). Bump on every such change.
EXECUTION_MODEL_ID = "v4-path5s-2"
MINUTE = 60_000
INSTRUMENTS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP")
TIMEFRAMES = {"1m": MINUTE, "5m": 5 * MINUTE, "15m": 15 * MINUTE, "1h": 60 * MINUTE, "4h": 240 * MINUTE, "1d": 1440 * MINUTE}
HOUR, DAY = 60 * MINUTE, 1440 * MINUTE
FEATURES = {"return_bps": (1, 1440), "rms_bar_bps": (2, 500), "sma_gap_bps": (1, 200),
            "realized_vol_bps": (2, 720), "dist_high_bps": (1, 200), "dist_low_bps": (1, 200)}
POSITIONING = {"funding_settled_bps": (1, 90), "premium_bps": (1, 200), "premium_change_bps": (1, 200), "basis_mark_bps": (1, 200)}
# Block B anchors: a fixed timeframe; n is a parameter (the hour of return_since_utc_hour_bps), not a window.
ANCHORS = {"utc_minute_of_day": ("1m", 1, 1), "utc_weekday": ("1d", 1, 1), "us_session_minute": ("1m", 1, 1),
           "return_since_utc_day_open_bps": ("1d", 1, 1), "return_since_utc_week_open_bps": ("1d", 7, 7),
           "return_since_utc_hour_bps": ("1h", 0, 23)}
CLOCK = {"utc_minute_of_day", "utc_weekday", "us_session_minute"}  # need no price: never UNKNOWN
FEATURES.update(POSITIONING)
FEATURES.update({name: (low, high) for name, (_, low, high) in ANCHORS.items()})
FUNDING_DELAY_MS = MINUTE  # settlement S is known from the first bar close >= S + 60 s, in history as in Live
FUNDING_STEP_MS = 480 * MINUTE  # the longest OKX interval; rates are expressed per 8 h, a longer step is a gap
SECONDARY_LAG_MS = MINUTE  # index and mark bars are read one minute late, as Live sees them after the swap bar
MAX_LOOKBACK_MS = 90 * 1440 * MINUTE  # warm-up the evaluator provides before the first decision
OPS = {"gt": operator.gt, "gte": operator.ge, "lt": operator.lt, "lte": operator.le}
BOUNDS = {"size_pct": (5, 30), "stop_loss_bps": (20, 1500), "take_profit_bps": (40, 3000),
          "max_hold_seconds": (60, 86400), "cooldown_seconds": (0, 86400)}
PAYLOAD_KEYS = {"strategy_schema_version", "name", "author", "description", "original_text", "confirmed", "instrument", "rules"}
RULE_KEYS = set(BOUNDS) | {"side", "entry", "order"}


class ContractError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                     allow_nan=False).encode("utf-8")).hexdigest()


def known(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def bounded(value, low, high, integer=False):
    if not known(value) or not low <= value <= high or (integer and int(value) != value):
        raise ContractError("invalid_number")
    return value


def feature_key(spec):
    return f'{spec["name"]}@{spec["tf"]}:{spec["n"]}'


def validate_feature(spec):
    if not isinstance(spec, dict) or set(spec) != {"name", "tf", "n"} or spec["name"] not in FEATURES or spec["tf"] not in TIMEFRAMES:
        raise ContractError("feature_shape_name_or_timeframe")
    low, high = FEATURES[spec["name"]]
    n = bounded(spec["n"], low, high, integer=True)
    width = TIMEFRAMES[spec["tf"]]
    if spec["name"] in ANCHORS:
        if spec["tf"] != ANCHORS[spec["name"]][0]:
            raise ContractError("anchor_timeframe")
        reach = {"return_since_utc_week_open_bps": 7 * DAY}.get(spec["name"], DAY) + MINUTE
    elif spec["name"] == "funding_settled_bps":
        reach = n * FUNDING_STEP_MS + width
    else:
        reach = (n + 1) * width + SECONDARY_LAG_MS * (spec["name"] in POSITIONING)
    if reach > MAX_LOOKBACK_MS:
        raise ContractError("feature_lookback_over_90_days")
    return {"name": spec["name"], "tf": spec["tf"], "n": int(n)}


def validate_predicate(node):
    """Bounded all/any/not trees; a leaf compares a feature with a number or with mult x another feature."""
    budget = [0]

    def visit(item, depth):
        budget[0] += 1
        if depth > 6 or budget[0] > 64 or not isinstance(item, dict):
            raise ContractError("predicate_depth_nodes_or_shape")
        if set(item) in ({"all"}, {"any"}):
            key = next(iter(item))
            if not isinstance(item[key], list) or not 1 <= len(item[key]) <= 8:
                raise ContractError("predicate_group_size")
            return {key: [visit(child, depth + 1) for child in item[key]]}
        if set(item) == {"not"}:
            return {"not": visit(item["not"], depth + 1)}
        if item.get("op") not in OPS:
            raise ContractError("predicate_operator")
        if set(item) == {"feature", "op", "value"}:
            return {"feature": validate_feature(item["feature"]), "op": item["op"], "value": bounded(item["value"], -1e9, 1e9)}
        if set(item) == {"feature", "op", "ref", "mult"}:
            mult = bounded(item["mult"], -10, 10)
            if mult == 0:
                raise ContractError("predicate_zero_multiplier")
            return {"feature": validate_feature(item["feature"]), "op": item["op"], "ref": validate_feature(item["ref"]), "mult": mult}
        raise ContractError("predicate_leaf_shape")

    return visit(node, 1)


def validate_order(order):
    if order == {"type": "taker"}:
        return {"type": "taker"}
    if not isinstance(order, dict) or set(order) != {"type", "offset", "timeout_seconds"} or order["type"] != "post_only":
        raise ContractError("order_shape")
    offset = order["offset"]
    if isinstance(offset, dict) and set(offset) == {"bps"}:
        clean = {"bps": bounded(offset["bps"], 1, 500)}
    elif isinstance(offset, dict) and set(offset) == {"mult", "of"}:
        clean = {"mult": bounded(offset["mult"], .01, 10), "of": validate_feature(offset["of"])}
    else:
        raise ContractError("order_offset_shape")
    timeout = bounded(order["timeout_seconds"], 60, 7200, integer=True)
    if timeout % 60:
        raise ContractError("order_timeout_whole_minutes")
    return {"type": "post_only", "offset": clean, "timeout_seconds": int(timeout)}


def validate_rules(rules):
    if not isinstance(rules, dict) or set(rules) != RULE_KEYS:
        raise ContractError("v4_rule_keys")
    clean = {key: bounded(rules[key], *limits, integer=key in {"max_hold_seconds", "cooldown_seconds"})
             for key, limits in BOUNDS.items()}
    if clean["max_hold_seconds"] % 60 or clean["cooldown_seconds"] % 60:
        raise ContractError("hold_and_cooldown_whole_minutes")  # decisions and exits happen on minute closes
    if rules["side"] not in ("long", "short", "both"):
        raise ContractError("v4_side")
    entry = rules["entry"]
    if rules["side"] == "both":  # two branches, one net position (CATALOGUE_R1_FULL_UA.md section 4)
        if (not isinstance(entry, dict) or set(entry) != {"long", "short"}
                or any(not isinstance(entry[s], dict) or set(entry[s]) != {"when"} for s in ("long", "short"))):
            raise ContractError("v4_entry_shape")
        clean_entry = {s: {"when": validate_predicate(entry[s]["when"])} for s in ("long", "short")}
    else:
        if not isinstance(entry, dict) or set(entry) != {"when"}:
            raise ContractError("v4_entry_shape")
        clean_entry = {"when": validate_predicate(entry["when"])}
    if clean["take_profit_bps"] <= 14:
        raise ContractError("take_profit_below_published_cost_model")
    clean.update(side=rules["side"], entry=clean_entry, order=validate_order(rules["order"]))
    return clean


def validate_payload(payload):
    """Clean v4 recipe with recipe_sha256 over its canonical JSON (the same recipe always hashes alike)."""
    if not isinstance(payload, dict) or set(payload) != PAYLOAD_KEYS or payload["strategy_schema_version"] != 4:
        raise ContractError("v4_payload_keys")
    if payload["instrument"] not in INSTRUMENTS:
        raise ContractError("v4_instrument")
    for key in ("name", "description", "original_text"):
        if not isinstance(payload[key], str) or not 1 <= len(payload[key]) <= (200 if key == "name" else 20000):
            raise ContractError("v4_text_fields")
    if payload["confirmed"] is not True or not isinstance(payload["author"], dict):
        raise ContractError("v4_author_or_confirmation")
    clean = {**deepcopy({k: payload[k] for k in ("name", "author", "description", "original_text", "confirmed", "instrument")}),
             "strategy_schema_version": 4, "rules": validate_rules(payload["rules"])}
    clean["semantics"] = SEMANTICS
    clean["recipe_sha256"] = digest({k: v for k, v in clean.items() if k != "recipe_sha256"})
    # eval_only describes this candle-engine record; the exact engines admit BTC taker recipes through
    # arena_strategies.validate_v4, while ETH/SOL and post-only stay evaluator-only until R2 (G11).
    clean.update(eval_only=True, catalogue=CATALOGUE, execution_model_id=EXECUTION_MODEL_ID,
                 semantic_sha256=digest({"semantics": SEMANTICS, "catalogue": CATALOGUE, "execution_model_id": EXECUTION_MODEL_ID,
                                         "instrument": clean["instrument"], "rules": clean["rules"]}))
    return clean


def predicate_features(node, found=None):
    """Every feature spec a predicate (or order offset) reads, keyed canonically."""
    found = {} if found is None else found
    if "all" in node or "any" in node:
        for child in node.get("all", node.get("any")):
            predicate_features(child, found)
    elif "not" in node:
        predicate_features(node["not"], found)
    else:
        for key in ("feature", "ref"):
            if key in node:
                found[feature_key(node[key])] = node[key]
    return found


def entry_branches(rules):
    """{side: entry predicate}: the one branch of a long or short recipe, or both branches of side both."""
    if rules["side"] == "both":
        return {side: rules["entry"][side]["when"] for side in ("long", "short")}
    return {rules["side"]: rules["entry"]["when"]}


def entry_side(rules, values):
    """(side or None, checks, conflict): a side enters only when its branch holds and every other branch is known and
    false; UNKNOWN anywhere or two true branches (a conflict) hold the entry."""
    verdicts, checks = {}, []
    for side, predicate in entry_branches(rules).items():
        verdicts[side], found = evaluate(predicate, values)
        checks += [{**check, "branch": side} for check in found] if rules["side"] == "both" else found
    if any(v is None for v in verdicts.values()):
        return None, checks, False
    true = [side for side, verdict in verdicts.items() if verdict]
    return (true[0] if len(true) == 1 else None), checks, len(true) > 1


def required_features(rules):
    found = {}
    for predicate in entry_branches(rules).values():
        predicate_features(predicate, found)
    offset = rules["order"].get("offset", {})
    if "of" in offset:
        found[feature_key(offset["of"])] = offset["of"]
    return found


# ----------------------------------------------------------------------------------------------------------- reference
def closed_bars(minutes, tf):
    """Closed bars of timeframe tf from consecutive closed 1m candles [(open_ms, o, h, l, c), ...] (UTC-aligned).

    A bar is closed once its last minute is; a bar with any minute missing is returned as None."""
    width = TIMEFRAMES[tf]
    if not minutes:
        return []
    last_close = minutes[-1][0] + MINUTE
    groups = {}
    for row in minutes:
        groups.setdefault(row[0] - row[0] % width, []).append(row)
    out = []
    for start in sorted(groups):
        if start + width > last_close:
            continue
        rows = groups[start]
        if len(rows) != width // MINUTE:
            out.append((start, None))
            continue
        out.append((start, (rows[0][1], max(r[2] for r in rows), min(r[3] for r in rows), rows[-1][4])))
    if out and out[0][0] < minutes[0][0]:
        out = out[1:]
    return out


def feature_value(spec, minutes, secondary=None):
    """Reference value of one feature at the close of the last minute in `minutes` (None = UNKNOWN).

    minutes: consecutive closed 1m swap candles (open_ms, o, h, l, c[, volume]); secondary (positioning only):
    {"index": {open_ms: close}, "mark": {open_ms: close}, "funding": [(settlement_ms, rate or None), ...]}."""
    if spec["name"] in POSITIONING:
        return positioning_value(spec, minutes, secondary or {})
    if not minutes:
        return None
    if spec["name"] in ANCHORS:
        opens = [m[0] for m in minutes]

        def open_of(open_ms):
            k = bisect_left(opens, open_ms)
            return minutes[k][1] if k < len(opens) and opens[k] == open_ms else None
        return anchor_feature(spec, minutes[-1][0] + MINUTE, minutes[-1][4], open_of)
    return price_feature(spec, closed_bars(minutes, spec["tf"]), minutes[-1][0] + MINUTE, minutes[-1][4])


def us_summer_time(year):
    """[start, end) of US daylight saving time in UTC ms: the 2nd Sunday of March 07:00 UTC to the 1st Sunday of November
    06:00 UTC (02:00 local both times; rules since 2007)."""
    def sunday(month, nth):
        first = datetime(year, month, 1, tzinfo=timezone.utc)
        return round(first.timestamp() * 1000) + ((6 - first.weekday()) % 7 + 7 * (nth - 1)) * DAY
    return sunday(3, 2) + 7 * HOUR, sunday(11, 1) + 6 * HOUR


def us_session_minute(close):
    """Minutes since 09:30 America/New_York at `close` on Mon-Fri before 16:00, else -1. US daylight saving since 2007:
    from the 2nd Sunday of March 02:00 EST (07:00 UTC) to the 1st Sunday of November 02:00 EDT (06:00 UTC); no holidays."""
    start, end = us_summer_time(datetime.fromtimestamp(close / 1000, timezone.utc).year)
    local = close - (4 if start <= close < end else 5) * HOUR
    minute = int(local // MINUTE % 1440)
    return minute - 570 if (local // DAY + 3) % 7 < 5 and 570 <= minute < 960 else -1


def anchor_open_ms(spec, last_open):
    """Open of the minute an anchor return starts from, for the newest closed minute (it opens at last_open)."""
    day = last_open - last_open % DAY
    if spec["name"] == "return_since_utc_day_open_bps":
        return day
    if spec["name"] == "return_since_utc_week_open_bps":
        return day - ((day // DAY + 3) % 7) * DAY  # Monday 00:00 UTC; 1970-01-01 was a Thursday
    start = day + spec["n"] * HOUR
    return start if start <= last_open else start - DAY


def anchor_feature(spec, close, price, open_of):
    """A block-B anchor at the decision close; open_of(open_ms) gives that minute's open or None (UNKNOWN). The
    reference and the feature book share this code."""
    if spec["name"] == "utc_minute_of_day":
        return int(close // MINUTE % 1440)
    if spec["name"] == "utc_weekday":
        return int((close // DAY + 3) % 7)
    if spec["name"] == "us_session_minute":
        return us_session_minute(close)
    start = open_of(anchor_open_ms(spec, close - MINUTE))
    return None if start is None else (price / start - 1) * 10000


def price_feature(spec, bars, close, price):
    """A price feature from the closed tf bars [(open_ms, (o, h, l, c) or None), ...] at the decision close (the close of
    the newest minute, whose close is `price`); the reference and the feature book share this code."""
    n = spec["n"]
    width = TIMEFRAMES[spec["tf"]]
    if not bars or bars[-1][0] != (close // width - 1) * width:
        return None  # the latest closed bar itself is missing: never fall back to an older one
    if spec["name"] == "return_bps":
        if len(bars) < n + 1 or bars[-1][1] is None or bars[-1 - n][1] is None:
            return None
        if bars[-1][0] - bars[-1 - n][0] != n * TIMEFRAMES[spec["tf"]]:
            return None
        return (bars[-1][1][3] / bars[-1 - n][1][3] - 1) * 10000
    if spec["name"] == "realized_vol_bps":  # std (ddof 1) of the n log returns ending with the latest closed bar
        window = bars[-n - 1:]
        if len(window) < n + 1 or any(b[1] is None for b in window) or window[-1][0] - window[0][0] != n * width:
            return None
        logs = [math.log(window[k][1][3] / window[k - 1][1][3]) * 10000 for k in range(1, n + 1)]
        mean = sum(logs) / n
        return math.sqrt(sum((x - mean) ** 2 for x in logs) / (n - 1))
    window = bars[-n:]
    if len(window) < n or any(b[1] is None for b in window) or window[-1][0] - window[0][0] != (n - 1) * TIMEFRAMES[spec["tf"]]:
        return None
    if spec["name"] == "rms_bar_bps":
        return math.sqrt(sum(((b[1][3] / b[1][0] - 1) * 10000) ** 2 for b in window) / n)
    if spec["name"] == "dist_high_bps":
        return (price / max(b[1][1] for b in window) - 1) * 10000
    if spec["name"] == "dist_low_bps":
        return (price / min(b[1][2] for b in window) - 1) * 10000
    return (price / (sum(b[1][3] for b in window) / n) - 1) * 10000


def funding_value(settlements, bar_close, n):
    """Mean of the last n settled rates in bps per 8 h known at bar_close. The schedule comes from the data: a
    settlement counts only when its step from the previous one repeats the step before it (a new interval is
    confirmed by two equal steps), the step is at most 8 h and the rate is known; the next settlement is overdue
    one step + 60 s after the last. Otherwise None. A step shorter than 8 h rescales its rate to 8 h."""
    seen = [(at, rate) for at, rate in settlements if at + FUNDING_DELAY_MS <= bar_close]
    if len(seen) < n + 2 or bar_close - seen[-1][0] >= seen[-1][0] - seen[-2][0] + FUNDING_DELAY_MS:
        return None  # each settlement needs two predecessors for its steps; an overdue one is a gap
    total = 0.0
    for k in range(len(seen) - n, len(seen)):
        at, rate = seen[k]
        step, before = at - seen[k - 1][0], seen[k - 1][0] - seen[k - 2][0]
        rate = float(rate) if rate is not None else math.nan
        if not known(rate) or not 0 < step <= FUNDING_STEP_MS + MINUTE or abs(step - before) > MINUTE:
            return None
        total += rate * (1.0 if abs(step - FUNDING_STEP_MS) <= MINUTE else FUNDING_STEP_MS / step)
    return total / n * 10000


def minute_spread(name, row, index_close, mark_close):
    """One swap minute's spread in bps over the index: the swap close (premium) or the mark close (basis_mark_bps); None
    without trades in the minute or without the closes it needs."""
    top = mark_close if name == "basis_mark_bps" else row[4]
    if len(row) > 5 and row[5] > 0 and known(index_close) and index_close > 0 and known(top):
        return (top / index_close - 1) * 10000
    return None


def secondary_bars(minutes, secondary, name, tf, until_ms):
    """[(bar_open_ms, mean bps or None)] of tf bars closed by until_ms: per minute 1e4 x (swap close / index close - 1)
    (premium) or 1e4 x (mark close / index close - 1) (basis_mark_bps); a minute needs the index close, the mark close
    for basis and swap volume > 0, and a bar needs every one of its minutes."""
    width = TIMEFRAMES[tf]
    index, mark = secondary.get("index") or {}, secondary.get("mark") or {}
    groups = {}
    for row in minutes:
        if row[0] + MINUTE > until_ms:
            continue
        groups.setdefault(row[0] - row[0] % width, []).append(minute_spread(name, row, index.get(row[0]), mark.get(row[0])))
    out = []
    for start in sorted(groups):
        values = groups[start]
        if start + width > until_ms:
            continue
        whole = len(values) == width // MINUTE and all(v is not None for v in values)
        out.append((start, sum(values) / len(values) if whole else None))
    return out


def positioning_value(spec, minutes, secondary):
    if not minutes:
        return None
    width, n = TIMEFRAMES[spec["tf"]], spec["n"]
    close = minutes[-1][0] + MINUTE  # the decision follows this minute's close
    if spec["name"] == "funding_settled_bps":
        return funding_value(secondary.get("funding") or [], close - close % width, n)
    until = lagged_until(spec, close)
    if until is None:
        return None
    return lagged_feature(spec, secondary_bars(minutes, secondary, spec["name"], spec["tf"], until), until)


def lagged_until(spec, close):
    """End of the lagged secondary bars a decision at `close` reads; None when the swap already shows a newer bar of
    this timeframe, so no rule mixes two bars."""
    width = TIMEFRAMES[spec["tf"]]
    until = close - SECONDARY_LAG_MS
    return None if width > MINUTE and until // width < close // width else until


def lagged_feature(spec, bars, until):
    """premium_bps / premium_change_bps / basis_mark_bps from the lagged bars [(open_ms, mean bps or None), ...] closed by
    `until`; the reference and the feature book share this code."""
    width, n = TIMEFRAMES[spec["tf"]], spec["n"]
    if not bars or bars[-1][0] != (until // width - 1) * width:
        return None  # the latest lagged bar itself is missing
    if spec["name"] == "premium_change_bps":
        if len(bars) < n + 1 or bars[-1][1] is None or bars[-1 - n][1] is None or bars[-1][0] - bars[-1 - n][0] != n * width:
            return None
        return bars[-1][1] - bars[-1 - n][1]
    window = bars[-n:]
    if len(window) < n or any(b[1] is None for b in window) or window[-1][0] - window[0][0] != (n - 1) * width:
        return None
    return sum(b[1] for b in window) / n


def evaluate(node, values):
    """(verdict, checks): verdict None when any leaf is UNKNOWN; no short circuit, like V2."""
    checks = []

    def visit(item):
        if "all" in item or "any" in item:
            key = "all" if "all" in item else "any"
            results = [visit(child) for child in item[key]]
            return None if any(r is None for r in results) else (all if key == "all" else any)(results)
        if "not" in item:
            result = visit(item["not"])
            return None if result is None else not result
        actual = values.get(feature_key(item["feature"]))
        if "ref" in item:
            ref = values.get(feature_key(item["ref"]))
            expected = ref * item["mult"] if known(ref) else None
        else:
            expected = item["value"]
        passed = OPS[item["op"]](actual, expected) if known(actual) and known(expected) else None
        checks.append({"feature": feature_key(item["feature"]), "op": item["op"], "actual": actual if known(actual) else None,
                       "expected": expected if known(expected) else None, "passed": passed})
        return passed

    return visit(node), checks


def limit_price(rules, close, values, tick, side=None):
    """Post-only limit for the signal close, rounded to the passive side of the tick; None if the offset is UNKNOWN."""
    side = side or rules["side"]
    offset = rules["order"]["offset"]
    bps = offset["bps"] if "bps" in offset else (values.get(feature_key(offset["of"])) or math.nan) * offset["mult"]
    if not known(bps) or bps <= 0:
        return None
    if side == "long":
        return math.floor(close * (1 - bps / 10000) / tick + 1e-9) * tick
    return math.ceil(close * (1 + bps / 10000) / tick - 1e-9) * tick
