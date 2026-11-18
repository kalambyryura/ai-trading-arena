"use strict";
/* Market context of a battle ("weather"): what the price of the season instrument (BTC or ETH, R2) did while the participants played (B-22).
   It explains a result, it does not cause one: a battle without a trend is not the author's fault, a violent one is not their merit.
   Pure arithmetic over recorded frames; no requests, no hidden randomness. An unrecorded price is "unknown", never flat. */
((root, factory) => {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ArenaBattleContext = api;
})(typeof window === "undefined" ? {} : window, () => {
  const MAX_GAP_MIN = 10;      // a longer silence between two frames is a gap, not a return
  const DIRECTION_SHARE = 0.25; // the move counts as a direction when the net change is at least this share of the high-low range
  const num = value => value === null || value === undefined || value === "" || !Number.isFinite(Number(value)) ? null : Number(value);

  /* frames: run payload frames [{ at, market: { mid } }]; options.fundingRate: last known funding rate (a fraction), if any;
     options.coin: base currency of the season instrument ("ETH"), BTC when not given (journals before R2).
     -> { known, samples, ... } */
  function fromFrames(frames, options = {}) {
    // R2.1.12 (MIX): a mixed season's frame has markets per contract; the bot's own contract is options.instrument
    const midOf = frame => options.instrument && frame && frame.markets ? num((frame.markets[options.instrument] || {}).mid) : num(frame && frame.market && frame.market.mid);
    const raw = (Array.isArray(frames) ? frames : []).map(frame => ({ t: Date.parse(frame && frame.at), mid: midOf(frame) }))
      .filter(point => Number.isFinite(point.t) && point.mid !== null && point.mid > 0).sort((a, b) => a.t - b.t);
    const points = raw.filter((point, i) => i === raw.length - 1 || raw[i + 1].t !== point.t);   // one price per instant: the last one wins
    if (points.length < 3) return { known: false, samples: points.length, reason: "У пакеті мало кадрів із ціною." };
    let high = -Infinity, low = Infinity, peak = points[0].mid, drawdown = 0, runup = 0, trough = points[0].mid, squares = 0, minutes = 0, gaps = 0;
    points.forEach((point, i) => {
      high = Math.max(high, point.mid); low = Math.min(low, point.mid);
      peak = Math.max(peak, point.mid); drawdown = Math.max(drawdown, (peak - point.mid) / peak * 1e4);
      trough = Math.min(trough, point.mid); runup = Math.max(runup, (point.mid - trough) / trough * 1e4);
      if (i === 0) return;
      const step = (point.t - points[i - 1].t) / 60000;
      if (step > MAX_GAP_MIN) { gaps += 1; return; }
      const move = Math.log(point.mid / points[i - 1].mid);
      squares += move * move; minutes += step;
    });
    const first = points[0], last = points[points.length - 1];
    const change = (last.mid / first.mid - 1) * 1e4, range = (high / low - 1) * 1e4;
    const direction = range > 0 && Math.abs(change) >= DIRECTION_SHARE * range ? (change > 0 ? "up" : "down") : "sideways";
    const funding = num(options.fundingRate);
    return {
      known: true, samples: points.length, start_at: new Date(first.t).toISOString(), end_at: new Date(last.t).toISOString(),
      span_seconds: (last.t - first.t) / 1000, start_mid: first.mid, end_mid: last.mid, high, low,
      change_bps: change, range_bps: range, max_drawdown_bps: drawdown, max_runup_bps: runup,
      volatility_bps_per_sqrt_minute: minutes > 0 ? Math.sqrt(squares / minutes) * 1e4 : null, gaps, direction,
      funding_rate: funding, coin: typeof options.coin === "string" && /^[A-Z0-9]{2,15}$/.test(options.coin) ? options.coin : "BTC"
    };
  }

  const fmt = (value, digits) => new Intl.NumberFormat("uk-UA", { maximumFractionDigits: digits }).format(value);
  const pct = (bps, digits = 2) => `${bps > 0 ? "+" : bps < 0 ? "−" : ""}${fmt(Math.abs(bps) / 100, digits)} %`;
  const plural = (n, one, few, many) => {
    const m10 = n % 10, m100 = n % 100;
    return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20) ? few : many;
  };
  const DIRECTIONS = { up: "ціна здебільшого йшла вгору", down: "ціна здебільшого йшла вниз", sideways: "чіткого напрямку не було" };

  // One paragraph for pages and reports. It says "context, not cause" every time.
  function describe(context) {
    if (!context || !context.known) return `Рух ціни за бій не записано${context && context.reason ? `: ${context.reason.replace(/\.$/, "").toLowerCase()}` : ""}. Це не означає, що ринок стояв.`;
    const parts = [
      `${context.coin || "BTC"} за бій: ${fmt(context.start_mid, 1)} → ${fmt(context.end_mid, 1)} (${pct(context.change_bps)})`,
      `розмах від мінімуму до максимуму ${pct(context.range_bps).replace("+", "")}`,
      `найбільша просадка від піку ${pct(context.max_drawdown_bps).replace("+", "")}`
    ];
    if (context.volatility_bps_per_sqrt_minute !== null) parts.push(`мінливість ≈ ${fmt(context.volatility_bps_per_sqrt_minute, 1)} bps за √хв`);
    const funding = context.funding_rate === null || context.funding_rate === undefined ? "" : `; остання відома ставка funding ${fmt(context.funding_rate * 1e4, 2)} bps`;
    const gap = context.gaps ? `; у записі ${context.gaps} ${plural(context.gaps, "пропуск", "пропуски", "пропусків")} понад ${MAX_GAP_MIN} хв, на мінливість вони не впливають` : "";
    return `${parts.join("; ")}; ${DIRECTIONS[context.direction]}${funding}${gap}. Контекст, не причина результату.`;
  }

  return { MAX_GAP_MIN, DIRECTION_SHARE, fromFrames, describe };
});
