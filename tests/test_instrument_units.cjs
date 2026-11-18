"use strict";
// Season instrument and quantity units (R2: a season trades BTC-USDT-SWAP or ETH-USDT-SWAP). Display only; no browser, requests or keys.
const test=require("node:test"),assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path"),vm=require("node:vm");
const M=require("../web/review-model.js");
const web=name=>path.join(__dirname,"..","web",name);
const eth={id:"eth-demo",mode:"OKX_DEMO",rules:{instrument:"ETH-USDT-SWAP"}}, btc={id:"btc-live",mode:"OKX_LIVE",rules:{instrument:"BTC-USDT-SWAP"}};
const legacy={id:"old-journal",mode:"OKX_LIVE",rules:{leverage:1}};   // a journal from before R2: no instrument, it traded BTC

test("base currency comes from rules.instrument; a journal without one is BTC",()=>{
  assert.equal(M.baseCurrency(eth),"ETH");assert.equal(M.baseCurrency(btc),"BTC");
  assert.equal(M.baseCurrency(legacy),"BTC");assert.equal(M.baseCurrency({}),"BTC");assert.equal(M.baseCurrency(null),"BTC");assert.equal(M.baseCurrency(undefined),"BTC");
  assert.equal(M.instrument(eth),"ETH-USDT-SWAP");assert.equal(M.instrument(legacy),"BTC-USDT-SWAP");assert.equal(M.instrumentOf(legacy),null);
  assert.equal(M.baseCurrency("ETH-USDT-SWAP"),"ETH","an instrument id works directly");
  assert.equal(M.baseCurrency({instrument:"ETH-USDT-SWAP"}),"ETH","a schema-4 recipe names its instrument at the top level");
  assert.equal(M.baseCurrency({match_manifest:{instrument:"ETH-USDT-SWAP"}}),"ETH");
  assert.equal(M.baseCurrency({rules:{instrument:"ETH-USDT-SWAP"},instrument:"BTC-USDT-SWAP"}),"ETH","rules.instrument wins");
  for(const junk of ["eth-usdt-swap","ETH","",42,{rules:{instrument:{id:"ETH-USDT-SWAP"}}},{instrument:"<b>ETH</b>-USDT"}])assert.equal(M.baseCurrency(junk),"BTC",JSON.stringify(junk));
  assert.equal(M.baseCurrency(legacy,null),null,"an explicit fallback is returned when nothing is recorded");
});
test("market pair labels follow the season",()=>{
  assert.equal(M.pairLabel(eth),"ETH / USDT");assert.equal(M.pairLabel(btc),"BTC / USDT");assert.equal(M.pairLabel(legacy),"BTC / USDT");assert.equal(M.pairLabel(null),"BTC / USDT");
});
test("coins and OKX contracts are separate, each with its own unit; nothing unlabelled",()=>{
  assert.equal(M.quantityText({coins:"0.013",contracts:"0.13"},"ETH"),"0,013 ETH · 0,13 контр.");
  assert.equal(M.quantityText({contracts:"0.13"},"ETH"),"0,13 контр.","only contracts known: contracts only, never guessed coins");
  assert.equal(M.quantityText({coins:"0.013"},"ETH"),"0,013 ETH");
  assert.equal(M.quantityText({coins:-0.013,contracts:null},"ETH"),"−0,013 ETH","a short keeps its sign");
  assert.equal(M.quantityText({coins:"0.00010000"},"BTC"),"0,0001 BTC","decimal strings stay exact");
  assert.equal(M.quantityText({},"ETH"),"—");assert.equal(M.quantityText({coins:"",contracts:"n/a"},"ETH"),"—");assert.equal(M.quantityText(null,"ETH"),"—");
  assert.equal(M.withUnit("0.13",M.CONTRACTS_UNIT),"0,13 контр.");assert.equal(M.withUnit(null,"ETH"),null);assert.equal(M.amount(true),null);
});
test("contract value hint names the instrument: 0.1 ETH vs 0.01 BTC; unknown instruments get none",()=>{
  assert.equal(M.contractNote(eth),"1 контр. ETH-USDT-SWAP = 0,1 ETH");
  assert.equal(M.contractNote(btc),"1 контр. BTC-USDT-SWAP = 0,01 BTC");
  assert.equal(M.contractNote(legacy),"1 контр. BTC-USDT-SWAP = 0,01 BTC");
  assert.equal(M.contractNote({rules:{instrument:"SOL-USDT-SWAP"}}),"1 контр. SOL-USDT-SWAP = 1 SOL");  // R2.1.13
  assert.equal(M.contractNote({rules:{instrument:"XRP-USDT-SWAP"}}),null);
});
test("coin label: seasons default to BTC; review/replay DTOs of an exchange run without an instrument do not guess",()=>{
  assert.equal(M.displayCoin(eth),"ETH");assert.equal(M.displayCoin(legacy),"BTC","a season object without an instrument is a BTC journal");
  assert.equal(M.displayCoin({mode:"PAPER"}),"BTC","PAPER is BTC-only by the server rule");
  assert.equal(M.displayCoin({mode:"OKX_LIVE",bots:[{id:"cash"},{id:"a",base_ccy:"SOL"}]}),"SOL","R2.1.11: the bots' base_ccy first");
  assert.equal(M.displayCoin({mode:"OKX_LIVE",bots:[{id:"a",base_ccy:"<b>"}],rules:{instrument:"ETH-USDT-SWAP"}}),"ETH","a malformed base_ccy is ignored");
  assert.equal(M.baseCurrency({bots:[{id:"a",base_ccy:"SOL"}],rules:{instrument:"ETH-USDT-SWAP"}}),"SOL","R2.1.11: the panel's coin too");
  assert.equal(M.baseCurrency({rules:{instrument:"ETH-USDT-SWAP"}}),"ETH","journals before it: the instrument");
  assert.equal(M.displayCoin({mode:"OKX_DEMO"}),M.UNKNOWN_COIN);assert.equal(M.displayCoin({mode:"OKX_LIVE"}),M.UNKNOWN_COIN);assert.notEqual(M.UNKNOWN_COIN,"BTC");
  assert.equal(M.displayCoin({mode:"OKX_DEMO",instrument:"ETH-USDT-SWAP"}),"ETH","a DTO that names its instrument is shown as such");
  assert.equal(M.displayCoin(null),"BTC");assert.equal(M.displayCoin({}),"BTC");
});

