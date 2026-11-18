"""Strict public evidence and causal replay: fictional journals only, no transport."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import sys
import unittest

PATH = Path(__file__).resolve().parent
ROOT = PATH.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(PATH))
from tests.test_cycle_diagnostics import state_for, event, filled, settled
from test_management_integration import recipe, market
from trading_lab import arena_engine as paper
from trading_lab import battle_review as review, event_replay as replay
from trading_lab.arena_strategies import ArenaError


def exchange_fixture():
    state = state_for(("fictional-one",))
    state.update(id="a" * 32, mode="OKX_DEMO", budget_usdt=100, frames=[],
        created_at="2026-09-30T12:00:00Z", started_at="2026-09-30T12:00:00Z",
        admission_end_at="2026-09-30T12:05:00Z", duration_seconds=300,
        code_sha256="e" * 64, history_complete=True, incomplete=False)
    state["strategies"][0].update(recipe_sha256="b" * 64, version=1,
        author={"kind": "example"}, rules={"max_entries": 1, "side": "long"})
    bot = state["bots"][0]
    bot["_instrument"] = {"contract_value": ".01", "contract_multiplier": "1"}
    filled(state, bot, "FICTIONAL_ENTRY", qty=".02", fee="-.01")
    filled(state, bot, "FICTIONAL_CLOSE", qty=".02", side="sell", reduce=True,
        fee="-.01", gross=".014")
    settled(state, bot, gross=".014", fees="-.02", funding="-.001")
    state["events"][-1]["data"] = deepcopy(state["events"][-1]["data"])
    event(state, None, "season_done", step=6)
    state.update(status="DONE", finished_at=state["events"][-1]["at"])
    return state


def paper_fixture():
    item = recipe(reduce=False)
    item["rules"]["entry"]["long"]["confirm"]["value"] = 999
    state = paper.new_season([item], 100, 300, 60, "e" * 64)
    paper.tick(state, market(61))
    paper.tick(state, market(360))
    assert state["status"] == "DONE"
    return state


def rehash_packet(packet):
    packet["package_hash"] = review.digest({k: v for k, v in packet.items() if k != "package_hash"})
    return packet


class ReplayEvidenceIntegrationTests(unittest.TestCase):
    def test_good_exchange_packet_has_exact_hash_and_does_not_mutate_source(self):
        source = exchange_fixture()
        before = deepcopy(source)
        packet = replay.build_packet(source)
        self.assertTrue(replay.verify_packet(packet)["verified"])
        self.assertEqual(source, before)
        self.assertNotIn("FICTIONAL_ENTRY", json.dumps(packet))
        self.assertNotIn("FICTIONAL_CLOSE", json.dumps(packet))

    def test_rehashed_unknown_fields_are_not_a_supported_public_dto(self):
        for target in ("packet", "manifest", "participant", "event", "financial", "quality"):
            with self.subTest(target=target):
                packet = replay.build_packet(exchange_fixture())
                holder = {"packet": packet, "manifest": packet["manifest"],
                    "participant": packet["manifest"]["participants"][0], "event": packet["events"][0]}.get(target)
                if target == "financial": holder = packet["events"][0]["financial"]
                if target == "quality":
                    holder = packet["events"][0]["quality"] = {"coverage": "UNKNOWN", "billing": "UNKNOWN"}
                holder["private_future_field"] = {"uid": "PRIVATE_MARKER"}
                rehash_packet(packet)
                with self.assertRaises(ArenaError): replay.verify_packet(packet)

    def test_dto_rejects_unknown_participant_duplicate_slot_and_bool_sequence(self):
        mutations = [lambda p: p["events"][0].update(bot_id="foreign-account"),
            lambda p: p["manifest"]["participants"][1].update(slot=0),
            lambda p: p["events"][0].update(seq=True)]
        for mutate in mutations:
            packet = replay.build_packet(exchange_fixture())
            mutate(packet)
            with self.assertRaises(ArenaError): replay.verify_packet(rehash_packet(packet))

    def test_dto_rejects_noncausal_availability_and_future_source_time(self):
        for field, value in (("available_at_ms", -1), ("available_at_ms", 1),
                             ("source_at_ms", 2000000000000)):
            with self.subTest(field=field, value=value):
                packet = replay.build_packet(exchange_fixture())
                packet["events"][-1][field] = value
                with self.assertRaises(ArenaError): replay.verify_packet(rehash_packet(packet))

    def test_dto_rejects_nondecimal_financials_and_noninteger_trades(self):
        for key, value in (("pnl_usdt", 0), ("pnl_usdt", "NaN"),
                           ("trades", "2"), ("trades", True), ("trades", -1)):
            with self.subTest(key=key, value=value):
                packet = replay.build_packet(exchange_fixture())
                packet["events"][0]["financial"][key] = value
                with self.assertRaises(ArenaError): replay.verify_packet(rehash_packet(packet))

    def test_final_accounting_never_rewrites_earlier_fill_observations(self):
        source = exchange_fixture()
        original = replay.build_packet(source)
        changed = deepcopy(source)
        changed["bots"][0]["accounting"].update(gross_pnl_usdt="7", signed_funding_usdt="-2", cash_delta_usdt="4.98")
        changed["bots"][0].update(equity_usdt=104.98, pnl_usdt=4.98)
        candidate = replay.build_packet(changed)
        original_early = [e for e in original["events"] if e.get("source_event_seq", 0) < 5]
        candidate_early = [e for e in candidate["events"] if e.get("source_event_seq", 0) < 5]
        self.assertEqual(original_early, candidate_early)
        settlement = next(e for e in candidate["events"] if e["code"] == "accounting_confirmed")
        self.assertEqual(Decimal(settlement["financial"]["pnl_usdt"]), Decimal("-.007"))

    def test_paper_settlement_requires_an_actual_completion_boundary(self):
        source = paper_fixture()
        finished = replay.ms(source["finished_at"])
        original = replay.build_packet(source)
        settlements = [e for e in original["events"] if e.get("settled") and e.get("bot_id") != "cash"]
        self.assertTrue(settlements)
        self.assertTrue(all(e["available_at_ms"] >= finished for e in settlements))
        # Simulate a truncated final checkpoint with old events/frames retained.
        source["events"] = [e for e in source["events"] if replay.ms(e["at"]) < finished]
        source["frames"] = [f for f in source["frames"] if replay.ms(f["at"]) < finished]
        source.update(history_complete=False, _history_compact=True)
        truncated = replay.build_packet(source)
        self.assertFalse(any(e.get("settled") and e.get("bot_id") != "cash" for e in truncated["events"]))
        self.assertFalse(any(e.get("phase") == "DONE" for e in truncated["events"]))

    def test_numeric_observation_is_projected_while_unknown_trade_count_stays_unknown(self):
        source = exchange_fixture()
        row = event(source, source["bots"][0], "financial_observation", {
            "financial": {"position_btc": "0", "pnl_usdt": "-.007", "trades": {"uid": "PRIVATE_MARKER"}},
            "settled": True})
        bundle = review.build_review(source)
        self.assertNotIn("PRIVATE_MARKER", json.dumps(bundle))
        packet = replay.build_packet(source)
        projected = next(e for e in packet["events"] if e.get("source_event_seq") == row["seq"])
        self.assertIsNone(projected["financial"].get("trades"))
        self.assertTrue(replay.verify_packet(packet)["verified"])

    def test_legacy_frame_numeric_fields_never_copy_arbitrary_nested_values(self):
        source = exchange_fixture()
        source["frames"] = [{"at": source["created_at"], "event_seq": 1,
            "bots": [{"id": source["bots"][0]["id"], "pnl_usdt": {"uid": "PRIVATE_MARKER"},
                "trades": {"prompt": "PRIVATE_MARKER"}, "status": {"credentials": "PRIVATE_MARKER"}}]}]
        bundle = review.build_review(source)
        self.assertNotIn("PRIVATE_MARKER", json.dumps(bundle))

    def test_unknown_funding_then_known_cumulative_value_remains_readable(self):
        source = exchange_fixture()
        bot = source["bots"][0]
        event(source, bot, "exchange_funding", {"signed_funding_usdt": None})
        event(source, bot, "exchange_funding", {"signed_funding_usdt": "-.001"})
        bundle = review.build_review(source)
        self.assertTrue(review.verify_bundle(bundle)["verified"])
        funding = [r for r in bundle["participants"][0]["trade_ledger"]["rows"] if r["role"] == "funding"]
        self.assertEqual(len(funding), 2)
        self.assertIsNone(funding[0]["signed_funding_usdt"])
        # The second total is known; its increment from an unknown prior total isn't zero.
        self.assertIsNone(funding[1]["signed_funding_usdt"])


if __name__ == "__main__": unittest.main()
