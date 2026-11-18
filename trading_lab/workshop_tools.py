"""Pure Workshop lint/explain/lot/paired report helpers. No I/O/API."""
from copy import deepcopy
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import math

from .strategy_v2 import (ContractError, MARKET_FEATURES, POSITION_FEATURES,
                                    evaluate_predicate, known, validate_rules)


def lint_rules(rules):
    """A valid DSL need not be profitable; warnings never silently alter a recipe."""
    clean = validate_rules(rules)
    warnings = []
    if clean["max_hold_seconds"] is None:
        warnings.append({"code": "authored_hold_unbounded", "effect": "admission deadline never closes position"})
    if clean["max_entries"] > 1 and clean["add_when"] is None:
        warnings.append({"code": "no_formal_add_predicate", "effect": "new V2 add is disabled"})
    if clean["max_entries"] == 1 and clean["add_when"] is not None:
        warnings.append({"code": "add_disabled_by_entry_count", "effect": "max_entries=1 blocks all adds"})
    if clean["cost_checkpoint"] and clean["max_hold_seconds"] is not None and (
            clean["cost_checkpoint"]["after_seconds"] >= clean["max_hold_seconds"]):
        warnings.append({"code": "checkpoint_after_authored_hold", "effect": "authored hold exit has priority"})
    return {"valid": True, "rules": clean, "warnings": warnings,
            "limitations": ["Static reachability of numeric predicates is not proved.",
                            "Formal validation and synthetic examples are not evidence of edge."]}


def explain_predicate(predicate, values):
    """Return root verdict plus every leaf; scalar margins are not probabilities.

    The nearest threshold is diagnostic only: an any/not subtree can pass while
    a leaf fails. Never label the failed-leaf count a set of admission vetoes.
    """
    verdict, checks = evaluate_predicate(predicate, values)
    leaves = []
    for check in checks:
        margin = None
        if known(check["actual"]) and known(check["expected"]):
            direction = 1 if check["op"] in {"gt", "gte"} else -1
            margin = direction * (check["actual"] - check["expected"])
        leaves.append({**check, "threshold_margin_in_feature_units": margin})
    return {"root_verdict": "UNKNOWN" if verdict is None else "PASS" if verdict else "FAIL",
            "leaves": leaves, "counts": {"true": sum(row["passed"] is True for row in checks),
                                         "false": sum(row["passed"] is False for row in checks),
                                         "unknown": sum(row["passed"] is None for row in checks)}}


