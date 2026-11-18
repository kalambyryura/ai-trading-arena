// Event-code phrases of the replay labels: shape, coverage of the recorder vocabulary, fallbacks. No browser, requests or keys.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Labels = require('../web/replay-labels.js');
const Fixture = require('./v8_ui/synthetic-fixture.js');

// Codes the engines emit (trading_lab `emit(...)` literals + the two dynamic fill codes + the V2 temporal codes of tools/arena_eval.py
// + the R2 post-only entry codes of arena_engine.py / exchange_lab.py + R2.1.5's refused/absent orders and the feature book).
const RECORDER_CODES = `entry_order_placed entry_order_expired entry_order_rejected entry_order_cancelled entry_order_cancel_failed
entry_order_unwatched_cancel execution_profile leverage_confirmed stop_renewed step_timing exchange_order_read degraded_window_started degraded_window_ended degraded_extension_ended protective_close_pending entry_hold fill_read_lag fill_protected fill_watch_failed protective_placed protective_amended protective_terminal exchange_order_posted exchange_cancel_posted accounting_evidence account_checked accounting_confirmed admission_closed clock_gap close_no_fill closing_paused data_gap demo_execution_mode
demo_execution_review demo_funding_unknown entry_cancelled entry_checks entry_no_fill entry_rechecked exchange_check_failed exchange_error exchange_funding
exchange_order_intent exchange_reserved exit_trigger financial_observation funding_schedule_restored jev_decision jev_gate jev_pending jev_request
jev_shadow_payload market_unavailable model_funding no_signal order_ack_uncertain paper_close_fill paper_entry_fill paused position_checks
protective_child_adopted protective_intent recording_blocked recovery_started reduction_remainder_veto reduction_trigger reduction_veto report_pending
risk_limit risk_veto rules_close rules_decision rules_only_mode rules_only_review season_done season_finished season_started stop_requested
exchange_entry_fill exchange_close_fill setup_armed setup_waiting setup_waiting_confirmation setup_confirmed setup_expired setup_invalidated setup_gap_reset
cost_checkpoint_checked cost_checkpoint_unknown
exchange_order_rejected exchange_order_absent exchange_order_not_visible feature_book exchange_order_reopened exchange_order_fill_not_visible exchange_order_found
protective_fired_during_check protective_fire_pending protective_fire_resolved protective_fire_expired settled_account_verified settled_account_unverified
entry_position_facts liquidation_unknown liquidation_too_close liquidation_closer_than_stop liquidation_detected
protective_absent protective_not_created`.split(/\s+/);

const event = (type, code, extra = {}) => ({ seq: 1, type, code, available_at_ms: 0, ...extra });

test('every phrase is a short non-empty Ukrainian text under a snake_case code', () => {
  for (const [code, text] of Object.entries(Labels.PHRASES)) {
    assert.match(code, /^[a-z][a-z0-9_]*$/, code);
    assert.equal(typeof text, 'string');
    assert.ok(text.length >= 5 && text.length <= 60, code + ': ' + text.length);
    assert.match(text, /[А-Яа-яІіЇїЄє]/, code + ' is not Ukrainian');
    assert.equal(text, text.trim());
  }
});

test('the whole recorder vocabulary has a phrase and nothing else is invented', () => {
  const missing = RECORDER_CODES.filter(code => !Labels.phrase(code));
  assert.deepEqual(missing, []);
  const extra = Object.keys(Labels.PHRASES).filter(code => !RECORDER_CODES.includes(code));
  assert.deepEqual(extra, [], 'a phrase for a code the recorder never emits would be a guess');
});

test('an unknown or malformed code has no phrase: the page keeps the raw code', () => {
  for (const code of ['formal_signal', 'ENTRY_CHECKS', '', null, undefined, 42, {}, '__proto__', 'constructor', 'toString', 'hasOwnProperty', { toString: () => 'paused' }, ['paused']])
    assert.equal(Labels.phrase(code), null, String(code));
  const label = Labels.describe(event('CHECK', 'something_new'));
  assert.equal(label.phrase, null);
  assert.equal(label.detail, null);
  assert.equal(Labels.summary(label), 'Перевірка');
});

