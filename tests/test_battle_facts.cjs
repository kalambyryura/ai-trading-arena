// Pure fact/wording checks. No browser, requests, keys, launchers or financial actions.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const facts = require('../web/battle-facts.js');

const entry = (extra = {}) => ({ checks: 0, signals: 0, signals_unknown: 0, failed_checks: 0, unknown_checks: 0, jev_decisions: 0, jev_calls: 0,
  jev_allows: 0, jev_holds: 0, jev_vetoes: 0, jev_outcomes_unknown: 0, rechecks: 0, recheck_vetoes: 0, ...extra });
const funnel = (entryExtra = {}, extra = {}) => ({ entry: entry(entryExtra), entry_filled_orders: 0, round_trips: 0, order_intents: 0, no_signal_unknown: 0, ...extra });
const costs = (extra = {}) => ({ gross_pnl_usdt: '0', signed_fees_usdt: '0', signed_funding_usdt: '0', net_usdt: '0', complete: true,
  jev: { known_cost_usd: '0.0001', unknown_billing_requests: 0, complete: true }, ...extra });
const lifecycle = (f, c = costs(), extra = {}) => ({ bot_id: 'b1', complete: true, funnel: f, costs: c, ...extra });
const event = (code, data, botId = 'b1') => ({ bot_id: botId, code, data });
const run = (extra = {}) => ({ status: 'DONE', incomplete: false, history_complete: true, ...extra });
const make = (over = {}) => facts.factsForBot({ bot: { id: 'b1', name: 'S04' }, lifecycleBot: lifecycle(funnel()), events: [], run: run(), ...over });
const freeJev = { jev: { known_cost_usd: '0', unknown_billing_requests: 0, complete: true } };

class Node {
  constructor(tag) { this.tag = tag; this.className = ''; this.textContent = ''; this.children = []; }
  append(...nodes) { this.children.push(...nodes); }
}
const doc = { createElement: tag => new Node(tag) };
const allText = node => [node.textContent, ...node.children.map(allText)].join(' ');

// ---- exact money ---------------------------------------------------------------------------------------------
test('decimal strings stay exact and compare without floating point', () => {
  assert.equal(facts.decimalText('1E-7'), '0.0000001');
  assert.equal(facts.decimalText(1e-7), '0.0000001');
  assert.equal(facts.decimalText(-0.01682001), '-0.01682001');
  assert.equal(facts.decimalText('-0.016820010'), '-0.01682001');
  assert.equal(facts.decimalText('0.000'), '0');
  assert.equal(facts.decimalText('+5'), '5');
  for (const bad of [null, undefined, '', 'abc', NaN, Infinity, true]) assert.equal(facts.decimalText(bad), null, String(bad));
  assert.equal(facts.compareDecimal('0.1', '0.10'), 0);
  assert.equal(facts.compareDecimal('-0.000000000000000001', '0'), -1, '18 decimals still count');
  assert.equal(facts.compareDecimal('12345678901234567890.5', '12345678901234567890.4'), 1, 'beyond double precision');
  assert.equal(facts.compareDecimal('0.3', '0.30000000000000004'), -1, 'the classic float sum is not equal to 0.3');
  assert.equal(facts.compareDecimal('x', '0'), null);
});

// ---- one participant -----------------------------------------------------------------------------------------
test('no observed checks is "unknown", never "did not trade"', () => {
  const none = make({ lifecycleBot: null });
  assert.equal(none.outcome, 'unknown');
  assert.match(facts.headline(none), /не записано/);
  assert.ok(none.unknown.includes('воронка рішень'));
  assert.ok(facts.rows(none).some(row => row.label === 'Не записано'), 'the gap is shown, not hidden');
  assert.equal(none.eligible, false);
  const silent = make({ lifecycleBot: lifecycle(funnel({ checks: 0 })) });
  assert.equal(silent.outcome, 'unknown');
  const unsure = make({ lifecycleBot: lifecycle(funnel({ checks: 40, signals_unknown: 3 })) });
  assert.equal(unsure.outcome, 'unknown', 'an unknown signal outcome could hide an entry');
  const unsureJev = make({ lifecycleBot: lifecycle(funnel({ checks: 40, signals: 2, jev_decisions: 2, jev_outcomes_unknown: 1 })) });
  assert.equal(unsureJev.outcome, 'unknown');
  const hidden = make({ lifecycleBot: lifecycle(funnel({ checks: 40 }, { order_intents: 1 })) });
  assert.equal(hidden.outcome, 'unknown', 'an order intent without a recorded fill could hide an entry');
});

