"""Operating-mode scope, races and truthful evidence on temp/fake exchanges only."""
from copy import deepcopy
from decimal import Decimal
import http.client
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock

import test_exchange_lab as fixture
from trading_lab import demo_execution as policy
from trading_lab import exchange_engine as engine
from trading_lab.arena_strategies import ArenaError
from trading_lab.demo_v8_review import review
from trading_lab.match_journal import restore_events
from trading_lab.web_panel import Panel, Server


class DemoMechanicsTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.ExchangeTests(methodName="runTest")
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        payload = json.loads((Path(__file__).resolve().parents[1] / "03_For_Client_Deliverables/V8_DEMO_SHORT_PARTIAL_TAIL.json").read_text(encoding="utf-8"))
        self.identity = self.fx.workshop.add_version(self.fx.workshop.data["generation"], payload)["id"]
        self.fx.lab.jev_factory = Mock(side_effect=AssertionError("No model construction in the operating check"))
        (self.fx.root / "web").mkdir(exist_ok=True)
        (self.fx.root / "web/workshop.html").write_bytes((Path(__file__).resolve().parents[1] / "web/workshop.html").read_bytes())

    def start(self):
        lab = self.fx.lab
        result = lab.start(lab.data["generation"], "workshop_demo", [self.identity], ["demo"], 100, 300,
                           execution_mode=policy.MODE)
        state = lab.load(result["id"])
        state.update(fixture_only=True, evidence_kind="SYNTHETIC")
        lab.prepare(state, lab.chosen_profiles(["demo"]), False)
        self.state, self.bot, self.fake = state, state["bots"][0], self.fx.fakes["demo"]
        return state

    def step(self, seconds=0):
        self.fx.now += seconds
        self.fx.lab.step(self.state)

    def full_state(self):
        state = deepcopy(self.fx.lab.load(self.state["id"]))
        restore_events(self.fx.lab.path / (state["id"] + ".events.jsonl"), state, full=True)
        return state

    def test_full_operating_check_without_model_or_ledger_reservation(self):
        before = self.fx.lab.ledgers["workshop_demo"].summary("")
        self.start()
        self.step()
        self.assertLess(self.bot["position_btc"], 0)
        for _ in range(88): self.step(5)
        state = self.full_state()
        report = review(state)
        self.assertEqual(state["status"], "DONE")
        self.assertTrue(report["all_primary_observed"], report["rows"])
        settled = [e["seq"] for e in state["events"] if e["code"] == "accounting_confirmed"][-1]
        self.assertEqual(next(r["event_seq"] for r in report["rows"] if r["id"] == "flat_and_settled"), settled)  # D-3: the final reconciliation
        self.assertEqual(report["evidence_kind"], "SYNTHETIC")
        self.assertEqual(report["execution_mode"], policy.MODE)
        self.assertEqual(self.bot["ai_requests"], 0)
        self.assertEqual(self.bot["ai_cost_usd"], 0)
        self.fx.lab.jev_factory.assert_not_called()
        self.assertIsNone(self.fx.lab.resources[state["id"]]["gate"])
        self.assertFalse(any(e["code"] in {"jev_request", "jev_decision"} for e in state["events"]))
        self.assertTrue(any(e["code"] == "demo_execution_review" for e in state["events"]))
        self.assertEqual(before, self.fx.lab.ledgers["workshop_demo"].summary(""))
        orders = list(self.fake.orders.values())
        self.assertEqual(sum(not o["reduceOnly"] for o in orders), 1)
        self.assertTrue(all(o["reduceOnly"] for o in orders[1:]))
        public = self.fx.lab.history(state["id"])
        self.assertTrue(public["demo_operating_review"]["all_primary_observed"])
        stages = public["lifecycle"]["bots"][0]["rows"]
        self.assertFalse(next(s for s in stages if s["id"] == "jev_entry")["required"])
        self.assertEqual(self.fx.lab.status()["history"][0]["execution_mode"], policy.MODE)

    def test_a_slow_demo_account_api_still_opens_the_short(self):
        """Owner Demo 2026-10-31 (a76a80d5…): ~2-3 s a private request made every entry 'decision_snapshot_too_old'
        under R1 full.1. Account checks now come before the fresh quote, so the short reaches the exchange."""
        self.start()
        for api in self.fx.lab.resources[self.state["id"]]["apis"]:
            original = api.transport
            def transport(request, original=original):
                path = request.full_url.split("://", 1)[-1].split("/", 1)[-1].split("?")[0]
                if request.get_method() == "GET" and path.startswith(("api/v5/account/", "api/v5/trade/")):
                    self.fx.now += 3.0
                return original(request)
            api.transport = transport
        self.step()
        self.assertLess(self.bot["position_btc"], 0)
        state = self.full_state()
        rechecked = next(e for e in state["events"] if e["code"] == "entry_rechecked")["data"]
        self.assertLess(rechecked["decision_age_seconds"], 5, "the decision snapshot follows the ~27 s account checks")
        self.assertLessEqual(rechecked["fresh_age_seconds"], 10)
        self.assertFalse(any(e["code"] == "entry_cancelled" for e in state["events"]))

    def test_the_measured_demo_profile_of_round_a76a80d5_opens_the_short(self):
        """Owner Demo a76a80d5 under R1 full.1: 4 signals, 0 orders. Its measured latency (~1.7 s a private GET,
        ~0.5 s a public GET) on the fake transport: the short reaches the exchange and the run completes."""
        self.start()
        resource = self.fx.lab.resources[self.state["id"]]
        apis = list(resource["apis"]) + [getattr(resource["feed"], "api", None)]
        for api in {id(a): a for a in apis if a is not None}.values():
            original = api.transport
            def transport(request, original=original):
                path = request.full_url.split("://", 1)[-1].split("/", 1)[-1].split("?")[0]
                if request.get_method() == "GET":
                    self.fx.now += 1.7 if path.startswith(("api/v5/account/", "api/v5/trade/")) else 0.5
                return original(request)
            api.transport = transport
        self.step()
        self.assertLess(self.bot["position_btc"], 0)
        state = self.full_state()
        rechecked = next(e for e in state["events"] if e["code"] == "entry_rechecked")["data"]
        self.assertLessEqual(max(rechecked["decision_age_seconds"], rechecked["fresh_age_seconds"],
                                 rechecked["account_check_age_seconds"]), 10)
        self.assertFalse(any(e["code"] == "entry_cancelled" for e in state["events"]))

    def test_scope_rejected_before_reservation_or_exchange_access(self):
        calls = Mock(side_effect=AssertionError("Invalid scope must not read exchange"))
        self.fx.lab.api_factory = calls
        lab = self.fx.lab
        for context, ids, accounts, budget, duration, mode in (
                ("arena_live", [self.identity], ["arena-a"], 100, 300, policy.MODE),
                ("workshop_demo", [self.fx.version], ["demo"], 100, 300, policy.MODE),
                ("workshop_demo", [self.identity], ["demo"], 100, 900, policy.MODE),
                ("workshop_demo", [self.identity], ["demo"], 200, 300, policy.MODE),
                ("workshop_demo", [self.identity], ["arena-a"], 100, 300, policy.MODE),
                ("workshop_demo", [self.identity], ["demo"], 100, 300, "rules")):
            with self.subTest(context=context, ids=ids, budget=budget, duration=duration, mode=mode):
                with self.assertRaises(ArenaError):
                    lab.start(lab.data["generation"], context, ids, accounts, budget, duration, execution_mode=mode)
        self.assertFalse(lab.data["runs"])
        calls.assert_not_called()

    def test_unknown_paid_billing_still_blocks_normal_and_is_never_reset(self):
        lab = self.fx.lab
        ledger = lab.ledgers["workshop_demo"]
        reservation = ledger.reserve("old-owner-run", "unknown", self.fx.now)
        ledger.finish(reservation, None)
        before = ledger.summary("")
        with self.assertRaisesRegex(ArenaError, "бюджет"):
            lab.start(lab.data["generation"], "workshop_demo", [self.identity], ["demo"], 100, 300)
        self.start()
        self.step()
        self.assertLess(self.bot["position_btc"], 0)
        self.assertEqual(before, ledger.summary(""))
        self.fx.lab.jev_factory.assert_not_called()

    def test_final_fresh_conditions_still_veto_entry(self):
        self.start()
        feed = self.fx.lab.resources[self.state["id"]]["feed"]
        original = feed.snapshot
        count = 0
        def changed():
            nonlocal count
            count += 1
            value = original()
            if count > 1: value["features"]["spread_bps"] = 2
            return value
        feed.snapshot = changed
        self.step()
        self.assertFalse(self.fake.orders)
        self.assertEqual(self.bot["position_btc"], 0)
        # Demo 4a6e4af6 (R2.1.4): a wider fresh spread is the ordinary step-1 cancel with its checks, not the
        # step-3 "checks unfinished" fault with demo_mechanics_entry_scope
        cancel = [e for e in self.state["events"] if e["code"] == "entry_cancelled"][-1]
        self.assertEqual(cancel["step"], 1)
        self.assertNotIn("demo_mechanics_entry_scope", cancel["data"]["reasons"])
        self.assertIn("spread_bps", [c["feature"] for c in cancel["data"]["checks"] if c.get("passed") is False])

    def test_owner_stop_during_final_check_prevents_entry(self):
        self.start()
        feed = self.fx.lab.resources[self.state["id"]]["feed"]
        original = feed.snapshot
        count = 0
        def stopped():
            nonlocal count
            count += 1
            if count > 1:
                self.fx.lab.stop(self.fx.lab.data["generation"], self.state["id"])
            return original()
        feed.snapshot = stopped
        self.step()
        self.assertFalse(self.fake.orders)
        self.assertEqual(self.bot["ai_requests"], 0)

    def test_cutoff_never_creates_first_entry(self):
        self.start()
        self.step(305)
        self.assertFalse(self.fake.orders)
        self.assertFalse(review(self.full_state())["all_primary_observed"])

    def test_owner_stop_closes_own_fill_without_model(self):
        self.start()
        self.step()
        self.step(5)
        self.fx.lab.stop(self.fx.lab.data["generation"], self.state["id"])
        self.step(5)
        rows = {r["id"]: r["status"] for r in review(self.full_state())["rows"]}
        self.assertEqual(rows["owner_stop_close"], "observed")
        self.assertEqual(rows["authored_full_exit"], "not_observed")
        self.assertEqual(self.bot["ai_requests"], 0)
        self.assertEqual(len(self.fake.orders), 2)

    def test_recovery_closes_only_existing_owned_position_without_model(self):
        self.start()
        self.step()
        self.step(5)
        engine.stop(self.state, self.fx.now, reason="Відновлення після перерви: лише звірка й закриття.")
        self.state.update(_recovery=True, incomplete=True)
        self.fx.lab.resources[self.state["id"]]["apis"][0].close_only = True
        self.step(5)
        rows = {r["id"]: r["status"] for r in review(self.full_state())["rows"]}
        self.assertEqual(rows["recovery_close_only"], "observed")
        self.assertEqual(len(self.fake.orders), 2)
        self.assertEqual(self.bot["ai_requests"], 0)
        self.assertTrue(self.state["incomplete"])

    def test_manifest_tampering_blocks_prepare_before_exchange_access(self):
        self.start()
        altered = deepcopy(self.state)
        altered["context"] = "arena_live"
        calls = Mock(side_effect=AssertionError("No exchange call after scope tamper"))
        self.fx.lab.api_factory = calls
        with self.assertRaises(ArenaError):
            self.fx.lab.prepare(altered, self.fx.lab.chosen_profiles(["arena-a"]), False)
        calls.assert_not_called()
        altered = deepcopy(self.state)
        altered["match_manifest"]["repeat_cycles"] = True
        with self.assertRaises(ArenaError): policy.validate_state(altered)

    def test_lost_ack_is_reconciled_without_second_entry(self):
        self.start()
        self.fake.lost_ack = True
        self.step()
        self.step(35)
        self.assertEqual(sum(not o["reduceOnly"] for o in self.fake.orders.values()), 1)
        self.assertTrue(any(e["code"] == "order_ack_uncertain" for e in self.state["events"]))

    def test_zero_fill_uses_one_entry_attempt_without_retries(self):
        self.start()
        self.fake.entry_fraction = 0
        self.step()
        self.step(35)
        self.step(35)
        self.assertEqual(self.bot["_entry_attempts"], 1)
        self.assertEqual(len(self.fake.orders), 1)
        self.assertEqual(self.bot["position_btc"], 0)

    def test_http_accepts_explicit_demo_mode_and_rejects_live_mode(self):
        server = Server(Panel(self.fx.root, launcher=lambda *a, **k: None), 0, arena=self.fx.arena,
                        exchange_factory=lambda *a: self.fx.lab)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
            headers = {"Content-Type": "application/json", "Origin": server.origin, "X-Panel-Token": server.panel.token}
            body = {"generation": self.fx.lab.data["generation"], "context": "workshop_demo",
                    "strategy_ids": [self.identity], "account_ids": ["demo"], "budget_usdt": 100,
                    "duration_seconds": 300, "execution_mode": policy.MODE}
            conn.request("POST", "/api/exchange/start", json.dumps(body), headers)
            response = conn.getresponse()
            self.assertEqual(response.status, 202, response.read())
            body.update(generation=self.fx.lab.data["generation"], context="arena_live", account_ids=["arena-a"])
            conn.request("POST", "/api/exchange/start", json.dumps(body), headers)
            response = conn.getresponse()
            self.assertEqual(response.status, 409, response.read())
            conn.close()
            self.assertEqual(len(self.fx.lab.data["runs"]), 1)
            self.fx.lab.jev_factory.assert_not_called()
        finally:
            server.shutdown(); thread.join(2); server.server_close()

    def test_pending_protection_never_breaks_panel_state_or_claims_verified_sl(self):
        self.start()
        self.step()
        record = self.bot["_protections"][0]
        record.update(receipt=None, proof=None)
        self.bot["protection"] = {"status": "PLACEMENT_PENDING", "verified": False}
        self.fx.lab.save(self.state)
        for receipt in (None, [], "pending"):
            with self.subTest(receipt=receipt):
                pending = self.full_state()
                pending["bots"][0]["_protections"][0]["receipt"] = receipt
                report = review(pending)
                self.assertFalse(report["all_primary_observed"])
                self.assertNotEqual(next(r["status"] for r in report["rows"]
                                         if r["id"] == "initial_hosted_sl"), "observed")
        server = Server(Panel(self.fx.root, launcher=lambda *a, **k: None), 0, arena=self.fx.arena,
                        exchange_factory=lambda *a: self.fx.lab)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
            for route in ("/api/state", "/api/exchange/state", "/api/workshop/state", "/workshop"):
                conn.request("GET", route)
                response = conn.getresponse()
                content = response.read()
                self.assertEqual(response.status, 200, (route, content))
                if route == "/api/state":
                    self.assertTrue(json.loads(content)["exchange_unfinished"])
            conn.close()
            self.fx.lab.jev_factory.assert_not_called()
        finally:
            server.shutdown(); thread.join(2); server.server_close()

    def test_interrupted_pending_stop_recovers_close_only_without_duplicate_protection(self):
        self.start()
        self.step()
        record = self.bot["_protections"][0]
        record.update(receipt=None, proof=None)
        self.bot["protection"] = {"status": "PLACEMENT_PENDING", "verified": False}
        self.fx.lab.save(self.state)
        self.fx.lab.resources.pop(self.state["id"])["stack"].close()
        result = self.fx.lab.resume(self.fx.lab.data["generation"], self.state["id"])
        self.assertTrue(result["close_only"])
        self.fx.lab.prepare(self.state, self.fx.lab.chosen_profiles(["demo"]), True)
        self.step(5)
        self.step(5)
        self.assertEqual(self.state["status"], "DONE")
        self.assertEqual(self.fake.units, 0)
        self.assertEqual(len(self.fake.algos), 1)
        self.assertEqual(len(self.fake.orders), 2)
        self.assertTrue(list(self.fake.orders.values())[-1]["reduceOnly"])
        self.assertTrue(self.bot["accounting_reconciled"])
        self.assertEqual(self.bot["ai_requests"], 0)
        self.fx.lab.jev_factory.assert_not_called()
        self.assertTrue(self.state["incomplete"])


