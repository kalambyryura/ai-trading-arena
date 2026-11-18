"""Research-only pure contracts. No exchange, credentials, file or network I/O.

Root may use these fixtures when implementing new journals. Existing journals,
strategy-defined exits and owner Stop/close-only recovery keep their semantics.
"""
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import hashlib
import json
import math
import re


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def numeric_text(value):
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


@dataclass
class OperationalState:
    admission_deadline_mono: float
    admission_closed: bool = False
    admission_reason: str | None = None
    ops_reasons: set[str] = field(default_factory=set)
    protected: bool = False
    owned_contracts: Decimal = Decimal(0)
    manual_review: bool = False

    def tick(self, mono):
        if mono >= self.admission_deadline_mono:
            self.admission_closed = True
            self.admission_reason = self.admission_reason or "admission_cutoff"

    def block_entries(self, reason, *, permanent=False):
        """A resource condition is never translated into a position exit."""
        self.ops_reasons.add(reason)
        if permanent:
            self.admission_closed = True
            self.admission_reason = self.admission_reason or reason

    def resolve_ops(self, reason):
        self.ops_reasons.discard(reason)

    def permits_entry(self):
        return not (self.admission_closed or self.ops_reasons or self.manual_review)

    def status(self):
        if self.manual_review:
            return "MANUAL_REVIEW"
        if self.ops_reasons:
            return "DEGRADED_PROTECTED" if self.protected else "DEGRADED_UNPROTECTED"
        if self.admission_closed:
            return "DRAINING" if self.owned_contracts else "SETTLEMENT_PENDING"
        return "OPEN"


def own_exit_reason(*, owner_stop=False, risk_exit=False, formal_exit=False,
                    gain_bps=None, stop_bps=None, target_bps=None,
                    hold_age=None, strategy_max_hold=None):
    """No match time, GET count, disk count, Jev budget or heartbeat parameter."""
    if owner_stop:
        return "OWNER_STOP"
    if risk_exit:
        return "RISK_EXIT"
    if gain_bps is not None:
        if stop_bps is not None and gain_bps <= -stop_bps:
            return "STRATEGY_STOP"
        if target_bps is not None and gain_bps >= target_bps:
            return "STRATEGY_TARGET"
    if strategy_max_hold is not None and hold_age is not None and hold_age >= strategy_max_hold:
        return "STRATEGY_MAX_HOLD"
    return "FORMAL_EXIT" if formal_exit else None


@dataclass
class ClockGuard:
    """Wall is an audit label; within one process, elapsed time uses monotonic."""
    last_wall: float | None = None
    last_mono: float | None = None
    last_quote_ms: int | None = None
    gap_seconds: float = 90

    def observe(self, *, wall, mono, quote_ms):
        if not all(type(v) in (float, int) and math.isfinite(v) for v in (wall, mono, quote_ms)):
            raise ValueError("clock_shape")
        reasons = []
        gap = 0
        wall_delta = mono_delta = 0
        if self.last_mono is not None:
            wall_delta, mono_delta = wall - self.last_wall, mono - self.last_mono
            if mono_delta < 0:
                raise ValueError("monotonic_regression")
            if abs(wall_delta - mono_delta) > 2:
                reasons.append("wall_clock_jump")
            if mono_delta > self.gap_seconds:
                gap = mono_delta
                reasons.append("observation_gap")
            if quote_ms < self.last_quote_ms:
                reasons.append("quote_regression")
        # Accepted independently validated future samples may recover after a gap.
        # This guard intentionally does not keep rejecting against an old _last.
        if "quote_regression" not in reasons:
            self.last_wall, self.last_mono, self.last_quote_ms = wall, mono, quote_ms
        return {"reasons": reasons, "wall_delta_s": wall_delta, "mono_delta_s": mono_delta,
                "gap_s": gap, "invalidate_temporal_state": bool(reasons),
                "force_position_close": False}


@dataclass
class RollingRequestBudget:
    """Bounded memory, finite rolling throughput, no lifetime read deadline.

    The headroom is for own-position/protection/child-fill reconciliation only.
    Paid requests and order intent counters must use their persistent ledgers.
    """
    limit: int = 8
    window_seconds: float = 2
    critical_headroom: int = 2
    observations: deque = field(default_factory=deque)
    last_mono: float = float("-inf")

    def acquire(self, mono, *, critical=False):
        if mono < self.last_mono:
            raise ValueError("monotonic_regression")
        self.last_mono = mono
        while self.observations and self.observations[0] <= mono - self.window_seconds:
            self.observations.popleft()
        allowance = self.limit if critical else self.limit - self.critical_headroom
        if len(self.observations) >= allowance:
            return False
        self.observations.append(mono)
        return True


