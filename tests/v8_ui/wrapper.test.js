"use strict";
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path");
const Verify = require("../../web/review-verify.js"), Labels = require("../../web/replay-labels.js"), Page = require("../../web/review.js"), Replay = require("../../web/event-replay.js");
const {fixtures} = require("./wrapper-fixture.js");

test("wire canonicalization preserves Python float tokens, unicode and sorted nested keys", () => {
  const raw = '{ "z": 3.0, "a": [1e-07, "Юрій"], "nested": {"b":true,"a":null}, "integrity": {} }';
  assert.equal(Verify.canonicalWire(raw,["integrity"]), '{"a":[1e-07,"Юрій"],"nested":{"a":null,"b":true},"z":3.0}');
  assert.notEqual(Verify.canonicalWire(raw,["integrity"]), Replay.canonical(JSON.parse(raw)));
  assert.throws(() => Verify.canonicalWire('{"a":1,"a":2}'), /Повторений/);
  assert.throws(() => Verify.canonicalWire('{"a":1} trailing'), /Додаткові/);
});
test("unmodified real-shape author bundle verifies; a modified microPnL fails hash", async () => {
  const {review} = await fixtures();
  assert.equal((await Verify.verifyReview(JSON.stringify(review))).verified,true);
  review.participants[0].costs.net_usdt = "-0.0014000001";
  assert.equal((await Verify.verifyReview(JSON.stringify(review))).verified,false);
});
test("a valid digest cannot conceal broken ledger references or accounting", async () => {
  const {review} = await fixtures();
  review.participants[0].trade_ledger.rows[0].seq = 100000;
  const body = {...review}; delete body.integrity;
  review.integrity.bundle_sha256 = await Verify.sha256(Replay.canonical(body));
  assert.match((await Verify.verifyReview(JSON.stringify(review))).reason,/Відсутній доказ/);
});
test("typed labels distinguish signed partials, adds, flat and reversal without floating error", () => {
  const fill = position => ({seq:1,type:"FILL",code:"observed_fill",financial:{position_btc:position}});
  assert.equal(Labels.describe(fill("0.0001"),"0.0002").action,"Часткове скорочення LONG");
  assert.equal(Labels.describe(fill("-0.0001"),"-0.0003").action,"Часткове скорочення SHORT");
  assert.equal(Labels.describe(fill("-0.0004"),"-0.0003").action,"Докупівля SHORT");
  assert.equal(Labels.describe(fill("0"),"-0.0001").action,"Повне закриття SHORT · FLAT");
  assert.equal(Labels.describe(fill("-0.0001"),"0.0002").action,"Reversal LONG → SHORT");
  assert.equal(Labels.describe(fill("0.000000000000000000000000000002"),"0.000000000000000000000000000001").action,"Докупівля LONG");
  assert.equal(Labels.describe(fill("0.0001"),null).recorded_transition,false);
  assert.equal(Labels.describe({seq:1,type:"INTENT",code:"close_intent"},"-0.0003").action,"Намір ордера");
});
test("signed causal fixture is hash-valid; LONG and SHORT stay open after admission", async () => {
  const {replay:packet,open,corrupt} = await fixtures(); const replay = new Replay.Replay(packet);
  assert.equal(await Replay.packetDigest(packet),packet.package_hash);
  const tail = replay.seek(packet.manifest.admission_end_at_ms).state;
  assert.equal(tail.phase,"DRAINING");
  assert.equal(tail.bots.find(b => b.id === "fixture-a").financial.position_btc,"0.0002");
  assert.equal(tail.bots.find(b => b.id === "fixture-b").financial.position_btc,"-0.0003");
  const openTape = new Replay.Replay(open).seek(open.manifest.recording_end_at_ms).state;
  assert.equal(openTape.phase,"DRAINING"); assert.equal(openTape.bots[1].financial.position_btc,"-0.0003");
  assert.notEqual(await Replay.packetDigest(corrupt),corrupt.package_hash);
});
test("page paths admit GET review/replay only, and reject traversal or owner actions", () => {
  assert.deepEqual(Page.route("?mode=paper&id=fixture&view=replay&seq=13"),{mode:"paper",id:"fixture",view:"replay",botId:null,eventSeq:13,sourceEventSeq:null});
  for(const search of ["?mode=live&id=x","?mode=paper&id=../x","?mode=paper&id=x&seq=-1","?mode=paper&id=x&seq=Infinity"]) assert.throws(() => Page.route(search));
  for(const route of ["/api/play","/api/exchange/start","/api/exchange/stop","/api/exchange/resume","https://example.com/api/review/paper/a","/api/review/paper/../a","/api/league"]) assert.equal(Page.allowedGET(route),false,route);
  assert.equal(Page.allowedGET("/api/review/exchange/0123456789abcdef0123456789abcdef"),true);
  assert.equal(Page.allowedGET("/api/replay/paper/fixture"),true);
});
test("Review journal references resolve through explicit mapping, not shifted packet seq", () => {
  const packet = {events:[{seq:1,code:"cash_baseline"},{seq:2,source_event_seq:7,code:"entry_fill"},
    {seq:3,source_event_seq:7,code:"recorded_frame"}]};
  assert.equal(Page.packetSequenceForSource(packet,7),2);
  assert.throws(() => Page.packetSequenceForSource(packet,2),/source_event_seq/);
  assert.throws(() => Page.route("?mode=paper&id=fixture&seq=2&event=7"));
});
test("wrapper excludes arena owner bootstrap and any POST handlers", () => {
  const html=fs.readFileSync(path.join(__dirname,"../../web/review.html"),"utf8"), source=fs.readFileSync(path.join(__dirname,"../../web/review.js"),"utf8");
  assert.equal(/src="\/arena\.js"|panel-token|api\/exchange\/(start|stop|resume)/.test(html),false);
  assert.equal(/method\s*:\s*["']POST|panel-token|XMLHttpRequest|WebSocket|\/api\/exchange\/(start|stop|resume)/.test(source),false);
  assert.equal(source.includes('method:"GET"'),true);
  const current=fs.readFileSync(path.join(__dirname,"../../web/arena.js"),"utf8").split(/\r?\n/).slice(0,376).join("\n").trim();
  const copied=fs.readFileSync(path.join(__dirname,"../../web/arena-battle-view.js"),"utf8").replace(/^\uFEFF/,"").replace(/\r\n/g,"\n").trim();
  assert.equal(copied,current);
  assert.equal(/\bfetch\s*\(|\/api\//.test(copied),false);
});
