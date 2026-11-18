"""R2 (b): post-only entries (D096) in the PAPER engine and on the exchange executor. Synthetic candles in temp folders,
the fake exchange and temp roots only: no network, key, Jev or real order."""
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import Mock
import unittest
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
for _path in (str(ROOT), str(ROOT / "tools"), str(ROOT / "tests")):
    if _path not in sys.path:
        sys.path.insert(0, _path)
import team_data
import team_eval
from test_strategy_v4_runtime import T0, payload, write_store
import test_exchange_lab as fixture
from test_exchange_eth import ETH, contracts_named, eth_payload
from trading_lab import arena_engine, exchange_engine as engine, exchange_execution as policy, match_protocol, strategy_v4, strategy_v4_runtime as v4rt
from trading_lab.exchange_api import AMEND_ID, CLIENT_ID
from trading_lab.okx_readonly import PreflightError

MAKER = {"type": "post_only", "offset": {"bps": 5}, "timeout_seconds": 600}


def recipe_of(**rules):
    return v4rt.engine_recipe(strategy_v4.validate_payload(payload(**rules)))


def seconds(event):
    return datetime.fromisoformat(event["at"]).timestamp()


def last(snapshot):
    return float(snapshot["raw"]["ticker"][0]["last"])


class PaperPostOnlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = write_store(Path(self.tmp.name) / "BTC-USDT-SWAP", 6 * 60)

    def tape(self, start, length):
        rows = team_eval.RowFeed(self.store)
        try:
            store = team_eval.row_store(rows.slice(T0 * 1000, (start + length + 120) * 1000), team_data.load_funding(self.store),
                                        team_eval.DEFAULT_INSTRUMENT)
        finally:
            rows.close()
        tape = store.tape(start, start + length)
        for snapshot in tape:  # the rule always holds: one decision, then the order, on every visible bar while flat
            close = (snapshot["at"] - v4rt.VISIBILITY_SECONDS) // 60 * 60
            snapshot["v4_features"] = {"bar_close_ms": int(close) * 1000, "values": {"return_bps@1m:5": -20.0}}
        return tape

    def season(self, recipe, start=T0 + 3600, length=1800, admission=None, stop_after_placement=False, watch=None):
        tape = self.tape(start, length)
        state = arena_engine.new_season([recipe], 100, admission or length - 600, tape[0]["at"], "test")
        for snapshot in tape:
            if state["status"] not in {"RUNNING", "DRAINING", "CLOSING"}:
                break
            arena_engine.tick(state, snapshot)
            if watch:
                watch(state)
            if stop_after_placement and state["bots"][0].get("_maker") and not state["bots"][0]["stop_requested"]:
                arena_engine.request_stop(state, snapshot["at"])
        return state, {s["sha256"]: s for s in tape}

    def events(self, state, code):
        return [e for e in state["events"] if e["code"] == code]

    def test_the_limit_rests_and_fills_only_once_the_path_passes_it(self):
        state, by_hash = self.season(recipe_of(order=MAKER))
        placed, fills = self.events(state, "entry_order_placed"), self.events(state, "paper_entry_fill")
        self.assertTrue(placed and fills)
        order, fill = placed[0]["data"], fills[0]["data"]
        limit = order["limit_usdt"]
        intent = [e for e in self.events(state, "entry_intent") if seconds(e) < seconds(placed[0])][-1]
        decision = by_hash[intent["data"]["snapshot_sha256"]]
        bar = decision["v4_features"]["bar_close_ms"] - 60_000
        close = next(float(r[4]) for r in decision["raw"]["candles"] if int(r[0]) == bar)
        self.assertEqual(limit, strategy_v4.limit_price(recipe_of(order=MAKER)["rules"], close, {}, .1, "long"))  # the decision's bar
        self.assertEqual((fill["price_usdt"], fill["order_type"]), (limit, "post_only"))
        through = max(limit / 10000, .2)
        placed_at, filled_at = seconds(placed[0]), seconds(fills[0])
        self.assertLess(placed_at, filled_at)
        self.assertLessEqual(filled_at - placed_at, MAKER["timeout_seconds"])
        resting = [s for s in by_hash.values() if placed_at < s["at"] < filled_at]
        self.assertTrue(all(last(s) > limit - through for s in resting))
        self.assertLessEqual(last(by_hash[fill["snapshot_sha256"]]), limit - through)
        self.assertAlmostEqual(fill["fee_usdt"], fill["quantity_btc"] * limit * .0002, places=9)
        self.assertAlmostEqual(fill["adverse_selection_usdt"], fill["quantity_btc"] * limit * .0002, places=9)
        self.assertFalse(self.events(state, "entry_order_rejected"))

    def test_an_unfilled_limit_expires_and_the_next_decision_waits_for_a_later_bar(self):
        far = {"type": "post_only", "offset": {"bps": 300}, "timeout_seconds": 120}  # the swings never reach 300 bps
        state, _ = self.season(recipe_of(order=far))
        placed, expired = self.events(state, "entry_order_placed"), self.events(state, "entry_order_expired")
        self.assertFalse(self.events(state, "paper_entry_fill"))
        self.assertTrue(placed)
        self.assertIn(len(placed) - len(expired), (0, 1))  # the last order may still rest when the tape ends
        first, gone = seconds(placed[0]), seconds(expired[0])
        self.assertEqual(gone, first + 125)  # the first quote after the expiry; the quote at the expiry still could fill
        later = [seconds(e) for e in self.events(state, "entry_checks") if seconds(e) > gone]
        self.assertTrue(later)
        bar_close = (later[0] - v4rt.VISIBILITY_SECONDS) // 60 * 60
        self.assertGreaterEqual(bar_close + v4rt.VISIBILITY_SECONDS, first + 120)  # visible from the expiry on, as the evaluator

    def test_stop_cancels_a_resting_order(self):
        far = {"type": "post_only", "offset": {"bps": 300}, "timeout_seconds": 600}
        state, _ = self.season(recipe_of(order=far), stop_after_placement=True)
        self.assertEqual(len(self.events(state, "entry_order_cancelled")), 1)
        self.assertEqual(state["status"], "DONE")
        self.assertFalse(self.events(state, "paper_entry_fill"))

    def test_the_admission_cutoff_leaves_a_resting_order_until_its_own_expiry(self):
        far = {"type": "post_only", "offset": {"bps": 300}, "timeout_seconds": 600}
        probe, _ = self.season(recipe_of(order=far))
        placed_at = seconds(self.events(probe, "entry_order_placed")[0])
        seen = []
        state, _ = self.season(recipe_of(order=far), admission=int(placed_at - (T0 + 3600)) + 60,
                               watch=lambda s: seen.append((s["status"], bool(s["bots"][0].get("_maker")))))
        self.assertIn(("DRAINING", True), seen)  # still resting after the cutoff
        self.assertEqual(len(self.events(state, "entry_order_placed")), 1)  # and never a new order after it
        self.assertEqual(len(self.events(state, "entry_order_expired")), 1)
        self.assertEqual(state["status"], "DONE")

    def test_crossing_and_unknown_limits_place_nothing(self):
        recipe = recipe_of(order=MAKER)
        state = arena_engine.new_season([recipe], 100, 600, T0, "test")
        bot = state["bots"][0]
        snapshot = {"at": T0 + 10, "bid": 80000.0, "ask": 80000.1, "lot_btc": .0001, "min_btc": .0001, "sha256": "0" * 64}
        for plan, reason in (({"limit": 80000.1, "through": 8.0, "tick": .1, "bar_close_ms": 0, "timeout_seconds": 600}, "post_only_would_cross"),
                             (None, "post_only_limit_unknown")):
            arena_engine.place_maker(state, bot, recipe, snapshot, {"maker": plan, "signal_intent": "paper-1"})
            self.assertEqual(self.events(state, "entry_order_rejected")[-1]["data"]["reason"], reason)
            self.assertIsNone(bot.get("_maker"))

    def test_an_unreadable_path_price_never_fills(self):
        order = {"limit": 100.0, "through": 1.0, "side": "long"}
        self.assertFalse(v4rt.maker_filled(order, {"raw": {"ticker": [{"last": ""}]}}))
        self.assertFalse(v4rt.maker_filled(order, {}))
        self.assertTrue(v4rt.maker_filled(order, {"raw": {"ticker": [{"last": "99"}]}}))
        self.assertFalse(v4rt.maker_filled({**order, "side": "short"}, {"raw": {"ticker": [{"last": "100.9"}]}}))


