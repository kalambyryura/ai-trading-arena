"""Demo and Live feature book (schema 4): a hole of more than one minute is read from public history, not left open.

strategy_v4_book fills exactly one missing minute flat at the previous close, and a snapshot carries about 30 closed
minutes. After a longer stretch without minutes (a market feed outage, or a time without admission, when
strategy_v4_feed.BookFeed does no work) the newer minutes would join the book behind a hole, and every feature that
reaches over it would stay UNKNOWN for its whole reach (rms over 168 hours: 7 days; SMA20 of daily closes: 20 days)
without a word. GapBookFeed first reads the missing swap minutes (and index and mark closes when a spread feature needs
them) through the same four public routes, on the book's own GET budget, at most PAGES_PER_SNAPSHOT pages a snapshot
like the warm-up. Until the hole is read the book stays behind the clock, so no decision reads it
(strategy_v4_runtime.book). A failed read is retried RETRY_SECONDS later, GAP_ATTEMPTS times; minutes the exchange
history does not have, or a hole given up, stay open and book_state() says so. A single missing minute is read too
(R2.1.6: at the edge of the snapshot's window it is no hole at all); one the history lacks is filled flat by the book,
as the evaluator does, without an event. After more than RECENT new minutes, index and mark closes are read for all of
them, not only the last RECENT rows of a minute's read (R2.1.6).

book_state() is the panel's and the journal's view of the book: warming (with pages read), ready, degraded,
unavailable or exhausted (every warm-up attempt failed: no schema-4 decision for the rest of the season).

PAPER keeps strategy_v4_feed.BookFeed. This module is in the exchange engine hash only, so the training and arena
hashes, and the qualifications pinned on them, stay as they are.
"""
from . import strategy_v4 as v4
from .strategy_v4_feed import BookFeed, PAGES_PER_SNAPSHOT, RECENT, RETRY_SECONDS, SNAPSHOT_SECONDS, WARMUP_ATTEMPTS, series_pages, windows
from .strategy_v4_runtime import VISIBILITY_SECONDS

GAP_ATTEMPTS = 3  # failed reads of one hole before the newer minutes join the book with the hole left open
SECONDARY_RETRY_SECONDS = 10  # R2.1.7: a failed index/mark catch-up is tried again this much later, GAP_ATTEMPTS times


