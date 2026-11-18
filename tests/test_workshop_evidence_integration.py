"""Workshop/SDK all-attempts, immutable V2 diagnostics and paired replay fixtures."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parent
ROOT = PATH.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(PATH))
from test_management_integration import recipe, market
from trading_lab.arena import Arena
from trading_lab.arena_strategies import ArenaError, catalogue_defaults
from trading_lab.strategy_contract import payload_of
from trading_lab.strategy_training import run_trial, SYNTHETIC
from trading_lab.strategy_v2 import ContractError
from trading_lab.workshop import Workshop
from trading_lab.workshop_diagnostics import report, diagnostic_rules
from trading_lab.workshop_tools import compare_trials
from tools import strategy_workshop as sdk


def no_entry_recipe():
    item = recipe(reduce=False)
    item["rules"].update(max_entries=1, max_hold_seconds=30, add_when=None)
    item["rules"]["entry"]["long"]["confirm"]["value"] = 999
    return item


class WorkshopEvidenceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        def forbidden_feed(): raise AssertionError("Workshop diagnostic must not open a feed or provider")
        self.arena = Arena(self.root, feed_factory=forbidden_feed, background=False)
        self.addCleanup(self.arena.close)
        self.workshop = Workshop(self.root, self.arena)

    def add(self, item=None, parent=None):
        return self.workshop.add_version(self.workshop.data["generation"], payload_of(item or recipe()), parent)["id"]

    def test_validation_failures_remain_durable_numbered_attempts_without_retry(self):
        identity = self.add()
        for dataset, budget in (("missing-dataset", 100), (SYNTHETIC["id"], 1)):
            with self.assertRaises(ArenaError):
                self.workshop.train(self.workshop.data["generation"], identity, dataset, budget)
        version = self.workshop.detail(identity)
        self.assertEqual(version["trial_count"], 2)
        self.assertEqual([t["attempt_number"] for t in version["trials"]], [1, 2])
        self.assertTrue(all(t["status"] == "FAILED" for t in version["trials"]))
        self.assertTrue(all(self.workshop.trial(t["id"])["status"] == "FAILED" for t in version["trials"]))
        reloaded = Workshop(self.root, self.arena).detail(identity)
        self.assertEqual(reloaded["trial_ids"], version["trial_ids"])
        self.assertEqual(reloaded["trial_count"], 2)

    def test_pending_attempt_is_saved_before_execution_and_error_keeps_lineage(self):
        parent = self.add()
        child = self.add(parent=parent)
        seen = []
        def interrupted(*args):
            attempt = self.workshop.data["trials"][-1]
            seen.append(deepcopy(self.workshop.trial(attempt["id"])))
            raise OSError("fictional replay I/O interruption")
        with patch("trading_lab.workshop.run_trial", side_effect=interrupted):
            with self.assertRaises(ArenaError):
                self.workshop.train(self.workshop.data["generation"], child, SYNTHETIC["id"], 100)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["status"], "PENDING")
        result = self.workshop.trial(seen[0]["id"])
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["lineage_parent_id"], parent)
        self.assertEqual(result["attempt_number"], 1)

    def test_v2_clone_retains_dsl_schema_rules_and_immutable_parent(self):
        parent_id = self.add(recipe("both"))
        parent = deepcopy(self.workshop.version(parent_id))
        cloned = payload_of(parent["strategy"])
        self.assertEqual(cloned["strategy_schema_version"], 2)
        cloned["rules"]["reduce_pct"] = 50
        child_id = self.workshop.add_version(self.workshop.data["generation"], cloned, parent_id)["id"]
        child = self.workshop.version(child_id)
        self.assertEqual(child["strategy"]["strategy_schema_version"], 2)
        self.assertEqual(child["parent_id"], parent_id)
        self.assertEqual(child["lineage_id"], parent["lineage_id"])
        self.assertNotEqual(child["recipe_sha256"], parent["recipe_sha256"])
        self.assertEqual(child["strategy"]["rules"]["reduce_pct"], 50)
        self.assertEqual(self.workshop.version(parent_id), parent)

    def test_diagnostics_are_read_only_and_report_root_unknown_for_missing_features(self):
        identity = self.add(recipe("both"))
        before = deepcopy(self.workshop.data)
        result = report(self.workshop, {"version_id": identity, "features": {"sma_gap_bps": 15}})
        self.assertFalse(result["execution_started"])
        self.assertTrue(any(row["state"] == "UNKNOWN" for row in result["explain"] if row["path"] == "reduce_when"))
        self.assertEqual(result["lots"]["result"], "UNKNOWN")
        self.assertEqual(self.workshop.data, before)

    def test_v1_diagnostic_projection_never_saves_a_new_v2_recipe(self):
        legacy = deepcopy(catalogue_defaults()[0])
        before = deepcopy(legacy)
        projected = diagnostic_rules(legacy)
        self.assertEqual(set(projected["entry"]), {"long"})
        self.assertIsNone(projected["entry"]["long"]["setup"])
        self.assertEqual(projected["max_hold_seconds"], legacy["rules"]["max_hold_seconds"])
        self.assertEqual(legacy, before)

    def test_diagnostic_lot_veto_returns_required_minimum_without_increasing_size(self):
        identity = self.add()
        instrument = {"tick_size": .1, "lot_step": .01, "contract_value": .01,
            "contract_multiplier": 1, "minimum_order_units": 1}
        result = report(self.workshop, {"version_id": identity, "snapshot": market(60),
            "instrument": instrument, "budget_usdt": 25, "equity_usdt": 25})
        self.assertEqual(result["lots"]["result"], "UNEXECUTABLE")
        self.assertIn("minimum_order_units", result["lots"]["reasons"])
        self.assertGreater(result["lots"]["minimum_allocation_usdt_at_size"], 25)
        self.assertFalse(result["execution_started"])

    def invoke(self, args):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()): return sdk.main(args)

    def test_sdk_failed_parse_is_logged_once_before_recipe_validation(self):
        invalid = self.root / "invalid.json"
        invalid.write_text("{invalid", encoding="utf-8")
        log = self.root / "history"
        self.assertEqual(self.invoke(["test", str(invalid), "--log-root", str(log)]), 1)
        rows = [json.loads(path.read_text(encoding="utf-8")) for path in log.glob("*.json")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "FAILED")
        self.assertEqual(rows[0]["runtime"], "RULES_ONLY")
        self.assertEqual(rows[0]["ai_cost_usd"], 0)

    def test_sdk_default_synthetic_test_completes_and_has_one_durable_receipt(self):
        path = self.root / "recipe.json"
        path.write_text(json.dumps(payload_of(no_entry_recipe())), encoding="utf-8")
        log, output = self.root / "history", self.root / "result.json"
        with patch("trading_lab.strategy_training.code_hash", return_value="e" * 64):
            result = self.invoke(["test", str(path), "--output", str(output), "--log-root", str(log)])
        self.assertEqual(result, 0)
        saved = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(saved["dataset_kind"], "SYNTHETIC")
        self.assertEqual(saved["status"], "COMPLETE")
        rows = [json.loads(path.read_text(encoding="utf-8")) for path in log.glob("*.json")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], saved["id"])
        self.assertEqual(rows[0]["status"], "COMPLETE")

    def paired_reports(self):
        tape = [market(at) for at in range(60, 1261, 60)]
        source = {"id": "fictional-tape", "name": "Fictional tape", "kind": "RECORDED_FIXTURE"}
        with patch("trading_lab.strategy_training.code_hash", return_value="e" * 64):
            left = run_trial(no_entry_recipe(), source, 100, [("same tape", tape, 300)])
            right = run_trial(no_entry_recipe(), source, 100, [("same tape", tape, 900)])
        self.assertEqual(left["status"], "COMPLETE")
        self.assertEqual(right["status"], "COMPLETE")
        return left, right

    def test_paired_comparison_rejects_different_admission_windows_on_same_tape(self):
        left, right = self.paired_reports()
        with self.assertRaises(ContractError): compare_trials(left, right)

    def test_paired_comparison_rejects_different_evidence_allocation_code_and_mode(self):
        left, _ = self.paired_reports()
        for field, value in (("data_sha256", "f" * 64), ("dataset_kind", "RECORDED_PUBLIC"),
            ("budget_usdt", 200), ("code_sha256", "f" * 64), ("runtime", "OKX_DEMO_SHARED_JEV"), ("status", "FAILED")):
            with self.subTest(field=field):
                right = deepcopy(left)
                right[field] = value
                with self.assertRaises(ContractError): compare_trials(left, right)

    def test_exact_same_tape_free_pair_reports_metrics_without_claiming_holdout(self):
        left, _ = self.paired_reports()
        right = deepcopy(left)
        right["strategy_sha256"] = "f" * 64
        right["runs"][0]["metrics"]["pnl_usdt"] += .25
        result = compare_trials(left, right)
        self.assertEqual(result["paired_runs"][0]["delta_right_minus_left"]["pnl_usdt"], .25)
        self.assertIn("not a new holdout", result["interpretation"])


if __name__ == "__main__": unittest.main()
