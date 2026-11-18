"""Owner-operated SHADOW episode runner sharing the existing Jev ledger.

No default gate/key/transport, order callback, entry gate or exchange controller.
SharedJevGate.decide is never called: its proceed/hold candidate API is a separate
feature. We use its existing reserve/finish accounting and send-once adapter.
"""
import json
import re
import sqlite3

from .shadow_jev import build_pack, consume, canonical, finite


class OwnerShadowEpisodeRunner:
    def __init__(self, gate, *, owner_enabled=False, fixture_only=False):
        # Require the existing ledger interface, not a replacement budget or
        # shadow-only new series that could bypass prior attempts/unknown costs.
        if any(not hasattr(gate, name) for name in ("root", "ledger", "transport", "key_reader", "clock", "timer")):
            raise ValueError("existing_shared_jev_gate_required")
        if any(not hasattr(gate.ledger, name) for name in ("connect", "reserve", "finish", "summary", "budget", "path")):
            raise ValueError("existing_arena_jev_ledger_required")
        if fixture_only and getattr(gate.transport, "is_fake", False) is not True:
            raise ValueError("fixture_shadow_transport_required")
        self.gate, self.owner_enabled, self.fixture_only = gate, owner_enabled is True, fixture_only is True
        with gate.ledger.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS shadow_episodes_v1 (season TEXT NOT NULL, episode TEXT NOT NULL, "
                       "payload_sha256 TEXT NOT NULL, status TEXT NOT NULL, reservation_id TEXT, result_json TEXT, "
                       "PRIMARY KEY(season,episode))")
            db.execute("CREATE TABLE IF NOT EXISTS shadow_payloads_v1 (payload_sha256 TEXT PRIMARY KEY, body BLOB NOT NULL)")

    def _base(self, reason, *, attempted=False):
        return {"schema": "owner-shadow-episode-v1", "reason": reason, "labels": {}, "allowed": [],
                "execution_effect": "NONE", "attempted": attempted, "reported_cost_usd": None,
                "fixture_only": self.fixture_only, "retry_permitted": False, "order_control_callbacks": False}

    def _claim(self, season, episode, payload_hash, body):
        with self.gate.ledger.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT payload_sha256 FROM shadow_episodes_v1 WHERE season=? AND episode=?",
                                  (season, episode)).fetchone()
            if previous:
                return "episode_already_claimed_no_retry"
            count = db.execute("SELECT COUNT(*) FROM shadow_episodes_v1").fetchone()[0]
            if count >= self.gate.ledger.budget["total_attempts"]:
                return "shadow_episode_resource_capacity"
            db.execute("INSERT INTO shadow_episodes_v1 VALUES(?,?,?,'claimed',NULL,NULL)",
                       (season, episode, payload_hash))
            old = db.execute("SELECT body FROM shadow_payloads_v1 WHERE payload_sha256=?", (payload_hash,)).fetchone()
            if old and bytes(old[0]) != body: raise ValueError("shadow_payload_hash_collision")
            db.execute("INSERT OR IGNORE INTO shadow_payloads_v1 VALUES(?,?)", (payload_hash, body))
        return None

    def _record(self, season, episode, result, reservation=None):
        body = canonical(result).decode("utf-8")
        if len(body.encode("utf-8")) > 65536:
            raise ValueError("shadow_result_size")
        with self.gate.ledger.connect() as db:
            db.execute("UPDATE shadow_episodes_v1 SET status=?,reservation_id=COALESCE(?,reservation_id),result_json=? "
                       "WHERE season=? AND episode=?", ("attempted" if result["attempted"] else "not_sent",
                                                         reservation, body, season, episode))
        return result

    def episode_status(self, season, episode):
        with self.gate.ledger.connect() as db:
            row = db.execute("SELECT status,payload_sha256,reservation_id,result_json FROM shadow_episodes_v1 "
                             "WHERE season=? AND episode=?", (season, episode)).fetchone()
        if row is None:
            return {"status": "NOT_CLAIMED", "execution_effect": "NONE"}
        return {"status": row[0], "payload_sha256": row[1], "reservation_exists": row[2] is not None,
                "result": json.loads(row[3]) if row[3] else None, "retry_permitted": False,
                "execution_effect": "NONE"}

    def evaluate_episode(self, season, episode, context, setups, *, current_source_sha256=None):
        if not self.fixture_only and not self.owner_enabled:
            return self._base("owner_shadow_not_enabled")
        if any(not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}", value)
               for value in (season, episode)):
            return self._base("shadow_episode_identity")
        try:
            pack = build_pack(context, setups)
        except (ValueError, KeyError, TypeError, OverflowError):
            return self._base("invalid_market_shadow_payload_no_split")
        try:
            refusal = self._claim(season, episode, pack.sha256, pack.body)
        except sqlite3.Error:
            return self._base("episode_registry_unavailable")
        if refusal:
            return self._base(refusal)
        result = self._base("not_sent")
        result.update(payload_sha256=pack.sha256, payload_bytes=len(pack.body), question_count=len(pack.payload["questions"]))
        reservation = None
        try:
            key = self.gate.key_reader(self.gate.root)
            reservation = self.gate.ledger.reserve(season, pack.sha256, self.gate.clock())
        except (OSError, ValueError, TypeError, sqlite3.Error):
            result["reason"] = "key_or_existing_ledger_unavailable"
        else:
            result["reason"] = "budget_or_unknown_billing" if reservation is None else "reserved"
        if reservation is None:
            try:
                return self._record(season, episode, result)
            except (sqlite3.Error, OSError, ValueError):
                return {**result, "reason": "episode_registry_write_failed"}
        # Both reservation and episode→reservation link are durable before any
        # send. Interrupted claims are never eligible for an automatic resend.
        try:
            with self.gate.ledger.connect() as db:
                db.execute("UPDATE shadow_episodes_v1 SET status='send_reserved',reservation_id=? WHERE season=? AND episode=?",
                           (reservation, season, episode))
            started = self.gate.timer()
            if not finite(started):
                raise ValueError("timer_unavailable")
        except (sqlite3.Error, ValueError, OSError):
            return {**result, "reason": "send_reservation_unconfirmed_no_send"}
        result["attempted"] = True
        response = None
        try:
            response = self.gate.transport(pack.body, key)
        except Exception:
            pass  # Never persist HTTP/provider exception text, bodies or key.
        try:
            elapsed_ms = (self.gate.timer() - started) * 1000
        except Exception:
            elapsed_ms = float("inf")
        source_hash = context["snapshot_sha256"] if current_source_sha256 is None else current_source_sha256
        typed = consume(pack, response, elapsed_ms=elapsed_ms, source_sha256=source_hash)
        result.update(typed)
        result["billing_unknown"] = result["reported_cost_usd"] is None
        if result["billing_unknown"]:
            result.update(labels={}, reason="billing_unknown")
        maximum_age_ms = self.gate.ledger.budget["maximum_answer_age_seconds"] * 1000
        if finite(elapsed_ms) and elapsed_ms > maximum_age_ms:
            result.update(labels={}, reason="answer_expired")
        result["elapsed_ms"] = round(elapsed_ms, 3) if finite(elapsed_ms) and elapsed_ms >= 0 else None
        try:
            self.gate.ledger.finish(reservation, result["reported_cost_usd"])
        except sqlite3.Error:
            result.update(labels={}, reason="existing_ledger_write_failed")
        try:
            return self._record(season, episode, result, reservation)
        except (sqlite3.Error, OSError, ValueError):
            return {**result, "labels": {}, "reason": "episode_registry_write_failed"}
