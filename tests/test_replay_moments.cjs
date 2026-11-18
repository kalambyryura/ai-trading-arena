// Pure replay checks on the fictional 24 h fixture. No browser, requests, keys, launchers or financial actions.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Labels = require('../web/replay-labels.js');
const Replay = require('../web/event-replay.js');
const Panels = require('../web/research-panels.js');
const Fixture = require('./v8_ui/synthetic-fixture.js');

const copy = value => JSON.parse(JSON.stringify(value));
const start = Fixture.replay.manifest.started_at_ms;

test('moments are the entries, closes, cut-off, data gaps and one settlement, in order', () => {
  const list = Labels.moments(Fixture.replay);
  assert.deepEqual(list.map(m => [m.seq, m.kind, m.importance]), [[7, 'entry', 3], [9, 'quality', 2], [11, 'admission_closed', 2], [13, 'reduce', 2], [14, 'close', 3], [16, 'settlement', 1], [19, 'finished', 1]]);
  assert.deepEqual(list.map(m => m.label), ['Учасник A · вигаданий: Вхід LONG', 'Якість запису: пропуск даних', 'Час нових входів завершено · відкриті позиції тривають',
    'Учасник A · вигаданий: Часткове скорочення LONG', 'Учасник A · вигаданий: Повне закриття LONG · FLAT', 'Звірка рахунків: завершено 3 з 3 учасників', 'Запис завершено']);
  assert.deepEqual(list.map(m => m.at_ms - start), [33000, 61000, 86400000, 86461000, 86500000, 86700000, 90000000]);
  assert.ok(list.every((m, i) => i === 0 || m.seq > list[i - 1].seq));
});

test('a moment is only a pointer into the packet: the packet is untouched and every seq exists', () => {
  const before = JSON.stringify(Fixture.replay);
  const list = Labels.moments(Fixture.replay);
  assert.equal(JSON.stringify(Fixture.replay), before);
  const seqs = new Set(Fixture.replay.events.map(e => e.seq));
  assert.ok(list.every(m => seqs.has(m.seq)));
});

test('checks, intents and plain accounting never become moments; a reversal and an add do', () => {
  const packet = copy(Fixture.replay);
  const fills = packet.events.filter(e => e.type === 'FILL');
  assert.ok(Labels.moments(packet).every(m => !['CHECK', 'INTENT', 'DECISION'].includes(packet.events[m.seq - 1].type)));
  fills[1].financial.position_btc = '0.0004';   // 0.0002 -> 0.0004: an add
  fills[2].financial.position_btc = '-0.0001';  // 0.0004 -> -0.0001: a reversal
  const kinds = Labels.moments(packet).filter(m => m.seq === fills[1].seq || m.seq === fills[2].seq).map(m => m.kind);
  assert.deepEqual(kinds, ['add', 'reversal']);
});

test('a data-quality moment says what the recorder marked and never echoes free text', () => {
  const label = quality => {
    const packet = copy(Fixture.replay);
    packet.events.find(e => e.type === 'QUALITY').quality = quality;
    return Labels.moments(packet).find(m => m.kind === 'quality').label;
  };
  assert.equal(label({ coverage: 'GAP', billing: 'UNKNOWN', reason: 'market_unavailable' }), 'Якість запису: пропуск даних · ринок недоступний');
  assert.equal(label({ coverage: 'GAP', billing: 'UNKNOWN', reason: 'exchange_error' }), 'Якість запису: пропуск даних · помилка біржі');
  assert.equal(label({ coverage: 'GAP', billing: 'UNKNOWN', reason: 'paused' }), 'Якість запису: пропуск даних · пауза');
  assert.equal(label({ coverage: 'GAP', billing: 'UNKNOWN', reason: 'data_gap' }), 'Якість запису: пропуск даних · немає даних');
  assert.equal(label({ coverage: 'GAP', billing: 'UNKNOWN', reason: 'clock_gap' }), 'Якість запису: пропуск даних · розрив годинника');
  assert.equal(label({ coverage: 'GAP', billing: 'KNOWN' }), 'Якість запису: пропуск даних');
  assert.equal(label({ coverage: 'GAP', billing: 'KNOWN', reason: 'free text from somewhere' }), 'Якість запису: пропуск даних');
  assert.equal(label({ coverage: 'COMPLETE', billing: 'KNOWN' }), 'Якість запису: дані повні');
  assert.equal(label({ coverage: 'UNKNOWN', billing: 'UNKNOWN' }), 'Якість запису: стан даних невідомий');
});

