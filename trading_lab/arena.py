"""Local PAPER seasons; public market data, optional bounded Jev, separate from V3."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time

from .arena_engine import (RULES, emit, failed_snapshot, frame, iso, new_season, pause,
                           public_season, request_stop, tick)
from .arena_market import PublicFeed
from .arena_strategies import ArenaError, catalogue_defaults, number, validate_strategy, fingerprint
from .match_protocol import MAX_DURATION_SECONDS, JOURNAL_BYTES, close_admission, install, validate_manifest
from .match_protocol import draining_policy
from .match_journal import append_events, checkpoint, restore_events, compact_memory
from .ops_storage import replace_with_retry
from . import strategy_runtime as runtime

IDENTITY = re.compile(r"[0-9a-f]{32}")


SOURCE_FILES = ("ikigai_g1.py", "ikigai_g1_reference.py", "causal_htf.py", "htf_feed.py", "arena.py", "arena_engine.py", "arena_market.py",
                "arena_strategies.py", "strategy_v2.py", "strategy_v4.py", "strategy_v4_book.py", "strategy_v4_feed.py", "strategy_v4_runtime.py", "strategy_runtime.py", "strategy_contract.py", "ops_policy.py", "ops_storage.py",
                "cycle_probe.py", "cycle_diagnostics.py", "match_protocol.py", "match_journal.py", "arena_jev.py", "jev.py", "risk.py")


def arena_source_hash(folder=None):
    """sha256 over the arena sources with only CRLF -> LF normalised (D098)."""
    folder = Path(folder or Path(__file__).parent)
    return hashlib.sha256(b"".join((folder / name).read_bytes().replace(b"\r\n", b"\n") for name in SOURCE_FILES)).hexdigest()


LOADED_SOURCE_HASH = arena_source_hash()


def read(path, default=None):
    if not path.exists():
        return deepcopy(default)
    if path.stat().st_size > 512 * 1024 * 1024:
        raise ArenaError("Файл сезону перевищує локальний ліміт.")
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ArenaError("Пошкоджений локальний журнал арени.")
    return result


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    target = path.with_suffix(path.suffix + ".tmp")
    with target.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        file.flush()
        os.fsync(file.fileno())
    replace_with_retry(target, path)


@contextmanager
def exclusive(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    file = path.open("a+b")
    file.seek(0, 2)
    if not file.tell():
        file.write(b"0"); file.flush()
    file.seek(0)
    locked = False
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        yield
    except OSError:
        if not locked:
            raise ArenaError("Арена вже відкрита іншим процесом; онови її стан.") from None
        raise
    finally:
        if locked:
            file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        file.close()


class Arena:
    def __init__(self, root, feed_factory=PublicFeed, clock=time.time, background=True, jev_factory=None):
        self.root = Path(root)
        self.path = self.root / "runtime" / "arena_v4"
        self.manifest_path = self.path / "manifest.json"
        self.lock = threading.RLock()
        self.feed_factory, self.clock, self.background = feed_factory, clock, background
        self.jev_factory, self.jev_gate = jev_factory, None
        self.defaults = catalogue_defaults()
        self.thread = None
        self.wakeup = threading.Event()
        self.shutdown_requested = threading.Event()
        self.session = None
        self.owner = None
        self.admission_deadline = None
        self.event_position, self.checkpoint_time = 0, 0
        self.data = read(self.manifest_path, {"generation": 0, "strategies": [], "seasons": [], "active_id": None})
        # Merely opening a page never resumes a season or opens the market feed.
        if self.data["active_id"]:
            self.session = self.load(self.data["active_id"])
        # Exactly one manager can mutate this workspace's catalogue/journals, even on another port.
        self.control_owner = exclusive(self.path / "manager.lock")
        self.control_owner.__enter__()

    def load(self, identity, *, reader=False):
        """reader=True (journal_copy): nothing in the engine changes; the caller reads the journal itself."""
        if not isinstance(identity, str) or not IDENTITY.fullmatch(identity) or identity not in self.data["seasons"]:
            raise ArenaError("Сезон не знайдено.")
        state = read(self.path / (identity + ".json"))
        if not state or state.get("id") != identity or state.get("mode") != "PAPER":
            raise ArenaError("Пошкоджений журнал сезону.")
        validate_manifest(state)
        if draining_policy(state) and not reader:
            count = restore_events(self.path / (identity + ".events.jsonl"), state)
            if identity == self.data.get("active_id"):
                self.event_position = count
        return state

    def save(self, *, force=True):
        if self.session:
            if draining_policy(self.session):
                self.event_position = append_events(self.path / (self.session["id"] + ".events.jsonl"),
                                                    self.session, self.event_position)
                if (not force and time.monotonic() - self.checkpoint_time
                        < self.session["rules"]["checkpoint_interval_seconds"]):
                    return
            write(self.path / (self.session["id"] + ".json"),
                  checkpoint(self.session) if draining_policy(self.session) else self.session)
            self.checkpoint_time = time.monotonic()
            compact_memory(self.session)
        write(self.manifest_path, self.data)

    def check_generation(self, generation):
        if type(generation) is not int or generation != self.data["generation"]:
            raise ArenaError("Стан змінився. Онови сторінку й повтори дію.")

    def catalogue(self):
        return self.defaults + self.data["strategies"]

    def status(self):
        with self.lock:
            seasons = []
            for identity in reversed(self.data["seasons"]):
                state = self.session if self.session and self.session["id"] == identity else self.load(identity)
                ranked = sorted(state["bots"], key=lambda b: b["equity_usdt"], reverse=True)
                top = ranked[0]["equity_usdt"]
                leader = " / ".join(b["name"] for b in ranked if abs(b["equity_usdt"] - top) < 1e-8)
                seasons.append({"id": identity, "created_at": state["created_at"], "status": self.shown_status(state),
                                "execution_mode": state.get("execution_mode", "rules"), "jev_usage": deepcopy(state.get("jev_usage")),
                                "duration_seconds": state["duration_seconds"], "budget_usdt": state["budget_usdt"],
                                "participants": len(state["bots"]), "leader_name": leader,
                                "winner_name": leader if state["status"] == "DONE" and not state["incomplete"] else None,
                                "incomplete": state["incomplete"]})
            active = self.public(self.session, compact=True) if self.session else None
            return {"api_version": 4, "mode": "PAPER", "generation": self.data["generation"],
                    "active_id": self.data["active_id"], "active": active, "catalogue": deepcopy(self.catalogue()),
                    "seasons": seasons, "rules": RULES}

    def shown_status(self, state):
        # A stopped server isn't evidence of a closed virtual position.
        if state["status"] in {"RUNNING", "DRAINING", "CLOSING"} and self.owner is None:
            return "PAUSED"
        return state["status"]

    def public(self, state, *, compact=False):
        shown_status = self.shown_status(state)
        result = public_season({**state, "status": shown_status}, compact=compact)
        if shown_status == "PAUSED" and state["status"] != "PAUSED":
            result["error"] = "Сервер перезапущено. Рецепти не поновлюються автоматично; доступне лише закриття залишку."
            result["incomplete"] = True
            result["phase"] = "PAUSED"
        return result

    def history(self, identity):
        with self.lock:
            state = self.session if self.session and self.session["id"] == identity else self.load(identity)
            state = deepcopy(state)
            restore_events(self.path / (identity + ".events.jsonl"), state, full=True)
            return self.public(state)

    def add_strategy(self, generation, payload):
        from .cycle_probe import is_probe
        if isinstance(payload, dict) and is_probe(payload):
            raise ArenaError("Технічний план дозволено лише в Demo майстерні, не на арені.")
        with self.lock:
            self.check_generation(generation)
            # Same displayed name/author creates a new immutable version, never edits a running recipe.
            if not isinstance(payload, dict) or not isinstance(payload.get("author"), dict):
                raise ArenaError("Потрібен паспорт стратегії.")
            prior = [s for s in self.catalogue() if s["name"] == payload.get("name") and s["author"]["name"] == payload["author"].get("name")]
            version = max([s["version"] for s in prior], default=0) + 1
            strategy = validate_strategy(payload, version)
            self.data["strategies"].append(strategy)
            self.data["generation"] += 1
            write(self.manifest_path, self.data)
            return {"id": strategy["id"], "version": version}

    def import_frozen_strategy(self, strategy, provenance):
        """Workshop-only promotion preserves the tested ID/hash; retries are idempotent."""
        from .strategy_contract import payload_of
        from .cycle_probe import is_probe
        if is_probe(strategy):
            raise ArenaError("Технічний план не допускається на арену.")
        with self.lock:
            validated = validate_strategy(payload_of(strategy), strategy["version"])
            if (validated["recipe_sha256"] != strategy["recipe_sha256"] or not IDENTITY.fullmatch(strategy["id"])
                    or (strategy.get("strategy_schema_version") == 4 and validated["rules"] != strategy["rules"])):
                raise ArenaError("Не збігається зафіксований рецепт.")
            prior = next((s for s in self.catalogue() if s["id"] == strategy["id"]), None)
            if prior:
                if prior["recipe_sha256"] != strategy["recipe_sha256"]:
                    raise ArenaError("Конфлікт ідентифікатора рецепта.")
                return {"id": prior["id"], "version": prior["version"], "already_registered": True}
            clean = deepcopy(strategy)
            clean["workshop"] = deepcopy(provenance)
            self.data["strategies"].append(clean)
            self.data["generation"] += 1
            write(self.manifest_path, self.data)
            return {"id": clean["id"], "version": clean["version"]}

    def acquire(self):
        owner = exclusive(self.path / "season.lock")
        owner.__enter__()
        self.owner = owner

    def release(self):
        if self.owner is not None:
            owner, self.owner = self.owner, None
            owner.__exit__(None, None, None)

    def start(self, generation, strategy_ids, budget_usdt, duration_seconds, execution_mode="rules"):
        with self.lock:
            self.check_generation(generation)
            if arena_source_hash() != LOADED_SOURCE_HASH:
                raise ArenaError("Код оновлено. Перезапусти панель через Update Jev Trading Lab перед новим сезоном.")
            if execution_mode not in ("rules", "shared_jev"):
                raise ArenaError("Невідомий runtime сезону.")
            if self.session and self.session["status"] != "DONE":
                raise ArenaError("Спочатку заверши попередній сезон або віднови його закриття.")
            budget = number(budget_usdt, 25, 1000)
            duration = number(duration_seconds, 300, MAX_DURATION_SECONDS)
            if int(duration) != duration:
                raise ArenaError("Тривалість має бути цілою кількістю секунд.")
            if (not isinstance(strategy_ids, list) or not 1 <= len(strategy_ids) <= RULES["max_participants"]
                    or any(not isinstance(i, str) for i in strategy_ids) or len(set(strategy_ids)) != len(strategy_ids)):
                raise ArenaError("Вибери від 1 до 6 різних готових стратегій.")
            catalogue = {s["id"]: s for s in self.catalogue()}
            if any(i not in catalogue or catalogue[i]["status"] != "ready" for i in strategy_ids):
                raise ArenaError("Стратегія ще не готова; потрібен погоджений рецепт.")
            from .cycle_probe import is_probe
            if any(is_probe(catalogue[i]) for i in strategy_ids):
                raise ArenaError("Технічний план не допускається на арену.")
            if execution_mode == "shared_jev" and any(catalogue[i].get("strategy_schema_version") == 4 for i in strategy_ids):
                raise ArenaError("Схема 4 виконується лише за правилами: Jev її входів не перевіряє.")
            from .strategy_contract import recipe_intact
            if any(catalogue[i].get("strategy_schema_version") == 4 and not recipe_intact(catalogue[i]) for i in strategy_ids):
                raise ArenaError("Схема 4: правила двигуна не відповідають зафіксованому рецепту.")
            if any(catalogue[i].get("strategy_schema_version") == 4 and catalogue[i]["instrument"] != "BTC-USDT-SWAP" for i in strategy_ids):
                raise ArenaError("PAPER-арена читає ринок BTC-USDT-SWAP; рецепт іншого інструмента іде окремим сезоном OKX Demo / Live.")
            from .ikigai_g1 import enabled, G1Error
            if execution_mode != "rules" and any(enabled(catalogue[i]) for i in strategy_ids):
                raise G1Error("g1_sharedjev_forbidden_before_gate_creation")
            self.acquire()
            try:
                code_hash = LOADED_SOURCE_HASH
                next_session = new_season([catalogue[i] for i in strategy_ids], budget, duration, self.clock(), code_hash)
                next_session["execution_mode"] = execution_mode
                next_gate = None
                if execution_mode == "shared_jev":
                    from .arena_jev import SharedJevGate, BUDGET
                    next_gate = (self.jev_factory or SharedJevGate)(self.root)
                    next_session["rules"]["ai_role"] = "Спільний Jev підтверджує або відхиляє дозволені entry/add. Ризик і вихід — детермінований код."
                    next_session["rules"]["jev_budget"] = deepcopy(BUDGET)
                    next_session["jev_usage"] = next_gate.ledger.summary(next_session["id"])
                    for bot in next_session["bots"]:
                        bot.update(ai_requests=0, ai_cost_complete=True)
                from .arena_strategies import fingerprint
                next_session["config_sha256"] = fingerprint({"strategies": next_session["strategies"], "budget": budget,
                    "duration": duration, "rules": next_session["rules"], "execution_mode": execution_mode})
                install(next_session, jev_budget=next_session["rules"].get("jev_budget"))
                next_session["events"][0]["data"]["config_sha256"] = next_session["config_sha256"]
                self.session, self.jev_gate = next_session, next_gate
                if next_gate:
                    def record_request(payload, payload_hash, requested_ms, candidates):
                        with self.lock:
                            emit(next_session, requested_ms / 1000, None, 2, "jev_request", "Структурований запит записано до виклику Jev.",
                                payload=payload, payload_sha256=payload_hash, request_at_ms=requested_ms,
                                payload_wire_json=json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
                                payload_content_sha256=fingerprint(payload),
                                candidate_ids=[c["id"] for c in candidates], snapshot_sha256=(next_session.get("_last") or {}).get("sha256"),
                                observed_at=(next_session.get("_last") or {}).get("at"), bar_time_ms=(next_session.get("_last") or {}).get("bar_close_ms"))
                            self.save()
                    next_gate.before_send = record_request
                self.event_position, self.checkpoint_time = 0, 0
                self.admission_deadline = time.monotonic() + duration
                self.data["seasons"].append(self.session["id"])
                self.data["active_id"] = self.session["id"]
                self.data["generation"] += 1
                self.save()
                self.launch()
            except Exception:
                self.release()
                raise
            return {"id": self.session["id"], "mode": "PAPER"}

    def stop(self, generation, season_id, bot_id=None):
        with self.lock:
            self.check_generation(generation)
            if not self.session or self.session["id"] != season_id:
                raise ArenaError("Це не поточний сезон.")
            if self.owner is None or self.session["status"] == "PAUSED":
                raise ArenaError("Сезон призупинено. Використай відновлення закриття.")
            request_stop(self.session, self.clock(), bot_id, origin="owner")
            self.data["generation"] += 1
            self.save()
            self.wakeup.set()
            return {"id": season_id, "accepted": True}

    def resume(self, generation, season_id):
        with self.lock:
            self.check_generation(generation)
            if not self.session or self.session["id"] != season_id or self.shown_status(self.session) != "PAUSED":
                raise ArenaError("Немає призупиненого сезону для відновлення.")
            if self.owner is not None:
                raise ArenaError("Працівник ще завершує зупинку. Онови стан.")
            self.acquire()
            try:
                state = self.session
                state["incomplete"] = True
                request_stop(state, self.clock())
                state["_closing_since"] = self.clock()
                state["_failures"] = 0
                emit(state, self.clock(), None, 6, "recovery_started", "Відновлення: лише закриття віртуального залишку, без нових входів.")
                self.data["generation"] += 1
                self.save()
                self.launch()
            except Exception:
                self.release()
                raise
            return {"id": season_id, "mode": "PAPER"}

    def launch(self):
        if self.background:
            self.shutdown_requested.clear()
            self.wakeup.clear()
            self.thread = threading.Thread(target=self.work, name="paper-arena", daemon=True)
            self.thread.start()

    def advance(self, snapshot):
        """Offline fixtures inject shared market snapshots; production feed is public only."""
        candidates = []
        with self.lock:
            if snapshot is None:
                failed_snapshot(self.session, self.clock())
            else:
                if (self.admission_deadline is not None and time.monotonic() >= self.admission_deadline
                        and self.session["status"] == "RUNNING"):
                    close_admission(self.session, snapshot["at"], emit)
                # Persist the evidence before applying simulated fills. Raw fields never include credentials.
                raw_path = self.path / (self.session["id"] + ".snapshots.jsonl")
                encoded = json.dumps(snapshot, allow_nan=False, separators=(",", ":")) + "\n"
                try:
                    if len(encoded.encode("utf-8")) > self.session["rules"].get("snapshot_line_bytes", 2 * 1024**2):
                        raise ArenaError("snapshot_line_capacity")
                    if self.session.get("_snapshot_rows", 0) >= self.session["rules"].get("snapshot_rows", 10**9):
                        raise ArenaError("snapshot_row_capacity")
                    limit = self.session["rules"].get("snapshot_journal_bytes", JOURNAL_BYTES)
                    if (raw_path.stat().st_size if raw_path.exists() else 0) + len(encoded.encode("utf-8")) > limit:
                        raise ArenaError("snapshot_capacity")
                    with raw_path.open("a", encoding="utf-8", newline="\n") as file:
                        file.write(encoded)
                        file.flush(); os.fsync(file.fileno())
                    self.session["_snapshot_rows"] = self.session.get("_snapshot_rows", 0) + 1
                except (OSError, ArenaError):
                    if self.session.get("protocol_version", 0) < 8: raise
                    self.session.update(incomplete=True, admission_blocked="snapshot_storage")
                    emit(self.session, snapshot["at"], None, None, "recording_blocked", "Запис котирувань недоступний; нових входів немає, власні виходи тривають.")
                    close_admission(self.session, snapshot["at"], emit)
                tick(self.session, snapshot)
                if self.session.get("execution_mode") == "shared_jev" and self.session["status"] == "RUNNING":
                    recipes = {s["id"]: s for s in self.session["strategies"]}
                    for bot in self.session["bots"]:
                        pending = bot["_pending"]
                        if pending and pending["kind"] == "entry" and not pending.get("jev_requested"):
                            pending["jev_requested"] = True
                            recipe = recipes[bot["strategy_id"]]
                            candidates.append({"id": bot["id"], "pending": deepcopy(pending),
                                "recipe_sha256": recipe["recipe_sha256"], "side": runtime.side(recipe, bot),
                                "kind": "add" if bot["position_btc"] else "entry", "checks": deepcopy(bot["checks"]),
                                "rules": deepcopy(recipe["rules"]), "position_btc": bot["position_btc"],
                                "features": deepcopy(snapshot["features"]), "local_checks_passed": True})
                            emit(self.session, snapshot["at"], bot, 5 if bot["position_btc"] else 2, "jev_pending",
                                 "Очікуємо перевірку Jev; вхід ще не дозволений.")
                    if candidates:
                        frame(self.session, snapshot["at"], snapshot)
            self.save(force=self.session["status"] in {"DONE", "PAUSED"})
            season_id = self.session["id"]
            if not self.background and self.session["status"] in {"DONE", "PAUSED"}:
                self.release()
        if candidates:
            # Network is outside the state lock: Stop can cancel intents immediately while Jev replies.
            outcome = self.jev_gate.decide(season_id, candidates)
            with self.lock:
                if self.session["id"] != season_id:
                    return
                state = self.session
                from .arena_jev import BUDGET
                expired = self.clock() - snapshot["at"] > BUDGET["maximum_answer_age_seconds"]
                for candidate in candidates:
                    bot = next(b for b in state["bots"] if b["id"] == candidate["id"])
                    if outcome["attempted"]:
                        bot["ai_requests"] = bot.get("ai_requests", 0) + 1
                        if outcome["reported_cost_usd"] is None:
                            bot["ai_cost_complete"] = False
                        else:
                            # Provider prices the batch; equal allocation is explicitly an estimate per bot.
                            bot["ai_cost_usd"] += outcome["reported_cost_usd"] / len(candidates)
                    unchanged = bot["_pending"] == candidate["pending"]
                    allowed = (not expired and unchanged and not bot["stop_requested"]
                               and state["status"] == "RUNNING" and self.clock() < state["_end"]
                               and (self.admission_deadline is None or time.monotonic() < self.admission_deadline)
                               and bot["id"] in outcome["allowed"])
                    if unchanged:
                        if allowed:
                            bot["_pending"]["jev_approved"] = True
                        else:
                            from .arena_engine import cancel_entry
                            cancel_entry(state, bot)
                            bot["status"] = "LONG" if bot["position_btc"] > 0 else "SHORT" if bot["position_btc"] else "WAITING"
                    emit(state, snapshot["at"], bot, 5 if candidate["kind"] == "add" else 2, "jev_gate",
                         "Jev: підтверджено; перед fill буде повторна перевірка." if allowed else "Jev: HOLD; нового входу немає.",
                         allowed=allowed, reason="state_changed_or_expired" if expired or not unchanged else outcome["reason"],
                         answer=outcome["answers"].get(bot["id"]), attempted=outcome["attempted"],
                         elapsed_ms=outcome["elapsed_ms"], batch_cost_usd=outcome["reported_cost_usd"],
                         cost_allocation="equal_share_of_batch", model=outcome["model"],
                         kind=candidate["kind"], snapshot_sha256=snapshot["sha256"], recipe_sha256=candidate["recipe_sha256"],
                         payload_sha256=outcome.get("payload_sha256"), request_at_ms=outcome.get("request_at_ms"), reply_at_ms=int(self.clock()*1000),
                         observed_at=snapshot["at"], bar_time_ms=snapshot.get("bar_close_ms"))
                state["jev_usage"] = self.jev_gate.ledger.summary(season_id)
                frame(state, snapshot["at"], snapshot)
                self.save()

    @staticmethod
    def no_public_history(path, params):
        raise ArenaError("feed_without_public_history")

    def work(self):
        try:
            from .ikigai_g1 import enabled as uses_g1
            if self.feed_factory is PublicFeed and any(uses_g1(r) for r in self.session["strategies"]):
                from .htf_feed import HTFPublicFeed
                feed = HTFPublicFeed(self.path / (self.session["id"] + ".htf"))
            else:
                feed = self.feed_factory()
            from .strategy_v4_feed import BookFeed, specs_of
            book_specs = specs_of(self.session["strategies"])
            if book_specs:  # schema 4: the book reads public history through the same feed, on its own budget
                feed = BookFeed(feed, getattr(feed, "get_public", None) or self.no_public_history, book_specs,
                                budget=self.session["rules"].get("max_book_gets"),
                                admitting=lambda: self.session["status"] == "RUNNING")
            if hasattr(feed, "max_calls"):
                feed.max_calls = self.session["rules"].get("max_public_gets", 5000)
            if hasattr(feed, "calls"):
                feed.calls = self.session.get("public_get_count", 0)
            while not self.shutdown_requested.is_set():
                with self.lock:
                    if self.session["status"] not in {"RUNNING", "DRAINING", "CLOSING"}:
                        break
                try:
                    snapshot = feed.snapshot()
                except Exception:
                    snapshot = None  # Never serialize HTTP exception text, headers or environment.
                with self.lock:
                    self.session["public_get_count"] = getattr(feed, "calls", self.session.get("public_get_count", 0))
                self.advance(snapshot)
                self.wakeup.wait(RULES["poll_seconds"])
                self.wakeup.clear()
        except Exception:
            with self.lock:
                if self.session and self.session["status"] not in {"DONE", "PAUSED"}:
                    pause(self.session, self.clock(), "Локальна помилка. Сезон призупинено, журнал збережено; лише відновлення закриття.")
                    self.save()
        finally:
            with self.lock:
                self.release()
                if self.shutdown_requested.is_set():
                    self.release_control()

    def release_control(self):
        if self.control_owner is not None:
            owner, self.control_owner = self.control_owner, None
            owner.__exit__(None, None, None)

    def close(self):
        self.shutdown_requested.set()
        self.wakeup.set()
        if self.thread:
            self.thread.join(timeout=2)
        # On process shutdown an incomplete journal remains recoverable, never silently flat.
        if not self.thread or not self.thread.is_alive():
            self.release()
            self.release_control()
