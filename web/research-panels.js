"use strict";
/* Scoped reusable UI. Callbacks are supplied by the owner page; no fetch or request code. */
((root, factory) => {
  const api = factory(typeof module === "object" && module.exports ? require("./review-model.js") : root.ArenaReviewModel);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ArenaResearchPanels = api;
})(typeof window === "undefined" ? {} : window, Model => {
  const safe = (value, fallback = "Невідомо") => value === undefined || value === null || value === "" ? fallback : String(value);
  const count = value => Number.isSafeInteger(value) && value >= 0 ? String(value) : "Не записано";
  const money = (value, unit = "USDT", signed = true) => {
    if (typeof value === "number") {
      if (!Number.isFinite(value)) return "Невідомо";
      return (signed && value > 0 ? "+" : "") + String(value).replace("-", "−").replace(".", ",") + " " + unit;
    }
    return Model.exact(value, unit, signed);
  };
  const status = value => ({PASS:"Пройдено", FAIL:"Не пройдено", UNKNOWN:"Невідомо", NOT_RECORDED:"Не записано",
    COMPLETE:"Повні", INCOMPLETE:"Неповні", ELIGIBLE:"Допущено", INELIGIBLE:"Не допущено", SETTLED:"Звірено",
    PENDING:"Очікуємо", FLAT:"Без позиції", OPEN:"Відкрита позиція", EXACT:"Точний запис", DERIVED:"Виведено",
    RUNNING:"Нові входи відкриті", DRAINING:"Входи завершено · позиції тривають", PAUSED:"Потрібне відновлення",
    DONE:"Завершено", FAILED:"Помилка спроби", UNEXECUTABLE:"Розмір не виконується",
    observed:"Спостережено", blocked:"Заблоковано", pending:"Очікується", not_observed:"Не спостережено",
    not_applicable:"Не застосовується", SYNTHETIC:"Вигаданий UI fixture", MODEL:"Модельний результат", ACTUAL:"Фактичний журнал"}[value] || safe(value));
  const node = (tag, cls = "", text) => {
    const result = document.createElement(tag); if (cls) result.className = cls;
    if (text !== undefined) result.textContent = String(text); return result;
  };
  const button = (label, callback, cls = "research-button") => {
    const result = node("button", cls, label); result.type = "button";
    result.addEventListener("click", callback); return result;
  };
  // options.cards: on a phone each row becomes a card (the header row is kept for screen readers, every cell gets its column name).
  function table(headers, rows, options = {}) {
    const wrap = node("div", "research-table-scroll"), result = node("table", options.cards ? "research-table research-table-cards" : "research-table");
    const head = node("thead"), headRow = node("tr"), body = node("tbody");
    headers.forEach(text => {const cell=node("th", "", text); cell.scope="col"; headRow.append(cell);});
    head.append(headRow);
    rows.forEach(values => {
      const row = node("tr");
      values.forEach((value, index) => {const cell=node("td"); if (options.cards) cell.dataset.label=headers[index]||""; cell.append(value instanceof Node ? value : node("span", "", safe(value, "—"))); row.append(cell);});
      body.append(row);
    });
    result.append(head, body); wrap.append(result);
    if (!rows.length) wrap.append(node("p", "research-empty", "Записів немає. Це не нульовий результат."));
    return wrap;
  }
  let tabCounter = 0;
  function tabs(host, choices, draw) {
    const key = "research-tabs-" + (++tabCounter), list = node("div", "research-tabs"), content = node("section", "research-tab-panel");
    list.setAttribute("role", "tablist"); content.setAttribute("role", "tabpanel"); content.id = key + "-panel";
    const buttons = choices.map(([id, label], index) => {
      const item = button(label, () => select(index), "research-tab");
      item.id = key + "-" + id; item.setAttribute("role", "tab"); item.setAttribute("aria-controls", content.id);
      item.addEventListener("keydown", event => {
        let next = null;
        if (event.key === "ArrowRight") next = (index + 1) % buttons.length;
        if (event.key === "ArrowLeft") next = (index + buttons.length - 1) % buttons.length;
        if (event.key === "Home") next = 0; if (event.key === "End") next = buttons.length - 1;
        if (next !== null) {event.preventDefault(); select(next); buttons[next].focus();}
      });
      list.append(item); return item;
    });
    let selected = 0;
    function select(index) {
      selected = index; buttons.forEach((item, i) => {
        item.setAttribute("aria-selected", String(i === index)); item.tabIndex = i === index ? 0 : -1;
      });
      content.setAttribute("aria-labelledby", buttons[index].id); content.replaceChildren();
      draw(choices[index][0], content); return content;
    }
    host.append(list, content); select(0);
    return {refresh(){select(selected);},select(id){const index=choices.findIndex(row=>row[0]===id);if(index>=0)select(index);}};
  }
  function factRows(values) {
    const result = node("dl", "research-facts");
    values.forEach(([label, value]) => {result.append(node("dt", "", label), node("dd", "", safe(value)));});
    return result;
  }
  function evidenceButton(reference, options) {
    if (!reference || !options.onEvent) return node("span", "research-muted", "Без посилання");
    const seq = typeof reference === "object" ? reference.seq : reference;
    return button("Подія " + seq, () => options.onEvent(seq), "research-link");
  }
  function normalizeParticipant(participant) {
    const costs = participant.costs || {}, funnel = participant.funnel || {};
    const ledger = participant.trade_ledger || {};
    const stages = Array.isArray(funnel) ? funnel : Array.isArray(funnel.stages) ? funnel.stages : [
      ["checks", "Перевірки правил", funnel.entry?.checks ?? funnel.checks, "перевірки"],
      ["signals", "Формальні сигнали", funnel.entry?.signals, "перевірки з сигналом · свічки можуть повторюватись"],
      ["bars", "Окремі завершені свічки", participant.quality?.distinct_closed_bars, "свічки за записаними IDs"],
      ["formal-bars", "Окремі свічки з сигналом", funnel.formal_bars ?? funnel.entry?.formal_bars, "свічки"],
      ["episodes", "Епізоди сигналу", funnel.derived_episodes ?? funnel.entry?.episodes, "епізоди · виведено"],
      ["jev", "Запити Jev", costs.jev?.requests ?? funnel.entry?.jev_calls, "запити"],
      ["gate", "Дозволені входи", funnel.entry?.jev_allows, "дозволи gate"],
      ["intents", "Наміри ордера", funnel.order_intents, "наміри"],
      ["fills", "Заповнені входи", funnel.entry_filled_orders ?? funnel.entry?.entry_filled_orders, "ордери"],
      ["cycles", "Завершені цикли за раунд", funnel.round_trips, "цикли за весь раунд"]
    ].map(([id,label,value,unit])=>({id,label,count:value,unit}));
    return {...participant, costs, stages,
      trade_ledger: Array.isArray(ledger) ? ledger : ledger.rows || [],
      round_trips: Array.isArray(ledger.round_trips) ? ledger.round_trips : [],
      trade_ledger_limitations: Array.isArray(ledger.limitations) ? ledger.limitations : [],
      quality: participant.quality || {}, coverage: participant.coverage || {}};
  }
  function adaptBundle(bundle) {
    if (bundle.schema_version !== 1 || !bundle.match || !Array.isArray(bundle.participants)) {
      throw new Error("Потрібен BattleReview schema_version=1 з match і participants");
    }
    return {...bundle, match:{...bundle.match,
      evidence_kind:bundle.match.evidence_kind || (bundle.match.mode==="PAPER"?"MODEL":"ACTUAL")},
      participants: bundle.participants.map(normalizeParticipant)};
  }

  // B-01a: one honest Jev line per participant when the page also loaded bot-waiting.js and battle-facts.js.
  function jevSummary(bot, bundle) {
    const W = typeof window === "undefined" ? null : window.ArenaBotWaiting, F = typeof window === "undefined" ? null : window.ArenaBattleFacts;
    const view = W?.jev?.({id:bot.id}, {events:bundle.events || [], facts:F});
    if (!view || !view.counts?.decisions) return null;
    const c = view.counts, range = c.min === null ? "" : " · confidence " + (c.min === c.max ? String(c.min) : c.min + "–" + c.max).replace(/[.]/g, ",");
    const gate = c.threshold ?? 0.7;
    return view.summary.replace(/[.]$/, "") + range + " (поріг " + String(gate).replace(".", ",") + " ⇔ p(proceed) ≥ " + String(Math.round((1 + gate) / 2 * 100) / 100).replace(".", ",") + "). " + view.note;
  }

  function mountReview(host, original, options = {}) {
    const bundle = adaptBundle(original); host.replaceChildren(); host.classList.add("research-shell");
    const header=node("header","research-heading"), heading=node("div");
    heading.append(node("p","research-kicker","РОЗБІР БОЮ"),node("h2","","Рішення, результат і надійність"));
    heading.append(node("p","research-muted",safe(bundle.match.id) + " · " + status(bundle.match.evidence_kind) +
      " · " + (bundle.match.scope === "PREFIX" ? "До вибраної події" : "Підсумковий розбір журналу")));
    header.append(heading);
    const bundleButton = button("Пакет для авторів", ()=>options.onBundle?.(bundle), "research-button");
    const release = bundle.author_bundle || {};
    bundleButton.disabled = !options.onBundle || !(options.bundleVerified === true ||
      release.state === "RELEASED" && release.integrity === "PASS");
    bundleButton.title = bundleButton.disabled ? safe(release.reason,"Очікуємо спільного випуску перевіреного пакета") :
      "Однаковий пакет і cutoff для всіх авторів; це завантаження, не надсилання";
    header.append(bundleButton);host.append(header);
    const controls=node("div","research-controls"), label=node("label","","Учасник ");
    const select=node("select"), names=bundle.participants;
    names.forEach(participant=>{const option=node("option","",participant.name);option.value=participant.id;select.append(option);});
    label.append(select);controls.append(label);host.append(controls);
    let selectedId=names.some(p=>p.id===options.botId)?options.botId:names[0]?.id;select.value=selectedId;
    const selected=()=>names.find(p=>p.id===selectedId);
    const tabAPI=tabs(host,[["funnel","Рішення"],["ledger","Угоди та витрати"],["quality","Якість даних"]],(id,content)=>{
      const bot=selected();if(!bot){content.append(node("p","research-empty","Учасників не записано."));return;}
      if(id==="funnel"){
        content.append(node("p","research-intro","Перевірки, свічки, епізоди й угоди мають різні одиниці. Proceed Jev ще не означає дозвіл gate або виконану угоду."));
        content.append(table(["Етап","Кількість","Одиниця","Доказ"],bot.stages.map(s=>[
          safe(s.label),count(s.count),safe(s.unit,"Не записано"),evidenceButton(s.reference,options)])));
        if(Array.isArray(bot.explain))content.append(table(["Умова","Отримано","Потрібно","Стан","Доказ"],bot.explain.map(row=>[
          safe(row.label||row.path),safe(row.actual,"Не записано"),safe(row.expected,"Не записано"),status(row.state),evidenceButton(row.reference,options)])));
        content.append(node("h3","","Перевірки за фазами"));
        const phases=bot.phase_diagnostics?.phases;
        if(phases && typeof phases==="object"){
          content.append(table(["Фаза","Перевірки","Унікальні завершені свічки","Повторні перевірки"],
            [["entry","Вхід"],["add","Докупівля"],["hold","Утримання"],["exit","Вихід"]].map(([key,label])=>{
              const p=phases[key]||{};return [label,count(p.evaluations),count(p.unique_closed_bars),count(p.rechecks)];
            })));
          for(const [key,label] of [["entry","Вхід"],["add","Докупівля"],["hold","Утримання"],["exit","Вихід"]]){
            const p=phases[key]||{};content.append(node("h4","",label));
            content.append(table(["Умова","Не виконано · перевірки","Невідомо · перевірки","Не виконано · завершені свічки"],
              (Array.isArray(p.failed_predicates)?p.failed_predicates:[]).map(c=>[
                [c.feature,c.op,c.expected].map(x=>safe(x,"?")).join(" "),count(c.false_evaluations),count(c.unknown_evaluations),count(c.false_unique_closed_bars)])));
          }
          content.append(node("p","research-muted","Копії рішень не збільшують кількість перевірок. Повторні перевірки наведено окремо. Невиконана умова виходу означає, що вона не спрацювала. Окрема умова вкладеної групи не завжди визначає результат усієї групи."));
          if(bot.phase_diagnostics.unclassified_check_event_seqs?.length || bot.phase_diagnostics.unclassified_position_checks)
            content.append(node("p","research-unknown","Для частини записаних перевірок фазу не встановлено; їх не зараховано до входу, докупівлі, утримання чи виходу."));
        }else content.append(node("p","research-muted","Фази не записано у старому пакеті. Відсутнє значення не дорівнює нулю."));
        const legacy=node("details","research-provenance");legacy.append(node("summary","","Змішаний підсумок старого формату"),
          node("p","research-muted","Тут об'єднано різні фази та копії подій. Ці числа не є кількістю відмов лише для входу."),
          node("p","research-muted",Object.entries(bot.funnel?.failed_features||{}).map(([k,v])=>k+": "+v).join(", ")||"Не записано"));content.append(legacy);
        if(bundle.diagnostics_context?.market_snapshots==="UNAVAILABLE")content.append(node("p","research-unknown","Запис ринку недоступний. Кількість завершених свічок і межі прогалин показано лише там, де їх підтверджують збережені події."));
        const jevLine=jevSummary(bot,bundle);if(jevLine)content.append(node("p","research-note",jevLine));
        content.append(factRows([["Зараз entries",count(bot.current_entries)],["За раунд циклів",count(bot.seasonal_round_trips??bot.funnel?.round_trips)],
          ["Причина очікування",bot.wait_reason?.message||bot.wait_reason||"Не записано"]]));
      }
      if(id==="ledger"){
        const c=bot.costs;
        content.append(factRows([["Валовий PnL",money(c.gross_pnl_usdt)],["Комісії · зі знаком",money(c.signed_fees_usdt)],
          ["Funding · зі знаком",money(c.signed_funding_usdt)],["Чистий PnL",money(c.net_usdt)],["Зміна cash",money(c.cash_delta_usdt)],
          ["Облік",c.complete===true?"Звірено":c.complete===false?"Неповний":"Повнота невідома"]]));
        const jev=c.jev||{};
        content.append(node("p","research-note","Jev окремо: " + money(jev.known_cost_usd,"USD",false) +
          " · " + count(jev.requests) + " запитів · " + count(jev.unknown_billing_requests) + " рахунків невідомі" +
          (String(jev.source||"").includes("estimate")?" · розподіл між ботами є оцінкою":"")));
        // Exchange ledger rows count OKX contracts (quantity and position after); PAPER rows count coins of the season instrument.
        const unit=row=>row.source==="exchange_incremental_contracts"?" контрактів":row.source==="paper_incremental_btc"?" "+Model.displayCoin(bundle.match):"";
        const counted=(value,row)=>value===undefined||value===null||value===""?"Не записано":String(value)+unit(row);
        content.append(table(["Подія / роль","Кількість","Залишок після","Валовий PnL","Комісії","Funding","Доказ"],bot.trade_ledger.map(row=>[
          safe(row.seq,"—")+" · "+safe(row.role||row.status),counted(row.quantity,row),
          counted(row.position_after,row),
          money(row.gross_pnl_usdt??row.gross_usdt),money(row.signed_fees_usdt),money(row.signed_funding_usdt),
          evidenceButton(row.reference||row.seq||row.exit_event_seq,options)])));
        if(bot.round_trips.length)content.append(table(["Цикл","Вхід","Закриття","Стан"],bot.round_trips.map(row=>[
          count(row.number),evidenceButton(row.entry_seq,options),evidenceButton(row.close_seq,options),row.closed?"Flat fill спостережено":"Ще відкритий"])));
        const reasons={g1_stop_loss:"G1: абсолютний стоп",g1_take_profit:"G1: геометрична ціль",g1_failed_breakout:"G1: невдалий пробій підтверджено",g1_max_hold:"G1: 24 години утримання",g1_owned_paper_state_mismatch:"G1: стан позиції неузгоджений",g1_owned_paper_state_unreadable:"G1: стан позиції не читається",rules_exit:"Правила виходу",formal_exit:"Правила виходу",max_hold:"Час утримання за рецептом",stop_loss:"Обмеження збитку",take_profit:"Фіксація прибутку",owner_stop:"Зупинка власником",stop_requested:"Запит зупинки",recovery:"Відновлення",risk_limit:"Обмеження ризику",deadline:"Дедлайн старого протоколу",cost_checkpoint:"Перевірка витрат",formal_reduce:"Часткове скорочення"};
        if(Array.isArray(bot.trade_explanations))content.append(table(["Цикл","Намір входу → виконання, с","Намір закриття → виконання, с","Умова виходу → виконання, с","Причина виходу"],bot.trade_explanations.map(t=>[
          count(t.number),safe(t.entry_intent_to_fill_seconds,"Не записано"),safe(t.close_intent_to_fill_seconds,"Не записано"),safe(t.exit_trigger_to_fill_seconds,"Не записано"),reasons[t.exit_reason]||"Не записано"])));
        if(bundle.match.mode==="PAPER")content.append(node("p","research-note","Виконання та витрати PAPER змодельовано. Фактичні біржові гроші тут не використовуються. Спред і прослизання враховано в ціні виконання; окремі складові витрат не записані."));
        bot.trade_ledger_limitations.forEach(text=>content.append(node("p","research-muted",text)));
        content.append(node("p","research-muted","Намір і підтвердження прийняття ордера ще не означають виконання. Часткове закриття не завершує цикл. Відомі збитки зберігаються й для неповного бою."));
      }
      if(id==="quality"){
        const quality=bot.quality, coverage=bot.coverage;
        content.append(factRows([["Settlement",status(quality.settlement?.status||quality.settlement)],
          ["Доступність / прогалини",status(quality.availability?.status||quality.availability||quality.data_status||coverage.status)],
          ["Порівнянність",status(quality.competitive_validity?.status||quality.competitive_validity||"Не обчислено")],
          ["Чому",quality.reason||coverage.reason||"Пояснення не записано"]]));
        const timing=quality.timing||[
          {label:"Окремі завершені свічки",value:quality.distinct_closed_bars,source_kind:quality.distinct_closed_bars_source==="recorded_bar_time_ms"?"EXACT":"NOT_RECORDED"},
          {label:"Записані Jev latency, ms",value:quality.jev_latency_ms?.length?quality.jev_latency_ms.join(", "):null,
            source_kind:quality.jev_latency_ms?.length?"EXACT":"NOT_RECORDED"},
          {label:"Exact exit trigger",value:quality.exact_exit_trigger_recorded===true?"Є запис":null,
            source_kind:quality.exact_exit_trigger_recorded===true?"EXACT":"NOT_RECORDED",
            reference:quality.exit_trigger_evidence?.[0]?.seq}];
        content.append(table(["Час / перевірка","Значення","Походження","Доказ"],timing.map(row=>[
          safe(row.label),safe(row.value,"Не записано"),status(row.source_kind||"NOT_RECORDED"),evidenceButton(row.reference,options)])));
        if(Array.isArray(coverage.rows)){
          content.append(node("h3","","Покриття гілок циклу"));
          content.append(node("p","research-muted",count(coverage.observed_required_count)+" / "+count(coverage.required_count)+
            " спостережених гілок. Це механічне покриття, окреме від придатності до рейтингу."));
          content.append(table(["Гілка","Стан","Деталь","Доказ"],coverage.rows.map(row=>[
            row.label,status(row.status),row.detail,evidenceButton(row.event_seq||row.seq,options)])));
        }
        content.append(table(["Причина недоступності","Тривалість","Вплив"],(coverage.gaps||[]).map(row=>[
          safe(row.kind),safe(row.duration_ms,"Не записано"),safe(row.effect,"Не записано")])));
        if(Array.isArray(bundle.market_gaps) && bundle.market_gaps.length){
          const time=q=>q && Number.isFinite(q.at)?new Date(q.at*1000).toISOString():"Не записано";
          content.append(node("h3","","Ринкові прогалини · перебіг усередині невідомий"));
          content.append(table(["Доказ","Останній запис до · UTC","Перший запис після · UTC","Інтервал між записами, с","Позиції на момент прогалини"],bundle.market_gaps.map(g=>[
            evidenceButton(g.event_seq,options),time(g.before),time(g.recovery),safe(g.known_snapshot_interval_seconds,"Не записано"),
            (g.positions_at_gap||[]).map(p=>safe(names.find(n=>n.id===p.id)?.name,p.id)+": "+status(p.status)).join("; ")])));
          content.append(node("p","research-muted","Інтервал між спостереженнями не встановлює тривалість збою. Його початок, причина та ціна всередині невідомі. Повний журнал подій не доводить безперервність ринкових даних."));
        }
        if(quality.gap_event_seqs?.length){
          const gaps=node("div","research-row-actions");
          quality.gap_event_seqs.forEach(seq=>gaps.append(evidenceButton(seq,options)));content.append(gaps);
        }
        (quality.timing_limitations||[]).forEach(text=>content.append(node("p","research-muted",text)));
        content.append(node("p","research-muted","Позиція або cooldown не є outage. Flat/settled не усуває прогалини даних. Старі відсутні exact timestamps позначено «Не записано»."));
      }
    });
    const provenance=node("details","research-provenance");provenance.append(node("summary","","Походження та хеші"));
    const origin=node("div");provenance.append(origin);host.append(provenance);
    function updateOrigin(){origin.replaceChildren(factRows([["Manifest",bundle.match.manifest_sha256],
      ["Source / core",bundle.match.source_sha256||bundle.match.code_sha256],["Recipe",selected()?.recipe_sha256],
      ["Bundle",bundle.integrity?.bundle_sha256],["Projection",bundle.projection_version||bundle.projection]]));}
    updateOrigin();select.addEventListener("change",()=>{selectedId=select.value;tabAPI.refresh();updateOrigin();});
    return {selectView:id=>tabAPI.select(id),selectBot(id){if(names.some(p=>p.id===id)){selectedId=id;select.value=id;tabAPI.refresh();updateOrigin();}}};
  }

  function mountWorkshopTools(host, report, options={}) {
    host.replaceChildren();host.classList.add("research-shell");
    host.append(node("h2","","Перевірка рецепта"),node("p","research-muted",safe(report.version_name) + " · " + safe(report.evidence_kind)));
    const api=tabs(host,[["lint","Помилки"],["explain","Пояснення"],["lots","Лоти"],["trials","Спроби"],["compare","Порівняння"]],(id,content)=>{
      if(id==="lint")content.append(table(["Поле","Проблема","Рішення"],(report.lint||[]).map(issue=>{
        const field=options.onField?button(issue.path,()=>options.onField(issue.path),"research-link"):issue.path;
        return[field,safe(issue.message),safe(issue.hint,"Без автоматичної зміни рецепта")];})));
      if(id==="explain"){
        content.append(node("p","research-intro","Пояснення використовує один snapshot з його available_at. UNKNOWN блокує новий вхід; майбутня свічка не додається."));
        content.append(table(["Гілка","Отримано","Умова","Стан"],(report.explain||[]).map(row=>[
          safe(row.path),safe(row.actual,"UNKNOWN"),safe(row.expected),status(row.state)])));
      }
      if(id==="lots"){
        // Quantities are coins of the recipe instrument (options.instrument; a recipe without one trades BTC-USDT-SWAP).
        const l=report.lots||{},coin=Model.baseCurrency(options.instrument);
        content.append(factRows([["Бік preview",safe(l.side)],["Цільовий notional",money(l.target_notional_usdt)],["Lot / min qty",safe(l.lot_qty)+" / "+safe(l.min_qty)+" "+coin],
          ["Бажана кількість",safe(l.requested_qty)+" "+coin],["Після округлення вниз",safe(l.rounded_qty)+" "+coin],
          ["Виконуваний notional",money(l.rounded_notional_usdt)],["Результат",status(l.result)],
          ["Snapshot / policy",safe(l.source_at)+" / "+safe(l.policy_hash)]]));
        content.append(node("p","research-note","Це preview під чинним ризиком, без ордера. Відсоток allocation не гарантує мінімального лота; UI не збільшує розмір автоматично."));
        (l.reasons||[]).forEach(reason=>content.append(node("p","research-muted",reason)));
      }
      if(id==="trials"){
        content.append(node("p","research-intro","Усі спроби цієї lineage, включно з помилками й відмовами. Переглянуті дані вже є development data."));
        content.append(table(["№","Версія / набір","Стан","Режим","Net","Угоди","Докладніше"],(report.trials||[]).map(trial=>[
          count(trial.attempt_number),safe(trial.version_name)+" / "+safe(trial.dataset),status(trial.status),
          status(trial.evidence_kind),money(trial.net_usdt),count(trial.trades),
          options.onTrial?button("Відкрити",()=>options.onTrial(trial.id),"research-link"):safe(trial.id)])));
      }
      if(id==="compare"){
        const c=report.compare||{};
        content.append(node("p",c.comparable?"research-note":"research-note research-unknown",safe(c.reason,"Оберіть дві версії та спільні дані.")));
        content.append(table(["Що змінилося","Базова версія","Кандидат"],(c.rows||[]).map(row=>[row.path,safe(row.before),safe(row.after)])));
        content.append(node("p","research-muted","Новий parent/hash/гіпотеза й журнал усіх trials. Кандидат застосовується з наступного lock, після спільного upgrade-вікна; deployed версія не змінюється тут."));
      }
    });return api;
  }

  function mountLeague(host, league, options={}) {
    host.replaceChildren();host.classList.add("research-shell");
    host.append(node("h2","","Історія боїв"),node("p","research-intro","Ліга триває. Бої та версії накопичуються; наступний слот залежить від готовності рахунків і зафіксованого розкладу."));
    const counters=Model.leagueCounters(league.battles||[],league.slots||[]);
    const strip=node("div","research-counter-strip");
    [["Розпочато","started"],["Звірено","settled"],["Порівнянних","comparable"],["Позиції тривають","draining"],["Пропущено слотів","skipped"]]
      .forEach(([label,key])=>strip.append(node("span","",label+" · "+counters[key])));host.append(strip);
    const controls=node("div","research-controls"), modeLabel=node("label","","Режим "), mode=node("select");
    [["all","Усі режими"],["OKX_LIVE","Live"],["OKX_DEMO","Demo"],["PAPER","PAPER"]].forEach(([value,label])=>{
      const option=node("option","",label);option.value=value;mode.append(option);});modeLabel.append(mode);controls.append(modeLabel);host.append(controls);
    const content=node("div");host.append(content);
    const when=value=>{const time=Date.parse(value);return Number.isFinite(time)?new Intl.DateTimeFormat("uk-UA",{day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit",timeZoneName:"short"}).format(time):"";};
    function draw(){
      const battles=(league.battles||[]).filter(b=>mode.value==="all"||b.mode===mode.value);
      content.replaceChildren(table(["Бій · група","Режим","Стан","На учасника","У таблиці ліги","Дії"],battles.map(battle=>{
        const actions=node("div","research-row-actions");
        if(options.onReview)actions.append(button("Розбір",()=>options.onReview(battle.id,battle),"research-link"));
        if(options.onReplay)actions.append(button("Повтор",()=>options.onReplay(battle.id,battle),"research-link"));
        const name=node("span","research-battle-name"),group=battle.cohort?"група "+safe(battle.cohort).slice(0,8):"";
        const detail=(battle.display_label?[when(battle.created_at||battle.label),group]:[group]).filter(Boolean).join(" · ");
        name.append(node("strong","",safe(battle.display_label||battle.label,battle.id)));
        if(detail){const small=node("small","research-muted",detail);if(battle.cohort)small.title="Група: "+safe(battle.cohort);name.append(small);}
        return[name,{OKX_LIVE:"Live",OKX_DEMO:"Demo",PAPER:"PAPER"}[battle.mode]||safe(battle.mode),status(battle.status),
          money(battle.allocation_usdt,"USDT",false),battle.score?.reason==="example_only"?"Технічна проба, не рахується":status(battle.competitive_validity),actions];}),{cards:true}));
    }
    mode.addEventListener("change",draw);draw();
    const policy=node("details","research-provenance");policy.append(node("summary","","Регламент та оновлення версій"));
    policy.append(node("p","research-muted",safe(league.policy_note,"Спільний cutoff і feedback release. Allocation та USD витрати окремі. Sole wins і спільне перше місце рахуються окремо; missing не дорівнює Cash.")));host.append(policy);
  }

  function mountReplayControls(host, replay, options={}) {
    host.replaceChildren();host.classList.add("research-replay");const manifest=replay.packet.manifest;
    const toggle=button("Відтворити",()=>{stopDigest();replay.playing?replay.pause():replay.play();publish(false);});
    const speed=node("select");speed.setAttribute("aria-label","Швидкість повтору");
    [1,10,100,1000].forEach(value=>{const option=node("option","",value+"×");option.value=String(value);speed.append(option);});
    const label=node("label","research-seek","Час повтору "),range=node("input");range.type="range";
    range.min="0";range.max=String(manifest.recording_end_at_ms-manifest.started_at_ms);range.step="1000";range.value="0";
    const elapsedLabel=ms=>{const s=Math.max(0,Math.floor(Number(ms)/1000)),h=Math.floor(s/3600),m=Math.floor(s%3600/60);return "+"+(h?h+" год ":"")+(h||m?m+" хв ":"")+(s%60)+" с від старту";};
    const localClock=ms=>new Intl.DateTimeFormat("uk-UA",{day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit",second:"2-digit",timeZoneName:"short"}).format(ms);
    label.append(range);const clock=node("output","research-replay-clock"),follow=node("select");follow.setAttribute("aria-label","Учасник для переходу до події");
    const all=node("option","","Події всіх");all.value="";follow.append(all);
    manifest.participants.filter(p=>p.avatar_kind!=="baseline").forEach(p=>{const option=node("option","",p.name);option.value=p.id;follow.append(option);});
    const next=button("Наступна подія",()=>{stopDigest();replay.nextAction(follow.value||null);publish(true);});
    // Key moments (B-03b): entries, closes, the admission cut-off, data gaps and the settlement, from the packet's recorded fills.
    const labels=window.ArenaReplayLabels,moments=labels&&labels.moments?labels.moments(replay.packet):[];
    const hint=moments.length?"Ключових моментів: "+moments.length+" — кнопки «Момент» переходять між ними.":"У цьому записі немає ключових моментів.";
    const momentNote=node("p","research-muted research-moment",hint);momentNote.setAttribute("role","status");
    const previousMoment=button("◀ Момент",()=>jump(labels.previousMoment(moments,replay.snapshot().state.seq,follow.value||null),"Раніше ключових моментів немає."));
    const nextMoment=button("Момент ▶",()=>jump(labels.nextMoment(moments,replay.snapshot().state.seq,follow.value||null),"Далі ключових моментів немає."));
    const digest=button("Підсумок за 60 с",()=>toggleDigest());
    previousMoment.setAttribute("aria-label","Попередній ключовий момент");nextMoment.setAttribute("aria-label","Наступний ключовий момент");
    previousMoment.disabled=nextMoment.disabled=digest.disabled=!moments.length;
    host.append(toggle,speed,label,clock,follow,next,previousMoment,nextMoment,digest,momentNote,node("p","research-muted","Повтор записаних подій. Пауза й перемотка змінюють лише перегляд."));
    let raf=null,lastNow=null,disposed=false,digestTimer=null,noteSeq=null;
    // The note describes the event it was written at; at any other position the hint comes back.
    function say(text){momentNote.textContent=text;noteSeq=replay.snapshot().state.seq;}
    function stopDigest(){if(digestTimer!==null){clearTimeout(digestTimer);digestTimer=null;}digest.textContent="Підсумок за 60 с";}
    function jump(moment,none,keepDigest){
      if(!keepDigest)stopDigest();
      if(!moment){say(none);return;}
      replay.pause();replay.seekSeq(moment.seq);publish(true);
      say(moment.label+" · "+elapsedLabel(moment.at_ms-manifest.started_at_ms)+" · "+localClock(moment.at_ms));
    }
    function toggleDigest(){
      if(digestTimer!==null){stopDigest();return;}
      const plan=labels.digestPlan(moments);let at=0;
      replay.pause();digest.textContent="Зупинити підсумок";
      const step=()=>{
        if(at>=plan.steps.length){stopDigest();say("Підсумок завершено: показано "+plan.shown+" з "+plan.total+" моментів.");return;}
        jump(plan.steps[at++],"",true);digestTimer=setTimeout(step,plan.dwell_ms);
      };
      step();
    }
    function publish(reset){
      const snapshot=replay.snapshot();
      if(noteSeq!==null&&snapshot.state.seq!==noteSeq){momentNote.textContent=hint;noteSeq=null;}
      range.value=String(snapshot.at_ms-manifest.started_at_ms);range.setAttribute("aria-valuetext",elapsedLabel(snapshot.at_ms-manifest.started_at_ms));
      clock.textContent=localClock(snapshot.at_ms)+" · "+elapsedLabel(snapshot.at_ms-manifest.started_at_ms)+" · "+snapshot.speed+"× · "+snapshot.event_count+" подій";
      toggle.textContent=snapshot.playing?"Пауза повтору":"Відтворити";
      options.onFrame?.(replay.frame(),snapshot,{reset});
    }
    speed.addEventListener("change",()=>{replay.setSpeed(Number(speed.value));publish(false);});
    range.addEventListener("input",()=>{stopDigest();replay.pause();replay.seek(manifest.started_at_ms+Number(range.value));publish(true);});
    function animate(now){
      if(disposed)return;
      if(document.hidden&&replay.playing){replay.pause();publish(false);}
      if(lastNow!==null&&replay.playing){replay.tick(now-lastNow);publish(false);}
      lastNow=now;raf=requestAnimationFrame(animate);
    }
    publish(true);raf=requestAnimationFrame(animate);
    return {seek(at){replay.pause();replay.seek(at);publish(true);},
      seekEvent(seq){replay.pause();replay.seekSeq(seq);publish(true);},
      destroy(){disposed=true;stopDigest();cancelAnimationFrame(raf);host.replaceChildren();}};
  }
  return {adaptBundle,normalizeParticipant,mountReview,mountWorkshopTools,mountLeague,mountReplayControls,table};
});
