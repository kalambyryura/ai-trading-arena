'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const Text = require('../web/rule-text.js');
const Waiting = require('../web/bot-waiting.js');

const leaf = (feature, op, value) => ({ feature, op, value });
const opusV1 = { side: 'long', entry: { mode: 'all', conditions: [leaf('return_5m_bps', 'lt', -40), leaf('return_1m_bps', 'gt', 0)] },
  exit: { mode: 'any', conditions: [] }, size_pct: 30, max_entries: 1, add_after_bps: 30, stop_loss_bps: 100, take_profit_bps: 150,
  max_hold_seconds: 10800, cooldown_seconds: 300, max_spread_bps: 2 };
const pause = { side: 'long', size_pct: 5, max_entries: 1, add_after_bps: 30, stop_loss_bps: 50, take_profit_bps: 100, max_hold_seconds: 3600,
  cooldown_seconds: 600, max_spread_bps: 5, entry: { long: { setup: null, confirm: leaf('utc_weekday', 'gt', 6), invalidate: null, expiry_bars: null } },
  exit: null, add_when: null, cost_checkpoint: null, reduce_when: null, reduce_pct: null };
const rich = { side: 'both', size_pct: 20, max_entries: 2, add_after_bps: 30, stop_loss_bps: 120, take_profit_bps: 300, max_hold_seconds: null,
  cooldown_seconds: 90, max_spread_bps: 3,
  entry: {
    long: { setup: { any: [leaf('return_5m_bps', 'lte', -50), { not: leaf('rsi_14', 'gt', 30) }] },
      confirm: { all: [leaf('return_1m_bps', 'gt', 0), { feature: 'ema_gap_bps', op: 'gt', ref: 'sma_gap_bps' }] },
      invalidate: leaf('return_5m_bps', 'lt', -150), expiry_bars: 5 },
    short: { setup: null, confirm: leaf('funding_rate_bps', 'gte', 3), invalidate: null, expiry_bars: null } },
  exit: { any: [leaf('net_gain_bps', 'gte', 80), leaf('position_age_seconds', 'gte', 7200)] },
  add_when: leaf('net_gain_bps', 'gte', 40),
  reduce_when: leaf('gross_gain_bps', 'gte', 150), reduce_pct: 50,
  cost_checkpoint: { after_seconds: 1800, when: { feature: 'net_gain_bps', op: 'lt', ref: 'entry_cost_hurdle_bps' } } };

const section = (described, key) => described.sections.find(s => s.key === key);
const texts = items => items.map(item => item.text);

test('frozen V1 Opus reads as its rules: entry, gates, exits and size', () => {
  const d = Text.describe(opusV1);
  assert.equal(d.schema, 1);
  const entry = section(d, 'entry');
  assert.equal(entry.title, 'Коли входить · LONG (купівля)');
  assert.equal(entry.items[0].kind, 'all');
  assert.deepEqual(texts(entry.items[0].children), ['Зміна ціни за 5 хв < −40 bps (−0,4 %)', 'Зміна ціни за 1 хв > 0 bps (0 %)']);
  assert.deepEqual(texts(section(d, 'gates').items), [
    'Рішення ухвалюються за завершеними хвилинними свічками; невідомі дані блокують вхід.',
    'Спред не більший за 2 bps (0,02 %).',
    'Після закриття угоди чекає щонайменше 5 хв, перш ніж увійти знову.']);
  assert.deepEqual(texts(section(d, 'exit').items), [
    'Стоп: закриває, якщо ціна пішла проти позиції на 1 % від входу.',
    'Ціль: закриває, якщо ціна пішла на користь позиції на 1,5 % від входу.',
    'Час: якщо не спрацювали ні стоп, ні ціль, закриває через 3 год після входу.']);
  assert.deepEqual(texts(section(d, 'size').items), ['На вхід — 30 % бюджету бою.', 'Один вхід у позицію, без докупівлі.']);
  assert.equal(Text.summary(opusV1),
    'LONG · вхід: Зміна ціни за 5 хв < −40 bps (−0,4 %) і Зміна ціни за 1 хв > 0 bps (0 %) · стоп 1 % · ціль 1,5 % · до 3 год');
});

