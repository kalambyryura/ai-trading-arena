"""R2.1.13 (SOL, D152; team/platform/R2113_SOL_PLAN_UA.md and its check team/reviews/R2113_PLAN_CHECK_UA.md): SOL-USDT-SWAP on
the executor, a season of three contracts and the time its reads take. Strict fakes, temp roots: no network, key, Jev or order."""
from decimal import Decimal
import io
import json
from pathlib import Path
import threading
from unittest.mock import Mock, patch
import unittest
import zipfile

import test_exchange_lab as fixture
from test_exchange_eth import contracts_named, eth_payload
from test_r2112_mix import private
from trading_lab import exchange_engine as engine, exchange_execution as policy, exchange_lab, strategy_training
from trading_lab.arena_strategies import ArenaError, catalogue_defaults
from trading_lab.battle_export import create_archive, verify_archive
from trading_lab.exchange_api import venue_error
from trading_lab.league import instruments_mark
from trading_lab.match_journal import restore_events
from trading_lab.strategy_contract import payload_of

BTC, ETH, SOL = "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"
PRICE = 89  # plan check p. 4: one tick is 1.12 bps, two are 2.25 bps, over MAX_SPREAD_BPS = 2
SPEC_FILE = Path(__file__).resolve().parents[1] / "tools" / "okx_instrument_sol_usdt_swap.json"
SOL_SPEC = json.loads(SPEC_FILE.read_text(encoding="utf-8"))[0]
ACCOUNTS = ["arena-a", "arena-b", "arena-c", "arena-d", "arena-e"]


def sol_payload(**changes):
    return eth_payload(**{"instrument": SOL, **changes})


