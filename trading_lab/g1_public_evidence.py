"""Pure bounded public G1 trace projection. No imports of execution or I/O.

Unknown keys are never traversed/copied. Known fields have closed schemas and
types; arbitrary messages/reasons/IDs are never converted to strings for export.
This preserves recorded model geometry, not fills or hosted-stop proof.
"""
from decimal import Decimal
import math
import re

SCHEMA='ikigai-g1-public-evidence/v1'
ANCHOR_IDS=('P0','A','B','C','D','E')
SETUP_REASONS=frozenset('''NEED_SIX_PIVOTS ANCHORS_PREDATE_LAST_FLAT ALTERNATION
    NOT_ORDINARY_CONTRACTION LEG_TOO_SHORT PATTERN_DURATION LEG_TOO_SMALL FINAL_WIDTH
    APEX HTF_CONTEXT_UNKNOWN_OR_MISMATCH NONPOSITIVE_LEVEL USED_SETUP
    ALREADY_BROKEN_BEFORE_SETUP APEX_PASSED_BEFORE_CONFIRMATION'''.split())
ENTRY_REASONS=frozenset('UNKNOWN_POSITION POSITION_EXISTS ADMISSION_CLOSED COOLDOWN'.split())
PLAN_REASONS=frozenset('''FILL_RECHECK METADATA_CHANGED UNKNOWN_METADATA_OR_QUOTE
    TICK_METADATA_DRIFT STALE_OR_PREDECISION_QUOTE ADMISSION_OR_SETUP_EXPIRED
    SPREAD RETURNED_INSIDE_OR_CHASE STOP_DISTANCE NET_REWARD_RISK BELOW_MINIMUM_NO_UPSIZE'''.split())
EXIT_REASONS=frozenset('''g1_max_hold g1_stop_loss g1_take_profit g1_failed_breakout
    g1_owned_paper_state_mismatch g1_owned_paper_state_unreadable'''.split())
INPUT_REASONS=frozenset('''g1_numeric_input_unknown g1_instrument_mismatch
    g1_normalized_lot_mismatch g1_instrument_metadata_unknown g1_anchors_corrupt
    g1_checkpoint_capacity g1_missing_checkpoint_with_position_or_intent
    g1_snapshot_required_for_initialization g1_checkpoint_epoch_or_checksum
    g1_consumed_ids_capacity g1_h1_record_shape g1_h1_not_completed_or_invalid
    g1_instrument_epoch_changed g1_htf_provider_unknown g1_htf_order_unknown
    g1_bootstrap_required g1_bootstrap_gap g1_h1_revision g1_completed_bar_unknown
    g1_setup_capacity g1_close_checkpoint_unreadable'''.split())
HTF_REASONS=frozenset('''NOT_INITIALIZED CLOCK_UNCERTAINTY STALE_CLOCK CLOCK_SKEW
    LOCAL_CLOCK_DISCONTINUITY CLOCK_REGRESSION REQUEST_BUDGET FETCH_FAILED
    RESPONSE_SHAPE RESPONSE_BOUND RECEIPT_REGRESSION FETCH_DEADLINE
    INVALID_TIMESTAMP INVALID_NUMBER INVALID_CANDLE CANDLE_ALIGNMENT DUPLICATE_BAR
    PAGINATION INVALID_OHLC FUTURE_CONFIRMED_BAR GAP INSUFFICIENT_HISTORY REVISION
    CONTEXT_NOT_DURABLE CONTEXT_SCHEMA CONTEXT_BOUND CONTEXT_CAUSALITY NONCANONICAL_NUMBER
    NOT_YET_AVAILABLE WAITING_CLOSED_BAR STALE_CLOSED_BAR STALE_PROVIDER
    RECORDED_CONTEXT_INVALID G1_SECOND_BOUNDARY PACKED_CONTEXT_BOUND PACKED_CONTEXT_INVALID
    CONTEXT_HASH MISSING_CONTEXT VIEW_SCHEMA UNKNOWN_HAS_VALUES VIEW_CAUSALITY
    SNAPSHOT_ASOF RAW_HTF_SCHEMA HTF_COLLISION'''.split())
