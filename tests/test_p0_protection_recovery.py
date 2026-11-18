"""P0 desired behavior: injected fake transport/temp roots, never a real account."""
import json
from decimal import Decimal
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import test_rules_only_exchange as rules_fixture
from test_exchange_lab import FakeExchange
from trading_lab import exchange_engine as engine


class P0ProtectionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = rules_fixture.RulesOnlyTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fx = self.fixture.fx

    def ticks(self, state, count=1):
        for _ in range(count):
            self.fx.lab.step(state)
            self.fx.now += 5

    @staticmethod
    def posts(fake, path):
        return sum(r.get_method() == "POST" and urlsplit(r.full_url).path == path
                   for r in fake.requests)

    def failed_stop(self, fake, request):
        result = self.original(fake, request)
        if request.get_method() == "POST" and urlsplit(request.full_url).path == "/api/v5/trade/order-algo":
            fake.algos[json.loads(request.data)["algoClOrdId"]].update(state="order_failed", failCode="51000")
        return result

    def run_rejected_stop(self, owner_stop):
        self.original = FakeExchange.transport
        with patch.object(FakeExchange, "transport", lambda fake, req: self.failed_stop(fake, req)):
            state = self.fixture.start()
            self.ticks(state)
            bot, fake = self.fixture.bot, self.fixture.fake
            if owner_stop:
                engine.stop(state, self.fx.now, reason="Fictional owner Stop")
            else:
                self.assertTrue(bot["stop_requested"], "Unverified own SL must start bounded close-only recovery")
            self.ticks(state, 4)
            self.assertEqual(Decimal(bot["_contracts"]), 0)
            self.assertEqual(fake.units, 0)
            self.assertEqual(state["status"], "DONE")
            self.assertEqual(bot["protection"]["status"], "FLAT")
            self.assertEqual(self.posts(fake, "/api/v5/trade/order-algo"), 1)
            self.assertEqual(sum(not o["reduceOnly"] for o in fake.orders.values()), 1)
            self.assertTrue(bot["accounting_reconciled"])
            self.assertEqual(bot["ai_requests"], 0)
            self.fx.lab.jev_factory.assert_not_called()

    def test_rejected_initial_sl_automatically_closes_owned_fill(self):
        self.run_rejected_stop(owner_stop=False)

    def test_owner_stop_does_not_require_successful_sl_replacement(self):
        self.run_rejected_stop(owner_stop=True)

    def test_lost_sl_ack_is_reconciled_without_duplicate_or_false_failure(self):
        original = FakeExchange.transport
        def lost(fake, request):
            result = original(fake, request)
            if request.get_method() == "POST" and urlsplit(request.full_url).path == "/api/v5/trade/order-algo":
                raise OSError("fictional ACK lost after acceptance")
            return result
        with patch.object(FakeExchange, "transport", lost):
            state = self.fixture.start()
            self.ticks(state, 2)
            self.assertTrue(self.fixture.bot["protection"]["verified"])
            self.assertFalse(self.fixture.bot["stop_requested"])
            self.assertEqual(self.posts(self.fixture.fake, "/api/v5/trade/order-algo"), 1)

    def test_unknown_sl_receipt_is_bounded_and_never_claims_flat(self):
        original = FakeExchange.transport
        def unavailable(fake, request):
            if request.get_method() == "GET" and urlsplit(request.full_url).path == "/api/v5/trade/order-algo":
                raise OSError("fictional receipt unavailable")
            return original(fake, request)
        with patch.object(FakeExchange, "transport", unavailable):
            state = self.fixture.start()
            self.ticks(state)
            self.assertTrue(self.fixture.bot["stop_requested"])
            self.ticks(state, 25)
            self.assertEqual(state["status"], "PAUSED")
            self.assertNotEqual(self.fixture.bot["status"], "STOPPED")
            self.assertGreater(self.fixture.fake.units, 0)
            self.assertFalse(self.fixture.bot["accounting_reconciled"])
            self.assertEqual(self.posts(self.fixture.fake, "/api/v5/trade/order-algo"), 1)

    def test_explicit_negative_placement_ack_without_algo_closes_owned_fill(self):
        original = FakeExchange.transport
        def rejected(fake, request):
            if request.get_method() == "POST" and urlsplit(request.full_url).path == "/api/v5/trade/order-algo":
                fake.requests.append(request)
                identity = json.loads(request.data)["algoClOrdId"]
                return {"code": "1", "data": [{"sCode": "51008", "algoId": "", "algoClOrdId": identity}]}
            return original(fake, request)
        with patch.object(FakeExchange, "transport", rejected):
            state = self.fixture.start()
            self.ticks(state, 6)
            bot = self.fixture.bot
            self.assertEqual(self.fixture.fake.units, 0)
            self.assertTrue(bot["accounting_reconciled"])
            self.assertEqual(state["status"], "DONE")
            self.assertEqual(self.posts(self.fixture.fake, "/api/v5/trade/order-algo"), 1)

    def test_provider_timeout_code_is_unknown_not_explicit_placement_rejection(self):
        original = FakeExchange.transport
        def timeout(fake, request):
            if request.get_method() == "POST" and urlsplit(request.full_url).path == "/api/v5/trade/order-algo":
                fake.requests.append(request)
                identity = json.loads(request.data)["algoClOrdId"]
                return {"code": "1", "data": [{"sCode": "50004", "algoId": "", "algoClOrdId": identity}]}
            return original(fake, request)
        with patch.object(FakeExchange, "transport", timeout):
            state = self.fixture.start()
            self.ticks(state, 26)
            self.assertEqual(state["status"], "PAUSED")
            self.assertGreater(self.fixture.fake.units, 0)
            self.assertFalse(self.fixture.bot["_protections"][0].get("terminal", False))
            self.assertFalse(self.fixture.bot["accounting_reconciled"])


if __name__ == "__main__":
    unittest.main()
