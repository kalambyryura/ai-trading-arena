// Pure arithmetic checks. No browser, requests, keys, launchers or financial actions.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const context = require('../web/battle-context.js');

const T0 = Date.parse('2026-10-03T10:00:00.000Z');
const frames = (mids, minutes = 1, start = T0) => mids.map((mid, i) => ({ at: new Date(start + i * minutes * 60000).toISOString(), market: mid === null ? null : { mid } }));
const close = (actual, expected, tolerance = 1e-6) => assert.ok(Math.abs(actual - expected) < tolerance, `${actual} != ${expected}`);

test('too few priced frames is "not recorded", never a flat market', () => {
  for (const input of [undefined, null, [], frames([100, 101]), frames([null, null, null, 100]), frames([100, 0, -5, NaN]), 'text']) {
    const result = context.fromFrames(input);
    assert.equal(result.known, false);
    assert.match(context.describe(result), /^Рух ціни за бій не записано/);
  }
  assert.equal(context.fromFrames(frames([100, 101])).samples, 2);
  assert.match(context.describe(context.fromFrames(frames([100]))), /у пакеті мало кадрів із ціною\. Це не означає, що ринок стояв\./);
});

test('range, change, drawdown, run-up and volatility follow from a series worked out by hand', () => {
  const r = context.fromFrames(frames([100, 101, 100]));
  assert.equal(r.known, true);
  assert.equal(r.samples, 3);
  close(r.change_bps, 0);
  close(r.range_bps, 100);                       // 101 / 100 - 1
  close(r.max_drawdown_bps, 1 / 101 * 1e4);      // 101 -> 100 from the peak
  close(r.max_runup_bps, 100);                   // 100 -> 101 from the trough
  close(r.volatility_bps_per_sqrt_minute, Math.log(1.01) * 1e4);
  assert.equal(r.direction, 'sideways');
  assert.equal(r.span_seconds, 120);
  assert.equal(r.start_at, '2026-10-03T10:00:00.000Z');
  assert.equal(r.end_at, '2026-10-03T10:02:00.000Z');
});

test('volatility is per square-root minute, so spacing does not change what the market did', () => {
  const fast = context.fromFrames(frames([100, 101, 100, 101, 100], 1));
  const slow = context.fromFrames(frames([100, 101, 100, 101, 100], 2));
  close(slow.volatility_bps_per_sqrt_minute, fast.volatility_bps_per_sqrt_minute / Math.SQRT2, 1e-9);
  assert.equal(slow.range_bps, fast.range_bps);
});

test('a long silence is a gap: counted, and its jump is not treated as a one-minute return', () => {
  const mids = [100, 100.5, 100, 130, 130.5, 130];
  const withHole = frames(mids.slice(0, 3), 1).concat(frames(mids.slice(3), 1, T0 + 40 * 60000));
  const r = context.fromFrames(withHole);
  assert.equal(r.gaps, 1);
  const calm = context.fromFrames(frames([100, 100.5, 100, 100.5, 100]));
  assert.ok(r.volatility_bps_per_sqrt_minute < calm.volatility_bps_per_sqrt_minute * 1.5, 'the 30 % jump over the hole did not inflate the volatility');
  close(r.range_bps, (130.5 / 100 - 1) * 1e4);
  assert.match(context.describe(r), /у записі 1 пропуск понад 10 хв/);
});

test('direction needs a net move of at least a quarter of the range', () => {
  assert.equal(context.fromFrames(frames([100, 100.4, 100.8, 101.2, 101.6])).direction, 'up');
  assert.equal(context.fromFrames(frames([100, 99.6, 99.2, 98.8, 98.4])).direction, 'down');
  assert.equal(context.fromFrames(frames([100, 102, 98, 102, 100.5])).direction, 'sideways');   // net +0.5 of a 4 % range
  assert.equal(context.fromFrames(frames([100, 100, 100, 100])).direction, 'sideways');
  assert.equal(context.fromFrames(frames([100, 103, 99, 101.5])).direction, 'up', 'a net +1.5 % inside a 4 % range is 37 % of it');
  assert.equal(context.fromFrames(frames([100, 103, 99, 100.9])).direction, 'sideways', 'a net +0.9 % inside the same range is 22 % of it');
});

test('the order and duplicates of frames do not matter', () => {
  const ordered = frames([100, 101.5, 99, 100.7, 102]);
  const shuffled = [ordered[3], ordered[0], ordered[4], ordered[2], ordered[1], ordered[2]];
  assert.deepEqual(context.fromFrames(shuffled), context.fromFrames(ordered));
  const twice = [...ordered.slice(0, 2), { at: ordered[1].at, market: { mid: 101.5 } }, ...ordered.slice(2)];
  assert.equal(context.fromFrames(twice).samples, 5, 'one price per instant');
});

test('funding passes through only when it is a number', () => {
  assert.equal(context.fromFrames(frames([100, 101, 100]), { fundingRate: 0.0001 }).funding_rate, 0.0001);
  assert.equal(context.fromFrames(frames([100, 101, 100]), { fundingRate: 'n/a' }).funding_rate, null);
  assert.equal(context.fromFrames(frames([100, 101, 100])).funding_rate, null);
});

test('the paragraph states the numbers and says it is context, not cause', () => {
  const text = context.describe(context.fromFrames(frames([84000, 84210, 83790, 84168]), { fundingRate: 0.0001 })).replace(/ /g, ' ');   // thousands use a no-break space
  assert.match(text, /^BTC за бій: 84 000 → 84 168 \(\+0,2 %\); розмах від мінімуму до максимуму 0,5 %; найбільша просадка від піку 0,5 %; мінливість ≈ /);
  assert.match(text, /ціна здебільшого йшла вгору|чіткого напрямку не було/);
  assert.match(text, /остання відома ставка funding 1 bps/);
  assert.match(text, /Контекст, не причина результату\.$/);
});

test('the module is arithmetic only: no requests, no clock, no randomness', () => {
  const source = fs.readFileSync(path.join(__dirname, '../web/battle-context.js'), 'utf8');
  assert.equal(/\bfetch\s*\(|XMLHttpRequest|sendBeacon|WebSocket|EventSource|\/api\/|panel-token|localStorage|document\./.test(source), false);
  assert.equal(/Math\.random|Date\.now|new Date\(\)/.test(source), false);
});
