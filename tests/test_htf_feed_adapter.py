"""Root-copyable HTF adapter/replay checks. Synthetic transport + temp files only.

Run from repository root:
  python -B -X utf8 -m unittest discover -s <this directory> -p test_htf_feed_adapter.py -v

No production runtime, network, credentials, model, trading worker, or Git calls.
The AcceptanceGaps class asserts proposed stricter contracts; a RED is evidence
for root review, not a claim that an unauthorized request occurred.
"""
import ast
import base64
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import zipfile
import zlib

from trading_lab import arena_market as market
from trading_lab import causal_htf as htf
from trading_lab.arena_strategies import ArenaError
from trading_lab.htf_feed import HTFPublicFeed


HOUR = 3_600_000
BASE = 1_800_000_000_000 // (4 * HOUR) * (4 * HOUR)
SOURCE_ROOT = Path(market.__file__).resolve().parent


def candle(start, close='100', confirm='1'):
    return [str(start), '100', '105', '95', close, '10', '0.1', '10', confirm]


class SimulatedClock:
    def __init__(self):
        self.wall_ms = BASE + 300 * HOUR + 10_000
        self.mono_ms = 100_000

    def clock(self):
        return self.wall_ms / 1000

    def monotonic(self):
        return self.mono_ms / 1000

    def advance(self, milliseconds):
        self.wall_ms += milliseconds
        self.mono_ms += milliseconds


class FakeResponse:
    def __init__(self, transport, record, payload):
        self.transport, self.record, self.payload = transport, record, payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        self.record['read_limit'] = limit
        self.transport.time.advance(self.transport.delay_ms(self.record))
        return self.payload[:limit]


class FakePublicTransport:
    """All payloads are synthetic, generated without external state."""
    def __init__(self, clock):
        self.time = clock
        self.requests = []
        self.fail_once = lambda record: False
        self.delay_ms = lambda record: 100
        self.replace_payload = lambda record, payload: payload

    def open(self, request, timeout):
        parsed = urlsplit(request.full_url)
        record = {'path': parsed.path, 'query': parse_qs(parsed.query),
                  'host': parsed.netloc, 'method': request.get_method(),
                  'headers': dict(request.header_items()), 'timeout': timeout,
                  'started_mono_ms': self.time.mono_ms}
        self.requests.append(record)
        if self.fail_once(record):
            self.fail_once = lambda record: False
            raise OSError('SYNTHETIC_TRANSPORT_DETAIL_MUST_NOT_ESCAPE')
        now = self.time.wall_ms
        path, query = record['path'], record['query']
        if path == '/api/v5/public/time':
            data = [{'ts': str(now)}]
        elif path == '/api/v5/public/instruments':
            data = [{'instId': htf.INSTRUMENT, 'state': 'live', 'ctValCcy': 'BTC',
                     'settleCcy': 'USDT', 'ctVal': '.01', 'ctMult': '1',
                     'lotSz': '.01', 'minSz': '.01'}]
        elif path == '/api/v5/public/funding-rate':
            data = [{'instId': htf.INSTRUMENT, 'fundingRate': '.0001',
                     'fundingTime': str(now + 1_000_000), 'ts': str(now)}]
        elif path == '/api/v5/market/ticker':
            data = [{'instId': htf.INSTRUMENT, 'bidPx': '100', 'askPx': '100.1',
                     'bidSz': '100', 'askSz': '100', 'ts': str(now)}]
        elif path in ('/api/v5/market/history-candles', '/api/v5/market/candles'):
            bar = query['bar'][0]
            interval = {'1m': 60_000, '1H': HOUR, '15m': 900_000}[bar]
            boundary = now // interval * interval
            rows = [candle(boundary - i * interval) for i in range(1, 301)]
            if 'after' in query:
                rows = [row for row in rows if int(row[0]) < int(query['after'][0])]
            data = rows[:int(query['limit'][0])]
        else:
            # For the adversarial duck-typed-request test only: no real endpoint.
            data = []
        payload = json.dumps({'code': '0', 'data': data}, separators=(',', ':')).encode()
        return FakeResponse(self, record, self.replace_payload(record, payload))


class AdapterFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-htf-adapter-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.sim = SimulatedClock()
        self.transport = FakePublicTransport(self.sim)
        self.feed = HTFPublicFeed(self.root / 'context', clock=self.sim.clock,
                                  monotonic=self.sim.monotonic)
        self.feed.opener = self.transport
        # Parent PublicFeed still uses module time.time rather than self.clock.
        self.time_patch = patch.object(market.time, 'time', self.sim.clock)
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)
        # Catch any accidental nonfake network call instead of making it.
        self.network_patch = patch.object(socket.socket, 'connect',
                                          side_effect=AssertionError('NETWORK_FORBIDDEN'))
        self.network_patch.start()
        self.addCleanup(self.network_patch.stop)

    def ready(self):
        value = self.feed.snapshot()
        self.assertEqual(value['htf']['status'], 'READY')
        return value

    def document(self, snapshot):
        return self.feed.provider.context(snapshot['htf']['context_sha256'])

    def public_row(self, snapshot):
        result = {key: deepcopy(value) for key, value in snapshot.items() if key != 'raw'}
        result.update(htf_context=deepcopy(snapshot['raw']['htf_context']),
                      source='SYNTHETIC', capture_at=snapshot['at'],
                      source_snapshot_sha256=snapshot['sha256'],
                      features_rebuilt_from_recorded_public=True)
        return result


