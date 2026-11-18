"""D167: the Telegram notifier (tools/arena_notifier.py; plan team/platform/NOTIFIER_PLAN_UA.md, its check
team/reviews/NOTIFIER_PLAN_CHECK_UA.md). Fake panel, fake Telegram, temp roots; no network, no real token, no order."""
from datetime import datetime, timedelta, timezone
import http.server
import importlib.util
import io
import json
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import arena_notifier as notifier  # noqa: E402
from tools.arena_notifier import CRITICAL, IMPORTANT, ROUTINE, Notifier, SendFailed, Telegram  # noqa: E402

TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijklmnopq"
RID, RID2 = "a" * 32, "b" * 32
SECRETS = ("arena-secret-acct", "J5secretclordid", "998877665544", "algo-secret-77", TOKEN, "424242")
T0 = datetime(2026, 11, 11, 5, 0, tzinfo=timezone.utc).timestamp()  # 07:00 Kyiv


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def bot(bid="b1", label="Opus V1", position=0):
    return {"id": bid, "name": label, "position_btc": position, "account_id": "arena-secret-acct", "status": "WAITING"}


def ev(seq, kind, bot_id="b1", at=T0, **data):
    return {"seq": seq, "at": iso(at), "bot_id": bot_id, "code": kind, "message": "free text " + TOKEN[:3],
            "data": {"clOrdId": "J5secretclordid", "ordId": "998877665544", "algoId": "algo-secret-77", **data}}


def run(rid=RID, status="RUNNING", events=(), bots=None, context="arena_live"):
    return {"id": rid, "context": context, "status": status, "duration_seconds": 86400, "events": list(events),
            "bots": (bots if bots is not None else [bot()]) + [{"id": "cash", "name": "Cash"}], "degraded_windows": []}


def state(*runs, history=()):
    active = {"arena_live": None, "workshop_demo": None}
    for r in runs:
        active[r["context"]] = r
    return {"active": active, "history": list(history)}


class FakePanel:
    def __init__(self):
        self.exchange, self.finals, self.calls, self.down = state(), {}, [], False

    def get(self, path):
        self.calls.append(path)
        if self.down:
            raise OSError("panel down")
        if path == "/api/exchange/state":
            return json.loads(json.dumps(self.exchange))
        return json.loads(json.dumps(self.finals[path.rsplit("/", 1)[-1]]))


class FakeTelegram:
    def __init__(self, case):
        self.case, self.sent, self.fail = case, [], []

    def send(self, chat, text):
        if self.fail:
            raise self.fail.pop(0)
        self.sent.append((self.case.now, chat, text))


class NotifierCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = T0
        self.panel, self.tg = FakePanel(), FakeTelegram(self)
        self.make()

    def make(self):
        self.n = Notifier(self.root, self.panel, self.tg, 424242, clock=lambda: self.now)

    def drain(self, limit=200):
        for _ in range(limit):
            wait = self.n.send_next()
            if wait is None:
                return
            self.now += wait

    def texts(self):
        return [t for _, _, t in self.tg.sent]

    def cycle(self, exchange=None, seconds=60):
        if exchange is not None:
            self.panel.exchange = exchange
        self.n.poll()
        self.drain()
        self.now += seconds

    def launched(self):
        self.cycle(state())
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn("сповіщувач запущено", self.texts()[0])
        self.tg.sent.clear()


