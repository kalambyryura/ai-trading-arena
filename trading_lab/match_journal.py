"""Append-only step evidence plus periodically checkpointed long-match state."""
from copy import deepcopy
import hashlib
import json
import os

from .arena_strategies import ArenaError
from .ops_storage import SegmentedJournal, StorageBlocked, encode

EVENT_LIMIT = 128 * 1024 * 1024
SEGMENT_BYTES, TOTAL_BYTES, ROW_BYTES = 8 * 1024 * 1024, 512 * 1024 * 1024, 131072  # as segmented() opens a writer
_JOURNALS = {}  # writers only: path -> the SegmentedJournal this process appends through
RECOVERY_PREFIX_MAX_ROWS = 128
RECOVERY_PREFIX_MAX_BYTES = 256 * 1024


def _critical_event(event, recovery):
    if event.get("code") == "exchange_order_intent":
        # A new entry/add or discretionary partial reduction cannot spend the
        # close reserve merely by being named a financial intent.
        return recovery and event.get("data", {}).get("request", {}).get("reduceOnly") is True
    return event.get("code") in {"exchange_entry_fill", "exchange_close_fill", "protective_intent",
        "protective_child_adopted", "accounting_confirmed", "exit_trigger", "stop_requested",
        "season_done", "closing_paused"}


def _row_bytes(journal, event):
    data = {"seq": journal.seq + 1, "previous_sha256": journal.last_hash, "event": event}
    return len(encode({**data, "sha256": hashlib.sha256(encode(data)).hexdigest()})) + 1


def require_normal_capacity(path, state, proposed):
    """Preflight the ordered evidence BEFORE a discretionary intent is reserved.

    This is a conservative capacity check, not a substitute for fsync. A real
    write can still fail later; those unknown durable intents are never erased.
    Required recovery facts in the pending prefix cannot lend normal capacity
    to this new entry/add/partial-reduction admission.
    """
    if state.get("protocol_version", 0) < 8:
        return
    journal = segmented(path)
    pending = [event for event in state["events"] if event["seq"] > journal.seq] + [proposed]
    needed = 0
    segment_used = journal.segments[-1]["bytes"] if journal.segments else 0
    segments = len(journal.segments)
    for offset, event in enumerate(pending, 1):
        if event["seq"] != journal.seq + offset:
            raise ArenaError("event_sequence_mismatch")
        data = {"seq": event["seq"], "previous_sha256": journal.last_hash,
                "event": {k: v for k, v in event.items() if k != "seq"}}
        size = len(encode({**data, "sha256": hashlib.sha256(encode(data)).hexdigest()})) + 1
        if size > min(journal.max_row_bytes, journal.segment_bytes):
            raise ArenaError("event_row_too_large")
        needed += size
        if not segments or segment_used + size > journal.segment_bytes:
            segments += 1
            segment_used = 0
        segment_used += size
    if journal.total_used + needed > journal.total_limit - journal.critical_reserve:
        raise ArenaError("evidence_quota_blocks_recording_not_close")
    if journal.free_space(journal.path) - needed < journal.min_free:
        raise ArenaError("disk_reserve_blocks_writes_not_close")
    if segments > journal.max_segments:
        raise ArenaError("segment_capacity_blocks_recording_not_close")


def segmented(path):
    key = str(path)
    if key not in _JOURNALS:
        if len(_JOURNALS) >= 12: _JOURNALS.pop(next(iter(_JOURNALS)))
        _JOURNALS[key] = SegmentedJournal(path.with_suffix(".segments"), total_bytes=TOTAL_BYTES,
                                         critical_reserve_bytes=16 * 1024 * 1024)
    return _JOURNALS[key]


def drop_writer(path):
    """Forget the cached writer after a failed append: the next writer re-verifies the files and fails closed on a
    torn row instead of appending after it."""
    _JOURNALS.pop(str(path), None)


