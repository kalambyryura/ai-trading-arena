"use strict";
/* Public static league page. Reads only the JSON embedded at build time (receipted battles); no requests, tokens,
   owner controls or financial routes. Pure view-model functions are exported for Node tests; the DOM is textContent only. */
((root, factory) => {
  const node = typeof module === "object" && module.exports;
  const api = factory(node ? require("./battle-facts.js") : root.ArenaBattleFacts, node ? require("./league-stats.js") : root.ArenaLeagueStats,
    node ? require("./arena-coliseum.js") : root.ArenaColiseum);
  if (node) module.exports = api; else { root.ArenaPublicSite = api; api.mount(document); }
})(typeof window === "undefined" ? {} : window, (Facts, Stats, Coliseum) => {
  const MODES = {OKX_LIVE:"LIVE · реальні кошти на біржі OKX", OKX_DEMO:"DEMO · тестові кошти біржі OKX", PAPER:"PAPER · модельне виконання без біржі"};
  const KINDS = {human:"людина", model:"ШІ-модель", example:"приклад"};
  const EXECUTION = {rules:"лише правила (RULES_ONLY)", shared_jev:"правила + спільна перевірка Jev"};
  const STATUS = {DONE:"завершено", RUNNING:"триває", DRAINING:"завершує позиції", CLOSING:"закриває позиції", PAUSED:"пауза", READY:"готується"};
  const VERDICTS = {ABOVE_CASH:"вище за Cash", BELOW_CASH:"нижче за Cash", INDISTINGUISHABLE:"не відрізнити від Cash", FEW_DATA:"мало даних", NO_DATA:"немає даних", BASELINE:"еталон"};
  const EVIDENCE = {ACTUAL:"реальні записані дані", MODEL:"модельні угоди", SYNTHETIC:"вигадані дані (тест)"};
  const NOTE = "Ліга вимірює стратегії; поки жодна не довела перевагу над Cash після витрат.";
  const SEED = "arena-public-v1", ID = /^[a-z0-9]{32}$/, NBSP = " ";
  const num = v => v === null || v === undefined || v === "" || typeof v === "boolean" || !Number.isFinite(Number(v)) ? null : Number(v);
  const text = (v, fallback = "") => typeof v === "string" && v.trim() ? v.trim() : fallback;
  const plural = (n, one, few, many) => { const a = Math.abs(n) % 100, b = a % 10; return a > 10 && a < 20 ? many : b === 1 ? one : b >= 2 && b <= 4 ? few : many; };
  const counted = (n, one, few, many) => n === null ? "—" : n + NBSP + plural(n, one, few, many);
  function usdt(value, signed = true) {
    const n = num(value); if (n === null) return "—";
    const size = Math.abs(n), digits = size && size < .0001 ? size.toFixed(8).replace(/\.?0+$/, "") : size.toFixed(4);
    return (n < 0 ? "−" : signed && n > 0 ? "+" : "") + digits.replace(".", ",");
  }
  // UTC and Kyiv side by side: the journal is UTC, the audience is in Ukraine.
  function when(value) {
    const ms = typeof value === "number" ? value : Date.parse(value);
    if (!Number.isFinite(ms)) return {utc:"—", kyiv:""};
    const utc = new Date(ms).toISOString().slice(0, 16).replace("T", " ") + " UTC";
    let kyiv = "";
    try { kyiv = new Intl.DateTimeFormat("uk-UA", {day:"2-digit", month:"short", hour:"2-digit", minute:"2-digit", timeZone:"Europe/Kyiv"}).format(ms) + " Київ"; } catch { kyiv = ""; }
    return {utc, kyiv};
  }
  function author(row) {
    if (!row || row.id === "cash") return "не торгує · net = 0";
    const name = text(row.author_name), model = text(row.model);
    const parts = [name, KINDS[row.author_kind], model && model !== name ? model : ""].filter(Boolean);
    return parts.length ? parts.join(" · ") : "автор не вказаний";
  }
  // Share of the stake for one battle; tiny values keep three decimals instead of "<0,01 %".
  function stakeShare(net, allocation) {
    const n = num(net), a = num(allocation);
    if (n === null || !a) return "";
    const p = n / a * 100, size = Math.abs(p);
    if (size < 0.0005) return "";
    return (p < 0 ? "−" : "+") + size.toFixed(size < 0.1 ? 3 : 2).replace(".", ",") + NBSP + "%";
  }
  function cardWhy(row, top) {
    if (row.excludedReason) return row.excludedReason === "no_filled_entry" ? "не взяв участі: немає виконаного входу" : "не ранжовано: запис неповний";
    if (row.id === "cash") return !row.top ? "" : top && top.length > 1 ? "нічия на першому місці" : "інші учасники закінчили в мінусі";
    return row.why || (row.top ? (top && top.length > 1 ? "нічия на першому місці" : "найбільший net у бою") : "");
  }
  const compare = (a, b) => Facts ? Facts.compareDecimal(a, b) : (num(a) === null || num(b) === null ? null : Math.sign(num(a) - num(b)));

  /* league: battles.json "league" {battles, frequencies, cohorts}  ->  {groups:[{cohort, short, title, battles, rows:[...]}], headline} */
  function standings(league) {
    const cohorts = new Map((league && Array.isArray(league.cohorts) ? league.cohorts : []).filter(c => c && typeof c.cohort === "string").map(c => [c.cohort, c]));
    const grouped = new Map();
    for (const row of league && Array.isArray(league.frequencies) ? league.frequencies : []) {
      if (!row || typeof row.cohort !== "string") continue;
      if (!grouped.has(row.cohort)) grouped.set(row.cohort, []);
      grouped.get(row.cohort).push(row);
    }
    const order = [...cohorts.keys()].filter(key => grouped.has(key)).concat([...grouped.keys()].filter(key => !cohorts.has(key)));
    const intervals = Stats && league && Array.isArray(league.battles) ? Stats.intervals(league.battles, {seed:SEED}) : {};
    const groups = order.map(cohort => {
      const meta = cohorts.get(cohort) || {}, allocation = num(meta.allocation_usdt);
      const activeOnly = meta.score_policy?.id === "filled_entry_net_v1";
      const eligible = row => !activeOnly || row.id !== "cash" && num(row.participated_battles) > 0;
      const sorted = [...grouped.get(cohort)].sort((a, b) => Number(eligible(b)) - Number(eligible(a)) || (compare(b.net_sum_usdt, a.net_sum_usdt) || 0) || String(a.id).localeCompare(String(b.id)));
      let rank = 0, rankedCount = 0, previous = null;
      const rows = sorted.map(row => {
        let place = null;
        if (eligible(row)) {
          rankedCount += 1;
          if (previous === null || compare(row.net_sum_usdt, previous) !== 0) rank = rankedCount;
          place = rank; previous = row.net_sum_usdt;
        }
        const cash = row.id === "cash", entry = cash ? null : intervals[cohort + ":" + row.id] || null;
        const verdict = cash ? "BASELINE" : Stats ? Stats.verdict(entry) : "NO_DATA";
        const mean = num(row.net_mean_usdt);
        return {id:String(row.id), rank:place, cash, activeOnly, name: cash ? "Cash" : text(row.name, String(row.id)), author: author(row),
          version: Number.isInteger(row.version) && !cash ? row.version : null, battles: num(row.comparable_battles),
          participated: num(row.participated_battles), noEntry: num(row.no_entry_battles),
          net: row.net_sum_usdt ?? null, mean: row.net_mean_usdt ?? null, percent: allocation && mean !== null ? mean / allocation * 100 : null,
          above: num(row.above_cash), at: num(row.at_cash), below: num(row.below_cash), sole: num(row.sole_wins), shared: num(row.shared_top),
          interval: entry && entry.status === "OK" ? {low:entry.low, high:entry.high} : null, sample: entry ? {n:entry.n, days:entry.days, status:entry.status} : null, verdict};
      });
      const strategies = rows.filter(r => !r.cash).length;
      const title = [MODES[meta.mode] || text(meta.mode), text(meta.cohort_mark), text(meta.duration_label), allocation !== null ? String(allocation).replace(".", ",") + " USDT на учасника" : "",
        strategies + " " + plural(strategies, "стратегія", "стратегії", "стратегій") + " + Cash", EXECUTION[meta.execution_mode],
        activeOnly ? "перемога після входу" : meta.score_policy ? "історичний регламент з Cash" : ""].filter(Boolean).join(" · ");
      return {cohort, short: cohort.slice(0, 8), mode: meta.mode || null, title, activeOnly, battles: num(meta.battles) ?? Math.max(0, ...rows.map(r => r.battles || 0)), rows};
    });
    const results = groups.flatMap(g => g.rows).filter(r => !r.cash && r.net !== null);
    const above = results.filter(r => compare(r.net, "0") > 0).length, battles = groups.reduce((total, g) => total + g.battles, 0);
    const judged = results.filter(r => ["ABOVE_CASH", "BELOW_CASH", "INDISTINGUISHABLE"].includes(r.verdict)), steady = judged.filter(r => r.verdict === "ABOVE_CASH").length;
    const headline = !groups.length ? "Ще немає порівнянних завершених боїв: таблиця з’явиться після першого звіреного бою за спільним регламентом."
      : "Вище за Cash за сумою net: " + above + " з " + results.length + " " + plural(results.length, "результату", "результатів", "результатів") + " · "
        + battles + " " + plural(battles, "порівнянний бій", "порівнянні бої", "порівнянних боїв") + (groups.length > 1 ? " у " + groups.length + " групах." : ".")
        + (judged.length ? " Стабільно вище за Cash (90 % інтервал): " + steady + " з " + judged.length + "." : " Висновок щодо Cash — після 30 боїв.");
    return {groups, above, results: results.length, battles, headline};
  }

  /* league.battles rows -> one list item per battle, newest first as built */
  function battleList(league) {
    return (league && Array.isArray(league.battles) ? league.battles : []).filter(b => b && typeof b.id === "string").map(b => {
      const score = b.score || {}, names = new Map((Array.isArray(b.participants) ? b.participants : []).map(p => [p.id, text(p.name, String(p.id))]));
      names.set("cash", "Cash");
      const name = id => names.get(id) || String(id);
      let result;
      if (score.status === "SOLE_WIN") result = score.sole_winner === "cash" ? "Переміг Cash: усі ранжовані учасники закінчили в мінусі" : "Переміг " + name(score.sole_winner);
      else if (score.status === "TIE_AT_TOP") result = "Спільне перше місце: " + (score.shared_top || []).map(name).join(", ");
      else if (score.status === "NO_CONTEST") result = "Без переможця: жодного виконаного входу";
      else if (score.status === "INELIGIBLE" && score.reason === "example_only") result = "Технічна проба, не рахується";
      else if (score.status === "INELIGIBLE") result = "Непорівнянний бій: запис неповний або неоднакові ставки";
      else result = b.status === "DONE" ? "Підсумок ще не звірено" : "Бій ще не завершено";
      if (score.score_policy?.id === "filled_entry_net_v1" && ["SOLE_WIN", "TIE_AT_TOP"].includes(score.status) &&
          (score.known_results || []).some(r => (score.shared_top || []).includes(r.id) && compare(r.net_usdt, "0") < 0)) result += " · зі збитком, нижче Cash";
      const stamp = when(b.created_at), id = String(b.id);
      return {id, label: text(b.display_label, id), mode: b.mode || null, modeText: MODES[b.mode] || text(b.mode, "режим невідомий"),
        status: b.status || null, statusText: STATUS[b.status] || text(b.status, "—"), incomplete: b.incomplete === true,
        utc: stamp.utc, kyiv: stamp.kyiv, duration: Coliseum && Coliseum.durationLabel ? Coliseum.durationLabel(b.duration_seconds) : "",
        participants: [...names.entries()].filter(([id]) => id !== "cash").map(([, value]) => value), result,
        page: ID.test(id) && typeof b.page === "string" && b.page === "battle-" + id + ".html" ? b.page : null,
        evidence: EVIDENCE[b.evidence_kind] || null, hash: typeof b.package_hash === "string" ? b.package_hash.slice(0, 12) : null};
    });
  }

  /* data: the embedded {battles, latest:{id, facts}} -> the latest-battle card or null */
  function latestCard(data) {
    const latest = data && data.latest, league = data && data.battles && data.battles.league;
    if (!latest || !latest.facts || !Facts) return null;
    const bf = Facts.factsForBattle(latest.facts), headline = Facts.battleHeadline(bf);
    const item = battleList(league).find(b => b.id === latest.id) || null;
    const row = league && Array.isArray(league.battles) ? league.battles.find(b => b && b.id === latest.id) : null;
    const strategies = (row && Array.isArray(row.participants) ? row.participants : []).map(p => ({id:p.id, name:p.name, author:{name:p.author_name, model:p.model}}));
    const allocation = ((latest.facts.participants || [])[0] || {}).allocation_usdt;
    const top = bf.top && Array.isArray(bf.top.ids) ? bf.top.ids : [];
    const rows = (Coliseum && Coliseum.battleRows ? Coliseum.battleRows(bf, {strategies, allocation}) : []).map(row => ({...row,
      money: row.id === "cash" ? "0" : row.netText + (stakeShare(row.net, allocation) ? " · " + stakeShare(row.net, allocation) : ""),
      reason: cardWhy(row, top)}));
    return {id: latest.id, headline, rows, item, final: bf.match.final, mode: bf.match.mode, source: text(latest.facts.source)};
  }

  const el = (doc, tag, cls = "", value) => { const node = doc.createElement(tag); if (cls) node.className = cls; if (value !== undefined) node.textContent = String(value); return node; };
  function table(doc, labels, rows, each) {
    const wrap = el(doc, "div", "public-table-scroll"), grid = el(doc, "table", "public-table"), head = el(doc, "thead"), line = el(doc, "tr"), body = el(doc, "tbody");
    for (const label of labels) { const cell = el(doc, "th", "", label); cell.scope = "col"; line.append(cell); }
    head.append(line); grid.append(head, body); wrap.append(grid);
    for (const row of rows) {
      const line = each(row);
      Array.from(line.children).forEach((cell, index) => { if (cell.tag === undefined ? cell.tagName === "TD" : cell.tag === "td") cell.dataset.label = labels[index]; });
      body.append(line);
    }
    return wrap;
  }
  function renderLatest(doc, host, card) {
    host.replaceChildren();
    if (!card) return;
    host.append(el(doc, "h2", "", card.final ? "Останній завершений бій" : "Останній бій"));
    if (card.item) {
      const badge = el(doc, "p", "public-mode", card.item.modeText); badge.dataset.mode = String(card.item.mode || "");
      const duration = card.item.duration && !card.item.label.includes(card.item.duration) ? card.item.duration : "";
      host.append(badge, el(doc, "p", "public-meta", [card.item.label, card.item.utc + (card.item.kyiv ? " · " + card.item.kyiv : ""), duration, card.item.evidence].filter(Boolean).join(" · ")));
    }
    host.append(el(doc, "p", "public-headline", card.headline));
    host.append(table(doc, ["Місце", "Учасник", "Результат, USDT", "Угоди", "Чому так"], card.rows, row => {
      const line = el(doc, "tr"); if (row.id === "cash") line.dataset.cash = "true";
      const who = el(doc, "th"); who.scope = "row"; who.append(el(doc, "strong", "", row.name));
      if (row.author || row.model) who.append(el(doc, "span", "public-author", [row.author, row.model].filter(Boolean).join(" · ")));
      line.append(el(doc, "td", "public-rank", row.place === null ? "—" : row.place), who,
        el(doc, "td", "", row.money), el(doc, "td", "", row.outcomeText), el(doc, "td", "public-why", row.reason));
      return line;
    }));
    if (card.item && card.item.page) { const link = el(doc, "p", "public-nav"); const a = el(doc, "a", "", "Повтор бою з перевіреним пакетом →"); a.href = card.item.page; link.append(a); host.append(link); }
  }
  function renderStandings(doc, host, model) {
    host.replaceChildren();
    host.append(el(doc, "h2", "", "Таблиця ліги"), el(doc, "p", "public-headline", model.headline));
    if (!model.groups.length) return;
    host.append(el(doc, "p", "research-intro", "Нові бої: переможець має виконаний вхід і найбільший net USDT після комісій та funding. Cash — еталон 0; без входів переможця немає. Перемога зі збитком не означає прибутковості. Історичні бої зберігають свій регламент. Суми включають бої без входів: це економічна аналітика, а не доказ переваги."));
    for (const group of model.groups) {
      const section = el(doc, "section", "public-group");
      const meta = el(doc, "p", "public-meta", [group.battles + " " + plural(group.battles, "бій", "бої", "боїв"), "група " + group.short].join(" · "));
      meta.title = "Повний ідентифікатор групи: " + group.cohort;
      section.append(el(doc, "h3", "", group.title || "Група без опису"), meta);
      section.append(table(doc, ["Місце", "Стратегія", "Боїв", "Σ net, USDT", "За бій, USDT", "90 % інтервал за бій", "Проти Cash", "Перемоги", "Висновок"], group.rows, row => {
        const line = el(doc, "tr"); if (row.cash) line.dataset.cash = "true";
        const who = el(doc, "th"); who.scope = "row"; who.append(el(doc, "strong", "", row.name), el(doc, "span", "public-author", row.author + (row.version !== null ? " · v" + row.version : "")));
        const perBattle = usdt(row.mean) + (row.percent !== null && !row.cash ? " · " + (row.percent > 0 ? "+" : row.percent < 0 ? "−" : "") + Math.abs(row.percent).toFixed(3).replace(".", ",") + NBSP + "% від ставки" : "");
        const range = row.cash ? "—" : row.interval ? usdt(row.interval.low) + " … " + usdt(row.interval.high) : row.sample ? "мало даних · " + row.sample.n + " з 30" : "—";
        const cash = row.cash ? "—" : [["краще", row.above], ["нарівні", row.at], ["гірше", row.below]].map(([label, value]) => label + NBSP + (value ?? "—")).join(" · ");
        const badge = el(doc, "span", "public-verdict", VERDICTS[row.verdict] || "—"); badge.dataset.verdict = row.verdict || "NO_DATA";
        const verdict = el(doc, "td"); verdict.append(badge);
        const participation = row.activeOnly && !row.cash ? " · входив " + (row.participated ?? "—") + " · без входу " + (row.noEntry ?? "—") : "";
        line.append(el(doc, "td", "public-rank", row.rank ?? "—"), who, el(doc, "td", "", (row.battles ?? "—") + participation), el(doc, "td", "", usdt(row.net)), el(doc, "td", "", perBattle),
          el(doc, "td", "", range), el(doc, "td", "", cash), el(doc, "td", "", "одноосібні" + NBSP + (row.sole ?? "—") + " · спільні" + NBSP + (row.shared ?? "—")), verdict);
        return line;
      }));
      host.append(section);
    }
    host.append(el(doc, "p", "research-muted", "90 % інтервал — межі середнього net за бій з урахуванням того, що бої однієї доби UTC мають спільний ринок (bootstrap за добами). «Вище» чи «нижче за Cash» — лише коли весь інтервал по один бік від нуля і є щонайменше 30 боїв. Це опис невизначеності, а не гарантія майбутнього результату."));
  }
  function renderBattles(doc, host, items) {
    host.replaceChildren();
    host.append(el(doc, "h2", "", "Усі бої (" + items.length + ")"));
    if (!items.length) { host.append(el(doc, "p", "public-empty", "Схвалених до показу боїв ще немає.")); return; }
    const list = el(doc, "ul", "public-battle-list");
    for (const item of items) {
      const li = el(doc, "li", "public-battle");
      const badge = el(doc, "span", "public-mode", item.modeText); badge.dataset.mode = String(item.mode || "");
      li.append(el(doc, "strong", "", item.label), badge,
        el(doc, "span", "public-meta", [item.utc + (item.kyiv ? " · " + item.kyiv : ""), item.duration && !item.label.includes(item.duration) ? item.duration : "", item.statusText + (item.incomplete ? " · запис неповний" : ""), item.evidence].filter(Boolean).join(" · ")),
        el(doc, "span", "", "Учасники: " + (item.participants.length ? item.participants.join(", ") : "—") + " + Cash"), el(doc, "span", "", item.result));
      if (item.page) { const a = el(doc, "a", "", "Повтор бою →"); a.href = item.page; li.append(a); }
      if (item.hash) li.append(el(doc, "span", "research-muted", "пакет " + item.hash + "…"));
      list.append(li);
    }
    host.append(list);
  }
  function render(doc, data) {
    const battles = data && data.battles, league = battles && battles.league;
    const note = doc.getElementById("public-note");
    if (note) note.textContent = NOTE + " Усі суми — в USDT після комісій і funding; Cash = 0 бере участь у кожному бою.";
    const latest = doc.getElementById("public-latest"); if (latest) renderLatest(doc, latest, latestCard(data));
    const standingsHost = doc.getElementById("public-standings"); if (standingsHost) renderStandings(doc, standingsHost, standings(league));
    const battlesHost = doc.getElementById("public-battles"); if (battlesHost) renderBattles(doc, battlesHost, battleList(league));
    const guide = doc.getElementById("public-guide");
    if (guide) {
      guide.replaceChildren(); for (const line of Coliseum && Array.isArray(Coliseum.GUIDE) ? Coliseum.GUIDE : []) guide.append(el(doc, "li", "", line));
      if (guide.parentElement && String(guide.parentElement.tagName).toUpperCase() === "DETAILS") guide.parentElement.open = true;
    }
    const built = doc.getElementById("public-built");
    if (built) {
      const stamp = battles && num(battles.built_at_ms) ? when(Number(battles.built_at_ms)) : null;
      built.textContent = (stamp ? "Дані схвалено до показу станом на " + stamp.utc + (stamp.kyiv ? " (" + stamp.kyiv + ")" : "") + ". " : "")
        + (battles && battles.indexable === false ? "Сторінка не індексується пошуковиками." : "");
    }
    return {latest: latestCard(data), standings: standings(league), battles: battleList(league)};
  }
  function mount(doc) {
    const host = doc.getElementById("public-site-data"), note = doc.getElementById("public-note");
    try { render(doc, JSON.parse(host.textContent)); }
    catch (error) { if (note) note.textContent = "Сторінку не вдалося показати: " + error.message; }
  }
  return {MODES, NOTE, usdt, when, author, stakeShare, standings, battleList, latestCard, render, mount};
});
