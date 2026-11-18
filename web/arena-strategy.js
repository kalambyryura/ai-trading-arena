(function () {
  "use strict";

  const FEATURES = [
    ["sma_gap_bps", "Відхилення SMA 5 від SMA 20", "б.п."],
    ["return_5m_bps", "Зміна ціни за 5 хв", "б.п."],
    ["return_1m_bps", "Зміна ціни за 1 хв", "б.п."],
    ["rsi_14", "RSI 14", "0–100"],
    ["range_position_20", "Позиція в діапазоні 20 свічок", "% 0–100"],
    ["volume_ratio_5", "Обсяг: останні 5 / попередні 5 свічок", "множник"],
    ["spread_bps", "Спред купівлі-продажу", "б.п."]
  ];
  const OPERATORS = [["gt", "> більше"], ["gte", "≥ не менше"], ["lt", "< менше"], ["lte", "≤ не більше"]];
  const DEFAULTS = {
    rules: {
      side: "long", entry: { mode: "all", conditions: [] }, exit: { mode: "any", conditions: [] },
      size_pct: 20, max_entries: 1, add_after_bps: 20, stop_loss_bps: 50,
      take_profit_bps: 100, max_hold_seconds: 300, cooldown_seconds: 60, max_spread_bps: 5
    }
  };

  function el(tag, attrs, content) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([key, value]) => {
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "for") node.htmlFor = value;
      else node.setAttribute(key, value);
    });
    if (content != null) node.textContent = content;
    return node;
  }

  function field(label, control, hint) {
    const wrap = el("div", { class: "strategy-field" });
    const id = control.id || `strategy-field-${Math.random().toString(36).slice(2)}`;
    control.id = id;
    wrap.append(el("label", { for: id, text: label }), control);
    if (hint) wrap.append(el("small", { class: "strategy-help", text: hint }));
    return wrap;
  }

  function input(type, value, attrs) {
    const node = el("input", Object.assign({ type, value: value == null ? "" : String(value) }, attrs || {}));
    return node;
  }

  function select(options, value, attrs) {
    const node = el("select", attrs || {});
    options.forEach(([key, label]) => {
      const option = el("option", { value: key, text: label });
      option.selected = key === value;
      node.append(option);
    });
    return node;
  }

  function makeCondition(condition, onRemove, removable) {
    const row = el("div", { class: "strategy-condition" });
    const feature = select(FEATURES.map(([key, label]) => [key, `${label} (${FEATURES.find(x => x[0] === key)[2]})`]), condition?.feature || FEATURES[0][0], { "aria-label": "Ознака" });
    const op = select(OPERATORS, condition?.op || "gt", { "aria-label": "Порівняння" });
    const val = input("number", condition?.value ?? "", { step: "any", required: "", "aria-label": "Порогове значення" });
    row.append(feature, op, val);
    if (removable) {
      const remove = el("button", { type: "button", class: "strategy-remove", text: "Прибрати" });
      remove.addEventListener("click", onRemove);
      row.append(remove);
    }
    return row;
  }

  function stripBody(source) {
    const rules = source?.rules || {};
    const conditionList = (group) => Array.isArray(group?.conditions) ? group.conditions.slice(0, 6).map((item) => ({
      feature: FEATURES.some(x => x[0] === item?.feature) ? item.feature : FEATURES[0][0],
      op: OPERATORS.some(x => x[0] === item?.op) ? item.op : "gt",
      value: Number.isFinite(Number(item?.value)) ? Number(item.value) : ""
    })) : [];
    return {
      name: typeof source?.name === "string" ? source.name : "",
      author: {
        name: typeof source?.author?.name === "string" ? source.author.name : "",
        kind: ["human", "model", "example"].includes(source?.author?.kind) ? source.author.kind : "human",
        model: typeof source?.author?.model === "string" ? source.author.model : "",
        submitted_at: typeof source?.author?.submitted_at === "string" ? source.author.submitted_at : "",
        prompt: typeof source?.author?.prompt === "string" ? source.author.prompt : "",
        attempts: Number.isInteger(source?.author?.attempts) ? source.author.attempts : 1,
        edited_by: typeof source?.author?.edited_by === "string" ? source.author.edited_by : ""
      },
      description: typeof source?.description === "string" ? source.description : "",
      original_text: typeof source?.original_text === "string" ? source.original_text : "",
      rules: {
        side: rules.side === "short" ? "short" : "long",
        entry: { mode: rules.entry?.mode === "any" ? "any" : "all", conditions: conditionList(rules.entry) },
        exit: { mode: rules.exit?.mode === "all" ? "all" : "any", conditions: conditionList(rules.exit) },
        size_pct: Number(rules.size_pct ?? DEFAULTS.rules.size_pct),
        max_entries: Number(rules.max_entries ?? DEFAULTS.rules.max_entries),
        add_after_bps: Number(rules.add_after_bps ?? DEFAULTS.rules.add_after_bps),
        stop_loss_bps: Number(rules.stop_loss_bps ?? DEFAULTS.rules.stop_loss_bps),
        take_profit_bps: Number(rules.take_profit_bps ?? DEFAULTS.rules.take_profit_bps),
        max_hold_seconds: Number(rules.max_hold_seconds ?? DEFAULTS.rules.max_hold_seconds),
        cooldown_seconds: Number(rules.cooldown_seconds ?? DEFAULTS.rules.cooldown_seconds),
        max_spread_bps: Number(rules.max_spread_bps ?? DEFAULTS.rules.max_spread_bps)
      },
      confirmed: false
    };
  }

  function open(template, onSave) {
    const body = stripBody(template || {});
    const dialog = el("dialog", { class: "strategy-editor", "aria-labelledby": "strategy-editor-title" });
    dialog.innerHTML = `
      <form class="strategy-editor-form" novalidate>
        <header class="strategy-editor-head"><div><p class="strategy-kicker">ПАСПОРТ УЧАСНИКА · PAPER</p><h2 id="strategy-editor-title">Стратегія арени</h2></div><button type="button" class="strategy-close" aria-label="Закрити редактор">×</button></header>
        <p class="strategy-intro">Опиши задум і зафіксуй правила, які може виконати симулятор. Нове збереження створює незмінну версію стратегії.</p>
        <div class="strategy-error" role="alert" aria-live="polite" hidden></div>
        <section class="strategy-section"><h3>Ідея та автор</h3><div class="strategy-grid strategy-grid-two">
          <div data-field="name"></div><div data-field="author-name"></div><div data-field="author-kind"></div><div data-field="author-model"></div><div data-field="submitted-at"></div>
          <div data-field="description" class="strategy-span-two"></div><div data-field="author-prompt" class="strategy-span-two"></div>
          <div data-field="author-attempts"></div><div data-field="author-edited"></div><div data-field="original" class="strategy-span-two"></div>
        </div><p class="strategy-help">Для моделі вкажи точну назву моделі та збережи її первинну відповідь. Не подавай чужий рецепт від імені автора.</p></section>
        <section class="strategy-section"><h3>Вхід</h3><div class="strategy-grid strategy-grid-two"><div data-field="side"></div><div data-field="entry-mode"></div></div><div class="strategy-condition-list" data-conditions="entry"></div><button type="button" class="strategy-add" data-add="entry">＋ Додати умову входу</button><p class="strategy-help">Вхід вимагає 1–6 умов. У режимі «усі» мають справдитися всі; у режимі «хоча б одна» досить однієї істинної умови, але дані для кожної умови мають бути відомі.</p></section>
        <section class="strategy-section"><h3>Вихід і межі</h3><div class="strategy-grid strategy-grid-two"><div data-field="exit-mode"></div><div data-field="size"></div><div data-field="max-entries"></div><div data-field="add-after"></div><div data-field="stop"></div><div data-field="take"></div><div data-field="hold"></div><div data-field="cooldown"></div><div data-field="spread"></div></div><div class="strategy-condition-list" data-conditions="exit"></div><button type="button" class="strategy-add" data-add="exit">＋ Додати умову виходу</button><p class="strategy-help">Вихідні умови необов’язкові: без них позицію закриє stop, ціль або ліміт часу. У режимі «хоча б одна» всі ознаки все одно мають бути відомі. Докупівля можлива лише після руху на користь позиції від середньої ціни входу за модельною ціною закриття й за кількості входів понад один.</p></section>
        <section class="strategy-section strategy-cost-note"><h3>Витрати сезону</h3><p>У PAPER розрахунку враховуються припущення 5 б.п. комісії та 2 б.п. несприятливого прослизання за кожну сторону, спред і модельний funding: остання спостережена індикативна ставка та попередня середня ціна в момент планового funding. Це не фактично списаний OKX funding. 100 б.п. = 1%.</p></section>
        <label class="strategy-confirm"><input type="checkbox" name="confirmed"><span>Підтверджую: формальні правила вище точно відтворюють рецепт автора. Непідтримані вимоги з тексту не виконуються.</span></label>
        <footer class="strategy-actions"><button type="button" class="strategy-cancel">Скасувати</button><button type="submit" class="strategy-save">Зберегти паспорт</button></footer>
      </form>`;

    const form = dialog.querySelector("form");
    const setField = (key, label, control, hint) => {
      const slot = dialog.querySelector(`[data-field="${key}"]`);
      slot.append(field(label, control, hint));
      return control;
    };
    const fields = {
      name: setField("name", "Ім’я стратегії", input("text", body.name, { maxlength: "60", required: "", autocomplete: "off" })),
      authorName: setField("author-name", "Ім’я автора / персонажа", input("text", body.author.name, { maxlength: "60", required: "", autocomplete: "off" })),
      authorKind: setField("author-kind", "Тип автора", select([["human", "Людина"], ["model", "Модель ШІ"], ["example", "Навчальний приклад"]], body.author.kind)),
      authorModel: setField("author-model", "Модель / версія (якщо є)", input("text", body.author.model, { maxlength: "150", autocomplete: "off" }), "Точна назва з джерела; для людини можна залишити порожнім."),
      description: setField("description", "Короткий опис задуму", el("textarea", { maxlength: "1000", rows: "3", required: "" }), "Контекст і сенс стратегії для читача."),
      prompt: setField("author-prompt", "Спільний запит до автора / моделі", el("textarea", { maxlength: "8000", rows: "3" }), "Збережи однаковий запит для порівнюваних відповідей."),
      submittedAt: setField("submitted-at", "Дата отримання відповіді", input("text", body.author.submitted_at, { maxlength: "60", autocomplete: "off" }), "Збережи дату з джерела; обов’язково для моделі."),
      attempts: setField("author-attempts", "Кількість спроб автора", input("number", body.author.attempts, { min: "1", max: "1000", step: "1", required: "" })),
      edited: setField("author-edited", "Хто формалізував / редагував", input("text", body.author.edited_by, { maxlength: "300", autocomplete: "off" })),
      original: setField("original", "Оригінальний рецепт автора", el("textarea", { maxlength: "16000", rows: "5", required: "" }), "Збережи точну отриману відповідь або текст людини."),
      side: setField("side", "Напрямок першої ліги", select([["long", "Long · очікує зростання"], ["short", "Short · очікує зниження"]], body.rules.side)),
      entryMode: setField("entry-mode", "Умови входу", select([["all", "Усі умови"], ["any", "Хоча б одна"]], body.rules.entry.mode)),
      exitMode: setField("exit-mode", "Умови виходу", select([["any", "Хоча б одна"], ["all", "Усі умови"]], body.rules.exit.mode)),
      size: setField("size", "Обсяг кожного входу, % стартового капіталу", input("number", body.rules.size_pct, { min: "5", max: "30", step: "0.1", required: "" }), "Дозволено 5–30%."),
      maxEntries: setField("max-entries", "Максимум входів", input("number", body.rules.max_entries, { min: "1", max: "3", step: "1", required: "" }), "Від 1 до 3 входів."),
      addAfter: setField("add-after", "Докупівля після руху на користь позиції, б.п.", input("number", body.rules.add_after_bps, { min: "20", max: "500", step: "1", required: "" }), "20–500 б.п. від середньої ціни входу за модельною ціною закриття; 100 б.п. = 1%. Діє лише коли дозволено ще один вхід."),
      stop: setField("stop", "Stop loss, б.п.", input("number", body.rules.stop_loss_bps, { min: "20", max: "500", step: "1", required: "" })),
      take: setField("take", "Take profit, б.п.", input("number", body.rules.take_profit_bps, { min: "40", max: "1000", step: "1", required: "" })),
      hold: setField("hold", "Максимальний час позиції, секунд", input("number", body.rules.max_hold_seconds, { min: "30", max: "86400", step: "1", required: "" })),
      cooldown: setField("cooldown", "Пауза після закриття, секунд", input("number", body.rules.cooldown_seconds, { min: "15", max: "600", step: "1", required: "" })),
      spread: setField("spread", "Не входити за спреду понад, б.п.", input("number", body.rules.max_spread_bps, { min: "0.1", max: "20", step: "0.1", required: "" }))
    };
    fields.description.value = body.description;
    fields.prompt.value = body.author.prompt;
    fields.original.value = body.original_text;
    const conditionLists = { entry: dialog.querySelector('[data-conditions="entry"]'), exit: dialog.querySelector('[data-conditions="exit"]') };
    const modes = { entry: fields.entryMode, exit: fields.exitMode };
    const initialConditions = { entry: body.rules.entry.conditions, exit: body.rules.exit.conditions };

    function addCondition(group, data) {
      const list = conditionLists[group];
      if (list.children.length >= 6) return;
      const row = makeCondition(data, () => { row.remove(); }, list.children.length > 0 || group === "exit");
      list.append(row);
    }
    ["entry", "exit"].forEach((group) => initialConditions[group].forEach((item) => addCondition(group, item)));
    dialog.querySelectorAll("[data-add]").forEach((button) => button.addEventListener("click", () => addCondition(button.dataset.add, null)));

    const error = dialog.querySelector(".strategy-error");
    const showError = (message) => { error.textContent = message; error.hidden = !message; };
    const syncKind = () => {
      const isModel = fields.authorKind.value === "model";
      fields.authorModel.required = isModel;
      fields.prompt.required = isModel;
      fields.submittedAt.required = isModel;
      fields.original.required = true;
      fields.authorModel.closest(".strategy-field").classList.toggle("strategy-required", isModel);
      fields.prompt.closest(".strategy-field").classList.toggle("strategy-required", isModel);
      fields.submittedAt.closest(".strategy-field").classList.toggle("strategy-required", isModel);
    };
    fields.authorKind.addEventListener("change", syncKind);
    syncKind();

    function readConditions(group) {
      return Array.from(conditionLists[group].children).map((row) => ({
        feature: row.children[0].value,
        op: row.children[1].value,
        value: Number(row.children[2].value)
      }));
    }
    function collect() {
      return {
        name: fields.name.value.trim(),
        author: {
          name: fields.authorName.value.trim(), kind: fields.authorKind.value,
          model: fields.authorModel.value.trim(),
          submitted_at: fields.submittedAt.value.trim(),
          prompt: fields.prompt.value.trim(), attempts: Number(fields.attempts.value), edited_by: fields.edited.value.trim()
        },
        description: fields.description.value.trim(), original_text: fields.original.value.trim(),
        rules: {
          side: fields.side.value,
          entry: { mode: modes.entry.value, conditions: readConditions("entry") },
          exit: { mode: modes.exit.value, conditions: readConditions("exit") },
          size_pct: Number(fields.size.value), max_entries: Number(fields.maxEntries.value),
          add_after_bps: Number(fields.addAfter.value), stop_loss_bps: Number(fields.stop.value),
          take_profit_bps: Number(fields.take.value), max_hold_seconds: Number(fields.hold.value),
          cooldown_seconds: Number(fields.cooldown.value), max_spread_bps: Number(fields.spread.value)
        },
        confirmed: dialog.querySelector('[name="confirmed"]').checked
      };
    }
    function validate(strategy) {
      if (!strategy.name || !strategy.author.name || !strategy.description || !strategy.original_text) return "Заповни назву стратегії, автора, опис і оригінальний рецепт.";
      if (strategy.author.kind === "model" && (!strategy.author.model || !strategy.author.prompt || !strategy.author.submitted_at)) return "Для автора-моделі потрібні точна назва моделі, дата відповіді та спільний prompt.";
      if (!Number.isInteger(strategy.author.attempts) || strategy.author.attempts < 1 || strategy.author.attempts > 1000) return "Кількість спроб має бути цілим числом від 1 до 1000.";
      if (strategy.rules.entry.conditions.length < 1) return "Додай щонайменше одну умову входу.";
      for (const group of ["entry", "exit"]) {
        if (strategy.rules[group].conditions.some((c) => !Number.isFinite(c.value) || Math.abs(c.value) > 1e9)) return "Для кожної умови вкажи числове порогове значення в межах ±1 000 000 000.";
      }
      const ranges = [[strategy.rules.size_pct, 5, 30, "Обсяг має бути від 5 до 30%"], [strategy.rules.max_entries, 1, 3, "Кількість входів має бути від 1 до 3"], [strategy.rules.add_after_bps, 20, 500, "Поріг докупівлі має бути 20–500 б.п."], [strategy.rules.stop_loss_bps, 20, 500, "Stop loss має бути 20–500 б.п."], [strategy.rules.take_profit_bps, 40, 1000, "Take profit має бути 40–1000 б.п."], [strategy.rules.max_hold_seconds, 30, 86400, "Час позиції має бути 30–86400 секунд (до 24 годин)"], [strategy.rules.cooldown_seconds, 15, 600, "Пауза має бути 15–600 секунд"], [strategy.rules.max_spread_bps, 0.1, 20, "Максимальний спред має бути 0,1–20 б.п."]];
      const invalid = ranges.find(([value, min, max]) => !Number.isFinite(value) || value < min || value > max);
      if (invalid) return invalid[3];
      if (![strategy.rules.max_entries, strategy.rules.max_hold_seconds, strategy.rules.cooldown_seconds, strategy.author.attempts].every(Number.isInteger)) return "Кількість входів, часи та спроби мають бути цілими числами.";
      if (strategy.rules.take_profit_bps <= 14 + strategy.rules.max_spread_bps) return "Take profit має перевищувати модельні витрати кола.";
      if (!strategy.confirmed) return "Підтверди, що формальні умови точно відповідають рецепту автора.";
      return "";
    }

    const returnFocus = document.activeElement;
    return new Promise((resolve, reject) => {
      let settled = false;
      const close = (value) => {
        if (settled) return;
        settled = true;
        dialog.close(); dialog.remove();
        if (returnFocus && typeof returnFocus.focus === "function" && returnFocus.isConnected) returnFocus.focus();
        value instanceof Error ? reject(value) : resolve(value);
      };
      dialog.querySelector(".strategy-close").addEventListener("click", () => close(null));
      dialog.querySelector(".strategy-cancel").addEventListener("click", () => close(null));
      dialog.addEventListener("cancel", (event) => { event.preventDefault(); close(null); });
      dialog.addEventListener("click", (event) => { if (event.target === dialog) close(null); });
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        showError("");
        const strategy = collect();
        const issue = validate(strategy);
        if (issue) { showError(issue); return; }
        const save = dialog.querySelector(".strategy-save");
        save.disabled = true; save.textContent = "Збереження…";
        try {
          await onSave(strategy);
          close(strategy);
        } catch (err) {
          showError(typeof err === "string" ? err : err?.message || "Не вдалося зберегти паспорт. Перевір з’єднання та спробуй ще раз.");
          save.disabled = false; save.textContent = "Зберегти паспорт";
        }
      });
      document.body.append(dialog);
      dialog.showModal();
      fields.name.focus();
    });
  }

  window.ArenaStrategyEditor = { open };
})();
