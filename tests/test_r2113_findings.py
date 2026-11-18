"""R2.1.13: what the R2.1.12 reviews left open for the mixed season (team/reviews/R2112_REVIEW_UA.md, R2112_RC2_REVIEW_UA.md):
X1-03, X2-01, X2-03, X2-04, X2-07, X2-09, X3-02 and the rest of X3-07. Strict fakes and temp roots only."""
from copy import deepcopy
import hashlib
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch
import unittest
import zipfile

import test_r2112_mix as mix
from test_league_evidence_integration import FakeController
from test_league_public_table import battle as standing_row
from test_public_site import approved
from trading_lab import exchange_engine as engine, exchange_lab
from trading_lab.arena_strategies import ArenaError
from trading_lab.battle_debrief import build_debrief, load_debrief, render_html
from trading_lab.battle_export import create_archive, verify_archive
from trading_lab.battle_review import build_review
from trading_lab.event_replay import build_packet, verify_packet
from trading_lab.execution_sensitivity import analyze_archive
from trading_lab.league import League, instruments_mark, public_standings, score
from trading_lab.match_journal import append_events, checkpoint, restore_events
from trading_lab.public_site import battle_item, build as build_site, digest, leak_scan, public_review, verify_public_review

BTC, ETH = mix.BTC, mix.ETH
ROOT = Path(__file__).resolve().parents[1]
R2112_ZIP = Path(__file__).parent / "fixtures" / "r2112_mixed_season.zip"  # a fake mixed season exported by the R2.1.12 code (8fa65ad), byte for byte


