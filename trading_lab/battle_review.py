"""Read-only, versioned author evidence. No exchange or model transport.

Old journals are projected as recorded; missing timing/payloads remain unknown.
Private exchange identifiers are used internally for cumulative-fill dedup only.
"""
import copy
import hashlib
import json
import re
from collections import Counter
from decimal import Decimal

from .cycle_diagnostics import (_order_trace, _paper_trace, _paper_events,
                                _costs, _paper_costs, _funnel, lifecycle)
from .arena_strategies import ArenaError

VERSION = 1
NUMERIC = {"quantity_btc", "price_usdt", "fee_usdt", "gross_pnl_usdt", "remaining_btc",
           "position_btc", "signed_amount_usdt", "signed_funding_usdt", "rate",
           "confidence_threshold", "reported_batch_cost_usd", "cost_share_usd",
           "elapsed_ms", "quote_age_seconds", "decision_latency_ms", "fill_latency_ms",
           "bar_time_ms", "funding_time_ms", "observed_at", "trigger_at", "asof_ms", "request_at_ms", "reply_at_ms",
           "position_age_seconds", "reduce_pct", "contracts", "remaining_contracts", "shadow_event_seq",
           "decision_age_seconds", "fresh_age_seconds", "account_check_age_seconds", "account_check_before_quote_seconds",
           "post_seconds", "seconds_from_venue_fill", "seconds_from_observation", "target_seconds", "trigger_price",
           "previous_trigger_price", "previous_contracts", "venue_updated_ms", "venue_created_ms",
           "resting_reads",  # R2.1.3 fields are new keys: older journals lack them, so their bundles keep their hashes
           "warmup_attempt", "book_gets", "bar_close_ms", "gap_minutes", "gap_read", "gap_missing",  # feature_book events
           "rejected_code", "absent_reads", "absent_seconds",  # R2.1.5: a refused or never created order
           "retry_after_seconds", "refusals",  # R2.1.7: the pause before the next close
           "cancel_code",  # R2.1.8: the venue's code for a cancel
           "seconds_from_venue_fire", "stop_fire_wait_checks", "stop_fire_wait_seconds",  # R2.1.14: a stop that fired during a check
           "stop_fire_venue_contracts", "stop_fire_own_contracts"}  # R2.1.14 RC2: the wait's position on the venue and in own fills
FLAGS = {"local_checks_passed", "jev_allowed", "ai_used", "simulated", "estimated", "sent", "rules_allowed", "affects_execution",
         "within_target"}
TOKENS = {"kind", "side", "reason", "blocked_reason", "choice", "exit_reason", "source", "execution_mode", "diagnostic", "origin",
          "outcome", "venue_state", "trigger_type", "seen_by", "cancelled_by", "book_status", "reopened_from", "evidence"}
SAFE_VALUES = {"entry", "add", "hold", "close", "proceed", "long", "short", "buy", "sell",
               # R2.1.3: which read saw a fill (seen_by) and who took a stop off (cancelled_by)
               "watch_slice", "step_end", "account_check", "submit", "cancel_check", "closing", "protection",
               "own_cleanup", "venue",
               "typed_answer", "low_confidence", "choice_hold", "rules_only", "max_hold",
               "stop_loss", "take_profit", "rules_exit", "owner_stop", "risk_limit", "recovery",
               "deadline", "data_unavailable", "unknown_billing", "close_only", "shadow", "formal_reduce", "formal_exit", "cost_checkpoint", "shared_jev", "demo_mechanics",
               "g1_max_hold", "g1_stop_loss", "g1_take_profit", "g1_failed_breakout",
               "g1_owned_paper_state_mismatch", "g1_owned_paper_state_unreadable",
               # entry_cancelled reasons (exchange_lab.decide)
               "season_not_running", "admission_ended", "season_deadline", "decision_snapshot_too_old", "decision_too_slow",
               "fresh_snapshot_failed", "local_checks_failed", "kind_changed", "exit_condition", "signal_changed", "side_changed",
               "fresh_quote_too_old", "account_check_too_old", "unresolved_order", "manual_review", "admission_blocked", "protection_unverified",
               "entry_final_checks_failed", "minimum_lot_liquidity_or_exposure_veto", "diagnostic_notional_or_intent_cap",
               "stop_loss_notional_veto",  # R2.1.15 (D181 p. 1): no older journal has it
               "diagnostic_demo_only", "recipe_refused", "owner_stop", "demo_mechanics_entry_scope",
               "post_only_would_cross", "post_only_limit_unknown", "minimum_lot", "risk_veto", "owner", "system",
               "paused", "worker_failed", "manual_review",
               # R2.1: POST outcome, owned stop states and trigger type
               "acknowledged", "uncertain", "live", "canceled", "effective", "partially_effective", "order_failed", "last",
               "exchange_test_live_lot_cap",
               # feature_book: the schema-4 book's state (exchange_book.GapBookFeed.book_state)
               "warming", "ready", "degraded", "unavailable", "exhausted",
               # R2.1.5: exchange_order_posted outcome of a POST the venue refused
               "rejected",
               # R2.1.14 RC2: how a wait for our own stop's facts ended without them
               "wait_limit", "stop_not_fired", "other_facts",
               # R2.1.6: exchange_order_reopened (what it was recorded as, and what showed it exists)
               "absent", "pending", "order_get",
               # R2.1.7: an order that existed and is no longer shown; a cancel that found nothing; reopened by a cancel
               "unseen", "nothing_to_cancel", "cancel",
               # R2.1.11: entry_hold after an unseen verdict (no older journal has this token)
               "unseen_hold"}
SCHEMA4_KEY = re.compile(r"[a-z][a-z0-9_]*@(?:1m|5m|15m|1h|4h|1d):[0-9]{1,6}")  # return_bps@1m:60
HASHES = {"snapshot_sha256", "trigger_snapshot_sha256", "recipe_sha256", "payload_sha256", "payload_content_sha256", "shadow_payload_sha256", "execution_snapshot_sha256", "rules_snapshot_sha256", "decision_snapshot_sha256"}
BOT_FIELDS = {"position_btc", "entry_price", "pnl_usdt", "equity_usdt", "fees_usdt",
              "funding_usdt", "ai_cost_usd", "status", "entries", "trades", "max_drawdown_pct"}
