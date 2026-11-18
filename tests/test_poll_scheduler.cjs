// Pure policy/scheduler checks with a fake clock. No browser, requests, keys, launchers or financial actions.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const sched = require('../web/poll-scheduler.js');

const flush = () => new Promise(resolve => setImmediate(resolve));
const BOTH = {arena: true, exchange: true, scheduled: false};

function harness({run = async () => ({}), hidden = false, context = {}} = {}) {
  let clock = 0, nextId = 1, inflight = 0, maxInflight = 0;
  const timers = new Map(), calls = [], ctx = {value: context}, flags = {hidden};
  const env = {
    now: () => clock,
    setTimer: (fn, ms) => { const id = nextId++; timers.set(id, {at: clock + ms, fn}); return id; },
    clearTimer: id => timers.delete(id),
    context: () => ctx.value,
    hidden: () => flags.hidden,
    run: async parts => {
      calls.push({at: clock, parts});
      inflight++; maxInflight = Math.max(maxInflight, inflight);
      try { return await run(parts, calls.length, ctx); } finally { inflight--; }
    }
  };
  const poller = sched.create(env);
  async function advance(ms) {
    const target = clock + ms;
    for (;;) {
      await flush();
      const due = [...timers.entries()].filter(([, timer]) => timer.at <= target).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      timers.delete(due[0]); clock = Math.max(clock, due[1].at); due[1].fn();
    }
    clock = target; await flush();
  }
  return {poller, calls, timers, ctx, flags, advance, now: () => clock, setClock: value => { clock = value; }, maxInflight: () => maxInflight};
}
const times = (calls, part) => calls.filter(call => call.parts[part]).map(call => call.at);

test('activity reads moving, paused and finished runs from the last answers', () => {
  const idle = {paperLive: false, paperPaused: false, exchangeLive: false, exchangePaused: false};
  assert.deepEqual(sched.activity(null, null), idle);
  assert.deepEqual(sched.activity({active: null}, {active: {arena_live: null, workshop_demo: {status: 'DONE'}}}), idle);
  for (const status of ['RUNNING', 'DRAINING', 'CLOSING']) {
    assert.deepEqual(sched.activity({active: {status}}, {active: {arena_live: {status}}}),
      {paperLive: true, paperPaused: false, exchangeLive: true, exchangePaused: false}, status);
  }
  assert.deepEqual(sched.activity({active: {status: 'PAUSED'}}, {active: {arena_live: {status: 'PAUSED'}}}),
    {paperLive: false, paperPaused: true, exchangeLive: false, exchangePaused: true});
  assert.equal(sched.activity(null, {active: {arena_live: {status: 'STARTING'}}}).exchangeLive, true, 'an unfamiliar status polls fast');
  assert.deepEqual(sched.activity(null, {active: {arena_live: {status: 'DONE'}, workshop_demo: {status: 'RUNNING'}}}),
    {...idle, exchangeLive: true});
  assert.deepEqual(sched.activity({active: 'RUNNING'}, {active: 5}), idle, 'junk answers do not throw and do not speed polling up');
});

test('activity can be limited to the contexts a page actually shows', () => {
  const exchange = {active: {arena_live: {status: 'DONE'}, workshop_demo: {status: 'RUNNING'}}};
  assert.equal(sched.activity(null, exchange).exchangeLive, true, 'default: any context');
  assert.equal(sched.activity(null, exchange, ['arena_live']).exchangeLive, false, 'a Demo run elsewhere does not speed the arena up');
  assert.equal(sched.activity(null, exchange, ['workshop_demo']).exchangeLive, true);
  assert.equal(sched.activity(null, exchange, ['missing']).exchangeLive, false);
  assert.equal(sched.activity(null, {active: {arena_live: {status: 'PAUSED'}}}, ['arena_live']).exchangePaused, true);
});

