"""Bounded OKX GET-only diagnostics; contains no order or transfer sender."""

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlencode, urlsplit
import urllib.error
import urllib.request

from .jev import NoRedirect

SITES = {"global": "https://openapi.okx.com", "eea": "https://eea.okx.com",
         "us": "https://us.okx.com", "tr": "https://tr.okx.com"}
PRODUCTS = {"swap": ("SWAP", "BTC-USDT-SWAP"), "spot": ("SPOT", "BTC-USDT")}
PUBLIC_PATHS = {"/api/v5/public/time", "/api/v5/public/instruments", "/api/v5/market/ticker"}
PRIVATE_PATHS = {"/api/v5/account/config", "/api/v5/account/instruments",
                 "/api/v5/account/balance", "/api/v5/account/positions",
                 "/api/v5/account/trade-fee", "/api/v5/trade/orders-pending"}
MAX_REQUESTS = 9
MAX_RESPONSE_BYTES = 1048576


class PreflightError(ValueError):
    """Messages are local codes, never provider bodies or credential values."""


@dataclass(frozen=True)
class Credentials:
    key: str = field(repr=False)
    secret: str = field(repr=False)
    passphrase: str = field(repr=False)

    def __post_init__(self):
        for value in (self.key, self.secret, self.passphrase):
            if (not isinstance(value, str) or not value.strip() or len(value) > 512
                    or any(ord(c) < 32 or ord(c) > 126 for c in value)):
                raise PreflightError("missing_or_invalid_okx_credentials")


def load_settings(root):
    names = {"OKX_SITE", "OKX_ENV", "OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE"}
    values = {}
    path = Path(root) / ".env.okx.local"
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() in names:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[name.strip()] = value
    for name in names:
        if name in os.environ:
            values[name] = os.environ[name]
    site, environment = values.get("OKX_SITE", "global"), values.get("OKX_ENV", "live")
    if site not in SITES or environment not in {"live", "demo"}:
        raise PreflightError("invalid_okx_site_or_environment")
    return site, environment, values


def credentials_from(values):
    return Credentials(*(values.get(name, "") for name in
                         ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE")))


def decimal_value(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)) or len(str(value)) > 80:
        raise PreflightError("invalid_numeric_data")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        raise PreflightError("invalid_numeric_data") from None
    if (not result.is_finite() or (positive and result <= 0)
            or abs(result.as_tuple().exponent) > 30 or (result and not -30 <= result.adjusted() <= 30)):
        raise PreflightError("invalid_numeric_data")
    return result


def numeric_text(value):
    return format(decimal_value(value), "f")


def signature(secret, timestamp, request_path):
    message = (timestamp + "GET" + request_path).encode("utf-8")
    return base64.b64encode(hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()).decode("ascii")


def send_get(request):
    """No retries, redirects, POSTs, arbitrary hosts or response body logging."""
    if request.get_method() != "GET" or request.data is not None:
        raise PreflightError("write_request_forbidden")
    parsed = urlsplit(request.full_url)
    if (parsed.scheme != "https" or parsed.netloc not in {urlsplit(site).netloc for site in SITES.values()}
            or parsed.fragment or parsed.path not in PUBLIC_PATHS | PRIVATE_PATHS):
        raise PreflightError("request_destination_not_allowed")
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=10) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        error.close()
        raise PreflightError("http_" + str(error.code)) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        raise PreflightError("network_error") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise PreflightError("response_too_large")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise PreflightError("invalid_json_response") from None


def one_row(rows):
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise PreflightError("unexpected_response_shape")
    return rows[0]


