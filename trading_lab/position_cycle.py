"""Owner-started bounded position loop. Stop/restart always converges to recovery."""

from decimal import Decimal
import hashlib
import http.client
import json
from pathlib import Path
import time
import uuid

from .cycle_budget import CYCLE_COST, LEDGER_NAME, MAX_DECISIONS, series_available
from .jev import BudgetLedger, MODEL, read_key, send_once
from .one_trade import (OneTrade, SYMBOL, TERMINAL_RUNS, check, market_plan,
                        positions, no_pending_orders)
from .okx_readonly import PreflightError, decimal_value as D, numeric_text as N, one_row, quote_summary
from .risk import finite_number

PROFILE = "jev-position-v3"
MAX_CYCLE_SECONDS = 300
MAX_ENTRIES = 3
MAX_TOTAL_NOTIONAL = Decimal("30")
MAX_TOTAL_BTC = Decimal("0.0003")
MAX_HOLD_SECONDS = 300
REVIEW_SECONDS = 30
CHECK_SECONDS = 5
STOP_RETURN = Decimal("-0.002")
TARGET_RETURN = Decimal("0.0035")
ADD_RETURN = Decimal("0.001")
STOP_FILE = "stop.request.json"


def candle_features(rows, server_now):
    """Validate contiguous completed 1m bars; ignore the currently forming bar."""
    bars = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 9 or row[8] not in {"0", "1"}:
            raise PreflightError("invalid_candles")
        if row[8] == "0":
            continue
        stamp = D(row[0], positive=True)
        o, h, low, c = (D(v, positive=True) for v in row[1:5])
        volume = D(row[7])
        if stamp % 60000 or not low <= min(o, c) <= max(o, c) <= h or volume < 0:
            raise PreflightError("invalid_candles")
        bars.append((stamp, o, h, low, c, volume))
    bars.sort()
    if len(bars) < 30 or len({row[0] for row in bars}) != len(bars):
        raise PreflightError("insufficient_or_duplicate_candles")
    bars = bars[-30:]
    if any(bars[i][0] - bars[i-1][0] != 60000 for i in range(1, len(bars))):
        raise PreflightError("gapped_candles")
    age = server_now - float(bars[-1][0]) / 1000 - 60
    if not -2 <= age <= 90:
        raise PreflightError("stale_or_future_candles")
    closes = [row[4] for row in bars]
    sma5, sma20 = sum(closes[-5:]) / 5, sum(closes[-20:]) / 20
    returns = {f"return_{n}m_bps": N((closes[-1] / closes[-1-n] - 1) * 10000) for n in (1, 5, 15)}
    return {**returns, "closed_bars": 30, "bar_seconds": 60, "last_close_usdt": N(closes[-1]),
            "last_bar_close_server_ms": N(bars[-1][0] + 60000),
            "sma5_usdt": N(sma5), "sma20_usdt": N(sma20),
            "mean_range_14m_bps": N(sum((b[2]-b[3])/b[4]*10000 for b in bars[-14:]) / 14),
            "volume_last5_usdt": N(sum(b[5] for b in bars[-5:])),
            "volume_previous5_usdt": N(sum(b[5] for b in bars[-10:-5])),
            "baseline_candidate": sma5 > sma20 and closes[-1] > closes[-6]}


def market_snapshot(api, instrument):
    candles = api.get("/api/v5/market/candles", {"instId": SYMBOL, "bar": "1m", "limit": "31"})
    funding = one_row(api.get("/api/v5/public/funding-rate", {"instId": SYMBOL}))
    quote = quote_summary(api.get("/api/v5/market/ticker", {"instId": SYMBOL}), api, "swap", instrument)
    timestamp = api.server_now()
    if funding.get("instId") != SYMBOL or not -2 <= timestamp - float(D(funding.get("ts"))) / 1000 <= 90:
        raise PreflightError("invalid_or_stale_funding")
    due = D(funding.get("fundingTime"), positive=True)
    rate = D(funding.get("fundingRate"))
    if abs(rate) > Decimal("0.05"):
        raise PreflightError("unexpected_funding_rate")
    return {"instrument": SYMBOL, **candle_features(candles, timestamp),
            "bid_usdt": quote["bid_usdt"], "ask_usdt": quote["ask_usdt"], "spread_bps": quote["spread_bps"],
            "funding_rate": N(rate), "seconds_to_funding": round(float(due) / 1000 - timestamp, 3),
            "observed_server_ms": round(timestamp * 1000),
            "assumed_roundtrip_fee_bps": "20", "slippage_allowance_bps": "20"}


