from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_one_trade import Exchange
from trading_lab.cycle_budget import LEDGER_NAME, MAX_DECISIONS
from trading_lab.jev import BudgetLedger
from trading_lab.one_trade import OneTradeAPI, SYMBOL, main
from trading_lab.okx_readonly import PreflightError
from trading_lab.position_cycle import (PositionCycle, STOP_FILE, candle_features, decide, market_snapshot)


def candles(now=6000):
    end = int(now // 60) * 60
    rows = []
    for i in range(30):
        p = 83700 + 10 * i
        rows.append([str((end - (30-i)*60)*1000), str(p), str(p+12), str(p-2), str(p+10), "100", "1", "84000", "1"])
    rows.append([str(end*1000), "84000", "84001", "84000", "84001", "2", ".02", "1600", "0"])
    return list(reversed(rows))


class MarketExchange(Exchange):
    def __init__(self, root):
        super().__init__(root)
        self.now = 6000.
        self.price = lambda elapsed: Decimal("84000")
        self.on_sleep = lambda: None
        self.bad_quote = False
        self.interrupt_add = False
        self.entry_fills = []

    def server_now(self):
        return self.now

    def sleep(self, seconds):
        super().sleep(seconds)
        self.on_sleep()

    def get(self, path, params=None, private=False):
        if path.endswith(("/candles", "/funding-rate", "/ticker")):
            self.requests += 1
            self.gets.append((path, params))
        if path.endswith("/candles"):
            return candles(self.now)
        if path.endswith("/funding-rate"):
            return [{"instId": SYMBOL, "ts": str(int(self.now*1000)), "fundingRate": ".0001", "fundingTime": str(int((self.now+3600)*1000))}]
        if path.endswith("/ticker"):
            if self.bad_quote and self.position:
                raise PreflightError("stale_or_future_quote")
            bid = self.price(self.now-6000)
            return [{"instId": SYMBOL, "instType":"SWAP", "bidPx":str(bid), "askPx":str(bid+Decimal(".1")), "ts":str(int(self.now*1000))}]
        return super().get(path, params, private)

    def post(self, path, body):
        if path.endswith("/order") and body["side"] == "buy" and self.entry_fills:
            self.fill = Decimal(self.entry_fills.pop(0))
        response = super().post(path, body)
        if path.endswith("/order"):
            record = self.orders[body["clOrdId"]]
            filled = Decimal(record["accFillSz"])
            price = self.price(self.now-6000) + (Decimal(".1") if body["side"] == "buy" else 0)
            record.update(avgPx=str(price) if filled else "", fee=str(-filled*Decimal(".01")*price*Decimal(".0005")), uTime=str(int(self.now*1000)))
            if self.interrupt_add and body["side"] == "buy" and body["clOrdId"].endswith("B1"):
                self.interrupt_add = False
                raise KeyboardInterrupt()
        return response


class PositionCycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.api = MarketExchange(self.root)
        self.choices = ["buy", "hold", "close"]
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def decision(self, root, market, stage):
        self.calls.append(deepcopy(market))
        choice = self.choices.pop(0) if self.choices else "hold"
        return {"choice": choice, "reason":"valid_market_choice", "cost_usd":.00003, "elapsed_ms":500}

    def bot(self, decision=None):
        return PositionCycle(self.root, self.api, decision=decision or self.decision,
                             sleep=self.api.sleep, timer=lambda:self.api.now, log=lambda _:None)

    def stop(self):
        path = self.root / "runtime" / STOP_FILE
        path.parent.mkdir(exist_ok=True)
        path.write_text('{}')

    def orders(self):
        return [b for p,b in self.api.posts if p.endswith("/order")]

    def test_entry_hold_close_uses_new_market_reviews_then_finishes(self):
        result = self.bot().run()
        self.assertEqual(result["phase"], "DONE")
        self.assertEqual(result["close_reason"], "jev_close")
        self.assertGreaterEqual(result["monitor"]["held_seconds"], 60)
        self.assertEqual([d["choice"] for d in result["decisions"]], ["buy", "hold", "close"])
        self.assertEqual([b["side"] for b in self.orders()], ["buy", "sell"])
        self.assertEqual(self.bot().run(), result)
        self.assertEqual(len(self.calls), 3)

    def test_stop_before_checks_or_during_entry_decision_never_buys(self):
        self.stop()
        self.assertEqual(self.bot().run()["phase"], "STOPPED_BEFORE_ENTRY")
        self.assertEqual(self.api.gets, [])
        self.assertEqual(self.calls, [])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def decision(*args):
                (root/"runtime"/STOP_FILE).write_text('{}')
                return {"choice":"buy"}
            result = PositionCycle(root,self.api,decision=decision).run()
            self.assertEqual(result["phase"], "STOPPED_BEFORE_ENTRY")
        self.assertEqual(self.orders(), [])

    def test_stop_while_waiting_closes_without_another_jev_call(self):
        self.api.on_sleep = lambda:self.stop() if self.api.now >= 6001 else None
        result = self.bot().run()
        self.assertEqual(result["phase"], "DONE")
        self.assertEqual(result["close_reason"], "user_stop")
        self.assertLess(self.api.now-6000, 2)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.api.position, 0)

    def test_stop_during_position_jev_overrides_add(self):
        self.api.price = lambda t:Decimal("84000") + (90 if t >= 29 else 0)
        def decision(root,market,stage):
            if stage == "position":
                self.stop()
                return {"choice":"add","reason":"valid_market_choice"}
            return {"choice":"buy","reason":"valid_market_choice"}
        result = self.bot(decision).run()
        self.assertEqual(result["close_reason"], "user_stop")
        self.assertEqual(len(result["entries"]), 1)
        self.assertEqual(self.api.position, 0)

    def test_adds_have_distinct_ids_weighted_entry_and_one_aggregate_close(self):
        self.choices = ["buy", "add", "add", "add", "close"]
        self.api.price = lambda t:Decimal("84000") + int((t+1e-6)//30)*90
        result = self.bot().run()
        self.assertEqual(result["phase"], "DONE")
        buys = [b for b in self.orders() if b["side"] == "buy"]
        self.assertEqual(len(buys), 3)
        self.assertEqual(len({b["clOrdId"] for b in buys}), 3)
        self.assertEqual(self.orders()[-1]["sz"], "0.03")
        self.assertEqual(result["average_entry_usdt"], "84090.1")
        self.assertLessEqual(Decimal(result["entry_notional_usdt"]), 30)
        self.assertEqual(self.api.position, 0)

    def test_stop_after_multiple_partial_entries_closes_actual_sum(self):
        self.choices = ["buy", "add"]
        self.api.entry_fills = ["0.005", "0.005"]
        self.api.price = lambda t:Decimal("84000") + (90 if t >= 29 else 0)
        self.api.on_sleep = lambda:self.stop() if len([b for b in self.orders() if b["side"] == "buy"]) == 2 else None
        result = self.bot().run()
        self.assertEqual(result["phase"], "DONE")
        self.assertEqual(result["close_reason"], "user_stop")
        self.assertEqual(Decimal(self.orders()[-1]["sz"]), Decimal("0.01"))
        self.assertEqual(self.api.position, 0)

    def test_add_rejected_when_position_not_profitable(self):
        self.choices = ["buy", "add", "close"]
        result = self.bot().run()
        self.assertEqual(len(result["entries"]), 1)
        self.assertFalse(self.calls[1]["may_add"])

    def test_stop_arriving_during_buy_submission_reconciles_then_closes(self):
        post=self.api.post
        def wire(path,body):
            answer=post(path,body)
            if path.endswith('/order') and body['side']=='buy': self.stop()
            return answer
        self.api.post=wire
        result=self.bot().run()
        self.assertEqual(result['phase'],'DONE')
        self.assertEqual(result['close_reason'],'user_stop')
        self.assertEqual([b['side'] for b in self.orders()],['buy','sell'])

    def test_empty_entry_and_empty_add_never_create_extra_sell_quantity(self):
        self.api.entry_fills=['0']
        result=self.bot().run()
        self.assertEqual(result['phase'],'NO_FILL')
        self.assertEqual(len(self.orders()),1)
        with tempfile.TemporaryDirectory() as directory:
            api=MarketExchange(Path(directory)); api.entry_fills=['.01','0']
            api.price=lambda t:Decimal('84000')+(90 if t>=29 else 0)
            choices=iter(['buy','add','close'])
            result=PositionCycle(directory,api,decision=lambda *args:{'choice':next(choices),'reason':'valid_market_choice'},sleep=api.sleep,timer=lambda:api.now,log=lambda _:None).run()
            self.assertEqual(result['phase'],'DONE')
            self.assertEqual(Decimal(result['bought_contracts']),Decimal('.01'))
            self.assertEqual(Decimal(result['sold_contracts']),Decimal('.01'))

    def test_initial_weak_trend_and_funding_window_cannot_enter(self):
        from trading_lab.okx_readonly import instrument_summary
        instrument=instrument_summary(self.api.get('/api/v5/public/instruments'), 'swap')
        market=market_snapshot(self.api,instrument)
        for changes, expected_calls in [({'baseline_candidate':False},2),({'seconds_to_funding':100},0)]:
            with tempfile.TemporaryDirectory() as directory:
                api=MarketExchange(Path(directory)); sent=[]
                def review(*args):
                    sent.append(1); return {'choice':'buy','reason':'valid_market_choice'}
                with patch('trading_lab.position_cycle.market_snapshot',return_value={**market,**changes}), patch('trading_lab.position_cycle.MAX_CYCLE_SECONDS',60):
                    result=PositionCycle(directory,api,decision=review,sleep=api.sleep,timer=lambda:api.now,log=lambda _:None).run()
                self.assertEqual(result['phase'],'SKIPPED')
                self.assertEqual(len(sent),expected_calls)
                self.assertEqual(api.posts,[])

    def test_cli_monitored_profile_is_wired_to_shared_credentials_offline(self):
        from trading_lab.position_cycle import PositionCycle as RealCycle
        identity='e'*32
        def cycle_factory(root,api,**kwargs):
            return RealCycle(root,api,**kwargs,sleep=api.sleep,timer=lambda:api.now,log=lambda _:None)
        def decision(root,market,stage,*,credential_root):
            self.assertEqual(credential_root,self.root)
            return self.decision(root,market,stage)
        with patch('trading_lab.one_trade.ROOT',self.root), \
             patch('trading_lab.one_trade.load_settings',return_value=('global','live',{})), \
             patch('trading_lab.one_trade.credentials_from',return_value=self.api.credentials), \
             patch('trading_lab.one_trade.OneTradeAPI',return_value=self.api), \
             patch('trading_lab.position_cycle.PositionCycle',side_effect=cycle_factory), \
             patch('trading_lab.position_cycle.decide',side_effect=decision),patch('builtins.print'):
            self.assertEqual(main(['--live','--strategy','monitored','--web-run',identity]),0)
        self.assertEqual([b['side'] for b in self.orders()],['buy','sell'])

    def test_stop_target_and_data_failure_close_without_extra_jev(self):
        for delta, reason in [(-200,"stop_loss"),(400,"take_profit")]:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                api = MarketExchange(Path(directory))
                api.price = lambda t:Decimal("84000") + (delta if t >= 4 else 0)
                result = PositionCycle(directory,api,decision=lambda *args:{"choice":"buy","reason":"valid_market_choice"},sleep=api.sleep,timer=lambda:api.now,log=lambda _:None).run()
                self.assertEqual(result["close_reason"], reason)
                self.assertEqual(result["phase"], "DONE")
                self.assertEqual(api.position, 0)
        self.api.bad_quote = True
        result = self.bot().run()
        self.assertEqual(result["phase"], "DONE")
        self.assertEqual(result["last_error"], "stale_or_future_quote")

    def test_jev_failure_and_time_limit_exit(self):
        self.choices = ["buy", "INVALID"]
        self.assertEqual(self.bot().run()["close_reason"], "jev_close")
        with tempfile.TemporaryDirectory() as directory:
            api = MarketExchange(Path(directory))
            result = PositionCycle(directory,api,decision=lambda r,m,s:{"choice":"buy" if s=="entry" else "hold","reason":"valid_market_choice"},sleep=api.sleep,timer=lambda:api.now,log=lambda _:None).run()
            self.assertIn(result["close_reason"], {"time_limit","request_reserve"})
            self.assertLessEqual(api.now-6000, 301)
            self.assertEqual(api.position, 0)

    def test_restart_after_ambiguous_add_recovers_all_entries_without_rebuy(self):
        self.choices = ["buy", "add"]
        self.api.price = lambda t:Decimal("84000") + (90 if t >= 29 else 0)
        self.api.interrupt_add = True
        # Simulate process loss with recovery also unavailable; then restart against visible orders.
        bot = self.bot()
        with patch.object(bot, "recover", side_effect=PreflightError("network_down")):
            with self.assertRaises(PreflightError):
                bot.run()
        self.assertEqual(self.api.position, Decimal(".02"))
        result = self.bot().run()
        self.assertEqual(result["phase"], "DONE")
        self.assertEqual([b["side"] for b in self.orders()], ["buy", "buy", "sell"])
        self.assertEqual(len(self.calls), 2)

    def test_foreign_position_is_not_closed(self):
        self.api.on_sleep = lambda:setattr(self.api, "position", Decimal(".025"))
        with self.assertRaisesRegex(PreflightError,"position_changed_outside_bot"):
            self.bot().run()
        self.assertEqual([b["side"] for b in self.orders()], ["buy"])

    def test_partial_close_and_aggregate_write_guard(self):
        self.choices = ["buy", "add", "close"]
        self.api.price = lambda t:Decimal("84000") + (90 if t >= 29 else 0)
        self.api.exit_fills = [".005", ".005"]
        result = self.bot().run()
        self.assertEqual(result["phase"], "DONE")
        self.assertEqual([Decimal(b["sz"]) for b in self.orders() if b["side"] == "sell"], [Decimal("0.02"),Decimal("0.015"),Decimal("0.01")])
        api = OneTradeAPI("global","live",self.api.credentials,writes=True)
        close = self.orders()[-1]
        with patch.object(api,"request",return_value=[]):
            api.post("/api/v5/trade/order",{**close,"sz":".03"})
            for body in ({**close,"sz":".031"},{**self.orders()[0],"sz":".02"}):
                with self.assertRaises(PreflightError):
                    api.post("/api/v5/trade/order",body)


    def test_skip_rechecks_then_buys_and_persists_every_step(self):
        self.choices = ["skip", "buy", "hold", "close"]
        state = self.bot().run()
        self.assertEqual(state["phase"], "DONE")
        self.assertEqual([d["choice"] for d in state["decisions"]], ["skip", "buy", "hold", "close"])
        self.assertEqual([o["side"] for o in self.orders()], ["buy", "sell"])
        codes = [e["code"] for e in state["events"]]
        self.assertIn("entry_deferred", codes)
        self.assertEqual(sum(e["code"] == "clock" and e["status"] == "passed" for e in state["events"]), 2)
        self.assertEqual({e["step"] for e in state["events"]}, {1, 2, 3, 4, 5, 6})
        self.assertEqual([e["seq"] for e in state["events"]], list(range(1, len(state["events"]) + 1)))
        saved = json.loads((self.root/"runtime/one_trade_v1.json").read_text())
        self.assertEqual(saved["events"], state["events"])
        self.assertLess((self.root/"runtime/one_trade_v1.json").stat().st_size, 2097152)
        self.assertEqual(len(state["code_sha256"]), 64)
        reconciled_buys = [e for e in state["events"] if e["code"] == "order_reconcile" and e["data"].get("direction") == "buy"]
        self.assertEqual([e["step"] for e in reconciled_buys], [3, 6])

    def test_stop_during_entry_wait_never_buys_or_pays_again(self):
        self.choices = ["skip", "buy"]
        self.api.on_sleep = lambda: self.stop() if self.api.now >= 6001 else None
        state = self.bot().run()
        self.assertEqual(state["phase"], "STOPPED_BEFORE_ENTRY")
        self.assertEqual(state["close_reason"], "user_stop")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.orders(), [])
        self.assertEqual(sum(e["code"] == "stop_observed" for e in state["events"]), 1)
        self.assertLess(self.api.now - 6000, 2)

    def test_stop_during_preflight_does_not_continue_to_next_check_or_jev(self):
        get = self.api.get
        def stop_after_config(path, *args, **kwargs):
            reply = get(path, *args, **kwargs)
            if path.endswith("/config"):
                self.stop()
            return reply
        self.api.get = stop_after_config
        state = self.bot().run()
        self.assertEqual(state["phase"], "STOPPED_BEFORE_ENTRY")
        self.assertNotIn("last_error", state)
        self.assertFalse(any(path.endswith("/instruments") for path, params in self.api.gets))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.api.posts, [])

    def test_all_skips_end_at_deadline_without_orders(self):
        self.choices = ["skip"] * 20
        state = self.bot().run()
        self.assertEqual(state["phase"], "SKIPPED")
        self.assertEqual(state["close_reason"], "entry_time_limit")
        self.assertLessEqual(len(self.calls), MAX_DECISIONS)
        self.assertLessEqual(self.calls[8]["remaining_cycle_seconds"], 60)
        self.assertLessEqual(self.api.now - 6000, 300.01)
        self.assertEqual(self.api.posts, [])

    def test_decision_error_is_terminal_not_an_automatic_paid_retry(self):
        count = []
        def failed(*args):
            count.append(1)
            return {"choice":"skip", "reason":"jev_request_failed", "attempted":True}
        state = self.bot(failed).run()
        self.assertEqual(len(count), 1)
        self.assertEqual(state["close_reason"], "jev_request_failed")
        self.assertEqual(self.api.posts, [])

    def test_shared_decision_budget_limits_waiting_reviews(self):
        self.choices = ["skip"] * 10
        with patch("trading_lab.position_cycle.MAX_DECISIONS", 2):
            state = self.bot().run()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(state["close_reason"], "decision_limit")
        self.assertEqual(self.api.posts, [])

    def test_late_jev_buy_after_deadline_never_sets_leverage_or_submits(self):
        def late(*args):
            self.api.now += 301
            return {"choice":"buy", "reason":"valid_market_choice"}
        state = self.bot(late).run()
        self.assertEqual(state["phase"], "SKIPPED")
        self.assertEqual(self.api.posts, [])

    def test_waiting_time_does_not_restart_the_total_cycle_clock(self):
        self.choices = ["skip"] * 8 + ["buy", "hold", "hold"]
        state = self.bot().run()
        self.assertEqual(state["phase"], "DONE")
        self.assertLessEqual(self.api.now - 6000, 300.01)
        self.assertLessEqual(float(state["monitor"]["held_seconds"]), 60)
        self.assertEqual(self.api.position, 0)
        self.assertLessEqual(len(self.calls), MAX_DECISIONS)

    def test_repeated_preflight_detects_foreign_position_without_closing_it(self):
        self.choices = ["skip", "buy"]
        self.api.on_sleep = lambda: setattr(self.api, "position", Decimal(".02"))
        state = self.bot().run()
        self.assertEqual(state["last_error"], "position_already_exists")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.api.posts, [])
        self.assertEqual(self.api.position, Decimal(".02"))
        self.assertTrue(any(e["code"] == "no_position" and e["status"] == "failed" for e in state["events"]))