def retag(payload, change):
    """The ZIP with `change({name: bytes})` applied, its manifest and receipt signed anew, as a forger would."""
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    change(members)
    manifest = json.loads(members["manifest.json"])
    manifest["files"] = [{"path": n, "bytes": len(members[n]), "sha256": hashlib.sha256(members[n]).hexdigest()}
                         for n in sorted(members) if n not in {"manifest.json", "manifest-receipt.json"}]
    members["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    receipt = json.loads(members["manifest-receipt.json"])
    receipt.update(manifest_bytes=len(members["manifest.json"]), manifest_sha256=hashlib.sha256(members["manifest.json"]).hexdigest())
    members["manifest-receipt.json"] = json.dumps(receipt).encode("utf-8")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return output.getvalue()


def edit_manifest(change):
    def apply(members):
        manifest = json.loads(members["manifest.json"])
        change(manifest)
        members["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    return apply


class MixedEvidence(unittest.TestCase):
    setUp = mix.MixedSeason.setUp
    start = mix.MixedSeason.start
    steps = mix.MixedSeason.steps
    events = mix.MixedSeason.events
    finish = mix.MixedSeason.finish
    feed_api = mix.MixedSeason.feed_api
    break_ticker = mix.MixedSeason.break_ticker

    def journal(self):
        lab = self.fx.lab
        full = deepcopy(lab.load(self.state["id"]))
        restore_events(lab.path / (full["id"] + ".events.jsonl"), full, full=True)
        return full

    def archive(self, full=None):
        full = full or self.journal()
        with create_archive(full, snapshots_path=engine.snapshot_files(self.fx.lab.path, full)) as stream:
            return stream.read()

    def traded_season(self):
        self.start()
        self.steps(80, until=lambda: self.b["position_btc"] and self.e["position_btc"])
        self.assertTrue(self.b["position_btc"] and self.e["position_btc"])
        self.finish()
        self.assertEqual(self.state["status"], "DONE")

    # ---- X2-01 ----
    def test_a_contract_down_from_the_first_step_never_borrows_the_other_contracts_snapshots(self):
        self.start()
        self.break_ticker(ETH)
        self.steps(40, until=lambda: self.b["position_btc"])
        self.steps(4)
        self.finish()
        full = self.journal()
        with zipfile.ZipFile(io.BytesIO(self.archive(full))) as archive:
            review = json.loads(archive.read("battle-review.json"))
        self.assertEqual(review["diagnostics_context"], {"market_snapshots": "NOT_RECORDED"})  # BTC has rows, ETH none
        owners = {e["seq"]: e.get("bot_id") for e in full["events"]}
        eth_gaps = [g for g in review["market_gaps"] if g["instrument"] == ETH]
        self.assertTrue(eth_gaps)
        for gap in eth_gaps:
            for end in ("before", "recovery"):
                point = gap[end]
                self.assertTrue(point is None or owners.get(point.get("event_seq")) == self.e["id"], (gap["event_seq"], end, point))

    def test_recorded_only_when_every_contract_has_quotes(self):
        self.traded_season()
        with zipfile.ZipFile(io.BytesIO(self.archive())) as archive:
            self.assertEqual(json.loads(archive.read("battle-review.json"))["diagnostics_context"], {"market_snapshots": "RECORDED"})

    # ---- X2-03, X2-04: the verifier of a mixed ZIP ----
    def test_verify_archive_refuses_what_a_forger_can_change_in_a_mixed_zip(self):
        self.traded_season()
        payload = self.archive()
        self.assertTrue(verify_archive(io.BytesIO(payload))["verified"])

        def swap(members):  # the counters swapped too: only the rows' own instrument can tell
            a, b = "market." + BTC + ".jsonl", "market." + ETH + ".jsonl"
            members[a], members[b] = members[b], members[a]
            manifest = json.loads(members["manifest.json"])
            for field in ("market_samples_by_instrument", "market_rows_omitted_by_instrument"):
                manifest[field][BTC], manifest[field][ETH] = manifest[field][ETH], manifest[field][BTC]
            members["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")

        def drop(members):
            del members["market." + ETH + ".jsonl"]

        def bump(field, key=BTC):
            return edit_manifest(lambda m: m[field].__setitem__(key, m[field][key] + 1))
        floating = edit_manifest(lambda m: m["market_samples_by_instrument"].__setitem__(BTC, float(m["market_samples_by_instrument"][BTC])))
        no_omitted = edit_manifest(lambda m: m.pop("market_rows_omitted_by_instrument"))

        def negative_with_the_same_sum(manifest):
            manifest["market_rows_omitted_by_instrument"][BTC] -= 1
            manifest["market_rows_omitted_by_instrument"][ETH] += 1
        negative = edit_manifest(negative_with_the_same_sum)
        for name, change in (("swapped members", swap), ("a contract's member missing", drop), ("samples + 1", bump("market_samples_by_instrument")),
                             ("omitted + 1", bump("market_rows_omitted_by_instrument")), ("16.0 as a counter", floating),
                             ("no omitted counters", no_omitted), ("negative counter", negative)):
            with self.subTest(name), self.assertRaises(ArenaError):
                verify_archive(io.BytesIO(retag(payload, change)))
        self.assertTrue(verify_archive(io.BytesIO(retag(payload, lambda members: None)))["verified"])  # the signing itself is sound

    def test_every_market_row_of_a_mixed_zip_names_its_contract(self):
        self.traded_season()
        with zipfile.ZipFile(io.BytesIO(self.archive())) as archive:
            for contract in (BTC, ETH):
                rows = [json.loads(line) for line in archive.read("market." + contract + ".jsonl").splitlines()]
                self.assertTrue(rows and all(r["instrument"] == contract for r in rows), contract)

    def test_a_sparse_mixed_season_writes_markets_only_on_the_frames_it_adds(self):
        lab = self.fx.lab
        result = lab.start(lab.data["generation"], "arena_live", self.ids, ["arena-a", "arena-b"], 100, 7200, execution_mode=mix.policy.RULES_ONLY)
        state = lab.load(result["id"])
        state.update(fixture_only=True, evidence_kind="SYNTHETIC")
        lab.prepare(state, lab.chosen_profiles(["arena-a", "arena-b"]), False)
        self.state, (self.b, self.e) = state, state["bots"][:2]
        self.steps(30)
        frames = state["frames"]
        self.assertLess(len(frames), 30)  # a 2 h window keeps every n-th frame
        self.assertTrue(frames[1:] and all(f["market"] is None and sorted(f["markets"]) == [BTC, ETH] for f in frames[1:]))  # the first is the start's

    def test_the_review_header_says_mixed_for_a_mixed_season(self):
        from trading_lab.arena import Arena
        from trading_lab.exchange_lab import ExchangeLab
        from trading_lab.web_panel import Panel, Server
        self.traded_season()
        full = self.journal()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            no_call = lambda *a, **kw: self.fail("No exchange, market, model or process calls")
            arena = Arena(root, background=False, feed_factory=no_call)
            factory = lambda r, w, a: ExchangeLab(r, w, a, background=False, api_factory=no_call, feed_factory=no_call, jev_factory=no_call)
            server = Server(Panel(root, launcher=no_call), 0, arena=arena, exchange_factory=factory)
            append_events(server.exchange.path / (full["id"] + ".events.jsonl"), full, 0)
            saved = checkpoint(full)
            server.exchange.load = lambda identity, **kwargs: deepcopy(saved)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for route, season, expected in (("review", "MIXED", "MIXED"), ("replay", "MIXED", "MIXED"), ("review", "SOL-USDT-SWAP", "SOL-USDT-SWAP")):
                    saved["rules"]["instrument"] = season  # the header reads the journal's rules; a SOL season is named, not defaulted to BTC
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                    try:
                        connection.request("GET", "/api/" + route + "/exchange/" + saved["id"])
                        response = connection.getresponse()
                        response.read()
                        self.assertEqual((route, response.getheader("X-Season-Instrument")), (route, expected))
                    finally:
                        connection.close()
            finally:
                server.shutdown()
                server.server_close()
                arena.close()

    # ---- X2-07 ----
    def outage_journal(self):
        self.start()
        self.steps(2)
        self.break_ticker(ETH)
        self.steps(4)
        self.finish()
        return self.journal()

    def test_the_replay_packet_names_whose_market_failed(self):
        packet = build_packet(self.outage_journal())
        verify_packet(packet)
        quality = [e["quality"] for e in packet["events"] if (e.get("quality") or {}).get("reason") == "market_unavailable"]
        self.assertTrue(quality and all(q["instrument"] == ETH for q in quality))

    def test_the_debrief_names_the_contract_of_a_gap(self):
        full = self.outage_journal()
        html = render_html(build_debrief(build_review(full), {c: [] for c in (BTC, ETH)}))
        self.assertIn("· " + ETH, html)

    def test_public_events_name_their_contract_and_the_verifier_holds_it_to_the_match(self):
        dto = public_review(build_review(self.outage_journal()))
        verify_public_review(dto)
        named = [e for e in dto["events"] if e["code"] == "market_unavailable"]
        self.assertTrue(named and all(e["data"]["instrument"] == ETH for e in named))
        forged = deepcopy(dto)
        next(e for e in forged["events"] if e["data"].get("instrument"))["data"]["instrument"] = "SOL-USDT-SWAP"
        forged["integrity"]["review_sha256"] = digest({k: v for k, v in forged.items() if k != "integrity"})
        with self.assertRaises(ArenaError):
            verify_public_review(forged)

    # ---- X2-09 ----
    def test_the_sensitivity_analysis_reads_a_mixed_zip(self):
        self.traded_season()
        report = analyze_archive(self.archive())
        self.assertTrue(report)

    # ---- X1-03 ----
    def test_the_v2_transition_sees_only_its_own_contracts_block(self):
        state = self.start()
        seen = []
        original = exchange_lab.runtime.advance

        def recording(st, bot, recipe, snapshot, emit):
            seen.append((bot["id"], st.get("admission_blocked")))
            return original(st, bot, recipe, snapshot, emit)
        state["admission_blocked_by_instrument"] = {BTC: "test_block"}
        with patch.object(exchange_lab.runtime, "advance", recording):
            self.steps(2)
        self.assertEqual({v for i, v in seen if i == self.b["id"]}, {"test_block"})
        self.assertEqual({v for i, v in seen if i == self.e["id"]}, {None})
        self.assertNotIn("admission_blocked", state)

    def test_a_contract_blocked_between_decision_and_order_is_named_in_the_cancellation(self):
        state = self.start()
        lab = self.fx.lab
        real = lab.fresh_snapshot

        def blocking(st, resource, candidates, snapshot, contract=None):
            if contract == ETH:  # the ETH entry has passed its decision; its recheck is next
                st["admission_blocked_by_instrument"] = {ETH: "test_block"}
            return real(st, resource, candidates, snapshot, contract)
        with patch.object(lab, "fresh_snapshot", blocking):
            self.steps(80, until=lambda: any(e["code"] == "entry_cancelled" and e["bot_id"] == self.e["id"] for e in state["events"]))
        cancelled = next(e for e in state["events"] if e["code"] == "entry_cancelled" and e["bot_id"] == self.e["id"])
        self.assertIn("admission_blocked", cancelled["data"]["reasons"])

    def test_the_book_does_no_work_under_its_own_contracts_block(self):
        state = self.start()
        feed = self.fx.lab.resources[state["id"]]["feeds"][ETH]
        self.assertTrue(feed.admitting())
        state["admission_blocked_by_instrument"] = {BTC: "test_block"}
        self.assertTrue(feed.admitting())  # another contract's block
        state["admission_blocked_by_instrument"] = {ETH: "test_block"}
        self.assertFalse(feed.admitting())

    # ---- R2113-VERIFY-OLD-MIXED ----
    def test_a_member_with_the_contract_on_only_some_rows_is_refused(self):
        self.traded_season()
        payload = self.archive()

        def strip_one(members):
            name = "market." + ETH + ".jsonl"
            lines = members[name].splitlines()
            row = json.loads(lines[-1])
            del row["instrument"]
            lines[-1] = json.dumps(row, ensure_ascii=False).encode("utf-8")
            members[name] = b"\n".join(lines) + b"\n"

        def wrong_one(members):
            name = "market." + ETH + ".jsonl"
            lines = members[name].splitlines()
            row = json.loads(lines[0])
            row["instrument"] = BTC
            lines[0] = json.dumps(row, ensure_ascii=False).encode("utf-8")
            members[name] = b"\n".join(lines) + b"\n"
        for change in (strip_one, wrong_one):
            with self.subTest(change.__name__), self.assertRaises(ArenaError):
                verify_archive(io.BytesIO(retag(payload, change)))

    # ---- X3-02, X3-07 ----
    def test_the_league_shows_the_mark_of_a_mixed_cohort(self):
        self.traded_season()
        full = self.journal()
        with tempfile.TemporaryDirectory() as directory:
            arena = FakeController(Path(directory) / "paper")
            exchange = FakeController(Path(directory) / "exchange", [full], kind="runs")
            status = League(directory, arena, exchange).status()
        self.assertEqual(status["battles"][0]["cohort_mark"], "змішаний: BTC ×1 + ETH ×1")
        if status["battles"][0]["competitive_validity"] == "ELIGIBLE":
            self.assertEqual(status["cohorts"][0]["cohort_mark"], "змішаний: BTC ×1 + ETH ×1")

    def test_an_incomplete_mixed_battle_is_scored_ineligible(self):
        self.traded_season()
        full = self.journal()
        full["incomplete"] = True
        result = score(build_review(full))
        self.assertEqual(result["status"], "INELIGIBLE")
        self.assertIsNone(result["sole_winner"])

    def test_the_public_site_keeps_the_mark_and_stays_clean(self):
        self.traded_season()
        full = self.journal()
        with tempfile.TemporaryDirectory() as directory:
            item = battle_item("exchange", full, approved(build_packet(full)), now_ms=100)
            self.assertEqual(item["row"]["cohort_mark"], "змішаний: BTC ×1 + ETH ×1")
            verify_public_review(item["review"])
            site = Path(directory) / "site"
            build_site(site, ROOT / "web", [item], now_ms=100, derive=False)
            self.assertTrue(leak_scan(site)["clean"])

    def test_the_cohort_row_carries_the_mark_only_for_a_mixed_battle(self):
        rows = [{**standing_row("b1", "2026-10-06T10:00:00+00:00", {"aa": "0.01"}, sole="aa"), "cohort_mark": "змішаний: BTC ×1 + ETH ×1"},
                standing_row("b2", "2026-10-07T10:00:00+00:00", {"aa": "0.01"}, sole="aa", cohort="d" * 64)]
        _, cohorts = public_standings(rows)
        marks = {c["cohort"]: c.get("cohort_mark") for c in cohorts}
        self.assertEqual(marks, {"c" * 64: "змішаний: BTC ×1 + ETH ×1", "d" * 64: None})
        self.assertIsNone(instruments_mark({"rules": {"instrument": ETH}}))


class MixedZipOfR2112(unittest.TestCase):
    """The first mixed season is played on R2.1.12: its ZIP must pass what coord and the review run from main."""
    def rows(self):
        with zipfile.ZipFile(R2112_ZIP) as archive:
            return {n: [json.loads(line) for line in archive.read(n).splitlines()] for n in archive.namelist() if n.startswith("market.")}

    def test_the_fixture_is_what_it_says(self):
        rows = self.rows()
        self.assertEqual(sorted(rows), ["market." + BTC + ".jsonl", "market." + ETH + ".jsonl"])
        self.assertTrue(all(lines for lines in rows.values()))
        self.assertFalse(any("instrument" in row for lines in rows.values() for row in lines))  # R2.1.12 did not write it

    def test_the_standard_checks_accept_it(self):
        self.assertTrue(verify_archive(R2112_ZIP)["verified"])
        self.assertTrue(load_debrief(R2112_ZIP))
        self.assertTrue(analyze_archive(R2112_ZIP.read_bytes()))

    def test_one_row_with_a_contract_makes_it_no_longer_a_zip_of_r2112(self):
        payload = R2112_ZIP.read_bytes()

        def add_one(members):
            name = "market." + BTC + ".jsonl"
            lines = members[name].splitlines()
            lines[0] = json.dumps({**json.loads(lines[0]), "instrument": BTC}, ensure_ascii=False).encode("utf-8")
            members[name] = b"\n".join(lines) + b"\n"
        with self.assertRaises(ArenaError):
            verify_archive(io.BytesIO(retag(payload, add_one)))


if __name__ == "__main__":
    unittest.main()
