/* Pure presentation policy; the server independently validates the pinned Demo scope. */
(function(root,factory){const api=factory();if(typeof module==="object"&&module.exports)module.exports=api;else root.DemoExecutionUI=api;})(typeof window!=="undefined"?window:globalThis,function(){
  "use strict";
  const MODE="demo_mechanics";
  function eligible(version,capability){return Boolean(version&&capability?.version===1&&capability.execution_mode===MODE&&capability.context==="workshop_demo"&&capability.mode==="OKX_DEMO"&&capability.uses_jev===false&&capability.budget_usdt===100&&capability.duration_seconds===300&&version.recipe_sha256===capability.recipe_sha256);}
  function settings(version,capability,requestedMode,unknownBilling,ordinaryModes=[]){const available=eligible(version,capability),mechanics=available&&requestedMode===MODE,rulesOnly=Boolean(version)&&requestedMode==="rules_only"&&ordinaryModes.includes("rules_only")&&version.strategy?.rules?.test_plan!=="demo_cycle_v1";return {available,mechanics,mode:mechanics?MODE:rulesOnly?"rules_only":"shared_jev",locked:mechanics,paidBlocked:Boolean(unknownBilling)&&!mechanics&&!rulesOnly};}
  const stages={short_fill:"Short: фактичний вхід",initial_hosted_sl:"SL: початковий обсяг",partial_fill_and_residual:"Частковий вихід і залишок",resized_hosted_sl:"SL: залишок після скорочення",open_after_admission:"Позиція після 5 хвилин",no_new_entries_after_cutoff:"Нові входи після дедлайну заборонені",authored_full_exit:"Повний вихід за правилами",flat_and_settled:"Нульова позиція та звірені розрахунки",owner_stop_close:"Окремий тест: Stop → закриття",recovery_close_only:"Окремий тест: відновлення → закриття"};
  function stageLabel(id){return stages[id]||String(id);}
  function modeLabel(mode){return mode===MODE?"Механіка OKX Demo · без Jev":mode==="rules_only"?"RULES_ONLY · SHADOW запис · без викликів Jev":"Стратегія · з Jev";}
  /* The mechanics hint reads the chosen pinned recipe: its side, a post-only wait, a partial exit and how it ends. */
  const listed=items=>items.length>1?`${items.slice(0,-1).join(", ")} і ${items[items.length-1]}`:items[0]||"";
  function mechanicsHelp(rules){
    const r=rules&&typeof rules==="object"?rules:{},side=r.side==="long"||r.side==="short"?`Один ${r.side}-вхід`:"Один вхід";
    const wait=r.order?.type==="post_only"&&Number.isFinite(r.order.timeout_seconds)?` Вхід post-only: ордер чекає до ${r.order.timeout_seconds} с, далі залишок скасовується.`:"";
    const checks=[...(r.stop_loss_bps?["SL"]:[]),...(r.reduce_when?["частковий вихід"]:[]),...(r.max_hold_seconds===null&&r.exit?["завершення позиції після дедлайну"]:Number.isFinite(r.max_hold_seconds)?[`вихід ${r.take_profit_bps?"за ціллю або ":""}через ${r.max_hold_seconds} с утримання`]:[])];
    return `${side} за правилами, 100 Demo USDT, вікно входу 5 хв.${wait}${checks.length?` Перевіряємо ${listed(checks)}.`:""} Без запитів або витрат Jev. Це перевірка виконання, не результат змагання стратегій.`;
  }
  return {MODE,eligible,settings,stageLabel,modeLabel,mechanicsHelp};
});
