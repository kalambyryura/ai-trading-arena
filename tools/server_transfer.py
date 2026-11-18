"""Owner-operated cold transfer. No exchange/model calls, launches or ledger reset."""
from contextlib import ExitStack
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trading_lab.arena import exclusive
from trading_lab.exchange_accounts import profiles, SETTINGS_FILE
from trading_lab.one_trade import TERMINAL_RUNS

SCHEMA = "jev-arena-cold-transfer/v1"
IMPORTED = "runtime/server_state_import.json"
HANDOFF = "runtime/server_handoff.json"
FOLDERS = ("exchange_v1", "arena_v4", "workshop_v1", "league_v1",
           "market_recorder_v1", "web_panel", "web_runs")
REQUIRED = ("runtime/exchange_v1/manifest.json", "runtime/arena_v4/manifest.json",
            "runtime/workshop_v1/manifest.json", "runtime/league_v1/manifest.json",
            "runtime/exchange_v1/workshop_jev_e038.sqlite3",
            "runtime/exchange_v1/arena_jev_e039.sqlite3",
            "runtime/arena_v4/shared_jev_e037.sqlite3")
MAX_BYTES = 2 * 1024**3
MAX_FILES = 100000
MAX_HANDOFF_CHAIN = 32
MAX_HANDOFF_MARKER = 32768
REISSUE_SCHEMA = "jev-arena-cold-reissue/v1"
STATE_EXCLUDED_SUFFIXES = (".lock", ".tmp", ".log", ".py", ".pyc")
# Only our fixed reason codes may reach the console, never arbitrary exception text.
FAILURE_CODES = frozenset({
    "invalid_state_file", "invalid_state_json", "unsafe_archive_path",
    "unsafe_windows_archive_path", "source_path_outside_workspace",
    "symlink_or_reparse_point", "state_file_missing", "position_not_known_flat",
    "invalid_run_manifest", "unfinished_battle_owner_must_finish_first",
    "unfinished_legacy_cycle", "legacy_position_not_flat",
    "unexpected_credential_file_in_state", "credential_file_size",
    "incomplete_state_no_fresh_ledger_fallback", "transfer_capacity",
    "output_exists_or_inside_runtime", "workspace_already_handed_off",
    "close_idle_source_panel_before_export", "pending_transfer_requires_review",
    "source_changed_during_cold_export", "duplicate_archive_paths",
    "invalid_archive_member", "transfer_manifest_missing", "transfer_manifest_capacity",
    "invalid_transfer_manifest", "invalid_credential_manifest", "invalid_transfer_file",
    "archive_file_not_allowlisted", "archive_size_mismatch", "archive_hash_mismatch",
    "credential_file_missing", "owner_must_stop_source_before_import",
    "destination_already_has_state", "never_overwrite_destination_history_or_ledger",
    "never_overwrite_destination_settings", "never_overwrite_destination_file",
    "destination_symlink_or_reparse", "credential_not_in_profiles",
    "unindexed_run_evidence",
    "reissue_all_flags_required", "invalid_reissue_marker", "reissue_prior_archive_mismatch",
    "invalid_handoff_chain", "reissue_chain_capacity", "reissue_marker_changed",
    "reissue_marker_pending_requires_review",
})
FAILURE_HINTS = {
    "unfinished_battle_owner_must_finish_first": "Заверши незакінчені бої в панелі перед перенесенням.",
    "unfinished_legacy_cycle": "Заверши попередній торговий цикл перед перенесенням.",
    "position_not_known_flat": "Журнал ще не підтверджує нульові позиції; потрібна звірка в панелі.",
    "legacy_position_not_flat": "У попередньому циклі ще є залишок позиції.",
    "close_idle_source_panel_before_export": "Закрий локальну панель і дочекайся завершення її процесу.",
    "pending_transfer_requires_review": "Збережи файл .pending; його треба перевірити перед повторним експортом.",
    "workspace_already_handed_off": "Стан уже передано; збережи ZIP і маркер server_handoff.json.",
    "output_exists_or_inside_runtime": "Архів уже існує або шлях вказано всередині runtime; не перезаписуй попереднє перенесення.",
    "archive_file_not_allowlisted": "В архіві є файл поза дозволеним складом стану.",
    "incomplete_state_no_fresh_ledger_fallback": "Не вистачає історії або ledger; нові порожні ліміти не замінюють їх.",
    "destination_already_has_state": "Папка призначення вже має стан; не перезаписуй його.",
    "never_overwrite_destination_history_or_ledger": "Використовуй папку без попередньої торгової історії.",
    "unindexed_run_evidence": "Є журнал запуску поза індексом. Збережи всі файли; потрібна перевірка перед перенесенням.",
    "reissue_all_flags_required": "Повторний export потребує prior ZIP і двох явних підтверджень власника: authoritative source та всі target зупинені.",
    "reissue_prior_archive_mismatch": "Попередній ZIP не збігається з чинним handoff-маркером. Збережи обидва; не знімай захист.",
    "reissue_marker_pending_requires_review": "Збережи marker pending і ZIP pending; потрібна перевірка незавершеного publish.",
}


