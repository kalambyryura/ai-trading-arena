from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import http.client
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from trading_lab.arena_strategies import ArenaError
from trading_lab.battle_review import build_review, digest
from trading_lab.battle_debrief import build_debrief, check_journal_capacity, load_debrief, recorded_quotes, render_html, _chart
from tests import test_battle_review as review_fixtures

BASE = datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp()


def seal(bundle):
    bundle.pop("integrity", None)
    bundle["integrity"] = {"bundle_sha256": digest(bundle), "source_event_count": len(bundle["events"])}
    return bundle


def fixture(side="long", closed=True):
    def stamp(offset):
        return datetime.fromtimestamp(BASE + offset, timezone.utc).isoformat()
    rows = []
    specs = [(2, "entry", "1", "100", "0", "-.01", "1"),
             (4, "add", "1", "110", "0", "-.011", "2"),
             (5, "close", "1", "120", "15", "-.012", "1")]
    if closed:
        specs.append((8, "close", "1", "105", "0", "-.0105", "0"))
    events = [{"seq": i, "at": stamp(i), "bot_id": "human", "code": "hold", "data": {}} for i in range(1, 10)]
    events[0].update(code="entry_checks", data={"local_checks_passed": True, "checks": [
        {"feature": "return_5m_bps", "actual": "30", "expected": "20", "op": "gt", "passed": True}]})
    events[2].update(code="position_checks", data={"checks": []})
    events[6].update(code="exit_trigger", data={"exit_reason": "formal_exit", "exit_checks": []})
    for seq, role, quantity, price, gross, fee, remaining in specs:
        rows.append({"seq": seq, "at": stamp(seq), "role": role, "quantity": quantity,
                     "order": "order-" + str(seq), "position_before": "0" if seq == 2 else "2" if seq == 5 else "1",
                     "position_after": remaining, "source": "paper_incremental_btc", "price_usdt": price,
                     "gross_pnl_usdt": gross, "signed_fees_usdt": fee})
        events[seq - 1].update(code="paper_entry_fill" if role in {"entry", "add"} else "paper_close_fill",
                               data={"side": side, "price_usdt": price, "quantity_btc": quantity})
    rows.append({"seq": 6, "at": stamp(6), "role": "funding", "signed_funding_usdt": "-.005", "source": "paper_model"})
    events[5].update(code="model_funding", data={"signed_amount_usdt": "-.005"})
    fees = sum(Decimal(r["signed_fees_usdt"]) for r in rows if "signed_fees_usdt" in r)
    costs = {"gross_pnl_usdt": "15", "signed_fees_usdt": str(fees), "signed_funding_usdt": "-.005",
             "net_usdt": str(Decimal(15) + fees - Decimal(".005")), "mark_pnl_usdt": "14.9",
             "complete": closed, "source": "paper_model_events", "jev": {"known_cost_usd": "0", "complete": True}}
    trip = {"number": 1, "entry_seq": 2, "entry_at": stamp(2), "fill_seqs": [s[0] for s in specs], "closed": closed}
    if closed:
        trip.update(close_seq=8, close_at=stamp(8))
    participant = {"id": "human", "name": "Юрій", "version": 1, "status": "DONE" if closed else "LONG",
                   "position_btc": "0" if closed else "1",
                   "costs": costs, "funnel": {"failed_features": {"return_5m_bps": 2}, "entry_filled_orders": 1},
                   "quality": {"data_status": "COMPLETE", "settlement": {"status": "SETTLED" if closed else "PENDING"},
                               "availability": {"status": "COMPLETE"}, "gap_event_seqs": []},
                   "trade_ledger": {"rows": sorted(rows, key=lambda r: r["seq"]), "round_trips": [trip], "inventory_known": True,
                                    "remaining": "0" if closed else "1", "missing_order_results": False, "unresolved": False}}
    return seal({"schema_version": 1, "match": {"id": "fictional-debrief", "mode": "PAPER",
                 "status": "DONE" if closed else "RUNNING", "started_at": stamp(0), "incomplete": False},
                 "events": events, "frames": [{"at": stamp(i), "bots": [{"id": "human", "pnl_usdt": str(i)}]} for i in range(1, 10)],
                 "participants": [participant], "history_complete": True, "projection": "author_equal_v1_allowlist"})


