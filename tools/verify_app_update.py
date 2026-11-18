"""Verify an in-place application update on FAKE state only (the path of the 2026-10-27 delivery receipt).

An old application ZIP gets a cold-transfer fake state, the new application ZIP is extracted over the same folder,
and then: runtime/ and credential files are byte-identical, every pinned file of the new manifest matches, and the
packaged READY check passes. A fresh install of the new ZIP must also import a state exported after the update.
Files of the old application that the new one no longer ships are listed (they stay on disk, unpinned and unused).

  py -3.12 -X utf8 -B tools/verify_app_update.py <old app ZIP> <new app ZIP> --receipt <receipt.json>

No network, exchange, keys or real state.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_server_delivery import cold_fixture

EXPORT = ("from pathlib import Path;import sys,json;from tools.server_transfer import export_state;"
          "print(json.dumps(export_state(Path('.').resolve(),Path(sys.argv[1]),include_credentials=True)))")
IMPORT = ("from pathlib import Path;import sys,json;from tools.server_transfer import import_state;"
          "print(json.dumps(import_state(Path('.').resolve(),Path(sys.argv[1]),source_stopped=True)))")
CHECK = "import json;from deployment.server.run_server import check;print(json.dumps(check()))"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def extract(archive, destination):
    with zipfile.ZipFile(archive) as z:
        if z.testzip() is not None or not all(n.startswith("JevArena/") and ".." not in Path(n).parts and "\\" not in n
                                              for n in z.namelist()):
            raise SystemExit(f"{archive}: unexpected ZIP layout")
        z.extractall(destination)
        names = {n[len("JevArena/"):] for n in z.namelist() if not n.endswith("/")}
    return destination / "JevArena", names


def child(root, code, *args):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", code, *map(str, args)],
                            cwd=root, env=env, text=True, capture_output=True, check=True, timeout=120)
    return json.loads(result.stdout)


def verify(old_zip, new_zip):
    with tempfile.TemporaryDirectory(prefix="jev-app-update-") as temporary:
        base = Path(temporary).resolve()
        source, old_names = extract(old_zip, base / "source")
        cold_fixture(source)
        state = base / "fake-old-state.private.zip"
        child(source, EXPORT, state)
        target, _ = extract(old_zip, base / "target")
        child(target, IMPORT, state)
        protected = {p.relative_to(target).as_posix(): sha(p) for p in target.rglob("*")
                     if p.is_file() and (p.relative_to(target).parts[0] in ("runtime", "LocalSettings") or p.name.startswith(".env"))}
        _, new_names = extract(new_zip, base / "target")
        changed = sorted(name for name, digest in protected.items() if sha(target / name) != digest)
        if changed:
            raise SystemExit(f"state or credential files changed by the update: {changed}")
        manifest = json.loads((target / "deployment/server/BUNDLE_MANIFEST.json").read_text(encoding="utf-8"))
        for row in manifest["files"]:
            path = target / row["path"]
            if path.stat().st_size != row["bytes"] or sha(path) != row["sha256"]:
                raise SystemExit(f"pinned file mismatch after the update: {row['path']}")
        checked = child(target, CHECK)
        if checked["status"] != "READY" or checked["automatic_trade_start"] is not False:
            raise SystemExit(f"READY check failed after the update: {checked}")
        newer_state = base / "fake-new-state.private.zip"
        child(target, EXPORT, newer_state)
        destination, _ = extract(new_zip, base / "destination")
        child(destination, IMPORT, newer_state)
        if child(destination, CHECK)["status"] != "READY":
            raise SystemExit("READY check failed on a fresh install with the updated state")
        return {"schema": "jev-app-update-delivery/v1", "archive": Path(new_zip).name, "sha256": sha(new_zip),
                "bytes": Path(new_zip).stat().st_size, "previous_archive": Path(old_zip).name, "previous_sha256": sha(old_zip),
                "source_commit": manifest["source_commit"], "application_pins": len(manifest["files"]), "engine": manifest["engine"],
                "crc": "PASS", "in_place_update_on_fake_state": "PASS", "ready_after_update": "READY",
                "fake_protected_files_unchanged": len(protected), "updated_export_then_fresh_import": "PASS",
                "old_files_no_longer_shipped": sorted(old_names - new_names - {"deployment/server/BUNDLE_MANIFEST.json"}),
                "actual_state_imported": False, "network_calls": 0, "financial_actions": 0, "paid_calls": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("old_zip", type=Path)
    parser.add_argument("new_zip", type=Path)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.old_zip.resolve(), args.new_zip.resolve())
    args.receipt.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
