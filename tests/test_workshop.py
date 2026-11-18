"""Offline workshop contract and lifecycle tests; no provider, exchange, or runtime data."""
from copy import deepcopy
import http.client
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from trading_lab.arena import Arena
from trading_lab.arena_market import build_snapshot
from trading_lab.arena_strategies import ArenaError, catalogue_defaults
from trading_lab.strategy_training import SYNTHETIC, read_recorded, run_trial, synthetic_tape
from trading_lab.web_panel import Panel, Server
from trading_lab.workshop import Workshop

ROOT = Path(__file__).resolve().parents[1]


def raw_public(at):
    minute = int(at // 60) * 60 - 60
    candles = []
    for i in range(30):
        close = 84000 + i * 2
        candles.append([str((minute - (29 - i) * 60) * 1000, ), str(close), str(close + 1),
                        str(close - 1), str(close), "10", "0", "0", "1"])
    return {"clock": [{"ts": str(at * 1000)}], "candles": candles,
            "ticker": [{"instId": "BTC-USDT-SWAP", "bidPx": "84000", "askPx": "84001",
                        "bidSz": "100", "askSz": "100", "ts": str(at * 1000)}],
            "instrument": [{"instId": "BTC-USDT-SWAP", "state": "live", "ctValCcy": "BTC",
                            "settleCcy": "USDT", "ctVal": "0.01", "ctMult": "1",
                            "lotSz": "0.01", "minSz": "0.01"}],
            "funding": [{"instId": "BTC-USDT-SWAP", "fundingTime": str((at + 3600) * 1000),
                         "ts": str(at * 1000), "fundingRate": "0.0001"}]}


def recipe():
    return deepcopy(catalogue_defaults()[0])


def payload_of(strategy):
    return {key: deepcopy(strategy[key]) for key in
            ("name", "author", "description", "original_text", "rules", "confirmed")}


class TempWorkshop(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.feed_calls = []

        def no_feed():
            self.feed_calls.append(True)
            raise AssertionError("Workshop tests must not request market data")

        self.arena = Arena(self.root, feed_factory=no_feed, background=False)
        self.workshop = Workshop(self.root, self.arena)

    def tearDown(self):
        self.arena.close()
        self.temp.cleanup()

    def add(self, strategy=None, parent_id=None):
        return self.workshop.add_version(self.workshop.data["generation"],
                                         payload_of(strategy or recipe()), parent_id)


class TrainingTests(unittest.TestCase):
    def test_same_synthetic_inputs_have_same_metrics_and_hashes(self):
        strategy = recipe()
        first = run_trial(strategy)
        second = run_trial(strategy)
        self.assertEqual(first["summary"], second["summary"])
        self.assertEqual(first["strategy_sha256"], second["strategy_sha256"])
        self.assertEqual(first["data_sha256"], second["data_sha256"])
        self.assertEqual(first["code_sha256"], second["code_sha256"])
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(first["created_at"], "")

    def test_synthetic_features_are_built_only_from_closed_past_candles(self):
        from trading_lab import strategy_training

        original = strategy_training.candle_features
        observations = []

        def inspect(rows, now):
            observations.append((now, tuple(float(row[0]) / 1000 + 60 for row in rows)))
            return original(rows, now)

        with patch("trading_lab.strategy_training.candle_features", side_effect=inspect):
            tape = synthetic_tape("up")
        self.assertEqual(len(tape), len(observations))
        for now, close_times in observations:
            self.assertTrue(close_times)
            self.assertTrue(all(closed_at <= now for closed_at in close_times))
        self.assertTrue(all(s["raw"].get("synthetic") is True for s in tape))

    def test_report_keeps_synthetic_source_and_no_trade_is_complete_training(self):
        strategy = recipe()
        strategy["rules"]["entry"]["conditions"] = [
            {"feature": "return_1m_bps", "op": "gt", "value": 1e9}]
        strategy["recipe_sha256"] = "local-test-recipe-hash"
        report = run_trial(strategy)
        self.assertEqual(report["dataset_kind"], "SYNTHETIC")
        self.assertIn("СИНТЕТИЧНИЙ", report["feedback"][0])
        self.assertEqual(report["runtime"], "RULES_ONLY")
        self.assertEqual(report["ai_cost_usd"], 0)
        self.assertEqual(report["status"], "COMPLETE")
        self.assertEqual(report["summary"]["trades"], 0)
        self.assertTrue(all(run["status"] == "COMPLETE" for run in report["runs"]))

    def test_incomplete_short_tape_is_marked_incomplete(self):
        report = run_trial(recipe(), SYNTHETIC, 100,
                           [("short fixture", synthetic_tape("up")[:8], 900)])
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertTrue(report["summary"]["complete_runs"] < report["summary"]["total_runs"])
        self.assertTrue(any("неповні" in item.lower() for item in report["feedback"]))

    def test_recording_rebuilds_derived_values_and_rejects_bad_hash(self):
        base = 1800000030
        rows = []
        for offset in range(0, 101, 5):
            at = base + offset
            raw = raw_public(at)
            snapshot = build_snapshot(raw, at)
            rows.append({"at": at, "raw": raw, "sha256": snapshot["sha256"],
                         "features": {"return_1m_bps": 999999}, "bid": -1})
        path = self._write_jsonl(rows)
        tape = read_recorded(path)
        self.assertEqual(len(tape), len(rows))
        expected = build_snapshot(rows[0]["raw"], rows[0]["at"])
        self.assertEqual(tape[0]["features"], expected["features"])
        self.assertNotEqual(tape[0]["features"]["return_1m_bps"], 999999)
        rows[0]["sha256"] = "0" * 64
        bad = self._write_jsonl(rows)
        with self.assertRaises(ArenaError):
            read_recorded(bad)

    def _write_jsonl(self, rows):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        path = folder / "recording.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    def test_synthetic_raw_cannot_be_relabelled_recorded_public(self):
        # A synthetic marker must not become trusted market history merely because
        # a fixture also carries plausible endpoint-shaped JSON and a self-hash.
        at = 1800000030
        raw = raw_public(at)
        raw["synthetic"] = True
        snapshot = build_snapshot(raw, at)
        rows = [{"at": at + offset, "raw": raw,
                 "sha256": snapshot["sha256"]} for offset in range(0, 101, 5)]
        with self.assertRaises(ArenaError):
            read_recorded(self._write_jsonl(rows))

    def test_malformed_recording_shapes_are_validation_errors(self):
        at = 1800000030
        raw = raw_public(at)
        raw["clock"] = []
        for row in ([], {"at": at, "raw": raw, "sha256": "0" * 64}):
            with self.subTest(row=row), self.assertRaises(ArenaError):
                read_recorded(self._write_jsonl([row]))

class WorkshopLifecycleTests(TempWorkshop):
    def test_dataset_discovery_does_not_list_synthetic_raw_as_recorded_public(self):
        import uuid

        identity = uuid.uuid4().hex
        at = 1800000030
        raw = raw_public(at)
        raw["synthetic"] = True
        snap = build_snapshot(raw, at)
        self.arena.data["seasons"].append(identity)
        self.arena.path.mkdir(parents=True, exist_ok=True)
        (self.arena.path / f"{identity}.json").write_text(json.dumps({
            "id": identity, "mode": "PAPER", "status": "DONE", "created_at": "fixture"
        }), encoding="utf-8")
        (self.arena.path / f"{identity}.snapshots.jsonl").write_text(json.dumps({
            "at": at, "raw": raw, "sha256": snap["sha256"]
        }) + "\n", encoding="utf-8")
        self.assertNotIn("recorded-" + identity,
                         [item["id"] for item in self.workshop.datasets()])

    def test_no_trade_complete_trial_can_freeze(self):
        no_trade = recipe()
        no_trade["rules"]["entry"]["conditions"] = [
            {"feature": "return_1m_bps", "op": "gt", "value": 1e9}]
        saved = self.add(no_trade)
        trained = self.workshop.train(self.workshop.data["generation"], saved["id"],
                                      SYNTHETIC["id"], 100)
        detail = self.workshop.trial(trained["id"])
        self.assertEqual(detail["status"], "COMPLETE")
        self.assertEqual(detail["summary"]["trades"], 0)
        frozen = self.workshop.freeze(self.workshop.data["generation"], saved["id"])
        self.assertEqual(frozen["status"], "frozen")

    def test_negative_complete_trial_can_freeze(self):
        losing = recipe()
        losing["rules"]["entry"]["conditions"] = [
            {"feature": "return_1m_bps", "op": "gt", "value": -999}]
        saved = self.add(losing)
        trained = self.workshop.train(self.workshop.data["generation"], saved["id"],
                                      SYNTHETIC["id"], 100)
        detail = self.workshop.trial(trained["id"])
        self.assertEqual(detail["status"], "COMPLETE")
        self.assertLess(detail["summary"]["pnl_usdt"], 0)
        frozen = self.workshop.freeze(self.workshop.data["generation"], saved["id"])
        self.assertEqual(frozen["status"], "frozen")

    def test_incomplete_run_cannot_freeze(self):
        saved = self.add()
        report = run_trial(recipe(), SYNTHETIC, 100,
                           [("short fixture", synthetic_tape("up")[:8], 900)])
        report["version_id"] = saved["id"]
        report["strategy_sha256"] = self.workshop.version(saved["id"])["recipe_sha256"]
        self.workshop.data["trials"].append({k: deepcopy(v) for k, v in report.items()
                                              if k not in {"runs", "feedback"}})
        with self.assertRaises(ArenaError):
            self.workshop.freeze(self.workshop.data["generation"], saved["id"])

    def test_freeze_requires_current_code_hash(self):
        saved = self.add()
        trained = self.workshop.train(self.workshop.data["generation"], saved["id"],
                                      SYNTHETIC["id"], 100)
        summary = next(t for t in self.workshop.data["trials"] if t["id"] == trained["id"])
        with patch("trading_lab.workshop.code_hash", return_value="different-current-code"):
            with self.assertRaises(ArenaError):
                self.workshop.freeze(self.workshop.data["generation"], saved["id"])

    def test_revisions_preserve_old_frozen_recipe_and_promotion_is_exact_idempotent(self):
        saved = self.add()
        trained = self.workshop.train(self.workshop.data["generation"], saved["id"],
                                      SYNTHETIC["id"], 100)
        before = self.workshop.version(saved["id"])
        expected_hash = before["recipe_sha256"]
        frozen = self.workshop.freeze(self.workshop.data["generation"], saved["id"])
        self.assertEqual(frozen["status"], "frozen")
        changed = payload_of(before["strategy"])
        changed["rules"]["size_pct"] = 25
        child = self.add(validate_strategy_for_test(changed), saved["id"])
        old_after = self.workshop.version(saved["id"])
        self.assertEqual(old_after["recipe_sha256"], expected_hash)
        self.assertEqual(old_after["strategy"]["rules"]["size_pct"], 20)
        self.assertEqual(self.workshop.version(child["id"])["parent_id"], saved["id"])

        promoted = self.workshop.promote(self.workshop.data["generation"], saved["id"])
        imported = next(s for s in self.arena.catalogue() if s["id"] == saved["id"])
        self.assertEqual(promoted["id"], saved["id"])
        self.assertEqual(imported["recipe_sha256"], expected_hash)
        generation = self.workshop.data["generation"]
        promoted_again = self.workshop.promote(generation, saved["id"])
        self.assertEqual(promoted_again["id"], saved["id"])
        self.assertTrue(promoted_again["already_registered"])
        self.assertEqual(sum(s["id"] == saved["id"] for s in self.arena.catalogue()), 1)
        self.assertIsNone(self.arena.session)
        self.assertEqual(self.arena.data["seasons"], [])
        self.assertEqual(self.feed_calls, [])

    def test_generation_and_path_traversal_are_rejected(self):
        with self.assertRaises(ArenaError):
            self.workshop.add_version(1, payload_of(recipe()))
        for bad in ("../manifest.json", "a" * 32, ""):
            with self.assertRaises(ArenaError):
                self.workshop.version(bad)
            with self.assertRaises(ArenaError):
                self.workshop.trial(bad)


def validate_strategy_for_test(payload):
    from trading_lab.arena_strategies import validate_strategy
    return validate_strategy(payload)


class WorkshopHttpTests(TempWorkshop):
    @unittest.skipUnless(shutil.which("node"), "node runs web/workshop.js")
    def test_h10_imported_through_the_page_keeps_v4_dcf48666(self):
        """R2.1.4, the owner's TEST-LIVE path: H10_ETH_recipe.json through web/workshop.js (#recipe-file, #confirm-match,
        #save-version), the unchanged POST guard and add_version give v4 dcf486662716f1b7..., as the file does. Before,
        the page's JSON.stringify wrote mult 3.0 as 3 and the version became another recipe (v4 d70e7c21...)."""
        recipe = ROOT / "team/authors/sonnet/candidates/H10_ETH/recipe.json"  # = the kits' H10_ETH_recipe.json (504be5db...)
        panel = Panel(self.root, launcher=lambda *args, **kwargs: self.fail("the workshop never launches"))
        with Server(panel, 0, arena=self.arena) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def call(method, path, body=None, headers=None):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=20)
                conn.request(method, path, body, headers or {})
                response = conn.getresponse()
                result = response.status, response.read()
                conn.close()
                return result
            try:
                generation = json.loads(call("GET", "/api/workshop/state")[1])["generation"]
                page = subprocess.run(["node", str(ROOT / "tests/v8_ui/workshop-page-fixture.js"), str(recipe), panel.token,
                                       str(generation)], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                                      timeout=60, check=True)
                [post] = json.loads(page.stdout)["posts"]
                self.assertEqual(post["path"], "/api/workshop/versions")
                headers = {**post["headers"], "Origin": server.origin}  # a browser adds Origin itself
                body = post["body"].encode("utf-8")
                self.assertLessEqual(len(body), 131072)
                self.assertEqual(call("POST", post["path"], body, {**headers, "X-Panel-Token": "wrong"})[0], 403)
                self.assertEqual(call("POST", post["path"], body, {**headers, "Origin": "https://untrusted.invalid"})[0], 403)
                code, reply = call("POST", post["path"], body, headers)
                self.assertEqual(code, 202, reply)
                code, detail = call("GET", "/api/workshop/versions/" + json.loads(reply)["id"])
                strategy = json.loads(detail)["strategy"]
                self.assertEqual(strategy["v4_recipe_sha256"][:16], "dcf486662716f1b7")
                self.assertEqual(strategy["recipe_sha256"][:8], "d744a6f5")
                self.assertEqual(strategy["semantic_sha256"][:8], "aec2c346")
                self.assertIs(type(strategy["v4_payload"]["rules"]["entry"]["when"]["all"][0]["mult"]), float)
            finally:
                server.shutdown()
                thread.join(timeout=3)

    def test_contract_templates_survive_the_browsers_json_round_trip(self):
        """The page shows (loadTemplate) and posts (addDemoProbeRecipe) templates it parsed with JSON.parse: a whole-number
        float (3.0 -> 3) or an integer beyond 2**53 would change their hashes there, so the contract carries none."""
        from trading_lab.strategy_contract import author_contract

        def lossy(value, at="contract"):
            if isinstance(value, dict):
                return [x for key, item in value.items() for x in lossy(item, at + "/" + str(key))]
            if isinstance(value, list):
                return [x for index, item in enumerate(value) for x in lossy(item, f"{at}/{index}")]
            if isinstance(value, float) and value.is_integer():
                return [at]
            if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 2 ** 53:
                return [at]
            return []
        self.assertEqual(lossy(author_contract()), [])

    def test_http_csrf_exact_body_and_workshop_never_starts_market_or_live(self):
        launcher_calls = []
        panel = Panel(self.root, launcher=lambda *args, **kwargs: launcher_calls.append(args))
        with Server(panel, 0, arena=self.arena) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def call(method, path, body=None, headers=None):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=20)
                conn.request(method, path, json.dumps(body) if body is not None else None, headers or {})
                response = conn.getresponse()
                data = json.loads(response.read())
                code = response.status
                conn.close()
                return code, data

            valid = {"Origin": server.origin, "X-Panel-Token": panel.token,
                     "Content-Type": "application/json"}
            wrong_origin = {**valid, "Origin": "https://untrusted.invalid"}
            strategy = payload_of(recipe())
            try:
                status, state = call("GET", "/api/workshop/state")
                self.assertEqual(status, 200)
                self.assertEqual(state["datasets"][0]["kind"], "SYNTHETIC")
                self.assertEqual(call("POST", "/api/workshop/versions", {
                    "generation": 0, "strategy": strategy, "parent_id": None}, wrong_origin)[0], 403)
                self.assertEqual(call("POST", "/api/workshop/versions", {
                    "generation": 0, "strategy": strategy, "parent_id": None, "extra": True}, valid)[0], 409)
                code, saved = call("POST", "/api/workshop/versions", {
                    "generation": 0, "strategy": strategy, "parent_id": None}, valid)
                self.assertEqual(code, 202)
                code, trained = call("POST", "/api/workshop/train", {
                    "generation": 1, "version_id": saved["id"], "dataset_id": SYNTHETIC["id"],
                    "budget_usdt": 100}, valid)
                self.assertEqual(code, 202)
                self.assertEqual(trained["status"], "COMPLETE")
                _, state = call("GET", "/api/workshop/state")
                code, frozen = call("POST", "/api/workshop/freeze", {
                    "generation": state["generation"], "version_id": saved["id"]}, valid)
                self.assertEqual(code, 202)
                self.assertEqual(frozen["status"], "frozen")
                _, state = call("GET", "/api/workshop/state")
                code, promoted = call("POST", "/api/workshop/promote", {
                    "generation": state["generation"], "version_id": saved["id"]}, valid)
                self.assertEqual(code, 202)
                self.assertEqual(promoted["id"], saved["id"])
                self.assertEqual(self.arena.data["seasons"], [])
                self.assertIsNone(self.arena.session)
                self.assertEqual(self.feed_calls, [])
                self.assertEqual(launcher_calls, [])
            finally:
                server.shutdown()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
