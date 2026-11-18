"use strict";
/* Per-bot evidence renderer; no execution or financial requests. */
((root, factory) => {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ArenaLifecycleV6 = api;
})(typeof window === "undefined" ? {} : window, () => {
  const DURATIONS = Object.freeze([
    [300, "5 хвилин"], [900, "15 хвилин"], [1800, "30 хвилин"],
    [3600, "1 година"], [7200, "2 години"], [18000, "5 годин"], [86400, "24 години"]
  ]);
  const STATUS_UA = Object.freeze({
    observed: "Зафіксовано", blocked: "Заблоковано", pending: "Очікується",
    not_observed: "Не спостережено", not_applicable: "Не застосовується"
  });
  const PHASE_UA = Object.freeze({
    OPEN: "Вікно нових входів", ADMISSION: "Вікно нових входів", RUNNING: "Вікно нових входів",
    DRAINING: "Завершення відкритих позицій", CLOSING: "Закриття позицій",
    PAUSED: "Пауза · лише відновлення закриття", DONE: "Завершено"
  });
  const fmtNumber = (value, digits = 3) => value === null || value === undefined || value === "" || !Number.isFinite(Number(value))
    ? "—" : new Intl.NumberFormat("uk-UA", { maximumFractionDigits: digits }).format(Number(value));
  const fmtCost = value => value === null || value === undefined || value === "" || !Number.isFinite(Number(value))
    ? "Невідомо" : "$" + new Intl.NumberFormat("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 6 }).format(Number(value)) + " USD";
  const fmtSigned = value => {
    const number = finiteNumber(value);
    return number === null ? "Невідомо" : `${number > 0 ? "+" : ""}${fmtNumber(number, 8)} USDT`;
  };
  const text = (value, fallback = "—") => value === null || value === undefined || value === "" ? fallback : String(value);
  // The schema-4 feature book of an exchange run (run.feature_book, exchange_book.GapBookFeed.book_state): one line
  // for the owner instead of a silent "checking the next entry" while it warms up, reads a hole or has failed.
  function bookText(book) {
    if (!book || typeof book !== "object") return null;
    const last = book.last_gap, lastNote = last && last.flat_filled  // R2.1.9: a lone edge minute filled flat, features still counted
      ? " Останню пропущену хвилину заповнено пласко за попередньою ціною; цінові ознаки рахуються далі."
        + (book.spread ? " Spread-ознаки над нею UNKNOWN на весь свій період." : "")  // R2.1.11 (R219-BOOK-SPREAD)
      : last && last.gave_up && last.read === last.minutes
      ? ` Свічки останнього пропуску дочитано (${fmtNumber(last.read, 0)} з ${fmtNumber(last.minutes, 0)} хв); index і mark дочитуються окремо.`
      : last && (last.gave_up || last.missing > 1)
      ? ` Останній пропуск: дочитано ${fmtNumber(last.read, 0)} з ${fmtNumber(last.minutes, 0)} хв, ознаки над ним UNKNOWN на весь свій період.` : "";
    if (book.status === "warming") {
      const pages = book.warmup_pages === null || book.warmup_pages === undefined ? "" : ` · прочитано ${fmtNumber(book.warmup_pages, 0)} з ≈ ${fmtNumber(book.warmup_pages_expected, 0)} сторінок історії`;
      return `Книга ознак схеми 4: прогрів${pages} · спроба ${fmtNumber(book.attempts, 0)} з ${fmtNumber(book.warmup_attempts, 0)}. Рішень про вхід за схемою 4 ще немає.`;
    }
    if (book.status === "exhausted") return `Прогрів книги ознак не вдався ${fmtNumber(book.warmup_attempts, 0)} рази: до кінця раунду рішень про вхід за схемою 4 немає. Виходи й стоп це не зачіпає.`;
    if (book.status === "unavailable") return book.warm ? "Книга ознак схеми 4 тимчасово недоступна; повтор через 30 с." : `Прогрів книги ознак не вдався (спроба ${fmtNumber(book.attempts, 0)} з ${fmtNumber(book.warmup_attempts, 0)}); повтор через 30 с.`;
    if (book.gap) return `Книга ознак схеми 4: дочитуємо пропуск ${fmtNumber(book.gap.minutes, 0)} хв із публічної історії; доти рішень про вхід за схемою 4 немає.`;
    if (book.status === "degraded") {
      const series = { index: "індекс", mark: "mark-ціну", funding: "funding" };
      const missing = String(book.detail || "").split(":").slice(1).join(":").split(",").map(item => series[item.trim()] || item.trim()).filter(Boolean);
      return `Книга ознак схеми 4: не прочитано ${missing.join(", ") || "частину рядів"}; ознаки, що їх потребують, UNKNOWN.${lastNote}`;
    }
    if (book.status === "ready") return `Книга ознак схеми 4 готова: рішення про вхід на кожному новому закритому барі.${lastNote}`;
    return null;
  }
  const finiteNumber = value => value === null || value === undefined || value === "" || !Number.isFinite(Number(value)) ? null : Number(value);
  const el = (tag, className, value) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = value;
    return node;
  };

  function normalizeFunnel(funnel) {
    if (Array.isArray(funnel)) return funnel;
    if (!funnel || typeof funnel !== "object") return [];
    const rows = [];
    const chains = [
      ["entry", "Вхід", ["checks", "signals", "jev_allows", "rechecks", "entry_filled_orders"]],
      ["add", "Докупівля", ["checks", "signals", "jev_allows", "rechecks", "add_filled_orders"]]
    ];
    for (const [key, label, fields] of chains) {
      const flow = funnel[key];
      if (!flow || typeof flow !== "object") continue;
      const fieldLabels = ["перевірок", "сигналів", "дозволів Jev", "повторних перевірок", "виконаних ордерів"];
      const counts = fields.map((name, index) => `${text(flow[name] ?? funnel[name], "—")} ${fieldLabels[index]}`);
      rows.push({ id: key, label: `${label} · ${counts.join(" → ")}`, count: null });
    }
    if (funnel.hold && typeof funnel.hold === "object") {
      const hold = funnel.hold;
      rows.push({ id: "hold", label: `Jev · перевірка утримання · перевірки ${text(hold.checks, "—")} · сигнали ${text(hold.signals, "—")} · Jev: ${text(hold.jev_decisions, "—")} рішень / ${text(hold.jev_calls, "—")} запитів / ${text(hold.jev_allows, "—")} дозволів / ${text(hold.jev_holds, "—")} пауз / ${text(hold.jev_vetoes, "—")} відмов · повторні перевірки ${text(hold.rechecks, "—")} / відмови ${text(hold.recheck_vetoes, "—")} · невідомий результат ${text(hold.jev_outcomes_unknown, "—")}`, count: null });
    }
    const globalLabels = {
      close_filled_orders: "Виконані закриття", round_trips: "Повні цикли угоди", order_intents: "Наміри ордера",
      flat_waits: "Очікування без позиції", position_holds: "Перевірки відкритої позиції",
      no_signal_unknown: "Невідомий результат сигналу", cumulative_fill_updates: "Оновлення фактичного виконання"
    };
    for (const [field, label] of Object.entries(globalLabels)) {
      if (funnel[field] !== undefined) rows.push({ id: field, label: `${label} · ${text(funnel[field])}`, count: null });
    }
    if (funnel.limitation) rows.push({ id: "limitation", label: `Обмеження · ${text(funnel.limitation)}`, count: null });
    if (rows.length) return rows;
    return Object.entries(funnel).map(([key, value]) => {
      if (typeof value === "number" || typeof value === "string") return { id: key, label: key, count: value };
      return value && typeof value === "object" ? { id: key, label: text(value.label, key), ...value } : { id: key, label: key, count: null };
    });
  }

  function normalizeChecks(checks) {
    if (Array.isArray(checks)) return checks;
    if (!checks || typeof checks !== "object") return [];
    return Object.entries(checks).map(([key, value]) => value && typeof value === "object" ? { id: key, ...value } : { id: key, actual: value });
  }

  function phaseText(phase) {
    if (phase && typeof phase === "object") return text(phase.label, PHASE_UA[String(phase.code || "").toUpperCase()] || text(phase.code, "Невідомо"));
    return text(phase, "Невідомо");
  }

  function waitLabel(reason) {
    if (reason && typeof reason === "object") return text(reason.message, text(reason.code, ""));
    return text(reason, "");
  }

  function formatCheck(check) {
    if (!check || typeof check !== "object") return text(check);
    const title = text(check.label, text(check.name, text(check.feature, text(check.code, "Умова"))));
    const showValue = value => value && typeof value === "object" ? JSON.stringify(value) : text(value);
    const expression = [check.op, check.expected !== undefined ? `очікувалося ${showValue(check.expected)}` : null,
      check.actual !== undefined ? `отримано ${showValue(check.actual)}` : check.value !== undefined ? `значення ${showValue(check.value)}` : null,
      check.passed !== undefined ? (check.passed === null ? "невідомо" : check.passed ? "пройдено" : "не пройдено") : null].filter(Boolean).join(" · ");
    return expression ? `${title} · ${expression}` : title;
  }

  function appendChecks(host, failed, unknown) {
    for (const [kind, values] of [["failed", failed], ["unknown", unknown]]) {
      if (!values.length) continue;
      const section = el("div", `v6-checks ${kind}`);
      section.append(el("b", "", kind === "failed" ? "Умови, що не пройшли" : "Умови з невідомим результатом"));
      const list = el("ul", "v6-check-list");
      for (const check of values) list.append(el("li", "", formatCheck(check)));
      section.append(list);
      host.append(section);
    }
  }

  function appendCostBreakdown(host, costs) {
    const box = el("div", "v6-accounting");
    box.append(el("b", "v6-accounting-title", "Облік у USDT"));
    const rows = el("dl", "v6-accounting-rows");
    const values = [
      ["Валовий PnL", costs.gross_pnl_usdt],
      ["Комісії · зі знаком", costs.signed_fees_usdt],
      ["Funding · зі знаком", costs.signed_funding_usdt],
      [costs.complete === true ? "Підсумковий чистий результат" : "Результат за виконаними заявками", costs.net_usdt],
      ["Зміна грошового залишку", costs.cash_delta_usdt]
    ];
    if (costs.open_position_mark_includes_exit_fee_estimate) values.push(["Поточний PnL · оцінка з виходом", costs.mark_pnl_usdt]);
    for (const [label, value] of values) {
      rows.append(el("dt", "", label), el("dd", "", fmtSigned(value)));
    }
    box.append(rows);
    const completeness = costs.complete === true ? "Облік завершений" : costs.complete === false ? "Облік неповний" : "Повнота обліку невідома";
    box.append(el("p", "v6-accounting-source", `${completeness} · джерело: ${text(costs.source, "невідомо")}`));

    const jev = costs.jev && typeof costs.jev === "object" ? costs.jev : null;
    if (jev) {
      const jevBox = el("div", "v6-jev-accounting");
      jevBox.append(el("b", "", "Jev · окремо в USD"));
      jevBox.append(el("span", "", `Відомі витрати · ${jev.known_cost_usd === null || jev.known_cost_usd === undefined ? "невідомо" : fmtCost(jev.known_cost_usd)}`));
      jevBox.append(el("span", "", `Запити · ${text(jev.requests, "невідомо")}`));
      jevBox.append(el("span", "", `Запити з невідомим рахунком · ${text(jev.unknown_billing_requests, "невідомо")}`));
      jevBox.append(el("span", "", `${jev.complete === true ? "Облік завершений" : jev.complete === false ? "Облік неповний" : "Повнота невідома"} · джерело: ${text(jev.source, "невідомо")}`));
      box.append(jevBox);
    }
    host.append(box);
  }

  function durationOptions(selectedValue) {
    const selected = Number(selectedValue);
    return DURATIONS.map(([seconds, label]) => ({ value: seconds, label, selected: seconds === selected }));
  }

  function countdown(admissionEndAt, now = Date.now()) {
    const end = typeof admissionEndAt === "number" ? admissionEndAt : Date.parse(admissionEndAt || "");
    if (!Number.isFinite(end)) return null;
    const remaining = Math.max(0, Math.ceil((end - now) / 1000));
    const hours = Math.floor(remaining / 3600), minutes = Math.floor((remaining % 3600) / 60), seconds = remaining % 60;
    return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
      : `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  }

  function runCountdown(lifecycle, options={}) {
    if(options.serverTimer) {
      const seconds=options.remainingSeconds;
      return typeof seconds==="number"&&Number.isFinite(seconds)?countdown(Math.max(0,seconds)*1000,0):null;
    }
    return countdown(lifecycle?.admission_end_at ?? lifecycle?.end_at, options.now ?? Date.now());
  }

  function lifecycleModel(lifecycle) {
    const bots = Array.isArray(lifecycle?.bots) ? lifecycle.bots : [];
    return bots.map(bot => {
      const rows = Array.isArray(bot.rows) ? bot.rows : [];
      const funnel = normalizeFunnel(bot.funnel);
      const observed = finiteNumber(bot.observed_required_count);
      const required = finiteNumber(bot.required_count);
      return {
        id: text(bot.bot_id, "unknown"), name: text(bot.name, text(bot.label, "Бот")), rows,
        funnel, observedRequired: observed, required,
        complete: bot.complete === true,
        diagnostic: bot.diagnostic === true,
        limitations: Array.isArray(bot.limitations) ? bot.limitations : [],
        currentPhase: bot.current_phase && typeof bot.current_phase === "object" ? bot.current_phase : { label: text(bot.current_phase, "Невідомо") },
        waitReason: bot.wait_reason && typeof bot.wait_reason === "object" ? bot.wait_reason : { message: text(bot.wait_reason, "") },
        failedChecks: normalizeChecks(bot.wait_reason?.failed_checks), unknownChecks: normalizeChecks(bot.wait_reason?.unknown_checks),
        costs: bot.costs && typeof bot.costs === "object" ? bot.costs : {}
      };
    });
  }

  function appendRows(host, rows) {
    const list = el("ol", "v6-lifecycle-rows");
    for (const row of rows) {
      const status = Object.hasOwn(STATUS_UA, row.status) ? row.status : "unknown";
      const item = el("li", `v6-lifecycle-row ${status}`);
      const label = el("b", "", text(row.label, text(row.id, "Етап")));
      const state = el("span", "v6-row-status", `${row.required === false ? "Додатковий · " : ""}${STATUS_UA[status] || text(row.status, "Невідомо")}`);
      item.append(label, state);
      if (row.condition) item.append(el("p", "v6-row-condition", `Умова · ${row.condition}`));
      if (row.step !== null && row.step !== undefined) item.append(el("small", "v6-row-step", `Крок · ${text(row.step)}`));
      if (row.detail) item.append(el("p", "v6-row-detail", row.detail));
      const evidence = [];
      if (row.event_at) evidence.push(text(row.event_at));
      if (row.event_seq !== null && row.event_seq !== undefined) evidence.push(`подія #${text(row.event_seq)}`);
      if (row.evidence) evidence.push(text(row.evidence));
      if (evidence.length) item.append(el("small", "v6-row-evidence", evidence.join(" · ")));
      if (row.failed_checks || row.unknown_checks) appendChecks(item, normalizeChecks(row.failed_checks), normalizeChecks(row.unknown_checks));
      list.append(item);
    }
    host.append(list);
  }

  function renderManifest(lifecycle) {
    if (!lifecycle?.match_manifest && !lifecycle?.match_manifest_sha256) return null;
    const box = el("details", "v6-manifest");
    const summary = el("summary", "", "Спільний маніфест умов запуску");
    box.append(summary);
    if (lifecycle.match_manifest_sha256) box.append(el("p", "v6-manifest-hash", `SHA-256 · ${text(lifecycle.match_manifest_sha256)}`));
    if (lifecycle.match_manifest) {
      const pre = el("pre", "v6-manifest-json", JSON.stringify(lifecycle.match_manifest, null, 2));
      box.append(pre);
    }
    return box;
  }

  function renderLifecycle(lifecycle, options = {}) {
    const host = el("section", `v6-lifecycle ${options.executionMode || "paper"}`);
    host.setAttribute("aria-label", "Перевірки та перебіг кожного бота");
    const mode = options.executionMode || "paper";
    const modeLabel = options.fixtureOnly ? "Тест UI · симульовані біржові угоди" : { paper: "PAPER · віртуальні угоди", demo: "OKX Demo · демонстраційні угоди", live: "OKX Live · фактичні угоди" }[mode] || "Режим не визначено";
    host.append(el("div", "v6-lifecycle-mode", modeLabel));

    const phase = text(options.phase ?? lifecycle?.phase, "UNKNOWN").toUpperCase();
    const phaseHeading = PHASE_UA[phase] || text(lifecycle?.phase, "Стан циклу не повідомлено");
    const header = el("div", "v6-lifecycle-head");
    header.append(el("h3", "", "Перевірки та докази по ботах"));
    const clock = runCountdown(lifecycle, options);
    const clockLabel = clock === null ? "Дедлайн вікна входів не повідомлено" : `До дедлайну входів · ${clock}`;
    header.append(el("span", "v6-cutoff", `${phaseHeading} · ${clockLabel}`));
    host.append(header);

    if (lifecycle?.bots?.some(bot => bot.diagnostic === true)) {
      host.append(el("p", "v6-admission-note", "Технічний Demo сценарій перевіряє один цикл: до 2 покупок по ≤10 USDT, до 6 запитів Jev. Повторний вхід після закриття в цьому сценарії не дозволений."));
    } else if (options.timePolicy === "admission_then_drain" && (phase === "DRAINING" || lifecycle?.admission_closed_at)) {
      host.append(el("p", "v6-draining-note", "Вікно входів завершилося. Нові входи й докупівлі зупинені; відкриті позиції завершують власні правила виходу або максимальний час утримання."));
    } else if (options.timePolicy === "admission_then_drain") {
      host.append(el("p", "v6-admission-note", "Поки триває вікно входів, бот може починати повторні цикли після завершення попереднього. Після дедлайну нових входів і докупівель не буде."));
    } else {
      host.append(el("p", "v6-legacy-note", "Історичний запуск без політики завершення після вікна входів: для нього збережено стару жорстку межу часу. Він не підтверджує роботу довгих циклів."));
    }
    if (lifecycle?.admission_closed_at) host.append(el("small", "v6-closed-at", `Вікно входів закрилося · ${text(lifecycle.admission_closed_at)}`));
    const book = ["OPEN", "ADMISSION", "RUNNING"].includes(phase) ? bookText(options.featureBook) : null;  // no book work after the cutoff
    if (book) host.append(el("p", "v6-admission-note v6-book-note", book));
    if (options.technicalCaps) host.append(el("p", "v6-caps-note", options.technicalCaps));
    const manifest = renderManifest(lifecycle);
    if (manifest) host.append(manifest);

    const bots = lifecycleModel(lifecycle);
    if (!bots.length) {
      host.append(el("p", "v6-lifecycle-empty", "Окремі дані по ботах ще не надійшли від сервера. Загальні лічильники не замінюють перевірку кожного бота."));
      return host;
    }
    const grid = el("div", "v6-bot-lifecycles");
    for (const bot of bots) {
      const card = el("details", "v6-bot-lifecycle");
      card.dataset.botId = bot.id;
      const summary = el("summary", "v6-bot-summary");
      const count = bot.required === null || bot.observedRequired === null ? "Обов’язкове покриття невідоме" : `Обов’язкові етапи · ${bot.observedRequired}/${bot.required}`;
      const coverage = bot.complete ? "Повне покриття" : count;
      summary.append(el("b", "", bot.name), el("span", bot.complete ? "v6-complete" : "v6-incomplete", `${bot.diagnostic ? "Діагностика · " : ""}${coverage}`));
      card.append(summary);
      const meta = el("div", "v6-bot-meta");
      const phase = bot.currentPhase;
      const botPhaseText = `${phaseText(phase)}${phase.step !== null && phase.step !== undefined ? ` · крок ${text(phase.step)}` : ""}`;
      meta.append(el("span", "", `Етап: ${botPhaseText}`));
      if (phase.event_at || phase.event_seq !== undefined) meta.append(el("small", "v6-row-evidence", [phase.event_at, phase.event_seq !== undefined ? `подія #${phase.event_seq}` : null].filter(Boolean).join(" · ")));
      const reason = bot.waitReason;
      if (waitLabel(reason)) meta.append(el("span", "", `Очікує: ${waitLabel(reason)}`));
      if (reason.event_at || reason.event_seq !== undefined) meta.append(el("small", "v6-row-evidence", [reason.event_at, reason.event_seq !== undefined ? `подія #${reason.event_seq}` : null].filter(Boolean).join(" · ")));
      card.append(meta);
      appendChecks(card, bot.failedChecks, bot.unknownChecks);
      if (bot.funnel.length) {
        const funnel = el("ol", "v6-funnel");
        funnel.setAttribute("aria-label", `Воронка перевірок ${bot.name}`);
        for (const step of bot.funnel) {
          const countValue = step.count ?? step.observed_count;
          funnel.append(el("li", "", countValue === null || countValue === undefined ? text(step.label, text(step.id, "Перевірка")) : `${text(step.label, text(step.id, "Перевірка"))} · ${fmtNumber(countValue, 0)}${step.blocked_count !== undefined ? ` · заблоковано ${fmtNumber(step.blocked_count, 0)}` : ""}`));
        }
        card.append(funnel);
        card.append(el("small", "v6-funnel-note", "Лічильники етапів можуть описувати ті самі події та не додаються між собою."));
      }
      appendRows(card, bot.rows);
      appendCostBreakdown(card, bot.costs);
      if (bot.limitations.length) {
        const limitations = el("ul", "v6-limitations");
        for (const item of bot.limitations) limitations.append(el("li", "", typeof item === "string" ? item : JSON.stringify(item)));
        card.append(limitations);
      }
      grid.append(card);
    }
    host.append(grid);
    return host;
  }

  function renderBotDetail(lifecycle, botId, options = {}) {
    const bot = lifecycleModel(lifecycle).find(item => item.id === String(botId));
    if (!bot) return null;
    const section = el("section", "v6-bot-detail");
    section.append(el("h3", "", `Деталі перевірок · ${bot.name}`));
    const phase = bot.currentPhase, reason = bot.waitReason;
    section.append(el("p", "v6-bot-detail-phase", `Поточний етап · ${phaseText(phase)}${phase.step !== null && phase.step !== undefined ? ` · крок ${text(phase.step)}` : ""}${waitLabel(reason) ? ` · Очікує: ${waitLabel(reason)}` : ""}`));
    appendChecks(section, bot.failedChecks, bot.unknownChecks);
    if (bot.funnel.length) {
      const funnel = el("ol", "v6-funnel");
      for (const step of bot.funnel) {
        const count = step.count ?? step.observed_count;
        funnel.append(el("li", "", count === null || count === undefined ? text(step.label, text(step.id, "Перевірка")) : `${text(step.label, text(step.id, "Перевірка"))} · ${fmtNumber(count, 0)}`));
      }
      section.append(funnel);
      section.append(el("small", "v6-funnel-note", "Лічильники етапів можуть описувати ті самі події та не додаються між собою."));
    }
    appendRows(section, bot.rows);
    appendCostBreakdown(section, bot.costs);
    if (bot.limitations.length) {
      const limits = el("ul", "v6-limitations");
      for (const item of bot.limitations) limits.append(el("li", "", typeof item === "string" ? item : JSON.stringify(item)));
      section.append(limits);
    }
    if (options.includeManifest) {
      const manifest = renderManifest(lifecycle);
      if (manifest) section.append(manifest);
    }
    return section;
  }

  function optionsForRun(run) {
    const rules = run.rules || {}, manifest = run.match_manifest || {}, budget = manifest.jev_budget || {};
    const mode = run.mode === "OKX_LIVE" ? "live" : run.mode === "OKX_DEMO" ? "demo" : "paper";
    const parts = [];
    if (rules.max_total_entries) parts.push(`До ${rules.max_total_entries} спроб входу за раунд`);
    if (rules.max_order_intents_per_bot) parts.push(`до ${rules.max_order_intents_per_bot} заявок на бота`);
    if (budget.season_attempts) parts.push(`до ${budget.season_attempts} запитів Jev на весь раунд`);
    if (rules.max_drawdown_pct) parts.push(`межа просадки ${rules.max_drawdown_pct}%`);
    return { executionMode: mode, fixtureOnly: run.fixture_only === true, phase: run.phase || run.status, timePolicy: run.time_policy, featureBook: run.feature_book || null,
      serverTimer: run.clock_policy === "okx_utc_monotonic_v1", remainingSeconds: run.timing?.admission_remaining_seconds ?? null,
      technicalCaps: run.lifecycle?.bots?.some(bot => bot.diagnostic === true) ? "Окрема технічна перевірка: 100 USDT Demo · до 20 USDT номіналу · 5 хвилин · одна позиція, без повторного входу." : parts.length ? `${parts.join(" · ")}. Довший час не збільшує торгові й AI-ліміти.` : null };
  }

  const renderCache = new WeakMap();
  function mount(host, lifecycle, options = {}) {
    const clock = ["OPEN", "RUNNING"].includes(options.phase) ? runCountdown(lifecycle, options) : null;
    const signature = JSON.stringify([lifecycle, options, clock]);
    if (renderCache.get(host) === signature) return;
    const opened = new Set(Array.from(host.querySelectorAll("details[data-bot-id][open]"), item => item.dataset.botId));
    const manifestOpen = Boolean(host.querySelector(".v6-manifest[open]"));
    const scrolls = new Map(Array.from(host.querySelectorAll(".v6-manifest-json"), item => ["manifest", item.scrollTop]));
    const node = renderLifecycle(lifecycle, options);
    for (const card of node.querySelectorAll("details[data-bot-id]")) card.open = opened.has(card.dataset.botId);
    const manifest = node.querySelector(".v6-manifest");
    if (manifest) manifest.open = manifestOpen;
    host.replaceChildren(node);
    const pre = node.querySelector(".v6-manifest-json");
    if (pre) pre.scrollTop = scrolls.get("manifest") || 0;
    renderCache.set(host, signature);
  }

  return { DURATIONS, durationOptions, countdown, runCountdown, lifecycleModel, renderLifecycle, renderBotDetail, optionsForRun, mount, bookText };
});
