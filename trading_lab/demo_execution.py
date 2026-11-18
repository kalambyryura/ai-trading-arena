"""Explicit, pinned OKX Demo operating check. Never an AI approval or Live policy."""
from .arena_strategies import ArenaError, fingerprint
from .strategy_contract import payload_of
from .match_protocol import validate_manifest

MODE = "demo_mechanics"
RECIPE_SHA256 = "01dafc62827d69fab9e12cd578d5f647364fba549c93a44e9d119198f5fdc9bb"  # V8 DEMO ONLY short/partial/tail (BTC)
ETH_POST_ONLY_SHA256 = "aee9bdfcccaf083e3b9a593a89fb9af998ce9efa47b42a444d8a30daa359f8ac"  # R2 DEMO ONLY ETH post-only short
ETH_REST_SHA256 = "113628c20985bce497b04f3204ac8305494dd8625f52517b2e667532bff55ccf"  # R2 DEMO ONLY ETH post-only rest and timeout
ETH_LONG_SHA256 = "46adb4156dbbbe8aeaa457b1d4a46aa75571fcc157f08ba81029f8af4a53157f"  # R2 DEMO ONLY ETH post-only long
RECIPES = {RECIPE_SHA256: "Перевірка механіки · OKX Demo · без Jev",
           ETH_POST_ONLY_SHA256: "Механіка post-only ETH · OKX Demo · без Jev",
           ETH_REST_SHA256: "Механіка post-only ETH: очікування й тайм-аут · OKX Demo · без Jev",
           ETH_LONG_SHA256: "Механіка post-only ETH long · OKX Demo · без Jev"}
SIDES = {ETH_LONG_SHA256: "long"}  # every other pinned recipe enters short
AI_ROLE = "Технічна перевірка OKX Demo без Jev; рішення за зафіксованими правилами, не оцінка стратегії."


def capability(recipe_sha256=RECIPE_SHA256):
    """The pinned scope of one mechanics recipe; the BTC one is the manifest every earlier journal carries."""
    return {"version": 1, "execution_mode": MODE, "recipe_sha256": recipe_sha256,
            "context": "workshop_demo", "mode": "OKX_DEMO", "budget_usdt": 100,
            "duration_seconds": 300, "maximum_entry_attempts": 1, "uses_jev": False,
            "label": RECIPES[recipe_sha256]}


def capabilities():
    return [capability(identity) for identity in RECIPES]


def validate_request(execution_mode, context, recipes, budget, duration):
    if execution_mode == "shared_jev":
        return False
    if (execution_mode != MODE or context != "workshop_demo" or len(recipes) != 1
            or budget != 100 or duration != 300
            or recipes[0].get("recipe_sha256") not in RECIPES
            or fingerprint(payload_of(recipes[0])) != recipes[0]["recipe_sha256"]):
        raise ArenaError("Перевірка механіки: лише точний V8 Demo short/partial/tail або закріплений R2 ETH post-only рецепт, "
                         "один Demo-бот, 100 USDT і 5 хвилин.")
    return True


def validate_state(state):
    """Recheck immutable scope before preparation, approval and each new entry."""
    validate_manifest(state)
    recipes = [r for r in state["strategies"] if r["id"] != "cash"]
    bots = [b for b in state["bots"] if b["id"] != "cash"]
    if not validate_request(state.get("execution_mode"), state.get("context"), recipes,
                            state["budget_usdt"], state["duration_seconds"]):
        raise ArenaError("demo_mechanics_mode_required")
    manifest = state.get("match_manifest") or {}
    if (state.get("mode") != "OKX_DEMO" or state.get("protocol_version", 0) < 8
            or len(bots) != 1 or state["rules"].get("ai_role") != AI_ROLE
            or state["rules"].get("max_total_entries") != 1
            or manifest.get("evaluation") != "demo_execution_acceptance"
            or manifest.get("repeat_cycles") is not False
            or manifest.get("demo_execution_check") != capability(recipes[0]["recipe_sha256"])
            or manifest.get("jev_budget") is not None):
        raise ArenaError("demo_mechanics_scope_changed")
    return True


def entry_side(state):
    """The one side a mechanics run may enter: that of its pinned recipe."""
    recipe = next(r for r in state["strategies"] if r["id"] != "cash")
    return SIDES.get(recipe["recipe_sha256"], "short")


def allows(state, candidate):
    validate_state(state)
    bot = next((b for b in state["bots"] if b["id"] == candidate["id"]), None)
    return bool(bot and candidate.get("kind") == "entry" and candidate.get("side") == entry_side(state)
                and candidate.get("local_checks_passed") is True
                and not bot["stop_requested"] and not bot["position_btc"]
                and bot["_entry_attempts"] == 0 and state["status"] == "RUNNING")
