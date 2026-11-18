"""Root integration regressions against fictional FakeExchange transport only.

This suite intentionally exposes current in-progress defects; copy into tests
after fixes. No profile/key files, real transport, Jev, or worker are accessed.
"""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_exchange_lab import FakeExchange
from trading_lab import exchange_engine as engine, exchange_protection as protection
from trading_lab.arena_strategies import catalogue_defaults
from trading_lab.okx_readonly import PreflightError
from trading_lab.exchange_lab import ExchangeLab
from trading_lab.ops_policy import ClockGuard
from trading_lab.ops_policy import validate_stop_receipt
from trading_lab.ops_storage import HealthState


class FaultExchange(FakeExchange):
    def __init__(self, clock):
        super().__init__(clock)
        self.lost_once = set()
        self.reject_once = set()
        self.child_aliases = {}

    def transport(self, request):
        route = urlsplit(request.full_url)
        key = (request.get_method(), route.path)
        if key in self.reject_once:
            self.reject_once.remove(key)
            self.requests.append(request)
            raise PreflightError("fictional_provider_rejection")
        response = super().transport(request)
        if key in self.lost_once:
            self.lost_once.remove(key)
            raise OSError("fictional_lost_ack_after_effect")
        if key == ("GET", "/api/v5/trade/order"):
            queried = parse_qs(route.query).get("ordId", [None])[0]
            if queried in self.child_aliases:
                returned = self.child_aliases[queried]
                response = {"code": "0", "data": [deepcopy(next(
                    row for row in self.orders.values() if row["ordId"] == returned))]}
        return response

    def post_count(self, path):
        return sum(r.get_method() == "POST" and urlsplit(r.full_url).path == path
                   for r in self.requests)


