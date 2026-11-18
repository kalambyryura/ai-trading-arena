"""Live feature book (R1 full F2): warm-up from public history, minute updates, failures, budget wiring. A fake public
transport with OKX paging semantics only: no network, account route or key."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_strategy_v4_book import SPECS, filled, market
from trading_lab import strategy_v4 as v4, strategy_v4_feed as feedmod

T0 = 1_735_689_600_000
MIN = 60_000


class FakeOkx:
    """history-candles style pages: newest first, `after` = strictly older, at most `limit`; only what exists by now."""

    def __init__(self, rows, index, mark, funding):
        self.rows, self.index, self.mark, self.funding = rows, index, mark, funding
        self.now_ms, self.calls, self.fail = 0, [], set()

    def get(self, path, params):
        kind = next(k for k, (route, _) in feedmod.ROUTES.items() if route == path)
        fixed = feedmod.ROUTES[kind][1]
        assert all(params[k] == v for k, v in fixed.items()), params
        self.calls.append((kind, dict(params)))
        if kind in self.fail:
            raise OSError("down")
        after, limit = int(params.get("after", 10 ** 15)), int(params["limit"])
        if kind == "funding":
            rows = sorted((at for at, _ in self.funding if at <= self.now_ms and at < after), reverse=True)[:limit]
            rates = dict(self.funding)
            return [{"fundingTime": str(at), "realizedRate": rates[at] or ""} for at in rows]
        if kind == "swap":
            rows = [r for r in reversed(self.rows) if r[0] + MIN <= self.now_ms and r[0] < after][:limit]
            return [[str(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4]), str(r[5]), "0", "0", "1"] for r in rows]
        source = self.index if kind == "index" else self.mark
        stamps = sorted((t for t in source if t + MIN <= self.now_ms and t < after), reverse=True)[:limit]
        return [[str(t), "1", "1", "1", repr(source[t]), "1"] for t in stamps]


class BaseFeed:
    """The engine feed: 30 confirmed candles plus the minute in progress, as arena_market's raw candles."""

    def __init__(self, okx):
        self.okx, self.calls, self.max_calls = okx, 0, 2000

    def snapshot(self):
        now = self.okx.now_ms
        done = [r for r in self.okx.rows if r[0] + MIN <= now][-30:]
        candles = [[str(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4]), str(r[5]), "0", "0", "1"] for r in done]
        candles.append([str(now // MIN * MIN), "1", "1", "1", "1", "1", "0", "0", "0"])
        return {"at": now / 1000, "raw": {"candles": list(reversed(candles))}}


class BookFeedTests(unittest.TestCase):
    def setUp(self):
        self.rows, self.index, self.mark, self.funding = market(4, seed=21)
        self.okx = FakeOkx(self.rows, self.index, self.mark, self.funding)
        self.feed = feedmod.BookFeed(BaseFeed(self.okx), self.okx.get, SPECS)
        self.reference = filled(self.rows)

    def expected(self, close_ms):
        minutes = [r for r in self.reference if r[0] + MIN <= close_ms]
        secondary = {"index": self.index, "mark": self.mark, "funding": self.funding}
        return {v4.feature_key(s): v4.feature_value(s, minutes, secondary) for s in SPECS}

    def at(self, ms):
        self.okx.now_ms = ms
        before = len(self.okx.calls)
        snapshot = self.feed.snapshot()
        self.assertLessEqual(len(self.okx.calls) - before, max(feedmod.PAGES_PER_SNAPSHOT, 3))  # never a stall
        return snapshot

    def warm(self, start):
        for k in range(0, 600, 5):
            if "v4_features" in self.at(start + k * 1000):
                return start + k * 1000
        self.fail("no book after 10 minutes")

    def test_a_paced_warm_up_then_minute_updates_equal_the_reference(self):
        start = T0 + 3 * 86_400_000 + 7 * 3_600_000  # day 4: the warm-up reaches back over holes and the 4 h funding switch
        ready = self.warm(start)
        self.assertGreater(ready, start)  # several snapshots, two pages each
        checked = 0
        for k in range(5, 40 * 60, 5):
            snapshot = self.at(ready + k * 1000)
            book = snapshot["v4_features"]
            self.assertTrue(5_000 <= snapshot["at"] * 1000 - book["bar_close_ms"] < 65_000, k)
            if (snapshot["at"] * 1000 - book["bar_close_ms"]) // 1000 == 5:
                self.assertEqual(book["values"], self.expected(book["bar_close_ms"]), book["bar_close_ms"])
                checked += 1
        self.assertGreaterEqual(checked, 39)
        windows = feedmod.windows(SPECS)
        pages = {kind: sum(1 for k, params in self.okx.calls if k == kind and "after" in params) for kind in windows}
        self.assertLess(pages["index"], pages["swap"])  # every series pages over its own window only

    def test_a_long_warm_up_catches_up_on_the_minutes_that_passed(self):
        self.okx.rows, self.okx.index, self.okx.mark = self.rows, self.index, self.mark
        start = T0 + 3 * 86_400_000
        old = feedmod.PAGES_PER_SNAPSHOT
        feedmod.PAGES_PER_SNAPSHOT = 1
        try:
            ready = None
            for k in range(0, 3600, 60):  # one snapshot a minute: the paging outlasts the 20-minute catch-up limit
                if "v4_features" in self.at(start + k * 1000 + 5000):
                    ready = start + k * 1000 + 5000
                    break
        finally:
            feedmod.PAGES_PER_SNAPSHOT = old
        self.assertIsNotNone(ready)
        snapshot = self.at(ready + 60_000)
        self.assertEqual(snapshot["v4_features"]["values"], self.expected(snapshot["v4_features"]["bar_close_ms"]))

    def test_a_failed_warm_up_gives_no_book_and_retries_from_scratch(self):
        start = T0 + 2 * 86_400_000
        self.okx.fail.add("mark")
        for k in range(0, 120, 5):
            self.assertNotIn("v4_features", self.at(start + k * 1000))
            if self.feed.status.startswith("unavailable"):
                failed_at = start + k * 1000
                break
        self.okx.fail.clear()
        self.assertNotIn("v4_features", self.at(failed_at + 10_000))  # waits RETRY_SECONDS after the failure itself
        ready = self.warm(failed_at + feedmod.RETRY_SECONDS * 1000)
        book = self.at(ready + 5000)["v4_features"]
        self.assertEqual(book["values"], self.expected(book["bar_close_ms"]))
        self.assertEqual(self.feed.attempts, 2)

    def test_a_failed_minute_read_is_not_retried_and_the_minute_still_counts(self):
        ready = self.warm(T0 + 2 * 86_400_000)
        self.okx.fail.add("index")
        calls = len(self.okx.calls)
        for k in range(1, 13):  # one minute of snapshots with the index down
            snapshot = self.at(ready + 55_000 + k * 5000)
        index_reads = sum(1 for kind, _ in self.okx.calls[calls:] if kind == "index")
        minutes = len({(ready + 55_000 + k * 5000 - 5000) // MIN for k in range(1, 13)})  # visible minutes in the span
        self.assertLessEqual(index_reads, minutes)  # once per new minute, not on each of the 12 snapshots
        self.assertTrue(self.feed.status.startswith("degraded"))
        self.assertIn("v4_features", snapshot)  # the swap minute joined the book; only spread features may be UNKNOWN
        self.okx.fail.clear()
        later = self.at(ready + 5 * MIN)["v4_features"]
        self.assertEqual(later["bar_close_ms"], (ready + 5 * MIN - 5000) // MIN * MIN)

    def test_no_book_work_while_the_season_admits_no_entries(self):
        admitting = [False]
        feed = feedmod.BookFeed(BaseFeed(self.okx), self.okx.get, SPECS, admitting=lambda: admitting[0])
        self.okx.now_ms = T0 + 2 * 86_400_000
        for k in range(20):
            self.okx.now_ms += 5000
            self.assertNotIn("v4_features", feed.snapshot())
        self.assertEqual(self.okx.calls, [])  # recovery, closing, draining: not one history GET
        admitting[0] = True
        self.okx.now_ms += 5000
        feed.snapshot()
        self.assertTrue(self.okx.calls)

    def test_the_book_stops_at_its_own_budget(self):
        feed = feedmod.BookFeed(BaseFeed(self.okx), self.okx.get, SPECS, budget=5)
        for k in range(200):
            self.okx.now_ms = T0 + 2 * 86_400_000 + k * 5000
            self.assertNotIn("v4_features", feed.snapshot())
        self.assertLessEqual(len(self.okx.calls), 5)

    def test_engine_settings_reach_the_wrapped_feed(self):
        self.feed.max_calls = 9999
        self.assertEqual((self.feed.base.max_calls, self.feed.max_calls, self.feed.calls), (9999, 9999, 0))
        self.assertEqual(feedmod.PATHS, {"/api/v5/market/history-candles", "/api/v5/market/history-index-candles",
                                         "/api/v5/market/history-mark-price-candles", "/api/v5/public/funding-rate-history"})


class WiringTests(unittest.TestCase):
    def recipes(self):
        from trading_lab import strategy_v4_runtime as v4rt
        payload = {"strategy_schema_version": 4, "name": "w", "author": {"name": "platform", "kind": "model"}, "description": "d",
                   "original_text": "t", "confirmed": True, "instrument": "BTC-USDT-SWAP",
                   "rules": {"side": "long", "order": {"type": "taker"}, "stop_loss_bps": 50, "take_profit_bps": 80,
                             "max_hold_seconds": 600, "cooldown_seconds": 60, "size_pct": 10,
                             "entry": {"when": {"all": [{"feature": {"name": "sma_gap_bps", "tf": "1d", "n": 20}, "op": "gt", "value": 0},
                                                        {"feature": {"name": "basis_mark_bps", "tf": "5m", "n": 3}, "op": "gt", "value": -50}]}}}}
        v1 = {"id": "v1", "rules": {"max_hold_seconds": 600, "size_pct": 10}}
        return v1, v4rt.engine_recipe(v4.validate_payload(payload))

    def test_the_book_has_its_own_allowance_and_the_v1_formula_stays(self):
        from trading_lab import match_protocol
        v1, recipe = self.recipes()
        plain = match_protocol.capacities(3600, [v1])
        with_book = match_protocol.capacities(3600, [v1, recipe])
        observations = plain["max_observations"]
        self.assertEqual(plain["max_public_gets"], observations * 4 + 61 * 1 * 4 + 512)  # V1/V2: the formula as before
        self.assertNotIn("max_book_gets", plain)
        self.assertEqual(with_book["max_public_gets"], observations * 4 + 61 * 2 * 4 + 512)
        self.assertEqual(with_book["max_book_gets"], feedmod.request_budget([recipe], 3600 + 600 + 120))
        self.assertEqual(feedmod.specs_of([v1]), [])

    def test_every_series_pages_over_its_own_window(self):
        long_swap = [{"name": "return_bps", "tf": "1d", "n": 60}, {"name": "premium_bps", "tf": "1m", "n": 1}]
        plan = feedmod.windows(long_swap)
        self.assertGreater(plan["swap"], 60 * 86_400_000)
        self.assertLess(plan["index"], 10 * MIN)  # the review's case: no 63-day index paging for a 1-minute premium
        feedmod.BookFeed(None, None, long_swap)  # inside the per-series page cap

    def test_the_live_client_allows_the_book_routes_for_get_only(self):
        from trading_lab import exchange_api
        self.assertTrue(feedmod.PATHS <= exchange_api.GET_PATHS)
        self.assertFalse(feedmod.PATHS & exchange_api.POST_PATHS)

    def test_the_paper_feed_serves_only_book_routes_apart_from_its_own_limit(self):
        import io
        import json as jsonlib
        from trading_lab import arena_market
        from trading_lab.arena_strategies import ArenaError
        seen = []

        class Opener:
            def open(self, request, timeout):
                seen.append(request.full_url)
                return io.BytesIO(jsonlib.dumps({"code": "0", "data": []}).encode())
        public = arena_market.PublicFeed()
        public.opener, public.max_calls = Opener(), 0
        self.assertEqual(public.get_public("/api/v5/market/history-candles", {"instId": "BTC-USDT-SWAP", "bar": "1m", "limit": "100"}), [])
        with self.assertRaises(ArenaError):
            public.get_public("/api/v5/account/balance", {})
        self.assertEqual((public.calls, public.book_calls, len(seen)), (0, 1, 1))  # the quote allowance is untouched
        self.assertTrue(seen[0].startswith("https://openapi.okx.com/api/v5/market/history-candles?instId=BTC-USDT-SWAP"))

    def test_a_failing_source_stops_after_the_last_warm_up_attempt(self):
        rows, index, mark, funding = market(2, seed=2)
        okx = FakeOkx(rows, index, mark, funding)
        okx.fail.add("swap")
        feed = feedmod.BookFeed(BaseFeed(okx), okx.get, SPECS)
        for k in range(40):
            okx.now_ms = T0 + 86_400_000 + k * 5000 * 3
            self.assertNotIn("v4_features", feed.snapshot())
        self.assertEqual((feed.attempts, sum(c[0] == "swap" for c in okx.calls)), (feedmod.WARMUP_ATTEMPTS, feedmod.WARMUP_ATTEMPTS))


if __name__ == "__main__":
    unittest.main()
