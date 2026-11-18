"""R2 (b) step в: post-only (D096) schema-4 recipes on ETH in the exact PAPER engine against tools/team_eval_v4.py
mode="official" (tools/v4_conformance.py), long and short, orders and trades; plus the tape model's instrument and
exact path price, and the engine's adverse selection in tools/team_eval.trades_from_events. Synthetic ETH-like candles in
temp folders only; no network, key, Jev or order."""
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
for _path in (str(ROOT), str(ROOT / "tools")):
    if _path not in sys.path:
        sys.path.insert(0, _path)
import okx_tape_model
import team_data
import team_eval
import team_eval_v4
import v4_conformance

T0 = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
ETH = "ETH-USDT-SWAP"
ETH_JSON = ROOT / "tools" / "okx_instrument_eth_usdt_swap.json"
SIGNAL = {"feature": {"name": "return_bps", "tf": "1m", "n": 5}}


def payload(**rules):
    base = {"side": "both", "entry": {"long": {"when": {**SIGNAL, "op": "lte", "value": -12}},
                                      "short": {"when": {**SIGNAL, "op": "gte", "value": 12}}},
            "order": {"type": "post_only", "offset": {"bps": 3}, "timeout_seconds": 300}, "stop_loss_bps": 25,
            "take_profit_bps": 40, "max_hold_seconds": 900, "cooldown_seconds": 60, "size_pct": 11}
    base.update(rules)
    return {"strategy_schema_version": 4, "name": "v4 post-only conformance test", "author": {"name": "platform", "kind": "model"},
            "description": "test", "original_text": "test", "confirmed": True, "instrument": ETH, "rules": base}


