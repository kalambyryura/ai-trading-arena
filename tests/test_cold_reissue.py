"""Fake/temp archives only. No real workspace, credentials, transport or process stop."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from tools import server_transfer as transfer
from tests.test_server_delivery import cold_fixture,write_json


class ReissueTests(unittest.TestCase):
    def fixture(self,historical=False):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        base=Path(temporary.name);source=base/'source';cold_fixture(source)
        prior=base/'first.private.zip'
        if historical:
            with patch.object(transfer,'engine_epoch',return_value={k:'f'*64 for k in ('training','exchange','paper')}):
                transfer.export_state(source,prior,include_credentials=True)
        else:transfer.export_state(source,prior,include_credentials=True)
        write_json(source/'runtime/workshop_v1/newer.json',{'generation':2,'fixture':True})
        (source/'.env.okx.demo.local').write_text('PRIVATE_ROTATED_KEY=FAKE_CURRENT_VALUE\n',encoding='utf-8')
        return base,source,prior

    def reissue(self,source,prior,output,**kwargs):
        return transfer.export_state(source,output,include_credentials=True,reissue_from=prior,
                                     source_authoritative=True,all_targets_stopped=True,**kwargs)

    def marker(self,source):return json.loads((source/transfer.HANDOFF).read_text())

    def manifest(self,path):
        with zipfile.ZipFile(path) as archive:return json.loads(archive.read('TRANSFER_MANIFEST.json'))

    def alter_prior(self,source,prior,change,*,extra=None):
        with zipfile.ZipFile(prior) as archive:members={name:archive.read(name) for name in archive.namelist()}
        manifest=json.loads(members['TRANSFER_MANIFEST.json']);change(manifest)
        members['TRANSFER_MANIFEST.json']=json.dumps(manifest).encode()
        if extra:members.update(extra)
        with zipfile.ZipFile(prior,'w') as archive:
            for name,data in members.items():archive.writestr(name,data)
        marker=self.marker(source);marker['archive_sha256']=transfer.sha_file(prior)
        write_json(source/transfer.HANDOFF,marker)
        return manifest

    def test_normal_export_still_refuses_existing_handoff(self):
        base,source,prior=self.fixture();before=(source/transfer.HANDOFF).read_bytes()
        with self.assertRaisesRegex(transfer.TransferError,'workspace_already_handed_off'):
            transfer.export_state(source,base/'new.zip')
        self.assertEqual((source/transfer.HANDOFF).read_bytes(),before)

    def test_all_three_flags_required_together_before_any_export(self):
        base,source,prior=self.fixture()
        for supplied in [dict(reissue_from=prior),dict(source_authoritative=True),dict(all_targets_stopped=True),
                         dict(reissue_from=prior,source_authoritative=True),dict(reissue_from=prior,all_targets_stopped=True),
                         dict(source_authoritative=True,all_targets_stopped=True),
                         dict(reissue_from=prior,source_authoritative=1,all_targets_stopped=True)]:
            with self.subTest(supplied=tuple(supplied)),self.assertRaisesRegex(transfer.TransferError,'reissue_all_flags_required'):
                transfer.export_state(source,base/'never.zip',**supplied)
        self.assertFalse((base/'never.zip.pending').exists())

    def test_historical_predecessor_current_export_and_credential_rotation_preserved(self):
        base,source,prior=self.fixture(historical=True);old_sha=transfer.sha_file(prior);old_marker=(source/transfer.HANDOFF).read_bytes()
        with zipfile.ZipFile(prior) as archive:
            with self.assertRaises(transfer.TransferError):transfer.verify_archive(archive)
        result=self.reissue(source,prior,base/'second.private.zip')
        self.assertEqual(result['status'],'REISSUED_PRIVATE');self.assertEqual(transfer.sha_file(prior),old_sha)
        current=self.marker(source);manifest=self.manifest(base/'second.private.zip')
        self.assertEqual(current['archive_sha256'],result['archive_sha256'])
        self.assertEqual(manifest['engine'],transfer.engine_epoch());self.assertEqual(manifest['handoff_chain'],current['handoff_chain'])
        self.assertEqual(current['handoff_chain'][0],{'archive_sha256':old_sha,'marker_sha256':hashlib.sha256(old_marker).hexdigest(),
                                                    'exported_at':json.loads(old_marker)['exported_at']})
        with zipfile.ZipFile(base/'second.private.zip') as archive:
            transfer.verify_archive(archive)
            self.assertIn(b'PRIVATE_ROTATED_KEY',archive.read('.env.okx.demo.local'))
            self.assertIn('runtime/workshop_v1/newer.json',archive.namelist())
            self.assertNotIn(transfer.HANDOFF,archive.namelist())

    def test_chain_extends_exact_prefix_and_import_remains_current_epoch_only(self):
        base,source,prior=self.fixture();second=base/'second.private.zip';third=base/'third.private.zip'
        self.reissue(source,prior,second);prefix=deepcopy(self.marker(source)['handoff_chain'])
        self.reissue(source,second,third);chain=self.marker(source)['handoff_chain']
        self.assertEqual(chain[:-1],prefix);self.assertEqual(chain[-1]['archive_sha256'],transfer.sha_file(second))
        self.assertEqual(self.manifest(third)['handoff_chain'],chain)
        result=transfer.import_state(base/'destination',third,source_stopped=True)
        self.assertEqual(result['status'],'IMPORTED');self.assertFalse(result['automatic_trade_start'])

    def test_chain_limit_rejects_without_truncation_or_marker_change(self):
        base,source,prior=self.fixture()
        with patch.object(transfer,'MAX_HANDOFF_CHAIN',2):
            second=base/'second.zip';third=base/'third.zip';self.reissue(source,prior,second);self.reissue(source,second,third)
            before=(source/transfer.HANDOFF).read_bytes()
            with self.assertRaisesRegex(transfer.TransferError,'reissue_chain_capacity'):
                self.reissue(source,third,base/'fourth.zip')
            self.assertEqual((source/transfer.HANDOFF).read_bytes(),before)

    def test_wrong_prior_sha_and_marker_date_fail_closed(self):
        base,source,prior=self.fixture();before=(source/transfer.HANDOFF).read_bytes()
        wrong=base/'wrong.zip';wrong.write_bytes(b'FAKE_WRONG_ARCHIVE')
        with self.assertRaisesRegex(transfer.TransferError,'reissue_prior_archive_mismatch'):self.reissue(source,wrong,base/'new.zip')
        marker=self.marker(source);marker['exported_at']='PRIVATE_INVALID_DATE';write_json(source/transfer.HANDOFF,marker)
        with self.assertRaises(transfer.TransferError):self.reissue(source,prior,base/'new.zip')
        self.assertFalse((base/'new.zip.pending').exists());self.assertTrue((source/transfer.HANDOFF).exists())

    def test_marker_and_archive_date_must_match(self):
        base,source,prior=self.fixture();marker=self.marker(source)
        marker['exported_at']=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat();write_json(source/transfer.HANDOFF,marker)
        with self.assertRaisesRegex(transfer.TransferError,'reissue_prior_archive_mismatch'):self.reissue(source,prior,base/'new.zip')

    def test_authenticated_unsafe_prior_archive_member_is_rejected(self):
        base,source,prior=self.fixture()
        self.alter_prior(source,prior,lambda manifest:None,extra={'../escape':b'PRIVATE_FAKE'})
        with self.assertRaises(transfer.TransferError):self.reissue(source,prior,base/'new.zip')
        self.assertFalse((base/'escape').exists());self.assertFalse((base/'new.zip.pending').exists())

    def test_authenticated_invalid_prior_schema_is_rejected(self):
        base,source,prior=self.fixture();self.alter_prior(source,prior,lambda m:m.update(schema='PRIVATE_FAKE_SCHEMA'))
        with self.assertRaises(transfer.TransferError):self.reissue(source,prior,base/'new.zip')

    def test_manager_lock_active_and_orphan_current_journals_block_reissue(self):
        base,source,prior=self.fixture()
        with transfer.exclusive(source/'runtime/exchange_v1/manager.lock'):
            with self.assertRaisesRegex(transfer.TransferError,'close_idle_source_panel_before_export'):
                self.reissue(source,prior,base/'new.zip')
        identity='a'*32;write_json(source/'runtime/exchange_v1/manifest.json',{'runs':[identity]})
        write_json(source/'runtime/exchange_v1'/(identity+'.json'),{'status':'RUNNING','bots':[{'id':'fake','position_btc':'.001'}]})
        with self.assertRaisesRegex(transfer.TransferError,'unfinished_battle'):self.reissue(source,prior,base/'new.zip')
        write_json(source/'runtime/exchange_v1/manifest.json',{'runs':[]})
        with self.assertRaisesRegex(transfer.TransferError,'unindexed_run_evidence'):self.reissue(source,prior,base/'new.zip')

    def test_marker_is_never_absent_during_successful_reissue(self):
        base,source,prior=self.fixture();original=transfer.os.replace;seen=[]
        def observe(a,b):
            self.assertTrue((source/transfer.HANDOFF).exists());seen.append(str(b))
            result=original(a,b);self.assertTrue((source/transfer.HANDOFF).exists());return result
        with patch.object(transfer.os,'replace',side_effect=observe):self.reissue(source,prior,base/'new.zip')
        self.assertEqual(seen,[str(source/transfer.HANDOFF)])

    def test_failure_before_marker_replace_keeps_old_guard_and_verified_pending(self):
        base,source,prior=self.fixture();before=(source/transfer.HANDOFF).read_bytes()
        with patch.object(transfer.os,'replace',side_effect=OSError('PRIVATE_PUBLISH_DETAIL')):
            with self.assertRaises(OSError):self.reissue(source,prior,base/'new.zip')
        self.assertEqual((source/transfer.HANDOFF).read_bytes(),before)
        self.assertTrue((source/(transfer.HANDOFF+'.reissue.pending')).is_file())
        with zipfile.ZipFile(base/'new.zip.pending') as archive:transfer.verify_archive(archive)
        self.assertFalse((base/'new.zip').exists())

    def test_failure_after_marker_replace_keeps_new_guard_and_verified_pending(self):
        base,source,prior=self.fixture();old_sha=transfer.sha_file(prior)
        with patch.object(transfer,'_publish_reissue',side_effect=OSError('PRIVATE_PUBLISH_DETAIL')):
            with self.assertRaises(OSError):self.reissue(source,prior,base/'new.zip')
        self.assertEqual(self.marker(source)['archive_sha256'],transfer.sha_file(base/'new.zip.pending'))
        self.assertEqual(transfer.sha_file(prior),old_sha);self.assertTrue((source/transfer.HANDOFF).exists())

    def test_output_collision_never_overwrites_unrelated_file(self):
        base,source,prior=self.fixture();output=base/'new.zip';original=transfer.os.replace
        def collide(a,b):
            output.write_bytes(b'PRESERVE_EXISTING_OUTPUT');return original(a,b)
        with patch.object(transfer.os,'replace',side_effect=collide):
            with self.assertRaises(FileExistsError):self.reissue(source,prior,output)
        self.assertEqual(output.read_bytes(),b'PRESERVE_EXISTING_OUTPUT');self.assertTrue((base/'new.zip.pending').exists())

    def test_pending_artifacts_and_existing_output_are_never_reused(self):
        for suffix in ('output','archive_pending','marker_pending'):
            base,source,prior=self.fixture();output=base/'new.zip'
            path=output if suffix=='output' else base/'new.zip.pending' if suffix=='archive_pending' else source/(transfer.HANDOFF+'.reissue.pending')
            path.write_bytes(b'PRESERVE')
            with self.assertRaises(transfer.TransferError):self.reissue(source,prior,output)
            self.assertEqual(path.read_bytes(),b'PRESERVE')

    def test_cli_requires_owner_attestations_and_never_prints_credential_values(self):
        base,source,prior=self.fixture();args=['export','--source',str(source),'--output',str(base/'new.zip'),'--include-credentials','--reissue-from',str(prior)]
        output=io.StringIO();error=io.StringIO()
        with patch('sys.stdout',output),patch('sys.stderr',error):self.assertEqual(transfer.main(args),2)
        self.assertIn('reissue_all_flags_required',error.getvalue())
        output=io.StringIO();error=io.StringIO()
        with patch('sys.stdout',output),patch('sys.stderr',error):
            self.assertEqual(transfer.main(args+['--source-authoritative','--all-targets-stopped']),0)
        self.assertIn('REISSUED_PRIVATE',output.getvalue());self.assertNotIn('PRIVATE_ROTATED_KEY',output.getvalue()+error.getvalue())
        self.assertNotIn('FAKE_CURRENT_VALUE',output.getvalue()+error.getvalue())

    def test_current_source_change_during_export_keeps_old_guard(self):
        base,source,prior=self.fixture();before=(source/transfer.HANDOFF).read_bytes();verify=transfer.verify_archive
        def change_current(archive):
            verified=verify(archive)
            (source/'runtime/workshop_v1/newer.json').write_text('{"changed":true}',encoding='utf-8')
            return verified
        with patch.object(transfer,'verify_archive',side_effect=change_current):
            with self.assertRaisesRegex(transfer.TransferError,'source_changed_during_cold_export'):
                self.reissue(source,prior,base/'new.zip')
        self.assertEqual((source/transfer.HANDOFF).read_bytes(),before);self.assertTrue((base/'new.zip.pending').is_file())

    def test_marker_change_during_export_is_not_overwritten(self):
        base,source,prior=self.fixture();verify=transfer.verify_archive;replacement=b'{"external_manual_review":true}'
        def change_marker(archive):
            verified=verify(archive);(source/transfer.HANDOFF).write_bytes(replacement);return verified
        with patch.object(transfer,'verify_archive',side_effect=change_marker):
            with self.assertRaisesRegex(transfer.TransferError,'reissue_marker_changed'):
                self.reissue(source,prior,base/'new.zip')
        self.assertEqual((source/transfer.HANDOFF).read_bytes(),replacement)

    def test_pending_verification_failure_never_replaces_marker(self):
        base,source,prior=self.fixture();before=(source/transfer.HANDOFF).read_bytes()
        with patch.object(transfer,'verify_archive',side_effect=transfer.TransferError('archive_hash_mismatch')):
            with self.assertRaises(transfer.TransferError):self.reissue(source,prior,base/'new.zip')
        self.assertEqual((source/transfer.HANDOFF).read_bytes(),before);self.assertFalse((base/'new.zip').exists())

    def test_future_marker_and_mismatching_prior_chain_are_rejected(self):
        base,source,prior=self.fixture();marker=self.marker(source)
        marker['exported_at']=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat();write_json(source/transfer.HANDOFF,marker)
        with self.assertRaises(transfer.TransferError):self.reissue(source,prior,base/'new.zip')
        base,source,prior=self.fixture();second=base/'second.zip';self.reissue(source,prior,second)
        marker=self.marker(source);marker['handoff_chain'][0]['marker_sha256']='0'*64;write_json(source/transfer.HANDOFF,marker)
        with self.assertRaises(transfer.TransferError):self.reissue(source,second,base/'third.zip')

    def test_prior_archive_parser_details_never_reach_cli(self):
        base,source,prior=self.fixture();output=io.StringIO();error=io.StringIO()
        args=['export','--source',str(source),'--output',str(base/'new.zip'),'--reissue-from',str(prior),
              '--source-authoritative','--all-targets-stopped']
        with patch.object(transfer,'_verify_archive',side_effect=RuntimeError('PRIVATE_ARCHIVE_PARSER_DETAIL')),patch('sys.stdout',output),patch('sys.stderr',error):
            self.assertEqual(transfer.main(args),2)
        self.assertIn('reissue_prior_archive_mismatch',error.getvalue())
        self.assertNotIn('PRIVATE_ARCHIVE_PARSER_DETAIL',output.getvalue()+error.getvalue())


if __name__=='__main__':
    with patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('No outbound calls')):
        unittest.main(verbosity=2)
