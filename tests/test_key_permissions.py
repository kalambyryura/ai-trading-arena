"""R2.1.10 (D-1, D136 p. 7): a Live key with Withdraw fails the access check and a fresh Start; Demo gets a warning."""
import unittest
from unittest.mock import patch

import test_exchange_lab as fixture
from trading_lab import exchange_diagnostics
from trading_lab.battle_review import safe_data
from trading_lab.exchange_accounts import public_profiles
from trading_lab.okx_readonly import PreflightError

REASON = "live_key_has_unused_permissions"


class KeyPermissionTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.ExchangeTests()
        self.fx.setUp()
        self.network = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network forbidden"))
        self.network.start()

    def tearDown(self):
        self.network.stop()
        self.fx.tearDown()

    def posts(self, name):
        return [r for r in self.fx.fakes[name].requests if r.get_method() == "POST"]

    def test_d1_live_key_with_withdraw_fails_the_access_check(self):
        self.fx.fakes["arena-a"].perm = "read_only,trade,withdraw"
        result = self.fx.lab.check(self.fx.lab.data["generation"], ["arena-a"])
        self.assertFalse(result["ready"])
        check = self.fx.lab.data["checks"]["arena-a"]
        self.assertEqual(check["reason"], exchange_diagnostics.MESSAGES[REASON])
        self.assertIn("Withdraw", check["reason"])
        self.assertEqual(self.posts("arena-a"), [])

    def test_d1_live_start_with_withdraw_is_refused_before_any_write(self):
        self.fx.fakes["arena-a"].perm = "read_only,trade,withdraw"
        result = self.fx.lab.start(self.fx.lab.data["generation"], "arena_live", ["sample-trend"], ["arena-a"], 100, 300)
        state = self.fx.lab.load(result["id"])
        with self.assertRaises(PreflightError) as caught:
            self.fx.lab.prepare(state, self.fx.lab.chosen_profiles(["arena-a"]), False)
        self.assertEqual(str(caught.exception), REASON)
        self.assertEqual(self.posts("arena-a"), [])  # no leverage setting, no order
        self.assertFalse(any(e["code"] == "account_checked" for e in state["events"]))
        # the worker's handler shows the human reason, not exchange_process_failed
        self.assertEqual(exchange_diagnostics.failure(caught.exception)["reason"], REASON)
        self.assertIn("Read+Trade", exchange_diagnostics.failure(caught.exception)["message"])

    def test_d1_a_resume_with_withdraw_is_never_refused(self):
        result = self.fx.lab.start(self.fx.lab.data["generation"], "arena_live", ["sample-trend"], ["arena-a"], 100, 300)
        state = self.fx.lab.load(result["id"])
        lab = self.fx.lab
        lab.prepare(state, lab.chosen_profiles(["arena-a"]), False)
        self.fx.fakes["arena-a"].perm = "read_only,trade,withdraw"
        for initialized in (True, False):  # False: a recovery that reads the full preflight again
            lab.resources.pop(state["id"])["stack"].close()
            lab.resume(lab.data["generation"], state["id"])
            state = lab.sessions[state["id"]]
            state["bots"][0]["_initialized"] = initialized
            lab.prepare(state, lab.chosen_profiles(["arena-a"]), True)  # an open Live position stays closable
            self.assertTrue(state["_ready"])

    def test_d1_withdraw_added_during_a_live_round_never_stops_it(self):
        result = self.fx.lab.start(self.fx.lab.data["generation"], "arena_live", ["sample-trend"], ["arena-a"], 100, 300)
        state = self.fx.lab.load(result["id"])
        self.fx.lab.prepare(state, self.fx.lab.chosen_profiles(["arena-a"]), False)
        self.fx.fakes["arena-a"].perm = "read_only,trade,withdraw"  # the owner widened the key mid-round
        for _ in range(4):
            self.fx.lab.step(state)
            self.fx.now += 5
        self.assertFalse([e for e in state["events"] if e["data"].get("reason") == REASON])
        self.assertIn(state["status"], {"RUNNING", "DRAINING"})

    def test_d1_demo_key_with_withdraw_passes_with_a_visible_warning(self):
        self.fx.fakes["demo"].perm = "read_only,trade,withdraw"
        result = self.fx.lab.check(self.fx.lab.data["generation"], ["demo"])
        self.assertTrue(result["ready"])
        check = self.fx.lab.data["checks"]["demo"]
        self.assertIn("Withdraw", check["permission_warning"])
        self.assertIn(check["permission_warning"], check["reason"])
        profile = next(p for p in public_profiles(self.fx.root, self.fx.lab.data["checks"]) if p["id"] == "demo")
        self.assertEqual(profile["permission_warning"], check["permission_warning"])
        state = self.fx.start()
        event = next(e for e in state["events"] if e["code"] == "account_checked")
        self.assertEqual(event["data"]["extra_permissions"], ["withdraw"])
        self.assertEqual(safe_data(event["data"], event_code="account_checked").get("extra_permissions"), ["withdraw"])
        self.assertNotIn("unused_permissions", safe_data(event["data"], event_code="account_checked"))  # the old key stays out

    def test_d1_clean_keys_are_as_before(self):
        result = self.fx.lab.check(self.fx.lab.data["generation"], ["arena-a", "demo"])
        self.assertTrue(result["ready"])
        self.assertNotIn("permission_warning", self.fx.lab.data["checks"]["demo"])
        state = self.fx.start()
        event = next(e for e in state["events"] if e["code"] == "account_checked")
        self.assertEqual(event["data"]["extra_permissions"], [])


if __name__ == "__main__":
    unittest.main()
