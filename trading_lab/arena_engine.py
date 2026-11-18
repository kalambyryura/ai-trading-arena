"""Bounded virtual execution and accounting. Never imports a live executor or keys."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
import uuid

from .arena_strategies import ArenaError, evaluate, fingerprint
from .match_protocol import TIME_POLICY, close_admission, draining_policy, install, public_phase
from . import strategy_runtime as runtime
from . import strategy_v4_runtime as v4rt
from . import ikigai_g1 as g1

RULES = {"mode": "PAPER", "instrument": "BTC-USDT-SWAP", "leverage": 1,
         "fee_bps_per_side": 5, "slippage_bps_per_side": 2,
         "decision_seconds": 30, "poll_seconds": 5, "minimum_latency_seconds": 1,
         "max_drawdown_pct": 10, "max_total_entries": 20, "max_participants": 6,
         "top_book_fraction": .1, "max_data_failures": 3, "recovery_seconds": 120,
         "funding_model": "Остання спостережена ставка й mid перед плановим funding; модель, не фактичне списання OKX.",
         "fill_model": "Наступне свіже котирування після рішення; spread + 2 bps проти позиції, 5 bps комісії на бік. Вхід повністю або пропуск; вихід може бути частковим. До 10% видимого найкращого рівня на учасника.",
         "equity_model": "Капітал після realized PnL, комісій і модельного funding; відкрита позиція за очікуваною ціною закриття з комісією.",
         "ai_role": "Правила авторів; під час сезону Jev та інші моделі не викликаються.",
         "ai_cost_usd": 0}
COLORS = ["#536c38", "#ba693e", "#6b6cad", "#218482", "#a75580", "#847344", "#758477"]


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="milliseconds")


def dec(value):
    return Decimal(str(value))


def rounded(value):
    return float(dec(value).quantize(Decimal("0.0000000001")))


def quantity(notional, price, lot):
    return float((dec(notional) / dec(price) / dec(lot)).to_integral_value(rounding=ROUND_DOWN) * dec(lot))


def cash_strategy():
    return {"id": "cash", "name": "Скриня", "version": 1, "status": "ready",
            "author": {"name": "Контрольний учасник", "kind": "baseline"},
            "description": "Тримає стартовий капітал в одиниці обліку USDT. Без угод і доходу.",
            "original_text": "Кошти не рухаються; орієнтир нульової дохідності в USDT.", "rules": None,
            "recipe_sha256": fingerprint({"rule": "cash", "version": 1})}


def emit(state, stamp, bot, step, code, message, **data):
    seq = state.get("_event_seq", state["events"][-1]["seq"] if state["events"] else 0) + 1
    state["_event_seq"] = seq
    state["events"].append({"seq": seq, "at": iso(stamp),
                            "bot_id": bot["id"] if bot else None, "step": step,
                            "code": code, "message": message, "data": data})
    if bot and code != "financial_observation":
        bot["last_reason"] = message


def cancel_entry(state, bot, stamp=None):
    pending = bot.get("_pending")
    if pending and pending["kind"] == "entry":
        recipe = next(r for r in state["strategies"] if r["id"] == bot["id"])
        if g1.enabled(recipe): g1.cancel(state, bot, recipe, "CORE_CANCEL")
        else: runtime.terminal_no_fill(recipe, bot, pending.get("signal_intent"))
        bot["_pending"] = None
    resting = bot.get("_maker")
    if resting:  # a resting post-only order: Stop, risk and data faults cancel it; the cutoff alone does not
        recipe = next(r for r in state["strategies"] if r["id"] == bot["id"])
        runtime.terminal_no_fill(recipe, bot, resting["signal_intent"])
        bot["_maker"] = None
        if stamp is not None:
            emit(state, stamp, bot, 3, "entry_order_cancelled", "Віртуальну post-only заявку скасовано без виконання.", simulated=True)


def new_season(strategies, budget, duration, now, code_hash):
    if duration > 18000 and any(g1.enabled(r) for r in strategies):
        raise g1.G1Error("g1_pilot_admission_max_5h")
    for recipe in strategies:
        if g1.enabled(recipe): g1.assert_recipe(recipe)
    state = {"id": uuid.uuid4().hex, "mode": "PAPER", "status": "RUNNING", "created_at": iso(now),
             "started_at": iso(now), "end_at": iso(now + duration), "duration_seconds": duration,
             "budget_usdt": budget, "rules": deepcopy(RULES), "strategies": deepcopy(strategies) + [cash_strategy()],
             "code_sha256": code_hash, "bots": [], "events": [], "frames": [], "latest_market": None,
             "error": None, "incomplete": False, "public_get_count": 0, "_start": now, "_end": now + duration,
             "_last": None, "_failures": 0, "_funding_applied": [], "_ticks": 0}
    if any(v4rt.post_only(r) for r in strategies):  # seasons without post-only recipes keep the rules text as before
        state["rules"]["fill_model"] += (" Post-only схеми 4 (D096): ліміт від закриття бару рішення стоїть із повторної перевірки до "
            "тайм-ауту рецепта; виконання за лімітом, щойно остання ціна пройде його на max(1 bps, 2 тики); maker 2 bps + 2 bps "
            "несприятливого відбору; заявку, що перетнула б книгу, не виставлено.")
    state["config_sha256"] = fingerprint({"strategies": state["strategies"], "budget": budget, "duration": duration, "rules": RULES})
    for i, strategy in enumerate(state["strategies"]):
        state["bots"].append({"id": strategy["id"], "strategy_id": strategy["id"], "name": strategy["name"],
             "author": strategy["author"], "avatar_kind": strategy["author"]["kind"], "color": COLORS[i],
             "status": "WAITING", "equity_usdt": budget, "pnl_usdt": 0, "return_pct": 0,
             "realized_pnl_usdt": 0, "unrealized_pnl_usdt": 0, "fees_usdt": 0, "funding_usdt": 0,
             "funding_estimated": True, "ai_cost_usd": 0, "max_drawdown_pct": 0,
             "position_btc": 0, "entry_price": 0, "entries": 0, "trades": 0,
             "last_reason": "Очікуємо спільні котирування.", "checks": [], "stop_requested": False, "curve": [],
             "_bank": budget, "_peak": budget, "_pending": None, "_next_decision": now,
             "_total_entries": 0, "_hold_start": None})
    install(state)
    state["config_sha256"] = fingerprint({"strategies": state["strategies"], "budget": budget,
        "duration": duration, "rules": state["rules"], "time_policy": TIME_POLICY})
    emit(state, now, None, 1, "season_started", "PAPER-сезон створено. Рецепти й умови зафіксовані.",
         config_sha256=state["config_sha256"], ai_requests=0)
    frame(state, now, None)
    return state


def close_price(bot, snapshot):
    return snapshot["bid"] * .9998 if bot["position_btc"] > 0 else snapshot["ask"] * 1.0002


def mark(state, bot, snapshot):
    qty = dec(bot["position_btc"])
    unrealized = Decimal(0)
    if qty:
        price = dec(close_price(bot, snapshot))
        unrealized = qty * (price - dec(bot["entry_price"])) - abs(qty) * price * Decimal(".0005")
    bot["unrealized_pnl_usdt"] = rounded(unrealized)
    bot["equity_usdt"] = rounded(dec(bot["_bank"]) + unrealized)
    bot["realized_pnl_usdt"] = rounded(dec(bot["_bank"]) - dec(state["budget_usdt"]))
    bot["pnl_usdt"] = rounded(dec(bot["equity_usdt"]) - dec(state["budget_usdt"]))
    bot["return_pct"] = bot["pnl_usdt"] / state["budget_usdt"] * 100
    bot["_peak"] = max(bot["_peak"], bot["equity_usdt"])
    bot["max_drawdown_pct"] = max(bot["max_drawdown_pct"], (bot["_peak"] - bot["equity_usdt"]) / bot["_peak"] * 100)


def frame(state, stamp, snapshot):
    # Long matches keep every event; chart/replay frames sample time plus actual fills.
    interval = state["rules"].get("chart_interval_seconds", 0)
    previous = state["frames"][-1] if state["frames"] else None
    transitions = {"paper_entry_fill", "paper_close_fill", "exchange_entry_fill", "exchange_close_fill",
                   "stop_requested", "admission_closed", "paused", "season_done", "season_finished", "closing_paused"}
    changed = False
    if previous:  # events only grow, in seq order: look back to the previous frame, not over the whole season
        for event in reversed(state["events"]):
            if event["seq"] <= previous["event_seq"]:
                break
            if event["code"] in transitions:
                changed = True
                break
    if (previous and interval and stamp - state.get("_frame_stamp", 0) < interval
            and not changed and state["status"] not in {"DONE", "PAUSED"}):
        return
    bots = []
    for bot in state["bots"]:
        if state.get("protocol_version", 0) >= 8:
            financial = {k: deepcopy(bot.get(k)) for k in ("position_btc", "equity_usdt", "pnl_usdt", "trades")}
            financial.update(signed_fees_usdt=bot.get("_signed_fees", -bot.get("fees_usdt", 0)),
                signed_funding_usdt=bot.get("_funding", bot.get("funding_usdt", 0)),
                known_jev_usd=bot.get("ai_cost_usd") if bot.get("ai_cost_complete", True) else None)
            financial["gross_usdt"] = bot.get("_gross", bot.get("_bank", state["budget_usdt"]) - state["budget_usdt"] + bot.get("fees_usdt", 0) - bot.get("funding_usdt", 0))
            emit(state, stamp, bot, 4, "financial_observation", "Фінансовий стан на момент спостереження.",
                 financial=financial, settled=bool(bot.get("accounting_reconciled", state["mode"] == "PAPER" and state["status"] == "DONE")))
        bot["curve"].append({"at": iso(stamp), "equity_usdt": bot["equity_usdt"]})
        bots.append({k: deepcopy(v) for k, v in bot.items() if not k.startswith("_") and k != "curve"})
    state["frames"].append({"at": iso(stamp), "bots": bots,
                            "market": {"mid": snapshot["mid"]} if snapshot else None,
                            "event_seq": state.get("_event_seq", len(state["events"]))})
    state["_frame_stamp"] = stamp


def request_stop(state, stamp, identity=None, origin="system"):
    if state["status"] == "DONE":
        raise ArenaError("Сезон уже завершено.")
    targets = [b for b in state["bots"] if identity is None or b["id"] == identity]
    if not targets:
        raise ArenaError("Учасника не знайдено.")
    for bot in targets:
        if bot["stop_requested"]:
            continue
        bot["stop_requested"] = True
        cancel_entry(state, bot, stamp)
        bot["status"] = "CLOSING" if bot["position_btc"] else "STOPPED"
        emit(state, stamp, bot, 6, "stop_requested", "Зупинка: нових входів не буде. Закриваємо віртуальний залишок.",
             position_btc=bot["position_btc"], origin=origin)
    if identity is None:
        state["status"] = "CLOSING"
        state.setdefault("_closing_since", stamp)


def pause(state, stamp, message):
    request_stop(state, stamp)
    state["status"] = "PAUSED"
    state["error"] = message
    state["incomplete"] = True
    emit(state, stamp, None, 1, "paused", message)
    frame(state, stamp, state["_last"])


def failed_snapshot(state, stamp):
    state["_failures"] += 1
    state["error"] = "Котирування недоступні або не пройшли перевірку; нових входів немає."
    emit(state, stamp, None, 1, "market_unavailable", state["error"], consecutive_failures=state["_failures"])
    # A pre-outage buy intention must never execute when connectivity returns.
    for bot in state["bots"]:
        cancel_entry(state, bot, stamp)
    if stamp >= state["_end"] and state["status"] == "RUNNING":
        if draining_policy(state):
            close_admission(state, stamp, emit)
        else:
            request_stop(state, stamp)
    if state["_failures"] >= RULES["max_data_failures"]:
        if state.get("protocol_version", 0) >= 8:
            state["admission_blocked"] = "market_unavailable"
            state["incomplete"] = True
        else:
            pause(state, stamp, "Стрічка перервалася. Сезон призупинено; відкриті віртуальні позиції збережено. Відновлення виконає лише закриття.")


def intent(state, bot, snapshot, kind, reason, reduce_pct=None):
    if bot["_pending"]:
        if kind == "close" and bot["_pending"]["kind"] == "reduce":
            bot["_pending"] = None  # Full strategy/risk exit supersedes an unfinished authored reduction.
        else:
            return
    identity = "paper-" + str(state.get("_event_seq", len(state["events"])) + 1)
    recipe = next(r for r in state["strategies"] if r["id"] == bot["id"])
    target = None
    if kind == "reduce":
        target = quantity(abs(bot["position_btc"]) * reduce_pct / 100, 1, snapshot["lot_btc"])
        if target < snapshot["min_btc"] or target >= abs(bot["position_btc"]):
            emit(state, snapshot["at"], bot, 5, "reduction_veto", "Частковий обсяг не проходить мінімальний лот; позицію збережено.")
            return
        runtime.reserve_reduction(recipe, bot, snapshot, identity)
    if kind == "entry":
        if g1.enabled(recipe): g1.reserve(state, bot, recipe, snapshot, identity)
        else: runtime.reserve(recipe, bot, snapshot, identity)
    bot["_pending"] = {"kind": kind, "at": snapshot["at"], "quote_ts": snapshot["quote_ts"], "reason": reason, "signal_intent": identity}
    if kind == "entry" and v4rt.post_only(recipe):  # D096: the limit comes from the decision's bar, placed at the recheck
        bot["_pending"]["maker"] = v4rt.maker_limit(recipe, runtime.side(recipe, bot), snapshot)
    if target is not None: bot["_pending"]["remaining_target_btc"] = target
    if kind in {"close", "reduce"}:
        bot["status"] = "CLOSING"
    emit(state, snapshot["at"], bot, 6 if kind in {"close", "reduce"} else 2, "close_intent" if kind in {"close", "reduce"} else "entry_intent",
         reason + " Виконання — на наступному свіжому котируванні.", snapshot_sha256=snapshot["sha256"], simulated=True)


def entry_gates(state, bot, recipe, snapshot, intent_id=None):
    if g1.enabled(recipe):
        return g1.entry_gates(state, bot, recipe, snapshot, intent_id)
    rules = recipe["rules"]
    signal = runtime.entry(recipe, bot, snapshot, intent_id) if runtime.declarative(recipe) else None
    passed, checks = (signal["allowed"], signal["checks"]) if signal else evaluate(rules["entry"], snapshot["features"])
    if passed and signal: bot["_candidate_side"] = signal["side"]
    checks += [
        {"feature": "spread_bps", "op": "lte", "expected": rules["max_spread_bps"],
         "actual": snapshot["features"]["spread_bps"], "passed": snapshot["features"]["spread_bps"] <= rules["max_spread_bps"]},
        {"feature": "total_entries", "op": "lt", "expected": RULES["max_total_entries"],
         "actual": bot["_total_entries"], "passed": bot["_total_entries"] < RULES["max_total_entries"]},
    ]
    bot["checks"] = checks
    return (passed and all(c["passed"] is True for c in checks[-2:]) and not bot["stop_requested"]
            and state["status"] == "RUNNING" and snapshot["at"] < state["_end"]
            and bot["entries"] < rules["max_entries"] and not state.get("admission_blocked"))


def fill_pending(state, bot, recipe, snapshot):
    pending = bot["_pending"]
    if not pending or snapshot["quote_ts"] <= pending["quote_ts"] or snapshot["at"] < pending["at"] + RULES["minimum_latency_seconds"]:
        return
    if pending["kind"] == "entry":
        if state.get("execution_mode") == "shared_jev" and pending.get("jev_approved") is not True:
            return  # Persisted intent alone is never permission to bypass the common Jev gate.
        bot["_pending"] = None
        g1_plan = None
        if g1.enabled(recipe):
            g1_plan = g1.consume_fill_plan(state, bot, recipe, snapshot, pending.get("signal_intent"))
            if g1_plan["kind"] != "OFFLINE_ENTRY_PLAN":
                emit(state, snapshot["at"], bot, 3, "entry_cancelled", "G1: повторна перевірка відхилила вхід.", reason=g1_plan.get("reason"), simulated=True)
                return
            runtime.before_fill(bot)
            sign = 1 if g1_plan["side"] == "long" else -1
            price, qty = g1_plan["entry_model_price"], g1_plan["base_quantity"]
        else:
            rules = recipe["rules"]
            add_allowed = True
            exit_passed, exit_checks = False, []
            if bot["position_btc"]:
                exit_passed, exit_checks = runtime.exit_view(recipe, bot, snapshot) if runtime.enabled(recipe) else evaluate(rules["exit"], snapshot["features"])
                direction = 1 if bot["position_btc"] > 0 else -1
                gain_bps = direction * (close_price(bot, snapshot) / bot["entry_price"] - 1) * 10000
                add_allowed = (rules["add_after_bps"] <= gain_bps < rules["take_profit_bps"]
                               and (rules["max_hold_seconds"] is None or snapshot["at"] - bot["_hold_start"] < rules["max_hold_seconds"])
                               and not exit_passed and all(c["passed"] is not None for c in exit_checks))
            if not entry_gates(state, bot, recipe, snapshot, pending.get("signal_intent")) or not add_allowed:
                runtime.terminal_no_fill(recipe, bot, pending.get("signal_intent"))
                emit(state, snapshot["at"], bot, 1, "entry_cancelled", "Повторна перевірка перед виконанням не пройдена.", checks=bot["checks"], exit_checks=exit_checks)
                if exit_passed:
                    intent(state, bot, snapshot, "close", "Умова виходу має пріоритет над докупівлею.")
                return
            if v4rt.post_only(recipe):
                place_maker(state, bot, recipe, snapshot, pending)
                return
            runtime.before_fill(bot)
            sign = 1 if runtime.side(recipe, bot) == "long" else -1
            price = snapshot["ask"] * 1.0002 if sign > 0 else snapshot["bid"] * .9998
            qty = quantity(state["budget_usdt"] * recipe["rules"]["size_pct"] / 100, price, snapshot["lot_btc"])
        liquidity = snapshot["ask_btc"] if sign > 0 else snapshot["bid_btc"]
        if qty < snapshot["min_btc"] or qty > liquidity * RULES["top_book_fraction"]:
            runtime.terminal_no_fill(recipe, bot, pending.get("signal_intent"))
            emit(state, snapshot["at"], bot, 3, "entry_no_fill", "Модель не виконує вхід: мінімальний лот або недостатня видима ліквідність.", simulated=True)
            return
        fee = dec(qty) * dec(price) * Decimal(".0005")
        # Reserve current exposure plus new entry and fee inside the participant's own equity.
        if (abs(dec(bot["position_btc"])) + dec(qty)) * dec(price) + fee > dec(bot["equity_usdt"]) * Decimal(".95"):
            runtime.terminal_no_fill(recipe, bot, pending.get("signal_intent"))
            emit(state, snapshot["at"], bot, 1, "risk_veto", "Власного капіталу учасника недостатньо для експозиції.")
            return
        old_qty = abs(dec(bot["position_btc"]))
        emit(state, snapshot["at"], bot, 1, "entry_rechecked", "Свіжі умови перевірено перед модельним виконанням.",
             checks=bot["checks"], kind="add" if old_qty else "entry", simulated=True,
             snapshot_sha256=snapshot["sha256"])
        bot["entry_price"] = rounded((old_qty * dec(bot["entry_price"]) + dec(qty) * dec(price)) / (old_qty + dec(qty)))
        bot["position_btc"] = rounded(dec(bot["position_btc"]) + sign * dec(qty))
        runtime.entry_notional(bot, float(dec(qty) * dec(price)))
        bot["fees_usdt"] = rounded(dec(bot["fees_usdt"]) + fee)
        bot["_bank"] = rounded(dec(bot["_bank"]) - fee)
        bot["entries"] += 1
        bot["_total_entries"] += 1
        if bot["_hold_start"] is None:
            bot["_hold_start"] = snapshot["at"]
        bot["status"] = "LONG" if sign > 0 else "SHORT"
        if g1_plan is not None: g1.on_entry_fill(bot, recipe, snapshot, g1_plan, qty, price)
        emit(state, snapshot["at"], bot, 3, "paper_entry_fill", "Віртуальний вхід виконано." if bot["entries"] == 1 else "Віртуальну докупівлю виконано.",
             side="long" if sign > 0 else "short", quantity_btc=qty, price_usdt=price, fee_usdt=float(fee),
             snapshot_sha256=snapshot["sha256"], simulated=True)
    else:
        available = snapshot["bid_btc"] if bot["position_btc"] > 0 else snapshot["ask_btc"]
        max_qty = quantity(available * RULES["top_book_fraction"], 1, snapshot["lot_btc"])
        target = dec(pending["remaining_target_btc"]) if pending["kind"] == "reduce" and not bot["stop_requested"] else abs(dec(bot["position_btc"]))
        qty = min(abs(dec(bot["position_btc"])), dec(max_qty), target)
        if qty <= 0:
            emit(state, snapshot["at"], bot, 6, "close_no_fill", "Закриття очікує ліквідності; залишок не обнулено.", simulated=True)
            return
        price = dec(close_price(bot, snapshot))
        sign = 1 if bot["position_btc"] > 0 else -1
        fee = qty * price * Decimal(".0005")
        gross = qty * sign * (price - dec(bot["entry_price"]))
        bot["_bank"] = rounded(dec(bot["_bank"]) + gross - fee)
        bot["fees_usdt"] = rounded(dec(bot["fees_usdt"]) + fee)
        bot["position_btc"] = rounded(dec(bot["position_btc"]) - qty * sign)
        if g1.enabled(recipe): g1.on_close_fill(bot, recipe, snapshot)
        emit(state, snapshot["at"], bot, 6, "paper_close_fill", "Віртуальну позицію закрито." if not bot["position_btc"] else "Часткове віртуальне закриття; очікуємо решту.",
             quantity_btc=float(qty), price_usdt=float(price), fee_usdt=float(fee), gross_pnl_usdt=float(gross),
             remaining_btc=bot["position_btc"], reason=pending["reason"], snapshot_sha256=snapshot["sha256"], simulated=True)
        if bot["position_btc"] and pending["kind"] == "reduce" and not bot["stop_requested"] and target <= qty:
            bot["_pending"] = None
            bot["status"] = "LONG" if bot["position_btc"] > 0 else "SHORT"
            bot["_next_decision"] = snapshot["at"] + RULES["decision_seconds"]
        elif bot["position_btc"]:
            bot["_pending"] = {**pending, "quote_ts": snapshot["quote_ts"], "at": snapshot["at"]}
            if pending["kind"] == "reduce": bot["_pending"]["remaining_target_btc"] = float(target - qty)
        else:
            bot["_pending"] = None
            bot["status"] = "STOPPED" if bot["stop_requested"] else "WAITING"
            bot["entries"] = 0
            bot["entry_price"] = 0
            bot["_hold_start"] = None
            bot["trades"] += 1
            bot["_next_decision"] = snapshot["at"] + recipe["rules"]["cooldown_seconds"]


def apply_funding(state, snapshot):
    previous = state["_last"]
    if not previous:
        return
    realized = snapshot.get("funding_accounting") == "realized-events-v2"
    if realized:
        events = snapshot.get("funding_events", [])
    elif previous.get("funding_at") is not None and previous.get("funding_rate") is not None:
        events = [{"at": previous["funding_at"], "rate": previous["funding_rate"],
                   "funding_time_ms": previous["funding_time_ms"]}]
    else:
        return
    for event in events:
        key, rate = event["funding_time_ms"], event["rate"]
        if not previous["at"] < event["at"] <= snapshot["at"] or key in state["_funding_applied"]:
            continue
        if rate is None:
            state["incomplete"] = True
            emit(state, snapshot["at"], None, 4, "funding_unknown", "Немає фактичної ставки модельного нарахування; результат неповний.",
                 funding_time_ms=key)
        else:
            for bot in state["bots"]:
                payment = -dec(bot["position_btc"]) * dec(previous["mid"]) * dec(rate)
                if bot["position_btc"]:
                    bot["funding_usdt"] = rounded(dec(bot["funding_usdt"]) + payment)
                    bot["_bank"] = rounded(dec(bot["_bank"]) + payment)
                    emit(state, snapshot["at"], bot, 4, "model_funding", "Нараховано модельний funding; ціна оцінки модельна.",
                         signed_amount_usdt=float(payment), rate=rate, funding_time_ms=key,
                         price_usdt=previous["mid"], estimated=True,
                         accounting_model="realized-events-v2" if realized else "observed-forecast-v1")
        state["_funding_applied"].append(key)


def place_maker(state, bot, recipe, snapshot, pending):
    """D096 post-only entry at the recheck: the decision's limit rests from this quote until it fills or expires. An
    order that would cross the book, one below the minimum lot or beyond the bot's equity is never placed."""
    plan, side = pending.get("maker"), runtime.side(recipe, bot)
    qty = quantity(state["budget_usdt"] * recipe["rules"]["size_pct"] / 100, plan["limit"], snapshot["lot_btc"]) if plan else 0
    reason = ("post_only_limit_unknown" if plan is None else "minimum_lot" if qty < snapshot["min_btc"]
              else "post_only_would_cross" if v4rt.maker_crosses(plan, side, snapshot)
              else "risk_veto" if qty * plan["limit"] * (1 + v4rt.MAKER_FEE_RATE) > bot["equity_usdt"] * .95 else None)
    if reason:
        runtime.terminal_no_fill(recipe, bot, pending.get("signal_intent"))
        emit(state, snapshot["at"], bot, 3, "entry_order_rejected", "Post-only заявку не виставлено; модельної угоди немає.",
             reason=reason, limit_usdt=plan and plan["limit"], snapshot_sha256=snapshot["sha256"], simulated=True)
        return
    bot["_maker"] = {**plan, "side": side, "quantity": qty, "placed_at": snapshot["at"],
                     "expires_at": snapshot["at"] + plan["timeout_seconds"], "signal_intent": pending.get("signal_intent")}
    emit(state, snapshot["at"], bot, 3, "entry_order_placed", "Віртуальна post-only заявка стоїть за лімітом бару сигналу.",
         side=side, limit_usdt=plan["limit"], quantity_btc=qty, timeout_seconds=plan["timeout_seconds"],
         snapshot_sha256=snapshot["sha256"], simulated=True)


