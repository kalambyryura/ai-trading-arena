"""Quota, owned-close and honest storage-failure regressions; fake transports only."""
from copy import deepcopy
from decimal import Decimal
import json
import socket
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from trading_lab import exchange_engine as engine, exchange_lab as lab_module, match_journal
from trading_lab.arena_engine import emit
from trading_lab.arena_strategies import ArenaError
from trading_lab.exchange_api import ExchangeFeed
from trading_lab.exchange_lab import ExchangeLab
from trading_lab.okx_readonly import PreflightError
import test_rules_only_exchange as fixture


class F01RecoveryReserveTests(unittest.TestCase):
    def setUp(self):
        for attr in ("connect", "connect_ex"):
            self.enterContext(patch.object(socket.socket, attr, side_effect=AssertionError("Network forbidden")))
        self.enterContext(patch.object(socket, "create_connection", side_effect=AssertionError("Network forbidden")))
        self.layout = {"total": None, "reserve": 16 * 1024 * 1024}
        self.free_bytes = 64 * 1024**3
        constructor = match_journal.SegmentedJournal

        def journal_factory(path, **kwargs):
            if self.layout["total"] is not None:
                kwargs.update(total_bytes=self.layout["total"], critical_reserve_bytes=self.layout["reserve"])
            kwargs["free_space"] = lambda path: self.free_bytes
            return constructor(path, **kwargs)

        self.enterContext(patch.object(match_journal, "SegmentedJournal", side_effect=journal_factory))
        self.case = fixture.RulesOnlyTests(methodName="runTest")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.fx = self.case.fx
        self.state = self.bot = self.fake = self.journal = None
        self.evidence = {}

    def start(self, *, enter=True, two=False):
        self.state = self.case.start(live=two, count=2 if two else 1)
        self.bot = self.state["bots"][0]
        self.fake = self.fx.fakes[self.bot["account_id"]]
        if enter:
            self.fx.lab.step(self.state)
            self.fx.now += 1
            self.assertEqual(self.fake.units, Decimal(".02"))
        self.path = self.fx.lab.path / (self.state["id"] + ".events.jsonl")
        self.journal = match_journal.segmented(self.path)
        self.layout.update(total=self.journal.total_limit, reserve=self.journal.critical_reserve)
        self.limits = deepcopy(engine.LIMITS)
        self.billing = {k: deepcopy(v.summary(self.state["id"])) for k, v in self.fx.lab.ledgers.items()}
        self.previous_segments = {p.name: p.read_bytes() for p in self.path.with_suffix(".segments").glob("segment-*.jsonl")}

    def squeeze_normal(self, *, reserve=16 * 1024 * 1024):
        self.layout.update(reserve=reserve, total=self.journal.total_used + reserve + 16)
        self.journal.total_limit = self.layout["total"]
        self.journal.critical_reserve = reserve
        self.before_capacity = self.journal.capacity()
        self.assertEqual(self.before_capacity["normal_headroom_bytes"], 16)

    def normal_event(self, *, text="Queued fictional telemetry"):
        emit(self.state, self.fx.now, self.bot, 4, "fixture_telemetry", text)

    def posts(self):
        return [r for r in self.fake.requests if r.get_method() == "POST" and urlsplit(r.full_url).path == "/api/v5/trade/order"]

    def ticks(self, count=5):
        for _ in range(count):
            if self.state["status"] in {"DONE", "PAUSED"}:
                break
            self.fx.lab.step(self.state)
            self.fx.now += 5

    def owner_stop(self):
        try:
            return self.fx.lab.stop(self.fx.lab.data["generation"], self.state["id"])
        except (ArenaError, OSError, PreflightError) as error:
            self.evidence.update(error=str(error), actual_residual=str(self.fake.units), close_posts=len(self.posts()) - 1)
            self.fail("Queued normal event must not veto a durable owned Stop/close while critical reserve exists: " + str(error))

    def assert_caps_and_accounting(self):
        self.assertEqual(engine.LIMITS, self.limits)
        self.assertEqual(self.bot["_entry_attempts"], 1)
        self.assertEqual(self.bot["ai_requests"], 0)
        self.fx.lab.jev_factory.assert_not_called()
        self.assertEqual({k: v.summary(self.state["id"]) for k, v in self.fx.lab.ledgers.items()}, self.billing)
        self.assertLessEqual(self.journal.total_used, self.layout["total"])
        self.assertEqual(self.journal.critical_reserve, self.layout["reserve"])

    def assert_flat(self):
        self.evidence.update(actual_residual=str(self.fake.units), state_status=self.state["status"],
                             order_posts=len(self.posts()), accounting=self.bot["accounting_reconciled"])
        self.assertEqual(self.fake.units, 0)
        self.assertEqual(Decimal(self.bot["_contracts"]), 0)
        self.assertTrue(self.bot["accounting_reconciled"])
        self.assertEqual(self.bot["protection"]["status"], "FLAT")
        self.assertEqual(self.state["status"], "DONE")
        self.assertEqual(len(self.posts()), 2)
        self.assertEqual(sum(not json.loads(r.data)["reduceOnly"] for r in self.posts()), 1)
        self.assert_caps_and_accounting()

    def test_owned_stop_flushes_exact_prefix_then_close_and_flat_settlement(self):
        self.start()
        self.squeeze_normal()
        self.normal_event()
        prefix = deepcopy(self.state["events"])
        self.owner_stop()
        self.assertTrue(self.state["admission_blocked"])
        self.ticks()
        self.assert_flat()
        self.assertEqual(self.state["events"][:len(prefix)], prefix)
        self.assertGreater(self.journal.total_used, self.before_capacity["used_bytes"] + 16)
        for name, old in self.previous_segments.items():
            self.assertTrue((self.path.with_suffix(".segments") / name).read_bytes().startswith(old))
        restored = deepcopy(self.state)
        seq = match_journal.restore_events(self.path, restored, full=True)
        self.assertEqual(seq, self.state["_event_seq"])
        self.assertEqual(restored["events"], self.state["events"])
        self.assertEqual([e["seq"] for e in restored["events"]], list(range(1, seq + 1)))
        self.evidence["restored_hashchain_seq"] = seq

    def test_restart_restores_prefix_hashchain_and_closes_without_new_entry(self):
        self.start()
        self.squeeze_normal()
        self.normal_event()
        self.owner_stop()
        run_id = self.state["id"]
        before = deepcopy(self.state["events"])
        self.fx.lab.close()
        restarted = ExchangeLab(self.fx.root, self.fx.workshop, self.fx.arena, clock=self.fx.clock,
            monotonic=self.fx.clock, background=False,
            api_factory=lambda profile: self.fx.fakes[profile["id"]].api(profile["environment"]),
            jev_factory=Mock(side_effect=AssertionError("No model during recovery")),
            feed_factory=lambda api: ExchangeFeed(api, clock=self.fx.clock))
        self.fx.lab = restarted
        self.state = restarted.load(run_id)
        self.bot = self.state["bots"][0]
        restored = deepcopy(self.state)
        match_journal.restore_events(self.path, restored, full=True)
        self.assertEqual(restored["events"], before)
        restarted.resume(restarted.data["generation"], run_id)
        self.state = restarted.load(run_id)
        self.bot = self.state["bots"][0]
        restarted.prepare(self.state, restarted.chosen_profiles(["demo"]), True)
        self.journal = match_journal.segmented(self.path)
        self.ticks()
        self.assert_flat()
        self.assertTrue(self.state["_recovery"])
        self.assertTrue(self.state["incomplete"])
        self.assertEqual(self.state["events"][:len(before)], before)

    def discretionary_body(self, *, partial=False):
        if partial:
            self.bot.update(_reduce_remaining=".01", _exit_requested=True)
            return engine.close_body(self.bot, self.fx.lab.identity_for(self.state, self.bot, "C"), ".01")
        snap = self.fx.lab.resources[self.state["id"]]["feed"].snapshot()
        self.state["_last"] = {k: deepcopy(v) for k, v in snap.items() if k != "raw"}
        return engine.entry_body(self.state, self.bot, self.state["strategies"][0], snap,
                                 self.fx.lab.identity_for(self.state, self.bot, "E"))

    def assert_discretionary_blocked(self, body):
        prior_posts, prior_units = len(self.posts()), self.fake.units
        orders = deepcopy(self.bot["_orders"])
        attempts, status = self.bot["_entry_attempts"], self.bot["status"]
        events = deepcopy(self.state["events"])
        used = self.journal.total_used
        error = None
        with patch.object(lab_module.runtime, "reserve", wraps=lab_module.runtime.reserve) as reserve:
            try:
                self.fx.lab.submit(self.state, self.bot, self.fx.lab.resources[self.state["id"]]["apis"][0], body, self.fx.now)
            except (ArenaError, OSError, PreflightError) as caught:
                error = str(caught)
            reserve_calls = reserve.call_count
        self.evidence.update(error=error, order_posts_added=len(self.posts()) - prior_posts,
                             actual_residual=str(self.fake.units), state_status=self.state["status"],
                             runtime_reserve_calls=reserve_calls, new_orders=len(self.bot["_orders"])-len(orders))
        self.assertIsNotNone(error, "discretionary submit must not borrow the critical reserve")
        self.assertEqual(len(self.posts()), prior_posts)
        self.assertEqual(self.fake.units, prior_units)
        self.assertEqual(self.journal.total_used, used)
        self.assertEqual(self.bot["_orders"], orders, "normal quota rejection must precede a send_started intent")
        self.assertEqual(self.bot["_entry_attempts"], attempts)
        self.assertEqual(self.bot["status"], status)
        self.assertEqual(self.state["events"], events)
        self.assertEqual(reserve_calls, 0)
        self.assertEqual(self.state["admission_blocked"], "evidence_storage")
        self.assertTrue(self.state["incomplete"])

    def test_first_entry_cannot_use_critical_intent_reserve(self):
        self.start(enter=False)
        body = self.discretionary_body()
        self.squeeze_normal()
        self.assert_discretionary_blocked(body)
        self.fx.lab.jev_factory.assert_not_called()

    def test_add_cannot_use_critical_intent_reserve(self):
        self.start()
        body = self.discretionary_body()
        self.squeeze_normal()
        self.assert_discretionary_blocked(body)

    def test_discretionary_partial_reduce_cannot_use_close_reserve(self):
        self.start()
        body = self.discretionary_body(partial=True)
        self.squeeze_normal()
        self.assert_discretionary_blocked(body)

    def test_other_bot_closing_cannot_lend_reserve_to_first_entry(self):
        self.start(enter=False, two=True)
        body = self.discretionary_body()
        self.squeeze_normal()
        engine.stop(self.state, self.fx.now, self.state["bots"][1]["id"], "Fictional other-bot Stop")
        self.assertEqual(self.state["status"], "RUNNING")
        self.assertFalse(self.bot["stop_requested"])
        self.assert_discretionary_blocked(body)

    def test_other_bot_closing_cannot_lend_reserve_to_add(self):
        self.start(two=True)
        body = self.discretionary_body()
        self.squeeze_normal()
        engine.stop(self.state, self.fx.now, self.state["bots"][1]["id"], "Fictional other-bot Stop")
        self.assertEqual(self.state["status"], "RUNNING")
        self.assertFalse(self.bot["stop_requested"])
        self.assert_discretionary_blocked(body)

    def test_normal_telemetry_without_closing_stays_normal(self):
        self.start()
        self.squeeze_normal()
        self.normal_event()
        used = self.journal.total_used
        with self.assertRaises(ArenaError):
            match_journal.append_events(self.path, self.state, self.fx.lab.event_positions[self.state["id"]])
        self.assertEqual(self.journal.total_used, used)
        self.assertEqual(self.fake.units, Decimal(".02"))

    def test_recovery_prefix_row_count_is_bounded_without_skipping_history(self):
        self.start()
        self.squeeze_normal()
        seq = self.journal.seq
        for _ in range(129):
            self.normal_event()
        with self.assertRaisesRegex(ArenaError, "^recovery_prefix_limit$"):
            self.fx.lab.stop(self.fx.lab.data["generation"], self.state["id"])
        self.assertEqual(self.journal.seq - seq, 128)
        self.assertEqual(self.state["status"], "PAUSED")
        self.assertEqual(self.fake.units, Decimal(".02"))
        self.assertEqual(len(self.posts()), 1)
        self.assertLessEqual(self.journal.total_used, self.layout["total"])

    def test_recovery_prefix_byte_budget_is_bounded(self):
        self.start()
        self.squeeze_normal()
        for _ in range(3):
            self.normal_event(text="fictional" * 12500)
        with self.assertRaisesRegex(ArenaError, "^recovery_prefix_limit$"):
            self.fx.lab.stop(self.fx.lab.data["generation"], self.state["id"])
        self.assertEqual(self.state["status"], "PAUSED")
        self.assertEqual(self.fake.units, Decimal(".02"))
        self.assertEqual(len(self.posts()), 1)
        self.assertLessEqual(self.journal.total_used - self.before_capacity["used_bytes"], 256 * 1024)

    def test_exhausted_total_quota_keeps_cap_residual_and_honest_pause(self):
        self.start()
        self.squeeze_normal(reserve=512)
        self.normal_event()
        with self.assertRaises(ArenaError):
            self.fx.lab.stop(self.fx.lab.data["generation"], self.state["id"])
        self.assertEqual(self.state["status"], "PAUSED")
        self.assertTrue(self.state["incomplete"])
        self.assertFalse(self.bot["accounting_reconciled"])
        self.assertEqual(self.fake.units, Decimal(".02"))
        self.assertEqual(len(self.posts()), 1)
        self.assertLessEqual(self.journal.total_used, self.layout["total"])
        self.assert_caps_and_accounting()

    def test_persistent_injected_disk_full_worker_pauses_without_false_close(self):
        self.start()
        self.normal_event()
        engine.stop(self.state, self.fx.now, reason="Fictional owner recovery")
        self.free_bytes = 0
        resource = self.fx.lab.resources.pop(self.state["id"])
        resource["stack"].close()
        try:
            self.fx.lab.worker(self.state, self.fx.lab.chosen_profiles(["demo"]), True)
        except Exception as error:
            self.fail("Persistent storage failure must leave an honest PAUSED controller, not escape its handler: " + type(error).__name__)
        self.assertEqual(self.state["status"], "PAUSED")
        self.assertTrue(self.state["incomplete"])
        self.assertTrue(self.state["error"])
        self.assertFalse(self.bot["accounting_reconciled"])
        self.assertEqual(self.fake.units, Decimal(".02"))
        self.assertEqual(Decimal(self.bot["_contracts"]), Decimal(".02"))
        self.assertEqual(len(self.posts()), 1)
        self.assert_caps_and_accounting()


if __name__ == "__main__":
    unittest.main()