class Events(NotifierCase):
    def test_launch_start_and_no_duplicates_across_polls_and_a_restart(self):
        self.launched()
        self.cycle(state(run()))
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn("старт раунду aaaaaaaa · Live", self.texts()[0])
        self.cycle(state(run()))
        self.make()  # a restart of the notifier with its saved state
        self.cycle(state(run()))
        self.assertEqual(len(self.tg.sent), 1)

    def test_a_first_launch_mid_round_replays_no_history(self):
        self.cycle(state(run(events=[ev(1, "admission_closed"), ev(2, "protective_recovery")])))
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn("Активних раундів: 1", self.texts()[0])

    def test_a_pause_is_critical_with_its_code_once_per_pause(self):
        self.launched()
        self.cycle(state(run()))
        self.cycle(state(run(status="PAUSED", events=[ev(3, "stop_requested", reason_code="close_timeout"), ev(4, "closing_paused")])))
        self.cycle(state(run(status="PAUSED", events=[ev(3, "stop_requested", reason_code="close_timeout"), ev(4, "closing_paused")])))
        pauses = [t for t in self.texts() if "ПАУЗА" in t]
        self.assertEqual(len(pauses), 1)
        self.assertIn("Код: closing_paused", pauses[0])
        self.cycle(state(run(status="CLOSING")))
        self.cycle(state(run(status="PAUSED")))
        self.assertEqual(len([t for t in self.texts() if "ПАУЗА" in t]), 2)

    def test_a_pause_a_manual_review_and_a_stopped_bot_pass_a_full_routine_limit(self):
        self.launched()
        self.cycle(state(run()))
        self.n.state["routine"] = [self.now] * notifier.ROUTINE_PER_HOUR
        self.cycle(state(run(status="PAUSED", events=[ev(5, "exchange_check_failed", reason="foreign_algo_order"),
                                                      ev(6, "exchange_check_failed", reason="account_identity_changed"),
                                                      ev(7, "protective_recovery")])))
        self.assertEqual(sum(("ПАУЗА" in t) + ("РУЧНА ЗВІРКА" in t) + ("зупинено до кінця" in t) for t in self.texts()), 3)

    def test_a_process_stop_is_a_pause_too(self):
        self.launched()
        self.cycle(state(run(status="PAUSED")))
        self.assertTrue(any("ПАУЗА" in t and "process_stopped" in t for t in self.texts()))

    def test_a_manual_review_names_the_bot_and_its_reason(self):
        self.launched()
        self.cycle(state(run(events=[ev(5, "exchange_check_failed", reason="actual_position_differs_from_owned_fills")])))
        review = [t for t in self.texts() if "РУЧНА ЗВІРКА" in t]
        self.assertEqual(len(review), 1)
        self.assertIn("«Opus V1»", review[0])
        self.assertIn("actual_position_differs_from_owned_fills", review[0])

    def test_a_bot_stopped_by_its_protection_is_told(self):
        self.launched()
        self.cycle(state(run(events=[ev(5, "protective_recovery")])))
        self.assertTrue(any("зупинено до кінця раунду" in t for t in self.texts()))

    def test_a_stop_okx_does_not_show_and_its_verdict_are_told(self):
        """R2.1.15 RC2 (PP-05): protective_absent IMPORTANT, protective_not_created CRITICAL, each event once."""
        self.launched()
        seen, original = [], self.n.enqueue

        def capture(key, priority, kind, text):
            seen.append((kind, priority))
            return original(key, priority, kind, text)
        self.n.enqueue = capture
        self.cycle(state(run(events=[ev(5, "protective_absent")])))
        self.cycle(state(run(events=[ev(5, "protective_absent"), ev(9, "protective_not_created")])))
        self.cycle(state(run(events=[ev(5, "protective_absent"), ev(9, "protective_not_created")])))
        absent = [t for t in self.texts() if "не показує щойно поставленого стопа" in t]
        verdict = [t for t in self.texts() if "стоп на біржі не створено" in t]
        self.assertEqual((len(absent), len(verdict)), (1, 1))
        self.assertIn(("stop_absent", notifier.IMPORTANT), seen)
        self.assertIn(("stop_not_created", notifier.CRITICAL), seen)

    def test_the_admission_window_closing_is_told(self):
        self.launched()
        self.cycle(state(run(events=[ev(5, "admission_closed")])))
        self.assertTrue(any("вікно входів раунду aaaaaaaa · Live закрито" in t for t in self.texts()))


class Windows(NotifierCase):
    def window_texts(self):
        return [t for t in self.texts() if "вікно деградації" in t]

    def test_a_window_with_a_position_and_no_verified_stop_is_told_at_once(self):
        self.launched()
        self.cycle(state(run(bots=[bot(position=0.01)], events=[ev(5, "degraded_window_started", extended=False)])))
        self.assertTrue(any("без перевіреного стопа" in t for t in self.window_texts()))

    def test_a_window_with_a_position_over_120_seconds(self):
        self.launched()
        started = self.now
        self.cycle(state(run(bots=[bot(position=0.01)], events=[ev(5, "degraded_window_started", at=started, extended=True)])), seconds=60)
        self.assertEqual(self.window_texts(), [])
        self.cycle(seconds=65)
        self.assertEqual(self.window_texts(), [])  # 65 s by now: the 3 s spacing of the start message
        self.now = started + 121
        self.cycle()
        self.assertTrue(any("з позицією вже 121 с" in t for t in self.window_texts()))

    def test_a_flat_window_over_five_minutes_and_one_that_skipped_decisions(self):
        self.launched()
        started = self.now
        self.cycle(state(run(events=[ev(5, "degraded_window_started", at=started, extended=False)])))
        self.now = started + 301
        self.cycle()
        self.assertTrue(any("без позиції вже ≈ 5 хв" in t for t in self.window_texts()))
        self.cycle(state(run(events=[ev(6, "degraded_window_ended", outcome="recovered", skipped_decisions=6)])))
        self.assertTrue(any("пропущено рішень: 6" in t for t in self.window_texts()))

    def test_a_window_ended_by_recovery_or_pause_is_critical(self):
        self.launched()
        self.n.state["routine"] = [self.now] * notifier.ROUTINE_PER_HOUR  # the routine limit is full
        self.cycle(state(run(events=[ev(5, "degraded_window_started", extended=False),
                                     ev(6, "degraded_window_ended", outcome="recovery", skipped_decisions=0)])))
        self.assertTrue(any("закінчилось: recovery" in t for t in self.window_texts()))


