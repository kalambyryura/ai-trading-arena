"""R2.1.10 (D136 p. 3): A1, where a request failed and what the venue said; A2, the table of transient OKX codes."""
import http.client
import io
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
from urllib.parse import urlsplit

import test_exchange_lab as fixture
from trading_lab import exchange_api, exchange_diagnostics
from trading_lab.battle_review import safe_data
from trading_lab.exchange_api import ExchangeAPI, send
from trading_lab.okx_readonly import Credentials, PreflightError

URL = "https://openapi.okx.com/api/v5/trade/orders-algo-pending?ordType=conditional&instId=ETH-USDT-SWAP"


class Body(io.BytesIO):
    def __init__(self, raw=b"", fail=None):
        super().__init__(raw)
        self.fail = fail

    def read(self, *args):
        if self.fail:
            raise self.fail
        return super().read(*args)


def http_error(status, body):
    return urllib.error.HTTPError(URL, status, "reason", {}, body)


class Opener:
    def __init__(self, error):
        self.error = error

    def open(self, request, timeout):
        raise self.error


class SendTests(unittest.TestCase):
    def send(self, error):
        with patch.object(urllib.request, "build_opener", return_value=Opener(error)):
            with self.assertRaises(PreflightError) as caught:
                send(urllib.request.Request(URL, method="GET"))
        return caught.exception

    def test_a1_the_okx_code_of_an_http_error_body_is_read(self):
        error = self.send(http_error(503, Body(b'{"code":"50001","msg":"Service temporarily unavailable","data":[]}')))
        self.assertEqual(str(error), "http_503")  # the reason stays as before: unknown codes are classified as now
        self.assertEqual((error.http_status, error.okx_code), (503, "50001"))
        self.assertFalse(hasattr(error, "msg"))

    def test_a1_a_broken_body_never_hides_the_http_status(self):
        for body in (Body(fail=http.client.IncompleteRead(b"{")), Body(fail=TimeoutError()), Body(b"{not json"),
                     Body(b"[1,2]"), Body(b"[" * 100000 + b"]" * 100000), Body(b'{"code":"5000a"}'), Body(b'{"code":50001}'),
                     Body(b'{"code":"' + b"1" * (exchange_api.ERROR_BODY_BYTES + 10) + b'"}')):
            error = self.send(http_error(500, body))
            self.assertEqual((str(error), error.http_status, error.okx_code), ("http_500", 500, None))


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.ExchangeTests()
        self.fx.setUp()
        self.network = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network forbidden"))
        self.network.start()
        self.answer = None

    def tearDown(self):
        self.network.stop()
        self.fx.tearDown()

    def transport(self, request):
        """The fake exchange, but one endpoint answers self.answer = (path, status, payload)."""
        path, status, payload = self.answer or (None, None, None)
        if urlsplit(request.full_url).path == path:
            request.http_status = status
            return payload
        request.http_status = 200
        return self.fx.fakes["demo"].transport(request)

    def api(self):
        return ExchangeAPI("global", "demo", Credentials("FAKE_KEY", "FAKE_SECRET", "FAKE_PASS"),
                           transport=self.transport, clock=self.fx.clock, monotonic=self.fx.clock)

    def test_a1_a_code_without_data_is_the_okx_code_with_its_endpoint_and_status(self):
        self.answer = ("/api/v5/trade/order", 200, {"code": "51290", "msg": "The Bot engine is being upgraded"})
        with self.assertRaises(PreflightError) as caught:  # before R2.1.10: invalid_provider_envelope
            self.api().get("/api/v5/trade/order", {"instId": "ETH-USDT-SWAP", "ordId": "123456", "clOrdId": "J5x"}, private=True)
        error = caught.exception
        self.assertEqual(str(error), "okx_error_51290")
        self.assertEqual((error.endpoint, error.method, error.http_status, error.okx_code),
                         ("/api/v5/trade/order", "GET", 200, "51290"))  # the path only: the query holds ordId and clOrdId
        facts = exchange_diagnostics.venue_facts(error)
        self.assertEqual(facts, {"endpoint": "/api/v5/trade/order", "method": "GET", "http_status": 200, "okx_code": "51290",
                                 "transient": True})
        self.assertNotIn("msg", repr(facts))

    def test_a1_a_local_refusal_names_its_endpoint_without_a_status(self):
        api = self.api()
        api.requests = api.request_limit
        with self.assertRaises(PreflightError) as caught:
            api.get("/api/v5/account/positions", {"instType": "SWAP"}, private=True)
        self.assertEqual((str(caught.exception), caught.exception.endpoint, caught.exception.http_status),
                         ("exchange_request_limit", "/api/v5/account/positions", None))

    def test_a1_an_unknown_status_from_a_fake_transport_stays_unknown(self):
        def bare(request):
            return {"code": "50013", "msg": "", "data": []}
        api = ExchangeAPI("global", "demo", Credentials("FAKE_KEY", "FAKE_SECRET", "FAKE_PASS"), transport=bare,
                          clock=self.fx.clock, monotonic=self.fx.clock)
        with self.assertRaises(PreflightError) as caught:
            api.get("/api/v5/public/time")
        self.assertIsNone(caught.exception.http_status)
        self.assertIsNone(exchange_diagnostics.transient(caught.exception))  # a pair, never the code alone

    def test_a1_the_account_check_event_carries_the_facts_and_the_bundle_exports_them(self):
        state = self.fx.start()
        api = self.fx.lab.resources[state["id"]]["apis"][0]
        api.transport = self.transport
        self.answer = ("/api/v5/account/positions", 503, {"code": "50001", "msg": "x", "data": []})
        self.fx.lab.step(state)
        event = next(e for e in state["events"] if e["code"] == "exchange_check_failed")
        self.assertEqual({k: event["data"].get(k) for k in ("reason", "endpoint", "method", "http_status", "okx_code", "transient")},
                         {"reason": "okx_error_50001", "endpoint": "/api/v5/account/positions", "method": "GET",
                          "http_status": 503, "okx_code": "50001", "transient": True})
        exported = safe_data(event["data"], event_code="exchange_check_failed")
        self.assertEqual({k: exported.get(k) for k in ("endpoint", "method", "http_status", "okx_code", "transient")},
                         {"endpoint": "/api/v5/account/positions", "method": "GET", "http_status": 503, "okx_code": "50001",
                          "transient": True})

    def test_a1_export_drops_what_is_not_an_allowlisted_fact(self):
        exported = safe_data({"reason": "http_500", "stage": "account_check", "endpoint": URL, "method": "DELETE",
                              "http_status": "500", "okx_code": "5x", "transient": "yes", "msg": "secret"},
                             event_code="exchange_check_failed")
        self.assertEqual(exported, {"reason": "http_500", "stage": "account_check"})
        # an older journal's event has none of the new keys: its export is as before
        self.assertEqual(safe_data({"reason": "okx_error_51290", "stage": "account_check"}, event_code="exchange_check_failed"),
                         {"reason": "okx_error_51290", "stage": "account_check"})


