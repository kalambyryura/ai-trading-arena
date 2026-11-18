"""Optional common Jev veto gate for PAPER candidates; never an exchange executor."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from .jev import MODEL, CONFIDENCE_THRESHOLD, MAX_BODY_BYTES, read_key, send_once
from .risk import finite_number

BUDGET = {"season_attempts": 30, "total_attempts": 200, "season_soft_usd": .005,
          "total_soft_usd": .05, "maximum_answer_age_seconds": 8}


class ArenaJevLedger:
    """Separate E037 series. Existing smoke/live ledgers are neither read nor reset."""
    def __init__(self, root, *, relative_path=None, budget=None):
        self.budget = BUDGET if budget is None else dict(budget)
        self.path = Path(root) / (relative_path or "runtime/arena_v4/shared_jev_e037.sqlite3")
        if not self.path.resolve().is_relative_to((Path(root) / "runtime").resolve()):
            raise ValueError("Ledger must stay inside runtime")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, season TEXT NOT NULL, "
                       "started REAL NOT NULL, payload_sha256 TEXT NOT NULL, cost REAL, status TEXT NOT NULL)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def reserve(self, season, payload_hash, now):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT season,cost,status FROM requests").fetchall()
            own = [r for r in rows if r[0] == season]
            headroom = self.budget.get("request_headroom_usd", 0)
            if (len(rows) >= self.budget["total_attempts"] or len(own) >= self.budget["season_attempts"]
                    or any(r[2] != "accounted" or r[1] is None for r in rows)
                    or sum(r[1] for r in rows) + headroom >= self.budget["total_soft_usd"]
                    or sum(r[1] for r in own) + headroom >= self.budget["season_soft_usd"]):
                return None
            identity = uuid.uuid4().hex
            db.execute("INSERT INTO requests VALUES(?,?,?,?,NULL,'reserved')", (identity, season, now, payload_hash))
            return identity

    def finish(self, identity, cost):
        with self.connect() as db:
            db.execute("UPDATE requests SET cost=?,status=? WHERE id=?",
                       (cost, "accounted" if cost is not None else "billing_unknown", identity))

    def summary(self, season):
        with self.connect() as db:
            rows = db.execute("SELECT season,cost,status FROM requests").fetchall()
        own = [r for r in rows if r[0] == season]
        return {"attempts": len(own), "reported_cost_usd": sum(r[1] or 0 for r in own),
                "unknown_billing": sum(r[2] != "accounted" for r in own),
                "series_attempts": len(rows), "series_reported_cost_usd": sum(r[1] or 0 for r in rows),
                "series_unknown_billing": sum(r[2] != "accounted" for r in rows), "limits": dict(self.budget)}


# The gate shows Jev the protocol-8 market catalogue only. Context features added later (clock, funding)
# stay with the recipe's own checks, so legacy V1 payloads remain byte-identical across code versions.
GATE_FEATURES = frozenset({"sma_gap_bps", "return_5m_bps", "return_1m_bps", "rsi_14", "range_position_20",
                           "volume_ratio_5", "atr_14_bps", "body_bps", "upper_wick_bps", "lower_wick_bps",
                           "bollinger_z_20", "ema_gap_bps", "volume_ratio_1", "spread_bps"})


def make_payload(candidates, mode="PAPER"):
    # Only validated numeric rules, facts and hashes; author prose cannot inject new instructions.
    features = candidates[0]["features"]
    if any(c["features"] != features for c in candidates):
        raise ValueError("Candidates must share one market observation")
    if mode not in {"PAPER", "OKX_DEMO", "OKX_LIVE"}:
        raise ValueError("Unknown execution environment")
    state = {"mode": mode, "features": {k: v for k, v in features.items() if k in GATE_FEATURES},
             "check_columns": ["feature", "op", "expected", "actual", "passed"],
             "group_format": "[all|any, [[feature,op,threshold],...]]",
             "policy": "These are locally permitted entries/adds in the labeled execution environment. Numeric checks were computed by code. "
             "Confirm only an internally consistent, fully checked candidate under its rules and shared features. "
             "Do not forecast prices, reinterpret thresholds, or change side, size, stops or rules. "
             "If uncertain choose hold. Entry/exit groups use group_format; checks use check_columns."}
    # V2 keeps its nested predicates and side branches. Describe that syntax
    # explicitly instead of labelling it as the compact V1 all/any format.
    def schema(rules):
        entry = rules.get("entry")
        if isinstance(entry, dict) and ("when" in entry or any(isinstance(b, dict) and "when" in b for b in entry.values())):
            return 4
        return 2 if isinstance(entry, dict) and entry and set(entry) <= {"long", "short"} else 1
    schemas = [schema(row["rules"]) for row in candidates]
    if 4 in schemas:
        state["v4_rule_format"] = ("Schema 4: entry {when: predicate} or {long: {when}, short: {when}}; a leaf feature is "
                                   "{name, tf, n} on closed bars; numeric checks were computed by code.")
    if 2 in schemas:
        state["check_columns"] += ["path", "side", "rhs_feature"]
        state["v2_rule_format"] = {
            "predicate": "{feature,op,value} or {feature,op,ref}; nested {all:[...]}, {any:[...]}, {not:predicate}.",
            "entry": "Side branches {long|short:{setup,confirm,invalidate,expiry_bars}}. Null setup is direct confirm; temporal setup needs prior-bar evidence.",
            "checks": "path locates a predicate leaf; side selects its branch; rhs_feature names a referenced value. Leaves are evidence, not all(checks): any/not may contain false leaves.",
        }
        state["policy"] = (
            "These are locally permitted entries/adds in the labeled execution environment. Numeric checks were computed by code. "
            "Confirm only an internally consistent, fully checked current candidate under its rules and shared features. "
            "Use rule_schema_version: V1 entry/exit use group_format; V2 uses v2_rule_format. Review the proposed kind and side. "
            "Future exit/reduce_when/cost_checkpoint predicates need not be true at entry. Add predicates apply only to kind add. "
            "Do not forecast prices, reinterpret thresholds, or change side, size, stops or rules. "
            "Do not invent missing temporal setup evidence. If uncertain choose hold. Checks use check_columns."
        )
    questions = {}
    for index, row in enumerate(candidates):
        key = "c" + str(index)
        state[key] = {k: row[k] for k in ("recipe_sha256", "side", "kind", "position_btc", "local_checks_passed")}
        if 2 in schemas:
            state[key]["rule_schema_version"] = schemas[index]
        state[key]["checks"] = [[c.get(k) for k in state["check_columns"]] for c in row["checks"]]
        if row.get("test_plan"):
            from .cycle_probe import PLAN, TIMING
            if mode != "OKX_DEMO" or row["test_plan"] != PLAN or row.get("plan") != TIMING:
                raise ValueError("Diagnostic plan is Demo only")
            state[key].update(test_plan=PLAN, plan=dict(TIMING),
                diagnostic_policy="Execution acceptance test, no profitability claim. The fixed plan replaces add_after_bps with position age60s. "
                "kind hold is one consistency review of an existing position and never creates an order. "
                "proceed confirms that review; hold remains safe holding. Stop/risk/time exits are independent of this answer.")
        elif row["kind"] not in {"entry", "add"}:
            raise ValueError("Unsupported action")
        rules = dict(row["rules"])
        for name in ("entry", "exit"):
            group = rules[name]
            if isinstance(group, dict) and "conditions" in group:
                rules[name] = [group["mode"], [[c["feature"], c["op"], c["value"]] for c in group["conditions"]]]
        state[key]["rules"] = rules
        questions[key] = {"type": "choice", "instructions": "Apply `policy` to candidate `" + key + "` only, using `features` and `check_columns`.",
                         "criteria": {"proceed": "Passed checks; consistent candidate.", "hold": "Uncertain or inconsistent candidate."}}
    return {"model": MODEL, "state": state, "questions": questions}


class SharedJevGate:
    def __init__(self, root, transport=send_once, key_reader=read_key, clock=time.time, timer=time.perf_counter,
                 *, ledger=None, mode="PAPER"):
        self.root, self.transport, self.key_reader, self.clock, self.timer = root, transport, key_reader, clock, timer
        self.ledger = ledger if ledger is not None else ArenaJevLedger(root)
        self.mode = mode
        self.before_send = None

    def decide(self, season, candidates):
        outcome = {"allowed": [], "reason": "local_hold", "attempted": False, "reported_cost_usd": None,
                   "answers": {}, "elapsed_ms": 0, "model": MODEL}
        try:
            if not 1 <= len(candidates) <= 6:
                raise ValueError()
            body = json.dumps(make_payload(candidates, self.mode), ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
            if len(body) > MAX_BODY_BYTES:
                raise ValueError()
            key = self.key_reader(self.root)
            reservation = self.ledger.reserve(season, hashlib.sha256(body).hexdigest(), self.clock())
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            outcome["reason"] = "key_payload_or_ledger_unavailable"
            return outcome
        if reservation is None:
            outcome["reason"] = "budget_or_unknown_billing"
            return outcome
        outcome.update(payload_sha256=hashlib.sha256(body).hexdigest(), request_at_ms=int(self.clock() * 1000))
        if self.before_send:
            try:
                self.before_send(json.loads(body), outcome["payload_sha256"], outcome["request_at_ms"], candidates)
            except Exception:
                self.ledger.finish(reservation, 0)
                outcome["reason"] = "request_evidence_unavailable"
                return outcome
        outcome["attempted"] = True
        start = self.timer()
        try:
            response = self.transport(body, key)
        except Exception:
            response = None  # No HTTP exception text, provider bodies or secrets in journals.
        elapsed = (self.timer() - start) * 1000
        outcome["elapsed_ms"] = round(elapsed, 3)
        response = response if isinstance(response, dict) else {}
        usage = response.get("usage")
        cost = usage.get("cost") if isinstance(usage, dict) else None
        cost = cost if finite_number(cost) and cost >= 0 else None
        outcome["reported_cost_usd"] = cost
        try:
            self.ledger.finish(reservation, cost)
        except sqlite3.Error:
            outcome["reason"] = "ledger_write_failed"
            return outcome
        if cost is None:
            outcome["reason"] = "billing_unknown"
            return outcome
        if elapsed > self.ledger.budget["maximum_answer_age_seconds"] * 1000:
            outcome["reason"] = "answer_expired"
            return outcome
        answers = response.get("answers")
        # One malformed batch invalidates the batch; no partial interpretation of provider schema.
        expected = {"c" + str(i) for i in range(len(candidates))}
        if not isinstance(answers, dict) or set(answers) != expected:
            outcome["reason"] = "invalid_answers"
            return outcome
        for index, candidate in enumerate(candidates):
            answer = answers["c" + str(index)]
            if not isinstance(answer, dict) or answer.get("type") != "choice":
                outcome["reason"] = "invalid_answers"
                return outcome
            confidence, choice = answer.get("confidence"), answer.get("choice")
            if choice not in ("proceed", "hold") or not finite_number(confidence) or not 0 <= confidence <= 1:
                outcome["reason"] = "invalid_answers"
                return outcome
        for index, candidate in enumerate(candidates):
            answer = answers["c" + str(index)]
            outcome["answers"][candidate["id"]] = {"choice": answer["choice"], "confidence": answer["confidence"]}
            if answer["choice"] == "proceed" and answer["confidence"] >= CONFIDENCE_THRESHOLD:
                outcome["allowed"].append(candidate["id"])
        outcome["reason"] = "typed_answer"
        return outcome