def verified_rows(path):
    """The journal's rows straight from its fsynced segment files, verified exactly as a writer verifies them, for
    readers. A reader never rebuilds or evicts a writer's cached SegmentedJournal (that made the running worker
    re-verify its whole journal under the lab lock at its next save) and never writes (no index rewrite racing the
    writer's). A run being written is read under the engine lock (journal_copy), so a torn line is always a fault."""
    files = sorted(path.with_suffix(".segments").glob("segment-????????.jsonl"))
    seq, last, total = 0, "0" * 64, 0
    for index, file in enumerate(files, 1):
        if file.name != f"segment-{index:08}.jsonl" or file.stat().st_size > SEGMENT_BYTES:
            raise StorageBlocked("segment_shape")
        with file.open("rb") as stream:
            for line in stream:
                if not line.endswith(b"\n") or len(line) > ROW_BYTES:
                    raise StorageBlocked("journal_incomplete_or_oversize_line")
                try:
                    row = json.loads(line)
                    data = {key: value for key, value in row.items() if key != "sha256"}
                    row_hash = hashlib.sha256(encode(data)).hexdigest()
                    if row.get("seq") != seq + 1 or row.get("previous_sha256") != last or row.get("sha256") != row_hash:
                        raise ValueError()
                except (ValueError, TypeError, KeyError, AttributeError):
                    raise StorageBlocked("journal_chain_corrupt") from None
                seq, last = row["seq"], row_hash
                total += len(line)
                if total > TOTAL_BYTES:
                    raise StorageBlocked("journal_total_exceeds_policy")
                yield row


def running_status(controller, identity):
    """The status of a run this process drives in memory (an exchange session or the active PAPER season), else None."""
    sessions = getattr(controller, "sessions", None)
    state = sessions.get(identity) if sessions is not None else getattr(controller, "session", None)
    return state["status"] if state and state.get("id") == identity else None


def journal_receipt(path):
    """sha256 of each segment file and the verified chain head (last seq, its row sha256): an export that loses float
    formatting (JSON in a browser) still pins the journal, and a segment file can be checked against it. None for a
    journal before protocol 8."""
    folder = path.with_suffix(".segments")
    if not folder.exists():
        return None
    segments = [{"file": f.name, "bytes": f.stat().st_size, "sha256": hashlib.sha256(f.read_bytes()).hexdigest()}
                for f in sorted(folder.glob("segment-????????.jsonl"))]
    head = None
    for head in verified_rows(path):
        pass
    return {"segments": segments, "last_seq": head["seq"] if head else 0, "last_event_sha256": head["sha256"] if head else None}


def journal_copy(controller, identity, *, receipt=False):
    """A full copy of a run for a reader. A run this process drives is copied and read under the engine's lock (its
    state and journal change together). Any other run has no writer here: its files are read without the lock the
    trading loop needs (seconds for a 5-hour battle) by a load that changes nothing in the engine; if the run was
    resumed meanwhile, it is read again under the lock. receipt=True adds journal_receipt read in the same way."""
    path = controller.path / (identity + ".events.jsonl")

    def locked():
        with controller.lock:
            state = deepcopy(controller.load(identity))
            restore_events(path, state, full=True)
        return state

    def finish(state, live):
        if receipt:  # never under the lock: it rereads every segment; a running journal may be ahead of the copy
            try:
                state["journal_receipt"] = journal_receipt(path)
            except (ArenaError, ValueError, OSError):
                if not live:
                    raise
                state["journal_receipt"] = None  # a row being appended right now: ask again
        return state

    with controller.lock:
        live = running_status(controller, identity) is not None
    if live:
        return finish(locked(), True)
    try:
        state = controller.load(identity, reader=True)
        restore_events(path, state, full=True)
    except (ArenaError, ValueError, OSError):
        if running_status(controller, identity) is None:
            raise
        return finish(locked(), True)
    if running_status(controller, identity) is not None:
        return finish(locked(), True)
    return finish(state, False)


def checkpoint(state):
    # Financial facts remain complete. Step history already has a durable append log.
    return {**state, "events": state["events"][-128:], "_journal_checkpoint_seq": state.get("_event_seq", len(state["events"]))}