class GapBookFeed(BookFeed):
    OWN = BookFeed.OWN | {"gap", "gaps", "last_gap", "read_until", "index_until", "mark_until", "secondary_tries", "secondary_retry_at"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.gap, self.gaps, self.last_gap = None, 0, None  # the hole being read, holes reported, the last reported one
        # end of the last hole read; R2.1.11 (R219-BOOK-SPREAD): index and mark each read up to their own open, so one
        # finished series is never read again for the other
        self.read_until, self.index_until, self.mark_until = None, None, None
        self.secondary_tries, self.secondary_retry_at = 0, None  # failed catch-ups of the current stretch, next try

    def open_gap(self, snapshot, now_ms):
        """A read job for the minutes between the book and the snapshot's first newer visible minute, when any minute is
        missing; None otherwise."""
        last = self.book.last
        if last is None:
            return None
        newer = [int(r[0]) for r in snapshot["raw"]["candles"] if r[8] == "1" and int(r[0]) > last[0]
                 and int(r[0]) + v4.MINUTE + VISIBILITY_SECONDS * 1000 <= now_ms]
        if not newer or min(newer) <= last[0] + v4.MINUTE:
            return None
        start, first, newest = last[0] + v4.MINUTE, min(newer), max(newer) + v4.MINUTE
        # Index and mark closes over the snapshot's own minutes too: a minute's read gives only the last RECENT rows.
        tasks = [{"kind": "swap", "start": start, "end": first}] + [
            {"kind": kind, "start": start, "end": newest} for kind in ("index", "mark") if self.needs[kind]]
        return {"start": start, "end": first, "failures": 0, "retry_at": None,
                "tasks": [{**task, "after": task["end"], "rows": {}, "pages": 0, "done": False} for task in tasks]}

    def read_gap(self, now_ms):
        """Up to PAGES_PER_SNAPSHOT pages of the hole; once it is read (or given up) its minutes join the book."""
        gap = self.gap
        if gap["retry_at"] is not None and now_ms < gap["retry_at"]:
            return
        started, pages, gave_up = self.monotonic(), 0, False
        try:
            while pages < PAGES_PER_SNAPSHOT and self.monotonic() - started < SNAPSHOT_SECONDS:
                task = next((t for t in gap["tasks"] if not t["done"]), None)
                if task is None:
                    break
                self.page(task)
                pages += 1
        except Exception:
            gap["failures"] += 1
            if gap["end"] - gap["start"] <= v4.MINUTE:
                gave_up = True  # R2.1.7 (BOOK-1R): one minute is filled flat at once, as before; retries let the window slide
            elif gap["failures"] < GAP_ATTEMPTS:
                gap["retry_at"] = now_ms + RETRY_SECONDS * 1000
                raise  # BookFeed.snapshot reports the book unavailable until the retry
            else:
                gave_up = True
        if not gave_up and any(not t["done"] for t in gap["tasks"]):
            return
        rows = {t["kind"]: t["rows"] for t in gap["tasks"]}
        swap = [rows["swap"][k] for k in sorted(rows["swap"])]
        flat = []
        last = self.book.last
        if gave_up and not swap and gap["end"] - gap["start"] == v4.MINUTE and last is not None and last[0] + v4.MINUTE == gap["start"]:
            # R2.1.8 (R217-BOOK1R-SLIDE): the lone minute is filled flat now, as strategy_v4_book fills one, before the
            # snapshot's window can slide and turn it into a two-minute hole the book never fills
            flat = [[str(gap["start"]), last[4], last[4], last[4], last[4], 0.0]]
        self.add_rows(swap + flat, [rows["index"][k] for k in sorted(rows.get("index", {}))],
                      [rows["mark"][k] for k in sorted(rows.get("mark", {}))])
        minutes = (gap["end"] - gap["start"]) // v4.MINUTE
        self.gap, self.read_until = None, gap["end"]
        # index and mark count as read only where their reads finished (a given-up hole leaves them to the catch-up)
        for kind in ("index", "mark"):
            setattr(self, kind + "_until", max([t["end"] for t in gap["tasks"] if t["kind"] == kind and t["done"]]
                                               + [getattr(self, kind + "_until") or 0]))
        unread = [t["kind"] for t in gap["tasks"] if t["kind"] != "swap" and not t["done"]]
        if minutes > 1 or swap or gave_up:  # a lone minute the history lacks is no outage: filled flat, no event
            self.gaps += 1
            self.last_gap = {"start_ms": gap["start"], "end_ms": gap["end"], "minutes": minutes, "read": len(swap),
                             "missing": minutes - len(swap), "flat": len(flat), "failures": gap["failures"], "gave_up": gave_up,
                             "flat_filled": bool(flat),  # R2.1.9 (R218-BOOK-EV): a flat minute, not UNKNOWN features
                             "secondary_unread": unread}  # R2.1.11: index/mark of the hole left to the catch-up
            # R2.1.10 (R219-GAP-MISSING): missing counts the minutes the history lacks, as before R2.1.9; flat, the ones of
            # them filled flat
        self.degraded.discard("gap")

    def catch_up_secondary(self, snapshot, now_ms):
        """Index and mark closes of every new minute when more than RECENT came at once (after a short outage): one page
        each. "read" when they are in the book (the minutes join on the next snapshot), "retry" while a failed read waits
        SECONDARY_RETRY_SECONDS (R2.1.7, BOOK-4R: GAP_ATTEMPTS tries, the minutes held meanwhile), None when there is
        nothing to read or the tries are spent (those spread features UNKNOWN, the series degraded)."""
        last = self.book.last
        fresh = sorted(int(r[0]) for r in snapshot["raw"]["candles"] if r[8] == "1" and (last is None or int(r[0]) > last[0])
                       and int(r[0]) + v4.MINUTE + VISIBILITY_SECONDS * 1000 <= now_ms)
        kinds = [k for k in ("index", "mark") if self.needs[k] and fresh and fresh[-1] + v4.MINUTE > (getattr(self, k + "_until") or 0)]
        if len(fresh) <= RECENT or not kinds:
            return None
        if self.secondary_retry_at is not None and now_ms < self.secondary_retry_at:
            return "retry"
        failed = []
        for kind in kinds:
            task = {"kind": kind, "start": fresh[0], "end": fresh[-1] + v4.MINUTE, "after": fresh[-1] + v4.MINUTE,
                    "rows": {}, "pages": 0, "done": False}
            try:
                while not task["done"] and task["pages"] < 2:
                    self.page(task)
            except Exception:  # noqa: BLE001 - only those spread features become UNKNOWN
                self.failures[kind] = self.failures.get(kind, 0) + 1
                failed.append(kind)
                continue
            for stamp, row in task["rows"].items():
                self.book.add_secondary(kind, stamp, float(row[4]))
            setattr(self, kind + "_until", fresh[-1] + v4.MINUTE)  # this series is read: a retry reads only the other
        if failed:
            self.secondary_tries += 1
            if self.secondary_tries < GAP_ATTEMPTS:
                self.secondary_retry_at = now_ms + SECONDARY_RETRY_SECONDS * 1000
                return "retry"
            self.degraded.update(failed)  # given up: the book says degraded, not ready
        for kind in failed:  # given up: those spread features UNKNOWN, the series degraded
            setattr(self, kind + "_until", fresh[-1] + v4.MINUTE)
        self.secondary_tries, self.secondary_retry_at = 0, None
        return "read" if not failed else None

    def update(self, snapshot, now_ms):
        if self.gap is None:
            gap = self.open_gap(snapshot, now_ms)
            # A hole that starts inside one already read is a stretch the exchange history lacks (or a hole given up):
            # the newer minutes join the book with it left open. A hole after it is a new one.
            if gap is not None and not (self.read_until and gap["start"] < self.read_until):
                self.gap = gap
        if self.gap is not None:
            if self.gap["end"] - self.gap["start"] > v4.MINUTE:
                self.degraded.add("gap")
            self.read_gap(now_ms)
            return  # while the hole is read, and on the snapshot that finishes it, the book stays a minute behind
        if (self.needs["index"] or self.needs["mark"]) and self.catch_up_secondary(snapshot, now_ms):
            return  # its own snapshot (or a wait for its retry): the minutes join on the next one, with their closes
        super().update(snapshot, now_ms)

    def book_state(self):
        """{status, detail, ...}: the book's warm-up, holes and request use, with no account data."""
        status = self.status.split(":", 1)[0]
        if not self.warm and self.job is None and self.attempts >= WARMUP_ATTEMPTS and status == "unavailable":
            status = "exhausted"
        gap = self.gap if self.gap is not None and self.gap["end"] - self.gap["start"] > v4.MINUTE else None
        return {"status": status, "detail": self.status, "warm": self.warm, "attempts": self.attempts,
                "warmup_attempts": WARMUP_ATTEMPTS, "gets": self.gets, "budget": self.budget,
                "warmup_pages": sum(t["pages"] for t in self.job["tasks"]) if self.job else None,
                "warmup_pages_expected": sum(series_pages(kind, window) for kind, window in windows(self.specs).items()),
                "bar_close_ms": self.book.last[0] + v4.MINUTE if self.book.last is not None else None,
                "gap": None if gap is None else {"start_ms": gap["start"], "end_ms": gap["end"],
                                                  "minutes": (gap["end"] - gap["start"]) // v4.MINUTE, "failures": gap["failures"]},
                "gaps_read": self.gaps, "last_gap": self.last_gap,
                "spread": bool(self.needs["index"] or self.needs["mark"])}  # R2.1.11: the recipe has spread features
