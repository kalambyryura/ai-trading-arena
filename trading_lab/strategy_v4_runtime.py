"""Schema-v4 recipes (SDK v3) in the exact engines: the strategy_runtime interface, official D106 timing.

One rule decision per newly visible closed 1m bar, at the first snapshot at or after its close + 5 s; the entry is
rechecked at the next fresh quote; a taker entry fills there, a post-only one (D096, R2) rests from there at the limit
of the decision's bar until it fills or expires; one entry per position, no adds, no formal exit; the stop is live from the fill, the target from the next minute after the fill; max hold and cooldown
run as the engine runs them for V1. tools/team_eval_v4.py mode="official" simulates the same rules on candles and
additionally exits at the stop in a minute where both the stop and the target are reachable, because candles hide
the order of prices inside a minute; the engine sees that order on its quote path and needs no such rule.

Feature values come from the snapshot's feature book, snapshot["v4_features"] = {"bar_close_ms": close of the newest
closed 1m bar, "values": {feature key: value of strategy_v4.feature_value or None}}. A missing book, a bar that is
not yet visible or a book a whole bar behind the clock is UNKNOWN and never an entry.
"""
import math

from . import strategy_v4 as v4

VISIBILITY_SECONDS = 5  # D106: a bar is visible from the first quote at or after its close + 5 s
MAX_SPREAD_BPS = 2  # D106 risk parameter for Live; the model tapes quote one tick, so the evaluator never meets it
NO_ADDS_BPS = 500  # V1-shaped field the engines read; never reached, because max_entries is 1
NO_EXIT = {"mode": "all", "conditions": []}  # empty formal exit: arena_strategies.evaluate returns False
EXECUTED_INSTRUMENTS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP")  # G11, D152: the executor trades these
MAKER_FEE_RATE = .0002  # D096: the model's maker fee of a post-only fill (PAPER; Live books OKX's actual fee)
ADVERSE_SELECTION_RATE = .0002  # D096: adverse selection booked on every modelled post-only fill
MAKER_THROUGH_BPS = 1  # D096: a resting limit fills once the path passes it by max(1 bps of L, 2 ticks)


def enabled(recipe):
    return isinstance(recipe, dict) and recipe.get("strategy_schema_version") == 4


def engine_rules(rules):
    """The fields both engines read, from clean v4 rules (strategy_v4.validate_rules); v4 keys stay as they are."""
    return {"side": rules["side"], "size_pct": rules["size_pct"], "stop_loss_bps": rules["stop_loss_bps"],
            "take_profit_bps": rules["take_profit_bps"], "max_hold_seconds": rules["max_hold_seconds"],
            "cooldown_seconds": rules["cooldown_seconds"], "max_entries": 1, "add_after_bps": NO_ADDS_BPS,
            "max_spread_bps": MAX_SPREAD_BPS, "entry": rules["entry"], "exit": NO_EXIT, "order": rules["order"]}


def engine_recipe(clean):
    """An engine participant from a clean v4 recipe (strategy_v4.validate_payload): an executed instrument, taker or post-only."""
    if clean["instrument"] not in EXECUTED_INSTRUMENTS:
        raise v4.ContractError("instrument_executes_from_r2")
    return {"id": clean["recipe_sha256"][:16], "name": clean["name"], "author": clean["author"], "version": 1,
            "status": "ready", "strategy_schema_version": 4, "instrument": clean["instrument"],
            "description": clean["description"], "original_text": clean["original_text"], "confirmed": True,
            "rules": engine_rules(clean["rules"]), "recipe_sha256": clean["recipe_sha256"],
            "semantic_sha256": clean["semantic_sha256"], "execution_model_id": clean["execution_model_id"]}


def rules_intact(recipe):
    """True when the stored engine rules are exactly those the recipe's own validated payload gives with this code;
    recipe_sha256 covers only the payload, so this closes the gap between what is hashed and what the engines run."""
    try:
        return recipe["rules"] == engine_rules(v4.validate_payload(recipe["v4_payload"])["rules"])
    except (KeyError, TypeError, v4.ContractError):
        return False


