"""YURII_IKIGAI_TRIANGLE_G1: pure OFFLINE reference, NOT an arena recipe.

Geometric adaptation, not an Elliott-wave parser or an order executor.
No I/O, network, credentials, exchange adapters or paid/model calls.
Timestamps: UTC integer seconds; bars are standard, unadjusted H1 OHLC.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from hashlib import sha256
import json
import math

HOUR = 3600
STRATEGY_ID = "YURII_IKIGAI_TRIANGLE_G1"


@dataclass(frozen=True)
class Params:
    pivot_left: int = 2
    pivot_right: int = 2
    min_leg_hours: int = 3
    min_pattern_hours: int = 12
    max_pattern_hours: int = 72
    min_leg_bps: float = 20
    min_final_width_ratio: float = 0.10
    max_final_width_ratio: float = 0.75
    h4_trend_min_bps: float = 100
    setup_lifetime_hours: int = 12
    max_apex_hours_after_e: int = 24
    entry_buffer_bps: float = 5
    stop_buffer_bps: float = 10
    min_stop_bps: float = 20
    max_stop_bps: float = 500
    max_entry_chase_bps: float = 50
    max_spread_bps: float = 10
    risk_equity_fraction: float = 0.005
    max_notional_equity_fraction: float = 0.30
    book_fraction: float = 0.10
    min_net_reward_risk: float = 1.5
    fee_bps_per_side: float = 5
    slippage_bps_per_side: float = 2
    adverse_funding_reserve_bps: float = 10
    max_bar_lag_seconds: int = 120
    max_quote_lag_seconds: int = 5
    min_execution_delay_seconds: int = 1
    max_entry_delay_seconds: int = 60
    cooldown_seconds: int = 14400
    max_hold_seconds: int = 86400
    failed_breakout_closes: int = 2
    max_history_bars: int = 256


@dataclass(frozen=True)
class Bar:
    open_ts: int
    received_at: int
    open: float
    high: float
    low: float
    close: float
    complete: bool = True

    @property
    def close_ts(self):
        return self.open_ts + HOUR


@dataclass(frozen=True)
class Pivot:
    kind: str
    open_ts: int
    price: float
    confirmed_at: int


@dataclass(frozen=True)
class Setup:
    setup_id: str
    side: str
    anchors: tuple[Pivot, ...]  # P0, A, B, C, D, E
    created_at: int
    created_bar_ts: int
    expires_at: int
    apex_open_ts: float
    trigger: float
    stop: float
    target: float
    height: float
    context: tuple[tuple, ...]  # frozen H4 (close_ts, high, low, close)
    tick_size: float


@dataclass(frozen=True)
class Signal:
    setup: Setup
    decided_at: int
    signal_bar_ts: int


@dataclass(frozen=True)
class Quote:
    ts: int
    received_at: int
    bid: float
    ask: float
    book_base_size: float  # relevant ask size for long, bid size for short


@dataclass(frozen=True)
class Instrument:
    tick_size: float
    base_per_contract: float
    lot_contracts: float
    min_contracts: float


def _finite_positive(*values):
    return all(isinstance(x, (int, float)) and not isinstance(x, bool)
               and math.isfinite(x) and x > 0 for x in values)


def _round_step(value, step, up=False):
    d, s = Decimal(str(value)), Decimal(str(step))
    return float((d / s).to_integral_value(
        rounding=ROUND_CEILING if up else ROUND_FLOOR) * s)


def bar_valid(bar: Bar, now: int, params: Params):
    return (isinstance(bar.open_ts, int) and bar.open_ts % HOUR == 0
            and isinstance(bar.received_at, int) and bar.complete is True
            and _finite_positive(bar.open, bar.high, bar.low, bar.close)
            and bar.low <= min(bar.open, bar.close)
            <= max(bar.open, bar.close) <= bar.high
            and bar.close_ts <= bar.received_at <= now
            and now - bar.close_ts <= params.max_bar_lag_seconds)


class Detector:
    """Append-only decisions, bounded history, immutable confirmed pivots.

    Caller supplies admission cutoff and VERIFIED position state. It must call
    mark_flat only after actual reconciliation, not after an unfilled exit intent.
    """
    def __init__(self, tick_size: float, params: Params = Params()):
        if not _finite_positive(tick_size):
            raise ValueError("positive tick size required")
        self.tick_size, self.params = tick_size, params
        self.bars: list[Bar] = []
        self.pivots: list[Pivot] = []
        self.pending: Setup | None = None
        self.used_setup_ids: set[str] = set()
        self.events: list[dict] = []
        self.last_seen_ts: int | None = None
        self.cooldown_until = 0
        self.last_flat_at: int | None = None

    def _event(self, kind, at, **data):
        event = {"kind": kind, "at": at, **data}
        self.events.append(deepcopy(event))
        return event

    def _reset(self):
        self.bars.clear()
        self.pivots.clear()
        self.pending = None
        # used IDs/cooldown/events survive gaps; no historical repaint/reset.

    def mark_flat(self, reconciled_at: int):
        self.last_flat_at = reconciled_at
        self.pending = None
        self.cooldown_until = max(self.cooldown_until,
                                  reconciled_at + self.params.cooldown_seconds)
        self._event("FLAT_RECONCILED", reconciled_at,
                    cooldown_until=self.cooldown_until)

    def _context(self, before_open_ts):
        groups = defaultdict(list)
        for b in self.bars:
            if b.close_ts <= before_open_ts:
                groups[b.open_ts // (4 * HOUR)].append(b)
        completed = []
        for group, bars in sorted(groups.items()):
            start = group * 4 * HOUR
            if [b.open_ts for b in bars] == [start + i * HOUR for i in range(4)]:
                completed.append((start + 4 * HOUR, max(b.high for b in bars),
                                  min(b.low for b in bars), bars[-1].close))
        if len(completed) < 3:
            return None, ()
        context = tuple(completed[-3:])
        change = (context[-1][3] / context[0][3] - 1) * 10000
        up = all(a[1] < b[1] and a[2] < b[2]
                 for a, b in zip(context, context[1:]))
        down = all(a[1] > b[1] and a[2] > b[2]
                   for a, b in zip(context, context[1:]))
        if up and change >= self.params.h4_trend_min_bps:
            return "long", context
        if down and change <= -self.params.h4_trend_min_bps:
            return "short", context
        return None, context

    def _confirmed_pivot(self, now):
        p = self.params
        if len(self.bars) < p.pivot_left + p.pivot_right + 1:
            return None
        i = len(self.bars) - p.pivot_right - 1
        window = self.bars[i - p.pivot_left:i + p.pivot_right + 1]
        center = self.bars[i]
        peers = [b for b in window if b is not center]
        high = all(center.high > b.high for b in peers)
        low = all(center.low < b.low for b in peers)
        if high and low:
            self.pivots.clear()
            self._event("AMBIGUOUS_PIVOT", now, open_ts=center.open_ts)
            return None
        if high or low:
            return Pivot("H" if high else "L", center.open_ts,
                         center.high if high else center.low, now)
        return None

    def _make_setup(self, now, current):
        p = self.params
        if len(self.pivots) < 6:
            return None, "NEED_SIX_PIVOTS"
        anchors = tuple(self.pivots[-6:])
        if self.last_flat_at is not None and anchors[0].open_ts <= self.last_flat_at:
            return None, "ANCHORS_PREDATE_LAST_FLAT"
        kinds = "".join(x.kind for x in anchors)
        if kinds not in ("HLHLHL", "LHLHLH"):
            return None, "ALTERNATION"
        side = "long" if kinds == "HLHLHL" else "short"
        sign = 1 if side == "long" else -1
        v = [sign * x.price for x in anchors]
        times = [x.open_ts / HOUR for x in anchors]
        if not (v[0] > v[2] > v[4] > v[5] > v[3] > v[1]):
            return None, "NOT_ORDINARY_CONTRACTION"
        if any(b - a < p.min_leg_hours for a, b in zip(times, times[1:])):
            return None, "LEG_TOO_SHORT"
        if not p.min_pattern_hours <= times[-1] - times[0] <= p.max_pattern_hours:
            return None, "PATTERN_DURATION"
        lengths = [abs(b.price - a.price) for a, b in zip(anchors, anchors[1:])]
        if min(lengths) / anchors[0].price * 10000 < p.min_leg_bps:
            return None, "LEG_TOO_SMALL"
        width = (v[4] - v[5]) / (v[0] - v[1])
        if not p.min_final_width_ratio <= width <= p.max_final_width_ratio:
            return None, "FINAL_WIDTH"
        upper_slope = (v[4] - v[2]) / (times[4] - times[2])
        lower_slope = (v[5] - v[3]) / (times[5] - times[3])
        ui = v[2] - upper_slope * times[2]
        li = v[3] - lower_slope * times[3]
        apex = (li - ui) / (upper_slope - lower_slope)
        if not times[-1] < apex <= times[-1] + p.max_apex_hours_after_e:
            return None, "APEX"
        context_side, context = self._context(anchors[0].open_ts)
        if context_side != side:
            return None, "HTF_CONTEXT_UNKNOWN_OR_MISMATCH"
        b_price, e_price = anchors[2].price, anchors[5].price
        db, de, tick = map(lambda x: Decimal(str(x)), (b_price, e_price, self.tick_size))
        entry_buffer = max(2 * tick, db * Decimal(str(p.entry_buffer_bps)) / 10000)
        stop_buffer = max(2 * tick, de * Decimal(str(p.stop_buffer_bps)) / 10000)
        trigger = _round_step(db + sign * entry_buffer, tick, sign > 0)
        stop = _round_step(de - sign * stop_buffer, tick, sign < 0)
        height_decimal = max(abs(Decimal(str(b.price)) - Decimal(str(a.price)))
                             for a, b in zip(anchors, anchors[1:]))
        height = float(height_decimal)
        target = _round_step(db + sign * height_decimal, tick, sign < 0)
        if not _finite_positive(trigger, stop, target):
            return None, "NONPOSITIVE_LEVEL"
        key = json.dumps([STRATEGY_ID, asdict(p), self.tick_size,
                          [asdict(x) for x in anchors]], sort_keys=True)
        setup_id = sha256(key.encode()).hexdigest()[:24]
        if setup_id in self.used_setup_ids:
            return None, "USED_SETUP"
        self.used_setup_ids.add(setup_id)  # every valid setup attempted just once
        if sign * (current.close - trigger) > 0:
            return None, "ALREADY_BROKEN_BEFORE_SETUP"
        expires = min(now + p.setup_lifetime_hours * HOUR, math.floor(apex * HOUR))
        if expires <= now:
            return None, "APEX_PASSED_BEFORE_CONFIRMATION"
        return Setup(setup_id, side, anchors, now, current.open_ts, expires,
                     apex * HOUR, trigger, stop, target, height, context,
                     self.tick_size), None

    def on_bar(self, bar: Bar, now: int, admission_until: int,
               has_position: bool | None = False):
        """Return new decisions only. No price/position/order mutation occurs."""
        start = len(self.events)
        if not bar_valid(bar, now, self.params):
            self._reset()
            self._event("UNKNOWN_DATA", now)
            return deepcopy(self.events[start:])
        if self.last_seen_ts is not None and bar.open_ts <= self.last_seen_ts:
            self._reset()
            self._event("UNKNOWN_REVISION_OR_OUT_OF_ORDER", now)
            return deepcopy(self.events[start:])
        if self.last_seen_ts is not None and bar.open_ts != self.last_seen_ts + HOUR:
            self._reset()
            self._event("GAP_RESET", now)
        self.last_seen_ts = bar.open_ts
        previous = self.bars[-1] if self.bars else None
        self.bars.append(bar)
        self.bars = self.bars[-self.params.max_history_bars:]
        self.pivots = [x for x in self.pivots if x.open_ts >= self.bars[0].open_ts]
        if self.pending:
            s = self.pending
            sign = 1 if s.side == "long" else -1
            e = s.anchors[-1].price
            invalid = bar.low < e if sign > 0 else bar.high > e
            if invalid or now >= s.expires_at:
                self.pending = None
                self._event("INVALIDATED" if invalid else "EXPIRED", now,
                            setup_id=s.setup_id)
            elif previous and bar.open_ts > s.created_bar_ts and (
                    sign * (previous.close - s.trigger) <= 0
                    < sign * (bar.close - s.trigger)):
                self.pending = None
                if has_position is None:
                    reason = "UNKNOWN_POSITION"
                elif has_position:
                    reason = "POSITION_EXISTS"
                elif now >= admission_until:
                    reason = "ADMISSION_CLOSED"
                elif now < self.cooldown_until:
                    reason = "COOLDOWN"
                else:
                    reason = None
                self._event("NO_ENTRY" if reason else "ENTRY_SIGNAL", now,
                            reason=reason, signal=asdict(Signal(s, now, bar.open_ts)))
        pivot = self._confirmed_pivot(now)
        if pivot:
            if self.pivots and self.pivots[-1].kind == pivot.kind:
                self.pivots = [pivot]  # start a NEW chain, never rewrite old pivot
                self._event("PIVOT_CHAIN_RESET", now, pivot=asdict(pivot))
            else:
                self.pivots.append(pivot)
                self.pivots = self.pivots[-6:]
                self._event("PIVOT_CONFIRMED", now, pivot=asdict(pivot))
            if not self.pending:
                setup, reason = self._make_setup(now, bar)
                if setup:
                    self.pending = setup
                    self._event("SETUP", now, setup=asdict(setup))
                elif reason not in ("NEED_SIX_PIVOTS", "USED_SETUP"):
                    self._event("SETUP_REJECTED", now, reason=reason)
        return deepcopy(self.events[start:])


def plan_entry(signal: Signal, quote: Quote, instrument: Instrument,
               equity: float, admission_until: int, params: Params = Params()):
    """Pure hypothetical sizing, no order submission or retry mechanism.

    One quote / one attempt per consumed signal; caller persists NO_FILL too.
    Loss is a modeled stop scenario, NOT a guaranteed maximum loss.
    """
    s = signal.setup
    reject = lambda why: {"kind": "NO_ENTRY", "reason": why, "setup_id": s.setup_id}
    if not _finite_positive(equity, quote.bid, quote.ask, quote.book_base_size,
                            instrument.tick_size, instrument.base_per_contract,
                            instrument.lot_contracts, instrument.min_contracts):
        return reject("UNKNOWN_METADATA_OR_QUOTE")
    if instrument.tick_size != s.tick_size:
        return reject("TICK_METADATA_DRIFT")
    if quote.bid > quote.ask or not (
            signal.decided_at + params.min_execution_delay_seconds <= quote.ts
            <= quote.received_at <= signal.decided_at + params.max_entry_delay_seconds
            and quote.received_at - quote.ts <= params.max_quote_lag_seconds):
        return reject("STALE_OR_PREDECISION_QUOTE")
    if quote.received_at >= min(admission_until, s.expires_at):
        return reject("ADMISSION_OR_SETUP_EXPIRED")
    sign = 1 if s.side == "long" else -1
    mid = (quote.ask + quote.bid) / 2
    spread = (quote.ask - quote.bid) / mid
    if spread * 10000 > params.max_spread_bps:
        return reject("SPREAD")
    slip, fee = params.slippage_bps_per_side / 10000, params.fee_bps_per_side / 10000
    raw_entry = quote.ask if sign > 0 else quote.bid
    entry = raw_entry * (1 + sign * slip)
    if sign * (entry - s.trigger) < 0 or (
            sign * (entry - s.trigger) / s.trigger * 10000 > params.max_entry_chase_bps):
        return reject("RETURNED_INSIDE_OR_CHASE")
    distance_bps = sign * (entry - s.stop) / entry * 10000
    if not params.min_stop_bps <= distance_bps <= params.max_stop_bps:
        return reject("STOP_DISTANCE")
    stop_fill = s.stop * (1 - sign * (slip + spread / 2))
    target_fill = s.target * (1 - sign * (slip + spread / 2))
    reserve = entry * params.adverse_funding_reserve_bps / 10000
    loss = sign * (entry - stop_fill) + fee * (entry + stop_fill) + reserve
    reward = sign * (target_fill - entry) - fee * (entry + target_fill) - reserve
    if loss <= 0 or reward / loss < params.min_net_reward_risk:
        return reject("NET_REWARD_RISK")
    budget = equity * params.risk_equity_fraction
    notional_cap = min(equity * params.max_notional_equity_fraction,
                       quote.book_base_size * raw_entry * params.book_fraction)
    base = min(budget / loss, notional_cap / entry)
    contracts = _round_step(base / instrument.base_per_contract,
                            instrument.lot_contracts)
    if contracts < instrument.min_contracts:
        return reject("BELOW_MINIMUM_NO_UPSIZE")
    base = contracts * instrument.base_per_contract
    return {"kind": "OFFLINE_ENTRY_PLAN", "setup_id": s.setup_id, "side": s.side,
            "planned_at": quote.received_at, "contracts": contracts,
            "base_quantity": base, "entry_model_price": entry, "stop": s.stop,
            "target": s.target, "notional": base * entry, "risk_budget": budget,
            "modeled_stop_loss_with_reserve": base * loss,
            "net_reward_risk": reward / loss, "leverage": 1,
            "guaranteed_max_loss": False, "can_submit_orders": False}


@dataclass(frozen=True)
class Position:
    side: str
    actual_fill_at: int
    stop: float
    target: float
    b_level: float


def time_exit(position: Position, now: int, owner_stop=False, params=Params()):
    """Return an INTENT, never a fabricated fill/flat state."""
    if owner_stop:
        return "OWNER_STOP_CLOSE_OWNED"
    if now >= position.actual_fill_at + params.max_hold_seconds:
        return "AUTHORED_MAX_HOLD"
    return None


def bar_exit(position: Position, post_fill_bars: list[Bar], now: int, params=Params()):
    """Conservative diagnostic barrier model; not actual quote/hosted-SL proof.

    Refuses a bar containing pre-fill prices. Same-bar stop/target => stop first.
    Actual target/stop execution, reconciliation and funding remain integration gaps.
    """
    due = time_exit(position, now, params=params)
    if not post_fill_bars or any(not bar_valid(b, b.received_at, params)
            or b.received_at > now or b.open_ts < position.actual_fill_at
            for b in post_fill_bars):
        return due or "UNKNOWN_POST_FILL_COVERAGE"
    first_full_open = math.ceil(position.actual_fill_at / HOUR) * HOUR
    if (post_fill_bars[0].open_ts != first_full_open
            or now - post_fill_bars[-1].received_at > params.max_bar_lag_seconds):
        return due or "UNKNOWN_POST_FILL_COVERAGE"
    if any(b.open_ts != a.open_ts + HOUR for a, b in zip(post_fill_bars, post_fill_bars[1:])):
        return due or "UNKNOWN_POST_FILL_GAP"
    sign = 1 if position.side == "long" else -1
    # Earliest exit in chronological completed bars, including formal failures.
    inside_count = 0
    for b in post_fill_bars:
        if b.received_at >= position.actual_fill_at + params.max_hold_seconds:
            return "AUTHORED_MAX_HOLD"  # clock event before this bar is known
        stopped = b.low <= position.stop if sign > 0 else b.high >= position.stop
        target = b.high >= position.target if sign > 0 else b.low <= position.target
        if stopped:
            return "STOP_MODEL_FIRST_IF_AMBIGUOUS"
        if target:
            return "TARGET_MODEL"
        inside_count = inside_count + 1 if sign * (b.close - position.b_level) <= 0 else 0
        if inside_count >= params.failed_breakout_closes:
            return "FAILED_BREAKOUT_CLOSES"
    return due or "HOLD"