# R2.1.11 (G2): new frame keys, absent from older frames (whose bundles keep their hashes); filled_entry_orders itself stays
# out: older R2.x frames carry it
G2_NUMBERS = ("position_base", "signed_fees_usdt")
COIN = re.compile(r"[A-Z0-9]{2,10}")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def number(value):
    try:
        result = Decimal(str(value))
        return format(result, "f") if result.is_finite() else None
    except Exception:
        return None


def feature_name(value):
    """A check's feature: a plain name, or a schema-4 key name@tf:n (strategy_v4.feature_key); else "unknown"."""
    if not isinstance(value, str) or len(value) > 80:
        return "unknown"
    return value if value.replace("_", "").isalnum() or SCHEMA4_KEY.fullmatch(value) else "unknown"


def checks(values):
    if not isinstance(values, list):
        return []
    return [{"feature": feature_name(v.get("feature")),
             "op": v.get("op") if v.get("op") in {">", ">=", "<", "<=", "==", "!=", "gt", "gte", "lt", "lte", "eq"} else None,
             "actual": number(v.get("actual")), "expected": number(v.get("expected")),
             "passed": v.get("passed") if type(v.get("passed")) is bool else None}
            for v in values if isinstance(v, dict)]


def safe_token(value):
    """A fixed vocabulary value, or a public exchange diagnostic code (never free text)."""
    from .exchange_diagnostics import public_reason
    return value in SAFE_VALUES or public_reason(value) == value


ORDER_STATES = {"live", "partially_filled", "filled", "canceled", "mmp_canceled"}
ORDER_BODY = {"instId": re.compile(r"[A-Z0-9]{2,12}-USDT-SWAP"), "tdMode": {"isolated", "cross"}, "posSide": {"net", "long", "short"},
              "side": {"buy", "sell"}, "ordType": {"market", "limit", "post_only", "ioc", "fok", "optimal_limit_ioc"}}
EXIT_NUMBERS = ("compare_price", "gain_bps", "stop_loss_bps", "take_profit_bps", "max_hold_seconds", "hold_deadline_ms",
                "hold_overrun_seconds")
PROFILE = {"exit_price_basis": {"touch"}, "hold_clock": {"venue_fill_time"}, "hold_backup_clock": {"monotonic"},
           "hosted_stop_trigger": {"last"}, "hosted_stop_order": {"market"}}
PROFILE_NUMBERS = ("model_exit_slippage_bps", "account_checks_per_step", "poll_seconds", "test_live_max_lots")
PROFILE_FLAGS = ("hosted_stop", "hosted_stop_cancel_on_close", "bounded_extension", "resumed")
WINDOW_OUTCOMES = {"recovered", "recovery", "paused", "manual_review", "stopped", "restarted"}
EXTENSION_ENDS = {"limit_120s", "not_transient", "market_not_fresh", "unresolved_order", "recovery", "manual_review",
                  "recovered", "stopped", "paused", "flat", "restarted", "stop_unmatched"}  # stopped: also an owner Stop inside the extension
CHECKED_AT = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC")
TIMING_NUMBERS = ("steps", "step_p50_seconds", "step_p90_seconds", "step_max_seconds", "public_gets")
# R2.1.15 (Judge's condition 4): the events of a leveraged bot's position; new codes, so older bundles stay byte for byte
LEVERAGE_EVENTS = {"entry_position_facts", "liquidation_unknown", "liquidation_too_close", "liquidation_closer_than_stop",
                   "liquidation_detected"}
LEVERAGE_NUMBERS = ("leverage", "position_contracts", "avg_price", "notional_usdt", "notional_usd", "margin_usdt",
                    "liquidation_price", "liquidation_distance_bps", "required_distance_bps", "stop_loss_bps", "liquidation_reads",
                    "bill_rows", "contracts", "pnl_usdt", "fee_usdt", "venue_at_ms")
LIQUIDATION_KINDS = {"liquidation", "adl", "liquidation_and_adl"}


def leverage_of(state, strategy_id):
    """R2.1.15: a bot's row of the manifest's leverage block ({} for every season at 1x: their bundles stay byte for byte)."""
    block = (state.get("match_manifest") or {}).get("leverage")
    if not isinstance(block, dict):
        return {}
    row = next((b for b in block.get("bots", []) if isinstance(b, dict) and b.get("strategy_id") == strategy_id), None)
    if row is None or type(row.get("leverage")) is not int or type(row.get("limit")) is not int:
        return {}
    return {"leverage": row["leverage"], "leverage_limit": row["limit"],
            "margin_mode": "isolated" if block.get("margin_mode") == "isolated" else None}


def leverage_cohort(state):
    """R2.1.15: the league's and the public site's cohort key gets each bot's leverage only above 1x, so that 1x and 3x
    rounds on the same code never mix and every older cohort keeps its key."""
    block = (state.get("match_manifest") or {}).get("leverage")
    if not isinstance(block, dict):
        return {}
    return {"leverage": sorted([b.get("recipe_sha256"), b.get("leverage")] for b in block.get("bots", []) if isinstance(b, dict))}


def numbers(values, limit):
    return [number(v) for v in values[:limit]] if isinstance(values, list) else None


