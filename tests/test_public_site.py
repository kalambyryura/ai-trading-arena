"""Public static site: fictional receipted journals, temp folders only; no transport, owner route or financial action."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trading_lab.arena import Arena, write
from trading_lab.arena_strategies import ArenaError
from trading_lab.battle_review import build_review, digest
from trading_lab.event_replay import build_packet
from trading_lab.league import League
from trading_lab.match_protocol import SCORE_POLICY
from trading_lab.public_site import (battle_item, build, collect, league_projection, leak_scan, load_journal,
                                     public_review, verify_public_review, verify_site)
from trading_lab.spectator_export import receipt_template
from tests.test_replay_evidence_integration import exchange_fixture, paper_fixture
import tools.public_site as cli

PRIVATE_KEYS = ('"expected"', '"actual"', '"snapshot_sha256"', '"payload"', '"features"', '"ordId"', '"clOrdId"', '"result"',
                '"trade_ledger"', '"request_payloads"', '"rules"', '"prompt"')
PRIVATE_VALUES = ("FICTIONAL_ENTRY", "FICTIONAL_CLOSE", "order_FICTIONAL", "PRIVATE_MARKER", "84000")


def approved(packet, release_after_ms=0):
    return {**receipt_template(packet), "privacy": "PASS", "rights": "APPROVED", "rights_reference": "Synthetic fixture; no exchange data",
            "owner_release": "APPROVED", "owner_reference": "Offline release test", "access": "PUBLIC", "release_after_ms": release_after_ms}


def forbidden(*args, **kwargs):
    raise AssertionError("transport or financial call forbidden")


def as_model(state):
    """R2.1.11 (E, D141 p. 2): example-authored participants never count; where a test ranks, its recipes are a model's."""
    from trading_lab.arena_strategies import fingerprint
    from trading_lab.strategy_contract import payload_of
    for row in state["strategies"]:
        if row.get("id") != "cash" and isinstance(row.get("author"), dict):
            row["author"] = {**row["author"], "kind": "model"}
            if "name" in row:  # a full recipe keeps its fingerprint true; the minimal fixture rows have none to keep
                row["recipe_sha256"] = fingerprint(payload_of(row))
    return state


