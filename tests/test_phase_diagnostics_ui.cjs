"use strict";
const test=require("node:test"),assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path"),vm=require("node:vm");
const scope=path.resolve(__dirname,"..");let root=scope;
while(!fs.existsSync(path.join(root,"trading_lab")) && path.dirname(root)!==root)root=path.dirname(root);
const model=require(path.join(root,"web/review-model.js"));
class Element{
  constructor(tag){this.tagName=tag;this.children=[];this.textContent="";this.dataset={};this.attributes={};this.listeners={};this.classList={add(){}};}
  append(...values){this.children.push(...values);}
  replaceChildren(...values){this.children=values;this.textContent="";}
  setAttribute(k,v){this.attributes[k]=v;}
  addEventListener(k,v){this.listeners[k]=v;}
  focus(){}
}
function text(n){return n instanceof Element?String(n.textContent)+n.children.map(text).join(" "):String(n);}
function setup(){
  const source=fs.readFileSync(path.join(scope,"web/research-panels.js"),"utf8"),window={ArenaReviewModel:model};
  vm.runInNewContext(source,{window,document:{createElement:t=>new Element(t)},Node:Element,console});
  return window.ArenaResearchPanels;
}
function bundle(){return {schema_version:1,match:{id:"fake",mode:"PAPER"},participants:[{id:"a",name:"Author",costs:{net_usdt:"-0.012345678901"},funnel:{failed_features:{r5:5}},trade_ledger:{rows:[],round_trips:[]}}],events:[]};}
test("legacy bundles remain usable and totals are explicitly mixed",()=>{
  const ui=setup(),host=new Element("main"),source=bundle(),before=JSON.stringify(source);
  ui.mountReview(host,source);assert.match(text(host),/Фази не записано/);assert.match(text(host),/Змішаний підсумок старого формату/);
  assert.equal(JSON.stringify(source),before);
});
test("new phase rows and failed predicates render separately",()=>{
  const ui=setup(),host=new Element("main"),source=bundle();
  source.participants[0].phase_diagnostics={phases:{entry:{evaluations:2,unique_closed_bars:1,rechecks:1,failed_predicates:[{feature:"r5",op:"gte",expected:"20",false_evaluations:2,unknown_evaluations:0,false_unique_closed_bars:1}]},hold:{evaluations:0,unique_closed_bars:null,failed_predicates:[]}}};
  ui.mountReview(host,source);assert.match(text(host),/Перевірки за фазами/);assert.match(text(host),/Унікальні завершені свічки/);
  assert.match(text(host),/r5 gte 20/);assert.match(text(host),/Невиконана умова виходу/);
  assert.doesNotMatch(text(host),/Check events|closed bars|Leaf FALSE/);
});
test("quality view shows known endpoints and UNKNOWN inside without outage duration",()=>{
  const ui=setup(),host=new Element("main"),source=bundle();
  source.market_gaps=[{event_seq:8,before:{at:100},recovery:{at:120},known_snapshot_interval_seconds:20,positions_at_gap:[{id:"a",status:"OPEN"}]}];
  const view=ui.mountReview(host,source,{onEvent(){}});view.selectView("quality");
  assert.match(text(host),/перебіг усередині невідомий/);assert.match(text(host),/не встановлює тривалість збою/);assert.match(text(host),/Відкрита позиція/);assert.match(text(host),/1970-01-01T00:02:00.000Z/);
});
test("ledger exposes intent timing, reason and exact money without write controls",()=>{
  const ui=setup(),host=new Element("main"),source=bundle();
  source.participants[0].trade_explanations=[{number:1,entry_intent_to_fill_seconds:6.399,close_intent_to_fill_seconds:6.45,exit_reason:"rules_exit"}];
  const view=ui.mountReview(host,source);view.selectView("ledger");
  assert.match(text(host),/Намір входу → виконання/);assert.match(text(host),/6.399/);assert.match(text(host),/Правила виходу/);
  assert.match(text(host),/0,012345678901/);assert.match(text(host),/витрати PAPER змодельовано/);
});
test("unknown G1 check phase and unreadable captures remain visible without failure",()=>{
  const ui=setup(),host=new Element("main"),source=bundle();
  source.diagnostics_context={market_snapshots:"UNAVAILABLE"};
  source.participants[0].phase_diagnostics={phases:{},unclassified_check_event_seqs:[10]};
  ui.mountReview(host,source);
  assert.match(text(host),/фазу не встановлено/);assert.match(text(host),/Запис ринку недоступний/);
});
