"""Pure accounting/rules for exchange-backed seasons. No transport or paper fills."""
from copy import deepcopy
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN, ROUND_UP
from pathlib import Path

from .arena_engine import emit, frame, iso, new_season, public_season, rounded
from .arena_strategies import ArenaError, evaluate, fingerprint
from .okx_readonly import decimal_value as D, numeric_text as N, PreflightError
from .cycle_probe import PLAN, TIMING, is_probe, lifecycle
from .match_protocol import draining_policy, install
from . import strategy_runtime as runtime
from . import strategy_v4_runtime as v4rt
from . import demo_execution
from . import exchange_execution

LIMITS = {"instrument": "BTC-USDT-SWAP", "leverage": 1, "margin": "isolated", "position_mode": "net",
          "decision_seconds": 30, "poll_seconds": 5, "maximum_quote_age_seconds": 10,
          "max_drawdown_pct": 10, "max_total_entries": 20, "max_participants": 6,
          "top_book_fraction": .1, "max_data_failures": 3, "recovery_seconds": 120,
          "max_order_intents_per_bot": 60, "max_observations": 420,
          "fill_model": "Фактичні OKX IOC/market виконання; не симуляція. STP cancel_taker.",
          "funding_model": "Фактичні підписані USDT pnl у bills subtype173/174; фінальна звірка.",
          "equity_model": "Виділений бюджет + фактичні PnL/fees/funding; відкрита позиція за bid/ask з оцінкою комісії виходу.",
          "ai_role": "Спільний Jev veto тільки для входу/докупівлі після формальних перевірок. Вихід виконує код."}


def season_instrument(strategies):
    """One contract per season (R2): a schema-4 recipe names its own, V1/V2 recipes trade BTC; a mixed season is refused."""
    found = {r.get("instrument") if v4rt.enabled(r) else LIMITS["instrument"] for r in strategies}
    if len(found) != 1 or not found <= set(v4rt.EXECUTED_INSTRUMENTS):
        raise ArenaError("Один інструмент на сезон: " + ", ".join(v4rt.EXECUTED_INSTRUMENTS) + "; змішаний сезон не допускається.")
    return found.pop()


MIXED = "MIXED"  # R2.1.12 (MIX): rules.instrument of a season whose bots trade different contracts


def bot_instruments(strategies):
    """R2.1.12 (MIX): {recipe id: contract} by the rule of season_instrument (schema 4 names its own, V1/V2 trade BTC)."""
    found = {r["id"]: r.get("instrument") if v4rt.enabled(r) else LIMITS["instrument"] for r in strategies if r.get("id") != "cash"}
    if not set(found.values()) <= set(v4rt.EXECUTED_INSTRUMENTS):
        raise ArenaError("Інструмент рецепта: лише " + ", ".join(v4rt.EXECUTED_INSTRUMENTS) + ".")
    return found


def season_instruments(strategies, context, execution_mode):
    """R2.1.12 (MIX, D148): one contract for a season, or a mixed season - only an owner's Live round in RULES_ONLY
    (the shared Jev and the Demo mechanics take one market observation for every bot)."""
    found = bot_instruments(strategies)
    if len(set(found.values())) == 1:
        return season_instrument(strategies)
    if context != "arena_live" or execution_mode != exchange_execution.RULES_ONLY:
        raise ArenaError("Змішаний сезон (боти на різних інструментах) — лише Live у режимі RULES_ONLY.")
    return MIXED


def instrument_for(state, bot_id):
    """R2.1.12 (MIX, plan check p. 2): the contract a bot trades, from the rules under config_sha256 (never the
    preflight summary, which is missing before initialize); journals before R2 traded BTC."""
    rules = state.get("rules") or {}
    if rules.get("instrument") == MIXED:
        return None if bot_id == "cash" else rules["instruments"][bot_id]
    return rules.get("instrument", LIMITS["instrument"])


def mixed(state):
    return (state.get("rules") or {}).get("instrument") == MIXED


def contract_block(state, bot):
    """R2.1.12 (MIX, D149 p. 3): the entry block of the bot's own contract in a mixed season, or None; read only."""
    return (state.get("admission_blocked_by_instrument") or {}).get(instrument_for(state, bot["id"])) if mixed(state) else None


def contracts(state):
    """R2.1.12 (MIX): the contracts of a mixed season, sorted; [None] for a one-contract season (its keys as before)."""
    return sorted(set(state["rules"]["instruments"].values())) if mixed(state) else [None]


def snapshot_files(folder, state):
    """R2.1.12 (MIX, plan check p. 13): the public snapshot journals, {None: one file} or {contract: its file} of a
    mixed season; the recorder, the ZIP, the debrief and the review read them from here."""
    folder = Path(folder)
    if mixed(state):
        return {i: folder / (state["id"] + ".snapshots." + i + ".jsonl") for i in contracts(state)}
    return {None: folder / (state["id"] + ".snapshots.jsonl")}


