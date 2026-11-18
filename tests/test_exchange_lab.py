"""Exchange-backed lifecycle on a stateful fake transport; no network/key access."""
from copy import deepcopy
import base64
from decimal import Decimal
import hashlib
import hmac
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from trading_lab.arena import Arena
from trading_lab.arena_jev import SharedJevGate
from trading_lab.arena_market import build_snapshot
from trading_lab.arena_strategies import ArenaError, catalogue_defaults
from trading_lab.exchange_accounts import profiles, load_profile, public_profiles
from trading_lab.exchange_api import ExchangeAPI, ExchangeFeed, send
from trading_lab.exchange_lab import ExchangeLab, POLICIES
from trading_lab import exchange_engine as engine
from trading_lab.okx_readonly import Credentials, PreflightError, instrument_summary
from trading_lab.strategy_contract import payload_of
from trading_lab.workshop import Workshop
from trading_lab.web_panel import Server, Panel
from test_workshop import raw_public

SPEC = {"instType": "SWAP", "instId": "BTC-USDT-SWAP", "state": "live", "ctType": "linear",
        "ctVal": ".01", "ctMult": "1", "ctValCcy": "BTC", "settleCcy": "USDT",
        "lotSz": ".01", "minSz": ".01", "tickSz": ".1", "groupId": "4"}
ETH_SPEC = {**SPEC, "instId": "ETH-USDT-SWAP", "ctVal": ".1", "ctValCcy": "ETH", "tickSz": ".01"}


