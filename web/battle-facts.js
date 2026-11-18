"use strict";
/* Battle facts for the arena pages: what each participant did, why nobody traded, what it cost, who is on top.
   Pure functions over data the run payload or the review bundle already carries; no requests, no actions.
   An unobserved value is never turned into zero: "did not trade" needs observed checks and no unknown outcome,
   money is handled as exact decimal strings, and an incomplete record is shown but never ranked. */
((root, factory) => {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ArenaBattleFacts = api;
})(typeof window === "undefined" ? {} : window, () => {
  const GATE_THRESHOLD = 0.7; // arena code (not Jev) refuses an entry whose Jev confidence is below this
  const SCALE = 18;
  const num = value => value === null || value === undefined || value === "" || !Number.isFinite(Number(value)) ? null : Number(value);

  // ---- exact money: decimal strings in, decimal strings out ---------------------------------------------------
  function parseDecimal(value) {
    if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
    let text = typeof value === "number" ? (Number.isFinite(value) ? String(value) : null) : String(value).trim();
    if (text === null) return null;
    if (!/^[+-]?\d+(\.\d*)?$/.test(text)) {            // exponent forms such as 1E-7: go through Number, lossy only beyond 18 digits
      const n = Number(text);
      if (text === "" || !Number.isFinite(n)) return null;
      text = n.toFixed(SCALE);
    }
    const negative = text.startsWith("-");
    const [whole, fraction = ""] = text.replace(/^[+-]/, "").split(".");
    const digits = (whole + fraction.slice(0, SCALE).padEnd(SCALE, "0")).replace(/^0+(?=\d)/, "");
    const big = BigInt(digits) * (negative ? -1n : 1n);
    return { big, text: formatBig(big) };
  }
  function formatBig(big) {
    const negative = big < 0n, abs = (negative ? -big : big).toString().padStart(SCALE + 1, "0");
    const whole = abs.slice(0, abs.length - SCALE), fraction = abs.slice(abs.length - SCALE).replace(/0+$/, "");
    return `${negative ? "-" : ""}${whole}${fraction ? "." + fraction : ""}`;
  }
  const decimalText = value => { const d = parseDecimal(value); return d === null ? null : d.text; };
  function compareDecimal(a, b) {
    const x = parseDecimal(a), y = parseDecimal(b);
    if (x === null || y === null) return null;
    return x.big < y.big ? -1 : x.big > y.big ? 1 : 0;
  }
  const toNumber = text => (text === null || text === undefined ? null : Number(text));

  // ---- evidence scanners ---------------------------------------------------------------------------------------
  const ownEvents = (events, botId) => (Array.isArray(events) ? events : []).filter(event => event && event.bot_id === botId);

  function rankFailed(tally, checks) {
    const rows = [...tally.entries()].map(([feature, count]) => ({ feature, count, share: checks ? count / checks : null }));
    rows.sort((a, b) => b.count - a.count || (a.feature < b.feature ? -1 : 1));
    return { checks, rows };
  }
  // Which failed conditions kept the entry shut, counted over the bot's own entry checks.
  function failedFeatures(events) {
    const tally = new Map();
    let checks = 0;
    for (const event of events) {
      if (event.code !== "entry_checks") continue;
      checks += 1;
      for (const check of Array.isArray((event.data || {}).checks) ? event.data.checks : []) {
        if (check && check.passed === false) tally.set(check.feature || "unknown", (tally.get(check.feature || "unknown") || 0) + 1);
      }
    }
    return rankFailed(tally, checks);
  }
  // The review bundle already carries the tally (funnel.failed_features) next to the number of entry checks.
  function failedFromFunnel(funnel) {
    const source = funnel && typeof funnel.failed_features === "object" && funnel.failed_features ? funnel.failed_features : null;
    if (!source) return null;
    const tally = new Map(Object.entries(source).filter(([, count]) => num(count) !== null).map(([name, count]) => [name, num(count)]));
    return rankFailed(tally, funnel.entry ? num(funnel.entry.checks) || 0 : 0);
  }

  // One shared-gate answer, whichever journal wrote it. Exchange journals record "jev_decision" {jev_allowed, blocked_reason};
  // PAPER journals record "jev_gate" {allowed, attempted, answer}. A PAPER gate is read the way cycle_diagnostics._paper_events
  // reads it for the review bundle: allowed is the verdict, and a refused "hold" answer is a choice_hold. Anything else is not a decision.
  function jevDecision(event) {
    if (!event) return null;
    if (event.code === "jev_decision") return event.data || {};
    if (event.code !== "jev_gate") return null;
    const data = event.data || {};
    return { ...data, jev_allowed: data.allowed,
      blocked_reason: Object.hasOwn(data, "blocked_reason") ? data.blocked_reason : data.allowed === false && (data.answer || {}).choice === "hold" ? "choice_hold" : undefined };
  }

  // Why Jev did not open the door, as the engine recorded it: low_confidence (below the gate), choice_hold (Jev said hold),
  // anything else (no usable answer in time). The gate threshold is read from the events themselves when present.
  function jevAnswers(events) {
    const confidences = [], reasons = { low_confidence: 0, choice_hold: 0, other: 0 };
    let allowed = 0, blocked = 0, unknown = 0, threshold = null;
    for (const event of events) {
      const data = jevDecision(event);
      if (!data) continue;
      const confidence = num((data.answer || {}).confidence);
      if (confidence !== null) confidences.push(confidence);
      if (threshold === null) threshold = num(data.confidence_threshold);
      if (data.jev_allowed === true) allowed += 1;
      else if (data.jev_allowed === false) {
        blocked += 1;
        // Older journals have no blocked_reason: a confidence below the gate then counts as low_confidence, anything else as "other".
        const gate = num(data.confidence_threshold) !== null ? num(data.confidence_threshold) : GATE_THRESHOLD;
        const reason = data.blocked_reason === "low_confidence" || data.blocked_reason === "choice_hold" ? data.blocked_reason
          : data.blocked_reason ? "other" : confidence !== null && confidence < gate ? "low_confidence" : "other";
        reasons[reason] += 1;
      } else unknown += 1;
    }
    return {
      decisions: allowed + blocked + unknown, allowed, blocked, unknown, reasons, threshold,
      min: confidences.length ? Math.min(...confidences) : null, max: confidences.length ? Math.max(...confidences) : null
    };
  }

  // ---- one participant ----------------------------------------------------------------------------------------
  function costsOf(costs, extra) {
    const c = costs || null, jev = (c && c.jev) || null, own = extra || {};
    return {
      gross: c ? decimalText(c.gross_pnl_usdt) : null,
      fees: c ? decimalText(c.signed_fees_usdt) : null,
      funding: c ? decimalText(c.signed_funding_usdt) : null,
      net: c ? decimalText(c.net_usdt) : null,
      complete: c ? c.complete === true : false,
      jevUsd: jev ? decimalText(jev.known_cost_usd) : decimalText(own.jevUsd),
      jevComplete: jev ? jev.complete === true && num(jev.unknown_billing_requests) === 0 : own.jevComplete === true
    };
  }

  /* input: { id, name, funnel, costs, pnl, botEntries, botTrades, failed, jev, quality, run, extra }
       funnel/costs  lifecycle-style objects (run.lifecycle.bots[] item, or a review-bundle participant)
       pnl           equity-based PnL shown by the roster, when the caller has it (it then wins over costs.net for the headline)
       failed/jev    evidence from failedFeatures()/failedFromFunnel() and jevAnswers()
       quality       review-bundle participant.quality (settlement/availability/competitive_validity), when present
       run           { status, incomplete, historyComplete } */
  function analyse(input) {
    const { funnel = null, run = {}, quality = null } = input;
    const activeOnly = run.scorePolicy?.id === "filled_entry_net_v1";
    const entry = funnel && funnel.entry ? funnel.entry : null;
    const costs = costsOf(input.costs, input.extra);
    const filled = funnel ? num(funnel.entry_filled_orders) : null;
    const roundTrips = funnel ? num(funnel.round_trips) : null;
    const intents = funnel && num(funnel.order_intents) !== null ? num(funnel.order_intents) : 0;
    const botEntries = num(input.botEntries);
    const unknown = [];
    if (!funnel) unknown.push("воронка рішень");
    if (run.incomplete === true) unknown.push("повнота журналу");
    if (run.historyComplete === false) unknown.push("початок журналу скорочено");
    if (!costs.complete) unknown.push("облік витрат");

    const observedChecks = entry ? num(entry.checks) : null;
    const sureTraded = (filled !== null && filled > 0) || (!activeOnly && botEntries !== null && botEntries > 0);
    // An order intent without a recorded fill could hide an entry, so it blocks the "did not trade" claim.
    const clean = Boolean(entry) && num(entry.signals_unknown) === 0 && num(entry.jev_outcomes_unknown) === 0
      && num(funnel.no_signal_unknown) === 0 && intents === 0;
    let outcome = "unknown";
    if (sureTraded) outcome = "traded";
    else if (observedChecks !== null && observedChecks > 0 && clean && filled === 0) outcome = "no_trade";
    else if (activeOnly && filled === 0 && run.historyComplete === true && costs.complete) outcome = "no_trade";

    const settlementPending = run.status === "DONE" && quality && quality.settlement && quality.settlement.status === "PENDING";
    const dataIncomplete = quality && (quality.data_status === "INCOMPLETE" || quality.availability && quality.availability.status === "INCOMPLETE");
    const incomplete = run.incomplete === true || Boolean(settlementPending) || Boolean(dataIncomplete);
    const factualOutcome = outcome;
    if (incomplete) outcome = "incomplete";

    const funnelRow = entry ? {
      checks: observedChecks, signals: num(entry.signals), jevDecisions: num(entry.jev_decisions), jevAllows: num(entry.jev_allows),
      jevBlocks: num(entry.jev_vetoes), rechecks: num(entry.rechecks), recheckVetoes: num(entry.recheck_vetoes),
      entries: filled, roundTrips
    } : null;

    let whyNoTrades = null;
    if (outcome === "no_trade") {
      if (funnelRow.signals === 0) {
        const failed = input.failed || { checks: 0, rows: [] };
        whyNoTrades = { code: "no_signal", evidence: { failed: failed.rows.slice(0, 3), failedChecks: failed.checks || funnelRow.checks } };
      } else if (funnelRow.jevDecisions > 0 && funnelRow.jevAllows === 0) {
        const jev = input.jev || jevAnswers([]);
        whyNoTrades = { code: "jev_declined", evidence: { jev: { ...jev, threshold: jev.threshold !== null ? jev.threshold : GATE_THRESHOLD } } };
      } else {
        whyNoTrades = { code: "later_step", evidence: { recheckVetoes: funnelRow.recheckVetoes } };
      }
    }

    // A participant with no fills has, by construction, spent nothing: the order-based net is zero even when no cost line was recorded.
    let net = input.pnl !== undefined && decimalText(input.pnl) !== null ? decimalText(input.pnl) : costs.net;
    if (net === null && factualOutcome === "no_trade") net = "0";
    const vs = net === null ? null : compareDecimal(net, "0");
    const evidenceEligible = !incomplete && run.historyComplete !== false && (outcome === "traded" || outcome === "no_trade")
      && (!quality || !quality.competitive_validity || quality.competitive_validity.status === "ELIGIBLE");
    const eligible = evidenceEligible && (!activeOnly || (Number.isSafeInteger(filled) && filled > 0));
    const facts = {
      id: input.id || null, name: input.name || null, outcome, eligible, evidenceEligible,
      participation: activeOnly ? filled === 0 ? "NO_ENTRY" : Number.isSafeInteger(filled) && filled > 0 ? "PARTICIPATED" : "UNKNOWN" : null,
      final: run.status === "DONE" && run.incomplete !== true,
      trades: roundTrips !== null ? roundTrips : num(input.botTrades), entries: filled !== null ? filled : botEntries,
      net_usdt: net, vsCash: vs === null ? "unknown" : vs > 0 ? "above" : vs < 0 ? "below" : "equal",
      funnel: funnelRow, whyNoTrades, costs, unknown
    };
    if (whyNoTrades) whyNoTrades.text = sentence(facts);
    facts.headline = headline(facts);
    return facts;
  }

  /* run-payload shape (the arena page): { bot, lifecycleBot, events, run }
       bot          the run's final bot object (pnl_usdt, entries, ...)
       lifecycleBot run.lifecycle.bots[] item (funnel, costs)
       events       the run's events (filtered here by bot id)
       run          { status, incomplete, history_complete } */
  function factsForBot(input) {
    const { bot = {}, lifecycleBot = null, run = {} } = input || {};
    const events = ownEvents(input && input.events, bot.id);
    return analyse({
      id: bot.id, name: bot.name, funnel: lifecycleBot && lifecycleBot.funnel, costs: lifecycleBot && lifecycleBot.costs,
      pnl: bot.pnl_usdt, botEntries: bot.entries, botTrades: bot.trades,
      failed: failedFeatures(events), jev: jevAnswers(events), quality: null,
      run: { status: run.status, incomplete: run.incomplete === true, historyComplete: run.history_complete,
        scorePolicy: run.match_manifest?.score_policy },
      extra: { jevUsd: bot.ai_cost_usd, jevComplete: bot.ai_cost_complete }
    });
  }

  // ---- one battle ---------------------------------------------------------------------------------------------
  /* bundle: the review bundle that the review endpoint serves (build_review output): { match, participants[], events[], cash, history_complete } */
  function factsForBattle(bundle, options = {}) {
    const b = bundle && typeof bundle === "object" ? bundle : {};
    const match = b.match || {};
    // R2.1.11 (D141 p. 2): the server says it beside the hashed bundle (the review header, the history row)
    const exampleOnly = options.exampleOnly === true;
    const policy = match.score_policy, activeOnly = policy?.id === "filled_entry_net_v1";
    const policyKnown = policy === undefined || policy?.id === "legacy_net_with_cash_v1" || (activeOnly && policy.include_cash === false
      && policy.minimum_entry_filled_orders === 1 && policy.primary === "RECONCILED_NET_USDT" && policy.tie_tolerance_usdt === "0.00000001");
    const run = { status: match.status, incomplete: match.incomplete === true, historyComplete: b.history_complete, scorePolicy: policy };
    const events = Array.isArray(b.events) ? b.events : [];
    const participants = (Array.isArray(b.participants) ? b.participants : []).map(p => {
      const own = ownEvents(events, p.id);
      return analyse({ id: p.id, name: p.name, funnel: p.funnel, costs: p.costs, failed: failedFromFunnel(p.funnel) || failedFeatures(own),
        jev: jevAnswers(own), quality: p.quality, run });
    });
    const rawRows = Array.isArray(b.participants) ? b.participants : [], allocation = decimalText(rawRows[0]?.allocation_usdt);
    const complete = match.status === "DONE" && match.incomplete !== true && b.history_complete === true && policyKnown
      && participants.every(p => p.evidenceEligible && p.costs.complete && p.net_usdt !== null)
      && allocation !== null && rawRows.every(p => decimalText(p.position_btc) !== null && compareDecimal(p.position_btc, "0") === 0
        && decimalText(p.allocation_usdt) !== null && compareDecimal(p.allocation_usdt, allocation) === 0);
    const entries = participants.filter(p => policyKnown && (!activeOnly || complete) && p.eligible && p.net_usdt !== null).map((p, index) => ({ id: p.id, net: p.net_usdt, index }));
    if (!activeOnly && policyKnown) entries.push({ id: "cash", net: "0", index: -1 });
    entries.sort((a, b2) => compareDecimal(b2.net, a.net) || a.index - b2.index);   // net desc; equal nets keep bundle order, Cash first
    const ranked = new Set(entries.map(e => e.id));
    const within = (left, right) => typeof require === "function" ? require("./review-model.js").within(left, right, "0.00000001")
      : typeof window !== "undefined" && window.ArenaReviewModel ? window.ArenaReviewModel.within(left, right, "0.00000001") : compareDecimal(left, right) === 0;
    const topIds = entries.filter(e => activeOnly ? within(e.net, entries[0].net) : compareDecimal(e.net, entries[0].net) === 0).map(e => e.id);
    return {
      match: {
        id: match.id || null, mode: match.mode || null, status: match.status || null, incomplete: match.incomplete === true,
        final: match.status === "DONE" && match.incomplete !== true, started_at: match.started_at || null,
        admission_ends_at: match.admission_ends_at || null, ended_at: match.ended_at || null,
        duration_seconds: num(match.duration_seconds), score_policy: policy || null, policyKnown,
        noContest: activeOnly && complete && participants.length > 0 && participants.every(p => p.participation === "NO_ENTRY"),
        exampleOnly
      },
      participants, cash: { net_usdt: "0" }, ranking: entries.map(e => e.id),
      excluded: participants.filter(p => !ranked.has(p.id)).map(p => ({ id: p.id, reason: activeOnly && p.participation === "NO_ENTRY" && p.evidenceEligible ? "no_filled_entry" : p.outcome === "incomplete" ? "incomplete" : p.outcome === "unknown" ? "unknown" : "ineligible" })),
      top: { ids: topIds, withCash: topIds.includes("cash"), tie: topIds.length > 1 }
    };
  }

  // ---- words --------------------------------------------------------------------------------------------------
  const fmt = (value, digits = 2) => new Intl.NumberFormat("uk-UA", { maximumFractionDigits: digits }).format(value);
  const amount = (text, digits = 4) => {
    const value = toNumber(text);
    return value === null ? "невідомо" : `${value > 0 ? "+" : value < 0 ? "−" : ""}${fmt(Math.abs(value), digits)} USDT`;
  };
  const plural = (n, one, few, many) => {
    const m10 = n % 10, m100 = n % 100;
    return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20) ? few : many;
  };

  // The "did not trade" sentence; analyse() stores it in whyNoTrades.text.
  function sentence(facts) {
    const f = facts.funnel, w = facts.whyNoTrades, lead = facts.final ? "Не торгував" : "Поки не торгував";
    if (w.code === "no_signal") {
      const top = w.evidence.failed.length ? ` Найчастіше не виконувалась умова «${w.evidence.failed[0].feature}» (у ${w.evidence.failed[0].count} з ${w.evidence.failedChecks} перевірок).` : "";
      return `${lead}: умови входу не виконувались — з ${f.checks} ${plural(f.checks, "перевірки", "перевірок", "перевірок")} жодна не дала сигналу.${top}`;
    }
    if (w.code === "jev_declined") {
      const jev = w.evidence.jev, r = jev.reasons, times = n => `${n} ${plural(n, "раз", "рази", "разів")}`;
      const range = jev.min === null ? "" : jev.min === jev.max ? ` (confidence ${fmt(jev.min)})` : ` (confidence ${fmt(jev.min)}–${fmt(jev.max)})`;
      const signals = `правила дали ${f.signals} ${plural(f.signals, "сигнал", "сигнали", "сигналів")}`;
      if (r.low_confidence > 0 && r.choice_hold === 0 && r.other === 0) {
        return `${lead}: ${signals}, але перевірка Jev жодного разу не набрала порога ${fmt(jev.threshold)}${range}. Вхід блокує код арени за порогом; це не прогноз ринку.`;
      }
      const parts = [];
      if (r.low_confidence) parts.push(`${times(r.low_confidence)} confidence нижче порога ${fmt(jev.threshold)}${range}`);
      if (r.choice_hold) parts.push(`${times(r.choice_hold)} Jev відповів «утримати»`);
      if (r.other) parts.push(`${times(r.other)} Jev не дав придатної відповіді`);
      return `${lead}: ${signals}, але вхід не відбувся — ${parts.join("; ")}. Рішення про вхід ухвалює код арени, це не прогноз ринку.`;
    }
    const veto = w.evidence.recheckVetoes;
    return `${lead}: сигнал був, але вхід не відбувся на пізнішому кроці${veto ? ` (скасувань під час повторної перевірки: ${veto})` : ""}.`;
  }

  // One short paragraph in plain Ukrainian. It never claims more than the data shows.
  function headline(facts) {
    if (facts.outcome === "incomplete") {
      return `Запис раунду неповний, тому учасника не ранжовано.${facts.net_usdt !== null ? ` Відомий результат: ${amount(facts.net_usdt)}.` : ""}`;
    }
    if (facts.outcome === "unknown") return "Недостатньо записаних даних, щоб сказати, чи торгував учасник: «не записано» не означає «нуль».";
    if (facts.outcome === "traded") {
      const n = facts.trades, e = facts.entries;
      const head = n > 0 ? `Зробив ${n} ${plural(n, "угоду", "угоди", "угод")}`
        : e > 0 ? (facts.final ? `Відкрив позицію (входів: ${e}), але закритої угоди не зафіксовано` : `Відкрив позицію (входів: ${e}), закритих угод ще немає`)
        : "Торгував";
      return `${head}. ${facts.final ? "Результат" : "Поточний PnL"}: ${amount(facts.net_usdt)} (Cash — 0).`;
    }
    return facts.whyNoTrades.text || sentence(facts);
  }

  // The battle in one sentence: who is on top, and whether Cash is there too.
  function battleHeadline(bf) {
    if (!bf || !bf.match) return "";
    if (bf.match.exampleOnly) return "Технічна проба, не рахується: у бою лише навчальні приклади, переможця в лізі немає.";
    if (!bf.match.final) return bf.match.incomplete ? "Запис бою неповний: підсумок не підбито." : "Бій ще триває: підсумок буде після завершення.";
    if(bf.match.policyKnown===false)return "Переможця не визначено: невідомий регламент бою.";
    const name = id => id === "cash" ? "Cash" : ((bf.participants.find(p => p.id === id) || {}).name || id);
    const byId = id => bf.participants.find(p => p.id === id);
    if (bf.match.score_policy?.id === "filled_entry_net_v1") {
      if (bf.match.noContest) return "Переможця немає: жодна стратегія не виконала вхід у ринок. Cash — лише еталон.";
      const leaders = bf.top.ids, noEntry = bf.excluded.filter(p => p.reason === "no_filled_entry");
      if (!leaders.length) return "Підсумок не підтверджено: недостатньо повних звірених даних.";
      const net = byId(leaders[0])?.net_usdt, loss = compareDecimal(net, "0") < 0;
      return `${leaders.length === 1 ? "Переміг " : "Ділять перемогу: "}${leaders.map(name).join(", ")}: ${amount(net)}. ${loss ? "Найкращий серед торгових учасників, але зі збитком; Cash — 0." : "Cash — 0, поза змаганням."}${noEntry.length ? ` Без виконаного входу, поза рейтингом: ${noEntry.map(p => name(p.id)).join(", ")}.` : ""}`;
    }
    const note = bf.excluded.length ? ` Не ранжовано (запис неповний): ${bf.excluded.map(e => name(e.id)).join(", ")}.` : "";
    const bots = bf.top.ids.filter(id => id !== "cash");
    if (bf.ranking.length === 1) return `Ніхто з учасників не ранжований: ${bf.excluded.length ? "записи неповні" : "даних немає"}.${note}`;
    if (!bots.length) {
      const lost = bf.ranking.length - 1;
      return `Cash — єдиний переможець: ${lost === 1 ? "єдиний ранжований учасник програв Cash" : `кожен із ${lost} ранжованих учасників програв Cash`}.${note}`;
    }
    if (bf.top.withCash) {
      const quiet = bots.filter(id => (byId(id) || {}).outcome === "no_trade");
      return `Спільне перше місце з Cash: ${bots.map(name).join(", ")} — ${quiet.length === bots.length ? "не торгували, тож не втратили нічого" : "ні в плюс, ні в мінус"}.${note}`;
    }
    const net = (byId(bots[0]) || {}).net_usdt;
    return bots.length === 1
      ? `Переміг ${name(bots[0])}: ${amount(net)} (Cash — 0).${note}`
      : `Ділять перше місце: ${bots.map(name).join(", ")} — по ${amount(net)}.${note}`;
  }

  // Supporting rows for the participant dialog: [{label, text}]. Counts only, in the order the decision flows.
  function rows(facts) {
    const out = [];
    const f = facts.funnel;
    if (f) {
      const part = (value, one, few, many) => value === null ? `${many}: невідомо` : `${fmt(value, 0)} ${plural(value, one, few, many)}`;
      out.push({ label: "Вхід", text: [part(f.checks, "перевірка", "перевірки", "перевірок"), part(f.signals, "сигнал", "сигнали", "сигналів"),
        part(f.jevAllows, "дозвіл Jev", "дозволи Jev", "дозволів Jev"), part(f.entries, "вхід", "входи", "входів"), part(f.roundTrips, "угода", "угоди", "угод")].join(" → ") });
    }
    const c = facts.costs;
    const moved = [c.gross, c.fees, c.funding].some(value => value !== null && compareDecimal(value, "0") !== 0);
    if ((facts.outcome === "traded" || moved) && (c.gross !== null || c.fees !== null || c.funding !== null)) {
      out.push({ label: "Витрати", text: `gross ${amount(c.gross)} → комісії ${amount(c.fees)} → funding ${amount(c.funding)} → net за ордерами ${amount(c.net)}` });
    }
    if (c.jevUsd !== null && (compareDecimal(c.jevUsd, "0") > 0 || !c.jevComplete)) out.push({ label: "Jev (USD, окремо від USDT)", text: `$${fmt(toNumber(c.jevUsd), 6)}${c.jevComplete ? "" : " · облік неповний"}` });
    if (facts.unknown.length) out.push({ label: "Не записано", text: facts.unknown.join("; ") });
    return out;
  }

  // host: any element supporting append(). options.doc defaults to the global document; options.omit lists row labels to leave out
  // (the participant dialog already prints the funnel right below this block).
  function render(host, facts, options = {}) {
    const d = options.doc || (typeof document !== "undefined" ? document : null);
    if (!d || !host || !facts) return null;
    const box = d.createElement("div");
    box.className = "battle-facts";
    const lead = d.createElement("p");
    lead.className = `battle-facts-headline ${facts.outcome}`;
    lead.textContent = facts.headline;
    box.append(lead);
    const list = d.createElement("dl");
    list.className = "battle-facts-rows";
    for (const row of rows(facts).filter(item => !(options.omit || []).includes(item.label))) {
      const term = d.createElement("dt"), detail = d.createElement("dd");
      term.textContent = row.label;
      detail.textContent = row.text;
      list.append(term, detail);
    }
    box.append(list);
    host.append(box);
    return box;
  }

  // R2.1.11 (D141 p. 2) / R2.1.12 (MIX-9): "examples only" beside the hashed bundle: the history row or the review header
  function exampleOnlyFrom(target, headers) {
    return (target && target.example_only === true) || Boolean(headers && typeof headers.get === "function" && headers.get("X-Season-Example-Only") === "1");
  }

  return { GATE_THRESHOLD, factsForBot, factsForBattle, headline, battleHeadline, rows, render, failedFeatures, jevAnswers, decimalText, compareDecimal, exampleOnlyFrom };
});
