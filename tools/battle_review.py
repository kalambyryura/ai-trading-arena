"""Offline author bundle producer/verifier. Reads a local journal, no APIs."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trading_lab.battle_review import build_review, verify_bundle
from trading_lab.event_replay import build_packet, verify_packet
from trading_lab.arena_strategies import ArenaError


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("bundle", "verify", "replay", "verify-replay"))
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--include-jev-requests", action="store_true", help="Private author bundle with exact structured request evidence; never spectator export")
    args = parser.parse_args()
    try:
        if args.source.stat().st_size > 512 * 1024 * 1024:
            raise ArenaError("Завеликий файл.")
        source = json.loads(args.source.read_text(encoding="utf-8-sig"))
        if args.include_jev_requests and args.command != "bundle":
            raise ArenaError("Exact requests are private author evidence only.")
        if args.command in {"bundle", "replay"}:
            from trading_lab.match_journal import restore_events
            restore_events(args.source.with_name(args.source.stem + ".events.jsonl"), source, full=True)
            result = (build_review(source, include_request_payloads=args.include_jev_requests)
                      if args.command == "bundle" else build_packet(source))
        else: result = source
        proof = verify_packet(result) if args.command in {"replay", "verify-replay"} else verify_bundle(result)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        print(json.dumps(proof, ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError):
        print("Нечинний або неповний локальний журнал/bundle.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__": raise SystemExit(main())
