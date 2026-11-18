"""Application allowlist, cold import and readiness, without a task/process/API launch."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from tools.build_server_bundle import build, ROOT
from tools.server_transfer import export_state, import_state, engine_epoch
from deployment.server.run_server import check
from tests.test_server_delivery import cold_fixture


class BundleTests(unittest.TestCase):
    def test_secret_free_bundle_can_import_full_fake_state_with_same_engine_epoch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            application = root / "application.zip"
            receipt = build(application)
            self.assertFalse(receipt["contains_credentials"])
            self.assertFalse(receipt["contains_runtime"])
            self.assertEqual(receipt["engine"], engine_epoch())
            with zipfile.ZipFile(application) as archive:
                self.assertIsNone(archive.testzip())
                for name in archive.namelist():
                    relative = name.removeprefix("JevArena/")
                    self.assertFalse(relative.startswith((".env", "runtime/", "LocalSettings/", "01_", "tests/")))
                archive.extractall(root / "server")
            destination = root / "server/JevArena"
            with self.assertRaises(ValueError):
                check(destination)
            source = root / "fake_source"
            cold_fixture(source)
            transfer = root / "fake.private.zip"
            export_state(source, transfer, include_credentials=True)
            import_state(destination, transfer, source_stopped=True)
            result = check(destination)
            self.assertEqual(result["status"], "READY")
            self.assertFalse(result["automatic_trade_start"])
            self.assertFalse(result["credentials_loaded"])
            self.assertTrue(all(p["credential_file_present"] for p in result["profiles"]))
            # A separate interpreter must resolve imports/hash files from the ZIP,
            # rather than accidentally using this checkout's already-loaded modules.
            fresh = subprocess.run([sys.executable, str(destination / "deployment/server/run_server.py"), "check"],
                                   cwd=destination, capture_output=True, text=True, timeout=30)
            self.assertEqual(fresh.returncode, 0, fresh.stderr)
            self.assertIn('"status": "READY"', fresh.stdout)
            self.assertEqual((destination / "trading_lab/exchange_engine.py").read_bytes(),
                             (ROOT / "trading_lab/exchange_engine.py").read_bytes())
            (destination / "web/arena.js").write_text("changed", encoding="utf-8")
            with self.assertRaises(ValueError):
                check(destination)


if __name__ == "__main__":
    unittest.main()
