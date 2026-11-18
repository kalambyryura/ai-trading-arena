"""Public UI assets stay local, allowlisted and free of trading side effects."""
import http.client
from pathlib import Path
import shutil
import tempfile
import threading
import unittest

from trading_lab.arena import Arena
from trading_lab.exchange_lab import ExchangeLab
from trading_lab.web_panel import Panel, Server


class ColiseumHTTPTests(unittest.TestCase):
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

    def test_local_assets_and_fonts_are_available_without_financial_side_effects(self):
        for path, content_type in {"/arena":"text/html", "/arena-coliseum.js":"text/javascript", "/arena-coliseum.css":"text/css",
                                   "/assets/coliseum-floor.png":"image/png", "/assets/human-poses.png":"image/png",
                                   "/assets/robot-poses.png":"image/png", "/assets/cash.png":"image/png",
                                   "/assets/human-portrait.png":"image/png", "/assets/robot-portrait.png":"image/png",
                                   "/assets/icons.css":"text/css", "/assets/Phosphor.woff2":"font/woff2",
                                   "/assets/CormorantGaramond.ttf":"font/ttf", "/assets/Manrope.ttf":"font/ttf"}.items():
            with self.subTest(path=path):
                status, headers, body = self.get(path)
                self.assertEqual(status, 200)
                self.assertTrue(headers["Content-Type"].startswith(content_type))
                self.assertGreater(len(body), 100)
                self.assertIn("font-src 'self'", headers["Content-Security-Policy"])
                self.assertNotIn("unsafe-", headers["Content-Security-Policy"])
                if path.endswith(".png"):
                    self.assertTrue(body.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(self.calls, [])
        self.assertFalse(self.server.arena.session)
        self.assertFalse(any(self.server.exchange.status()["active"].values()))

    def test_asset_allowlist_cannot_read_private_or_unlisted_files(self):
        (self.root / "web" / "assets" / "private.txt").write_text("PRIVATE_SENTINEL", encoding="utf-8")
        for path in ("/assets/private.txt", "/assets/../arena.html", "/assets/%2e%2e/.env.okx.local", "/assets/coliseum-prompts.json", "/.env.okx.local"):
            status, headers, body = self.get(path)
            self.assertEqual(status, 404)
            self.assertNotIn(b"PRIVATE_SENTINEL", body)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
