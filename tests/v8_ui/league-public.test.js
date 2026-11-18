"use strict";
// Public league standings: pure view-model + textContent DOM. No browser, requests or financial routes.
const test=require("node:test"),assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path");
const League=require("../../web/league.js");

class Element {
  constructor(tag){this.tag=tag;this.className="";this.textContent="";this.children=[];this.dataset={};this.title="";
    this.classList={add:(...names)=>{this.className=[...new Set([...this.className.split(" ").filter(Boolean),...names])].join(" ");}};}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=children;}
}
const NBSP=" ";
const text=node=>[node.textContent,...node.children.map(text)].join(" ");
const find=(node,predicate)=>[...(predicate(node)?[node]:[]),...node.children.flatMap(child=>find(child,predicate))];
const cohort="c".repeat(64),other="d".repeat(64);
const row=(id,battles,sum,mean,extra={})=>({id,cohort,recipe_sha256:id.repeat(8),version:1,name:"Стратегія "+id,author_name:"Автор "+id,
  author_kind:"model",model:"model-"+id,comparable_battles:battles,sole_wins:1,shared_top:0,above_cash:2,below_cash:1,at_cash:0,
  net_sum_usdt:sum,net_mean_usdt:mean,...extra});
const cash=(battles,group=cohort)=>({id:"cash",cohort:group,recipe_sha256:"cash",version:0,name:"Cash",author_name:null,author_kind:"baseline",
  model:null,comparable_battles:battles,sole_wins:0,shared_top:1,above_cash:0,below_cash:0,at_cash:battles,net_sum_usdt:"0",net_mean_usdt:"0"});
const meta=(group,battles,extra={})=>({cohort:group,mode:"OKX_LIVE",mode_label:"Live-бій",allocation_usdt:"100",duration_seconds:86400,
  duration_label:"24 год",execution_mode:"shared_jev",participants:2,battles,first_at:"2026-10-06T10:00:00+00:00",last_at:"2026-10-08T10:00:00+00:00",...extra});

