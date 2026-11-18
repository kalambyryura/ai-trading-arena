"""Research reference: constant-memory health, segmented evidence and backups.

No network/executor imports. Tests use temp directories. Exhaustion raises an
operational error for an admission gate; it NEVER supplies a position-close
reason. Private backups stay local/ignored; this is not a public exporter.
"""
from collections import deque
from contextlib import closing
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
import uuid


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def replace_with_retry(source, target, pauses=(0.05, 0.1, 0.2, 0.3, 0.35)):
    """os.replace that waits out a transient Windows sharing lock on the files (antivirus, indexer) for at most 1 s in
    total; a lock that lasts longer, or any other error, still raises."""
    for pause in pauses:
        try:
            return os.replace(source, target)
        except PermissionError:
            time.sleep(pause)
    return os.replace(source, target)


def atomic_write(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".tmp")
    with pending.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    replace_with_retry(pending, path)


class StorageBlocked(ValueError):
    pass


@dataclass
class HealthState:
    """A fresh UI/server heartbeat is not a fresh market/account/protection read."""
    boot_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    latest: dict = field(default_factory=dict)
    issues: set = field(default_factory=set)
    last_durable_seq: int = 0

    def mark(self, kind, mono, *, durable_seq=None):
        if kind not in {"loop", "market", "account", "protection", "checkpoint", "backup"}:
            raise ValueError("heartbeat_kind")
        if kind in self.latest and mono < self.latest[kind]:
            raise ValueError("heartbeat_monotonic_regression")
        self.latest[kind] = mono
        if durable_seq is not None:
            if type(durable_seq) is not int or durable_seq < self.last_durable_seq:
                raise ValueError("durable_sequence_regression")
            self.last_durable_seq = durable_seq

    def status(self, now_mono):
        ages = {key: max(0, now_mono - self.latest[key]) if key in self.latest else None
                for key in ("loop", "market", "account", "protection", "checkpoint", "backup")}
        limits = {"loop": 15, "market": 10, "account": 30, "protection": 30, "checkpoint": 60}
        stale = [key for key, cap in limits.items() if ages[key] is None or ages[key] > cap]
        return {"schema": "arena-health-v1", "boot_id": self.boot_id, "ages_seconds": ages,
                "stale_components": stale, "ops_reasons": sorted(self.issues),
                "last_durable_seq": self.last_durable_seq, "new_entry_permitted": not stale and not self.issues,
                "force_position_close": False}


