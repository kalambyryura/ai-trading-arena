"""User-started, one-shot BTC perpetual round trip. Never starts on import."""

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import signal
import time
from urllib.parse import urlencode, urlsplit
import urllib.error
import urllib.request
import uuid

from .jev import BudgetLedger, MODEL, NoRedirect, read_key, send_once
from .okx_readonly import (
    OkxReader, PreflightError, SITES, PUBLIC_PATHS, PRIVATE_PATHS,
    credentials_from, load_settings, decimal_value as D, numeric_text as N,
    one_row, instrument_summary, quote_summary, config_summary, fee_summary,
)
from .risk import finite_number

SYMBOL = "BTC-USDT-SWAP"
MAX_NOTIONAL = Decimal("10")
MAX_BASE_BTC = Decimal("0.0001")
ALGOS = ("conditional", "oco", "trigger", "move_order_stop", "chase")
EXECUTOR_PUBLIC_PATHS = PUBLIC_PATHS | {"/api/v5/market/candles", "/api/v5/public/funding-rate"}
READ_PATHS = EXECUTOR_PUBLIC_PATHS | PRIVATE_PATHS | {
    "/api/v5/trade/order", "/api/v5/trade/orders-algo-pending", "/api/v5/account/leverage-info"}
WRITE_PATHS = {"/api/v5/trade/order", "/api/v5/trade/cancel-order", "/api/v5/account/set-leverage"}
TERMINAL_ORDERS = {"filled", "canceled", "mmp_canceled"}
TERMINAL_RUNS = {"DONE", "SKIPPED", "NO_FILL", "STOPPED_BEFORE_ENTRY"}
ROOT = Path(__file__).resolve().parent.parent


def send_request(request):
    """Fixed OKX destinations, no retries/redirects, no raw error messages."""
    parsed, method = urlsplit(request.full_url), request.get_method()
    allowed = READ_PATHS if method == "GET" else WRITE_PATHS if method == "POST" else set()
    if (parsed.scheme != "https" or parsed.netloc not in {urlsplit(v).netloc for v in SITES.values()}
            or parsed.path not in allowed or parsed.fragment or (method == "GET" and request.data is not None)):
        raise PreflightError("destination_or_method_forbidden")
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=6) as response:
            raw = response.read(1048577)
        if len(raw) > 1048576:
            raise PreflightError("response_too_large")
        return json.loads(raw)
    except urllib.error.HTTPError as error:
        error.close()
        raise PreflightError("http_" + str(error.code)) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException, UnicodeError, ValueError):
        raise PreflightError("network_or_response_error") from None