test('a type-level action gets the phrase as its detail; fills and positions already say what happened', () => {
  const check = Labels.describe(event('CHECK', 'entry_checks'));
  assert.equal(check.action, 'Перевірка');
  assert.equal(check.detail, 'Перевірка умов входу');
  assert.equal(Labels.summary(check), 'Перевірка · Перевірка умов входу');
  const fill = Labels.describe(event('FILL', 'exchange_entry_fill', { financial: { position_btc: '-0.0003' } }), '0');
  assert.equal(fill.action, 'Вхід SHORT');
  assert.equal(fill.phrase, 'Вхід підтверджено біржею');
  assert.equal(fill.detail, null, 'the transition already says it');
  assert.equal(Labels.summary(fill), 'Вхід SHORT');
  const bare = Labels.describe(event('FILL', 'paper_entry_fill'));
  assert.equal(bare.action, 'Fill');
  assert.equal(bare.detail, 'Модельний вхід виконано', 'a fill without a position change has nothing else to say');
  const position = Labels.describe(event('POSITION', 'position_checks', { financial: { position_btc: '0' } }), '0');
  assert.equal(position.phrase, 'Перевірка відкритої позиції');
  assert.equal(position.detail, null, 'a position observation is already named by its side');
  assert.equal(Labels.summary(position), 'Спостереження позиції · FLAT');
});

test('describe keeps its earlier fields and values on the fictional fixture', () => {
  const labels = Labels.index(Fixture.replay);
  const entry = labels.get(7);
  assert.deepEqual({ seq: entry.seq, type: entry.type, action: entry.action, side: entry.side, recorded_transition: entry.recorded_transition },
    { seq: 7, type: 'FILL', action: 'Вхід LONG', side: 'LONG', recorded_transition: true });
  assert.equal(labels.get(4).action, 'Перевірка');
  assert.equal(labels.get(4).phrase, null, 'the fixture\'s fictional code has no phrase');
});

test('the spectator list and the review event detail use the phrase only where it adds something', () => {
  const root = path.join(__dirname, '..', 'web');
  const spectator = fs.readFileSync(path.join(root, 'spectator.js'), 'utf8');
  const review = fs.readFileSync(path.join(root, 'review.js'), 'utf8');
  assert.match(spectator, /ArenaReplayLabels\.summary\(labels\.get\(event\.seq\)\)/);
  assert.match(review, /label\.detail && !event\.message \? label\.detail \+ " · " : ""\) \+ event\.type \+ " · " \+ event\.code/,
    'review keeps the raw type · code and adds the phrase only when the engine sent no message of its own');
});

test('phrases are display text: no request, clock or randomness in the labels module', () => {
  const source = fs.readFileSync(path.join(__dirname, '../web/replay-labels.js'), 'utf8');
  assert.equal(/\bfetch\s*\(|XMLHttpRequest|sendBeacon|WebSocket|EventSource|\/api\/|localStorage|Math\.random|Date\.now/.test(source), false);
});

test('an exchange order update without a counted fill is never "entry confirmed"', () => {
  const update = Labels.describe({seq: 7, type: 'FILL', code: 'exchange_entry_fill'}, '0');  // live, or cancelled with 0 contracts
  assert.equal(update.action, 'Оновлення заявки');
  assert.equal(update.phrase, 'Стан заявки на вхід · fill не враховано');
  const fill = Labels.describe({seq: 8, type: 'FILL', code: 'exchange_entry_fill', financial: {position_btc: '0.013'}}, '0');
  assert.equal(fill.action, 'Вхід LONG');
  assert.equal(fill.phrase, 'Вхід підтверджено біржею');
});