test('entry fills prove trading even when the rest of the evidence is incomplete', () => {
  const traded = make({ lifecycleBot: lifecycle(funnel({ checks: 5, signals: 1 }, { entry_filled_orders: 1, round_trips: 1 }), costs({ net_usdt: '-0.01682', gross_pnl_usdt: '-0.00002', signed_fees_usdt: '-0.0168' })) });
  assert.equal(traded.outcome, 'traded');
  assert.equal(traded.vsCash, 'below');
  assert.equal(traded.net_usdt, '-0.01682');
  assert.equal(facts.headline(traded), 'Зробив 1 угоду. Результат: −0,0168 USDT (Cash — 0).');
  const viaBot = facts.factsForBot({ bot: { id: 'b1', entries: 2, pnl_usdt: '0.5' }, lifecycleBot: null, events: [], run: run() });
  assert.equal(viaBot.outcome, 'traded');
  assert.equal(viaBot.vsCash, 'above');
  assert.equal(viaBot.net_usdt, '0.5');
});

test('a running battle speaks in the present tense', () => {
  const traded = make({ run: run({ status: 'RUNNING' }), lifecycleBot: lifecycle(funnel({ checks: 5, signals: 1 }, { entry_filled_orders: 1, round_trips: 0 }), costs({ net_usdt: '0.02' })) });
  assert.equal(traded.final, false);
  assert.equal(facts.headline(traded), 'Відкрив позицію (входів: 1), закритих угод ще немає. Поточний PnL: +0,02 USDT (Cash — 0).');
  const waiting = make({ run: run({ status: 'RUNNING' }), lifecycleBot: lifecycle(funnel({ checks: 12 })) });
  assert.match(facts.headline(waiting), /^Поки не торгував/);
  assert.equal(make({ run: run({ incomplete: true }) }).final, false, 'an incomplete DONE run is not final');
});

test('an incomplete record is labelled incomplete, keeps its numbers and is never eligible', () => {
  const f = make({ run: run({ incomplete: true }), lifecycleBot: lifecycle(funnel({ checks: 5, signals: 1 }, { entry_filled_orders: 1, round_trips: 1 }), costs({ net_usdt: '-0.3' })) });
  assert.equal(f.outcome, 'incomplete');
  assert.equal(f.eligible, false);
  assert.equal(f.net_usdt, '-0.3');
  assert.equal(facts.headline(f), 'Запис раунду неповний, тому учасника не ранжовано. Відомий результат: −0,3 USDT.');
  assert.ok(f.unknown.includes('повнота журналу'));
  assert.equal(make({ run: run({ incomplete: true }), lifecycleBot: null }).outcome, 'incomplete');
});

test('the sentence uses the same PnL as the roster, the order-based net stays in the costs row', () => {
  const f = make({ bot: { id: 'b1', pnl_usdt: '-0.01682001' }, lifecycleBot: lifecycle(funnel({ checks: 2, signals: 1 }, { entry_filled_orders: 1, round_trips: 1 }), costs({ net_usdt: '-0.0084', signed_fees_usdt: '-0.0084' })) });
  assert.equal(facts.headline(f), 'Зробив 1 угоду. Результат: −0,0168 USDT (Cash — 0).');
  assert.match(facts.rows(f).find(r => r.label === 'Витрати').text, /net за ордерами −0,0084 USDT$/);
  const closedWithoutTrip = make({ lifecycleBot: lifecycle(funnel({ checks: 2, signals: 1 }, { entry_filled_orders: 2, round_trips: 0 }), costs({ net_usdt: '-0.5' })) });
  assert.equal(facts.headline(closedWithoutTrip), 'Відкрив позицію (входів: 2), але закритої угоди не зафіксовано. Результат: −0,5 USDT (Cash — 0).');
});

test('no signal: names the condition that failed most often', () => {
  const events = [];
  for (let i = 0; i < 10; i += 1) {
    events.push(event('entry_checks', { local_checks_passed: false, checks: [
      { feature: 'return_5m_bps', passed: i < 9 ? false : true }, { feature: 'rsi_14', passed: i % 2 === 0 ? false : true }, { feature: 'spread_bps', passed: true }] }));
  }
  events.push(event('entry_checks', { checks: [{ feature: 'return_5m_bps', passed: false }] }, 'someone-else'));
  const f = make({ lifecycleBot: lifecycle(funnel({ checks: 10 })), events });
  assert.equal(f.outcome, 'no_trade');
  assert.equal(f.net_usdt, '0');
  assert.equal(f.whyNoTrades.code, 'no_signal');
  assert.deepEqual(f.whyNoTrades.evidence.failed.map(row => [row.feature, row.count]), [['return_5m_bps', 9], ['rsi_14', 5]]);
  assert.equal(f.whyNoTrades.evidence.failedChecks, 10, 'other bots are not counted');
  const sentence = 'Не торгував: умови входу не виконувались — з 10 перевірок жодна не дала сигналу. Найчастіше не виконувалась умова «return_5m_bps» (у 9 з 10 перевірок).';
  assert.equal(facts.headline(f), sentence);
  assert.equal(f.whyNoTrades.text, sentence);
});