def lots_preview(*, budget_usdt, size_pct, side, snapshot, instrument, equity_usdt,
                 current_contracts=0, taker_signed_fee_rate=-.0005, maximum_units=10):
    """Same deterministic rounding/envelope as current exchange.entry_body.

    Only declared inputs are consumed. This returns preview facts, never an
    order request, order ID or live permission. Account-specific fee/liquidity
    still require actual owner's preflight and final recheck before execution.
    """
    if side not in {"long", "short"}:
        raise ContractError("preview_side")
    scalar_inputs = (budget_usdt, size_pct, equity_usdt, current_contracts, taker_signed_fee_rate, maximum_units)
    if (any(not known(value) for value in scalar_inputs) or min(budget_usdt, equity_usdt, maximum_units) <= 0
            or not 5 <= size_pct <= 30):
        raise ContractError("preview_unknown_numeric_input")
    numeric_spec = {key: Decimal(str(instrument[key])) for key in
                    ("tick_size", "lot_step", "contract_value", "contract_multiplier", "minimum_order_units")}
    if any(not value.is_finite() or value <= 0 for value in numeric_spec.values()):
        raise ContractError("preview_invalid_instrument")
    tick, lot = numeric_spec["tick_size"], numeric_spec["lot_step"]
    ct = numeric_spec["contract_value"] * numeric_spec["contract_multiplier"]
    quote = snapshot["ask"] if side == "long" else snapshot["bid"]
    visible = snapshot["ask_btc"] if side == "long" else snapshot["bid_btc"]
    if not known(quote) or quote <= 0 or not known(visible) or visible < 0:
        raise ContractError("preview_unknown_quote_liquidity")
    raw_price = Decimal(str(quote)) * (Decimal("1.0002") if side == "long" else Decimal(".9998"))
    price = (raw_price / tick).to_integral_value(rounding=ROUND_UP if side == "long" else ROUND_DOWN) * tick
    target = Decimal(str(budget_usdt)) * Decimal(str(size_pct)) / 100
    units = (target / price / ct / lot).to_integral_value(rounding=ROUND_DOWN) * lot
    quantity = units * ct
    notional = quantity * price
    total_exposure = (abs(Decimal(str(current_contracts))) + units) * ct * price
    reserve_fee = notional * abs(Decimal(str(taker_signed_fee_rate)))
    reasons = []
    if units < numeric_spec["minimum_order_units"]:
        reasons.append("minimum_order_units")
    if units > Decimal(str(maximum_units)):
        reasons.append("maximum_order_units")
    if quantity > Decimal(str(visible)) * Decimal(".1"):
        reasons.append("top_book_fraction")
    if total_exposure + reserve_fee > Decimal(str(equity_usdt)) * Decimal(".95"):
        reasons.append("own_equity_exposure")
    minimum_notional = numeric_spec["minimum_order_units"] * ct * price
    return {"side": side, "target_notional_usdt": float(target), "limit_price_usdt": float(price),
            "requested_quantity_btc": str(target / price),
            "contracts": str(units), "quantity_btc": float(quantity), "actual_notional_usdt": float(notional),
            "minimum_notional_usdt": float(minimum_notional), "minimum_allocation_usdt_at_size": float(minimum_notional * 100 / Decimal(str(size_pct))),
            "reserve_entry_fee_usdt": float(reserve_fee), "allowed": not reasons, "veto_reasons": reasons,
            "semantics": "preview_only_no_execution"}


def compare_trials(left, right):
    """Pair complete free trials on identical evidence/allocation; no ranks/edge claims."""
    required = ("data_sha256", "dataset_id", "dataset_kind", "budget_usdt", "runtime", "code_sha256")
    if (any(left.get(key) is None or left.get(key) != right.get(key) for key in required)
            or left.get("runtime") != "RULES_ONLY"):
        raise ContractError("trial_comparison_evidence_or_mode_mismatch")
    if left.get("status") != "COMPLETE" or right.get("status") != "COMPLETE":
        raise ContractError("trial_comparison_incomplete")
    if len(left.get("runs", [])) != len(right.get("runs", [])):
        raise ContractError("trial_comparison_run_count")
    paired = []
    for old, new in zip(left["runs"], right["runs"]):
        if old["name"] != new["name"] or old["status"] != "COMPLETE" or new["status"] != "COMPLETE":
            raise ContractError("trial_comparison_run_not_paired")
        if any(old.get(k) is None or old.get(k) != new.get(k) for k in ("admission_seconds", "time_policy")):
            raise ContractError("trial_comparison_admission_policy_mismatch")
        deltas = {}
        for key in ("pnl_usdt", "trades", "fees_usdt", "funding_usdt", "max_drawdown_pct"):
            a, b = old["metrics"].get(key), new["metrics"].get(key)
            deltas[key] = b - a if known(a) and known(b) else None
        paired.append({"name": old["name"], "delta_right_minus_left": deltas})
    return {"paired_runs": paired, "data_sha256": left["data_sha256"],
            "left_strategy_sha256": left.get("strategy_sha256"), "right_strategy_sha256": right.get("strategy_sha256"),
            "left_code_sha256": left.get("code_sha256"), "right_code_sha256": right.get("code_sha256"),
            "interpretation": "training comparison; same-tape selection is not a new holdout or proof of edge"}
