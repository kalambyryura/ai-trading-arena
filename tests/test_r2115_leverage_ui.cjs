// R2.1.15 (plan section 8 p. 8): the Live Start form's leverage per bot up to its version's limit, the form's notional
// summary, and the leverage beside a bot in the coliseum details. No browser, server or request; Start is never pressed.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const WEB = path.join(__dirname, '..', 'web');
const fresh = name => {delete require.cache[require.resolve(path.join(WEB, name))]; return require(path.join(WEB, name));};
const Lever = fresh('live-leverage.js');

const SOL = {id: 'sol', name: 'Sol V1', recipe_sha256: 'a'.repeat(64), rules: {size_pct: 30}};
const H10 = {id: 'h10', name: 'H10', recipe_sha256: 'b'.repeat(64), strategy_schema_version: 4, rules: {size_pct: 20}};
const DRAFT = {id: 'draft', name: 'Чернетка', recipe_sha256: 'c'.repeat(64), rules: {size_pct: 10}};
const LIMITS = {[SOL.recipe_sha256]: 10, [H10.recipe_sha256]: 8};

test('the limit comes from the server table; a version it does not list, or a bad entry, is 1x', () => {
  assert.equal(Lever.limit(LIMITS, SOL), 10);
  assert.equal(Lever.limit(LIMITS, H10), 8);
  assert.equal(Lever.limit(LIMITS, DRAFT), 1);
  assert.equal(Lever.limit(undefined, SOL), 1);  // an older server sends no table
  for (const bad of [12, 0, 2.5, '3', null]) assert.equal(Lever.limit({[SOL.recipe_sha256]: bad}, SOL), bad === '3' ? 3 : 1);
});

test('the choice never goes above the limit nor below 1x', () => {
  assert.equal(Lever.choice({}, LIMITS, SOL), 1);
  assert.equal(Lever.choice({sol: 3}, LIMITS, SOL), 3);
  assert.equal(Lever.choice({h10: 9}, LIMITS, H10), 1);  // above H10's 8x: back to 1x, never sent
  assert.equal(Lever.choice({draft: 2}, LIMITS, DRAFT), 1);
  assert.equal(Lever.choice({sol: 0}, LIMITS, SOL), 1);
});