def write_eth_store(folder, minutes):
    """ETH-like swings of about +-70 bps over ~40 minutes with wicks of a few bps, on the 0.01 tick of ETH-USDT-SWAP, so
    post-only limits fill, expire and are refused as crossing, and stops, targets and holds all happen."""
    folder.mkdir(parents=True, exist_ok=True)
    stamp = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    price = lambda i: 3000 * (1 + .006 * math.sin(i / 6.5) + .002 * math.sin(i / 2.1) + .0009 * math.sin(i * 1.7))
    candles = folder / f"okx_{ETH}_1m_2026-01-01_2026-01-02.csv"
    with candles.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(okx_tape_model.CANDLE_HEADER)
        for i in range(minutes):
            ts, o, c = T0 + 60 * i, price(i), price(i + 1)
            wick = .4 + .9 * abs(math.sin(i * 0.37))
            writer.writerow([ts * 1000, stamp(ts), f"{o:.2f}", f"{max(o, c) + wick:.2f}", f"{min(o, c) - wick:.2f}", f"{c:.2f}",
                             "8100", "810", "2430000", "1"])
    funding = folder / f"okx_{ETH}_funding_2026-01-01_2026-01-02.csv"
    with funding.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["funding_time_ms", "utc", "funding_rate", "realized_rate", "method", "formula_type"])
        for hours in (2, 4):  # inside the seasons, so open positions settle funding on both sides
            writer.writerow([(T0 + hours * 3600) * 1000, stamp(T0 + hours * 3600), "0.0001", "0.0001", "current_period", "withRate"])
    scan = team_data.scan_candles(candles)
    manifest = {"schema": "public-history/v1",
                "candles": {"file": candles.name, "sha256": team_data.sha256_file(candles), "rows": scan["rows"],
                            "first_open_utc": scan["first_open_utc"], "last_open_utc": scan["last_open_utc"], "gap_count": 0},
                "funding": {"file": funding.name, "sha256": team_data.sha256_file(funding)}}
    (folder / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert team_data.catalog(folder, ETH)["problems"] == []
    return folder


class PostOnlyConformanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = write_eth_store(Path(self.tmp.name) / ETH, 8 * 60)

    def recipe(self, name, **rules):
        path = Path(self.tmp.name) / f"{name}.json"
        path.write_text(json.dumps(payload(**rules)), encoding="utf-8")
        return path

    def conformance(self, path, features):
        return v4_conformance.run(path, "2026-01-01T01:00:00Z", 2, self.store, None, admission=7200, features=features)

    def assert_conforms(self, report, label):
        self.assertEqual(report["mismatches"], [], (label, report["mismatches"][:3]))
        self.assertEqual(report["order_mismatches"], [], (label, report["order_mismatches"][:3]))
        self.assertEqual(report["orders"]["engine"], report["orders"]["evaluator"], label)
        self.assertEqual(report["orders"]["matched"], report["orders"]["evaluator"], label)
        self.assertEqual(report["worst_matched_limit_bps"], 0.0, label)  # the same limit on both sides, not merely close
        self.assertLessEqual(report["worst_matched_price_bps"], v4_conformance.PRICE_BPS, label)
        engine, evaluator = report["maker"]["engine"], report["maker"]["evaluator"]
        for key in ("orders", "rejected_crossing", "unfilled", "filled", "too_small"):
            self.assertEqual(engine[key], evaluator[key], (label, key, engine, evaluator))
        self.assertEqual((engine["cancelled"], engine["resting_at_end"], engine["other_rejections"]), (0, 0, 0), label)
        self.assertEqual(report["trades"]["engine"], report["trades"]["matched"] + report["trades"]["ambiguous_minute"], label)
        self.assertEqual(engine["filled"], report["trades"]["engine"], label)  # every fill is a closed trade within the tail
        self.assertTrue(all(not w["errors"] and w["status"] == "DONE" for w in report["windows"]), (label, report["windows"]))

    def test_post_only_long_and_short_match_the_official_evaluator(self):
        path = self.recipe("both")
        for features in ("arrays", "book"):  # F1 alone, then F1 with the F2 feature book
            report = self.conformance(path, features)
            self.assert_conforms(report, features)
            maker = report["maker"]["engine"]
            self.assertGreaterEqual(maker["orders"], 20, maker)
            self.assertTrue(maker["filled"] >= 8 and maker["unfilled"] >= 3 and maker["rejected_crossing"] >= 1, maker)
            sides = {row["side"] for row in report["matched"]}
            reasons = {row["engine"][1] for row in report["matched"]} | {row["engine"][1] for row in report["ambiguous"]}
            self.assertEqual(sides, {"long", "short"}, features)
            self.assertTrue({"stop_loss", "take_profit"} <= reasons, reasons)
            self.assertEqual((report["instrument"], report["order_type"]), (ETH, "post_only"))
            self.assertEqual(report["tail_seconds"], 900 + 300 + v4_conformance.TAIL_EXTRA)  # the timeout extends the tail

    def test_a_feature_offset_and_a_short_timeout_match_as_well(self):
        offset = {"mult": .5, "of": {"name": "rms_bar_bps", "tf": "1m", "n": 10}}
        path = self.recipe("feature_offset", side="long", entry={"when": {**SIGNAL, "op": "lte", "value": -12}},
                           order={"type": "post_only", "offset": offset, "timeout_seconds": 120})
        report = self.conformance(path, "book")
        self.assert_conforms(report, "feature offset")
        self.assertTrue(report["maker"]["engine"]["filled"] and report["maker"]["engine"]["unfilled"], report["maker"])
        self.assertEqual({row["side"] for row in report["matched"]}, {"long"})

    def test_engine_net_includes_adverse_selection_like_the_evaluator(self):
        report = self.conformance(self.recipe("both"), "book")
        pairs = report["matched"]
        self.assertTrue(pairs)
        for row in pairs:  # net = gross - fees - adverse selection + funding on both sides; float noise only
            self.assertAlmostEqual(row["engine"][2], row["evaluator"][2], places=6, msg=row)
        self.assertAlmostEqual(report["net_usdt"]["engine"], report["net_usdt"]["evaluator"], places=5)

    def test_the_tape_carries_the_instrument_and_the_exact_path_price(self):
        rows = team_eval.RowFeed(self.store)
        try:
            store = team_eval.row_store(rows.slice(T0 * 1000, (T0 + 3 * 3600) * 1000), team_data.load_funding(self.store), ETH_JSON)
        finally:
            rows.close()
        start = T0 + 3600
        plain = store.tape(start, start + 600)
        self.assertTrue(all(len(s["raw"]["ticker"][0]["last"].split(".")[1]) == 2 for s in plain))  # default tapes: as before
        store.precise_last = True
        tape = store.tape(start, start + 600)
        series = team_eval_v4.load_minutes(self.store, T0 * 1000, (T0 + 3 * 3600) * 1000)
        quotes = team_eval_v4.Quotes(series, .01)
        times = [s["at"] for s in tape]
        expected, bids, asks = quotes.book(times)
        for snapshot, p, bid, ask in zip(tape, expected, bids, asks):
            ticker = snapshot["raw"]["ticker"][0]
            self.assertEqual((ticker["instId"], snapshot["raw"]["funding"][0]["instId"]), (ETH, ETH))
            self.assertEqual(float(ticker["last"]), float(p))  # bit for bit the evaluator's path price
            self.assertEqual((snapshot["bid"], snapshot["ask"]), (float(bid), float(ask)))
            self.assertEqual(len(ticker["bidPx"].split(".")[1]), 2)  # bid and ask stay on the tick
        self.assertTrue(any(len(s["raw"]["ticker"][0]["last"].split(".")[1]) > 2 for s in tape))  # last is not rounded

    def test_the_conformance_refuses_a_wrong_instrument_or_store(self):
        path = self.recipe("both")
        with self.assertRaises(SystemExit):
            v4_conformance.run(path, "2026-01-01T01:00:00Z", 1, self.store, team_eval.DEFAULT_INSTRUMENT, admission=7200)
        btc = json.loads(path.read_text(encoding="utf-8"))
        btc["instrument"] = "BTC-USDT-SWAP"
        other = Path(self.tmp.name) / "btc.json"
        other.write_text(json.dumps(btc), encoding="utf-8")
        with self.assertRaises(SystemExit):  # an ETH store under a BTC recipe
            v4_conformance.run(other, "2026-01-01T01:00:00Z", 1, self.store, None, admission=7200)

    def test_taker_trades_keep_their_shape_and_post_only_trades_book_adverse_selection(self):
        def event(code, at, **data):
            return {"code": code, "at": datetime.fromtimestamp(at, timezone.utc).isoformat(timespec="milliseconds"), "data": data}
        close = {"quantity_btc": .003, "price_usdt": 3010.0, "fee_usdt": .0045, "gross_pnl_usdt": .03, "remaining_btc": 0, "reason": "take_profit"}
        taker = [event("paper_entry_fill", T0, side="long", quantity_btc=.003, price_usdt=3000.0, fee_usdt=.0045),
                 event("paper_close_fill", T0 + 60, **close)]
        trades, _ = team_eval.trades_from_events(taker, "w")
        self.assertNotIn("adverse_selection_usdt", trades[0])  # V1, V2 and taker ledgers are byte for byte as before
        self.assertEqual(trades[0]["net_usdt"], round(.03 - .009, 8))
        maker = [event("paper_entry_fill", T0, side="long", quantity_btc=.003, price_usdt=3000.0, fee_usdt=.0018,
                       adverse_selection_usdt=.0018, order_type="post_only"), event("paper_close_fill", T0 + 60, **close)]
        trades, _ = team_eval.trades_from_events(maker, "w")
        self.assertEqual(trades[0]["adverse_selection_usdt"], .0018)
        self.assertEqual(trades[0]["net_usdt"], round(.03 - .0063 - .0018, 8))


if __name__ == "__main__":
    unittest.main()