def create(strategies, account_ids, context, budget, duration, stamp, code_hash, execution_mode="shared_jev"):
    from .ikigai_g1 import enabled, G1Error
    if any(enabled(r) for r in strategies):
        raise G1Error("g1_paper_only_live_demo_forbidden")
    mechanics = exchange_execution.validate_request(execution_mode, context, strategies, budget, duration)
    if any(is_probe(r) for r in strategies) and (context != "workshop_demo" or
            budget != TIMING["budget_usdt"] or duration != TIMING["duration_seconds"] or len(strategies) != 1):
        raise PreflightError("diagnostic_requires_single_demo_100usdt_300s")
    state = new_season(strategies, budget, duration, stamp, code_hash)
    state.update(mode="OKX_DEMO" if context == "workshop_demo" else "OKX_LIVE", context=context,
                 execution_mode=execution_mode, rules=deepcopy(LIMITS), events=[], frames=[],
                 _ready=False, _recovery=False, _ticks=0, _event_seq=0, _closing_since=None, _failures=0)
    state["rules"]["mode"] = state["mode"]
    state["rules"]["instrument"] = season_instruments(strategies, context, execution_mode)  # BTC seasons keep their pre-R2 config hash
    if state["rules"]["instrument"] == MIXED:
        state["rules"]["instruments"] = bot_instruments(strategies)  # R2.1.12 (MIX): new keys only in a mixed season
    if context == "arena_live" and any(v4rt.enabled(r) for r in strategies):
        # G12: a schema-4 recipe has no final proof yet, so in the owner's Live it trades 1 lot ("test"), whatever its budget;
        # frozen V1/V2 bots of the same season keep their own size
        state["rules"]["test_live_max_lots"] = 1
    if any(v4rt.post_only(r) for r in strategies):
        state["rules"]["fill_model"] = ("Фактичні OKX IOC/post-only/market виконання; не симуляція. STP cancel_taker. Post-only (D096) "
            "стоїть до тайм-ауту рецепта; залишок скасовується, фактичні часткові заповнення лишаються власними й захищеними.")
    if mechanics:
        state["rules"].update(ai_role=demo_execution.AI_ROLE, max_total_entries=1)
    elif execution_mode == exchange_execution.RULES_ONLY:
        state["rules"]["ai_role"] = exchange_execution.AI_ROLE
    install(state, account_ids)
    if state["rules"]["instrument"] == MIXED:
        # R2.1.12 (MIX, plan check p. 9): the book of each contract on its own allowance, before config_sha256; no rules
        # key after the final install
        from .match_protocol import capacities
        books = {i: capacities(duration, [r for r in strategies if state["rules"]["instruments"][r["id"]] == i]).get("max_book_gets")
                 for i in sorted(set(state["rules"]["instruments"].values()))}
        state["rules"]["book_gets_by_instrument"] = {i: n for i, n in books.items() if n}
    state["config_sha256"] = fingerprint({"strategies": strategies, "accounts": account_ids,
        "budget": budget, "duration": duration, "rules": state["rules"],
        **({"execution_mode": execution_mode} if mechanics or execution_mode == exchange_execution.RULES_ONLY else {})})
    for index, bot in enumerate(state["bots"]):
        bot.update(funding_estimated=False, funding_reconciled=False, accounting_reconciled=False,
                   ai_requests=0, ai_cost_complete=True, _orders=[], _contracts="0", _funding="0",
                   _entry_attempts=0, _next_decision=stamp, _initialized=False,
                   _manual_review=False, _account_tag=None, _start_balance=None, _instrument=None)
        if bot["id"] != "cash":
            bot["account_id"] = account_ids[index]
        else:
            bot.update(status="CASH", funding_reconciled=True, accounting_reconciled=True)
    emit(state, stamp, None, 1, "exchange_reserved", state["mode"] + ": запуск власника збережено; спершу GET перевірки.",
         config_sha256=state["config_sha256"])
    frame(state, stamp, None)
    return state


STOP_CODES = frozenset({"owner_stop", "recovery", "worker_failed", "deadline", "observation_limit", "data_unavailable",
                        "window_closed_flat", "manual_review", "account_unavailable", "close_timeout", "protection_unverified",
                        "risk_limit", "close_only", "max_hold", "stop_loss", "take_profit", "rules_exit", "system"})


