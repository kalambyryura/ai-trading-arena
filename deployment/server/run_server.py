"""Start the owner UI only. Browser/RDP disconnect does not stop server workers."""
import argparse
import json
from logging.handlers import RotatingFileHandler
import logging
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def check(root=ROOT):
    from tools.server_transfer import IMPORTED, HANDOFF, REQUIRED, read, regular, engine_epoch, sha_file
    if sys.version_info[:2] != (3, 12):
        raise ValueError("Python 3.12 required")
    if (root / HANDOFF).exists():
        raise ValueError("This workspace was transferred away")
    manifest = read(root / "deployment/server/BUNDLE_MANIFEST.json")
    if (manifest.get("schema") != "jev-arena-server-application/v1" or manifest.get("python") != "3.12"
            or manifest.get("contains_credentials") is not False or manifest.get("contains_runtime") is not False
            or manifest.get("automatic_trade_start") is not False or manifest.get("engine") != engine_epoch()
            or not isinstance(manifest.get("files"), list) or not manifest["files"]):
        raise ValueError("Invalid application manifest")
    if len({r["path"].casefold() for r in manifest["files"]}) != len(manifest["files"]):
        raise ValueError("Duplicate application file")
    for row in manifest["files"]:
        if row["path"].startswith(("runtime/", "LocalSettings/", ".env")):
            raise ValueError("State/credential in application manifest")
        path = regular(root, row["path"])
        if path.stat().st_size != row["bytes"] or sha_file(path) != row["sha256"]:
            raise ValueError("Application file mismatch")
    marker = read(root / IMPORTED)
    if (marker.get("schema") != "jev-arena-cold-transfer/v1"
            or marker.get("source_stopped_by_owner") is not True or marker.get("paid_ledgers_preserved") is not True):
        raise ValueError("Import complete state before starting this server")
    for relative in REQUIRED:
        regular(root, relative)
    from trading_lab.exchange_accounts import profiles
    accounts = profiles(root)
    return {"status": "READY", "credentials_loaded": False,
            "profiles": [{"id": p["id"], "credential_file_present": (root / p["settings_file"]).is_file()}
                         for p in accounts], "automatic_trade_start": False}


class ServerLog:
    def __init__(self, root):
        path = root / "runtime/server"
        path.mkdir(parents=True, exist_ok=True)
        self.handler = RotatingFileHandler(path / "panel.log", maxBytes=4 * 1024**2, backupCount=2, encoding="utf-8")
        self.logger = logging.Logger("JevServer", level=logging.INFO)
        self.logger.addHandler(self.handler)
    def write(self, text):
        text = text.strip()
        if text:
            self.logger.info(text)
    def flush(self):
        self.handler.flush()


def start_notifier(root, port, *, thread=threading.Thread, out=print):
    """D167: the Telegram notifier (tools/arena_notifier.py), a separate detached process, when its secret file is there.
    A watch starts it again within a minute if it does not hold its lock (plan check p. 4). The panel never reads the
    file's values and never waits for the notifier."""
    from tools.arena_notifier import CONFIG_NAME, watch
    if not (root / CONFIG_NAME).is_file():
        out("Telegram notifier: not configured (NOTIFIER_TELEGRAM_UA.md).")
        return None
    out("Telegram notifier: on.")
    watcher = thread(target=watch, args=(root, port), name="notifier-watch", daemon=True)
    watcher.start()
    return watcher


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "serve"))
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        result = check()
    except (ValueError, OSError, KeyError, TypeError):
        print("Server is not ready. Import the complete state and check SERVER_WINDOWS_UA.md.", file=sys.stderr)
        return 2
    if args.command == "check":
        print(json.dumps(result))
        return 0
    from trading_lab.web_panel import main as serve
    try:
        start_notifier(ROOT, args.port)
    except Exception:  # the notifier never keeps the panel from serving
        print("Telegram notifier: did not start.", file=sys.stderr)
    log = ServerLog(ROOT)
    sys.stdout = log
    # No redirection of exception contents into logs; trade adapters keep them private.
    try:
        serve(["--port", str(args.port)])
    except Exception:
        log.write("Panel exited unexpectedly; inspect the local UI and journals before owner recovery.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