class EthPostOnlyMechanicsTests(unittest.TestCase):
    """R2 (g): the pinned ETH post-only short in the mechanics mode, on a fake exchange whose post-only orders rest."""
    def setUp(self):
        from test_post_only import RestingFake
        self.fx = fixture.ExchangeTests(methodName="runTest")
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        for fake in self.fx.fakes.values():
            fake.__class__ = RestingFake
            fake.use(fixture.ETH_SPEC, 3000, 0)
        self.payload = json.loads((Path(__file__).resolve().parents[1] / "03_For_Client_Deliverables/R2_DEMO_ETH_POST_ONLY.json").read_text(encoding="utf-8"))
        self.identity = self.fx.workshop.add_version(self.fx.workshop.data["generation"], self.payload)["id"]
        self.fx.lab.jev_factory = Mock(side_effect=AssertionError("No model construction in the operating check"))
        self.fake = self.fx.fakes["demo"]

    def start(self):
        lab = self.fx.lab
        result = lab.start(lab.data["generation"], "workshop_demo", [self.identity], ["demo"], 100, 300, execution_mode=policy.MODE)
        self.state = lab.load(result["id"])
        self.state.update(fixture_only=True, evidence_kind="SYNTHETIC")
        lab.prepare(self.state, lab.chosen_profiles(["demo"]), False)
        self.bot = self.state["bots"][0]

    def step(self, seconds=5):
        self.fx.now += seconds
        self.fx.lab.step(self.state)

    def test_the_pinned_recipe_and_the_btc_scope_stay_exact(self):
        version = self.fx.workshop.version(self.identity)
        self.assertEqual(version["recipe_sha256"], policy.ETH_POST_ONLY_SHA256)
        self.assertEqual(policy.capability(), {"version": 1, "execution_mode": "demo_mechanics", "recipe_sha256": policy.RECIPE_SHA256,
                         "context": "workshop_demo", "mode": "OKX_DEMO", "budget_usdt": 100, "duration_seconds": 300,
                         "maximum_entry_attempts": 1, "uses_jev": False, "label": "Перевірка механіки · OKX Demo · без Jev"})
        self.assertEqual([c["recipe_sha256"] for c in self.fx.lab.status()["demo_execution_checks"]],
                         [policy.RECIPE_SHA256, policy.ETH_POST_ONLY_SHA256, policy.ETH_REST_SHA256, policy.ETH_LONG_SHA256])
        other = self.fx.workshop.add_version(self.fx.workshop.data["generation"], {**self.payload, "name": "ETH post-only, changed"})["id"]
        with self.assertRaises(ArenaError):
            self.fx.lab.start(self.fx.lab.data["generation"], "workshop_demo", [other], ["demo"], 100, 300, execution_mode=policy.MODE)

    def test_one_short_post_only_order_rests_fills_is_protected_and_settles(self):
        self.start()
        self.assertEqual(self.state["match_manifest"]["demo_execution_check"], policy.capability(policy.ETH_POST_ONLY_SHA256))
        for _ in range(60):
            self.step()
            if self.fake.orders:
                break
        identity, order = next(iter(self.fake.orders.items()))
        self.assertEqual((order["instId"], order["side"], order["ordType"]), ("ETH-USDT-SWAP", "sell", "post_only"))
        self.assertGreater(Decimal(order["px"]), self.fake.ask)  # passive: above the touch, 1 bps over the bar's close
        for _ in range(4):
            self.step()
        self.assertEqual(order["state"], "live")
        self.fake.fill(identity, order["sz"])
        self.step()
        self.assertLess(self.bot["position_btc"], 0)
        self.assertTrue(any(a["state"] == "live" and a["instId"] == "ETH-USDT-SWAP" for a in self.fake.algos.values()))
        for _ in range(80):  # the 300 s hold ends the cycle; no second order in this mode
            if self.state["status"] == "DONE":
                break
            self.step()
        self.assertEqual(self.state["status"], "DONE")
        self.assertEqual(sum(not o["reduceOnly"] for o in self.fake.orders.values()), 1)
        self.assertTrue(self.bot["accounting_reconciled"])
        self.fx.lab.jev_factory.assert_not_called()

    def use_recipe(self, name):
        payload = json.loads((Path(__file__).resolve().parents[1] / "03_For_Client_Deliverables" / name).read_text(encoding="utf-8"))
        self.identity = self.fx.workshop.add_version(self.fx.workshop.data["generation"], payload)["id"]

    def placed(self):
        for _ in range(60):
            self.step()
            if self.fake.orders:
                return next(iter(self.fake.orders.items()))
        self.fail("no order")

    def test_the_resting_recipe_waits_past_the_window_and_is_cancelled_at_its_timeout(self):
        """R2.1: a resting order read on the venue for several checks, still resting after the window, cancelled at the timeout."""
        self.use_recipe("R2_DEMO_ETH_POST_ONLY_REST.json")
        self.start()
        self.assertEqual(self.state["match_manifest"]["demo_execution_check"], policy.capability(policy.ETH_REST_SHA256))
        identity, order = self.placed()
        self.assertEqual((order["side"], order["ordType"]), ("sell", "post_only"))
        self.assertGreater(Decimal(order["px"]), self.fake.ask * Decimal("1.009"))  # 100 bps over the bar's close
        seen = []
        for _ in range(120):
            if self.state["status"] == "DONE":
                break
            self.step()
            seen.append((self.state["status"], order["state"]))
        self.assertIn(("DRAINING", "live"), seen)  # still resting after the admission window closed
        self.assertEqual((order["state"], self.state["status"]), ("canceled", "DONE"))
        self.assertEqual(len([e for e in self.state["events"] if e["code"] == "entry_order_expired"]), 1)
        self.assertEqual(len(self.fake.orders), 1)  # no second order after the timeout
        self.assertTrue(self.bot["accounting_reconciled"])

    def test_the_long_recipe_buys_below_the_touch_is_protected_and_settles(self):
        """R2.1: the buy side of the mechanics check (H10 is long)."""
        self.use_recipe("R2_DEMO_ETH_POST_ONLY_LONG.json")
        self.start()
        identity, order = self.placed()
        self.assertEqual((order["side"], order["ordType"]), ("buy", "post_only"))
        self.assertLess(Decimal(order["px"]), self.fake.bid)
        self.fake.fill(identity, order["sz"])
        self.step()
        self.assertGreater(self.bot["position_btc"], 0)
        self.assertTrue(any(a["state"] == "live" and a["side"] == "sell" for a in self.fake.algos.values()))  # the owned stop sells
        for _ in range(80):
            if self.state["status"] == "DONE":
                break
            self.step()
        self.assertEqual(self.state["status"], "DONE")
        self.assertEqual([o["side"] for o in self.fake.orders.values()], ["buy", "sell"])
        self.assertTrue(self.bot["accounting_reconciled"])
        lifecycle = json.dumps(self.fx.lab.history(self.state["id"])["lifecycle"], ensure_ascii=False)
        self.assertNotIn("RULES_ONLY", lifecycle)  # R2.1.1: a mechanics run is explained as one
        self.assertIn("Перевірка механіки OKX Demo", lifecycle)


if __name__ == "__main__": unittest.main()
