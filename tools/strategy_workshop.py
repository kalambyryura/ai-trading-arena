"""Free author SDK: validate/lint/explain/lots/paired replay. No keys or orders."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trading_lab.arena import write
from trading_lab.arena_strategies import ArenaError, validate_strategy
from trading_lab.strategy_contract import author_contract
from trading_lab.strategy_training import run_trial, read_recorded, SYNTHETIC
from trading_lab.workshop_diagnostics import diagnostic_rules
from trading_lab.workshop_tools import lint_rules, explain_predicate, lots_preview, compare_trials

def read_json(path, maximum=131072):
    if path.is_symlink() or path.stat().st_size > maximum: raise ArenaError("input_size_or_symlink")
    return json.loads(path.read_text(encoding="utf-8-sig"))

def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("contract").add_argument("--output", type=Path)
    for name in ("validate", "lint", "explain", "lots", "test"):
        command = commands.add_parser(name); command.add_argument("recipe", type=Path)
        command.add_argument("--output", type=Path)
        if name == "explain": command.add_argument("--features-json", type=Path, required=True)
        if name == "lots": command.add_argument("--input-json", type=Path, required=True)
        if name == "test":
            command.add_argument("--budget", type=float, default=100)
            command.add_argument("--recording", type=Path)
            command.add_argument("--admission-seconds", type=int)
            command.add_argument("--log-root", type=Path, help="All-attempt history; default beside output or runtime/workshop_sdk")
    compare = commands.add_parser("compare")
    compare.add_argument("left", type=Path); compare.add_argument("right", type=Path); compare.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    history = pending = None
    if args.command == "test":
        history = (args.log_root or ((args.output.parent / "trial_history") if args.output else Path.cwd() / "runtime" / "workshop_sdk")) / (uuid.uuid4().hex + ".json")
        pending = {"id": history.stem, "status": "PENDING", "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "runtime": "RULES_ONLY", "ai_cost_usd": 0, "error_code": None}
        write(history, pending)
    try:
        if args.command == "contract": result = author_contract()
        elif args.command == "compare": result = compare_trials(read_json(args.left, 512*1024*1024), read_json(args.right, 512*1024*1024))
        elif args.command in ("validate", "lint") and isinstance(payload := read_json(args.recipe), dict) and payload.get("strategy_schema_version") == 4:
            # SDK v3 (schema 4): official gates in tools/team_eval.py; the executed instruments also enter the workshop and arena.
            from trading_lab.strategy_v4 import validate_payload, required_features
            from trading_lab.strategy_v4_runtime import EXECUTED_INSTRUMENTS
            strategy = validate_payload(payload)
            btc = strategy["instrument"] == "BTC-USDT-SWAP"
            result = {"valid": True, "name": strategy["name"], "recipe_sha256": strategy["recipe_sha256"], "strategy_schema_version": 4,
                      "engines": ("workshop, PAPER, Demo and Live (RULES_ONLY)" if btc else "workshop (synthetic), Demo and Live (RULES_ONLY)"
                                  if strategy["instrument"] in EXECUTED_INSTRUMENTS else "EVAL_ONLY")}
            if args.command == "lint":
                result.update(features=sorted(required_features(strategy["rules"])), warnings=[],
                              limitations=["Official gates: tools/team_eval.py (candle engine). The exact engines run schema 4 without Jev; "
                                           "PAPER reads the BTC market only, other instruments run on Demo and Live.",
                                           "Formal validation is not evidence of edge."])
        else:
            strategy = validate_strategy(read_json(args.recipe))
            rules = diagnostic_rules(strategy)
            if args.command == "validate": result = {"valid": True, "name": strategy["name"], "recipe_sha256": strategy["recipe_sha256"]}
            elif args.command == "lint": result = lint_rules(rules)
            elif args.command == "explain":
                values = read_json(args.features_json)
                result = {"evidence_kind": "PROVIDED_INPUTS", "execution_started": False, "entry": {
                    side: {key: explain_predicate(predicate, values) for key, predicate in branch.items() if key in {"setup", "confirm", "invalidate"} and predicate is not None}
                    for side, branch in rules["entry"].items()}, "position": {
                    key: explain_predicate(rules[key], values) for key in ("exit", "add_when", "reduce_when") if rules[key] is not None}}
            elif args.command == "lots": result = lots_preview(size_pct=rules["size_pct"], **read_json(args.input_json))
            else:
                tapes = None; dataset = SYNTHETIC
                if args.recording:
                    tape = read_recorded(args.recording)
                    receipt_path = args.recording.with_name(args.recording.name + ".receipt.json")
                    fixture = False
                    if receipt_path.exists():
                        receipt = read_json(receipt_path)
                        if hashlib.sha256(args.recording.read_bytes()).hexdigest() != receipt.get("sha256"): raise ArenaError("recording_hash_mismatch")
                        fixture = receipt.get("fixture_only") is True
                    dataset = {"id": "cli-recording", "name": args.recording.name, "kind": "RECORDED_FIXTURE" if fixture else "RECORDED_PUBLIC"}
                    duration = args.admission_seconds if args.admission_seconds is not None else min(86400, tape[-1]["at"] - tape[0]["at"] - 15)
                    if args.admission_seconds is not None and not 300 <= duration <= 86400: raise ArenaError("admission_seconds")
                    tapes = [(dataset["name"], tape, duration)]
                elif args.admission_seconds is not None: raise ArenaError("admission_requires_recording")
                result = run_trial(strategy, dataset, args.budget, tapes); result["id"] = pending["id"]
                write(history, result)
        output = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output + "\n", encoding="utf-8")
            print(json.dumps({"saved": str(args.output), "status": result.get("status"), "summary": result.get("summary"), "history_id": pending["id"] if pending else None}, ensure_ascii=False))
        else: print(output)
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        if history: write(history, {**pending, "status": "FAILED", "error_code": "recipe_dataset_validation_or_io"})
        print("Recipe/dataset validation failed; training attempts remain in local history.", file=sys.stderr)
        return 1

if __name__ == "__main__": raise SystemExit(main())
