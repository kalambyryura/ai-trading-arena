"use strict";
// R2.1.4: the workshop page posts a new version with the recipe's numbers exactly as written. Page fixture only: no
// browser, server, network, keys or financial actions.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { saveImported } = require("./v8_ui/workshop-page-fixture.js");

const ROOT = path.join(__dirname, "..");
const read = relative => fs.readFileSync(path.join(ROOT, relative), "utf8");
const H10 = "team/authors/sonnet/candidates/H10_ETH/recipe.json";  // = the kits' H10_ETH_recipe.json (504be5db...)

test("H10 through the page keeps mult 3.0 (v4 dcf48666...) behind the same guard headers", async () => {
  const text = read(H10);
  const { posts } = await saveImported({ recipeText: text, token: "page-token-h10", generation: 11 });
  assert.equal(posts.length, 1);
  const [sent] = posts;
  assert.equal(sent.path, "/api/workshop/versions");
  assert.equal(sent.headers["X-Panel-Token"], "page-token-h10");
  assert.equal(sent.headers["Content-Type"], "application/json");
  assert.ok(sent.body.includes('"mult":3.0'), "JSON.stringify would have written 3");
  assert.ok(Buffer.byteLength(sent.body) <= 131072);
  const body = JSON.parse(sent.body);
  assert.deepEqual(Object.keys(body), ["generation", "strategy", "parent_id"]);
  assert.deepEqual([body.generation, body.parent_id], [11, null]);
  assert.deepEqual(body.strategy, { ...JSON.parse(text), confirmed: true });
});

test("recipes without whole-number floats are posted byte for byte as before", async () => {
  for (const file of ["03_For_Client_Deliverables/R2_DEMO_ETH_POST_ONLY_LONG.json", "03_For_Client_Deliverables/AUTHOR_RECIPE_EXAMPLE_V8.json"]) {
    const text = read(file);
    const { posts } = await saveImported({ recipeText: text, generation: 7 });
    const strategy = JSON.parse(text);
    strategy.confirmed = true;  // parseRecipe sets the checkbox in place, keeping the key's position
    assert.equal(posts[0].body, JSON.stringify({ generation: 7, strategy, parent_id: null }), file);
  }
});

test("edge cases: the checkbox, a nested confirmed key, number lexemes, escapes, whitespace and duplicates", async () => {
  const text = '{\n  "name": "\u0416 \\"q\\"",\t"author": {"kind": "human", "confirmed": 1.0},\r\n "description": "d",'
    + ' "original_text": "t", "rules": {"a": 1.50, "b": 2e3, "c": -0.0, "d": 12345678901234567890, "e": [1.0, {"f": 0.10}]},'
    + ' "confirmed": true, "confirmed": false }';
  const { posts } = await saveImported({ recipeText: text, generation: 3, confirm: false });
  assert.equal(posts[0].body, '{"generation":3,"strategy":{"name":"Ж \\"q\\"","author":{"kind":"human","confirmed":1.0},'
    + '"description":"d","original_text":"t","rules":{"a":1.50,"b":2e3,"c":-0.0,"d":12345678901234567890,"e":[1.0,{"f":0.10}]},'
    + '"confirmed":false,"confirmed":false},"parent_id":null}');
  const checked = await saveImported({ recipeText: text, generation: 3, confirm: true });
  assert.ok(checked.posts[0].body.includes('"original_text":"t","rules"') && checked.posts[0].body.endsWith('"confirmed":true,"confirmed":true},"parent_id":null}'));
  for (const broken of [text.replace('"t",', '"t",,'), text.replace(/}\s*$/, ""), "[1.0]"]) {
    const result = await saveImported({ recipeText: broken });
    assert.equal(result.posts.length, 0, broken.slice(-20));
  }
});

test("the selected version shows the v4 recipe hash beside the recipe fingerprint", () => {
  const page = read("web/workshop.js");
  assert.ok(page.includes('version.v4_recipe_sha256?`v4-рецепт: ${safe(version.v4_recipe_sha256)}`:null'));
  assert.ok(page.includes('body:typeof body==="string"?body:JSON.stringify(body)'), "only the exact envelope is sent as text");
});
