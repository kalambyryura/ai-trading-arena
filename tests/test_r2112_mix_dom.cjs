// R2.1.12 RC2 (X3-01, X3-03, X3-04, X3-05, X3-07): the pages themselves on a fake DOM, so a defect at a call site cannot pass.
// No browser, no real server, no requests beyond the fake fetch; Start, Stop and Resume are never pressed.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const WEB = path.join(__dirname, '..', 'web');
const fresh = name => {delete require.cache[require.resolve(path.join(WEB, name))]; return require(path.join(WEB, name));};
const BTC = 'BTC-USDT-SWAP', ETH = 'ETH-USDT-SWAP';

class FakeNode {
  constructor(tag = 'div') {
    this.tagName = String(tag).toUpperCase(); this.className = ''; this._text = ''; this.children = []; this.attrs = {}; this.listeners = {};
    this.dataset = {}; this.hidden = false; this.disabled = false; this.value = ''; this.type = ''; this.title = ''; this.open = false; this.parent = null; this.href = '';
    const self = this;
    this.classList = {
      add: (...names) => {const set = new Set(self.className.split(/\s+/).filter(Boolean)); names.forEach(n => set.add(n)); self.className = [...set].join(' ');},
      remove: (...names) => {self.className = self.className.split(/\s+/).filter(n => n && !names.includes(n)).join(' ');},
      toggle: (name, force) => {const has = self.className.split(/\s+/).includes(name), on = force === undefined ? !has : !!force; if (on && !has) self.classList.add(name); if (!on && has) self.classList.remove(name); return on;},
      contains: name => self.className.split(/\s+/).includes(name)};
    this.style = new Proxy({}, {get: (t, k) => k === 'setProperty' ? ((a, b) => {t[a] = b;}) : k === 'removeProperty' ? (a => {delete t[a];}) : (t[k] ?? ''), set: (t, k, v) => {t[k] = v; return true;}});
    this.clientWidth = 1200; this.offsetWidth = 1200; this.clientHeight = 800; this.scrollLeft = 0; this.scrollTop = 0; this.width = 1200; this.height = 800;
  }
  get lastChild() {return this.children.at(-1) ?? null;}
  get firstChild() {return this.children[0] ?? null;}
  get lastElementChild() {return this.children.at(-1) ?? new FakeNode('span');}
  get textContent() {return this._text + this.children.map(c => c.textContent).join('');}
  set textContent(v) {this._text = String(v); this.children.forEach(c => {c.parent = null;}); this.children = [];}
  append(...nodes) {for (const n of nodes) {const node = typeof n === 'string' ? Object.assign(new FakeNode('#text'), {_text: n}) : n; if (node.parent) node.remove(); node.parent = this; this.children.push(node);}}
  prepend(...nodes) {const old = this.children; this.children = []; this.append(...nodes); this.children.push(...old);}
  after(...nodes) {if (!this.parent) return; const p = this.parent, i = p.children.indexOf(this); nodes.forEach(n => {if (n.parent) n.remove(); n.parent = p;}); p.children.splice(i + 1, 0, ...nodes);}
  before(...nodes) {if (!this.parent) return; const p = this.parent, i = p.children.indexOf(this); nodes.forEach(n => {if (n.parent) n.remove(); n.parent = p;}); p.children.splice(i, 0, ...nodes);}
  replaceChildren(...nodes) {this._text = ''; this.children.forEach(c => {c.parent = null;}); this.children = []; this.append(...nodes);}
  remove() {if (this.parent) {this.parent.children = this.parent.children.filter(c => c !== this); this.parent = null;}}
  contains(node) {for (let n = node; n; n = n.parent) if (n === this) return true; return false;}
  setAttribute(k, v) {this.attrs[k] = String(v);}
  getAttribute(k) {return this.attrs[k] ?? null;}
  removeAttribute(k) {delete this.attrs[k];}
  hasAttribute(k) {return k in this.attrs;}
  toggleAttribute(k, force) {const on = force === undefined ? !(k in this.attrs) : !!force; if (on) this.attrs[k] = ''; else delete this.attrs[k]; return on;}
  addEventListener(type, fn) {(this.listeners[type] = this.listeners[type] || []).push(fn);}
  removeEventListener(type, fn) {this.listeners[type] = (this.listeners[type] || []).filter(f => f !== fn);}
  focus() {} blur() {} scrollIntoView() {} scrollTo() {} showModal() {this.open = true;} close() {this.open = false;}
  getBoundingClientRect() {return {left: 0, top: 0, width: 1200, height: 800, right: 1200, bottom: 800};}
  querySelector() {return new FakeNode('option');} querySelectorAll() {return [];}
  getContext() {return new Proxy({}, {get: (t, k) => k in t ? t[k] : () => ({addColorStop() {}}), set: (t, k, v) => {t[k] = v; return true;}});}
}
const walk = (node, out = []) => {out.push(node); node.children.forEach(c => walk(c, out)); return out;};
const media = () => ({matches: false, addEventListener() {}, removeEventListener() {}});
const saved = {};
function sandbox(search = '') {
  for (const key of ['document', 'window', 'location', 'fetch', 'setInterval', 'requestAnimationFrame', 'cancelAnimationFrame', 'ResizeObserver', 'IntersectionObserver']) saved[key] = Object.getOwnPropertyDescriptor(globalThis, key);
  const nodes = new Map();
  const set = (name, value) => Object.defineProperty(globalThis, name, {value, configurable: true, writable: true});
  set('document', {getElementById: id => {if (!nodes.has(id)) nodes.set(id, new FakeNode('div')); return nodes.get(id);},
    createElement: tag => new FakeNode(tag), createTextNode: text => Object.assign(new FakeNode('#text'), {_text: String(text)}),
    createElementNS: (_ns, tag) => new FakeNode(tag), querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, removeEventListener() {}, body: new FakeNode('body'), hidden: false, visibilityState: 'visible'});
  set('location', {search});
  set('window', {matchMedia: media, addEventListener() {}});
  set('requestAnimationFrame', () => 1); set('cancelAnimationFrame', () => {});
  set('setInterval', () => 1);
  for (const name of ['ResizeObserver', 'IntersectionObserver']) set(name, class {observe() {} unobserve() {} disconnect() {}});
  return {node: id => document.getElementById(id), nodes};
}
function restore() {for (const [key, d] of Object.entries(saved)) {if (d) Object.defineProperty(globalThis, key, d); else delete globalThis[key];}}
const answer = (body, headers = {}) => ({ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body), headers: {get: name => headers[name] ?? null}});
const tick = async () => {for (let i = 0; i < 30; i++) await new Promise(resolve => setImmediate(resolve));};

