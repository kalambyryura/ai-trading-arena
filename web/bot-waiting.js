"use strict";
/* What a participant is waiting for, in words, from facts already in the public run (B-05), and the Jev gate (B-01a).
   Pure model: no DOM, requests or controls. Conditions are the recipe's rules, not a price forecast. */
((root, factory) => {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ArenaBotWaiting = api;
})(typeof window === "undefined" ? {} : window, () => {
  // Public names; scale = distance at which the closeness bar is empty (null: no bar for this feature).
  const FEATURES = {
    return_1m_bps:{label:"Зміна ціни за 1 хв",unit:"bps",scale:20,digits:1},
    return_5m_bps:{label:"Зміна ціни за 5 хв",unit:"bps",scale:40,digits:1},
    sma_gap_bps:{label:"Середня за 5 хв відносно середньої за 20 хв",unit:"bps",scale:20,digits:1},
    ema_gap_bps:{label:"Швидка EMA відносно повільної",unit:"bps",scale:20,digits:1},
    rsi_14:{label:"RSI за 14 хв",unit:"",scale:50,digits:1},
    range_position_20:{label:"Місце ціни в діапазоні 20 хв (0 — дно, 100 — верх)",unit:"",scale:50,digits:1},
    volume_ratio_5:{label:"Обсяг 5 хв відносно попередніх 5 хв",unit:"×",scale:1,digits:2},
    volume_ratio_1:{label:"Обсяг останньої хвилини відносно середнього",unit:"×",scale:1,digits:2},
    spread_bps:{label:"Спред",unit:"bps",scale:5,digits:2},
    atr_14_bps:{label:"Середній розмах хвилини (ATR 14)",unit:"bps",scale:10,digits:1},
    body_bps:{label:"Тіло останньої свічки",unit:"bps",scale:10,digits:1},
    upper_wick_bps:{label:"Верхня тінь останньої свічки",unit:"bps",scale:10,digits:1},
    lower_wick_bps:{label:"Нижня тінь останньої свічки",unit:"bps",scale:10,digits:1},
    bollinger_z_20:{label:"Відхилення від середньої за 20 хв (z Боллінджера)",unit:"σ",scale:2,digits:2},
    utc_hour:{label:"Година UTC",unit:"",scale:null,digits:0},
    utc_weekday:{label:"День тижня UTC (0 — понеділок)",unit:"",scale:null,digits:0},
    funding_rate_bps:{label:"Ставка funding",unit:"bps",scale:1,digits:2},
    minutes_to_funding:{label:"Хвилин до funding",unit:"хв",scale:60,digits:0},
    total_entries:{label:"Входів цього учасника в бою",unit:"",scale:null,digits:0}};
  const OPS = {gt:">", gte:"≥", lt:"<", lte:"≤"};
  const INVERT = {gt:"lte", gte:"lt", lt:"gte", lte:"gt"};
  const SIDES = {long:"LONG", short:"SHORT"};
  const TRACE = new Set(["setup_waiting","setup_unknown","setup_armed","setup_waiting_confirmation","setup_confirmed","setup_expired",
    "setup_invalidated","setup_unknown_reset","confirmation_expired","setup_gap_reset"]);
  const RESETS = {setup_expired:"Попередній setup прострочено — чекаємо новий.", setup_invalidated:"Попередній setup скасовано умовою «не входить».",
    setup_unknown_reset:"Попередній setup скинуто: бракувало даних.", confirmation_expired:"Підтвердження не використано на своїй хвилині — setup скинуто.",
    setup_gap_reset:"Пропуск хвилинних даних — setup скинуто."};
  const NOTE = "Це умови рецепта, а не прогноз ціни.";
  const finite = value => typeof value === "number" && Number.isFinite(value) ? value : null;
  const feature = name => FEATURES[name] || {label:String(name || "ознака").replace(/_/g, " "), unit:"", scale:null, digits:2};
  function amount(value, info, signed = true) {
    const n = finite(value);
    if (n === null) return "—";
    const text = new Intl.NumberFormat("uk-UA", {maximumFractionDigits:info.digits}).format(Math.abs(n));
    return (n < 0 && signed ? "−" : "") + text + (info.unit === "×" || info.unit === "σ" ? info.unit : info.unit ? " " + info.unit : "");
  }
  function leaves(node, path = "", negated = false) {
    if (!node || typeof node !== "object") return [];
    if (Array.isArray(node.all) || Array.isArray(node.any)) {
      const key = Array.isArray(node.all) ? "all" : "any";
      return node[key].flatMap((child, index) => leaves(child, `${path}/${key}/${index}`, negated));
    }
    if (node.not) return leaves(node.not, path + "/not", !negated);
    return [{path, feature:node.feature, op:node.op, value:node.value, ref:node.ref, negated}];
  }
  function treeMode(node) {
    if (!node || typeof node !== "object") return "all";
    const key = Array.isArray(node.all) ? "all" : Array.isArray(node.any) ? "any" : null;
    if (!key) return node.not ? "tree" : "all";
    return node[key].every(child => child && !child.all && !child.any && !child.not) ? key : "tree";
  }
  // A leaf under an odd number of `not` is shown as its effective condition: not(x > 5) reads as x ≤ 5.
  function condition(leaf, check) {
    const op = leaf.negated ? INVERT[leaf.op] || leaf.op : leaf.op, info = feature(leaf.feature);
    const actual = check ? finite(check.actual) : null;
    const threshold = check && finite(check.expected) !== null ? finite(check.expected) : leaf.ref ? null : finite(leaf.value);
    const passed = check ? check.passed : undefined;
    const status = passed === undefined ? "pending" : passed === null ? "unknown" :
      (leaf.negated ? passed === false : passed === true) ? "met" : "not_met";
    let gap = null, direction = null, closeness = status === "met" ? 1 : null;
    if (status === "not_met" && actual !== null && threshold !== null) {
      gap = Math.abs(actual - threshold);
      direction = op === "gt" || op === "gte" ? "up" : "down";
      closeness = info.scale ? Math.max(0, Math.min(1, 1 - gap / info.scale)) : null;
    }
    const rhs = leaf.ref ? feature(leaf.ref).label : amount(threshold, info);
    return {feature:leaf.feature, label:info.label, op, opText:OPS[op] || op, threshold, thresholdFeature:leaf.ref || null,
      actual, status, gap, direction, closeness, text:`${info.label} ${OPS[op] || op} ${rhs}`,
      now:status === "unknown" ? "невідомо" : status === "pending" ? "ще не перевірено" : "зараз " + amount(actual, info),
      need:status !== "not_met" || gap === null ? null : gap === 0 ? (direction === "up" ? "треба вище за поріг" : "треба нижче за поріг") :
        `ще на ${amount(gap, info, false)} ${direction === "up" ? "вище" : "нижче"}`};
  }
  function asVeto(c) {
    const status = c.status === "met" ? "veto_hit" : c.status === "not_met" ? "veto_clear" : c.status;
    return {...c, status, need:null, closeness:null,
      now:status === "veto_hit" ? "блокує вхід · " + c.now : status === "veto_clear" ? "не заважає · " + c.now : c.now};
  }
  function splitGates(checks) {
    const n = checks.length;
    if (n >= 2 && checks[n - 2]?.feature === "spread_bps" && checks[n - 1]?.feature === "total_entries") return {own:checks.slice(0, n - 2), gates:checks.slice(n - 2)};
    return {own:checks, gates:[]};
  }
  const latest = (events, test) => { for (let i = events.length - 1; i >= 0; i -= 1) if (test(events[i])) return events[i]; return null; };
  const matches = (leaf, check) => check && check.feature === leaf.feature && check.op === leaf.op;
  function attach(list, checks) {
    const fresh = checks.length === list.length && list.every((leaf, index) => matches(leaf, checks[index]));
    return list.map((leaf, index) => condition(leaf, fresh ? checks[index] : undefined));
  }
  function v1(rules, checks) {
    const entry = rules.entry || {}, list = (entry.conditions || []).map((c, i) => ({path:String(i), feature:c.feature, op:c.op, value:c.value, negated:false}));
    return {groups:[{side:rules.side || null, kind:"entry", title:"Умови входу" + (SIDES[rules.side] ? " · " + SIDES[rules.side] : ""),
      mode:entry.mode === "any" ? "any" : "all", conditions:attach(list, checks)}], stages:[]};
  }
  function v2(rules, checks, events) {
    const groups = [], stages = [];
    for (const side of ["long", "short"]) {
      const plan = rules.entry?.[side];
      if (!plan) continue;
      const confirm = leaves(plan.confirm), invalid = leaves(plan.invalidate), setup = plan.setup == null ? null : leaves(plan.setup);
      const veto = values => invalid.length ? [{side, kind:"veto", title:"Не входить, якщо · " + SIDES[side], mode:treeMode(plan.invalidate), conditions:values.map(asVeto)}] : [];
      if (!setup) {
        const own = checks.filter(check => check.side === side);
        groups.push({side, kind:"entry", title:"Умови входу · " + SIDES[side], mode:treeMode(plan.confirm), conditions:attach(confirm, own.slice(0, confirm.length))},
          ...veto(attach(invalid, own.slice(confirm.length))));
        continue;
      }
      const trace = latest(events, e => TRACE.has(e?.code) && e.data?.side === side), phase = trace?.data?.after || "IDLE";
      const traced = Array.isArray(trace?.data?.checks) ? trace.data.checks : [];
      if (phase === "ARMED") {
        const armed = latest(events, e => e?.code === "setup_armed" && e.data?.side === side), bar = finite(armed?.data?.bar_ms);
        const waiting = trace.code === "setup_waiting_confirmation";
        stages.push({side, step:2, phase, text:"крок 2 з 2: setup спрацював, чекає підтвердження",
          armedAtMs:bar, expiresAtMs:bar === null ? null : bar + Number(plan.expiry_bars || 0) * 60000});
        groups.push({side, kind:"confirm", title:"Підтвердження · " + SIDES[side], mode:treeMode(plan.confirm),
          conditions:attach(confirm, waiting ? traced.slice(invalid.length) : [])}, ...veto(attach(invalid, waiting ? traced.slice(0, invalid.length) : [])));
      } else if (phase === "READY" || phase === "RESERVED") {
        stages.push({side, step:2, phase, text:"підтвердження отримано, вхід на цій хвилині", armedAtMs:null, expiresAtMs:null});
        const own = checks.filter(check => check.side === side);
        groups.push({side, kind:"confirm", title:"Підтвердження · " + SIDES[side], mode:treeMode(plan.confirm), conditions:attach(confirm, own.slice(0, confirm.length))});
      } else {
        const fresh = trace && (trace.code === "setup_waiting" || trace.code === "setup_unknown");
        stages.push({side, step:1, phase:"IDLE", text:"крок 1 з 2: чекає setup", armedAtMs:null, expiresAtMs:null,
          detail:trace && RESETS[trace.code] || null, expiryBars:Number(plan.expiry_bars) || null});
        groups.push({side, kind:"setup", title:"Setup · " + SIDES[side], mode:treeMode(plan.setup), conditions:attach(setup, fresh ? traced : [])});
      }
    }
    return {groups, stages};
  }
  const label = stage => `${SIDES[stage.side]} · ${stage.text}`;
  function tally(groups) {
    const conditions = groups.filter(group => group.kind !== "veto").flatMap(group => group.conditions);
    return {total:conditions.length, met:conditions.filter(c => c.status === "met").length,
      unknown:conditions.filter(c => c.status === "unknown").length, pending:conditions.filter(c => c.status === "pending").length};
  }
  // Genitive after «з N»: «з 1 умови», «з 21 умови», but «з 2 умов», «з 11 умов».
  const of = (n, one, many) => `${n} ${n % 10 === 1 && n % 100 !== 11 ? one : many}`;
  function headline(groups, stages, gates) {
    const count = tally(groups), mode = groups.find(group => group.kind !== "veto")?.mode;
    const blocked = gates.filter(gate => gate.status === "not_met").map(gate => gate.feature === "spread_bps" ? "спред ширший за ліміт" : "вичерпано ліміт входів");
    const vetoed = groups.some(group => group.kind === "veto" && group.conditions.some(c => c.status === "veto_hit"));
    let text = !count.total ? "Умов входу в рецепті не знайдено." : count.pending === count.total ? "Умови ще не перевірені на цьому кадрі." :
      count.unknown ? `Бракує даних для ${count.unknown} з ${of(count.total, "умови", "умов")} — поки вони невідомі, вхід неможливий.` :
      mode === "any" ? `Виконано ${count.met} з ${of(count.total, "умови", "умов")} (достатньо однієї).` :
      mode === "tree" ? `Виконано ${count.met} з ${of(count.total, "перевірки", "перевірок")} (формула з «або / не»).` :
      `Виконано ${count.met} з ${of(count.total, "умови", "умов")} входу.`;
    if (vetoed) text += " Умова «не входить» зараз блокує вхід.";
    if (blocked.length) text += " Обмеження арени: " + blocked.join(", ") + ".";
    return text;
  }
  function waiting(bot, context = {}) {
    const strategy = context.strategy || {}, rules = strategy.rules || {}, run = context.run || {};
    const diagnostic = context.diagnostic || {}, phase = diagnostic.current_phase?.code;
    const events = (Array.isArray(context.events) ? context.events : []).filter(event => event && event.bot_id === bot?.id);
    const position = finite(bot?.position_btc), base = {note:NOTE, groups:[], stages:[], gates:[], checkedAt:null, funnel:null};
    const entry = diagnostic.funnel?.entry;
    if (entry && Number.isSafeInteger(entry.checks)) base.funnel = {checks:entry.checks, signals:Number.isSafeInteger(entry.signals) ? entry.signals : null};
    const state = (key, summary) => ({...base, state:key, summary});
    if (!bot || bot.id === "cash" || bot.strategy_id === "cash" || bot.avatar_kind === "baseline") return state("cash", "Cash не торгує — це точка відліку з результатом 0.");
    if (bot.prepared) return state("prepared", "Учасника ще не запущено.");
    if (position === null) return state("unknown", "Обсяг позиції не підтверджено — умови входу не показуємо, щоб не вводити в оману.");
    if (run.status === "PAUSED" || phase === "paused" || bot._manual_review) return state("paused", "Потрібне відновлення або звірка; нових дій немає.");
    if (position !== 0) return state("position", "Позиція відкрита — нових входів не шукає; вихід визначають правила рецепта.");
    if (bot.stop_requested || ["STOPPED", "DONE"].includes(bot.status) || run.status === "DONE" || phase === "finished") return state("finished", "Завершив — нових входів не буде.");
    if (["DRAINING", "CLOSING"].includes(run.status) || diagnostic.wait_reason?.code === "entry_window_closed") return state("closed", "Вікно нових входів закрито.");
    if (phase === "jev") return state("jev", "Умови виконано — чекає відповіді Jev: це перевірка узгодженості з правилами, не прогноз.");
    if (phase === "pending_model_fill" || phase === "order_reconciliation") return state("pending", "Умови виконано — заявка на вхід виконується.");
    if (strategy.strategy_schema_version === 3 && rules.builtin === "IKIGAI_G1_PAPER_BUILTIN_V1") {
      const g1 = latest(events, e => ["g1_input_unknown", "g1_warmup", "g1_pivot_confirmed", "g1_setup", "g1_entry_signal", "g1_expired", "g1_invalidated"].includes(e.code));
      const text = g1?.code === "g1_input_unknown" ? "IKIGAI: старші дані H1/H4 ще не підтверджені. Вхід заблокований до відновлення даних." :
        g1?.code === "g1_setup" ? "IKIGAI: трикутник зафіксовано; чекає підтвердженого пробою на закритті H1." :
        g1?.code === "g1_entry_signal" ? "IKIGAI: пробій підтверджено; перевіряє ціну, розмір і ризик входу." :
        "IKIGAI: збирає підтверджені точки трикутника H1 та контекст H4. Історія при старті не створює угод.";
      return {...state(g1?.code === "g1_input_unknown" ? "unknown" : "entry", text), checkedAt:g1?.at || null};
    }
    const event = latest(events, e => e.code === "entry_checks" && Array.isArray(e.data?.checks));
    const raw = event ? event.data.checks : context.replay ? [] : Array.isArray(bot.checks) ? bot.checks : [];
    const {own, gates} = splitGates(raw);
    const built = strategy.strategy_schema_version === 2 ? v2(rules, own, events) : v1(rules, own);
    const gateRows = gates.map(check => condition({feature:check.feature, op:check.op, value:check.expected, negated:false}, check));
    return {...base, state:"entry", summary:headline(built.groups, built.stages, gateRows), groups:built.groups,
      stages:built.stages.map(stage => ({...stage, label:label(stage)})),
      gates:gateRows, checkedAt:event?.at || null};
  }
  // B-01a — the shared Jev gate, honestly. Counts come from the shared facts model (battle-facts.js jevAnswers);
  // PAPER journals record "jev_gate", exchange journals "jev_decision": both are read the same way here.
  // For a two-option choice Jev's confidence is p(chosen) − p(other) = 2p − 1, so the arena gate 0,70 means p(proceed) ≥ 0,85.
  const JEV_GATE = 0.7;
  const JEV_NOTE = "Jev перевіряє, чи кандидат узгоджений із правилами рецепта. Це не прогноз ціни і не шанс прибутку.";
  function jevEvents(events) {
    return (Array.isArray(events) ? events : []).filter(e => e && (e.code === "jev_decision" || e.code === "jev_gate")).map(e => e.code === "jev_decision" ? e :
      {...e, code:"jev_decision", data:{...(e.data || {}), jev_allowed:typeof e.data?.allowed === "boolean" ? e.data.allowed : undefined,
        // Same normalisation as cycle_diagnostics._paper_events: a refused "hold" answer is a choice_hold.
        blocked_reason:e.data?.blocked_reason ?? (e.data?.allowed === false && e.data?.answer?.choice === "hold" ? "choice_hold" : undefined)}});
  }
  function jev(bot, context = {}) {
    const facts = context.facts, diagnostic = context.diagnostic || {};
    const own = jevEvents((Array.isArray(context.events) ? context.events : []).filter(e => e && e.bot_id === bot?.id));
    const row = (Array.isArray(diagnostic.rows) ? diagnostic.rows : []).find(r => r && r.id === "jev_entry");
    if (!bot || bot.id === "cash" || bot.strategy_id === "cash" || bot.avatar_kind === "baseline") return null;
    const run = context.run || {}, mode = run.execution_mode ?? (run.mode === "PAPER" ? "rules" : run.mode ? "shared_jev" : null);
    if (!own.length && (row ? row.status === "not_applicable" : mode !== null && mode !== "shared_jev")) return {state:"unused", counts:null, decision:null, note:JEV_NOTE,
      summary:mode === "rules_only" ? "RULES_ONLY: Jev не викликається; запит гейту лише записується для офлайн SHADOW." : "У цьому бою Jev не використовується: рішення ухвалюють лише правила."};
    if (!facts || typeof facts.jevAnswers !== "function") return null;
    const counts = facts.jevAnswers(own), last = own[own.length - 1] || null, data = last?.data || {}, answer = data.answer || null;
    const confidence = answer ? finite(answer.confidence) : null;
    const gate = finite(data.confidence_threshold) ?? finite(counts.threshold) ?? JEV_GATE;
    const chosen = confidence === null ? null : (1 + confidence) / 2;
    const proceed = chosen === null ? null : answer.choice === "proceed" ? chosen : answer.choice === "hold" ? 1 - chosen : null;
    const decision = !last ? null : {at:last.at || null, kind:data.kind === "add" ? "add" : data.kind === "hold" ? "hold" : "entry",
      allowed:data.jev_allowed === true ? true : data.jev_allowed === false ? false : null, choice:answer?.choice || null,
      confidence, gate, pProceed:proceed, pGate:(1 + gate) / 2, blockedReason:data.blocked_reason || null};
    const reasons = counts.reasons || {}, blocked = [["нижче порога", reasons.low_confidence], ["«утримати»", reasons.choice_hold], ["без відповіді", reasons.other]]
      .filter(pair => pair[1]).map(pair => `${pair[0]}: ${pair[1]}`);
    return {state:counts.decisions ? "asked" : "idle", counts, decision, note:JEV_NOTE,
      summary:counts.decisions ? `Запитів до Jev: ${counts.decisions} · дозволів: ${counts.allowed} · відмов: ${counts.blocked}` + (blocked.length ? ` (${blocked.join(", ")})` : "") + "."
        : "Jev ще не запитували: умови входу не виконувалися."};
  }
  return {waiting, jev, jevEvents, condition, leaves, treeMode, feature, FEATURES, NOTE, JEV_NOTE};
});