class FakeExchange:
    """Cumulative order responses and matching bills/cash, including lost ACKs."""
    def __init__(self, clock, uid="101", main="100"):
        self.clock, self.uid, self.main = clock, uid, main
        self.perm = "read_only,trade"  # R2.1.10 (D-1): a Live key with Withdraw is refused
        self.requests, self.orders, self.bills = [], {}, []
        self.algos = {}
        self.units, self.basis, self.cash = Decimal(0), Decimal(0), Decimal(5000)
        self.bid, self.ask = Decimal(84000), Decimal("84000.1")
        self.lost_ack = False
        self.entry_fraction = Decimal(1)
        self.close_fraction = Decimal(1)
        self.lookup_broken = False
        self.foreign_pending = False
        self.mode = "net_mode"
        self.funding_schedule_offset = 3600
        self.funding_data_age = 0
        self.spec, self.candle_base, self.candle_step = deepcopy(SPEC), 84000, 20
        # R2.1.12 (MIX): other contracts this fake's PUBLIC endpoints serve (a mixed season reads every contract through
        # its first bot's account); every request naming a contract it does not serve; strict: refused as OKX would
        self.markets, self.violations, self.strict = {}, [], False

    def use(self, spec, price, step):
        """Trade another contract (R2: one per season): its spec, rows and a market around `price`."""
        self.spec, self.candle_base, self.candle_step = deepcopy(spec), price, step
        self.bid, self.ask = Decimal(price), Decimal(price) + Decimal(spec["tickSz"])

    def serve(self, spec, price, step):
        """R2.1.12 (MIX): the public market of another contract beside this account's own (its spec, rows, quote)."""
        self.markets[spec["instId"]] = {"spec": deepcopy(spec), "bid": Decimal(price), "ask": Decimal(price) + Decimal(spec["tickSz"]),
                                        "candle_base": price, "candle_step": step}

    @staticmethod
    def named(request, query):
        """Every instId a request names, in its query or in its JSON body (a list for cancel-algos)."""
        names = set(query.get("instId", []))
        if request.data:
            body = json.loads(request.data)
            names.update(row["instId"] for row in (body if isinstance(body, list) else [body]) if isinstance(row, dict) and "instId" in row)
        return names

    def transport(self, request):
        self.requests.append(request)
        route = urlsplit(request.full_url); query = parse_qs(route.query)
        method, path = request.get_method(), route.path
        public = path.startswith(("/api/v5/market/", "/api/v5/public/"))
        foreign = sorted(n for n in self.named(request, query) if n != self.spec["instId"] and not (public and n in self.markets))
        if foreign:
            self.violations.append((method, path, foreign))
            if self.strict:
                return {"code": "51001", "msg": "Instrument ID does not exist", "data": []}
        market = self.markets.get((query.get("instId") or [None])[0]) if public else None
        spec, bid, ask = (market["spec"], market["bid"], market["ask"]) if market else (self.spec, self.bid, self.ask)
        candle_base, candle_step = (market["candle_base"], market["candle_step"]) if market else (self.candle_base, self.candle_step)
        rows = []
        if path == "/api/v5/public/time": rows = [{"ts": str(int(self.clock() * 1000))}]
        elif path in {"/api/v5/public/instruments", "/api/v5/account/instruments"}: rows = [deepcopy(spec)]
        elif path == "/api/v5/account/config":
            rows = [{"perm": self.perm, "acctLv": "2", "posMode": self.mode,
                     "uid": self.uid, "mainUid": self.main, "ip": ""}]
        elif path == "/api/v5/account/trade-fee":
            rows = [{"instType": "SWAP", "feeGroup": [{"groupId": "4", "maker": "-.0002", "taker": "-.0005"}]}]
        elif path == "/api/v5/account/positions":
            rows = [{"instId": self.spec["instId"], "mgnMode": "isolated", "posSide": "net", "lever": "1", "pos": str(self.units)}]
        elif path == "/api/v5/account/balance":
            rows = [{"details": [{"ccy": "USDT", "eq": str(self.cash), "cashBal": str(self.cash), "availBal": str(self.cash)}]}]
        elif path == "/api/v5/account/leverage-info":
            rows = [{"instId": self.spec["instId"], "mgnMode": "isolated", "lever": "1"}]
        elif path == "/api/v5/trade/orders-pending":
            rows = [{"instId": self.spec["instId"], "clOrdId": "manual-order"}] if self.foreign_pending else []
        elif path == "/api/v5/trade/orders-algo-pending":
            rows = [deepcopy(a) for a in self.algos.values() if a["state"] == "live" and a["ordType"] == query["ordType"][0]]
        elif path == "/api/v5/trade/order-algo":
            if method == "POST":
                body = json.loads(request.data); algo_id = str(700000 + len(self.algos))
                assert body["algoClOrdId"] not in self.algos
                stamp = str(int(self.clock() * 1000))  # OKX's cTime/uTime of the algo order (R2.1.10: venue intervals)
                self.algos[body["algoClOrdId"]] = {**body, "algoId": algo_id, "state": "live", "failCode": "0", "ordIdList": [],
                                                   "cTime": stamp, "uTime": stamp}
                rows = [{"sCode": "0", "algoClOrdId": body["algoClOrdId"], "algoId": algo_id}]
            else:
                rows = [deepcopy(self.algos[query["algoClOrdId"][0]])]
                # Match OKX's documented GET response instead of echoing every
                # POST option. Keep explicit false values for conflict tests.
                if rows[0].get("cxlOnClosePos") is True:
                    rows[0].pop("cxlOnClosePos")
        elif path == "/api/v5/trade/amend-algos":
            body = json.loads(request.data); algo = next(a for a in self.algos.values() if a["algoId"] == body["algoId"])
            algo.update(sz=body["newSz"], slTriggerPx=body["newSlTriggerPx"], uTime=str(int(self.clock() * 1000)))
            rows = [{"sCode": "0", "algoId": body["algoId"], "reqId": body["reqId"]}]
        elif path == "/api/v5/trade/cancel-algos":
            body = json.loads(request.data)[0]; algo = next(a for a in self.algos.values() if a["algoId"] == body["algoId"])
            algo["state"] = "canceled"; rows = [{"sCode": "0", "algoId": body["algoId"]}]
        elif path == "/api/v5/trade/order":
            if method == "POST":
                body = json.loads(request.data)
                if body["clOrdId"] in self.orders: raise AssertionError("Duplicate order POST")
                q = Decimal(body["sz"]) * (self.close_fraction if body["reduceOnly"] else self.entry_fraction)
                side = Decimal(1) if body["side"] == "buy" else Decimal(-1)
                price = self.ask if side > 0 else self.bid
                value = Decimal(self.spec["ctVal"])
                gross = q * value * (price - self.basis) * (-side) if body["reduceOnly"] else Decimal(0)
                fee = -q * value * price * Decimal(".0005")
                if body["reduceOnly"]:
                    assert q <= abs(self.units)
                elif q:
                    self.basis = (abs(self.units) * self.basis + q * price) / (abs(self.units) + q)
                self.units += side * q
                if not self.units:
                    for algo in self.algos.values(): algo["state"] = "canceled"
                self.cash += gross + fee
                ord_id = str(100000 + len(self.orders))
                result = {**body, "ordId": ord_id, "state": "filled" if q == Decimal(body["sz"]) else "canceled",
                          "accFillSz": str(q), "avgPx": str(price) if q else "", "fee": str(fee), "feeCcy": "USDT",
                          "pnl": str(gross), "rebate": "0", "uTime": str(int(self.clock() * 1000))}
                self.orders[body["clOrdId"]] = result
                if q:
                    self.bills.append({"billId": str(len(self.bills) + 1), "ts": str(int(self.clock() * 1000)),
                        "type": "2", "subType": "3", "ccy": "USDT", "instId": self.spec["instId"], "ordId": ord_id,
                        "pnl": str(gross), "fee": str(fee), "balChg": str(gross + fee), "posBalChg": "0"})
                if self.lost_ack:
                    self.lost_ack = False
                    raise PreflightError("network_error")
                rows = [{"sCode": "0", "clOrdId": body["clOrdId"], "ordId": ord_id}]
            else:
                if self.lookup_broken: raise PreflightError("network_error")
                rows = [deepcopy(self.orders[query["clOrdId"][0]])] if "clOrdId" in query else [deepcopy(next(o for o in self.orders.values() if o["ordId"] == query["ordId"][0]))]
        elif path == "/api/v5/account/bills": rows = deepcopy(self.bills)
        elif path == "/api/v5/market/candles":
            rows = raw_public(self.clock())["candles"]
            for i,row in enumerate(rows):
                close=candle_base+i*candle_step
                row[1:5]=[str(close),str(close+1),str(close-1),str(close)]
        elif path == "/api/v5/public/funding-rate":
            rows = raw_public(self.clock())["funding"]
            rows[0]["instId"] = spec["instId"]
            rows[0]["fundingTime"] = str(int((self.clock() + self.funding_schedule_offset) * 1000))
            rows[0]["ts"] = str(int((self.clock() - self.funding_data_age) * 1000))
        elif path == "/api/v5/market/ticker":
            rows = [{**raw_public(self.clock())["ticker"][0], "instId": spec["instId"], "bidPx": str(bid), "askPx": str(ask)}]
        elif path == "/api/v5/market/history-candles":  # the schema-4 book: closed minutes, newest first, OKX paging
            if query["instId"] != [spec["instId"]]: raise AssertionError("History of another contract")
            now = int(self.clock() * 1000) // 60000 * 60000
            after, limit = int(query.get("after", [now])[0]), int(query["limit"][0])
            price = str(candle_base)
            rows = [[str(t), price, price, price, price, "1", "0", "0", "1"]
                    for t in range(now - 60000, now - 60000 * 3000, -60000) if t < after][:limit]
        return {"code": "0", "data": rows}

    def api(self, environment="demo"):
        return ExchangeAPI("global", environment, Credentials("FAKE_KEY", "FAKE_SECRET", "FAKE_PASS"),
                           transport=self.transport, clock=self.clock, monotonic=self.clock)


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.now = 1800000030.; self.clock = lambda: self.now
        self.fakes = {"demo": FakeExchange(self.clock), "arena-a": FakeExchange(self.clock, "102"), "arena-b": FakeExchange(self.clock, "103")}
        # Fake tokens exist only in a disposable root, never loaded from environment/main live file.
        for name, env in (("demo", "demo"), ("arena-a", "live"), ("arena-b", "live")):
            (self.root / (".env.okx." + name + ".local")).write_text("OKX_SITE=global\nOKX_ENV=" + env +
                "\nOKX_API_KEY=FAKE_KEY\nOKX_API_SECRET=FAKE_SECRET\nOKX_API_PASSPHRASE=FAKE_PASS\n", encoding="utf-8")
        self.arena = Arena(self.root, background=False)
        self.workshop = Workshop(self.root, self.arena)
        def factory(context, ledger):
            def transport(body, key):
                return {"usage": {"cost": .00001}, "answers": {k: {"type": "choice", "choice": "proceed", "confidence": .99}
                        for k in json.loads(body)["questions"]}}
            return SharedJevGate(self.root, ledger=ledger, mode="OKX_DEMO" if context == "workshop_demo" else "OKX_LIVE",
                transport=transport, key_reader=lambda root: "FAKE_JEV", clock=self.clock)
        self.lab = ExchangeLab(self.root, self.workshop, self.arena, clock=self.clock, monotonic=self.clock, background=False,
            api_factory=lambda p: self.fakes[p["id"]].api(p["environment"]), jev_factory=factory,
            feed_factory=lambda api: ExchangeFeed(api, clock=self.clock))
        self.version = self.workshop.add_version(0, payload_of(catalogue_defaults()[0]))["id"]

    def tearDown(self):
        self.lab.close(); self.arena.close(); self.temp.cleanup()

    def start(self, context="workshop_demo", ids=None, accounts=None):
        result = self.lab.start(self.lab.data["generation"], context, ids or [self.version], accounts or ["demo"], 100, 300)
        state = self.lab.load(result["id"])
        self.lab.prepare(state, self.lab.chosen_profiles(accounts or ["demo"]), False)
        return state

    def probe(self):
        from trading_lab.cycle_probe import template
        identity = self.workshop.add_version(self.workshop.data["generation"], template())["id"]
        return self.start(ids=[identity])

    def test_probe_full_cycle_in_losing_market_has_all_nine_evidenced_stages(self):
        from trading_lab.arena_jev import make_payload
        state = self.probe(); bot = state["bots"][0]; f = self.fakes["demo"]
        self.lab.step(state)
        self.assertEqual(bot["ai_requests"], 1); self.assertEqual(len(f.orders), 1)
        self.now += 30; self.lab.step(state)
        self.assertEqual(bot["ai_requests"], 2); self.assertEqual(len(f.orders), 1)
        self.assertTrue(bot["_probe_hold_checked"])
        self.now += 30; f.bid = Decimal("83960"); f.ask = Decimal("83960.1")
        self.lab.step(state)
        self.assertLess(engine.gain_bps(bot, state["_last"]), 0)
        self.assertEqual(bot["ai_requests"], 3); self.assertEqual(len(f.orders), 2)
        self.assertEqual(bot["entries"], 2)
        cand = engine.candidate(state, bot, state["strategies"][0], state["_last"], self.now)
        payload = make_payload([cand], "OKX_DEMO")
        self.assertEqual(payload["state"]["c0"]["plan"]["add_after_seconds"], 60)
        with self.assertRaises(ValueError): make_payload([cand], "OKX_LIVE")
        self.now += 30; self.lab.step(state)
        self.assertEqual(bot["ai_requests"], 3); self.assertEqual(len(f.orders), 2)
        self.now += 30; self.lab.step(state)
        self.assertTrue(list(f.orders.values())[-1]["reduceOnly"])
        self.now += 1; self.lab.step(state)
        self.assertEqual(state["status"], "DONE"); self.assertFalse(state["incomplete"])
        self.assertEqual(bot["position_btc"], 0); self.assertEqual(bot["trades"], 1)
        self.assertLess(bot["pnl_usdt"], 0)
        proof = self.lab.history(state["id"])["lifecycle"]
        self.assertTrue(proof["all_stages_observed"]); self.assertEqual(proof["completed"], 9)
        self.assertEqual(next(r for r in proof["rows"] if r["id"] == "owner_stop")["status"], "not_observed")
        self.assertTrue(all(r["event_seq"] is not None for r in proof["rows"] if r["required"]))
        self.workshop.record_exchange_trial(state)
        report = self.workshop.trial(state["id"])
        self.assertEqual(report["status"], "COMPLETE"); self.assertTrue(report["lifecycle"]["all_stages_observed"])
        for operation in (self.workshop.freeze, self.workshop.promote):
            with self.assertRaises(ArenaError): operation(self.workshop.data["generation"], bot["id"])
        self.assertFalse(self.arena.data["strategies"])

    def test_probe_stop_during_add_approval_closes_only_owned_fill(self):
        state = self.probe(); f = self.fakes["demo"]; self.lab.step(state)
        self.now += 30; self.lab.step(state)
        gate = self.lab.resources[state["id"]]["gate"]; original = gate.transport
        def stop_during_approval(body, key):
            self.lab.stop(self.lab.data["generation"], state["id"])
            return original(body, key)
        gate.transport = stop_during_approval
        self.now += 30; self.lab.step(state)
        self.assertEqual(len(f.orders), 1)
        self.now += 1; self.lab.step(state); self.now += 1; self.lab.step(state)
        self.assertEqual(state["status"], "DONE"); self.assertEqual(f.units, 0)
        proof = self.lab.history(state["id"])["lifecycle"]
        self.assertEqual(next(r for r in proof["rows"] if r["id"] == "owner_stop")["status"], "observed")
        self.assertEqual(next(r for r in proof["rows"] if r["id"] == "add")["status"], "not_observed")
        self.assertFalse(proof["all_stages_observed"])

    def test_probe_jev_hold_and_unknown_cost_do_not_force_entry_or_reset_budget(self):
        for billing_unknown in (False, True):
            with self.subTest(billing_unknown=billing_unknown):
                state = self.probe(); gate = self.lab.resources[state["id"]]["gate"]
                gate.transport = lambda body,key: {"usage": {} if billing_unknown else {"cost": .00001},
                    "answers": {k: {"type":"choice", "choice":"hold", "confidence": .99} for k in json.loads(body)["questions"]}}
                before = self.lab.ledgers["workshop_demo"].summary("")["series_attempts"]
                for _ in range(4):
                    self.lab.step(state); self.now += 30
                self.assertEqual(state["status"], "DONE")
                self.assertFalse(self.fakes["demo"].orders)
                self.assertFalse(self.lab.history(state["id"])["lifecycle"]["all_stages_observed"])
                after = self.lab.ledgers["workshop_demo"].summary("")
                self.assertEqual(after["series_attempts"] - before, 1 if billing_unknown else 3)
                self.assertEqual(state["incomplete"], billing_unknown)
                self.lab.resources.pop(state["id"])["stack"].close()

    def test_low_confidence_proceed_is_logged_as_blocked_without_order(self):
        state = self.probe(); gate = self.lab.resources[state["id"]]["gate"]
        gate.transport = lambda body,key: {"usage":{"cost":.00001}, "answers":{
            k:{"type":"choice","choice":"proceed","confidence":.36} for k in json.loads(body)["questions"]}}
        self.lab.step(state)
        decision = next(e for e in state["events"] if e["code"] == "jev_decision")
        self.assertFalse(self.fakes["demo"].orders)
        self.assertFalse(decision["data"]["jev_allowed"])
        self.assertEqual(decision["data"]["blocked_reason"], "low_confidence")
        self.assertEqual(decision["data"]["confidence_threshold"], .7)
        self.assertIn("0.36 < 0.70", decision["message"])
        evidence = next(r for r in self.lab.history(state["id"])["lifecycle"]["rows"] if r["id"] == "jev_entry")
        self.assertEqual(evidence["status"], "observed")
        self.assertIn("дія заблокована", evidence["detail"])
        self.assertEqual(state["bots"][0]["ai_requests"], 1)
        self.now += 90; self.lab.step(state)
        self.assertEqual(state["status"], "DONE")
        self.assertFalse(state["incomplete"])
        self.assertFalse(self.lab.history(state["id"])["lifecycle"]["all_stages_observed"])

    def test_probe_has_fixed_demo_scope_and_cannot_become_arena_strategy(self):
        from trading_lab.cycle_probe import template
        from trading_lab.arena_strategies import validate_strategy
        from trading_lab.strategy_training import run_trial
        payload = template(); clean = validate_strategy(payload)
        self.assertEqual(payload_of(clean), payload)
        changed = deepcopy(payload); changed["rules"]["size_pct"] = 30
        with self.assertRaises(ArenaError): validate_strategy(changed)
        with self.assertRaises(ArenaError): self.arena.add_strategy(0, payload)
        with self.assertRaises(ArenaError): self.arena.import_frozen_strategy(clean, {})
        with self.assertRaises(ArenaError): run_trial(clean)
        with self.assertRaises(PreflightError): engine.create([clean], ["arena-a"], "arena_live", 100, 300, self.now, "test")
        with self.assertRaises(PreflightError): engine.create([clean], ["demo"], "workshop_demo", 500, 300, self.now, "test")
        with self.assertRaises(PreflightError): engine.create([clean], ["demo"], "workshop_demo", 100, 900, self.now, "test")
        self.assertTrue(all(not f.requests for f in self.fakes.values()))

    def test_probe_order_caps_apply_at_submission_and_zero_fill_is_not_retried(self):
        state = self.probe(); bot = state["bots"][0]; f = self.fakes["demo"]
        resource = self.lab.resources[state["id"]]
        snapshot = resource["feed"].snapshot()
        body = engine.entry_body(state, bot, state["strategies"][0], snapshot, "oversize")
        body["sz"] = ".02"
        with self.assertRaisesRegex(PreflightError, "diagnostic_notional"):
            self.lab.submit(state, bot, resource["apis"][0], body, self.now)
        self.assertFalse(bot["_orders"]); self.assertFalse(f.orders)
        f.entry_fraction = Decimal(0)
        self.lab.step(state); self.now += 5; self.lab.step(state)
        self.assertEqual(len(f.orders), 1); self.assertEqual(bot["ai_requests"], 1)
        self.assertEqual(state["status"], "DONE")
        proof = self.lab.history(state["id"])["lifecycle"]
        self.assertFalse(proof["all_stages_observed"])
        self.assertEqual(next(r for r in proof["rows"] if r["id"] == "entry")["status"], "not_observed")

    def test_probe_hold_review_failure_keeps_position_but_prevents_add(self):
        state = self.probe(); self.lab.step(state)
        gate = self.lab.resources[state["id"]]["gate"]
        gate.transport = lambda body,key: {"usage":{"cost":.00001}, "answers":{}}
        self.now += 30; self.lab.step(state)
        self.assertFalse(state["bots"][0]["_probe_hold_checked"])
        for _ in range(3): self.now += 30; self.lab.step(state)
        self.now += 1; self.lab.step(state)
        self.assertEqual(state["status"], "DONE")
        self.assertEqual(len(self.fakes["demo"].orders), 2)
        self.assertFalse(self.lab.history(state["id"])["lifecycle"]["all_stages_observed"])
        self.assertEqual(next(r for r in self.lab.history(state["id"])["lifecycle"]["rows"] if r["id"] == "jev_hold")["status"], "blocked")

    def test_normal_recipe_rejects_losing_add_and_reports_known_failed_signal(self):
        payload = payload_of(catalogue_defaults()[0]); payload["rules"]["max_entries"] = 2
        identity = self.workshop.add_version(self.workshop.data["generation"], payload)["id"]
        state = self.start(ids=[identity]); self.lab.step(state)
        self.now += 30; f = self.fakes["demo"]; f.bid = Decimal("83960"); f.ask = Decimal("83960.1")
        self.lab.step(state)
        self.assertEqual(len(f.orders), 1); self.assertEqual(state["bots"][0]["ai_requests"], 1)
        event = next(e for e in reversed(state["events"]) if e["code"] == "position_checks")
        self.assertIn("gain_bps", event["message"]); self.assertNotIn("неповні дані", event["message"])
        self.assertTrue(all(c["actual"] is not None for c in event["data"]["checks"]))

    def test_lifecycle_does_not_mistake_two_round_trips_or_repeat_fill_updates_for_add(self):
        state = self.start(); self.lab.step(state)
        bot = state["bots"][0]; api = self.lab.resources[state["id"]]["apis"][0]
        self.lab.submit(state, bot, api, engine.close_body(bot, self.lab.identity_for(state, bot, "C")), self.now)
        self.now += 30
        self.lab.submit(state, bot, api, engine.entry_body(state, bot, state["strategies"][0], state["_last"],
                        self.lab.identity_for(state, bot, "E")), self.now)
        self.assertEqual(bot["trades"], 1); self.assertEqual(bot["entries"], 1)
        proof = self.lab.history(state["id"])["lifecycle"]
        self.assertNotEqual(next(r for r in proof["rows"] if r["id"] == "add")["status"], "observed")
        engine.record_result(state, bot, bot["_orders"][-1], deepcopy(bot["_orders"][-1]["result"]), self.now)
        self.assertNotEqual(next(r for r in self.lab.history(state["id"])["lifecycle"]["rows"] if r["id"] == "add")["status"], "observed")

    def test_stop_on_flat_after_previous_trade_is_not_stop_close_coverage(self):
        state = self.start(); self.lab.step(state)
        bot = state["bots"][0]; api = self.lab.resources[state["id"]]["apis"][0]
        self.lab.submit(state, bot, api, engine.close_body(bot, self.lab.identity_for(state, bot, "C")), self.now)
        self.lab.stop(self.lab.data["generation"], state["id"])
        self.now += 1; self.lab.step(state)
        proof = self.lab.history(state["id"])["lifecycle"]
        self.assertEqual(next(r for r in proof["rows"] if r["id"] == "close")["status"], "observed")
        self.assertEqual(next(r for r in proof["rows"] if r["id"] == "owner_stop")["status"], "not_observed")

    def test_probe_six_request_cap_never_forces_add_or_blocks_timed_close(self):
        state = self.probe(); resource = self.lab.resources[state["id"]]; seen = []
        def transport(body, key):
            kind = json.loads(body)["state"]["c0"]["kind"]; seen.append(kind)
            choice = "proceed" if kind == "hold" or kind == "entry" and seen.count("entry") == 3 else "hold"
            return {"usage":{"cost":.00001}, "answers":{"c0":{"type":"choice","choice":choice,"confidence":.99}}}
        resource["gate"].transport = transport
        for _ in range(7): self.lab.step(state); self.now += 30
        self.now -= 29; self.lab.step(state)
        self.assertEqual(state["status"], "DONE")
        self.assertEqual(state["bots"][0]["ai_requests"], 6)
        self.assertEqual(seen, ["entry", "entry", "entry", "hold", "add", "add"])
        self.assertEqual(len(self.fakes["demo"].orders), 2)
        self.assertEqual(state["bots"][0]["position_btc"], 0)

    def test_get_construction_and_status_never_calls_exchange_or_model(self):
        state = self.lab.status()
        self.assertTrue(all(not f.requests for f in self.fakes.values()))
        self.assertEqual(state["budgets"]["workshop"]["series_attempts"], 0)
        self.assertEqual(state["profiles"][0]["environment"], "OKX_DEMO")
        self.assertNotIn("FAKE_", json.dumps(state))

    def test_demo_past_funding_calendar_uses_unknowns_and_actual_account_bills(self):
        f = self.fakes["demo"]; f.funding_schedule_offset = -5 * 86400
        state = self.start(); self.lab.step(state)
        self.assertGreater(state["bots"][0]["position_btc"], 0)
        snapshot = self.lab.resources[state["id"]]["feed"].snapshot()
        self.assertEqual(snapshot["source"], "OKX_DEMO")
        self.assertEqual(snapshot["funding_status"], "unknown_schedule")
        self.assertTrue(all(snapshot[k] is None for k in ("funding_at", "funding_rate", "funding_time_ms")))
        self.assertLess(int(snapshot["raw"]["funding"][0]["fundingTime"]), self.now * 1000)
        self.assertGreater(state["public_get_count"], 0)
        self.now += 5; self.lab.step(state)
        self.assertEqual(len([e for e in state["events"] if e["code"] == "demo_funding_unknown"]), 1)
        # Funding isn't assumed to be free just because its public calendar is invalid.
        f.cash -= Decimal(".025")
        f.bills.append({"billId": "10000", "ts": str(int(self.now * 1000)), "subType": "173",
                        "ccy": "USDT", "instId": "BTC-USDT-SWAP", "pnl": "-.025", "fee": "0"})
        self.lab.stop(self.lab.data["generation"], state["id"])
        self.now += 1; self.lab.step(state); self.now += 1; self.lab.step(state)
        self.assertEqual(state["status"], "DONE"); self.assertFalse(state["incomplete"])
        self.assertEqual(state["bots"][0]["accounting"]["signed_funding_usdt"], "-0.025")
        self.assertTrue(state["bots"][0]["accounting"]["cash_delta_matches"])
        # A Demo recording with an unknown calendar cannot silently become a PAPER funding simulation.
        with self.assertRaises(ArenaError): build_snapshot(snapshot["raw"], snapshot["at"])

    def test_live_past_funding_calendar_remains_rejected(self):
        f = self.fakes["arena-a"]; f.funding_schedule_offset = -5 * 86400
        api = f.api("live")
        with self.assertRaisesRegex(ArenaError, "funding"):
            ExchangeFeed(api, clock=self.clock).snapshot()
        self.assertFalse(api.writes); self.assertTrue(all(r.get_method() == "GET" for r in f.requests))

    def test_demo_exception_never_hides_bad_quotes_candles_or_funding_data(self):
        for stage in ("ticker", "candles", "funding"):
            with self.subTest(stage=stage):
                f = FakeExchange(self.clock); f.funding_schedule_offset = -5 * 86400
                original = f.transport
                def transport(request):
                    payload = original(request)
                    path = urlsplit(request.full_url).path
                    if stage == "ticker" and path.endswith("/ticker"):
                        payload["data"][0]["ts"] = str(int((self.now - 11) * 1000))
                    elif stage == "candles" and path.endswith("/candles"):
                        payload["data"].pop(10)
                    elif stage == "funding" and path.endswith("/funding-rate"):
                        payload["data"][0]["ts"] = str(int((self.now - 91) * 1000))
                    return payload
                api = f.api(); api.transport = transport
                with self.assertRaises(ArenaError): ExchangeFeed(api, clock=self.clock).snapshot()

    def test_read_only_check_includes_market_and_explicit_demo_funding_warning(self):
        f = self.fakes["demo"]; f.funding_schedule_offset = -5 * 86400
        result = self.lab.check(0, ["demo"])
        self.assertTrue(result["ready"])
        profile = self.lab.status()["profiles"][0]
        self.assertTrue(profile["market_checked"])
        self.assertEqual(profile["market_source"], "OKX_DEMO")
        self.assertEqual(profile["funding_status"], "unknown_schedule")
        self.assertIn("bills", profile["reason"])
        self.assertTrue(all(r.get_method() == "GET" for r in f.requests))
        self.assertFalse(f.orders)
        self.assertEqual(self.lab.status()["budgets"]["workshop"]["series_attempts"], 0)
        f.funding_data_age = 91
        result = self.lab.check(self.lab.data["generation"], ["demo"])
        self.assertFalse(result["ready"])
        self.assertIn("funding", self.lab.status()["profiles"][0]["reason"])

    def test_rejected_arrival_snapshot_never_reaches_jev_or_order(self):
        state = self.start(); feed = self.lab.resources[state["id"]]["feed"]
        original = feed.snapshot
        def stale():
            snapshot = original(); snapshot["at"] = self.now - 11
            return snapshot
        feed.snapshot = stale
        for _ in range(3): self.lab.step(state); self.now += 5
        self.assertFalse(self.fakes["demo"].orders)
        self.assertEqual(state["jev_usage"]["attempts"], 0)
        self.assertEqual(state["status"], "RUNNING"); self.assertTrue(state["incomplete"])
        self.assertEqual(state["admission_blocked"], "market_unavailable")
        events = [e for e in state["events"] if e["code"] == "market_unavailable"]
        self.assertEqual(events[-1]["data"]["reason"], "market_arrival_stale")
        self.assertEqual(events[-1]["data"]["consecutive_failures"], 3)

    def test_journal_failure_blocks_jev_and_accumulates_failure_limit(self):
        state = self.start()
        with patch.object(self.lab, "record_snapshot", side_effect=OSError("FAKE_SECRET")):
            for _ in range(3): self.lab.step(state); self.now += 5
        self.assertFalse(self.fakes["demo"].orders)
        self.assertEqual(state["jev_usage"]["attempts"], 0)
        self.assertEqual(state["status"], "RUNNING")
        self.assertNotIn("FAKE_SECRET", json.dumps(self.lab.history(state["id"])))
        event = next(e for e in state["events"] if e["code"] == "recording_blocked")
        self.assertEqual(state["admission_blocked"], "snapshot_storage")
        self.assertTrue(state["incomplete"])
        self.assertNotIn("FAKE_SECRET", json.dumps(event))

    def test_market_failure_records_safe_stage_cause_and_count(self):
        state = self.start(); api = self.lab.resources[state["id"]]["feed"].api
        original = api.transport
        def transport(request):
            if urlsplit(request.full_url).path.endswith("/candles"):
                raise PreflightError("network_error")
            return original(request)
        api.transport = transport; self.lab.step(state)
        event = next(e for e in state["events"] if e["code"] == "market_unavailable")
        self.assertEqual(event["data"]["stage"], "candles")
        self.assertEqual(event["data"]["reason"], "network_error")
        self.assertEqual(event["data"]["public_get_count"], 2)
        self.assertFalse(self.fakes["demo"].orders)
        self.assertEqual(state["jev_usage"]["attempts"], 0)

    def test_full_actual_fake_cycle_fees_bills_cash_and_report(self):
        state = self.start(); self.lab.step(state)
        bot = state["bots"][0]
        self.assertGreater(bot["position_btc"], 0)
        self.assertEqual(bot["ai_requests"], 1)
        self.lab.stop(self.lab.data["generation"], state["id"])
        self.now += 1; self.lab.step(state); self.now += 1; self.lab.step(state)
        self.assertEqual(state["status"], "DONE")
        self.assertEqual(bot["position_btc"], 0)
        self.assertTrue(bot["accounting_reconciled"])
        self.assertLess(bot["pnl_usdt"], 0)
        self.assertEqual(bot["trades"], 1)
        self.workshop.record_exchange_trial(state); self.workshop.record_exchange_trial(state)
        self.assertEqual(len(self.workshop.data["trials"]), 1)
        report = self.workshop.trial(state["id"])
        self.assertEqual(report["runtime"], "OKX_DEMO_SHARED_JEV")
        self.assertEqual(report["status"], "COMPLETE")
        self.workshop.freeze(self.workshop.data["generation"], self.version)
        self.workshop.promote(self.workshop.data["generation"], self.version)
        self.assertEqual(self.arena.data["strategies"][0]["workshop"]["training_runtime"], "OKX_DEMO_SHARED_JEV")
        text = json.dumps(self.lab.history(state["id"]))
        self.assertNotIn("FAKE_", text); self.assertNotIn("account_tag", text); self.assertNotIn("paper_entry_fill", text)

    def test_partial_ioc_and_lost_ack_never_repeat_entry(self):
        f = self.fakes["demo"]; f.entry_fraction = Decimal(".5"); f.lost_ack = True
        state = self.start(); self.lab.step(state)
        self.assertEqual(len(f.orders), 1)
        self.assertGreater(state["bots"][0]["position_btc"], 0)
        self.lab.stop(self.lab.data["generation"], state["id"])
        self.now += 1; self.lab.step(state); self.now += 1; self.lab.step(state)
        self.assertEqual(state["status"], "DONE"); self.assertEqual(f.units, 0)
        self.assertEqual(len([o for o in f.orders.values() if not o["reduceOnly"]]), 1)
        self.assertEqual(list(f.orders.values())[1]["sz"], list(f.orders.values())[0]["accFillSz"])

    def test_stop_during_jev_never_opens(self):
        state = self.start()
        gate = self.lab.resources[state["id"]]["gate"]
        original = gate.transport
        def transport(body, key):
            self.lab.stop(self.lab.data["generation"], state["id"])
            return original(body, key)
        gate.transport = transport
        self.lab.step(state)
        self.assertFalse(self.fakes["demo"].orders)
        self.now += 1; self.lab.step(state)
        self.assertEqual(state["status"], "DONE")

    def test_stop_during_order_ack_closes_actual_fill_without_add(self):
        state = self.start(); f = self.fakes["demo"]; original = f.transport
        def transport(request):
            result = original(request)
            if request.get_method() == "POST" and urlsplit(request.full_url).path == "/api/v5/trade/order" and not json.loads(request.data)["reduceOnly"]:
                self.lab.stop(self.lab.data["generation"], state["id"])
            return result
        self.lab.resources[state["id"]]["apis"][0].transport = transport
        self.lab.step(state); self.now += 1; self.lab.step(state); self.now += 1; self.lab.step(state)
        self.assertEqual(state["status"], "DONE"); self.assertEqual(f.units, 0)
        self.assertEqual(len(f.orders), 2)

    def test_foreign_position_blocks_writes_preserves_manual_review(self):
        state = self.start(); self.lab.step(state)
        f = self.fakes["demo"]; f.units += Decimal(".01")
        self.lab.stop(self.lab.data["generation"], state["id"])
        self.now += 1; self.lab.step(state)
        self.assertTrue(state["bots"][0]["_manual_review"])
        self.assertEqual(len(f.orders), 1)
        self.now += 121; self.lab.step(state)
        self.assertEqual(state["status"], "PAUSED")
        self.assertGreater(state["bots"][0]["position_btc"], 0)

    def test_two_keys_same_live_account_rejected_before_any_write(self):
        self.fakes["arena-b"].uid = self.fakes["arena-a"].uid
        result = self.lab.start(0, "arena_live", ["sample-trend", "sample-short"], ["arena-a", "arena-b"], 100, 300)
        state = self.lab.load(result["id"])
        with self.assertRaisesRegex(PreflightError, "two_keys"):
            self.lab.prepare(state, self.lab.chosen_profiles(["arena-a", "arena-b"]), False)
        self.assertTrue(all(r.get_method() == "GET" for f in self.fakes.values() for r in f.requests))

    def test_individual_stop_accounting_timeout_preserves_other_bot_and_pauses(self):
        state = self.start("arena_live", ["sample-trend", "sample-short"], ["arena-a", "arena-b"])
        self.lab.step(state)
        bot = state["bots"][0]
        self.lab.stop(self.lab.data["generation"], state["id"], bot["id"])
        self.now += 1; self.lab.step(state)
        self.fakes["arena-a"].bills.clear()
        self.now += 1; self.lab.step(state)
        self.assertEqual(state["status"], "RUNNING")
        self.now += 120; self.lab.step(state)
        self.assertEqual(state["status"], "PAUSED")
        self.assertFalse(bot["accounting_reconciled"])
        self.assertTrue(state["bots"][1]["stop_requested"])
        self.assertTrue(state["incomplete"])

    def test_monotonic_deadline_blocks_late_entry_even_if_wall_clock_moves_back(self):
        state = self.start()
        resource = self.lab.resources[state["id"]]
        snapshot = resource["feed"].snapshot()
        bot = state["bots"][0]
        body = engine.entry_body(state, bot, state["strategies"][0], snapshot, self.lab.identity_for(state, bot, "E"))
        with patch.object(self.lab, "monotonic", return_value=resource["deadline"] + 1):
            self.assertFalse(self.lab.submit(state, bot, resource["apis"][0], body, self.now))
        self.assertFalse(self.fakes["demo"].orders)

    def test_funding_uses_pnl_not_isolated_balance_change(self):
        state = self.start(); self.lab.step(state)
        f = self.fakes["demo"]
        f.bills.append({"billId":"999","ts":str(int(self.now*1000)),"type":"8","subType":"173","ccy":"USDT",
                       "instId":"BTC-USDT-SWAP","pnl":"-.02","fee":"0","balChg":"0","posBalChg":"-.02"})
        f.cash -= Decimal(".02")
        self.lab.stop(self.lab.data["generation"],state["id"])
        self.now += 1; self.lab.step(state); self.now += 1; self.lab.step(state)
        self.assertEqual(state["status"],"DONE"); self.assertEqual(state["bots"][0]["funding_usdt"],.02)
        self.assertEqual(state["bots"][0]["accounting"]["signed_funding_usdt"],"-0.02")

    def test_missing_bills_never_labels_run_reconciled(self):
        state = self.start(); self.lab.step(state)
        self.lab.stop(self.lab.data["generation"],state["id"])
        self.now += 1; self.lab.step(state)
        self.fakes["demo"].bills.clear()
        self.now += 1; self.lab.step(state)
        self.assertFalse(state["bots"][0]["accounting_reconciled"])
        self.now += 121; self.lab.step(state)
        self.assertEqual(state["status"], "PAUSED")

    def test_unknown_billing_blocks_future_paid_entries_but_not_close(self):
        state = self.start(); self.lab.step(state)
        ledger = self.lab.ledgers["workshop_demo"]
        reservation=ledger.reserve(state["id"],"unknown",self.now); ledger.finish(reservation,None)
        self.lab.stop(self.lab.data["generation"],state["id"])
        self.now += 1; self.lab.step(state); self.now += 1; self.lab.step(state)
        self.assertEqual(state["status"],"DONE")
        self.assertTrue(state["incomplete"])
        self.assertFalse(state["bots"][0]["ai_cost_complete"])
        with self.assertRaisesRegex(ArenaError, "бюджет"):
            self.lab.start(self.lab.data["generation"], "workshop_demo", [self.version],["demo"],100,300)

    def test_resume_close_only_no_second_jev_or_entry(self):
        state = self.start(); self.lab.step(state)
        resource=self.lab.resources.pop(state["id"]); resource["stack"].close()
        self.assertEqual(self.lab.history(state["id"])["status"],"PAUSED")
        self.lab.resume(self.lab.data["generation"],state["id"])
        self.lab.prepare(state,self.lab.chosen_profiles(["demo"]),True)
        self.now += 1; self.lab.step(state); self.now += 1; self.lab.step(state)
        self.assertEqual(state["status"],"DONE"); self.assertTrue(state["incomplete"])
        self.assertEqual(state["bots"][0]["ai_requests"],1)
        self.assertEqual(len(self.fakes["demo"].orders),2)

    def test_stop_before_prepare_sends_no_order_or_settings_write(self):
        result=self.lab.start(0,"workshop_demo",[self.version],["demo"],100,300)
        state=self.lab.load(result["id"])
        self.lab.stop(self.lab.data["generation"],state["id"])
        self.lab.prepare(state,self.lab.chosen_profiles(["demo"]),False); self.lab.step(state)
        self.assertEqual(state["status"],"DONE")
        self.assertTrue(all(r.get_method()=="GET" for r in self.fakes["demo"].requests))

    def test_unknown_ack_survives_restart_without_blind_entry_retry(self):
        state=self.start(); f=self.fakes["demo"]; f.lost_ack=True; f.lookup_broken=True
        self.lab.step(state)
        self.assertEqual(len(f.orders),1)
        self.assertEqual(state["bots"][0]["position_btc"],0)
        for _ in range(3): self.now+=5; self.lab.step(state)
        self.assertEqual(len(f.orders),1)
        self.now+=121; self.lab.step(state)
        self.assertEqual(state["status"],"PAUSED")
        resource=self.lab.resources.pop(state["id"]);resource["stack"].close()
        f.lookup_broken=False
        self.lab.resume(self.lab.data["generation"],state["id"])
        self.lab.prepare(state,self.lab.chosen_profiles(["demo"]),True)
        self.now+=1;self.lab.step(state);self.now+=1;self.lab.step(state)
        self.assertEqual(state["status"],"DONE");self.assertEqual(f.units,0)
        self.assertEqual(len([r for r in f.orders.values() if not r["reduceOnly"]]),1)
        self.assertEqual(state["bots"][0]["ai_requests"],1)

    def test_live_bots_use_distinct_accounts_cash_and_one_common_jev_batch(self):
        # Two immutable recipes with the same signal but opposite account identities.
        first=deepcopy(catalogue_defaults()[0]); second=payload_of(first);second["name"]="Second example"
        result=self.arena.add_strategy(0,second);ids=[first["id"],result["id"]]
        state=self.start("arena_live",ids,["arena-a","arena-b"]);self.lab.step(state)
        self.assertEqual(self.lab.ledgers["arena_live"].summary(state["id"])["attempts"],1)
        self.assertTrue(all(b["position_btc"]>0 for b in state["bots"][:2]))
        self.assertEqual(state["bots"][-1]["pnl_usdt"],0)
        self.lab.stop(self.lab.data["generation"],state["id"],ids[0])
        self.now+=1;self.lab.step(state);self.now+=1;self.lab.step(state)
        self.assertEqual(self.fakes["arena-a"].units,0);self.assertGreater(self.fakes["arena-b"].units,0)
        self.lab.stop(self.lab.data["generation"],state["id"])
        self.now+=1;self.lab.step(state);self.now+=1;self.lab.step(state)
        self.assertEqual(state["status"],"DONE")
        self.assertTrue(all(b["accounting_reconciled"] for b in state["bots"]))

    def test_stop_is_responsive_while_jev_thread_is_blocked(self):
        state=self.start(); gate=self.lab.resources[state["id"]]["gate"]
        entered,released=threading.Event(),threading.Event();original=gate.transport
        def blocked(body,key):
            entered.set()
            if not released.wait(3): raise AssertionError("Stop held the model lock")
            return original(body,key)
        gate.transport=blocked
        thread=threading.Thread(target=self.lab.step,args=(state,));thread.start()
        self.assertTrue(entered.wait(2))
        self.lab.stop(self.lab.data["generation"],state["id"])
        self.assertTrue(state["bots"][0]["stop_requested"])
        saved=json.loads((self.lab.path/(state["id"]+".json")).read_text(encoding="utf-8"))
        self.assertTrue(saved["bots"][0]["stop_requested"])
        released.set();thread.join(3);self.assertFalse(thread.is_alive())
        self.assertFalse(self.fakes["demo"].orders)

    def test_new_series_budget_is_persistent_and_does_not_reset_old_ledger(self):
        ledger=self.lab.ledgers["workshop_demo"]
        token=ledger.reserve("old","hash",self.now);ledger.finish(token,.0096)
        self.assertIsNone(ledger.reserve("new","hash",self.now))
        self.lab.close()
        reloaded=ExchangeLab(self.root,self.workshop,self.arena,background=False)
        try:
            self.assertEqual(reloaded.status()["budgets"]["workshop"]["series_reported_cost_usd"],.0096)
            with self.assertRaises(ArenaError):reloaded.start(0,"workshop_demo",[self.version],["demo"],100,300)
        finally:reloaded.close()

    def test_status_reads_a_finished_run_once_until_its_checkpoint_changes(self):
        # A poll must not re-read every journal of the history: a 5-hour battle holds thousands of events.
        identity = self.start()["id"]
        self.lab.close()
        def reopen():
            lab = ExchangeLab(self.root, self.workshop, self.arena, background=False)
            return lab, patch.object(lab, "load", wraps=lab.load)
        lab, spy = reopen()
        try:
            with spy as load:
                lab.status()
                unfinished = lab.status()
            self.assertEqual(load.call_count, 2)  # unfinished: the full view on every poll, with recovery shown
            self.assertEqual(unfinished["active"]["workshop_demo"]["status"], "PAUSED")
            self.assertEqual(unfinished["history"][0]["status"], "PAUSED")
        finally:
            lab.close()
        path = self.lab.path / (identity + ".json")
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved["status"] = "DONE"
        path.write_text(json.dumps(saved), encoding="utf-8")
        lab, spy = reopen()
        try:
            with spy as load:
                first, second = lab.status(), lab.status()
                self.assertEqual(load.call_count, 1)
                self.assertEqual(first["history"], second["history"])
                self.assertIsNone(second["active"]["workshop_demo"])
                second["history"][0]["status"] = "edited"
                self.assertEqual(lab.status()["history"][0]["status"], "DONE")  # a caller's copy never reaches the cache
                self.assertEqual(load.call_count, 1)
                path.write_bytes(path.read_bytes() + b"\n")
                third = lab.status()
                self.assertEqual(load.call_count, 2)
                self.assertEqual(third["history"][0]["status"], "DONE")
        finally:
            lab.close()

    def test_panel_polls_do_not_starve_a_running_worker_on_real_size_history(self):
        # The other PC on R2.1.1: two 5-hour battles of ~7200 events; every poll re-read them under the lab lock and the
        # trading loop stood 91 s (clock_gap). Here three finished runs of that size, a running one in memory, readers
        # polling without pause, and a worker that saves an event under the lab lock every 50 ms.
        import time
        from trading_lab.arena_engine import emit
        from trading_lab.league import League
        from trading_lab.match_journal import segmented, verified_rows
        from trading_lab.ops_storage import encode
        active = self.start()
        with self.lab.lock:
            self.lab.save(active)
        journal = lambda identity: self.lab.path / (identity + ".events.jsonl")
        events = [row["event"] for row in verified_rows(journal(active["id"]))]
        filler = {"events": [], "_event_seq": len(events)}
        conditions = [{"name": "condition " + str(k), "status": "not_met", "now": "1.23456", "need": "> 2.5"} for k in range(6)]
        while len(events) + len(filler["events"]) < 7200:
            emit(filler, self.now, active["bots"][0], 6, "no_signal", "Умови входу не виконано", reason="no_signal", conditions=conditions)
        events += [{k: v for k, v in e.items() if k != "seq"} for e in filler["events"]]
        saved = json.loads((self.lab.path / (active["id"] + ".json")).read_text(encoding="utf-8"))
        finished = []
        for index in range(3):
            identity = hashlib.sha256(str(index).encode()).hexdigest()[:32]
            folder = self.lab.path / (identity + ".events.segments")
            folder.mkdir()
            seq, last, lines = 0, "0" * 64, []
            for event in events:
                data = {"seq": seq + 1, "previous_sha256": last, "event": event}
                last, seq = hashlib.sha256(encode(data)).hexdigest(), seq + 1
                lines.append(encode({**data, "sha256": last}) + b"\n")
            (folder / "segment-00000001.jsonl").write_bytes(b"".join(lines))
            (self.lab.path / (identity + ".json")).write_text(json.dumps({**saved, "id": identity, "status": "DONE",
                "events": [{"seq": i + 1, **e} for i, e in enumerate(events)][-128:],
                "_journal_checkpoint_seq": seq, "_event_seq": seq}), encoding="utf-8")
            self.lab.data["runs"].append(identity)
            finished.append(identity)
        self.assertGreater(sum(p.stat().st_size for p in folder.glob("*.jsonl")), 4 * 1024 * 1024)
        league = League(self.root, self.arena, self.lab, workshop=self.workshop)
        self.lab.status()  # the first read after start loads the history once, before the trading loop runs
        league.status()
        writer = segmented(journal(active["id"]))
        stop, waits, errors = threading.Event(), [], []
        def worker():
            while not stop.is_set():
                started = time.monotonic()
                try:
                    with self.lab.lock:
                        waits.append(time.monotonic() - started)
                        emit(active, self.now, None, 6, "no_signal", "Умови входу не виконано")
                        self.lab.save(active)
                except Exception as error:  # noqa: BLE001 - any worker fault is the finding
                    errors.append("worker " + repr(error))
                stop.wait(.05)
        def reader(name, read):
            while not stop.is_set():
                try:
                    read()
                except Exception as error:  # noqa: BLE001
                    errors.append(name + " " + repr(error))
                stop.wait(.02)  # far denser than any panel; without a pause the readers mostly share out the GIL
        readers = {"status": self.lab.status, "status again": self.lab.status, "league": league.status,
                   "history": lambda: self.lab.history(finished[0]), "running history": lambda: self.lab.history(active["id"])}
        threads = [threading.Thread(target=worker)] + [threading.Thread(target=reader, args=item) for item in readers.items()]
        for thread in threads:
            thread.start()
        stop.wait(4)
        stop.set()
        for thread in threads:
            thread.join(120)
        self.assertEqual(errors, [])
        self.assertGreaterEqual(len(waits), 10)  # the worker kept saving
        # R2.1.1 on this scenario: waits of several seconds (8.3 s at most offline); 2 s leaves room for a busy laptop.
        self.assertLess(max(waits), 2.0, "the trading loop never waits seconds for the lab lock behind panel reads")
        self.assertIs(segmented(journal(active["id"])), writer)  # no reader rebuilt or evicted the worker's journal
        self.assertEqual({row["id"]: row["status"] for row in self.lab.status()["history"] if row["id"] in finished},
                         dict.fromkeys(finished, "DONE"))

    def test_foreign_bills_and_cash_delta_cannot_be_ranked_as_success(self):
        state=self.start();self.lab.step(state)
        self.lab.stop(self.lab.data["generation"],state["id"])
        self.now+=1;self.lab.step(state)
        self.fakes["demo"].cash+=1
        self.now+=1;self.lab.step(state)
        self.assertFalse(state["bots"][0]["accounting_reconciled"])


