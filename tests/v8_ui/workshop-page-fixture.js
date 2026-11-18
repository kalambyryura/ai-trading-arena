"use strict";
// Workshop page fixture (R2.1.4): runs web/workshop.js in a vm with a stub DOM and a stub fetch, imports a recipe file
// through the page's own #recipe-file handler, sets #confirm-match, clicks #save-version and returns the captured POST
// (path, headers, body). No browser, server, network, keys or financial actions. Used by
// tests/test_workshop_exact_numbers.cjs and, as a command (recipe token generation), by tests/test_workshop.py, which
// sends the same bytes to a real server. demoModeHelp loads the page with web/demo-execution.js and a stub Demo state
// and returns the #demo-mode-help text for the chosen recipe and mode (tests/test_workshop_demo_help.cjs).
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const PAGE = path.join(__dirname, "..", "..", "web", "workshop.js");
const DEMO_UI = path.join(__dirname, "..", "..", "web", "demo-execution.js");

const universal = new Proxy(function () {}, {
  get: (t, p) => (p === Symbol.toPrimitive ? () => "" : p === "then" ? undefined : universal),
  apply: () => universal,
  construct: () => universal,
  set: () => true,
});

function element(id) {
  const listeners = {};
  const base = {
    id, value: "", checked: false, textContent: "", hidden: false, disabled: false, dataset: {}, style: {}, listeners,
    addEventListener(type, fn) { (listeners[type] || (listeners[type] = [])).push(fn); },
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
  };
  return new Proxy(base, {
    get: (t, p) => (p in t ? t[p] : typeof p === "symbol" ? undefined : universal),
    set: (t, p, v) => { t[p] = v; return true; },
  });
}

async function loadPage({ token, workshop, exchange, pagePath = PAGE, demoUi = false }) {
  const elements = {};
  const byId = id => elements[id] || (elements[id] = element(id));
  const posts = [];
  const reply = data => ({ ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(data)) });
  const fetch = async (url, options = {}) => {
    if (options.method === "POST") {
      posts.push({ path: url, headers: options.headers, body: options.body });
      return reply({ id: "f".repeat(32), version: 1 });
    }
    if (url === "/api/workshop/state") return reply(workshop);
    if (url === "/api/exchange/state") return reply(exchange);
    return reply({});
  };
  const document = {
    getElementById: byId,
    querySelector: sel => (String(sel).includes("panel-token") ? { content: token } : element("q")),
    createElement: tag => element(tag), createElementNS: (ns, tag) => element(tag), createTextNode: () => element("text"),
    body: element("body"),
  };
  const shell = { ArenaWorkshopToolsUI: { mount: () => ({ selectVersion() {} }) }, addEventListener() {} };
  if (demoUi) shell.DemoExecutionUI = require(DEMO_UI);
  const window = new Proxy(shell,
    { get: (t, p) => (p in t ? t[p] : typeof p === "symbol" ? undefined : universal) });
  const context = vm.createContext({
    window, document, location: { search: "" }, URLSearchParams, fetch, Intl, console, AbortController,
    navigator: { clipboard: { writeText: async () => {} } }, setTimeout, clearTimeout, setInterval: () => 1, clearInterval() {},
    Blob, URL, TextEncoder,
  });
  vm.runInContext(fs.readFileSync(pagePath, "utf8"), context, { filename: pagePath });
  const flush = () => new Promise(resolve => setTimeout(resolve, 0));
  for (let i = 0; i < 5; i++) await flush();
  return { byId, posts, flush };
}

async function saveImported({ recipeText, fileName = "recipe.json", token = "page-token", generation = 7, confirm = true, pagePath = PAGE }) {
  const { byId, posts, flush } = await loadPage({ token, pagePath,
    workshop: { generation, versions: [], datasets: [], trials: [], contract: {} },
    exchange: { generation: 1, profiles: [], history: [], active: {}, budgets: {} } });
  const file = { name: fileName, text: async () => recipeText };
  await Promise.all(byId("recipe-file").listeners.change.map(fn => fn({ target: { files: [file] } })));
  byId("confirm-match").checked = confirm;
  await Promise.all(byId("save-version").listeners.click.map(fn => fn()));
  for (let i = 0; i < 5; i++) await flush();
  return { posts, editor: byId("recipe-json").value, error: byId("recipe-error").textContent };
}

// One saved version of the recipe, the server's pinned mechanics scope for it and a ready Demo profile; the page itself
// picks the version, and a change of #demo-execution-mode re-renders the hint as a click on the select would.
async function demoModeHelp({ recipe, recipeSha256, mode = "demo_mechanics", pinned = true }) {
  const version = { id: "a".repeat(32), version: 1, status: "draft", name: recipe.name, recipe_sha256: recipeSha256, strategy: recipe };
  const check = { version: 1, execution_mode: "demo_mechanics", recipe_sha256: recipeSha256, context: "workshop_demo", mode: "OKX_DEMO",
    budget_usdt: 100, duration_seconds: 300, maximum_entry_attempts: 1, uses_jev: false, label: "pinned" };
  const { byId, flush } = await loadPage({ token: "page-token", demoUi: true,
    workshop: { generation: 7, versions: [version], datasets: [], trials: [], contract: {} },
    exchange: { generation: 1, history: [], active: {}, budgets: {}, demo_execution_checks: pinned ? [check] : [],
      profiles: [{ id: "demo", label: "Demo", environment: "OKX_DEMO", configured: true, ready: true }] } });
  byId("demo-execution-mode").value = mode;
  for (const fn of byId("demo-execution-mode").listeners.change || []) fn();
  await flush();
  return { help: byId("demo-mode-help").textContent, version: byId("demo-version").value, mode: byId("demo-execution-mode").value };
}

module.exports = { saveImported, demoModeHelp, PAGE };

if (require.main === module) {
  const [recipePath, token, generation, confirm] = process.argv.slice(2);
  saveImported({ recipeText: fs.readFileSync(recipePath, "utf8"), fileName: path.basename(recipePath),
    token: token || "page-token", generation: Number(generation || 7), confirm: confirm !== "0" })
    .then(result => process.stdout.write(JSON.stringify(result)))
    .catch(error => { console.error(error); process.exit(1); });
}
