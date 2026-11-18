"""One registered Jev review of public OKX data; never an execution signal."""

import hashlib
import json
from pathlib import Path
import sqlite3
import time
import urllib.error

from .jev import BudgetLedger, MAX_BODY_BYTES, MODEL, read_key, send_once
from .okx_readonly import PreflightError, decimal_value, numeric_text, preflight
from .risk import finite_number

EXPERIMENT = "E019-okx-public-review-v1"
LEDGER_NAME = "jev_okx_public_review_v1.sqlite3"
MAX_REVIEW_AGE_SECONDS = 30


def public_review_state(report):
    """Construct an explicit public-field allowlist; never copy account data."""
    if report.get("status") != "public_checks_passed" or not report.get("public_data_verified"):
        raise PreflightError("public_preflight_required")
    instrument, quote = report["instrument"], report["quote"]
    if (instrument.get("instrument") != "BTC-USDT-SWAP"
            or instrument.get("instrument_type") != "SWAP"
            or instrument.get("size_unit") != "contracts"
            or instrument.get("state") != "live"):
        raise PreflightError("unsupported_review_instrument")
    size, step, value, multiplier, base, bid, ask = (
        decimal_value(item, positive=True) for item in (
            instrument["minimum_order_units"], instrument["lot_step"],
            instrument["contract_value"], instrument["contract_multiplier"],
            instrument["minimum_base_btc"], quote["bid_usdt"], quote["ask_usdt"]))
    if size % step != 0 or size * value * multiplier != base or bid > ask:
        raise PreflightError("inconsistent_contract_arithmetic")
    age = quote["age_seconds_using_server_clock"]
    if not finite_number(age) or not -2 <= age <= 10:
        raise PreflightError("stale_public_quote")
    fee = decimal_value("0.0005")  # Declared scenario assumption, not private account data.
    return {
        "purpose": "technical_consistency_review_only",
        "market_data": "observed_public_OKX_snapshot_not_synthetic",
        "instrument": "BTC-USDT-SWAP",
        "contract_type": "linear_USDT_settled_BTC",
        "contract_value_btc": numeric_text(value),
        "contract_multiplier": numeric_text(multiplier),
        "minimum_contracts": numeric_text(size),
        "lot_step_contracts": numeric_text(step),
        "proposed_test_quantity_contracts": numeric_text(size),
        "proposed_test_quantity_btc": numeric_text(base),
        "bid_usdt": numeric_text(bid), "ask_usdt": numeric_text(ask),
        "reference_entry_notional_usdt": numeric_text(base * ask),
        "assumed_taker_fee_rate_per_side": numeric_text(fee),
        "fee_assumption_is_not_account_verification": True,
        "estimated_round_trip_fees_at_snapshot_usdt": numeric_text(base * (ask + bid) * fee),
        "estimated_spread_cost_usdt": numeric_text(base * (ask - bid)),
        "slippage_funding_price_movement_excluded": True,
        "scenario": "user_operated_buy_then_close_same_filled_quantity",
        "proposed_margin_mode": "isolated",
        "proposed_leverage": 1,
        "account_margin_leverage_and_algos_not_verified_by_this_review": True,
        "orders_submitted": 0,
        "live_execution_enabled": False,
    }