def book(snapshot):
    """The visible bar's feature book, or None when it is absent, not visible yet or a whole bar behind the clock."""
    current = snapshot.get("v4_features")
    if not isinstance(current, dict) or not isinstance(current.get("values"), dict):
        return None
    close = current.get("bar_close_ms")
    if isinstance(close, bool) or not isinstance(close, int) or close % v4.MINUTE:
        return None
    age = snapshot["at"] - close / 1000
    if not VISIBILITY_SECONDS <= age < VISIBILITY_SECONDS + 60:
        return None
    return current


def decision_due(bot, snapshot):
    """A newly visible bar that became visible at or after the cooldown end and has not been decided yet."""
    current = book(snapshot)
    if current is None:
        return False
    close = current["bar_close_ms"]
    return close > (bot.get("_v4") or {}).get("decided_bar_ms", -1) and close / 1000 + VISIBILITY_SECONDS >= bot["_next_decision"]


def decided(bot, snapshot):
    current = book(snapshot)
    if current is not None:
        bot["_v4"] = {**(bot.get("_v4") or {}), "decided_bar_ms": current["bar_close_ms"]}


def entry(recipe, bot, snapshot, intent_id=None):
    """The side whose branch holds (strategy_v4.entry_side). A recheck (intent_id set) passes only for the side chosen at
    the decision, so side both never turns into the other side between the decision and the fill."""
    rules = recipe["rules"]
    fixed = None if rules["side"] == "both" else rules["side"]
    if bot.get("position_btc"):
        return {"allowed": False, "checks": [], "reason": "one_entry_per_position", "side": fixed}
    current = book(snapshot)
    if current is None:
        return {"allowed": False, "checks": [], "reason": "strategy_input_unknown", "side": fixed}
    side, checks, conflict = v4.entry_side(rules, current["values"])
    if side is not None and intent_id is not None and side != bot.get("_candidate_side"):
        return {"allowed": False, "checks": checks, "reason": "side_changed_before_fill", "side": side}
    unknown = any(c["passed"] is None for c in checks)
    reason = ("entry_rule_passed" if side else "side_conflict" if conflict else "strategy_input_unknown" if unknown
              else "entry_rule_failed")
    return {"allowed": side is not None, "checks": checks, "reason": reason, "side": side or fixed,
            "bar_close_ms": current["bar_close_ms"]}


def post_only(recipe):
    return enabled(recipe) and recipe["rules"]["order"]["type"] == "post_only"


def maker_limit(recipe, side, snapshot):
    """The post-only order of a decision (D096): the limit L of strategy_v4.limit_price from the close and the feature
    values of the bar the decision saw, rounded to the passive side of the tick, and its fill threshold. None when the
    bar, its close, the tick or the offset is UNKNOWN: then no order reaches the book."""
    current = book(snapshot)
    raw = snapshot.get("raw") or {}
    if current is None:
        return None
    try:
        tick = float(raw["instrument"][0]["tickSz"])
        close = next(float(r[4]) for r in raw["candles"] if int(r[0]) + v4.MINUTE == current["bar_close_ms"])
    except (KeyError, IndexError, TypeError, ValueError, StopIteration):
        return None
    limit = v4.limit_price(recipe["rules"], close, current["values"], tick, side) if tick > 0 else None
    if limit is None or not math.isfinite(limit) or limit <= 0:
        return None
    return {"limit": limit, "through": max(limit * MAKER_THROUGH_BPS / 10000, 2 * tick), "tick": tick,
            "bar_close_ms": current["bar_close_ms"], "timeout_seconds": recipe["rules"]["order"]["timeout_seconds"]}


def maker_crosses(plan, side, snapshot):
    """A post-only order that would take liquidity at placement is refused, never turned into a taker fill."""
    return snapshot["ask"] <= plan["limit"] if side == "long" else snapshot["bid"] >= plan["limit"]


def maker_filled(order, snapshot):
    """True when the path price (the venue's last trade; the model tapes' path price) passed the resting limit by the
    threshold. An unreadable price is no fill."""
    try:
        price = float(snapshot["raw"]["ticker"][0]["last"])
    except (KeyError, IndexError, TypeError, ValueError):
        return False
    if not math.isfinite(price):
        return False
    return price <= order["limit"] - order["through"] if order["side"] == "long" else price >= order["limit"] + order["through"]


def target_live(bot, now):
    """The target counts from the minute after the fill; the stop is live from the fill itself."""
    start = bot.get("_hold_start")
    return start is not None and math.floor(now / 60) > math.floor(start / 60)
