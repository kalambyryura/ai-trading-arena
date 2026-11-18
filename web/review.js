"use strict";
/* This page reads only the review/replay DTOs. No owner bootstrap, token or financial action. */
((root, factory) => {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else {root.ArenaReviewPage = api; api.mount().catch(error => api.showFatal(error.message));}
})(typeof window === "undefined" ? {} : window, () => {
  // R2.1.11 (D141 p. 2) / R2.1.12 (MIX-9, X3-05): the page's identity line in either view, with the probe label from the review header
  function identityText(requested, match, exampleOnly, view = "review") {
    return requested.id + " · " + match?.mode + " · " + (view === "review" ? match?.status : "причинний перегляд") +
      (exampleOnly ? " · технічна проба, не рахується" : "");
  }
  // R2.1.11 (G2) / R2.1.12 (MIX-7): the coin source of the page: the last frame's bots' base_ccy first, then the match's
  // or the season header's instrument
  function coinSourceFor(original, seasonInstrument) {
    const M = typeof window !== "undefined" && window.ArenaReviewModel ? window.ArenaReviewModel : require("./review-model.js");
    const bots = (original?.frames || []).at(-1)?.bots, match = original?.match;
    const base = M.instrumentOf(match) || !seasonInstrument ? match : {...match, instrument: seasonInstrument};
    return Array.isArray(bots) ? {...base, bots} : base;
  }
  function route(search) {
    const query = new URLSearchParams(search), mode = query.get("mode"), id = query.get("id");
    if (!["exchange", "paper"].includes(mode) || !id || !/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$/.test(id)) {
      throw new Error("Цю сторінку відкривають з ліги або історії арени: оберіть бій і натисніть «Розбір» чи «Повтор». (Технічно: потрібні mode=exchange або paper та id журналу.)");
    }
    const seq = query.get("seq"), event = query.get("event");
    if (seq !== null && event !== null) throw new Error("Вкажіть packet seq або journal event, одне посилання.");
    for (const item of [seq,event]) if (item !== null && (!/^\d+$/.test(item) || !Number.isSafeInteger(Number(item)))) throw new Error("Некоректний номер події.");
    return {mode, id, view: query.get("view") === "replay" ? "replay" : "review", botId: query.get("bot") || null,
      eventSeq: seq === null ? null : Number(seq), sourceEventSeq: event === null ? null : Number(event)};
  }
  function packetSequenceForSource(packet, sourceSeq) {
    if (!Number.isSafeInteger(sourceSeq) || sourceSeq < 1) throw new Error("Некоректний номер доказу.");
    const event = packet.events.find(event => event.source_event_seq === sourceSeq);
    if (!event) throw new Error("Доказ є у Review, але causal packet не містить його source_event_seq.");
    return event.seq;
  }
  function allowedGET(path) {
    return /^\/api\/(?:review|replay)\/(?:exchange|paper)\/[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$/.test(path);
  }
  async function readGET(path) {
    if (!allowedGET(path)) throw new Error("Ця сторінка читає лише review/replay DTO.");
    const response = await fetch(path, {method:"GET", cache:"no-store", credentials:"same-origin", redirect:"error"});
    const raw = await response.text();
    if (raw.length > 24 * 1024 * 1024) throw new Error("Пакет перевищує межу сторінки; потрібен indexed replay endpoint.");
    let data; try {data = JSON.parse(raw);} catch {throw new Error("Сервер не повернув JSON журналу.");}
    if (!response.ok) throw new Error(data.error || "HTTP " + response.status);
    // R2.1.11 (D141 p. 2): beside the hashed DTO, like the instrument
    return {data, raw, instrument: response.headers.get("X-Season-Instrument"), exampleOnly: response.headers.get("X-Season-Example-Only") === "1"};
  }
  function showFatal(text) {
    const notice = document.getElementById("review-notice");
    if (notice) {
      notice.textContent = text; notice.dataset.kind = "error";
      const back = document.createElement("a"); back.href = "/league"; back.textContent = "До ліги →"; notice.append(" ", back);
    }
  }
  async function mount() {
    const requested = route(location.search), P = window.ArenaResearchPanels, R = window.ArenaEventReplay,
      V = window.ArenaReviewVerify, L = window.ArenaReplayLabels, M = window.ArenaReviewModel;
    const $ = id => document.getElementById(id), mk = (tag, cls, text) => {
      const node = document.createElement(tag); node.className = cls || ""; if (text !== undefined) node.textContent = text; return node;
    };
    let original = null, rawBundle = null, verified = null, review = null, replay = null, controls = null,
      exportURL = "/api/battle-export/" + requested.mode + "/" + encodeURIComponent(requested.id),
      coliseum = null, replayPromise = null, labels = null, renderedSeq = null, renderedPlaying = null, activeView = "review",
      seasonInstrument = null, exampleOnly = false;
    // The review match names a non-BTC coin itself; otherwise the season header of the response does.
    const coinSource = () => coinSourceFor(original, seasonInstrument);
    $("review-save-battle").href = exportURL;
    $("review-save-debrief").href = "/api/debrief/" + requested.mode + "/" + requested.id;
    const notice = (text, kind = "okay") => {$("review-notice").textContent = text; $("review-notice").dataset.kind = kind;};
    function fact(label, value) {$("review-integrity-facts").append(mk("dt", "", label), mk("dd", "", String(value ?? "Не записано")));}
    function provenance() {
      $("review-integrity-facts").replaceChildren();
      fact("BattleReview", verified?.verified ? "SHA-256 і докази перевірено" : verified?.reason || "Очікуємо");
      fact("Bundle SHA-256", original?.integrity?.bundle_sha256); fact("Manifest SHA-256", original?.match?.manifest_sha256);
      fact("Projection", original?.projection || original?.projection_version); fact("Режим", original?.match?.mode);
      fact("Повний журнал", original?.history_complete === false ? "Ні · неповний" : original?.history_complete === true ? "Так" : "Не записано");
      fact("Replay SHA-256", replay?.packet.package_hash); fact("Джерело повтору", replay?.packet.evidence_kind);
    }
    function download() {
      if (!verified?.verified || !rawBundle) return;
      const release = original.author_bundle;
      if (release && (release.state !== "RELEASED" || release.integrity !== "PASS")) return;
      const blob = new Blob([rawBundle], {type:"application/json;charset=utf-8"}), url = URL.createObjectURL(blob), link = mk("a");
      link.href = url; link.download = "battle-review-" + requested.mode + "-" + requested.id + ".json";
      document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      notice("Завантажено вихідний локальний пакет із тим самим hash для всіх авторів.");
    }
    function renderReview(botId = requested.botId) {
      if (!original) return;
      const release = original.author_bundle;
      const downloadable = verified?.verified && (!release || release.state === "RELEASED" && release.integrity === "PASS");
      review = P.mountReview($("review-host"), original, {botId, bundleVerified: downloadable,
        onBundle: downloadable ? download : undefined,
        onEvent(seq) {show("replay", null, seq).catch(error => notice(error.message, "error"));}});
    }
    const participantCoin = id => ((original?.participants || []).find(p => p.id === id)?.instrument || "").split("-")[0];
    function renderPrefix(snapshot) {
      const host = $("replay-prefix"); host.replaceChildren();
      const scroll = mk("div", "research-table-scroll"), table = mk("table", "research-table"), head = mk("thead"), titles = mk("tr"), body = mk("tbody");
      const mixedSeason = M.mixed(original?.match);  // R2.1.12 (MIX): each participant's own coin, no season coin
      ["Учасник", mixedSeason ? "Позиція, монета бота" : "Позиція, " + M.displayCoin(coinSource()), "PnL USDT", "Jev USD", "Settlement"].forEach(label => {const cell = mk("th", "", label); cell.scope = "col"; titles.append(cell);});
      head.append(titles);
      for (const participant of replay.packet.manifest.participants.slice().sort((a,b) => a.slot - b.slot)) {
        const state = snapshot.state.bots.find(bot => bot.id === participant.id), f = state.financial, row = mk("tr");
        const coin = mixedSeason ? " " + participantCoin(participant.id) : "";
        const position = f.position_btc === null ? "UNKNOWN" : L.side(f.position_btc) + " · " + M.exact(f.position_btc, "", false) + coin;
        [participant.name, position, M.exact(f.pnl_usdt, "", true), M.exact(f.known_jev_usd, "", false), state.settled ? "Звірено" : "Очікуємо"].forEach(value => row.append(mk("td", "", value)));
        body.append(row);
      }
      table.append(head, body); scroll.append(table); host.append(scroll);
    }
    function renderEvents(snapshot) {
      const host = $("replay-events"); host.replaceChildren();
      const events = replay.packet.events.slice(Math.max(0, snapshot.event_count - 24), snapshot.event_count);
      const mixedSeason = M.mixed(original?.match), seasonCoin = mixedSeason ? "" : M.displayCoin(coinSource());
      for (const event of events.reverse()) {
        const coin = mixedSeason ? participantCoin(event.bot_id) : seasonCoin;  // R2.1.12 RC2 (X3-04): a mixed season has no season coin
        const label = labels.get(event.seq), item = mk("li"), button = mk("button", "review-event-button"); button.type = "button";
        if (event.seq === snapshot.state.seq) button.setAttribute("aria-current", "step");
        const action = mk("span", "review-event-action", label.action);
        const participant = replay.packet.manifest.participants.find(p => p.id === event.bot_id);
        action.append(mk("span", "review-event-detail", [participant?.name, (label.detail && !event.message ? label.detail + " · " : "") + event.type + " · " + event.code, event.message].filter(Boolean).join(" · ")));
        action.append(mk("time", "review-event-at", new Date(event.available_at_ms).toISOString()));
        const position = mk("span", "review-event-position");
        // position_btc holds coins of the season instrument; the review DTO names it only for PAPER (BTC-only) so far.
        if (Object.hasOwn(event.financial || {}, "position_btc")) position.textContent =
          M.exact(label.before, coin, false) + " → " + M.exact(label.after, coin, false);
        button.append(mk("span", "review-event-seq", "#" + event.seq), action, position);
        button.addEventListener("click", () => controls.seekEvent(event.seq)); item.append(button); host.append(item);
      }
      if (!events.length) host.append(mk("li", "research-muted", "Ще немає доступних спостережень. UNKNOWN не означає нуль."));
    }
    function onFrame(frame, snapshot, operation) {
      const changed = operation.reset || renderedSeq !== snapshot.state.seq || renderedPlaying !== snapshot.playing;
      if (operation.reset || !coliseum) {
        coliseum?.destroy(); coliseum = window.ArenaColiseum.create($("replay-coliseum"), {
          onJournal(_run, botId) {show("review").then(() => review?.selectBot(botId)).catch(error => notice(error.message, "error"));}
        });
      }
      if (changed) {
        const instrument = M.instrumentOf(coinSource());   // the replay packet has no instrument; the review match or the season header names it
        coliseum.update(instrument ? {...frame.run, instrument} : frame.run, {...frame.extras, previous:frame.previous}); renderPrefix(snapshot); renderEvents(snapshot);
        renderedSeq = snapshot.state.seq; renderedPlaying = snapshot.playing;
      }
      const phases = {RUNNING:"Входи відкриті", DRAINING:"Входи / add завершено · чинні позиції тривають", CLOSING:"Закриття за записаною умовою", PAUSED:"Потрібне відновлення", DONE:"Flat і settlement спостережено"};
      $("replay-status").textContent = replay.packet.evidence_kind + " · " + (phases[snapshot.state.phase] || snapshot.state.phase) +
        " · дані " + snapshot.state.quality.coverage + " · лише prefix #" + snapshot.state.seq;
    }
    async function loadReplay() {
      if (replay) return;
      if (replayPromise) return replayPromise;
      replayPromise = (async () => {
        $("replay-status").textContent = "Читаємо й перевіряємо causal packet…";
        const response = await readGET("/api/replay/" + requested.mode + "/" + requested.id), packet = response.data;
        R.validate(packet);
        if (packet.manifest.id !== requested.id) throw new Error("Replay належить іншому журналу.");
        if (await R.packetDigest(packet) !== packet.package_hash) throw new Error("SHA-256 replay не збігається; колізей не відкрито.");
        replay = new R.Replay(packet); labels = L.index(packet);
        controls = P.mountReplayControls($("replay-controls"), replay, {onFrame});
        provenance();
      })();
      try {await replayPromise;} catch (error) {
        replayPromise = null;
        $("replay-status").textContent = /causal replay/i.test(error.message) ? "Для цього бою ще немає перевіреного повтору. Розбір доступний у вкладці «Розбір»." : error.message;
        throw error;
      }
    }
    async function show(view, seq = null, sourceSeq = null) {
      activeView = view;
      if (original) $("review-identity").textContent = identityText(requested, original.match, exampleOnly, view);
      $("review-view").hidden = view !== "review"; $("replay-view").hidden = view !== "replay";
      ["review", "replay"].forEach(name => {const button = $(name + "-view-button");
        if (name === view) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");});
      if (view === "replay") {
        await loadReplay();
        if (activeView !== "replay") {replay.pause(); return;}
        if (sourceSeq !== null) seq = packetSequenceForSource(replay.packet, sourceSeq);
        if (seq !== null) {
          if (seq < 0 || seq > replay.packet.events.length) throw new Error("Подія не входить до доступного causal packet.");
          controls.seekEvent(seq);
        }
        notice("Повтор перевіреного пакета · " + replay.packet.evidence_kind + ". Видно лише відомі на вибраний момент факти.");
      } else {
        if (replay && controls) controls.seekEvent(replay.snapshot().state.seq);
        if (original) notice(verified?.verified ? "Підсумковий розбір локального журналу. Hash і докази перевірено." : verified.reason, verified?.verified ? "okay" : "error");
      }
    }
    $("review-view-button").addEventListener("click", () => show("review").catch(error => notice(error.message, "error")));
    $("replay-view-button").addEventListener("click", () => show("replay").catch(error => notice(error.message, "error")));
    $("review-verify").addEventListener("click", async () => {
      $("review-verify").disabled = true; notice("Повторно перевіряємо вихідний пакет…", "pending");
      verified = await V.verifyReview(rawBundle); renderReview(); provenance(); $("review-verify").disabled = false;
      notice(verified.verified ? "SHA-256, послідовність, посилання й місток gross/fees/funding/net перевірено." : verified.reason,
        verified.verified ? "okay" : "error");
    });
    window.addEventListener("beforeunload", () => {controls?.destroy(); coliseum?.destroy();});
    const response = await readGET("/api/review/" + requested.mode + "/" + requested.id);
    original = response.data; rawBundle = response.raw; verified = await V.verifyReview(rawBundle);
    seasonInstrument = M.instrumentOf(response.instrument);  // outside the hashed DTO: names the coin of a BTC exchange season
    if (original.match?.id !== requested.id) throw new Error("BattleReview належить іншому журналу.");
    exampleOnly = response.exampleOnly;
    $("review-verify").disabled = false; renderReview(); provenance();
    await show(requested.view, requested.view === "replay" ? requested.eventSeq : null,
      requested.view === "replay" ? requested.sourceEventSeq : null);
    return {show, destroy() {controls?.destroy(); coliseum?.destroy();}};
  }
  return {route, allowedGET, packetSequenceForSource, readGET, mount, showFatal, identityText, coinSourceFor};
});