class HttpExchangeTests(unittest.TestCase):
    def test_get_and_csrf_unsafe_payloads_never_start_or_read_exchange(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);calls=[]
            arena=Arena(root,background=False)
            def factory(root,workshop,arena):
                return ExchangeLab(root,workshop,arena,background=False,api_factory=lambda p:calls.append(p))
            server=Server(Panel(root,launcher=lambda *a,**k:None),0,arena=arena,exchange_factory=factory)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                conn=http.client.HTTPConnection("127.0.0.1",server.server_port)
                conn.request("GET","/api/exchange/state");response=conn.getresponse();self.assertEqual(response.status,200);response.read()
                conn.request("POST","/api/exchange/start",json.dumps({}),{"Content-Type":"application/json"})
                response=conn.getresponse();self.assertEqual(response.status,403);response.read()
                headers={"Content-Type":"application/json","Origin":server.origin,"X-Panel-Token":server.panel.token}
                conn.request("POST","/api/exchange/start",json.dumps({"generation":0,"order":{"side":"buy"}}),headers)
                response=conn.getresponse();self.assertEqual(response.status,409);response.read()
                conn.request("GET","/api/exchange/runs/../../.env.okx.local");response=conn.getresponse()
                self.assertEqual(response.status,404);response.read();conn.close()
                self.assertFalse(calls);self.assertFalse(server.exchange.data["runs"])
            finally:server.shutdown();thread.join(2);server.server_close()


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.fake=FakeExchange(lambda:1800000030.); self.api=self.fake.api()

    def test_readiness_is_get_only_requires_correct_mode_and_unique_live_subaccount(self):
        check=self.api.preflight(100,True)
        self.assertTrue(check["is_subaccount"]); self.assertFalse(self.api.writes)
        self.assertTrue(all(r.get_method()=="GET" for r in self.fake.requests))
        self.fake.mode="long_short_mode"
        with self.assertRaises(PreflightError): self.api.preflight(100)
        self.fake.mode="net_mode"; self.fake.uid=self.fake.main
        with self.assertRaisesRegex(PreflightError,"subaccount"): self.api.preflight(100,True)

    def test_demo_header_every_get_and_signed_post_body_expiration(self):
        self.api.preflight(100); self.api.arm(100)
        body={"instId":"BTC-USDT-SWAP","tdMode":"isolated","posSide":"net","side":"buy","ordType":"ioc",
              "sz":".01","px":"84000.2","reduceOnly":False,"clOrdId":"J5"+"a"*16+"0E0000","stpMode":"cancel_taker"}
        self.api.order(body)
        self.assertTrue(all(dict((k.lower(),v) for k,v in r.headers.items())["x-simulated-trading"]=="1" for r in self.fake.requests))
        r=self.fake.requests[-1]; h={k.lower():v for k,v in r.headers.items()}
        signature=base64.b64encode(hmac.new(b"FAKE_SECRET",(h["ok-access-timestamp"]+"POST/api/v5/trade/order").encode()+r.data,hashlib.sha256).digest()).decode()
        self.assertEqual(h["ok-access-sign"],signature); self.assertEqual(int(h["exptime"]),1800000035000)

    def test_writes_unarmed_wrong_endpoint_non_reduce_recovery_and_oversize_blocked(self):
        self.api.preflight(100)
        body={"instId":"BTC-USDT-SWAP","tdMode":"isolated","posSide":"net","side":"buy","ordType":"ioc",
              "sz":".01","px":"84000.2","reduceOnly":False,"clOrdId":"J5"+"a"*16+"0E0000","stpMode":"cancel_taker"}
        with self.assertRaises(PreflightError):self.api.order(body)
        self.api.arm(100,close_only=True)
        with self.assertRaises(PreflightError):self.api.order(body)
        self.api.arm(100); body["sz"]="1"
        with self.assertRaises(PreflightError):self.api.order(body)
        with self.assertRaises(PreflightError):self.api.request("POST","/api/v5/asset/transfer",{})
        close={**body,"side":"sell","ordType":"market","sz":"15","reduceOnly":True,
               "clOrdId":"J5"+"a"*16+"0C0003"}
        close.pop("px")
        self.api.arm(100,close_only=True)
        self.api.validate_write("/api/v5/trade/order",close)
        close["sz"]="31"
        with self.assertRaises(PreflightError):self.api.validate_write("/api/v5/trade/order",close)
        self.assertEqual(self.api.write_count,0)

    def test_profiles_never_fall_back_to_live_environment_or_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); demo=profiles(root)[0]
            (root/".env.okx.local").write_text("OKX_API_KEY=MAIN_LIVE",encoding="utf-8")
            with patch.dict("os.environ", {"OKX_API_KEY":"LIVE_ENV"}):
                with self.assertRaises(PreflightError):load_profile(root,demo)
                self.assertFalse(public_profiles(root,{})[0]["configured"])
            (root/".env.okx.demo.local").write_text("OKX_SITE=global\nOKX_ENV=live\n",encoding="utf-8")
            with self.assertRaises(PreflightError):load_profile(root,demo)


if __name__=="__main__": unittest.main()
