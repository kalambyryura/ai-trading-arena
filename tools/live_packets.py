"""Live packets (P1-7a): a Battle ZIP -> one sanitized personal packet per author, with a sha256 manifest and verify.

Each author gets only what belongs to their own bot: their own recipe (absolute local paths redacted), their own
participant summary and funnel, their own events and frames, their own unsent gate payloads, every signal with the
condition values at that moment and what happened to it, the round's public settings without account names, and
market.jsonl unchanged (public prices). Other authors' recipes, funnels, events, names and hashes, account names,
UIDs, keys and local paths are left out, and a leak check refuses to write a packet that still contains any of them.
The ZIP itself is never handed to authors; packets go to an ignored runtime folder and coord hands them out.

  py -3.12 -X utf8 -B tools/live_packets.py build --zip <Battle ZIP> --authors-from <round manifest.json> --out <dir>
  py -3.12 -X utf8 -B tools/live_packets.py verify --packet <dir>/<author>

No network, keys, exchange code or account access.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import zipfile

SCHEMA = "jev-live-packet/v1"
MEMBERS = {"battle-review.json", "battle-replay.json", "strategies.json", "match-settings.json", "market.jsonl",
           "README_UA.txt", "manifest.json", "manifest-receipt.json"}  # the receipt: Battle ZIPs from R2.1.10
MARKET_MEMBER = re.compile(r"market\.[A-Z0-9]{2,12}-USDT-SWAP\.jsonl")  # R2.1.12 (MIX): a mixed season's, one per contract
MAX_MEMBER_BYTES = 64 * 1024 * 1024
LOCAL_PATH = re.compile(r"(?:\b[A-Za-z]:[/\\]|/home/|/Users/)[^\s,;)\"'`]*")
LIFECYCLE = {"rules_only_mode", "admission_closed", "season_done"}  # season-wide events with no bot or account data
OLD_RULE_SECONDS = 10  # before R1 full.1 an entry also needed its decision snapshot <= 10 s old (P0, 2026-10-31)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def read_zip(path):
    """Members by name, checked against the known Battle ZIP layout (flat, bounded, no unknown files)."""
    raw = Path(path).read_bytes()
    out = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = [info.filename for info in archive.infolist()]
        if len(names) != len(set(names)):  # R2111-PACKETS-DUP: verify_archive refuses them too; never the last copy
            raise SystemExit("duplicate ZIP member " + repr(sorted({n for n in names if names.count(n) > 1})))
        for info in archive.infolist():
            if (info.filename not in MEMBERS and not MARKET_MEMBER.fullmatch(info.filename)) or info.file_size > MAX_MEMBER_BYTES:
                raise SystemExit(f"unexpected ZIP member {info.filename!r}")
            out[info.filename] = archive.read(info)
    manifest = json.loads(out["manifest.json"])
    if "manifest-receipt.json" in out:  # R2.1.11 (R2110-RECEIPT): a receipt is checked, never only accepted
        stamp = json.loads(out["manifest-receipt.json"])
        if (len(out["manifest-receipt.json"]) > 4096 or not isinstance(stamp, dict)
                or stamp.get("schema") != "jev-arena-manifest-receipt/v1"
                or stamp.get("manifest_bytes") != len(out["manifest.json"]) or stamp.get("manifest_sha256") != sha256(out["manifest.json"])):
            raise SystemExit("manifest-receipt.json does not match manifest.json")
    for entry in manifest["files"]:
        if sha256(out[entry["path"]]) != entry["sha256"]:
            raise SystemExit(f"{entry['path']} differs from the ZIP manifest")
    return out, sha256(raw), manifest


def redact(value, found):
    """Absolute local paths in any string -> '<local path>'; the JSON paths changed are listed in found."""
    def walk(node, where):
        if isinstance(node, dict):
            return {k: walk(v, f"{where}.{k}") for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v, f"{where}[{i}]") for i, v in enumerate(node)]
        if isinstance(node, str) and LOCAL_PATH.search(node):
            found.append(where)
            return LOCAL_PATH.sub("<local path>", node)
        return node
    return walk(value, "$")


def stamp(text):
    return datetime.fromisoformat(text).timestamp()


def signals(events, bot_id):
    """Every passed entry check of this bot with the condition values and what followed it, in order."""
    out, seq = [], [e for e in events if e["bot_id"] in (bot_id, None)]
    for i, event in enumerate(seq):
        if event["code"] != "entry_checks" or event["bot_id"] != bot_id or not event["data"].get("local_checks_passed"):
            continue
        observed = float(event["data"]["observed_at"])
        following = []
        for later in seq[i + 1:]:
            if later["bot_id"] == bot_id and later["code"] in ("entry_checks", "no_signal"):
                break  # the next tick of this bot
            if later["bot_id"] == bot_id or (later["code"] == "jev_shadow_payload" and later["data"].get("snapshot_sha256") == event["data"]["snapshot_sha256"]):
                following.append(later)
        steps = [{"code": e["code"], "seconds_after_snapshot": round(stamp(e["at"]) - observed, 3), "data": e["data"]} for e in following]
        cancel = next((s for s in steps if s["code"] == "entry_cancelled"), None)
        order = next((s for s in steps if s["code"] in ("entry_rechecked", "exchange_order_intent")), None)
        if order:
            outcome, reason, source = "rechecked_or_ordered", None, "event"
        elif cancel and cancel["data"].get("reason"):
            outcome, reason, source = "cancelled", cancel["data"]["reason"], "event"
        elif cancel:
            outcome, reason, source = "cancelled", ("decision_snapshot_too_old" if cancel["seconds_after_snapshot"] > OLD_RULE_SECONDS
                                                    else "unknown"), "inferred_from_timing_pre_r1_full_1"
        else:
            last = steps[-1]["seconds_after_snapshot"] if steps else round(stamp(event["at"]) - observed, 3)
            outcome, reason, source = "no_event", ("decision_snapshot_too_old" if last > OLD_RULE_SECONDS or
                                                   stamp(event["at"]) - observed > OLD_RULE_SECONDS else "unknown"), "inferred_from_timing_pre_r1_full_1"
        out.append({"at": event["at"], "observed_at": observed, "seconds_to_entry_checks": round(stamp(event["at"]) - observed, 3),
                    "snapshot_sha256": event["data"]["snapshot_sha256"], "bar_time_ms": event["data"].get("bar_time_ms"),
                    "kind": event["data"].get("kind"), "checks": event["data"].get("checks", []),
                    "outcome": outcome, "reason": reason, "reason_source": source, "steps": steps})
    return out


def packet(files, zip_sha256, zip_manifest, author, bot_id):
    review = json.loads(files["battle-review.json"])
    strategies = json.loads(files["strategies.json"])
    settings = json.loads(files["match-settings.json"])
    own = next((p for p in review["participants"] if p["id"] == bot_id), None)
    if own is None:
        raise SystemExit(f"{author}: bot {bot_id} is not a participant of this battle")
    others = [p for p in review["participants"] if p["id"] != bot_id]
    redactions = []
    recipe_entry = next(s for s in strategies if s["id"] == bot_id)
    recipe = {"id": recipe_entry["id"], "version": recipe_entry["version"], "recipe_sha256": recipe_entry["recipe_sha256"],
              "recipe": redact(recipe_entry["recipe"], redactions)}
    recipe["redacted_fields"] = redactions
    recipe["note"] = ("recipe_sha256 is the frozen recipe's; the fields listed in redacted_fields had absolute local paths "
                      "replaced by '<local path>', so this copy does not re-hash to it") if redactions else None
    shadow = {r["payload_sha256"]: r for r in review["request_payloads"]}
    own_shadow = {h for h, r in shadow.items() if r["candidate_ids"] == [bot_id]}
    events = [e for e in review["events"] if e["bot_id"] == bot_id or e["code"] in LIFECYCLE
              or (e["code"] == "jev_shadow_payload" and e["data"].get("payload_sha256") in own_shadow)]
    own_settings = {k: v for k, v in settings.items() if k != "participants"}
    own_settings["participant"] = {k: v for k, v in next(p for p in settings["participants"] if p["strategy_id"] == bot_id).items()
                                   if k != "account_id"}
    match = {k: v for k, v in review["match"].items()}
    summary = {"schema": SCHEMA, "battle_id": review["match"]["id"], "author": author, "bot_id": bot_id, "match": match,
               "participant": own, "cash": review["cash"], "limitations": review["limitations"],
               "history_complete": review["history_complete"], "participants_in_battle": len(review["participants"])}
    signal_rows = signals(review["events"], bot_id)
    out = {
        "summary.json": summary,
        "recipe.json": recipe,
        "match-settings.json": own_settings,
        "signals.json": {"bot_id": bot_id, "signals": signal_rows,
                         "reason_note": "reason_source 'event' is recorded by the engine; 'inferred_from_timing_pre_r1_full_1' "
                                        "is the platform's diagnosis for rounds before R1 full.1, whose entry_cancelled carried no reason"},
        "events.jsonl": events,
        "frames.jsonl": [{"at": f["at"], "event_seq": f["event_seq"], "bot": next((b for b in f["bots"] if b["id"] == bot_id), None)}
                         for f in review["frames"]],
        "gate-payloads.json": [shadow[h] for h in sorted(own_shadow, key=lambda h: shadow[h]["seq"])],
    }
    encoded = {}
    for name, value in out.items():
        if name.endswith(".jsonl"):
            encoded[name] = "".join(json.dumps(v, ensure_ascii=False, sort_keys=True) + "\n" for v in value).encode("utf-8")
        else:
            encoded[name] = (json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8")
    # R2.1.12 (MIX, plan check p. 18): in a mixed season the author gets the market of the bot's own contract
    encoded["market.jsonl"] = files["market." + own["instrument"] + ".jsonl"] if own.get("instrument") else files["market.jsonl"]
    markers = [m for p in others for m in (p["id"], p["recipe_sha256"], p["name"])]
    markers += [p["account_id"] for p in settings["participants"] if p.get("account_id")]
    markers += [s["recipe_sha256"] for s in strategies if s["id"] != bot_id]
    leaks = sorted({(name, marker[:12]) for name, data in encoded.items() for marker in markers
                    if marker and marker.encode("utf-8") in data})
    paths = sorted({name for name, data in encoded.items() if LOCAL_PATH.search(data.decode("utf-8", "replace"))})
    if leaks or paths:
        raise SystemExit(f"{author}: leak check failed: {leaks or ''} {paths or ''}")
    encoded["README_UA.txt"] = README.format(author=author, battle=review["match"]["id"], market=(
        f"market.jsonl — публічні ціни лише твого інструмента ({own['instrument']}) без змін; цін інших інструментів раунду тут немає."
        if own.get("instrument") else "market.jsonl — публічні ціни раунду без змін.")).encode("utf-8")
    # R2.1.12 RC2 (X2-08): the source ZIP's list without the market member of another contract
    listed = [f for f in zip_manifest["files"] if not MARKET_MEMBER.fullmatch(f["path"]) or f["path"] == "market." + str(own.get("instrument")) + ".jsonl"]
    manifest = {"schema": SCHEMA, "battle_id": review["match"]["id"], "author": author, "bot_id": bot_id,
                "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "source_zip_sha256": zip_sha256, "source_zip_manifest": {"exported_at": zip_manifest.get("exported_at"),
                                                                         "files": listed},
                "tool_sha256": sha256(Path(__file__).read_bytes()), "leak_check": "passed",
                "excluded": "other authors' recipes, funnels, events, names and hashes; account names; UIDs; keys; local paths",
                "files": [{"path": name, "bytes": len(data), "sha256": sha256(data)} for name, data in sorted(encoded.items())]}
    return encoded, manifest


README = """Особистий Live-пакет автора {author} · бій {battle}

