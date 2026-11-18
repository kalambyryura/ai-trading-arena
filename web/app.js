"use strict";
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="panel-token"]').content;
let snapshot = null, posting = false, stopping = false, notice = "";
let historySignature = "";
const version = cycle => cycle?.bot_id === "jev-position-v3" ? "V3" : cycle?.bot_id === "jev-position-v2" ? "V2" : "V1";
const value = x => x === null || x === undefined || x === "" || !Number.isFinite(Number(x)) ? null : Number(x);
const fmt = (x, digits = 8) => value(x) === null ? "—" : new Intl.NumberFormat("uk-UA", {maximumFractionDigits: digits}).format(Number(x));
const plural = (count, forms) => forms[new Intl.PluralRules("uk-UA").select(count)] || forms.many;
const time = iso => {
  if (!iso || !Number.isFinite(new Date(iso).getTime())) return "—";
  return new Intl.DateTimeFormat("uk-UA", {day:"2-digit", month:"2-digit", hour:"2-digit", minute:"2-digit", second:"2-digit"}).format(new Date(iso));
};
const phases = {
  CHECKING: ["Перевіряємо готовність", "Перевірка акаунта, контракту й відсутності інших заявок.", 0],
  REVIEWING: ["Jev оцінює вхід", "Тренд, спред і витрати. Рішення: купити або пропустити.", 1],
  WAITING_ENTRY: ["Очікуємо сигнал для входу", "Покупки ще немає. Приблизно через 30 с: нові перевірки → Jev. Стоп завершує очікування.", 0],
  ENTERING: ["Купівля мінімального обсягу", "Очікуємо підтвердження фактичного виконання.", 2],
  MONITORING: ["Перевіряємо позицію", "Фактичний обсяг, ціна, ліміти ризику та час утримання.", 3],
  HOLDING: ["Утримуємо позицію", "Перевірки кожні 5 с. Наступне рішення Jev — приблизно через 30 с.", 3],
  POSITION_REVIEW: ["Jev оцінює продовження", "Утримати, докупити або закрити. Усі покупки мають спільний ліміт часу.", 4],
  CLOSING: ["Закриваємо всю позицію", "Звіряємо всі покупки та закриваємо їхній фактичний обсяг через reduce-only.", 5],
  DONE: ["Цикл завершено", "Купівлю й закриття підтверджено. Бот зупинився.", 6],
  SKIPPED: ["Тест пропущено", "Jev або перевірка його відповіді не дозволили вхід. Покупки не було.", -1],
  NO_FILL: ["Заявка не виконалась", "Обмежена заявка не отримала виконання. Повторної купівлі немає.", -1],
  STOPPED_BEFORE_ENTRY: ["Зупинено до входу", "Цикл зупинений. Торговий вхід не надсилався.", -1],
  NEEDS_REVIEW: ["Потрібна перевірка", "Стан журналу потребує звірки перед подальшими діями.", -1]
};
const choices = {buy:"Купити", skip:"Пропустити", hold:"Утримувати", add:"Докупити", close:"Закрити", run_test:"Технічний тест V1"};
const reasons = {user_stop:"Зупинено користувачем", time_limit:"Ліміт часу", stop_loss:"Ціновий стоп", take_profit:"Ціль ціни", jev_close:"Закриття за рішенням Jev", jev_unavailable:"Jev недоступний або ліміт запитів", check_failed:"Помилка перевірки → закриття", no_entry_signal:"Немає сигналу для входу", restart_recovery:"Відновлення після зупинки", funding_near:"Наближається funding", request_reserve:"Резерв запитів для закриття", decision_limit:"Ліміт рішень", spread_limit:"Завеликий спред", no_fill:"Немає виконання заявки"};

async function request(path, body) {
  const options = {cache: "no-store", credentials: "same-origin"};
  if (body !== undefined) Object.assign(options, {method: "POST", headers: {"Content-Type": "application/json", "X-Panel-Token": token}, body: JSON.stringify(body)});
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Локальна панель недоступна.");
  return data;
}

