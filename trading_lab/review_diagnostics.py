"""Read-only diagnostic projections; no trading, transport or inferred market path."""
from bisect import bisect_left, bisect_right
from datetime import datetime
from decimal import Decimal, InvalidOperation
import math

GAPS = {'market_unavailable', 'data_gap', 'clock_gap', 'clock_skew'}
REASONS = {'rules_exit','formal_exit','max_hold','stop_loss','take_profit','owner_stop',
           'stop_requested','recovery','risk_limit','deadline','cost_checkpoint','formal_reduce',
           'g1_max_hold','g1_stop_loss','g1_take_profit','g1_failed_breakout',
           'g1_owned_paper_state_mismatch','g1_owned_paper_state_unreadable'}
LEGACY = 'LEGACY_MIXED_EVENT_TOTAL_INCLUDES_DECISION_MIRRORS'


def stamp(value):
    try:
        if isinstance(value,str):
            parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
            if parsed.tzinfo is None: return None
            result=parsed.timestamp()
        else: result=float(value)
        return result if not isinstance(value,bool) and math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def bar(value):
    try:
        x=Decimal(str(value))
        return int(x) if not isinstance(value,bool) and x.is_finite() and 0<=x<=10**15 and x==x.to_integral_value() else None
    except (InvalidOperation,ValueError,TypeError):
        return None


def context_rows(context, events):
    rows=[]
    for q in context or []:
        at=stamp(q.get('at')); pin=q.get('snapshot_sha256',q.get('sha256'))
        if at is not None:
            rows.append({'at':at,'snapshot_sha256':pin if isinstance(pin,str) and len(pin)==64 and all(c in '0123456789abcdef' for c in pin) else None,
                         'bar_close_ms':bar(q.get('bar_close_ms')), 'source':'recorded_snapshot'})
    # Old bundles may have event references but no captured quote context.
    if not rows:
        seen=set()
        for e in events:
            d=e.get('data') or {}; pin=d.get('snapshot_sha256'); at=stamp(e.get('at'))
            if isinstance(pin,str) and len(pin)==64 and pin not in seen and at is not None:
                seen.add(pin); rows.append({'at':at,'snapshot_sha256':pin,'bar_close_ms':bar(d.get('bar_time_ms')),
                                           'event_seq':e['seq'],'source':'recorded_event_snapshot_reference'})
    return sorted(rows,key=lambda q:q['at'])


def phase_diagnostics(events, context=None):
    by_hash={q['snapshot_sha256']:q.get('bar_close_ms') for q in context or [] if q.get('snapshot_sha256')}
    groups={k:[] for k in ('entry','add','hold','exit')}; rechecks={'entry':0,'add':0,'hold':0,'exit':0}
    def exit_ref(e):
        d=e.get('data') or {}
        return d.get('snapshot_sha256') or (('at',e['at']) if e.get('at') else ('seq',e['seq']))
    # A trigger can precede its recorded checks. Prefer the check event in either order.
    canonical_exit_refs={exit_ref(e) for e in events if
        e.get('code')=='position_checks' and isinstance((e.get('data') or {}).get('exit_checks'),list) or
        e.get('code')=='exit_checks'}
    exit_refs=set(); hold_decisions=0; unclassified=0; unclassified_seqs=[]
    for e in events:
        d=e.get('data') or {}; code=e.get('code'); phase=None
        b=bar(d.get('bar_time_ms'))
        if b is None: b=by_hash.get(d.get('snapshot_sha256'))
        if code in {'entry_rechecked','entry_recheck'}:
            rechecks['add' if d.get('kind')=='add' else 'entry']+=1
            continue
        if code=='entry_checks': phase='entry'
        elif code=='position_checks':
            phase=d.get('kind') if d.get('kind') in {'add','hold'} else None
            if phase is None: unclassified+=1
        elif code=='hold_checks': phase='hold'
        elif code=='exit_checks': phase='exit'
        if phase: groups[phase].append((e,d.get('checks') or [],b))
        elif isinstance(d.get('checks'),list) and code!='rules_decision': unclassified_seqs.append(e['seq'])
        ref=exit_ref(e)
        if code=='position_checks' and isinstance(d.get('exit_checks'),list):
            groups['exit'].append((e,d['exit_checks'],b)); exit_refs.add(ref)
        elif code in {'exit_trigger','position_exit_trigger'} and isinstance(d.get('exit_checks'),list):
            if ref not in canonical_exit_refs and ref not in exit_refs:
                groups['exit'].append((e,d['exit_checks'],b)); exit_refs.add(ref)
        # A hold decision has no implied failed "hold predicate".
        if code=='rules_decision' and d.get('choice')=='hold': hold_decisions+=1
    result={}
    for phase, records in groups.items():
        known={b for _,_,b in records if b is not None}; missing=sum(b is None for _,_,b in records); leaves={}
        for e, checks, b in records:
            for c in checks:
                if c.get('passed') is True: continue
                key=(c.get('feature'),c.get('op'),str(c.get('expected')))
                row=leaves.setdefault(key,{'feature':key[0],'op':key[1],'expected':c.get('expected'),
                     'false_evaluations':0,'unknown_evaluations':0,'_false_bars':set(),'unknown_bar_evaluations':0})
                row['false_evaluations' if c.get('passed') is False else 'unknown_evaluations']+=1
                if c.get('passed') is False:
                    if b is None: row['unknown_bar_evaluations']+=1
                    else: row['_false_bars'].add(b)
        for row in leaves.values():
            row['false_known_unique_closed_bars']=len(row.pop('_false_bars'))
            row['false_unique_closed_bars']=None if row['unknown_bar_evaluations'] else row['false_known_unique_closed_bars']
        result[phase]={'evaluations':len(records),'rechecks':rechecks[phase],
          'unique_closed_bars':len(known) if records and not missing else None,
          'known_unique_closed_bars':len(known),'unknown_bar_evaluations':missing,
          'predicate_evidence':'RECORDED' if records else 'NOT_RECORDED',
          'failed_predicates':list(leaves.values()),
          'interpretation':'FALSE means exit predicate did not trigger; not a blocked close.' if phase=='exit' else
                           'Recorded leaf FALSE/UNKNOWN; nested leaf failure is not necessarily a root veto.'}
    return {'version':1,'phases':result,'recorded_hold_decisions':hold_decisions,
            'unclassified_position_checks':unclassified,
            'unclassified_check_event_seqs':unclassified_seqs,
            'method':'Canonical check events only; rules_decision mirrors ignored; rechecks counted separately. No bar inferred from event clock.'}


