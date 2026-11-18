"use strict";
/* Links and read-only workflow state. This module never calls an execution API. */
((root, factory) => {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.StrategyResearch = api;
})(typeof window === "undefined" ? {} : window, () => {
  const safeId = value => typeof value === "string" && /^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$/.test(value);
  function link(parent) {
    return safeId(parent) ? "/workshop?" + new URLSearchParams({parent, research:"1"}) : null;
  }
  function request(search) {
    const query = new URLSearchParams(search);
    return query.get("research") === "1" && safeId(query.get("parent")) ? query.get("parent") : null;
  }
  function status(exchange) {
    const live = exchange?.active?.arena_live, demo = exchange?.active?.workshop_demo;
    return {
      liveActive: Boolean(live && live.status !== "DONE"), demoBusy: Boolean(demo && demo.status !== "DONE"),
      text: (live && live.status !== "DONE" ? "Live-бій триває. " : "Дослідження наступного бою. ") +
        "Бойовий рецепт зафіксований; зміни зберігаються в окремій чернетці. " +
        (demo && demo.status !== "DONE" ? "Demo-рахунок зайнятий: наступний тест — після завершення й звірки поточного. " : "OKX Demo має окремий рахунок; біржовий тест запускає власник. ") +
        "Для нового бою потрібні спільні дані, cutoff і вікно ревізій."
    };
  }
  return {link, request, status};
});
