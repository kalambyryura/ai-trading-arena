"""R2 (a): one contract per season, ETH-USDT-SWAP beside BTC. Temp roots, the fake exchange and fake public history
only: no network, key, Jev or real order."""
from decimal import Decimal
import json
from unittest.mock import Mock, patch
import unittest
from urllib.parse import parse_qs, urlsplit

import test_exchange_lab as fixture
from test_strategy_v4_admission import payload as v4_payload
from trading_lab import exchange_engine as engine, exchange_execution as policy, strategy_training
from trading_lab import strategy_v4_feed as feedmod
from trading_lab.arena_strategies import ArenaError, catalogue_defaults, validate_strategy
from trading_lab.exchange_api import ExchangeFeed
from trading_lab.okx_readonly import PreflightError
from trading_lab.ops_policy import owned_stop_intent

ETH = "ETH-USDT-SWAP"
ALWAYS = {"when": {"feature": {"name": "return_bps", "tf": "1m", "n": 5}, "op": "gt", "value": -1000000}}


def eth_payload(**changes):
    return v4_payload(**{"instrument": ETH, "entry": ALWAYS, "max_hold_seconds": 600, **changes})


def contracts_named(requests):
    """Every instId a request names, in its query or in its JSON body (a list for cancel-algos)."""
    names = set()
    for request in requests:
        names.update(parse_qs(urlsplit(request.full_url).query).get("instId", []))
        if request.data:
            body = json.loads(request.data)
            names.update(row["instId"] for row in (body if isinstance(body, list) else [body]) if "instId" in row)
    return names


class EthClientTests(unittest.TestCase):
    def setUp(self):
        self.now = 1800000030.
        self.fake = fixture.FakeExchange(lambda: self.now)
        self.fake.use(fixture.ETH_SPEC, 3000, 1)
        self.api = self.fake.api("demo")
        self.api.inst_id = ETH

    def test_preflight_reads_the_eth_contract_and_writes_only_eth(self):
        self.api.preflight(100, False)
        self.assertEqual((self.api.instrument["instrument"], Decimal(self.api.instrument["contract_value"])), (ETH, Decimal(".1")))
        self.api.arm(100)
        body = {"instId": ETH, "tdMode": "isolated", "posSide": "net", "side": "buy", "ordType": "ioc", "sz": "0.03",
                "px": "3000", "reduceOnly": False, "clOrdId": "J5" + "0" * 16 + "1E0001", "stpMode": "cancel_taker"}
        self.api.validate_write("/api/v5/trade/order", body)
        with self.assertRaisesRegex(PreflightError, "exchange_write_instrument"):
            self.api.validate_write("/api/v5/trade/order", {**body, "instId": "BTC-USDT-SWAP"})
        with self.assertRaisesRegex(PreflightError, "exchange_entry_notional_limit"):  # 0.1 ETH a contract: 1 contract > 100 USDT
            self.api.validate_write("/api/v5/trade/order", {**body, "sz": "1"})
        self.assertEqual(contracts_named(self.fake.requests), {ETH})

    def test_a_btc_contract_or_position_is_foreign_in_an_eth_season(self):
        self.fake.use(fixture.SPEC, 84000, 20)
        with self.assertRaisesRegex(PreflightError, "instrument_unavailable_or_mismatched"):
            self.api.preflight(100, False)
        self.fake.units = Decimal("0.01")
        with self.assertRaisesRegex(PreflightError, "foreign_or_unsupported_position"):
            self.api.position()

    def test_the_feed_reads_and_checks_the_eth_market(self):
        feed = ExchangeFeed(self.api, clock=lambda: self.now)
        snapshot = feed.snapshot()
        self.assertEqual(snapshot["bid"], 3000.0)
        self.assertEqual(contracts_named(self.fake.requests), {ETH})
        self.fake.use(fixture.SPEC, 84000, 20)  # the venue answers with BTC rows: never an ETH quote
        with self.assertRaises((ArenaError, PreflightError)):
            ExchangeFeed(self.api, clock=lambda: self.now).snapshot()

    def test_book_routes_and_the_stop_name_the_season_contract(self):
        routes = feedmod.routes(ETH)
        self.assertEqual({k: v[1]["instId"] for k, v in routes.items()},
                         {"swap": ETH, "index": "ETH-USDT", "mark": ETH, "funding": ETH})
        self.assertEqual(feedmod.ROUTES, feedmod.routes("BTC-USDT-SWAP"))  # BTC seasons read exactly as before
        stop = dict(client_id="J8" + "0" * 16 + "0S0000", contracts="0.03", basis="3000", stop_bps=20, tick_size=".01")
        self.assertEqual(owned_stop_intent(**stop, instrument=ETH).body["instId"], ETH)
        self.assertEqual(owned_stop_intent(**stop).body["instId"], "BTC-USDT-SWAP")


