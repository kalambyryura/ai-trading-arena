"use strict";
/* Pure input adapter + one fixed local diagnostics POST. It never starts a trial or exchange worker. */
((root,factory)=>{
  const api=factory();if(typeof module==="object"&&module.exports)module.exports=api;else root.ArenaWorkshopToolsUI=api;
})(typeof window==="undefined"?{}:window,()=>{
  const object=value=>value!==null&&typeof value==="object"&&!Array.isArray(value);
  function parseObject(text,label,optional=true) {
    if(!String(text||"").trim()){if(optional)return undefined;throw new Error(label+" не заповнено.");}
    let value;try{value=JSON.parse(text);}catch{throw new Error(label+": JSON містить помилку.");}
    if(!object(value))throw new Error(label+": потрібен JSON об'єкт.");return value;
  }
  function payload(input) {
    const result={};
    if(input.source==="version"){
      if(!/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$/.test(input.versionId||""))throw new Error("Вкажи ID збереженої версії.");
      result.version_id=input.versionId;
    }else result.strategy=parseObject(input.recipe,"Рецепт",false);
    for(const [field,label] of [["features","Ознаки"],["snapshot","Snapshot"],["instrument","Instrument"]]){
      const value=parseObject(input[field],label);if(value!==undefined)result[field]=value;
    }
    for(const [field,key,label] of [["budget","budget_usdt","Бюджет"],["equity","equity_usdt","Equity"]]){
      if(String(input[field]||"").trim()){const value=Number(input[field]);if(!Number.isFinite(value)||value<=0)throw new Error(label+" має бути додатним числом.");result[key]=value;}
    }
    for(const [field,key] of [["leftTrial","left_trial_id"],["rightTrial","right_trial_id"]]){
      if(String(input[field]||"").trim()){if(!/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$/.test(input[field]))throw new Error("Некоректний ID проби.");result[key]=input[field];}
    }
    if(Boolean(result.left_trial_id)!==Boolean(result.right_trial_id))throw new Error("Для парного порівняння вкажи обидва trial IDs.");
    return result;
  }
  async function request(body,token) {
    if(typeof token!=="string"||!token||token==="__PANEL_TOKEN__")throw new Error("Локальний token сторінки відсутній. Онови майстерню.");
    const response=await fetch("/api/workshop/tools",{method:"POST",cache:"no-store",credentials:"same-origin",redirect:"error",
      headers:{"Content-Type":"application/json","X-Panel-Token":token},body:JSON.stringify(body)});
    let report;try{report=await response.json();}catch{throw new Error("Diagnostics не повернув JSON звіту.");}
    if(!response.ok)throw new Error(report.error||"HTTP "+response.status);return report;
  }
  function mount(host,options={}) {
    const panels=window.ArenaResearchPanels;if(!panels)throw new Error("Workshop renderer ще не завантажено.");
    const editor=options.editor||document.getElementById("recipe-json"),mk=(tag,cls,text)=>{const item=document.createElement(tag);item.className=cls||"";if(text!==undefined)item.textContent=text;return item;};
    host.replaceChildren();host.classList.add("workshop-tools-ui");
    host.append(mk("h2","","Локальний розбір рецепта"),mk("p","research-muted","Lint, пояснення умов, лоти та порівняння локальних проб під чинним регламентом."));
    const freedom=mk("details"),freedomSummary=mk("summary","","Можливості V2 для автора");freedom.append(freedomSummary,
      mk("p","tools-freedom","Окремі LONG і SHORT branches, setup → confirm → invalidate, all/any/not, власні add та exit. reduce_when + reduce_pct задають часткове скорочення. max_hold_seconds:null залишає часову межу автору; admission завершує лише entry/add. side:both вибирає напрямок однієї позиції; одночасний конфлікт сигналів дає HOLD."));host.append(freedom);
    const sourceRow=mk("div","tools-source"),sourceLabel=mk("label","","Джерело "),source=mk("select");source.setAttribute("aria-label","Джерело діагностики");
    [["editor","Поточний JSON"],["version","Збережена версія"]].forEach(([value,label])=>{const item=mk("option","",label);item.value=value;source.append(item);});sourceLabel.append(source);
    const version=mk("input");version.type="text";version.placeholder="ID збереженої версії";version.setAttribute("aria-label","ID версії для діагностики");version.hidden=true;sourceRow.append(sourceLabel,version);host.append(sourceRow);
    source.addEventListener("change",()=>{version.hidden=source.value!=="version";if(source.value==="version"&&options.getVersionId?.())version.value=options.getVersionId();});
    const context=mk("details"),contextSummary=mk("summary","","Дані для пояснення, лотів і парного порівняння");context.append(contextSummary,mk("p","research-muted","Необов'язкові локальні входи. Відсутні значення лишаються невідомими; котирування не завантажуються автоматично."));
    const fields=mk("div","tools-fields"),inputs={};
    function field(key,label,multiline,placeholder=""){
      const wrap=mk("label","tools-field",label),input=mk(multiline?"textarea":"input");input.setAttribute("aria-label",label);input.placeholder=placeholder;
      if(multiline){input.spellcheck=false;wrap.classList.add("tools-wide");}else input.type="text";
      wrap.append(input);fields.append(wrap);inputs[key]=input;
    }
    field("features","Features · JSON",true,'{"return_1m_bps":25,"position_age_seconds":60}');
    field("snapshot","Snapshot · JSON",true,'{"bid":100000,"ask":100001,"bid_btc":1,"ask_btc":1}');
    field("instrument","Instrument · JSON",true,'{"tick_size":0.1,"lot_step":0.01,"contract_value":0.01,"contract_multiplier":1,"minimum_order_units":0.01}');
    field("budget","Бюджет preview · USDT",false,"100");field("equity","Equity preview · USDT",false,"100");
    field("leftTrial","Ліва trial ID",false);field("rightTrial","Права trial ID",false);context.append(fields);host.append(context);
    const actions=mk("div","tools-actions"),run=mk("button","research-button","Перевірити рецепт");run.type="button";
    actions.append(run,mk("span","research-muted","Локальні дані · $0 AI"));host.append(actions);
    const notice=mk("p","tools-notice","Підготуй рецепт або ID версії та відкрий локальний розбір."),reportHost=mk("div");notice.setAttribute("role","status");notice.setAttribute("aria-live","polite");host.append(notice,reportHost);
    let busy=false,disposed=false;
    const tell=(text,kind="okay")=>{notice.textContent=text;notice.dataset.kind=kind;};
    function changed(){if(reportHost.childElementCount)tell("Вхідні дані змінилися. Онови розбір, щоб бачити чинний рецепт.","pending");}
    Object.values(inputs).forEach(input=>input.addEventListener("input",changed));editor?.addEventListener("input",changed);
    source.addEventListener("change",changed);version.addEventListener("input",changed);
    async function check(){
      if(busy||disposed)return;busy=true;run.disabled=true;
      try{
        const fromInputs={source:source.value,versionId:version.value.trim()||options.getVersionId?.(),recipe:options.getRecipe?.()??editor?.value??""};
        Object.entries(inputs).forEach(([key,input])=>{fromInputs[key]=input.value.trim();});
        const body=payload(fromInputs),token=options.token??document.querySelector('meta[name="panel-token"]')?.content;
        tell("Читаємо локальний diagnostics…","pending");
        const report=await request(body,token);if(disposed)return;
        panels.mountWorkshopTools(reportHost,report,{instrument:body.strategy?.instrument??options.getInstrument?.(body.version_id)??null,onField:options.onField||((path)=>{editor?.focus();tell("Перевір поле "+path+" у чинному JSON.");}),
          onTrial:options.onTrial||((id)=>host.dispatchEvent(new CustomEvent("arena:workshop-trial",{bubbles:true,detail:{id}})))});
        tell("Локальний розбір готовий. Рецепт збережено в редакторі без змін.");
      }catch(error){tell(error.message,"error");}finally{busy=false;run.disabled=false;}
    }
    run.addEventListener("click",check);
    return{check,selectVersion(id){version.value=id||"";changed();},destroy(){disposed=true;editor?.removeEventListener("input",changed);host.replaceChildren();}};
  }
  return{parseObject,payload,request,mount};
});
