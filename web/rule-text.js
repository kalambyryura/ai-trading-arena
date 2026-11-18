"use strict";
/* Recipe rules in words (B-06): the frozen rules of one version as plain Ukrainian sentences for the strategy card.
   Pure model plus an optional render(document, ...): no requests, storage or controls, and every string reaches the page
   through textContent. Text comes only from the rules and the fixed phrases below; market feature names are the same as in
   bot-waiting.js. The engine executes the rules; this is a description, not a forecast. */
((root, factory) => {
  const node = typeof module === "object" && module.exports;
  const api = factory(node ? require("./bot-waiting.js") : root.ArenaBotWaiting);
  if (node) module.exports = api;
  else root.ArenaRuleText = api;
})(typeof window === "undefined" ? {} : window, Waiting => {
  const POSITION = Object.freeze({
    position_age_seconds: {label: "Вік позиції", unit: "с", digits: 0},
    gross_gain_bps: {label: "Прибуток позиції до витрат", unit: "bps", digits: 1},
    net_gain_bps: {label: "Прибуток позиції після витрат", unit: "bps", digits: 1},
    entry_cost_hurdle_bps: {label: "Поріг беззбитковості (витрати входу й виходу)", unit: "bps", digits: 1},
    entries: {label: "Входів у цій позиції", unit: "", digits: 0}});
  // Bounded features only: a leaf outside these values never holds (or always holds).
  const RANGES = Object.freeze({utc_hour: [0, 23], utc_weekday: [0, 6], rsi_14: [0, 100], range_position_20: [0, 100]});
  const OPS = Object.freeze({gt: ">", gte: "≥", lt: "<", lte: "≤"});
  const INVERT = Object.freeze({gt: "lte", gte: "lt", lt: "gte", lte: "gt"});
  const SIDES = Object.freeze({long: "LONG (купівля)", short: "SHORT (продаж)"});
  // Schema 4 (SDK v3) features carry a timeframe and a window: {name, tf, n}.
  const V4 = Object.freeze({
    return_bps: (tf, n) => "Зміна ціни за " + n + " × " + tf,
    rms_bar_bps: (tf, n) => "Типовий рух бару " + tf + " (RMS за " + n + ")",
    sma_gap_bps: (tf, n) => "Відстань ціни від SMA " + n + " барів " + tf,
    funding_settled_bps: (tf, n) => "Середній фандинг " + n + " розрахунків (на 8 год)",
    premium_bps: (tf, n) => "Премія свопу до індексу, середня за " + n + " × " + tf,
    premium_change_bps: (tf, n) => "Зміна премії за " + n + " × " + tf,
    basis_mark_bps: (tf, n) => "Базис mark до індексу, середній за " + n + " × " + tf,
    realized_vol_bps: (tf, n) => "Волатильність (std лог-дохідностей) за " + n + " × " + tf,
    dist_high_bps: (tf, n) => "Відстань ціни до максимуму " + n + " барів " + tf,
    dist_low_bps: (tf, n) => "Відстань ціни до мінімуму " + n + " барів " + tf,
    utc_minute_of_day: () => "Хвилина доби UTC",
    utc_weekday: () => "День тижня UTC (0 = понеділок)",
    us_session_minute: () => "Хвилина сесії США (від 09:30 за Нью-Йорком; −1 поза сесією)",
    return_since_utc_day_open_bps: () => "Зміна ціни від початку доби UTC",
    return_since_utc_week_open_bps: () => "Зміна ціни від початку тижня UTC",
    return_since_utc_hour_bps: (tf, n) => "Зміна ціни від " + n + ":00 UTC"});
  const V4_PLAIN = new Set(["utc_minute_of_day", "utc_weekday", "us_session_minute"]);
  const NOTE = "Це правила рецепта словами, а не прогноз. Виконує їх код рушія: стоп, ціль і ліміт часу діють незалежно від Jev.";
  const MISSING = "Правила цієї версії недоступні для показу.";
  const MINUS = "−";

  const isObject = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const info = name => POSITION[name] || (Waiting && typeof Waiting.feature === "function" ? Waiting.feature(name)
    : {label: String(name || "ознака").replace(/_/g, " "), unit: "", digits: 2});
  function num(value, digits = 2) {
    if (!finite(value)) return "—";
    const text = new Intl.NumberFormat("uk-UA", {maximumFractionDigits: digits}).format(Math.abs(value));
    return (value < 0 ? MINUS : "") + text;
  }
  const percentOfBps = value => num(value / 100, 2) + " %";
  function amount(value, unit, digits) {
    if (unit === "bps") return num(value, digits) + " bps (" + percentOfBps(value) + ")";
    if (unit === "×" || unit === "σ") return num(value, digits) + unit;
    return num(value, digits) + (unit ? " " + unit : "");
  }
  function duration(seconds) {
    if (!finite(seconds) || seconds < 0) return "—";
    const s = Math.round(seconds), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), rest = s % 60;
    const parts = [];
    if (h) parts.push(h + " год");
    if (m) parts.push(m + " хв");
    if (rest || !parts.length) parts.push(rest + " с");
    return parts.join(" ");
  }

  /* never/always for bounded features; null when the leaf can go either way or the feature is unbounded. */
  function certainty(feature, op, value) {
    const range = RANGES[feature];
    if (!range || !finite(value)) return null;
    const [low, high] = range;
    const never = {gt: value >= high, gte: value > high, lt: value <= low, lte: value < low}[op];
    const always = {gt: value < low, gte: value <= low, lt: value > high, lte: value >= high}[op];
    return never ? "never" : always ? "always" : null;
  }

  const v4Label = spec => isObject(spec) && V4[spec.name] ? V4[spec.name](String(spec.tf), num(spec.n, 0)) : "ознака";

  function v4Leaf(leaf, op) {
    const plain = isObject(leaf.feature) && V4_PLAIN.has(leaf.feature.name);
    const right = isObject(leaf.ref) ? num(leaf.mult, 2) + " × " + v4Label(leaf.ref) : plain ? num(leaf.value, 0) : amount(leaf.value, "bps", 1);
    return {kind: "leaf", text: v4Label(leaf.feature) + " " + (OPS[op] || String(op)) + " " + right,
      feature: isObject(leaf.feature) ? String(leaf.feature.name) : null, certainty: null};
  }

  function leafNode(leaf, negated) {
    const op = negated ? INVERT[leaf.op] : leaf.op;
    if (isObject(leaf.feature)) return v4Leaf(leaf, op);
    const left = info(leaf.feature);
    const right = typeof leaf.ref === "string" ? info(leaf.ref).label : amount(leaf.value, left.unit, left.digits);
    const sure = typeof leaf.ref === "string" ? null : certainty(leaf.feature, op, leaf.value);
    const text = left.label + " " + (OPS[op] || String(op)) + " " + right
      + (sure === "never" ? " — ніколи не виконується" : sure === "always" ? " — виконується завжди" : "");
    return {kind: "leaf", text, feature: String(leaf.feature), certainty: sure};
  }

  /* Predicate tree -> nested {kind, text, children}. A single-member group collapses; NOT of a leaf flips its operator. */
  function tree(node, negated = false) {
    if (!isObject(node)) return {kind: "leaf", text: "—", feature: null, certainty: null};
    if (isObject(node.not)) return tree(node.not, !negated);
    const key = Array.isArray(node.all) ? "all" : Array.isArray(node.any) ? "any" : null;
    if (key) {
      if (node[key].length === 1) return tree(node[key][0], negated);
      const children = node[key].map(child => tree(child, false));
      const group = {kind: key, text: key === "all" ? "Усі умови разом:" : "Будь-яка з умов:", children};
      return negated ? {kind: "not", text: "Ця група умов не виконується:", children: [group]} : group;
    }
    if ("feature" in node) return leafNode(node, negated);
    return {kind: "leaf", text: "—", feature: null, certainty: null};
  }

  /* One sentence for a tree: "A і (B або C)". */
  function sentence(item) {
    if (item.kind === "leaf") return item.text;
    const inner = item.children.map(child => child.kind === "leaf" ? child.text : "(" + sentence(child) + ")");
    if (item.kind === "not") return "не " + inner[0];
    return inner.join(item.kind === "all" ? " і " : " або ");
  }

  const line = text => ({kind: "line", text});
  const v1Group = group => isObject(group) && Array.isArray(group.conditions) && group.conditions.length
    ? (group.conditions.length === 1 ? tree(group.conditions[0]) : tree({[group.mode === "any" ? "any" : "all"]: group.conditions}))
    : null;

  function entrySections(rules, v2) {
    if (!v2) {
      const when = v1Group(rules.entry);
      return [{key: "entry", title: "Коли входить · " + (SIDES[rules.side] || String(rules.side)),
        items: when ? [when] : [line("Умов входу немає.")]}];
    }
    return Object.keys(SIDES).filter(side => isObject(rules.entry[side])).map(side => {
      const branch = rules.entry[side], items = [];
      if (isObject(branch.setup)) {
        items.push(line("Спершу має статися (setup):"), tree(branch.setup));
        items.push(line("Потім протягом " + num(branch.expiry_bars, 0) + " наступних хвилинних свічок — підтвердження:"), tree(branch.confirm));
        if (isObject(branch.invalidate)) items.push(line("Очікування скасовується, якщо:"), tree(branch.invalidate));
      } else items.push(tree(branch.confirm));
      return {key: "entry_" + side, title: "Коли входить · " + SIDES[side], items};
    });
  }

  const v4Branches = rules => rules.side === "both"
    ? Object.keys(SIDES).filter(s => isObject(rules.entry[s]) && isObject(rules.entry[s].when)).map(s => [s, rules.entry[s].when])
    : [[rules.side, rules.entry.when]];
  const isV4 = rules => isObject(rules.entry.when) || (rules.side === "both" && isObject(rules.entry.long) && isObject(rules.entry.long.when));

  function describeV4(rules) {
    const gates = [line("Рішення — раз на кожен новий закритий 1m-бар, через 5 с після його закриття; невідомі дані блокують вхід."),
      line("Вхід taker на наступній котировці, після повторної перевірки умови."),
      line("Спред не більший за " + amount(rules.max_spread_bps, "bps", 2) + ".")];
    if (finite(rules.cooldown_seconds)) gates.push(line("Після закриття угоди чекає щонайменше " + duration(rules.cooldown_seconds) + ", перш ніж увійти знову."));
    const exits = [line("Стоп: закриває, якщо ціна пішла проти позиції на " + percentOfBps(rules.stop_loss_bps) + " від входу; діє з моменту входу."),
      line("Ціль: закриває, якщо ціна пішла на користь позиції на " + percentOfBps(rules.take_profit_bps) + " від входу; діє з наступної хвилини після входу."),
      line("Час: якщо не спрацювали ні стоп, ні ціль, закриває через " + duration(rules.max_hold_seconds) + " після входу.")];
    const entries = v4Branches(rules).map(([s, when]) => ({key: rules.side === "both" ? "entry_" + s : "entry",
      title: "Коли входить · " + (SIDES[s] || String(s)), items: [tree(when)]}));
    if (rules.side === "both") gates.unshift(line("Обидва напрями, одна позиція: вхід лише тоді, коли виконується рівно одна гілка; обидві разом або невідомі дані — без входу."));
    return {schema: 4, side: rules.side, summary: summary(rules), note: NOTE, sections: [
      ...entries,
      {key: "gates", title: "Загальні умови", items: gates},
      {key: "exit", title: "Як виходить", items: exits},
      {key: "size", title: "Розмір", items: [line("На вхід — " + num(rules.size_pct, 0) + " % бюджету бою."), line("Один вхід у позицію, без докупівлі.")]}]};
  }

  function describe(rules) {
    if (!isObject(rules) || !isObject(rules.entry)) return null;
    if (isV4(rules)) return describeV4(rules);
    const v2 = !("conditions" in rules.entry) && !("mode" in rules.entry);
    const sections = [];
    if (rules.side === "both") sections.push({key: "side", title: "Сторона", items: [line(
      "Може відкрити LONG або SHORT, але лише одну позицію за раз: напрям визначає гілка, умови якої виконались.")]});
    sections.push(...entrySections(rules, v2));
    const common = [line("Рішення ухвалюються за завершеними хвилинними свічками; невідомі дані блокують вхід."),
      line("Спред не більший за " + amount(rules.max_spread_bps, "bps", 2) + ".")];
    if (finite(rules.cooldown_seconds)) common.push(line("Після закриття угоди чекає щонайменше " + duration(rules.cooldown_seconds) + ", перш ніж увійти знову."));
    sections.push({key: "gates", title: "Загальні умови", items: common});

    const exits = [line("Стоп: закриває, якщо ціна пішла проти позиції на " + percentOfBps(rules.stop_loss_bps) + " від входу."),
      line("Ціль: закриває, якщо ціна пішла на користь позиції на " + percentOfBps(rules.take_profit_bps) + " від входу.")];
    exits.push(line(rules.max_hold_seconds === null ? "Ліміту часу утримання немає: позиція закривається стопом, ціллю або умовами нижче."
      : "Час: якщо не спрацювали ні стоп, ні ціль, закриває через " + duration(rules.max_hold_seconds) + " після входу."));
    const exitTree = v2 ? (isObject(rules.exit) ? tree(rules.exit) : null) : v1Group(rules.exit);
    if (exitTree) exits.push(line("Достроково закриває, коли:"), exitTree);
    if (v2 && isObject(rules.reduce_when) && finite(rules.reduce_pct))
      exits.push(line("Скорочує позицію на " + num(rules.reduce_pct, 0) + " %, коли:"), tree(rules.reduce_when));
    if (v2 && isObject(rules.cost_checkpoint) && isObject(rules.cost_checkpoint.when))
      exits.push(line("Через " + duration(rules.cost_checkpoint.after_seconds) + " після входу один раз перевіряє і закриває позицію, якщо:"),
        tree(rules.cost_checkpoint.when));
    sections.push({key: "exit", title: "Як виходить", items: exits});

    const size = [line("На вхід — " + num(rules.size_pct, 0) + " % бюджету бою.")];
    if (finite(rules.max_entries) && rules.max_entries > 1) {
      size.push(line("Докупівля: до " + num(rules.max_entries, 0) + " входів у позиції, лише коли позиція вже в плюсі щонайменше на "
        + percentOfBps(rules.add_after_bps) + " і ще не досягла цілі" + (v2 ? "." : "; умови входу мають виконатися знову.")));
      if (v2 && isObject(rules.add_when)) size.push(line("Умова докупівлі автора:"), tree(rules.add_when));
    } else size.push(line("Один вхід у позицію, без докупівлі."));
    sections.push({key: "size", title: "Розмір", items: size});
    return {schema: v2 ? 2 : 1, side: rules.side, sections, summary: summary(rules), note: NOTE};
  }

  function summary(rules) {
    if (!isObject(rules) || !isObject(rules.entry)) return MISSING;
    const v2 = !("conditions" in rules.entry) && !("mode" in rules.entry);
    const side = rules.side === "both" ? "LONG або SHORT" : (rules.side || "").toUpperCase();
    let entry;
    if (isV4(rules)) entry = v4Branches(rules).map(([s, when]) => (rules.side === "both" ? s.toUpperCase() + ": " : "") + sentence(tree(when))).join("; ");
    else if (v2) entry = Object.keys(SIDES).filter(s => isObject(rules.entry[s])).map(s => {
      const branch = rules.entry[s], confirm = sentence(tree(branch.confirm));
      return (rules.side === "both" ? s.toUpperCase() + ": " : "") + (isObject(branch.setup) ? sentence(tree(branch.setup)) + ", потім " + confirm : confirm);
    }).join("; ");
    else { const when = v1Group(rules.entry); entry = when ? sentence(when) : "—"; }
    const hold = rules.max_hold_seconds === null ? "без ліміту часу" : "до " + duration(rules.max_hold_seconds);
    return side + " · вхід: " + entry + " · стоп " + percentOfBps(rules.stop_loss_bps) + " · ціль " + percentOfBps(rules.take_profit_bps) + " · " + hold;
  }

  /* DOM for a description; strings only through textContent. Returns null without a description. */
  function render(doc, described, options = {}) {
    const level = Number.isInteger(options.headingLevel) && options.headingLevel >= 2 && options.headingLevel <= 5 ? options.headingLevel : 3;
    const make = (tag, className, text) => {
      const el = doc.createElement(tag);
      if (className) el.className = className;
      if (text !== undefined) el.textContent = text;
      return el;
    };
    if (!isObject(described) || !Array.isArray(described.sections)) return make("p", "rule-text-missing", MISSING);
    const root = make("section", "rule-text");
    const list = items => {
      const ul = make("ul", "rule-text-list");
      for (const item of items) {
        const li = make("li", "rule-text-" + item.kind, item.text);
        if (item.certainty) li.className += " rule-text-" + item.certainty;
        if (Array.isArray(item.children) && item.children.length) li.append(list(item.children));
        ul.append(li);
      }
      return ul;
    };
    for (const section of described.sections) {
      root.append(make("h" + level, "rule-text-title", section.title));
      root.append(list(section.items));
    }
    root.append(make("p", "rule-text-note", described.note));
    return root;
  }

  return {describe, summary, render, tree, sentence, duration, NOTE, MISSING};
});
