"""Cold migration and battle downloads in temporary roots; never paid APIs or production."""
from copy import deepcopy
from contextlib import closing
import http.client
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from tools.server_transfer import export_state, import_state, TransferError, REQUIRED, HANDOFF, IMPORTED, safe_name, main as transfer_main
from trading_lab.arena import Arena, exclusive
from trading_lab.arena_strategies import catalogue_defaults, fingerprint, ArenaError
from trading_lab.battle_export import create_archive, verify_archive
from trading_lab.arena_market import build_snapshot
from trading_lab.exchange_lab import ExchangeLab
from trading_lab.match_journal import append_events, checkpoint
from trading_lab.strategy_contract import payload_of
from trading_lab.web_panel import Panel, Server, PanelError
from tests import test_battle_review
from tests.test_cycle_diagnostics import event
from tests.test_workshop import raw_public


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def cold_fixture(root):
    for relative in REQUIRED:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative.endswith(".sqlite3"):
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("create table spent(attempts integer, cost text)")
                connection.execute("insert into spent values (19, '0.0012')")
                connection.commit()
        else:
            write_json(path, {"runs": [], "seasons": [], "generation": 0})
    for name, env in (("demo", "demo"), ("arena-a", "live"), ("arena-b", "live")):
        (root / (".env.okx." + name + ".local")).write_text(
            "OKX_SITE=global\nOKX_ENV=" + env + "\nOKX_API_KEY=FAKE_TRANSFER_KEY\n"
            "OKX_API_SECRET=FAKE_TRANSFER_SECRET\nOKX_API_PASSPHRASE=FAKE_PASS\n", encoding="utf-8")
    (root / ".env.local").write_text("OPENROUTER_API_KEY=FAKE_JEV_KEY\n", encoding="utf-8")
    write_json(root / "runtime/workshop_v1/frozen.json", {"recipe_sha256": "a" * 64, "version": 1})
    (root / "runtime/exchange_v1/old.snapshots.jsonl").write_text('{"old":true}\n', encoding="utf-8")


def battle_fixture():
    state = test_battle_review.ReviewTests().fixture()
    state.update(fixture_only=True, duration_seconds=300, execution_mode="shared_jev")
    recipes = []
    for current in state["strategies"]:
        row = deepcopy(catalogue_defaults()[0])
        row.update(id=current["id"], name=current["id"], version=1)
        row["recipe_sha256"] = fingerprint(payload_of(row))
        recipes.append(row)
    state["strategies"] = recipes
    return state


