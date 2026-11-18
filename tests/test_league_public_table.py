"""Public league table (B-04): human battle names, passport identity, Σnet vs Cash per cohort. Temp roots only."""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

PATH = Path(__file__).resolve().parent
sys.path.insert(0, str(PATH.parent))
sys.path.insert(0, str(PATH))
from test_league_evidence_integration import FakeController
from test_replay_evidence_integration import paper_fixture
from trading_lab.league import League, duration_label, label_battles, public_identity, public_standings


def battle(identity, created, nets, sole=None, top=(), cohort="c" * 64, mode="OKX_LIVE", eligible=True):
    return {"id": identity, "label": created, "created_at": created, "mode": mode, "duration_seconds": 86400,
            "cohort": cohort, "allocation_usdt": "100", "execution_mode": "shared_jev",
            "competitive_validity": "ELIGIBLE" if eligible else "INELIGIBLE",
            "participants": [{"id": pid, "recipe_sha256": pid * 8, "version": 1, "name": "Стратегія " + pid,
                              "author_name": "Автор " + pid, "author_kind": "model", "model": "m-" + pid} for pid in nets],
            "score": {"status": "SOLE_WIN" if sole else "TIE_AT_TOP", "sole_winner": sole, "shared_top": list(top),
                      "known_results": [{"id": pid, "net_usdt": net} for pid, net in nets.items()]}}


class PublicStandingTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            battle("b1", "2026-10-06T10:00:00+00:00", {"aa": "0.01", "bb": "-0.02"}, sole="aa"),
            battle("b2", "2026-10-07T10:00:00+00:00", {"aa": "0", "bb": "0"}, top=("aa", "bb", "cash")),
            battle("b3", "2026-10-08T10:00:00+00:00", {"aa": "-0.005", "bb": "0.003"}, sole="bb"),
            battle("b4", "2026-10-09T10:00:00+00:00", {"aa": "5", "bb": "-5"}, sole="aa", eligible=False),
        ]

    def test_sums_mean_and_cash_comparison_use_eligible_battles_only(self):
        rows, cohorts = public_standings(self.rows)
        by = {r["id"]: r for r in rows}
        self.assertEqual(set(by), {"aa", "bb", "cash"})
        self.assertEqual(Decimal(by["aa"]["net_sum_usdt"]), Decimal("0.005"))
        self.assertEqual(Decimal(by["bb"]["net_sum_usdt"]), Decimal("-0.017"))
        self.assertEqual((by["aa"]["above_cash"], by["aa"]["below_cash"], by["aa"]["at_cash"]), (1, 1, 1))
        self.assertEqual((by["aa"]["sole_wins"], by["aa"]["shared_top"], by["aa"]["comparable_battles"]), (1, 1, 3))
        self.assertEqual((by["bb"]["sole_wins"], by["bb"]["shared_top"]), (1, 1))
        self.assertEqual((by["cash"]["net_sum_usdt"], by["cash"]["shared_top"], by["cash"]["sole_wins"]), ("0", 1, 0))
        self.assertAlmostEqual(Decimal(by["aa"]["net_mean_usdt"]), Decimal("0.005") / 3, places=12)
        self.assertNotIn("net_mean_se_usdt", by["aa"])  # no naive standard error: battles are not independent
        self.assertEqual((cohorts[0]["battles"], cohorts[0]["participants"]), (3, 2))
        self.assertEqual((cohorts[0]["mode_label"], cohorts[0]["duration_label"]), ("Live-бій", "24 год"))
        self.assertEqual((cohorts[0]["first_at"], cohorts[0]["last_at"]), ("2026-10-06T10:00:00+00:00", "2026-10-08T10:00:00+00:00"))
        self.assertEqual([r["id"] for r in rows], ["aa", "cash", "bb"])  # sorted by Σnet within the cohort

    def test_cohorts_never_merge(self):
        other = battle("o1", "2026-10-06T11:00:00+00:00", {"aa": "1"}, sole="aa", cohort="d" * 64)
        rows, cohorts = public_standings([self.rows[0], other])
        self.assertEqual(len(cohorts), 2)
        lone = next(r for r in rows if r["cohort"] == "d" * 64 and r["id"] == "aa")
        self.assertEqual((lone["comparable_battles"], lone["net_sum_usdt"], lone["net_mean_usdt"]), (1, "1", "1"))

    def test_human_labels_are_chronological_per_mode_and_rows_are_copies(self):
        paper = battle("p1", "2026-10-05T09:00:00+00:00", {"aa": "0"}, mode="PAPER")
        labelled = label_battles([self.rows[2], paper, self.rows[0], {"id": "broken", "status": "UNKNOWN"}])
        names = {r["id"]: r.get("display_label") for r in labelled}
        self.assertEqual(names["b1"], "Live-бій №1 · 24 год")
        self.assertEqual(names["b3"], "Live-бій №2 · 24 год")
        self.assertEqual(names["p1"], "PAPER-сезон №1 · 24 год")
        self.assertIsNone(names["broken"])
        self.assertNotIn("display_label", self.rows[0])
        self.assertEqual([duration_label(v) for v in (300, 900, 18000, 86400, 45, 0, True)],
                         ["5 хв", "15 хв", "5 год", "24 год", "45 с", None, None])

    def test_public_identity_never_exposes_prompts(self):
        recipe = {"name": "Відскок", "author": {"name": "Claude Opus · автор", "kind": "model", "model": "claude-opus-5-5",
                                                "prompt": "SECRET PROMPT", "attempts": 1000}}
        self.assertEqual(public_identity(recipe), {"name": "Відскок", "author_name": "Claude Opus · автор",
                                                   "author_kind": "model", "model": "claude-opus-5-5"})
        self.assertEqual(public_identity(None)["name"], None)
        self.assertEqual(public_identity({"author": {"kind": "admin"}})["author_kind"], None)

    def test_league_status_projects_names_labels_and_cohort_table(self):
        def settled(bundle, examples=frozenset()):  # The fixture is a mechanical PAPER replay with incomplete costs; only its score is faked.
            pid = bundle["participants"][0]["id"]
            if pid in examples:  # R2.1.11 (E): what the real score answers for a battle of examples only
                return {"status": "INELIGIBLE", "reason": "example_only", "known_results": [], "excluded": [{"id": pid, "reason": "example_author"}]}
            net = {"a" * 32: "0.02", "b" * 32: "-0.01"}[bundle["match"]["id"]]
            top = pid if Decimal(net) > 0 else "cash"
            return {"status": "SOLE_WIN", "sole_winner": top, "shared_top": [top], "known_results": [{"id": pid, "net_usdt": net}]}
        with tempfile.TemporaryDirectory() as directory, mock.patch("trading_lab.league.score", settled):
            first = paper_fixture()
            for row in first["strategies"]:  # R2.1.11 (E): a model's recipe ranks; the same battle of an example never does
                if row.get("id") != "cash":
                    row["author"] = {**row["author"], "kind": "model"}
            first.update(id="a" * 32, created_at="2026-10-06T10:00:00+00:00")
            second = deepcopy(first)
            second.update(id="b" * 32, created_at="2026-10-07T10:00:00+00:00")
            arena = FakeController(Path(directory) / "paper", [first, second])
            exchange = FakeController(Path(directory) / "exchange", kind="runs")
            state = League(directory, arena, exchange).status()
        battles = {b["id"]: b for b in state["battles"]}
        self.assertEqual(battles["a" * 32]["display_label"], "PAPER-сезон №1 · 5 хв")
        self.assertEqual(battles["b" * 32]["display_label"], "PAPER-сезон №2 · 5 хв")
        self.assertEqual(battles["a" * 32]["label"], "2026-10-06T10:00:00+00:00")  # legacy field kept
        participant = battles["a" * 32]["participants"][0]
        self.assertEqual((participant["name"], participant["author_name"], participant["author_kind"], participant["model"]),
                         ("Codex V2 mechanical example", "Codex", "model", None))
        self.assertNotIn("Mechanical V2 fixture", str(state))  # the author's prompt text is never projected
        by = {r["id"]: r for r in state["frequencies"]}
        self.assertEqual(set(by), {participant["id"], "cash"})
        mine = by[participant["id"]]
        self.assertEqual((mine["comparable_battles"], mine["sole_wins"], mine["above_cash"], mine["below_cash"]), (2, 1, 1, 1))
        self.assertEqual(Decimal(mine["net_sum_usdt"]), Decimal("0.01"))
        self.assertEqual((by["cash"]["sole_wins"], by["cash"]["net_sum_usdt"]), (1, "0"))
        self.assertEqual([(c["battles"], c["mode"]) for c in state["cohorts"]], [(2, "PAPER")])
        self.assertIn("описові", state["policy_note"])

    def test_e_a_battle_of_examples_only_is_ineligible_and_out_of_the_table(self):
        with tempfile.TemporaryDirectory() as directory:  # the real score: the fixture's recipes are a Codex example's
            state = paper_fixture()
            state.update(id="c" * 32, created_at="2026-10-08T10:00:00+00:00")
            arena = FakeController(Path(directory) / "paper", [state])
            exchange = FakeController(Path(directory) / "exchange", kind="runs")
            status = League(directory, arena, exchange).status()
        battle = status["battles"][0]
        self.assertEqual((battle["competitive_validity"], battle["score"]["reason"]), ("INELIGIBLE", "example_only"))
        self.assertEqual(status["frequencies"], [])


if __name__ == "__main__":
    unittest.main()
