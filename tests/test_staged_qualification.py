from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from trading_lab.arena import Arena
from trading_lab.strategy_contract import payload_of
from trading_lab.strategy_training import SYNTHETIC, code_hash, run_trial
from trading_lab.staged_qualification import apply
from trading_lab.workshop import Workshop
from tests.test_workshop import recipe


class IdleExchange:
    def status(self): return {"active": {"arena_live": None, "workshop_demo": None}}


class QualificationTests(unittest.TestCase):
    def prepare(self, root):
        arena = Arena(root, background=False, feed_factory=lambda: self.fail("No feed"))
        workshop = Workshop(root, arena)
        identity = workshop.add_version(0, payload_of(recipe()))["id"]
        workshop.train(workshop.data["generation"], identity, SYNTHETIC["id"], 100)
        workshop.freeze(workshop.data["generation"], identity)
        result = run_trial(workshop.version(identity)["strategy"])
        result["version_id"] = identity
        folder = workshop.path / "qualification_pending"; folder.mkdir()
        raw = json.dumps(result, ensure_ascii=False).encode()
        (folder / (result["id"]+".report.json")).write_bytes(raw)
        receipt = {"schema":"free-qualification/v1", "code_sha256": code_hash(), "items": [{
            "version_id":identity,"trial_id":result["id"],"report_sha256":hashlib.sha256(raw).hexdigest()}]}
        file = folder / (code_hash()+".fixture.receipt.json")
        file.write_text(json.dumps(receipt),encoding="utf-8")
        return arena, workshop, identity, result, file

    def test_current_free_report_import_is_idempotent_preserves_frozen_identity_and_makes_no_transports(self):
        with tempfile.TemporaryDirectory() as root:
            arena,w,identity,result,_ = self.prepare(root)
            try:
                before = deepcopy(w.version(identity))
                with patch("urllib.request.urlopen",side_effect=AssertionError("No API")):
                    self.assertEqual(apply(w,IdleExchange())["imported"],1)
                    self.assertEqual(apply(w,IdleExchange())["imported"],0)
                after = w.version(identity)
                self.assertEqual(after["strategy"], before["strategy"])
                self.assertEqual(after["frozen_at"], before["frozen_at"])
                self.assertEqual(after["qualification_trial_id"],result["id"])
                self.assertEqual(len(after["qualification_history"]),1)
                self.assertIsNone(arena.session)
            finally: arena.close()

    def test_tampered_report_or_unsupported_cost_never_imports(self):
        with tempfile.TemporaryDirectory() as root:
            arena,w,identity,result,_ = self.prepare(root)
            try:
                before=deepcopy(w.data)
                path=w.path/"qualification_pending"/(result["id"]+".report.json")
                path.write_text('{}',encoding="utf-8")
                self.assertEqual(apply(w,IdleExchange())["status"],"BLOCKED")
                self.assertEqual(w.data,before)
            finally: arena.close()

    def test_unfinished_run_defers_without_changing_workshop(self):
        with tempfile.TemporaryDirectory() as root:
            arena,w,_,_,_=self.prepare(root)
            class Busy:
                def status(self): return {"active":{"arena_live":{"status":"DRAINING"}}}
            try:
                before=deepcopy(w.data)
                self.assertEqual(apply(w,Busy())["status"],"DEFERRED")
                self.assertEqual(w.data,before)
            finally: arena.close()


if __name__=="__main__": unittest.main()
