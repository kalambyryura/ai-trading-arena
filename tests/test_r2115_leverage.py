"""R2.1.15 (D168 p. 3, D170, D171, D178; plan team/platform/R2115_LEVERAGE_PLAN_UA.md, its check
team/reviews/R2115_PLAN_CHECK_UA.md): each bot's leverage at a Start, not above its version's limit; margin as the budget's
share, notional = margin x leverage; the Judge's fields (leverage, margin mode, actual notional and liquidation price at
each entry, liquidation events). Strict fakes, temp roots: no network, key, Jev call or real order."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import test_exchange_lab as fixture  # noqa: E402
import test_order_outcomes as outcomes  # noqa: E402
from trading_lab import exchange_engine as engine, exchange_execution as policy, live_leverage  # noqa: E402
from trading_lab.arena_strategies import ArenaError, fingerprint  # noqa: E402
from trading_lab.battle_export import match_settings  # noqa: E402
from trading_lab.battle_review import build_review, leverage_cohort  # noqa: E402
from trading_lab.exchange_api import ExchangeAPI  # noqa: E402
from trading_lab.match_journal import restore_events  # noqa: E402
from trading_lab.okx_readonly import PreflightError  # noqa: E402

SET, INFO, POSITIONS = "/api/v5/account/set-leverage", "/api/v5/account/leverage-info", "/api/v5/account/positions"
TIERS = "/api/v5/public/position-tiers"
TIER_ROWS = [{"instFamily": "", "tier": "1", "minSz": "0", "maxSz": "5000", "mmr": "0.004", "imr": "0.005", "maxLever": "100"},
             {"instFamily": "", "tier": "2", "minSz": "5000", "maxSz": "10000", "mmr": "0.006", "imr": "0.008", "maxLever": "75"}]
ORDER = "/api/v5/trade/order"


class LeverMixin:
    """OKX's leverage settings on top of a fake: set-leverage stores the lever, leverage-info and positions report it, and an
    open position shows avgPx, liqPx (computed from the lever, or self.liq: "" empty, or a fixed price), margin and notionalUsd."""
    lever, info_lever, liq, tiers, no_avg = "1", None, None, None, False

    def transport(self, request):
        path, method = urlsplit(request.full_url).path, request.get_method()
        if path == TIERS:
            self.requests.append(request)
            family = self.spec["instId"].rsplit("-", 1)[0]
            rows = self.tiers if self.tiers is not None else [{**row, "instFamily": family} for row in TIER_ROWS]
            return {"code": "0", "data": deepcopy(rows)}
        if path == SET and method == "POST":
            self.requests.append(request)
            body = json.loads(request.data)
            self.__dict__.setdefault("set_posts", []).append(body)
            self.lever = body["lever"]
            return {"code": "0", "data": [{"instId": body["instId"], "lever": body["lever"], "mgnMode": body["mgnMode"], "posSide": "net"}]}
        answer = super().transport(request)
        if path == INFO:
            answer = {**answer, "data": [{**row, "lever": self.info_lever or self.lever} for row in answer["data"]]}
        if path == POSITIONS:
            rows = []
            for row in answer["data"]:
                row = {**row, "lever": self.lever}
                if Decimal(row["pos"]):
                    notional = abs(Decimal(row["pos"])) * Decimal(self.spec["ctVal"]) * self.basis
                    row.update(avgPx="" if self.no_avg else str(self.basis), liqPx=self.liq_px(), margin=str(notional / Decimal(self.lever)),
                               notionalUsd=str(notional * Decimal("1.0002")), mmr="0.1", adl="1")
                rows.append(row)
            answer = {**answer, "data": rows}
        return answer

    def liq_px(self):
        if self.liq is not None:
            return self.liq
        side = 1 if Decimal(self.units) > 0 else -1
        return str((self.basis * (1 - side * Decimal(".95") / Decimal(self.lever))).quantize(Decimal(self.spec["tickSz"])))


class LeverFake(LeverMixin, fixture.FakeExchange):
    pass


class LeverOutcome(LeverMixin, outcomes.OutcomeFake):
    pass


def limits_for(*rows):
    """The table with the test recipes' rows ({sha: (limit, name)}), restored after the test."""
    return patch.dict(live_leverage.LEVERAGE_LIMITS, dict(rows))


