"use strict";
/* Battle is a read-only projection of visible equity frames. No trading actions here. */
((root, factory) => {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ArenaBattle = api;
})(typeof window === "undefined" ? {} : window, () => {
  const EPS = 1e-8;
  const clamp = (n, a, b) => Math.max(a, Math.min(b, n));
  const value = n => n === null || n === undefined || n === "" || !Number.isFinite(Number(n)) ? null : Number(n);
  const format = (n, places = 3) => n === null ? "—" : new Intl.NumberFormat("uk-UA", {maximumFractionDigits:places}).format(n);
  const signed = n => (n > 0 ? "+" : "") + format(n); /* position_btc holds coins of the season instrument (R2): ETH in an ETH season */
  const units = () => (typeof window !== "undefined" && window.ArenaReviewModel) || (typeof require === "function" ? require("./review-model.js") : null), coinOf = input => units()?.baseCurrency({bots:input.bots, instrument:input.instrument}) ?? "BTC", COLORS = ["#a84b39", "#486c82", "#688047", "#916196", "#bd8739", "#318a83", "#807362"];
  const CYCLES = {
    checking:[1,"Перевіряє умови","check"], deciding:[2,"Оцінює вхід","think"],
    waiting:[2,"Чекає сигналу","think"], entry_pending:[3,"Чекає виконання входу","ready"],
    reviewing:[2,"Jev перевіряє вхід","think"], reviewing_position:[5,"Jev перевіряє докупівлю","think"],
    entering:[3,"Вхід виконано","walk"], position_check:[4,"Перевіряє позицію","check"],
    holding:[5,"Утримує позицію","guard"], add_pending:[3,"Чекає докупівлі","ready"],
    adding:[3,"Докупівлю виконано","power"], closing:[6,"Закриває · ще в ринку","close"],
    exiting:[6,"Позицію закрито","walk"], retired:[6,"Цикл завершено","rest"],
    cash:[0,"Зберігає капітал","rest"], ko:[0,"Нокаут · капітал ≤ 0","rest"],
    unknown:[0,"Стан позиції невідомий","rest"], paused:[0,"Пауза · позиція в ринку","guard"]
  };
  // Cut off events inside the projection too: a replay must never borrow future intent.
  function visibleEvents(input) {
    const seq=value(input.eventSeq), at=Date.parse(input.at);
    if(input.mode==="replay"&&seq===null&&!Number.isFinite(at))return [];
    return (input.events||[]).filter(e=>(seq===null||(value(e.seq)!==null&&Number(e.seq)<=seq))&&
      (input.mode!=="replay"||!Number.isFinite(at)||!e.at||(Number.isFinite(Date.parse(e.at))&&Date.parse(e.at)<=at)));
  }
  function cycleOf(bot, previous, events, input) {
    const position=value(bot.position_btc), before=value(previous?.position_btc);
    const open=position!==null&&position!==0, wasOpen=before!==null&&before!==0;
    const history=events.filter(e=>e.bot_id===bot.id);
    const relevant=new Set(["entry_checks","position_checks","rules_decision","entry_intent","paper_entry_fill",
      "entry_cancelled","entry_no_fill","risk_veto","close_intent","close_no_fill","paper_close_fill","stop_requested","risk_limit","jev_pending","jev_gate",
      "exchange_order_intent","exchange_entry_fill","exchange_close_fill","exchange_order_read","jev_decision","rules_close"]);
    const last=history.filter(e=>relevant.has(e.code)).at(-1);
    const cash=bot.strategy_id==="cash"||bot.id==="cash"||bot.avatar_kind==="baseline";
    const ko=value(bot.equity_usdt)!==null&&Number(bot.equity_usdt)<=0;
    const transition=cash||ko||position===null||before===null?null:
      open&&!wasOpen?"enter":!open&&wasOpen?"exit":open&&wasOpen&&Math.sign(position)===Math.sign(before)&&Math.abs(position)>Math.abs(before)?"add":null;
    let cycle;
    if(cash)cycle="cash";
    else if(ko)cycle="ko";
    else if(position===null)cycle="unknown";
    else if(open) {
      if(bot.stop_requested||["CLOSING","STOPPED","DONE"].includes(bot.status)||["close_intent","close_no_fill","paper_close_fill","stop_requested","risk_limit","rules_close"].includes(last?.code)||last?.code==="exchange_order_intent"&&last.data?.request?.reduceOnly===true)cycle="closing";
      else if(input.paused)cycle="paused";
      else if(bot.status==="JEV_PENDING"||last?.code==="jev_pending"||last?.code==="jev_decision")cycle="reviewing_position";
      else if(bot.status==="ENTRY_PENDING"||last?.code==="entry_intent"||last?.code==="exchange_order_intent"&&last.data?.request?.reduceOnly!==true||(last?.code==="jev_gate"&&last.data?.allowed===true))cycle="add_pending";
      else if(transition==="enter")cycle="entering";
      else if(transition==="add")cycle="adding";
      else if(last?.code==="position_checks"||last?.code==="entry_checks")cycle="position_check";
      else cycle="holding";
    } else if(transition==="exit")cycle="exiting";
    else if(bot.stop_requested||["STOPPED","DONE"].includes(bot.status))cycle="retired";
    else if(bot.status==="JEV_PENDING"||last?.code==="jev_pending"||last?.code==="jev_decision")cycle="reviewing";
    else if(bot.status==="ENTRY_PENDING"||last?.code==="entry_intent"||last?.code==="exchange_order_intent"&&last.data?.request?.reduceOnly!==true||(last?.code==="jev_gate"&&last.data?.allowed===true))cycle="entry_pending";
    else if(last?.code==="entry_checks")cycle="checking";
    else if(["paper_close_fill","exchange_close_fill"].includes(last?.code))cycle="exiting";
    else cycle="waiting";
    const [step,label,pose]=CYCLES[cycle];
    const previousSeq=value(input.previousEventSeq);
    const steps=previousSeq===null?[]:history.filter(e=>Number(e.seq)>previousSeq&&Number(e.step)>0).map(e=>Number(e.step)).filter((s,i,all)=>i===0||s!==all[i-1]);
    return {position,previousPosition:before,inMarket:!cash&&open,zone:cash?"outside":position===null?"unknown":open?"ring":"outside",
      cycle,step,cycleLabel:(step?step+" · ":"")+label,pose,transition,steps};
  }
  function model(input) {
    const budget = value(input.budget);
    const previous = new Map((input.previousBots || []).map(b => [b.id, b]));
    const events=visibleEvents(input);
    const actors = (input.bots || []).map((bot, index) => {
      const equity = value(bot.equity_usdt), pnl = value(bot.pnl_usdt);
      const before = value(previous.get(bot.id)?.pnl_usdt);
      const cash = bot.strategy_id === "cash" || bot.id === "cash" || bot.avatar_kind === "baseline";
      const ko = equity !== null && equity <= 0;
      const cycle=cycleOf(bot,previous.get(bot.id),events,input);
      const retired = !ko && !cash && cycle.position===0 && (bot.stop_requested||["STOPPED", "DONE"].includes(bot.status));
      const known = equity !== null && pnl !== null && budget !== null && budget > 0;
      return {id:bot.id, name:String(bot.name || "Учасник"), index, cash, ko, retired, known,
        equity, pnl, health:known ? Math.max(0, equity / budget * 100) : null,
        delta:known && before !== null ? pnl - before : null,
        kind:bot.avatar_kind || bot.author?.kind || "model", color:COLORS[index % COLORS.length], coin:units()?.botCoin?.(bot,{rules:input.rules,bots:input.bots,instrument:input.instrument}) ?? null, /* R2.1.12 RC2 (X3-04): each bot's own */
        ...cycle,active:known && cycle.inMarket && !ko && !retired && !cash, status:bot.status};
    });
    const active = actors.filter(a => a.active);
    const top = actors.filter(a => a.known && !a.ko).reduce((n, a) => Math.max(n, a.pnl), -Infinity);
    for (const actor of actors) {
      actor.leader = actor.known && !actor.ko && Math.abs(actor.pnl - top) <= EPS;
      actor.state = !actor.known || actor.position===null ? "unknown" : actor.ko ? "ko" : actor.cash ? "cash" : actor.retired ? "retired" : !actor.inMarket ? "idle" : actor.cycle==="closing" ? "closing" :
        actor.delta !== null && actor.delta < -EPS ? "hit" :
        actors.some(other => other.active && other.pnl > actor.pnl + EPS) ? "defend" :
        actor.pnl > EPS ? "attack" : "guard";
    }
    return {id:input.id, key:String(input.key), base:coinOf(input), actors, budget, active:actors.some(a=>!a.ko&&!a.cash&&(!a.retired||a.transition)),
      fighters:active.length,
      finished:!!input.finished, incomplete:!!input.incomplete, at:input.at,
      mode:input.mode || "live", moving:!!input.moving};
  }
  function planBeat(scene, beat) {
    const active = scene.actors.filter(a => a.active);
    const attackers=active.filter(a=>a.cycle!=="closing"&&a.cycle!=="paused");
    // Each observed gain produces one visual wave. A beat cannot debit another bot.
    const gainers = attackers.filter(a => a.delta !== null && a.delta > EPS);
    if (beat < gainers.length) {
      const attacker = gainers[beat];
      const targets=active.filter(a=>a.id!==attacker.id).map(a=>a.id);
      return targets.length?{kind:"wave", attacker:attacker.id, targets, delta:attacker.delta}:null;
    }
    const pairs = attackers.flatMap(a => active.filter(b => b.id !== a.id && a.pnl > b.pnl + EPS).map(b => ({kind:"punch", attacker:a.id, targets:[b.id]})));
    return pairs.length ? pairs[(beat - gainers.length) % pairs.length] : null;
  }
  function demoFrames() {
    const names=["Людина А","Робот Б","Людина В","Робот Г","Робот Д","Людина Е","Скриня"];
    const results=[[0,0,0,0,0,0,0],[0,0,0,0,0,0,0],[1,-1,0,0,0,0,0],[3,-2,1,-4,2,0,0],
      [5,-4,3,-9,4,0,0],[7,-6,4,-18,6,0,0],[10,-9,6,-30,8,-1,0],[13,-11,8,-47,11,-4,0],
      [14,-14,10,-65,13,-6,0],[15,-18,12,-83,15,-9,0],[15,-21,14,-100,18,-12,0],
      [17,-24,17,-100,20,-15,0],[20,-27,21,-100,23,-16,0],[20,-27,21,-100,23,-16,0]];
    const events=[];let seq=0;
    return results.map((row,index)=>{
      const bots=row.map((pnl,i)=>{
        const start=i<2?2:i<5?3:6, cash=i===6, ko=i===3&&index>=10;
        let position=cash||index<start?0:(i===1||i===4?-.001:.001);
        if(i===0&&index>=6&&index<=7)position=.002;
        if(i===0&&(index===9||index===10)||i===5&&index>=12||index===13&&!ko)position=0;
        let code="rules_decision",step=position?5:2;
        if(index===0){code="entry_checks";step=1;}
        if(index===start-1||i===0&&(index===5||index===10)){code="entry_intent";step=2;}
        if(index===start||i===0&&(index===6||index===11)){code="paper_entry_fill";step=3;}
        if(index===4&&position){code="position_checks";step=4;}
        if(i===0&&index===7||i===5&&index===11){code="close_intent";step=6;}
        if(i===0&&(index===8||index===9)||i===5&&index===12||index===13&&!ko){code="paper_close_fill";step=6;}
        if(!cash)events.push({seq:++seq,bot_id:"demo-"+i,code,step});
        const stopped=i===5&&index>=12||index===13&&!ko;
        return {id:"demo-"+i,strategy_id:cash?"cash":"demo-"+i,name:names[i],
          avatar_kind:cash?"baseline":i===0||i===2||i===5?"human":"model",equity_usdt:100+pnl,pnl_usdt:pnl,
          position_btc:position,stop_requested:stopped||i===5&&index===11,
          status:cash?"CASH":stopped?"STOPPED":code==="close_intent"||i===0&&index===8?"CLOSING":position<0?"SHORT":position>0?"LONG":"WAITING"};
      });
      return {index,bots,events:events.map(e=>({...e})),eventSeq:seq};
    });
  }
  // Stable numbered places in both zones: neighbours never jump when someone enters.
  function ringLayout(actors,width) {
    const narrow=width<600, ringActors=actors.filter(a=>!a.cash), cols=width>=900?4:3;
    const ringRows=Math.max(1,Math.ceil(ringActors.length/cols));
    const outsideCols=width>=900?7:width>=600?4:3, outsideRows=Math.max(1,Math.ceil(actors.length/outsideCols));
    const ring={x:20,y:65,width:width-40,height:Math.max(215,ringRows*(narrow?145:155)+52)};
    const deckY=ring.y+ring.height+48, height=deckY+outsideRows*(narrow?132:146)+56;
    const slots=(list,columns,y,rowHeight,inRing)=>new Map(list.map((a,i)=>{
      const row=Math.floor(i/columns),count=Math.min(columns,list.length-row*columns),col=i%columns;
      const inset=inRing?35:12,cell=(width-inset*2)/count;
      return [a.id,{id:a.id,x:inset+cell*(col+.5),y:y+rowHeight*row,
        scale:narrow?(inRing?.68:.56):inRing?.88:.70,labelWidth:cell-12}];
    }));
    return {ring,deckY,height,gate:{x:width/2,y:ring.y+ring.height-9},
      inside:slots(ringActors,cols,ring.y+ring.height/ringRows*.70,narrow?145:155,true),
      outside:slots(actors,outsideCols,deckY+86,narrow?132:146,false)};
  }
  function create(host, onDetails, onReplay) {
    if (typeof window !== "undefined" && window.ArenaColiseum) {
      if (host.classList.contains("legacy-floor")) return {update(){},connection(){},destroy(){}};
      const view=window.ArenaColiseum.create(host,{onJournal:(_,id)=>id?onDetails(id):onReplay?.()});
      return {update(input){view.update({id:input.id,bots:input.bots,events:visibleEvents(input),budget_usdt:input.budget,instrument:input.instrument,rules:input.rules,
        status:input.finished?"DONE":input.paused?"PAUSED":"RUNNING",mode:input.mode==="demo"?"DEMO":"PAPER",
        incomplete:input.incomplete,fixture_only:input.mode==="demo"},
        {key:input.key,previous:{bots:input.previousBots||[]},still:input.moving===false,replay:input.mode==="replay",at:input.at,eventSeq:input.eventSeq});},
        connection:ok=>view.connection(ok),destroy:()=>view.destroy()};
    }
    const node = (tag, className, text) => {const n=document.createElement(tag);n.className=className;if(text !== undefined)n.textContent=text;return n;};
    host.classList.add("battle-floor");host.replaceChildren();
    const bar=node("div","battle-toolbar"), heading=node("b","","БІЙ СТРАТЕГІЙ"), mode=node("span","battle-mode"), pause=node("button","battle-pause","Ⅱ Пауза анімації");
    const watch=node("button","battle-pause battle-watch","↺ Дивитися бій");
    pause.type=watch.type="button";pause.setAttribute("aria-pressed","false");watch.addEventListener("click",onReplay);
    bar.append(heading,mode,watch,pause);
    const cycleKey=node("div","battle-cycle-key");
    for(const label of ["1–2 · Перевірки за рингом","3 · Виконаний вхід → ринг","4–5 · Утримання / докупівля","6 · Повне закриття → вихід"])cycleKey.append(node("span","",label));
    const stage=node("div","battle-stage"), canvas=node("canvas","battle-canvas");canvas.setAttribute("aria-hidden","true");stage.append(canvas);
    const commentary=node("p","battle-commentary","Очікуємо ринковий кадр.");commentary.setAttribute("role","status");
    const roster=node("div","battle-roster");roster.setAttribute("aria-label","Учасники бою та їхній капітал");
    const legend=node("p","battle-legend","Ринг = відкрита позиція. Намір входу ще не переносить бота на ринг; часткове закриття ще не виводить. Удари відображають PnL і не змінюють гроші. HP = капітал / старт; KO лише при капіталі ≤ 0. Скриня залишається зовні.");
    host.append(bar,cycleKey,stage,commentary,roster,legend);
    const ctx=canvas.getContext("2d"), reduced=window.matchMedia("(prefers-reduced-motion: reduce)");
    let scene=null, lastKey=null, raf=null, elapsed=0, lastNow=0, paused=false, connected=true, width=0, height=0, dpr=1, hitBoxes=[], cards=new Map(), shownBeat="", intersecting=true;
    let detailIds=[];
    const resize = () => {width=canvas.getBoundingClientRect().width;height=scene?ringLayout(scene.actors,width).height:600;canvas.style.height=height+"px";dpr=Math.min(window.devicePixelRatio || 1,2);canvas.width=Math.round(width*dpr);canvas.height=Math.round(height*dpr);draw();};
    const observer=new ResizeObserver(resize);observer.observe(canvas);
    const visibility=new IntersectionObserver(entries=>{intersecting=entries[0].isIntersecting;sync();});visibility.observe(stage);
    function moving() {return scene && scene.moving && scene.active && connected && !paused && !reduced.matches && !document.hidden && intersecting && (scene.mode!=="live" || Date.now()-new Date(scene.at).getTime()<30000);}
    function sync() {lastNow=0;if(moving()){if(raf===null)raf=requestAnimationFrame(animate);}else if(raf!==null){cancelAnimationFrame(raf);raf=null;}draw();}
    function animate(now) {raf=null;if(!moving()){draw();return;}if(lastNow)elapsed+=Math.min((now-lastNow)/1000,.08);lastNow=now;draw();raf=requestAnimationFrame(animate);}
    pause.addEventListener("click",()=>{paused=!paused;pause.textContent=paused?"▶ Рух персонажів":"Ⅱ Пауза анімації";pause.setAttribute("aria-pressed",String(paused));sync();});
    const changed=()=>sync();reduced.addEventListener("change",changed);document.addEventListener("visibilitychange",changed);
    canvas.addEventListener("click",event=>{const rect=canvas.getBoundingClientRect(),x=event.clientX-rect.left,y=event.clientY-rect.top;const hit=hitBoxes.find(b=>x>=b.x-45&&x<=b.x+45&&y>=b.y-95&&y<=b.y+52);if(hit)onDetails(hit.id);});
    function update(input) {
      scene=model(input);
      const key=scene.id+":"+scene.mode+":"+scene.key;
      if(key!==lastKey){elapsed=0;lastNow=0;shownBeat="";lastKey=key;}
      const ids=scene.actors.map(a=>a.id);
      if(ids.join("|")!==detailIds.join("|")) {
        roster.replaceChildren();cards=new Map();detailIds=ids;
        for(const actor of scene.actors){const card=node("button","battle-card");card.type="button";card.addEventListener("click",()=>onDetails(actor.id));
          const name=node("span","battle-card-name"),state=node("span","battle-card-state"),pnl=node("strong","battle-card-pnl"),hp=node("span","battle-hp"),fill=node("i",""),capital=node("small","battle-card-capital"),cycle=node("span","battle-card-cycle"),position=node("small","battle-card-position");
          hp.append(fill);card.append(name,state,pnl,cycle,position,hp,capital);roster.append(card);cards.set(actor.id,{card,name,state,pnl,fill,capital,cycle,position});}
      }
      const states={attack:"Атакує",defend:"Захищається",guard:"На рингу · напоготові",hit:"Втрачає капітал",idle:"Поза боєм",closing:"Чекає закриття",cash:"Без позицій",retired:"Вийшов із бою",ko:"Нокаут",unknown:"Даних немає"};
      for(const actor of scene.actors){const c=cards.get(actor.id);c.card.dataset.state=actor.state;c.card.dataset.zone=actor.zone;c.card.dataset.cycle=actor.cycle;c.card.style.setProperty("--battle-color",actor.color);c.name.textContent=actor.name;c.state.textContent=states[actor.state];c.pnl.textContent="PnL "+signed(actor.pnl)+" USDT";c.pnl.classList.toggle("negative",actor.pnl<0);c.fill.style.width=clamp(actor.health||0,0,100)+"%";c.capital.textContent=format(actor.equity)+" USDT · HP "+format(actor.health,2)+"%";
        c.cycle.textContent=actor.cycleLabel;c.cycle.title=actor.steps.length?"Записані кроки цього кадру: "+actor.steps.join(" → "):actor.cycleLabel;
        c.position.textContent=actor.inMarket?"На рингу · "+(actor.position>0?"Long":"Short")+" · "+format(Math.abs(actor.position),8)+" "+(actor.coin||scene.base):actor.zone==="unknown"?"Розміщення не підтверджено":"Поза ринком · позиція 0";
        c.card.setAttribute("aria-label",actor.name+". "+c.cycle.textContent+". "+c.position.textContent+". "+c.pnl.textContent+". "+c.capital.textContent+". Відкрити деталі.");}
      watch.hidden=scene.mode!=="live"||!scene.finished;pause.hidden=scene.finished&&scene.mode!=="replay";
      pause.disabled=!scene.moving;pause.textContent=!scene.moving?"Нерухомий кадр":paused?"▶ Рух персонажів":"Ⅱ Пауза анімації";
      resize();sync();
    }
    function stroke(points, color="#33352e", line=2.6) {ctx.strokeStyle=color;ctx.lineWidth=line;ctx.beginPath();points.forEach((p,i)=>i?ctx.lineTo(p[0],p[1]):ctx.moveTo(p[0],p[1]));ctx.stroke();}
    function text(content,x,y,size=12,color="#43473c",align="center",font="sans-serif") {ctx.fillStyle=color;ctx.font=(font==="serif"?"italic ":"")+size+"px "+font;ctx.textAlign=align;ctx.fillText(content,x,y);}
    function ellipse(x,y,rx,ry,color) {ctx.fillStyle=color;ctx.beginPath();ctx.ellipse(x,y,rx,ry,0,0,Math.PI*2);ctx.fill();}
    function roundedRect(x,y,w,h,r,fill,line="#33352e") {ctx.beginPath();ctx.roundRect(x,y,w,h,r);ctx.fillStyle=fill;ctx.fill();if(line){ctx.strokeStyle=line;ctx.lineWidth=2;ctx.stroke();}}
    function layout(animate) {
      const spaces=ringLayout(scene.actors,width),progress=animate?clamp(elapsed/1.5,0,1):1;
      return scene.actors.map(actor=>{
        const inside=spaces.inside.get(actor.id),outside=spaces.outside.get(actor.id);
        const target=actor.inMarket?inside:outside;
        const position={...target,travelling:false,facing:target.x>width/2?-1:1};
        if(progress<1&&["enter","exit"].includes(actor.transition)) {
          const from=actor.transition==="enter"?outside:inside,to=actor.transition==="enter"?inside:outside;
          const way=[from,{...spaces.gate,scale:from.scale},to],segment=progress<.5?0:1,t=(progress-segment*.5)*2;
          const eased=t*t*(3-2*t),a=way[segment],b=way[segment+1];
          position.x=a.x+(b.x-a.x)*eased;position.y=a.y+(b.y-a.y)*eased;
          position.scale=from.scale+(to.scale-from.scale)*progress;position.travelling=true;position.facing=b.x>=a.x?1:-1;
        }
        return position;
      });
    }
    function background() {
      ctx.fillStyle="#faf7ed";ctx.fillRect(0,0,width,height);ctx.lineCap="round";ctx.lineJoin="round";
      ctx.strokeStyle="#8f938216";ctx.lineWidth=1;
      for(let x=18;x<width;x+=32){ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x+3,height);ctx.stroke();}
      for(let y=14;y<height;y+=32){ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(width,y+1);ctx.stroke();}
      // One ring for positions and a separate waiting deck, with an open centre gate.
      const cx=width/2,spaces=ringLayout(scene?.actors||[],width),ring=spaces.ring,bottom=ring.y+ring.height;
      roundedRect(ring.x,ring.y,ring.width,ring.height,24,"#e7ebd64d","#b4b49a");
      ctx.setLineDash([5,7]);roundedRect(ring.x+8,ring.y+9,ring.width-16,ring.height-18,20,"#e7ebd61c","#cac7b1");ctx.setLineDash([]);
      for(let line=0;line<2;line++){
        const y=bottom+line*5;stroke([[ring.x+10,y],[cx-31,y]],"#918c70",1.8);stroke([[cx+31,y],[width-ring.x-10,y]],"#918c70",1.8);
      }
      for(const x of [cx-31,cx+31]){stroke([[x,bottom-12],[x,bottom+17]],"#6f775d",4);ellipse(x,bottom-14,4,4,"#9ba77f");}
      text("РИНОК · ВІДКРИТІ ПОЗИЦІЇ",cx,ring.y+23,width<600?10:12,"#657353");
      text("ПОЗА РИНКОМ · ОЧІКУВАННЯ",cx,spaces.deckY+1,width<600?10:12,"#8a785d");
      stroke([[20,spaces.deckY+13],[width-20,spaces.deckY+13]],"#c1b398",1);
      for(let i=0;i<Math.floor(width/32);i++){const x=18+i*32, y=27+Math.sin(i*1.8)*4;ellipse(x,y,2.4,2.6,"#b6b09c");stroke([[x,y+4],[x-2,y+11],[x+4,y+10]],"#b6b09c",1.3);}
      text("MARKET RING",cx,52,width<600?18:23,"#9e9b80","center","serif");
      text(scene?.mode==="demo"?"ДЕМО · ВИГАДАНІ ДАНІ · БЕЗ УГОД":"ВХІД ПІСЛЯ ВИКОНАННЯ · ВИХІД ПІСЛЯ ЗАКРИТТЯ",cx,height-12,width<600?8:10,"#9c9885");
    }
    function character(actor,position,pose,phase,facing=1) {
      let x=position.x,y=position.y,s=position.scale;
      const pulse=pose==="hit"?Math.sin(phase*Math.PI*6)*5:0;
      const bounce=pose==="walk"?Math.sin(phase*Math.PI*10)*3:pose==="guard"||pose==="ready"?Math.sin(elapsed*3)*1.3:0;
      ellipse(x,y+4,27*s,5,"#6263501c");
      ctx.save();ctx.translate(x+pulse,y+bounce);ctx.scale(s*facing,s);
      const faceText=(label,x,y,size)=>{ctx.save();ctx.translate(x,y);ctx.scale(facing,1);text(label,0,0,size,actor.color);ctx.restore();};
      if(actor.ko){ctx.translate(35,-6);ctx.rotate(-Math.PI/2);}
      if(actor.retired&&!position.travelling)ctx.globalAlpha=.60;
      if(actor.cash){roundedRect(-22,-36,44,32,4,"#ddbf69");stroke([[-22,-23],[22,-23]],"#665b36",2);roundedRect(-4,-26,8,12,2,"#faf0bd");}
      else {
        const walk=pose==="walk"?Math.sin(phase*Math.PI*10)*13:0;
        const lean=pose==="punch"?8:pose==="hit"?-6:0;
        stroke([[0,-39],[-10-walk,-20],[-16+walk,0]],"#33352e",3.2);
        stroke([[0,-39],[12+walk,-18],[20-walk,0]],"#33352e",3.2);
        stroke([[lean,-65],[0,-38]],actor.color,8);
        stroke([[lean,-65],[0,-38]],"#33352e",1.5);
        if(pose==="punch"){
          stroke([[lean,-61],[25,-66],[47,-65]],"#33352e",3.2);ellipse(49,-65,5,4,actor.color);
          stroke([[lean,-59],[-15,-44],[-5,-37]],"#33352e",3);
        } else if(pose==="guard"||pose==="hit"||pose==="ready"){
          stroke([[lean,-60],[18,-45],[22,-73]],"#33352e",3.2);stroke([[lean,-59],[-16,-51],[1,-77]],"#33352e",3.2);
          ellipse(23,-74,4,4,actor.color);ellipse(2,-78,4,4,actor.color);
        } else if(pose==="check") {
          stroke([[lean,-60],[-16,-49],[-6,-45]],"#33352e",3);stroke([[lean,-60],[15,-43],[29,-52]],"#33352e",3);
          roundedRect(16,-65,20,26,2,"#fff8df",actor.color);
          for(let i=0;i<3;i++)stroke([[20,-59+i*7],[31,-59+i*7]],"#a8aa91",1);
          stroke([[20,-57],[23,-53],[29,-61]],actor.color,2);
        } else if(pose==="think") {
          stroke([[lean,-60],[-16,-45],[-8,-38]],"#33352e",3);stroke([[lean,-60],[19,-54],[12,-78]],"#33352e",3);
        } else if(pose==="close") {
          stroke([[lean,-60],[-18,-48],[-8,-73]],"#33352e",3);stroke([[lean,-60],[17,-73],[25,-91]],actor.color,3);
          faceText("6 →",29,-101,11);
        } else if(pose==="power") {
          stroke([[lean,-60],[-20,-77],[-28,-91]],"#33352e",3);stroke([[lean,-60],[20,-77],[29,-91]],"#33352e",3);
          ellipse(-28,-91,4,4,actor.color);ellipse(29,-91,4,4,actor.color);
        } else {
          stroke([[lean,-60],[-17,-46-walk/3],[-23,-56]],"#33352e",3);stroke([[lean,-60],[17,-44+walk/3],[27,-55]],"#33352e",3);
        }
        if(actor.kind==="human"){
          ellipse(lean,-81,13,14,"#f0d3a2");ctx.strokeStyle="#33352e";ctx.lineWidth=2.3;ctx.beginPath();ctx.ellipse(lean,-81,13,14,0,0,Math.PI*2);ctx.stroke();
          stroke([[lean-13,-85],[lean-9,-97],[lean+10,-92]],actor.color,4);
        }else{roundedRect(lean-14,-95,28,25,5,"#dae1d9");stroke([[lean,-95],[lean+2,-104]],"#33352e",2);ellipse(lean+2,-106,2.8,2.8,actor.color);}
        if(actor.ko){stroke([[lean-7,-86],[lean-2,-79],[lean-7,-79],[lean-2,-86]],"#33352e",1.5);stroke([[lean+3,-86],[lean+8,-79],[lean+3,-79],[lean+8,-86]],"#33352e",1.5);}
        else {ellipse(lean-5,-83,1.8,2,"#33352e");ellipse(lean+6,-83,1.8,2,"#33352e");stroke([[lean-4,-76],[lean+5,-76]],"#33352e",1.4);}
      }
      if(pose==="guard"&&!actor.ko){ctx.strokeStyle=actor.color;ctx.lineWidth=2;ctx.beginPath();ctx.arc(21,-60,31,-1.25,1.25);ctx.stroke();}
      if((pose==="think"||pose==="ready")&&!actor.ko){
        roundedRect(15,-120,36,18,7,"#fffbf1","#b3b59f");
        if(pose==="ready")faceText(actor.inMarket?"+ ?":"3 ?",33,-107,11);
        else for(let i=0;i<3;i++)ellipse(24+i*9,-111,1.8,1.8,Math.floor(elapsed*2)%3===i?actor.color:"#c7c8b8");
      }
      ctx.restore();
      // Labels remain in screen coordinates, outside the moving body.
      const maxLetters=Math.max(7,Math.floor(position.labelWidth/7));
      const label=actor.name.length>maxLetters?actor.name.slice(0,maxLetters-1)+"…":actor.name;
      text(label,x,y+18,width<600?10:12,"#373b31");
      const stageText=actor.ko?"KO":actor.inMarket?(actor.position>0?"LONG":"SHORT")+" · "+(actor.step?actor.step+" крок":"пауза"):actor.zone==="unknown"?"? ПОЗИЦІЯ":actor.cash?"БЕЗ УГОД":actor.cycle==="entry_pending"?"3 · ЧЕКАЄ ВХОДУ":actor.retired?"ЗАВЕРШИВ":actor.cycle==="exiting"?"6 · ЗАКРИТО":"1 ↔ 2 · ОЧІКУЄ";
      text(stageText,x,y+32,width<600?8:9,actor.inMarket?"#547153":"#8d7455");
      const bw=Math.min(position.labelWidth-4,width<600?70:94);ctx.fillStyle="#d6d2c5";ctx.fillRect(x-bw/2,y+39,bw,3);
      ctx.fillStyle=actor.ko?"#b54e40":actor.health<90?"#b47742":actor.color;ctx.fillRect(x-bw/2,y+39,bw*clamp(actor.health||0,0,100)/100,3);
      text(signed(actor.pnl)+" USDT",x,y+55,width<600?9:11,actor.pnl<0?"#a64b38":"#5a7447");
      if(actor.leader&&!actor.ko&&scene.finished&&!scene.incomplete)text("♛",x,y-116*s,25,"#b78d31");
    }
    function burst(x,y,phase,label,color="#b7523b") {
      ctx.save();ctx.globalAlpha=clamp(1-phase*.55,0,1);for(let i=0;i<9;i++){const a=i*Math.PI*2/9;const inner=8+phase*10,outer=20+phase*27;stroke([[x+Math.cos(a)*inner,y+Math.sin(a)*inner],[x+Math.cos(a)*outer,y+Math.sin(a)*outer]],color,2);}
      text(label,x,y-27-phase*12,17,color,"center","serif");ctx.restore();
    }
    function draw() {
      if(!ctx||!width||!height)return;ctx.setTransform(dpr,0,0,dpr,0,0);background();if(!scene)return;
      const stamp=new Date(scene.at).getTime();
      const stale=scene.mode==="live"&&(!connected||!Number.isFinite(stamp)||Date.now()-stamp>=30000)&&!scene.finished;
      mode.textContent=scene.mode==="demo"?"ДЕМО · ВИГАДАНІ ДАНІ":stale?"ЧЕКАЄМО ДАНІ":reduced.matches?"СПОКІЙНИЙ РЕЖИМ":scene.finished&&scene.mode!=="replay"?"ФІНАЛ":scene.mode==="replay"?"ПОВТОР": "PNL → РУХ";
      const active=scene.moving&&!stale&&!paused&&!reduced.matches;
      // Finish the observed entrance/exit before playing cosmetic combat beats.
      const arriving=scene.actors.some(a=>a.transition),fightTime=Math.max(0,elapsed-(arriving?1.6:0));
      const gains=scene.actors.filter(a=>a.active&&a.cycle!=="closing"&&a.cycle!=="paused"&&a.delta!==null&&a.delta>EPS).length;
      const opening=gains>0&&fightTime<2.8, duration=opening?2.8/gains:2.8;
      const beat=opening?Math.floor(fightTime/duration):(gains+Math.floor((fightTime-(gains?2.8:0))/2.8));
      const phase=opening?(fightTime%duration)/duration:((fightTime-(gains?2.8:0))%2.8)/2.8;
      const checks=scene.actors.some(a=>a.steps.some(s=>[1,2,4,5].includes(s)));
      const action=active&&elapsed>=(arriving?1.6:checks?1.4:0)?planBeat(scene,beat):null;
      const positions=layout(active), byId=new Map(positions.map(p=>[p.id,p])), poses=new Map(), effects=[];
      if(action) {
        const attacker=scene.actors.find(a=>a.id===action.attacker), origin=byId.get(action.attacker);
        if(action.kind==="wave"){
          poses.set(attacker.id,{pose:"power",facing:1});
          if(phase>.2&&phase<.88){const travel=(phase-.2)/.68;for(const id of action.targets){const target=byId.get(id);const x=origin.x+(target.x-origin.x)*Math.min(1,travel*1.8),y=origin.y-55+(target.y-origin.y)*Math.min(1,travel*1.8);effects.push({x,y,phase:travel,label:travel>.6?"БАМ!":"",color:attacker.color});poses.set(id,{pose:travel>.56?"hit":"guard",facing:target.x>origin.x?-1:1});}}
        } else {
          const target=byId.get(action.targets[0]),direction=target.x>=origin.x?1:-1,amount=phase<.28?phase/.28:phase<.6?1:(1-phase)/.4;
          const targetX=target.x-direction*62*origin.scale, targetY=target.y;
          origin.x+=(targetX-origin.x)*clamp(amount,0,1);origin.y+=(targetY-origin.y)*clamp(amount,0,1);
          poses.set(attacker.id,{pose:phase>.27&&phase<.58?"punch":"walk",facing:direction});
          poses.set(action.targets[0],{pose:phase>.35&&phase<.60?"hit":"guard",facing:-direction});
          if(phase>.35&&phase<.62)effects.push({x:target.x-direction*17,y:target.y-66*target.scale,phase:(phase-.35)/.27,label:"БЛОК!",color:attacker.color});
        }
        const announcement=scene.key+":"+beat;
        if(shownBeat!==announcement){shownBeat=announcement;commentary.textContent=action.kind==="wave"?attacker.name+": PnL зріс на "+format(action.delta)+" USDT — ударна хвиля!":attacker.name+" тисне на "+scene.actors.find(a=>a.id===action.targets[0]).name+" — вищий PnL дає перевагу.";}
      } else if(shownBeat!=="rest:"+scene.key) {
        shownBeat="rest:"+scene.key;
        const changes=scene.actors.filter(a=>a.transition).map(a=>a.name+": "+(a.transition==="enter"?"вхід виконано → на ринг":a.transition==="exit"?"позиція закрита → за ринг":"докупівля виконана → лишається на рингу"));
        commentary.textContent=stale?"Рух призупинено до свіжого ринкового кадру. Розташування зберігає останній підтверджений стан.":scene.finished?(scene.mode==="demo"?"Демо завершено. Усі результати тут вигадані.":"Фінальний кадр. Відкрий повтор, щоб побачити весь цикл."):changes.length?changes.join(". ")+".":paused||!scene.moving||reduced.matches?"Кадр зафіксовано. Позиції, етапи й PnL — за записаними даними.":scene.fighters===0?"Ринг порожній. Учасники перевіряють умови зовні; сигнал ще не означає виконаний вхід.":scene.fighters===1?"Один учасник у ринку. Решта чекають виконаного входу за межами рингу.":"Учасники з відкритими позиціями на рингу; решта готуються зовні.";
      }
      const sorted=[...scene.actors].sort((a,b)=>byId.get(a.id).y-byId.get(b.id).y);hitBoxes=[];
      for(const actor of sorted){const position=byId.get(actor.id);
        const transitionPose=actor.transition==="add"&&active&&elapsed<1.6?"power":null;
        const settledPose=["walk","power"].includes(actor.pose)?(actor.inMarket?"guard":"rest"):actor.pose;
        const recordedCheck=active&&elapsed<1.4&&!actor.ko&&actor.steps.some(s=>[1,2,4,5].includes(s));
        const cue=recordedCheck?(elapsed<.7&&actor.steps.some(s=>s===1||s===4)?"check":"think"):null;
        const pose=position.travelling?{pose:"walk",facing:position.facing}:actor.cycle==="closing"?{pose:"close",facing:position.facing}:transitionPose?{pose:transitionPose,facing:1}:cue?{pose:cue,facing:position.facing}:poses.get(actor.id)||{pose:settledPose,facing:position.facing};
        character(actor,position,pose.pose,position.travelling?elapsed:phase,pose.facing);hitBoxes.unshift({...position});
        if(transitionPose)text("+ ОБСЯГ",position.x,position.y-121*position.scale,11,actor.color);
        if(active&&actor.inMarket&&!position.travelling&&!transitionPose&&phase<.62&&actor.delta!==null&&Math.abs(actor.delta)>EPS){text(signed(actor.delta),position.x,position.y-128*position.scale-phase*14,12,actor.delta<0?"#b14b3b":"#4c7845");}}
      for(const effect of effects)burst(effect.x,effect.y,effect.phase,effect.label,effect.color);
      if(scene.finished&&scene.mode!=="replay")text(scene.incomplete?"НЕПОВНИЙ ЗАПИС":"РАУНД ЗАВЕРШЕНО",width/2,height-32,14,"#7d7967","center","serif");
    }
    return {update, connection(ok){connected=ok;sync();}, destroy(){if(raf!==null)cancelAnimationFrame(raf);observer.disconnect();visibility.disconnect();reduced.removeEventListener("change",changed);document.removeEventListener("visibilitychange",changed);}};
  }
  return {model,planBeat,create,demoFrames,visibleEvents,ringLayout};
});
