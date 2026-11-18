"""Bounded owner-operated OKX executor, independent of the legacy one-shot bot.

Construction and preflight are GET-only. The controller arms writes only after an
explicit owner start, account isolation checks and a durable run reservation.
"""
import base64
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import hmac
import http.client
import json
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlsplit, parse_qsl

from .exchange_accounts import account_tag, load_profile
from .arena_strategies import ArenaError
from .jev import NoRedirect
from .ops_policy import RollingRequestBudget
from .okx_readonly import (OkxReader, PreflightError, SITES, config_summary, one_row,
                           instrument_summary, fee_summary, decimal_value as D, numeric_text as N)

INSTRUMENT = "BTC-USDT-SWAP"  # default; a season trades one contract (exchange_engine.season_instrument)
CLIENT_ID = re.compile(r"J5[0-9a-f]{16}[0-5][EC][0-9]{4}")
AMEND_ID = re.compile(r"J5[0-9a-f]{16}[0-5][AC][0-9]{4}")  # A since R2; C in journals from before
GET_PATHS = {
    "/api/v5/public/time", "/api/v5/public/instruments", "/api/v5/market/ticker",
    "/api/v5/market/candles", "/api/v5/public/funding-rate",
    "/api/v5/account/config", "/api/v5/account/instruments", "/api/v5/account/balance", "/api/v5/asset/balances",
    "/api/v5/account/positions", "/api/v5/account/trade-fee", "/api/v5/account/leverage-info",
    "/api/v5/account/bills", "/api/v5/trade/orders-pending", "/api/v5/trade/orders-algo-pending",
    "/api/v5/trade/order", "/api/v5/trade/order-algo",
    # schema-4 feature book (strategy_v4_feed.routes): public market history of the season's contract, no account data
    "/api/v5/market/history-candles", "/api/v5/market/history-index-candles",
    "/api/v5/market/history-mark-price-candles", "/api/v5/public/funding-rate-history",
    "/api/v5/public/position-tiers",  # R2.1.15 (D181 p. 2): public, the isolated tiers of a leveraged bot's contract
}
POST_PATHS = {"/api/v5/trade/order", "/api/v5/trade/cancel-order", "/api/v5/account/set-leverage",
              "/api/v5/trade/order-algo", "/api/v5/trade/cancel-algos", "/api/v5/trade/amend-algos"}
ALGOS = ("conditional", "oco", "trigger", "move_order_stop", "chase")
TERMINAL = {"filled", "canceled", "mmp_canceled"}
DEMO_FUNDING_WARNING = ("OKX Demo повернув дату funding у минулому. Календар і прогноз ставки невідомі; "
                        "фактичний funding звіряється лише за OKX bills.")


def market_diagnostic(error, feed=None):
    """Return fixed local descriptions/codes, never raw exception/provider text."""
    known = {
        "Дата funding у минулому.": ("funding", "funding_schedule_past"),
        "Невідома або застаріла ставка funding.": ("funding", "funding_stale_or_invalid"),
        "Застарілі або перехресні котирування.": ("ticker", "quote_stale_or_crossed"),
        "Немає завершених свічок.": ("candles", "candles_missing"),
        "Некоректна форма свічок.": ("candles", "candles_invalid_shape"),
        "Нечинна або майбутня свічка.": ("candles", "candle_invalid_or_future"),
        "Недостатньо унікальних завершених свічок.": ("candles", "candles_insufficient_or_duplicate"),
        "Пропуск у хвилинних свічках.": ("candles", "candles_gap"),
        "Застарілі завершені свічки.": ("candles", "candles_stale"),
        "Інший інструмент у стрічці.": ("validation", "market_instrument_mismatch"),
        "Непідтримуваний контракт.": ("instrument", "market_contract_unsupported"),
    }
    text = str(error)
    stage = getattr(feed, "stage", "validation")
    stage = stage if stage in {"instrument", "candles", "funding", "ticker", "clock", "validation", "journal"} else "validation"
    reason, message = "market_invalid", "Некоректний або неповний пакет ринкових даних."
    if isinstance(error, ArenaError) and text in known:
        stage, reason = known[text]
        message = text  # Exact match to one of the fixed strings above.
    elif isinstance(error, PreflightError):
        fixed = {
            "network_error": "Не вдалося отримати відповідь OKX у межах таймауту.",
            "invalid_json_response": "OKX повернув нечинний JSON.",
            "invalid_provider_envelope": "Нечинна форма відповіді OKX.",
            "response_too_large": "Відповідь OKX перевищила ліміт розміру.",
            "ambiguous_public_market_data": "Публічна відповідь OKX порожня або неоднозначна.",
            "exchange_public_feed_limit": "Вичерпано ліміт публічних GET.",
            "rolling_read_rate_limit": "Досягнуто короткочасного ліміту читань OKX.",
            "market_arrival_stale": "Пакет ринкових даних отримано запізно.",
            "market_gap_or_clock_regression": "Пропуск даних або регресія часу котирування.",
            "session_clock_source_disagreement": "Час OKX не узгоджується з монотонним годинником поточного процесу.",
            "snapshot_journal_limit": "Журнал котирувань перевищив ліміт розміру.",
        }
        if text in fixed:
            reason, message = text, fixed[text]
        elif re.fullmatch(r"(?:http_[0-9]{3}|okx_error_[0-9]{1,8})", text):
            reason, message = text, "OKX відхилив публічний GET: " + text + "."
    return {"stage": stage, "reason": reason, "message": message}


