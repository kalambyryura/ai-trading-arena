const test=require('node:test');
const assert=require('node:assert/strict');
const facts=require('../web/battle-facts.js');
const col=require('../web/arena-coliseum.js');
const league=require('../web/league.js');
const research=require('../web/strategy-research.js');
const site=require('../web/public-site.js');
const policy={id:'filled_entry_net_v1',primary:'RECONCILED_NET_USDT',tie_tolerance_usdt:'0.00000001',include_cash:false,minimum_entry_filled_orders:1};
const participant=(id,net,filled)=>({id,name:id,allocation_usdt:'100',position_btc:'0',
  funnel:{entry:{checks:3,signals:2,signals_unknown:0,jev_outcomes_unknown:0},entry_filled_orders:filled,order_intents:2,round_trips:filled,no_signal_unknown:0},
  costs:{net_usdt:net,gross_pnl_usdt:net,signed_fees_usdt:'0',signed_funding_usdt:'0',complete:true},
  quality:{data_status:'COMPLETE',settlement:{status:'SETTLED'},availability:{status:'COMPLETE'},competitive_validity:{status:'ELIGIBLE'}}});
const battle=rows=>({match:{status:'DONE',score_policy:policy},history_complete:true,participants:rows,events:[],cash:{net_usdt:'0'}});

test('winner at a loss: idle and Cash retain results but no tournament place',()=>{
  const model=facts.factsForBattle(battle([participant('idle','0',0),participant('best','-.1',1),participant('other','-.2',2)]));
  assert.deepEqual(model.ranking,['best','other']);assert.deepEqual(model.top.ids,['best']);
  assert.equal(model.excluded[0].reason,'no_filled_entry');
  assert.match(facts.battleHeadline(model),/зі збитком/);
  const rows=col.battleRows(model);assert.equal(rows.find(r=>r.id==='cash').place,null);assert.equal(rows.find(r=>r.id==='idle').place,null);
  assert.match(col.battleSummary(model,rows),/нижче Cash/);
  const sole=facts.factsForBattle(battle([participant('only','-0.1',1)]));
  assert.match(col.battleSummary(sole,col.battleRows(sole)),/^Перше місце/);
});
test('all idle is no winner, not a Cash tie; pending or missing evidence never wins',()=>{
  const b=battle([participant('idle','0',0)]),model=facts.factsForBattle(b);
  assert.equal(model.match.noContest,true);assert.deepEqual(model.top.ids,[]);
  assert.match(facts.battleHeadline(model),/Переможця немає/);
  const pending=battle([participant('best','1',1)]);pending.participants[0].position_btc='-.0003';
  assert.deepEqual(facts.factsForBattle(pending).top.ids,[]);
  const unknown=battle([participant('best','1',1)]);unknown.participants[0].funnel.entry_filled_orders=null;
  assert.deepEqual(facts.factsForBattle(unknown).top.ids,[]);
});
test('active winner ties use the frozen decimal tolerance',()=>{
  const model=facts.factsForBattle(battle([participant('a','.01',1),participant('b','.010000009',1)]));
  assert.deepEqual(new Set(model.top.ids),new Set(['a','b']));
  assert.deepEqual(col.battleRows(model).filter(r=>r.id!=='cash').map(r=>r.place),[1,1]);
  const distinct=facts.factsForBattle(battle([participant('a','.01',1),participant('b','.010000011',1)]));
  assert.deepEqual(distinct.top.ids,['b']);
});
test('league ranks trading recipes separately from Cash and never-entered recipes',()=>{
  const row=(id,net,n)=>({id,recipe_sha256:id,cohort:'new',score_policy:policy,participated_battles:n,no_entry_battles:n?0:1,comparable_battles:1,net_sum_usdt:net,net_mean_usdt:net});
  const model=league.standings({cohorts:[{cohort:'new',score_policy:policy,allocation_usdt:'100'}],frequencies:[row('cash','0',0),row('idle','0',0),row('best','-0.1',1)],battles:[]});
  const rows=model.groups[0].rows;assert.equal(rows.find(r=>r.id==='best').rank,1);
  assert.equal(rows.find(r=>r.id==='idle').rank,null);assert.equal(rows.find(r=>r.id==='cash').rank,null);
});
test('research opens only a safe draft link and states occupied Demo while Live runs',()=>{
  assert.equal(research.request('?research=1&parent=author-v1'),'author-v1');
  assert.equal(research.request('?research=1&parent=../secret'),null);
  assert.equal(research.link('../secret'),null);assert.equal(research.link('author-v1'),'/workshop?parent=author-v1&research=1');
  const status=research.status({active:{arena_live:{status:'RUNNING'},workshop_demo:{status:'DRAINING'}}});
  assert.equal(status.liveActive,true);assert.equal(status.demoBusy,true);
  assert.match(status.text,/зафіксований/);assert.match(status.text,/Demo-рахунок зайнятий/);
});
test('standalone public standings and battle card obey the same entry policy',()=>{
  const row=(id,net,n)=>({id,recipe_sha256:id,cohort:'new',score_policy:policy,participated_battles:n,no_entry_battles:n?0:1,comparable_battles:1,net_sum_usdt:net,net_mean_usdt:net});
  const standings={cohorts:[{cohort:'new',score_policy:policy,allocation_usdt:'100'}],frequencies:[row('cash','0',0),row('idle','0',0),row('best','-0.1',1)],battles:[]};
  const rows=site.standings(standings).groups[0].rows;
  assert.equal(rows.find(r=>r.id==='best').rank,1);
  assert.equal(rows.find(r=>r.id==='idle').rank,null);assert.equal(rows.find(r=>r.id==='cash').rank,null);
  const b=battle([participant('idle','0',0),participant('best','-0.1',1)]);
  const card=site.latestCard({latest:{id:'new',facts:b},battles:{league:standings}});
  assert.match(card.headline,/зі збитком/);assert.equal(card.rows.find(r=>r.id==='best').place,1);
  assert.equal(card.rows.find(r=>r.id==='cash').place,null);
  assert.match(site.battleList({battles:[{id:'new',score:{status:'NO_CONTEST'}}]})[0].result,/жодного виконаного входу/);
});

test('R2.1.11 (D141 p. 2): the coliseum says a battle of examples only does not count, whoever is on top',()=>{
  const model=facts.factsForBattle(battle([participant('only','0.1',1)]),{exampleOnly:true});
  assert.match(col.battleSummary(model,col.battleRows(model)),/^Технічна проба, не рахується/);
  const counted=facts.factsForBattle(battle([participant('only','0.1',1)]));
  assert.match(col.battleSummary(counted,col.battleRows(counted)),/^Перше місце/);
});
