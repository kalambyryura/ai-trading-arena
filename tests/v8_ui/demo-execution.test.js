"use strict";
const {test}=require("node:test"),assert=require("node:assert/strict");
const fs=require("node:fs"),path=require("node:path");
const ui=require("../../web/demo-execution.js");
const cap={version:1,execution_mode:"demo_mechanics",context:"workshop_demo",mode:"OKX_DEMO",uses_jev:false,budget_usdt:100,duration_seconds:300,recipe_sha256:"pinned-recipe"};
const recipe={recipe_sha256:"pinned-recipe"};
test("mechanics requires explicit choice; normal mode retains paid guard",()=>{
  assert.deepEqual(ui.settings(recipe,cap,"shared_jev",true),{available:true,mechanics:false,mode:"shared_jev",locked:false,paidBlocked:true});
  assert.deepEqual(ui.settings(recipe,cap,"demo_mechanics",true),{available:true,mechanics:true,mode:"demo_mechanics",locked:true,paidBlocked:false});
});
test("other recipe, old server, Live descriptor and changed bounds cannot select mechanics",()=>{
  for(const [version,descriptor] of [[{recipe_sha256:"other"},cap],[recipe,null],[recipe,{...cap,mode:"OKX_LIVE"}],[recipe,{...cap,budget_usdt:200}],[recipe,{...cap,duration_seconds:900}],[recipe,{...cap,uses_jev:true}]]){
    const state=ui.settings(version,descriptor,"demo_mechanics",true);
    assert.equal(state.available,false);assert.equal(state.mechanics,false);assert.equal(state.mode,"shared_jev");assert.equal(state.paidBlocked,true);
  }
});
test("operating labels distinguish none-model Demo from strategy gate",()=>{
  assert.match(ui.modeLabel("demo_mechanics"),/без Jev/);
  assert.match(ui.modeLabel("shared_jev"),/з Jev/);
  assert.match(ui.stageLabel("short_fill"),/фактичний/);
  assert.match(ui.stageLabel("owner_stop_close"),/Окремий тест/);
});
test("mechanics hint follows the chosen pinned recipe: side, post-only wait, partial exit, ending",()=>{
  const rules=name=>JSON.parse(fs.readFileSync(path.join(__dirname,"..","..","03_For_Client_Deliverables",name),"utf8")).rules;
  assert.equal(ui.mechanicsHelp(rules("V8_DEMO_SHORT_PARTIAL_TAIL.json")),"Один short-вхід за правилами, 100 Demo USDT, вікно входу 5 хв. Перевіряємо SL, частковий вихід і завершення позиції після дедлайну. Без запитів або витрат Jev. Це перевірка виконання, не результат змагання стратегій.");
  const long=ui.mechanicsHelp(rules("R2_DEMO_ETH_POST_ONLY_LONG.json"));
  assert.equal(long,"Один long-вхід за правилами, 100 Demo USDT, вікно входу 5 хв. Вхід post-only: ордер чекає до 600 с, далі залишок скасовується. Перевіряємо SL і вихід за ціллю або через 300 с утримання. Без запитів або витрат Jev. Це перевірка виконання, не результат змагання стратегій.");
  assert.doesNotMatch(long,/short|частков/);
  assert.match(ui.mechanicsHelp(rules("R2_DEMO_ETH_POST_ONLY.json")),/^Один short-вхід .* чекає до 600 с.* Перевіряємо SL і вихід за ціллю або через 300 с утримання\./);
  assert.match(ui.mechanicsHelp(rules("R2_DEMO_ETH_POST_ONLY_REST.json")),/^Один short-вхід .* чекає до 360 с, далі залишок скасовується\./);
  for(const unknown of [undefined,null,{},{side:"both"}])assert.equal(ui.mechanicsHelp(unknown),"Один вхід за правилами, 100 Demo USDT, вікно входу 5 хв. Без запитів або витрат Jev. Це перевірка виконання, не результат змагання стратегій.");
});
test("ordinary rules requires explicit choice and new server capability, preserves probe limits",()=>{
  assert.equal(ui.settings(recipe,cap,"shared_jev",true,["rules_only"]).paidBlocked,true);
  assert.equal(ui.settings(recipe,cap,"rules_only",true).mode,"shared_jev");
  const chosen=ui.settings(recipe,cap,"rules_only",true,["shared_jev","rules_only"]);
  assert.equal(chosen.mode,"rules_only");assert.equal(chosen.locked,false);assert.equal(chosen.paidBlocked,false);
  assert.equal(ui.settings({...recipe,strategy:{rules:{test_plan:"demo_cycle_v1"}}},cap,"rules_only",false,["rules_only"]).mode,"shared_jev");
  assert.equal(ui.settings(null,cap,"rules_only",false,["rules_only"]).mode,"shared_jev");
  assert.match(ui.modeLabel("rules_only"),/SHADOW/);
});