ERROR_BODY_BYTES = 65536  # R2.1.10 (A1): the most of an HTTP error body read for its OKX code


def venue_error(reason, *, http_status=None, okx_code=None):
    """A PreflightError with the venue's HTTP status and OKX code; request() adds the endpoint and method."""
    error = PreflightError(reason)
    error.http_status, error.okx_code = http_status, okx_code
    return error


def okx_code(payload, unknown="unknown"):
    code = payload.get("code")
    return code if isinstance(code, str) and re.fullmatch(r"[0-9]{1,8}", code) else unknown


def error_code(error):
    """The OKX code in an HTTP error body, or None. Read apart, so a broken body never hides the HTTP status; the
    body's msg, the headers and the body itself are never kept."""
    try:
        raw = error.read(ERROR_BODY_BYTES + 1)
        if len(raw) > ERROR_BODY_BYTES:
            return None
        code = json.loads(raw).get("code")
    except (http.client.HTTPException, OSError, RecursionError, ValueError, AttributeError):
        return None  # IncompleteRead, a timeout, a nested or broken JSON, not an object
    return code if isinstance(code, str) and re.fullmatch(r"[0-9]{1,8}", code) else None


def send(request):
    method = request.get_method()
    parsed = urlsplit(request.full_url)
    if (parsed.scheme != "https" or parsed.netloc not in {urlsplit(s).netloc for s in SITES.values()}
            or parsed.fragment or parsed.username or parsed.password
            or method not in {"GET", "POST"}
            or parsed.path not in (GET_PATHS if method == "GET" else POST_PATHS)
            or (method == "GET" and request.data is not None)):
        raise PreflightError("exchange_destination_or_method_forbidden")
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=6) as response:
            raw = response.read(1048577)
            request.http_status = response.status  # R2.1.10 (A1): the status goes with the answer, explicitly
    except urllib.error.HTTPError as error:
        status = error.code
        try:
            code = error_code(error)
        finally:
            error.close()
        raise venue_error("http_" + str(status), http_status=status, okx_code=code) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        raise PreflightError("network_error") from None
    if len(raw) > 1048576:
        raise PreflightError("response_too_large")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise PreflightError("invalid_json_response") from None


# R2.1.15 (QR-01, RC2 PP-01): the explicit refusals of a stop's POST: parameters, instrument, funds, and the trigger
# already beyond the last price (51278 / 51280, OKX Error codes)
STOP_REFUSALS = frozenset({"51000", "51001", "51008", "51278", "51280"})
# R2.1.15: the owned position row's facts kept for the entry_position_facts event (raw OKX strings)
POSITION_FACTS = ("lever", "mgnMode", "avgPx", "liqPx", "notionalUsd", "margin", "mmr", "adl")

class ProtectionRejected(PreflightError):
    """A matched negative placement response, not a timeout or ambiguous ACK."""
    def __init__(self, code):
        self.code = code
        super().__init__("protective_placement_rejected_" + code)


# R2.1.6: a refusal only for codes the official OKX v5 Error Code table (https://www.okx.com/docs-v5/en/, read
# 2026-11-03) describes as an order that failed validation (HTTP 200); its text is next to each code. OKX docs
# (Place order): sCode "0 means success"; the "Response Example When All Failed" has ordId "" with sCode 51008. Every
# other answer stays uncertain and the 51603 rule decides: the timeouts 51054 and 51149 ("does not mean that the
# request was successful or failed, please check the request result"), the duplicates 51011 and 51016 ("already
# exists"), the system codes 50xxx, and any 51xxx not listed here (R2.1.5 took the whole class: a review found it unsafe).
REJECTED_ORDER_CODES = frozenset({
    "51000",  # Parameter {param0} error
    "51001",  # Instrument ID, Instrument ID code or Spread ID doesn't exist.
    "51004",  # Order failed. ... can't be more than {param1}(contracts) which is the maximum position amount ... (51004_11xx)
    "51005",  # Your order amount exceeds the max order amount.
    "51006",  # Order price is not within the price limit (max buy price ..., min sell price ...)
    "51007",  # Order failed. Please place orders of at least 1 contract or more.
    "51008",  # Order failed. Insufficient USDT balance in account (the docs' failure example)
    "51010",  # Request unsupported under current account mode
    "51020",  # Your order should meet or exceed the minimum order amount.
    "51024",  # Trading account is blocked. (and its account-restriction variants)
    "51112",  # Close order size exceeds available size for this position.
    "51116",  # Order price or trigger price exceeds {param0}.
    "51120",  # Order amount is less than {param0}, please try again.
    "51121",  # Order quantity must be a multiple of the lot size.
    "51127",  # Available balance is 0.
    "51131",  # Insufficient balance.
    "51135",  # Your closing price has triggered the limit price, and the max buy price is {param0}.
    "51136",  # Your closing price has triggered the limit price, and the min sell price is {param0}.
    "51137",  # The highest price limit for buy orders is {param0}.
    "51138",  # The lowest price limit for sell orders is {param0}.
    "51148",  # Failed to place order. The new order may execute an opposite trading direction of your existing reduce-only positions.
    "51169",  # Order failed because you don't have any positions in this direction for this contract to reduce or close.
})


