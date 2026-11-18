"""Free, pinned V8 revision trials. No model, credential, account or order access."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trading_lab.arena import write
from trading_lab.arena_strategies import validate_strategy, fingerprint
from trading_lab.strategy_training import code_hash, run_trial, synthetic_tape, read_recorded
from trading_lab.workshop_tools import compare_trials

DEFAULT = Path(__file__).resolve().parents[1] / "runtime/strategy_round_v8_2026_10_02/common"


def read(path, limit=512*1024*1024):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError("unsafe_or_missing_file")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def inside(path, parent):
    path, parent = Path(path), Path(parent).resolve()
    if path.is_symlink() or not path.resolve().is_relative_to(parent):
        raise ValueError("outside_scoped_directory")
    return path


def packet(root):
    root = Path(root).resolve()
    value = read(root / "manifest.json", 131072)
    if value.get("schema") != "strategy-round-v8-common/v1" or not value.get("development_only"):
        raise ValueError("packet_schema")
    if fingerprint({k: v for k, v in value.items() if k != "packet_sha256"}) != value.get("packet_sha256"):
        raise ValueError("packet_hash")
    if value["engine"]["training"] != code_hash():
        raise ValueError("engine_changed_reissue_equal_packet")
    for name, expected in value["files"].items():
        source = inside(root / name, root)
        if not source.is_file() or source.stat().st_size > 512*1024*1024 or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
            raise ValueError("packet_file_hash")
    return value


def dataset(root, value, identity):
    item = next((d for d in value["datasets"] if d["id"] == identity), None)
    if not item:
        raise ValueError("dataset_not_pinned")
    if item["kind"] == "SYNTHETIC":
        tapes = [(name, synthetic_tape(regime, item["tail_seconds"]), item["admission_seconds"])
                 for name, regime in (("Зростання", "up"), ("Падіння", "down"), ("Боковик", "range"))]
    else:
        tape = read_recorded(inside(Path(root) / item["file"], root))
        if len(tape) != item["observations"] or int(tape[-1]["at"]*1000) != item["end_at_ms"]:
            raise ValueError("recorded_evidence_mismatch")
        tapes = [(identity, tape, item["admission_seconds"])]
    return {"id": value["round_id"] + ":" + identity, "name": identity,
            "kind": item["kind"]}, tapes


def trial(root, author, recipe, identity):
    value = packet(root)
    if author not in value["authors"]:
        raise ValueError("unknown_author")
    scope = Path(root).resolve().parent / author
    scope.mkdir(exist_ok=True)
    recipe = Path(recipe)
    if not recipe.resolve().is_relative_to(scope) and recipe.resolve() != (Path(root) / "baselines" / (author + ".json")).resolve():
        raise ValueError("recipe_outside_own_scope_or_baseline")
    attempt = uuid.uuid4().hex
    destination = scope / "trials" / (attempt + ".json")
    pending = {"id": attempt, "status": "PENDING", "created_at": datetime.now(timezone.utc).isoformat(),
               "runtime": "RULES_ONLY", "ai_cost_usd": 0, "packet_sha256": value["packet_sha256"],
               "dataset_id": identity, "author_scope": author}
    write(destination, pending)
    try:
        strategy = validate_strategy(read(recipe, 131072))
        data, tapes = dataset(root, value, identity)
        result = run_trial(deepcopy(strategy), data, value["budget_usdt"], tapes)
        result.update(id=attempt, packet_sha256=value["packet_sha256"], author_scope=author,
                      hypothesis_role="DEVELOPMENT_NOT_HOLDOUT", recipe_file_sha256=hashlib.sha256(recipe.read_bytes()).hexdigest())
        write(destination, result)
        return {"report": str(destination), "status": result["status"], "summary": result["summary"],
                "recipe_sha256": result["strategy_sha256"], "data_sha256": result["data_sha256"],
                "packet_sha256": value["packet_sha256"], "financial_actions": False}
    except Exception:
        write(destination, {**pending, "status": "FAILED", "error_code": "validation_or_training_failure"})
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet-root", type=Path, default=DEFAULT)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("verify")
    test = commands.add_parser("test")
    test.add_argument("--author", choices=("sol", "astra", "opus", "sonnet"), required=True)
    test.add_argument("--recipe", type=Path, required=True)
    test.add_argument("--dataset", choices=("synthetic", "live5h", "live15m"), required=True)
    compare = commands.add_parser("compare")
    compare.add_argument("--author", choices=("sol", "astra", "opus", "sonnet"), required=True)
    compare.add_argument("--left", type=Path, required=True)
    compare.add_argument("--right", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        value = packet(args.packet_root)
        if args.command == "verify":
            result = {"verified": True, "packet_sha256": value["packet_sha256"], "files": len(value["files"]), "paid": False}
        elif args.command == "test":
            result = trial(args.packet_root, args.author, args.recipe, args.dataset)
        else:
            scope = args.packet_root.resolve().parent / args.author
            left, right = read(inside(args.left, scope)), read(inside(args.right, scope))
            if left.get("packet_sha256") != right.get("packet_sha256") or left.get("packet_sha256") != value["packet_sha256"]:
                raise ValueError("comparison_packet_mismatch")
            result = compare_trials(left, right)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        print("Pinned trial rejected; PENDING/FAILED histories are preserved. No network or orders.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
