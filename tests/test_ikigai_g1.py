"""Bounded offline fixtures: controlled candidate modules + real PAPER accounting.
No public feed, exchange transport, credentials, paid model or market dataset.
"""
import importlib.util
import json
from dataclasses import asdict,replace
from decimal import Decimal
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.dont_write_bytecode=True
from trading_lab import ikigai_g1_reference as ref, causal_htf as htf, ikigai_g1 as g1
from trading_lab import arena_strategies as recipes, arena_engine as engine
HERE=Path(__file__).resolve().parent
PROJECT=HERE.parent
from tests import test_ikigai_reference as original_tests

def payload():
    return {'name':'Yurii G1 fixture','author':{'name':'Yurii','kind':'human','model':'',
        'submitted_at':'2026-10-26','prompt':'','attempts':1,'edited_by':'AI formalization'},
        'description':'For Yurii, IKIGAI geometric adaptation; synthetic fixture only',
        'original_text':'Immutable G1; no financial result or qualification claim',
        'confirmed':True,'strategy_schema_version':3,'rules':json.loads(json.dumps(g1.RULES))}


def snapshot(now,bar=None,bootstrap=False,history=None,bid=108.30,ask=108.32,depth=100,local_offset=0):
    if history is not None:
        rows=history
    elif bar:
        index=bar.open_ts//3600
        base=original_tests.fixture()
        mirror=index<76 and abs(bar.close-(220-base[index].close))<.00001
        rows=original_tests.fixture(mirror=mirror)[:index]+[bar]
    else:
        rows=[]
    snap={'at':now+local_offset,'server_time':now,'quote_ts':now*1000,'bid':bid,'ask':ask,'mid':(bid+ask)/2,
        'bid_btc':depth,'ask_btc':depth,'lot_btc':.01,'min_btc':.01,
        'bar_close_ms':int(now//60)*60000,'sha256':'f'*64,'features':{'spread_bps':(ask-bid)/((bid+ask)/2)*10000},
        'funding_rate':0,'funding_at':None,'funding_time_ms':None,
        'raw':{'instrument':[{'instId':'BTC-USDT-SWAP','ctValCcy':'BTC','settleCcy':'USDT',
                            'tickSz':'.01','ctVal':'.01','ctMult':'1','lotSz':'1','minSz':'1'}]}}
    initial=(72*3600+1)*1000
    number=lambda x:format(Decimal(str(x)).normalize(),'f')
    parsed=[]
    for b in rows:
        is_initial=b.close_ts*1000<=initial
        received=initial if is_initial else int(b.received_at*1000)
        available=initial+local_offset*1000 if is_initial else received+local_offset*1000
        parsed.append({'open_ms':b.open_ts*1000,'close_ms':b.close_ts*1000,
          **{k:number(getattr(b,k))for k in ('open','high','low','close')},
          'volume':'1','vol_ccy':'1','vol_quote':'1','available_at_ms':available,
          'confirmed_at_ms':available,'observed_server_ms':received,
          'available_server_ms_lower':received,'available_server_ms_upper':received,
          'knowledge':'initial_history'if is_initial else'forward_confirmed'})
    doc={'schema':htf.SCHEMA,'instrument':htf.INSTRUMENT,'h1_bars':len(rows),'include_m15':False,
         'initial_knowledge_at_ms':initial+local_offset*1000,'initial_server_cutoff_ms':initial,
         'created_at_ms':int((now+local_offset)*1000),'series':{'1H':parsed}}
    latest=parsed[-1]['close_ms']
    view={'schema':htf.VIEW_SCHEMA,'instrument':htf.INSTRUMENT,'status':'READY','reason':None,
          'context_sha256':htf.digest(doc),'asof_local_ms':int(snap['at']*1000),
          'asof_server_ms':int(now*1000),'asof_monotonic_ms':int(now*1000),
          'last_refresh_monotonic_ms':int(now*1000),'max_cache_age_ms':120000,
          'bar_lag_grace_ms':120000,'latest_h1_close_ms':latest,
          'h1_signal_eligible':latest>initial and 0<=now*1000-latest<=120000,
          'features':htf._features(doc)}
    return htf.attach_context(snap,view,lambda key:doc if htf.digest(doc)==key else None)


def prepared(mirror=False,local_offset=0):
    r=recipes.validate_strategy(payload())
    bars=original_tests.fixture(mirror=mirror)
    start=72*3600-600+local_offset
    state=engine.new_season([r],100,18000,start,'candidate-source-epoch')
    bot=state['bots'][0]
    warm=snapshot(72*3600+1,history=bars[:72],bootstrap=True,local_offset=local_offset)
    g1.advance(state,bot,r,warm,engine.emit)
    for b in bars[72:75]:
        snap=snapshot(b.received_at,bar=b,local_offset=local_offset)
        g1.advance(state,bot,r,snap,engine.emit)
    return state,bot,r,bars


def signal_and_fill(mirror=False,local_offset=0):
    state,bot,r,bars=prepared(mirror,local_offset)
    b=bars[75];bid,ask=(111.68,111.70)if mirror else(108.30,108.32)
    snap=snapshot(b.received_at,bar=b,bid=bid,ask=ask,local_offset=local_offset)
    engine.tick(state,snap)
    follow=snapshot(b.received_at+2,bar=b,bid=bid,ask=ask,local_offset=local_offset)
    engine.tick(state,follow)
    return state,bot,r,bars,follow


class BridgeTests(unittest.TestCase):
    def test_portable_payload_hash_roundtrip_and_second_validation(self):
        from trading_lab.strategy_contract import payload_of
        original=recipes.validate_strategy(payload())
        portable=payload_of(original)
        self.assertEqual(recipes.fingerprint(portable),original['recipe_sha256'])
        second=recipes.validate_strategy(json.loads(json.dumps(portable)),version=2)
        self.assertEqual(second['recipe_sha256'],original['recipe_sha256'])
        self.assertEqual(payload_of(second),portable)
        self.assertEqual(second['status'],'draft')
        self.assertNotIn('readiness',portable)

    def test_transient_unknown_open_preserves_coverage_and_recovers_exit(self):
        state,bot,r,bars,snap=signal_and_fill();history=list(bars)
        for index in (76,77):
            b=ref.Bar(index*3600,(index+1)*3600+1,106,106.05,105.95,106)
            history.append(b)
            engine.tick(state,snapshot(b.received_at,history=history,bid=106,ask=106.02))
        self.assertEqual(bot['_ikigai_g1']['failed_closes'],1)
        known=json.loads(json.dumps(bot['_ikigai_g1']['detector']))
        inputs=dict(bot['_ikigai_g1']['bar_inputs'])
        unknown=snapshot(b.received_at+1,history=history,bid=106,ask=106.02)
        unknown['raw'].pop('htf_context')
        engine.tick(state,unknown)
        # JSON restore while HTF is unknown must keep the actual position and
        # already-known close count; recovery adds no entry/setup or past fill.
        state=json.loads(json.dumps(state));bot=state['bots'][0]
        engine.tick(state,snapshot(b.received_at+3,history=history,bid=106,ask=106.02))
        self.assertNotIn('_g1_error',bot)
        self.assertTrue(bot['_ikigai_g1']['bootstrapped'])
        self.assertEqual(bot['_ikigai_g1']['failed_closes'],1)
        self.assertEqual(bot['_ikigai_g1']['bar_inputs'],inputs)
        self.assertEqual(bot['_ikigai_g1']['detector']['used_setup_ids'],known['used_setup_ids'])
        self.assertIsNone(bot['_ikigai_g1']['ready_signal'])
        b=ref.Bar(78*3600,79*3600+1,106,106.05,105.95,106);history.append(b)
        engine.tick(state,snapshot(b.received_at,history=history,bid=106,ask=106.02))
        self.assertEqual(bot['_ikigai_g1']['failed_closes'],2)
        self.assertIsNotNone(bot['_pending'])
        self.assertEqual(bot['_pending']['reason'],'g1_failed_breakout')
        self.assertEqual(bot['_total_entries'],1)

    def test_unknown_recovery_does_not_accept_revision_or_disable_quote_stop(self):
        state,bot,r,bars,snap=signal_and_fill();history=list(bars)
        for index in (76,77):
            b=ref.Bar(index*3600,(index+1)*3600+1,106,106.05,105.95,106)
            history.append(b)
            engine.tick(state,snapshot(b.received_at,history=history,bid=106,ask=106.02))
        known=json.loads(json.dumps(bot['_ikigai_g1']['detector']))
        unknown=snapshot(b.received_at+1,history=history,bid=106,ask=106.02)
        unknown['raw'].pop('htf_context');engine.tick(state,unknown)
        self.assertEqual(bot['_ikigai_g1']['failed_closes'],1)
        self.assertEqual(json.loads(json.dumps(bot['_ikigai_g1']['detector']))['bars'],known['bars'])
        revised=history[:-1]+[replace(history[-1],high=106.06)]
        engine.tick(state,snapshot(b.received_at+3,history=revised,bid=106,ask=106.02))
        self.assertIn('revision',bot['_g1_error'])
        self.assertEqual(bot['_ikigai_g1']['failed_closes'],1)
        self.assertIsNone(bot['_ikigai_g1']['ready_signal'])
        self.assertEqual(bot['_total_entries'],1)
        stop=bot['_ikigai_g1']['cycle']['stop']
        engine.tick(state,snapshot(b.received_at+5,history=revised,bid=stop-.01,ask=stop+.01))
        self.assertEqual(bot['_pending']['reason'],'g1_stop_loss')


    def test_fixed_schema3_validation_and_stable_payload_hash(self):
        a=recipes.validate_strategy(payload());b=recipes.validate_strategy(payload())
        self.assertEqual(a['recipe_sha256'],b['recipe_sha256'])
        self.assertEqual(a['status'],'draft')
        self.assertEqual(a['strategy_schema_version'],3)
        for mutate in [lambda p:p['rules'].update(cooldown_seconds=600),
                       lambda p:p['rules'].update(builtin='arbitrary_python'),
                       lambda p:p['rules'].update(max_entries=True),
                       lambda p:p.update(strategy_schema_version=3.0)]:
            p=payload();mutate(p)
            with self.assertRaises(ValueError):recipes.validate_strategy(p)

    def test_paper_long_nextquote_actual_existing_accounting(self):
        state,bot,r,bars,snap=signal_and_fill()
        self.assertGreater(bot['position_btc'],0)
        self.assertEqual(bot['entries'],1)
        self.assertEqual(bot['_total_entries'],1)
        self.assertGreater(bot['fees_usdt'],0)
        self.assertAlmostEqual(bot['_bank'],100-bot['fees_usdt'],places=9)
        self.assertEqual(bot['_ikigai_g1']['cycle']['target'],118.15)
        fills=[e for e in state['events']if e['code']=='paper_entry_fill']
        self.assertEqual(len(fills),1)
        self.assertEqual(bot['_ikigai_g1']['attempts'],1)

    def test_paper_short_mirror(self):
        state,bot,r,bars,snap=signal_and_fill(True)
        self.assertLess(bot['position_btc'],0)
        self.assertEqual(bot['status'],'SHORT')

    def test_no_samequote_fill_and_restart_does_not_repeat(self):
        state,bot,r,bars=prepared();b=bars[75]
        snap=snapshot(b.received_at,bar=b)
        engine.tick(state,snap)
        self.assertEqual(bot['position_btc'],0)
        self.assertIsNotNone(bot['_pending'])
        restored=json.loads(json.dumps(state,allow_nan=False))
        engine.tick(restored,snapshot(b.received_at+2,bar=b))
        self.assertGreater(restored['bots'][0]['position_btc'],0)
        second=json.loads(json.dumps(restored))
        engine.tick(second,snapshot(b.received_at+4,bar=b))
        self.assertEqual(second['bots'][0]['_total_entries'],1)
        self.assertEqual(second['bots'][0]['entries'],1)

    def test_cutoff_between_signal_and_quote_no_fill(self):
        state,bot,r,bars=prepared();b=bars[75]
        state['_end']=b.received_at+1
        engine.tick(state,snapshot(b.received_at,bar=b))
        engine.tick(state,snapshot(b.received_at+2,bar=b))
        self.assertEqual(bot['position_btc'],0)
        self.assertIsNone(bot['_ikigai_g1']['reserved'])
        self.assertEqual(bot['_ikigai_g1']['attempts'],1)
        self.assertTrue(bot['_ikigai_g1']['terminal_intents'])

    def test_stop_between_signal_and_quote(self):
        state,bot,r,bars=prepared();b=bars[75]
        engine.tick(state,snapshot(b.received_at,bar=b))
        engine.request_stop(state,b.received_at+1,bot['id'])
        engine.tick(state,snapshot(b.received_at+2,bar=b))
        self.assertEqual(bot['position_btc'],0)
        self.assertIsNone(bot['_ikigai_g1']['reserved'])

    def test_unknown_htf_cancels_old_intent(self):
        state,bot,r,bars=prepared();b=bars[75]
        engine.tick(state,snapshot(b.received_at,bar=b))
        snap=snapshot(b.received_at+2,bar=b);snap['raw'].pop('htf_context')
        engine.tick(state,snap)
        self.assertEqual(bot['position_btc'],0)
        self.assertFalse(bot['_ikigai_g1']['bootstrapped'])
        self.assertIsNone(bot['_pending'])

    def test_absolute_stop_skips_generic_bps_and_close_remains(self):
        state,bot,r,bars,snap=signal_and_fill();b=bars[75]
        # Generic take_profit1000bps would NOT trigger; G1 absolute target must.
        target=bot['_ikigai_g1']['cycle']['target']
        engine.tick(state,snapshot(snap['at']+2,bar=b,bid=target+.01,ask=target+.03))
        self.assertEqual(bot['_pending']['kind'],'close')
        self.assertEqual(bot['_pending']['reason'],'g1_take_profit')
        engine.tick(state,snapshot(snap['at']+4,bar=b,bid=target+.01,ask=target+.03))
        self.assertEqual(bot['position_btc'],0)
        self.assertEqual(bot['trades'],1)
        self.assertIsNone(bot['_ikigai_g1']['cycle'])
        self.assertEqual(bot['_ikigai_g1']['detector']['cooldown_until'],snap['at']+4+14400)
        self.assertEqual(bot['_total_entries'],1)

    def test_drain_does_not_close_position(self):
        state,bot,r,bars,snap=signal_and_fill();b=bars[75]
        state['_end']=snap['at']+1
        engine.tick(state,snapshot(snap['at']+2,bar=b))
        self.assertEqual(state['status'],'DRAINING')
        self.assertNotEqual(bot['position_btc'],0)
        self.assertIsNone(bot['_pending'])

    def test_partial_close_preserves_levels_and_flat_cooldown(self):
        state,bot,r,bars,snap=signal_and_fill();b=bars[75]
        before=json.loads(json.dumps(bot['_ikigai_g1']['cycle']))
        engine.request_stop(state,snap['at']+1,bot['id'])
        engine.tick(state,snapshot(snap['at']+2,bar=b))
        engine.tick(state,snapshot(snap['at']+4,bar=b,depth=.1))
        self.assertGreater(bot['position_btc'],0)
        self.assertEqual(bot['_ikigai_g1']['cycle']['stop'],before['stop'])
        self.assertEqual(bot['_ikigai_g1']['cycle']['first_fill_at'],before['first_fill_at'])
        self.assertEqual(bot['_ikigai_g1']['detector']['cooldown_until'],0)
        engine.tick(state,snapshot(snap['at']+6,bar=b))
        self.assertEqual(bot['position_btc'],0)
        self.assertGreater(bot['_ikigai_g1']['detector']['cooldown_until'],snap['at'])

    def test_checkpoint_corruption_and_metadata_drift_no_entry(self):
        state,bot,r,bars=prepared();b=bars[75]
        bot['_ikigai_g1']['detector']['cooldown_until']=-1
        engine.tick(state,snapshot(b.received_at,bar=b))
        self.assertEqual(bot['position_btc'],0)
        self.assertIn('checksum',bot['_g1_error'])
        state,bot,r,bars=prepared();snap=snapshot(b.received_at,bar=b)
        snap['raw']['instrument'][0]['tickSz']='.1'
        engine.tick(state,snap)
        self.assertEqual(bot['position_btc'],0)
        self.assertIn('epoch',bot['_g1_error'])

    def test_bootstrap_has_no_retroactive_signal(self):
        state,bot,r,bars=prepared()
        self.assertFalse(any(e['code']=='g1_entry_signal' for e in state['events']))
        self.assertEqual(bot['position_btc'],0)

    def test_duplicate_bar_not_reprocessed_and_revision_blocked(self):
        state,bot,r,bars=prepared();b=bars[74]
        before=json.loads(json.dumps(bot['_ikigai_g1']['detector']))
        g1.advance(state,bot,r,snapshot(b.received_at+1,bar=b),engine.emit)
        self.assertEqual(json.loads(json.dumps(bot['_ikigai_g1']['detector'])),before)
        revised=replace(b,high=b.high+.01)
        g1.advance(state,bot,r,snapshot(b.received_at+2,bar=revised),engine.emit)
        self.assertIn('revision',bot['_g1_error'])
        self.assertFalse(g1.entry_gates(state,bot,r,snapshot(b.received_at+2,bar=revised)))

    def test_live_demo_sharedjev_training_failclosed_before_other_calls(self):
        state,bot,r,bars=prepared()
        for mode in ['OKX_LIVE','OKX_DEMO','LIVE']:
            with self.assertRaises(g1.G1Error):g1.assert_paper({**state,'mode':mode})
        with self.assertRaises(g1.G1Error):g1.assert_paper({**state,'execution_mode':'shared_jev'})
        from trading_lab import exchange_engine as exchange
        with patch.object(exchange.exchange_execution,'validate_request',side_effect=AssertionError('must not reach policy/model')):
            with self.assertRaises(g1.G1Error):exchange.create([r],[], 'arena_live',100,300,0,'hash')
        with self.assertRaises(g1.G1Error):exchange.candidate({}, {}, r, {}, 0)
        with self.assertRaises(g1.G1Error):exchange.entry_body({}, {}, r, {}, 'forbidden')
        from trading_lab import strategy_training as training
        with self.assertRaises(ValueError):training.run_trial(r)

    def test_sharedjev_rejected_before_manager_gate_or_acquire(self):
        from trading_lab import arena
        # A synthetic ready catalogue exercises the existing manager guard only;
        # no real recipe status/qualification or runtime file is changed.
        r=recipes.validate_strategy(payload());r['status']='ready'
        manager=arena.Arena.__new__(arena.Arena)
        manager.lock=threading.RLock();manager.session=None
        manager.check_generation=lambda _:None
        manager.catalogue=lambda:[r]
        with patch.object(manager,'acquire',side_effect=AssertionError('must not acquire')):
            with self.assertRaisesRegex(g1.G1Error,'before_gate'):
                manager.start(0,[r['id']],100,18000,'shared_jev')

    def test_tick_rejects_nonpaper_before_mutating_state(self):
        state,bot,r,bars=prepared();state['mode']='OKX_DEMO'
        before=json.dumps(state,sort_keys=True)
        with self.assertRaises(g1.G1Error):engine.tick(state,snapshot(bars[75].received_at,bar=bars[75]))
        self.assertEqual(json.dumps(state,sort_keys=True),before)

    def test_separate_exchange_and_local_clock_fill_cutoff_and_cooldown(self):
        # Synthetic +/-4s clock domains, within provider nominal skew bound.
        for offset in (-4,4):
            with self.subTest(offset=offset):
                state,bot,r,bars,snap=signal_and_fill(local_offset=offset);b=bars[75]
                self.assertGreater(bot['position_btc'],0)
                cycle=bot['_ikigai_g1']['cycle']
                self.assertEqual(cycle['first_fill_at']-cycle['first_fill_at_exchange'],offset)
                target=cycle['target'];now=b.received_at+4
                engine.tick(state,snapshot(now,bar=b,bid=target+.01,ask=target+.03,local_offset=offset))
                engine.tick(state,snapshot(now+2,bar=b,bid=target+.01,ask=target+.03,local_offset=offset))
                self.assertEqual(bot['position_btc'],0)
                self.assertEqual(bot['_ikigai_g1']['detector']['cooldown_until'],now+2+14400)
                self.assertEqual(bot['_next_decision'],now+2+offset+14400)
                state,bot,r,bars=prepared(local_offset=offset)
                state['_end']=b.received_at+1+offset
                engine.tick(state,snapshot(b.received_at,bar=b,local_offset=offset))
                engine.tick(state,snapshot(b.received_at+2,bar=b,local_offset=offset))
                self.assertEqual(bot['position_btc'],0)

    def test_quote_rechecks_are_one_consumed_attempt(self):
        for case in ('expired','lag','spread','chase','minimum'):
            with self.subTest(case=case):
                state,bot,r,bars=prepared();b=bars[75]
                engine.tick(state,snapshot(b.received_at,bar=b))
                now=b.received_at+(61 if case=='expired'else 10 if case=='lag'else 2)
                snap=snapshot(now,bar=b,
                    bid=109.3 if case=='chase'else 108.30,
                    ask=109.32 if case=='chase'else 108.5 if case=='spread'else 108.32,
                    depth=.001 if case=='minimum'else 100)
                if case=='lag':snap['quote_ts']=(now-6)*1000
                engine.tick(state,snap)
                self.assertEqual(bot['position_btc'],0)
                self.assertEqual(bot['_ikigai_g1']['attempts'],1)
                self.assertIsNone(bot['_pending'])
                self.assertEqual({v['status']for v in bot['_ikigai_g1']['terminal_intents'].values()},{'NO_FILL'})
                engine.tick(state,snapshot(now+2,bar=b))
                self.assertEqual(bot['_ikigai_g1']['attempts'],1)
                self.assertEqual(bot['_total_entries'],0)

    def test_frozen_setup_restart_and_bootstrap_fullhistory_no_retroentry(self):
        state,bot,r,bars=prepared()
        frozen=json.loads(json.dumps(bot['_ikigai_g1']['detector']['pending']))
        self.assertIsNotNone(frozen)
        restored=json.loads(json.dumps(state))
        bot=restored['bots'][0]
        engine.tick(restored,snapshot(bars[74].received_at+1,bar=bars[74]))
        self.assertEqual(json.loads(json.dumps(bot['_ikigai_g1']['detector']['pending'])),frozen)
        cold=engine.new_season([r],100,18000,bars[75].received_at-10,'cold-epoch')
        engine.tick(cold,snapshot(bars[75].received_at,history=bars))
        engine.tick(cold,snapshot(bars[75].received_at+2,history=bars))
        self.assertEqual(cold['bots'][0]['position_btc'],0)
        self.assertIsNone(cold['bots'][0]['_ikigai_g1']['detector']['pending'])
        self.assertFalse(any(e['code']=='g1_entry_signal'for e in cold['events']))

    def test_fractional_availability_waits_for_conservative_exchange_second(self):
        state,bot,r,bars=prepared();b=replace(bars[75],received_at=bars[75].received_at+.25)
        first=snapshot(b.received_at,bar=b)
        view=htf.g1_view(first)
        self.assertEqual(view['status'],'UNKNOWN')
        self.assertEqual(view['reason'],'G1_SECOND_BOUNDARY')
        engine.tick(state,first)
        self.assertEqual(bot['position_btc'],0)
        self.assertIsNone(bot['_pending'])
        # UNKNOWN recovers via conservative structure-only bootstrap, no late
        # backdated setup. This may miss a trade and is an explicit pilot policy.
        later=snapshot(b.received_at+.75,bar=b)
        self.assertEqual(htf.g1_view(later)['records'][-1]['received_at'],b.received_at+.75)
        engine.tick(state,later)
        self.assertEqual(bot['position_btc'],0)
        self.assertTrue(bot['_ikigai_g1']['bootstrapped'])

    def test_corrupt_open_checkpoint_can_close_paper_without_reopening(self):
        state,bot,r,bars,snap=signal_and_fill();b=bars[75]
        bot['_ikigai_g1']['detector']['cooldown_until']=-1
        evidence=json.loads(json.dumps(bot['_ikigai_g1']))
        engine.tick(state,snapshot(b.received_at+4,bar=b))
        self.assertEqual(bot['_pending']['reason'],'g1_owned_paper_state_unreadable')
        engine.tick(state,snapshot(b.received_at+6,bar=b))
        self.assertEqual(bot['position_btc'],0)
        self.assertEqual(bot['trades'],1)
        self.assertEqual(json.loads(json.dumps(bot['_ikigai_g1'])),evidence)
        self.assertEqual(bot['_g1_error'],'g1_close_checkpoint_unreadable')
        engine.tick(state,snapshot(b.received_at+8,bar=b))
        self.assertEqual(bot['_total_entries'],1)

    def test_failed_breakout_uses_two_fully_postfill_h1_closes(self):
        state,bot,r,bars,snap=signal_and_fill()
        history=list(bars)
        for index,count in [(76,0),(77,1),(78,2)]:
            b=ref.Bar(index*3600,(index+1)*3600+1,106,106.05,105.95,106)
            history.append(b)
            engine.tick(state,snapshot(b.received_at,history=history,bid=106,ask=106.02))
            self.assertEqual(bot['_ikigai_g1']['failed_closes'],count)
            if count<2:self.assertIsNone(bot['_pending'])
        self.assertEqual(state['status'],'DRAINING')
        self.assertEqual(bot['_pending']['reason'],'g1_failed_breakout')
        engine.tick(state,snapshot(history[-1].received_at+2,history=history,bid=106,ask=106.02))
        self.assertEqual(bot['position_btc'],0)

    def test_authored_24h_hold_uses_local_fill_age_and_closes_after_cutoff(self):
        state,bot,r,bars,snap=signal_and_fill(local_offset=4)
        fill=bot['_ikigai_g1']['cycle']['first_fill_at']
        self.assertIsNone(g1.exit_reason(state,bot,r,{**snap,'at':fill+86400-1}))
        self.assertEqual(g1.exit_reason(state,bot,r,{**snap,'at':fill+86400}),'g1_max_hold')
        # Build contiguous neutral future H1 solely for the actual engine hook.
        history=list(bars)
        for i in range(76,100):history.append(ref.Bar(i*3600,(i+1)*3600+1,109,109.05,108.95,109))
        now=snap['server_time']+86400
        engine.tick(state,snapshot(now,history=history,local_offset=4,bid=109,ask=109.02))
        self.assertEqual(state['status'],'DRAINING')
        self.assertEqual(bot['_pending']['reason'],'g1_max_hold')

    def test_compressed_context_strict_hash_asof_and_no_duplicate_projection(self):
        state,bot,r,bars=prepared();b=bars[75]
        clean=snapshot(b.received_at,bar=b)
        self.assertNotIn('g1_htf',clean)
        self.assertNotIn('records',clean['htf'])
        for case in ('asof','hash','unknownkey'):
            snap=json.loads(json.dumps(clean))
            if case=='asof':snap['at']+=1
            elif case=='hash':snap['raw']['htf_context']['context']['sha256']='0'*64
            else:snap['raw']['htf_context']['context']['secret']='forbidden'
            with self.subTest(case=case):
                with self.assertRaises(ValueError):g1.provider_view(snap)


def suite():
    result=unittest.TestSuite()
    result.addTests(unittest.defaultTestLoader.loadTestsFromModule(original_tests))
    result.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(BridgeTests))
    return result


if __name__=='__main__':
    result=unittest.TextTestRunner(verbosity=2).run(suite())
    sys.exit(not result.wasSuccessful())