class RestingFake(fixture.FakeExchange):
    """The fake exchange with post-only orders that rest until the test fills or the client cancels them."""
    def transport(self, request):
        route, method = urlsplit(request.full_url), request.get_method()
        if route.path == "/api/v5/account/positions" and getattr(self, "before_positions", None):
            hook, self.before_positions = self.before_positions, None
            hook()
        if route.path == "/api/v5/market/ticker" and getattr(self, "before_ticker", None):  # the step's market snapshot
            hook, self.before_ticker = self.before_ticker, None
            hook()
        if route.path == "/api/v5/trade/order" and method == "GET" and getattr(self, "fail_order_gets", 0):
            self.fail_order_gets -= 1
            raise PreflightError("network_error")
        if route.path == "/api/v5/trade/order" and method == "POST" and json.loads(request.data)["ordType"] == "post_only":
            self.requests.append(request)
            body = json.loads(request.data)
            if body["clOrdId"] in self.orders: raise AssertionError("Duplicate order POST")
            price = Decimal(body["px"])
            crossing = price >= self.ask if body["side"] == "buy" else price <= self.bid
            ord_id = str(100000 + len(self.orders))
            self.orders[body["clOrdId"]] = {**body, "ordId": ord_id, "state": "canceled" if crossing else "live", "accFillSz": "0",
                                            "avgPx": "", "fee": "0", "feeCcy": "USDT", "pnl": "0", "rebate": "0",
                                            "uTime": str(int(self.clock() * 1000))}
            return {"code": "0", "data": [{"sCode": "0", "clOrdId": body["clOrdId"], "ordId": ord_id}]}
        if route.path == "/api/v5/account/config" and getattr(self, "fail_config", 0):
            self.fail_config -= 1
            raise PreflightError("network_error")
        if route.path == "/api/v5/trade/cancel-order":
            self.requests.append(request)
            body = json.loads(request.data)
            if getattr(self, "on_cancel", None):  # e.g. a fill that lands while the cancel is in flight
                hook, self.on_cancel = self.on_cancel, None
                hook()
            if getattr(self, "fail_cancels", 0):
                self.fail_cancels -= 1
                raise PreflightError("network_error")
            order = self.orders.get(body["clOrdId"])
            if order is None or order["state"] not in {"live", "partially_filled"}:  # filled, cancelled or never created (OKX: 51400)
                return {"code": "1", "data": [{"sCode": "51400", "clOrdId": body["clOrdId"]}]}
            order.update(state="canceled", uTime=str(int(self.clock() * 1000)))
            return {"code": "0", "data": [{"sCode": "0", "clOrdId": body["clOrdId"]}]}
        if route.path == "/api/v5/trade/orders-pending":
            self.requests.append(request)
            return {"code": "0", "data": [{k: o[k] for k in ("instId", "clOrdId", "ordId", "side", "posSide", "tdMode", "ordType", "sz",
                                                            "reduceOnly", "px", "state", "accFillSz")}
                                          for o in self.orders.values() if o["state"] in {"live", "partially_filled"}]}
        return super().transport(request)

    def fill(self, identity, contracts):
        """A maker fill of the resting order at its limit: position, cash, a bill and the cumulative order facts."""
        order, q = self.orders[identity], Decimal(contracts)
        price, value = Decimal(order["px"]), Decimal(self.spec["ctVal"])
        side = Decimal(1) if order["side"] == "buy" else Decimal(-1)
        self.basis = (abs(self.units) * self.basis + q * price) / (abs(self.units) + q)
        self.units += side * q
        fee = -q * value * price * Decimal(".0002")
        self.cash += fee
        total = Decimal(order["accFillSz"]) + q
        order.update(accFillSz=str(total), avgPx=str(price), fee=str(Decimal(order["fee"]) + fee),
                     state="filled" if total == Decimal(order["sz"]) else "partially_filled", uTime=str(int(self.clock() * 1000)))
        self.bills.append({"billId": str(len(self.bills) + 1), "ts": str(int(self.clock() * 1000)), "type": "2", "subType": "3",
                           "ccy": "USDT", "instId": self.spec["instId"], "ordId": order["ordId"], "pnl": "0", "fee": str(fee),
                           "balChg": str(fee), "posBalChg": "0"})


class LivePostOnlyTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.ExchangeTests(methodName="runTest")
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        for fake in self.fx.fakes.values():
            fake.__class__ = RestingFake
            fake.use(fixture.ETH_SPEC, 3000, 0)  # flat closes: the 5 bps limit rests below the touch
        self.fx.lab.jev_factory = Mock(side_effect=AssertionError("No Jev construction for RULES_ONLY"))
        self.fake = self.fx.fakes["demo"]

    def start(self, order=MAKER, duration=300):
        fx, lab = self.fx, self.fx.lab
        version = fx.workshop.add_version(fx.workshop.data["generation"], eth_payload(order=order, size_pct=20, max_hold_seconds=3600))["id"]
        result = lab.start(lab.data["generation"], "workshop_demo", [version], ["demo"], 100, duration, execution_mode=policy.RULES_ONLY)
        self.state = lab.load(result["id"])
        self.state.update(fixture_only=True, evidence_kind="SYNTHETIC")
        lab.prepare(self.state, lab.chosen_profiles(["demo"]), False)
        self.bot = self.state["bots"][0]
        return self.state

    def step(self, seconds=5):
        self.fx.now += seconds
        self.fx.lab.step(self.state)

    def place(self):
        for _ in range(60):  # the book warms up from fake history, then one decision per visible minute
            self.fx.lab.step(self.state)
            if self.fake.orders:
                return next(iter(self.fake.orders.items()))
            self.fx.now += 5
        self.fail("no order")

    def codes(self, code):
        return [e for e in self.state["events"] if e["code"] == code]

    def cancels(self):
        return [r for r in self.fake.requests if urlsplit(r.full_url).path == "/api/v5/trade/cancel-order"]

    def test_the_battle_zip_of_an_eth_season_keeps_its_market_rows(self):
        # Demo 9b6289 (R2.1.2): create_archive rebuilt every recorded row as BTC, so all ETH rows were silently omitted
        # (market_rows_omitted 38, an empty market.jsonl), and a Demo row's unknown_schedule funding was not accepted.
        import zipfile
        from trading_lab.battle_export import create_archive, verify_archive
        from trading_lab.match_journal import journal_copy
        self.start()
        self.place()
        lab, identity = self.fx.lab, self.state["id"]
        source = lab.path / (identity + ".snapshots.jsonl")
        recorded = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
        self.assertGreater(len(recorded), 5)
        for row in recorded[:2]:  # a Demo venue without a funding schedule, as on OKX Demo
            row["funding_status"] = "unknown_schedule"
        source.write_text("".join(json.dumps(row) + "\n" for row in recorded), encoding="utf-8")
        result = create_archive(journal_copy(lab, identity), snapshots_path=source)
        self.addCleanup(result.close)
        self.assertTrue(verify_archive(result)["verified"])
        result.seek(0)
        with zipfile.ZipFile(result) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            rows = [json.loads(line) for line in archive.read("market.jsonl").splitlines()]
        self.assertEqual((manifest["market_rows_omitted"], manifest["market_samples"]), (0, len(recorded)))
        self.assertEqual([r["capture_at"] for r in rows], [r["at"] for r in recorded])
        self.assertGreater(min(r["bid"] for r in rows), 1000)  # ETH prices of the season, not a BTC rebuild

    def test_the_run_view_pins_the_journal_head_and_the_first_event_pins_code_and_manifest(self):
        # Demo 9b6289 review: an export through JSON.stringify turns whole floats into ints, so only some row hashes
        # replay; the chain head and the code were pinned nowhere in the exported view.
        import hashlib
        self.start()
        self.place()
        lab, identity = self.fx.lab, self.state["id"]
        view = lab.history(identity)
        receipt = view["journal_receipt"]
        folder = lab.path / (identity + ".events.segments")
        files = sorted(folder.glob("segment-*.jsonl"))
        self.assertEqual([s["sha256"] for s in receipt["segments"]], [hashlib.sha256(f.read_bytes()).hexdigest() for f in files])
        last = json.loads(files[-1].read_bytes().splitlines()[-1])
        self.assertEqual((receipt["last_seq"], receipt["last_event_sha256"]), (last["seq"], last["sha256"]))
        reserved = next(e for e in view["events"] if e["code"] == "exchange_reserved")["data"]
        self.assertEqual((reserved["code_sha256"], reserved["match_manifest_sha256"]),
                         (self.state["code_sha256"], self.state["match_manifest_sha256"]))

    def test_a_stop_without_a_position_says_so_and_a_season_without_a_fill_has_no_winner(self):
        from trading_lab.exchange_lab import winner_name
        self.start()
        with self.fx.lab.lock:
            engine.stop(self.state, self.fx.now)
        self.assertIn("позиції немає", self.codes("stop_requested")[-1]["message"])
        for _ in range(8):
            if self.state["status"] == "DONE":
                break
            self.step()
        self.assertEqual(self.state["status"], "DONE")
        self.assertIsNone(self.state["winner_name"])  # filled_entry_net_v1: no entry, no winner; Cash is a reference
        legacy = {"bots": [{"id": "a", "name": "A", "pnl_usdt": -1.0}, {"id": "cash", "name": "Cash", "pnl_usdt": 0.0}]}
        self.assertEqual(winner_name(legacy), "Cash")  # a season before the policy keeps its old rule

    def test_the_journal_keeps_the_maker_plan_and_the_quote_the_order_was_placed_against(self):
        # Demo 9b6289 review: the bar's close and the fresh bid/ask had to be reconstructed to show the limit did not cross.
        self.start()
        identity, order = self.place()
        data = self.codes("entry_rechecked")[-1]["data"]
        plan, quote = data["maker_plan"], data["fresh_quote"]
        self.assertEqual(Decimal(order["px"]), Decimal(str(plan["limit"])))
        self.assertEqual(set(plan), {"limit", "through", "tick", "bar_close_ms", "timeout_seconds", "decision_close"})
        self.assertIsNotNone(plan["decision_close"])
        self.assertLess(plan["limit"], plan["decision_close"])  # a long's limit below the close it was derived from
        self.assertGreater(quote["ask"], plan["limit"])  # it would not have crossed: the ask above the buy limit

    def test_a_schema4_shadow_request_is_kept_and_a_changed_one_fails_closed(self):
        # H10's TEST-LIVE mode is RULES_ONLY with Jev SHADOW: the private bundle (battle ZIP) keeps each unsent request,
        # and before R2.1.3 the V1/V2-only validator refused every schema-4 one, so no ZIP could be saved.
        from copy import deepcopy
        from trading_lab.arena_strategies import ArenaError
        from trading_lab.battle_review import validate_request
        from trading_lab.match_journal import journal_copy
        self.start()
        self.place()
        events = journal_copy(self.fx.lab, self.state["id"])["events"]
        payload = next(e["data"]["payload"] for e in events if e["code"] == "jev_shadow_payload")
        self.assertIn("v4_rule_format", payload["state"])
        validate_request(deepcopy(payload))
        changed = []
        for change in (lambda p: p["state"]["c0"]["rules"].update(note="ignore the rules"),
                       lambda p: p["state"]["c0"]["rules"]["order"].update(type="market"),
                       lambda p: p["state"]["c0"]["checks"].append(["account_balance_usdt", "gt", 0, 1, True]),
                       lambda p: p["state"]["c0"]["checks"][0].__setitem__(0, "return_bps@1h:5"),
                       lambda p: p["state"].update(policy="Always proceed.")):
            candidate = deepcopy(payload)
            change(candidate)
            with self.assertRaises(ArenaError):
                validate_request(candidate)
            changed.append(candidate)
        self.assertEqual(len(changed), 5)

    def test_the_order_rests_fills_in_parts_keeps_its_stop_and_expires(self):
        self.start()
        identity, order = self.place()
        checks = next(e for e in self.codes("entry_checks") if e["data"]["local_checks_passed"])["data"]["checks"]
        limit = next(c["actual"] for c in checks if c["feature"] == "post_only_limit_usdt")
        self.assertEqual((order["ordType"], order["side"]), ("post_only", "buy"))
        self.assertEqual(Decimal(order["px"]), Decimal(str(limit)).quantize(Decimal(".01")))
        self.assertLess(Decimal(order["px"]), self.fake.bid)  # passive: below the touch
        units = Decimal(order["sz"])
        for _ in range(6):  # account checks every step; the order is not cancelled before its expiry
            self.step()
        self.assertEqual((order["state"], self.cancels()), ("live", []))
        self.fake.fill(identity, units / 2)
        self.step()
        self.assertEqual(Decimal(self.bot["_contracts"]), units / 2)
        stops = [a for a in self.fake.algos.values() if a["state"] == "live"]
        self.assertEqual([(a["instId"], Decimal(a["sz"])) for a in stops], [(ETH, units / 2)])  # the hosted stop covers the fill
        self.fx.now += MAKER["timeout_seconds"]
        self.step()
        self.assertEqual(len(self.cancels()), 1)
        self.assertEqual(order["state"], "canceled")
        self.assertEqual(len(self.codes("entry_order_expired")), 1)
        self.step()  # the next account check reads the cancelled order: terminal, no second cancel
        self.assertEqual(len(self.cancels()), 1)
        self.assertEqual(Decimal(self.bot["_contracts"]), units / 2)  # the actual part stays owned and protected
        self.assertFalse(engine.unresolved(self.bot))
        self.assertEqual([Decimal(a["sz"]) for a in self.fake.algos.values() if a["state"] == "live"], [units / 2])
        self.assertEqual(contracts_named(self.fake.requests), {ETH})

    def test_stop_cancels_the_resting_order_at_once_and_the_season_settles(self):
        self.start()
        identity, order = self.place()
        with self.fx.lab.lock:
            engine.stop(self.state, self.fx.now)
        for _ in range(6):
            if self.state["status"] == "DONE":
                break
            self.step()
        self.assertEqual(len(self.cancels()), 1)
        self.assertEqual((order["state"], self.state["status"]), ("canceled", "DONE"))
        self.assertTrue(self.bot["accounting_reconciled"])

    def test_a_crossing_limit_is_never_sent(self):
        self.fake.bid, self.fake.ask = Decimal("2950"), Decimal("2950.01")  # far below the bars' closes: L would take liquidity
        self.start()
        for _ in range(40):
            self.step()
        self.assertFalse(self.fake.orders)
        self.assertIn("post_only_would_cross", [e["data"]["reason"] for e in self.codes("entry_cancelled")])

    def test_a_fill_between_the_order_and_position_reads_is_not_a_mismatch(self):
        self.start()
        identity, order = self.place()
        self.fake.before_positions = lambda: self.fake.fill(identity, Decimal(order["sz"]))  # after the order GET, before positions
        self.step()
        self.assertFalse(self.bot["_manual_review"])
        self.assertFalse(self.codes("exchange_check_failed"))
        self.assertEqual(Decimal(self.bot["_contracts"]), Decimal(order["sz"]))

    def test_recovery_after_a_restart_cancels_the_resting_rest_and_closes_only_the_owned_part(self):
        self.start()
        identity, order = self.place()
        self.fake.fill(identity, Decimal(order["sz"]) / 2)
        self.step()
        self.assertEqual(Decimal(self.bot["_contracts"]), Decimal(order["sz"]) / 2)
        self.fx.lab.save(self.state)
        self.fx.lab.resources.pop(self.state["id"])["stack"].close()  # the process stops with the order resting
        self.assertTrue(self.fx.lab.resume(self.fx.lab.data["generation"], self.state["id"])["close_only"])
        self.assertEqual(self.codes("stop_requested")[-1]["data"]["origin"], "recovery")  # D-5: not the owner's Stop
        self.fx.lab.prepare(self.state, self.fx.lab.chosen_profiles(["demo"]), True)
        for _ in range(6):
            if self.state["status"] == "DONE":
                break
            self.step()
        self.assertEqual((order["state"], self.state["status"], self.fake.units), ("canceled", "DONE", 0))
        self.assertEqual(len(self.cancels()), 1)
        self.assertEqual(sum(not o["reduceOnly"] for o in self.fake.orders.values()), 1)  # no new entry in recovery
        self.assertTrue(self.bot["accounting_reconciled"])

    def test_a_cancel_that_races_a_fill_reads_the_order_instead_of_failing(self):
        self.start()
        identity, order = self.place()
        self.fake.on_cancel = lambda: self.fake.fill(identity, Decimal(order["sz"]))  # fills while our cancel is in flight
        with self.fx.lab.lock:
            engine.stop(self.state, self.fx.now)
        self.step()
        self.assertEqual(len(self.cancels()), 1)  # the venue answered 51400: already filled
        self.assertFalse(self.codes("exchange_check_failed"))
        self.assertEqual(order["state"], "filled")
        closes = [o for o in self.fake.orders.values() if o["reduceOnly"]]
        self.assertEqual([(o["side"], Decimal(o["sz"])) for o in closes], [("sell", Decimal(order["sz"]))])  # Stop closed the whole fill
        for _ in range(6):
            if self.state["status"] == "DONE":
                break
            self.step()
        self.assertEqual((self.state["status"], self.fake.units), ("DONE", 0))  # the actual fill is closed, not lost

    def test_a_failed_cancel_is_repeated_until_the_order_is_terminal(self):
        self.start()
        identity, order = self.place()
        self.fake.fail_cancels = 1
        self.fx.lab.stop(self.fx.lab.data["generation"], self.state["id"])
        self.step()
        self.assertEqual((len(self.cancels()), order["state"]), (1, "live"))
        self.assertEqual(len(self.codes("entry_order_cancel_failed")), 1)
        self.assertEqual(self.codes("stop_requested")[-1]["data"]["origin"], "owner")  # D-5
        for _ in range(6):
            if self.state["status"] == "DONE":
                break
            self.step()
        self.assertEqual(len(self.cancels()), 2)
        self.assertEqual((order["state"], self.state["status"]), ("canceled", "DONE"))

    def test_a_resume_gets_a_fresh_cancel_allowance_after_a_minute_of_failures(self):
        self.start()
        identity, order = self.place()
        self.fake.fail_cancels = 10 ** 6  # the venue refuses every cancel until the owner's Resume
        self.fx.lab.stop(self.fx.lab.data["generation"], self.state["id"])
        for _ in range(40):
            if self.state["status"] == "PAUSED":
                break
            self.step()
        self.assertEqual((self.state["status"], order["state"]), ("PAUSED", "live"))
        self.assertEqual(len(self.cancels()), 13)  # bounded within one worker run: 12 retries and the last chance at the pause
        self.fake.fail_cancels = 0
        self.fx.lab.save(self.state)
        self.fx.lab.resources.pop(self.state["id"])["stack"].close()
        self.fx.lab.resume(self.fx.lab.data["generation"], self.state["id"])
        self.fx.lab.prepare(self.state, self.fx.lab.chosen_profiles(["demo"]), True)
        for _ in range(6):
            if self.state["status"] == "DONE":
                break
            self.step()
        self.assertEqual((order["state"], self.state["status"]), ("canceled", "DONE"))

    def test_the_last_cancel_before_a_pause_skips_the_retry_spacing(self):
        self.start()
        identity, order = self.place()
        self.fake.fail_cancels = 10 ** 6
        self.fx.lab.stop(self.fx.lab.data["generation"], self.state["id"])
        stopped = self.fx.now
        while self.fx.now + 5 - stopped < 120:  # until the step that reaches the 120 s closing limit
            self.step()
        self.assertEqual(len(self.cancels()), 12)  # this run's allowance is spent
        self.fake.fail_cancels = 0  # the venue recovers just as the season pauses
        self.step()
        self.assertEqual(len(self.cancels()), 13)  # the last chance: one attempt beyond the allowance, no spacing
        self.assertEqual(self.state["status"], "PAUSED")
        self.assertEqual(order["state"], "canceled")
        self.assertEqual(self.codes("entry_order_unwatched_cancel")[-1]["data"]["order_state"], "canceled")

    def test_the_unwatched_cancel_never_raises_and_a_handed_over_workspace_sends_nothing(self):
        from unittest.mock import patch
        self.start()
        identity, order = self.place()
        api = self.fx.lab.resources[self.state["id"]]["apis"][0]
        marker = self.fx.root / "runtime/server_handoff.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}", encoding="utf-8")
        self.fx.lab.cancel_resting(self.state, self.bot, api, "paused", final=True)
        self.assertEqual((self.cancels(), order["state"]), ([], "live"))
        marker.unlink()
        with patch.object(self.fx.lab, "save", side_effect=OSError("disk")):
            self.fx.lab.cancel_resting(self.state, self.bot, api, "paused", final=True)  # a journal fault: still sent, no raise
        self.assertEqual((len(self.cancels()), order["state"]), (1, "canceled"))

    def test_after_an_unfilled_expiry_the_next_decision_waits_for_a_bar_visible_from_it(self):
        self.start({"type": "post_only", "offset": {"bps": 5}, "timeout_seconds": 120}, duration=1800)
        identity, order = self.place()
        self.fake.fail_cancels = 1  # the expiry cancel fails once and is repeated
        expires = next(o for o in self.bot["_orders"] if o["request"]["clOrdId"] == identity)["expires_at"]
        self.fx.now = expires
        self.step()
        self.assertEqual((len(self.codes("entry_order_expired")), len(self.codes("entry_order_cancel_failed"))), (1, 1))
        self.assertEqual(self.bot["_next_decision"], expires)
        for _ in range(40):
            self.step()
            if len(self.fake.orders) == 2:
                break
        self.assertEqual(order["state"], "canceled")
        self.assertEqual(len(self.fake.orders), 2)
        self.assertGreaterEqual(self.bot["_v4"]["decided_bar_ms"] / 1000 + v4rt.VISIBILITY_SECONDS, expires)

    def test_manual_review_cancels_the_resting_order(self):
        self.start()
        identity, order = self.place()
        self.fake.units = Decimal("-0.05")  # a foreign short appears while our buy rests: never a fill of ours
        self.step()
        self.assertTrue(self.bot["_manual_review"])
        self.assertEqual(order["state"], "canceled")
        self.assertEqual(self.codes("entry_order_unwatched_cancel")[-1]["data"]["order_state"], "canceled")

    def test_three_failed_account_checks_pause_and_cancel_the_resting_order(self):
        self.start()
        identity, order = self.place()
        self.fake.fail_config = 3
        for _ in range(3):
            self.step()
        self.assertEqual(self.state["status"], "PAUSED")
        self.assertEqual(order["state"], "canceled")
        self.assertEqual(self.codes("entry_order_unwatched_cancel")[-1]["data"]["reason"], "paused")

    def test_a_same_side_position_the_order_never_explains_is_not_a_lag_for_long(self):
        self.start()
        identity, order = self.place()
        self.fake.units = Decimal("0.01")  # same side, inside the resting size, but the order's GET never shows a fill
        self.step()
        self.step()
        self.assertFalse(self.bot["_manual_review"])
        self.assertEqual(len(self.codes("fill_read_lag")), 2)
        self.step()
        self.assertTrue(self.bot["_manual_review"])  # the third check: not a lag
        self.assertEqual(order["state"], "canceled")

    def test_resizing_the_stop_uses_its_own_request_id_and_the_quote_age_is_logged(self):
        self.start()
        identity, order = self.place()
        self.assertGreaterEqual(self.codes("entry_rechecked")[-1]["data"]["quote_age_seconds"], 0)  # D-4
        half = Decimal(order["sz"]) / 2
        self.fake.fill(identity, half)
        self.step()
        self.fake.fill(identity, half)
        self.step()
        self.assertEqual([Decimal(a["sz"]) for a in self.fake.algos.values() if a["state"] == "live"], [2 * half])
        amends = [json.loads(r.data)["reqId"] for r in self.fake.requests if urlsplit(r.full_url).path == "/api/v5/trade/amend-algos"]
        self.assertTrue(amends and all(AMEND_ID.fullmatch(i) and i[-5] == "A" and not CLIENT_ID.fullmatch(i) for i in amends))  # D-6

    def test_the_season_counts_the_order_timeout_in_its_capacity(self):
        recipe = recipe_of(order={"type": "post_only", "offset": {"bps": 5}, "timeout_seconds": 3600}, max_hold_seconds=300)
        taker = recipe_of(max_hold_seconds=300)
        self.assertEqual(match_protocol.capacities(300, [recipe])["max_observations"], -(-(300 + 300 + 3600 + 120) // 5) + 128)
        self.assertEqual(match_protocol.capacities(300, [taker])["max_observations"], -(-(300 + 300 + 120) // 5) + 128)


class FastProtectionTests(unittest.TestCase):
    """R2.1: a passive fill is protected between steps; the owned stop, every POST, the bills and the fill times are in
    the protected journal; hold and target count from the venue's fill time."""
    setUp, start, step, place, codes, cancels = (LivePostOnlyTests.setUp, LivePostOnlyTests.start, LivePostOnlyTests.step,
                                                 LivePostOnlyTests.place, LivePostOnlyTests.codes, LivePostOnlyTests.cancels)

    def live_stops(self):
        return [Decimal(a["sz"]) for a in self.fake.algos.values() if a["state"] == "live"]

    def test_a_passive_fill_is_protected_within_one_watch_slice(self):
        self.start()
        identity, order = self.place()
        resource = self.fx.lab.resources[self.state["id"]]
        slices = []
        resource["wakeup"].wait = lambda seconds: slices.append(seconds) or False  # no real sleep; one slice per call
        self.fake.fill(identity, Decimal(order["sz"]) / 2)  # fills on the venue right after a step
        self.fx.lab.idle(self.state, resource, 5)
        self.assertEqual(slices[0], 1)
        self.assertEqual(self.live_stops(), [Decimal(order["sz"]) / 2])  # protected without a full account check
        event = self.codes("fill_protected")[-1]["data"]
        self.assertEqual((event["seconds_from_observation"], event["seconds_from_venue_fill"]), (0, 0))
        self.assertEqual((event["target_seconds"], event["within_target"]), (5, True))
        self.assertEqual(event["seen_by"], "watch_slice")  # R2.1.3: the path that saw the fill, and the reads so far
        self.assertEqual(self.codes("exchange_entry_fill")[-1]["data"]["seen_by"], "watch_slice")
        self.assertGreaterEqual(event["resting_reads"], 2)  # the account check's and this slice's reads at least
        self.fake.fill(identity, Decimal(order["sz"]) / 2)
        self.fx.lab.watch_fills(self.state)
        self.assertEqual(self.live_stops(), [Decimal(order["sz"])])  # the second fill: the stop is resized at once
        self.assertEqual(len(self.codes("fill_protected")), 2)

    def fire_stop(self, contracts):
        """The venue triggers our live stop for `contracts`: the algo turns effective with a filled market child."""
        parent = next(a for a in self.fake.algos.values() if a["state"] == "live")
        price, value = self.fake.bid, Decimal(self.fake.spec["ctVal"])
        gross, fee = contracts * value * (price - self.fake.basis), -contracts * value * price * Decimal(".0005")
        parent.update(state="effective", ordIdList=["100099"])
        self.fake.orders["stop-child"] = {
            "instId": self.fake.spec["instId"], "tdMode": "isolated", "posSide": "net", "side": "sell", "ordType": "market",
            "reduceOnly": True, "sz": str(contracts), "clOrdId": "", "ordId": "100099", "algoId": parent["algoId"],
            "state": "filled", "accFillSz": str(contracts), "avgPx": str(price), "fee": str(fee), "feeCcy": "USDT",
            "pnl": str(gross), "rebate": "0", "uTime": str(int(self.fx.now * 1000))}
        self.fake.units -= contracts
        self.fake.cash += gross + fee
        self.fake.bills.append({"billId": "100099", "ts": str(int(self.fx.now * 1000)), "type": "2", "subType": "3", "ccy": "USDT",
                                "instId": self.fake.spec["instId"], "ordId": "100099", "pnl": str(gross), "fee": str(fee),
                                "balChg": str(gross + fee), "posBalChg": "0"})

    def test_a_stop_that_triggered_meanwhile_is_taken_in_before_the_watch_arms_the_rest(self):
        self.start()
        identity, order = self.place()
        half = Decimal(order["sz"]) / 2
        self.fake.fill(identity, half)
        self.step()
        self.fake.fill(identity, half)  # the rest fills as the price trades through the limit ...
        self.fire_stop(half)  # ... and the stop on the first half triggers in the same move
        self.fx.now += 1
        self.fx.lab.watch_fills(self.state)
        self.assertFalse(self.codes("fill_watch_failed"))
        self.assertFalse(self.bot["stop_requested"] or self.state["incomplete"])
        self.assertEqual((Decimal(self.bot["_contracts"]), self.live_stops()), (half, [half]))  # the child adopted, the rest armed
        self.assertEqual(self.codes("protective_terminal")[-1]["data"]["venue_state"], "effective")

    def test_the_watch_keeps_the_step_cadence_and_writes_only_on_a_change(self):
        self.start()
        identity, order = self.place()
        resource = self.fx.lab.resources[self.state["id"]]
        gets = lambda: sum(urlsplit(r.full_url).path == "/api/v5/trade/order" and r.get_method() == "GET" for r in self.fake.requests)
        def slow_wait(seconds):
            self.fx.now += seconds + 2  # each slice plus a slow venue answer, as on Demo
            return False
        resource["wakeup"].wait = slow_wait
        saves = []
        original = self.fx.lab.save
        self.fx.lab.save = lambda *a, **k: saves.append(1) or original(*a, **k)
        before, started = gets(), self.fx.now
        self.fx.lab.idle(self.state, resource, 5)
        self.assertLessEqual(self.fx.now - started, 5 + 3)  # bounded by time, not by five slow slices
        self.assertGreaterEqual(gets() - before, 1)
        self.assertEqual(saves, [])  # the order did not change: no checkpoint write

    def test_the_bills_evidence_comes_in_bounded_parts_once_per_distinct_set(self):
        from unittest.mock import patch
        from trading_lab import exchange_lab
        patcher = patch.object(exchange_lab, "BILLS_PER_EVENT", 20)  # the fake serves one page of bills: parts of 20 here
        patcher.start()
        self.addCleanup(patcher.stop)
        self.start()
        identity, order = self.place()
        self.fake.fill(identity, Decimal(order["sz"]))
        self.step()
        for n in range(60):  # a longer season's bills: many fills and funding rows
            self.fake.bills.append({"billId": str(10 ** 6 + n), "ts": str(int(self.fx.now * 1000)), "type": "8", "subType": "173", "ccy": "USDT",
                                    "instId": self.fake.spec["instId"], "ordId": "", "pnl": "0", "fee": "0", "balChg": "0", "posBalChg": "0"})
        with self.fx.lab.lock:
            engine.stop(self.state, self.fx.now)
        for _ in range(8):
            if self.state["status"] == "DONE":
                break
            self.step()
        parts = [e["data"] for e in self.codes("accounting_evidence")]
        self.assertTrue(parts and all(len(p["bills"]) <= exchange_lab.BILLS_PER_EVENT for p in parts))
        self.assertEqual(sum(len(p["bills"]) for p in parts), len(self.fake.bills))  # one complete set, not repeated
        self.assertEqual([p["part"] for p in parts], list(range(1, parts[0]["parts"] + 1)))

    def test_a_fill_during_the_step_is_protected_before_the_step_ends(self):
        """R2.1.1: the step reads the order early (account check); a fill later in the same step is caught by its last read."""
        self.start()
        identity, order = self.place()
        self.fake.before_ticker = lambda: self.fake.fill(identity, Decimal(order["sz"]))  # after the account check's order read
        self.step()
        self.assertEqual(self.live_stops(), [Decimal(order["sz"])])  # armed within this step, not after the next idle slice
        self.assertTrue(self.codes("fill_protected")[-1]["data"]["within_target"])
        self.assertEqual(self.codes("fill_protected")[-1]["data"]["seen_by"], "step_end")

    def test_the_account_check_reads_the_resting_entry_first_and_arms_the_stop_before_its_other_gets(self):
        """Demo 9b6289: the check read the resting order only after seven GETs (identity, pending, five algo lists) and
        the stop waited for a position GET as well. Now its first read is the order, the stop comes right after it."""
        self.start()
        identity, order = self.place()
        self.fake.fill(identity, Decimal(order["sz"]))  # the venue fills between two steps
        first = len(self.fake.requests)
        self.step()
        paths = [urlsplit(r.full_url).path for r in self.fake.requests[first:]]
        paths = [p for p in paths if p != "/api/v5/public/time"]
        self.assertEqual(paths[0], "/api/v5/trade/order")  # the resting entry, before /account/config
        self.assertLess(paths.index("/api/v5/trade/order-algo"), paths.index("/api/v5/account/config"))
        fill = self.codes("exchange_entry_fill")[-1]["data"]
        self.assertEqual(fill["seen_by"], "account_check")
        self.assertGreaterEqual(fill["resting_reads"], 1)
        protected = self.codes("fill_protected")[-1]["data"]
        self.assertEqual(protected["seen_by"], "account_check")
        self.assertGreaterEqual(protected["resting_reads"], 1)
        from trading_lab.battle_review import build_review
        from trading_lab.match_journal import journal_copy
        review = build_review(journal_copy(self.fx.lab, self.state["id"]))  # the marker reaches the review and the ZIP
        row = next(e for e in review["events"] if e["code"] == "fill_protected")
        self.assertEqual(row["data"]["seen_by"], "account_check")
        self.assertGreaterEqual(Decimal(row["data"]["resting_reads"]), 1)  # review numbers are decimal strings

    def test_the_watch_cancels_at_the_expiry_and_times_the_cancel_post(self):
        self.start()
        identity, order = self.place()
        expires = next(o for o in self.bot["_orders"] if o["request"]["clOrdId"] == identity)["expires_at"]
        self.fx.now = expires + 1
        self.fx.lab.watch_fills(self.state)  # within a slice of the expiry, not at the next step
        self.assertEqual(len(self.codes("entry_order_expired")), 1)
        posted = self.codes("exchange_cancel_posted")[-1]["data"]
        self.assertEqual((posted["outcome"], posted["client_order_id"]), ("acknowledged", identity))
        self.assertGreaterEqual(posted["post_seconds"], 0)
        self.fx.lab.watch_fills(self.state)
        self.assertEqual(self.fake.orders[identity]["state"], "canceled")

    def test_a_test_live_season_of_a_schema_4_recipe_trades_one_lot(self):
        """G12: a recipe without the final proof trades 1 lot in the owner's Live, whatever its budget and size."""
        shop, lab = self.fx.workshop, self.fx.lab
        version = shop.add_version(shop.data["generation"], eth_payload(order=MAKER, size_pct=20, max_hold_seconds=3600))["id"]
        shop.train(shop.data["generation"], version, "synthetic-suite-v2", 100)
        shop.freeze(shop.data["generation"], version)
        promoted = shop.promote(shop.data["generation"], version)["id"]
        result = lab.start(lab.data["generation"], "arena_live", [promoted], ["arena-a"], 100, 300, execution_mode=policy.RULES_ONLY)
        self.state = lab.load(result["id"])
        self.state.update(fixture_only=True, evidence_kind="SYNTHETIC")
        lab.prepare(self.state, lab.chosen_profiles(["arena-a"]), False)
        self.bot, self.fake = self.state["bots"][0], self.fx.fakes["arena-a"]
        self.assertEqual((self.state["rules"]["test_live_max_lots"], self.state["match_manifest"]["risk_and_execution"]["test_live_max_lots"]), (1, 1))
        identity, order = self.place()
        self.assertEqual(Decimal(order["sz"]), Decimal("0.01"))  # 20 USDT would buy 0.06 contracts; G12 sends one lot
        api = lab.resources[self.state["id"]]["apis"][0]
        self.assertEqual(api.max_entry_units, Decimal("0.01"))
        body = {**{k: order[k] for k in ("instId", "tdMode", "posSide", "side", "ordType", "px", "reduceOnly", "stpMode")},
                "sz": "0.02", "clOrdId": order["clOrdId"][:-4] + "0099"}
        with self.assertRaisesRegex(PreflightError, "exchange_test_live_lot_cap"):
            api.validate_write("/api/v5/trade/order", body)  # the API refuses more than one lot on its own

    def test_the_one_lot_cap_binds_only_the_schema_4_bot_of_a_mixed_live_season(self):
        from test_strategy_v4_admission import payload as v4_payload
        from trading_lab.arena_strategies import catalogue_defaults
        from trading_lab.strategy_contract import payload_of
        shop, lab = self.fx.workshop, self.fx.lab
        for fake in self.fx.fakes.values():
            fake.use(fixture.SPEC, 84000, 0)  # a BTC season: frozen V1 beside a BTC schema-4 recipe
        promoted = []
        for item in (payload_of(catalogue_defaults()[0]), v4_payload(order=MAKER)):
            version = shop.add_version(shop.data["generation"], item)["id"]
            shop.train(shop.data["generation"], version, "synthetic-suite-v2", 100)
            shop.freeze(shop.data["generation"], version)
            promoted.append(shop.promote(shop.data["generation"], version)["id"])
        result = lab.start(lab.data["generation"], "arena_live", promoted, ["arena-a", "arena-b"], 100, 300, execution_mode=policy.RULES_ONLY)
        state = lab.load(result["id"])
        state.update(fixture_only=True, evidence_kind="SYNTHETIC")
        lab.prepare(state, lab.chosen_profiles(["arena-a", "arena-b"]), False)
        self.assertEqual(state["rules"]["test_live_max_lots"], 1)
        caps = [api.max_entry_units for api in lab.resources[state["id"]]["apis"]]
        self.assertEqual(caps, [None, Decimal("0.01")])  # the V1 bot keeps its own size; only the schema-4 bot is capped

    def test_h10_freezes_promotes_and_starts_as_a_one_lot_eth_test_live_season(self):
        """The owner's path for the first TEST-LIVE: Sonnet's H10 (dcf48666...) from the workshop to a Live ETH season.
        R2.1.4: the recipe reaches add_version as the workshop page posts it (#recipe-file, #confirm-match, #save-version),
        not through Python's json.loads; tests/test_workshop.py sends the same bytes through the HTTP guard."""
        import shutil
        import subprocess
        if not shutil.which("node"):
            self.skipTest("node runs web/workshop.js")
        path = ROOT / "team/authors/sonnet/candidates/H10_ETH/recipe.json"
        shop, lab = self.fx.workshop, self.fx.lab
        page = subprocess.run(["node", str(ROOT / "tests/v8_ui/workshop-page-fixture.js"), str(path), "page-token",
                               str(shop.data["generation"])], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                              timeout=60, check=True)
        [post] = json.loads(page.stdout)["posts"]
        body = json.loads(post["body"])  # what web_panel parses before Workshop.add_version
        version = shop.add_version(body["generation"], body["strategy"], body["parent_id"])["id"]
        self.assertEqual(shop.version(version)["strategy"]["v4_recipe_sha256"][:16], "dcf486662716f1b7")
        self.assertEqual(shop.version(version)["recipe_sha256"][:8], "d744a6f5")
        trial = shop.train(shop.data["generation"], version, "synthetic-suite-v2", 100)
        self.assertEqual(trial["status"], "COMPLETE")
        shop.freeze(shop.data["generation"], version)
        promoted = shop.promote(shop.data["generation"], version)["id"]
        result = lab.start(lab.data["generation"], "arena_live", [promoted], ["arena-a"], 25, 86400, execution_mode=policy.RULES_ONLY)
        state = lab.load(result["id"])
        state.update(fixture_only=True, evidence_kind="SYNTHETIC")
        lab.prepare(state, lab.chosen_profiles(["arena-a"]), False)
        self.assertEqual((state["rules"]["instrument"], state["rules"]["test_live_max_lots"], state["mode"]), ("ETH-USDT-SWAP", 1, "OKX_LIVE"))
        self.assertEqual(lab.resources[state["id"]]["apis"][0].max_entry_units, Decimal("0.01"))
        self.assertEqual(lab.resources[state["id"]]["feed"].routes["swap"][1]["instId"], "ETH-USDT-SWAP")  # its feature book reads ETH

    def test_the_export_carries_the_opening_balance_and_the_units(self):
        self.start()
        self.assertEqual(Decimal(self.codes("account_checked")[-1]["data"]["start_balance"]["cash"]), Decimal(5000))
        public = self.fx.lab.public(self.state)
        self.assertEqual(public["quantity_units"]["coin"], "ETH")
        self.assertIn("контракти OKX", public["quantity_units"]["note"])

    def test_the_replay_types_order_updates_and_stop_events_by_what_they_are(self):
        from copy import deepcopy
        from trading_lab.event_replay import build_packet, verify_packet
        from trading_lab.match_journal import restore_events
        self.start()
        identity, order = self.place()
        self.fake.fill(identity, Decimal(order["sz"]))
        self.step()
        with self.fx.lab.lock:
            engine.stop(self.state, self.fx.now)
        for _ in range(8):
            if self.state["status"] == "DONE":
                break
            self.step()
        full = deepcopy(self.fx.lab.load(self.state["id"]))
        restore_events(self.fx.lab.path / (full["id"] + ".events.jsonl"), full, full=True)
        packet = build_packet(full)
        verify_packet(packet)
        kinds = {}
        for event in packet["events"]:
            kinds.setdefault(event["code"], set()).add(event["type"])
        entry = [e for e in packet["events"] if e["code"] == "exchange_entry_fill"]
        self.assertEqual({e["type"] for e in entry}, {"FILL"})  # unchanged typing: old exchange packets keep their hashes
        self.assertTrue(all("position_btc" in e.get("financial", {}) for e in entry))  # every fill code is a counted fill
        reads = [e for e in packet["events"] if e["code"] == "exchange_order_read"]  # R2.1.10 (G1): the live update
        self.assertTrue(reads)
        self.assertEqual({e["type"] for e in reads}, {"CHECK"})
        for code, kind in (("protective_placed", "CHECK"), ("protective_terminal", "CHECK"), ("fill_protected", "CHECK"),
                           ("exchange_order_posted", "INTENT"), ("accounting_evidence", "CHECK")):
            self.assertEqual(kinds.get(code), {kind}, code)

    def test_a_failed_watch_read_is_an_event_and_the_full_check_decides(self):
        self.start()
        identity, order = self.place()
        self.fake.fail_order_gets = 1
        self.fx.lab.watch_fills(self.state)  # never raises
        self.assertEqual(self.codes("fill_watch_failed")[-1]["data"]["reason"], "network_error")
        self.assertEqual(self.state["status"], "RUNNING")
        self.step()
        self.assertFalse(self.codes("exchange_check_failed"))

    def test_hold_and_target_count_from_the_venue_fill_time(self):
        self.start()
        identity, order = self.place()
        filled_at = self.fx.now + 1
        self.fx.now = filled_at
        self.fake.fill(identity, Decimal(order["sz"]))  # the venue fills at filled_at; we see it 4 s later
        self.step(4)
        self.assertEqual(self.bot["_hold_start"], filled_at)
        recorded = next(o for o in self.bot["_orders"] if o["request"]["clOrdId"] == identity)
        self.assertEqual((recorded["fill_venue_at"], recorded["fill_observed_at"]), (filled_at, filled_at + 4))

    def test_the_owned_stop_life_cycle_every_post_and_the_raw_bills_are_journalled(self):
        self.start()
        identity, order = self.place()
        half = Decimal(order["sz"]) / 2
        self.fake.fill(identity, half)
        self.step()
        placed = self.codes("protective_placed")[-1]["data"]
        self.assertEqual((Decimal(placed["contracts"]), placed["venue_state"]), (half, "live"))
        self.assertTrue(placed["algo_id"] and placed["trigger_price"] and placed["post_seconds"] is not None)
        self.fake.fill(identity, half)
        self.step()
        amended = self.codes("protective_amended")[-1]["data"]
        self.assertEqual((Decimal(amended["previous_contracts"]), Decimal(amended["contracts"])), (half, 2 * half))
        with self.fx.lab.lock:
            engine.stop(self.state, self.fx.now)
        for _ in range(8):
            if self.state["status"] == "DONE":
                break
            self.step()
        self.assertEqual(self.state["status"], "DONE")
        self.assertEqual(self.codes("protective_terminal")[-1]["data"]["venue_state"], "canceled")
        # R2.1.3 labels: the close order's intent is not called an entry; the only strategy with a fill is the winner
        # by filled_entry_net_v1 (never the cash reference), whatever its sign; the durable seq reaches the last event
        close = [e for e in self.codes("exchange_order_intent") if e["data"]["request"]["reduceOnly"]][-1]
        self.assertNotIn("вхід", close["message"])
        self.assertEqual(self.state["winner_name"], self.state["bots"][0]["name"])
        self.assertEqual((self.bot["filled_entry_orders"], self.bot["entries"], self.bot["trades"]), (1, 0, 1))
        self.assertEqual(self.state["ops_health"]["last_durable_seq"], self.state["_event_seq"])
        # R2.1.3: who took the stop off; here the venue did on the close (cxlOnClosePos), so no cancel POST of ours
        self.assertEqual(self.codes("protective_terminal")[-1]["data"]["cancelled_by"], "venue")
        self.assertEqual(self.codes("protective_cancel_posted"), [])
        posts = [e["data"] for e in self.codes("exchange_order_posted")]
        self.assertEqual([p["outcome"] for p in posts], ["acknowledged", "acknowledged"])  # the entry and the close
        self.assertTrue(all(p["post_seconds"] >= 0 for p in posts))
        evidence = self.codes("accounting_evidence")[-1]["data"]
        self.assertEqual([b["billId"] for b in evidence["bills"]], [b["billId"] for b in self.fake.bills])
        self.assertEqual(Decimal(evidence["balance"]["cash"]), self.fake.cash)


if __name__ == "__main__":
    unittest.main()