def stop(state, stamp, bot_id=None, reason="Зупинка власником.", origin="system", code=None):
    """code (R2.1.10): the fixed token of the reason, exported instead of the message (STOP_CODES)."""
    code = code if code in STOP_CODES else "owner_stop" if origin == "owner" else "recovery" if origin == "recovery" else "system"
    targets = [b for b in state["bots"] if b["id"] != "cash" and (bot_id is None or b["id"] == bot_id)]
    if not targets:
        raise PreflightError("unknown_bot")
    for bot in targets:
        first = not bot["stop_requested"]
        if first:
            bot["stop_requested"] = True
            bot["status"] = "CLOSING"
            bot["_close_reason"] = reason
            bot["_closing_since"] = stamp
            closing = ("закриваємо фактичний залишок." if D(bot.get("_contracts") or "0")
                       else "позиції немає — знімаємо власні заявки й звіряємо рахунок.")
            emit(state, stamp, bot, 6, "stop_requested", reason + " Нових входів немає; " + closing,
                 origin=origin, reason_code=code)  # owner: the owner's Stop; recovery: a Resume; system: deadline, risk or a fault
        elif origin in {"owner", "recovery"} and bot["status"] != "STOPPED":  # R2.1.10 (C): every change; R2.1.11: not a stopped bot
            emit(state, stamp, bot, 6, "stop_renewed", reason + " Закриття починається одразу.", origin=origin, reason_code=code)
        if first or origin in {"owner", "recovery"}:  # R2.1.7: a new Stop, an owner Stop or a Resume closes at once
            bot.pop("_close_retry_at", None)
            bot.pop("_close_refusals", None)
            bot.pop("_close_empty", None)
        bot["_approval"] = None
    if bot_id is None or all(b["stop_requested"] for b in state["bots"] if b["id"] != "cash"):
        state["status"] = "CLOSING"
        if state["_closing_since"] is None:
            state["_closing_since"] = stamp


def initialize(bot, check):
    bot.update(_initialized=True, _account_tag=check["account_tag"], _main_tag=check["main_tag"],
               _start_balance=check["balance"], _begin_ms=check["server_start_ms"],
               _instrument=check["instrument"], _fee_rate=check["taker_signed_rate"], status="WAITING")


def instrument_of(bot):
    """The bot's own contract from its preflight summary; journals from before R2 traded BTC only."""
    return (bot.get("_instrument") or {}).get("instrument") or "BTC-USDT-SWAP"


def contract_btc(bot):
    return D(bot["_instrument"]["contract_value"]) * D(bot["_instrument"]["contract_multiplier"])


# R2.1.15 (D168 p. 3, plan check p. 3): above 1x, the loss at the recipe's stop on the whole notional is at most this
# share of the bot's budget. A module constant, not LIMITS: LIMITS goes into every season's rules and its export.
STOP_NOTIONAL_BUDGET_PCT = 5


def leverage(bot):
    """The bot's leverage set at the Start; 1 for every journal from before R2.1.15."""
    return D(str(bot.get("leverage", 1)))


def keeps_resting(state, bot, order, stamp, close_only):
    """A post-only entry rests on the book until its expiry (D096), across the admission cutoff like any submitted order;
    Stop, an exit, recovery, blocked admission or manual review cancel it at once, as they would an IOC remainder."""
    return (order["request"]["ordType"] == "post_only" and not order["request"]["reduceOnly"]
            and stamp < order.get("expires_at", float("-inf")) and not close_only
            and not bot["stop_requested"] and not bot.get("_exit_requested") and not bot["_manual_review"]
            and not state.get("admission_blocked") and not contract_block(state, bot)
            and not bot.get("admission_blocked") and state["status"] in {"RUNNING", "DRAINING"})


def fill_read_lag(bot, position):
    """True when the venue's position exceeds the owned fills only by part of a resting post-only entry on its side: a
    fill the order's GET has not shown yet (it is re-read at the next check), never a foreign position."""
    remaining = Decimal(0)
    for order in unresolved(bot):
        if order["request"]["ordType"] == "post_only" and not order["request"]["reduceOnly"]:
            filled = D((order.get("result") or {}).get("contracts", "0"))
            remaining += (D(order["request"]["sz"]) - filled) * (1 if order["request"]["side"] == "buy" else -1)
    extra = position - D(bot["_contracts"])
    return bool(remaining) and bool(extra) and (extra > 0) == (remaining > 0) and abs(extra) <= abs(remaining)


def unresolved(bot):
    return [o for o in bot["_orders"] if not o.get("result") or o["result"]["state"] not in {"filled", "canceled", "mmp_canceled"}]


CLOSE_RETRY_SECONDS = 5  # R2.1.7: the pause before another close after one ended with nothing (refused, or no fill)
EMPTY_CLOSE_MAX_SECONDS = 40  # R2.1.7: the longest pause after accepted closes with no fill in a row (6 tries in 120 s)


RESUME_CLOSE_RESERVE = 20  # R2.1.8 (R217-F4C-R): close intents each owner Resume adds above the season's hard cap


def intent_count(bot):
    """R2.1.6: the order intents that count against the season's limits; an order the venue refused or never created
    does not (its client ID still takes a place in identity_for). A close the venue accepted and ended with no fill is a
    physical order and counts (the P0 hard cap of 60 holds within a run); each Resume adds RESUME_CLOSE_RESERVE close
    intents, so a position that a market matching nothing kept open can still be closed."""
    return sum(1 for o in bot["_orders"] if not o.get("not_created"))


def empty_close(order):
    result = order.get("result") or {}
    return (order["request"]["reduceOnly"] and not order.get("_protective_parent") and result.get("state") in {"canceled", "mmp_canceled"}
            and not D(result.get("contracts") or "0"))


def unseen_range(orders):
    """R2.1.6: (low, high) signed contracts by which the fills of these sent orders, not visible on the venue yet, may
    have moved the position."""
    low = high = Decimal(0)
    for order in orders:
        size = D(order["request"]["sz"])
        if order["request"]["side"] == "buy":
            high += size
        else:
            low -= size
    return low, high


