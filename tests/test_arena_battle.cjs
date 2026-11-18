// Only the pure presentation model is loaded; no DOM, network or execution entrypoint.
const test = require('node:test');
const assert = require('node:assert/strict');
const {model, planBeat, demoFrames, ringLayout} = require('../web/arena.js');
const bot = (id, pnl, extra={}) => ({id, name:id, equity_usdt:100+pnl, pnl_usdt:pnl, status:'LONG', position_btc:.001, ...extra});
const scene = (bots, previousBots=[]) => model({id:'fixture',key:2,budget:100,bots,previousBots,moving:true});

test('higher PnL attacks lower PnL; equal PnL never invents a winner', () => {
  const s=scene([bot('one',3),bot('two',-2),bot('three',1)]);
  for(let i=0;i<9;i++) {const beat=planBeat(s,i);assert.equal(beat.kind,'punch');assert.ok(s.actors.find(a=>a.id===beat.attacker).pnl>s.actors.find(a=>a.id===beat.targets[0]).pnl);}
  const tied=scene([bot('one',1),bot('two',1)]);assert.equal(planBeat(tied,0),null);assert.ok(tied.actors.every(a=>a.leader));
});
test('an observed gain hits every eligible peer without changing their money', () => {
  const bots=[bot('gain',1),bot('loss',-1),bot('cash',0,{strategy_id:'cash',position_btc:0}),bot('retired',2,{status:'STOPPED',position_btc:0}),bot('ko',-100)];
  const frozen=structuredClone(bots),s=scene(bots,[bot('gain',.5)]),beat=planBeat(s,0);
  assert.deepEqual(beat,{kind:'wave',attacker:'gain',targets:['loss'],delta:.5});
  assert.deepEqual(bots,frozen);
});
test('all gainers get their own opening wave; losses stay losses', () => {
  const s=scene([bot('a',2),bot('b',-1)],[bot('a',1),bot('b',-2)]);
  assert.equal(planBeat(s,0).attacker,'a');assert.equal(planBeat(s,1).attacker,'b');
  assert.equal(s.actors[1].pnl,-1);assert.equal(s.actors[1].delta,1);
});
test('KO only follows actual nonpositive equity; risk stop is not death', () => {
  const s=scene([bot('risk',-10,{status:'STOPPED',position_btc:0}),bot('zero',-100),bot('negative',-110)]);
  assert.equal(s.actors[0].state,'retired');assert.equal(s.actors[0].health,90);assert.equal(s.actors[0].ko,false);
  assert.equal(s.actors[1].state,'ko');assert.equal(s.actors[2].health,0);assert.equal(planBeat(s,0),null);
});
test('HP reflects equity, with gains above 100 percent and no visual debit', () => {
  const s=scene([bot('profit',20),bot('loss',-.01)]);
  assert.equal(s.actors[0].health,120);assert.equal(s.actors[1].health,99.99);
  for(let i=0;i<20;i++)planBeat(s,i);
  assert.equal(s.actors[1].equity,99.99);
});
test('cash is passive even when it leads; missing values are not a zero balance', () => {
  const s=scene([bot('cash',0,{strategy_id:'cash'}),bot('loss',-1),bot('unknown',null,{equity_usdt:null})]);
  assert.equal(planBeat(s,0),null);assert.equal(s.actors[2].state,'unknown');assert.equal(s.actors[2].ko,false);
});
test('first frame and absent previous values do not fabricate profit waves', () => {
  const s=scene([bot('one',2),bot('two',1)]);
  assert.equal(s.actors[0].delta,null);assert.equal(planBeat(s,0).kind,'punch');
  assert.equal(scene([bot('one',2)],[bot('one',null)]).actors[0].delta,null);
});
test('replay depends only on its selected frame and immediate predecessor', () => {
  const frames=[[bot('a',0),bot('b',0)],[bot('a',1),bot('b',-1)],[bot('a',70),bot('b',-100)]];
  const first=scene(frames[0]),middle=scene(frames[1],frames[0]);
  assert.equal(first.actors[0].health,100);assert.equal(planBeat(first,0),null);
  assert.equal(middle.actors[0].delta,1);assert.equal(middle.actors[1].ko,false);
  assert.deepEqual(scene(frames[1],frames[0]),middle);
});
test('explicit demo covers seven actors, a KO and a solvent retirement', () => {
  const frames=demoFrames();assert.equal(frames.length,14);assert.ok(frames.every(f=>f.bots.length===7));
  const last=scene(frames.at(-1).bots,frames.at(-2).bots);
  assert.ok(last.actors.some(a=>a.ko));assert.ok(last.actors.some(a=>a.retired&&a.equity>0));
  assert.ok(frames.every(f=>f.bots.at(-1).pnl_usdt===0));
  frames[0].bots[0].equity_usdt=0;assert.equal(demoFrames()[0].bots[0].equity_usdt,100);
});

