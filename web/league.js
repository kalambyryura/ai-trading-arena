"use strict";
/* Local history GET only. Metadata planning and financial controls live in the owner page. */
((root, factory) => {
  const node = typeof module === "object" && module.exports;
  const api = factory(node ? require("./review-model.js") : root.ArenaReviewModel, node ? require("./league-stats.js") : root.ArenaLeagueStats);
  if (typeof module === "object" && module.exports) module.exports = api;
  else {root.ArenaLeaguePage=api;api.mount().catch(error=>api.showError(error.message));}
})(typeof window === "undefined" ? {} : window, (Model, Stats) => {
  /* Public standings: projection of the server's per-cohort rows; scoring stays in league.py.
     Uncertainty comes only from league-stats.js (day-clustered bootstrap, one fixed seed): adjacent battles share the market. */
  const KINDS={human:"людина",model:"ШІ-модель",example:"приклад"};
  const MODES={OKX_LIVE:"Live · реальні гроші",OKX_DEMO:"Demo · тестові гроші біржі",PAPER:"PAPER · модельне виконання"};
  const EXECUTION={rules:"лише правила",shared_jev:"правила + спільний Jev"};
  const NBSP="\u00a0", SEED="arena-league-v1";
  const VERDICTS={ABOVE_CASH:"вище за Cash",BELOW_CASH:"нижче за Cash",INDISTINGUISHABLE:"не відрізнити від Cash",FEW_DATA:"мало даних",NO_DATA:"немає даних"};
  const text=(value,fallback="")=>typeof value==="string"&&value.trim()?value.trim():fallback;
  const whole=value=>Number.isSafeInteger(value)&&value>=0?value:null;
  const amount=value=>typeof value==="number"&&Number.isFinite(value)?amount(String(value)):typeof value==="string"&&Model.decimalCompare(value,"0")!==null?value:null;
  const plural=(n,one,few,many)=>{const a=n%10,b=n%100;return n+" "+(a===1&&b!==11?one:a>=2&&a<=4&&(b<12||b>14)?few:many);};
  function usdt(value,signed=true) {
    const exact=amount(value);if(exact===null)return "—";
    const size=Math.abs(Number(exact)),sign=Model.decimalCompare(exact,"0");
    const digits=size&&size<.0001?size.toFixed(8).replace(/\.?0+$/,""):size.toFixed(4);
    return (sign<0?"−":signed&&sign>0?"+":"")+digits.replace(".",",");
  }
  function author(row) {
    if(row.id==="cash")return row.score_policy?.id==="filled_entry_net_v1"?"еталон · не торгує · net = 0":"не торгує · net = 0";
    const name=text(row.author_name),model=text(row.model);
    const parts=[name,KINDS[row.author_kind],model&&model!==name?model:""].filter(Boolean);
    return parts.length?parts.join(" · "):"автор не вказаний";
  }
  function ranked(rows) {
    if(rows.some(row=>amount(row.net_sum_usdt)===null))return rows.map(row=>({row,rank:null}));
    const activeOnly=rows.some(row=>row.score_policy?.id==="filled_entry_net_v1");
    const candidates=activeOnly?rows.filter(row=>row.id!=="cash"&&row.participated_battles>0):rows;
    const rest=activeOnly?rows.filter(row=>!candidates.includes(row)):[];
    const sorted=[...candidates].sort((a,b)=>Model.decimalCompare(amount(b.net_sum_usdt),amount(a.net_sum_usdt))||String(a.id).localeCompare(String(b.id)));
    let rank=0;
    return sorted.map((row,index)=>{
      if(!index||Model.decimalCompare(amount(row.net_sum_usdt),amount(sorted[index-1].net_sum_usdt))!==0)rank=index+1;
      return{row,rank};}).concat(rest.map(row=>({row,rank:null})));
  }
  // 90 % day-clustered interval of the mean net per battle (Cash = 0), and one shared verdict wording.
  function uncertainty(entry, cash, stats) {
    if(cash||!stats)return{interval:null,sample:null,verdict:cash?"BASELINE":null};
    const verdict=stats.verdict(entry),ok=entry&&entry.status==="OK"&&Number.isFinite(entry.low)&&Number.isFinite(entry.high);
    return{interval:ok?{low:entry.low,high:entry.high}:null,sample:entry?{n:entry.n,days:entry.days,status:entry.status}:null,verdict};
  }
  function day(value) {const time=Date.parse(value);return Number.isFinite(time)?new Intl.DateTimeFormat("uk-UA",{day:"numeric",month:"short"}).format(time):null;}
  function standings(league, stats = Stats) {
    const info=new Map((Array.isArray(league?.cohorts)?league.cohorts:[]).filter(c=>c&&typeof c.cohort==="string").map(c=>[c.cohort,c]));
    const grouped=new Map();
    for(const row of Array.isArray(league?.frequencies)?league.frequencies:[]){
      if(!row||typeof row.cohort!=="string")continue;
      if(!grouped.has(row.cohort))grouped.set(row.cohort,[]);grouped.get(row.cohort).push(row);
    }
    const order=[...info.keys()].filter(key=>grouped.has(key)).concat([...grouped.keys()].filter(key=>!info.has(key)));
    const intervals=stats&&Array.isArray(league?.battles)?stats.intervals(league.battles,{seed:SEED}):{};
    const groups=order.map(cohort=>{
      const meta=info.get(cohort)||{},allocation=amount(meta.allocation_usdt);
      const rows=ranked(grouped.get(cohort)).map(({row,rank})=>({id:String(row.id),rank,cash:row.id==="cash",
        name:row.id==="cash"?"Cash":text(row.name,text(String(row.id??""),"Без назви")),author:author(row),version:whole(row.version),
        battles:whole(row.comparable_battles),participated:whole(row.participated_battles),noEntry:whole(row.no_entry_battles),
        activeOnly:row.score_policy?.id==="filled_entry_net_v1",net:amount(row.net_sum_usdt),mean:amount(row.net_mean_usdt),
        above:whole(row.above_cash),at:whole(row.at_cash),below:whole(row.below_cash),sole:whole(row.sole_wins),shared:whole(row.shared_top),
        percent:allocation!==null&&Number(allocation)>0&&amount(row.net_mean_usdt)!==null?Number(row.net_mean_usdt)/Number(allocation)*100:null,
        ...uncertainty(row.id==="cash"?null:intervals[cohort+":"+row.id],row.id==="cash",stats)}));
      const strategies=whole(meta.participants)??rows.filter(row=>!row.cash).length;
      const title=[MODES[meta.mode],text(meta.cohort_mark),text(meta.duration_label),allocation!==null?Model.exact(allocation,"USDT")+" на учасника":"",
        plural(strategies,"стратегія","стратегії","стратегій")+" + Cash",EXECUTION[meta.execution_mode],
        meta.score_policy?.id==="filled_entry_net_v1"?"перемога після входу":meta.score_policy?"історичний регламент з Cash":""].filter(Boolean).join(" · ");
      const first=day(meta.first_at),last=day(meta.last_at);
      return{cohort,short:cohort.slice(0,8),title,period:first&&last?(first===last?first:first+" – "+last):null,
        battles:whole(meta.battles)??Math.max(0,...rows.map(row=>row.battles||0)),rows};
    });
    const results=groups.flatMap(group=>group.rows).filter(row=>!row.cash&&row.net!==null);
    const above=results.filter(row=>Model.decimalCompare(row.net,"0")>0).length,battles=groups.reduce((total,group)=>total+group.battles,0);
    const judged=results.filter(row=>["ABOVE_CASH","BELOW_CASH","INDISTINGUISHABLE"].includes(row.verdict)),steady=judged.filter(row=>row.verdict==="ABOVE_CASH").length;
    const of=(n,one,many)=>n+(n%10===1&&n%100!==11?one:many);
    const headline=!groups.length?"Ще немає завершених порівнянних боїв. Таблиця з’явиться після першого звіреного бою за спільним регламентом."
      :!results.length?"Порівнянні бої є, але сум ще немає: оновіть панель після звірки."
      :"Вище за Cash за сумою net: "+above+" з "+of(results.length," результату"," результатів")+
        " · "+plural(battles,"порівнянний бій","порівнянні бої","порівнянних боїв")+(groups.length>1?" у "+groups.length+" групах.":".")+
        (!stats?"":judged.length?" Стабільно вище за Cash (90 % інтервал): "+steady+" з "+of(judged.length," результату"," результатів")+".":" Висновок щодо Cash — після 30 боїв.");
    return{groups,above,results:results.length,battles,judged:judged.length,steady,headline};
  }
  function el(tag,cls="",value) {const node=document.createElement(tag);if(cls)node.className=cls;if(value!==undefined)node.textContent=String(value);return node;}
  function mountStandings(host,league) {
    const model=standings(league);
    host.replaceChildren();host.classList.add("research-shell","league-standings-shell");
    host.append(el("p","research-kicker","ХТО ПОПЕРЕДУ"),el("h2","","Таблиця ліги"),el("p","league-standings-lead",model.headline));
    if(!model.groups.length)return model;
    host.append(el("p","research-intro","Нові бої: перемога — найбільший net USDT після комісій і funding серед тих, хто виконав хоча б один вхід. Без входу — без перемоги; Cash = 0 лише еталон. Старі бої зберігають свій регламент. Групи не змішуються. Перемога зі збитком не доводить прибутковості."));
    for(const group of model.groups){
      const section=el("section","league-group"),meta=el("p","research-muted",[plural(group.battles,"бій","бої","боїв"),group.period,"група "+group.short].filter(Boolean).join(" · "));
      meta.title="Повний ідентифікатор групи: "+group.cohort;section.append(el("h3","",group.title||"Група без опису"),meta);
      const wrap=el("div","research-table-scroll"),table=el("table","research-table league-standings"),head=el("tr"),body=el("tbody");
      const labels=["Місце","Стратегія","Боїв","Σ net, USDT","За бій, USDT","90 % інтервал за бій","Проти Cash","Перемоги","Висновок"];
      labels.forEach(label=>{const cell=el("th","",label);cell.scope="col";head.append(cell);});
      for(const row of group.rows){
        const line=el("tr"),who=el("th");if(row.cash)line.dataset.cash="true";
        who.scope="row";who.append(el("strong","",row.name),el("small","league-author",row.author+(row.version!==null&&!row.cash?" · v"+row.version:"")));
        if(row.activeOnly&&!row.cash)who.append(el("small","league-author",`З входом: ${row.participated??"—"} · без входу: ${row.noEntry??"—"}${row.participated===0?" · поза рейтингом переможців":""}`));
        const net=el("span","",usdt(row.net));if(row.net!==null)net.title=Model.exact(row.net,"USDT",true);
        const perBattle=el("span");perBattle.append(el("span","",usdt(row.mean)));
        if(row.percent!==null&&!row.cash)perBattle.append(el("small","league-author",(row.percent>0?"+":row.percent<0?"−":"")+Math.abs(row.percent).toFixed(3).replace(".",",")+" % від ставки"));
        const cash=row.cash?"—":[["краще",row.above],["нарівні",row.at],["гірше",row.below]].map(([label,value])=>label+NBSP+(value??"—")).join(" · ");
        const range=el("span","",row.cash?"—":row.interval?usdt(row.interval.low)+" … "+usdt(row.interval.high)
          :row.sample?.status==="FEW_DATA"?"мало даних · "+row.sample.n+" з 30":"—");
        if(row.sample)range.title=plural(row.sample.n,"бій","бої","боїв")+" за "+plural(row.sample.days,"добу","доби","діб")+" UTC";
        const badge=el("span","league-verdict",row.cash?"еталон":VERDICTS[row.verdict]||"—");badge.dataset.verdict=row.cash?"BASELINE":row.verdict||"NO_DATA";
        const cells=[el("span","league-rank",row.rank??"—"),who,el("span","",row.battles??"—"),net,perBattle,range,el("span","",cash),
          el("span","","одноосібні"+NBSP+(row.sole??"—")+" · спільні"+NBSP+(row.shared??"—")),badge];
        cells.forEach((value,index)=>{if(value===who)line.append(who);else{const cell=el("td");cell.dataset.label=labels[index];cell.append(value);line.append(cell);}});
        body.append(line);
      }
      const thead=el("thead");thead.append(head);table.append(thead,body);wrap.append(table);section.append(wrap);host.append(section);
    }
    const help=el("details","research-provenance");help.append(el("summary","","Як читати таблицю"));
    ["Місце — за Σ net у межах групи. Σ net — сума чистих результатів усіх порівнянних боїв групи після комісій, проковзування й funding. Витрати на Jev у USD показано в розборі бою окремо; у net USDT їх не включено.",
      "За бій — середній net; відсоток — від ставки учасника. Проти Cash — скільки боїв стратегія закінчила вище нуля, рівно (у межах 0,00000001 USDT) і нижче.",
      "Перемоги: одноосібні — єдине перше місце в бою; спільні — нічия торгових учасників. У новому регламенті Cash і стратегії без виконаного входу не можуть перемогти. Бої без входів залишаються в аналітиці з нулем перемог. Лише історичний регламент допускає перше місце з Cash.",
      "90 % інтервал за бій — межі, в яких, найімовірніше, лежить середній net стратегії, якщо врахувати, що бої одного дня мають спільний ринок (bootstrap за календарними добами UTC). «Вище» чи «нижче за Cash» — лише коли весь інтервал по один бік від нуля і є щонайменше 30 боїв. Це опис невизначеності, а не гарантія майбутнього результату.",
      "Суми й місця описові: сусідні бої не є незалежними доказами, тож більша сума сама по собі ще не доводить переваги над Cash."]
      .forEach(line=>help.append(el("p","research-muted",line)));
    host.append(help);
    return model;
  }
  function reviewHref(battle, replay=false) {
    const mode = battle.kind || (battle.mode === "PAPER" ? "paper" : ["OKX_DEMO","OKX_LIVE"].includes(battle.mode) ? "exchange" : null);
    if (!["exchange","paper"].includes(mode) || !/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$/.test(battle.id || "")) throw new Error("Історія не містить безпечного посилання на журнал.");
    const query = new URLSearchParams({mode,id:battle.id}); if(replay)query.set("view","replay");
    return "/review?" + query;
  }
  function showError(text) {const host=document.getElementById("league-notice");if(host){host.textContent=text;host.dataset.kind="error";}}
  async function read() {
    const response=await fetch("/api/league",{method:"GET",cache:"no-store",credentials:"same-origin",redirect:"error"});
    let data;try{data=await response.json();}catch{throw new Error("Історія ліги не повернула JSON.");}
    if(!response.ok)throw new Error(data.error || "HTTP " + response.status);
    if(!Array.isArray(data.battles))throw new Error("Непідтримуваний формат історії ліги.");
    return data;
  }
  async function mount() {
    const button=document.getElementById("league-refresh"),notice=document.getElementById("league-notice");
    let busy=false;
    async function refresh() {
      if(busy)return;busy=true;button.disabled=true;
      try {
        const data=await read(),standingsHost=document.getElementById("league-standings");
        if(standingsHost)mountStandings(standingsHost,data);
        window.ArenaResearchPanels.mountLeague(document.getElementById("league-host"),data,{
          onReview(_id,battle){location.href=reviewHref(battle);},onReplay(_id,battle){location.href=reviewHref(battle,true);}});
        notice.dataset.kind="okay";notice.textContent="Локальні журнали прочитано. Незавершені й непорівнянні бої зберігаються в історії.";
      }catch(error){showError(error.message);}finally{busy=false;button.disabled=false;}
    }
    button.addEventListener("click",refresh);await refresh();return{refresh};
  }
  return {reviewHref,read,mount,showError,standings,mountStandings,usdt};
});
