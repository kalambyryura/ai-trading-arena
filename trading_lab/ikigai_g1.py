"""Fixed built-in G1 PAPER bridge candidate for root review. No network/file I/O.

Fixed reference/spec. JSON state is stored inside the existing bot checkpoint.
Production integration still requires the accompanying reviewed engine hooks.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import uuid

from . import ikigai_g1_reference as ref

TYPE = "IKIGAI_G1_PAPER_BUILTIN_V1"
SPEC_SHA256 = "18ab6f5f2001d2300ce91650af31787f5c035c3f77ed340e11efcc35cc7718f8"
REFERENCE_SHA256 = "912015f147731f85907465d83b9cc680613ae32df589214ec58718d8deb25312"
SCHEMA = "ikigai-g1-paper-state/v1"
MAX_IDS = 1024
RULES = {"builtin": TYPE, "spec_sha256": SPEC_SHA256,
         "side": "both", "entry": {"mode": "any", "conditions": []},
         "exit": {"mode": "any", "conditions": []},
         "size_pct": 30, "max_entries": 1, "add_after_bps": 500,
         "stop_loss_bps": 500, "take_profit_bps": 1000,
         "max_hold_seconds": 86400, "cooldown_seconds": 14400,
         "max_spread_bps": 10}


class G1Error(ValueError):
    pass


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode()).hexdigest()


def enabled(recipe):
    return isinstance(recipe, dict) and isinstance(recipe.get("rules"), dict) and "builtin" in recipe["rules"]


def assert_recipe(recipe):
    if not enabled(recipe) or digest(recipe["rules"]) != digest(RULES):
        raise G1Error("g1_builtin_spec_or_rules_mismatch")


def assert_paper(state):
    if state.get("mode") != "PAPER" or state.get("execution_mode", "rules") not in {"rules", "rules_only"}:
        raise G1Error("g1_paper_only_live_demo_sharedjev_forbidden")


def validate_payload(payload, version=1):
    # Fixed built-in descriptor, not arbitrary strategy code or parameter overrides.
    from .arena_strategies import text_field, number, fingerprint, ArenaError
    expected = {"name", "author", "description", "original_text", "confirmed", "rules", "strategy_schema_version"}
    if (not isinstance(payload, dict) or set(payload) != expected or payload.get("confirmed") is not True
            or type(payload.get("strategy_schema_version")) is not int or payload['strategy_schema_version']!=3):
        raise ArenaError("G1: fixed confirmed built-in descriptor required")
    try:
        assert_recipe(payload)
    except G1Error as error:
        raise ArenaError(str(error)) from None
    a = payload["author"]
    if (not isinstance(a, dict) or set(a) != {"name", "kind", "model", "submitted_at", "prompt", "attempts", "edited_by"}
            or a.get("kind") != "human"):
        raise ArenaError("G1: human passport with AI formalization attribution required")
    author = {k: text_field(a[k], {"name":60, "model":150, "submitted_at":60,
              "prompt":8000, "edited_by":300}[k], k == "name")
              for k in ("name", "model", "submitted_at", "prompt", "edited_by")}
    attempts = number(a["attempts"], 1, 1000)
    if int(attempts) != attempts:
        raise ArenaError("G1: integer attempts required")
    author.update(kind="human", attempts=attempts)
    clean = {"name":text_field(payload["name"],60), "author":author,
             "description":text_field(payload["description"],1000),
             "original_text":text_field(payload["original_text"],16000),
             "confirmed":True,"strategy_schema_version":3,"rules":deepcopy(RULES)}
    # The portable payload alone owns recipe identity. Display/readiness fields
    # are runtime metadata and strategy_contract.payload_of intentionally omits them.
    recipe_hash=fingerprint(clean)
    clean.update(builtin_type=TYPE,builtin_spec_sha256=SPEC_SHA256,
                 readiness="PAPER_INTEGRATION_CANDIDATE_ONLY",
                 id=uuid.uuid4().hex, version=version, status="draft",recipe_sha256=recipe_hash,
                 created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return clean


def _number(value, positive=False):
    if not isinstance(value, (int,float)) or isinstance(value,bool) or not math.isfinite(value) or (positive and value <= 0):
        raise G1Error("g1_numeric_input_unknown")
    return value


def instrument(snapshot):
    try:
        raw = snapshot["raw"]["instrument"][0]
        if raw["instId"] != "BTC-USDT-SWAP" or raw["ctValCcy"] != "BTC" or raw["settleCcy"] != "USDT":
            raise G1Error("g1_instrument_mismatch")
        values = [float(raw[k]) for k in ("tickSz","ctVal","lotSz","minSz")]
        tick, unit, lot, minimum = [_number(x,True) for x in values]
        unit *= _number(float(raw.get("ctMult") or 1),True)
        obj = ref.Instrument(tick,unit,lot,minimum)
        for expected,actual in [(unit*lot,snapshot["lot_btc"]),(unit*minimum,snapshot["min_btc"])]:
            if not math.isclose(expected,_number(actual,True),rel_tol=1e-10,abs_tol=1e-14):
                raise G1Error("g1_normalized_lot_mismatch")
        return obj
    except (KeyError,IndexError,TypeError,ValueError) as error:
        raise G1Error("g1_instrument_metadata_unknown") from error


def _decode_setup(raw):
    if raw is None:
        return None
    obj = deepcopy(raw)
    obj["anchors"] = tuple(ref.Pivot(**x) for x in obj["anchors"])
    obj["context"] = tuple(tuple(x) for x in obj["context"])
    if len(obj["anchors"]) != 6:
        raise G1Error("g1_anchors_corrupt")
    return ref.Setup(**obj)


def _decode_signal(raw):
    return ref.Signal(_decode_setup(raw["setup"]),raw["decided_at"],raw["signal_bar_ts"])


def _dump_detector(d):
    return {"bars":[asdict(b)for b in d.bars],"pivots":[asdict(p)for p in d.pivots],
            "pending":asdict(d.pending)if d.pending else None,
            "used_setup_ids":sorted(d.used_setup_ids),"last_seen_ts":d.last_seen_ts,
            "cooldown_until":d.cooldown_until,"last_flat_at":d.last_flat_at}


def _restore_detector(raw, tick):
    if len(raw["bars"])>256 or len(raw["pivots"])>6 or len(raw["used_setup_ids"])>MAX_IDS:
        raise G1Error("g1_checkpoint_capacity")
    d = ref.Detector(tick)
    d.bars = [ref.Bar(**x)for x in raw["bars"]]
    d.pivots = [ref.Pivot(**x)for x in raw["pivots"]]
    d.pending = _decode_setup(raw["pending"])
    d.used_setup_ids = set(raw["used_setup_ids"])
    d.last_seen_ts = raw["last_seen_ts"]
    d.cooldown_until = raw["cooldown_until"]
    d.last_flat_at = raw["last_flat_at"]
    return d


def _save(bot, data, detector=None):
    if detector is not None:
        data["detector"] = _dump_detector(detector)
    data.pop("checksum",None)
    data["checksum"] = digest(data)
    bot["_ikigai_g1"] = data


def _load(bot, recipe, snapshot=None):
    assert_recipe(recipe)
    data = deepcopy(bot.get("_ikigai_g1"))
    if data is None:
        if bot.get("position_btc") or bot.get("_pending"):
            raise G1Error("g1_missing_checkpoint_with_position_or_intent")
        if snapshot is None:
            raise G1Error("g1_snapshot_required_for_initialization")
        meta = instrument(snapshot)
        data = {"schema":SCHEMA,"spec_sha256":SPEC_SHA256,"reference_sha256":REFERENCE_SHA256,
                "recipe_sha256":recipe["recipe_sha256"],"instrument":asdict(meta),
                "detector":_dump_detector(ref.Detector(meta.tick_size)),"bootstrapped":False,
                "bar_inputs":{},"ready_signal":None,"ready_local_at":None,"reserved":None,"cycle":None,
                "entry_error":None,"attempts":0,"terminal_intents":{},"failed_closes":0,
                "failed_exit_latched":False,"last_failure_bar":None}
        _save(bot,data)
    else:
        checksum = data.pop("checksum",None)
        if (checksum != digest(data) or data["schema"] != SCHEMA or data["spec_sha256"] != SPEC_SHA256
                or data["reference_sha256"] != REFERENCE_SHA256 or data["recipe_sha256"] != recipe["recipe_sha256"]):
            raise G1Error("g1_checkpoint_epoch_or_checksum")
        if len(data["terminal_intents"]) > MAX_IDS:
            raise G1Error("g1_consumed_ids_capacity")
    d = _restore_detector(data["detector"],data["instrument"]["tick_size"])
    return data,d


def _bar(raw, now):
    # Interface from the HTF provider; no request/aggregation code lives here.
    if set(raw) != {"open_ts","received_at","open","high","low","close","complete"}:
        raise G1Error("g1_h1_record_shape")
    received = math.ceil(_number(raw["received_at"]))
    b = ref.Bar(**{**raw,"received_at":received})
    if (type(b.open_ts) is not int or b.open_ts%3600 or b.close_ts>now or raw['received_at']<b.close_ts
            or raw["received_at"]>now or b.complete is not True
            or not all(_number(x,True)>0 for x in (b.open,b.high,b.low,b.close))
            or not b.low<=min(b.open,b.close)<=max(b.open,b.close)<=b.high):
        raise G1Error("g1_h1_not_completed_or_invalid")
    return b


def provider_view(snapshot):
    """Transient projection of Astra's strict compressed raw contract; not stored in snapshot."""
    from . import causal_htf as htf
    view=htf.g1_view(snapshot)
    if view['status']!='READY':
        raise G1Error('g1_htf_unknown:'+str(view['reason']))
    return {'ready':True,'bootstrap':True,'bars':view['records'],
            'signal_eligible':view['h1_signal_eligible']}