test('intervals follow what is moving and what is on screen', () => {
  const d = sched.DEFAULTS;
  assert.deepEqual(sched.intervals({exchangeLive: true}), {arena: d.unusedMs, exchange: d.activeMs});
  assert.deepEqual(sched.intervals({exchangePaused: true}), {arena: d.unusedMs, exchange: d.pausedMs});
  assert.deepEqual(sched.intervals({}), {arena: d.unusedMs, exchange: d.idleMs}, 'finished or empty arena: heartbeat only');
  assert.deepEqual(sched.intervals({paperVisible: true, paperLive: true}), {arena: d.activeMs, exchange: d.idleMs});
  assert.deepEqual(sched.intervals({paperVisible: true, paperPaused: true}), {arena: d.pausedMs, exchange: d.idleMs});
  assert.deepEqual(sched.intervals({paperVisible: true}), {arena: d.idleMs, exchange: d.idleMs});
  assert.deepEqual(sched.intervals({paperLive: true}), {arena: d.idleMs, exchange: d.idleMs}, 'live PAPER nobody looks at is not polled fast');
  assert.deepEqual(sched.intervals(undefined), {arena: d.unusedMs, exchange: d.idleMs});
  assert.ok(d.activeMs <= 5000, 'the stage reports silence after 15 s, so at least three polls must fit');
  assert.ok(d.timeoutMs < d.idleMs && d.timeoutMs >= d.activeMs);
});

test('backoff doubles from 2 s and stops at 30 s', () => {
  assert.deepEqual([0, 1, 2, 3, 4, 5, 6, 50].map(n => sched.backoffMs(n)), [0, 2000, 4000, 8000, 16000, 30000, 30000, 30000]);
  for (const bad of [-1, NaN, undefined, null]) assert.equal(sched.backoffMs(bad), 0);
});

test('start reads everything once, then each part follows its own cadence', async () => {
  const h = harness({context: {exchangeLive: true}});
  h.poller.start(); await h.advance(0);
  assert.deepEqual(h.calls, [{at: 0, parts: BOTH}]);
  await h.advance(120000);
  assert.deepEqual(times(h.calls, 'exchange'), Array.from({length: 41}, (_, i) => i * 3000));
  assert.deepEqual(times(h.calls, 'arena'), [0, 120000]);
  assert.equal(h.maxInflight(), 1);
  assert.equal(h.timers.size, 1, 'exactly one pending timer');
});

test('a slow answer is never overlapped: requests meanwhile become one follow-up run', async () => {
  const gates = [];
  const h = harness({run: () => new Promise(resolve => gates.push(resolve))});
  h.poller.start(); await flush();
  assert.equal(h.calls.length, 1);
  const settled = [];
  const kicks = [h.poller.kick({exchange: true}), h.poller.kick({exchange: true}), h.poller.kick({arena: true})]
    .map((promise, i) => promise.then(value => { settled.push(i); return value; }));
  await flush();
  assert.equal(h.calls.length, 1, 'no second request while the first is in flight');
  assert.equal(h.poller.state().queued, 3);
  gates[0]({}); await flush();
  assert.equal(h.calls.length, 2, 'one coalesced follow-up');
  assert.deepEqual(h.calls[1].parts, BOTH);
  assert.deepEqual(settled, [], 'callers wait for a run that started after their request');
  gates[1]({}); await flush(); await Promise.all(kicks);
  assert.deepEqual(settled.sort(), [0, 1, 2]);
  assert.equal(h.maxInflight(), 1);
  assert.equal(h.timers.size, 1);
});

test('only timer-driven runs are marked scheduled; anything a person asked for is read fresh', async () => {
  const h = harness({context: {exchangeLive: true}});
  h.poller.start(); await h.advance(0);
  assert.equal(h.calls[0].parts.scheduled, false, 'the first read after load');
  await h.advance(3000);
  assert.deepEqual(h.calls[1].parts, {arena: false, exchange: true, scheduled: true});
  await h.poller.kick({exchange: true});
  assert.equal(h.calls.at(-1).parts.scheduled, false, 'an explicit refresh');
  h.poller.setHidden(true); h.poller.setHidden(false); await flush();
  assert.equal(h.calls.at(-1).parts.scheduled, false, 'coming back to the tab');
});