Лише твоє: рецепт (абсолютні локальні шляхи замінено на <local path>), підсумок і воронка твого бота,
кожен сигнал зі значеннями умов у момент сигналу й тим, що з ним сталося (signals.json), твої події та кадри,
твої невідправлені gate-payload. {market}
Рецептів, воронок, подій, назв і хешів інших авторів, назв акаунтів, UID, ключів і локальних шляхів тут немає.
reason_source "inferred_from_timing_pre_r1_full_1" — висновок платформи з таймінгу для раундів до R1 full.1.
manifest.json: sha256 кожного файлу; перевірка: py -3.12 -X utf8 -B tools/live_packets.py verify --packet <ця тека>.
Це файл аналізу, не дозвіл на публікацію.
"""


def build(zip_path, authors, out):
    files, zip_sha256, zip_manifest = read_zip(zip_path)
    battle = json.loads(files["battle-review.json"])["match"]["id"]
    root = Path(out) / battle
    written = {}
    for author, bot_id in sorted(authors.items()):
        target = root / author
        if target.exists():
            raise SystemExit(f"{target} exists: a packet is never overwritten")
        encoded, manifest = packet(files, zip_sha256, zip_manifest, author, bot_id)
        target.mkdir(parents=True)
        for name, data in encoded.items():
            (target / name).write_bytes(data)
        (target / "manifest.json").write_bytes((json.dumps(manifest, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
        written[author] = {"path": str(target).replace("\\", "/"), "manifest_sha256": sha256((target / "manifest.json").read_bytes()),
                           "files": len(manifest["files"])}
    return written


def verify(folder):
    folder = Path(folder)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    problems = []
    listed = {f["path"] for f in manifest["files"]}
    present = {p.name for p in folder.iterdir() if p.is_file()} - {"manifest.json"}
    if listed != present:
        problems.append({"files_not_in_manifest": sorted(present - listed), "missing": sorted(listed - present)})
    for entry in manifest["files"]:
        path = folder / entry["path"]
        if path.is_file() and sha256(path.read_bytes()) != entry["sha256"]:
            problems.append({"sha256_mismatch": entry["path"]})
        if path.is_file() and LOCAL_PATH.search(path.read_text(encoding="utf-8", errors="replace")):
            problems.append({"local_path": entry["path"]})
    return {"ok": not problems, "author": manifest.get("author"), "battle_id": manifest.get("battle_id"), "problems": problems}


def authors_from(manifest_path, extra):
    found = dict(json.loads(Path(manifest_path).read_text(encoding="utf-8"))["authors"]) if manifest_path else {}
    for item in extra or []:
        name, _, bot = item.partition("=")
        found[name] = bot
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--zip", required=True)
    b.add_argument("--authors-from", help="round manifest with an authors map {author: bot id}")
    b.add_argument("--author", action="append", help="author=bot_id, adds to or overrides --authors-from")
    b.add_argument("--out", required=True)
    v = sub.add_parser("verify")
    v.add_argument("--packet", required=True)
    args = parser.parse_args(argv)
    if args.command == "build":
        print(json.dumps(build(args.zip, authors_from(args.authors_from, args.author), args.out), ensure_ascii=False, indent=1))
        return 0
    result = verify(args.packet)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