def rebuild(bot):
    """Rebuild from cumulative exchange facts, so repeated reads cannot double count."""
    units, basis, fees, gross = Decimal(0), Decimal(0), Decimal(0), Decimal(0)
    entries, trades, hold_start, filled_entries = 0, 0, None, 0  # entries: of the open position; filled_entries: all
    for order in bot["_orders"]:
        result = order.get("result")
        if not result:
            continue
        filled, price = D(result["contracts"]), D(result["average_price"])
        fee, pnl = D(result["signed_fee_usdt"]), D(result["gross_pnl_usdt"])
        fees += fee
        gross += pnl
        if not filled:
            continue
        direction = Decimal(1) if order["request"]["side"] == "buy" else Decimal(-1)
        if order["request"]["reduceOnly"]:
            if not units or units * direction >= 0 or filled > abs(units):
                raise PreflightError("close_exceeds_owned_fill")
            units += direction * filled
            if not units:
                trades += 1
                basis, entries, hold_start = Decimal(0), 0, None
        else:
            if units * direction < 0 or pnl != 0:
                raise PreflightError("unexpected_entry_direction_or_pnl")
            basis = (abs(units) * basis + filled * price) / (abs(units) + filled)
            if not units:  # journals before R2.1 have only the local observation time
                hold_start = order.get("fill_venue_at", order.get("fill_observed_at", order["at"]))
            units += direction * filled
            entries += 1
            filled_entries += 1
    if not units:  # R2.1.7: nothing left to close, so no refusal pause carries over to the next position
        bot.pop("_close_retry_at", None)
        bot.pop("_close_refusals", None)
        bot.pop("_close_empty", None)
    bot.update(_contracts=N(units), position_btc=rounded(units * contract_btc(bot)), entry_price=rounded(basis),
               _hold_start=hold_start, entries=entries, trades=trades, filled_entry_orders=filled_entries, _gross=N(gross), _signed_fees=N(fees),
               fees_usdt=rounded(-fees), funding_usdt=rounded(-D(bot["_funding"])),
               # R2.1.11 (G2): the same facts under neutral names (position_btc holds the season coin, ETH in an ETH season;
               # entries counts the open position's entries; entries_total is filled_entry_orders under a new name)
               base_ccy=instrument_of(bot).split("-")[0], position_base=rounded(units * contract_btc(bot)),
               signed_fees_usdt=rounded(fees), entries_total=filled_entries)


def mark(state, bot, snapshot=None):
    if bot["id"] == "cash" or not bot["_initialized"]:
        return
    rebuild(bot)
    realized = D(bot.get("_gross", "0")) + D(bot.get("_signed_fees", "0")) + D(bot["_funding"])
    unrealized = D(str(bot.get("unrealized_pnl_usdt", 0)))
    units = D(bot["_contracts"])
    if not units:
        unrealized = Decimal(0)
    elif snapshot:
        qty = units * contract_btc(bot)
        price = D(str(snapshot["bid"] if qty > 0 else snapshot["ask"]))
        unrealized = qty * (price - D(str(bot["entry_price"]))) + abs(qty) * price * D(bot["_fee_rate"])
    bot.update(realized_pnl_usdt=rounded(realized), unrealized_pnl_usdt=rounded(unrealized),
               equity_usdt=rounded(D(str(state["budget_usdt"])) + realized + unrealized),
               pnl_usdt=rounded(realized + unrealized), _bank=rounded(D(str(state["budget_usdt"])) + realized))
    bot["return_pct"] = bot["pnl_usdt"] / state["budget_usdt"] * 100
    bot["_peak"] = max(bot["_peak"], bot["equity_usdt"])
    bot["max_drawdown_pct"] = max(bot["max_drawdown_pct"], (bot["_peak"] - bot["equity_usdt"]) / bot["_peak"] * 100)


