"use strict";
const test=require("node:test"),assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path");
const Workshop=require("../../web/workshop-tools-ui.js"),League=require("../../web/league.js");
test("Workshop input preserves complete V2/partial recipe and optional supplied context",()=>{
  const strategy={strategy_schema_version:2,rules:{side:"both",max_hold_seconds:null,reduce_pct:25,reduce_when:{feature:"net_gain_bps",op:"gt",value:15}}};
  const body=Workshop.payload({source:"editor",recipe:JSON.stringify(strategy),features:'{"return_1m_bps":25,"entries":1}',snapshot:'{"ask":100001}',instrument:'{"lot_step":0.01}',budget:"100",equity:"101",leftTrial:"left",rightTrial:"right"});
  assert.deepEqual(body.strategy,strategy);assert.equal(body.strategy.rules.max_hold_seconds,null);
  assert.deepEqual(body.features,{return_1m_bps:25,entries:1});assert.equal(body.budget_usdt,100);assert.equal(body.right_trial_id,"right");
});
test("blank optional inputs are absent rather than manufactured quotes/features/equity",()=>{
  assert.deepEqual(Workshop.payload({source:"version",versionId:"v1"}),{version_id:"v1"});
  assert.deepEqual(Workshop.payload({source:"editor",recipe:'{"rules":{}}'}),{strategy:{rules:{}}});
});
test("malformed context, missing recipe or one unpaired trial is rejected locally",()=>{
  for(const extra of [{features:"[]"},{snapshot:"bad json"},{equity:"NaN"},{budget:"0"},{leftTrial:"a"},{leftTrial:"../a",rightTrial:"b"}])assert.throws(()=>Workshop.payload({source:"editor",recipe:"{}",...extra}));
  assert.throws(()=>Workshop.payload({source:"version",versionId:"../keys"}));assert.throws(()=>Workshop.payload({recipe:""}));
});
test("Workshop missing token fails before request; all valid diagnostics use a fixed local POST",async()=>{
  const previous=global.fetch,calls=[];global.fetch=async(path,options)=>{calls.push({path,options});return{ok:true,json:async()=>({lint:[],explain:[],lots:{},trials:[],compare:{}})};};
  try{
    await assert.rejects(()=>Workshop.request({strategy:{}},""));assert.equal(calls.length,0);
    await Workshop.request({version_id:"fixture"},"fixture-token");assert.equal(calls[0].path,"/api/workshop/tools");
    assert.equal(calls[0].options.method,"POST");assert.equal(calls[0].options.credentials,"same-origin");assert.equal(calls[0].options.redirect,"error");
    assert.deepEqual(JSON.parse(calls[0].options.body),{version_id:"fixture"});
  }finally{global.fetch=previous;}
});
test("League links retain paper/exchange identity and select the proper Review/Replay route",()=>{
  assert.equal(League.reviewHref({id:"fixture",kind:"exchange",mode:"OKX_DEMO"}),"/review?mode=exchange&id=fixture");
  assert.equal(League.reviewHref({id:"fixture",mode:"PAPER"},true),"/review?mode=paper&id=fixture&view=replay");
  assert.throws(()=>League.reviewHref({id:"../keys",mode:"PAPER"}));assert.throws(()=>League.reviewHref({id:"x",mode:"UNKNOWN"}));
});
test("League reads one local GET and refuses a non-history response",async()=>{
  const previous=global.fetch,calls=[];global.fetch=async(path,options)=>{calls.push({path,options});return{ok:true,json:async()=>({battles:[],target:1000})};};
  try{await League.read();assert.equal(calls[0].path,"/api/league");assert.equal(calls[0].options.method,"GET");
    global.fetch=async()=>({ok:true,json:async()=>({})});await assert.rejects(()=>League.read());
  }finally{global.fetch=previous;}
});
test("League and diagnostics files contain no financial route, owner token output or auto-start",()=>{
  for(const file of ["league.js","workshop-tools-ui.js","league.html"]){
    const source=fs.readFileSync(path.join(__dirname,"../../web",file),"utf8");
    assert.equal(/\/api\/(?:play|exchange\/(?:start|stop|resume)|arena\/(?:start|stop|resume))|console\.log\s*\(/.test(source),false,file);
  }
  const source=fs.readFileSync(path.join(__dirname,"../../web/league.js"),"utf8");assert.equal(/method\s*:\s*["']POST/.test(source),false);
});
