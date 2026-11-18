"""No exchange or paid calls: all market values and HTTP launchers are local fixtures."""
from copy import deepcopy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from trading_lab.arena import Arena
from trading_lab.arena_engine import new_season, request_stop, tick, failed_snapshot, public_season
from trading_lab.arena_market import PublicFeed, PATHS, build_snapshot, candle_features
from trading_lab.arena_strategies import ArenaError, catalogue_defaults, evaluate, validate_strategy
from trading_lab.web_panel import Panel, Server


def recipe(side="long"):
    result = catalogue_defaults()[0]
    result["rules"]["side"] = side
    result["rules"]["entry"]["conditions"] = [{"feature": "return_1m_bps", "op": "gt", "value": -999}]
    result["rules"]["exit"]["conditions"] = []
    return result


def payload():
    return {k: deepcopy(v) for k, v in recipe().items() if k in {"name", "author", "description", "original_text", "rules", "confirmed"}}


def market(now=1000, price=100, stamp=None):
    return {"at": now, "server_time": now, "quote_ts": (now if stamp is None else stamp) * 1000,
            "bid": price, "ask": price, "mid": price, "bid_btc": 10, "ask_btc": 10,
            "lot_btc": .01, "min_btc": .01, "features": {"return_1m_bps": 1, "spread_bps": 0},
            "funding_rate": .001, "funding_at": 3600, "funding_time_ms": 3600000,
            "sha256": "fixture", "raw": {"public_fixture": True}}


def state(side="long", duration=300):
    return new_season([recipe(side)], 100, duration, 1000, "fixture-code-hash")


def candles(now=1800000030, flat=False):
    last = int(now // 60) * 60 - 60
    result = []
    for i in range(30):
        close = 100 if flat else 100 + i
        result.append([str((last - (29-i)*60)*1000), str(close), str(close+1), str(close-1), str(close), "10", "1", "1", "1"])
    return list(reversed(result))


def raw_market(now=1800000030):
    return {"clock": [{"ts": str(now*1000)}], "candles": candles(now),
            "ticker": [{"instId":"BTC-USDT-SWAP","bidPx":"100","askPx":"100.1","bidSz":"1000","askSz":"1000","ts":str(now*1000)}],
            "instrument": [{"instId":"BTC-USDT-SWAP","state":"live","ctValCcy":"BTC","settleCcy":"USDT","ctVal":".01","ctMult":"1","lotSz":".01","minSz":".01"}],
            "funding": [{"instId":"BTC-USDT-SWAP","fundingTime":str((now+3600)*1000),"ts":str(now*1000),"fundingRate":".0001"}]}


class RecipeTests(unittest.TestCase):
    def test_templates_never_imply_received_human_or_model_submissions(self):
        items = catalogue_defaults()
        self.assertEqual(len([s for s in items if s["status"] == "draft"]), 5)
        self.assertTrue(all(s["author"]["name"] == "Codex" for s in items if s["status"] == "ready"))
        self.assertTrue(all(not s["original_text"] for s in items if s["status"] == "draft"))

    def test_confirmation_and_exact_schema_and_finite_bounds(self):
        cases = []
        p=payload();p["confirmed"]=False;cases.append(p)
        p=payload();p["execute"]="arbitrary-code";cases.append(p)
        p=payload();p["rules"]["size_pct"]=float("nan");cases.append(p)
        p=payload();p["rules"]["max_entries"]=1.5;cases.append(p)
        p=payload();p["rules"]["side"]="both";cases.append(p)
        p=payload();p["rules"]["entry"]["conditions"][0]["feature"]="private_balance";cases.append(p)
        p=payload();p["rules"]["size_pct"]=True;cases.append(p)
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ArenaError): validate_strategy(case)

    def test_model_authorship_requires_exact_provenance(self):
        p=payload();p["author"].update(kind="model",name="Submitted model",model="",prompt="",submitted_at="")
        with self.assertRaises(ArenaError):validate_strategy(p)
        p["author"].update(model="actual-model-version",prompt="shared original prompt",submitted_at="2026-09-29")
        self.assertEqual(validate_strategy(p)["author"]["model"],"actual-model-version")

    def test_missing_features_remain_unknown_and_cannot_pass_any(self):
        group={"mode":"any","conditions":[{"feature":"return_1m_bps","op":"gt","value":0},
                {"feature":"volume_ratio_5","op":"gt","value":1}]}
        passed, checks=evaluate(group,{"return_1m_bps":2})
        self.assertFalse(passed);self.assertIsNone(checks[-1]["passed"])