test('a kick that lands while a scheduled run is in flight makes the follow-up fresh', async () => {
  const gates = [];
  const h = harness({context: {exchangeLive: true}, run: () => new Promise(resolve => gates.push(resolve))});
  h.poller.start(); await flush();
  gates[0]({}); await flush();               // first read done, next timer armed
  await h.advance(3000);                      // scheduled run starts and waits on its gate
  assert.equal(h.calls.at(-1).parts.scheduled, true);
  const kicked = h.poller.kick({exchange: true});
  gates[1]({}); await flush();
  assert.equal(h.calls.at(-1).parts.scheduled, false, 'the follow-up serves the person, not the timer');
  gates[2]({}); await kicked;
});

test('a kick that asks for one part runs only that part', async () => {
  const h = harness({context: {}});
  h.poller.start(); await h.advance(0);
  h.calls.length = 0;
  await h.poller.kick({exchange: true});
  await h.poller.kick({arena: true});
  assert.deepEqual(h.calls.map(call => call.parts), [{arena: false, exchange: true, scheduled: false}, {arena: true, exchange: false, scheduled: false}]);
});

test('a hidden tab stops polling and refreshes everything when it returns', async () => {
  const h = harness({context: {exchangeLive: true}});
  h.poller.start(); await h.advance(0);
  h.poller.setHidden(true);
  assert.equal(h.timers.size, 0);
  await h.advance(600000);
  assert.equal(h.calls.length, 1, 'no requests for ten hidden minutes');
  h.poller.setHidden(false); await flush();
  assert.equal(h.calls.length, 2);
  assert.deepEqual(h.calls[1], {at: 600000, parts: BOTH});
  assert.equal(h.timers.size, 1, 'schedule resumes');
  h.poller.setHidden(false); await flush();
  assert.equal(h.calls.length, 2, 'repeated visible events do not refresh again');
});

test('a tab opened in the background reads once, then nothing until it is shown', async () => {
  const h = harness({hidden: true});
  h.poller.start(); await h.advance(120000);
  assert.deepEqual(h.calls, [{at: 0, parts: BOTH}], 'one read so the page is not blank when shown');
  assert.equal(h.timers.size, 0);
  h.flags.hidden = false; h.poller.setHidden(false); await flush();
  assert.equal(h.calls.length, 2);
  assert.equal(h.timers.size, 1);
});

test('an answer that lands after the tab was hidden does not restart the timer', async () => {
  const gates = [];
  const h = harness({run: () => new Promise(resolve => gates.push(resolve))});
  h.poller.start(); await flush();
  h.poller.setHidden(true);
  gates[0]({}); await flush();
  assert.equal(h.timers.size, 0);
  assert.equal(h.poller.state().running, false);
});

test('an explicit kick still works in a hidden tab but does not resume polling', async () => {
  const h = harness({hidden: true});
  h.poller.start(); await flush();
  await h.poller.kick(BOTH);
  assert.equal(h.calls.length, 2, 'the first read plus the explicit one');
  assert.equal(h.timers.size, 0);
});

test('failures back off 2, 4, 8, 16, 30, 30 s and one success restores the cadence', async () => {
  const h = harness({run: async (parts, n) => ({exchange: n > 6})});
  h.poller.start(); await h.advance(125000);
  assert.deepEqual(times(h.calls, 'exchange'), [0, 2000, 6000, 14000, 30000, 60000, 90000, 120000]);
  assert.deepEqual(times(h.calls, 'arena'), [0, 120000], 'a failing exchange does not slow the PAPER part');
  assert.deepEqual(h.poller.state().failures, {arena: 0, exchange: 0});
});

test('failure counters are independent per part', async () => {
  const h = harness({context: {paperVisible: true, exchangeLive: true}, run: async parts => ({arena: false, exchange: true})});
  h.poller.start(); await h.advance(14000);
  assert.deepEqual(times(h.calls, 'arena'), [0, 2000, 6000, 14000]);
  assert.deepEqual(times(h.calls, 'exchange'), [0, 3000, 6000, 9000, 12000], 'exchange keeps its own cadence');
});

test('an explicit refresh that succeeds ends the backoff', async () => {
  const h = harness({run: async (parts, n) => ({exchange: n > 2})});
  h.poller.start(); await h.advance(2000);
  assert.equal(h.poller.state().failures.exchange, 2);
  await h.poller.kick({exchange: true});
  assert.equal(h.poller.state().failures.exchange, 0);
  const before = h.calls.length;
  await h.advance(29999);
  assert.equal(h.calls.length, before, 'idle cadence, not a retry');
  await h.advance(1);
  assert.equal(h.calls.length, before + 1);
});