test('settlements of every participant collapse into one moment that says how many finished', () => {
  const packet = copy(Fixture.replay);
  packet.events = packet.events.filter(e => e.code !== 'settled_b' && e.code !== 'settled_cash');
  const only = Labels.moments(packet).find(m => m.kind === 'settlement');
  assert.equal(only.label, 'Звірка рахунків: завершено 1 з 3 учасників');
  const none = copy(Fixture.replay);
  none.events = none.events.filter(e => e.type !== 'SETTLEMENT');
  assert.ok(!Labels.moments(none).some(m => m.kind === 'settlement'));
});

test('a packet without any fill still offers its cut-off and its end', () => {
  const packet = copy(Fixture.replay);
  packet.events = packet.events.filter(e => e.type !== 'FILL');
  assert.deepEqual(Labels.moments(packet).map(m => m.kind), ['quality', 'admission_closed', 'settlement', 'finished']);
});

test('next and previous follow the current event, honour the participant filter and stop at the ends', () => {
  const list = Labels.moments(Fixture.replay);
  assert.equal(Labels.nextMoment(list, 0).seq, 7);
  assert.equal(Labels.nextMoment(list, 7).seq, 9);
  assert.equal(Labels.nextMoment(list, 8).seq, 9, 'between moments the next one is still the next');
  assert.equal(Labels.nextMoment(list, 19), null);
  assert.equal(Labels.previousMoment(list, 7), null);
  assert.equal(Labels.previousMoment(list, 14).seq, 13);
  assert.equal(Labels.previousMoment(list, 15).seq, 14, 'one event after a moment, that moment is the previous one');
  assert.equal(Labels.nextMoment(list, 0, 'fixture-b').seq, 9, 'moments of another participant are skipped, global ones kept');
  assert.equal(Labels.previousMoment(list, 19, 'fixture-b').seq, 16);
});

test('the 60 second summary keeps everything when it fits and the most important moments when it does not', () => {
  const list = Labels.moments(Fixture.replay);
  const fits = Labels.digestPlan(list);
  assert.deepEqual([fits.shown, fits.total, fits.dwell_ms], [7, 7, 5000], 'a long dwell is capped');
  const many = Array.from({ length: 200 }, (_, i) => ({ seq: i + 1, at_ms: i * 1000, bot_id: null, kind: 'k', importance: i % 7 === 0 ? 3 : i % 3 === 0 ? 2 : 1, label: 'm' + i }));
  const cut = Labels.digestPlan(many);
  assert.equal(cut.shown, 50);
  assert.equal(cut.dwell_ms, 1200, 'a step never gets shorter than a readable moment');
  assert.ok(cut.steps.every((m, i) => i === 0 || m.seq > cut.steps[i - 1].seq), 'still in time order');
  assert.ok(many.filter(m => m.importance === 3).every(m => cut.steps.includes(m)), 'all the important ones survive');
  assert.deepEqual(Labels.digestPlan(many), cut, 'deterministic');
  const some = Labels.digestPlan(many.slice(0, 20));
  assert.deepEqual([some.shown, some.dwell_ms], [20, 3000]);
  assert.deepEqual(Labels.digestPlan([]), { steps: [], dwell_ms: 0, shown: 0, total: 0 });
});

