import base64
from copy import deepcopy
from decimal import Decimal
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from trading_lab.okx_readonly import Credentials, PreflightError
from trading_lab.one_trade import (OneTrade, OneTradeAPI, ROOT, SYMBOL, ask_jev, check,
                                   main, single_process)


class Exchange:
    """Stateful offline exchange: IOC fills, position changes, fees and order queries."""
    def __init__(self, root, fill="0.01"):
        self.site, self.environment, self.writes = "global", "live", True
        self.credentials = Credentials("unit-test-key", "unit-test-secret", "unit-test-pass")
        self.requests, self.now, self.position = 0, 1000.0, Decimal(0)
        self.orders, self.posts, self.gets = {}, [], []
        self.fill = Decimal(fill)
        self.algo = False
        self.lost_entry_response = self.lost_exit_response = self.interrupt_entry = False
        self.hide_orders = False
        self.exit_fills = []
        self.root = root

    def calibrate_clock(self):
        return {}

    def server_now(self):
        return 1000

    def sleep(self, seconds):
        self.now += seconds

    def get(self, path, params=None, private=False):
        self.requests += 1
        self.gets.append((path, params))
        if path.endswith("/config"):
            return [{"acctLv": "2", "posMode": "net_mode", "perm": "read_only,trade"}]
        if path.endswith("/positions"):
            return [{"instId": SYMBOL, "posSide": "net", "mgnMode": "isolated", "pos": str(self.position)}]
        if path.endswith("/orders-algo-pending"):
            return [{"instId": SYMBOL}] if self.algo else []
        if path.endswith("/orders-pending"):
            return []
        if path.endswith("/instruments"):
            return [{"instId": SYMBOL, "instType": "SWAP", "state": "live", "ctType": "linear",
                     "settleCcy": "USDT", "ctValCcy": "BTC", "ctVal": "0.01", "ctMult": "1",
                     "minSz": "0.01", "lotSz": "0.005", "tickSz": "0.1", "groupId": "4"}]
        if path.endswith("/balance"):
            return [{"details": [{"ccy": "USDT", "availBal": "250"}]}]
        if path.endswith("/trade-fee"):
            return [{"instType": "SWAP", "feeGroup": [{"groupId": "4", "maker": "-0.0002", "taker": "-0.0005"}]}]
        if path.endswith("/ticker"):
            return [{"instId": SYMBOL, "instType": "SWAP", "bidPx": "84000", "askPx": "84000.1", "ts": "1000000"}]
        if path.endswith("/leverage-info"):
            return [{"instId": SYMBOL, "mgnMode": "isolated", "lever": "1"}]
        if path.endswith("/order"):
            if self.hide_orders or params["clOrdId"] not in self.orders:
                raise PreflightError("okx_51603")
            return [deepcopy(self.orders[params["clOrdId"]])]
        raise AssertionError("Unexpected endpoint: " + path)

    def post(self, path, body):
        if not self.writes:
            raise AssertionError("Write attempted by read-only check")
        self.posts.append((path, deepcopy(body)))
        if path.endswith("/set-leverage"):
            self.asserted_1x = body == {"instId": SYMBOL, "lever": "1", "mgnMode": "isolated"}
            return [{"lever": "1"}]
        if path.endswith("/cancel-order"):
            return [{"sCode": "0"}]
        if not path.endswith("/order"):
            raise AssertionError(path)
        is_buy = body["side"] == "buy"
        if is_buy:
            filled, price = self.fill, Decimal("84000.1")
            self.position += filled
        else:
            if body.get("reduceOnly") is not True:
                raise AssertionError("Exit can open a short")
            filled = Decimal(self.exit_fills.pop(0)) if self.exit_fills else min(self.position, Decimal(body["sz"]))
            price = Decimal("84000")
            self.position -= filled
        order = {**body, "ordId": str(900 + len(self.orders)), "accFillSz": str(filled),
                 "avgPx": str(price) if filled else "", "feeCcy": "USDT",
                 "fee": str(-filled * Decimal("0.01") * price * Decimal("0.0005")),
                 "state": "filled" if filled == Decimal(body["sz"]) else "canceled", "uTime": "1000000"}
        self.orders[body["clOrdId"]] = order
        if is_buy and self.interrupt_entry:
            self.interrupt_entry = False
            raise KeyboardInterrupt()
        if (is_buy and self.lost_entry_response) or (not is_buy and self.lost_exit_response):
            raise PreflightError("network_or_response_error")
        return [{"sCode": "0", "ordId": order["ordId"]}]


class OneTradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.api = Exchange(self.root)
        self.jev_calls = 0

    def tearDown(self):
        self.temp.cleanup()

    def jev(self, root, plan):
        self.jev_calls += 1
        self.assertEqual(Decimal(plan["contracts"]), Decimal("0.01"))
        return {"choice": "run_test", "confidence": .9}

    def bot(self, jev=None):
        return OneTrade(self.root, self.api, jev=jev or self.jev,
                        sleep=self.api.sleep, timer=lambda: self.api.now, log=lambda s: None)

    def order_posts(self):
        return [body for path, body in self.api.posts if path.endswith("/order")]

    def test_full_cycle_and_restart_do_not_trade_again(self):
        result = self.bot().run()
        self.assertEqual(result["phase"], "DONE")
        self.assertEqual(result["remaining_contracts"], "0")
        self.assertEqual(result["pnl_excluding_funding_usdt"], "-0.008410005")
        self.assertEqual([r["side"] for r in self.order_posts()], ["buy", "sell"])
        self.assertTrue(self.api.asserted_1x)
        self.assertEqual(self.bot().run()["phase"], "DONE")
        self.assertEqual(len(self.order_posts()), 2)
        self.assertEqual(self.jev_calls, 1)

    def test_jev_skip_and_existing_position_or_algo_never_buy(self):
        self.api.position = Decimal("0.01")
        with self.assertRaises(PreflightError):
            self.bot().run()
        self.api.position = Decimal(0)
        self.api.algo = True
        with self.assertRaises(PreflightError):
            self.bot().run()
        self.api.algo = False
        result = self.bot(lambda r, p: {"choice": "skip"}).run()
        self.assertEqual(result["phase"], "SKIPPED")
        self.assertEqual(self.api.posts, [])

    def test_partial_entry_closes_only_actual_fill(self):
        self.api.fill = Decimal("0.005")
        result = self.bot().run()
        self.assertEqual(result["bought_contracts"], "0.005")
        self.assertEqual(self.order_posts()[1]["sz"], "0.005")
        self.assertEqual(self.api.position, 0)

    def test_partial_close_only_reduces_remaining_quantity(self):
        self.api.exit_fills = ["0.005"]
        self.assertEqual(self.bot().run()["phase"], "DONE")
        self.assertEqual([Decimal(b["sz"]) for b in self.order_posts()], [Decimal("0.01"), Decimal("0.01"), Decimal("0.005")])
        self.assertTrue(all(b["reduceOnly"] for b in self.order_posts()[1:]))

    def test_unfilled_ioc_does_not_sell_or_reenter(self):
        self.api.fill = Decimal(0)
        self.assertEqual(self.bot().run()["phase"], "NO_FILL")
        self.assertEqual(self.bot().run()["phase"], "NO_FILL")
        self.assertEqual(len(self.order_posts()), 1)

    def test_lost_acknowledgements_are_reconciled_without_duplicate_orders(self):
        self.api.lost_entry_response = self.api.lost_exit_response = True
        self.assertEqual(self.bot().run()["phase"], "DONE")
        self.assertEqual(len(self.order_posts()), 2)

    def test_restart_after_interrupted_buy_only_closes_existing_entry(self):
        self.api.interrupt_entry = True
        with self.assertRaises(KeyboardInterrupt):
            self.bot().run()
        self.assertGreater(self.api.position, 0)
        self.assertEqual(self.bot().run()["phase"], "DONE")
        self.assertEqual(len(self.order_posts()), 2)
        self.assertEqual(self.jev_calls, 1)

    def test_unresolved_entry_is_never_reposted_then_recovers_when_visible(self):
        self.api.hide_orders = True
        with self.assertRaises(PreflightError):
            self.bot().run()
        with self.assertRaises(PreflightError):
            self.bot().run()
        self.assertEqual(len(self.order_posts()), 1)
        self.api.hide_orders = False
        self.assertEqual(self.bot().run()["phase"], "DONE")
        self.assertEqual(len(self.order_posts()), 2)

    def test_account_key_change_blocks_recovery(self):
        self.api.interrupt_entry = True
        with self.assertRaises(KeyboardInterrupt):
            self.bot().run()
        self.api.credentials = Credentials("different-key", "test-secret", "test-pass")
        with self.assertRaisesRegex(PreflightError, "different_account"):
            self.bot().run()
        self.assertEqual(len(self.order_posts()), 1)

    def test_foreign_position_change_is_not_liquidated_by_recovery(self):
        self.api.interrupt_entry = True
        with self.assertRaises(KeyboardInterrupt):
            self.bot().run()
        self.api.position = Decimal("0.03")
        with self.assertRaisesRegex(PreflightError, "position_changed_outside_bot"):
            self.bot().run()
        self.assertEqual(len(self.order_posts()), 1)

    def test_check_mode_cannot_write_or_call_jev(self):
        self.api.writes = False
        with patch("trading_lab.one_trade.load_settings", return_value=("global", "live", {})), \
             patch("trading_lab.one_trade.credentials_from", return_value=self.api.credentials), \
             patch("trading_lab.one_trade.OneTradeAPI", return_value=self.api), \
             patch("builtins.print"):
            self.assertEqual(main(["--check"]), 0)
        self.assertEqual(self.api.posts, [])
        self.assertEqual(self.jev_calls, 0)

    def test_single_process_lock_rejects_concurrent_start(self):
        with single_process(self.root):
            with self.assertRaisesRegex(PreflightError, "already_running"):
                with single_process(self.root):
                    pass

    def test_jev_fixed_new_ledger_public_only_and_one_attempt(self):
        plan = check(self.api)[1]
        sent = []
        def reply(body, key):
            sent.append(json.loads(body))
            self.assertNotIn("250", json.dumps(sent[-1]))
            self.assertNotIn(self.api.credentials.key, body.decode())
            return {"answers": {"one_cycle": {"choice": "run_test", "confidence": .65}}, "usage": {"cost": .00004}}
        with patch("trading_lab.one_trade.read_key", return_value="jev-test-key"):
            result = ask_jev(self.root, plan, transport=reply)
            self.assertEqual(result["choice"], "run_test")  # No uncalibrated confidence trading threshold.
            self.assertEqual(ask_jev(self.root, plan, transport=reply)["choice"], "skip")
        self.assertEqual(len(sent), 1)
        self.assertFalse((self.root / "runtime" / "jev_ledger.sqlite3").exists())

    def test_transport_signature_body_and_read_only_write_guard(self):
        seen = []
        def wire(request):
            seen.append(request)
            if request.full_url.endswith("/time"):
                return {"code": "0", "data": [{"ts": "1000000"}]}
            return {"code": "0", "data": [{"sCode": "0"}]}
        api = OneTradeAPI("global", "live", self.api.credentials, transport=wire, clock=lambda: 1000, monotonic=lambda: 1000)
        body = {"instId": SYMBOL, "lever": "1", "mgnMode": "isolated"}
        with self.assertRaisesRegex(PreflightError, "writes_disabled"):
            api.post("/api/v5/account/set-leverage", body)
        self.assertEqual(seen, [])
        api.writes = True
        api.post("/api/v5/account/set-leverage", body)
        request = seen[-1]
        timestamp = request.get_header("Ok-access-timestamp")
        signed = timestamp + "POST/api/v5/account/set-leverage" + request.data.decode()
        expected = base64.b64encode(hmac.new(self.api.credentials.secret.encode(), signed.encode(), hashlib.sha256).digest()).decode()
        self.assertEqual(request.get_header("Ok-access-sign"), expected)
        with self.assertRaises(PreflightError):
            api.post("/api/v5/asset/withdrawal", {"instId": SYMBOL})
        with self.assertRaises(PreflightError):
            api.post("/api/v5/account/set-leverage", {**body, "lever": "3"})

    def test_jev_timeout_bad_choice_and_unknown_cost_do_not_allow_entry(self):
        plan = check(self.api)[1]
        cases = [None, {"answers": {"one_cycle": {"choice": "buy_more", "confidence": .9}}, "usage": {"cost": .0001}},
                 {"answers": {"one_cycle": {"choice": "run_test", "confidence": .9}}}]
        def timeout(body, key):
            raise TimeoutError()
        for transport in [timeout] + [lambda b, k, value=value: value for value in cases]:
            with tempfile.TemporaryDirectory() as folder, patch("trading_lab.one_trade.read_key", return_value="test-key"):
                self.assertEqual(ask_jev(folder, plan, transport=transport)["choice"], "skip")
        self.assertEqual(self.api.posts, [])

    def test_web_run_uses_shared_credentials_and_separate_journal(self):
        identity = "a" * 32
        observed = []
        def review(root, plan, *, credential_root):
            observed.append((root, credential_root))
            return self.jev(root, plan)
        with patch("trading_lab.one_trade.ROOT", self.root), \
             patch("trading_lab.one_trade.load_settings", return_value=("global", "live", {})), \
             patch("trading_lab.one_trade.credentials_from", return_value=self.api.credentials), \
             patch("trading_lab.one_trade.OneTradeAPI", return_value=self.api), \
             patch("trading_lab.one_trade.ask_jev", side_effect=review), patch("builtins.print"):
            self.assertEqual(main(["--live", "--web-run", identity]), 0)
            self.assertEqual(main(["--live", "--web-run", identity]), 0)
        run_root = self.root / "runtime" / "web_runs" / identity
        self.assertEqual(observed, [(run_root, self.root)])
        self.assertEqual(json.loads((run_root / "runtime" / "one_trade_v1.json").read_text())["phase"], "DONE")
        self.assertFalse((self.root / "runtime" / "one_trade_v1.json").exists())
        self.assertEqual(len(self.order_posts()), 2)


