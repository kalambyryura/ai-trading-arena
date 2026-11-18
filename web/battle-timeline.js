"use strict";
/* Long-battle states (B-13): participant badges built on the shared facts model (battle-facts.js) and a data
   timeline of the battle: data gaps, minutes without frames, pauses and recovery. Pure model: no DOM, requests
   or controls. Zero signals is a fact, an unknown funnel is not zero, and a tie with Cash is said as a tie. */
((root, factory) => {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ArenaBattleTimeline = api;
})(typeof window === "undefined" ? {} : window, () => {
  const STALE_MINUTES = 10; // the same silence that battle-context.js treats as a gap, not a return
  const MARKS = {data_gap:"Пропуск даних понад 90 с", market_unavailable:"Ринкові дані недоступні", clock_gap:"Розрив годинника",
    recovery_started:"Відновлення: лише закриття", closing_paused:"Закриття призупинено"};
  const time = value => { const t = typeof value === "number" ? value : Date.parse(value); return Number.isFinite(t) ? t : null; };
  const plural = (n, one, few, many) => { const a = n % 10, b = n % 100; return a === 1 && b !== 11 ? one : a >= 2 && a <= 4 && (b < 12 || b > 14) ? few : many; };
  function span(ms) {
    const minutes = Math.max(0, Math.round(ms / 60000)), hours = Math.floor(minutes / 60), rest = minutes % 60;
    return hours ? `${hours} год${rest ? ` ${rest} хв` : ""}` : `${minutes} хв`;
  }

  /* run: the public run payload { status, started_at|created_at, finished_at, admission_closed_at|admission_end_at|end_at, frames[], events[] }
     -> { known, start, end, live, admissionEnd, segments[{kind, from, to, open}], marks[{kind, at, label}], position(t), summary } */
  function timeline(run, options = {}) {
    const start = time(run?.started_at) ?? time(run?.created_at);
    if (!run || start === null) return {known:false, segments:[], marks:[], summary:"Час початку бою не записано."};
    const frames = (Array.isArray(run.frames) ? run.frames : []).map(frame => time(frame?.at)).filter(t => t !== null && t >= start).sort((a, b) => a - b);
    const live = run.status !== "DONE";
    const finished = time(run.finished_at) ?? time(run.ended_at) ?? (live ? null : frames[frames.length - 1] ?? null);
    // Exchange runs use the arena's OKX-derived clock, even on a browser with a wrong date.
    // An interrupted run without a fresh anchor ends at the last recorded observation.
    const lastEvent = (Array.isArray(run.events) ? run.events : []).reduce((last,e)=>Math.max(last,time(e?.at)??start),start);
    const now = run.clock_policy === "okx_utc_monotonic_v1"
      ? time(run.timing?.server_now_ms) ?? lastEvent
      : time(options.now) ?? Date.now();
    const end = finished ?? Math.max(start, now, frames[frames.length - 1] ?? start);
    const admissionEnd = time(run.admission_closed_at) ?? time(run.admission_end_at) ?? time(run.admission_ends_at) ?? time(run.end_at);
    const segments = [];
    let previous = start;
    for (const t of frames) {
      if (t - previous > STALE_MINUTES * 60000) segments.push({kind:"stale", from:previous, to:t, open:false});
      previous = Math.max(previous, t);
    }
    if (end - previous > STALE_MINUTES * 60000) segments.push({kind:"stale", from:previous, to:end, open:live});
    const events = (Array.isArray(run.events) ? run.events : []).filter(e => e && e.bot_id == null && time(e.at) !== null)
      .sort((a, b) => time(a.at) - time(b.at));
    let pausedAt = null;
    for (const event of events) {
      if (event.code === "paused" && pausedAt === null) pausedAt = time(event.at);
      else if (event.code === "recovery_started" && pausedAt !== null) { segments.push({kind:"paused", from:pausedAt, to:time(event.at), open:false}); pausedAt = null; }
    }
    if (pausedAt !== null) segments.push({kind:"paused", from:pausedAt, to:end, open:live});
    const marks = events.filter(e => MARKS[e.code]).map(e => ({kind:e.code, at:time(e.at), label:MARKS[e.code]}));
    const stale = segments.filter(s => s.kind === "stale"), pauses = segments.filter(s => s.kind === "paused");
    const count = kind => marks.filter(m => m.kind === kind).length;
    const parts = [];
    if (count("data_gap")) parts.push(`${count("data_gap")} ${plural(count("data_gap"), "пропуск", "пропуски", "пропусків")} даних понад 90 с`);
    if (stale.length) parts.push(`${stale.length} ${plural(stale.length, "відрізок", "відрізки", "відрізків")} без кадрів довше ${STALE_MINUTES} хв (разом ${span(stale.reduce((t, s) => t + s.to - s.from, 0))})`);
    const faults = count("market_unavailable") + count("clock_gap");
    if (faults) parts.push(`${faults} ${plural(faults, "збій", "збої", "збоїв")} ринкових даних або годинника`);
    if (pauses.length) parts.push(`${pauses.length} ${plural(pauses.length, "пауза", "паузи", "пауз")}${count("recovery_started") ? `, відновлень: ${count("recovery_started")}` : ", без відновлення"}`);
    const length = `${live ? "Триває" : "Записано"} ${span(end - start)}`;
    const summary = parts.length ? `${length}: ${parts.join("; ")}.` : `${length}: без пропусків даних, пауз і відновлень.`;
    const position = t => end > start ? Math.max(0, Math.min(1, (t - start) / (end - start))) : 0;
    return {known:true, start, end, live, admissionEnd, segments, marks, position, summary};
  }

  /* facts: ArenaBattleFacts.factsForBot(...) of one participant; options.battle: factsForBattle(...) of the SAME battle.
     -> [{key, text, tone, title}] most important first; nothing is guessed when the funnel is unknown. */
  function badges(facts, options = {}) {
    if (!facts) return [];
    const out = [], signals = facts.funnel ? facts.funnel.signals : null, battle = options.battle;
    if (facts.outcome === "no_trade" && signals === 0)
      out.push({key:"waited", tone:"neutral", text:facts.final ? "чекав весь бій · 0 сигналів" : "поки 0 сигналів",
        title:"Умови входу жодного разу не виконалися. Нуль угод — теж результат: як у Cash, без комісій."});
    else if (facts.outcome === "no_trade" && signals > 0)
      out.push({key:"blocked", tone:"neutral", text:"сигнали були · угод немає", title:facts.whyNoTrades?.text || ""});
    else if (facts.outcome === "traded" && facts.vsCash === "equal")
      out.push({key:"flat", tone:"neutral", text:"торгував · net 0", title:"Угоди були, але сумарний net дорівнює нулю — це не те саме, що чекати весь бій."});
    else if (facts.outcome === "incomplete") out.push({key:"incomplete", tone:"warn", text:"облік неповний", title:(facts.unknown || []).join(", ")});
    else if (facts.outcome === "unknown") out.push({key:"unknown", tone:"warn", text:"стан невідомий", title:(facts.unknown || []).join(", ")});
    if (battle && battle.match?.final && battle.top?.withCash && battle.top.tie && battle.top.ids.includes(facts.id))
      out.unshift({key:"shared_top", tone:"good", text:"спільне перше з Cash", title:"Найкращий net бою дорівнює нулю й ділиться з Cash, який не торгує."});
    return out;
  }

  return {STALE_MINUTES, MARKS, timeline, badges, span};
});