def advance(state,bot,recipe,snapshot,emit):
    """Hook BEFORE fill_pending. Reference uses exchange seconds; core uses local wall seconds.

    Bootstrap loads historical structure only, never a historical entry/setup.
    Compressed raw is the only source; projected rows remain transient.
    """
    assert_paper(state)
    local_now = snapshot["at"]
    now = math.floor(_number(snapshot['server_time'],True))
    try:
        data,d = _load(bot,recipe,snapshot)
        if asdict(instrument(snapshot)) != data["instrument"]:
            raise G1Error("g1_instrument_epoch_changed")
        packet = provider_view(snapshot)
        if not isinstance(packet,dict) or packet.get("ready") is not True or not isinstance(packet.get("bars"),list) or len(packet["bars"])>256:
            raise G1Error("g1_htf_provider_unknown")
        records = [_bar(row,now)for row in packet["bars"]]
        if not records or any(b.open_ts<=a.open_ts for a,b in zip(records,records[1:])):
            raise G1Error("g1_htf_order_unknown")
        if not data["bootstrapped"]:
            if packet.get("bootstrap") is not True or bot.get("position_btc"):
                raise G1Error("g1_bootstrap_required")
            if any(b.open_ts!=a.open_ts+3600 for a,b in zip(records,records[1:])):
                raise G1Error("g1_bootstrap_gap")
            for b in records:
                d.bars.append(b); d.bars=d.bars[-256:]
                pivot=d._confirmed_pivot(now)
                if pivot:
                    d.pivots=([pivot]if d.pivots and d.pivots[-1].kind==pivot.kind else (d.pivots+[pivot])[-6:])
                d.last_seen_ts=b.open_ts
            d.events=[]; d.pending=None
            data["bootstrapped"]=True
            emit(state,local_now,bot,1,"g1_warmup","G1: передісторія завантажена без ретроспективного входу.",bars=len(records),exchange_known_at=now,simulated=True)
        else:
            for b in records:
                key=str(b.open_ts)
                content=digest({k:v for k,v in asdict(b).items()if k!='received_at'})
                old=data["bar_inputs"].get(key)
                if old is not None:
                    if old!=content:
                        raise G1Error("g1_h1_revision")
                    continue
                if d.last_seen_ts is not None and b.open_ts<=d.last_seen_ts:
                    continue
                if now<b.received_at:
                    continue  # ceil-to-second is conservative; retry on later poll
                # Admission belongs to core local time, never compare it with exchange timestamps.
                admission_open=(packet['signal_eligible']and state['status']=='RUNNING'and local_now<state['_end']
                                and not state.get('admission_blocked')and not bot.get('stop_requested'))
                traces=d.on_bar(b,now,now+1 if admission_open else now,bool(bot.get("position_btc")))
                for trace in traces:
                    emit(state,local_now,bot,1,"g1_"+trace["kind"].lower(),"G1: причинний перехід стану.",trace=trace,exchange_known_at=now,simulated=True)
                    if trace["kind"]=='ENTRY_SIGNAL':
                        data["ready_signal"]=trace["signal"]
                        data['ready_local_at']=local_now
                    if trace['kind'].startswith('UNKNOWN'):
                        raise G1Error('g1_completed_bar_unknown')
                if data.get("cycle") and b.open_ts>=data["cycle"]["first_fill_at_exchange"] and b.open_ts!=data["last_failure_bar"]:
                    sign=1 if data["cycle"]["side"]=='long'else-1
                    inside=sign*(b.close-data["cycle"]["b_level"])<=0
                    data["failed_closes"]=data["failed_closes"]+1 if inside else 0
                    data["failed_exit_latched"] |= data["failed_closes"]>=2
                    data["last_failure_bar"]=b.open_ts
                d.events=[]
        data["bar_inputs"]={str(b.open_ts):digest({k:v for k,v in asdict(b).items()if k!='received_at'})for b in d.bars}
        if len(d.used_setup_ids)>=MAX_IDS:
            raise G1Error("g1_setup_capacity")
        data["entry_error"]=None
        bot.pop("_g1_error",None)
        _save(bot,data,d)
    except (G1Error,KeyError,TypeError,ValueError) as error:
        bot["_g1_error"]=str(error)
        cancel(state,bot,recipe,"DATA_UNKNOWN")
        emit(state,local_now,bot,1,"g1_input_unknown","G1: невідомий стан; нового входу немає.",reason=str(error),simulated=True)


