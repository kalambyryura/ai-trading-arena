"""B-11 public history fetcher: offline fake transport only; no network, credentials or trading routes."""
import csv
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import public_history as history

START = history.parse_time("2026-09-01T00:00:00Z")
CUTOFF = history.parse_time("2026-09-03T12:30:30.500Z")


class FakeOkx:
    """Pages newest-first like OKX, 100 rows per page, strictly older than `after`."""

    def __init__(self, missing=(), unconfirmed=(), leak_future=False, funding_every=8 * 3600_000):
        self.calls, self.lock = [], threading.Lock()
        self.missing, self.unconfirmed, self.leak = set(missing), set(unconfirmed), leak_future
        self.candles = [stamp for stamp in range(START - 3 * history.DAY, CUTOFF + 2 * history.DAY, history.MINUTE) if stamp not in self.missing]
        self.funding = list(range(START - 30 * history.DAY, CUTOFF + 3 * history.DAY, funding_every))

    def get(self, route, query):
        with self.lock:
            self.calls.append((route, dict(query)))
        if route == history.INSTRUMENTS:
            rows = [{"instId": query["instId"], "instType": query["instType"], "tickSz": "0.01", "lotSz": "0.01", "ctVal": "0.1", "minSz": "0.01"}]
            return rows, history.hashlib.sha256(json.dumps(rows).encode()).hexdigest()
        after = int(query["after"])
        if route in (history.INDEX_CANDLES, history.MARK_CANDLES):
            older = [stamp for stamp in self.candles if stamp < after][::-1][:100]
            rows = [[str(s), "1", "2", "0.5", "1.5", "0" if s in self.unconfirmed else "1"] for s in older]
        elif route == history.CANDLES:
            older = [stamp for stamp in self.candles if stamp < after][::-1][:100]
            if self.leak:
                older = [self.candles[-1]] + older  # a misbehaving page must still never leak post-cutoff candles
            rows = [[str(s), "1", "2", "0.5", "1.5", "10", "0.1", "15", "0" if s in self.unconfirmed else "1"] for s in older]
        else:
            older = [stamp for stamp in self.funding if stamp < after][::-1][:100]
            rows = [{"fundingTime": str(s), "fundingRate": "0.0001", "realizedRate": "0.0001", "method": "current_period", "formulaType": "withRate"} for s in older]
        return rows, history.hashlib.sha256(json.dumps(rows).encode()).hexdigest()


class PublicHistoryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.out = Path(self.folder.name) / "dataset"

    def tearDown(self):
        self.folder.cleanup()

    def test_time_parsing_and_last_closed_candle(self):
        self.assertEqual(history.parse_time("2026-09-01"), START)
        self.assertEqual(history.parse_time(str(START)), START)
        with self.assertRaises(ValueError):
            history.parse_time("2026-09-01T00:00:00+03:00")
        self.assertEqual(history.last_open_before(CUTOFF), history.parse_time("2026-09-03T12:29:00Z"))
        self.assertEqual(history.last_open_before(history.parse_time("2026-09-03T12:30:00Z")), history.parse_time("2026-09-03T12:29:00Z"))

    def test_fetch_stores_only_closed_confirmed_candles_up_to_the_cutoff_and_lists_gaps(self):
        gap = [history.parse_time("2026-09-02T10:00:00Z") + i * history.MINUTE for i in range(3)]
        unconfirmed = history.parse_time("2026-09-02T23:59:00Z")
        okx = FakeOkx(missing=gap, unconfirmed={unconfirmed}, leak_future=True)
        manifest = history.fetch(self.out, "BTC-USDT-SWAP", START, CUTOFF, okx, workers=3)
        candles = manifest["candles"]
        expected = (history.last_open_before(CUTOFF) - START) // history.MINUTE + 1
        self.assertEqual(candles["expected_rows"], expected)
        self.assertEqual(candles["rows"], expected - 4)
        self.assertEqual(candles["last_open_utc"], "2026-09-03T12:29:00Z")
        self.assertEqual(candles["gap_count"], 2)
        self.assertEqual(candles["missing_minutes"], 4)
        self.assertEqual(candles["gaps_first_200"][0], ["2026-09-02T10:00:00Z", "2026-09-02T10:02:00Z", 3])
        with (self.out / candles["file"]).open(newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0], history.COLUMNS)
        stamps = [int(row[0]) for row in rows[1:]]
        self.assertEqual(stamps, sorted(set(stamps)))
        self.assertLessEqual(max(stamps), history.last_open_before(CUTOFF))
        self.assertNotIn(unconfirmed, stamps)
        self.assertEqual(rows[1][1], "2026-09-01T00:00:00Z")
        self.assertTrue(all(route in {history.CANDLES, history.FUNDING} for route, _ in okx.calls))
        self.assertEqual(history.verify(self.out), [])

    def test_funding_is_bounded_by_start_and_cutoff(self):
        manifest = history.fetch(self.out, "BTC-USDT-SWAP", START, CUTOFF, FakeOkx(), workers=2)
        funding = manifest["funding"]
        with (self.out / funding["file"]).open(newline="", encoding="utf-8") as handle:
            stamps = [int(row[0]) for row in list(csv.reader(handle))[1:]]
        self.assertTrue(stamps and min(stamps) >= START and max(stamps) <= CUTOFF)
        self.assertEqual(funding["rows"], len(stamps))
        self.assertIn("absent, not zero", funding["note"])

    def test_interrupted_fetch_resumes_from_cached_day_chunks(self):
        history.fetch(self.out, "BTC-USDT-SWAP", START, CUTOFF, FakeOkx(), workers=2, with_funding=False)
        again = FakeOkx()
        first = json.loads((self.out / "MANIFEST.json").read_text(encoding="utf-8"))
        second = history.fetch(self.out, "BTC-USDT-SWAP", START, CUTOFF, again, workers=2, with_funding=False)
        self.assertEqual(again.calls, [])
        self.assertEqual(first["candles"]["sha256"], second["candles"]["sha256"])
        later = history.fetch(self.out, "BTC-USDT-SWAP", START, CUTOFF + history.DAY, FakeOkx(), workers=2, with_funding=False)
        self.assertGreater(later["candles"]["rows"], second["candles"]["rows"])

    def test_verify_reports_tampering_and_post_cutoff_rows(self):
        manifest = history.fetch(self.out, "BTC-USDT-SWAP", START, CUTOFF, FakeOkx(), workers=2, with_funding=False)
        path = self.out / manifest["candles"]["file"]
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        future = history.last_open_before(CUTOFF) + history.MINUTE
        lines.append(f"{future},{history.iso(future)},1,2,0.5,1.5,10,0.1,15,1\r\n")
        path.write_text("".join(lines), encoding="utf-8", newline="")
        problems = history.verify(self.out)
        self.assertTrue(any("sha256" in problem for problem in problems))
        self.assertTrue(any("після cutoff" in problem for problem in problems))
        self.assertEqual(history.verify(Path(self.folder.name) / "missing"), ["MANIFEST.json відсутній або нечитаний"])

    def test_index_and_mark_candles_keep_six_field_rows_and_verify(self):
        okx = FakeOkx(unconfirmed={history.parse_time("2026-09-02T23:59:00Z")})
        for kind, route in (("index", history.INDEX_CANDLES), ("mark", history.MARK_CANDLES)):
            out = self.out / kind
            manifest = history.fetch(out, "BTC-USDT", START, CUTOFF, okx, workers=2, kind=kind)
            entry = manifest["candles"]
            self.assertEqual((entry["kind"], entry["columns"], entry["source"]), (kind, history.PRICE_COLUMNS, history.BASE + route))
            self.assertIn(f"_{kind}-1m_", entry["file"])
            self.assertNotIn("funding", manifest, "funding belongs to the traded swap only")
            with (out / entry["file"]).open(encoding="utf-8", newline="") as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(rows[0], history.PRICE_COLUMNS)
            self.assertEqual(len(rows) - 1, entry["rows"])
            self.assertEqual(entry["gap_count"], 1, "the unconfirmed minute is left out")
            self.assertEqual(history.verify(out), [])
        self.assertTrue({route for route, _ in okx.calls} >= {history.INDEX_CANDLES, history.MARK_CANDLES})

    def test_instrument_specification_is_one_public_get(self):
        okx = FakeOkx()
        data = history.instrument(okx, "ETH-USDT-SWAP")
        self.assertEqual((data[0]["instId"], okx.calls[-1][0], okx.calls[-1][1]["instType"]), ("ETH-USDT-SWAP", history.INSTRUMENTS, "SWAP"))

    def test_client_refuses_any_route_except_the_public_history_endpoints(self):
        class Never:
            def open(self, *args, **kwargs):
                raise AssertionError("no network in tests")
        client = history.PublicClient(history.Pacer(Path(self.folder.name) / "pace", 1000), opener=Never(), attempts=1)
        for route in ("/api/v5/account/balance", "/api/v5/trade/order", "/api/v5/asset/withdrawal", "/api/v5/market/ticker"):
            with self.assertRaises(ValueError):
                client.get(route, {})

    def test_default_pace_file_is_shared_by_all_worktrees_of_the_repository(self):
        from tools.research_limits import shared_repository_root
        root = shared_repository_root(Path(__file__).resolve().parents[1])
        self.assertEqual(history.PACER, root / "runtime" / "public_rate" / "okx_public.pace")

    def test_pacer_reserves_spaced_slots_across_instances_sharing_one_file(self):
        path = Path(self.folder.name) / "pace"
        first, second = history.Pacer(path, 10), history.Pacer(path, 10)
        delays = [pacer.reserve() for pacer in (first, second, first, second)]
        self.assertTrue(all(b - a > .05 for a, b in zip(delays, delays[1:])), delays)
        self.assertFalse(path.with_name(path.name + ".lock").exists())


if __name__ == "__main__":
    unittest.main()
