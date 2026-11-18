// Pure statistics checks. No browser, requests, keys, launchers or financial actions.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const stats = require('../web/league-stats.js');

const day = i => `2026-10-${String(1 + (i % 28)).padStart(2, '0')}`;
const rows = (values, per = 1, extra = {}) => values.map((net, i) => ({ id: `b${i}`, net_usdt: String(net), cluster: day(Math.floor(i / per)), ...extra }));

test('no battles is stated, not turned into a zero', () => {
  const r = stats.pairedDiff([]);
  assert.deepEqual([r.n, r.mean, r.ci, r.label], [0, null, null, 'немає боїв']);
  assert.equal(stats.pairedDiff(null).n, 0);
});

test('unknown results are skipped, never counted as zero', () => {
  const r = stats.pairedDiff(rows([1, null, 'abc', undefined, '', 2]), { minBattles: 1 });
  assert.equal(r.n, 2);
  assert.equal(r.sum, 3);
});

test('wins, ties and losses are counted against the Cash of the same battle', () => {
  const r = stats.pairedDiff(rows(['0.5', '0', '0.000', '-0.2', '0.0000001', '-0']), { minBattles: 1 });
  assert.deepEqual([r.wins, r.ties, r.losses], [2, 3, 1]);
  const withCash = stats.pairedDiff([{ net_usdt: '0.3', cash_net_usdt: '0.3', cluster: 'a' }, { net_usdt: '0.4', cash_net_usdt: '0.3', cluster: 'b' }], { minBattles: 1 });
  assert.deepEqual([withCash.ties, withCash.wins], [1, 1], 'the difference to the Cash of the same battle counts');
});

test('identical outcomes give a degenerate interval around the mean', () => {
  const r = stats.pairedDiff(rows(Array(40).fill(-1), 4), { seed: 'cohort-1' });
  assert.equal(r.mean, -1);
  assert.deepEqual(r.ci, [-1, -1]);
  assert.equal(r.enough, true);
  assert.equal(r.label, '');
});

test('the result is deterministic for a seed and changes with it', () => {
  const data = rows(Array.from({ length: 60 }, (_, i) => Math.sin(i) * 2 - 0.3), 3);
  const a = stats.pairedDiff(data, { seed: 'cohort-A' }), b = stats.pairedDiff(data, { seed: 'cohort-A' }), c = stats.pairedDiff(data, { seed: 'cohort-B' });
  assert.deepEqual(a, b);
  assert.notDeepEqual(a.ci, c.ci);
  assert.ok(Math.abs(a.ci[0] - c.ci[0]) < 0.5 && Math.abs(a.ci[1] - c.ci[1]) < 0.5, 'a different seed only moves the edges a little');
  assert.deepEqual(stats.pairedDiff(data.slice().reverse(), { seed: 'cohort-A' }), a, 'the order of the battles does not matter');
});

test('the interval brackets the mean and widens when battles of one day move together', () => {
  const values = Array.from({ length: 80 }, (_, i) => (Math.floor(i / 4) % 2 === 0 ? 1.2 : -1.2));   // 4 battles per day, same sign inside a day
  const byDay = stats.pairedDiff(rows(values, 4), { seed: 's' });
  const asIfIndependent = stats.pairedDiff(rows(values, 4), { seed: 's', clusterKey: row => row.id });
  assert.ok(byDay.ci[0] <= byDay.mean && byDay.mean <= byDay.ci[1]);
  assert.ok(byDay.ci[1] - byDay.ci[0] > asIfIndependent.ci[1] - asIfIndependent.ci[0], 'same-day battles are one market, not independent evidence');
  assert.equal(byDay.clusters, 20);
  assert.equal(asIfIndependent.clusters, 80);
});

test('an author who clearly loses has an interval entirely below zero; a coin-flip author straddles it', () => {
  const loser = stats.pairedDiff(rows(Array.from({ length: 90 }, (_, i) => -0.4 - (i % 5) * 0.05), 3), { seed: 'x' });
  assert.ok(loser.ci[1] < 0);
  const coin = stats.pairedDiff(rows(Array.from({ length: 90 }, (_, i) => (i % 2 ? 0.5 : -0.5)), 3), { seed: 'x' });
  assert.ok(coin.ci[0] < 0 && coin.ci[1] > 0);
});

test('a lower confidence level gives a narrower interval', () => {
  const data = rows(Array.from({ length: 80 }, (_, i) => Math.sin(i * 0.9) * 2), 4);
  const wide = stats.pairedDiff(data, { seed: 'lvl', level: 0.9 }), narrow = stats.pairedDiff(data, { seed: 'lvl', level: 0.5 });
  assert.ok(narrow.ci[1] - narrow.ci[0] < wide.ci[1] - wide.ci[0]);
  assert.equal(narrow.level, 0.5);
});

test('few battles carry the plain-language warning', () => {
  const r = stats.pairedDiff(rows([1, -1, 0.5], 1), { seed: 'few' });
  assert.equal(r.enough, false);
  assert.equal(r.label, 'мало даних: 3 з 30 боїв');
  assert.equal(stats.pairedDiff(rows([1, -1, 0.5], 1), { minBattles: 3 }).label, '');
});

