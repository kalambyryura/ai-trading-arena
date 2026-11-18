"""Independent wall/server/elapsed clocks. All requests use a fake transport."""
from datetime import datetime, timezone
from decimal import Decimal
import json
import unittest
from unittest.mock import patch

import test_exchange_lab as fixture
from trading_lab.exchange_lab import ExchangeLab
from trading_lab.exchange_api import ExchangeFeed
from trading_lab.okx_readonly import PreflightError
from trading_lab.strategy_contract import payload_of


class TimebaseTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.ExchangeTests(); self.fx.setUp(); self.fx.lab.close()
        self.wall_offset = 3600 * 24 * 30
        self.elapsed = 100.
        self.wall = lambda: self.fx.now + self.wall_offset
        self.mono = lambda: self.elapsed
        self.apis = []
        def api(profile):
            result = self.fx.fakes[profile['id']].api(profile['environment'])
            result.clock, result.monotonic = self.wall, self.mono
            self.apis.append(result)
            return result
        self.lab = self.fx.lab = ExchangeLab(self.fx.root, self.fx.workshop, self.fx.arena,
            clock=self.wall, monotonic=self.mono, background=False, api_factory=api,
            feed_factory=lambda a: ExchangeFeed(a, clock=lambda: self.lab.clock()),
            jev_factory=lambda *_: self.fail('No paid/model gate in RULES_ONLY'))
        self.network = patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden'))
        self.network.start()

    def tearDown(self):
        self.network.stop(); self.fx.tearDown()

    def advance(self, seconds):
        self.fx.now += seconds; self.elapsed += seconds

    def start(self, *, enter=False, hold=21600):
        recipe = payload_of(self.fx.workshop.version(self.fx.version)['strategy'])
        recipe['rules'].update(max_hold_seconds=hold, max_entries=1,
            stop_loss_bps=500, take_profit_bps=1000)
        recipe['rules']['entry'] = {'mode':'all','conditions':[{'feature':'spread_bps','op':'lt','value':5 if enter else 0}]}
        recipe['rules']['exit'] = {'mode':'any','conditions':[]}
        identity = self.fx.workshop.add_version(self.fx.workshop.data['generation'], recipe)['id']
        result = self.lab.start(self.lab.data['generation'], 'workshop_demo', [identity], ['demo'], 100, 18000,
                                execution_mode='rules_only')
        state = self.lab.load(result['id'])
        self.lab.prepare(state, self.lab.chosen_profiles(['demo']), False)
        return state

    def test_signatures_use_okx_time_despite_wrong_wall_and_jump_during_sample(self):
        api = self.lab.api_factory(self.lab.chosen_profiles(['demo'])[0])
        original = api.transport
        def jump(request):
            result = original(request)
            if request.full_url.endswith('/public/time'): self.wall_offset -= 3600 * 24 * 90
            return result
        api.transport = jump
        result = api.calibrate_clock()
        self.assertAlmostEqual(api.server_now(), self.fx.now)
        self.assertFalse(result['windows_clock_modified'])
        seen = []
        def capture(request):
            seen.append(request.get_header('Ok-access-timestamp')); return original(request)
        api.transport = capture
        self.wall_offset += 3600 * 24 * 365
        self.advance(2)
        api.get('/api/v5/account/config', private=True)
        stamp = datetime.fromisoformat(seen[0].replace('Z','+00:00')).timestamp()
        self.assertAlmostEqual(stamp, self.fx.now, places=2)
        self.advance(31)
        with self.assertRaisesRegex(PreflightError,'expired'): api.server_now()
        # The executor refreshes an expired anchor with a public GET before signing.
        api.get('/api/v5/account/config',private=True)
        self.assertAlmostEqual(datetime.fromisoformat(seen[-1].replace('Z','+00:00')).timestamp(), self.fx.now, places=2)
        self.advance(31)
        api.transport = lambda _: (_ for _ in ()).throw(PreflightError('network_error'))
        with self.assertRaisesRegex(PreflightError,'network_error'): api.get('/api/v5/account/config',private=True)
        self.assertIsNone(api.server_anchor)

    def test_a_bills_window_renews_a_stale_anchor_instead_of_failing_every_check(self):
        # The other PC on R2.1.1: after a request slower than 10 s the anchor was over 30 s old, and bills() renewed only
        # a missing one, so every account check of the stopped Demo failed with server_clock_calibration_expired.
        api = self.lab.api_factory(self.lab.chosen_profiles(['demo'])[0])
        api.calibrate_clock()
        self.advance(31)
        with self.assertRaisesRegex(PreflightError, 'expired'): api.server_now()
        seen = []
        original = api.transport
        def capture(request):
            seen.append(request.full_url); return original(request)
        api.transport = capture
        api.bills(int((self.fx.now - 600) * 1000))
        self.assertTrue(seen[0].endswith('/api/v5/public/time'))  # renewed before the window end is taken
        end = [u for u in seen if '/account/bills' in u][0]
        self.assertIn('end=' + str(int(self.fx.now * 1000)), end)
        self.assertAlmostEqual(api.server_now(), self.fx.now, places=2)

    def test_a_start_that_waited_for_the_lock_still_synchronizes_the_session(self):
        # Demo 9b6289 review: Start and Resume read server_now() after waiting for the lab lock; past 30 s the anchor
        # expired and Start failed with a misleading balance error. The session timebase extrapolates for minutes.
        api = self.lab.api_factory(self.lab.chosen_profiles(['demo'])[0])
        api.calibrate_clock()
        self.advance(45)
        with self.assertRaisesRegex(PreflightError, 'expired'): api.server_now()
        self.assertAlmostEqual(api.session_server_now(), self.fx.now, places=3)
        self.advance(300)
        with self.assertRaisesRegex(PreflightError, 'expired'): api.session_server_now()

    def test_slow_or_invalid_okx_sample_still_blocks(self):
        api = self.lab.api_factory(self.lab.chosen_profiles(['demo'])[0])
        original = api.transport
        def slow(request):
            result = original(request); self.advance(6); return result
        api.transport = slow
        with self.assertRaisesRegex(PreflightError,'unreliable_clock_sample'): api.calibrate_clock()
        self.assertIsNone(api.server_anchor)

    def test_five_hours_are_exact_under_forward_backward_and_date_jumps(self):
        state = self.start(); start = self.elapsed
        self.assertAlmostEqual(datetime.fromisoformat(state['started_at']).timestamp(), self.fx.now, places=2)
        self.assertAlmostEqual(self.lab.resources[state['id']]['deadline'] - start, 18000)
        self.lab.step(state)
        for offset in (3600, -7200, 86400 * 100, -86400 * 100):
            self.wall_offset = offset; self.advance(10); self.lab.step(state)
            self.assertEqual(state['status'],'RUNNING')
            self.assertFalse(state['incomplete'])
            self.assertFalse(state['admission_closed_at'])
            timing = self.lab.public(state)['timing']
            self.assertAlmostEqual(timing['admission_remaining_seconds'],18000-(self.elapsed-start))
            self.assertAlmostEqual(timing['server_now_ms'],self.fx.now*1000)
            self.assertEqual(self.lab.public(state)['clock_policy'],'okx_utc_monotonic_v1')
        self.advance(17999 - (self.elapsed-start)); self.lab.step(state)
        self.assertEqual(state['status'],'RUNNING')
        self.advance(1); self.lab.step(state)
        self.assertEqual(state['status'],'DONE')
        self.assertIsNotNone(state['admission_closed_at'])
        self.assertEqual(len(self.fx.fakes['demo'].orders),0)

    def test_position_survives_cutoff_and_exits_on_authored_hold_timer(self):
        state = self.start(enter=True); self.lab.step(state)
        self.assertNotEqual(self.fx.fakes['demo'].units,0)
        self.wall_offset -= 86400 * 80
        self.advance(18000); self.lab.step(state)
        self.assertEqual(state['status'],'DRAINING')
        self.assertNotEqual(self.fx.fakes['demo'].units,0)
        self.assertFalse(state['bots'][0]['stop_requested'])
        self.wall_offset += 86400 * 160
        self.advance(3600); self.lab.step(state)
        self.advance(1); self.lab.step(state)
        self.assertEqual(state['status'],'DONE')
        self.assertEqual(self.fx.fakes['demo'].units,0)
        self.assertEqual(len(self.fx.fakes['demo'].orders),2)

    def test_failed_time_lookup_never_reserves_or_arms_a_run(self):
        before = self.lab.data['generation']
        with patch.object(self.lab,'api_factory',side_effect=PreflightError('network_error')):
            with self.assertRaises(Exception): self.start()
        self.assertEqual(self.lab.data['generation'],before)
        self.assertEqual(self.lab.data['runs'],[])

    def test_restart_recovery_reanchors_without_new_entry_or_paid_call(self):
        self.recover_after_restart()

    def test_legacy_journal_recovery_accepts_new_clock_without_reopening_admission(self):
        self.recover_after_restart(legacy=True)

    def recover_after_restart(self, *, legacy=False):
        state = self.start(enter=True); self.lab.step(state)
        if legacy:
            from trading_lab.arena_strategies import fingerprint
            state.pop('clock_policy');state['match_manifest'].pop('clock_policy')
            state['match_manifest_sha256']=fingerprint(state['match_manifest'])
            self.lab.save(state)
        self.lab.close()
        self.wall_offset -= 3600 * 48
        # Simulate process restart with a different monotonic epoch and no in-memory anchor.
        self.elapsed = 3.
        self.lab = self.fx.lab = ExchangeLab(self.fx.root,self.fx.workshop,self.fx.arena,
            clock=self.wall,monotonic=self.mono,background=False,
            api_factory=lambda p:self.make_recovery_api(p),
            feed_factory=lambda a:ExchangeFeed(a,clock=lambda:self.lab.clock()),
            jev_factory=lambda *_:self.fail('Recovery must not call Jev'))
        self.lab.resume(self.lab.data['generation'],state['id'])
        restored=self.lab.load(state['id']);self.lab.prepare(restored,self.lab.chosen_profiles(['demo']),True)
        self.lab.step(restored);self.advance(1);self.lab.step(restored)
        self.assertEqual(restored['status'],'DONE')
        self.assertEqual(self.fx.fakes['demo'].units,0)
        self.assertEqual(len(self.fx.fakes['demo'].orders),2)

    def test_inconsistent_server_sample_blocks_entries_without_moving_deadline(self):
        state=self.start(enter=True)
        resource=self.lab.resources[state['id']]
        deadline=resource['deadline'];snapshot=resource['feed'].snapshot()
        snapshot['server_time']+=3600
        with patch.object(resource['feed'],'snapshot',return_value=snapshot):self.lab.step(state)
        self.assertEqual(resource['deadline'],deadline)
        self.assertEqual(len(self.fx.fakes['demo'].orders),0)
        self.assertTrue(any(e['code']=='market_unavailable' and e['data']['reason']=='session_clock_source_disagreement'
                            for e in state['events']))

    def test_time_policy_is_in_export(self):
        from trading_lab.battle_export import match_settings
        from trading_lab.battle_review import build_review
        state=self.start()
        self.assertEqual(match_settings(state)['clock_policy'],'okx_utc_monotonic_v1')
        review=build_review(state)
        self.assertEqual(review['match']['clock_policy'],'okx_utc_monotonic_v1')
        clock_event=next(e for e in review['events'] if e['code']=='exchange_clock_synchronized')
        self.assertEqual(float(clock_event['data']['server_minus_local_seconds']),-self.wall_offset)
        self.assertIs(clock_event['data']['windows_clock_modified'],False)
        self.assertNotIn('FAKE_KEY',json.dumps(review))

    def test_invalid_server_values_and_regressed_monotonic_are_rejected(self):
        from trading_lab.exchange_clock import ExchangeClock
        clock=ExchangeClock(self.wall,self.mono)
        for value in (float('nan'),float('inf'),0,-1,253402300800):
            with self.assertRaisesRegex(PreflightError,'invalid_server_time'):clock.synchronize(value)
        clock.synchronize(self.fx.now);self.elapsed-=1
        with self.assertRaisesRegex(PreflightError,'session_monotonic_regression'):clock.now()

    def make_recovery_api(self, profile):
        api=self.fx.fakes[profile['id']].api(profile['environment'])
        api.clock,api.monotonic=self.wall,self.mono
        return api


if __name__=='__main__':unittest.main()