class Done(NotifierCase):
    def test_the_end_of_a_round_sums_up_each_bot_and_why_it_is_incomplete(self):
        self.launched()
        self.cycle(state(run(events=[ev(5, "exchange_check_failed", reason="foreign_or_unbounded_pending_orders")])))
        self.panel.finals[RID] = {"bots": [{**bot(), "trades": 3, "pnl_usdt": -0.4567, "accounting_reconciled": True},
                                           {"id": "cash", "name": "Cash"}]}
        self.cycle(state(history=[{"id": RID, "context": "arena_live", "status": "DONE", "incomplete": True,
                                   "jev_usage": {"unknown_billing": 0}}]))
        done = [t for t in self.texts() if "завершено" in t]
        self.assertEqual(len(done), 1)
        self.assertIn("НЕПОВНИЙ (ручна звірка)", done[0])
        self.assertIn("· Opus V1: угод 3, net -0.46 USDT, облік звірено", done[0])
        self.assertIn("/api/exchange/runs/" + RID, self.panel.calls)

    def test_reasons_the_round_names_itself_come_first_and_free_text_never(self):
        self.launched()
        self.cycle(state(run()))
        self.panel.finals[RID] = {"bots": [], "incomplete_reasons": ["manual_review", "Free text! " + TOKEN]}
        self.cycle(state(history=[{"id": RID, "context": "arena_live", "status": "DONE", "incomplete": True}]))
        done = [t for t in self.texts() if "завершено" in t][0]
        self.assertIn("НЕПОВНИЙ (ручна звірка, unknown)", done)
        self.assertNotIn(TOKEN, done)

    def test_a_complete_round_says_so(self):
        self.launched()
        self.cycle(state(run()))
        self.panel.finals[RID] = {"bots": []}
        self.cycle(state(history=[{"id": RID, "context": "arena_live", "status": "DONE", "incomplete": False}]))
        self.assertTrue(any("завершено, повний." in t for t in self.texts()))


class PanelSilence(NotifierCase):
    def test_a_state_it_cannot_read_is_told_once_an_hour_and_it_goes_on(self):
        """N-01: an event of an unexpected shape among the last 500 no longer ends the process in a silent loop."""
        self.launched()
        broken = state(run(events=[{"seq": "x", "code": "admission_closed", "data": {}}]))
        self.cycle(broken)
        self.cycle(broken)
        told = [t for t in self.texts() if "не розібрав стан панелі" in t]
        self.assertEqual(len(told), 1)
        self.assertIn("(typeerror)", told[0])
        self.assertIn("parse typeerror", (self.root / notifier.STATE_DIR / "notifier.log").read_text(encoding="utf-8"))
        self.now += 3600
        self.cycle(broken)
        self.assertEqual(len([t for t in self.texts() if "не розібрав стан панелі" in t]), 2)
        self.cycle(state(run()))
        self.assertTrue(any("старт раунду" in t for t in self.texts()))  # and it goes on

    def test_three_failed_polls_then_one_message_and_one_when_it_answers(self):
        self.launched()
        self.panel.down = True
        self.cycle()
        self.cycle()
        self.assertFalse([t for t in self.texts() if "не відповідає" in t])
        self.cycle()
        self.cycle()
        silent = [t for t in self.texts() if "не відповідає" in t]
        self.assertEqual(len(silent), 1)
        self.panel.down = False
        self.cycle()
        self.cycle()
        self.assertEqual(len([t for t in self.texts() if "знову відповідає" in t]), 1)


class Alive(NotifierCase):
    def test_once_a_day_at_nine_kyiv(self):
        self.launched()  # 07:00 Kyiv: the launch message says it is alive for today
        self.now = datetime(2026, 11, 11, 7, 30, tzinfo=timezone.utc).timestamp()  # 09:30 Kyiv, the same day
        self.cycle()
        self.assertFalse(any("живий" in t for t in self.texts()))
        day2 = datetime(2026, 11, 12, 6, 59, tzinfo=timezone.utc).timestamp()  # 08:59 Kyiv (UTC+2)
        self.now = day2
        self.cycle()
        self.assertFalse(any("живий" in t for t in self.texts()))
        self.now = day2 + 60
        self.cycle()
        self.cycle()
        self.assertEqual(len([t for t in self.texts() if "сповіщувач живий" in t]), 1)

    def test_kyiv_time_and_its_summer_change(self):
        utc = lambda *a: datetime(*a, tzinfo=timezone.utc)
        self.assertEqual(notifier.kyiv(utc(2026, 7, 1, 6, 0)).hour, 9)
        self.assertEqual(notifier.kyiv(utc(2026, 11, 11, 7, 0)).hour, 9)
        self.assertEqual(notifier.kyiv(utc(2026, 3, 29, 0, 59)) - utc(2026, 3, 29, 0, 59), timedelta(hours=2))
        self.assertEqual(notifier.kyiv(utc(2026, 3, 29, 1, 0)) - utc(2026, 3, 29, 1, 0), timedelta(hours=3))
        self.assertEqual(notifier.kyiv(utc(2026, 10, 25, 0, 59)) - utc(2026, 10, 25, 0, 59), timedelta(hours=3))
        self.assertEqual(notifier.kyiv(utc(2026, 10, 25, 1, 0)) - utc(2026, 10, 25, 1, 0), timedelta(hours=2))


