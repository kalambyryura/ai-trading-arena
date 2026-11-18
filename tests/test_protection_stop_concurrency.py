"""Event-gated F02 regression, fake transport/disposable roots only; no sleeps."""
from copy import deepcopy
from decimal import Decimal
import json
import socket
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import test_rules_only_exchange as rules_fixture
from trading_lab import exchange_engine as engine, exchange_protection as protection
from trading_lab import match_journal
from trading_lab.arena_engine import emit
from trading_lab.okx_readonly import PreflightError

WAIT = 15  # Deadlock guard only; elapsed milliseconds are never a pass condition.
OBSERVATIONS = []

class ProbeRLock:
    def __init__(self):
        self.raw = threading.RLock()
        self.probed = threading.Event()
        self.stop_acquired = None
    def acquire(self, *args, **kwargs):
        if threading.current_thread().name == "F02-Stop" and not self.probed.is_set():
            acquired = self.raw.acquire(False)
            self.stop_acquired = acquired
            self.probed.set()
            return True if acquired else self.raw.acquire(*args, **kwargs)
        return self.raw.acquire(*args, **kwargs)
    def release(self):
        self.raw.release()
    def __enter__(self):
        self.acquire(); return self
    def __exit__(self, *unused):
        self.release()

class DelayedTransport:
    def __init__(self, original, method, path, *, lost_ack=False):
        self.original, self.method, self.path = original, method, path
        self.entered, self.release = threading.Event(), threading.Event()
        self.once, self.lost_ack = False, lost_ack
    def __call__(self, request):
        if not self.once and request.get_method() == self.method and urlsplit(request.full_url).path == self.path:
            self.once = True
            result = self.original(request)  # effect/receipt exists before ACK is delayed
            self.entered.set()
            if not self.release.wait(WAIT):
                raise AssertionError("test transport release guard expired")
            if self.lost_ack:
                raise OSError("fictional lost protective ACK after acceptance")
            return result
        return self.original(request)