test('the no-entry Pause says its entry condition can never hold', () => {
  const d = Text.describe(pause);
  assert.equal(d.schema, 2);
  const only = section(d, 'entry_long').items[0];
  assert.equal(only.certainty, 'never');
  assert.equal(only.text, 'День тижня UTC (0 — понеділок) > 6 — ніколи не виконується');
  assert.equal(section(d, 'side'), undefined);
  assert.ok(Text.summary(pause).includes('ніколи не виконується'));
});

test('a rich V2 recipe: both sides, setup and confirmation, exits, reduction, checkpoint and adds', () => {
  const d = Text.describe(rich);
  assert.match(section(d, 'side').items[0].text, /лише одну позицію за раз/);
  const long = section(d, 'entry_long').items;
  assert.equal(long[0].text, 'Спершу має статися (setup):');
  assert.equal(long[1].kind, 'any');
  assert.deepEqual(texts(long[1].children), ['Зміна ціни за 5 хв ≤ −50 bps (−0,5 %)', 'RSI за 14 хв ≤ 30']);
  assert.equal(long[2].text, 'Потім протягом 5 наступних хвилинних свічок — підтвердження:');
  assert.deepEqual(texts(long[3].children), ['Зміна ціни за 1 хв > 0 bps (0 %)',
    Waiting.FEATURES.ema_gap_bps.label + ' > ' + Waiting.FEATURES.sma_gap_bps.label]);
  assert.equal(long[4].text, 'Очікування скасовується, якщо:');
  assert.equal(section(d, 'entry_short').items[0].text, 'Ставка funding ≥ 3 bps (0,03 %)');
  const exits = texts(section(d, 'exit').items);
  assert.ok(exits.includes('Ліміту часу утримання немає: позиція закривається стопом, ціллю або умовами нижче.'));
  assert.ok(exits.includes('Достроково закриває, коли:'));
  assert.ok(exits.includes('Скорочує позицію на 50 %, коли:'));
  assert.ok(exits.includes('Через 30 хв після входу один раз перевіряє і закриває позицію, якщо:'));
  const checkpoint = section(d, 'exit').items.at(-1);
  assert.equal(checkpoint.text, 'Прибуток позиції після витрат < Поріг беззбитковості (витрати входу й виходу)');
  const size = texts(section(d, 'size').items);
  assert.equal(size[1], 'Докупівля: до 2 входів у позиції, лише коли позиція вже в плюсі щонайменше на 0,3 % і ще не досягла цілі.');
  assert.equal(size[2], 'Умова докупівлі автора:');
  assert.match(Text.summary(rich), /^LONG або SHORT · вхід: LONG: .+, потім .+; SHORT: Ставка funding ≥ 3 bps \(0,03 %\) · стоп 1,2 % · ціль 3 % · без ліміту часу$/);
});

test('trees: NOT flips a leaf, wraps a group, and a one-member group collapses', () => {
  assert.equal(Text.tree({ not: leaf('return_1m_bps', 'gte', 5) }).text, 'Зміна ціни за 1 хв < 5 bps (0,05 %)');
  const wrapped = Text.tree({ not: { any: [leaf('rsi_14', 'lt', 30), leaf('rsi_14', 'gt', 70)] } });
  assert.equal(wrapped.kind, 'not');
  assert.equal(wrapped.children[0].kind, 'any');
  assert.equal(Text.tree({ all: [leaf('rsi_14', 'lt', 30)] }).kind, 'leaf');
  assert.equal(Text.tree({ not: { all: [leaf('rsi_14', 'lt', 30)] } }).text, 'RSI за 14 хв ≥ 30');
  assert.equal(Text.sentence(Text.tree({ all: [leaf('rsi_14', 'lt', 30), { any: [leaf('return_1m_bps', 'gt', 0), leaf('body_bps', 'gt', 5)] }] })),
    'RSI за 14 хв < 30 і (Зміна ціни за 1 хв > 0 bps (0 %) або Тіло останньої свічки > 5 bps (0,05 %))');
  assert.equal(Text.sentence(wrapped), 'не (RSI за 14 хв < 30 або RSI за 14 хв > 70)');
  assert.equal(Text.tree(leaf('mystery_x', 'gt', 1)).text, 'mystery x > 1');
});

