"""News recorder (D114): feed parsing, honest times, dedupe and revisions, chain, gaps, conditional GET. Fake HTTP only."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import news_recorder as nr

T0 = 1_793_232_000_000  # 2026-10-29T00:00:00Z


def feed(*items):
    body = "".join(f"<item><title>{t}</title><link>https://example.org/{g}?utm=x</link><guid>{g}</guid>"
                   f"<pubDate>{p}</pubDate><description>&lt;p&gt;{d} &amp;amp; more&lt;/p&gt;</description></item>"
                   for g, t, p, d in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'.encode()


class FakeHttp:
    def __init__(self):
        self.bodies, self.calls, self.fail = {}, [], set()

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        if url in self.fail:
            raise TimeoutError("down")
        body = self.bodies.get(url, b"")
        if headers.get("If-None-Match") == "v-" + str(len(body)):
            return 304, b"", {"Date": "Thu, 29 Oct 2026 00:00:00 GMT"}
        return 200, body, {"ETag": "v-" + str(len(body)), "Date": "Thu, 29 Oct 2026 00:00:00 GMT"}


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.http, self.now = FakeHttp(), [T0]
        self.url = "https://feed.example/rss"
        self.recorder = nr.Recorder(self.root, {"demo": self.url}, self.http, lambda: self.now[0])
        self.recorder.start()

    def tearDown(self):
        self.tmp.cleanup()

    def lines(self):
        return [json.loads(line) for path in sorted((self.root / "demo").glob("*.jsonl")) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_items_keep_both_times_and_flags(self):
        self.http.bodies[self.url] = feed(("a1", "Old item", "Wed, 28 Oct 2026 10:00:00 +0000", "x"),
                                          ("a2", "Fresh item", "Thu, 29 Oct 2026 00:01:00 +0000", "y"))
        self.now[0] = T0 + 120_000
        self.recorder.round()
        old, fresh = sorted(self.lines(), key=lambda r: r["guid"])
        self.assertEqual((old["before_start"], old["late"], fresh["before_start"], fresh["late"]), (True, True, False, False))
        self.assertEqual((fresh["published_at"], fresh["first_seen_at"]), ("2026-10-29T00:01:00.000Z", "2026-10-29T00:02:00.000Z"))
        self.assertEqual(fresh["summary"], "y & more")
        self.assertTrue(fresh["after_gap"])  # the first round after a start has no earlier heartbeat to vouch for it

    def test_repeats_are_skipped_revisions_keep_the_first_publication_time_and_the_chain_verifies(self):
        self.http.bodies[self.url] = feed(("b1", "Title", "Thu, 29 Oct 2026 00:01:00 +0000", "s"))
        self.now[0] += 60_000
        self.recorder.round()
        self.now[0] += 120_000
        self.recorder.round()  # 304: nothing new
        self.assertEqual(self.http.calls[-1][1].get("If-None-Match"), "v-" + str(len(self.http.bodies[self.url])))
        self.http.bodies[self.url] = feed(("b1", "Title, corrected", "Thu, 29 Oct 2026 03:00:00 +0000", "s"))
        self.now[0] += 120_000
        self.recorder.round()
        first, revision = self.lines()
        self.assertEqual((revision["revision"], revision["record_sha256"], revision["published_at"]),
                         (1, first["record_sha256"], first["published_at"]))
        self.assertFalse(revision["after_gap"])
        self.assertEqual(nr.verify(self.root)["ok"], True)
        path = next((self.root / "demo").glob("*.jsonl"))
        path.write_text(path.read_text(encoding="utf-8").replace("corrected", "edited"), encoding="utf-8")
        self.assertFalse(nr.verify(self.root)["ok"])

    def test_a_long_pause_marks_the_next_items_after_gap_and_errors_do_not_retry(self):
        self.http.bodies[self.url] = feed(("c1", "One", "Thu, 29 Oct 2026 00:01:00 +0000", "s"))
        self.now[0] += 60_000
        self.recorder.round()
        self.http.fail.add(self.url)
        self.now[0] += 120_000
        beat = self.recorder.round()
        self.assertEqual((beat["sources"]["demo"]["error"], len(self.http.calls)), ("TimeoutError", 2))
        self.http.fail.clear()
        self.http.bodies[self.url] = feed(("c2", "Two", "Thu, 29 Oct 2026 01:00:00 +0000", "s"))
        self.now[0] += 3_600_000  # the PC slept
        beat = self.recorder.round()
        self.assertTrue(beat["after_gap"])
        self.assertTrue(self.lines()[-1]["after_gap"])

    def test_entities_and_items_without_times_are_handled_safely(self):
        with self.assertRaises(ValueError):
            nr.parse_feed(b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "x">]><rss><channel></channel></rss>')
        self.http.bodies[self.url] = feed(("d1", "No time", "", "s"))
        self.now[0] += 60_000
        self.recorder.round()
        self.assertEqual((self.lines()[0]["published_at"], self.lines()[0]["before_start"]), (None, True))

    def test_one_cached_date_does_not_flag_a_source_and_the_median_does(self):
        urls = {name: f"https://{name}.example/rss" for name in ("a", "b", "c")}
        now = [T0]

        def fetch(url, headers):
            from datetime import datetime, timezone
            from email.utils import format_datetime
            behind = 500 if "//a." in url else 6  # a CDN serves a 500 s old Date; the others are fine
            date = format_datetime(datetime.fromtimestamp(now[0] / 1000 - behind, timezone.utc), usegmt=True)
            return 200, feed(("g" + url[8], "T", "Thu, 29 Oct 2026 00:01:00 +0000", "s")), {"Date": date}
        recorder = nr.Recorder(self.root / "multi", urls, fetch, lambda: now[0])
        recorder.start()
        now[0] += 120_000
        beat = recorder.round()
        self.assertEqual(beat["clock_skew_median_s"], 6.0)
        records = [json.loads(l) for name in urls for l in next((self.root / "multi" / name).glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
        self.assertEqual({r["clock_suspect"] for r in records}, {False})

    def test_the_heartbeat_is_chained_and_anchors_every_source_head(self):
        self.http.bodies[self.url] = feed(("h1", "One", "Thu, 29 Oct 2026 00:01:00 +0000", "s"))
        for _ in range(3):
            self.now[0] += 120_000
            self.recorder.round()
        self.assertTrue(nr.verify(self.root)["ok"])
        beats = (self.root / "heartbeat.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(beats[-1])["sources"]["demo"]["head_sha256"], self.lines()[-1]["line_sha256"])
        (self.root / "heartbeat.jsonl").write_text("\n".join(beats[:1] + beats[2:]) + "\n", encoding="utf-8")  # a beat cut out
        self.assertFalse(nr.verify(self.root)["ok"])

    def test_a_broken_200_keeps_no_validator_so_later_rounds_ask_again(self):
        self.http.bodies[self.url] = b"<rss><channel><item><title>cut"  # a CDN stub page, not a feed
        self.now[0] += 120_000
        beat = self.recorder.round()
        self.assertIn("error", beat["sources"]["demo"])
        self.assertEqual(beat["sources"]["demo"]["head_sha256"], None)
        self.now[0] += 120_000
        self.recorder.round()
        self.assertNotIn("If-None-Match", self.http.calls[-1][1])  # no 304 can pretend the source was read (review N2)

    def test_a_redirect_is_followed_only_to_the_same_https_host(self):
        import urllib.request
        handler = nr.NoRedirect()
        request = urllib.request.Request("https://www.coindesk.com/arc/outboundfeeds/rss/")
        for target in ("https://evil.example/rss", "http://www.coindesk.com/rss", "https://coindesk.com/rss", "//evil.example/x"):
            self.assertIsNone(handler.redirect_request(request, None, 302, "Found", {}, target), target)
        self.assertIsNotNone(handler.redirect_request(request, None, 308, "Permanent", {}, "/arc/outboundfeeds/rss"))

    def test_the_heartbeat_counts_what_each_feed_held(self):
        self.http.bodies[self.url] = feed(("n1", "Fresh", "Thu, 29 Oct 2026 00:01:00 +0000", "s"), ("n2", "Backlog", "Mon, 26 Oct 2026 00:00:00 +0000", "s"))
        self.now[0] += 120_000
        first = self.recorder.round()["sources"]["demo"]
        self.assertEqual((first["items"], first["new"], first["unchanged"], first["flagged"]["late"], first["flagged"]["before_start"]),
                         (2, 2, 0, 1, 1))
        self.assertEqual(first["newest_published_at"], "2026-10-29T00:01:00.000Z")
        self.http.bodies[self.url] = self.http.bodies[self.url] + b" "  # a rebuilt feed with the same items
        self.now[0] += 120_000
        again = self.recorder.round()["sources"]["demo"]
        self.assertEqual((again["new"], again["unchanged"]), (0, 2))

    def test_a_doctype_is_refused_in_any_encoding(self):
        body = '<?xml version="1.0" encoding="utf-16"?><!DOCTYPE r [<!ENTITY a "INJECTED">]><rss><channel><item><title>&a;</title></item></channel></rss>'
        with self.assertRaises(ValueError):
            nr.parse_feed(body.encode("utf-16"))

    def test_the_first_start_is_kept_across_restarts(self):
        first = json.loads((self.root / "RECORDER.json").read_text(encoding="utf-8"))["first_started_at"]
        self.now[0] += 86_400_000
        again = nr.Recorder(self.root, {"demo": self.url}, self.http, lambda: self.now[0])
        again.start()
        meta = json.loads((self.root / "RECORDER.json").read_text(encoding="utf-8"))
        self.assertEqual((meta["first_started_at"], len(meta["starts"])), (first, 2))


if __name__ == "__main__":
    unittest.main()