class TransferError(ValueError):
    pass


def read(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 512 * 1024**2:
        raise TransferError("invalid_state_file")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        raise TransferError("invalid_state_json") from None


def sha_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024**2), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_name(name):
    if not isinstance(name, str) or "\\" in name or ":" in name or any(ord(c) < 32 for c in name):
        raise TransferError("unsafe_archive_path")
    path = PurePosixPath(name)
    if not name or path.is_absolute() or any(p in {"", ".", ".."} for p in name.split("/")):
        raise TransferError("unsafe_archive_path")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if any(p.endswith((" ", ".")) or any(c in p for c in '<>"|?*') or p.split(".")[0].upper() in reserved
           for p in path.parts):
        raise TransferError("unsafe_windows_archive_path")
    return name


def regular(root, relative):
    path = root / safe_name(relative)
    if not path.resolve().is_relative_to(root.resolve()):
        raise TransferError("source_path_outside_workspace")
    if any(p.is_symlink() or p.is_junction() for p in [path, *list(path.parents)[:len(PurePosixPath(relative).parts)]]):
        raise TransferError("symlink_or_reparse_point")
    if not path.is_file():
        raise TransferError("state_file_missing")
    return path


def engine_epoch():
    from trading_lab.strategy_training import code_hash
    from trading_lab.exchange_lab import source_hash
    from trading_lab.arena import arena_source_hash
    return {"training": code_hash(), "exchange": source_hash(), "paper": arena_source_hash()}


def _sha256_value(value):
    return isinstance(value, str) and re.fullmatch("[a-f0-9]{64}", value) is not None


def _export_time(value):
    try:
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError()
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError()
        return stamp.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise TransferError("invalid_handoff_chain") from None


def _handoff_chain(document):
    if "handoff_chain" not in document and "reissue" not in document:
        return []
    chain, reissue = document.get("handoff_chain"), document.get("reissue")
    if (not isinstance(chain, list) or not 1 <= len(chain) <= MAX_HANDOFF_CHAIN
            or not isinstance(reissue, dict) or set(reissue) != {
                "schema", "previous_archive_sha256", "previous_marker_sha256", "previous_exported_at",
                "source_authoritative", "all_targets_stopped"}
            or reissue["schema"] != REISSUE_SCHEMA or reissue["source_authoritative"] is not True
            or reissue["all_targets_stopped"] is not True):
        raise TransferError("invalid_handoff_chain")
    seen, previous = set(), None
    for row in chain:
        if (not isinstance(row, dict) or set(row) != {"archive_sha256", "marker_sha256", "exported_at"}
                or not _sha256_value(row["archive_sha256"]) or not _sha256_value(row["marker_sha256"])
                or row["archive_sha256"] in seen):
            raise TransferError("invalid_handoff_chain")
        stamp = _export_time(row["exported_at"])
        if (previous is not None and stamp < previous) or stamp > _export_time(document.get("exported_at")):
            raise TransferError("invalid_handoff_chain")
        seen.add(row["archive_sha256"]); previous = stamp
    last = chain[-1]
    if (reissue["previous_archive_sha256"] != last["archive_sha256"]
            or reissue["previous_marker_sha256"] != last["marker_sha256"]
            or reissue["previous_exported_at"] != last["exported_at"]):
        raise TransferError("invalid_handoff_chain")
    return chain


