"""Local public-recording repository and isolated private-backup service.

No exchange account, order, Jev or trading-control imports/callbacks. Public
recording is explicit; constructing/status/querying this service never fetches
anything. Owner SHADOW is implemented separately in owner_shadow_reference.
"""
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import threading
import time
import uuid

from .ops_storage import StorageBlocked, atomic_write, encode, create_private_backup
from .public_recorder import PublicRecorder, public_projection, fixture_raw

IDENTITY = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}")
MAX_LINE = 131072


def identity(value):
    if not isinstance(value, str) or not IDENTITY.fullmatch(value):
        raise ValueError("local_identity")
    return value


def read_json(path, max_bytes=131072):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > max_bytes:
        raise ValueError("local_json_size_or_missing")
    return json.loads(path.read_bytes())


def sha_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1048576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def finite_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def verified_capture(record):
    if not isinstance(record, dict) or set(record) != {"mode", "at", "sha256", "raw", "capture"}:
        raise ValueError("public_record_outer_projection")
    capture = record["capture"]
    keys = {"boot_id", "sample_seq", "wall_sent_at", "wall_received_at", "mono_sent", "mono_received", "quality"}
    if not isinstance(capture, dict) or set(capture) != keys:
        raise ValueError("public_capture_projection")
    if (not isinstance(capture["boot_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", capture["boot_id"])
            or type(capture["sample_seq"]) is not int or not 1 <= capture["sample_seq"] <= 1000000
            or any(not finite_number(capture[k]) for k in {"wall_sent_at", "wall_received_at", "mono_sent", "mono_received"})):
        raise ValueError("public_capture_value")
    quality = capture["quality"]
    if (not isinstance(quality, dict) or set(quality) != {"reasons", "wall_delta_s", "mono_delta_s", "gap_s",
                                                        "invalidate_temporal_state", "force_position_close"}
            or quality["force_position_close"] is not False or type(quality["invalidate_temporal_state"]) is not bool
            or not isinstance(quality["reasons"], list) or len(quality["reasons"]) > 3
            or any(v not in {"wall_clock_jump", "observation_gap", "quote_regression"} for v in quality["reasons"])
            or any(not finite_number(quality[k]) for k in {"wall_delta_s", "mono_delta_s", "gap_s"})):
        raise ValueError("public_quality_projection")


def publish_new(pending, destination):
    """Atomic create without replacing an existing file, including a race."""
    os.link(pending, destination)
    Path(pending).unlink()


class FixturePublicSequence:
    """Pure fake public feed advancing 5s per observation, no transport/key."""
    is_fake = True

    def __init__(self, step_seconds=5):
        self.raw = fixture_raw()
        self.step_ms = step_seconds * 1000
        self.last_received_ms = int(self.raw["clock"][0]["ts"])
        self.calls = 0

    def wall(self):
        return self.last_received_ms / 1000

    def __call__(self, name):
        self.calls += 1
        result = deepcopy(self.raw[name])
        if name == "clock":
            self.last_received_ms = int(result[0]["ts"])
            following = self.last_received_ms + self.step_ms
            for key in ("clock", "ticker", "funding"):
                self.raw[key][0]["ts"] = str(following)
            last_close = int(self.raw["candles"][-1][0]) + 60000
            shift = ((following - last_close) // 60000) * 60000
            if shift > 0:
                for row in self.raw["candles"]:
                    row[0] = str(int(row[0]) + shift)
        return result


class PublicRecorderRepository:
    def __init__(self, root, *, build_snapshot_fn, get_factory=None, source="FIXTURE",
                 wall=time.time, mono=time.monotonic, max_sessions=64,
                 max_repository_bytes=512 * 1024 * 1024, recorder_options=None):
        if source not in {"FIXTURE", "OKX_LIVE_PUBLIC"} or not callable(build_snapshot_fn):
            raise ValueError("public_service_source")
        if type(max_sessions) is not int or not 1 <= max_sessions <= 4096:
            raise ValueError("public_service_session_capacity")
        if type(max_repository_bytes) is not int or max_repository_bytes <= 0:
            raise ValueError("public_service_byte_capacity")
        self.root = Path(root).resolve()
        self.sessions = self.root / "sessions"
        self.sessions.mkdir(parents=True, exist_ok=True)
        self.build_snapshot, self.get_factory, self.source = build_snapshot_fn, get_factory, source
        self.wall, self.mono = wall, mono
        self.max_sessions, self.max_repository_bytes = max_sessions, max_repository_bytes
        self.recorder_options = dict(recorder_options or {})
        self.lock = threading.RLock()
        self.worker, self.stop_event = None, threading.Event()
        self.status_path, self.lease_path = self.root / "status.json", self.root / "recorder.lock"
        self.stop_path = self.root / "stop-recording.json"
        if not self.status_path.exists():
            self._write_status({"status": "IDLE", "source": source, "dataset_id": None})

    def _write_status(self, state):
        value = {**state, "schema": "public-recorder-service-v1", "affects_trading": False,
                 "paid_calls": False, "private_account_data": False, "automatic_execution": False}
        atomic_write(self.status_path, encode(value))
        return value

    def capacity(self):
        sessions = [p for p in self.sessions.iterdir() if p.is_dir()]
        if len(sessions) > self.max_sessions:
            raise StorageBlocked("public_repository_session_capacity")
        used = 0
        for path in self.sessions.rglob("*"):
            if path.is_symlink():
                raise StorageBlocked("public_repository_symlink")
            if path.is_file():
                used += path.stat().st_size
                if used > self.max_repository_bytes:
                    break
        return {"sessions": len(sessions), "maximum_sessions": self.max_sessions,
                "used_bytes": used, "limit_bytes": self.max_repository_bytes,
                "force_position_close": False, "affects_trading": False}

    def status(self):
        with self.lock:
            value = read_json(self.status_path)
            value["local_worker_alive"] = bool(self.worker and self.worker.is_alive())
            value["lease_exists"] = self.lease_path.exists()
            value["capacity"] = self.capacity()
            return value

    def list_datasets(self, *, offset=0, limit=50):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("dataset_page_bounds")
        paths = sorted(p for p in self.sessions.iterdir() if p.is_dir())
        if len(paths) > self.max_sessions:
            raise StorageBlocked("public_repository_session_capacity")
        rows = []
        for path in paths[offset:offset + limit]:
            manifest = read_json(path / "manifest.json", 2 * 1024 * 1024)
            rows.append({"id": path.name, "status": manifest["status"], "source": manifest["source"],
                         "samples": manifest["samples"], "observations": manifest.get("observations", 0),
                         "gaps": manifest.get("gaps", 0), "epochs": len(manifest["epochs"])})
        return {"datasets": rows, "total": len(paths), "offset": offset, "limit": limit}

    def start_recording(self, *, owner_local=False, interval_seconds=5, max_observations=17280):
        if self.source != "FIXTURE" and owner_local is not True:
            raise ValueError("owner_public_recording_required")
        if not callable(self.get_factory):
            raise ValueError("public_transport_factory_required")
        if (not finite_number(interval_seconds) or interval_seconds < (0 if self.source == "FIXTURE" else 1)
                or interval_seconds > 60 or type(max_observations) is not int or not 1 <= max_observations <= 1000000):
            raise ValueError("recording_quantum_policy")
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ValueError("public_recording_already_active")
            capacity = self.capacity()
            if capacity["sessions"] >= self.max_sessions or capacity["used_bytes"] + MAX_LINE > capacity["limit_bytes"]:
                raise StorageBlocked("public_repository_capacity_recording_only")
            dataset_id = uuid.uuid4().hex
            # A second process, or an unacknowledged abandoned lease, cannot
            # silently resume or replace the previous public recording.
            with self.lease_path.open("x", encoding="utf-8") as lease:
                json.dump({"dataset_id": dataset_id, "pid": os.getpid(), "source": self.source}, lease)
                lease.flush()
                os.fsync(lease.fileno())
            self.stop_event = threading.Event()
            self._write_status({"status": "STARTING_RECORDING", "dataset_id": dataset_id,
                                "source": self.source, "worker_pid": os.getpid(), "created_wall": self.wall()})
            self.worker = threading.Thread(target=self._run, args=(dataset_id, interval_seconds, max_observations),
                                           name="public-recorder", daemon=True)
            self.worker.start()
            return {"dataset_id": dataset_id, "status": "STARTING_RECORDING", "affects_trading": False}

    def _requested_stop(self, dataset_id):
        if self.stop_event.is_set():
            return True
        if not self.stop_path.exists():
            return False
        try:
            return read_json(self.stop_path, 4096).get("dataset_id") == dataset_id
        except (OSError, ValueError, AttributeError):
            return False

    def _run(self, dataset_id, interval, maximum):
        recorder = None
        try:
            feed = self.get_factory()
            if self.source == "FIXTURE" and getattr(feed, "is_fake", False) is not True:
                raise ValueError("fixture_public_transport_required")
            recorder_wall = getattr(feed, "wall", self.wall) if self.source == "FIXTURE" else self.wall
            recorder = PublicRecorder(self.sessions / dataset_id, get_public=feed, build_snapshot_fn=self.build_snapshot,
                                      source=self.source, wall=recorder_wall, mono=self.mono, **self.recorder_options)
            last_success_wall = None
            for _ in range(maximum):
                if self._requested_stop(dataset_id):
                    recorder.stop_recording("owner_stopped_recording_only")
                    break
                capacity = self.capacity()
                if capacity["used_bytes"] + MAX_LINE > capacity["limit_bytes"]:
                    recorder.stop_recording("public_repository_byte_capacity")
                    break
                before = recorder.samples
                state = recorder.tick()
                if recorder.samples > before:
                    last_success_wall = self.wall()
                with self.lock:
                    self._write_status({"status": state["status"], "source": self.source, "dataset_id": dataset_id,
                                        "worker_pid": os.getpid(), "samples": state["samples"], "gaps": state.get("gaps", 0),
                                        "observations": state.get("observations", 0), "last_tick_wall": self.wall(),
                                        "last_success_wall": last_success_wall, "recorder_boot_id": state["boot_id"],
                                        "reason": state.get("reason"), "projected_record_bytes": state.get("projected_record_bytes", 0)})
                if state["status"] != "RECORDING" or self.stop_event.wait(interval):
                    break
            recorder.stop_recording("owner_stopped_recording_only" if self._requested_stop(dataset_id)
                                    else "collection_quantum_completed")
            with self.lock:
                current = read_json(self.status_path)
                self._write_status({**current, "status": "STOPPED_RECORDING", "reason": recorder.state.get("reason"),
                                    "finished_wall": self.wall(), "samples": recorder.samples})
        except Exception:
            if recorder is not None:
                try:
                    recorder.stop_recording("public_service_error")
                except (OSError, ValueError):
                    pass
            with self.lock:
                self._write_status({"status": "ERROR_RECORDING", "dataset_id": dataset_id, "source": self.source,
                                    "reason": "public_service_failed_no_provider_text", "finished_wall": self.wall()})
        finally:
            # Delete only this worker's exact lease, never another worker's file.
            try:
                if read_json(self.lease_path, 4096).get("dataset_id") == dataset_id:
                    self.lease_path.unlink()
            except (OSError, ValueError, AttributeError):
                pass

    def request_stop_recording(self):
        with self.lock:
            state = read_json(self.status_path)
            dataset_id = state.get("dataset_id")
            if not dataset_id or state.get("status") not in {"RECORDING", "STARTING_RECORDING"}:
                return {"status": "NO_ACTIVE_RECORDING", "affects_trading": False}
            atomic_write(self.stop_path, encode({"dataset_id": dataset_id, "operation": "STOP_RECORDING_ONLY"}))
            self.stop_event.set()
            return {"status": "STOP_RECORDING_REQUESTED", "dataset_id": dataset_id, "affects_trading": False}

    def wait_recording(self, timeout=30):
        worker = self.worker
        if worker:
            worker.join(timeout)
        return self.status()

    def iter_records(self, dataset_id):
        root = self.sessions / identity(dataset_id)
        manifest = read_json(root / "manifest.json", 2 * 1024 * 1024)
        if manifest.get("schema") != "public-recording-v1" or manifest.get("status") == "RECORDING":
            raise ValueError("sealed_public_recording_required")
        if not isinstance(manifest.get("epochs"), list) or len(manifest["epochs"]) > 4096:
            raise ValueError("public_epoch_manifest")
        global_sample, total_bytes = 0, 0
        for epoch in manifest["epochs"]:
            name = epoch.get("directory")
            if not isinstance(name, str) or not re.fullmatch(r"epoch-[0-9]{8}", name):
                raise ValueError("public_epoch_directory")
            path = root / name
            previous, sequence = "0" * 64, 0
            segments = sorted(path.glob("segment-????????.jsonl"))
            if len(segments) > 4096:
                raise ValueError("public_segment_capacity")
            for index, segment in enumerate(segments, 1):
                if (segment.name != f"segment-{index:08}.jsonl" or segment.is_symlink()
                        or not segment.resolve().is_relative_to(root.resolve())):
                    raise ValueError("public_segment_path")
                with segment.open("rb") as stream:
                    for line in stream:
                        total_bytes += len(line)
                        if not line.endswith(b"\n") or len(line) > MAX_LINE or total_bytes > self.max_repository_bytes:
                            raise ValueError("public_segment_line_or_capacity")
                        row = json.loads(line)
                        data = {key: value for key, value in row.items() if key != "sha256"}
                        digest = hashlib.sha256(encode(data)).hexdigest()
                        if (row.get("seq") != sequence + 1 or row.get("previous_sha256") != previous
                                or row.get("sha256") != digest):
                            raise ValueError("public_segment_chain")
                        sequence, previous = row["seq"], digest
                        event = row.get("event")
                        if not isinstance(event, dict) or event.get("kind") not in {"public_market", "public_gap"}:
                            raise ValueError("public_record_kind")
                        if event["kind"] == "public_gap":
                            yield {"kind": "public_gap"}
                            continue
                        record = event["record"]
                        verified_capture(record)
                        if record["mode"] != ("FIXTURE" if manifest["source"] == "FIXTURE" else "OKX_LIVE"):
                            raise ValueError("public_record_mode_source")
                        projected = public_projection(record["raw"])
                        if projected != record["raw"]:
                            raise ValueError("public_record_projection")
                        snapshot = self.build_snapshot(projected, record["at"])
                        if snapshot["sha256"] != record.get("sha256"):
                            raise ValueError("recorded_public_snapshot_hash_mismatch")
                        global_sample += 1
                        if record.get("capture", {}).get("sample_seq") != global_sample:
                            raise ValueError("public_sample_sequence")
                        yield {"kind": "public_market", "record": record, "snapshot": snapshot}
            if previous != epoch.get("last_event_sha256"):
                raise ValueError("public_epoch_final_hash")
        if global_sample != manifest.get("samples"):
            raise ValueError("public_manifest_sample_count")

    def export_training_tape(self, dataset_id, destination, *, max_quotes=36000):
        """Stream verified raw rows; read_recorded rebuilds all derived features."""
        if type(max_quotes) is not int or not 20 <= max_quotes <= 36000:
            raise ValueError("training_tape_quote_capacity")
        destination = Path(destination).resolve()
        if destination.exists() or destination.is_relative_to(self.sessions):
            raise ValueError("training_export_destination")
        destination.parent.mkdir(parents=True, exist_ok=True)
        receipt_path = destination.with_name(destination.name + ".receipt.json")
        if receipt_path.exists():
            raise ValueError("training_export_receipt_exists")
        pending = destination.with_name(destination.name + ".pending-" + uuid.uuid4().hex)
        count = gaps = 0
        first_at = previous_at = previous_quote = None
        with pending.open("xb") as output:
            for event in self.iter_records(dataset_id):
                if event["kind"] == "public_gap":
                    gaps += 1
                    continue
                record, snapshot = event["record"], event["snapshot"]
                if count >= max_quotes:
                    raise ValueError("training_tape_quote_capacity_no_truncation")
                if previous_at is not None and (record["at"] <= previous_at or snapshot["quote_ts"] < previous_quote):
                    raise ValueError("training_tape_time_order_requires_separate_recording")
                first_at = record["at"] if first_at is None else first_at
                previous_at, previous_quote = record["at"], snapshot["quote_ts"]
                output.write(encode(record) + b"\n")
                count += 1
            if count < 20 or previous_at - first_at < 90:
                raise ValueError("insufficient_recorded_training_tape")
            output.flush()
            os.fsync(output.fileno())
        publish_new(pending, destination)
        manifest = read_json(self.sessions / identity(dataset_id) / "manifest.json", 2 * 1024 * 1024)
        receipt = {"schema": "recorded-training-tape-v1", "dataset_id": dataset_id, "source": manifest["source"],
                   "samples": count, "gaps_omitted_but_audited": gaps, "elapsed_recorded_seconds": previous_at - first_at,
                   "sha256": sha_file(destination), "derived_features_trusted": False,
                   "fixture_only": manifest["source"] == "FIXTURE", "actual_elapsed_24h_proven": False,
                   "execution_permitted": False, "paid_calls": False}
        with receipt_path.open("xb") as output:
            output.write(encode(receipt))
            output.flush()
            os.fsync(output.fileno())
        return receipt


class BackupRepository:
    """Source is read-only; restore creates an inert isolated copy only."""
    def __init__(self, current_runtime, backup_root, restore_root, *, max_backups=64, max_restores=64,
                 max_bytes=512 * 1024 * 1024, repository_bytes=2 * 1024 * 1024 * 1024,
                 min_free_bytes=64 * 1024 * 1024):
        self.current_runtime, self.backup_root, self.restore_root = map(lambda p: Path(p).resolve(),
                                                                     (current_runtime, backup_root, restore_root))
        self.protected_runtime_roots = {self.current_runtime, *[p for p in self.current_runtime.parents if p.name.lower() == "runtime"]}
        if (not self.backup_root.is_relative_to(self.current_runtime)
                or any(self.restore_root.is_relative_to(p) or p.is_relative_to(self.restore_root)
                       for p in self.protected_runtime_roots)):
            raise ValueError("backup_restore_isolation_roots")
        if (type(max_backups) is not int or not 1 <= max_backups <= 4096
                or type(max_restores) is not int or not 1 <= max_restores <= 4096
                or type(max_bytes) is not int or max_bytes <= 0
                or type(repository_bytes) is not int or repository_bytes <= 0
                or type(min_free_bytes) is not int or min_free_bytes < 0):
            raise ValueError("backup_repository_capacity")
        self.max_backups, self.max_restores, self.max_bytes = max_backups, max_restores, max_bytes
        self.repository_bytes, self.min_free_bytes = repository_bytes, min_free_bytes

    def _directory_capacity(self, directory, incoming, count_limit):
        directory.mkdir(parents=True, exist_ok=True)
        if sum(p.is_dir() for p in directory.iterdir()) >= count_limit:
            raise StorageBlocked("backup_or_restore_count_capacity")
        used = 0
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise StorageBlocked("backup_or_restore_repository_symlink")
            if path.is_file():
                used += path.stat().st_size
                if used + incoming > self.repository_bytes:
                    raise StorageBlocked("backup_or_restore_byte_capacity")
        if used + incoming > self.repository_bytes or shutil.disk_usage(directory).free - incoming < self.min_free_bytes:
            raise StorageBlocked("backup_or_restore_disk_capacity")

    def create(self, files, *, backup_id=None):
        backup_id = identity(backup_id or uuid.uuid4().hex)
        if not isinstance(files, dict) or {"receipt.json", "recovery_manifest.json"} & set(files):
            raise ValueError("backup_file_manifest")
        selected = {}
        for name, relative in files.items():
            if not isinstance(name, str) or not isinstance(relative, str):
                raise ValueError("backup_source_path")
            path = (self.current_runtime / relative).resolve()
            if (not path.is_relative_to(self.current_runtime) or path.is_relative_to(self.backup_root)
                    or any(word in path.name.lower() for word in ("secret", "credential", "passphrase", ".env"))):
                raise ValueError("backup_source_path")
            selected[name] = path
        incoming = sum(path.stat().st_size for path in selected.values())
        self._directory_capacity(self.backup_root, incoming + 131072, self.max_backups)
        receipt = create_private_backup(runtime_root=self.current_runtime, destination=self.backup_root / backup_id,
                                        files=selected, max_bytes=self.max_bytes)
        return {"backup_id": backup_id, **receipt}

    def _verified_receipt(self, backup_id):
        root = self.backup_root / identity(backup_id)
        if root.is_symlink() or not root.resolve().is_relative_to(self.backup_root):
            raise ValueError("backup_root_path")
        receipt = read_json(root / "receipt.json")
        if (receipt.get("schema") != "private-recovery-backup-v1" or receipt.get("public_export") is not False
                or receipt.get("auto_restore") is not False or receipt.get("execution_permitted") is not False
                or not isinstance(receipt.get("files"), list) or not 1 <= len(receipt["files"]) <= 32):
            raise ValueError("backup_receipt")
        total, names = 0, set()
        for row in receipt["files"]:
            name = row.get("name")
            if (not isinstance(name, str) or Path(name).name != name or name in names
                    or name in {"receipt.json", "recovery_manifest.json"} or Path(name).suffix not in {".json", ".jsonl", ".sqlite3"}
                    or any(word in name.lower() for word in ("secret", "credential", "passphrase", ".env"))
                    or type(row.get("bytes")) is not int or row["bytes"] < 0
                    or not isinstance(row.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"])):
                raise ValueError("backup_file_receipt")
            path = root / name
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError("backup_file_path")
            total += row["bytes"]
            if total > self.max_bytes or path.stat().st_size != row["bytes"] or sha_file(path) != row["sha256"]:
                raise ValueError("backup_hash_or_capacity")
            names.add(name)
        return root, receipt

    def verify(self, backup_id):
        _, receipt = self._verified_receipt(backup_id)
        return {"backup_id": backup_id, "verified": True, "files": len(receipt["files"]),
                "execution_permitted": False, "overwrite_current_billing_ledger": False,
                "recovery_requires_owner_reconciliation": True}

    def restore_isolated(self, backup_id, *, restore_id=None):
        root, receipt = self._verified_receipt(backup_id)
        restore_id = identity(restore_id or uuid.uuid4().hex)
        destination = self.restore_root / restore_id
        resolved = destination.resolve()
        if (destination.exists() or not resolved.is_relative_to(self.restore_root)
                or any(resolved.is_relative_to(p) or p.is_relative_to(resolved) for p in self.protected_runtime_roots)):
            raise ValueError("restore_requires_new_isolated_directory")
        self._directory_capacity(self.restore_root, sum(r["bytes"] for r in receipt["files"]) + 131072, self.max_restores)
        destination.mkdir(parents=True)
        for row in receipt["files"]:
            source, target = root / row["name"], destination / row["name"]
            with source.open("rb") as incoming, target.open("xb") as outgoing:
                for chunk in iter(lambda: incoming.read(1048576), b""):
                    outgoing.write(chunk)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if sha_file(target) != row["sha256"]:
                raise ValueError("restore_source_changed_hash")
        recovery = {"schema": "isolated-recovery-copy-v1", "backup_id": backup_id, "restore_id": restore_id,
                    "status": "ISOLATED_REVIEW_ONLY", "execution_permitted": False, "automatic_execution": False,
                    "overwrite_current_runtime": False, "overwrite_current_billing_ledger": False,
                    "reconciliation_required_before_any_owner_execution": True,
                    "files": [{"name": r["name"], "sha256": r["sha256"]} for r in receipt["files"]]}
        with (destination / "recovery_manifest.json").open("xb") as output:
            output.write(encode(recovery))
            output.flush()
            os.fsync(output.fileno())
        return recovery
