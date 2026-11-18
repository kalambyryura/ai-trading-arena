"""R2.1.14 (WinError 10053, D157 p. 5, D160): a viewer that goes away mid-answer (a page closed or reloaded, the 8 s poll
timeout of web/arena.js) is no server fault: no second 503 answer on the dead socket and no stack in the console. Real
server on a temp root and port 0, a launcher that refuses any worker; no financial route, key file or network."""
import contextlib
import http.client
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from trading_lab.web_panel import Handler, Panel, Server

GONE = (ConnectionAbortedError(10053, "An established connection was aborted by the software in your host machine"),
        ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host"),
        BrokenPipeError(32, "Broken pipe"))


def refuse(*args, **kwargs):
    raise AssertionError("No worker in this test")


class ClientGone(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "web").mkdir()
        for page in ("index.html", "arena.html"):  # "/" is the arena page
            (root / "web" / page).write_text('<meta name="panel-token" content="__PANEL_TOKEN__">', encoding="utf-8")
        self.server = Server(Panel(root, launcher=refuse), 0)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.sent = []
        real = Handler.send

        def recording(handler, status, *args, **kwargs):
            self.sent.append(status)
            return real(handler, status, *args, **kwargs)
        patcher = patch.object(Handler, "send", recording)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, method, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=b"" if method == "POST" else None, headers=headers or {})
            reply = connection.getresponse()
            return reply.status
        except (http.client.HTTPException, OSError):
            return None  # the server closed the connection without an answer
        finally:
            connection.close()

    def gone_while_answering(self, method, path, error):
        """Every write of this answer meets the dead socket, as on Windows after the browser dropped the request."""
        def dead(handler):
            raise error
        console = io.StringIO()
        with patch.object(Handler, "end_headers", dead), contextlib.redirect_stderr(console):
            status = self.call(method, path)
        return status, console.getvalue()

    def test_a_viewer_gone_mid_answer_gets_no_503_and_leaves_no_stack(self):
        for error in GONE:
            for method, path, first in (("GET", "/", 200), ("GET", "/api/state", 200), ("POST", "/api/no-such-route", 403)):
                with self.subTest(error=type(error).__name__, method=method, path=path):
                    self.sent.clear()
                    status, console = self.gone_while_answering(method, path, error)
                    self.assertIsNone(status)
                    self.assertEqual(self.sent, [first])  # the one answer it tried; no 503 after it
                    self.assertNotIn("Traceback", console)
                    self.assertNotIn("Exception occurred", console)
                    self.sent.clear()
                    self.assertEqual(self.call(method, path), first)  # the server goes on answering

    def test_a_real_state_read_fault_still_answers_503(self):
        """Plan p. 3 (RC-08): a fault reading the local state is no client abort: 503 as before, the client alive."""
        self.sent.clear()
        with patch.object(self.server.exchange, "status", side_effect=OSError("disk")):
            status = self.call("GET", "/api/exchange/state")
        self.assertEqual(status, 503)
        self.assertEqual(self.sent, [503])

    def test_a_request_cut_off_before_its_answer_leaves_no_stack(self):
        for error in GONE:
            with self.subTest(error=type(error).__name__):
                console = io.StringIO()
                with contextlib.redirect_stderr(console):
                    try:
                        raise error
                    except OSError:
                        self.server.handle_error(None, ("127.0.0.1", 1))
                self.assertEqual(console.getvalue(), "")

    def test_any_other_fault_keeps_its_stack(self):
        console = io.StringIO()
        with contextlib.redirect_stderr(console):
            try:
                raise ValueError("a real fault")
            except ValueError:
                self.server.handle_error(None, ("127.0.0.1", 1))
        self.assertIn("Traceback", console.getvalue())
        self.assertIn("a real fault", console.getvalue())


if __name__ == "__main__":
    unittest.main()
