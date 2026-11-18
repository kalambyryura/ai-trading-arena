"""Scoped source overlay; fake/temp only. --baseline intentionally runs RED."""
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
from tools import run_paper_package as runner
from tools.server_transfer import assert_finished, HANDOFF, TransferError
from trading_lab.exchange_lab import ExchangeLab
from trading_lab.arena_strategies import ArenaError,catalogue_defaults,fingerprint
from tests.test_server_delivery import cold_fixture,write_json


class EndProbe(Exception):pass
class StartFailed(Exception):pass


class PackageFixTests(unittest.TestCase):
    def temp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);return Path(temp.name)

    def package(self,existing=False):
        root=self.temp();(root/'trading_lab').mkdir();(root/'runtime/arena_v4').mkdir(parents=True)
        (root/'trading_lab/stub.py').write_bytes(b'# no executable package imports in this fixture\n')
        roster={'strategies':[deepcopy(catalogue_defaults()[0])],'seasons':['a'*32] if existing else [],
                'active_id':'a'*32 if existing else None,'generation':0}
        write_json(root/'runtime/arena_v4/manifest.json',roster)
        if existing:write_json(root/'runtime/arena_v4'/('a'*32+'.json'),{'fixture':True})
        package={'schema':runner.SCHEMA,'mode':'PAPER','execution_mode':'rules','budget_usdt_per_bot':100,
                 'admission_options_seconds':[18000],'files':{'trading_lab/stub.py':hashlib.sha256((root/'trading_lab/stub.py').read_bytes()).hexdigest()},
                 'recipes':{s['id']:s['recipe_sha256'] for s in roster['strategies']},'catalogue_sha256':fingerprint(roster['strategies']),
                 'source_content_sha256':'e'*64}
        write_json(root/'paper-package.json',package);return root

    def fake_run(self,root,states=('RUNNING',),view_only=False,fail_start=False,fail_export=False):
        import trading_lab.arena as arena_module
        import trading_lab.web_panel as panel_module
        from tools import export_paper_observation
        roster=json.loads((root/'runtime/arena_v4/manifest.json').read_text())
        calls={'starts':0,'exports':[],'start_marker_seen':False}
        state={'id':'a'*32,'status':states[0],'started_at':'2026-10-26T00:00:00Z','end_at':'2026-10-26T05:00:00Z',
               'duration_seconds':18000,'incomplete':states[0]=='PAUSED','_event_seq':1,'code_sha256':'f'*64}
        class Arena:
            lock=threading.RLock();data=roster
            owner=None if states[0] in {'PAUSED','DONE'} else object()
            session=state if roster['seasons'] else None
            def shown_status(self,s):return s['status']
            def start(self,*args,**kwargs):
                calls['starts']+=1;calls['start_marker_seen']=(root/'runtime/portable-paper-once.json').is_file()
                if fail_start:raise StartFailed()
                self.session=state
        arena=Arena()
        class Server:
            def __init__(self,*a,**k):self.arena=arena
            def __enter__(self):return self
            def __exit__(self,*a):return False
            def serve_forever(self,**kw):raise AssertionError('No real server thread')
            def shutdown(self):pass
        class Thread:
            def __init__(self,*a,**kw):pass
            def start(self):pass
        count=0
        def sleep(seconds):
            nonlocal count
            count+=1
            if count>=len(states):raise EndProbe()
            state['status']=states[count];state['_event_seq']+=1
            arena.owner=None if state['status'] in {'PAUSED','DONE'} else object()
        def exported(*a):
            calls['exports'].append(state['status'])
            if fail_export:raise OSError('FAKE_IO_ERROR')
            return {'verified':True}
        with ExitStack() as stack:
            stack.enter_context(patch.object(arena_module,'__file__',str(root/'trading_lab/arena.py')))
            stack.enter_context(patch.object(panel_module,'Panel',side_effect=lambda root:SimpleNamespace(root=root)))
            stack.enter_context(patch.object(panel_module,'Server',Server))
            stack.enter_context(patch.object(runner.threading,'Thread',Thread))
            stack.enter_context(patch.object(runner.time,'sleep',side_effect=sleep))
            stack.enter_context(patch.object(export_paper_observation,'export',side_effect=exported))
            stack.enter_context(patch.dict(runner.os.environ,{},clear=True))
            stack.enter_context(patch('sys.stdout',new=io.StringIO()))
            stack.enter_context(patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('No network')))
            try:runner.run(root,duration=18000,view_only=view_only)
            except (EndProbe,StartFailed):pass
        return calls

    def test_cold_transfer_orphan_active_nonflat_and_flat_done_are_rejected(self):
        for folder in ('exchange_v1','arena_v4'):
            root=self.temp();cold_fixture(root)
            for status,qty in [('RUNNING','.001'),('DONE','0')]:
                write_json(root/'runtime'/folder/('a'*32+'.json'),{'status':status,'bots':[{'id':'fake','position_btc':qty}]})
                with self.assertRaises(TransferError):assert_finished(root)

    def test_cold_transfer_orphan_segment_and_snapshot_evidence_are_rejected(self):
        for suffix in ('.events.jsonl','.snapshots.jsonl.gz','.events.segments'):
            root=self.temp();cold_fixture(root)
            p=root/'runtime/exchange_v1'/('A'*32+suffix)
            if suffix.endswith('segments'):p.mkdir()
            else:p.write_text('{}',encoding='utf-8')
            with self.assertRaises(TransferError):assert_finished(root)

    def test_cold_transfer_known_flat_and_non_run_metadata_remain_valid(self):
        root=self.temp();cold_fixture(root)
        write_json(root/'runtime/exchange_v1/manifest.json',{'runs':['a'*32]})
        write_json(root/'runtime/exchange_v1'/('a'*32+'.json'),{'status':'DONE','bots':[{'id':'fake','position_btc':'0'}]})
        write_json(root/'runtime/exchange_v1/metadata.json',{'fixture':True})
        assert_finished(root)

    def test_handoff_blocks_backend_before_mutation_factories_or_worker(self):
        root=self.temp();(root/HANDOFF).parent.mkdir(parents=True);(root/HANDOFF).write_text('{}',encoding='utf-8')
        lab=object.__new__(ExchangeLab);lab.root=root;lab.lock=threading.RLock();lab.closed=False;lab.data={'generation':0}
        lab.save=lambda *a,**k:self.fail('No journal writes');lab.api_factory=lambda *a:self.fail('No API factory')
        lab.chosen_profiles=lambda *a:self.fail('No profile preparation')
        calls=[lambda:lab.start(0,'arena_live',['fake'],['arena-a'],100,18000),lambda:lab.stop(0,'a'*32),lambda:lab.resume(0,'a'*32),
               lambda:lab.launch({},[],False),lambda:lab.prepare({},[],False),lambda:lab.checked_account({}, {},None),
               lambda:lab.submit({}, {},None,{},0),lambda:lab.closing({}, {},None),lambda:lab.step({}),lambda:lab.decide({}, {},[],{})]
        for call in calls:
            with self.subTest(call=call),self.assertRaisesRegex(ArenaError,'передано'):call()
        lab.guard(0)  # Generic read/check guard remains available.

    def test_paper_orphan_is_rejected_and_bytes_are_not_rewritten(self):
        root=self.package();path=root/'runtime/arena_v4'/('a'*32+'.events.jsonl');path.write_bytes(b'PRESERVE')
        with self.assertRaises(ValueError):runner.verify_package(root)
        self.assertEqual(path.read_bytes(),b'PRESERVE')

    def test_paper_once_is_fsynced_before_start_and_survives_failed_start(self):
        root=self.package()
        with patch.object(runner.os,'fsync',wraps=runner.os.fsync) as sync:
            calls=self.fake_run(root,fail_start=True)
            self.assertTrue(calls['start_marker_seen']);self.assertGreaterEqual(sync.call_count,1)
        path=root/'runtime/portable-paper-once.json';before=path.read_bytes()
        with self.assertRaisesRegex(ValueError,'one_shot'):self.fake_run(root)
        self.assertEqual(path.read_bytes(),before)

    def test_paper_check_and_empty_viewer_never_reserve_or_start(self):
        root=self.package();runner.verify_package(root);calls=self.fake_run(root,view_only=True)
        self.assertEqual(calls['starts'],0);self.assertFalse((root/'runtime/portable-paper-once.json').exists())

    def test_atomic_start_reservation_has_one_winner_and_partial_marker_is_never_reset(self):
        from concurrent.futures import ThreadPoolExecutor
        root=self.package();package,_=runner.verify_package(root)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:runner._reserve_start(root,package,18000),range(2)))
        self.assertEqual(sorted(results),[False,True])
        json.loads((root/'runtime/portable-paper-once.json').read_text())
        other=self.package();marker=other/'runtime/portable-paper-once.json';marker.write_bytes(b'{partial')
        with self.assertRaisesRegex(ValueError,'one_shot'):self.fake_run(other)
        self.assertEqual(marker.read_bytes(),b'{partial')

    def test_paper_paused_then_done_export_once_each_and_no_retry_on_reopen(self):
        root=self.package(existing=True);calls=self.fake_run(root,states=('PAUSED','PAUSED','DONE','DONE'))
        self.assertEqual(calls['starts'],0);self.assertEqual(calls['exports'],['PAUSED','DONE'])
        self.assertEqual(self.fake_run(root,states=('DONE',))['exports'],[])
        self.assertEqual(len(list((root/'runtime/portable-export-attempts').glob('*.json'))),2)

    def test_failed_export_keeps_attempt_and_does_not_suppress_later_done(self):
        root=self.package(existing=True);calls=self.fake_run(root,states=('PAUSED','PAUSED','DONE'),fail_export=True)
        self.assertEqual(calls['exports'],['PAUSED','DONE'])
        self.assertEqual(self.fake_run(root,states=('DONE',),fail_export=True)['exports'],[])

    def test_five_hour_protocol_rejects_old_24h_manifest(self):
        root=self.package();path=root/'paper-package.json';doc=json.loads(path.read_text());doc['admission_options_seconds']=[18000,86400]
        write_json(path,doc)
        with self.assertRaises(ValueError):runner.verify_package(root)

    def test_installer_pure_os_gate_accepts_windows_10_11_and_servers(self):
        result=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(ROOT/'tests/fixtures/check_windows_installer.ps1'),
                               '-Installer',str(ROOT/'deployment/server/install_windows.ps1')],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        proof=json.loads(result.stdout);self.assertEqual(proof['parse'],'PASS');self.assertFalse(proof['installer_executed'])


if __name__=='__main__':
    try:unittest.main(verbosity=2)
    finally:SUPPORT.cleanup()
