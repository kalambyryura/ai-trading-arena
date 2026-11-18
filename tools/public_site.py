"""Local-only public site builder. A receipt is the owner's recorded decision; nothing here uploads anything.

  py -3.12 -X utf8 -B tools/public_site.py receipt exchange:<id> --output receipts/<id>.json
  py -3.12 -X utf8 -B tools/public_site.py build --battle exchange:<id> --battle paper:<id> --receipts receipts --output <new folder>
  py -3.12 -X utf8 -B tools/public_site.py verify <folder>
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trading_lab.arena_strategies import ArenaError
from trading_lab.public_site import build, collect, load_journal, receipt_for, verify_site

ROOT = Path(__file__).resolve().parents[1]


def parse_battle(text):
    kind, _, identity = text.partition(":")
    if kind not in ("paper", "exchange") or not identity:
        raise argparse.ArgumentTypeError("Формат: paper:<id> або exchange:<id>.")
    return kind, identity


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    receipt = commands.add_parser("receipt", help="PENDING receipt для одного бою; це ще не дозвіл")
    receipt.add_argument("battle", type=parse_battle)
    receipt.add_argument("--root", type=Path, default=ROOT)
    receipt.add_argument("--output", type=Path, required=True)
    built = commands.add_parser("build", help="зібрати нову теку сайту з боїв, що мають approved receipt")
    built.add_argument("--battle", type=parse_battle, action="append", required=True, metavar="KIND:ID")
    built.add_argument("--receipts", type=Path, required=True, help="тека з <id>.json receipt на кожен бій")
    built.add_argument("--output", type=Path, required=True, help="нова тека; існуюча не перезаписується")
    built.add_argument("--root", type=Path, default=ROOT)
    built.add_argument("--web", type=Path, default=ROOT / "web")
    built.add_argument("--index", action="store_true", help="дозволити індексацію пошуковиками (без прапорця — noindex)")
    built.add_argument("--original-assets", action="store_true", help="копіювати PNG-оригінали замість похідних WebP (Pillow)")
    verify = commands.add_parser("verify", help="перевірити manifest, receipts, replay/review DTO та відсутність приватних маркерів")
    verify.add_argument("site", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "receipt":
            state = load_journal(args.root, *args.battle)
            with args.output.open("x", encoding="utf-8") as handle:
                json.dump(receipt_for(state), handle, ensure_ascii=False, indent=2)
            print("Створено PENDING receipt; це не дозвіл публікації.")
            return 0
        if args.command == "build":
            collected = collect(args.root, args.battle, args.receipts)
            for skipped in collected["skipped"]:
                print(f"Пропущено {skipped['kind']}:{skipped['id']} — {skipped['reason']}", file=sys.stderr)
            result = build(args.output, args.web, collected["items"], indexable=args.index, derive=not args.original_assets)
            print(json.dumps({**result, "skipped": collected["skipped"]}, ensure_ascii=False))
            print("Теку зібрано локально; публікацію виконує власник після verify.")
            return 0
        print(json.dumps(verify_site(args.site), ensure_ascii=False))
        return 0
    except (ArenaError, OSError, ValueError, KeyError, TypeError) as error:
        print("Збірку заблоковано: " + (str(error) if isinstance(error, ArenaError) else "потрібні чинні журнали, receipts і нова тека."), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
