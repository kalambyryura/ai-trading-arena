"""Offline tests of tools/jev_gate_probe.py: payload fidelity on a recorded battle, ledger, cap and billing rules."""
from copy import deepcopy
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import jev_gate_probe as probe
from trading_lab import jev
from trading_lab.arena_market import build_snapshot
from trading_lab.arena_strategies import validate_strategy

FIXTURE = Path(__file__).parent / "fixtures" / "jev_gate_live5h.json"
# Payload bytes of the 8 live decisions of battle 8d53b2a8... as built by the production gate at d6089e8.
# A mismatch means the gate payload format changed: review probes, fixtures and authors' guidance first.
GOLDEN = {
    "sonnet:seq53": "82b3111ffd52289dc99dc016c4e535b5ae844bb829b14b4a8faa52184da7a127",
    "sonnet:seq61": "3a50f9f66cd56864592d4a0e7b4a6a7e04ab679e905944a9f341d5fbb485ba46",
    "sonnet:seq69": "ec63e0d7958c6097a41355ba4a55926ccd6f062412ab0e78493885c7365daf5e",
    "sonnet:seq77": "989ef14323b59abd73fb0d98f01fab9275bed582fda915444104a2192b6f67d8",
    "sonnet:seq81": "73b5b4deac1a91c302c2588d8e51230a79c933af5c483c233030cc9e3eb45379",
    "astra:seq1901": "2fafd2444701b03c3b749fe1c8ce9043a134ba636b607c0cec93f9953d4c8425",
    "astra:seq2222": "69ea55ab863381201412495b6a8ed6c57393d5c255e2287a770d559cca4cc8d8",
    "astra:seq3250": "ae5964c03c76be3379779ba7945e7ec4c95987e25d20bfd83cd55b718c5747c2",
}
FAKE_KEY = "fake-key-must-never-appear"


def v2_of(raw):
    """Same logic as a declarative V2 recipe (direct confirm, no setup)."""
    raw = deepcopy(raw)
    rules = raw["rules"]
    leaves = [{"feature": c["feature"], "op": c["op"], "value": c["value"]} for c in rules["entry"]["conditions"]]
    exits = rules["exit"]["conditions"]
    rules["entry"] = {rules["side"]: {"setup": None, "confirm": leaves[0] if len(leaves) == 1 else {"all": leaves},
                                      "invalidate": None, "expiry_bars": None}}
    rules["exit"] = None if not exits else (exits[0] if len(exits) == 1 else {"any": exits})
    rules.update(add_when=None, cost_checkpoint=None, reduce_when=None, reduce_pct=None)
    raw["strategy_schema_version"] = 2
    return raw


def answering(cost=0.00005, confidence=0.8):
    def transport(body, key):
        data = json.loads(body)
        return {"answers": {k: {"type": "choice", "choice": "proceed", "confidence": confidence,
                                "probabilities": {"proceed": 0.9, "hold": 0.1}} for k in data["questions"]},
                "usage": {"cost": cost, "input_tokens": 1200, "output_tokens": 1}, "model": "typesafe/jev-1.13-test"}
    return transport


def run_cli(argv, **kwargs):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = probe.main(argv, **kwargs)
    return code, out.getvalue()


class FixtureFidelityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = probe.load_fixture(FIXTURE)
        cls.cases = probe.replay_cases(cls.fixture)

    def test_payload_bytes_are_pinned_to_the_recorded_battle(self):
        got = {c["case_id"].rsplit(":r", 1)[0]: hashlib.sha256(c["body"]).hexdigest() for c in self.cases}
        self.assertEqual(got, GOLDEN)

    def test_engine_style_v1_checks_equal_the_logged_checks(self):
        recipes = {n: validate_strategy(deepcopy(r)) for n, r in self.fixture["recipes"].items()}
        for d in self.fixture["decisions"]:
            snapshot = build_snapshot(d["raw"], d["snapshot_at"])
            logged = d["logged_checks"]
            attempts = int(next(c["actual"] for c in logged if c["feature"] == "total_entries"))  # earlier entries of that bot
            built = probe.v1_candidate(recipes[d["author"]], snapshot["features"], entry_attempts=attempts)
            self.assertEqual(len(built["checks"]), len(logged), d["seq"])
            for mine, theirs in zip(built["checks"], logged):
                self.assertEqual((mine["feature"], mine["op"], mine["passed"]), (theirs["feature"], theirs["op"], theirs["passed"]))
                self.assertAlmostEqual(float(mine["actual"]), float(theirs["actual"]), places=9)
                self.assertAlmostEqual(float(mine["expected"]), float(theirs["expected"]), places=9)
            self.assertTrue(built["local_checks_passed"])

    def test_v2_translation_passes_local_checks_and_declares_the_v2_syntax(self):
        recipe = validate_strategy(v2_of(self.fixture["recipes"]["sonnet"]))
        d = next(x for x in self.fixture["decisions"] if x["author"] == "sonnet")
        snapshot = build_snapshot(d["raw"], d["snapshot_at"])
        candidate = probe.candidate_for(recipe, snapshot)
        self.assertTrue(candidate["local_checks_passed"])
        body = json.loads(probe.payload_body([candidate]))
        self.assertIn("v2_rule_format", body["state"])

    def test_tape_cases_find_the_signal_bars_of_a_recorded_tape(self):
        with tempfile.TemporaryDirectory() as tmp:
            tape = Path(tmp) / "x.snapshots.jsonl"
            with open(tape, "w", encoding="utf-8") as handle:
                for d in self.fixture["decisions"]:
                    handle.write(json.dumps({"at": d["snapshot_at"], "raw": d["raw"], "sha256": d["snapshot_sha256"]}) + "\n")
            recipe = validate_strategy(deepcopy(self.fixture["recipes"]["sonnet"]))
            cases = probe.tape_cases(tape, [("sonnet_v1", recipe)], limit=10)
        self.assertGreaterEqual(len(cases), 1)
        self.assertTrue(all(c["meta"]["local_checks_passed"] for c in cases))