def record_result(state, bot, order, result, stamp, seen_by=None):
    """seen_by names the path whose read showed this update (watch_slice, step_end, account_check, submit, cancel_check,
    closing, protection): a fill event says which path saw it first, with the reads of that resting entry so far."""
    previous = order.get("result")
    if previous and (D(previous["contracts"]) > D(result["contracts"])
                     or previous["ordId"] != result["ordId"]
                     or (previous["state"] in {"filled", "canceled", "mmp_canceled"} and previous["state"] != result["state"])):
        raise PreflightError("order_facts_regressed")
    if previous == result:
        return
    was_open = bool(D(bot.get("_contracts") or "0"))
    if D(result["contracts"]) > D((previous or {}).get("contracts", "0")):
        # the venue's time of the update that shows the fill (server-synchronised clock), never after we saw it nor
        # before we sent the order; hold and target count from the first one, as the evaluator counts from its fill
        venue = min(stamp, max(order["at"], int(D(result["updated_ms"])) / 1000))
        order.update(last_fill_observed_at=stamp, last_fill_venue_at=venue, last_fill_seen_by=seen_by)
        if "fill_observed_at" not in order:
            order.update(fill_observed_at=stamp, fill_venue_at=venue, fill_seen_by=seen_by)
    if D(result["contracts"]) > 0 and not order["request"]["reduceOnly"] and not bot.get("position_btc"):
        runtime.before_fill(bot)
    if not order["request"]["reduceOnly"] and D(result["contracts"]) > D((previous or {}).get("contracts", "0")):
        prior = D((previous or {}).get("contracts", "0")) * D((previous or {}).get("average_price", "0"))
        runtime.entry_notional(bot, float((D(result["contracts"]) * D(result["average_price"]) - prior) * contract_btc(bot)))
    order["result"] = deepcopy(result)
    if (order["request"]["reduceOnly"] and not order.get("_protective_parent") and result["state"] in {"canceled", "mmp_canceled"}
            and D(result["contracts"]) == D((previous or {}).get("contracts", "0")) and D(bot.get("_contracts") or "0")):
        # R2.1.7 (R216-F4-C): an accepted close that ended with no new fill spends one of the season's order intents, so it
        # waits a growing pause: 5 s, then twice as long, at most 40 s (a refused close, which spends none, waits 5 s)
        bot["_close_empty"] = bot.get("_close_empty", 0) + 1
        pause = min(EMPTY_CLOSE_MAX_SECONDS, CLOSE_RETRY_SECONDS * 2 ** (bot["_close_empty"] - 1))
        bot["_close_retry_at"] = max(bot.get("_close_retry_at", 0), stamp + pause)
    elif order["request"]["reduceOnly"] and D(result["contracts"]) > D((previous or {}).get("contracts", "0")):
        bot.pop("_close_empty", None)  # a close that fills ends the growing pause
    if order.get("_reduction"):
        delta = D(result["contracts"]) - D((previous or {}).get("contracts", "0"))
        bot["_reduce_remaining"] = str(max(Decimal(0), D(bot.get("_reduce_remaining", "0")) - delta))
    rebuild(bot)
    if result["state"] in {"filled", "canceled", "mmp_canceled"} and D(result["contracts"]) == 0 and not order["request"]["reduceOnly"]:
        recipe = next(r for r in state["strategies"] if r["id"] == bot["id"])
        runtime.terminal_no_fill(recipe, bot, order["request"]["clOrdId"])
    recipe = next(r for r in state["strategies"] if r["id"] == bot["id"])
    if order.get("_protective_parent") and was_open and not D(bot["_contracts"]):
        # R2.1.5: the owned stop on the venue closed the position. The recipe's cooldown, as closing() sets after its own
        # exit and as the evaluator and PAPER set after every exit: the next bar is no new entry.
        bot["_next_decision"] = max(bot.get("_next_decision", 0), stamp + recipe["rules"]["cooldown_seconds"])
    if (is_probe(recipe) and not order["request"]["reduceOnly"] and D(result["contracts"]) > 0
            and bot.get("_probe_entry_at") is None):
        bot["_probe_entry_at"] = stamp
    if bot["stop_requested"] or bot.get("_exit_requested"):
        bot["status"] = "CLOSING"
    elif unresolved(bot):
        bot["status"] = "ENTRY_PENDING"
    else:
        bot["status"] = "HOLDING" if bot["position_btc"] else "WAITING"
    step = 6 if order["request"]["reduceOnly"] else 3
    path = {} if seen_by is None else {"seen_by": seen_by, "resting_reads": order.get("_resting_reads", 0)}
    filled = D(result["contracts"]) > D((previous or {}).get("contracts", "0"))
    # R2.1.10 (G1): a read with no new fill (live, cancelled, a first read of 0) is exchange_order_read, never a fill
    # code; written events only, older ones are read as recorded
    emit(state, stamp, bot, step, ("exchange_close_fill" if step == 6 else "exchange_entry_fill") if filled else "exchange_order_read",
         "OKX: оновлено фактичне виконання ордера." if filled else "OKX: прочитано стан ордера; нового заповнення немає.",
         result=result, position_btc=bot["position_btc"], **path,
         order_state=result.get("state"), venue_updated_ms=result.get("updated_ms"))  # R2.1.10 (C): new keys


def record_not_created(state, bot, order, stamp, *, kind, code=None):
    """R2.1.5: an order the venue refused (kind "rejected", its sCode) or a send whose order never appeared (kind
    "absent"): terminal with no fill, never an unresolved order that pauses the season. An entry releases its runtime
    reservation; its attempt stays spent."""
    order["result"] = {"ordId": "", "clOrdId": order["request"]["clOrdId"], "state": "canceled", "contracts": "0",
                       "average_price": "0", "signed_fee_usdt": "0", "gross_pnl_usdt": "0", "updated_ms": str(int(stamp * 1000))}
    order["not_created"] = {"kind": kind, "code": code, "at": stamp}
    rebuild(bot)
    if not order["request"]["reduceOnly"]:
        recipe = next(r for r in state["strategies"] if r["id"] == bot["id"])
        runtime.terminal_no_fill(recipe, bot, order["request"]["clOrdId"])
    if bot["stop_requested"] or bot.get("_exit_requested"):
        bot["status"] = "CLOSING"
    elif unresolved(bot):
        bot["status"] = "ENTRY_PENDING"
    else:
        bot["status"] = "HOLDING" if bot["position_btc"] else "WAITING"


