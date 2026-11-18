// R2.1.12 (MIX-7, MIX-9 = R2111RC2-TEST-WEB): behavioural checks of the labels and coins, no source-text regexps.
// Pure functions the pages call; no browser, requests, keys or financial actions.
const test = require('node:test');
const assert = require('node:assert/strict');
const M = require('../web/review-model.js');
const page = require('../web/review.js');
const facts = require('../web/battle-facts.js');
const col = require('../web/arena-coliseum.js');
const context = require('../web/battle-context.js');

const BTC = 'BTC-USDT-SWAP', ETH = 'ETH-USDT-SWAP';

test('MIX-9: the review identity line carries the probe label only when the header says so', () => {
  const requested = {id: 'run1'}, match = {mode: 'OKX_LIVE', status: 'DONE'};
  assert.equal(page.identityText(requested, match, true), 'run1 · OKX_LIVE · DONE · технічна проба, не рахується');
  assert.equal(page.identityText(requested, match, false), 'run1 · OKX_LIVE · DONE');
});

test('MIX-9: readGET turns the X-Season-Example-Only header into the flag, 1 only', async () => {
  const saved = global.fetch;
  try {
    for (const [value, expected] of [['1', true], ['0', false], [null, false]]) {
      global.fetch = async () => ({ok: true, status: 200, text: async () => '{}',
        headers: {get: name => ({'X-Season-Example-Only': value, 'X-Season-Instrument': BTC})[name] ?? null}});
      const response = await page.readGET('/api/review/exchange/run1');
      assert.equal(response.exampleOnly, expected, String(value));
    }
  } finally { global.fetch = saved; }
});

test('MIX-9: the arena takes "examples only" from the history row or the review header', () => {
  const header = value => ({get: name => name === 'X-Season-Example-Only' ? value : null});
  assert.equal(facts.exampleOnlyFrom({}, header('1')), true);
  assert.equal(facts.exampleOnlyFrom({}, header('0')), false);
  assert.equal(facts.exampleOnlyFrom({example_only: true}, header('0')), true);
  assert.equal(facts.exampleOnlyFrom({example_only: false}, null), false);
});

test('MIX-9: the coliseum rows show the position\'s and the round\'s entries apart, "—" when unknown', () => {
  assert.deepEqual(col.entriesText({entries: 1, entries_total: 3}), {entries: '1', entries_total: '3'});
  assert.deepEqual(col.entriesText({entries: 0}), {entries: '0', entries_total: '—'});
  assert.deepEqual(col.entriesText(null), {entries: '—', entries_total: '—'});
});

test('MIX-7: the review page takes its coin from the frames\' bots before the instrument', () => {
  const original = {match: {mode: 'OKX_LIVE'}, frames: [{bots: [{id: 'a', base_ccy: 'SOL'}]}]};
  assert.equal(M.displayCoin(page.coinSourceFor(original, BTC)), 'SOL');
  assert.equal(M.displayCoin(page.coinSourceFor({match: {mode: 'OKX_LIVE'}, frames: []}, ETH)), 'ETH');  // no frames: the header
});

test('MIX-7: a mixed season names each bot\'s coin and every contract\'s market, never one season coin', () => {
  const season = {rules: {instrument: 'MIXED', instruments: {a: BTC, b: ETH}},
    latest_markets: {[BTC]: {at: '2026-11-05T10:00:05Z', mid: 84000}, [ETH]: {at: '2026-11-05T10:00:10Z', mid: 3000}}};
  assert.equal(M.mixed(season), true);
  assert.equal(M.mixed({rules: {instrument: BTC}}), false);
  assert.equal(M.botCoin({id: 'b', base_ccy: 'ETH'}, season), 'ETH');
  assert.equal(M.botCoin({id: 'b'}, season), 'ETH');  // before its first frame: from the rules
  assert.equal(M.botCoin({id: 'a'}, season), 'BTC');
  assert.equal(M.botCoin({id: 'c', base_ccy: 'SOL'}, season), 'SOL');  // the coin the exchange wrote wins
  assert.equal(M.marketAt(season), '2026-11-05T10:00:10Z');
  assert.equal(M.marketAt({latest_market: {at: 'x'}}), 'x');  // a one-contract season, as before
  assert.deepEqual(Object.keys(M.marketsOf(season)), [BTC, ETH]);
});

test('MIX-7: the author report\'s price context is the bot\'s own contract in a mixed season', () => {
  const frames = Array.from({length: 6}, (_, i) => ({at: new Date(Date.UTC(2026, 10, 5, 10, i)).toISOString(), market: null,
    markets: {[BTC]: {mid: 84000 + i}, [ETH]: {mid: 3000 + i}}}));
  const eth = context.fromFrames(frames, {instrument: ETH, coin: 'ETH'});
  assert.equal(eth.known, true);
  assert.ok(eth.samples >= 3);
  assert.equal(context.fromFrames(frames, {}).known, false);  // no season market in a mixed season's frame
});
