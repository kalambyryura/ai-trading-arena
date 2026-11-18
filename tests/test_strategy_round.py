"""Pinned free comparisons: same tapes, scoped writes, durable failed attempts."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tools import strategy_round as round_tool
from trading_lab.arena_strategies import fingerprint
from trading_lab.strategy_training import code_hash


class PinnedRoundTests(unittest.TestCase):
    def packet(self, directory):
        root = Path(directory) / "common"
        (root / "baselines").mkdir(parents=True)
        recipe = json.loads((Path(__file__).parent / "fixtures/strategy_v2_temporal.json").read_text(encoding="utf-8"))
        recipe["rules"]["max_hold_seconds"] = 900
        baseline = root / "baselines/sol.json"
        baseline.write_text(json.dumps(recipe), encoding="utf-8")
        value = {"schema": "strategy-round-v8-common/v1", "round_id": "fictional-pinned-round",
            "development_only": True, "engine": {"training": code_hash()}, "budget_usdt": 100,
            "authors": {"sol": "fictional-v1"}, "files": {"baselines/sol.json": hashlib.sha256(baseline.read_bytes()).hexdigest()},
            "datasets": [{"id": "synthetic", "kind": "SYNTHETIC", "admission_seconds": 900, "tail_seconds": 120}]}
        value["packet_sha256"] = fingerprint(value)
        (root / "manifest.json").write_text(json.dumps(value), encoding="utf-8")
        return root, recipe, value

    def test_changed_common_file_is_rejected_before_creating_any_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _, _ = self.packet(directory)
            baseline = root / "baselines/sol.json"
            baseline.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "packet_file_hash"):
                round_tool.trial(root, "sol", baseline, "synthetic")
            self.assertFalse((Path(directory) / "sol/trials").exists())

    def test_author_cannot_test_another_authors_unsealed_recipe(self):
        with tempfile.TemporaryDirectory() as directory:
            root, recipe, _ = self.packet(directory)
            other = Path(directory) / "astra/candidate.json"
            other.parent.mkdir()
            other.write_text(json.dumps(recipe), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "recipe_outside_own_scope"):
                round_tool.trial(root, "sol", other, "synthetic")

    def test_validation_failure_remains_in_own_attempt_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _, _ = self.packet(directory)
            invalid = Path(directory) / "sol/invalid.json"
            invalid.parent.mkdir()
            invalid.write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError): round_tool.trial(root, "sol", invalid, "synthetic")
            trials = list((invalid.parent / "trials").glob("*.json"))
            self.assertEqual(len(trials), 1)
            failed = json.loads(trials[0].read_text(encoding="utf-8"))
            self.assertEqual(failed["status"], "FAILED")
            self.assertEqual(failed["ai_cost_usd"], 0)

    def test_changed_max_hold_keeps_identical_input_for_actual_paired_trials(self):
        with tempfile.TemporaryDirectory() as directory:
            root, recipe, value = self.packet(directory)
            alternate = deepcopy(recipe)
            alternate["rules"]["max_hold_seconds"] = None
            candidate = Path(directory) / "sol/candidate.json"
            candidate.parent.mkdir()
            candidate.write_text(json.dumps(alternate), encoding="utf-8")
            left = round_tool.trial(root, "sol", root / "baselines/sol.json", "synthetic")
            right = round_tool.trial(root, "sol", candidate, "synthetic")
            self.assertEqual(left["data_sha256"], right["data_sha256"])
            self.assertNotEqual(left["recipe_sha256"], right["recipe_sha256"])
            left_report = round_tool.read(left["report"])
            right_report = round_tool.read(right["report"])
            self.assertEqual(left_report["data_end_at_ms"], right_report["data_end_at_ms"])
            self.assertTrue(all(duration == 900 for _, _, duration in round_tool.dataset(root, value, "synthetic")[1]))

    def test_manifest_traversal_cannot_read_outside_packet(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _, value = self.packet(directory)
            secret = Path(directory) / "outside.txt"
            secret.write_text("fictional-private", encoding="utf-8")
            value["files"]["../outside.txt"] = hashlib.sha256(secret.read_bytes()).hexdigest()
            value.pop("packet_sha256")
            value["packet_sha256"] = fingerprint(value)
            (root / "manifest.json").write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "outside_scoped_directory"): round_tool.packet(root)


if __name__ == "__main__": unittest.main()
