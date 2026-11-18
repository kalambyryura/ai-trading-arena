"""Arena notifier (D167): a separate process next to the panel on the other PC that writes the owner in Telegram.

It reads only the local panel's GET routes and sends only to api.telegram.org: never a POST to the panel, a button, an
order, an OKX key or a file of a round. The bot token and the chat id live in the ignored file .env.telegram.local of the
application folder, read only here and never printed or logged. Plan: team/platform/NOTIFIER_PLAN_UA.md; its check:
team/reviews/NOTIFIER_PLAN_CHECK_UA.md. Outside the core hashes (not in any SOURCE_FILES): no requalification.

Commands (owner-run on the other PC; the panel's run_server.py starts `run` itself when the file is there):
  run        watch the panel and write (the default; one process at a time, runtime/notifier/notifier.lock)
  start      start `run` detached unless one runs already, then return
  restart    ask the running one to end (runtime/notifier/stop.flag, no process is killed), then start a new one: after a
             kit update or a new token (NOTIFIER_START.cmd runs this)
  find-chat  bind the owner's chat with a one-time code sent to the bot (writes TELEGRAM_CHAT_ID into the file)
  test       send one test message
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CONFIG_NAME = ".env.telegram.local"
STATE_DIR = Path("runtime") / "notifier"
TELEGRAM_HOST = "api.telegram.org"
POLL_SECONDS, PANEL_TIMEOUT, SEND_TIMEOUT = 60, 10, 10
SILENT_POLLS = 3  # three failed polls in a row (about 3 minutes): the panel does not answer
SEND_SPACING = 3  # seconds between two messages, every kind (Telegram allows about one a second in a chat)
ROUTINE_PER_HOUR = 20  # routine messages only; the critical ones never wait for this (plan check p. 3)
QUEUE_LIMIT = 50
FIND_CHAT_SECONDS = 300
ALIVE_HOUR = 9  # Kyiv
WINDOW_WITH_POSITION, WINDOW_FLAT, SKIPPED_DECISIONS = 120, 300, 5  # D166 p. 2, as far as the journal tells today
CRITICAL, IMPORTANT, ROUTINE = 0, 1, 2
CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
TOKEN = re.compile(r"\d{5,16}:[A-Za-z0-9_-]{30,64}")
CHAT = re.compile(r"-?\d{1,20}")
RUN_ID = re.compile(r"[0-9a-f]{32}")
CONTEXTS = {"arena_live": "Live", "workshop_demo": "Demo"}
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP (Windows)

try:  # the codes that mean the manual review; a missing module leaves only the season-wide stop code to tell it
    sys.path.insert(0, str(ROOT))
    from trading_lab.exchange_diagnostics import MANUAL_REVIEW_REASONS
except Exception:  # noqa: BLE001 - the notifier must start even if the trading code cannot be imported
    MANUAL_REVIEW_REASONS = frozenset()


class NotConfigured(Exception):
    """The secret file is missing or a value has the wrong form; the message never holds a value."""


class SendFailed(Exception):
    """A Telegram call failed; only a category (http_401, http_429, timeout, network, tls, answer) and retry_after."""

    def __init__(self, category, retry_after=None):
        super().__init__(category)
        self.category, self.retry_after = category, retry_after


def quiet_hooks(stream=None):
    """Plan check p. 2: an exception nobody handled writes only its type, never its text (a URL holds the token)."""
    def write(kind):
        try:
            (stream or sys.stderr).write("arena_notifier: unexpected " + kind + "\n")
        except Exception:  # noqa: BLE001
            pass
    sys.excepthook = lambda kind, value, trace: write(kind.__name__)
    threading.excepthook = lambda args: write(args.exc_type.__name__)


# ---- the secret file ----

def read_config(root, *, need_chat=True):
    """TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from .env.telegram.local (KEY=VALUE lines; a BOM that Notepad adds is
    dropped). Raises NotConfigured with a reason word only."""
    try:
        text = (Path(root) / CONFIG_NAME).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        raise NotConfigured("file") from None
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    token, chat = values.get("TELEGRAM_BOT_TOKEN", ""), values.get("TELEGRAM_CHAT_ID", "")
    if not TOKEN.fullmatch(token):
        raise NotConfigured("token")
    if chat and not CHAT.fullmatch(chat):
        raise NotConfigured("chat")
    if need_chat and not chat:
        raise NotConfigured("chat")
    return token, (int(chat) if chat else None)


def write_chat(root, chat_id):
    """The chat id into the secret file, every other line kept; through a temporary file and a replace."""
    path = Path(root) / CONFIG_NAME
    lines = [line for line in path.read_text(encoding="utf-8-sig").splitlines()
             if not line.strip().startswith("TELEGRAM_CHAT_ID")]
    lines.append("TELEGRAM_CHAT_ID=" + str(int(chat_id)))
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def log_line(root, kind, outcome):
    """A line of runtime/notifier/notifier.log (time, kind, category); never a value, a text, a token or a chat id."""
    path = Path(root) / STATE_DIR / "notifier.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 1 << 20:
            for index in (2, 1):
                older = path.with_name("notifier.log.%d" % index)
                if older.exists():
                    os.replace(older, path.with_name("notifier.log.%d" % (index + 1)))
            os.replace(path, path.with_name("notifier.log.1"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write("%s %s %s\n" % (datetime.now(timezone.utc).isoformat(timespec="seconds"), code(kind), code(outcome)))
    except OSError:
        pass


# ---- Telegram ----

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a redirect becomes an HTTPError: the token never goes to another address


def telegram_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                       urllib.request.HTTPSHandler(context=ssl.create_default_context()))


class Telegram:
    """api.telegram.org only, HTTPS with the system certificates, no proxy, no redirect. Plain text, no parse_mode
    (plan check p. 5): a strategy name with _ * < stays text."""

    def __init__(self, token, opener=None):
        self._token = token
        self._opener = opener or telegram_opener()

    def __repr__(self):
        return "Telegram(<hidden>)"

    def call(self, method, payload, timeout=SEND_TIMEOUT):
        request = urllib.request.Request("https://" + TELEGRAM_HOST + "/bot" + self._token + "/" + method,
                                         data=json.dumps(payload).encode("utf-8"),
                                         headers={"Content-Type": "application/json"}, method="POST")
        try:
            with self._opener.open(request, timeout=timeout) as response:
                answer = json.loads(response.read(1 << 20))
        except urllib.error.HTTPError as error:
            retry = None
            if error.code == 429:
                try:
                    retry = int(json.loads(error.read(65536))["parameters"]["retry_after"])
                except Exception:  # noqa: BLE001
                    retry = None
            raise SendFailed("http_%d" % error.code, retry) from None
        except urllib.error.URLError as error:
            reason = error.reason
            category = ("tls" if isinstance(reason, ssl.SSLError) else
                        "timeout" if isinstance(reason, (TimeoutError, socket.timeout)) else "network")
            raise SendFailed(category) from None
        except ssl.SSLError:
            raise SendFailed("tls") from None
        except (TimeoutError, socket.timeout):
            raise SendFailed("timeout") from None
        except (OSError, ValueError):
            raise SendFailed("network") from None
        if not isinstance(answer, dict) or answer.get("ok") is not True:
            raise SendFailed("answer")
        return answer.get("result")

    def send(self, chat_id, text):
        return self.call("sendMessage", {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True})


# ---- the panel ----

class Panel:
    """GET only, of /api/exchange/state (the viewers' shared copy) and, once a round ends, /api/exchange/runs/<id>."""

    def __init__(self, port=8765, opener=None):
        self.base, self.host = "http://127.0.0.1:%d" % port, "127.0.0.1:%d" % port
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def get(self, path):
        if not (path == "/api/exchange/state" or path.startswith("/api/exchange/runs/") and RUN_ID.fullmatch(path.rsplit("/", 1)[-1])):
            raise ValueError("route")
        headers = {"Host": self.host}
        if path == "/api/exchange/state":
            headers["X-Viewer-Poll"] = "1"  # the copy the open pages share: no extra journal read for the panel
        request = urllib.request.Request(self.base + path, headers=headers, method="GET")
        with self.opener.open(request, timeout=PANEL_TIMEOUT) as response:
            return json.loads(response.read(1 << 28))


# ---- time ----

def kyiv(utc):
    """Kyiv wall time: UTC+3 from the last Sunday of March 01:00 UTC to the last Sunday of October 01:00 UTC, else UTC+2."""
    def last_sunday(month):
        day = datetime(utc.year, month + 1, 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
        return day - timedelta(days=(day.weekday() + 1) % 7)
    return utc + timedelta(hours=3 if last_sunday(3) <= utc < last_sunday(10) else 2)


def parse_at(value):
    try:
        moment = datetime.fromisoformat(str(value))
        return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---- what a message may hold (plan p. 5): never an account, UID, order id, balance, key, token or chat id ----

def code(value):
    return value if isinstance(value, str) and CODE.fullmatch(value) else "unknown"


def name(value):
    text = "".join(ch for ch in str(value or "?") if ch.isprintable())
    return (text[:60] + "…") if len(text) > 60 else text


def money(value):
    try:
        return "%+.2f" % float(value)
    except (TypeError, ValueError):
        return "—"


def run_label(run):
    return "%s · %s" % (str(run.get("id", ""))[:8], CONTEXTS.get(run.get("context"), "?"))


INCOMPLETE_HINTS = {"manual_review": "ручна звірка", "pause": "пауза", "recovery": "відновлення закриття (Resume)",
                    "protective_recovery": "бота зупинено захистом", "settled_account_unverified": "звірку до паузи не підтверджено",
                    "unknown_billing": "невідома вартість Jev", "observation_limit": "ліміт спостережень",
                    "account_unavailable": "три збої перевірки рахунку поспіль", "degraded_window": "вікно деградації"}


class Notifier:
    def __init__(self, root, panel, telegram, chat_id, *, clock=time.time, log=None):
        self.root, self.panel, self.telegram, self.chat_id, self.clock = Path(root), panel, telegram, chat_id, clock
        self.dir = self.root / STATE_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.dir / "notifier.log"
        self.log = log or self.write_log
        path = self.dir / "state.json"
        try:
            self.state = json.loads(path.read_text(encoding="utf-8"))
            self.first = False
        except (OSError, ValueError):
            self.state, self.first = {}, True
        for key, default in (("runs", {}), ("sent", {}), ("queue", []), ("routine", []), ("failures", 0),
                             ("silent_since", None), ("silent_told", False), ("alive_date", ""), ("skipped", 0),
                             ("next_send", 0), ("backoff", 0), ("order", 0), ("last_ok", None)):
            self.state.setdefault(key, default)

    # -- journal, state --
    def write_log(self, kind, outcome):
        """A line with the time, the kind and an outcome category: never a message text, a token or a chat id."""
        log_line(self.root, kind, outcome)

    def save(self):
        now = self.clock()
        self.state["sent"] = {k: v for k, v in self.state["sent"].items() if now - v < 7 * 86400}
        path, temporary = self.dir / "state.json", self.dir / "state.json.tmp"
        temporary.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)

    def enqueue(self, key, priority, kind, text):
        """Once per key. Routine messages beyond the queue's room are counted and told in one summary later."""
        if key in self.state["sent"]:
            return
        self.state["sent"][key] = self.clock()
        queue = self.state["queue"]
        if len(queue) >= QUEUE_LIMIT:
            routine = [item for item in queue if item["priority"] == ROUTINE]
            if priority == ROUTINE or not routine:
                self.state["skipped"] += 1
                return
            queue.remove(routine[0])
            self.state["skipped"] += 1
        self.state["order"] += 1
        queue.append({"priority": priority, "order": self.state["order"], "kind": kind, "text": text})

    # -- the panel --
    def poll(self):
        try:
            exchange = self.panel.get("/api/exchange/state")
            if not isinstance(exchange, dict) or not isinstance(exchange.get("active"), dict):
                raise ValueError("shape")
        except Exception:  # noqa: BLE001 - any failure to read the panel is one silent poll
            self.panel_failed()
            self.save()
            return
        self.panel_answered()
        try:
            self.observe(exchange)
            self.alive(exchange)
        except Exception as error:  # noqa: BLE001 - N-01: one critical message an hour, the process goes on
            kind = type(error).__name__.lower()
            self.log("parse", kind)
            self.enqueue("parse:%d" % int(self.clock() // 3600), CRITICAL, "parse_failed",
                         "Jev Arena: сповіщувач не розібрав стан панелі (%s); повідомлення можуть пропускатися. Глянь панель." % code(kind))
        self.save()

    def panel_failed(self):
        self.state["failures"] += 1
        if self.state["silent_since"] is None:
            self.state["silent_since"] = self.clock()
        if self.state["failures"] >= SILENT_POLLS and not self.state["silent_told"]:
            self.state["silent_told"] = True
            minutes = max(1, round((self.clock() - (self.state["last_ok"] or self.state["silent_since"])) / 60))
            self.enqueue("silent:%d" % int(self.state["silent_since"]), CRITICAL, "panel_silent",
                         "Jev Arena: панель не відповідає вже ≈ %d хв. Перевір інший ПК (живлення, мережа, вікно панелі)." % minutes)
        self.log("panel_poll", "failed")

    def panel_answered(self):
        if self.state["silent_told"]:
            minutes = max(1, round((self.clock() - self.state["silent_since"]) / 60))
            self.enqueue("back:%d" % int(self.state["silent_since"]), CRITICAL, "panel_back",
                         "Jev Arena: панель знову відповідає (мовчала ≈ %d хв)." % minutes)
        self.state.update(failures=0, silent_since=None, silent_told=False, last_ok=self.clock())

    def observe(self, exchange):
        active = {run["id"]: run for run in exchange["active"].values() if isinstance(run, dict) and RUN_ID.fullmatch(str(run.get("id", "")))}
        history = {row.get("id"): row for row in exchange.get("history") or [] if isinstance(row, dict)}
        runs = self.state["runs"]
        if self.first:
            self.first = False
            for rid, run in active.items():
                runs[rid] = self.track(run, announced=True)
            lines = ["%s · %s" % (run_label(run), code(str(run.get("status", "")).lower())) for run in active.values()]
            self.state["alive_date"] = kyiv(datetime.fromtimestamp(self.clock(), timezone.utc)).date().isoformat()  # it says so itself
            self.enqueue("launched:%d" % int(self.clock()), IMPORTANT, "launched",
                         "Jev Arena: сповіщувач запущено. Активних раундів: %d%s" % (len(active), (": " + "; ".join(lines)) if lines else "."))
        for rid, run in active.items():
            if rid not in runs:
                runs[rid] = self.track(run, announced=False, seq=0)
            self.follow(runs[rid], run)
        for rid, tracked in list(runs.items()):
            if rid not in active and tracked.get("active"):
                row = history.get(rid)
                if row and row.get("status") == "DONE":
                    tracked["active"] = False
                    self.done(rid, tracked, row)
        for rid in [r for r, t in runs.items() if not t.get("active") and r not in history]:
            runs.pop(rid)  # long gone from the panel's history

    def track(self, run, *, announced, seq=None):
        events = run.get("events") or []
        last = max((e.get("seq", 0) for e in events if isinstance(e, dict)), default=0)
        return {"active": True, "announced": announced, "seq": last if seq is None else seq, "paused": False, "pauses": 0,
                "windows": {}, "hints": [], "bots": {}, "context": run.get("context")}

    def follow(self, tracked, run):
        rid, label = run["id"], run_label(run)
        bots = {b.get("id"): b for b in run.get("bots") or [] if isinstance(b, dict) and b.get("id") != "cash"}
        tracked["bots"] = {bid: name(b.get("name")) for bid, b in bots.items()}
        tracked["context"] = run.get("context")
        if not tracked["announced"]:
            tracked["announced"] = True
            hours = (run.get("duration_seconds") or 0) / 3600
            self.enqueue(rid + ":start", IMPORTANT, "start",
                         "Jev Arena: старт раунду %s · ботів: %d · вікно входів %s год. Сповіщувач живий." % (label, len(bots), ("%g" % round(hours, 2))))
        events = sorted((e for e in run.get("events") or [] if isinstance(e, dict) and e.get("seq", 0) > tracked["seq"]),
                        key=lambda e: e.get("seq", 0))
        for event in events:
            tracked["seq"] = max(tracked["seq"], event.get("seq", 0))
            self.event(rid, label, tracked, bots, event)
        status = str(run.get("status", ""))
        if status == "PAUSED" and not tracked["paused"]:
            tracked["paused"], tracked["pauses"] = True, tracked["pauses"] + 1
            self.hint(tracked, "pause")
            self.enqueue("%s:pause:%d" % (rid, tracked["pauses"]), CRITICAL, "pause",
                         "Jev Arena: ПАУЗА раунду %s. Код: %s. Відкрий панель; відновлення — лише власник." % (label, tracked.get("pause_code") or "process_stopped"))
        elif status != "PAUSED":
            tracked["paused"] = False
        now = datetime.fromtimestamp(self.clock(), timezone.utc)
        for bid, window in tracked["windows"].items():
            started = parse_at(window.get("at"))
            if started is None:
                continue
            age, holding = (now - started).total_seconds(), bool((bots.get(bid) or {}).get("position_btc"))
            if holding and age > WINDOW_WITH_POSITION:
                self.window(rid, label, tracked, bid, window, "long_with_position",
                            "з позицією вже %d с (межа D166 — 120 с)" % age)
            elif not holding and age > WINDOW_FLAT:
                self.window(rid, label, tracked, bid, window, "long_flat", "без позиції вже ≈ %d хв" % round(age / 60))

    def hint(self, tracked, word):
        if word not in tracked["hints"]:
            tracked["hints"].append(word)

    def window(self, rid, label, tracked, bid, window, kind, detail, priority=ROUTINE):
        self.enqueue("%s:window:%s:%s:%s" % (rid, bid, window.get("at"), kind), priority, "window",
                     "Jev Arena: вікно деградації, бот «%s», раунд %s: %s." % (tracked["bots"].get(bid, "?"), label, detail))

    def event(self, rid, label, tracked, bots, event):
        data, kind, bid = event.get("data") or {}, event.get("code"), event.get("bot_id")
        who = tracked["bots"].get(bid, "?")
        if kind == "exchange_check_failed" and data.get("reason") in MANUAL_REVIEW_REASONS:
            self.hint(tracked, "manual_review")
            self.enqueue("%s:manual:%s" % (rid, bid), CRITICAL, "manual_review",
                         "Jev Arena: РУЧНА ЗВІРКА, бот «%s», раунд %s. Причина: %s." % (who, label, code(data.get("reason"))))
        elif kind == "stop_requested":
            reason = code(data.get("reason_code"))
            tracked["pause_code"] = reason
            if reason in INCOMPLETE_HINTS:
                self.hint(tracked, reason)
            if reason == "manual_review" and not MANUAL_REVIEW_REASONS:
                self.enqueue("%s:manual:season" % rid, CRITICAL, "manual_review", "Jev Arena: РУЧНА ЗВІРКА в раунді %s." % label)
        elif kind in {"closing_paused", "paused"}:
            tracked["pause_code"] = code(kind)
        elif kind == "exchange_error":
            tracked["pause_code"] = code(data.get("reason"))
        elif kind == "protective_recovery":
            self.hint(tracked, "protective_recovery")
            self.enqueue("%s:recovery:%s" % (rid, bid), CRITICAL, "bot_stopped",
                         "Jev Arena: бота «%s» зупинено до кінця раунду %s: захист на біржі не підтверджено; позиція закривається." % (who, label))
        elif kind == "protective_absent":  # R2.1.15 RC2 (PP-05): a stop OKX does not show, up to 60 s
            self.enqueue("%s:absent:%s:%s" % (rid, bid, event.get("seq")), IMPORTANT, "stop_absent",
                         "Jev Arena: бот «%s», раунд %s: біржа не показує щойно поставленого стопа; перевіряємо до 60 с, без нових входів." % (who, label))
        elif kind == "protective_not_created":
            self.enqueue("%s:not_created:%s:%s" % (rid, bid, event.get("seq")), CRITICAL, "stop_not_created",
                         "Jev Arena: бот «%s», раунд %s: стоп на біржі не створено; ставимо новий або закриваємо позицію." % (who, label))
        elif kind == "settled_account_unverified":
            self.hint(tracked, "settled_account_unverified")
        elif kind == "admission_closed":
            self.enqueue(rid + ":admission", ROUTINE, "admission_closed",
                         "Jev Arena: вікно входів раунду %s закрито; відкриті позиції виходять за своїми правилами." % label)
        elif kind == "degraded_window_started" and bid:
            tracked["windows"][bid] = {"at": event.get("at"), "extended": bool(data.get("extended"))}
            if bool((bots.get(bid) or {}).get("position_btc")) and not data.get("extended"):
                self.window(rid, label, tracked, bid, tracked["windows"][bid], "no_verified_stop",
                            "з позицією, без перевіреного стопа на біржі")
        elif kind == "degraded_window_ended" and bid:
            window = tracked["windows"].pop(bid, {"at": event.get("at")})
            outcome, skipped = code(data.get("outcome")), data.get("skipped_decisions")
            if outcome in {"recovery", "paused"}:
                self.hint(tracked, "degraded_window")
                self.window(rid, label, tracked, bid, window, "ended_" + outcome, "закінчилось: %s" % outcome, CRITICAL)
            elif isinstance(skipped, int) and skipped > SKIPPED_DECISIONS:
                self.window(rid, label, tracked, bid, window, "skipped", "пропущено рішень: %d (межа — %d)" % (skipped, SKIPPED_DECISIONS))

    def done(self, rid, tracked, row):
        label = "%s · %s" % (rid[:8], CONTEXTS.get(row.get("context") or tracked.get("context"), "?"))
        bots, reasons = [], []
        try:
            final = self.panel.get("/api/exchange/runs/" + rid)
            bots = [b for b in final.get("bots") or [] if isinstance(b, dict) and b.get("id") != "cash"]
            reasons = [code(r) for r in final.get("incomplete_reasons") or []]  # R2.1.15 (PB-03) names them itself
        except Exception:  # noqa: BLE001 - the summary goes out with what the history row tells
            pass
        if (row.get("jev_usage") or {}).get("unknown_billing"):
            self.hint(tracked, "unknown_billing")
        if row.get("incomplete"):
            words = [INCOMPLETE_HINTS.get(r, r) for r in reasons] or [INCOMPLETE_HINTS[h] for h in tracked["hints"]]
            head = "НЕПОВНИЙ (%s)" % (", ".join(words) if words else "причину не названо, див. ZIP")
        else:
            head = "повний"
        lines = ["· %s: угод %s, net %s USDT, облік %s" % (name(b.get("name")), b.get("trades", "—"), money(b.get("pnl_usdt")),
                                                          "звірено" if b.get("accounting_reconciled") else "не звірено") for b in bots]
        self.enqueue(rid + ":done", IMPORTANT, "done", "Jev Arena: раунд %s завершено, %s.%s" % (label, head, ("\n" + "\n".join(lines)) if lines else ""))

    def alive(self, exchange):
        now = kyiv(datetime.fromtimestamp(self.clock(), timezone.utc))
        today = now.date().isoformat()
        if now.hour >= ALIVE_HOUR and self.state["alive_date"] != today:
            self.state["alive_date"] = today
            count = sum(1 for run in exchange["active"].values() if run)
            self.enqueue("alive:" + today, IMPORTANT, "alive", "Jev Arena: сповіщувач живий · %s · панель відповідає · активних раундів: %d." % (today, count))

    # -- Telegram --
    def pick(self):
        now = self.clock()
        self.state["routine"] = [t for t in self.state["routine"] if now - t < 3600]
        if self.state["skipped"] and len(self.state["routine"]) < ROUTINE_PER_HOUR and not self.state["queue"]:
            skipped, self.state["skipped"] = self.state["skipped"], 0
            self.enqueue("skipped:%d" % int(now), ROUTINE, "skipped",
                         "Jev Arena: пропущено %d рутинних подій (межа — %d на годину); подробиці — у панелі." % (skipped, ROUTINE_PER_HOUR))
        for item in sorted(self.state["queue"], key=lambda i: (i["priority"], i["order"])):
            if item["priority"] != ROUTINE or len(self.state["routine"]) < ROUTINE_PER_HOUR:
                return item
        return None

    def send_next(self):
        """One message if its time has come: critical first, routine only under the hourly limit, at least
        SEND_SPACING seconds apart. Returns the seconds to wait before the next try, or None with nothing to send."""
        now = self.clock()
        if now < self.state["next_send"]:
            return self.state["next_send"] - now
        item = self.pick()
        if item is None:
            return None
        try:
            self.telegram.send(self.chat_id, item["text"])
        except SendFailed as error:
            self.log(item["kind"], error.category)
            if error.category == "http_400":  # Telegram refuses this message as such: it never blocks the others
                self.state["queue"].remove(item)
                self.state["next_send"] = now + SEND_SPACING
                self.save()
                return SEND_SPACING
            if error.retry_after:  # 429: Telegram says how long; the growing pause is for the other failures
                wait = min(300, error.retry_after)
            else:
                self.state["backoff"] = wait = min(300, max(5, self.state["backoff"] * 2))
            self.state["next_send"] = now + max(SEND_SPACING, wait)
            self.save()
            return self.state["next_send"] - now
        self.log(item["kind"], "sent")
        self.state["queue"].remove(item)
        if item["priority"] == ROUTINE:
            self.state["routine"].append(now)
        self.state.update(next_send=now + SEND_SPACING, backoff=0)
        self.save()
        return SEND_SPACING

    def stop_asked(self):
        return (self.dir / "stop.flag").exists()

    def run_forever(self, sleep=time.sleep):
        """Polls every minute and sends in between; looks for the stop flag of `restart` every second (N-03). A failure of
        one step (a full disk, say) is journalled and the next one tries again: the process does not end on it (N-01)."""
        while not self.stop_asked():
            try:
                self.poll()
            except Exception as error:  # noqa: BLE001
                self.log("loop", type(error).__name__.lower())
            deadline = self.clock() + POLL_SECONDS
            while self.clock() < deadline and not self.stop_asked():
                try:
                    wait = self.send_next()
                except Exception as error:  # noqa: BLE001
                    self.log("send", type(error).__name__.lower())
                    wait = SEND_SPACING
                sleep(max(0.5, min(1.0, deadline - self.clock(), wait if wait is not None else 1.0)))
        self.log("stop", "asked")


# ---- one process at a time ----

class Lock:
    """An exclusive OS lock on runtime/notifier/notifier.lock for the process's lifetime (msvcrt on Windows)."""

    def __init__(self, root):
        self.path = Path(root) / STATE_DIR / "notifier.lock"
        self.handle = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self):
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def running(root):
    """True when a notifier holds the lock."""
    lock = Lock(root)
    if lock.acquire():
        lock.release()
        return False
    return True


def spawn(root, port=8765, popen=subprocess.Popen):
    """Starts `run` detached from the caller (the panel or a console) unless a notifier runs or the file is missing.
    Returns started, running or not_configured. The caller never reads the file's values: the child checks them."""
    if not (Path(root) / CONFIG_NAME).is_file():
        return "not_configured"
    if running(root):
        return "running"
    flags = {"creationflags": DETACHED} if os.name == "nt" else {"start_new_session": True}
    popen([sys.executable, "-X", "utf8", "-B", str(Path(root) / "tools" / "arena_notifier.py"), "run", "--port", str(port)],
          cwd=str(root), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, **flags)
    return "started"


def stop_running(root, *, wait=time.sleep, clock=time.time, seconds=90):
    """N-03: asks the notifier that holds the lock to end (runtime/notifier/stop.flag; it looks every second, a poll or a
    send takes at most ~25 s) and waits for the lock. No process is killed: no other process can be hit, and a notifier
    the panel's task started as SYSTEM stops without an administrator. not_running, stopped or still_running; the flag
    is removed either way."""
    if not running(root):
        return "not_running"
    flag = Path(root) / STATE_DIR / "stop.flag"
    flag.write_text("stop", encoding="ascii")
    try:
        deadline = clock() + seconds
        while clock() < deadline:
            if not running(root):
                return "stopped"
            wait(0.5)
        return "still_running"
    finally:
        try:
            flag.unlink()
        except OSError:
            pass


def watch(root, port=8765, *, interval=POLL_SECONDS, popen=subprocess.Popen, stop=None, sleep=time.sleep, out=print):
    """The panel's watch (plan check p. 4): every minute a notifier that does not hold its lock is started again. N-02: one
    that ends right after each start (a wrong token or chat id) three times in a row is told once in the panel's log and
    is tried again only every tenth minute then."""
    failed, started, tick, told = 0, False, 0, False
    while stop is None or not stop.is_set():
        try:
            if started:
                failed = 0 if running(root) else failed + 1
            if failed >= 3 and not told:
                told = True
                out("Telegram notifier: it ends right after each start; see runtime/notifier/notifier.log.")
            started = False
            if failed < 3 or tick % 10 == 0:
                result = spawn(root, port, popen)
                started = result == "started"
                if result == "running":
                    failed, told = 0, False
        except Exception:  # noqa: BLE001 - the watch never takes the panel down
            started = False
        tick += 1
        sleep(interval)


# ---- the one-time chat binding (plan check p. 1) ----

def find_chat(root, telegram, *, clock=time.time, out=print, seconds=FIND_CHAT_SECONDS):
    """Shows a random code; only a private chat that sends exactly this code within `seconds` is written. Updates that
    came before the code are acknowledged first and never read as an answer; a /start or any other text is ignored."""
    secret = "JA-" + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
    pending = telegram.call("getUpdates", {"timeout": 0, "allowed_updates": ["message"]}) or []
    offset = max((u.get("update_id", 0) for u in pending if isinstance(u, dict)), default=-1) + 1
    out("Надішли своєму боту в Telegram одним повідомленням цей код: %s" % secret)
    out("Чекаю %d хв. Чужі повідомлення й /start не приймаються." % (seconds // 60))
    deadline = clock() + seconds
    while clock() < deadline:
        updates = telegram.call("getUpdates", {"offset": offset, "timeout": 20, "allowed_updates": ["message"]}, timeout=30) or []
        for update in updates:
            if not isinstance(update, dict):
                continue
            offset = max(offset, update.get("update_id", 0) + 1)
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            if chat.get("type") == "private" and str(message.get("text", "")).strip() == secret and isinstance(chat.get("id"), int):
                telegram.call("getUpdates", {"offset": offset, "timeout": 0})  # acknowledged: never read again
                write_chat(root, chat["id"])
                out("Чат знайдено: %s. Записано." % name(chat.get("first_name") or chat.get("username") or "без імені"))
                return True
    telegram.call("getUpdates", {"offset": offset, "timeout": 0})
    out("Код не надійшов за %d хв. Нічого не записано; запусти ще раз." % (seconds // 60))
    return False


# ---- entry ----

REFUSED = {"http_401", "http_404"}  # Telegram's answer to a revoked or mistyped token


def telegram_failure(category):
    """D173 (N-02): what the console says about a failed Telegram call; never the token."""
    if category in REFUSED:
        return ("Telegram не прийняв токен (%s). Створи новий у @BotFather (/revoke або /token), впиши його у файл "
                "і запусти NOTIFIER_START.cmd ще раз." % category)
    return "Telegram зараз недоступний (%s): перевір мережу цього ПК." % category


def main(argv=None, *, root=ROOT, opener=None):
    quiet_hooks()
    parser = argparse.ArgumentParser(description="Jev Arena notifier: reads the local panel, writes the owner in Telegram.")
    parser.add_argument("command", nargs="?", default="run", choices=("run", "start", "restart", "find-chat", "test"))
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    root = Path(root)
    try:
        token, chat_id = read_config(root, need_chat=args.command not in {"find-chat"})
    except NotConfigured as error:
        if args.command == "run":
            log_line(root, "config", "not_configured_" + str(error))  # N-02: the panel's watch starts it; this says why it ended
        print("Сповіщувач не налаштовано (%s): дивись NOTIFIER_TELEGRAM_UA.md." % error, file=sys.stderr)
        return 2
    if args.command in {"start", "restart"}:
        try:
            Telegram(token, opener).call("getMe", {})  # D173 (N-02): a refused token is said here, before any start
        except SendFailed as error:
            print(telegram_failure(error.category), file=sys.stderr)
            if error.category in REFUSED:
                return 1
            print("Сповіщувач однаково запускається: він повторюватиме спроби сам.", file=sys.stderr)
        if args.command == "restart":
            stopped = stop_running(root)
            if stopped == "still_running":
                print("Старий процес сповіщувача не завершився за 90 с; спробуй ще раз або перезапусти ПК.", file=sys.stderr)
                return 1
        result = spawn(root, args.port)
        print({"started": "Сповіщувач запущено.", "running": "Сповіщувач уже працює."}.get(result, result))
        return 0
    telegram = Telegram(token, opener)
    if args.command == "find-chat":
        try:
            return 0 if find_chat(root, telegram) else 1
        except SendFailed as error:
            print(telegram_failure(error.category), file=sys.stderr)
            return 1
    if args.command == "test":
        try:
            telegram.send(chat_id, "Jev Arena: тест сповіщувача. Зв'язок працює.")
        except SendFailed as error:
            print("Тест не пройшов. " + telegram_failure(error.category), file=sys.stderr)
            return 1
        print("Тест надіслано.")
        return 0
    lock = Lock(root)
    if not lock.acquire():
        print("Сповіщувач уже працює.", file=sys.stderr)
        return 0
    try:
        (root / STATE_DIR / "stop.flag").unlink()  # a flag left by an interrupted restart never stops the new one
    except OSError:
        pass
    try:
        Notifier(root, Panel(args.port), telegram, chat_id).run_forever()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
