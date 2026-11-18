"use strict";
/* Read-only, stable presentation of journal facts. This module has no trading transport. */
((root, factory) => {
  const node = typeof module === "object" && module.exports;
  const api = factory(node ? require("./arena.js") : null, node ? require("./bot-waiting.js") : root.ArenaBotWaiting,
    node ? require("./battle-timeline.js") : root.ArenaBattleTimeline,
    node ? require("./battle-facts.js") : root.ArenaBattleFacts, node ? require("./review-model.js") : root.ArenaReviewModel);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ArenaColiseum = api;
})(typeof window === "undefined" ? {} : window, (base, Waiting, Timeline, Facts, Units) => {
  const number = v => v === null || v === undefined || v === "" || !Number.isFinite(Number(v)) ? null : Number(v);
  // position_btc holds coins of the season instrument (rules.instrument, R2): ETH in an ETH season; a season without one is a BTC journal.
  const coinOf = run => (Units || (typeof window !== "undefined" ? window.ArenaReviewModel : null))?.displayCoin(run) ?? "BTC";
  const fmt = (v, digits = 8) => number(v) === null ? "—" : new Intl.NumberFormat("uk-UA", {maximumFractionDigits:digits}).format(Number(v));
  function pnl(v, compact = false) {
    const n = number(v);
    if (n === null) return "—";
    const sign = n < 0 ? "−" : n > 0 ? "+" : "";
    if (n !== 0 && Math.abs(n) < (compact ? .001 : 1e-8)) return sign + "<" + (compact ? "0,001" : "0,00000001");
    return sign + fmt(Math.abs(n), compact ? 3 : 8);
  }
  const COLORS = ["#a94e3c", "#466e87", "#59733d", "#8b6498", "#8c6220", "#287d77"];
  const PHASES = {RUNNING:"Вікно нових входів", DRAINING:"Нових входів немає · завершуємо позиції", CLOSING:"Закриваємо позиції", PAUSED:"Потрібне відновлення або звірка", DONE:"Раунд завершено"};
  const POSES = {idle:0, check:1, think:2, walk:3, guard:4, punch:5, hit:6, rest:7, ko:8};
  const date = v => {
    const ms = number(v) !== null ? Number(v) * (Number(v) < 1e12 ? 1000 : 1) : Date.parse(v);
    return Number.isFinite(ms) ? ms : null;
  };
  const clock = v => date(v) === null ? "—" : new Intl.DateTimeFormat("uk-UA", {hour:"2-digit",minute:"2-digit",second:"2-digit"}).format(date(v));
  const clockLong = v => date(v) === null ? "—" : new Intl.DateTimeFormat("uk-UA", {day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit",second:"2-digit",timeZoneName:"short"}).format(date(v));
  // Plain-language labels for spectators. Journal events usually carry a Ukrainian
  // message; public replay packets carry only codes, so codes need words too.
  const STEPS = {1:"перевірки входу", 2:"Jev перед входом", 3:"заявка на вхід", 4:"відкрита позиція", 5:"рішення про вихід", 6:"закриття"};
  const EVIDENCE = {ACTUAL:"реальні записані дані", MODEL:"модельні угоди (PAPER)", SYNTHETIC:"вигадані дані (тест)"};
  const GLOSSARY = {
    inside:"Учасники з підтвердженою відкритою позицією — вони всередині рингу.",
    waiting:"Учасники без позиції, що чекають своїх умов входу. Більшість часу бою всі чекають — це нормально.",
    finished:"Завершили цикл: позиція закрита, нових входів не буде.",
    unknown:"Обсяг позиції не підтверджено біржею або записом. Це не нуль.",
    RUNNING:"Вікно нових входів відкрите: стратегії можуть відкривати позиції.",
    DRAINING:"Вікно входів закрилося. Відкриті позиції завершуються за власними правилами, нових входів немає.",
    CLOSING:"Позиції закриваються.",
    PAUSED:"Потрібне відновлення або звірка з біржею; нових дій немає.",
    DONE:"Раунд завершено. Результати — з його журналу.",
    INCOMPLETE:"Спостереження неповні: фінальний рейтинг не визначено, відомі збитки лишаються.",
    phase:"Поточний стан учасника і крок циклу: перевірки → Jev → заявка → позиція → вихід → закриття.",
    direction:"LONG — ставка на зростання, SHORT — на падіння. Одна позиція на учасника.",
    position:"Фактичний обсяг у монеті інструмента сезону (BTC або ETH) за підтвердженими виконаннями. OKX рахує ордери в контрактах.",
    pnl:"Результат у USDT після комісій і funding; поруч — відсоток від стартового капіталу учасника.",
    equity:"Поточний капітал учасника в USDT.",
    fees:"Комісії біржі за виконані ордери.",
    funding:"Періодичний платіж між long і short на perpetual futures; може бути витратою або доходом.",
    jev:"Вартість запитів до Jev — спільного ШІ-перевіряльника входів. Рахується в USD окремо від результату.",
    source:"Чи звірено комісії, funding і баланс із біржею. «Неповний» означає ще не звірено, а не помилку."};
  const GUIDE = [
    "Кожен персонаж — зафіксована стратегія автора (людини або ШІ-моделі) з однаковим стартовим капіталом і однаковими правилами. Автор і модель — у деталях учасника.",
    "Усередині рингу — ті, хто зараз має відкриту позицію; зовні — хто чекає своїх умов. Більшість часу всі чекають, і це нормально.",
    "Число під персонажем — результат у USDT після комісій. У нових боях перемогти може лише той, хто виконав хоча б один вхід. Cash (скриня) — еталон 0, поза змаганням; старі бої зберігають свої правила.",
    "Удари — лише картинка різниці результатів: гроші між учасниками не переходять.",
    "Jev — спільна ШІ-перевірка входів лише в режимі shared_jev: «дозволив» означає узгодженість із правилами рецепта, а не прогноз прибутку. У режимі RULES_ONLY входи вирішує код; запит до Jev не надсилається, а лише записується — відповідей Jev під час такого бою немає.",
    "Натисни на персонажа — побачиш його рішення, витрати та журнал кроків. Режим (Live, PAPER чи демонстрація) — у підзаголовку сторінки."];
  const GUIDE_NOTE = "Ліга вимірює стратегії; поки жодна не довела перевагу над Cash після витрат.";
  const explain = key => GLOSSARY[key] || "";
  const factsApi = () => Facts || (typeof window !== "undefined" ? window.ArenaBattleFacts : null);
  const humanize = text => String(text || "").replace(/\b[a-z][a-z0-9_]*_[a-z0-9_]+\b/g, key => Waiting?.FEATURES?.[key]?.label || key);
  // Display identity follows declared provenance, never rank, position or a slot number. The author's name decides
  // first: every team author runs on one model (Sonnet's H10 declares model claude-opus-5-5), so the model only
  // names an author whose name matches none.
  const AUTHORS = [
    ["sol", "Sol", "✦", "#a94e3c", /\bsol\b|gpt-6\.1-sol/i],
    ["astra", "Astra", "◎", "#466e87", /\bastra\b/i],
    ["opus", "Opus", "✒", "#59733d", /\bopus\b/i],
    ["sonnet", "Sonnet", "▤", "#8b6498", /\bsonnet\b/i]
  ];
  function authorIdentity(author = {}, name = "Учасник", cash = false) {
    if (cash) return {key:"cash", name:"Cash", mark:"", color:"#8c6220"};
    for (const source of [String(author.name || ""), String(author.model || "")])
      for (const [key, label, mark, color, pattern] of AUTHORS) if (pattern.test(source)) return {key, name:label, mark, color};
    const label = String(author.name || name || "Учасник");
    return {key:author.kind === "human" ? "human" : "unknown", name:label.length > 24 ? label.slice(0, 23) + "…" : label, mark:author.kind === "human" ? "◇" : "○", color:null};
  }
  // Battle summary rows built from the shared facts model (web/battle-facts.js,
  // factsForBattle): decimal strings in, display rows out. Excluded participants
  // keep their visible loss but no place; Cash is a ranked participant.
  const OUTCOMES = {traded:"торгував", no_trade:"без угод", incomplete:"неповний облік", unknown:"невідомо"};
  const MODES = {PAPER:"PAPER · віртуальні угоди", OKX_DEMO:"OKX Demo", OKX_LIVE:"OKX Live · реальні кошти"};
  const plural = (n, one, few, many) => {const a = Math.abs(Number(n)) % 100, b = a % 10; return a > 10 && a < 20 ? many : b === 1 ? one : b >= 2 && b <= 4 ? few : many;};
  function battleRows(bf, options = {}) {
    if (!bf || !Array.isArray(bf.participants)) return [];
    const strategies = new Map((options.strategies || []).map(s => [s.id, s])), bots = new Map((options.bots || []).map(b => [b.id, b]));
    const byId = new Map(bf.participants.map(p => [p.id, p])), excluded = new Map((bf.excluded || []).map(e => [e.id, e.reason]));
    const order = [...(bf.ranking || [])];
    for (const p of bf.participants) if (!order.includes(p.id)) order.push(p.id);
    if (!order.includes("cash") && bf.cash) order.push("cash");
    const topIds = new Set(bf.top?.ids || []), allocation = number(options.allocation);
    const rankedIds = new Set(bf.ranking || []);
    let place = 0, rankedCount = 0, previousNet = null;
    return order.map(id => {
      const p = id === "cash" ? {id, name:"Cash", outcome:"no_trade", trades:0, net_usdt:bf.cash?.net_usdt ?? "0"} : byId.get(id) || {id, name:id, outcome:"unknown", trades:null, net_usdt:null};
      const ranked = !excluded.has(id) && rankedIds.has(id);
      if (ranked) {
        rankedCount += 1;
        if (previousNet === null || factsApi()?.compareDecimal(previousNet, p.net_usdt) !== 0) place = rankedCount;
        previousNet = p.net_usdt;
      }
      const strategy = strategies.get(bots.get(id)?.strategy_id) || strategies.get(id) || {}, author = strategy.author || {};
      const net = number(p.net_usdt), trades = Number.isInteger(p.trades) ? p.trades : null, outcome = OUTCOMES[p.outcome] ? p.outcome : id === "cash" ? "no_trade" : "unknown";
      const identity = authorIdentity(author, p.name || strategy.name || id, id === "cash");
      return {id, place: ranked ? bf.match?.score_policy?.id==="filled_entry_net_v1"&&topIds.has(id)?1:place : null, name: String(p.name || strategy.name || id), shortName:identity.name, identity:identity.key, author: author.name || "", model: author.model || "",
        net, netText: pnl(p.net_usdt), pct: net !== null && allocation ? percent(net / allocation * 100) : "", trades, outcome,
        outcomeText: id === "cash" ? "не торгує" : outcome === "traded" && trades !== null ? `${trades} ${plural(trades, "угода", "угоди", "угод")}` : OUTCOMES[outcome],
        why: outcome === "no_trade" ? humanize(p.whyNoTrades?.text) : "", top: topIds.has(id), excludedReason: excluded.get(id) || null};
    });
  }
  // R2.1.11 (G2) / R2.1.12 (MIX-9): the open position's entries and the round's (entries_total); journals before it: "—"
  function entriesText(source) {
    const text = value => Number.isInteger(value) ? String(value) : "—";
    return {entries: text(source && source.entries), entries_total: text(source && source.entries_total)};
  }
  function battleSummary(bf, rows) {
    if (bf?.match?.exampleOnly) return "Технічна проба, не рахується · лише навчальні приклади";  // R2.1.11 (D141 p. 2)
    if (!bf?.match?.final) return "Підсумок попередній · облік ще не завершено";
    if (bf.match.noContest) return "Без переможця · ніхто не виконав вхід";
    const leaders = rows.filter(row => row.top && row.place !== null), names = leaders.map(row => row.shortName);
    if (!leaders.length) return "Немає підтвердженого підсумку";
    const allTied = (bf.match.score_policy?.id !== "filled_entry_net_v1" || leaders.length > 1) &&
      leaders.length === rows.filter(row => row.place !== null).length && !bf.excluded?.length;
    const prefix = allTied ? "Нічия" : leaders.length > 1 ? "Спільне перше місце" : "Перше місце";
    return prefix + " · " + names.join(" / ") + " · " + leaders[0].netText + " USDT" +
      (bf.match.score_policy?.id === "filled_entry_net_v1" && leaders[0].net < 0 ? " · зі збитком, нижче Cash" : "");
  }
  function percent(v) {
    const n = number(v);
    if (n === null) return "—";
    const sign = n < 0 ? "−" : n > 0 ? "+" : "";
    if (n !== 0 && Math.abs(n) < .01) return sign + "<0,01 %";
    return sign + fmt(Math.abs(n), 2) + " %";
  }
  const EVENTS = {entry_checks:"Перевірено умови входу", position_checks:"Перевірено відкриту позицію", rules_decision:"Рішення правил рецепта", jev_request:"Запит до Jev",
    jev_decision:"Відповідь Jev", jev_pending:"Чекаємо відповідь Jev", no_signal:"Умови входу не виконано", entry_intent:"Заявка на вхід", entry_cancelled:"Заявку на вхід скасовано",
    entry_rechecked:"Повторна перевірка перед входом", entry_no_fill:"Вхід без виконання", exchange_entry_fill:"Оновлено ордер на вхід", exchange_close_fill:"Оновлено ордер на вихід", exchange_order_read:"Прочитано стан ордера",
    paper_entry_fill:"Модельний вхід", paper_close_fill:"Модельне закриття", close_intent:"Заявка на закриття", exit_trigger:"Спрацювала умова виходу", rules_close:"Закриття за правилами",
    reduction_trigger:"Часткове скорочення позиції", reduction_veto:"Скорочення відхилено", reduction_remainder_veto:"Скорочення нижче мінімального лота", risk_veto:"Вхід зупинено ризик-перевіркою",
    risk_limit:"Досягнуто ризик-ліміт", protective_intent:"Виставляємо захисний стоп", protective_child_adopted:"Захисний стоп підтверджено", financial_observation:"Спостереження балансу й позиції",
    exchange_funding:"Враховано funding", demo_funding_unknown:"Funding невідомий", funding_schedule_restored:"Розклад funding відновлено", accounting_confirmed:"Облік звірено",
    account_checked:"Рахунок перевірено", exchange_reserved:"Зарезервовано кошти", exchange_check_failed:"Перевірка біржі не вдалася", exchange_error:"Помилка біржі",
    market_unavailable:"Ринкові дані недоступні", data_gap:"Пропуск даних", clock_gap:"Розрив годинника", recording_blocked:"Запис заблоковано", report_pending:"Звіт готується",
    admission_closed:"Вікно нових входів закрито", season_started:"Раунд почався", season_done:"Раунд завершено", season_finished:"Раунд завершено", stop_requested:"Запит на зупинку",
    paused:"Пауза", closing_paused:"Закриття призупинено", recovery_started:"Відновлення почалося", demo_execution_review:"Перевірка механіки Demo", setup_armed:"Умова setup спрацювала",
    setup_waiting_confirmation:"Чекаємо підтвердження", setup_confirmed:"Підтвердження отримано", setup_unknown:"Setup: дані невідомі", confirmation_expired:"Підтвердження прострочено",
    entry_order_placed:"Post-only заявку на вхід виставлено", entry_order_expired:"Post-only заявку знято за тайм-аутом", entry_order_rejected:"Post-only заявку не виставлено",
    entry_order_cancelled:"Post-only заявку скасовано", entry_order_cancel_failed:"Скасування post-only заявки не підтверджено",
    entry_order_unwatched_cancel:"Без нагляду: post-only заявку скасовуємо", fill_read_lag:"Заповнення ще не видно в даних ордера"};
  const eventLabel = event => event?.message || EVENTS[event?.code] || (event?.code ? String(event.code).replace(/_/g, " ") : "Подія");
  const phaseLabel = code => PHASES[code] || (code ? String(code) : "—");
  function runLabel(run={}) {
    if(run.status==="DONE"&&run.incomplete)return run.error||(run.events||[]).some(e=>e?.code==="exchange_error")?"Зупинено з помилкою":"Завершено · неповний звіт";
    return phaseLabel(run.status);
  }
  function failureNotice(run={}) {
    if(typeof run.error==="string"&&run.error)return run.error;
    const last=(run.events||[]).filter(e=>["exchange_error","exchange_check_failed","clock_gap"].includes(e?.code)).at(-1);
    if(!run.incomplete||!last)return "";
    if(last.data?.reason==="clock_difference_exceeds_five_minutes")return "Годинник Windows відрізняється від OKX більш ніж на 5 хвилин. Перевір часовий пояс і синхронізуй час; нові входи заблоковано.";
    if(last.code==="clock_gap")return "Зафіксовано зміну годинника або пропуск спостережень. Докладні дані — у журналі раунду.";
    return "Перевірку запуску або акаунта не завершено. Це неповний раунд; причина — у журналі.";
  }
  const stepLabel = step => STEPS[Number(step)] || (step === null || step === undefined ? "" : String(step));
  const evidenceLabel = kind => EVIDENCE[kind] || (kind ? String(kind) : "—");
  function durationLabel(seconds) {
    const s = number(seconds);
    if (s === null || s <= 0) return "";
    if (s < 3600) return Math.round(s / 60) + " хв";
    const h = Math.floor(s / 3600), m = Math.round((s - h * 3600) / 60);
    return m ? `${h} год ${m} хв` : `${h} год`;
  }
  function anchors(ids) {
    const count = ids.length, columns = count>12?12:count>6?6:3, rows = count>6?2:Math.max(1, Math.ceil(count / columns)), outsideColumns=count>12?12:6;
    const inside = new Map(), outside = new Map();
    ids.forEach((id, i) => {
      const cell=count>12?i<12?i*2:(i-12)*2+1:count>6?[0,2,4,6,8,10,1,3,5,7,9,11][i]:i;
      const row = Math.floor(cell / columns), col = cell % columns, rowCount = count>6?columns:Math.min(columns, count - row * columns);
      inside.set(id, {x:19 + (col + .5) * 62 / rowCount, y:rows===1?37:26+row*22/(rows-1)});
      const outsideRow=Math.floor(i/outsideColumns),outsideCol=i%outsideColumns,outsideCount=Math.min(outsideColumns,count-outsideRow*outsideColumns);
      outside.set(id, {x:8 + (outsideCol+.5)*76/outsideCount,y:outsideRow?96:79});
    });
    return {inside,outside};
  }
  function project(run, previous = null, options = {}) {
    const engine = base || (typeof window !== "undefined" ? window.ArenaBattle : null);
    if(options.replay)run={...run,lifecycle:null,events:engine?.visibleEvents({events:run.events,mode:"replay",eventSeq:options.eventSeq,at:options.at})||[]};
    const active = ["RUNNING", "DRAINING", "CLOSING"].includes(run?.status);
    const bots = run?.bots || [], events = run?.events || [], prev = new Map((previous?.bots || []).map(b=>[b.id,b]));
    const trace = engine?.model({id:run?.id, key:options.key, bots, previousBots:previous?.bots, budget:run?.budget_usdt,
      events, eventSeq:options.eventSeq ?? events.at(-1)?.seq, previousEventSeq:previous?.events?.at(-1)?.seq,
      mode:options.replay ? "replay" : "live", at:options.at, paused:run?.status === "PAUSED"});
    const visual = new Map((trace?.actors || []).map(a=>[a.id,a]));
    const diagnostics = new Map((run?.lifecycle?.bots || []).map(d=>[d.bot_id,d]));
    const slots = new Map((run?.manifest?.participants || []).map((p,i)=>[p.bot_id ?? p.id, i]));
    const actors = bots.map((bot, index) => {
      const diagnostic = diagnostics.get(bot.id) || {}, old = prev.get(bot.id), view = visual.get(bot.id) || {};
      const cash = bot.strategy_id === "cash" || bot.id === "cash" || bot.avatar_kind === "baseline";
      const position = number(bot.position_btc), equity = number(bot.equity_usdt), result = number(bot.pnl_usdt);
      const allocation = number(bot.allocation_usdt ?? bot.budget_usdt ?? run?.budget_usdt), pct = result !== null && allocation ? result / allocation * 100 : null;
      const unknown = position === null && !cash && !bot.prepared, ko = equity !== null && equity <= 0 && !cash;
      const open = !cash && position !== null && position !== 0;
      const closing = open && (bot.stop_requested || ["CLOSING","DONE","STOPPED"].includes(bot.status) || run.status === "CLOSING" || view.cycle === "closing" || diagnostic.current_phase?.code === "closing");
      const retired = !cash && !ko && !open && position === 0 && (bot.stop_requested || ["STOPPED","DONE"].includes(bot.status) || run.status === "DONE");
      const frozen = unknown || options.offline || options.stale || bot._manual_review || view.cycle === "paused";
      const phase = diagnostic.current_phase || {};
      const paused = run.status==="PAUSED" || bot._manual_review || view.cycle === "paused" || phase.code==="paused";
      const step = phase.step ?? view.step ?? null;
      const label = cash ? "Без угод" : bot.prepared ? "Ще не запущено" : unknown ? "Позиція невідома" : ko ? "Капітал вичерпано" : paused ? open?"Пауза · позиція ще відкрита":"Потрібна звірка" : closing ? "Закриває · ще на арені" :
        retired ? "Завершив" : open ? (phase.code === "jev" ? "Jev · оцінює позицію" : "Утримує позицію") :
        phase.label || view.cycleLabel?.replace(/^\d · /, "") || "Очікує сигналу";
      const strategy = (run.strategies || []).find(s=>s.id === bot.strategy_id) || bot.strategy || {};
      const slot = slots.get(bot.id) ?? index;
      const identity = authorIdentity(bot.author || strategy.author || {}, bot.name || strategy.name, cash);
      return {id:bot.id, source:bot, name:String(bot.name || strategy.name || "Учасник"), cash, position, equity, pnl:result,
        displayName:identity.name, identity:identity.key, identityMark:identity.mark,
        open, unknown, ko, retired, closing, frozen:frozen||paused, step, label, strategy, diagnostic, slot, allocation, pct,
        tag:cash ? "Cash" : "S" + String(slot + 1).padStart(2,"0"), color:identity.color || COLORS[slot % COLORS.length],
        kind:bot.avatar_kind || bot.author?.kind || strategy.author?.kind || "model",
        zone:cash ? "outside" : unknown ? (options.lastKnownZones?.get(bot.id) || (number(old?.position_btc) !== null ? number(old.position_btc) !== 0 ? "inside" : "outside" : "unknown")) : open ? "inside" : "outside",
        pose:ko ? "ko" : cash || retired ? "rest" : frozen || paused ? "idle" : closing ? "walk" :
          phase.code === "jev" || ["reviewing","reviewing_position","entry_pending","add_pending"].includes(view.cycle) ? "think" :
          open ? "guard" : "check",
        delta:old && result !== null && number(old.pnl_usdt) !== null ? result - Number(old.pnl_usdt) : null};
    });
    // Reserve every manifest member's place before the first fill. Later entries
    // cannot steal a compacted cohort's occupied anchor or move existing fighters.
    const layout=anchors(actors.filter(a=>!a.cash).sort((a,b)=>a.slot-b.slot).map(a=>a.id));
    actors.forEach(a=>a.anchor=a.cash?{inside:{x:93,y:81},outside:{x:93,y:81}}:{inside:layout.inside.get(a.id),outside:layout.outside.get(a.id)});
    return {run, actors, active, coin:coinOf(run), moving:active && !options.still && !options.offline && !options.stale && !run.error,
      incomplete:!!run.incomplete, phase:run.incomplete ? runLabel(run)+" · без місця в рейтингу" : PHASES[run.status] || "Готуємо склад",
      inside:actors.filter(a=>a.open).length, waiting:actors.filter(a=>!a.cash&&!a.open&&!a.retired&&!a.unknown&&!a.ko).length,
      finished:actors.filter(a=>a.retired||a.ko).length, unknown:actors.filter(a=>a.unknown).length};
  }
  function encounter(scene, tick) {
    if (!scene.moving) return null;
    tick=Math.max(0,Math.floor(number(tick)??0));
    const eligible = scene.actors.filter(a=>a.open&&!a.cash&&!a.frozen&&!a.retired&&!a.ko&&a.pnl!==null&&a.equity!==null);
    const attackers = eligible.filter(a=>!a.closing);
    const gainers = attackers.filter(a=>a.delta!==null&&a.delta>1e-8);
    if (tick < gainers.length) {
      const a = gainers[tick], targets = eligible.filter(b=>b.id!==a.id&&b.pnl<a.pnl);
      if (targets.length) return {kind:"wave", attacker:a.id, targets:targets.map(b=>b.id)};
    }
    const pairs = attackers.flatMap(a=>eligible.filter(b=>b.id!==a.id&&a.pnl>b.pnl+1e-8).map(b=>({kind:"punch",attacker:a.id,targets:[b.id]})));
    return pairs.length ? pairs[tick % pairs.length] : null;
  }
  function create(host, options = {}) {
    const el=(tag,cls,text)=>{const n=document.createElement(tag);n.className=cls||"";if(text!==undefined)n.textContent=text;return n;};
    const icon=name=>{const i=el("i","ph ph-"+name);i.setAttribute("aria-hidden","true");return i;};
    const button=(label,fn,name,cls="col-button")=>{const b=el("button",cls);b.type="button";if(name)b.append(icon(name));b.append(document.createTextNode(label));b.addEventListener("click",fn);return b;};
    host.classList.add("coliseum","audience-view");
    const summary=el("div","col-summary"), counts=el("div","col-counts"), phase=el("div","col-phase"), stats={};
    for(const [key,name,label] of [["inside","sword","Позиції"],["waiting","clock","Очікують"],["finished","check-circle","Завершили"],["unknown","question","Невідомо"]]) {
      const box=el("span","col-count col-term");box.title=explain(key);stats[key]=el("b","","0");box.append(icon(name),document.createTextNode(label+": "),stats[key]);counts.append(box);
    }
    summary.append(counts,phase);
    const failure=el("p","col-failure");failure.setAttribute("role","status");failure.hidden=true;
    const grid=el("div","col-grid"), field=el("section","col-field"), viewport=el("div","col-stage-viewport"), stage=el("div","col-stage"), players=el("div","col-players");
    stage.setAttribute("role","group");stage.setAttribute("aria-label","Колізей · відкриті позиції всередині, очікування зовні");
    const floor=el("img","col-floor");floor.src="/assets/coliseum-floor.png";floor.alt="";
    const innerLabel=el("div","col-zone-label col-inner-label","ВІДКРИТІ ПОЗИЦІЇ"), outerLabel=el("div","col-zone-label col-outer-label","ПОЗА РИНКОМ · ОЧІКУВАННЯ");
    const warning=el("div","col-data-warning");warning.setAttribute("role","status");warning.hidden=true;
    stage.append(floor,innerLabel,outerLabel,players,warning);
    const sceneBar=el("div","col-scene-bar"), narrative=el("p","col-narrative"), pause=button("Рух",()=>{paused=!paused;pause.setAttribute("aria-pressed",String(paused));pause.lastChild.textContent=paused?" Рух вимкнено":" Рух";},"pause");
    const minimap=el("div","col-minimap"),mapFloor=el("img","col-map-floor"),mapPlayers=el("div","col-map-players"),mapWindow=el("span","col-map-window");
    mapFloor.src="/assets/coliseum-floor.png";mapFloor.alt="";minimap.setAttribute("role","img");minimap.setAttribute("aria-label","Мінікарта всього поля. Рамка показує видиму область.");minimap.hidden=true;minimap.append(mapFloor,mapPlayers,mapWindow);
    pause.setAttribute("aria-label","Призупинити лише анімацію");pause.setAttribute("aria-pressed","false");sceneBar.append(narrative,minimap,pause);
    const legend=el("p","col-legend","Ринг = підтверджена позиція. Удари відображають PnL. KO = підтверджений капітал ≤ 0; завершення циклу — окремий стан.");
    const eventsBox=el("section","col-events"), eventsHead=el("div","col-events-head");
    eventsHead.append(el("h3","","Історія бою"),button("Повний журнал",()=>options.onJournal?.(scene?.run,null),"arrow-up-right","col-text-button"));
    const recent=el("div","col-recent-events");eventsBox.append(eventsHead,recent);viewport.append(stage);
    const timelineBox=el("section","col-timeline"), eventsPanel=el("section","col-events-panel");timelineBox.hidden=true;
    field.append(viewport,sceneBar,legend);eventsPanel.append(eventsBox,timelineBox);
    const side=el("aside","col-side"), roster=el("section","col-roster"), sideHead=el("div","col-side-head"), rosterTitle=el("h2","","Учасники"), count=el("span","col-roster-count");
    sideHead.append(rosterTitle,count);
    const search=el("input","col-search");search.type="search";search.placeholder="Знайти стратегію";search.setAttribute("aria-label","Знайти учасника за іменем чи ID");
    const rows=el("div","col-roster-rows");roster.append(sideHead,search,rows);
    const details=el("section","col-details");details.hidden=true;side.append(roster,details);grid.append(field,side,eventsPanel);
    const tabs=el("nav","audience-tabs");tabs.setAttribute("role","tablist");tabs.setAttribute("aria-label","Перегляд арени");
    const mobile=window.matchMedia("(max-width: 900px)"), panels={arena:field,participants:side,events:eventsPanel}, tabButtons=new Map();
    let activeTab="arena";
    function changeTab(name, focus=false) {
      activeTab=name;tabs.hidden=!mobile.matches;host.dataset.audienceTab=name;
      for(const [id,panel] of Object.entries(panels)) {
        panel.hidden=mobile.matches&&id!==name;
        const tab=tabButtons.get(id);tab?.setAttribute("aria-selected",String(id===name));if(tab)tab.tabIndex=id===name?0:-1;
        if(mobile.matches){panel.setAttribute("role","tabpanel");panel.setAttribute("aria-labelledby",panel.id+"-tab");}
        else {panel.removeAttribute("role");panel.removeAttribute("aria-labelledby");}
      }
      if(focus)tabButtons.get(name)?.focus();
    }
    for(const [id,label] of [["arena","Арена"],["participants","Учасники"],["events","Події"]]) {
      const panel=panels[id];panel.id=(host.id||"coliseum")+"-"+id;
      const tab=button(label,()=>changeTab(id),null,"audience-tab");tab.id=panel.id+"-tab";tab.setAttribute("role","tab");tab.setAttribute("aria-controls",panel.id);
      tabButtons.set(id,tab);tabs.append(tab);
    }
    tabs.addEventListener("keydown",event=>{const ids=[...tabButtons.keys()], index=ids.indexOf(activeTab);let next=null;
      if(event.key==="ArrowRight")next=ids[(index+1)%ids.length];if(event.key==="ArrowLeft")next=ids[(index+ids.length-1)%ids.length];
      if(event.key==="Home")next=ids[0];if(event.key==="End")next=ids.at(-1);if(next){event.preventDefault();changeTab(next,true);}});
    const resizeTabs=()=>changeTab(activeTab);mobile.addEventListener("change",resizeTabs);changeTab("arena");
    // Collapsed guide: one visible line keeps the scene on the first screen; never hides the mode line or Cash.
    const guide=el("details","col-onboarding"), guideList=el("ol","");for(const line of GUIDE)guideList.append(el("li","",line));
    guide.append(el("summary","","Як читати арену за 30 секунд"),guideList,el("p","col-onboarding-note",GUIDE_NOTE));
    const card=el("section","col-battle");card.hidden=true;card.setAttribute("aria-label","Підсумок бою");
    host.append(summary,failure,card,tabs,grid,guide);
    let scene=null, previous=null, selected=null, following=null, paused=false, offline=false, received=performance.now(), key=null, marketKey=null, raf=0, started=performance.now(), roundStarted=started, disposed=false, eventKey=null, lastExtras={};
    const cards=new Map(), list=new Map(), markers=new Map(), anchorsById=new Map(), lastKnownZones=new Map();
    const reduce=window.matchMedia("(prefers-reduced-motion: reduce)");
    function sprite(actor, pose="idle") {
      const n=el("span","col-sprite"+(actor.cash?" cash":actor.kind==="human"?" human":" robot"));
      if(actor.cash) {n.style.backgroundImage="url(/assets/cash.png)";n.style.backgroundSize="contain";n.style.backgroundPosition="center";}
      else setPose(n,pose);
      n.setAttribute("aria-hidden","true");return n;
    }
    function setPose(node,pose){if(node.classList.contains("cash"))return;const i=POSES[pose]??0;node.style.backgroundPosition=`${i%3*50}% ${Math.floor(i/3)*50}%`;}
    function allParticipants() {
      selected=null;details.hidden=true;roster.hidden=false;cards.forEach(c=>c.button.setAttribute("aria-pressed","false"));
      if(mobile.matches)changeTab("participants");
      const back=list.get(lastSelected)?.button;back?.focus({preventScroll:true});
      if(window.matchMedia("(max-width: 900px)").matches)back?.scrollIntoView({block:"nearest",behavior:reduce.matches?"auto":"smooth"});
    }
    let detailFields={}, detailActor=null, detailLog=null, followButton=null, lastSelected=null, cardFacts=null;
    function select(id) {
      if(!scene?.actors.some(a=>a.id===id))return;
      selected=id;lastSelected=id;roster.hidden=true;details.hidden=false;details.replaceChildren();detailFields={};if(mobile.matches)changeTab("participants");
      const back=button("Усі учасники",allParticipants,"arrow-left","col-text-button");
      const actor=scene.actors.find(a=>a.id===id), portrait=sprite(actor,actor.pose), title=el("h2","col-detail-title"), author=el("p","col-detail-author"), badges=el("p","col-detail-badges"), reason=el("p","col-wait-reason"), waiting=el("section","col-waiting"), jev=el("section","col-waiting col-jev");
      if(!actor.cash){portrait.classList.add("col-portrait");portrait.style.backgroundPosition="center";portrait.style.backgroundSize="contain";}
      const header=el("div","col-detail-header"), heading=el("div","col-detail-heading"), mark=el("span","col-identity-mark");mark.setAttribute("aria-hidden","true");
      header.dataset.author=actor.identity;details.dataset.author=actor.identity;header.style.setProperty("--actor-color",actor.color);details.style.setProperty("--actor-color",actor.color);
      heading.append(title,author);header.append(portrait,mark,heading);
      const hero=el("div","col-detail-hero"), result=el("strong","col-detail-hero-value col-detail-result"), returnText=el("span","col-detail-return col-detail-result-caption");
      hero.append(el("span","col-detail-hero-label","Результат, USDT · після біржових витрат"),result,returnText);detailFields.pnl=result;
      reason.className="col-reason-summary";
      const research=el("a","col-text-button col-research-link","Дослідити наступну версію");research.hidden=true;
      detailActor={portrait,title,author,badges,reason,waiting,jev,returnText,research};details.append(back,header,hero,badges,reason,research);
      const dl=el("dl","col-metrics");
      const accounting=el("details","col-detail-more"), costList=el("dl","col-metrics");accounting.append(el("summary","","Облік та витрати"),costList);
      for(const [id,label] of [["phase","Стан / крок"],["direction","Напрямок"],["position","Позиція, "+scene.coin],["equity","Капітал, USDT"],["entries","Входи в поточній позиції"],["entries_total","Входи за раунд"],["fees","Комісії, USDT"],["funding","Funding, USDT"],["jev","Jev, USD"],["source","Облік"]]){const value=el("dd",""),term=el("dt","",label);if(explain(id)){term.title=explain(id);term.classList.add("col-term");}if(id==="position")detailActor.positionTerm=term;detailFields[id]=value;(["phase","direction","position"].includes(id)?dl:costList).append(term,value);}
      const provenance=el("p","col-detail-author"), declared=actor.source.author||actor.strategy.author||{};
      provenance.textContent="Заявлене авторство: "+(declared.name||"не вказано")+(declared.model?" · "+declared.model:"");
      const conditions=el("details","col-condition-details");conditions.append(el("summary","","Умови та рішення Jev"),waiting,jev,provenance);conditions.hidden=actor.cash;
      details.append(dl,conditions,accounting);
      const controls=el("div","col-detail-controls");followButton=button("Стежити",()=>{following=following===id?null:id;renderSelection();},"crosshair");
      controls.append(button("Повний журнал",()=>options.onJournal?.(scene.run,id),"notebook"),followButton);details.append(controls);
      detailLog=el("div","col-detail-log");details.append(el("h3","","Останні кроки"),detailLog);
      side.scrollTop=0;renderDetails();renderSelection();
      const target=cards.get(id)?.button;if(target&&stage.offsetWidth>viewport.clientWidth)viewport.scrollLeft=Math.max(0,target.offsetLeft-viewport.clientWidth/2);
      back.focus({preventScroll:true});
      if(window.matchMedia("(max-width: 900px)").matches)side.scrollIntoView({block:"start",behavior:reduce.matches?"auto":"smooth"});
    }
    function renderSelection(){cards.forEach((c,id)=>{c.button.setAttribute("aria-pressed",String(id===selected));c.button.classList.toggle("following",id===following);});
      if(followButton){followButton.setAttribute("aria-pressed",String(following===selected));followButton.lastChild.textContent=following===selected?" Стежимо":" Стежити";}}
    function actorReason(actor) {
      if(actor.cash)return "Cash зберігає стартовий капітал. Його результат — орієнтир для всіх стратегій.";
      if(actor.unknown)return "Обсяг позиції не підтверджено. Показуємо останнє відоме розташування, доки стан не буде звірено.";
      if(actor.source.prepared)return "Стратегія готова до раунду. Рішень та угод у цьому перегляді ще немає.";
      const battle=cardFacts?.match?.id===scene.run.id?cardFacts:null;
      const fact=battle?.participants?.find(p=>p.id===actor.id);
      if(fact?.whyNoTrades?.text)return humanize(fact.whyNoTrades.text);
      if(fact?.outcome==="traded"&&Number.isInteger(fact.trades))return fact.trades+" "+plural(fact.trades,"угода","угоди","угод")+" за раунд. Результат включає біржові комісії та funding; витрати Jev показано окремо.";
      const model=Waiting?.waiting(actor.source,{strategy:actor.strategy,run:scene.run,diagnostic:actor.diagnostic,events:scene.run.events||[],replay:!!lastExtras.replay});
      return humanize(model?.summary||actor.diagnostic.wait_reason?.message||actor.source.last_reason||"Пояснення з’явиться після першої записаної перевірки.");
    }
    function audienceEvents(events) {
      const significant=new Set(["jev_decision","jev_gate","entry_intent","entry_no_fill","entry_cancelled","entry_order_placed","entry_order_expired","entry_order_rejected","entry_order_cancelled","entry_order_unwatched_cancel","close_intent","exit_trigger","rules_close","reduction_trigger","risk_limit","admission_closed","season_started","season_done","season_finished","accounting_confirmed","stop_requested","paused","closing_paused","recovery_started","setup_armed","setup_confirmed","confirmation_expired","market_unavailable","data_gap","clock_gap","recording_blocked"]);
      const grouped=[];
      for(const event of events.filter(event=>significant.has(event.code)||/(?:_fill|_veto|_error|_failed)$/.test(event.code||""))) {
        const previous=grouped.at(-1), signature=JSON.stringify([event.bot_id,event.code,event.message,event.data]);
        if(previous?._audienceSignature===signature)grouped[grouped.length-1]={...event,_audienceSignature:signature,_audienceCount:previous._audienceCount+1};
        else grouped.push({...event,_audienceSignature:signature,_audienceCount:1});
      }
      return grouped;
    }
    function writeLog(host, events, limit) {
      if(!events.length){host.replaceChildren(el("p","col-event-empty","У цьому фрагменті немає подій для короткої стрічки. Усі кроки — в повному журналі."));return;}
      const keep=events.slice(-limit).reverse(), existing=new Map([...host.children].map(n=>[n.dataset.seq,n]));
      for(const event of keep) {
        const key=String(event.seq??event.at??event.code);let row=existing.get(key);
        if(!row){row=el("details","col-log-item");row.dataset.seq=key;const caption=el("summary","");const when=el("time","",clock(event.at));if(date(event.at)!==null){when.dateTime=new Date(date(event.at)).toISOString();when.title=clockLong(event.at);}
          const actor=scene?.actors.find(a=>a.id===event.bot_id), body=el("span","col-event-copy");
          if(actor)body.append(el("b","col-event-author",actor.displayName+" · "));
          body.append(el("span","col-event-kind",humanize(EVENTS[event.code]||eventLabel(event))+(event._audienceCount>1?" · ×"+event._audienceCount:"")));caption.append(when,body);row.append(caption);
          if(event.message)row.append(el("p","col-event-explanation",humanize(event.message)));
          if(event.data&&Object.keys(event.data).length){const raw=el("details","col-event-raw");raw.append(el("summary","","Технічні деталі"),el("pre","",JSON.stringify(event.data,null,2)));row.append(raw);}}
        host.append(row);existing.delete(key);
      }
      existing.forEach(n=>n.remove());
    }
    function renderDetails() {
      if(!selected||!scene)return;const actor=scene.actors.find(a=>a.id===selected);if(!actor){allParticipants();return;}
      const costs=actor.diagnostic.costs||{}, strategy=actor.strategy, author=actor.source.author||strategy.author||{};
      detailActor.title.textContent=actor.displayName;
      detailActor.author.textContent=actor.cash?"Контрольний учасник · не торгує":actor.name+" · v"+(strategy.version||actor.source.strategy_version||"—");
      detailActor.author.title=(author.name||"Автор не вказаний")+" · "+(author.model||author.kind||"Автор");
      if(actor.cash)setPose(detailActor.portrait,actor.pose);
      const chips=badgesFor(actor);detailActor.badges.hidden=!chips.length;detailActor.badges.replaceChildren(...chips.map(b=>{const chip=el("span","col-badge",b.text);chip.dataset.tone=b.tone;if(b.title)chip.title=b.title;return chip;}));
      detailFields.phase.textContent=actor.label+(stepLabel(actor.step)?` · ${stepLabel(actor.step)}`:"");
      detailFields.direction.textContent=(actor.position===null?"Невідомо":actor.position>0?"LONG":actor.position<0?"SHORT":"Без позиції")
        +(Number.isInteger(actor.source?.leverage)&&actor.source.leverage>1?` · ${actor.source.leverage}x`:"");  // R2.1.15: only above 1x
      detailActor.positionTerm.textContent="Позиція, "+((Units||window.ArenaReviewModel)?.botCoin?.(actor.source,scene.run)??scene.coin);detailFields.position.textContent=fmt(actor.position,10);detailFields.pnl.textContent=pnl(actor.pnl);detailFields.pnl.className="col-detail-hero-value col-detail-result "+(actor.pnl<0?"negative":actor.pnl>0?"positive":"");
      detailActor.returnText.textContent=(actor.pct===null?"":percent(actor.pct)+" · старт "+fmt(actor.allocation,2)+" USDT · ")+(scene.run.status==="DONE"&&costs.complete===true?"звірено":actor.open?"поточна оцінка":"за журналом");
      detailFields.equity.textContent=fmt(actor.equity);
      // R2.1.11 (G2): entries counts the open position's entries; entries_total, the round's (journals before it: unknown)
      const entries=entriesText(actor.source);detailFields.entries.textContent=entries.entries;detailFields.entries_total.textContent=entries.entries_total;
      detailFields.fees.textContent=costs.signed_fees_usdt===undefined?number(actor.source.fees_usdt)===null?"—":`${fmt(actor.source.fees_usdt)} · витрата`:pnl(costs.signed_fees_usdt);
      detailFields.funding.textContent=costs.signed_funding_usdt===undefined?number(actor.source.funding_usdt)===null?"—":`${fmt(actor.source.funding_usdt)} · ${scene.run.mode==="PAPER"?"платіж":"витрата"}`:pnl(costs.signed_funding_usdt);
      const jev=costs.jev, known=jev?.known_cost_usd??actor.source.ai_cost_usd;
      detailFields.jev.textContent=number(known)===null?"Невідомо":`$${fmt(known,8)}${String(jev?.source||"").includes("estimate")?" · оцінка":""}${jev?.complete===false||actor.source.ai_cost_complete===false?" · неповно":""}`;
      detailFields.source.textContent=costs.complete===true&&scene.run.status==="DONE"?"Чистий · звірено":costs.complete===false?"Облік неповний":actor.open?"Поточна оцінка з виходом":scene.incomplete?"Неповний раунд":"За записаними даними";
      detailActor.reason.textContent=actorReason(actor);
      renderWaiting(detailActor.waiting,Waiting?.waiting(actor.source,{strategy:actor.strategy,run:scene.run,diagnostic:actor.diagnostic,events:scene.run.events||[],replay:!!lastExtras.replay}));
      renderJev(detailActor.jev,Waiting?.jev(actor.source,{events:scene.run.events||[],diagnostic:actor.diagnostic,run:scene.run,facts:typeof window==="undefined"?null:window.ArenaBattleFacts}));
      followButton.hidden=!scene.active||actor.cash;
      const researchHref=typeof window!=="undefined"?window.StrategyResearch?.link(strategy.id):null;
      detailActor.research.hidden=!researchHref||actor.cash;
      if(researchHref)detailActor.research.href=researchHref;
      writeLog(detailLog,audienceEvents((scene.run.events||[]).filter(e=>e.bot_id===actor.id)),6);
    }
    // «Jev перед входом» (B-01a): counts from the shared facts model, the last decision as p(proceed) against the gate.
    function renderJev(host,model) {
      host.replaceChildren();host.hidden=!model;if(!model)return;
      host.append(el("h3","","Jev перед входом"),el("p","col-waiting-summary",model.summary));
      const d=model.decision;
      if(d){const what={entry:"вхід",add:"докупівля",hold:"утримання"}[d.kind]||"вхід",verdict=d.allowed===true?"дозволив":d.allowed===false?"не дозволив":"результат невідомий";
        const line=el("p","col-waiting-stage",`Останнє рішення${d.at?" о "+clock(d.at):""} · ${what}: ${verdict}`+(d.pProceed===null?"":` · p(proceed) ≈ ${fmt(d.pProceed,2)}, поріг ${fmt(d.pGate,2)}`));
        line.dataset.step=d.allowed===true?"2":"1";if(d.confidence!==null)line.title=`confidence = p(proceed) − p(hold) = ${fmt(d.confidence,2)}; поріг коду арени ${fmt(d.gate,2)}`;host.append(line);}
      host.append(el("p","col-waiting-note",model.note));
    }
    // «Чого чекає»: recipe conditions with the latest checked values; text only, never a forecast.
    function renderWaiting(host,model) {
      host.replaceChildren();host.hidden=!model||model.state==="cash";if(host.hidden)return;
      host.append(el("h3","","Чого чекає"),el("p","col-waiting-summary",model.summary));
      for(const stage of model.stages){const line=el("p","col-waiting-stage",stage.label+(stage.expiresAtMs?` · до ${clock(stage.expiresAtMs)}`:"")+(stage.detail?` · ${stage.detail}`:""));line.dataset.step=String(stage.step);host.append(line);}
      const WORDS={met:"виконано",not_met:"ще ні",unknown:"невідомо",pending:"ще не перевірено"}, ICONS={met:"check-circle",not_met:"x",unknown:"question",pending:"hourglass",veto_clear:"check-circle",veto_hit:"warning"};
      for(const group of model.groups){
        const mode={all:"потрібні всі",any:"достатньо однієї",tree:"формула з «або / не»"}[group.mode];
        host.append(el("p","col-waiting-group",group.title+(mode?` · ${mode}`:"")));const list=el("ul","col-conditions");
        for(const c of group.conditions){
          const item=el("li","col-condition"),body=el("span","col-condition-body");item.dataset.status=c.status;
          body.append(el("span","col-condition-text",c.text),el("span","col-condition-now",[WORDS[c.status]&&c.status!=="unknown"&&c.status!=="pending"?WORDS[c.status]:null,c.now,c.need].filter(Boolean).join(" · ")));
          item.append(icon(ICONS[c.status]||"question"),body);
          if(c.status==="not_met"&&c.closeness!==null){const bar=el("span","col-condition-bar"),fill=el("i","");fill.style.width=Math.round(c.closeness*100)+"%";bar.setAttribute("aria-hidden","true");bar.title="Близькість до порогу";bar.append(fill);item.append(bar);}
          list.append(item);
        }
        host.append(list);
      }
      if(model.gates.length)host.append(el("p","col-waiting-facts","Обмеження арени: "+model.gates.map(g=>`${g.text} — ${WORDS[g.status]}`).join("; ")));
      const count=(n,one,few,lots)=>n+" "+plural(n,one,few,lots);
      const facts=[model.funnel?"За бій: "+count(model.funnel.checks,"перевірка","перевірки","перевірок")+(model.funnel.signals===null?"":" · "+count(model.funnel.signals,"сигнал","сигнали","сигналів")+" входу"):"",model.checkedAt?"станом на "+clock(model.checkedAt):""].filter(Boolean);
      if(facts.length)host.append(el("p","col-waiting-facts",facts.join(" · ")));
      host.append(el("p","col-waiting-note",model.note));
    }
    function render() {
      if(!scene)return;
      for(const [key,n] of Object.entries(stats))n.textContent=String(scene[key]);phase.textContent=scene.phase;phase.title=explain(scene.incomplete?"INCOMPLETE":scene.run.status);phase.classList.toggle("col-term",!!phase.title);
      failure.textContent=failureNotice(scene.run);failure.hidden=!failure.textContent;
      count.textContent=String(scene.actors.filter(a=>!a.cash).length)+" + Cash";search.hidden=scene.actors.length<=7&&!search.value;const ids=scene.actors.filter(a=>!a.cash).map(a=>a.id);
      for(const actor of scene.actors){
        if(!anchorsById.has(actor.id))anchorsById.set(actor.id,actor.anchor);
      }
      stage.dataset.density=ids.length>12?"24":ids.length>6?"12":"6";
      stage.dataset.small=String(ids.length<=4);
      stage.dataset.ringDensity=ids.length>12?"24":ids.length>6?"12":"6";
      const seen=new Set();
      for(const actor of scene.actors) {
        seen.add(actor.id);let card=cards.get(actor.id), row=list.get(actor.id);
        if(!card){const b=button("",()=>select(actor.id),null,"col-actor"), image=sprite(actor,actor.pose), identity=el("span","col-actor-name"), author=el("span","col-actor-author"), mark=el("span","col-identity-mark"), amount=el("span","col-actor-pnl"), state=el("span","col-actor-state");mark.setAttribute("aria-hidden","true");
          b.append(image,mark,author,identity,amount,state);b.style.setProperty("--actor-color",actor.color);b.dataset.actorId=actor.id;b.dataset.author=actor.identity;b.tabIndex=-1;players.append(b);card={button:b,image,identity,author,amount,state};cards.set(actor.id,card);}
        if(!row){const b=button("",()=>select(actor.id),null,"col-roster-row"), tag=el("span","col-tag"), mark=el("span","col-identity-mark"), name=el("span","col-row-name"), recipe=el("span","col-row-recipe"), amount=el("span","col-row-pnl"), phase=el("span","col-row-phase"), badge=el("span","col-row-badge");mark.setAttribute("aria-hidden","true");tag.append(mark,el("span","",actor.tag));badge.hidden=true;b.append(tag,name,amount,recipe,phase,badge);b.dataset.author=actor.identity;b.style.setProperty("--actor-color",actor.color);rows.append(b);row={button:b,tag,name,recipe,amount,phase,badge};list.set(actor.id,row);}
        if(!markers.has(actor.id)){const marker=el("span","col-map-marker");marker.style.backgroundColor=actor.color;mapPlayers.append(marker);markers.set(actor.id,marker);}
        card.identity.textContent=actor.cash?"":actor.tag;card.author.textContent=actor.displayName.length>14?actor.displayName.slice(0,8)+"…"+actor.displayName.slice(-4):actor.displayName;card.author.title=actor.displayName;card.amount.textContent=pnl(actor.pnl,true)+(actor.pnl===null?"":" USDT");card.amount.className="col-actor-pnl "+(actor.pnl<0?"negative":actor.pnl>0?"positive":"");
        card.state.textContent=actor.unknown?"?":actor.cash?"Без угод":actor.ko?"KO":actor.open?(actor.position>0?"LONG":"SHORT"):actor.retired?"Завершив":actor.source.prepared?"Готується":"Очікує";
        card.button.setAttribute("aria-label",`${actor.tag} ${actor.name}: ${actor.label}, ${pnl(actor.pnl)} USDT. Відкрити деталі`);
        card.button.dataset.zone=actor.zone;card.button.classList.toggle("cash",actor.cash);card.button.classList.toggle("uncertain",actor.frozen);card.button.classList.toggle("knocked-out",actor.ko);
        row.name.textContent=actor.displayName;row.recipe.textContent=actor.cash?"Контрольний учасник":actor.name;row.recipe.hidden=actor.displayName===actor.name||["sol","astra","opus","sonnet"].includes(actor.identity);row.phase.textContent=actor.label;row.amount.textContent=pnl(actor.pnl,true)+(actor.pct===null?"":` · ${percent(actor.pct)}`);row.amount.className="col-row-pnl "+(actor.pnl<0?"negative":actor.pnl>0?"positive":"");
        row.button.title=actorReason(actor);row.button.setAttribute("aria-label",actor.displayName+" · "+actor.name+" · "+actor.label+" · "+pnl(actor.pnl)+" USDT. Відкрити деталі");
        const chips=badgesFor(actor);row.badge.hidden=!chips.length;row.badge.textContent=chips[0]?.text||"";row.badge.dataset.tone=chips[0]?.tone||"";row.badge.title=chips.map(c=>c.title||c.text).join(" · ");
        row.button.hidden=!!search.value&&!`${actor.tag} ${actor.displayName} ${actor.name}`.toLowerCase().includes(search.value.toLowerCase());
      }
      for(const [id,card] of cards)if(!seen.has(id)){card.button.remove();cards.delete(id);list.get(id)?.button.remove();list.delete(id);markers.get(id)?.remove();markers.delete(id);}
      const frozen=offline||scene.active&&!lastExtras.replay&&performance.now()-received>15000;
      warning.hidden=!frozen;warning.textContent=offline?"Зв’язок втрачено · показуємо останній підтверджений стан":"Свіжого ринкового кадру немає · рух призупинено";
      narrative.textContent=scene.incomplete?"Спостереження неповні. Фінальний рейтинг не визначено.":scene.run.status==="DRAINING"?"Час нових входів завершено. Відкриті позиції чекають власного виходу.":scene.run.status==="DONE"?"Записаний раунд. Позиції й результати — з його журналу.":scene.inside?`${scene.inside} у ринку. Натисни на персонажа, щоб подивитися його рішення.`:"Колізей чекає входів. Учасники перевіряють умови поза ринком.";
      const story=audienceEvents(scene.run.events||[]), nextKey=story.slice(-6).map(e=>e.seq??e.at).join(":");if(nextKey!==eventKey){eventKey=nextKey;writeLog(recent,story,6);if(!recent.children.length)recent.append(el("p","col-event-empty","У цьому фрагменті журналу ще немає ключових подій. Перевірки доступні в повному журналі."));}
      renderTimeline(timelineBox,Timeline?.timeline(scene.run,{now:Date.now()}));
      renderDetails();renderSelection();
    }
    // B-13: badges from the shared facts model (battle-facts.js) and the battle card facts of the same round; none in replay.
    function badgesFor(actor) {
      const facts=typeof window==="undefined"?null:window.ArenaBattleFacts;
      if(!Timeline||!facts||actor.cash||!actor.source||lastExtras.replay)return[];
      const bot=facts.factsForBot({bot:actor.source,lifecycleBot:actor.diagnostic?.funnel?actor.diagnostic:null,events:scene.run.events||[],run:scene.run});
      return Timeline.badges(bot,{battle:cardFacts&&cardFacts.match?.id===scene.run.id?cardFacts:null}).map(chip=>chip.key==="unknown"&&!actor.unknown?{...chip,text:"підсумок не підтверджено"}:chip);
    }
    // B-13: when data was missing, stale, paused or recovered — a bar for the eye and the same facts as text.
    function renderTimeline(host,model) {
      if(!model?.known||lastExtras.replay){host.hidden=true;return;}host.hidden=false;
      const key=[model.summary,model.segments.length,model.marks.length,Math.round(model.end/60000)].join(":");if(host.dataset.key===key)return;host.dataset.key=key;host.replaceChildren();
      host.append(el("h3","","Хронологія бою"),el("p","col-timeline-summary",model.summary));
      const bar=el("div","col-timeline-bar");bar.setAttribute("aria-hidden","true");
      if(model.admissionEnd&&model.admissionEnd>model.start&&model.admissionEnd<model.end){const line=el("span","col-timeline-admission");line.style.left=(model.position(model.admissionEnd)*100)+"%";bar.append(line);}
      for(const s of model.segments){const part=el("span","col-timeline-seg");part.dataset.kind=s.kind;part.style.left=(model.position(s.from)*100)+"%";part.style.width=Math.max(.6,(model.position(s.to)-model.position(s.from))*100)+"%";bar.append(part);}
      for(const m of model.marks){const mark=el("span","col-timeline-mark");mark.dataset.kind=m.kind;mark.style.left=(model.position(m.at)*100)+"%";bar.append(mark);}
      const scale=el("div","col-timeline-scale");scale.append(el("span","",clockLong(model.start)),el("span","",model.admissionEnd&&model.admissionEnd<model.end?"| кінець входів "+clock(model.admissionEnd):""),el("span","",model.live?"зараз":clockLong(model.end)));
      host.append(bar,scale,el("p","col-timeline-legend","Сіре — немає кадрів довше 10 хв · жовте — пауза або звірка · риски — збої даних і відновлення."));
      const items=[...model.segments.map(s=>({at:s.from,text:`${clock(s.from)}–${s.open?"…":clock(s.to)} · ${s.kind==="paused"?"пауза або звірка":"немає кадрів"}`})),...model.marks.map(m=>({at:m.at,text:`${clock(m.at)} · ${m.label}`}))].sort((a,b)=>a.at-b.at);
      if(items.length){const list=el("ul","col-timeline-list");items.slice(0,8).forEach(item=>list.append(el("li","",item.text)));if(items.length>8)list.append(el("li","",`і ще ${items.length-8}`));host.append(list);}
    }
    function animate(now) {
      if(disposed)return;
      if(scene){const stale=scene.active&&!lastExtras.replay&&now-received>15000, moving=scene.moving&&!paused&&!reduce.matches&&!offline&&!stale;
        const elapsed=Math.max(0,(now-started)/1000), battleElapsed=Math.max(0,(now-roundStarted)/1000), beat=Math.floor(battleElapsed/3.2), progress=battleElapsed%3.2/3.2, action=moving?encounter(scene,beat):null;
        for(const actor of scene.actors){const card=cards.get(actor.id);if(!card)continue;const saved=anchorsById.get(actor.id);
          let point=actor.zone==="inside"?saved.inside:saved.outside, x=point.x,y=point.y,pose=actor.pose,facing=1;
          const before=previous?.bots?.find(b=>b.id===actor.id), oldOpen=number(before?.position_btc)!==null&&number(before?.position_btc)!==0;
          if(moving&&!actor.frozen&&before&&oldOpen!==actor.open&&elapsed<1.3){const from=oldOpen?saved.inside:saved.outside,t=Math.min(1,elapsed/1.3);x=from.x+(point.x-from.x)*t;y=from.y+(point.y-from.y)*t;pose="walk";}
          else if(action&&elapsed>1.3){if(action.attacker===actor.id){if(action.kind==="punch"){const target=scene.actors.find(a=>a.id===action.targets[0]),destination=anchorsById.get(target.id).inside,t=Math.sin(Math.PI*Math.min(1,progress));facing=destination.x>x?1:-1;x+=(destination.x-facing*8-x)*t*.85;y+=(destination.y-y)*t*.85;}pose=progress>.22&&progress<.65?"punch":"walk";}
            else if(action.targets.includes(actor.id)){const origin=anchorsById.get(action.attacker).inside;facing=origin.x>x?1:-1;pose=progress>.35&&progress<.65?"hit":"guard";}}
          if(moving&&!actor.frozen&&!actor.ko&&!actor.retired&&!actor.cash)y+=Math.sin(now/420+actor.tag.length+saved.inside.x)*.16;
          card.button.style.left=x+"%";card.button.style.top=y+"%";card.button.style.zIndex=String(Math.floor(y));setPose(card.image,pose);card.image.style.transform=`scaleX(${facing})`;
          // Keep names and numbers at the participant's own place during a punch.
          // The portrait and emblem move; nearby sprites cannot merge PnLs.
          const pinned=action&&elapsed>1.3?`translate(${(point.x-x)*stage.offsetWidth/100}px,${(point.y-y)*stage.offsetHeight/100}px)`:"";
          card.author.style.transform=pinned;card.identity.style.transform=pinned;card.amount.style.transform=pinned;card.state.style.transform=pinned;
          const marker=markers.get(actor.id);marker.style.left=x+"%";marker.style.top=y+"%";marker.classList.toggle("selected",actor.id===selected);
          if(following===actor.id&&stage.offsetWidth>viewport.clientWidth)viewport.scrollLeft=Math.max(0,stage.offsetWidth*x/100-viewport.clientWidth/2);
        }
        minimap.hidden=stage.offsetWidth<=viewport.clientWidth;
        if(!minimap.hidden){mapWindow.style.left=100*viewport.scrollLeft/stage.offsetWidth+"%";mapWindow.style.width=100*viewport.clientWidth/stage.offsetWidth+"%";}
        if(stale&&warning.hidden){warning.hidden=false;warning.textContent="Свіжого ринкового кадру немає · рух призупинено";}
      }
      raf=requestAnimationFrame(animate);
    }
    search.addEventListener("input",render);
    host.addEventListener("keydown",event=>{if(event.key==="Escape"&&selected){event.preventDefault();allParticipants();}});
    raf=requestAnimationFrame(animate);
    return {
      update(run, extras={}) {const nextKey=String(run.id)+":"+String(extras.key??run.latest_market?.at??run.frames?.at(-1)?.at??run.events?.at(-1)?.seq);
        const changedRun=scene?.run.id!==run.id;
        if(key!==null&&changedRun){cards.forEach(c=>c.button.remove());list.forEach(c=>c.button.remove());markers.forEach(m=>m.remove());cards.clear();list.clear();markers.clear();anchorsById.clear();lastKnownZones.clear();selected=null;following=null;details.hidden=true;roster.hidden=false;eventKey=null;roundStarted=performance.now();}
        if(nextKey!==key){previous=extras.previous||(changedRun?null:scene?.run);started=performance.now();key=nextKey;}
        const nextMarketKey=String(run.id)+":"+String(run.latest_market?.at??(Units||window.ArenaReviewModel)?.marketAt?.(run)??run.frames?.at(-1)?.at??extras.at??null);
        if(nextMarketKey!==marketKey){received=performance.now();marketKey=nextMarketKey;}
        lastExtras=extras;
        scene=project(run,previous,{...extras,offline,stale:false,lastKnownZones});
        scene.actors.filter(a=>a.position!==null).forEach(a=>lastKnownZones.set(a.id,a.open?"inside":"outside"));render();},
      connection(ok){offline=!ok;if(scene){scene=project(scene.run,previous,{...lastExtras,offline,lastKnownZones});render();}},
      battle(bf, options={}) {
        cardFacts=bf||null;
        card.replaceChildren();card.hidden=!bf;if(!bf)return;
        const m=bf.match||{}, meta=[MODES[m.mode]||m.mode||"", clockLong(m.started_at), durationLabel(m.duration_seconds)?"вікно входів "+durationLabel(m.duration_seconds):"", m.incomplete?"неповний раунд":""].filter(v=>v&&v!=="—").join(" · ");
        // Title, meta, headline and replay/review links remain visible; the detailed rows fold away.
        const box=el("details","col-battle-box");box.open=options.collapsed===false;const head=el("summary","col-battle-head");
        head.append(el("span","col-battle-title",options.title||(m.status==="DONE"?"Підсумок бою":"Поточний бій")),el("span","col-battle-meta",meta));
        const battleItems=battleRows(bf,options), places=new Map();for(const row of battleItems)if(row.place!==null)places.set(row.place,(places.get(row.place)||0)+1);
        const headline=battleSummary(bf,battleItems);head.append(el("span","col-battle-headline",headline));
        box.append(head);
        const list=el("ol","col-battle-rows");
        for(const row of battleItems){const li=el("li","col-battle-row"+(row.top?" top":"")+(row.excludedReason?" excluded":""));
          const place=el("span","col-battle-place",row.place?(places.get(row.place)>1?"=":"")+String(row.place):"—"), name=el("span","col-battle-name",row.shortName);place.title=row.place?(places.get(row.place)>1?"Спільне місце ":"Місце ")+row.place:"Поза рейтингом";name.title=row.name;
          li.append(place,name,
            el("span","col-battle-net "+(row.net<0?"negative":row.net>0?"positive":""),row.netText+(row.pct?` · ${row.pct}`:"")),
            el("span","col-battle-outcome",row.excludedReason==="no_filled_entry"?"Не взяв участі · без входу":row.outcomeText+(row.excludedReason?" · поза рейтингом":"")));
          if(row.why)li.append(el("span","col-battle-why",row.why));
          if(row.id!=="cash") {
            const bot=(options.bots||[]).find(b=>b.id===row.id), strategy=(options.strategies||[]).find(s=>s.id===(bot?.strategy_id||row.id));
            const href=typeof window!=="undefined"?window.StrategyResearch?.link(strategy?.id):null;
            if(href){const a=el("a","col-text-button","Дослідити наступну версію");a.href=href;li.append(a);}
          }
          list.append(li);}
        box.append(list);
        box.append(el("p","col-battle-note",m.score_policy?.id==="filled_entry_net_v1"
          ?"Перемога лише після виконаного входу. Cash — еталон 0, поза рейтингом. Σnet після комісій і funding; Jev USD окремо. Перемога зі збитком не є прибутковістю."
          :"Історичний регламент: Cash бере участь у рейтингу. Σnet після комісій і funding; Jev USD окремо."));
        card.append(box);
        if(options.reviewHref||options.replayHref){const links=el("div","col-battle-links");for(const [href,label] of [[options.replayHref,"Дивитися повтор"],[options.reviewHref,"Розбір результату"]])if(href){const a=el("a","col-text-button",label);a.href=href;links.append(a);}card.append(links);}
        if(scene)render();
      },
      inspect:select,
      destroy(){disposed=true;cancelAnimationFrame(raf);mobile.removeEventListener("change",resizeTabs);host.replaceChildren();}
    };
  }
  return {number,pnl,percent,plural,anchors,project,encounter,create,clock,clockLong,eventLabel,phaseLabel,runLabel,failureNotice,stepLabel,evidenceLabel,durationLabel,explain,battleRows,battleSummary,entriesText,GLOSSARY,GUIDE};
});
