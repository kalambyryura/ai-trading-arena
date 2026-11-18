"use strict";
/* Signed LONG/SHORT fake DTOs for the real page controller. Never uses a journal. */
const Base = require("./synthetic-fixture.js"), Replay = require("../../web/event-replay.js"), Verify = require("../../web/review-verify.js");
const copy = value => JSON.parse(JSON.stringify(value));
async function fixtures() {
  const packet = copy(Base.replay), start = packet.manifest.started_at_ms;
  const events = packet.events.filter(event => event.code !== "settled_b").map(event => ({...event, oldSeq:event.seq}));
  const extra = (offset, type, code, data) => events.push({available_at_ms:start + offset, type, code, ...data});
  extra(34000, "INTENT", "short_entry_intent", {bot_id:"fixture-b",step:3,message:"SHORT intent · fill ще немає"});
  extra(35000, "FILL", "short_entry_fill", {bot_id:"fixture-b",step:3,message:"Recorded SHORT fill", financial:{position_btc:"-0.0003",equity_usdt:"99.9996",pnl_usdt:"-0.0004",signed_fees_usdt:"-0.0004"}});
  extra(86462000, "FILL", "short_partial_reduction", {bot_id:"fixture-b",step:7,message:"SHORT partial reduction · залишок відкрито",financial:{position_btc:"-0.0001",equity_usdt:"100.0013",pnl_usdt:"0.0013",signed_fees_usdt:"-0.0007"}});
  extra(86800000, "FILL", "short_flat_fill", {bot_id:"fixture-b",step:7,message:"SHORT actual flat · settlement ще очікується",financial:{position_btc:"0",equity_usdt:"100.0015",pnl_usdt:"0.0015",gross_usdt:"0.0034",signed_fees_usdt:"-0.001",signed_funding_usdt:"-0.0009",trades:1}});
  extra(86801000, "SETTLEMENT", "settled_b", {bot_id:"fixture-b",step:9,settled:true});
  events.sort((a,b) => a.available_at_ms - b.available_at_ms);
  const mappings = new Map();
  packet.events = events.map((event,index) => {const {oldSeq,seq,...clean} = event; if(oldSeq)mappings.set(oldSeq,index+1); return {seq:index+1,source_event_seq:index+1,...clean};});
  packet.package_hash = await Replay.packetDigest(packet);
  const review = copy(Base.review), refKeys = new Set(["reference","seq","entry_seq","close_seq"]);
  function remap(value) {
    if (Array.isArray(value)) {value.forEach(remap); return;}
    if (!value || typeof value !== "object") return;
    for (const [key, child] of Object.entries(value)) {
      if (refKeys.has(key) && typeof child === "number") value[key] = mappings.get(child) || child;
      else remap(child);
    }
  }
  remap(review);
  const entry = packet.events.find(event => event.code === "short_entry_fill").seq,
    partial = packet.events.find(event => event.code === "short_partial_reduction").seq,
    flat = packet.events.find(event => event.code === "short_flat_fill").seq;
  Object.assign(review.participants[1], {current_entries:0, seasonal_round_trips:1, position_btc:"0",
    funnel:{stages:review.participants[0].funnel.stages.map(row => ({...row,count:row.id==="checks"?4:1,reference:row.id==="fills"?entry:null})),round_trips:1},
    costs:{gross_pnl_usdt:"0.0034",signed_fees_usdt:"-0.001",signed_funding_usdt:"-0.0009",net_usdt:"0.0015",cash_delta_usdt:"0.0015",complete:true,
      jev:{known_cost_usd:"0",requests:0,unknown_billing_requests:0,complete:true,source:"no requests"}},
    trade_ledger:{rows:[{seq:entry,role:"entry",quantity:"0.0003",position_before:"0",position_after:"-0.0003",gross_pnl_usdt:"0",signed_fees_usdt:"-0.0004",source:"paper_incremental_btc"},
      {seq:partial,role:"reduce",quantity:"0.0002",position_before:"-0.0003",position_after:"-0.0001",gross_pnl_usdt:"0.002",signed_fees_usdt:"-0.0003",source:"paper_incremental_btc"},
      {seq:flat,role:"close",quantity:"0.0001",position_before:"-0.0001",position_after:"0",gross_pnl_usdt:"0.0014",signed_fees_usdt:"-0.0003",source:"paper_incremental_btc"}],round_trips:[{number:1,entry_seq:entry,close_seq:flat,closed:true}],inventory_known:true,remaining:"0",limitations:[]}});
  review.events = packet.events.map(event => ({seq:event.seq,at:new Date(event.available_at_ms).toISOString(),bot_id:event.bot_id || null,step:event.step || null,code:event.code,data:{}}));
  review.frames = []; review.history_complete = true;
  review.integrity = {source_event_count:review.events.length};
  const noIntegrity = copy(review); delete noIntegrity.integrity;
  review.integrity.bundle_sha256 = await Verify.sha256(Replay.canonical(noIntegrity));
  const corrupt = copy(packet); corrupt.manifest.id = "fixture-corrupt";
  corrupt.package_hash = await Replay.packetDigest(corrupt); corrupt.events[0].financial.equity_usdt = "101";
  const open = copy(packet); open.manifest.id = "fixture-open-tail";
  open.events = open.events.filter(event => event.available_at_ms <= open.manifest.admission_end_at_ms);
  open.package_hash = await Replay.packetDigest(open);
  const league = copy(Base.league);
  league.battles[0].id="synthetic-v8-proof";league.battles[0].kind="paper";
  league.battles[1].id="fixture-open-tail";league.battles[1].kind="paper";league.battles[1].mode="PAPER";
  league.frequencies=[{id:"fixture-a",name:"Учасник A · вигаданий",version:1,cohort:"fixture-cohort-v1",comparable_battles:0,sole_wins:0,shared_top:0},
    {id:"fixture-b",name:"Учасник B · вигаданий",version:1,cohort:"fixture-cohort-v1",comparable_battles:0,sole_wins:0,shared_top:0},
    {id:"cash",name:"Cash",version:0,cohort:"fixture-cohort-v1",comparable_battles:0,sole_wins:0,shared_top:0}];
  return {review, replay:packet, corrupt, open, workshop:Base.workshop, league};
}
module.exports = {fixtures};
if (require.main === module) fixtures().then(value => process.stdout.write(JSON.stringify(value))).catch(error => {process.stderr.write(error.message);process.exitCode=1;});
