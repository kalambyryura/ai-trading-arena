"""Live packets (P1-7a): synthetic Battle ZIPs in temp folders only; no network, keys or exchange code."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import live_packets

A, B = "a" * 32, "b" * 32
RA, RB = "1" * 64, "2" * 64


def event(seq, at, bot, code, **data):
    return {"seq": seq, "at": at, "bot_id": bot, "code": code, "data": data}


def battle(extra_member=None, break_manifest=False, leak=False):
    snap = "c" * 64
    events = [event(1, "2026-10-30T16:23:30.000+00:00", None, "rules_only_mode", ai_used=False, execution_mode="rules_only"),
              event(2, "2026-10-30T16:24:02.000+00:00", A, "entry_checks", observed_at="1793377435.8", kind="entry",
                    local_checks_passed=True, snapshot_sha256=snap, checks=[{"feature": "return_5m_bps", "op": "lte", "expected": -35,
                                                                              "actual": -36.9, "passed": True}]),
              event(3, "2026-10-30T16:24:02.000+00:00", B, "entry_checks", observed_at="1793377435.8", kind="entry",
                    local_checks_passed=True, snapshot_sha256=snap, checks=[{"feature": "spread_bps", "op": "lte", "expected": 2,
                                                                              "actual": 0.1, "passed": True}]),
              event(4, "2026-10-30T16:24:02.100+00:00", None, "jev_shadow_payload", payload_sha256="d" * 64, snapshot_sha256=snap),
              event(5, "2026-10-30T16:24:02.100+00:00", None, "jev_shadow_payload", payload_sha256="e" * 64, snapshot_sha256=snap),
              event(6, "2026-10-30T16:24:02.200+00:00", A, "rules_only_review", kind="entry", rules_allowed=True, snapshot_sha256=snap),
              event(7, "2026-10-30T16:24:02.200+00:00", B, "rules_only_review", kind="entry", rules_allowed=True, snapshot_sha256=snap),
              event(8, "2026-10-30T16:24:06.900+00:00", A, "entry_cancelled"),
              event(9, "2026-10-30T16:24:07.000+00:00", A, "financial_observation", financial={"pnl_usdt": "0"}, settled=True),
              event(10, "2026-10-30T21:24:06.000+00:00", None, "season_done"),
              event(11, "2026-10-30T16:23:31.000+00:00", None, "exchange_clock_synchronized", round_trip_ms=80)]
    participant = lambda bot, name, recipe: {"id": bot, "name": name, "recipe_sha256": recipe, "version": 1, "status": "DONE",
                                             "funnel": {"signals": 1}, "trade_explanations": []}
    review = {"schema_version": 1, "match": {"id": "f" * 32, "mode": "OKX_LIVE", "code_sha256": "9" * 64},
              "participants": [participant(A, "Alpha · own", RA), participant(B, "Beta · other", RB)],
              "events": events, "frames": [{"at": "2026-10-30T16:24:07+00:00", "event_seq": 9,
                                             "bots": [{"id": A, "pnl_usdt": "0"}, {"id": B, "pnl_usdt": "0"}]}],
              "cash": {"name": "Cash", "net_usdt": "0"}, "projection": "private_author_equal_v1", "history_complete": True,
              "limitations": [], "request_payloads": [{"seq": 4, "payload_sha256": "d" * 64, "candidate_ids": [A], "payload": {"state": {"c0": 1}}},
                                                      {"seq": 5, "payload_sha256": "e" * 64, "candidate_ids": [B], "payload": {"state": {"c0": 2}}}],
              "market_gaps": [], "diagnostics_context": {}, "integrity": {}}
    if leak:
        review["participants"][0]["trade_explanations"] = ["compared with Beta · other"]
    strategies = [{"id": A, "version": 1, "recipe_sha256": RA, "recipe": {"name": "Alpha", "author": {"prompt": "work in C:/Projects/Trading and /home/x/y"}}},
                  {"id": B, "version": 1, "recipe_sha256": RB, "recipe": {"name": "Beta", "author": {"prompt": "secret idea"}}}]
    settings = {"schema": "jev-arena-match-settings/v1", "id": "f" * 32, "budget_usdt_per_bot": 100,
                "participants": [{"strategy_id": A, "recipe_sha256": RA, "account_id": "arena-a"},
                                 {"strategy_id": B, "recipe_sha256": RB, "account_id": "arena-b"}]}
    files = {"battle-review.json": json.dumps(review).encode(), "battle-replay.json": b"{}", "strategies.json": json.dumps(strategies).encode(),
             "match-settings.json": json.dumps(settings).encode(), "market.jsonl": b'{"at": 1793377435.8, "bid": 77415.5}\n',
             "README_UA.txt": "readme".encode()}
    manifest = {"schema": "jev-arena-battle-file/v1", "files": [{"path": k, "bytes": len(v), "sha256": hashlib.sha256(v).hexdigest()}
                                                                 for k, v in files.items()]}
    if break_manifest:
        manifest["files"][0]["sha256"] = "0" * 64
    files["manifest.json"] = json.dumps(manifest).encode()
    if extra_member:
        files[extra_member] = b"x"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class LivePacketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def build(self, **kw):
        path = self.root / "battle.zip"
        path.write_bytes(battle(**kw))
        return live_packets.build(path, {"alpha": A, "beta": B}, self.root / "out")

    def test_each_author_gets_only_their_own_data(self):
        written = self.build()
        folder = Path(written["alpha"]["path"])
        text = "".join(p.read_text(encoding="utf-8") for p in folder.iterdir())
        for marker in (B, RB, "Beta · other", "arena-a", "arena-b", "secret idea", "C:/Projects", "/home/x"):
            self.assertNotIn(marker, text)
        self.assertIn(A, text)
        recipe = json.loads((folder / "recipe.json").read_text(encoding="utf-8"))
        self.assertEqual(recipe["recipe"]["author"]["prompt"], "work in <local path> and <local path>")
        self.assertEqual(recipe["redacted_fields"], ["$.author.prompt"])
        events = [json.loads(l) for l in (folder / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual({e["seq"] for e in events}, {1, 2, 4, 6, 8, 9, 10}, "own events, own payload, lifecycle; no clock or other bot")
        payloads = json.loads((folder / "gate-payloads.json").read_text(encoding="utf-8"))
        self.assertEqual([p["candidate_ids"] for p in payloads], [[A]])
        frames = [json.loads(l) for l in (folder / "frames.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(frames[0]["bot"]["id"], A)
        self.assertEqual((folder / "market.jsonl").read_bytes(), b'{"at": 1793377435.8, "bid": 77415.5}\n', "public prices unchanged")
        settings = json.loads((folder / "match-settings.json").read_text(encoding="utf-8"))
        self.assertNotIn("account_id", settings["participant"])
        self.assertEqual(live_packets.verify(folder)["ok"], True)

    def test_signals_carry_the_condition_values_and_an_inferred_old_reason(self):
        folder = Path(self.build()["alpha"]["path"])
        rows = json.loads((folder / "signals.json").read_text(encoding="utf-8"))["signals"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["checks"][0]["actual"], -36.9)
        self.assertEqual((row["outcome"], row["reason"], row["reason_source"]),
                         ("cancelled", "decision_snapshot_too_old", "inferred_from_timing_pre_r1_full_1"))
        self.assertGreater(row["steps"][-1]["seconds_after_snapshot"], 10)
        beta = json.loads((Path(self.build_again()["beta"]["path"]) / "signals.json").read_text(encoding="utf-8"))["signals"][0]
        self.assertEqual((beta["outcome"], beta["reason"]), ("no_event", "unknown"), "a silent drop under 10 s stays unknown")

    def build_again(self):
        path = self.root / "battle2.zip"
        path.write_bytes(battle())
        return live_packets.build(path, {"beta": B}, self.root / "out2")

    def test_verify_catches_tampering_and_extra_files(self):
        folder = Path(self.build()["alpha"]["path"])
        (folder / "summary.json").write_text("{}", encoding="utf-8")
        self.assertFalse(live_packets.verify(folder)["ok"])
        folder = Path(self.build_again()["beta"]["path"])
        (folder / "extra.txt").write_text("x", encoding="utf-8")
        self.assertIn("files_not_in_manifest", json.dumps(live_packets.verify(folder)))

    def test_refusals_leaks_unknown_members_bad_manifest_and_overwrite(self):
        with self.assertRaises(SystemExit):
            self.build(leak=True)
        for kw in ({"extra_member": "extra.bin"}, {"break_manifest": True}):
            with self.assertRaises(SystemExit):
                path = self.root / "bad.zip"
                path.write_bytes(battle(**kw))
                live_packets.build(path, {"alpha": A}, self.root / "bad")
        self.build_again()
        with self.assertRaises(SystemExit):
            self.build_again()


if __name__ == "__main__":
    unittest.main()