test('the summary length and the dwell limits are options with the same rules', () => {
  const many = Array.from({ length: 200 }, (_, i) => ({ seq: i + 1, at_ms: i * 1000, bot_id: null, kind: 'k', importance: 1, label: 'm' + i }));
  const short = Labels.digestPlan(many, { seconds: 30, minDwellSeconds: 3, maxDwellSeconds: 4 });
  assert.deepEqual([short.shown, short.dwell_ms, short.total], [10, 3000, 200]);
  const tiny = Labels.digestPlan(many, { seconds: 1 });
  assert.deepEqual([tiny.shown, tiny.dwell_ms], [1, 1200], 'at least one step, and never shorter than readable');
  const two = Labels.digestPlan(many.slice(0, 2), { seconds: 6, maxDwellSeconds: 2.5 });
  assert.equal(two.dwell_ms, 2500);
});

test('1000x is a valid speed: a minute of the viewer is sixteen minutes of the battle', () => {
  assert.deepEqual([...Replay.SPEEDS], [1, 10, 100, 1000]);
  const replay = new Replay.Replay(Fixture.replay);
  replay.setSpeed(1000);
  replay.play();
  replay.tick(1000);
  assert.equal(replay.snapshot().at_ms, start + 1000 * 1000);
  assert.throws(() => replay.setSpeed(500), /1000x/);
});

// ---- the controls, on a minimal fake DOM -----------------------------------------------------------------------
class FakeNode {
  constructor(tag) { this.tag = tag; this.className = ''; this._text = ''; this.writes = 0; this.children = []; this.attrs = {}; this.listeners = {}; this.value = ''; this.disabled = false; this.type = '';
    this.classList = { add: name => { this.className += ' ' + name; } }; }
  get textContent() { return this._text; }
  set textContent(value) { this._text = String(value); this.writes += 1; }   // counted: a live region should not be rewritten for nothing
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  addEventListener(type, handler) { (this.listeners[type] = this.listeners[type] || []).push(handler); }
  fire(type) { for (const handler of this.listeners[type] || []) handler({ target: this }); }
  click() { if (!this.disabled) this.fire('click'); }
}
const walk = (node, found = []) => { found.push(node); node.children.forEach(child => walk(child, found)); return found; };

function mount(withLabels = true) {
  const timers = [];
  global.document = { createElement: tag => new FakeNode(tag), hidden: false };
  global.window = withLabels ? { ArenaReplayLabels: Labels } : {};
  global.requestAnimationFrame = () => 1;
  global.cancelAnimationFrame = () => {};
  global.setTimeout = (fn, ms) => { timers.push({ fn, ms, live: true }); return timers.length; };
  global.clearTimeout = id => { if (timers[id - 1]) timers[id - 1].live = false; };
  const replay = new Replay.Replay(Fixture.replay);
  const host = new FakeNode('div'), frames = [];
  const controls = Panels.mountReplayControls(host, replay, { onFrame: (frame, snapshot) => frames.push(snapshot.state.seq) });
  const all = walk(host);
  const byText = text => all.find(node => node.tag === 'button' && node.textContent === text);
  const note = all.find(node => node.className.includes('research-moment'));
  const speed = all.find(node => node.tag === 'select' && node.attrs['aria-label'] === 'Швидкість повтору');
  const range = all.find(node => node.tag === 'input');
  const runTimers = () => { const live = timers.filter(timer => timer.live); live.forEach(timer => { timer.live = false; timer.fn(); }); return live.length; };
  return { replay, controls, byText, note, speed, range, timers, runTimers, frames, host };
}

test.afterEach(() => { delete global.document; delete global.window; delete global.requestAnimationFrame; delete global.cancelAnimationFrame; });