test('no signal without per-check evidence still says what is known and nothing more', () => {
  const f = make({ lifecycleBot: lifecycle(funnel({ checks: 1412 })) });
  assert.equal(f.whyNoTrades.code, 'no_signal');
  assert.deepEqual(f.whyNoTrades.evidence.failed, []);
  assert.doesNotMatch(facts.headline(f), /Найчастіше/);
});

test('Jev declined: the code gate, not a market forecast, kept the entry shut', () => {
  const events = [0.6, 0.62, 0.63, 0.63, 0.65].map(confidence => event('jev_decision', { answer: { choice: 'proceed', confidence }, jev_allowed: false, ai_used: true }));
  const f = make({ lifecycleBot: lifecycle(funnel({ checks: 5, signals: 5, jev_decisions: 5, jev_calls: 5, jev_vetoes: 5 })), events });
  assert.equal(f.whyNoTrades.code, 'jev_declined');
  const jev = f.whyNoTrades.evidence.jev;
  assert.deepEqual([jev.min, jev.max, jev.threshold], [0.6, 0.65, 0.7]);
  assert.deepEqual(jev.reasons, { low_confidence: 5, choice_hold: 0, other: 0 }, 'a confidence below the gate is inferred when an older journal has no reason');
  assert.equal(facts.headline(f), 'Не торгував: правила дали 5 сигналів, але перевірка Jev жодного разу не набрала порога 0,7 (confidence 0,6–0,65). Вхід блокує код арени за порогом; це не прогноз ринку.');
});

test('Jev refusals are told apart: below the gate, said hold, no usable answer', () => {
  const declined = (reason, confidence, extra = {}) => event('jev_decision', { answer: confidence === null ? null : { choice: 'hold', confidence }, jev_allowed: false, blocked_reason: reason, confidence_threshold: 0.75, ...extra });
  const events = [declined('low_confidence', 0.6), declined('low_confidence', 0.7), declined('choice_hold', 0.9), declined('timeout', null)];
  const f = make({ lifecycleBot: lifecycle(funnel({ checks: 4, signals: 4, jev_decisions: 4, jev_vetoes: 4 })), events });
  assert.deepEqual(f.whyNoTrades.evidence.jev.reasons, { low_confidence: 2, choice_hold: 1, other: 1 });
  assert.equal(f.whyNoTrades.evidence.jev.threshold, 0.75, 'the threshold the engine used, not a hard-coded one');
  assert.equal(facts.headline(f), 'Не торгував: правила дали 4 сигнали, але вхід не відбувся — 2 рази confidence нижче порога 0,75 (confidence 0,6–0,9); 1 раз Jev відповів «утримати»; 1 раз Jev не дав придатної відповіді. Рішення про вхід ухвалює код арени, це не прогноз ринку.');
  const onlyHold = make({ lifecycleBot: lifecycle(funnel({ checks: 1, signals: 1, jev_decisions: 1, jev_vetoes: 1 })), events: [declined('choice_hold', 0.9)] });
  assert.match(facts.headline(onlyHold), /1 раз Jev відповів «утримати»/);
  assert.doesNotMatch(facts.headline(onlyHold), /жодного разу не набрала/);
});

// ---- PAPER journals write the shared gate as "jev_gate" ------------------------------------------------------
// arena.py emits jev_gate {allowed, attempted, answer, reason}; the exchange engine emits jev_decision {jev_allowed, ai_used, blocked_reason}.
const gate = (allowed, answer = null, extra = {}, botId = 'b1') => event('jev_gate', { allowed, attempted: answer !== null, reason: answer ? 'typed_answer' : 'local_hold', answer, ...extra }, botId);