def append_events(path, state, written, *, recovery=False):
    """Keep the exact ordered history, borrowing only for a bounded close prefix.

    The controller must explicitly forbid this writer mode for discretionary
    submits. Old immutable events may form the prefix before a close; writing
    their evidence never authorizes executing or retrying their old requests.
    No physical/quota/row/segment check is bypassed by the critical retry.
    """
    if type(recovery) is not bool:
        raise ValueError("recovery_writer_flag")
    if state.get("protocol_version", 0) >= 8:
        journal = segmented(path)
        borrowed_rows = borrowed_bytes = 0
        for event in state["events"]:
            if event["seq"] <= written: continue
            if event["seq"] <= journal.seq:
                committed = journal.query(after_seq=event["seq"] - 1, limit=1)
                if not committed or committed[0]["seq"] != event["seq"] or committed[0]["event"] != {k: v for k, v in event.items() if k != "seq"}:
                    raise ArenaError("event_committed_prefix_mismatch")
                written = event["seq"]
                continue
            if event["seq"] != journal.seq + 1: raise ArenaError("event_sequence_mismatch")
            payload = {k: v for k, v in event.items() if k != "seq"}
            try:
                journal.append(payload, critical=_critical_event(event, recovery))
            except StorageBlocked as error:
                if not recovery or str(error) != "evidence_quota_blocks_recording_not_close":
                    raise ArenaError(str(error)) from None
                needed = _row_bytes(journal, payload)
                if (borrowed_rows >= RECOVERY_PREFIX_MAX_ROWS
                        or borrowed_bytes + needed > RECOVERY_PREFIX_MAX_BYTES):
                    raise ArenaError("recovery_prefix_limit") from None
                try:
                    journal.append(payload, critical=True)
                except StorageBlocked as retry_error:
                    raise ArenaError(str(retry_error)) from None
                borrowed_rows += 1
                borrowed_bytes += needed
                # The normal recording budget is unavailable. Never admit
                # another bot's entry while this closing writer drains history.
                state["admission_blocked"] = "evidence_storage"
            written = event["seq"]
        state["evidence_storage"] = journal.capacity()
        return written
    events = state["events"][written:]
    if not events:
        return written
    text = "".join(json.dumps(e, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n" for e in events)
    if (path.stat().st_size if path.exists() else 0) + len(text.encode("utf-8")) > EVENT_LIMIT:
        raise ArenaError("Журнал подій перевищив ліміт; нових входів немає.")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as file:
        file.write(text)
        file.flush()
        os.fsync(file.fileno())
    return len(state["events"])


def restore_events(path, state, *, full=False):
    """Recover only read-only evidence; orders/owned quantities need their checkpoint."""
    if state.get("protocol_version", 0) >= 8 and path.with_suffix(".segments").exists():
        from collections import deque
        restored, after = ([] if full else deque(maxlen=500)), 0
        saved = {e["seq"]: e for e in state["events"]}
        matched = set()
        checkpoint_seq = state.get("_journal_checkpoint_seq", state.get("_event_seq", len(state["events"])))
        for row in verified_rows(path):  # re-verified from the fsynced files; cached metadata isn't integrity evidence
            event = {"seq": row["seq"], **row["event"]}
            if row["seq"] in saved:
                if event != saved[row["seq"]]: raise ArenaError("segmented_checkpoint_mismatch")
                matched.add(row["seq"])
            restored.append(event)
            after = row["seq"]
        if after < checkpoint_seq or matched != set(saved):
            raise ArenaError("segmented_checkpoint_mismatch")
        state.update(events=list(restored), _event_seq=after, _history_compact=after > len(restored))
        return after
    if not path.exists():
        if "_journal_checkpoint_seq" in state:
            raise ArenaError("Журнал подій відсутній; потрібна ручна перевірка.")
        return len(state["events"])
    if path.stat().st_size > EVENT_LIMIT:
        raise ArenaError("Журнал подій перевищив ліміт.")
    saved = state["events"]
    checkpoint_seq = state.get("_journal_checkpoint_seq", len(saved))
    if type(checkpoint_seq) is not int or checkpoint_seq < len(saved):
        raise ArenaError("Нечинний знімок журналу подій.")
    start_seq = checkpoint_seq - len(saved) + 1
    restored = []
    with path.open(encoding="utf-8") as file:
        for line in file:
            if len(line) > 131072:
                raise ArenaError("Нечинний рядок журналу подій.")
            try:
                event = json.loads(line)
                count = len(restored) + 1
                if event["seq"] != count:
                    raise ValueError()
                if start_seq <= count <= checkpoint_seq:
                    if event != saved[count - start_seq]:
                        raise ValueError()
                restored.append(event)
            except (ValueError, KeyError, TypeError):
                raise ArenaError("Пошкоджено журнал подій; потрібна ручна перевірка.") from None
    if len(restored) < checkpoint_seq:
        raise ArenaError("Журнал подій обірвано; потрібна ручна перевірка.")
    state["events"] = restored
    return len(restored)


def compact_memory(state):
    if state.get("protocol_version", 0) < 8: return
    state["events"] = state["events"][-500:]
    state["frames"] = state.get("frames", [])[-1440:]
    for bot in state["bots"]: bot["curve"] = bot.get("curve", [])[-1440:]
    state["_history_compact"] = state.get("_event_seq", 0) > len(state["events"])
