"""OKX Trading Statistics snapshot: offline fake transport only; no network, credentials or trading routes."""
import csv
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import okx_stats as stats

P = stats.parse_time
NOW = stats.DEV_END_MS + 5 * stats.DAY + 7 * stats.MINUTE


class FakeOkx:
    """Rubik-like pages: contract routes newest first, at most `limit`, strictly older than `end`; currency routes have
    no limit, an inclusive `end` and a fixed window. Beyond the retention OKX either returns nothing or zero rows."""

    def __init__(self, retention=250, beyond="empty", window=None, page_size=72, raw=None, range_error_after=None):
        self.retention, self.beyond, self.window, self.page_size = retention, beyond, window, page_size
        self.raw, self.range_error_after, self.calls, self.cache = raw, range_error_after, [], {}

    def stamps(self, period):
        if period not in self.cache:
            span = (max(self.retention, self.window or 0) + 300) * stats.approx_bar_ms(period)
            out, stamp = [], max(NOW - span, P("2020-01-01")) // stats.HOUR * stats.HOUR
            while stamp <= NOW:
                if stats.aligned(stamp, period):
                    out.append(stamp)
                    stamp = stats.bar_close_ms(stamp, period)
                else:
                    stamp += stats.HOUR
            self.cache[period] = out
        return self.cache[period]

    def page(self, data, code="0"):
        body = json.dumps({"code": code, "msg": "", "data": data}).encode()
        digest = hashlib.sha256(body).hexdigest()
        if self.raw:
            Path(self.raw).mkdir(parents=True, exist_ok=True)
            (Path(self.raw) / f"{digest}.json").write_bytes(body)
        return digest

    def get(self, route, query):
        self.calls.append((route, dict(query)))
        dataset = next(name for name, spec in stats.DATASETS.items() if spec["route"] == route and
                       all(query.get(k) == v for k, v in spec["params"].items()))
        width = len(stats.DATASETS[dataset]["columns"])
        stamps = self.stamps(query["period"])
        real = set(stamps[-self.retention:])
        end = int(query["end"]) if "end" in query else None
        if stats.DATASETS[dataset]["limit"]:
            older = [s for s in reversed(stamps) if end is None or s < end]
            if self.beyond == "empty":
                older = [s for s in older if s in real]
            chosen = older[:int(query["limit"])]
        else:
            window = stamps[-(self.window or len(stamps)):]
            if self.range_error_after and end is not None and end < window[-self.range_error_after]:
                return self.reply(query, [], "50030")
            chosen = [s for s in reversed(window) if end is None or s <= end][:self.page_size]
        data = [[str(s)] + ([f"{(s // 60000) % 997 + 1}.{i}5" for i in range(width)] if s in real else ["0"] * width) for s in chosen]
        return self.reply(query, data)

    def reply(self, query, data, code="0"):
        return {"query": dict(query), "ok": code == "0", "http": 200, "code": code, "msg": "", "data": data,
                "sha256": self.page(data, code), "received_at": stats.utc_now(), "attempts": 1}


def read_csv(folder):
    manifest = json.loads((folder / "MANIFEST.json").read_text(encoding="utf-8"))
    with (folder / manifest["file"]).open(newline="", encoding="utf-8") as handle:
        return manifest, list(csv.reader(handle))


class OkxStatsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        base = Path(self.folder.name)
        self.dev, self.forward, self.raw = base / "dev", base / "vault", base / "vault" / "_raw"

    def tearDown(self):
        self.folder.cleanup()

    def fetch(self, okx, dataset, key, period, replace=False):
        return stats.fetch_series(okx, dataset, key, period, self.dev, self.forward, replace)

    def test_bar_close_follows_okx_time_zones_and_calendar_months(self):
        self.assertEqual(stats.DEV_END_MS, P("2026-10-02T18:32:00Z"))
        self.assertEqual(stats.bar_close_ms(P("2026-10-02T18:25:00Z"), "5m"), P("2026-10-02T18:30:00Z"))
        self.assertEqual(stats.bar_close_ms(P("2026-10-01T16:00:00Z"), "1D"), P("2026-10-02T16:00:00Z"))
        self.assertEqual(stats.bar_close_ms(P("2026-09-30T16:00:00Z"), "1M"), P("2026-10-31T16:00:00Z"))
        self.assertEqual(stats.bar_close_ms(P("2026-01-31T16:00:00Z"), "1M"), P("2026-02-28T16:00:00Z"))
        self.assertEqual(stats.bar_close_ms(P("2026-07-01"), "3Mutc"), P("2026-10-01"))
        self.assertEqual(stats.bar_close_ms(P("2025-12-31T16:00:00Z"), "3M"), P("2026-03-31T16:00:00Z"))
        self.assertEqual(stats.bar_close_ms(P("2026-09-28T16:00:00Z"), "1W"), P("2026-10-05T16:00:00Z"))
        self.assertTrue(stats.aligned(P("2026-10-01T22:00:00Z"), "6H"))
        self.assertFalse(stats.aligned(P("2026-10-02T00:00:00Z"), "6H"))
        self.assertTrue(stats.aligned(P("2026-10-02T00:00:00Z"), "6Hutc"))
        self.assertTrue(stats.aligned(P("2026-09-30T16:00:00Z"), "1M"))
        self.assertFalse(stats.aligned(P("2026-10-01"), "1M"))
        with self.assertRaises(ValueError):
            stats.period_parts("1m")

    def test_hourly_split_uses_bar_close_drops_zero_padding_and_rebuilds_from_raw(self):
        okx = FakeOkx(retention=250, beyond="zeros", raw=self.raw)
        out = self.fetch(okx, "taker-volume-contract_unit2-usdt", "BTC-USDT-SWAP", "1H")
        self.assertEqual(out["stop"], "zero_page")
        self.assertEqual(out["rows_dev"] + out["rows_forward"], 250)
        manifest, rows = read_csv(self.dev / "BTC-USDT-SWAP" / "taker-volume-contract_unit2-usdt" / "1H")
        self.assertEqual(rows[0], stats.PREFIX + ["sell_vol_usdt", "buy_vol_usdt"])
        self.assertEqual(rows[-1][1], "2026-10-02T17:00:00Z")  # closes 18:00; the 18:00 bar closes 19:00 > DEV end
        self.assertTrue(all(int(row[2]) <= stats.DEV_END_MS for row in rows[1:]))
        self.assertGreater(manifest["series"]["zero_padding_dropped"], 0)
        self.assertNotIn("newest_utc", manifest["series"])
        self.assertTrue(all(len(page["sha256"]) == 64 for page in manifest["pages"]))
        forward, frows = read_csv(self.forward / "BTC-USDT-SWAP" / "taker-volume-contract_unit2-usdt" / "1H")
        self.assertEqual(frows[1][1], "2026-10-02T18:00:00Z")
        self.assertEqual((manifest["side"], forward["side"]), ("dev", "forward"))
        self.assertEqual(stats.verify(self.dev, self.raw), [])
        self.assertEqual(stats.verify(self.forward, self.raw), [])
        self.assertTrue(all(call[1]["unit"] == "2" and call[1]["limit"] == "100" for call in okx.calls))

    def test_daily_weekly_and_monthly_bars_that_straddle_the_cutoff_never_reach_dev(self):
        okx = FakeOkx(retention=2000, beyond="empty", raw=self.raw)
        for period, last_dev, first_forward in (("1Dutc", "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z"),
                                                ("1D", "2026-10-01T16:00:00Z", "2026-10-02T16:00:00Z"),
                                                ("1M", "2026-08-31T16:00:00Z", "2026-09-30T16:00:00Z"),
                                                ("3Mutc", "2026-07-01T00:00:00Z", "2026-10-01T00:00:00Z")):
            out = self.fetch(okx, "open-interest-history", "ETH-USDT-SWAP", period)
            self.assertEqual(out["stop"], "empty", period)
            _, dev = read_csv(self.dev / "ETH-USDT-SWAP" / "open-interest-history" / period)
            _, forward = read_csv(self.forward / "ETH-USDT-SWAP" / "open-interest-history" / period)
            self.assertEqual((dev[-1][1], forward[1][1]), (last_dev, first_forward), period)
        self.assertEqual(stats.verify(self.dev, self.raw), [])
        index = stats.write_index(self.dev, "dev")
        self.assertEqual(len(index["series"]), 4)

    def test_currency_endpoint_has_no_limit_and_pages_with_an_inclusive_end(self):
        okx = FakeOkx(retention=10_000, window=720, page_size=72, raw=self.raw)
        out = self.fetch(okx, "taker-volume_insttype-contracts", "SOL", "1H")
        self.assertEqual((out["stop"], out["rows_dev"] + out["rows_forward"]), ("no_progress", 720))
        self.assertTrue(all("limit" not in query and query["instType"] == "CONTRACTS" for _, query in okx.calls))
        self.assertEqual(len(okx.calls), 12)  # 72 + 10 x (71 new + the repeated end row), then a page with only that row
        self.assertEqual(stats.verify(self.forward, self.raw), [])

    def test_range_error_ends_paging_without_retry_and_is_recorded(self):
        okx = FakeOkx(retention=10_000, window=10_000, page_size=72, range_error_after=150)
        out = self.fetch(okx, "long-short-account-ratio", "BTC", "5m")
        self.assertEqual(out["stop"], "range_limit")
        self.assertEqual(out["errors"][0]["code"], "50030")
        manifest = json.loads((self.forward / "BTC" / "long-short-account-ratio" / "5m" / "MANIFEST.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["truncated"])
        self.assertEqual(manifest["pages"][-1]["code"], "50030")

    def test_existing_snapshot_is_kept_unless_replace(self):
        self.fetch(FakeOkx(), "open-interest-history", "BTC-USDT-SWAP", "1H")
        path = self.dev / "BTC-USDT-SWAP" / "open-interest-history" / "1H" / "MANIFEST.json"
        before = path.read_bytes()
        again = self.fetch(FakeOkx(retention=10), "open-interest-history", "BTC-USDT-SWAP", "1H")
        self.assertEqual(sorted(again["skipped"]), ["dev", "forward"])
        self.assertEqual(path.read_bytes(), before)
        self.fetch(FakeOkx(retention=10), "open-interest-history", "BTC-USDT-SWAP", "1H", replace=True)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["rows"], 0)

    def test_a_resumed_fetch_skips_finished_series_before_any_request(self):
        work = [("open-interest-history", "BTC-USDT-SWAP", "1H"), ("open-interest-history", "ETH-USDT-SWAP", "1H")]
        self.fetch(FakeOkx(), *work[0])
        self.assertEqual(stats.pending(work, self.dev, self.forward), work[1:])
        (self.forward / "BTC-USDT-SWAP" / "open-interest-history" / "1H" / "MANIFEST.json").unlink()
        self.assertEqual(stats.pending(work, self.dev, self.forward), work)  # a side without a manifest is unfinished

    def test_verify_reports_tampering_wrong_side_and_raw_mismatch(self):
        self.fetch(FakeOkx(raw=self.raw), "long-short-account-ratio-contract", "BTC-USDT-SWAP", "1H")
        folder = self.dev / "BTC-USDT-SWAP" / "long-short-account-ratio-contract" / "1H"
        manifest, rows = read_csv(folder)
        rows[5][4] = "9.99"
        future = stats.parse_time("2026-10-02T18:00:00Z")
        rows.append([str(future), stats.iso(future), str(future + stats.HOUR), stats.iso(future + stats.HOUR), "1.0"])
        with (folder / manifest["file"]).open("w", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerows(rows)
        problems = stats.verify(self.dev, self.raw)
        self.assertTrue(any("sha256" in p for p in problems))
        self.assertTrue(any("не на своєму боці" in p for p in problems))
        self.assertTrue(any("сирих сторінок" in p for p in problems))
        self.assertEqual(stats.verify(Path(self.folder.name) / "none"), ["жодного MANIFEST.json"])

    def test_probe_jumps_to_the_oldest_row_with_few_requests(self):
        for beyond in ("empty", "zeros"):
            okx = FakeOkx(retention=1440, beyond=beyond)
            out = stats.probe_series(okx, "open-interest-history" if beyond == "empty" else "taker-volume-contract_unit1-contracts",
                                     "BTC-USDT-SWAP", "5m")
            stamps = okx.stamps("5m")
            self.assertEqual((out["method"], out["requests"]), ("jump", 2), beyond)
            self.assertEqual(out["oldest_nonzero_utc"], stats.iso(stamps[-1440]), beyond)
            self.assertEqual(out["newest_utc"], stats.iso(stamps[-1]))
            self.assertEqual(out["depth_bars_estimate"], 1440)
            self.assertFalse(out["reaches_dev"])
        daily = stats.probe_series(FakeOkx(retention=900), "open-interest-history", "BTC-USDT-SWAP", "1Dutc")
        self.assertEqual((daily["method"], daily["nonzero_rows_seen"], daily["reaches_dev"]), ("paged", 900, True))


class Response:
    def __init__(self, body, status=200):
        self.body, self.status = body, status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=-1):
        return self.body


class Opener:
    def __init__(self, replies):
        self.replies, self.urls = list(replies), []

    def open(self, request, timeout=None):
        self.urls.append(request.full_url)
        assert request.get_method() == "GET" and not request.has_header("Authorization")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.pacer = stats.Pacer(Path(self.folder.name) / "pace", 1000)

    def tearDown(self):
        self.folder.cleanup()

    def client(self, replies):
        opener = Opener(replies)
        return stats.StatsClient(self.pacer, opener=opener, route_gap=0, retry_wait=0, raw_dir=Path(self.folder.name) / "raw"), opener

    def test_only_rubik_statistics_routes_are_allowed(self):
        client, opener = self.client([])
        for route in ("/api/v5/account/balance", "/api/v5/trade/order", "/api/v5/asset/withdrawal",
                      "/api/v5/market/history-candles", "/api/v5/rubik/stat/option/open-interest-volume"):
            with self.assertRaises(ValueError):
                client.get(route, {})
        self.assertEqual(opener.urls, [])

    def test_success_keeps_the_exact_raw_body_under_its_sha256(self):
        body = json.dumps({"code": "0", "msg": "", "data": [["1790960400000", "1", "2", "3"]]}).encode()
        client, opener = self.client([Response(body)])
        page = client.get(stats.DATASETS["open-interest-history"]["route"], {"instId": "BTC-USDT-SWAP", "period": "1H"})
        self.assertTrue(page["ok"])
        self.assertEqual(page["attempts"], 1)
        self.assertEqual((Path(self.folder.name) / "raw" / f"{page['sha256']}.json").read_bytes(), body)
        self.assertTrue(opener.urls[0].startswith("https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-history?"))

    def test_one_retry_after_a_failure_and_redirects_are_never_followed(self):
        redirect = urllib.error.HTTPError("https://www.okx.com/x", 302, "Found", {"Location": "https://evil.example"}, io.BytesIO(b""))
        client, opener = self.client([redirect, ConnectionResetError("reset")])
        page = client.get(stats.SUPPORT, {})
        self.assertEqual((page["ok"], page["attempts"], len(opener.urls)), (False, 2, 2))
        self.assertEqual(page.get("error"), "ConnectionResetError")
        retried, _ = self.client([Response(b'{"code":"50011","msg":"Too Many Requests","data":[]}', 429),
                                  Response(b'{"code":"0","msg":"","data":{"contract":["BTC"]}}')])
        self.assertTrue(retried.get(stats.SUPPORT, {})["ok"])

    def test_okx_business_errors_are_not_retried(self):
        client, opener = self.client([Response(b'{"code":"50030","msg":"Illegal time range","data":[]}')])
        page = client.get(stats.DATASETS["taker-volume_insttype-contracts"]["route"], {"ccy": "BTC", "period": "1D"})
        self.assertEqual((page["ok"], page["code"], page["attempts"], len(opener.urls)), (False, "50030", 1, 1))

    def test_redirect_handler_refuses_to_follow(self):
        self.assertIsNone(stats.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example"))


if __name__ == "__main__":
    unittest.main()
