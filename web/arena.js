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

(() => {
  if (typeof document === "undefined") return;
  const $ = id => document.getElementById(id);
  let battle = null, battleSeason = null, liveBattle=null, liveSeason=null, coliseum=null;
  const token = document.querySelector('meta[name="panel-token"]')?.content || "";
  const state = {snapshot:null, exchange:null, selected:new Set(), catalogueSig:"", busy:false, exchangeBusy:false, liveAssignments:{}, liveLeverage:{}, liveProfileSig:"", liveRowsSig:"", selectedLiveRun:null, selectedSeasonId:null, replay:false, replayData:null, replayIndex:0, replayTimer:null, replayPlaying:false, replaySpeed:1, timer:null, detailsBotId:null, notice:"", noticeError:false};
  const labels = {RUNNING:"Входи відкриті",DRAINING:"Завершує відкриті позиції",CLOSING:"Закриває позицію",PAUSED:"Пауза · відновлення закриє позиції",DONE:"Завершено",WAITING:"Чекає сигналу",LONG:"Long",SHORT:"Short",CLOSING_BOT:"Закривається",STOPPED:"Зупинено",CASH:"Скриня",ready:"Готова",draft:"Чернетка",human:"Людина",model:"ШІ",example:"Навчальний приклад",baseline:"Базова стратегія"};
  const eventText = {SEASON_STARTED:"Сезон почався на спільних умовах",MARKET_UPDATED:"Оновлено публічні дані ринку",CHECK:"Перевірка правила",SIGNAL:"Стратегія подала сигнал",NO_SIGNAL:"Умови входу не виконано",ENTRY:"Paper-вхід зафіксовано",EXIT:"Paper-позицію закрито",STOP_REQUESTED:"Запит на завершення прийнято",BOT_STOPPED:"Учасника зупинено",SEASON_PAUSED:"Сезон поставлено на паузу",SEASON_DONE:"Сезон завершено",RECOVERY:"Відновлення: лише закриття наявної paper-позиції",ERROR:"Помилка paper-сезону",FEE:"Враховано симульовану комісію",FUNDING:"Враховано симульований funding",paper_entry_fill:"Paper-вхід зафіксовано",paper_close_fill:"Paper-позицію закрито",entry_checks:"Перевірено умови входу",position_checks:"Перевірено відкриту позицію",rules_decision:"Рішення формалізованих правил",risk_veto:"Вхід зупинено ризик-перевіркою",no_signal:"Умови входу не виконано",season_started:"Сезон почався на спільних умовах",season_done:"Сезон завершено",stop_requested:"Запит на завершення прийнято",bot_stopped:"Учасника зупинено",model_funding:"Оцінено funding за останньою спостереженою ставкою"};
  const eventIcon = {ENTRY:"↗",EXIT:"↘",NO_SIGNAL:"…",CHECK:"✓",STOP_REQUESTED:"■",ERROR:"!",SEASON_DONE:"✦"};
  const numeric = v => v===null||v===undefined||v===""||!Number.isFinite(Number(v)) ? null : Number(v);
  const money = (v,d=3) => numeric(v)===null ? "—" : new Intl.NumberFormat("uk-UA",{minimumFractionDigits:0,maximumFractionDigits:d}).format(Number(v));
  const usdCost = (v,complete=true,d=6) => {const amount=numeric(v);if(!complete||amount===null)return amount===null?"ціна неповна · невідома":`ціна неповна · відомо $${money(amount,d)} USD`;return `$${money(amount,d)} USD`;};
  const pct = (v,d=2) => numeric(v)===null ? "—" : `${Number(v)>0?"+":""}${money(v,d)}%`;
  const time = v => {const d=new Date(v);return Number.isFinite(d.getTime())?new Intl.DateTimeFormat("uk-UA",{hour:"2-digit",minute:"2-digit",second:"2-digit"}).format(d):"—";};
  const dateTime = v => {const d=new Date(v);return Number.isFinite(d.getTime())?new Intl.DateTimeFormat("uk-UA",{day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit"}).format(d):"—";};
  const safe = (v, fallback="—") => v===null||v===undefined||v===""?fallback:String(v);
  // A season trades one instrument (rules.instrument, R2); a journal from before R2 has none and traded BTC. *_btc fields are its coins.
  const seasonCoin = season => window.ArenaReviewModel?.baseCurrency?.(season) ?? "BTC", seasonPair = season => window.ArenaReviewModel?.pairLabel?.(season) ?? "BTC / USDT";
  const mk = (tag, cls, text) => {const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=String(text);return n;};
  const button = (cls,label,fn,disabled=false) => {const n=mk("button",cls,label);n.type="button";n.disabled=disabled;n.addEventListener("click",fn);return n;};
  const motion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
  // `timeout` (ms) applies to polling GETs only: a silent server ends the tick with an error instead of stacking requests. Owner POSTs are never aborted.
  const POLL=Object.freeze({timeout:window.ArenaPollScheduler?.DEFAULTS.timeoutMs||8000});
  const POLL_RUN=Object.freeze({timeout:POLL.timeout*3});  // full run journals are large and wait behind the server lock; the small state answers keep the short limit
  async function request(path, body, {timeout=0,viewer=false,onHeaders=null}={}) {
    const opts={cache:"no-store",credentials:"same-origin"};
    if(body!==undefined)Object.assign(opts,{method:"POST",headers:{"Content-Type":"application/json","X-Panel-Token":token},body:JSON.stringify(body)});
    else if(viewer)opts.headers={"X-Viewer-Poll":"1"};  // scheduled polls may be answered from the server's shared short-lived copy
    let timer=null;const silent=()=>new Error("Сервер арени не відповів вчасно");
    if(timeout>0&&body===undefined&&typeof AbortController==="function"){const control=new AbortController();opts.signal=control.signal;timer=setTimeout(()=>control.abort(),timeout);}
    try{
      const res=await fetch(path,opts);onHeaders?.(res.headers);let data={};try{data=await res.json();}catch{if(opts.signal?.aborted)throw silent();}
      if(!res.ok)throw new Error(data.error||`HTTP ${res.status}`);return data;
    }catch(error){if(error?.name==="AbortError")throw silent();throw error;}
    finally{if(timer!==null)clearTimeout(timer);}
  }
  function notify(msg, error=false){state.notice=msg||"";state.noticeError=!!error;const box=$("season-message");box.textContent=state.notice;box.hidden=!state.notice;box.classList.toggle("error",state.noticeError);}
  function setConnected(ok,msg="Локальне підключення"){battle?.connection(ok);coliseum?.connection(ok);$("connection").classList.toggle("offline",!ok);$("connection").lastElementChild.textContent=msg;}
  function avatarKind(person){return person?.avatar_kind||person?.author?.kind||person?.kind||"model";}
  function authorName(person){if(person?.strategy_id==="cash"||person?.avatar_kind==="baseline")return "Базовий учасник";return person?.author?.name||person?.author_name||person?.name||"Невідомий автор";}
  function authorLine(strategy){if(strategy?.id==="cash"||strategy?.author?.kind==="baseline")return "Скриня · базовий учасник";const a=strategy?.author||{};const name=safe(a.name,"Автор не вказаний");const kind=labels[a.kind]||safe(a.kind,"Автор");return `${name} · ${kind}${a.model?` · ${a.model}`:""}`;}
  function sig(catalogue){return JSON.stringify((catalogue||[]).map(s=>[s.id,s.version,s.status,s.author?.name,s.description,s.recipe_sha256]));}
  function sortedBots(bots){return [...(bots||[])].sort((a,b)=>(numeric(b.equity_usdt)??-Infinity)-(numeric(a.equity_usdt)??-Infinity));}
  function cashBot(bots){return (bots||[]).find(b=>b.strategy_id==="cash"||b.avatar_kind==="baseline");}
  function liveProfiles(){return (state.exchange?.profiles||[]).filter(profile=>profile.environment==="OKX_LIVE");}
  function syncLiveControls(catalogue=state.snapshot?.catalogue||[]){
    const profiles=liveProfiles(),items=(catalogue||[]).filter(item=>item.status==="ready"&&state.selected.has(item.id));
    const modeSelector=$("live-execution-mode"),rulesReady=state.exchange?.ordinary_execution_modes?.includes("rules_only"),executionMode=modeSelector.value,modeReady=executionMode==="shared_jev"||(executionMode==="rules_only"&&rulesReady);
    modeSelector.querySelector('option[value="rules_only"]').disabled=!rulesReady;modeSelector.disabled=state.exchangeBusy;
    $("live-mode-help").textContent=executionMode==="rules_only"?"Вхід і докупівля за правилами, без вето Jev. Ризик-ліміти коду, SL і Stop діють. Jev не викликається під час бою; точний запит зберігається для офлайн SHADOW.":"Jev перевіряє вхід і докупівлю. Ризик та вихід контролює код.";
    const signature=JSON.stringify([items.map(item=>[item.id,item.name,item.version]),profiles.map(p=>[p.id,p.label,p.configured,p.ready,p.reason,p.last_checked_at]),state.liveAssignments,state.exchange?.leverage_limits||{}]);
    const rows=$("live-account-rows");if(signature!==state.liveRowsSig){rows.replaceChildren();if(!items.length)rows.append(mk("p","empty-data","Оберіть готові стратегії вище."));
    for(const item of items){const row=mk("div","live-account-row"),identity=mk("div",""),name=mk("strong","",safe(item.name,"Стратегія"));identity.append(name,mk("small","",`${authorLine(item)} · v${safe(item.version)} · ${item.strategy_schema_version===4&&item.instrument?String(item.instrument).split("-")[0]:"BTC"}`));const select=mk("select","");select.id=`live-account-${item.id}`;select.setAttribute("aria-label",`OKX Live subaccount для ${safe(item.name)}`);select.append(mk("option","","Оберіть окремий subaccount"));const used=items.filter(x=>x.id!==item.id).map(x=>state.liveAssignments[x.id]).filter(Boolean);for(const profile of profiles){const option=mk("option","",`${safe(profile.label,profile.id)} · ${profile.configured?(profile.ready?"готовий":"налаштований"):"не налаштований"}`);option.value=profile.id;option.disabled=used.includes(profile.id);select.append(option);}select.value=state.liveAssignments[item.id]||"";select.addEventListener("change",()=>{state.liveAssignments[item.id]=select.value;syncLiveControls(catalogue);});const top=leverageLimit(item),lever=mk("select","live-leverage");lever.id=`live-leverage-${item.id}`;lever.setAttribute("aria-label",`Плече для ${safe(item.name)}, до ${top}x`);for(let x=1;x<=top;x+=1){const option=mk("option","",`${x}x`);option.value=String(x);lever.append(option);}if(!(leverageOf(item)<=top))state.liveLeverage[item.id]=1;lever.value=String(leverageOf(item));lever.disabled=top===1;lever.addEventListener("change",()=>{state.liveLeverage[item.id]=Number(lever.value);syncLiveControls(catalogue);});row.append(identity,select,lever);rows.append(row);}state.liveRowsSig=signature;}
    $("live-leverage-summary").textContent=leverageSummary(items,Number($("live-budget").value));
    state.liveProfileSig=JSON.stringify(profiles.map(p=>[p.id,p.label,p.configured,p.ready,p.reason,p.last_checked_at]));
    const status=$("live-profile-status");if(!profiles.length)status.textContent="OKX Live-профілі не налаштовані. Додайте окремий subaccount на бота у конфігурації сервера.";else{const ready=profiles.filter(p=>p.configured&&p.ready).length,configured=profiles.filter(p=>p.configured).length,details=profiles.map(p=>`${safe(p.label,p.id)}: ${p.configured?(p.ready?"готовий":safe(p.reason,"потрібна перевірка")):safe(p.reason,"не налаштований")}`).join(" · ");status.textContent=`Live-профілі: ${configured} налаштовано, ${ready} пройшли недавню read-only перевірку. ${details}. Старт виконає свій GET preflight перед будь-якою заявкою.`;}
    const budget=Number($("live-budget").value),selected=items.length,ids=items.map(item=>state.liveAssignments[item.id]).filter(Boolean),unique=new Set(ids).size===ids.length,complete=ids.length===selected&&unique&&ids.every(id=>profiles.some(p=>p.id===id&&p.configured)),unknownBilling=Boolean(state.exchange?.budgets?.arena?.series_unknown_billing);const active=state.exchange?.active?.arena_live;$("live-start").disabled=state.exchangeBusy||!selected||!complete||!modeReady||(unknownBilling&&executionMode==="shared_jev")||!Number.isFinite(budget)||budget<25||budget>500||budget*selected>1000||Boolean(active&&active.status!=="DONE");$("live-check").disabled=state.exchangeBusy||!selected||!complete;
  }
  // R2.1.15: the pure helpers are web/live-leverage.js (ArenaLiveLeverage); without it every bot stays 1x and no summary
  function leverageLimit(item){return window.ArenaLiveLeverage?.limit(state.exchange?.leverage_limits,item)??1;}
  function leverageOf(item){return window.ArenaLiveLeverage?.choice(state.liveLeverage,state.exchange?.leverage_limits,item)??1;}
  function leverageSummary(items,budget){return window.ArenaLiveLeverage?.summary(items,budget,state.liveLeverage,state.exchange?.leverage_limits)??"";}
  function renderLiveBudget(exchange){const b=exchange?.budgets?.arena;if(!b){$("live-budget-status").textContent="Ліміти Jev недоступні.";return;}$("live-budget-status").textContent=`Jev у live-арені: ${safe(b.series_attempts,0)} запитів · $${money(b.series_reported_cost_usd,4)} звітної вартості / $1 сукупного порогу · ${b.series_unknown_billing?"рахунок невідомий — нові виклики заблоковано":"окремо від бюджету сезону"}. Один запит може перевищити поріг.`;}
  function renderLiveHistory(exchange){const host=$("live-season-history"),history=(exchange?.history||[]).filter(run=>run.context==="arena_live").slice(0,8),key=JSON.stringify(history);if(host.dataset.key===key)return;host.dataset.key=key;host.replaceChildren();if(!history.length){host.append(mk("p","empty-data","Історія live-сезонів з’явиться після першого запуску."));return;}host.append(mk("h3","","Історія OKX Live"));for(const run of history){const btn=button("live-history-button",`${dateTime(run.created_at)} · ${safe(run.status)} · ${safe(run.id)} · ${safe(run.participants)} ботів · ${money(run.budget_usdt)} USDT / бот`,()=>openLiveRun(run.id));host.append(btn);}}
  function exchangeEvents(run){const host=$("live-season-events");host.replaceChildren();const events=(run?.events||[]).slice(-14).reverse();if(!events.length){host.append(mk("div","event-empty","Очікуємо події OKX Live."));return;}for(const event of events){const row=mk("div","event-row");row.append(mk("span","event-time",time(event.at)),mk("span","event-step",eventIcon[event.code]||String(event.step||"·")),mk("span","event-msg",safe(event.message,eventText[event.code]||event.code||"Подія")),mk("span","event-bot",event.bot_id?safe((run.bots||[]).find(bot=>bot.id===event.bot_id)?.name,"Бот"):"Сезон"));host.append(row);}}
  function renderLiveRun(run){const panel=$("live-season-panel");panel.hidden=!run;if(!run)return;liveSeason=run;const frames=run.frames||[],frame=frames.at(-1),previous=frames.at(-2),bots=run.bots||frame?.bots||[],terminal=!['RUNNING','DRAINING','CLOSING','PAUSED'].includes(run.status);let warning=panel.querySelector(".live-run-warning");if(!warning){warning=mk("p","live-run-warning");warning.setAttribute("role","status");panel.prepend(warning);}const notes=[];if(run.error)notes.push(`Помилка сезону: ${safe(run.error)}`);if(terminal&&run.incomplete)notes.push("Неповний результат: фінальна звірка не підтверджена.");if(terminal&&(run.accounting_reconciled===false||run.funding_reconciled===false))notes.push("Звірка балансу або funding не підтверджена.");warning.textContent=notes.join(" ");warning.hidden=!notes.length;warning.classList.toggle("error",notes.length>0);let book=panel.querySelector(".live-run-book");if(!book){book=mk("p","live-run-book");book.setAttribute("role","status");warning.after(book);}const bookLine=run.status==="RUNNING"?window.ArenaLifecycleV6?.bookText?.(run.feature_book):null;book.textContent=bookLine||"";book.hidden=!bookLine;if(!liveBattle)liveBattle=window.ArenaBattle.create($("live-season-battle"),id=>{const bot=(liveSeason?.bots||[]).find(item=>item.id===id),strategy=(liveSeason?.strategies||[]).find(item=>item.id===bot?.strategy_id)||{};if(bot)openBotDetail({...liveSeason,_exchange:true},bot,strategy);},()=>{});liveBattle.update({id:run.id,key:String(run.id)+":"+String(run.events?.at(-1)?.seq??frame?.event_seq)+":"+run.status,instrument:run.rules?.instrument,rules:run.rules,bots,previousBots:previous?.bots,budget:run.budget_usdt,mode:"live",events:run.events,eventSeq:run.events?.at(-1)?.seq??frame?.event_seq,previousEventSeq:previous?.event_seq,at:run.latest_market?.at||run.events?.at(-1)?.at||frame?.at,moving:["RUNNING","DRAINING","CLOSING"].includes(run.status)&&!run.error,finished:run.status==="DONE",incomplete:run.incomplete,paused:run.status==="PAUSED"});exchangeEvents(run);if(window.ArenaLifecycleV6&&run.lifecycle)window.ArenaLifecycleV6.mount($("live-lifecycle"),run.lifecycle,window.ArenaLifecycleV6.optionsForRun(run));
    const actions=$("live-season-actions"),actionKey=JSON.stringify([run.id,run.status,state.exchangeBusy,bots.map(b=>[b.id,b.name,b.strategy_id,b.stop_requested,b.status])]);if(actions&&actions.dataset.key!==actionKey){actions.dataset.key=actionKey;actions.replaceChildren();if(["RUNNING","DRAINING","CLOSING"].includes(run.status)){actions.append(button("quiet-button","Зупинити live-сезон · закрити всі позиції",()=>liveMutation("/api/exchange/stop",{id:run.id,bot_id:null}),state.exchangeBusy));for(const bot of bots.filter(item=>item.strategy_id!=="cash"&&!item.stop_requested&&!['STOPPED','DONE'].includes(item.status)))actions.append(button("quiet-button",`Зупинити ${safe(bot.name,"бота")} · закрити позицію`,()=>liveMutation("/api/exchange/stop",{id:run.id,bot_id:bot.id}),state.exchangeBusy));}if(run.status==="PAUSED")actions.append(button("quiet-button","Відновити закриття live-позицій",()=>liveMutation("/api/exchange/resume",{id:run.id}),state.exchangeBusy));}}
  async function openLiveRun(id){
    state.watchMode="live";$("watch-mode").value="live";state.watchHistorySig="";
    await openWatchHistory(id);$("management-dialog").close();$("coliseum-watch").scrollIntoView({behavior:motion(),block:"start"});
  }
  async function refreshExchange(scheduled=false){
    try{
      const exchange=await request("/api/exchange/state",undefined,{...POLL,viewer:scheduled});state.exchange=exchange;
      renderLiveBudget(exchange);syncLiveControls(state.snapshot?.catalogue||[]);renderLiveHistory(exchange);
      const active=exchange.active?.arena_live,latest=(exchange.history||[]).find(item=>item.context==="arena_live");
      if(active){
        let run=active;if(!Array.isArray(run.events)||!Array.isArray(run.bots))run=await request(`/api/exchange/runs/${encodeURIComponent(active.id)}`,undefined,POLL_RUN);
        state.selectedLiveRun=run;
      }else if(latest&&(!state.selectedLiveRun||latest.id!==state.selectedLiveRun.id||latest.status!==state.selectedLiveRun.status)){
        state.selectedLiveRun=await request(`/api/exchange/runs/${encodeURIComponent(latest.id)}`,undefined,POLL_RUN);
      }
      if(state.watchHistoryRun?.id===state.selectedLiveRun?.id&&active)state.watchHistoryRun={...state.selectedLiveRun,_exchange:true};
      renderLiveRun(state.selectedLiveRun);
      if(state.watchMode!=="paper")setConnected(true,"Локальна панель · журнал OKX");
      renderWatch();
      state.exchangeOk=true;return true;
    }catch(error){
      $("live-profile-status").textContent=`Не вдалося прочитати OKX Live-профілі: ${error.message}`;$("live-start").disabled=true;
      if(state.watchMode!=="paper")setConnected(false,"Журнал OKX недоступний");
      state.exchangeOk=false;return false;
    }
  }
  async function liveMutation(path,payload){if(state.exchangeBusy)return;state.exchangeBusy=true;syncLiveControls();try{const fresh=await request("/api/exchange/state");state.exchange=fresh;const response=await request(path,{generation:fresh.generation,...payload});if(payload.context){state.selectedLiveRun=response;state.watchHistoryRun=null;state.watchMode="live";$("watch-mode").value="live";state.watchHistorySig="";$("management-dialog").close();}notify("Запит прийнято для OKX Live. Перевірте стан і журнал сезону.");await refresh();}catch(error){notify(error.message,true);refresh();}finally{state.exchangeBusy=false;syncLiveControls();}}
  async function checkLiveProfiles(){const catalogue=state.snapshot?.catalogue||[],items=catalogue.filter(item=>item.status==="ready"&&state.selected.has(item.id)),account_ids=items.map(item=>state.liveAssignments[item.id]);if(!items.length||account_ids.some(id=>!id))return;state.exchangeBusy=true;syncLiveControls();try{const fresh=await request("/api/exchange/state");state.exchange=fresh;await request("/api/exchange/check",{generation:fresh.generation,account_ids,strategy_ids:items.map(item=>item.id)});await refreshExchange();notify("Read-only перевірку вибраних OKX Live subaccount завершено.");}catch(error){notify(error.message,true);}finally{state.exchangeBusy=false;syncLiveControls();}}
  async function startLiveSeason(){const catalogue=state.snapshot?.catalogue||[],items=catalogue.filter(item=>item.status==="ready"&&state.selected.has(item.id)),strategy_ids=items.map(item=>item.id),account_ids=items.map(item=>state.liveAssignments[item.id]),budget_usdt=Number($("live-budget").value),duration_seconds=Number($("live-duration").value),executionMode=$("live-execution-mode").value;if(!["shared_jev","rules_only"].includes(executionMode)||(executionMode==="rules_only"&&!state.exchange?.ordinary_execution_modes?.includes("rules_only"))){notify("Вибраний режим недоступний у цій версії сервера.",true);return;}if(!items.length||account_ids.some(id=>!id)||new Set(account_ids).size!==account_ids.length){notify("Для кожної готової стратегії оберіть унікальний OKX Live subaccount.",true);return;}if(!Number.isFinite(budget_usdt)||budget_usdt<25||budget_usdt>500||budget_usdt*items.length>1000){notify("Live-бюджет: від 25 до 500 USDT на бота, не більше 1 000 USDT сукупно.",true);return;}await liveMutation("/api/exchange/start",{context:"arena_live",strategy_ids,account_ids,budget_usdt,duration_seconds,...(executionMode==="rules_only"?{execution_mode:executionMode}:{}),...(items.some(item=>leverageOf(item)>1)?{leverage:items.map(leverageOf)}:{})});}
  function preparedWatch(){
    const strategies=(state.snapshot?.catalogue||[]).filter(s=>s.status==="ready");
    // A live round trades the one instrument of its recipes; a mixed roster has no single coin yet (PAPER reads BTC by its mode).
    const instruments=[...new Set(strategies.map(s=>window.ArenaReviewModel?.instrumentOf?.(s)||"BTC-USDT-SWAP"))];
    return {id:"prepared-"+(state.watchMode||"live"),status:"READY",mode:state.watchMode==="paper"?"PAPER":"OKX_LIVE",strategies,
      ...(state.watchMode!=="paper"&&instruments.length===1?{rules:{instrument:instruments[0]}}:{}),
      bots:strategies.map(s=>({id:s.id,strategy_id:s.id,name:s.name,author:s.author,avatar_kind:s.author?.kind,prepared:true,
        position_btc:null,equity_usdt:null,pnl_usdt:null})),events:[]};
  }
  function watchRun(){
    if(state.watchHistoryRun)return state.watchHistoryRun;
    if(state.watchMode!=="paper")return state.selectedLiveRun||preparedWatch();
    if(state.replay&&state.replayData){
      const full=state.replayData,frame=full.frames?.[state.replayIndex];
      if(frame)return {...full,bots:frame.bots,lifecycle:null,events:window.ArenaBattle.visibleEvents({events:full.events,mode:"replay",eventSeq:frame.event_seq,at:frame.at}),
        status:state.replayIndex===full.frames.length-1?full.status:"RUNNING",incomplete:state.replayIndex===full.frames.length-1&&full.incomplete,latest_market:{...frame.market,at:frame.at},_watchReplay:true};
    }
    return state.snapshot?.active||state.latestPaperRun||preparedWatch();
  }
  // The page's own first render needs no server answer (bindColiseum); a render that throws says so in the subtitle
  // instead of leaving the static "reading the journal" text, and the delayed reload hint goes once a render succeeded.
  function renderWatch(){
    if(!coliseum)return;
    try{renderWatchView();}catch(error){$("watch-subtitle").textContent=`Не вдалося показати раунд: ${error?.message||error}. Перезавантаж вкладку; раунд і торгівлю це не зачіпає.`;console.error(error);return;}
    const hint=$("watch-load-hint");if(hint)hint.hidden=true;
  }
  function renderWatchView(){
    const run=watchRun(),exchange=state.watchMode!=="paper",label=run.fixture_only?"ДЕМОНСТРАЦІЯ · ВИГАДАНІ ДАНІ":exchange?"OKX LIVE · РЕАЛЬНІ КОШТИ":"PAPER · ВІРТУАЛЬНІ УГОДИ";
    $("watch-subtitle").textContent=`${label}${run.execution_mode==="rules_only"?" · За правилами · Jev не викликається":""} · ${(run.status==="READY"&&exchange?[...new Set((run.strategies||[]).map(seasonPair))].join(" · "):"")||seasonPair(run)}`;
    const roundState=$("watch-round-state"), recorded=run.status==="DONE"||run._watchReplay;
    if(roundState){roundState.textContent=run.status==="READY"?"Готові до нового раунду":recorded?(run._watchReplay?"Повтор":window.ArenaColiseum?.runLabel(run)||"Запис завершеного раунду")+" · "+dateTime(run.created_at||run.started_at):run.status==="PAUSED"?"Потрібна звірка · раунд призупинено":"Раунд триває · "+(window.ArenaColiseum?.phaseLabel(run.status)||labels[run.status]||run.status);roundState.dataset.state=recorded?"recorded":run.status==="READY"?"ready":run.status==="PAUSED"?"paused":"active";}
    // A running Live round whose schema-4 book is not ready says why no entry is checked (warm-up, hole, failure).
    const book=$("watch-book"),bookLine=exchange&&run.status==="RUNNING"&&run.feature_book&&run.feature_book.status!=="ready"?window.ArenaLifecycleV6?.bookText?.(run.feature_book):null;
    if(book){book.textContent=bookLine||"";book.hidden=!bookLine;}
    // R2.1.12 RC2 (X3-03): a mixed season has no season price; every contract's own price and time, Live only.
    const marketsNode=$("watch-markets"),marketsText=exchange?window.ArenaReviewModel?.marketsLine?.(run,v=>money(v,1),time):null;
    if(marketsNode){marketsNode.textContent=marketsText||"";marketsNode.hidden=!marketsText;}
    const history=exchange?(state.exchange?.history||[]).filter(r=>r.context==="arena_live"):state.snapshot?.seasons||[];
    const defaultRun=exchange?state.selectedLiveRun:state.snapshot?.active||state.latestPaperRun;
    const currentLabel=defaultRun?.status==="DONE"?"Останній завершений раунд":defaultRun?"Поточний раунд":"Готовий склад";
    const sig=currentLabel+JSON.stringify(history.map(r=>[r.id,r.status,r.incomplete]));
    if(sig!==state.watchHistorySig){state.watchHistorySig=sig;const select=$("watch-history"),prior=select.value;select.replaceChildren();const current=mk("option","",currentLabel);current.value="current";select.append(current);
      for(const r of history){const length=window.ArenaColiseum?.durationLabel?.(r.duration_seconds??r.admission_seconds)||"",members=numeric(r.participants)!==null?`${r.participants} учасн.`:"";
        const option=mk("option","",[dateTime(r.created_at),length,members,r.incomplete?"Неповний":labels[r.status]||r.status].filter(Boolean).join(" · "));option.value=r.id;select.append(option);}select.value=state.watchHistoryRun?.id||prior||"current";if(!select.value)select.value="current";}
    $("watch-events").disabled=run.status==="READY";
    const exportLink=$("watch-export"),exportable=/^[0-9a-f]{32}$/.test(String(run.id))&&run.status!=="READY";
    exportLink.hidden=!exportable;
    if(exportable){exportLink.href="/api/battle-export/"+(exchange?"exchange":"paper")+"/"+encodeURIComponent(run.id);
      exportLink.textContent=run.status==="DONE"?"Зберегти бій · ZIP":"Зберегти поточний запис · ZIP";}
    else exportLink.removeAttribute("href");
    const marketAt=window.ArenaReviewModel?.marketAt?.(run)??run.latest_market?.at;  // R2.1.12 (MIX): latest_markets too
    coliseum.update(run,{key:String(marketAt||run.frames?.at(-1)?.at||run.id)+":"+String(run.events?.at(-1)?.seq)+":"+run.status,
      still:run._watchReplay&&!state.replayPlaying,replay:!!run._watchReplay,at:marketAt,eventSeq:run.events?.at(-1)?.seq});
    renderBattleCard(run,exchange,history).catch(()=>{});
  }
  // Battle summary card (B-03): facts come from the shared model (battle-facts.js) over the
  // review DTO of a finished round; read-only GET, cached per round, hidden during live play and replay.
  async function renderBattleCard(run,exchange,history){
    if(!coliseum?.battle||!window.ArenaBattleFacts)return;
    const facts=window.ArenaBattleFacts;let target=null,title="Підсумок бою";
    if(run.status==="DONE"&&!run.prepared&&!run._watchReplay)target=run;
    else if(run.status==="READY"){target=history.find(r=>r.status==="DONE"&&!r.incomplete)||history.find(r=>r.status==="DONE")||null;title="Останній завершений бій";}
    if(!target){if(state.battleRendered){coliseum.battle(null);state.battleRendered=null;}return;}
    const mode=exchange?"exchange":"paper",id=String(target.id),key=mode+":"+id;
    if(state.battleFactsKey!==key){state.battleFactsKey=key;state.battleFacts=null;
      // R2.1.11 (D141 p. 2): the server says "examples only" beside the hashed bundle: the review header, the history row
      try{let exampleOnly=facts.exampleOnlyFrom(target,null);const bundle=await request(`/api/review/${mode}/${encodeURIComponent(id)}`,undefined,{onHeaders:h=>{exampleOnly=facts.exampleOnlyFrom(target,h);}});if(state.battleFactsKey!==key)return;const bf=facts.factsForBattle(bundle,{exampleOnly});bf.headline=facts.battleHeadline(bf);state.battleFacts=bf;}
      catch{state.battleFacts=null;if(state.battleRendered){coliseum.battle(null);state.battleRendered=null;}return;}}
    if(!state.battleFacts)return;
    const renderKey=key+":"+title+":"+String(run.id);if(state.battleRendered===renderKey)return;state.battleRendered=renderKey;
    const full=target===run?run:state.watchHistoryRun?.id===id?state.watchHistoryRun:null;
    coliseum.battle(state.battleFacts,{title,collapsed:true,strategies:full?.strategies||run.strategies||state.snapshot?.catalogue||[],bots:full?.bots||[],allocation:target.budget_usdt??run.budget_usdt,
      reviewHref:`/review?mode=${mode}&id=${encodeURIComponent(id)}`,replayHref:`/review?mode=${mode}&id=${encodeURIComponent(id)}&view=replay`});
  }
  async function openWatchHistory(id){
    state.watchHistoryRun=null;
    if(id!=="current"){try{const exchange=state.watchMode!=="paper";const run=await request(exchange?`/api/exchange/runs/${encodeURIComponent(id)}`:`/api/arena/seasons/${encodeURIComponent(id)}`);
      state.watchHistoryRun={...run,_exchange:exchange};}catch(error){notify(`Не вдалося прочитати раунд: ${error.message}`,true);}}
    renderWatch();
  }
  async function openWatchJournal(run,botId){
    const exchange=state.watchMode!=="paper",publicRun={...run,_exchange:exchange};
    if(botId){const bot=run.bots?.find(b=>b.id===botId),strategy=run.strategies?.find(s=>s.id===bot?.strategy_id)||{};if(bot)await openBotDetail(publicRun,bot,strategy);return;}
    try{const full=await detailSeason(publicRun),body=mk("div",""),summary=mk("p","",`${exchange?"OKX Live":"PAPER"} · ${labels[full.status]||full.status} · ${full.incomplete?"Неповний раунд · без рейтингу":"За записаними даними"}`);
      body.append(summary);if(full.lifecycle&&window.ArenaLifecycleV6)window.ArenaLifecycleV6.mount(body,full.lifecycle,window.ArenaLifecycleV6.optionsForRun(full));
      const timeline=mk("div","detail-timeline");for(const event of [...(full.events||[])].reverse()){const item=mk("details","timeline-item"),caption=mk("summary","timeline-row");caption.append(mk("span","timeline-time",time(event.at)),mk("span","",event.message||event.code));item.append(caption);if(event.data)item.append(mk("pre","timeline-data",JSON.stringify(event.data,null,2)));timeline.append(item);}
      body.append(section("Повний журнал раунду",timeline));showDialog("Журнал раунду","ПЕРЕГЛЯД · БЕЗ ВИКОНАННЯ",body);
    }catch(error){notify(`Не вдалося прочитати журнал: ${error.message}`,true);}
  }
  function bindColiseum(){
    // Owner controls must remain reachable if an older running server has not
    // loaded the new static routes yet. Missing presentation cannot hide Stop.
    const management=$("management-dialog");
    // PAPER state is read slowly while nothing shows it; opening the dialog refreshes it at once.
    $("open-management").addEventListener("click",()=>{management.showModal();refresh({arena:true});});
    $("open-strategies").addEventListener("click",()=>{management.showModal();refresh({arena:true});$("catalogue").scrollIntoView({block:"start"});});
    $("management-close").addEventListener("click",()=>management.close());
    management.addEventListener("click",e=>{if(e.target===management)management.close();});
    if(!window.ArenaColiseum){const hint=mk("p","col-update-required","Для нового колізею онови сервер через ярлик «Update Jev Trading Lab». Кнопка оновлення не запускає торгівлю.");$("coliseum-watch").append(hint);$("watch-events").disabled=true;$("watch-subtitle").textContent="Колізей не завантажився: перезавантаж вкладку; якщо не допоможе — онови сервер через «Update Jev Trading Lab».";const loadHint=$("watch-load-hint");if(loadHint)loadHint.hidden=true;return;}
    state.watchMode=new URLSearchParams(location.search).get("mode")==="paper"?"paper":"live";
    $("watch-mode").value=state.watchMode;state.watchHistoryRun=null;state.watchHistorySig="";
    coliseum=window.ArenaColiseum.create($("coliseum-watch"),{onJournal:openWatchJournal});
    $("watch-mode").addEventListener("change",()=>{state.watchMode=$("watch-mode").value;state.watchHistoryRun=null;state.watchHistorySig="";$("watch-history").value="current";renderWatch();if(state.watchMode==="paper")refresh({arena:true});});
    $("watch-history").addEventListener("change",()=>openWatchHistory($("watch-history").value));
    $("watch-events").addEventListener("click",()=>openWatchJournal(watchRun(),null));
    renderWatch();
  }
  function renderState(data){
    if(!data||data.api_version!==4||data.mode!=="PAPER")throw new Error("Сервер арени не відповідає протоколу PAPER v4.");
    state.snapshot=data;setConnected(true);
    const active=data.active||null;const season=state.replay&&state.replayData?state.replayData:active;
    $("season-status").textContent=active?({RUNNING:"Входи відкриті",DRAINING:"Час входів завершено · завершуємо позиції",CLOSING:"Закриває paper-позиції",PAUSED:"Сезон на паузі",DONE:"Сезон завершено"}[active.status]||safe(active.status)):"Очікує запуску";
    $("season-clock").classList.toggle("paused",active?.status==="PAUSED");
    $("market-mid").textContent=active?.latest_market?.mid?`${seasonCoin(active)} ${money(active.latest_market.mid,1)} · ${time(active.latest_market.at)}`:`${seasonCoin(active)} —`;
    $("empty-arena").hidden=!!season;$("arena-view").hidden=!season;
    if(season){renderSeason(season,!!state.replay);}
    renderCatalogue(data.catalogue||[]);
    renderHistory(data.seasons||[]);
    if(!data.catalogue?.length) $("catalogue").replaceChildren(mk("p","empty-data","Каталог поки порожній."));
    const ready=(data.catalogue||[]).filter(s=>s.status==="ready");
    for(const id of [...state.selected])if(!ready.some(s=>s.id===id))state.selected.delete(id);
    $("selected-count").textContent=String(state.selected.size);
    syncLiveControls(data.catalogue||[]);
    $("start-season").disabled=state.busy||!!(active&&active.status!=="DONE")||state.selected.size===0;
    $("start-season").textContent=active&&active.status==="PAUSED"?"▶ Новий paper-сезон":"▶ Запустити paper-сезон";
    const running=active&&["RUNNING","DRAINING","CLOSING","PAUSED"].includes(active.status);
    if(active&&active.status==="RUNNING")$("season-status").title=`Нові входи до ${dateTime(active.end_at)}; потім завершення відкритих позицій`;
    if(!active)$("season-actions").replaceChildren();
    else renderSeasonActions(active);
    const issue=state.watchMode==="paper"?(season?.error?`ПОПЕРЕДНІЙ СТАН · ${season.error}`:season?.incomplete?"ПОПЕРЕДНІЙ СТАН · запис сезону неповний; підсумок не підтверджено.":""):"";
    if(issue){const box=$("season-message");box.textContent=issue;box.hidden=false;box.classList.add("error");}else{const box=$("season-message");box.textContent=state.notice;box.hidden=!state.notice;box.classList.toggle("error",state.noticeError);}
    renderWatch();
  }
  function renderCatalogue(items){
    const next=sig(items);if(next===state.catalogueSig)return;state.catalogueSig=next;
    const host=$("catalogue");host.replaceChildren();
    for(const s of items){
      const ready=s.status==="ready",selected=state.selected.has(s.id),kind=s.author?.kind||"model";
      const label=mk("article",`strategy-card${selected?" selected":""}${!ready?" draft":""}`);
      const input=mk("input","strategy-checkbox");input.type="checkbox";input.checked=selected;input.disabled=!ready;input.setAttribute("aria-label",`${safe(s.name,"Стратегія")}: обрати для сезону`);
      input.addEventListener("change",()=>{if(input.checked){if(state.selected.size>=6){input.checked=false;notify("До одного сезону можна обрати не більше шести готових стратегій.",true);return;}state.selected.add(s.id);}else state.selected.delete(s.id);label.classList.toggle("selected",input.checked);$("selected-count").textContent=String(state.selected.size);$("start-season").disabled=state.busy||!!(state.snapshot?.active&&state.snapshot.active.status!=="DONE")||state.selected.size===0;const chosen=[...state.selected];$("edit-strategy").disabled=chosen.length!==1||items.find(x=>x.id===chosen[0])?.status!=="ready";syncLiveControls(items);});
      const av=avatar(s);av.classList.add("strategy-avatar");
      const info=mk("span","strategy-info");
      info.append(mk("span","strategy-title",safe(s.name,"Без назви")),mk("span","strategy-subtitle",`${authorLine(s)} · v${safe(s.version,"—")}`),mk("span","strategy-desc",safe(s.description,"Опис ще не подано.")));
      const meta=mk("span","strategy-meta");meta.append(mk("span",ready?"ready-tag":"draft-tag",labels[s.status]||s.status));
      if(s.author?.model)meta.append(mk("span","",s.author.model));if(ready&&s.author?.attempts!==undefined)meta.append(mk("span","",`${s.author.attempts} спроб`));
      info.append(meta);label.append(input,av,info);
      const actions=mk("div","strategy-card-actions");const passport=button("card-action","Паспорт",()=>openStrategy(s));passport.setAttribute("aria-label",`Паспорт: ${safe(s.name,"стратегія")}`);actions.append(passport);if(!ready)actions.append(button("card-action draft-action","Заповнити рецепт",()=>openDraft(s)));label.append(actions);
      if(ready)label.addEventListener("click",e=>{if(e.target===label||e.target===av||e.target===info||e.target.classList.contains("strategy-title")||e.target.classList.contains("strategy-desc")||e.target.classList.contains("strategy-subtitle")||e.target.classList.contains("strategy-avatar")){input.checked=!input.checked;input.dispatchEvent(new Event("change"));}});
      host.append(label);
    }
    const chosen=[...state.selected];$("edit-strategy").disabled=chosen.length!==1||items.find(s=>s.id===chosen[0])?.status!=="ready";
  }
  function avatar(person, large=false){
    const kind=avatarKind(person);
    const box=mk("span",`col-sprite ${kind==="human"?"human":kind==="baseline"?"cash":"robot"}${large?" large":""}`);
    box.dataset.kind=kind;box.setAttribute("aria-hidden","true");box.style.backgroundPosition=kind==="baseline"?"center":"0% 0%";return box;
  }
  function renderSeason(season,replay){
    if(!season)return;
    $("arena-round").textContent=`СЕЗОН · ${safe(season.id,"—")}`;$("arena-duration").textContent=`${money(Number(season.duration_seconds)/60,0)} ХВ · ${money(season.budget_usdt,0)} USDT / БОТ`;
    $("feed-mode").textContent=replay?"ПОВТОР · ЛИШЕ ЗАПИСАНІ КАДРИ":season.status==="PAUSED"?"ПАУЗА · CLOSE-ONLY RECOVERY":season.status==="DONE"?"ЗАВЕРШЕНИЙ PAPER-СЕЗОН":"ЖИВИЙ PAPER-СЕЗОН";
    $("arena-topline").classList.toggle("replay",replay);
    let frame=null;
    if(replay){const frames=season.frames||[];frame=frames[Math.min(state.replayIndex,Math.max(0,frames.length-1))]||null;$("replay-controls").hidden=false;$("replay-range").max=String(Math.max(0,frames.length-1));$("replay-range").value=String(state.replayIndex);$("replay-time").textContent=`${frames.length?state.replayIndex+1:0} / ${frames.length}`;$("replay-toggle").textContent=state.replayPlaying?"Ⅱ":"▶";}
    else $("replay-controls").hidden=true;
    const marketMid=replay?frame?.market?.mid:season.latest_market?.mid;$("market-mid").textContent=marketMid?`${seasonCoin(season)} ${money(marketMid,1)} · ${time(replay?frame?.at:season.latest_market?.at)}`:`${seasonCoin(season)} —`;
    const bots=frame?.bots||season.bots||[], ordered=sortedBots(bots);
    const jev=season.execution_mode==="shared_jev";
    const costIncomplete=bots.some(b=>b.ai_cost_complete===false||b.ai_cost_usd===null||b.ai_cost_usd===undefined),reportedCost=bots.reduce((n,b)=>n+(numeric(b.ai_cost_usd)||0),0);
    $("season-runtime").textContent=jev?`Ліга: правила + спільний Jev · ${costIncomplete?`ціна неповна · відомо $${money(reportedCost,6)} USD`:`AI-витрати $${money(reportedCost,6)} USD`}. AI-витрати показані окремо від PnL у USDT; розподіл вартості batch між ботами — порівну.`:"Ліга: формальні правила · без викликів Jev. Навчання і сезон з різними рушіями порівнюються окремо.";
    battleSeason=season;
    if(!battle)battle=window.ArenaBattle.create($("arena-floor"),id=>{
      const bot=(battleSeason.bots||[]).find(b=>b.id===id);
      const strategy=(battleSeason.strategies||[]).find(s=>s.id===bot?.strategy_id)||{};
      if(bot)openBotDetail(battleSeason,bot,strategy);
    },async()=>{await openReplay(battleSeason.id);if(state.replayData)toggleReplay();});
    const frameIndex=replay?state.replayIndex:Math.max(0,(season.frames||[]).length-1);
    const previous=(season.frames||[])[frameIndex-1];
    const selectedFrame=(season.frames||[])[frameIndex];
    battle.update({id:season.id,key:String(frameIndex)+":"+String(replay?selectedFrame?.event_seq:season.events?.at(-1)?.seq)+":"+season.status,
      bots,previousBots:previous?.bots,budget:season.budget_usdt,instrument:season.rules?.instrument,mode:replay?"replay":"live",
      events:season.events,eventSeq:replay?selectedFrame?.event_seq:season.events?.at(-1)?.seq,
      previousEventSeq:previous?.event_seq,paused:!replay&&season.status==="PAUSED",
      at:replay?frame?.at:season.latest_market?.at,
      moving:replay?(state.replayPlaying&&frameIndex<(season.frames||[]).length-1):(["RUNNING","DRAINING","CLOSING"].includes(season.status)&&!season.error),
      finished:season.status==="DONE"&&(!replay||frameIndex===(season.frames||[]).length-1),incomplete:season.incomplete});
    renderLeaderboard(ordered,season);
    const lifecycleHost=$("paper-lifecycle");lifecycleHost.hidden=replay;if(!replay&&window.ArenaLifecycleV6&&season.lifecycle)window.ArenaLifecycleV6.mount(lifecycleHost,season.lifecycle,window.ArenaLifecycleV6.optionsForRun(season));
    const evHost=$("arena-events");evHost.replaceChildren();const events=(season.events||[]).filter(e=>(!replay||frame?.event_seq===undefined||Number(e.seq)<=Number(frame.event_seq))&&(!state.detailsBotId||e.bot_id===state.detailsBotId||!e.bot_id)).slice(-12).reverse();
    if(!events.length)evHost.append(mk("div","event-empty",replay?"У цьому кадрі ще немає записаних подій.":"Події з’являться у міру перевірки стратегій."));
    for(const e of events){const row=mk("div","event-row");row.append(mk("span","event-time",time(e.at)),mk("span","event-step",eventIcon[e.code]||String(e.step||"·")),mk("span","event-msg",safe(e.message,eventText[e.code]||e.code||"Подія")),mk("span","event-bot",e.bot_id?safe((season.bots||[]).find(b=>b.id===e.bot_id)?.name,"Учасник"):"Сезон"));evHost.append(row);}
    if(state.watchMode==="paper")renderWatch();
  }
  function renderLeaderboard(bots,season){
    const host=$("leaderboard");host.replaceChildren();
    let rank=0,prior=null;for(const [i,b] of bots.entries()){const equity=numeric(b.equity_usdt);if(i===0||equity===null||prior===null||Math.abs(equity-prior)>=1e-8)rank=i+1;prior=equity;const strategy=(season.strategies||[]).find(s=>s.id===b.strategy_id)||b.strategy||{};const row=button("leader-row","",()=>openBotDetail(season,b,strategy));row.append(mk("span","leader-rank",String(rank).padStart(2,"0")),mk("span","leader-avatar",b.strategy_id==="cash"?"▣":(safe(b.name,strategy.name||"?").trim()[0]||"?").toUpperCase()));const name=mk("span","leader-name");name.append(document.createTextNode(safe(b.name,strategy.name||"Учасник")),mk("small","leader-author",authorName(b)));row.append(name);const result=mk("span",`leader-result${Number(b.pnl_usdt)<0?" negative":""}`);result.append(document.createTextNode(`${money(b.equity_usdt)} USDT`),mk("small","",`${pct(b.return_pct)} · ${money(b.pnl_usdt)} PnL`));row.append(result);host.append(row);}
    if(!bots.length)host.append(mk("div","event-empty","Очікуємо першого кадру."));
  }
  function renderSeasonActions(season){
    const host=$("season-actions");host.replaceChildren();
    if(state.replay){if(state.snapshot?.active)host.append(button("season-resume","↩ Повернутися до поточного сезону",()=>{pauseReplay();state.replay=false;state.replayData=null;state.replayIndex=0;state.detailsBotId=null;renderState(state.snapshot);}));return;}
    const active=["RUNNING","DRAINING","CLOSING"].includes(season.status);
    if(active)host.append(button("season-stop-all","■ Зупинити сезон · закрити всі paper-позиції",()=>stopSeason(season,null),state.busy));
    if(season.status==="PAUSED")host.append(button("season-resume","↻ Відновити закриття позицій",()=>resumeSeason(season),state.busy));
    if(active){for(const b of season.bots||[]){if(b.strategy_id==="cash"||["STOPPED","DONE"].includes(b.status))continue;host.append(button("bot-stop",`Зупинити ${safe(b.name,"бота")}`,()=>stopSeason(season,b.id),state.busy));}}
  }
  function renderHistory(seasons){
    const host=$("season-list");host.replaceChildren();
    if(!seasons.length){host.append(mk("div","history-empty","Тут з’явиться запис кожного завершеного сезону."));return;}
    for(const season of seasons){const strategyCount=Number.isFinite(Number(season.participants))?Math.max(0,Number(season.participants)-1):"—";const row=mk("div","season-item");row.tabIndex=0;row.setAttribute("role","button");const result=season.incomplete?"Підсумок неповний":`Лідер · ${season.winner_name||season.leader_name||"—"}`;const status=season.incomplete?`Неповний · ${labels[season.status]||safe(season.status)}`:(labels[season.status]||safe(season.status));row.append(mk("span","season-id",`№ ${safe(season.id,"—")}`),mk("span","season-date",dateTime(season.created_at)),mk("span","season-participants",`${strategyCount} стратегій + Скриня · ${season.execution_mode==="shared_jev"?"Jev":"Правила"}`),mk("span","season-result",result),mk("span",`season-state ${season.incomplete?"incomplete":String(season.status).toLowerCase()}`,status));row.addEventListener("click",()=>openReplay(season.id));row.addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();openReplay(season.id);}});host.append(row);}
  }
  async function refreshArena(scheduled=false){
    try{
      const data=await request("/api/arena/state",undefined,{...POLL,viewer:scheduled});
      if(!data.active&&data.seasons?.length&&state.latestPaperRun?.id!==data.seasons[0].id)state.latestPaperRun=await request(`/api/arena/seasons/${encodeURIComponent(data.seasons[0].id)}`,undefined,POLL_RUN);
      if(state.pollNotice){if(state.notice===state.pollNotice)notify("");state.pollNotice="";}
      renderState(data);if(!state.replay&&data.active&&data.active.status!=="DONE")state.selectedSeasonId=data.active.id;
      return true;
    }catch(e){setConnected(false,"Арена недоступна");if(!state.snapshot){$("empty-arena").hidden=false;$("empty-arena").querySelector("h3").textContent="Не вдалося прочитати стан арени.";}notify(e.message,true);state.pollNotice=e.message;return false;}
  }
  // Polling policy lives in poll-scheduler.js (cadence by what is moving, paused while the tab is hidden, backoff, one request group at a time).
  // An older server that does not serve that file keeps the previous behaviour: everything every 2 s.
  async function refreshAll(parts={arena:true,exchange:true}){
    const result={};
    if(parts.arena)result.arena=await refreshArena(parts.scheduled);
    if(parts.exchange)result.exchange=await refreshExchange(parts.scheduled);
    // A PAPER-only read must not overwrite what the Live journal last said about the connection.
    else if(result.arena&&state.exchangeOk!==undefined&&state.watchMode!=="paper")setConnected(state.exchangeOk,state.exchangeOk?"Локальна панель · журнал OKX":"Журнал OKX недоступний");
    return result;
  }
  function pollContext(){return {...window.ArenaPollScheduler.activity(state.snapshot,state.exchange,["arena_live"]),paperVisible:state.watchMode==="paper"||Boolean($("management-dialog")?.open)};}
  // `?poll=always` keeps polling while the tab is hidden (automated checks in a background browser pane).
  const keepPolling=new URLSearchParams(location.search).get("poll")==="always",tabHidden=()=>document.hidden&&!keepPolling;
  const poller=window.ArenaPollScheduler?window.ArenaPollScheduler.create({run:refreshAll,context:pollContext,hidden:tabHidden}):null;
  function refresh(parts){return poller?poller.kick(parts):refreshAll(parts);}
  async function post(path,body,success){if(state.busy)return false;state.busy=true;notify("");renderBusy();try{const out=await request(path,body);success?.(out);await refresh();if(out.error)notify(out.error,true);return true;}catch(e){notify(e.message,true);refresh();return false;}finally{state.busy=false;renderBusy();}}
  function renderBusy(){if(state.snapshot)renderState(state.snapshot);}
  function generation(){return state.snapshot?.generation;}
  function startSeason(){const budget=Number($("budget-input").value),duration=Number($("duration-select").value);if(!Number.isFinite(budget)||budget<25||budget>1000){notify("Бюджет має бути від 25 до 1 000 USDT на учасника.",true);return;}if(!state.selected.size){notify("Оберіть щонайменше одну готову стратегію.",true);return;}post("/api/arena/start",{generation:generation(),strategy_ids:[...state.selected],budget_usdt:budget,duration_seconds:duration,execution_mode:$("runtime-select").value},()=>{pauseReplay();state.replay=false;state.replayData=null;state.watchMode="paper";state.watchHistoryRun=null;state.watchHistorySig="";$("watch-mode").value="paper";notify("Сезон запущено. Усі учасники використовують лише paper-виконання.");});}
  function stopSeason(season,botId){post("/api/arena/stop",{generation:generation(),season_id:season.id,bot_id:botId||null},()=>notify("Запит прийнято. Paper-сезон закриває наявні симульовані позиції."));}
  function resumeSeason(season){post("/api/arena/resume",{generation:generation(),season_id:season.id},()=>notify("Відновлення виконує лише закриття вже наявних paper-позицій."));}
  async function openReplay(id){pauseReplay();state.replay=true;state.replayData=null;state.replayIndex=0;state.detailsBotId=null;state.watchMode="paper";state.watchHistoryRun=null;state.watchHistorySig="";$("watch-mode").value="paper";try{const season=await request(`/api/arena/seasons/${encodeURIComponent(id)}`);state.replayData=season;renderState(state.snapshot);$("arena-view").scrollIntoView({behavior:motion(),block:"center"});}catch(e){state.replay=false;notify(e.message,true);}}
  function toggleReplay(){
    if(state.replayPlaying){pauseReplay();renderSeason(state.replayData,true);return;}
    const season=state.replayData,frames=season?.frames||[];
    if(!state.replay||!frames.length)return;
    if(state.replayIndex>=frames.length-1)state.replayIndex=0;
    state.replayPlaying=true;renderSeason(season,true);
    const current=()=>state.replayPlaying&&state.replay&&state.replayData===season;
    const advance=()=>{
      if(!current())return;
      if(state.replayIndex>=frames.length-1){pauseReplay();renderSeason(season,true);return;}
      const from=new Date(frames[state.replayIndex]?.at).getTime(),to=new Date(frames[state.replayIndex+1]?.at).getTime();
      const recordedMs=Number.isFinite(from)&&Number.isFinite(to)&&to>from?to-from:1000;
      state.replayTimer=setTimeout(()=>{if(!current())return;state.replayIndex++;renderSeason(season,true);advance();},Math.max(40,recordedMs/state.replaySpeed));
    };advance();
  }
  function pauseReplay(){if(state.replayTimer){clearTimeout(state.replayTimer);state.replayTimer=null;}state.replayPlaying=false;}
  function bindReplay(){ $("replay-toggle").addEventListener("click",toggleReplay);$("replay-range").addEventListener("input",()=>{pauseReplay();state.replayIndex=Number($("replay-range").value);if(state.replayData)renderSeason(state.replayData,true);});$("replay-speed").addEventListener("change",()=>{state.replaySpeed=Number($("replay-speed").value)||1;if(state.replayTimer){pauseReplay();toggleReplay();}}); }
  function showDialog(title,kicker,body){$("detail-title").textContent=title;$("detail-kicker").textContent=kicker;const host=$("detail-body");host.replaceChildren();host.append(body);$("detail-dialog").showModal();}
  function section(title,content){const box=mk("section","detail-section");box.append(mk("h3","",title));box.append(content);return box;}
  function stat(label,value){const s=mk("div","detail-stat");s.append(mk("span","",label),mk("b","",value));return s;}
  function openStrategy(strategy){const body=mk("div","");const grid=mk("div","detail-grid");grid.append(stat("Авторство",authorLine(strategy)),stat("Версія / статус",`v${safe(strategy.version,"—")} · ${labels[strategy.status]||safe(strategy.status)}`),stat("Модель та дата",[strategy.author?.model,strategy.author?.submitted_at?dateTime(strategy.author.submitted_at):null].filter(Boolean).join(" · ")||"Не вказано"),stat("Редагування",safe(strategy.author?.edited_by,"Без додаткового запису")));if(strategy.status==="ready")grid.append(stat("Спроби автора",safe(strategy.author?.attempts,"Не зафіксовано")));body.append(grid,section("Ідея",mk("p","",safe(strategy.description,"Опис ще не подано."))),section("Оригінальний текст автора",mk("pre","",safe(strategy.original_text||strategy.author?.prompt,"Оригінальний опис не подано."))));const rules=mk("div","rule-list");for(const [k,v] of Object.entries(strategy.rules||{})){const entry=mk("div","rule-item");entry.textContent=`${k.replaceAll("_"," ")}: ${typeof v==="object"?JSON.stringify(v):String(v)}`;rules.append(entry);}if(Object.keys(strategy.rules||{}).length)body.append(section("Зафіксовані правила",rules));if(strategy.recipe_sha256)body.append(section("Відбиток зафіксованої версії",mk("p","",strategy.recipe_sha256)));showDialog(safe(strategy.name,"Стратегія"),"ПАСПОРТ · КАТАЛОГ",body);}
  function openDraft(strategy){if(!window.ArenaStrategyEditor?.open){notify("Редактор стратегій ще завантажується.",true);return;}window.ArenaStrategyEditor.open(strategy,saveStrategy);}
  async function detailSeason(season){if(season.bots&&season.events&&season.history_complete!==false)return season;const route=season._exchange?`/api/exchange/runs/${encodeURIComponent(season.id)}`:`/api/arena/seasons/${encodeURIComponent(season.id)}`;return request(route);}
  function saveText(name,content,type){const url=URL.createObjectURL(new Blob([content],{type:`${type};charset=utf-8`})),link=document.createElement("a");link.href=url;link.download=name;link.hidden=true;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  // B-19: one reproducible document per participant, built from the review bundle (a plain GET, like the review page). Facts only, no advice.
  function authorReportBox(season,bot){
    const box=mk("div","author-report-box"),status=mk("p","author-report-status"),out=mk("pre","author-report"),actions=mk("div","author-report-actions");
    let text="";status.setAttribute("role","status");out.hidden=true;
    const copy=button("quiet-button","Копіювати",async()=>{try{await navigator.clipboard.writeText(text);status.textContent="Скопійовано.";}catch{status.textContent="Браузер не дозволив копіювання: виділіть текст вручну.";}});
    const save=button("quiet-button","Завантажити .md",()=>saveText(`author-report-${bot.id}.md`,text,"text/markdown"));
    copy.hidden=save.hidden=true;
    const make=button("quiet-button","Підготувати звіт",async()=>{
      make.disabled=true;status.textContent="Читаємо журнал бою…";
      try{const bundle=await request(`/api/review/${season._exchange?"exchange":"paper"}/${encodeURIComponent(season.id)}`,undefined,POLL_RUN);text=window.ArenaAuthorReport.build(bundle,bot.id,{context:window.ArenaBattleContext?.fromFrames(season.frames,{fundingRate:season.latest_market?.funding_rate,coin:window.ArenaReviewModel?.botCoin?.(bot,season)??seasonCoin(season),instrument:season.rules?.instruments?.[bot.id]})}).markdown;out.textContent=text;out.hidden=copy.hidden=save.hidden=false;status.textContent="Звіт готовий: воронка рішень, умови, угоди, витрати. Лише факти.";}
      catch(error){status.textContent=error.message;}finally{make.disabled=false;}
    });
    actions.append(make,copy,save);box.append(actions,status,out);return box;
  }
  async function openBotDetail(season,bot,strategy){try{const isExchange=Boolean(season._exchange),full=await detailSeason(season);const inReplay=!isExchange&&state.replay&&state.replayData?.id===full.id;const frame=inReplay?(full.frames||[])[state.replayIndex]||null:null;const frameBot=frame?.bots?.find(x=>x.id===bot.id);const finalBot=(full.bots||[]).find(x=>x.id===bot.id)||bot;const actual=Object.assign({},finalBot,frameBot||{});state.detailsBotId=actual.id;const body=mk("div","");const grid=mk("div","detail-grid");grid.append(stat("Автор",authorLine({...strategy,author:actual.author||strategy.author})),stat("Стан",labels[actual.status]||safe(actual.status)),stat("Equity",`${money(actual.equity_usdt,8)} USDT`),stat("PnL / дохідність",`${(window.ArenaColiseum?.pnl(actual.pnl_usdt) ?? money(actual.pnl_usdt,8))} USDT · ${pct(actual.return_pct)}`),stat("Realized / unrealized",`${money(actual.realized_pnl_usdt,8)} / ${money(actual.unrealized_pnl_usdt,8)} USDT`),stat("Комісії / funding",`${money(actual.fees_usdt,8)} / ${money(actual.funding_usdt,8)} USDT`),stat("Jev",`${usdCost(actual.ai_cost_usd,actual.ai_cost_complete!==false)} · ${actual.ai_requests||0} запитів`),stat("Макс. просадка",pct(actual.max_drawdown_pct)));if(isExchange)grid.append(stat("OKX subaccount",safe(actual.account_id||actual.profile_id,"Прив’язка в сезоні")));if(inReplay&&frame?.at)grid.append(stat("Кадр повтору",dateTime(frame.at)));body.append(grid);
      if(!inReplay&&window.ArenaBattleFacts&&actual.strategy_id!=="cash"&&actual.avatar_kind!=="baseline"){const holder=mk("div","");window.ArenaBattleFacts.render(holder,window.ArenaBattleFacts.factsForBot({bot:actual,lifecycleBot:(full.lifecycle?.bots||[]).find(x=>x.bot_id===actual.id),events:full.events,run:full}),{omit:full.lifecycle?["Вхід"]:[]});body.append(section("Що сталося",holder));}
      if(!inReplay&&window.ArenaAuthorReport&&window.ArenaBattleFacts&&actual.strategy_id!=="cash"&&actual.avatar_kind!=="baseline")body.append(section("Звіт автору",authorReportBox(full,actual)));
      if(!inReplay&&window.ArenaLifecycleV6&&full.lifecycle){const evidence=window.ArenaLifecycleV6.renderBotDetail(full.lifecycle,actual.id,{includeManifest:true});if(evidence)body.append(evidence);}
      body.append(section("Паспорт стратегії",mk("p","",`${safe(strategy.description,"Опису немає.")}\n\nВерсія ${safe(strategy.version)} · ${safe(strategy.recipe_sha256,"Відбиток версії не записано")}`)));
      const original=mk("pre","",safe(strategy.original_text||strategy.author?.prompt,"Оригінальний текст автора не надано."));body.append(section("Оригінальні правила автора",original));
      const ruleBox=mk("div","rule-list");for(const [k,v] of Object.entries(strategy.rules||{})){const r=mk("div","rule-item");r.textContent=`${k.replaceAll("_"," ")}: ${typeof v==="object"?JSON.stringify(v):String(v)}`;ruleBox.append(r);}body.append(section("Формалізовані правила",ruleBox));
      const shownChecks=inReplay?(frameBot?.checks||[]):(actual.checks||[]);if(shownChecks.length){const checks=mk("div","checks");for(const c of shownChecks){const row=mk("div","check-row");const passed=c.passed===null||c.passed===undefined?null:Boolean(c.passed);row.append(mk("span","",safe(c.feature)),mk("span","",`${safe(c.op)} ${safe(c.expected)}`),mk("span","",safe(c.actual)),mk("b",passed===null?"check-unknown":passed?"check-pass":"check-fail",passed===null?"?":passed?"✓":"×"));checks.append(row);}body.append(section(inReplay?"Перевірки в цьому кадрі":"Фактичні перевірки",checks));}else body.append(section(inReplay?"Перевірки в цьому кадрі":"Фактичні перевірки",mk("p","empty-data",inReplay?"Кадр не містить окремого знімка перевірок.":"Немає записаних перевірок.")));
      const allCurve=actual.curve||[];const curve=inReplay&&frame?.at?allCurve.filter(p=>!p.at||new Date(p.at)<=new Date(frame.at)):allCurve;body.append(section(`Капітал у часі · equity USDT${inReplay?" · до вибраного кадру":""}`,chart(curve)));
      let events=(full.events||[]).filter(e=>e.bot_id===actual.id);if(inReplay&&frame){events=events.filter(e=>(frame.event_seq===undefined||Number(e.seq)<=Number(frame.event_seq))&&(!e.at||new Date(e.at)<=new Date(frame.at)));}
      const fills=events.filter(e=>/entry.*fill|close.*fill|exit.*fill/.test(String(e.code||"" ).toLowerCase()));if(fills.length){
        // OKX sizes orders in contracts, the journal keeps the position in coins of the season instrument: never one unlabelled column for both.
        const U=window.ArenaReviewModel,coin=U?.botCoin?.(actual,full)??seasonCoin(full),table=mk("table","order-table"),thead=mk("thead"),tr=mk("tr");for(const [label,hint] of [["Час"],["Дія"],["Ціна, USDT"],["Обсяг ордера",`OKX: контракти, виконані за ордером на момент запису (накопичено). PAPER: обсяг заповнення в ${coin}.`],["Комісія, USDT"],["Gross PnL, USDT"],["Позиція після",`Позиція учасника після запису, в ${coin}; мінус — short.`]]){const th=mk("th","",label);if(hint)th.title=hint;tr.append(th);}thead.append(tr);const tb=mk("tbody");for(const e of fills){const c=U.fillCells(e,coin,money),row=mk("tr");for(const value of [time(e.at),e.code||eventText[e.code]||"Заповнення",c.price,c.filled,c.fee,c.gross,c.after])row.append(mk("td","",value));tb.append(row);}table.append(thead,tb);const box=mk("div","");box.append(table);const note=isExchange?U.contractNote(full,actual):null;if(note)box.append(mk("p","",`${note}. OKX рахує ордери в контрактах; позицію тут показано в ${coin}.`));body.append(section(`${isExchange?`OKX ${full.mode||"Live"}`:"Paper"}-виконання · записи заповнення`,box));}else body.append(section(isExchange?"OKX execution":"Paper-виконання",mk("p","empty-data","До цього кадру ще немає записів про заповнення заявок.")));
      const timeline=mk("div","detail-timeline");if(!events.length)timeline.append(mk("p","empty-data","Записаних подій поки немає."));for(const e of [...events].reverse()){const item=mk("details","timeline-item"),summary=mk("summary","timeline-row");summary.append(mk("span","timeline-time",time(e.at)),mk("span","event-step",eventIcon[e.code]||String(e.step||"·")),mk("span","",safe(e.message,eventText[e.code]||e.code)));item.append(summary);if(e.data&&Object.keys(e.data).length)item.append(mk("pre","timeline-data",JSON.stringify(e.data,null,2)));timeline.append(item);}body.append(section(inReplay?"Хронологія до вибраного кадру":"Хронологія рішень і подій",timeline));
      showDialog(safe(actual.name,strategy.name||"Учасник"),inReplay?`ПАСПОРТ УЧАСНИКА · КАДР ${dateTime(frame?.at)}`:isExchange?`ПАСПОРТ УЧАСНИКА · ${full.mode==="OKX_DEMO"?"OKX Demo":"OKX Live"}`:"ПАСПОРТ УЧАСНИКА · PAPER",body);
    }catch(e){notify(`Не вдалося відкрити деталі: ${e.message}`,true);}}
  function chart(points){const svg=document.createElementNS("http://www.w3.org/2000/svg","svg");svg.setAttribute("viewBox","0 0 700 170");svg.setAttribute("class","detail-chart");svg.setAttribute("role","img");svg.setAttribute("aria-label","Графік equity учасника");const makeText=(x,y,text,anchor="start")=>{const t=document.createElementNS(svg.namespaceURI,"text");t.setAttribute("x",String(x));t.setAttribute("y",String(y));t.setAttribute("text-anchor",anchor);t.setAttribute("fill","#83907d");t.setAttribute("font-size","9");t.textContent=text;return t;};if(points.length<2){const t=makeText(350,82,"Для графіка ще недостатньо точок equity","middle");svg.append(t);return svg;}const valid=points.filter(p=>numeric(p.equity_usdt)!==null),vals=valid.map(p=>Number(p.equity_usdt));if(vals.length<2)return svg;const min=Math.min(...vals),max=Math.max(...vals),pad=Math.max((max-min)*.14,.01);const y=p=>12+(max+pad-p)/(max-min+2*pad)*120;const line=document.createElementNS(svg.namespaceURI,"polyline");line.setAttribute("points",valid.map((p,i)=>`${34+i*630/Math.max(1,valid.length-1)},${y(Number(p.equity_usdt))}`).join(" "));line.setAttribute("fill","none");line.setAttribute("stroke","#799853");line.setAttribute("stroke-width","3");line.setAttribute("stroke-linecap","round");line.setAttribute("stroke-linejoin","round");svg.append(line,makeText(29,16,money(max,3),"end"),makeText(29,133,money(min,3),"end"),makeText(34,154,time(valid[0].at)),makeText(664,154,time(valid.at(-1).at),"end"));return svg;}
  async function saveStrategy(strategy){const ok=await post("/api/arena/strategies",{generation:generation(),strategy},()=>{state.catalogueSig="";state.selected.clear();notify("Стратегію збережено у каталозі.");});if(!ok)throw new Error($("season-message").textContent||"Не вдалося зберегти стратегію.");}
  async function openEditor(){if(!window.ArenaStrategyEditor?.open){notify("Редактор стратегій ще завантажується.",true);return;}const chosen=[...state.selected];const template=chosen.length===1?(state.snapshot.catalogue||[]).find(s=>s.id===chosen[0]):null;window.ArenaStrategyEditor.open(template||null,saveStrategy);}
  function bindBattleDemo(){
    const dialog=$("battle-demo-dialog"), range=$("battle-demo-frame"), frames=window.ArenaBattle.demoFrames();
    range.max=String(frames.length-1);
    let demo=null,timer=null,index=0;
    function stop(){if(timer!==null)clearInterval(timer);timer=null;}
    function paint(running,finished=false){
      const current=frames[index],previous=frames[index-1];range.value=String(index);$("battle-demo-counter").textContent=(index+1)+" / "+frames.length;
      demo.update({id:"visual-demo",key:index,budget:100,bots:current.bots,previousBots:previous?.bots,
        events:current.events,eventSeq:current.eventSeq,previousEventSeq:previous?.eventSeq,
        at:new Date().toISOString(),mode:"demo",moving:running,finished});
    }
    function play(){stop();index=0;paint(true);timer=setInterval(()=>{index++;if(index>=frames.length-1){index=frames.length-1;stop();paint(true);timer=setTimeout(()=>{timer=null;paint(false,true);},3500);return;}paint(true);},3500);}
    $("battle-demo").addEventListener("click",()=>{if(demo)return;dialog.showModal();demo=window.ArenaBattle.create($("battle-demo-floor"),id=>{
      const bot=frames[index].bots.find(b=>b.id===id);$("battle-demo-info").textContent=bot.name+": вигаданий капітал "+money(bot.equity_usdt)+" USDT, PnL "+money(bot.pnl_usdt)+" USDT. Це персонаж демонстрації, не стратегія й не ринковий результат.";
    },play);play();});
    $("battle-demo-close").addEventListener("click",()=>dialog.close());
    $("battle-demo-restart").addEventListener("click",play);
    range.addEventListener("input",()=>{stop();index=Number(range.value);paint(false);});
    dialog.addEventListener("close",()=>{stop();demo?.destroy();demo=null;});
    dialog.addEventListener("click",event=>{if(event.target===dialog)dialog.close();});
  }
  function bind(){
    $("start-season").addEventListener("click",startSeason);$("new-strategy").addEventListener("click",()=>window.ArenaStrategyEditor?.open(null,saveStrategy));$("edit-strategy").addEventListener("click",openEditor);
    $("detail-close").addEventListener("click",()=>$("detail-dialog").close());$("detail-dialog").addEventListener("click",e=>{if(e.target===$("detail-dialog"))$("detail-dialog").close();});$("detail-dialog").addEventListener("close",()=>{state.detailsBotId=null;});bindReplay();
  }
  bind();bindColiseum();
  $("live-check").addEventListener("click",checkLiveProfiles);$("live-start").addEventListener("click",startLiveSeason);$("live-budget").addEventListener("input",()=>syncLiveControls());$("live-budget").addEventListener("change",()=>syncLiveControls());
  $("live-execution-mode").addEventListener("change",()=>syncLiveControls());
  bindBattleDemo();
  if(poller){document.addEventListener("visibilitychange",()=>poller.setHidden(tabHidden()));poller.start();}
  else{refreshAll();state.timer=setInterval(refreshAll,2000);}
  window.addEventListener("beforeunload",()=>{pauseReplay();poller?.stop();clearInterval(state.timer);battle?.destroy();liveBattle?.destroy();coliseum?.destroy();});
})();
