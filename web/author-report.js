"use strict";
/* "Author report": one plain-language, reproducible document per participant after a battle (B-19).
   Built only from the review bundle the server already serves, with the same facts model as the arena page.
   Facts only, no advice; the same bundle always gives the same text (times are printed in a fixed zone).
   No requests, no actions. */
((root, factory) => {
  const node = typeof module === "object" && module.exports;
  const facts = node ? require("./battle-facts.js") : root.ArenaBattleFacts;
  const context = node ? require("./battle-context.js") : root.ArenaBattleContext;
  const api = factory(facts, context);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ArenaAuthorReport = api;
})(typeof window === "undefined" ? {} : window, (facts, contextApi) => {
  const VERSION = "author-report/v1";
  const MODES = { PAPER: "PAPER (модель)", OKX_DEMO: "OKX Demo", OKX_LIVE: "OKX Live (реальні кошти)" };
  const MODE_NOTES = {
    PAPER: "PAPER — модельні заповнення, комісії та funding, а не біржові ордери.",
    OKX_DEMO: "OKX Demo — демонстраційний рахунок: заповнення й ціни не еквівалентні реальному ринку."
  };
  const OUTCOMES = { traded: "торгував", no_trade: "не торгував", incomplete: "запис неповний", unknown: "невідомо" };
  const EXIT_REASONS = { stop_loss: "стоп-лосс", take_profit: "тейк-профіт", max_hold: "максимальний час утримання", rules_exit: "формальний вихід за правилами" };
  const num = value => value === null || value === undefined || value === "" || !Number.isFinite(Number(value)) ? null : Number(value);

  const kyivFormat = new Intl.DateTimeFormat("uk-UA", { timeZone: "Europe/Kyiv", day: "2-digit", month: "2-digit", year: "numeric",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  const kyiv = value => {
    const when = new Date(value);
    return Number.isFinite(when.getTime()) ? kyivFormat.format(when).replace(",", "") + " Київ" : "невідомо";
  };
  const fmt = (value, digits) => new Intl.NumberFormat("uk-UA", { maximumFractionDigits: digits }).format(value);
  const money = (text, digits = 6) => {
    const value = num(text);
    return value === null ? "невідомо" : `${value > 0 ? "+" : value < 0 ? "−" : ""}${fmt(Math.abs(value), digits)} USDT`;
  };
  const signed = (text, digits = 6) => {
    const value = num(text);
    return value === null ? "невідомо" : `${value > 0 ? "+" : value < 0 ? "−" : ""}${fmt(Math.abs(value), digits)}`;
  };
  const cell = value => String(value === null || value === undefined ? "" : value).replace(/\|/g, "\\|").replace(/\n/g, " ");
  const table = (head, rows) => [`| ${head.map(cell).join(" | ")} |`, `| ${head.map(() => "---").join(" | ")} |`, ...rows.map(row => `| ${row.map(cell).join(" | ")} |`)].join("\n");
  const duration = seconds => {
    const s = num(seconds);
    if (s === null) return "невідомо";
    return s >= 3600 ? `${fmt(s / 3600, 1)} год` : s >= 60 ? `${fmt(s / 60, 0)} хв` : `${fmt(s, 0)} с`;
  };

  // How far the conditions that kept the entry shut were from firing, from the participant's own entry checks.
  function conditionMisses(events) {
    const groups = new Map();
    for (const event of events) {
      if (event.code !== "entry_checks") continue;
      for (const check of Array.isArray((event.data || {}).checks) ? event.data.checks : []) {
        if (!check || check.passed !== false) continue;
        const key = [check.feature, check.op, check.expected].join("\u0000");
        const group = groups.get(key) || { feature: check.feature || "unknown", op: check.op, expected: check.expected, count: 0, seen: [] };
        group.count += 1;
        const actual = num(check.actual);
        if (actual !== null) group.seen.push(actual);
        groups.set(key, group);
      }
    }
    return [...groups.values()].map(group => {
      const threshold = num(group.expected);
      const nearest = group.seen.length && threshold !== null ? group.seen.reduce((best, value) => Math.abs(value - threshold) < Math.abs(best - threshold) ? value : best) : null;
      return { feature: group.feature, op: group.op, expected: group.expected, count: group.count,
        min: group.seen.length ? Math.min(...group.seen) : null, max: group.seen.length ? Math.max(...group.seen) : null, nearest };
    }).sort((a, b) => b.count - a.count || (a.feature < b.feature ? -1 : a.feature > b.feature ? 1 : 0));
  }

  const OPS = { gt: ">", gte: "≥", lt: "<", lte: "≤" };

  // One row per closed or open round trip, from the order ledger the bundle carries.
  function trades(participant, events) {
    const ledger = participant.trade_ledger || {};
    const rows = Array.isArray(ledger.rows) ? ledger.rows : [], trips = Array.isArray(ledger.round_trips) ? ledger.round_trips : [];
    return trips.map(trip => {
      const seqs = new Set(Array.isArray(trip.fill_seqs) ? trip.fill_seqs : [trip.entry_seq, trip.close_seq]);
      const fills = rows.filter(row => seqs.has(row.seq));
      const entries = fills.filter(row => row.role === "entry"), closes = fills.filter(row => row.role === "close");
      const sum = (list, field) => list.reduce((total, row) => total + (num(row[field]) || 0), 0);
      const triggers = events.filter(event => event.code === "exit_trigger" && event.seq > trip.entry_seq && (trip.close_seq === null || trip.close_seq === undefined || event.seq < trip.close_seq));
      const last = triggers.reduce((best, event) => (best === null || event.seq > best.seq ? event : best), null);  // the highest seq, whatever the array order
      const reason = last ? (last.data || {}).exit_reason : null;
      const opened = entries.length ? Date.parse(entries[0].at) : Date.parse(trip.entry_at), closed = closes.length ? Date.parse(closes[closes.length - 1].at) : null;
      return {
        number: trip.number, entry_at: trip.entry_at, close_at: trip.closed ? (closes.length ? closes[closes.length - 1].at : trip.close_at) : null, closed: trip.closed === true,
        entry_price: entries.length ? entries[0].price_usdt : null, exit_price: closes.length ? closes[closes.length - 1].price_usdt : null,
        quantity_btc: entries.length ? String(sum(entries, "quantity")) : null, gross_usdt: String(sum(closes, "gross_pnl_usdt")), fees_usdt: String(sum(fills, "signed_fees_usdt")),
        held_seconds: Number.isFinite(opened) && Number.isFinite(closed) ? (closed - opened) / 1000 : null, exit_reason: reason || null
      };
    });
  }

  /* build(bundle, participantId, options) -> { version, markdown, data }.  Throws when the participant is not in the bundle.
     options.context: the market context of the battle (ArenaBattleContext.fromFrames(...)); without it the section says "not recorded". */
  function build(bundle, participantId, options = {}) {
    const battle = facts.factsForBattle(bundle);
    const mine = battle.participants.find(p => p.id === participantId);
    const raw = ((bundle || {}).participants || []).find(p => p.id === participantId);
    if (!mine || !raw) throw new Error("Учасника немає в цьому бою.");
    const events = ((bundle || {}).events || []).filter(event => event && event.bot_id === participantId);
    const match = battle.match, rankIndex = battle.ranking.indexOf(participantId);
    const jev = facts.jevAnswers(events);
    const misses = conditionMisses(events), trips = trades(raw, events);
    const funnel = raw.funnel || {}, entry = funnel.entry || {};
    // Competition ranking: participants with exactly equal net share a place (Cash and a participant who never traded are both at 0).
    const nameOf = id => id === "cash" ? "Cash" : (battle.participants.find(p => p.id === id) || {}).name || id;
    const netOf = id => id === "cash" ? "0" : (battle.participants.find(p => p.id === id) || {}).net_usdt;
    const placeOf = id => 1 + battle.ranking.filter(other => facts.compareDecimal(netOf(other), netOf(id)) > 0).length;
    const sharing = battle.ranking.filter(other => other !== participantId && facts.compareDecimal(netOf(other), netOf(participantId)) === 0).map(nameOf);
    const place = rankIndex >= 0
      ? `${placeOf(participantId)} з ${battle.ranking.length} (Cash входить до рейтингу)${sharing.length ? `; ділить місце з: ${sharing.join(", ")}` : ""}` : "не ранжовано";
    const lines = [];
    const push = (...items) => lines.push(...items);
    push(`# Звіт автору · ${mine.name || mine.id}`, "",
      `Бій \`${match.id}\` · ${MODES[match.mode] || match.mode || "режим невідомий"} · старт ${kyiv(match.started_at)} (UTC ${match.started_at || "—"}) · тривалість ${duration(match.duration_seconds)} · статус ${match.status || "—"}${match.incomplete ? " · запис неповний" : ""}`,
      `Версія рецепта: ${raw.version === undefined || raw.version === null ? "не записана" : raw.version} · відбиток \`${raw.recipe_sha256 || "не записано"}\` · формат звіту ${VERSION}`, "",
      "## 1. Підсумок", "", mine.headline, "", `Місце серед ранжованих: ${place}. ${facts.battleHeadline(battle)}`, "",
      "## 2. Хто як зіграв", "",
      table(["Учасник", "Підсумок", "Net, USDT", "Місце (рівні net ділять місце)"], [...battle.participants.map(p =>
        [(p.name || p.id) + (p.id === participantId ? " ← цей звіт" : ""), OUTCOMES[p.outcome] || p.outcome, signed(p.net_usdt), battle.ranking.includes(p.id) ? placeOf(p.id) : "не ранжовано"]),
        ["Cash", "не торгує", signed("0"), placeOf("cash")]]), "",
      "Чужі рецепти тут не показано: лише результат і те, торгував учасник чи ні.", "",
      "## 3. Контекст ринку", "", contextApi.describe(options.context), "",
      "## 4. Воронка рішень", "",
      table(["Крок", "Кількість"], [
        ["Перевірок входу", entry.checks], ["Сигналів (усі умови виконані)", entry.signals], ["Рішень Jev", entry.jev_decisions], ["Дозволів Jev", entry.jev_allows],
        ["Відмов Jev", entry.jev_vetoes], ["Повторних перевірок перед ордером", entry.rechecks], ["Скасувань під час повторної перевірки", entry.recheck_vetoes],
        ["Намірів виставити ордер", funnel.order_intents], ["Виконаних входів", funnel.entry_filled_orders], ["Закритих угод", funnel.round_trips]
      ].map(([label, value]) => [label, value === undefined || value === null ? "не записано" : value])), "");
    if (misses.length) {
      push("### Умови, що не виконувались під час перевірок входу", "", "Лише перевірки входу цього учасника; «найближче» — значення, що було ближче за все до порога.", "",
        table(["Умова", "Не виконувалась", "Частка перевірок", "Значення (мін … макс)", "Найближче до порога"],
          misses.slice(0, 6).map(miss => [`${miss.feature} ${OPS[miss.op] || miss.op} ${miss.expected}`, miss.count,
            entry.checks ? `${fmt(miss.count / entry.checks * 100, 0)} %` : "—",
            miss.min === null ? "не записано" : `${fmt(miss.min, 4)} … ${fmt(miss.max, 4)}`, miss.nearest === null ? "—" : fmt(miss.nearest, 4)])), "");
    }
    if (jev.decisions > 0) {
      const r = jev.reasons;
      push("### Jev", "", `Відповідей: ${jev.decisions}; дозволів: ${jev.allowed}; відмов: ${jev.blocked}; без вердикту виконання: ${jev.unknown}. `
        + `Confidence: ${jev.min === null ? "не записано" : `${fmt(jev.min, 2)} … ${fmt(jev.max, 2)}`}; поріг арени: ${fmt(jev.threshold !== null ? jev.threshold : facts.GATE_THRESHOLD, 2)}. `
        + `Причини відмов: нижче порога — ${r.low_confidence}, «утримати» — ${r.choice_hold}, без придатної відповіді — ${r.other}.`,
        "", "Вхід блокує код арени за порогом; confidence Jev не є прогнозом ціни.", "");
    }
    push("## 5. Угоди", "");
    if (trips.length) {
      push(table(["№", "Вхід", "Ціна входу", "Вихід", "Ціна виходу", "Gross, USDT", "Комісії, USDT", "Утримання", "Причина виходу"],
        trips.map(t => [t.number, kyiv(t.entry_at), t.entry_price === null ? "—" : fmt(num(t.entry_price), 2), t.closed ? kyiv(t.close_at) : "позиція відкрита",
          t.exit_price === null ? "—" : fmt(num(t.exit_price), 2), signed(t.gross_usdt), signed(t.fees_usdt), duration(t.held_seconds),
          t.exit_reason ? (EXIT_REASONS[t.exit_reason] || t.exit_reason) : "не записано"])), "");
    } else {
      push("Угод не було.", "");
    }
    const c = mine.costs;
    const unknownLines = [...mine.unknown, ...(MODE_NOTES[match.mode] ? [MODE_NOTES[match.mode]] : [])];
    push("## 6. Витрати", "", `gross ${money(c.gross)} → комісії ${money(c.fees)} → funding ${money(c.funding)} → net за ордерами ${money(c.net)}.`,
      `Jev (USD, окремо від USDT): ${c.jevUsd === null ? "невідомо" : "$" + fmt(num(c.jevUsd), 6)}${c.jevComplete ? "" : " · облік неповний"}. Облік витрат ${c.complete ? "звірено" : "не звірено до кінця"}.`, "",
      "## 7. Чого не знаємо", "", unknownLines.length ? unknownLines.map(item => `- ${item}`).join("\n") : "- Нічого, що вплинуло б на цей звіт, не відмічено.", "",
      "## 8. Цілісність", "", `Контрольна сума пакета: \`${((bundle || {}).integrity || {}).bundle_sha256 || "не записано"}\` · подій у журналі: ${((bundle || {}).integrity || {}).source_event_count === undefined ? "—" : bundle.integrity.source_event_count} · історія повна: ${bundle.history_complete === false ? "ні" : "так"}.`, "");
    const data = {
      version: VERSION, match, participant: { id: mine.id, name: mine.name, version: raw.version === undefined ? null : raw.version, recipe_sha256: raw.recipe_sha256 || null,
        outcome: mine.outcome, eligible: mine.eligible, net_usdt: mine.net_usdt, rank: rankIndex >= 0 ? rankIndex + 1 : null, headline: mine.headline,
        whyNoTrades: mine.whyNoTrades, funnel: raw.funnel || null, costs: mine.costs, unknown: mine.unknown },
      field: battle.participants.map(p => ({ id: p.id, name: p.name, outcome: p.outcome, net_usdt: p.net_usdt, eligible: p.eligible })), cash: battle.cash,
      ranking: battle.ranking, excluded: battle.excluded, top: battle.top, conditionMisses: misses, jev: jev.decisions ? jev : null, trades: trips,
      context: options.context || null, integrity: (bundle || {}).integrity || null
    };
    return { version: VERSION, markdown: lines.join("\n").replace(/\n{3,}/g, "\n\n").trimEnd() + "\n", data };
  }

  return { VERSION, build, conditionMisses };
});