class SeasonInstrumentTests(unittest.TestCase):
    def test_one_contract_per_season(self):
        eth, btc, v1 = validate_strategy(eth_payload()), validate_strategy(v4_payload()), catalogue_defaults()[0]
        self.assertEqual(engine.season_instrument([eth]), ETH)
        self.assertEqual(engine.season_instrument([btc, v1]), "BTC-USDT-SWAP")
        self.assertEqual(engine.season_instrument([{"strategy_schema_version": 4, "instrument": "SOL-USDT-SWAP"}]), "SOL-USDT-SWAP")  # R2.1.13
        for bad in ([eth, v1], [eth, btc], [{"strategy_schema_version": 4, "instrument": "DOGE-USDT-SWAP"}]):
            with self.assertRaises(ArenaError):
                engine.season_instrument(bad)

    def test_mix_a_mixed_season_only_live_rules_only(self):
        # R2.1.12 (MIX-1, D148): the bots' contracts in the rules under config_sha256; shared Jev and Demo refuse it
        eth, v1 = validate_strategy(eth_payload()), catalogue_defaults()[0]
        state = engine.create([eth, v1], ["a", "b"], "arena_live", 100, 300, 1800000000., "x" * 64, policy.RULES_ONLY)
        self.assertEqual(state["rules"]["instrument"], engine.MIXED)
        self.assertEqual(state["rules"]["instruments"], {eth["id"]: ETH, v1["id"]: "BTC-USDT-SWAP"})
        self.assertEqual(set(state["rules"]["book_gets_by_instrument"]), {ETH})  # only the schema-4 contract has a book
        self.assertEqual(state["match_manifest"]["instrument"], engine.MIXED)
        self.assertEqual(state["match_manifest"]["risk_and_execution"]["instruments"], state["rules"]["instruments"])
        self.assertEqual((engine.instrument_for(state, eth["id"]), engine.instrument_for(state, v1["id"])), (ETH, "BTC-USDT-SWAP"))
        for context, mode in (("arena_live", "shared_jev"), ("workshop_demo", policy.RULES_ONLY)):
            with self.subTest(context=context, mode=mode), self.assertRaisesRegex(ArenaError, "Змішаний сезон"):
                engine.season_instruments([eth, v1], context, mode)
        with self.assertRaisesRegex(ArenaError, "лише BTC-USDT-SWAP, ETH-USDT-SWAP, SOL-USDT-SWAP"):
            engine.bot_instruments([{"id": "x", "strategy_schema_version": 4, "instrument": "DOGE-USDT-SWAP"}])
        single = engine.create([v1], ["a"], "arena_live", 100, 300, 1800000000., "x" * 64, policy.RULES_ONLY)
        self.assertNotIn("instruments", single["rules"])  # a one-contract season: no new key
        self.assertNotIn("book_gets_by_instrument", single["rules"])
        self.assertEqual(engine.instrument_for(single, v1["id"]), "BTC-USDT-SWAP")

    def test_sol_enters_and_eth_and_sol_stay_out_of_btc_tapes(self):
        self.assertEqual(validate_strategy(v4_payload(instrument="SOL-USDT-SWAP"))["instrument"], "SOL-USDT-SWAP")  # R2.1.13
        with patch("trading_lab.strategy_v4_runtime.EXECUTED_INSTRUMENTS", ("BTC-USDT-SWAP", ETH)), self.assertRaises(ArenaError):
            validate_strategy(v4_payload(instrument="SOL-USDT-SWAP"))  # the guard stays for what the executor does not trade
        for recipe in (eth_payload(), v4_payload(instrument="SOL-USDT-SWAP")):
            with self.assertRaisesRegex(ArenaError, "синтетиці"):
                strategy_training.run_trial(validate_strategy(recipe), {"id": "recorded-x", "kind": "RECORDED_PUBLIC"}, 100, [])


class EthSeasonTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.ExchangeTests(methodName="runTest")
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        for fake in self.fx.fakes.values():
            fake.use(fixture.ETH_SPEC, 3000, 1)
        self.fx.lab.jev_factory = Mock(side_effect=AssertionError("No Jev construction for RULES_ONLY"))
        self.shop = self.fx.workshop
        self.version = self.shop.add_version(self.shop.data["generation"], eth_payload())["id"]

    def test_an_eth_rules_only_season_trades_protects_and_settles_eth_only(self):
        fx, lab, fake = self.fx, self.fx.lab, self.fx.fakes["demo"]
        result = lab.start(lab.data["generation"], "workshop_demo", [self.version], ["demo"], 100, 300, execution_mode=policy.RULES_ONLY)
        state = lab.load(result["id"])
        state.update(fixture_only=True, evidence_kind="SYNTHETIC")
        lab.prepare(state, lab.chosen_profiles(["demo"]), False)
        bot = state["bots"][0]
        self.assertEqual((state["rules"]["instrument"], bot["_instrument"]["instrument"]), (ETH, ETH))
        self.assertEqual(state["match_manifest"]["instrument"], ETH)  # the immutable evidence label as well
        for _ in range(60):  # the book warms up from fake history, then one decision per visible minute
            lab.step(state)
            if fake.orders:
                break
            fx.now += 5
        entry = next(iter(fake.orders.values()))
        self.assertEqual((entry["instId"], entry["side"], entry["reduceOnly"]), (ETH, "buy", False))
        self.assertGreater(bot["position_btc"], 0)  # the engine's base-unit field: ETH here
        self.assertTrue(fake.algos and all(a["instId"] == ETH for a in fake.algos.values()))
        fake.bid, fake.ask = Decimal("3015"), Decimal("3015.01")  # +50 bps: the 40 bps target, live from the next minute
        for _ in range(30):
            fx.now += 5
            lab.step(state)
            if not bot["position_btc"]:
                break
        self.assertEqual(bot["position_btc"], 0)
        fx.now += 400
        for _ in range(10):
            if state["status"] == "DONE":
                break
            lab.step(state)
            fx.now += 5
        self.assertEqual(state["status"], "DONE")
        self.assertTrue(bot["accounting_reconciled"])
        self.assertGreater(bot["pnl_usdt"], 0)
        self.assertEqual(contracts_named(fake.requests), {ETH})
        from trading_lab.battle_review import build_review
        from trading_lab.match_journal import restore_events
        from copy import deepcopy
        full = deepcopy(lab.load(state["id"]))
        restore_events(lab.path / (full["id"] + ".events.jsonl"), full, full=True)
        self.assertEqual(build_review(full)["match"]["instrument"], ETH)  # the review names the coin of an ETH season

    def test_paper_arena_refuses_an_eth_recipe(self):
        trial = self.shop.train(self.shop.data["generation"], self.version, strategy_training.SYNTHETIC["id"], 100)
        self.assertEqual(trial["status"], "COMPLETE")  # synthetic only: the recorded workshop tapes are BTC
        self.shop.freeze(self.shop.data["generation"], self.version)
        promoted = self.shop.promote(self.shop.data["generation"], self.version)
        with self.assertRaisesRegex(ArenaError, "PAPER"):
            self.fx.arena.start(self.fx.arena.data["generation"], [promoted["id"]], 100, 300, "rules")


if __name__ == "__main__":
    unittest.main()
