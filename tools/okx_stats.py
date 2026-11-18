"""Public OKX Trading Statistics (rubik) for perpetual swaps: probe history depth, fetch, split at the DEV cutoff, verify.

GET-only public endpoints of OKX's "Trading Statistics" REST group: no credentials, account or order route, no
redirects, at most 2 requests per second per endpoint plus the repository-wide public pace file, one retry after a
failure and never a retry loop. Every raw page is stored once, named by its sha256, under the Judge's vault.

Split rule: a row is a bar that opens at `ts`; it goes to the DEV store only when the bar has closed by the DEV end
2026-10-02T18:32:00Z (the close of the last DEV minute 18:31). Every later or straddling bar goes only to the vault,
so a 1D, 1W or 1M row that opened before the cutoff never leaks post-cutoff trading into DEV.

OKX keeps a rolling window (about 1440 rows per period), so the DEV side shrinks every day: fetch never overwrites
a folder that already has a MANIFEST.json unless --replace. Rows older than the first real value that OKX pads with
zeros are dropped and counted in the manifest; a zero is never read as "no flow".

  py -3.12 -X utf8 -B tools/okx_stats.py probe --out team/platform/OKX_STATS_PROBE.json --raw vault/forward/okx_stats/_raw
  py -3.12 -X utf8 -B tools/okx_stats.py fetch --dev data/okx_stats --forward vault/forward/okx_stats
  py -3.12 -X utf8 -B tools/okx_stats.py verify --root data/okx_stats [--raw vault/forward/okx_stats/_raw]
  py -3.12 -X utf8 -B tools/okx_stats.py get --dataset open-interest-history --key BTC-USDT-SWAP --period 1H [--end MS]
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from public_history import PACER, NoRedirect, Pacer, iso, parse_time, sha256_file, write_atomic
except ImportError:  # imported as tools.okx_stats
    from tools.public_history import PACER, NoRedirect, Pacer, iso, parse_time, sha256_file, write_atomic

BASE = "https://www.okx.com"
STAT = "/api/v5/rubik/stat/"
SUPPORT = STAT + "trading-data/support-coin"
SCHEMA = "okx-stats/v1"
TOOL = "tools/okx_stats.py"
MINUTE, HOUR, DAY = 60_000, 3_600_000, 86_400_000
CUTOFF_LAST_OPEN_MS = parse_time("2026-10-02T18:31:00Z")  # last DEV 1m candle (opens 18:31, closes 18:32)
DEV_END_MS = CUTOFF_LAST_OPEN_MS + MINUTE
DOC_EARLIEST_MS = parse_time("2024-01-01")  # OKX: "for period=1D, the data time range is up to January 1, 2024"
INSTRUMENTS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP")
CURRENCIES = ("BTC", "ETH", "SOL")
CONTRACT_PERIODS = ("5m", "15m", "30m", "1H", "2H", "4H", "6H", "12H", "1D", "2D", "3D", "5D", "1W", "1M", "3M",
                    "6Hutc", "12Hutc", "1Dutc", "2Dutc", "3Dutc", "5Dutc", "1Wutc", "1Mutc", "3Mutc")
CCY_PERIODS = ("5m", "1H", "1D")
FIXED = {"5m": 5 * MINUTE, "15m": 15 * MINUTE, "30m": 30 * MINUTE, "1H": HOUR, "2H": 2 * HOUR, "4H": 4 * HOUR,
         "6H": 6 * HOUR, "12H": 12 * HOUR, "1D": DAY, "2D": 2 * DAY, "3D": 3 * DAY, "5D": 5 * DAY, "1W": 7 * DAY}
MAX_BODY = 2_000_000
USER_AGENT = "TradingResearch/okx-stats (read-only public GET)"


def _dataset(route, level, columns, rate, units, params=None, periods=CONTRACT_PERIODS, limit=True):
    return {"route": STAT + route, "level": level, "columns": columns, "rate_limit": rate, "units": units,
            "params": params or {}, "periods": periods, "limit": limit}


DATASETS = {
    "open-interest-history": _dataset("contracts/open-interest-history", "instId", ["oi_contracts", "oi_ccy", "oi_usd"],
        "10 requests / 2 s, IP + instId", "oi in contracts, oiCcy in the base coin, oiUsd in USD"),
    "taker-volume-contract_unit1-contracts": _dataset("taker-volume-contract", "instId", ["sell_vol_contracts", "buy_vol_contracts"],
        "5 requests / 2 s, IP + instId", "taker sell / buy volume in contracts (unit=1)", {"unit": "1"}),
    "taker-volume-contract_unit2-usdt": _dataset("taker-volume-contract", "instId", ["sell_vol_usdt", "buy_vol_usdt"],
        "5 requests / 2 s, IP + instId", "taker sell / buy volume in USDT (unit=2, OKX calls it U)", {"unit": "2"}),
    "long-short-account-ratio-contract": _dataset("contracts/long-short-account-ratio-contract", "instId", ["long_short_acct_ratio"],
        "5 requests / 2 s, IP + instId", "accounts net long / accounts net short, all traders of the contract"),
    "long-short-account-ratio-contract-top-trader": _dataset("contracts/long-short-account-ratio-contract-top-trader", "instId",
        ["long_short_acct_ratio_top"], "5 requests / 2 s, IP + instId",
        "top traders (top 5% by open position value): accounts net long / net short"),
    "long-short-position-ratio-contract-top-trader": _dataset("contracts/long-short-position-ratio-contract-top-trader", "instId",
        ["long_short_pos_ratio_top"], "5 requests / 2 s, IP + instId", "top traders (top 5% by open position value): long / short positions"),
    "open-interest-volume": _dataset("contracts/open-interest-volume", "ccy", ["oi_usd", "vol_usd"], "5 requests / 2 s, IP",
        "total open interest and trading volume of all expiry futures and perpetuals of the currency, USD", periods=CCY_PERIODS, limit=False),
    "taker-volume_insttype-contracts": _dataset("taker-volume", "ccy", ["sell_vol", "buy_vol"], "5 requests / 2 s, IP",
        "taker sell / buy volume of all contracts of the currency (instType=CONTRACTS); OKX does not document the unit",
        {"instType": "CONTRACTS"}, CCY_PERIODS, False),
    "long-short-account-ratio": _dataset("contracts/long-short-account-ratio", "ccy", ["long_short_ratio"], "5 requests / 2 s, IP",
        "users net long / users net short, expiry futures and perpetuals of the currency", periods=CCY_PERIODS, limit=False),
}
ROUTES = {spec["route"] for spec in DATASETS.values()} | {SUPPORT}
PREFIX = ["ts_ms", "utc", "bar_close_ms", "bar_close_utc"]


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def period_parts(period):
    """(base period, local offset ms): UTC+8 bars for 6H and longer unless the period ends in 'utc'."""
    utc = period.endswith("utc")
    base = period[:-3] if utc else period
    if base not in FIXED and base not in ("1M", "3M"):
        raise ValueError("unknown period " + period)
    long_bar = base.endswith("M") or FIXED.get(base, 0) >= 6 * HOUR
    return base, 0 if utc or not long_bar else 8 * HOUR


def bar_close_ms(ts, period):
    """Close of the bar that opens at ts; months are calendar months in the bar's time zone."""
    base, offset = period_parts(period)
    if not base.endswith("M"):
        return ts + FIXED[base]
    local = datetime.fromtimestamp((ts + offset) / 1000, timezone.utc)
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    index = start.year * 12 + start.month - 1 + int(base[:-1])
    return round((start.replace(year=index // 12, month=index % 12 + 1) + (local - start)).timestamp() * 1000) - offset


def aligned(ts, period):
    """Whether ts is a bar boundary in the bar's time zone; multi-day bars (2D..1W) have no documented epoch."""
    base, offset = period_parts(period)
    if base.endswith("M"):
        local = datetime.fromtimestamp((ts + offset) / 1000, timezone.utc)
        return (local.day, local.hour, local.minute, local.second, local.microsecond) == (1, 0, 0, 0, 0)
    return FIXED[base] > DAY or (ts + offset) % FIXED[base] == 0


def approx_bar_ms(period):
    base, _ = period_parts(period)
    return int(30.44 * DAY * int(base[:-1])) if base.endswith("M") else FIXED[base]


def is_zero(row):
    try:
        return all(float(value) == 0 for value in row[1:])
    except (TypeError, ValueError):
        return False


class StatsClient:
    """Allowlisted rubik GET routes; per-route spacing plus the shared pace file; one retry after a failure."""

    def __init__(self, pacer, opener=None, route_gap=0.5, retry_wait=2.0, raw_dir=None):
        self.pacer, self.route_gap, self.retry_wait = pacer, route_gap, retry_wait
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.raw_dir = Path(raw_dir) if raw_dir else None
        self.lock, self.next_slot, self.requests = threading.Lock(), {}, 0

    def _wait(self, route):
        with self.lock:
            now = time.monotonic()
            slot = max(now, self.next_slot.get(route, 0.0))
            self.next_slot[route] = slot + self.route_gap
            self.requests += 1
        time.sleep(max(0.0, slot - now))
        self.pacer.wait()

    def _keep(self, body, digest):
        if self.raw_dir is None or body is None:
            return
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        path = self.raw_dir / f"{digest}.json"
        if not path.is_file():
            temporary = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
            temporary.write_bytes(body)
            os.replace(temporary, path)

    def _once(self, route, query):
        self._wait(route)
        url = BASE + route + ("?" + urllib.parse.urlencode(query) if query else "")
        page = {"query": dict(query), "received_at": None, "http": None, "code": None, "msg": None, "sha256": None,
                "bytes": 0, "data": None, "ok": False}
        request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}, method="GET")
        try:
            try:
                with self.opener.open(request, timeout=20) as response:
                    body, page["http"] = response.read(MAX_BODY + 1), getattr(response, "status", 200)
            except urllib.error.HTTPError as error:  # includes every 3xx: NoRedirect never follows
                body, page["http"] = error.read(MAX_BODY + 1), error.code
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
            page["received_at"], page["error"] = utc_now(), type(error).__name__
            return page
        page["received_at"], page["bytes"] = utc_now(), len(body)
        page["sha256"] = hashlib.sha256(body).hexdigest()
        self._keep(body, page["sha256"])
        if len(body) > MAX_BODY:
            page["error"] = "response too large"
            return page
        try:
            data = json.loads(body)
        except ValueError:
            page["error"] = "not JSON"
            return page
        if isinstance(data, dict):
            page["code"], page["msg"], page["data"] = data.get("code"), data.get("msg"), data.get("data")
        page["ok"] = page["http"] == 200 and page["code"] == "0" and page["data"] is not None
        return page

    def get(self, route, query):
        if route not in ROUTES:
            raise ValueError("Дозволені лише публічні маршрути OKX Trading Statistics.")
        page = self._once(route, query)
        retry = not page["ok"] and (page["http"] != 200 or page["code"] in (None, "50011"))
        if retry:  # network, HTTP or rate-limit failure: exactly one retry
            time.sleep(self.retry_wait * (2 if page["http"] == 429 or page["code"] == "50011" else 1))
            page = self._once(route, query)
        page["attempts"] = 2 if retry else 1
        return page


def base_query(dataset, key, period):
    spec = DATASETS[dataset]
    query = {spec["level"]: key, "period": period, **spec["params"]}
    if spec["limit"]:
        query["limit"] = "100"
    return query


def valid_rows(dataset, page):
    width = 1 + len(DATASETS[dataset]["columns"])
    data = page.get("data") if isinstance(page.get("data"), list) else []
    return [[str(x) for x in row] for row in data if isinstance(row, list) and len(row) == width and str(row[0]).isdigit()]


def page_series(client, dataset, key, period, max_pages=40, end=None):
    """Page backwards with `end` until an empty or all-zero page, an OKX range error, no progress or max_pages."""
    route, pages, rows, stop = DATASETS[dataset]["route"], [], {}, "max_pages"
    while len(pages) < max_pages:
        query = base_query(dataset, key, period)
        if end is not None:
            query["end"] = str(end)
        page = client.get(route, query)
        pages.append(page)
        if not page["ok"]:
            stop = "range_limit" if page.get("code") == "50030" else "error"
            break
        data = valid_rows(dataset, page)
        page["rows"], page["invalid_rows"] = len(data), len(page["data"]) - len(data) if isinstance(page["data"], list) else 0
        if not data:
            stop = "empty"
            break
        fresh = 0
        for row in data:
            if int(row[0]) not in rows:
                rows[int(row[0])], fresh = row, fresh + 1
        oldest = min(int(row[0]) for row in data)
        if all(is_zero(row) for row in data):
            stop = "zero_page"
            break
        if not fresh or (end is not None and oldest >= end):
            stop = "no_progress"
            break
        end = oldest
    return {"pages": pages, "rows": rows, "stop": stop}


def build(result, dataset, period):
    """Sorted rows with bar close times; leading (oldest) zero padding dropped; split at DEV_END_MS."""
    stamps = sorted(result["rows"])
    padding = 0
    while padding < len(stamps) and is_zero(result["rows"][stamps[padding]]):
        padding += 1
    kept = stamps[padding:]
    records = []
    for stamp in kept:
        close = bar_close_ms(stamp, period)
        records.append([stamp, iso(stamp), close, iso(close), *result["rows"][stamp][1:]])
    zero_kept = [iso(stamp) for stamp in kept if is_zero(result["rows"][stamp])]
    info = {"rows_returned": len(stamps), "zero_padding_dropped": padding,
            "zero_padding_span_utc": [iso(stamps[0]), iso(stamps[padding - 1])] if padding else None,
            "zero_rows_kept": len(zero_kept), "zero_rows_kept_first_50": zero_kept[:50],
            "misaligned_rows": sum(not aligned(stamp, period) for stamp in kept),
            "invalid_rows": sum(page.get("invalid_rows", 0) for page in result["pages"]),
            "oldest_utc": iso(kept[0]) if kept else None, "newest_utc": iso(kept[-1]) if kept else None}
    dev = [row for row in records if row[2] <= DEV_END_MS]
    return dev, records[len(dev):], info


def page_entries(pages):
    keys = ("sha256", "query", "http", "code", "msg", "rows", "bytes", "received_at", "attempts", "error")
    return [{key: page[key] for key in keys if key in page and page[key] is not None} for page in pages]


def write_side(root, side, dataset, key, period, records, info, result, fetched_at, replace=False):
    spec = DATASETS[dataset]
    folder = Path(root) / key / dataset / period
    if (folder / "MANIFEST.json").is_file() and not replace:
        return {"skipped": "MANIFEST.json exists (use --replace)", "folder": str(folder)}
    folder.mkdir(parents=True, exist_ok=True)
    name = f"okx_{key}_{dataset}_{period}.csv"
    temporary = folder / (name + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(PREFIX + spec["columns"])
        writer.writerows(records)
    os.replace(temporary, folder / name)
    chain = hashlib.sha256()
    for page in result["pages"]:
        chain.update(str(page.get("sha256")).encode())
    dev = side == "dev"
    open_at_fetch = sum(row[2] > parse_time(fetched_at[:19] + "Z") for row in records) if fetched_at else 0
    manifest = {
        "schema": SCHEMA, "side": side,
        "role": ("DEVELOPMENT public Trading Statistics: only bars closed at or before the DEV end; never a holdout"
                 if dev else "FORWARD, Judge's vault: bars closing after the DEV end; authors must never read this"),
        "dataset": dataset, "endpoint": BASE + spec["route"], "level": spec["level"], "key": key, "period": period,
        "query": base_query(dataset, key, period), "paging": "newest first, then end=<oldest ts of the previous page>",
        "rate_limit_documented": spec["rate_limit"], "units": spec["units"], "columns": PREFIX + spec["columns"],
        "time": "ts = bar open (OKX), bar_close = next bar open; periods 6H and longer without 'utc' are UTC+8 bars",
        "split_rule": f"bar_close_ms <= {DEV_END_MS} ({iso(DEV_END_MS)}) -> DEV; later -> FORWARD vault",
        "cutoff_last_open_utc": iso(CUTOFF_LAST_OPEN_MS), "dev_end_utc": iso(DEV_END_MS),
        "file": name, "sha256": sha256_file(folder / name), "rows": len(records),
        "first_utc": records[0][1] if records else None, "last_utc": records[-1][1] if records else None,
        "first_bar_close_utc": records[0][3] if records else None, "last_bar_close_utc": records[-1][3] if records else None,
        "series": info if not dev else {k: v for k, v in info.items() if k != "newest_utc"},
        "stop_reason": result["stop"], "truncated": result["stop"] in ("max_pages", "error"),
        "pages": page_entries(result["pages"]), "page_count": len(result["pages"]), "pages_sha256_chain": chain.hexdigest(),
        "raw_pages": "exact bodies kept by the Judge as <vault>/okx_stats/_raw/<sha256>.json",
        "fetched_at": fetched_at, "tool": TOOL, "tool_sha256": sha256_file(__file__),
        "method": "GET only, no credentials, no redirects, <= 2 requests / s per endpoint, one retry after a failure"}
    if dev:
        manifest["series"]["rows_forward"] = info.get("rows_forward")
    else:
        manifest["rows_open_at_fetch"] = open_at_fetch
        manifest["note"] = "Rows whose bar was still open at fetch time are preliminary; OKX revises the newest row."
    write_atomic(folder / "MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=1) + "\n")
    return {"folder": str(folder), "rows": len(records)}


def fetch_series(client, dataset, key, period, dev_root, forward_root, replace=False, max_pages=40):
    result = page_series(client, dataset, key, period, max_pages=max_pages)
    fetched_at = utc_now()
    dev, forward, info = build(result, dataset, period)
    info["rows_dev"], info["rows_forward"] = len(dev), len(forward)
    done = {side: write_side(root, side, dataset, key, period, records, dict(info), result, fetched_at, replace)
            for side, root, records in (("dev", dev_root, dev), ("forward", forward_root, forward))}
    return {"dataset": dataset, "key": key, "period": period, "stop": result["stop"], "pages": len(result["pages"]),
            "rows_dev": len(dev), "rows_forward": len(forward), "oldest_utc": info["oldest_utc"], "newest_utc": info["newest_utc"],
            "dev_first_utc": dev[0][1] if dev else None, "dev_last_utc": dev[-1][1] if dev else None,
            "zero_padding_dropped": info["zero_padding_dropped"], "zero_rows_kept": info["zero_rows_kept"],
            "misaligned_rows": info["misaligned_rows"], "skipped": [s for s, d in done.items() if "skipped" in d],
            "errors": [{"code": p.get("code"), "http": p.get("http"), "msg": p.get("msg"), "error": p.get("error")}
                       for p in result["pages"] if not p["ok"]]}


def probe_series(client, dataset, key, period):
    """Cheap depth probe: the newest page, then one jump to about 1430 bars back, paging only where needed."""
    spec, method = DATASETS[dataset], "latest"
    first = page_series(client, dataset, key, period, max_pages=1)
    pages, rows = list(first["pages"]), dict(first["rows"])

    def more(end, limit):
        part = page_series(client, dataset, key, period, max_pages=limit, end=end)
        pages.extend(part["pages"])
        for stamp, row in part["rows"].items():
            rows.setdefault(stamp, row)
        return part

    last = first
    if rows and first["stop"] == "max_pages":
        if not spec["limit"]:
            method, last = "paged", more(min(rows), 30)
        elif len(first["rows"]) < 100:
            method, last = "latest+confirm", more(min(rows), 1)
        else:
            jump = max(rows) - 1430 * approx_bar_ms(period)
            if jump <= DOC_EARLIEST_MS:
                method, last = "paged", more(min(rows), 16)
            else:
                method, last = "jump", more(jump, 1)
                real = [s for s, row in last["rows"].items() if not is_zero(row)]
                if len(real) >= 100:
                    method, last = "jump+paged", more(min(last["rows"]), 20)
                elif not real:
                    method, last = "jump-missed+paged", more(min(first["rows"]), 16)
    real = sorted(stamp for stamp, row in rows.items() if not is_zero(row))
    return {"dataset": dataset, "key": key, "period": period, "method": method, "requests": len(pages),
            "newest_utc": iso(max(rows)) if rows else None, "oldest_returned_utc": iso(min(rows)) if rows else None,
            "oldest_nonzero_utc": iso(real[0]) if real else None, "rows_seen": len(rows), "nonzero_rows_seen": len(real),
            "stop": last["stop"] if pages else None, "reaches_dev": bool(real and bar_close_ms(real[0], period) <= DEV_END_MS),
            "depth_bars_estimate": round((real[-1] - real[0]) / approx_bar_ms(period)) + 1 if real else 0,
            "pages": [{k: p.get(k) for k in ("sha256", "http", "code", "msg", "rows", "received_at") if p.get(k) is not None}
                      | {"end": p["query"].get("end")} for p in pages]}


def unit_check(client, inst, period="5m"):
    """unit=0 (coin) against unit=1 (contracts) on one newest page each; the two newest rows are skipped as preliminary."""
    route, pages = DATASETS["taker-volume-contract_unit1-contracts"]["route"], {}
    for unit in ("1", "0"):
        query = {"instId": inst, "period": period, "unit": unit, "limit": "100"}
        pages[unit] = client.get(route, query)
    rows = {u: {int(r[0]): r for r in valid_rows("taker-volume-contract_unit1-contracts", p)} for u, p in pages.items()}
    common = sorted(set(rows["1"]) & set(rows["0"]))[:-2]
    ratios = [float(rows["0"][s][i]) / float(rows["1"][s][i]) for s in common for i in (1, 2) if float(rows["1"][s][i])]
    return {"inst": inst, "period": period, "rows_compared": len(common),
            "coin_per_contract_median": statistics.median(ratios) if ratios else None,
            "coin_per_contract_min": min(ratios) if ratios else None, "coin_per_contract_max": max(ratios) if ratios else None,
            "pages": [{"unit": u, "sha256": p.get("sha256"), "code": p.get("code")} for u, p in pages.items()]}


def jobs(datasets=None, instruments=INSTRUMENTS, currencies=CURRENCIES, periods=None):
    for dataset in datasets or DATASETS:
        spec = DATASETS[dataset]
        for key in instruments if spec["level"] == "instId" else currencies:
            for period in spec["periods"]:
                if not periods or period in periods:
                    yield dataset, key, period


def pending(work, dev_root, forward_root):
    """Jobs whose DEV or FORWARD folder has no manifest yet: a resumed fetch spends no requests on finished series."""
    return [job for job in work if not all((Path(root) / job[1] / job[0] / job[2] / "MANIFEST.json").is_file()
                                           for root in (dev_root, forward_root))]


def run_by_route(work, function):
    """One worker per route, each running its jobs in order, so no endpoint gets more than its own spacing."""
    groups = {}
    for job in work:
        groups.setdefault(DATASETS[job[0]]["route"], []).append(job)
    lock, results = threading.Lock(), []

    def worker(items):
        for job in items:
            try:
                out = function(*job)
            except Exception as error:  # one series failing never stops the others; recorded, not retried
                out = {"dataset": job[0], "key": job[1], "period": job[2], "exception": repr(error)}
            with lock:
                results.append(out)
                print(json.dumps({k: out.get(k) for k in ("dataset", "key", "period", "stop", "rows_dev", "rows_forward",
                      "oldest_nonzero_utc", "oldest_utc", "requests", "exception") if k in out}, ensure_ascii=False), flush=True)

    with ThreadPoolExecutor(max_workers=max(1, len(groups))) as pool:
        list(pool.map(worker, groups.values()))
    return results


def write_index(root, side):
    """INDEX.json of every series folder under root (all runs, not only the last one)."""
    rows = []
    for path in sorted(Path(root).rglob("MANIFEST.json")):
        if "_raw" in path.parts:
            continue
        manifest = json.loads(path.read_text(encoding="utf-8"))
        rows.append({"path": path.parent.relative_to(root).as_posix(), "dataset": manifest["dataset"], "key": manifest["key"],
                     "period": manifest["period"], "rows": manifest["rows"], "first_utc": manifest["first_utc"],
                     "last_utc": manifest["last_utc"], "file": manifest["file"], "sha256": manifest["sha256"],
                     "stop_reason": manifest["stop_reason"], "fetched_at": manifest["fetched_at"]})
    index = {"schema": SCHEMA + "/index", "side": side, "dev_end_utc": iso(DEV_END_MS), "generated_at": utc_now(),
             "series": rows}
    write_atomic(Path(root) / "INDEX.json", json.dumps(index, ensure_ascii=False, indent=1) + "\n")
    return index


def verify(root, raw=None):
    """Problems in a store; an empty list means files, hashes, order, bar closes and the cutoff side agree.

    With raw (the vault's _raw folder) every listed page is re-hashed and the CSV is rebuilt from the raw bodies."""
    root, problems, seen = Path(root), [], 0
    for path in sorted(root.rglob("MANIFEST.json")):
        if "_raw" in path.parts:
            continue
        seen += 1
        where = str(path.parent.relative_to(root))
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            dataset, period, dev = manifest["dataset"], manifest["period"], manifest["side"] == "dev"
            spec = DATASETS[dataset]
        except (OSError, ValueError, KeyError) as error:
            problems.append(f"{where}: маніфест нечитаний ({error!r})")
            continue
        csv_path = path.parent / manifest["file"]
        if not csv_path.is_file():
            problems.append(f"{where}: файл {manifest['file']} відсутній")
            continue
        if sha256_file(csv_path) != manifest["sha256"]:
            problems.append(f"{where}: sha256 CSV не збігається")
        with csv_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            header, body = next(reader, None), list(reader)
        if header != PREFIX + spec["columns"]:
            problems.append(f"{where}: інші стовпці")
            continue
        previous = None
        for row in body:
            stamp, close = int(row[0]), int(row[2])
            if previous is not None and stamp <= previous:
                problems.append(f"{where}: порушено порядок часу на {row[1]}")
                break
            if row[1] != iso(stamp) or close != bar_close_ms(stamp, period) or row[3] != iso(close):
                problems.append(f"{where}: неузгоджений час бару {row[1]}")
                break
            if (close <= DEV_END_MS) != dev:
                problems.append(f"{where}: бар {row[1]} (закриття {row[3]}) не на своєму боці cutoff")
                break
            previous = stamp
        if len(body) != manifest["rows"]:
            problems.append(f"{where}: рядків {len(body)}, у маніфесті {manifest['rows']}")
        if body and (body[0][1], body[-1][1]) != (manifest["first_utc"], manifest["last_utc"]):
            problems.append(f"{where}: перший/останній час не збігається з маніфестом")
        if raw is not None:
            pages = []
            for entry in manifest["pages"]:
                raw_path = Path(raw) / f"{entry.get('sha256')}.json"
                if not raw_path.is_file() or sha256_file(raw_path) != entry.get("sha256"):
                    problems.append(f"{where}: сира сторінка {entry.get('sha256')} відсутня або змінена")
                    pages = None
                    break
                try:
                    data = json.loads(raw_path.read_bytes())
                except ValueError:
                    data = {}
                ok = entry.get("http") == 200 and isinstance(data, dict) and data.get("code") == "0"
                pages.append({"ok": ok, "data": data.get("data") if ok else None})
            if pages is not None:
                rows = {}
                for page in pages:
                    for row in valid_rows(dataset, page):
                        rows.setdefault(int(row[0]), row)
                dev_rows, forward_rows, _ = build({"rows": rows, "pages": []}, dataset, period)
                expected = [[str(x) for x in r] for r in (dev_rows if dev else forward_rows)]
                if expected != body:
                    problems.append(f"{where}: CSV не відтворюється з сирих сторінок")
    if not seen:
        problems.append("жодного MANIFEST.json")
    return problems


def make_client(rate, raw):
    if not 0 < rate <= 8:
        raise SystemExit("--rate: 0 < rate <= 8")
    return StatsClient(Pacer(PACER, rate), raw_dir=raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    probe = sub.add_parser("probe", help="history depth of every dataset x key x period; JSON with timestamps only")
    probe.add_argument("--out", required=True)
    probe.add_argument("--raw", required=True, help="vault folder for raw pages (_raw)")
    probe.add_argument("--rate", type=float, default=5.0, help="requests per second for all local processes together")
    get = sub.add_parser("fetch", help="fetch every series, write DEV and FORWARD folders with manifests")
    get.add_argument("--dev", required=True)
    get.add_argument("--forward", required=True)
    get.add_argument("--raw", help="default: <forward>/_raw")
    get.add_argument("--datasets", nargs="*")
    get.add_argument("--periods", nargs="*")
    get.add_argument("--replace", action="store_true", help="overwrite folders that already have a manifest")
    get.add_argument("--rate", type=float, default=5.0)
    one = sub.add_parser("get", help="one GET; prints counts and timestamps, never values")
    one.add_argument("--dataset", required=True, choices=sorted(DATASETS))
    one.add_argument("--key", required=True)
    one.add_argument("--period", required=True)
    one.add_argument("--end")
    one.add_argument("--begin")
    one.add_argument("--raw", required=True)
    check = sub.add_parser("verify", help="check files, hashes, order, bar closes and cutoff side")
    check.add_argument("--root", required=True)
    check.add_argument("--raw")
    args = parser.parse_args()
    if args.command == "verify":
        problems = verify(args.root, args.raw)
        print(json.dumps({"ok": not problems, "problems": problems[:200], "problem_count": len(problems)}, ensure_ascii=False))
        return 1 if problems else 0
    if args.command == "get":
        client = make_client(2.0, args.raw)
        query = base_query(args.dataset, args.key, args.period)
        query.update({k: v for k, v in (("end", args.end), ("begin", args.begin)) if v})
        page = client.get(DATASETS[args.dataset]["route"], query)
        rows = valid_rows(args.dataset, page) if page["ok"] else []
        stamps = sorted(int(r[0]) for r in rows)
        print(json.dumps({"query": query, "http": page["http"], "code": page["code"], "msg": page["msg"], "rows": len(rows),
                          "nonzero": sum(not is_zero(r) for r in rows), "oldest": iso(stamps[0]) if stamps else None,
                          "newest": iso(stamps[-1]) if stamps else None, "sha256": page["sha256"]}, ensure_ascii=False))
        return 0
    if args.command == "probe":
        client = make_client(args.rate, args.raw)
        started = utc_now()
        support = client.get(SUPPORT, {})
        contract = (support.get("data") or {}).get("contract", []) if isinstance(support.get("data"), dict) else []
        units = [unit_check(client, inst) for inst in INSTRUMENTS]
        series = run_by_route(list(jobs()), lambda d, k, p: probe_series(client, d, k, p))
        report = {"schema": SCHEMA + "/probe", "started_at": started, "finished_at": utc_now(), "requests": client.requests,
                  "dev_end_utc": iso(DEV_END_MS), "support_coin": {"sha256": support.get("sha256"), "code": support.get("code"),
                  "contract_has": {c: c in contract for c in CURRENCIES}},
                  "unit_check": units, "series": sorted(series, key=lambda r: (r["dataset"], r["key"], r["period"]))}
        write_atomic(Path(args.out), json.dumps(report, ensure_ascii=False, indent=1) + "\n")
        print(json.dumps({"requests": client.requests, "series": len(series)}))
        return 0
    client = make_client(args.rate, args.raw or Path(args.forward) / "_raw")
    work = list(jobs(args.datasets, periods=args.periods))
    if not args.replace:
        work = pending(work, args.dev, args.forward)
    results = run_by_route(work, lambda d, k, p: fetch_series(client, d, k, p, args.dev, args.forward, args.replace))
    for side, root in (("dev", args.dev), ("forward", args.forward)):
        write_index(root, side)
    summary = Path(args.forward) / f"FETCH_SUMMARY_{utc_now()[:10]}.json"
    write_atomic(summary, json.dumps({"requests": client.requests, "results": sorted(results, key=lambda r: (r["dataset"], r["key"], r["period"]))},
                                     ensure_ascii=False, indent=1) + "\n")
    print(json.dumps({"requests": client.requests, "series": len(results), "summary": str(summary),
                      "failed": [r for r in results if "exception" in r or r.get("errors")][:20]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
