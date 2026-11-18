"""Offline-first probe of the live Jev entry gate (owner-approved paid mode is explicit).

Builds the EXACT production gate payload (trading_lab.arena_jev.make_payload) for recorded battle decisions
or for historical candidates, so authors can see how the gate reads their recipe before a battle.

* Default is a DRY RUN: payloads are built and described; no key reader, no network.
* --paid needs --owner-paid (the owner's budget approval), a --ledger inside runtime/ and --cap-usd <= 1.0.
  Every call is reserved in SQLite BEFORE sending; billing that cannot be confirmed blocks all later calls.
* The API key is read in memory by trading_lab.jev.read_key (env OPENROUTER_API_KEY or <key-root>/.env.local)
  and is never printed, logged or stored. Arena ledgers E037/E038/E039 are never opened.
* No exchange, account, order or Start/Stop code is imported. Results are research evidence, not a market test:
  the gate answers a consistency question about the candidate; confidence = 2p - 1 for proceed/hold.

  py -3.12 -X utf8 -B tools/jev_gate_probe.py replay-live --fixture tests/fixtures/jev_gate_live5h.json
  py -3.12 -X utf8 -B tools/jev_gate_probe.py candidates --tape x.snapshots.jsonl --recipe a.json --recipe b.json
  py -3.12 -X utf8 -B tools/jev_gate_probe.py report --results runtime/jev_gate_probe/<run>/results.jsonl
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import statistics
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trading_lab import arena_jev, jev  # noqa: E402
from trading_lab import strategy_runtime as runtime  # noqa: E402
from trading_lab.arena_market import build_snapshot  # noqa: E402
from trading_lab.arena_strategies import evaluate, validate_strategy  # noqa: E402

HARD_CAP_USD = 1.0
DEFAULT_CAP_USD = 0.05
HEADROOM_USD = 0.0005          # in-flight reserve per call; never start a call that could cross the cap
STALE_RESERVATION_SECONDS = 90
MAX_TOTAL_ENTRIES = 20         # exchange_engine LIMITS["max_total_entries"] shown in the gate checks


class ProbeError(ValueError):
    pass


# ------------------------------------------------------------------ ledger and runner
class SpendLedger:
    """Reservation-before-send ledger with a hard cumulative cap (all runs that share the file)."""

    def __init__(self, path, cap_usd, *, root=ROOT):
        path, base = Path(path).resolve(), (Path(root) / "runtime").resolve()
        if not path.is_relative_to(base):
            raise ProbeError("ledger_must_stay_inside_runtime")
        if not 0 < cap_usd <= HARD_CAP_USD:
            raise ProbeError("cap_usd_must_be_in_(0,1]")
        self.path, self.cap = path, float(cap_usd)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock, self.in_flight = threading.Lock(), 0
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, case_id TEXT, "
                       "payload_sha256 TEXT, started REAL, status TEXT, cost REAL, input_tokens INTEGER, "
                       "output_tokens INTEGER, elapsed_ms REAL)")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def totals(self):
        with self._db() as db:
            count, cost, unresolved = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(cost),0), COALESCE(SUM(CASE WHEN status='billing_unknown' OR "
                "(status='reserved' AND started < ?) THEN 1 ELSE 0 END),0) FROM calls",
                (time.time() - STALE_RESERVATION_SECONDS,)).fetchone()
        return {"calls": count, "reported_cost_usd": cost, "unresolved": unresolved, "cap_usd": self.cap}

    def reserve(self, run_id, case_id, digest):
        with self.lock:
            totals = self.totals()
            if totals["unresolved"]:
                return None, "unresolved_billing_blocks_new_calls"
            if totals["reported_cost_usd"] + (self.in_flight + 1) * HEADROOM_USD >= self.cap:
                return None, "spend_cap_reached"
            with self._db() as db:
                row = db.execute("INSERT INTO calls(run_id,case_id,payload_sha256,started,status) VALUES(?,?,?,?,'reserved')",
                                 (run_id, case_id, digest, time.time())).lastrowid
            self.in_flight += 1
            return row, None

    def finish(self, row, status, cost, input_tokens, output_tokens, elapsed_ms):
        with self.lock:
            with self._db() as db:
                db.execute("UPDATE calls SET status=?,cost=?,input_tokens=?,output_tokens=?,elapsed_ms=? WHERE id=?",
                           (status, cost, input_tokens, output_tokens, elapsed_ms, row))
            self.in_flight -= 1


def _finite_cost(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and 0 <= value < 1


def run_cases(cases, ledger, *, transport, key, run_id, results_path, payloads_path, workers=2, rate=2.0, label=""):
    """Send each case once (no retry). A call whose cost is unknown halts the whole run."""
    halt, out_lock, gap, state = threading.Event(), threading.Lock(), 1.0 / rate, {"next": 0.0}
    results_path, payloads_path = Path(results_path), Path(payloads_path)
    results_path.parent.mkdir(parents=True, exist_ok=True)

    def pace():
        with out_lock:
            now = time.monotonic()
            start = max(now, state["next"])
            state["next"] = start + gap
        if start > now:
            time.sleep(start - now)

    def one(case):
        if len(case["body"]) > jev.MAX_BODY_BYTES:
            record = {"case_id": case["case_id"], "run_id": run_id, "status": "payload_too_large", "meta": case.get("meta")}
            with out_lock:
                with open(results_path, "a", encoding="utf-8", newline="\n") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            return record
        if halt.is_set():
            return {"case_id": case["case_id"], "status": "skipped_halted", "meta": case.get("meta")}
        digest = hashlib.sha256(case["body"]).hexdigest()
        row, refusal = ledger.reserve(run_id, case["case_id"], digest)
        if row is None:
            halt.set()
            return {"case_id": case["case_id"], "status": "refused", "reason": refusal, "meta": case.get("meta")}
        pace()
        started, response, failure = time.perf_counter(), None, None
        try:
            response = transport(case["body"], key)
        except Exception as error:  # class only: never provider text, headers or key
            failure = type(error).__name__ + (":" + str(error.code) if hasattr(error, "code") else "")
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        response = response if isinstance(response, dict) else {}
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        cost = usage.get("cost") if _finite_cost(usage.get("cost")) else None
        tokens = [usage.get(k) if isinstance(usage.get(k), int) else None for k in ("input_tokens", "output_tokens")]
        answers = response.get("answers") if isinstance(response.get("answers"), dict) else {}
        if cost is None:
            halt.set()
        ledger.finish(row, "accounted" if cost is not None else "billing_unknown", cost, tokens[0], tokens[1], elapsed)
        record = {"case_id": case["case_id"], "run_id": run_id, "label": label, "payload_sha256": digest,
                  "status": "ok" if cost is not None and answers and failure is None else ("billing_unknown" if cost is None else "no_answers"),
                  "failure": failure, "cost_usd": cost, "input_tokens": tokens[0], "output_tokens": tokens[1],
                  "elapsed_ms": elapsed, "model": response.get("model") if isinstance(response.get("model"), str) else None,
                  "answers": answers, "meta": case.get("meta")}
        with out_lock:
            with open(results_path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            with open(payloads_path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps({"sha256": digest, "payload": json.loads(case["body"])}, ensure_ascii=False, sort_keys=True) + "\n")
        return record

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, list(cases)))


# ------------------------------------------------------------------ candidates
def number(text):
    if isinstance(text, (int, float)):
        return text
    return int(text) if re.fullmatch(r"-?\d+", text) else float(text)


def payload_body(candidates, mode="OKX_LIVE"):
    """Same serialisation as SharedJevGate.decide."""
    return json.dumps(arena_jev.make_payload(candidates, mode), ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode()


def gates(rules, features, entry_attempts=0, entries=0):
    """The three engine gates appended to every candidate's checks (exchange_engine.candidate)."""
    rows = []
    for name, op, expected, actual in (("spread_bps", "lte", rules["max_spread_bps"], features["spread_bps"]),
                                       ("total_entries", "lt", MAX_TOTAL_ENTRIES, entry_attempts),
                                       ("position_entries", "lt", rules["max_entries"], entries)):
        rows.append({"feature": name, "op": op, "expected": expected, "actual": actual,
                     "passed": actual <= expected if op == "lte" else actual < expected})
    return rows