class MarketTests(unittest.TestCase):
    def test_only_completed_contiguous_past_bars_are_used(self):
        rows=candles();base=candle_features(rows,1800000030)
        self.assertEqual(base["rsi_14"],100)
        rows.insert(0,["1800000000000","999","999","999","999","999","1","1","0"])
        self.assertEqual(base,candle_features(rows,1800000030))
        rows[0][-1]="1"
        with self.assertRaises(ArenaError):candle_features(rows,1800000030)
        rows=candles();rows.pop(5)
        with self.assertRaises(ArenaError):candle_features(rows,1800000030)
        rows=candles();rows.append(rows[-1])
        with self.assertRaises(ArenaError):candle_features(rows,1800000030)

    def test_flat_rsi_and_undefined_volume_are_not_infinity(self):
        rows=candles(flat=True)
        for row in rows:row[5]="0"
        features=candle_features(rows,1800000030)
        self.assertEqual(features["rsi_14"],50);self.assertIsNone(features["volume_ratio_5"])

    def test_server_clock_offset_and_instrument_units(self):
        snap=build_snapshot(raw_market(),1800000000)
        self.assertAlmostEqual(snap["lot_btc"],.0001)
        self.assertEqual(snap["funding_at"],1800003600)
        self.assertEqual(snap["ask_btc"],10)
        self.assertEqual(len(snap["sha256"]),64)

    def test_stale_crossed_invalid_funding_and_foreign_contract_rejected(self):
        for key, field, value in (("ticker","ts","1"),("ticker","bidPx","101"),
                                  ("funding","fundingRate",""),("instrument","settleCcy","BTC"),
                                  ("ticker","askPx","NaN")):
            raw=raw_market();raw[key][0][field]=value
            with self.subTest(field=field),self.assertRaises(ArenaError):build_snapshot(raw,1800000000)

    def test_feed_uses_fixed_get_only_without_auth_or_environment(self):
        feed=PublicFeed();captured=[]
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,size):return b'{"code":"0","data":[{"ts":"1"}]}'
        def opened(request,**kwargs):captured.append(request);return Response()
        with patch.object(feed.opener,"open",side_effect=opened):
            feed.get("clock")
            with self.assertRaises(ArenaError):feed.get("/api/v5/trade/order")
        self.assertEqual(captured[0].method,"GET")
        self.assertEqual(set(dict(captured[0].header_items())),{"Accept","User-agent","Cache-control"})
        self.assertEqual(feed.calls,1)
        feed.calls=5000
        with self.assertRaises(ArenaError):feed.get("clock")