@dataclass(frozen=True)
class ProtectionIntent:
    """An explicit-size owned stop proposal, never a request sender."""
    client_id: str
    body: dict
    body_sha256: str
    owned_contracts: str


def owned_stop_intent(*, client_id, contracts, basis, stop_bps, tick_size, instrument="BTC-USDT-SWAP"):
    if not re.fullmatch(r"J8[0-9a-f]{16}[0-5]S[0-9]{4}", client_id):
        raise ValueError("stop_identity")
    quantity, average, stop, tick = map(Decimal, map(str, (contracts, basis, stop_bps, tick_size)))
    if not all(v.is_finite() for v in (quantity, average, stop, tick)):
        raise ValueError("stop_number")
    if not quantity or min(average, stop, tick) <= 0 or stop >= 10000:
        raise ValueError("stop_range")
    long = quantity > 0
    raw_trigger = average * (1 - stop / 10000 if long else 1 + stop / 10000)
    # Round toward earlier protection, never widen the loss threshold by a tick.
    trigger = (raw_trigger / tick).to_integral_value(rounding=ROUND_UP if long else ROUND_DOWN) * tick
    body = {"instId": instrument, "tdMode": "isolated", "posSide": "net",
            "side": "sell" if long else "buy", "ordType": "conditional", "sz": numeric_text(abs(quantity)),
            "reduceOnly": True, "algoClOrdId": client_id, "slTriggerPx": numeric_text(trigger),
            "slTriggerPxType": "last", "slOrdPx": "-1", "cxlOnClosePos": True}
    return ProtectionIntent(client_id, body, hashlib.sha256(canonical(body)).hexdigest(), numeric_text(quantity))


def validate_stop_receipt(intent, row, *, known_algo_id=None):
    if not isinstance(row, dict):
        raise ValueError("stop_receipt_unavailable")
    expected = intent.body
    for key in ("instId", "tdMode", "posSide", "side", "ordType", "algoClOrdId", "slTriggerPxType"):
        if row.get(key) != expected[key]:
            raise ValueError("stop_receipt_mismatch")
    for key in ("sz", "slTriggerPx", "slOrdPx"):
        try:
            actual = Decimal(str(row.get(key)))
            if not actual.is_finite() or actual != Decimal(expected[key]):
                raise ValueError()
        except (ValueError, ArithmeticError):
            raise ValueError("stop_receipt_mismatch") from None
    if str(row.get("reduceOnly")).lower() != "true":
        raise ValueError("stop_not_reduce_only")
    # OKX GET /trade/order-algo documents/returns reduceOnly, but does not echo
    # the placement-only cxlOnClosePos option. Never synthesize an API field:
    # ownership, explicit size and reduceOnly remain mandatory; flat cleanup
    # verifies/cancels our remaining live algo separately. A reported conflict
    # is still rejected, as is an intent that never requested cancel-on-close.
    if (expected.get("cxlOnClosePos") is not True or
            "cxlOnClosePos" in row and str(row["cxlOnClosePos"]).lower() != "true"):
        raise ValueError("stop_cancel_on_close_conflict")
    algo_id = row.get("algoId")
    if not isinstance(algo_id, str) or not algo_id.isdigit() or (known_algo_id and algo_id != known_algo_id):
        raise ValueError("stop_algo_identity")
    if row.get("state") not in {"live", "effective", "partially_effective", "canceled", "order_failed"}:
        raise ValueError("stop_state_unknown")
    child_ids = row.get("ordIdList", [])
    if (not isinstance(child_ids, list) or len(child_ids) > 10
            or any(not isinstance(i, str) or not re.fullmatch(r"[0-9]{1,40}", i) for i in child_ids)
            or len(set(child_ids)) != len(child_ids)):
        raise ValueError("stop_child_id_shape")
    return {"algo_id": algo_id, "live_verified": row["state"] == "live" and row.get("failCode") in {"", "0"},
            "child_order_ids": child_ids[:], "state": row["state"],
            "cancel_on_close_reported": "cxlOnClosePos" in row}