const mixedRun = (extra = {}) => ({id: 'a'.repeat(32), status: 'RUNNING', mode: 'OKX_LIVE', context: 'arena_live', execution_mode: 'rules_only', budget_usdt: 100,
  rules: {instrument: 'MIXED', instruments: {v1: BTC, h10: ETH}},
  strategies: [{id: 'v1', name: 'V1', version: 1, author: {kind: 'model', name: 'A'}}, {id: 'h10', name: 'H10', version: 1, author: {kind: 'model', name: 'B'}}],
  latest_markets: {[BTC]: {at: '2026-11-05T10:00:05Z', mid: 84000}, [ETH]: {at: '2026-11-05T09:30:00Z', mid: 3000}}, frames: [],
  events: [{seq: 1, at: '2026-11-05T10:00:00Z', bot_id: 'h10', code: 'exchange_entry_fill', step: 3, message: 'fill', data: {contracts: '1', position_btc: 0.1, price_usdt: 3000}}],
  bots: [{id: 'v1', strategy_id: 'v1', name: 'V1', position_btc: 0.0012, equity_usdt: 100, pnl_usdt: 0, base_ccy: 'BTC', entries: 1, entries_total: 1, allocation_usdt: 100, status: 'HOLDING'},
    {id: 'h10', strategy_id: 'h10', name: 'H10', position_btc: 0.1, equity_usdt: 101.25, pnl_usdt: 1.25, base_ccy: 'ETH', entries: 1, entries_total: 3, allocation_usdt: 100, status: 'HOLDING'}], ...extra});
const oneContractRun = () => mixedRun({rules: {instrument: ETH}, latest_markets: undefined, latest_market: {at: '2026-11-05T10:00:05Z', mid: 3000}});

