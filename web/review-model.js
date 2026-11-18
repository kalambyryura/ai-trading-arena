"use strict";
/* Display-only helpers. Financial strings stay exact; scoring is an explicit frozen policy. */
((root, factory) => {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ArenaReviewModel = api;
})(typeof window === "undefined" ? {} : window, () => {
  function parts(value) {
    if (typeof value !== "string" || !/^-?(0|[1-9]\d*)(\.\d+)?$/.test(value)) return null;
    const sign = value.startsWith("-") ? -1n : 1n, unsigned = value.replace(/^-/, "");
    const split = unsigned.split(".");
    return {integer: BigInt(split.join("")) * sign, scale: split[1]?.length || 0};
  }
  function decimalSum(values) {
    const parsed = values.map(parts); if (parsed.some(v => !v)) return null;
    const scale = Math.max(0, ...parsed.map(p => p.scale));
    const total = parsed.reduce((total, p) => total + p.integer * (10n ** BigInt(scale - p.scale)), 0n);
    const sign = total < 0n ? "-" : "", digits = (total < 0n ? -total : total).toString().padStart(scale + 1, "0");
    const rendered = scale ? digits.slice(0, -scale) + "." + digits.slice(-scale) : digits;
    return sign + rendered.replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
  }
  function decimalCompare(left, right) {
    const l = parts(left), r = parts(right); if (!l || !r) return null;
    const scale = Math.max(l.scale, r.scale), a = l.integer * (10n ** BigInt(scale - l.scale));
    const b = r.integer * (10n ** BigInt(scale - r.scale)); return a < b ? -1 : a > b ? 1 : 0;
  }
  const negative = value => !parts(value) ? null : value.startsWith("-") ? value.slice(1) : "-" + value;
  function within(left, right, tolerance) {
    const difference = decimalSum([left, negative(right)]);
    if (difference === null || decimalCompare(tolerance, "0") === null || decimalCompare(tolerance, "0") < 0) return false;
    return decimalCompare(difference.replace(/^-/, ""), tolerance) <= 0;
  }
  function exact(value, unit = "", signed = false) {
    if (!parts(value)) return "Невідомо";
    const clean = decimalSum([value]), prefix = signed && decimalCompare(clean, "0") > 0 ? "+" : "";
    return prefix + clean.replace("-", "−").replace(".", ",") + (unit ? " " + unit : "");
  }
  /* Season instrument (R2). Demo/Live/PAPER seasons carry rules.instrument; a journal from before R2 has none and traded
     BTC-USDT-SWAP. Bot fields named *_btc (position_btc, quantity_btc, remaining_btc, ...) hold base-coin amounts of that
     instrument (ETH in an ETH season) despite the historical name; OKX order sizes are contracts. Labels only: no field is
     renamed and nothing is converted. */
  const DEFAULT_INSTRUMENT = "BTC-USDT-SWAP", CONTRACTS_UNIT = "контр.", UNKNOWN_COIN = "монети інструмента";
  // OKX contract value (ctVal × ctMult) of the R2 instruments: a reconciliation hint beside contract counts, never used in money.
  const CONTRACT_VALUES = {"BTC-USDT-SWAP": "0.01", "ETH-USDT-SWAP": "0.1", "SOL-USDT-SWAP": "1"};
  const INSTRUMENT = /^[A-Z0-9]{2,15}-[A-Z0-9]{2,15}(?:-[A-Z0-9]{1,15})*$/;
  // source: an instrument id, or a season/run/match/recipe object (rules.instrument, then instrument, then a match manifest).
  function instrumentOf(source) {
    if (typeof source === "string") return INSTRUMENT.test(source) ? source : null;
    if (!source || typeof source !== "object") return null;
    for (const value of [source.rules?.instrument, source.instrument, source.match_manifest?.instrument, source.manifest?.instrument]) {
      if (typeof value === "string" && INSTRUMENT.test(value)) return value;
    }
    return null;
  }
  const instrument = (source, fallback = DEFAULT_INSTRUMENT) => instrumentOf(source) || fallback;
  // R2.1.11 (G2, D141 p. 3): the coin the exchange wrote for its bots first; journals before it: the instrument, as before
  function writtenCoin(source) {
    const written = source && Array.isArray(source.bots) ? source.bots.find(b => b && /^[A-Z0-9]{2,10}$/.test(String(b.base_ccy || ""))) : null;
    return written ? written.base_ccy : null;
  }
  // R2.1.12 (MIX): a mixed season has no season coin; each bot names its own (base_ccy) and each contract its market
  function mixed(source) {
    return Boolean(source && (source.instrument === "MIXED" || (source.rules && source.rules.instrument === "MIXED")
      || (source.match && source.match.instrument === "MIXED") || (Array.isArray(source.instruments) && source.instruments.length > 1)));
  }
  // The contract of one bot of a mixed season, from the rules under config_sha256; null in any other season.
  function botInstrument(bot, source) {
    return bot && source && source.rules && source.rules.instruments ? instrumentOf(source.rules.instruments[bot.id]) : null;
  }
  function botCoin(bot, source) {
    const coin = bot && /^[A-Z0-9]{2,10}$/.test(String(bot.base_ccy || "")) ? bot.base_ccy : null;
    if (coin) return coin;
    const own = botInstrument(bot, source);
    return own ? own.split("-")[0] : displayCoin(source);
  }
  // The contracts of a mixed season, sorted: from its rules, or the instruments list of a review match.
  function contractsOf(source) {
    const listed = source && source.rules && source.rules.instruments ? Object.values(source.rules.instruments)
      : source && Array.isArray(source.instruments) ? source.instruments : [];
    return [...new Set(listed.map(instrumentOf).filter(Boolean))].sort();
  }
  function marketsOf(source) {
    const markets = source && source.latest_markets;
    return markets && typeof markets === "object" && !Array.isArray(markets) ? markets : null;
  }
  // R2.1.12 RC2 (X3-03): a mixed season's markets on one line, each contract with its own price and its own time, so a
  // stale contract shows its old time instead of hiding behind a fresher one. null when the season is not mixed.
  function marketsLine(source, price, clock) {
    if (!mixed(source)) return null;
    const markets = marketsOf(source) || {};
    const ids = [...new Set([...contractsOf(source), ...Object.keys(markets)])].sort();
    return ids.map(id => {
      const market = markets[id], coin = id.split("-")[0];
      return market && market.mid ? coin + " " + price(market.mid) + " · " + clock(market.at) : coin + " —";
    }).join(" | ") || null;
  }
  function marketAt(source) {
    if (source && source.latest_market && source.latest_market.at) return source.latest_market.at;
    const markets = marketsOf(source);
    const times = markets ? Object.values(markets).map(m => m && m.at).filter(Boolean).sort() : [];
    return times.length ? times[times.length - 1] : null;
  }
  function baseCurrency(source, fallback = "BTC") {const coin = writtenCoin(source); if (coin) return coin; const id = instrumentOf(source); return id ? id.split("-")[0] : fallback;}
  function pairLabel(source) {
    const pair = id => {const [base, quote] = id.split("-"); return base + " / " + quote;};
    if (mixed(source)) return contractsOf(source).map(pair).join(" · ") || "кілька інструментів";  // R2.1.12 RC2 (X3-04)
    return pair(instrument(source));
  }
  /* Coin label of a season, run or review match. A season object always has rules (no instrument there = a BTC journal from
     before R2). Review/replay DTOs carry only the mode so far: PAPER is BTC-only by the server rule; an exchange one without an
     instrument cannot name its coin and says so instead of guessing. */
  function displayCoin(source) {
    const written = writtenCoin(source);  // R2.1.11 (G2)
    if (written) return written;
    const id = instrumentOf(source);
    if (id) return id.split("-")[0];
    return source && typeof source === "object" && !source.rules && /^OKX_/.test(String(source.mode)) ? UNKNOWN_COIN : "BTC";
  }
  // Exact for decimal strings, Intl for finite numbers; null when the value is missing. Never a bare number without its unit below.
  function amount(value, digits = 8) {
    if (typeof value === "string" && parts(value.trim())) return exact(value.trim());
    if (value === null || value === undefined || value === "" || typeof value === "boolean" || !Number.isFinite(Number(value))) return null;
    return new Intl.NumberFormat("uk-UA", {maximumFractionDigits: digits}).format(Number(value)).replace("-", "−");
  }
  function withUnit(value, unit, digits = 8) {const text = amount(value, digits); return text === null ? null : unit ? text + " " + unit : text;}
  // {coins, contracts} → "0,013 ETH · 0,13 контр."; only the recorded parts are shown, each with its unit.
  function quantityText(values = {}, coinUnit = "BTC") {
    const shown = [withUnit(values?.coins, coinUnit), withUnit(values?.contracts, CONTRACTS_UNIT)].filter(Boolean);
    return shown.length ? shown.join(" · ") : "—";
  }
  // bot (R2.1.12 RC2, X3-04): whose contract, in a mixed season; no note when that contract is not known.
  function contractNote(source, bot) {
    const id = mixed(source) ? botInstrument(bot, source) : instrument(source), value = id ? CONTRACT_VALUES[id] : null;
    return value ? "1 " + CONTRACTS_UNIT + " " + id + " = " + exact(value) + " " + id.split("-")[0] : null;
  }
  /* One fill event → its recorded amounts, kept apart by unit. Exchange fills: data.result.contracts are OKX contracts filled by
     the order so far, data.position_btc the coins after it. PAPER fills: data.quantity_btc in coins. */
  function fillAmounts(event) {
    const data = event?.data || {}, result = data.result && typeof data.result === "object" ? data.result : {};
    const raw = result.state && typeof result.state === "object" ? result.state : {};
    const pick = (...values) => values.find(v => v !== undefined && v !== null && v !== "") ?? null;
    return {price: pick(data.price_usdt, result.price_usdt, result.average_price, raw.avgPx, raw.fillPx),
      filled: {coins: pick(data.quantity_btc, result.quantity_btc), contracts: pick(data.contracts, result.contracts, raw.accFillSz, raw.contracts)},
      after: {coins: pick(data.remaining_btc, data.position_btc, result.remaining_btc, result.position_btc, raw.remaining_btc), contracts: pick(raw.pos)},
      fee: pick(data.fee_usdt, result.fee_usdt, result.signed_fee_usdt, raw.fee),
      gross: pick(data.gross_pnl_usdt, result.gross_pnl_usdt, raw.gross_pnl_usdt)};
  }
  // Cell texts of one fills-table row; money(value, digits) is the page's USDT formatter, quantities stay exact with their units.
  function fillCells(event, coinUnit = "BTC", money = (value, digits) => amount(value, digits)) {
    const f = fillAmounts(event), usdt = (value, digits) => value === null ? "—" : money(value, digits) ?? "—";
    return {price: usdt(f.price, 2), filled: quantityText(f.filled, coinUnit), fee: usdt(f.fee, 6), gross: usdt(f.gross, 6), after: quantityText(f.after, coinUnit)};
  }

  function bridge(ledger, tolerance = "0.0000000001") {
    const derived = decimalSum([ledger.gross_usdt, ledger.signed_fees_usdt, ledger.signed_funding_usdt]);
    if (derived === null || !parts(ledger.net_usdt) || !parts(ledger.cash_delta_usdt)) {
      return {status: "UNKNOWN", derived_net_usdt: derived, reason: "Бракує суми для грошової звірки"};
    }
    const pnlOk = within(derived, ledger.net_usdt, tolerance), cashOk = within(ledger.net_usdt, ledger.cash_delta_usdt, tolerance);
    const final = ledger.flat === true && ledger.settled === true && ledger.bills_complete === true;
    return {status: !pnlOk || !cashOk ? "MISMATCH" : final ? "PASS" : "PROVISIONAL",
      derived_net_usdt: derived, reason: !pnlOk || !cashOk ? "Суми не збігаються" : final ? "Flat, bills і cash звірено" : "Підсумкова звірка ще триває"};
  }

  function scoreBattle(battle, policy) {
    const known = (battle.outcomes || []).filter(o => parts(o.net_usdt));
    const base = {status: "PENDING", sole_winner: null, shared_top: [], ordered: [],
      economic_known_net_usdt: known.length ? decimalSum(known.map(o => o.net_usdt)) : null,
      economic_known_count: known.length, economic_complete: false};
    if (!policy || !parts(policy.allocation_usdt) || !parts(policy.tie_tolerance_usdt) ||
        decimalCompare(policy.tie_tolerance_usdt, "0") < 0 || policy.primary_score !== "RECONCILED_NET_USDT") {
      return {...base, status: "INELIGIBLE", reason: "Не зафіксований регламент score"};
    }
    const outcomes = battle.outcomes || [], ids = battle.expected_participant_ids || [];
    if (!ids.length || new Set(ids).size !== ids.length || outcomes.length !== ids.length ||
        new Set(outcomes.map(o => o.id)).size !== ids.length || ids.some(id => !outcomes.some(o => o.id === id))) {
      return {...base, reason: "Відсутній або повторений учасник; missing не дорівнює Cash"};
    }
    if (battle.status !== "DONE" || outcomes.some(o => o.flat !== true || o.settled !== true || !parts(o.net_usdt))) {
      return {...base, reason: "Позиції або підсумковий облік ще не завершено"};
    }
    if (battle.competitive_validity !== "ELIGIBLE" ||
        outcomes.some(o => o.coverage !== "COMPLETE" || o.competitive_validity !== "ELIGIBLE" ||
          decimalCompare(o.allocation_usdt, policy.allocation_usdt) !== 0)) {
      return {...base, status: "INELIGIBLE", reason: "Результат збережено; спільний score недоступний"};
    }
    const ordered = outcomes.slice().sort((a, b) => decimalCompare(b.net_usdt, a.net_usdt) || a.id.localeCompare(b.id));
    const shared = ordered.filter(o => within(o.net_usdt, ordered[0].net_usdt, policy.tie_tolerance_usdt)).map(o => o.id);
    return {...base, economic_complete: true, ordered: ordered.map(o => o.id), status: shared.length === 1 ? "SOLE_WIN" : "TIE_AT_TOP",
      sole_winner: shared.length === 1 ? shared[0] : null, shared_top: shared,
      reason: shared.length === 1 ? "Один переможець за зафіксованим score" : "Спільне перше місце; без вирішальної метрики кількості угод"};
  }

  function comparableTrials(left, right) {
    const fields = ["data_hash", "code_hash", "policy_hash", "execution_model_hash", "allocation_usdt", "evidence_kind"];
    const differences = fields.filter(field => left[field] !== right[field]);
    return {comparable: differences.length === 0, differences,
      reason: differences.length ? "Різні умови; показуємо відмінності без оголошення переможця" : "Однакові зафіксовані умови"};
  }
  function leagueCounters(battles, slots = []) {
    const unique = Array.from(new Map(battles.map(b => [b.id, b])).values());
    return {started: unique.length, settled: unique.filter(b => b.settlement === "SETTLED").length,
      comparable: unique.filter(b => b.status === "DONE" && b.competitive_validity === "ELIGIBLE").length,
      draining: unique.filter(b => b.status === "DRAINING").length,
      paused: unique.filter(b => b.status === "PAUSED").length,
      incomplete: unique.filter(b => b.competitive_validity === "INELIGIBLE").length,
      skipped: slots.filter(s => s.status === "SKIPPED").length};
  }
  function capitalAdmission(capital) {
    if (capital.unsettled_run_id) return {eligible:false,reason:"Попередній бій ще займає рахунок"};
    if (capital.funds_verified !== true || !parts(capital.free_usdt) || !parts(capital.allocation_usdt) ||
        !parts(capital.buffer_usdt)) return {eligible:false,reason:"Немає перевіреного capital preview"};
    if (decimalCompare(capital.allocation_usdt,"0") <= 0 || decimalCompare(capital.buffer_usdt,"0") < 0) {
      return {eligible:false,reason:"Некоректна зафіксована allocation або reserve policy"};
    }
    const remaining=decimalSum([capital.free_usdt,negative(capital.allocation_usdt),negative(capital.buffer_usdt)]);
    return {eligible:decimalCompare(remaining,"0") >= 0, remaining_free_usdt:remaining,
      reason:decimalCompare(remaining,"0") >= 0 ? "Allocation забезпечена; wallet не скидається" :
        "Недостатньо вільних коштів; UI не зменшує allocation та не виконує переказ"};
  }
  function upgradeEligibility(candidate, window, nowMs) {
    const requiredTimes=["opened_at_ms","closed_at_ms","feedback_released_at_ms","training_cutoff_ms","next_lock_at_ms"];
    if (!Number.isSafeInteger(nowMs) || requiredTimes.some(k=>!Number.isSafeInteger(window?.[k])) ||
        window.feedback_released_at_ms > window.opened_at_ms || window.closed_at_ms <= window.opened_at_ms ||
        window.next_lock_at_ms < window.closed_at_ms) return {eligible:false,reason:"Немає спільного зафіксованого upgrade-вікна"};
    if (nowMs < window.opened_at_ms || nowMs > window.closed_at_ms || candidate?.submitted_at_ms < window.opened_at_ms ||
        candidate?.submitted_at_ms > window.closed_at_ms || !Number.isSafeInteger(candidate?.submitted_at_ms)) {
      return {eligible:false,reason:"Версію не подано у спільне вікно"};
    }
    if (!candidate.parent_recipe_hash || !candidate.recipe_hash || candidate.recipe_hash===candidate.parent_recipe_hash ||
        candidate.qualified !== true || candidate.all_trials_logged !== true) {
      return {eligible:false,reason:"Потрібні нова immutable lineage, qualification і всі trials"};
    }
    for (const field of ["sdk_hash","feature_epoch_hash","execution_policy_hash","common_bundle_hash"]) {
      if (!window[field] || candidate[field] !== window[field]) return {eligible:false,reason:"Різний спільний доступ або epoch: " + field};
    }
    if (!Number.isSafeInteger(candidate.training_latest_at_ms) || candidate.training_latest_at_ms > window.training_cutoff_ms) {
      return {eligible:false,reason:"Дані після спільного training cutoff"};
    }
    return {eligible:true,effective_at_lock_ms:window.next_lock_at_ms,
      reason:"Кандидат готовий до наступного lock; поточний рецепт не змінюється"};
  }

  return {decimalSum, decimalCompare, exact, within, bridge, scoreBattle, comparableTrials, leagueCounters,
    capitalAdmission,upgradeEligibility,
    DEFAULT_INSTRUMENT, CONTRACTS_UNIT, UNKNOWN_COIN, instrumentOf, instrument, baseCurrency, pairLabel, displayCoin,
    mixed, botInstrument, botCoin, contractsOf, marketsOf, marketAt, marketsLine,
    amount, withUnit, quantityText, contractNote, fillAmounts, fillCells};
});
