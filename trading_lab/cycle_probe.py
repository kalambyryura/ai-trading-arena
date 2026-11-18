"""Fixed Demo-only acceptance recipe. No credentials, transport or model calls."""
from copy import deepcopy

PLAN = "demo_cycle_v1"
TIMING = {"entry_window_seconds": 90, "hold_review_seconds": 30, "add_after_seconds": 60,
          "close_after_seconds": 120, "max_jev_requests": 6, "max_entry_intents": 2,
          "entry_notional_usdt": 10, "total_notional_usdt": 20,
          "budget_usdt": 100, "duration_seconds": 300}
RULES = {"side": "long", "entry": {"mode": "all", "conditions": [
    {"feature": "spread_bps", "op": "lte", "value": 5}]},
    "exit": {"mode": "any", "conditions": []}, "size_pct": 10, "max_entries": 2,
    "add_after_bps": 20, "stop_loss_bps": 50, "take_profit_bps": 100,
    "max_hold_seconds": 120, "cooldown_seconds": 60, "max_spread_bps": 5}


def is_probe(recipe):
    return recipe.get("test_plan") == PLAN


def template():
    return {"name": "Перевірка циклу · Demo", "test_plan": PLAN, "confirmed": True,
            "author": {"name": "Codex", "kind": "example", "model": "",
                       "submitted_at": "2026-09-30", "attempts": 1, "edited_by": "",
                       "prompt": "Технічна перевірка повного циклу; не прогноз прибутку."},
            "description": "Один технічний OKX Demo цикл: вхід → Jev/утримання → докупівля → закриття → звірка.",
            "original_text": "Тільки OKX Demo, бюджет 100 USDT, до 300 с, isolated 1x. "
            "Усі ринкові та рахункові перевірки обов'язкові; spread ≤5 bps. Вхід без прогнозу "
            "напрямку ринку, тільки після підтвердження Jev. Вікно першого входу 90 с. "
            "Через ≥30 с після входу одна Jev перевірка утримання без ордера. Через ≥60 с "
            "після входу — одна докупівля після Jev та повторних перевірок. Лише в цьому "
            "діагностичному плані add_after_bps замінено часом: докупівля можлива й у мінусі. "
            "До двох entry IOC intents, кожний ≤10 USDT, сума лімітних номіналів ≤20 USDT. "
            "Через ≥120 с від першого входу або раніше за stop50/target100bps/Stop — reduce-only "
            "закриття фактичного залишку та звірка bills/cash. Нових циклів немає. "
            "До6 оплачуваних спроб Jev з наявного сукупного $0.01 ліміту майстерні; HOLD, "
            "низька впевненість, невідома вартість чи збій не дозволяють купівлю. "
            "Гарантії виконання всіх етапів немає: звіт показує лише факти. "
            "Технічний рецепт не заморожується та не допускається на арену. "
            "Для звичайних стратегій докупівля тільки у прибутку залишається чинною.",
            "rules": deepcopy(RULES)}


def check_summary(candidate):
    """Explain known false conditions separately from UNKNOWN; 'any' stays a group."""
    checks = candidate["checks"]
    unknown = [c["feature"] for c in checks if c["passed"] is None]
    failed = [c["feature"] for c in checks if c["passed"] is False]
    if unknown:
        return "Бракує даних для перевірок: " + ", ".join(unknown) + ". Нових входів немає."
    if candidate["local_checks_passed"]:
        return "Формальні умови й обмеження пройдено; передаємо допустиму дію Jev."
    if failed:
        return "Сигнал/обмеження ще не збіглися: " + ", ".join(failed) + ". Дані для цих перевірок є."
    return "Вхід не дозволено поточним станом або умовою виходу; дані для перевірок є."


def lifecycle(state):
    """Compatible entry point for the read-only per-bot projection."""
    from .cycle_diagnostics import lifecycle as project
    return project(state)
