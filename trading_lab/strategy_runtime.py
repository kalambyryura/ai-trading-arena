"""Bridge declarative V2 to both engines. One completed-bar advance, pure rechecks. Schema 4: strategy_v4_runtime."""
from . import strategy_v2 as v2
from . import strategy_v4_runtime as v4rt


def enabled(recipe):
    return recipe.get("strategy_schema_version") == 2


def declarative(recipe):
    """Entry checks come from the recipe's runtime (V2 or schema 4), not from the V1 condition group."""
    return enabled(recipe) or v4rt.enabled(recipe)


def observation(snapshot):
    return v2.packet(snapshot["bar_close_ms"], snapshot["sha256"], snapshot["at"],
                     snapshot["server_time"], snapshot["features"])


def values(bot, snapshot):
    qty = float(bot.get("position_btc") or 0)
    basis = float(bot.get("entry_price") or 0)
    close = snapshot["bid"] if qty > 0 else snapshot["ask"]
    if "_gross" not in bot: close *= .9998 if qty > 0 else 1.0002
    fee_rate = abs(float(bot.get("_fee_rate", .0005)))
    gross = (1 if qty > 0 else -1) * (close / basis - 1) * 10000 if basis and qty else 0
    first = bot.get("_hold_start")
    runtime = bot.get("_strategy_v2", {})
    origin = runtime.get("position") or {}
    costs = bot.get("_cycle_origin") or {}
    bank = float(bot.get("_bank", 0))
    realized = float(bot.get("_gross", 0))
    signed_fees = float(bot.get("_signed_fees", -bot.get("fees_usdt", 0)))
    funding = float(bot.get("_funding", bot.get("funding_usdt", 0)))
    # PAPER _bank already includes realized gross, fees and funding.
    if "_gross" not in bot:
        cycle_net = bank - costs.get("bank", bank)
        realized, signed_fees, funding = cycle_net, 0, 0
    net = v2.cycle_net_gain_bps(quantity_btc=qty, weighted_entry_price=basis, close_price=close,
        invested_notional_usdt=bot.get("_cycle_invested_notional", abs(qty) * basis), cycle_realized_gross_usdt=realized - costs.get("gross", 0),
        cycle_signed_fees_usdt=signed_fees - costs.get("fees", 0),
        cycle_signed_funding_usdt=funding - costs.get("funding", 0), exit_fee_rate=fee_rate) if qty and basis else None
    return {"position_age_seconds": max(0, snapshot["at"] - first) if first is not None else None,
            "gross_gain_bps": gross, "net_gain_bps": net,
            "entry_cost_hurdle_bps": origin.get("entry_cost_hurdle_bps"), "entries": bot.get("entries", 0)}


def before_fill(bot):
    if not bot.get("position_btc"):
        bot["_cycle_origin"] = {"bank": float(bot.get("_bank", 0)), "gross": float(bot.get("_gross", 0)),
                                "fees": float(bot.get("_signed_fees", -bot.get("fees_usdt", 0))),
                                "funding": float(bot.get("_funding", bot.get("funding_usdt", 0)))}
        bot["_cycle_invested_notional"] = 0


def entry_notional(bot, amount):
    bot["_cycle_invested_notional"] = bot.get("_cycle_invested_notional", 0) + amount


