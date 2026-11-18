from copy import deepcopy
import unittest

from trading_lab.public_market import parse_ticker


class PublicMarketTests(unittest.TestCase):
    def setUp(self):
        self.payload = {"code": "0", "data": [{"instId": "BTC-USDT-SWAP", "instType": "SWAP",
                                               "bidPx": "100", "askPx": "100.1", "ts": "1000000"}]}

    def test_valid_snapshot_is_read_only(self):
        result = parse_ticker(self.payload, 1001)
        self.assertEqual(result["freshness"], "fresh")
        self.assertEqual(result["orders_submitted"], 0)
        self.assertFalse(result["eligible_for_trading"])

    def test_stale_and_future_data_are_flagged(self):
        for now in (1040, 980):
            self.assertEqual(parse_ticker(self.payload, now)["freshness"], "stale_or_clock_skew")

    def test_bad_market_data_is_rejected(self):
        for field, value in [("bidPx", "nan"), ("askPx", "99"), ("bidPx", True),
                             ("ts", "inf"), ("instId", "OTHER"), ("askPx", "0")]:
            payload = deepcopy(self.payload)
            payload["data"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                parse_ticker(payload, 1001)


if __name__ == "__main__":
    unittest.main()
