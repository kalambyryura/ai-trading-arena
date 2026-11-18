"""Bounded public HTF candidate. Pure validation + injected I/O; no engine imports.

All timestamps ending _ms are integer milliseconds. Exchange candle times and
local receipt times are distinct domains. Clock request brackets map availability
conservatively into exchange time; historical warm-up is initial knowledge only.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext
import hashlib
import json
import math
import re
import threading
import zlib

HOUR = 3_600_000
INTERVALS = {'1H': HOUR, '15m': 900_000}
INSTRUMENT = 'BTC-USDT-SWAP'
SCHEMA = 'causal-htf-context/v1'
VIEW_SCHEMA = 'causal-htf-view/v1'
MAX_CONTEXT_BYTES = 262_144
MAX_PACKED_BYTES = 65_536
FEATURES = ('htf_1h_sma20_gap_bps', 'htf_15m_sma20_gap_bps')
NUMBER = re.compile(r'[0-9]{1,18}(?:\.[0-9]{1,16})?\Z')
BAR_KEYS = {'open_ms', 'close_ms', 'open', 'high', 'low', 'close', 'volume',
            'vol_ccy', 'vol_quote', 'available_at_ms', 'confirmed_at_ms',
            'observed_server_ms', 'available_server_ms_lower', 'available_server_ms_upper', 'knowledge'}
DOC_KEYS = {'schema', 'instrument', 'h1_bars', 'include_m15', 'initial_knowledge_at_ms',
            'initial_server_cutoff_ms', 'created_at_ms', 'series'}
VIEW_KEYS = {'schema', 'instrument', 'status', 'reason', 'context_sha256',
             'asof_local_ms', 'asof_server_ms', 'asof_monotonic_ms',
             'last_refresh_monotonic_ms', 'max_cache_age_ms', 'bar_lag_grace_ms',
             'latest_h1_close_ms', 'h1_signal_eligible', 'features'}
REASONS = frozenset('''NOT_INITIALIZED CLOCK_UNCERTAINTY STALE_CLOCK CLOCK_SKEW
    LOCAL_CLOCK_DISCONTINUITY CLOCK_REGRESSION CLOCK_DISCONTINUITY REQUEST_BUDGET FETCH_FAILED
    RESPONSE_SHAPE RESPONSE_BOUND RECEIPT_REGRESSION FETCH_DEADLINE
    INVALID_TIMESTAMP INVALID_NUMBER INVALID_CANDLE CANDLE_ALIGNMENT DUPLICATE_BAR
    PAGINATION INVALID_OHLC FUTURE_CONFIRMED_BAR GAP INSUFFICIENT_HISTORY REVISION
    CONTEXT_NOT_DURABLE CONTEXT_SCHEMA CONTEXT_BOUND CONTEXT_WIRE_BOUND CONTEXT_CAUSALITY NONCANONICAL_NUMBER
    NOT_YET_AVAILABLE WAITING_CLOSED_BAR STALE_CLOSED_BAR STALE_PROVIDER
    RECORDED_CONTEXT_INVALID G1_SECOND_BOUNDARY'''.split())


class HTFError(ValueError):
    """Reason codes only; no transport exception bodies/headers in evidence."""


def _fail(code):
    raise HTFError(code)


def _integer(value):
    if type(value) is not int or not 0 <= value < 10**16:
        _fail('INVALID_TIMESTAMP')
    return value


def _number(value, positive=False):
    if not isinstance(value, str) or not NUMBER.fullmatch(value):
        _fail('INVALID_NUMBER')
    result = Decimal(value)
    if positive and result <= 0:
        _fail('INVALID_NUMBER')
    exact = format(result, 'f')
    return exact.rstrip('0').rstrip('.') if '.' in exact else exact


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('ascii')


def digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


@dataclass(frozen=True)
class ClockSample:
    server_ms: int
    received_at_ms: int
    monotonic_ms: int
    request_started_mono_ms: int | None = None


@dataclass(frozen=True)
class Page:
    rows: list
    received_at_ms: int
    monotonic_ms: int


@dataclass(frozen=True)
class Request:
    path: str
    bar: str
    limit: int
    after_ms: int | None = None

    def params(self):
        if self.path not in {'/api/v5/market/history-candles', '/api/v5/market/candles'}:
            _fail('ENDPOINT_NOT_ALLOWED')
        if self.bar not in INTERVALS or type(self.limit) is not int or not 1 <= self.limit <= 100:
            _fail('REQUEST_BOUND')
        result = {'instId': INSTRUMENT, 'bar': self.bar, 'limit': str(self.limit)}
        if self.after_ms is not None:
            result['after'] = str(_integer(self.after_ms))
        return result


@dataclass(frozen=True)
class Config:
    h1_bars: int = 256
    include_m15: bool = False
    refresh_ms: int = 60_000
    max_cache_age_ms: int = 120_000
    bar_lag_grace_ms: int = 120_000
    max_clock_age_ms: int = 15_000
    max_clock_skew_ms: int = 5_000
    max_fetch_ms: int = 15_000
    max_requests: int = 4096

    def __post_init__(self):
        if type(self.h1_bars) is not int or not 20 <= self.h1_bars <= 256 or type(self.include_m15) is not bool:
            _fail('CONFIG_BOUND')
        bounds = {'refresh_ms': (60_000, 120_000), 'max_cache_age_ms': (120_000, 180_000),
                  'bar_lag_grace_ms': (0, 120_000), 'max_clock_age_ms': (1000, 15_000),
                  'max_clock_skew_ms': (0, 5000), 'max_fetch_ms': (1000, 15_000),
                  'max_requests': (1, 100_000)}
        if any(type(getattr(self, k)) is not int or not a <= getattr(self, k) <= b for k, (a, b) in bounds.items()):
            _fail('CONFIG_BOUND')


def _clock_check(clock, now, mono, config):
    if not isinstance(clock, ClockSample) or clock.request_started_mono_ms is None:
        _fail('CLOCK_UNCERTAINTY')
    for value in (*asdict(clock).values(), now, mono):
        _integer(value)
    rtt = clock.monotonic_ms - clock.request_started_mono_ms
    age = mono - clock.monotonic_ms
    if not 0 <= rtt <= config.max_clock_age_ms or not 0 <= age <= config.max_clock_age_ms:
        _fail('STALE_CLOCK')
    if abs(clock.received_at_ms - clock.server_ms) > config.max_clock_skew_ms + rtt:
        _fail('CLOCK_SKEW')
    if now < clock.received_at_ms or abs(now - clock.received_at_ms - age) > 1000:
        _fail('LOCAL_CLOCK_DISCONTINUITY')


def _clock_progress(previous, current):
    if previous is None:
        return
    server_delta = current.server_ms - previous.server_ms
    mono_delta = current.monotonic_ms - previous.monotonic_ms
    if server_delta < 0 or mono_delta < 0:
        _fail('CLOCK_REGRESSION')
    brackets = (previous.monotonic_ms - previous.request_started_mono_ms +
                current.monotonic_ms - current.request_started_mono_ms)
    if abs(server_delta - mono_delta) > brackets + 1000:
        _fail('CLOCK_DISCONTINUITY')


def _parse_rows(page, request, clock):
    if not isinstance(page.rows, list) or not 1 <= len(page.rows) <= request.limit:
        _fail('RESPONSE_BOUND')
    interval, result, seen, unconfirmed = INTERVALS[request.bar], [], set(), set()
    for row in page.rows:
        if not isinstance(row, list) or len(row) != 9 or type(row[8]) is not str or row[8] not in {'0', '1'}:
            _fail('INVALID_CANDLE')
        if type(row[0]) is not str or not re.fullmatch(r'[0-9]{1,16}', row[0]):
            _fail('INVALID_TIMESTAMP')
        start = _integer(int(row[0]))
        if start % interval or start > clock.server_ms:
            _fail('CANDLE_ALIGNMENT')
        if start in seen:
            _fail('DUPLICATE_BAR')
        seen.add(start)
        if request.after_ms is not None and start >= request.after_ms:
            _fail('PAGINATION')
        values = [_number(value, index < 4) for index, value in enumerate(row[1:8])]
        opening, high, low, close = map(Decimal, values[:4])
        if not low <= min(opening, close) <= max(opening, close) <= high:
            _fail('INVALID_OHLC')
        if row[8] == '0':
            unconfirmed.add(start)
            continue
        if start + interval > clock.server_ms:
            _fail('FUTURE_CONFIRMED_BAR')
        result.append({'open_ms': start, 'close_ms': start + interval,
                       **dict(zip(('open', 'high', 'low', 'close', 'volume', 'vol_ccy', 'vol_quote'), values))})
    return sorted(result, key=lambda bar: bar['open_ms']), min(seen), unconfirmed


def _consecutive(bars, interval):
    if not bars or any(b['open_ms'] != a['open_ms'] + interval for a, b in zip(bars, bars[1:])):
        _fail('GAP')


def validate_document(document):
    """Strict public DTO. Unknown keys rejected, including within compressed input."""
    if not isinstance(document, dict) or set(document) != DOC_KEYS:
        _fail('CONTEXT_SCHEMA')
    if document['schema'] != SCHEMA or document['instrument'] != INSTRUMENT:
        _fail('CONTEXT_SCHEMA')
    Config(h1_bars=document['h1_bars'], include_m15=document['include_m15'])
    for key in ('initial_knowledge_at_ms', 'initial_server_cutoff_ms', 'created_at_ms'):
        _integer(document[key])
    if document['created_at_ms'] < document['initial_knowledge_at_ms']:
        _fail('CONTEXT_CAUSALITY')
    expected = {'1H', '15m'} if document['include_m15'] else {'1H'}
    if not isinstance(document['series'], dict) or set(document['series']) != expected:
        _fail('CONTEXT_SCHEMA')
    for frame, bars in document['series'].items():
        wanted = document['h1_bars'] if frame == '1H' else 20
        interval = INTERVALS[frame]
        if not isinstance(bars, list) or len(bars) != wanted:
            _fail('CONTEXT_BOUND')
        for bar in bars:
            if not isinstance(bar, dict) or set(bar) != BAR_KEYS:
                _fail('CONTEXT_SCHEMA')
            for key in BAR_KEYS - {'open', 'high', 'low', 'close', 'volume', 'vol_ccy', 'vol_quote', 'knowledge'}:
                _integer(bar[key])
            if bar['open_ms'] % interval or bar['close_ms'] != bar['open_ms'] + interval:
                _fail('CANDLE_ALIGNMENT')
            for key in ('open', 'high', 'low', 'close', 'volume', 'vol_ccy', 'vol_quote'):
                if _number(bar[key], key in {'open', 'high', 'low', 'close'}) != bar[key]:
                    _fail('NONCANONICAL_NUMBER')
            if not Decimal(bar['low']) <= min(Decimal(bar['open']), Decimal(bar['close'])) <= max(Decimal(bar['open']), Decimal(bar['close'])) <= Decimal(bar['high']):
                _fail('INVALID_OHLC')
            if (bar['confirmed_at_ms'] != bar['available_at_ms'] or
                    not document['initial_knowledge_at_ms'] <= bar['available_at_ms'] <= document['created_at_ms'] or
                    not bar['close_ms'] <= bar['observed_server_ms'] <= bar['available_server_ms_lower'] <= bar['available_server_ms_upper']):
                _fail('CONTEXT_CAUSALITY')
            if (bar['available_server_ms_upper'] - bar['available_server_ms_lower'] > 15_000 or
                    bar['available_server_ms_lower'] - bar['observed_server_ms'] > 15_000 or
                    abs(bar['available_at_ms'] - bar['available_server_ms_lower']) > 20_000):
                _fail('CONTEXT_CAUSALITY')
            initial = bar['close_ms'] <= document['initial_server_cutoff_ms']
            if bar['knowledge'] != ('initial_history' if initial else 'forward_confirmed'):
                _fail('CONTEXT_CAUSALITY')
            if initial and bar['available_at_ms'] != document['initial_knowledge_at_ms']:
                _fail('CONTEXT_CAUSALITY')
        _consecutive(bars, interval)
    if len(_encoded(document)) > MAX_CONTEXT_BYTES:
        _fail('CONTEXT_BOUND')
    return document


def aggregate_h4(h1):
    """UTC-aligned4H1 only. Initial/final partial groups omitted, interior gaps fail."""
    _consecutive(h1, HOUR)
    groups = {}
    for bar in h1:
        groups.setdefault(bar['open_ms'] // (4 * HOUR) * (4 * HOUR), []).append(bar)
    result = []
    for start, bars in sorted(groups.items()):
        if [b['open_ms'] for b in bars] != [start + i * HOUR for i in range(4)]:
            continue
        result.append({'open_ms': start, 'close_ms': start + 4 * HOUR,
                       'open': bars[0]['open'], 'high': max((b['high'] for b in bars), key=Decimal),
                       'low': min((b['low'] for b in bars), key=Decimal), 'close': bars[-1]['close'],
                       'available_at_ms': max(b['available_at_ms'] for b in bars),
                       'available_server_ms_upper': max(b['available_server_ms_upper'] for b in bars),
                       'source': 'UTC_4_CONTIGUOUS_H1', 'knowledge': bars[-1]['knowledge']})
    return result


def _features(document):
    result = dict.fromkeys(FEATURES)
    for frame, key in [('1H', FEATURES[0]), ('15m', FEATURES[1])]:
        bars = document['series'].get(frame, [])
        if len(bars) >= 20:
            with localcontext() as context:
                context.prec = 50
                closes = [Decimal(bar['close']) for bar in bars[-20:]]
                result[key] = float((closes[-1] / (sum(closes) / 20) - 1) * 10000)
    return result


def _ready_reason(document, local, server, grace):
    if document['created_at_ms'] > local or any(b['available_at_ms'] > local or b['available_server_ms_upper'] > server
                                              for bars in document['series'].values() for b in bars):
        return 'NOT_YET_AVAILABLE'
    for frame, bars in document['series'].items():
        expected = server // INTERVALS[frame] * INTERVALS[frame]
        if bars[-1]['close_ms'] > expected:
            return 'FUTURE_CONFIRMED_BAR'
        if bars[-1]['close_ms'] < expected:
            return 'WAITING_CLOSED_BAR' if server - expected <= grace else 'STALE_CLOSED_BAR'
    return None


class HTFProvider:
    """One instance per shared worker. Locks independent of trading state locks.

    get_public(Request)->Page must account requests in the parent GET budget and
    enforce HTTP timeout/byte/no-redirect/host rules. persist_context(doc)->True
    acknowledges durable evidence. No retry inside a failed refresh; next scheduled
    refresh may recover transient transport/storage failure. Revision/gap is sticky.
    """
    def __init__(self, get_public, persist_context, *, config=Config()):
        if not callable(get_public) or not callable(persist_context):
            _fail('CALLBACK_REQUIRED')
        self.get_public, self.persist_context, self.config = get_public, persist_context, config
        self.calls = 0
        self._doc = None
        self._hash = None
        self._attempt_mono = None
        self._success_mono = None
        self._last_clock = None
        self._reason = 'NOT_INITIALIZED'
        self._fatal = False
        self._lock = threading.RLock()

    @property
    def retained_bars(self):
        return sum(map(len, self._doc['series'].values())) if self._doc else 0

    def due(self, monotonic_ms):
        """Schedule the extra clock/candle batch once per worker, not per bot."""
        _integer(monotonic_ms)
        with self._lock:
            return not self._fatal and (self._attempt_mono is None or monotonic_ms - self._attempt_mono >= self.config.refresh_ms)

    def _fetch(self, request, clock, previous_page):
        request.params()
        if self.calls >= self.config.max_requests:
            _fail('REQUEST_BUDGET')
        self.calls += 1  # failed calls consume allowance, never reset by bootstrap
        try:
            page = self.get_public(request)
        except Exception:
            raise HTFError('FETCH_FAILED') from None
        if not isinstance(page, Page):
            _fail('RESPONSE_SHAPE')
        _integer(page.received_at_ms); _integer(page.monotonic_ms)
        if (page.received_at_ms < clock.received_at_ms or page.monotonic_ms < clock.monotonic_ms or
                previous_page and (page.received_at_ms < previous_page.received_at_ms or page.monotonic_ms < previous_page.monotonic_ms)):
            _fail('RECEIPT_REGRESSION')
        if (page.monotonic_ms - clock.monotonic_ms > self.config.max_fetch_ms or
                abs(page.received_at_ms - clock.received_at_ms - (page.monotonic_ms - clock.monotonic_ms)) > 1000):
            _fail('FETCH_DEADLINE')
        return page

    def refresh(self, clock, now_ms, monotonic_ms):
        with self._lock:
            try:
                _clock_check(clock, now_ms, monotonic_ms, self.config)
                _clock_progress(self._last_clock, clock)
                if self._fatal:
                    return self._reason
                if self._attempt_mono is not None and monotonic_ms - self._attempt_mono < self.config.refresh_ms:
                    return self._reason
                self._last_clock = clock
                self._attempt_mono = monotonic_ms
                series, last_page = {}, None
                for frame in (('1H', '15m') if self.config.include_m15 else ('1H',)):
                    wanted = self.config.h1_bars if frame == '1H' else 20
                    previous = self._doc['series'][frame] if self._doc else []
                    fresh, cursor = {}, None
                    for _ in range(3 if not previous else 1):
                        request = Request('/api/v5/market/candles' if previous else '/api/v5/market/history-candles',
                                          frame, 32 if previous else 100, cursor)
                        last_page = self._fetch(request, clock, last_page)
                        parsed, oldest, unconfirmed = _parse_rows(last_page, request, clock)
                        if unconfirmed.intersection(bar['open_ms'] for bar in previous):
                            _fail('REVISION')
                        if cursor is not None and oldest >= cursor:
                            _fail('PAGINATION')
                        cursor = oldest
                        for bar in parsed:
                            if bar['open_ms'] in fresh:
                                _fail('DUPLICATE_BAR')
                            fresh[bar['open_ms']] = bar
                        if previous or len(fresh) >= wanted:
                            break
                    merged = {bar['open_ms']: deepcopy(bar) for bar in previous}
                    for stamp, bar in fresh.items():
                        if stamp in merged:
                            if any(merged[stamp][key] != value for key, value in bar.items()):
                                _fail('REVISION')
                        elif not previous or stamp > previous[-1]['open_ms']:
                            merged[stamp] = {**bar, 'available_at_ms': last_page.received_at_ms,
                                'confirmed_at_ms': last_page.received_at_ms, 'observed_server_ms': clock.server_ms,
                                'available_server_ms_lower': clock.server_ms + last_page.monotonic_ms - clock.monotonic_ms,
                                'available_server_ms_upper': clock.server_ms + last_page.monotonic_ms - clock.request_started_mono_ms,
                                'knowledge': 'forward_confirmed' if previous else 'initial_history'}
                    values = sorted(merged.values(), key=lambda bar: bar['open_ms'])
                    _consecutive(values, INTERVALS[frame])
                    if len(values) < wanted:
                        _fail('INSUFFICIENT_HISTORY')
                    series[frame] = values[-wanted:]
                initial = self._doc['initial_knowledge_at_ms'] if self._doc else last_page.received_at_ms
                if self._doc is None:
                    for bars in series.values():
                        for bar in bars:
                            bar.update(available_at_ms=initial, confirmed_at_ms=initial,
                                       available_server_ms_lower=clock.server_ms + last_page.monotonic_ms - clock.monotonic_ms,
                                       available_server_ms_upper=clock.server_ms + last_page.monotonic_ms - clock.request_started_mono_ms)
                changed = self._doc is None or series != self._doc['series']
                if changed:
                    document = {'schema': SCHEMA, 'instrument': INSTRUMENT, 'h1_bars': self.config.h1_bars,
                                'include_m15': self.config.include_m15, 'initial_knowledge_at_ms': initial,
                                'initial_server_cutoff_ms': self._doc['initial_server_cutoff_ms'] if self._doc else clock.server_ms,
                                'created_at_ms': last_page.received_at_ms, 'series': series}
                    validate_document(document)
                    try:
                        pack_context(document)
                    except HTFError:
                        raise HTFError('CONTEXT_WIRE_BOUND') from None
                    try:
                        acknowledged = self.persist_context(deepcopy(document)) is True
                    except Exception:
                        acknowledged = False
                    if not acknowledged:
                        _fail('CONTEXT_NOT_DURABLE')
                    self._doc, self._hash = document, digest(document)
                self._success_mono = last_page.monotonic_ms
                self._reason = None
            except HTFError as error:
                if type(monotonic_ms) is int and monotonic_ms >= 0 and (self._attempt_mono is None or monotonic_ms >= self._attempt_mono):
                    self._attempt_mono = monotonic_ms
                self._reason = str(error)
                self._fatal = self._fatal or self._reason not in {
                    'FETCH_FAILED', 'CONTEXT_NOT_DURABLE', 'INSUFFICIENT_HISTORY', 'STALE_CLOCK',
                    'FETCH_DEADLINE', 'CLOCK_UNCERTAINTY'}
            return self._reason

    def view(self, clock, now_ms, monotonic_ms):
        with self._lock:
            reason = self._reason
            try:
                _clock_check(clock, now_ms, monotonic_ms, self.config)
                _clock_progress(self._last_clock, clock)
                if not reason and (self._success_mono is None or not 0 <= monotonic_ms - self._success_mono <= self.config.max_cache_age_ms):
                    _fail('STALE_PROVIDER')
                if not reason:
                    reason = _ready_reason(self._doc, now_ms, clock.server_ms, self.config.bar_lag_grace_ms)
            except HTFError as error:
                reason = str(error)
            ready = reason is None
            latest = self._doc['series']['1H'][-1]['close_ms'] if self._doc else None
            return {'schema': VIEW_SCHEMA, 'instrument': INSTRUMENT, 'status': 'READY' if ready else 'UNKNOWN',
                    'reason': reason, 'context_sha256': self._hash, 'asof_local_ms': now_ms,
                    'asof_server_ms': clock.server_ms, 'asof_monotonic_ms': monotonic_ms,
                    'last_refresh_monotonic_ms': self._success_mono,
                    'max_cache_age_ms': self.config.max_cache_age_ms, 'bar_lag_grace_ms': self.config.bar_lag_grace_ms,
                    'latest_h1_close_ms': latest,
                    'h1_signal_eligible': bool(ready and latest > self._doc['initial_server_cutoff_ms'] and
                                               0 <= clock.server_ms - latest <= self.config.bar_lag_grace_ms),
                    'features': _features(self._doc) if ready else dict.fromkeys(FEATURES)}

    def context(self, context_sha256):
        """One bounded current document; old versions belong in durable evidence."""
        with self._lock:
            return deepcopy(self._doc) if context_sha256 == self._hash and self._doc else None


def replay_view(view, resolve_context):
    """Validate recorded knowledge; no provider, network, strategy or fills."""
    if not isinstance(view, dict) or set(view) != VIEW_KEYS or view['schema'] != VIEW_SCHEMA or view['instrument'] != INSTRUMENT:
        _fail('VIEW_SCHEMA')
    for key in ('asof_local_ms', 'asof_server_ms', 'asof_monotonic_ms'):
        _integer(view[key])
    if view['status'] not in {'READY', 'UNKNOWN'} or type(view['h1_signal_eligible']) is not bool:
        _fail('VIEW_SCHEMA')
    if not isinstance(view['features'], dict) or set(view['features']) != set(FEATURES):
        _fail('VIEW_SCHEMA')
    if any(value is not None and (type(value) not in (int, float) or not math.isfinite(value)) for value in view['features'].values()):
        _fail('VIEW_SCHEMA')
    for key in ('last_refresh_monotonic_ms', 'latest_h1_close_ms'):
        if view[key] is not None:
            _integer(view[key])
    config = Config(max_cache_age_ms=view['max_cache_age_ms'], bar_lag_grace_ms=view['bar_lag_grace_ms'])
    if view['context_sha256'] is not None and not (isinstance(view['context_sha256'], str) and re.fullmatch('[a-f0-9]{64}', view['context_sha256'])):
        _fail('CONTEXT_HASH')
    if view['status'] == 'UNKNOWN':
        if (not isinstance(view['reason'], str) or view['reason'] not in REASONS or
                view['h1_signal_eligible'] or any(v is not None for v in view['features'].values())):
            _fail('UNKNOWN_HAS_VALUES')
        return deepcopy(view)
    document = resolve_context(view['context_sha256'])
    if document is None:
        _fail('MISSING_CONTEXT')
    validate_document(document)
    if digest(document) != view['context_sha256']:
        _fail('CONTEXT_HASH')
    last = _integer(view['last_refresh_monotonic_ms'])
    if not 0 <= view['asof_monotonic_ms'] - last <= config.max_cache_age_ms:
        _fail('STALE_PROVIDER')
    reason = _ready_reason(document, view['asof_local_ms'], view['asof_server_ms'], config.bar_lag_grace_ms)
    latest = document['series']['1H'][-1]['close_ms']
    eligible = latest > document['initial_server_cutoff_ms'] and 0 <= view['asof_server_ms'] - latest <= config.bar_lag_grace_ms
    if (reason or view['reason'] is not None or view['features'] != _features(document) or
            view['latest_h1_close_ms'] != latest or view['h1_signal_eligible'] != eligible):
        _fail('VIEW_CAUSALITY')
    return deepcopy(view)


def pack_context(document):
    validate_document(document)
    encoded = base64.b64encode(zlib.compress(_encoded(document), level=6)).decode('ascii')
    if len(encoded) > MAX_PACKED_BYTES:
        _fail('PACKED_CONTEXT_BOUND')
    return {'encoding': 'zlib-base64', 'sha256': digest(document), 'data': encoded}


def unpack_context(envelope):
    if (not isinstance(envelope, dict) or set(envelope) != {'encoding', 'sha256', 'data'} or
            envelope['encoding'] != 'zlib-base64' or not isinstance(envelope['data'], str) or len(envelope['data']) > MAX_PACKED_BYTES):
        _fail('PACKED_CONTEXT_BOUND')
    try:
        compressed = base64.b64decode(envelope['data'], validate=True)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, MAX_CONTEXT_BYTES + 1)
        if len(raw) > MAX_CONTEXT_BYTES or decoder.unconsumed_tail or decoder.unused_data or not decoder.eof:
            _fail('PACKED_CONTEXT_BOUND')
        document = json.loads(raw)
    except (ValueError, zlib.error):
        raise HTFError('PACKED_CONTEXT_INVALID') from None
    validate_document(document)
    if raw != _encoded(document) or digest(document) != envelope['sha256']:
        _fail('CONTEXT_HASH')
    return document


def attach_context(snapshot, view, resolve_context):
    """Add self-contained public raw subtree; leave caller and legacy M1 untouched."""
    view = replay_view(view, resolve_context)
    if (int(snapshot['at'] * 1000) != view['asof_local_ms'] or
            int(snapshot['server_time'] * 1000) != view['asof_server_ms']):
        _fail('SNAPSHOT_ASOF')
    if any(key in snapshot['features'] for key in FEATURES) or 'htf_context' in snapshot['raw']:
        _fail('HTF_COLLISION')
    document = resolve_context(view['context_sha256']) if view['context_sha256'] else None
    if document is not None and digest(document) != view['context_sha256']:
        _fail('CONTEXT_HASH')
    envelope = pack_context(document) if document is not None else None
    result = deepcopy(snapshot)
    result['raw']['htf_context'] = {'view': view, 'context': envelope}
    result['htf'] = deepcopy(view)
    result['features'].update(view['features'])
    result['m1_snapshot_sha256'] = snapshot['sha256']
    result['sha256'] = digest(result['raw'])
    return result


def restore_context(snapshot, subtree):
    """For build_snapshot/export hook: decode then attach to the M1-only snapshot."""
    if not isinstance(subtree, dict) or set(subtree) != {'view', 'context'} or not isinstance(subtree['view'], dict):
        _fail('RAW_HTF_SCHEMA')
    document = unpack_context(subtree['context']) if subtree['context'] is not None else None
    if document is not None and digest(document) != subtree['view'].get('context_sha256'):
        _fail('CONTEXT_HASH')
    return attach_context(snapshot, subtree['view'], lambda key: document if document is not None and digest(document) == key else None)


def build_with_optional_htf(raw, received_at, legacy_builder, **kwargs):
    """Precise opt-in build_snapshot hook. HTF faults leave valid M1 functional.

    Pass the ORIGINAL M1 builder, not this wrapper recursively. Rejected optional
    subtree is not re-exported; only its digest is retained outside typed public
    context as source evidence. The original journal remains unchanged.
    """
    if 'htf_context' not in raw:
        return legacy_builder(raw, received_at, **kwargs)
    m1_raw = deepcopy(raw)
    subtree = m1_raw.pop('htf_context')
    snapshot = legacy_builder(m1_raw, received_at, **kwargs)
    try:
        return restore_context(snapshot, subtree)
    except (HTFError, KeyError, TypeError):
        view = {'schema': VIEW_SCHEMA, 'instrument': INSTRUMENT, 'status': 'UNKNOWN',
                'reason': 'RECORDED_CONTEXT_INVALID', 'context_sha256': None,
                'asof_local_ms': int(received_at * 1000), 'asof_server_ms': int(snapshot['server_time'] * 1000),
                'asof_monotonic_ms': 0, 'last_refresh_monotonic_ms': None,
                'max_cache_age_ms': 120_000, 'bar_lag_grace_ms': 120_000,
                'latest_h1_close_ms': None, 'h1_signal_eligible': False, 'features': dict.fromkeys(FEATURES)}
        result = attach_context(snapshot, view, lambda key: None)
        try:
            result['rejected_htf_source_sha256'] = digest(subtree)
        except (TypeError, ValueError):
            result['rejected_htf_source_sha256'] = None
        return result


def g1_view(snapshot):
    """Decode self-contained snapshot into G1 records; never seed a detector here.

    Caller bootstraps WITHOUT historical decisions and must require a new pivot
    after bootstrap, deduplicate open_ts, preserve consumed setups and honor the
    h1_signal_eligible flag. Integer-second rounding is conservative on BOTH sides.
    """
    subtree = snapshot.get('raw', {}).get('htf_context')
    if not isinstance(subtree, dict) or set(subtree) != {'view', 'context'}:
        _fail('RAW_HTF_SCHEMA')
    document = unpack_context(subtree['context']) if subtree['context'] is not None else None
    view = replay_view(subtree['view'], lambda key: document if document is not None and digest(document) == key else None)
    if int(snapshot['server_time'] * 1000) != view['asof_server_ms'] or int(snapshot['at'] * 1000) != view['asof_local_ms']:
        _fail('SNAPSHOT_ASOF')
    result = {'status': view['status'], 'reason': view['reason'], 'now': view['asof_server_ms'] // 1000,
              'context_sha256': view['context_sha256'], 'h1_signal_eligible': False,
              'records': [], 'provenance': [], 'h4': []}
    if view['status'] != 'READY':
        return result
    if any((b['available_server_ms_upper'] + 999) // 1000 > result['now'] for b in document['series']['1H']):
        return {**result, 'status': 'UNKNOWN', 'reason': 'G1_SECOND_BOUNDARY'}
    result.update(h1_signal_eligible=view['h1_signal_eligible'],
                  initial_server_cutoff_ms=document['initial_server_cutoff_ms'],
                  initial_knowledge_at_ms=document['initial_knowledge_at_ms'])
    for bar in document['series']['1H']:
        result['records'].append({'open_ts': bar['open_ms'] // 1000,
            'received_at': (bar['available_server_ms_upper'] + 999) // 1000,
            **{key: float(bar[key]) for key in ('open', 'high', 'low', 'close')}, 'complete': True})
        result['provenance'].append({key: bar[key] for key in ('knowledge', 'available_at_ms',
            'confirmed_at_ms', 'observed_server_ms', 'available_server_ms_lower', 'available_server_ms_upper')})
    result['h4'] = aggregate_h4(document['series']['1H'])
    return result