def advance(state, bot, recipe, snapshot, emit):
    if not enabled(recipe): return
    rules = recipe["rules"]
    runtime = bot.get("_strategy_v2") or v2.new_state(rules, recipe["recipe_sha256"])
    if bot.get("position_btc"):
        first = bot.get("_hold_start")
        runtime = v2.sync_position(runtime, str(bot["id"]) + ":" + str(first), first,
            "long" if bot["position_btc"] > 0 else "short",
            bot.get("_entry_cost_hurdle_bps"))
    elif runtime.get("position"):
        runtime = v2.sync_position(runtime, None)
    bot["_strategy_v2"] = runtime
    try:
        packet = observation(snapshot)
        current = values(bot, snapshot)
        formal_exit = v2.position_view(rules, runtime, snapshot["features"], current)["exit"] if runtime.get("position") else False
        exit_due = bool(bot.get("position_btc") and (formal_exit or v2.hold_expired(rules, current["position_age_seconds"])
            or current["gross_gain_bps"] <= -rules["stop_loss_bps"] or current["gross_gain_bps"] >= rules["take_profit_bps"]))
        runtime, traces = v2.advance_bar(rules, runtime, packet, current,
            entry_enabled=state["status"] == "RUNNING" and snapshot["at"] < state["_end"]
                and not bot["stop_requested"] and not state.get("admission_blocked"),
            checkpoint_enabled=not bot["stop_requested"] and state["status"] != "CLOSING" and not exit_due)
        bot["_strategy_v2"] = runtime
        bot.pop("_strategy_error", None)
        for trace in traces:
            emit(state, snapshot["at"], bot, 4 if bot.get("position_btc") else 1,
                 trace["code"], "Перехід формального стану Strategy V2.", **{k: v for k, v in trace.items() if k != "code"})
    except (v2.ContractError, KeyError):
        bot["_strategy_error"] = "completed_bar_unavailable_or_revised"


def entry(recipe, bot, snapshot, intent_id=None):
    if v4rt.enabled(recipe): return v4rt.entry(recipe, bot, snapshot, intent_id)
    if not enabled(recipe): raise ValueError("v2_only")
    runtime = bot.get("_strategy_v2")
    if not runtime or bot.get("_strategy_error"):
        return {"allowed": False, "checks": [], "reason": "strategy_input_unknown", "side": None}
    if bot.get("position_btc"):
        managed = v2.position_view(recipe["rules"], runtime, snapshot["features"], values(bot, snapshot))
        return {"allowed": managed["add"], "checks": managed["add_checks"],
                "reason": managed["reason"], "side": managed["side"]}
    try:
        return v2.entry_view(recipe["rules"], runtime, observation(snapshot), intent_id=intent_id)
    except (v2.ContractError, KeyError):
        return {"allowed": False, "checks": [], "reason": "strategy_input_unknown", "side": None}


def exit_view(recipe, bot, snapshot):
    if not bot.get("_strategy_v2") or bot.get("_strategy_error"):
        return False, []
    result = v2.position_view(recipe["rules"], bot["_strategy_v2"], snapshot["features"], values(bot, snapshot))
    return result["exit"], result["checks"]


def management(recipe, bot, snapshot):
    if not enabled(recipe) or not bot.get("_strategy_v2") or bot.get("_strategy_error"):
        return {"reduce": False, "reduce_pct": None}
    return v2.position_view(recipe["rules"], bot["_strategy_v2"], snapshot["features"], values(bot, snapshot))


def reserve_reduction(recipe, bot, snapshot, identity):
    bot["_strategy_v2"] = v2.consume_reduction(recipe["rules"], bot["_strategy_v2"], observation(snapshot), identity)


def side(recipe, bot):
    if bot.get("position_btc"):
        return "long" if bot["position_btc"] > 0 else "short"
    if v4rt.enabled(recipe):  # schema 4: the branch that passed at the decision (side both), else the fixed side
        return bot.get("_candidate_side") or recipe["rules"]["side"]
    return bot.get("_candidate_side") if enabled(recipe) else recipe["rules"]["side"]


def reserve(recipe, bot, snapshot, identity):
    if enabled(recipe) and not bot.get("position_btc"):
        candidate = entry(recipe, bot, snapshot)
        bot["_strategy_v2"] = v2.reserve_entry(recipe["rules"], bot["_strategy_v2"], candidate, identity)
        bot["_entry_cost_hurdle_bps"] = 2 * abs(float(bot.get("_fee_rate", .0005))) * 10000 + snapshot["features"]["spread_bps"] + (4 if "_gross" not in bot else 0)


def terminal_no_fill(recipe, bot, identity):
    runtime = bot.get("_strategy_v2")
    if enabled(recipe) and runtime and runtime.get("reservation"):
        bot["_strategy_v2"] = v2.finish_unfilled_intent(runtime, identity)
