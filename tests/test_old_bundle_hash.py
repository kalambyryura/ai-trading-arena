"""R2.1.10 (D136 p. 6): the bundle of a journal written before R2.1.10 stays byte for byte.

The fixture is a short fictional Live season on the fake exchange (taker entry, local stop-loss, settlement), written
by the R2.1.9 code (efc62fc); the pinned hash is the one R2.1.9 itself computed for its bundle."""
import gzip
import json
from pathlib import Path
import unittest

from trading_lab.battle_review import build_review, verify_bundle

FIXTURE = Path(__file__).parent / "fixtures" / "r219_live_journal.json.gz"
R219_BUNDLE_SHA256 = "618751ee06ba95cbfae46dda0ecd3506cededb00a51d4fc76e9da5cab9cb54a4"


class OldBundleHash(unittest.TestCase):
    def test_a_journal_from_before_r2110_exports_the_same_bundle(self):
        state = json.loads(gzip.decompress(FIXTURE.read_bytes()))
        self.assertEqual(state["status"], "DONE")
        bundle = build_review(state)
        verify_bundle(bundle)
        self.assertEqual(bundle["integrity"]["bundle_sha256"], R219_BUNDLE_SHA256)


if __name__ == "__main__":
    unittest.main()
