from copy import deepcopy
import base64
import hashlib
import hmac
import http.client
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
from urllib.parse import urlsplit
import urllib.request

from trading_lab.okx_readonly import (Credentials, MAX_REQUESTS, OkxReader, PreflightError,
                                    config_summary, decimal_value, fee_summary, instrument_summary,
                                    load_settings, preflight, send_get, signature)


def envelope(rows):
    return {"code": "0", "data": rows}


INSTRUMENT = {"instType": "SWAP", "instId": "BTC-USDT-SWAP", "state": "live", "ctType": "linear",
              "ctVal": "0.01", "ctMult": "1", "ctValCcy": "BTC", "settleCcy": "USDT",
              "lotSz": "0.01", "minSz": "0.01", "tickSz": "0.1", "groupId": "4"}
TICKER = {"instType": "SWAP", "instId": "BTC-USDT-SWAP", "bidPx": "84000", "askPx": "84000.1", "ts": "1053000"}
CONFIG = {"perm": "read_only", "acctLv": "2", "posMode": "net_mode", "enableSpotBorrow": False,
          "apikey": "do-not-save-this", "uid": "private-id", "ip": "private-address", "label": "private-label"}
FEE = {"instType": "SWAP", "feeGroup": [{"groupId": "4", "maker": "-0.0002", "taker": "-0.0005"}],
       "makerU": "-0.009", "takerU": "-0.009"}


class OkxReadonlyTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.responses = {
            "/api/v5/public/time": envelope([{"ts": "1053000"}]),
            "/api/v5/public/instruments": envelope([deepcopy(INSTRUMENT)]),
            "/api/v5/market/ticker": envelope([deepcopy(TICKER)]),
            "/api/v5/account/config": envelope([deepcopy(CONFIG)]),
            "/api/v5/account/instruments": envelope([deepcopy(INSTRUMENT)]),
            "/api/v5/account/trade-fee": envelope([deepcopy(FEE)]),
            "/api/v5/account/balance": envelope([{"details": [{"ccy": "USDT", "cashBal": "20", "availBal": "20"}]}]),
            "/api/v5/account/positions": envelope([]),
            "/api/v5/trade/orders-pending": envelope([]),
        }
        self.credentials = Credentials("fixture-key", "fixture-secret", "fixture-passphrase")
        self.reader = OkxReader(credentials=self.credentials, transport=self.transport,
                                clock=lambda: 1000, monotonic=lambda: 100)

    def transport(self, request):
        self.requests.append(request)
        return self.responses[urlsplit(request.full_url).path]

    def test_public_check_uses_server_clock_and_never_sends_credentials(self):
        result = preflight(self.reader)
        self.assertEqual(result["status"], "public_checks_passed")
        self.assertEqual(result["clock"]["server_minus_local_seconds"], 53)
        self.assertEqual(result["quote"]["reference_minimum_notional_usdt"], "8.40001")
        self.assertEqual(result["requests_attempted"], 3)
        for request in self.requests:
            self.assertEqual(request.get_method(), "GET")
            self.assertIsNone(request.data)
            self.assertFalse(any(k.lower().startswith("ok-access") for k in request.headers))

    def test_private_get_signature_includes_query_and_corrected_utc_time(self):
        self.reader.calibrate_clock()
        self.reader.get("/api/v5/account/balance", {"ccy": "BTC,USDT"}, private=True)
        headers = {k.lower(): v for k, v in self.requests[-1].headers.items()}
        self.assertEqual(headers["ok-access-timestamp"], "1970-01-01T00:17:33.000Z")
        message = b"1970-01-01T00:17:33.000ZGET/api/v5/account/balance?ccy=BTC%2CUSDT"
        expected = base64.b64encode(hmac.new(b"fixture-secret", message, hashlib.sha256).digest()).decode()
        self.assertEqual(headers["ok-access-sign"], expected)
        self.assertNotIn("fixture-secret", repr(self.requests[-1].headers))
        self.assertNotEqual(signature("fixture-secret", headers["ok-access-timestamp"], "/api/v5/account/balance"), expected)

    def test_full_read_only_preflight_sanitizes_identity_and_has_no_execution(self):
        result = preflight(self.reader, with_account=True)
        self.assertEqual(result["status"], "read_only_checks_passed")
        self.assertTrue(result["account_connected"])
        self.assertFalse(result["live_execution_enabled"])
        self.assertFalse(result["account"]["legal_or_trading_eligibility_verified"])
        self.assertEqual(result["orders_submitted"], 0)
        self.assertEqual(result["requests_attempted"], MAX_REQUESTS)
        self.assertEqual(result["account"]["fees"]["taker_signed_rate"], "-0.0005")
        encoded = json.dumps(result)
        for private_value in ("fixture-key", "fixture-secret", "fixture-passphrase", "private-id", "private-address", "private-label", "do-not-save-this"):
            self.assertNotIn(private_value, encoded)
        self.assertTrue(all(request.get_method() == "GET" for request in self.requests))

    def test_missing_read_or_unknown_permissions_stop_private_diagnostics(self):
        for permission in ("trade", "withdraw", "", "read_only,unknown"):
            self.responses["/api/v5/account/config"]["data"][0]["perm"] = permission
            reader = OkxReader(credentials=self.credentials, transport=self.transport, clock=lambda: 1000, monotonic=lambda: 100)
            result = preflight(reader, with_account=True)
            self.assertEqual(result["reason"], "read_permission_missing_or_unknown_scopes")
            self.assertEqual(result["requests_attempted"], 4)
            self.assertFalse(result["account_connected"])

    def test_broader_key_is_reported_but_only_read_permissions_are_used(self):
        self.responses["/api/v5/account/config"]["data"][0]["perm"] = "read_only,trade,withdraw"
        result = preflight(self.reader, with_account=True)
        self.assertEqual(result["status"], "read_only_checks_passed")
        self.assertEqual(result["account"]["unused_permissions"], ["trade", "withdraw"])
        self.assertEqual(result["account"]["permission_used"], "read_only")
        self.assertTrue(all(request.get_method() == "GET" for request in self.requests))
        self.assertFalse(result["live_execution_enabled"])
        with self.assertRaises(PreflightError):
            self.reader.get("/api/v5/trade/order", private=True)

    def test_unavailable_swap_endpoint_keeps_balance_without_claiming_success(self):
        self.responses["/api/v5/account/config"]["data"][0]["acctLv"] = "1"
        self.responses["/api/v5/account/instruments"] = envelope([])
        self.responses["/api/v5/account/trade-fee"] = {"code": "51010", "msg": "private diagnostic", "data": []}
        result = preflight(self.reader, with_account=True)
        self.assertEqual(result["status"], "account_checks_incomplete")
        self.assertTrue(result["account_connected"])
        self.assertTrue(result["account"]["account_mode_setup_needed_for_swap"])
        self.assertEqual(result["account"]["balances"][0]["available_balance"], "20")
        self.assertEqual(result["account"]["checks"]["instrument"]["reason"], "instrument_not_listed_for_account")
        self.assertEqual(result["account"]["checks"]["fees"]["reason"], "okx_error_51010")
        self.assertNotIn("private diagnostic", json.dumps(result))
        self.assertEqual(result["requests_attempted"], MAX_REQUESTS)

    def test_no_arbitrary_host_path_write_or_private_endpoint_as_public(self):
        for site in ("https://attacker.invalid", "global/../", "GLOBAL"):
            with self.assertRaises(PreflightError):
                OkxReader(site=site)
        for path, private in (("/api/v5/trade/order", True), ("/api/v5/asset/transfer", True),
                              ("/api/v5/account/config", False), ("/api/v5/public/time?x=1", False)):
            with self.assertRaises(PreflightError):
                self.reader.get(path, private=private)
        for request in (urllib.request.Request("https://attacker.invalid/api/v5/public/time"),
                        urllib.request.Request("https://openapi.okx.com/api/v5/trade/order", data=b"{}"),
                        urllib.request.Request("http://openapi.okx.com/api/v5/public/time")):
            with self.assertRaises(PreflightError):
                send_get(request)
        self.assertEqual(len(self.requests), 0)

    def test_stale_and_future_quotes_block(self):
        for timestamp in ("1000000", "1056000"):
            self.responses["/api/v5/market/ticker"]["data"][0]["ts"] = timestamp
            result = preflight(self.reader)
            self.assertEqual(result["reason"], "stale_or_future_quote")

    def test_expired_anchor_blocks_but_wall_skew_and_jumps_are_diagnostic_only(self):
        self.reader.calibrate_clock()
        self.reader.monotonic = lambda: 131
        with self.assertRaisesRegex(PreflightError, "expired"):
            self.reader.get("/api/v5/account/config", private=True)
        self.assertEqual(len(self.requests), 1)
        self.responses["/api/v5/public/time"] = envelope([{"ts": "1500000"}])
        self.assertEqual(self.reader.calibrate_clock()["server_minus_local_seconds"], 500)
        self.assertEqual(self.reader.server_now(), 1500)
        self.responses["/api/v5/public/time"] = envelope([{"ts": "1053000"}])
        times = iter([1000, 1060])
        self.reader.clock = lambda: next(times)
        self.assertEqual(self.reader.calibrate_clock()["local_clock_jump_seconds"], 60)
        self.assertEqual(self.reader.server_now(), 1053)

    def test_provider_errors_never_echo_messages_or_private_data(self):
        self.responses["/api/v5/account/config"] = {"code": "50102", "msg": "fixture-secret", "data": []}
        result = preflight(self.reader, with_account=True)
        self.assertEqual(result["reason"], "okx_error_50102")
        self.assertNotIn("fixture-secret", json.dumps(result))

    def test_invalid_numbers_and_contracts_are_rejected(self):
        for value in (True, float("nan"), "NaN", "Infinity", "1e999999", "0e999999", "1e-9999", "0" * 81):
            with self.assertRaises(PreflightError):
                decimal_value(value)
        for name, value in (("ctType", "inverse"), ("minSz", "0"), ("ctVal", "-1"),
                            ("instId", "OTHER"), ("settleCcy", "BTC")):
            row = deepcopy(INSTRUMENT)
            row[name] = value
            with self.assertRaises(PreflightError):
                instrument_summary([row], "swap")

    def test_demo_mode_is_explicit_and_credentials_are_not_represented(self):
        reader = OkxReader(environment="demo", transport=self.transport, clock=lambda: 1000, monotonic=lambda: 100)
        preflight(reader)
        self.assertTrue(all(request.headers.get("X-simulated-trading") == "1" for request in self.requests))
        self.assertEqual(repr(self.credentials), "Credentials()")
        with self.assertRaises(PreflightError):
            Credentials("injected\nheader", "secret", "pass")

    def test_fee_group_mismatch_or_missing_does_not_use_deprecated_rates(self):
        inst = instrument_summary([INSTRUMENT], "swap")
        for groups in ([], [{"groupId": "5", "taker": "-0.0005", "maker": "-0.0002"}]):
            row = deepcopy(FEE)
            row["feeGroup"] = groups
            with self.assertRaises(PreflightError):
                fee_summary([row], inst)

    def test_local_settings_environment_precedence_and_region_validation(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {}, clear=True):
            path = Path(folder) / ".env.okx.local"
            path.write_text("\n".join(["OKX_SITE" + "=eea", "OKX_ENV" + "=demo"]), encoding="utf-8")
            site, environment, _ = load_settings(folder)
            self.assertEqual((site, environment), ("eea", "demo"))
            with patch.dict("os.environ", {"OKX_SITE": "global"}):
                self.assertEqual(load_settings(folder)[0], "global")
            with patch.dict("os.environ", {"OKX_SITE": "https://other.invalid"}), self.assertRaises(PreflightError):
                load_settings(folder)

    def test_redirect_does_not_issue_second_request(self):
        error = urllib.error.HTTPError("https://openapi.okx.com/api/v5/public/time", 301, "Moved", {}, None)
        with patch("trading_lab.okx_readonly.urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaisesRegex(PreflightError, "http_301"):
                send_get(urllib.request.Request("https://openapi.okx.com/api/v5/public/time"))
            opener.return_value.open.assert_called_once()

    def test_incomplete_http_reply_returns_sanitized_error_without_retry(self):
        with patch("trading_lab.okx_readonly.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.side_effect = http.client.IncompleteRead(b"private-fragment")
            with self.assertRaisesRegex(PreflightError, "^network_error$"):
                send_get(urllib.request.Request("https://openapi.okx.com/api/v5/account/config"))
            opener.return_value.open.assert_called_once()


if __name__ == "__main__":
    unittest.main()
