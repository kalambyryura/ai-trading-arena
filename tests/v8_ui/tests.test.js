"use strict";
const test=require("node:test"),assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path");
const Replay=require("../../web/event-replay.js"),Model=require("../../web/review-model.js"),UI=require("../../web/research-panels.js");
const Fixture=require("./synthetic-fixture.js");
const Coliseum=require("../../web/arena-coliseum.js");
const copy=value=>JSON.parse(JSON.stringify(value));
const start=Fixture.replay.manifest.started_at_ms;
const bot=snapshot=>snapshot.state.bots.find(b=>b.id==="fixture-a");
function outcome(id,net,extra={}){return{id,net_usdt:net,allocation_usdt:"100",flat:true,settled:true,
  coverage:"COMPLETE",competitive_validity:"ELIGIBLE",...extra};}
const policy={primary_score:"RECONCILED_NET_USDT",allocation_usdt:"100",tie_tolerance_usdt:"0"};
const battle=outcomes=>({status:"DONE",competitive_validity:"ELIGIBLE",expected_participant_ids:["a","b","cash"],outcomes});

test("synthetic replay validates and never changes its immutable input",()=>{
  const before=JSON.stringify(Fixture.replay);assert.equal(Replay.validate(Fixture.replay),true);
  new Replay.Replay(Fixture.replay).seek(start+61000);
  assert.equal(JSON.stringify(Fixture.replay),before);
});
test("indexed seek equals a linear event prefix at every event",()=>{
  const replay=new Replay.Replay(Fixture.replay,{checkpointEvery:3});let expected=Replay.initial(Fixture.replay);
  for(const event of Fixture.replay.events){
    expected=Replay.reduce(expected,event);
    assert.deepEqual(replay.seekSeq(event.seq).state,expected);
  }
});
test("one timestamp can contain several events; event seek does not expose the next one",()=>{
  const replay=new Replay.Replay(Fixture.replay);
  assert.equal(replay.seekSeq(1).event_count,1);assert.equal(replay.snapshot().state.bots[1].financial.equity_usdt,null);
  assert.equal(replay.seek(start).event_count,3);
});
test("intent is not a fill and available state does not depend on final accounting",()=>{
  const replay=new Replay.Replay(Fixture.replay);
  assert.equal(bot(replay.seekSeq(6)).financial.position_btc,"0");
  assert.equal(bot(replay.seekSeq(7)).financial.position_btc,"0.0002");
  assert.equal(bot(replay.seekSeq(14)).financial.known_jev_usd,"0");
  assert.equal(bot(replay.seekSeq(15)).financial.known_jev_usd,"0.00002");
});
test("UNKNOWN retains its last-known zone when seeking directly",()=>{
  const replay=new Replay.Replay(Fixture.replay);replay.seek(start+60000);const f=replay.frame();
  const scene=Coliseum.project(f.run,f.previous,f.extras),actor=scene.actors.find(a=>a.id==="fixture-a");
  assert.equal(actor.position,null);assert.equal(actor.unknown,true);assert.equal(actor.zone,"inside");assert.equal(actor.ko,false);
});
test("UNKNOWN before any observation stays unknown, never cash or KO",()=>{
  const replay=new Replay.Replay(Fixture.replay);replay.seekSeq(0);const f=replay.frame();
  const actor=Coliseum.project(f.run,f.previous,f.extras).actors.find(a=>a.id==="fixture-a");
  assert.equal(actor.zone,"unknown");assert.equal(actor.ko,false);assert.equal(actor.pnl,null);
});
test("partial close stays inside; flat fill goes outside while settlement is pending",()=>{
  const replay=new Replay.Replay(Fixture.replay);
  replay.seekSeq(13);let frame=replay.frame();
  assert.equal(Coliseum.project(frame.run,frame.previous,frame.extras).actors.find(a=>a.id==="fixture-a").zone,"inside");
  replay.seekSeq(14);frame=replay.frame();
  assert.equal(Coliseum.project(frame.run,frame.previous,frame.extras).actors.find(a=>a.id==="fixture-a").zone,"outside");
  assert.equal(bot(replay.snapshot()).settled,false);
});
test("24h admission event leaves the existing position open",()=>{
  const replay=new Replay.Replay(Fixture.replay),s=replay.seek(Fixture.replay.manifest.admission_end_at_ms);
  assert.equal(s.state.phase,"DRAINING");assert.equal(bot(s).financial.position_btc,"0.0002");assert.equal(bot(s).settled,false);
});
test("replay recording end never synthesizes forced close or DONE",()=>{
  const packet=copy(Fixture.replay);packet.events=packet.events.slice(0,11);
  const replay=new Replay.Replay(packet),s=replay.seek(packet.manifest.recording_end_at_ms);
  assert.equal(s.state.phase,"DRAINING");assert.equal(bot(s).financial.position_btc,"0.0002");
});
test("1x, 10x and 100x change playback time only",()=>{
  const slow=new Replay.Replay(Fixture.replay),medium=new Replay.Replay(Fixture.replay),fast=new Replay.Replay(Fixture.replay);
  slow.play();medium.setSpeed(10);medium.play();fast.setSpeed(100);fast.play();
  for(let i=0;i<100;i++)slow.tick(1000);for(let i=0;i<10;i++)medium.tick(1000);fast.tick(1000);
  assert.deepEqual(slow.snapshot().state,medium.snapshot().state);assert.deepEqual(slow.snapshot().state,fast.snapshot().state);
  assert.throws(()=>fast.setSpeed(2));
});
test("next action skips polls, supports follow and same-time sequence",()=>{
  const replay=new Replay.Replay(Fixture.replay);
  assert.equal(replay.nextAction().state.seq,5);assert.equal(replay.nextAction().state.seq,6);
  assert.equal(replay.nextAction("fixture-b").state.seq,9);assert.equal(replay.nextAction("fixture-b").state.seq,11);
});
test("corrupt sequence, unavailable event and DONE without settlement are rejected",()=>{
  const duplicate=copy(Fixture.replay);duplicate.events[1].seq=1;
  assert.throws(()=>new Replay.Replay(duplicate),/sequence/);
  const future=copy(Fixture.replay);future.events[5].available_at_ms=start-1;
  assert.throws(()=>new Replay.Replay(future),/causal/);
  const done=copy(Fixture.replay);done.events[6].phase="DONE";
  assert.throws(()=>new Replay.Replay(done),/flat\/settled/);
});
test("privacy schema refuses arbitrary nested order/account/prompt fields",()=>{
  for(const field of ["uid","authorization","order_id","prompt","metadata"]){
    const packet=copy(Fixture.replay);packet.events[0][field]="PRIVATE_SENTINEL";
    assert.throws(()=>Replay.validate(packet),/forbidden field/);
  }
  const packet=copy(Fixture.replay);packet.manifest.participants[0].author={prompt:"PRIVATE_SENTINEL"};
  assert.throws(()=>Replay.validate(packet),/forbidden field/);
});
test("public spectator gate requires independent privacy, rights, integrity and owner release",()=>{
  const receipt={projection:"spectator/v1",package_hash:Fixture.replay.package_hash,integrity:"PASS",
    privacy:"PASS",rights:"APPROVED",owner_release:"APPROVED",access:"PUBLIC",release_after_ms:start};
  assert.equal(Replay.spectatorGate(Fixture.replay,receipt,start).allowed,true);
  for(const field of ["privacy","rights","integrity","owner_release","access"]){
    assert.equal(Replay.spectatorGate(Fixture.replay,{...receipt,[field]:"PENDING"},start).allowed,false);
  }
  assert.equal(Replay.spectatorGate(Fixture.replay,{...receipt,package_hash:"b".repeat(64)},start).allowed,false);
  assert.equal(Replay.spectatorGate(Fixture.replay,receipt,start-1).allowed,false);
});
test("standalone spectator checks the exact package digest before the permission predicate",async()=>{
  const packet=copy(Fixture.replay);packet.package_hash=await Replay.packetDigest(packet);
  const receipt={projection:"spectator/v1",package_hash:packet.package_hash,integrity:"PASS",
    privacy:"PASS",rights:"APPROVED",owner_release:"APPROVED",access:"PUBLIC",release_after_ms:start};
  assert.equal((await Replay.openSpectator(packet,receipt,start)).allowed,true);
  packet.events[0].financial.equity_usdt="101";
  assert.equal((await Replay.openSpectator(packet,receipt,start)).allowed,false);
});
test("financial helpers preserve microPnL and signed long funding precision",()=>{
  assert.equal(Model.decimalSum(["0.01534","-0.05177709","-0.0012217849622653"]),"-0.0376588749622653");
  assert.equal(Model.exact("-0.0000000001","USDT",true),"−0,0000000001 USDT");
  assert.equal(Model.decimalSum(["1",null]),null);assert.equal(Model.decimalCompare("0",null),null);
});
test("cash bridge needs recorded totals, actual flat, bills and settlement",()=>{
  const costs={gross_usdt:"0.001",signed_fees_usdt:"-0.0015",signed_funding_usdt:"-0.0009",
    net_usdt:"-0.0014",cash_delta_usdt:"-0.0014",flat:true,settled:true,bills_complete:true};
  assert.equal(Model.bridge(costs,"0").status,"PASS");
  assert.equal(Model.bridge({...costs,cash_delta_usdt:null}).status,"UNKNOWN");
  assert.equal(Model.bridge({...costs,settled:false}).status,"PROVISIONAL");
  assert.equal(Model.bridge({...costs,cash_delta_usdt:"0"}).status,"MISMATCH");
});
test("zero-trade and Cash may tie at the top; number of trades is never a tie-break",()=>{
  const result=Model.scoreBattle(battle([outcome("a","-0.001"),outcome("b","0",{trades:0}),outcome("cash","0")]),policy);
  assert.equal(result.status,"TIE_AT_TOP");assert.equal(result.sole_winner,null);
  assert.deepEqual(result.shared_top,["b","cash"]);assert.equal(result.economic_known_net_usdt,"-0.001");
});
test("missing counterpart cannot win; known loss remains in the economic ledger",()=>{
  const result=Model.scoreBattle(battle([outcome("a","-0.1"),outcome("cash","0")]),policy);
  assert.equal(result.status,"PENDING");assert.equal(result.economic_known_net_usdt,"-0.1");assert.equal(result.economic_complete,false);
  assert.equal(Model.scoreBattle(battle([]),policy).economic_known_net_usdt,null);
});
test("unequal allocation or incomplete coverage blocks score but preserves a loss",()=>{
  const values=[outcome("a","-0.1"),outcome("b","0"),outcome("cash","0")];
  let result=Model.scoreBattle(battle(values.map(o=>o.id==="a"?{...o,coverage:"GAP"}:o)),policy);
  assert.equal(result.status,"INELIGIBLE");assert.equal(result.economic_known_net_usdt,"-0.1");
  result=Model.scoreBattle(battle(values.map(o=>o.id==="b"?{...o,allocation_usdt:"101"}:o)),policy);
  assert.equal(result.status,"INELIGIBLE");
});
test("score remains pending while a tail is open or settlement is unknown",()=>{
  const values=[outcome("a","1",{flat:false}),outcome("b","0"),outcome("cash","0")];
  assert.equal(Model.scoreBattle(battle(values),policy).status,"PENDING");
  const values2=values.map(o=>o.id==="a"?{...o,flat:true,settled:false}:o);
  assert.equal(Model.scoreBattle(battle(values2),policy).status,"PENDING");
});
test("tie tolerance is explicit; sole win and shared top are separate counters",()=>{
  const match=battle([outcome("a","0.000001"),outcome("b","0"),outcome("cash","0")]);
  assert.equal(Model.scoreBattle(match,policy).status,"SOLE_WIN");
  assert.equal(Model.scoreBattle(match,{...policy,tie_tolerance_usdt:"0.000001"}).status,"TIE_AT_TOP");
  assert.equal(Model.scoreBattle(match,{...policy,tie_tolerance_usdt:"-1"}).status,"INELIGIBLE");
});
test("league history has no target, deduplicates resume and counts skipped slots separately",()=>{
  const result=Model.leagueCounters([...Fixture.league.battles,Fixture.league.battles[0]],Fixture.league.slots);
  assert.deepEqual(result,{started:2,settled:1,comparable:0,draining:1,paused:0,incomplete:1,skipped:1});
  assert.equal(Object.hasOwn(result,"target"),false);
});
test("trial comparison refuses a changed data, execution or capital policy",()=>{
  const a={data_hash:"x",code_hash:"a",policy_hash:"p",execution_model_hash:"e",allocation_usdt:"100",evidence_kind:"MODEL"};
  assert.equal(Model.comparableTrials(a,{...a}).comparable,true);
  assert.deepEqual(Model.comparableTrials(a,{...a,data_hash:"y"}).differences,["data_hash"]);
});
test("root bundle adapter preserves ledger object, missing fields and exact costs",()=>{
  const example={schema_version:1,match:{id:"fixture",mode:"OKX_DEMO",code_sha256:"x"},
    participants:[{id:"a",funnel:{entry:{checks:2,signals:1},entry_filled_orders:1,round_trips:1},
      costs:{net_usdt:"-0.007"},trade_ledger:{rows:[{seq:1,quantity:"0.01"}],round_trips:[{number:1,closed:true}]},
      quality:{distinct_closed_bars:null},coverage:{rows:[]}}]};
  const model=UI.adaptBundle(example),bot=model.participants[0];
  assert.equal(model.match.evidence_kind,"ACTUAL");assert.equal(bot.trade_ledger[0].seq,1);
  assert.equal(bot.round_trips[0].closed,true);assert.equal(bot.costs.net_usdt,"-0.007");
  assert.equal(bot.stages.find(s=>s.id==="fills").count,1);
  assert.equal(bot.stages.find(s=>s.id==="formal-bars").count,undefined);
});
test("capital admission uses declared allocation and reserve without wallet reset or resizing",()=>{
  const capital={free_usdt:"110",allocation_usdt:"100",buffer_usdt:"1",funds_verified:true};
  assert.equal(Model.capitalAdmission(capital).eligible,true);
  assert.equal(Model.capitalAdmission({...capital,free_usdt:"99"}).eligible,false);
  assert.equal(Model.capitalAdmission({...capital,free_usdt:null}).eligible,false);
  assert.equal(Model.capitalAdmission({...capital,unsettled_run_id:"old-tail"}).eligible,false);
  assert.equal(Model.capitalAdmission({...capital,buffer_usdt:null}).eligible,false);
});
test("upgrade requires equal bundle/cutoff/epoch, all trials and qualification",()=>{
  const window={opened_at_ms:100,closed_at_ms:200,feedback_released_at_ms:100,training_cutoff_ms:90,next_lock_at_ms:250,
    sdk_hash:"sdk",feature_epoch_hash:"features",execution_policy_hash:"policy",common_bundle_hash:"bundle"};
  const candidate={...window,parent_recipe_hash:"v1",recipe_hash:"v2",submitted_at_ms:150,training_latest_at_ms:90,
    qualified:true,all_trials_logged:true};
  const result=Model.upgradeEligibility(candidate,window,150);
  assert.equal(result.eligible,true);assert.equal(result.effective_at_lock_ms,250);
  assert.equal(Model.upgradeEligibility({...candidate,common_bundle_hash:"early-private-bundle"},window,150).eligible,false);
  assert.equal(Model.upgradeEligibility({...candidate,training_latest_at_ms:91},window,150).eligible,false);
  assert.equal(Model.upgradeEligibility({...candidate,all_trials_logged:false},window,150).eligible,false);
  assert.equal(Model.upgradeEligibility({...candidate,qualified:false},window,150).eligible,false);
});
test("late, early or hot upgrades cannot change the already locked version",()=>{
  const window={opened_at_ms:100,closed_at_ms:200,feedback_released_at_ms:100,training_cutoff_ms:90,next_lock_at_ms:250,
    sdk_hash:"sdk",feature_epoch_hash:"features",execution_policy_hash:"policy",common_bundle_hash:"bundle"};
  const candidate={...window,parent_recipe_hash:"v1",recipe_hash:"v2",submitted_at_ms:150,training_latest_at_ms:90,
    qualified:true,all_trials_logged:true};
  assert.equal(Model.upgradeEligibility(candidate,window,99).eligible,false);
  assert.equal(Model.upgradeEligibility(candidate,window,201).eligible,false);
  assert.equal(Model.upgradeEligibility(candidate,{...window,next_lock_at_ms:180},150).eligible,false);
  assert.equal(Model.upgradeEligibility({...candidate,recipe_hash:"v1"},window,150).eligible,false);
});
test("display helpers contain no execution/network transports",()=>{
  for(const file of ["event-replay.js","review-model.js","research-panels.js","replay-labels.js"]){
    const source=fs.readFileSync(path.join(__dirname,"../../web",file),"utf8");
    assert.equal(/\bfetch\s*\(|XMLHttpRequest|WebSocket|\/api\/exchange\/(start|stop|resume)|\/api\/play/.test(source),false,file);
  }
});
