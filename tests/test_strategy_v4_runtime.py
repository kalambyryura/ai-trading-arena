"""Schema 4 in the exact PAPER engine (R1 full F1): recipe normalisation, D106 decision timing, UNKNOWN, target timing
and conformance with tools/team_eval_v4.py mode="official". Synthetic candles in temp folders only; no network."""
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
import unittest

ROOT = Path(__file__).resolve().parents[1]
for _path in (str(ROOT), str(ROOT / "tools")):
    if _path not in sys.path:
        sys.path.insert(0, _path)
import okx_tape_model
import team_data
import team_eval
import v4_conformance
from trading_lab import arena_engine, exchange_engine as exchange, strategy_runtime, strategy_v4, strategy_v4_runtime as v4rt

T0 = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())


def payload(**rules):
    base = {"side": "long", "entry": {"when": {"feature": {"name": "return_bps", "tf": "1m", "n": 5}, "op": "lte", "value": -12}},
            "order": {"type": "taker"}, "stop_loss_bps": 20, "take_profit_bps": 40, "max_hold_seconds": 900,
            "cooldown_seconds": 60, "size_pct": 11}
    base.update(rules)
    return {"strategy_schema_version": 4, "name": "v4 runtime test", "author": {"name": "platform", "kind": "model"},
            "description": "test", "original_text": "test", "confirmed": True, "instrument": "BTC-USDT-SWAP", "rules": base}