// ---- the coliseum's bot details (X3-01) ----
function coliseumDetail(run, id) {
  const Coliseum = fresh('arena-coliseum.js');  // before the fake document: its require of arena.js must not start the page
  const page = sandbox();
  window.ArenaReviewModel = fresh('review-model.js');
  const host = new FakeNode('section'), view = Coliseum.create(host);
  view.update(run, {key: 'k1'});
  view.inspect(id);
  const nodes = walk(host), terms = nodes.filter(n => n.tagName === 'DT'), values = nodes.filter(n => n.tagName === 'DD');
  const field = label => {const i = terms.findIndex(t => t.textContent.startsWith(label)); return i < 0 ? null : {term: terms[i].textContent, value: values[i]?.textContent};};
  const hero = nodes.find(n => n.className.startsWith('col-detail-hero-value col-detail-result'));
  view.destroy();
  return {page, position: field('Позиція'), entries: field('Входи в поточній'), total: field('Входи за раунд'), hero};
}
test('X3-01: the coliseum details show the result and the position of a bot, in every season', () => {
  try {
    const {position, hero, entries, total} = coliseumDetail(oneContractRun(), 'h10');
    assert.equal(hero.textContent, '+1,25');
    assert.equal(entries.value, '1');
    assert.equal(total.value, '3');  // RC2 review, CALLSITE: the round's entries, not the position's
    assert.ok(hero.className.includes('positive'));
    assert.deepEqual(position, {term: 'Позиція, ETH', value: '0,1'});
  } finally {restore();}
});
test('X3-01: a losing bot gets the negative class; a mixed season names each bot its own coin', () => {
  try {
    const run = mixedRun();
    run.bots[0].pnl_usdt = -2.5; run.bots[0].equity_usdt = 97.5;
    const {position, hero} = coliseumDetail(run, 'v1');
    assert.equal(hero.textContent, '−2,5');
    assert.ok(hero.className.includes('negative'));
    assert.deepEqual(position, {term: 'Позиція, BTC', value: '0,0012'});
    restore();
    assert.deepEqual(coliseumDetail(mixedRun(), 'h10').position, {term: 'Позиція, ETH', value: '0,1'});
  } finally {restore();}
});

// ---- the battle cards without the coliseum (X3-04) ----
for (const file of ['arena-battle-view.js', 'arena.js']) test(`X3-04: the battle cards of a mixed season show each bot in its own coin (${file})`, () => {
  try {
    const Battle = fresh(file);  // before the fake document: arena.js would start its page
    const page = sandbox();
    window.ArenaReviewModel = fresh('review-model.js');
    assert.equal(window.ArenaColiseum, undefined);
    const host = new FakeNode('section'), run = mixedRun(), view = Battle.create(host, () => {}, () => {});
    view.update({id: run.id, key: '1', instrument: run.rules.instrument, rules: run.rules, bots: run.bots, budget: 100, mode: 'live', events: run.events, moving: false});
    const cards = walk(host).filter(n => n.className === 'battle-card-position').map(n => n.textContent);
    assert.deepEqual(cards, ['На рингу · Long · 0,0012 BTC', 'На рингу · Long · 0,1 ETH']);
    view.destroy();
    assert.ok(page.nodes);
  } finally {restore();}
});
test('X3-04: a bot before its first frame is named from the rules; a one-contract season keeps its coin', () => {
  try {
    sandbox();
    window.ArenaReviewModel = fresh('review-model.js');
    const Battle = fresh('arena-battle-view.js');
    const rules = mixedRun().rules, bots = [{id: 'v1', position_btc: 0.1}, {id: 'h10', position_btc: 0.1}];
    assert.deepEqual(Battle.model({id: 'r', key: 1, bots, rules, instrument: 'MIXED'}).actors.map(a => a.coin), ['BTC', 'ETH']);
    assert.deepEqual(Battle.model({id: 'r', key: 1, bots, instrument: ETH}).actors.map(a => a.coin), ['ETH', 'ETH']);
    assert.deepEqual(Battle.model({id: 'r', key: 1, bots}).actors.map(a => a.coin), ['BTC', 'BTC']);  // a journal from before R2
  } finally {restore();}
});

