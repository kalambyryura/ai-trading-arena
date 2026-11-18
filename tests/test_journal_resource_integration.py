"""Operational integration regressions. Injected journal, no production I/O."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trading_lab import match_journal
from trading_lab.arena_strategies import ArenaError
from trading_lab.ops_storage import StorageBlocked


class FictionalJournal:
    def __init__(self, events=(), *, fail_once_at=None):
        self.rows = [{"seq": e["seq"], "event": {k: v for k, v in e.items() if k != "seq"}}
                     for e in events]
        self.seq = len(self.rows)
        self.fail_once_at = fail_once_at

    def append(self, event, *, critical=False):
        if self.seq + 1 == self.fail_once_at:
            self.fail_once_at = None
            raise StorageBlocked("fictional_transient_index_or_capacity_error")
        self.seq += 1
        self.rows.append({"seq": self.seq, "event": deepcopy(event)})

    def query(self, *, after_seq=0, limit=500):
        return deepcopy([row for row in self.rows if row["seq"] > after_seq][:limit])

    def capacity(self):
        return {"force_position_close": False}


class JournalResourceTests(unittest.TestCase):
    def test_retry_adopts_already_durable_prefix_without_duplicate_or_false_corruption(self):
        events = [{"seq": 1, "code": "protective_intent", "data": {"fictional": 1}},
                  {"seq": 2, "code": "exchange_close_fill", "data": {"fictional": 2}}]
        state = {"protocol_version": 8, "events": deepcopy(events), "_event_seq": 2}
        journal = FictionalJournal(fail_once_at=2)
        with patch.object(match_journal, "segmented", return_value=journal):
            with self.assertRaises(ArenaError):
                match_journal.append_events(Path("fictional.events.jsonl"), state, 0)
            self.assertEqual(journal.seq, 1)  # First event had already been fsynced.
            written = match_journal.append_events(Path("fictional.events.jsonl"), state, 0)
        self.assertEqual(written, 2)
        self.assertEqual(len(journal.rows), 2)
        self.assertEqual([row["event"]["code"] for row in journal.rows],
                         ["protective_intent", "exchange_close_fill"])

    def test_restored_v8_state_keeps_a_bounded_tail_and_full_history_stays_queryable(self):
        events = [{"seq": n, "code": "fictional_observation", "data": {"n": n}} for n in range(1, 2001)]
        journal = FictionalJournal(events)
        state = {"protocol_version": 8, "events": deepcopy(events[-128:]),
                 "_journal_checkpoint_seq": 2000, "_event_seq": 2000, "bots": [], "frames": []}
        # Readers verify rows straight from the segment files (verified_rows), never through the writer's object.
        with patch.object(match_journal, "verified_rows", side_effect=lambda path: iter(deepcopy(journal.rows))), \
             patch.object(Path, "exists", return_value=True):
            restored_seq = match_journal.restore_events(Path("fictional.events.jsonl"), state)
        self.assertEqual(restored_seq, 2000)
        self.assertLessEqual(len(state["events"]), 500)
        self.assertTrue(state.get("_history_compact"))
        self.assertEqual(journal.query(after_seq=1999, limit=1)[0]["seq"], 2000)


if __name__ == "__main__":
    unittest.main()