class ProtectionIntegrationTests(unittest.TestCase):
    def fixture(self, *, short=False):
        self.now = 1800000030.
        clock = lambda: self.now
        fake = FaultExchange(clock)
        api = fake.api()
        check = api.preflight(100)
        api.arm(100)  # Only this explicitly injected fake transport can execute.
        recipe = deepcopy(catalogue_defaults()[0])
        if short:
            recipe["rules"]["side"] = "short"
        state = engine.create([recipe], ["fictional-account"], "workshop_demo",
                              100, 300, self.now, "f" * 64)
        bot = state["bots"][0]
        engine.initialize(bot, check)
        quantity = Decimal("-.04" if short else ".04")
        basis = Decimal("84000" if short else "84000.1")
        fake.units, fake.basis = quantity, basis
        bot["_orders"] = [{"at": self.now, "fill_observed_at": self.now,
            "request": {"side": "sell" if short else "buy", "reduceOnly": False,
                        "clOrdId": "J5" + state["id"][:16] + "0E0000"},
            "result": {"ordId": "100000", "state": "filled", "contracts": ".04",
                       "average_price": str(basis), "signed_fee_usdt": "-.0168",
                       "gross_pnl_usdt": "0", "updated_ms": str(int(self.now * 1000))}}]
        engine.rebuild(bot)
        self.saved = []
        lab = SimpleNamespace(clock=clock,
            save=lambda s: self.saved.append(deepcopy(s)),
            identity_for=lambda s, b, kind: "J5" + s["id"][:16] + "0" + kind + "0001")
        return lab, state, bot, api, fake

    @staticmethod
    def parent(bot, fake):
        return fake.algos[bot["_protections"][-1]["body"]["algoClOrdId"]]

    def child(self, parent, fake, identity, *, fill=".04", size=".04"):
        quantity = Decimal(fill)
        row = {"instId": "BTC-USDT-SWAP", "tdMode": "isolated", "posSide": "net",
            "side": parent["side"], "ordType": "market", "reduceOnly": True,
            "sz": size, "clOrdId": "", "ordId": identity, "algoId": parent["algoId"],
            "state": "filled" if quantity == Decimal(size) else "partially_filled",
            "accFillSz": fill, "avgPx": "84000", "fee": str(-quantity * Decimal(".01") * 84000 * Decimal(".0005")),
            "feeCcy": "USDT", "pnl": "0", "rebate": "0", "uTime": str(int(self.now * 1000))}
        fake.orders["fictional-child-" + identity] = row
        return row

    def test_untrusted_algo_id_cannot_cancel_or_amend_before_any_transport(self):
        lab, state, bot, api, fake = self.fixture()
        protection.ensure(lab, state, bot, api)
        intent = protection.intent_of(bot["_protections"][-1])
        before = len(fake.requests)
        with self.assertRaises(PreflightError):
            api.cancel_protection("999999")
        with self.assertRaises(PreflightError):
            api.amend_protection("999999", intent, lab.identity_for(state, bot, "C"))
        self.assertEqual(len(fake.requests), before)

    def test_verified_parent_server_identity_cannot_change_under_same_client_id(self):
        lab, state, bot, api, fake = self.fixture()
        protection.ensure(lab, state, bot, api)
        intent = protection.intent_of(bot["_protections"][-1])
        self.parent(bot, fake)["algoId"] = "999999"
        with self.assertRaises(PreflightError):
            api.protective_details(intent)
        self.assertNotIn("999999", api.owned_algos)

    def test_receipt_with_false_cancel_on_close_cannot_be_verified_protection(self):
        lab, state, bot, api, fake = self.fixture()
        protection.ensure(lab, state, bot, api)
        self.parent(bot, fake)["cxlOnClosePos"] = False
        with self.assertRaises(PreflightError):
            api.protective_details(protection.intent_of(bot["_protections"][-1]))

    def test_documented_receipt_without_cancel_on_close_echo_remains_owned_reduce_only(self):
        lab, state, bot, api, fake = self.fixture(short=True)
        protection.ensure(lab, state, bot, api)
        record = bot["_protections"][-1]
        self.assertNotIn("cxlOnClosePos", record["receipt"])
        self.assertTrue(bot["protection"]["verified"])
        self.assertFalse(record["proof"]["cancel_on_close_reported"])
        intent = protection.intent_of(record)
        for key, changed in (("reduceOnly", False), ("side", "sell"), ("sz", "0.05"),
                             ("algoClOrdId", "foreign"), ("slTriggerPx", "1")):
            with self.subTest(key=key):
                bad = deepcopy(record["receipt"])
                bad[key] = changed
                with self.assertRaises(ValueError):
                    validate_stop_receipt(intent, bad)
        missing_reduce = deepcopy(record["receipt"])
        missing_reduce.pop("reduceOnly")
        with self.assertRaises(ValueError):
            validate_stop_receipt(intent, missing_reduce)

    def test_pending_or_malformed_stop_receipt_is_unavailable_not_attribute_error(self):
        lab, state, bot, api, fake = self.fixture()
        protection.ensure(lab, state, bot, api)
        intent = protection.intent_of(bot["_protections"][-1])
        for receipt in (None, [], "pending", {"ordIdList": [{}]}):
            with self.subTest(receipt=receipt):
                with self.assertRaises(ValueError):
                    validate_stop_receipt(intent, receipt)
        bad = deepcopy(bot["_protections"][-1]["receipt"])
        bad["ordIdList"] = [{}]
        with self.assertRaisesRegex(ValueError, "stop_child_id_shape"):
            validate_stop_receipt(intent, bad)

    def test_child_get_must_match_the_exact_requested_server_order_identity(self):
        lab, state, bot, api, fake = self.fixture()
        protection.ensure(lab, state, bot, api)
        parent = self.parent(bot, fake)
        parent.update(state="effective", ordIdList=["100001", "100002"])
        self.child(parent, fake, "100001", fill=".02", size=".02")
        self.child(parent, fake, "100002", fill=".02", size=".02")
        fake.child_aliases["100001"] = "100002"
        with self.assertRaises(PreflightError):
            api.protective_child(protection.intent_of(bot["_protections"][-1]),
                                 parent, "100001", bot["_contracts"])

    def test_triggered_owned_child_is_adopted_once_before_position_comparison(self):
        lab, state, bot, api, fake = self.fixture()
        protection.ensure(lab, state, bot, api)
        parent = self.parent(bot, fake)
        parent.update(state="effective", ordIdList=["100001"])
        self.child(parent, fake, "100001")
        fake.units = Decimal(0)
        protection.reconcile(lab, state, bot, api)
        observed = deepcopy(bot["_orders"])
        protection.reconcile(lab, state, bot, api)
        self.assertEqual(bot["_orders"], observed)
        self.assertEqual(Decimal(bot["_contracts"]), api.position())
        self.assertEqual(bot["trades"], 1)
        self.assertEqual(len(bot["_orders"]), 2)
        self.assertEqual(fake.post_count("/api/v5/trade/order"), 0)
        self.assertEqual(Decimal(bot["_signed_fees"]), Decimal("-.0336"))

    def test_effective_parent_without_authoritative_child_ids_remains_unresolved(self):
        lab, state, bot, api, fake = self.fixture()
        protection.ensure(lab, state, bot, api)
        self.parent(bot, fake).update(state="effective", ordIdList=[])
        try:
            protection.reconcile(lab, state, bot, api)
        except PreflightError:
            pass  # A fail-closed unresolved result is also acceptable.
        self.assertFalse(bot["_protections"][-1].get("terminal", False))

    def test_lost_placement_ack_reconciles_by_client_id_without_second_post(self):
        lab, state, bot, api, fake = self.fixture()
        fake.lost_once.add(("POST", "/api/v5/trade/order-algo"))
        protection.ensure(lab, state, bot, api)
        protection.ensure(lab, state, bot, api)
        self.assertEqual(fake.post_count("/api/v5/trade/order-algo"), 1)
        self.assertTrue(bot["protection"]["verified"])
        self.assertTrue(any(s["bots"][0].get("_protections") for s in self.saved))

    def test_partial_add_amends_exact_actual_size_and_never_widens_old_stop(self):
        for short in (False, True):
            with self.subTest(short=short):
                lab, state, bot, api, fake = self.fixture(short=short)
                protection.ensure(lab, state, bot, api)
                old_trigger = Decimal(self.parent(bot, fake)["slTriggerPx"])
                bot["_contracts"] = "-.06" if short else ".06"
                bot["entry_price"] = 85000 if short else 83000  # Desired wider SL is prohibited.
                protection.ensure(lab, state, bot, api)
                parent = self.parent(bot, fake)
                self.assertEqual(Decimal(parent["sz"]), Decimal(".06"))
                new_trigger = Decimal(parent["slTriggerPx"])
                self.assertLessEqual(new_trigger, old_trigger) if short else self.assertGreaterEqual(new_trigger, old_trigger)
                self.assertEqual(fake.post_count("/api/v5/trade/order-algo"), 1)
                self.assertEqual(fake.post_count("/api/v5/trade/amend-algos"), 1)
                self.assertTrue(bot["protection"]["verified"])

    def test_lost_amend_ack_observation_resolves_uncertainty_before_later_add(self):
        lab, state, bot, api, fake = self.fixture()
        protection.ensure(lab, state, bot, api)
        bot["_contracts"] = ".06"
        fake.lost_once.add(("POST", "/api/v5/trade/amend-algos"))
        try:
            protection.ensure(lab, state, bot, api)
        except (OSError, PreflightError):
            pass
        protection.ensure(lab, state, bot, api)  # Exact new receipt, after ACK loss.
        self.assertFalse(bot["_protections"][-1].get("amend_uncertain", False))
        bot["_contracts"] = ".08"
        protection.ensure(lab, state, bot, api)
        self.assertEqual(fake.post_count("/api/v5/trade/amend-algos"), 2)
        self.assertEqual(fake.post_count("/api/v5/trade/order-algo"), 1)

    def test_rejected_amend_retains_old_verified_intent_for_reconciliation(self):
        lab, state, bot, api, fake = self.fixture()
        protection.ensure(lab, state, bot, api)
        old_body = deepcopy(bot["_protections"][-1]["body"])
        bot["_contracts"] = ".06"
        fake.reject_once.add(("POST", "/api/v5/trade/amend-algos"))
        try:
            protection.ensure(lab, state, bot, api)
        except (OSError, PreflightError):
            pass
        # A separate desired/pending intent may be durable; confirmed intent is immutable.
        self.assertEqual(bot["_protections"][-1]["body"], old_body)
        self.assertFalse(bot["protection"]["verified"])

    def test_flat_cleanup_not_verified_from_cancel_intent_or_uncertain_ack(self):
        lab, state, bot, api, fake = self.fixture()
        protection.ensure(lab, state, bot, api)
        bot["_contracts"] = "0"
        fake.lost_once.add(("POST", "/api/v5/trade/cancel-algos"))
        try:
            protection.ensure(lab, state, bot, api)
        except (OSError, PreflightError):
            pass
        self.assertFalse(bot["protection"]["verified"])
        protection.ensure(lab, state, bot, api)
        self.assertTrue(bot["protection"]["verified"])
        self.assertTrue(bot["_protections"][-1]["terminal"])
        self.assertEqual(fake.post_count("/api/v5/trade/cancel-algos"), 1)
        # R2.1.3: our cancel is journalled even when its answer was lost, and the terminal stop names who took it off
        posts = [e["data"] for e in state["events"] if e["code"] == "protective_cancel_posted"]
        self.assertEqual([p["outcome"] for p in posts], ["uncertain"])
        terminal = [e["data"] for e in state["events"] if e["code"] == "protective_terminal"]
        self.assertEqual(terminal[-1]["cancelled_by"], "own_cleanup")

    def test_close_only_recovery_with_known_owned_fill_can_close_without_existing_stop(self):
        lab_stub, state, bot, api, fake = self.fixture()
        api.close_only = True  # Crash after entry fill and before protective intent.
        engine.stop(state, self.now, reason="Fictional owner-authorized recovery")
        state["_recovery"] = True
        lab = ExchangeLab.__new__(ExchangeLab)
        fixture_root = tempfile.TemporaryDirectory()
        self.addCleanup(fixture_root.cleanup)
        lab.root = Path(fixture_root.name)
        lab.lock = threading.RLock()
        lab.clock = lab.monotonic = lambda: self.now
        from trading_lab.exchange_clock import ExchangeClock
        lab.timebase = ExchangeClock(lab.clock, lab.monotonic)
        lab.timebase.synchronize(self.now)
        lab.event_positions = {}
        lab.save = lambda s, **kw: self.saved.append(deepcopy(s))
        lab.record_snapshot = lab.note_market_quality = lambda *args: None
        lab.ledgers = {state["context"]: SimpleNamespace(summary=lambda identity: {"unknown_billing": 0})}
        snapshot = {"at": self.now, "server_time": self.now, "quote_ts": self.now * 1000,
            "bar_close_ms": int((self.now // 60 - 1) * 60000), "bid": 84000, "ask": 84000.1,
            "mid": 84000.05, "sha256": "a" * 64,
            "features": {"spread_bps": .012, "sma_gap_bps": 5, "rsi_14": 50,
                         "return_5m_bps": 5, "return_1m_bps": 1, "range_position_20": 60,
                         "volume_ratio_5": 1}}
        with ThreadPoolExecutor(max_workers=1) as pool:
            lab.resources = {state["id"]: {"apis": [api], "pool": pool,
                "feed": SimpleNamespace(calls=0, snapshot=lambda: snapshot), "deadline": self.now + 300,
                "clock_guard": ClockGuard(), "health": HealthState(), "hosted_protection": True,
                "closing_deadlines": {}, "hold_deadlines": {}}}
            lab.step(state)
        self.assertEqual(fake.post_count("/api/v5/trade/order-algo"), 0)
        self.assertEqual(fake.post_count("/api/v5/trade/order"), 1)
        self.assertEqual(fake.units, 0)


if __name__ == "__main__":
    unittest.main()