def entry_gates(state,bot,recipe,snapshot,intent_id=None):
    assert_paper(state)
    try:
        data,_ = _load(bot,recipe)
        raw=(data["reserved"]or{}).get("signal") if intent_id else data["ready_signal"]
        local_decided=(data['reserved']or{}).get('local_decided_at')if intent_id else data.get('ready_local_at')
        allowed=(raw is not None and not bot.get("_g1_error") and not data["entry_error"]
            and not bot.get("position_btc") and not bot.get("stop_requested")
            and state["status"]=='RUNNING' and snapshot["at"]<state["_end"]
            and not state.get("admission_blocked") and bot.get("_total_entries",0)<20
            and (data["attempts"]<=20 if intent_id else data["attempts"]<20)
            and math.floor(snapshot['server_time'])>=data["detector"]["cooldown_until"]
            and local_decided is not None and local_decided<=snapshot["at"]<=local_decided+60
            and (not intent_id or data["reserved"]["intent_id"]==intent_id))
    except (G1Error,KeyError,TypeError):
        allowed=False
    bot["checks"]=[{"feature":"g1_frozen_signal_and_paper_risk","passed":bool(allowed)}]
    return bool(allowed)


def reserve(state,bot,recipe,snapshot,identity):
    if not entry_gates(state,bot,recipe,snapshot):
        raise G1Error("g1_reserve_without_signal")
    data,d=_load(bot,recipe)
    data["reserved"]={"intent_id":identity,"signal":data["ready_signal"],"created_at":snapshot["at"],
                      'local_decided_at':data['ready_local_at']}
    data["ready_signal"]=None; data["attempts"]+=1
    _save(bot,data,d)


