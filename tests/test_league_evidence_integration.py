"""League scoring/provenance/upgrade lock regressions: temp roots, fake records."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parent
ROOT = PATH.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(PATH))
from test_replay_evidence_integration import exchange_fixture, paper_fixture
from test_management_integration import recipe
from trading_lab.arena import write
from trading_lab.arena_strategies import ArenaError, fingerprint
from trading_lab.battle_review import build_review, digest
from trading_lab.league import League, score
from trading_lab.strategy_contract import payload_of
from trading_lab.workshop import Workshop
from trading_lab.arena_engine import RULES
from trading_lab.exchange_engine import LIMITS
from trading_lab.match_protocol import SCORE_POLICY


def rehash_bundle(bundle):
    source = {k: v for k, v in bundle.items() if k != "integrity"}
    bundle["integrity"] = {"bundle_sha256": digest(source), "source_event_count": len(bundle["events"])}
    return bundle


def scored_bundle(nets, allocations=None):
    bundle = build_review(exchange_fixture())
    sample = bundle["participants"][0]
    bundle["participants"] = []
    for index, net in enumerate(nets):
        row = deepcopy(sample)
        row.update(id="fictional-" + str(index + 1), allocation_usdt=(allocations or ["100"] * len(nets))[index])
        row["costs"].update(gross_pnl_usdt=net, signed_fees_usdt="0", signed_funding_usdt="0", net_usdt=net,
            complete=net is not None)
        bundle["participants"].append(row)
    return rehash_bundle(bundle)


class FakeController:
    def __init__(self, path, states=(), kind="seasons"):
        self.path, self.lock = Path(path), threading.RLock()
        self.states = {s["id"]: deepcopy(s) for s in states}
        self.data = {kind: list(self.states)}
        for state in states: write(self.path / (state["id"] + ".json"), state)

    def load(self, identity, *, reader=False): return deepcopy(self.states[identity])
    def status(self): return {"strategies": []}


class LeagueEvidenceIntegrationTests(unittest.TestCase):
    def test_cash_wins_losses_and_no_trade_zero_is_shared_top(self):
        result = score(scored_bundle(["-.00000003", "-.1"]))
        self.assertEqual(result["sole_winner"], "cash")
        result = score(scored_bundle(["0", "-.1"]))
        self.assertEqual(result["status"], "TIE_AT_TOP")
        self.assertCountEqual(result["shared_top"], ["cash", "fictional-1"])

    def test_exact_decimal_tie_tolerance_and_one_distinct_winner(self):
        result = score(scored_bundle([".01", ".010000009"]))
        self.assertEqual(result["status"], "TIE_AT_TOP")
        self.assertNotIn("cash", result["shared_top"])
        result = score(scored_bundle([".01", ".010000011"]))
        self.assertEqual(result["sole_winner"], "fictional-2")

    def test_missing_results_open_inventory_or_incomplete_history_never_win(self):
        self.assertEqual(score(scored_bundle([None, "0"]))["status"], "INCOMPLETE")
        for modification in ("inventory", "quality", "history", "run"):
            with self.subTest(modification=modification):
                bundle = scored_bundle(["1"])
                if modification == "inventory": bundle["participants"][0]["position_btc"] = ".001"
                elif modification == "quality": bundle["participants"][0]["quality"]["data_status"] = "INCOMPLETE"
                elif modification == "history": bundle["history_complete"] = False
                else: bundle["match"]["incomplete"] = True
                result = score(rehash_bundle(bundle))
                self.assertIsNone(result["sole_winner"])
                self.assertFalse(result["shared_top"])

    def test_unequal_allocations_are_ineligible_and_equal_decimal_allocations_are_equal(self):
        self.assertEqual(score(scored_bundle(["1", "2"], ["100", "200"]))["status"], "INELIGIBLE")
        self.assertEqual(score(scored_bundle(["1", "2"], ["100", "100.0"]))["sole_winner"], "fictional-2")

    def test_source_evidence_cohorts_do_not_merge_forward_market_and_synthetic_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            forward = paper_fixture()
            forward.update(id="a" * 32, evidence_kind="MODEL")
            forward["match_manifest"].update(evaluation="forward_owner_match", source="recorded_public")
            fixture = deepcopy(forward)
            fixture.update(id="b" * 32, evidence_kind="SYNTHETIC", fixture_only=True)
            fixture["match_manifest"].update(evaluation="synthetic_training", source="synthetic")
            arena = FakeController(Path(directory) / "paper", [forward, fixture])
            exchange = FakeController(Path(directory) / "exchange", kind="runs")
            state = League(directory, arena, exchange).status()
            self.assertEqual(len(state["battles"]), 2)
            self.assertTrue(all("cohort" in row for row in state["battles"]))
            self.assertNotEqual(state["battles"][0]["cohort"], state["battles"][1]["cohort"])
            self.assertTrue(all(row["comparable_battles"] == 1 for row in state["frequencies"]))

    def test_finished_battle_is_reviewed_once_and_a_running_one_at_most_once_a_minute(self):
        # A poll must not re-read and re-review every journal: a 5-hour battle holds thousands of events.
        with tempfile.TemporaryDirectory() as directory:
            done = exchange_fixture()
            running = deepcopy(done)
            running.update(id="c" * 32, status="RUNNING")
            arena = FakeController(Path(directory) / "paper")
            exchange = FakeController(Path(directory) / "exchange", [done, running], kind="runs")
            exchange.sessions = {running["id"]: exchange.states[running["id"]]}
            loads, reviews, now = [], [], [100.]
            original = exchange.load
            exchange.load = lambda identity, **kwargs: loads.append(identity) or original(identity)
            def review(state, **kwargs):
                free = []
                def probe():
                    if exchange.lock.acquire(timeout=.5):
                        free.append(True)
                        exchange.lock.release()
                thread = threading.Thread(target=probe)
                thread.start()
                thread.join()
                self.assertTrue(free, "the review runs outside the engine's lock")
                reviews.append(state["id"])
                return build_review(state, **kwargs)
            league = League(directory, arena, exchange, monotonic=lambda: now[0])
            with patch("trading_lab.league.build_review", side_effect=review):
                first = league.status()
                write(exchange.path / (running["id"] + ".json"), {**running, "progress": 1})  # a running checkpoint moves on
                now[0] += 59
                second = league.status()
                self.assertEqual(sorted(loads), sorted([done["id"], running["id"]]))
                self.assertEqual(first["battles"], second["battles"])
                now[0] += 2
                league.status()
                self.assertEqual(loads.count(running["id"]), 2)
                self.assertEqual(loads.count(done["id"]), 1)
                exchange.states[running["id"]]["status"] = exchange.sessions[running["id"]]["status"] = "DONE"
                write(exchange.path / (running["id"] + ".json"), exchange.states[running["id"]])
                finished = league.status()  # finished within the minute: the final row at once, then cached
                self.assertEqual(loads.count(running["id"]), 3)
                self.assertEqual({b["id"]: b["status"] for b in finished["battles"]}, {done["id"]: "DONE", running["id"]: "DONE"})
                now[0] += 600
                league.status()
                self.assertEqual(len(loads), 4)
                self.assertEqual(reviews, loads)

    def make_lock(self, directory):
        arena = FakeController(Path(directory) / "paper")
        exchange = FakeController(Path(directory) / "exchange", kind="runs")
        workshop = Workshop(directory, arena)
        parent_id = workshop.add_version(0, payload_of(recipe()))["id"]
        revised = payload_of(recipe())
        revised["rules"]["entry"]["long"]["confirm"]["value"] += 1
        child_id = workshop.add_version(1, revised, parent_id)["id"]
        parent, child = workshop.version(parent_id), workshop.version(child_id)
        parent.update(status="frozen", promoted_id=parent_id)
        child.update(status="frozen", qualification_code_sha256="e" * 64,
            qualification_runtime="RULES_ONLY", qualification_trial_id="trial-one",
            created_at=datetime.fromtimestamp(.8, timezone.utc).isoformat(), trial_count=1, trial_ids=["trial-one"])
        workshop.data["trials"] = [{"id": "trial-one", "version_id": child_id, "status": "COMPLETE",
            "strategy_sha256": child["recipe_sha256"], "code_sha256": "e" * 64,
            "data_end_at_ms": 500, "runtime": "RULES_ONLY", "dataset_kind": "SYNTHETIC"}]
        write(workshop.path / "trials" / "trial-one.json", workshop.data["trials"][0])
        controller = League(directory, arena, exchange, clock=lambda: 1., workshop=workshop)
        window = {"feedback_released_at_ms": 700, "opened_at_ms": 800, "closed_at_ms": 1200,
            "training_cutoff_ms": 600, "next_lock_at_ms": 1500, "sdk_hash": "e" * 64,
            "feature_epoch_hash": hashlib.sha256((ROOT / "trading_lab" / "arena_market.py").read_bytes()).hexdigest(),
            "execution_policy_hash": fingerprint({"paper": RULES, "exchange": LIMITS}), "common_bundle_hash": fingerprint(["c" * 64])}
        controller.data["plans"] = [{"id": "plan-one", "participants": [parent_id],
            "recipe_hashes": {parent_id: parent["recipe_sha256"]}, "mode": "PAPER",
            "allocation_usdt": 100, "duration_seconds": 300, "score_policy": deepcopy(SCORE_POLICY)}]
        controller.data["feedback"] = [{"id": "feedback-one", "plan_id": "plan-one", "released_at_ms": 700,
            "window": window, "bundle_hashes": ["c" * 64]}]
        return controller, workshop, child, {"feedback_id": "feedback-one", "candidates": [{"version_id": child_id}]}

    def test_lock_reads_workshop_and_rejects_author_supplied_qualification_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            controller, _, _, body = self.make_lock(directory)
            body["candidates"][0].update(qualified=True, all_trials_logged=True, sdk_hash="e" * 64)
            with self.assertRaises(ArenaError): controller.action(0, "lock", body)
            self.assertFalse(controller.data["locks"])

    def test_current_qualification_can_lock_without_starting_any_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            controller, _, _, body = self.make_lock(directory)
            with patch("trading_lab.strategy_training.code_hash", return_value="e" * 64):
                result = controller.action(0, "lock", body)
            self.assertFalse(result["execution_started"])
            self.assertFalse(controller.data["locks"][0]["automatic_start"])
            self.assertEqual(controller.data["locks"][0]["verified_candidates"][0]["submitted_at_ms"], 1000)

    def test_arbitrary_future_synthetic_clock_does_not_block_a_calendar_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            controller, workshop, _, body = self.make_lock(directory)
            workshop.data["trials"][0]["data_end_at_ms"] = 1800000900000
            with patch("trading_lab.strategy_training.code_hash", return_value="e" * 64):
                controller.action(0, "lock", body)
            candidate = controller.data["locks"][0]["verified_candidates"][0]
            self.assertEqual(candidate["training_latest_at_ms"], 0)
            self.assertEqual(candidate["synthetic_clock_exempt_trials"], 1)

    def test_calendar_cutoff_still_rejects_future_recorded_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            controller, workshop, _, body = self.make_lock(directory)
            workshop.data["trials"][0].update(dataset_kind="RECORDED_PUBLIC", data_end_at_ms=601)
            with patch("trading_lab.strategy_training.code_hash", return_value="e" * 64):
                with self.assertRaises(ArenaError): controller.action(0, "lock", body)
            self.assertFalse(controller.data["locks"])

    def test_mixed_trials_use_recorded_market_time_not_synthetic_time(self):
        with tempfile.TemporaryDirectory() as directory:
            controller, workshop, child, body = self.make_lock(directory)
            synthetic = workshop.data["trials"][0]
            synthetic["data_end_at_ms"] = 1800000900000
            recorded = {**synthetic, "id": "trial-recorded", "dataset_kind": "RECORDED_PUBLIC", "data_end_at_ms": 500}
            workshop.data["trials"].append(recorded)
            child.update(trial_count=2, trial_ids=["trial-one", "trial-recorded"])
            with patch("trading_lab.strategy_training.code_hash", return_value="e" * 64):
                controller.action(0, "lock", body)
            self.assertEqual(controller.data["locks"][0]["verified_candidates"][0]["training_latest_at_ms"], 500)

    def test_unknown_failed_trial_is_not_hidden_by_synthetic_exemption(self):
        with tempfile.TemporaryDirectory() as directory:
            controller, workshop, child, body = self.make_lock(directory)
            workshop.data["trials"].append({**workshop.data["trials"][0], "id": "failed", "status": "FAILED", "data_end_at_ms": None})
            child.update(trial_count=2, trial_ids=["trial-one", "failed"])
            with patch("trading_lab.strategy_training.code_hash", return_value="e" * 64):
                with self.assertRaises(ArenaError): controller.action(0, "lock", body)
            self.assertFalse(controller.data["locks"])

    def test_lock_rejects_code_updated_since_feedback_even_if_old_window_hash_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            controller, _, _, body = self.make_lock(directory)
            with patch("trading_lab.strategy_training.code_hash", return_value="f" * 64):
                with self.assertRaises(ArenaError): controller.action(0, "lock", body)
            self.assertFalse(controller.data["locks"])

    def test_lock_rejects_policy_updated_since_feedback(self):
        with tempfile.TemporaryDirectory() as directory:
            controller, _, _, body = self.make_lock(directory)
            with patch("trading_lab.strategy_training.code_hash", return_value="e" * 64), patch.dict(RULES, fee_bps_per_side=6):
                with self.assertRaises(ArenaError): controller.action(0, "lock", body)

    def test_lock_rejects_qualification_not_backed_by_the_exact_complete_trial(self):
        for alteration in ("report_hash", "status", "trial_id"):
            with self.subTest(alteration=alteration), tempfile.TemporaryDirectory() as directory:
                controller, workshop, child, body = self.make_lock(directory)
                if alteration == "report_hash": workshop.data["trials"][0]["strategy_sha256"] = "a" * 64
                elif alteration == "status": workshop.data["trials"][0]["status"] = "FAILED"
                else: child["qualification_trial_id"] = "missing-trial"
                with patch("trading_lab.strategy_training.code_hash", return_value="e" * 64):
                    with self.assertRaises(ArenaError): controller.action(0, "lock", body)

    def test_lock_rejects_parent_lineage_unrelated_to_the_plan_locked_roster(self):
        with tempfile.TemporaryDirectory() as directory:
            controller, _, _, body = self.make_lock(directory)
            controller.data["plans"][0]["recipe_hashes"] = {"another-parent": "d" * 64}
            controller.data["plans"][0]["participants"] = ["another-parent"]
            with patch("trading_lab.strategy_training.code_hash", return_value="e" * 64):
                with self.assertRaises(ArenaError): controller.action(0, "lock", body)


if __name__ == "__main__": unittest.main()
