"""CURRENT shared integration proof; fake/temp only, no API or paid requests."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

REPOSITORY = next(p for p in Path(__file__).resolve().parents if (p / "trading_lab" / "arena_market.py").is_file())
sys.path.insert(0, str(REPOSITORY))
from trading_lab.arena_market import build_snapshot
from trading_lab.public_recorder import PublicRecorder, public_projection, fixture_raw
from trading_lab.strategy_training import read_recorded
from trading_lab.arena_jev import SharedJevGate, ArenaJevLedger, BUDGET
from trading_lab.exchange_lab import POLICIES, LEDGERS
from trading_lab.ops_service import PublicRecorderRepository, BackupRepository, FixturePublicSequence
from trading_lab.ops_storage import StorageBlocked
from trading_lab.owner_shadow import OwnerShadowEpisodeRunner
from tools.arena_ops import run_cli, spawn_recorder_child
from trading_lab.shadow_jev import build_pack, consume


class NetworkBlockedTest(unittest.TestCase):
    def setUp(self):
        self.network_guard = patch.object(socket, "create_connection", side_effect=AssertionError("actual_network_forbidden"))
        self.network_guard.start()

    def tearDown(self):
        self.network_guard.stop()


def market_context(count=20):
    bars = [[60000000 + i * 60000, 100 + i, 102 + i, 99 + i, 101 + i, 1000 + i] for i in range(count)]
    return {"source": "FIXTURE", "instrument": "BTC-USDT-SWAP", "snapshot_sha256": "a" * 64,
            "feature_version": "market-features-v1", "observed_server_ms": bars[-1][0] + 60000,
            "bars": bars, "features": {"spread_bps": 1.2, "return_5m_bps": 50, "rsi_14": 60, "volume_ratio_5": None}}


def setups(count=6):
    return [{"slot": "s" + str(i), "family": "trend_pullback", "side": "long" if i % 2 == 0 else "short",
             "horizon_seconds": None if i == 0 else 900} for i in range(count)]


def answer_response(pack):
    answers = {}
    for name, question in pack.payload["questions"].items():
        kind, criteria = question["type"], question["criteria"]
        if kind == "choice":
            choice = next(iter(criteria))
            answers[name] = {"type": kind, "choice": choice, "probabilities": {k: int(k == choice) for k in criteria},
                             "confidence": .9}
        elif kind == "score":
            answers[name] = {"type": kind, "score": 2, "legend": {str(i): v for i, v in enumerate(criteria)},
                             "probabilities": {str(i): int(i == 2) for i in range(len(criteria))}, "confidence": .9}
        else:
            answers[name] = {"type": "noul", "noul": .6}
    return {"model": "typesafe/jev-1.13", "answers": answers,
            "usage": {"cost": .00001, "input_tokens": 1000, "output_tokens": 40}}


class PublicServiceFixtures(NetworkBlockedTest):
    def repository(self, root, *, factory=FixturePublicSequence, **kwargs):
        return PublicRecorderRepository(root, build_snapshot_fn=build_snapshot, get_factory=factory,
                                        source="FIXTURE", recorder_options={"epoch_ticks": 10}, **kwargs)

    def test_background_fixture_service_has_durable_status_and_rebuilds_training_tape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = self.repository(root / "public")
            started = service.start_recording(interval_seconds=0, max_observations=22)
            status = service.wait_recording(15)
            self.assertFalse(status["local_worker_alive"])
            self.assertEqual(status["status"], "STOPPED_RECORDING")
            self.assertEqual(status["samples"], 22)
            self.assertFalse(status["paid_calls"])
            self.assertFalse(status["affects_trading"])
            self.assertFalse(status["lease_exists"])
            reloaded = self.repository(root / "public", factory=lambda: self.fail("GET on status forbidden"))
            self.assertEqual(reloaded.status()["samples"], 22)
            self.assertEqual(reloaded.list_datasets()["total"], 1)
            tape_path = root / "exports" / "tape.jsonl"
            receipt = reloaded.export_training_tape(started["dataset_id"], tape_path)
            self.assertEqual(receipt["samples"], 22)
            self.assertTrue(receipt["fixture_only"])
            self.assertFalse(receipt["derived_features_trusted"])
            tape = read_recorded(tape_path)
            self.assertEqual(len(tape), 22)
            originals = [row["snapshot"] for row in reloaded.iter_records(started["dataset_id"]) if row["kind"] == "public_market"]
            self.assertEqual([s["sha256"] for s in tape], [s["sha256"] for s in originals])
            self.assertEqual(tape[-1]["features"], originals[-1]["features"])

    def test_stop_from_second_service_instance_stops_recording_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            entered, released = threading.Event(), threading.Event()

            class SlowFake(FixturePublicSequence):
                def __call__(self, name):
                    if name == "ticker":
                        entered.set()
                        if not released.wait(5):
                            raise TimeoutError("fixture_wait")
                    return super().__call__(name)

            service = self.repository(temporary, factory=SlowFake)
            service.start_recording(interval_seconds=0, max_observations=100)
            self.assertTrue(entered.wait(5))
            observer = self.repository(temporary, factory=lambda: self.fail("observer must not fetch"))
            requested = observer.request_stop_recording()
            self.assertEqual(requested["status"], "STOP_RECORDING_REQUESTED")
            self.assertFalse(requested["affects_trading"])
            released.set()
            result = service.wait_recording(10)
            self.assertEqual(result["reason"], "owner_stopped_recording_only")
            self.assertEqual(result["samples"], 1)

    def test_repository_capacity_is_separate_from_any_match_or_position(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = self.repository(temporary, max_sessions=1)
            service.start_recording(interval_seconds=0, max_observations=1)
            service.wait_recording(10)
            with self.assertRaises(StorageBlocked):
                service.start_recording(interval_seconds=0, max_observations=1)
            self.assertFalse(service.capacity()["force_position_close"])
            self.assertFalse(service.status()["affects_trading"])

    def test_tampered_chain_and_too_short_tape_are_not_published(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = self.repository(root / "public")
            short = service.start_recording(interval_seconds=0, max_observations=3)
            service.wait_recording(10)
            with self.assertRaisesRegex(ValueError, "insufficient_recorded"):
                service.export_training_tape(short["dataset_id"], root / "short.jsonl")
            self.assertFalse((root / "short.jsonl").exists())
            long = service.start_recording(interval_seconds=0, max_observations=21)
            service.wait_recording(10)
            segment = next((root / "public" / "sessions" / long["dataset_id"]).rglob("segment-*.jsonl"))
            content = segment.read_bytes()
            segment.write_bytes(content.replace(b'"at":61800.0', b'"at":61801.0', 1))
            self.assertNotEqual(segment.read_bytes(), content)
            with self.assertRaisesRegex(ValueError, "public_segment_chain"):
                service.export_training_tape(long["dataset_id"], root / "tampered.jsonl")
            self.assertFalse((root / "tampered.jsonl").exists())

    def test_read_only_cli_does_not_construct_public_transport(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = self.repository(temporary)
            with patch("tools.arena_ops.OwnerPublicGet", side_effect=AssertionError("no GET factory on status")):
                self.assertEqual(run_cli(["status", "--service-root", temporary])["status"], "IDLE")
                self.assertEqual(run_cli(["datasets", "--service-root", temporary])["total"], 0)
                self.assertEqual(run_cli(["stop-recording", "--service-root", temporary])["status"], "NO_ACTIVE_RECORDING")

    def test_tool_fixture_record_and_tape_export_from_unrelated_cwd(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = REPOSITORY / "tools" / "arena_ops.py"
            recorded = subprocess.run([sys.executable, "-B", str(script), "record", "--fixture", "--service-root", str(root / "public"),
                                       "--max-observations", "20", "--interval", "0"], cwd=temporary,
                                      capture_output=True, text=True, timeout=15)
            self.assertEqual(recorded.returncode, 0, recorded.stderr)
            state = json.loads(recorded.stdout)
            self.assertEqual(state["samples"], 20)
            exported = run_cli(["export-tape", "--service-root", str(root / "public"),
                                "--dataset", state["dataset_id"], "--output", str(root / "tape.jsonl")])
            self.assertTrue(exported["fixture_only"])
            self.assertEqual(len(read_recorded(root / "tape.jsonl")), 20)

    def test_background_spawn_is_exact_hidden_child_without_shell(self):
        class FakeProcess:
            pid = 123456

        with patch("tools.arena_ops.subprocess.Popen", return_value=FakeProcess()) as popen:
            result = spawn_recorder_child(["record", "--fixture", "--service-root", "C:/fake/temp", "--background"])
            args, kwargs = popen.call_args
            self.assertNotIn("--background", args[0])
            self.assertFalse(kwargs["shell"])
            if sys.platform == "win32":
                self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result["status"], "PUBLIC_RECORDER_DISPATCHED")
            self.assertFalse(result["affects_trading"])


class BackupServiceFixtures(NetworkBlockedTest):
    def initialize(self, temporary):
        root = Path(temporary)
        runtime = root / "runtime"
        runtime.mkdir()
        (runtime / "state.json").write_text('{"owned_contracts":"3"}', encoding="utf-8")
        ledger = runtime / "paid_fixture.sqlite3"
        with closing(sqlite3.connect(ledger)) as db:
            with db:
                db.execute("CREATE TABLE requests(id INTEGER PRIMARY KEY, cost REAL)")
                db.execute("INSERT INTO requests VALUES(1,.00001)")
        service = BackupRepository(runtime, runtime / "backups", root / "isolated")
        return root, runtime, ledger, service

    def test_online_backup_verify_and_restore_never_overwrite_newer_current_ledger(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, runtime, ledger, service = self.initialize(temporary)
            service.create({"state.json": "state.json", "paid_fixture.sqlite3": "paid_fixture.sqlite3"}, backup_id="first")
            self.assertTrue(service.verify("first")["verified"])
            with closing(sqlite3.connect(ledger)) as db:
                with db:
                    db.execute("INSERT INTO requests VALUES(2,.00002)")
            recovery = service.restore_isolated("first", restore_id="inspection")
            self.assertFalse(recovery["automatic_execution"])
            self.assertFalse(recovery["overwrite_current_billing_ledger"])
            self.assertFalse(recovery["overwrite_current_runtime"])
            with closing(sqlite3.connect(ledger)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 2)
            with closing(sqlite3.connect(root / "isolated" / "inspection" / "paid_fixture.sqlite3")) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 1)
            with self.assertRaisesRegex(ValueError, "new_isolated"):
                service.restore_isolated("first", restore_id="inspection")

    def test_runtime_restore_and_path_escape_or_credential_manifest_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, runtime, _, service = self.initialize(temporary)
            with self.assertRaisesRegex(ValueError, "isolation_roots"):
                BackupRepository(runtime, runtime / "backups", runtime / "restore")
            with self.assertRaisesRegex(ValueError, "backup_source_path"):
                service.create({"outside.json": "../outside.json"})
            (runtime / ".env.local").write_text("FAKE_KEY=never copy", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "backup_source_path"):
                service.create({"credentials.json": ".env.local"})
            (runtime / "secret.json").write_text('{"FAKE_SECRET":"ignored"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "backup_source_path"):
                service.create({"ordinary.json": "secret.json"})
            with self.assertRaisesRegex(ValueError, "backup_file_manifest"):
                service.create({"receipt.json": "state.json"})

    def test_backup_tampering_blocks_restore_before_creating_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, runtime, _, service = self.initialize(temporary)
            service.create({"state.json": "state.json"}, backup_id="first")
            (runtime / "backups" / "first" / "state.json").write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "backup_hash"):
                service.restore_isolated("first", restore_id="inspection")
            self.assertFalse((root / "isolated" / "inspection").exists())

    def test_backup_cli_create_verify_restore_isolated_with_fake_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, runtime, _, _ = self.initialize(temporary)
            manifest = root / "files.json"
            manifest.write_text('{"state.json":"state.json"}', encoding="utf-8")
            common = ["--current-runtime", str(runtime), "--backup-root", str(runtime / "backups"),
                      "--restore-root", str(root / "isolated"), "--backup-id", "fixture"]
            self.assertEqual(run_cli(["backup-create", *common, "--files-json", str(manifest)])["backup_id"], "fixture")
            self.assertTrue(run_cli(["backup-verify", *common])["verified"])
            result = run_cli(["backup-restore-isolated", *common, "--restore-id", "inspection"])
            self.assertEqual(result["status"], "ISOLATED_REVIEW_ONLY")
            self.assertFalse(result["execution_permitted"])

    def test_restore_count_capacity_never_replaces_existing_recovery_or_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, runtime, ledger, _ = self.initialize(temporary)
            service = BackupRepository(runtime, runtime / "backups", root / "isolated", max_restores=1)
            service.create({"state.json": "state.json"}, backup_id="first")
            service.restore_isolated("first", restore_id="inspection")
            with self.assertRaises(StorageBlocked):
                service.restore_isolated("first", restore_id="second")
            self.assertFalse((root / "isolated" / "second").exists())
            self.assertTrue((runtime / "state.json").exists())


class OwnerShadowFixtures(NetworkBlockedTest):
    def gate(self, root, *, transport=None, timer=lambda: 0):
        pack = build_pack(market_context(), setups(6))

        class FakeTypedTransport:
            is_fake = True
            calls = 0

            def __call__(self, body, key):
                self.calls += 1
                self.body = body
                return answer_response(pack)

        transport = transport or FakeTypedTransport()
        return SharedJevGate(root, transport=transport, key_reader=lambda _: "fixture-key",
                             clock=lambda: 1000, timer=timer), transport

    def test_one_episode_many_typed_questions_uses_existing_ledger_and_never_gate_decide(self):
        with tempfile.TemporaryDirectory() as temporary:
            gate, fake = self.gate(temporary)
            with patch.object(gate, "decide", side_effect=AssertionError("execution gate must never be called")):
                runner = OwnerShadowEpisodeRunner(gate, fixture_only=True)
                result = runner.evaluate_episode("season", "episode1", market_context(), setups(6))
            self.assertEqual(fake.calls, 1)
            self.assertEqual(result["question_count"], 9)
            self.assertEqual(len(result["labels"]), 9)
            self.assertEqual(result["allowed"], [])
            self.assertEqual(result["execution_effect"], "NONE")
            self.assertEqual(gate.ledger.summary("season")["attempts"], 1)
            self.assertEqual(gate.ledger.summary("season")["limits"], BUDGET)
            payload = json.loads(fake.body)
            self.assertEqual(payload["state"]["mode"], "SHADOW")
            self.assertNotIn("position_btc", fake.body.decode())
            self.assertNotIn("author", fake.body.decode())

    def test_restart_same_episode_is_not_sent_again(self):
        with tempfile.TemporaryDirectory() as temporary:
            gate, fake = self.gate(temporary)
            OwnerShadowEpisodeRunner(gate, fixture_only=True).evaluate_episode("season", "episode1", market_context(), setups(6))
            second_gate, second_fake = self.gate(temporary)
            second = OwnerShadowEpisodeRunner(second_gate, fixture_only=True)
            result = second.evaluate_episode("season", "episode1", market_context(), setups(6))
            self.assertEqual(result["reason"], "episode_already_claimed_no_retry")
            self.assertEqual(second_fake.calls, 0)
            self.assertEqual(second.episode_status("season", "episode1")["status"], "attempted")
            self.assertEqual(second_gate.ledger.summary("season")["attempts"], 1)

    def test_existing_season_cap_is_preserved_without_ledger_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            gate, fake = self.gate(temporary)
            with gate.ledger.connect() as db:
                for i in range(BUDGET["season_attempts"]):
                    db.execute("INSERT INTO requests VALUES(?,?,?,?,?,?)", (str(i), "season", 900, "old", 0, "accounted"))
            before = gate.ledger.summary("season")
            result = OwnerShadowEpisodeRunner(gate, fixture_only=True).evaluate_episode("season", "new", market_context(), setups(6))
            self.assertEqual(result["reason"], "budget_or_unknown_billing")
            self.assertEqual(fake.calls, 0)
            self.assertEqual(gate.ledger.summary("season"), before)

    def test_unknown_transport_billing_blocks_all_later_episode_calls(self):
        class Failure:
            is_fake = True
            calls = 0

            def __call__(self, body, key):
                self.calls += 1
                raise TimeoutError("FAKE_PRIVATE_EXCEPTION_TEXT")

        with tempfile.TemporaryDirectory() as temporary:
            gate, fake = self.gate(temporary, transport=Failure())
            runner = OwnerShadowEpisodeRunner(gate, fixture_only=True)
            first = runner.evaluate_episode("season", "one", market_context(), setups(6))
            second = runner.evaluate_episode("season", "two", market_context(), setups(6))
            self.assertEqual(first["reason"], "billing_unknown")
            self.assertIsNone(first["reported_cost_usd"])
            self.assertEqual(second["reason"], "budget_or_unknown_billing")
            self.assertEqual(fake.calls, 1)
            self.assertEqual(gate.ledger.summary("season")["series_unknown_billing"], 1)
            self.assertNotIn("FAKE_PRIVATE", json.dumps(runner.episode_status("season", "one")))

    def test_invalid_expired_answers_still_account_known_cost_without_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            values = iter([0, 9])
            gate, fake = self.gate(temporary, timer=lambda: next(values))
            result = OwnerShadowEpisodeRunner(gate, fixture_only=True).evaluate_episode("season", "one", market_context(), setups(6))
            self.assertEqual(result["reason"], "answer_expired")
            self.assertEqual(result["labels"], {})
            self.assertEqual(result["reported_cost_usd"], .00001)
            self.assertEqual(gate.ledger.summary("season")["series_unknown_billing"], 0)
            self.assertEqual(fake.calls, 1)

    def test_owner_runner_requires_explicit_enabled_and_fixture_transport(self):
        with tempfile.TemporaryDirectory() as temporary:
            gate, fake = self.gate(temporary)
            disabled = OwnerShadowEpisodeRunner(gate)
            self.assertEqual(disabled.evaluate_episode("season", "one", market_context(), setups(6))["reason"], "owner_shadow_not_enabled")
            self.assertEqual(fake.calls, 0)
            gate.transport = lambda *args: self.fail("actual transport forbidden")
            with self.assertRaisesRegex(ValueError, "fixture_shadow_transport_required"):
                OwnerShadowEpisodeRunner(gate, fixture_only=True)

    def test_oversized_payload_is_rejected_before_claim_or_paid_reservation(self):
        with tempfile.TemporaryDirectory() as temporary:
            gate, fake = self.gate(temporary)
            context = market_context(60)
            for row in context["bars"]:
                row[1:5] = [10 ** 100, 2 * 10 ** 100, 10 ** 100, 10 ** 100]
            runner = OwnerShadowEpisodeRunner(gate, fixture_only=True)
            result = runner.evaluate_episode("season", "one", context, setups(6))
            self.assertEqual(result["reason"], "invalid_market_shadow_payload_no_split")
            self.assertEqual(fake.calls, 0)
            self.assertEqual(gate.ledger.summary("season")["series_attempts"], 0)
            self.assertEqual(runner.episode_status("season", "one")["status"], "NOT_CLAIMED")

    def test_fixture_shadow_cli_uses_only_temp_existing_ledger_and_dedupes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packet = root / "public-packet.json"
            packet.write_text(json.dumps({"context": market_context(), "setups": setups(6)}), encoding="utf-8")
            args = ["shadow-episode", "--fixture", "--root", temporary, "--season", "fixture", "--episode", "one",
                    "--input-json", str(packet)]
            first = run_cli(args)
            second = run_cli(args)
            self.assertEqual(first["reason"], "typed_shadow_only")
            self.assertEqual(first["usage"]["attempts"], 1)
            self.assertEqual(second["reason"], "episode_already_claimed_no_retry")
            self.assertEqual(second["usage"]["attempts"], 1)
            self.assertEqual(first["execution_effect"], "NONE")

    def test_two_runners_claim_same_episode_atomically_and_send_only_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            gate, fake = self.gate(temporary)
            other_gate, other_fake = self.gate(temporary)
            runners = [OwnerShadowEpisodeRunner(gate, fixture_only=True), OwnerShadowEpisodeRunner(other_gate, fixture_only=True)]
            ready = threading.Barrier(2)
            results = []

            def attempt(runner):
                ready.wait(timeout=5)
                results.append(runner.evaluate_episode("season", "one", market_context(), setups(6)))

            workers = [threading.Thread(target=attempt, args=(runner,)) for runner in runners]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(10)
            self.assertEqual(len(results), 2)
            self.assertEqual(sum(result["attempted"] for result in results), 1)
            self.assertEqual(fake.calls + other_fake.calls, 1)
            self.assertEqual(gate.ledger.summary("season")["series_attempts"], 1)
            self.assertTrue(all(result["execution_effect"] == "NONE" for result in results))

    def test_injected_e038_and_e039_existing_ledgers_preserve_their_own_caps(self):
        with tempfile.TemporaryDirectory() as temporary:
            for context in ("workshop_demo", "arena_live"):
                with self.subTest(context=context):
                    ledger = ArenaJevLedger(temporary, relative_path="runtime/exchange_v1/" + LEDGERS[context],
                                            budget=POLICIES[context])
                    with ledger.connect() as db:
                        for i in range(POLICIES[context]["season_attempts"]):
                            db.execute("INSERT INTO requests VALUES(?,?,?,?,?,?)", (context + str(i), "season", 900, "old", 0, "accounted"))
                    gate, fake = self.gate(temporary)
                    gate.ledger = ledger
                    before = ledger.summary("season")
                    result = OwnerShadowEpisodeRunner(gate, fixture_only=True).evaluate_episode("season", "one", market_context(), setups(6))
                    self.assertEqual(result["reason"], "budget_or_unknown_billing")
                    self.assertEqual(fake.calls, 0)
                    self.assertEqual(ledger.summary("season"), before)
                    self.assertEqual(ledger.path.name, LEDGERS[context])
                    self.assertEqual(ledger.summary("season")["limits"], POLICIES[context])


class CurrentSharedPureFixtures(NetworkBlockedTest):
    def test_current_public_recorder_projection_refuses_prose_in_unused_fields(self):
        for field in ("groupId", "ctMult"):
            raw = fixture_raw()
            raw["instrument"][0][field] = "FAKE_PRIVATE_PROSE"
            with self.assertRaisesRegex(ValueError, "public_numeric_value"):
                public_projection(raw)
        raw = fixture_raw()
        raw["candles"][0][6] = "FAKE_PRIVATE_PROSE"
        with self.assertRaisesRegex(ValueError, "public_candle_shape"):
            public_projection(raw)

    def test_current_public_recorder_gap_and_epoch_completion_never_emit_trade_action(self):
        with tempfile.TemporaryDirectory() as temporary:
            feed = FixturePublicSequence()
            fail = {"once": True}

            def public_get(name):
                if name == "ticker" and fail["once"]:
                    fail["once"] = False
                    raise TimeoutError("FAKE_PRIVATE_EXCEPTION_DO_NOT_PERSIST")
                return feed(name)

            recorder = PublicRecorder(temporary, get_public=public_get, build_snapshot_fn=build_snapshot,
                                      wall=feed.wall, mono=lambda: 10, epoch_ticks=1, max_epochs=2)
            self.assertEqual(recorder.tick()["gaps"], 1)
            self.assertEqual(recorder.tick()["samples"], 1)
            stopped = recorder.tick()
            self.assertEqual(stopped["reason"], "recorder_epoch_cap")
            self.assertFalse(stopped["affects_trading"])
            recorded = b"".join(p.read_bytes() for p in Path(temporary).rglob("*.jsonl"))
            self.assertNotIn(b"FAKE_PRIVATE", recorded)

    def test_current_typed_shadow_rejects_whole_invalid_batch_and_keeps_known_cost(self):
        pack = build_pack(market_context(), setups(6))
        response = answer_response(pack)
        response["answers"]["stalling"]["confidence"] = .9
        result = consume(pack, response, elapsed_ms=1, source_sha256="a" * 64)
        self.assertEqual(result["labels"], {})
        self.assertEqual(result["reported_cost_usd"], .00001)
        self.assertEqual(result["execution_effect"], "NONE")
        self.assertEqual(result["allowed"], [])
        self.assertFalse(pack.payload["provider"]["allow_fallbacks"])

    def test_current_typed_projection_never_accepts_account_or_author_prompt(self):
        context = market_context()
        context["position"] = {"contracts": "3"}
        with self.assertRaisesRegex(ValueError, "market_projection_shape"):
            build_pack(context, setups(6))
        context = market_context()
        setup = setups(6)
        setup[0]["author_prompt"] = "FAKE_UNTRUSTED_PROSE"
        with self.assertRaisesRegex(ValueError, "setup_projection_shape"):
            build_pack(context, setup)

    def test_current_export_disallows_overwrite_and_does_not_relabel_fixture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = PublicRecorderRepository(root / "public", build_snapshot_fn=build_snapshot,
                                              get_factory=FixturePublicSequence, source="FIXTURE")
            started = service.start_recording(interval_seconds=0, max_observations=20)
            service.wait_recording(15)
            target = root / "tape.jsonl"
            receipt = service.export_training_tape(started["dataset_id"], target)
            original = target.read_bytes()
            self.assertTrue(receipt["fixture_only"])
            self.assertEqual(receipt["source"], "FIXTURE")
            self.assertFalse(receipt["execution_permitted"])
            with self.assertRaisesRegex(ValueError, "destination"):
                service.export_training_tape(started["dataset_id"], target)
            self.assertEqual(target.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
