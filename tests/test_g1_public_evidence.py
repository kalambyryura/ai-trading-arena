"""Small pure synthetic projection fixtures. No workers, feeds, models or accounts."""
import copy
from dataclasses import asdict
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
import zipfile

from trading_lab import g1_public_evidence as helper, battle_review as candidate, arena_engine, ikigai_g1
from tests import test_battle_review as legacy_tests, test_ikigai_reference as reference_tests
from trading_lab import ikigai_g1_reference as reference


def events(mirror=False):return reference_tests.run(reference_tests.fixture(mirror=mirror)).events
def trace(kind='SETUP',mirror=False):return copy.deepcopy(next(e for e in events(mirror)if e['kind']==kind))
def evidence(raw=None):
    raw=trace()if raw is None else raw
    return {'trace':raw,'exchange_known_at':raw['at'],'simulated':True}
def project(raw=None):return helper.g1_public_evidence(evidence(raw))


def synthetic_bundle():
    payload={'name':'G1 SYNTHETIC projection fixture','author':{'name':'Fixture','kind':'human','model':'',
             'submitted_at':'2026-10-26','prompt':'','attempts':1,'edited_by':'AI fixture'},
             'description':'Fictional projection only','original_text':'Not a trade or qualification',
             'confirmed':True,'strategy_schema_version':3,'rules':copy.deepcopy(ikigai_g1.RULES)}
    recipe=ikigai_g1.validate_payload(payload)
    state=arena_engine.new_season([recipe],100,18000,72*3600,'f'*64)
    bot=state['bots'][0]
    for raw in events():
        data=evidence(raw);data.update(uid='PRIVATE_UID',authorization='PRIVATE_KEY',raw={'prompt':'PRIVATE_PROMPT'})
        arena_engine.emit(state,raw['at']+4,bot,1,'g1_'+raw['kind'].lower(),'Fixture public trace',**data)
    # Existing model target exit code: do not invent levels missing from the event.
    arena_engine.emit(state,76*3600+5,bot,5,'exit_trigger','Fixture target only',
                     exit_reason='g1_take_profit',snapshot_sha256='b'*64,simulated=True)
    state['status']='DONE'
    return candidate.build_review(state)


