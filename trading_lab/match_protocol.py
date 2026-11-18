"""Public, immutable match rules and bounded admission/drain limits. No I/O."""
from copy import deepcopy
from datetime import datetime, timezone
import math

from .arena_strategies import ArenaError, fingerprint
from .strategy_v4_feed import request_budget as book_request_budget

VERSION = 8
TIME_POLICY = "admission_then_drain"
MAX_DURATION_SECONDS = 86400
DURATIONS = (300, 900, 1800, 3600, 7200, 18000, 86400)
MAX_HOLD_SECONDS = 86400
JOURNAL_BYTES = 512 * 1024 * 1024
REPLAY_QUOTES = 36000
SCORE_POLICY = {"id": "filled_entry_net_v1", "primary": "RECONCILED_NET_USDT",
                "tie_tolerance_usdt": "0.00000001", "include_cash": False,
                "minimum_entry_filled_orders": 1}
LEGACY_SCORE_POLICY = {"id": "legacy_net_with_cash_v1", "primary": "RECONCILED_NET_USDT",
                       "tie_tolerance_usdt": "0.00000001", "include_cash": True}


def score_policy(source):
    """Missing means historical rules. An unknown explicit policy never becomes a default."""
    holder = source.get("match_manifest") or source
    if "score_policy" not in holder:
        return deepcopy(LEGACY_SCORE_POLICY)
    raw = holder["score_policy"]
    if raw == SCORE_POLICY:
        return deepcopy(SCORE_POLICY)
    legacy = {k: v for k, v in LEGACY_SCORE_POLICY.items() if k != "id"}
    if raw in (legacy, LEGACY_SCORE_POLICY):
        return deepcopy(LEGACY_SCORE_POLICY)
    return None


def entry_participation(funnel):
    """Only recorded filled entry orders count; an intent/signal/add isn't an entry."""
    filled = funnel.get("entry_filled_orders") if isinstance(funnel, dict) else None
    if type(filled) is not int or filled < 0:
        return {"status": "UNKNOWN", "entry_filled_orders": None, "eligible_to_win": False}
    return {"status": "PARTICIPATED" if filled else "NO_ENTRY", "entry_filled_orders": filled,
            "eligible_to_win": filled > 0}


def draining_policy(state):
    # Missing policy means the original journal's hard deadline, never a migration.
    return state.get("time_policy") == TIME_POLICY


def capacities(duration, strategies):
    holds = [s["rules"]["max_hold_seconds"] for s in strategies if s.get("rules")]
    # a post-only order placed before the cutoff may rest for its timeout before the hold starts (D096)
    waits = [s["rules"]["order"]["timeout_seconds"] for s in strategies if (s.get("rules") or {}).get("order", {}).get("type") == "post_only"]
    hold = max((v for v in holds if v is not None), default=0) + max(waits, default=0)
    observations = math.ceil((duration + hold + 120) / 5) + 128
    participants = sum(s["id"] != "cash" for s in strategies)
    g1 = any(s.get('strategy_schema_version') == 3 for s in strategies)
    extra_gets = 2 * math.ceil((duration + hold + 120) / 60) + 8 if g1 else 0
    book_gets = book_request_budget(strategies, duration + hold + 120)  # schema 4 only; its own allowance
    return {"max_observations": observations, "max_public_gets": observations * 4 + 61 * participants * 4 + 512 + extra_gets,
            **({"max_book_gets": book_gets} if book_gets else {}),
            "resource_policy": "rolling_health_blocks_admission_only", "unbounded_strategy_hold": None in holds,
            "max_api_requests_per_account": observations * 20 + 4096,
            "recovery_request_reserve": 4096, "snapshot_journal_bytes": 2 * 1024**3 if g1 else JOURNAL_BYTES,
            "chart_interval_seconds": max(5, math.ceil((duration + hold + 120) / 1440)),
            "checkpoint_interval_seconds": min(60, max(5, math.ceil((duration + hold) / 1440))),
            **({"snapshot_line_bytes": 48 * 1024, "snapshot_rows": 24000} if g1 else {})}


