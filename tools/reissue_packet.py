"""Reissue an equal strategy-round packet for the current engine hashes, next to the old one (D098).

The files are copied byte for byte and re-hashed; only the manifest's engine hashes, round id, provenance and
packet_sha256 change. An existing destination is never overwritten, the old packet stays as it was, so receipts
and journals that pin it remain verifiable. Run it from the checkout whose code the packet must match.

  py -3.12 -X utf8 -B tools/reissue_packet.py --from runtime/strategy_round_next_2026_10_26_v2/common \
      --to runtime/strategy_round_next_2026_10_29_r1/common --round-id strategy_round_next_2026_10_29_r1
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def reissue(source, target, round_id):
    from trading_lab.arena import arena_source_hash
    from trading_lab.arena_strategies import fingerprint
    from trading_lab.exchange_lab import source_hash
    from trading_lab.strategy_training import code_hash
    source, target = Path(source), Path(target)
    if target.exists():
        raise SystemExit(f"{target} exists: an equal packet is never overwritten")
    old = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if fingerprint({k: v for k, v in old.items() if k != "packet_sha256"}) != old.get("packet_sha256"):
        raise SystemExit("source manifest does not match its own packet_sha256")
    for name, expected in old["files"].items():
        if sha256_file(source / name) != expected:
            raise SystemExit(f"source file {name} differs from its manifest")
    for name in old["files"]:  # like strategy_round.inside(): a manifest may not point outside its packet
        for base in (source, target):
            if not (base / name).resolve().is_relative_to(base.resolve()):
                raise SystemExit(f"manifest entry {name} leaves the packet folder")
    target.mkdir(parents=True)
    for name in old["files"]:
        (target / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target / name)
        if sha256_file(target / name) != old["files"][name]:
            raise SystemExit(f"copy of {name} differs")
    new = {k: v for k, v in old.items() if k != "packet_sha256"}
    new.update(round_id=round_id, engine={"training": code_hash(), "exchange": source_hash(), "paper": arena_source_hash()},
               supersedes_packet_sha256=old["packet_sha256"], prepared_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
               reissue={"reason": "D098 EOL-independent core hashes; files byte-identical to the superseded packet",
                        "from": str(source).replace("\\", "/"), "previous_engine": old["engine"]})
    new["packet_sha256"] = fingerprint(new)
    (target / "manifest.json").write_bytes((json.dumps(new, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return {"packet_sha256": new["packet_sha256"], "engine": new["engine"], "files": len(new["files"]), "target": str(target)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="source", required=True)
    parser.add_argument("--to", dest="target", required=True)
    parser.add_argument("--round-id", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(reissue(args.source, args.target, args.round_id), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