def cycle_timing(events, trip, previous_close=0):
    start=trip['entry_seq']; end=trip.get('close_seq') or (events[-1]['seq'] if events else start)
    by_seq={e['seq']:e for e in events}; entry=by_seq.get(start); close=by_seq.get(trip.get('close_seq'))
    entry_intent=[e for e in events if previous_close<e['seq']<start and
                  (e['code']=='entry_intent' or e['code']=='exchange_order_intent' and e.get('step')==3)]
    closing=[e for e in events if start<=e['seq']<=end and
             (e['code']=='close_intent' or e['code']=='exchange_order_intent' and e.get('step')==6)]
    exits=[e for e in events if start<=e['seq']<=end and e['code'] in {'exit_trigger','position_exit_trigger','stop_requested'}]
    def ref(e): return {k:e.get(k) for k in ('seq','at','code')} if e else None
    def latency(a,b):
        x,y=stamp(a.get('at')) if a else None,stamp(b.get('at')) if b else None
        return y-x if x is not None and y is not None and y>=x else None
    a=entry_intent[-1] if entry_intent else None; b=closing[-1] if closing else None; trigger=exits[-1] if exits else None
    reason=(close or {}).get('data',{}).get('reason')
    if reason not in REASONS: reason=(trigger or {}).get('data',{}).get('exit_reason')
    return {'entry_intent':ref(a),'close_intent':ref(b),'exit_trigger':ref(trigger),
      'entry_intent_to_fill_seconds':latency(a,entry),'close_intent_to_fill_seconds':latency(b,close),
      'exit_trigger_to_fill_seconds':latency(trigger,close),'exit_reason':reason if reason in REASONS else None}


