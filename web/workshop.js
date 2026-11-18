"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const token = document.querySelector('meta[name="panel-token"]')?.content || "";
  let diagnosticsUI;
  let researchParent=new URLSearchParams(location.search).get("research")==="1"?new URLSearchParams(location.search).get("parent"):null;
  const S = {generation:null,versions:[],datasets:[],trials:[],contract:{},selectedId:null,selected:null,trial:null,parentId:null,busy:false,pendingVersion:null,notice:"",noticeError:false};
  const X = {generation:null,state:null,busy:false,profileSig:"",activeRun:null,selectedHistoryId:null,lastTerminalId:null,runTimer:null};
  const demoModeLabel=mode=>window.DemoExecutionUI?.modeLabel(mode)||(mode==="demo_mechanics"?"Механіка OKX Demo · без Jev":"Стратегія · з Jev");
  const $el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=String(text);return n;};
  const safe=(v,fallback="—")=>v===null||v===undefined||v===""?fallback:String(v);
  const num=v=>v===null||v===undefined||v===""||!Number.isFinite(Number(v))?null:Number(v);
  const fmt=(v,d=2)=>num(v)===null?"—":new Intl.NumberFormat("uk-UA",{maximumFractionDigits:d}).format(Number(v));
  const usdCost=(v,complete=true,d=6)=>{const amount=num(v);if(!complete||amount===null)return amount===null?"ціна неповна · невідома":`ціна неповна · відомо $${fmt(amount,d)} USD`;return `$${fmt(amount,d)} USD`;};
  const pct=(v,d=2)=>num(v)===null?"—":`${Number(v)>0?"+":""}${fmt(v,d)}%`;
  const plainPct=(v,d=2)=>num(v)===null?"—":`${fmt(v,d)}%`;
  const stamp=v=>{const date=new Date(v);return Number.isFinite(date.getTime())?new Intl.DateTimeFormat("uk-UA",{day:"2-digit",month:"short",year:"numeric",hour:"2-digit",minute:"2-digit"}).format(date):"—";};
  const clock=v=>{const date=new Date(v);return Number.isFinite(date.getTime())?new Intl.DateTimeFormat("uk-UA",{hour:"2-digit",minute:"2-digit",second:"2-digit"}).format(date):"—";};
  const kindLabel=k=>k==="SYNTHETIC"?"Синтетичні дані":k==="RECORDED_PUBLIC"?"Записані публічні котирування":k==="OKX_DEMO_FORWARD"?"OKX Demo · перевірка вперед":safe(k,"Невідомий тип даних");
  const statusLabel=s=>({draft:"Чернетка",frozen:"Зафіксовано",COMPLETE:"Завершено",INCOMPLETE:"Неповно"}[s]||safe(s));
  function showNotice(message,error=false){S.notice=message||"";S.noticeError=Boolean(error);const box=$("notice");box.textContent=S.notice;box.hidden=!S.notice;box.classList.toggle("error",S.noticeError);}
  function showError(message){const box=$("recipe-error");box.textContent=message||"";box.hidden=!message;}
  function setConnected(ok,label){$("connection").classList.toggle("offline",!ok);$("connection").lastElementChild.textContent=label;}
  async function request(path,body,{timeout=0,viewer=false}={}){const options={cache:"no-store",credentials:"same-origin"};if(body!==undefined)Object.assign(options,{method:"POST",headers:{"Content-Type":"application/json","X-Panel-Token":token},body:typeof body==="string"?body:JSON.stringify(body)});else if(viewer)options.headers={"X-Viewer-Poll":"1"};let timer=null;const late=()=>new Error("Сервер не відповів вчасно; спробуємо ще раз.");if(timeout>0&&body===undefined&&typeof AbortController==="function"){const control=new AbortController();options.signal=control.signal;timer=setTimeout(()=>control.abort(),timeout);}try{const response=await fetch(path,options);let data={};try{data=await response.json();}catch{if(options.signal?.aborted)throw late();}if(!response.ok)throw new Error(data.error||`Запит не виконано (HTTP ${response.status}).`);return data;}catch(error){if(error?.name==="AbortError")throw late();throw error;}finally{if(timer!==null)clearTimeout(timer);}}
  function versionPayload(version){if(!version)return null;const source=version.strategy&&typeof version.strategy==="object"?version.strategy:version;const keys=["name","author","description","original_text","rules","confirmed"];if(!keys.every(key=>Object.prototype.hasOwnProperty.call(source,key)))return null;const payload={};for(const key of keys)payload[key]=source[key];if(Object.prototype.hasOwnProperty.call(source,"strategy_schema_version"))payload.strategy_schema_version=source.strategy_schema_version;if(Object.prototype.hasOwnProperty.call(source,"test_plan"))payload.test_plan=source.test_plan;else if(Object.prototype.hasOwnProperty.call(version,"test_plan"))payload.test_plan=version.test_plan;return payload;}
  function testPlan(version){return versionPayload(version)?.test_plan||version?.test_plan||null;}
  // R2: a schema-4 recipe names its instrument (BTC-, ETH- or SOL-USDT-SWAP); an older recipe has none and trades BTC.
  function recipeInstrument(recipe){const M=window.ArenaReviewModel;return recipe&&M?.instrumentOf?(M.instrumentOf(recipe)??M.instrumentOf(recipe.strategy)):null;}
  function recipeCoin(recipe){return recipeInstrument(recipe)?.split("-")[0]??null;}
  function versionReady(version){return Boolean(version&&version.id&&versionPayload(version));}
  function allTrials(version){const related=[...S.trials,...(S.selected?.trials||[])];const map=new Map();for(const trial of related)if(trial?.version_id===version?.id)map.set(trial.id,trial);return [...map.values()].sort((a,b)=>new Date(b.created_at)-new Date(a.created_at));}
  function hasComplete(version){return allTrials(version).some(t=>t.status==="COMPLETE");}
  async function refresh(selectId=S.selectedId){try{const data=await request("/api/workshop/state");S.generation=data.generation;S.versions=Array.isArray(data.versions)?data.versions:[];S.datasets=Array.isArray(data.datasets)?data.datasets:[];S.trials=Array.isArray(data.trials)?data.trials:[];S.contract=data.contract||{};setConnected(true,"Майстерня готова");renderVersions();renderDatasets();renderContract();renderDemoVersionOptions();const nextId=S.pendingVersion||selectId;if(nextId&&S.versions.some(v=>v.id===nextId)){S.pendingVersion=null;await selectVersion(nextId,true);}else if(S.selectedId&&S.versions.some(v=>v.id===S.selectedId)){await selectVersion(S.selectedId,true);}else{renderSelectedSummary(null);renderTrials();}syncActions();if(!S.noticeError)showNotice(S.notice,false);}
    catch(error){setConnected(false,"Майстерня недоступна");showNotice(error.message,true);}}
  function renderVersions(){const host=$("version-list");host.replaceChildren();const versions=[...S.versions].sort((a,b)=>(new Date(b.created_at).getTime()||0)-(new Date(a.created_at).getTime()||0));$("version-count").textContent=`${versions.length} ${versions.length===1?"версія":"версій"}`;if(!versions.length){host.append($el("p","version-empty","Версій ще немає. Імпортуй перший рецепт або заповни навчальний приклад."));return;}for(const v of versions){const btn=document.createElement("button");btn.type="button";btn.className="version-item";btn.setAttribute("aria-current",String(v.id===S.selectedId));btn.setAttribute("aria-label",`${safe(v.name,"Стратегія")}, версія ${safe(v.version)}, ${statusLabel(v.status)}`);const name=$el("span","version-name",safe(v.name,"Без назви")),number=$el("span","version-number",`v${safe(v.version,"—")}`),author=$el("span","version-author",authorLabel(v.author)),date=$el("span","version-date",stamp(v.created_at)),badge=$el("span",`version-badge ${v.promoted_id?"promoted":v.status==="frozen"?"frozen":""}`,v.promoted_id?"В арені":statusLabel(v.status));btn.append(name,number,author,date,badge);btn.addEventListener("click",()=>selectVersion(v.id,true));host.append(btn);}}
  function authorLabel(author){if(!author||typeof author!=="object")return "Автор не вказаний";const bits=[author.name,author.kind==="model"?author.model:null].filter(Boolean);return bits.join(" · ")||"Автор не вказаний";}
  function renderDatasets(){const select=$("dataset-select"),keep=select.value;select.replaceChildren();if(!S.datasets.length){const option=$el("option","","Наборів даних немає");select.append(option);select.disabled=true;$("dataset-description").textContent="Спершу потрібен локальний або записаний публічний набір.";return;}for(const d of S.datasets){const option=document.createElement("option");option.value=d.id;option.textContent=`${d.name} · ${kindLabel(d.kind)}`;select.append(option);}select.disabled=false;select.value=S.datasets.some(d=>d.id===keep)?keep:S.datasets[0].id;updateDatasetDescription();}
  function renderContract(){const host=$("contract-runtime");host.replaceChildren();for(const [key,label] of [["features","Ознаки, доступні рецепту"],["limits","Межі пробного прогону"],["runtime","Середовище проби"]]){const value=S.contract?.[key];const row=$el("div",""),dt=$el("dt","",label),dd=$el("dd","",value===undefined?"Не описано в умовах проби":typeof value==="string"?value:JSON.stringify(value,null,2));row.append(dt,dd);host.append(row);}const template=S.contract?.demo_probe_template,button=$("add-demo-probe");if(button)button.disabled=S.busy||!template||template.test_plan!=="demo_cycle_v1";}
  function updateDatasetDescription(){const dataset=S.datasets.find(d=>d.id===$("dataset-select").value);$("dataset-description").textContent=dataset?`${kindLabel(dataset.kind)} — ${safe(dataset.description,"Опис не надано.")}`:"Оберіть набір даних.";}
  function setComposer(payload,parentId=null){$("recipe-json").value=payload?JSON.stringify(payload,null,2):"";$("confirm-match").checked=false;S.parentId=parentId;$("composer-parent").textContent=parentId?`Нова версія від ${parentId}`:"Нова стратегія";showError("");}
  async function selectVersion(id,loadDetail=true){if(!id)return;S.selectedId=id;diagnosticsUI?.selectVersion(id);const summary=S.versions.find(v=>v.id===id);if(!summary)return;renderVersions();S.selected=summary;S.trial=null;renderSelectedSummary(summary);renderTrials();syncActions();if(loadDetail){try{const detail=await request(`/api/workshop/versions/${encodeURIComponent(id)}`);if(S.selectedId!==id)return;S.selected=detail;renderSelectedSummary(detail);renderTrials();syncActions();}catch(error){showNotice(error.message,true);}}}
  function renderSelectedSummary(version){const title=$("selected-heading"),badge=$("version-status"),summary=$("selected-summary"),meta=$("selected-meta");summary.replaceChildren();meta.replaceChildren();if(!version){title.textContent="Ще не вибрано";badge.textContent="—";badge.className="status-chip";summary.append($el("p","subtle","Вибери рецепт зі списку версій або створи нову чернетку."));return;}title.textContent=safe(version.name,"Стратегія");badge.textContent=version.promoted_id?"В арені":statusLabel(version.status);badge.className=`status-chip ${version.promoted_id?"promoted":version.status==="frozen"?"frozen":""}`;const probe=testPlan(version)==="demo_cycle_v1",desc=$el("p","",safe(version.description,"Опис не надано."));summary.append(desc);if(probe)summary.append($el("p","demo-probe-lock","Лише OKX Demo: навчальний технічний цикл, не стратегія для змагання. Без локального RULES-тренування, freeze або просування в арену."));if(version.parent_id)summary.append($el("p","",`Похідна версія від ${version.parent_id}.`));const canonical=versionPayload(version);if(canonical?.original_text){const source=$el("details","selected-source"),sourceSummary=$el("summary","","Первинний текст автора");source.append(sourceSummary,$el("pre","",canonical.original_text));summary.append(source);}const author=version.author||canonical?.author||{};for(const text of [authorLabel(author),author.submitted_at?`Подано ${stamp(author.submitted_at)}`:null,author.model?`Модель, вказана автором: ${author.model}`:null,`Відбиток рецепта: ${safe(version.recipe_sha256,"ще не сформовано")}`,version.v4_recipe_sha256?`v4-рецепт: ${safe(version.v4_recipe_sha256)}`:null])if(text)meta.append($el("span","meta-pill",text));
    const rules=version.rules||version.strategy?.rules;if(rules){const list=$el("details","selected-rules"),summaryEl=$el("summary","","Переглянути формальні правила");const pre=$el("pre","",JSON.stringify(rules,null,2));list.append(summaryEl,pre);summary.append(list);}if(version.status==="frozen")summary.append($el("p","freeze-stamp","Зафіксована версія — нові зміни треба зберегти як окрему версію."));if(version.promoted_id)summary.append($el("p","promoted-note",`Точний рецепт уже зареєстровано в PAPER-арені: ${version.promoted_id}. Сезон не запущено.`));}
  function renderTrials(){const host=$("trial-list");host.replaceChildren();const version=S.selected;const trials=version?allTrials(version):[];$("trial-count").textContent=String(trials.length);if(!version){host.append($el("p","subtle","Обери версію, щоб побачити її прогони."));return;}if(!trials.length){host.append($el("p","subtle","Для цієї версії ще не проводили пробних прогонів."));return;}for(const t of trials){const btn=document.createElement("button");btn.type="button";btn.className="trial-item";btn.append($el("span","trial-title",`${safe(t.dataset_name,"Набір даних")} · ${kindLabel(t.dataset_kind)}`),$el("span",`trial-state ${t.status==="INCOMPLETE"?"incomplete":""}`,statusLabel(t.status)));btn.append($el("span","trial-date",stamp(t.created_at)));const result=$el("span","trial-result");const metric=t.summary||{};result.append($el("span",Number(metric.pnl_usdt)<0?"negative":Number(metric.pnl_usdt)>0?"positive":"",`PnL ${fmt(metric.pnl_usdt)} USDT`),$el("span","",`Дохідність ${pct(metric.return_pct)}`),$el("span","",`Просадка ${plainPct(metric.max_drawdown_pct)}`),$el("span","",`${fmt(metric.trades,0)} угод`));btn.append(result);btn.addEventListener("click",()=>openTrial(t.id));host.append(btn);}}
  function syncActions(){const v=S.selected,isDraft=v?.status==="draft",probe=testPlan(v)==="demo_cycle_v1",canTrain=versionReady(v)&&(isDraft||v?.status==="frozen")&&!probe&&S.datasets.length>0&&!S.busy;$("run-training").disabled=!canTrain;$("freeze-version").disabled=!((isDraft||v?.status==="frozen")&&!probe&&hasComplete(v)&&!S.busy);$("freeze-version").textContent=v?.status==="frozen"?"Оновити кваліфікацію":"Зафіксувати правила";$("promote-version").disabled=!(v?.status==="frozen"&&!probe&&!v.promoted_id&&!S.busy);$("promote-version").textContent=v?.promoted_id?"Уже в PAPER-арені":"Додати в PAPER-арену";$("clone-version").disabled=!v||S.busy;$("export-recipe").disabled=!v;$("save-version").disabled=S.busy;$("new-version").disabled=S.busy;$("add-demo-probe").disabled=S.busy||S.contract?.demo_probe_template?.test_plan!=="demo_cycle_v1";}
  async function withBusy(button,working,action){if(S.busy)return;S.busy=true;const old=button?.textContent;if(button){button.disabled=true;button.textContent=working;}syncActions();try{await action();}catch(error){showNotice(error.message,true);}finally{S.busy=false;if(button&&button.isConnected)button.textContent=old;syncActions();}}
  async function post(path,payload){return request(path,{generation:S.generation,...payload});}
  // Exact numbers (R2.1.4): JSON.stringify writes the JSON number 3.0 as 3 and the server hashes the recipe it receives,
  // so Sonnet's H10 file (v4 dcf486662716f1b7) became another recipe (v4 d70e7c21c808e44d) through this page. A new version
  // is posted as the editor's own tokens: numbers exactly as written, strings re-escaped as JSON.stringify does (the same
  // decoded text), no whitespace; the top-level "confirmed" follows the checkbox, as parseRecipe sets it. Nothing is sent
  // unless the exact text parses to the very object parseRecipe checked.
  function exactRecipe(text,parsed){
    const lexeme=/[ \t\n\r]*("(?:[^"\\]|\\.)*"|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null|[{}[\]:,])/y,tokens=[],out=[];
    let at=0;for(let m;(lexeme.lastIndex=at,m=lexeme.exec(text));at=lexeme.lastIndex)tokens.push(m[1]);
    if(/[^ \t\n\r]/.test(text.slice(at))||tokens[0]!=="{")throw new Error("Рецепт має бути JSON-об’єктом.");
    for(let i=0,depth=0;i<tokens.length;i++){
      const t=tokens[i];
      if(depth===1&&t[0]==='"'&&tokens[i+1]===":"&&JSON.parse(t)==="confirmed"){  // the top-level key: the checkbox decides
        out.push('"confirmed":'+(parsed.confirmed?"true":"false"));
        i+=2;for(let nested=0;;i++){const v=tokens[i];if(v==="{"||v==="[")nested++;else if(v==="}"||v==="]")nested--;if(nested===0)break;}
        continue;
      }
      if(t==="{"||t==="[")depth++;else if(t==="}"||t==="]")depth--;
      out.push(t[0]==='"'?JSON.stringify(JSON.parse(t)):t);
    }
    const result=out.join("");
    if(JSON.stringify(JSON.parse(result))!==JSON.stringify(parsed))throw new Error("Точний текст рецепта не збігся з розібраним JSON; нічого не надіслано.");
    return result;
  }
  function parseRecipe(text=$("recipe-json").value){let payload;try{payload=JSON.parse(text);}catch(error){throw new Error(`JSON не розібрано: ${error.message}`);}if(!payload||typeof payload!=="object"||Array.isArray(payload))throw new Error("Рецепт має бути JSON-об’єктом.");const required=["name","author","description","original_text","rules","confirmed"];const missing=required.filter(k=>!Object.prototype.hasOwnProperty.call(payload,k));if(missing.length)throw new Error(`У паспорті бракує полів: ${missing.join(", ")}.`);if(!payload.author||typeof payload.author!=="object"||!payload.rules||typeof payload.rules!=="object")throw new Error("Поля author та rules мають бути JSON-об’єктами.");payload.confirmed=$("confirm-match").checked;return payload;}
  async function saveVersion(){showError("");let exact;try{const text=$("recipe-json").value;exact=exactRecipe(text,parseRecipe(text));}catch(error){showError(error.message);return;}await withBusy($("save-version"),"Зберігаємо…",async()=>{const response=await request("/api/workshop/versions",`{"generation":${JSON.stringify(S.generation)},"strategy":${exact},"parent_id":${JSON.stringify(S.parentId||null)}}`);const id=response.id||response.version_id||response.version?.id;S.pendingVersion=id||null;setComposer(null,null);showNotice("Збережено нову незмінну чернетку.");await refresh(id||null);});}
  function newVersion(){S.selectedId=null;S.selected=null;S.trial=null;setComposer(null,null);renderVersions();renderSelectedSummary(null);renderTrials();syncActions();$("recipe-json").focus();}
  function cloneVersion(){if(!S.selected)return;const strategy=versionPayload(S.selected);if(!strategy){showError("У вибраної версії немає повного паспорта для копіювання.");return;}setComposer(strategy,S.selected.id);$("recipe-json").focus();showNotice("Завантажено копію. Зміни зберігаються як нова версія; початковий запис залишається незмінним.");}
  function loadTemplate(){const source=S.contract.template;if(!source||typeof source!=="object"){showError("Навчальний шаблон відсутній у контракті.");return;}const template={...source,author:{...(source.author||{}),name:"Codex",kind:"example",model:""},confirmed:false};setComposer(template,null);showNotice("Це навчальний приклад Codex, а не рецепт, поданий іншим автором.");$("recipe-json").focus();}
  async function importFile(file){if(!file)return;try{const text=await file.text();JSON.parse(text);$("recipe-json").value=text;S.parentId=null;$("composer-parent").textContent="Імпортований рецепт · нова версія";$("confirm-match").checked=false;showError("");showNotice(`Завантажено локальний файл ${file.name}. Перевір рецепт і підтверди відповідність перед збереженням.`);}catch(error){showError(error instanceof SyntaxError?`Файл не містить коректний JSON: ${error.message}`:error.message);}finally{$("recipe-file").value="";}}
  function download(name,content,type="application/json"){const blob=new Blob([content],{type:`${type};charset=utf-8`}),url=URL.createObjectURL(blob),anchor=document.createElement("a");anchor.href=url;anchor.download=name;anchor.hidden=true;document.body.append(anchor);anchor.click();anchor.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  function slug(text){return safe(text,"strategy").normalize("NFKD").replace(/[^a-zA-Z0-9_-]+/g,"-").replace(/^-+|-+$/g,"").slice(0,60)||"strategy";}
  function exportVersion(){if(!S.selected)return;const payload=versionPayload(S.selected);if(!payload){showNotice("Повний рецепт цієї версії недоступний для експорту.",true);return;}download(`${slug(S.selected.name)}-v${safe(S.selected.version,"1")}.json`,JSON.stringify(payload,null,2));}
  function promptText(){const p=S.contract.prompt;if(typeof p==="string")return p;return p?JSON.stringify(p,null,2):"Завдання для автора поки не опубліковано.";}
  async function copyText(text,label){try{await navigator.clipboard.writeText(text);showNotice(`${label} скопійовано.`);}catch{showNotice("Браузер не надав доступ до буфера. Скористайся кнопкою завантаження.",true);}}
  function downloadPrompt(){download("strategy-author-prompt.txt",promptText(),"text/plain");}
  async function train(){const version=S.selected;if(!versionReady(version)||!["draft","frozen"].includes(version.status)||testPlan(version)==="demo_cycle_v1")return;const datasetId=$("dataset-select").value,budget=Number($("trial-budget").value);if(!datasetId){showNotice("Спершу вибери набір даних.",true);return;}if(!Number.isFinite(budget)||budget<25||budget>1000){showNotice("Пробний бюджет має бути від 25 до 1 000 USDT.",true);return;}await withBusy($("run-training"),"Виконуємо локальну пробу…",async()=>{showNotice("Короткий локальний прогін формальних правил. Jev і моделі не викликаються.");const result=await post("/api/workshop/train",{version_id:version.id,dataset_id:datasetId,budget_usdt:budget});const trialId=result.id||result.trial_id||result.trial?.id;await refresh(version.id);if(trialId)await openTrial(trialId);else{const latest=allTrials(S.selected)[0];if(latest)await openTrial(latest.id);else showNotice("Пробу завершено. Онови список, щоб прочитати звіт.");}});}
  async function freeze(){const version=S.selected;if(!version||testPlan(version)==="demo_cycle_v1"||!["draft","frozen"].includes(version.status)||!hasComplete(version))return;await withBusy($("freeze-version"),"Фіксуємо правила…",async()=>{await post("/api/workshop/freeze",{version_id:version.id});await refresh(version.id);showNotice("Правила зафіксовано. Ця версія тепер незмінна.");});}
  async function promote(){const version=S.selected;if(!version||testPlan(version)==="demo_cycle_v1"||version.status!=="frozen"||version.promoted_id)return;await withBusy($("promote-version"),"Додаємо до арени…",async()=>{const result=await post("/api/workshop/promote",{version_id:version.id});await refresh(version.id);showNotice(result.strategy_id?`Рецепт зареєстровано в PAPER-арені (${result.strategy_id}). Сезон не запускався.`:"Рецепт зареєстровано в PAPER-арені. Сезон не запускався.");});}
  async function openTrial(id){try{const report=await request(`/api/workshop/trials/${encodeURIComponent(id)}`);S.trial=report;renderTrialDetail(report);$("trial-dialog").showModal();}catch(error){showNotice(error.message,true);}}
  function stat(label,value){const box=$el("div","report-metric");box.append($el("span","",label),$el("b","",value));return box;}
  function section(title,content){const box=$el("section","report-section");box.append($el("h3","",title),content);return box;}
  function svg(tag,attrs={},text){const n=document.createElementNS("http://www.w3.org/2000/svg",tag);for(const [k,v] of Object.entries(attrs))n.setAttribute(k,String(v));if(text!==undefined)n.textContent=String(text);return n;}
  function chart(points){const node=svg("svg",{viewBox:"0 0 720 170",class:"equity-chart",role:"img","aria-label":"Пробна крива equity"});const valid=(points||[]).filter(p=>num(p.equity_usdt)!==null);if(valid.length<2){node.append(svg("text",{x:360,y:88,"text-anchor":"middle",fill:"#849080","font-size":12},"Недостатньо точок для графіка"));return node;}const values=valid.map(p=>Number(p.equity_usdt)),lo=Math.min(...values),hi=Math.max(...values),pad=Math.max((hi-lo)*.12,.01),y=n=>12+(hi+pad-n)/(hi-lo+2*pad)*120,coords=valid.map((p,i)=>`${58+i*630/Math.max(1,valid.length-1)},${y(Number(p.equity_usdt))}`).join(" ");node.append(svg("line",{x1:58,y1:137,x2:688,y2:137,stroke:"#d9dfd1"}),svg("polyline",{points:coords,fill:"none",stroke:"#718e55","stroke-width":2.5,"stroke-linecap":"round","stroke-linejoin":"round"}),svg("text",{x:54,y:15,"text-anchor":"end",fill:"#849080","font-size":10},fmt(hi,3)),svg("text",{x:54,y:136,"text-anchor":"end",fill:"#849080","font-size":10},fmt(lo,3)),svg("text",{x:58,y:158,fill:"#849080","font-size":10},clock(valid[0].at)),svg("text",{x:688,y:158,"text-anchor":"end",fill:"#849080","font-size":10},clock(valid.at(-1).at)));return node;}
  function renderTrialDetail(report){
    $("trial-title").textContent=`Проба · ${safe(report.dataset_name,"набір даних")}`;
    $("trial-dialog").querySelector(".small-eyebrow").textContent=`ЗВІТ ПРОБНОГО ПРОГОНУ · ${usdCost(report.ai_cost_usd,report.ai_cost_complete!==false&&report.ai_cost_usd!==null&&report.ai_cost_usd!==undefined)}`;
    const host=$("trial-detail");host.replaceChildren();
    const exchangeDemo=report.dataset_kind==="OKX_DEMO_FORWARD",banner=$el("div",`report-banner ${report.status==="INCOMPLETE"?"incomplete":""}`,`${statusLabel(report.status)} · ${kindLabel(report.dataset_kind)} · ${stamp(report.created_at)} · Витрати AI: ${usdCost(report.ai_cost_usd,report.ai_cost_complete!==false&&report.ai_cost_usd!==null&&report.ai_cost_usd!==undefined)}. ${report.status==="INCOMPLETE"?"Цей звіт неповний; не використовуй його як підтвердження результату.":exchangeDemo?"Фактичні demo-виконання та облік звірено; прибутковість на Live цим не доведена.":"Це локальна проба правил, не доказ майбутньої доходності."}`);host.append(banner);
    const s=report.summary||{},metrics=$el("div","metric-grid");
    metrics.append(stat("PnL",`${fmt(s.pnl_usdt)} USDT`),stat("Дохідність",pct(s.return_pct)),stat("Макс. просадка",plainPct(s.max_drawdown_pct)),stat("Угоди / комісії",`${fmt(s.trades,0)} · ${fmt(s.fees_usdt,6)} USDT`),stat(exchangeDemo?"Фактичний funding":"Funding-модель",`${fmt(s.funding_usdt,6)} USDT`),stat("Завершені прогони",`${safe(s.complete_runs,0)} / ${safe(s.total_runs,0)}`));host.append(metrics);
    host.append(section("Відгук для автора",feedbackNode(report.feedback)));
    // remaining_btc holds coins of the recipe instrument (ETH for an ETH recipe); a recipe without one trades BTC-USDT-SWAP.
    const runs=$el("div","run-grid"),coin=recipeCoin(report.strategy)??recipeCoin(S.selected?.id===report.version_id?S.selected:null)??"BTC";
    for(const run of report.runs||[]){
      const card=$el("article","run-card"),head=$el("div","run-head");head.append($el("b","",safe(run.name,"Прогін")),$el("span","",run.incomplete||run.status==="INCOMPLETE"?"Неповний":statusLabel(run.status)));card.append(head);
      const m=run.metrics||{},grid=$el("div","run-metrics");
      for(const [label,value] of [["PnL",`${fmt(m.pnl_usdt)} USDT`],["Дохідність",pct(m.return_pct)],["Просадка",plainPct(m.max_drawdown_pct)],["Угоди",fmt(m.trades,0)],["Комісії",`${fmt(m.fees_usdt,6)} USDT`],["Funding",`${fmt(m.funding_usdt,6)} USDT`],["Залишок",`${fmt(m.remaining_btc,6)} ${coin}`]]){const line=$el("span","");line.append(document.createTextNode(`${label}: `),$el("b","",value));grid.append(line);}card.append(grid);
      const lifecycle=run.lifecycle||report.lifecycle;if(lifecycle)card.append(renderLifecycle(lifecycle));
      if(run.curve?.length>1)card.append(section("Equity",chart(run.curve)));
      const events=Array.isArray(run.events)?run.events:[];
      if(events.length){const disclosure=$el("details","run-events"),summary=$el("summary","",`Усі кроки (${events.length})`),list=$el("ol","run-event-list");
        for(const event of events){const item=$el("li",""),details=$el("details","run-event"),eventSummary=$el("summary",""),symbol=$el("b","",event.step??"·");eventSummary.append($el("time","",clock(event.at)),symbol,$el("span","",event.message||event.code||"Подія"));details.append(eventSummary);if(event.data!==undefined)details.append($el("pre","run-event-data",JSON.stringify(event.data,null,2)));item.append(details);list.append(item);}
        disclosure.append(summary,list);card.append(disclosure);
      }
      runs.append(card);
    }
    host.append(section("Окремі прогони",runs));const hashes=$el("div","hash-list");for(const [label,value] of [["Рецепт",report.strategy_sha256],["Дані",report.data_sha256],["Код",report.code_sha256]])hashes.append($el("div","",`${label}: ${safe(value,"—")}`));host.append(section("Відтворюваність · контрольні відбитки",hashes));
  }
  function renderLifecycle(lifecycle,options={}){if(window.ArenaLifecycleV6&&Array.isArray(lifecycle.bots))return window.ArenaLifecycleV6.renderLifecycle(lifecycle,options);const box=$el("section","lifecycle-report"),head=$el("div","lifecycle-head"),title=$el("b","",safe(lifecycle.label,"Перевірка циклу")),summary=$el("span","",`${lifecycle.diagnostic?"Діагностика":"Етапи"} · ${safe(lifecycle.completed,0)} / ${safe(lifecycle.total,0)} зафіксовано${lifecycle.all_stages_observed===true?" · усі етапи спостережено":lifecycle.all_stages_observed===false?" · не всі етапи спостережено":""}`);head.append(title,summary);box.append(head);const rows=Array.isArray(lifecycle.rows)?lifecycle.rows:[],list=$el("ol","lifecycle-list");const labels={observed:"Зафіксовано",pending:"Очікується",not_observed:"Не спостережено",blocked:"Заблоковано"};for(const row of rows){const state=Object.prototype.hasOwnProperty.call(labels,row.status)?row.status:"unknown",item=$el("li",`lifecycle-row ${state}`),name=$el("b","",safe(row.label,row.id||"Етап")),statusText=`${row.required===false?"Додаткова перевірка · ":""}${labels[state]||safe(row.status,"Невідомо")}`,status=$el("span","",statusText),detail=$el("p","",safe(row.detail,""));item.append(name,status);if(row.detail)item.append(detail);if(row.status==="observed"&&(row.event_at||row.event_seq!==undefined)){const ref=$el("small","lifecycle-ref",[row.event_at?clock(row.event_at):null,row.event_seq!==undefined?`подія #${safe(row.event_seq)}`:null].filter(Boolean).join(" · "));item.append(ref);}list.append(item);}box.append(list);if(Array.isArray(lifecycle.limitations)&&lifecycle.limitations.length){const limits=$el("ul","lifecycle-limitations");for(const item of lifecycle.limitations)limits.append($el("li","",typeof item==="string"?item:JSON.stringify(item)));box.append(limits);}return box;}
  function feedbackNode(feedback){const host=$el("div","report-feedback");if(Array.isArray(feedback)){if(!feedback.length){host.textContent="Відгук відсутній у звіті.";return host;}const list=$el("ul","feedback-list");for(const item of feedback)list.append($el("li","",typeof item==="string"?item:JSON.stringify(item)));host.append(list);return host;}host.textContent=feedback===null||feedback===undefined?"Відгук відсутній у звіті.":typeof feedback==="string"?feedback:JSON.stringify(feedback,null,2);return host;}
  function categorizedEvents(events,pattern){return (Array.isArray(events)?events:[]).filter(event=>pattern.test(String(event.code||"").toLowerCase()));}
  function authorReport(report){const strategy=report.strategy?versionPayload({strategy:report.strategy}):(S.selected?.id===report.version_id?versionPayload(S.selected):null);return {dataset_name:report.dataset_name,dataset_kind:report.dataset_kind,runtime:report.runtime??null,ai_cost_usd:report.ai_cost_usd??null,budget_usdt:report.budget_usdt??null,summary:report.summary??{},strategy_sha256:report.strategy_sha256??null,data_sha256:report.data_sha256??null,code_sha256:report.code_sha256??null,feedback:report.feedback??[],lifecycle:report.lifecycle??null,strategy,runs:(report.runs||[]).map(run=>{const events=Array.isArray(run.events)?run.events:[];return {name:run.name,status:run.status,metrics:run.metrics??{},lifecycle:run.lifecycle??report.lifecycle??null,notable_events:events.filter(event=>/entry.*fill|close.*fill|risk|drawdown|limit|entry_cancelled|entry_no_fill/.test(String(event.code||"").toLowerCase())),risk_limit:categorizedEvents(events,/risk|drawdown|limit/),entry_cancelled:categorizedEvents(events,/entry_cancelled/),entry_no_fill:categorizedEvents(events,/entry_no_fill/)};})};}
  function copyFeedback(){if(!S.trial)return;copyText(JSON.stringify(authorReport(S.trial),null,2),"Звіт автору");}
  function downloadFeedback(){if(!S.trial)return;download(`trial-${slug(S.trial.id)}.json`,JSON.stringify(S.trial,null,2));}
  function renderDemoVersionOptions(){const select=$("demo-version"),current=select.value;select.replaceChildren();const versions=S.versions.filter(version=>versionPayload(version));if(!versions.length){select.append($el("option","","Спершу додайте рецепт у майстерню"));select.disabled=true;return;}for(const version of versions){const option=$el("option","",`${safe(version.name,"Стратегія")} · v${safe(version.version,"—")}${testPlan(version)==="demo_cycle_v1"?" · технічна перевірка":` · ${statusLabel(version.status)}`}`);option.value=version.id;select.append(option);}select.disabled=false;select.value=versions.some(version=>version.id===current)?current:S.selectedId&&versions.some(version=>version.id===S.selectedId)?S.selectedId:versions[0].id;syncDemoStart();}
  function setDemoProbeDefaults(versionId){const selected=S.versions.find(version=>version.id===versionId);if(!selected||testPlan(selected)!=="demo_cycle_v1")return;$("demo-version").value=versionId;$("demo-budget").value="100";$("demo-duration").value="300";syncDemoStart();}
  async function addDemoProbeRecipe(){const button=$("add-demo-probe"),template=S.contract?.demo_probe_template;if(!template||template.test_plan!=="demo_cycle_v1"){showNotice("Технічний Demo-рецепт відсутній в актуальному контракті.",true);return;}const existing=S.versions.find(version=>testPlan(version)==="demo_cycle_v1");if(existing){await selectVersion(existing.id,true);setDemoProbeDefaults(existing.id);showNotice("Уже збережений технічний рецепт вибрано для OKX Demo. Нову версію не створено й сезон не запущено.");return;}const strategy=versionPayload({strategy:template});if(!strategy){showNotice("Demo-шаблон не містить повного формального рецепта.",true);return;}await withBusy(button,"Зберігаємо рецепт…",async()=>{const response=await post("/api/workshop/versions",{strategy,parent_id:null});const id=response.id||response.version_id||response.version?.id;if(!id)throw new Error("Сервер не повернув ідентифікатор нової версії.");S.pendingVersion=id;await refresh(id);setDemoProbeDefaults(id);showNotice("Технічний рецепт збережено й вибрано. Demo-сезон не запускався; натисни окрему кнопку запуску лише коли будеш готовий.");});}
  function selectedDemoProfile(){return X.state?.profiles?.find(profile=>profile.id===$("demo-account").value)||null;}
  function demoCheck(status,version){return (status?.demo_execution_checks||[]).find(check=>version&&check?.recipe_sha256===version.recipe_sha256)||status?.demo_execution_check;}
  function demoModeSettings(version){return window.DemoExecutionUI.settings(version,demoCheck(X.state,version),$("demo-execution-mode").value,X.state?.budgets?.workshop?.series_unknown_billing,X.state?.ordinary_execution_modes||[]);}
  function syncDemoStart(){
    const researchStatus=$("parallel-research-status"),researchState=window.StrategyResearch?.status(X.state);
    if(researchStatus&&researchState)researchStatus.textContent=researchState.text;
    if(!window.DemoExecutionUI){$("demo-start").disabled=true;$("demo-mode-help").textContent="Онови панель через Update Jev Trading Lab, щоб завантажити режим перевірки механіки.";return;}
    const profile=selectedDemoProfile(),versionId=$("demo-version").value,version=S.versions.find(item=>item.id===versionId),probe=testPlan(version)==="demo_cycle_v1",settings=demoModeSettings(version),mode=$("demo-execution-mode"),budget=$("demo-budget"),duration=$("demo-duration");
    mode.querySelector('option[value="demo_mechanics"]').disabled=!settings.available;
    mode.querySelector('option[value="rules_only"]').disabled=probe||!X.state?.ordinary_execution_modes?.includes("rules_only");
    mode.value=settings.mode;mode.disabled=X.busy;
    const locked=probe||settings.locked;
    if(locked){budget.value="100";duration.value="300";}
    budget.readOnly=locked;duration.disabled=locked;budget.classList.toggle("diagnostic-locked",locked);duration.classList.toggle("diagnostic-locked",locked);
    $("demo-mode-help").textContent=settings.mechanics?window.DemoExecutionUI.mechanicsHelp(versionPayload(version)?.rules):settings.available?"Jev може відхилити вхід. Щоб перевірити виконання цього рецепта без вето Jev, можна явно вибрати режим механіки.":"Jev підтверджує вхід. Режим механіки без Jev доступний лише для незмінених закріплених Demo-рецептів: V8 short / partial / tail і R2 ETH post-only.";
    $("demo-start").disabled=X.busy||!versionId||!profile?.configured||settings.paidBlocked||Boolean(X.state?.active?.workshop_demo&&X.state.active.workshop_demo.status!=="DONE");
    if(settings.mode==="rules_only")$("demo-mode-help").textContent="Вхід і докупівля за правилами рецепта, без вето Jev. Ризик-ліміти, SL і Stop діють. Точний запит записується для SHADOW після бою; Jev зараз не викликається.";
    $("demo-check").disabled=X.busy||!profile?.configured;
    const help=$("demo-profile-status");
    if(!profile){help.textContent="Профіль OKX Demo не налаштовано. Додайте окрему demo-підсистему у файлі конфігурації.";return;}
    help.textContent=profile.ready?`${safe(profile.label)} · Demo · перевірено ${stamp(profile.last_checked_at)} · готовий до запуску.`:profile.configured?`${safe(profile.label)} · Demo · ${safe(profile.reason,"доступ ще не перевірявся")}. Старт виконає read-only preflight.`:`${safe(profile.label)} · ${safe(profile.reason,"профіль не налаштовано")}. Налаштуйте конфігурацію OKX Demo.`;
    if(profile.ready&&profile.funding_status==="unknown_schedule")help.textContent+=" Funding: календар невідомий; фактичні суми звіряються за журналом OKX.";
    help.classList.toggle("warning",Boolean(profile.ready&&profile.permission_warning));
    if(profile.ready&&profile.permission_warning)help.textContent+=" "+safe(profile.permission_warning);
    if(settings.paidBlocked)help.textContent+=" Jev: рахунок невідомий — новий платний виклик заблоковано.";
    if(probe)help.textContent+=" Технічний цикл зафіксовано: 100 USDT · 300 с · Demo only.";
  }
  function renderDemoProfiles(){const profiles=(X.state?.profiles||[]).filter(profile=>profile.environment==="OKX_DEMO"),select=$("demo-account"),signature=JSON.stringify(profiles.map(p=>[p.id,p.label,p.configured,p.ready,p.reason,p.last_checked_at]));if(signature!==X.profileSig){const current=select.value;select.replaceChildren();for(const profile of profiles){const option=$el("option","",`${safe(profile.label,profile.id)} · ${profile.configured?(profile.ready?"готовий":"налаштований"):"не налаштований"}`);option.value=profile.id;select.append(option);}select.disabled=!profiles.length;select.value=profiles.some(profile=>profile.id===current)?current:profiles[0]?.id||"";X.profileSig=signature;}renderDemoVersionOptions();syncDemoStart();}
  function renderExchangeBudgets(){const host=$("demo-budget-status"),b=X.state?.budgets?.workshop;if(!b){host.textContent="Сукупний ліміт Jev недоступний.";return;}host.textContent=`Jev у Demo-майстерні: ${safe(b.series_attempts,0)} запитів · $${fmt(b.series_reported_cost_usd,4)} звітної вартості / $0.01 сукупного м’якого порогу${b.series_unknown_billing?" · рахунок невідомий":""}. Поріг може бути перевищений одним запитом.`;}
  function renderDemoRun(run){
    const host=$("demo-season");
    const sameRun=Boolean(run&&host.dataset.runId===String(run.id));
    let journalWasOpen=false,eventScrollTop=0;
    const openEventSeqs=new Set();
    const openBotIds=new Set();let manifestWasOpen=false;
    if(sameRun){
      host.querySelectorAll(".v6-bot-lifecycle[open][data-bot-id]").forEach(item=>openBotIds.add(item.dataset.botId));manifestWasOpen=Boolean(host.querySelector(".v6-manifest[open]"));
      const priorJournal=host.querySelector(".exchange-event-journal");
      journalWasOpen=Boolean(priorJournal?.open);
      const priorEvents=host.querySelector(".exchange-events");
      eventScrollTop=priorEvents?.scrollTop||0;
      priorEvents?.querySelectorAll(".exchange-event[open][data-event-seq]").forEach(item=>openEventSeqs.add(item.dataset.eventSeq));
    }
    host.replaceChildren();
    host.hidden=!run;
    if(!run){delete host.dataset.runId;return;}
    host.dataset.runId=String(run.id);

    const status={RUNNING:"Входи відкриті · OKX Demo",DRAINING:"Завершує відкриту Demo-позицію",CLOSING:"Закриває demo-позицію",PAUSED:"Пауза · відновлення закриває позиції",DONE:"Завершено · OKX Demo"}[run.status]||safe(run.status);
    const head=$el("div","exchange-run-head");
    head.append($el("strong","",`${status} · ${safe(run.id)}`),$el("span","",`${safe(run.duration_seconds,0)} с · ${fmt(run.budget_usdt)} USDT / бот`));
    host.append(head);
    host.append($el("p","exchange-status",demoModeLabel(run.execution_mode)));

    const terminal=!["RUNNING","DRAINING","CLOSING","PAUSED"].includes(run.status);
    if(terminal&&(run.error||run.incomplete))host.append($el("p","exchange-run-error",`${run.incomplete?"Неповний звіт. ":""}${safe(run.error,"Підсумок не підтверджено.")}`));
    const safeBots=Array.isArray(run.bots)?run.bots.filter(bot=>bot.strategy_id!=="cash"):[];
    const bots=$el("div","exchange-bots");
    for(const bot of safeBots){
      const row=$el("div","exchange-bot");
      row.append($el("b","",safe(bot.name,"Бот")),$el("span","",`${safe(bot.status)} · equity ${fmt(bot.equity_usdt)} USDT · PnL ${fmt(bot.pnl_usdt)} USDT · комісії ${fmt(bot.fees_usdt,6)} · funding ${fmt(bot.funding_usdt,6)} USDT · Jev ${usdCost(bot.ai_cost_usd,bot.ai_cost_complete!==false)}`));
      bots.append(row);
    }
    host.append(bots);
    if(run.demo_operating_review){
      const report=run.demo_operating_review,required=report.rows.filter(row=>row.required),observed=required.filter(row=>row.status==="observed").length;
      const proof=$el("details","contract-detail"),summary=$el("summary","",`${report.evidence_kind==="SYNTHETIC"?"Синтетичний приклад · ":""}Функції виконання · ${observed}/${required.length} підтверджено`),list=$el("dl","contract-list");proof.open=true;
      for(const row of report.rows){const item=$el("div",""),value={observed:"Підтверджено",not_observed:"Ще не спостерігали",unknown:"Неповні дані",violated:"Порушено"}[row.status]||"Невідомо";item.append($el("dt","",window.DemoExecutionUI?.stageLabel(row.id)||row.id),$el("dd","",value));list.append(item);}
      proof.append(summary,list,$el("p","subtle","Зараховуються фактичні виконання й перевірені записи. Stop та відновлення — окремі тести. Прибутковість тут не оцінюється."));host.append(proof);
    }
    if(run.lifecycle){const lifecycleNode=renderLifecycle(run.lifecycle,window.ArenaLifecycleV6?.optionsForRun(run)||{executionMode:"demo"});host.append(lifecycleNode);lifecycleNode.querySelectorAll("details[data-bot-id]").forEach(item=>item.open=sameRun&&openBotIds.has(item.dataset.botId));const manifest=lifecycleNode.querySelector(".v6-manifest");if(manifest)manifest.open=sameRun&&manifestWasOpen;}

    const actions=$el("div","exchange-actions");
    if(["RUNNING","DRAINING","CLOSING"].includes(run.status)){
      const stop=$el("button","quiet-action","Зупинити · закрити позицію");
      stop.type="button";stop.disabled=X.busy;
      stop.addEventListener("click",()=>exchangeMutation("/api/exchange/stop",{id:run.id,bot_id:null}));
      actions.append(stop);
    }
    if(run.status==="PAUSED"){
      const resume=$el("button","primary-action","Відновити закриття");
      resume.type="button";resume.disabled=X.busy;
      resume.addEventListener("click",()=>exchangeMutation("/api/exchange/resume",{id:run.id}));
      actions.append(resume);
    }
    host.append(actions);

    const allEvents=Array.isArray(run.events)?[...run.events].sort((a,b)=>(Number(a.seq)||0)-(Number(b.seq)||0)):[];
    if(!allEvents.length){host.append($el("p","subtle","Очікуємо перші події від OKX Demo."));return;}
    const journal=$el("details","exchange-event-journal"),journalSummary=$el("summary","",`Журнал кроків · ${allEvents.length} із ${run.events_count??allEvents.length} подій`),events=$el("div","exchange-events");
    const stepLabels={1:"Перевірка умов",2:"Рішення Jev / правила",3:"Вхід або докупівля",4:"Перевірка позиції",5:"Утримання",6:"Закриття"};
    journal.append(journalSummary);
    const exportEvents=$el("button","quiet-action","Завантажити повний журнал JSON");exportEvents.type="button";exportEvents.addEventListener("click",async()=>{try{const full=run.history_complete===false?await request(`/api/exchange/runs/${encodeURIComponent(run.id)}`):run;download(`okx-demo-${slug(run.id)}.json`,JSON.stringify(full,null,2));}catch(error){showNotice(error.message,true);}});journal.append(exportEvents);
    for(const event of allEvents){
      const seq=event.seq===null||event.seq===undefined?"":String(event.seq);
      const item=$el("details","exchange-event"),step=event.step?`Крок ${event.step} · ${stepLabels[event.step]||"етап"}`:"Крок не вказано";
      const summary=$el("summary","",`${clock(event.at)} · ${step} · ${safe(event.message,event.code||"Подія")}`);
      item.append(summary);
      if(seq)item.dataset.eventSeq=seq;
      if(event.data&&Object.keys(event.data).length)item.append($el("pre","",JSON.stringify(event.data,null,2)));
      if(seq&&openEventSeqs.has(seq))item.open=true;
      events.append(item);
    }
    journal.append(events);
    journal.open=sameRun&&journalWasOpen;
    host.append(journal);
    events.scrollTop=sameRun?eventScrollTop:0;
  }
  function renderDemoHistory(){const host=$("demo-history");host.replaceChildren();const relevant=(X.state?.history||[]).filter(run=>run.context==="workshop_demo").slice(0,8);if(!relevant.length){host.append($el("p","subtle","Історія OKX Demo з’явиться після першого запуску."));return;}host.append($el("h3","","Історія demo-запусків"));for(const run of relevant){const button=$el("button","exchange-history-item",`${stamp(run.created_at)} · ${safe(run.status)} · ${demoModeLabel(run.execution_mode)} · ${safe(run.id)}`);button.type="button";button.addEventListener("click",async()=>{try{const detail=await request(`/api/exchange/runs/${encodeURIComponent(run.id)}`);X.selectedHistoryId=run.id;X.activeRun=detail;renderDemoRun(detail);}catch(error){showNotice(error.message,true);}});host.append(button);}}
  let exchangeInFlight=null;  // one state request at a time: a slow answer is never overtaken by a pile of timer ticks behind it
  const STATE_POLL={timeout:30000},RUN_POLL={timeout:60000};  // a request that never answers cannot stop the polling
  // Timer ticks read the server's shared short-lived copy (one status per copy for all open tabs, lock time capped);
  // a read after an owner action is fresh, never a copy that was loading before the action.
  function refreshExchange(fresh=false){if(exchangeInFlight)return fresh?exchangeInFlight.then(()=>refreshExchange(true)):exchangeInFlight;exchangeInFlight=loadExchange(!fresh).finally(()=>{exchangeInFlight=null;});return exchangeInFlight;}
  async function loadExchange(viewer){try{const data=await request("/api/exchange/state",undefined,{...STATE_POLL,viewer});X.state=data;X.generation=data.generation;renderDemoProfiles();renderExchangeBudgets();renderDemoHistory();const active=data.active?.workshop_demo||null;if(active){X.selectedHistoryId=null;try{X.activeRun=Array.isArray(active.events)&&Array.isArray(active.bots)?active:await request(`/api/exchange/runs/${encodeURIComponent(active.id)}`,undefined,RUN_POLL);}catch{X.activeRun=active;}renderDemoRun(X.activeRun);}else{const history=(data.history||[]).find(run=>run.context==="workshop_demo"&&run.id===X.selectedHistoryId)|| (data.history||[]).find(run=>run.context==="workshop_demo");if(!X.selectedHistoryId&&history)X.selectedHistoryId=history.id;if(X.selectedHistoryId){if(X.activeRun?.id!==X.selectedHistoryId||history&&X.activeRun?.status!==history.status)try{X.activeRun=await request(`/api/exchange/runs/${encodeURIComponent(X.selectedHistoryId)}`,undefined,RUN_POLL);}catch{}renderDemoRun(X.activeRun);}else{X.activeRun=null;renderDemoRun(null);}}syncDemoStart();const terminal=(data.history||[]).find(run=>run.context==="workshop_demo"&&run.status==="DONE");if(terminal&&X.lastTerminalId!==terminal.id){X.lastTerminalId=terminal.id;refresh();}}
    catch(error){const host=$("demo-profile-status");host.textContent=`Не вдалося прочитати статус OKX Demo: ${error.message}`;$("demo-start").disabled=true;}}
  async function freshExchangeGeneration(){const data=await request("/api/exchange/state");X.state=data;X.generation=data.generation;return data;}
  async function exchangeMutation(path,payload){if(X.busy)return;X.busy=true;syncDemoStart();try{const latest=await freshExchangeGeneration();await request(path,{generation:latest.generation,...payload});showNotice("Запит прийнято для біржового режиму.");await refreshExchange(true);await refresh();}catch(error){showNotice(error.message,true);}finally{X.busy=false;syncDemoStart();}}
  async function checkDemoProfile(){if(X.busy)return;const profile=selectedDemoProfile();if(!profile?.configured)return;X.busy=true;syncDemoStart();try{const latest=await freshExchangeGeneration();await request("/api/exchange/check",{generation:latest.generation,account_ids:[profile.id]});await refreshExchange(true);showNotice("Read-only перевірку OKX Demo завершено.");}catch(error){showNotice(error.message,true);}finally{X.busy=false;syncDemoStart();}}
  async function startDemoSeason(){
    if(X.busy)return;
    const profile=selectedDemoProfile(),versionId=$("demo-version").value,version=S.versions.find(item=>item.id===versionId),settings=demoModeSettings(version),requestedMode=$("demo-execution-mode").value,budget=Number($("demo-budget").value),duration=Number($("demo-duration").value);
    if(!profile?.configured||!versionId){showNotice("Налаштуйте Demo-профіль і виберіть версію рецепта.",true);return;}
    if(requestedMode!==settings.mode){showNotice("Вибраний режим недоступний для цього рецепта або версії сервера.",true);return;}
    if((testPlan(version)==="demo_cycle_v1"||settings.mechanics)&&(budget!==100||duration!==300)){showNotice("Для технічної перевірки дозволені лише 100 USDT і 300 секунд.",true);return;}
    if(!Number.isFinite(budget)||budget<25||budget>500){showNotice("Бюджет OKX Demo має бути від 25 до 500 USDT.",true);return;}
    X.busy=true;syncDemoStart();
    try{
      const latest=await freshExchangeGeneration();
      if(settings.mechanics&&!window.DemoExecutionUI.eligible(version,demoCheck(latest,version)))throw new Error("Сервер більше не підтримує вибрану перевірку механіки.");
      if(settings.mode==="rules_only"&&!latest.ordinary_execution_modes?.includes("rules_only"))throw new Error("Сервер більше не підтримує RULES_ONLY.");
      await request("/api/exchange/start",{generation:latest.generation,context:"workshop_demo",strategy_ids:[versionId],account_ids:[profile.id],budget_usdt:budget,duration_seconds:duration,...(settings.mode!=="shared_jev"?{execution_mode:settings.mode}:{})});
      X.activeRun=null;X.selectedHistoryId=null;showNotice(settings.mechanics?"Перевірка механіки OKX Demo запущена без Jev. Позиція завершується за правилами рецепта.":"OKX Demo запущено. Виконання відбувається лише на demonstration account.");await refreshExchange(true);
    }catch(error){showNotice(error.message,true);}finally{X.busy=false;syncDemoStart();}
  }
  function bind(){
    $("refresh-state").addEventListener("click",()=>refresh());$("new-version").addEventListener("click",newVersion);$("clone-version").addEventListener("click",cloneVersion);$("save-version").addEventListener("click",saveVersion);$("fill-template").addEventListener("click",loadTemplate);$("export-recipe").addEventListener("click",exportVersion);$("recipe-file").addEventListener("change",event=>importFile(event.target.files?.[0]));$("dataset-select").addEventListener("change",updateDatasetDescription);$("run-training").addEventListener("click",train);$("freeze-version").addEventListener("click",freeze);$("promote-version").addEventListener("click",promote);$("copy-prompt").addEventListener("click",()=>copyText(promptText(),"Prompt"));$("copy-prompt-top").addEventListener("click",()=>copyText(promptText(),"Завдання автору"));$("download-prompt").addEventListener("click",downloadPrompt);$("copy-feedback").addEventListener("click",copyFeedback);$("download-feedback").addEventListener("click",downloadFeedback);$("close-trial").addEventListener("click",()=>$("trial-dialog").close());$("trial-dialog").addEventListener("click",event=>{if(event.target===$("trial-dialog"))$("trial-dialog").close();});
    $("demo-execution-mode").addEventListener("change",syncDemoStart);$("demo-account").addEventListener("change",syncDemoStart);$("demo-version").addEventListener("change",syncDemoStart);$("demo-check").addEventListener("click",checkDemoProfile);$("demo-start").addEventListener("click",startDemoSeason);$("add-demo-probe").addEventListener("click",addDemoProbeRecipe);
  }
  async function openResearch() {
    if(!researchParent)return;
    const parent=window.StrategyResearch?.request(location.search);researchParent=null;
    if(!parent||!S.versions.some(v=>v.id===parent)){showNotice("Бойову версію не знайдено в майстерні. Вибери її зі списку або імпортуй точний рецепт.",true);return;}
    await selectVersion(parent,true);cloneVersion();
    if($("demo-version"))$("demo-version").value=parent;
    showNotice("Підготовлено копію бойового рецепта. Збережи зміни як нову чернетку; бойова версія не змінюється. Demo обери для збереженої чернетки й запускай окремо.");
  }
  bind();diagnosticsUI=window.ArenaWorkshopToolsUI.mount($("workshop-tools-host"),{editor:$("recipe-json"),getVersionId:()=>S.selectedId,getInstrument:id=>recipeInstrument(S.selected?.id===id?S.selected:null)??recipeInstrument(S.versions.find(v=>v.id===id)),onTrial:openTrial});refresh().then(openResearch);refreshExchange();X.runTimer=setInterval(refreshExchange,2000);window.addEventListener("beforeunload",()=>clearInterval(X.runTimer));
})();
