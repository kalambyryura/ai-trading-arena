"use strict";
/* Scoped V8 proposal. Pure display reducer; no network, clock, model or execution transport. */
((root, factory) => {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ArenaEventReplay = api;
})(typeof window === "undefined" ? {} : window, () => {
  const SCHEMA = "arena-display-replay/v1";
  const SPEEDS = Object.freeze([1, 10, 100, 1000]);   // 1000x turns a 5 h battle into 18 s and a 24 h battle into 86 s
  const TYPES = new Set(["CHECK", "DECISION", "INTENT", "FILL", "POSITION", "ACCOUNTING",
    "ADMISSION_CLOSED", "QUALITY", "SETTLEMENT", "PHASE"]);
  const ACTIONS = new Set(["DECISION", "INTENT", "FILL", "ADMISSION_CLOSED", "QUALITY", "SETTLEMENT"]);
  const PHASES = new Set(["RUNNING", "DRAINING", "CLOSING", "PAUSED", "DONE"]);
  const FINANCIAL_KEYS = new Set(["position_btc", "equity_usdt", "pnl_usdt", "gross_usdt",
    "signed_fees_usdt", "signed_funding_usdt", "known_jev_usd", "trades"]);
  const clone = value => JSON.parse(JSON.stringify(value));
  const fail = message => { throw new Error(message); };
  const object = value => value && typeof value === "object" && !Array.isArray(value);
  const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);

  function exactKeys(value, allowed, required, label) {
    if (!object(value)) fail(label + ": expected object");
    for (const key of Object.keys(value)) if (!allowed.includes(key)) fail(label + ": forbidden field " + key);
    for (const key of required) if (!own(value, key)) fail(label + ": missing " + key);
  }
  function text(value, label, max = 240) {
    if (typeof value !== "string" || !value.length || value.length > max || /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(value)) {
      fail(label + ": expected bounded text");
    }
  }
  function identity(value, label) {
    if (typeof value !== "string" || !/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$/.test(value)) fail(label + ": invalid alias");
  }
  function ms(value, label) {
    if (!Number.isSafeInteger(value) || value < 0) fail(label + ": expected epoch milliseconds");
  }
  function decimal(value, label) {
    // Same bounded precision as Python; never round a remaining position to zero.
    if (value !== null && (typeof value !== "string" || !/^-?(0|[1-9]\d{0,39})(\.\d{1,64})?$/.test(value))) {
      fail(label + ": expected exact decimal string or null");
    }
  }

  function validate(packet) {
    exactKeys(packet, ["schema", "evidence_kind", "package_hash", "manifest", "events"],
      ["schema", "evidence_kind", "package_hash", "manifest", "events"], "packet");
    if (packet.schema !== SCHEMA) fail("Unsupported replay schema");
    if (!["ACTUAL", "MODEL", "SYNTHETIC"].includes(packet.evidence_kind)) fail("Unknown evidence kind");
    if (!/^[a-f0-9]{64}$/.test(packet.package_hash)) fail("Invalid package hash");
    const manifest = packet.manifest;
    exactKeys(manifest, ["id", "mode", "started_at_ms", "admission_end_at_ms", "recording_end_at_ms",
      "time_policy", "participants", "renderer_version", "assets_version", "visual_seed"],
      ["id", "mode", "started_at_ms", "admission_end_at_ms", "recording_end_at_ms", "time_policy",
        "participants", "renderer_version", "assets_version", "visual_seed"], "manifest");
    identity(manifest.id, "run id");
    if (!["OKX_LIVE", "OKX_DEMO", "PAPER"].includes(manifest.mode)) fail("Unknown replay mode");
    if (manifest.time_policy !== "admission_then_strategy_exit") fail("V8 requires explicit time policy");
    for (const key of ["started_at_ms", "admission_end_at_ms", "recording_end_at_ms"]) ms(manifest[key], key);
    if (manifest.admission_end_at_ms <= manifest.started_at_ms ||
        manifest.recording_end_at_ms < manifest.started_at_ms) fail("Invalid manifest times");
    for (const key of ["renderer_version", "assets_version", "visual_seed"]) text(manifest[key], key, 80);
    if (!Array.isArray(manifest.participants) || !manifest.participants.length || manifest.participants.length > 25) {
      fail("Invalid display roster; it is not an execution allowance");
    }
    const ids = new Set();
    for (const participant of manifest.participants) {
      exactKeys(participant, ["id", "name", "strategy_id", "version", "avatar_kind", "slot"],
        ["id", "name", "strategy_id", "version", "avatar_kind", "slot"], "participant");
      identity(participant.id, "participant id"); identity(participant.strategy_id, "strategy id");
      text(participant.name, "participant name", 80); text(participant.version, "version", 80);
      if (!["human", "model", "baseline"].includes(participant.avatar_kind)) fail("Unknown avatar kind");
      if (!Number.isSafeInteger(participant.slot) || participant.slot < 0 ||
          participant.slot >= manifest.participants.length || ids.has(participant.id)) fail("Duplicate/invalid slot or id");
      ids.add(participant.id);
    }
    if (new Set(manifest.participants.map(p => p.slot)).size !== ids.size) fail("Duplicate display slot");
    if (!Array.isArray(packet.events)) fail("Expected event array");
    let priorTime = manifest.started_at_ms, priorSeq = 0;
    for (const event of packet.events) {
      exactKeys(event, ["seq", "source_event_seq", "available_at_ms", "source_at_ms", "type", "bot_id", "code", "message",
        "step", "financial", "phase", "settled", "quality"], ["seq", "available_at_ms", "type", "code"], "event");
      if (event.seq !== priorSeq + 1) fail("Event sequence gap or duplicate");
      if (own(event, "source_event_seq") && (!Number.isSafeInteger(event.source_event_seq) || event.source_event_seq < 1)) fail("Invalid source event reference");
      priorSeq = event.seq; ms(event.available_at_ms, "available_at_ms");
      if (event.available_at_ms < priorTime || event.available_at_ms > manifest.recording_end_at_ms) fail("Non-causal event availability");
      priorTime = event.available_at_ms;
      if (own(event, "source_at_ms")) ms(event.source_at_ms, "source_at_ms");
      if (!TYPES.has(event.type)) fail("Unknown replay event type");
      identity(event.code, "event code");
      if (own(event, "message")) text(event.message, "message");
      if (own(event, "bot_id") && !ids.has(event.bot_id)) fail("Unknown participant reference");
      if (own(event, "step") && (!Number.isInteger(event.step) || event.step < 1 || event.step > 9)) fail("Invalid lifecycle step");
      if (own(event, "phase") && !PHASES.has(event.phase)) fail("Unknown phase");
      if (event.type === "ADMISSION_CLOSED" && (event.available_at_ms < manifest.admission_end_at_ms || event.phase !== "DRAINING")) {
        fail("Admission event cannot settle or close a position");
      }
      if (own(event, "settled") && (typeof event.settled !== "boolean" || !event.bot_id)) fail("Invalid settlement observation");
      if (own(event, "quality")) {
        exactKeys(event.quality, ["coverage", "billing", "reason", "instrument"],
          ["coverage", "billing"], "quality");
        if (!["COMPLETE", "GAP", "UNKNOWN"].includes(event.quality.coverage) ||
            !["KNOWN", "UNKNOWN"].includes(event.quality.billing)) fail("Invalid quality observation");
        if (own(event.quality, "reason")) text(event.quality.reason, "quality reason");
        if (own(event.quality, "instrument")) text(event.quality.instrument, "quality instrument");
      }
      if (own(event, "financial")) {
        if (!event.bot_id) fail("Financial observation requires participant");
        exactKeys(event.financial, Array.from(FINANCIAL_KEYS), [], "financial");
        for (const [key, value] of Object.entries(event.financial)) {
          if (key === "trades") {
            if (value !== null && (!Number.isSafeInteger(value) || value < 0)) fail("Invalid trades");
          } else decimal(value, key);
        }
      }
    }
    return true;
  }

  function initial(packet) {
    const bots = packet.manifest.participants.map(participant => {
      const baseline = participant.avatar_kind === "baseline";
      return {id: participant.id, financial: {
        position_btc: baseline ? "0" : null, equity_usdt: null, pnl_usdt: baseline ? "0" : null,
        gross_usdt: baseline ? "0" : null, signed_fees_usdt: baseline ? "0" : null,
        signed_funding_usdt: baseline ? "0" : null, known_jev_usd: null, trades: baseline ? 0 : null
      }, last_known_position_btc: baseline ? "0" : null, settled: false, step: null,
      last_reason: baseline ? "Cash · без угод" : "Ще немає спостереження", last_event_seq: 0};
    });
    return {seq: 0, available_at_ms: packet.manifest.started_at_ms, phase: "RUNNING",
      quality: {coverage: "UNKNOWN", billing: "UNKNOWN"}, bots};
  }
  function reduce(state, event) {
    const next = clone(state); next.seq = event.seq; next.available_at_ms = event.available_at_ms;
    if (event.phase) next.phase = event.phase;
    if (event.quality) next.quality = clone(event.quality);
    if (event.bot_id) {
      const bot = next.bots.find(b => b.id === event.bot_id);
      if (event.financial) {
        Object.assign(bot.financial, event.financial);
        if (own(event.financial, "position_btc") && event.financial.position_btc !== null) {
          bot.last_known_position_btc = event.financial.position_btc;
        }
        if (event.type !== "SETTLEMENT" && own(event.financial, "position_btc") &&
            event.financial.position_btc !== null && Number(event.financial.position_btc) !== 0) bot.settled = false;
      }
      if (own(event, "settled")) bot.settled = event.settled;
      if (event.step) bot.step = event.step;
      bot.last_reason = event.message || event.code; bot.last_event_seq = event.seq;
    }
    if (next.phase === "DONE" && next.bots.some(bot => bot.financial.position_btc === null ||
        Number(bot.financial.position_btc) !== 0 || !bot.settled)) fail("DONE without flat/settled facts");
    return next;
  }
  function upperBound(events, target) {
    let left = 0, right = events.length;
    while (left < right) {
      const mid = Math.floor((left + right) / 2);
      if (events[mid].available_at_ms <= target) left = mid + 1; else right = mid;
    }
    return left;
  }

  class Replay {
    constructor(packet, options = {}) {
      validate(packet); this.packet = clone(packet);
      const interval = options.checkpointEvery ?? 128;
      if (!Number.isSafeInteger(interval) || interval < 1 || interval > 4096) fail("Invalid checkpoint interval");
      this.checkpoints = [{index: 0, state: initial(packet)}];
      let state = initial(packet);
      this.packet.events.forEach((event, index) => {
        state = reduce(state, event);
        if ((index + 1) % interval === 0) this.checkpoints.push({index: index + 1, state: clone(state)});
      });
      this.cursor = packet.manifest.started_at_ms; this.speed = 1; this.playing = false;
      this.seek(this.cursor);
    }
    seek(at) {
      ms(at, "seek time");
      const manifest = this.packet.manifest;
      this.cursor = Math.min(Math.max(at, manifest.started_at_ms), manifest.recording_end_at_ms);
      const index = upperBound(this.packet.events, this.cursor);
      return this.restore(index);
    }
    seekSeq(seq) {
      if (!Number.isSafeInteger(seq) || seq < 0 || seq > this.packet.events.length) fail("Invalid target event sequence");
      this.cursor = seq ? this.packet.events[seq - 1].available_at_ms : this.packet.manifest.started_at_ms;
      return this.restore(seq);
    }
    restore(index) {
      let left = 0, right = this.checkpoints.length;
      while (left < right) {
        const middle = Math.floor((left + right) / 2);
        if (this.checkpoints[middle].index <= index) left = middle + 1; else right = middle;
      }
      const checkpoint = this.checkpoints[Math.max(0, left - 1)];
      this.state = clone(checkpoint.state);
      for (let i = checkpoint.index; i < index; i++) this.state = reduce(this.state, this.packet.events[i]);
      this.index = index; return this.snapshot();
    }
    setSpeed(speed) {
      if (!SPEEDS.includes(speed)) fail("Replay speed must be 1x, 10x, 100x or 1000x");
      this.speed = speed;
    }
    play() { this.playing = this.cursor < this.packet.manifest.recording_end_at_ms || this.index < this.packet.events.length; }
    pause() { this.playing = false; }
    tick(elapsedMs) {
      if (!Number.isFinite(elapsedMs) || elapsedMs < 0) fail("Invalid elapsed playback time");
      if (this.playing) this.seek(Math.round(this.cursor + elapsedMs * this.speed));
      if (this.cursor >= this.packet.manifest.recording_end_at_ms && this.index >= this.packet.events.length) this.pause();
      return this.snapshot();
    }
    nextAction(botId = null) {
      if (botId !== null && !this.packet.manifest.participants.some(p => p.id === botId)) fail("Unknown follow participant");
      const event = this.packet.events.slice(this.index).find(event =>
        ACTIONS.has(event.type) && (!botId || !event.bot_id || event.bot_id === botId));
      this.pause(); return event ? this.seekSeq(event.seq) : this.snapshot();
    }
    snapshot() {
      return {at_ms: this.cursor, speed: this.speed, playing: this.playing, event_count: this.index,
        state: clone(this.state), events: clone(this.packet.events.slice(Math.max(0, this.index - 128), this.index))};
    }
    frame() {
      const snapshot = this.snapshot(), manifest = this.packet.manifest;
      const participants = manifest.participants.slice().sort((a, b) => a.slot - b.slot);
      const bots = participants.map(participant => {
        const state = snapshot.state.bots.find(bot => bot.id === participant.id), f = state.financial;
        return {id: participant.id, name: participant.name, strategy_id: participant.strategy_id,
          strategy_version: participant.version, avatar_kind: participant.avatar_kind,
          position_btc: f.position_btc, equity_usdt: f.equity_usdt, pnl_usdt: f.pnl_usdt,
          fees_usdt: f.signed_fees_usdt === null ? null : -Number(f.signed_fees_usdt),
          funding_usdt: f.signed_funding_usdt === null ? null : -Number(f.signed_funding_usdt),
          ai_cost_usd: f.known_jev_usd, trades: f.trades, last_reason: state.last_reason,
          status: state.settled ? "DONE" : snapshot.state.phase === "CLOSING" ? "CLOSING" :
            f.position_btc === null ? "UNKNOWN" : Number(f.position_btc) === 0 ? "WAITING" : "POSITION"};
      });
      const events = snapshot.events.map(event => ({seq: event.seq, at: new Date(event.available_at_ms).toISOString(),
        bot_id: event.bot_id, code: event.code, step: event.step, message: event.message || event.code}));
      return {run: {id: manifest.id, mode: manifest.mode, status: snapshot.state.phase,
        fixture_only: this.packet.evidence_kind === "SYNTHETIC", time_policy: manifest.time_policy, bots, events,
        manifest: {participants: participants.map(p => ({bot_id: p.id, slot: p.slot}))},
        incomplete: snapshot.state.quality.coverage === "GAP"},
      previous: {bots: snapshot.state.bots.map(b => ({id: b.id, position_btc: b.last_known_position_btc}))},
      extras: {key: snapshot.state.seq, replay: true, at: new Date(snapshot.at_ms).toISOString(),
        eventSeq: snapshot.state.seq, still: !snapshot.playing}};
    }
  }

  function spectatorGate(packet, receipt, nowMs) {
    validate(packet); ms(nowMs, "release time");
    if (!object(receipt) || receipt.projection !== "spectator/v1" ||
        receipt.package_hash !== packet.package_hash || receipt.integrity !== "PASS" ||
        receipt.privacy !== "PASS" || receipt.rights !== "APPROVED" || receipt.owner_release !== "APPROVED" ||
        receipt.access !== "PUBLIC" || !Number.isSafeInteger(receipt.release_after_ms) ||
        nowMs < receipt.release_after_ms) return {allowed: false, reason: "Пакет ще не допущений до публічного перегляду"};
    return {allowed: true, reason: "Перевірений пакет глядача"};
  }
  function canonical(value) {
    if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
    if (value && typeof value === "object") {
      return "{" + Object.keys(value).sort().map(key => JSON.stringify(key) + ":" + canonical(value[key])).join(",") + "}";
    }
    return JSON.stringify(value);
  }
  async function packetDigest(packet) {
    validate(packet);
    const payload=clone(packet);delete payload.package_hash;
    const provider=typeof crypto!=="undefined"&&crypto.subtle ? crypto :
      typeof module==="object"&&module.exports ? require("node:crypto").webcrypto : null;
    if(!provider)fail("SHA-256 verifier unavailable");
    const bytes=await provider.subtle.digest("SHA-256",new TextEncoder().encode(canonical(payload)));
    return Array.from(new Uint8Array(bytes),byte=>byte.toString(16).padStart(2,"0")).join("");
  }
  async function openSpectator(packet,receipt,nowMs) {
    const digest=await packetDigest(packet);
    if(digest!==packet.package_hash)return {allowed:false,reason:"Хеш пакета не збігається"};
    return spectatorGate(packet,receipt,nowMs);
  }

  return {SCHEMA, SPEEDS, ACTIONS, validate, initial, reduce, Replay, spectatorGate,canonical,packetDigest,openSpectator};
});