class ProjectionTests(unittest.TestCase):
    def test_setup_has_named_anchors_frozen_levels_and_causal_h4(self):
        raw=trace();before=copy.deepcopy(raw);out=project(raw)
        self.assertEqual(raw,before)
        setup=out['trace']['setup']
        self.assertEqual([a['anchor_id']for a in setup['anchors']],list(helper.ANCHOR_IDS))
        self.assertEqual(setup['setup_id'],raw['setup']['setup_id'])
        self.assertEqual(setup['model_levels'],{'entry_trigger':'108.11','stop_loss':'103.34',
                         'take_profit':'118.15','b_level':'108.05'})
        self.assertEqual(setup['level_semantics'],'FROZEN_MODEL_LEVELS_NOT_FILL_OR_HOSTED_SL_PROOF')
        self.assertEqual(len(setup['context']),3)
        self.assertTrue(all(x['close_ts']<=setup['anchors'][0]['open_ts']for x in setup['context']))
        self.assertEqual(json.loads(json.dumps(out)),out)

    def test_actual_reference_long_and_short_entry_signal_retain_identifiers(self):
        for mirror in (False,True):
            raw=trace('ENTRY_SIGNAL',mirror)
            out=project(raw)['trace']
            self.assertEqual(out['kind'],'ENTRY_SIGNAL')
            self.assertEqual(out['signal']['decided_at'],raw['signal']['decided_at'])
            self.assertEqual(out['signal']['signal_bar_ts'],raw['signal']['signal_bar_ts'])
            self.assertEqual(out['signal']['setup']['side'],'short'if mirror else'long')
            self.assertEqual(out['signal']['setup']['setup_id'],raw['signal']['setup']['setup_id'])

    def test_every_known_reference_trace_kind_has_a_closed_projection(self):
        setup=trace()['setup'];signal=trace('ENTRY_SIGNAL')['signal'];at=signal['decided_at']
        pivot=copy.deepcopy(setup['anchors'][-1])
        raws=[{'kind':k,'at':at}for k in helper.NO_PAYLOAD_KINDS]
        raws += [{'kind':'AMBIGUOUS_PIVOT','at':at,'open_ts':pivot['open_ts']},
                 {'kind':'INVALIDATED','at':at,'setup_id':setup['setup_id']},
                 {'kind':'EXPIRED','at':at,'setup_id':setup['setup_id']},
                 {'kind':'PIVOT_CONFIRMED','at':at,'pivot':pivot},
                 {'kind':'PIVOT_CHAIN_RESET','at':at,'pivot':pivot},
                 {'kind':'SETUP','at':at,'setup':setup},
                 {'kind':'SETUP_REJECTED','at':at,'reason':'APEX'},
                 {'kind':'NO_ENTRY','at':at,'reason':'COOLDOWN','signal':signal},
                 {'kind':'ENTRY_SIGNAL','at':at,'reason':None,'signal':signal},
                 {'kind':'FLAT_RECONCILED','at':at,'cooldown_until':at+14400}]
        self.assertEqual({r['kind']for r in raws},set(helper.KINDS))
        for raw in raws:
            with self.subTest(kind=raw['kind']):self.assertEqual(project(raw)['trace']['kind'],raw['kind'])

    def test_all_known_rejection_reasons_and_unknown_free_text(self):
        for reason in helper.SETUP_REASONS:
            raw={'kind':'SETUP_REJECTED','at':273601,'reason':reason}
            self.assertEqual(project(raw)['trace']['reason'],reason)
        raw={'kind':'SETUP_REJECTED','at':273601,'reason':'PRIVATE_AUTHORIZATION'}
        self.assertEqual(project(raw)['trace']['reason'],'UNRECOGNIZED')
        signal=trace('ENTRY_SIGNAL')['signal']
        for reason in helper.ENTRY_REASONS:
            raw={'kind':'NO_ENTRY','at':273601,'reason':reason,'signal':signal}
            self.assertEqual(project(raw)['trace']['reason'],reason)

    def test_arbitrary_nested_private_keys_are_never_copied_or_traversed(self):
        raw=trace();raw.update(prompt='PRIVATE_PROMPT',account={'uid':'PRIVATE_UID'})
        raw['setup'].update(api_key='PRIVATE_KEY',raw={'authorization':'PRIVATE_AUTH'})
        raw['setup']['anchors'][0].update(private_id='PRIVATE_ANCHOR')
        data=evidence(raw);data.update(_ikigai_g1={'cycle':'PRIVATE_CYCLE'},uid='PRIVATE_UID')
        out=helper.g1_public_evidence(data)
        self.assertIn('setup',out['trace'])
        self.assertNotIn('PRIVATE_',json.dumps(out))
        self.assertEqual(set(out['trace']['setup']['anchors'][0]),{'anchor_id','kind','open_ts','price','confirmed_at'})

    def test_hostile_wrong_types_and_nonfinite_prices_fail_closed(self):
        values=[None,True,False,'PRIVATE_PRICE',[],{},float('nan'),float('inf'),-1,0,10**1000,1e-320]
        for value in values:
            raw=trace();raw['setup']['stop']=value
            with self.subTest(type=type(value).__name__):
                out=project(raw)
                self.assertEqual(out.get('trace_status'),'INVALID_OR_UNRECOGNIZED')
                self.assertNotIn('trace',out)
                self.assertNotIn('PRIVATE_',json.dumps(out,allow_nan=False))

    def test_anchor_context_bounds_and_public_identifier_format(self):
        for field,value in [('anchors',[]),('anchors',[{}]*257),('context',[[]]*256),
                            ('setup_id','PRIVATE_CLIENT_ORDER'),('setup_id','a'*64),('side','PRIVATE_SIDE')]:
            raw=trace();raw['setup'][field]=value
            self.assertNotIn('trace',project(raw))

    def test_causal_future_and_clock_mismatch_are_not_relabelled_as_known(self):
        raw=trace();raw['setup']['context']=list(raw['setup']['context'])
        raw['setup']['context'][-1]=(999*14400,110,100,105)
        self.assertEqual(project(raw)['trace_status'],'INVALID_OR_UNRECOGNIZED')
        raw=trace('ENTRY_SIGNAL');raw['signal']['decided_at']=raw['at']+1
        self.assertNotIn('trace',project(raw))
        data=evidence(trace());data['exchange_known_at']-=1
        self.assertNotIn('trace',helper.g1_public_evidence(data))

    def test_nonobjects_unknown_kind_and_malformed_fields_never_stringify_private_values(self):
        for value in (None,False,1,'PRIVATE',[],{'trace':'PRIVATE'},
                      {'trace':{'kind':[],'at':0}},{'trace':{'kind':'PRIVATE_KIND','at':0}},
                      {'reason':{'authorization':'PRIVATE'}},{'bars':True},
                      {'exchange_known_at':'PRIVATE_TIME'}):
            out=helper.g1_public_evidence(value)
            self.assertNotIn('PRIVATE',json.dumps(out,allow_nan=False))

    def test_warmup_unknown_and_exit_reason_envelopes_are_typed(self):
        self.assertEqual(helper.g1_public_evidence({'bars':256,'exchange_known_at':273601})['bars'],256)
        self.assertNotIn('bars',helper.g1_public_evidence({'bars':257,'exchange_known_at':273601}))
        for reason in ('g1_h1_revision','VIEW_CAUSALITY','g1_htf_unknown:STALE_PROVIDER'):
            self.assertEqual(helper.g1_public_evidence({'reason':reason})['reason'],reason)
        self.assertEqual(helper.g1_public_evidence({'reason':'g1_htf_unknown:PRIVATE_KEY'})['reason'],'UNRECOGNIZED')
        for reason in helper.EXIT_REASONS:
            self.assertEqual(helper.g1_public_evidence({'exit_reason':reason})['exit_reason'],reason)

    def test_event_code_gate_and_original_step_identifiers(self):
        raw=trace();data=evidence(raw)
        self.assertNotIn('g1_evidence',candidate.safe_data(data))
        self.assertNotIn('g1_evidence',candidate.safe_data(data,event_code='unrelated_setup'))
        event={'seq':91,'at':'2026-10-26T23:00:00+02:00','bot_id':'a'*32,
               'step':1,'code':'g1_setup','data':data}
        out=candidate.event_view(event)
        for field in ('seq','at','bot_id','step','code'):self.assertEqual(out[field],event[field])
        self.assertEqual(out['data']['g1_evidence']['trace']['setup']['setup_id'],raw['setup']['setup_id'])
        exitdata={'exit_reason':'g1_take_profit','simulated':True}
        out=candidate.safe_data(exitdata,event_code='exit_trigger')
        self.assertEqual(out['g1_evidence']['exit_reason'],'g1_take_profit')
        self.assertNotIn('model_levels',out['g1_evidence'])
        self.assertNotIn('g1_evidence',candidate.safe_data({'exit_reason':[]},event_code='exit_trigger'))


    def test_event_kind_mismatch_and_unknown_g1_codes_fail_closed(self):
        data=evidence(trace('ENTRY_SIGNAL'))
        self.assertIsNone(helper.g1_event_evidence(data,'g1_private_future_field'))
        self.assertNotIn('g1_evidence',candidate.safe_data(data,event_code='g1_private_future_field'))
        out=helper.g1_event_evidence(data,'g1_setup')
        self.assertEqual(out['trace_status'],'EVENT_KIND_MISMATCH')
        self.assertNotIn('trace',out)
        self.assertIsNone(helper.g1_event_evidence(data,[]))

    def test_g1_entry_plan_rejection_codes_survive_without_arbitrary_reasons(self):
        for reason in helper.PLAN_REASONS:
            data={'reason':reason,'simulated':True,'raw':'PRIVATE_RESPONSE'}
            out=candidate.safe_data(data,event_code='entry_cancelled')
            self.assertEqual(out['g1_evidence']['reason'],reason)
            self.assertNotIn('PRIVATE',json.dumps(out))
        self.assertNotIn('g1_evidence',candidate.safe_data({'reason':'PRIVATE_API_ERROR'},event_code='entry_cancelled'))

    def test_full_bundle_json_zip_roundtrip_retains_g1_and_integrity(self):
        bundle=synthetic_bundle()
        self.assertTrue(candidate.verify_bundle(bundle)['verified'])
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED)as archive:
            archive.writestr('battle_review.json',json.dumps(bundle,allow_nan=False))
        with zipfile.ZipFile(io.BytesIO(buffer.getvalue()))as archive:
            restored=json.loads(archive.read('battle_review.json'))
        self.assertTrue(candidate.verify_bundle(restored)['verified'])
        setup_events=[e for e in restored['events']if e['code']=='g1_setup']
        self.assertEqual(len(setup_events),1)
        e=setup_events[0];self.assertEqual(e['step'],1);self.assertIsInstance(e['seq'],int)
        self.assertEqual(e['data']['g1_evidence']['trace']['setup']['model_levels']['stop_loss'],'103.34')
        self.assertNotIn('PRIVATE_',json.dumps(restored))
        self.assertEqual(restored['match']['mode'],'PAPER')
        e['data']['g1_evidence']['trace']['setup']['stop']='1'
        with self.assertRaises(ValueError):candidate.verify_bundle(restored)


def suite():
    result=unittest.TestSuite()
    result.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(ProjectionTests))
    result.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(legacy_tests.ReviewTests))
    return result


if __name__=='__main__':
    result=unittest.TextTestRunner(verbosity=2).run(suite());sys.exit(not result.wasSuccessful())