function showError(text) {
  $("error").textContent = text || "";
  $("error").hidden = !text;
}

function render(state) {
  snapshot = state;
  const cycle = state.current;
  const compatible = state.api_version === 3;
  const phase = cycle?.phase || "READY";
  const legacyFinished = cycle && cycle.bot_id !== "jev-position-v3" && !state.running && state.action !== "resume";
  const meta = legacyFinished ? ["Готовий до циклу V3", "Новий запуск чекатиме на сигнал і повторюватиме перевірки та Jev. Попередній результат збережено в історії.", -1] : phases[phase] || ["Готовий до першого тесту", "Натисни ▶, щоб запустити один цикл із реальними коштами.", -1];
  $("connection").classList.remove("offline");
  $("connection").lastChild.textContent = "Локальне підключення";
  $("phase-tag").textContent = state.running ? "ПРАЦЮЄ" : legacyFinished ? version(cycle) + " · " + phase : phase;
  $("phase-tag").classList.toggle("warning", ["SKIPPED", "STOPPED_BEFORE_ENTRY", "NEEDS_REVIEW"].includes(phase));
  $("status-title").textContent = meta[0];
  $("status-description").textContent = !state.running && state.action === "resume" ? "Процес зупинився. Відновлення завершить цей самий цикл без нових покупок." : cycle?.stop_requested && state.running ? "Стоп прийнято. Чекаємо поточну відповідь API, потім звіряємо й закриваємо весь обсяг." : meta[1];
  $("remaining").textContent = value(cycle?.remaining_contracts) === 0 ? "0 · за журналом завершення" : value(cycle?.remaining_contracts) !== null ? fmt(cycle.remaining_contracts) + " контрактів · за журналом" : "Ще не підтверджено";
  for (const item of document.querySelectorAll(".steps li")) {
    const stage = Number(item.dataset.step);
    item.classList.toggle("done", stage < meta[2]);
    item.classList.toggle("active", state.running && stage === meta[2]);
  }
  $("play").disabled = posting || stopping || !state.can_play || !compatible;
  $("stop").disabled = posting || stopping || !state.can_stop || !compatible || (cycle?.stop_requested && state.running);
  $("stop").textContent = stopping ? "Надсилаємо стоп…" : cycle?.stop_requested && state.running ? "Стоп прийнято · очікуємо закриття" : state.can_stop && !state.running ? "■ Відновити закриття позиції" : "■ Стоп і закрити позицію";
  $("play-label").textContent = posting ? "Запускаємо…" : state.running ? "Цикл працює…" : state.action === "resume" ? "Відновити цей цикл" : "Запустити один цикл";
  $("launch-hint").textContent = !compatible ? "Відкрий ярлик Update Jev Trading Lab, коли поточний цикл завершено, потім онови сторінку. Потрібна нова версія сервера." : state.blocked || (state.running ? "Стоп завершить очікування або закриє всі покупки цього циклу. Не вимикай ПК до завершення." : state.action === "resume" ? "Перевірка й закриття вже розпочатого циклу. Нових покупок не буде." : "До 5 хв разом з очікуванням входу. До 3 покупок, разом ≤30 USDT; після закриття бот завершується.");
  showError(notice || (!["DONE","NO_FILL"].includes(phase) && cycle?.error) || (cycle?.worker_exit_code && state.action === "resume" ? "Процес завершився до підтвердження результату. Перевір позицію на OKX; можна відновити цей цикл." : ""));

  const completed = state.cycles.filter(item => item.phase === "DONE");
  const latest = completed.at(-1);
  $("result-date").textContent = latest ? time(latest.time) : "Ще немає завершених циклів";
  $("pnl").textContent = fmt(latest?.pnl_usdt);
  $("pnl").className = value(latest?.pnl_usdt) < 0 ? "negative" : "";
  $("fees").textContent = value(latest?.signed_fees_usdt) === null ? "—" : fmt(-Number(latest.signed_fees_usdt));
  $("jev-cost").textContent = fmt(latest?.usage?.cost_usd ?? latest?.jev?.cost_usd);
  $("jev-latency").textContent = latest?.jev?.elapsed_ms ? fmt(latest.jev.elapsed_ms, 1) + " мс" : "—";
  $("jev-choice").textContent = latest?.jev?.choice ? (choices[latest.jev.choice] || latest.jev.choice) + " · остання відповідь" : "—";
  $("buy-price").textContent = latest?.buy_price ? fmt(latest.buy_price, 1) + " USDT" : "—";
  $("sell-price").textContent = latest?.sell_price ? fmt(latest.sell_price, 1) + " USDT" : "—";
  $("trade-size").textContent = latest?.base_btc ? fmt(latest.base_btc) + " BTC · isolated 1×" : "—";
  $("total-jev").textContent = fmt(state.total_jev.cost_usd) + " USD" + (state.total_jev.unresolved ? " + невідомі витрати" : "");
  $("total-jev").title = `${state.total_jev.attempts} запитів: попередні перевірки та всі цикли. За локальними ledgers; не звірено з білінгом.`;
  $("series-budget").textContent = `${state.budget.attempts} / ${state.budget.max_attempts ?? 20} запитів Jev · до ${state.budget.max_cycles} циклів · поріг ${fmt(state.budget.cost_limit_usd, 2)} USD`;
  if (state.balance) renderBalance(state.balance);
  renderHistory(state.cycles);
  renderChart(completed);
  renderMonitor(cycle);
}