def gain_bps(bot, snapshot):
    if not bot["position_btc"]:
        return 0
    side = 1 if bot["position_btc"] > 0 else -1
    return side * ((snapshot["bid"] if side > 0 else snapshot["ask"]) / bot["entry_price"] - 1) * 10000


def exit_reason(state, bot, recipe, snapshot, stamp):
    if bot["stop_requested"]:
        return bot.get("_close_reason", "Зупинка.")
    if stamp >= state["_end"] and not draining_policy(state):
        return "Час сезону завершено."
    if bot["max_drawdown_pct"] >= LIMITS["max_drawdown_pct"]:
        return "Ліміт просадки або спроб входу."
    if is_probe(recipe):
        if state["context"] != "workshop_demo" or state["mode"] != "OKX_DEMO":
            raise PreflightError("diagnostic_demo_only")
        if bot.get("_probe_entry_at") is not None:
            if not bot["position_btc"] and not unresolved(bot):
                return "Технічний цикл завершено; повторних входів немає."
            if stamp - bot["_probe_entry_at"] >= TIMING["close_after_seconds"]:
                return "Технічний цикл: 120 с утримання завершено."
        elif bot["_entry_attempts"] and not unresolved(bot):
            return "Технічний IOC не дав входу; повторного ордера немає."
        elif stamp - state["_start"] >= TIMING["entry_window_seconds"]:
            return "Технічний цикл: за 90 с допустимий вхід не підтверджено."
    if not bot["position_btc"] or snapshot is None:
        return None
    gain, rules = gain_bps(bot, snapshot), recipe["rules"]
    if gain <= -rules["stop_loss_bps"]:
        return "Локальний stop-loss стратегії."
    if gain >= rules["take_profit_bps"] and (not v4rt.enabled(recipe) or v4rt.target_live(bot, stamp)):
        return "Ціль стратегії."
    hold_start = bot.get("_probe_entry_at", bot["_hold_start"]) if is_probe(recipe) else bot["_hold_start"]
    if rules["max_hold_seconds"] is not None and stamp - hold_start >= rules["max_hold_seconds"]:
        return "Час утримання вичерпано."
    passed, _ = runtime.exit_view(recipe, bot, snapshot) if runtime.enabled(recipe) else evaluate(rules["exit"], snapshot["features"])
    return "Формальна умова виходу." if passed else None


def candidate(state, bot, recipe, snapshot, stamp):
    from .ikigai_g1 import enabled, G1Error
    if enabled(recipe): raise G1Error("g1_paper_only_live_demo_forbidden")
    rules = recipe["rules"]
    v2 = runtime.declarative(recipe)  # V2 or schema 4: the recipe's own runtime gives the entry checks
    signal = runtime.entry(recipe, bot, snapshot) if v2 else None
    passed, checks = (signal["allowed"], signal["checks"]) if v2 else evaluate(rules["entry"], snapshot["features"])
    predicate_count = len(checks)
    exit_passed, exit_checks = runtime.exit_view(recipe, bot, snapshot) if v2 else evaluate(rules["exit"], snapshot["features"])
    add = bool(bot["position_btc"])
    diagnostic = is_probe(recipe)
    if diagnostic and (state["context"] != "workshop_demo" or state["mode"] != "OKX_DEMO"):
        raise PreflightError("diagnostic_demo_only")
    kind = "hold" if diagnostic and add and not bot.get("_probe_hold_attempted") else "add" if add else "entry"
    gates = {"spread_bps": (snapshot["features"]["spread_bps"], rules["max_spread_bps"], "lte"),
             "total_entries": (bot["_entry_attempts"], state["rules"].get("max_total_entries", LIMITS["max_total_entries"]), "lt"),
             "position_entries": (bot["entries"], rules["max_entries"], "lt")}
    for name, (actual, expected, op) in gates.items():
        checks.append({"feature": name, "op": op, "expected": expected, "actual": actual,
                       "passed": actual <= expected if op == "lte" else actual < expected})
    maker = None
    if v4rt.post_only(recipe) and not add:  # D096: the limit of this decision's bar, placed at the recheck
        maker = v4rt.maker_limit(recipe, signal["side"], snapshot)
        checks.append({"feature": "post_only_limit_usdt", "op": "gt", "expected": 0,
                       "actual": maker["limit"] if maker else None, "passed": maker is not None})
    if add and not diagnostic:
        gain = gain_bps(bot, snapshot)
        checks.append({"feature": "gain_bps", "op": "gte", "expected": rules["add_after_bps"],
                       "actual": gain, "passed": gain >= rules["add_after_bps"]})
    if diagnostic:
        delay = TIMING["hold_review_seconds"] if kind == "hold" else TIMING["add_after_seconds"]
        age = max(0, stamp - bot.get("_probe_entry_at", stamp))
        extra = [("probe_jev_requests", "lt", TIMING["max_jev_requests"], bot["ai_requests"]),
                 ("probe_entry_intents", "lt", TIMING["max_entry_intents"], bot["_entry_attempts"])]
        if add:
            extra.append(("position_age_seconds", "gte", delay, age))
        else:
            extra.append(("entry_window_age_seconds", "lt", TIMING["entry_window_seconds"], stamp - state["_start"]))
        if kind == "add":
            extra.append(("probe_hold_checked", "gte", 1, int(bot.get("_probe_hold_checked", False))))
        for feature, op, expected, actual in extra:
            checks.append({"feature": feature, "op": op, "expected": expected, "actual": actual,
                           "passed": actual < expected if op == "lt" else actual >= expected})
    bot["checks"] = checks
    allowed = (passed and all(c["passed"] is True for c in checks[predicate_count:])
               and not exit_passed and all(c["passed"] is not None for c in exit_checks)
               and not bot["stop_requested"] and state["status"] == "RUNNING" and stamp < state["_end"]
               and not unresolved(bot) and not bot["_manual_review"] and not state.get("admission_blocked")
               and not contract_block(state, bot) and not bot.get("admission_blocked") and not bot.get("entries_blocked")
               and (not bot.get("protection") or bot["protection"].get("verified") is True))
    side = signal["side"] if v2 else rules["side"]
    if allowed: bot["_candidate_side"] = side
    result = {"id": bot["id"], "recipe_sha256": recipe["recipe_sha256"], "features": snapshot["features"],
            "rules": deepcopy(rules), "checks": checks, "side": side, "kind": "add" if add else "entry",
            "position_btc": bot["position_btc"], "local_checks_passed": bool(allowed)}
    result["kind"] = kind
    if v2: result["signal_id"] = signal.get("signal_id")
    if maker: result["maker"] = maker
    if diagnostic:
        result.update(test_plan=PLAN, plan=deepcopy(TIMING))
    return result


