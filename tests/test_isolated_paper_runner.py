"""Network boundary for the isolated public-market PAPER launcher."""
import importlib.util
from pathlib import Path
import unittest
from urllib.request import Request

spec = importlib.util.spec_from_file_location("isolated_paper_runner", Path(__file__).parents[1] / "tools/run_isolated_paper.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class PublicOnlyTests(unittest.TestCase):
    def test_only_expected_public_get_is_admitted(self):
        self.assertTrue(runner.public_request_allowed(Request("https://openapi.okx.com/api/v5/market/ticker?instId=BTC-USDT-SWAP")))
        for url in ("https://openapi.okx.com/api/v5/trade/order", "https://openapi.okx.com/api/v5/account/balance",
                    "https://api.typesafe.ai/decision", "https://other.example/api/v5/public/time",
                    "http://openapi.okx.com/api/v5/public/time"):
            self.assertFalse(runner.public_request_allowed(Request(url)))

    def test_writes_bodies_and_authentication_are_rejected(self):
        url = "https://openapi.okx.com/api/v5/public/time"
        self.assertFalse(runner.public_request_allowed(Request(url, method="POST")))
        self.assertFalse(runner.public_request_allowed(Request(url, data=b"{}", method="GET")))
        self.assertFalse(runner.public_request_allowed(Request(url), data=b"{}"))
        for header in ("OK-ACCESS-KEY", "Authorization", "Proxy-Authorization"):
            self.assertFalse(runner.public_request_allowed(Request(url, headers={header: "fictional"})))


if __name__ == "__main__":
    unittest.main()