def live_evidence(out, data, event_code):
    """R2.1.10 (C, D136 p. 6): Live evidence written only under new keys or in new events, so that the bundles of
    older journals stay byte for byte (old keys such as request, result or unused_permissions stay unexported)."""
    from .exchange_api import CLIENT_ID
    from .exchange_engine import STOP_CODES
    if event_code == "exchange_order_intent" and isinstance(data.get("order_body"), dict):
        body, view = data["order_body"], {}
        for key, allowed in ORDER_BODY.items():
            value = body.get(key)
            if isinstance(value, str) and (allowed.fullmatch(value) if hasattr(allowed, "fullmatch") else value in allowed):
                view[key] = value
        view.update({k: number(body[k]) for k in ("sz", "px") if k in body and number(body[k]) is not None})
        if type(body.get("reduceOnly")) is bool:
            view["reduceOnly"] = body["reduceOnly"]
        if isinstance(body.get("clOrdId"), str) and CLIENT_ID.fullmatch(body["clOrdId"]):
            view["clOrdId"] = body["clOrdId"]  # built from the public match id (D136 p. 6)
        out["order_body"] = view
    if event_code == "feature_book" and type(data.get("gap_flat")) is int and data["gap_flat"] >= 0:
        out["gap_flat"] = data["gap_flat"]  # R219-GAP-MISSING
    if event_code == "fill_protected" and type(data.get("venue_stop_after_fill_ms")) is int:
        out["venue_stop_after_fill_ms"] = data["venue_stop_after_fill_ms"]
    if event_code in {"exchange_entry_fill", "exchange_close_fill", "exchange_order_read"} and data.get("order_state") in ORDER_STATES:
        out["order_state"] = data["order_state"]
    if event_code == "accounting_confirmed":
        if type(data.get("bills_reconciled")) is int and data["bills_reconciled"] >= 0:
            out["bills_reconciled"] = data["bills_reconciled"]
        if type(data.get("cash_delta_reconciled")) is bool:
            out["cash_delta_reconciled"] = data["cash_delta_reconciled"]
    if event_code == "protective_terminal" and isinstance(data.get("cancel_source_code"), str) and re.fullmatch(r"[0-9]{1,3}", data["cancel_source_code"]):
        out["cancel_source_code"] = data["cancel_source_code"]
    if event_code in {"stop_requested", "stop_renewed"} and data.get("reason_code") in STOP_CODES:
        out["reason_code"] = data["reason_code"]
    if event_code == "exit_trigger":
        if data.get("price_basis") in {"bid", "ask"}:
            out["price_basis"] = data["price_basis"]
        if data.get("hold_clock") in {"monotonic", "venue_fill_time"}:
            out["hold_clock"] = data["hold_clock"]
        out.update({k: number(data[k]) for k in EXIT_NUMBERS if k in data and number(data[k]) is not None})
    if event_code in LEVERAGE_EVENTS:
        out.update({k: number(data[k]) for k in LEVERAGE_NUMBERS if k in data})  # an unknown liqPx stays an explicit null
        if data.get("margin_mode") in {"isolated", "cross"}:
            out["margin_mode"] = data["margin_mode"]
        if "liquidation_source" in data:
            out["liquidation_source"] = (data["liquidation_source"] if data["liquidation_source"] in {"okx", "computed", "model_1x_long"}
                                         else None)
        if number(data.get("tier_mmr")) is not None:
            out["tier_mmr"] = number(data["tier_mmr"])
        if "distance_ok" in data:
            out["distance_ok"] = data["distance_ok"] if type(data["distance_ok"]) is bool else None
        if event_code == "liquidation_detected":
            out["kind"] = data.get("kind") if data.get("kind") in LIQUIDATION_KINDS else None
            if isinstance(data.get("bill_subtypes"), list):
                out["bill_subtypes"] = [v for v in data["bill_subtypes"] if isinstance(v, str) and re.fullmatch(r"1[0-2][0-9]", v)]
    if event_code == "leverage_confirmed":
        if number(data.get("lever")) is not None:
            out["lever"] = number(data["lever"])
        if data.get("mgn_mode") == "isolated":
            out["mgn_mode"] = "isolated"
    if event_code == "execution_profile":
        out.update({k: data[k] for k, allowed in PROFILE.items() if data.get(k) in allowed})
        out.update({k: number(data[k]) for k in PROFILE_NUMBERS if data.get(k) is not None and number(data[k]) is not None})
        out.update({k: data[k] for k in PROFILE_FLAGS if type(data.get(k)) is bool})
        if isinstance(data.get("transient_codes_checked_at"), str) and CHECKED_AT.fullmatch(data["transient_codes_checked_at"]):
            # R2.1.11 (R2110-ALLOWLIST): any date in the table's format, never "equal to this release's": a later check of
            # the table must not change the bundles of older journals (export allowlists only grow)
            out["transient_codes_checked_at"] = data["transient_codes_checked_at"]
        if isinstance(data.get("code_sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", data["code_sha256"]):
            out["code_sha256"] = data["code_sha256"]  # R2.1.11 (R2110-HASH-COVER): this event's own key, not HASHES
    if event_code in {"degraded_window_started", "degraded_window_ended"} and type(data.get("extended")) is bool:
        out["extended"] = data["extended"]  # A3/A4 (R2.1.10)
    if event_code == "degraded_window_started" and number(data.get("protected_age_seconds")) is not None:
        out["protected_age_seconds"] = number(data["protected_age_seconds"])
    if event_code == "degraded_window_ended":
        from .exchange_api import GET_PATHS, POST_PATHS
        from .exchange_diagnostics import public_reason
        if data.get("outcome") in WINDOW_OUTCOMES:
            out["outcome"] = data["outcome"]
        if data.get("extension_end") in EXTENSION_ENDS:
            out["extension_end"] = data["extension_end"]
        out.update({k: number(data[k]) for k in ("start_ms", "seconds", "failures", "skipped_decisions")
                    if data.get(k) is not None and number(data[k]) is not None})
        if isinstance(data.get("codes"), list):
            out["codes"] = [c for c in data["codes"][:10] if isinstance(c, str) and (re.fullmatch(r"[0-9]{1,8}", c) or public_reason(c) == c)]
        if isinstance(data.get("endpoints"), list):
            out["endpoints"] = [e for e in data["endpoints"][:10] if e in GET_PATHS | POST_PATHS]
        if isinstance(data.get("http_statuses"), list):
            out["http_statuses"] = [s for s in data["http_statuses"][:10] if type(s) is int and 100 <= s <= 599]
    if event_code == "degraded_extension_ended":
        if data.get("reason") in EXTENSION_ENDS:
            out["reason"] = data["reason"]
        out.update({k: number(data[k]) for k in ("seconds", "failures") if data.get(k) is not None and number(data[k]) is not None})
    if event_code == "step_timing":
        out.update({k: number(data[k]) for k in TIMING_NUMBERS if data.get(k) is not None and number(data[k]) is not None})
        for key in ("account_requests", "account_writes"):
            if numbers(data.get(key), 6) is not None:
                out[key] = numbers(data[key], 6)
        if isinstance(data.get("calibration_rtt_ms"), list):
            out["calibration_rtt_ms"] = [numbers(v, 2000) or [] for v in data["calibration_rtt_ms"][:6]]


def safe_data(data, *, event_code=None):
    """Allowlist, rather than recursive removal of guessed secret field names."""
    if not isinstance(data, dict):
        return {}
    out = {k: number(v) for k, v in data.items() if k in NUMERIC}
    out.update({k: v for k, v in data.items() if k in FLAGS and type(v) is bool})
    out.update({k: v for k, v in data.items() if k in TOKENS and isinstance(v, str) and safe_token(v)})
    out.update({k: v for k, v in data.items() if k in HASHES and isinstance(v, str)
                and len(v) == 64 and all(c in "0123456789abcdef" for c in v)})
    if isinstance(data.get("reasons"), list):
        out["reasons"] = [v for v in data["reasons"] if isinstance(v, str) and safe_token(v)]
    if "checks" in data:
        out["checks"] = checks(data["checks"])
    if "exit_checks" in data:
        out["exit_checks"] = checks(data["exit_checks"])
    if isinstance(data.get("features"), dict):
        from .strategy_v2 import MARKET_FEATURES
        out["features"] = {k: number(v) for k, v in data["features"].items() if k in MARKET_FEATURES}
    if isinstance(data.get("financial"), dict):
        out["financial"] = {k: number(v) for k, v in data["financial"].items() if k in {
            "position_btc", "equity_usdt", "pnl_usdt", "gross_usdt", "signed_fees_usdt", "signed_funding_usdt", "known_jev_usd"}}
        trades = data["financial"].get("trades")
        out["financial"]["trades"] = trades if type(trades) is int and trades >= 0 else None
    if type(data.get("settled")) is bool: out["settled"] = data["settled"]
    answer = data.get("answer")
    if isinstance(answer, dict):
        out["answer"] = {"choice": answer.get("choice") if answer.get("choice") in SAFE_VALUES else None,
                         "confidence": number(answer.get("confidence"))}
    from .g1_public_evidence import g1_event_evidence
    evidence = g1_event_evidence(data, event_code)
    if evidence is not None:
        out["g1_evidence"] = evidence
    # Preserve fixed diagnostic codes and measured clock evidence, not arbitrary
    # exception text. Old archives with omitted fields remain unknown.
    from .exchange_diagnostics import public_reason, STAGES, CLOCK_REASONS
    if event_code in {"exchange_error", "exchange_check_failed"}:
        reason = public_reason(data.get("reason"))
        if reason is not None:
            out["reason"] = reason
        if isinstance(data.get("stage"), str) and data["stage"] in STAGES:
            out["stage"] = data["stage"]
    live_evidence(out, data, event_code)
    if event_code == "account_checked" and isinstance(data.get("extra_permissions"), list):
        out["extra_permissions"] = [v for v in data["extra_permissions"] if v == "withdraw"]  # R2.1.10 (D-1), a new key
    if event_code in {"exchange_error", "exchange_check_failed", "fill_watch_failed", "degraded_window_started"}:
        # R2.1.10 (A1): new keys, absent from older journals, whose bundles keep their hashes
        from .exchange_api import GET_PATHS, POST_PATHS
        if data.get("endpoint") in GET_PATHS | POST_PATHS:
            out["endpoint"] = data["endpoint"]
        if data.get("method") in {"GET", "POST"}:
            out["method"] = data["method"]
        if type(data.get("http_status")) is int and 100 <= data["http_status"] <= 599:
            out["http_status"] = data["http_status"]
        if isinstance(data.get("okx_code"), str) and re.fullmatch(r"[0-9]{1,8}", data["okx_code"]):
            out["okx_code"] = data["okx_code"]
        if type(data.get("transient")) is bool:
            out["transient"] = data["transient"]
    if event_code == "clock_gap":
        if isinstance(data.get("reasons"), list):
            out["reasons"] = [v for v in data["reasons"] if isinstance(v, str) and v in CLOCK_REASONS][:3]
        for key in ("wall_delta_s", "mono_delta_s", "gap_s"):
            if number(data.get(key)) is not None:
                out[key] = number(data[key])
        for key in ("invalidate_temporal_state", "force_position_close"):
            if type(data.get(key)) is bool:
                out[key] = data[key]
    if event_code == "exchange_clock_synchronized":
        for key in ("server_minus_local_seconds", "round_trip_ms", "local_clock_jump_seconds", "calibration_lifetime_seconds"):
            if number(data.get(key)) is not None:
                out[key] = number(data[key])
        if type(data.get("windows_clock_modified")) is bool:
            out["windows_clock_modified"] = data["windows_clock_modified"]
    if isinstance(data.get("instrument"), str) and ORDER_BODY["instId"].fullmatch(data["instrument"]):
        out["instrument"] = data["instrument"]  # R2.1.12 (MIX, plan check p. 15): a mixed season's contract of the event
    # Full prompt, free-form model reasons, account facts and raw responses are never exported.
    return out


def event_view(event):
    return {"seq": event.get("seq"), "at": event.get("at"), "bot_id": event.get("bot_id"),
            "step": event.get("step"), "code": event.get("code"),
            "data": safe_data(event.get("data"), event_code=event.get("code"))}


def frame_view(frame):
    out = {k: frame.get(k) for k in ("at", "seq", "event_seq") if k in frame}
    bots = frame.get("bots", [])
    out["bots"] = []
    for bot in bots:
        if not isinstance(bot, dict): continue
        row = {"id": bot.get("id")}
        for key in BOT_FIELDS:
            if key not in bot: continue
            if key == "status": row[key] = bot[key] if isinstance(bot[key], str) and bot[key] in {"WAITING", "HOLDING", "LONG", "SHORT", "POSITION", "CLOSING", "DONE", "STOPPED", "PAUSED", "JEV_PENDING", "ENTRY_PENDING", "UNKNOWN"} else "UNKNOWN"
            elif key in {"entries", "trades"}: row[key] = bot[key] if type(bot[key]) is int and bot[key] >= 0 else None
            else: row[key] = number(bot[key])
        for key in G2_NUMBERS:
            if key in bot:
                row[key] = number(bot[key])
        if "entries_total" in bot:
            row["entries_total"] = bot["entries_total"] if type(bot["entries_total"]) is int and bot["entries_total"] >= 0 else None
        if "base_ccy" in bot:
            row["base_ccy"] = bot["base_ccy"] if isinstance(bot["base_ccy"], str) and COIN.fullmatch(bot["base_ccy"]) else None
        out["bots"].append(row)
    return out


def ledger(bot, own, trace, paper=False):
    """Delta observations and round-trip boundaries; late settlement stays separate.

    A cumulative fee revision with no new quantity is still a cost update. An
    unobserved final order result isn't retroactively assigned to a past fill.
    """
    aliases, rows, cumulative = {}, [], {}
    for fill in trace["fills"]:
        aliases.setdefault(fill["key"], "order-" + str(len(aliases) + 1))
    for event in own:
        if event.get("code") in {"exchange_funding", "model_funding"}:
            data = event.get("data") or {}
            if paper:
                value = number(data.get("signed_amount_usdt"))
            else:
                total = number(data.get("signed_funding_usdt"))
                previous = cumulative.get("funding", "0")
                value = str(Decimal(total) - Decimal(previous)) if total is not None and previous is not None else None
                cumulative["funding"] = total
            rows.append({"seq": event.get("seq"), "at": event.get("at"), "role": "funding",
                         "signed_funding_usdt": value, "source": "paper_model" if paper else "actual_bill_observation"})
    trips, active = [], None
    for fill in trace["fills"]:
        event, role = fill["event"], fill["role"]
        data = event.get("data") or {}
        row = {"seq": event.get("seq"), "at": event.get("at"), "role": role,
               "order": aliases[fill["key"]], "quantity": number(fill["delta"]),
               "position_before": number(fill["before"]), "position_after": number(fill["after"]),
               "source": "paper_incremental_btc" if paper else "exchange_incremental_contracts"}
        if paper:
            row.update(price_usdt=number(data.get("price_usdt")),
                       signed_fees_usdt=str(-Decimal(str(data["fee_usdt"]))) if number(data.get("fee_usdt")) is not None else None,
                       gross_pnl_usdt=number(data.get("gross_pnl_usdt", 0)))
        else:
            result = data.get("result") or {}
            prior = cumulative.setdefault(fill["key"], {"signed_fee_usdt": "0", "gross_pnl_usdt": "0"})
            for field, output in (("signed_fee_usdt", "signed_fees_usdt"), ("gross_pnl_usdt", "gross_pnl_usdt")):
                value, before = number(result.get(field)), prior.get(field)
                row[output] = str(Decimal(value) - Decimal(before)) if value is not None and before is not None else None
                prior[field] = value
            row["price_usdt"] = number(result.get("average_price"))
        rows.append(row)
        if role == "entry" and active is None:
            active = {"number": len(trips) + 1, "entry_seq": row["seq"], "entry_at": row["at"], "fill_seqs": []}
        if active is not None:
            active["fill_seqs"].append(row["seq"])
            if role == "close" and fill["after"] == 0:
                active.update(close_seq=row["seq"], close_at=row["at"], closed=True)
                trips.append(active)
                active = None
    if active:
        trips.append({**active, "closed": False})
    return {"rows": sorted(rows, key=lambda r: r.get("seq") or 0), "round_trips": trips,
            "inventory_known": trace["inventory_known"], "remaining": number(trace["owned"]),
            "missing_order_results": trace["missing_order_results"], "unresolved": trace["unresolved"],
            "limitations": trace["limitations"],
            "cost_allocation": "Observed deltas only. Final reconciled totals below include later bill/fee revisions."}


def mixed_instrument(state, bot_id):
    """R2.1.12 (MIX): the participant's contract in a mixed season, else None (a one-contract season: as before)."""
    rules = state.get("rules") or {}
    return (rules.get("instruments") or {}).get(bot_id) if rules.get("instrument") == "MIXED" else None


def quality(state, own, bot_id=None):
    checks_events = [e for e in own if e.get("code") in {"entry_checks", "position_checks"}]
    bars = {(e.get("data") or {}).get("bar_time_ms") for e in checks_events}
    bars.discard(None)
    timed = [(e.get("data") or {}).get("elapsed_ms") for e in own if e.get("code") in {"jev_decision", "jev_gate"}]
    contract = mixed_instrument(state, bot_id)  # R2.1.12 RC2 (X2-05): a participant without events is still on its contract
    gaps = [e.get("seq") for e in state.get("events", []) if e.get("code") in {"data_gap", "market_unavailable", "clock_gap", "clock_skew"}
            and (contract is None or (e.get("data") or {}).get("instrument") in {None, contract})]  # R2.1.12 (MIX, p. 15)
    exits = [e for e in own if e.get("code") in {"exit_trigger", "position_exit_trigger"}]
    return {"distinct_closed_bars": len(bars) if bars else None,
            "distinct_closed_bars_source": "recorded_bar_time_ms" if bars else "not_recorded",
            "jev_latency_ms": [number(x) for x in timed if number(x) is not None],
            "gap_event_seqs": gaps,
            "exit_trigger_evidence": [event_view(e) for e in exits],
            "exact_exit_trigger_recorded": bool(exits),
            "complete_history": state.get("history_complete", not state.get("_history_compact", False)),
            "data_status": "COMPLETE" if not state.get("incomplete") and not state.get("_history_compact") else "INCOMPLETE",
            "timing_limitations": ["Missing trigger snapshots, request payloads or as-of times are not reconstructed as observed facts."]}


def build_review(state, *, include_request_payloads=False, snapshot_context=None, snapshot_context_status=None):
    if not isinstance(state, dict) or state.get("mode") not in {"PAPER", "OKX_DEMO", "OKX_LIVE"}:
        raise ArenaError("Непідтримуваний журнал бою.")
    events = state.get("events", [])
    seqs = [e.get("seq") for e in events]
    if any(type(s) is not int for s in seqs) or any(b != a + 1 for a, b in zip(seqs, seqs[1:])):
        raise ArenaError("Непослідовний журнал; звіт не підтверджено.")
    paper = state["mode"] == "PAPER"
    recipes = {r.get("id"): r for r in state.get("strategies", [])}
    coverage = {b["bot_id"]: b for b in (lifecycle(state) or {}).get("bots", [])}
    participants = []
    for bot in state.get("bots", []):
        if bot.get("id") == "cash":
            continue
        own = [e for e in events if e.get("bot_id") == bot.get("id")]
        recipe = recipes.get(bot.get("strategy_id") or bot.get("id"), {})
        if paper:
            own = _paper_events(own)
        trace = _paper_trace(bot, recipe, own) if paper else _order_trace(bot, own)
        costs = _paper_costs(state, bot, own, trace) if paper else _costs(bot, own, trace)
        funnel = _funnel(own, trace)
        failed = Counter(c.get("feature", "unknown") for e in own for c in (e.get("data") or {}).get("checks", []) if c.get("passed") is False)
        cov = coverage.get(bot.get("id"), {})
        lever = leverage_of(state, bot.get("strategy_id") or bot.get("id"))
        if lever and (state.get("rules") or {}).get("test_live_max_lots") and recipe.get("strategy_schema_version") == 4:
            lots = state["rules"]["test_live_max_lots"]  # G12 (FR-B-01, D162 p. 2): one lot whatever the leverage
            unit = number((bot.get("_instrument") or {}).get("minimum_order_units"))
            lever["size_cap"] = {"lots": lots, "units": number(Decimal(unit) * lots) if unit is not None else None}
        participants.append({"id": bot.get("id"), "name": bot.get("name"),
                             **({"instrument": mixed_instrument(state, bot.get("id"))} if mixed_instrument(state, bot.get("id")) else {}),
                             **({"leverage": lever} if lever else {}),
                             "recipe_sha256": recipe.get("recipe_sha256"), "version": recipe.get("version"),
                             "status": bot.get("status"), "position_btc": number(bot.get("position_btc")),
                             "allocation_usdt": number(state.get("budget_usdt")),
                             "funnel": {**funnel, "failed_features": dict(failed)}, "costs": costs,
                             "trade_ledger": ledger(bot, own, trace, paper), "quality": quality(state, own, bot.get("id")),
                             "coverage": {k: copy.deepcopy(cov.get(k)) for k in ("rows", "complete", "required_count", "observed_required_count", "limitations")}})
    match = {k: state.get(k) for k in ("id", "mode", "execution_mode", "status", "started_at", "admission_ends_at", "ended_at", "duration_seconds", "time_policy", "clock_policy", "incomplete", "code_sha256")}
    match.update(started_at=state.get("started_at", state.get("created_at")),
                 admission_ends_at=state.get("admission_end_at"), ended_at=state.get("finished_at"))
    match["manifest_sha256"] = state.get("match_manifest_sha256", state.get("manifest_sha256"))
    match["phase"] = state.get("phase", state.get("status"))
    instrument = (state.get("rules") or {}).get("instrument")
    if instrument and instrument != "BTC-USDT-SWAP":  # R2: an ETH season names its coin; BTC projections stay byte-identical
        match["instrument"] = instrument
    if instrument == "MIXED":  # R2.1.12 (MIX, plan check p. 17): a sorted list, never {bot_id: ...} (live_packets)
        match["instruments"] = sorted(set((state["rules"].get("instruments") or {}).values()))
    block = (state.get("match_manifest") or {}).get("leverage")
    if isinstance(block, dict) and isinstance(block.get("table_sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", block["table_sha256"]):
        # R2.1.15 (plan check p. 4): the table of the limits each bot's leverage was checked against
        match["leverage_table_sha256"] = block["table_sha256"]
    # Preserve legacy projections byte-for-byte. New rules travel with the immutable match.
    policy = (state.get("match_manifest") or {}).get("score_policy")
    if "score_policy" in (state.get("match_manifest") or {}) and not (
            isinstance(policy, dict) and policy == {"primary": "RECONCILED_NET_USDT",
                "tie_tolerance_usdt": "0.00000001", "include_cash": True}):
        match["score_policy"] = copy.deepcopy(policy)
    bundle = {"schema_version": VERSION, "match": match, "participants": participants,
              "events": [event_view(e) for e in events], "frames": [frame_view(f) for f in state.get("frames", [])],
              "cash": {"name": "Cash", "net_usdt": "0", "source": "fixed_no_trade_baseline"},
              "projection": "author_equal_v1_allowlist", "history_complete": state.get("history_complete", not state.get("_history_compact", False)),
              "limitations": ["Jev USD is separate from trading USDT. PAPER is a model. A missing result isn't zero."]}
    for row in participants:
        settled = row["costs"].get("complete") and number(row["position_btc"]) is not None and Decimal(number(row["position_btc"])) == 0
        available = row["quality"]["data_status"] == "COMPLETE"
        row["quality"].update(settlement={"status": "SETTLED" if settled else "PENDING"},
            availability={"status": "COMPLETE" if available else "INCOMPLETE"},
            competitive_validity={"status": "ELIGIBLE" if settled and available and state["status"] == "DONE" and bundle["history_complete"] else "INELIGIBLE"})
        if isinstance(policy, dict) and policy.get("id") == "filled_entry_net_v1":
            from .match_protocol import entry_participation
            participation = entry_participation(row["funnel"])
            participation["eligible_to_win"] &= row["quality"]["competitive_validity"]["status"] == "ELIGIBLE"
            row["participation"] = participation
    if include_request_payloads:
        requests = []
        for event in events:
            data = event.get("data") or {}
            payload = data.get("payload")
            if event["code"] not in {"jev_request", "jev_shadow_payload"} or not isinstance(payload, dict): continue
            shadow = event["code"] == "jev_shadow_payload"
            if shadow and data.get("sent") is not False: raise ArenaError("Shadow-запит не позначено як ненадісланий.")
            record = request_evidence(payload, data, unsent_shadow=shadow)
            requests.append({"seq": event["seq"], "requested_at_ms": data.get("request_at_ms"), **record,
                             **({"sent": False, "candidate_ids": copy.deepcopy(data.get("candidate_ids", []))} if shadow else {})})
        bundle.update(request_payloads=requests, projection="private_author_equal_v1")
        if any(r["wire_hash_status"] == "NOT_RETAINED" for r in requests):
            bundle["request_payload_limitations"] = ["Старий журнал зберіг значення JSON, але не вихідний порядок ключів. Зміст перевірено; початкові wire bytes та їх SHA не відтворюємо як факт."]
    if isinstance(state.get("degraded_windows"), list):  # R2.1.11 (A4): journals started by R2.1.11 only (D138 p. 3)
        bundle["degraded_windows"] = [{"bot_id": w.get("bot_id") if isinstance(w.get("bot_id"), str) else None,
                                       **safe_data(w, event_code="degraded_window_ended")}
                                      for w in state["degraded_windows"] if isinstance(w, dict)]
    from .review_diagnostics import decorate
    decorate(bundle, snapshot_context, snapshot_context_status)
    bundle["integrity"] = {"bundle_sha256": digest(bundle), "source_event_count": len(events)}
    return bundle


def schema4_rules(rules):
    """(clean rules, leaf feature keys) of a schema-4 row, or (None, empty set) for V1/V2. The row's rules must be
    exactly the engine form of valid schema-4 rules, so no prose or extra field can ride along; its checks may name
    only its own predicate leaves besides the shared ones (post_only_limit_usdt is its order's limit)."""
    entry = rules.get("entry") if isinstance(rules, dict) else None
    if not (isinstance(entry, dict) and ("when" in entry or any(isinstance(b, dict) and "when" in b for b in entry.values()))):
        return None, set()
    from .strategy_v4 import RULE_KEYS, feature_key, validate_rules
    from .strategy_v4_runtime import engine_rules
    clean = validate_rules({key: rules[key] for key in RULE_KEYS})
    engine = dict(engine_rules(clean))
    for name in ("entry", "exit"):  # the gate payload's compact all/any groups (arena_jev.make_payload)
        group = engine[name]
        if isinstance(group, dict) and "conditions" in group:
            engine[name] = [group["mode"], [[c["feature"], c["op"], c["value"]] for c in group["conditions"]]]
    if engine != rules:
        raise ValueError("schema4_rules_differ")
    leaves = {"post_only_limit_usdt"}
    def walk(node):
        if isinstance(node, dict):
            for key in ("feature", "ref"):
                if isinstance(node.get(key), dict): leaves.add(feature_key(node[key]))
            for child in node.values(): walk(child)
        elif isinstance(node, list):
            for child in node: walk(child)
    walk(clean["entry"])
    return clean, leaves


def validate_request(payload):
    """Rebuild the fixed numeric gate payload; unknown prose/fields fail closed."""
    from .arena_jev import make_payload
    from .arena_strategies import catalogue_defaults, validate_strategy
    from .strategy_v2 import MARKET_FEATURES, POSITION_FEATURES, known
    try:
        if set(payload) != {"model", "state", "questions"}: raise ValueError()
        state = payload["state"]
        features = state["features"]
        if not isinstance(features, dict) or set(features) - MARKET_FEATURES or any(v is not None and not known(v) for v in features.values()): raise ValueError()
        base_columns = ["feature", "op", "expected", "actual", "passed"]
        columns = state["check_columns"]
        if columns not in (base_columns, base_columns + ["path", "side", "rhs_feature"]): raise ValueError()
        candidates = []
        for index in range(6):
            key = "c" + str(index)
            if key not in state: break
            row = state[key]
            if set(row) - {"recipe_sha256", "side", "kind", "position_btc", "local_checks_passed", "checks", "rules", "test_plan", "plan", "diagnostic_policy", "rule_schema_version"}: raise ValueError()
            sha = row["recipe_sha256"]
            if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha): raise ValueError()
            if row["side"] not in {"long", "short"} or not known(row["position_btc"]) or type(row["local_checks_passed"]) is not bool: raise ValueError()
            v4_rules, leaves = schema4_rules(row["rules"])  # a schema-4 Jev SHADOW row (H10's RULES_ONLY mode), else None
            checked = []
            for values in row["checks"]:
                if not isinstance(values, list) or len(values) != len(columns): raise ValueError()
                feature, op, expected, actual, passed = values[:5]
                if feature not in MARKET_FEATURES | POSITION_FEATURES | leaves | {"total_entries", "position_entries", "gain_bps", "position_gain_bps", "equity_exposure", "formal_exit", "position_age", "diagnostic_age_seconds", "probe_jev_requests", "probe_entry_intents", "entry_window_age_seconds", "probe_hold_checked"} or op not in {"lt", "lte", "gt", "gte", "eq"}: raise ValueError()
                if any(v is not None and not known(v) for v in (actual, expected)) or passed is not None and type(passed) is not bool: raise ValueError()
                if len(columns) == 8:
                    path, side, rhs = values[5:]
                    if path is not None and (not isinstance(path, str) or len(path) > 128 or not re.fullmatch(r"(?:/(?:all|any)/\d{1,2}|/not)*", path)): raise ValueError()
                    if side not in {None, "long", "short"} or rhs is not None and rhs not in MARKET_FEATURES | POSITION_FEATURES: raise ValueError()
                checked.append(dict(zip(columns, values)))
            rules = copy.deepcopy(row["rules"])
            legacy = isinstance(rules["entry"], list)
            if v4_rules is not None:
                pass  # exactly the engine form of valid schema-4 rules (schema4_rules)
            elif legacy:
                for name in ("entry", "exit"):
                    mode, leaves = rules[name]
                    rules[name] = {"mode": mode, "conditions": [{"feature": c[0], "op": c[1], "value": c[2]} for c in leaves]}
            if v4_rules is None:
                from .strategy_contract import payload_of
                sample = payload_of(catalogue_defaults()[0])
                sample["rules"] = rules
                if not legacy: sample["strategy_schema_version"] = 2
                validate_strategy(sample)
            candidates.append({"id": key, **{k: row[k] for k in ("recipe_sha256", "side", "kind", "position_btc", "local_checks_passed")},
                "checks": checked, "rules": rules, "features": features,
                **{k: row[k] for k in ("test_plan", "plan") if k in row}})
        if not candidates: raise ValueError()
        expected = make_payload(candidates, state["mode"])
        if columns == base_columns and "v2_rule_format" in expected["state"]:
            # Read the exact pre-D066 V2 wire shape; never rewrite its journal or
            # claim that its old description of V2 was the corrected policy.
            baseline = {**candidates[0], "rules": catalogue_defaults()[0]["rules"]}
            expected["state"]["policy"] = make_payload([baseline], state["mode"])["state"]["policy"]
            expected["state"].pop("v2_rule_format")
            expected["state"]["check_columns"] = base_columns
            for index in range(len(candidates)):
                row = expected["state"]["c" + str(index)]
                row.pop("rule_schema_version")
                row["checks"] = [c[:5] for c in row["checks"]]
        if expected != payload: raise ValueError()
    except (KeyError, ValueError, TypeError, IndexError):
        raise ArenaError("Запит Jev містить непідтримані або приватні поля.")


def request_evidence(payload, data, *, unsent_shadow=False):
    """Preserve wire proof when retained, explicitly mark old sorted JSON as unknown."""
    validate_request(payload)
    raw = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    sha = data.get("payload_sha256")
    limit = 65536 if unsent_shadow else 8000
    if len(raw) > limit or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ArenaError("Невалідний розмір або хеш запиту Jev.")
    content_sha = digest(payload)
    if "payload_content_sha256" in data and data["payload_content_sha256"] != content_sha:
        raise ArenaError("Хеш змісту запиту Jev не збігається.")
    result = {"payload_sha256": sha, "payload_content_sha256": content_sha, "payload": copy.deepcopy(payload)}
    if "payload_wire_json" in data:
        wire = data["payload_wire_json"]
        try:
            decoded = json.loads(wire) if isinstance(wire, str) else None
        except ValueError:
            raise ArenaError("Невалідні wire bytes запиту Jev.")
        if (not isinstance(wire, str) or len(wire.encode()) > limit
                or hashlib.sha256(wire.encode()).hexdigest() != sha or decoded != payload):
            raise ArenaError("Хеш або зміст точних wire bytes Jev не збігається.")
        result.update(payload_wire_json=wire, wire_hash_status="VERIFIED_WIRE")
    else:
        result["wire_hash_status"] = "VERIFIED_SERIALIZATION" if hashlib.sha256(raw).hexdigest() == sha else "NOT_RETAINED"
    return result


def verify_bundle(bundle):
    if not isinstance(bundle, dict) or bundle.get("schema_version") != VERSION:
        raise ArenaError("Непідтримуваний формат author bundle.")
    original = copy.deepcopy(bundle)
    integrity = original.pop("integrity", {})
    if digest(original) != integrity.get("bundle_sha256"):
        raise ArenaError("Хеш author bundle не збігається.")
    events = bundle.get("events", [])
    if len(events) != integrity.get("source_event_count"):
        raise ArenaError("Обірвано author bundle.")
    ordered = [e.get("seq") for e in events]
    if any(type(s) is not int for s in ordered) or any(b != a + 1 for a, b in zip(ordered, ordered[1:])):
        raise ArenaError("Непослідовний author bundle.")
    seqs = set(ordered)
    request_statuses = []
    event_by_seq = {e["seq"]: e for e in events}
    for request in bundle.get("request_payloads", []):
        if request.get("seq") not in seqs: raise ArenaError("Відсутній доказ запиту Jev.")
        event = event_by_seq[request["seq"]]
        shadow = event["code"] == "jev_shadow_payload"
        if event["code"] not in {"jev_request", "jev_shadow_payload"} or (event.get("data") or {}).get("payload_sha256") != request.get("payload_sha256"):
            raise ArenaError("Запит Jev не відповідає події журналу.")
        if shadow and (request.get("sent") is not False or (event.get("data") or {}).get("sent") is not False):
            raise ArenaError("Ненадісланий Shadow-запит позначено неправильно.")
        checked = request_evidence(request["payload"], request, unsent_shadow=shadow)
        content_pin = (event.get("data") or {}).get("payload_content_sha256")
        if content_pin is not None and content_pin != checked["payload_content_sha256"]:
            raise ArenaError("Зміст запиту Jev не відповідає події журналу.")
        if request.get("wire_hash_status", checked["wire_hash_status"]) != checked["wire_hash_status"]:
            raise ArenaError("Невірно позначено wire-доказ запиту Jev.")
        request_statuses.append(checked["wire_hash_status"])
    for bot in bundle.get("participants", []):
        for row in bot.get("trade_ledger", {}).get("rows", []):
            if row.get("seq") not in seqs:
                raise ArenaError("Відсутній доказ для рядка угоди.")
        costs = bot.get("costs", {})
        fields = [number(costs.get(k)) for k in ("gross_pnl_usdt", "signed_fees_usdt", "signed_funding_usdt", "net_usdt")]
        if all(v is not None for v in fields) and sum(map(Decimal, fields[:3])) != Decimal(fields[3]):
            raise ArenaError("Місток gross/fees/funding/net не збігається.")
    return {"verified": True, "bundle_sha256": integrity["bundle_sha256"], "events": len(events), "participants": len(bundle.get("participants", [])),
            "request_wire_integrity_complete": all(s != "NOT_RETAINED" for s in request_statuses)}
