"""Read-only fixed-recorded-fill cost overlays for verified battle evidence.

No filesystem discovery, credentials, transport, model, execution or replay.
Integrity verification is not market authenticity or proof of profitability.
Amounts are Decimal-derived JSON strings; Jev USD never enters trading USDT.
"""
from collections import defaultdict
from decimal import Context, Decimal, InvalidOperation, localcontext
from hashlib import sha256
from io import BytesIO
import json
import re
import zipfile

from trading_lab.battle_review import verify_bundle
from trading_lab.battle_export import LAYOUTS, MARKET_MEMBER, verify_archive

SCHEMA = "fixed-recorded-fill-sensitivity/v1"
SCENARIO_BPS = (Decimal(0), Decimal(2), Decimal(5))
MAX_REVIEW_BYTES = 16 * 1024**2
MAX_ZIP_BYTES = 32 * 1024**2
MAX_ARCHIVE_BYTES = 64 * 1024**2
MAX_EVENTS = 100_000
MAX_LEDGER_ROWS = 100_000
MAX_PARTICIPANTS = 64  # analysis bound, never an execution participant permission
DECIMAL_PRECISION = 512  # covers bounded products/sums even across exponent -40..40
ALIAS = re.compile(r"order-[1-9][0-9]{0,6}\Z")


class SensitivityError(ValueError):
    """Fixed public error codes; never quote arbitrary input/provider strings."""


def _decimal(value, *, optional=False):
    if value is None and optional:
        return None
    if type(value) not in (str, int, float) or isinstance(value, bool) or len(str(value)) > 128:
        raise SensitivityError("invalid_decimal")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise SensitivityError("invalid_decimal") from None
    if (not result.is_finite() or len(result.as_tuple().digits) > 38
            or abs(result.as_tuple().exponent) > 40):
        raise SensitivityError("invalid_decimal")
    return result


def _text(value):
    return None if value is None else "0" if value == 0 else format(value, "f")


def _clone(value):
    chunks, size = [], 0
    try:
        for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":")).iterencode(value):
            size += len(chunk.encode("utf-8"))
            if size > MAX_REVIEW_BYTES:
                raise SensitivityError("review_size_limit")
            chunks.append(chunk)
        return json.loads("".join(chunks))
    except SensitivityError:
        raise
    except (TypeError, ValueError, RecursionError):
        raise SensitivityError("invalid_review_json") from None


def _mapping(value):
    if not isinstance(value, dict):
        raise SensitivityError("invalid_review_shape")
    return value


def _contract_factor(bot, rows, events):
    """Derive only from retained paired contracts/BTC inventory facts; no .01 guess."""
    candidates = []
    for row in rows:
        if row.get("role") == "funding":
            continue
        contracts = _decimal(row.get("position_after"), optional=True)
        event = events[row["seq"]]
        btc = _decimal(_mapping(event.get("data") or {}).get("position_btc"), optional=True)
        if contracts == 0 and btc not in (None, 0):
            return None, "inconsistent_or_rounded_unit_pairs"
        if contracts not in (None, 0) and btc is not None:
            factor = btc / contracts
            try:
                _decimal(str(factor))
            except SensitivityError:
                return None, "inconsistent_or_rounded_unit_pairs"
            if factor <= 0 or factor * contracts != btc:
                return None, "inconsistent_or_rounded_unit_pairs"
            candidates.append(factor)
    if not candidates:
        contracts = _decimal(bot["trade_ledger"].get("remaining"), optional=True)
        btc = _decimal(bot.get("position_btc"), optional=True)
        if bot["trade_ledger"].get("inventory_known") is True and contracts not in (None, 0) and btc is not None:
            factor = btc / contracts
            try:
                _decimal(str(factor))
            except SensitivityError:
                return None, "inconsistent_or_rounded_unit_pairs"
            if factor > 0 and factor * contracts == btc:
                candidates.append(factor)
    if not candidates:
        return None, "contract_to_btc_not_retained"
    if any(value != candidates[0] for value in candidates):
        return None, "inconsistent_or_rounded_unit_pairs"
    return candidates[0], "paired_recorded_contract_and_btc_inventory"


