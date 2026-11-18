from copy import deepcopy
from decimal import Decimal
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from trading_lab.jev import BudgetLedger
from trading_lab.one_trade import other_unfinished_cycle, single_process
from trading_lab.okx_readonly import PreflightError
from trading_lab.web_panel import Panel, PanelError, Server, public_cycle, read_balance, write_json
from trading_lab.position_cycle import STOP_FILE, PROFILE


def done_state():
    return {"phase": "DONE", "account_tag": "PRIVATE_ACCOUNT_TAG", "plan": {"base_btc": "0.0001", "contract_btc": "0.01"},
            "entry": {"request": {"clOrdId": "PRIVATE_ORDER_ID"}, "result": {"accFillSz": "0.01", "avgPx": "84000"}},
            "exits": [{"result": {"accFillSz": "0.01", "avgPx": "83999.9", "uTime": "1790673457755"}}],
            "remaining_contracts": "0", "signed_fees_usdt": "-0.0084", "pnl_excluding_funding_usdt": "-0.00841",
            "jev": {"choice": "run_test", "confidence": .05, "cost_usd": .00002, "elapsed_ms": 620}}


class FakeProcess:
    returncode = None

    def poll(self):
        return self.returncode


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.calls = []
        self.processes = []
        def launch(command, **kwargs):
            self.calls.append((command, kwargs))
            proc = FakeProcess()
            self.processes.append(proc)
            return proc
        self.launch = launch
        self.panel = Panel(self.root, launcher=launch)

    def tearDown(self):
        self.temp.cleanup()

    def journal(self, identity, state):
        write_json(self.panel.run_root(identity) / "runtime" / "one_trade_v1.json", state)

    def test_status_reads_never_start_worker_or_leak_identifiers(self):
        self.journal("legacy", done_state())
        for _ in range(3):
            result = self.panel.status()
            self.assertEqual(result["current"]["phase"], "DONE")
            self.assertEqual(result["current"]["pnl_usdt"], "-0.00841")
            self.assertNotIn("PRIVATE_", json.dumps(result))
        self.assertEqual(self.calls, [])

    def test_old_history_shows_only_existing_records_without_invented_checks(self):
        self.journal("legacy", done_state())
        history = self.panel.history("legacy")
        self.assertEqual(history["mode"], "legacy_partial")
        self.assertEqual([e["code"] for e in history["events"]], ["legacy_decision", "legacy_order", "legacy_order", "legacy_result"])
        self.assertNotIn("PRIVATE_", json.dumps(history))
        self.assertFalse(any(e["step"] == 1 for e in history["events"]))
        self.assertEqual(self.calls, [])
        for identity in ("../.env.okx.local", "f"*32, "legacy/.."):
            with self.assertRaises(LookupError):
                self.panel.history(identity)

    def test_new_history_includes_sanitized_checks_decisions_and_execution(self):
        state = done_state()
        state.update(version=3, profile=PROFILE, timeline_version=1, phase="WAITING_ENTRY",
                     code_sha256="a"*64, limits={"cycle_seconds":300,"secret":"PRIVATE_LIMIT"},
                     events=[{"seq":1, "at":"2026-09-29T11:00:00+00:00", "step":2, "code":"jev_decision",
                              "status":"passed", "data":{"choice":"skip","cost_usd":.00003,
                              "baseline_candidate":False,"secret":"PRIVATE_KEY","order_id":"PRIVATE_ORDER",
                              "bid_usdt":"NaN", "confidence":{"secret":"PRIVATE_OBJECT"}}}])
        self.journal("a"*32,state)
        history = self.panel.history("a"*32)
        self.assertEqual(history["mode"],"recorded_steps")
        self.assertEqual(history["cycle"]["phase"],"WAITING_ENTRY")
        self.assertEqual(history["events"][0]["data"],{"choice":"skip","cost_usd":"0.00003","baseline_candidate":False})
        self.assertEqual(history["limits"],{"cycle_seconds":"300"})
        self.assertNotIn("PRIVATE",json.dumps(history))
        self.assertEqual(self.calls,[])

    def test_v2_profile_remains_v2_in_history_after_v3_upgrade(self):
        state=done_state(); state["profile"]="jev-position-v2"
        self.assertEqual(public_cycle(state,"a"*32)["bot_id"],"jev-position-v2")

    def test_stop_is_persistent_cooperative_and_duplicate_cannot_launch_again(self):
        run = self.panel.play(0)
        result = self.panel.stop(1, run["id"])
        self.assertTrue(result["accepted"])
        self.assertFalse(result["closing_confirmed"])
        self.assertTrue((self.panel.run_root(run["id"])/"runtime"/STOP_FILE).exists())
        self.assertEqual(len(self.calls),1)
        self.assertTrue(self.panel.status()["current"]["stop_requested"])
        with self.assertRaises(PanelError):
            self.panel.stop(1,run["id"])
        self.assertEqual(len(self.calls),1)

    def test_stop_restarts_only_recovery_with_flag_already_written(self):
        run = self.panel.play(0)
        self.processes[0].returncode = 1
        def recovery(command, **kwargs):
            self.assertTrue((self.panel.run_root(run["id"])/"runtime"/STOP_FILE).exists())
            self.assertIn("monitored",command)
            return self.launch(command,**kwargs)
        self.panel.launcher = recovery
        self.panel.stop(1,run["id"])
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.calls[-1][0][-1],run["id"])
        self.assertEqual(len(self.panel.data["jobs"]),1)

    def test_stop_wrong_target_or_finished_cycle_never_starts_worker(self):
        self.journal("legacy",done_state())
        with self.assertRaises(PanelError):
            self.panel.stop(0,"legacy")
        run = self.panel.play(0)
        with self.assertRaises(PanelError):
            self.panel.stop(1,"legacy")
        self.assertEqual(len(self.calls),1)

    def test_broken_billing_does_not_block_stop(self):
        run=self.panel.play(0)
        path=self.panel.run_root(run["id"])/"runtime"/"jev_position_v2.sqlite3"
        path.write_text("broken fixture")
        state=self.panel.status()
        self.assertTrue(state["budget"]["unresolved"])
        self.assertTrue(state["can_stop"])
        self.assertTrue(self.panel.stop(1,run["id"])["accepted"])

    def test_multiple_entry_projection_and_all_decisions_costs_are_sanitized(self):
        state=done_state()
        state.update(profile=PROFILE, entries=[{"send_started":True,"result":{"accFillSz":".01","avgPx":"84000"}},
                       {"send_started":True,"result":{"accFillSz":".005","avgPx":"84200"}}],
                     average_entry_usdt="84066.666666", average_exit_usdt="84100", phase="HOLDING",
                     monitor={"estimated_close_pnl_usdt":".012", "secret":"PRIVATE_SECRET"},
                     decisions=[{"stage":"position","choice":"add","cost_usd":.00003,"market":{"return_5m_bps":"12","account":"PRIVATE_ACCOUNT"}}])
        result=public_cycle(state,"a"*32)
        self.assertEqual(Decimal(result["base_btc"]),Decimal(".00015"))
        self.assertEqual(result["entries_used"],2)
        self.assertEqual(result["buy_price"],"84066.666666")
        self.assertEqual(result["bot_id"],PROFILE)
        self.assertNotIn("PRIVATE",json.dumps(result))

    def test_duplicate_click_running_lock_and_new_click_are_distinct(self):
        first = self.panel.play(0)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.calls[0][1]["shell"])
        self.assertEqual(self.calls[0][0][-2:], ["--web-run", first["id"]])
        with self.assertRaises(PanelError):
            self.panel.play(0)
        with self.assertRaises(PanelError):
            self.panel.play(1)
        self.journal(first["id"], done_state())
        self.processes[0].returncode = 0
        self.assertFalse(self.panel.status()["running"])
        with self.assertRaises(PanelError):
            self.panel.play(0)
        second = self.panel.play(1)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(self.panel.status()["cycles"]), 2)

    def test_restart_recovers_same_incomplete_journal_not_new_entry(self):
        first = self.panel.play(0)
        state = done_state(); state["phase"] = "ENTERING"
        self.journal(first["id"], state)
        restarted = Panel(self.root, launcher=self.launch)
        self.assertEqual(restarted.status()["action"], "resume")
        resumed = restarted.play(1)
        self.assertEqual(first["id"], resumed["id"])
        self.assertEqual(len(restarted.data["jobs"]), 1)

    def test_legacy_and_orphan_unfinished_journals_are_never_bypassed(self):
        state = done_state(); state["phase"] = "CLOSING"
        self.journal("legacy", state)
        self.assertEqual(self.panel.play(0)["id"], "legacy")
        self.assertNotIn("--web-run", self.calls[0][0])
        self.processes[0].returncode = 0
        orphan = "a" * 32
        self.journal(orphan, state)
        self.assertFalse(self.panel.status()["can_play"])
        with self.assertRaises(PanelError):
            self.panel.play(1)

    def test_shared_process_lock_blocks_play_from_another_entrypoint(self):
        with single_process(self.root):
            self.assertTrue(self.panel.status()["running"])
            with self.assertRaises(PanelError):
                self.panel.play(0)
        self.assertEqual(self.calls, [])

    def test_unconfirmed_billing_blocks_new_cycle_but_allows_recovery(self):
        first = self.panel.play(0)
        ledger = BudgetLedger(self.panel.run_root(first["id"]) / "runtime" / "jev_one_trade_v1.sqlite3")
        ledger.reserve("test-hash", 1000)
        self.journal(first["id"], done_state())
        self.processes[0].returncode = 0
        self.assertTrue(self.panel.status()["budget"]["unresolved"])
        self.assertFalse(self.panel.status()["can_play"])
        state = done_state(); state["phase"] = "CLOSING"
        self.journal(first["id"], state)
        self.assertTrue(self.panel.status()["can_play"])
        self.assertEqual(self.panel.play(1)["id"], first["id"])

    def test_reported_cost_and_cycle_limits_survive_restart(self):
        for index in range(20):
            identity = f"{index:032x}"
            self.panel.data["jobs"].append({"id": identity, "started_at": "2026-09-29T12:00:00+00:00"})
            self.journal(identity, done_state())
        write_json(self.panel.path, self.panel.data)
        restarted = Panel(self.root, launcher=self.launch)
        self.assertFalse(restarted.status()["can_play"])
        self.assertEqual(self.calls, [])
        self.panel.data["jobs"] = self.panel.data["jobs"][:1]
        identity = self.panel.data["jobs"][0]["id"]
        ledger = BudgetLedger(self.panel.run_root(identity) / "runtime" / "jev_one_trade_v1.sqlite3")
        token = ledger.reserve("test", 1000)
        ledger.finish(token, .06, "skip")
        self.assertEqual(self.panel.accounting()["cost_usd"], "0.06")
        self.assertFalse(self.panel.status()["can_play"])

    def test_total_usage_includes_previous_research_and_new_runs(self):
        for filename in ("jev_ledger.sqlite3", "jev_okx_public_review_v1.sqlite3", "jev_one_trade_v1.sqlite3"):
            ledger = BudgetLedger(self.root / "runtime" / filename)
            token = ledger.reserve("test", 1000); ledger.finish(token, .00002, "ok")
        self.assertEqual(self.panel.status()["total_jev"], {"attempts": 3, "cost_usd": "0.00006", "unresolved": False})
        self.assertEqual(self.panel.accounting()["attempts"], 0)

    def test_balance_is_cached_and_never_requested_by_status(self):
        calls = []
        def balance(root):
            from trading_lab.web_panel import now
            calls.append(root)
            return {"updated_at": now(), "portfolio_equity_usd": "270", "available_usdt": "250"}
        panel = Panel(self.root, launcher=self.launch, balance_reader=balance)
        self.assertIsNone(panel.status()["balance"])
        self.assertEqual(calls, [])
        self.assertEqual(panel.refresh_balance()["available_usdt"], "250")
        panel.refresh_balance()
        self.assertEqual(len(calls), 1)

    def test_balance_adapter_is_read_only_and_sanitizes_account_data(self):
        class Reader:
            requests = 2
            def __init__(self, *args, **kwargs):
                self_writes = kwargs["writes"]
                if self_writes: raise AssertionError("writes must be false")
            def get(self, path, *, private):
                if path != "/api/v5/account/balance" or not private: raise AssertionError(path)
                return [{"totalEq": "270", "uid": "PRIVATE_UID", "details": [{"ccy": "USDT", "eq": "250", "availBal": "249"}]}]
        with patch("trading_lab.web_panel.load_settings", return_value=("global", "live", {})), \
             patch("trading_lab.web_panel.credentials_from", return_value=None), \
             patch("trading_lab.web_panel.OneTradeAPI", Reader):
            result = read_balance(self.root)
        self.assertEqual(result["available_usdt"], "249")
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_worker_guard_blocks_other_journals_and_allows_own_recovery(self):
        state = done_state(); state["phase"] = "ENTERING"
        self.journal("legacy", state)
        with patch("trading_lab.one_trade.ROOT", self.root):
            with self.assertRaises(PreflightError):
                other_unfinished_cycle(self.panel.run_root("a" * 32))
            other_unfinished_cycle(self.root)

    def test_http_only_explicit_same_origin_valid_play_can_start_fake_worker(self):
        # Isolated fixture, fake process launcher: HTTP tests cannot reach the real bot.
        web = self.root / "web"; web.mkdir()
        (web / "index.html").write_text('<meta name="panel-token" content="__PANEL_TOKEN__">', encoding="utf-8")
        (web / "arena.html").write_text('<meta name="panel-token" content="__PANEL_TOKEN__">', encoding="utf-8")
        with Server(self.panel, 0) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            def call(method, path, body=None, headers=None):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                connection.request(method, path, body=body, headers=headers or {})
                reply = connection.getresponse(); result = (reply.status, dict(reply.headers), reply.read())
                connection.close(); return result
            try:
                response = call("GET", "/")
                self.assertEqual(response[0], 200)
                self.assertIn(self.panel.token.encode(), response[2])
                self.assertEqual(response[1]["X-Frame-Options"], "DENY")
                self.assertEqual(call("GET", "/api/state")[0], 200)
                self.journal("legacy", done_state())
                history_response = call("GET", "/api/history/legacy")
                self.assertEqual(history_response[0], 200)
                self.assertEqual(json.loads(history_response[2])["mode"], "legacy_partial")
                self.assertNotIn(b"PRIVATE_", history_response[2])
                self.assertEqual(call("GET", "/api/history/"+"f"*32)[0],404)
                self.assertEqual(call("GET", "/api/history/../.env.okx.local")[0],404)
                self.assertEqual(call("GET", "/api/history/legacy", headers={"Host":"attacker.example"})[0],403)
                self.assertEqual(call("GET", "/api/play")[0], 404)
                self.assertEqual(call("GET", "/.env.okx.local")[0], 404)
                self.assertEqual(call("GET", "/api/state", headers={"Host":"attacker.example"})[0], 403)
                self.assertEqual(call("POST", "/api/play", '{}')[0], 403)
                self.assertEqual(call("GET", "/api/stop")[0], 404)
                self.assertEqual(call("POST", "/api/stop", '{}')[0], 403)
                self.assertEqual(self.calls, [])
                valid = {"Origin":server.origin, "X-Panel-Token":self.panel.token, "Content-Type":"application/json"}
                foreign = {**valid, "Origin":"https://external.example"}
                self.assertEqual(call("POST", "/api/play", '{"generation":0}', foreign)[0], 403)
                self.assertEqual(call("POST", "/api/play", '{"generation":0,"size":1000}', valid)[0], 503)
                self.assertEqual(call("POST", "/api/play", '{"generation":0}', valid)[0], 202)
                self.assertEqual(call("POST", "/api/play", '{"generation":0}', valid)[0], 409)
                self.assertEqual(len(self.calls), 1)
                identity=self.panel.data["active_id"]
                payload=json.dumps({"generation":1,"id":identity})
                self.assertEqual(call("POST","/api/stop",payload,foreign)[0],403)
                self.assertEqual(call("POST","/api/stop",payload,valid)[0],202)
                self.assertEqual(call("POST","/api/stop",payload,valid)[0],409)
                self.assertEqual(len(self.calls),1)
            finally:
                server.shutdown(); thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
