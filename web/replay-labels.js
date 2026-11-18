"use strict";
/* Presentation labels come only from signed observations in the causal packet. */
((root, factory) => {
  const api = factory(typeof module === "object" && module.exports ? require("./review-model.js") : root.ArenaReviewModel);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ArenaReplayLabels = api;
})(typeof window === "undefined" ? {} : window, Model => {
  const side = position => {
    const direction = Model.decimalCompare(position, "0");
    return direction === null ? "UNKNOWN" : direction > 0 ? "LONG" : direction < 0 ? "SHORT" : "FLAT";
  };
  const absolute = value => typeof value === "string" ? value.replace(/^-/, "") : null;
  const TYPES = {CHECK:"Перевірка", DECISION:"Рішення", INTENT:"Намір ордера", FILL:"Fill", POSITION:"Спостереження позиції",
    ACCOUNTING:"Грошове спостереження", QUALITY:"Якість даних", SETTLEMENT:"Звірка", PHASE:"Фаза",
    ADMISSION_CLOSED:"Вхід / add завершено · позиції тривають"};
  /* Short Ukrainian names for the recorder's event codes (the `emit(...)` vocabulary of the engines). Never guessed: an unknown code has
     no phrase and the pages keep showing the raw code. */
  const PHRASES = {
    season_started: "Сезон розпочато", season_finished: "Сезон завершено", season_done: "Сезон завершено, залишки звірено",
    admission_closed: "Нові входи завершено", stop_requested: "Запит на зупинку", recovery_started: "Відновлення: лише закриття залишку",
    entry_checks: "Перевірка умов входу", entry_rechecked: "Повторна перевірка перед входом", entry_cancelled: "Вхід скасовано",
    entry_no_fill: "Вхід не виконано: лот або ліквідність",
    entry_order_placed: "Post-only заявку на вхід виставлено", entry_order_expired: "Post-only заявку знято за тайм-аутом",
    entry_order_rejected: "Post-only заявку не виставлено", entry_order_cancelled: "Post-only заявку скасовано",
    entry_order_cancel_failed: "Скасування post-only заявки не підтверджено", entry_order_unwatched_cancel: "Без нагляду: post-only заявку скасовуємо",
    fill_read_lag: "Заповнення ще не видно в даних ордера", no_signal: "Немає сигналу: очікування", position_checks: "Перевірка відкритої позиції",
    fill_protected: "Заповнення покрите власним stop", fill_watch_failed: "Швидке читання заповнення не вдалося",
    protective_placed: "Власний stop стоїть на біржі", protective_amended: "Власний stop змінено", protective_terminal: "Власний stop завершено",
    protective_fired_during_check: "Власний stop спрацював під час перевірки", protective_fire_pending: "Чекаємо фактів власного stop",
    protective_fire_resolved: "Факти власного stop прочитано", protective_fire_expired: "Факти власного stop не з'явились: ручна звірка",
    settled_account_verified: "Звірку до паузи збережено: рахунок порожній", settled_account_unverified: "Звірку до паузи не підтверджено",
    exchange_order_posted: "POST заявки: відповідь біржі", exchange_cancel_posted: "POST скасування: відповідь біржі",
    exchange_order_rejected: "Біржа відхилила заявку: ордер не створено", exchange_order_absent: "Заявки на біржі немає: не створено",
    exchange_order_not_visible: "Біржа ще не показує заявку", feature_book: "Книга ознак схеми 4",
    exchange_order_reopened: "Заявка все ж існує: ведемо далі", exchange_order_fill_not_visible: "Виконання заявки ще не видно",
    exchange_order_found: "Скасування знайшло приховану заявку",
    execution_profile: "Режим виконання запуску", leverage_confirmed: "Плече й isolated підтверджено",
    entry_position_facts: "Факти позиції з плечем", liquidation_unknown: "Ціни ліквідації немає: входи заборонено",
    liquidation_too_close: "Ліквідація надто близько: входи заборонено", liquidation_closer_than_stop: "Ліквідація ближча за стоп: закриття",
    liquidation_detected: "Біржа примусово закрила позицію",
    protective_absent: "Біржа не показує власного stop", protective_not_created: "Stop не створено: новий або закриття", stop_renewed: "Stop повторено: закриття одразу",
    step_timing: "Тривалість кроків і запити OKX", exchange_order_read: "Прочитано стан заявки без заповнення",
    degraded_window_started: "Перевірки біржі не проходять", degraded_window_ended: "Вікно непройдених перевірок завершено",
    degraded_extension_ended: "Продовження під стопом на біржі завершено", protective_close_pending: "Стоп знято під час нашого закриття", entry_hold: "Входи притримано до повної перевірки",
    accounting_evidence: "Сирі bills і кінцевий баланс",
    paper_entry_fill: "Модельний вхід виконано", paper_close_fill: "Модельне закриття виконано",
    exchange_entry_fill: "Вхід підтверджено біржею", exchange_close_fill: "Закриття підтверджено біржею",
    exchange_order_intent: "Ордер зарезервовано до відправки", exchange_reserved: "Заявку зарезервовано", order_ack_uncertain: "Відповідь біржі невизначена",
    close_no_fill: "Закриття очікує ліквідності", exit_trigger: "Спрацювала умова виходу", rules_decision: "Рішення за правилами рецепта",
    rules_close: "Закриття за правилами рецепта", reduction_trigger: "Часткове скорочення за правилом", reduction_veto: "Скорочення пропущено",
    reduction_remainder_veto: "Залишок скорочення скасовано", protective_intent: "Захисний stop зарезервовано", protective_child_adopted: "Біржовий stop підтверджено",
    financial_observation: "Спостереження рахунку", accounting_confirmed: "Рахунок звірено", model_funding: "Модельний funding", exchange_funding: "Funding з біржових проводок",
    funding_schedule_restored: "Календар funding відновлено", demo_funding_unknown: "Funding невідомий",
    jev_request: "Запит Jev записано", jev_pending: "Очікуємо Jev", jev_gate: "Шлюз Jev", jev_decision: "Рішення Jev", jev_shadow_payload: "Запит записано для SHADOW",
    rules_only_mode: "Режим RULES_ONLY", rules_only_review: "Огляд RULES_ONLY", demo_execution_mode: "Режим перевірки Demo", demo_execution_review: "Огляд перевірки Demo",
    risk_limit: "Досягнуто ліміт ризику", risk_veto: "Недостатньо власного капіталу",
    data_gap: "Пропуск даних", clock_gap: "Розрив годинника", market_unavailable: "Ринок недоступний", exchange_error: "Помилка біржі", exchange_check_failed: "Перевірка біржі не пройшла",
    account_checked: "Акаунт і контракт перевірено", recording_blocked: "Запис котирувань недоступний", paused: "Пауза", closing_paused: "Закриття на паузі", report_pending: "Звіт очікує",
    setup_armed: "Сетап взято в роботу", setup_waiting: "Сетап очікує підтвердження", setup_waiting_confirmation: "Сетап очікує підтвердження",
    setup_confirmed: "Сетап підтверджено", setup_expired: "Сетап прострочено", setup_invalidated: "Сетап скасовано умовою", setup_gap_reset: "Сетап скинуто через пропуск даних",
    cost_checkpoint_checked: "Контрольна перевірка витрат", cost_checkpoint_unknown: "Контрольна перевірка витрат: невідомо"};
  const phrase = code => typeof code === "string" && Object.hasOwn(PHRASES, code) ? PHRASES[code] : null;
  /* An exchange order update the packet carries without a position (no counted fill: live, cancelled with 0 contracts,
     or a fill the causal trace could not count) is an order state, never "entry confirmed". */
  const NO_FILL = {exchange_entry_fill: "Стан заявки на вхід · fill не враховано", exchange_close_fill: "Стан заявки на закриття · fill не враховано"};
  const counted = event => Object.hasOwn(event.financial || {}, "position_btc");
  const phraseOf = event => Object.hasOwn(NO_FILL, event.code) && !counted(event) ? NO_FILL[event.code] : phrase(event.code);
  function describe(event, before = null) {
    const after = event.financial?.position_btc, beforeSide = side(before), afterSide = side(after);
    let action = TYPES[event.type] || event.type;
    if (event.type === "FILL" && Object.hasOwn(NO_FILL, event.code) && !counted(event)) action = "Оновлення заявки";
    if (event.type === "FILL" && Object.hasOwn(event.financial || {}, "position_btc")) {
      if (afterSide === "UNKNOWN") action = "Fill · обсяг невідомий";
      else if (beforeSide === "UNKNOWN") action = "Fill · " + afterSide + " · попередній обсяг невідомий";
      else if (beforeSide === "FLAT" && afterSide !== "FLAT") action = "Вхід " + afterSide;
      else if (afterSide === "FLAT" && beforeSide !== "FLAT") action = "Повне закриття " + beforeSide + " · FLAT";
      else if (beforeSide !== afterSide) action = "Reversal " + beforeSide + " → " + afterSide;
      else if (afterSide !== "FLAT") {
        const change = Model.decimalCompare(absolute(after), absolute(before));
        action = change < 0 ? "Часткове скорочення " + afterSide : change > 0 ? "Докупівля " + afterSide : "Fill · обсяг " + afterSide + " незмінний";
      }
    } else if (event.type === "POSITION") action += " · " + afterSide;
    // `detail` accompanies a type-level action ("Перевірка", "Рішення"); fills and position observations already say what happened.
    const said = event.type === "POSITION" || (event.type === "FILL" && Object.hasOwn(event.financial || {}, "position_btc"));
    return {seq: event.seq, type: event.type, action, code: event.code, before, after,
      side: afterSide, recorded_transition: event.type === "FILL" && beforeSide !== "UNKNOWN" && afterSide !== "UNKNOWN",
      phrase: phraseOf(event), detail: said ? null : phraseOf(event)};
  }
  function index(packet) {
    const positions = new Map(packet.manifest.participants.map(p => [p.id, p.avatar_kind === "baseline" ? "0" : null]));
    const result = new Map();
    for (const event of packet.events) {
      result.set(event.seq, describe(event, positions.get(event.bot_id) ?? null));
      if (event.bot_id && Object.hasOwn(event.financial || {}, "position_btc")) positions.set(event.bot_id, event.financial.position_btc);
    }
    return result;
  }
  /* Key moments of a long battle (B-03b): where something happened that a viewer would want to jump to.
     Built only from the recorded fills (with the position before and after), the admission cut-off, data-quality gaps and
     the final settlement. A moment is a pointer into the packet, never a new claim. */
  const KINDS = {entry: 3, close: 3, reversal: 3, add: 2, reduce: 2, admission_closed: 2, quality: 2, fill: 1, settlement: 1, finished: 1};
  // The recorder marks only gaps (reason = the code of what failed); COMPLETE/UNKNOWN are allowed by the packet format.
  const GAP_REASONS = {data_gap: "немає даних", clock_gap: "розрив годинника", market_unavailable: "ринок недоступний", exchange_error: "помилка біржі", paused: "пауза"};
  function qualityText(quality) {
    const base = quality?.coverage === "GAP" ? "пропуск даних" : quality?.coverage === "COMPLETE" ? "дані повні" : "стан даних невідомий";
    return "Якість запису: " + base + (GAP_REASONS[quality?.reason] ? " · " + GAP_REASONS[quality.reason] : "") + (quality?.instrument ? " · " + quality.instrument : "");
  }
  function kindOfFill(action) {
    if (action.startsWith("Вхід")) return "entry";
    if (action.startsWith("Повне закриття")) return "close";
    if (action.startsWith("Reversal")) return "reversal";
    if (action.startsWith("Докупівля")) return "add";
    if (action.startsWith("Часткове скорочення")) return "reduce";
    return "fill";
  }
  function moments(packet) {
    const labels = index(packet), names = new Map(packet.manifest.participants.map(p => [p.id, p.name]));
    const out = [], settled = new Set();
    let firstSettlement = null;
    for (const event of packet.events) {
      let kind = null, text = null;
      if (event.type === "FILL") { const d = labels.get(event.seq); kind = kindOfFill(d.action); text = d.action; }
      else if (event.type === "ADMISSION_CLOSED") { kind = "admission_closed"; text = "Час нових входів завершено · відкриті позиції тривають"; }
      else if (event.type === "QUALITY") { kind = "quality"; text = qualityText(event.quality); }
      else if (event.type === "SETTLEMENT") { if (firstSettlement === null) firstSettlement = event; if (event.bot_id) settled.add(event.bot_id); continue; }   // one moment for all of them, below
      else if (event.type === "PHASE" && event.phase === "DONE") { kind = "finished"; text = "Запис завершено"; }
      if (!kind) continue;
      const name = event.bot_id ? names.get(event.bot_id) || event.bot_id : null;
      out.push({seq: event.seq, at_ms: event.available_at_ms, bot_id: event.bot_id || null, kind, importance: KINDS[kind],
        label: name ? name + ": " + text : text});
    }
    if (firstSettlement !== null) {
      const total = packet.manifest.participants.length;
      out.push({seq: firstSettlement.seq, at_ms: firstSettlement.available_at_ms, bot_id: null, kind: "settlement", importance: KINDS.settlement,
        label: "Звірка рахунків: завершено " + settled.size + " з " + total + " учасників"});
      out.sort((a, b) => a.seq - b.seq);
    }
    return out;
  }
  const visible = (moment, botId) => !botId || !moment.bot_id || moment.bot_id === botId;
  // The first moment after the current event (or the last one before it); null at the ends. `seq` is the number of events applied.
  function nextMoment(list, seq, botId = null) { return list.find(m => m.seq > seq && visible(m, botId)) || null; }
  function previousMoment(list, seq, botId = null) { return [...list].reverse().find(m => m.seq < seq && visible(m, botId)) || null; }
  /* "Summary in 60 seconds": the moments to step through and how long to stay on each. When there are more than fit, the most
     important are kept (in time order); the dwell time is clamped so a step stays readable. */
  function digestPlan(list, options = {}) {
    const seconds = options.seconds || 60, minDwell = options.minDwellSeconds || 1.2, maxDwell = options.maxDwellSeconds || 5;
    const capacity = Math.max(1, Math.floor(seconds / minDwell));
    let steps = list;
    if (steps.length > capacity) {
      const bySignificance = steps.map((m, i) => ({m, i})).sort((a, b) => b.m.importance - a.m.importance || a.i - b.i).slice(0, capacity);
      steps = bySignificance.sort((a, b) => a.i - b.i).map(x => x.m);
    }
    const dwell = steps.length ? Math.min(maxDwell, Math.max(minDwell, seconds / steps.length)) : 0;
    return {steps, dwell_ms: Math.round(dwell * 1000), shown: steps.length, total: list.length};
  }
  const summary = label => label.action + (label.detail ? " · " + label.detail : "");
  return {side, describe, index, moments, nextMoment, previousMoment, digestPlan, MOMENT_KINDS: KINDS, PHRASES, phrase, summary};
});
