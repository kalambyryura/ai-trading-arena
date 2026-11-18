"""R2.1.11 (team/platform/R2111_PLAN_UA.md, its check team/reviews/R2111_PLAN_CHECK_UA.md) on the fake exchange."""
from datetime import datetime
import io
from pathlib import Path
import json
import unittest
import zipfile
from urllib.parse import urlsplit

import test_order_outcomes as outcomes
from trading_lab.arena_strategies import ArenaError
from trading_lab.battle_export import create_archive, verify_archive
from trading_lab.battle_review import build_review
from trading_lab.exchange_api import venue_error

POSITIONS = "/api/v5/account/positions"


def at(event):
    return datetime.fromisoformat(event["at"]).timestamp()


class UnseenHold(outcomes.SeasonCase):
    """R219-UNSEEN-HOLD: after an unseen verdict no entry or add until a check that started after it has finished."""
    DURATION = 1800

    def test_unseen_hold_no_entry_in_the_verdict_step(self):
        marks = {}
        api = self.fx.lab.resources[self.state["id"]]["apis"][0]
        original = api.transport

        def slow(request):  # every private request takes 0.2 s: the verdict's check ends after it started
            self.fx.now += 0.2
            return original(request)
        api.transport = slow
        lab = self.fx.lab

        def ticking():  # the session clock moves on every read: the verdict comes before its check's end
            self.fx.now += 0.001
            return self.fx.now
        lab.monotonic = ticking
        starts, step = [], self.fx.lab.step

        def counted(state):  # the event seq at each step's start: which step an event belongs to
            starts.append(state.get("_event_seq", 0))
            return step(state)
        self.fx.lab.step = counted
        step_of = lambda event: sum(1 for b in starts if b < event["seq"])

        def act(t):
            live = [o for o in self.fake.orders.values() if o.get("ordType") == "post_only" and o["state"] == "live"]
            first = self.bot["_orders"][0] if self.bot["_orders"] else None
            if live and "drop" not in marks and first and (first.get("result") or {}).get("state") == "live":
                live[0].update(state="canceled", uTime=str(int(self.fx.now * 1000) - 1000))  # cancelled unfilled...
                self.fake.purge_after = 0.5  # ...and dropped from GET at once
                marks["drop"] = t

        entries = lambda: [e for e in self.events("exchange_order_intent") if e["data"]["request"]["reduceOnly"] is False]
        self.run_season(act, until=lambda: len(entries()) >= 2, limit=400)
        verdict = next(e for e in self.events("exchange_order_absent") if e["data"]["kind"] == "unseen")
        hold = self.events("entry_hold")
        self.assertEqual([e["data"]["reason"] for e in hold], ["unseen_hold"])
        self.assertLess(verdict["seq"], hold[0]["seq"])
        self.assertEqual(len(entries()), 2)  # the hold delays the next entry, it never forbids it
        self.assertGreater(step_of(entries()[1]), step_of(verdict))  # not in the verdict step: a check that started after it first
        self.assertNotIn("_unseen_hold", self.bot)



