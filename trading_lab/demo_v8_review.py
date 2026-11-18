"""Read-only V8 Demo operating evidence. Never invokes transport or a model."""
from datetime import datetime
from decimal import Decimal
from .cycle_diagnostics import _order_trace, _costs, _decimal
from .arena_strategies import fingerprint
from .strategy_contract import payload_of
from .match_protocol import validate_manifest
from .exchange_protection import intent_of
from .ops_policy import validate_stop_receipt


def epoch(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError, TypeError):
        return None


def review(state):
    if (state.get("mode") != "OKX_DEMO" or state.get("context") != "workshop_demo"
            or state.get("protocol_version", 0) < 8):
        raise ValueError("ordinary_v8_demo_required")
    validate_manifest(state)
    recipes = [r for r in state["strategies"] if r["id"] != "cash"]
    bots = [b for b in state["bots"] if b["id"] != "cash"]
    if len(recipes) != 1 or len(bots) != 1:
        raise ValueError("single_demo_bot_required")
    recipe, bot = recipes[0], bots[0]
    if fingerprint(payload_of(recipe)) != recipe["recipe_sha256"]:
        raise ValueError("recipe_hash_changed")
    events = [e for e in state["events"] if e.get("bot_id") in {None, bot["id"]}]
    trace = _order_trace(bot, events)
    complete = not state.get("_history_compact", False) and trace["inventory_known"] and not trace["limitations"]
    fills = trace["fills"] if complete else []
    entries = [f for f in fills if f["role"] == "entry"]
    first = entries[0] if entries else None
    reductions = {o["request"].get("clOrdId") for o in bot.get("_orders", []) if o.get("_reduction")}
    partial = next((f for f in fills if f["key"] in reductions and f["role"] == "close"
        and 0 < abs(f["after"]) < abs(f["before"])), None)
    close = next((f for f in reversed(fills) if f["role"] == "close" and f["after"] == 0), None)
    cutoff = epoch(state.get("_end"))
    cutoff_event = next((e for e in events if e.get("code") == "admission_closed"), None)
    late_entries = [o for o in bot.get("_orders", []) if o.get("request", {}).get("reduceOnly") is False
        and epoch(o.get("at")) is not None and cutoff is not None and epoch(o["at"]) >= cutoff]
    tail = next((e for e in events if e.get("bot_id") == bot["id"] and e.get("code") == "financial_observation"
        and epoch(e.get("at")) is not None and cutoff is not None and epoch(e["at"]) >= cutoff
        and _decimal((e.get("data") or {}).get("financial", {}).get("position_btc")) not in {None, Decimal(0)}), None)
    exit_event = next((e for e in events if e.get("bot_id") == bot["id"] and e.get("code") == "exit_trigger"
        and (e.get("data") or {}).get("exit_reason") == "rules_exit"), None)

    owned_stop = False
    for record in bot.get("_protections", []):
        try:
            if not record["body"]["algoClOrdId"].startswith("J8" + state["id"][:16]):
                continue
            validate_stop_receipt(intent_of(record), record["receipt"], known_algo_id=(record.get("proof") or {}).get("algo_id"))
            owned_stop = True
        except (KeyError, TypeError, ValueError):
            continue

    def coverage(after, before=None, *, expected_contracts=None, fill_seq=None):
        if not owned_stop or after is None:
            return None
        unit = Decimal(str(bot.get("_instrument", {}).get("contract_value", 0))) * Decimal(str(
            bot.get("_instrument", {}).get("contract_multiplier", 0)))
        for frame in state.get("frames", []):
            stamp = epoch(frame.get("at"))
            if stamp is None or stamp < after or before is not None and stamp >= before:
                continue
            if fill_seq is not None and (type(frame.get("event_seq")) is not int or frame["event_seq"] < fill_seq):
                continue
            row = next((b for b in frame.get("bots", []) if b.get("id") == bot["id"]), {})
            protection = row.get("protection") or {}
            try:
                qty, covered = abs(Decimal(str(row.get("position_btc")))), Decimal(str(protection.get("covered_contracts")))
                if (qty > 0 and unit > 0 and protection.get("status") == "ARMED" and protection.get("verified") is True
                        and covered*unit == qty and (expected_contracts is None or covered == abs(expected_contracts))):
                    return {"frame_at": frame["at"], "event_seq": frame.get("event_seq"),
                        "evidence_level": "recorded_executor_verified_coverage_with_owned_stop_receipt"}
            except (ValueError, ArithmeticError, TypeError):
                continue
        return None

    first_at = epoch(first["event"].get("at")) if first else None
    partial_at = epoch(partial["event"].get("at")) if partial else None
    close_at = epoch(close["event"].get("at")) if close else None
    initial_contracts = partial["before"] if partial else close["before"] if close else first["after"] if first else None
    sl_initial = coverage(first_at, partial_at or close_at, expected_contracts=initial_contracts,
                          fill_seq=first["event"].get("seq") if first else None)
    sl_resized = coverage(partial_at, close_at, expected_contracts=partial["after"],
                          fill_seq=partial["event"].get("seq")) if partial else None
    stages = []

    def row(identity, observed, detail, *, optional=False, violated=False):
        event = observed["event"] if isinstance(observed, dict) and "event" in observed else observed
        stages.append({"id": identity, "status": "violated" if violated else "observed" if observed else "not_observed" if complete else "unknown",
            "required": not optional, "event_seq": event.get("seq", event.get("event_seq")) if isinstance(event, dict) else None,
            "detail": detail})

    row("short_fill", first if first and first["after"] < 0 else None, "Ненульовий cumulative sell entry; не ACK чи намір.")
    row("initial_hosted_sl", sl_initial, "Збережений executor-кадр із покриттям фактичного обсягу; власний stop receipt перевірено. Історичну відповідь не вигадуємо.")
    row("partial_fill_and_residual", partial, "Фактичний reduce-only fill позначеного часткового виходу; залишок ненульовий.")
    row("resized_hosted_sl", sl_resized, "Після partial збережено verified coverage залишку; сторонній algo не зараховується.")
    row("open_after_admission", tail if recipe["rules"].get("max_hold_seconds") is None else None, "Після cutoff спостерігалася відкрита позиція з max_hold_seconds:null.")
    row("no_new_entries_after_cutoff", cutoff_event if complete and not late_entries else None, "Перевірено час усіх non-reduce intents; пізній fill раніше поданого ордера дозволений.", violated=bool(late_entries))
    row("authored_full_exit", close if exit_event and close and epoch(exit_event["at"]) <= close_at else None, "Формальний exit trigger і наступний фактичний повний вихід; Stop/SL не підмінюють цей етап.")
    costs = _costs(bot, events, trace)
    settled = next((e for e in reversed(events) if e.get("bot_id") == bot["id"] and e.get("code") == "accounting_confirmed"), None)
    row("flat_and_settled", settled if close and _decimal(bot.get("position_btc")) == 0 and costs["complete"] and not trace["unresolved"] else None,
        "Нульовий залишок; fees/funding/cash і Jev звірено (подія фінальної звірки).")
    row("owner_stop_close", trace["owner_close"] if close else None, "Окремий запуск: owner Stop під час власної відкритої позиції і фактичне закриття.", optional=True)
    recovery = next((e for e in events if e.get("code") == "stop_requested" and "Відновлення після перерви" in e.get("message", "")), None)
    recovery_at = epoch(recovery["at"]) if recovery else None
    recovered_no_entry = recovery and not any(o.get("request", {}).get("reduceOnly") is False and epoch(o.get("at")) is not None and epoch(o["at"]) >= recovery_at for o in bot.get("_orders", []))
    recovered_no_paid = recovery and not any(e.get("code") == "jev_decision" and (e.get("data") or {}).get("ai_used") is True and epoch(e.get("at")) is not None and epoch(e["at"]) >= recovery_at for e in events)
    row("recovery_close_only", close if state.get("_recovery") and recovered_no_entry and recovered_no_paid and close and close_at >= recovery_at else None, "Окрема recovery-гілка: власний залишок закрито без нового входу чи paid Jev; incomplete зберігається.", optional=True)
    synthetic = bool(state.get("fixture_only")) or state.get("evidence_kind") == "SYNTHETIC"
    return {"schema": "v8-demo-review/v1", "id": state["id"], "mode": "OKX_DEMO", "status": state["status"],
        "execution_mode": state.get("execution_mode", "shared_jev"),
        "recipe_sha256": recipe["recipe_sha256"], "evidence_kind": "SYNTHETIC" if synthetic else "ACTUAL_DEMO_JOURNAL",
        "causal_history_complete": complete, "rows": stages, "costs": costs,
        "all_primary_observed": all(r["status"] == "observed" for r in stages if r["required"]),
        "financial_actions": False, "limitations": trace["limitations"] + [
            "Це перевірка збереженого менеджером журналу; скрипт не виконує новий GET до біржі.",
            "Fictional fixture не є actual Demo. Етап not_observed не вважається успішним.",
            "Host SL coverage береться з історичних executor-кадрів і own receipt; це не відтворена історична API-відповідь.",
            "Покриття функцій не доводить прибутковості; Stop та recovery — окремі гілки."]}