test('the buttons walk through the moments and say where they are', () => {
  const ui = mount();
  const next = ui.byText('Момент ▶'), previous = ui.byText('◀ Момент');
  assert.ok(next && previous && !next.disabled);
  assert.equal(ui.note.textContent, 'Ключових моментів: 7 — кнопки «Момент» переходять між ними.');
  assert.equal(ui.note.attrs.role, 'status');
  assert.equal(next.attrs['aria-label'], 'Наступний ключовий момент');
  assert.equal(previous.attrs['aria-label'], 'Попередній ключовий момент');
  ui.byText('Відтворити').click();
  assert.equal(ui.replay.snapshot().playing, true);
  next.click();
  assert.equal(ui.replay.snapshot().playing, false, 'a jump pauses a running replay');
  assert.equal(ui.replay.snapshot().state.seq, 7);
  assert.match(ui.note.textContent, /^Учасник A · вигаданий: Вхід LONG · \+33 с від старту · /);
  next.click(); next.click();
  assert.equal(ui.replay.snapshot().state.seq, 11);
  previous.click();
  assert.equal(ui.replay.snapshot().state.seq, 9);
  previous.click(); previous.click();
  assert.equal(ui.note.textContent, 'Раніше ключових моментів немає.');
  assert.equal(ui.replay.snapshot().state.seq, 7, 'the position does not move when there is nowhere to go');
  for (let i = 0; i < 6; i += 1) next.click();                       // 9, 11, 13, 14, 16, 19
  assert.equal(ui.replay.snapshot().state.seq, 19);
  next.click();
  assert.equal(ui.note.textContent, 'Далі ключових моментів немає.');
  assert.equal(ui.replay.snapshot().state.seq, 19, 'and the same at the end');
  ui.controls.destroy();
});

test('the note describes only the position it was written at: elsewhere the hint comes back', () => {
  const ui = mount();
  const hint = ui.note.textContent;
  ui.byText('Момент ▶').click();
  const described = ui.note.textContent;
  assert.notEqual(described, hint);
  ui.speed.value = '10'; ui.speed.fire('change');
  assert.equal(ui.note.textContent, described, 'same position, same note');
  ui.range.value = '90000000'; ui.range.fire('input');              // the end of the recording
  assert.equal(ui.note.textContent, hint, 'a seek elsewhere');
  const writes = ui.note.writes;
  ui.controls.seekEvent(5); ui.controls.seekEvent(12); ui.range.value = '1000'; ui.range.fire('input');
  assert.equal(ui.note.writes, writes, 'the hint is not rewritten at every later move');
  ui.byText('◀ Момент').click();
  assert.notEqual(ui.note.textContent, hint);
  ui.byText('Наступна подія').click();
  assert.equal(ui.note.textContent, hint, 'the next event');
  ui.byText('Момент ▶').click();
  ui.controls.seekEvent(3);
  assert.equal(ui.note.textContent, hint, 'a programmatic seek');
  ui.byText('Підсумок за 60 с').click();
  for (let i = 0; i < 7; i += 1) ui.runTimers();
  assert.match(ui.note.textContent, /^Підсумок завершено/);
  ui.byText('◀ Момент').click();
  assert.match(ui.note.textContent, /^Звірка рахунків: завершено 3 з 3 учасників · /, 'a new jump replaces the closing message');
  ui.controls.destroy();
});

test('the speed selector offers 1000x and applies it', () => {
  const ui = mount();
  const values = ui.speed.children.map(option => option.value);
  assert.deepEqual(values, ['1', '10', '100', '1000']);
  ui.speed.value = '1000';
  ui.speed.fire('change');
  assert.equal(ui.replay.snapshot().speed, 1000);
  ui.controls.destroy();
});

test('the 60 second summary steps through the moments and stops on request, on seeking and on destroy', () => {
  const ui = mount();
  const digest = ui.byText('Підсумок за 60 с');
  digest.click();
  assert.equal(digest.textContent, 'Зупинити підсумок');
  assert.equal(digest.attrs['aria-pressed'], undefined, 'a button whose label changes is not also a pressed-toggle');
  assert.equal(ui.replay.snapshot().state.seq, 7, 'the first moment is shown at once');
  assert.equal(ui.timers.filter(t => t.live).length, 1);
  assert.equal(ui.timers.at(-1).ms, 5000);
  for (const expected of [9, 11, 13, 14, 16, 19]) { assert.equal(ui.runTimers(), 1); assert.equal(ui.replay.snapshot().state.seq, expected); }
  ui.runTimers();
  assert.equal(digest.textContent, 'Підсумок за 60 с');
  assert.equal(ui.note.textContent, 'Підсумок завершено: показано 7 з 7 моментів.');
  assert.equal(ui.timers.filter(t => t.live).length, 0);

  digest.click(); digest.click();                                   // start, then stop by the same button
  assert.equal(ui.timers.filter(t => t.live).length, 0);
  digest.click();
  ui.range.value = '5000'; ui.range.fire('input');                  // seeking by hand takes over
  assert.equal(ui.timers.filter(t => t.live).length, 0);
  assert.equal(digest.textContent, 'Підсумок за 60 с', 'and the label says it can be started again');
  digest.click();
  ui.byText('Наступна подія').click();
  assert.equal(ui.timers.filter(t => t.live).length, 0, 'any manual navigation stops the summary');
  digest.click();
  ui.controls.destroy();
  assert.equal(ui.timers.filter(t => t.live).length, 0, 'destroy leaves no timer behind');
});