def rewritten_zip(source, replacement):
    source.seek(0)
    out = io.BytesIO()
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(out, "w") as target:
        for name in original.namelist():
            target.writestr(name, replacement.get(name, original.read(name)))
    out.seek(0)
    return out


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.source, self.destination = self.path / "source", self.path / "destination"
        cold_fixture(self.source)
        self.archive = self.path / "state.private.zip"

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_transfer_preserves_ledgers_recipes_and_optional_credentials(self):
        result = export_state(self.source, self.archive, include_credentials=True)
        self.assertTrue(result["paid_ledgers_preserved"])
        self.assertTrue((self.source / HANDOFF).is_file())
        with zipfile.ZipFile(self.archive) as archive:
            manifest = json.loads(archive.read("TRANSFER_MANIFEST.json"))
            self.assertNotIn(HANDOFF, archive.namelist())
            before = {r["path"]: (self.source / r["path"]).read_bytes() for r in manifest["files"]}
        with self.assertRaises(TransferError):
            import_state(self.destination, self.archive)
        result = import_state(self.destination, self.archive, source_stopped=True)
        self.assertFalse(result["financial_actions"])
        self.assertFalse(result["automatic_trade_start"])
        for name, content in before.items():
            self.assertEqual((self.destination / name).read_bytes(), content)
        self.assertTrue((self.destination / IMPORTED).is_file())
        with self.assertRaises(TransferError):
            import_state(self.destination, self.archive, source_stopped=True)
        with self.assertRaises(TransferError):
            export_state(self.source, self.path / "again.zip")

    def test_default_export_omits_credentials(self):
        result = export_state(self.source, self.archive)
        self.assertFalse(result["contains_credentials"])
        with zipfile.ZipFile(self.archive) as archive:
            self.assertFalse(any(name.startswith(".env") for name in archive.namelist()))

    def test_old_panel_preview_script_does_not_block_transfer_of_full_history(self):
        preview = self.source / "runtime/web_panel/preview_v3.py"
        preview.parent.mkdir(parents=True)
        preview.write_bytes(b"# old local preview, not runtime state\n")
        upper = preview.with_name("legacy.PY")
        upper.write_bytes(b"# Windows suffix variant\n")
        hidden = preview.with_name(".py")
        hidden.write_bytes(b"# single extension filename\n")
        before = {path.name: path.read_bytes() for path in (preview, upper, hidden)}
        history = self.source / "runtime/web_panel/history.json"
        history.write_bytes(b'{"cycles":["completed"]}')
        export_state(self.source, self.archive, include_credentials=True)
        with zipfile.ZipFile(self.archive) as archive:
            self.assertNotIn("runtime/web_panel/preview_v3.py", archive.namelist())
            self.assertNotIn("runtime/web_panel/legacy.PY", archive.namelist())
            self.assertNotIn("runtime/web_panel/.py", archive.namelist())
            self.assertEqual(archive.read("runtime/web_panel/history.json"), history.read_bytes())
        import_state(self.destination, self.archive, source_stopped=True)
        self.assertEqual((self.destination / "runtime/web_panel/history.json").read_bytes(), history.read_bytes())
        self.assertEqual((self.destination / "runtime/exchange_v1/old.snapshots.jsonl").read_bytes(),
                         (self.source / "runtime/exchange_v1/old.snapshots.jsonl").read_bytes())
        for path in (preview, upper, hidden):
            self.assertEqual(path.read_bytes(), before[path.name])
        for relative in REQUIRED:
            self.assertEqual((self.destination / relative).read_bytes(), (self.source / relative).read_bytes())

    def test_cli_reports_known_failure_code_without_raw_state_or_secret_text(self):
        args = ["export", "--source", str(self.source), "--output", str(self.archive)]
        output = io.StringIO()
        with patch("tools.server_transfer.export_state", side_effect=TransferError("pending_transfer_requires_review")), \
                patch("tools.server_transfer.sys.stderr", output):
            self.assertEqual(transfer_main(args), 2)
        self.assertIn("pending_transfer_requires_review", output.getvalue())
        self.assertIn(".pending", output.getvalue())
        for failure in (TransferError("PRIVATE_UNEXPECTED_SECRET"), ValueError("PRIVATE_BAD_STATE"),
                        OSError("PRIVATE_KEY_VALUE")):
            output = io.StringIO()
            with patch("tools.server_transfer.export_state", side_effect=failure), \
                    patch("tools.server_transfer.sys.stderr", output):
                self.assertEqual(transfer_main(args), 2)
            self.assertNotIn("PRIVATE_", output.getvalue())

    def test_active_and_unknown_position_are_refused_without_handoff(self):
        identity = "c" * 32
        write_json(self.source / REQUIRED[0], {"runs": [identity]})
        for status, qty in (("RUNNING", 0), ("PAUSED", 0), ("DRAINING", .0001), ("DONE", None), ("DONE", .0001)):
            with self.subTest(status=status, qty=qty):
                write_json(self.source / ("runtime/exchange_v1/" + identity + ".json"),
                           {"status": status, "bots": [{"id": "bot", "position_btc": qty}]})
                with self.assertRaises(TransferError):
                    export_state(self.source, self.archive)
                self.assertFalse(self.archive.exists())
                self.assertFalse((self.source / HANDOFF).exists())

    def test_manager_lock_blocks_cold_copy(self):
        with exclusive(self.source / "runtime/exchange_v1/manager.lock"):
            with self.assertRaises(TransferError):
                export_state(self.source, self.archive)
        self.assertFalse((self.source / HANDOFF).exists())

    def test_existing_ledger_is_never_replaced(self):
        export_state(self.source, self.archive)
        original = self.destination / REQUIRED[4]
        original.parent.mkdir(parents=True)
        original.write_bytes(b"NEVER_RESET")
        with self.assertRaises(TransferError):
            import_state(self.destination, self.archive, source_stopped=True)
        self.assertEqual(original.read_bytes(), b"NEVER_RESET")
        self.assertFalse((self.destination / IMPORTED).exists())

    def test_corruption_and_unsafe_extra_member_are_rejected_before_import(self):
        export_state(self.source, self.archive)
        with self.archive.open("rb") as source:
            bad = rewritten_zip(source, {REQUIRED[4]: b"CHANGED"})
        self.archive.write_bytes(bad.read())
        with self.assertRaises(TransferError):
            import_state(self.destination, self.archive, source_stopped=True)
        self.assertFalse((self.destination / "runtime").exists())
        with zipfile.ZipFile(self.archive, "a") as archive:
            archive.writestr("../escape", b"oops")
        with self.assertRaises(TransferError):
            import_state(self.destination, self.archive, source_stopped=True)
        self.assertFalse((self.path / "escape").exists())

    def test_windows_special_paths_refused(self):
        for name in ("../a", "/a", "runtime/CON.json", "runtime/a.", "runtime/NUL", "C:/a", "runtime\\a"):
            with self.subTest(name=name), self.assertRaises(TransferError):
                safe_name(name)

    def test_local_panel_cannot_restart_after_transfer(self):
        export_state(self.source, self.archive)
        with patch("trading_lab.web_panel.Arena", side_effect=AssertionError("must not initialize")):
            with self.assertRaises(PanelError):
                Server(Panel(self.source, launcher=lambda *args, **kwargs: None), 0)

    def test_failed_final_publish_keeps_source_guard(self):
        with patch("tools.server_transfer.os.replace", side_effect=OSError("publish failed")):
            with self.assertRaises(OSError):
                export_state(self.source, self.archive)
        self.assertTrue((self.source / HANDOFF).exists())
        self.assertFalse(self.archive.exists())
        self.assertTrue(self.archive.with_suffix(".zip.pending").is_file())