test('a PAPER jev_gate is counted exactly like the exchange jev_decision it stands for', () => {
  const paper = [gate(true, { choice: 'proceed', confidence: 0.9 }), gate(false, { choice: 'proceed', confidence: 0.6 }), gate(false, { choice: 'hold', confidence: 0.9 }),
    gate(false), gate(undefined, { choice: 'proceed', confidence: 0.8 })];
  const exchange = [event('jev_decision', { answer: { choice: 'proceed', confidence: 0.9 }, jev_allowed: true, ai_used: true }),
    event('jev_decision', { answer: { choice: 'proceed', confidence: 0.6 }, jev_allowed: false, ai_used: true }),
    event('jev_decision', { answer: { choice: 'hold', confidence: 0.9 }, jev_allowed: false, ai_used: true, blocked_reason: 'choice_hold' }),
    event('jev_decision', { answer: null, jev_allowed: false, ai_used: false }),
    event('jev_decision', { answer: { choice: 'proceed', confidence: 0.8 }, ai_used: true })];
  const counted = facts.jevAnswers(paper);
  assert.deepEqual(counted, facts.jevAnswers(exchange));
  assert.deepEqual([counted.decisions, counted.allowed, counted.blocked, counted.unknown], [5, 1, 3, 1], 'a gate without a verdict is unknown, never allowed');
  assert.deepEqual(counted.reasons, { low_confidence: 1, choice_hold: 1, other: 1 });
  assert.deepEqual([counted.min, counted.max, counted.threshold], [0.6, 0.9, null]);
});

test('a PAPER gate that was never asked is a refusal without a usable answer', () => {
  const counted = facts.jevAnswers([gate(false, null, { reason: 'budget_or_unknown_billing' })]);
  assert.deepEqual([counted.decisions, counted.allowed, counted.blocked, counted.unknown], [1, 0, 1, 0]);
  assert.deepEqual(counted.reasons, { low_confidence: 0, choice_hold: 0, other: 1 });
  assert.deepEqual([counted.min, counted.max], [null, null]);
});

test('a PAPER gate keeps what the engine wrote and invents nothing else', () => {
  const explicit = facts.jevAnswers([gate(false, { choice: 'hold', confidence: 0.95 }, { blocked_reason: 'low_confidence', confidence_threshold: 0.8 })]);
  assert.deepEqual(explicit.reasons, { low_confidence: 1, choice_hold: 0, other: 0 }, 'an explicit reason wins over the hold inference');
  assert.equal(explicit.threshold, 0.8);
  const allowedHold = facts.jevAnswers([gate(true, { choice: 'hold', confidence: 0.9 })]);
  assert.deepEqual([allowedHold.allowed, allowedHold.blocked, allowedHold.reasons.choice_hold], [1, 0, 0], 'a hold answer is a choice_hold only when the gate refused');
  const stringVerdict = facts.jevAnswers([gate('false', { choice: 'hold', confidence: 0.9 }), gate(1, { choice: 'proceed', confidence: 0.9 }), gate(null, { choice: 'proceed', confidence: 0.9 })]);
  assert.deepEqual([stringVerdict.allowed, stringVerdict.blocked, stringVerdict.unknown], [0, 0, 3], 'only a real boolean is a verdict');
  const own = event('jev_gate', { allowed: true, jev_allowed: false, answer: null });
  assert.deepEqual([facts.jevAnswers([own]).allowed, facts.jevAnswers([own]).blocked], [1, 0], 'the PAPER verdict is allowed, as in cycle_diagnostics');
});

test('both journal shapes in one list are counted once each, and other events are not decisions', () => {
  const mixed = [event('jev_decision', { answer: { choice: 'proceed', confidence: 0.9 }, jev_allowed: true }), gate(true, { choice: 'proceed', confidence: 0.91 }),
    event('jev_request', { allowed: true }), event('jev_pending', {}), event('entry_checks', {}), event('jev_gate'), event('jev_decision'), null, undefined, 'jev_gate', 5, [], {}];
  const counted = facts.jevAnswers(mixed);
  assert.deepEqual([counted.decisions, counted.allowed, counted.blocked, counted.unknown], [4, 2, 0, 2], 'a gate or decision without data is one unknown answer');
  assert.deepEqual(facts.jevAnswers([]), { decisions: 0, allowed: 0, blocked: 0, unknown: 0, reasons: { low_confidence: 0, choice_hold: 0, other: 0 }, threshold: null, min: null, max: null });
});