def _reissue_context(source, prior_path):
    marker = regular(source, HANDOFF)
    if marker.stat().st_size > MAX_HANDOFF_MARKER:
        raise TransferError("invalid_reissue_marker")
    marker_raw = marker.read_bytes()
    try:
        old = json.loads(marker_raw)
    except (ValueError, TypeError):
        raise TransferError("invalid_reissue_marker") from None
    base_keys = {"schema", "archive_sha256", "exported_at", "owner_transfer"}
    if (not isinstance(old, dict) or set(old) not in (base_keys, base_keys | {"handoff_chain", "reissue"})
            or old.get("schema") != SCHEMA or old.get("owner_transfer") is not True
            or not _sha256_value(old.get("archive_sha256"))):
        raise TransferError("invalid_reissue_marker")
    if _export_time(old.get("exported_at")) > datetime.now(timezone.utc):
        raise TransferError("invalid_reissue_marker")
    prior = Path(prior_path).absolute()
    if (prior.is_symlink() or prior.is_junction() or not prior.is_file()
            or prior.stat().st_size > MAX_BYTES + 128 * 1024**2
            or sha_file(prior) != old["archive_sha256"]):
        raise TransferError("reissue_prior_archive_mismatch")
    # Historical source epochs are accepted only for this SHA-bound predecessor.
    # Normal import and the new pending archive still require the current epoch.
    try:
        with zipfile.ZipFile(prior) as archive:
            manifest = _verify_archive(archive, expected_engine=None)
    except TransferError:
        raise
    except (zipfile.BadZipFile, RuntimeError, ValueError, KeyError, TypeError):
        raise TransferError("reissue_prior_archive_mismatch") from None
    chain = _handoff_chain(manifest)
    if (manifest.get("exported_at") != old["exported_at"] or _handoff_chain(old) != chain
            or old.get("reissue") != manifest.get("reissue")
            or old["archive_sha256"] in {row["archive_sha256"] for row in chain}):
        raise TransferError("reissue_prior_archive_mismatch")
    if len(chain) >= MAX_HANDOFF_CHAIN:
        raise TransferError("reissue_chain_capacity")
    marker_sha = hashlib.sha256(marker_raw).hexdigest()
    return {"prior": prior, "previous_archive_sha256": old["archive_sha256"], "previous_marker_sha256": marker_sha,
            "handoff_chain": [*chain, {"archive_sha256": old["archive_sha256"], "marker_sha256": marker_sha,
                                      "exported_at": old["exported_at"]}],
            "reissue": {"schema": REISSUE_SCHEMA, "previous_archive_sha256": old["archive_sha256"],
                        "previous_marker_sha256": marker_sha, "previous_exported_at": old["exported_at"],
                        "source_authoritative": True, "all_targets_stopped": True}}


def _publish_reissue(pending, output):
    # Windows rename refuses an existing destination. POSIX link provides the same
    # exclusive, atomic publication without overwriting an unrelated file.
    if os.name == "nt":
        os.rename(pending, output)
    else:
        os.link(pending, output)
        pending.unlink()


def assert_flat(doc):
    for bot in doc.get("bots", []):
        if bot.get("id") == "cash":
            continue
        try:
            position = Decimal(str(bot.get("position_btc")))
        except (InvalidOperation, TypeError):
            raise TransferError("position_not_known_flat") from None
        if not position.is_finite() or position != 0:
            raise TransferError("position_not_known_flat")


