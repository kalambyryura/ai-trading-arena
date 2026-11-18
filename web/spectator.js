"use strict";
/* Standalone, approved packet only. No fetch, token, author bundle or executor. */
(async () => {
  const status=document.getElementById("spectator-status"),R=window.ArenaEventReplay;
  try {
    const {packet,receipt}=JSON.parse(document.getElementById("spectator-data").textContent);
    const gate=await R.openSpectator(packet,receipt,Date.now());
    if(!gate.allowed)throw new Error(gate.reason);
    const replay=new R.Replay(packet),labels=window.ArenaReplayLabels.index(packet);
    let scene;
    window.ArenaResearchPanels.mountReplayControls(document.getElementById("spectator-controls"),replay,{onFrame(frame,snapshot,operation){
      if(operation.reset||!scene){scene?.destroy();scene=window.ArenaColiseum.create(document.getElementById("spectator-coliseum"));}
      scene.update(frame.run,{...frame.extras,previous:frame.previous});
      const C=window.ArenaColiseum;
      status.textContent=(C.evidenceLabel?.(packet.evidence_kind)||packet.evidence_kind)+" · "+(C.phaseLabel?.(snapshot.state.phase)||snapshot.state.phase)+" · "+(C.clockLong?.(snapshot.at_ms)||new Date(snapshot.at_ms).toISOString())+" · подія "+snapshot.state.seq;
      const results=document.getElementById("spectator-results");results.replaceChildren();
      for(const bot of snapshot.state.bots){const row=document.createElement("p"),p=packet.manifest.participants.find(p=>p.id===bot.id);
        row.textContent=p.name+" · "+window.ArenaReplayLabels.side(bot.financial.position_btc)+" · PnL "+window.ArenaReviewModel.exact(bot.financial.pnl_usdt,"USDT",true)+" · Jev "+window.ArenaReviewModel.exact(bot.financial.known_jev_usd,"USD",false);results.append(row);}
      const host=document.getElementById("spectator-events");host.replaceChildren();
      for(const event of snapshot.events.slice(-12).reverse()){const li=document.createElement("li");li.textContent="#"+event.seq+" · "+window.ArenaReplayLabels.summary(labels.get(event.seq));host.append(li);}
    }});
  } catch(error){status.textContent="Перегляд заблоковано: "+error.message;}
})();