class A4Export(outcomes.SeasonCase):
    """The A4 export (D138 p. 3): a section in battle-review.json and a counter in manifest.json, only from the state of
    a journal started by R2.1.11."""
    TAKER = True

    def windows(self, count):
        api = self.fx.lab.resources[self.state["id"]]["apis"][0]
        original = api.transport
        failing = {"on": False}

        def transport(request):
            if urlsplit(request.full_url).path == POSITIONS and failing["on"]:
                raise venue_error("http_503", http_status=503, okx_code="50001")
            return original(request)
        api.transport = transport
        self.run_season(until=lambda: self.bot["position_btc"] != 0, limit=60)
        for _ in range(count):
            failing["on"] = True
            self.fx.lab.step(self.state); self.fx.now += 5
            failing["on"] = False
            self.fx.lab.step(self.state); self.fx.now += 5

    def test_a4_a_new_journal_exports_its_windows_and_the_counter(self):
        self.assertEqual(self.state["degraded_windows"], [])  # made by the fresh Start
        self.windows(2)
        bundle = build_review(self.state)
        self.assertEqual([w["outcome"] for w in bundle["degraded_windows"]], ["recovered", "recovered"])
        self.assertEqual({w["bot_id"] for w in bundle["degraded_windows"]}, {self.bot["id"]})
        self.assertEqual(bundle["degraded_windows"][0]["codes"], ["50001"])
        with create_archive(self.state) as stream:
            verify_archive(stream)
            stream.seek(0)
            with zipfile.ZipFile(stream) as archive:
                files = {n: archive.read(n) for n in archive.namelist()}
        self.assertEqual(json.loads(files["manifest.json"])["degraded_windows"], 2)
        manifest = json.loads(files["manifest.json"])
        manifest["degraded_windows"] = 3
        files["manifest.json"] = json.dumps(manifest).encode("utf-8")
        files.pop("manifest-receipt.json")  # a ZIP of the old layout: the counter is still checked
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        out.seek(0)
        with self.assertRaisesRegex(ArenaError, "Лічильник вікон"):
            verify_archive(out)

    def test_a4_a_journal_started_before_r2111_gets_no_section(self):
        self.state.pop("degraded_windows")  # as a Start on R2.1.10, then a Resume on R2.1.11
        self.windows(1)
        self.assertNotIn("degraded_windows", self.state)
        self.assertNotIn("degraded_windows", build_review(self.state))
        self.assertTrue(self.events("degraded_window_ended"))  # the events stay as they were



class G2Units(outcomes.SeasonCase):
    """G2: base_ccy, position_base, signed_fees_usdt and entries_total as new public bot fields and new frame keys."""
    TAKER = True

    def test_g2_frames_carry_the_neutral_names_and_export_them(self):
        from decimal import Decimal
        self.run_season(until=lambda: self.bot["position_btc"] != 0, limit=60)
        self.fake.bid, self.fake.ask = outcomes.PRICE * Decimal(".994"), outcomes.PRICE * Decimal(".994") + Decimal(".01")
        self.run_season(until=lambda: self.bot["position_btc"] == 0 and self.bot["trades"] >= 1, limit=60)
        bot = self.bot
        self.assertEqual((bot["base_ccy"], bot["entries"], bot["entries_total"]), ("ETH", 0, bot["filled_entry_orders"]))
        self.assertEqual(bot["entries_total"], 1)  # not reset after the close, as entries is
        self.assertLess(bot["signed_fees_usdt"], 0)  # a cost, with OKX's sign
        self.assertEqual(bot["signed_fees_usdt"], -bot["fees_usdt"])
        rows = [b for f in build_review(self.state)["frames"] for b in f["bots"] if b["id"] == bot["id"]]
        self.assertTrue(rows)
        held = [r for r in rows if r.get("position_base") not in (None, "0")]
        self.assertTrue(held and all(r["position_base"] == r["position_btc"] for r in held))
        self.assertEqual({r["base_ccy"] for r in rows if "base_ccy" in r}, {"ETH"})  # frames before the first rebuild: none
        self.assertNotIn("filled_entry_orders", rows[-1])  # the old key stays out of the export



