"""Versioned, declarative recipes. No eval, model calls, credentials or trading imports."""
from copy import deepcopy
from .cycle_probe import PLAN, RULES as PROBE_RULES, is_probe
from datetime import datetime, timezone
import hashlib
import json
import math
import operator
import uuid

FEATURES = {
    "sma_gap_bps", "return_5m_bps", "return_1m_bps", "rsi_14",
    "range_position_20", "volume_ratio_5", "spread_bps",
}
OPS = {"gt": operator.gt, "gte": operator.ge, "lt": operator.lt, "lte": operator.le}
LIMITS = {"size_pct": (5, 30), "max_entries": (1, 3), "add_after_bps": (20, 500),
          "stop_loss_bps": (20, 500), "take_profit_bps": (40, 1000),
          "max_hold_seconds": (30, 86400), "cooldown_seconds": (15, 600),
          "max_spread_bps": (.1, 20)}


class ArenaError(ValueError):
    pass


def number(value, low=-1e9, high=1e9):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ArenaError("Очікується звичайне число.")
    if not math.isfinite(value) or not low <= value <= high:
        raise ArenaError(f"Число має бути в межах {low}…{high}.")
    return value


def text_field(value, maximum, required=True):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ArenaError(f"Текст має містити від {1 if required else 0} до {maximum} символів.")
    if any(ord(c) < 32 and c not in "\n\t\r" for c in value):
        raise ArenaError("Недопустимі керувальні символи.")
    return value.strip()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def validate_group(group, minimum):
    if not isinstance(group, dict) or set(group) != {"mode", "conditions"} or group["mode"] not in {"all", "any"}:
        raise ArenaError("Умови потребують mode all/any та conditions.")
    rows = group["conditions"]
    if not isinstance(rows, list) or not minimum <= len(rows) <= 6:
        raise ArenaError(f"Потрібно {minimum}…6 умов.")
    result = []
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"feature", "op", "value"}
                or row["feature"] not in FEATURES or row["op"] not in OPS):
            raise ArenaError("Непідтримувана ознака або оператор умови.")
        result.append({"feature": row["feature"], "op": row["op"], "value": number(row["value"])})
    return {"mode": group["mode"], "conditions": result}


PASSPORT = {"name", "kind", "model", "submitted_at", "prompt", "attempts", "edited_by"}


def clean_passport(author):
    if not isinstance(author, dict) or set(author) != PASSPORT:
        raise ArenaError("Заповни паспорт автора.")
    if author["kind"] not in {"human", "model", "example"}:
        raise ArenaError("Невідомий тип автора.")
    clean_author = {"name": text_field(author["name"], 60), "kind": author["kind"],
                    "model": text_field(author["model"], 150, author["kind"] == "model"),
                    "submitted_at": text_field(author["submitted_at"], 60, False),
                    "prompt": text_field(author["prompt"], 8000, author["kind"] == "model"),
                    "edited_by": text_field(author["edited_by"], 300, False),
                    "attempts": number(author["attempts"], 1, 1000)}
    if int(clean_author["attempts"]) != clean_author["attempts"]:
        raise ArenaError("Кількість спроб має бути цілою.")
    if author["kind"] == "model" and not clean_author["submitted_at"]:
        raise ArenaError("Для стратегії моделі потрібна дата отримання відповіді.")
    return clean_author


