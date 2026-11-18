"""Explicit ordinary execution policies; no transport or model calls."""
import hashlib
import json
from .arena_strategies import ArenaError
from .cycle_probe import is_probe
from . import demo_execution
from .match_protocol import validate_manifest

RULES_ONLY = "rules_only"
AI_ROLE = "RULES_ONLY: формальний рецепт і ризик перевіряє код; Jev payload записується для офлайн SHADOW, не надсилається."
SHADOW_POLICY = {"schema": "recorded-jev-shadow/v1", "send_during_match": False,
                 "affects_execution": False, "replay": "offline_owner_probe"}


def capability():
    return {"version": 1, "execution_mode": RULES_ONLY, "uses_jev": False,
            "contexts": ["workshop_demo", "arena_live"], "shadow": "offline_owner_paid_from_journal",
            "label": "RULES_ONLY · Jev SHADOW"}


def validate_request(mode, context, recipes, budget, duration):
    if mode == RULES_ONLY:
        if context not in {"workshop_demo", "arena_live"} or any(is_probe(r) for r in recipes):
            raise ArenaError("RULES_ONLY: лише звичайні рецепти Demo/Live; технічний Jev-план має власний режим.")
        return False
    return demo_execution.validate_request(mode, context, recipes, budget, duration)


def validate_state(state):
    validate_manifest(state)
    manifest = state.get("match_manifest") or {}
    recipes = [r for r in state["strategies"] if r["id"] != "cash"]
    if (state.get("execution_mode") != RULES_ONLY or state.get("protocol_version", 0) < 8
            or state.get("context") not in {"workshop_demo", "arena_live"}
            or state.get("mode") != ("OKX_DEMO" if state["context"] == "workshop_demo" else "OKX_LIVE")
            or any(is_probe(r) for r in recipes)
            or state["rules"].get("ai_role") != AI_ROLE
            or manifest.get("jev_budget") is not None
            or manifest.get("shadow_policy") != SHADOW_POLICY
            or manifest.get("decision_policy") != capability()
            or manifest.get("evaluation") != "forward_owner_match"):
        raise ArenaError("rules_only_execution_manifest_required")


def decision(state, candidates):
    """Capture the same wire payload as the gate, without producing an AI answer."""
    from .arena_jev import make_payload
    from .arena_strategies import fingerprint
    validate_state(state)
    payload = make_payload(candidates, state["mode"])
    wire = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return {"attempted": False, "reported_cost_usd": 0, "answers": {}, "elapsed_ms": 0,
            "reason": "formal_rules", "allowed": [c["id"] for c in candidates
                if c.get("local_checks_passed") is True and c.get("kind") in {"entry", "add"}],
            "payload": payload, "payload_wire_json": wire,
            "payload_sha256": hashlib.sha256(wire.encode()).hexdigest(),
            "payload_content_sha256": fingerprint(payload), "payload_bytes": len(wire.encode())}
