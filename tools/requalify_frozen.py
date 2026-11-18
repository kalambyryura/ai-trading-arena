"""Free requalification of already frozen recipes on the current code (D098, D106 p. 6; R2.1.13 plan, section 2).

Runs the free RULES_ONLY synthetic trial of each recipe, compares it with a baseline made by the previous code, and writes the
reports and the receipts trading_lab/staged_qualification.py imports at the first start of the updated panel. It writes only into
--out: never the workshop state, the catalogue or a paid ledger. No network, model, exchange or order.

The plan names, for each recipe, the version id ON THE MACHINE THAT WILL IMPORT IT (a report's version_id must equal it) and the
expected recipe_sha256. A receipt that fails stops the ones after it (they are imported in name order), so the V1 group gets the
first name and each candidate its own group:

  {"groups": [{"name": "1-v1", "items": [{"name": "sol", "version_id": "<32 hex>", "recipe_sha256": "<64 hex>", "payload": "recipe.json"},
                                         {"name": "astra", "version_id": "...", "recipe_sha256": "...", "manifest": "manifest.json", "source_version_id": "..."}]},
              {"name": "2-h10", "items": [...]}]}

  old tree:  py -3.12 -X utf8 -B tools/requalify_frozen.py --plan plan.json --baseline-out <dir>
  new tree:  py -3.12 -X utf8 -B tools/requalify_frozen.py --plan plan.json --compare-with <dir> --out <pending dir> --label r2113 --proof proof.json
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

HEX32, HEX64 = re.compile(r"[0-9a-f]{32}"), re.compile(r"[0-9a-f]{64}")
NAME = re.compile(r"[0-9a-z][0-9a-z-]{0,39}")
COMPARED = ("metrics", "curve")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8")
    Path(path).write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def load_plan(path):
    plan = read_json(path)
    groups = plan.get("groups") if isinstance(plan, dict) else None
    if not isinstance(groups, list) or not groups:
        raise SystemExit("the plan names no groups")
    seen = set()
    for group in groups:
        if not NAME.fullmatch(str(group.get("name"))) or not 1 <= len(group.get("items", [])) <= 6:
            raise SystemExit(f"bad group: {group.get('name')!r} (a receipt holds 1 to 6 items)")
        for item in group["items"]:
            if (not NAME.fullmatch(str(item.get("name"))) or not HEX32.fullmatch(str(item.get("version_id")))
                    or not HEX64.fullmatch(str(item.get("recipe_sha256"))) or ("payload" in item) == ("manifest" in item)):
                raise SystemExit(f"bad item: {item.get('name')!r}")
            if item["name"] in seen or item["version_id"] in seen:
                raise SystemExit(f"repeated in the plan: {item['name']}")
            seen.update((item["name"], item["version_id"]))
    return groups


def strategy_of(item, base):
    from trading_lab.arena_strategies import validate_strategy
    if "payload" in item:
        strategy = validate_strategy(read_json(base / item["payload"]))
    else:
        version = next((v for v in read_json(base / item["manifest"])["versions"] if v["id"] == item["source_version_id"]), None)
        if version is None or version.get("status") != "frozen":
            raise SystemExit(f"{item['name']}: no such frozen version in the manifest")
        strategy = deepcopy(version["strategy"])
    if strategy["recipe_sha256"] != item["recipe_sha256"]:
        raise SystemExit(f"{item['name']}: recipe_sha256 {strategy['recipe_sha256'][:12]} is not the one the plan expects")
    return strategy


def trial(item, base):
    from trading_lab.strategy_training import run_trial
    report = run_trial(strategy_of(item, base))
    if report["status"] != "COMPLETE" or report.get("ai_cost_usd") != 0 or report.get("runtime") != "RULES_ONLY":
        raise SystemExit(f"{item['name']}: the free trial is not COMPLETE, free and RULES_ONLY")
    report["version_id"] = item["version_id"]
    return report


def same(left, right):
    return left["summary"] == right["summary"] and len(left["runs"]) == len(right["runs"]) and all(
        a[field] == b[field] for a, b in zip(left["runs"], right["runs"]) for field in COMPARED)


def baseline(groups, base, out):
    """On the previous tree: what the code before the change gives, one file per recipe."""
    for group in groups:
        for item in group["items"]:
            report = trial(item, base)
            write_json(Path(out) / (item["name"] + ".json"), {"summary": report["summary"], "runs": [{f: r[f] for f in COMPARED} for r in report["runs"]],
                                                                 "code_sha256": report["code_sha256"]})
    return {"baselines": sum(len(g["items"]) for g in groups), "out": str(out)}


def requalify(groups, base, out, label, compare_with):
    """Reports and receipts for the current code. A drift from the baseline stops everything: it would mean the release changed a rule."""
    from trading_lab.strategy_training import code_hash
    if not NAME.fullmatch(label):
        raise SystemExit("bad label")
    out, rows, receipts = Path(out), [], {}
    out.mkdir(parents=True, exist_ok=True)
    training = code_hash()
    for group in groups:
        items = []
        for item in group["items"]:
            report = trial(item, base)
            prior = read_json(Path(compare_with) / (item["name"] + ".json"))
            if prior["code_sha256"] == training:
                raise SystemExit(f"{item['name']}: the baseline was made by this same code")
            if not same(report, prior):
                raise SystemExit(f"{item['name']}: metrics or curves differ from the baseline: the release changed a rule")
            digest = write_json(out / (report["id"] + ".report.json"), report)
            items.append({"version_id": item["version_id"], "trial_id": report["id"], "report_sha256": digest})
            rows.append({"group": group["name"], "name": item["name"], "version_id": item["version_id"], "recipe_sha256": item["recipe_sha256"],
                         "trial_id": report["id"], "status": report["status"], "runs": len(report["runs"]), "exact_prior_metrics_curves": True,
                         "baseline_code_sha256": prior["code_sha256"]})
        name = f"{training}.{group['name']}.{label}.receipt.json"
        receipts[name] = write_json(out / name, {"schema": "free-qualification/v1", "code_sha256": training, "items": items})
    return {"code_sha256": training, "receipts": receipts, "qualifications": rows}


def proof(result, path):
    from trading_lab.arena import arena_source_hash
    from trading_lab.exchange_lab import source_hash
    write_json(path, {"schema": "frozen-requalification/v1", "prepared_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "engine_hashes": {"training": result["code_sha256"], "exchange": source_hash(), "paper": arena_source_hash()},
                      "receipts": result["receipts"], "qualifications": result["qualifications"],
                      "network_calls": 0, "paid_calls": 0, "financial_actions": 0, "workshop_state_written": False})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--baseline-out")
    parser.add_argument("--compare-with")
    parser.add_argument("--out")
    parser.add_argument("--label")
    parser.add_argument("--proof")
    args = parser.parse_args(argv)
    groups, base = load_plan(args.plan), Path(args.plan).resolve().parent
    if args.baseline_out:
        print(json.dumps(baseline(groups, base, args.baseline_out), ensure_ascii=False))
        return 0
    if not (args.out and args.label and args.compare_with):
        parser.error("--out, --label and --compare-with go together")
    result = requalify(groups, base, args.out, args.label, args.compare_with)
    if args.proof:
        proof(result, args.proof)
    print(json.dumps({"code_sha256": result["code_sha256"], "receipts": result["receipts"], "items": len(result["qualifications"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