class OrderRejected(PreflightError):
    """A matched negative placement response for this client ID (a listed sCode, no ordId): the order was not created."""
    def __init__(self, code):
        self.code = code
        super().__init__("order_rejected_" + code)


# R2.1.7: a cancel the venue refuses because nothing is there to cancel (official Error Code table): 51400 "Order
# cancellation failed as the order has been filled, canceled or does not exist.", 51401/51402 (already canceled /
# completed), 51603 "Order does not exist." and (R2.1.8) 51405 "Cancellation failed as you don't have any pending
# orders.". Not here: 51410 (already in canceling status), 51412 (timed out) and every other code.
NOTHING_TO_CANCEL_CODES = frozenset({"51400", "51401", "51402", "51405", "51603"})


class CancelRefused(PreflightError):
    """A cancel answer with a NOTHING_TO_CANCEL_CODES code: no live order to cancel."""
    def __init__(self, code, top):
        self.code = code
        super().__init__("okx_error_" + top)  # the same diagnostic code as before


class CancelFailed(PreflightError):
    """R2.1.8: a cancel answer with another code (journalled as it is): the lab does not know whether an order is live."""
    def __init__(self, code, top):
        self.code = code
        super().__init__("okx_error_" + top)


class OrderAbsent(PreflightError):
    """GET of this client ID answered 51603 "Order does not exist" (as ccxt and gocryptotrader map it); one read is no
    proof: the order may not be visible yet. The lab decides after repeated reads over time (exchange_lab)."""
    def __init__(self):
        super().__init__("okx_error_51603")  # the same diagnostic code as before