test('a run that throws counts as a failure and polling continues', async () => {
  const h = harness({run: async (parts, n) => { if (n === 1) throw new Error('network down'); return {}; }});
  h.poller.start(); await h.advance(2000);
  assert.deepEqual(times(h.calls, 'exchange'), [0, 2000]);
  assert.equal(h.poller.state().failures.exchange, 0);
});

test('a broken context probe falls back to the idle cadence instead of stopping polling', async () => {
  const h = harness();
  h.ctx.value = null;
  Object.defineProperty(h.ctx, 'value', {get() { throw new Error('dom gone'); }});
  h.poller.start(); await h.advance(30000);
  assert.deepEqual(times(h.calls, 'exchange'), [0, 30000]);
});

test('the cadence follows the round: idle, then moving, then finished again', async () => {
  const h = harness({run: async (parts, n, ctx) => {
    if (n === 2) ctx.value = {exchangeLive: true};
    if (n === 5) ctx.value = {};
    return {};
  }});
  h.poller.start(); await h.advance(120000);
  assert.deepEqual(times(h.calls, 'exchange'), [0, 30000, 33000, 36000, 39000, 69000, 99000]);
  assert.deepEqual(times(h.calls, 'arena'), [0, 120000]);
});

test('a timer that fires early reschedules instead of polling', async () => {
  const h = harness({context: {exchangeLive: true}});
  h.poller.start(); await h.advance(0);
  const fire = () => { const [[id, timer]] = [...h.timers.entries()]; h.timers.delete(id); timer.fn(); return flush(); };
  h.setClock(2900); await fire();
  assert.equal(h.calls.length, 1, 'still the first call');
  assert.equal(h.timers.size, 1, 'rescheduled for the remainder');
  h.setClock(2990); await fire();
  assert.equal(h.calls.length, 2, 'within the slack the poll goes ahead');
});

test('stop cancels the timer; a later kick runs once without restarting the schedule', async () => {
  const h = harness({context: {exchangeLive: true}});
  h.poller.start(); await h.advance(0);
  h.poller.stop();
  assert.equal(h.timers.size, 0);
  await h.advance(60000);
  assert.equal(h.calls.length, 1);
  await h.poller.kick(BOTH);
  assert.equal(h.calls.length, 2);
  assert.equal(h.timers.size, 0);
  h.poller.start(); await h.advance(0);
  assert.equal(h.calls.length, 3, 'start after stop reads again');
});

test('a finished round costs well under 0.5 MB per minute (was 14.1 MB per minute)', async () => {
  // Payload sizes measured on the recorded fixture round: /api/arena/state 478.8 KB, /api/exchange/state 13.3 KB per poll.
  const SIZE = {arena: 478.8e3, exchange: 13.3e3}, MINUTES = 10;
  const h = harness({context: {}});
  h.poller.start(); await h.advance(MINUTES * 60000);
  const bytes = h.calls.reduce((sum, call) => sum + (call.parts.arena ? SIZE.arena : 0) + (call.parts.exchange ? SIZE.exchange : 0), 0);
  const perMinute = bytes / MINUTES / 1e6;
  assert.ok(perMinute < 0.5, `${perMinute.toFixed(2)} MB/min`);
  const before = (SIZE.arena + SIZE.exchange) * (60000 / 2000) / 1e6;
  assert.ok(before / perMinute > 20, `reduction ×${(before / perMinute).toFixed(0)}`);
});

test('a moving live round with the stage visible still updates more than once per stale-banner window', async () => {
  const h = harness({context: {exchangeLive: true}});
  h.poller.start(); await h.advance(60000);
  const stamps = times(h.calls, 'exchange');
  const gaps = stamps.slice(1).map((at, i) => at - stamps[i]);
  assert.ok(Math.max(...gaps) * 3 <= 15000, 'at least three polls inside the 15 s stale window');
});

