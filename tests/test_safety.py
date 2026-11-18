from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from trading_lab.jev import BudgetLedger, JevClient
from trading_lab.risk import RiskPolicy, Snapshot


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = 1000.0
        self.state = Snapshot(observed_at=self.now, revision="r1")
        self.calls = 0

    def tearDown(self):
        self.tmp.cleanup()

    def response(self, body, key):
        self.calls += 1
        self.assertEqual(json.loads(body)["model"], "typesafe/jev-1.13")
        return {"answers": {"candidate_filter": {"choice": "paper_allow", "confidence": .9}},
                "usage": {"cost": .00003}}

    def client(self, transport=None):
        return JevClient(self.root, clock=lambda: self.now, transport=transport or self.response,
                         key_reader=lambda root: "unit-test-placeholder")

    def test_valid_paper_recommendation_and_persistent_attempt_limit(self):
        first = self.client().decide(self.state, lambda: self.state)
        self.assertEqual(first.action, "paper_allow")
        self.assertFalse(first.live_execution_enabled)
        second = self.client().decide(self.state, lambda: self.state)
        self.assertEqual(second.reason, "request_budget_or_cooldown")
        self.assertEqual(self.calls, 1)

    def test_timeout_consumes_attempt_and_records_unknown_billing(self):
        def timeout(body, key):
            self.calls += 1
            raise TimeoutError()
        client = self.client(timeout)
        self.assertEqual(client.decide(self.state, lambda: self.state).action, "hold")
        self.assertEqual(client.ledger.summary()["unresolved_billing_attempts"], 1)
        self.assertEqual(client.decide(self.state, lambda: self.state).reason, "request_budget_or_cooldown")
        self.assertEqual(self.calls, 1)

    def test_changed_current_state_cannot_apply_old_answer(self):
        result = self.client().decide(self.state, lambda: replace(self.state, revision="r2"))
        self.assertEqual(result.reason, "state_changed")
        self.assertEqual(result.action, "hold")

    def test_stale_after_response(self):
        def slow(body, key):
            self.now += 31
            return self.response(body, key)
        result = self.client(slow).decide(self.state, lambda: self.state)
        self.assertEqual(result.reason, "stale_or_future_state")

    def test_malformed_answers_fail_closed(self):
        for value in [None, [], "bad", {"answers": []},
                      {"answers": {"candidate_filter": {"choice": "withdraw", "confidence": 1}}},
                      {"answers": {"candidate_filter": {"choice": "paper_allow", "confidence": True}}},
                      {"answers": {"candidate_filter": {"choice": "paper_allow", "confidence": float("nan")}}},
                      {"answers": {"candidate_filter": {"choice": "paper_allow", "confidence": .69}}}]:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as folder:
                client = JevClient(folder, clock=lambda: self.now, transport=lambda b, k: value,
                                   key_reader=lambda r: "unit-test-placeholder")
                self.assertEqual(client.decide(self.state, lambda: self.state).action, "hold")

    def test_unsafe_inputs_do_not_call_network(self):
        states = [replace(self.state, mode="live"), replace(self.state, kill_switch=True),
                  replace(self.state, notional_usd=1000), replace(self.state, daily_loss_usd=5),
                  replace(self.state, daily_loss_usd=4.5), replace(self.state, open_positions=1),
                  replace(self.state, notional_usd=float("inf")), replace(self.state, spread_bps=True)]
        for state in states:
            self.assertEqual(self.client().decide(state, lambda: state).action, "hold")
        self.assertEqual(self.calls, 0)

    def test_concurrent_reservations_allow_exactly_one(self):
        path = self.root / "shared.sqlite3"
        ledgers = [BudgetLedger(path) for _ in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda ledger: ledger.reserve("test", self.now), ledgers))
        self.assertEqual(sum(token is not None for token in results), 1)

    def test_unknown_cost_is_not_silently_zero(self):
        def no_cost(body, key):
            result = self.response(body, key)
            result["usage"]["cost"] = True
            return result
        client = self.client(no_cost)
        result = client.decide(self.state, lambda: self.state)
        self.assertIsNone(result.reported_cost_usd)
        self.assertEqual(client.ledger.summary()["unresolved_billing_attempts"], 1)


if __name__ == "__main__":
    unittest.main()