def v1_candidate(recipe, features, bot_id="c", entry_attempts=0, entries=0):
    rules = recipe["rules"]
    passed, checks = evaluate(rules["entry"], features)
    extra = gates(rules, features, entry_attempts, entries)
    return {"id": bot_id, "recipe_sha256": recipe["recipe_sha256"], "features": features, "rules": deepcopy(rules),
            "checks": list(checks) + extra, "side": rules["side"], "kind": "entry", "position_btc": 0,
            "local_checks_passed": bool(passed and all(c["passed"] is True for c in extra))}


def v2_candidate(recipe, snapshot, history=(), bot_id="c", entry_attempts=0):
    """Production V2 bridge on completed-bar snapshots (history first, then the decision snapshot)."""
    bot = {"id": bot_id, "position_btc": 0, "entry_price": 0, "entries": 0, "stop_requested": False,
           "_hold_start": None, "_entry_attempts": entry_attempts, "_manual_review": False}
    state = {"status": "RUNNING", "_end": snapshot["at"] + 86400}
    for past in list(history) + [snapshot]:
        runtime.advance(state, bot, recipe, past, lambda *a, **k: None)
    signal = runtime.entry(recipe, bot, snapshot)
    extra = gates(recipe["rules"], snapshot["features"], entry_attempts)
    return {"id": bot_id, "recipe_sha256": recipe["recipe_sha256"], "features": snapshot["features"],
            "rules": deepcopy(recipe["rules"]), "checks": [dict(c) for c in signal["checks"]] + extra,
            "side": signal["side"] or recipe["rules"].get("side"), "kind": "entry", "position_btc": 0,
            "local_checks_passed": bool(signal["allowed"] and all(c["passed"] is True for c in extra))}


