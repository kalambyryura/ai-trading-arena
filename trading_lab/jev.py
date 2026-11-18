"""Bounded Jev connectivity adapter. Produces paper recommendations only."""

from dataclasses import dataclass, asdict
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import os
import sqlite3
import time
import urllib.error
import urllib.request
import uuid

from .risk import RiskPolicy, finite_number

ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
MODEL = "typesafe/jev-1.13"
MAX_BODY_BYTES = 8000
MAX_RESPONSE_BYTES = 262144
CONFIDENCE_THRESHOLD = 0.70  # Engineering heuristic; NOT probability of profit.


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_key(root):
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    path = Path(root) / ".env.local"
    if not key and path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() == "OPENROUTER_API_KEY":
                key = value.strip().strip("\"'")
                break
    if not key or "\n" in key or "\r" in key:
        raise ValueError("Missing or invalid local credential")
    return key


def send_once(body, key):
    request = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json",
    })
    # No application retry; refuses redirect so credentials stay on this endpoint.
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=12) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("Response too large")
    return json.loads(raw)


class BudgetLedger:
    """Shared SQLite reservations survive restart and failed/ambiguous requests.

    Initial deployment allows ONE attempt in total, not one per process/session.
    This limits calls, not the provider's total account-wide billing.
    """

    def __init__(self, path, max_attempts=1, cost_threshold_usd=0.005, cooldown_seconds=12):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_attempts = max_attempts
        self.cost_threshold_usd = cost_threshold_usd
        self.cooldown_seconds = cooldown_seconds
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, "
                       "started REAL NOT NULL, state_hash TEXT NOT NULL, status TEXT NOT NULL, "
                       "reported_cost REAL, outcome TEXT)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def reserve(self, state_hash, now):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT started,status,reported_cost FROM attempts").fetchall()
            if len(rows) >= self.max_attempts:
                return None
            if any(status != "accounted" or cost is None for _, status, cost in rows):
                return None
            if sum(row[2] for row in rows) >= self.cost_threshold_usd:
                return None
            if rows and now - max(row[0] for row in rows) < self.cooldown_seconds:
                return None
            token = uuid.uuid4().hex
            db.execute("INSERT INTO attempts(id,started,state_hash,status) VALUES(?,?,?,?)",
                       (token, now, state_hash, "reserved"))
            return token

    def finish(self, token, cost, outcome):
        status = "accounted" if cost is not None else "billing_unknown"
        with self.connect() as db:
            db.execute("UPDATE attempts SET status=?,reported_cost=?,outcome=? WHERE id=?",
                       (status, cost, outcome, token))

    def summary(self):
        with self.connect() as db:
            count, cost, unresolved = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(reported_cost),0),"
                "COALESCE(SUM(CASE WHEN status != 'accounted' THEN 1 ELSE 0 END),0) FROM attempts"
            ).fetchone()
        return {"attempts": count, "reported_cost_usd": cost,
                "unresolved_billing_attempts": unresolved, "max_attempts": self.max_attempts}


@dataclass(frozen=True)
class Decision:
    action: str = "hold"
    source: str = "local_fallback"
    reason: str = "not_requested"
    confidence: float | None = None
    elapsed_ms: float | None = None
    reported_cost_usd: float | None = None
    response_model: str | None = None
    live_execution_enabled: bool = False

    def as_dict(self):
        return asdict(self)


def make_payload(state):
    return {
        "model": MODEL,
        "state": state.as_state(),
        "questions": {"candidate_filter": {
            "type": "choice",
            "instructions": (
                "Evaluate only whether the supplied candidate can proceed to a synthetic paper "
                "test. This is a connectivity test, not a forecast of price or profitability. "
                "Never suggest changing size, stops, leverage or safety limits. "
                "If uncertain or inconsistent choose hold."
            ),
            "criteria": {
                "paper_allow": "The paper-mode candidate is internally consistent and has no obvious warning.",
                "hold": "The candidate is uncertain, contradictory, unsafe or not in paper mode.",
            },
        }},
    }


class JevClient:
    def __init__(self, root, ledger=None, policy=None, transport=send_once,
                 clock=time.time, timer=time.perf_counter, key_reader=read_key):
        self.root = Path(root)
        self.ledger = ledger or BudgetLedger(self.root / "runtime" / "jev_ledger.sqlite3")
        self.policy = policy or RiskPolicy()
        self.transport, self.clock, self.timer, self.key_reader = transport, clock, timer, key_reader

    def decide(self, snapshot, current_snapshot):
        initial_failure = self.policy.check(snapshot, self.clock())
        if initial_failure:
            return Decision(reason=initial_failure)
        try:
            body = json.dumps(make_payload(snapshot), ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(body) > MAX_BODY_BYTES:
                return Decision(reason="snapshot_too_large")
            key = self.key_reader(self.root)
        except (ValueError, TypeError, OSError):
            return Decision(reason="invalid_snapshot_or_missing_key")
        try:
            reservation = self.ledger.reserve(hashlib.sha256(body).hexdigest(), self.clock())
        except sqlite3.Error:
            return Decision(reason="ledger_unavailable")
        if reservation is None:
            return Decision(reason="request_budget_or_cooldown")
        started = self.timer()
        result, failure = None, None
        try:
            result = self.transport(body, key)
        except urllib.error.HTTPError as error:
            failure = "http_" + str(error.code)
        except (urllib.error.URLError, TimeoutError, ValueError, TypeError, OSError):
            failure = "request_failed_billing_unconfirmed"
        elapsed = round((self.timer() - started) * 1000, 3)
        result = result if isinstance(result, dict) else {}
        usage = result.get("usage")
        raw_cost = usage.get("cost") if isinstance(usage, dict) else None
        cost = raw_cost if finite_number(raw_cost) and raw_cost >= 0 else None
        answers = result.get("answers")
        answer = answers.get("candidate_filter") if isinstance(answers, dict) else None
        answer = answer if isinstance(answer, dict) else {}
        choice, confidence = answer.get("choice"), answer.get("confidence")
        valid = (isinstance(choice, str) and choice in {"hold", "paper_allow"}
                 and finite_number(confidence) and CONFIDENCE_THRESHOLD <= confidence <= 1)
        try:
            current = current_snapshot()
            current_failure = self.policy.check(current, self.clock())
            if current.revision != snapshot.revision or current != snapshot:
                current_failure = "state_changed"
        except Exception:
            current_failure = "current_state_unavailable"
        reason = failure or current_failure or (None if valid else "invalid_or_low_confidence_answer")
        action = choice if reason is None else "hold"
        try:
            self.ledger.finish(reservation, cost, reason or "valid_answer")
        except sqlite3.Error:
            reason, action = "ledger_write_failed", "hold"
        response_model = result.get("model")
        safe_model = (response_model if isinstance(response_model, str)
                      and response_model.startswith(MODEL) and len(response_model) < 80 else None)
        return Decision(action=action, source="Jev" if reason is None else "local_fallback",
                        reason=reason or "valid_paper_recommendation", confidence=confidence if
                        finite_number(confidence) and 0 <= confidence <= 1 else None,
                        elapsed_ms=elapsed, reported_cost_usd=cost, response_model=safe_model)
