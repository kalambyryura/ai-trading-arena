"""New victory eligibility and historical isolation. Fake journals and temporary roots only."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_league_evidence_integration import scored_bundle, rehash_bundle, FakeController
from test_replay_evidence_integration import exchange_fixture
from test_cycle_diagnostics import filled, settled
from trading_lab.battle_review import build_review
from trading_lab.league import score, public_standings, League
from trading_lab.match_protocol import SCORE_POLICY, score_policy
import test_exchange_lab as fixtures
from trading_lab.arena_strategies import ArenaError


def active_bundle(nets, fills):
    bundle = scored_bundle(nets)
    bundle["match"]["score_policy"] = deepcopy(SCORE_POLICY)
    for row, count in zip(bundle["participants"], fills):
        row["funnel"]["entry_filled_orders"] = count
    return rehash_bundle(bundle)


class ParticipationRuleTests(unittest.TestCase):
    def test_best_trader_can_win_at_a_loss_cash_and_idle_cannot(self):
        result = score(active_bundle(["0", "-.1", "-.2"], [0, 1, 3]))
        self.assertEqual(result["sole_winner"], "fictional-2")
        self.assertEqual(result["ordered"], ["fictional-2", "fictional-3"])
        self.assertEqual(result["excluded"], [{"id": "fictional-1", "reason": "no_filled_entry"}])
        self.assertEqual(len(result["known_results"]), 3)

    def test_no_entries_is_no_contest_and_stays_in_economic_statistics(self):
        result = score(active_bundle(["0", "0"], [0, 0]))
        self.assertEqual(result["status"], "NO_CONTEST")
        self.assertIsNone(result["sole_winner"])
        self.assertEqual(result["shared_top"], [])
        battle = {"competitive_validity": "ELIGIBLE", "score": result, "cohort": "new",
                  "participants": [{"id": r["id"], "recipe_sha256": r["id"]} for r in result["known_results"]]}
        rows, cohorts = public_standings([battle])
        self.assertTrue(all(r["sole_wins"] == r["shared_top"] == 0 for r in rows))
        self.assertEqual(cohorts[0]["no_contest_battles"], 1)
        for row in rows:
            if row["id"] != "cash":
                self.assertEqual((row["participated_battles"], row["no_entry_battles"]), (0, 1))
                self.assertEqual(row["net_sum_usdt"], "0")

    def test_decimal_tie_and_profit_have_no_cash_contender(self):
        result = score(active_bundle([".01", ".010000009", "0"], [1, 1, 0]))
        self.assertEqual(result["status"], "TIE_AT_TOP")
        self.assertCountEqual(result["shared_top"], ["fictional-1", "fictional-2"])
        self.assertEqual(score(active_bundle([".01", ".010000011"], [1, 1]))["sole_winner"], "fictional-2")

    def test_intents_signals_and_adds_never_replace_entry_fill_evidence(self):
        bundle = active_bundle(["0"], [0])
        bundle["participants"][0]["funnel"].update(order_intents=4, add_filled_orders=2)
        bundle["participants"][0]["funnel"]["entry"].update(signals=10, jev_allows=10)
        self.assertEqual(score(rehash_bundle(bundle))["status"], "NO_CONTEST")
        for count in (None, True, -1, "1"):
            with self.subTest(count=count):
                self.assertEqual(score(active_bundle(["1"], [count]))["status"], "INELIGIBLE")

    def test_filled_exchange_entry_qualifies_even_after_flat_and_costs(self):
        state = exchange_fixture()
        state["match_manifest"] = {"score_policy": deepcopy(SCORE_POLICY)}
        bundle = build_review(state)
        self.assertEqual(bundle["participants"][0]["participation"]["status"], "PARTICIPATED")
        self.assertTrue(bundle["participants"][0]["participation"]["eligible_to_win"])
        self.assertEqual(score(bundle)["sole_winner"], state["bots"][0]["id"])

    def test_zero_fill_cancelled_exchange_order_is_not_participation(self):
        state = exchange_fixture()
        state["events"] = []
        bot = state["bots"][0]
        bot.update(_orders=[], position_btc=0)
        _, observed = filled(state, bot, "FICTIONAL_UNFILLED", qty="0", fee="0")
        observed["data"]["result"]["state"] = "canceled"
        settled(state, bot, gross="0", fees="0", funding="0")
        state["match_manifest"] = {"score_policy": deepcopy(SCORE_POLICY)}
        bundle = build_review(state)
        self.assertEqual(bundle["participants"][0]["participation"]["status"], "NO_ENTRY")
        self.assertEqual(score(bundle)["status"], "NO_CONTEST")

    def test_unknown_policy_or_unsettled_trade_never_gets_a_winner(self):
        bundle = active_bundle(["1"], [1])
        bundle["match"]["score_policy"]["minimum_entry_filled_orders"] = 0
        self.assertEqual(score(rehash_bundle(bundle))["status"], "INELIGIBLE")
        self.assertIsNone(score_policy({"score_policy": None}))
        bundle = active_bundle(["1"], [1])
        bundle["participants"][0]["position_btc"] = "-.0003"
        self.assertEqual(score(rehash_bundle(bundle))["status"], "INCOMPLETE")

    def test_legacy_score_and_bundle_projection_are_preserved(self):
        state = exchange_fixture()
        original = build_review(state)
        state["match_manifest"] = {"score_policy": {k: v for k, v in
            {"primary": "RECONCILED_NET_USDT", "tie_tolerance_usdt": "0.00000001", "include_cash": True}.items()}}
        self.assertEqual(build_review(state), original)
        self.assertEqual(score(original)["sole_winner"], "cash")
        self.assertCountEqual(score(scored_bundle(["0", "-.1"]))["shared_top"], ["cash", "fictional-1"])

    def test_scoring_policies_separate_cohorts_even_on_same_code(self):
        with tempfile.TemporaryDirectory() as directory:
            old = exchange_fixture()
            for row in old["strategies"]:  # R2.1.11 (E, D141 p. 2): an example author never counts; this test ranks
                if row.get("id") != "cash" and isinstance(row.get("author"), dict):
                    row["author"] = {**row["author"], "kind": "model"}
            new = deepcopy(old)
            new["id"] = "b" * 32
            new["match_manifest"] = {"score_policy": deepcopy(SCORE_POLICY)}
            paper = FakeController(Path(directory) / "paper")
            exchange = FakeController(Path(directory) / "exchange", [old, new], kind="runs")
            league = League(directory, paper, exchange).status()
            self.assertEqual(len({b["cohort"] for b in league["battles"]}), 2)
            winners = {b["id"]: b["score"]["sole_winner"] for b in league["battles"]}
            self.assertEqual(winners[old["id"]], "cash")
            self.assertEqual(winners[new["id"]], new["bots"][0]["id"])


class ParallelResearchTests(unittest.TestCase):
    setUp = fixtures.ExchangeTests.setUp
    tearDown = fixtures.ExchangeTests.tearDown

    def test_demo_can_run_beside_live_but_one_demo_account_has_one_worker(self):
        live_recipe = deepcopy(self.arena.catalogue()[0])
        self.arena.data["strategies"].append(live_recipe) if not any(s["id"]==live_recipe["id"] for s in self.arena.data["strategies"]) else None
        live = self.lab.start(self.lab.data["generation"], "arena_live", [live_recipe["id"]], ["arena-a"], 100, 300, "rules_only")
        demo = self.lab.start(self.lab.data["generation"], "workshop_demo", [self.version], ["demo"], 100, 300, "rules_only")
        self.assertNotEqual(live["id"], demo["id"])
        self.assertEqual(self.lab.load(live["id"])["strategies"][0]["recipe_sha256"], live_recipe["recipe_sha256"])
        with self.assertRaises(ArenaError):
            self.lab.start(self.lab.data["generation"], "workshop_demo", [self.version], ["demo"], 100, 300, "rules_only")
        self.assertTrue(all(not fake.orders for fake in self.fakes.values()))


if __name__ == "__main__": unittest.main()