class LiveDenylist(outcomes.SeasonCase):
    """E (D136 p. 5): the recipe of the D135 probe is refused for a fresh Live Start, before any request."""

    def test_e_the_list_names_the_probe_recipe(self):
        from pathlib import Path
        from trading_lab.arena_strategies import fingerprint
        from trading_lab.live_denylist import LIVE_START_DENIED
        recipe = json.loads((Path(__file__).resolve().parents[1] / "03_For_Client_Deliverables" / "LIVE_PROBE_ETH_POST_ONLY_LONG.json")
                            .read_text(encoding="utf-8"))
        self.assertIn(fingerprint(recipe), LIVE_START_DENIED)

    def test_e_a_retired_recipe_is_refused_for_a_live_start(self):
        from unittest.mock import patch
        lab = self.fx.lab
        retired = next(r for r in self.state["strategies"] if r["id"] != "cash")["recipe_sha256"]
        lab.stop(lab.data["generation"], self.state["id"])  # the first round ends: a new Live Start may come
        self.run_season(limit=40)
        self.assertEqual(self.state["status"], "DONE")
        before = len(self.fx.fakes["arena-b"].requests)
        with patch.dict("trading_lab.live_denylist.LIVE_START_DENIED", {retired: "technical probe (test)"}):
            with self.assertRaisesRegex(ArenaError, "знято з ужитку"):
                lab.start(lab.data["generation"], "arena_live", [self.promoted], ["arena-b"], 25, 300, execution_mode="rules_only")
        self.assertEqual(len(self.fx.fakes["arena-b"].requests), before)  # refused before any request


class ExampleOnly(outcomes.SeasonCase):
    """D141 p. 2 (RC2): a battle of examples only is marked beside the hashed bundle, in the history row and the review header."""

    def test_example_only_needs_every_participant_to_be_an_example(self):
        from trading_lab.league import example_only
        example = {"kind": "example", "name": "Codex"}
        self.assertTrue(example_only({"strategies": [{"id": "cash"}, {"id": "a", "author": example}, {"id": "b", "author": example}]}))
        self.assertFalse(example_only({"strategies": [{"id": "a", "author": example}, {"id": "b", "author": {"kind": "model"}}]}))
        self.assertFalse(example_only({"strategies": [{"id": "cash"}]}))  # no participant: nothing to mark
        self.assertFalse(example_only({}))

    def test_the_history_row_says_it_and_the_bundle_does_not(self):
        lab = self.fx.lab
        recipes = [r for r in self.state["strategies"] if r["id"] != "cash"]
        for kind in ("model", "example"):
            for recipe in recipes:
                recipe["author"] = {"kind": kind, "name": kind}
            self.assertIs(lab.history_row(self.state)["example_only"], kind == "example")
        self.assertNotIn("example_only", json.dumps(build_review(self.state), ensure_ascii=False))


class R2110Lows(unittest.TestCase):
    """The six low findings of team/reviews/R2110_REVIEW_UA.md (D141 p. 1) that need no running season."""

    def test_hash_cover_the_exporter_hash_covers_review_diagnostics(self):
        from trading_lab import battle_export
        self.assertIn("review_diagnostics.py", battle_export.EXPORTER_FILES)  # it shapes the bundle

    def test_hash_cover_execution_profile_names_the_code_of_its_process(self):
        from trading_lab.battle_review import safe_data
        from trading_lab.exchange_lab import LOADED_HASH, execution_profile
        state = {"rules": {}, "strategies": [], "protocol_version": 8}
        data = execution_profile(state, True)
        self.assertEqual(data["code_sha256"], LOADED_HASH)
        self.assertEqual(safe_data(data, event_code="execution_profile")["code_sha256"], LOADED_HASH)
        self.assertNotIn("code_sha256", safe_data({"code_sha256": "x" * 64}, event_code="execution_profile"))

    def test_allowlist_a_later_check_date_of_the_table_is_exported_too(self):
        from trading_lab.battle_review import safe_data
        for date in ("2026-11-04 12:30 UTC", "2027-01-02 03:04 UTC"):  # this release's and a later re-check
            self.assertEqual(safe_data({"transient_codes_checked_at": date}, event_code="execution_profile")["transient_codes_checked_at"], date)
        for bad in ("2026-11-04", "<b>2026-11-04 12:30 UTC</b>", 20261104):
            self.assertNotIn("transient_codes_checked_at", safe_data({"transient_codes_checked_at": bad}, event_code="execution_profile"))



