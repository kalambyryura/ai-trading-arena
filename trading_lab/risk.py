"""Deterministic checks for a synthetic paper candidate; not a live risk engine."""

from dataclasses import dataclass, asdict
import math


def finite_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


@dataclass(frozen=True)
class Snapshot:
    observed_at: float
    revision: str
    symbol: str = "BTC"
    timeframe_minutes: int = 15
    candidate: str = "long"
    notional_usd: float = 100.0
    stop_bps: float = 50.0
    round_trip_cost_bps: float = 14.0
    spread_bps: float = 1.0
    daily_loss_usd: float = 0.0
    experiment_loss_usd: float = 0.0
    open_positions: int = 0
    kill_switch: bool = False
    mode: str = "paper"
    synthetic: bool = True

    def as_state(self):
        return asdict(self)


@dataclass(frozen=True)
class RiskPolicy:
    # Proposed research settings, not user-approved live trading limits.
    max_notional_usd: float = 100.0
    max_planned_loss_usd: float = 1.0
    max_daily_loss_usd: float = 5.0
    max_experiment_loss_usd: float = 100.0
    max_age_seconds: float = 30.0
    max_spread_bps: float = 4.0

    def check(self, state, now):
        if state.mode != "paper":
            return "live_disabled"
        if type(state.kill_switch) is not bool or state.kill_switch:
            return "kill_switch"
        if state.symbol not in {"BTC", "ETH"} or state.candidate not in {"long", "short"}:
            return "no_supported_candidate"
        if not isinstance(state.revision, str) or not 1 <= len(state.revision) <= 128:
            return "invalid_revision"
        if type(state.timeframe_minutes) is not int or state.timeframe_minutes < 1:
            return "invalid_timeframe"
        numeric = (state.observed_at, now, state.notional_usd, state.stop_bps,
                   state.round_trip_cost_bps, state.spread_bps, state.daily_loss_usd,
                   state.experiment_loss_usd)
        if not all(finite_number(value) for value in numeric):
            return "invalid_numeric_state"
        if now < state.observed_at or now - state.observed_at > self.max_age_seconds:
            return "stale_or_future_state"
        if type(state.open_positions) is not int or state.open_positions != 0:
            return "position_exists_or_invalid"
        if min(state.spread_bps, state.round_trip_cost_bps, state.daily_loss_usd,
               state.experiment_loss_usd) < 0:
            return "negative_state_value"
        if state.daily_loss_usd >= self.max_daily_loss_usd:
            return "daily_loss_limit"
        if state.experiment_loss_usd >= self.max_experiment_loss_usd:
            return "experiment_loss_limit"
        if not 0 < state.notional_usd <= self.max_notional_usd or not 0 < state.stop_bps < 10000:
            return "size_or_stop_limit"
        if state.spread_bps > self.max_spread_bps:
            return "spread_limit"
        planned_loss = state.notional_usd * (state.stop_bps + state.round_trip_cost_bps) / 10000
        if planned_loss > self.max_planned_loss_usd:
            return "planned_loss_limit"
        if (state.daily_loss_usd + planned_loss > self.max_daily_loss_usd
                or state.experiment_loss_usd + planned_loss > self.max_experiment_loss_usd):
            return "remaining_loss_budget"
        return None
