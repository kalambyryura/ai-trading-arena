"use strict";
const test=require("node:test"),assert=require("node:assert/strict");
const {project,encounter,pnl,percent,plural,anchors,durationLabel,eventLabel,phaseLabel,stepLabel,evidenceLabel,explain,battleRows,GUIDE}=require("../web/arena-coliseum.js");
const bot=(id,position=0,result=0)=>({id,name:id,position_btc:position,equity_usdt:100+result,pnl_usdt:result,status:"WAITING"});
const run=(bots,extras={})=>({id:"test",status:"RUNNING",budget_usdt:100,bots,events:[],...extras});

test("failed start is not presented as a completed five-hour battle",()=>{
  const ui=require("../web/arena-coliseum.js"), failed=run([bot("a")],{status:"DONE",incomplete:true,error:"Перевір годинник Windows."});
  assert.equal(ui.runLabel(failed),"Зупинено з помилкою");
  assert.equal(ui.failureNotice(failed),"Перевір годинник Windows.");
  assert.equal(ui.runLabel({...failed,incomplete:false,error:null}),"Раунд завершено");
  assert.equal(ui.runLabel({...failed,status:"PAUSED"}),"Потрібне відновлення або звірка");
});

test("an entry intent never moves an unfilled position inside",()=>{
  const s=project(run([bot("a")],{events:[{seq:1,bot_id:"a",code:"exchange_order_intent",step:3,data:{intent:"entry"}}]}));
  assert.equal(s.actors[0].open,false);assert.equal(s.actors[0].zone,"outside");assert.equal(s.inside,0);
});
test("any confirmed nonzero fill, including a tiny partial fill, enters",()=>{
  const s=project(run([bot("a",1e-12),bot("b",-.0001)]));
  assert.equal(s.inside,2);assert.ok(s.actors.every(a=>a.zone==="inside"));
});
test("partial close remains inside until confirmed flat",()=>{
  const a={...bot("a",.00005,-.004),stop_requested:true};
  let s=project(run([a]));assert.equal(s.actors[0].closing,true);assert.equal(s.actors[0].zone,"inside");
  s=project(run([{...a,position_btc:0}]));assert.equal(s.actors[0].zone,"outside");assert.equal(s.actors[0].retired,true);
});
test("manual review retains a known open position but freezes movement",()=>{
  const a={...bot("a",.001),_manual_review:true};const s=project(run([a]));
  assert.equal(s.actors[0].zone,"inside");assert.equal(s.actors[0].frozen,true);assert.equal(encounter(s,0),null);
});
test("a paused run never labels an unreconciled position as actively managed",()=>{
  const s=project(run([bot("a",.001,1)],{status:"PAUSED"}));
  assert.equal(s.actors[0].zone,"inside");assert.equal(s.actors[0].label,"Пауза · позиція ще відкрита");assert.equal(s.actors[0].frozen,true);assert.equal(s.moving,false);
});
test("repeated unknown position frames preserve the last confirmed zone",()=>{
  const zones=new Map([["a","inside"]]);const s=project(run([{...bot("a"),position_btc:null,pnl_usdt:null,equity_usdt:null}]),run([{...bot("a"),position_btc:null}]),{lastKnownZones:zones});
  assert.equal(s.actors[0].zone,"inside");assert.equal(s.actors[0].unknown,true);assert.equal(s.inside,0);assert.equal(s.unknown,1);
});
test("unknown equity and position do not become a zero-capital KO",()=>{
  const s=project(run([{...bot("a"),position_btc:null,equity_usdt:null,pnl_usdt:null}]));
  assert.equal(s.actors[0].ko,false);assert.equal(s.actors[0].zone,"unknown");
});
test("prepared recipes carry no invented balance or position",()=>{
  const s=project(run([{id:"a",prepared:true,position_btc:null,equity_usdt:null,pnl_usdt:null}],{status:"READY"}));
  assert.equal(s.actors[0].label,"Ще не запущено");assert.equal(s.unknown,0);assert.equal(s.waiting,1);assert.equal(s.moving,false);
});
test("retirement is separate from a confirmed exhausted-capital KO",()=>{
  const s=project(run([{...bot("a",0,-.1),status:"DONE"},{...bot("b",0,-100),status:"DONE"}]));
  assert.equal(s.actors[0].retired,true);assert.equal(s.actors[0].ko,false);assert.equal(s.actors[1].ko,true);
});
test("cash never fights even when it outranks losing participants",()=>{
  const s=project(run([bot("a",.001,-1),{...bot("cash",0),strategy_id:"cash"}]));
  assert.equal(encounter(s,0),null);assert.equal(s.actors[1].zone,"outside");
});
test("free-for-all encounters rotate among eligible participants",()=>{
  const s=project(run([bot("a",.001,3),bot("b",-.001,2),bot("c",.001,-1),bot("d",0,9)]));
  const pairs=Array.from({length:6},(_,i)=>encounter(s,i));
  assert.deepEqual(new Set(pairs.map(p=>p.attacker)),new Set(["a","b"]));
  assert.ok(pairs.every(p=>!p.targets.includes("d")&&p.attacker!=="d"));
});
test("closing participants can defend but never attack",()=>{
  const s=project(run([{...bot("a",.001,4),stop_requested:true},bot("b",.001,5)]));
  const e=encounter(s,0);assert.equal(e.attacker,"b");assert.deepEqual(e.targets,["a"]);
});
test("a queued animation frame earlier than a newly received snapshot cannot index a negative beat",()=>{
  const s=project(run([bot("a",.001,4),bot("b",-.001,1)]),run([bot("a",.001,3),bot("b",-.001,1)]));
  for(const tick of [-1,-.1,0,.5,NaN,Infinity])assert.equal(encounter(s,tick).attacker,"a");
});
test("offline, stale and replay pause independently suppress animation",()=>{
  for(const opts of [{offline:true},{stale:true},{still:true}]){
    const s=project(run([bot("a",.001,4),bot("b",-.001,1)]),null,opts);assert.equal(s.moving,false);assert.equal(encounter(s,0),null);
  }
});
test("replay drops future intents and current lifecycle diagnostics",()=>{
  const data=run([bot("a",0)],{events:[{seq:1,at:"2026-10-01T10:00:00Z",bot_id:"a",code:"entry_checks"},{seq:2,at:"2026-10-01T11:00:00Z",bot_id:"a",code:"exchange_order_intent",step:6}],
    lifecycle:{bots:[{bot_id:"a",current_phase:{code:"closing",step:6,label:"Future"}}]}});
  const s=project(data,null,{replay:true,eventSeq:1,at:"2026-10-01T10:00:00Z"});
  assert.equal(s.run.events.length,1);assert.equal(s.run.lifecycle,null);assert.notEqual(s.actors[0].step,6);
  assert.equal(data.events.length,2);assert.ok(data.lifecycle);
});
test("micro PnL preserves its sign rather than becoming zero",()=>{
  assert.equal(pnl(-.000004,true),"−<0,001");assert.equal(pnl(.000004,true),"+<0,001");
  assert.equal(pnl(-1e-10),"−<0,00000001");assert.equal(pnl(null),"—");assert.equal(pnl(0),"0");
});
test("2/6/12/24 display layouts are finite and inside their intended zones",()=>{
  for(const size of [2,6,12,24]){
    const ids=Array.from({length:size},(_,i)=>String(i)),layout=anchors(ids);
    for(const id of ids){const a=layout.inside.get(id),b=layout.outside.get(id);assert.ok(a.x>=19&&a.x<=81&&a.y>=22&&a.y<=53);assert.ok(b.x>=8&&b.x<=84&&b.y>=79&&b.y<98);}
  }
});
test("later fills, closes, reentries and ranking order cannot steal occupied manifest anchors",()=>{
  const ids=["a","b","c","d","e","f"],manifest={participants:ids.map(bot_id=>({bot_id}))};
  const original=project(run(ids.map((id,i)=>bot(id,i===0?.001:0,i)),{manifest}));
  const stable=new Map(original.actors.map(a=>[a.id,a.anchor]));
  for(let frame=0;frame<12;frame++){
    const bots=ids.map((id,i)=>bot(id,(i+frame)%3?.001:0,12-i-frame)).reverse();
    const s=project(run(bots,{manifest}));
    for(const actor of s.actors)assert.deepEqual(actor.anchor,stable.get(actor.id));
    const points=s.actors.map(a=>JSON.stringify(a.anchor.inside));assert.equal(new Set(points).size,6);
  }
});
test("spectator labels turn codes, phases, steps and durations into plain words",()=>{
  assert.equal(durationLabel(300),"5 хв");assert.equal(durationLabel(900),"15 хв");assert.equal(durationLabel(3600),"1 год");assert.equal(durationLabel(5400),"1 год 30 хв");assert.equal(durationLabel(86400),"24 год");
  assert.equal(durationLabel(null),"");assert.equal(durationLabel(0),"");assert.equal(durationLabel("abc"),"");
  assert.equal(eventLabel({code:"entry_checks"}),"Перевірено умови входу");
  assert.equal(eventLabel({code:"entry_checks",message:"Перевірено вхідні умови."}),"Перевірено вхідні умови.");
  assert.equal(eventLabel({code:"some_new_code"}),"some new code");assert.equal(eventLabel({}),"Подія");assert.equal(eventLabel(null),"Подія");
  assert.equal(phaseLabel("DRAINING"),"Нових входів немає · завершуємо позиції");assert.equal(phaseLabel("WEIRD"),"WEIRD");assert.equal(phaseLabel(null),"—");
  assert.equal(stepLabel(4),"відкрита позиція");assert.equal(stepLabel("6"),"закриття");assert.equal(stepLabel(null),"");assert.equal(stepLabel(9),"9");
  assert.equal(evidenceLabel("SYNTHETIC"),"вигадані дані (тест)");assert.equal(evidenceLabel("ACTUAL"),"реальні записані дані");assert.equal(evidenceLabel(undefined),"—");
});
test("percent of allocation and glossary texts support honest spectator labels",()=>{
  assert.equal(percent(3.25),"+3,25 %");assert.equal(percent(-.004),"−<0,01 %");assert.equal(percent(0),"0 %");assert.equal(percent(null),"—");assert.equal(percent(-12.5),"−12,5 %");
  assert.ok(GUIDE.length>=5&&GUIDE.every(line=>typeof line==="string"&&line.length>20));
  assert.ok(GUIDE.some(line=>line.includes("RULES_ONLY")&&line.includes("shared_jev")),"the Jev line must describe both execution modes");
  for(const key of ["inside","waiting","unknown","RUNNING","DRAINING","INCOMPLETE","funding","jev","source"])assert.ok(explain(key).length>10,key);
  assert.equal(explain("nonsense"),"");
  const s=project(run([bot("a",.001,3.25),{...bot("cash",0),strategy_id:"cash"}]));
  assert.equal(s.actors[0].allocation,100);assert.ok(Math.abs(s.actors[0].pct-3.25)<1e-9);assert.equal(s.actors[1].pct,0);
  const u=project(run([{...bot("a"),pnl_usdt:null}]));assert.equal(u.actors[0].pct,null);
  const n=project({id:"t",status:"RUNNING",bots:[bot("a",.001,1)],events:[]});assert.equal(n.actors[0].allocation,null);assert.equal(n.actors[0].pct,null);
});
test("battle summary rows follow the shared facts model: places, exclusions, Cash and plain outcomes",()=>{
  const bf={match:{id:"m",mode:"OKX_LIVE",status:"DONE",incomplete:false,duration_seconds:18000},
    participants:[{id:"a",name:"Astra",outcome:"traded",trades:3,net_usdt:"-0.0376588749622653",costs:{}},
      {id:"b",name:"Sol",outcome:"no_trade",trades:0,net_usdt:"0",whyNoTrades:{code:"no_signal",text:"Умови входу жодного разу не виконались."}},
      {id:"c",name:"Opus",outcome:"incomplete",trades:1,net_usdt:"-0.5"},{id:"d",name:"Sonnet",outcome:"unknown",trades:null,net_usdt:null}],
    cash:{net_usdt:"0"},ranking:["b","cash","a"],excluded:[{id:"c",reason:"incomplete"},{id:"d",reason:"unknown"}],top:{ids:["b","cash"],withCash:true,tie:true}};
  const rows=battleRows(bf,{allocation:100,strategies:[{id:"s-a",name:"Astra",author:{name:"GPT-6 Astra",model:"gpt-6-astra"}}],bots:[{id:"a",strategy_id:"s-a"}]});
  // Competition ranking since the audience UI (1b2dddd): equal nets share one place, the next place skips (1, 1, 3), as the league policy says a tie shares first place.
  assert.deepEqual(rows.map(r=>[r.id,r.place]),[["b",1],["cash",1],["a",3],["c",null],["d",null]]);
  assert.equal(rows[0].top,true);assert.equal(rows[0].why,"Умови входу жодного разу не виконались.");assert.equal(rows[0].outcomeText,"без угод");
  assert.equal(rows[1].outcomeText,"не торгує");assert.equal(rows[1].netText,"0");
  assert.equal(rows[2].author,"GPT-6 Astra");assert.equal(rows[2].outcomeText,"3 угоди");assert.equal(rows[2].netText,"−0,03765887");assert.equal(rows[2].pct,"−0,04 %");
  assert.equal(rows[3].excludedReason,"incomplete");assert.equal(rows[3].netText,"−0,5");assert.equal(rows[3].outcomeText,"неповний облік");
  assert.equal(rows[4].netText,"—");assert.equal(rows[4].pct,"");assert.equal(rows[4].outcomeText,"невідомо");
  assert.deepEqual(battleRows(null),[]);assert.deepEqual(battleRows({participants:"x"}),[]);
  assert.equal(plural(1,"угода","угоди","угод"),"угода");assert.equal(plural(3,"угода","угоди","угод"),"угоди");assert.equal(plural(11,"угода","угоди","угод"),"угод");assert.equal(plural(22,"угода","угоди","угод"),"угоди");
});
test("the projection does not mutate run, events, diagnostics or balances",()=>{
  const data=run([bot("a",.001,1),bot("b",-.001,-1)]),before=JSON.stringify(data);
  for(let i=0;i<20;i++)encounter(project(data),i);
  assert.equal(JSON.stringify(data),before);
});

test("a participant is labelled by its author's name, the model only when the name matches no author",()=>{
  const bf={match:{id:"m",mode:"OKX_LIVE",status:"RUNNING"},participants:[{id:"h",name:"H10",outcome:"no_trade",trades:0,net_usdt:"0"},
    {id:"o",name:"Opus V1",outcome:"no_trade",trades:0,net_usdt:"0"}],ranking:["h","o"]};
  const rows=battleRows(bf,{allocation:25,strategies:[{id:"s-h",name:"H10",author:{name:"Sonnet",kind:"model",model:"claude-opus-5-5"}},
    {id:"s-o",name:"Opus V1",author:{name:"",kind:"model",model:"claude-opus-5-5"}}],bots:[{id:"h",strategy_id:"s-h"},{id:"o",strategy_id:"s-o"}]});
  assert.deepEqual(rows.map(r=>[r.id,r.shortName,r.identity]),[["h","Sonnet","sonnet"],["o","Opus","opus"]]);
});
