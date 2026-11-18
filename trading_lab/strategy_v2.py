"""Pure Strategy V2 kernel. No transport, filesystem, model or orders.
advance_bar returns a NEW JSON state; persist it before candidates/Jev.
entry_view is a pure recheck; it MUST NOT arm, confirm or consume an episode.
V1 must keep its original evaluator and scheduling instead of being migrated.
"""
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import math
import operator

SEMANTICS = "strategy-v2-minimal-1"
CATALOGUE = "closed-1m-features-v1"
CANDLE_FEATURES = frozenset({"sma_gap_bps", "return_5m_bps", "return_1m_bps", "rsi_14",
                             "range_position_20", "volume_ratio_5", "atr_14_bps", "body_bps",
                             "upper_wick_bps", "lower_wick_bps", "bollinger_z_20", "ema_gap_bps", "volume_ratio_1",
                             "utc_hour", "utc_weekday"})  # clock of the completed bar: bar-scoped like candles
# Quote-scoped facts change inside a bar: direct entries, exits and position rules only, never temporal setup/confirm.
MARKET_FEATURES = CANDLE_FEATURES | {"spread_bps", "funding_rate_bps", "minutes_to_funding"}
POSITION_FEATURES = frozenset({"position_age_seconds", "gross_gain_bps", "net_gain_bps",
                               "entry_cost_hurdle_bps", "entries"})
OPS = {"gt": operator.gt, "gte": operator.ge, "lt": operator.lt, "lte": operator.le}
BOUNDS = {"size_pct": (5, 30), "max_entries": (1, 3), "add_after_bps": (20, 500),
          "stop_loss_bps": (20, 500), "take_profit_bps": (40, 1000),
          "cooldown_seconds": (15, 600), "max_spread_bps": (.1, 20)}
REDUCTION_KEYS = {"reduce_when", "reduce_pct"}
RULE_KEYS = set(BOUNDS) | {"max_hold_seconds", "side", "entry", "exit", "add_when", "cost_checkpoint"}