def entry_body(state, bot, recipe, snapshot, identity, maker=None):
    """An IOC entry bounded 2 bps through the touch, or (schema 4, D096) a post-only order at the decision's limit
    `maker` that rests until it fills or expires; a post-only order that would cross the fresh book is never sent."""
    from .ikigai_g1 import enabled, G1Error
    if enabled(recipe): raise G1Error("g1_paper_only_live_demo_forbidden")
    rules, spec = recipe["rules"], bot["_instrument"]
    direction = runtime.side(recipe, bot) == "long"
    tick, lot, ct = D(spec["tick_size"]), D(spec["lot_step"]), contract_btc(bot)
    resting = v4rt.post_only(recipe)
    if resting:
        if maker is None:
            raise PreflightError("post_only_limit_unknown")
        if v4rt.maker_crosses(maker, "long" if direction else "short", snapshot):
            raise PreflightError("post_only_would_cross")
        price = (D(str(maker["limit"])) / tick).to_integral_value(rounding=ROUND_HALF_EVEN) * tick
    else:
        # IOC limit ceiling is bounded, with no unbounded market entry.
        raw_price = D(str(snapshot["ask"] if direction else snapshot["bid"])) * (Decimal("1.0002") if direction else Decimal(".9998"))
        price = (raw_price / tick).to_integral_value(rounding=ROUND_UP if direction else ROUND_DOWN) * tick
    # R2.1.15 (D168 p. 3): the margin is the budget's share as before; the notional is the margin times the leverage
    lever = leverage(bot)
    target = D(str(state["budget_usdt"])) * D(str(rules["size_pct"])) / 100 * lever
    if is_probe(recipe):
        if state["context"] != "workshop_demo" or state["mode"] != "OKX_DEMO":
            raise PreflightError("diagnostic_demo_only")
        target = min(target, D(str(TIMING["entry_notional_usdt"])))  # E041's notional caps hold at any leverage
    units = (target / price / ct / lot).to_integral_value(rounding=ROUND_DOWN) * lot
    lots = (state.get("rules") or {}).get("test_live_max_lots")  # the workshop's lot preview passes no season rules
    if lots and v4rt.enabled(recipe):  # G12: at most N minimum lots for a schema-4 bot
        units = min(units, D(spec["minimum_order_units"]) * lots)
    liquidity = D(str(snapshot["ask_btc"] if direction else snapshot["bid_btc"])) * Decimal(".1")
    qty = units * ct
    exposure = (abs(D(bot["_contracts"])) + units) * ct * price
    margin = exposure / lever  # 1x: the notional, as before
    if (units < D(spec["minimum_order_units"]) or units > 10 or (qty > liquidity and not resting)
            or margin + qty * price * abs(D(bot["_fee_rate"])) > D(str(bot["equity_usdt"])) * Decimal(".95")):
        raise PreflightError("minimum_lot_liquidity_or_exposure_veto")
    if lever > 1 and exposure * D(str(rules["stop_loss_bps"])) / 10000 > D(str(state["budget_usdt"])) * STOP_NOTIONAL_BUDGET_PCT / 100:
        raise PreflightError("stop_loss_notional_veto")  # D181 p. 1: the loss at the stop on the whole notional, its own reason
    body = {"instId": instrument_of(bot), "tdMode": "isolated", "posSide": "net",
            "side": "buy" if direction else "sell", "ordType": "post_only" if resting else "ioc", "sz": N(units), "px": N(price),
            "reduceOnly": False, "clOrdId": identity, "stpMode": "cancel_taker"}
    enforce_probe_order(state, bot, body)
    return body


