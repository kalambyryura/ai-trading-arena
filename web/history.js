"use strict";
let selectedHistory = null, historyData = null, historyBusy = false, historyRenderKey = "";
Object.assign(reasons, {entry_time_limit:"Час очікування вичерпано", jev_skip:"Jev вирішив зачекати", trend_filter:"Фільтр тренду не дозволяє вхід", valid_market_choice:"Відповідь Jev валідна", add_price_or_trend:"Ціна або тренд не дозволяють докупівлю", size_limit:"Ліміт обсягу", add_price_trend_or_count:"Умови докупівлі не виконані", series_budget_or_billing:"Ліміт серії або невідомі витрати", cycle_budget_or_cooldown:"Ліміт запитів або інтервал Jev", jev_budget_exhausted:"Бюджет Jev вичерпано"});
const eventNames = {
  cycle_started:"Цикл розпочато", phase:"Перехід між кроками", clock:"Синхронізація часу",
  account_mode_permissions:"Режим акаунта та дозволи", no_position:"Відсутність відкритої позиції",
  no_pending_orders:"Відсутність сторонніх заявок", contract:"Параметри контракту", available_funds:"Достатність коштів",
  fee_rate:"Перевірка комісії", entry_size_quote:"Розмір заявки та котирування", market_snapshot:"Актуальність свічок, спреду та funding",
  jev_request:"Підготовлено дані для Jev", jev_decision:"Рішення Jev", entry_deferred:"Вхід відкладено → крок 1",
  wait_before_checks:"Очікування наступної перевірки", entry_accepted:"Вхід дозволено → купівля", leverage:"Перевірка плеча 1×",
  buy_or_add:"Перевірка й виконання купівлі", entry_checks:"Позиція, кошти, плече, ціна та ліміти",
  order_send:"Заявку записано; початок відправки", order_ack:"Відповідь на відправку отримано",
  order_ack_unknown:"Відповідь не підтверджена → звірка", order_reconcile:"Звірка заявки з біржею",
  order_result:"Фактичний результат заявки", cancel_unresolved_order:"Скасування невизначеної заявки цього циклу",
  position_risk:"Перевірка відкритої позиції", position_sample:"Ціна, обсяг і оцінка PnL",
  hold_until_review:"Утримання → наступний аналіз", add_not_allowed:"Докупівлю заблоковано",
  stop_observed:"Запит Stop отримано", close_requested:"Перехід до закриття", close_reconcile:"Звірка та закриття",
  recovery:"Відновлення за записаними заявками", flat_no_pending:"Нульова позиція й відсутність заявок підтверджені",
  cycle_summary:"Підсумок циклу", cycle_error:"Помилка виконання", entry_wait_finished:"Очікування завершено",
  legacy_decision:"Збережене рішення Jev", legacy_order:"Збережена заявка", legacy_result:"Збережений підсумок"
};
const fieldNames = {
  choice:"Рішення", reason:"Причина / код", phase:"Стан", direction:"Напрям", order_state:"Стан заявки",
  baseline_candidate:"Фільтр тренду", may_add:"Докупівля дозволена", cost_usd:"Jev, USD", elapsed_ms:"Тривалість, мс",
  confidence:"Confidence Jev (не ймовірність прибутку)", return_1m_bps:"Рух за 1 хв, bps", return_5m_bps:"Рух за 5 хв, bps",
  return_15m_bps:"Рух за 15 хв, bps", sma5_usdt:"SMA5, USDT", sma20_usdt:"SMA20, USDT", bid_usdt:"Bid, USDT",
  ask_usdt:"Ask, USDT", spread_bps:"Спред, bps", funding_rate:"Ставка funding", seconds_to_funding:"До funding, с",
  contracts:"Заявлено контрактів", filled_contracts:"Виконано контрактів", average_price_usdt:"Ціна виконання, USDT",
  limit_price_usdt:"Гранична ціна, USDT", fee_usdt:"Комісія зі знаком, USDT", reduce_only:"Тільки зменшення позиції",
  remaining_contracts:"Залишок контрактів", held_seconds:"У позиції, с", return_bps:"Рух від входу, bps",
  estimated_close_pnl_usdt:"Оцінка PnL, USDT", pnl_usdt:"PnL без funding/Jev, USDT", signed_fees_usdt:"Комісії зі знаком, USDT",
  next_review_seconds:"До повторної перевірки, с", cycle_seconds:"Ліміт усього циклу, с", review_seconds:"Інтервал Jev, с",
  check_seconds:"Інтервал перевірок позиції, с", max_decisions:"Ліміт Jev-запитів", max_entries:"Ліміт покупок",
  max_notional_usdt:"Ліміт номіналу, USDT", leverage:"Плече", assumed_roundtrip_fee_bps:"Припущення комісій, bps",
  slippage_allowance_bps:"Запас прослизання, bps", closed_bars:"Завершених свічок", bar_seconds:"Тривалість свічки, с",
  last_close_usdt:"Останнє закриття свічки, USDT", mean_range_14m_bps:"Середній діапазон 14 хв, bps",
  volume_last5_usdt:"Обсяг останніх 5 хв, USDT", volume_previous5_usdt:"Обсяг попередніх 5 хв, USDT",
  position_return_bps:"Рух від входу, bps", entries_used:"Використано покупок", adding:"Докупівля", attempted:"Запит Jev виконано",
  maximum_entry_notional_usdt:"Максимальний номінал входу, USDT", server_ms:"Час біржі", observed_server_ms:"Час ринкового знімка",
  last_bar_close_server_ms:"Час закриття свічки", remaining_cycle_seconds:"Залишок часу всього циклу, с"
};
const eventStatuses = {started:"Початок", passed:"Готово", finished:"Завершено", failed:"Помилка", warning:"Увага", recorded:"Зі старого журналу"};
function eventValue(key, item) {
  if (typeof item === "boolean") return item ? "Так" : "Ні";
  if (key.endsWith("server_ms") || key === "server_ms") return time(Number(item));
  if (key === "choice") return choices[item] || item;
  if (key === "reason") return reasons[item] || item;
  if (key === "direction") return item === "buy" ? "Купівля" : "Продаж / закриття";
  return value(item) === null ? item : fmt(item, 10);
}
async function openHistory(id, title) {
  selectedHistory = id; historyData = null; historyRenderKey = "";
  $("history-title").textContent = title;
  $("history-summary").textContent = "Читаємо журнал…";
  $("history-note").textContent = ""; $("history-events").replaceChildren(); $("history-error").hidden = true;
  $("history-step").value = "all"; $("history-event-count").textContent = "";
  if (!$("history-dialog").open) $("history-dialog").showModal();
  await refreshHistory();
}
async function refreshHistory() {
  if (!selectedHistory || !$("history-dialog").open || historyBusy) return;
  const id = selectedHistory; historyBusy = true;
  try {
    if (snapshot?.api_version !== 3) throw new Error("Для журналу потрібне оновлення сервера через ярлик Update Jev Trading Lab.");
    const data = await request("/api/history/" + encodeURIComponent(id));
    if (selectedHistory !== id || !$("history-dialog").open) return;
    historyData = data; $("history-error").hidden = true; renderTimeline();
  } catch (error) {
    if (selectedHistory === id) { $("history-error").textContent = error.message; $("history-error").hidden = false; }
  } finally { historyBusy = false; }
}
function renderTimeline() {
  if (!historyData) return;
  const data = historyData, filter = $("history-step").value;
  const signature = JSON.stringify([data, filter]);
  if (signature === historyRenderKey) return;
  historyRenderKey = signature;
  const c = data.cycle;
  $("history-summary").textContent = c.phase + " · Jev: " + (c.usage?.attempts ?? 0) + " запитів / $" + fmt(c.usage?.cost_usd, 10) + " · PnL: " + fmt(c.pnl_usdt) + " USDT";
  $("history-note").textContent = data.mode === "pending" ? "Процес ще не записав першу подію. Журнал оновлюється автоматично." : data.mode === "legacy_partial"
    ? "Старий цикл: показуємо лише збережені рішення й результати. Детальна історія перевірок тоді не записувалась; порядок різних груп подій не відновлюємо. Час рішення — час ринкового знімка."
    : "Події записані під час виконання. Час подій — за комп’ютером; час ринку — за OKX. Звичайний skip повертає до перевірок. Вартість Jev за даними API; PnL без funding і Jev.";
  const list = $("history-events"), open = new Set([...list.querySelectorAll("details[open]")].map(node=>node.dataset.seq));
  list.replaceChildren();
  const events = data.events.filter(event=>filter === "all" || event.step === Number(filter));
  $("history-event-count").textContent = events.length + " / " + data.events.length + " подій";
  for (const event of events) {
    const li = document.createElement("li"), head = document.createElement("div"), step = document.createElement("span"), label = document.createElement("strong"), stamp = document.createElement("time"), status = document.createElement("span");
    head.className = "event-head"; step.className = "event-step"; step.textContent = event.step;
    label.textContent = eventNames[event.code] || event.code || "Подія";
    stamp.textContent = event.at ? time(event.at) : "Час не записаний";
    status.className = "event-status " + (["failed","warning"].includes(event.status) ? event.status : "");
    status.textContent = eventStatuses[event.status] || event.status;
    head.append(step,label,stamp,status); li.append(head);
    const fields = Object.entries(event.data || {});
    if (fields.length) {
      const extra = document.createElement("details"), summary = document.createElement("summary"), grid = document.createElement("dl");
      extra.dataset.seq = String(event.seq); extra.open = open.has(String(event.seq));
      summary.textContent = event.data.choice ? (choices[event.data.choice] || event.data.choice) + " · показники та результат"
        : event.data.reason ? (reasons[event.data.reason] || event.data.reason) + " · деталі" : "Показники та результат";
      for (const [key,item] of fields) {
        const term = document.createElement("dt"), val = document.createElement("dd");
        term.textContent = fieldNames[key] || key; val.textContent = eventValue(key,item); grid.append(term,val);
      }
      extra.append(summary,grid); li.append(extra);
    }
    list.append(li);
  }
  if (!events.length) { const empty = document.createElement("li"); empty.textContent = "Для цього кроку подій ще немає."; list.append(empty); }
}
$("history-close").addEventListener("click", () => $("history-dialog").close());
$("history-dialog").addEventListener("close", () => { selectedHistory = null; historyData = null; });
$("history-step").addEventListener("change", renderTimeline);
