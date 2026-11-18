"""Replay must preserve recorded small decimals without inventing a flat position."""
from copy import deepcopy
import unittest

from trading_lab.arena_strategies import ArenaError
from trading_lab import event_replay
from test_replay_evidence_integration import exchange_fixture, rehash_packet


class ReplayDecimalPrecisionTests(unittest.TestCase):
    def test_small_recorded_amount_survives_exactly(self):
        for value in ("-0.0000000000000031866870253693946", "0." + "0" * 63 + "1"):
            with self.subTest(value=value):
                packet = event_replay.build_packet(exchange_fixture())
                packet["events"][0]["financial"]["gross_usdt"] = value
                rehash_packet(packet)
                before = deepcopy(packet)
                self.assertTrue(event_replay.verify_packet(packet)["verified"])
                self.assertEqual(packet, before)

    def test_fraction_limit_and_nondecimal_inputs_still_rejected(self):
        for value in ("0." + "0" * 64 + "1", "1e-31", "NaN", "Infinity", "01.0", True):
            with self.subTest(value=value):
                packet = event_replay.build_packet(exchange_fixture())
                packet["events"][0]["financial"]["gross_usdt"] = value
                with self.assertRaises(ArenaError):
                    event_replay.verify_packet(rehash_packet(packet))

    def test_tiny_remaining_position_is_not_flat(self):
        packet = event_replay.build_packet(exchange_fixture())
        final = next(e for e in reversed(packet["events"]) if "financial" in e)
        final["financial"]["position_btc"] = "0." + "0" * 63 + "1"
        with self.assertRaises(ArenaError):
            event_replay.verify_packet(rehash_packet(packet))


if __name__ == "__main__":
    unittest.main()