class DebriefTests(unittest.TestCase):
    def test_generic_stop_does_not_claim_owner_action(self):
        source = fixture()
        source["events"][6].update(code="stop_requested", data={})
        trade = build_debrief(seal(source))["participants"][0]["trades"][0]
        self.assertEqual(trade["exit_reason"], "stop_requested")
        self.assertEqual(trade["exit_evidence"]["seq"], 7)
        source["events"][7]["data"]["reason"] = "owner_stop"
        self.assertEqual(build_debrief(seal(source))["participants"][0]["trades"][0]["exit_reason"], "owner_stop")

    def test_unresolved_order_is_not_no_entry(self):
        state = review_fixtures.state_for()
        state.update(mode="OKX_DEMO", budget_usdt=100)
        state["bots"][0]["_orders"] = [{"request": {"clOrdId": "pending", "reduceOnly": False, "side": "buy"}, "result": None}]
        review = build_review(state)
        self.assertEqual(build_debrief(review)["participants"][0]["entry_state"], "UNKNOWN")
        state["bots"][0]["_orders"] = []
        state["bots"][0]["position_btc"] = ".001"
        self.assertEqual(build_debrief(build_review(state))["participants"][0]["entry_state"], "UNKNOWN")

    def test_cycle_number_is_not_html(self):
        source = fixture()
        source["participants"][0]["trade_ledger"]["round_trips"][0]["number"] = '<style>footer{display:none}</style>'
        with self.assertRaises(ArenaError):
            build_debrief(seal(source))

    def test_extreme_decimal_exponents_are_rejected_before_upstream_formatting(self):
        for value in ("1e1000000000", "1e-1000000000", "NaN", True):
            source = fixture()
            source["participants"][0]["costs"]["net_usdt"] = value
            with self.assertRaises(ArenaError):
                build_debrief(seal(source))

    def test_export_history_bound_checked_before_full_restore(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"fixture.events.jsonl"
            with self.assertRaises(ArenaError):
                check_journal_capacity(path, {"_journal_checkpoint_seq": 100001})
            path.with_suffix(".segments").mkdir()
            segment = path.with_suffix(".segments")/"segment-00000001.jsonl"
            with segment.open("wb") as stream:
                stream.truncate(64*1024**2+1)
            with self.assertRaises(ArenaError):
                check_journal_capacity(path, {"_journal_checkpoint_seq": 10})

    def test_decimation_preserves_real_gaps_in_both_charts(self):
        for gap in (0, 300):
            with self.subTest(gap=gap):
                source = fixture(closed=False)
                times = [BASE + 2 + i*30 + (gap if i >= 1250 else 0) for i in range(2500)]
                source["events"][-1]["at"] = datetime.fromtimestamp(times[-1], timezone.utc).isoformat()
                source["frames"] = [{"at": at, "bots": [{"id": "human", "pnl_usdt": str(i)}]} for i, at in enumerate(times)]
                quotes = [{"at": at, "mid": str(100+i/1000)} for i, at in enumerate(times)]
                bot = build_debrief(seal(source), quotes)["participants"][0]
                for chart in (bot["pnl_curve"], bot["trades"][0]["price_path"]):
                    svg = _chart(chart["points"], "test")
                    self.assertEqual(svg.count("M"), 1 if gap == 0 else 2)
                    self.assertLessEqual(len(chart["points"]), 500)

    def test_real_review_finality_uses_existing_quality_contract(self):
        report = build_debrief(build_review(review_fixtures.ReviewTests().fixture()))
        self.assertEqual(report["participants"][1]["result_state"], "FINAL")

    def test_partial_add_roundtrip_costs_and_bounded_price_path(self):
        source = fixture()
        before = deepcopy(source)
        quotes = [{"at": BASE + i, "mid": p} for i, p in [(0, 1000), (2, 100), (3, 120), (4, 90), (8, 105), (9, 2000)]]
        report = build_debrief(source, quotes)
        bot = report["participants"][0]
        trip = bot["trades"][0]
        self.assertEqual(trip["observed_realized_net_usdt"], "14.9515")
        self.assertEqual(len(trip["fills"]), 4)
        self.assertEqual(trip["entry_evidence"]["seq"], 1)
        self.assertEqual(trip["exit_evidence"]["seq"], 7)
        self.assertEqual(trip["price_path"]["best_observed_bps"], "2000.0")
        self.assertEqual(trip["price_path"]["worst_observed_bps"], "-1000.0")
        self.assertEqual(trip["price_path"]["samples"], 4)
        self.assertEqual(bot["reconciliation_deltas"]["net_usdt"], "0.0000")
        self.assertEqual(source, before)
        sensitivity = bot["cost_sensitivity"]
        self.assertEqual(sensitivity["status"], "COMPLETE_FIXED_LEDGER")
        self.assertEqual(Decimal(sensitivity["scenarios"][2]["additional_cost_usdt"]), Decimal(".2175"))
        self.assertEqual(Decimal(sensitivity["scenarios"][2]["recorded_net_after_overlay_usdt"]), Decimal("14.734"))
        self.assertIn("Чутливість до додаткових витрат", render_html(report))

    def test_short_direction_is_reversed_without_changing_observed_accounting(self):
        report = build_debrief(fixture("short"), [{"at": BASE + 2, "mid": "100"}, {"at": BASE + 3, "mid": "90"}])
        path = report["participants"][0]["trades"][0]["price_path"]
        self.assertEqual(Decimal(path["best_observed_bps"]), Decimal(1000))
        self.assertEqual(Decimal(path["worst_observed_bps"]), Decimal(0))

    def test_actual_partial_cumulative_fees_and_missing_market_stay_explicit(self):
        bundle = build_review(review_fixtures.ReviewTests().fixture())
        report = build_debrief(bundle)
        bot = report["participants"][1]
        self.assertEqual(len(bot["trades"]), 1)
        self.assertEqual(bot["trades"][0]["observed_fees_usdt"], "-0.020")
        self.assertEqual(bot["trades"][0]["side"], "unknown")
        self.assertIsNone(bot["trades"][0]["price_path"]["best_observed_bps"])
        self.assertEqual(bot["reconciliation_deltas"]["net_usdt"], "-0.001")
        self.assertNotIn("PRIVATE_", json.dumps(report))

    def test_open_incomplete_no_entry_and_unknown_are_not_final_results(self):
        bundle = fixture(closed=False)
        bot = build_debrief(bundle)["participants"][0]
        self.assertEqual(bot["result_state"], "PROVISIONAL_OR_UNKNOWN")
        self.assertFalse(bot["trades"][0]["closed"])
        self.assertIsNone(bot["reconciliation_deltas"]["net_usdt"])
        bundle = fixture()
        bundle["history_complete"] = False
        bot = build_debrief(seal(bundle))["participants"][0]
        self.assertEqual(bot["result_state"], "PROVISIONAL_OR_UNKNOWN")
        empty = build_debrief(build_review(review_fixtures.ReviewTests().fixture()))["participants"][0]
        self.assertEqual(empty["entry_state"], "NO_ENTRY")
        self.assertEqual(empty["trades"], [])

    def test_tamper_bad_quotes_and_wrong_bot_evidence_fail_closed(self):
        source = fixture()
        source["participants"][0]["costs"]["net_usdt"] = "1000"
        with self.assertRaises(ArenaError):
            build_debrief(source)
        with self.assertRaises(ArenaError):
            build_debrief(fixture(), [{"at": BASE, "mid": "NaN"}])
        with self.assertRaises(ArenaError):
            build_debrief(fixture(), [{"at": BASE + 3, "mid": 100}, {"at": BASE + 2, "mid": 101}])
        source = fixture()
        source["events"][1]["bot_id"] = "another-author"
        with self.assertRaises(ArenaError):
            build_debrief(seal(source))

    def test_html_escapes_prose_has_no_script_and_survives_missing_path(self):
        source = fixture()
        source["participants"][0]["name"] = '<script>alert("x")</script>'
        report = build_debrief(seal(source))
        html = render_html(report)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("Content-Security-Policy", html)
        self.assertIn("недостатньо записаних", html)
        report["participants"][0]["name"] = "tampered"
        with self.assertRaises(ArenaError):
            render_html(report)

    def test_offline_cli_preserves_existing_output_and_source(self):
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp) / "review.json", Path(temp) / "report.html"
            source.write_text(json.dumps(fixture()), encoding="utf-8")
            self.assertEqual(load_debrief(source)["schema"], "jev-battle-debrief/v1")
            output.write_text("existing", encoding="utf-8")
            command = [sys.executable, "tools/battle_debrief.py", str(source), "--output", str(output)]
            result = subprocess.run(command, capture_output=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output.read_text(), "existing")
            output.unlink()
            result = subprocess.run(command, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Кожна угода", output.read_text(encoding="utf-8"))

    def test_recorded_quotes_do_not_read_after_snapshot_watermark(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "record.jsonl"
            rows = [{"at": BASE + i, "raw": {"ticker": [{"bidPx": str(100+i), "askPx": str(102+i)}]}}
                    for i in range(4)]
            path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            quotes = recorded_quotes(path, BASE+1)
            self.assertEqual([q["mid"] for q in quotes], ["101", "102"])
            with self.assertRaises(ArenaError):
                recorded_quotes(path, None)

    def test_zip_import_keeps_actual_units_and_rejects_tampering(self):
        from tests.test_server_delivery import battle_fixture, rewritten_zip
        from trading_lab.battle_export import create_archive
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "battle.zip"
            with create_archive(battle_fixture()) as output:
                path.write_bytes(output.read())
            report = load_debrief(path)
            self.assertEqual(report["match"]["mode"], "OKX_LIVE")
            self.assertEqual(report["participants"][1]["trades"][0]["fills"][0]["source"], "exchange_incremental_contracts")
            with rewritten_zip(io.BytesIO(path.read_bytes()), {"battle-review.json": b"{}"}) as changed:
                path.write_bytes(changed.read())
            with self.assertRaises(ArenaError):
                load_debrief(path)

    def test_http_debrief_is_read_only_bounded_download_on_temporary_server(self):
        from tests.test_server_delivery import battle_fixture
        from trading_lab.arena import Arena
        from trading_lab.exchange_lab import ExchangeLab
        from trading_lab.match_journal import append_events, checkpoint
        from trading_lab.web_panel import Panel, Server
        state = battle_fixture()
        state["protocol_version"] = 8
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            no_call = lambda *a, **k: self.fail("No exchange, market, model or process calls")
            arena = Arena(root, background=False, feed_factory=no_call)
            factory = lambda r, w, a: ExchangeLab(r, w, a, background=False,
                                                 api_factory=no_call, feed_factory=no_call, jev_factory=no_call)
            server = Server(Panel(root, launcher=no_call), 0, arena=arena, exchange_factory=factory)
            append_events(server.exchange.path / (state["id"] + ".events.jsonl"), state, 0)
            saved = checkpoint(state)
            server.exchange.load = lambda identity: deepcopy(saved)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = "/api/debrief/exchange/" + state["id"]
            def get(path, host=None):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                connection.request("GET", path, headers={"Host": host or "127.0.0.1:" + str(server.server_port)})
                response = connection.getresponse()
                result = response.status, dict(response.headers), response.read()
                connection.close()
                return result
            try:
                status, headers, body = get(url)
                self.assertEqual(status, 200)
                self.assertIn("attachment;", headers["Content-Disposition"])
                self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
                self.assertNotIn(b"PRIVATE_", body)
                self.assertIn("Кожна угода".encode(), body)
                self.assertEqual(get(url, "attacker.example")[0], 403)
                self.assertEqual(get("/api/debrief/live/" + state["id"])[0], 404)
                with server.battle_export_lock:
                    self.assertEqual(get(url)[0], 409)
                self.assertEqual(get(url)[0], 200)
            finally:
                server.shutdown()
                thread.join(timeout=3)
                server.server_close()


if __name__ == "__main__":
    unittest.main()