def _participant(bot, match, events, history_complete):
    identity = bot.get("id")
    if not isinstance(identity, str) or not 1 <= len(identity) <= 128:
        raise SensitivityError("invalid_participant_identity")
    costs, ledger = _mapping(bot.get("costs")), _mapping(bot.get("trade_ledger"))
    rows = ledger.get("rows")
    if not isinstance(rows, list) or len(rows) > MAX_LEDGER_ROWS:
        raise SensitivityError("ledger_row_limit_or_shape")
    paper = match["mode"] == "PAPER"
    cost_sources = {"paper_model_events"} if paper else {
        "actual_bills_and_cash_reconciled", "actual_cumulative_order_facts_unreconciled"}
    if costs.get("source") not in cost_sources:
        raise SensitivityError("cost_source_conflicts_with_mode")
    amount_keys = ("gross_pnl_usdt", "signed_fees_usdt", "signed_funding_usdt", "net_usdt")
    base = {key: _decimal(costs.get(key), optional=True) for key in amount_keys}
    if all(value is not None for value in base.values()) and sum((base[key] for key in amount_keys[:3]), Decimal(0)) != base["net_usdt"]:
        raise SensitivityError("trading_cost_bridge_mismatch")
    issues, seen, previous_seq = set(), set(), 0
    last_position = Decimal(0) if history_complete else None
    fill_count = funding_rows = 0
    known_turnover = Decimal(0)
    unknown_seqs = []
    orders = defaultdict(lambda: {"quantity": Decimal(0), "average": None, "unknown_quantity": False})
    expected_source = "paper_incremental_btc" if paper else "exchange_incremental_contracts"
    for row in rows:
        _mapping(row)
        seq = row.get("seq")
        if type(seq) is not int or seq not in events or seq in seen or seq <= previous_seq or events[seq].get("bot_id") != identity:
            raise SensitivityError("ledger_reference_duplicate_or_owner_mismatch")
        seen.add(seq)
        previous_seq = seq
        role = row.get("role")
        if role == "funding":
            if events[seq].get("code") != ("model_funding" if paper else "exchange_funding"):
                raise SensitivityError("funding_event_mismatch")
            funding_rows += 1
            continue  # authoritative base funding is already included; never double-charge
        if role not in {"entry", "add", "close"} or row.get("source") != expected_source:
            raise SensitivityError("unsupported_fill_ledger_contract")
        expected_code = ("paper_" if paper else "exchange_") + ("close_fill" if role == "close" else "entry_fill")
        if events[seq].get("code") != expected_code:
            raise SensitivityError("fill_event_mismatch")
        alias = row.get("order")
        if not isinstance(alias, str) or not ALIAS.fullmatch(alias):
            raise SensitivityError("invalid_public_order_alias")
        quantity = _decimal(row.get("quantity"), optional=True)
        price = _decimal(row.get("price_usdt"), optional=True)
        before = _decimal(row.get("position_before"), optional=True)
        after = _decimal(row.get("position_after"), optional=True)
        if quantity is not None and quantity < 0 or price is not None and price < 0:
            raise SensitivityError("negative_quantity_or_price")
        if quantity is not None and before is not None and after is not None:
            if last_position is not None and before != last_position:
                raise SensitivityError("inventory_sequence_mismatch")
            if quantity == 0:
                valid = before == after
            elif role == "close":
                valid = before != 0 and before * after >= 0 and abs(before) - abs(after) == quantity
            else:
                valid = abs(after - before) == quantity and before * (after - before) >= 0
            if not valid:
                raise SensitivityError("fill_quantity_inventory_mismatch")
            last_position = after
        else:
            issues.add("missing_inventory_or_quantity")
            last_position = after
        if quantity is None:
            unknown_seqs.append(seq)
            orders[alias]["unknown_quantity"] = True
            continue
        if quantity > 0:
            fill_count += 1
            if price == 0:
                raise SensitivityError("zero_positive_fill_price")
        if paper:
            if quantity == 0:
                continue
            if price is None:
                unknown_seqs.append(seq)
            else:
                known_turnover += quantity * price
        else:
            order = orders[alias]
            group = "close" if role == "close" else "non_reduce"
            if order.get("group", group) != group:
                raise SensitivityError("order_role_changed")
            order["group"] = group
            order["quantity"] += quantity
            # This is the cumulative order VWAP, not the incremental fill price.
            # Final cumulative Q*VWAP equals the sum of all fill notionals and
            # does not re-charge repeated partial observations/fee-only rows.
            if quantity > 0 or price not in (None, 0):
                order["average"] = price
    factor, factor_source = (Decimal(1), "ledger_quantity_btc") if paper else _contract_factor(bot, rows, events)
    unknown_orders = []
    if not paper:
        for alias, order in orders.items():
            if order["quantity"] == 0 and not order["unknown_quantity"]:
                continue
            if order["unknown_quantity"] or order["average"] is None or factor is None:
                unknown_orders.append(alias)
            else:
                known_turnover += order["quantity"] * order["average"] * factor
    funnel = _mapping(bot.get("funnel", {}))
    entries = funnel.get("entry_filled_orders")
    if entries is not None and (type(entries) is not int or entries < 0):
        raise SensitivityError("invalid_entry_count")
    intents = funnel.get("order_intents")
    if intents is not None and (type(intents) is not int or intents < 0):
        raise SensitivityError("invalid_order_intent_count")
    resolution_keys = ("missing_order_results", "unresolved")
    for key in resolution_keys:
        if key in ledger and type(ledger[key]) is not bool:
            raise SensitivityError("invalid_order_resolution_flag")
        if ledger.get(key) is True:
            issues.add(key)
    explicit_clear = all(ledger.get(key) is False for key in resolution_keys)
    legacy_clear = not any(ledger.get(key) is True for key in resolution_keys) and intents == 0
    order_resolution_known = explicit_clear or legacy_clear
    if not order_resolution_known:
        issues.add("order_resolution_unconfirmed")
    remaining = _decimal(ledger.get("remaining"), optional=True)
    position_btc = _decimal(bot.get("position_btc"), optional=True)
    # Zero observed fills cannot establish non-participation while an order
    # may have an unobserved result. Funding/cost completeness is independent.
    no_entry_known = (fill_count == 0 and not unknown_seqs and entries == 0
                      and history_complete and ledger.get("inventory_known") is True
                      and not ledger.get("limitations") and remaining == 0 and position_btc == 0
                      and order_resolution_known)
    if not fill_count and not no_entry_known:
        issues.add("empty_or_missing_fill_coverage")
    if no_entry_known and any(base[key] not in (None, 0) for key in amount_keys[:3]):
        issues.add("nonzero_trading_costs_without_fills")
    if ledger.get("inventory_known") is not True or ledger.get("limitations"):
        issues.add("ledger_inventory_or_history_unconfirmed")
    if not history_complete:
        issues.add("history_incomplete")
    if last_position is not None and remaining is not None and last_position != remaining:
        raise SensitivityError("ledger_remaining_mismatch")
    if remaining != 0 or position_btc != 0:
        issues.add("open_or_unknown_remaining")
    if factor is None and orders:
        issues.add(factor_source)
    notional_known = not unknown_seqs and not unknown_orders and "empty_or_missing_fill_coverage" not in issues
    if not notional_known:
        issues.add("fill_notional_incomplete")
    turnover = known_turnover if notional_known else None
    quality = _mapping(bot.get("quality", {}))
    final = (match.get("status") == "DONE" and match.get("incomplete") is False and history_complete
             and costs.get("complete") is True and quality.get("data_status") == "COMPLETE"
             and not issues and all(value is not None for value in base.values()) and notional_known)
    jev = _mapping(costs.get("jev", {}))
    jev_cost = _decimal(jev.get("known_cost_usd"), optional=True)
    if jev_cost is not None and jev_cost < 0:
        raise SensitivityError("negative_jev_cost")
    if jev.get("complete") is not True or jev_cost is None:
        final = False
        issues.add("jev_cost_incomplete_separate_usd")
    scenarios = []
    for bps in SCENARIO_BPS:
        known_extra = known_turnover * bps / Decimal(10_000)
        extra = Decimal(0) if bps == 0 else turnover * bps / Decimal(10_000) if turnover is not None else None
        net = base["net_usdt"] - extra if base["net_usdt"] is not None and extra is not None else None
        scenarios.append({"extra_bps_per_fill": _text(bps), "additional_cost_usdt": _text(extra),
            "known_subtotal_additional_cost_usdt": _text(known_extra),
            "recorded_net_after_overlay_usdt": _text(net),
            "settled_eligible_net_after_overlay_usdt": _text(net) if final else None})
    return {"id": identity, "cost_kind": "MODEL" if paper else "ACTUAL_OBSERVATIONS",
        "cost_source": costs["source"], "base_trading_usdt": {k: _text(v) for k, v in base.items()},
        "funding_source": costs.get("funding_source"),
        "jev_usd": {"known_cost_usd": _text(jev_cost), "complete": jev.get("complete") is True,
                    "source": jev.get("source"), "included_in_trading_usdt": False},
        "spread_slippage_usdt": None, "spread_slippage_decomposition": "NOT_IDENTIFIABLE_FROM_FILL_LEDGER",
        "positive_fill_observations": fill_count, "funding_observations": funding_rows,
        "participation": "NO_ENTRY" if no_entry_known else "FILLED" if fill_count else "UNKNOWN",
        "order_resolution": {"missing_order_results": ledger.get("missing_order_results"),
            "unresolved": ledger.get("unresolved"), "order_intents": intents,
            "confirmed_clear": order_resolution_known,
            "source": "EXPLICIT_FLAGS" if explicit_clear else "LEGACY_ZERO_INTENTS" if legacy_clear else "UNKNOWN"},
        "contract_btc": _text(factor) if not paper else None, "quantity_conversion_source": factor_source,
        "notional_basis": "BTC_DELTA_TIMES_FILL_PRICE" if paper else "PER_ORDER_CUMULATIVE_Q_TIMES_LAST_OBSERVED_VWAP_TIMES_BTC_FACTOR",
        "known_fill_notional_subtotal_usdt": _text(known_turnover), "full_recorded_fill_notional_usdt": _text(turnover),
        "unknown_notional_event_seqs": unknown_seqs, "unknown_notional_order_aliases": unknown_orders,
        "status": "COMPLETE_FIXED_LEDGER" if final else "INCOMPLETE_DIAGNOSTIC_ONLY",
        "issues": sorted(issues), "scenarios": scenarios}