class Limits(NotifierCase):
    def test_spacing_routine_limit_and_a_pause_past_it(self):
        """Plan check p. 3: 20 routine messages, then a pause goes out at once, not in the next hour."""
        for i in range(25):
            self.n.enqueue("r%d" % i, ROUTINE, "admission_closed", "routine %d" % i)
        self.drain()
        self.assertEqual(len(self.tg.sent), notifier.ROUTINE_PER_HOUR)
        times = [t for t, _, _ in self.tg.sent]
        self.assertTrue(all(b - a >= notifier.SEND_SPACING for a, b in zip(times, times[1:])))
        self.n.enqueue("pause", CRITICAL, "pause", "ПАУЗА now")
        self.n.enqueue("done", IMPORTANT, "done", "summary now")
        self.drain()
        self.assertEqual(self.texts()[-2:], ["ПАУЗА now", "summary now"])
        self.now += 3600
        self.drain()
        self.assertEqual(len([t for t in self.texts() if t.startswith("routine")]), 25)

    def test_no_second_message_before_the_spacing(self):
        self.n.enqueue("one", CRITICAL, "pause", "one")
        self.n.enqueue("two", CRITICAL, "pause", "two")
        self.assertEqual(self.n.send_next(), notifier.SEND_SPACING)
        self.now += notifier.SEND_SPACING - 0.5
        self.assertEqual(self.n.send_next(), 0.5)
        self.assertEqual(self.texts(), ["one"])

    def test_a_full_queue_counts_what_it_dropped_and_tells_it(self):
        for i in range(notifier.QUEUE_LIMIT + 10):
            self.n.enqueue("r%d" % i, ROUTINE, "admission_closed", "routine %d" % i)
        self.assertEqual(len(self.n.state["queue"]), notifier.QUEUE_LIMIT)
        for _ in range(4):
            self.drain()
            self.now += 3600
        self.assertTrue(any("пропущено 10 рутинних подій" in t for t in self.texts()))

    def test_telegram_failures_back_off_and_a_bad_message_never_blocks_the_rest(self):
        self.n.enqueue("bad", CRITICAL, "pause", "x")
        self.n.enqueue("good", CRITICAL, "pause", "y")
        self.tg.fail = [SendFailed("http_429", 7), SendFailed("network"), SendFailed("http_400")]
        self.assertEqual(self.n.send_next(), 7)
        self.now += 7
        self.assertEqual(self.n.send_next(), 5)
        self.now += 5
        self.n.send_next()  # 400: dropped
        self.now += 3
        self.n.send_next()
        self.assertEqual(self.texts(), ["y"])


class Privacy(NotifierCase):
    def test_no_account_order_id_token_or_chat_id_in_any_message_or_the_log(self):
        self.n = Notifier(self.root, self.panel, self.tg, 424242, clock=lambda: self.now)  # the real log file
        self.cycle(state())
        events = [ev(5, "exchange_check_failed", reason="actual_position_differs_from_owned_fills"),
                  ev(6, "protective_recovery"), ev(7, "admission_closed"),
                  ev(8, "degraded_window_started", extended=False), ev(9, "degraded_window_ended", outcome="recovery"),
                  ev(10, "stop_requested", reason_code="manual_review")]
        self.cycle(state(run(status="PAUSED", bots=[bot(position=0.01)], events=events)))
        self.panel.finals[RID] = {"bots": [{**bot(), "trades": 1, "pnl_usdt": 1, "accounting_reconciled": False}]}
        self.cycle(state(history=[{"id": RID, "context": "arena_live", "status": "DONE", "incomplete": True}]))
        everything = "\n".join(self.texts()) + (self.root / notifier.STATE_DIR / "notifier.log").read_text(encoding="utf-8")
        self.assertGreater(len(self.tg.sent), 6)
        for secret in SECRETS:
            self.assertNotIn(secret, everything)
        self.assertNotIn("Jev Arena", (self.root / notifier.STATE_DIR / "notifier.log").read_text(encoding="utf-8"))