// ---- the review page (X3-04, X3-05) ----
const participants = [{id: 'v1', instrument: BTC}, {id: 'h10', instrument: ETH}];
const packet = {package_hash: 'h', evidence_kind: 'SYNTHETIC', manifest: {id: 'run1', participants: [{id: 'v1', name: 'V1', slot: 0}, {id: 'h10', name: 'H10', slot: 1}]},
  events: [{seq: 1, bot_id: 'v1', type: 'fill', code: 'entry', available_at_ms: 0, financial: {position_btc: '0.0012'}},
    {seq: 2, bot_id: 'h10', type: 'fill', code: 'entry', available_at_ms: 1000, financial: {position_btc: '0.1'}}]};
const snapshot = {playing: false, event_count: 2, state: {seq: 2, phase: 'RUNNING', quality: {coverage: 'FULL'}, bots: [
  {id: 'v1', financial: {position_btc: '0.0012', pnl_usdt: '0', known_jev_usd: '0'}, settled: false},
  {id: 'h10', financial: {position_btc: '0.1', pnl_usdt: '0', known_jev_usd: '0'}, settled: false}]}};
async function reviewPage({exampleOnly, match, view = ''}) {
  const page = sandbox('?mode=exchange&id=run1' + view), node = page.node;
  const bundle = {match: {id: 'run1', mode: 'OKX_LIVE', status: 'DONE', ...match}, participants, events: [], frames: []};
  globalThis.fetch = async url => answer(url.includes('/replay/') ? packet : bundle, {'X-Season-Example-Only': exampleOnly ? '1' : '0', 'X-Season-Instrument': 'BTC-USDT-SWAP'});
  let onFrame = null;
  Object.assign(window, {ArenaReviewModel: fresh('review-model.js'), ArenaReviewVerify: {verifyReview: async () => ({verified: true})},
    ArenaResearchPanels: {mountReview: () => ({selectBot() {}}), mountReplayControls: (_h, _r, options) => {onFrame = options.onFrame; return {seekEvent() {}, destroy() {}};}},
    ArenaEventReplay: {validate() {}, packetDigest: async () => 'h', Replay: class {constructor(p) {this.packet = p;} snapshot() {return snapshot;} pause() {}}},
    ArenaReplayLabels: {index: () => new Map([[1, {action: 'Вхід', before: '0', after: '0.0012'}], [2, {action: 'Вхід', before: '0', after: '0.1'}]]), side: v => (Number(v) > 0 ? 'Long' : 'Short')},
    ArenaColiseum: {create: () => ({update() {}, destroy() {}})}});
  const mounted = await fresh('review.js').mount();
  return {node, mounted, onFrame: () => onFrame};
}
test('X3-05: the visible identity line of the review keeps the probe label after mount, in both views', async () => {
  try {
    const {node, mounted} = await reviewPage({exampleOnly: true, match: {}});
    assert.equal(node('review-identity').textContent, 'run1 · OKX_LIVE · DONE · технічна проба, не рахується');
    await mounted.show('replay');
    assert.equal(node('review-identity').textContent, 'run1 · OKX_LIVE · причинний перегляд · технічна проба, не рахується');
    await mounted.show('review');
    assert.equal(node('review-identity').textContent, 'run1 · OKX_LIVE · DONE · технічна проба, не рахується');
  } finally {restore();}
});
test('X3-05: an ordinary battle has no such label in either view', async () => {
  try {
    const {node, mounted} = await reviewPage({exampleOnly: false, match: {}});
    assert.equal(node('review-identity').textContent, 'run1 · OKX_LIVE · DONE');
    await mounted.show('replay');
    assert.equal(node('review-identity').textContent, 'run1 · OKX_LIVE · причинний перегляд');
  } finally {restore();}
});
test('X3-04: the replay events of a mixed season are in each participant\'s coin', async () => {
  try {
    const {node, mounted, onFrame} = await reviewPage({exampleOnly: false, match: {instrument: 'MIXED', instruments: [BTC, ETH]}});
    await mounted.show('replay');
    onFrame()({run: {}, extras: {}, previous: null}, snapshot, {reset: true});
    const positions = walk(node('replay-events')).filter(n => n.className === 'review-event-position').map(n => n.textContent);
    assert.deepEqual(positions, ['0 ETH → 0,1 ETH', '0 BTC → 0,0012 BTC']);  // newest first
    assert.deepEqual(walk(node('replay-prefix')).filter(n => n.tagName === 'TD' && /Long/.test(n.textContent)).map(n => n.textContent), ['Long · 0,0012 BTC', 'Long · 0,1 ETH']);
  } finally {restore();}
});
test('X3-04: the replay events of a one-contract season keep the season coin', async () => {
  try {
    const {node, mounted, onFrame} = await reviewPage({exampleOnly: false, match: {instrument: ETH}});
    await mounted.show('replay');
    onFrame()({run: {}, extras: {}, previous: null}, snapshot, {reset: true});
    const positions = walk(node('replay-events')).filter(n => n.className === 'review-event-position').map(n => n.textContent);
    assert.deepEqual(positions, ['0 ETH → 0,1 ETH', '0 ETH → 0,0012 ETH']);
  } finally {restore();}
});