test('equal nonpositive PnL keeps open positions on guard without inventing a punch', () => {
  const s=scene([bot('one',-.01),bot('two',-.01),bot('cash',0,{strategy_id:'cash',position_btc:0})]);
  assert.equal(planBeat(s,0),null);
  assert.ok(s.actors.slice(0,2).every(a=>a.state==='guard'&&a.zone==='ring'&&a.active));
});

const event=(seq,id,code,step=2)=>({seq,bot_id:id,code,step});
const project=(bots,previousBots=[],events=[],extra={})=>model({id:'cycle',key:1,budget:100,bots,previousBots,events,eventSeq:20,previousEventSeq:0,...extra});
const flat=extra=>bot('a',5,{position_btc:0,status:'WAITING',...extra});

test('a waiting winner stays outside and never hits or receives a hit',()=>{
  const s=scene([flat(),bot('b',1),bot('c',-1)],[flat({pnl_usdt:0})]);
  assert.equal(s.actors[0].zone,'outside');assert.equal(s.actors[0].active,false);
  for(let i=0;i<12;i++){const action=planBeat(s,i);assert.notEqual(action.attacker,'a');assert.ok(!action.targets.includes('a'));}
});
test('entry intent stays outside until a recorded position exists',()=>{
  const pending=project([flat()],[],[event(1,'a','entry_intent')]).actors[0];
  assert.equal(pending.cycle,'entry_pending');assert.equal(pending.zone,'outside');assert.equal(pending.transition,null);
  const filled=project([bot('a',5)],[flat()],[event(1,'a','entry_intent'),event(2,'a','paper_entry_fill',3)]).actors[0];
  assert.equal(filled.zone,'ring');assert.equal(filled.cycle,'entering');assert.equal(filled.transition,'enter');
});
test('long, short and a very small nonzero remainder all belong on the ring',()=>{
  for(const quantity of [.001,-.001,1e-10,-1e-10]){
    const actor=project([bot('a',0,{position_btc:quantity,status:quantity<0?'SHORT':'LONG'})]).actors[0];
    assert.equal(actor.zone,'ring');assert.equal(actor.inMarket,true);
  }
});
test('an add intent and an actual add retain ring membership with distinct stages',()=>{
  const original=bot('a',5),events=[event(1,'a','entry_intent')];
  const pending=project([original],[original],events).actors[0];
  assert.equal(pending.cycle,'add_pending');assert.equal(pending.transition,null);
  const added=project([bot('a',5,{position_btc:.002})],[original],[...events,event(2,'a','paper_entry_fill',3)]).actors[0];
  assert.equal(added.zone,'ring');assert.equal(added.transition,'add');assert.equal(added.cycle,'adding');
});
test('stop, terminal labels and partial closes cannot pretend a remaining position is flat',()=>{
  for(const status of ['CLOSING','STOPPED','DONE']){
    const actor=project([bot('a',5,{position_btc:.0001,status,stop_requested:true})],[bot('a',5)],
      [event(1,'a','paper_close_fill',6)]).actors[0];
    assert.equal(actor.zone,'ring');assert.equal(actor.retired,false);assert.equal(actor.transition,null);assert.equal(actor.cycle,'closing');
  }
  const closed=project([flat({stop_requested:true,status:'STOPPED'})],[bot('a',5)],
    [event(1,'a','paper_close_fill',6)]).actors[0];
  assert.equal(closed.transition,'exit');assert.equal(closed.zone,'outside');assert.equal(closed.ko,false);
});
test('missing positions are unknown, not outside-zero or guessed from LONG status',()=>{
  const actor=project([bot('a',5,{position_btc:null})]).actors[0];
  assert.equal(actor.cycle,'unknown');assert.equal(actor.zone,'unknown');assert.equal(actor.active,false);assert.equal(actor.transition,null);
});
test('a cancelled entry and failed add do not preserve pending intent',()=>{
  const events=[event(1,'a','entry_intent'),event(2,'a','entry_cancelled',1)];
  assert.equal(project([flat()],[],events).actors[0].cycle,'waiting');
  assert.equal(project([bot('a',5)],[],events).actors[0].cycle,'holding');
});
test('Jev review and HOLD cannot masquerade as a pending fill, including replay',()=>{
  const events=[event(1,'a','entry_intent'),event(2,'a','jev_pending',2),
    {...event(3,'a','jev_gate',2),data:{allowed:false}}];
  assert.equal(project([flat()],[],events,{mode:'replay',eventSeq:2}).actors[0].cycle,'reviewing');
  assert.equal(project([flat()],[],events).actors[0].cycle,'waiting');
  assert.equal(project([bot('a',5)],[],events).actors[0].cycle,'holding');
  events[2].data.allowed=true;
  assert.equal(project([flat()],[],events).actors[0].cycle,'entry_pending');
  assert.equal(project([bot('a',5)],[],events).actors[0].cycle,'add_pending');
});
test('replay stage ignores future events by sequence and timestamp; rewinding removes entry',()=>{
  const events=[event(1,'a','entry_checks',1),event(2,'a','entry_intent'),event(3,'a','paper_entry_fill',3),event(4,'a','close_intent',6)];
  const early=project([flat()],[],events,{mode:'replay',eventSeq:1}).actors[0];
  assert.equal(early.zone,'outside');assert.equal(early.cycle,'checking');
  const middle=project([bot('a',5)],[flat()],events,{mode:'replay',eventSeq:3}).actors[0];
  assert.equal(middle.cycle,'entering');
  const rewind=project([flat()],[],events,{mode:'replay',eventSeq:1}).actors[0];assert.deepEqual(rewind,early);
  const timed=project([flat()],[],[...events.slice(0,1),{...events[1],at:'2026-09-29T12:00:01Z'}],
    {mode:'replay',at:'2026-09-29T12:00:00Z',eventSeq:2}).actors[0];assert.equal(timed.cycle,'checking');
});
test('technical pause retains the actual position without retirement',()=>{
  const actor=project([bot('a',5)],[],[],{paused:true}).actors[0];
  assert.equal(actor.zone,'ring');assert.equal(actor.cycle,'paused');assert.equal(actor.retired,false);
});
test('a closing participant awaits exit on the ring and can take a hit, but stops initiating attacks',()=>{
  const s=scene([bot('closing',2,{status:'CLOSING'}),bot('b',3),bot('c',1)],[bot('closing',1)]);
  assert.equal(s.actors[0].inMarket,true);
  const actions=Array.from({length:6},(_,i)=>planBeat(s,i));
  assert.ok(actions.every(a=>a.attacker!=='closing'));assert.ok(actions.some(a=>a.targets.includes('closing')));
});
test('demo shows checks, pending fills, entrance, add, partial close, exit and reentry',()=>{
  const frames=demoFrames(),actorAt=i=>project(frames[i].bots,frames[i-1]?.bots,frames[i].events,{eventSeq:frames[i].eventSeq}).actors[0];
  assert.ok(frames[0].bots.every(b=>b.position_btc===0));assert.equal(actorAt(1).cycle,'entry_pending');
  assert.equal(actorAt(2).transition,'enter');assert.equal(actorAt(5).cycle,'add_pending');assert.equal(actorAt(6).transition,'add');
  assert.equal(actorAt(8).zone,'ring');assert.equal(actorAt(8).cycle,'closing');assert.equal(actorAt(9).transition,'exit');
  assert.equal(actorAt(10).zone,'outside');assert.equal(actorAt(11).transition,'enter');
});
test('desktop and narrow layouts keep each actor and its label in the correct zone',()=>{
  const actors=scene(demoFrames()[3].bots).actors;
  for(const width of [320,390,700,1100]){
    const layout=ringLayout(actors,width),bottom=layout.ring.y+layout.ring.height;
    for(const actor of actors){
      const outside=layout.outside.get(actor.id);assert.ok(outside.y>bottom);assert.ok(outside.y+55<layout.height-25);
      assert.ok(outside.x>20&&outside.x<width-20);
      const inside=layout.inside.get(actor.id);if(inside){assert.ok(inside.y-95*inside.scale>layout.ring.y);assert.ok(inside.y+55<bottom);}
    }
  }
});