def analyze_review(bundle: dict) -> dict:
    """Verify immutable review evidence; calculate only additional fixed-fill costs."""
    review = _mapping(_clone(bundle))
    match = _mapping(review.get("match"))
    if match.get("mode") not in {"PAPER", "OKX_DEMO", "OKX_LIVE"}:
        raise SensitivityError("unsupported_execution_mode")
    if not isinstance(match.get("id"), str) or not 1 <= len(match["id"]) <= 128:
        raise SensitivityError("invalid_match_identity")
    events, bots = review.get("events"), review.get("participants")
    if not isinstance(events, list) or len(events) > MAX_EVENTS or not isinstance(bots, list) or len(bots) > MAX_PARTICIPANTS:
        raise SensitivityError("review_count_limit_or_shape")
    if (any(not isinstance(bot, dict) or not isinstance(bot.get("id"), str) for bot in bots)
            or len({bot.get("id") for bot in bots}) != len(bots)):
        raise SensitivityError("duplicate_or_invalid_participant")
    # Bound monetary exponents before the existing verifier formats Decimal
    # values; a short untrusted "1e1000000000" must never expand into memory.
    for bot in bots:
        costs = _mapping(bot.get("costs"))
        for key in ("gross_pnl_usdt", "signed_fees_usdt", "signed_funding_usdt", "net_usdt"):
            _decimal(costs.get(key), optional=True)
    with localcontext(Context(prec=DECIMAL_PRECISION)):
        try:
            verification = verify_bundle(review)
        except (ValueError, KeyError, TypeError, ArithmeticError, AttributeError):
            raise SensitivityError("review_verification_failed") from None
        by_seq = {e["seq"]: e for e in events}
        full_history = review.get("history_complete") is True and (not events or events[0]["seq"] == 1)
        rows = [_participant(bot, match, by_seq, full_history) for bot in bots]
    return {"schema": SCHEMA, "method": "FIXED_RECORDED_FILL_ADDITIONAL_COST_OVERLAY",
        "scenario_bps_per_fill": ["0", "2", "5"], "dynamic_replay": False,
        "input_bundle_sha256": verification["bundle_sha256"], "mode": match["mode"],
        "match_id": match.get("id"), "participants": rows,
        "cash": {"net_usdt": "0", "source": "PASSIVE_COMPARISON_ONLY_NOT_A_WINNER"},
        "limitations": ["Fill quantities, prices, decisions, timing, funding and risk checks are frozen.",
            "Additional cost is sum(abs(BTC fill notional))*bps/10000; base fees/slippage are not charged again.",
            "Verification checks evidence integrity, not exchange authenticity or complete market coverage.",
            "Recorded net is diagnostic if input is incomplete; unknown data is not zero or Cash.",
            "No allocation, win rate, edge, holdout, maker fill or dynamic delay result is inferred."]}