// The owner's ETH Demo case: the fill carries OKX contracts (0.13) and the position after it in coins (−0.013 ETH).
const ethShortFill={seq:9,code:"exchange_entry_fill",bot_id:"a",at:"2026-10-30T12:00:00Z",
  data:{result:{ordId:"1",state:"filled",contracts:"0.13",average_price:"2249.9",signed_fee_usdt:"-0.0146",gross_pnl_usdt:"0"},position_btc:"-0.013"}};
const paperFill={seq:4,code:"paper_entry_fill",bot_id:"b",data:{quantity_btc:0.001,price_usdt:84000.5,fee_usdt:0.042,remaining_btc:0.001}};
const legacyRawFill={seq:2,code:"exchange_close_fill",data:{result:{state:{accFillSz:"3",avgPx:"84010",fee:"-0.1",pos:"0"}}}};

test("fill amounts keep contracts and coins apart for exchange, PAPER and legacy raw events",()=>{
  const f=M.fillAmounts(ethShortFill);
  assert.deepEqual(f.filled,{coins:null,contracts:"0.13"});assert.deepEqual(f.after,{coins:"-0.013",contracts:null});
  assert.equal(f.price,"2249.9");assert.equal(f.fee,"-0.0146");
  const p=M.fillAmounts(paperFill);assert.deepEqual(p.filled,{coins:0.001,contracts:null});assert.deepEqual(p.after,{coins:0.001,contracts:null});
  const r=M.fillAmounts(legacyRawFill);assert.deepEqual(r.filled,{coins:null,contracts:"3"});assert.deepEqual(r.after,{coins:null,contracts:"0"},"OKX pos is contracts");
  assert.deepEqual(M.fillAmounts({}).filled,{coins:null,contracts:null});
});
test("rendered fills cells of an ETH season say ETH and контр., never BTC or a bare quantity",()=>{
  const cells=M.fillCells(ethShortFill,M.baseCurrency(eth));
  assert.equal(cells.filled,"0,13 контр.");assert.equal(cells.after,"−0,013 ETH");assert.equal(cells.price,"2249,9");assert.equal(cells.fee,"−0,0146");
  const text=Object.values(cells).join(" | ");
  assert.doesNotMatch(text,/BTC/);assert.match(text,/ETH/);
  const paper=M.fillCells(paperFill,M.baseCurrency({rules:{instrument:"BTC-USDT-SWAP"}}));
  assert.equal(paper.filled,"0,001 BTC");assert.equal(paper.after,"0,001 BTC");
  const page=fs.readFileSync(web("arena.js"),"utf8");
  assert.match(page,/U\.fillCells\(e,coin,money\)/,"the arena fills table renders through the shared helper");
  assert.equal(/Обсяг BTC|Залишок BTC|" BTC"/.test(page),false,"no hard-coded BTC quantity label is left in the arena page");
});

