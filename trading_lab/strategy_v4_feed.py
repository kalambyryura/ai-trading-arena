"""Live feature book for schema-4 recipes (R1 full F2): public history at the season start, then every minute.

BookFeed wraps an engine's market feed (arena_market.PublicFeed for PAPER, exchange_api.ExchangeFeed for Demo and
Live): every snapshot gets snapshot["v4_features"] from one strategy_v4_book.FeatureBook holding the features of every
schema-4 participant.

The warm-up never stalls the season: each snapshot reads at most PAGES_PER_SNAPSHOT history pages within
SNAPSHOT_SECONDS, every series pages only over its own window (swap candles over the longest feature reach, index and
mark only over the spread features' reach, funding over the funding features'), and a short catch-up closes the
minutes that passed meanwhile. While the season is not admitting entries (recovery, closing, draining, paused) the
book does no work at all, so V1/V2 participants and close-only recovery are never delayed by it. After the warm-up
the confirmed candles of each snapshot feed the book, and index, mark and funding of the last minutes are read once
per new minute, whether the read succeeds or not.

GET only, four fixed public routes of BTC-USDT-SWAP and its index, through the wrapped feed's client, on a request
budget of its own (match_protocol.capacities: max_book_gets) that never draws on the base feed's allowance. Until the
warm-up has finished the snapshot carries no book, so the entry is UNKNOWN, never a guess. A failed warm-up starts
again from scratch RETRY_SECONDS after the failure, at most WARMUP_ATTEMPTS times a season.
"""
import time

from . import strategy_v4 as v4
from .strategy_v4_book import BookError, FeatureBook, SPREADS, warmup_ms
from .strategy_v4_runtime import VISIBILITY_SECONDS

def routes(instrument="BTC-USDT-SWAP"):
    index = instrument.rsplit("-", 1)[0]  # BTC-USDT-SWAP -> the BTC-USDT index
    return {
        "swap": ("/api/v5/market/history-candles", {"instId": instrument, "bar": "1m"}),
        "index": ("/api/v5/market/history-index-candles", {"instId": index, "bar": "1m"}),
        "mark": ("/api/v5/market/history-mark-price-candles", {"instId": instrument, "bar": "1m"}),
        "funding": ("/api/v5/public/funding-rate-history", {"instId": instrument}),
    }


ROUTES = routes()
PATHS = frozenset(path for path, _ in ROUTES.values())
PAGE = 100
MAX_SERIES_PAGES = 1400  # 90 days of 1m candles is 1296 pages of 100
PAGES_PER_SNAPSHOT = 2
SNAPSHOT_SECONDS = 1.5
RETRY_SECONDS = 30
RECENT = 5  # index/mark/funding rows read per new minute
WARMUP_ATTEMPTS = 3


def specs_of(strategies):
    """Every feature the season's schema-4 recipes read (empty without such a recipe)."""
    found = {}
    for recipe in strategies:
        if recipe.get("strategy_schema_version") == 4:
            found.update(v4.required_features(recipe["rules"]))
    return list(found.values())


def windows(specs):
    """{series: history window in ms} the book needs: swap over every feature, index/mark/funding over their own."""
    clean = [v4.validate_feature(spec) for spec in specs]
    out = {"swap": max((warmup_ms(s) for s in clean), default=0)}
    for kind, names in (("index", set(SPREADS)), ("mark", {"basis_mark_bps"}), ("funding", {"funding_settled_bps"})):
        reach = [warmup_ms(s) for s in clean if s["name"] in names]
        if reach:
            out[kind] = max(reach)
    return out