NO_PAYLOAD_KINDS=frozenset('UNKNOWN_DATA UNKNOWN_REVISION_OR_OUT_OF_ORDER GAP_RESET'.split())
KINDS=NO_PAYLOAD_KINDS|frozenset('''AMBIGUOUS_PIVOT INVALIDATED EXPIRED NO_ENTRY ENTRY_SIGNAL
    PIVOT_CHAIN_RESET PIVOT_CONFIRMED SETUP SETUP_REJECTED FLAT_RECONCILED'''.split())
EVENT_CODES=frozenset('g1_'+kind.lower()for kind in KINDS)|{'g1_warmup','g1_input_unknown'}


class InvalidEvidence(ValueError):
    pass


def _integer(value):
    if type(value)is not int or not 0<=value<10**12:
        raise InvalidEvidence()
    return value


def _aligned(value,seconds=3600):
    value=_integer(value)
    if value%seconds:raise InvalidEvidence()
    return value


def _decimal(value):
    if type(value)not in (int,float)or not 0<value<10**12 or not math.isfinite(value):
        raise InvalidEvidence()
    result=format(Decimal(str(value)),'f')
    if '.'in result:result=result.rstrip('0').rstrip('.')
    if len(result)>64:raise InvalidEvidence()
    return result


def _identifier(value):
    if type(value)is not str or len(value)!=24 or not re.fullmatch('[0-9a-f]{24}',value):raise InvalidEvidence()
    return value


def _reason(value,allow):
    return value if type(value)is str and len(value)<=100 and value in allow else 'UNRECOGNIZED'


def _pivot(raw):
    if type(raw)is not dict or type(raw.get('kind'))is not str or raw['kind']not in ('H','L'):raise InvalidEvidence()
    out={'kind':raw['kind'],'open_ts':_aligned(raw.get('open_ts')),
         'price':_decimal(raw.get('price')),'confirmed_at':_integer(raw.get('confirmed_at'))}
    if out['confirmed_at']<out['open_ts']+10800:raise InvalidEvidence()
    return out


def _setup(raw):
    if type(raw)is not dict or type(raw.get('side'))is not str or raw['side']not in ('long','short'):raise InvalidEvidence()
    anchors=raw.get('anchors');context=raw.get('context')
    if type(anchors)not in (list,tuple)or len(anchors)!=6 or type(context)not in (list,tuple)or len(context)!=3:
        raise InvalidEvidence()
    anchors=[{'anchor_id':name,**_pivot(value)}for name,value in zip(ANCHOR_IDS,anchors)]
    if any(b['open_ts']<=a['open_ts']for a,b in zip(anchors,anchors[1:])):raise InvalidEvidence()
    out={'setup_id':_identifier(raw.get('setup_id')),'side':raw['side'],'anchors':anchors,
         'created_at':_integer(raw.get('created_at')),'created_bar_ts':_aligned(raw.get('created_bar_ts')),
         'expires_at':_integer(raw.get('expires_at')),'apex_open_ts':_decimal(raw.get('apex_open_ts')),
         **{key:_decimal(raw.get(key))for key in ('trigger','stop','target','height','tick_size')}}
    if (max(a['confirmed_at']for a in anchors)>out['created_at']
            or out['created_bar_ts']+3600>out['created_at']or out['expires_at']<=out['created_at']):
        raise InvalidEvidence()
    rows=[]
    for row in context:
        if type(row)not in (list,tuple)or len(row)!=4:raise InvalidEvidence()
        close_ts=_aligned(row[0],14400)
        high,low,close=(_decimal(v)for v in row[1:])
        if close_ts>anchors[0]['open_ts']or not Decimal(low)<=Decimal(close)<=Decimal(high):raise InvalidEvidence()
        rows.append({'close_ts':close_ts,'high':high,'low':low,'close':close})
    if any(b['close_ts']!=a['close_ts']+14400 for a,b in zip(rows,rows[1:])):raise InvalidEvidence()
    out['context']=rows
    out['context_source']='UTC_4_CONTIGUOUS_H1'
    out['model_levels']={'entry_trigger':out['trigger'],'stop_loss':out['stop'],
                         'take_profit':out['target'],'b_level':anchors[2]['price']}
    out['level_semantics']='FROZEN_MODEL_LEVELS_NOT_FILL_OR_HOSTED_SL_PROOF'
    return out


