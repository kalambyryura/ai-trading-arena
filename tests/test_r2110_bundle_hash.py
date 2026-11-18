"""R2.1.11 (plan p. 8): the exports of journals written by R2.1.10 stay byte for byte.

Three fictional journals on the fake exchange, written by the R2.1.10 code (main 16822e0); the pins are what R2.1.10
itself computed: bundle_sha256 and the ZIP data members (D137 p. 6 as the R2.1.10 review applied it). They cover two
ETH cycles, an extended A4 window with exchange_process_failed among its codes, a window open when the journal was saved,
stop_renewed, accounting_confirmed, exchange_order_read, order_body, the A1 fields, feature_book, an unseen verdict.
R2.1.11 RC2 (R2111_REVIEW condition (2) item 5): a fourth, r2110_flat_minute, has a lone edge minute the R2.1.10 book
filled flat, so its feature_book event carries an integer gap_flat (the other three have it null); written and pinned
by R2.1.10 the same way."""
import gzip
import hashlib
import json
from pathlib import Path
import unittest
import zipfile

from trading_lab.battle_export import create_archive, verify_archive
from trading_lab.battle_review import build_review

FIXTURES = Path(__file__).parent / "fixtures"
PINS = {
    "r2110_open_window": ("af8573225e2e0dc1def6fd6b6d771781c476a5c7dd5c6cd630b3543f59d3f878", {
        "battle-replay.json": "4a2fc88061c59d364ef50f561a9ada0985c6472529135e40c889b70a78977b26",
        "match-settings.json": "5e6b6d5a52ae660ed548070ad1818a9109b491db6bc3c0406c6bc2a5997ca49a",
        "strategies.json": "ea99a96a0991934772e96ea5c9a523de187df82fddd1148fbec0ccd490aa1803"}),
    "r2110_two_cycles_done": ("c49876a9bca371f85b57d9c5ddbb08606195c69c8fef6a21065d4aeef29ed910", {
        "battle-replay.json": "b849d797b914396a23579b078b13e9e150c5ff2a8f41eeaa3e361754360d2e0b",
        "match-settings.json": "283b8fb5895fa0bd990f4495acc21ef2115b479d931656421adb684c05837bce",
        "strategies.json": "ea99a96a0991934772e96ea5c9a523de187df82fddd1148fbec0ccd490aa1803"}),
    "r2110_unseen_done": ("a5865a54d58acd407752b1968a91942f6fb1b7f8aaf713f50314a2ebab71ef7e", {
        "battle-replay.json": "a25d012d1529e1dc6b6b0c28a3aec1c7cce5c9d197f0024afdbc7ff023d072f9",
        "match-settings.json": "8e76ad1a4c68cb3008265471caa30f788f52b98c7fc019d7822c3365124e9719",
        "strategies.json": "6ffac227f601ed7ee41849809b919dcb146955f339c46560640ff5a5a4e215df"}),
    "r2110_flat_minute": ("61cb9cd45367c0ab01f371374e5cb9ca981b67c5b8abffb2cc21d459a8df33e8", {
        "battle-replay.json": "62d870b2831cfad16c5ecb86619f276b0d2277bdeed8e02412e4d42cf05bc847",
        "match-settings.json": "2aae7f61ccca9baca0f6fc344b30ac36668eaa8307f2a311dc9270a732558102",
        "strategies.json": "b47e847b3d7da6d5e9d8c66167b51644ebc5c4db8a513595c38563e2bdd60e09"}),
}


def journal(name):
    return json.loads(gzip.decompress((FIXTURES / (name + ".json.gz")).read_bytes()))


class R2110BundleHash(unittest.TestCase):
    def test_journals_from_r2110_export_the_same_bundle_and_data_files(self):
        for name, (bundle_sha256, files) in PINS.items():
            with self.subTest(name):
                state = journal(name)
                self.assertEqual(build_review(state)["integrity"]["bundle_sha256"], bundle_sha256)
                with create_archive(state) as stream:
                    self.assertTrue(verify_archive(stream)["verified"])
                    stream.seek(0)
                    with zipfile.ZipFile(stream) as archive:
                        self.assertEqual({n: hashlib.sha256(archive.read(n)).hexdigest() for n in files}, files)
                        manifest = json.loads(archive.read("manifest.json"))
                        self.assertNotIn("degraded_windows", manifest)  # A4's counter only for journals that have the state

    def test_the_fixture_covers_what_it_pins(self):
        codes = set().union(*({e["code"] for e in journal(n)["events"]} for n in PINS))
        for code in ("exchange_order_read", "exchange_reserved", "execution_profile", "degraded_extension_ended",
                     "degraded_window_ended", "stop_renewed", "accounting_confirmed", "exchange_order_absent",
                     "exchange_check_failed", "feature_book", "leverage_confirmed", "step_timing"):
            self.assertIn(code, codes)
        done = journal("r2110_two_cycles_done")["bots"][0]
        self.assertEqual((done["entries"], done["filled_entry_orders"]), (0, 2))
        self.assertTrue(journal("r2110_open_window")["bots"][0].get("_degraded"))
        flat = [e["data"]["gap_flat"] for e in journal("r2110_flat_minute")["events"] if e["code"] == "feature_book"
                and e["data"].get("gap_flat") is not None]
        self.assertEqual(flat, [1])  # correction 13: the integer branch of gap_flat is pinned too
        self.assertEqual([e["data"]["gap_flat"] for e in build_review(journal("r2110_flat_minute"))["events"]
                          if e["code"] == "feature_book" and "gap_flat" in e["data"]], [1])


if __name__ == "__main__":
    unittest.main()
