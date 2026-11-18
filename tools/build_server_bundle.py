"""Build the secret-free Windows application ZIP. Does not export running state."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.server_transfer import sha_file, regular
from trading_lab.strategy_training import code_hash
from trading_lab.exchange_lab import source_hash
from trading_lab.arena import arena_source_hash

WEB_SUFFIXES = {".html", ".js", ".css", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".woff", ".woff2", ".json"}
EXTRA = ("web_panel.py", "one_trade.py", "INSTALL_WINDOWS_SERVER.cmd", "EXPORT_SERVER_STATE.cmd",
         "START_WINDOWS_PANEL.cmd", "CHECK_WINDOWS_PACKAGE.cmd", "tools/check_exchange_profile.py",
         "REISSUE_SERVER_STATE.cmd", "deployment/server/reissue_from_pc.ps1",
         "IMPORT_SERVER_STATE.cmd", "tools/server_transfer.py", "tools/build_server_bundle.py",
         "tools/analyze_battle_file.py", "tools/strategy_workshop.py", "tools/arena_ops.py",
         "tools/research_limits.py", "tools/strategy_round.py",
         "tools/check_order_retention.py", "tools/check_cancel_answer.py",  # R2.1.8: GET-only / one Demo cancel, owner-run
         "deployment/server/run_server.py", "deployment/server/install_windows.ps1",
         "deployment/server/export_from_pc.ps1",
         "tools/arena_notifier.py", "NOTIFIER_SETUP.cmd", "NOTIFIER_START.cmd")  # D167: the Telegram notifier, owner-set


def build(output, *, root=ROOT):
    root, output = Path(root).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError("Bundle destination already exists")
    files = {p.relative_to(root).as_posix(): p for p in (root / "trading_lab").glob("*.py")}
    for p in (root / "web").rglob("*"):
        if p.is_file() and (p.suffix.lower() in WEB_SUFFIXES or p.name.upper().startswith("LICENSE")):
            files[p.relative_to(root).as_posix()] = p
    for name in EXTRA:
        files[name] = regular(root, name)
    guide = regular(root, "03_For_Client_Deliverables/SERVER_WINDOWS_UA.md")
    files["SERVER_WINDOWS_UA.md"] = guide
    files["START_HERE_LIVE_UA.md"] = regular(root, "03_For_Client_Deliverables/NEXT_LIVE_WINDOWS_UA.md")
    files["RECOVER_CLOCK_AND_TRANSFER_UA.md"] = regular(root, "03_For_Client_Deliverables/RECOVER_CLOCK_AND_TRANSFER_UA.md")
    files["NOTIFIER_TELEGRAM_UA.md"] = regular(root, "03_For_Client_Deliverables/NOTIFIER_TELEGRAM_UA.md")  # D167
    for name, path in files.items():
        if path.is_symlink() or name.startswith(("runtime/", "LocalSettings/", ".env", "01_", "02_")):
            raise ValueError("Application allowlist violation")
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                                capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = None
    manifest = {"schema": "jev-arena-server-application/v1", "created_at": datetime.now(timezone.utc).isoformat(),
                "source_commit": commit, "python": "3.12", "platform": "Windows 10/11 or Windows Server",
                "contains_credentials": False, "contains_runtime": False, "automatic_trade_start": False,
                "engine": {"training": code_hash(), "exchange": source_hash(), "paper": arena_source_hash()},
                "files": [{"path": name, "bytes": path.stat().st_size, "sha256": sha_file(path)}
                          for name, path in sorted(files.items())]}
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, path in sorted(files.items()):
            archive.write(path, "JevArena/" + name)
        archive.writestr("JevArena/deployment/server/BUNDLE_MANIFEST.json",
                        json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2).encode())
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise ValueError("Bundle CRC verification failed")
    return {"archive": str(output), "bytes": output.stat().st_size, "sha256": sha_file(output),
            "files": len(files), "engine": manifest["engine"], "contains_credentials": False,
            "contains_runtime": False, "automatic_trade_start": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.output), ensure_ascii=False))
