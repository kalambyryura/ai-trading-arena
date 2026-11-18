const test = require('node:test');
const assert = require('node:assert/strict');
const Replay = require('../web/event-replay.js');
const Fixture = require('./v8_ui/synthetic-fixture.js');
const copy = () => JSON.parse(JSON.stringify(Fixture.replay));

test('recorded 31-digit amount and bounded 64-digit fraction survive replay exactly', () => {
  for (const value of ['-0.0000000000000031866870253693946', '0.' + '0'.repeat(63) + '1']) {
    const packet = copy();
    const event = packet.events.find(e => e.financial);
    event.financial.gross_usdt = value;
    const before = JSON.stringify(packet);
    const replay = new Replay.Replay(packet);
    const snapshot = replay.seekSeq(event.seq);
    assert.equal(snapshot.state.bots.find(b => b.id === event.bot_id).financial.gross_usdt, value);
    assert.equal(JSON.stringify(packet), before);
  }
});

test('excess precision and non-decimal values remain rejected', () => {
  for (const value of ['0.' + '0'.repeat(64) + '1', '1e-31', 'NaN', 'Infinity', '01.0', true]) {
    const packet = copy();
    packet.events.find(e => e.financial).financial.gross_usdt = value;
    assert.throws(() => new Replay.Replay(packet));
  }
});

test('a tiny remaining position cannot silently become flat at DONE', () => {
  const packet = copy();
  const last = [...packet.events].reverse().find(e => e.financial);
  last.financial.position_btc = '0.' + '0'.repeat(63) + '1';
  assert.throws(() => new Replay.Replay(packet));
});