def _strict_json(payload):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise SensitivityError("duplicate_json_key")
            result[key] = value
        return result
    try:
        return json.loads(payload, object_pairs_hook=pairs,
                          parse_constant=lambda unused: (_ for _ in ()).throw(SensitivityError("nonfinite_json")))
    except SensitivityError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise SensitivityError("invalid_archive_json") from None


def analyze_archive(payload: bytes | bytearray) -> dict:
    """Bounded immutable ZIP bytes; use the existing complete battle verifier."""
    if not isinstance(payload, (bytes, bytearray)) or len(payload) > MAX_ZIP_BYTES:
        raise SensitivityError("archive_input_limit_or_type")
    immutable = bytes(payload)
    try:
        with zipfile.ZipFile(BytesIO(immutable)) as archive:
            infos = archive.infolist()
            names = {i.filename for i in infos}
            contracts = {n for n in names if MARKET_MEMBER.fullmatch(n)}  # R2.1.13 (X2-09): a mixed season's one member per contract
            if len(names) != len(infos) or ((names - contracts) | {"market.jsonl"} if contracts else names) not in LAYOUTS:
                raise SensitivityError("archive_member_contract")
            if sum(i.file_size for i in infos) > MAX_ARCHIVE_BYTES or archive.getinfo("battle-review.json").file_size > MAX_REVIEW_BYTES:
                raise SensitivityError("archive_expansion_limit")
            _strict_json(archive.read("manifest.json"))
            review = _strict_json(archive.read("battle-review.json"))
        report = analyze_review(review)  # validates numeric bounds before ZIP's verifier
        with localcontext(Context(prec=DECIMAL_PRECISION)):
            verification = verify_archive(BytesIO(immutable))
    except SensitivityError:
        raise
    except (zipfile.BadZipFile, ValueError, KeyError, TypeError, ArithmeticError, OSError, AttributeError):
        raise SensitivityError("archive_verification_failed") from None
    report["archive"] = {"sha256": sha256(immutable).hexdigest(), "verification": verification}
    return report