def _signal(raw):
    if type(raw)is not dict:raise InvalidEvidence()
    out={'setup':_setup(raw.get('setup')),'decided_at':_integer(raw.get('decided_at')),
         'signal_bar_ts':_aligned(raw.get('signal_bar_ts'))}
    if (out['decided_at']<out['setup']['created_at']or out['signal_bar_ts']<=out['setup']['created_bar_ts']
            or out['signal_bar_ts']+3600>out['decided_at']):raise InvalidEvidence()
    return out


def _trace(raw):
    if type(raw)is not dict or type(raw.get('kind'))is not str or raw['kind']not in KINDS:raise InvalidEvidence()
    kind=raw['kind'];out={'kind':kind,'at':_integer(raw.get('at'))}
    if kind=='AMBIGUOUS_PIVOT':out['open_ts']=_aligned(raw.get('open_ts'))
    elif kind in ('INVALIDATED','EXPIRED'):out['setup_id']=_identifier(raw.get('setup_id'))
    elif kind in ('PIVOT_CONFIRMED','PIVOT_CHAIN_RESET'):
        out['pivot']=_pivot(raw.get('pivot'))
        if out['pivot']['confirmed_at']>out['at']:raise InvalidEvidence()
    elif kind=='SETUP':
        out['setup']=_setup(raw.get('setup'))
        if out['setup']['created_at']>out['at']:raise InvalidEvidence()
    elif kind=='SETUP_REJECTED':out['reason']=_reason(raw.get('reason'),SETUP_REASONS)
    elif kind in ('ENTRY_SIGNAL','NO_ENTRY'):
        out['signal']=_signal(raw.get('signal'))
        if out['signal']['decided_at']>out['at']:raise InvalidEvidence()
        if kind=='ENTRY_SIGNAL':
            if raw.get('reason')is not None:raise InvalidEvidence()
            out['reason']=None
        else:out['reason']=_reason(raw.get('reason'),ENTRY_REASONS)
    elif kind=='FLAT_RECONCILED':
        out['cooldown_until']=_integer(raw.get('cooldown_until'))
        if out['cooldown_until']<out['at']:raise InvalidEvidence()
    return out


def g1_public_evidence(data):
    """Return a new typed DTO or None; caller gates it to G1 event codes.

    Event seq/step/bot/code remain in battle_review.event_view, never inferred.
    Missing levels, plan sizing or intent IDs cannot be reconstructed here.
    """
    if type(data)is not dict:return None
    out={}
    if 'trace'in data:
        try:out['trace']=_trace(data['trace'])
        except InvalidEvidence:out['trace_status']='INVALID_OR_UNRECOGNIZED'
    if 'exchange_known_at'in data:
        try:out['exchange_known_at']=_integer(data['exchange_known_at'])
        except InvalidEvidence:out['clock_status']='INVALID_OR_UNRECOGNIZED'
    if 'bars'in data and type(data['bars'])is int and 0<=data['bars']<=256:out['bars']=data['bars']
    if 'reason'in data:
        reason=data['reason']
        allowed=INPUT_REASONS|HTF_REASONS|PLAN_REASONS|frozenset('g1_htf_unknown:'+r for r in HTF_REASONS)
        out['reason']=_reason(reason,allowed)
    if type(data.get('exit_reason'))is str and data['exit_reason']in EXIT_REASONS:out['exit_reason']=data['exit_reason']
    if not out:return None
    if 'trace'in out and 'exchange_known_at'in out and out['trace']['at']>out['exchange_known_at']:
        out.pop('trace');out['trace_status']='INVALID_OR_UNRECOGNIZED'
    return {'schema':SCHEMA,'time_domain':'EXCHANGE_UTC_SECONDS',**out}


def g1_event_evidence(data,event_code):
    """Closed event-code gate for battle_review; no prefix-based new schemas."""
    if type(data)is not dict or type(event_code)is not str:return None
    if not(event_code in EVENT_CODES or event_code=='exit_trigger'
           and type(data.get('exit_reason'))is str and data['exit_reason']in EXIT_REASONS
           or event_code=='entry_cancelled'and type(data.get('reason'))is str and data['reason']in PLAN_REASONS):
        return None
    out=g1_public_evidence(data)
    if out is not None and 'trace'in out and event_code!='g1_'+out['trace']['kind'].lower():
        out.pop('trace');out['trace_status']='EVENT_KIND_MISMATCH'
    return out
