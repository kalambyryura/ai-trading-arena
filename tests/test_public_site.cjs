"use strict";
/* Public static site view-model and textContent DOM. No browser, requests, tokens or financial routes. */
const test = require("node:test"), assert = require("node:assert/strict");
const Site = require("../web/public-site.js");

class Element {
  constructor(tag) { this.tag = tag; this.className = ""; this.textContent = ""; this.children = []; this.dataset = {}; this.title = ""; this.href = null; this.scope = null; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
}
function document() {
  const ids = {};
  for (const id of ["public-note", "public-latest", "public-standings", "public-battles", "public-guide", "public-built", "public-site-data"]) ids[id] = new Element("div");
  return { createElement: tag => new Element(tag), getElementById: id => ids[id] || null, ids };
}
const text = node => [node.textContent, ...node.children.map(text)].join(" ");
const find = (node, predicate) => [...(predicate(node) ? [node] : []), ...node.children.flatMap(child => find(child, predicate))];

const ID = "a".repeat(32), OTHER = "b".repeat(32), COHORT = "c".repeat(64);
const participant = { id: "a1", name: "Стратегія A", author_name: "Автор A", author_kind: "model", model: "model-x", recipe_sha256: "d".repeat(64), version: 1 };
const battle = (id, extra = {}) => ({ id, kind: "exchange", mode: "OKX_LIVE", status: "DONE", cohort: COHORT, allocation_usdt: "100", created_at: "2026-10-06T10:00:00Z",
  duration_seconds: 86400, execution_mode: "rules", competitive_validity: "ELIGIBLE", settlement: "SETTLED", incomplete: false, display_label: "Live-бій №1 · 24 год",
  score: { status: "SOLE_WIN", sole_winner: "cash", shared_top: ["cash"], known_results: [{ id: "a1", net_usdt: "-0.5" }], ordered: ["cash", "a1"] },
  participants: [participant], package_hash: "f".repeat(64), evidence_kind: "ACTUAL", page: "battle-" + id + ".html", ...extra });
const league = {
  battles: [battle(ID), battle(OTHER, { status: "RUNNING", display_label: "Live-бій №2 · 24 год", created_at: "2026-10-07T10:00:00Z", competitive_validity: "INELIGIBLE",
    score: { status: "INCOMPLETE", sole_winner: null, shared_top: [], known_results: [] } })],
  frequencies: [{ ...participant, cohort: COHORT, comparable_battles: 1, sole_wins: 0, shared_top: 0, above_cash: 0, below_cash: 1, at_cash: 0, net_sum_usdt: "-0.5", net_mean_usdt: "-0.5" },
    { id: "cash", cohort: COHORT, recipe_sha256: "cash", version: 0, name: "Cash", author_name: null, author_kind: "baseline", model: null, comparable_battles: 1, sole_wins: 1, shared_top: 0, above_cash: 0, below_cash: 0, at_cash: 1, net_sum_usdt: "0", net_mean_usdt: "0" }],
  cohorts: [{ cohort: COHORT, mode: "OKX_LIVE", mode_label: "Live-бій", allocation_usdt: "100", duration_seconds: 86400, duration_label: "24 год", execution_mode: "rules", participants: 1, battles: 1, first_at: "2026-10-06T10:00:00Z", last_at: "2026-10-06T10:00:00Z" }]
};
const facts = { match: { id: ID, mode: "OKX_LIVE", status: "DONE", incomplete: false, started_at: "2026-10-06T10:00:00Z", admission_ends_at: "2026-10-07T10:00:00Z", ended_at: "2026-10-07T10:00:00Z", duration_seconds: 86400 },
  participants: [{ id: "a1", name: "Стратегія A", allocation_usdt: "100",
    funnel: { entry: { checks: 10, signals: 1, signals_unknown: 0, jev_decisions: 0, jev_allows: 0, jev_vetoes: 0, rechecks: 0, recheck_vetoes: 0 }, entry_filled_orders: 1, round_trips: 1, order_intents: 1, no_signal_unknown: 0, failed_features: {} },
    costs: { gross_pnl_usdt: "-0.4", signed_fees_usdt: "-0.1", signed_funding_usdt: "0", net_usdt: "-0.5", complete: true, jev: { known_cost_usd: "0", unknown_billing_requests: 0, complete: true } },
    quality: { settlement: { status: "SETTLED" }, availability: { status: "COMPLETE" }, competitive_validity: { status: "ELIGIBLE" } } }],
  cash: { name: "Cash", net_usdt: "0" }, history_complete: true, events: [], source: "battles/" + ID + "/review.json" };
const data = { battles: { schema: "arena-public-site/v1", built_at_ms: Date.parse("2026-10-07T12:00:00Z"), indexable: false, league }, latest: { id: ID, facts } };

test("battle list: mode in words, result from the shared score, relative page only for a safe id", () => {
  const items = Site.battleList(league);
  assert.equal(items.length, 2);
  assert.equal(items[0].modeText, "LIVE · реальні кошти на біржі OKX");
  assert.equal(items[0].page, "battle-" + ID + ".html");
  assert.match(items[0].result, /^Переміг Cash/);
  assert.equal(items[0].utc, "2026-10-06 10:00 UTC");
  assert.equal(items[0].duration, "24 год");
  assert.deepEqual(items[0].participants, ["Стратегія A"]);
  assert.equal(items[1].result, "Бій ще не завершено");
  assert.equal(items[1].statusText, "триває");
  assert.equal(Site.battleList({ battles: [battle("../owner", { page: "battle-../owner.html" })] })[0].page, null);
  assert.equal(Site.battleList({ battles: [battle(ID, { page: "/api/review" })] })[0].page, null);
});

test("standings: Cash ranks by Σnet like every strategy, few battles give no verdict", () => {
  const model = Site.standings(league);
  assert.equal(model.groups.length, 1);
  const group = model.groups[0];
  assert.deepEqual(group.rows.map(r => [r.id, r.rank, r.verdict]), [["cash", 1, "BASELINE"], ["a1", 2, "FEW_DATA"]]);
  for (const part of ["LIVE · реальні кошти", "24 год", "100 USDT на учасника", "1 стратегія + Cash", "лише правила"]) assert.ok(group.title.includes(part), part);
  assert.equal(group.rows[1].author, "Автор A · ШІ-модель · model-x");
  assert.equal(group.rows[0].author, "не торгує · net = 0");
  assert.match(model.headline, /^Вище за Cash за сумою net: 0 з 1 /);
  assert.ok(model.headline.includes("після 30 боїв"));
  assert.match(Site.standings({}).headline, /^Ще немає порівнянних/);
});

test("latest card uses the shared facts model and the Cash baseline", () => {
  const card = Site.latestCard(data);
  assert.equal(card.id, ID);
  assert.ok(card.final);
  assert.match(card.headline, /Cash — єдиний переможець/);
  assert.deepEqual(card.rows.map(r => [r.id, r.place]), [["cash", 1], ["a1", 2]]);
  assert.equal(card.rows[1].author, "Автор A");
  assert.equal(card.item.page, "battle-" + ID + ".html");
  assert.equal(Site.latestCard({ battles: { league } }), null);
});

test("render: textContent only, relative links only, honest note, guide and noindex footer", () => {
  const doc = document();
  const model = Site.render(doc, data);
  assert.equal(model.battles.length, 2);
  const everything = Object.values(doc.ids).map(text).join(" ");
  for (const forbidden of ["/api/", "panel-token", "http", "Керування", "<"]) assert.ok(!everything.includes(forbidden), forbidden);
  assert.ok(text(doc.ids["public-note"]).includes("поки жодна не довела перевагу над Cash"));
  const links = Object.values(doc.ids).flatMap(node => find(node, n => n.tag === "a"));
  assert.ok(links.length >= 2);
  for (const link of links) assert.match(link.href, /^battle-[a-z0-9]{32}\.html$/);
  assert.ok(text(doc.ids["public-latest"]).includes("Останній завершений бій"));
  assert.ok(text(doc.ids["public-latest"]).includes("LIVE · реальні кошти"));
  assert.ok(text(doc.ids["public-standings"]).includes("Таблиця ліги"));
  assert.ok(text(doc.ids["public-battles"]).includes("Усі бої (2)"));
  assert.equal(doc.ids["public-guide"].children.length, 6);
  assert.ok(text(doc.ids["public-built"]).includes("2026-10-07 12:00 UTC"));
  assert.ok(text(doc.ids["public-built"]).includes("не індексується"));
  const badges = Object.values(doc.ids).flatMap(node => find(node, n => n.className === "public-mode"));
  assert.ok(badges.every(b => b.dataset.mode === "OKX_LIVE"));
});

test("mount shows a plain error instead of a blank page when the embedded record is broken", () => {
  const doc = document();
  doc.ids["public-site-data"].textContent = "{not json";
  Site.mount(doc);
  assert.match(doc.ids["public-note"].textContent, /^Сторінку не вдалося показати/);
  doc.ids["public-site-data"].textContent = JSON.stringify(data);
  Site.mount(doc);
  assert.ok(text(doc.ids["public-battles"]).includes("Live-бій №1"));
});

test("money and time words", () => {
  assert.deepEqual(["0.01", "-0.02", "0", "0.00004", null].map(v => Site.usdt(v)), ["+0,0100", "−0,0200", "0,0000", "+0,00004", "—"]);
  assert.equal(Site.usdt("0.5", false), "0,5000");
  const stamp = Site.when("2026-10-06T10:00:00Z");
  assert.equal(stamp.utc, "2026-10-06 10:00 UTC");
  assert.ok(stamp.kyiv === "" || stamp.kyiv.includes("13:00"), stamp.kyiv);
  assert.equal(Site.when("nonsense").utc, "—");
});

test("latest card polish: Cash shows a plain 0 and why it led, small shares keep their digits", () => {
  const card = Site.latestCard(data);
  const [cash, a1] = card.rows;
  assert.equal(cash.money, "0");
  assert.equal(cash.reason, "інші учасники закінчили в мінусі");
  assert.equal(a1.money, "−0,5 · −0,50" + " " + "%");
  assert.equal(a1.reason, "");
  assert.deepEqual([Site.stakeShare("-0.007", "100"), Site.stakeShare("0", "100"), Site.stakeShare("0.5", "100"), Site.stakeShare("0.0000001", "100"), Site.stakeShare("1", null)],
    ["−0,007" + " " + "%", "", "+0,50" + " " + "%", "", ""]);
});
test("render polish: column labels on every data cell, no repeated duration, guide opened for new visitors", () => {
  const doc = document();
  const details = { tagName: "DETAILS", open: false };
  doc.ids["public-guide"].parentElement = details;
  Site.render(doc, data);
  assert.equal(details.open, true);
  const cells = Object.values(doc.ids).flatMap(node => find(node, n => n.tag === "td"));
  assert.ok(cells.length > 0 && cells.every(cell => typeof cell.dataset.label === "string" && cell.dataset.label.length > 0));
  const meta = find(doc.ids["public-latest"], n => n.className === "public-meta")[0].textContent;
  assert.equal(meta.split("24 год").length - 1, 1, meta);
  for (const item of find(doc.ids["public-battles"], n => n.className === "public-battle")) assert.ok(text(item).split("24 год").length - 1 <= 1, text(item));
});