class EngineTests(unittest.TestCase):
    def enter(self,s):
        tick(s,market());self.assertEqual(s["bots"][0]["position_btc"],0)
        tick(s,market(1005));self.assertNotEqual(s["bots"][0]["position_btc"],0)

    def close_all(self,s):
        request_stop(s,1006);tick(s,market(1010));tick(s,market(1015))

    def test_known_long_accounting_costs_and_cash_baseline(self):
        s=state();self.enter(s);bot=s["bots"][0]
        self.assertAlmostEqual(bot["position_btc"],.19)
        self.assertAlmostEqual(bot["entry_price"],100.02)
        self.assertAlmostEqual(bot["pnl_usdt"],-.0266)
        self.close_all(s)
        self.assertEqual(s["status"],"DONE")
        self.assertAlmostEqual(bot["pnl_usdt"],-.0266)
        self.assertAlmostEqual(bot["fees_usdt"],.019)
        self.assertEqual(bot["unrealized_pnl_usdt"],0)
        self.assertEqual(s["bots"][-1]["equity_usdt"],100)

    def test_short_profit_direction_is_signed_correctly(self):
        s=state("short");self.enter(s)
        tick(s,market(1010,99))
        self.assertGreater(s["bots"][0]["pnl_usdt"],0)
        request_stop(s,1011);tick(s,market(1015,99));tick(s,market(1020,99))
        self.assertGreater(s["bots"][0]["pnl_usdt"],0)
        self.assertEqual(s["bots"][0]["position_btc"],0)

    def test_same_quote_and_latency_do_not_fill_signal_bar(self):
        s=state();tick(s,market())
        tick(s,market(1000.5,stamp=1000.5));self.assertEqual(s["bots"][0]["position_btc"],0)
        tick(s,market(1002,stamp=1000.5));self.assertNotEqual(s["bots"][0]["position_btc"],0)
        s=state();tick(s,market());tick(s,market(1005,stamp=1000))
        self.assertEqual(s["bots"][0]["position_btc"],0)

    def test_recheck_before_entry_can_veto_changed_signal(self):
        s=state();tick(s,market());snap=market(1005);snap["features"]["return_1m_bps"]=-1000
        tick(s,snap);self.assertEqual(s["bots"][0]["position_btc"],0)
        self.assertTrue(any(e["code"]=="entry_cancelled" for e in s["events"]))

    def test_stop_before_pending_entry_prevents_fill(self):
        s=state();tick(s,market());request_stop(s,1001);tick(s,market(1005))
        self.assertEqual(s["status"],"DONE");self.assertEqual(s["bots"][0]["fees_usdt"],0)

    def test_deadline_cancels_entry_and_never_extends_for_new_entry(self):
        s=state();s["_end"]=1003;tick(s,market());tick(s,market(1005))
        self.assertEqual(s["status"],"DONE");self.assertEqual(s["bots"][0]["position_btc"],0)

    def test_individual_stop_isolates_positions(self):
        first=recipe();second=recipe();second["id"]="other"
        s=new_season([first,second],100,300,1000,"hash");self.enter(s)
        request_stop(s,1006,first["id"]);tick(s,market(1010));tick(s,market(1015))
        self.assertEqual(s["bots"][0]["position_btc"],0)
        self.assertGreater(s["bots"][1]["position_btc"],0)
        self.assertEqual(s["status"],"RUNNING")

    def test_insufficient_liquidity_and_partial_close_preserve_residual(self):
        s=state();tick(s,market());snap=market(1005);snap["ask_btc"]=.01;tick(s,snap)
        self.assertEqual(s["bots"][0]["position_btc"],0)
        s=state();self.enter(s);request_stop(s,1006);tick(s,market(1010))
        snap=market(1015);snap["bid_btc"]=.5;tick(s,snap)
        self.assertAlmostEqual(s["bots"][0]["position_btc"],.14)
        self.assertEqual(s["status"],"CLOSING")
        tick(s,market(1020));self.assertEqual(s["status"],"DONE")

    def test_network_failures_never_invent_a_close_or_zero_position(self):
        s=state();self.enter(s)
        for t in (1010,1015,1020):failed_snapshot(s,t)
        self.assertEqual(s["status"],"RUNNING")
        self.assertEqual(s["admission_blocked"],"market_unavailable")
        self.assertGreater(s["bots"][0]["position_btc"],0)
        self.assertTrue(s["incomplete"])

    def test_pending_buy_cancelled_after_one_failed_snapshot(self):
        s=state();tick(s,market());failed_snapshot(s,1002);tick(s,market(1005))
        self.assertEqual(s["bots"][0]["position_btc"],0)

    def test_funding_is_signed_once_and_explicitly_modelled(self):
        s=state();self.enter(s);s["_last"]["funding_at"]=1007
        tick(s,market(1010));bot=s["bots"][0]
        self.assertAlmostEqual(bot["funding_usdt"],-.019)
        tick(s,market(1015));self.assertAlmostEqual(bot["funding_usdt"],-.019)
        events=[e for e in s["events"] if e["code"]=="model_funding"]
        self.assertEqual(len(events),1);self.assertTrue(events[0]["data"]["estimated"])

    def test_recipe_frozen_and_public_view_has_no_mutable_internal_intents(self):
        original=recipe();s=new_season([original],100,300,1000,"hash")
        original["rules"]["size_pct"]=30
        self.assertEqual(s["strategies"][0]["rules"]["size_pct"],20)
        tick(s,market());public=public_season(s)
        self.assertNotIn("_pending",json.dumps(public));self.assertEqual(public["mode"],"PAPER")
        public["bots"][0]["name"]="mutated"
        self.assertNotEqual(s["bots"][0]["name"],"mutated")

    def test_add_revalidated_and_not_allowed_after_price_reverses(self):
        r=recipe();r["rules"]["max_entries"]=3
        s=new_season([r],100,300,1000,"hash");self.enter(s)
        tick(s,market(1030,100.4))
        self.assertEqual(s["bots"][0]["_pending"]["kind"],"entry")
        tick(s,market(1035,99.99))
        self.assertEqual(s["bots"][0]["entries"],1)

    def test_successful_additions_keep_original_hold_deadline_and_own_capital(self):
        r=recipe();r["rules"].update(max_entries=3,add_after_bps=20,take_profit_bps=200)
        s=new_season([r],100,300,1000,"hash");self.enter(s)
        started=s["bots"][0]["_hold_start"]
        for t,p in ((1030,100.4),(1035,100.4),(1060,100.8),(1065,100.8),(1090,101.2),(1095,101.2)):
            tick(s,market(t,p))
        bot=s["bots"][0]
        self.assertEqual(bot["entries"],3)
        self.assertEqual(bot["_hold_start"],started)
        self.assertLessEqual(abs(bot["position_btc"])*101.2,100*.95)

    def test_exit_condition_or_unknown_exit_data_blocks_pending_add(self):
        for exit_value in (2, None):
            with self.subTest(exit_value=exit_value):
                r=recipe();r["rules"]["max_entries"]=3
                r["rules"]["exit"]={"mode":"any","conditions":[{"feature":"volume_ratio_5","op":"gt","value":1}]}
                s=new_season([r],100,300,1000,"hash");self.enter(s)
                snap=market(1030,100.4);snap["features"]["volume_ratio_5"]=0;tick(s,snap)
                self.assertEqual(s["bots"][0]["_pending"]["kind"],"entry")
                snap=market(1035,100.4);snap["features"]["volume_ratio_5"]=exit_value;tick(s,snap)
                bot=s["bots"][0];self.assertEqual(bot["entries"],1)
                if exit_value is not None:
                    self.assertEqual(bot["_pending"]["kind"],"close")
                    tick(s,market(1040,100.4));self.assertEqual(bot["position_btc"],0)
                else:
                    self.assertIsNone(bot["_pending"])
                    tick(s,market(1060,100.4));self.assertIsNone(bot["_pending"])
    def test_tiny_budget_under_minimum_lot_records_no_fill(self):
        s=state();s["budget_usdt"]=25
        tick(s,market(1000,84000));tick(s,market(1005,84000))
        self.assertEqual(s["bots"][0]["position_btc"],0)
        self.assertTrue(any(e["code"]=="entry_no_fill" for e in s["events"]))

    def test_large_data_gap_flags_result_and_keeps_authored_position(self):
        s=state();self.enter(s);tick(s,market(1110));tick(s,market(1115))
        self.assertEqual(s["status"],"RUNNING");self.assertTrue(s["incomplete"])
        self.assertGreater(s["bots"][0]["position_btc"],0)
        self.assertFalse(s["bots"][0]["stop_requested"])
        self.assertEqual(sum(e["code"]=="paper_entry_fill" for e in s["events"]),1)

    def test_recovery_timeout_preserves_unfilled_virtual_position(self):
        s=state();self.enter(s);request_stop(s,1006)
        for t in range(1010,1140,5):
            snap=market(t);snap["bid_btc"]=0;tick(s,snap)
            if s["status"]=="PAUSED":break
        self.assertEqual(s["status"],"PAUSED");self.assertGreater(s["bots"][0]["position_btc"],0)

    def test_total_entry_cap_blocks_entries_and_drawdown_stops_bot(self):
        s=state();self.enter(s);s["bots"][0]["_total_entries"]=20;tick(s,market(1010))
        self.assertFalse(s["bots"][0]["stop_requested"])
        self.assertIsNone(s["bots"][0]["_pending"])
        s=state();self.enter(s);tick(s,market(1010,40))
        self.assertGreater(s["bots"][0]["max_drawdown_pct"],10)
        self.assertTrue(s["bots"][0]["stop_requested"])


class ArenaServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.now=1000
        self.feed_calls=[]
        def forbidden_feed():self.feed_calls.append(True);raise AssertionError("No network in fixtures")
        self.arena=Arena(self.root,feed_factory=forbidden_feed,clock=lambda:self.now,background=False)

    def tearDown(self):
        self.arena.close();self.tmp.cleanup()

    def start(self):return self.arena.start(self.arena.data["generation"],["sample-trend"],100,300)

    def test_read_only_status_never_initializes_network_or_session(self):
        for _ in range(3):self.assertIsNone(self.arena.status()["active"])
        self.assertEqual(self.feed_calls,[])

    def test_duplicate_click_draft_and_overlap_are_blocked(self):
        with self.assertRaises(ArenaError):self.arena.start(0,["draft-yurii"],100,300)
        self.start()
        with self.assertRaises(ArenaError):self.arena.start(0,["sample-trend"],100,300)
        with self.assertRaises(ArenaError):self.arena.start(1,["sample-short"],100,300)
        self.assertEqual(len(self.arena.data["seasons"]),1)

    def test_another_manager_cannot_write_same_workspace_on_other_port(self):
        with self.assertRaises(ArenaError):Arena(self.root,background=False)

    def test_strategy_versions_never_modify_a_frozen_running_recipe(self):
        self.start();old=self.arena.session["strategies"][0]["recipe_sha256"]
        p=payload();p["rules"]["size_pct"]=30
        saved=self.arena.add_strategy(1,p)
        self.assertEqual(saved["version"],2)
        self.assertEqual(old,self.arena.session["strategies"][0]["recipe_sha256"])
        self.assertEqual(self.arena.session["strategies"][0]["rules"]["size_pct"],20)

    def test_restart_is_paused_and_resume_cannot_make_new_entries(self):
        self.start();identity=self.arena.session["id"]
        self.arena.close()
        self.arena=Arena(self.root,clock=lambda:self.now,background=False)
        self.assertEqual(self.arena.status()["active"]["status"],"PAUSED")
        self.assertEqual(self.feed_calls,[])
        self.arena.resume(1,identity)
        self.assertTrue(all(b["stop_requested"] for b in self.arena.session["bots"]))
        self.arena.advance(market(1005))
        self.assertEqual(self.arena.session["status"],"DONE")
        self.assertTrue(self.arena.session["incomplete"])
        self.assertTrue(all(b["fees_usdt"]==0 for b in self.arena.session["bots"]))

    def test_history_is_local_immutable_and_path_limited(self):
        self.start();identity=self.arena.session["id"]
        view=self.arena.history(identity);view["mode"]="LIVE"
        self.assertEqual(self.arena.history(identity)["mode"],"PAPER")
        for bad in ("../../.env.local","f"*32):
            with self.assertRaises(ArenaError):self.arena.history(bad)

    def test_http_guards_and_paper_start_never_invoke_live_launcher(self):
        live_calls=[]
        def forbidden_live(*args,**kwargs):live_calls.append(args);raise AssertionError("Live worker forbidden")
        panel=Panel(self.root,launcher=forbidden_live)
        with Server(panel,0,arena=self.arena) as server:
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def call(method,path,body=None,headers=None):
                conn=http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=3)
                conn.request(method,path,json.dumps(body) if body is not None else None,headers or {})
                res=conn.getresponse();data=json.loads(res.read());code=res.status;conn.close();return code,data
            valid={"Origin":server.origin,"X-Panel-Token":panel.token,"Content-Type":"application/json"}
            body={"generation":0,"strategy_ids":["sample-trend"],"budget_usdt":100,"duration_seconds":300}
            try:
                self.assertEqual(call("GET","/api/arena/state")[0],200)
                self.assertEqual(call("POST","/api/arena/start",body)[0],403)
                wrong={**valid,"Origin":"https://evil.invalid"}
                self.assertEqual(call("POST","/api/arena/start",body,wrong)[0],403)
                code,response=call("POST","/api/arena/start",body,valid)
                self.assertEqual(code,202);self.assertEqual(response["mode"],"PAPER")
                self.assertEqual(call("POST","/api/arena/start",body,valid)[0],409)
                self.assertEqual(call("GET","/api/arena/seasons/"+response["id"])[0],200)
                self.assertEqual(call("POST","/api/arena/strategies",{"generation":1,"strategy":[]},valid)[0],409)
                self.assertEqual(live_calls,[]);self.assertEqual(self.feed_calls,[])
            finally:server.shutdown();thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