class OkxReader:
    def __init__(self, site="global", environment="live", credentials=None,
                 transport=send_get, clock=time.time, monotonic=time.monotonic):
        if site not in SITES or environment not in {"live", "demo"}:
            raise PreflightError("invalid_okx_site_or_environment")
        self.base = SITES[site]
        self.site, self.environment, self.credentials = site, environment, credentials
        self.transport, self.clock, self.monotonic = transport, clock, monotonic
        self.requests = 0
        self.server_anchor = self.monotonic_anchor = None

    def server_now(self):
        if self.server_anchor is None or self.monotonic_anchor is None:
            raise PreflightError("server_clock_not_calibrated")
        elapsed = self.monotonic() - self.monotonic_anchor
        if not 0 <= elapsed <= 30:
            raise PreflightError("server_clock_calibration_expired")
        return self.server_anchor + elapsed

    def get(self, path, params=None, *, private=False):
        allowed = PRIVATE_PATHS if private else PUBLIC_PATHS
        if path not in allowed or "?" in path or "#" in path:
            raise PreflightError("endpoint_not_allowed")
        if self.requests >= MAX_REQUESTS:
            raise PreflightError("request_limit_reached")
        request_path = path + (("?" + urlencode(params)) if params else "")
        headers = {"Accept": "application/json", "User-Agent": "TradingResearch/0.2 (read-only)",
                   "Cache-Control": "no-cache"}
        if self.environment == "demo":
            headers["x-simulated-trading"] = "1"
        if private:
            if self.credentials is None:
                raise PreflightError("missing_okx_credentials")
            timestamp = datetime.fromtimestamp(self.server_now(), timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            headers.update({"OK-ACCESS-KEY": self.credentials.key,
                            "OK-ACCESS-PASSPHRASE": self.credentials.passphrase,
                            "OK-ACCESS-TIMESTAMP": timestamp,
                            "OK-ACCESS-SIGN": signature(self.credentials.secret, timestamp, request_path)})
        request = urllib.request.Request(self.base + request_path, method="GET", headers=headers)
        self.requests += 1
        payload = self.transport(request)
        if not isinstance(payload, dict):
            raise PreflightError("invalid_provider_envelope")
        if payload.get("code") != "0":
            code = payload.get("code")
            safe_code = code if isinstance(code, str) and re.fullmatch(r"[0-9]{1,8}", code) else "unknown"
            raise PreflightError("okx_error_" + safe_code)
        if not isinstance(payload.get("data"), list):
            raise PreflightError("invalid_provider_data")
        return payload["data"]

    def calibrate_clock(self):
        self.server_anchor = self.monotonic_anchor = None
        local_start, start = self.clock(), self.monotonic()
        row = one_row(self.get("/api/v5/public/time"))
        end, local_end = self.monotonic(), self.clock()
        rtt = end - start
        if not 0 <= rtt <= 5:
            raise PreflightError("unreliable_clock_sample")
        server = float(decimal_value(row.get("ts"), positive=True)) / 1000
        if not 0 < server < 253402300799:
            raise PreflightError("invalid_server_time")
        offset = server - ((local_start + local_end) / 2)
        self.server_anchor, self.monotonic_anchor = server + rtt / 2, end
        return {"server_minus_local_seconds": round(offset, 3), "round_trip_ms": round(rtt * 1000, 3),
                "local_clock_jump_seconds": round((local_end - local_start) - rtt, 3),
                "calibration_lifetime_seconds": 30, "windows_clock_modified": False}


def instrument_summary(rows, product, inst_id=None):
    inst_type, inst_id = PRODUCTS[product][0], inst_id or PRODUCTS[product][1]
    row = one_row(rows)
    if row.get("instId") != inst_id or row.get("instType") != inst_type or row.get("state") != "live":
        raise PreflightError("instrument_unavailable_or_mismatched")
    minimum, step = (decimal_value(row.get(k), positive=True) for k in ("minSz", "lotSz"))
    units = (minimum / step).to_integral_value(rounding=ROUND_CEILING) * step
    for name in ("tickSz",):
        decimal_value(row.get(name), positive=True)
    group = row.get("groupId", "")
    if not isinstance(group, str) or not re.fullmatch(r"[0-9]{1,8}", group):
        raise PreflightError("missing_instrument_fee_group")
    if product == "swap":
        if row.get("ctType") != "linear" or row.get("ctValCcy") != inst_id.split("-")[0] or row.get("settleCcy") != "USDT":
            raise PreflightError("unsupported_contract_specification")
        base_size = units * decimal_value(row.get("ctVal"), positive=True) * decimal_value(row.get("ctMult"), positive=True)
    else:
        if row.get("baseCcy") != "BTC" or row.get("quoteCcy") != "USDT":
            raise PreflightError("unsupported_spot_pair")
        base_size = units
    result = {"instrument": inst_id, "instrument_type": inst_type, "state": "live", "fee_group": group,
              "minimum_order_units": format(units, "f"), "size_unit": "contracts" if product == "swap" else "BTC",
              "lot_step": numeric_text(row["lotSz"]), "tick_size": numeric_text(row["tickSz"]),
              "minimum_base_btc": format(base_size, "f")}
    if product == "swap":
        result.update(contract_value=numeric_text(row["ctVal"]), contract_multiplier=numeric_text(row["ctMult"]))
    return result


def quote_summary(rows, reader, product, instrument):
    row = one_row(rows)
    if (row.get("instId"), row.get("instType")) != (PRODUCTS[product][1], PRODUCTS[product][0]):
        raise PreflightError("ticker_instrument_mismatch")
    bid, ask = (decimal_value(row.get(k), positive=True) for k in ("bidPx", "askPx"))
    if bid > ask:
        raise PreflightError("crossed_quote")
    timestamp = decimal_value(row.get("ts"), positive=True)
    age = reader.server_now() - float(timestamp) / 1000
    if not -2 <= age <= 10:
        raise PreflightError("stale_or_future_quote")
    minimum_cost = decimal_value(instrument["minimum_base_btc"], positive=True) * ask
    return {"bid_usdt": format(bid, "f"), "ask_usdt": format(ask, "f"),
            "age_seconds_using_server_clock": round(age, 3),
            "spread_bps": format((ask - bid) / ((ask + bid) / 2) * 10000, ".6f"),
            "reference_minimum_notional_usdt": format(minimum_cost, "f"),
            "minimum_notional_is_estimate_not_order": True}


def config_summary(rows):
    row = one_row(rows)
    permissions = row.get("perm")
    granted = {p.strip() for p in permissions.split(",")} if isinstance(permissions, str) else set()
    if "read_only" not in granted or granted - {"read_only", "trade", "withdraw"}:
        raise PreflightError("read_permission_missing_or_unknown_scopes")
    if row.get("acctLv") not in {"1", "2", "3", "4"} or row.get("posMode") not in {"net_mode", "long_short_mode"}:
        raise PreflightError("unknown_account_configuration")
    # Never retain apikey, uid, account label, names, email, or bound IP addresses.
    return {"permission_used": "read_only", "granted_permissions": sorted(granted),
            "unused_permissions": sorted(granted - {"read_only"}),
            "account_level": row["acctLv"], "position_mode": row["posMode"],
            "spot_auto_borrow_enabled": row.get("enableSpotBorrow") is True,
            "ip_binding_configured": bool(row.get("ip"))}


def fee_summary(rows, instrument):
    row = one_row(rows)
    groups = row.get("feeGroup")
    if row.get("instType") != instrument["instrument_type"] or not isinstance(groups, list):
        raise PreflightError("fee_group_data_missing")
    group = one_row([item for item in groups if isinstance(item, dict) and item.get("groupId") == instrument["fee_group"]])
    return {"group_id": instrument["fee_group"], "maker_signed_rate": numeric_text(group.get("maker")),
            "taker_signed_rate": numeric_text(group.get("taker")),
            "sign_convention": "negative=commission; positive=rebate; zero-fee promotions need UI check"}


def account_summary(reader, product, instrument):
    inst_type, inst_id = PRODUCTS[product]
    summary = config_summary(reader.get("/api/v5/account/config", private=True))
    summary["checks"] = {}
    summary["instrument_listed_for_account"] = False

    def run_check(name, operation):
        # Preserve successful independent observations when one product endpoint is unavailable.
        try:
            operation()
            summary["checks"][name] = {"status": "passed"}
        except PreflightError as error:
            summary["checks"][name] = {"status": "unavailable", "reason": str(error)}
        except (ValueError, TypeError, KeyError, ArithmeticError, OSError):
            summary["checks"][name] = {"status": "unavailable", "reason": "invalid_data_or_local_error"}

    def check_instrument():
        rows = reader.get("/api/v5/account/instruments", {"instType": inst_type, "instId": inst_id}, private=True)
        if not rows:
            raise PreflightError("instrument_not_listed_for_account")
        account_inst = instrument_summary(rows, product)
        if account_inst != instrument:
            raise PreflightError("account_instrument_differs_from_public")
        summary["instrument_listed_for_account"] = True

    def check_fees():
        rows = reader.get("/api/v5/account/trade-fee", {"instType": inst_type, "groupId": instrument["fee_group"]}, private=True)
        summary["fees"] = fee_summary(rows, instrument)

    def check_balance():
        balance = one_row(reader.get("/api/v5/account/balance", {"ccy": "BTC,USDT"}, private=True))
        if not isinstance(balance.get("details"), list):
            raise PreflightError("invalid_balance_response")
        summary["balances"] = [{"currency": item["ccy"], "cash_balance": numeric_text(item.get("cashBal")),
                                "available_balance": numeric_text(item["availBal"]) if item.get("availBal") not in {None, ""} else None}
                               for item in balance["details"] if isinstance(item, dict) and item.get("ccy") in {"BTC", "USDT"}]

    def check_positions():
        positions = reader.get("/api/v5/account/positions", {"instId": inst_id}, private=True)
        if any(not isinstance(row, dict) or row.get("instId") != inst_id for row in positions):
            raise PreflightError("invalid_position_response")
        summary["nonzero_position_rows"] = sum(decimal_value(row.get("pos")) != 0 for row in positions)

    def check_orders():
        orders = reader.get("/api/v5/trade/orders-pending", {"instType": inst_type, "instId": inst_id, "limit": "100"}, private=True)
        if any(not isinstance(row, dict) or row.get("instId") != inst_id for row in orders):
            raise PreflightError("invalid_order_response")
        summary["pending_regular_orders_first_page"] = len(orders)
        summary["orders_may_be_paginated"] = len(orders) == 100

    for name, operation in (("instrument", check_instrument), ("fees", check_fees),
                            ("balance", check_balance), ("positions", check_positions), ("orders", check_orders)):
        run_check(name, operation)
    summary["account_mode_setup_needed_for_swap"] = product == "swap" and summary["account_level"] == "1"
    summary["algo_orders_checked"] = False
    summary["legal_or_trading_eligibility_verified"] = False
    return summary


def preflight(reader, product="swap", with_account=False):
    if product not in PRODUCTS:
        raise PreflightError("unsupported_product")
    report = {"venue": "OKX", "site": reader.site, "environment": reader.environment,
              "product": product, "orders_submitted": 0, "live_execution_enabled": False,
              "manual_execution_required": True, "account_requested": with_account,
              "account_connected": False, "jev_calls": 0}
    stage = "clock"
    try:
        report["clock"] = reader.calibrate_clock()
        stage = "public_instrument"
        inst_type, inst_id = PRODUCTS[product]
        report["instrument"] = instrument_summary(reader.get("/api/v5/public/instruments",
                                                              {"instType": inst_type, "instId": inst_id}), product)
        stage = "public_quote"
        report["quote"] = quote_summary(reader.get("/api/v5/market/ticker", {"instId": inst_id}), reader,
                                          product, report["instrument"])
        report["public_data_verified"] = True
        if with_account:
            stage = "account"
            report["account"] = account_summary(reader, product, report["instrument"])
            report["account_connected"] = True
        incomplete = with_account and any(check["status"] != "passed" for check in report["account"]["checks"].values())
        report["status"] = "account_checks_incomplete" if incomplete else ("read_only_checks_passed" if with_account else "public_checks_passed")
    except PreflightError as error:
        report.update(status="blocked", blocked_stage=stage, reason=str(error))
    except (ValueError, TypeError, KeyError, ArithmeticError, OSError):
        report.update(status="blocked", blocked_stage=stage, reason="invalid_data_or_local_error")
    report["requests_attempted"] = reader.requests
    return report