class ExchangeAPI(OkxReader):
    def __init__(self, site, environment, credentials, transport=send, **kwargs):
        super().__init__(site, environment, credentials, transport, **kwargs)
        self.writes = False
        self.entry_limit = Decimal(0)
        self.close_only = False
        self.write_count = 0
        self.request_limit = 10000
        self.recovery_request_reserve = 4096
        self.recovery_transport = False
        self.rolling_reads = None
        self.index_reads = None  # R2.1.12 (MIX): history-index-candles is 10/2 s per IP at OKX; shared by a mixed season's feeds
        self.owned_algos = {}
        self.calibration_rtts = []  # R2.1.10 (C): every clock calibration's round trip, ms, until step_timing takes them
        self.inst_id = INSTRUMENT  # the season's contract; the lab sets it before preflight
        self.max_entry_units = None  # G12 TEST-LIVE: the lab sets the one-lot cap, checked on every entry write
        self.leverage = 1  # R2.1.15: the bot's leverage, set by the lab before arm; every position read and setting checks it
        self.last_position = None  # R2.1.15: the facts of the owned row of the last position read (None when flat)

    @classmethod
    def from_profile(cls, root, profile):
        return cls(*load_profile(root, profile))

    def get(self, path, params=None, *, private=False):
        return self.request("GET", path, params, private=private)

    def fresh_server_now(self):
        """Server time for a request window (a bills end, the start mark), renewed by the signed-request rule (an anchor
        older than 20 s). bills() used to renew only a missing anchor: after one request slower than 10 s (a starved
        worker, R2.1.1 on the other PC) every account check failed with server_clock_calibration_expired."""
        if self.server_anchor is None or self.monotonic() - self.monotonic_anchor > 20:
            self.calibrate_clock()
        return self.server_now()

    def session_server_now(self, max_age=300):
        """Server time from the last calibration for the session timebase, read under the lab lock by Start and Resume.
        A monotonic extrapolation stays exact to milliseconds over minutes (clock drift is a few ppm), so a wait for the
        lock no longer fails them with an expired anchor (it surfaced as a misleading balance error); a signature still
        needs an anchor renewed by the 20 s rule."""
        if self.server_anchor is None or self.monotonic_anchor is None:
            raise PreflightError("server_clock_not_calibrated")
        elapsed = self.monotonic() - self.monotonic_anchor
        if not 0 <= elapsed <= max_age:
            raise PreflightError("server_clock_calibration_expired")
        return self.server_anchor + elapsed

    def request(self, method, path, values=None, *, private=True):
        """R2.1.10 (A1): a failure carries its endpoint (an allowlisted path, never the URL: its query holds clOrdId,
        ordId and billId), the method, and the HTTP status and OKX code when the venue gave them."""
        self.last_http_status = None
        try:
            return self._request(method, path, values, private=private)
        except PreflightError as error:
            if getattr(error, "endpoint", None) is None:  # a nested clock request names its own
                error.endpoint = path if path in GET_PATHS | POST_PATHS else None
                error.method = method if method in {"GET", "POST"} else None
                if getattr(error, "http_status", None) is None:
                    error.http_status = self.last_http_status
            raise

    def _request(self, method, path, values=None, *, private=True):
        request_cap =self.request_limit + (self.recovery_request_reserve if self.recovery_transport else 0)
        if method == "GET" and self.rolling_reads is not None:
            if not self.rolling_reads.acquire(self.monotonic(), critical=self.recovery_transport):
                raise PreflightError("rolling_read_rate_limit")
            if (self.index_reads is not None and path == "/api/v5/market/history-index-candles"
                    and not self.index_reads.acquire(self.monotonic())):
                raise PreflightError("rolling_read_rate_limit")
        elif self.rolling_reads is None and self.requests >= request_cap:
            raise PreflightError("exchange_request_limit")
        if self.recovery_transport and (
                (not private and path != "/api/v5/public/time")
                or (method == "POST" and path == "/api/v5/account/set-leverage")
                or (method == "POST" and path == "/api/v5/trade/order" and values.get("reduceOnly") is not True)):
            raise PreflightError("recovery_transport_only_account_and_close")
        if method == "GET":
            if path not in GET_PATHS:
                raise PreflightError("exchange_endpoint_forbidden")
        elif method == "POST":
            if not self.writes or path not in POST_PATHS or self.write_count >= 120:
                raise PreflightError("exchange_write_not_armed_or_limit")
            self.validate_write(path, values)
        else:
            raise PreflightError("exchange_method_forbidden")
        if private and (self.server_anchor is None or self.monotonic() - self.monotonic_anchor > 20):
            try:
                self.calibrate_clock()
            except PreflightError as error:
                if getattr(error, "endpoint", None) is None:  # R2.1.11 (R2110-LABELS): e.g. unreliable_clock_sample
                    error.endpoint, error.method, error.http_status = "/api/v5/public/time", "GET", None
                raise
        route = path + (("?" + urlencode(values)) if method == "GET" and values else "")
        body = json.dumps(values, separators=(",", ":"), allow_nan=False).encode() if method == "POST" else None
        headers = {"Accept": "application/json", "Cache-Control": "no-cache", "User-Agent": "JevTradingLab/5"}
        if self.environment == "demo":
            headers["x-simulated-trading"] = "1"
        if private:
            stamp = datetime.fromtimestamp(self.server_now(), timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            message = (stamp + method + route).encode() + (body or b"")
            signed = base64.b64encode(hmac.new(self.credentials.secret.encode(), message, hashlib.sha256).digest()).decode()
            headers.update({"OK-ACCESS-KEY": self.credentials.key, "OK-ACCESS-PASSPHRASE": self.credentials.passphrase,
                            "OK-ACCESS-TIMESTAMP": stamp, "OK-ACCESS-SIGN": signed})
        if body is not None:
            headers["Content-Type"] = "application/json"
            if path == "/api/v5/trade/order":
                headers["expTime"] = str(int((self.server_now() + 5) * 1000))
            self.write_count += 1
        self.requests += 1
        sent = urllib.request.Request(self.base + route, data=body, method=method, headers=headers)
        self.last_http_status = None  # not a nested clock request's
        payload = self.transport(sent)
        self.last_http_status = getattr(sent, "http_status", None)  # send() sets it; a fake transport may not
        if not isinstance(payload, dict):
            raise PreflightError("invalid_provider_envelope")
        if okx_code(payload, None) not in {None, "0"} and not isinstance(payload.get("data"), list):  # R2.1.11: digits only
            raise venue_error("okx_error_" + okx_code(payload), okx_code=okx_code(payload, None))  # the code before the data
        if not isinstance(payload.get("data"), list):
            raise PreflightError("invalid_provider_envelope")
        if method == "POST" and path == "/api/v5/trade/order" and payload.get("code") in {"0", "1"}:
            rows = payload["data"]
            if len(rows) == 1 and isinstance(rows[0], dict):
                row, code = rows[0], rows[0].get("sCode")
                if (code in REJECTED_ORDER_CODES and row.get("clOrdId") == values["clOrdId"]
                        and row.get("ordId") in {None, ""}):
                    raise OrderRejected(code)
        if method == "POST" and path == "/api/v5/trade/cancel-order" and payload.get("code") != "0":
            # R2.1.8 (R217-A2): one row for our client ID or with an empty one (OKX need not echo it on a failure), or a
            # top-level code with no rows; nothing to cancel by the listed codes, any other code journalled as it is
            rows, top = payload["data"], str(payload.get("code"))
            row = rows[0] if len(rows) == 1 and isinstance(rows[0], dict) else {}
            code = (row.get("sCode") if row.get("clOrdId") in {values["clOrdId"], "", None} else None) if rows else top
            if code in NOTHING_TO_CANCEL_CODES:
                raise CancelRefused(code, top)
            if isinstance(code, str) and re.fullmatch(r"[0-9]{1,8}", code) and re.fullmatch(r"[0-9]{1,8}", top):
                raise CancelFailed(code, top)
        if method == "GET" and path == "/api/v5/trade/order" and payload.get("code") == "51603" and not payload["data"]:
            raise OrderAbsent()
        if method == "POST" and path == "/api/v5/trade/order-algo" and payload.get("code") in {"0", "1"}:
            rows = payload["data"]
            # Narrow allowlist of explicit parameter/instrument/funds rejection.
            # In particular 50004 is UNKNOWN, never proof of non-acceptance.
            if len(rows) == 1 and isinstance(rows[0], dict):
                row = rows[0]
                # R2.1.15 (QR-01, D187): 51278 / 51280, the stop's trigger already beyond the last price (OKX Error codes);
                # RC2 (PP-01): our client ID or none echoed back, as for a cancel (R217-A2)
                if (row.get("sCode") in STOP_REFUSALS
                        and row.get("algoClOrdId") in {values["algoClOrdId"], "", None} and row.get("algoId") in {None, ""}):
                    raise ProtectionRejected(row["sCode"])
        if (method == "POST" and path == "/api/v5/trade/order-algo" and payload.get("code") in STOP_REFUSALS
                and payload["data"] == []):
            raise ProtectionRejected(payload["code"])  # RC2 (PP-01): the refusal as a top-level code with no rows
        if payload.get("code") != "0":
            raise venue_error("okx_error_" + okx_code(payload), okx_code=okx_code(payload, None))
        return payload["data"]

    def arm(self, budget, *, close_only=False):
        budget = D(str(budget), positive=True)
        if not Decimal(25) <= budget <= Decimal(500):
            raise PreflightError("invalid_execution_budget")
        if type(self.leverage) is not int or not 1 <= self.leverage <= 10:
            raise PreflightError("invalid_execution_leverage")
        self.entry_limit = budget * Decimal(".3") * self.leverage  # R2.1.15: margin <= 30 % of the budget
        self.close_only = close_only
        self.writes = True

    def validate_write(self, path, body):
        if path == "/api/v5/trade/cancel-algos":
            if (not isinstance(body, list) or len(body) != 1 or set(body[0]) != {"instId", "algoId"}
                    or body[0]["instId"] != self.inst_id or body[0]["algoId"] not in self.owned_algos):
                raise PreflightError("unowned_algo_cancel")
            return
        if not isinstance(body, dict) or body.get("instId") != self.inst_id:
            raise PreflightError("exchange_write_instrument")
        if path == "/api/v5/trade/order-algo":
            from .ops_policy import owned_stop_intent
            if self.close_only or not re.fullmatch(r"J8[0-9a-f]{16}[0-5]S[0-9]{4}", str(body.get("algoClOrdId", ""))):
                raise PreflightError("protective_identity_or_recovery")
            keys = {"instId", "tdMode", "posSide", "side", "ordType", "sz", "reduceOnly", "algoClOrdId", "slTriggerPx", "slTriggerPxType", "slOrdPx", "cxlOnClosePos"}
            if (set(body) != keys or body.get("reduceOnly") is not True or body.get("cxlOnClosePos") is not True
                    or body.get("tdMode") != "isolated" or body.get("posSide") != "net" or body.get("ordType") != "conditional"
                    or body.get("slOrdPx") != "-1" or body.get("slTriggerPxType") != "last"
                    or body.get("side") not in {"buy", "sell"} or not 0 < D(body["sz"]) <= 30 or D(body["slTriggerPx"]) <= 0):
                raise PreflightError("protective_shape")
            return
        if path == "/api/v5/trade/amend-algos":
            keys = {"instId", "algoId", "newSz", "newSlTriggerPx", "newSlOrdPx", "cxlOnFail", "reqId"}
            if (set(body) != keys or body.get("algoId") not in self.owned_algos or body.get("cxlOnFail") is not False
                    or body.get("newSlOrdPx") != "-1" or not 0 < D(body.get("newSz")) <= 30
                    or D(body.get("newSlTriggerPx")) <= 0 or not AMEND_ID.fullmatch(str(body.get("reqId", "")))):
                raise PreflightError("unowned_or_invalid_algo_amend")
            return
        if path == "/api/v5/account/set-leverage":
            if body != {"instId": self.inst_id, "lever": str(self.leverage), "mgnMode": "isolated"}:
                raise PreflightError("exchange_leverage_must_be_one" if self.leverage == 1 else "exchange_leverage_not_the_bots")
            return
        identity = body.get("clOrdId")
        if not isinstance(identity, str) or not CLIENT_ID.fullmatch(identity):
            raise PreflightError("exchange_order_identity")
        if path == "/api/v5/trade/cancel-order":
            if set(body) != {"instId", "clOrdId"}:
                raise PreflightError("exchange_cancel_shape")
            return
        base = {"instId", "tdMode", "posSide", "side", "ordType", "sz", "reduceOnly", "clOrdId", "stpMode"}
        reduce = body.get("reduceOnly") is True
        if (set(body) != (base if reduce else base | {"px"}) or type(body.get("reduceOnly")) is not bool
                or body.get("tdMode") != "isolated" or body.get("posSide") != "net"
                or body.get("side") not in {"buy", "sell"} or body.get("stpMode") != "cancel_taker"
                or body.get("ordType") not in ({"market"} if reduce else {"ioc", "post_only"})):
            raise PreflightError("exchange_order_shape")
        units = D(body["sz"], positive=True)
        # Closing may combine three owned entries; never apply the per-entry cap to an exit.
        if units > (30 if reduce else 10) or (reduce and identity[-5] != "C") or (not reduce and identity[-5] != "E"):
            raise PreflightError("exchange_order_size_or_kind")
        if not reduce and self.max_entry_units is not None and units > self.max_entry_units:
            raise PreflightError("exchange_test_live_lot_cap")
        if not reduce:
            if self.close_only:
                raise PreflightError("recovery_is_close_only")
            if not hasattr(self, "instrument"):
                raise PreflightError("exchange_instrument_unchecked")
            base_btc = units * D(self.instrument["contract_value"]) * D(self.instrument["contract_multiplier"])
            if base_btc * D(body["px"], positive=True) > self.entry_limit:
                raise PreflightError("exchange_entry_notional_limit")

    def identity(self, require_subaccount):
        row = one_row(self.get("/api/v5/account/config", private=True))
        summary = config_summary([row])
        if "trade" not in summary["granted_permissions"]:
            raise PreflightError("trade_permission_missing")
        if summary["account_level"] != "2" or summary["position_mode"] != "net_mode":
            raise PreflightError("set_futures_and_net_mode_in_okx")
        tag = account_tag(self.site, self.environment, row.get("uid"))
        main = account_tag(self.site, self.environment, row.get("mainUid"))
        if require_subaccount and tag == main:
            raise PreflightError("live_bot_requires_dedicated_subaccount")
        return {"account_tag": tag, "main_tag": main, "is_subaccount": tag != main,
                "unused_permissions": sorted(set(summary["granted_permissions"]) - {"read_only", "trade"})}

    def balance(self):
        row = one_row(self.get("/api/v5/account/balance", {"ccy": "USDT"}, private=True))
        if not isinstance(row.get("details"), list):
            raise PreflightError("invalid_balance_response")
        detail = one_row([r for r in row["details"] if isinstance(r, dict) and r.get("ccy") == "USDT"])
        return {"equity": N(detail.get("eq")), "available": N(detail.get("availBal")), "cash": N(detail.get("cashBal"))}

    def position(self):
        rows = self.get("/api/v5/account/positions", private=True)
        active, facts = [], None
        for row in rows:
            if not isinstance(row, dict):
                raise PreflightError("invalid_positions")
            units = D(row.get("pos"))
            if not units:
                continue
            if (row.get("instId") != self.inst_id or row.get("mgnMode") != "isolated"
                    or row.get("posSide") != "net" or D(row.get("lever")) != self.leverage):
                raise PreflightError("foreign_or_unsupported_position")
            active.append(units)
            facts = {k: row[k] for k in POSITION_FACTS if isinstance(row.get(k), str)}  # R2.1.15: raw strings, as OKX sent them
        if len(active) > 1:
            raise PreflightError("multiple_positions")
        self.last_position = facts
        return active[0] if active else Decimal(0)

    def pending(self, allowed_ids=(), allowed_algo_ids=(), allowed_child_ids=(), owned_orders=()):
        rows = self.get("/api/v5/trade/orders-pending", {"limit": "100"}, private=True)
        if len(rows) >= 100 or any(not isinstance(r, dict) or r.get("instId") != self.inst_id
                                   or not (r.get("clOrdId") in allowed_ids or r.get("ordId") in allowed_child_ids) for r in rows):
            raise PreflightError("foreign_or_unbounded_pending_orders")
        expected = {o["request"]["clOrdId"]: o for o in owned_orders if not o.get("_protective_parent")}
        for row in rows:
            order = expected.get(row.get("clOrdId"))
            if order:
                body, result = order["request"], order.get("result") or {}
                if result.get("ordId") and row.get("ordId") != result["ordId"]:
                    raise PreflightError("pending_order_server_identity_mismatch")
                if any(row.get(k) != body[k] for k in ("instId", "side", "posSide", "tdMode", "ordType")) or D(row.get("sz")) != D(body["sz"]) or str(row.get("reduceOnly")).lower() != str(body["reduceOnly"]).lower():
                    raise PreflightError("pending_order_metadata_mismatch")
        for kind in ALGOS:
            algos = self.get("/api/v5/trade/orders-algo-pending", {"ordType": kind, "limit": "100"}, private=True)
            if len(algos) >= 100 or any(not isinstance(a, dict) or a.get("instId") != self.inst_id
                    or a.get("algoId") not in allowed_algo_ids for a in algos):
                raise PreflightError("foreign_algo_order")
            if any(a.get("algoClOrdId") != self.owned_algos.get(a.get("algoId")) for a in algos):
                raise PreflightError("pending_algo_parent_identity_mismatch")
        return rows

    def preflight(self, budget=25, require_subaccount=False):
        self.calibrate_clock()
        identity = self.identity(require_subaccount)
        self.instrument = instrument_summary(self.get("/api/v5/account/instruments",
            {"instType": "SWAP", "instId": self.inst_id}, private=True), "swap", self.inst_id)
        public = instrument_summary(self.get("/api/v5/public/instruments",
            {"instType": "SWAP", "instId": self.inst_id}), "swap", self.inst_id)
        for key in ("contract_value", "contract_multiplier", "lot_step", "minimum_order_units", "tick_size"):
            if D(public[key]) != D(self.instrument[key]):
                raise PreflightError("public_account_contract_mismatch")
        fees = fee_summary(self.get("/api/v5/account/trade-fee", {"instType": "SWAP",
            "groupId": self.instrument["fee_group"]}, private=True), self.instrument)
        rate = D(fees["taker_signed_rate"])
        if abs(rate) > Decimal(".001"):
            raise PreflightError("fee_exceeds_experiment_bound")
        if self.position():
            raise PreflightError("account_must_start_flat")
        self.pending()
        balance = self.balance()
        if D(balance["available"]) < D(str(budget)) * Decimal("1.01"):
            raise PreflightError("insufficient_available_usdt")
        return {**identity, "balance": balance, "instrument": self.instrument, "taker_signed_rate": N(rate),
                "server_start_ms": int(self.fresh_server_now() * 1000)}

    def calibrate_clock(self):
        result = super().calibrate_clock()
        if len(self.calibration_rtts) < 2000:  # bounded; step_timing empties it about every hour
            self.calibration_rtts.append(result["round_trip_ms"])
        return result

    def position_tiers(self):
        """R2.1.15 (D181 p. 2): OKX's public isolated tiers of this contract's family, for the liquidation price the engine
        computes when a position read gives no liqPx. Any other answer refuses the Start."""
        family = self.inst_id.rsplit("-", 1)[0]
        rows = self.get("/api/v5/public/position-tiers", {"instType": "SWAP", "tdMode": "isolated", "instFamily": family})
        tiers = []
        for row in rows:
            if not isinstance(row, dict) or row.get("instFamily", family) != family:
                raise PreflightError("invalid_position_tiers")
            tiers.append({"tier": str(row.get("tier", "")), "min_contracts": N(D(row.get("minSz"))),
                          "max_contracts": N(D(row.get("maxSz"), positive=True)), "mmr": N(D(row.get("mmr"), positive=True)),
                          "max_leverage": N(D(row.get("maxLever"), positive=True))})
        if not tiers or not any(D(t["min_contracts"]) == 0 for t in tiers) or any(D(t["mmr"]) >= Decimal(".5") for t in tiers):
            raise PreflightError("invalid_position_tiers")
        return tiers

    def set_leverage(self):
        """Set the bot's leverage (R2.1.15; 1x before), isolated, and confirm it with GET leverage-info; the confirmed
        facts (R2.1.10: leverage_confirmed)."""
        lever = str(self.leverage)
        self.request("POST", "/api/v5/account/set-leverage", {"instId": self.inst_id, "lever": lever, "mgnMode": "isolated"})
        rows = self.get("/api/v5/account/leverage-info", {"instId": self.inst_id, "mgnMode": "isolated"}, private=True)
        if not rows or any(not isinstance(r, dict) or r.get("instId") != self.inst_id
                           or r.get("mgnMode") != "isolated" or D(r.get("lever")) != self.leverage for r in rows):
            raise PreflightError("one_x_leverage_unconfirmed" if self.leverage == 1 else "leverage_unconfirmed")
        return {"lever": lever, "mgn_mode": "isolated", "rows": len(rows)}

    def order(self, body):
        row = one_row(self.request("POST", "/api/v5/trade/order", body))
        if row.get("sCode") != "0" or row.get("clOrdId") != body["clOrdId"]:
            raise PreflightError("order_ack_unconfirmed")
        return {"ordId": row.get("ordId"), "clOrdId": body["clOrdId"]}

    def cancel(self, identity):
        self.request("POST", "/api/v5/trade/cancel-order", {"instId": self.inst_id, "clOrdId": identity})

    def order_result(self, body):
        row = one_row(self.get("/api/v5/trade/order", {"instId": self.inst_id, "clOrdId": body["clOrdId"]}, private=True))
        if any(row.get(k) != body[k] for k in ("instId", "clOrdId", "side", "posSide", "tdMode", "ordType")):
            raise PreflightError("order_result_identity_mismatch")
        if D(row.get("sz")) != D(body["sz"]) or str(row.get("reduceOnly")).lower() != str(body["reduceOnly"]).lower():
            raise PreflightError("order_result_quantity_or_reduce_only_mismatch")
        return self.parse_order_result(row, body)

    def parse_order_result(self, row, body):
        units = D(row.get("accFillSz"))
        if not 0 <= units <= D(body["sz"]) or row.get("state") not in TERMINAL | {"live", "partially_filled"}:
            raise PreflightError("invalid_order_result")
        fee = D(row.get("fee")) if units else Decimal(0)
        if units and row.get("feeCcy") != "USDT":
            raise PreflightError("unaccounted_fee_currency")
        rebate = D(row.get("rebate") or "0")
        # Futures order fee already includes rebates; separate `rebate` is Spot/Margin only.
        if rebate:
            raise PreflightError("unexpected_separate_swap_rebate")
        price = D(row.get("avgPx"), positive=True) if units else Decimal(0)
        pnl = D(row.get("pnl")) if units else Decimal(0)
        return {"ordId": row.get("ordId"), "clOrdId": body["clOrdId"], "state": row["state"],
                "contracts": N(units), "average_price": N(price), "signed_fee_usdt": N(fee),
                "gross_pnl_usdt": N(pnl), "updated_ms": N(D(row.get("uTime"), positive=True))}

    def protective_details(self, intent, known_algo_id=None, alternate=None):
        from .ops_policy import validate_stop_receipt
        row = one_row(self.get("/api/v5/trade/order-algo", {"algoClOrdId": intent.client_id}, private=True))
        known = known_algo_id or next((key for key, value in self.owned_algos.items() if value == intent.client_id), None)
        try:
            proof = validate_stop_receipt(intent, row, known_algo_id=known)
        except ValueError:
            try:
                if alternate is None or alternate.client_id != intent.client_id:
                    raise ValueError()
                proof = validate_stop_receipt(alternate, row, known_algo_id=known)
            except ValueError:
                raise PreflightError("protective_receipt_mismatch") from None
        self.owned_algos[proof["algo_id"]] = intent.client_id
        return row, proof

    def place_protection(self, intent):
        """The algoId of a positive ACK (RC2, PP-03): ours from now on, so that the pending check knows our stop before its
        first GET receipt."""
        row = one_row(self.request("POST", "/api/v5/trade/order-algo", intent.body))
        if row.get("sCode") != "0" or row.get("algoClOrdId") != intent.client_id:
            raise PreflightError("protective_ack_uncertain")
        algo_id = row.get("algoId")
        if isinstance(algo_id, str) and re.fullmatch(r"[0-9]{1,40}", algo_id):
            self.owned_algos[algo_id] = intent.client_id
            return algo_id
        return None

    def cancel_protection(self, algo_id):
        row = one_row(self.request("POST", "/api/v5/trade/cancel-algos", [{"instId": self.inst_id, "algoId": algo_id}]))
        if row.get("sCode") != "0" or row.get("algoId") != algo_id:
            raise PreflightError("protective_cancel_uncertain")

    def amend_protection(self, algo_id, desired, identity):
        body = {"instId": self.inst_id, "algoId": algo_id, "newSz": desired.body["sz"],
                "newSlTriggerPx": desired.body["slTriggerPx"], "newSlOrdPx": "-1", "cxlOnFail": False, "reqId": identity}
        row = one_row(self.request("POST", "/api/v5/trade/amend-algos", body))
        if row.get("sCode") != "0" or row.get("algoId") != algo_id or row.get("reqId") != identity:
            raise PreflightError("protective_amend_uncertain")

    def protective_child(self, intent, parent, identity, owned, previous="0"):
        from .ops_policy import adopt_stop_child
        row = one_row(self.get("/api/v5/trade/order", {"instId": self.inst_id, "ordId": identity}, private=True))
        if row.get("ordId") != identity:
            raise PreflightError("protective_child_identity_mismatch")
        try:
            adopt_stop_child(intent, parent, row, current_owned_contracts=owned, previous_acc_fill=previous)
        except ValueError:
            raise PreflightError("protective_child_mismatch") from None
        body = {"instId": self.inst_id, "tdMode": "isolated", "posSide": "net", "side": intent.body["side"],
                "ordType": "market", "sz": row["sz"], "reduceOnly": True, "clOrdId": row.get("clOrdId", "")}
        return body, self.parse_order_result(row, body)

    def bills(self, begin_ms):
        rows, after = [], None
        end_ms = str(int(self.fresh_server_now() * 1000))
        for _ in range(5):
            params = {"ccy": "USDT", "begin": str(begin_ms), "end": end_ms, "limit": "100"}
            if after:
                params["after"] = after
            page = self.get("/api/v5/account/bills", params, private=True)
            if any(not isinstance(r, dict) or not isinstance(r.get("billId"), str)
                   or not re.fullmatch(r"[0-9]{1,40}", r["billId"]) for r in page):
                raise PreflightError("invalid_bills")
            rows.extend(page)
            if len(page) < 100:
                if len({r["billId"] for r in rows}) != len(rows):
                    raise PreflightError("ambiguous_bills_pagination")
                return rows
            new_after = page[-1]["billId"]
            if new_after == after:
                break
            after = new_after
        raise PreflightError("bills_pagination_limit")


class ExchangeFeed:
    """Common public feed from the execution site's matching Demo/Live environment."""
    def __init__(self, api, clock=time.time):
        self.api, self.clock, self.instrument = api, clock, None
        self.calls = 0
        self.max_calls = 2000
        self.stage = "validation"

    def get(self, name):
        from .arena_market import paths
        routes = paths(self.api.inst_id)
        if name not in routes or self.calls >= self.max_calls:
            raise PreflightError("exchange_public_feed_limit")
        self.stage = name
        self.calls += 1
        route = urlsplit(routes[name])
        result = self.api.get(route.path, dict(parse_qsl(route.query)), private=False)
        if not result or (name != "candles" and len(result) != 1):
            raise PreflightError("ambiguous_public_market_data")
        return result

    def snapshot(self):
        from .arena_market import build_snapshot
        if self.instrument is None:
            self.instrument = self.get("instrument")
        raw = {"instrument": self.instrument, "candles": self.get("candles"),
               "funding": self.get("funding"), "ticker": self.get("ticker"), "clock": self.get("clock")}
        self.stage = "validation"
        snapshot = build_snapshot(raw, self.clock(), allow_unknown_funding_schedule=self.api.environment == "demo",
                                  instrument=self.api.inst_id)
        snapshot["source"] = "OKX_DEMO" if self.api.environment == "demo" else "OKX_LIVE"
        return snapshot
