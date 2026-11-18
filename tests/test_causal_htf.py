"""Small fake-transport tests; no network, engines, credentials or trading runs."""
import copy
import base64
import json
from pathlib import Path
import sys
import unittest
import zlib
from trading_lab import causal_htf as htf

from trading_lab.causal_htf import (ClockSample, Config, HTFError, HTFProvider, Page,
                        aggregate_h4, attach_context, replay_view, digest)

HOUR = 3_600_000
BASE = 1_800_000_000_000 // (4 * HOUR) * (4 * HOUR)


def candle(start, close='100', confirm='1'):
    return [str(start), '100', '105', '95', close, '10', '0.1', '10', confirm]


class Fake:
    def __init__(self, h1=256, m15=False):
        self.now = BASE + 300 * HOUR + 10_000
        self.mono = 100_000
        self.rows = {'1H': [candle(BASE + i * HOUR) for i in range(300)]}
        if m15:
            self.rows['15m'] = [candle(BASE + i * 900_000) for i in range(1200)]
        self.requests, self.docs = [], {}
        self.fail = False
        self.ack = True
        self.transform = lambda rows, request: rows
        self.provider = HTFProvider(self.get, self.persist, config=Config(h1_bars=h1, include_m15=m15))

    def clock(self):
        return ClockSample(self.now, self.now, self.mono, self.mono)

    def get(self, request):
        self.requests.append(request)
        if self.fail:
            raise RuntimeError('DO_NOT_LEAK_TRANSPORT_DETAILS')
        candidates = self.rows[request.bar]
        if request.after_ms is not None:
            candidates = [r for r in candidates if int(r[0]) < request.after_ms]
        result = sorted(candidates, key=lambda r: int(r[0]), reverse=True)[:request.limit]
        result = self.transform(copy.deepcopy(result), request)
        return Page(result, self.now + 1, self.mono + 1)

    def persist(self, document):
        if self.ack:
            self.docs[digest(document)] = copy.deepcopy(document)
        return self.ack

    def refresh(self):
        result = self.provider.refresh(self.clock(), self.now, self.mono)
        self.now += 2
        self.mono += 2
        return result

    def view(self):
        return self.provider.view(self.clock(), self.now, self.mono)

    def advance(self, ms=60_001):
        self.now += ms
        self.mono += ms

    def document(self):
        return self.docs[self.view()['context_sha256']]