def validate_v4(payload, version):
    """Schema 4 (SDK v3) for the workshop, the arena and the engines: BTC or ETH, taker or post-only. The payload is
    kept exactly as submitted, so its strategy_v4 hash (v4_recipe_sha256) is the one in the team evaluator's ledgers;
    recipe_sha256 is the engines' fingerprint of that payload, as for every recipe. Engines read the normalised rules
    of strategy_v4_runtime.engine_rules (one entry, no adds, no formal exit, max spread 2 bps)."""
    from .strategy_v4 import PAYLOAD_KEYS as V4_KEYS, ContractError, validate_payload
    from .strategy_v4_runtime import EXECUTED_INSTRUMENTS, engine_rules
    if not isinstance(payload, dict) or payload.get("confirmed") is not True:
        raise ArenaError("Підтвердь відповідність формальних правил оригінальному опису.")
    if clean_passport(payload.get("author")) != payload["author"]:
        raise ArenaError("Паспорт автора схеми 4 має бути вже чистим: без зайвих пробілів, кількість спроб ціла.")
    for key, limit in (("name", 60), ("description", 1000), ("original_text", 16000)):
        if text_field(payload.get(key), limit) != payload.get(key):
            raise ArenaError("Назва й описи схеми 4 мають бути вже чистими: без пробілів на краях, у межах довжини.")
    try:
        clean = validate_payload(payload)
    except ContractError as error:
        raise ArenaError("Схема 4: " + str(error)) from None
    if clean["instrument"] not in EXECUTED_INSTRUMENTS:
        raise ArenaError("Схема 4 у майстерні, на арені й на біржі — лише " + " / ".join(EXECUTED_INSTRUMENTS)
                         + "; інші інструменти оцінюються тільки tools/team_eval.py.")
    rules = engine_rules(clean["rules"])
    if rules["take_profit_bps"] <= 14 + rules["max_spread_bps"]:
        raise ArenaError("Ціль виходу має перевищувати модельні витрати кола.")
    record = {key: deepcopy(payload[key]) for key in V4_KEYS}
    record["rules"] = deepcopy(clean["rules"])
    strategy = {key: deepcopy(record[key]) for key in ("name", "author", "description", "original_text", "confirmed", "instrument")}
    strategy.update(strategy_schema_version=4, rules=rules, v4_payload=record, v4_recipe_sha256=clean["recipe_sha256"],
                    semantic_sha256=clean["semantic_sha256"], execution_model_id=clean["execution_model_id"],
                    catalogue=clean["catalogue"], id=uuid.uuid4().hex, version=version, status="ready",
                    recipe_sha256=fingerprint(record), created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return strategy


def validate_strategy(payload, version=1):
    if isinstance(payload, dict) and payload.get("strategy_schema_version") == 3:
        from .ikigai_g1 import validate_payload
        return validate_payload(payload, version)
    if isinstance(payload, dict) and payload.get("strategy_schema_version") == 4:
        return validate_v4(payload, version)
    keys = {"name", "author", "description", "original_text", "rules", "confirmed"}
    v2 = isinstance(payload, dict) and payload.get("strategy_schema_version") == 2
    if isinstance(payload, dict) and "strategy_schema_version" in payload and (type(payload["strategy_schema_version"]) is not int or payload["strategy_schema_version"] != 2):
        raise ArenaError("Непідтримувана версія формату стратегії.")
    if (not isinstance(payload, dict) or set(payload) not in (keys, keys | {"test_plan"}, keys | {"strategy_schema_version"})
            or payload["confirmed"] is not True):
        raise ArenaError("Підтвердь відповідність формальних правил оригінальному опису.")
    if "test_plan" in payload and payload["test_plan"] != PLAN:
        raise ArenaError("Невідомий технічний план.")
    author = payload["author"]
    clean_author = clean_passport(author)
    rules = payload["rules"]
    if v2:
        from .strategy_v2 import validate_rules, ContractError
        try:
            clean_rules = validate_rules(rules)
        except ContractError as error:
            raise ArenaError("Strategy V2: " + str(error)) from None
    elif not isinstance(rules, dict) or set(rules) != set(LIMITS) | {"entry", "exit", "side"}:
        raise ArenaError("Неповний або непідтримуваний рецепт.")
    if not v2 and rules["side"] not in {"long", "short"}:
        raise ArenaError("Вибери long або short.")
    if not v2:
        clean_rules = {key: number(rules[key], *bounds) for key, bounds in LIMITS.items()}
        for key in ("max_entries", "max_hold_seconds", "cooldown_seconds"):
            if int(clean_rules[key]) != clean_rules[key]:
                raise ArenaError(f"{key}: потрібне ціле число.")
    # Target must exceed the published round-trip fee + slippage + allowed spread model.
    if clean_rules["take_profit_bps"] <= 14 + clean_rules["max_spread_bps"]:
        raise ArenaError("Ціль виходу має перевищувати модельні витрати кола.")
    if not v2:
        clean_rules.update(side=rules["side"], entry=validate_group(rules["entry"], 1),
                           exit=validate_group(rules["exit"], 0))
    if is_probe(payload) and (clean_rules != PROBE_RULES or author["kind"] != "example"):
        raise ArenaError("Технічний Demo план має фіксовані правила та позначення example; для власної стратегії вилучи test_plan.")
    clean = {"name": text_field(payload["name"], 60), "author": clean_author,
             "description": text_field(payload["description"], 1000),
             "original_text": text_field(payload["original_text"], 16000), "rules": clean_rules,
             "confirmed": True}
    if is_probe(payload):
        clean["test_plan"] = PLAN
    if v2:
        clean["strategy_schema_version"] = 2
    clean.update(id=uuid.uuid4().hex, version=version, status="ready", recipe_sha256=fingerprint(clean),
                 created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return clean


def evaluate(group, features):
    checks = []
    for condition in group["conditions"]:
        value = features.get(condition["feature"])
        known = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        passed = OPS[condition["op"]](value, condition["value"]) if known else None
        checks.append({"feature": condition["feature"], "op": condition["op"],
                       "expected": condition["value"], "actual": value if known else None, "passed": passed})
    # UNKNOWN never becomes an entry through an 'any' branch.
    if not checks or any(c["passed"] is None for c in checks):
        return False, checks
    return (all if group["mode"] == "all" else any)(c["passed"] for c in checks), checks


def catalogue_defaults():
    base = {"name": "", "author": {"name": "Codex", "kind": "example", "model": "",
            "submitted_at": "2026-09-29", "prompt": "Навчальні формальні приклади для перевірки арени.",
            "attempts": 1, "edited_by": ""}, "description": "", "original_text": "", "confirmed": True,
            "rules": {"side": "long", "entry": {"mode": "all", "conditions": []},
                      "exit": {"mode": "any", "conditions": []}, "size_pct": 20, "max_entries": 1,
                      "add_after_bps": 30, "stop_loss_bps": 50, "take_profit_bps": 100,
                      "max_hold_seconds": 300, "cooldown_seconds": 60, "max_spread_bps": 5}}
    examples = [
        ("sample-trend", "За трендом", "long", [("sma_gap_bps", "gt", 0), ("return_5m_bps", "gt", 5)],
         [("sma_gap_bps", "lt", 0)], "Long, коли SMA5 вище SMA20, а зростання за 5 хвилин перевищує 5 bps; вихід при зворотному перетині."),
        ("sample-rebound", "Після відкату", "long", [("rsi_14", "lt", 35), ("return_1m_bps", "gt", 0)],
         [("rsi_14", "gt", 60)], "Long після перепроданості RSI14<35 і позитивної завершеної хвилини; вихід при RSI14>60."),
        ("sample-short", "Слабкий ринок", "short", [("sma_gap_bps", "lt", 0), ("return_5m_bps", "lt", -5)],
         [("sma_gap_bps", "gt", 0)], "Short, коли SMA5 нижче SMA20, а падіння за 5 хвилин перевищує 5 bps; вихід при зворотному перетині."),
    ]
    result = []
    for identity, name, side, entry, exit_rows, description in examples:
        item = deepcopy(base)
        item.update(name=name, description="Навчальний приклад · " + description,
                    original_text=description + " Один вхід на 20% стартового капіталу; stop 50 bps, target 100 bps; до 300 с, пауза 60 с, spread до 5 bps. Це рецепт Codex для тесту продукту, не перевірена прибуткова стратегія.")
        item["rules"].update(side=side)
        for stage, rows in (("entry", entry), ("exit", exit_rows)):
            item["rules"][stage]["conditions"] = [{"feature": f, "op": op, "value": v} for f, op, v in rows]
        item = validate_strategy(item)
        item.update(id=identity, builtin=True)
        result.append(item)
    for name, kind in (("Guest", "human"), ("Yurii", "human"), ("ChatGPT", "model"), ("Claude", "model"), ("Gemini", "model")):
        result.append({"id": "draft-" + name.lower(), "name": name,
                       "author": {"name": name, "kind": kind, "model": "", "submitted_at": "", "prompt": "", "attempts": 1, "edited_by": ""},
                       "version": 0, "status": "draft", "description": "Очікуємо оригінальну стратегію автора.",
                       "original_text": "", "rules": deepcopy(base["rules"]), "builtin": True})
    return result
