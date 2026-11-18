"""Release identity, light part (D106 p. 6, R1 full F4): full run_trial results of the frozen V1 baselines and the V2 draft
on the fixed tapes of a strategy-round package (synthetic suite, live5h, live15m), as one sha256 per recipe and tape.

Random ids, created_at and hashes that cover them or the engine's own code hash are masked, so two runs of the same code
print the same table and a release that changes no V1/V2 behaviour prints the same table as before it. Run it once on
the code before and once after (e.g. a `git archive` copy for "before") and compare the two outputs. Synthetic and
recorded public tapes only; no network, key, Jev or order.

  py -3.12 -X utf8 -B tools/release_identity.py --root . --common <round>/common --v2 <V8_DEMO_SHORT_PARTIAL_TAIL.json>
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

HEX32 = re.compile(r"\b[0-9a-f]{32}\b")
DERIVED = re.compile(r'"(config_sha256|match_manifest_sha256|code_sha256|engine_sha256)": "[0-9a-f]{64}"')


def normalized(result):
    text = json.dumps(result, sort_keys=True, ensure_ascii=False)
    seen = {}
    text = HEX32.sub(lambda m: seen.setdefault(m.group(0), f"ID{len(seen)}"), text)
    text = re.sub(r'"created_at": "[^"]*"', '"created_at": "X"', text)
    text = DERIVED.sub(lambda m: f'"{m.group(1)}": "H"', text)
    return hashlib.sha256(text.encode()).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, required=True, help="code to test (a checkout or an archive copy)")
    parser.add_argument("--common", type=Path, required=True, help="strategy-round common folder (baselines/, data/)")
    parser.add_argument("--v2", type=Path, required=True)
    args = parser.parse_args(argv)
    sys.path.insert(0, str(args.root.resolve()))
    from trading_lab.strategy_training import run_trial, read_recorded
    from trading_lab.arena_strategies import validate_strategy
    recipes = {path.stem: path for path in sorted((args.common / "baselines").glob("*.json"))}
    recipes["v2_draft"] = args.v2
    out = {}
    for name, path in recipes.items():
        strategy = validate_strategy(json.loads(path.read_text(encoding="utf-8-sig")))
        out[name + "/synthetic"] = normalized(run_trial(strategy))
        for tape_name in ("live5h", "live15m"):
            tape = read_recorded(args.common / "data" / f"{tape_name}.snapshots.jsonl")
            duration = min(86400, tape[-1]["at"] - tape[0]["at"] - 15)
            out[name + "/" + tape_name] = normalized(run_trial(strategy, {"id": tape_name, "name": tape_name, "kind": "RECORDED_PUBLIC"},
                                                               100, [(tape_name, tape, duration)]))
    print(json.dumps(out, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