def install(state, account_ids=None, jev_budget=None):
    """Call once, after mode/rules/recipes are fixed and before reserving a season."""
    state.update(protocol_version=VERSION, time_policy=TIME_POLICY, phase="OPEN",
                 admission_end_at=state["end_at"], admission_closed_at=None)
    state["rules"].update(capacities(state["duration_seconds"], state["strategies"]))
    participants = [{"strategy_id": s["id"], "version": s["version"],
                     "recipe_sha256": s["recipe_sha256"], "author": deepcopy(s["author"]),
                     "max_hold_seconds": s["rules"]["max_hold_seconds"],
                     "size_pct": s["rules"]["size_pct"]}
                    for s in state["strategies"] if s["id"] != "cash"]
    if account_ids is not None:
        for participant, account in zip(participants, account_ids):
            participant["account_id"] = account  # Public local profile name, never UID/key.
    manifest = {"version": VERSION, "mode": state["mode"], "instrument": state["rules"].get("instrument", "BTC-USDT-SWAP"),
                "admission_start_basis": "run_reservation_includes_preparation",
                "execution_mode": state.get("execution_mode", "rules"), "participants": participants,
                "budget_usdt_per_bot": state["budget_usdt"], "started_at": state["started_at"],
                "admission_seconds": state["duration_seconds"], "admission_end_at": state["end_at"],
                "time_policy": TIME_POLICY, "no_add_after_deadline": True,
                "repeat_cycles": True, "risk_and_execution": deepcopy(state["rules"]),
                "jev_budget": deepcopy(jev_budget), "code_sha256": state["code_sha256"],
                "baseline": "cash_zero_usdt", "comparison_unit": "frozen_strategy",
                "score_policy": deepcopy(SCORE_POLICY),
                "provenance": "author_self_reported", "evaluation": "forward_owner_match"}
    state["match_manifest"] = manifest
    state["match_manifest_sha256"] = fingerprint(manifest)


def public_phase(state):
    if state["status"] in {"DONE", "PAUSED", "CLOSING", "DRAINING"}:
        return state["status"]
    return "OPEN"


def close_admission(state, stamp, emit):
    """Cut off entry/add; an already submitted order remains owned and reconciled."""
    if not draining_policy(state) or state.get("admission_closed_at"):
        return
    state["admission_closed_at"] = datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="milliseconds")
    state["phase"] = "DRAINING"
    if state["status"] == "RUNNING":
        state["status"] = "DRAINING"
    for bot in state["bots"]:
        if bot["id"] == "cash":
            continue
        pending = bot.get("_pending")
        if pending and pending["kind"] == "entry":
            from .arena_engine import cancel_entry
            cancel_entry(state, bot)
        bot["_approval"] = None
    emit(state, stamp, None, 4, "admission_closed",
         "Час нових входів завершено. Докупівель немає; відкриті позиції завершують свої цикли.",
         admission_end_at=state["admission_end_at"])


def validate_manifest(state):
    manifest = state.get("match_manifest")
    if manifest is None:
        return
    if fingerprint(manifest) != state.get("match_manifest_sha256"):
        raise ArenaError("match_manifest_changed")
    expected = {"mode": state["mode"], "execution_mode": state.get("execution_mode", "rules"),
                "time_policy": state.get("time_policy"), "budget_usdt_per_bot": state["budget_usdt"],
                "admission_seconds": state["duration_seconds"], "admission_end_at": state["end_at"],
                "started_at": state["started_at"], "code_sha256": state["code_sha256"],
                "risk_and_execution": state["rules"]}
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise ArenaError("match_state_differs_from_manifest")
    participants = {p["strategy_id"]: p for p in manifest["participants"]}
    recipes = {s["id"]: s for s in state["strategies"] if s["id"] != "cash"}
    if participants.keys() != recipes.keys() or any(
            participants[i]["recipe_sha256"] != r["recipe_sha256"]
            or participants[i]["max_hold_seconds"] != r["rules"]["max_hold_seconds"]
            or participants[i]["size_pct"] != r["rules"]["size_pct"] for i, r in recipes.items()):
        raise ArenaError("match_recipe_differs_from_manifest")
