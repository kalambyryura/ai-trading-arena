"""Independent desired-behaviour T01-T08 regressions; fake/offline ONLY.

Uses the target repo's existing FakeExchange/RulesOnlyTests fixtures, not a
second execution engine. All disposable fixture files stay below QA_SCOPE.
Run this standalone file with P0_TARGET_REPO set explicitly. No worker process,
real profile, socket, paid Jev, CLI executor, or production endpoint is used.
Baseline RED is intentional; do not use expectedFailure to hide it.
"""
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

sys.dont_write_bytecode = True
QA_SCOPE = Path(os.environ.get("P0_FAULT_QA_SCOPE", str(Path(__file__).resolve().parent))).resolve()
TARGET = Path(os.environ.get("P0_TARGET_REPO", str(Path(__file__).resolve().parents[1]))).resolve()
if not (TARGET / "trading_lab/exchange_protection.py").is_file():
    raise RuntimeError("Explicit P0 target repository required")
SOURCE_PATHS = ["trading_lab/exchange_lab.py", "trading_lab/exchange_protection.py",
                "trading_lab/exchange_engine.py", "trading_lab/exchange_api.py",
                "tests/test_exchange_lab.py", "tests/test_protection_integration.py"]
PINS_BEFORE_IMPORT = {p: hashlib.sha256((TARGET / p).read_bytes()).hexdigest() for p in SOURCE_PATHS}
sys.path.insert(0, str(TARGET))
sys.path.insert(0, str(TARGET / "tests"))
import test_exchange_lab as fixture_module
import test_rules_only_exchange as rules_fixture
import test_protection_integration as protection_fixture
from trading_lab import exchange_engine as engine, exchange_protection as protection
from trading_lab.exchange_api import ExchangeFeed
from trading_lab.exchange_lab import ExchangeLab
from trading_lab.okx_readonly import PreflightError