// ---- the arena page's Live viewer (X3-03, X3-04) ----
async function arenaPage(run, exampleOnly = '0') {
  const page = sandbox(), captured = {};
  const select = page.node('live-execution-mode'); select.value = 'rules_only';
  page.node('live-budget').value = '100'; page.node('live-duration').value = '300';
  const facts = fresh('battle-facts.js');
  Object.assign(window, {ArenaReviewModel: fresh('review-model.js'), ArenaBattle: fresh('arena-battle-view.js'),
    ArenaBattleFacts: {exampleOnlyFrom: facts.exampleOnlyFrom, battleHeadline: () => 'h', factsForBattle: (_bundle, options) => ({match: {exampleOnly: options.exampleOnly}}), factsForBot: () => ({}), render() {}},
    ArenaColiseum: {
    create: (_host, options) => {captured.options = options; return {update(r) {captured.run = r;}, connection() {}, destroy() {}, battle(bf) {captured.battle = bf;}};},
    runLabel: () => 'Раунд', phaseLabel: () => 'Триває', durationLabel: () => '5 хв', pnl: v => String(v)}});
  globalThis.fetch = async url => {
    if (url === '/api/arena/state') return answer({api_version: 4, mode: 'PAPER', active: null, seasons: [], catalogue: []});
    if (url === '/api/exchange/state') return answer({generation: 1, active: {arena_live: run}, history: [], profiles: [], ordinary_execution_modes: ['rules_only']});
    if (url.startsWith('/api/review/')) return answer({match: {}}, {'X-Season-Example-Only': exampleOnly});
    return answer(run);
  };
  require.cache[require.resolve(path.join(WEB, 'arena.js'))] && delete require.cache[require.resolve(path.join(WEB, 'arena.js'))];
  require(path.join(WEB, 'arena.js'));
  await tick();
  return {...page, captured};
}
const clock = iso => new Intl.DateTimeFormat('uk-UA', {hour: '2-digit', minute: '2-digit', second: '2-digit'}).format(new Date(iso));
test('X3-03, X3-04: the Live viewer of a mixed season names both pairs and every contract\'s price with its own time', async () => {
  try {
    const run = mixedRun(), {node, captured} = await arenaPage(run);
    assert.equal(captured.run.id, run.id, node('live-profile-status').textContent);
    assert.ok(node('watch-subtitle').textContent.endsWith('BTC / USDT · ETH / USDT'), node('watch-subtitle').textContent);
    assert.equal(node('watch-markets').hidden, false);
    assert.equal(node('watch-markets').textContent, `BTC 84 000 · ${clock('2026-11-05T10:00:05Z')} | ETH 3 000 · ${clock('2026-11-05T09:30:00Z')}`.replace(/ (?=000)/g, ' '));
  } finally {restore();}
});
test('X3-03: a one-contract Live season shows no such line and its one pair', async () => {
  try {
    const {node} = await arenaPage(oneContractRun());
    assert.equal(node('watch-markets').hidden, true);
    assert.equal(node('watch-markets').textContent, '');
    assert.ok(node('watch-subtitle').textContent.endsWith('· ETH / USDT'), node('watch-subtitle').textContent);
    assert.ok(!node('watch-subtitle').textContent.includes('BTC'));
  } finally {restore();}
});
test('X3-04: the contract note under a bot\'s fills names that bot\'s contract', async () => {
  try {
    const run = mixedRun(), {node, captured} = await arenaPage(run);
    await captured.options.onJournal(run, 'h10');
    await tick();
    const body = node('detail-body').textContent;
    assert.ok(body.includes('1 контр. ETH-USDT-SWAP = 0,1 ETH'), body.slice(0, 300));
    assert.ok(!body.includes('BTC-USDT-SWAP'));
  } finally {restore();}
});

