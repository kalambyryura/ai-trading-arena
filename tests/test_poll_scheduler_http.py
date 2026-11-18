"""The arena page scripts (polling policy, battle facts) are served from the local allowlist and loaded before the page script; nothing here touches an account."""
import http.client
from pathlib import Path
import shutil
import tempfile
import threading
import unittest

from trading_lab.arena import Arena
from trading_lab.exchange_lab import ExchangeLab
from trading_lab.web_panel import Panel, Server


class ArenaScriptsHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        shutil.copytree(Path(__file__).resolve().parents[1] / "web", self.root / "web")
        self.calls = []

        def forbidden(*args, **kwargs):
            self.calls.append((args, kwargs))
            raise AssertionError("UI GET must not execute or call a private account")
        arena = Arena(self.root, background=False, jev_factory=forbidden)
        self.server = Server(Panel(self.root, launcher=forbidden, balance_reader=forbidden), 0, arena=arena,
                             exchange_factory=lambda root, workshop, arena: ExchangeLab(root, workshop, arena, background=False))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        conn.request("GET", path)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def test_scheduler_is_served_and_loaded_before_the_page_script(self):
        status, headers, body = self.get("/poll-scheduler.js")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/javascript"))
        self.assertIn(b"ArenaPollScheduler", body)
        self.assertNotIn("unsafe-", headers["Content-Security-Policy"])
        status, _, page = self.get("/arena")
        self.assertEqual(status, 200)
        html = page.decode("utf-8")
        self.assertLess(html.index('src="/poll-scheduler.js"'), html.index('src="/arena.js"'))
        self.assertEqual(self.calls, [])

    def test_battle_facts_is_served_and_loaded_before_the_page_script(self):
        status, headers, body = self.get("/battle-facts.js")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/javascript"))
        self.assertIn(b"ArenaBattleFacts", body)
        status, _, page = self.get("/arena")
        html = page.decode("utf-8")
        self.assertLess(html.index('src="/battle-facts.js"'), html.index('src="/arena.js"'))
        self.assertEqual(self.calls, [])

    def test_league_stats_is_served_for_the_league_page(self):
        status, headers, body = self.get("/league-stats.js")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/javascript"))
        self.assertIn(b"ArenaLeagueStats", body)
        self.assertEqual(self.get("/league-stats.js.map")[0], 404)
        self.assertEqual(self.calls, [])

    def test_author_report_is_served_and_loaded_between_the_facts_model_and_the_page(self):
        status, headers, body = self.get("/author-report.js")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/javascript"))
        self.assertIn(b"ArenaAuthorReport", body)
        html = self.get("/arena")[2].decode("utf-8")
        self.assertLess(html.index('src="/battle-facts.js"'), html.index('src="/author-report.js"'))
        self.assertLess(html.index('src="/author-report.js"'), html.index('src="/arena.js"'))
        self.assertEqual(self.get("/author-report.js.map")[0], 404)
        self.assertEqual(self.calls, [])

    def test_battle_context_is_served_and_loaded_before_the_report(self):
        status, headers, body = self.get("/battle-context.js")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/javascript"))
        self.assertIn(b"ArenaBattleContext", body)
        html = self.get("/arena")[2].decode("utf-8")
        self.assertLess(html.index('src="/battle-facts.js"'), html.index('src="/battle-context.js"'))
        self.assertLess(html.index('src="/battle-context.js"'), html.index('src="/author-report.js"'))
        self.assertEqual(self.get("/battle-context.js.map")[0], 404)
        self.assertEqual(self.calls, [])

    def test_unlisted_neighbours_stay_closed(self):
        for path in ("/poll-scheduler.cjs", "/web/poll-scheduler.js", "/poll-scheduler.js.map", "/poll-scheduler",
                     "/battle-facts.cjs", "/battle-facts.js.map", "/battle-facts"):
            with self.subTest(path=path):
                status, _, body = self.get(path)
                self.assertEqual(status, 404)
                self.assertNotIn(b"ArenaPollScheduler", body)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
