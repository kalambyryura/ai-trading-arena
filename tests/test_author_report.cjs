// Pure report checks. No browser, requests, keys, launchers or financial actions.
const test = require('node:test');
const assert = require('node:assert/strict');
const childProcess = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const report = require('../web/author-report.js');
const contextApi = require('../web/battle-context.js');

const fixture = () => JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'review_bundle_paper.json'), 'utf8'));
const section = (markdown, title) => {
  const start = markdown.indexOf(title);
  assert.ok(start >= 0, `missing ${title}`);
  const next = markdown.indexOf('\n## ', start + title.length);
  return markdown.slice(start, next < 0 ? undefined : next);
};

test('the report has every section, in order, with the facts of the battle in the header', () => {
  const { markdown, version } = report.build(fixture(), 'sample-trend');
  assert.equal(version, 'author-report/v1');
  const titles = ['## 1. Підсумок', '## 2. Хто як зіграв', '## 3. Контекст ринку', '## 4. Воронка рішень', '## 5. Угоди', '## 6. Витрати', '## 7. Чого не знаємо', '## 8. Цілісність'];
  const at = titles.map(title => markdown.indexOf(title));
  assert.ok(at.every(index => index > 0) && at.every((index, i) => i === 0 || index > at[i - 1]), String(at));
  assert.match(markdown.split('\n')[0], /^# Звіт автору · За трендом$/);
  assert.match(markdown, /Бій `f33670bf0d384e0fa2c5f2d2414e07d5` · PAPER \(модель\) · старт 03\.10\.2026 12:09:43 Київ \(UTC 2026-10-03T09:09:43\.176\+00:00\) · тривалість 5 хв · статус DONE/);
  assert.match(markdown, /Версія рецепта: 1 · відбиток `770f52d1001e5af674997a37907940fd1086d6237e271fa3f9b9a983d84ea4bd`/);
  assert.match(section(markdown, '## 1. Підсумок'), /Зробив 1 угоду\. Результат: \+0,0558 USDT \(Cash — 0\)\./);
  assert.match(markdown, /Контрольна сума пакета: `d6ea0987/);
});

test('times are printed in a fixed zone, so the text does not depend on the machine', () => {
  const here = report.build(fixture(), 'sample-trend').markdown;
  const script = `const r=require(${JSON.stringify(path.join(__dirname, '../web/author-report.js'))});`
    + `const b=JSON.parse(require('fs').readFileSync(${JSON.stringify(path.join(__dirname, 'fixtures', 'review_bundle_paper.json'))},'utf8'));`
    + `process.stdout.write(r.build(b,'sample-trend').markdown)`;
  const there = childProcess.execFileSync(process.execPath, ['-e', script], { env: { ...process.env, TZ: 'America/Los_Angeles' }, encoding: 'utf8' });
  assert.equal(there, here);
  assert.equal(report.build(fixture(), 'sample-trend').markdown, here, 'and it is repeatable');
});

test('the field table shows everyone, marks this report, and equal results share a place', () => {
  const quiet = report.build(fixture(), 'sample-rebound').markdown;
  const rows = section(quiet, '## 2. Хто як зіграв').split('\n').filter(line => line.startsWith('| ') && !line.startsWith('| ---') && !line.startsWith('| Учасник'));
  assert.deepEqual(rows.map(line => line.split(' | ')[0].replace('| ', '')), ['За трендом', 'Після відкату ← цей звіт', 'Слабкий ринок', 'Cash']);
  const places = Object.fromEntries(rows.map(line => { const cells = line.split(' | '); return [cells[0].replace('| ', '').replace(' ← цей звіт', ''), cells[3].replace(' |', '')]; }));
  assert.deepEqual(places, { 'За трендом': '2', 'Після відкату': '3', 'Слабкий ринок': '1', Cash: '3' });
  assert.match(section(quiet, '## 1. Підсумок'), /Місце серед ранжованих: 3 з 4 \(Cash входить до рейтингу\); ділить місце з: Cash\. Переміг Слабкий ринок/);
  assert.match(section(quiet, '## 2. Хто як зіграв'), /Чужі рецепти тут не показано/);
  assert.doesNotMatch(quiet, /sma_gap_bps lt|stop_loss_bps|take_profit_bps/, 'no rival recipe details');
});

test('trades come from the order ledger with prices, costs, hold time and the exit reason', () => {
  const { markdown, data } = report.build(fixture(), 'sample-trend');
  const row = section(markdown, '## 5. Угоди').split('\n').find(line => line.startsWith('| 1 ')).replace(/ /g, ' ');  // numbers use a no-break space as the thousands separator
  assert.equal(row, '| 1 | 03.10.2026 12:09:48 Київ | 84 058,86 | 03.10.2026 12:11:48 Київ | 84 422,35 | +0,072698 | −0,016848 | 2 хв | формальний вихід за правилами |');
  assert.equal(data.trades.length, 1);
  assert.equal(data.trades[0].exit_reason, 'rules_exit');
  assert.equal(data.trades[0].held_seconds, 120);
  assert.equal(typeof data.trades[0].gross_usdt, 'string');
});

test('the exit reason is the last trigger before the close fill, and a trigger outside the trade is ignored', () => {
  const bundle = fixture();
  bundle.events.push({ seq: 100, bot_id: 'sample-trend', code: 'exit_trigger', data: { exit_reason: 'stop_loss' } },
    { seq: 5, bot_id: 'sample-trend', code: 'exit_trigger', data: { exit_reason: 'take_profit' } },
    { seq: 270, bot_id: 'sample-short', code: 'exit_trigger', data: { exit_reason: 'max_hold' } },
    { seq: 139, bot_id: 'sample-short', code: 'exit_trigger', data: { exit_reason: 'stop_loss' } });
  const orphan = fixture();   // this trade has no trigger of its own: an older one must not be borrowed
  orphan.events = orphan.events.filter(e => !(e.bot_id === 'sample-trend' && e.code === 'exit_trigger'));
  orphan.events.push({ seq: 5, bot_id: 'sample-trend', code: 'exit_trigger', data: { exit_reason: 'take_profit' } });
  const trade = report.build(orphan, 'sample-trend');
  assert.equal(trade.data.trades[0].exit_reason, null);
  assert.match(trade.markdown, /\| 2 хв \| не записано \|/);
  assert.equal(report.build(bundle, 'sample-trend').data.trades[0].exit_reason, 'rules_exit', 'seq 127 is the last one before the close fill at 138');
  assert.equal(report.build(bundle, 'sample-short').data.trades[0].exit_reason, 'max_hold', 'seq 270 beats 267 and 139, and a trigger before the entry (139 < 140) is outside the trade');
});

test('the market context is a section of its own: described when given, honestly empty when not', () => {
  const frames = [84000, 84210, 83790, 84168].map((mid, i) => ({ at: new Date(Date.parse('2026-10-03T09:09:43Z') + i * 60000).toISOString(), market: { mid } }));
  const withContext = report.build(fixture(), 'sample-trend', { context: contextApi.fromFrames(frames, { fundingRate: 0.0001 }) });
  const text = section(withContext.markdown, '## 3. Контекст ринку').replace(/ /g, ' ');
  assert.match(text, /BTC за бій: 84 000 → 84 168 \(\+0,2 %\)/);
  assert.match(text, /Контекст, не причина результату\./);
  assert.equal(withContext.data.context.known, true);
  assert.equal(withContext.data.context.direction, 'up');
  const without = report.build(fixture(), 'sample-trend');
  assert.match(section(without.markdown, '## 3. Контекст ринку'), /Рух ціни за бій не записано\. Це не означає, що ринок стояв\./);
  assert.equal(without.data.context, null);
  const sparse = report.build(fixture(), 'sample-trend', { context: contextApi.fromFrames(frames.slice(0, 2)) });
  assert.match(section(sparse.markdown, '## 3. Контекст ринку'), /у пакеті мало кадрів із ціною/);
  assert.equal(without.markdown.split(String.fromCharCode(10)).filter(line => line.startsWith('## ')).length, 8, 'the same eight sections for everyone');
});

test('a participant that never traded gets the conditions that kept the door shut, with how far they were', () => {
  const { markdown, data } = report.build(fixture(), 'sample-rebound');
  assert.match(section(markdown, '## 5. Угоди'), /Угод не було\./);
  assert.match(markdown, /Не торгував: умови входу не виконувались — з 10 перевірок жодна не дала сигналу/);
  const misses = section(markdown, '## 4. Воронка рішень');
  assert.match(misses, /\| rsi_14 < 35 \| 8 \| 80 % \| 39,4765 … 79,9248 \| 39,4765 \|/);
  assert.match(misses, /\| return_1m_bps > 0 \| 6 \| 60 % \| -4,95 … -1,054 \| -1,054 \|/);
  assert.equal(data.conditionMisses[0].feature, 'rsi_14');
  assert.ok(Math.abs(data.conditionMisses[0].nearest - 39.4765) < 1e-4);
  assert.equal(data.participant.whyNoTrades.code, 'no_signal');
});

test('the JSON side keeps exact money strings and the ranking', () => {
  const { data } = report.build(fixture(), 'sample-short');
  assert.equal(data.participant.net_usdt, '0.157956245570611749');
  assert.equal(data.participant.rank, 1);
  assert.deepEqual(data.ranking.slice(0, 2), ['sample-short', 'sample-trend']);
  assert.equal(data.cash.net_usdt, '0');
  assert.equal(data.field.length, 3);
});

test('an unknown participant is an error, not an empty report', () => {
  assert.throws(() => report.build(fixture(), 'nobody'), /Учасника немає в цьому бою/);
  assert.throws(() => report.build({}, 'x'), /Учасника немає/);
});

test('an incomplete battle says so, shows the known loss and does not rank', () => {
  const bundle = fixture();
  bundle.match.incomplete = true;
  const { markdown, data } = report.build(bundle, 'sample-trend');
  assert.match(section(markdown, '## 1. Підсумок'), /Запис раунду неповний, тому учасника не ранжовано\. Відомий результат: \+0,0558 USDT\./);
  assert.match(section(markdown, '## 1. Підсумок'), /Місце серед ранжованих: не ранжовано/);
  assert.match(markdown, /статус DONE · запис неповний/);
  assert.equal(data.participant.rank, null);
  assert.deepEqual(data.excluded.map(item => item.reason), ['incomplete', 'incomplete', 'incomplete']);
});

test('Jev answers are summarised with the gate threshold and the reasons for refusals', () => {
  const bundle = fixture();
  const mine = bundle.participants.find(p => p.id === 'sample-rebound');
  Object.assign(mine.funnel.entry, { signals: 3, jev_decisions: 3, jev_vetoes: 3 });
  bundle.events.push(...[0.61, 0.66, 0.9].map((confidence, i) => ({ seq: 400 + i, bot_id: 'sample-rebound', code: 'jev_decision',
    data: { answer: { choice: i === 2 ? 'hold' : 'proceed', confidence: String(confidence) }, jev_allowed: false, blocked_reason: i === 2 ? 'choice_hold' : 'low_confidence' } })));
  const { markdown, data } = report.build(bundle, 'sample-rebound');
  assert.match(markdown, /### Jev\n\nВідповідей: 3; дозволів: 0; відмов: 3; без вердикту виконання: 0\. Confidence: 0,61 … 0,9; поріг арени: 0,7\. Причини відмов: нижче порога — 2, «утримати» — 1, без придатної відповіді — 0\./);
  assert.match(markdown, /confidence Jev не є прогнозом ціни/);
  assert.equal(data.jev.decisions, 3);
});

test('table cells survive pipes and line breaks in names', () => {
  const bundle = fixture();
  bundle.participants[0].name = 'A | B\nC';
  const { markdown } = report.build(bundle, 'sample-rebound');
  assert.ok(markdown.includes('| A \\| B C |'), 'the name is escaped and flattened');
});

test('the report states facts: no advice wording is ever generated', () => {
  const texts = ['sample-trend', 'sample-rebound', 'sample-short'].map(id => report.build(fixture(), id).markdown.toLowerCase());
  for (const text of texts) {
    for (const word of ['рекомендуємо', 'радимо', 'варто', 'слід ', 'спробуйте', 'змініть', 'збільшіть', 'зменшіть']) {
      assert.ok(!text.includes(word), `advice marker «${word}»`);
    }
  }
});

test('the module only formats data: no requests, no DOM, no owner endpoints', () => {
  const source = fs.readFileSync(path.join(__dirname, '../web/author-report.js'), 'utf8');
  assert.equal(/\bfetch\s*\(|XMLHttpRequest|sendBeacon|WebSocket|EventSource|\/api\/|panel-token|localStorage|document\./.test(source), false);
  assert.equal(/Date\.now|Math\.random|new Date\(\)/.test(source), false, 'nothing that changes between two runs');
});

test('the arena page loads it after the facts model and before the page script, and degrades without it', () => {
  const html = fs.readFileSync(path.join(__dirname, '../web/arena.html'), 'utf8');
  const page = fs.readFileSync(path.join(__dirname, '../web/arena.js'), 'utf8');
  assert.ok(html.includes('<script src="/author-report.js" defer></script>'));
  assert.ok(html.includes('<script src="/battle-context.js" defer></script>'));
  assert.ok(html.indexOf('/battle-facts.js') < html.indexOf('/battle-context.js') && html.indexOf('/battle-context.js') < html.indexOf('/author-report.js') && html.indexOf('/author-report.js') < html.indexOf('/arena.js'));
  assert.ok(page.includes('window.ArenaAuthorReport'), 'the dialog uses it');
  assert.ok(/window\.ArenaAuthorReport\s*&&/.test(page) || page.includes('window.ArenaAuthorReport?.'), 'and tolerates a server that does not serve it');
});