test('playing or jumping by hand while the summary runs stops it', () => {
  const ui = mount();
  const digest = ui.byText('Підсумок за 60 с');
  digest.click();
  ui.byText('Відтворити').click();
  assert.equal(ui.timers.filter(t => t.live).length, 0, 'play stops the summary');
  assert.equal(digest.textContent, 'Підсумок за 60 с');
  assert.equal(ui.replay.snapshot().playing, true, 'and starts the ordinary replay');
  digest.click();
  assert.equal(ui.replay.snapshot().playing, false, 'the summary takes over from a running replay');
  ui.byText('Момент ▶').click();
  assert.equal(ui.timers.filter(t => t.live).length, 0, 'a manual jump stops the summary');
  assert.equal(digest.textContent, 'Підсумок за 60 с');
  digest.click();
  ui.byText('◀ Момент').click();
  assert.equal(ui.timers.filter(t => t.live).length, 0, 'in either direction');
  ui.controls.destroy();
});

test('the buttons respect the participant selector: other authors\' moments are skipped, shared ones stay', () => {
  const ui = mount();
  const follow = walk(ui.host).find(node => node.tag === 'select' && node.attrs['aria-label'] === 'Учасник для переходу до події');
  assert.deepEqual(follow.children.map(o => o.value), ['', 'fixture-a', 'fixture-b'], 'baseline Cash is not an option');
  follow.value = 'fixture-b';
  ui.byText('Момент ▶').click();
  assert.equal(ui.replay.snapshot().state.seq, 9, "A's entry at 7 is skipped");
  ui.byText('Момент ▶').click();
  assert.equal(ui.replay.snapshot().state.seq, 11);
  ui.byText('Момент ▶').click();
  assert.equal(ui.replay.snapshot().state.seq, 16, "A's reduce and close are skipped");
  ui.controls.seekEvent(15);
  ui.byText('◀ Момент').click();
  assert.equal(ui.replay.snapshot().state.seq, 11, "going back also skips A's close");
  follow.value = '';
  ui.controls.seekEvent(15);
  ui.byText('◀ Момент').click();
  assert.equal(ui.replay.snapshot().state.seq, 14, 'with everyone selected the close is the previous moment');
  ui.controls.destroy();
});

test('without the labels module the moment controls are disabled and the rest still works', () => {
  const ui = mount(false);
  assert.ok(ui.byText('Момент ▶').disabled && ui.byText('◀ Момент').disabled && ui.byText('Підсумок за 60 с').disabled);
  assert.equal(ui.note.textContent, 'У цьому записі немає ключових моментів.');
  assert.ok(ui.byText('Відтворити'));
  ui.byText('Наступна подія').click();
  assert.equal(ui.replay.snapshot().state.seq > 0, true);
  ui.controls.destroy();
});

test('the moments code only reads the packet: no requests, no clock, no randomness', () => {
  const source = fs.readFileSync(path.join(__dirname, '../web/replay-labels.js'), 'utf8');
  assert.equal(/\bfetch\s*\(|XMLHttpRequest|sendBeacon|WebSocket|EventSource|\/api\/|panel-token|localStorage|Math\.random|Date\.now/.test(source), false);
});