def cancel(state,bot,recipe,reason="NO_FILL"):
    try:
        data,d=_load(bot,recipe)
        pending=data["reserved"]
        if pending:
            data["terminal_intents"][pending["intent_id"]]={"status":"NO_FILL","reason":reason}
        data["reserved"]=None; data["ready_signal"]=None
        if reason=='DATA_UNKNOWN':
            data['entry_error']='DATA_UNKNOWN'
            cycle=data.get('cycle')
            if (bot.get('position_btc')and cycle and data['bootstrapped']
                    and cycle['position_btc']==bot['position_btc']):
                # A transient provider fault is not a revision of previously
                # verified bars. Keep their causal coverage for owned exits;
                # resume only on a fresh guarded packet, never bootstrap entries.
                d.pending=None
            else:
                d._reset(); data['bootstrapped']=False
        _save(bot,data,d)
    except (G1Error,KeyError,TypeError,ValueError):
        pass  # Corrupt state remains blocked; never synthesize a fresh checkpoint.
    if bot.get("_pending",{} ) and bot["_pending"].get("kind")=='entry':
        bot["_pending"]=None


def consume_fill_plan(state,bot,recipe,snapshot,identity):
    assert_paper(state)
    if not entry_gates(state,bot,recipe,snapshot,identity):
        cancel(state,bot,recipe,"FILL_RECHECK")
        return {"kind":"NO_ENTRY","reason":"FILL_RECHECK"}
    data,d=_load(bot,recipe)
    signal=_decode_signal(data["reserved"]["signal"])
    meta=instrument(snapshot)
    if asdict(meta)!=data["instrument"]:
        cancel(state,bot,recipe,"METADATA_CHANGED")
        return {"kind":"NO_ENTRY","reason":"METADATA_CHANGED"}
    side=signal.setup.side
    exchange_now=math.floor(snapshot['server_time'])
    quote=ref.Quote(snapshot["quote_ts"]/1000,exchange_now,snapshot["bid"],snapshot["ask"],
                    snapshot["ask_btc"]if side=='long'else snapshot["bid_btc"])
    # Local cutoff was rechecked above. Limit vendor quote plan to this exchange-time batch.
    plan=ref.plan_entry(signal,quote,meta,bot["equity_usdt"],exchange_now+1)
    data["reserved"]=None
    data["terminal_intents"][identity]={"status":"PLAN"if plan["kind"]=='OFFLINE_ENTRY_PLAN'else'NO_FILL',
                                        "setup_id":signal.setup.setup_id}
    if plan["kind"]=='OFFLINE_ENTRY_PLAN':
        plan.update(signal=asdict(signal),intent_id=identity)
    _save(bot,data,d)
    return plan