test("money keeps the sign, four decimals and exact tiny values",()=>{
  assert.deepEqual(["0.01","-0.02","0","0.00004","-0.000012",null,"abc"].map(value=>League.usdt(value)),
    ["+0,0100","−0,0200","0,0000","+0,00004","−0,000012","—","—"]);
  assert.equal(League.usdt("0.5",false),"0,5000");
});
test("cohorts keep server order, never merge, and share a place on equal Σnet",()=>{
  const league={cohorts:[meta(other,1),meta(cohort,3)],frequencies:[row("a",3,"0.5","0.1666"),cash(3),row("b",3,"0.5","0.1666"),
    row("z",1,"-1","-1",{cohort:other}),cash(1,other)]};
  const model=League.standings(league);
  assert.deepEqual(model.groups.map(group=>group.cohort),[other,cohort]);
  const group=model.groups[1];
  assert.deepEqual(group.rows.map(r=>[r.id,r.rank]),[["a",1],["b",1],["cash",3]]);
  assert.equal(group.short,"cccccccc");assert.equal(group.battles,3);
  for(const part of ["Live · реальні гроші","24 год","100 USDT на учасника","2 стратегії + Cash","правила + спільний Jev"])assert.ok(group.title.includes(part),part);
  assert.ok(group.period.includes("–"));
  assert.equal(group.rows[0].author,"Автор a · ШІ-модель · model-a");assert.equal(group.rows[2].author,"не торгує · net = 0");
  assert.ok(Math.abs(group.rows[0].percent-0.1666)<1e-9);
  assert.deepEqual(model.groups[0].rows.map(r=>[r.id,r.rank]),[["cash",1],["z",2]]);
});
test("headline is descriptive: counts results above Cash by sum and defers any verdict to 30 battles",()=>{
  assert.match(League.standings({battles:[]}).headline,/^Ще немає завершених порівнянних боїв/);
  const one=League.standings({cohorts:[meta(cohort,3)],frequencies:[row("a",3,"0.3","0.1"),row("b",3,"-0.3","-0.1"),cash(3)]});
  assert.equal(one.headline,"Вище за Cash за сумою net: 1 з 2 результатів · 3 порівнянні бої. Висновок щодо Cash — після 30 боїв.");
  const two=League.standings({cohorts:[meta(other,1),meta(cohort,21)],frequencies:[row("a",21,"0","0"),cash(21),row("z",1,"0.1","0.1",{cohort:other}),cash(1,other)]});
  assert.match(two.headline,/^Вище за Cash за сумою net: 1 з 2 результатів · 22 порівнянні бої у 2 групах\./);
  const single=League.standings({cohorts:[meta(cohort,1)],frequencies:[row("a",1,"0","0"),cash(1)]},null);
  assert.equal(single.headline,"Вище за Cash за сумою net: 0 з 1 результату · 1 порівнянний бій.","without the stats module the table stays descriptive");
  for(const model of [one,two,single])assert.doesNotMatch(model.headline,/доведено|перевага|переміг/);
});
test("intervals come from league-stats: only a whole 90 % interval above zero reads «вище за Cash»",()=>{
  const day=i=>`2026-10-${String(1+(i%20)).padStart(2,"0")}T10:00:00+00:00`;
  const battles=Array.from({length:40},(_,i)=>({id:"b"+i,cohort,created_at:day(i),competitive_validity:"ELIGIBLE",
    score:{known_results:[{id:"a",net_usdt:String(0.1+(i%5)*0.01)},{id:"b",net_usdt:String(i%2?0.05:-0.05)}]}}));
  battles.push({id:"x",cohort,created_at:day(3),competitive_validity:"INELIGIBLE",score:{known_results:[{id:"a",net_usdt:"-999"}]}});
  const league={battles,cohorts:[meta(cohort,40)],frequencies:[row("a",40,"4.8","0.12"),row("b",40,"0","0"),cash(40)]};
  const model=League.standings(league),by=Object.fromEntries(model.groups[0].rows.map(r=>[r.id,r]));
  assert.equal(by.a.verdict,"ABOVE_CASH");assert.ok(by.a.interval.low>0&&by.a.interval.high<0.15);assert.deepEqual([by.a.sample.n,by.a.sample.days],[40,20]);
  assert.equal(by.b.verdict,"INDISTINGUISHABLE");assert.ok(by.b.interval.low<0&&by.b.interval.high>0);
  assert.equal(by.cash.verdict,"BASELINE");assert.equal(by.cash.interval,null);
  assert.match(model.headline,/Стабільно вище за Cash \(90 % інтервал\): 1 з 2 результатів\.$/);
  assert.deepEqual(League.standings(league).groups[0].rows.map(r=>r.interval),model.groups[0].rows.map(r=>r.interval),"fixed seed: no flicker between reloads");
  const few=League.standings({...league,battles:battles.slice(0,10)}).groups[0].rows.find(r=>r.id==="a");
  assert.equal(few.verdict,"FEW_DATA");assert.equal(few.interval,null);assert.equal(few.sample.n,10);
});
test("legacy rows without cohort metadata or net fields render without ranks",()=>{
  const legacy={frequencies:[{id:"fixture-a",name:"Учасник A · вигаданий",version:1,cohort:"fixture-cohort-v1",comparable_battles:0,sole_wins:0,shared_top:0},
    {id:"cash",name:"Cash",version:0,cohort:"fixture-cohort-v1",comparable_battles:0,sole_wins:0,shared_top:0}]};
  const model=League.standings(legacy),group=model.groups[0];
  assert.deepEqual(group.rows.map(r=>r.rank),[null,null]);assert.equal(group.rows[0].net,null);
  assert.equal(group.title,"1 стратегія + Cash");assert.equal(group.period,null);assert.equal(group.rows[0].author,"автор не вказаний");
  assert.match(model.headline,/^Порівнянні бої є, але сум ще немає/);
});
test("standings DOM uses text nodes, row headers, a Cash marker and the reading notes",()=>{
  const previous=global.document;global.document={createElement:tag=>new Element(tag)};
  try{
    const host=new Element("section"),league={cohorts:[meta(cohort,30)],frequencies:[row("a",30,"3","0.1"),cash(30)]};
    League.mountStandings(host,league);
    assert.ok(host.className.split(" ").includes("research-shell"));
    assert.equal(find(host,node=>node.tag==="th"&&node.scope==="row").length,2);
    assert.equal(find(host,node=>node.tag==="tr"&&node.dataset.cash==="true").length,1);
    assert.deepEqual(find(host,node=>node.tag==="td").map(node=>node.dataset.label).slice(0,8),
      ["Місце","Боїв","Σ net, USDT","За бій, USDT","90 % інтервал за бій","Проти Cash","Перемоги","Висновок"]);
    const all=text(host);
    for(const part of ["Таблиця ліги","Вище за Cash за сумою net: 1 з 1 результату","краще"+NBSP+"2 · нарівні"+NBSP+"0 · гірше"+NBSP+"1",
      "одноосібні"+NBSP+"1 · спільні"+NBSP+"0","+0,1000","+0,100 % від ставки","Як читати таблицю","не доводить переваги","bootstrap за календарними добами UTC"])
      assert.ok(all.includes(part),part);
    assert.deepEqual(find(host,node=>node.className==="league-verdict").map(node=>[node.dataset.verdict,node.textContent]),[["NO_DATA","немає даних"],["BASELINE","еталон"]]);
    assert.equal(/±|Попереду Cash|Позаду Cash/.test(all),false);
    League.mountStandings(host,{battles:[]});assert.equal(find(host,node=>node.tag==="table").length,0);
  }finally{global.document=previous;}
  const source=fs.readFileSync(path.join(__dirname,"../../web/league.js"),"utf8");
  assert.equal(/innerHTML|insertAdjacentHTML|outerHTML|document\.write/.test(source),false);
});
