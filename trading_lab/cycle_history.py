"""Allowlisted, read-only timeline projection. Never expose raw account/order records."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re

NUMBERS = set("elapsed_ms cost_usd confidence server_ms return_1m_bps return_5m_bps return_15m_bps "
    "sma5_usdt sma20_usdt last_close_usdt bid_usdt ask_usdt spread_bps funding_rate seconds_to_funding "
    "closed_bars bar_seconds mean_range_14m_bps volume_last5_usdt volume_previous5_usdt "
    "assumed_roundtrip_fee_bps slippage_allowance_bps position_return_bps held_seconds entries_used "
    "contracts filled_contracts average_price_usdt limit_price_usdt fee_usdt remaining_contracts "
    "return_bps estimated_close_pnl_usdt pnl_usdt signed_fees_usdt maximum_entry_notional_usdt "
    "next_review_seconds cycle_seconds review_seconds check_seconds max_decisions max_entries "
    "max_notional_usdt leverage observed_server_ms last_bar_close_server_ms remaining_cycle_seconds".split())
FLAGS = {"baseline_candidate", "may_add", "reduce_only", "adding", "attempted"}
CODES = {"choice", "reason", "phase", "direction", "order_state"}


def code(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_]{1,120}", value) else None


def numeric(value):
    try:
        number = Decimal(str(value))
        return format(number, "f") if number.is_finite() and abs(number) < Decimal("1e15") else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def details(data):
    if not isinstance(data, dict):
        return {}
    result = {}
    for key, value in data.items():
        clean = numeric(value) if key in NUMBERS else value if key in FLAGS and type(value) is bool else code(value) if key in CODES else None
        if clean is not None:
            result[key] = clean
    return result


def timestamp(value, milliseconds=False):
    try:
        date = datetime.fromtimestamp(float(value) / 1000, timezone.utc) if milliseconds else datetime.fromisoformat(value)
        return date.isoformat(timespec="milliseconds") if date.tzinfo else None
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def project_history(state):
    full = state.get("timeline_version") == 1
    events = []
    if full:
        for event in state.get("events", []):
            if not isinstance(event, dict) or type(event.get("step")) is not int or event["step"] not in range(1, 7):
                continue
            events.append({"seq": len(events) + 1, "at": timestamp(event.get("at")), "step": event["step"],
                           "code": code(event.get("code")), "status": code(event.get("status")),
                           "data": details(event.get("data"))})
    else:
        # Old runs lack check/transition timestamps. Show only evidence that actually exists.
        def add(step, name, data, stamp=None):
            events.append({"seq": len(events) + 1, "at": timestamp(stamp, True), "step": step,
                           "code": name, "status": "recorded", "data": details(data)})
        decisions = state.get("decisions", [])
        if not decisions and state.get("jev"):
            decisions = [{"stage": "entry", **state["jev"]}]
        for decision in decisions:
            market = decision.get("market") or {}
            add(5 if decision.get("stage") == "position" else 2, "legacy_decision", {**market, **decision}, market.get("observed_server_ms"))
        for step, records in ((3, state.get("entries", [state.get("entry")])), (6, state.get("exits", []))):
            for record in records:
                if not record:
                    continue
                request, result = record.get("request") or {}, record.get("result") or {}
                add(step, "legacy_order", {"direction": request.get("side"), "contracts": request.get("sz"),
                    "reduce_only": request.get("reduceOnly"), "order_state": result.get("state"),
                    "filled_contracts": result.get("accFillSz"), "average_price_usdt": result.get("avgPx"),
                    "fee_usdt": result.get("fee") if result.get("feeCcy") == "USDT" else None}, result.get("uTime"))
        add(6, "legacy_result", {"phase": state.get("phase"), "reason": state.get("close_reason") or state.get("last_error"),
            "remaining_contracts": state.get("remaining_contracts"), "pnl_usdt": state.get("pnl_excluding_funding_usdt"),
            "signed_fees_usdt": state.get("signed_fees_usdt")})
    revision = state.get("code_sha256")
    return {"mode": "recorded_steps" if full else "legacy_partial", "events": events,
            "limits": details(state.get("limits")),
            "code_sha256": revision if isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{64}", revision) else None}
