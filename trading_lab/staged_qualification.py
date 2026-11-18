"""Import already computed free qualification receipts under the server's manager lock.

Never starts training, market/model transports, workers or exchange actions.
The running old server keeps its manifest until the owner updates it.
"""
from copy import deepcopy
import hashlib
import json
from .arena import IDENTITY, write
from .arena_strategies import ArenaError
from .strategy_training import code_hash


def apply(workshop, exchange):
    active = exchange.status().get("active", {})
    if any(v and v.get("status") != "DONE" for v in active.values()) or (
            workshop.arena.session and workshop.arena.session.get("status") != "DONE"):
        return {"status": "DEFERRED", "imported": 0}
    imported = 0
    for receipt_path in sorted((workshop.path / "qualification_pending").glob(code_hash()+".*.receipt.json")):
        previous = deepcopy(workshop.data)
        try:
            if receipt_path.is_symlink() or receipt_path.stat().st_size > 131072:
                raise ArenaError("receipt_shape")
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if (set(receipt) != {"schema", "code_sha256", "items"} or receipt["schema"] != "free-qualification/v1"
                    or receipt["code_sha256"] != code_hash() or not isinstance(receipt["items"], list)
                    or not 1 <= len(receipt["items"]) <= 6):
                raise ArenaError("receipt_epoch")
            updates, seen = [], set()
            for item in receipt["items"]:
                if set(item) != {"version_id", "trial_id", "report_sha256"} or not all(
                        IDENTITY.fullmatch(item[key]) for key in ("version_id", "trial_id")) or item["version_id"] in seen:
                    raise ArenaError("receipt_identity")
                seen.add(item["version_id"])
                version = workshop.version(item["version_id"])
                path = receipt_path.parent / (item["trial_id"] + ".report.json")
                if path.is_symlink() or path.stat().st_size > 512*1024*1024:
                    raise ArenaError("report_shape")
                raw = path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != item["report_sha256"]:
                    raise ArenaError("report_hash")
                report = json.loads(raw)
                if (version["status"] != "frozen" or report.get("id") != item["trial_id"]
                        or report.get("version_id") != version["id"] or report.get("status") != "COMPLETE"
                        or report.get("runtime") != "RULES_ONLY" or report.get("ai_cost_usd") != 0
                        or report.get("strategy_sha256") != version["recipe_sha256"]
                        or report.get("code_sha256") != receipt["code_sha256"] or not report.get("data_sha256")):
                    raise ArenaError("report_qualification")
                if item["trial_id"] not in version["trial_ids"]:
                    updates.append((version, report))
            for version, report in updates:
                report.update(attempt_number=version["trial_count"]+1, lineage_parent_id=version["parent_id"])
                write(workshop.path / "trials" / (report["id"] + ".json"), report)
                workshop.data["trials"].append({k: deepcopy(v) for k, v in report.items() if k not in {"runs", "feedback", "strategy"}})
                version["trial_ids"].append(report["id"])
                version["trial_count"] += 1
                version.setdefault("qualification_history", []).append({k: version.get(k) for k in
                    ("qualification_trial_id", "qualification_code_sha256", "qualification_runtime", "frozen_at")})
                version.update(qualification_trial_id=report["id"], qualification_code_sha256=receipt["code_sha256"],
                               qualification_runtime="RULES_ONLY")
            if updates:
                workshop.save()
                imported += len(updates)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            workshop.data = previous
            return {"status": "BLOCKED", "imported": imported, "reason": "pending_receipt_invalid_or_stale"}
    return {"status": "APPLIED" if imported else "UNCHANGED", "imported": imported}
