"use strict";
/* Public strategy passport: static text approved by the author and by the owner, found by the hash of the frozen recipe.
   The recipe hash covers name, author, text and rules of one version, so a card cannot be attached to another strategy.
   Nothing here reads the network, storage, rules or results. The schema is closed (an unknown field is an error), every string
   reaches the page through textContent, and the only attribute taken from data is an internal href that passed the allowlist.
   A draft is never rendered unless the caller asks for a preview, and a preview carries a banner the data cannot remove. */
((root, factory) => {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ArenaPassport = api;
})(typeof window === "undefined" ? {} : window, () => {
  const SCHEMA = "arena-passports/v1";
  const LIMITS = Object.freeze({passports: 64, name: 60, author: 80, model: 150, tagline: 140, idea: 400, item: 300, note: 400,
    signature: 80, label: 60, links: 3, attempts: 1000000, version: 9999});
  // [min, max] items per list; evidence reports results and may therefore carry numbers even when thresholds are withheld.
  const LISTS = Object.freeze({waits_for: [1, 6], when_silent: [1, 5], exits: [1, 6], risk: [1, 5], weak_regimes: [0, 4], evidence: [0, 4]});
  const KINDS = Object.freeze({human: "людина", model: "ШІ-модель", example: "навчальний приклад"});
  const ORIGIN = Object.freeze({
    model: "Назву моделі вказав сам автор подання; платформа її не перевіряє.",
    human: "Авторство вказав сам автор подання; платформа особу не перевіряє.",
    example: "Навчальний приклад показує формат і не є чиєюсь змагальною стратегією."});
  const JEV = Object.freeze({
    none: "Jev не бере участі: рішення ухвалюють лише правила.",
    gate: "Jev може лише дозволити або заборонити вхід, який правила вже допустили.",
    advisor: "Jev радить щодо входу, утримання й виходу (окремий режим із власною таблицею)."});
  const DISCLAIMER = "Це опис правил, а не прогноз і не порада. Результат боїв може бути збитковим; очікування автора — гіпотеза, а не обіцянка. " +
    "Минулі результати не гарантують майбутніх.";
  const MISSING = "Паспорт цієї стратегії ще не опубліковано: автор його не подав або не погодив текст.";
  const DRAFT = "ЧЕРНЕТКА · ще не опубліковано · потрібне схвалення автора й власника";
  const MONTHS = ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"];
  const ENTRY_KEYS = new Set(["recipe_sha256", "version", "replaces", "name", "author", "tagline", "idea", "numbers", "waits_for", "when_silent",
    "exits", "risk", "weak_regimes", "jev", "attempts", "author_note", "evidence", "evidence_asof", "links", "approval"]);
  const HEX64 = /^[0-9a-f]{64}$/;
  // Control, zero-width and bidirectional-override characters can disguise public text (inclusive code point ranges).
  const INVISIBLE = [[0x00, 0x1f], [0x7f, 0x9f], [0xad, 0xad], [0x61c, 0x61c], [0x180e, 0x180e], [0x200b, 0x200f], [0x2028, 0x202e], [0x2060, 0x206f], [0xfeff, 0xfeff]];
  const hidden = value => Array.from(value).some(char => { const code = char.codePointAt(0); return INVISIBLE.some(([from, to]) => code >= from && code <= to); });
  const NBSP = String.fromCharCode(0xa0);
  const LINKISH = /:\/\/|www\.|@[a-z0-9-]+\.[a-z]/i;
  const TOKEN = /[A-Za-z0-9_-]{32,}/;
  // Internal pages only: no scheme, no host, no dot segments.
  const HREF = /^\/(arena|league|review|workshop)(\?[A-Za-z0-9_=&.:%-]{1,200})?(#[A-Za-z0-9_.-]{1,60})?$/;

  const isObject = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const size = value => Array.from(value).length;
  function day(value) {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    const time = Date.parse(value + "T00:00:00Z");
    return Number.isFinite(time) && new Date(time).toISOString().slice(0, 10) === value;
  }

  /* Problems of one passport as [{path, message}]; empty means the entry may be shown (subject to its approval). */
  function check(entry, path = "passport") {
    const errors = [];
    const fail = (where, message) => { errors.push({path: path + where, message}); };
    if (!isObject(entry)) return [{path, message: "must be an object"}];
    for (const key of Object.keys(entry)) if (!ENTRY_KEYS.has(key)) fail("." + key, "unknown field");
    const withheld = entry.numbers === "withheld";
    const text = (value, max, where, numbers = true) => {
      if (typeof value !== "string" || !value) return fail(where, "must be a non-empty text");
      if (value !== value.trim()) fail(where, "has leading or trailing spaces");
      if (size(value) > max) fail(where, "is longer than " + max + " characters");
      if (hidden(value)) fail(where, "contains control or invisible characters");
      if (LINKISH.test(value)) fail(where, "contains a link or address; links belong to the links field");
      if (TOKEN.test(value)) fail(where, "contains a key-like or hash-like token");
      if (!numbers && /[0-9]/.test(value)) fail(where, "contains digits although thresholds are withheld");
    };
    const closed = (value, keys, where) => {
      if (!isObject(value)) { fail(where, "must be an object"); return false; }
      for (const key of Object.keys(value)) if (!keys.includes(key)) fail(where + "." + key, "unknown field");
      return true;
    };

    if (typeof entry.recipe_sha256 !== "string" || !HEX64.test(entry.recipe_sha256)) fail(".recipe_sha256", "must be 64 lowercase hex characters");
    if (!Number.isInteger(entry.version) || entry.version < 1 || entry.version > LIMITS.version) fail(".version", "must be an integer 1.." + LIMITS.version);
    if (entry.replaces !== undefined && (typeof entry.replaces !== "string" || !HEX64.test(entry.replaces) || entry.replaces === entry.recipe_sha256))
      fail(".replaces", "must be the hash of another version");
    text(entry.name, LIMITS.name, ".name");
    if (closed(entry.author, ["name", "kind", "model"], ".author")) {
      text(entry.author.name, LIMITS.author, ".author.name");
      if (!Object.hasOwn(KINDS, entry.author.kind)) fail(".author.kind", "must be one of " + Object.keys(KINDS).join(", "));
      else if (entry.author.kind === "model") text(entry.author.model, LIMITS.model, ".author.model");
      else if (entry.author.model !== undefined && entry.author.model !== "") fail(".author.model", "only a model author names a model");
    }
    if (entry.numbers !== "shown" && entry.numbers !== "withheld") fail(".numbers", "must be shown or withheld");
    text(entry.tagline, LIMITS.tagline, ".tagline", !withheld);
    text(entry.idea, LIMITS.idea, ".idea", !withheld);
    for (const [key, [min, max]] of Object.entries(LISTS)) {
      const list = entry[key];
      if (list === undefined && min === 0) continue;
      if (!Array.isArray(list) || list.length < min || list.length > max) fail("." + key, "must be a list of " + min + ".." + max + " texts");
      else list.forEach((item, index) => text(item, LIMITS.item, "." + key + "[" + index + "]", !withheld || key === "evidence"));
    }
    if (entry.evidence_asof !== undefined && !day(entry.evidence_asof)) fail(".evidence_asof", "must be a calendar date YYYY-MM-DD");
    if (Array.isArray(entry.evidence) && entry.evidence.length && entry.evidence_asof === undefined) fail(".evidence_asof", "is required when evidence is given");
    if ((entry.evidence === undefined || (Array.isArray(entry.evidence) && !entry.evidence.length)) && entry.evidence_asof !== undefined)
      fail(".evidence_asof", "is allowed only with evidence");
    if (closed(entry.jev, ["role", "text"], ".jev")) {
      if (!Object.hasOwn(JEV, entry.jev.role)) fail(".jev.role", "must be one of " + Object.keys(JEV).join(", "));
      text(entry.jev.text, LIMITS.item, ".jev.text", !withheld);
    }
    if (closed(entry.attempts, ["count", "note"], ".attempts")) {
      const count = entry.attempts.count;
      if (count !== null && (!Number.isInteger(count) || count < 0 || count > LIMITS.attempts)) fail(".attempts.count", "must be null or an integer 0.." + LIMITS.attempts);
      text(entry.attempts.note, LIMITS.note, ".attempts.note");
    }
    if (entry.author_note !== undefined && closed(entry.author_note, ["text", "signed_by"], ".author_note")) {
      text(entry.author_note.text, LIMITS.note, ".author_note.text");
      text(entry.author_note.signed_by, LIMITS.signature, ".author_note.signed_by");
    }
    if (entry.links !== undefined) {
      if (!Array.isArray(entry.links) || entry.links.length > LIMITS.links) fail(".links", "must be a list of at most " + LIMITS.links + " links");
      else entry.links.forEach((link, index) => {
        const where = ".links[" + index + "]";
        if (!closed(link, ["label", "href"], where)) return;
        text(link.label, LIMITS.label, where + ".label");
        if (typeof link.href !== "string" || !HREF.test(link.href)) fail(where + ".href", "must be an internal arena, league, review or workshop path");
      });
    }
    if (closed(entry.approval, ["status", "author_approved_at", "owner_approved_at"], ".approval")) {
      const {status, author_approved_at: author, owner_approved_at: owner} = entry.approval;
      for (const [key, value] of [["author_approved_at", author], ["owner_approved_at", owner]])
        if (value !== null && !day(value)) fail(".approval." + key, "must be null or a calendar date YYYY-MM-DD");
      if (status === "APPROVED") {
        if (!day(author) || !day(owner)) fail(".approval", "APPROVED needs both the author and the owner date");
        else if (author > owner) fail(".approval", "the author approves before the owner");
      } else if (status === "DRAFT") {
        if (owner !== null) fail(".approval.owner_approved_at", "a draft has no owner approval yet");
      } else fail(".approval.status", "must be DRAFT or APPROVED");
    }
    return errors;
  }

  /* Whole file: ok only when the schema matches and every entry is sound; approved/drafts count sound entries. */
  function validate(doc) {
    const errors = [];
    if (!isObject(doc)) return {ok: false, errors: [{path: "", message: "must be an object"}], approved: 0, drafts: 0};
    for (const key of Object.keys(doc)) if (!["schema", "updated", "passports"].includes(key)) errors.push({path: key, message: "unknown field"});
    if (doc.schema !== SCHEMA) errors.push({path: "schema", message: "must be " + SCHEMA});
    if (doc.updated !== undefined && !day(doc.updated)) errors.push({path: "updated", message: "must be a calendar date YYYY-MM-DD"});
    if (!Array.isArray(doc.passports) || doc.passports.length > LIMITS.passports) {
      errors.push({path: "passports", message: "must be a list of at most " + LIMITS.passports + " passports"});
      return {ok: false, errors, approved: 0, drafts: 0};
    }
    const seen = new Set();
    let approved = 0, drafts = 0;
    doc.passports.forEach((entry, index) => {
      const found = check(entry, "passports[" + index + "]");
      errors.push(...found);
      const sha = isObject(entry) ? entry.recipe_sha256 : undefined;
      if (typeof sha === "string") {
        if (seen.has(sha)) errors.push({path: "passports[" + index + "].recipe_sha256", message: "duplicate recipe_sha256"});
        seen.add(sha);
      }
      if (!found.length) { if (entry.approval.status === "APPROVED") approved++; else drafts++; }
    });
    return {ok: !errors.length, errors, approved, drafts};
  }

  /* The sound passport of exactly this recipe, or null: unknown or ambiguous hash, a broken entry, or a draft without preview.
     One broken entry never hides the others. */
  function byRecipe(doc, sha, options) {
    if (!isObject(doc) || doc.schema !== SCHEMA || !Array.isArray(doc.passports) || typeof sha !== "string" || !HEX64.test(sha)) return null;
    const found = doc.passports.filter(entry => isObject(entry) && entry.recipe_sha256 === sha);
    if (found.length !== 1 || check(found[0]).length) return null;
    return (isObject(options) && options.preview === true) || found[0].approval.status === "APPROVED" ? found[0] : null;
  }

  /* Differences between a passport and the catalogue strategy it claims to describe; empty means they agree. */
  function mismatches(passport, strategy) {
    if (!isObject(passport) || !isObject(strategy)) return ["nothing to compare"];
    const found = [];
    const author = isObject(strategy.author) ? strategy.author : {};
    const same = (label, mine, theirs) => { if (mine !== theirs) found.push(label + ": passport " + JSON.stringify(mine) + ", catalogue " + JSON.stringify(theirs)); };
    same("recipe_sha256", passport.recipe_sha256, strategy.recipe_sha256);
    same("name", passport.name, strategy.name);
    same("author.name", passport.author?.name, author.name);
    same("author.kind", passport.author?.kind, author.kind);
    if (passport.author?.kind === "model") same("author.model", passport.author.model, author.model);
    if (strategy.version !== undefined) same("version", passport.version, strategy.version);
    if (author.attempts !== undefined) same("attempts.count", passport.attempts?.count, author.attempts);
    return found;
  }

  const grouped = number => String(number).replace(/\B(?=(\d{3})+(?!\d))/g, NBSP);
  function variants(number) {
    const last = number % 10, tens = number % 100;
    return grouped(number) + NBSP + (last === 1 && tens !== 11 ? "варіант" : last >= 2 && last <= 4 && (tens < 12 || tens > 14) ? "варіанти" : "варіантів");
  }
  const dateText = value => { const [year, month, date] = value.split("-"); return Number(date) + " " + MONTHS[Number(month) - 1] + " " + year; };
  const make = (doc, tag, className, text) => {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  /* One card, or null when the passport is not sound or not approved (and no preview was asked for). Strings go in as text only. */
  function render(doc, passport, options) {
    if (!isObject(passport) || check(passport).length) return null;
    const asked = isObject(options) ? options : {};
    const approved = passport.approval.status === "APPROVED";
    if (!approved && asked.preview !== true) return null;
    const level = Number.isInteger(asked.headingLevel) && asked.headingLevel >= 2 && asked.headingLevel <= 5 ? asked.headingLevel : 3;
    const card = make(doc, "article", "passport");
    card.setAttribute("data-state", approved ? "approved" : "draft");
    card.setAttribute("aria-label", "Паспорт стратегії: " + passport.name);
    if (!approved) card.append(make(doc, "p", "passport-banner", DRAFT));
    const kind = passport.author.kind;
    const head = make(doc, "header", "passport-head");
    head.append(make(doc, "h" + level, "passport-title", passport.name),
      make(doc, "p", "passport-author", ["Автор: " + passport.author.name, KINDS[kind], kind === "model" ? passport.author.model : ""].filter(Boolean).join(" · ")),
      make(doc, "p", "passport-origin", ORIGIN[kind]));
    card.append(head, make(doc, "p", "passport-tagline", passport.tagline));
    if (passport.numbers === "withheld") card.append(make(doc, "p", "passport-note", "Числові пороги автор не розкриває."));
    const section = (key, title, ...children) => {
      const node = make(doc, "section", "passport-section passport-part-" + key);
      node.append(make(doc, "h" + (level + 1), "passport-heading", title), ...children);
      card.append(node);
    };
    const list = items => {
      const node = make(doc, "ul", "passport-list");
      for (const item of items) node.append(make(doc, "li", "", item));
      return node;
    };
    section("idea", "Ідея", make(doc, "p", "", passport.idea));
    section("waits", "Чого чекає", list(passport.waits_for));
    section("silent", "Коли не торгує", list(passport.when_silent));
    section("exits", "Як виходить", list(passport.exits));
    section("risk", "Ризик", list(passport.risk));
    if (passport.weak_regimes?.length) section("weak", "Де слабка", list(passport.weak_regimes));
    section("jev", "Роль Jev", make(doc, "p", "passport-jev-role", JEV[passport.jev.role]), make(doc, "p", "", passport.jev.text));
    const count = passport.attempts.count;
    section("attempts", "Скільки варіантів перебрано", make(doc, "p", "passport-attempts", count === null ? "Кількість не зафіксовано." : variants(count) + "."),
      make(doc, "p", "", passport.attempts.note));
    if (passport.author_note) section("author", "Автор сам оцінює", make(doc, "blockquote", "passport-quote", passport.author_note.text),
      make(doc, "p", "passport-signed", "— " + passport.author_note.signed_by));
    if (passport.evidence?.length) section("evidence", "Що вже відомо про результати · станом на " + dateText(passport.evidence_asof), list(passport.evidence));
    const fine = ["Версія " + passport.version, "відбиток рецепта " + passport.recipe_sha256.slice(0, 12) + "…"];
    if (passport.replaces) fine.push("замінює версію з відбитком " + passport.replaces.slice(0, 12) + "…");
    card.append(make(doc, "p", "passport-fine", fine.join(" · ")));
    if (passport.links?.length) {
      const links = make(doc, "ul", "passport-links");
      for (const link of passport.links) {
        const anchor = make(doc, "a", "passport-link", link.label), item = make(doc, "li", "");
        anchor.setAttribute("href", link.href);
        item.append(anchor);
        links.append(item);
      }
      card.append(links);
    }
    card.append(make(doc, "p", "passport-disclaimer", DISCLAIMER));
    return card;
  }

  const renderMissing = doc => make(doc, "p", "passport-missing", MISSING);
  /* What a page shows for a strategy: its approved card, or the plain "not published" note. */
  function view(doc, data, sha, options) {
    const passport = byRecipe(data, sha, options);
    return (passport && render(doc, passport, options)) || renderMissing(doc);
  }

  return {SCHEMA, LIMITS, LISTS, KINDS, JEV, DISCLAIMER, MISSING, DRAFT, validate, check, byRecipe, mismatches, render, renderMissing, view};
});
