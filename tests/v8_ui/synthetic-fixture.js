"use strict";
/* FICTIONAL fixtures only. Names, observations, prices and results are not actual account evidence. */
((root,factory)=>{
  const value=factory();
  if(typeof module==="object"&&module.exports)module.exports=value;else root.ArenaUXFixture=value;
})(typeof window==="undefined"?{}:window,()=>{
  const start=1790931600000, cutoff=start+86400000, end=cutoff+3600000;
  const hash="a".repeat(64);
  const participants=[
    {id:"fixture-a",name:"Учасник A · вигаданий",strategy_id:"fixture-trend",version:"v1",avatar_kind:"human",slot:0},
    {id:"fixture-b",name:"Учасник B · вигаданий",strategy_id:"fixture-wait",version:"v1",avatar_kind:"model",slot:1},
    {id:"cash",name:"Cash",strategy_id:"cash",version:"baseline",avatar_kind:"baseline",slot:2}
  ];
  const events=[];
  const append=(offset,type,code,data={})=>events.push({seq:events.length+1,available_at_ms:start+offset,type,code,...data});
  for(const p of participants)append(0,"ACCOUNTING","initial_accounting",{bot_id:p.id,financial:{
    position_btc:"0",equity_usdt:"100",pnl_usdt:"0",gross_usdt:"0",signed_fees_usdt:"0",
    signed_funding_usdt:"0",known_jev_usd:"0",trades:0}});
  append(30000,"CHECK","formal_signal",{bot_id:"fixture-a",step:1,message:"Умови пройдено на завершеній свічці"});
  append(31000,"DECISION","jev_proceed",{bot_id:"fixture-a",step:2,message:"Proceed · gate дозволено; fill ще немає"});
  append(32000,"INTENT","entry_intent",{bot_id:"fixture-a",step:3,message:"Намір купівлі; позиція ще нульова"});
  append(33000,"FILL","entry_filled",{bot_id:"fixture-a",step:3,message:"Фактичний fill · персонаж на рингу",financial:{
    position_btc:"0.0002",equity_usdt:"99.9995",pnl_usdt:"-0.0005",signed_fees_usdt:"-0.0005"}});
  append(60000,"POSITION","position_unknown",{bot_id:"fixture-a",step:4,message:"Обсяг тимчасово невідомий",financial:{position_btc:null}});
  append(61000,"QUALITY","account_gap",{quality:{coverage:"GAP",billing:"KNOWN",reason:"Вигадана недоступність account reader"}});
  append(80000,"POSITION","position_reconciled",{bot_id:"fixture-a",step:4,message:"Обсяг відновлено",financial:{
    position_btc:"0.0002",equity_usdt:"100.005",pnl_usdt:"0.005"}});
  append(86400000,"ADMISSION_CLOSED","admission_closed",{phase:"DRAINING",message:"Лише нові входи та add завершено; позиція триває"});
  append(86460000,"INTENT","strategy_close",{bot_id:"fixture-a",step:6,message:"Close за умовою стратегії після admission"});
  append(86461000,"FILL","partial_close",{bot_id:"fixture-a",step:7,message:"Partial close · залишок усе ще на рингу",financial:{
    position_btc:"0.0001",equity_usdt:"100.004",pnl_usdt:"0.004",signed_fees_usdt:"-0.0008"}});
  append(86500000,"FILL","flat_filled",{bot_id:"fixture-a",step:7,message:"Actual flat · settlement ще триває",financial:{
    position_btc:"0",equity_usdt:"99.9986",pnl_usdt:"-0.0014",gross_usdt:"0.001",signed_fees_usdt:"-0.0015",
    signed_funding_usdt:"-0.0009",trades:1}});
  append(86600000,"ACCOUNTING","late_cost",{bot_id:"fixture-a",step:8,message:"Пізно відомий рахунок Jev, не додається до минулого кадру",financial:{known_jev_usd:"0.00002"}});
  append(86700000,"SETTLEMENT","settled_a",{bot_id:"fixture-a",step:9,settled:true,message:"Cash/bills звірено · data gap збережено"});
  append(86800000,"SETTLEMENT","settled_b",{bot_id:"fixture-b",step:9,settled:true});
  append(86800000,"SETTLEMENT","settled_cash",{bot_id:"cash",settled:true});
  append(90000000,"PHASE","recording_complete",{phase:"DONE"});
  const replay={schema:"arena-display-replay/v1",evidence_kind:"SYNTHETIC",package_hash:hash,manifest:{
    id:"synthetic-v8-proof",mode:"PAPER",started_at_ms:start,admission_end_at_ms:cutoff,recording_end_at_ms:end,
    time_policy:"admission_then_strategy_exit",participants,renderer_version:"coliseum-v7",assets_version:"coliseum-v7",
    visual_seed:"fictional-v8-1"},events};
  const costsA={gross_pnl_usdt:"0.001",signed_fees_usdt:"-0.0015",signed_funding_usdt:"-0.0009",
    net_usdt:"-0.0014",cash_delta_usdt:"-0.0014",complete:true,jev:{
      known_cost_usd:"0.00002",requests:1,unknown_billing_requests:0,complete:true,source:"equal-share estimate"}};
  const stages=[
    {id:"checks",label:"Перевірки правил",count:4,unit:"перевірки",reference:4},
    {id:"bars",label:"Окремі свічки з сигналом",count:1,unit:"свічки",reference:4},
    {id:"episodes",label:"Епізоди сигналу",count:1,unit:"епізоди · виведено",reference:4},
    {id:"jev",label:"Запити Jev",count:1,unit:"запити",reference:5},
    {id:"gate",label:"Дозволений gate",count:1,unit:"дозволи",reference:5},
    {id:"intents",label:"Наміри входу",count:1,unit:"наміри",reference:6},
    {id:"fills",label:"Заповнені входи",count:1,unit:"ордери",reference:7},
    {id:"cycles",label:"Завершені цикли",count:1,unit:"цикли за весь раунд",reference:16}
  ];
  const review={schema_version:1,projection_version:"private-author/v1",match:{id:replay.manifest.id,mode:"PAPER",
    evidence_kind:"SYNTHETIC",status:"DONE",scope:"FINAL",time_policy:replay.manifest.time_policy,
    manifest_sha256:hash,source_sha256:hash},integrity:{bundle_sha256:hash},
    author_bundle:{state:"RELEASED",integrity:"PASS",reason:"Лише synthetic UI proof"},
    participants:[{id:"fixture-a",name:participants[0].name,recipe_sha256:hash,current_entries:0,seasonal_round_trips:1,
      funnel:{stages,round_trips:1},explain:[{label:"Умова входу",actual:"25 bps",expected:">20 bps",state:"PASS",reference:4}],
      costs:costsA,trade_ledger:{rows:[
        {seq:7,role:"entry",quantity:"0.0002",position_before:"0",position_after:"0.0002",
          gross_pnl_usdt:"0",signed_fees_usdt:"-0.0005",source:"paper_incremental_btc"},
        {seq:13,role:"close",quantity:"0.0001",position_before:"0.0002",position_after:"0.0001",
          gross_pnl_usdt:"0.0006",signed_fees_usdt:"-0.0003",source:"paper_incremental_btc"},
        {seq:14,role:"close",quantity:"0.0001",position_before:"0.0001",position_after:"0",
          gross_pnl_usdt:"0.0004",signed_fees_usdt:"-0.0007",source:"paper_incremental_btc"},
        {seq:14,role:"funding",signed_funding_usdt:"-0.0009",source:"fixture_bill"}
      ],round_trips:[{number:1,entry_seq:7,close_seq:14,closed:true}],
      inventory_known:true,remaining:"0",limitations:["Рядки є recorded delta; фінальні bills/cash показані окремо."]},
      quality:{settlement:"SETTLED",competitive_validity:"INELIGIBLE",reason:"Грошова звірка завершена; gap залишається",
        timing:[{label:"Intent → observed fill",value:"1000 ms",source_kind:"EXACT",reference:7},
          {label:"Час відповіді старої моделі",value:null,source_kind:"NOT_RECORDED"}]},
      coverage:{status:"INCOMPLETE",gaps:[{kind:"Account reader unavailable",duration_ms:20000,effect:"Позиція UNKNOWN; нових ордерів немає"}]}},
      {id:"fixture-b",name:participants[1].name,recipe_sha256:hash,current_entries:0,seasonal_round_trips:0,
        funnel:{stages:stages.map(s=>({...s,count:s.id==="checks"?4:0,reference:null})),round_trips:0},
        costs:{gross_pnl_usdt:"0",signed_fees_usdt:"0",signed_funding_usdt:"0",net_usdt:"0",cash_delta_usdt:"0",complete:true,
          jev:{known_cost_usd:"0",requests:0,unknown_billing_requests:0,complete:true,source:"no requests"}},
        trade_ledger:[],quality:{settlement:"SETTLED",competitive_validity:"ELIGIBLE",timing:[]},coverage:{status:"COMPLETE"}}]};
  const workshop={version_name:"Fictional candidate v2",evidence_kind:"SYNTHETIC",
    lint:[{path:"/entry/setup/lookback",message:"Дані для lookback відсутні",hint:"Вибрати підтримане поле або спільний новий dataset"}],
    explain:[{path:"entry.setup",actual:"25 bps",expected:">20 bps",state:"PASS"},
      {path:"entry.confirm",actual:null,expected:"next closed bar",state:"UNKNOWN"}],
    lots:{target_notional_usdt:"7.5",lot_qty:"0.0001",min_qty:"0.0001",requested_qty:"0.000075",rounded_qty:"0",
      rounded_notional_usdt:"0",result:"UNEXECUTABLE",source_at:"Фіктивний snapshot",policy_hash:hash,
      reasons:["Ціль менша за один виконуваний лот у цьому fixture."]},
    trials:[{id:"fixture-t1",attempt_number:1,version_name:"v1",dataset:"synthetic A",status:"FAILED",evidence_kind:"SYNTHETIC",net_usdt:null,trades:null},
      {id:"fixture-t2",attempt_number:2,version_name:"v2",dataset:"synthetic A",status:"COMPLETE",evidence_kind:"SYNTHETIC",net_usdt:"-0.0014",trades:1}],
    compare:{comparable:true,reason:"Однаковий fixture/data/policy; лише recipe різний",rows:[
      {path:"max_hold_seconds",before:"900",after:"null · часової межі рецепта немає"},
      {path:"entry.confirm",before:"same bar",after:"next closed bar"}]}};
  const league={battles:[{id:"fixture-finished",label:"Вигаданий бій 1",mode:"PAPER",status:"DONE",settlement:"SETTLED",
      competitive_validity:"INELIGIBLE",allocation_usdt:"100",cohort:"fixture-cohort-v1"},
    {id:"fixture-open-tail",label:"Вигаданий бій 2",mode:"OKX_DEMO",status:"DRAINING",settlement:"PENDING",
      competitive_validity:"PENDING",allocation_usdt:"100",cohort:"fixture-cohort-v2"}],
    slots:[{id:"fixture-slot-3",status:"SKIPPED"}],
    policy_note:"Fixture: 24h admission; optional max hold=null, вихід лише за стратегією/ризиком/власником. Однакове upgrade-вікно, наступний lock. Жодної цілі 1000."};
  return {replay,review,workshop,league};
});
