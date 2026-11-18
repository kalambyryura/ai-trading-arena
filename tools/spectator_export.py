"""Local-only exporter. Pending receipt is not approval; nothing is uploaded."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trading_lab.spectator_export import export, receipt_template


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("receipt", "build"))
    parser.add_argument("packet", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.packet.stat().st_size > 512*1024*1024: raise ValueError()
        packet = json.loads(args.packet.read_text(encoding="utf-8-sig"))
        if args.command == "receipt":
            result = receipt_template(packet)
            with args.output.open("x", encoding="utf-8") as handle: json.dump(result, handle, ensure_ascii=False, indent=2)
            print("Створено PENDING receipt; це не дозвіл публікації.")
        else:
            if not args.receipt or args.receipt.stat().st_size > 8192: raise ValueError()
            proof = export(packet, json.loads(args.receipt.read_text(encoding="utf-8-sig")), args.output, Path(__file__).resolve().parents[1]/"web")
            print(json.dumps(proof, ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError):
        print("Експорт заблоковано: потрібен чинний packet, approved receipt і нова тека.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__": raise SystemExit(main())
