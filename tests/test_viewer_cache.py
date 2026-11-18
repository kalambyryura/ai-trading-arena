"""Viewer snapshot cache: shared short-lived copies for polling tabs, adaptive TTL, instant invalidation, owner reads stay fresh."""
import http.client
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest

from trading_lab.arena import Arena
from trading_lab.exchange_lab import ExchangeLab
from trading_lab.viewer_cache import ViewerSnapshots
from trading_lab.web_panel import Panel, Server


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ViewerSnapshotsTests(unittest.TestCase):
    def make(self, **kwargs):
        self.clock = FakeClock()
        self.calls = {"exchange": 0, "arena": 0}
        self.cost = {"exchange": 0.0, "arena": 0.0}

        def loader(name):
            def load():
                self.calls[name] += 1
                self.clock.advance(self.cost[name])  # a slow status() takes (fake) time
                return {"name": name, "load": self.calls[name]}
            return load
        return ViewerSnapshots({"exchange": loader("exchange"), "arena": loader("arena")}, clock=self.clock, **kwargs)

    def test_readers_inside_the_ttl_share_one_load(self):
        cache = self.make()
        first = cache.get("exchange")
        self.clock.advance(1.4)
        self.assertIs(cache.get("exchange"), first)
        self.assertEqual(self.calls["exchange"], 1)
        self.clock.advance(0.2)
        second = cache.get("exchange")
        self.assertIsNot(second, first)
        self.assertEqual(second["load"], 2)

    def test_names_do_not_share_a_ttl(self):
        cache = self.make()
        cache.get("exchange")
        cache.get("arena")
        self.assertEqual(self.calls, {"exchange": 1, "arena": 1})

    def test_ttl_stretches_with_the_load_time_so_readers_hold_the_lock_at_most_a_quarter_of_the_time(self):
        cache = self.make()
        self.cost["exchange"] = 2.0                  # 300 runs: status() takes about two seconds
        first = cache.get("exchange")
        self.clock.advance(5.9)                      # ttl = max(1.5, 2.0 * 0.75 / 0.25) = 6.0
        self.assertIs(cache.get("exchange"), first)
        self.clock.advance(0.2)
        self.assertIsNot(cache.get("exchange"), first)
        self.assertLessEqual(2.0 / (6.0 + 2.0), 0.25 + 1e-9, "duty cap: load time over the whole cycle")

    def test_a_quick_loader_keeps_the_base_ttl(self):
        cache = self.make()
        self.cost["arena"] = 0.01
        first = cache.get("arena")
        self.clock.advance(1.49)
        self.assertIs(cache.get("arena"), first)
        self.clock.advance(0.02)
        self.assertIsNot(cache.get("arena"), first)

    def test_invalidate_drops_the_copies_at_once(self):
        cache = self.make()
        first = cache.get("exchange")
        cache.invalidate()
        self.assertIsNot(cache.get("exchange"), first)
        self.assertEqual(self.calls["exchange"], 2)

    def test_concurrent_readers_wait_for_one_load_and_share_its_result(self):
        gate, started = threading.Event(), threading.Event()
        loads = []

        def slow():
            loads.append(1)
            started.set()
            gate.wait(5)
            return {"shared": True}
        cache = ViewerSnapshots({"exchange": slow}, clock=FakeClock())
        results = []
        threads = [threading.Thread(target=lambda: results.append(cache.get("exchange"))) for _ in range(8)]
        threads[0].start()
        self.assertTrue(started.wait(5))
        for thread in threads[1:]:
            thread.start()
        gate.set()
        for thread in threads:
            thread.join(5)
        self.assertEqual(len(loads), 1)
        self.assertEqual(len(results), 8)
        self.assertTrue(all(result is results[0] for result in results))

    def test_invalidate_never_waits_for_a_running_load_and_the_stale_copy_is_not_stored(self):
        gate, started = threading.Event(), threading.Event()
        count = []

        def slow():
            count.append(1)
            started.set()
            gate.wait(5)
            return {"load": len(count)}
        cache = ViewerSnapshots({"exchange": slow}, clock=FakeClock())
        reader = threading.Thread(target=lambda: cache.get("exchange"))
        reader.start()
        self.assertTrue(started.wait(5))
        done = threading.Event()
        threading.Thread(target=lambda: (cache.invalidate(), done.set())).start()
        self.assertTrue(done.wait(1), "an owner write must not queue behind a slow status() call")
        gate.set()
        reader.join(5)
        gate.clear(); started.clear()
        gate.set()
        again = cache.get("exchange")
        self.assertEqual(again["load"], 2, "the load that overlapped the write was not kept")

    def test_a_failing_load_is_not_cached_and_reaches_every_waiter(self):
        gate, started = threading.Event(), threading.Event()
        attempts = []

        def broken():
            attempts.append(1)
            started.set()
            gate.wait(5)
            raise RuntimeError("status failed")
        cache = ViewerSnapshots({"exchange": broken}, clock=FakeClock())
        errors = []

        def read():
            try:
                cache.get("exchange")
            except RuntimeError as error:
                errors.append(str(error))
        threads = [threading.Thread(target=read) for _ in range(3)]
        threads[0].start()
        self.assertTrue(started.wait(5))
        for thread in threads[1:]:
            thread.start()
        gate.set()
        for thread in threads:
            thread.join(5)
        self.assertEqual(errors, ["status failed"] * 3)
        self.assertEqual(len(attempts), 1)
        with self.assertRaises(RuntimeError):
            cache.get("exchange")
        self.assertEqual(len(attempts), 2, "nothing was cached after the failure")

    def test_duty_must_be_a_fraction(self):
        for bad in (0, 1, -0.1, 1.5):
            with self.subTest(duty=bad):
                with self.assertRaises(ValueError):
                    ViewerSnapshots({}, duty=bad)


class ViewerCacheHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        shutil.copytree(Path(__file__).resolve().parents[1] / "web", self.root / "web")

        def forbidden(*args, **kwargs):
            raise AssertionError("UI GET must not execute or call a private account")
        arena = Arena(self.root, background=False, jev_factory=forbidden)
        self.server = Server(Panel(self.root, launcher=forbidden, balance_reader=forbidden), 0, arena=arena,
                             exchange_factory=lambda root, workshop, arena: ExchangeLab(root, workshop, arena, background=False))
        self.exchange_calls, self.arena_calls = [], []
        exchange_status, arena_status = self.server.exchange.status, self.server.arena.status
        self.server.exchange.status = lambda: (self.exchange_calls.append(1), exchange_status())[1]
        self.server.arena.status = lambda: (self.arena_calls.append(1), arena_status())[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def request(self, method, path, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        result = response.status, response.read()
        conn.close()
        return result

    def get(self, path, poll=False):
        return self.request("GET", path, {"X-Viewer-Poll": "1"} if poll else {})

    def test_scheduled_polls_share_a_copy_and_everything_else_is_fresh(self):
        first = self.get("/api/exchange/state", poll=True)
        second = self.get("/api/exchange/state", poll=True)
        self.assertEqual((first[0], second[0]), (200, 200))
        self.assertEqual(first[1], second[1])
        self.assertEqual(len(self.exchange_calls), 1, "two scheduled polls, one status() call")
        fresh = self.get("/api/exchange/state")
        self.assertEqual(fresh[0], 200)
        self.assertEqual(len(self.exchange_calls), 2, "a read without the header is never served from the copy")
        self.get("/api/arena/state", poll=True)
        self.get("/api/arena/state", poll=True)
        self.assertEqual(len(self.arena_calls), 1)
        self.get("/api/arena/state")
        self.assertEqual(len(self.arena_calls), 2)
        self.assertEqual(json.loads(first[1])["api_version"], json.loads(fresh[1])["api_version"])

    def test_the_header_value_must_be_exactly_one(self):
        for value in ("0", "true", "", "1 "):
            with self.subTest(value=value):
                before = len(self.exchange_calls)
                self.request("GET", "/api/exchange/state", {"X-Viewer-Poll": value})
                self.assertEqual(len(self.exchange_calls), before + 1)

    def test_any_post_drops_the_copies(self):
        self.get("/api/exchange/state", poll=True)
        self.get("/api/arena/state", poll=True)
        self.assertEqual((len(self.exchange_calls), len(self.arena_calls)), (1, 1))
        headers = {"Origin": self.server.origin, "X-Panel-Token": self.server.panel.token, "Content-Type": "application/json", "Content-Length": "2"}
        status, _ = self.request("POST", "/api/not-a-route", headers, b"{}")
        self.assertEqual(status, 404)
        self.get("/api/exchange/state", poll=True)
        self.get("/api/arena/state", poll=True)
        self.assertEqual((len(self.exchange_calls), len(self.arena_calls)), (2, 2), "the next scheduled poll loads again after a write")

    def test_a_rejected_post_also_drops_the_copies(self):
        self.get("/api/exchange/state", poll=True)
        status, _ = self.request("POST", "/api/exchange/stop", {"Content-Type": "application/json", "Content-Length": "2"}, b"{}")
        self.assertEqual(status, 403, "no token, no Origin: refused")
        self.get("/api/exchange/state", poll=True)
        self.assertEqual(len(self.exchange_calls), 2)


if __name__ == "__main__":
    unittest.main()
