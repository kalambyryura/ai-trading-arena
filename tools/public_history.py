"""Resumable public OKX history for authors: 1m candles and funding up to a cutoff, with a manifest.

GET-only public market endpoints: no credentials, account, order or redirect. Only candles that closed
at or before the cutoff are stored. Day chunks are cached, so an interrupted fetch resumes; one pacing
file under the primary repository's runtime/ keeps every local process and worktree together under the shared public IP limit.
Besides traded candles (--kind trade) it fetches index candles (--kind index, e.g. --inst BTC-USDT; OKX limit
10 requests / 2 s, so --rate 4) and mark-price candles (--kind mark, e.g. --inst BTC-USDT-SWAP), and the public
instrument specification (instrument --inst ETH-USDT-SWAP --out file.json).

  py -3.12 -X utf8 -B tools/public_history.py fetch --out <dir> --start 2025-10-01 --cutoff 2026-10-02T18:32:35.865Z
  py -3.12 -X utf8 -B tools/public_history.py fetch --kind index --inst BTC-USDT --rate 4 --out <dir> --start 2024-01-01 --cutoff ...
  py -3.12 -X utf8 -B tools/public_history.py verify --out <dir>
  py -3.12 -X utf8 -B tools/public_history.py instrument --inst ETH-USDT-SWAP --out tools/okx_instrument_eth_usdt_swap.json
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://openapi.okx.com"
CANDLES = "/api/v5/market/history-candles"
FUNDING = "/api/v5/public/funding-rate-history"
INDEX_CANDLES = "/api/v5/market/history-index-candles"
MARK_CANDLES = "/api/v5/market/history-mark-price-candles"
INSTRUMENTS = "/api/v5/public/instruments"
MARKET_HISTORY = "/api/v5/public/market-data-history"  # OKX "Get historical market data": a listing, never auto-downloaded
COLUMNS = ["ts_ms", "utc", "open", "high", "low", "close", "vol_contracts", "vol_btc", "vol_quote_usdt", "confirm"]
PRICE_COLUMNS = ["ts_ms", "utc", "open", "high", "low", "close", "confirm"]
KINDS = {"trade": (CANDLES, COLUMNS, 9), "index": (INDEX_CANDLES, PRICE_COLUMNS, 6), "mark": (MARK_CANDLES, PRICE_COLUMNS, 6)}
FUNDING_COLUMNS = ["funding_time_ms", "utc", "funding_rate", "realized_rate", "method", "formula_type"]
SCHEMA = "public-history/v1"
ROLE = "DEVELOPMENT public market data; never a holdout; nothing after the cutoff"
MINUTE, DAY = 60_000, 86_400_000
try:  # one pace file for the whole repository, shared by every worktree (same root as the research lease)
    from research_limits import shared_repository_root
except ImportError:  # imported as tools.public_history
    from tools.research_limits import shared_repository_root
PACER = shared_repository_root(Path(__file__).resolve().parents[1]) / "runtime" / "public_rate" / "okx_public.pace"


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text):
    """Epoch milliseconds, a UTC date or an ISO UTC timestamp ending in Z."""
    text = str(text).strip()
    if text.isdigit():
        return int(text)
    if len(text) == 10:
        text += "T00:00:00Z"
    if not text.endswith("Z"):
        raise ValueError("Час має бути в UTC: мілісекунди, YYYY-MM-DD або ISO із Z.")
    return round(datetime.fromisoformat(text[:-1] + "+00:00").timestamp() * 1000)


def last_open_before(cutoff_ms):
    """Open time of the newest 1m candle that closed at or before the cutoff."""
    return (cutoff_ms // MINUTE - 1) * MINUTE


class Pacer:
    """Cross-process pacing: every caller reserves the next slot in one small file under an exclusive lock."""

    def __init__(self, path=PACER, per_second=8.0):
        self.path, self.gap, self.local = Path(path), 1.0 / per_second, threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def reserve(self):
        lock = self.path.with_name(self.path.name + ".lock")
        with self.local:
            while True:
                try:
                    handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    break
                except (FileExistsError, PermissionError):  # Windows: PermissionError while another process deletes it
                    try:
                        if time.time() - lock.stat().st_mtime > 5:  # a crashed holder never blocks others for long
                            lock.unlink()
                    except OSError:
                        pass
                    time.sleep(.005)
            try:
                now = time.time()
                try:
                    slot = max(now, float(self.path.read_text(encoding="ascii")))
                except (OSError, ValueError):
                    slot = now
                self.path.write_text(repr(slot + self.gap), encoding="ascii")
            finally:
                os.close(handle)
                lock.unlink()
        return slot - now

    def wait(self):
        time.sleep(max(0.0, self.reserve()))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PublicClient:
    """Allowlisted public GET routes only; patient backoff because the IP limit is shared."""

    def __init__(self, pacer, opener=None, attempts=40):
        self.pacer, self.attempts = pacer, attempts
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def get(self, route, query):
        if route not in {CANDLES, FUNDING, INDEX_CANDLES, MARK_CANDLES, INSTRUMENTS, MARKET_HISTORY}:
            raise ValueError("Дозволені лише публічні історичні маршрути OKX.")
        url = BASE + route + "?" + urllib.parse.urlencode(query)
        for attempt in range(self.attempts):
            self.pacer.wait()
            try:
                request = urllib.request.Request(url, headers={"Accept": "application/json",
                    "User-Agent": "TradingResearch/public-history (read-only public GET)"}, method="GET")
                with self.opener.open(request, timeout=20) as response:
                    body = response.read(2_000_001)
                if len(body) > 2_000_000:
                    raise ValueError("response too large")
                data = json.loads(body)
                if data.get("code") == "0" and isinstance(data.get("data"), list):
                    return data["data"], hashlib.sha256(body).hexdigest()
                time.sleep(min(2 + attempt, 20))
            except urllib.error.HTTPError as error:
                time.sleep(min(3 + 2 * attempt, 30) if error.code == 429 else min(2 + attempt, 20))
            except (urllib.error.URLError, TimeoutError, ValueError, ConnectionError, OSError):
                time.sleep(min(2 + 2 * attempt, 30))
        raise RuntimeError("Публічний GET не вдався кілька разів: " + route)


def listing(client, query):
    """One GET of the historical-market-data listing; returns OKX's code, message and data unchanged, no retry.

    Whatever it lists (for example monthly funding files) is reported only: fetching files is a separate,
    owner-approved step."""
    if client.attempts != 1:
        raise ValueError("listing is a single probe: use attempts=1")
    url = BASE + MARKET_HISTORY + "?" + urllib.parse.urlencode(query)
    client.pacer.wait()
    request = urllib.request.Request(url, headers={"Accept": "application/json",
        "User-Agent": "TradingResearch/public-history (read-only public GET)"}, method="GET")
    try:
        with client.opener.open(request, timeout=20) as response:
            body = response.read(2_000_001)
        data = json.loads(body)
        return {"http_status": 200, "code": data.get("code"), "msg": data.get("msg"), "data": data.get("data")}
    except urllib.error.HTTPError as error:
        return {"http_status": error.code, "body": error.read(2000).decode("utf-8", "replace")}


def write_atomic(path, text):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def fetch_chunk(client, inst, start_ms, end_ms, cache, kind="trade"):
    """Confirmed 1m candles with start_ms <= ts < end_ms; the cached file name pins its exact bounds."""
    route, _, width = KINDS[kind]
    prefix = "1m" if kind == "trade" else kind + "1m"
    path = cache / f"{prefix}_{inst}_{iso(start_ms)[:10]}_{start_ms}_{end_ms}.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    rows, pages, chain, after = {}, 0, hashlib.sha256(), end_ms
    while after > start_ms:
        data, digest = client.get(route, {"instId": inst, "bar": "1m", "after": after, "limit": 100})
        pages += 1
        chain.update(digest.encode())
        if not data:
            break
        for row in data:
            stamp = int(row[0])
            if len(row) == width and row[width - 1] == "1" and not stamp % MINUTE and start_ms <= stamp < end_ms:
                rows[str(stamp)] = row
        oldest = min(int(row[0]) for row in data)
        if oldest >= after:
            break
        after = oldest
    result = {"start_ms": start_ms, "end_ms": end_ms, "pages": pages, "pages_sha256": chain.hexdigest(), "rows": rows}
    write_atomic(path, json.dumps(result))
    return result


def candles(client, inst, start_ms, cutoff_ms, cache, workers=4, kind="trade"):
    end = last_open_before(cutoff_ms) + MINUTE
    start = -(-start_ms // MINUTE) * MINUTE
    if start >= end:
        raise ValueError("Початок має бути раніше за останню закриту до cutoff свічку.")
    cache.mkdir(parents=True, exist_ok=True)
    bounds = [(day, min(day - day % DAY + DAY, end)) for day in [start] + list(range(start - start % DAY + DAY, end, DAY))]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        parts = list(pool.map(lambda b: fetch_chunk(client, inst, b[0], b[1], cache, kind), bounds))
    rows, pages, chain = {}, 0, hashlib.sha256()
    for part in parts:
        pages += part["pages"]
        chain.update(part["pages_sha256"].encode())
        rows.update({int(stamp): row for stamp, row in part["rows"].items()})
    stamps = sorted(rows)
    edges = [start - MINUTE] + stamps + [end]
    gaps = [[iso(a + MINUTE), iso(b - MINUTE), (b - a) // MINUTE - 1] for a, b in zip(edges, edges[1:]) if b - a != MINUTE]
    return {"rows": [rows[stamp] for stamp in stamps], "pages": pages, "pages_sha256_chain": chain.hexdigest(),
            "expected_rows": (end - start) // MINUTE, "gaps": gaps, "start_ms": start, "last_open_ms": end - MINUTE}


def funding(client, inst, start_ms, cutoff_ms):
    rows, after, pages, chain = {}, cutoff_ms + 1, 0, hashlib.sha256()
    while after > start_ms:
        data, digest = client.get(FUNDING, {"instId": inst, "after": after, "limit": 100})
        pages += 1
        chain.update(digest.encode())
        if not data:
            break
        for row in data:
            stamp = int(row["fundingTime"])
            if start_ms <= stamp <= cutoff_ms:
                rows[stamp] = row
        oldest = min(int(row["fundingTime"]) for row in data)
        if oldest >= after:
            break
        after = oldest
    return {"rows": [rows[stamp] for stamp in sorted(rows)], "pages": pages, "pages_sha256_chain": chain.hexdigest()}


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(out, inst, start_ms, cutoff_ms, client, workers=4, with_funding=True, kind="trade"):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    route, columns, width = KINDS[kind]
    with_funding = with_funding and kind == "trade"
    found = candles(client, inst, start_ms, cutoff_ms, out / "cache", workers, kind)
    if not found["rows"]:
        raise RuntimeError("OKX не повернув жодної закритої свічки для цього періоду.")
    first, last = int(found["rows"][0][0]), int(found["rows"][-1][0])
    series = "1m" if kind == "trade" else kind + "-1m"
    name = f"okx_{inst}_{series}_{iso(first)[:10]}_{iso(last)[:10]}.csv"
    with (out / name).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in found["rows"]:
            writer.writerow([int(row[0]), iso(int(row[0])), *row[1:width]])
    manifest = {"schema": SCHEMA, "role": ROLE, "instrument": inst, "bar": "1m",
                "cutoff_ms": cutoff_ms, "cutoff_utc": datetime.fromtimestamp(cutoff_ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "requested_start_utc": iso(start_ms),
                "method": "GET only, no credentials, no redirects, paged backwards with after=, confirm=1 rows only",
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "tool": "tools/public_history.py", "tool_sha256": sha256_file(__file__),
                "candles": {"source": BASE + route, "kind": kind, "file": name, "sha256": sha256_file(out / name), "columns": columns,
                            "rows": len(found["rows"]), "expected_rows": found["expected_rows"],
                            "first_open_utc": iso(first), "last_open_utc": iso(last), "gap_count": len(found["gaps"]),
                            "missing_minutes": sum(gap[2] for gap in found["gaps"]), "gaps_first_200": found["gaps"][:200],
                            "pages": found["pages"], "pages_sha256_chain": found["pages_sha256_chain"]}}
    if with_funding:
        paid = funding(client, inst, start_ms, cutoff_ms)
        if paid["rows"]:
            stamps = [int(row["fundingTime"]) for row in paid["rows"]]
            funding_name = f"okx_{inst}_funding_{iso(stamps[0])[:10]}_{iso(stamps[-1])[:10]}.csv"
            with (out / funding_name).open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(FUNDING_COLUMNS)
                for stamp, row in zip(stamps, paid["rows"]):
                    writer.writerow([stamp, iso(stamp), row.get("fundingRate", ""), row.get("realizedRate", ""),
                                     row.get("method", ""), row.get("formulaType", "")])
            manifest["funding"] = {"source": BASE + FUNDING, "file": funding_name, "sha256": sha256_file(out / funding_name),
                "columns": FUNDING_COLUMNS, "rows": len(stamps), "first_utc": iso(stamps[0]), "last_utc": iso(stamps[-1]),
                "pages": paid["pages"], "pages_sha256_chain": paid["pages_sha256_chain"],
                "note": "OKX serves roughly the last three months of funding history; earlier funding is absent, not zero."}
        else:
            manifest["funding"] = {"source": BASE + FUNDING, "file": None, "rows": 0, "pages": paid["pages"]}
    write_atomic(out / "MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=1) + "\n")
    return manifest


def verify(out):
    """Problems found in a fetched folder; an empty list means files, hashes, order and cutoff agree."""
    out, problems = Path(out), []
    try:
        manifest = json.loads((out / "MANIFEST.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ["MANIFEST.json відсутній або нечитаний"]
    last_allowed = last_open_before(manifest["cutoff_ms"])
    for key, header, limit in (("candles", COLUMNS, last_allowed), ("funding", FUNDING_COLUMNS, manifest["cutoff_ms"])):
        entry = manifest.get(key)
        if not entry or not entry.get("file"):
            continue
        header = entry.get("columns", header)
        confirm = header.index("confirm") if "confirm" in header else None
        path = out / entry["file"]
        if not path.is_file():
            problems.append(f"{key}: файл {entry['file']} відсутній")
            continue
        if sha256_file(path) != entry["sha256"]:
            problems.append(f"{key}: sha256 не збігається")
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            if next(reader, None) != header:
                problems.append(f"{key}: інші стовпці")
                continue
            previous, count = None, 0
            for row in reader:
                stamp, count = int(row[0]), count + 1
                if previous is not None and stamp <= previous:
                    problems.append(f"{key}: порушено порядок часу на {row[1]}")
                    break
                if stamp > limit:
                    problems.append(f"{key}: запис {row[1]} після cutoff")
                    break
                if key == "candles" and row[confirm] != "1":
                    problems.append(f"{key}: незакрита свічка {row[1]}")
                    break
                previous = stamp
        if count != entry["rows"]:
            problems.append(f"{key}: рядків {count}, у маніфесті {entry['rows']}")
    return problems


def instrument(client, inst, inst_type="SWAP"):
    """Public instrument specification (tick, lot, contract value) as OKX returns it: a one-element list."""
    data, _ = client.get(INSTRUMENTS, {"instType": inst_type, "instId": inst})
    if len(data) != 1 or data[0].get("instId") != inst:
        raise RuntimeError("OKX не повернув специфікацію інструмента " + inst)
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    get = sub.add_parser("fetch", help="завантажити або продовжити завантаження")
    get.add_argument("--out", required=True)
    get.add_argument("--start", required=True, help="UTC: YYYY-MM-DD, ISO із Z або мілісекунди")
    get.add_argument("--cutoff", required=True, help="спільний training cutoff, UTC")
    get.add_argument("--inst", default="BTC-USDT-SWAP")
    get.add_argument("--rate", type=float, default=8.0, help="запитів за секунду на всі процеси разом (публічний ліміт 20 за 2 с)")
    get.add_argument("--workers", type=int, default=4)
    get.add_argument("--no-funding", action="store_true")
    get.add_argument("--kind", choices=sorted(KINDS), default="trade", help="trade candles, index candles or mark-price candles")
    probe = sub.add_parser("listing", help="one GET of OKX historical-market-data listing (module 3 = funding); prints, never downloads")
    probe.add_argument("--module", required=True)
    probe.add_argument("--inst-type", default="SWAP")
    probe.add_argument("--family", required=True, help="instrument family, e.g. BTC-USDT")
    probe.add_argument("--aggr", choices=("daily", "monthly"), default="monthly")
    probe.add_argument("--begin", required=True)
    probe.add_argument("--end", required=True)
    spec = sub.add_parser("instrument", help="public instrument specification to a JSON file")
    spec.add_argument("--inst", required=True)
    spec.add_argument("--out", required=True)
    check = sub.add_parser("verify", help="перевірити файли, хеші, порядок і cutoff")
    check.add_argument("--out", required=True)
    args = parser.parse_args()
    if args.command == "verify":
        problems = verify(args.out)
        print(json.dumps({"ok": not problems, "problems": problems}, ensure_ascii=False))
        return 1 if problems else 0
    if args.command == "listing":
        query = {"module": args.module, "instType": args.inst_type, "instFamilyList": args.family, "dateAggrType": args.aggr,
                 "begin": parse_time(args.begin), "end": parse_time(args.end)}
        print(json.dumps(listing(PublicClient(Pacer(PACER, 2.0), attempts=1), query), ensure_ascii=False, indent=1)[:6000])
        return 0
    if args.command == "instrument":
        data = instrument(PublicClient(Pacer(PACER, 2.0)), args.inst)
        write_atomic(Path(args.out), json.dumps(data, ensure_ascii=False, indent=1) + "\n")
        print(json.dumps({"inst": args.inst, "out": args.out, "tickSz": data[0].get("tickSz"), "lotSz": data[0].get("lotSz"),
                          "ctVal": data[0].get("ctVal"), "minSz": data[0].get("minSz")}, ensure_ascii=False))
        return 0
    if not 0 < args.rate <= 9:
        parser.error("--rate: 0 < rate <= 9, щоб лишити запас іншим локальним процесам")
    client = PublicClient(Pacer(PACER, args.rate))
    manifest = fetch(args.out, args.inst, parse_time(args.start), parse_time(args.cutoff), client,
                     max(1, min(args.workers, 8)), not args.no_funding, args.kind)
    print(json.dumps({"file": manifest["candles"]["file"], "rows": manifest["candles"]["rows"],
                      "gap_count": manifest["candles"]["gap_count"], "funding_rows": manifest.get("funding", {}).get("rows")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