class P0ProtectionFaultTests(unittest.TestCase):
    def setUp(self):
        sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(sandbox.cleanup)
        self.tmp = Path(sandbox.name)
        self.old_tempdir = tempfile.tempdir
        tempfile.tempdir = str(self.tmp)
        self.addCleanup(setattr, tempfile, "tempdir", self.old_tempdir)
        for attr in ("connect", "connect_ex"):
            self.enterContext(patch.object(socket.socket, attr, side_effect=AssertionError("Network forbidden in P0 fixture")))
        self.enterContext(patch.object(socket, "create_connection", side_effect=AssertionError("Network forbidden in P0 fixture")))
        self.fault = {"sl_reject": False, "sl_lost_ack": False,
                      "sl_lookup_failures": 0, "partial_pending": False,
                      "late_client": None, "late_seen": False}
        original = fixture_module.FakeExchange.transport

        def transport(fake, request):
            route = urlsplit(request.full_url)
            method, path = request.get_method(), route.path
            query = parse_qs(route.query)
            if method == "GET" and path == "/api/v5/trade/order-algo" and self.fault["sl_lookup_failures"]:
                fake.requests.append(request)
                self.fault["sl_lookup_failures"] -= 1
                raise PreflightError("network_error")
            if method == "POST" and path == "/api/v5/trade/cancel-order":
                fake.requests.append(request)
                body = json.loads(request.data)
                row = fake.orders[body["clOrdId"]]
                # The cancel ACK is NOT a final fill fact. A final cumulative
                # fill can arrive before the next authoritative order GET.
                self.fault["late_client"] = body["clOrdId"]
                return {"code": "0", "data": [{"sCode": "0", "ordId": row["ordId"], "clOrdId": body["clOrdId"]}]}
            if method == "GET" and path == "/api/v5/trade/order":
                client = query.get("clOrdId", [None])[0]
                if client == self.fault["late_client"] and client is not None and not self.fault["late_seen"]:
                    self.fault["late_seen"] = True
                    row = fake.orders[client]
                    delta = Decimal(row["sz"]) - Decimal(row["accFillSz"])
                    price = Decimal(row["avgPx"])
                    fee = -delta * Decimal(".01") * price * Decimal(".0005")
                    sign = Decimal(1) if row["side"] == "buy" else Decimal(-1)
                    fake.units += sign * delta
                    fake.cash += fee
                    row.update(state="filled", accFillSz=row["sz"], fee=str(Decimal(row["fee"]) + fee),
                               uTime=str(int(fake.clock() * 1000)))
                    bill = next(b for b in fake.bills if b["ordId"] == row["ordId"])
                    bill.update(fee=row["fee"], balChg=row["fee"])
            response = original(fake, request)
            if method == "POST" and path == "/api/v5/trade/order":
                body = json.loads(request.data)
                if not body["reduceOnly"] and self.fault["partial_pending"]:
                    fake.orders[body["clOrdId"]]["state"] = "partially_filled"
            if method == "POST" and path == "/api/v5/trade/order-algo":
                body = json.loads(request.data)
                if self.fault["sl_reject"]:
                    fake.algos[body["algoClOrdId"]].update(state="order_failed", failCode="51000")
                if self.fault["sl_lost_ack"]:
                    self.fault["sl_lost_ack"] = False
                    raise OSError("fictional_lost_ack_after_effect")
            return response

        self.enterContext(patch.object(fixture_module.FakeExchange, "transport", transport))
        self.fx_case = rules_fixture.RulesOnlyTests(methodName="runTest")
        self.fx_case.setUp()
        self.addCleanup(self.fx_case.doCleanups)
        self.fx = self.fx_case.fx
        self.assertTrue(self.fx.root.resolve().is_relative_to(self.tmp.resolve()))
        self.state = self.bot = self.fake = None

    def start(self):
        self.state = self.fx_case.start()
        self.bot = self.state["bots"][0]
        self.fake = self.fx.fakes["demo"]
        self.assertEqual(self.state["execution_mode"], "rules_only")
        return self.state

    def ticks(self, count=1, seconds=5):
        for _ in range(count):
            if self.state["status"] in {"DONE", "PAUSED"}:
                break
            self.fx.lab.step(self.state)
            self.fx.now += seconds

    def stop(self):
        engine.stop(self.state, self.fx.now, reason="Fictional owner Stop in isolated fault fixture")
        self.fx.lab.save(self.state)

    def posts(self, path):
        return [r for r in self.fake.requests if r.get_method() == "POST" and urlsplit(r.full_url).path == path]

    def no_new_entry(self):
        self.assertEqual(sum(not row["reduceOnly"] for row in self.fake.orders.values()), 1)
        self.fx.lab.jev_factory.assert_not_called()

    def flat_settled(self):
        self.assertEqual(self.fake.units, Decimal(0), "actual fictional exchange residual must reach zero")
        self.assertEqual(Decimal(self.bot["_contracts"]), Decimal(0), "journal must reconcile actual residual")
        self.assertTrue(self.bot["accounting_reconciled"], "ACK alone cannot establish settlement")
        self.assertEqual(self.state["status"], "DONE")
        self.no_new_entry()

    def seed_terminal_no_fill_history(self, count):
        """Preload persisted fictional canceled close receipts for cap boundaries.

        No fills/fees/cash are invented or counters reset. These rows model
        previously sent, terminal reduce-only IOC/market intents, not 60 POSTs
        performed by the test. The current owned fill remains authoritative.
        """
        self.assertLessEqual(len(self.bot["_orders"]), count)
        self.assertLessEqual(count, engine.LIMITS["max_order_intents_per_bot"])
        api = self.fx.lab.resources[self.state["id"]]["apis"][0]
        initial = self.bot["_orders"][0]["request"]
        actual_before = self.fake.units
        while len(self.bot["_orders"]) < count:
            body = {"instId": initial["instId"], "tdMode": "isolated", "posSide": "net",
                    "side": "sell" if initial["side"] == "buy" else "buy", "ordType": "market",
                    "sz": initial["sz"], "reduceOnly": True, "stpMode": "cancel_taker",
                    "clOrdId": self.fx.lab.identity_for(self.state, self.bot, "C")}
            raw = {**body, "ordId": str(200000 + len(self.fake.orders)), "state": "canceled",
                   "accFillSz": "0", "avgPx": "", "fee": "0", "feeCcy": "USDT",
                   "pnl": "0", "rebate": "0", "uTime": str(int(self.fx.now * 1000))}
            self.assertNotIn(body["clOrdId"], self.fake.orders)
            self.fake.orders[body["clOrdId"]] = raw
            order = {"request": body, "at": self.fx.now, "send_started": True, "result": None}
            self.bot["_orders"].append(order)
            engine.record_result(self.state, self.bot, order, api.parse_order_result(raw, body), self.fx.now)
        self.assertEqual(self.fake.units, actual_before)
        self.assertFalse(engine.unresolved(self.bot))
        self.fx.lab.save(self.state)

    def test_T01_failed_initial_sl_requests_bounded_recovery_without_owner_stop(self):
        self.fault["sl_reject"] = True
        self.start()
        self.ticks(7)
        self.assertTrue(self.bot["stop_requested"] or self.bot.get("_exit_requested") or self.state["status"] == "DONE",
                        "terminal SL failure must initiate bounded owned close, not admission-only HOLD")
        self.flat_settled()
        self.assertLessEqual(len(self.posts("/api/v5/trade/order-algo")), 1, "failed initial protection must not become a create loop")

    def test_T01_owner_stop_closes_known_fill_despite_rejected_initial_sl(self):
        self.fault["sl_reject"] = True
        self.start()
        self.ticks()
        self.stop()
        self.ticks(5)
        self.flat_settled()
        closes = [json.loads(r.data) for r in self.posts("/api/v5/trade/order") if json.loads(r.data)["reduceOnly"]]
        self.assertEqual(len(closes), 1)
        self.assertEqual(Decimal(closes[0]["sz"]), Decimal(".02"))

    def test_T02_entry_accepted_lost_ack_does_not_repeat_buy(self):
        self.fx.fakes["demo"].lost_ack = True
        self.start()
        self.ticks(2)
        self.assertGreater(self.fake.units, 0)
        self.no_new_entry()
        self.stop()
        self.ticks(4)
        self.flat_settled()

    def test_T02_sl_accepted_lost_ack_unknown_lookup_then_same_id_reconciles(self):
        self.fault.update(sl_lost_ack=True, sl_lookup_failures=1)
        self.start()
        self.ticks()
        self.assertEqual(len(self.posts("/api/v5/trade/order-algo")), 1)
        self.assertFalse(self.bot.get("protection", {}).get("verified", False))
        self.ticks(3)
        self.assertEqual(len(self.posts("/api/v5/trade/order-algo")), 1)
        self.assertTrue(self.bot["protection"]["verified"])
        self.no_new_entry()
        self.stop()
        self.ticks(4)
        self.flat_settled()

    def test_T03_partial_entry_cancel_late_fill_failed_sl_closes_final_actual_volume(self):
        self.fx.fakes["demo"].entry_fraction = Decimal(".5")
        self.fault.update(partial_pending=True, sl_reject=True)
        self.start()
        self.ticks()
        self.stop()
        self.ticks(7)
        self.assertTrue(self.fault["late_seen"], "cancel ACK must be followed by final cumulative fill reconciliation")
        self.flat_settled()
        fills = [row for row in self.fake.orders.values() if not row["reduceOnly"]]
        self.assertEqual(Decimal(fills[0]["accFillSz"]), Decimal(".02"))
        self.assertEqual(sum(Decimal(row["accFillSz"]) for row in self.fake.orders.values() if row["reduceOnly"]), Decimal(".02"))

    def test_T03_partial_close_preserves_remaining_until_second_actual_close(self):
        self.start()
        self.ticks()
        self.fake.close_fraction = Decimal(".5")
        self.stop()
        self.ticks()
        self.assertEqual(self.fake.units, Decimal(".01"))
        self.assertEqual(Decimal(self.bot["_contracts"]), Decimal(".01"))
        self.assertNotEqual(self.state["status"], "DONE")
        self.assertFalse(self.bot["accounting_reconciled"])
        self.fake.close_fraction = Decimal(1)
        self.ticks(4)
        self.flat_settled()

    def fire_owned_sl(self):
        parent = next(a for a in self.fake.algos.values() if a["state"] == "live")
        q = abs(self.fake.units)
        price = self.fake.bid
        gross = q * Decimal(".01") * (price - self.fake.basis)
        fee = -q * Decimal(".01") * price * Decimal(".0005")
        oid = "100099"
        parent.update(state="effective", ordIdList=[oid])
        self.fake.orders["fictional-stop-child"] = {
            "instId": "BTC-USDT-SWAP", "tdMode": "isolated", "posSide": "net", "side": "sell",
            "ordType": "market", "reduceOnly": True, "sz": str(q), "clOrdId": "", "ordId": oid,
            "algoId": parent["algoId"], "state": "filled", "accFillSz": str(q), "avgPx": str(price),
            "fee": str(fee), "feeCcy": "USDT", "pnl": str(gross), "rebate": "0", "uTime": str(int(self.fx.now * 1000))}
        self.fake.units = Decimal(0)
        self.fake.cash += gross + fee
        self.fake.bills.append({"billId": "100099", "ts": str(int(self.fx.now * 1000)), "type": "2", "subType": "3",
                                "ccy": "USDT", "instId": "BTC-USDT-SWAP", "ordId": oid, "pnl": str(gross),
                                "fee": str(fee), "balChg": str(gross + fee), "posBalChg": "0"})

    def test_T04_sl_trigger_and_repeated_stop_adopt_one_child_no_reversal(self):
        self.start()
        self.ticks()
        self.fire_owned_sl()
        self.stop()
        self.stop()
        self.ticks(4)
        self.flat_settled()
        self.assertEqual(len(self.posts("/api/v5/trade/order")), 1, "triggered SL already closed: no duplicate market close")
        self.assertEqual(sum(bool(o.get("_protective_parent")) for o in self.bot["_orders"]), 1)
        self.assertTrue(all(p.get("terminal") for p in self.bot["_protections"]))

    def test_T04_flat_cleanup_waits_for_child_evidence_when_effective_parent_has_no_ids(self):
        helper = protection_fixture.ProtectionIntegrationTests(methodName="runTest")
        lab, state, bot, api, fake = helper.fixture()
        protection.ensure(lab, state, bot, api)
        parent = helper.parent(bot, fake)
        parent.update(state="effective", ordIdList=[])
        close = {"request": engine.close_body(bot, lab.identity_for(state, bot, "C")),
                 "at": helper.now, "send_started": True, "result": None}
        bot["_orders"].append(close)
        engine.record_result(state, bot, close,
            {"ordId": "100100", "state": "filled", "contracts": ".04", "average_price": "84000",
             "signed_fee_usdt": "-.0168", "gross_pnl_usdt": "0", "updated_ms": str(int(helper.now * 1000))}, helper.now)
        fake.units = Decimal(0)
        self.assertEqual(Decimal(bot["_contracts"]), 0)
        protection.reconcile(lab, state, bot, api)
        try:
            protection.ensure(lab, state, bot, api)
        except (OSError, PreflightError):
            pass
        self.assertFalse(bot["_protections"][-1].get("terminal", False),
                         "effective parent with unavailable child IDs is unresolved even after another owned order makes flat")
        self.assertNotEqual(bot["protection"]["status"], "FLAT", "flat inventory does not prove orphan child cleanup")

    def test_T04_manager_cannot_finish_flat_until_exact_terminal_child_is_reconciled(self):
        self.start()
        self.ticks()
        api = self.fx.lab.resources[self.state["id"]]["apis"][0]
        parent = next(iter(self.fake.algos.values()))
        pending = {"child_ids_missing": True}
        original = api.transport

        def unresolved_after_other_close(request):
            response = original(request)
            if (request.get_method() == "GET" and urlsplit(request.full_url).path == "/api/v5/trade/order-algo"
                    and not self.fake.units):
                # Inject an effective parent receipt after the separate Stop
                # close, with its authoritative child identity initially lost.
                parent.update(state="effective", ordIdList=[] if pending["child_ids_missing"] else ["100099"])
                return {"code": "0", "data": [deepcopy(parent)]}
            return response

        api.transport = unresolved_after_other_close
        self.stop()
        self.ticks(3)
        self.assertEqual(self.fake.units, 0)
        self.assertEqual(Decimal(self.bot["_contracts"]), 0)
        self.assertFalse(self.bot["_protections"][-1].get("terminal", False))
        self.assertNotEqual(self.bot["protection"]["status"], "FLAT")
        self.assertNotEqual(self.state["status"], "DONE")
        self.assertNotEqual(self.bot["status"], "STOPPED")
        self.assertFalse(self.bot["accounting_reconciled"])
        self.assertEqual(len(self.posts("/api/v5/trade/order")), 2)
        self.fake.orders["fictional-zero-fill-stop-child"] = {
            "instId": "BTC-USDT-SWAP", "tdMode": "isolated", "posSide": "net", "side": "sell",
            "ordType": "market", "reduceOnly": True, "sz": ".02", "clOrdId": "", "ordId": "100099",
            "algoId": parent["algoId"], "state": "canceled", "accFillSz": "0", "avgPx": "",
            "fee": "0", "feeCcy": "USDT", "pnl": "0", "rebate": "0", "uTime": str(int(self.fx.now * 1000))}
        pending["child_ids_missing"] = False
        self.ticks(3)
        self.flat_settled()
        self.assertTrue(self.bot["_protections"][-1]["terminal"])
        self.assertEqual(sum(bool(o.get("_protective_parent")) for o in self.bot["_orders"]), 1)
        self.assertEqual(len(self.posts("/api/v5/trade/order")), 2, "adoption must not issue another close")

    def test_T05_restart_with_owned_fill_failed_sl_and_repeated_stop_is_close_only(self):
        self.fault["sl_reject"] = True
        self.start()
        self.ticks()
        self.stop()
        self.stop()
        self.fx.lab.close()
        restarted = ExchangeLab(self.fx.root, self.fx.workshop, self.fx.arena,
            clock=self.fx.clock, monotonic=self.fx.clock, background=False,
            api_factory=lambda p: self.fx.fakes[p["id"]].api(p["environment"]),
            jev_factory=Mock(side_effect=AssertionError("Recovery must not instantiate Jev")),
            feed_factory=lambda api: ExchangeFeed(api, clock=self.fx.clock))
        self.fx.lab = restarted
        restarted.resume(restarted.data["generation"], self.state["id"])
        self.state = restarted.load(self.state["id"])
        self.bot = self.state["bots"][0]
        restarted.prepare(self.state, restarted.chosen_profiles(["demo"]), True)
        self.ticks(5)
        self.flat_settled()
        self.assertTrue(self.state["_recovery"])
        self.assertTrue(self.state["incomplete"], "restart history must not be rewritten complete evidence")

    def test_T06_entry_attempt_budget_exhausted_does_not_veto_owned_close(self):
        self.start()
        self.ticks()
        self.bot["_entry_attempts"] = 20
        limits = deepcopy(engine.LIMITS)
        self.stop()
        self.ticks(4)
        self.flat_settled()
        self.assertEqual(self.bot["_entry_attempts"], 20)
        self.assertEqual(engine.LIMITS, limits)
        self.assertLessEqual(len(self.bot["_orders"]), engine.LIMITS["max_order_intents_per_bot"])

    def test_T06_flat_entry_cannot_spend_reserved_final_twenty_intents(self):
        self.start()
        self.ticks()
        api = self.fx.lab.resources[self.state["id"]]["apis"][0]
        body = engine.close_body(self.bot, self.fx.lab.identity_for(self.state, self.bot, "C"))
        self.assertTrue(self.fx.lab.submit(self.state, self.bot, api, body, self.fx.now))
        self.fx.lab.checked_account(self.state, self.bot, api)
        self.fx.lab.closing(self.state, self.bot, api)
        self.assertEqual(self.fake.units, 0)
        self.assertFalse(self.bot["stop_requested"])
        self.assertEqual(self.state["status"], "RUNNING")
        self.seed_terminal_no_fill_history(40)
        entry = deepcopy(self.bot["_orders"][0]["request"])
        entry["clOrdId"] = self.fx.lab.identity_for(self.state, self.bot, "E")
        before = deepcopy(self.bot["_orders"])
        posts_before = len(self.posts("/api/v5/trade/order"))
        with self.assertRaisesRegex(PreflightError, "^close_intent_reserve$"):
            self.fx.lab.submit(self.state, self.bot, api, entry, self.fx.now)
        self.assertEqual(self.bot["_orders"], before)
        self.assertEqual(len(self.posts("/api/v5/trade/order")), posts_before)
        self.assertEqual(self.fake.units, 0)
        self.no_new_entry()

    def test_T06_forty_intents_blocks_add_and_partial_reduce_but_allows_full_stop(self):
        self.start()
        self.ticks()
        self.seed_terminal_no_fill_history(40)
        api = self.fx.lab.resources[self.state["id"]]["apis"][0]
        limits = deepcopy(engine.LIMITS)
        ledger = self.fx.lab.ledgers[self.state["context"]]
        billing_before = deepcopy(ledger.summary(self.state["id"]))
        before = deepcopy(self.bot["_orders"])
        posts_before = len(self.posts("/api/v5/trade/order"))
        add = deepcopy(self.bot["_orders"][0]["request"])
        add["clOrdId"] = self.fx.lab.identity_for(self.state, self.bot, "E")
        with self.assertRaisesRegex(PreflightError, "^close_intent_reserve$"):
            self.fx.lab.submit(self.state, self.bot, api, add, self.fx.now)
        self.bot["_reduce_remaining"] = ".01"
        reduction = engine.close_body(self.bot, self.fx.lab.identity_for(self.state, self.bot, "C"), ".01")
        with self.assertRaisesRegex(PreflightError, "^discretionary_reduction_close_reserve$"):
            self.fx.lab.submit(self.state, self.bot, api, reduction, self.fx.now)
        self.assertEqual(self.bot["_orders"], before)
        self.assertEqual(len(self.posts("/api/v5/trade/order")), posts_before)
        self.assertEqual(self.fake.units, Decimal(".02"))
        self.stop()
        self.ticks(4)
        self.flat_settled()
        close = json.loads(self.posts("/api/v5/trade/order")[-1].data)
        self.assertTrue(close["reduceOnly"])
        self.assertEqual(Decimal(close["sz"]), Decimal(".02"), "Stop closes all owned fill, not stale partial target")
        self.assertEqual(len(self.bot["_orders"]), 41)
        self.assertEqual(engine.LIMITS, limits)
        self.assertEqual(ledger.summary(self.state["id"]), billing_before)

    def test_T06_sixtieth_close_intent_is_allowed_without_cap_or_counter_reset(self):
        self.start()
        self.ticks()
        self.seed_terminal_no_fill_history(59)
        limits = deepcopy(engine.LIMITS)
        previous_ids = [o["request"]["clOrdId"] for o in self.bot["_orders"]]
        self.stop()
        self.ticks(4)
        self.flat_settled()
        self.assertEqual(len(self.bot["_orders"]), 60)
        self.assertEqual([o["request"]["clOrdId"] for o in self.bot["_orders"][:59]], previous_ids)
        self.assertEqual(engine.LIMITS, limits)
        self.assertEqual(len(self.posts("/api/v5/trade/order")), 2, "preloaded historical no-fill receipts are not new POSTs")

    def test_T06_exhausted_physical_sixty_intents_keeps_actual_residual_and_pauses(self):
        self.start()
        self.ticks()
        self.seed_terminal_no_fill_history(60)
        limits = deepcopy(engine.LIMITS)
        previous_ids = [o["request"]["clOrdId"] for o in self.bot["_orders"]]
        ledger = self.fx.lab.ledgers[self.state["context"]]
        billing_before = deepcopy(ledger.summary(self.state["id"]))
        self.stop()
        self.ticks(2)
        self.assertNotEqual(self.state["status"], "DONE")
        self.assertEqual(self.fake.units, Decimal(".02"))
        self.fx.now += engine.LIMITS["recovery_seconds"] + 1
        self.ticks()
        self.assertEqual(self.state["status"], "PAUSED")
        self.assertTrue(self.state["incomplete"])
        self.assertTrue(self.state["error"])
        self.assertFalse(self.bot["accounting_reconciled"])
        self.assertNotEqual(self.bot["status"], "STOPPED")
        self.assertEqual(self.fake.units, Decimal(".02"))
        self.assertEqual(Decimal(self.bot["_contracts"]), Decimal(".02"))
        self.assertEqual([o["request"]["clOrdId"] for o in self.bot["_orders"]], previous_ids)
        self.assertEqual(len(self.posts("/api/v5/trade/order")), 1, "hard cap forbids a 61st order, including close")
        self.assertEqual(engine.LIMITS, limits)
        self.assertEqual(ledger.summary(self.state["id"]), billing_before)
        self.no_new_entry()

    def test_T05_unknown_entry_ack_survives_fresh_manager_restart_without_new_entry(self):
        fake = self.fx.fakes["demo"]
        fake.lost_ack, fake.lookup_broken = True, True
        self.start()
        self.ticks(4)
        self.assertGreater(fake.units, 0)
        self.assertTrue(engine.unresolved(self.bot))
        self.no_new_entry()
        self.fx.lab.close()
        fake.lookup_broken = False
        restarted = ExchangeLab(self.fx.root, self.fx.workshop, self.fx.arena,
            clock=self.fx.clock, monotonic=self.fx.clock, background=False,
            api_factory=lambda p: self.fx.fakes[p["id"]].api(p["environment"]),
            jev_factory=Mock(side_effect=AssertionError("Unknown ACK restart must not instantiate Jev")),
            feed_factory=lambda api: ExchangeFeed(api, clock=self.fx.clock))
        self.fx.lab = restarted
        restarted.resume(restarted.data["generation"], self.state["id"])
        self.state = restarted.load(self.state["id"])
        self.bot = self.state["bots"][0]
        restarted.prepare(self.state, restarted.chosen_profiles(["demo"]), True)
        self.ticks(5)
        self.flat_settled()
        self.assertEqual(self.bot["ai_requests"], 0)

    def test_T06_exhausted_jev_unknown_billing_closes_without_reset_or_model(self):
        self.start()
        self.ticks()
        self.bot["ai_requests"] = 61
        ledger = self.fx.lab.ledgers[self.state["context"]]
        reservation = ledger.reserve(self.state["id"], "fictional-unknown", self.fx.now)
        ledger.finish(reservation, None)
        before = deepcopy(ledger.summary(self.state["id"]))
        self.stop()
        self.ticks(4)
        self.flat_settled()
        self.assertEqual(self.bot["ai_requests"], 61)
        self.assertEqual(ledger.summary(self.state["id"]), before)
        self.assertFalse(self.bot["ai_cost_complete"])
        self.assertTrue(self.state["incomplete"])

    def test_T06_expired_admission_and_no_market_snapshot_do_not_veto_stop(self):
        self.start()
        self.ticks()
        resource = self.fx.lab.resources[self.state["id"]]
        resource["feed"].snapshot = Mock(side_effect=PreflightError("market_data_unavailable"))
        self.fx.now = self.state["_end"] + 1
        self.stop()
        self.ticks(4)
        self.flat_settled()

    def test_T07_foreign_position_blocks_blind_close_and_requires_manual_review(self):
        self.start()
        self.ticks()
        self.fake.units += Decimal(".01")
        self.stop()
        self.ticks()
        self.assertTrue(self.bot["_manual_review"])
        self.assertEqual(len(self.posts("/api/v5/trade/order")), 1)
        self.assertEqual(self.fake.units, Decimal(".03"))
        self.fx.now += 121
        self.ticks()
        self.assertEqual(self.state["status"], "PAUSED")
        self.assertFalse(self.bot["accounting_reconciled"])
        self.no_new_entry()

    def test_T07_position_unknown_offline_keeps_residual_and_bounded_visible_pause(self):
        self.start()
        self.ticks()
        api = self.fx.lab.resources[self.state["id"]]["apis"][0]
        api.position = Mock(side_effect=PreflightError("network_error"))
        self.stop()
        self.ticks(3)
        self.assertEqual(len(self.posts("/api/v5/trade/order")), 1)
        self.assertNotEqual(self.state["status"], "DONE")
        self.fx.now += 121
        self.ticks()
        self.assertEqual(self.state["status"], "PAUSED")
        self.assertTrue(self.state["error"])
        self.assertGreater(self.fake.units, 0)
        self.assertFalse(self.bot["accounting_reconciled"])
        self.no_new_entry()

    def test_T08_rejected_amend_retains_confirmed_old_stop_and_pending_desired(self):
        helper = protection_fixture.ProtectionIntegrationTests(methodName="runTest")
        lab, state, bot, api, fake = helper.fixture()
        protection.ensure(lab, state, bot, api)
        old_body = deepcopy(bot["_protections"][-1]["body"])
        old_parent = deepcopy(helper.parent(bot, fake))
        bot["_contracts"] = ".06"
        fake.reject_once.add(("POST", "/api/v5/trade/amend-algos"))
        try:
            protection.ensure(lab, state, bot, api)
        except (OSError, PreflightError):
            pass
        stored = bot["_protections"][-1]
        self.assertEqual(stored["body"], old_body)
        self.assertEqual(helper.parent(bot, fake), old_parent)
        self.assertEqual(fake.post_count("/api/v5/trade/cancel-algos"), 0)
        self.assertEqual(fake.post_count("/api/v5/trade/order-algo"), 1)
        self.assertFalse(bot["protection"]["verified"])
        self.assertEqual(Decimal(stored["pending_amend"]["sz"]), Decimal(".06"))

    def test_T08_unconfirmed_lost_amend_ack_keeps_old_intent_until_exact_new_receipt(self):
        helper = protection_fixture.ProtectionIntegrationTests(methodName="runTest")
        lab, state, bot, api, fake = helper.fixture()
        protection.ensure(lab, state, bot, api)
        old_body = deepcopy(bot["_protections"][-1]["body"])
        old_receipt = deepcopy(bot["_protections"][-1]["receipt"])
        bot["_contracts"] = ".06"
        fake.lost_once.add(("POST", "/api/v5/trade/amend-algos"))
        original = api.transport
        def old_observed(request):
            if request.get_method() == "GET" and urlsplit(request.full_url).path == "/api/v5/trade/order-algo":
                fake.requests.append(request)
                return {"code": "0", "data": [deepcopy(old_receipt)]}
            return original(request)
        api.transport = old_observed
        try:
            protection.ensure(lab, state, bot, api)
        except (OSError, PreflightError):
            pass
        self.assertEqual(bot["_protections"][-1]["body"], old_body)
        self.assertIn("pending_amend", bot["_protections"][-1])
        self.assertFalse(bot["protection"]["verified"])
        self.assertEqual(fake.post_count("/api/v5/trade/amend-algos"), 1)
        self.assertEqual(fake.post_count("/api/v5/trade/cancel-algos"), 0)
        api.transport = original
        # A safety fix may have latched recovery. Resolve the persisted receipt
        # directly; proving the amendment does not cancel a legitimate Stop.
        _, proof = protection.observe(api, bot["_protections"][-1])
        self.assertTrue(proof["live_verified"])
        self.assertNotIn("pending_amend", bot["_protections"][-1])
        self.assertEqual(Decimal(bot["_protections"][-1]["body"]["sz"]), Decimal(".06"))
        self.assertEqual(fake.post_count("/api/v5/trade/amend-algos"), 1)


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.records.append({"test": test.id(), "status": "PASS"})

    def addFailure(self, test, error):
        super().addFailure(test, error)
        self.records.append({"test": test.id(), "status": "FAIL", "detail": self._exc_info_to_string(error, test)})

    def addError(self, test, error):
        super().addError(test, error)
        self.records.append({"test": test.id(), "status": "ERROR", "detail": self._exc_info_to_string(error, test)})


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(P0ProtectionFaultTests)
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
    destination = os.environ.get("P0_RESULT_PATH")
    if destination:
        destination = Path(destination).resolve()
        if not destination.is_relative_to(QA_SCOPE):
            raise RuntimeError("Result writes must remain inside explicit QA scope")
        pins_after = {p: hashlib.sha256((TARGET / p).read_bytes()).hexdigest() for p in SOURCE_PATHS}
        receipt = {"schema": "p0-independent-fault-qa/v1", "target_repo": str(TARGET),
                   "evidence_kind": "SYNTHETIC_FAULT_FIXTURES_ONLY", "tests_run": result.testsRun,
                   "passed": result.testsRun - len(result.failures) - len(result.errors),
                   "failures": len(result.failures), "errors": len(result.errors), "records": result.records,
                   "source_pins": pins_after, "source_pins_before_import": PINS_BEFORE_IMPORT,
                   "source_drift": pins_after != PINS_BEFORE_IMPORT,
                   "suite_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                   "network": "SOCKET_BLOCKED_FAKE_TRANSPORT_ONLY", "production_actions": 0, "paid_calls": 0}
        destination.write_text(json.dumps(receipt, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
    raise SystemExit(0 if result.wasSuccessful() else 1)