class TelegramTransport(unittest.TestCase):
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(self, *outcomes):
        seen = []

        def open_(request, timeout=None):
            seen.append(request)
            outcome = outcomes[len(seen) - 1]
            if isinstance(outcome, Exception):
                raise outcome
            return self.Response(json.dumps(outcome).encode())
        return types.SimpleNamespace(open=open_), seen

    def test_only_telegram_https_post_plain_text(self):
        opener, seen = self.opener({"ok": True, "result": {}})
        Telegram(TOKEN, opener).send(424242, "Назва_з *зірками* <і> дужками")
        request = seen[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(urllib.request.urlparse(request.full_url).hostname, "api.telegram.org")
        self.assertTrue(request.full_url.startswith("https://"))
        payload = json.loads(request.data)
        self.assertEqual(set(payload), {"chat_id", "text", "disable_web_page_preview"})  # no parse_mode (plan check p. 5)

    def test_the_default_opener_has_no_proxy_and_follows_no_redirect(self):
        with patch.dict("os.environ", {"HTTPS_PROXY": "http://proxy.example:3128", "HTTP_PROXY": "http://proxy.example:3128"}):
            opener = notifier.telegram_opener()  # the empty ProxyHandler keeps urllib's default one (environment proxies) out
        self.assertFalse([h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler) and h.proxies])
        redirect = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
        self.assertTrue(redirect and all(isinstance(h, notifier.NoRedirect) for h in redirect))
        self.assertIsNone(notifier.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://elsewhere"))

    def test_failures_are_categories_without_the_token(self):
        body = io.BytesIO(json.dumps({"ok": False, "parameters": {"retry_after": 7}}).encode())
        cases = [(urllib.error.HTTPError("https://x/bot" + TOKEN, 401, "Unauthorized", {}, io.BytesIO(b"{}")), "http_401", None),
                 (urllib.error.HTTPError("https://x/bot" + TOKEN, 429, "Too Many", {}, body), "http_429", 7),
                 (urllib.error.URLError(ssl.SSLError("bad cert")), "tls", None),
                 (urllib.error.URLError(socket.timeout("slow")), "timeout", None),
                 (urllib.error.URLError("no route " + TOKEN), "network", None), (TimeoutError(), "timeout", None)]
        for raised, category, retry in cases:
            with self.subTest(category=category):
                opener, _ = self.opener(raised)
                with self.assertRaises(SendFailed) as caught:
                    Telegram(TOKEN, opener).send(1, "x")
                self.assertEqual((caught.exception.category, caught.exception.retry_after), (category, retry))
                self.assertNotIn(TOKEN, str(caught.exception) + repr(caught.exception))
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__)
        self.assertNotIn(TOKEN, repr(Telegram(TOKEN)))

    def test_a_failing_test_command_prints_no_token(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        (Path(temp.name) / notifier.CONFIG_NAME).write_text("TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=424242\n" % TOKEN, encoding="utf-8")
        for raised in (urllib.error.HTTPError("https://api.telegram.org/bot" + TOKEN + "/sendMessage", 401, "x", {}, io.BytesIO(b"{}")),
                       urllib.error.URLError(ssl.SSLError("x"))):
            opener, _ = self.opener(raised)
            out, err = io.StringIO(), io.StringIO()
            hooks = (sys.excepthook, threading.excepthook)
            try:
                with patch("sys.stdout", out), patch("sys.stderr", err):
                    self.assertEqual(notifier.main(["test"], root=temp.name, opener=opener), 1)
            finally:
                sys.excepthook, threading.excepthook = hooks
            self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())
            self.assertNotIn("424242", out.getvalue() + err.getvalue())

    def test_an_unhandled_exception_writes_only_its_type(self):
        """Plan check p. 2: sys.excepthook and threading.excepthook."""
        hooks, stream = (sys.excepthook, threading.excepthook), io.StringIO()
        try:
            notifier.quiet_hooks(stream)
            sys.excepthook(ValueError, ValueError("https://api.telegram.org/bot" + TOKEN), None)
            threading.excepthook(types.SimpleNamespace(exc_type=OSError, exc_value=OSError(TOKEN), exc_traceback=None, thread=None))
        finally:
            sys.excepthook, threading.excepthook = hooks
        self.assertEqual(stream.getvalue(), "arena_notifier: unexpected ValueError\narena_notifier: unexpected OSError\n")


