"use strict";
/* R2.1.15: the Live Start form's leverage per bot. The limit comes from the server's table (1x for a version it does not
   list), the choice from the form; neither ever goes above 10x. Display only: no request, no trading action here. */
((root, factory) => {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ArenaLiveLeverage = api;
})(typeof window === "undefined" ? {} : window, () => {
  function limit(limits, item) {
    const top = Number(limits?.[item?.recipe_sha256]);
    return Number.isInteger(top) && top >= 1 && top <= 10 ? top : 1;
  }
  function choice(chosen, limits, item) {
    const x = Number(chosen?.[item?.id] || 1);
    return Number.isInteger(x) && x >= 1 && x <= limit(limits, item) ? x : 1;
  }
  function summary(items, budget, chosen, limits) {
    if (!items?.length || !Number.isFinite(budget)) return "";
    const fmt = v => new Intl.NumberFormat("uk-UA", {minimumFractionDigits:0, maximumFractionDigits:3}).format(v);
    const label = v => String(v?.name || v?.id || "Стратегія");
    let notional = 0, lots = 0;
    const parts = [];
    for (const item of items) {
      const x = choice(chosen, limits, item);
      if (item.strategy_schema_version === 4) {lots += 1; parts.push(`${label(item)} ${x}x · 1 лот`); continue;}
      const size = Number(item.rules?.size_pct), one = (Number.isFinite(size) ? budget * size / 100 : budget * .3) * x;
      notional += one;
      parts.push(`${label(item)} ${x}x · вхід до ${fmt(one)} USDT`);
    }
    return `Плече: ${parts.join(" · ")}. Сукупний нотіонал перших входів: ${fmt(notional)} USDT`
      + `${lots ? ` + ${lots} бот(и) схеми 4 по 1 лоту (G12)` : ""}. Маржа — частка бюджету, нотіонал = маржа × плече;`
      + " на 3x результат і просадка повнорозмірних ботів утричі більші.";
  }
  return {limit, choice, summary};
});