test('a PAPER battle on the arena page explains why Jev kept the door shut', () => {
  const events = [gate(false, { choice: 'hold', confidence: 0.9 }), gate(false, { choice: 'proceed', confidence: 0.62 }), gate(false, { choice: 'hold', confidence: 0.9 }, {}, 'other-bot')];
  const f = make({ lifecycleBot: lifecycle(funnel({ checks: 2, signals: 2, jev_decisions: 2, jev_vetoes: 2 })), events });
  assert.equal(f.whyNoTrades.code, 'jev_declined');
  assert.deepEqual(f.whyNoTrades.evidence.jev.reasons, { low_confidence: 1, choice_hold: 1, other: 0 }, "only this bot's gates are counted");
  assert.equal(f.whyNoTrades.evidence.jev.threshold, 0.7, 'a PAPER gate does not carry the threshold, so the arena default is named');
  assert.match(facts.headline(f), /1 раз confidence нижче порога 0,7 \(confidence 0,62–0,9\); 1 раз Jev відповів «утримати»/);
  assert.doesNotMatch(facts.headline(f), /жодного разу не набрала|не дав придатної відповіді/);
});

test('live battle 8d53b2a8: Sonnet V1 was declined five times, Astra was allowed', () => {
  const fixture = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'jev_gate_live5h.json'), 'utf8'));
  const eventsFor = author => fixture.decisions.filter(d => d.author === author).map(d => event('jev_decision', {
    answer: { choice: d.recorded.choice, confidence: d.recorded.confidence }, jev_allowed: d.recorded.choice === 'proceed' && d.recorded.confidence >= facts.GATE_THRESHOLD, ai_used: true }));
  const sonnet = eventsFor('sonnet'), astra = eventsFor('astra');
  assert.equal(sonnet.length, 5);
  const declined = make({ lifecycleBot: lifecycle(funnel({ checks: 5, signals: 5, jev_decisions: 5, jev_calls: 5, jev_vetoes: 5 })), events: sonnet });
  assert.equal(declined.outcome, 'no_trade');
  assert.equal(declined.whyNoTrades.code, 'jev_declined');
  assert.deepEqual([declined.whyNoTrades.evidence.jev.min, declined.whyNoTrades.evidence.jev.max], [0.6, 0.65]);
  const allowed = facts.jevAnswers(astra);
  assert.deepEqual([allowed.allowed, allowed.blocked], [3, 0]);
});

test('Jev answers without an execution verdict are counted as unknown, never as allowed', () => {
  const answers = facts.jevAnswers([event('jev_decision', { answer: { confidence: 0.9 } }),
    event('jev_decision', { answer: { confidence: 0.4 }, jev_allowed: false }), event('jev_decision', { answer: { confidence: 0.8 }, jev_allowed: true }),
    event('entry_checks', { checks: [] })]);
  assert.deepEqual([answers.decisions, answers.allowed, answers.blocked, answers.unknown], [3, 1, 1, 1]);
  assert.deepEqual([answers.min, answers.max], [0.4, 0.9]);
});

test('a signal that Jev allowed but that never became an entry points at the later step', () => {
  const f = make({ lifecycleBot: lifecycle(funnel({ checks: 9, signals: 2, jev_decisions: 2, jev_allows: 2, rechecks: 2, recheck_vetoes: 2 })) });
  assert.equal(f.whyNoTrades.code, 'later_step');
  assert.match(facts.headline(f), /скасувань під час повторної перевірки: 2/);
});

test('costs keep USDT and Jev USD apart and mark incomplete accounting', () => {
  const f = make({ lifecycleBot: lifecycle(funnel({ checks: 3, signals: 1 }, { entry_filled_orders: 1, round_trips: 1 }), costs({ gross_pnl_usdt: '1.5', signed_fees_usdt: '-0.4', signed_funding_usdt: '0.1', net_usdt: '1.2', complete: false, jev: { known_cost_usd: '0.0007', unknown_billing_requests: 1, complete: false } })) });
  assert.deepEqual([f.costs.gross, f.costs.fees, f.costs.funding, f.costs.net], ['1.5', '-0.4', '0.1', '1.2']);
  assert.equal(f.costs.jevComplete, false);
  assert.ok(f.unknown.includes('облік витрат'));
  const rows = facts.rows(f);
  assert.match(rows.find(r => r.label === 'Витрати').text, /gross \+1,5 USDT → комісії −0,4 USDT → funding \+0,1 USDT → net за ордерами \+1,2 USDT/);
  assert.match(rows.find(r => r.label.startsWith('Jev')).text, /\$0,0007 · облік неповний/);
  assert.equal(rows.find(r => r.label === 'Вхід').text, '3 перевірки → 1 сигнал → 0 дозволів Jev → 1 вхід → 1 угода');
  const unseen = facts.rows(make({ lifecycleBot: lifecycle({ entry: entry({ checks: 4 }), entry_filled_orders: null, round_trips: null, no_signal_unknown: 0 }) }));
  assert.match(unseen.find(r => r.label === 'Вхід').text, /входів: невідомо → угод: невідомо$/);
});