function renderMonitor(cycle) {
  const active = ["jev-position-v2", "jev-position-v3"].includes(cycle?.bot_id) ? cycle : null;
  const monitor = active?.monitor;
  $("monitor-stamp").textContent = monitor?.server_ms ? "Дані OKX · " + time(Number(monitor.server_ms)) : active ? "Позиція ще не відкрита" : "Очікує першого запуску V3";
  $("live-pnl").textContent = fmt(monitor?.estimated_close_pnl_usdt);
  $("held").textContent = value(monitor?.held_seconds) === null ? "—" : fmt(monitor.held_seconds, 0) + " с";
  $("entries-count").textContent = active ? `${active.entries_used} / 3` : "—";
  $("cycle-cost").textContent = active ? fmt(active.usage?.cost_usd) + " USD" : "—";
  $("cycle-calls").textContent = active ? `${active.usage?.attempts ?? 0} / 11 запитів` + (active.usage?.unresolved ? " · вартість уточнюється" : " · поріг $0,005") : "До 11 запитів · поріг $0,005";
  $("latest-decision").textContent = choices[active?.jev?.choice] || "—";
  $("close-reason").textContent = reasons[active?.close_reason] || active?.close_reason || "";
  const list = $("decisions"); list.replaceChildren();
  for (const decision of [...(active?.decisions || [])].reverse()) {
    const item = document.createElement("li");
    item.textContent = `${time(Number(decision.time_ms))} · ${choices[decision.choice] || decision.choice} · рух за 5 хв: ${fmt(value(decision.return_5m_bps) === null ? null : Number(decision.return_5m_bps)/100, 3)}% · ${fmt(decision.elapsed_ms,0)} мс · $${fmt(decision.cost_usd)}`;
    list.append(item);
  }
  const chart = $("position-chart"); chart.replaceChildren();
  const samples = (active?.samples || []).filter(s => value(s.estimated_close_pnl_usdt) !== null && value(s.held_seconds) !== null);
  if (samples.length < 2) { chart.textContent = "Динаміка оцінки PnL з’явиться під час утримання позиції."; return; }
  const amounts = samples.map(s => Number(s.estimated_close_pnl_usdt));
  const min = Math.min(0,...amounts), max = Math.max(0,...amounts), pad = Math.max((max-min)*.15,.0001);
  const x = s => 70 + Number(s.held_seconds) / Math.max(1, Number(samples.at(-1).held_seconds)) * 870;
  const y = p => 12 + (max+pad-p)/(max-min+2*pad) * 95;
  const svg = svgNode("svg", {viewBox:"0 0 980 135", "aria-hidden":"true"});
  svg.append(svgNode("line",{x1:70,x2:940,y1:y(0),y2:y(0),stroke:"#ced9c7","stroke-dasharray":"3 5"}));
  svg.append(svgNode("polyline",{points:samples.map(s=>`${x(s)},${y(Number(s.estimated_close_pnl_usdt))}`).join(" "),fill:"none",stroke:"#739453","stroke-width":2.5}));
  for (const p of [min,max]) svg.append(svgNode("text",{x:62,y:y(p)+4,"text-anchor":"end",fill:"#839477","font-size":11},fmt(p,5)));
  svg.append(svgNode("text",{x:70,y:130,fill:"#839477","font-size":11},"Від першої покупки"));
  svg.append(svgNode("text",{x:940,y:130,"text-anchor":"end",fill:"#839477","font-size":11},fmt(samples.at(-1).held_seconds,0)+" с"));
  chart.append(svg);
}

