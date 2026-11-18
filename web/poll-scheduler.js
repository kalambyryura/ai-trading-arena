"use strict";
/* Polling policy for the arena page: when to ask the local server for state, never what to do with it.
   Pure helpers plus a single-flight scheduler with an injected clock; no requests, no DOM, no actions. */
((root, factory) => {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ArenaPollScheduler = api;
})(typeof window === "undefined" ? {} : window, () => {
  const DEFAULTS = Object.freeze({
    activeMs: 3000,       // a round is moving: engines emit a frame about every 5 s, the stage reports silence after 15 s
    pausedMs: 10000,      // PAUSED: nothing advances until the owner resumes
    idleMs: 30000,        // nothing is running: a slow heartbeat that still notices a round started from another tab
    unusedMs: 120000,     // PAPER state that no visible panel shows (Live view, management dialog closed)
    backoffBaseMs: 2000,  // first retry after a failure; doubles up to backoffMaxMs
    backoffMaxMs: 30000,
    timeoutMs: 8000,      // the page aborts a polling GET that stays silent; the next tick waits for the previous one
    slackMs: 50           // tolerance for timers that fire marginally early
  });
  const PARTS = Object.freeze(["arena", "exchange"]);

  const statusOf = run => run && typeof run === "object" && typeof run.status === "string" ? run.status : "";
  // Anything but DONE/PAUSED counts as moving, so an unfamiliar status keeps polling fast instead of silently slowing down.
  const moving = status => status !== "" && status !== "DONE" && status !== "PAUSED";

  // `contexts` limits which exchange contexts count (the arena page shows only arena_live; a Demo run elsewhere is not its business).
  function activity(snapshot, exchange, contexts) {
    const paper = statusOf(snapshot && snapshot.active);
    const active = exchange && exchange.active && typeof exchange.active === "object" ? exchange.active : {};
    const runs = (Array.isArray(contexts) ? contexts.map(name => active[name]) : Object.values(active)).map(statusOf);
    return {
      paperLive: moving(paper), paperPaused: paper === "PAUSED",
      exchangeLive: runs.some(moving), exchangePaused: runs.includes("PAUSED")
    };
  }

  // `paperVisible` = some panel that renders PAPER state is on screen (paper watch mode or the management dialog).
  function intervals(context, options = DEFAULTS) {
    const c = context || {};
    const exchange = c.exchangeLive ? options.activeMs : c.exchangePaused ? options.pausedMs : options.idleMs;
    const arena = c.paperVisible
      ? (c.paperLive ? options.activeMs : c.paperPaused ? options.pausedMs : options.idleMs)
      : (c.paperLive || c.paperPaused ? options.idleMs : options.unusedMs);
    return { arena, exchange };
  }

  function backoffMs(failures, options = DEFAULTS) {
    if (!(failures > 0)) return 0;
    return Math.min(options.backoffMaxMs, options.backoffBaseMs * 2 ** (Math.min(failures, 30) - 1));
  }

  /* env.run({arena, exchange, scheduled}) -> Promise<{arena?: boolean, exchange?: boolean}>; `false` marks a failed part.
     env.context() -> flags from activity() plus paperVisible; env.hidden() -> boolean.
     One run at a time: requests made while a run is in flight are merged into a single follow-up run. */
  function create(env) {
    const options = { ...DEFAULTS, ...(env.options || {}) };
    const now = env.now || (() => (typeof performance !== "undefined" ? performance.now() : Date.now()));
    const setTimer = env.setTimer || ((fn, ms) => setTimeout(fn, ms));
    const clearTimer = env.clearTimer || (id => clearTimeout(id));
    const last = { arena: -Infinity, exchange: -Infinity };
    const failures = { arena: 0, exchange: 0 };
    let timer = null, running = false, started = false, hidden = false, pending = [];

    // `scheduled` marks runs started by the timer: the page may let the server answer those from a shared short-lived copy.
    // Anything a person asked for (a kick) stays unmarked and is read fresh.
    const wanted = parts => parts && (parts.arena || parts.exchange)
      ? { arena: !!parts.arena, exchange: !!parts.exchange, scheduled: !!parts.scheduled } : { arena: true, exchange: true, scheduled: false };

    function context() {
      try { return env.context ? env.context() : {}; } catch (error) { return {}; }  // a broken probe must not stop polling
    }
    function waits(at) {
      const base = intervals(context(), options);
      const result = {};
      for (const part of PARTS) result[part] = last[part] + (failures[part] ? backoffMs(failures[part], options) : base[part]) - at;
      return result;
    }
    function cancel() { if (timer !== null) { clearTimer(timer); timer = null; } }
    function schedule() {
      cancel();
      if (!started || hidden || running || pending.length) return;
      const wait = waits(now());
      timer = setTimer(onTimer, Math.max(0, Math.min(wait.arena, wait.exchange)));
    }
    function onTimer() {
      timer = null;
      if (!started || hidden) return;
      const wait = waits(now());
      const parts = { arena: wait.arena <= options.slackMs, exchange: wait.exchange <= options.slackMs };
      if (parts.arena || parts.exchange) kick({ ...parts, scheduled: true }); else schedule();
    }
    async function execute(parts) {
      let result;
      try { result = (await env.run({ ...parts })) || {}; }
      catch (error) { result = { arena: parts.arena ? false : undefined, exchange: parts.exchange ? false : undefined }; }
      const finished = now();
      for (const part of PARTS) {
        if (!parts[part]) continue;
        last[part] = finished;
        failures[part] = result[part] === false ? failures[part] + 1 : 0;
      }
      return result;
    }
    async function pump() {
      if (running) return;
      running = true; cancel();
      try {
        while (pending.length) {
          const batch = pending; pending = [];
          const result = await execute({ arena: batch.some(item => item.parts.arena), exchange: batch.some(item => item.parts.exchange),
            scheduled: batch.every(item => item.parts.scheduled) });
          for (const item of batch) item.resolve(result);
        }
      } finally { running = false; }
      schedule();
    }
    // Immediate refresh (owner action, tab became visible). Resolves after a run that started after this call.
    function kick(parts) {
      return new Promise(resolve => { pending.push({ parts: wanted(parts), resolve }); pump(); });
    }
    function setHidden(value) {
      const was = hidden;
      hidden = !!value;
      if (hidden) cancel();
      else if (was && started) kick();
    }
    // The first read happens even in a hidden tab, so a page opened in the background is not blank when it is shown;
    // only the recurring polling waits for the tab to become visible.
    function start() {
      if (started) return;
      started = true; hidden = !!(env.hidden && env.hidden());
      kick();
    }
    function stop() { started = false; cancel(); }
    const state = () => ({ started, hidden, running, queued: pending.length, timerActive: timer !== null, failures: { ...failures } });
    return { start, stop, kick, setHidden, state };
  }

  return { DEFAULTS, PARTS, activity, intervals, backoffMs, create };
});