def adopt_stop_child(intent, algo_receipt, child, *, current_owned_contracts, previous_acc_fill="0"):
    """A triggered stop child is adopted BEFORE actual-position mismatch checking.

    Root must fetch it by ordId, map it to a reduce-only ledger event, and retain
    cumulative fills, signed fees and bills. An ACK/effective label is not a fill.
    """
    receipt = validate_stop_receipt(intent, algo_receipt)
    if child.get("ordId") not in receipt["child_order_ids"] or child.get("algoId") != receipt["algo_id"]:
        raise ValueError("unowned_stop_child")
    for key in ("instId", "tdMode", "posSide", "side"):
        if child.get(key) != intent.body[key]:
            raise ValueError("stop_child_mismatch")
    if str(child.get("reduceOnly")).lower() != "true" or child.get("ordType") != "market":
        raise ValueError("stop_child_not_owned_close")
    quantity = Decimal(str(child.get("accFillSz")))
    previous = Decimal(str(previous_acc_fill))
    owned = Decimal(str(current_owned_contracts))
    requested = Decimal(str(child.get("sz")))
    if (not all(v.is_finite() for v in (quantity, previous, owned, requested))
            or not 0 <= previous <= quantity <= requested <= abs(Decimal(intent.owned_contracts))
            or quantity - previous > abs(owned)):
        raise ValueError("stop_child_exceeds_owned_fill")
    return {"origin": "OWNED_PROTECTIVE_ALGO", "ordId": child["ordId"], "reduceOnly": True,
            "contracts": format(quantity, "f"), "new_fill_delta": format(quantity - previous, "f"),
            "source_algo_id": receipt["algo_id"]}


@dataclass
class ProtectiveLifecycle:
    """Pure action planner for a durably reserved protection intent.

    Caller persists `send_started` BEFORE a permitted PLACE_ONCE. No ACK alone
    makes a position protected. A failed GET never licenses a second placement.
    Close requests do not cancel an armed stop before confirmed flatness.
    """
    intent: ProtectionIntent
    send_started: bool = False
    algo_id: str | None = None
    receipt: dict | None = None

    def reserve_send(self):
        if self.send_started:
            raise ValueError("stop_place_already_reserved")
        self.send_started = True
        return {"kind": "PLACE_ONCE", "body": deepcopy_dict(self.intent.body)}

    def ack_uncertain(self):
        return {"kind": "GET_BY_CLIENT_ID", "client_id": self.intent.client_id}

    def observe(self, row):
        receipt = validate_stop_receipt(self.intent, row, known_algo_id=self.algo_id)
        self.algo_id, self.receipt = receipt["algo_id"], receipt
        return receipt

    def plan(self, *, owned_contracts, verified_flat=False, owner_close=False):
        quantity = Decimal(str(owned_contracts))
        if not quantity.is_finite():
            raise ValueError("owned_number")
        if self.receipt is None:
            return self.ack_uncertain() if self.send_started else {"kind": "RESERVE_STOP_INTENT"}
        if self.receipt["child_order_ids"]:
            return {"kind": "RECONCILE_CHILDREN_FIRST", "ord_ids": self.receipt["child_order_ids"]}
        if not quantity:
            if verified_flat and self.receipt["live_verified"]:
                return {"kind": "CANCEL_OWN_AFTER_FLAT", "algo_id": self.algo_id}
            return {"kind": "VERIFY_FLAT_AND_SETTLEMENT"}
        if not self.receipt["live_verified"]:
            return {"kind": "PROTECTION_NOT_VERIFIED", "block_entry_add": True}
        if owner_close:
            return {"kind": "OWN_REDUCE_ONLY_CLOSE_WITH_STOP_ARMED", "contracts": format(abs(quantity), "f")}
        if quantity * Decimal(self.intent.owned_contracts) <= 0:
            return {"kind": "MANUAL_REVIEW_POSITION_SIDE", "block_entry_add": True}
        if abs(quantity) != Decimal(self.intent.body["sz"]):
            return {"kind": "AMEND_OWN_STOP_SIZE_NO_WIDENING", "contracts": numeric_text(abs(quantity)),
                    "block_entry_add": True}
        return {"kind": "MONITOR_OWN_POSITION", "covered_contracts": self.intent.body["sz"]}


def deepcopy_dict(value):
    return json.loads(canonical(value))