test('a thousand battles stay fast', () => {
  const data = rows(Array.from({ length: 1000 }, (_, i) => Math.sin(i * 1.7)), 5);
  const started = Date.now();
  const r = stats.pairedDiff(data, { seed: 'big' });
  assert.ok(Date.now() - started < 3000, `${Date.now() - started} ms`);
  assert.equal(r.n, 1000);
});

// ---- the shape of the league rows ----------------------------------------------------------------------------
const battle = (cohort, createdAt, results, validity = 'ELIGIBLE') => ({ id: `${cohort}-${createdAt}`, cohort, created_at: createdAt, competitive_validity: validity,
  score: { status: 'COMPLETE', known_results: results.map(([id, net]) => ({ id, net_usdt: net })) } });

test('intervals: one entry per cohort and participant, from eligible battles only', () => {
  const battles = [];
  for (let i = 0; i < 40; i += 1) {
    battles.push(battle('c1', `2026-10-${String(1 + (i % 20)).padStart(2, '0')}T10:00:00.000+00:00`, [['alpha', i % 2 ? '-0.5' : '-0.3'], ['beta', '0']]));
  }
  battles.push(battle('c1', '2026-10-05T10:00:00.000+00:00', [['alpha', '99']], 'INELIGIBLE'));
  battles.push(battle('c2', '2026-10-05T10:00:00.000+00:00', [['alpha', '-1']]));
  const out = stats.intervals(battles, { level: 0.9, minBattles: 30 });
  assert.deepEqual(Object.keys(out), ['c1:alpha', 'c1:beta', 'c2:alpha']);
  assert.equal(out['c1:alpha'].n, 40, 'the ineligible battle is not counted');
  assert.equal(out['c1:alpha'].days, 20);
  assert.equal(out['c1:alpha'].status, 'OK');
  assert.ok(out['c1:alpha'].high < 0 && out['c1:alpha'].mean < 0);
  assert.deepEqual([out['c1:beta'].mean, out['c1:beta'].low, out['c1:beta'].high], [0, 0, 0], 'no trades: exactly Cash');
  assert.equal(out['c2:alpha'].status, 'FEW_DATA');
  assert.equal(stats.verdict(out['c1:alpha']), 'BELOW_CASH');
  assert.equal(stats.verdict(out['c1:beta']), 'INDISTINGUISHABLE');
  assert.equal(stats.verdict(out['c2:alpha']), 'FEW_DATA');
  assert.equal(stats.verdict(null), 'NO_DATA');
});

test('intervals: clusters are UTC days, Cash rows and unknown times are ignored, bad input gives nothing', () => {
  assert.equal(stats.utcDay('2026-10-02T23:30:00.000-02:00'), '2026-10-03');
  assert.equal(stats.utcDay('2026-10-02T21:00:00.000+00:00'), '2026-10-02', 'the UTC day, not a shifted or local one');
  assert.equal(stats.utcDay('2026-10-02T23:59:59.999Z'), '2026-10-02');
  assert.equal(stats.utcDay('not a time'), null);
  const rowsIn = [battle('c', '2026-10-02T23:30:00.000-02:00', [['a', '1'], ['cash', '0']]), battle('c', '2026-10-03T00:10:00.000+00:00', [['a', '-1']]),
    battle('c', 'garbage', [['a', '5']]), { cohort: 'c' }, null];
  const out = stats.intervals(rowsIn, { minBattles: 1 });
  assert.deepEqual(Object.keys(out), ['c:a']);
  assert.equal(out['c:a'].n, 2);
  assert.equal(out['c:a'].days, 1, 'both battles started on the same UTC day');
  assert.deepEqual(stats.intervals(null), {});
  assert.deepEqual(stats.intervals([]), {});
});

test('intervals: a row does not change when other rows change, and equal results give equal intervals', () => {
  const base = [];
  for (let i = 0; i < 45; i += 1) base.push(battle('c', `2026-09-${String(1 + (i % 28)).padStart(2, '0')}T12:00:00.000+00:00`, [['a', String(Math.sin(i))]]));
  const alone = stats.intervals(base);
  const withOther = stats.intervals([...base, ...base.map(b => ({ ...b, score: { known_results: [{ id: 'z', net_usdt: '3' }] } }))]);
  assert.deepEqual(withOther['c:a'], alone['c:a']);
  const twins = stats.intervals(base.map(b => ({ ...b, score: { known_results: [{ id: 'a', net_usdt: b.score.known_results[0].net_usdt }, { id: 'b', net_usdt: b.score.known_results[0].net_usdt }] } })));
  assert.deepEqual(twins['c:a'], twins['c:b'], 'two participants with the same results must show the same interval');
  assert.notDeepEqual(stats.intervals(base, { seed: 'one' })['c:a'], stats.intervals(base, { seed: 'two' })['c:a']);
});

test('the module is pure arithmetic: no requests, no DOM, no owner endpoints', () => {
  const source = fs.readFileSync(path.join(__dirname, '../web/league-stats.js'), 'utf8');
  assert.equal(/\bfetch\s*\(|XMLHttpRequest|sendBeacon|WebSocket|EventSource|\/api\/|panel-token|localStorage|document\./.test(source), false);
  assert.equal(/Math\.random/.test(source), false, 'no hidden randomness: the same data must give the same number');
});