class StartAndSettings(unittest.TestCase):
    """The Start's checks, the venue setting and its confirmation, the API's barriers (V1 catalogue recipes, BTC)."""

    def setUp(self):
        self.fx = fixture.ExchangeTests(methodName="runTest")
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        for fake in self.fx.fakes.values():
            fake.__class__ = LeverFake
        known = {r["id"]: r for r in self.fx.arena.catalogue()}
        self.sha = {i: known[i]["recipe_sha256"] for i in ("sample-trend", "sample-short")}
        self.table = limits_for((self.sha["sample-trend"], (10, "Test trend")), (self.sha["sample-short"], (8, "Test short")))
        self.table.start()
        self.addCleanup(self.table.stop)

    def start(self, leverage, accounts=("arena-a", "arena-b")):
        lab = self.fx.lab
        result = lab.start(lab.data["generation"], "arena_live", ["sample-trend", "sample-short"], list(accounts), 100, 300,
                           leverage=leverage)
        state = lab.load(result["id"])
        lab.prepare(state, lab.chosen_profiles(list(accounts)), False)
        self.addCleanup(lambda: (lab.resources.pop(state["id"], {"stack": Mock()})["stack"].close()))
        return state

    def no_requests(self):
        return all(not f.requests for f in self.fx.fakes.values())

    def test_without_the_field_every_bot_is_1x_as_before(self):
        state = self.start(None)
        self.assertNotIn("leverage", state["match_manifest"])
        self.assertTrue(all("leverage" not in b for b in state["bots"]))
        for name in ("arena-a", "arena-b"):
            self.assertEqual(self.fx.fakes[name].set_posts, [{"instId": "BTC-USDT-SWAP", "lever": "1", "mgnMode": "isolated"}])
        confirmed = [e for e in state["events"] if e["code"] == "leverage_confirmed"]
        self.assertEqual([e["message"] for e in confirmed], ["Плече 1x isolated підтверджено GET leverage-info."] * 2)
        self.assertEqual({e["data"]["lever"] for e in confirmed}, {"1"})

    def test_all_1x_given_is_the_same_as_none(self):
        state = self.start([1, 1])
        self.assertNotIn("leverage", state["match_manifest"])
        self.assertTrue(all("leverage" not in b for b in state["bots"]))

    def test_3x_sets_confirms_and_pins_each_bot_and_the_table_in_the_manifest(self):
        state = self.start([3, 2])
        block = state["match_manifest"]["leverage"]
        self.assertEqual(block["table_sha256"], live_leverage.table_sha256())
        self.assertEqual(block["margin_mode"], "isolated")
        self.assertEqual(block["stop_notional_budget_pct"], engine.STOP_NOTIONAL_BUDGET_PCT)
        self.assertEqual(block["bots"], [
            {"strategy_id": "sample-trend", "recipe_sha256": self.sha["sample-trend"], "leverage": 3, "limit": 10},
            {"strategy_id": "sample-short", "recipe_sha256": self.sha["sample-short"], "leverage": 2, "limit": 8}])
        self.assertEqual(state["match_manifest_sha256"], fingerprint(state["match_manifest"]))
        self.assertEqual([b.get("leverage") for b in state["bots"] if b["id"] != "cash"], [3, 2])
        self.assertEqual(self.fx.fakes["arena-a"].set_posts, [{"instId": "BTC-USDT-SWAP", "lever": "3", "mgnMode": "isolated"}])
        self.assertEqual(self.fx.fakes["arena-b"].set_posts, [{"instId": "BTC-USDT-SWAP", "lever": "2", "mgnMode": "isolated"}])
        confirmed = [e for e in state["events"] if e["code"] == "leverage_confirmed"]
        self.assertEqual([(e["message"], e["data"]["lever"]) for e in confirmed],
                         [("Плече 3x isolated підтверджено GET leverage-info.", "3"), ("Плече 2x isolated підтверджено GET leverage-info.", "2")])

    def test_a_limit_or_a_shape_violation_is_refused_before_any_request(self):
        lab = self.fx.lab
        for leverage in ([11, 1], [1, 9], [0, 1], [True, 1], [3.0, 1], ["3", 1], [3], [3, 3, 3], 3, {"a": 3}):
            with self.subTest(leverage=leverage):
                with self.assertRaises(ArenaError):
                    lab.start(lab.data["generation"], "arena_live", ["sample-trend", "sample-short"], ["arena-a", "arena-b"], 100, 300,
                              leverage=leverage)
                self.assertTrue(self.no_requests())
        self.assertFalse(lab.data["runs"])

    def test_a_recipe_the_table_does_not_list_is_1x_only(self):
        self.table.stop()
        self.table = limits_for()
        self.table.start()
        lab = self.fx.lab
        with self.assertRaisesRegex(ArenaError, "межа версії 1x"):
            lab.start(lab.data["generation"], "arena_live", ["sample-trend", "sample-short"], ["arena-a", "arena-b"], 100, 300, leverage=[2, 1])
        self.assertTrue(self.no_requests())

    def test_no_limit_above_10x_whatever_the_table_says(self):
        live_leverage.LEVERAGE_LIMITS[self.sha["sample-trend"]] = (12, "Test trend")
        self.assertEqual(live_leverage.limit(self.sha["sample-trend"]), 10)
        lab = self.fx.lab
        with self.assertRaises(ArenaError):
            lab.start(lab.data["generation"], "arena_live", ["sample-trend", "sample-short"], ["arena-a", "arena-b"], 100, 300, leverage=[11, 1])

    def test_the_six_versions_of_d170_d171(self):
        rows = {name: (sha, top) for sha, (top, name) in live_leverage.LEVERAGE_LIMITS.items() if sha not in self.sha.values()}
        self.assertEqual({n: t for n, (s, t) in rows.items()},
                         {"Sol V1": 10, "Astra V1": 10, "Opus V1": 10, "H10": 8, "Гібрид ETH": 3, "Гібрид SOL": 3})
        self.assertEqual(rows["Гібрид SOL"][0], "3328c4e29c143ab385ff670d972c777b9ddaae240410d507d5500cf750109913")
        self.assertEqual(live_leverage.limit("0" * 64), 1)

    def test_leverage_info_with_another_lever_stops_the_start(self):
        self.fx.fakes["arena-a"].info_lever = "5"
        lab = self.fx.lab
        result = lab.start(lab.data["generation"], "arena_live", ["sample-trend", "sample-short"], ["arena-a", "arena-b"], 100, 300,
                           leverage=[3, 1])
        state = lab.load(result["id"])
        with self.assertRaisesRegex(PreflightError, "^leverage_unconfirmed$"):
            lab.prepare(state, lab.chosen_profiles(["arena-a", "arena-b"]), False)
        self.assertFalse(self.fx.fakes["arena-a"].orders)
        self.fx.fakes["arena-a"].lever, self.fx.fakes["arena-a"].info_lever = "1", "3"  # and at 1x: the old reason
        api = self.fx.fakes["arena-a"].api("live")
        api.arm(100)
        with self.assertRaisesRegex(PreflightError, "^one_x_leverage_unconfirmed$"):
            api.set_leverage()

    def test_bad_tiers_refuse_a_leveraged_start_before_the_setting(self):
        for rows in ([], [{"instFamily": "BTC-USDT", "tier": "2", "minSz": "1", "maxSz": "9", "mmr": "0.004", "maxLever": "100"}],
                     [{"instFamily": "ETH-USDT", "tier": "1", "minSz": "0", "maxSz": "9", "mmr": "0.004", "maxLever": "100"}],
                     [{"instFamily": "BTC-USDT", "tier": "1", "minSz": "0", "maxSz": "9", "mmr": "", "maxLever": "100"}]):
            with self.subTest(rows=rows):
                fake = self.fx.fakes["arena-a"]
                fake.tiers, fake.set_posts = rows, []
                lab = self.fx.lab
                result = lab.start(lab.data["generation"], "arena_live", ["sample-trend"], ["arena-a"], 100, 300, leverage=[3])
                state = lab.load(result["id"])
                with self.assertRaisesRegex(PreflightError, "^invalid_position_tiers$|^invalid_numeric_data$"):
                    lab.prepare(state, lab.chosen_profiles(["arena-a"]), False)
                self.assertEqual(fake.set_posts, [])
                self.assertFalse(fake.orders)
                state["status"] = "DONE"
                lab.save(state)

    def test_the_computed_liquidation_reproduces_okx_s_example(self):
        """OKX's help example (as in L1's method): 3x, entry 60 996, 50 contracts, margin 5 000 USDT -> 51 226.5, with the
        tier's mmr 0.4 % and the taker fee 0.05 %; and the margin-free form for a short."""
        from trading_lab.exchange_lab import computed_liquidation
        bot = {"leverage": 3, "_fee_rate": "-0.0005", "_tiers": [{"min_contracts": "0", "max_contracts": "1000", "mmr": "0.004"}],
               "_instrument": {"contract_value": "0.01", "contract_multiplier": "1", "tick_size": "0.1"}}
        price, tier = computed_liquidation(bot, Decimal(50), Decimal(60996), Decimal(5000))
        self.assertEqual(price, Decimal("51226.5"))
        self.assertEqual(tier["mmr"], "0.004")
        short, _ = computed_liquidation(bot, Decimal(-50), Decimal(60996), None)
        self.assertEqual(short, (Decimal(60996) * (1 + Decimal(1) / 3) / Decimal("1.0045") / Decimal(".1")).to_integral_value() * Decimal(".1"))
        self.assertEqual(computed_liquidation({**bot, "_tiers": []}, Decimal(50), Decimal(60996), None), (None, None))
        self.assertEqual(computed_liquidation(bot, Decimal(2000), Decimal(60996), None), (None, None))  # beyond every tier

    def test_the_api_allows_only_the_bots_own_lever(self):
        api = self.fx.fakes["arena-a"].api("live")
        body = lambda lever: {"instId": "BTC-USDT-SWAP", "lever": lever, "mgnMode": "isolated"}
        with self.assertRaisesRegex(PreflightError, "^exchange_leverage_must_be_one$"):
            api.validate_write(SET, body("3"))
        api.validate_write(SET, body("1"))
        api.leverage = 3
        api.validate_write(SET, body("3"))
        for wrong in ("1", "5", "3.0", 3):
            with self.subTest(wrong=wrong), self.assertRaisesRegex(PreflightError, "^exchange_leverage_not_the_bots$"):
                api.validate_write(SET, body(wrong))
        with self.assertRaises(PreflightError):
            api.validate_write(SET, {**body("3"), "mgnMode": "cross"})

    def test_arm_scales_the_entry_limit_with_the_leverage_and_refuses_a_bad_one(self):
        api = self.fx.fakes["arena-a"].api("live")
        api.arm(100)
        self.assertEqual(api.entry_limit, Decimal(30))
        api.leverage = 3
        api.arm(100)
        self.assertEqual(api.entry_limit, Decimal(90))
        for bad in (0, 11, "3", True, 2.0):
            api.leverage = bad
            with self.subTest(bad=bad), self.assertRaisesRegex(PreflightError, "^invalid_execution_leverage$"):
                api.arm(100)

    def test_a_position_at_the_bots_lever_is_own_and_any_other_is_foreign(self):
        fake = self.fx.fakes["arena-a"]
        fake.units, fake.basis, fake.lever = Decimal("0.05"), Decimal(84000), "3"
        api = fake.api("live")
        with self.assertRaisesRegex(PreflightError, "^foreign_or_unsupported_position$"):
            api.position()  # a 1x bot never takes a 3x position as its own
        api.leverage = 3
        self.assertEqual(api.position(), Decimal("0.05"))
        self.assertEqual(api.last_position["lever"], "3")
        self.assertEqual(api.last_position["avgPx"], "84000")
        self.assertIn("liqPx", api.last_position)
        fake.lever = "5"
        with self.assertRaisesRegex(PreflightError, "^foreign_or_unsupported_position$"):
            api.position()
        fake.units, fake.lever = Decimal(0), "3"
        self.assertEqual(api.position(), 0)
        self.assertIsNone(api.last_position)

    def test_ll02_a_1x_bot_in_a_leveraged_round_gets_its_facts_too(self):
        """D189 (LL-02): every bot of a round with the leverage block gets the tiers read and its liquidation facts; a 1x
        long without OKX's liqPx computes 0 (never liquidated), its distance 100 %, and nothing blocks it."""
        self.fx.fakes["arena-a"].liq = ""
        state = self.start([1, 3])
        for name in ("arena-a", "arena-b"):
            self.assertEqual(len([r for r in self.fx.fakes[name].requests if urlsplit(r.full_url).path == TIERS]), 1)
        trend = next(b for b in state["bots"] if b["id"] == "sample-trend")
        for _ in range(4):
            self.fx.lab.step(state)
            self.fx.now += 5
        self.assertNotEqual(trend["position_btc"], 0)
        facts = [e["data"] for e in state["events"] if e["code"] == "entry_position_facts" and e.get("bot_id") == trend["id"]]
        self.assertTrue(facts)
        self.assertEqual(facts[0]["leverage"], 1)
        self.assertEqual((facts[0]["liquidation_price"], facts[0]["liquidation_source"], facts[0]["distance_ok"]), ("0", "model_1x_long", True))
        self.assertEqual(facts[0]["liquidation_distance_bps"], "10000.00")
        self.assertFalse(trend.get("entries_blocked"))
        self.assertFalse([e for e in state["events"] if e["code"] in {"liquidation_closer_than_stop", "liquidation_too_close"}])
        full = deepcopy(state)
        restore_events(self.fx.lab.path / (full["id"] + ".events.jsonl"), full, full=True)
        exported = [e["data"] for e in build_review(full)["events"] if e["code"] == "entry_position_facts" and e.get("bot_id") == trend["id"]]
        self.assertEqual(exported[0]["liquidation_source"], "model_1x_long")  # the bundle names it apart from OKX's liqPx

    def test_the_status_gives_the_form_each_versions_limit(self):
        limits = self.fx.lab.status()["leverage_limits"]
        self.assertEqual(limits[self.sha["sample-trend"]], 10)
        self.assertEqual(limits["d744a6f52356d446e740090d35eb8dc520b7cdcb4c238722265140125edd7752"], 8)

    def test_the_panel_accepts_the_leverage_key(self):
        from trading_lab.web_panel import Handler
        handler = Handler.__new__(Handler)
        handler.path, sent = "/api/exchange/start", []
        handler.send = lambda code, body: sent.append((code, body))
        handler.server = Mock()
        handler.server.panel.status.return_value = {"running": False, "action": "new"}
        body = {"generation": 0, "context": "arena_live", "strategy_ids": ["a"], "account_ids": ["arena-a"], "budget_usdt": 100,
                "duration_seconds": 300}
        handler.exchange_post({**body, "leverage": [3]})
        handler.server.exchange.start.assert_called_with(**body, leverage=[3])
        with self.assertRaises(ArenaError):
            handler.exchange_post({**body, "lever": [3]})


