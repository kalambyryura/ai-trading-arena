"""Portable, constrained authoring contract. Text is evidence, never executable code."""
from copy import deepcopy
import json

from .arena_engine import RULES
from .arena_strategies import FEATURES, LIMITS, catalogue_defaults
from .cycle_probe import template as probe_template

PAYLOAD_KEYS = ("name", "author", "description", "original_text", "rules", "confirmed")
FEATURE_NOTES = {
    "sma_gap_bps": "(SMA5 / SMA20 - 1) × 10000, завершені хвилини",
    "return_1m_bps": "(останній close / попередній close - 1) × 10000",
    "return_5m_bps": "(останній close / close 5 хвилин тому - 1) × 10000",
    "rsi_14": "RSI зі сум gains/losses 14 змін; не Wilder EMA; flat = 50",
    "range_position_20": "Позиція close в high/low останніх 20 хвилин, 0…100; flat = 50",
    "volume_ratio_5": "Сума обсягів 5 завершених хвилин / попередні 5; нульовий знаменник = UNKNOWN",
    "spread_bps": "(ask - bid) / mid × 10000 на поточному котируванні",
}


def recipe_intact(recipe):
    """The recipe hash covers what the engines will run: the payload fingerprint, and for schema 4 also the engine
    rules rebuilt from that payload."""
    from .arena_strategies import fingerprint
    from .strategy_v4_runtime import enabled, rules_intact
    return fingerprint(payload_of(recipe)) == recipe["recipe_sha256"] and (not enabled(recipe) or rules_intact(recipe))


def payload_of(strategy):
    if strategy.get("strategy_schema_version") == 4:
        return deepcopy(strategy["v4_payload"])  # the author's schema-4 payload, exactly as validated
    payload = {key: deepcopy(strategy[key]) for key in PAYLOAD_KEYS}
    if "test_plan" in strategy:
        payload["test_plan"] = deepcopy(strategy["test_plan"])
    if "strategy_schema_version" in strategy:
        payload["strategy_schema_version"] = strategy["strategy_schema_version"]
    return payload


def v2_template():
    payload = payload_of(catalogue_defaults()[0])
    payload.update(name="Мій рецепт V2 · приклад", confirmed=False, strategy_schema_version=2)
    payload["rules"] = {**payload["rules"], "side": "both", "max_hold_seconds": None,
        "entry": {"long": {"setup": None, "confirm": {"feature": "return_5m_bps", "op": "gt", "value": 20}, "invalidate": None, "expiry_bars": None},
                  "short": {"setup": None, "confirm": {"feature": "return_5m_bps", "op": "lt", "value": -20}, "invalidate": None, "expiry_bars": None}},
        "exit": {"feature": "position_age_seconds", "op": "gte", "value": 900},
        "add_when": None, "cost_checkpoint": None, "reduce_when": None, "reduce_pct": None}
    payload["original_text"] = "Навчальний V2: обирає long при return5m>20bps або short при return5m<−20bps; без докупівлі та часткового виходу. Вихід за stop/target або авторським правилом віку900s. Час арени не є часом утримання. Це приклад, не перевірена прибуткова стратегія."
    return payload


