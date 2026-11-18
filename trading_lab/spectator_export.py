"""Owner-gated standalone display export. No credentials, transports or publishing."""
import json
from pathlib import Path
import shutil
import time
from .arena_strategies import ArenaError
from .event_replay import verify_packet

WEB_FILES = ("event-replay.js", "replay-labels.js", "review-model.js", "research-panels.js",
             "arena-battle-view.js", "arena-coliseum.js", "arena.css", "arena-coliseum.css",
             "research-panels.css", "review.css", "spectator.js")
ASSETS = ("coliseum-floor.png", "cash.png", "human-poses.png", "robot-poses.png",
          "human-portrait.png", "robot-portrait.png", "Manrope.ttf", "CormorantGaramond.ttf")


def receipt_template(packet):
    verify_packet(packet)
    return {"projection": "spectator/v1", "package_hash": packet["package_hash"], "integrity": "PASS",
            "privacy": "PENDING", "rights": "PENDING", "rights_reference": "",
            "owner_release": "PENDING", "owner_reference": "", "access": "PRIVATE", "release_after_ms": 0}


def gate(packet, receipt, now_ms=None):
    verify_packet(packet)
    allowed = {"projection", "package_hash", "integrity", "privacy", "rights", "rights_reference", "owner_release", "owner_reference", "access", "release_after_ms"}
    if not isinstance(receipt, dict) or set(receipt) != allowed: raise ArenaError("Потрібен повний дозвіл експорту глядача.")
    now_ms = int(time.time()*1000) if now_ms is None else now_ms
    if (receipt["projection"] != "spectator/v1" or receipt["package_hash"] != packet["package_hash"] or
        receipt["integrity"] != "PASS" or receipt["privacy"] != "PASS" or receipt["rights"] != "APPROVED" or
        receipt["owner_release"] != "APPROVED" or receipt["access"] != "PUBLIC" or
        type(receipt["release_after_ms"]) is not int or not 0 <= receipt["release_after_ms"] <= now_ms):
        raise ArenaError("Публічний експорт ще не схвалено власником / правами на дані.")
    if any(not isinstance(receipt[k], str) or not 1 <= len(receipt[k]) <= 240 or any(ord(c) < 32 for c in receipt[k])
           for k in ("rights_reference", "owner_reference")): raise ArenaError("Зафіксуй підставу прав на дані й рішення власника.")
    return {"allowed": True, "package_hash": packet["package_hash"], "public_hosting": False}


def export(packet, receipt, destination, web, now_ms=None):
    proof = gate(packet, receipt, now_ms)
    destination, web = Path(destination).absolute(), Path(web).resolve()
    if destination.exists() or any(p.is_symlink() for p in (destination, *destination.parents)):
        raise ArenaError("Експорт потребує нової окремої теки без symlink.")
    # Check all source assets before creating a package; no arbitrary path copy.
    for name in (*WEB_FILES, "spectator.html", *("assets/"+n for n in ASSETS)):
        source = web / name
        if not source.is_file() or source.is_symlink(): raise ArenaError("Не вистачає ресурсу глядача.")
    destination.mkdir(parents=True)
    try:
        for name in WEB_FILES:
            text = (web / name).read_text(encoding="utf-8")
            (destination / name).write_text(text.replace("/assets/", "assets/"), encoding="utf-8")
        (destination / "assets").mkdir()
        for name in ASSETS: shutil.copyfile(web / "assets" / name, destination / "assets" / name)
        data = json.dumps({"packet": packet, "receipt": receipt}, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        # Non-executable JSON embedded in HTML; escape closing script and all markup.
        data = data.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
        html = (web / "spectator.html").read_text(encoding="utf-8").replace("__SPECTATOR_DATA__", data)
        (destination / "index.html").write_text(html, encoding="utf-8")
        (destination / "release.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        # Preserve an incomplete new package for inspection; never overwrite/delete user data.
        raise ArenaError("Експорт не завершено; не публікуй цю теку.")
    return {**proof, "index": str(destination / "index.html")}