test('a bot that never traded gets no empty cost lines and no false "incomplete log" note', () => {
  const f = make({ lifecycleBot: lifecycle(funnel({ checks: 10 }), costs(freeJev), { complete: false }) });
  assert.equal(f.outcome, 'no_trade');
  assert.deepEqual(facts.rows(f).map(r => r.label), ['Вхід']);
  assert.deepEqual(f.unknown, [], 'lifecycle coverage of later stages is not a gap for a bot that never entered');
  const withJev = make({ lifecycleBot: lifecycle(funnel({ checks: 10 }), costs({ jev: { known_cost_usd: '0.002', unknown_billing_requests: 0, complete: true } })) });
  assert.deepEqual(facts.rows(withJev).map(r => r.label), ['Вхід', 'Jev (USD, окремо від USDT)']);
});

test('plural forms follow Ukrainian rules in the sentences', () => {
  const trades = n => facts.headline({ outcome: 'traded', trades: n, entries: n, final: true, net_usdt: '0' }).split('.')[0];
  assert.deepEqual([1, 2, 4, 5, 11, 12, 21, 22, 25].map(trades), ['Зробив 1 угоду', 'Зробив 2 угоди', 'Зробив 4 угоди', 'Зробив 5 угод', 'Зробив 11 угод', 'Зробив 12 угод', 'Зробив 21 угоду', 'Зробив 22 угоди', 'Зробив 25 угод']);
  const signals = n => facts.headline(make({ lifecycleBot: lifecycle(funnel({ checks: 9, signals: n, jev_decisions: n, jev_vetoes: n })), events: [] })).match(/дали (\d+ \S+),/)[1];
  assert.deepEqual([1, 2, 5].map(signals), ['1 сигнал', '2 сигнали', '5 сигналів']);
});

test('render builds a headline and a definition list without touching anything else', () => {
  const host = new Node('div');
  const box = facts.render(host, make({ lifecycleBot: lifecycle(funnel({ checks: 10 })) }), { doc });
  assert.equal(host.children.length, 1);
  assert.equal(box.children[0].tag, 'p');
  assert.match(box.children[0].className, /battle-facts-headline no_trade/);
  assert.equal(box.children[1].tag, 'dl');
  assert.match(allText(box), /Не торгував/);
  assert.equal(facts.render(null, make(), { doc }), null);
  const trimmed = facts.render(new Node('div'), make({ lifecycleBot: lifecycle(funnel({ checks: 10 })) }), { doc, omit: ['Вхід'] });
  assert.ok(!allText(trimmed).includes('Вхід'), 'omitted rows are not drawn');
  assert.ok(allText(box).includes('Вхід'), 'and are drawn when not omitted');
});

// ---- one battle ----------------------------------------------------------------------------------------------
const settled = { settlement: { status: 'SETTLED' }, availability: { status: 'COMPLETE' }, competitive_validity: { status: 'ELIGIBLE' } };
const participant = (id, name, f, c, quality = settled, failed = {}) => ({ id, name, funnel: { ...f, failed_features: failed }, costs: c, quality });
const lost = (id, name, net) => participant(id, name, funnel({ checks: 9, signals: 2 }, { entry_filled_orders: 1, round_trips: 1 }), costs({ net_usdt: net, signed_fees_usdt: net, ...freeJev }));
const quiet = (id, name, failed) => participant(id, name, funnel({ checks: 30 }), costs({ net_usdt: null, ...freeJev }), settled, failed);
const bundle = (participants, over = {}) => ({
  match: { id: 'm1', mode: 'OKX_LIVE', status: 'DONE', incomplete: false, started_at: '2026-10-02T05:00:00.000+00:00', admission_ends_at: '2026-10-02T09:00:00.000+00:00',
    ended_at: '2026-10-02T10:00:00.000+00:00', duration_seconds: 18000, ...(over.match || {}) },
  participants, events: over.events || [], cash: { name: 'Cash', net_usdt: '0' }, history_complete: over.history_complete !== false });

