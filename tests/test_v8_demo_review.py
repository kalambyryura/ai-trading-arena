"""Prepared ordinary probe on injected fake exchanges and a fake Jev only."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import unittest
import test_exchange_lab as exchange_fixture
from trading_lab import exchange_engine as engine
from trading_lab.demo_v8_review import review
from trading_lab.match_journal import restore_events


class OrdinaryDemoProbeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = exchange_fixture.ExchangeTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        payload = json.loads((Path(__file__).resolve().parents[1] / "03_For_Client_Deliverables/V8_DEMO_SHORT_PARTIAL_TAIL.json").read_text(encoding="utf-8"))
        identity = self.fixture.workshop.add_version(self.fixture.workshop.data["generation"], payload)["id"]
        self.state = self.fixture.start(ids=[identity])
        self.state.update(fixture_only=True, evidence_kind="SYNTHETIC")
        self.bot = self.state["bots"][0]
        self.fake = self.fixture.fakes["demo"]

    def step(self, seconds):
        self.fixture.now += seconds
        self.fixture.lab.step(self.state)

    def report(self):
        state = deepcopy(self.fixture.lab.load(self.state["id"]))
        restore_events(self.fixture.lab.path / (state["id"] + ".events.jsonl"), state, full=True)
        return review(state)

    def test_full_probe_has_short_partial_resized_stop_tail_and_authored_exit(self):
        self.step(0)
        self.assertLess(self.bot["position_btc"], 0)
        initial = abs(self.fake.units)
        for _ in range(88): self.step(5)
        self.assertEqual(self.state["status"], "DONE")
        report = self.report()
        self.assertTrue(report["all_primary_observed"], report["rows"])
        self.assertEqual(report["evidence_kind"], "SYNTHETIC")
        self.assertIn(len(self.fake.orders), (3, 4))
        reduction = list(self.fake.orders.values())[1]
        self.assertTrue(reduction["reduceOnly"])
        self.assertGreater(Decimal(reduction["accFillSz"]), 0)
        self.assertLess(Decimal(reduction["accFillSz"]), initial)
        self.assertTrue(all(o["reduceOnly"] for o in list(self.fake.orders.values())[1:]))
        self.assertEqual(self.bot["ai_requests"], 1)
        altered = deepcopy(self.state)
        for record in altered["bots"][0].get("_protections", []):
            record["receipt"]["reduceOnly"] = False
        rejected = review(altered)
        self.assertFalse(rejected["all_primary_observed"])
        self.assertEqual(next(r["status"] for r in rejected["rows"] if r["id"] == "resized_hosted_sl"), "not_observed")
        missing = deepcopy(self.state)
        missing["frames"] = []
        self.assertFalse(review(missing)["all_primary_observed"])
        stale = deepcopy(self.state)
        for frame in stale["frames"]:
            row = frame["bots"][0]
            if (row.get("protection") or {}).get("status") == "ARMED":
                row["position_btc"] = -float(initial * Decimal(".01"))
                row["protection"]["covered_contracts"] = str(initial)
        self.assertEqual(next(r["status"] for r in review(stale)["rows"] if r["id"] == "resized_hosted_sl"), "not_observed")

    def test_owner_stop_is_separate_from_authored_exit_and_drain_proof(self):
        self.step(0)
        self.step(5)
        self.fixture.lab.stop(self.fixture.lab.data["generation"], self.state["id"])
        self.step(5)
        report = self.report()
        rows = {r["id"]: r["status"] for r in report["rows"]}
        self.assertEqual(rows["owner_stop_close"], "observed")
        self.assertEqual(rows["authored_full_exit"], "not_observed")
        self.assertFalse(report["all_primary_observed"])

    def test_recovery_close_only_reports_incomplete_without_new_gate_or_entry(self):
        self.step(0)
        self.step(5)
        state = self.state
        engine.stop(state, self.fixture.now, reason="Відновлення після перерви: лише звірка й закриття.")
        state.update(_recovery=True, incomplete=True)
        self.fixture.lab.resources[state["id"]]["apis"][0].close_only = True
        self.step(5)
        report = self.report()
        self.assertEqual(next(r["status"] for r in report["rows"] if r["id"] == "recovery_close_only"), "observed")
        self.assertEqual(self.bot["ai_requests"], 1)
        self.assertEqual(len(self.fake.orders), 2)
        self.assertTrue(state["incomplete"])

    def test_no_fills_never_counts_as_probe_success(self):
        self.step(305)
        report = self.report()
        self.assertFalse(report["all_primary_observed"])
        self.assertEqual(next(r["status"] for r in report["rows"] if r["id"] == "short_fill"), "not_observed")


if __name__ == "__main__": unittest.main()