class DryRunTests(unittest.TestCase):
    def test_dry_run_never_reads_the_key_or_sends(self):
        with patch.object(jev, "read_key", side_effect=AssertionError("key read")), \
                patch.object(jev, "send_once", side_effect=AssertionError("network")):
            code, out = run_cli(["replay-live", "--fixture", str(FIXTURE)])
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", out)

    def test_paid_mode_needs_owner_flag_runtime_ledger_and_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = ["replay-live", "--fixture", str(FIXTURE), "--runtime-root", str(root), "--paid"]
            code, _ = run_cli(base)
            self.assertEqual(code, 2)  # no --owner-paid / --ledger
            code, _ = run_cli(base + ["--owner-paid", "--ledger", str(root / "elsewhere.sqlite3")])
            self.assertEqual(code, 2)  # ledger outside runtime/
            code, _ = run_cli(base + ["--owner-paid", "--ledger", str(root / "runtime" / "l.sqlite3"), "--cap-usd", "2"])
            self.assertEqual(code, 2)  # cap above the $1 hard limit
            self.assertFalse((root / "runtime" / "l.sqlite3").exists())


class PaidModeFakeTransportTests(unittest.TestCase):
    def args(self, root, *extra):
        return ["replay-live", "--fixture", str(FIXTURE), "--runtime-root", str(root), "--paid", "--owner-paid",
                "--ledger", str(root / "runtime" / "ledger.sqlite3"), "--out", str(root / "runtime" / "out"),
                "--rate", "1000", "--workers", "1", "--run-id", "t1", *extra]

    def test_every_call_is_accounted_and_the_key_never_leaks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code, out = run_cli(self.args(root, "--repeat", "2"), transport=answering(), key_reader=lambda r: FAKE_KEY)
            self.assertEqual(code, 0, out)
            ledger = probe.SpendLedger(root / "runtime" / "ledger.sqlite3", 0.5, root=root)
            totals = ledger.totals()
            self.assertEqual((totals["calls"], totals["unresolved"]), (16, 0))
            self.assertAlmostEqual(totals["reported_cost_usd"], 16 * 0.00005, places=9)
            blobs = [out] + [p.read_text(encoding="utf-8") for p in (root / "runtime" / "out" / "t1").iterdir()]
            self.assertTrue(all(FAKE_KEY not in b for b in blobs))
            with open(root / "runtime" / "out" / "t1" / "results.jsonl", encoding="utf-8") as handle:
                rows = [json.loads(line) for line in handle]
            self.assertEqual(len(rows), 16)
            self.assertTrue(all(r["status"] == "ok" and r["answers"]["c0"]["confidence"] == 0.8 for r in rows))

    def test_unknown_billing_halts_the_run_and_blocks_later_runs(self):
        def no_usage(body, key):
            return {"answers": {"c0": {"type": "choice", "choice": "proceed", "confidence": 0.9}}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code, _ = run_cli(self.args(root), transport=no_usage, key_reader=lambda r: FAKE_KEY)
            self.assertEqual(code, 1)
            totals = probe.SpendLedger(root / "runtime" / "ledger.sqlite3", 0.5, root=root).totals()
            self.assertEqual((totals["calls"], totals["unresolved"]), (1, 1))
            calls = []
            code, _ = run_cli(self.args(root, "--run-id", "t2"), transport=lambda b, k: calls.append(1) or answering()(b, k),
                              key_reader=lambda r: FAKE_KEY)
            self.assertEqual((code, calls), (1, []))

    def test_cap_stops_before_it_could_be_crossed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code, _ = run_cli(self.args(root, "--cap-usd", "0.002", "--repeat", "5"), transport=answering(cost=0.0001),
                              key_reader=lambda r: FAKE_KEY)
            self.assertEqual(code, 1)  # some cases refused
            totals = probe.SpendLedger(root / "runtime" / "ledger.sqlite3", 0.002, root=root).totals()
            self.assertLessEqual(totals["reported_cost_usd"], 0.002)
            self.assertGreater(totals["calls"], 0)
            self.assertLess(totals["calls"], 40)

    def test_report_summarises_a_results_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_cli(self.args(root), transport=answering(), key_reader=lambda r: FAKE_KEY)
            code, out = run_cli(["report", "--results", str(root / "runtime" / "out" / "t1" / "results.jsonl")])
        self.assertEqual(code, 0)
        self.assertIn("sonnet", out)
        self.assertIn("astra", out)


if __name__ == "__main__":
    unittest.main()