class Labels(outcomes.SeasonCase):
    """R2110-LABELS (R2110_REVIEW:84), items 1-4."""
    TAKER = True

    def test_labels_1_a_local_fault_in_a_window_has_the_events_reason_among_its_codes(self):
        api = self.fx.lab.resources[self.state["id"]]["apis"][0]
        original = api.transport
        broken = {"on": False}

        def transport(request):
            if urlsplit(request.full_url).path == POSITIONS and broken["on"]:
                raise TypeError("fictional local fault")
            return original(request)
        api.transport = transport
        self.run_season(until=lambda: self.bot["position_btc"] != 0, limit=60)
        broken["on"] = True
        self.fx.lab.step(self.state); self.fx.now += 5
        broken["on"] = False
        self.fx.lab.step(self.state)
        failed = self.events("exchange_check_failed")[-1]["data"]["reason"]
        self.assertEqual(self.events("degraded_window_ended")[-1]["data"]["codes"], [failed])
        self.assertEqual(failed, "exchange_check_failed")  # R2.1.10 wrote exchange_process_failed in the window's codes

    def test_labels_4_no_stop_renewed_for_a_stopped_bot(self):
        lab = self.fx.lab
        lab.stop(lab.data["generation"], self.state["id"])
        self.run_season(limit=40)
        self.assertEqual(self.bot["status"], "STOPPED")
        from trading_lab import exchange_engine as engine
        before = len(self.events("stop_renewed"))
        engine.stop(self.state, self.fx.now, self.bot["id"], origin="owner")
        self.assertEqual(len(self.events("stop_renewed")), before)


class LabelsApi(unittest.TestCase):
    """R2110-LABELS items 2 and 3, on ExchangeAPI with a fake transport."""

    def api(self, answer):
        from trading_lab.exchange_api import ExchangeAPI
        from trading_lab.okx_readonly import Credentials
        clock = lambda: 1_800_000_000.0
        return ExchangeAPI("global", "demo", Credentials("FAKE_KEY", "FAKE_SECRET", "FAKE_PASS"), transport=answer,
                           clock=clock, monotonic=clock)

    def test_labels_2_a_refused_clock_sample_is_the_time_endpoints_failure(self):
        from unittest.mock import patch
        from trading_lab.okx_readonly import OkxReader, PreflightError
        api = self.api(lambda request: {"code": "0", "data": []})
        with patch.object(OkxReader, "calibrate_clock", side_effect=PreflightError("unreliable_clock_sample")):
            with self.assertRaises(PreflightError) as caught:
                api.get("/api/v5/account/positions", {"instType": "SWAP"}, private=True)
        self.assertEqual((caught.exception.endpoint, caught.exception.http_status), ("/api/v5/public/time", None))

    def test_labels_3_a_code_of_no_digits_without_data_is_an_invalid_envelope(self):
        from trading_lab.okx_readonly import PreflightError
        api = self.api(lambda request: {"code": "abc", "msg": "x"})
        with self.assertRaises(PreflightError) as caught:
            api.get("/api/v5/public/time")
        self.assertEqual(str(caught.exception), "invalid_provider_envelope")



