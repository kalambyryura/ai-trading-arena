"""Fixed public failure codes; never serialize exception bodies or credentials."""
import re

from .okx_readonly import PreflightError


# Ownership/contract evidence must still trigger the existing stop + manual review.
# Do not classify by substrings: clock_difference_exceeds_five_minutes is not a
# position exceeding its owned fill (the target-PC incident on 2026-10-27).
MANUAL_REVIEW_REASONS = frozenset({
    "profile_environment_mismatch", "fee_exceeds_experiment_bound",
    "foreign_algo_order", "foreign_or_unbounded_pending_orders", "foreign_or_unsupported_position",
    "multiple_positions", "order_result_identity_mismatch", "order_result_quantity_or_reduce_only_mismatch",
    "pending_algo_parent_identity_mismatch", "pending_order_metadata_mismatch", "pending_order_server_identity_mismatch",
    "protective_child_identity_mismatch", "protective_child_mismatch", "protective_receipt_mismatch",
    "public_account_contract_mismatch", "bill_window_or_currency_mismatch", "close_exceeds_owned_fill",
    "foreign_account_activity_in_bills", "order_facts_regressed", "account_identity_changed",
    "actual_position_differs_from_owned_fills", "recovery_account_changed", "recovery_contract_changed",
    "protection_owner_state_changed", "protection_position_changed_during_cleanup", "protection_position_or_owner_changed",
    "protection_record_changed", "protection_record_changed_during_io", "protective_child_state_changed_during_io",
    "account_instrument_differs_from_public", "instrument_unavailable_or_mismatched", "ticker_instrument_mismatch",
})

MESSAGES = {
    "stop_fire_pending": "Власний stop спрацював; чекаємо його фактів від OKX.",  # R2.1.14
    "stop_fire_recovery": "Власний stop спрацював, позиція OKX ще відстає: зупиняємо лише цього бота.",  # R2.1.14 (D169)
    "clock_difference_exceeds_five_minutes": "Годинник комп’ютера відрізняється від OKX більш ніж на 5 хвилин. У Windows перевір дату, часовий пояс і натисни «Синхронізувати зараз».",
    "unreliable_clock_sample": "Перевірка часу ненадійна: годинник змінився або відповідь затрималася. Перевір синхронізацію Windows і зв’язок.",
    "server_clock_calibration_expired": "Перевірка часу OKX застаріла; потрібна нова перевірка доступу.",
    "server_clock_not_calibrated": "Час OKX ще не перевірено.",
    "network_error": "OKX не відповів у межах таймауту. Перевір зв’язок із біржею.",
    "trade_permission_missing": "Ключ субакаунта не має дозволу Trade.",
    "read_permission_missing_or_unknown_scopes": "Дозволи API-ключа не підтверджені; перевір Read і Trade.",
    "set_futures_and_net_mode_in_okx": "Для субакаунта потрібні режим ф’ючерсів і односторонній режим позицій.",
    "live_bot_requires_dedicated_subaccount": "Потрібен окремий субакаунт для цієї стратегії.",
    "two_keys_are_one_account": "Два ключі належать одному акаунту; учасникам потрібні окремі субакаунти.",
    "live_accounts_must_share_site_and_parent": "Субакаунти мають належати одному головному акаунту й регіону OKX.",
    "account_must_start_flat": "Перед новим боєм потрібно звірити наявні позиції та ордери цього субакаунта.",
    "insufficient_available_usdt": "На торговому балансі субакаунта недостатньо доступних USDT для цього бюджету.",
    "missing_okx_credentials": "Для профілю не заповнено API-ключ.",
    # R2.1.10 (D-1): Live needs Read+Trade without Withdraw; never refused on a Resume or a recovery
    "live_key_has_unused_permissions": "Ключ Live має зайве право Withdraw. Створи для субакаунта ключ лише Read+Trade, без Withdraw, і перевір доступ знову.",
    "rolling_read_rate_limit": "Досягнуто короткочасного ліміту перевірок OKX; нових входів немає.",
    "exchange_request_limit": "Досягнуто ліміту перевірок OKX для цього запуску.",
    "exchange_check_failed": "Перевірка OKX не завершена; нових входів немає.",
    "exchange_process_failed": "Процес не завершено через технічну помилку. Потрібна діагностика збереженого журналу.",
    "invalid_server_time": "OKX повернув некоректний час; нові входи заблоковано.",
    "session_clock_source_disagreement": "Час OKX не узгоджується з таймером поточного процесу; нових входів немає до надійної перевірки.",
    "session_monotonic_regression": "Монотонний таймер процесу некоректний; потрібне відновлення зі звіркою.",
    # R2.1.6: the season's order-intent limits (refused or never created orders do not count)
    "order_intent_limit": "Ліміт заявок бота на сезон вичерпано; нових заявок немає.",
    "close_intent_reserve": "Решта заявок сезону зарезервована для закриття; нових входів немає.",
    "discretionary_reduction_close_reserve": "Решта заявок сезону зарезервована для повного закриття; часткове скорочення не надіслано.",
}
KNOWN_REASONS = MANUAL_REVIEW_REASONS | MESSAGES.keys() | {
    "invalid_json_response", "invalid_provider_envelope", "invalid_provider_data", "response_too_large",
    "stale_or_future_quote", "one_x_leverage_unconfirmed", "invalid_balance_response", "invalid_positions",
    "leverage_unconfirmed", "exchange_leverage_not_the_bots", "invalid_execution_leverage",  # R2.1.15
    "stop_loss_notional_veto", "invalid_position_tiers",  # R2.1.15 (D181)
    "market_gap_or_clock_regression", "market_arrival_stale", "snapshot_journal_limit", "snapshot_journal_write_failed",
}
STAGES = frozenset({"preflight", "execution_setup", "market_setup", "running", "account_check"})