class F02Concurrency(unittest.TestCase):
    def setUp(self):
        self.net_patch = patch.object(socket.socket,"connect",side_effect=AssertionError("Actual network forbidden"))
        self.net_patch.start(); self.addCleanup(self.net_patch.stop)
        self.fx = rules_fixture.RulesOnlyTests(methodName="runTest")
        self.fx.setUp(); self.addCleanup(self.fx.doCleanups)
        self.state = self.fx.start()
        self.lab, self.bot, self.fake = self.fx.fx.lab, self.fx.bot, self.fx.fake
        self.api = self.lab.resources[self.state["id"]]["apis"][0]
        self.lock = ProbeRLock(); self.lab.lock = self.lock

    def first_fill(self):
        self.lab.step(self.state)
        self.assertGreater(abs(self.fake.units),0)
        self.assertTrue(self.bot["protection"]["verified"])

    def count_posts(self, path):
        return sum(r.get_method()=="POST" and urlsplit(r.full_url).path==path for r in self.fake.requests)

    def pending_stop(self, target, delay, label):
        errors, results = [], []
        done, stopped = threading.Event(), threading.Event()
        def work():
            try: results.append(target())
            except BaseException as error: errors.append(error)
            finally: done.set()
        def stop():
            try: self.lab.stop(self.lab.data["generation"],self.state["id"])
            except BaseException as error: errors.append(error)
            finally: stopped.set()
        worker = threading.Thread(target=work,name="F02-worker")
        stopper = threading.Thread(target=stop,name="F02-Stop")
        worker.start()
        self.assertTrue(delay.entered.wait(WAIT),"fake I/O never entered")
        stopper.start()
        observation = {"scenario":label}
        try:
            self.assertTrue(self.lock.probed.wait(WAIT),"Stop lock probe not reached")
            observation["stop_lock_available_while_transport_pending"] = self.lock.stop_acquired
            self.assertTrue(self.lock.stop_acquired,"F02: protection I/O owns state lock; Stop cannot latch before release")
            self.assertTrue(stopped.wait(WAIT),"Stop did not complete independently of transport")
            self.assertFalse(delay.release.is_set())
            saved = json.loads((self.lab.path/(self.state["id"]+".json")).read_text(encoding="utf-8"))
            self.assertTrue(saved["bots"][0]["stop_requested"])
            self.assertEqual(saved["status"],"CLOSING")
            observation["durable_stop_before_transport_release"] = True
        finally:
            delay.release.set()
            worker.join(WAIT); stopper.join(WAIT)
            self.assertFalse(worker.is_alive() or stopper.is_alive(),"thread cleanup guard")
            observation["transport_released_after_probe"] = True
            observation["worker_errors"] = [type(e).__name__+":"+str(e) for e in errors]
            OBSERVATIONS.append(observation)
        return errors, results

    def no_new_risk_then_close(self, expected_protection_posts=1):
        old = deepcopy(next(o["request"] for o in self.bot["_orders"] if not o["request"]["reduceOnly"]))
        before = len(self.fake.orders)
        for kind in ("entry","add"):
            old["clOrdId"] = self.lab.identity_for(self.state,self.bot,"E")
            self.assertFalse(self.lab.submit(self.state,self.bot,self.api,old,self.fx.fx.now),kind)
        self.assertEqual(len(self.fake.orders),before)
        for _ in range(6):
            self.fx.fx.now += 5
            self.lab.step(self.state)
            if self.state["status"] == "DONE": break
        self.assertEqual(self.fake.units,0)
        self.assertEqual(Decimal(self.bot["_contracts"]),0)
        self.assertEqual(self.state["status"],"DONE")
        self.assertTrue(self.bot["accounting_reconciled"])
        self.assertEqual(self.bot["protection"]["status"],"FLAT")
        self.assertEqual(self.count_posts("/api/v5/trade/order-algo"),expected_protection_posts)
        self.fx.fx.lab.jev_factory.assert_not_called()

    def placement_case(self,lost):
        delay = DelayedTransport(self.api.transport,"POST","/api/v5/trade/order-algo",lost_ack=lost)
        self.api.transport = delay
        errors,_ = self.pending_stop(lambda:self.lab.step(self.state),delay,"placement_lost_ack" if lost else "post_fill_placement")
        self.assertEqual(errors,[])
        self.assertEqual(sum(not o["reduceOnly"] for o in self.fake.orders.values()),1)
        self.no_new_risk_then_close()

    def test_post_fill_protection_post_does_not_block_durable_stop(self): self.placement_case(False)
    def test_lost_protection_ack_no_duplicate_after_stop(self): self.placement_case(True)

    def test_lost_entry_ack_after_stop_uses_current_recovery_writer(self):
        original = self.api.transport
        def effect_then_exhaust_normal(request):
            result = original(request)
            if request.get_method() == "POST" and urlsplit(request.full_url).path == "/api/v5/trade/order":
                journal = match_journal.segmented(self.lab.path / (self.state["id"] + ".events.jsonl"))
                journal.total_limit = journal.total_used + journal.critical_reserve + 16
                emit(self.state, self.fx.fx.now, self.bot, 1, "fixture_pending_telemetry",
                     "Fictional normal quota exhausted after durable send.")
            return result
        delay = DelayedTransport(effect_then_exhaust_normal, "POST", "/api/v5/trade/order", lost_ack=True)
        self.api.transport = delay
        errors, _ = self.pending_stop(lambda: self.lab.step(self.state), delay,
                                      "lost_entry_ack_stop_and_quota")
        self.assertEqual(errors, [])
        self.assertNotEqual(self.state["status"], "PAUSED", "Current Stop must retain reconcile/close reserve")
        self.assertIsNotNone(self.bot["_orders"][0]["result"], "Already-sent entry must reach GET reconciliation")
        self.api.transport = original
        self.no_new_risk_then_close(expected_protection_posts=0)

    def test_checked_account_reconcile_get_does_not_block_stop(self):
        self.first_fill()
        delay = DelayedTransport(self.api.transport,"GET","/api/v5/trade/order-algo")
        self.api.transport = delay
        errors,_ = self.pending_stop(lambda:self.lab.checked_account(self.state,self.bot,self.api),delay,"reconcile_get")
        self.assertEqual(errors,[])
        self.no_new_risk_then_close()

    def test_stop_during_protection_io_also_drains_quota_blocked_prefix(self):
        self.first_fill()
        path = self.lab.path / (self.state["id"] + ".events.jsonl")
        journal = match_journal.segmented(path)
        journal.total_limit = journal.total_used + journal.critical_reserve + 16
        emit(self.state, self.fx.fx.now, self.bot, 1, "fixture_pending_telemetry",
             "Fictional ordinary event before Stop.")
        prefix = deepcopy(self.state["events"])
        delay = DelayedTransport(self.api.transport, "GET", "/api/v5/trade/order-algo")
        self.api.transport = delay
        errors, _ = self.pending_stop(
            lambda: self.lab.checked_account(self.state, self.bot, self.api),
            delay, "combined_quota_and_protection_delay")
        self.assertEqual(errors, [])
        self.no_new_risk_then_close()
        restored = deepcopy(self.state)
        match_journal.restore_events(path, restored, full=True)
        self.assertEqual(restored["events"][:len(prefix)], prefix)
        self.assertEqual([e["seq"] for e in restored["events"]],
                         list(range(1, len(restored["events"]) + 1)))

    def test_add_fill_pending_sl_resize_stop_vetoes_later_amend(self):
        self.first_fill()
        body = deepcopy(self.bot["_orders"][0]["request"])
        body["clOrdId"] = self.lab.identity_for(self.state,self.bot,"E")
        delay = DelayedTransport(self.api.transport,"GET","/api/v5/trade/order-algo")
        self.api.transport = delay
        errors,_ = self.pending_stop(lambda:self.lab.submit(self.state,self.bot,self.api,body,self.fx.fx.now),delay,"add_fill_pending_resize")
        self.assertEqual(errors,[])
        self.assertEqual(sum(not o["reduceOnly"] for o in self.fake.orders.values()),2)
        self.assertEqual(self.count_posts("/api/v5/trade/amend-algos"),0)
        self.no_new_risk_then_close()

    def test_stop_during_already_submitted_amend_retains_owned_receipt(self):
        self.first_fill()
        body = deepcopy(self.bot["_orders"][0]["request"])
        body["clOrdId"] = self.lab.identity_for(self.state,self.bot,"E")
        delay = DelayedTransport(self.api.transport,"POST","/api/v5/trade/amend-algos")
        self.api.transport = delay
        errors,_ = self.pending_stop(lambda:self.lab.submit(self.state,self.bot,self.api,body,self.fx.fx.now),delay,"amend_ack_pending")
        self.assertEqual(errors,[])
        self.assertEqual(self.count_posts("/api/v5/trade/amend-algos"),1)
        self.assertNotIn("pending_amend",self.bot["_protections"][-1])
        self.no_new_risk_then_close()

    def test_changed_foreign_sl_server_id_is_not_adopted_or_closed(self):
        self.first_fill()
        parent = self.fake.algos[self.bot["_protections"][-1]["body"]["algoClOrdId"]]
        parent["algoId"] = "999999"
        delay = DelayedTransport(self.api.transport,"GET","/api/v5/trade/order-algo")
        self.api.transport = delay
        before = len(self.fake.orders)
        errors,_ = self.pending_stop(lambda:self.lab.checked_account(self.state,self.bot,self.api),delay,"foreign_parent_identity")
        self.assertEqual(len(errors),1)
        self.assertIsInstance(errors[0],PreflightError)
        self.assertIn("mismatch",str(errors[0]))
        self.assertNotIn("999999",self.api.owned_algos)
        self.lab.step(self.state)
        self.assertTrue(self.bot["_manual_review"])
        self.assertEqual(len(self.fake.orders),before)
        self.assertNotEqual(self.fake.units,0)

    def child_fill(self):
        stored = self.bot["_protections"][-1]
        parent = self.fake.algos[stored["body"]["algoClOrdId"]]
        quantity = Decimal(".01")
        owned = abs(self.fake.units)
        self.assertGreater(owned,quantity)
        side = "sell" if self.fake.units>0 else "buy"
        price = self.fake.bid if side=="sell" else self.fake.ask
        sign = Decimal(1) if self.fake.units>0 else Decimal(-1)
        gross = quantity*Decimal(".01")*(price-self.fake.basis)*sign
        fee = -quantity*Decimal(".01")*price*Decimal(".0005")
        identity = "990001"
        row = {"instId":"BTC-USDT-SWAP","tdMode":"isolated","posSide":"net","side":side,
            "ordType":"market","reduceOnly":True,"sz":str(owned),"clOrdId":"","ordId":identity,
            "algoId":parent["algoId"],"state":"canceled","accFillSz":str(quantity),"avgPx":str(price),
            "fee":str(fee),"feeCcy":"USDT","pnl":str(gross),"rebate":"0","uTime":str(int(self.fx.fx.now*1000))}
        self.fake.orders["fictional-sl-child"] = row
        self.fake.units -= sign*quantity
        self.fake.cash += gross+fee
        self.fake.bills.append({"billId":str(len(self.fake.bills)+1),"ts":str(int(self.fx.fx.now*1000)),"type":"2","subType":"3","ccy":"USDT","instId":"BTC-USDT-SWAP","ordId":identity,"pnl":str(gross),"fee":str(fee),"balChg":str(gross+fee),"posBalChg":"0"})
        parent.update(state="effective",ordIdList=[identity])

    def test_delayed_owned_sl_child_reconciles_once_then_closes_remaining(self):
        self.first_fill(); self.child_fill()
        delay = DelayedTransport(self.api.transport,"GET","/api/v5/trade/order-algo")
        self.api.transport = delay
        errors,_ = self.pending_stop(lambda:self.lab.checked_account(self.state,self.bot,self.api),delay,"owned_partial_child")
        self.assertEqual(errors,[])
        self.lab.checked_account(self.state,self.bot,self.api)
        self.assertEqual(sum(o.get("_child_id")=="990001" for o in self.bot["_orders"]),1)
        self.assertEqual(Decimal(self.bot["_contracts"]),self.fake.units)
        self.no_new_risk_then_close()

    def test_foreign_pending_remains_veto_after_delayed_protection_read(self):
        self.first_fill(); self.fake.foreign_pending = True
        delay = DelayedTransport(self.api.transport,"GET","/api/v5/trade/order-algo")
        self.api.transport = delay
        before = len(self.fake.orders)
        errors,_ = self.pending_stop(lambda:self.lab.checked_account(self.state,self.bot,self.api),delay,"foreign_pending")
        self.assertEqual(len(errors),1)
        self.assertIsInstance(errors[0],PreflightError)
        self.assertIn("foreign",str(errors[0]))
        self.lab.step(self.state)
        self.assertTrue(self.bot["_manual_review"])
        self.assertEqual(len(self.fake.orders),before)
        self.assertNotEqual(self.fake.units,0)
        self.assertFalse(self.bot["accounting_reconciled"])

if __name__ == "__main__":
    unittest.main()