class CausalHTFTests(unittest.TestCase):
    def test_bounded_warmup_and_initial_knowledge(self):
        f = Fake(); f.refresh(); v = f.view(); doc = f.document()
        self.assertEqual(v['status'], 'READY')
        self.assertEqual(len(f.requests), 3)
        self.assertEqual(len(doc['series']['1H']), 256)
        self.assertTrue(all(b['knowledge'] == 'initial_history' for b in doc['series']['1H']))
        self.assertTrue(all(b['available_at_ms'] == doc['initial_knowledge_at_ms'] for b in doc['series']['1H']))
        self.assertTrue(all(r.path == '/api/v5/market/history-candles' and r.limit == 100 for r in f.requests))
        self.assertLess(len(json.dumps(v)), 4000)

    def test_h4_alignment_and_children_availability(self):
        f = Fake(); f.refresh(); bars = f.document()['series']['1H']; h4 = aggregate_h4(bars)
        self.assertGreaterEqual(len(h4), 63)
        self.assertTrue(all(b['open_ms'] % (4 * HOUR) == 0 for b in h4))
        self.assertTrue(all(b['close_ms'] - b['open_ms'] == 4 * HOUR for b in h4))
        self.assertEqual(h4[-1]['available_at_ms'], max(b['available_at_ms'] for b in bars[-4:]))

    def test_partial_h4_omitted_gap_not_fabricated(self):
        f = Fake(); f.refresh(); bars = f.document()['series']['1H'][-8:]
        self.assertEqual(len(aggregate_h4(bars[:-1])), 1)
        with self.assertRaises(HTFError): aggregate_h4(bars[:2] + bars[3:])

    def test_unconfirmed_never_exposed(self):
        f = Fake(); f.rows['1H'].append(candle(BASE + 300 * HOUR, confirm='0')); f.refresh()
        self.assertEqual(len(f.document()['series']['1H']), 256)
        self.assertEqual(f.document()['series']['1H'][-1]['close_ms'], BASE + 300 * HOUR)

    def test_confirmed_future_rejected(self):
        f = Fake(); f.rows['1H'].append(candle(BASE + 300 * HOUR)); f.refresh()
        self.assertEqual(f.view()['reason'], 'FUTURE_CONFIRMED_BAR')
        self.assertFalse(f.docs)

    def test_no_provider_knowledge_before_arrival(self):
        f = Fake(); initial = f.now; f.refresh()
        v = f.provider.view(ClockSample(initial, initial, 100_000, 100_000), initial, 100_000)
        self.assertEqual(v['status'], 'UNKNOWN')
        self.assertIsNone(v['features']['htf_1h_sma20_gap_bps'])

    def test_forward_bar_not_bootstrap(self):
        f = Fake(); f.refresh(); old = copy.deepcopy(f.document())
        f.advance(HOUR); f.rows['1H'].append(candle(BASE + 300 * HOUR, '101')); f.refresh()
        doc = f.document(); last = doc['series']['1H'][-1]
        self.assertEqual(last['knowledge'], 'forward_confirmed')
        self.assertEqual(last['confirmed_at_ms'], last['available_at_ms'])
        self.assertEqual(len(doc['series']['1H']), 256)
        self.assertEqual(len(f.docs), 2)
        self.assertEqual(old, f.docs[digest(old)])

    def test_no_duplicate_fetch_for_multiple_bots(self):
        f = Fake(); f.refresh(); calls = len(f.requests)
        for _ in range(6):
            f.refresh(); self.assertEqual(f.view()['status'], 'READY')
        self.assertEqual(len(f.requests), calls)
        self.assertEqual(len(f.docs), 1)

    def test_unchanged_refresh_reuses_context(self):
        f = Fake(); f.refresh(); first = f.view()['context_sha256']
        f.advance(); f.refresh()
        self.assertEqual(f.view()['context_sha256'], first)
        self.assertEqual(len(f.docs), 1)

    def test_revised_closed_bar_sticky_fail_closed(self):
        f = Fake(); f.refresh(); f.advance(); f.rows['1H'][-1][4] = '101'; f.refresh()
        self.assertEqual(f.view()['reason'], 'REVISION')
        calls = len(f.requests); f.rows['1H'][-1][4] = '100'; f.advance(); f.refresh()
        self.assertEqual(f.view()['status'], 'UNKNOWN'); self.assertEqual(len(f.requests), calls)
        self.assertEqual(len(f.docs), 1)

    def test_gap_in_initial_history(self):
        f = Fake(); del f.rows['1H'][-90]; f.refresh()
        self.assertEqual(f.view()['reason'], 'GAP')
        self.assertFalse(f.docs)

    def test_gap_in_forward_updates(self):
        f = Fake(); f.refresh(); f.advance(2 * HOUR)
        f.rows['1H'].append(candle(BASE + 301 * HOUR)); f.refresh()
        self.assertEqual(f.view()['reason'], 'GAP')

    def test_rows_outside_retained_knowledge_do_not_claim_revision_detection(self):
        f = Fake(h1=20); f.refresh(); f.advance()
        f.rows['1H'][-21][4] = '102'; f.refresh()
        # This row was not in the retained20-bar knowledge set, so not a known revision.
        self.assertEqual(f.view()['status'], 'READY')

    def test_no_new_bar_yet_is_unknown_then_stale(self):
        f = Fake(); f.refresh(); f.advance(HOUR); f.refresh()
        self.assertEqual(f.view()['reason'], 'WAITING_CLOSED_BAR')
        f.advance(120_001); f.refresh()
        self.assertEqual(f.view()['reason'], 'STALE_CLOSED_BAR')

    def test_cache_stale_without_refresh(self):
        f = Fake(); f.refresh(); f.advance(120_001)
        self.assertEqual(f.view()['reason'], 'STALE_PROVIDER')

    def test_transport_failure_clears_readiness_no_immediate_retry(self):
        f = Fake(); f.refresh(); f.advance(); f.fail = True; f.refresh()
        self.assertEqual(f.view()['reason'], 'FETCH_FAILED')
        self.assertNotIn('DO_NOT_LEAK', json.dumps(f.view()))
        count = len(f.requests); f.refresh(); self.assertEqual(len(f.requests), count)
        f.fail = False; f.advance(); f.refresh(); self.assertEqual(f.view()['status'], 'READY')

    def test_durable_ack_required(self):
        f = Fake(); f.ack = False; f.refresh()
        self.assertEqual(f.view()['reason'], 'CONTEXT_NOT_DURABLE')
        self.assertFalse(f.docs)

    def test_call_budget_counts_failed_attempts(self):
        f = Fake(); f.provider = HTFProvider(f.get, f.persist, config=Config(max_requests=2))
        f.refresh(); self.assertEqual(f.provider.calls, 2)
        self.assertEqual(f.view()['reason'], 'REQUEST_BUDGET')
        f.advance(); f.refresh(); self.assertEqual(f.provider.calls, 2)

    def test_pagination_does_not_loop_on_nonprogress(self):
        f = Fake()
        first = sorted(f.rows['1H'], key=lambda r: int(r[0]), reverse=True)[:100]
        f.transform = lambda rows, request: copy.deepcopy(first)
        f.refresh(); self.assertEqual(f.view()['reason'], 'PAGINATION')
        self.assertEqual(len(f.requests), 2)

    def test_invalid_rows_fail_closed(self):
        for index, value in [(0, True), (1, 'NaN'), (2, '90'), (4, '1e9999'), (5, '-1'), (8, 1)]:
            with self.subTest(index=index, value=value):
                f = Fake(); f.rows['1H'][-1][index] = value; f.refresh()
                self.assertEqual(f.view()['status'], 'UNKNOWN'); self.assertFalse(f.docs)

    def test_conflicting_duplicate_rejected(self):
        f = Fake(); f.rows['1H'].append(candle(BASE + 299 * HOUR, '101')); f.refresh()
        self.assertEqual(f.view()['reason'], 'DUPLICATE_BAR')

    def test_oversize_response_rejected(self):
        f = Fake(); f.transform = lambda rows, request: rows * 2; f.refresh()
        self.assertEqual(f.view()['reason'], 'RESPONSE_BOUND')

    def test_clock_skew_and_regression(self):
        f = Fake(); f.provider.refresh(ClockSample(f.now - 6_000, f.now, f.mono, f.mono), f.now, f.mono)
        self.assertEqual(f.view()['reason'], 'CLOCK_SKEW')
        f = Fake(); f.refresh(); f.advance(); clock = ClockSample(f.now - HOUR, f.now - HOUR, f.mono, f.mono)
        f.provider.refresh(clock, f.now - HOUR, f.mono)
        self.assertEqual(f.view()['status'], 'UNKNOWN')

    def test_delayed_page_receipt_rejected(self):
        f = Fake(); original = f.get
        def late(request):
            p = original(request); return Page(p.rows, p.received_at_ms + 30_000, p.monotonic_ms + 30_000)
        f.provider = HTFProvider(late, f.persist); f.refresh()
        self.assertEqual(f.view()['reason'], 'FETCH_DEADLINE')

    def test_m15_and_h1_sma_features(self):
        f = Fake(m15=True); f.rows['15m'][-1][4] = '102'; f.refresh(); v = f.view()
        self.assertEqual(len(f.document()['series']['15m']), 20)
        self.assertAlmostEqual(v['features']['htf_15m_sma20_gap_bps'], (102 / 100.1 - 1) * 10_000)
        self.assertEqual(v['features']['htf_1h_sma20_gap_bps'], 0)

    def test_replay_resolves_exact_document_no_network(self):
        f = Fake(m15=True); f.refresh(); v = f.view(); calls = len(f.requests)
        self.assertEqual(replay_view(v, f.docs.get), v)
        self.assertEqual(len(f.requests), calls)

    def test_missing_or_tampered_context_never_ready(self):
        f = Fake(); f.refresh(); v = f.view()
        with self.assertRaises(HTFError): replay_view(v, lambda key: None)
        d = f.document(); d['series']['1H'][-1]['close'] = '101'
        with self.assertRaises(HTFError): replay_view(v, lambda key: d)

    def test_replay_recomputes_features_and_rejects_future_availability(self):
        f = Fake(); f.refresh(); v = f.view(); v['features']['htf_1h_sma20_gap_bps'] = 123
        with self.assertRaises(HTFError): replay_view(v, f.docs.get)
        v = f.view(); v['asof_local_ms'] -= 1000
        with self.assertRaises(HTFError): replay_view(v, f.docs.get)

    def test_unknown_replay_keeps_none_features(self):
        f = Fake(); v = f.view(); self.assertEqual(replay_view(v, lambda key: None), v)
        v['features']['htf_1h_sma20_gap_bps'] = 1
        with self.assertRaises(HTFError): replay_view(v, lambda key: None)

    def test_attach_does_not_mutate_m1(self):
        f = Fake(); f.refresh(); original = {'at': f.now / 1000, 'server_time': f.now / 1000,
            'raw': {'synthetic': True}, 'features': {'sma_gap_bps': 7.25}, 'sha256': 'old'}
        saved = copy.deepcopy(original); attached = attach_context(original, f.view(), f.docs.get)
        self.assertEqual(original, saved); self.assertEqual(attached['features']['sma_gap_bps'], 7.25)
        self.assertEqual(attached['m1_snapshot_sha256'], 'old')
        self.assertEqual(attached['sha256'], digest(attached['raw']))
        self.assertNotIn('series', attached['raw']['htf_context'])

    def test_attach_wrong_snapshot_time_rejected(self):
        f = Fake(); f.refresh(); s = {'at': 1, 'server_time': 1, 'raw': {}, 'features': {}, 'sha256': 'old'}
        with self.assertRaises(HTFError): attach_context(s, f.view(), f.docs.get)

    def test_context_memory_bound_over_forward_bars(self):
        f = Fake(h1=32); f.refresh()
        for hour in range(300, 310):
            f.advance(HOUR); f.rows['1H'].append(candle(BASE + hour * HOUR)); f.refresh()
            self.assertEqual(len(f.document()['series']['1H']), 32)
        self.assertEqual(f.provider.retained_bars, 32)

    def test_unknown_dto_rejects_nested_untyped_field(self):
        f = Fake(); v = f.view(); v['latest_h1_close_ms'] = {'private': 'must not pass'}
        with self.assertRaises(HTFError): replay_view(v, lambda key: None)

    def test_feature_boolean_is_not_numeric(self):
        f = Fake(); f.refresh(); v = f.view(); v['features']['htf_1h_sma20_gap_bps'] = False
        with self.assertRaises(HTFError): replay_view(v, f.docs.get)

    def test_decimal_strings_preserve_all_source_digits(self):
        f = Fake(); raw = '123456789012345678.1234567890123456'; f.rows['1H'][-1][5] = raw
        f.refresh(); self.assertEqual(f.document()['series']['1H'][-1]['volume'], raw)

    def test_confirmation_regression_invalidates_known_bar(self):
        f = Fake(); f.refresh(); f.advance(); f.rows['1H'][-1][8] = '0'; f.refresh()
        self.assertEqual(f.view()['reason'], 'REVISION')

    def test_context_roundtrip_self_contained(self):
        f = Fake(m15=True); f.refresh(); doc = f.document()
        self.assertEqual(htf.unpack_context(htf.pack_context(doc)), doc)
        original = {'at': f.now / 1000, 'server_time': f.now / 1000, 'raw': {'synthetic': True}, 'features': {'x': 7}, 'sha256': 'old'}
        attached = attach_context(original, f.view(), f.docs.get)
        self.assertEqual(htf.restore_context(original, attached['raw']['htf_context']), attached)

    def test_compressed_unknown_private_keys_rejected(self):
        f = Fake(); f.refresh(); doc = f.document(); doc['series']['1H'][0]['private'] = 'forbidden'
        raw = json.dumps(doc, sort_keys=True, separators=(',', ':')).encode()
        envelope = {'encoding': 'zlib-base64', 'sha256': digest(doc), 'data': base64.b64encode(zlib.compress(raw)).decode()}
        with self.assertRaises(HTFError): htf.unpack_context(envelope)

    def test_compression_bomb_and_trailing_stream_rejected(self):
        for raw in [b'x' * (htf.MAX_CONTEXT_BYTES + 1)]:
            envelope = {'encoding': 'zlib-base64', 'sha256': 'a'*64, 'data': base64.b64encode(zlib.compress(raw)).decode()}
            with self.assertRaises(HTFError): htf.unpack_context(envelope)
        f = Fake(); f.refresh(); p = htf.pack_context(f.document())
        p['data'] = base64.b64encode(base64.b64decode(p['data']) + zlib.compress(b'{}')).decode()
        with self.assertRaises(HTFError): htf.unpack_context(p)

    def test_missing_clock_request_bracket_unknown(self):
        f = Fake(); f.provider.refresh(ClockSample(f.now, f.now, f.mono), f.now, f.mono)
        self.assertEqual(f.view()['reason'], 'CLOCK_UNCERTAINTY'); self.assertFalse(f.requests)

    def test_clock_rtt_upper_bound_delays_availability(self):
        f = Fake(); c = ClockSample(f.now - 500, f.now, f.mono, f.mono - 1000)
        f.provider.refresh(c, f.now, f.mono)
        f.now += 2; f.mono += 2
        self.assertEqual(f.view()['reason'], 'NOT_YET_AVAILABLE')
        f.advance(1000); self.assertEqual(f.view()['status'], 'READY')

    def test_g1_integer_seconds_conservative_and_no_implicit_bootstrap_trade(self):
        f = Fake(); f.refresh()
        def snapshot():
            s = {'at': f.now / 1000, 'server_time': f.now / 1000, 'raw': {}, 'features': {}, 'sha256': 'old'}
            return attach_context(s, f.view(), f.docs.get)
        self.assertEqual(htf.g1_view(snapshot())['reason'], 'G1_SECOND_BOUNDARY')
        f.advance(1000); g = htf.g1_view(snapshot())
        self.assertEqual(g['status'], 'READY'); self.assertFalse(g['h1_signal_eligible'])
        self.assertEqual(len(g['records']), 256)
        self.assertTrue(all(b['received_at'] <= g['now'] for b in g['records']))
        self.assertTrue(all(p['knowledge'] == 'initial_history' for p in g['provenance']))

    def test_g1_new_bar_fresh_and_cached_hour_not_signal(self):
        f = Fake(); f.refresh(); f.advance(HOUR); f.rows['1H'].append(candle(BASE + 300 * HOUR)); f.refresh(); f.advance(1000)
        s = {'at': f.now / 1000, 'server_time': f.now / 1000, 'raw': {}, 'features': {}, 'sha256': 'old'}
        self.assertTrue(htf.g1_view(attach_context(s, f.view(), f.docs.get))['h1_signal_eligible'])
        f.advance(180_000); f.refresh(); self.assertFalse(f.view()['h1_signal_eligible'])

    def test_request_only_fixed_public_paths(self):
        from trading_lab.causal_htf import Request
        for r in [Request('/api/v5/trade/order', '1H', 1), Request('/api/v5/market/candles', '1m', 1),
                  Request('/api/v5/market/candles', '1H', 101), Request('/api/v5/market/candles', '1H', 1, -1)]:
            with self.assertRaises(HTFError): r.params()

    def test_real_m1_builder_legacy_parity_and_optional_fault_isolation(self):
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root))
        from trading_lab.arena_market import build_snapshot
        now = BASE / 1000 + 30 * 60 + 10
        raw = {'clock': [{'ts': str(int(now * 1000))}],
            'instrument': [{'instId': 'BTC-USDT-SWAP', 'state': 'live', 'ctValCcy': 'BTC', 'settleCcy': 'USDT',
                            'ctVal': '0.01', 'ctMult': '1', 'lotSz': '0.01', 'minSz': '0.01'}],
            'ticker': [{'instId': 'BTC-USDT-SWAP', 'bidPx': '100', 'askPx': '100.1', 'ts': str(int(now*1000)), 'bidSz': '100', 'askSz': '100'}],
            'funding': [{'instId': 'BTC-USDT-SWAP', 'fundingRate': '0.0001', 'fundingTime': str(int((now+1000)*1000)), 'ts': str(int(now*1000))}],
            'candles': [candle(BASE + i*60_000) for i in range(30)]}
        expected = build_snapshot(raw, now)
        self.assertEqual(htf.build_with_optional_htf(raw, now, build_snapshot), expected)
        for bad in [{'secret': 'never export'}, {'view': [], 'context': None}, float('nan')]:
            with self.subTest(bad=str(type(bad))):
                enriched = htf.build_with_optional_htf({**raw, 'htf_context': bad}, now, build_snapshot)
                self.assertEqual(enriched['bid'], expected['bid'])
                self.assertEqual({k: enriched['features'][k] for k in expected['features']}, expected['features'])
                self.assertEqual(enriched['htf']['status'], 'UNKNOWN')
                self.assertNotIn('never export', json.dumps(enriched))

        # Full actual M1 builder -> enriched raw -> fresh rebuild, no live transport.
        f = Fake(); f.refresh(); f.advance(1000); now = f.now / 1000
        raw['clock'][0]['ts'] = str(f.now)
        raw['ticker'][0]['ts'] = str(f.now)
        raw['funding'][0].update(ts=str(f.now), fundingTime=str(f.now + 1_000_000))
        last_close = f.now // 60_000 * 60_000
        raw['candles'] = [candle(last_close - (30-i)*60_000) for i in range(30)]
        base = build_snapshot(raw, now)
        enriched = attach_context(base, f.view(), f.docs.get)
        rebuilt = htf.build_with_optional_htf(enriched['raw'], now, build_snapshot)
        self.assertEqual(rebuilt, enriched)
        self.assertEqual(htf.g1_view(rebuilt)['status'], 'READY')

    def test_clock_forward_discontinuity_blocks_context(self):
        f = Fake(); f.refresh()
        jumped = ClockSample(f.now + 5000, f.now + 5000, f.mono, f.mono)
        v = f.provider.view(jumped, f.now + 5000, f.mono)
        self.assertEqual(v['reason'], 'CLOCK_DISCONTINUITY')

    def test_due_blocks_extra_fetch_until_interval_and_after_fatal(self):
        f = Fake(); self.assertTrue(f.provider.due(f.mono)); f.refresh()
        self.assertFalse(f.provider.due(f.mono)); f.advance(); self.assertTrue(f.provider.due(f.mono))
        f.rows['1H'][-1][4] = '102'; f.refresh(); f.advance()
        self.assertFalse(f.provider.due(f.mono))

    def test_no_network_needed_for_entire_data_path(self):
        from unittest.mock import patch
        with patch('socket.create_connection', side_effect=AssertionError('network forbidden')):
            f = Fake(); f.refresh(); f.advance(1000)
            s = {'at': f.now/1000, 'server_time': f.now/1000, 'raw': {}, 'features': {}, 'sha256': 'old'}
            a = attach_context(s, f.view(), f.docs.get)
            self.assertEqual(htf.g1_view(htf.restore_context(s, a['raw']['htf_context']))['status'], 'READY')


if __name__ == '__main__':
    unittest.main(verbosity=2)