test('never and always only for bounded features, at the exact edges', () => {
  const sure = (feature, op, value) => Text.tree(leaf(feature, op, value)).certainty;
  assert.equal(sure('utc_hour', 'gt', 23), 'never');
  assert.equal(sure('utc_hour', 'gt', 22), null);
  assert.equal(sure('utc_hour', 'gte', 0), 'always');
  assert.equal(sure('utc_hour', 'lt', 0), 'never');
  assert.equal(sure('utc_weekday', 'lte', 6), 'always');
  assert.equal(sure('utc_weekday', 'gte', 7), 'never');
  assert.equal(sure('rsi_14', 'lte', 100), 'always');
  assert.equal(sure('rsi_14', 'lt', 100), null);
  assert.equal(sure('return_5m_bps', 'gt', 1e9), null);
  assert.equal(Text.tree({ feature: 'utc_hour', op: 'gt', ref: 'utc_weekday' }).certainty, null);
});

test('durations and missing rules', () => {
  assert.deepEqual([0, 45, 90, 300, 3600, 5400, 86400].map(Text.duration), ['0 с', '45 с', '1 хв 30 с', '5 хв', '1 год', '1 год 30 хв', '24 год']);
  assert.equal(Text.describe(null), null);
  assert.equal(Text.describe({ side: 'long' }), null);
  assert.equal(Text.summary(undefined), Text.MISSING);
});

function fakeDocument() {
  const make = tag => ({ tag, className: '', textContent: '', children: [], append(...nodes) { this.children.push(...nodes); } });
  return { createElement: make };
}
const walk = node => [node, ...node.children.flatMap(walk)];

test('render builds headings and nested lists through textContent only', () => {
  const card = Text.render(fakeDocument(), Text.describe(rich), { headingLevel: 2 });
  assert.equal(card.tag, 'section');
  const nodes = walk(card);
  assert.equal(nodes.filter(n => n.tag === 'h2').length, Text.describe(rich).sections.length);
  assert.ok(nodes.some(n => n.className === 'rule-text-any' && n.children.some(c => c.tag === 'ul')));
  assert.equal(nodes.at(-1).className, 'rule-text-note');
  assert.equal(nodes.at(-1).textContent, Text.NOTE);
  const never = walk(Text.render(fakeDocument(), Text.describe(pause))).find(n => /never/.test(n.className));
  assert.equal(never.className, 'rule-text-leaf rule-text-never');
  assert.equal(walk(Text.render(fakeDocument(), Text.describe(pause))).filter(n => n.tag === 'h3').length, 4);
  const missing = Text.render(fakeDocument(), null);
  assert.equal(missing.className, 'rule-text-missing');
  assert.equal(missing.textContent, Text.MISSING);
});

test('a schema-4 recipe reads its timeframe features, D106 timing and taker-only exits', () => {
  const v4 = { side: 'long', size_pct: 11, max_entries: 1, add_after_bps: 500, max_spread_bps: 2, stop_loss_bps: 500,
    take_profit_bps: 1000, max_hold_seconds: 86400, cooldown_seconds: 600, order: { type: 'taker' },
    exit: { mode: 'all', conditions: [] },
    entry: { when: { all: [
      { feature: { name: 'return_bps', tf: '1m', n: 60 }, op: 'gte', ref: { name: 'rms_bar_bps', tf: '1h', n: 168 }, mult: 3 },
      { not: { feature: { name: 'sma_gap_bps', tf: '1d', n: 20 }, op: 'lte', value: 0 } }] } } };
  const d = Text.describe(v4);
  assert.equal(d.schema, 4);
  assert.deepEqual(texts(section(d, 'entry').items[0].children),
    ['Зміна ціни за 60 × 1m ≥ 3 × Типовий рух бару 1h (RMS за 168)', 'Відстань ціни від SMA 20 барів 1d > 0 bps (0 %)']);
  assert.match(texts(section(d, 'gates').items)[0], /кожен новий закритий 1m-бар, через 5 с/);
  assert.match(texts(section(d, 'exit').items)[1], /з наступної хвилини після входу/);
  assert.deepEqual(texts(section(d, 'size').items), ['На вхід — 11 % бюджету бою.', 'Один вхід у позицію, без докупівлі.']);
  assert.match(Text.summary(v4), /^LONG · вхід: Зміна ціни за 60 × 1m ≥ 3 × .* і Відстань ціни від SMA 20 барів 1d > 0 bps/);
});

