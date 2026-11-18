// «Чого чекає бот» (B-05): pure model over public run facts. No browser, requests, keys or financial actions.
const test=require("node:test"),assert=require("node:assert/strict");
const W=require("../web/bot-waiting.js");
const gates=[{feature:"spread_bps",op:"lte",expected:5,actual:.4,passed:true},{feature:"total_entries",op:"lt",expected:20,actual:0,passed:true}];
const v1={id:"fx",rules:{side:"long",entry:{mode:"all",conditions:[{feature:"rsi_14",op:"lt",value:35},{feature:"return_1m_bps",op:"gt",value:0}]}}};
const bot=(checks,extra={})=>({id:"fx",status:"WAITING",position_btc:0,checks,...extra});
const running={status:"RUNNING",mode:"PAPER"};

test("G1 explains H1/H4 preparation, data failure and a frozen setup without invented scalar conditions",()=>{
  const strategy={strategy_schema_version:3,rules:{builtin:"IKIGAI_G1_PAPER_BUILTIN_V1"}};
  const view=code=>W.waiting(bot([]),{strategy,run:running,events:code?[{bot_id:"fx",code,at:"2026-10-26T21:00:00Z"}]:[]});
  assert.match(view().summary,/точки трикутника H1/); assert.deepEqual(view().groups,[]);
  assert.equal(view("g1_input_unknown").state,"unknown");
  assert.match(view("g1_input_unknown").summary,/Вхід заблокований/);
  assert.match(view("g1_setup").summary,/трикутник зафіксовано/);
  assert.equal(view("g1_setup").checkedAt,"2026-10-26T21:00:00Z");
  assert.equal(W.waiting(bot([], {position_btc:.001}),{strategy,run:running}).state,"position");
  assert.equal(W.waiting(bot([]),{strategy,run:{status:"DRAINING"}}).state,"closed");
});

