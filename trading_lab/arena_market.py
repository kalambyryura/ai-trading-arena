"""Public OKX market feed for PAPER only. Fixed GET URLs, no authentication path."""
import hashlib
import json
import math
import time
from urllib.parse import urlencode
import urllib.request

from .arena_strategies import ArenaError

BASE = "https://openapi.okx.com"
def paths(instrument="BTC-USDT-SWAP"):
    return {
        "clock": "/api/v5/public/time",
        "ticker": "/api/v5/market/ticker?instId=" + instrument,
        "candles": "/api/v5/market/candles?instId=" + instrument + "&bar=1m&limit=31",
        "funding": "/api/v5/public/funding-rate?instId=" + instrument,
        "instrument": "/api/v5/public/instruments?instType=SWAP&instId=" + instrument,
    }


PATHS = paths()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def numeric(value, positive=False):
    if isinstance(value, bool):
        raise ArenaError("Нечислові ринкові дані.")
    try:
        result = float(value)
    except (ValueError, TypeError):
        raise ArenaError("Нечислові ринкові дані.") from None
    if not math.isfinite(result) or (positive and result <= 0):
        raise ArenaError("Некоректні ринкові дані.")
    return result


_CANDLES = {}  # completed rows -> (features, their open stamps); a pure function of those rows, checked against the clock
_RICHER = {}
_CACHE_SIZE = 8


def _text_rows(rows):
    """The rows as a hashable key when every value is a string (the OKX wire format); None otherwise (no caching)."""
    if not all(isinstance(row, list) and all(type(value) is str for value in row) for row in rows):
        return None
    return tuple(tuple(row) for row in rows)


def candle_features(rows, server_time):
    """V1 features of the completed candles. They change once a minute while snapshots come every 5 s, so the values are
    kept per exact set of completed rows (strings only). A cached call walks the rows in the same order as the full
    computation, with the same shape check, the same future-candle check at the same row and the same staleness check,
    so every value and every error is the one the full computation gives."""
    if not isinstance(rows, list):
        raise ArenaError("Немає завершених свічок.")
    completed = [row for row in rows if isinstance(row, list) and len(row) == 9 and row[8] == "1"]
    key = _text_rows(completed)
    hit = _CANDLES.get(key) if key is not None else None
    if hit is not None:
        features, stamps = hit
        k = 0
        for row in rows:
            if not isinstance(row, list) or len(row) != 9 or row[8] not in {"0", "1"}:
                raise ArenaError("Некоректна форма свічок.")
            if row[8] == "0":
                continue
            if stamps[k] / 1000 + 60 > server_time + .5:
                raise ArenaError("Нечинна або майбутня свічка.")
            k += 1
        if not -.5 <= server_time - (max(stamps) / 1000 + 60) <= 90:
            raise ArenaError("Застарілі завершені свічки.")
        return dict(features)
    features = _candle_features(rows, server_time)
    if key is not None:
        if len(_CANDLES) >= _CACHE_SIZE:
            _CANDLES.clear()
        _CANDLES[key] = (dict(features), tuple(numeric(row[0]) for row in completed))
    return features