test('battle: every participant lost to Cash, so Cash is alone on top', () => {
  const bf = facts.factsForBattle(bundle([lost('a', 'Alpha', '-0.4'), lost('b', 'Beta', '-0.1'), lost('c', 'Gamma', '-1.25')]));
  assert.deepEqual(bf.ranking, ['cash', 'b', 'a', 'c']);
  assert.deepEqual(bf.top, { ids: ['cash'], withCash: true, tie: false });
  assert.deepEqual(bf.excluded, []);
  assert.equal(bf.cash.net_usdt, '0');
  assert.equal(facts.battleHeadline(bf), 'Cash — єдиний переможець: кожен із 3 ранжованих учасників програв Cash.');
  assert.equal(bf.match.final, true);
  assert.equal(bf.match.admission_ends_at, '2026-10-02T09:00:00.000+00:00');
  assert.equal(bf.match.duration_seconds, 18000);
});

test('battle: a participant that never traded shares first place with Cash', () => {
  const bf = facts.factsForBattle(bundle([lost('a', 'Alpha', '-0.4'), quiet('q', 'Quiet'), quiet('r', 'Rare')]));
  const q = bf.participants.find(p => p.id === 'q');
  assert.equal(q.outcome, 'no_trade');
  assert.equal(q.net_usdt, '0', 'no fills means nothing was spent');
  assert.deepEqual(bf.ranking, ['cash', 'q', 'r', 'a']);
  assert.deepEqual(bf.top, { ids: ['cash', 'q', 'r'], withCash: true, tie: true });
  assert.equal(facts.battleHeadline(bf), 'Спільне перше місце з Cash: Quiet, Rare — не торгували, тож не втратили нічого.');
});

test('battle: a clear winner above Cash', () => {
  const bf = facts.factsForBattle(bundle([lost('a', 'Alpha', '-0.4'), lost('w', 'Winner', '0.1234567'), quiet('q', 'Quiet')]));
  assert.deepEqual(bf.ranking, ['w', 'cash', 'q', 'a']);
  assert.deepEqual(bf.top, { ids: ['w'], withCash: false, tie: false });
  assert.equal(facts.battleHeadline(bf), 'Переміг Winner: +0,1235 USDT (Cash — 0).');
  const tied = facts.factsForBattle(bundle([lost('x', 'Xi', '0.25'), lost('y', 'Psi', '0.250')]));
  assert.deepEqual(tied.top, { ids: ['x', 'y'], withCash: false, tie: true });
  assert.equal(facts.battleHeadline(tied), 'Ділять перше місце: Xi, Psi — по +0,25 USDT.');
});

test('battle: incomplete and unknown records are shown but never ranked', () => {
  const incomplete = facts.factsForBattle(bundle([lost('a', 'Alpha', '-0.4'), participant('i', 'Inc', funnel({ checks: 9, signals: 2 }, { entry_filled_orders: 1, round_trips: 1 }), costs({ net_usdt: '-9', ...freeJev }),
    { settlement: { status: 'PENDING' }, availability: { status: 'COMPLETE' }, competitive_validity: { status: 'INELIGIBLE' } }), participant('u', 'Unseen', null, null)]));
  assert.deepEqual(incomplete.ranking, ['cash', 'a']);
  assert.deepEqual(incomplete.excluded, [{ id: 'i', reason: 'incomplete' }, { id: 'u', reason: 'unknown' }]);
  const inc = incomplete.participants.find(p => p.id === 'i');
  assert.equal(inc.outcome, 'incomplete');
  assert.equal(inc.net_usdt, '-9', 'the loss stays visible');
  assert.match(facts.battleHeadline(incomplete), /Не ранжовано \(запис неповний\): Inc, Unseen\.$/);
  const flagged = facts.factsForBattle(bundle([lost('a', 'Alpha', '-0.4')], { match: { incomplete: true } }));
  assert.equal(flagged.participants[0].outcome, 'incomplete');
  assert.equal(flagged.match.final, false);
  assert.equal(facts.battleHeadline(flagged), 'Запис бою неповний: підсумок не підбито.');
  assert.deepEqual(flagged.ranking, ['cash']);
});

test('battle: a validity flag from the bundle excludes an otherwise clean participant', () => {
  const inelig = participant('e', 'Elig', funnel({ checks: 9, signals: 2 }, { entry_filled_orders: 1, round_trips: 1 }), costs({ net_usdt: '5', ...freeJev }),
    { settlement: { status: 'SETTLED' }, availability: { status: 'COMPLETE' }, competitive_validity: { status: 'INELIGIBLE' } });
  const bf = facts.factsForBattle(bundle([inelig, lost('a', 'Alpha', '-0.4')]));
  assert.deepEqual(bf.excluded, [{ id: 'e', reason: 'ineligible' }]);
  assert.equal(bf.participants[0].eligible, false);
  assert.equal(bf.participants[0].outcome, 'traded');
  assert.deepEqual(bf.ranking, ['cash', 'a']);
});