class ContractError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def known(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def bounded(value, low, high, integer=False):
    if not known(value) or not low <= value <= high or (integer and int(value) != value):
        raise ContractError("invalid_number")
    return value


def validate_predicate(node, allowed, *, optional=False):
    """Bounded all/any/not trees; no functions, arithmetic or arbitrary references."""
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
        if set(item) not in ({"feature", "op", "value"}, {"feature", "op", "ref"}):
            raise ContractError("predicate_leaf_shape")
        if item["feature"] not in allowed or item["op"] not in OPS:
            raise ContractError("predicate_feature_scope_or_operator")
        if "ref" in item:
            if item["ref"] not in allowed:
                raise ContractError("predicate_rhs_scope")
        else:
            bounded(item["value"], -1e9, 1e9)
        return deepcopy(item)

    if node is None and optional:
        return None
    return visit(node, 1)


def evaluate_predicate(node, values):
    """UNKNOWN propagates through EVERY branch, including any/not; no short circuit.

    The root verdict is authoritative. Flattened checks are evidence, not an
    all(checks) gate: a false leaf can belong to a successful any/not subtree.
    """
    checks = []

    def visit(item, path):
        if "all" in item or "any" in item:
            key = "all" if "all" in item else "any"
            results = [visit(child, f"{path}/{key}/{index}") for index, child in enumerate(item[key])]
            return None if any(value is None for value in results) else (all if key == "all" else any)(results)
        if "not" in item:
            result = visit(item["not"], path + "/not")
            return None if result is None else not result
        actual = values.get(item["feature"])
        expected = values.get(item["ref"]) if "ref" in item else item["value"]
        passed = OPS[item["op"]](actual, expected) if known(actual) and known(expected) else None
        checks.append({"path": path, "feature": item["feature"], "op": item["op"],
                       "expected": expected if known(expected) else None,
                       "actual": actual if known(actual) else None, "passed": passed,
                       **({"rhs_feature": item["ref"]} if "ref" in item else {})})
        return passed

    return (False, []) if node is None else (visit(node, ""), checks)


def validate_rules(rules):
    if not isinstance(rules, dict) or set(rules) not in (RULE_KEYS, RULE_KEYS | REDUCTION_KEYS):
        raise ContractError("v2_rule_keys")
    clean = {key: bounded(rules[key], *limits, integer=key in {"max_entries", "cooldown_seconds"})
             for key, limits in BOUNDS.items()}
    hold = rules["max_hold_seconds"]
    clean["max_hold_seconds"] = None if hold is None else bounded(hold, 30, 86400, integer=True)
    if clean["take_profit_bps"] <= 14 + clean["max_spread_bps"]:
        raise ContractError("take_profit_below_published_cost_model")
    sides = {"long"} if rules["side"] == "long" else {"short"} if rules["side"] == "short" else {
        "long", "short"} if rules["side"] == "both" else set()
    if not sides or not isinstance(rules["entry"], dict) or set(rules["entry"]) != sides:
        raise ContractError("v2_side_or_branches")
    clean.update(side=rules["side"], entry={})
    for side in sorted(sides):
        branch = rules["entry"][side]
        if not isinstance(branch, dict) or set(branch) != {"setup", "confirm", "invalidate", "expiry_bars"}:
            raise ContractError("entry_branch_shape")
        direct = branch["setup"] is None
        if direct and (branch["invalidate"] is not None or branch["expiry_bars"] is not None):
            raise ContractError("direct_branch_has_temporal_fields")
        if not direct:
            bounded(branch["expiry_bars"], 1, 60, integer=True)
        scope = MARKET_FEATURES if direct else CANDLE_FEATURES
        clean["entry"][side] = {"setup": validate_predicate(branch["setup"], CANDLE_FEATURES, optional=True),
                                "confirm": validate_predicate(branch["confirm"], scope),
                                "invalidate": validate_predicate(branch["invalidate"], CANDLE_FEATURES, optional=True),
                                "expiry_bars": branch["expiry_bars"]}
    scope = MARKET_FEATURES | POSITION_FEATURES
    clean["exit"] = validate_predicate(rules["exit"], scope, optional=True)
    clean["add_when"] = validate_predicate(rules["add_when"], scope, optional=True)
    clean["reduce_when"] = validate_predicate(rules.get("reduce_when"), scope, optional=True)
    reduction = rules.get("reduce_pct")
    if clean["reduce_when"] is None:
        if reduction is not None:
            raise ContractError("reduction_percentage_without_predicate")
        clean["reduce_pct"] = None
    else:
        clean["reduce_pct"] = bounded(reduction, 5, 95)
    checkpoint = rules["cost_checkpoint"]
    if checkpoint is None:
        clean["cost_checkpoint"] = None
    else:
        if not isinstance(checkpoint, dict) or set(checkpoint) != {"after_seconds", "when"}:
            raise ContractError("checkpoint_shape")
        clean["cost_checkpoint"] = {"after_seconds": bounded(checkpoint["after_seconds"], 1, 86400, integer=True),
                                    "when": validate_predicate(checkpoint["when"], scope)}
    return clean



def semantic_hash(rules):
    return digest({"semantics": SEMANTICS, "catalogue": CATALOGUE, "rules": rules})


def idle(consumed_bar_ms=None):
    return {"phase": "IDLE", "consumed_bar_ms": consumed_bar_ms}


def new_state(rules, recipe_sha256):
    return {"schema_version": 2, "semantic_sha256": semantic_hash(rules), "recipe_sha256": recipe_sha256,
            "last_bar_ms": None, "last_bar_input_sha256": None,
            "branches": {side: idle() for side in rules["entry"]}, "reservation": None, "position": None}


def bind(rules, state):
    if state.get("schema_version") != 2 or state.get("semantic_sha256") != semantic_hash(rules):
        raise ContractError("runtime_semantic_mismatch")


def packet(bar_ms, snapshot_sha256, observed_at, server_time, features):
    """bar_ms is COMPLETED bar CLOSE time; caller derives it from validated candles.

    Spread is quote scoped and excluded from the completed-bar input digest.
    Received-at and exchange server-time are different clock domains.
    """
    bounded(bar_ms, 60000, 1e15, integer=True)
    if bar_ms % 60000 or not known(server_time) or bar_ms / 1000 > server_time:
        raise ContractError("bar_not_completed")
    if not known(observed_at):
        raise ContractError("observed_at_unknown")
    candle_values = {key: features.get(key) if known(features.get(key)) else None for key in sorted(CANDLE_FEATURES)}
    return {"bar_ms": bar_ms, "snapshot_sha256": snapshot_sha256, "observed_at": observed_at,
            "bar_input_sha256": digest({"catalogue": CATALOGUE, "bar_ms": bar_ms, "features": candle_values}),
            "features": deepcopy(features)}


def advance_bar(rules, state, observation, position_values=None, *, entry_enabled=True, checkpoint_enabled=True):
    """One transition per NEW completed bar. Gap resets; same-bar revisions reject.

    A temporal setup at N confirms only at N+1..N+expiry_bars inclusive.
    Expiry/invalidation/UNKNOWN reset without rearming on that same bar.
    READY is usable only on its confirmation bar. RESERVED survives bars until
    actual fill or terminal no-fill; ACK/intent never create an open position.
    """
    bind(rules, state)
    result, trace = deepcopy(state), []
    current, previous = observation["bar_ms"], state["last_bar_ms"]
    if previous is not None and current < previous:
        raise ContractError("completed_bar_regressed")
    if current == previous:
        if observation["bar_input_sha256"] != state["last_bar_input_sha256"]:
            raise ContractError("completed_bar_revised")
        return result, trace
    gap = previous is not None and current != previous + 60000
    result.update(last_bar_ms=current, last_bar_input_sha256=observation["bar_input_sha256"])
    evidence = {"bar_ms": current, "snapshot_sha256": observation["snapshot_sha256"],
                "bar_input_sha256": observation["bar_input_sha256"]}
    trace.append({"code": "bar_observed", **evidence, "gap": gap})
    for side, plan in rules["entry"].items():
        branch = result["branches"][side]
        if branch["phase"] == "RESERVED":
            continue
        if gap or result["position"] or not entry_enabled:
            result["branches"][side] = idle(current)
            if gap:
                trace.append({"code": "setup_gap_reset", "side": side, **evidence})
            continue
        if plan["setup"] is None:
            result["branches"][side] = idle(branch.get("consumed_bar_ms"))
            continue
        before = branch["phase"]
        checks = []
        if before == "READY":
            branch = idle(current)
            code = "confirmation_expired"
        elif before == "ARMED":
            invalid, checks = evaluate_predicate(plan["invalidate"], observation["features"])
            confirmed, confirm_checks = evaluate_predicate(plan["confirm"], observation["features"])
            checks += confirm_checks
            if current > branch["expires_bar_ms"]:
                branch, code = idle(current), "setup_expired"
            elif invalid is None or confirmed is None:
                branch, code = idle(current), "setup_unknown_reset"
            elif invalid:
                branch, code = idle(current), "setup_invalidated"
            elif confirmed:
                branch = {**branch, "phase": "READY", "confirm_input": evidence}
                code = "setup_confirmed"
            else:
                code = "setup_waiting_confirmation"
        else:
            armed, checks = evaluate_predicate(plan["setup"], observation["features"])
            if armed is True:
                branch = {"phase": "ARMED", "armed_input": evidence,
                          "expires_bar_ms": current + int(plan["expiry_bars"]) * 60000,
                          "consumed_bar_ms": branch.get("consumed_bar_ms")}
                code = "setup_armed"
            else:
                code = "setup_unknown" if armed is None else "setup_waiting"
        result["branches"][side] = branch
        trace.append({"code": code, "side": side, "before": before, "after": branch["phase"],
                      **evidence, "checks": checks})
    position, checkpoint = result["position"], rules["cost_checkpoint"]
    if (position and checkpoint and checkpoint_enabled and not gap and position["checkpoint"]["status"] == "PENDING"
            and current / 1000 >= position["first_fill_at"] + checkpoint["after_seconds"]):
        # First new completed bar reaching age; duplicates cannot reconsider it.
        values = {**observation["features"], **(position_values or {}),
                  "entry_cost_hurdle_bps": position["entry_cost_hurdle_bps"]}
        verdict, checks = evaluate_predicate(checkpoint["when"], values)
        if verdict is not None:
            position["checkpoint"] = {"status": "CHECKED", "close": bool(verdict), **evidence, "checks": checks}
        trace.append({"code": "cost_checkpoint_unknown" if verdict is None else "cost_checkpoint_checked",
                      "position_id": position["id"], **evidence, "close": verdict, "checks": checks})
    return result, trace


def entry_view(rules, state, observation, *, entry_enabled=True, intent_id=None):
    """Pure candidate/fresh recheck. It never advances or mutates runtime state."""
    bind(rules, state)
    result = {"allowed": False, "side": None, "signal_id": None, "reason": "waiting", "checks": []}
    if not entry_enabled:
        return {**result, "reason": "admission_closed"}
    if state["position"]:
        return {**result, "reason": "position_open"}
    if state["reservation"] and (not intent_id or state["reservation"]["intent_id"] != intent_id):
        return {**result, "reason": "unresolved_reservation"}
    if (observation["bar_ms"] != state["last_bar_ms"]
            or observation["bar_input_sha256"] != state["last_bar_input_sha256"]):
        return {**result, "reason": "bar_not_advanced_or_revised"}
    matches, unknown = [], False
    for side, plan in rules["entry"].items():
        branch = state["branches"][side]
        reservation = state["reservation"]
        reserved_for_this_intent = bool(intent_id and reservation and reservation["intent_id"] == intent_id
                                        and reservation["side"] == side and reservation["bar_ms"] == observation["bar_ms"])
        if branch["phase"] == "RESERVED" and not reserved_for_this_intent:
            continue
        if branch.get("consumed_bar_ms") == observation["bar_ms"] and not reserved_for_this_intent:
            continue
        if plan["setup"] is not None:
            if branch["phase"] not in {"READY", "RESERVED"} or branch["confirm_input"]["bar_ms"] != observation["bar_ms"]:
                continue
        verdict, checks = evaluate_predicate(plan["confirm"], observation["features"])
        invalid, invalid_checks = evaluate_predicate(plan["invalidate"], observation["features"])
        result["checks"] += [{**check, "side": side} for check in checks + invalid_checks]
        unknown = unknown or verdict is None or invalid is None
        if verdict is True and invalid is False:
            matches.append(side)
    if unknown:
        return {**result, "reason": "unknown_predicate"}
    if len(matches) > 1:
        return {**result, "reason": "long_short_conflict"}
    if not matches:
        return result
    side = matches[0]
    return {**result, "allowed": True, "side": side, "reason": "entry_confirmed",
            "signal_id": digest([state["semantic_sha256"], side, observation["bar_ms"]])}


def reserve_entry(rules, state, candidate, intent_id):
    """Attach to durable intent atomically BEFORE write; never treats it as fill."""
    bind(rules, state)
    if not candidate["allowed"] or not isinstance(intent_id, str) or not intent_id:
        raise ContractError("invalid_reservation")
    result = deepcopy(state)
    reservation = {"signal_id": candidate["signal_id"], "side": candidate["side"],
                   "bar_ms": state["last_bar_ms"], "intent_id": intent_id}
    if state["reservation"]:
        if state["reservation"] == reservation:
            return result
        raise ContractError("unresolved_signal_reservation")
    result["reservation"] = reservation
    result["branches"][candidate["side"]]["phase"] = "RESERVED"
    result["branches"][candidate["side"]]["consumed_bar_ms"] = state["last_bar_ms"]
    return result


def finish_unfilled_intent(state, intent_id):
    """Only a terminal reconciled zero-fill result can release a reservation."""
    if not state["reservation"] or state["reservation"]["intent_id"] != intent_id:
        raise ContractError("terminal_intent_mismatch")
    result = deepcopy(state)
    result["branches"][state["reservation"]["side"]] = idle(state["last_bar_ms"])
    result["reservation"] = None
    return result


def sync_position(state, position_id, first_fill_at=None, side=None, entry_cost_hurdle_bps=None):
    """Actual first fill/flat transitions only. Adds/partials keep the same ID/age.

    Call with position_id=None only after actual zero remaining and reconciled
    intents. A missing position response is not a flat transition.
    """
    result = deepcopy(state)
    old = state["position"]
    if position_id is None:
        if old:
            result["position"] = None
            result["branches"] = {direction: idle(state["last_bar_ms"]) for direction in state["branches"]}
            result["reservation"] = None
        return result
    if not isinstance(position_id, str) or not position_id or not known(first_fill_at) or side not in {"long", "short"}:
        raise ContractError("position_identity_or_first_fill_unknown")
    if old:
        if old["id"] != position_id or old["first_fill_at"] != first_fill_at or old["side"] != side:
            raise ContractError("position_identity_or_first_fill_changed")
        return result
    result["position"] = {"id": position_id, "first_fill_at": first_fill_at, "side": side,
                          "entry_cost_hurdle_bps": entry_cost_hurdle_bps if known(entry_cost_hurdle_bps) else None,
                          "checkpoint": {"status": "PENDING"}, "reduction_ticket": None}
    result["branches"] = {direction: idle(state["last_bar_ms"]) for direction in state["branches"]}
    result["reservation"] = None
    return result



def position_view(rules, state, market_values, position_values):
    """Pure authored management flags; engine owns Stop/risk/SL/TP/size/hold.

    UNKNOWN management input blocks add; it never manufactures an exit. A due
    unknown checkpoint blocks add until a later new completed bar resolves it.
    """
    bind(rules, state)
    if not state["position"]:
        return {"exit": False, "reduce": False, "reduce_pct": None, "add": False, "reason": "flat", "checks": []}
    position = state["position"]
    values = {**market_values, **position_values, "entry_cost_hurdle_bps": position["entry_cost_hurdle_bps"]}
    exit_verdict, exit_checks = evaluate_predicate(rules["exit"], values)
    reduce_verdict, reduce_checks = evaluate_predicate(rules.get("reduce_when"), values)
    add_verdict, add_checks = evaluate_predicate(rules["add_when"], values)
    checked = position["checkpoint"]
    checkpoint_exit = checked["status"] == "CHECKED" and checked["close"]
    due_unknown = bool(rules["cost_checkpoint"] and checked["status"] == "PENDING"
                       and known(values.get("position_age_seconds"))
                       and values["position_age_seconds"] >= rules["cost_checkpoint"]["after_seconds"])
    close = exit_verdict is True or checkpoint_exit
    ticket = position.get("reduction_ticket")
    consumed = bool(ticket and ticket["bar_ms"] == state["last_bar_ms"])
    reduction = reduce_verdict is True and exit_verdict is False and not close and not due_unknown and not consumed
    return {"exit": close, "reduce": reduction, "reduce_pct": rules.get("reduce_pct") if reduction else None,
            "add": add_verdict is True and exit_verdict is False and reduce_verdict is False and not close and not due_unknown,
            "reason": "formal_exit" if exit_verdict is True else "cost_checkpoint" if checkpoint_exit else
                      "cost_checkpoint_pending" if due_unknown else "formal_reduce" if reduction else
                      "reduction_consumed" if consumed and reduce_verdict is True else "hold", "side": position["side"],
            "checks": exit_checks, "reduce_checks": reduce_checks, "add_checks": add_checks}



def hold_expired(rules, position_age_seconds):
    hold = rules["max_hold_seconds"]
    return hold is not None and known(position_age_seconds) and position_age_seconds >= hold


def cycle_net_gain_bps(*, quantity_btc, weighted_entry_price, close_price, invested_notional_usdt,
                       cycle_realized_gross_usdt, cycle_signed_fees_usdt, cycle_signed_funding_usdt,
                       exit_fee_rate):
    """As-of estimate of THIS cycle if remaining volume closes now.

    Bid/ask (and PAPER modeled execution slippage) are already in close_price:
    do not subtract spread a second time. Signed fees/funding include partial
    closes, exclude past cycles, and UNKNOWN stays UNKNOWN rather than zero.
    Jev USD is never folded into this USDT value without an explicit FX policy.
    """
    inputs = locals()
    if any(not known(value) for value in inputs.values()):
        return None
    if quantity_btc == 0 or min(weighted_entry_price, close_price, invested_notional_usdt) <= 0 or exit_fee_rate < 0:
        raise ContractError("invalid_position_cost_basis")
    values = {key: Decimal(str(value)) for key, value in inputs.items()}
    unrealized = values["quantity_btc"] * (values["close_price"] - values["weighted_entry_price"])
    exit_fee = abs(values["quantity_btc"]) * values["close_price"] * values["exit_fee_rate"]
    expected_net = (values["cycle_realized_gross_usdt"] + unrealized + values["cycle_signed_fees_usdt"]
                    + values["cycle_signed_funding_usdt"] - exit_fee)
    return float(expected_net / values["invested_notional_usdt"] * 10000)


def consume_reduction(rules, state, observation, intent_id):
    """One partial-reduction intent per position/completed bar, before order POST.

    Caller must first validate position_view.reduce plus owned quantity, lot,
    unresolved-order and risk gates. An uncertain ACK never releases a ticket;
    the next bar still requires no unresolved intent before another reduction.
    This pure ticket is neither an order nor evidence of a partial fill.
    """
    bind(rules, state)
    position = state["position"]
    if (not position or rules.get("reduce_when") is None or not isinstance(intent_id, str) or not intent_id
            or observation["bar_ms"] != state["last_bar_ms"]
            or observation["bar_input_sha256"] != state["last_bar_input_sha256"]):
        raise ContractError("invalid_reduction_reservation")
    ticket = {"id": digest([state["semantic_sha256"], position["id"], observation["bar_ms"], "reduce"]),
              "position_id": position["id"], "bar_ms": observation["bar_ms"], "intent_id": intent_id,
              "reduce_pct": rules["reduce_pct"]}
    existing = position.get("reduction_ticket")
    if existing and existing["bar_ms"] == observation["bar_ms"]:
        if existing == ticket:
            return deepcopy(state)
        raise ContractError("reduction_already_consumed_this_bar")
    result = deepcopy(state)
    result["position"]["reduction_ticket"] = ticket
    return result