def decide(root, market, stage, *, credential_root, transport=send_once, clock=time.time, timer=time.monotonic):
    """Bounded typed choice; no credentials, balance or exchange IDs in the payload."""
    fallback = "skip" if stage == "entry" else "close"
    permitted = {"buy", "skip"} if stage == "entry" else {"hold", "add", "close"}
    fields = {"instrument", "closed_bars", "bar_seconds", "last_close_usdt", "last_bar_close_server_ms",
              "return_1m_bps", "return_5m_bps", "return_15m_bps", "sma5_usdt", "sma20_usdt",
              "mean_range_14m_bps", "volume_last5_usdt", "volume_previous5_usdt", "baseline_candidate",
              "bid_usdt", "ask_usdt", "spread_bps", "funding_rate", "seconds_to_funding", "observed_server_ms",
              "assumed_roundtrip_fee_bps", "slippage_allowance_bps", "position_return_bps",
              "held_seconds", "entries_used", "may_add", "remaining_cycle_seconds"}
    payload = {"model": MODEL, "state": {k: v for k, v in market.items() if k in fields},
        "questions": {"position_action": {"type": "choice", "instructions": (
            "Evaluate a tiny long-only BTC perpetual experiment using only this observed market snapshot. "
            "For entry, choose buy only if continuation of the positive trend plausibly covers the stated "
            "fees and slippage within remaining_cycle_seconds; otherwise skip. This remaining window "
            "already accounts for time spent waiting for entry. For an open position, choose hold if "
            "continuation remains plausible, close if it is weakening or uncertain, or add only if may_add "
            "is true and the trend strengthens. Holding or adding does not reset the 5-minute deadline. "
            "Momentum is not a prediction or proven edge. No averaging down. No new shorts. "
            "Never change size/leverage/time/stop limits. Confidence is not probability of profit."),
            "criteria": {k: {"buy": "Enter the fixed minimum long under a supported upward trend.",
                             "skip": "No compelling entry after costs or insufficient evidence.",
                             "hold": "Keep the existing position until the next bounded review.",
                             "add": "Add one minimum tranche; permitted, profitable so far, strengthening trend.",
                             "close": "Exit the entire accumulated position; weakening, uncertain or exhausted."}[k]
                         for k in sorted(permitted)}}}}
    body = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
    if len(body) > 8000:
        raise PreflightError("jev_payload_limit")
    key = read_key(credential_root)
    if not series_available(Path(credential_root)):
        return {"choice": fallback, "reason": "series_budget_or_billing", "attempted": False}
    ledger = BudgetLedger(Path(root) / "runtime" / LEDGER_NAME, max_attempts=MAX_DECISIONS,
                          cost_threshold_usd=float(CYCLE_COST), cooldown_seconds=REVIEW_SECONDS)
    reservation = ledger.reserve(hashlib.sha256(body).hexdigest(), clock())
    if reservation is None:
        return {"choice": fallback, "reason": "cycle_budget_or_cooldown", "attempted": False}
    started, result, failure = timer(), {}, None
    try:
        result = transport(body, key)
    except (ValueError, TypeError, OSError, http.client.HTTPException):
        failure = "jev_request_failed"
    result = result if isinstance(result, dict) else {}
    usage, answers = result.get("usage"), result.get("answers")
    cost = usage.get("cost") if isinstance(usage, dict) else None
    cost = cost if finite_number(cost) and cost >= 0 else None
    answer = answers.get("position_action") if isinstance(answers, dict) else None
    answer = answer if isinstance(answer, dict) else {}
    choice, confidence = answer.get("choice"), answer.get("confidence")
    if not isinstance(choice, str) or choice not in permitted or not finite_number(confidence) or not 0 <= confidence <= 1:
        failure = failure or "invalid_jev_answer"
    if cost is None or cost > float(CYCLE_COST):
        failure = failure or "jev_cost_unconfirmed_or_above_threshold"
    elapsed = timer() - started
    if elapsed > 15:
        failure = failure or "jev_response_expired"
    ledger.finish(reservation, cost, failure or choice)
    if not failure and (ledger.summary()["reported_cost_usd"] >= float(CYCLE_COST)
                        or not series_available(Path(credential_root))):
        failure = "jev_budget_exhausted"
    return {"choice": fallback if failure else choice, "reason": failure or "valid_market_choice",
            "confidence": confidence if finite_number(confidence) and 0 <= confidence <= 1 else None,
            "cost_usd": cost, "elapsed_ms": round(elapsed * 1000, 3), "attempted": True}