def enforce_probe_order(state, bot, body):
    recipe = next(r for r in state["strategies"] if r["id"] == bot["id"])
    if not is_probe(recipe) or body["reduceOnly"]:
        return
    if state["context"] != "workshop_demo" or state["mode"] != "OKX_DEMO":
        raise PreflightError("diagnostic_demo_only")
    ct = contract_btc(bot)
    notional = D(body["sz"]) * ct * D(body["px"])
    spent = sum(D(o["request"]["sz"]) * ct * D(o["request"]["px"])
                for o in bot["_orders"] if not o["request"]["reduceOnly"])
    if (bot["_entry_attempts"] >= TIMING["max_entry_intents"] or notional > TIMING["entry_notional_usdt"]
            or spent + notional > TIMING["total_notional_usdt"]):
        raise PreflightError("diagnostic_notional_or_intent_cap")


def close_body(bot, identity, maximum_units=None):
    units = D(bot["_contracts"])
    if not units:
        raise PreflightError("no_owned_position_to_close")
    size = min(abs(units), D(maximum_units)) if maximum_units is not None else abs(units)
    if size <= 0: raise PreflightError("no_owned_reduction_remaining")
    return {"instId": instrument_of(bot), "tdMode": "isolated", "posSide": "net",
            "side": "sell" if units > 0 else "buy", "ordType": "market", "sz": N(size),
            "reduceOnly": True, "clOrdId": identity, "stpMode": "cancel_taker"}


def funding_total(bot, bills):
    funding = Decimal(0)
    for bill in bills:
        if bill.get("subType") not in {"173", "174"}:
            continue
        pnl = D(bill.get("pnl") or "0")
        if (D(bill.get("ts"), positive=True) < bot["_begin_ms"] or bill.get("ccy") != "USDT"
                or bill.get("instId") != instrument_of(bot) or D(bill.get("fee") or "0")
                or (bill["subType"] == "173" and pnl > 0) or (bill["subType"] == "174" and pnl < 0)):
            raise PreflightError("unowned_or_invalid_funding_bill")
        funding += pnl
    return funding


def settle(state, bot, bills, ending_balance):
    """Strict terminal reconciliation. Funding uses pnl, never isolated balChg."""
    if unresolved(bot) or D(bot["_contracts"]):
        raise PreflightError("unresolved_order_or_remaining_position")
    own = {o["result"]["ordId"] for o in bot["_orders"] if o.get("result") and D(o["result"]["contracts"])}
    found, funding, gross, fee = set(), funding_total(bot, bills), Decimal(0), Decimal(0)
    for bill in bills:
        if D(bill.get("ts"), positive=True) < bot["_begin_ms"] or bill.get("ccy") != "USDT":
            raise PreflightError("bill_window_or_currency_mismatch")
        pnl, signed_fee = D(bill.get("pnl") or "0"), D(bill.get("fee") or "0")
        subtype = bill.get("subType")
        if subtype in {"173", "174"}:
            if (bill.get("instId") != instrument_of(bot) or signed_fee != 0
                    or (subtype == "173" and pnl > 0) or (subtype == "174" and pnl < 0)):
                raise PreflightError("unowned_or_invalid_funding_bill")
        elif bill.get("ordId") in own and bill.get("instId") == instrument_of(bot) and bill.get("type") == "2":
            found.add(bill["ordId"])
            gross += pnl
            fee += signed_fee
        elif pnl or signed_fee or D(bill.get("balChg") or "0") or D(bill.get("posBalChg") or "0"):
            raise PreflightError("foreign_account_activity_in_bills")
    tolerance = Decimal(".000001")
    if found != own or abs(gross - D(bot.get("_gross", "0"))) > tolerance or abs(fee - D(bot.get("_signed_fees", "0"))) > tolerance:
        raise PreflightError("trade_bills_not_reconciled")
    expected = gross + fee + funding
    cash_delta = D(ending_balance["cash"]) - D(bot["_start_balance"]["cash"])
    if abs(cash_delta - expected) > tolerance or abs(D(ending_balance["equity"]) - D(ending_balance["cash"])) > tolerance:
        raise PreflightError("cash_delta_not_reconciled")
    bot.update(_funding=N(funding), funding_reconciled=True, accounting_reconciled=True,
               accounting={"source": state["mode"], "cash_delta_usdt": N(cash_delta),
                           "gross_pnl_usdt": N(gross), "signed_fees_usdt": N(fee),
                           "signed_funding_usdt": N(funding), "bill_count": len(bills),
                           "cash_delta_matches": True})
    mark(state, bot)


def public(state, *, compact=False):
    result = public_season(state, compact=compact)
    # Order requests/results are journal events, with no auth/UID fields. Raw account details stay private.
    result.pop("raw", None)
    return result