class TableTests(unittest.TestCase):
    def failure(self, code, status, method):
        error = PreflightError("okx_error_" + code)
        error.okx_code, error.http_status, error.method = code, status, method
        return error

    def test_a2_the_table_is_exactly_d136(self):
        table = exchange_diagnostics.TRANSIENT_CODES
        self.assertEqual(set(table), {("50001", 503), ("50004", 400), ("50011", 200), ("50011", 429), ("50013", 429),
                                      ("50026", 500), ("50040", 200), ("51054", 500), ("51290", 200)})
        self.assertEqual({k for k, v in table.items() if not v["extension"]}, {("51290", 200)})
        self.assertEqual({k for k, v in table.items() if v["methods"] == {"GET"}}, {("50004", 400), ("51054", 500)})
        self.assertTrue(all(v["text"] and v["text"] == v["text"].strip() for v in table.values()))
        self.assertIn("docs-v5", exchange_diagnostics.OKX_ERROR_SOURCE)

    def test_a2_only_a_listed_pair_is_transient(self):
        transient = exchange_diagnostics.transient
        self.assertIsNotNone(transient(self.failure("51290", 200, "GET")))
        self.assertIsNotNone(transient(self.failure("50004", 400, "GET")))
        self.assertIsNone(transient(self.failure("50004", 400, "POST")))  # UNKNOWN, as before
        self.assertIsNone(transient(self.failure("51054", 500, "POST")))
        self.assertIsNone(transient(self.failure("50001", 500, "GET")))  # the code with another status
        for code in ("51149", "51412", "50017", "50018", "50019", "50020", "50021", "50022", "59999"):
            self.assertIsNone(transient(self.failure(code, 200, "GET")), code)
            self.assertIsNone(transient(self.failure(code, 500, "GET")), code)


if __name__ == "__main__":
    unittest.main()