class Sizing(unittest.TestCase):
    """Margin as the budget's share, notional = margin x leverage; the veto by margin and the notional stop limit (V1, BTC)."""

    def setUp(self):
        self.fx = fixture.ExchangeTests(methodName="runTest")
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        for fake in self.fx.fakes.values():
            fake.__class__ = LeverFake
        known = {r["id"]: r for r in self.fx.arena.catalogue()}
        table = limits_for((known["sample-trend"]["recipe_sha256"], (10, "Test")))
        table.start()
        self.addCleanup(table.stop)
        lab = self.fx.lab
        self.state = lab.load(lab.start(lab.data["generation"], "arena_live", ["sample-trend"], ["arena-a"], 100, 300, leverage=[3])["id"])
        lab.prepare(self.state, lab.chosen_profiles(["arena-a"]), False)
        self.addCleanup(lambda: lab.resources.pop(self.state["id"], {"stack": Mock()})["stack"].close())
        self.bot, self.recipe = self.state["bots"][0], self.state["strategies"][0]
        self.snapshot = lab.resources[self.state["id"]]["feed"].snapshot()

    def body(self, leverage, recipe=None, **bot):
        b = {**deepcopy(self.bot), "leverage": leverage, **bot}
        return engine.entry_body(self.state, b, recipe or self.recipe, self.snapshot, self.fx.lab.identity_for(self.state, self.bot, "E"))

    def expected(self, leverage):
        rules, ct = self.recipe["rules"], Decimal(".01")
        price = Decimal(self.body(1)["px"])
        units = (Decimal(100) * Decimal(str(rules["size_pct"])) / 100 * leverage / price / ct / ct).to_integral_value(rounding="ROUND_DOWN") * ct
        return units

    def test_the_notional_is_the_margin_times_the_leverage(self):
        for leverage in (1, 2, 3, 10):
            with self.subTest(leverage=leverage):
                self.assertEqual(Decimal(self.body(leverage)["sz"]), self.expected(leverage))
        self.assertGreater(Decimal(self.body(3)["sz"]), 2 * Decimal(self.body(1)["sz"]))

    def test_the_exposure_veto_counts_the_margin_not_the_notional(self):
        body = self.body(3)
        exposure = Decimal(body["sz"]) * Decimal(".01") * Decimal(body["px"])
        margin = exposure / 3 + exposure * Decimal(".0005")
        just = float((margin / Decimal(".95")).quantize(Decimal(".0001"), rounding="ROUND_UP"))
        self.body(3, equity_usdt=just)  # the margin fits
        with self.assertRaisesRegex(PreflightError, "minimum_lot_liquidity_or_exposure_veto"):
            self.body(3, equity_usdt=just - .01)
        with self.assertRaisesRegex(PreflightError, "minimum_lot_liquidity_or_exposure_veto"):
            self.body(1, equity_usdt=float(exposure / Decimal(".95")) - .01, _contracts=str(Decimal(body["sz"]) * 2))  # 1x: the notional

    def test_above_1x_the_loss_at_the_stop_on_the_notional_is_at_most_5_percent_of_the_budget(self):
        body = self.body(3)
        exposure = Decimal(body["sz"]) * Decimal(".01") * Decimal(body["px"])
        edge = int((Decimal(5) * 10000 / exposure))  # stop_loss_bps at which the loss on the notional is 5 USDT of 100
        recipe = deepcopy(self.recipe)
        recipe["rules"]["stop_loss_bps"] = edge
        self.body(3, recipe)
        recipe["rules"]["stop_loss_bps"] = edge + 1
        with self.assertRaisesRegex(PreflightError, "^stop_loss_notional_veto$"):  # D181 p. 1: its own reason
            self.body(3, recipe)
        one = self.body(1)
        exposure_1x = Decimal(one["sz"]) * Decimal(".01") * Decimal(one["px"])
        recipe["rules"]["stop_loss_bps"] = int(Decimal(5) * 10000 / exposure_1x) + 50  # past 5 % even at 1x
        self.body(1, recipe)  # 1x: no such limit, as before

    def test_ll08_the_stop_limit_counts_the_whole_position_after_an_add(self):
        body = self.body(3)
        exposure = Decimal(body["sz"]) * Decimal(".01") * Decimal(body["px"])
        recipe = deepcopy(self.recipe)
        recipe["rules"]["stop_loss_bps"] = int(Decimal(5) * 10000 / exposure)  # this entry alone: within 5 %
        self.body(3, recipe)
        with self.assertRaisesRegex(PreflightError, "^stop_loss_notional_veto$"):
            self.body(3, recipe, _contracts=body["sz"], equity_usdt=1000)  # an add on the same size: twice the notional

    def test_the_api_entry_limit_is_30_percent_margin(self):
        api = self.fx.lab.resources[self.state["id"]]["apis"][0]
        self.assertEqual(api.leverage, 3)
        self.assertEqual(api.entry_limit, Decimal(90))
        body = self.body(3)
        api.validate_write(ORDER, body)
        big = {**body, "sz": str(Decimal(91) / Decimal(body["px"]) / Decimal(".01") // Decimal(".01") * Decimal(".01") + Decimal(".01"))}
        with self.assertRaisesRegex(PreflightError, "exchange_entry_notional_limit"):
            api.validate_write(ORDER, big)

    def test_a_first_v1_entry_at_3x_on_the_venue(self):
        self.fx.lab.step(self.state)
        entry = next(o for o in self.fx.fakes["arena-a"].orders.values() if not o["reduceOnly"])
        self.assertEqual(Decimal(entry["sz"]), self.expected(3))


class LeveragedSeason(unittest.TestCase):
    """A Live RULES_ONLY schema-4 season (ETH long, IOC) at 3x: the G12 one lot, the position facts, the liquidation guards."""
    SIDE, LEVERAGE = "long", 3

    def setUp(self):
        self.fx = fixture.ExchangeTests(methodName="runTest")
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        for fake in self.fx.fakes.values():
            fake.__class__ = LeverOutcome
            fake.use(fixture.ETH_SPEC, outcomes.PRICE, 0)
            fake.refused = []
        self.fx.lab.jev_factory = Mock(side_effect=AssertionError("No Jev for RULES_ONLY"))
        shop = self.fx.workshop
        recipe = json.loads(outcomes.RECIPE.read_text(encoding="utf-8"))
        recipe["rules"]["order"] = {"type": "taker"}
        recipe["rules"]["side"] = self.SIDE
        version = shop.add_version(shop.data["generation"], recipe)["id"]
        shop.train(shop.data["generation"], version, "synthetic-suite-v2", 100)
        shop.freeze(shop.data["generation"], version)
        self.promoted = shop.promote(shop.data["generation"], version)["id"]
        sha = next(r for r in self.fx.arena.catalogue() if r["id"] == self.promoted)["recipe_sha256"]
        table = limits_for((sha, (3, "Test ETH")))
        table.start()
        self.addCleanup(table.stop)
        lab = self.fx.lab
        self.state = lab.load(lab.start(lab.data["generation"], "arena_live", [self.promoted], ["arena-a"], 25, 900,
                                        execution_mode=policy.RULES_ONLY, leverage=[self.LEVERAGE])["id"])
        lab.prepare(self.state, lab.chosen_profiles(["arena-a"]), False)
        lab.resources[self.state["id"]]["wakeup"].wait = lambda seconds: False
        self.addCleanup(lambda: lab.resources.pop(self.state["id"], {"stack": Mock()})["stack"].close())
        self.bot, self.fake, self.start = self.state["bots"][0], self.fx.fakes["arena-a"], self.fx.now
        self.stop_bps = Decimal(str(recipe["rules"]["stop_loss_bps"]))

    def run_until(self, until, limit=200, dt=5):
        for _ in range(limit):
            self.fx.lab.step(self.state)
            if self.state["status"] in {"DONE", "PAUSED"} or until():
                return
            self.fx.now += dt

    def events(self, code):
        return [e for e in self.state["events"] if e["code"] == code]

    def hold(self):
        self.run_until(lambda: self.bot["position_btc"] != 0 and bool(self.events("entry_position_facts")))
        self.assertNotEqual(self.bot["position_btc"], 0)

    def review(self):
        full = deepcopy(self.state)
        restore_events(self.fx.lab.path / (full["id"] + ".events.jsonl"), full, full=True)
        return build_review(full)

    def posts(self, path=ORDER):
        return [json.loads(r.data) for r in self.fake.requests if r.get_method() == "POST" and urlsplit(r.full_url).path == path]

    def test_one_lot_under_g12_whatever_the_leverage(self):
        self.hold()
        entry = self.posts()[0]
        self.assertEqual(Decimal(entry["sz"]), Decimal(fixture.ETH_SPEC["minSz"]))
        self.assertEqual(self.fake.set_posts, [{"instId": "ETH-USDT-SWAP", "lever": "3", "mgnMode": "isolated"}])

    def test_entry_position_facts_on_the_first_agreeing_read(self):
        self.hold()
        facts = self.events("entry_position_facts")
        self.assertEqual(len(facts), 1)
        data = facts[0]["data"]
        avg, liq = Decimal(data["avg_price"]), Decimal(data["liquidation_price"])
        contracts = Decimal(data["position_contracts"])
        self.assertEqual(data["leverage"], 3)
        self.assertEqual(data["margin_mode"], "isolated")
        self.assertEqual(Decimal(data["notional_usdt"]), abs(contracts) * Decimal(fixture.ETH_SPEC["ctVal"]) * avg)  # USDT, own data
        self.assertNotEqual(Decimal(data["notional_usd"]), Decimal(data["notional_usdt"]))  # OKX's USD field apart
        self.assertEqual(Decimal(data["liquidation_distance_bps"]), (abs(avg - liq) / avg * 10000).quantize(Decimal(".01")))
        self.assertEqual(Decimal(data["required_distance_bps"]), 2 * self.stop_bps)
        self.assertTrue(data["distance_ok"])
        self.assertEqual(data["liquidation_reads"], 1)
        self.fx.now += 5
        self.fx.lab.step(self.state)
        self.assertEqual(len(self.events("entry_position_facts")), 1)  # the same size: once
        self.assertFalse(self.bot.get("entries_blocked"))

    def test_an_empty_liq_px_is_computed_from_the_tier_and_never_blocks_entries(self):
        """D181 p. 2: without OKX's liqPx the engine computes it (entry price, margin, tier mmr plus the taker fee) and
        names the source; it reads OKX's on the next two checks; no block for that alone."""
        self.fake.liq = ""
        self.hold()
        for _ in range(4):
            self.fx.now += 5
            self.fx.lab.step(self.state)
        facts = self.events("entry_position_facts")
        self.assertEqual(len(facts), 1)
        data = facts[0]["data"]
        self.assertEqual((data["liquidation_source"], data["tier_mmr"], data["distance_ok"]), ("computed", "0.004", True))
        avg, margin, base = Decimal(data["avg_price"]), Decimal(data["margin_usdt"]), abs(Decimal(data["position_contracts"])) * Decimal(".1")
        m = Decimal(".004") + Decimal(".0005")
        expected = (base * avg - margin) / (base * (1 - m)) if self.SIDE == "long" else (base * avg + margin) / (base * (1 + m))
        self.assertEqual(Decimal(data["liquidation_price"]), (expected / Decimal(".01")).to_integral_value() * Decimal(".01"))
        self.assertFalse(self.events("liquidation_unknown"))
        self.assertFalse(self.bot.get("entries_blocked"))
        self.assertIsNone(self.bot["_liq_reads"])  # read three times, then no more
        exported = next(e for e in self.review()["events"] if e["code"] == "entry_position_facts")["data"]
        self.assertEqual((exported["liquidation_source"], exported["tier_mmr"]), ("computed", "0.004"))

    def test_a_liq_px_shown_one_read_late_gives_the_okx_facts_again(self):
        self.fake.liq = ""
        self.hold()
        self.fake.liq = None
        self.fx.now += 5
        self.fx.lab.step(self.state)
        facts = self.events("entry_position_facts")
        self.assertEqual([(f["data"]["liquidation_reads"], f["data"]["liquidation_source"]) for f in facts], [(1, "computed"), (2, "okx")])
        self.assertNotIn("tier_mmr", facts[1]["data"])
        self.assertTrue(facts[1]["data"]["distance_ok"])
        self.assertFalse(self.events("liquidation_unknown"))
        self.assertFalse(self.bot.get("entries_blocked"))
        self.fx.now += 5
        self.fx.lab.step(self.state)
        self.assertEqual(len(self.events("entry_position_facts")), 2)

    def test_with_neither_okx_nor_a_tier_the_bots_entries_are_blocked_after_three_reads(self):
        self.fake.liq = ""
        self.bot["_tiers"] = []  # no tier covers the size: nothing to compute from
        self.hold()
        for read in (2, 3):
            self.assertFalse(self.events("liquidation_unknown"), read)  # not before the third read
            self.assertFalse(self.bot.get("entries_blocked"))
            self.fx.now += 5
            self.fx.lab.step(self.state)
        for _ in range(2):
            self.fx.now += 5
            self.fx.lab.step(self.state)
        facts = self.events("entry_position_facts")
        self.assertEqual(len(facts), 1)
        self.assertEqual((facts[0]["data"]["liquidation_price"], facts[0]["data"]["liquidation_source"], facts[0]["data"]["distance_ok"]),
                         (None, None, None))
        self.assertEqual(len(self.events("liquidation_unknown")), 1)
        self.assertEqual(self.bot["entries_blocked"], "liquidation_unknown")
        self.assertFalse(self.bot["stop_requested"])  # the position stays: its stop on the venue protects it
        flat = {**self.bot, "position_btc": 0, "_contracts": "0"}
        cand = engine.candidate(self.state, flat, self.state["strategies"][0], self.state["_last"], self.fx.now)
        self.assertFalse(cand["local_checks_passed"])
        from trading_lab.exchange_lab import entry_blockers
        passed = {**cand, "checks": [{**c, "passed": True} for c in cand["checks"]]}
        self.assertIn("admission_blocked", entry_blockers(self.state, flat, passed, self.fx.now))
        exported = next(e for e in self.review()["events"] if e["code"] == "entry_position_facts")["data"]
        self.assertIsNone(exported["liquidation_price"])
        self.assertIsNone(exported["distance_ok"])

    def test_the_block_alone_refuses_a_candidate_the_rules_would_allow(self):
        self.hold()
        from trading_lab import strategy_runtime as runtime
        flat = deepcopy(self.bot)
        flat.update(position_btc=0, _contracts="0", entries=0, _entry_attempts=0, _orders=[], protection={"verified": True})
        flat.pop("entries_blocked", None)
        recipe, snapshot = self.state["strategies"][0], self.state["_last"]
        with patch.object(runtime, "entry", return_value={"allowed": True, "checks": [], "side": self.SIDE}),                 patch.object(runtime, "exit_view", return_value=(False, [])):
            free = engine.candidate(self.state, flat, recipe, snapshot, self.fx.now)
            blocked = engine.candidate(self.state, {**flat, "entries_blocked": "liquidation_too_close"}, recipe, snapshot, self.fx.now)
        self.assertTrue(free["local_checks_passed"], free["checks"])
        self.assertFalse(blocked["local_checks_passed"])

    def test_ll04_facts_without_avg_px_are_given_again_once_it_comes(self):
        """RC2 (LL-04): the first agreeing read has no avgPx (no distance known): reads go on, and the facts come again
        with the price and distance when avgPx comes."""
        self.fake.no_avg = True
        self.hold()
        first = self.events("entry_position_facts")[0]["data"]
        self.assertIsNone(first["liquidation_distance_bps"])
        self.fake.no_avg = False
        self.fx.now += 5
        self.fx.lab.step(self.state)
        facts = self.events("entry_position_facts")
        self.assertEqual(len(facts), 2)
        self.assertIsNotNone(facts[1]["data"]["liquidation_distance_bps"])
        self.assertEqual(facts[1]["data"]["liquidation_source"], "okx")
        self.assertFalse(self.events("liquidation_unknown"))

    def test_ll01_a_liquidation_bill_is_named_once_whatever_the_resumes(self):
        self.hold()
        self.liquidate(101)
        api = self.fx.lab.resources[self.state["id"]]["apis"][0]
        self.fx.lab.liquidation_seen(self.state, self.bot, api)
        self.fx.lab.liquidation_seen(self.state, self.bot, api)  # as after a Resume: the same bills again
        self.assertEqual(len(self.events("liquidation_detected")), 1)
        self.fake.bills.append({**self.fake.bills[-1], "billId": "100201", "subType": "126", "type": "9"})
        self.fx.lab.liquidation_seen(self.state, self.bot, api)
        detected = self.events("liquidation_detected")
        self.assertEqual(len(detected), 2)
        self.assertEqual(detected[1]["data"]["bill_subtypes"], ["126"])  # only the new row

    def test_the_tiers_are_read_once_at_the_start_and_kept_in_the_journal(self):
        reads = [r for r in self.fake.requests if urlsplit(r.full_url).path == TIERS]
        self.assertEqual(len(reads), 1)
        self.assertIn("instFamily=ETH-USDT", reads[0].full_url)
        self.assertIn("tdMode=isolated", reads[0].full_url)
        self.assertEqual(self.bot["_tiers"][0], {"tier": "1", "min_contracts": "0", "max_contracts": "5000", "mmr": "0.004",
                                                 "max_leverage": "100"})

    def test_a_liquidation_under_twice_the_stop_blocks_entries_and_keeps_the_position(self):
        move = Decimal(1) - 15 * self.stop_bps / 10000 / 10 if self.SIDE == "long" else Decimal(1) + 15 * self.stop_bps / 10000 / 10
        self.fake.liq = str((outcomes.PRICE * move).quantize(Decimal(".01")))  # 1.5 stops away
        self.hold()
        self.assertEqual(len(self.events("liquidation_too_close")), 1)
        self.assertFalse(self.events("entry_position_facts")[0]["data"]["distance_ok"])
        self.assertEqual(self.bot["entries_blocked"], "liquidation_too_close")
        self.assertFalse(self.bot["stop_requested"])
        self.assertEqual(len([p for p in self.posts() if p["reduceOnly"]]), 0)

    def test_a_liquidation_at_or_inside_the_stop_closes_reduce_only(self):
        move = Decimal(1) - self.stop_bps / 10000 / 2 if self.SIDE == "long" else Decimal(1) + self.stop_bps / 10000 / 2
        self.fake.liq = str((outcomes.PRICE * move).quantize(Decimal(".01")))  # half a stop away
        self.run_until(lambda: bool(self.events("entry_position_facts")))
        self.assertEqual(len(self.events("liquidation_closer_than_stop")), 1)
        stop = self.events("stop_requested")[-1]["data"]
        self.assertEqual(stop["reason_code"], "risk_limit")
        self.run_until(lambda: False, limit=20)
        self.assertEqual(self.state["status"], "DONE")
        closes = [p for p in self.posts() if p["reduceOnly"]]
        self.assertTrue(closes)
        self.assertEqual(self.fake.units, 0)
        self.assertEqual(len(self.events("liquidation_closer_than_stop")), 1)

    def liquidate(self, subtype):
        contracts = Decimal(self.fake.units)
        for algo in self.fake.algos.values():
            algo["state"] = "canceled"
        self.fake.units = Decimal(0)
        self.fake.bills.append({"billId": "100200", "ts": str(int(self.fx.now * 1000)), "type": "5" if subtype < 120 else "9",
                                "subType": str(subtype), "ccy": "USDT", "instId": self.fake.spec["instId"], "ordId": "100200",
                                "sz": str(abs(contracts)), "pnl": "-0.75", "fee": "-0.01", "balChg": "-0.76", "posBalChg": "-0.76"})

    def test_a_liquidation_or_adl_row_is_named_before_the_manual_review(self):
        for subtype, kind in ((101, "liquidation"), (126, "adl")):
            with self.subTest(kind=kind):
                if subtype == 126:
                    self.tearDown_and_setUp()
                self.hold()
                self.liquidate(subtype)
                self.fx.now += 5
                self.run_until(lambda: self.bot["_manual_review"], limit=5)
                detected = self.events("liquidation_detected")
                self.assertEqual(len(detected), 1)
                self.assertEqual(detected[0]["data"]["kind"], kind)
                self.assertEqual(detected[0]["data"]["bill_subtypes"], [str(subtype)])
                self.assertEqual(detected[0]["data"]["pnl_usdt"], "-0.75")
                self.assertTrue(self.bot["_manual_review"])
                failed = self.events("exchange_check_failed")[-1]["data"]
                self.assertEqual(failed["reason"], "actual_position_differs_from_owned_fills")
                self.assertTrue(self.state["incomplete"])

    def tearDown_and_setUp(self):
        self.doCleanups()
        self.setUp()

    def test_the_bundle_and_the_settings_name_each_bots_leverage(self):
        self.hold()
        review = self.review()
        row = review["participants"][0]["leverage"]
        self.assertEqual(row, {"leverage": 3, "leverage_limit": 3, "margin_mode": "isolated",
                               "size_cap": {"lots": 1, "units": str(Decimal(fixture.ETH_SPEC["minSz"]))}})
        self.assertEqual(review["match"]["leverage_table_sha256"], live_leverage.table_sha256())
        facts = next(e for e in review["events"] if e["code"] == "entry_position_facts")["data"]
        for key in ("leverage", "notional_usdt", "notional_usd", "liquidation_price", "liquidation_distance_bps",
                    "required_distance_bps", "margin_mode", "distance_ok"):
            self.assertIn(key, facts)
        settings = match_settings(self.state)
        self.assertNotIn("leverage", settings["risk_and_execution"])
        self.assertEqual(settings["leverage_table_sha256"], live_leverage.table_sha256())
        self.assertEqual({k: settings["participants"][0][k] for k in ("leverage", "leverage_limit", "margin_mode")},
                         {"leverage": 3, "leverage_limit": 3, "margin_mode": "isolated"})

    def test_the_cohort_key_takes_the_leverage_only_above_1x(self):
        from trading_lab.public_site import cohort_of
        self.assertEqual(leverage_cohort(self.state), {"leverage": [[self.state["strategies"][0]["recipe_sha256"], 3]]})
        flat = deepcopy(self.state)
        flat["match_manifest"].pop("leverage")
        self.assertEqual(leverage_cohort(flat), {})
        self.assertNotEqual(cohort_of(self.state), cohort_of(flat))

    def test_a_resume_sets_nothing_and_takes_the_3x_position_as_own(self):
        self.hold()
        lab = self.fx.lab
        lab.resources.pop(self.state["id"])["stack"].close()
        self.state["status"] = "RUNNING"
        lab.save(self.state)
        before = len(self.fake.set_posts)
        lab.resume(lab.data["generation"], self.state["id"])
        state = lab.load(self.state["id"])
        lab.prepare(state, lab.chosen_profiles(["arena-a"]), True)
        lab.resources[state["id"]]["wakeup"].wait = lambda seconds: False
        self.addCleanup(lambda: lab.resources.pop(state["id"], {"stack": Mock()})["stack"].close())
        self.assertEqual(len(self.fake.set_posts), before)  # no POST set-leverage on a Resume
        self.assertEqual(lab.resources[state["id"]]["apis"][0].leverage, 3)
        for _ in range(10):
            lab.step(state)
            if state["status"] in {"DONE", "PAUSED"}:
                break
            self.fx.now += 5
        self.assertEqual(state["status"], "DONE")
        self.assertFalse(state["bots"][0]["_manual_review"])
        self.assertEqual(self.fake.units, 0)


class LeveragedShortSeason(LeveragedSeason):
    SIDE = "short"


class OneXSeasonUnchanged(LeveragedSeason):
    """The same season at 1x: no new event, key or bills read for a liquidation (golden tests pin the bytes)."""
    LEVERAGE = 1

    def test_one_x_has_none_of_the_new_keys(self):
        self.run_until(lambda: self.bot["position_btc"] != 0)
        self.fx.now += 5
        self.fx.lab.step(self.state)
        self.assertNotIn("leverage", self.state["match_manifest"])
        self.assertNotIn("leverage", self.bot)
        self.assertFalse([e for e in self.state["events"] if e["code"] in {"entry_position_facts", "liquidation_unknown",
                          "liquidation_too_close", "liquidation_closer_than_stop", "liquidation_detected"}])
        review = self.review()
        self.assertNotIn("leverage", review["participants"][0])
        self.assertNotIn("leverage_table_sha256", review["match"])
        self.assertEqual(match_settings(self.state)["risk_and_execution"]["leverage"], 1)
        self.assertEqual(leverage_cohort(self.state), {})

    def test_one_x_reads_no_tier(self):
        self.assertFalse([r for r in self.fake.requests if urlsplit(r.full_url).path == TIERS])
        self.assertNotIn("_tiers", self.bot)

    def test_one_x_liquidation_reads_no_bills_first(self):
        self.run_until(lambda: self.bot["position_btc"] != 0)
        self.liquidate(101)
        self.fx.now += 5
        self.run_until(lambda: self.bot["_manual_review"], limit=5)
        self.assertFalse(self.events("liquidation_detected"))
        self.assertTrue(self.bot["_manual_review"])

    # the leveraged checks do not apply at 1x
    test_one_lot_under_g12_whatever_the_leverage = None
    test_entry_position_facts_on_the_first_agreeing_read = None
    test_an_empty_liq_px_is_computed_from_the_tier_and_never_blocks_entries = None
    test_a_liq_px_shown_one_read_late_gives_the_okx_facts_again = None
    test_with_neither_okx_nor_a_tier_the_bots_entries_are_blocked_after_three_reads = None
    test_the_tiers_are_read_once_at_the_start_and_kept_in_the_journal = None
    test_the_block_alone_refuses_a_candidate_the_rules_would_allow = None
    test_ll04_facts_without_avg_px_are_given_again_once_it_comes = None
    test_ll01_a_liquidation_bill_is_named_once_whatever_the_resumes = None
    test_a_liquidation_under_twice_the_stop_blocks_entries_and_keeps_the_position = None
    test_a_liquidation_at_or_inside_the_stop_closes_reduce_only = None
    test_a_liquidation_or_adl_row_is_named_before_the_manual_review = None
    test_the_bundle_and_the_settings_name_each_bots_leverage = None
    test_the_cohort_key_takes_the_leverage_only_above_1x = None
    test_a_resume_sets_nothing_and_takes_the_3x_position_as_own = None



class DemoLimits(unittest.TestCase):
    """Demo: the same limits; the technical plan E041 and the Demo mechanics stay 1x (D181 p. 2: no Demo at 3x)."""

    def setUp(self):
        self.fx = fixture.ExchangeTests(methodName="runTest")
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        for fake in self.fx.fakes.values():
            fake.__class__ = LeverFake

    def test_e041_and_a_workshop_draft_above_1x_are_refused_before_any_request(self):
        from trading_lab.cycle_probe import template
        lab, shop = self.fx.lab, self.fx.workshop
        identity = shop.add_version(shop.data["generation"], template())["id"]
        for version in (identity, self.fx.version):
            with self.subTest(version=version), self.assertRaisesRegex(ArenaError, "межа версії 1x"):
                lab.start(lab.data["generation"], "workshop_demo", [version], ["demo"], 100, 300, leverage=[2])
        self.assertTrue(all(not f.requests for f in self.fx.fakes.values()))

    def test_a_listed_version_on_demo_is_1x_only(self):
        """D189 (LL-03): Demo takes no leverage, as the gates' model; Live takes the version's limit."""
        known = {r["id"]: r for r in self.fx.arena.catalogue()}
        from trading_lab.exchange_lab import leverage_plan
        recipe = {"recipe_sha256": known["sample-trend"]["recipe_sha256"], "name": "x"}
        with limits_for((recipe["recipe_sha256"], (3, "Test"))):
            with self.assertRaisesRegex(ArenaError, "межа версії 1x"):
                leverage_plan("workshop_demo", "shared_jev", [recipe], [2])
            self.assertEqual(leverage_plan("workshop_demo", "shared_jev", [recipe], [1]), ([1], [1]))
            self.assertEqual(leverage_plan("arena_live", "shared_jev", [recipe], [3]), ([3], [3]))
            with self.assertRaises(ArenaError):
                leverage_plan("arena_live", "shared_jev", [recipe], [4])

    def test_the_plan_function_keeps_the_demo_mechanics_1x(self):
        from trading_lab.exchange_lab import leverage_plan
        from trading_lab import demo_execution
        from trading_lab.cycle_probe import template
        probe = {**template(), "recipe_sha256": "p" * 64}
        with limits_for(("x" * 64, (3, "X")), ("p" * 64, (3, "P"))):  # even a listed recipe: 1x for these two
            with self.assertRaises(ArenaError):
                leverage_plan("workshop_demo", demo_execution.MODE, [{"recipe_sha256": "x" * 64, "name": "m"}], [2])
            with self.assertRaises(ArenaError):
                leverage_plan("workshop_demo", "shared_jev", [probe], [2])
            self.assertEqual(leverage_plan("arena_live", "shared_jev", [{"recipe_sha256": "x" * 64}], [3]), ([3], [3]))
        with self.assertRaises(ArenaError):
            leverage_plan("workshop_demo", demo_execution.MODE, [{"recipe_sha256": "x", "name": "m"}], [2])
        self.assertEqual(leverage_plan("workshop_demo", demo_execution.MODE, [{"recipe_sha256": "x"}], [1]), ([1], [1]))
        self.assertEqual(leverage_plan("arena_live", "shared_jev", [{}, {}], None), ([1, 1], [1, 1]))


if __name__ == "__main__":
    unittest.main()
