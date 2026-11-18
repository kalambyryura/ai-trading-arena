"""Read an existing ordinary V8 Demo journal. No API, model, Start/Stop/Resume."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trading_lab.arena import IDENTITY, write
from trading_lab.demo_v8_review import review
from trading_lab.match_journal import restore_events


def read_run(root, identity):
    if not IDENTITY.fullmatch(identity):
        raise ValueError("invalid_run_id")
    base = Path(root).resolve() / "runtime/exchange_v1"
    path = base / (identity + ".json")
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64*1024*1024:
        raise ValueError("missing_or_unsafe_journal")
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("id") != identity:
        raise ValueError("journal_identity")
    restore_events(base / (identity + ".events.jsonl"), state, full=True)
    return review(state)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        report = read_run(args.root, args.id)
        path = args.root.resolve() / "runtime/strategy_round_v8_2026_10_02/demo_receipts" / (args.id + ".json")
        write(path, report)
        print(json.dumps({"report": str(path), "status": report["status"], "evidence_kind": report["evidence_kind"],
            "all_primary_observed": report["all_primary_observed"], "rows": [{"id": r["id"], "status": r["status"]} for r in report["rows"]],
            "financial_actions": False}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        print("Existing V8 Demo evidence unavailable; no financial action was taken.", file=sys.stderr)
        return 1


if __name__ == "__main__": raise SystemExit(main())