class SegmentedJournal:
    """Hash-linked, fsynced segments with bounded memory and constant latest state.

    Init verifies existing segments while retaining only `tail_rows` events.
    Critical reserves are for intent/fill/protection/settlement facts, not extra
    entries. Caller must separately persist its bounded financial checkpoint.
    """
    def __init__(self, path, *, segment_bytes=8 * 1024 * 1024, total_bytes=256 * 1024 * 1024,
                 critical_reserve_bytes=8 * 1024 * 1024, min_free_bytes=64 * 1024 * 1024,
                 max_row_bytes=131072, tail_rows=128, max_segments=128, free_space=None):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.segment_bytes, self.total_limit = segment_bytes, total_bytes
        self.critical_reserve, self.min_free, self.max_row_bytes = critical_reserve_bytes, min_free_bytes, max_row_bytes
        if min(segment_bytes, total_bytes, max_row_bytes, tail_rows, max_segments) <= 0 or not 0 <= critical_reserve_bytes < total_bytes:
            raise ValueError("storage_policy")
        self.max_segments, self.free_space = max_segments, free_space or (lambda path: shutil.disk_usage(path).free)
        self.seq, self.last_hash, self.total_used = 0, "0" * 64, 0
        self.tail = deque(maxlen=tail_rows)
        self.segments = []
        self._active_hash = hashlib.sha256()
        self._restore()

    def _restore(self):
        files = sorted(self.path.glob("segment-????????.jsonl"))
        if len(files) > self.max_segments:
            raise StorageBlocked("segment_count_exceeds_policy")
        for index, path in enumerate(files, 1):
            if path.name != f"segment-{index:08}.jsonl" or path.stat().st_size > self.segment_bytes:
                raise StorageBlocked("segment_shape")
            digest, first = hashlib.sha256(), self.seq + 1
            with path.open("rb") as stream:
                for line in stream:
                    if not line.endswith(b"\n") or len(line) > self.max_row_bytes:
                        raise StorageBlocked("journal_incomplete_or_oversize_line")
                    try:
                        row = json.loads(line)
                        data = {key: value for key, value in row.items() if key != "sha256"}
                        row_hash = hashlib.sha256(encode(data)).hexdigest()
                        if row.get("seq") != self.seq + 1 or row.get("previous_sha256") != self.last_hash or row.get("sha256") != row_hash:
                            raise ValueError()
                    except (ValueError, TypeError, KeyError, AttributeError):
                        raise StorageBlocked("journal_chain_corrupt") from None
                    self.seq, self.last_hash = row["seq"], row_hash
                    self.tail.append(row)
                    digest.update(line)
                    self.total_used += len(line)
            self.segments.append({"file": path.name, "first_seq": first, "last_seq": self.seq,
                                  "bytes": path.stat().st_size, "sha256": digest.hexdigest()})
            self._active_hash = digest
        if self.total_used > self.total_limit:
            raise StorageBlocked("journal_total_exceeds_policy")
        self._save_index()

    def _save_index(self):
        atomic_write(self.path / "index.json", encode({"schema": "segmented-evidence-v1", "last_seq": self.seq,
                      "last_event_sha256": self.last_hash, "segments": self.segments}))

    def capacity(self):
        free = self.free_space(self.path)
        return {"used_bytes": self.total_used, "limit_bytes": self.total_limit,
                "normal_headroom_bytes": max(0, self.total_limit - self.critical_reserve - self.total_used),
                "critical_headroom_bytes": max(0, self.total_limit - self.total_used),
                "free_bytes": free, "min_free_bytes": self.min_free, "segments": len(self.segments),
                "tail_rows_in_memory": len(self.tail), "force_position_close": False}

    def append(self, event, *, critical=False):
        if not isinstance(event, dict) or not event or {"seq", "previous_sha256", "sha256"} & set(event):
            raise ValueError("event_shape")
        data = {"seq": self.seq + 1, "previous_sha256": self.last_hash, "event": event}
        row_hash = hashlib.sha256(encode(data)).hexdigest()
        row = {**data, "sha256": row_hash}
        line = encode(row) + b"\n"
        if len(line) > min(self.max_row_bytes, self.segment_bytes):
            raise StorageBlocked("event_row_too_large")
        quota = self.total_limit if critical else self.total_limit - self.critical_reserve
        if self.total_used + len(line) > quota:
            raise StorageBlocked("evidence_quota_blocks_recording_not_close")
        if self.free_space(self.path) - len(line) < self.min_free:
            raise StorageBlocked("disk_reserve_blocks_writes_not_close")
        if not self.segments or self.segments[-1]["bytes"] + len(line) > self.segment_bytes:
            if len(self.segments) >= self.max_segments:
                raise StorageBlocked("segment_capacity_blocks_recording_not_close")
            self.segments.append({"file": f"segment-{len(self.segments) + 1:08}.jsonl", "first_seq": self.seq + 1,
                                  "last_seq": self.seq, "bytes": 0, "sha256": hashlib.sha256().hexdigest()})
            self._active_hash = hashlib.sha256()
        metadata = self.segments[-1]
        with (self.path / metadata["file"]).open("ab") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
        self._active_hash.update(line)
        self.seq, self.last_hash = row["seq"], row_hash
        self.total_used += len(line)
        metadata.update(last_seq=self.seq, bytes=metadata["bytes"] + len(line), sha256=self._active_hash.hexdigest())
        self.tail.append(row)
        self._save_index()
        return {"seq": self.seq, "sha256": row_hash, "segment": metadata["file"]}

    def query(self, *, after_seq=0, limit=500):
        if type(after_seq) is not int or after_seq < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("query_bounds")
        result = []
        for segment in self.segments:
            if segment["last_seq"] <= after_seq:
                continue
            with (self.path / segment["file"]).open("rb") as stream:
                for line in stream:
                    row = json.loads(line)
                    if row["seq"] > after_seq:
                        result.append(row)
                        if len(result) == limit:
                            return result
        return result


