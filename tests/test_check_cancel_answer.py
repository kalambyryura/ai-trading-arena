"""tools/check_cancel_answer.py (OWNER_ACTIONS 21): one cancel of a client ID no order ever had, on a Demo profile only,
and OKX's raw answer. Fake exchange and temp root only: no network, real key or order."""
import json
from pathlib import Path
import sys
import unittest
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
import check_cancel_answer as tool  # noqa: E402
import test_exchange_lab as fixture  # noqa: E402


class CancelAnswerTool(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.ExchangeTests(methodName="runTest")
        self.fx.setUp()
        self.sent = []

    def tearDown(self):
        self.fx.tearDown()

    def transport(self, answer):
        fake = self.fx.fakes["demo"]

        def send(request):
            self.sent.append((request.get_method(), urlsplit(request.full_url).path, dict(request.headers)))
            if urlsplit(request.full_url).path == "/api/v5/trade/cancel-order":
                body = json.loads(request.data)
                return answer(body["clOrdId"])
            return fake.transport(request)
        return send

    def test_one_cancel_on_demo_and_the_raw_answer_without_keys(self):
        answer = lambda ident: {"code": "1", "msg": "", "data": [{"sCode": "51400", "sMsg": "Order cancellation failed", "clOrdId": ident, "ordId": ""}]}
        report = tool.check(self.fx.root, "demo", transport=self.transport(answer))
        self.assertEqual(report["raw_answer"], answer(report["client_order_id"]))
        self.assertEqual(report["r218_reads_it_as"], "nothing to cancel (51400)")
        self.assertEqual(report["requests"], ["GET /api/v5/public/time", "POST /api/v5/trade/cancel-order"])
        self.assertEqual(report["orders_sent"], 0)
        self.assertEqual([m for m, *_ in self.sent].count("POST"), 1)
        printed = json.dumps(report)
        for secret in ("FAKE_KEY", "FAKE_SECRET", "FAKE_PASS", "OK-ACCESS"):
            self.assertNotIn(secret, printed)  # the answer only: no key, signature or header

    def test_other_shapes_are_reported_as_they_are(self):
        for answer, reads in ((lambda i: {"code": "51400", "msg": "", "data": []}, "nothing to cancel (51400)"),
                              (lambda i: {"code": "1", "data": [{"sCode": "51410", "clOrdId": i}]}, "another code: 51410")):
            report = tool.check(self.fx.root, "demo", transport=self.transport(answer))
            self.assertEqual(report["r218_reads_it_as"], reads)

    def test_an_unknown_profile_is_named_not_a_stop_iteration(self):
        with self.assertRaises(SystemExit) as caught:
            tool.check(self.fx.root, "nope", transport=self.transport(lambda i: {"code": "0", "data": []}))
        self.assertIn("nope", str(caught.exception))
        self.assertIn("demo", str(caught.exception))
        self.assertEqual(self.sent, [])

    def test_a_live_profile_is_refused_before_any_request(self):
        with self.assertRaises(SystemExit):
            tool.check(self.fx.root, "arena-a", transport=self.transport(lambda i: {"code": "0", "data": []}))
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
