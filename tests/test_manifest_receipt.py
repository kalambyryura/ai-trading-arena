"""R2.1.10 (H): the code hashes in manifest.json and the manifest's own receipt; ZIPs from before still verify."""
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools import live_packets
from trading_lab.arena import arena_source_hash
from trading_lab.arena_strategies import ArenaError, fingerprint
from trading_lab.battle_export import RECEIPT, create_archive, exporter_hash, verify_archive
from trading_lab.exchange_lab import source_hash as exchange_hash
from trading_lab.strategy_contract import payload_of
from trading_lab.strategy_training import source_hash as training_hash
from tests.test_replay_evidence_integration import paper_fixture


def members(stream):
    stream.seek(0)
    with zipfile.ZipFile(stream) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def rezip(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    output.seek(0)
    return output


class ManifestReceiptTests(unittest.TestCase):
    def setUp(self):
        self.state = paper_fixture()
        for row in self.state["strategies"]:
            if row["id"] != "cash":
                row["recipe_sha256"] = fingerprint(payload_of(row))
        with create_archive(self.state) as output:
            self.files = members(output)

    def test_h_manifest_names_the_code_and_the_receipt_pins_the_manifest(self):
        self.assertIn(RECEIPT, self.files)
        manifest = json.loads(self.files["manifest.json"])
        self.assertEqual(manifest["code_hashes"], {"journal": "e" * 64, "exporter": exporter_hash(), "arena": arena_source_hash(),
                                                   "exchange": exchange_hash(), "training": training_hash()})
        stamp = json.loads(self.files[RECEIPT])
        self.assertEqual(stamp["manifest_sha256"], hashlib.sha256(self.files["manifest.json"]).hexdigest())
        self.assertEqual(stamp["manifest_bytes"], len(self.files["manifest.json"]))
        self.assertTrue(verify_archive(rezip(self.files))["verified"])

    def test_h_a_changed_manifest_fails_its_receipt(self):
        files = dict(self.files)
        raw = files["manifest.json"]
        at = raw.index(b'"exported_at":"') + len(b'"exported_at":"')
        files["manifest.json"] = raw[:at] + (b"1" if raw[at:at + 1] != b"1" else b"2") + raw[at + 1:]  # same size
        with self.assertRaisesRegex(ArenaError, "Квитанція"):
            verify_archive(rezip(files))

    def test_h_a_zip_from_before_without_a_receipt_still_verifies(self):
        files = {k: v for k, v in self.files.items() if k != RECEIPT}
        self.assertTrue(verify_archive(rezip(files))["verified"])
        files["extra.json"] = b"{}"
        with self.assertRaisesRegex(ArenaError, "склад"):
            verify_archive(rezip(files))

    def test_h_exporter_hash_ignores_only_line_endings(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(__file__).resolve().parents[1] / "trading_lab"
            for name in ("battle_export.py", "battle_review.py", "event_replay.py", "cycle_diagnostics.py",
                         "g1_public_evidence.py", "battle_debrief.py", "review_diagnostics.py"):
                (Path(folder) / name).write_bytes((source / name).read_bytes().replace(b"\n", b"\r\n"))
            self.assertEqual(exporter_hash(folder), exporter_hash())
            (Path(folder) / "battle_review.py").write_bytes(b"#" + (source / "battle_review.py").read_bytes())
            self.assertNotEqual(exporter_hash(folder), exporter_hash())

    def test_h_live_packets_reads_a_zip_with_the_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "battle.zip"
            path.write_bytes(rezip(self.files).getvalue())
            out, _, manifest = live_packets.read_zip(path)
        self.assertIn(RECEIPT, out)
        self.assertIn("code_hashes", manifest)

    def test_receipt_live_packets_checks_the_receipt(self):  # R2.1.11 (R2110-RECEIPT)
        files = dict(self.files)
        files[RECEIPT] = files[RECEIPT].replace(b'"manifest_bytes":', b'"manifest_bytes":1')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "battle.zip"
            path.write_bytes(rezip(files).getvalue())
            with self.assertRaises(SystemExit):
                live_packets.read_zip(path)

    def test_packets_dup_live_packets_refuses_a_duplicated_member(self):  # R2.1.11 (R2111-PACKETS-DUP)
        import warnings
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "battle.zip"
            output = rezip(self.files)
            with zipfile.ZipFile(output, "a") as archive, warnings.catch_warnings():
                warnings.simplefilter("ignore")  # zipfile warns on a duplicate name and writes it anyway
                archive.writestr("strategies.json", b"[]")  # a second copy, the one a last-wins reader would take
            path.write_bytes(output.getvalue())
            with self.assertRaisesRegex(SystemExit, "duplicate ZIP member"):
                live_packets.read_zip(path)
            with path.open("rb") as stream, self.assertRaises(ArenaError):
                verify_archive(stream)  # the same answer as the exporter's own check


if __name__ == "__main__":
    unittest.main()