def review_public_snapshot(root, reader, *, transport=send_once, key_reader=read_key,
                           timer=time.monotonic, clock=time.time):
    root = Path(root)
    report = {"experiment": EXPERIMENT, "status": "blocked", "verdict": "needs_review",
              "live_execution_enabled": False, "orders_submitted": 0, "jev_calls": 0,
              "financial_cycle_completed": False, "profitability_evaluated": False,
              "private_account_data_sent": False}
    ledger = BudgetLedger(root / "runtime" / LEDGER_NAME, max_attempts=1)
    report["budget"] = ledger.summary()
    # A repeated CLI command must not silently create another paid request or new ledger.
    if report["budget"]["attempts"]:
        report["reason"] = "registered_attempt_already_consumed"
        return report
    started = timer()
    market = preflight(reader, "swap", False)
    report["market"] = market
    try:
        state = public_review_state(market)
        report["reviewed_public_state"] = state
        body = json.dumps({"model": MODEL, "state": state, "questions": {
            "technical_consistency": {
                "type": "choice",
                "instructions": (
                    "Check only contract units and the declared cost arithmetic in this snapshot. "
                    "This is a technical integration test, not permission to trade or a price forecast. "
                    "Account readiness, leverage configuration and pending algo orders are outside "
                    "this review. Do not invent them or interpret confidence as probability of profit. "
                    "Do not change the scenario, size or leverage. Choose needs_review if the supplied "
                    "units or arithmetic are inconsistent or uncertain."
                ),
                "criteria": {
                    "consistent": "Supplied contract units and declared scenario arithmetic are internally consistent.",
                    "needs_review": "Units, arithmetic or the declared scenario are inconsistent or uncertain.",
                },
            }}}, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(body) > MAX_BODY_BYTES:
            raise ValueError("payload_limit")
        key = key_reader(root)
        if timer() - started > MAX_REVIEW_AGE_SECONDS:
            raise ValueError("stale_review")
    except (KeyError, ValueError, TypeError, OSError, ArithmeticError):
        report["reason"] = "invalid_public_snapshot_or_missing_key"
        return report
    digest = hashlib.sha256(body).hexdigest()
    report["request_sha256"] = digest
    try:
        reservation = ledger.reserve(digest, clock())
    except sqlite3.Error:
        reservation = None
    if reservation is None:
        report["reason"] = "request_budget_or_ledger_unavailable"
        return report
    request_started = timer()
    report["jev_calls"] = 1
    failure = None
    try:
        result = transport(body, key)
    except urllib.error.HTTPError as error:
        failure = "http_" + str(error.code)
        error.close()
        result = {}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, TypeError):
        failure, result = "request_failed_billing_unconfirmed", {}
    report["elapsed_ms"] = round((timer() - request_started) * 1000, 3)
    result = result if isinstance(result, dict) else {}
    usage, answers = result.get("usage"), result.get("answers")
    raw_cost = usage.get("cost") if isinstance(usage, dict) else None
    cost = raw_cost if finite_number(raw_cost) and raw_cost >= 0 else None
    answer = answers.get("technical_consistency") if isinstance(answers, dict) else None
    answer = answer if isinstance(answer, dict) else {}
    choice, confidence = answer.get("choice"), answer.get("confidence")
    valid = (isinstance(choice, str) and choice in {"consistent", "needs_review"}
             and finite_number(confidence) and 0.70 <= confidence <= 1)
    age = timer() - started
    reason = failure or ("review_snapshot_expired" if not 0 <= age <= MAX_REVIEW_AGE_SECONDS else None)
    reason = reason or (None if valid else "invalid_or_low_confidence_answer")
    reason = reason or ("billing_unconfirmed" if cost is None else None)
    reason = reason or ("reported_cost_above_review_threshold" if cost is not None and cost > .005 else None)
    try:
        ledger.finish(reservation, cost, reason or "valid_technical_review")
    except sqlite3.Error:
        reason = "ledger_write_failed"
    report.update(status="review_completed" if reason is None else "blocked",
                  verdict=choice if reason is None else "needs_review",
                  reason=reason or "snapshot_only_not_trade_authorization",
                  reported_cost_usd=cost, snapshot_elapsed_seconds=round(age, 3),
                  confidence=confidence if finite_number(confidence) and 0 <= confidence <= 1 else None)
    response_model = result.get("model")
    report["response_model"] = response_model if (isinstance(response_model, str)
        and response_model.startswith(MODEL) and len(response_model) < 80) else None
    report["budget"] = ledger.summary()
    return report