test('the module is display only: no request, clock or randomness', () => {
  const source = require('node:fs').readFileSync(path.join(WEB, 'live-leverage.js'), 'utf8');
  assert.equal(/fetch\s*\(|XMLHttpRequest|\/api\/|Date\.now|Math\.random/.test(source), false);
});

test('the arena page loads the module before arena.js and the panel serves it', () => {
  const fs = require('node:fs');
  const html = fs.readFileSync(path.join(WEB, 'arena.html'), 'utf8');
  assert.ok(html.indexOf('src="/live-leverage.js"') > 0 && html.indexOf('src="/live-leverage.js"') < html.indexOf('src="/arena.js"'));
  assert.ok(fs.readFileSync(path.join(__dirname, '..', 'trading_lab', 'web_panel.py'), 'utf8').includes('"live-leverage.js"'));
});

test('the summary names each bot, its first-entry notional and the owner risk at 3x', () => {
  const text = Lever.summary([SOL, H10], 100, {sol: 3, h10: 3}, LIMITS);
  assert.match(text, /^Плече: Sol V1 3x · вхід до 90 USDT · H10 3x · 1 лот\./);
  assert.match(text, /Сукупний нотіонал перших входів: 90 USDT \+ 1 бот\(и\) схеми 4 по 1 лоту \(G12\)\./);
  assert.match(text, /утричі більші/);
  assert.match(Lever.summary([SOL], 100, {}, LIMITS), /Sol V1 1x · вхід до 30 USDT/);
  assert.equal(Lever.summary([], 100, {}, LIMITS), '');
});

// ---- the coliseum details: «LONG · 3x» only above 1x ----
class Node {
  constructor(tag = 'div') {
    this.tagName = String(tag).toUpperCase(); this.className = ''; this._text = ''; this.children = []; this.attrs = {}; this.dataset = {};
    this.hidden = false; this.parent = null; this.title = ''; this.value = '';
    const self = this;
    this.classList = {add: (...n) => {self.className = [...new Set([...self.className.split(/\s+/).filter(Boolean), ...n])].join(' ');},
      remove: (...n) => {self.className = self.className.split(/\s+/).filter(c => c && !n.includes(c)).join(' ');},
      toggle: (n, f) => {const on = f === undefined ? !self.className.split(/\s+/).includes(n) : !!f; on ? self.classList.add(n) : self.classList.remove(n); return on;},
      contains: n => self.className.split(/\s+/).includes(n)};
    this.style = new Proxy({}, {get: (t, k) => k === 'setProperty' ? ((a, b) => {t[a] = b;}) : k === 'removeProperty' ? (a => {delete t[a];}) : (t[k] ?? ''), set: (t, k, v) => {t[k] = v; return true;}});
    this.clientWidth = 1200; this.offsetWidth = 1200; this.clientHeight = 800; this.width = 1200; this.height = 800;
  }
  get textContent() {return this._text + this.children.map(c => c.textContent).join('');}
  set textContent(v) {this._text = String(v); this.children = [];}
  get firstChild() {return this.children[0] ?? null;}
  get lastChild() {return this.children.at(-1) ?? null;}
  get lastElementChild() {return this.children.at(-1) ?? new Node('span');}
  append(...nodes) {for (const n of nodes) {const node = typeof n === 'string' ? Object.assign(new Node('#text'), {_text: n}) : n; if (node.parent) node.remove(); node.parent = this; this.children.push(node);}}
  prepend(...nodes) {const old = this.children; this.children = []; this.append(...nodes); this.children.push(...old);}
  after() {} before() {}
  replaceChildren(...nodes) {this._text = ''; this.children = []; this.append(...nodes);}
  remove() {if (this.parent) {this.parent.children = this.parent.children.filter(c => c !== this); this.parent = null;}}
  contains(node) {for (let n = node; n; n = n.parent) if (n === this) return true; return false;}
  setAttribute(k, v) {this.attrs[k] = String(v);} getAttribute(k) {return this.attrs[k] ?? null;} removeAttribute(k) {delete this.attrs[k];}
  hasAttribute(k) {return k in this.attrs;} toggleAttribute(k, f) {const on = f === undefined ? !(k in this.attrs) : !!f; if (on) this.attrs[k] = ''; else delete this.attrs[k]; return on;}
  addEventListener() {} removeEventListener() {} focus() {} blur() {} scrollIntoView() {} scrollTo() {} showModal() {} close() {}
  getBoundingClientRect() {return {left: 0, top: 0, width: 1200, height: 800, right: 1200, bottom: 800};}
  querySelector() {return new Node('option');} querySelectorAll() {return [];}
  getContext() {return new Proxy({}, {get: (t, k) => k in t ? t[k] : () => ({addColorStop() {}}), set: (t, k, v) => {t[k] = v; return true;}});}
}
const walk = (node, out = []) => {out.push(node); node.children.forEach(c => walk(c, out)); return out;};
const saved = {};
function sandbox() {
  for (const key of ['document', 'window', 'requestAnimationFrame', 'cancelAnimationFrame', 'ResizeObserver', 'IntersectionObserver']) saved[key] = Object.getOwnPropertyDescriptor(globalThis, key);
  const set = (name, value) => Object.defineProperty(globalThis, name, {value, configurable: true, writable: true});
  const media = () => ({matches: false, addEventListener() {}, removeEventListener() {}});
  set('document', {getElementById: () => new Node(), createElement: tag => new Node(tag), createTextNode: t => Object.assign(new Node('#text'), {_text: String(t)}),
    createElementNS: (_n, tag) => new Node(tag), querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, removeEventListener() {},
    body: new Node('body'), hidden: false, visibilityState: 'visible'});
  set('window', {matchMedia: media, addEventListener() {}});
  set('requestAnimationFrame', () => 1); set('cancelAnimationFrame', () => {});
  for (const name of ['ResizeObserver', 'IntersectionObserver']) set(name, class {observe() {} unobserve() {} disconnect() {}});
}
function restore() {for (const [key, d] of Object.entries(saved)) {if (d) Object.defineProperty(globalThis, key, d); else delete globalThis[key];}}

function direction(extra) {
  const Coliseum = fresh('arena-coliseum.js');
  sandbox();
  window.ArenaReviewModel = fresh('review-model.js');
  const run = {id: 'r'.repeat(32), status: 'RUNNING', mode: 'OKX_LIVE', context: 'arena_live', budget_usdt: 100, rules: {instrument: 'ETH-USDT-SWAP'},
    strategies: [{id: 'h10', name: 'H10', version: 1, author: {kind: 'model', name: 'B'}}], frames: [], events: [],
    latest_market: {at: '2026-11-16T10:00:05Z', mid: 3000},
    bots: [{id: 'h10', strategy_id: 'h10', name: 'H10', position_btc: 0.01, equity_usdt: 100, pnl_usdt: 0, entries: 1, entries_total: 1,
      allocation_usdt: 100, status: 'HOLDING', ...extra}]};
  const host = new Node('section'), view = Coliseum.create(host);
  view.update(run, {key: 'k1'});
  view.inspect('h10');
  const nodes = walk(host), terms = nodes.filter(n => n.tagName === 'DT'), values = nodes.filter(n => n.tagName === 'DD');
  const i = terms.findIndex(t => t.textContent.startsWith('Напрямок'));
  view.destroy();
  return values[i].textContent;
}

test('the coliseum names the leverage beside the direction only above 1x', () => {
  try {
    assert.equal(direction({leverage: 3}), 'LONG · 3x');
    restore();
    assert.equal(direction({}), 'LONG');  // a 1x bot (no key): as before
    restore();
    assert.equal(direction({leverage: 3, position_btc: -0.01}), 'SHORT · 3x');
  } finally {restore();}
});
