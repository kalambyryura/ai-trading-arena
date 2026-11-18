"""Loopback-only UI. Serving a page never starts trading; Play is a protected POST."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import uuid
import webbrowser
from urllib.parse import urlsplit
from copy import deepcopy

from .one_trade import ROOT, TERMINAL_RUNS, OneTradeAPI, single_process
from .okx_readonly import PreflightError, credentials_from, load_settings, one_row
from .cycle_budget import MAX_CYCLES, SERIES_ATTEMPTS, SERIES_COST, ledger_usage, web_ledgers
from .position_cycle import PROFILE, STOP_FILE
from .cycle_history import project_history
from .arena import Arena
from .workshop import Workshop
from .arena_strategies import ArenaError
from .viewer_cache import ViewerSnapshots

BOT = {"id": PROFILE, "name": "Jev · керована позиція", "instrument": "BTC-USDT-SWAP",
       "max_notional_usdt": "30", "max_base_btc": "0.0003", "leverage": 1,
       "max_entries": 3, "max_hold_seconds": 300, "max_cycle_seconds": 300, "review_seconds": 30}
JEV_COST_LIMIT = SERIES_COST
RUN_ID = re.compile(r"[0-9a-f]{32}")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path, default=None):
    if not path.exists():
        return default
    if path.stat().st_size > 2097152:
        raise ValueError("local_state_too_large")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("invalid_local_state")
    return value


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, allow_nan=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def number(value):
    try:
        result = Decimal(str(value))
        return format(result, "f") if result.is_finite() and abs(result) < Decimal("1e15") else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def public_cycle(state, identity, fallback_time=None):
    """Explicit display fields only: never API keys, account tags or order IDs."""
    state = state or {}
    entries = state.get("entries", [state.get("entry") or {}])
    entry_results = [item.get("result") or {} for item in entries]
    entry = (state.get("entry") or {}).get("result") or {}
    exits = [item.get("result") or {} for item in state.get("exits", [])]
    jev, plan = state.get("jev") or {}, state.get("plan") or {}
    phase = state.get("phase", "CHECKING")
    if phase not in TERMINAL_RUNS | {"REVIEWING", "ENTERING", "CLOSING", "CHECKING", "WAITING_ENTRY", "HOLDING", "MONITORING", "POSITION_REVIEW"}:
        phase = "NEEDS_REVIEW"
    filled = str(sum((Decimal(number(r.get("accFillSz")) or "0") for r in entry_results), Decimal(0)))
    base = number(plan.get("contract_btc"))
    timestamp = fallback_time
    if exits and number(exits[-1].get("uTime")):
        try:
            timestamp = datetime.fromtimestamp(float(exits[-1]["uTime"]) / 1000, timezone.utc).isoformat(timespec="seconds")
        except (ValueError, OSError, OverflowError):
            pass
    reason = state.get("last_error", "")
    decisions = state.get("decisions", [])
    monitor = state.get("monitor") or {}
    safe_code = lambda value: value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_]{1,120}", value) else None
    profile = state.get("profile") if state.get("profile") in {PROFILE, "jev-position-v2"} else "jev-roundtrip"
    return {"id": identity, "bot_id": profile, "phase": phase, "time": timestamp,
            "event_count": len(state.get("events", [])),
            "base_btc": str(Decimal(filled) * Decimal(base)) if filled and base else number(plan.get("base_btc")),
            "buy_price": number(state.get("average_entry_usdt", entry.get("avgPx"))),
            "sell_price": number(state.get("average_exit_usdt", exits[-1].get("avgPx") if exits else None)),
            "remaining_contracts": number(state.get("remaining_contracts")),
            "pnl_usdt": number(state.get("pnl_excluding_funding_usdt")),
            "signed_fees_usdt": number(state.get("signed_fees_usdt")),
            "funding_reconciled": state.get("funding_reconciled") is True,
            "jev": {"choice": jev.get("choice") if jev.get("choice") in {"run_test", "skip", "buy", "hold", "add", "close"} else None,
                    "cost_usd": number(jev.get("cost_usd")), "elapsed_ms": number(jev.get("elapsed_ms")),
                    "confidence": number(jev.get("confidence"))},
            "entries_used": len([r for r in entries if r.get("send_started")]),
            "close_reason": safe_code(state.get("close_reason")),
            "monitor": {k: number(monitor.get(k)) for k in ("bid_usdt", "return_bps", "held_seconds", "estimated_close_pnl_usdt", "server_ms")},
            "decisions": [{"stage": d.get("stage") if d.get("stage") in {"entry", "position"} else None,
                           "choice": safe_code(d.get("choice")), "reason": safe_code(d.get("reason")),
                           "cost_usd": number(d.get("cost_usd")), "elapsed_ms": number(d.get("elapsed_ms")),
                           "return_5m_bps": number(d.get("market", {}).get("return_5m_bps")),
                           "time_ms": number(d.get("market", {}).get("observed_server_ms"))} for d in decisions],
            "samples": [{k: number(sample.get(k)) for k in ("held_seconds", "estimated_close_pnl_usdt")}
                        for sample in state.get("samples", [])],
            "error": safe_code(reason)}


def read_balance(root):
    site, environment, values = load_settings(root)
    api = OneTradeAPI(site, environment, credentials_from(values), writes=False)
    row = one_row(api.get("/api/v5/account/balance", private=True))
    details = row.get("details")
    if not isinstance(details, list):
        raise PreflightError("balance_unavailable")
    usdt = one_row([d for d in details if isinstance(d, dict) and d.get("ccy") == "USDT"])
    return {"updated_at": now(), "portfolio_equity_usd": number(row.get("totalEq")),
            "usdt_equity": number(usdt.get("eq")), "available_usdt": number(usdt.get("availBal")),
            "requests": api.requests}



def season_snapshots(folder, journal, identity):
    """The run's public snapshot journal, or (R2.1.12, MIX) {contract: journal} of a mixed season (plan check p. 13)."""
    from .exchange_engine import mixed, snapshot_files
    return snapshot_files(folder, journal) if mixed(journal) else folder / (identity + ".snapshots.jsonl")