def series_pages(kind, window):
    span = 8 * 3_600_000 if kind == "funding" else v4.MINUTE  # one row per settlement or per minute
    return -(-window // (PAGE * span)) + 2


def request_budget(strategies, seconds):
    """Public GETs the book may use in a season of `seconds` (admission + hold tail): every warm-up attempt pages each
    series over its own window plus a catch-up, then index, mark and funding once per minute."""
    specs = specs_of(strategies)
    if not specs:
        return 0
    plan = windows(specs)
    pages = sum(series_pages(kind, window) + 2 for kind, window in plan.items())
    return WARMUP_ATTEMPTS * pages + (len(plan) - 1) * (-(-seconds // 60) + 2) + 16


class BookFeed:
    OWN = frozenset({"base", "get_public", "specs", "needs", "book", "warm", "status", "gets", "budget", "retry_at",
                     "funding_checked", "attempts", "admitting", "job", "monotonic", "failures", "degraded", "routes"})

    def __init__(self, base, get, specs, budget=None, admitting=None, monotonic=time.monotonic, instrument="BTC-USDT-SWAP"):
        """base: the engine feed; get(path, params) -> rows of one public GET; specs: features of all v4 recipes;
        budget: the book's own GET limit (None = no limit, tests); admitting(): True while the season admits entries."""
        self.base, self.get_public, self.specs = base, get, list(specs)
        plan = windows(self.specs)
        if any(series_pages(kind, window) > MAX_SERIES_PAGES for kind, window in plan.items()):
            raise BookError("warmup_beyond_the_page_cap")
        self.needs = {kind: kind in plan for kind in ("index", "mark", "funding")}
        self.book, self.warm, self.status, self.gets, self.budget = FeatureBook(self.specs), False, "warming", 0, budget
        self.retry_at, self.funding_checked, self.attempts, self.job, self.failures = None, None, 0, None, {}
        self.degraded = set()  # series whose last per-minute read failed
        self.admitting, self.monotonic = admitting or (lambda: True), monotonic
        self.routes = routes(instrument)

    def __getattr__(self, name):  # calls, max_calls, instrument, stage... belong to the wrapped feed
        return getattr(self.base, name)

    def __setattr__(self, name, value):  # the engines set max_calls on the feed: it must reach the wrapped one
        object.__setattr__(self, name, value) if name in self.OWN else setattr(self.base, name, value)

    # ----------------------------------------------------------------------------------------------------------- reads
    def fetch(self, kind, **extra):
        if self.budget is not None and self.gets >= self.budget:
            raise BookError("book_request_budget")
        self.gets += 1
        path, params = self.routes[kind]
        data = self.get_public(path, {**params, **{k: str(v) for k, v in extra.items()}})
        if not isinstance(data, list):
            raise BookError("public_history_shape")
        return data

    def page(self, task):
        """One backward page of a warm-up task {kind, start, end, after, rows, pages, done}."""
        task["pages"] += 1
        if task["pages"] > MAX_SERIES_PAGES:
            raise BookError("warmup_page_limit")
        if task["kind"] == "funding":
            data = [r for r in self.fetch("funding", after=task["after"], limit=PAGE)
                    if isinstance(r, dict) and str(r.get("fundingTime", "")).isdigit()]
            stamps = [int(r["fundingTime"]) for r in data]
        else:
            data = [r for r in self.fetch(task["kind"], after=task["after"], limit=PAGE)
                    if isinstance(r, list) and len(r) >= 6 and str(r[0]).isdigit() and r[-1] == "1"]
            stamps = [int(r[0]) for r in data]
        for row, stamp in zip(data, stamps):
            if task["start"] <= stamp < task["end"]:
                task["rows"][stamp] = row
        oldest = min(stamps, default=None)
        if oldest is None or oldest >= task["after"] or oldest <= task["start"]:
            task["done"] = True
        else:
            task["after"] = oldest

    # ---------------------------------------------------------------------------------------------------------- inputs
    def add_rows(self, swap, index=(), mark=()):
        for kind, rows in (("index", index), ("mark", mark)):
            for row in rows:
                if isinstance(row, list) and len(row) >= 6 and str(row[0]).isdigit() and row[-1] == "1":
                    self.book.add_secondary(kind, int(row[0]), float(row[4]))
        for row in swap:
            self.book.add_minute((int(row[0]), row[1], row[2], row[3], row[4], row[5]))

    def start_job(self, now_ms):
        end = (now_ms - VISIBILITY_SECONDS * 1000) // v4.MINUTE * v4.MINUTE  # opens before end are visible minutes
        tasks = []
        for kind, window in windows(self.specs).items():
            stop = now_ms + 1 if kind == "funding" else end
            tasks.append({"kind": kind, "start": end - window, "end": stop, "after": stop, "rows": {}, "pages": 0, "done": False})
        self.job = {"end": end, "tasks": tasks}

    def advance_job(self, now_ms):
        """Read up to PAGES_PER_SNAPSHOT pages; True once the history and its catch-up are complete and in the book."""
        started, pages = self.monotonic(), 0
        while pages < PAGES_PER_SNAPSHOT and self.monotonic() - started < SNAPSHOT_SECONDS:
            task = next((t for t in self.job["tasks"] if not t["done"]), None)
            if task is None:
                end = (now_ms - VISIBILITY_SECONDS * 1000) // v4.MINUTE * v4.MINUTE
                if end - self.job["end"] > 20 * v4.MINUTE:  # minutes went by while paging: close them before the book
                    for t in self.job["tasks"]:
                        if t["kind"] != "funding":
                            t.update(start=self.job["end"], end=end, after=end, done=False)
                    self.job["end"] = end
                    continue
                self.finish_job(now_ms)
                return True
            self.page(task)
            pages += 1
        return False

    def finish_job(self, now_ms):
        rows = {t["kind"]: t["rows"] for t in self.job["tasks"]}
        for at, row in sorted(rows.get("funding", {}).items()):
            self.book.add_settlement(at, row.get("realizedRate") or None)
        self.funding_checked = now_ms
        self.add_rows([rows["swap"][k] for k in sorted(rows["swap"])],
                      [rows.get("index", {})[k] for k in sorted(rows.get("index", {}))],
                      [rows.get("mark", {})[k] for k in sorted(rows.get("mark", {}))])
        self.job = None

    def recent(self, kind):
        """One read of the last rows of a series; a failure is counted and the minute goes on without them."""
        try:
            rows = self.fetch(kind, limit=RECENT)
        except Exception:  # noqa: BLE001 - a missing index/mark/funding row only makes those features UNKNOWN
            self.failures[kind] = self.failures.get(kind, 0) + 1
            self.degraded.add(kind)
            return []
        self.degraded.discard(kind)
        return rows

    def update(self, snapshot, now_ms):
        visible = sorted((r for r in snapshot["raw"]["candles"] if r[8] == "1" and int(r[0]) + v4.MINUTE + VISIBILITY_SECONDS * 1000 <= now_ms),
                         key=lambda r: int(r[0]))
        fresh = [r for r in visible if self.book.last is None or int(r[0]) > self.book.last[0]]
        if not fresh:
            return
        index = self.recent("index") if self.needs["index"] else []
        mark = self.recent("mark") if self.needs["mark"] else []
        if self.needs["funding"] and (self.funding_checked is None or now_ms - self.funding_checked >= v4.MINUTE):
            self.funding_checked = now_ms
            for row in self.recent("funding"):
                if isinstance(row, dict) and str(row.get("fundingTime", "")).isdigit():
                    self.book.add_settlement(int(row["fundingTime"]), row.get("realizedRate") or None)
        self.add_rows(fresh, index, mark)

    # -------------------------------------------------------------------------------------------------------- snapshot
    def snapshot(self):
        snapshot = self.base.snapshot()
        if not self.admitting():  # recovery, closing, draining or paused: no new entry, so the book does nothing
            return snapshot
        now_ms = round(snapshot["at"] * 1000)
        try:
            if not self.warm:
                if (self.retry_at is not None and now_ms < self.retry_at) or self.attempts >= WARMUP_ATTEMPTS:
                    return snapshot
                if self.job is None:
                    self.book, self.attempts = FeatureBook(self.specs), self.attempts + 1
                    self.start_job(now_ms)
                if not self.advance_job(now_ms):
                    self.status = "warming"
                    return snapshot
                self.warm = True
            self.update(snapshot, now_ms)
            self.status = "degraded: " + ", ".join(sorted(self.degraded)) if self.degraded else "ready"
        except Exception as error:  # noqa: BLE001 - a failed warm-up leaves the snapshot without a book (UNKNOWN)
            self.status = "unavailable: " + type(error).__name__
            if not self.warm:
                self.job, self.retry_at = None, now_ms + RETRY_SECONDS * 1000
            return snapshot
        current = self.book.book()
        if current is not None:
            snapshot["v4_features"] = current
        return snapshot