test('schema-4 catalogue: volatility, extremes and clock anchors read in plain words', () => {
  const rules = { side: 'short', size_pct: 10, max_entries: 1, add_after_bps: 500, max_spread_bps: 2, stop_loss_bps: 80,
    take_profit_bps: 120, max_hold_seconds: 3600, cooldown_seconds: 600, exit: { mode: 'all', conditions: [] },
    entry: { when: { all: [
      { feature: { name: 'dist_high_bps', tf: '1d', n: 1 }, op: 'gte', value: 0 },
      { feature: { name: 'us_session_minute', tf: '1m', n: 1 }, op: 'gte', value: 0 },
      { feature: { name: 'return_since_utc_hour_bps', tf: '1h', n: 13 }, op: 'gt', ref: { name: 'realized_vol_bps', tf: '1h', n: 24 }, mult: 2 }] } } };
  const leaves = texts(section(Text.describe(rules), 'entry').items[0].children);
  assert.deepEqual(leaves, ['Відстань ціни до максимуму 1 барів 1d ≥ 0 bps (0 %)',
    'Хвилина сесії США (від 09:30 за Нью-Йорком; −1 поза сесією) ≥ 0',
    'Зміна ціни від 13:00 UTC > 2 × Волатильність (std лог-дохідностей) за 24 × 1h']);
});

test('schema-4 side both: one section per branch and the single-position rule', () => {
  const branch = (op, value) => ({ when: { feature: { name: 'return_bps', tf: '1m', n: 5 }, op, value } });
  const rules = { side: 'both', size_pct: 11, max_entries: 1, add_after_bps: 500, max_spread_bps: 2, stop_loss_bps: 20,
    take_profit_bps: 40, max_hold_seconds: 900, cooldown_seconds: 60, exit: { mode: 'all', conditions: [] },
    entry: { long: branch('lte', -12), short: branch('gte', 12) } };
  const d = Text.describe(rules);
  assert.equal(d.schema, 4);
  assert.deepEqual(d.sections.slice(0, 2).map(s => s.title), ['Коли входить · LONG (купівля)', 'Коли входить · SHORT (продаж)']);
  assert.match(texts(section(d, 'gates').items)[0], /рівно одна гілка/);
  assert.match(Text.summary(rules), /^LONG АБО SHORT|^LONG або SHORT/);
  assert.match(Text.summary(rules), /LONG: Зміна ціни за 5 × 1m ≤ −12 bps.*; SHORT: Зміна ціни за 5 × 1m ≥ 12 bps/);
});

test('the module reads nothing and writes no markup', () => {
  const source = fs.readFileSync(path.join(__dirname, '../web/rule-text.js'), 'utf8');
  for (const forbidden of ['innerHTML', 'outerHTML', 'insertAdjacentHTML', 'fetch(', 'XMLHttpRequest', 'localStorage', 'sessionStorage', 'eval(']) {
    assert.equal(source.includes(forbidden), false, forbidden);
  }
  const hidden = Array.from(source).some(ch => { const c = ch.codePointAt(0); return (c < 32 && c !== 9 && c !== 10 && c !== 13) || (c >= 0x200b && c <= 0x200f) || (c >= 0x2028 && c <= 0x202e) || (c >= 0x2060 && c <= 0x206f) || c === 0xfeff; });
  assert.equal(hidden, false, 'no invisible characters');
});