test('battle: a running battle gets no verdict; why-no-trades comes from the bundle tally', () => {
  const running = facts.factsForBattle(bundle([quiet('q', 'Quiet')], { match: { status: 'RUNNING' } }));
  assert.equal(running.match.final, false);
  assert.equal(facts.battleHeadline(running), 'Бій ще триває: підсумок буде після завершення.');
  const withTally = facts.factsForBattle(bundle([quiet('q', 'Quiet', { return_5m_bps: 28, rsi_14: 11 })]));
  const why = withTally.participants[0].whyNoTrades;
  assert.equal(why.code, 'no_signal');
  assert.deepEqual(why.evidence.failed.map(r => [r.feature, r.count]), [['return_5m_bps', 28], ['rsi_14', 11]]);
  assert.match(why.text, /Найчастіше не виконувалась умова «return_5m_bps» \(у 28 з 30 перевірок\)/);
  assert.equal(facts.factsForBattle(null).ranking.length, 1, 'garbage in: only Cash, no throw');
  assert.equal(facts.battleHeadline(facts.factsForBattle({})), 'Бій ще триває: підсумок буде після завершення.');
});

test('battle: Jev evidence is read from the bundle events of that participant only', () => {
  const events = [0.61, 0.64].map(confidence => ({ bot_id: 'j', code: 'jev_decision', data: { answer: { choice: 'proceed', confidence: String(confidence) }, jev_allowed: false } }));
  events.push({ bot_id: 'other', code: 'jev_decision', data: { answer: { choice: 'proceed', confidence: '0.99' }, jev_allowed: true } });
  const declined = participant('j', 'Jev', funnel({ checks: 2, signals: 2, jev_decisions: 2, jev_vetoes: 2 }), costs({ net_usdt: null, ...freeJev }));
  const bf = facts.factsForBattle(bundle([declined], { events }));
  const why = bf.participants[0].whyNoTrades;
  assert.equal(why.code, 'jev_declined');
  assert.deepEqual([why.evidence.jev.min, why.evidence.jev.max], [0.61, 0.64]);
});

// ---- guards --------------------------------------------------------------------------------------------------
test('the module only interprets data: no requests, no DOM globals, no owner endpoints', () => {
  const source = fs.readFileSync(path.join(__dirname, '../web/battle-facts.js'), 'utf8');
  assert.equal(/\bfetch\s*\(|XMLHttpRequest|sendBeacon|WebSocket|EventSource|\/api\/|panel-token|localStorage/.test(source), false);
  assert.equal(/method\s*:\s*["']POST|exchange\/(start|stop|resume)/.test(source), false);
});

test('arena page loads the module before the page script and degrades without it', () => {
  const html = fs.readFileSync(path.join(__dirname, '../web/arena.html'), 'utf8');
  const page = fs.readFileSync(path.join(__dirname, '../web/arena.js'), 'utf8');
  assert.ok(html.includes('<script src="/battle-facts.js" defer></script>'));
  assert.ok(html.indexOf('/battle-facts.js') < html.indexOf('/arena.js'));
  assert.ok(page.includes('window.ArenaBattleFacts?.') || page.includes('window.ArenaBattleFacts&&'), 'the page must tolerate a server that does not serve the module');
});

test('R2.1.11 (D141 p. 2): a battle of examples only says "технічна проба, не рахується" in the headline', () => {
  const bundle = { match: { id: 'x', mode: 'OKX_LIVE', status: 'DONE', incomplete: false }, participants: [], events: [], history_complete: true };
  assert.match(facts.battleHeadline(facts.factsForBattle(bundle, { exampleOnly: true })), /^Технічна проба, не рахується/);
  assert.doesNotMatch(facts.battleHeadline(facts.factsForBattle(bundle)), /Технічна проба/);
});

test('R2.1.11: the arena no longer tags examples "Навчальний приклад · Codex" (D135 p. 7)', () => {
  const page = fs.readFileSync(path.join(__dirname, '../web/arena.js'), 'utf8');
  const html = fs.readFileSync(path.join(__dirname, '../web/arena.html'), 'utf8');
  assert.ok(!page.includes('Навчальний приклад · Codex'));
  assert.ok(!html.includes('приклади підписані Codex'));
});

// R2.1.12 (MIX-9): the label's source-text checks became behavioural tests in test_r2112_mix_ui.cjs
