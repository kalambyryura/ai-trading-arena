// Long-battle states (B-13): pure timeline and badges. No browser, requests, keys or financial actions.
const test=require("node:test"),assert=require("node:assert/strict");
const T=require("../web/battle-timeline.js"),Facts=require("../web/battle-facts.js");
const start=Date.parse("2026-10-03T00:00:00Z"),at=minutes=>new Date(start+minutes*60000).toISOString();
const frames=minutes=>minutes.map(m=>({at:at(m),market:{mid:80000}}));

test("exchange timeline uses OKX time and a stopped process never falls back to the browser date",()=>{
  const run={status:"RUNNING",clock_policy:"okx_utc_monotonic_v1",started_at:at(0),
    frames:frames([0,1]),events:[{at:at(2)}],timing:{server_now_ms:Date.parse(at(3))}};
  for(const now of [0,9999999999999]) assert.equal(T.timeline(run,{now}).end,Date.parse(at(3)));
  run.timing.server_now_ms=null;run.status="PAUSED";
  assert.equal(T.timeline(run,{now:9999999999999}).end,Date.parse(at(2)));
  run.status="DONE";run.ended_at=at(4);
  assert.equal(T.timeline(run,{now:9999999999999}).end,Date.parse(at(4)));
});

test("a finished day reads its gaps, minutes without frames, pauses and recovery in words and on one scale",()=>{
  const run={status:"DONE",started_at:at(0),finished_at:at(1440),admission_closed_at:at(1440),
    frames:frames([...Array.from({length:300},(_,i)=>i*2),620,622,...Array.from({length:400},(_,i)=>624+i*2)]),
    events:[{bot_id:null,code:"data_gap",at:at(100)},{bot_id:null,code:"paused",at:at(700)},{bot_id:null,code:"recovery_started",at:at(730)},
      {bot_id:null,code:"market_unavailable",at:at(900)},{bot_id:"x",code:"paused",at:at(5)},{bot_id:null,code:"admission_closed",at:at(1440)}]};
  const model=T.timeline(run);
  assert.equal(model.known,true);assert.equal(model.live,false);
  assert.deepEqual(model.segments.map(s=>[s.kind,(s.from-start)/60000,(s.to-start)/60000]),[["stale",598,620],["stale",1422,1440],["paused",700,730]]);
  assert.deepEqual(model.marks.map(m=>m.kind),["data_gap","recovery_started","market_unavailable"]);
  assert.equal(model.position(Date.parse(at(720))),0.5);assert.equal(model.position(start-1),0);
  assert.equal(model.summary,"Записано 24 год: 1 пропуск даних понад 90 с; 2 відрізки без кадрів довше 10 хв (разом 40 хв); 1 збій ринкових даних або годинника; 1 пауза, відновлень: 1.");
});
test("a clean record says so; a live battle keeps open segments and never invents an end",()=>{
  const clean=T.timeline({status:"DONE",started_at:at(0),finished_at:at(60),frames:frames(Array.from({length:31},(_,i)=>i*2)),events:[]});
  assert.equal(clean.summary,"Записано 1 год: без пропусків даних, пауз і відновлень.");
  const live=T.timeline({status:"RUNNING",started_at:at(0),frames:frames([0,2,4,6,8,10]),events:[{bot_id:null,code:"paused",at:at(20)}]},{now:Date.parse(at(30))});
  assert.equal(live.live,true);assert.equal(live.end,Date.parse(at(30)));
  assert.deepEqual(live.segments.map(s=>[s.kind,s.open]),[["stale",true],["paused",true]]);
  assert.match(live.summary,/^Триває 30 хв: 1 відрізок без кадрів довше 10 хв \(разом 20 хв\); 1 пауза, без відновлення\.$/);
  assert.equal(T.timeline({status:"RUNNING",frames:[]}).known,false);
});
test("badges: zero signals, signals without trades, traded to zero, unknown and a shared first place with Cash",()=>{
  const base={id:"a",final:true,vsCash:"equal",unknown:[]};
  assert.deepEqual(T.badges({...base,outcome:"no_trade",funnel:{signals:0}}).map(b=>[b.key,b.text]),[["waited","чекав весь бій · 0 сигналів"]]);
  assert.equal(T.badges({...base,final:false,outcome:"no_trade",funnel:{signals:0}})[0].text,"поки 0 сигналів");
  assert.equal(T.badges({...base,outcome:"no_trade",funnel:{signals:4},whyNoTrades:{text:"Jev не дав дозволу"}})[0].title,"Jev не дав дозволу");
  assert.equal(T.badges({...base,outcome:"traded",funnel:{signals:9}})[0].text,"торгував · net 0");
  assert.deepEqual(T.badges({...base,outcome:"traded",vsCash:"above",funnel:{signals:9}}),[]);
  assert.equal(T.badges({...base,outcome:"unknown",funnel:null,unknown:["воронка рішень"]})[0].title,"воронка рішень");
  assert.equal(T.badges({...base,outcome:"incomplete",funnel:null})[0].tone,"warn");
  const battle={match:{final:true},top:{ids:["a","cash"],withCash:true,tie:true}};
  assert.deepEqual(T.badges({...base,outcome:"no_trade",funnel:{signals:0}},{battle}).map(b=>b.key),["shared_top","waited"]);
  assert.deepEqual(T.badges({...base,id:"b",outcome:"no_trade",funnel:{signals:0}},{battle}).map(b=>b.key),["waited"]);
  assert.deepEqual(T.badges({...base,outcome:"no_trade",funnel:{signals:0}},{battle:{...battle,match:{final:false}}}).map(b=>b.key),["waited"]);
  assert.deepEqual(T.badges(null),[]);
});
test("badges use the shared facts model as is: a clean zero-signal funnel on a finished run waited all battle",()=>{
  const lifecycleBot={bot_id:"a",funnel:{entry:{checks:2880,signals:0,signals_unknown:0,jev_outcomes_unknown:0,jev_decisions:0,jev_allows:0},
    no_signal_unknown:0,order_intents:0,entry_filled_orders:0,round_trips:0},costs:{net_usdt:"0",complete:true,jev:{known_cost_usd:"0",complete:true,unknown_billing_requests:0}}};
  const facts=Facts.factsForBot({bot:{id:"a",name:"A",pnl_usdt:0,entries:0,trades:0},lifecycleBot,events:[],run:{status:"DONE",incomplete:false,history_complete:true}});
  assert.equal(facts.outcome,"no_trade");
  assert.deepEqual(T.badges(facts).map(b=>b.text),["чекав весь бій · 0 сигналів"]);
  const unknown=Facts.factsForBot({bot:{id:"a",name:"A"},lifecycleBot:null,events:[],run:{status:"DONE"}});
  assert.deepEqual(T.badges(unknown).map(b=>b.key),["unknown"],"no funnel is never shown as zero signals");
});