function renderBalance(balance) {
  $("equity").textContent = fmt(balance.portfolio_equity_usd, 2) + " USD";
  $("available").textContent = fmt(balance.available_usdt, 2) + " USDT";
  $("balance-date").textContent = time(balance.updated_at);
}

function renderHistory(cycles) {
  const signature = JSON.stringify(cycles);
  if (signature === historySignature) return;
  historySignature = signature;
  const focused = document.activeElement?.dataset?.run;
  $("history-count").textContent = `${cycles.length} ${plural(cycles.length, {one:"тест",few:"тести",many:"тестів"})}`;
  const tbody = $("history");
  tbody.replaceChildren();
  if (!cycles.length) {
    const row = tbody.insertRow(); const cell = row.insertCell();
    cell.colSpan = 5; cell.className = "empty-row"; cell.textContent = "Результати з’являться після першого запуску.";
    return;
  }
  [...cycles].reverse().forEach((cycle, index) => {
    const row = tbody.insertRow();
    const title = version(cycle) + " · " + String(cycles.length - index).padStart(2, "0");
    const texts = [title, time(cycle.time), cycle.phase, fmt(cycle.base_btc), fmt(cycle.pnl_usdt)];
    row.className = "history-row";
    row.addEventListener("click", () => window.openHistory ? openHistory(cycle.id, title) : showError("Онови сервер через ярлик Update Jev Trading Lab і перезавантаж сторінку."));
    texts.forEach((text, column) => {
      const cell = row.insertCell();
      if (column >= 3) cell.className = "numeric";
      if (column === 0) { const button = document.createElement("button"); button.type = "button"; button.className = "history-open"; button.dataset.run = cycle.id; button.textContent = title + " ↗"; button.setAttribute("aria-label", "Відкрити журнал " + title); cell.append(button); }
      else if (column === 2) { const badge = document.createElement("span"); badge.className = "history-pill"; badge.textContent = text; cell.append(badge); }
      else cell.textContent = text;
    });
  });
  if (focused) [...tbody.querySelectorAll("button")].find(button => button.dataset.run === focused)?.focus();
}

function svgNode(tag, attributes, text) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.entries(attributes).forEach(([key, item]) => element.setAttribute(key, String(item)));
  if (text !== undefined) element.textContent = text;
  return element;
}