def _candle_features(rows, server_time):
    parsed = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 9 or row[8] not in {"0", "1"}:
            raise ArenaError("Некоректна форма свічок.")
        if row[8] == "0":
            continue
        stamp, opening, high, low, close, volume = [numeric(v) for v in row[:6]]
        if (stamp % 60000 or stamp / 1000 + 60 > server_time + .5
                or min(opening, high, low, close) <= 0 or volume < 0
                or low > min(opening, close) or high < max(opening, close) or low > high):
            raise ArenaError("Нечинна або майбутня свічка.")
        parsed.append((stamp, opening, high, low, close, volume))
    parsed.sort()
    if len(parsed) < 25 or len({c[0] for c in parsed}) != len(parsed):
        raise ArenaError("Недостатньо унікальних завершених свічок.")
    parsed = parsed[-30:]
    if any(b[0] - a[0] != 60000 for a, b in zip(parsed, parsed[1:])):
        raise ArenaError("Пропуск у хвилинних свічках.")
    if not -.5 <= server_time - (parsed[-1][0] / 1000 + 60) <= 90:
        raise ArenaError("Застарілі завершені свічки.")
    closes = [c[4] for c in parsed]
    changes = [b - a for a, b in zip(closes[-15:-1], closes[-14:])]
    gain, loss = sum(max(0, x) for x in changes), sum(max(0, -x) for x in changes)
    rsi = 50 if gain == loss == 0 else 100 if loss == 0 else 100 - 100 / (1 + gain / loss)
    high, low = max(c[2] for c in parsed[-20:]), min(c[3] for c in parsed[-20:])
    prior_volume = sum(c[5] for c in parsed[-10:-5])
    return {
        "sma_gap_bps": (sum(closes[-5:]) / 5 / (sum(closes[-20:]) / 20) - 1) * 10000,
        "return_5m_bps": (closes[-1] / closes[-6] - 1) * 10000,
        "return_1m_bps": (closes[-1] / closes[-2] - 1) * 10000,
        "rsi_14": rsi, "range_position_20": 50 if high == low else (closes[-1] - low) / (high - low) * 100,
        "volume_ratio_5": sum(c[5] for c in parsed[-5:]) / prior_volume if prior_volume else None,
    }


def build_snapshot(raw, received_at, *, allow_unknown_funding_schedule=False, allow_model_funding=False, instrument="BTC-USDT-SWAP"):
    from .causal_htf import build_with_optional_htf
    return build_with_optional_htf(raw, received_at, _build_m1_snapshot, instrument=instrument,
        allow_unknown_funding_schedule=allow_unknown_funding_schedule, allow_model_funding=allow_model_funding)