def maker_step(state, bot, recipe, snapshot):
    """A resting post-only order (D096): it fills at its limit when the path passes it by the threshold, from the quote
    after placement up to its expiry; after the expiry it is cancelled, and the next decision waits for a bar that is
    visible from the expiry on."""
    order = bot.get("_maker")
    if not order or snapshot["at"] <= order["placed_at"]:
        return
    if snapshot["at"] > order["expires_at"]:
        runtime.terminal_no_fill(recipe, bot, order["signal_intent"])
        bot["_maker"] = None
        bot["_next_decision"] = order["expires_at"]
        emit(state, snapshot["at"], bot, 3, "entry_order_expired", "Post-only заявку скасовано за тайм-аутом без виконання.",
             limit_usdt=order["limit"], simulated=True)
        return
    if not v4rt.maker_filled(order, snapshot):
        return
    bot["_maker"] = None
    sign, qty, price = 1 if order["side"] == "long" else -1, order["quantity"], order["limit"]
    fee = dec(qty) * dec(price) * dec(v4rt.MAKER_FEE_RATE)
    adverse = dec(qty) * dec(price) * dec(v4rt.ADVERSE_SELECTION_RATE)
    runtime.before_fill(bot)
    bot["entry_price"] = rounded(price)
    bot["position_btc"] = rounded(sign * dec(qty))
    runtime.entry_notional(bot, float(dec(qty) * dec(price)))
    bot["fees_usdt"] = rounded(dec(bot["fees_usdt"]) + fee)
    bot["_bank"] = rounded(dec(bot["_bank"]) - fee - adverse)
    bot["entries"] += 1
    bot["_total_entries"] += 1
    bot["_hold_start"] = snapshot["at"]
    bot["status"] = "LONG" if sign > 0 else "SHORT"
    emit(state, snapshot["at"], bot, 3, "paper_entry_fill", "Віртуальну post-only заявку виконано за лімітом.",
         side=order["side"], quantity_btc=qty, price_usdt=price, fee_usdt=float(fee), adverse_selection_usdt=float(adverse),
         order_type="post_only", snapshot_sha256=snapshot["sha256"], simulated=True)


