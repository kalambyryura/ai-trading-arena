"use strict";
/* League statistics for the public table: how far an author is from Cash across many battles, and how sure we can be.
   Pure functions, deterministic for a given seed (the number on the page must not flicker between reloads), no requests.
   The unit of evidence is the battle, and battles that start on the same calendar day are not independent (one market),
   so the bootstrap resamples whole days. No p-values: the interval and the plain counts are shown instead. */
((root, factory) => {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ArenaLeagueStats = api;
})(typeof window === "undefined" ? {} : window, () => {
  const DEFAULTS = Object.freeze({ resamples: 4000, level: 0.9, minBattles: 30 });

  const number = value => {
    if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  };
  // xmur3 string hash -> mulberry32 generator: small, fast, identical in every JS engine.
  function seeded(seed) {
    let h = 1779033703 ^ String(seed).length;
    for (let i = 0; i < String(seed).length; i += 1) {
      h = Math.imul(h ^ String(seed).charCodeAt(i), 3432918353);
      h = (h << 13) | (h >>> 19);
    }
    let state = (Math.imul(h ^ (h >>> 16), 2246822507) ^ Math.imul(h ^ (h >>> 13), 3266489909)) >>> 0;
    return () => {
      state = (state + 0x6D2B79F5) >>> 0;
      let t = state;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  const quantile = (sorted, q) => {
    const position = (sorted.length - 1) * q, low = Math.floor(position), high = Math.ceil(position);
    return sorted[low] + (sorted[high] - sorted[low]) * (position - low);
  };

  /* rows: [{ net_usdt | net, cash_net_usdt?, cluster }]  one row per battle for ONE author (cash defaults to 0)
     options: { seed, resamples, level, minBattles, clusterKey(row) -> string }
     -> { n, clusters, sum, mean, ci: [low, high] | null, level, wins, ties, losses, enough, label } */
  function pairedDiff(rows, options = {}) {
    const o = { ...DEFAULTS, ...options };
    const clusterKey = o.clusterKey || (row => row.cluster);
    const diffs = [];
    for (const row of Array.isArray(rows) ? rows : []) {
      const net = number(row && (row.net_usdt !== undefined ? row.net_usdt : row.net));
      const cash = number(row && row.cash_net_usdt) || 0;
      if (net === null) continue;                     // an unknown result is not a zero: it simply is not counted here
      diffs.push({ d: net - cash, tie: net - cash === 0, key: String(clusterKey(row)) });
    }
    diffs.sort((x, y) => x.d - y.d || (x.key < y.key ? -1 : x.key > y.key ? 1 : 0));   // a fixed summation order: the same battles always give the same bits
    const n = diffs.length;
    const result = { n, clusters: 0, sum: 0, mean: null, ci: null, level: o.level, wins: 0, ties: 0, losses: 0, enough: n >= o.minBattles, label: "" };
    if (!n) { result.label = "немає боїв"; return result; }
    for (const item of diffs) {
      result.sum += item.d;
      if (item.tie) result.ties += 1; else if (item.d > 0) result.wins += 1; else result.losses += 1;
    }
    result.mean = result.sum / n;
    const byCluster = new Map();
    for (const item of diffs) {
      const bucket = byCluster.get(item.key) || { sum: 0, count: 0 };
      bucket.sum += item.d; bucket.count += 1; byCluster.set(item.key, bucket);
    }
    const clusters = [...byCluster.keys()].sort().map(key => byCluster.get(key));   // sorted: the same data gives the same draw order
    result.clusters = clusters.length;
    const random = seeded(o.seed === undefined ? "" : o.seed), means = [];
    for (let r = 0; r < o.resamples; r += 1) {
      let sum = 0, count = 0;
      for (let k = 0; k < clusters.length; k += 1) {
        const pick = clusters[Math.floor(random() * clusters.length)];
        sum += pick.sum; count += pick.count;
      }
      means.push(sum / count);
    }
    means.sort((a, b) => a - b);
    const tail = (1 - o.level) / 2;
    result.ci = [quantile(means, tail), quantile(means, 1 - tail)];
    result.label = result.enough ? "" : `мало даних: ${n} з ${o.minBattles} боїв`;
    return result;
  }

  // The UTC calendar day of a timestamp: the cluster every viewer computes identically; null when the time is unknown.
  const utcDay = value => {
    if (value === null || value === undefined || value === "") return null;
    const when = new Date(value);
    return Number.isFinite(when.getTime()) ? when.toISOString().slice(0, 10) : null;
  };

  /* battles: the rows served by the league endpoint: { cohort, created_at, competitive_validity, score: { known_results: [{ id, net_usdt }] } }
     Only ELIGIBLE battles count. Cash is 0 in every battle, so "author - Cash" is the author's net.
     -> { "<cohort>:<id>": { n, days, mean, low, high, status: "OK" | "FEW_DATA" } }, numbers in USDT per battle, keys sorted.
     One seed for all rows (options.seed): identical results always give identical intervals, and a row never depends on the others. */
  function intervals(battles, options = {}) {
    const groups = new Map();
    for (const battle of Array.isArray(battles) ? battles : []) {
      if (!battle || battle.competitive_validity !== "ELIGIBLE") continue;
      const day = utcDay(battle.created_at);
      const results = battle.score && Array.isArray(battle.score.known_results) ? battle.score.known_results : [];
      if (day === null) continue;
      for (const result of results) {
        if (!result || result.id === null || result.id === undefined || result.id === "cash") continue;
        const key = `${battle.cohort}:${result.id}`;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push({ net_usdt: result.net_usdt, cluster: day });
      }
    }
    const out = {};
    for (const key of [...groups.keys()].sort()) {
      const r = pairedDiff(groups.get(key), options);
      out[key] = { n: r.n, days: r.clusters, mean: r.mean, low: r.ci ? r.ci[0] : null, high: r.ci ? r.ci[1] : null, status: r.enough ? "OK" : "FEW_DATA" };
    }
    return out;
  }

  // One plain verdict per interval, so every page words it the same way.
  function verdict(entry) {
    if (!entry || entry.low === null || entry.high === null) return "NO_DATA";
    if (entry.status !== "OK") return "FEW_DATA";
    return entry.low > 0 ? "ABOVE_CASH" : entry.high < 0 ? "BELOW_CASH" : "INDISTINGUISHABLE";
  }

  return { DEFAULTS, pairedDiff, intervals, verdict, utcDay, seeded };
});