class Config(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / notifier.CONFIG_NAME

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        hooks = (sys.excepthook, threading.excepthook)
        try:
            with patch("sys.stdout", out), patch("sys.stderr", err):
                code = notifier.main(list(argv), root=self.root)
        finally:
            sys.excepthook, threading.excepthook = hooks
        return code, out.getvalue() + err.getvalue()

    def test_missing_or_wrong_values_stop_with_one_line_and_no_value(self):
        code, text = self.main("run")
        self.assertEqual((code, text.count("\n")), (2, 1))
        self.file.write_text("TELEGRAM_BOT_TOKEN=not-a-token\nTELEGRAM_CHAT_ID=1\n", encoding="utf-8")
        code, text = self.main("run")
        self.assertEqual(code, 2)
        self.assertNotIn("not-a-token", text)
        self.file.write_text("TELEGRAM_BOT_TOKEN=%s\n" % TOKEN, encoding="utf-8")
        self.assertEqual(self.main("run")[0], 2)  # no chat yet: only find-chat may run
        self.file.write_text("TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=12ab\n" % TOKEN, encoding="utf-8")
        self.assertEqual(self.main("run")[0], 2)

    def test_a_bom_from_notepad_and_quotes_are_dropped(self):
        self.file.write_bytes(("\ufeffTELEGRAM_BOT_TOKEN=\"%s\"\r\n# a note\r\nTELEGRAM_CHAT_ID=-1001\r\n" % TOKEN).encode("utf-8"))
        self.assertEqual(notifier.read_config(self.root), (TOKEN, -1001))

    def test_one_process_at_a_time(self):
        self.file.write_text("TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=1\n" % TOKEN, encoding="utf-8")
        held = notifier.Lock(self.root)
        self.assertTrue(held.acquire())
        try:
            self.assertFalse(notifier.Lock(self.root).acquire())
            self.assertTrue(notifier.running(self.root))
            code, text = self.main("run")
            self.assertEqual(code, 0)
            self.assertIn("уже працює", text)
            popen = Mock()
            self.assertEqual(notifier.spawn(self.root, 8765, popen), "running")
            popen.assert_not_called()
        finally:
            held.release()
        self.assertFalse(notifier.running(self.root))


class Launch(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_spawn_only_with_the_file_detached_and_never_reading_values(self):
        popen = Mock()
        self.assertEqual(notifier.spawn(self.root, 8765, popen), "not_configured")
        popen.assert_not_called()
        (self.root / notifier.CONFIG_NAME).write_text("anything\n", encoding="utf-8")  # the child checks the values
        self.assertEqual(notifier.spawn(self.root, 8800, popen), "started")
        argv, kwargs = popen.call_args
        self.assertEqual(argv[0][-4:], [str(self.root / "tools" / "arena_notifier.py"), "run", "--port", "8800"][-4:])
        self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
        self.assertIn("creationflags" if notifier.os.name == "nt" else "start_new_session", kwargs)

    def start(self, command, answer):
        """main(start|restart) with Telegram answering getMe by `answer`; spawn and the stop are recorded, never real."""
        (self.root / notifier.CONFIG_NAME).write_text("TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=1\n" % TOKEN, encoding="utf-8")

        def open_(request, timeout=None):
            if isinstance(answer, Exception):
                raise answer
            return TelegramTransport.Response(json.dumps(answer).encode())
        out, err, spawned = io.StringIO(), io.StringIO(), []
        hooks = (sys.excepthook, threading.excepthook)
        try:
            with patch("sys.stdout", out), patch("sys.stderr", err), \
                    patch.object(notifier, "spawn", lambda root, port: spawned.append(port) or "started"), \
                    patch.object(notifier, "stop_running", lambda root: "not_running"):
                code = notifier.main([command], root=self.root, opener=types.SimpleNamespace(open=open_))
        finally:
            sys.excepthook, threading.excepthook = hooks
        return code, out.getvalue() + err.getvalue(), spawned

    def test_a_refused_token_is_said_in_the_console_and_nothing_starts(self):
        """D173 p. 3 (N-02): a revoked or mistyped token shows in NOTIFIER_START.cmd's window; the token never does."""
        refused = urllib.error.HTTPError("https://api.telegram.org/bot" + TOKEN + "/getMe", 401, "Unauthorized", {}, io.BytesIO(b"{}"))
        for command in ("start", "restart"):
            code, text, spawned = self.start(command, refused)
            self.assertEqual((code, spawned), (1, []))
            self.assertIn("Telegram не прийняв токен (http_401)", text)
            self.assertNotIn(TOKEN, text)
        code, text, spawned = self.start("restart", urllib.error.URLError("no route"))
        self.assertEqual((code, spawned), (0, [8765]))  # no network now: it starts and tries again itself
        self.assertIn("Telegram зараз недоступний (network)", text)
        code, text, spawned = self.start("restart", {"ok": True, "result": {"is_bot": True}})
        self.assertEqual((code, spawned, text.strip()), (0, [8765], "Сповіщувач запущено."))

    def test_a_stale_pid_of_another_process_is_never_touched(self):
        """D173 p. 2 (N-03): restart kills nothing; an old notifier.pid naming another process (here this test's own) is
        ignored; only the stop flag asks the lock holder to end."""
        (self.root / notifier.STATE_DIR).mkdir(parents=True, exist_ok=True)
        (self.root / notifier.STATE_DIR / "notifier.pid").write_text(str(notifier.os.getpid()), encoding="ascii")
        held = notifier.Lock(self.root)
        self.assertTrue(held.acquire())
        with patch.object(notifier.os, "kill", side_effect=AssertionError("no kill")) as kill:
            self.assertEqual(notifier.stop_running(self.root, wait=lambda s: held.release()), "stopped")
        kill.assert_not_called()
        self.assertTrue((self.root / notifier.STATE_DIR / "notifier.pid").exists())

    def test_restart_asks_the_holder_to_end_by_a_flag_and_kills_nothing(self):
        """N-03: no PID, no kill; the flag is gone afterwards either way."""
        flag = self.root / notifier.STATE_DIR / "stop.flag"
        self.assertEqual(notifier.stop_running(self.root), "not_running")
        self.assertFalse(flag.exists())
        held = notifier.Lock(self.root)
        self.assertTrue(held.acquire())
        seen = []

        def holder_sees_the_flag(seconds):
            seen.append(flag.exists())
            held.release()
        self.assertEqual(notifier.stop_running(self.root, wait=holder_sees_the_flag), "stopped")
        self.assertEqual((seen, flag.exists()), ([True], False))
        self.assertTrue(held.acquire())
        try:
            now = [0.0]
            self.assertEqual(notifier.stop_running(self.root, wait=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0]),
                             "still_running")
            self.assertFalse(flag.exists())
        finally:
            held.release()
        self.assertFalse(hasattr(notifier, "signal"))

    def test_the_loop_ends_on_the_flag_and_a_new_run_clears_a_stale_one(self):
        (self.root / notifier.CONFIG_NAME).write_text("TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=1\n" % TOKEN, encoding="utf-8")
        flag = self.root / notifier.STATE_DIR / "stop.flag"
        n = Notifier(self.root, FakePanel(), FakeTelegram(types.SimpleNamespace(now=0)), 1, clock=lambda: T0)
        polls = []
        n.poll = lambda: polls.append(1)
        flag.write_text("stop", encoding="ascii")
        n.run_forever(sleep=lambda s: None)
        self.assertEqual(polls, [])
        flag.unlink()
        n.run_forever(sleep=lambda s: flag.write_text("stop", encoding="ascii"))  # asked while it waits between polls
        self.assertEqual(polls, [1])
        self.assertIn("stop asked", (self.root / notifier.STATE_DIR / "notifier.log").read_text(encoding="utf-8"))
        runs = []
        with patch.object(notifier.Notifier, "run_forever", lambda self, sleep=None: runs.append(flag.exists())):
            hooks = (sys.excepthook, threading.excepthook)
            try:
                self.assertEqual(notifier.main(["run"], root=self.root), 0)
            finally:
                sys.excepthook, threading.excepthook = hooks
        self.assertEqual(runs, [False])  # the stale flag of an interrupted restart was cleared first

    def test_a_failing_step_is_journalled_and_the_loop_goes_on(self):
        """N-01: run_forever does not end on a step's exception."""
        flag = self.root / notifier.STATE_DIR / "stop.flag"
        n = Notifier(self.root, FakePanel(), FakeTelegram(types.SimpleNamespace(now=0)), 1, clock=lambda: T0)
        calls = []

        def poll():
            calls.append(1)
            if len(calls) == 1:
                raise OSError("disk full")
            flag.write_text("stop", encoding="ascii")
        n.poll = poll
        ticks = [T0]
        n.clock = lambda: ticks[0]
        n.run_forever(sleep=lambda s: ticks.__setitem__(0, ticks[0] + 61))
        self.assertEqual(len(calls), 2)
        self.assertIn("loop oserror", (self.root / notifier.STATE_DIR / "notifier.log").read_text(encoding="utf-8"))

    def test_a_bad_file_says_why_in_the_journal(self):
        """N-02: the run the watch starts ends at once on a bad file; the journal names the reason, never a value."""
        (self.root / notifier.CONFIG_NAME).write_text("TELEGRAM_BOT_TOKEN=revoked-or-wrong\n", encoding="utf-8")
        hooks = (sys.excepthook, threading.excepthook)
        try:
            with patch("sys.stderr", io.StringIO()):
                self.assertEqual(notifier.main(["run"], root=self.root), 2)
        finally:
            sys.excepthook, threading.excepthook = hooks
        journal = (self.root / notifier.STATE_DIR / "notifier.log").read_text(encoding="utf-8")
        self.assertIn("config not_configured_token", journal)
        self.assertNotIn("revoked-or-wrong", journal)

    def test_the_watch_tells_a_notifier_that_ends_at_once_and_slows_down(self):
        """N-02: three starts in a row that do not hold the lock: one line in the panel's log, then a try every 10 min."""
        (self.root / notifier.CONFIG_NAME).write_text("x\n", encoding="utf-8")
        popen, stop, waits, lines = Mock(), threading.Event(), [], []

        def sleep(seconds):
            waits.append(seconds)
            if len(waits) == 21:
                stop.set()
        notifier.watch(self.root, 8765, popen=popen, stop=stop, sleep=sleep, out=lines.append)
        self.assertEqual(popen.call_count, 5)  # minutes 0, 1, 2, then 10 and 20
        self.assertEqual(lines, ["Telegram notifier: it ends right after each start; see runtime/notifier/notifier.log."])

    def test_the_watch_starts_it_again_each_minute(self):
        (self.root / notifier.CONFIG_NAME).write_text("x\n", encoding="utf-8")
        popen, stop, waits = Mock(), threading.Event(), []

        def sleep(seconds):
            waits.append(seconds)
            if len(waits) == 3:
                stop.set()
        notifier.watch(self.root, 8765, popen=popen, stop=stop, sleep=sleep)
        self.assertEqual((popen.call_count, waits), (3, [60, 60, 60]))

    def test_the_panel_runner_starts_the_watch_only_with_the_file(self):
        spec = importlib.util.spec_from_file_location("run_server_for_test", ROOT / "deployment" / "server" / "run_server.py")
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        lines, threads = [], []

        class Thread:
            def __init__(self, target, args, name, daemon):
                threads.append((target, args, daemon))

            def start(self):
                pass
        self.assertIsNone(runner.start_notifier(self.root, 8765, thread=Thread, out=lines.append))
        self.assertEqual((threads, lines), ([], ["Telegram notifier: not configured (NOTIFIER_TELEGRAM_UA.md)."]))
        (self.root / notifier.CONFIG_NAME).write_text("x\n", encoding="utf-8")
        runner.start_notifier(self.root, 8765, thread=Thread, out=lines.append)
        self.assertEqual(threads, [(notifier.watch, (self.root, 8765), True)])


class FindChat(unittest.TestCase):
    """Plan check p. 1: the one-time code; a /start or any other text, or a group, never binds a chat."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / notifier.CONFIG_NAME).write_text("TELEGRAM_BOT_TOKEN=%s\n" % TOKEN, encoding="utf-8")
        self.now, self.code, self.lines, self.calls = 0.0, None, [], []

    def out(self, line):
        self.lines.append(line)
        if "код:" in line:
            self.code = line.rsplit(" ", 1)[-1]

    def bot(self, answer):
        case = self

        class Bot:
            def call(self, method, payload, timeout=None):
                case.calls.append(dict(payload))
                case.now += 20
                return answer(payload)
        return Bot()

    @staticmethod
    def message(uid, chat, text, kind="private"):
        return {"update_id": uid, "message": {"chat": {"id": chat, "type": kind, "first_name": "Owner"}, "text": text}}

    def test_only_the_code_from_a_private_chat_binds_it(self):
        def answer(payload):
            if payload.get("timeout") == 0 and "offset" not in payload:
                return [self.message(5, 666, "/start")]  # a stranger before the code was shown
            if payload.get("offset") == 6 and payload.get("timeout") == 20:
                return [self.message(6, 777, "/start"), self.message(7, 888, self.code, "group"),
                        self.message(8, 999, "hello"), self.message(9, 31337, self.code)]
            return []
        bound = notifier.find_chat(self.root, self.bot(answer), clock=lambda: self.now, out=self.out)
        self.assertTrue(bound)
        self.assertEqual(notifier.read_config(self.root), (TOKEN, 31337))
        self.assertEqual(self.calls[-1], {"offset": 10, "timeout": 0})  # acknowledged
        printed = "\n".join(self.lines)
        self.assertIn("Чат знайдено: Owner", printed)
        self.assertNotIn("31337", printed)
        self.assertNotIn(TOKEN, printed)

    def test_no_code_in_time_writes_nothing(self):
        def answer(payload):
            return [self.message(1, 666, "/start"), self.message(2, 666, "JA-WRONGCOD")] if payload.get("timeout") == 20 else []
        self.assertFalse(notifier.find_chat(self.root, self.bot(answer), clock=lambda: self.now, out=self.out, seconds=100))
        self.assertNotIn("TELEGRAM_CHAT_ID", (self.root / notifier.CONFIG_NAME).read_text(encoding="utf-8"))
        self.assertEqual(len(self.code), len("JA-") + 8)


class PanelOverHttp(unittest.TestCase):
    """The notifier's only requests to the panel are GETs of the state and of a finished round."""

    def setUp(self):
        self.seen = []
        case = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def answer(self):
                case.seen.append((self.command, self.path, self.headers.get("Host"), self.headers.get("X-Viewer-Poll")))
                body = json.dumps(state(run()) if self.path == "/api/exchange/state" else {"bots": []}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            do_GET = do_POST = answer

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_gets_only(self):
        port = self.server.server_port
        panel = notifier.Panel(port)
        n = Notifier(self.temp.name, panel, FakeTelegram(types.SimpleNamespace(now=0)), 1, clock=lambda: T0)
        n.poll()
        n.poll()
        panel.get("/api/exchange/runs/" + RID)
        for path in ("/api/exchange/start", "/api/exchange/stop", "/api/exchange/runs/../x", "/api/ops/state"):
            with self.assertRaises(ValueError):
                panel.get(path)
        self.assertEqual({c for c, *_ in self.seen}, {"GET"})
        self.assertEqual({p for _, p, *_ in self.seen}, {"/api/exchange/state", "/api/exchange/runs/" + RID})
        self.assertTrue(all(h == "127.0.0.1:%d" % port for _, _, h, _ in self.seen))
        self.assertTrue(all(v == "1" for _, p, _, v in self.seen if p == "/api/exchange/state"))


class Boundaries(unittest.TestCase):
    def test_outside_every_core_hash(self):
        from trading_lab import arena, battle_export, exchange_lab, strategy_training
        for files in (exchange_lab.SOURCE_FILES, strategy_training.SOURCE_FILES, arena.SOURCE_FILES, battle_export.EXPORTER_FILES):
            self.assertTrue(all("/" not in f and "\\" not in f and "notifier" not in f for f in files))

    def test_the_kit_ships_the_notifier_and_no_secret_file(self):
        from tools import build_server_bundle as bundle
        for name in ("tools/arena_notifier.py", "NOTIFIER_SETUP.cmd", "NOTIFIER_START.cmd"):
            self.assertIn(name, bundle.EXTRA)
            self.assertTrue((ROOT / name).is_file())
        self.assertTrue((ROOT / "03_For_Client_Deliverables" / "NOTIFIER_TELEGRAM_UA.md").is_file())
        self.assertFalse(any(n.startswith(".env") for n in bundle.EXTRA))

    def test_the_guard_blocks_the_secret_file_and_the_secret_scan_knows_the_token_key(self):
        event = {"tool_name": "Bash", "tool_input": {"command": "cat " + notifier.CONFIG_NAME}, "cwd": "C:/Projects/Trading"}
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        result = subprocess.run(["node", str(ROOT / "team" / "tools" / "guard.cjs")], input=json.dumps(event).encode(),
                                capture_output=True, env={**__import__("os").environ, "CLAUDE_PROJECT_DIR": "", "TEAM_WORKTREES_ROOT": temp.name})
        self.assertEqual(result.returncode, 2)
        self.assertIn("'TELEGRAM_BOT_TOKEN'", (ROOT / "team" / "tools" / "secret_scan.cjs").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