class PositionCycle(OneTrade):
    def __init__(self, root, api, *, decision, **kwargs):
        super().__init__(root, api, **kwargs)
        self.decision = decision
        self.stop_path = self.root / "runtime" / STOP_FILE
        self.opened = None
        self.instrument = None
        self.deadline = None

    def stopped(self):
        stopped = self.stop_path.exists()
        if stopped and self.state and not self.state.get("stop_observed"):
            self.save(stop_observed=True)
            self.event(6, "stop_observed", "warning")
        return stopped

    def expired(self):
        return self.deadline is not None and self.timer() >= self.deadline

    def checked(self, code, action, step=1):
        if step != 6 and self.stopped():
            raise PreflightError("user_stop")
        self.event(step, code, "started")
        started = self.timer()
        try:
            result = action()
        except (Exception, KeyboardInterrupt) as error:
            self.event(step, code, "failed", reason=str(error) if isinstance(error, PreflightError) else type(error).__name__)
            raise
        self.event(step, code, "passed", elapsed_ms=round((self.timer() - started) * 1000, 3))
        return result

    def wait_until(self, deadline):
        while self.timer() < deadline and not self.stopped():
            self.sleep(min(.2, deadline - self.timer()))

    def finish_waiting(self, reason):
        self.event(1, "entry_wait_finished", "finished", reason=reason)
        self.save(phase="SKIPPED", close_reason=reason)
        return self.state

    def totals(self):
        rows = [record.get("result", {}) for record in self.state["entries"]]
        filled = [r for r in rows if D(r.get("accFillSz", "0")) > 0]
        quantity = sum((D(r["accFillSz"]) for r in filled), Decimal(0))
        value = sum((D(r["accFillSz"]) * D(r["avgPx"]) for r in filled), Decimal(0))
        return quantity, value / quantity if quantity else Decimal(0), value * D(self.state["plan"]["contract_btc"])

    def close(self, reason):
        self.save(close_reason=reason)
        self.event(6, "close_requested", "started", reason=reason)
        result = self.checked("close_reconcile", self.recover, step=6)
        self.event(6, "cycle_summary", "finished", reason=reason,
                   remaining_contracts=result.get("remaining_contracts"),
                   pnl_usdt=result.get("pnl_excluding_funding_usdt"), signed_fees_usdt=result.get("signed_fees_usdt"))
        return result

    def review(self, market, stage):
        market = {**market, "remaining_cycle_seconds": round(max(0, self.deadline - self.timer()), 1)}
        self.save(phase="REVIEWING" if stage == "entry" else "POSITION_REVIEW")
        if self.stopped():
            return {"choice": "skip" if stage == "entry" else "close", "reason": "user_stop"}
        self.event(2 if stage == "entry" else 5, "jev_request", "started", **market)
        verdict = self.decision(self.root, market, stage)
        self.state["decisions"].append({"stage": stage, "market": market, **verdict})
        self.save(jev=verdict)
        self.event(2 if stage == "entry" else 5, "jev_decision", "passed" if verdict.get("reason") == "valid_market_choice" else "warning", **verdict)
        return verdict

    def buy(self, reviewed_market, *, adding=False):
        """Every entry has a separate persisted intent. Ambiguous sends only reconcile."""
        if self.stopped():
            return False
        self.event(3, "entry_checks", "started", adding=adding)
        if len(self.state["entries"]) >= MAX_ENTRIES:
            return False
        quantity, average, notional = self.totals()
        if positions(self.api) != quantity:
            raise PreflightError("position_changed_outside_bot_manual_review")
        no_pending_orders(self.api)
        fresh = market_plan(self.api, self.instrument)
        fresh["taker_fee_rate"] = self.state["plan"]["taker_fee_rate"]
        balance = one_row(self.api.get("/api/v5/account/balance", {"ccy": "USDT"}, private=True))
        details = balance.get("details")
        if not isinstance(details, list):
            raise PreflightError("balance_unavailable")
        usdt = one_row([r for r in details if isinstance(r, dict) and r.get("ccy") == "USDT"])
        if D(usdt.get("availBal")) < D(fresh["maximum_entry_notional_usdt"]) * Decimal("1.05"):
            raise PreflightError("insufficient_available_usdt")
        lever = one_row(self.api.get("/api/v5/account/leverage-info", {"instId": SYMBOL, "mgnMode": "isolated"}, private=True))
        if lever.get("instId") != SYMBOL or lever.get("mgnMode") != "isolated" or D(lever.get("lever")) != 1:
            raise PreflightError("isolated_1x_not_confirmed")
        bid, ask = D(fresh["quote"]["bid_usdt"]), D(fresh["quote"]["ask_usdt"])
        if abs(ask / D(reviewed_market["ask_usdt"]) - 1) > Decimal("0.001"):
            raise PreflightError("price_changed_after_jev_no_entry")
        if (self.api.server_now() * 1000 - reviewed_market["observed_server_ms"]) > 20000:
            raise PreflightError("market_review_expired")
        if adding and (bid / average - 1 < ADD_RETURN or bid / average - 1 >= TARGET_RETURN
                       or not reviewed_market["baseline_candidate"]):
            self.event(3, "add_not_allowed", "warning", reason="add_price_or_trend")
            return False
        if (notional + D(fresh["maximum_entry_notional_usdt"]) > MAX_TOTAL_NOTIONAL
                or (quantity + D(fresh["contracts"])) * D(fresh["contract_btc"]) > MAX_TOTAL_BTC):
            self.event(3, "add_not_allowed", "warning", reason="size_limit")
            return False
        if self.stopped() or self.expired() or (self.opened is not None and self.timer() - self.opened >= MAX_HOLD_SECONDS):
            return False
        self.event(3, "entry_checks", "passed", contracts=fresh["contracts"],
                   maximum_entry_notional_usdt=fresh["maximum_entry_notional_usdt"])
        record = {"request": {"instId": SYMBOL, "tdMode": "isolated", "posSide": "net", "side": "buy",
                  "ordType": "ioc", "sz": fresh["contracts"], "px": fresh["entry_limit_usdt"],
                  "reduceOnly": False, "clOrdId": self.state["id"] + "B" + str(len(self.state["entries"]))}}
        self.state["entries"].append(record)
        if not self.state.get("entry"):
            self.state["entry"] = record
        self.save(phase="ENTERING", plan=fresh)
        # Last stop check before sending; a stop arriving during POST is handled by reconciliation.
        if self.stopped():
            return False
        submitted_at = self.timer()
        self.submit(record)
        filled = self.order(record)
        if filled and self.opened is None:
            self.opened = submitted_at  # Includes acknowledgement/reconciliation latency.
            self.save(opened_server_ms=round(self.api.server_now() * 1000))
        quantity, average, notional = self.totals()
        self.save(bought_contracts=N(quantity), remaining_contracts=N(quantity), average_entry_usdt=N(average),
                  entry_notional_usdt=N(notional))
        return bool(filled)

    def risk_sample(self):
        quantity, average, _ = self.totals()
        if positions(self.api) != quantity:
            raise PreflightError("position_changed_outside_bot_manual_review")
        quote = quote_summary(self.api.get("/api/v5/market/ticker", {"instId": SYMBOL}),
                              self.api, "swap", self.instrument)
        bid = D(quote["bid_usdt"])
        change = bid / average - 1
        held = max(0, self.timer() - self.opened)
        paid = [r["result"] for r in self.state["entries"] if D(r.get("result", {}).get("accFillSz", "0")) > 0]
        estimate = None
        if all(r.get("feeCcy") == "USDT" and r.get("fee") not in (None, "") for r in paid):
            base = quantity * D(self.state["plan"]["contract_btc"])
            estimate = (bid - average) * base + sum(D(r["fee"]) for r in paid) - bid * base * D(self.state["plan"]["taker_fee_rate"])
        sample = {"server_ms": round(self.api.server_now() * 1000), "bid_usdt": N(bid),
                  "return_bps": N(change * 10000), "held_seconds": round(held, 1),
                  "estimated_close_pnl_usdt": N(estimate) if estimate is not None else None,
                  "remaining_contracts": N(quantity)}
        self.state["samples"].append(sample)
        self.save(monitor=sample)
        reason = ("user_stop" if self.stopped() else "time_limit" if held >= MAX_HOLD_SECONDS
                  or self.expired()
                  else "stop_loss" if change <= STOP_RETURN else "take_profit" if change >= TARGET_RETURN
                  else "spread_limit" if D(quote["spread_bps"]) > 5 else None)
        self.event(4, "position_sample", "warning" if reason else "passed", **sample, reason=reason)
        return sample, reason

    def monitor(self):
        next_review = self.timer() + REVIEW_SECONDS
        while True:
            if self.stopped():
                return self.close("user_stop")
            if self.expired() or self.timer() - self.opened >= MAX_HOLD_SECONDS:
                return self.close("time_limit")
            if self.api.requests >= 230:
                return self.close("request_reserve")
            self.save(phase="MONITORING")
            sample, reason = self.checked("position_risk", self.risk_sample, step=4)
            if reason:
                return self.close(reason)
            if self.timer() >= next_review:
                if len(self.state["decisions"]) >= MAX_DECISIONS:
                    return self.close("decision_limit")
                self.checked("no_pending_orders", lambda: no_pending_orders(self.api), step=4)
                market = self.checked("market_snapshot", lambda: market_snapshot(self.api, self.instrument), step=4)
                if market["seconds_to_funding"] <= 60:
                    return self.close("funding_near")
                sample, reason = self.risk_sample()
                if reason:
                    return self.close(reason)
                market.update(position_return_bps=sample["return_bps"], held_seconds=sample["held_seconds"],
                              entries_used=len(self.state["entries"]),
                              may_add=(len(self.state["entries"]) < MAX_ENTRIES
                                       and D(sample["return_bps"]) >= ADD_RETURN * 10000
                                       and market["baseline_candidate"]))
                verdict = self.review(market, "position")
                if self.stopped():
                    return self.close("user_stop")
                sample, reason = self.risk_sample()  # Never trust a pre-Jev quote after its response.
                if reason:
                    return self.close(reason)
                choice = verdict.get("choice")
                if choice not in {"hold", "add"}:
                    return self.close("jev_close" if verdict.get("reason") == "valid_market_choice" else "jev_unavailable")
                if choice == "add" and market["may_add"]:
                    self.checked("buy_or_add", lambda: self.buy(market, adding=True), step=3)
                elif choice == "add":
                    self.event(5, "add_not_allowed", "warning", reason="add_price_trend_or_count")
                else:
                    self.event(5, "hold_until_review", "passed", next_review_seconds=REVIEW_SECONDS)
                next_review = self.timer() + REVIEW_SECONDS
                self.save(next_review_in_seconds=REVIEW_SECONDS)
            self.save(phase="HOLDING")
            deadline = min(self.timer() + CHECK_SECONDS, next_review, self.opened + MAX_HOLD_SECONDS, self.deadline)
            self.wait_until(deadline)

    def await_entry(self):
        """A valid skip returns to fresh checks; errors/budgets never cause paid retries."""
        while True:
            if self.stopped():
                return self.close("user_stop")
            if self.expired():
                return self.finish_waiting("entry_time_limit")
            if len(self.state["decisions"]) >= MAX_DECISIONS:
                return self.finish_waiting("decision_limit")
            if self.api.requests >= 210:  # Full preflight + entry still leave close-request capacity.
                return self.finish_waiting("request_reserve")
            self.save(phase="CHECKING")
            self.instrument, plan = check(self.api, checked=self.checked)
            self.save(plan=plan)
            market = self.checked("market_snapshot", lambda: market_snapshot(self.api, self.instrument))
            if self.stopped():
                return self.close("user_stop")
            if self.expired():
                return self.finish_waiting("entry_time_limit")
            if market["seconds_to_funding"] < MAX_HOLD_SECONDS + 60:
                self.event(1, "entry_deferred", "warning", reason="funding_near", seconds_to_funding=market["seconds_to_funding"])
            else:
                verdict = self.review(market, "entry")
                if self.stopped():
                    return self.close("user_stop")
                if self.expired():
                    return self.finish_waiting("entry_time_limit")
                if verdict.get("reason") != "valid_market_choice":
                    return self.finish_waiting(verdict.get("reason") or "jev_unavailable")
                if verdict.get("choice") == "buy" and market["baseline_candidate"]:
                    self.event(2, "entry_accepted", "passed", baseline_candidate=True)
                    return None
                self.event(2, "entry_deferred", "passed", choice=verdict.get("choice"),
                           reason="jev_skip" if verdict.get("choice") == "skip" else "trend_filter",
                           baseline_candidate=market["baseline_candidate"])
            self.save(phase="WAITING_ENTRY", next_review_in_seconds=REVIEW_SECONDS)
            self.event(1, "wait_before_checks", "started", next_review_seconds=REVIEW_SECONDS)
            self.wait_until(min(self.timer() + REVIEW_SECONDS, self.deadline))

    def run(self):
        if not self.api.writes:
            raise PreflightError("explicit_user_launch_required")
        if self.state:
            if self.state.get("phase") in TERMINAL_RUNS:
                return self.state
            return self.close("user_stop" if self.stopped() else "restart_recovery")
        self.deadline = self.timer() + MAX_CYCLE_SECONDS
        code_hash = hashlib.sha256(b"".join((Path(__file__).parent / name).read_bytes()
            for name in ("position_cycle.py", "one_trade.py", "cycle_budget.py", "jev.py", "okx_readonly.py"))).hexdigest()
        self.state = {"version": 3, "profile": PROFILE, "id": "J1" + uuid.uuid4().hex[:24],
                      "account_tag": self.account_tag(), "phase": "CHECKING", "plan": {},
                      "entry": None, "entries": [], "exits": [], "decisions": [], "samples": [],
                      "timeline_version": 1, "events": [], "code_sha256": code_hash,
                      "limits": {"cycle_seconds": MAX_CYCLE_SECONDS, "review_seconds": REVIEW_SECONDS,
                                 "check_seconds": CHECK_SECONDS, "max_decisions": MAX_DECISIONS,
                                 "max_entries": MAX_ENTRIES, "max_notional_usdt": N(MAX_TOTAL_NOTIONAL),
                                 "leverage": 1}}
        self.save()
        self.event(1, "cycle_started", "started", **self.state["limits"])
        try:
            if self.stopped():
                self.save(phase="STOPPED_BEFORE_ENTRY", close_reason="user_stop")
                return self.state
            finished = self.await_entry()
            if finished is not None:
                return finished
            market = self.state["decisions"][-1]["market"]
            if self.stopped():
                return self.close("user_stop")
            if self.expired():
                return self.finish_waiting("entry_time_limit")
            self.event(3, "leverage", "started")
            self.api.post("/api/v5/account/set-leverage", {"instId": SYMBOL, "lever": "1", "mgnMode": "isolated"})
            lever = one_row(self.api.get("/api/v5/account/leverage-info", {"instId": SYMBOL, "mgnMode": "isolated"}, private=True))
            if lever.get("instId") != SYMBOL or lever.get("mgnMode") != "isolated" or D(lever.get("lever")) != 1:
                raise PreflightError("isolated_1x_not_confirmed")
            self.event(3, "leverage", "passed", leverage=1)
            if not self.checked("buy_or_add", lambda: self.buy(market), step=3):
                return self.close("user_stop" if self.stopped() else "time_limit" if self.expired() else "no_fill")
            return self.monitor()
        except (KeyboardInterrupt, Exception) as error:
            if self.stopped():
                return self.close("user_stop")
            code = str(error) if isinstance(error, PreflightError) else type(error).__name__
            self.save(last_error=code)
            self.event(6, "cycle_error", "failed", reason=code)
            return self.close("user_stop" if self.stopped() else "check_failed")