class BattleFileTests(unittest.TestCase):
    def test_paper_v2_is_labelled_as_model_and_keeps_own_recipe(self):
        from tests.test_replay_evidence_integration import paper_fixture
        state = paper_fixture()
        for row in state["strategies"]:
            if row["id"] != "cash":
                row["recipe_sha256"] = fingerprint(payload_of(row))
        with create_archive(state) as output:
            verify_archive(output)
            output.seek(0)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(json.loads(archive.read("manifest.json"))["mode"], "PAPER")
                self.assertEqual(json.loads(archive.read("battle-replay.json"))["evidence_kind"], "MODEL")
                self.assertEqual(json.loads(archive.read("strategies.json"))[0]["recipe"]["strategy_schema_version"], 2)

    def test_exact_numeric_jev_wire_payload_is_retained_and_verified_without_a_model_call(self):
        from tests import test_battle_review_v2_request as request_tests
        from trading_lab.arena_jev import make_payload
        case = request_tests.ValidateRequestSchemaTests()
        case.setUpClass()
        payload = make_payload([case.candidate(case.v2)], "OKX_LIVE")
        wire = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        state = battle_fixture()
        evidence = event(state, None, "jev_request", {"payload": payload,
            "payload_sha256": hashlib.sha256(wire.encode()).hexdigest(), "payload_wire_json": wire}, 2)
        with create_archive(state) as output:
            verify_archive(output)
            output.seek(0)
            with zipfile.ZipFile(output) as archive:
                record = json.loads(archive.read("battle-review.json"))["request_payloads"][0]
                self.assertEqual(record["seq"], evidence["seq"])
                self.assertEqual(record["payload_wire_json"], wire)
                self.assertEqual(record["wire_hash_status"], "VERIFIED_WIRE")

    def test_match_settings_include_limits_and_ignore_foreign_private_fields(self):
        from trading_lab.exchange_engine import LIMITS
        from trading_lab.match_protocol import SCORE_POLICY
        state = battle_fixture()
        state["match_manifest"] = {"score_policy": deepcopy(SCORE_POLICY),
            "risk_and_execution": {**deepcopy(LIMITS), "private_key": "PRIVATE_SETTINGS"},
            "jev_budget": {"season_attempts": 61, "private_key": "PRIVATE_BUDGET"},
            "participants": [{"strategy_id": "Sol", "version": 1, "account_id": "arena-a",
                              "uid": "PRIVATE_ACCOUNT_UID"}]}
        with create_archive(state) as output:
            verify_archive(output)
            output.seek(0)
            with zipfile.ZipFile(output) as archive:
                settings = json.loads(archive.read("match-settings.json"))
                self.assertEqual(settings["risk_and_execution"]["max_total_entries"], 20)
                self.assertEqual(settings["score_policy"], SCORE_POLICY)
                self.assertEqual(settings["participants"][0]["account_id"], "arena-a")
                self.assertNotIn(b"PRIVATE_", archive.read("match-settings.json"))

    def test_verified_archive_has_full_recipes_fills_costs_and_no_private_identifiers(self):
        state = battle_fixture()
        before = deepcopy(state)
        with create_archive(state) as output:
            receipt = verify_archive(output)
            self.assertTrue(receipt["verified"])
            self.assertEqual(receipt["events"], len(state["events"]))
            self.assertEqual(receipt["participants"], 2)
            self.assertTrue(receipt["final"])
            output.seek(0)
            with zipfile.ZipFile(output) as archive:
                review = json.loads(archive.read("battle-review.json"))
                self.assertEqual(review["participants"][1]["costs"]["net_usdt"], "-0.007")
                data = b"".join(archive.read(name) for name in archive.namelist())
                for private in (b"PRIVATE_CLIENT", b"PRIVATE_CLOSE", b"PRIVATE_UID", b"PRIVATE_KEY", b"PRIVATE_PROMPT"):
                    self.assertNotIn(private, data)
        self.assertEqual(state, before)

    def test_active_and_incomplete_are_not_reported_as_confirmed_final(self):
        state = battle_fixture()
        for status, incomplete in (("RUNNING", False), ("DRAINING", False), ("PAUSED", True), ("DONE", True)):
            state.update(status=status, incomplete=incomplete)
            with self.subTest(status=status), create_archive(state) as output:
                receipt = verify_archive(output)
                self.assertEqual(receipt["final"], status == "DONE")
                self.assertEqual(receipt["incomplete"], incomplete)

    def test_public_market_rebuilt_bounded_at_journal_snapshot_and_invalid_counted(self):
        state = battle_fixture()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "market.jsonl"
            at = 1800000030
            raw = raw_public(at)
            raw["authorization"] = "PRIVATE_PROVIDER"
            snapshot = build_snapshot(raw, at)
            state["latest_market"] = snapshot
            rows = [json.dumps({"at": at, "raw": raw, "funding_status": "available"}), "broken",
                    json.dumps({"at": at + 60, "raw": raw_public(at + 60), "funding_status": "available"})]
            path.write_text("\n".join(rows) + "\n", encoding="utf-8")
            with create_archive(state, snapshots_path=path) as output:
                verify_archive(output)
                output.seek(0)
                with zipfile.ZipFile(output) as archive:
                    metadata = json.loads(archive.read("manifest.json"))
                    self.assertEqual(metadata["market_samples"], 1)
                    self.assertEqual(metadata["market_rows_omitted"], 1)
                    market = json.loads(archive.read("market.jsonl"))
                    self.assertEqual(market["features"], snapshot["features"])
                    self.assertEqual(len(market["candles"]), 30)
                    self.assertEqual(market["candles"][-1]["close"], raw["candles"][-1][4])
                    self.assertNotIn("raw", market)
                    self.assertNotIn(b"PRIVATE_PROVIDER", archive.read("market.jsonl"))

    def test_archive_cap_and_tamper_never_pass_verification(self):
        state = battle_fixture()
        with self.assertRaises(ArenaError):
            create_archive(state, max_bytes=50)
        with create_archive(state) as output:
            changed = rewritten_zip(output, {"strategies.json": b"[]"})
            with self.assertRaises(ArenaError):
                verify_archive(changed)
        with create_archive(state) as output:
            with zipfile.ZipFile(output) as archive:
                manifest = json.loads(archive.read("manifest.json"))
            manifest["status"] = "RUNNING"
            changed = rewritten_zip(output, {"manifest.json": json.dumps(manifest).encode()})
            with self.assertRaises(ArenaError):
                verify_archive(changed)

    def test_missing_recipe_bytes_refused_instead_of_substituting_newest_version(self):
        state = battle_fixture()
        state["strategies"][0]["recipe_sha256"] = "f" * 64
        with self.assertRaises(ArenaError):
            create_archive(state)

    def test_http_export_restores_all_segmented_events_and_has_no_execution(self):
        state = battle_fixture()
        for _ in range(610):
            event(state, state["bots"][0], "entry_checks", {"local_checks_passed": False, "checks": []})
        state.update(protocol_version=8)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            arena = Arena(root, background=False, feed_factory=lambda: self.fail("no market call"))
            factory = lambda r, w, a: ExchangeLab(r, w, a, background=False,
                        api_factory=lambda *args: self.fail("no exchange call"),
                        feed_factory=lambda *args: self.fail("no market call"),
                        jev_factory=lambda *args: self.fail("no model call"))
            panel = Panel(root, launcher=lambda *args, **kwargs: self.fail("no subprocess launch"))
            server = Server(panel, 0, arena=arena, exchange_factory=factory)
            events_path = server.exchange.path / (state["id"] + ".events.jsonl")
            append_events(events_path, state, 0)
            compact = checkpoint(state)
            server.exchange.load = lambda identity: deepcopy(compact) if identity == state["id"] else (_ for _ in ()).throw(ArenaError("missing"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            def get(path, host=None):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=20)
                connection.request("GET", path, headers={"Host": host or "127.0.0.1:" + str(server.server_port)})
                response = connection.getresponse()
                result = response.status, dict(response.headers), response.read()
                connection.close()
                return result
            url = "/api/battle-export/exchange/" + state["id"]
            try:
                response = get(url)
                self.assertEqual(response[0], 200)
                self.assertEqual(response[1]["Content-Type"], "application/zip")
                self.assertIn("attachment;", response[1]["Content-Disposition"])
                proof = verify_archive(io.BytesIO(response[2]))
                self.assertEqual(proof["events"], len(state["events"]))
                self.assertTrue(proof["history_complete"])
                self.assertEqual(get(url, "attacker.example")[0], 403)
                self.assertEqual(get("/api/battle-export/live/" + state["id"])[0], 404)
                with server.battle_export_lock:
                    self.assertEqual(get(url)[0], 429)
                self.assertEqual(get("/api/battle-export/exchange/" + "e" * 32)[0], 404)
            finally:
                server.shutdown()
                thread.join(timeout=3)
                server.server_close()


if __name__ == "__main__":
    unittest.main()