def market_gaps(bundle, context):
    events=bundle.get('events',[]); times=[q['at'] for q in context]; result=[]
    updates={}; current={}
    for bot in bundle.get('participants',[]):
        for row in bot.get('trade_ledger',{}).get('rows',[]):
            if row.get('position_after') is not None:
                unit='contracts' if row.get('source')=='exchange_incremental_contracts' else 'BTC'
                updates.setdefault(row['seq'],[]).append((bot['id'],row['position_after'],row.get('at'),'ledger_position_after',unit))
    for e in events:
        for identity,quantity,at,source,unit in updates.get(e['seq'],[]):
            current[identity]=(e['seq'],quantity,at,source,unit)
        if e.get('code')=='financial_observation':
            quantity=(e.get('data') or {}).get('financial',{}).get('position_btc')
            if quantity is not None: current[e.get('bot_id')]=(e['seq'],quantity,e.get('at'),'financial_position_btc','BTC')
        if e.get('code') not in GAPS: continue
        at=stamp(e.get('at')); i=bisect_left(times,at) if at is not None else 0; j=bisect_right(times,at) if at is not None else len(times)
        before=context[i-1] if i else None; after=context[j] if j<len(context) else None
        positions=[]
        for bot in bundle.get('participants',[]):
            last=current.get(bot['id'])
            try: qty=Decimal(str(last[1])) if last else None
            except InvalidOperation: qty=None
            valid=qty is not None and qty.is_finite() and abs(qty)<=Decimal('1e12') and abs(qty.as_tuple().exponent)<=64
            positions.append({'id':bot['id'],'status':('FLAT' if qty==0 else 'OPEN') if valid else 'UNKNOWN',
              'quantity':str(qty) if valid else None,'event_seq':last[0] if last else None,'at':last[2] if last else None,
              'source':last[3] if last else 'NOT_RECORDED','quantity_unit':last[4] if last else None})
        result.append({'event_seq':e['seq'],'event_at':e.get('at'),'kind':e['code'],'reason_detail':'UNKNOWN',
          'before':before,'recovery':after,'positions_at_gap':positions,'inside_status':'UNKNOWN',
          'outage_duration_seconds':None,'known_snapshot_interval_seconds':after['at']-before['at'] if before and after else None,
          'limitation':'Known snapshot endpoints only; outage start/duration and inside path are not observed. COMPLETE history is separate from market continuity.'})
    return result


def describe(bundle, snapshot_context=None):
    events=bundle.get('events',[]); participants={}
    mixed=isinstance(snapshot_context,dict) or bool(bundle.get('match',{}).get('instruments'))
    # R2.1.12 (MIX, plan check p. 14): each participant on its own contract's quotes; a market gap of one contract
    # (its event names it) is that contract's participants' gap, a season-wide one (no instrument) everyone's
    # R2.1.13 (X2-01): the fallback references of a contract without quotes are its own bots' and its own market events' only
    owners={b['id']:b.get('instrument') for b in bundle.get('participants',[])}
    own_events=lambda c:[e for e in events if owners.get(e.get('bot_id'))==c or (e.get('data') or {}).get('instrument')==c]
    contexts={c:context_rows((snapshot_context or {}).get(c) if isinstance(snapshot_context,dict) else None,own_events(c))
              for c in bundle.get('match',{}).get('instruments',[])} if mixed else None
    context=None if mixed else context_rows(snapshot_context,events)
    for bot in bundle.get('participants',[]):
        own=[e for e in events if e.get('bot_id')==bot['id']]; previous=0; trades=[]
        for trip in bot.get('trade_ledger',{}).get('round_trips',[]):
            trades.append({'number':trip['number'],**cycle_timing(own,trip,previous)})
            previous=trip.get('close_seq') or previous
        participants[bot['id']]={'phase_diagnostics':phase_diagnostics(own,contexts.get(bot.get('instrument'),[]) if mixed else context),'trade_explanations':trades}
    if not mixed:
        return {'participants':participants,'market_gaps':market_gaps(bundle,context)}
    gaps=[]
    for contract,rows in contexts.items():
        part={**bundle,'participants':[b for b in bundle.get('participants',[]) if b.get('instrument')==contract],
              'events':[e for e in events if e.get('code') not in GAPS or (e.get('data') or {}).get('instrument') in {None,contract}]}
        gaps.extend({**gap,'instrument':contract} for gap in market_gaps(part,rows))
    return {'participants':participants,'market_gaps':sorted(gaps,key=lambda g:(g['event_seq'],g['instrument']))}


def decorate(bundle, snapshot_context=None, snapshot_context_status=None):
    projection=describe(bundle,snapshot_context)
    for bot in bundle.get('participants',[]):
        bot.update(projection['participants'][bot['id']])
        bot['funnel']['failed_features_scope']=LEGACY
    bundle['market_gaps']=projection['market_gaps']
    status=snapshot_context_status if snapshot_context_status in {'UNAVAILABLE','NOT_RECORDED','RECORDED'} else 'RECORDED' if snapshot_context else 'NOT_RECORDED'
    if status=='RECORDED' and isinstance(snapshot_context,dict) and not all(snapshot_context.get(c) for c in bundle.get('match',{}).get('instruments',[])):
        status='NOT_RECORDED'  # R2.1.13 (X2-01): RECORDED only when every contract of a mixed season has quotes
    bundle['diagnostics_context']={'market_snapshots':status}
    return bundle
