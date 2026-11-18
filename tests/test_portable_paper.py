from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from tools.build_paper_package import build
from tools.run_paper_package import verify_package
from tools.export_paper_observation import export
from trading_lab.arena import exclusive
from trading_lab.arena_engine import new_season, request_stop, tick
from trading_lab.arena_strategies import catalogue_defaults


class PortablePaperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        cls.addClassCleanup(cls.temp.cleanup)
        cls.roster = [deepcopy(catalogue_defaults()[0])]
        cls.archive = cls.base / 'package.zip'
        cls.receipt = build(cls.archive, cls.roster)
        with zipfile.ZipFile(cls.archive) as archive:
            archive.extractall(cls.base / 'initial')
        cls.source = cls.base / 'initial/JevArena-PAPER'

    def copy(self):
        destination = self.base / ('copy-' + str(len(list(self.base.iterdir()))))
        with zipfile.ZipFile(self.archive) as archive:
            archive.extractall(destination)
        return destination / 'JevArena-PAPER'

    def test_secret_free_source_pins_and_separate_interpreter_check(self):
        package, roster = verify_package(self.source)
        self.assertEqual(len(roster['strategies']), 1)
        self.assertEqual(package['admission_options_seconds'], [18000])
        self.assertFalse(self.receipt['contains_credentials'])
        with zipfile.ZipFile(self.archive) as archive:
            self.assertFalse(any('/.env' in n or '/LocalSettings/' in n for n in archive.namelist()))
        result = subprocess.run([sys.executable, '-B', str(self.source / 'tools/run_paper_package.py'),
                                 '--root', str(self.source), '--check-only'],
                                cwd=self.source, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('PORTABLE_PAPER_PACKAGE_OK', result.stdout)
        self.assertFalse(list((self.source / 'runtime/arena_v4').glob('*.events*')))

    def test_tamper_keys_and_extra_code_fail_closed(self):
        for relative, contents in [('trading_lab/arena_engine.py', 'changed'),
                                   ('.env.okx.local', 'fictional=1'),
                                   ('tools/unpinned.py', 'pass')]:
            root = self.copy()
            (root / relative).write_text(contents, encoding='utf-8')
            with self.assertRaises(ValueError):
                verify_package(root)

    def test_catalogue_metadata_cannot_change_and_started_run_is_viewable(self):
        root = self.copy()
        path = root / 'runtime/arena_v4/manifest.json'
        doc = json.loads(path.read_text(encoding='utf-8'))
        doc.update(seasons=['a' * 32], active_id='a' * 32)
        path.write_text(json.dumps(doc), encoding='utf-8')
        verify_package(root)  # run() opens an existing round; start is only for an empty seasons list.
        doc['strategies'][0]['version'] += 1
        path.write_text(json.dumps(doc), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'catalogue'):
            verify_package(root)

    def test_no_overwrite_existing_bundle(self):
        with self.assertRaisesRegex(ValueError, 'destination_exists'):
            build(self.archive, self.roster)

    def test_export_terminal_round_keeps_full_events_and_keys_out(self):
        root = self.copy()
        state = new_season(self.roster, 100, 18000, 1800000000, 'f' * 64)
        state['execution_mode'] = 'rules'
        # A zero-entry finished observation is still a valid complete artifact.
        request_stop(state, 1800000001, origin='owner')
        state.update(status='DONE', finished_at='2027-01-15T08:00:01+00:00')
        for bot in state['bots']:
            bot['position_btc'] = 0
        path = root / 'runtime/arena_v4'
        (path / 'manifest.json').write_text(json.dumps({'active_id': state['id'], 'seasons': [state['id']]}), encoding='utf-8')
        (path / (state['id'] + '.json')).write_text(json.dumps(state), encoding='utf-8')
        receipt = export(root)
        self.assertTrue(receipt['verified'])
        self.assertTrue(receipt['final'])
        self.assertFalse(receipt['contains_credentials'])
        self.assertTrue(receipt['html_created'])
        self.assertTrue((root / 'exports' / receipt['archive']).is_file())
        with exclusive(path / 'season.lock'):
            with self.assertRaises(ValueError):
                export(root)


if __name__ == '__main__':
    unittest.main()