class TwoPhaseGaps(outcomes.SeasonCase):
    """R219 test gaps: the second phase's time, and a Resume between the two phases of a resting entry's verdict."""
    DURATION = 1800

    def first_phase(self):
        from trading_lab import exchange_engine as engine
        self.fake.lose_entry = True  # lost before the venue: GET 51603, cancel -> nothing to cancel, position = own fills
        marked = lambda: [o for o in engine.unresolved(self.bot) if (o.get("_absent") or {}).get("nothing_at")]
        self.run_season(until=lambda: bool(marked()), limit=200)
        order = marked()[0]
        self.assertFalse(self.events("exchange_order_absent"))  # phase 1 marks only
        return order

    def test_the_second_phase_comes_at_a_later_check(self):
        order = self.first_phase()
        marked_at = order["_absent"]["nothing_at"]
        self.fx.now += 5  # the next step: its account check gives the verdict
        self.fx.lab.step(self.state)
        verdict = self.events("exchange_order_absent")[0]
        self.assertGreaterEqual(at(verdict) - marked_at, 5)  # the next account check, not the one that marked it
        self.assertFalse(self.bot["_manual_review"])

    def test_a_resume_between_the_phases_keeps_the_mark_and_ends_done(self):
        order = self.first_phase()
        lab = self.fx.lab
        lab.resources.pop(self.state["id"])["stack"].close()
        lab.resume(lab.data["generation"], self.state["id"])
        self.state = lab.sessions[self.state["id"]]
        self.bot = self.state["bots"][0]
        lab.prepare(self.state, lab.chosen_profiles(["arena-a"]), True)
        resumed = next(o for o in self.bot["_orders"] if o["request"]["clOrdId"] == order["request"]["clOrdId"])
        self.assertIn("nothing_at", resumed["_absent"])  # the mark is in the journal: it survives the Resume
        lab.resources[self.state["id"]]["wakeup"].wait = lambda seconds: False
        self.run_season(limit=60)
        self.assertEqual(self.state["status"], "DONE")
        self.assertTrue(self.events("exchange_order_absent"))
        self.assertFalse(self.bot["_manual_review"])


class ExportFilters(unittest.TestCase):
    """R2110-TESTS: the export filters whose mutants lived (check p. 14)."""

    def test_window_codes_endpoints_and_statuses_keep_only_their_allowlists(self):
        from trading_lab.battle_review import safe_data
        data = {"outcome": "recovered", "codes": ["50001", "exchange_check_failed", "rm -rf", 50001, "okx_error_51290"],
                "endpoints": ["/api/v5/account/positions", "/api/v5/account/positions?ordId=1", "/api/v5/other"],
                "http_statuses": [503, 200, 99, "500", 600]}
        view = safe_data(data, event_code="degraded_window_ended")
        self.assertEqual(view["codes"], ["50001", "exchange_check_failed", "okx_error_51290"])
        self.assertEqual(view["endpoints"], ["/api/v5/account/positions"])
        self.assertEqual(view["http_statuses"], [503, 200])

    def test_step_timing_counters_extra_permissions_and_order_state(self):
        from trading_lab.battle_review import safe_data
        timing = safe_data({"steps": 3, "account_requests": [5, "x", 7, 1, 2, 3, 4, 5], "account_writes": [1],
                            "calibration_rtt_ms": [[100.5, "bad"], "x"]}, event_code="step_timing")
        self.assertEqual(timing["account_requests"], ["5", None, "7", "1", "2", "3"])  # at most 6 bots, numbers only
        self.assertEqual(timing["calibration_rtt_ms"], [["100.5", None], []])
        permissions = safe_data({"extra_permissions": ["withdraw", "trade", "read_only", "<x>"]}, event_code="account_checked")
        self.assertEqual(permissions["extra_permissions"], ["withdraw"])
        for state, kept in (("filled", True), ("partially_filled", True), ("mmp_canceled", True), ("effective", False), ("<x>", False)):
            view = safe_data({"order_state": state}, event_code="exchange_entry_fill")
            self.assertEqual("order_state" in view, kept, state)


class RetentionTool(unittest.TestCase):
    """R219 test gap: an unknown --profile in check_order_retention names the profiles by ID only."""

    def test_unknown_profile_is_named_with_ids_only(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        import check_order_retention as tool
        import test_exchange_lab as fixture
        fx = fixture.ExchangeTests(methodName="runTest")
        fx.setUp()
        try:
            with self.assertRaises(SystemExit) as caught:
                tool.check(fx.root, "nope", "0" * 32, transport=lambda request: self.fail("no request for an unknown profile"))
            text = str(caught.exception)
            self.assertIn("nope", text)
            self.assertIn("demo", text)
            for profile in tool.profiles(fx.root):  # IDs only: never a label or a settings file
                self.assertNotIn(profile["settings_file"], text)
        finally:
            fx.tearDown()


if __name__ == "__main__":
    unittest.main()
