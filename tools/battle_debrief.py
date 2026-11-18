"""Create a readable offline HTML/JSON debrief from an existing battle ZIP or review."""
import argparse
import json
from pathlib import Path
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trading_lab.arena_strategies import ArenaError
from trading_lab.battle_debrief import load_debrief, render_html


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="New HTML file; existing files are preserved")
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    try:
        targets = [p for p in (args.output, args.json_output) if p is not None]
        if len({p.resolve() for p in targets}) != len(targets) or any(p.exists() or p.resolve() == args.source.resolve() for p in targets):
            raise ArenaError("Вихід уже існує або збігається з джерелом; файл збережено.")
        report = load_debrief(args.source)
        html = render_html(report)
        for path in targets:
            path.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8", newline="\n") as output:
            output.write(html)
        if args.json_output:
            with args.json_output.open("x", encoding="utf-8", newline="\n") as output:
                json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
                output.write("\n")
        print(json.dumps({"status": "COMPLETE", "mode": report["match"]["mode"],
                          "participants": len(report["participants"]), "html": str(args.output)}, ensure_ascii=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
        print("Розбір не створено: " + (str(error) if isinstance(error, ArenaError) else type(error).__name__), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