def author_contract():
    template = payload_of(catalogue_defaults()[0])
    template["name"] = "Мій рецепт · приклад"
    template["confirmed"] = False
    template["author"] = {"name": "Вкажи автора", "kind": "human", "model": "",
                          "submitted_at": "", "prompt": "", "attempts": 1, "edited_by": ""}
    from .strategy_v2 import MARKET_FEATURES, POSITION_FEATURES
    notes = {**FEATURE_NOTES, "atr_14_bps": "Середній true range останніх14 завершених хвилин / close ×10000",
        "body_bps": "(close/open−1)×10000 останньої завершеної свічки",
        "upper_wick_bps": "(high−max(open,close))/close×10000", "lower_wick_bps": "(min(open,close)−low)/close×10000",
        "bollinger_z_20": "(close−mean20)/population_std20; flat=0", "ema_gap_bps": "(EMA5/EMA20−1)×10000; seed=firstclose у25–30свічках",
        "volume_ratio_1": "Обсяг останньої свічки / середній попередніх5; нуль→UNKNOWN",
        "utc_hour": "Година UTC 0…23 часу закриття останньої завершеної свічки (поточна хвилина); ознака свічки, дозволена в temporal setup/confirm",
        "utc_weekday": "День тижня UTC 0=понеділок…6=неділя для того самого часу; ознака свічки",
        "funding_rate_bps": "Поточна ставка funding із котирування ×10000 (bps за відповідний інтервал, не фіксовані 8 год); невідомий розклад→UNKNOWN. Ознака котирування: лише прямий вхід (setup=null), exit і позиційні правила, не temporal setup/confirm. У моделі зі свічок без історичного as-of прогнозу це UNKNOWN; остаточна ставка доступна лише для обліку після нарахування",
        "minutes_to_funding": "Хвилини до наступного fundingTime за часом сервера; невідомий розклад→UNKNOWN. Ознака котирування, як funding_rate_bps"}
    v2_prompt = """Ти — автор стратегії багатосторонньої Market Arena. Поверни один JSON за V2 template. Назва моделі є декларацією, не перевіреною ідентичністю. rules — повна виконувана логіка; original_text — її пояснення та слабкі місця.
strategy_schema_version=2; side=long|short|both. Кожна гілка entry має setup,confirm,invalidate,expiry_bars. setup=null означає одразу confirm; з setup підтвердження лише на наступних завершених1m свічках, доexpiry_bars1..60. setup та confirm не можуть виконатися на одній свічці. UNKNOWN блокує дію; одночасний long/short→HOLD. Після невизначеного ACK сигнал не повторюється.
Предикат — {all:[...]}, {any:[...]}, {not:...} або {feature,op,value} чи {feature,op,ref}; op gt/gte/lt/lte; ref — назва другої доступної ознаки. Глибина≤6, вузлів≤64. Entry використовує лише market_features. Exit/add_when/reduce_when/cost_checkpoint.when можуть також position_features. null вимикає предикат.
exit — повне закриття; reduce_when та reduce_pct5..95 — часткове скорочення фактичного обсягу, раз на завершену свічку, lot округлюється вниз. Невиконувана частка→HOLD, не збільшення size. Залишок зберігає напрямок, weighted basis, вік від першого fill і всі витрати. add_when — докупівля лише при прибутку і чинних ризикових межах. Пріоритет: Stop/risk/SL/TP/author hold/full exit > partial reduce > add. На одному акаунті одночасно одна net позиція, без long+short разом; інший напрямок лише після підтвердженого flat і cooldown.
cost_checkpoint=null або {after_seconds:1..86400,when:predicate}. Це одноразова перевірка на новій завершеній свічці після вказаного віку. entry_cost_hurdle_bps фіксується до першого ордера; net_gain_bps включає realized gross, signedfees/funding поточного циклу та оцінку закриття решти. JevUSD окремо. UNKNOWN не підмінюється нулем.
max_hold_seconds=null дозволяє позиції чекати своїх exit/SL/TP; або автор задає30..86400. Час матчу — лише вікно entry/add300/900/1800/3600/7200/18000/86400s. Після дедлайну partial/full exit працюють далі, новий вхід/add заборонені. Ліга триває без заданої кількості боїв. Ресурсний/paid ліміт блокує нові входи, не вигадує вихід позиції.
Ризик та execution визначає код: isolated1x, окремийsubaccount, allocation25..500/бот і≤1000разом, DD10%trigger, max_entries1..3, normaladd лише в прибутку, до20entryattempts/60orderintents, reserve для close. Size5..30%, spread.1..20bps, stop20..500bps,target40..1000bps і target>14+maxspread,cooldown15..600s. У звичайних OKX Demo/Live власник явно обирає execution_mode: shared_jev (default: Jev підтверджує допустимі entry/add; невідомий billing блокує нові paid) або rules_only (рішення лише за rules та ризиком; Jev не викликається). rules_only зберігає точні невідправлені bytes поточного Jev gate для окремого офлайн SHADOW; його відповіді ніколи не змінюють уже виконані угоди. Окремі SHADOW market-only questions також не торгують і не змінюють live gate. Технічний Demo E041 не допускає rules_only.
PAPER: fee5bps/side, slip2bps/side+spread, fundingмодельний. OKX Demo/Live: cumulative actualfills/fees/bills/cash reconciliation, reduce-only ownqty; hosted SL лише після точного GETreceipt. Готовність live підтверджується власником, synthetic≠market proof.
Усі спроби, помилки та переглянуті дані записуються; після feedback це developmentdata. Нові бої мають score_policy=filled_entry_net_v1: перемогти може лише стратегія з хоча б одним фактично виконаним входом; спроба/сигнал без fill не рахується. Найбільший звірений net USDT серед торгових учасників перемагає навіть у мінусі; Cash=0 лише еталон, не переможець. Без входів — без переможця. Старі бої зберігають свій score. Author changes→newimmutableversion; V1 не переписуються. Під час бою можна досліджувати наступну версію в окремій чернетці: free RULES replay або owner-started OKX Demo на окремому рахунку. Один Demo worker за раз, важкі дослідження під shared OS lease. Бойову версію не замінюють; нові дані потребують наступного спільного cutoff/feedback-вікна, не прихованого доступу одного автора.
CLI: py -3.12 tools/strategy_workshop.py contract|validate|test|lint|explain|lots|compare. Без credentials/provider/order в локальних RULES_ONLY тестах.
"""
    v2_prompt += "\nMARKET FEATURES:\n" + json.dumps(notes, ensure_ascii=False, indent=2)
    v2_prompt += "\nPOSITION FEATURES:\n" + json.dumps(sorted(POSITION_FEATURES))
    v2_prompt += "\nV2 TEMPLATE:\n" + json.dumps(v2_template(), ensure_ascii=False, indent=2)
    from .match_protocol import SCORE_POLICY
    return {"schema_version": 3, "match_protocol_version": 8, "time_policy": "admission_then_drain",
            "feature_semantics": "completed-close-wicks-v1;asof-funding-v2",
            "description_revision": "2026-10-09-wick-denominator-erratum",
            "score_policy": deepcopy(SCORE_POLICY),
            "duration_options": [300, 900, 1800, 3600, 7200, 18000, 86400],
            "prompt": v2_prompt, "template": v2_template(), "legacy_template": template,
            "features": notes, "position_features": sorted(POSITION_FEATURES), "limits": LIMITS, "rules": deepcopy(RULES),
            "runtime": "Free RULES_ONLY replay; owner-started ordinary OKX Demo/Live: shared_jev default or explicit rules_only + recorded offline Jev SHADOW; owner-started PAPER arena",
            "ordinary_exchange_execution_modes": ["shared_jev", "rules_only"],
            "rules_only_check": {"version": 1, "execution_mode": "rules_only", "uses_jev": False,
                                "contexts": ["workshop_demo", "arena_live"], "shadow": "offline_owner_paid_from_journal",
                                "label": "RULES_ONLY · Jev SHADOW"},
            "demo_probe_template": probe_template()}