def _build_m1_snapshot(raw, received_at, *, allow_unknown_funding_schedule=False, allow_model_funding=False, instrument="BTC-USDT-SWAP"):
    """Validate market facts; only explicitly labelled replay models allow realized funding events.

    Demo accounting uses actual account bills. PAPER needs a valid schedule to
    model funding, and Live retains the strict default. Never invent a date/rate.
    """
    server_time = numeric(raw["clock"][0]["ts"], True) / 1000
    ticker, contract, funding = (raw[key][0] for key in ("ticker", "instrument", "funding"))
    if any(row.get("instId") != instrument for row in (ticker, contract, funding)):
        raise ArenaError("Інший інструмент у стрічці.")
    if contract.get("state") != "live" or contract.get("ctValCcy") != instrument.split("-")[0] or contract.get("settleCcy") != "USDT":
        raise ArenaError("Непідтримуваний контракт.")
    ct = numeric(contract.get("ctVal"), True) * numeric(contract.get("ctMult") or 1, True)
    lot_btc = numeric(contract.get("lotSz"), True) * ct
    min_btc = numeric(contract.get("minSz"), True) * ct
    bid, ask, stamp = (numeric(ticker.get(k), True) for k in ("bidPx", "askPx", "ts"))
    if bid > ask or not -2 <= server_time - stamp / 1000 <= 10:
        raise ArenaError("Застарілі або перехресні котирування.")
    model_funding = funding.get("source") == "MODEL:realized-funding-events-v2"
    if funding.get("source") == "MODEL:settled-rate-of-next-fundingTime":
        raise ArenaError("Модель funding v1 містить майбутню ставку; перебудуйте tape як v2.")
    funding_events = []
    if model_funding:
        if not allow_model_funding or funding.get("fundingRate") is not None or funding.get("fundingTime") is not None:
            raise ArenaError("Модельні funding events дозволені лише в позначеному replay.")
        events = funding.get("settlements")
        if not isinstance(events, list) or len(events) > 1:
            raise ArenaError("Неоднозначні модельні funding events.")
        for event in events:
            event_ms = numeric(event.get("fundingTime"), True)
            event_rate = numeric(event["realizedRate"]) if event.get("realizedRate") is not None else None
            if event_ms != int(event_ms) or event_ms / 1000 > server_time or (event_rate is not None and abs(event_rate) > .1):
                raise ArenaError("Майбутній або нечинний funding event.")
            funding_events.append({"funding_time_ms": int(event_ms), "rate": event_rate,
                                   "at": received_at + event_ms / 1000 - server_time})
        rate = next_funding = None
        funding_status = "unknown_forecast"
    else:
        funding_stamp = numeric(funding.get("ts"), True) / 1000
        next_funding = numeric(funding.get("fundingTime"), True) / 1000
        rate = numeric(funding.get("fundingRate"))
        if not -2 <= server_time - funding_stamp <= 90 or abs(rate) > .1:
            raise ArenaError("Невідома або застаріла ставка funding.")
        funding_status = "available"
        if next_funding < server_time - 10:
            if not allow_unknown_funding_schedule:
                raise ArenaError("Дата funding у минулому.")
            funding_status = "unknown_schedule"
            rate = next_funding = None
    features = candle_features(raw["candles"], server_time)
    completed = sorted((row for row in raw["candles"] if row[8] == "1"), key=lambda r: float(r[0]))[-30:]
    features.update(richer_features(completed))
    mid = (bid + ask) / 2
    features["spread_bps"] = (ask - bid) / mid * 10000
    features.update(context_features(int(float(completed[-1][0])) + 60000, server_time, rate, next_funding))
    snapshot = {"at": received_at, "server_time": server_time, "quote_ts": stamp,
                "bar_close_ms": int(float(completed[-1][0])) + 60000,
                "bid": bid, "ask": ask, "mid": mid,
                "bid_btc": max(0, numeric(ticker.get("bidSz"))) * ct,
                "ask_btc": max(0, numeric(ticker.get("askSz"))) * ct,
                "lot_btc": lot_btc, "min_btc": min_btc, "features": features,
                "funding_rate": rate, "funding_at": received_at + next_funding - server_time if next_funding is not None else None,
                "funding_time_ms": int(next_funding * 1000) if next_funding is not None else None,
                "funding_status": funding_status, "raw": raw}
    snapshot["funding_feature_provenance"] = {
        "status": funding_status, "available_at": None if model_funding else received_at,
        "source_timestamp_ms": None if model_funding else funding.get("ts"),
        "source": "history-without-asof-forecast" if model_funding else "OKX-public-funding-rate"}
    if model_funding:
        snapshot.update(funding_events=funding_events, funding_accounting="realized-events-v2")
    snapshot["sha256"] = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return snapshot


def context_features(bar_close_ms, server_time, funding_rate=None, funding_time=None):
    """Clock and funding context for V2 recipes; V1 features and formulas are untouched.

    utc_hour/utc_weekday describe the close of the latest completed bar (= the current UTC minute), so a
    replay of the same candles gives the same values. funding_rate_bps and minutes_to_funding are
    quote-scoped facts of the funding response; an unknown schedule stays None (UNKNOWN blocks entry).
    """
    stamp = time.gmtime(bar_close_ms / 1000)
    rate_known = isinstance(funding_rate, (int, float)) and not isinstance(funding_rate, bool) and math.isfinite(funding_rate)
    time_known = isinstance(funding_time, (int, float)) and not isinstance(funding_time, bool) and math.isfinite(funding_time)
    return {"utc_hour": stamp.tm_hour, "utc_weekday": stamp.tm_wday,
            "funding_rate_bps": funding_rate * 10000 if rate_known else None,
            "minutes_to_funding": max(0.0, (funding_time - server_time) / 60) if time_known else None}


def richer_features(rows):
    """V2 catalogue over the same validated completed candles; V1 formulas unchanged. Kept per exact rows, as above."""
    key = _text_rows(rows)
    if key is None:
        return _richer_features(rows)
    hit = _RICHER.get(key)
    if hit is None:
        if len(_RICHER) >= _CACHE_SIZE:
            _RICHER.clear()
        hit = _RICHER[key] = _richer_features(rows)
    return dict(hit)