for (const [header, expected] of [['1', true], ['0', false]]) test(`RC2 review, CALLSITE: the battle card takes "examples only" from the review header (${header})`, async () => {
  try {
    const {captured} = await arenaPage(mixedRun({status: 'DONE'}), header);
    assert.equal(captured.battle.match.exampleOnly, expected);
  } finally {restore();}
});

// ---- the league and the public site (X3-02) ----
const leagueInput = mark => ({battles: [], frequencies: [{id: 'a', cohort: 'c'.repeat(64), name: 'A', version: 1, comparable_battles: 1, net_sum_usdt: '1', net_mean_usdt: '1', above_cash: 1, at_cash: 0, below_cash: 0, sole_wins: 1, shared_top: 0}],
  cohorts: [{cohort: 'c'.repeat(64), mode: 'OKX_LIVE', allocation_usdt: '100', duration_label: '24 год', battles: 1, participants: 1, ...(mark ? {cohort_mark: mark} : {})}]});
test('X3-02: the league and the public site name a mixed cohort in the group title, and only a mixed one', () => {
  const mark = 'змішаний: BTC ×1 + ETH ×1', League = fresh('league.js'), Site = fresh('public-site.js');
  for (const [name, api] of [['league', League], ['public site', Site]]) {
    assert.ok(api.standings(leagueInput(mark)).groups[0].title.includes(mark), name);
    assert.ok(!api.standings(leagueInput(null)).groups[0].title.includes('змішаний'), name);
  }
});

// ---- the replay (X2-07) ----
test('X2-07: the replay page accepts and shows whose market failed', () => {
  const Replay = fresh('event-replay.js'), Labels = fresh('replay-labels.js'), Fixture = require('./v8_ui/synthetic-fixture.js');
  const packet = JSON.parse(JSON.stringify(Fixture.replay)), event = packet.events.find(e => e.quality);
  event.quality = {coverage: 'GAP', billing: 'UNKNOWN', reason: 'market_unavailable', instrument: ETH};
  assert.equal(Replay.validate(packet), true);
  const moment = Labels.moments(packet).find(m => m.kind === 'quality');
  assert.ok(moment.label.includes(ETH), moment.label);
});

// ---- the pure helpers the pages use ----
test('X3-03: a contract without a quote is named, a stale one keeps its own time', () => {
  const M = fresh('review-model.js'), rules = {instrument: 'MIXED', instruments: {a: BTC, b: ETH}};
  const line = (markets) => M.marketsLine({rules, latest_markets: markets}, v => String(v), at => at);
  assert.equal(line({[BTC]: {mid: 1, at: 't1'}}), 'BTC 1 · t1 | ETH —');
  assert.equal(line({[BTC]: {mid: 1, at: 't9'}, [ETH]: {mid: 2, at: 't1'}}), 'BTC 1 · t9 | ETH 2 · t1');
  assert.equal(line(undefined), 'BTC — | ETH —');
  assert.equal(M.marketsLine({rules: {instrument: BTC}, latest_market: {mid: 1, at: 't'}}, String, String), null);
});
test('X3-04: the pair label and the contract note of a mixed season', () => {
  const M = fresh('review-model.js'), season = {rules: {instrument: 'MIXED', instruments: {a: BTC, b: ETH}}};
  assert.equal(M.pairLabel(season), 'BTC / USDT · ETH / USDT');
  assert.equal(M.pairLabel({rules: {instrument: ETH}}), 'ETH / USDT');
  assert.equal(M.pairLabel({}), 'BTC / USDT');
  assert.equal(M.contractNote(season, {id: 'b'}), '1 контр. ETH-USDT-SWAP = 0,1 ETH');
  assert.equal(M.contractNote(season, {id: 'a'}), '1 контр. BTC-USDT-SWAP = 0,01 BTC');
  assert.equal(M.contractNote(season, {id: 'unknown'}), null);  // never another contract's value
  assert.equal(M.contractNote(season), null);
  assert.equal(M.contractNote({rules: {instrument: ETH}}), '1 контр. ETH-USDT-SWAP = 0,1 ETH');
});