function renderChart(cycles) {
  const chart = $("chart"); chart.replaceChildren();
  const filled = cycles.filter(cycle => value(cycle.pnl_usdt) !== null);
  if (!filled.length) {
    const empty = document.createElement("div"); empty.className = "empty-chart"; empty.textContent = "Графік побудуємо за фактичними завершеними циклами."; chart.append(empty);
    $("total-pnl").textContent = "—"; return;
  }
  const values = [0]; filled.forEach(cycle => values.push(values.at(-1) + Number(cycle.pnl_usdt)));
  $("total-pnl").textContent = fmt(values.at(-1));
  chart.setAttribute("aria-label", `Накопичений PnL за ${filled.length} ${plural(filled.length, {one:"цикл",few:"цикли",many:"циклів"})}: ${fmt(values.at(-1))} USDT, без funding і Jev.`);
  const width = 980, height = 176, left = 88, right = 26, top = 15, bottom = 26;
  const min = Math.min(...values), max = Math.max(...values), pad = Math.max((max-min) * .16, .0001);
  const low = min-pad, high = max+pad;
  const x = index => left + (width-left-right) * index / (values.length-1);
  const y = amount => top + (high-amount) / (high-low) * (height-top-bottom);
  const svg = svgNode("svg", {viewBox:`0 0 ${width} ${height}`, preserveAspectRatio:"none", "aria-hidden":"true"});
  [max, (max+min)/2, min].filter((n,i,a)=>a.indexOf(n)===i).forEach(amount => {
    svg.append(svgNode("line", {x1:left,y1:y(amount),x2:width-right,y2:y(amount),stroke:"#e7ece1","stroke-dasharray":"3 5"}));
    svg.append(svgNode("text", {x:left-13,y:y(amount)+3,"text-anchor":"end",fill:"#94a087","font-size":"10"}, fmt(amount, 5)));
  });
  const coords = values.map((amount,index)=>`${x(index)},${y(amount)}`).join(" ");
  svg.append(svgNode("polyline", {points:coords,fill:"none",stroke:"#749258","stroke-width":"2.4","stroke-linecap":"round","stroke-linejoin":"round"}));
  values.forEach((amount,index) => {
    const dot = svgNode("circle", {cx:x(index),cy:y(amount),r:index===values.length-1?4:2.5,fill:"#6a8950",stroke:"white","stroke-width":2});
    dot.append(svgNode("title", {}, `${index===0?"Старт":"Цикл "+index}: ${fmt(amount)} USDT`)); svg.append(dot);
    if (values.length <= 8 || index === 0 || index === values.length-1) svg.append(svgNode("text", {x:x(index),y:height-5,"text-anchor":"middle",fill:"#94a087","font-size":"10"},index===0?"Старт":"#"+index));
  });
  chart.append(svg);
}

async function refresh() {
  try { render(await request("/api/state")); }
  catch (error) {
    $("connection").classList.add("offline"); $("connection").lastChild.textContent = "Немає зв’язку з панеллю";
    $("play").disabled = true; $("stop").disabled = true; showError(error.message + " Якщо бот уже запущений, він може ще працювати.");
  }
}

$("play").addEventListener("click", async () => {
  if (posting || !snapshot?.can_play || snapshot.api_version !== 3) return;
  posting = true; notice = ""; render(snapshot);
  try { await request("/api/play", {generation:snapshot.generation}); }
  catch (error) { notice = error.message; } // Never retry a financial action automatically.
  finally { posting = false; await refresh(); }
});

$("stop").addEventListener("click", async () => {
  if (posting || stopping || !snapshot?.can_stop || snapshot.api_version !== 3) return;
  stopping = true; notice = ""; render(snapshot);
  try { await request("/api/stop", {generation:snapshot.generation, id:snapshot.current.id}); }
  catch (error) { notice = error.message; }
  finally { stopping = false; await refresh(); }
});

$("refresh-balance").addEventListener("click", async () => {
  $("refresh-balance").disabled = true;
  $("refresh-balance").textContent = "Оновлюємо…";
  try { renderBalance(await request("/api/balance", {})); notice = ""; }
  catch (error) { notice = error.message; showError(notice); }
  finally { $("refresh-balance").disabled = false; $("refresh-balance").textContent = "↻ Оновити баланс"; }
});

async function poll() { await refresh(); if (window.refreshHistory) await refreshHistory(); window.setTimeout(poll, document.hidden ? 5000 : 1500); }
poll(); // Local journal reads only. No balance, Jev or trade call on page load.
