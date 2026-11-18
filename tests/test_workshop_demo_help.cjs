"use strict";
// The workshop's Demo mode hint describes the chosen pinned recipe, not always the V8 short / partial / tail one. Page
// fixture only: no browser, server, network, keys or financial actions; Start is never clicked.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { demoModeHelp } = require("./v8_ui/workshop-page-fixture.js");

const ROOT = path.join(__dirname, "..");
const recipe = name => JSON.parse(fs.readFileSync(path.join(ROOT, "03_For_Client_Deliverables", name), "utf8"));
const LONG = "46adb4156dbbbe8aeaa457b1d4a46aa75571fcc157f08ba81029f8af4a53157f";  // demo_execution.ETH_LONG_SHA256
const V8 = "01dafc62827d69fab9e12cd578d5f647364fba549c93a44e9d119198f5fdc9bb";  // demo_execution.RECIPE_SHA256

test("mechanics on the R2 ETH long recipe says long, post-only and no partial exit", async () => {
  const { help, version, mode } = await demoModeHelp({ recipe: recipe("R2_DEMO_ETH_POST_ONLY_LONG.json"), recipeSha256: LONG });
  assert.equal(version, "a".repeat(32));
  assert.equal(mode, "demo_mechanics");
  assert.match(help, /^Один long-вхід за правилами, 100 Demo USDT, вікно входу 5 хв\. Вхід post-only: ордер чекає до 600 с/);
  assert.match(help, /Перевіряємо SL і вихід за ціллю або через 300 с утримання\./);
  assert.doesNotMatch(help, /short|частков/);
});

test("mechanics on the V8 recipe keeps its short / partial / tail hint", async () => {
  const { help } = await demoModeHelp({ recipe: recipe("V8_DEMO_SHORT_PARTIAL_TAIL.json"), recipeSha256: V8 });
  assert.equal(help, "Один short-вхід за правилами, 100 Demo USDT, вікно входу 5 хв. Перевіряємо SL, частковий вихід і завершення "
    + "позиції після дедлайну. Без запитів або витрат Jev. Це перевірка виконання, не результат змагання стратегій.");
});

test("the Jev mode hints name neither a side nor the V8 recipe alone", async () => {
  const pinned = await demoModeHelp({ recipe: recipe("R2_DEMO_ETH_POST_ONLY_LONG.json"), recipeSha256: LONG, mode: "shared_jev" });
  assert.equal(pinned.mode, "shared_jev");
  assert.match(pinned.help, /^Jev може відхилити вхід\./);
  assert.doesNotMatch(pinned.help, /short|частков/);
  const other = await demoModeHelp({ recipe: recipe("R2_DEMO_ETH_POST_ONLY_LONG.json"), recipeSha256: LONG, pinned: false });
  assert.equal(other.mode, "shared_jev");
  assert.match(other.help, /V8 short \/ partial \/ tail і R2 ETH post-only/);
});
