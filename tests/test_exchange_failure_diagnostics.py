"""Regression fixtures for the 27 October target-PC clock incident. No network."""
import json
import ast
from pathlib import Path
import unittest
from unittest.mock import patch

import test_exchange_lab as fixture
from trading_lab.battle_review import safe_data
from trading_lab.okx_readonly import PreflightError


class FailureTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.ExchangeTests()
        self.fx.setUp()
        self.network = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network forbidden"))
        self.network.start()

    def tearDown(self):
        self.network.stop()
        self.fx.tearDown()

    def test_clock_difference_does_not_claim_foreign_activity_or_stop_other_bots(self):
        state = self.fx.start()
        with patch.object(self.fx.lab, "checked_account", side_effect=PreflightError("clock_difference_exceeds_five_minutes")):
            for _ in range(3):
                self.fx.lab.step(state)
                self.fx.now += 5
        bot = state["bots"][0]
        self.assertFalse(bot["_manual_review"])
        self.assertFalse(bot["stop_requested"])
        self.assertEqual(bot["admission_blocked"], "account_unavailable")
        self.assertEqual(state["status"], "RUNNING")
        self.assertEqual(len(self.fx.fakes["demo"].orders), 0)
        self.assertEqual(bot["ai_requests"], 0)
        self.assertFalse(any(e["code"] == "stop_requested" for e in state["events"]))

    def test_real_ownership_mismatch_still_stops_and_requires_review(self):
        state = self.fx.start()
        with patch.object(self.fx.lab, "checked_account", side_effect=PreflightError("actual_position_differs_from_owned_fills")):
            self.fx.lab.step(state)
        self.assertTrue(state["bots"][0]["_manual_review"])
        self.assertTrue(state["bots"][0]["stop_requested"])
        self.assertTrue(state["incomplete"])
        self.assertEqual(len(self.fx.fakes["demo"].orders), 0)

    def test_existing_ownership_guards_keep_their_manual_review_classification(self):
        from trading_lab.exchange_diagnostics import MANUAL_REVIEW_REASONS
        root = Path(__file__).resolve().parents[1] / "trading_lab"
        for name in ("exchange_accounts.py", "exchange_api.py", "exchange_engine.py",
                     "exchange_lab.py", "exchange_protection.py", "okx_readonly.py"):
            for node in ast.walk(ast.parse((root / name).read_text(encoding="utf-8-sig"))):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "PreflightError" and node.args
                        and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                    continue
                code = node.args[0].value
                if code != "clock_difference_exceeds_five_minutes" and any(
                        s in code for s in ("foreign", "differs", "changed", "multiple", "exceeds", "mismatch", "regressed")):
                    self.assertIn(code, MANUAL_REVIEW_REASONS, name)

    def failed_worker(self, error):
        lab = self.fx.lab
        result = lab.start(lab.data["generation"], "workshop_demo", [self.fx.version], ["demo"], 100, 18000)
        state = lab.load(result["id"])
        with patch.object(lab, "prepare", side_effect=error):
            lab.worker(state, lab.chosen_profiles(["demo"]), False)
        return state

    def test_start_failure_keeps_fixed_reason_and_actionable_clock_message(self):
        state = self.failed_worker(PreflightError("clock_difference_exceeds_five_minutes"))
        event = next(e for e in state["events"] if e["code"] == "exchange_error")
        self.assertEqual(event["data"].get("reason"), "clock_difference_exceeds_five_minutes")
        self.assertEqual(event["data"].get("stage"), "preflight")
        self.assertIn("годинник", state["error"].lower())
        self.assertTrue(state["incomplete"])
        self.assertEqual(state["status"], "DONE")
        self.assertEqual(len(self.fx.fakes["demo"].orders), 0)

    def test_exception_bodies_never_enter_saved_error_or_export(self):
        for error in (RuntimeError("SECRET_KEY authorization PRIVATE_BODY"),
                      PreflightError("SECRET_ALPHANUMERIC_TOKEN")):
            with self.subTest(error=type(error).__name__):
                state = self.failed_worker(error)
                event = next(e for e in state["events"] if e["code"] == "exchange_error")
                self.assertEqual(event["data"].get("reason"), "exchange_process_failed")
                self.assertNotIn("SECRET", json.dumps([state["error"], event]))

    def test_bundle_preserves_known_failure_and_drops_arbitrary_reason(self):
        code = "clock_difference_exceeds_five_minutes"
        result = safe_data({"reason": code, "stage": "preflight", "secret": "PRIVATE"}, event_code="exchange_error")
        self.assertEqual(result.get("reason"), code)
        self.assertEqual(result.get("stage"), "preflight")
        for bad in ("PRIVATE_KEY", "https://private.example/key", "clock_difference_exceeds_five_minutes_PRIVATE"):
            self.assertNotIn("reason", safe_data({"reason": bad}, event_code="exchange_check_failed"))
        self.assertNotIn("stage", safe_data({"stage": ["PRIVATE"]}, event_code="exchange_error"))

    def test_bundle_preserves_measured_clock_deltas_without_free_text(self):
        data = {"reasons": ["wall_clock_jump", "observation_gap", "PRIVATE"],
                "wall_delta_s": 3611.7, "mono_delta_s": 11.1, "gap_s": 0,
                "invalidate_temporal_state": True, "force_position_close": False,
                "secret": "PRIVATE"}
        result = safe_data(data, event_code="clock_gap")
        self.assertEqual(result.get("reasons"), ["wall_clock_jump", "observation_gap"])
        self.assertEqual(result.get("wall_delta_s"), "3611.7")
        self.assertEqual(result.get("mono_delta_s"), "11.1")
        self.assertIs(result.get("force_position_close"), False)
        self.assertNotIn("PRIVATE", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
