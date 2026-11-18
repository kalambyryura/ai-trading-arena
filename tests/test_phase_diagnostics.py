"""No replay/transports. Run with --candidate for the isolated overlay."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCOPE = ROOT
from trading_lab.battle_review import build_review, digest, verify_bundle
from trading_lab.battle_debrief import build_debrief, load_debrief, recorded_quotes, render_html
from tests.test_cycle_diagnostics import state_for, event
from tests.test_battle_debrief import fixture, seal, BASE


def state():
    s = state_for(); s.update(mode='OKX_DEMO', budget_usdt=100, frames=[])
    return s


def leaf(passed=False):
    return {'feature':'return_5m_bps','op':'gte','expected':20,'actual':10,'passed':passed}


class DiagnosticsTests(unittest.TestCase):
    def test_canonical_events_are_not_decision_mirrors(self):
        s=state(); b=s['bots'][0]
        for code in ['entry_checks','rules_decision','entry_checks','entry_rechecked']:
            event(s,b,code,{'checks':[leaf()], 'bar_time_ms':1790769600000})
        row=build_review(s)['participants'][0]
        p=row['phase_diagnostics']['phases']['entry']
        self.assertEqual((p['evaluations'],p['unique_closed_bars'],p['failed_predicates'][0]['false_evaluations']),(2,1,2))
        self.assertEqual(p['rechecks'],1)
        self.assertEqual(row['funnel']['failed_features']['return_5m_bps'],4)
        self.assertIn('LEGACY',row['funnel']['failed_features_scope'])

    def test_phases_and_false_exit_mean_no_trigger(self):
        s=state(); b=s['bots'][0]
        event(s,b,'position_checks',{'kind':'add','checks':[leaf()], 'exit_checks':[leaf()], 'bar_time_ms':1790769600000})
        event(s,b,'hold_checks',{'checks':[leaf(None)], 'bar_time_ms':1790769600000})
        event(s,b,'exit_trigger',{'exit_checks':[leaf()], 'bar_time_ms':1790769600000})
        phases=build_review(s)['participants'][0]['phase_diagnostics']['phases']
        self.assertEqual(phases['add']['evaluations'],1)
        self.assertEqual(phases['hold']['failed_predicates'][0]['unknown_evaluations'],1)
        self.assertEqual(phases['exit']['evaluations'],2)  # distinct observations, not same snapshot/time
        self.assertIn('FALSE',phases['exit']['interpretation'])

    def test_exit_trigger_same_snapshot_is_not_counted_twice(self):
        s=state(); b=s['bots'][0]
        data={'kind':'add','snapshot_sha256':'a'*64,'exit_checks':[leaf()]}
        event(s,b,'position_checks',data); event(s,b,'exit_trigger',data)
        self.assertEqual(build_review(s)['participants'][0]['phase_diagnostics']['phases']['exit']['evaluations'],1)

    def test_exit_trigger_mirror_is_deduplicated_in_both_recording_orders(self):
        for check_code in ('position_checks','exit_checks'):
            with self.subTest(check_code=check_code):
                s=state(); b=s['bots'][0]
                data={'kind':'hold','snapshot_sha256':'a'*64,'checks':[leaf()], 'exit_checks':[leaf()]}
                event(s,b,'exit_trigger',data); event(s,b,check_code,data)
                phases=build_review(s)['participants'][0]['phase_diagnostics']['phases']
                self.assertEqual(phases['exit']['evaluations'],1)

    def test_http_review_reads_snapshot_context_without_external_calls(self):
        import http.client
        import threading
        from unittest.mock import patch
        from tests.test_server_delivery import battle_fixture
        from trading_lab.arena import Arena
        from trading_lab.exchange_lab import ExchangeLab
        from trading_lab.match_journal import append_events, checkpoint
        from trading_lab.web_panel import Panel, Server
        journal=battle_fixture(); journal['protocol_version']=8
        event(journal,journal['bots'][0],'entry_checks',{'checks':[leaf()],'snapshot_sha256':'a'*64})
        at=datetime.fromisoformat(journal['events'][-1]['at'].replace('Z','+00:00')).timestamp()
        journal['latest_market']={'at':at+1}
        with tempfile.TemporaryDirectory(dir=SCOPE) as temp:
            root=Path(temp)
            no_call=lambda *a, **kw: self.fail('No exchange, market, model or process calls')
            arena=Arena(root,background=False,feed_factory=no_call)
            factory=lambda r,w,a: ExchangeLab(r,w,a,background=False,api_factory=no_call,feed_factory=no_call,jev_factory=no_call)
            server=Server(Panel(root,launcher=no_call),0,arena=arena,exchange_factory=factory)
            append_events(server.exchange.path/(journal['id']+'.events.jsonl'),journal,0)
            saved=checkpoint(journal); server.exchange.load=lambda identity, **kwargs: deepcopy(saved)
            quote=server.exchange.path/(journal['id']+'.snapshots.jsonl')
            quote.write_text(json.dumps({'at':at,'sha256':'a'*64,'bar_close_ms':int(at*1000),
                'raw':{'authorization':'PRIVATE','ticker':[{'bidPx':'100','askPx':'102'}]}})+'\n',encoding='utf-8')
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def get():
                connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
                try:
                    connection.request('GET','/api/review/exchange/'+journal['id'])
                    response=connection.getresponse();return response.status,response.read()
                finally: connection.close()
            try:
                status,body=get();self.assertEqual(status,200)
                review=json.loads(body);self.assertTrue(verify_bundle(review)['verified'])
                connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
                try:  # R2.1: the season's contract travels beside the hashed review, so a BTC review needs no new key
                    connection.request('GET','/api/review/exchange/'+journal['id']);response=connection.getresponse();response.read()
                    self.assertEqual(response.getheader('X-Season-Instrument'),'BTC-USDT-SWAP');self.assertNotIn('instrument',review['match'])
                    from trading_lab.league import example_only  # R2.1.11 (D141 p. 2): beside the review, never inside it
                    self.assertEqual(response.getheader('X-Season-Example-Only'),'1' if example_only(saved) else '0');self.assertNotIn('example_only',review['match'])
                finally: connection.close()
                for kind,expected in (('example','1'),('model','0')):  # R2.1.12 (R2111RC2-TEST-HEADER): both answers, both routes
                    for recipe in saved['strategies']:
                        if recipe.get('id')!='cash': recipe['author']={**(recipe.get('author') or {}),'kind':kind}
                    for route in ('review','replay'):
                        connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
                        try:
                            connection.request('GET','/api/'+route+'/exchange/'+journal['id']);response=connection.getresponse();response.read()
                            self.assertEqual((route,kind,response.status,response.getheader('X-Season-Example-Only')),(route,kind,200,expected))
                        finally: connection.close()
                self.assertEqual(review['participants'][0]['phase_diagnostics']['phases']['entry']['known_unique_closed_bars'],1)
                self.assertNotIn(b'PRIVATE',body)
                with patch('trading_lab.battle_debrief.recorded_quotes',side_effect=OSError('PRIVATE_FILE_DETAIL')):
                    status,body=get();self.assertEqual(status,200);self.assertNotIn(b'PRIVATE_FILE_DETAIL',body)
                    fallback=json.loads(body);self.assertEqual(fallback['diagnostics_context']['market_snapshots'],'UNAVAILABLE')
                    self.assertTrue(verify_bundle(fallback)['verified'])
                quote.unlink()
                status,body=get();self.assertEqual(status,200)
                self.assertEqual(json.loads(body)['diagnostics_context']['market_snapshots'],'NOT_RECORDED')
            finally:
                server.shutdown();thread.join(timeout=3);server.server_close()

    def test_zip_debrief_recomputes_bar_counts_from_retained_market_context(self):
        import zipfile
        from unittest.mock import patch
        from tests.test_server_delivery import battle_fixture
        from tests.test_workshop import raw_public
        from trading_lab.battle_export import create_archive, verify_archive
        journal=battle_fixture();at=1790769600
        for e in journal['events']:
            if e['code']=='entry_checks':
                e['data']['snapshot_sha256']='a'*64;e['data'].pop('bar_time_ms',None)
        journal['latest_market']={'at':at}
        with tempfile.TemporaryDirectory(dir=SCOPE) as temp:
            root=Path(temp);path=root/'capture.jsonl';archive_path=root/'battle.zip'
            path.write_text(json.dumps({'at':at,'sha256':'a'*64,'raw':raw_public(at)})+'\n',encoding='utf-8')
            def without_context(state,**kwargs):
                kwargs.pop('snapshot_context',None);kwargs.pop('snapshot_context_status',None)
                return build_review(state,**kwargs)
            # Reproduce an older archived projection even after root enriches new exports.
            with patch('trading_lab.battle_export.build_review',side_effect=without_context):
                with create_archive(journal,snapshots_path=path) as archive:
                    archive_path.write_bytes(archive.read())
            self.assertTrue(verify_archive(archive_path)['verified'])
            with zipfile.ZipFile(archive_path) as archive:
                bundle=json.loads(archive.read('battle-review.json'))
                self.assertIsNone(bundle['participants'][0]['phase_diagnostics']['phases']['entry']['unique_closed_bars'])
                market=json.loads(archive.read('market.jsonl'))
                self.assertEqual(market['source_snapshot_sha256'],'a'*64)
                self.assertEqual(market['bar_close_ms'],int(at*1000))
            report=load_debrief(archive_path)
            self.assertEqual(report['participants'][0]['phase_diagnostics']['phases']['entry']['unique_closed_bars'],1)

    def test_missing_bar_is_unknown_not_event_minute(self):
        s=state();event(s,s['bots'][0],'entry_checks',{'checks':[leaf()]})
        p=build_review(s)['participants'][0]['phase_diagnostics']['phases']['entry']
        self.assertIsNone(p['unique_closed_bars']);self.assertEqual(p['unknown_bar_evaluations'],1)

    def test_recorded_snapshot_resolves_closed_bar_without_leaking_raw(self):
        s=state();event(s,s['bots'][0],'entry_checks',{'checks':[leaf()],'snapshot_sha256':'a'*64})
        context=[{'at':1790769600,'snapshot_sha256':'a'*64,'bar_close_ms':1790769600000,'raw':{'authorization':'PRIVATE'}}]
        bundle=build_review(s,snapshot_context=context)
        self.assertEqual(bundle['participants'][0]['phase_diagnostics']['phases']['entry']['unique_closed_bars'],1)
        self.assertNotIn('PRIVATE',json.dumps(bundle))
        self.assertTrue(verify_bundle(bundle)['verified'])

    def test_gap_endpoints_positions_and_unknown_inside(self):
        s=state();b=s['bots'][0]
        event(s,b,'financial_observation',{'financial':{'position_btc':'.001'}})
        gap=event(s,None,'market_unavailable')
        start=datetime.fromisoformat(gap['at'].replace('Z','+00:00')).timestamp()
        context=[{'at':start-5,'snapshot_sha256':'a'*64},{'at':start+15,'snapshot_sha256':'b'*64}]
        bundle=build_review(s,snapshot_context=context)
        g=bundle['market_gaps'][0]
        self.assertEqual(g['known_snapshot_interval_seconds'],20)
        self.assertIsNone(g['outage_duration_seconds']);self.assertEqual(g['inside_status'],'UNKNOWN')
        self.assertEqual(g['positions_at_gap'][0]['status'],'OPEN')
        self.assertEqual(g['recovery']['at'],start+15)
        self.assertTrue(bundle['history_complete'])
        html=render_html(build_debrief(bundle))
        self.assertIn('2026-09-30T11:59:56.000000Z',html)
        self.assertNotIn('epoch s',html)

    def test_unrecovered_gap_does_not_invent_flat(self):
        s=state(); event(s,None,'market_unavailable')
        g=build_review(s)['market_gaps'][0]
        self.assertIsNone(g['recovery']);self.assertEqual(g['positions_at_gap'][0]['status'],'UNKNOWN')

    def test_debrief_can_enrich_old_bundle_without_resealing_it(self):
        bundle=fixture();before=deepcopy(bundle)
        bundle['events'][0]['data']['checks']=[leaf()];seal(bundle);before=deepcopy(bundle)
        report=build_debrief(bundle)
        self.assertEqual(report['participants'][0]['phase_diagnostics']['phases']['entry']['evaluations'],1)
        self.assertEqual(bundle,before)
        self.assertIn('Перевірки за фазами',render_html(report))

    def test_intent_latency_and_exit_reason_are_visible(self):
        bundle=fixture()
        bundle['events'][0].update(code='entry_intent',data={})
        bundle['events'][6]['code']='close_intent';bundle['events'][6]['data']={'exit_reason':'rules_exit'}
        seal(bundle);report=build_debrief(bundle);trade=report['participants'][0]['trades'][0]
        self.assertEqual(trade['timing']['entry_intent_to_fill_seconds'],1)
        self.assertEqual(trade['timing']['close_intent_to_fill_seconds'],1)
        self.assertIn('Намір входу → виконання',render_html(report))
        self.assertEqual(trade['observed_realized_net_usdt'],'14.9515')

    def test_missing_intent_stays_unknown(self):
        trade=build_debrief(fixture())['participants'][0]['trades'][0]
        self.assertIsNone(trade['timing']['entry_intent_to_fill_seconds'])

    def test_quote_reader_keeps_only_allowed_diagnostic_context(self):
        with tempfile.TemporaryDirectory(dir=SCOPE) as temp:
            p=Path(temp)/'quotes.jsonl'
            p.write_text(json.dumps({'at':BASE,'bar_close_ms':int(BASE*1000),'sha256':'a'*64,
                'raw':{'authorization':'PRIVATE','ticker':[{'bidPx':'100','askPx':'102'}]}})+'\n',encoding='utf-8')
            q=recorded_quotes(p,BASE)[0]
            self.assertEqual(q['bar_close_ms'],int(BASE*1000));self.assertEqual(q['snapshot_sha256'],'a'*64)
            self.assertNotIn('PRIVATE',json.dumps(q))

    def test_iso_millisecond_watermark_keeps_its_precision_bucket_only(self):
        boundary=datetime.fromtimestamp(BASE+1.892,timezone.utc).isoformat(timespec='milliseconds')
        with tempfile.TemporaryDirectory(dir=SCOPE) as temp:
            p=Path(temp)/'quotes.jsonl'
            p.write_text('\n'.join(json.dumps({'at':at,'raw':{'ticker':[{'bidPx':'100','askPx':'102'}]}})
                for at in [BASE+1.8915,BASE+1.8923008,BASE+1.893]),encoding='utf-8')
            quotes=recorded_quotes(p,boundary)
            self.assertEqual(len(quotes),2)
            self.assertEqual(quotes[-1]['boundary_precision'],'ISO_MILLISECOND_BUCKET')
            self.assertEqual(len(recorded_quotes(p,BASE+1.892)),1)  # numeric means exact

    def test_unknown_g1_phase_is_not_misclassified_or_rejected(self):
        s=state();b=s['bots'][0]
        event(s,b,'position_checks',{'kind':'new_g1_phase','checks':[leaf()]})
        event(s,b,'g1_setup_checks',{'checks':[leaf()]})
        review=build_review(s)
        self.assertTrue(verify_bundle(review)['verified'])
        d=review['participants'][0]['phase_diagnostics']
        self.assertEqual(d['unclassified_position_checks'],1)
        self.assertEqual(len(d['unclassified_check_event_seqs']),2)
        self.assertTrue(all(p['evaluations']==0 for p in d['phases'].values()))

    def test_completed_nine_cycle_fixture_is_projection_only(self):
        folder=ROOT/'runtime/arena_postbattle_2026_10_26/common'
        if not (folder/'battle-review.json').is_file(): self.skipTest('optional local read-only completed fixture')
        source=json.loads((folder/'battle-review.json').read_text(encoding='utf-8'))
        original=json.loads((folder/'debrief.json').read_text(encoding='utf-8'))
        quotes=recorded_quotes(folder/'source'/f'{source["match"]["id"]}.snapshots.jsonl',source['match']['ended_at'])
        report=build_debrief(source,quotes)
        self.assertEqual(report['market_samples'],2690)
        self.assertEqual(sum(len(b['trades']) for b in report['participants']),9)
        self.assertEqual([p['costs'] for p in report['participants']],[p['costs'] for p in original['participants']])
        sol=report['participants'][0]['phase_diagnostics']['phases']['entry']
        self.assertEqual(sol['unique_closed_bars'],285)
        self.assertEqual(next(c for c in sol['failed_predicates'] if c['feature']=='return_5m_bps')['false_evaluations'],468)
        astra=report['participants'][1]['phase_diagnostics']['phases']['entry']
        self.assertEqual(next(c for c in astra['failed_predicates'] if c['feature']=='return_5m_bps')['false_evaluations'],351)
        g=report['market_gaps'][0]
        self.assertAlmostEqual(g['known_snapshot_interval_seconds'],19.73155117034912)
        self.assertEqual([p['status'] for p in g['positions_at_gap']],['FLAT','OPEN','OPEN','OPEN'])
        self.assertIsNone(g['outage_duration_seconds'])


if __name__=='__main__': unittest.main()