def assert_finished(root):
    """Journal assertions complement the manager/worker OS locks; no API claims."""
    for relative in REQUIRED:
        regular(root, relative)
    exchange = read(root / "runtime/exchange_v1/manifest.json")
    arena = read(root / "runtime/arena_v4/manifest.json")
    for folder, ids in (("exchange_v1", exchange.get("runs")), ("arena_v4", arena.get("seasons"))):
        if not isinstance(ids, list) or any(not isinstance(i, str) or
                len(i) != 32 or any(c not in "0123456789abcdef" for c in i) for i in ids):
            raise TransferError("invalid_run_manifest")
        if len(set(ids)) != len(ids):
            raise TransferError("invalid_run_manifest")
        # A restored/partial manifest must never hide a saved run from the flat check.
        # This includes segmented logs and snapshots without their state JSON.
        for evidence in (root / "runtime" / folder).iterdir():
            match = re.match(r"([a-fA-F0-9]{32})(?:[.]|$)", evidence.name)
            if match and match.group(1).lower() not in ids:
                raise TransferError("unindexed_run_evidence")
        for identity in ids:
            doc = read(regular(root, "runtime/" + folder + "/" + identity + ".json"))
            if doc.get("status") != "DONE":
                raise TransferError("unfinished_battle_owner_must_finish_first")
            assert_flat(doc)
    legacy = [root / "runtime/one_trade_v1.json"]
    legacy += list((root / "runtime/web_runs").glob("*/runtime/one_trade_v1.json"))
    for path in legacy:
        if not path.exists():
            continue
        doc = read(path)
        if doc.get("phase") not in TERMINAL_RUNS:
            raise TransferError("unfinished_legacy_cycle")
        remaining = doc.get("remaining_contracts", "0")
        try:
            if not Decimal(str(remaining)).is_finite() or Decimal(str(remaining)) != 0:
                raise TransferError("legacy_position_not_flat")
        except InvalidOperation:
            raise TransferError("legacy_position_not_flat") from None


def selected_files(root, include_credentials):
    result = {}
    for folder in FOLDERS:
        base = root / "runtime" / folder
        for path in sorted(base.rglob("*")):
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise TransferError("symlink_or_reparse_point")
            # Historical preview scripts belong to the PC, not the state archive.
            # Keep selection consistent with the archive verifier, including Windows case.
            if not path.is_file() or path.name.casefold().endswith(STATE_EXCLUDED_SUFFIXES):
                continue
            if any(p == "__pycache__" for p in path.parts):
                continue
            relative = path.relative_to(root).as_posix()
            if any(word in path.name.lower() for word in (".env", "credential", "secret", "passphrase")):
                raise TransferError("unexpected_credential_file_in_state")
            result[relative] = regular(root, relative)
    for path in (root / "runtime").glob("*.sqlite3"):
        relative = path.relative_to(root).as_posix()
        result[relative] = regular(root, relative)
        for suffix in ("-wal", "-shm", "-journal"):
            auxiliary = Path(str(path) + suffix)
            if auxiliary.exists():
                name = auxiliary.relative_to(root).as_posix()
                result[name] = regular(root, name)
    legacy = root / "runtime/one_trade_v1.json"
    if legacy.exists():
        result["runtime/one_trade_v1.json"] = regular(root, "runtime/one_trade_v1.json")
    configured = profiles(root)  # Validates metadata; never loads a credential value.
    config = root / "LocalSettings/exchange_accounts.json"
    if config.exists():
        result["LocalSettings/exchange_accounts.json"] = regular(root, "LocalSettings/exchange_accounts.json")
    credentials = []
    if include_credentials:
        for profile in configured:
            name = profile["settings_file"]
            path = root / name
            if path.exists():
                if path.stat().st_size > 8192:
                    raise TransferError("credential_file_size")
                result[name] = regular(root, name)
                credentials.append(name)
        if (root / ".env.local").is_file():
            if (root / ".env.local").stat().st_size > 8192:
                raise TransferError("credential_file_size")
            result[".env.local"] = regular(root, ".env.local")
            credentials.append(".env.local")
    if not set(REQUIRED) <= set(result):
        raise TransferError("incomplete_state_no_fresh_ledger_fallback")
    if len(result) > MAX_FILES or sum(p.stat().st_size for p in result.values()) > MAX_BYTES:
        raise TransferError("transfer_capacity")
    return result, credentials


