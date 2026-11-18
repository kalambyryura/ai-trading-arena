"""One unauthenticated OKX ticker read. No orders, account access or Jev calls."""

import json
import time
import urllib.error
import urllib.request

from .jev import NoRedirect
from .risk import finite_number

# Official Global REST domain, recommended in OKX's 2026-05-20 changelog.
# Regional account domains must be verified before any future authenticated adapter.
TICKER_URL = "https://openapi.okx.com/api/v5/market/ticker?instId=BTC-USDT-SWAP"


def parse_ticker(payload, received_at):
    if not isinstance(payload, dict) or payload.get("code") != "0":
        raise ValueError("provider_error")
    rows = payload.get("data")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError("invalid_ticker_shape")
    row = rows[0]
    if row.get("instId") != "BTC-USDT-SWAP" or row.get("instType") != "SWAP":
        raise ValueError("instrument_mismatch")
    try:
        values = [row[name] for name in ("bidPx", "askPx", "ts")]
        if any(isinstance(value, bool) for value in values):
            raise ValueError("invalid_numeric_ticker")
        bid, ask, timestamp_ms = map(float, values)
    except (KeyError, ValueError, TypeError):
        raise ValueError("invalid_numeric_ticker") from None
    if not all(finite_number(value) for value in (bid, ask, timestamp_ms, received_at)):
        raise ValueError("nonfinite_ticker")
    if bid <= 0 or ask <= 0 or bid > ask or timestamp_ms <= 0:
        raise ValueError("invalid_or_crossed_ticker")
    age = received_at - timestamp_ms / 1000
    freshness = "fresh" if -5 <= age <= 30 else "stale_or_clock_skew"
    return {"venue": "OKX", "instrument": row["instId"], "bid": bid, "ask": ask,
            "market_timestamp_ms": int(timestamp_ms), "received_at_unix": received_at,
            "age_seconds": round(age, 3), "freshness": freshness,
            "spread_bps": round((ask - bid) / ((ask + bid) / 2) * 10000, 6),
            "authenticated": False, "orders_submitted": 0,
            "eligible_for_trading": False,
            "note": "Public data connectivity only; no account eligibility or execution verified."}


def fetch_snapshot():
    started = time.perf_counter()
    request = urllib.request.Request(TICKER_URL, headers={"Accept": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=12) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError("response_too_large")
        result = parse_ticker(json.loads(raw), time.time())
    except urllib.error.HTTPError as error:
        return {"status": "failed", "reason": "http_" + str(error.code), "orders_submitted": 0}
    except (urllib.error.URLError, OSError, ValueError, TypeError):
        return {"status": "failed", "reason": "network_or_invalid_data", "orders_submitted": 0}
    return {"status": "received", "elapsed_ms": round((time.perf_counter() - started) * 1000, 3), **result}