def _richer_features(rows):
    """V2 catalogue over the same validated completed candles; V1 formulas unchanged."""
    candles = [[float(v) for v in r[:6]] for r in rows]
    closes = [r[4] for r in candles]
    previous = closes[:-1]
    tr = [max(b[2] - b[3], abs(b[2] - a), abs(b[3] - a)) for a, b in zip(previous, candles[1:])][-14:]
    last = candles[-1]; mean = sum(closes[-20:]) / 20
    variance = sum((v - mean) ** 2 for v in closes[-20:]) / 20
    def ema(period):
        value = closes[0]; alpha = 2 / (period + 1)
        for close in closes[1:]: value = alpha * close + (1 - alpha) * value
        return value
    volume_mean = sum(r[5] for r in candles[-6:-1]) / 5
    return {"atr_14_bps": sum(tr) / len(tr) / last[4] * 10000,
            "body_bps": (last[4] / last[1] - 1) * 10000,
            "upper_wick_bps": (last[2] - max(last[1], last[4])) / last[4] * 10000,
            "lower_wick_bps": (min(last[1], last[4]) - last[3]) / last[4] * 10000,
            "bollinger_z_20": (last[4] - mean) / math.sqrt(variance) if variance else 0,
            "ema_gap_bps": (ema(5) / ema(20) - 1) * 10000,
            "volume_ratio_1": last[5] / volume_mean if volume_mean else None}


class PublicFeed:
    """One shared feed per finite season, with an explicit GET limit."""
    def __init__(self):
        self.calls = 0
        self.max_calls = 5000
        self.opener = urllib.request.build_opener(NoRedirect())
        self.instrument = None

    def get(self, name):
        if name not in PATHS or self.calls >= self.max_calls:
            raise ArenaError("Досягнуто ліміту публічних запитів цього сезону.")
        self.calls += 1
        request = urllib.request.Request(BASE + PATHS[name], method="GET", headers={
            "Accept": "application/json", "User-Agent": "TradingResearch/0.2 (read-only)", "Cache-Control": "no-cache"})
        with self.opener.open(request, timeout=6) as response:
            data = response.read(524289)
        if len(data) > 524288:
            raise ArenaError("Завелика відповідь ринку.")
        data = json.loads(data)
        if not isinstance(data, dict) or data.get("code") != "0" or not isinstance(data.get("data"), list) or not data["data"]:
            raise ArenaError("Недоступні публічні ринкові дані.")
        if name != "candles" and len(data["data"]) != 1:
            raise ArenaError("Неоднозначна відповідь ринку.")
        return data["data"]

    def get_public(self, path, params):
        """One public history GET of the schema-4 feature book (strategy_v4_feed.PATHS only); counted apart from the
        feed's own limit (the book keeps its own budget), so it never takes a quote from a V1/V2 participant; an empty
        page is a valid answer."""
        from .strategy_v4_feed import PATHS as BOOK_PATHS
        if path not in BOOK_PATHS:
            raise ArenaError("Дозволені лише публічні маршрути історії книги ознак.")
        self.book_calls = getattr(self, "book_calls", 0) + 1
        request = urllib.request.Request(BASE + path + "?" + urlencode(params), method="GET", headers={
            "Accept": "application/json", "User-Agent": "TradingResearch/0.2 (read-only)", "Cache-Control": "no-cache"})
        with self.opener.open(request, timeout=6) as response:
            data = response.read(524289)
        if len(data) > 524288:
            raise ArenaError("Завелика відповідь ринку.")
        data = json.loads(data)
        if not isinstance(data, dict) or data.get("code") != "0" or not isinstance(data.get("data"), list):
            raise ArenaError("Недоступні публічні ринкові дані.")
        return data["data"]

    def snapshot(self):
        if self.instrument is None:
            self.instrument = self.get("instrument")
        raw = {"instrument": self.instrument, "candles": self.get("candles"),
               "funding": self.get("funding"), "ticker": self.get("ticker"), "clock": self.get("clock")}
        return build_snapshot(raw, time.time())