def tick(state, snapshot):
    if any(g1.enabled(r) for r in state["strategies"]): g1.assert_paper(state)
    if state["status"] not in {"RUNNING", "DRAINING", "CLOSING"}:
        raise ArenaError("Сезон не виконується.")
    now = snapshot["at"]
    previous = state["_last"]
    if previous and (now <= previous["at"] or snapshot["quote_ts"] < previous["quote_ts"]):
        raise ArenaError("Час ринкової стрічки пішов назад.")
    if previous and now - previous["at"] > 90:
        state["incomplete"] = True
        if state.get("protocol_version", 0) >= 8:
            state["admission_blocked"] = "data_gap"
            for bot in state["bots"]:
                cancel_entry(state, bot, now)
        else:
            request_stop(state, now)
        emit(state, now, None, 1, "data_gap", "Понад 90 секунд без спостереження; результат неповний.")
    elif state.get("admission_blocked") in {"market_unavailable", "data_gap"}:
        state.pop("admission_blocked", None)
    state["_ticks"] += 1
    state["_failures"] = 0
    state["error"] = None
    apply_funding(state, snapshot)
    if now >= state["_end"] and state["status"] == "RUNNING":
        if draining_policy(state):
            close_admission(state, now, emit)
        else:
            request_stop(state, now)
    strategies = {s["id"]: s for s in state["strategies"]}
    for bot in state["bots"]:
        if bot["id"] == "cash":
            bot["last_reason"] = "Тримає стартовий капітал. Без угод."
            continue
        recipe = strategies[bot["strategy_id"]]
        rules = recipe["rules"]
        mark(state, bot, snapshot)
        if bot["max_drawdown_pct"] >= RULES["max_drawdown_pct"]:
            if not bot["stop_requested"]:
                request_stop(state, now, bot["id"])
                emit(state, now, bot, 4, "risk_limit", "Досягнуто ліміт просадки або кількості входів.")
        if bot["stop_requested"] and bot["_pending"] and bot["_pending"]["kind"] == "entry":
            cancel_entry(state, bot)
        if bot["position_btc"] and bot["_pending"] and bot["_pending"]["kind"] == "reduce":
            change = (1 if bot["position_btc"] > 0 else -1) * (close_price(bot, snapshot) / bot["entry_price"] - 1) * 10000
            formal, _ = runtime.exit_view(recipe, bot, snapshot) if runtime.enabled(recipe) else evaluate(rules["exit"], snapshot["features"])
            if (bot["stop_requested"] or formal or change <= -rules["stop_loss_bps"] or change >= rules["take_profit_bps"]
                    or rules["max_hold_seconds"] is not None and now - bot["_hold_start"] >= rules["max_hold_seconds"]):
                bot["_pending"] = {**bot["_pending"], "kind": "close", "reason": "Повний вихід має пріоритет над частковим."}
        if g1.enabled(recipe): g1.advance(state, bot, recipe, snapshot, emit)
        fill_pending(state, bot, recipe, snapshot)
        maker_step(state, bot, recipe, snapshot)
        mark(state, bot, snapshot)
        runtime.advance(state, bot, recipe, snapshot, emit)
        if state["status"] == "DRAINING" and not bot["position_btc"] and not bot["_pending"] and not bot.get("_maker"):
            bot.update(status="STOPPED", stop_requested=True)
            continue
        if g1.enabled(recipe) and bot["position_btc"]:
            reason = g1.exit_reason(state, bot, recipe, snapshot)
            if reason:
                emit(state, now, bot, 5, "exit_trigger", "G1: правило повного виходу.", exit_reason=reason, snapshot_sha256=snapshot["sha256"], simulated=True)
                intent(state, bot, snapshot, "close", reason)
            continue  # No generic bps stop/target/add/reduce fallback for G1.
        if bot["position_btc"]:
            sign = 1 if bot["position_btc"] > 0 else -1
            change = sign * (close_price(bot, snapshot) / bot["entry_price"] - 1) * 10000
            target = change >= rules["take_profit_bps"] and (not v4rt.enabled(recipe) or v4rt.target_live(bot, now))
            reason = ("Зупинка учасника." if bot["stop_requested"] else
                      "Досягнуто stop стратегії." if change <= -rules["stop_loss_bps"] else
                      "Досягнуто ціль стратегії." if target else
                      "Вичерпано час утримання." if rules["max_hold_seconds"] is not None and now - bot["_hold_start"] >= rules["max_hold_seconds"] else None)
            if reason:
                if bot["_pending"] and bot["_pending"]["kind"] == "entry":
                    cancel_entry(state, bot)
                emit(state, now, bot, 5, "exit_trigger", "Спостережено правило повного виходу.",
                     exit_reason="owner_stop" if bot["stop_requested"] else "stop_loss" if change <= -rules["stop_loss_bps"] else "take_profit" if target else "max_hold",
                     snapshot_sha256=snapshot["sha256"], observed_at=now, bar_time_ms=snapshot.get("bar_close_ms"),
                     position_btc=bot["position_btc"], position_age_seconds=now-bot["_hold_start"], features=snapshot["features"])
                intent(state, bot, snapshot, "close", reason)
            elif now >= bot["_next_decision"] and not bot["_pending"]:
                exit_passed, exit_checks = runtime.exit_view(recipe, bot, snapshot) if runtime.enabled(recipe) else evaluate(rules["exit"], snapshot["features"])
                can_add = (entry_gates(state, bot, recipe, snapshot) and change >= rules["add_after_bps"]
                           and all(c["passed"] is not None for c in exit_checks))
                emit(state, now, bot, 4, "position_checks", "Перевірено відкриту позицію.", checks=bot["checks"], exit_checks=exit_checks,
                     kind="add", local_checks_passed=bool(can_add and not exit_passed), return_bps=change, snapshot_sha256=snapshot["sha256"])
                emit(state, now, bot, 5, "rules_decision", "Рішення правил: " + ("закрити." if exit_passed else "докупити." if can_add else "утримувати."),
                     choice="close" if exit_passed else "add" if can_add else "hold", checks=bot["checks"], ai_used=False)
                if exit_passed:
                    emit(state, now, bot, 5, "exit_trigger", "Спостережено формальну умову виходу.", exit_reason="rules_exit", exit_checks=exit_checks,
                         snapshot_sha256=snapshot["sha256"], observed_at=now, bar_time_ms=snapshot.get("bar_close_ms"),
                         position_btc=bot["position_btc"], position_age_seconds=now-bot["_hold_start"], features=snapshot["features"])
                    intent(state, bot, snapshot, "close", "Умова виходу стратегії.")
                elif runtime.management(recipe, bot, snapshot).get("reduce"):
                    intent(state, bot, snapshot, "reduce", "Формальне часткове закриття.", runtime.management(recipe, bot, snapshot)["reduce_pct"])
                elif can_add:
                    intent(state, bot, snapshot, "entry", "Підтверджена умова докупівлі у прибутку.")
                bot["_next_decision"] = now + RULES["decision_seconds"]
        elif not bot["stop_requested"] and state["status"] == "RUNNING" and (
                v4rt.decision_due(bot, snapshot) if v4rt.enabled(recipe) else now >= bot["_next_decision"]) and not bot["_pending"] and not bot.get("_maker"):
            allowed = entry_gates(state, bot, recipe, snapshot)
            emit(state, now, bot, 1, "entry_checks", "Перевірено вхідні умови.", checks=bot["checks"],
                 kind="entry", local_checks_passed=allowed, snapshot_sha256=snapshot["sha256"])
            emit(state, now, bot, 2, "rules_decision", "Рішення правил: вхід." if allowed else "Очікуємо: умови входу ще не виконані.", ai_used=False)
            if allowed:
                intent(state, bot, snapshot, "entry", "Умови формального рецепта виконано.")
            if v4rt.enabled(recipe):
                v4rt.decided(bot, snapshot)  # D106: one decision per newly visible bar, not the 30 s grid
            else:
                bot["_next_decision"] = now + RULES["decision_seconds"]
    state["_last"] = {k: deepcopy(v) for k, v in snapshot.items() if k != "raw"}
    state["latest_market"] = {"at": iso(now), "bid": snapshot["bid"], "ask": snapshot["ask"], "mid": snapshot["mid"],
                               "features": snapshot["features"], "snapshot_sha256": snapshot["sha256"]}
    all_stopped = all(b["stop_requested"] for b in state["bots"] if b["id"] != "cash")
    if all_stopped and not any(b["position_btc"] or b["_pending"] or b.get("_maker") for b in state["bots"]):
        state["status"] = "DONE"
        state["finished_at"] = iso(now)
        for bot in state["bots"]:
            bot["status"] = "DONE" if bot["id"] == "cash" else "STOPPED"
        emit(state, now, None, 6, "season_finished", "Сезон завершено. Віртуальних відкритих позицій немає.", incomplete=state["incomplete"])
    frame(state, now, snapshot)
    state["phase"] = public_phase(state)
    if state["status"] == "CLOSING" and now - state.get("_closing_since", now) >= RULES["recovery_seconds"]:
        pause(state, now, "Закриття не завершилося за 120 секунд. Залишок збережено; потрібне відновлення закриття.")
    if state["_ticks"] >= state["rules"].get("max_observations", 900) and state["status"] not in {"DONE", "PAUSED"}:
        if state.get("protocol_version", 0) >= 8:
            state["admission_blocked"] = "capacity"
            if state["status"] == "RUNNING": close_admission(state, now, emit)
        else:
            pause(state, now, "Вичерпано ліміт спостережень сезону. Залишок збережено.")


def public_season(state, *, compact=False):
    def clean(value):
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items() if not key.startswith("_")}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value
    projected = state if not compact else {**state, "events": state["events"][-500:]}
    result = clean(projected)
    result["events_count"] = state.get("_event_seq", len(state["events"]))
    result["history_complete"] = not compact and not state.get("_history_compact", False)
    if draining_policy(state):
        result["phase"] = public_phase(state)
    from .cycle_probe import lifecycle
    result["lifecycle"] = lifecycle(state)
    if result["lifecycle"]:
        result["lifecycle"].update({key: result[key] for key in (
            "phase", "time_policy", "admission_end_at", "admission_closed_at", "end_at",
            "match_manifest", "match_manifest_sha256") if key in result})
    return result
