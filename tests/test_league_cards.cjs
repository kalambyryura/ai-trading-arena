// Battle history on a phone: one card per battle. The table helper marks cells with their column name only when asked.
// Minimal fake DOM, no browser, requests or keys.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

class FakeNode {
  constructor(tag) { this.tag = tag; this.className = ''; this._text = ''; this.children = []; this.attrs = {}; this.listeners = {}; this.dataset = {}; this.value = ''; this.type = '';
    this.classList = { add: name => { this.className = (this.className + ' ' + name).trim(); } }; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this._text = ''; this.children = nodes; }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  addEventListener(type, handler) { (this.listeners[type] = this.listeners[type] || []).push(handler); }
  click() { for (const handler of this.listeners.click || []) handler({ target: this }); }
}
const walk = (node, found = []) => { found.push(node); node.children.forEach(child => walk(child, found)); return found; };

global.document = { createElement: tag => { const made = new FakeNode(tag); if (tag === 'select') made.value = 'all'; return made; } };   // a select starts on its first option
global.Node = FakeNode;
const Panels = require('../web/research-panels.js');

const battles = [
  { id: 'a1', mode: 'OKX_DEMO', status: 'DONE', competitive_validity: 'ELIGIBLE', settlement: 'SETTLED', allocation_usdt: '100', display_label: 'Бій 1 · 24 год', created_at: '2026-10-03T10:00:00Z', cohort: 'c0ffee0123456789' },
  { id: 'b2', mode: 'PAPER', status: 'DRAINING', competitive_validity: 'INELIGIBLE', allocation_usdt: '100', label: 'PAPER-сезон' }];

function history() {
  const host = new FakeNode('div');
  const calls = [];
  Panels.mountLeague(host, { battles, slots: [] }, { onReview: id => calls.push(['review', id]), onReplay: id => calls.push(['replay', id]) });
  const tables = walk(host).filter(node => node.tag === 'table');
  assert.equal(tables.length, 1);
  return { host, table: tables[0], calls };
}

test('the battle history is a card table: every cell carries its column name', () => {
  const { table } = history();
  assert.equal(table.className, 'research-table research-table-cards');
  const headers = walk(table).filter(node => node.tag === 'th').map(node => node.textContent);
  assert.deepEqual(headers, ['Бій · група', 'Режим', 'Стан', 'На учасника', 'У таблиці ліги', 'Дії']);
  const rows = walk(table).filter(node => node.tag === 'tr' && node.children[0]?.tag === 'td');
  assert.equal(rows.length, 2);
  for (const row of rows) assert.deepEqual(row.children.map(cell => cell.dataset.label), headers);
});

test('the card for a battle still shows its name, mode, state and the two actions', () => {
  const { table, calls } = history();
  const [first] = walk(table).filter(node => node.tag === 'tr' && node.children[0]?.tag === 'td');
  assert.match(first.children[0].textContent, /Бій 1 · 24 год/);
  assert.equal(first.children[1].textContent, 'Demo');
  assert.equal(first.children[2].textContent, 'Завершено');
  const buttons = walk(first.children[5]).filter(node => node.tag === 'button');
  assert.deepEqual(buttons.map(node => node.textContent), ['Розбір', 'Повтор']);
  buttons[0].click(); buttons[1].click();
  assert.deepEqual(calls, [['review', 'a1'], ['replay', 'a1']]);
});

test('a table asked for without cards is the plain scrolling table, with no cell labels', () => {
  const plain = Panels.table(['Один', 'Два'], [['a', 'b']]);
  const table = walk(plain).find(node => node.tag === 'table');
  assert.equal(table.className, 'research-table');
  assert.deepEqual(walk(table).filter(node => node.tag === 'td').map(cell => cell.dataset.label), [undefined, undefined]);
  const empty = Panels.table(['Один'], [], { cards: true });
  assert.match(walk(empty).map(node => node.textContent).join(' '), /Записів немає\. Це не нульовий результат\./);
});

test('the phone rules hide only the header row visually and put the label before each value', () => {
  const css = fs.readFileSync(path.join(__dirname, '../web/research-panels.css'), 'utf8');
  const block = css.slice(css.indexOf('research-table-cards'));
  assert.match(block, /@media\(max-width:640px\)/);
  assert.match(block, /\.research-table-cards thead\{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset\(50%\)\}/, 'the header stays for screen readers');
  assert.match(block, /\.research-table-cards td::before\{content:attr\(data-label\)/);
  assert.match(block, /\.research-table\.research-table-cards\{min-width:0\}/, 'the 500px minimum of the scrolling table must not apply to cards');
  assert.equal(/display:none/.test(block), false, 'display:none would hide the header from assistive technology');
});