test("V1 entry conditions read as words with the latest value, distance and closeness",()=>{
  const model=W.waiting(bot([{feature:"rsi_14",op:"lt",expected:35,actual:42,passed:false},{feature:"return_1m_bps",op:"gt",expected:0,actual:3.2,passed:true},...gates]),
    {strategy:v1,run:running,diagnostic:{funnel:{entry:{checks:120,signals:0}}}});
  assert.equal(model.state,"entry");assert.equal(model.summary,"Виконано 1 з 2 умов входу.");
  const [rsi,move]=model.groups[0].conditions;
  assert.equal(model.groups[0].title,"Умови входу · LONG");assert.equal(model.groups[0].mode,"all");
  assert.equal(rsi.text,"RSI за 14 хв < 35");assert.equal(rsi.status,"not_met");assert.equal(rsi.now,"зараз 42");assert.equal(rsi.need,"ще на 7 нижче");
  assert.ok(Math.abs(rsi.closeness-.86)<1e-9);
  assert.equal(move.status,"met");assert.equal(move.now,"зараз 3,2 bps");assert.equal(move.need,null);assert.equal(move.closeness,1);
  assert.deepEqual(model.gates.map(g=>[g.text,g.status]),[["Спред ≤ 5 bps","met"],["Входів цього учасника в бою < 20","met"]]);
  assert.deepEqual(model.funnel,{checks:120,signals:0});assert.equal(model.note,"Це умови рецепта, а не прогноз ціни.");
});
test("unknown values are said out loud and never counted as met; arena limits are named",()=>{
  const checks=[{feature:"rsi_14",op:"lt",expected:35,actual:null,passed:null},{feature:"return_1m_bps",op:"gt",expected:0,actual:1,passed:true},
    {...gates[0],actual:7,passed:false},gates[1]];
  const model=W.waiting(bot(checks),{strategy:v1,run:running});
  assert.equal(model.groups[0].conditions[0].status,"unknown");assert.equal(model.groups[0].conditions[0].now,"невідомо");
  assert.match(model.summary,/^Бракує даних для 1 з 2 умов — поки вони невідомі, вхід неможливий\. Обмеження арени: спред ширший за ліміт\.$/);
});
test("checks that do not match the recipe are shown as not yet checked instead of being guessed",()=>{
  const model=W.waiting(bot([...gates]),{strategy:v1,run:running});
  assert.deepEqual(model.groups[0].conditions.map(c=>c.status),["pending","pending"]);assert.equal(model.summary,"Умови ще не перевірені на цьому кадрі.");
  const replay=W.waiting(bot([{feature:"rsi_14",op:"lt",expected:35,actual:20,passed:true},{feature:"return_1m_bps",op:"gt",expected:0,actual:1,passed:true},...gates]),{strategy:v1,run:running,replay:true});
  assert.deepEqual(replay.groups[0].conditions.map(c=>c.status),["pending","pending"],"replay never shows the final live checks as historical values");
  const event={bot_id:"fx",code:"entry_checks",at:"2026-10-03T10:00:00+00:00",data:{checks:[{feature:"rsi_14",op:"lt",expected:35,actual:30,passed:true},{feature:"return_1m_bps",op:"gt",expected:0,actual:-1,passed:false},...gates]}};
  const visible=W.waiting(bot([]),{strategy:v1,run:running,replay:true,events:[event]});
  assert.deepEqual(visible.groups[0].conditions.map(c=>c.status),["met","not_met"]);assert.equal(visible.checkedAt,"2026-10-03T10:00:00+00:00");
});
test("states before conditions: Cash, unknown size, open position, finished, closed window, Jev and pending fill",()=>{
  const ctx={strategy:v1,run:running};
  assert.equal(W.waiting({id:"cash",position_btc:0},ctx).state,"cash");
  assert.equal(W.waiting(bot([],{position_btc:null}),ctx).state,"unknown");
  assert.equal(W.waiting(bot([],{position_btc:.001}),ctx).state,"position");
  assert.equal(W.waiting(bot([],{stop_requested:true}),ctx).state,"finished");
  assert.equal(W.waiting(bot([]),{...ctx,run:{status:"DRAINING"}}).state,"closed");
  assert.equal(W.waiting(bot([]),{...ctx,run:{status:"PAUSED"}}).state,"paused");
  const jev=W.waiting(bot([]),{...ctx,diagnostic:{current_phase:{code:"jev"}}});assert.equal(jev.state,"jev");assert.match(jev.summary,/не прогноз/);
  assert.equal(W.waiting(bot([]),{...ctx,diagnostic:{current_phase:{code:"pending_model_fill"}}}).state,"pending");
  assert.equal(W.waiting(bot([],{prepared:true}),ctx).state,"prepared");
});
test("V2 direct entry: `not` reads as its effective condition and invalidate is a separate veto group",()=>{
  const strategy={strategy_schema_version:2,rules:{side:"short",entry:{short:{setup:null,expiry_bars:1,
    confirm:{all:[{feature:"bollinger_z_20",op:"gt",value:2},{not:{feature:"spread_bps",op:"gt",value:2}}]},
    invalidate:{any:[{feature:"funding_rate_bps",op:"lt",value:-1}]}}}}};
  const checks=[{path:"/all/0",feature:"bollinger_z_20",op:"gt",expected:2,actual:1.5,passed:false,side:"short"},
    {path:"/all/1/not",feature:"spread_bps",op:"gt",expected:2,actual:1,passed:false,side:"short"},
    {path:"/any/0",feature:"funding_rate_bps",op:"lt",expected:-1,actual:.5,passed:false,side:"short"},...gates];
  const model=W.waiting(bot(checks),{strategy,run:running});
  const [entry,veto]=model.groups;
  assert.equal(entry.mode,"tree");assert.equal(entry.title,"Умови входу · SHORT");
  assert.deepEqual(entry.conditions.map(c=>[c.text,c.status]),[["Відхилення від середньої за 20 хв (z Боллінджера) > 2σ","not_met"],["Спред ≤ 2 bps","met"]]);
  assert.equal(entry.conditions[0].need,"ще на 0,5σ вище");
  assert.equal(veto.kind,"veto");assert.equal(veto.title,"Не входить, якщо · SHORT");
  assert.deepEqual([veto.conditions[0].status,veto.conditions[0].need,veto.conditions[0].closeness,veto.conditions[0].now],["veto_clear",null,null,"не заважає · зараз 0,5 bps"]);
  assert.equal(model.summary,"Виконано 1 з 2 перевірок (формула з «або / не»).");
  const blocked=W.waiting(bot(checks.map(c=>c.feature==="funding_rate_bps"?{...c,actual:-3,passed:true}:c)),{strategy,run:running});
  assert.equal(blocked.groups[1].conditions[0].status,"veto_hit");assert.match(blocked.summary,/Умова «не входить» зараз блокує вхід\.$/);
});
test("V2 temporal: step 1 waits for setup, step 2 shows confirmation with its deadline, resets are explained",()=>{
  const strategy={strategy_schema_version:2,rules:{side:"long",entry:{long:{expiry_bars:5,
    setup:{all:[{feature:"return_5m_bps",op:"lt",value:-30}]},confirm:{all:[{feature:"return_1m_bps",op:"gt",value:2}]},
    invalidate:{any:[{feature:"return_1m_bps",op:"lt",value:-20}]}}}}};
  const bar=Date.parse("2026-10-03T10:00:00Z");
  const trace=(seq,code,after,checks,extra={})=>({seq,bot_id:"fx",code,data:{side:"long",before:"IDLE",after,bar_ms:bar+seq*60000,checks,...extra}});
  const idle=W.waiting(bot([...gates]),{strategy,run:running,events:[trace(1,"setup_waiting","IDLE",[{path:"/all/0",feature:"return_5m_bps",op:"lt",expected:-30,actual:-12,passed:false}])]});
  assert.equal(idle.stages[0].step,1);assert.equal(idle.stages[0].label,"LONG · крок 1 з 2: чекає setup");
  assert.equal(idle.groups[0].kind,"setup");assert.equal(idle.groups[0].conditions[0].need,"ще на 18 bps нижче");
  assert.equal(idle.summary,"Виконано 0 з 1 умови входу.");
  const armedEvents=[trace(1,"setup_armed","ARMED",[]),trace(2,"setup_waiting_confirmation","ARMED",[
    {path:"/any/0",feature:"return_1m_bps",op:"lt",expected:-20,actual:1,passed:false},{path:"/all/0",feature:"return_1m_bps",op:"gt",expected:2,actual:1,passed:false}])];
  const armed=W.waiting(bot([...gates]),{strategy,run:running,events:armedEvents});
  assert.equal(armed.stages[0].step,2);assert.equal(armed.stages[0].expiresAtMs,bar+60000+5*60000);
  assert.deepEqual(armed.groups.map(g=>g.kind),["confirm","veto"]);
  assert.equal(armed.groups[0].conditions[0].now,"зараз 1 bps");assert.equal(armed.groups[1].conditions[0].status,"veto_clear");
  const reset=W.waiting(bot([...gates]),{strategy,run:running,events:[...armedEvents,trace(3,"setup_expired","IDLE",[])]});
  assert.equal(reset.stages[0].step,1);assert.equal(reset.stages[0].detail,"Попередній setup прострочено — чекаємо новий.");
  assert.equal(reset.groups[0].conditions[0].status,"pending");
});
test("leaf paths follow the engine's depth-first order and unknown features keep a readable label",()=>{
  assert.deepEqual(W.leaves({all:[{feature:"a",op:"gt",value:1},{any:[{feature:"b",op:"lt",value:2},{not:{feature:"c",op:"gte",value:3}}]}]}).map(l=>[l.path,l.negated]),
    [["/all/0",false],["/all/1/any/0",false],["/all/1/any/1/not",true]]);
  assert.equal(W.treeMode({any:[{feature:"a",op:"gt",value:1}]}),"any");assert.equal(W.treeMode({not:{feature:"a",op:"gt",value:1}}),"tree");
  assert.equal(W.feature("new_feature_x").label,"new feature x");
  const c=W.condition({feature:"rsi_14",op:"gt",value:50,negated:false},{feature:"rsi_14",op:"gt",expected:50,actual:50,passed:false});
  assert.equal(c.need,"треба вище за поріг");
});
test("Jev gate (B-01a): PAPER jev_gate and exchange jev_decision count alike; the last answer reads as p(proceed) against the gate",()=>{
  const F=require("../web/battle-facts.js");
  const events=[{bot_id:"fx",code:"jev_gate",at:"2026-10-03T10:00:00Z",data:{allowed:false,answer:{choice:"proceed",confidence:.6},kind:"entry"}},
    {bot_id:"fx",code:"jev_gate",at:"2026-10-03T10:02:00Z",data:{allowed:false,answer:{choice:"hold",confidence:.9},kind:"entry"}},
    {bot_id:"other",code:"jev_decision",data:{jev_allowed:true,answer:{choice:"proceed",confidence:.99}}},
    {bot_id:"fx",code:"jev_decision",at:"2026-10-03T10:05:00Z",data:{jev_allowed:true,confidence_threshold:.7,answer:{choice:"proceed",confidence:.86},kind:"add"}}];
  const view=W.jev(bot([]),{events,facts:F});
  assert.equal(view.state,"asked");
  assert.deepEqual([view.counts.decisions,view.counts.allowed,view.counts.blocked],[3,1,2]);
  assert.equal(view.summary,"Запитів до Jev: 3 · дозволів: 1 · відмов: 2 (нижче порога: 1, «утримати»: 1).");
  assert.deepEqual([view.decision.kind,view.decision.allowed,view.decision.choice,view.decision.gate,view.decision.pGate],["add",true,"proceed",.7,.85]);
  assert.ok(Math.abs(view.decision.pProceed-.93)<1e-12);
  const hold=W.jev(bot([]),{events:events.slice(0,2),facts:F});
  assert.ok(Math.abs(hold.decision.pProceed-.05)<1e-12,"a confident hold is a low p(proceed)");assert.equal(hold.decision.allowed,false);
  assert.match(view.note,/не прогноз ціни і не шанс прибутку/);
});
test("Jev gate (B-01a): rules-only rounds say so; Cash and missing facts show nothing; no answers yet is stated",()=>{
  const F=require("../web/battle-facts.js");
  assert.equal(W.jev(bot([]),{events:[],facts:F,diagnostic:{rows:[{id:"jev_entry",status:"not_applicable"}]}}).state,"unused");
  assert.equal(W.jev({id:"cash",position_btc:0},{events:[],facts:F}),null);
  const rulesOnly=W.jev(bot([]),{events:[],facts:F,run:{mode:"OKX_LIVE",execution_mode:"rules_only"}});
  assert.equal(rulesOnly.state,"unused");assert.match(rulesOnly.summary,/^RULES_ONLY: Jev не викликається/);
  assert.equal(W.jev(bot([]),{events:[],facts:F,run:{mode:"PAPER"}}).state,"unused","PAPER defaults to rules");
  assert.equal(W.jev(bot([]),{events:[],facts:F,run:{mode:"OKX_DEMO"}}).state,"idle","exchange defaults to shared_jev");
  assert.equal(W.jev(bot([]),{events:[{bot_id:"fx",code:"jev_gate",data:{allowed:true}}]}),null,"without the shared facts model nothing is guessed");
  const idle=W.jev(bot([]),{events:[],facts:F,diagnostic:{rows:[{id:"jev_entry",status:"pending"}]}});
  assert.equal(idle.state,"idle");assert.equal(idle.decision,null);assert.equal(idle.summary,"Jev ще не запитували: умови входу не виконувалися.");
});
