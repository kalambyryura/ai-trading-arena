"use strict";
/* Verify the unmodified wire bundle. Keep numeric tokens: Python 3.0 and JS 3 hash differently. */
((root, factory) => {
  const api = factory(typeof module === "object" && module.exports ? require("./review-model.js") : root.ArenaReviewModel);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ArenaReviewVerify = api;
})(typeof window === "undefined" ? {} : window, Model => {
  const fail = text => {throw new Error(text);};
  const sortKey = (a, b) => {
    const left = Array.from(a, c => c.codePointAt(0)), right = Array.from(b, c => c.codePointAt(0));
    for (let i = 0; i < Math.min(left.length, right.length); i++) if (left[i] !== right[i]) return left[i] - right[i];
    return left.length - right.length;
  };
  function canonicalWire(raw, omitRoot = []) {
    if (typeof raw !== "string") fail("Потрібен вихідний JSON пакета");
    let at = 0;
    const ws = () => {while (/\s/.test(raw[at] || "") && at < raw.length) at++;};
    function string() {
      const start = at++; let escaped = false;
      while (at < raw.length) {
        const char = raw[at++];
        if (!escaped && char === '"') return JSON.parse(raw.slice(start, at));
        if (!escaped && char === "\\") escaped = true; else escaped = false;
      }
      fail("Незавершений JSON рядок");
    }
    function value(depth) {
      if (depth > 100) fail("Надмірна вкладеність JSON");
      ws(); const char = raw[at];
      if (char === '"') return JSON.stringify(string());
      if (char === "{") {
        at++; ws(); const rows = [], seen = new Set();
        if (raw[at] !== "}") while (at < raw.length) {
          ws(); if (raw[at] !== '"') fail("Невалідний JSON ключ");
          const key = string(); if (seen.has(key)) fail("Повторений JSON ключ: " + key); seen.add(key);
          ws(); if (raw[at++] !== ":") fail("Невалідний JSON об'єкт");
          const child = value(depth + 1);
          if (depth !== 0 || !omitRoot.includes(key)) rows.push([key, child]);
          ws(); if (raw[at] === "}") break;
          if (raw[at++] !== ",") fail("Невалідний JSON об'єкт");
        }
        if (raw[at++] !== "}") fail("Незавершений JSON об'єкт");
        return "{" + rows.sort((a, b) => sortKey(a[0], b[0])).map(([key, child]) => JSON.stringify(key) + ":" + child).join(",") + "}";
      }
      if (char === "[") {
        at++; ws(); const rows = [];
        if (raw[at] !== "]") while (at < raw.length) {
          rows.push(value(depth + 1)); ws(); if (raw[at] === "]") break;
          if (raw[at++] !== ",") fail("Невалідний JSON масив");
        }
        if (raw[at++] !== "]") fail("Незавершений JSON масив");
        return "[" + rows.join(",") + "]";
      }
      const token = /^(?:-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null)/.exec(raw.slice(at));
      if (!token) fail("Невалідний JSON token");
      at += token[0].length; return token[0];
    }
    const output = value(0); ws(); if (at !== raw.length) fail("Додаткові дані після JSON");
    return output;
  }
  async function sha256(text) {
    const crypto = globalThis.crypto?.subtle ? globalThis.crypto :
      typeof require === "function" ? require("node:crypto").webcrypto : null;
    if (!crypto?.subtle) fail("SHA-256 недоступний у цьому браузері");
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
    return Array.from(new Uint8Array(digest), x => x.toString(16).padStart(2, "0")).join("");
  }
  function facts(bundle) {
    if (bundle?.schema_version !== 1 || !bundle.match || !Array.isArray(bundle.participants) ||
        !Array.isArray(bundle.events)) fail("Непідтримуваний BattleReview schema_version");
    if (!Number.isSafeInteger(bundle.integrity?.source_event_count) ||
        bundle.integrity.source_event_count !== bundle.events.length) fail("Кількість подій не збігається");
    const seqs = new Set(); let previous = null;
    for (const event of bundle.events) {
      if (!Number.isSafeInteger(event.seq) || event.seq < 1 || seqs.has(event.seq) ||
          previous !== null && event.seq !== previous + 1) fail("Непослідовні події пакета");
      seqs.add(event.seq); previous = event.seq;
    }
    const ids = new Set();
    for (const bot of bundle.participants) {
      if (!bot.id || ids.has(bot.id)) fail("Повторений або відсутній учасник"); ids.add(bot.id);
      const ledger = Array.isArray(bot.trade_ledger) ? bot.trade_ledger : bot.trade_ledger?.rows || [];
      if (!Array.isArray(ledger)) fail("Невалідний ledger");
      for (const row of ledger) if (!seqs.has(row.seq)) fail("Відсутній доказ для рядка угоди");
      for (const trip of bot.trade_ledger?.round_trips || []) {
        if (!seqs.has(trip.entry_seq) || trip.closed && !seqs.has(trip.close_seq)) fail("Відсутній доказ циклу");
      }
      const c = bot.costs || {}, components = [c.gross_pnl_usdt, c.signed_fees_usdt, c.signed_funding_usdt];
      if (components.every(x => typeof x === "string") && typeof c.net_usdt === "string") {
        if (Model.decimalCompare(Model.decimalSum(components), c.net_usdt) !== 0) fail("Місток gross/fees/funding/net не збігається");
      }
    }
    return {events: bundle.events.length, participants: bundle.participants.length};
  }
  async function verifyReview(raw) {
    try {
      const canonical = canonicalWire(raw, ["integrity"]), bundle = JSON.parse(raw);
      const expected = bundle.integrity?.bundle_sha256;
      if (typeof expected !== "string" || !/^[a-f0-9]{64}$/.test(expected)) fail("Відсутній SHA-256 пакета");
      const actual = await sha256(canonical); if (actual !== expected) fail("SHA-256 пакета не збігається");
      return {verified: true, bundle_sha256: actual, ...facts(bundle)};
    } catch (error) {return {verified: false, reason: error.message};}
  }
  return {canonicalWire, sha256, facts, verifyReview};
});