class HTFAdapterTests(AdapterFixture):
    def test_initial_snapshot_is_nine_shared_gets_with_durable_seed(self):
        snapshot = self.ready()
        self.assertEqual(self.feed.calls, 9)
        self.assertEqual(self.feed.provider.calls, 3)
        self.assertEqual(len(self.transport.requests), 9)
        doc = self.document(snapshot)
        self.assertEqual(len(doc['series']['1H']), 256)
        self.assertTrue(all(bar['knowledge'] == 'initial_history' for bar in doc['series']['1H']))
        self.assertFalse(snapshot['htf']['h1_signal_eligible'])
        files = list(self.feed.context_path.glob('*.json'))
        self.assertEqual(len(files), 1)
        self.assertEqual(json.loads(files[0].read_bytes()), doc)
        self.assertEqual(files[0].stem, htf.digest(doc))

    def test_initial_history_paginates_older_with_limit_one_hundred(self):
        self.ready()
        rows = [r for r in self.transport.requests if r['path'].endswith('history-candles')]
        self.assertEqual(len(rows), 3)
        self.assertNotIn('after', rows[0]['query'])
        self.assertGreater(int(rows[1]['query']['after'][0]), int(rows[2]['query']['after'][0]))
        for row in rows:
            self.assertEqual(row['query']['bar'], ['1H'])
            self.assertEqual(row['query']['limit'], ['100'])
            self.assertEqual(row['query']['instId'], [htf.INSTRUMENT])

    def test_no_auth_only_get_fixed_host_timeout_and_bounded_read(self):
        self.ready()
        allowed = {urlsplit(path).path for path in market.PATHS.values()}
        allowed.add('/api/v5/market/history-candles')
        for row in self.transport.requests:
            self.assertEqual(row['method'], 'GET')
            self.assertEqual(row['host'], urlsplit(market.BASE).netloc)
            self.assertIn(row['path'], allowed)
            self.assertEqual(row['timeout'], 6)
            self.assertFalse(any(k.lower() in {'authorization', 'cookie', 'ok-access-key'} for k in row['headers']))
            limit = 262145 if row['query'].get('bar') == ['1H'] else 524289
            self.assertEqual(row['read_limit'], limit)

    def test_less_than_sixty_seconds_only_four_m1_gets(self):
        first = self.ready()
        self.sim.advance(5000)
        second = self.ready()
        self.assertEqual(self.feed.calls, 13)
        self.assertEqual(self.feed.provider.calls, 3)
        self.assertEqual(first['htf']['context_sha256'], second['htf']['context_sha256'])
        self.assertEqual(sum(r['path'].endswith('instruments') for r in self.transport.requests), 1)

    def test_scheduled_refresh_is_six_gets_and_no_duplicate_document(self):
        first = self.ready()
        self.sim.advance(60_000)
        second = self.ready()
        self.assertEqual(self.feed.calls, 15)
        self.assertEqual(self.feed.provider.calls, 4)
        self.assertEqual(first['htf']['context_sha256'], second['htf']['context_sha256'])
        self.assertEqual(len(list(self.feed.context_path.glob('*.json'))), 1)
        self.assertEqual(self.transport.requests[10]['query']['limit'], ['32'])

    def test_candle_failure_retains_m1_and_no_immediate_retry(self):
        self.transport.fail_once = lambda row: row['path'].endswith('history-candles')
        snapshot = self.feed.snapshot()
        self.assertEqual(snapshot['htf']['status'], 'UNKNOWN')
        self.assertEqual(snapshot['htf']['reason'], 'FETCH_FAILED')
        self.assertEqual(snapshot['features']['rsi_14'], 50)
        self.assertNotIn('SYNTHETIC_TRANSPORT_DETAIL', json.dumps(snapshot))
        self.assertEqual(self.feed.calls, 7)
        self.sim.advance(5000)
        self.feed.snapshot()
        self.assertEqual(self.feed.calls, 11)
        self.assertEqual(self.feed.provider.calls, 1)

    def test_bootstrap_clock_failure_keeps_m1_and_unknown_htf(self):
        self.transport.fail_once = lambda row: row['path'].endswith('/time')
        snapshot = self.feed.snapshot()
        self.assertEqual(snapshot['htf']['status'], 'UNKNOWN')
        self.assertEqual(snapshot['features']['rsi_14'], 50)
        self.assertEqual(self.feed.calls, 6)
        self.assertEqual(self.feed.provider.calls, 0)

    def test_page_budget_rejects_before_transport(self):
        self.feed.max_calls = 0
        with self.assertRaises(ArenaError):
            self.feed.get_page(htf.Request('/api/v5/market/candles', '1H', 32))
        self.assertEqual(self.feed.calls, 0)
        self.assertEqual(self.transport.requests, [])

    def test_failed_page_consumes_global_budget(self):
        self.transport.fail_once = lambda row: True
        self.feed.max_calls = 1
        request = htf.Request('/api/v5/market/candles', '1H', 32)
        with self.assertRaises(OSError):
            self.feed.get_page(request)
        with self.assertRaises(ArenaError):
            self.feed.get_page(request)
        self.assertEqual(self.feed.calls, 1)
        self.assertEqual(len(self.transport.requests), 1)

    def test_request_validation_before_budget_and_transport(self):
        for request in (htf.Request('/api/v5/account/balance', '1H', 10),
                        htf.Request('/api/v5/market/candles', '4H', 10),
                        htf.Request('/api/v5/market/candles', '1H', 101)):
            with self.subTest(request=request), self.assertRaises(htf.HTFError):
                self.feed.get_page(request)
        self.assertEqual(self.feed.calls, 0)
        self.assertEqual(self.transport.requests, [])

    def test_page_size_limit_rejects_and_counts_attempt(self):
        self.transport.replace_payload = lambda row, payload: b'x' * 262145
        with self.assertRaisesRegex(ArenaError, 'htf_response_too_large'):
            self.feed.get_page(htf.Request('/api/v5/market/candles', '1H', 32))
        self.assertEqual(self.feed.calls, 1)
        self.assertEqual(self.transport.requests[0]['read_limit'], 262145)

    def test_page_shape_and_row_count_are_validated(self):
        for value in ({'code': '1', 'data': []}, {'code': '0', 'data': {}},
                      {'code': '0', 'data': [[], []]}, []):
            self.transport.replace_payload = lambda row, payload: json.dumps(value).encode()
            with self.subTest(value=value), self.assertRaises(ArenaError):
                self.feed.get_page(htf.Request('/api/v5/market/candles', '1H', 1))
        self.assertEqual(self.feed.calls, 4)

    def test_clock_bracket_spans_transport_and_page_receipt_is_after_read(self):
        before = self.sim.mono_ms
        self.feed.get('clock')
        clock = self.feed.clock_sample
        self.assertEqual(clock.request_started_mono_ms, before)
        self.assertEqual(clock.monotonic_ms, before + 100)
        page = self.feed.get_page(htf.Request('/api/v5/market/candles', '1H', 32))
        self.assertEqual(page.monotonic_ms, before + 200)
        self.assertEqual(page.received_at_ms, self.sim.wall_ms)

    def test_fetch_over_fifteen_seconds_is_unknown_after_read_returns(self):
        self.transport.delay_ms = lambda row: 16_000 if row['path'].endswith('history-candles') else 100
        snapshot = self.feed.snapshot()
        self.assertEqual(snapshot['htf']['reason'], 'FETCH_DEADLINE')
        self.assertEqual(snapshot['features']['rsi_14'], 50)
        self.assertGreaterEqual(self.sim.mono_ms - 100_000, 16_000)
        # This proves stale-result rejection, not a hard wall-time cancellation.

    def test_redirect_handler_does_not_return_followup_request(self):
        self.assertIsNone(market.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.invalid/'))

    def test_persist_is_idempotent_and_corruption_does_not_ack(self):
        snapshot = self.ready()
        doc = self.document(snapshot)
        self.assertTrue(self.feed.persist(doc))
        path = self.feed.context_path / (htf.digest(doc) + '.json')
        path.write_bytes(b'corrupt synthetic context')
        self.assertFalse(self.feed.persist(doc))

    def test_context_file_count_cap_retains_valid_existing_context(self):
        snapshot = self.ready()
        doc = self.document(snapshot)
        for index in range(511):
            (self.feed.context_path / ('fixture-' + str(index) + '.json')).write_bytes(b'{}')
        self.assertTrue(self.feed.persist(doc))
        other = deepcopy(doc)
        other['created_at_ms'] += 1
        self.assertFalse(self.feed.persist(other))
        self.assertEqual(len(list(self.feed.context_path.glob('*.json'))), 512)

    def test_recorded_snapshot_rebuild_is_exact(self):
        snapshot = self.ready()
        rebuilt = market.build_snapshot(deepcopy(snapshot['raw']), snapshot['at'])
        self.assertEqual(snapshot, rebuilt)
        self.assertEqual(htf.g1_view(snapshot), htf.g1_view(rebuilt))

    def test_absent_htf_is_exact_legacy_snapshot(self):
        snapshot = self.ready()
        raw = deepcopy(snapshot['raw'])
        del raw['htf_context']
        self.assertEqual(market.build_snapshot(raw, snapshot['at']),
                         market._build_m1_snapshot(raw, snapshot['at']))

    def test_corrupt_recorded_context_is_sanitized_and_keeps_m1(self):
        snapshot = self.ready()
        raw = deepcopy(snapshot['raw'])
        raw['htf_context']['context']['data'] = 'SYNTHETIC_SENTINEL_BAD_BASE64'
        raw['htf_context']['unrecognized'] = 'SYNTHETIC_SENTINEL_FIELD'
        rebuilt = market.build_snapshot(raw, snapshot['at'])
        self.assertEqual(rebuilt['htf']['reason'], 'RECORDED_CONTEXT_INVALID')
        self.assertEqual(rebuilt['features']['rsi_14'], 50)
        self.assertIsNone(rebuilt['raw']['htf_context']['context'])
        self.assertNotIn('SYNTHETIC_SENTINEL', json.dumps(rebuilt))
        self.assertEqual(rebuilt['rejected_htf_source_sha256'], htf.digest(raw['htf_context']))

    def test_packed_document_extra_key_rejected_even_after_rehash(self):
        snapshot = self.ready()
        doc = self.document(snapshot)
        doc['unrecognized'] = 'SYNTHETIC_SENTINEL_FIELD'
        encoded = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        envelope = {'encoding': 'zlib-base64', 'sha256': htf.digest(doc),
                    'data': base64.b64encode(zlib.compress(encoded)).decode()}
        with self.assertRaises(htf.HTFError):
            htf.unpack_context(envelope)

    def test_packed_zlib_trailing_data_rejected(self):
        snapshot = self.ready()
        envelope = deepcopy(snapshot['raw']['htf_context']['context'])
        envelope['data'] = base64.b64encode(base64.b64decode(envelope['data']) + b'extra').decode()
        with self.assertRaises(htf.HTFError):
            htf.unpack_context(envelope)

    def test_packed_decompression_over_bound_rejected(self):
        envelope = {'encoding': 'zlib-base64', 'sha256': 'a' * 64,
                    'data': base64.b64encode(zlib.compress(b'x' * (htf.MAX_CONTEXT_BYTES + 1))).decode()}
        with self.assertRaises(htf.HTFError):
            htf.unpack_context(envelope)

    def test_current_source_hash_lists_cover_transitive_htf_dependencies(self):
        for name, function in (('arena.py', 'arena_source_hash'),
                               ('strategy_training.py', 'source_hash'),
                               ('exchange_lab.py', 'source_hash')):
            tree = ast.parse((SOURCE_ROOT / name).read_text(encoding='utf-8-sig'))
            node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function)
            # Since D098 the file list is the module-level SOURCE_FILES the function hashes.
            listed = [n for n in tree.body if isinstance(n, ast.Assign) and any(getattr(t, 'id', None) == 'SOURCE_FILES' for t in n.targets)]
            self.assertTrue(any(isinstance(n, ast.Name) and n.id == 'SOURCE_FILES' for n in ast.walk(node)), name)
            strings = {n.value for item in [node, *listed] for n in ast.walk(item) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
            expected = {'causal_htf.py', 'ikigai_g1.py', 'ikigai_g1_reference.py', 'arena_market.py'}
            if name == 'arena.py':
                expected.add('htf_feed.py')
            with self.subTest(source=name):
                self.assertTrue(expected.issubset(strings), expected - strings)


class AcceptanceGaps(AdapterFixture):
    def test_failed_scheduled_clock_marks_htf_unknown_until_successful_refresh(self):
        self.ready()
        self.sim.advance(60_000)
        self.transport.fail_once = lambda row: row['path'].endswith('/time')
        snapshot = self.feed.snapshot()
        self.assertEqual(snapshot['features']['rsi_14'], 50)
        self.assertEqual(snapshot['htf']['status'], 'UNKNOWN')

    def test_failed_clock_stays_unknown_between_polls_then_recovers_on_schedule(self):
        self.ready()
        self.sim.advance(60_000)
        self.transport.fail_once = lambda row: row['path'].endswith('/time')
        failed = self.feed.snapshot()
        self.assertEqual(failed['htf']['status'], 'UNKNOWN')
        calls = self.feed.calls
        self.sim.advance(5000)
        still_unknown = self.feed.snapshot()
        self.assertEqual(still_unknown['htf']['status'], 'UNKNOWN')
        self.assertFalse(still_unknown['htf']['h1_signal_eligible'])
        self.assertTrue(all(value is None for value in still_unknown['htf']['features'].values()))
        self.assertEqual(self.feed.calls - calls, 4)
        self.sim.advance(60_000)
        recovered = self.ready()
        self.assertEqual(recovered['htf']['context_sha256'], failed['htf']['context_sha256'])

    def test_valid_existing_context_is_read_with_an_explicit_byte_bound(self):
        snapshot = self.ready()
        doc = self.document(snapshot)
        path = self.feed.context_path / (htf.digest(doc) + '.json')
        expected = path.read_bytes()
        original_open = Path.open
        limits = []
        class GuardedRead:
            def __init__(self, handle): self.handle = handle
            def __enter__(self): return self
            def __exit__(self, *args): self.handle.close()
            def fileno(self): return self.handle.fileno()
            def read(self, size=-1):
                limits.append(size)
                if size < 0 or size > len(expected) + 1:
                    raise AssertionError('EXISTING_CONTEXT_READ_NOT_BOUNDED')
                return self.handle.read(size)
        def guarded_open(item, *args, **kwargs):
            handle = original_open(item, *args, **kwargs)
            return GuardedRead(handle) if item == path else handle
        with patch.object(Path, 'open', guarded_open):
            self.assertTrue(self.feed.persist(doc))
        self.assertEqual(limits, [len(expected) + 1])

    def test_adversarial_request_object_cannot_replace_allowlist_validation(self):
        request = SimpleNamespace(path='/api/v5/account/balance', limit=1, params=lambda: {})
        with self.assertRaises((htf.HTFError, ArenaError, TypeError)):
            self.feed.get_page(request)
        self.assertEqual(self.transport.requests, [])

    def test_wrong_size_existing_context_rejected_before_unbounded_read(self):
        snapshot = self.ready()
        doc = self.document(snapshot)
        path = self.feed.context_path / (htf.digest(doc) + '.json')
        with path.open('ab') as out:
            out.write(b'one extra byte')
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('UNBOUNDED_READ_USED')):
            self.assertFalse(self.feed.persist(doc))

    def test_revised_pilot_capture_capacity_accepts_24000_temporary_quotes(self):
        from trading_lab.battle_debrief import recorded_quotes
        path = self.root / 'synthetic-24000-quotes.jsonl'
        with path.open('w', encoding='utf-8') as output:
            for index in range(24_000):
                row = {'at': self.sim.clock() + index * 5,
                       'raw': {'synthetic': True, 'ticker': [{'bidPx': '100', 'askPx': '100.1'}]}}
                output.write(json.dumps(row, separators=(',', ':')) + '\n')
        try:
            quotes = recorded_quotes(path, self.sim.clock() + 24_000 * 5)
        except ArenaError as error:
            self.fail('Revised PAPER 24000-capture profile cannot be read: ' + str(error))
        self.assertEqual(len(quotes), 24_000)