class OneTradeAPI(OkxReader):
    """Separate executor; the existing GET-only reader remains unchanged."""

    def __init__(self, site, environment, credentials, *, writes=False, transport=send_request, **kwargs):
        super().__init__(site, environment, credentials, transport=transport, **kwargs)
        self.writes = writes

    def get(self, path, params=None, *, private=False):
        return self.request("GET", path, params=params, private=private)

    def post(self, path, body):
        if not self.writes:
            raise PreflightError("writes_disabled")
        if body.get("instId") != SYMBOL:
            raise PreflightError("wrong_write_instrument")
        if path == "/api/v5/account/set-leverage":
            if body != {"instId": SYMBOL, "lever": "1", "mgnMode": "isolated"}:
                raise PreflightError("only_isolated_1x_allowed")
        elif path == "/api/v5/trade/order":
            if (body.get("tdMode") != "isolated" or body.get("posSide") != "net"
                    or not re.fullmatch(r"J1[0-9a-f]{24}[BE][0-2]", body.get("clOrdId", ""))):
                raise PreflightError("invalid_order_scope")
            entry = body.get("side") == "buy" and body.get("ordType") == "ioc" and body.get("reduceOnly") is False
            close = body.get("side") == "sell" and body.get("ordType") == "market" and body.get("reduceOnly") is True
            cap = Decimal("0.03") if close else Decimal("0.01")
            if not (entry or close) or D(body.get("sz"), positive=True) > cap:
                raise PreflightError("invalid_order_size_or_direction")
            if entry:
                D(body.get("px"), positive=True)
        elif path == "/api/v5/trade/cancel-order":
            if set(body) != {"instId", "clOrdId"} or not re.fullmatch(r"J1[0-9a-f]{24}[BE][0-2]", body.get("clOrdId", "")):
                raise PreflightError("invalid_cancel_scope")
        return self.request("POST", path, body=body, private=True)

    def request(self, method, path, *, params=None, body=None, private=False):
        if method == "GET" and body is not None:
            raise PreflightError("get_body_forbidden")
        if method not in {"GET", "POST"} or path not in (READ_PATHS if method == "GET" else WRITE_PATHS) or (method == "POST" and not self.writes):
            raise PreflightError("request_forbidden")
        if (path not in EXECUTOR_PUBLIC_PATHS and not private) or self.requests >= 300:
            raise PreflightError("private_scope_or_request_limit")
        if private and (self.server_anchor is None or self.monotonic() - self.monotonic_anchor > 20):
            self.calibrate_clock()
        route = path + (("?" + urlencode(params)) if params else "")
        body_text = json.dumps(body, separators=(",", ":"), allow_nan=False) if body is not None else ""
        headers = {"Accept": "application/json", "Content-Type": "application/json",
                   "User-Agent": "TradingOneShot/1", "Cache-Control": "no-cache"}
        if self.environment == "demo":
            headers["x-simulated-trading"] = "1"
        if private:
            if self.credentials is None:
                raise PreflightError("missing_credentials")
            timestamp = datetime.fromtimestamp(self.server_now(), timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            message = (timestamp + method + route + body_text).encode()
            signature = base64.b64encode(hmac.new(self.credentials.secret.encode(), message, hashlib.sha256).digest()).decode()
            headers.update({"OK-ACCESS-KEY": self.credentials.key, "OK-ACCESS-PASSPHRASE": self.credentials.passphrase,
                            "OK-ACCESS-TIMESTAMP": timestamp, "OK-ACCESS-SIGN": signature})
            if method == "POST" and path == "/api/v5/trade/order":
                headers["expTime"] = str(int((self.server_now() + 5) * 1000))
        self.requests += 1
        result = self.transport(urllib.request.Request(self.base + route, method=method,
            data=body_text.encode() if body is not None else None, headers=headers))
        if not isinstance(result, dict) or result.get("code") != "0":
            code = result.get("code") if isinstance(result, dict) else None
            raise PreflightError("okx_" + (code if isinstance(code, str) and re.fullmatch(r"[0-9]{1,8}", code) else "invalid_response"))
        rows = result.get("data")
        if not isinstance(rows, list):
            raise PreflightError("invalid_provider_data")
        if method == "POST":
            row = one_row(rows)
            if row.get("sCode", "0") != "0":
                code = str(row.get("sCode"))
                raise PreflightError("order_" + (code if re.fullmatch(r"[0-9]{1,8}", code) else "rejected"))
        return rows


def positions(api):
    rows = api.get("/api/v5/account/positions", {"instId": SYMBOL}, private=True)
    active = []
    for row in rows:
        if not isinstance(row, dict) or row.get("instId") != SYMBOL:
            raise PreflightError("invalid_position_data")
        amount = D(row.get("pos"))
        if amount:
            if row.get("posSide") != "net" or row.get("mgnMode") != "isolated" or amount < 0:
                raise PreflightError("foreign_position_requires_manual_review")
            active.append(amount)
    if len(active) > 1:
        raise PreflightError("multiple_positions_require_review")
    return sum(active, Decimal(0))


def no_pending_orders(api):
    if api.get("/api/v5/trade/orders-pending", {"instType": "SWAP", "instId": SYMBOL, "limit": "1"}, private=True):
        raise PreflightError("existing_regular_order")
    for kind in ALGOS:
        if api.get("/api/v5/trade/orders-algo-pending", {"ordType": kind, "instId": SYMBOL, "limit": "1"}, private=True):
            raise PreflightError("existing_algo_order")


def market_plan(api, instrument):
    quote = quote_summary(api.get("/api/v5/market/ticker", {"instId": SYMBOL}), api, "swap", instrument)
    size, tick = D(instrument["minimum_order_units"], positive=True), D(instrument["tick_size"], positive=True)
    base = D(instrument["minimum_base_btc"], positive=True)
    # IOC limits entry slippage to 0.1%; it may fill nothing and then exit without a trade.
    ceiling = (D(quote["ask_usdt"]) * Decimal("1.001") / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    if (size > Decimal("0.01") or base > MAX_BASE_BTC or base * ceiling > MAX_NOTIONAL
            or D(quote["spread_bps"]) > 5):
        raise PreflightError("minimum_size_notional_or_spread_limit")
    return {"instrument": SYMBOL, "contracts": N(size), "base_btc": N(base),
            "entry_limit_usdt": N(ceiling), "maximum_entry_notional_usdt": N(base * ceiling),
            "contract_btc": N(D(instrument["contract_value"]) * D(instrument["contract_multiplier"])),
            "margin": "isolated", "leverage": 1, "quote": quote}


def check(api, checked=None):
    checked = checked or (lambda code, action: action())
    checked("clock", api.calibrate_clock)
    def account():
        config = config_summary(api.get("/api/v5/account/config", private=True))
        if config["account_level"] != "2" or config["position_mode"] != "net_mode":
            raise PreflightError("requires_futures_net_mode")
        if "trade" not in config["granted_permissions"]:
            raise PreflightError("trade_permission_required_for_user_launch")
    checked("account_mode_permissions", account)
    def flat():
        if positions(api):
            raise PreflightError("position_already_exists")
    checked("no_position", flat)
    checked("no_pending_orders", lambda: no_pending_orders(api))
    instrument = checked("contract", lambda: instrument_summary(api.get(
        "/api/v5/public/instruments", {"instType": "SWAP", "instId": SYMBOL}), "swap"))
    def funds():
        balance = one_row(api.get("/api/v5/account/balance", {"ccy": "USDT"}, private=True))
        details = balance.get("details")
        if not isinstance(details, list):
            raise PreflightError("balance_unavailable")
        usdt = one_row([r for r in details if isinstance(r, dict) and r.get("ccy") == "USDT"])
        if D(usdt.get("availBal")) < MAX_NOTIONAL * Decimal("1.05"):
            raise PreflightError("insufficient_available_usdt")
    checked("available_funds", funds)
    def fee_check():
        fees = fee_summary(api.get("/api/v5/account/trade-fee", {"instType": "SWAP", "groupId": instrument["fee_group"]}, private=True), instrument)
        if abs(D(fees["taker_signed_rate"])) > Decimal("0.001"):
            raise PreflightError("unexpected_fee_rate")
        return fees
    fees = checked("fee_rate", fee_check)
    plan = checked("entry_size_quote", lambda: market_plan(api, instrument))
    plan["taker_fee_rate"] = N(abs(D(fees["taker_signed_rate"])))
    return instrument, plan


def ask_jev(root, plan, *, transport=send_once, credential_root=None):
    """One paid attempt per journal; shared credentials stay in the project root."""
    payload = {"model": MODEL, "state": {
        "test": "operator_requested_one_buy_then_immediate_reduce_only_close",
        "instrument": SYMBOL, "public_quote": plan["quote"], "contracts": plan["contracts"],
        "base_btc": plan["base_btc"], "max_notional_usdt": "10", "isolated_leverage": 1,
        "purpose": "test_execution_connectivity_not_profit_or_price_direction"},
        "questions": {"one_cycle": {"type": "choice", "instructions": (
            "Review this tiny operator-requested connectivity test. Choose run_test only if the public "
            "quote and proposed contract units are coherent. It buys the minimum then immediately "
            "closes the actual fill; a small fee loss is expected. This is not an investment forecast. "
            "Do not choose direction, increase size/leverage or invent account facts. If uncertain choose skip. "
            "A deterministic program checks the account and limits separately."),
            "criteria": {"run_test": "Public data and fixed minimum-size round-trip test are coherent.",
                         "skip": "Public data or proposed units are uncertain or inconsistent."}}}}
    body = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
    if len(body) > 8000:
        raise PreflightError("jev_payload_limit")
    key = read_key(credential_root or root)
    ledger = BudgetLedger(Path(root) / "runtime" / "jev_one_trade_v1.sqlite3", max_attempts=1)
    reservation = ledger.reserve(hashlib.sha256(body).hexdigest(), time.time())
    if reservation is None:
        return {"choice": "skip", "reason": "one_paid_attempt_already_consumed"}
    started, result, failure = time.monotonic(), {}, None
    try:
        result = transport(body, key)
    except (ValueError, TypeError, OSError, http.client.HTTPException):
        failure = "jev_request_failed"
    result = result if isinstance(result, dict) else {}
    usage, answers = result.get("usage"), result.get("answers")
    cost = usage.get("cost") if isinstance(usage, dict) else None
    cost = cost if finite_number(cost) and cost >= 0 else None
    answer = answers.get("one_cycle") if isinstance(answers, dict) else None
    answer = answer if isinstance(answer, dict) else {}
    choice, confidence = answer.get("choice"), answer.get("confidence")
    if (not isinstance(choice, str) or choice not in {"run_test", "skip"}
            or not finite_number(confidence) or not 0 <= confidence <= 1):
        failure = failure or "invalid_jev_answer"
    if cost is None or cost > .005:
        failure = failure or "jev_cost_unconfirmed_or_above_threshold"
    if time.monotonic() - started > 15:
        failure = failure or "jev_response_expired"
    ledger.finish(reservation, cost, failure or choice)
    return {"choice": "skip" if failure else choice, "reason": failure or "valid_test_choice",
            "confidence": confidence if finite_number(confidence) else None,
            "cost_usd": cost, "elapsed_ms": round((time.monotonic() - started) * 1000, 3)}


@contextmanager
def single_process(root):
    path = Path(root) / "runtime" / "one_trade_v1.lock"
    path.parent.mkdir(exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise PreflightError("bot_already_running") from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def replace_with_retry(source, target, pauses=(0.05, 0.1, 0.2, 0.3, 0.35)):
    """os.replace that waits out a transient Windows sharing lock on the files (antivirus, indexer) for at most 1 s in
    total; a lock that lasts longer, or any other error, still raises."""
    for pause in pauses:
        try:
            return os.replace(source, target)
        except PermissionError:
            time.sleep(pause)
    return os.replace(source, target)


class OneTrade:
    def __init__(self, root, api, *, jev=ask_jev, sleep=time.sleep, timer=time.monotonic, log=print):
        self.root, self.api = Path(root), api
        self.path = self.root / "runtime" / "one_trade_v1.json"
        self.path.parent.mkdir(exist_ok=True)
        self.jev, self.sleep, self.timer, self.log = jev, sleep, timer, log
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else None

    def event(self, step, code, status="passed", **data):
        # New journals opt in. Old journals retain their original, incomplete history.
        if not self.state or self.state.get("timeline_version") != 1:
            return
        self._append_event(step, code, status, data)
        self.save()

    def _append_event(self, step, code, status, data):
        events = self.state.setdefault("events", [])
        events.append({"seq": len(events) + 1, "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                       "step": step, "code": code, "status": status, "data": data})

    def save(self, **changes):
        if (self.state.get("timeline_version") == 1 and "phase" in changes
                and changes["phase"] != self.state.get("phase")):
            phase = changes["phase"]
            step = {"CHECKING": 1, "WAITING_ENTRY": 1, "REVIEWING": 2, "ENTERING": 3,
                    "MONITORING": 4, "HOLDING": 4, "POSITION_REVIEW": 5}.get(phase, 6)
            self._append_event(step, "phase", "finished" if phase in TERMINAL_RUNS else "started", {"phase": phase})
        self.state.update(changes)
        temp = self.path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(self.state, handle, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(temp, self.path)

    def account_tag(self):
        # Stable binding prevents a resume from operating on another key/account.
        return hashlib.sha256((self.api.site + ":" + self.api.environment + ":" + self.api.credentials.key).encode()).hexdigest()

    def submit(self, record):
        record["send_started"] = True
        self.save()  # Persist BEFORE transmission; never repeat a POST after an ambiguous result.
        request = record["request"]
        step = 3 if request["side"] == "buy" else 6
        self.event(step, "order_send", "started", direction=request["side"], contracts=request["sz"],
                   limit_price_usdt=request.get("px"), reduce_only=request.get("reduceOnly") is True)
        try:
            self.api.post("/api/v5/trade/order", record["request"])
            self.event(step, "order_ack", "passed", direction=request["side"])
        except PreflightError as error:
            record["submission_error"] = str(error)
            self.save()
            self.event(step, "order_ack_unknown", "warning", reason=str(error))

    def order(self, record):
        step = 6 if self.state.get("phase") == "CLOSING" or record["request"]["side"] == "sell" else 3
        self.event(step, "order_reconcile", "started", direction=record["request"]["side"])
        deadline = self.timer() + 12
        while self.timer() < deadline:
            try:
                row = one_row(self.api.get("/api/v5/trade/order", {
                    "instId": SYMBOL, "clOrdId": record["request"]["clOrdId"]}, private=True))
                request = record["request"]
                if (row.get("instId") != SYMBOL or row.get("clOrdId") != request["clOrdId"]
                        or row.get("side") != request["side"] or row.get("tdMode") != "isolated"
                        or row.get("posSide") != "net" or D(row.get("sz")) != D(request["sz"])):
                    raise PreflightError("order_identity_mismatch")
                filled = D(row.get("accFillSz"))
                if not 0 <= filled <= D(request["sz"]):
                    raise PreflightError("invalid_filled_quantity")
                if row.get("state") in TERMINAL_ORDERS:
                    record["result"] = {k: row.get(k, "") for k in (
                        "ordId", "clOrdId", "state", "accFillSz", "avgPx", "fee", "feeCcy", "uTime")}
                    if filled:
                        D(record["result"]["avgPx"], positive=True)
                    self.save()
                    self.event(step, "order_result", "passed", direction=request["side"],
                               order_state=row.get("state"), filled_contracts=N(filled),
                               average_price_usdt=row.get("avgPx"), fee_usdt=row.get("fee") if row.get("feeCcy") == "USDT" else None,
                               server_ms=row.get("uTime"))
                    return filled
            except PreflightError as error:
                if str(error) in {"order_identity_mismatch", "invalid_filled_quantity"}:
                    raise
            self.sleep(.4)
        # Cancellation only addresses this run's order, never an unrelated position/order.
        try:
            self.event(step, "cancel_unresolved_order", "warning")
            self.api.post("/api/v5/trade/cancel-order", {"instId": SYMBOL, "clOrdId": record["request"]["clOrdId"]})
        except PreflightError:
            pass
        raise PreflightError("order_result_unresolved_resume_same_bot_no_new_buy")

    def recover(self):
        self.event(6, "recovery", "started")
        if self.state.get("account_tag") != self.account_tag():
            raise PreflightError("journal_belongs_to_different_account_key")
        config = config_summary(self.api.get("/api/v5/account/config", private=True))
        if config["account_level"] != "2" or config["position_mode"] != "net_mode":
            raise PreflightError("account_mode_changed_manual_review")
        entries = self.state.get("entries", [self.state.get("entry")])
        entries = [entry for entry in entries if entry and entry.get("send_started")]
        if not entries:
            self.save(phase="STOPPED_BEFORE_ENTRY")
            return self.state
        self.save(phase="CLOSING")
        self.log("Reconciling the existing entry; no additional buy will be sent.")
        bought = sum((self.order(entry) for entry in entries), Decimal(0))
        if not bought:
            if positions(self.api):
                raise PreflightError("unexpected_position_after_unfilled_order")
            self.save(phase="NO_FILL")
            return self.state
        sold = Decimal(0)
        for close in self.state["exits"]:
            if not close.get("send_started"):
                self.submit(close)
            sold += self.order(close)
        while sold < bought and len(self.state["exits"]) < 3:
            remaining = bought - sold
            # Allow brief exchange propagation, but never claim zero exposure just from ACKs.
            for _ in range(5):
                current = positions(self.api)
                if current == remaining:
                    break
                self.sleep(.3)
            if current != remaining:
                raise PreflightError("position_changed_outside_bot_manual_review")
            close = {"request": {"instId": SYMBOL, "tdMode": "isolated", "posSide": "net",
                "side": "sell", "ordType": "market", "sz": N(remaining), "reduceOnly": True,
                "clOrdId": self.state["id"] + "E" + str(len(self.state["exits"]))}}
            self.state["exits"].append(close)
            self.save(phase="CLOSING")
            self.log("Closing only the actual filled quantity (reduce-only).")
            self.submit(close)
            sold += self.order(close)
        if sold != bought:
            raise PreflightError("close_incomplete_manual_review_required")
        for _ in range(5):
            remaining = positions(self.api)
            if remaining == 0:
                break
            self.sleep(.3)
        if remaining:
            raise PreflightError("position_not_flat")
        no_pending_orders(self.api)
        self.event(6, "flat_no_pending", "passed", remaining_contracts="0")
        entry_results = [entry["result"] for entry in entries]
        exit_results = [r["result"] for r in self.state["exits"]]
        results = entry_results + exit_results
        entry_value = sum((D(r["accFillSz"]) * D(r["avgPx"]) for r in entry_results if D(r["accFillSz"]) > 0), Decimal(0))
        exit_value = sum((D(r["accFillSz"]) * D(r["avgPx"]) for r in exit_results if D(r["accFillSz"]) > 0), Decimal(0))
        pnl = (exit_value - entry_value) * D(self.state["plan"]["contract_btc"])
        fees = None
        paid_results = [r for r in results if D(r["accFillSz"]) > 0]
        if all(r["feeCcy"] == "USDT" and r["fee"] != "" for r in paid_results):
            fees = sum((D(r["fee"]) for r in paid_results), Decimal(0))
        self.save(phase="DONE", bought_contracts=N(bought), sold_contracts=N(sold),
            remaining_contracts="0", gross_price_pnl_usdt=N(pnl), signed_fees_usdt=N(fees) if fees is not None else None,
            pnl_excluding_funding_usdt=N(pnl + fees) if fees is not None else None, funding_reconciled=False,
            average_entry_usdt=N(entry_value / bought), average_exit_usdt=N(exit_value / sold))
        self.log("DONE: entry and close confirmed; remaining position = 0. Bot finished.")
        return self.state

    def run(self):
        if not self.api.writes:
            raise PreflightError("explicit_user_launch_required")
        if self.state:
            if self.state.get("phase") in TERMINAL_RUNS:
                self.log("This one-shot run is already finished. No new buy or Jev call.")
                return self.state
            return self.recover()
        instrument, plan = check(self.api)
        self.state = {"version": 1, "id": "J1" + uuid.uuid4().hex[:24], "account_tag": self.account_tag(),
                      "phase": "REVIEWING", "plan": plan, "entry": None, "exits": []}
        self.save()
        self.log("One Jev request; no order yet.")
        verdict = self.jev(self.root, plan)
        self.save(jev=verdict)
        if verdict.get("choice") != "run_test":
            self.save(phase="SKIPPED")
            self.log("SKIPPED by Jev: no order placed.")
            return self.state
        if positions(self.api):
            raise PreflightError("position_appeared_before_entry")
        no_pending_orders(self.api)
        self.api.post("/api/v5/account/set-leverage", {"instId": SYMBOL, "lever": "1", "mgnMode": "isolated"})
        lever = one_row(self.api.get("/api/v5/account/leverage-info", {"instId": SYMBOL, "mgnMode": "isolated"}, private=True))
        if lever.get("instId") != SYMBOL or lever.get("mgnMode") != "isolated" or D(lever.get("lever")) != 1:
            raise PreflightError("isolated_1x_not_confirmed")
        fresh = market_plan(self.api, instrument)
        if abs(D(fresh["quote"]["ask_usdt"]) / D(plan["quote"]["ask_usdt"]) - 1) > Decimal("0.001"):
            raise PreflightError("price_changed_after_jev_no_entry")
        entry = {"request": {"instId": SYMBOL, "tdMode": "isolated", "posSide": "net", "side": "buy",
            "ordType": "ioc", "sz": fresh["contracts"], "px": fresh["entry_limit_usdt"], "reduceOnly": False,
            "clOrdId": self.state["id"] + "B0"}}
        self.save(phase="ENTERING", plan=fresh, entry=entry)
        self.log("Submitting the single minimum-size IOC entry.")
        self.submit(entry)
        return self.recover()


def other_unfinished_cycle(state_root):
    """All user entrypoints share one position; never start around an older journal."""
    journals = [ROOT / "runtime" / "one_trade_v1.json"]
    journals += list((ROOT / "runtime" / "web_runs").glob("*/runtime/one_trade_v1.json"))
    target = state_root / "runtime" / "one_trade_v1.json"
    for path in journals:
        if path != target and path.exists():
            state = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or state.get("phase") not in TERMINAL_RUNS:
                raise PreflightError("another_unfinished_cycle_resume_it_first")


def main(argv=None):
    parser = argparse.ArgumentParser(description="One Jev-reviewed BTC perpetual round trip; user-started only.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="GET checks only: no orders, settings writes or paid Jev call.")
    mode.add_argument("--live", action="store_true", help="REAL FUNDS: one <=10 USDT notional cycle, then automatic exit.")
    parser.add_argument("--web-run", help="Panel-owned journal ID; no automatic repeat or reset.")
    parser.add_argument("--strategy", choices=("roundtrip", "monitored"), default="roundtrip")
    args = parser.parse_args(argv)
    if args.web_run and (not args.live or not re.fullmatch(r"[0-9a-f]{32}", args.web_run)):
        parser.error("--web-run requires --live and a 32-character journal ID")
    state_root = ROOT / "runtime" / "web_runs" / args.web_run if args.web_run else ROOT
    bot = None
    try:
        site, environment, values = load_settings(ROOT)
        if environment != "live":
            raise PreflightError("this_launcher_requires_OKX_ENV_live")
        api = OneTradeAPI(site, environment, credentials_from(values), writes=args.live)
        if args.check:
            _, plan = check(api)
            print(json.dumps({"status": "CHECK_ONLY_OK", "plan": plan, "requests": api.requests,
                              "orders_submitted": 0, "settings_writes": 0, "jev_calls": 0}, indent=2))
            return 0
        print("LIVE: isolated 1x; automatic reduce-only close; " +
              ("up to 3 entries, <=30 USDT total, <=5 minutes." if args.strategy == "monitored"
               else "one entry, <=10 USDT notional."))
        print("Keep this window open until DONE. Ctrl+C requests recovery/close; force-kill cannot do that.")
        with single_process(ROOT):
            other_unfinished_cycle(state_root)
            state_root.mkdir(parents=True, exist_ok=True)
            if args.strategy == "monitored":
                from .position_cycle import PositionCycle, decide
                bot = PositionCycle(state_root, api,
                    decision=lambda root, market, stage: decide(root, market, stage, credential_root=ROOT))
            else:
                bot = OneTrade(state_root, api, jev=lambda root, plan: ask_jev(root, plan, credential_root=ROOT))
            try:
                result = bot.run()
            except (KeyboardInterrupt, Exception) as error:
                # Ctrl+C cannot interrupt this bounded recovery a second time.
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                if bot.state:
                    bot.save(last_error=str(error) if isinstance(error, PreflightError) else type(error).__name__,
                             close_reason=bot.state.get("close_reason") or
                             ("interrupted" if isinstance(error, KeyboardInterrupt) else "check_failed"))
                    result = bot.recover()
                else:
                    raise
            print(json.dumps(result, indent=2))
            return 0 if result.get("phase") == "DONE" else 2
    except (KeyboardInterrupt, Exception) as error:
        message = str(error) if isinstance(error, PreflightError) else type(error).__name__
        print("STOPPED: " + message)
        if bot and bot.state and (bot.state.get("entry") or {}).get("send_started"):
            print("Entry may exist. Check OKX BTC-USDT-SWAP immediately. Restart the SAME bot for recovery only.")
        print("Local journal: " + str(state_root / "runtime" / "one_trade_v1.json"))
        print("Do not delete the journal or Jev ledger to retry.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