class Workspace:
    """Temp root with one PAPER season and one fictional Demo run, both marked fixture_only, plus approved receipts."""

    def __enter__(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.paper = paper_fixture(); self.paper["fixture_only"] = True
        self.demo = as_model(exchange_fixture()); self.demo["fixture_only"] = True
        write(self.root / "runtime" / "arena_v4" / (self.paper["id"] + ".json"), self.paper)
        write(self.root / "runtime" / "exchange_v1" / (self.demo["id"] + ".json"), self.demo)
        self.receipts = self.root / "receipts"
        self.receipts.mkdir()
        for state in (self.paper, self.demo):
            self.receipt(state, approved(build_packet(state)))
        self.battles = [("paper", self.paper["id"]), ("exchange", self.demo["id"])]
        return self

    def receipt(self, state, receipt):
        (self.receipts / (state["id"] + ".json")).write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")

    def __exit__(self, *exc):
        self.directory.cleanup()


class PublicSiteTests(unittest.TestCase):
    def test_e_a_battle_of_examples_only_never_counts(self):
        state = exchange_fixture()  # its recipes are Codex examples
        state["match_manifest"] = {"score_policy": deepcopy(SCORE_POLICY)}
        item = battle_item("exchange", state, approved(build_packet(state)), 100)
        row = league_projection([item])["battles"][0]
        self.assertEqual((row["competitive_validity"], row["score"]["reason"]), ("INELIGIBLE", "example_only"))
        self.assertEqual({e["reason"] for e in row["score"]["excluded"]}, {"example_author"})

    def test_public_projection_preserves_new_score_policy_and_legacy_absence(self):
        state = as_model(exchange_fixture())
        self.assertNotIn("score_policy", public_review(build_review(state))["match"])
        state["match_manifest"] = {"score_policy": deepcopy(SCORE_POLICY)}
        bundle = build_review(state)
        dto = public_review(bundle)
        self.assertEqual(dto["match"]["score_policy"], SCORE_POLICY)
        self.assertTrue(verify_public_review(dto)["verified"])
        item = battle_item("exchange", state, approved(build_packet(state)), 100)
        row = league_projection([item])["battles"][0]
        self.assertEqual(row["score"]["sole_winner"], state["bots"][0]["id"])
        self.assertNotIn("cash", row["score"]["ordered"])
        altered = deepcopy(dto)
        altered["match"]["score_policy"]["private"] = "PRIVATE_MARKER"
        altered["integrity"]["review_sha256"] = digest({k: v for k, v in altered.items() if k != "integrity"})
        with self.assertRaises(ArenaError): verify_public_review(altered)

    def test_public_review_is_a_closed_allowlist_without_recipe_or_private_fields(self):
        for state in (paper_fixture(), exchange_fixture()):
            bundle = build_review(state)
            dto = public_review(bundle)
            self.assertTrue(verify_public_review(dto)["verified"])
            self.assertEqual(dto["source_bundle_sha256"], bundle["integrity"]["bundle_sha256"])
            dumped = json.dumps(dto, ensure_ascii=False)
            for marker in PRIVATE_KEYS + PRIVATE_VALUES:
                self.assertNotIn(marker, dumped, marker)
            self.assertEqual(len(dto["events"]), len(state["events"]))
            self.assertIsInstance(dto["participants"][0]["funnel"]["entry"]["checks"], int)
            # The public DTO never feeds the private verifier, and tampering or any unknown field is rejected.
            for mutate in (lambda d: d["match"].update(private="PRIVATE_MARKER"), lambda d: d["participants"][0].update(uid="PRIVATE_MARKER"),
                           lambda d: d["participants"][0]["costs"].update(cash_delta_usdt="1"), lambda d: d["events"][0]["data"].update(features={"x": "1"}),
                           lambda d: d["events"][0].update(seq=True), lambda d: d.update(request_payloads=[])):
                altered = deepcopy(dto); mutate(altered)
                altered["integrity"]["review_sha256"] = digest({k: v for k, v in altered.items() if k != "integrity"})
                with self.assertRaises(ArenaError): verify_public_review(altered)
            altered = deepcopy(dto); altered["participants"][0]["costs"]["net_usdt"] = "1"
            with self.assertRaises(ArenaError): verify_public_review(altered)

    def test_build_is_gated_deterministic_relative_and_leak_free(self):
        with Workspace() as ws, patch("urllib.request.urlopen", side_effect=forbidden):
            requests = ws.battles + [("paper", "f" * 32), ("exchange", ws.paper["id"]), ("paper", "../escape")]
            collected = collect(ws.root, requests, ws.receipts, now_ms=100)
            self.assertEqual([item["id"] for item in collected["items"]], [ws.paper["id"], ws.demo["id"]])
            self.assertEqual(len(collected["skipped"]), 3)
            self.assertTrue(all(skipped["reason"] for skipped in collected["skipped"]))
            ws.receipt(ws.demo, receipt_template(build_packet(ws.demo)))
            pending = collect(ws.root, ws.battles, ws.receipts, now_ms=100)
            self.assertEqual([item["id"] for item in pending["items"]], [ws.paper["id"]])
            self.assertIn("не схвалено", pending["skipped"][0]["reason"])
            ws.receipt(ws.demo, approved(build_packet(ws.demo), 101))
            early = collect(ws.root, ws.battles, ws.receipts, now_ms=100)  # release_after_ms in the future → not yet public
            self.assertEqual([item["id"] for item in early["items"]], [ws.paper["id"]])
            ws.receipt(ws.demo, approved(build_packet(ws.demo)))

            first = build(ws.root / "site-a", ROOT / "web", collected["items"], now_ms=100, derive=False)
            second = build(ws.root / "site-b", ROOT / "web", collected["items"], now_ms=100, derive=False)
            self.assertFalse(first["public_hosting"])
            self.assertEqual(first["manifest_sha256"], second["manifest_sha256"])
            self.assertEqual((ws.root / "site-a" / "manifest.sha256").read_bytes(), (ws.root / "site-b" / "manifest.sha256").read_bytes())
            site = ws.root / "site-a"
            self.assertEqual(verify_site(site, now_ms=100)["battles"], 2)
            self.assertTrue(leak_scan(site)["clean"])
            with self.assertRaises(ArenaError): build(site, ROOT / "web", collected["items"], now_ms=100)

            index = (site / "index.html").read_text(encoding="utf-8")
            self.assertIn('name="robots" content="noindex', index)
            self.assertNotIn("__PUBLIC_SITE_DATA__", index)
            for page in (index, (site / f"battle-{ws.demo['id']}.html").read_text(encoding="utf-8")):
                for absolute in ('href="/', 'src="/', "/api/", "panel-token", "__SPECTATOR_DATA__"):
                    self.assertNotIn(absolute, page)
            battle = (site / f"battle-{ws.demo['id']}.html").read_text(encoding="utf-8")
            self.assertIn("DEMO · тестові кошти біржі OKX", battle)
            self.assertIn('href="index.html"', battle)
            self.assertIn('name="robots"', battle)
            self.assertNotIn("/assets/", (site / "arena-coliseum.js").read_text(encoding="utf-8"))
            battles = json.loads((site / "battles.json").read_text(encoding="utf-8"))
            self.assertEqual([row["id"] for row in battles["league"]["battles"]], [ws.demo["id"], ws.paper["id"]], "newest first")
            self.assertEqual(battles["latest"]["id"], ws.demo["id"])
            self.assertEqual(battles["built_at_ms"], 0)
            # The no-trade PAPER fixture has no modelled settlement, so it is shown but never ranked; only the Demo battle forms a cohort.
            validity = {row["id"]: row["competitive_validity"] for row in battles["league"]["battles"]}
            self.assertEqual(validity, {ws.paper["id"]: "INELIGIBLE", ws.demo["id"]: "ELIGIBLE"})
            self.assertEqual([c["cohort"] for c in battles["league"]["cohorts"]], [battles["league"]["battles"][0]["cohort"]])
            self.assertEqual({row["id"] for row in battles["league"]["frequencies"]}, {"cash", ws.demo["bots"][0]["id"]})
            self.assertTrue(all(row["display_label"] for row in battles["league"]["battles"]))
            self.assertLessEqual((site / "latest.json").stat().st_size, 2048)
            for identity in (ws.paper["id"], ws.demo["id"]):
                folder = site / "battles" / identity
                self.assertTrue(verify_public_review(json.loads((folder / "review.json").read_text(encoding="utf-8")))["verified"])
                self.assertEqual(json.loads((folder / "replay.json").read_text(encoding="utf-8"))["evidence_kind"], "SYNTHETIC")
            dumped = (site / "battles.json").read_text(encoding="utf-8") + index
            for marker in PRIVATE_VALUES:
                self.assertNotIn(marker, dumped)

            indexable = build(ws.root / "site-c", ROOT / "web", collected["items"], indexable=True, now_ms=100, derive=False)
            self.assertTrue(indexable["indexable"])
            self.assertNotIn('name="robots"', (ws.root / "site-c" / "index.html").read_text(encoding="utf-8"))
            self.assertFalse(indexable["assets"]["derived"])
            self.assertTrue((ws.root / "site-c" / "assets" / "coliseum-floor.png").is_file())
            self.assertIn("assets/coliseum-floor.png", (ws.root / "site-c" / "arena-coliseum.js").read_text(encoding="utf-8"))
            self.assertEqual(indexable["assets"]["bytes_served"], indexable["assets"]["bytes_source"])

            # A tampered or extra file in a built folder fails verification before any upload.
            (site / "battles.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(ArenaError): verify_site(site, now_ms=100)
            (ws.root / "site-b" / "extra.txt").write_text("x", encoding="utf-8")
            with self.assertRaises(ArenaError): verify_site(ws.root / "site-b", now_ms=100)

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow not installed: originals are served")
    def test_derived_webp_assets_replace_the_large_pngs_and_their_references(self):
        with Workspace() as ws:
            collected = collect(ws.root, ws.battles, ws.receipts, now_ms=100)
            result = build(ws.root / "site", ROOT / "web", collected["items"], now_ms=100)
            site = ws.root / "site"
            self.assertTrue(result["assets"]["derived"])
            self.assertLess(result["assets"]["bytes_served"], result["assets"]["bytes_source"] * 0.4)
            for name in ("coliseum-floor", "cash", "human-poses", "robot-poses", "human-portrait", "robot-portrait"):
                self.assertTrue((site / "assets" / (name + ".webp")).is_file(), name)
                self.assertFalse((site / "assets" / (name + ".png")).exists(), name)
            self.assertTrue((site / "assets" / "Manrope.ttf").is_file())
            for name in ("arena-coliseum.js", "arena-coliseum.css"):
                text = (site / name).read_text(encoding="utf-8")
                self.assertNotIn(".png", text, name)
                self.assertNotIn("/assets/", text, name)
            self.assertIn("assets/coliseum-floor.webp", (site / "arena-coliseum.js").read_text(encoding="utf-8"))
            release = json.loads((site / "release.json").read_text(encoding="utf-8"))
            self.assertEqual({row["served"] for row in release["assets"]["files"] if row["source"] == "cash.png"}, {"cash.webp"})
            for original in ("coliseum-floor.png", "cash.png"):  # the originals in web/assets stay untouched
                self.assertEqual((ROOT / "web" / "assets" / original).stat().st_size, next(r["bytes_source"] for r in release["assets"]["files"] if r["source"] == original))
            self.assertEqual(verify_site(site, now_ms=100)["battles"], 2)

    def test_league_rows_match_the_private_league_status_projection(self):
        with Workspace() as ws:
            arena = Arena(ws.root, background=False, feed_factory=forbidden)
            try:
                arena.data["seasons"].append(ws.paper["id"])
                exchange = SimpleNamespace(lock=threading.RLock(), data={"runs": []}, path=ws.root / "runtime" / "exchange_v1")
                private = League(ws.root, arena, exchange).status()["battles"][0]
            finally:
                arena.control_owner.__exit__(None, None, None)
            receipt = json.loads((ws.receipts / (ws.paper["id"] + ".json")).read_text(encoding="utf-8"))
            public = league_projection([battle_item("paper", load_journal(ws.root, "paper", ws.paper["id"]), receipt, 100)])["battles"][0]
            for key in ("id", "cohort", "score", "competitive_validity", "settlement", "bundle_hash", "participants", "display_label",
                        "allocation_usdt", "execution_mode", "duration_seconds", "created_at", "mode", "status"):
                self.assertEqual(public[key], private[key], key)
            self.assertNotIn("error", public)

    def test_leak_scan_names_private_markers_and_verify_refuses_unfinished_folders(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "a.json").write_text('{"uid":"1","ordId":"2"}', encoding="utf-8")
            (folder / "b.js").write_text("fetch('/api/state')", encoding="utf-8")
            (folder / "c.txt").write_text("C:\\Projects\\Trading\\.env.okx.local", encoding="utf-8")
            (folder / "clean.css").write_text("body{align-items:flex-start}", encoding="utf-8")
            (folder / "image.png").write_bytes(b"uid /api/ fetch(")
            scan = leak_scan(folder)
            self.assertFalse(scan["clean"])
            self.assertEqual({f["marker"] for f in scan["findings"]}, {"account-id", "order-id", "transport", "owner-route", "local-path", "private-file"})
            self.assertEqual({f["file"] for f in scan["findings"]}, {"a.json", "b.js", "c.txt"})
            with self.assertRaises(ArenaError): verify_site(folder)

    def test_cli_builds_receipts_and_verifies_without_transport(self):
        with Workspace() as ws, patch("urllib.request.urlopen", side_effect=forbidden):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(cli.main(["receipt", f"paper:{ws.paper['id']}", "--root", str(ws.root), "--output", str(ws.root / "pending.json")]), 0)
                self.assertEqual(cli.main(["build", "--root", str(ws.root), "--battle", f"paper:{ws.paper['id']}", "--battle", f"exchange:{ws.demo['id']}",
                                           "--battle", "paper:" + "0" * 32, "--receipts", str(ws.receipts), "--output", str(ws.root / "cli-site"), "--original-assets"]), 0)
                self.assertEqual(cli.main(["verify", str(ws.root / "cli-site")]), 0)
                self.assertEqual(cli.main(["build", "--root", str(ws.root), "--battle", "paper:" + "0" * 32, "--receipts", str(ws.receipts),
                                           "--output", str(ws.root / "cli-empty")]), 1)
            self.assertEqual(json.loads((ws.root / "pending.json").read_text(encoding="utf-8"))["owner_release"], "PENDING")
            self.assertIn("Пропущено paper:" + "0" * 32, err.getvalue())
            self.assertIn("Збірку заблоковано", err.getvalue())
            lines = [line for line in out.getvalue().splitlines() if line.startswith("{")]
            self.assertEqual(json.loads(lines[0])["battles"], 2)
            self.assertFalse(json.loads(lines[0])["public_hosting"])
            self.assertEqual(json.loads(lines[1])["battles"], 2)
            self.assertFalse((ws.root / "cli-empty").exists())


if __name__ == "__main__":
    unittest.main()
