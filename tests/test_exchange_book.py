"""Demo/Live feature book: a hole of more than one minute is read from public history (exchange_book.GapBookFeed), its
state reaches the panel and the journal. The fake public transport of test_strategy_v4_feed only: no network, account
route, key, order or paid call."""
import json
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_strategy_v4_book import SPECS, filled, market
from test_strategy_v4_feed import BaseFeed, FakeOkx, MIN, T0
from trading_lab import battle_review, exchange_book, exchange_lab, strategy_v4 as v4, strategy_v4_feed as feedmod
from trading_lab import strategy_v4_runtime as v4rt
from trading_lab.exchange_book import GapBookFeed


class GapBookFeedTests(unittest.TestCase):
    def setUp(self):
        self.rows, self.index, self.mark, self.funding = market(4, seed=21)
        self.okx = FakeOkx(self.rows, self.index, self.mark, self.funding)
        self.reference = filled(self.rows)
        self.events, self.state = [], {"events": [], "status": "RUNNING"}

    def feed(self, kind=GapBookFeed):
        return kind(BaseFeed(self.okx), self.okx.get, SPECS)

    def expected(self, close_ms):
        minutes = [r for r in self.reference if r[0] + MIN <= close_ms]
        secondary = {"index": self.index, "mark": self.mark, "funding": self.funding}
        return {v4.feature_key(s): v4.feature_value(s, minutes, secondary) for s in SPECS}

    def at(self, feed, ms):
        self.okx.now_ms = ms
        before = len(self.okx.calls)
        snapshot = feed.snapshot()
        self.assertLessEqual(len(self.okx.calls) - before, 3)  # a hole is read a few pages a snapshot: never a stall
        exchange_lab.ExchangeLab.note_feature_book(types.SimpleNamespace(clock=lambda: ms / 1000), self.state, feed)
        return snapshot

    def warm(self, feed, start):
        for k in range(0, 600, 5):
            if "v4_features" in self.at(feed, start + k * 1000):
                return start + k * 1000
        self.fail("no book after 10 minutes")

    def history_reads(self, since, kind="swap"):
        return sum(1 for k, params in self.okx.calls[since:] if k == kind and "after" in params)

    def run_after_outage(self, feed, resume, seconds=240):
        """Snapshots every 5 s from `resume` on; the book is never read by a decision while a hole is open."""
        decided = []
        for k in range(0, seconds, 5):
            snapshot = self.at(feed, resume + k * 1000)
            if feed.gap is not None:
                self.assertIsNone(v4rt.book(snapshot))  # a minute behind the clock: no decision reads it
            current = v4rt.book(snapshot)
            if current is not None and (snapshot["at"] * 1000 - current["bar_close_ms"]) // 1000 == 5:
                decided.append(current)
        return decided

    def test_an_outage_longer_than_the_snapshot_is_read_from_history_and_the_values_match_the_reference(self):
        start = T0 + 3 * 86_400_000 + 7 * 3_600_000
        plain, gaps = self.feed(feedmod.BookFeed), self.feed()
        ready = max(self.warm(plain, start), self.warm(gaps, start))
        resume = ready + 50 * MIN  # 50 minutes without a snapshot: the next one carries only the last 30 closed minutes
        calls = len(self.okx.calls)
        fixed = self.run_after_outage(gaps, resume)
        self.assertTrue(fixed)
        for book in fixed:
            self.assertEqual(book["values"], self.expected(book["bar_close_ms"]), book["bar_close_ms"])
        self.assertGreater(self.history_reads(calls), 0)
        state = gaps.book_state()
        self.assertEqual((state["status"], state["gaps_read"], state["gap"]), ("ready", 1, None))
        self.assertEqual(state["last_gap"]["read"] + state["last_gap"]["missing"], state["last_gap"]["minutes"])
        self.assertFalse(state["last_gap"]["gave_up"])
        # Without the read, the same outage leaves the long features UNKNOWN for their whole reach.
        self.okx.now_ms = resume
        for k in range(0, 240, 5):
            self.okx.now_ms = resume + k * 1000
            snapshot = plain.snapshot()
        book = snapshot["v4_features"]
        self.assertNotEqual(book["values"], self.expected(book["bar_close_ms"]))
        self.assertIsNone(book["values"]["rms_bar_bps@5m:12"])  # 12 five-minute bars reach over the hole
        self.assertIsNotNone(self.expected(book["bar_close_ms"])["rms_bar_bps@5m:12"])

    def test_a_failing_hole_read_is_retried_then_given_up_and_never_read_again(self):
        feed = self.feed()
        ready = self.warm(feed, T0 + 3 * 86_400_000 + 7 * 3_600_000)
        self.okx.fail.add("swap")
        calls = len(self.okx.calls)
        decided = self.run_after_outage(feed, ready + 50 * MIN, seconds=exchange_book.GAP_ATTEMPTS * feedmod.RETRY_SECONDS + 60)
        self.assertEqual(self.history_reads(calls), exchange_book.GAP_ATTEMPTS)  # one page per try, RETRY_SECONDS apart
        state = feed.book_state()
        self.assertTrue(state["last_gap"]["gave_up"])
        self.assertEqual(state["status"], "ready")
        self.assertTrue(decided)  # the newer minutes joined the book with the hole left open
        self.assertIsNone(decided[-1]["values"]["rms_bar_bps@5m:12"])
        self.assertIsNotNone(self.expected(decided[-1]["bar_close_ms"])["rms_bar_bps@5m:12"])
        self.okx.fail.clear()
        later = len(self.okx.calls)
        self.run_after_outage(feed, ready + 50 * MIN + 300_000, seconds=120)
        self.assertEqual(self.history_reads(later), 0)
        events = [e for e in self.state["events"] if e["code"] == "feature_book"]
        self.assertIn("Пропуск не вдалося дочитати", events[-1]["message"])
        self.assertTrue(any(e["data"]["book_status"] == "unavailable" for e in events))

    def test_minutes_the_exchange_history_lacks_stay_a_hole_and_are_not_read_again(self):
        start = T0 + 3 * 86_400_000 + 7 * 3_600_000
        feed = self.feed()
        ready = self.warm(feed, start)
        resume = ready + 60 * MIN
        lost = {r[0] for r in self.rows if ready + 5 * MIN <= r[0] < ready + 15 * MIN}
        self.okx.rows = [r for r in self.rows if r[0] not in lost]
        self.reference = filled(self.okx.rows)
        calls = len(self.okx.calls)
        decided = self.run_after_outage(feed, resume)
        for book in decided:
            self.assertEqual(book["values"], self.expected(book["bar_close_ms"]))  # the same hole the reference has
        state = feed.book_state()
        self.assertGreaterEqual(state["last_gap"]["missing"], len(lost))
        reads = self.history_reads(calls)
        self.run_after_outage(feed, resume + 300_000, seconds=120)
        self.assertEqual(self.history_reads(calls), reads)  # the stretch is not paged again and again
        message = [e for e in self.state["events"] if e["code"] == "feature_book"][-1]["message"]
        self.assertIn("немає в історії біржі", message)

    def test_book_state_and_events_follow_the_warm_up_without_an_event_per_page(self):
        feed = self.feed()
        self.okx.now_ms = T0 + 3 * 86_400_000
        self.at(feed, self.okx.now_ms)
        state = feed.book_state()
        self.assertEqual(state["status"], "warming")
        self.assertGreater(state["warmup_pages_expected"], 0)
        self.assertGreater(state["warmup_pages"], 0)
        ready = self.warm(feed, self.okx.now_ms + 5000)
        self.at(feed, ready + 5000)
        events = [e for e in self.state["events"] if e["code"] == "feature_book"]
        self.assertEqual([e["data"]["book_status"] for e in events], ["warming", "ready"])
        self.assertIn("прогрівається", events[0]["message"])
        self.assertEqual(self.state["feature_book"]["status"], "ready")
        self.assertEqual(self.state["feature_book"]["warmup_attempts"], feedmod.WARMUP_ATTEMPTS)
        # The review keeps the book fields (allowlisted values only).
        kept = battle_review.safe_data(events[-1]["data"])
        self.assertEqual(kept["book_status"], "ready")
        self.assertIn("book_gets", kept)

    def test_every_warm_up_failing_is_exhausted_and_said_once(self):
        feed = self.feed()
        self.okx.fail.add("swap")
        now = T0 + 3 * 86_400_000
        for k in range(0, (feedmod.WARMUP_ATTEMPTS + 1) * feedmod.RETRY_SECONDS + 30, 5):
            self.at(feed, now + k * 1000)
        state = feed.book_state()
        self.assertEqual((state["status"], state["attempts"]), ("exhausted", feedmod.WARMUP_ATTEMPTS))
        events = [e for e in self.state["events"] if e["code"] == "feature_book"]
        self.assertEqual([e["data"]["book_status"] for e in events].count("exhausted"), 1)
        self.assertIn("до кінця раунду", events[-1]["message"])

    # --- R2.1.7 (TEST-BOOK of the R2.1.6 review): each fails if BOOK-1..4 or BOOK-1R/4R is reverted ---------------------
    def edge_minute(self, feed):
        """Warm the book, then (no snapshots meanwhile) move to the moment the snapshot's 30 closed minutes begin one minute
        after the book's last: exactly one minute is missing, at the edge of the window. (that minute, its row)"""
        ready = self.warm(feed, T0 + 3 * 86_400_000 + 7 * 3_600_000)
        have = {r[0]: r for r in self.rows}
        last = feed.book.last[0]
        while not all(last + k * MIN in have for k in range(1, 33)):  # a stretch with no hole of the data itself
            ready += MIN
            for k in range(0, 60, 5):
                self.at(feed, ready + k * 1000)
            last = feed.book.last[0]
        return ready, last + MIN, have[last + MIN]

    def one_minute(self, feed, start):
        group = next((g for g in feed.book.groups["1m"] if g[0] == start), None)
        return None if group is None else tuple(group[1:5])

    def test_book1_a_missing_minute_at_the_window_edge_is_read_from_history_not_filled_flat(self):
        feed = self.feed()
        ready, missing, row = self.edge_minute(feed)
        jump = missing + 31 * MIN + 5000  # the snapshot's closed minutes are missing + 1 .. missing + 30
        for k in range(0, 30, 5):
            self.at(feed, jump + k * 1000)
        self.assertEqual(self.one_minute(feed, missing), tuple(float(x) for x in row[1:5]))  # the real minute, not flat
        read = [e["data"] for e in self.state["events"] if e["code"] == "feature_book" and e["data"].get("gap_minutes") == 1]
        self.assertEqual([(d["gap_read"], d["gap_missing"]) for d in read], [(1, 0)])  # an outage of ~31 min: said once

    def test_book1r_a_lone_edge_minute_whose_read_fails_is_filled_flat_at_once(self):
        feed = self.feed()
        ready, missing, row = self.edge_minute(feed)
        previous = feed.book.last[4]
        self.okx.fail.add("swap")
        jump = missing + 31 * MIN + 5000
        for k in range(0, 20, 5):  # four snapshots: the given-up read, the index/mark catch-up, the minutes
            self.at(feed, jump + k * 1000)
        flat = (previous,) * 4
        self.assertEqual(self.one_minute(feed, missing), flat)  # as R2.1.5 did, no 90 s of retries while the window slides
        said = [e["message"] for e in self.state["events"] if e["code"] == "feature_book" and e["data"].get("gap_minutes") == 1]
        self.assertTrue(said and "пласко" in said[-1] and "Цінові ознаки рахуються далі" in said[-1])  # R2.1.9 (BOOK-EV)
        # R2.1.11 (R219-BOOK-SPREAD): a flat minute has no volume: spread features over it UNKNOWN, as the evaluator has them
        self.assertTrue(feed.book_state()["spread"])
        self.assertIn("Spread-ознаки, що охоплюють цю хвилину, UNKNOWN на весь свій період", said[-1])
        self.assertTrue(feed.book_state()["last_gap"]["flat_filled"])
        # R2.1.10 (R219-GAP-MISSING): missing counts the minute the history lacks, as before R2.1.9; flat, the one filled flat
        self.assertEqual((feed.book_state()["last_gap"]["missing"], feed.book_state()["last_gap"]["flat"]), (1, 1))
        event = [e for e in self.state["events"] if e["code"] == "feature_book" and e["data"].get("gap_minutes") == 1][-1]
        self.assertEqual((event["data"]["gap_missing"], event["data"]["gap_flat"]), (1, 1))
        self.assertGreaterEqual(feed.book.last[0], missing + 30 * MIN)  # the newer minutes joined within seconds

    def test_book1r_slide_a_flat_edge_minute_stays_one_minute_when_the_window_slides(self):
        feed = self.feed()
        ready, missing, row = self.edge_minute(feed)
        previous = feed.book.last[4]
        self.okx.fail.add("swap")
        jump = missing + 31 * MIN + 5000
        self.at(feed, jump)  # the lone minute's read fails: given up and filled flat at once
        self.assertEqual(self.one_minute(feed, missing), (previous,) * 4)
        self.okx.fail.clear()
        for k in range(0, 30, 5):  # a minute later the window has slid by one
            self.at(feed, jump + MIN + k * 1000)
        nxt = next(r for r in self.rows if r[0] == missing + MIN)
        self.assertEqual(self.one_minute(feed, missing + MIN), tuple(float(x) for x in nxt[1:5]))  # read, not a 2-minute hole

    def test_book_sec_a_failed_index_page_during_the_edge_read_is_caught_up(self):
        # R2.1.9 (R218-TEST-BOOK-SEC): only finished index/mark reads move secondary_until
        feed = self.feed()
        ready, missing, row = self.edge_minute(feed)
        jump = missing + 31 * MIN + 5000
        self.okx.fail.add("index")
        self.at(feed, jump)  # the swap page reads, the index page fails: the lone minute is given up with its swap row
        self.okx.fail.clear()
        said = [e["message"] for e in self.state["events"] if e["code"] == "feature_book" and e["data"].get("gap_minutes") == 1]
        self.assertIn("Свічки пропуску дочитано", said[-1])  # R2.1.11 (R219-BOOK-SPREAD): not "UNKNOWN for the whole period"
        self.assertEqual(feed.book_state()["last_gap"]["secondary_unread"], ["index", "mark"])  # mark never reached
        decided = self.run_after_outage(feed, jump + 5000, seconds=7200)
        self.assertGreater(len(decided), 100)
        for book in decided:
            self.assertEqual(book["values"], self.expected(book["bar_close_ms"]), book["bar_close_ms"])

    def test_book2_the_book_line_shows_only_while_entries_are_admitted(self):
        import subprocess
        script = ("const ui=require('./web/arena-lifecycle.js');"
                  "const doc={createElement:t=>({tag:t,className:'',textContent:'',children:[],dataset:{},append(...c){this.children.push(...c);},setAttribute(){}})};"
                  "global.document=doc;"
                  "const count=p=>{const v=ui.renderLifecycle({bots:[]},{phase:p,featureBook:{status:'ready',last_gap:null}});"
                  "return v.children.filter(c=>String(c.className).includes('v6-book-note')).length;};"
                  "console.log(JSON.stringify(['RUNNING','DRAINING','PAUSED','DONE'].map(count)));")
        out = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(out), [1, 0, 0, 0])

    def test_book3_no_book_event_while_the_season_does_no_book_work(self):
        feed = self.feed()
        for status, extra in (("DRAINING", {}), ("CLOSING", {}), ("RUNNING", {"_recovery": True}), ("RUNNING", {"admission_blocked": "clock_gap"})):
            state = {"events": [], "status": status, **extra}
            exchange_lab.ExchangeLab.note_feature_book(types.SimpleNamespace(clock=lambda: 1.0), state, feed)
            self.assertEqual(state["events"], [], (status, extra))
        state = {"events": [], "status": "RUNNING"}
        exchange_lab.ExchangeLab.note_feature_book(types.SimpleNamespace(clock=lambda: 1.0), state, feed)
        self.assertEqual([e["data"]["book_status"] for e in state["events"]], ["warming"])

    def test_book4_after_a_short_outage_the_spread_features_get_every_minutes_closes(self):
        feed = self.feed()
        ready = self.warm(feed, T0 + 3 * 86_400_000 + 7 * 3_600_000)
        decided = self.run_after_outage(feed, ready + 10 * MIN, seconds=120)  # inside the 30-minute window: no swap hole
        self.assertTrue(decided)
        for book in decided:
            self.assertEqual(book["values"], self.expected(book["bar_close_ms"]))  # premium over 30 minutes included
        self.assertTrue(any(book["values"]["premium_bps@1m:30"] is not None for book in decided))

    def test_book4r_a_failed_catch_up_is_tried_again(self):
        feed = self.feed()
        ready = self.warm(feed, T0 + 3 * 86_400_000 + 7 * 3_600_000)
        self.okx.fail.add("index")
        self.at(feed, ready + 10 * MIN)  # the catch-up fails once
        self.okx.fail.clear()
        decided = self.run_after_outage(feed, ready + 10 * MIN + exchange_book.SECONDARY_RETRY_SECONDS * 1000, seconds=120)
        self.assertTrue(decided)
        for book in decided:
            self.assertEqual(book["values"], self.expected(book["bar_close_ms"]))

    def test_book_spread_a_failed_mark_catch_up_never_reads_index_again(self):
        # R2.1.11 (R219-BOOK-SPREAD): index and mark finish on their own; the retry reads only the failed series
        feed = self.feed()
        ready = self.warm(feed, T0 + 3 * 86_400_000 + 7 * 3_600_000)
        since = len(self.okx.calls)
        self.okx.fail.add("mark")
        self.at(feed, ready + 10 * MIN)  # the catch-up: index reads, mark fails
        index_reads = self.history_reads(since, "index")
        self.assertGreater(index_reads, 0)
        self.okx.fail.clear()
        decided = self.run_after_outage(feed, ready + 10 * MIN + exchange_book.SECONDARY_RETRY_SECONDS * 1000, seconds=120)
        self.assertEqual(self.history_reads(since, "index"), index_reads)  # index read once
        self.assertGreater(self.history_reads(since, "mark"), 0)
        self.assertTrue(decided)
        for book in decided:
            self.assertEqual(book["values"], self.expected(book["bar_close_ms"]))
        self.assertTrue({"index_until", "mark_until"} <= exchange_book.GapBookFeed.OWN)  # never written to the wrapped feed

    def test_exchange_runs_use_the_gap_book_and_hash_it(self):
        self.assertIn("exchange_book.py", exchange_lab.SOURCE_FILES)
        from trading_lab import arena, strategy_training
        self.assertNotIn("exchange_book.py", strategy_training.SOURCE_FILES)
        self.assertNotIn("exchange_book.py", arena.SOURCE_FILES)
        source = (ROOT / "trading_lab" / "exchange_lab.py").read_text(encoding="utf-8")
        self.assertIn("feed = GapBookFeed(feed,", source)


class ReviewFeatureNameTests(unittest.TestCase):
    def test_schema4_keys_keep_their_names_and_anything_else_stays_unknown(self):
        rows = battle_review.checks([{"feature": "return_bps@1m:60", "op": "gte", "actual": 1, "expected": 2, "passed": False},
                                     {"feature": "spread_bps", "op": "lte", "actual": 1, "expected": 2, "passed": True},
                                     {"feature": "return_bps@1m:60 <script>", "passed": None},
                                     {"feature": "x@2y:3", "passed": None}])
        self.assertEqual([r["feature"] for r in rows], ["return_bps@1m:60", "spread_bps", "unknown", "unknown"])
        for spec in SPECS:
            self.assertEqual(battle_review.feature_name(v4.feature_key(spec)), v4.feature_key(spec))


if __name__ == "__main__":
    unittest.main()
