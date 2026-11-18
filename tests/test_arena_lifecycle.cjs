// Pure evidence/UI checks. No browser, requests, keys, launchers or financial actions.
const test = require('node:test');
const assert = require('node:assert/strict');
const ui = require('../web/arena-lifecycle.js');

class Node {
  constructor(tag) { this.tag=tag; this.className=''; this.textContent=''; this.children=[]; this.dataset={}; this.open=false; this.scrollTop=0; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=children; }
  setAttribute(key,value) { this[key]=value; }
  querySelectorAll(selector) {
    const matches = node => {
      if(selector.startsWith('.')&&!node.className.split(' ').includes(selector.match(/^\.([\w-]+)/)[1]))return false;
      if(selector.startsWith('details')&&node.tag!=='details')return false;
      if(selector.includes('[data-bot-id]')&&!node.dataset.botId)return false;
      if(selector.includes('[open]')&&!node.open)return false;
      return true;
    };
    return this.children.flatMap(child=>[...(matches(child)?[child]:[]),...child.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0]||null; }
}
global.document = {createElement: tag=>new Node(tag)};
const allText = node => [node.textContent,...node.children.map(allText)].join(' ');
const bot = (id,extra={}) => ({bot_id:id,name:id,required_count:6,observed_required_count:0,complete:false,
  rows:[{id:'entry',label:'Вхід',status:'not_observed',required:true}],
  current_phase:{code:'entry_checks',step:1,label:'Перевірка входу',event_seq:1},
  wait_reason:{message:'Чекаємо сигнал',failed_checks:[{feature:'rsi_14',op:'lte',expected:30,actual:60}],unknown_checks:['volume_ratio_5']},
  funnel:{entry:{checks:12,signals:0,jev_allows:0,rechecks:0},entry_filled_orders:0,round_trips:0},
  costs:{gross_pnl_usdt:'0',signed_fees_usdt:'-.01',signed_funding_usdt:null,net_usdt:null,cash_delta_usdt:null,
    source:'actual_order_facts',complete:false,jev:{requests:0,known_cost_usd:'0',unknown_billing_requests:0,complete:true}},...extra});
const lifecycle = () => ({bots:[bot('Sol'),bot('Astra',{observed_required_count:6,complete:true,
  funnel:{entry:{checks:14,signals:1,jev_allows:1,rechecks:1},entry_filled_orders:1,close_filled_orders:1,round_trips:1}})]});

test('all duration selectors use one supported list, with a single selected option',()=>{
  const options=ui.durationOptions(7200);
  assert.deepEqual(options.map(o=>o.value),[300,900,1800,3600,7200,18000,86400]);
  assert.deepEqual(options.filter(o=>o.selected).map(o=>o.value),[7200]);
});
test('exchange countdown uses the server duration despite wrong browser wall time',()=>{
  const run={mode:'OKX_LIVE',status:'RUNNING',clock_policy:'okx_utc_monotonic_v1',timing:{source:'okx_utc_monotonic_v1',admission_remaining_seconds:18000}};
  const opts=ui.optionsForRun(run), oldNow=Date.now;
  try { Date.now=()=>0; assert.equal(ui.runCountdown({},opts),'5:00:00');
    Date.now=()=>9999999999999; assert.equal(ui.runCountdown({},opts),'5:00:00');
    assert.equal(ui.runCountdown({},ui.optionsForRun({...run,timing:{...run.timing,admission_remaining_seconds:1}})),'00:01');
    assert.equal(ui.runCountdown({},ui.optionsForRun({...run,timing:{...run.timing,admission_remaining_seconds:null}})),null);
  } finally { Date.now=oldNow; }
});

test('countdown handles 24 hours, expiry and unknown dates without creating a deadline',()=>{
  assert.equal(ui.countdown('2026-10-01T00:00:00Z',Date.parse('2026-09-30T00:00:00Z')),'24:00:00');
  assert.equal(ui.countdown('2026-09-30T00:00:00Z',Date.parse('2026-10-01T00:00:00Z')),'00:00');
  assert.equal(ui.countdown(null),null);
});
test('per-bot model keeps idle Sol separate from completed Astra and never changes input',()=>{
  const source=lifecycle(),before=structuredClone(source),models=ui.lifecycleModel(source);
  assert.equal(models[0].observedRequired,0);assert.equal(models[1].observedRequired,6);
  assert.equal(models[0].complete,false);assert.equal(models[1].complete,true);
  assert.match(models[1].funnel.find(row=>row.id==='entry').label,/1 виконаних ордерів/);
  assert.deepEqual(source,before);
});
test('renderer explains failed versus unknown checks and separates signed USDT from unknown Jev USD',()=>{
  const source=lifecycle();source.bots[0].costs.jev.known_cost_usd=null;
  const rendered=allText(ui.renderLifecycle(source,{executionMode:'demo',timePolicy:'admission_then_drain',phase:'DRAINING'}));
  assert.match(rendered,/OKX Demo/);assert.match(rendered,/нові входи|Нові входи/);
  assert.match(rendered,/rsi_14.*60/);assert.match(rendered,/volume_ratio_5/);
  assert.match(rendered,/Комісії · зі знаком/);assert.match(rendered,/Jev · окремо в USD/);
  assert.match(rendered,/Відомі витрати · невідомо/);
});
test('late-round UI and caps describe drainage; old journals retain legacy notice',()=>{
  const run={mode:'OKX_LIVE',phase:'DRAINING',time_policy:'admission_then_drain',rules:{max_total_entries:20,max_drawdown_pct:10},match_manifest:{jev_budget:{season_attempts:61}}};
  const options=ui.optionsForRun(run);
  assert.equal(options.executionMode,'live');assert.match(options.technicalCaps,/61 запитів/);
  assert.match(allText(ui.renderLifecycle(lifecycle(),options)),/відкриті позиції завершують власні правила/);
  assert.match(allText(ui.renderLifecycle(lifecycle())),/стару жорстку межу часу/);
});
test('polling preserves expanded evidence cards, and injected strings remain literal text',()=>{
  const host=new Node('div'),source=lifecycle();source.bots[0].name='<script>order()</script>';
  ui.mount(host,source);host.querySelector('details[data-bot-id]').open=true;
  ui.mount(host,source);
  assert.equal(host.querySelector('details[data-bot-id]').open,true);
  assert.equal(host.querySelectorAll('details[data-bot-id]').length,2);
  assert.match(allText(host),/<script>order\(\)<\/script>/);
  assert.equal(ui.renderBotDetail(source,'missing'),null);
});

test('fixed Demo technical plan never advertises repeated entries or an ordinary 61-call cap',()=>{
  const evidence=lifecycle();evidence.bots=[bot('Probe',{diagnostic:true})];
  const run={mode:'OKX_DEMO',phase:'DONE',time_policy:'admission_then_drain',lifecycle:evidence,
    rules:{max_total_entries:20},match_manifest:{jev_budget:{season_attempts:61}}};
  const rendered=allText(ui.renderLifecycle(evidence,ui.optionsForRun(run)));
  assert.match(rendered,/до 6 запитів Jev/);assert.match(rendered,/Повторний вхід.*не дозволений/);
  assert.doesNotMatch(rendered,/61 запитів|може починати повторні цикли/);
});

test('the schema-4 feature book line names warm-up, holes and failure instead of a silent wait', () => {
  assert.equal(ui.bookText(null), null);
  assert.match(ui.bookText({status:'warming',attempts:1,warmup_attempts:3,warmup_pages:120,warmup_pages_expected:333}), /прогрів · прочитано 120 з ≈ 333 сторінок історії · спроба 1 з 3\. Рішень про вхід за схемою 4 ще немає\./);
  assert.match(ui.bookText({status:'ready',last_gap:null}), /^Книга ознак схеми 4 готова/);
  assert.match(ui.bookText({status:'degraded',detail:'degraded: gap',gap:{minutes:22}}), /дочитуємо пропуск 22 хв/);
  assert.match(ui.bookText({status:'degraded',detail:'degraded: index, mark',gap:null}), /не прочитано індекс, mark-ціну/);
  assert.match(ui.bookText({status:'exhausted',warmup_attempts:3}), /не вдався 3 рази: до кінця раунду/);
  assert.match(ui.bookText({status:'ready',last_gap:{read:10,minutes:20,missing:10,gave_up:false}}), /дочитано 10 з 20 хв, ознаки над ним UNKNOWN/);
  const options = ui.optionsForRun({mode:'OKX_LIVE',status:'RUNNING',feature_book:{status:'warming',attempts:1,warmup_attempts:3}});
  assert.equal(options.featureBook.status, 'warming');
  const view = ui.renderLifecycle({bots:[]}, options);
  assert.ok(view.querySelectorAll('.v6-book-note').length === 1);
});

test('a lone edge minute filled flat is no "UNKNOWN for the whole period" (R2.1.9)', () => {
  const flat = ui.bookText({status:'ready',last_gap:{read:0,minutes:1,missing:0,gave_up:true,flat_filled:true}});
  assert.match(flat, /заповнено пласко/);
  assert.doesNotMatch(flat, /UNKNOWN/);
  assert.match(ui.bookText({status:'ready',last_gap:{read:0,minutes:3,missing:3,gave_up:true,flat_filled:false}}), /UNKNOWN/);
});

test('R2.1.11 (R219-BOOK-SPREAD): spread features over a flat minute are UNKNOWN; read candles are no "UNKNOWN"', () => {
  const flat = ui.bookText({status:'ready',spread:true,last_gap:{read:0,minutes:1,missing:0,gave_up:true,flat_filled:true}});
  assert.match(flat, /цінові ознаки рахуються далі/);
  assert.match(flat, /Spread-ознаки над нею UNKNOWN на весь свій період/);
  const read = ui.bookText({status:'ready',spread:true,last_gap:{read:1,minutes:1,missing:0,gave_up:true,flat_filled:false}});
  assert.match(read, /Свічки останнього пропуску дочитано/);
  assert.doesNotMatch(read, /UNKNOWN на весь свій період/);
});