def candidate_for(recipe, snapshot, history=()):
    if recipe.get("strategy_schema_version") == 2:
        return v2_candidate(recipe, snapshot, history)
    return v1_candidate(recipe, snapshot["features"])


def logged_candidate(recipe, decision):
    """Candidate of a recorded battle decision: logged checks, features recomputed by the production function."""
    snapshot = build_snapshot(decision["raw"], decision["snapshot_at"])
    if snapshot["sha256"] != decision["snapshot_sha256"]:
        raise ProbeError("snapshot_hash_mismatch")
    checks = [{"feature": c["feature"], "op": c["op"], "expected": number(c["expected"]), "actual": number(c["actual"]),
               "passed": c["passed"]} for c in decision["logged_checks"]]
    for c in checks:
        if c["feature"] in snapshot["features"] and c["actual"] != snapshot["features"][c["feature"]]:
            raise ProbeError("logged_actual_differs_from_recomputed_feature:" + c["feature"])
    return {"id": decision["bot_id"], "recipe_sha256": recipe["recipe_sha256"], "features": snapshot["features"],
            "rules": recipe["rules"], "checks": checks, "side": recipe["rules"].get("side", "long"),
            "kind": decision["logged_kind"], "position_btc": 0, "local_checks_passed": True}


def load_fixture(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != "jev-gate-live5h-fixture/v1":
        raise ProbeError("unknown_fixture_schema")
    return data


def replay_cases(fixture, mode="OKX_LIVE", repeat=1):
    recipes = {name: validate_strategy(deepcopy(raw)) for name, raw in fixture["recipes"].items()}
    cases = []
    for d in fixture["decisions"]:
        candidate = logged_candidate(recipes[d["author"]], d)
        body = payload_body([candidate], mode)
        for r in range(repeat):
            cases.append({"case_id": "%s:seq%d:r%d" % (d["author"], d["seq"], r), "body": body,
                          "meta": {"group": d["author"], "seq": d["seq"], "recorded": d["recorded"], "bytes": len(body),
                                   "local_checks_passed": candidate["local_checks_passed"]}})
    return cases


def tape_cases(tape, recipes, mode="OKX_LIVE", limit=100):
    """One case per (recipe, completed bar) whose local checks pass, sampled evenly over the tape."""
    rows, last_bar, snaps = [], None, []
    with open(tape, encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            snapshot = build_snapshot(row["raw"], row["at"])
            if snapshot["bar_close_ms"] != last_bar:
                last_bar = snapshot["bar_close_ms"]
                snaps.append(snapshot)
    cases = []
    for label, recipe in recipes:
        found = []
        for i, snapshot in enumerate(snaps):
            candidate = candidate_for(recipe, snapshot, history=snaps[max(0, i - 1):i])
            if candidate["local_checks_passed"]:
                found.append((snapshot, candidate))
        if len(found) > limit:
            found = [found[int(k)] for k in sorted({round(j * (len(found) - 1) / (limit - 1)) for j in range(limit)})]
        for snapshot, candidate in found:
            body = payload_body([candidate], mode)
            cases.append({"case_id": "%s:%d" % (label, snapshot["bar_close_ms"]), "body": body,
                          "meta": {"group": label, "bar_close_ms": snapshot["bar_close_ms"], "bytes": len(body),
                                   "local_checks_passed": True}})
    return cases


# ------------------------------------------------------------------ reporting
def signed_confidence(record, slot="c0"):
    answer = (record.get("answers") or {}).get(slot) or {}
    confidence = answer.get("confidence")
    if not isinstance(confidence, (int, float)):
        return None
    return confidence if answer.get("choice") == "proceed" else -confidence


def summarize(records):
    groups = {}
    for r in records:
        value = signed_confidence(r) if r.get("status") == "ok" else None
        if value is not None:
            groups.setdefault((r.get("meta") or {}).get("group", "-"), []).append(value)
    rows = []
    for name, values in sorted(groups.items()):
        rows.append({"group": name, "n": len(values), "mean_signed_confidence": round(statistics.mean(values), 4),
                     "sd": round(statistics.pstdev(values), 4), "pass_ge_0_70": round(sum(v >= jev.CONFIDENCE_THRESHOLD for v in values) / len(values), 4),
                     "holds": sum(v < 0 for v in values)})
    return rows


def print_table(rows):
    print("%-26s %5s %9s %6s %8s %6s" % ("group", "n", "mean conf", "sd", "pass>=.70", "holds"))
    for r in rows:
        print("%-26s %5d %9.3f %6.3f %8.0f%% %6d" % (r["group"][:26], r["n"], r["mean_signed_confidence"], r["sd"],
                                                    100 * r["pass_ge_0_70"], r["holds"]))


# ------------------------------------------------------------------ CLI
def add_run_options(parser):
    parser.add_argument("--mode", default="OKX_LIVE", choices=["PAPER", "OKX_DEMO", "OKX_LIVE"])
    parser.add_argument("--paid", action="store_true", help="send real requests (needs --owner-paid)")
    parser.add_argument("--owner-paid", action="store_true", help="the owner approved this spend explicitly")
    parser.add_argument("--ledger", help="SQLite ledger inside runtime/ (cumulative spend cap)")
    parser.add_argument("--cap-usd", type=float, default=DEFAULT_CAP_USD, help="cumulative cap of the ledger, <= 1.0")
    parser.add_argument("--key-root", default=str(ROOT), help="folder with .env.local (read by trading_lab.jev.read_key)")
    parser.add_argument("--out", default=str(ROOT / "runtime" / "jev_gate_probe"))
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--runtime-root", default=str(ROOT), help="repository root that owns the runtime/ folder of --ledger")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--rate", type=float, default=2.0, help="requests per second")


def execute(args, cases, *, transport, key_reader):
    print("cases: %d | bytes %d..%d | est cost <= $%.4f" % (len(cases), min(c["meta"]["bytes"] for c in cases) if cases else 0,
                                                              max(c["meta"]["bytes"] for c in cases) if cases else 0, len(cases) * 0.00007))
    for c in cases[:12]:
        print("  %-34s %5d bytes sha %s checks_pass=%s" % (c["case_id"], c["meta"]["bytes"],
                                                          hashlib.sha256(c["body"]).hexdigest()[:12], c["meta"]["local_checks_passed"]))
    if not args.paid:
        print("DRY RUN: no key was read and no request was sent (use --paid --owner-paid --ledger ... to send).")
        return 0
    if not args.owner_paid or not args.ledger:
        print("refused: --paid needs --owner-paid and --ledger", file=sys.stderr)
        return 2
    try:
        ledger = SpendLedger(args.ledger, args.cap_usd, root=args.runtime_root)
    except ProbeError as error:
        print("refused:", error, file=sys.stderr)
        return 2
    run_id = args.run_id or time.strftime("run%Y%m%dT%H%M%S")
    out = Path(args.out) / run_id
    key = (key_reader or jev.read_key)(args.key_root)  # in memory only
    records = run_cases(cases, ledger, transport=transport or jev.send_once, key=key, run_id=run_id,
                        results_path=out / "results.jsonl", payloads_path=out / "payloads.jsonl",
                        workers=args.workers, rate=args.rate, label=run_id)
    problems = sorted({r["status"] for r in records if r.get("status") != "ok"})
    print_table(summarize(records))
    if args.command == "shadow-run":
        from trading_lab.gate_shadow import comparison
        summary = comparison(records)
        (out / "shadow_comparison.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False))
    print(json.dumps({"ledger": ledger.totals(), "problems": problems, "results": str(out / "results.jsonl")}))
    return 1 if problems else 0


def main(argv=None, *, transport=None, key_reader=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    replay = sub.add_parser("replay-live", help="rebuild the gate payloads of a recorded battle (fixture)")
    replay.add_argument("--fixture", required=True)
    replay.add_argument("--repeat", type=int, default=1)
    add_run_options(replay)
    cand = sub.add_parser("candidates", help="gate payloads of recipes on the completed bars of a recorded tape")
    cand.add_argument("--tape", required=True, help="*.snapshots.jsonl recording")
    cand.add_argument("--recipe", action="append", required=True, help="recipe JSON; repeat to compare variants")
    cand.add_argument("--limit", type=int, default=100, help="max cases per recipe")
    add_run_options(cand)
    rep = sub.add_parser("report", help="summarise a results.jsonl")
    rep.add_argument("--results", required=True)
    shadow = sub.add_parser("shadow-run", help="replay exact unsent bytes from a private RULES_ONLY author bundle; default dry run")
    shadow.add_argument("--bundle", required=True)
    shadow.add_argument("--repeat", type=int, default=1)
    shadow.add_argument("--strategies", required=True,  # R2111-GATE: required, and checked against the bundle
                        help="strategies.json of the same battle's ZIP, checked against the bundle; its example participants are skipped")
    add_run_options(shadow)
    args = parser.parse_args(argv)
    try:
        if args.command == "report":
            records = [json.loads(line) for line in open(args.results, encoding="utf-8")]
            print_table(summarize(records))
            if any((r.get("meta") or {}).get("slots") for r in records):
                from trading_lab.gate_shadow import comparison
                print(json.dumps(comparison(records), ensure_ascii=False))
            return 0
        if args.command == "shadow-run":
            from trading_lab.gate_shadow import cases as shadow_cases, examples_in
            bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8"))
            # R2.1.11 (E, R2111-GATE): strategies.json of the same ZIP names each author kind; checked against the bundle
            examples = examples_in(bundle, json.loads(Path(args.strategies).read_text(encoding="utf-8")))
            cases = shadow_cases(bundle, args.repeat, examples)
        elif args.command == "replay-live":
            cases = replay_cases(load_fixture(args.fixture), args.mode, args.repeat)
        else:
            recipes = [(Path(p).stem, validate_strategy(json.loads(Path(p).read_text(encoding="utf-8")))) for p in args.recipe]
            cases = tape_cases(args.tape, recipes, args.mode, args.limit)
        return execute(args, cases, transport=transport, key_reader=key_reader)
    except (ProbeError, OSError, KeyError, ValueError) as error:
        print("probe error:", type(error).__name__, str(error)[:160], file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
