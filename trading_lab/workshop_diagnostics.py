"""Read-only recipe diagnostics; provided/synthetic snapshots never imply a trade."""
from copy import deepcopy
from decimal import Decimal
from .arena_strategies import ArenaError, validate_strategy
from .strategy_contract import payload_of
from .strategy_v2 import MARKET_FEATURES
from .workshop_tools import lint_rules, explain_predicate, lots_preview, compare_trials


def diagnostic_rules(recipe):
    rules = deepcopy(recipe["rules"])
    if recipe.get("strategy_schema_version") == 2: return rules
    def predicate(group):
        return {group["mode"]: deepcopy(group["conditions"])} if group["conditions"] else None
    rules.update(entry={rules["side"]: {"setup": None, "confirm": predicate(rules["entry"]), "invalidate": None, "expiry_bars": None}},
        exit=predicate(rules["exit"]), add_when=None, cost_checkpoint=None, reduce_when=None, reduce_pct=None)
    return rules  # Diagnostic projection only; it is never saved or executed as a new recipe.


def report(workshop, body):
    allowed = {"version_id", "strategy", "features", "snapshot", "instrument", "budget_usdt", "equity_usdt", "left_trial_id", "right_trial_id"}
    if not isinstance(body, dict) or set(body) - allowed or ("version_id" in body) == ("strategy" in body):
        raise ArenaError("Вкажи один рецепт або одну версію.")
    version = workshop.version(body["version_id"]) if "version_id" in body else None
    try:
        recipe = version["strategy"] if version else validate_strategy(body["strategy"])
    except (ArenaError, ValueError, KeyError, TypeError) as error:
        return {"lint": [{"path": "strategy", "message": str(error)[:240], "hint": "Виправ формальний рецепт; перевірка не змінює його."}],
                "explain": [], "lots": {"result": "UNKNOWN", "reasons": ["Нечинний рецепт."]}, "trials": [],
                "compare": {"comparable": False, "rows": [], "reason": "Потрібен чинний рецепт."}}
    rules = diagnostic_rules(recipe)
    findings = lint_rules(rules)
    features = body.get("features", {})
    if not isinstance(features, dict) or set(features) - (MARKET_FEATURES | {"position_age_seconds", "gross_gain_bps", "net_gain_bps", "entry_cost_hurdle_bps", "entries"}):
        raise ArenaError("Непідтримувані ознаки.")
    explanation = []
    for side, branch in rules["entry"].items():
        for label in ("setup", "confirm", "invalidate"):
            if branch[label] is None: continue
            result = explain_predicate(branch[label], features)
            explanation.append({"path": "entry."+side+"."+label, "actual": result["root_verdict"], "expected": "root PASS", "state": result["root_verdict"]})
            explanation += [{"path": "entry."+side+"."+label+"."+c["feature"], "actual": c["actual"], "expected": str(c["op"])+" "+str(c["expected"]),
                "state": "UNKNOWN" if c["passed"] is None else "PASS" if c["passed"] else "FAIL"} for c in result["leaves"]]
    for label in ("exit", "reduce_when", "add_when"):
        if rules[label] is not None:
            result = explain_predicate(rules[label], features)
            explanation.append({"path": label, "actual": result["root_verdict"], "expected": "root PASS", "state": result["root_verdict"]})
    lots = {"result": "UNKNOWN", "reasons": ["Надай snapshot та instrument для точного розрахунку; UNKNOWN не означає дозвіл."]}
    if body.get("snapshot") and body.get("instrument") and "budget_usdt" in body and "equity_usdt" in body:
        side = "long" if rules["side"] == "both" else rules["side"]
        sample = lots_preview(budget_usdt=body["budget_usdt"], size_pct=rules["size_pct"], side=side,
            snapshot=body["snapshot"], instrument=body["instrument"], equity_usdt=body["equity_usdt"])
        lots = {**sample, "result": "PASS" if sample["allowed"] else "UNEXECUTABLE", "reasons": sample["veto_reasons"],
                "lot_qty": str(Decimal(str(body["instrument"]["lot_step"])) * Decimal(str(body["instrument"]["contract_value"])) * Decimal(str(body["instrument"]["contract_multiplier"]))),
                "min_qty": str(Decimal(str(body["instrument"]["minimum_order_units"])) * Decimal(str(body["instrument"]["contract_value"])) * Decimal(str(body["instrument"]["contract_multiplier"]))),
                "requested_qty": sample["requested_quantity_btc"], "rounded_qty": sample["quantity_btc"], "rounded_notional_usdt": sample["actual_notional_usdt"],
                "source_at": body["snapshot"].get("at"), "policy_hash": "preview_only"}
        if rules["side"] == "both":
            other = lots_preview(budget_usdt=body["budget_usdt"], size_pct=rules["size_pct"], side="short",
                snapshot=body["snapshot"], instrument=body["instrument"], equity_usdt=body["equity_usdt"])
            lots["variants"] = [sample, other]
            lots["side"] = "both · наведені числа LONG; SHORT окремо у JSON"
            lots["reasons"] += ["SHORT: " + ("PASS" if other["allowed"] else ", ".join(other["veto_reasons"]))]
            lots["result"] = "PASS" if sample["allowed"] and other["allowed"] else "UNEXECUTABLE"
        lots["reasons"].append("Локальна оцінка: entry fee 5 bps, slippage 2 bps; остаточні fee/ліквідність перевіряє execution.")
    elif body.get("snapshot") or body.get("instrument"):
        lots["reasons"] = ["Потрібні snapshot, instrument, budget_usdt та equity_usdt; пропущені числа не підставляються."]
    lineage = version.get("lineage_id") if version else None
    versions = {v["id"]: v for v in workshop.data["versions"] if v.get("lineage_id") == lineage} if lineage else {}
    trials = [{"id": t["id"], "attempt_number": t.get("attempt_number"), "version_name": versions[t["version_id"]]["name"]+" v"+str(versions[t["version_id"]]["version"]),
               "dataset": t.get("dataset_name"), "status": t["status"], "evidence_kind": t.get("dataset_kind"),
               "net_usdt": (t.get("summary") or {}).get("pnl_usdt"), "trades": (t.get("summary") or {}).get("trades")}
              for t in workshop.data["trials"] if t["version_id"] in versions]
    comparison = {"comparable": False, "rows": [], "reason": "Обери дві повні проби на однакових даних та коді."}
    if body.get("left_trial_id") and body.get("right_trial_id"):
        paired = compare_trials(workshop.trial(body["left_trial_id"]), workshop.trial(body["right_trial_id"]))
        comparison = {"comparable": True, "rows": [{"path": r["name"]+"."+k, "before": "Базова проба", "after": value} for r in paired["paired_runs"] for k, value in r["delta_right_minus_left"].items()], "reason": paired["interpretation"]}
    return {"version_name": recipe["name"], "evidence_kind": "PROVIDED_INPUTS" if features else "UNKNOWN",
        "lint": [{"path": "rules", "message": warning["code"], "hint": warning["effect"]} for warning in findings["warnings"]],
        "explain": explanation, "lots": lots, "trials": trials, "compare": comparison, "execution_started": False}