def write_store(folder, minutes):
    """Swings of about +-60 bps over ~40 minutes, so stops, targets and holds all happen; two funding settlements."""
    folder.mkdir(parents=True, exist_ok=True)
    stamp = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    price = lambda i: 80000 * (1 + .006 * math.sin(i / 6.5) + .002 * math.sin(i / 2.1))
    with (folder / "okx_BTC-USDT-SWAP_1m_2026-01-01_2026-01-02.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(okx_tape_model.CANDLE_HEADER)
        for i in range(minutes):
            ts, o, c = T0 + 60 * i, price(i), price(i + 1)
            writer.writerow([ts * 1000, stamp(ts), f"{o:.1f}", f"{max(o, c) + 4:.1f}", f"{min(o, c) - 4:.1f}", f"{c:.1f}",
                             "120.5", "1.205", "96400.0", "1"])
    with (folder / "okx_BTC-USDT-SWAP_funding_2026-01-01_2026-01-02.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["funding_time_ms", "utc", "funding_rate", "realized_rate", "method", "formula_type"])
        for hours in (8, 16):
            writer.writerow([(T0 + hours * 3600) * 1000, stamp(T0 + hours * 3600), "0.0001", "0.0001", "current_period", "withRate"])
    candles = next(folder.glob("okx_*_1m_*.csv"))
    scan = team_data.scan_candles(candles)
    manifest = {"schema": "public-history/v1",
                "candles": {"file": candles.name, "sha256": team_data.sha256_file(candles), "rows": scan["rows"],
                            "first_open_utc": scan["first_open_utc"], "last_open_utc": scan["last_open_utc"], "gap_count": 0},
                "funding": {"file": "okx_BTC-USDT-SWAP_funding_2026-01-01_2026-01-02.csv",
                            "sha256": team_data.sha256_file(folder / "okx_BTC-USDT-SWAP_funding_2026-01-01_2026-01-02.csv")}}
    (folder / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert team_data.catalog(folder, "BTC-USDT-SWAP")["problems"] == []
    return folder


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = write_store(Path(self.tmp.name) / "BTC-USDT-SWAP", 6 * 60)
        self.recipe = v4rt.engine_recipe(strategy_v4.validate_payload(payload()))

    def tearDown(self):
        self.tmp.cleanup()

    def season(self, values, start=T0 + 3600, seconds=1800, recipe=None):
        """One PAPER season over a model tape whose every snapshot carries values(bar_close_s) as its feature book."""
        rows = team_eval.RowFeed(self.store)
        try:
            store = team_eval.row_store(rows.slice(T0 * 1000, (start + seconds + 120) * 1000), team_data.load_funding(self.store),
                                        team_eval.DEFAULT_INSTRUMENT)
        finally:
            rows.close()
        tape = store.tape(start, start + seconds)
        for snapshot in tape:
            close = (snapshot["at"] - v4rt.VISIBILITY_SECONDS) // 60 * 60
            snapshot["v4_features"] = {"bar_close_ms": int(close) * 1000, "values": {"return_bps@1m:5": values(close)}}
        state = arena_engine.new_season([recipe or self.recipe], 100, seconds - 600, tape[0]["at"], "test")
        for snapshot in tape:
            if state["status"] not in {"RUNNING", "DRAINING", "CLOSING"}:
                break
            arena_engine.tick(state, snapshot)
        return state

    def test_engine_recipe_is_btc_or_eth_with_one_entry_and_no_formal_exit(self):
        rules = self.recipe["rules"]
        self.assertEqual((rules["max_entries"], rules["max_spread_bps"], rules["exit"]), (1, 2, {"mode": "all", "conditions": []}))
        self.assertTrue(strategy_runtime.declarative(self.recipe) and not strategy_runtime.enabled(self.recipe))
        maker = {"type": "post_only", "offset": {"bps": 5}, "timeout_seconds": 600}
        self.assertEqual(v4rt.engine_recipe(strategy_v4.validate_payload(payload(order=maker)))["rules"]["order"], maker)
        sol = strategy_v4.validate_payload({**payload(), "instrument": "SOL-USDT-SWAP"})
        self.assertEqual(v4rt.engine_recipe(sol)["instrument"], "SOL-USDT-SWAP")  # R2.1.13 (D152)
        with patch.object(v4rt, "EXECUTED_INSTRUMENTS", ("BTC-USDT-SWAP", "ETH-USDT-SWAP")), self.assertRaises(strategy_v4.ContractError):
            v4rt.engine_recipe(sol)  # the guard stays for what the executor does not trade
        eth = v4rt.engine_recipe(strategy_v4.validate_payload({**payload(), "instrument": "ETH-USDT-SWAP"}))
        self.assertEqual(eth["instrument"], "ETH-USDT-SWAP")  # R2 (a): the executor trades ETH as well

    def test_one_decision_per_newly_visible_bar_at_close_plus_five_seconds(self):
        state = self.season(lambda close: 0.0)  # known values, the rule never passes: a decision on every bar
        checks = [e for e in state["events"] if e["code"] == "entry_checks"]
        stamps = [datetime.fromisoformat(e["at"]).timestamp() for e in checks]
        self.assertTrue(stamps and all(int(t) % 60 == v4rt.VISIBILITY_SECONDS for t in stamps), stamps[:5])
        self.assertEqual(len(stamps), len(set(int(t) // 60 for t in stamps)))  # never twice for one bar
        self.assertEqual(len(stamps), (1800 - 600) // 60)  # every bar that became visible during the admission

    def test_an_unknown_book_never_enters_and_a_passing_rule_enters_at_the_next_quote(self):
        unknown = self.season(lambda close: None)
        self.assertFalse([e for e in unknown["events"] if e["code"] in ("entry_intent", "paper_entry_fill")])
        state = self.season(lambda close: -20.0)
        intent = next(e for e in state["events"] if e["code"] == "entry_intent")
        fill = next(e for e in state["events"] if e["code"] == "paper_entry_fill")
        gap = datetime.fromisoformat(fill["at"]).timestamp() - datetime.fromisoformat(intent["at"]).timestamp()
        self.assertEqual((int(datetime.fromisoformat(intent["at"]).timestamp()) % 60, gap), (v4rt.VISIBILITY_SECONDS, 5.0))

    def test_a_book_behind_the_clock_or_not_yet_visible_is_unknown(self):
        snapshot = {"at": T0 + 125, "v4_features": {"bar_close_ms": (T0 + 120) * 1000, "values": {}}}
        self.assertIsNotNone(v4rt.book(snapshot))
        self.assertIsNone(v4rt.book({**snapshot, "at": T0 + 124}))  # visible only from close + 5 s
        self.assertIsNone(v4rt.book({**snapshot, "at": T0 + 185}))  # the next bar should be visible by now
        self.assertIsNone(v4rt.book({**snapshot, "v4_features": {"bar_close_ms": (T0 + 121) * 1000, "values": {}}}))

    def test_the_target_counts_from_the_minute_after_the_fill(self):
        bot = {"_hold_start": T0 + 125}
        self.assertFalse(v4rt.target_live(bot, T0 + 175))
        self.assertTrue(v4rt.target_live(bot, T0 + 180))
        self.assertFalse(v4rt.target_live({"_hold_start": None}, T0 + 900))

    def test_engine_trades_match_the_official_evaluator(self):
        recipe = Path(self.tmp.name) / "recipe.json"
        recipe.write_text(json.dumps(payload()), encoding="utf-8")
        for features in ("arrays", "book"):  # F1 alone, then F1 with the F2 feature book
            report = v4_conformance.run(recipe, "2026-01-01T01:00:00Z", 2, self.store, team_eval.DEFAULT_INSTRUMENT,
                                        admission=7200, features=features)
            reasons = {row["engine"][1] for row in report["matched"]}
            self.assertGreaterEqual(report["trades"]["engine"], 4, features)
            self.assertEqual(report["mismatches"], [], (features, report["mismatches"][:3]))
            self.assertLessEqual(report["worst_matched_price_bps"], v4_conformance.PRICE_BPS)
            self.assertTrue({"stop_loss", "take_profit"} <= reasons | {row["engine"][1] for row in report["ambiguous"]}, reasons)


class LiveEngineTests(unittest.TestCase):
    """The Live engine on the fictional exchange of the management tests: no profile, key, request or paid gate."""

    def run_with(self, contracts):
        sys.path.insert(0, str(ROOT / "tests"))
        from test_management_integration import FictionalExchangeRun, market
        recipe = v4rt.engine_recipe(strategy_v4.validate_payload(payload()))
        run = FictionalExchangeRun(recipe, contracts=contracts)
        self.addCleanup(run.close)
        run.bot["_next_decision"] = run.now
        run.book = lambda close: 0.0  # known values; the rule never passes, so no order and no gate is reached

        def snapshot():
            item = market(run.now, bid=float(run.fake.bid), ask=float(run.fake.ask))
            close = (run.now - v4rt.VISIBILITY_SECONDS) // 60 * 60
            if run.book(close) is not False:
                item["v4_features"] = {"bar_close_ms": int(close) * 1000, "values": {"return_bps@1m:5": run.book(close)}}
            return item
        run.snapshot = snapshot
        return run, recipe

    def test_live_decides_once_per_visible_bar_and_not_without_a_book(self):
        run, _ = self.run_with("0")
        checks = lambda: [e for e in run.state["events"] if e["code"] == "entry_checks"]
        run.book = lambda close: False  # no feature book in the snapshot
        run.step(35)  # :05 of the next minute
        self.assertEqual(checks(), [])
        run.book = lambda close: 0.0
        run.step(5)
        run.step(5)
        run.step(50)  # :05 of the following minute: the next bar
        stamps = [e["at"] for e in checks()]
        self.assertEqual(len(stamps), 2, stamps)

    def test_live_target_waits_for_the_next_minute_and_the_stop_does_not(self):
        run, recipe = self.run_with(".04")
        bot, state = run.bot, run.state
        bot["_hold_start"] = run.now  # second :30 of the fill minute
        rich = {**run.snapshot(), "bid": bot["entry_price"] * 1.01, "ask": bot["entry_price"] * 1.0101}
        self.assertIsNone(exchange.exit_reason(state, bot, recipe, rich, run.now + 25))
        self.assertEqual(exchange.exit_reason(state, bot, recipe, rich, run.now + 30), "Ціль стратегії.")
        poor = {**rich, "bid": bot["entry_price"] * .99, "ask": bot["entry_price"] * .9901}
        self.assertEqual(exchange.exit_reason(state, bot, recipe, poor, run.now + 1), "Локальний stop-loss стратегії.")


if __name__ == "__main__":
    unittest.main()