test("battle projections carry the season coin to the position labels",()=>{
  const battle=require("../web/arena.js"),coliseum=require("../web/arena-coliseum.js");
  const bots=[{id:"a",name:"A",position_btc:"-0.013",equity_usdt:"100",pnl_usdt:"0",status:"SHORT"}];
  assert.equal(battle.model({id:"r",key:"1",bots,instrument:"ETH-USDT-SWAP"}).base,"ETH");
  assert.equal(battle.model({id:"r",key:"1",bots}).base,"BTC","a battle input without an instrument is a BTC journal");
  const written=bots.map(b=>({...b,base_ccy:"SOL"}));  // R2.1.11 (G2, D141 p. 3): the coin the exchange wrote wins on the panel too
  assert.equal(battle.model({id:"r",key:"1",bots:written,instrument:"ETH-USDT-SWAP"}).base,"SOL");
  assert.equal(require("../web/arena-battle-view.js").model({id:"r",key:"1",bots:written}).base,"SOL");
  assert.equal(coliseum.project({...eth,status:"RUNNING",budget_usdt:100,bots,events:[]}).coin,"ETH");
  assert.equal(coliseum.project({...legacy,status:"RUNNING",budget_usdt:100,bots,events:[]}).coin,"BTC");
  assert.equal(coliseum.project({id:"x",status:"RUNNING",instrument:"ETH-USDT-SWAP",bots,events:[]}).coin,"ETH","the ArenaBattle wrapper passes instrument");
  assert.equal(coliseum.project({id:"x",mode:"OKX_DEMO",status:"RUNNING",bots,events:[]}).coin,M.UNKNOWN_COIN,"a replay-built exchange run has no instrument yet");
  assert.equal(coliseum.project({id:"x",mode:"PAPER",status:"RUNNING",bots,events:[]}).coin,"BTC");
  assert.doesNotMatch(coliseum.explain("position"),/у BTC за/);
  const source=fs.readFileSync(web("arena-coliseum.js"),"utf8");
  assert.equal(source.includes('"Позиція, BTC"'),false);assert.match(source,/"Позиція, "\+scene\.coin/);
});
test("R2 post-only entry codes have short Ukrainian labels in the coliseum and the replay",()=>{
  const coliseum=require("../web/arena-coliseum.js"),labels=require("../web/replay-labels.js");
  for(const code of ["entry_order_placed","entry_order_expired","entry_order_rejected","entry_order_cancelled","entry_order_cancel_failed","entry_order_unwatched_cancel","fill_read_lag"]){
    const shown=coliseum.eventLabel({code});
    assert.notEqual(shown,code.replace(/_/g," "),code);assert.match(shown,/[А-Яа-яІіЇїЄє]/,code);
    assert.equal(labels.phrase(code),shown,code+": one wording on both pages");
  }
});
test("market context of an ETH battle names ETH",()=>{
  const context=require("../web/battle-context.js");
  const frames=[100,101,100.5].map((mid,i)=>({at:new Date(Date.UTC(2026,9,30,12,i)).toISOString(),market:{mid:mid*22}}));
  assert.match(context.describe(context.fromFrames(frames,{coin:"ETH"})),/^ETH за бій: /);
  assert.match(context.describe(context.fromFrames(frames)),/^BTC за бій: /);
  assert.match(context.describe(context.fromFrames(frames,{coin:"<i>"})),/^BTC за бій: /,"only a plain currency code is shown");
});

