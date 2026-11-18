"""Read-only per-bot causal evidence. No transport, credentials or migrations."""
from decimal import Decimal, InvalidOperation

from .cycle_probe import is_probe


_TERMINAL_ORDERS = {"filled", "canceled", "mmp_canceled"}
_COMMON_CODES = {"season_done", "season_finished", "market_unavailable", "exchange_error",
                 "closing_paused", "entry_window_closed", "trading_window_closed",
                 "admission_closed", "paused", "data_gap",
                 "demo_funding_unknown", "funding_schedule_restored"}


def _decimal(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _number(value):
    return None if value is None else format(value, "f")


def _data(event):
    value = event.get("data")
    return value if isinstance(value, dict) else {}


def _reference(event):
    return {"event_seq": event.get("seq") if event else None,
            "event_at": event.get("at") if event else None}


def _kind(event):
    value = _data(event).get("kind")
    if value in {"entry", "add", "hold"}:
        return value
    if event.get("code") == "entry_checks":
        return "entry"
    if event.get("code") == "position_checks":
        return "add"
    return "entry" if event.get("step") == 2 else "add"


def _owner_stop(event):
    data = _data(event)
    return (event.get("code") == "stop_requested" and
            (data.get("origin") == "owner" or data.get("owner_requested") is True or
             str(event.get("message", "")).startswith("Зупинка власником.")))


def _order_trace(bot, events):
    """Deduplicate cumulative fills; preserve causal inventory at an owner's Stop.

    A repeated partial/terminal read never creates another fill, entry, add or
    round trip. An add is a DIFFERENT non-reduce order whose first actual fill
    arrives while an owned position is already open. Buy closes a short; it is
    not automatically another entry. Public old logs can recover requests from
    their order-intent events. Missing request facts stay unknown.
    """
    requests, final, identities, intent_keys = {}, {}, {}, set()
    limitations = []
    orders = bot.get("_orders")
    orders = orders if isinstance(orders, list) else []
    for order in orders:
        request, result = order.get("request") or {}, order.get("result") or {}
        client = request.get("clOrdId") or result.get("clOrdId")
        identity = result.get("ordId") or client
        if client:
            requests[client] = request
            intent_keys.add(client)
        if identity:
            requests[identity] = request
            identities[identity] = client or identity
            if result:
                final[client or identity] = result
    for event in events:
        if event.get("code") == "exchange_order_intent":
            request = _data(event).get("request") or {}
            if request.get("clOrdId"):
                requests[request["clOrdId"]] = request
                intent_keys.add(request["clOrdId"])
    owned, known, seen, roles = Decimal(0), True, {}, {}
    fill_rows, stops, owner_close = [], [], None
    entry_ids, add_ids, close_ids = set(), set(), set()
    round_trips = 0
    for event in events:
        if _owner_stop(event):
            # An earlier trade that is already flat is not Stop coverage.
            if known and owned != 0:
                stops.append((event, owned))
            continue
        if event.get("code") not in {"exchange_entry_fill", "exchange_close_fill"}:
            continue
        result = _data(event).get("result") or {}
        identity = result.get("ordId") or result.get("clOrdId")
        key = result.get("clOrdId") or identities.get(identity) or identity
        qty = _decimal(result.get("contracts"))
        if not key or qty is None or qty < 0:
            limitations.append("Неповний або нечисловий cumulative fill не підтверджує виконання.")
            known = False
            continue
        request = requests.get(key) or requests.get(identity) or {}
        previous = seen.get(key, Decimal(0))
        if qty < previous:
            limitations.append("Cumulative quantity регресує; історію залишку не підтверджено.")
            known = False
            continue
        final[key] = result
        seen[key] = qty
        delta = qty - previous
        if delta == 0:
            continue
        side = request.get("side")
        reduce = request.get("reduceOnly")
        if side not in {"buy", "sell"} or not isinstance(reduce, bool):
            limitations.append("Старий fill не має напрямку/reduce-only intent; entry/add/Stop не виводяться здогадом.")
            known = False
            continue
        direction = Decimal(1) if side == "buy" else Decimal(-1)
        before = owned
        if reduce:
            role = "close"
            if not known or before == 0 or before * direction >= 0 or delta > abs(before):
                limitations.append("Close quantity не зіставлена з попереднім власним залишком.")
                known = False
                continue
            owned += direction * delta
            close_ids.add(key)
            if owned == 0:
                round_trips += 1
            if stops and owner_close is None:
                owner_close = event
        else:
            if known and before * direction < 0:
                limitations.append("Non-reduce fill змінює напрямок наявної позиції; класифікація непідтверджена.")
                known = False
                continue
            role = roles.get(key)
            if role is None:
                role = "add" if before else "entry"
                roles[key] = role
            owned += direction * delta
            (add_ids if role == "add" else entry_ids).add(key)
        fill_rows.append({"event": event, "role": role, "key": key,
                          "delta": delta, "before": before, "after": owned})
    # Private final facts can be complete even when historical events are partial.
    # Preserve them for cost/intent counts, but never invent causal stage evidence.
    for order in orders:
        result = order.get("result") or {}
        if not result:
            continue
        identity = result.get("clOrdId") or result.get("ordId")
        if not identity:
            continue
        old = final.get(identity)
        if old is None or (_decimal(result.get("contracts")) or Decimal(0)) >= (
                _decimal(old.get("contracts")) or Decimal(0)):
            final[identity] = result
    unresolved = any(key not in final or final[key].get("state") not in _TERMINAL_ORDERS
                     for key in intent_keys)
    positive_final = {key for key, result in final.items()
                      if (_decimal(result.get("contracts")) or Decimal(0)) > 0}
    classified = entry_ids | add_ids | close_ids
    if positive_final - classified:
        limitations.append("Є final order facts без повної causal історії; стадії й Stop не домальовуються.")
    return {"fills": fill_rows, "facts": final, "requests": requests, "intent_count": len(intent_keys),
            "entry_ids": entry_ids, "add_ids": add_ids, "close_ids": close_ids,
            "round_trips": round_trips, "owned": owned, "inventory_known": known,
            "missing_order_results": any(key not in final for key in intent_keys),
            "unresolved": unresolved, "owner_close": owner_close,
            "limitations": list(dict.fromkeys(limitations))}


def _funnel(events, trace):
    counters = {kind: {"checks": 0, "signals": 0, "signals_unknown": 0, "failed_checks": 0, "unknown_checks": 0,
                       "jev_decisions": 0, "jev_calls": 0, "jev_allows": 0,
                       "jev_holds": 0, "jev_vetoes": 0, "jev_outcomes_unknown": 0,
                       "rechecks": 0, "recheck_vetoes": 0}
                for kind in ("entry", "add", "hold")}
    known_kind, no_signal_unknown = None, 0
    flat_waits, position_holds = 0, 0
    for event in events:
        code, data = event.get("code"), _data(event)
        if code in {"entry_checks", "position_checks"}:
            known_kind = _kind(event)
            bucket = counters[known_kind]
            bucket["checks"] += 1
            if data.get("local_checks_passed") is True:
                bucket["signals"] += 1
            elif data.get("local_checks_passed") is not False:
                bucket["signals_unknown"] += 1
            checks = data.get("checks") or []
            bucket["failed_checks"] += int(any(c.get("passed") is False for c in checks))
            bucket["unknown_checks"] += int(any(c.get("passed") is None for c in checks))
        elif code == "no_signal":
            if known_kind == "entry":
                flat_waits += 1
            elif known_kind in {"add", "hold"}:
                position_holds += 1
            else:
                no_signal_unknown += 1
        elif code == "rules_decision" and data.get("choice") == "hold":
            if event.get("step") == 2:
                flat_waits += 1
            else:
                position_holds += 1
        elif code == "jev_decision":
            known_kind = _kind(event)
            bucket = counters[known_kind]
            bucket["jev_decisions"] += 1
            bucket["jev_calls"] += int(data.get("ai_used") is True)
            answer = data.get("answer") or {}
            bucket["jev_holds"] += int(answer.get("choice") == "hold")
            if data.get("jev_allowed") is True:
                bucket["jev_allows"] += 1
            elif data.get("jev_allowed") is False:
                bucket["jev_vetoes"] += 1
            else:
                # Valid old typed answer does not prove that an execution gate allowed it.
                bucket["jev_outcomes_unknown"] += 1
        elif code in {"entry_rechecked", "entry_cancelled"}:
            kind = data.get("kind") or known_kind
            if kind in counters:
                counters[kind]["rechecks" if code == "entry_rechecked" else "recheck_vetoes"] += 1
    counters.update(entry_filled_orders=len(trace["entry_ids"]),
                    add_filled_orders=len(trace["add_ids"]),
                    close_filled_orders=len(trace["close_ids"]),
                    round_trips=trace["round_trips"], order_intents=trace["intent_count"],
                    flat_waits=flat_waits, position_holds=position_holds,
                    no_signal_unknown=no_signal_unknown,
                    cumulative_fill_updates=len(trace["fills"]),
                    limitation="Перевірки з повторними свічками не є незалежними ринковими спостереженнями.")
    return counters


def _costs(bot, events, trace):
    account = bot.get("accounting") or {}
    reconciled = (bot.get("accounting_reconciled") is True and
                  bot.get("funding_reconciled") is True and account.get("cash_delta_matches") is True)
    facts = list(trace["facts"].values())

    def cumulative(field):
        if trace["missing_order_results"]:
            # An uncertain POST may already have filled. No response is not zero.
            return None
        values = []
        for result in facts:
            quantity = _decimal(result.get("contracts"))
            if quantity is None:
                return None
            if quantity == 0:
                continue
            value = _decimal(result.get(field))
            if value is None:
                return None
            values.append(value)
        return sum(values, Decimal(0))

    if reconciled:
        gross = _decimal(account.get("gross_pnl_usdt"))
        fees = _decimal(account.get("signed_fees_usdt"))
        funding = _decimal(account.get("signed_funding_usdt"))
        source = "actual_bills_and_cash_reconciled"
    else:
        gross, fees = cumulative("gross_pnl_usdt"), cumulative("signed_fee_usdt")
        funding_events = [e for e in events if e.get("code") == "exchange_funding"]
        funding = (_decimal(_data(funding_events[-1]).get("signed_funding_usdt")) if funding_events else
                   _decimal(bot.get("_funding")) if bot.get("_next_funding_read") is not None else None)
        source = "actual_cumulative_order_facts_unreconciled"
    net = gross + fees + funding if None not in (gross, fees, funding) else None
    decisions = [e for e in events if e.get("code") == "jev_decision"]
    attempted = [e for e in decisions if _data(e).get("ai_used") is True]
    unknown = sum(_data(e).get("reported_batch_cost_usd") is None for e in attempted)
    requests = bot.get("ai_requests")
    requests = requests if isinstance(requests, int) and not isinstance(requests, bool) else len(attempted)
    missing_requests = max(0, requests - len(attempted))
    unknown += missing_requests if bot.get("ai_cost_complete") is not True else 0
    known_cost = _decimal(bot.get("ai_cost_usd"))
    jev_complete = bot.get("ai_cost_complete") is True and unknown == 0 and known_cost is not None
    flat = _decimal(bot.get("position_btc")) == 0
    return {"gross_pnl_usdt": _number(gross), "signed_fees_usdt": _number(fees),
            "signed_funding_usdt": _number(funding), "net_usdt": _number(net),
            "cash_delta_usdt": _number(_decimal(account.get("cash_delta_usdt"))) if reconciled else None,
            "source": source, "complete": bool(reconciled and flat and net is not None and jev_complete),
            "mark_pnl_usdt": bot.get("pnl_usdt"),
            "open_position_mark_includes_exit_fee_estimate": not flat,
            "funding_source": "actual_bills_reconciled" if reconciled else "actual_bills_unreconciled" if funding is not None else "unknown",
            "jev": {"known_cost_usd": _number(known_cost), "unknown_billing_requests": unknown,
                    "requests": requests, "complete": jev_complete,
                    "source": "reported_batch_cost_equal_share_estimate" if known_cost is not None else "unknown",
                    "limitation": "USD Jev обліковується окремо від USDT PnL; частка batch є оцінкою."}}


def _paper_events(events):
    """Normalize old PAPER gate names WITHOUT changing original journals.

    Paper entry_intent is causal evidence that a local gate permitted entry/add;
    absence of that event never becomes a false boolean for an old check.
    """
    normalized, position = [], Decimal(0)
    last_checks = {}
    for event in events:
        copy = {**event, "data": {**_data(event)}}
        data, code = copy["data"], copy.get("code")
        if code == "paper_entry_fill":
            qty = _decimal(data.get("quantity_btc"))
            if qty is not None and data.get("side") in {"long", "short"}:
                position += qty * (1 if data["side"] == "long" else -1)
        elif code == "paper_close_fill":
            remaining = _decimal(data.get("remaining_btc"))
            if remaining is not None:
                position = remaining
        if code in {"entry_checks", "position_checks"}:
            kind = data.setdefault("kind", "entry" if code == "entry_checks" else "add")
            last_checks[kind] = len(normalized)
        elif code == "entry_intent":
            kind = data.setdefault("kind", "add" if position else "entry")
            index = last_checks.get(kind)
            if index is not None and "local_checks_passed" not in normalized[index]["data"]:
                normalized[index]["data"]["local_checks_passed"] = True
        elif code == "jev_gate":
            copy["code"] = "jev_decision"
            data.update(ai_used=data.get("attempted"), jev_allowed=data.get("allowed"),
                        reported_batch_cost_usd=data.get("batch_cost_usd"))
            data.setdefault("kind", "entry" if copy.get("step") == 2 else "add")
            if data.get("allowed") is False and (data.get("answer") or {}).get("choice") == "hold":
                data.setdefault("blocked_reason", "choice_hold")
        normalized.append(copy)
    return normalized


def _paper_trace(bot, recipe, events):
    """Paper fills are incremental BTC model facts, NEVER cumulative OKX facts."""
    owned, known, seen = Decimal(0), True, set()
    fill_rows, limitations, stops, owner_close = [], [], [], None
    entries, adds, closes, facts = set(), set(), set(), {}
    rounds, intents = 0, 0
    for index, event in enumerate(events):
        code, data = event.get("code"), _data(event)
        if code in {"entry_intent", "close_intent"}:
            intents += 1
        if _owner_stop(event):
            if known and owned:
                stops.append(event)
            continue
        if code not in {"paper_entry_fill", "paper_close_fill"}:
            continue
        seq = event.get("seq")
        key = "paper:" + str(seq) if seq is not None else "unidentified:" + str(index)
        if key in seen:
            continue
        seen.add(key)
        if seq is None:
            known = False
            limitations.append("Старий PAPER fill не має seq; унікальність і повноту model fill історії не підтверджено.")
        qty = _decimal(data.get("quantity_btc"))
        if qty is None or qty <= 0:
            limitations.append("Неповний PAPER quantity не підтверджує модельне виконання.")
            known = False
            continue
        before = owned
        if code == "paper_entry_fill":
            side = data.get("side") or recipe.get("rules", {}).get("side")
            if side not in {"long", "short"}:
                limitations.append("Напрямок старого PAPER fill невідомий.")
                known = False
                continue
            sign = Decimal(1) if side == "long" else Decimal(-1)
            if before * sign < 0:
                known = False
                limitations.append("PAPER entry має протилежний напрямок наявного залишку.")
                continue
            role = "add" if before else "entry"
            owned += sign * qty
            (adds if role == "add" else entries).add(key)
        else:
            remaining = _decimal(data.get("remaining_btc"))
            if not before or qty > abs(before) or remaining is None:
                known = False
                limitations.append("PAPER close не зіставлений із попереднім залишком/remaining_btc.")
                continue
            sign = Decimal(1) if before > 0 else Decimal(-1)
            owned -= sign * qty
            if abs(owned - remaining) > Decimal(".00000001"):
                known = False
                limitations.append("PAPER remaining_btc не збігається з causal fills.")
            # Engine rounds balances to 8 decimal places; use its explicit remaining.
            owned = remaining
            role = "close"
            closes.add(key)
            if owned == 0:
                rounds += 1
            if known and stops and owner_close is None:
                owner_close = event
        facts[key] = data
        fill_rows.append({"event": event, "role": role, "key": key,
                          "delta": qty, "before": before, "after": owned})
    return {"fills": fill_rows, "facts": facts, "requests": {}, "intent_count": intents,
            "entry_ids": entries, "add_ids": adds, "close_ids": closes,
            "round_trips": rounds, "owned": owned, "inventory_known": known,
            "missing_order_results": False, "unresolved": bool(bot.get("_pending")),
            "owner_close": owner_close, "limitations": list(dict.fromkeys(limitations))}


def _paper_costs(state, bot, events, trace):
    gross, fees = Decimal(0), Decimal(0)
    for fill in trace["fills"]:
        data = _data(fill["event"])
        fee = _decimal(data.get("fee_usdt"))
        pnl = _decimal(data.get("gross_pnl_usdt")) if fill["role"] == "close" else Decimal(0)
        fees = fees - fee if fees is not None and fee is not None else None
        gross = gross + pnl if gross is not None and pnl is not None else None
    funding = _decimal(bot.get("funding_usdt"))
    # PAPER funding_usdt is a signed PAYMENT; Exchange funding_usdt is a cost.
    net = gross + fees + funding if None not in (gross, fees, funding) and trace["inventory_known"] else None
    jev = _costs(bot, events, {**trace, "facts": {}, "missing_order_results": False})["jev"]
    if state.get("execution_mode", "rules") != "shared_jev":
        jev = {"known_cost_usd": "0", "unknown_billing_requests": 0, "requests": 0,
               "complete": True, "source": "not_used_rules_only",
               "limitation": "Безкоштовні формальні правила; Jev не викликався."}
    flat = _decimal(bot.get("position_btc")) == 0
    realized = _decimal(bot.get("realized_pnl_usdt"))
    difference = realized - net if realized is not None and net is not None else None
    matching = difference is not None and abs(difference) <= Decimal(".000001")
    complete = (not state.get("incomplete", False) and flat and trace["inventory_known"] and
                trace["owned"] == 0 and not trace["unresolved"] and matching and jev["complete"])
    return {"gross_pnl_usdt": _number(gross), "signed_fees_usdt": _number(fees),
            "signed_funding_usdt": _number(funding), "net_usdt": _number(net),
            "cash_delta_usdt": None, "model_cash_delta_usdt": _number(realized),
            "source": "paper_model_events", "complete": complete,
            "mark_pnl_usdt": bot.get("pnl_usdt"), "rounding_difference_usdt": _number(difference),
            "open_position_mark_includes_exit_fee_estimate": not flat,
            "funding_source": "estimated_paper_funding" if funding is not None else "unknown",
            "jev": jev,
            "limitation": "Модельні fills/fees/slippage/funding, не біржові ордери, bills чи фактичний cash."}


def _phase(state, bot, recipe, events):
    terminal = state.get("status") in {"DONE", "PAUSED"} or bot.get("status") == "STOPPED"
    position = _decimal(bot.get("position_btc"))
    open_position = position is not None and position != 0
    relevant = [e for e in events if e.get("code") in {
        "entry_checks", "position_checks", "jev_decision", "no_signal", "entry_cancelled",
        "entry_rechecked", "exchange_entry_fill", "exchange_close_fill", "stop_requested",
        "market_unavailable", "exchange_check_failed", "exchange_error", "closing_paused",
        "jev_pending", "rules_decision", "entry_intent", "close_intent",
        "paper_entry_fill", "paper_close_fill", "entry_no_fill", "risk_veto", "paused", "data_gap"}]
    last = relevant[-1] if relevant else None
    checks_events = [e for e in events if e.get("code") in {"entry_checks", "position_checks"}]
    checks = _data(checks_events[-1]).get("checks", []) if checks_events else bot.get("checks", [])
    failed = [{"feature": c.get("feature"), "actual": c.get("actual"),
               "op": c.get("op"), "expected": c.get("expected")}
              for c in checks if c.get("passed") is False]
    unknown = [c.get("feature") for c in checks if c.get("passed") is None]
    status = bot.get("status")
    if state.get("status") == "PAUSED" or bot.get("_manual_review"):
        code, step, label = "paused", 6, "Потрібне відновлення/ручна звірка"
    elif terminal:
        code, step, label = "finished", 6, "Завершено" if not open_position else "Залишок потребує звірки"
    elif status in {"CLOSING", "ENTRY_PENDING"}:
        code, step, label = ("closing", 6, "Закриття/звірка") if status == "CLOSING" else ("order_reconciliation", 3, "Звірка фактичного ордера")
    elif status == "JEV_PENDING" or (state.get("mode") == "PAPER" and bot.get("_pending")
            and bot["_pending"].get("jev_requested") and not bot["_pending"].get("jev_approved")):
        code, step, label = "jev", 5 if open_position else 2, "Очікуємо відповідь Jev"
    elif state.get("mode") == "PAPER" and bot.get("_pending") and bot["_pending"].get("kind") == "entry":
        code, step, label = "pending_model_fill", 3, "Очікуємо наступне котирування для модельного fill"
    elif open_position:
        code, step, label = "position_checks", 4, "Керуємо відкритою позицією"
    else:
        code, step, label = "entry_checks", 1, "Перевіряємо наступний вхід"
    waiting = "position_managed" if open_position else "conditions_not_met"
    message = "Позиція відкрита; виходи контролює код." if open_position else "Чекаємо збігу формальних умов."
    if code == "paused":
        waiting, message = "manual_review", label
    elif terminal:
        waiting, message = "finished" if not open_position else "remaining_position", label
    elif last and last.get("code") in {"market_unavailable", "exchange_check_failed", "exchange_error", "closing_paused", "paused", "data_gap"}:
        waiting, message = _data(last).get("reason", last["code"]), str(last.get("message", "Перевірка недоступна."))
    elif code == "closing":
        waiting, message = "closing", str(bot.get("_close_reason") or bot.get("last_reason") or label)
    elif code == "order_reconciliation":
        waiting, message = "pending_order", label
    elif code == "jev":
        waiting, message = "jev_pending", label
    elif code == "pending_model_fill":
        waiting, message = "pending_model_fill", label
    elif last and last.get("code") == "jev_decision":
        data, answer = _data(last), _data(last).get("answer") or {}
        if data.get("jev_allowed") is False or answer.get("choice") == "hold":
            waiting = data.get("blocked_reason") or ("jev_hold" if answer.get("choice") == "hold" else data.get("reason") or "jev_veto")
            message = str(last.get("message", "Jev не дозволив новий ордер."))
    elif last and last.get("code") == "entry_cancelled":
        waiting, message = "final_recheck_cancelled", str(last.get("message", "Фінальні умови змінилися."))
    elif last and last.get("code") in {"entry_no_fill", "risk_veto"}:
        waiting, message = last["code"], str(last.get("message", "Модельний fill не відбувся."))
    elif unknown:
        waiting, message = "unknown_features", "Бракує значень для формальної перевірки: " + ", ".join(str(x) for x in unknown)
    elif open_position and recipe.get("rules", {}).get("max_entries") == 1:
        waiting, message = "position_managed_no_adds", "Позиція відкрита; цей рецепт не передбачає докупівлю."
    critical = last and last.get("code") in {
        "market_unavailable", "exchange_check_failed", "exchange_error", "closing_paused", "paused", "data_gap"}
    draining_wait = state.get("status") == "DRAINING" and not terminal
    if draining_wait and code not in {"closing", "paused", "order_reconciliation"} and not critical:
        waiting, message = ("entry_window_closed_position_managed", "Час нових входів завершено; позиція виходить за власними правилами.") if open_position else (
            "entry_window_closed", "Час нових входів завершено; повторний цикл не запускається.")
    return ({"code": code, "step": step, "label": label, **_reference(last)},
            {"code": waiting, "message": message, "failed_checks": failed,
             "unknown_checks": unknown, **_reference(last)})


def _bot_lifecycle(state, bot, recipe):
    bot_id = bot.get("id")
    own = [e for e in state.get("events", []) if e.get("bot_id") == bot_id]
    applicable = [e for e in state.get("events", []) if e.get("bot_id") == bot_id or
                  e.get("bot_id") is None and e.get("code") in _COMMON_CODES]
    paper = state.get("mode") == "PAPER"
    if paper:
        own = _paper_events(own)
        by_seq = {e.get("seq"): e for e in own if e.get("seq") is not None}
        applicable = [by_seq.get(e.get("seq"), e) if e.get("bot_id") == bot_id else e for e in applicable]
    diagnostic = is_probe(recipe)
    mechanics = state.get("execution_mode") == "demo_mechanics" and state.get("mode") == "OKX_DEMO"
    uses_jev = not mechanics and state.get("execution_mode", "rules" if paper else "shared_jev") == "shared_jev"
    maximum = _decimal(recipe.get("rules", {}).get("max_entries"))
    adds = diagnostic or maximum is not None and maximum > 1
    terminal = state.get("status") in {"DONE", "PAUSED"} or bot.get("status") == "STOPPED"
    trace = _paper_trace(bot, recipe, own) if paper else _order_trace(bot, own)
    rows = []

    def first(code, predicate=lambda e: True, common=False):
        return next((e for e in applicable if e.get("code") == code and predicate(e)
                     and (common or e.get("bot_id") == bot_id)), None)

    def row(identity, label, evidence=None, detail="", blocked=None, required=True, applicable=True):
        status = ("not_applicable" if not applicable else "observed" if evidence else
                  "blocked" if blocked else "not_observed" if terminal else "pending")
        rows.append({"id": identity, "label": label, "status": status, "detail": detail,
                     "required": bool(required and applicable), **_reference(evidence)})

    def jev(kind):
        calls = [e for e in own if e.get("code") == "jev_decision" and _kind(e) == kind]
        valid = next((e for e in calls if _data(e).get("ai_used") is True and
                      _data(e).get("reason") == "typed_answer" and _data(e).get("answer")), None)
        rejected = calls[-1] if calls and valid is None else None
        if valid:
            answer, data = _data(valid)["answer"], _data(valid)
            detail = "Відповідь: " + str(answer.get("choice", "невідома"))
            confidence = answer.get("confidence")
            if confidence is not None:
                detail += " · confidence " + str(confidence)
            if data.get("blocked_reason") == "low_confidence":
                threshold = _decimal(data.get("confidence_threshold"))
                detail += f" < {threshold:.2f}; дія заблокована." if threshold is not None else "; дія заблокована низькою confidence."
            elif data.get("jev_allowed") is False:
                detail += "; новий ордер не дозволено."
        else:
            detail = str(_data(rejected).get("reason", "Відповідь не підтверджено.")) if rejected else "Ще не викликався."
        return valid, detail, rejected

    def fill(role):
        return next((f["event"] for f in trace["fills"] if f["role"] == role), None)

    row("checks", "1 · Дані та перевірки входу", first("entry_checks"), "Фактичні значення умов записані у журналі цього бота.")
    row("jev_entry", "2 · Jev перед входом", *jev("entry"), applicable=uses_jev, required=uses_jev)
    if not uses_jev:
        rows[-1]["detail"] = "Перевірка механіки OKX Demo: Jev не використовується." if mechanics else "RULES_ONLY: формальний рецепт виконується без Jev; payload гейту журналюється для офлайн SHADOW."
    row("entry", "3 · Підтверджений модельний вхід" if paper else "3 · Підтверджений вхід OKX", fill("entry"),
        "Ненульовий PAPER fill за наступним котируванням; не біржовий ордер." if paper else "Потрібний ненульовий власний cumulative fill.")
    row("position_checks", "4 · Перевірки відкритої позиції", first("position_checks"), "Ринкові дані та власний залишок перевірено.")
    row("jev_hold", "5 · Jev перевірив утримання", *jev("hold"), applicable=diagnostic,
        required=diagnostic)
    if not diagnostic:
        rows[-1]["detail"] = "У звичайній стратегії утримання/вихід перевіряє код; Jev тут не викликається."
    row("jev_add", "5 → 2 · Jev перед докупівлею", *jev("add"), applicable=adds and uses_jev, required=adds and uses_jev)
    if adds and not uses_jev:
        rows[-1]["detail"] = ("Перевірка механіки OKX Demo: докупівлю перевіряє код; Jev не викликається." if mechanics
                              else "RULES_ONLY: докупівлю перевіряє код; Jev не викликається.")
    row("add", "3 · Підтверджена модельна докупівля" if paper else "3 · Підтверджена докупівля OKX", fill("add"),
        "Окремий модельний fill у вже відкриту позицію." if paper else "Окремий fill у вже відкриту власну позицію.", applicable=adds, required=adds)
    if not adds:
        for item in rows[-2:]:
            item["detail"] = "Рецепт не передбачає докупівлю." if maximum is not None else "У старому записі немає max_entries; застосовність цієї гілки невідома."
    flat = _decimal(bot.get("position_btc")) == 0
    closed = fill("close") if (flat and trace["inventory_known"] and trace["owned"] == 0 and not trace["unresolved"]) else None
    row("close", "6 · Модельне закриття й нульовий залишок" if paper else "6 · Закриття й нульовий залишок", closed,
        "PAPER close fill і модельний залишок 0; не фактичне біржове закриття." if paper else "Власний reduce-only fill, нульовий залишок і відсутність непевних ордерів.")
    costs = _paper_costs(state, bot, own, trace) if paper else _costs(bot, own, trace)
    settled = None
    if paper and costs["complete"]:
        settled = first("season_finished", common=True) or closed
    elif not paper and (bot.get("accounting_reconciled") is True and bot.get("funding_reconciled") is True
            and bot.get("ai_cost_complete") is True):
        settled = first("accounting_confirmed") or first("season_done", common=True)
    row("accounting", "Модельні fees, funding та equity зіставлено" if paper else "Комісії, funding, cash та Jev звірено", settled,
        "Зіставлення model events та PAPER equity; не звірка фактичних біржових bills/cash." if paper else "Нуль угод сам по собі не підтверджує торговий цикл.")
    row("owner_stop", "Stop → модельне закриття · окрема перевірка" if paper else "Stop → закриття · окрема перевірка", trace["owner_close"] if closed else None,
        "Потрібний явний owner origin у Stop під час відкритої модельної позиції та наступний PAPER close fill." if paper else "Потрібний Stop власника під час ненульової власної позиції та наступне фактичне закриття.", required=False)
    required = [r for r in rows if r["required"]]
    observed = sum(r["status"] == "observed" for r in required)
    phase, wait_reason = _phase(state, bot, recipe, applicable)
    limitations = ["Покриття підтверджує механіку, не прибутковість чи перевагу стратегії."] + trace["limitations"]
    if not diagnostic:
        limitations.append("Звичайний рецепт викликає Jev для entry/add; утримання й вихід контролює код." if uses_jev else
                           "Перевірка механіки OKX Demo: усі рішення виконує код; Jev не викликається." if mechanics else
                           "RULES_ONLY: усі рішення виконує код; Jev не викликається.")
    if paper:
        limitations.append("PAPER — модель ринку; fills, fees/slippage та funding не є фактичними біржовими виконаннями.")
    if maximum is None:
        limitations.append("Контракт старого рецепта неповний; complete не може бути підтверджено.")
    if not trace["owner_close"] or not closed:
        limitations.append("Гілку Stop → модельне закриття цим запуском не підтверджено." if paper else "Гілку Stop → фактичне закриття цим запуском не підтверджено.")
    funnel = _funnel(own, trace)
    funnel["source"] = "paper_model" if paper else "actual_exchange_journal"
    if paper:
        funnel["model_fill_events"] = funnel.pop("cumulative_fill_updates")
    return {"bot_id": bot_id, "name": bot.get("name", bot_id),
            "label": "Технічна перевірка Demo" if diagnostic else "Покриття циклу · " + str(bot.get("name", bot_id)),
            "diagnostic": diagnostic, "rows": rows, "required_count": len(required),
            "observed_required_count": observed, "complete": observed == len(required) and maximum is not None and costs["complete"],
            "limitations": limitations, "funnel": funnel,
            "current_phase": phase, "wait_reason": wait_reason, "costs": costs}


def lifecycle(state):
    """Project every noncash bot, with old summary keys retained for consumers.

    Single-bot rows/summary preserve the existing contract. Multi-bot old rows
    are flattened WITH bot identity and totals aggregate all applicable stages;
    the first idle participant cannot hide another participant's execution.
    """
    recipes = {r.get("id"): r for r in state.get("strategies", []) if isinstance(r, dict)}
    bots = [_bot_lifecycle(state, b, recipes.get(b.get("id"), {}))
            for b in state.get("bots", []) if b.get("id") != "cash"]
    if not bots:
        return None
    total = sum(b["required_count"] for b in bots)
    observed = sum(b["observed_required_count"] for b in bots)
    rows = bots[0]["rows"] if len(bots) == 1 else [
        {**r, "bot_id": b["bot_id"], "bot_name": b["name"]} for b in bots for r in b["rows"]]
    limitations = list(dict.fromkeys(item for b in bots for item in b["limitations"]))
    return {"version": 2, "label": bots[0]["label"] if len(bots) == 1 else "Покриття циклу · усі боти",
            "diagnostic": any(b["diagnostic"] for b in bots), "completed": observed,
            "total": total, "all_stages_observed": all(b["complete"] for b in bots),
            "rows": rows, "limitations": limitations, "bots": bots}