def write_repacked_market(archive_source, transform):
    """Rehash intentionally altered synthetic public row to test schema, not ZIP SHA."""
    archive_source.seek(0)
    with zipfile.ZipFile(archive_source) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    rows = [json.loads(line) for line in members['market.jsonl'].splitlines()]
    transform(rows[0])
    members['market.jsonl'] = b''.join(json.dumps(row, separators=(',', ':')).encode() + b'\n' for row in rows)
    manifest = json.loads(members['manifest.json'])
    for entry in manifest['files']:
        if entry['path'] == 'market.jsonl':
            entry.update(bytes=len(members['market.jsonl']), sha256=hashlib.sha256(members['market.jsonl']).hexdigest())
    members['manifest.json'] = json.dumps(manifest).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in members.items():
            archive.writestr(name, value)
    output.seek(0)
    return output


class ExportAcceptanceTests(AdapterFixture):
    def archive(self, *, with_htf=True, malformed_tail=False):
        from trading_lab.arena_strategies import catalogue_defaults, fingerprint
        from trading_lab.battle_export import create_archive
        from trading_lab.strategy_contract import payload_of
        from tests.test_battle_review import ReviewTests
        state = ReviewTests().fixture()  # Existing pure synthetic in-memory fixture.
        state.update(fixture_only=True, duration_seconds=300, execution_mode='rules_only')
        recipes = []
        for current in state['strategies']:
            row = deepcopy(catalogue_defaults()[0])
            row.update(id=current['id'], name=current['id'], version=1)
            row['recipe_sha256'] = fingerprint(payload_of(row))
            recipes.append(row)
        state['strategies'] = recipes
        snapshot = self.ready()
        snapshot['raw']['synthetic'] = True
        if not with_htf:
            del snapshot['raw']['htf_context']
        snapshot = market.build_snapshot(snapshot['raw'], snapshot['at'])
        state['latest_market'] = snapshot
        path = self.root / 'synthetic-snapshots.jsonl'
        path.write_text(json.dumps(snapshot) + '\n' + ('broken\n' if malformed_tail else ''), encoding='utf-8')
        result = create_archive(state, snapshots_path=path)
        self.addCleanup(result.close)
        return result

    def test_export_retains_self_contained_context_and_rebuilds_g1(self):
        from trading_lab.battle_export import verify_archive
        source = self.archive()
        self.assertTrue(verify_archive(source)['verified'])
        source.seek(0)
        with zipfile.ZipFile(source) as archive:
            row = json.loads(archive.read('market.jsonl').splitlines()[0])
        self.assertEqual(row['source'], 'SYNTHETIC')
        self.assertNotIn('raw', row)
        result = htf.g1_view({**row, 'raw': {'htf_context': row['htf_context']}})
        self.assertEqual(result['context_sha256'], row['htf']['context_sha256'])
        self.assertTrue(row['features_rebuilt_from_recorded_public'])

    def test_archive_rejects_unrecognized_public_market_field(self):
        from trading_lab.battle_export import verify_archive
        source = self.archive()
        changed = write_repacked_market(source, lambda row: row.update(unrecognized='SYNTHETIC_SENTINEL_FIELD'))
        with changed, self.assertRaises((ArenaError, htf.HTFError)):
            verify_archive(changed)

    def test_archive_rejects_top_level_view_mismatch(self):
        from trading_lab.battle_export import verify_archive
        source = self.archive()
        changed = write_repacked_market(source, lambda row: row['htf'].update(latest_h1_close_ms=0))
        with changed, self.assertRaises((ArenaError, htf.HTFError)):
            verify_archive(changed)

    def test_archive_rejects_htf_feature_mismatch(self):
        from trading_lab.battle_export import verify_archive
        source = self.archive()
        changed = write_repacked_market(source, lambda row: row['features'].update(htf_1h_sma20_gap_bps=999))
        with changed, self.assertRaises((ArenaError, htf.HTFError)):
            verify_archive(changed)

    def test_archive_rejects_unknown_view_with_an_unrelated_context_hash(self):
        from trading_lab.battle_export import verify_archive
        source = self.archive()
        def mutate(row):
            view = row['htf_context']['view']
            view.update(status='UNKNOWN', reason='FETCH_FAILED', context_sha256='b' * 64,
                        h1_signal_eligible=False, features=dict.fromkeys(htf.FEATURES))
            row['htf'] = deepcopy(view)
            row['features'].update(view['features'])
        changed = write_repacked_market(source, mutate)
        with changed, self.assertRaises((ArenaError, htf.HTFError)):
            verify_archive(changed)

    def test_archive_rejects_missing_extension_context(self):
        from trading_lab.battle_export import verify_archive
        source = self.archive()
        changed = write_repacked_market(source, lambda row: row.pop('htf_context'))
        with changed, self.assertRaises((ArenaError, htf.HTFError)):
            verify_archive(changed)

    def test_archive_accepts_existing_m1_only_export_without_new_htf_fields(self):
        from trading_lab.battle_export import verify_archive
        source = self.archive(with_htf=False)
        self.assertTrue(verify_archive(source)['verified'])
        source.seek(0)
        with zipfile.ZipFile(source) as archive:
            row = json.loads(archive.read('market.jsonl').splitlines()[0])
        self.assertNotIn('htf', row)
        self.assertNotIn('htf_context', row)
        self.assertFalse(set(htf.FEATURES) & set(row['features']))

    def test_archive_rejects_bool_in_duplicated_view_numeric_feature(self):
        from trading_lab.battle_export import verify_archive
        source = self.archive()
        changed = write_repacked_market(source, lambda row: row['htf']['features'].update(htf_1h_sma20_gap_bps=False))
        with changed, self.assertRaises((ArenaError, htf.HTFError)):
            verify_archive(changed)

    def test_malformed_snapshot_is_counted_and_optional_diagnostics_marked_unavailable(self):
        from trading_lab.battle_export import verify_archive
        source = self.archive(malformed_tail=True)
        self.assertTrue(verify_archive(source)['verified'])
        source.seek(0)
        with zipfile.ZipFile(source) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            review = json.loads(archive.read('battle-review.json'))
        self.assertEqual(manifest['market_samples'], 1)
        self.assertEqual(manifest['market_rows_omitted'], 1)
        self.assertEqual(review['diagnostics_context']['market_snapshots'], 'UNAVAILABLE')


if __name__ == '__main__':
    unittest.main()