class SolSeason(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.ExchangeTests(methodName="runTest")
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        fx, shop = self.fx, self.fx.workshop
        fx.lab.jev_factory = Mock(side_effect=AssertionError("No Jev construction for RULES_ONLY"))
        for fake in fx.fakes.values():
            fake.use(SOL_SPEC, PRICE, .03)
            fake.strict = True
        self.fake = fx.fakes["arena-a"]
        self.version = shop.add_version(shop.data["generation"], sol_payload())["id"]
        trial = shop.train(shop.data["generation"], self.version, strategy_training.SYNTHETIC["id"], 100)
        self.assertEqual(trial["status"], "COMPLETE")
        shop.freeze(shop.data["generation"], self.version)
        self.promoted = shop.promote(shop.data["generation"], self.version)

    def start(self):
        lab = self.fx.lab
        state = lab.load(lab.start(lab.data["generation"], "arena_live", [self.promoted["id"]], ["arena-a"], 100, 300,
                                   execution_mode=policy.RULES_ONLY)["id"])
        state.update(fixture_only=True, evidence_kind="SYNTHETIC")
        lab.prepare(state, lab.chosen_profiles(["arena-a"]), False)
        self.state, self.bot = state, state["bots"][0]
        return state

    def steps(self, n, until=None, dt=5):
        for _ in range(n):
            self.fx.lab.step(self.state)
            if self.state["status"] in {"DONE", "PAUSED"} or (until and until()):
                return
            self.fx.now += dt

    def entered(self):
        self.start()
        self.steps(60, until=lambda: self.fake.orders)
        return next(iter(self.fake.orders.values())) if self.fake.orders else None

    def test_the_contract_is_read_from_the_venue_spec(self):
        self.start()
        contract = self.bot["_instrument"]
        self.assertEqual((contract["instrument"], Decimal(contract["contract_value"]), Decimal(contract["lot_step"])), (SOL, Decimal(1), Decimal(".01")))
        self.assertEqual(self.state["rules"]["instrument"], SOL)
        self.assertEqual(self.state["match_manifest"]["instrument"], SOL)

    def test_one_lot_is_a_hundredth_of_a_coin_and_is_protected_on_its_tick(self):
        order = self.entered()
        self.assertIsNotNone(order, [e["code"] for e in self.state["events"][-12:]])
        self.assertEqual((order["instId"], order["side"], order["reduceOnly"]), (SOL, "buy", False))
        self.assertEqual(Decimal(order["sz"]), Decimal(".01"))  # G12: one lot on Live; ctVal 1, so 0.01 SOL, about 0.89 USDT
        self.steps(4, until=lambda: self.fake.algos)
        self.assertEqual(self.bot["position_btc"], .01)  # the engine's base-unit field, in SOL here
        self.assertLess(abs(self.bot["equity_usdt"] - 100), .1)  # a mis-mark by ctVal would show here
        self.assertTrue(self.fake.algos)
        for algo in self.fake.algos.values():
            self.assertEqual(algo["instId"], SOL)
            self.assertEqual(Decimal(algo["slTriggerPx"]) % Decimal(".01"), 0)  # rounded on the 0.01 tick
            self.assertLess(Decimal(algo["slTriggerPx"]), Decimal(PRICE))
        public = self.fx.lab.status()["active"]["arena_live"]
        self.assertEqual(next(b for b in public["bots"] if b["id"] == self.bot["id"]).get("base_ccy"), "SOL")

    def test_a_sol_season_closes_at_its_target_settles_and_exports_on_sol_only(self):
        self.entered()
        self.steps(4, until=lambda: self.fake.algos)
        self.fake.bid, self.fake.ask = Decimal("89.45"), Decimal("89.46")  # +50 bps: the 40 bps target, live from the next minute
        self.steps(30, until=lambda: not self.bot["position_btc"])
        self.assertEqual(self.bot["position_btc"], 0, [e["code"] for e in self.state["events"][-12:]])
        close = [o for o in self.fake.orders.values() if o["reduceOnly"]]
        self.assertTrue(close and all(o["instId"] == SOL and Decimal(o["sz"]) == Decimal(".01") for o in close))
        self.fx.now += 400
        self.steps(12, until=lambda: self.state["status"] == "DONE")
        self.assertEqual(self.state["status"], "DONE", [e["code"] for e in self.state["events"][-12:]])
        self.assertTrue(self.bot["accounting_reconciled"])
        self.assertGreater(self.bot["pnl_usdt"], 0)
        self.assertLess(self.bot["pnl_usdt"], .01)  # 0.01 SOL at +0.45 USDT less fees: never scaled by a wrong contract value
        self.assertEqual(contracts_named(private(self.fake.requests)), {SOL})
        self.assertEqual(self.fake.violations, [])
        lab = self.fx.lab
        full = json.loads(json.dumps(lab.load(self.state["id"])))
        restore_events(lab.path / (full["id"] + ".events.jsonl"), full, full=True)
        with create_archive(full, snapshots_path=engine.snapshot_files(lab.path, full)[None]) as stream:
            payload = stream.read()
        self.assertTrue(verify_archive(io.BytesIO(payload))["verified"])
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertIn("market.jsonl", archive.namelist())
            self.assertEqual(json.loads(archive.read("battle-review.json"))["match"]["instrument"], SOL)

    def test_a_stop_closes_reduce_only_on_sol(self):
        self.entered()
        self.steps(4, until=lambda: self.fake.algos)
        lab = self.fx.lab
        lab.stop(lab.data["generation"], self.state["id"])
        self.steps(30, until=lambda: self.state["status"] == "DONE")
        self.assertEqual(self.state["status"], "DONE", [e["code"] for e in self.state["events"][-12:]])
        close = [o for o in self.fake.orders.values() if o["reduceOnly"]]
        self.assertTrue(close and all(o["instId"] == SOL for o in close))
        self.assertEqual((self.fake.units, self.bot["position_btc"]), (0, 0))
        self.assertTrue(self.bot["accounting_reconciled"])

    def test_at_about_89_usdt_one_tick_of_spread_enters(self):
        self.assertIsNotNone(self.entered(), [e["code"] for e in self.state["events"][-12:]])

    def test_at_about_89_usdt_two_ticks_of_spread_are_refused(self):
        # plan check p. 4: 2.25 bps over MAX_SPREAD_BPS = 2; the evaluator's tapes quote one tick and never see this
        self.fake.ask = self.fake.bid + Decimal(".02")
        self.assertIsNone(self.entered())
        spread = [c for e in self.state["events"] if e["code"] == "entry_checks" for c in e["data"]["checks"] if c.get("feature") == "spread_bps"]
        self.assertTrue(spread)
        self.assertTrue(all(c["passed"] is False and c["actual"] > 2 for c in spread), spread[:2])

    def test_the_paper_arena_refuses_sol_and_the_recorded_btc_tapes_too(self):
        with self.assertRaisesRegex(ArenaError, "PAPER"):
            self.fx.arena.start(self.fx.arena.data["generation"], [self.promoted["id"]], 100, 300, "rules")


class ThreeContracts(unittest.TestCase):
    """Five bots: three BTC, one ETH, one SOL, each on its own strict fake subaccount (the plan's check of R2.1.12's isolation)."""
    def setUp(self):
        self.fx = fixture.ExchangeTests(methodName="runTest")
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        fx, shop = self.fx, self.fx.workshop
        fx.lab.jev_factory = Mock(side_effect=AssertionError("No Jev construction for RULES_ONLY"))
        for uid, name in (("104", "arena-c"), ("105", "arena-d"), ("106", "arena-e")):
            fx.fakes[name] = fixture.FakeExchange(fx.clock, uid)
        fx.lab.profile_provider = lambda root: [{"id": a, "label": a, "environment": "live", "settings_file": ".env.okx." + a + ".local"} for a in ACCOUNTS]
        fx.fakes["arena-d"].use(fixture.ETH_SPEC, 3000, 1)
        fx.fakes["arena-e"].use(SOL_SPEC, PRICE, .03)
        market = fx.fakes["arena-a"]  # the season's market client is its first bot's account
        market.serve(fixture.ETH_SPEC, 3000, 1)
        market.serve(SOL_SPEC, PRICE, .03)
        self.fakes = [fx.fakes[a] for a in ACCOUNTS]
        for fake in self.fakes:
            fake.strict = True
        ids = [fx.version]
        for index in (1, 2):
            ids.append(shop.add_version(shop.data["generation"], payload_of(catalogue_defaults()[index]))["id"])
        ids += [shop.add_version(shop.data["generation"], eth_payload())["id"], shop.add_version(shop.data["generation"], sol_payload())["id"]]
        for version in ids:
            shop.train(shop.data["generation"], version, strategy_training.SYNTHETIC["id"], 100)
            shop.freeze(shop.data["generation"], version)
            shop.promote(shop.data["generation"], version)
        lab = fx.lab
        result = lab.start(lab.data["generation"], "arena_live", ids, ACCOUNTS, 100, 3600, execution_mode=policy.RULES_ONLY)  # admission stays open: the books keep reading
        state = lab.load(result["id"])
        state.update(fixture_only=True, evidence_kind="SYNTHETIC")
        lab.prepare(state, lab.chosen_profiles(ACCOUNTS), False)
        self.state, self.bots = state, state["bots"][:5]
        self.own = dict(zip(ACCOUNTS, (BTC, BTC, BTC, ETH, SOL)))

    def steps(self, n, until=None, dt=5):
        for _ in range(n):
            self.fx.lab.step(self.state)
            if self.state["status"] in {"DONE", "PAUSED"} or (until and until()):
                return
            self.fx.now += dt

    def open_three(self):
        self.steps(90, until=lambda: all(self.bots[i]["position_btc"] for i in (0, 3, 4)))
        self.assertTrue(all(self.bots[i]["position_btc"] for i in (0, 3, 4)), [e["code"] for e in self.state["events"][-12:]])

    def test_each_bot_trades_only_its_contract_and_the_zip_has_three_markets(self):
        state = self.state
        self.assertEqual(sorted(state["rules"]["instruments"].values()), [BTC, BTC, BTC, ETH, SOL])
        self.assertEqual(sorted(state["rules"]["book_gets_by_instrument"]), [ETH, SOL])
        self.open_three()
        self.assertEqual(Decimal(next(o for o in self.fakes[4].orders.values() if not o["reduceOnly"])["sz"]), Decimal(".01"))
        self.assertLess(abs(self.bots[4]["equity_usdt"] - 100), .1)
        self.assertEqual(set(state["latest_markets"]), {BTC, ETH, SOL})
        lab = self.fx.lab
        lab.stop(lab.data["generation"], state["id"])
        self.steps(60, until=lambda: state["status"] == "DONE")
        self.assertEqual(state["status"], "DONE", [e["code"] for e in state["events"][-15:]])
        self.assertTrue(all(b["accounting_reconciled"] for b in self.bots))
        for account, fake in zip(ACCOUNTS, self.fakes):
            with self.subTest(account):
                self.assertEqual(fake.violations, [])
                self.assertEqual(contracts_named(private(fake.requests)), {self.own[account]})
        self.assertEqual(instruments_mark(state), "змішаний: BTC ×3 + ETH ×1 + SOL ×1")
        full = json.loads(json.dumps(lab.load(state["id"])))
        restore_events(lab.path / (full["id"] + ".events.jsonl"), full, full=True)
        with create_archive(full, snapshots_path=engine.snapshot_files(lab.path, full)) as stream:
            payload = stream.read()
        self.assertTrue(verify_archive(io.BytesIO(payload))["verified"])
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            self.assertEqual(sorted(n for n in names if n.startswith("market.")), ["market." + c + ".jsonl" for c in (BTC, ETH, SOL)])
            self.assertEqual(json.loads(archive.read("battle-review.json"))["match"]["instruments"], [BTC, ETH, SOL])

    def latency(self, get):
        """Plan check p. 2: every public GET and every private one of the worker thread takes `get` seconds on the fake clock."""
        fx, resource, main = self.fx, self.fx.lab.resources[self.state["id"]], threading.main_thread()

        def timed(original):
            def transport(request):
                if "/api/v5/market/" in request.full_url or "/api/v5/public/" in request.full_url or threading.current_thread() is main:
                    fx.now += get
                return original(request)
            return transport
        for api in resource["apis"] + [getattr(feed, "base", feed).api for feed in resource["feeds"].values()]:
            api.transport = timed(api.transport)

    def worst_step(self, get):
        """After the entries, the slowest of the next 16 steps (books read a new minute) with the age of each contract's quote
        when the first bot of that contract is judged; a transient failure of the BTC bot's account check runs A3 through it."""
        self.open_three()
        self.settle_and_slow_down(get)
        ages, durations = [], []
        original = exchange_lab.own_snapshot

        def recording(state, bot, snapshot):
            if snapshot is not None:
                ages.append((snapshot["_instrument"], self.fx.now - snapshot["quote_ts"] / 1000))
            return original(state, bot, snapshot)
        lab = self.fx.lab
        with patch.object(exchange_lab, "own_snapshot", recording):
            for _ in range(16):
                began = self.fx.now
                lab.step(self.state)
                durations.append(self.fx.now - began)
                self.fx.now += 5
        return ages, durations

    def run_a3(self, get, index=0, **fresh):
        """A BTC position under its verified stop meets a transient OKX code for six slow steps, a new minute among them."""
        self.settle_and_slow_down(get)
        resource = self.fx.lab.resources[self.state["id"]]
        api, original = resource["apis"][index], resource["apis"][index].transport

        def failing(request):
            if "/api/v5/account/positions" in request.full_url and request.get_method() == "GET":
                raise venue_error("http_503", http_status=503, okx_code="50001")
            return original(request)
        api.transport = failing
        lab = self.fx.lab
        with patch.object(lab, "fresh_snapshot", **(fresh or {"wraps": lab.fresh_snapshot})) as self.fresh:
            for _ in range(6):
                lab.step(self.state)
                self.fx.now += 5
        return [e for e in self.state["events"] if e["code"] in {"degraded_window_started", "degraded_extension_ended"}]

    def settle_and_slow_down(self, get):
        self.open_three()
        self.steps(8, until=lambda: self.fx.lab.resources[self.state["id"]].get("stop_matched_mono", {}).get(self.bots[0]["id"]) is not None)
        self.fx.now += 5  # out of the 2 s read budget that the fixture's stationary clock filled
        self.latency(get)

    def test_at_343_ms_a_get_three_feeds_stay_inside_the_ten_seconds(self):
        # plan check p. 2: 12 GETs a step plus the books' pages; the oldest quote when its first bot is judged
        ages, durations = self.worst_step(.343)
        self.assertLess(max(durations), 7)
        self.assertLess(max(a for _, a in ages), exchange_lab.FRESH_QUOTE_MAX_AGE_SECONDS)

    def test_at_922_ms_a_get_the_first_quote_passes_ten_seconds_by_the_end_of_the_reads(self):
        # what the re-read below answers: this model is why A3 reads the contract again
        ages, durations = self.worst_step(.922)
        self.assertLess(max(durations), 35)
        self.assertGreater(max(a for c, a in ages if c == BTC), exchange_lab.FRESH_QUOTE_MAX_AGE_SECONDS)

    def test_a3_at_the_slowest_measured_get_extends_the_first_bot(self):
        events = self.run_a3(.922)
        self.assertTrue(events and events[0]["data"]["extended"], events)
        self.assertNotIn("market_not_fresh", [e["data"].get("reason") for e in events])

    def test_a3_reads_the_contract_again_when_the_other_reads_aged_its_quote(self):
        # 1.2 s a GET: nine GETs after the BTC ticker pass the ten seconds even for the first bot
        events = self.run_a3(1.2)
        self.assertTrue(events and events[0]["data"]["extended"], events)
        self.assertNotIn("market_not_fresh", [e["data"].get("reason") for e in events])
        self.assertTrue(any(c.args[2] == [] and c.args[4] == BTC for c in self.fresh.call_args_list))

    def test_a_fresh_quote_is_not_read_a_second_time(self):
        # the review's R2113-TEST-REREAD-FRESH: four GETs in every step of a failed check, otherwise
        events = self.run_a3(.343)
        self.assertTrue(events[0]["data"]["extended"], events)
        self.assertFalse([c for c in self.fresh.call_args_list if c.args[2] == []])

    def test_a_flat_bot_whose_check_failed_costs_no_second_read(self):
        self.run_a3(1.2, index=1)  # the second BTC bot's recipe has not entered
        self.assertFalse(self.bots[1]["position_btc"])
        self.assertFalse([c for c in self.fresh.call_args_list if c.args[2] == []])

    def test_a3_is_not_extended_when_the_second_read_fails_too(self):
        events = self.run_a3(1.2, return_value=None)
        self.assertFalse(events[0]["data"]["extended"], events)


if __name__ == "__main__":
    unittest.main()
