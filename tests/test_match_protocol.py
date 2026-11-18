"""V6 scheduling/accounting tests: disposable roots, fake API/Jev, virtual clocks."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import test_exchange_lab as fixtures
from test_arena import market, recipe
from trading_lab.arena_engine import new_season, tick, request_stop, emit
from trading_lab.arena_strategies import validate_strategy, ArenaError
from trading_lab.match_protocol import capacities, validate_manifest
from trading_lab.match_journal import append_events, checkpoint, restore_events
from trading_lab.okx_readonly import PreflightError
from trading_lab.strategy_contract import payload_of


class SchedulingTests(unittest.TestCase):
    setUp = fixtures.ExchangeTests.setUp
    tearDown = fixtures.ExchangeTests.tearDown

    def normal(self, duration=300, hold=900, count=1, never=False):
        ids, accounts = [], ["demo"] if count == 1 else ["arena-a", "arena-b"]
        for i in range(count):
            p = payload_of(recipe())
            p["name"] = "Clock fixture " + str(i)
            p["rules"].update(max_hold_seconds=hold, cooldown_seconds=15, max_entries=1,
                              stop_loss_bps=500, take_profit_bps=1000)
            p["rules"]["entry"]["conditions"] = [{"feature": "spread_bps", "op": "lte", "value": -1 if never else 5}]
            clean = validate_strategy(p)
            if count == 1:
                ids.append(self.workshop.add_version(self.workshop.data["generation"], p)["id"])
            else:
                clean["id"] = "normal-fixture-" + str(i)
                self.arena.data["strategies"].append(clean)
                ids.append(clean["id"])
        context = "workshop_demo" if count == 1 else "arena_live"
        accepted = self.lab.start(self.lab.data["generation"], context, ids, accounts, 100, duration)
        state = self.lab.load(accepted["id"])
        self.lab.prepare(state, self.lab.chosen_profiles(accounts), False)
        return state

    def advance_to(self, state, target):
        while self.now < target and state["status"] not in {"DONE", "PAUSED"}:
            self.now = min(target, self.now + 30)
            self.lab.step(state)

    def test_deadline_drains_without_force_close_then_last_exit_finishes(self):
        state = self.normal(); start = self.now; bot = state["bots"][0]
        self.lab.step(state); self.assertGreater(bot["position_btc"], 0)
        self.advance_to(state, start + 300)
        self.assertEqual(state["status"], "DRAINING")
        self.assertFalse(bot["stop_requested"])
        self.assertEqual(len(self.fakes["demo"].orders), 1)
        self.assertFalse(any(e["code"] == "rules_close" for e in state["events"]))
        self.advance_to(state, start + 930)
        self.assertEqual(state["status"], "DONE"); self.assertFalse(state["incomplete"])
        self.assertEqual(bot["position_btc"], 0); self.assertEqual(bot["trades"], 1)
        self.assertEqual(state["match_manifest"]["admission_seconds"], 300)
        validate_manifest(state)

    def test_reentry_before_cutoff_preserves_capital_and_cooldown(self):
        state = self.normal(duration=900, hold=30); bot = state["bots"][0]
        self.lab.step(state); self.now += 30; self.lab.step(state)
        self.now += 1; self.lab.step(state)
        self.assertEqual(bot["position_btc"], 0); first_pnl = bot["realized_pnl_usdt"]
        self.assertEqual(state["status"], "RUNNING")
        self.now += 14; self.lab.step(state)
        self.assertEqual(len(self.fakes["demo"].orders), 2)
        self.now += 1; self.lab.step(state)
        self.assertGreater(bot["position_btc"], 0)
        self.assertEqual(bot["_entry_attempts"], 2); self.assertEqual(bot["trades"], 1)
        self.assertLessEqual(bot["realized_pnl_usdt"], first_pnl)
        self.lab.stop(self.lab.data["generation"], state["id"])
        self.lab.step(state); self.now += 1; self.lab.step(state)

    def test_owner_stop_during_drain_closes_and_never_reenters(self):
        state = self.normal(); start = self.now
        self.lab.step(state); self.advance_to(state, start + 300)
        self.lab.stop(self.lab.data["generation"], state["id"])
        self.lab.step(state); self.now += 1; self.lab.step(state)
        self.assertEqual(state["status"], "DONE")
        self.assertEqual(len(self.fakes["demo"].orders), 2)
        self.assertTrue(list(self.fakes["demo"].orders.values())[-1]["reduceOnly"])

    def test_drain_recovery_after_restart_only_closes_and_uses_no_new_jev(self):
        state = self.normal(); start = self.now
        self.lab.step(state); self.advance_to(state, start + 300)
        resource = self.lab.resources.pop(state["id"]); resource["stack"].close()
        self.lab.sessions.pop(state["id"])
        restored = self.lab.load(state["id"])
        self.assertEqual(self.lab.public(restored)["status"], "PAUSED")
        self.assertEqual(self.lab.public(restored)["lifecycle"]["bots"][0]["current_phase"]["code"], "paused")
        self.lab.resume(self.lab.data["generation"], state["id"])
        restored = self.lab.load(state["id"])
        self.lab.prepare(restored, self.lab.chosen_profiles(["demo"]), True)
        self.now += 1; self.lab.step(restored); self.now += 1; self.lab.step(restored)
        self.assertEqual(restored["status"], "DONE")
        self.assertEqual(restored["bots"][0]["ai_requests"], 1)
        self.assertEqual(len(self.fakes["demo"].orders), 2)
        self.assertEqual(self.fakes["demo"].units, 0)

    def test_lost_ack_partial_entry_crossing_deadline_is_reconciled_without_another_entry(self):
        state = self.normal(); origin = self.now; fake = self.fakes["demo"]
        fake.lost_ack = True; fake.entry_fraction = Decimal(".5")
        self.lab.step(state)
        self.advance_to(state, origin + 300)
        self.now = origin + 301; self.lab.step(state)
        self.assertEqual(state["status"], "DRAINING")
        self.assertEqual(len(fake.orders), 1)
        self.assertGreater(state["bots"][0]["position_btc"], 0)
        self.lab.stop(self.lab.data["generation"], state["id"])
        self.lab.step(state); self.now += 1; self.lab.step(state)
        self.assertEqual(state["status"], "DONE"); self.assertFalse(state["incomplete"])
        self.assertEqual(fake.units, 0)
        entry, close = fake.orders.values()
        self.assertEqual(close["sz"], entry["accFillSz"])

    def test_individual_stop_preserves_other_position_past_cutoff(self):
        state = self.normal(count=2); start = self.now
        self.lab.step(state)
        self.assertTrue(all(b["position_btc"] for b in state["bots"][:-1]))
        self.lab.stop(self.lab.data["generation"], state["id"], state["bots"][0]["id"])
        self.lab.step(state); self.now += 1; self.lab.step(state)
        self.assertEqual(state["bots"][0]["position_btc"], 0)
        self.assertGreater(state["bots"][1]["position_btc"], 0)
        self.advance_to(state, start + 300)
        self.assertEqual(state["status"], "DRAINING")
        self.assertGreater(state["bots"][1]["position_btc"], 0)
        self.advance_to(state, start + 930)
        self.assertEqual(state["status"], "DONE"); self.assertFalse(state["incomplete"])

    def test_jev_reply_after_cutoff_cannot_open(self):
        state = self.normal(); origin = self.now
        gate = self.lab.resources[state["id"]]["gate"]
        original = gate.decide
        def late(*args):
            outcome = original(*args); self.now = origin + 301; return outcome
        gate.decide = late
        self.lab.step(state)
        self.assertFalse(self.fakes["demo"].orders)
        self.assertEqual(state["jev_usage"]["attempts"], 1)
        self.lab.step(state)
        self.assertEqual(state["status"], "DONE")

    def test_monotonic_cutoff_closes_admission_without_stopping_existing_position(self):
        state = self.normal(); self.lab.step(state)
        resource = self.lab.resources[state["id"]]
        with patch.object(self.lab, "monotonic", return_value=resource["deadline"] + 1):
            self.lab.step(state)
        self.assertEqual(state["status"], "DRAINING")
        self.assertGreater(state["bots"][0]["position_btc"], 0)
        self.assertFalse(state["bots"][0]["stop_requested"])
        self.assertEqual(len(self.fakes["demo"].orders), 1)

    def test_recovery_reserve_allows_owned_close_when_normal_get_cap_exhausted(self):
        state = self.normal(); self.lab.step(state)
        api = self.lab.resources[state["id"]]["apis"][0]
        api.requests = api.request_limit
        self.lab.stop(self.lab.data["generation"], state["id"])
        self.lab.step(state); self.now += 1; self.lab.step(state)
        self.assertEqual(state["status"], "DONE"); self.assertFalse(state["incomplete"])
        self.assertEqual(state["bots"][0]["position_btc"], 0)
        self.assertGreater(api.requests, api.request_limit)
        with self.assertRaises(PreflightError):
            api.get("/api/v5/market/ticker", private=False)

    def test_accelerated_24h_wait_does_not_hit_old_short_limits(self):
        state = self.normal(duration=86400, hold=30, never=True); start = self.now
        self.lab.step(state)
        self.advance_to(state, start + 86400)
        resource = self.lab.resources[state["id"]]
        self.assertGreater(resource["feed"].calls, 10000)
        self.assertGreater(resource["apis"][0].requests, 10000)
        self.assertGreater(state["_ticks"], 420)
        self.assertEqual(state["status"], "DONE"); self.assertFalse(state["incomplete"])
        self.assertFalse(self.fakes["demo"].orders); self.assertEqual(state["jev_usage"]["attempts"], 0)
        self.assertLess(len(state["frames"]), 1500)
        self.assertEqual(len(state["events"]), 500)
        self.assertGreater(state["_event_seq"], 5000)
        saved = self.lab.load(state["id"])
        self.assertEqual(saved["events"], state["events"])
        from trading_lab.arena import read
        disk = read(self.lab.path / (state["id"] + ".json"))
        self.assertEqual(len(disk["events"]), 128)
        restore_events(self.lab.path / (state["id"] + ".events.jsonl"), disk)
        self.assertEqual(disk["events"], state["events"])
        public = self.lab.public(state, compact=True)
        self.assertEqual(len(public["events"]), 500)
        self.assertEqual(public["events_count"], state["_event_seq"])
        self.assertFalse(public["history_complete"])
        full = self.lab.history(state["id"])
        self.assertTrue(full["history_complete"])
        self.assertGreater(full["lifecycle"]["bots"][0]["funnel"]["entry"]["checks"], 2000)
        self.assertEqual(public["bots"], self.lab.history(state["id"])["bots"])


class PureProtocolTests(unittest.TestCase):
    def test_paper_drain_and_no_add_during_tail(self):
        r = recipe(); r["rules"].update(max_hold_seconds=900, max_entries=3)
        s = new_season([r], 100, 300, 1000, "fixture")
        for at in range(1000, 1301, 5): tick(s, market(at))
        bot = s["bots"][0]
        self.assertEqual(s["status"], "DRAINING"); self.assertGreater(bot["position_btc"], 0)
        self.assertFalse(bot["stop_requested"])
        snapshot = market(1305, 100.3); tick(s, snapshot)
        self.assertFalse(any(e["code"] == "paper_entry_fill" and e["at"] > s["admission_end_at"] for e in s["events"]))
        request_stop(s, 1306, origin="owner"); tick(s, market(1310)); tick(s, market(1315))
        self.assertEqual(s["status"], "DONE")

    def test_legacy_policy_still_force_closes_and_manifest_is_immutable(self):
        s = new_season([recipe()], 100, 300, 1000, "fixture")
        for key in ("time_policy", "match_manifest", "match_manifest_sha256"):
            s.pop(key)
        for at in range(1000, 1311, 5):
            if s["status"] != "DONE": tick(s, market(at))
        self.assertEqual(s["status"], "DONE")
        s = new_season([recipe()], 100, 86400, 1000, "fixture")
        saved = deepcopy(s["match_manifest"]); tick(s, market(1000))
        self.assertEqual(s["match_manifest"], saved)
        s["match_manifest"]["budget_usdt_per_bot"] = 999
        with self.assertRaises(ValueError): validate_manifest(s)

    def test_bounds_include_last_position_and_do_not_expand_financial_risk(self):
        r = recipe(); r["rules"]["max_hold_seconds"] = 86400
        clean = validate_strategy(payload_of(r))
        limits = capacities(86400, [clean])
        self.assertGreater(limits["max_observations"], 34560)
        self.assertGreater(limits["max_public_gets"], 138240)
        r["rules"]["max_hold_seconds"] = 86401
        with self.assertRaises(ArenaError): validate_strategy(payload_of(r))

    def test_manifest_rejects_changed_runtime_even_if_manifest_hash_is_unchanged(self):
        s = new_season([recipe()], 100, 300, 1000, "fixture")
        s["rules"]["max_drawdown_pct"] = 99
        with self.assertRaises(ArenaError): validate_manifest(s)

    def test_compact_checkpoint_restores_all_steps_and_rejects_truncated_or_missing_journal(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            s = new_season([recipe()], 100, 300, 1000, "fixture")
            s["protocol_version"] = 6  # This regression verifies the legacy JSONL contract.
            for i in range(400): emit(s, 1001 + i, s["bots"][0], 1, "checks", "fixture")
            append_events(path, s, 0); saved = checkpoint(s)
            self.assertEqual(len(saved["events"]), 128)
            restore_events(path, saved); self.assertEqual(saved["events"], s["events"])
            lines = path.read_text(encoding="utf-8").splitlines()
            path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
            with self.assertRaises(ArenaError): restore_events(path, checkpoint(s))
            path.unlink()
            with self.assertRaises(ArenaError): restore_events(path, checkpoint(s))

    def test_replay_with_no_exit_tail_is_incomplete_and_keeps_owned_position(self):
        from trading_lab.strategy_training import run_trial, SYNTHETIC
        r = recipe(); r["rules"].update(max_hold_seconds=900, stop_loss_bps=500, take_profit_bps=1000)
        tape = [market(at) for at in range(1000, 1311, 5)]
        report = run_trial(r, SYNTHETIC, 100, [("missing tail", tape, 300)])
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertGreater(report["runs"][0]["metrics"]["remaining_btc"], 0)
        self.assertEqual(report["runs"][0]["lifecycle"]["bots"][0]["rows"][7]["status"], "pending")

    def test_append_only_events_restore_tail_without_inventing_order_facts(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            s = new_season([recipe()], 100, 300, 1000, "fixture")
            s["protocol_version"] = 6
            saved = deepcopy(s); count = append_events(path, s, 0)
            emit(s, 1005, s["bots"][0], 1, "entry_checks", "fixture", checks=[])
            self.assertEqual(append_events(path, s, count), count + 1)
            restore_events(path, saved)
            self.assertEqual(saved["events"], s["events"])
            self.assertEqual(saved["bots"][0]["position_btc"], 0)
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), count + 1)

    def test_bills_have_server_end_and_page_from_latest(self):
        f = fixtures.FakeExchange(lambda: 1800000030.)
        api = f.api(); seen = []
        original = api.transport
        def transport(request):
            query = parse_qs(urlsplit(request.full_url).query)
            if urlsplit(request.full_url).path == "/api/v5/account/bills":
                seen.append(query)
                if "after" not in query:
                    return {"code": "0", "data": [{"billId": str(i)} for i in range(200, 100, -1)]}
                return {"code": "0", "data": [{"billId": "100"}]}
            return original(request)
        api.transport = transport
        rows = api.bills(1799999999000)
        self.assertEqual(len(rows), 101)
        self.assertEqual(seen[0]["end"], ["1800000030000"])
        self.assertEqual(seen[1]["after"], ["101"])


if __name__ == "__main__": unittest.main()