class ReplaceRetryTests(unittest.TestCase):
    """A transient Windows sharing lock (antivirus, indexer) on an atomic state write is waited out, at most 1 s."""

    def test_a_brief_lock_is_waited_out_and_a_lasting_one_still_raises(self):
        from trading_lab import one_trade
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / "a.tmp", Path(folder) / "a.json"
            source.write_text("x", encoding="utf-8")
            real, calls, slept = one_trade.os.replace, [], []
            def locked_twice(a, b):
                calls.append(1)
                if len(calls) <= 2:
                    raise PermissionError(5, "Access is denied")
                return real(a, b)
            with patch.object(one_trade.os, "replace", locked_twice), patch.object(one_trade.time, "sleep", slept.append):
                one_trade.replace_with_retry(source, target)
            self.assertEqual((len(calls), target.read_text(encoding="utf-8")), (3, "x"))
            def always(a, b):
                raise PermissionError(5, "Access is denied")
            slept.clear()
            with patch.object(one_trade.os, "replace", always), patch.object(one_trade.time, "sleep", slept.append):
                with self.assertRaises(PermissionError):
                    one_trade.replace_with_retry(target, source)
            self.assertLessEqual(sum(slept), 1.0)
            with patch.object(one_trade.os, "replace", side_effect=FileNotFoundError("gone")):
                with self.assertRaises(FileNotFoundError):  # only a sharing lock is retried
                    one_trade.replace_with_retry(target, source)


class CoreReplaceRetryTests(unittest.TestCase):
    """R2: the journals and checkpoints of every engine get the same bounded retry (ops_storage.atomic_write and
    arena.write)."""

    def test_atomic_write_and_arena_write_wait_out_a_brief_lock(self):
        from trading_lab import arena, ops_storage
        with tempfile.TemporaryDirectory() as folder:
            real, calls = ops_storage.os.replace, []
            def locked_once(a, b):
                calls.append(1)
                if len(calls) % 2:
                    raise PermissionError(5, "Access is denied")
                return real(a, b)
            with patch.object(ops_storage.os, "replace", locked_once), patch.object(ops_storage.time, "sleep", lambda _: None):
                ops_storage.atomic_write(Path(folder) / "index.json", b"{}")
                arena.write(Path(folder) / "state.json", {"ok": True})
            self.assertEqual(len(calls), 4)
            self.assertEqual(json.loads((Path(folder) / "state.json").read_text(encoding="utf-8")), {"ok": True})
            self.assertEqual((Path(folder) / "index.json").read_bytes(), b"{}")


if __name__ == "__main__":
    unittest.main()