test('the module only decides timing: no requests, no DOM, no owner endpoints', () => {
  const source = fs.readFileSync(path.join(__dirname, '../web/poll-scheduler.js'), 'utf8');
  assert.equal(/\bfetch\s*\(|XMLHttpRequest|sendBeacon|WebSocket|EventSource|\/api\/|panel-token|document\.|localStorage/.test(source), false);
  assert.equal(/method\s*:\s*["']POST|exchange\/(start|stop|resume)/.test(source), false);
});

test('only scheduled polls of the two state endpoints ask for the shared copy', () => {
  const page = fs.readFileSync(path.join(__dirname, '../web/arena.js'), 'utf8');
  assert.equal((page.match(/X-Viewer-Poll/g) || []).length, 1, 'one place builds the header');
  assert.ok(page.includes('else if(viewer)opts.headers={"X-Viewer-Poll":"1"}'), 'GET only: the POST branch never carries it');
  assert.ok(page.includes('request("/api/exchange/state",undefined,{...POLL,viewer:scheduled})'));
  assert.ok(page.includes('request("/api/arena/state",undefined,{...POLL,viewer:scheduled})'));
  // The owner-critical reads (generation before Start/Stop, profile check) call request() without the option.
  assert.ok(page.includes('const fresh=await request("/api/exchange/state");'), 'pre-mutation read stays fresh');
  assert.equal((page.match(/viewer:/g) || []).length, 2, 'no other request opts into the shared copy');
  assert.ok(page.includes('refreshArena(parts.scheduled)') && page.includes('refreshExchange(parts.scheduled)'));
});

test('arena page loads the scheduler before the page script and keeps a legacy fallback', () => {
  const html = fs.readFileSync(path.join(__dirname, '../web/arena.html'), 'utf8');
  const page = fs.readFileSync(path.join(__dirname, '../web/arena.js'), 'utf8');
  assert.ok(html.includes('<script src="/poll-scheduler.js" defer></script>'));
  assert.ok(html.indexOf('/poll-scheduler.js') < html.indexOf('/arena.js'), 'deferred scripts run in document order');
  assert.ok(page.includes('window.ArenaPollScheduler'));
  assert.ok(page.includes('setInterval(refreshAll,2000)'), 'an older server without the new static route keeps the old cadence');
});

test('workshop keeps one exchange state request in flight, and an owner action waits for a read that starts after it', async () => {
  const page = fs.readFileSync(path.join(__dirname, '../web/workshop.js'), 'utf8');
  const source = page.match(/function refreshExchange\(fresh=false\)\{.*?return exchangeInFlight;\}/)[0];
  const calls = [], pending = [];
  const refresh = new Function('loadExchange', `let exchangeInFlight=null;${source};return refreshExchange;`)(
    viewer => { calls.push(viewer); return new Promise(resolve => pending.push(resolve)); });
  refresh(); refresh(); refresh();  // timer ticks while the server's first answer is slow
  assert.equal(calls.length, 1, 'no pile of requests behind a slow answer');
  let done = false;
  const after = refresh(true).then(() => { done = true; });
  pending.shift()();
  await flush();
  assert.equal(calls.length, 2, 'the action reads again once the older answer is in');
  assert.equal(done, false);
  pending.shift()();
  await after;
  assert.equal(calls.length, 2);
  assert.deepEqual(calls, [true, false], 'timer ticks read the shared copy, the read after an action is fresh');
  assert.ok(page.includes('setInterval(refreshExchange,2000)'));
  assert.equal((page.match(/await refreshExchange\(true\)/g) || []).length, 3, 'start, mutation and read-only check');
  // A request that never answers would keep the single flight forever: the polled reads carry a timeout.
  assert.ok(page.includes('request("/api/exchange/state",undefined,{...STATE_POLL,viewer})'));
  assert.ok(page.includes('else if(viewer)options.headers={"X-Viewer-Poll":"1"}'), 'only a GET may ask for the shared copy');
  assert.equal((page.match(/,undefined,RUN_POLL\)/g) || []).length, 2);
  assert.ok(/async function request\(path,body,\{timeout=0,viewer=false\}=\{\}\)\{[^]*?new AbortController\(\)[^]*?clearTimeout\(timer\)/.test(page));
});
