"""tools/requalify_frozen.py on a throwaway workshop: the receipts import as staged_qualification expects, under the ids of the
importing machine; a candidate that cannot be imported leaves the V1 group applied. Temp roots only: no network, model or order."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import Mock, patch
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import requalify_frozen as tool
import test_exchange_lab as fixture
from test_exchange_eth import eth_payload
from trading_lab import staged_qualification, strategy_training
from trading_lab.arena_strategies import catalogue_defaults
from trading_lab.strategy_contract import payload_of


class Requalify(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.ExchangeTests(methodName="runTest")
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        shop = self.shop = self.fx.workshop
        self.v1 = self.frozen(payload_of(catalogue_defaults()[1]))
        self.eth = self.frozen(eth_payload())
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        (self.dir / "v1.json").write_text(json.dumps(payload_of(self.v1["strategy"])), encoding="utf-8")
        (self.dir / "eth.json").write_text(json.dumps(eth_payload()), encoding="utf-8")
        self.groups = [{"name": "1-v1", "items": [self.item("v1", self.v1, "v1.json")]}, {"name": "2-h10", "items": [self.item("h10", self.eth, "eth.json")]}]
        self.exchange = Mock()
        self.exchange.status.return_value = {"active": {}}
        self.assertIsNotNone(shop)

    def frozen(self, payload):
        shop = self.shop
        version = shop.add_version(shop.data["generation"], payload)
        shop.train(shop.data["generation"], version["id"], strategy_training.SYNTHETIC["id"], 100)
        shop.freeze(shop.data["generation"], version["id"])
        return shop.version(version["id"])

    @staticmethod
    def item(name, version, payload):
        return {"name": name, "version_id": version["id"], "recipe_sha256": version["recipe_sha256"], "payload": payload}

    def pending(self):
        return self.shop.path / "qualification_pending"

    def baseline_of_another_code(self):
        out = self.dir / "baseline"
        tool.baseline(self.groups, self.dir, out)
        for path in out.glob("*.json"):
            row = json.loads(path.read_text(encoding="utf-8"))
            row["code_sha256"] = "0" * 64
            path.write_text(json.dumps(row), encoding="utf-8")
        return out

    def test_the_receipts_import_once_under_the_importing_machines_ids(self):
        baseline = self.baseline_of_another_code()
        result = tool.requalify(self.groups, self.dir, self.pending(), "r2113", baseline)
        names = sorted(result["receipts"])
        self.assertEqual(len(names), 2)
        self.assertTrue(names[0].startswith(result["code_sha256"] + ".1-v1."))  # the V1 group first, by name
        self.assertTrue(all(r["exact_prior_metrics_curves"] for r in result["qualifications"]))
        self.assertEqual(staged_qualification.apply(self.shop, self.exchange), {"status": "APPLIED", "imported": 2})
        self.assertEqual(staged_qualification.apply(self.shop, self.exchange), {"status": "UNCHANGED", "imported": 0})
        for version in (self.v1, self.eth):
            self.assertEqual(self.shop.version(version["id"])["qualification_code_sha256"], result["code_sha256"])
        receipts = [json.loads((self.pending() / n).read_text(encoding="utf-8")) for n in names]
        self.assertTrue(all(r["schema"] == "free-qualification/v1" and r["code_sha256"] == result["code_sha256"] for r in receipts))

    def test_a_candidate_that_cannot_be_imported_leaves_the_v1_group_applied(self):
        baseline = self.baseline_of_another_code()
        self.groups[1]["items"][0]["version_id"] = "f" * 32  # not a version of this workshop
        tool.requalify(self.groups, self.dir, self.pending(), "r2113", baseline)
        self.assertEqual(staged_qualification.apply(self.shop, self.exchange)["status"], "BLOCKED")
        self.assertEqual(self.shop.version(self.v1["id"])["qualification_code_sha256"], strategy_training.code_hash())
        self.assertEqual(len(self.shop.version(self.v1["id"])["qualification_history"]), 1)

    def test_a_drift_from_the_baseline_stops_everything(self):
        baseline = self.baseline_of_another_code()
        path = baseline / "h10.json"
        row = json.loads(path.read_text(encoding="utf-8"))
        row["runs"][0]["curve"] = list(reversed(row["runs"][0]["curve"]))
        row["runs"][0]["metrics"] = {**row["runs"][0]["metrics"], "drift": 1}
        path.write_text(json.dumps(row), encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "differ from the baseline"):
            tool.requalify(self.groups, self.dir, self.pending(), "r2113", baseline)

    def test_a_baseline_of_this_same_code_proves_nothing(self):
        out = self.dir / "same"
        tool.baseline(self.groups, self.dir, out)
        with self.assertRaisesRegex(SystemExit, "same code"):
            tool.requalify(self.groups, self.dir, self.pending(), "r2113", out)

    def test_the_wrong_recipe_is_refused_before_any_trial(self):
        self.groups[0]["items"][0]["recipe_sha256"] = "0" * 64
        with patch.object(strategy_training, "run_trial", side_effect=AssertionError("no trial")), self.assertRaisesRegex(SystemExit, "not the one the plan expects"):
            tool.baseline(self.groups, self.dir, self.dir / "b")

    def test_the_plan_is_checked(self):
        for bad in ({"groups": []}, {"groups": [{"name": "1", "items": []}]},
                    {"groups": [{"name": "1", "items": [{"name": "a", "version_id": "x", "recipe_sha256": "0" * 64, "payload": "p"}]}]},
                    {"groups": [{"name": "1", "items": [{"name": "a", "version_id": "a" * 32, "recipe_sha256": "0" * 64, "payload": "p", "manifest": "m"}]}]}):
            path = self.dir / "plan.json"
            path.write_text(json.dumps(deepcopy(bad)), encoding="utf-8")
            with self.assertRaises(SystemExit):
                tool.load_plan(path)


if __name__ == "__main__":
    unittest.main()