def export_state(source, output, *, include_credentials=False, reissue_from=None,
                 source_authoritative=False, all_targets_stopped=False):
    reissue = reissue_from is not None or source_authoritative or all_targets_stopped
    if reissue and (reissue_from is None or source_authoritative is not True or all_targets_stopped is not True):
        raise TransferError("reissue_all_flags_required")
    if reissue and (Path(output).is_symlink() or Path(output).is_junction()):
        raise TransferError("symlink_or_reparse_point")
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists() or output.is_relative_to(source / "runtime"):
        raise TransferError("output_exists_or_inside_runtime")
    if (source / HANDOFF).exists() and not reissue:
        raise TransferError("workspace_already_handed_off")
    paths = [source / "runtime/arena_v4/manager.lock",
             source / "runtime/exchange_v1/manager.lock",
             source / "runtime/one_trade_v1.lock",
             source / "runtime/research_resources/heavy.lock"]
    paths += sorted({p for folder in FOLDERS for p in (source / "runtime" / folder).rglob("*.lock")} - set(paths))
    with ExitStack() as stack:
        for path in paths:
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise TransferError("symlink_or_reparse_point")
            try:
                stack.enter_context(exclusive(path))
            except ValueError:
                raise TransferError("close_idle_source_panel_before_export") from None
        context = _reissue_context(source, reissue_from) if reissue else None
        marker = source / HANDOFF
        marker_pending = marker.with_suffix(marker.suffix + ".reissue.pending")
        if reissue and marker_pending.exists():
            raise TransferError("reissue_marker_pending_requires_review")
        assert_finished(source)
        files, credentials = selected_files(source, include_credentials)
        output.parent.mkdir(parents=True, exist_ok=True)
        pending = output.with_suffix(output.suffix + ".pending")
        if pending.exists():
            raise TransferError("pending_transfer_requires_review")
        manifest = {"schema": SCHEMA, "exported_at": datetime.now(timezone.utc).isoformat(),
                    "cold": True, "private": True, "contains_credentials": bool(credentials),
                    "credential_files": credentials, "automatic_trade_start": False,
                    "engine": engine_epoch(),
                    "files": [{"path": name, "bytes": path.stat().st_size, "sha256": sha_file(path)}
                              for name, path in sorted(files.items())]}
        if context:
            manifest.update(handoff_chain=context["handoff_chain"], reissue=context["reissue"])
        try:
            with zipfile.ZipFile(pending, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for name, path in sorted(files.items()):
                    archive.write(path, name)
                archive.writestr("TRANSFER_MANIFEST.json", json.dumps(manifest, ensure_ascii=False, allow_nan=False).encode())
            if context:
                with pending.open("r+b") as handle:
                    os.fsync(handle.fileno())
            with zipfile.ZipFile(pending) as archive:
                verify_archive(archive)
            if any(sha_file(files[row["path"]]) != row["sha256"] for row in manifest["files"]):
                raise TransferError("source_changed_during_cold_export")
            # A later local panel start refuses this workspace. Do not export the marker.
            # Publish the guard first: an interrupted handoff fails closed locally.
            new_marker = {"schema": SCHEMA, "archive_sha256": sha_file(pending),
                          "exported_at": manifest["exported_at"], "owner_transfer": True}
            if context:
                if (sha_file(marker) != context["previous_marker_sha256"]
                        or sha_file(context["prior"]) != context["previous_archive_sha256"]):
                    raise TransferError("reissue_marker_changed")
                new_marker.update(handoff_chain=context["handoff_chain"], reissue=context["reissue"])
                with marker_pending.open("x", encoding="utf-8") as handle:
                    json.dump(new_marker, handle, ensure_ascii=False, allow_nan=False)
                    handle.flush(); os.fsync(handle.fileno())
                if sha_file(marker) != context["previous_marker_sha256"]:
                    raise TransferError("reissue_marker_changed")
                # Never unlink the old guard. Only replace it after verified pending exists.
                os.replace(marker_pending, marker)
                _publish_reissue(pending, output)
            else:
                with marker.open("x", encoding="utf-8") as handle:
                    json.dump(new_marker, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(pending, output)
        except Exception:
            if not reissue and pending.exists() and not (source / HANDOFF).exists():
                pending.unlink()
            raise
    return {"status": "REISSUED_PRIVATE" if reissue else "EXPORTED_PRIVATE", "files": len(files), "contains_credentials": bool(credentials),
            "archive": str(output), "archive_sha256": sha_file(output), "local_handoff_guard": True,
            "paid_ledgers_preserved": True, "financial_actions": False}


def verify_archive(archive):
    return _verify_archive(archive, expected_engine=engine_epoch())


def _verify_archive(archive, *, expected_engine):
    names = archive.namelist()
    if len(names) != len(set(n.casefold() for n in names)) or len(names) > MAX_FILES + 1:
        raise TransferError("duplicate_archive_paths")
    for info in archive.infolist():
        safe_name(info.filename)
        if info.is_dir() or stat.S_ISLNK(info.external_attr >> 16) or info.file_size < 0:
            raise TransferError("invalid_archive_member")
    if "TRANSFER_MANIFEST.json" not in names:
        raise TransferError("transfer_manifest_missing")
    if archive.getinfo("TRANSFER_MANIFEST.json").file_size > 32 * 1024**2:
        raise TransferError("transfer_manifest_capacity")
    manifest = json.loads(archive.read("TRANSFER_MANIFEST.json"))
    if (not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA or manifest.get("cold") is not True
            or manifest.get("automatic_trade_start") is not False or manifest.get("private") is not True
            or not isinstance(manifest.get("files"), list)
            or not isinstance(manifest.get("engine"), dict)
            or set(manifest["engine"]) != {"training", "exchange", "paper"}
            or not all(_sha256_value(v) for v in manifest["engine"].values())
            or expected_engine is not None and manifest["engine"] != expected_engine):
        raise TransferError("invalid_transfer_manifest")
    _export_time(manifest.get("exported_at"))
    _handoff_chain(manifest)
    credentials = manifest.get("credential_files")
    if (not isinstance(credentials, list) or len(credentials) != len(set(credentials))
            or any(not isinstance(n, str) or not (n == ".env.local" or SETTINGS_FILE.fullmatch(n)) for n in credentials)
            or manifest.get("contains_credentials") is not bool(credentials)):
        raise TransferError("invalid_credential_manifest")
    files, total = {}, 0
    for row in manifest["files"]:
        name = safe_name(row["path"])
        if name == "TRANSFER_MANIFEST.json" or name in files or name not in names:
            raise TransferError("invalid_transfer_file")
        allowed = name.startswith(tuple("runtime/" + folder + "/" for folder in FOLDERS))
        allowed |= bool(re.fullmatch(r"runtime/jev[a-z0-9_]*\.sqlite3(?:-wal|-shm|-journal)?", name))
        allowed |= name == "runtime/one_trade_v1.json" or name == "LocalSettings/exchange_accounts.json"
        allowed |= name in credentials
        if not allowed or name.casefold().endswith(STATE_EXCLUDED_SUFFIXES):
            raise TransferError("archive_file_not_allowlisted")
        size = row["bytes"]
        if type(size) is not int or size < 0 or archive.getinfo(name).file_size != size:
            raise TransferError("archive_size_mismatch")
        if name in credentials and size > 8192:
            raise TransferError("credential_file_size")
        total += size
        if total > MAX_BYTES:
            raise TransferError("transfer_capacity")
        digest = hashlib.sha256()
        with archive.open(name) as handle:
            for chunk in iter(lambda: handle.read(1024**2), b""):
                digest.update(chunk)
        if digest.hexdigest() != row["sha256"]:
            raise TransferError("archive_hash_mismatch")
        files[name] = row
    if set(names) != set(files) | {"TRANSFER_MANIFEST.json"} or not set(REQUIRED) <= set(files):
        raise TransferError("incomplete_state_no_fresh_ledger_fallback")
    if not set(credentials) <= set(files):
        raise TransferError("credential_file_missing")
    return manifest


def import_state(destination, archive_path, *, source_stopped=False):
    destination, archive_path = Path(destination).resolve(), Path(archive_path).resolve()
    if not source_stopped:
        raise TransferError("owner_must_stop_source_before_import")
    if (destination / HANDOFF).exists() or (destination / IMPORTED).exists():
        raise TransferError("destination_already_has_state")
    for name in FOLDERS:
        path = destination / "runtime" / name
        if path.exists() and any(path.rglob("*")):
            raise TransferError("never_overwrite_destination_history_or_ledger")
    if (destination / "LocalSettings").exists() and any((destination / "LocalSettings").iterdir()):
        raise TransferError("never_overwrite_destination_settings")
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        manifest = verify_archive(archive)
        for row in manifest["files"]:
            target = destination / row["path"]
            if target.exists():
                raise TransferError("never_overwrite_destination_file")
            regular_parent = target.parent
            while regular_parent != destination:
                if regular_parent.is_symlink() or (hasattr(regular_parent, "is_junction") and regular_parent.is_junction()):
                    raise TransferError("destination_symlink_or_reparse")
                regular_parent = regular_parent.parent
        with tempfile.TemporaryDirectory(prefix=".import-", dir=destination) as pending:
            staged = Path(pending)
            for row in manifest["files"]:
                target = staged / row["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(row["path"]) as source, target.open("xb") as out:
                    shutil.copyfileobj(source, out, 1024**2)
                if os.name != "nt":
                    target.chmod(0o600)
            assert_finished(staged)
            configured = profiles(staged)
            if not set(manifest["credential_files"]) <= {".env.local", *(p["settings_file"] for p in configured)}:
                raise TransferError("credential_not_in_profiles")
            for row in manifest["files"]:
                target = destination / row["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                with (staged / row["path"]).open("rb") as source, target.open("xb") as out:
                    shutil.copyfileobj(source, out, 1024**2)
                if os.name != "nt":
                    target.chmod(0o600)
            marker = destination / IMPORTED
            with marker.open("x", encoding="utf-8") as handle:
                json.dump({"schema": SCHEMA, "imported_at": datetime.now(timezone.utc).isoformat(),
                           "archive_sha256": sha_file(archive_path), "source_stopped_by_owner": True,
                           "required_files": list(REQUIRED), "paid_ledgers_preserved": True}, handle)
    return {"status": "IMPORTED", "files": len(manifest["files"]), "paid_ledgers_preserved": True,
            "automatic_trade_start": False, "financial_actions": False}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--source", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--include-credentials", action="store_true")
    export.add_argument("--reissue-from", type=Path)
    export.add_argument("--source-authoritative", action="store_true")
    export.add_argument("--all-targets-stopped", action="store_true")
    restore = commands.add_parser("import")
    restore.add_argument("--root", type=Path, required=True)
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--source-stopped", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = export_state(args.source, args.output, include_credentials=args.include_credentials,
                              reissue_from=args.reissue_from, source_authoritative=args.source_authoritative,
                              all_targets_stopped=args.all_targets_stopped) if args.command == "export" else (
            import_state(args.root, args.archive, source_stopped=args.source_stopped))
    except (ValueError, OSError, KeyError, TypeError, zipfile.BadZipFile) as error:
        # No provider bodies, raw state or credential values in failure output.
        code = str(error) if isinstance(error, TransferError) and str(error) in FAILURE_CODES else "transfer_failed"
        hint = FAILURE_HINTS.get(code, "Перевір повноту стану й архіву; збережи історію та чинні ліміти.")
        print(f"Перенесення не виконано. Код: {code}. {hint} Деталі: SERVER_WINDOWS_UA.md.", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