def checkpoint_payload(*, manifest_hash, event_seq, owned_orders, protection, financial_state):
    """Caller passes bounded facts only; never events/frames/curves/raw accounts."""
    if type(event_seq) is not int or event_seq < 0 or len(owned_orders) > 100 or len(protection) > 100:
        raise ValueError("checkpoint_bounds")
    return encode({"schema": "ops-checkpoint-v1", "manifest_sha256": manifest_hash, "event_seq": event_seq,
                   "owned_orders": owned_orders, "protection": protection, "financial_state": financial_state})


def create_private_backup(*, runtime_root, destination, files, max_bytes=512 * 1024 * 1024):
    """Exact explicit allowlist only. Source .sqlite3 uses SQLite's online backup.

    Invoke under the controller checkpoint lock. Keep returned recovery metadata
    private. This helper neither restores nor replaces a current billing ledger.
    """
    root, output = Path(runtime_root).resolve(), Path(destination).resolve()
    if not output.is_relative_to(root) or output.exists() or not isinstance(files, dict) or not 1 <= len(files) <= 32:
        raise ValueError("backup_destination_or_manifest")
    selected = []
    total = 0
    for name, value in files.items():
        path = Path(value).resolve()
        if (Path(name).name != name or not path.is_relative_to(root) or not path.is_file()
                or path.suffix not in {".json", ".jsonl", ".sqlite3"}
                or any(word in name.lower() for word in ("secret", "credential", "passphrase", ".env"))):
            raise ValueError("backup_file_allowlist")
        total += path.stat().st_size
        selected.append((name, path))
    if total > max_bytes:
        raise StorageBlocked("backup_capacity")
    output.mkdir(parents=True)
    receipt = {"schema": "private-recovery-backup-v1", "public_export": False,
               "auto_restore": False, "execution_permitted": False, "files": []}
    for name, path in selected:
        target = output / name
        if path.suffix == ".sqlite3":
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source:
                with closing(sqlite3.connect(target)) as backup:
                    source.backup(backup)
        else:
            with path.open("rb") as source:
                atomic_write(target, source.read(max_bytes + 1))
        if target.stat().st_size > max_bytes:
            raise StorageBlocked("backup_copy_capacity")
        receipt["files"].append({"name": name, "source_runtime_path": path.relative_to(root).as_posix(),
                                 "bytes": target.stat().st_size, "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                                 "billing_ledger_may_never_overwrite_newer_current": path.suffix == ".sqlite3"})
    atomic_write(output / "receipt.json", encode(receipt))
    return receipt


def verify_private_backup(path):
    path = Path(path).resolve()
    receipt = json.loads((path / "receipt.json").read_bytes())
    if receipt.get("schema") != "private-recovery-backup-v1" or receipt.get("public_export") is not False:
        raise ValueError("backup_receipt")
    for row in receipt["files"]:
        name = row["name"]
        if Path(name).name != name:
            raise ValueError("backup_path_escape")
        target = path / name
        if target.stat().st_size != row["bytes"] or hashlib.sha256(target.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("backup_hash_mismatch")
    return {"verified": True, "execution_permitted": False,
            "recovery_requires_owner_reconciliation": True, "overwrite_current_billing_ledger": False}
