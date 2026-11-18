"""Segmented journal readers (panel polls, league, review) never touch the running writer; temp folders only."""
import json
from pathlib import Path
import tempfile
import unittest

from trading_lab import match_journal
from trading_lab.match_journal import restore_events, segmented, verified_rows
from trading_lab.ops_storage import StorageBlocked


class JournalReaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / ("a" * 32 + ".events.jsonl")
        self.addCleanup(match_journal._JOURNALS.pop, str(self.path), None)
        self.writer = segmented(self.path)
        for seq in range(1, 31):
            self.writer.append({"code": "no_signal", "at": seq})

    def state(self, seq=30):
        return {"protocol_version": 8, "events": [{"seq": seq, "code": "no_signal", "at": seq}], "_journal_checkpoint_seq": seq}

    def test_a_reader_keeps_the_writers_object_and_writes_nothing(self):
        # Before R2.1.2 a reader rebuilt the cached writer: the running worker re-verified its whole journal under the
        # lab lock at its next save, and a reader racing its append left it one event behind (event_sequence_mismatch).
        index = self.path.with_suffix(".segments") / "index.json"
        before = index.stat().st_mtime_ns, index.read_bytes()
        state = self.state()
        self.assertEqual(restore_events(self.path, state, full=True), 30)
        self.assertEqual([e["seq"] for e in state["events"]], list(range(1, 31)))
        self.assertIs(segmented(self.path), self.writer)
        self.assertEqual((index.stat().st_mtime_ns, index.read_bytes()), before)
        self.writer.append({"code": "no_signal", "at": 31})  # the writer goes on from where it was
        self.assertEqual(restore_events(self.path, self.state(31), full=True), 31)

    def test_a_reader_fails_exactly_where_the_writer_fails(self):
        # A run being written is read under the engine lock, so a torn row is a fault (a crash), never hidden.
        segment = self.path.with_suffix(".segments") / "segment-00000001.jsonl"
        complete = segment.read_bytes()
        for torn in (b'{"seq": 31, "previo', b"x" * 131073):
            segment.write_bytes(complete + torn)
            with self.assertRaises(StorageBlocked):
                list(verified_rows(self.path))
        segment.write_bytes(complete)
        (segment.parent / "segment-00000002.jsonl").write_bytes(b"")
        self.assertEqual(len(list(verified_rows(self.path))), 30)
        lines = complete.splitlines(keepends=True)
        row = json.loads(lines[9])
        row["event"]["at"] = 999
        segment.write_bytes(b"".join(lines[:9]) + (json.dumps(row) + "\n").encode() + b"".join(lines[10:]))
        with self.assertRaises(StorageBlocked):
            list(verified_rows(self.path))
        segment.write_bytes(b"".join(lines[:9]) + lines[9][:-1] + b"".join(lines[10:]))
        with self.assertRaises(StorageBlocked):
            list(verified_rows(self.path))
        segment.write_bytes(b"".join(lines[:29]) + lines[29][:-1])  # torn in a segment that is not the last
        with self.assertRaises(StorageBlocked):
            list(verified_rows(self.path))

    def test_a_failed_append_leaves_no_cached_writer(self):
        match_journal.drop_writer(self.path)
        self.assertIsNot(segmented(self.path), self.writer)  # rebuilt from the files, as after a restart


if __name__ == "__main__": unittest.main()