// Fake DOM for the shared research panels (same approach as test_phase_diagnostics_ui.cjs).
class Element{
  constructor(tag){this.tagName=tag;this.children=[];this.textContent="";this.dataset={};this.attributes={};this.listeners={};this.classList={add(){}};}
  append(...values){this.children.push(...values);}
  replaceChildren(...values){this.children=values;this.textContent="";}
  setAttribute(k,v){this.attributes[k]=v;}
  addEventListener(k,v){this.listeners[k]=v;}
  focus(){}
}
const text=n=>n instanceof Element?String(n.textContent)+" "+n.children.map(text).join(" "):String(n);
function panels(){
  const window={ArenaReviewModel:M};
  vm.runInNewContext(fs.readFileSync(web("research-panels.js"),"utf8"),{window,document:{createElement:t=>new Element(t)},Node:Element,console});
  return window.ArenaResearchPanels;
}
test("rendered lots preview of an ETH recipe shows ETH and not BTC",()=>{
  const report={version_name:"ETH draft",evidence_kind:"PROVIDED_INPUTS",lint:[],explain:[],trials:[],compare:{},
    lots:{side:"short",result:"PASS",reasons:[],lot_qty:"0.001",min_qty:"0.001",requested_qty:"0.0133",rounded_qty:"0.013",target_notional_usdt:30,rounded_notional_usdt:29.25}};
  const ui=panels(),host=new Element("main");
  ui.mountWorkshopTools(host,report,{instrument:"ETH-USDT-SWAP"}).select("lots");
  const shown=text(host);assert.match(shown,/0\.0133 ETH/);assert.match(shown,/0\.013 ETH/);assert.match(shown,/0\.001 \/ 0\.001 ETH/);assert.doesNotMatch(shown,/BTC/);
  const old=new Element("main");ui.mountWorkshopTools(old,report,{}).select("lots");assert.match(text(old),/0\.013 BTC/,"a recipe without an instrument is BTC");
});
test("rendered review ledger labels exchange rows in contracts and PAPER rows in the season coin",()=>{
  const row=(source,quantity,after)=>({seq:5,role:"entry",quantity,position_before:"0",position_after:after,source});
  const bundle=(mode,rows)=>({schema_version:1,match:{id:"fake",mode},participants:[{id:"a",name:"Author",costs:{},funnel:{},trade_ledger:{rows,round_trips:[]}}],events:[]});
  const ui=panels(),exchange=new Element("main"),paper=new Element("main");
  ui.mountReview(exchange,bundle("OKX_DEMO",[row("exchange_incremental_contracts","0.13","-0.13")])).selectView("ledger");
  const shownExchange=text(exchange);assert.match(shownExchange,/0\.13 контрактів/);assert.match(shownExchange,/-0\.13 контрактів/);assert.doesNotMatch(shownExchange,/BTC|ETH/);
  ui.mountReview(paper,bundle("PAPER",[row("paper_incremental_btc","0.001","0.001")])).selectView("ledger");
  assert.match(text(paper),/0\.001 BTC/);
  const unknown=new Element("main");ui.mountReview(unknown,bundle("PAPER",[row("paper_incremental_btc",null,null)])).selectView("ledger");
  assert.match(text(unknown),/Не записано/);assert.doesNotMatch(text(unknown),/Не записано BTC/);
});