def on_entry_fill(bot,recipe,snapshot,plan,quantity,price):
    data,d=_load(bot,recipe)
    if data["cycle"] or quantity<=0 or plan["kind"]!='OFFLINE_ENTRY_PLAN':
        raise G1Error("g1_unexpected_fill")
    signal=_decode_signal(plan["signal"])
    data["cycle"]={"side":plan["side"],"stop":plan["stop"],"target":plan["target"],
                    "b_level":signal.setup.anchors[2].price,"first_fill_at":snapshot["at"],
                    'first_fill_at_exchange':snapshot['server_time'],
                    "position_btc":bot["position_btc"],"entry_price":price,
                    "setup_id":plan["setup_id"]}
    data["terminal_intents"][plan["intent_id"]]["status"]='FILLED'
    data["failed_closes"]=0;data["failed_exit_latched"]=False;data["last_failure_bar"]=None
    _save(bot,data,d)


def on_close_fill(bot,recipe,snapshot):
    try:
        data,d=_load(bot,recipe)
        if not data["cycle"]:
            raise G1Error("g1_close_without_owned_cycle")
    except (G1Error,KeyError,TypeError,ValueError):
        # PAPER accounting must complete the conservative close even if its
        # strategy checkpoint is unreadable. Preserve corrupt evidence, no reset.
        bot['_g1_error']='g1_close_checkpoint_unreadable'
        return
    data["cycle"]["position_btc"]=bot["position_btc"]
    if not bot["position_btc"]:
        d.mark_flat(math.ceil(snapshot['server_time']))
        data["cycle"]=None;data["reserved"]=None;data["ready_signal"]=None
        data["failed_closes"]=0;data["failed_exit_latched"]=False
    _save(bot,data,d)


def exit_reason(state,bot,recipe,snapshot):
    assert_paper(state)
    if bot.get("stop_requested"):
        return "owner_stop"
    try:
        data,_=_load(bot,recipe)
        cycle=data["cycle"]
        if not cycle or cycle["position_btc"]!=bot["position_btc"]:
            return "g1_owned_paper_state_mismatch"
        if snapshot["at"]>=cycle["first_fill_at"]+86400:
            return "g1_max_hold"
        sign=1 if cycle["side"]=='long'else-1
        px=snapshot["bid"]if sign>0 else snapshot["ask"]
        if sign*(px-cycle["stop"])<=0:
            return "g1_stop_loss"
        if sign*(px-cycle["target"])>=0:
            return "g1_take_profit"
        if data["failed_exit_latched"]:
            return "g1_failed_breakout"
        return None
    except (G1Error,KeyError,TypeError):
        return "g1_owned_paper_state_unreadable"