def season_quotes(folder, journal, identity):
    """Recorded quotes up to the journal's watermark, or {contract: quotes} of a mixed season, each to its own."""
    from .battle_debrief import recorded_quotes
    from .exchange_engine import mixed
    paths = season_snapshots(folder, journal, identity)
    if mixed(journal):
        return {c: recorded_quotes(p, ((journal.get("latest_markets") or {}).get(c) or {}).get("at") or journal.get("finished_at"))
                for c, p in paths.items()}
    return recorded_quotes(paths, (journal.get("latest_market") or {}).get("at") or (journal.get("_last") or {}).get("at")
                           or journal.get("finished_at"))

class PanelError(Exception):
    pass


class Panel:
    def __init__(self, root=ROOT, *, launcher=subprocess.Popen, balance_reader=read_balance):
        self.root = Path(root)
        self.path = self.root / "runtime" / "web_panel" / "state.json"
        self.data = read_json(self.path, {"version": 1, "generation": 0, "jobs": []})
        if (self.data.get("version") != 1 or not isinstance(self.data.get("generation"), int)
                or not isinstance(self.data.get("jobs"), list) or len(self.data["jobs"]) > MAX_CYCLES
                or any(not RUN_ID.fullmatch(job.get("id", "")) for job in self.data["jobs"])):
            raise ValueError("invalid_panel_state")
        self.launcher, self.balance_reader = launcher, balance_reader
        self.process = None
        self.lock = threading.RLock()
        self.balance_lock = threading.Lock()
        self.token = secrets.token_urlsafe(32)

    def run_root(self, identity):
        return self.root if identity == "legacy" else self.root / "runtime" / "web_runs" / identity

    def cycles(self):
        items = []
        legacy = read_json(self.run_root("legacy") / "runtime" / "one_trade_v1.json")
        if legacy:
            items.append(public_cycle(legacy, "legacy"))
        for job in self.data["jobs"]:
            state = read_json(self.run_root(job["id"]) / "runtime" / "one_trade_v1.json")
            item = public_cycle(state if state is not None else {"profile": job.get("bot_id")}, job["id"], job["started_at"])
            item["worker_exit_code"] = job.get("exit_code")
            items.append(item)
        # Unknown journals must be recovered/reviewed, never bypassed via a fresh manifest.
        known = {job["id"] for job in self.data["jobs"]}
        run_folder = self.root / "runtime" / "web_runs"
        for path in sorted(run_folder.glob("*/runtime/one_trade_v1.json")):
            identity = path.parent.parent.name
            if identity not in known and RUN_ID.fullmatch(identity):
                items.append(public_cycle(read_json(path), identity))
        for item in items:
            folder = self.run_root(item["id"]) / "runtime"
            item["stop_requested"] = (folder / STOP_FILE).exists()
            paths = [folder / name for name in ("jev_one_trade_v1.sqlite3", "jev_position_v2.sqlite3")]
            item["usage"] = self.ledger_usage([path for path in paths if path.exists()])
        return items

    def accounting(self):
        usage = self.ledger_usage(web_ledgers(self.root))
        return {**usage, "max_cycles": MAX_CYCLES, "max_attempts": SERIES_ATTEMPTS, "cost_limit_usd": str(JEV_COST_LIMIT)}

    def history(self, identity):
        if identity != "legacy" and not RUN_ID.fullmatch(identity):
            raise LookupError("unknown_cycle")
        cycle = next((item for item in self.cycles() if item["id"] == identity), None)
        if cycle is None:
            raise LookupError("unknown_cycle")
        state = read_json(self.run_root(identity) / "runtime" / "one_trade_v1.json", {})
        history = project_history(state)
        if not state:
            history.update(mode="pending", events=[])
        return {"cycle": cycle, **history}

    @staticmethod
    def ledger_usage(paths):
        return ledger_usage(paths)

    def total_usage(self):
        paths = [self.root / "runtime" / name for name in (
            "jev_ledger.sqlite3", "jev_okx_public_review_v1.sqlite3", "jev_one_trade_v1.sqlite3")]
        paths += web_ledgers(self.root)
        return self.ledger_usage([path for path in paths if path.exists()])

    def reap(self):
        if self.process and self.process.poll() is not None:
            identity = self.data.get("active_id")
            for job in self.data["jobs"]:
                if job["id"] == identity:
                    job.update(exit_code=self.process.returncode, stopped_at=now())
            self.process = None
            write_json(self.path, self.data)

    def status(self):
        with self.lock:
            self.reap()
            cycles, budget = self.cycles(), self.accounting()
            running = self.process is not None
            # A separately launched CLI worker uses the same OS lock.
            if not running:
                try:
                    with single_process(self.root):
                        pass
                except PreflightError:
                    running = True
            unfinished = [item for item in cycles if item["phase"] not in TERMINAL_RUNS]
            blocked = None
            if len(unfinished) > 1:
                blocked = "Кілька незавершених журналів. Потрібна перевірка перед запуском."
            elif not unfinished and (len(cycles) - int(any(c["id"] == "legacy" for c in cycles)) >= MAX_CYCLES or budget["attempts"] >= SERIES_ATTEMPTS):
                blocked = "Серію з 20 тестів завершено. Нову серію налаштуємо окремо."
            elif not unfinished and (budget["unresolved"] or Decimal(budget["cost_usd"]) >= JEV_COST_LIMIT):
                blocked = "Потрібна звірка витрат Jev перед наступним тестом."
            return {"api_version": 3, "bot": BOT, "generation": self.data["generation"], "running": running,
                    "can_play": not running and blocked is None,
                    "can_stop": len(unfinished) == 1,
                    "action": "resume" if unfinished else "new", "blocked": blocked,
                    "current": unfinished[0] if unfinished else cycles[-1] if cycles else None,
                    "cycles": cycles, "budget": budget, "total_jev": self.total_usage(),
                    "balance": read_json(self.path.with_name("balance.json"))}

    def play(self, generation):
        with self.lock:
            current = self.status()
            if type(generation) is not int or generation != self.data["generation"]:
                raise PanelError("Стан змінився. Онови сторінку перед наступним натисканням.")
            if not current["can_play"]:
                raise PanelError(current["blocked"] or "Бот уже працює. Дочекайся завершення.")
            identity = current["current"]["id"] if current["action"] == "resume" else uuid.uuid4().hex
            if current["action"] == "new":
                self.data["jobs"].append({"id": identity, "bot_id": BOT["id"], "started_at": now()})
            self.data["generation"] += 1
            self.data["active_id"] = identity
            # Reserve this click durably before spawning. A duplicate POST cannot create another job.
            write_json(self.path, self.data)
            self.launch_worker(identity)
            return {"accepted": True, "id": identity, "generation": self.data["generation"]}

    def launch_worker(self, identity):
        folder = self.run_root(identity) / "runtime"
        folder.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, "-u", str(self.root / "one_trade.py"), "--live", "--strategy", "monitored"]
        if identity != "legacy":
            command += ["--web-run", identity]
        try:
            with (folder / "console.log").open("ab") as output:
                self.process = self.launcher(command, cwd=str(self.root), stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.STDOUT, shell=False,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except OSError:
            raise PanelError("Процес не запустився. Повторне натискання відновить цей самий цикл.") from None

    def stop(self, generation, identity):
        """User-only command: cooperative close, never terminate/kill the worker."""
        with self.lock:
            current = self.status()
            if (type(generation) is not int or generation != self.data["generation"]
                    or not current["can_stop"] or identity != current["current"]["id"]):
                raise PanelError("Стан змінився. Онови сторінку; команда стосується лише поточного циклу.")
            path = self.run_root(identity) / "runtime" / STOP_FILE
            if not path.exists():
                write_json(path, {"requested_at": now(), "action": "close_cycle"})
            self.data["generation"] += 1
            self.data["active_id"] = identity
            write_json(self.path, self.data)
            if not current["running"]:
                # Persistent stop exists before startup: even a missing entry journal cannot buy.
                self.launch_worker(identity)
            return {"accepted": True, "id": identity, "closing_confirmed": False}

    def refresh_balance(self):
        with self.balance_lock:
            cached = read_json(self.path.with_name("balance.json"))
            if cached and (datetime.now(timezone.utc) - datetime.fromisoformat(cached["updated_at"])).total_seconds() < 10:
                return cached
            snapshot = self.balance_reader(self.root)
            write_json(self.path.with_name("balance.json"), snapshot)
            return snapshot


# R2.1.14 (WinError 10053/10054, EPIPE): the viewer went away mid-answer (a page closed or reloaded, the 8 s poll timeout
# of web/arena.js); no server fault, so neither a second 503 answer on the dead socket nor a stack in the console
CLIENT_GONE = (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], CLIENT_GONE):
            return  # R2.1.14: e.g. a request line cut off by the viewer; every other fault keeps its stack
        super().handle_error(request, client_address)

    def __init__(self, panel, port=8765, arena=None, exchange_factory=None):
        if (panel.root / "runtime" / "server_handoff.json").exists():
            raise PanelError("Цей workspace перенесено на сервер. Локальний запуск заблоковано, щоб не торгувати двічі.")
        self.panel = panel
        self.battle_export_lock = threading.Lock()
        self.arena = arena if arena is not None else Arena(panel.root)
        try:
            self.workshop = Workshop(panel.root, self.arena)
            from .exchange_lab import ExchangeLab
            self.exchange = (exchange_factory or ExchangeLab)(panel.root, self.workshop, self.arena)
            from .staged_qualification import apply as apply_qualification
            self.workshop.qualification_import = apply_qualification(self.workshop, self.exchange)
            from .league import League
            from .ops_service import PublicRecorderRepository
            from .public_recorder import OwnerPublicGet
            from .arena_market import build_snapshot
            self.league = League(panel.root, self.arena, self.exchange, workshop=self.workshop)
            self.recorder = PublicRecorderRepository(panel.root / "runtime" / "market_recorder_v1",
                build_snapshot_fn=build_snapshot, get_factory=OwnerPublicGet, source="OKX_LIVE_PUBLIC")
            self.workshop.recorder = self.recorder
            # Scheduled viewer polls share short-lived copies; owner reads and writes never do (see Handler.state_for / do_POST).
            self.viewer = ViewerSnapshots({"exchange": lambda: self.exchange.status(), "arena": lambda: self.arena.status()})
            super().__init__(("127.0.0.1", port), Handler)
        except Exception:
            if hasattr(self, "exchange"):
                self.exchange.close()
            self.arena.close()
            raise
        self.origin = "http://127.0.0.1:" + str(self.server_port)

    def server_close(self):
        self.exchange.close()
        self.arena.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Request URLs/headers and private state are not logged.

    def send(self, status, content, content_type="application/json; charset=utf-8", headers=None):
        if not isinstance(content, bytes):
            content = json.dumps(content, ensure_ascii=False, allow_nan=False).encode()
        try:
            self.send_response(status)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(content)
        except CLIENT_GONE:
            self.close_connection = True  # R2.1.14: nobody to answer; the caller's except sends no 503 after it

    def local_request(self):
        return self.headers.get("Host") == self.server.origin.removeprefix("http://")

    def state_for(self, name):
        """Scheduled viewer polls (X-Viewer-Poll: 1) share a short-lived copy of the state; every other reader, including the
        owner's read before a Start/Stop and any tool, gets a fresh one."""
        if self.headers.get("X-Viewer-Poll") == "1":
            return self.server.viewer.get(name)
        return getattr(self.server, name).status()

    def do_GET(self):
        if not self.local_request():
            return self.send(403, {"error": "Host not allowed"})
        try:
            route = urlsplit(self.path).path
            if route.startswith("/api/battle-export/"):
                parts = route.split("/")
                if len(parts) != 5 or parts[3] not in {"exchange", "paper"} or not RUN_ID.fullmatch(parts[4]):
                    return self.send(404, {"error": "Бій не знайдено."})
                if not self.server.battle_export_lock.acquire(blocking=False):
                    return self.send(429, {"error": "Інший файл бою ще готується. Спробуй після завершення."})
                try:
                    from .battle_export import create_archive
                    controller = self.server.exchange if parts[3] == "exchange" else self.server.arena
                    with controller.lock:
                        journal = deepcopy(controller.load(parts[4]))
                        from .match_journal import restore_events
                        restore_events(controller.path / (parts[4] + ".events.jsonl"), journal, full=True)
                    with create_archive(journal, snapshots_path=season_snapshots(controller.path, journal, parts[4])) as output:
                        output.seek(0, 2)
                        length = output.tell()
                        output.seek(0)
                        self.send_response(200)
                        self.send_header("Content-Type", "application/zip")
                        self.send_header("Content-Disposition", 'attachment; filename="JevArena-battle-' + parts[3] + "-" + parts[4] + '.zip"')
                        self.send_header("Content-Length", str(length))
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("X-Content-Type-Options", "nosniff")
                        self.send_header("Referrer-Policy", "no-referrer")
                        self.end_headers()
                        for chunk in iter(lambda: output.read(1024**2), b""):
                            self.wfile.write(chunk)
                    return
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return  # A cancelled download is not a financial action or retry.
                except ArenaError as error:
                    return self.send(404, {"error": str(error)})
                finally:
                    self.server.battle_export_lock.release()
            if route.startswith("/api/debrief/"):
                parts = route.split("/")
                if len(parts) != 5 or parts[3] not in {"exchange", "paper"} or not RUN_ID.fullmatch(parts[4]):
                    return self.send(404, {"error": "Бій не знайдено."})
                if not self.server.battle_export_lock.acquire(blocking=False):
                    return self.send(409, {"error": "Інший розбір або експорт уже готується. Спробуй після завершення."})
                try:
                    from .battle_review import build_review
                    from .battle_debrief import build_debrief, check_journal_capacity, recorded_quotes, render_html
                    controller = self.server.exchange if parts[3] == "exchange" else self.server.arena
                    with controller.lock:
                        journal = deepcopy(controller.load(parts[4]))
                        from .match_journal import restore_events
                        check_journal_capacity(controller.path / (parts[4] + ".events.jsonl"), journal)
                        restore_events(controller.path / (parts[4] + ".events.jsonl"), journal, full=True)
                    quotes = season_quotes(controller.path, journal, parts[4])
                    report = build_debrief(build_review(journal, snapshot_context=quotes), quotes)
                    content = render_html(report).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Disposition", 'attachment; filename="JevArena-debrief-' + parts[3] + "-" + parts[4] + '.html"')
                    self.send_header("Content-Length", str(len(content)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
                    self.end_headers()
                    self.wfile.write(content)
                    return
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return
                except (ArenaError, OSError, ValueError, KeyError, TypeError):
                    return self.send(422, {"error": "Неповний або завеликий запис для розбору. Вихідні файли збережено."})
                finally:
                    self.server.battle_export_lock.release()
            if route.startswith(("/api/review/", "/api/replay/")):
                parts = route.split("/")
                if len(parts) != 5 or parts[3] not in {"exchange", "paper"} or not RUN_ID.fullmatch(parts[4]):
                    return self.send(404, {"error": "Бій не знайдено."})
                from .battle_review import build_review, verify_bundle
                controller = self.server.exchange if parts[3] == "exchange" else self.server.arena
                try:
                    from .match_journal import journal_copy
                    journal = journal_copy(controller, parts[4])  # a finished battle's journal is read outside the lock
                    # The season's contract beside the hashed DTO, never inside it: the page names the coin of a BTC
                    # exchange season whose review (byte-identical to earlier releases) does not carry it.
                    instrument = (journal.get("rules") or {}).get("instrument")
                    season = {"X-Season-Instrument": instrument if instrument in {"BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "MIXED"} else "BTC-USDT-SWAP"}
                    from .league import example_only  # R2.1.11 (D141 p. 2): beside the hashed DTO, never inside it
                    season["X-Season-Example-Only"] = "1" if example_only(journal) else "0"
                    if parts[2] == "replay":
                        from .event_replay import build_packet, verify_packet
                        packet = build_packet(journal)
                        verify_packet(packet)
                        return self.send(200, packet, headers=season)
                    context_status = None
                    try:
                        quotes = season_quotes(controller.path, journal, parts[4])
                    except (ArenaError, OSError, ValueError, KeyError, TypeError):
                        quotes, context_status = [], "UNAVAILABLE"
                    review = build_review(journal, snapshot_context=quotes, snapshot_context_status=context_status)
                    verify_bundle(review)
                    return self.send(200, review, headers=season)
                except ArenaError as error:
                    return self.send(404, {"error": str(error)})
                except (OSError, ValueError, KeyError, TypeError):
                    return self.send(422, {"error": "Неповний або пошкоджений запис для огляду. Вихідні файли збережено."})
            if route == "/api/league":
                return self.send(200, self.server.league.status())
            if route == "/api/ops/state":
                exchange = self.server.exchange.status()
                return self.send(200, {"recorder": self.server.recorder.status(),
                    "datasets": self.server.recorder.list_datasets(),
                    "runs": {c: {"id": r["id"], "health": r.get("ops_health"), "storage": r.get("evidence_storage")} if r else None for c, r in exchange["active"].items()},
                    "execution_started": False})
            if self.path == "/api/state":
                state = self.server.panel.status()
                with self.server.arena.lock:
                    season = self.server.arena.session
                    state["arena_unfinished"] = bool(season and season["status"] != "DONE")
                state["exchange_unfinished"] = any(self.server.exchange.status()["active"].values())
                return self.send(200, state)
            if self.path == "/api/exchange/state":
                return self.send(200, self.state_for("exchange"))
            if self.path.startswith("/api/exchange/runs/"):
                try:
                    return self.send(200, self.server.exchange.history(self.path.rsplit("/", 1)[-1]))
                except ArenaError as error:
                    return self.send(404, {"error": str(error)})
            if self.path == "/api/arena/state":
                return self.send(200, self.state_for("arena"))
            if self.path == "/api/workshop/state":
                return self.send(200, self.server.workshop.status())
            if self.path.startswith(("/api/workshop/versions/", "/api/workshop/trials/")):
                try:
                    method = self.server.workshop.detail if self.path.startswith("/api/workshop/versions/") else self.server.workshop.trial
                    return self.send(200, method(self.path.rsplit("/", 1)[-1]))
                except ArenaError as error:
                    return self.send(404, {"error": str(error)})
            if self.path.startswith("/api/arena/seasons/"):
                try:
                    return self.send(200, self.server.arena.history(self.path.removeprefix("/api/arena/seasons/")))
                except ArenaError as error:
                    return self.send(404, {"error": str(error)})
            if self.path.startswith("/api/history/"):
                try:
                    return self.send(200, self.server.panel.history(self.path.removeprefix("/api/history/")))
                except LookupError:
                    return self.send(404, {"error": "Цикл не знайдено."})
            files = {"/": ("arena.html", "text/html; charset=utf-8"),
                     "/arena": ("arena.html", "text/html; charset=utf-8"),
                     "/workshop": ("workshop.html", "text/html; charset=utf-8"),
                     "/review": ("review.html", "text/html; charset=utf-8"),
                     "/review.js": ("review.js", "text/javascript; charset=utf-8"),
                     "/review.css": ("review.css", "text/css; charset=utf-8"),
                     "/league": ("league.html", "text/html; charset=utf-8"),
                     "/league.js": ("league.js", "text/javascript; charset=utf-8"),
                     "/league.css": ("league.css", "text/css; charset=utf-8"),
                     "/workshop.js": ("workshop.js", "text/javascript; charset=utf-8"),
                     "/strategy-research.js": ("strategy-research.js", "text/javascript; charset=utf-8"),
                     "/demo-execution.js": ("demo-execution.js", "text/javascript; charset=utf-8"),
                     "/workshop.css": ("workshop.css", "text/css; charset=utf-8"),
                     "/live": ("index.html", "text/html; charset=utf-8"),
                     "/arena.js": ("arena.js", "text/javascript; charset=utf-8"),
                     "/arena.css": ("arena.css", "text/css; charset=utf-8"),
                     "/arena-strategy.js": ("arena-strategy.js", "text/javascript; charset=utf-8"),
                     "/arena-strategy.css": ("arena-strategy.css", "text/css; charset=utf-8"),
                     "/arena-lifecycle.js": ("arena-lifecycle.js", "text/javascript; charset=utf-8"),
                     "/arena-lifecycle.css": ("arena-lifecycle.css", "text/css; charset=utf-8"),
                     "/arena-coliseum.js": ("arena-coliseum.js", "text/javascript; charset=utf-8"),
                     "/arena-coliseum.css": ("arena-coliseum.css", "text/css; charset=utf-8"),
                     "/bot-waiting.js": ("bot-waiting.js", "text/javascript; charset=utf-8"),
                     "/battle-timeline.js": ("battle-timeline.js", "text/javascript; charset=utf-8"),
                     "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                     "/history.js": ("history.js", "text/javascript; charset=utf-8"),
                     "/style.css": ("style.css", "text/css; charset=utf-8")}
            # Explicit public asset names only; no user-supplied filesystem path.
            assets = {
                "coliseum-floor.png": "image/png", "human-poses.png": "image/png",
                "robot-poses.png": "image/png", "cash.png": "image/png",
                "human-portrait.png": "image/png", "robot-portrait.png": "image/png",
                "icons.css": "text/css; charset=utf-8", "Phosphor.woff2": "font/woff2",
                "CormorantGaramond.ttf": "font/ttf", "Manrope.ttf": "font/ttf",
            }
            files.update({"/assets/" + name: ("assets/" + name, kind) for name, kind in assets.items()})
            files.update({"/" + name: (name, "text/javascript; charset=utf-8") for name in
                ("event-replay.js", "review-model.js", "review-verify.js", "replay-labels.js", "research-panels.js", "arena-battle-view.js", "workshop-tools-ui.js", "poll-scheduler.js", "battle-facts.js", "league-stats.js", "author-report.js", "battle-context.js",
                 "live-leverage.js")})  # R2.1.15: the Live Start form's leverage
            files.update({"/" + name: (name, "text/css; charset=utf-8") for name in ("research-panels.css", "workshop-tools-ui.css")})
            if route not in files:
                return self.send(404, {"error": "Not found"})
            name, kind = files[route]
            content = (self.server.panel.root / "web" / name).read_bytes()
            if name in {"index.html", "arena.html", "workshop.html", "league.html"}:
                content = content.replace(b"__PANEL_TOKEN__", self.server.panel.token.encode())
            self.send(200, content, kind)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            self.send(503, {"error": "Не вдалося прочитати локальний стан. Торгівля не запускається."})

    def do_POST(self):
        try:
            self.handle_post()
        finally:
            # Whatever the write did (or refused to do), scheduled polls load again; a load that began before this
            # call is not stored (ViewerSnapshots epoch), so no pre-write copy survives.
            self.server.viewer.invalidate()

    def handle_post(self):
        if (not self.local_request() or self.headers.get("Origin") != self.server.origin
                or not secrets.compare_digest(self.headers.get("X-Panel-Token", ""), self.server.panel.token)):
            return self.send(403, {"error": "Відкрий панель напряму на цьому комп’ютері."})
        arena_routes = {"/api/arena/start", "/api/arena/stop", "/api/arena/resume", "/api/arena/strategies"}
        workshop_routes = {"/api/workshop/versions", "/api/workshop/train", "/api/workshop/freeze", "/api/workshop/promote", "/api/workshop/tools"}
        exchange_routes = {"/api/exchange/check", "/api/exchange/start", "/api/exchange/stop", "/api/exchange/resume"}
        metadata_routes = {"/api/league/action", "/api/ops/record", "/api/ops/stop-recording", "/api/ops/export-tape"}
        if self.path not in {"/api/play", "/api/stop", "/api/balance"} | arena_routes | workshop_routes | exchange_routes | metadata_routes:
            return self.send(404, {"error": "Not found"})
        if (self.headers.get("Content-Type") != "application/json" or self.headers.get("Transfer-Encoding")
                or not self.headers.get("Content-Length", "").isdigit()
                or int(self.headers["Content-Length"]) > (131072 if self.path in arena_routes | workshop_routes | metadata_routes else 4096 if self.path in exchange_routes else 512)):
            return self.send(400, {"error": "Invalid request"})
        try:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if not isinstance(body, dict):
                raise ValueError()
            if self.path == "/api/league/action":
                if set(body) != {"generation", "action", "payload"}: raise ArenaError("Нечинна дія ліги.")
                return self.send(200, self.server.league.action(**body))
            if self.path == "/api/ops/record":
                if set(body) != {"interval_seconds", "max_observations"}: raise ArenaError("Нечинний запис даних.")
                return self.send(202, self.server.recorder.start_recording(owner_local=True, **body))
            if self.path == "/api/ops/stop-recording":
                if body: raise ArenaError("Нечинний запит.")
                return self.send(200, self.server.recorder.request_stop_recording())
            if self.path == "/api/ops/export-tape":
                if set(body) != {"dataset_id"} or not RUN_ID.fullmatch(body["dataset_id"]): raise ArenaError("Нечинний запис даних.")
                destination = self.server.recorder.root / "exports" / (body["dataset_id"] + ".snapshots.jsonl")
                return self.send(200, self.server.recorder.export_training_tape(body["dataset_id"], destination))
            if self.path in arena_routes:
                return self.arena_post(body)
            if self.path in workshop_routes:
                return self.workshop_post(body)
            if self.path in exchange_routes:
                return self.exchange_post(body)
            if self.path == "/api/play":
                if set(body) != {"generation"}:
                    raise ValueError()
                if self.server.exchange.status()["active"]["arena_live"]:
                    raise PanelError("Заверши біржову live-арену перед запуском старого бота.")
                return self.send(202, self.server.panel.play(body["generation"]))
            if self.path == "/api/stop":
                if set(body) != {"generation", "id"}:
                    raise ValueError()
                return self.send(202, self.server.panel.stop(body["generation"], body["id"]))
            if body:
                raise ValueError()
            self.send(200, self.server.panel.refresh_balance())
        except (PanelError, ArenaError) as error:
            self.send(409, {"error": str(error)})
        except PreflightError:
            self.send(502, {"error": "Баланс недоступний. Перевір локальні ключі та зв’язок з OKX."})
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            self.send(503, {"error": "Не вдалося виконати дію. Онови стан; повторний вхід автоматично не надсилається."})

    def workshop_post(self, body):
        workshop = self.server.workshop
        if self.path == "/api/workshop/tools":
            from .workshop_diagnostics import report
            with workshop.lock:
                return self.send(200, report(workshop, body))
        expected = {"versions": {"generation", "strategy", "parent_id"},
                    "train": {"generation", "version_id", "dataset_id", "budget_usdt"},
                    "freeze": {"generation", "version_id"}, "promote": {"generation", "version_id"}}
        action = self.path.rsplit("/", 1)[-1]
        if set(body) != expected[action]:
            raise ArenaError("Неповний або невідомий запит до майстерні.")
        method = workshop.add_version if action == "versions" else getattr(workshop, action)
        self.send(202, method(**body))

    def exchange_post(self, body):
        action = self.path.rsplit("/", 1)[-1]
        expected = {"check": {"generation", "account_ids"},
                    "start": {"generation", "context", "strategy_ids", "account_ids", "budget_usdt", "duration_seconds"},
                    "stop": {"generation", "id", "bot_id"}, "resume": {"generation", "id"}}
        if action == "start" and "execution_mode" in body:
            expected[action] = expected[action] | {"execution_mode"}
        if action == "start" and "leverage" in body:  # R2.1.15: one whole number per bot; without it every bot 1x
            expected[action] = expected[action] | {"leverage"}
        if action == "check" and "strategy_ids" in body:  # R2.1.12 (MIX): the recipes name each account's contract
            expected[action] = expected[action] | {"strategy_ids"}
        if set(body) != expected[action]:
            raise ArenaError("Неповний або невідомий біржовий запит.")
        if action == "start" and body["context"] == "arena_live":
            legacy = self.server.panel.status()
            if legacy["running"] or legacy["action"] != "new":
                raise ArenaError("Спершу заверши старий live-цикл на /live.")
        self.send(200 if action == "check" else 202, getattr(self.server.exchange, action)(**body))

    def arena_post(self, body):
        arena = self.server.arena
        expected = {"/api/arena/start": {"generation", "strategy_ids", "budget_usdt", "duration_seconds"},
                    "/api/arena/stop": {"generation", "season_id", "bot_id"},
                    "/api/arena/resume": {"generation", "season_id"},
                    "/api/arena/strategies": {"generation", "strategy"}}
        allowed = expected[self.path]
        if self.path == "/api/arena/start" and "execution_mode" in body:
            allowed = allowed | {"execution_mode"}
        if set(body) != allowed:
            raise ArenaError("Неповний або невідомий запит до арени.")
        if self.path.endswith("/start"):
            result = arena.start(**body)
        elif self.path.endswith("/stop"):
            result = arena.stop(**body)
        elif self.path.endswith("/resume"):
            result = arena.resume(**body)
        else:
            if not isinstance(body["strategy"], dict) or not isinstance(body["strategy"].get("author"), dict):
                raise ArenaError("Потрібен паспорт стратегії.")
            result = arena.add_strategy(body["generation"], body["strategy"])
        self.send(202, result)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local Jev dashboard; no trade on start or page load.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="Open the page, reusing a running local panel if available.")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("invalid port")
    if args.open and args.port:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/api/state", timeout=2) as response:
                existing = json.loads(response.read(262145))
            if existing.get("bot", {}).get("id") in {BOT["id"], "jev-position-v2", "jev-roundtrip"}:
                webbrowser.open(f"http://127.0.0.1:{args.port}")
                return
        except (OSError, ValueError, AttributeError):
            pass
    panel = Panel()
    with Server(panel, args.port) as server:
        if sys.stdout is not None:
            print("Jev panel: " + server.origin + " (no trade starts automatically)", flush=True)
        if args.open:
            webbrowser.open(server.origin)
        try:
            server.serve_forever(poll_interval=.5)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