# R2.1.10 (D136 p. 3, A2): the OKX failures that mean "try again later". Only these code + HTTP status pairs are
# transient; every other code is classified as before: 51149 and 51412, ADL and forced liquidation (50017-50022) and
# any unknown code. "methods": where the pair is transient (50004 and 51054 on a POST stay UNKNOWN, as before);
# "extension": whether a run of them may keep a protected position open (A3). 51290 has none: the owned stop lives in
# OKX's algo engine, the one being upgraded. Texts verbatim from the official table.
OKX_ERROR_SOURCE = "https://www.okx.com/docs-v5/en/ (Error Code, REST API)"
OKX_ERROR_CHECKED_AT = "2026-11-04 12:30 UTC"
TRANSIENT_CODES = {
    ("50001", 503): {"methods": {"GET", "POST"}, "extension": True,
                     "text": "Service temporarily unavailable. Please try again later."},
    ("50004", 400): {"methods": {"GET"}, "extension": True,
                     "text": "API endpoint request timeout. (does not mean that the request was successful or failed, "
                             "please check the request result)."},
    ("50011", 200): {"methods": {"GET", "POST"}, "extension": True,
                     "text": "Rate limit reached. Please refer to API documentation and throttle requests accordingly."},
    ("50011", 429): {"methods": {"GET", "POST"}, "extension": True, "text": "Too Many Requests"},
    ("50013", 429): {"methods": {"GET", "POST"}, "extension": True, "text": "Systems are busy. Please try again later."},
    ("50026", 500): {"methods": {"GET", "POST"}, "extension": True, "text": "System error. Try again later"},
    ("50040", 200): {"methods": {"GET", "POST"}, "extension": True, "text": "Too frequent operations, please try again later"},
    ("51054", 500): {"methods": {"GET"}, "extension": True, "text": "Request timed out. Please try again."},
    ("51290", 200): {"methods": {"GET", "POST"}, "extension": False,
                     "text": "The Bot engine is being upgraded. Please try again later"},
}
ENDPOINT = re.compile(r"/api/v5/[a-z0-9/-]{1,60}")


def transient(error):
    """The table entry of a failure, when its OKX code, HTTP status and method are a listed transient pair; else None."""
    entry = TRANSIENT_CODES.get((getattr(error, "okx_code", None), getattr(error, "http_status", None)))
    return entry if entry and getattr(error, "method", None) in entry["methods"] else None


def venue_facts(error):
    """R2.1.10 (A1): where a request failed and what the venue said, as journal fields; never a URL, msg or body."""
    facts = {}
    endpoint, method = getattr(error, "endpoint", None), getattr(error, "method", None)
    status, code = getattr(error, "http_status", None), getattr(error, "okx_code", None)
    if isinstance(endpoint, str) and ENDPOINT.fullmatch(endpoint):
        facts["endpoint"] = endpoint
    if method in {"GET", "POST"}:
        facts["method"] = method
    if type(status) is int and 100 <= status <= 599:
        facts["http_status"] = status
    if isinstance(code, str) and re.fullmatch(r"[0-9]{1,8}", code):
        facts["okx_code"] = code
    if facts:
        facts["transient"] = transient(error) is not None
    return facts
CLOCK_REASONS = frozenset({"wall_clock_jump", "observation_gap", "quote_regression"})


def public_reason(value):
    if not isinstance(value, str):
        return None
    if value in KNOWN_REASONS or re.fullmatch(r"(?:http_[0-9]{3}|okx_error_[0-9]{1,8})", value):
        return value
    return None


def failure(error, *, stage="running", fallback="exchange_process_failed"):
    reason = (public_reason(str(error)) if isinstance(error, PreflightError) else None) or fallback
    message = MESSAGES.get(reason)
    if message is None:
        message = ("Неузгоджені дані акаунта або власних ордерів; потрібна ручна звірка."
                   if reason in MANUAL_REVIEW_REASONS else "Перевірку OKX не пройдено.") + " Код: " + reason + "."
    return {"reason": reason, "stage": stage if stage in STAGES else "running", "message": message}