class MarketAndDecisionTests(unittest.TestCase):
    def test_completed_candles_only_and_bad_market_data_rejected(self):
        parsed = candle_features(candles(),6000)
        self.assertTrue(parsed["baseline_candidate"])
        self.assertEqual(parsed["closed_bars"],30)
        self.assertEqual(parsed["last_close_usdt"],"84000")
        for bad in [candles()[:-1],candles()+[candles()[1]],candles(5700),candles(6300)]:
            with self.assertRaises(PreflightError):
                candle_features(bad,6000)
        broken = candles(); broken[3][4]="NaN"
        with self.assertRaises(PreflightError):
            candle_features(broken,6000)

    def test_decision_limits_public_allowlist_and_no_retries(self):
        with tempfile.TemporaryDirectory() as directory, patch("trading_lab.position_cycle.read_key",return_value="FAKE_KEY"):
            root=Path(directory); run=root/"runtime"/"web_runs"/("a"*32)
            seen=[]
            def transport(body,key):
                seen.append(json.loads(body))
                return {"answers":{"position_action":{"choice":"hold","confidence":.4}},"usage":{"cost":.00003}}
            market={"instrument":SYMBOL,"secret":"DO_NOT_SEND","balance":"DO_NOT_SEND","baseline_candidate":True}
            for i in range(MAX_DECISIONS+1):
                result=decide(run,market,"position",credential_root=root,transport=transport,clock=lambda:1000+i*31)
            self.assertEqual(result["choice"],"close")
            self.assertEqual(len(seen),MAX_DECISIONS)
            self.assertNotIn("DO_NOT_SEND",json.dumps(seen))
            self.assertFalse((run/"runtime"/"jev_one_trade_v1.sqlite3").exists())

    def test_unknown_cost_global_budget_timeout_or_invalid_answer_fails_closed(self):
        cases=[{}, {"answers":{"position_action":{"choice":"add","confidence":.5}}},
               {"answers":{"position_action":{"choice":"buy","confidence":.5}},"usage":{"cost":.00003}},
               {"answers":{"position_action":{"choice":"hold","confidence":.5}},"usage":{"cost":.006}}]
        for payload in cases:
            with tempfile.TemporaryDirectory() as directory, patch("trading_lab.position_cycle.read_key",return_value="fake"):
                root=Path(directory); run=root/"runtime"/"web_runs"/("a"*32)
                result=decide(run,{},"position",credential_root=root,transport=lambda *args:payload)
                self.assertEqual(result["choice"],"close")
        with tempfile.TemporaryDirectory() as directory, patch("trading_lab.position_cycle.read_key",return_value="fake"):
            root=Path(directory); run=root/"runtime"/"web_runs"/("b"*32)
            ledger=BudgetLedger(run/"runtime"/LEDGER_NAME)
            ledger.reserve("unknown",1000)
            result=decide(root,{},"entry",credential_root=root,transport=lambda *args:self.fail("must not send"))
            self.assertEqual(result["choice"],"skip")
            self.assertFalse(result["attempted"])


if __name__ == "__main__":
    unittest.main()
