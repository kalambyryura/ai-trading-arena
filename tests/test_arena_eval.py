"""B-15 author stand: exact recipe through the production engine on model tapes, and the field calculator.

Synthetic candles and funding in a temp folder only; no network, keys, Jev or exchange code.
"""
import csv
from datetime import datetime, timezone
import io
import json
import math
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import arena_eval
import field_ff
from trading_lab import strategy_v2

T0 = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_data(folder, minutes=180):
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "okx_BTC-USDT-SWAP_1m_2026-01-01_2026-01-01.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(arena_eval_tape_header())
        for i in range(minutes):
            ts = T0 + 60 * i
            o, c = 80000 + 40 * math.sin(i / 7), 80000 + 40 * math.sin((i + 1) / 7)
            writer.writerow([ts * 1000, iso(ts), f"{o:.1f}", f"{max(o, c) + 3:.1f}", f"{min(o, c) - 3:.1f}", f"{c:.1f}",
                             "120.5", "1.205", "96400.0", "1"])
    with (folder / "okx_BTC-USDT-SWAP_funding_2026-01-01_2026-01-01.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["funding_time_ms", "utc", "funding_rate", "realized_rate", "method", "formula_type"])
        for hours in (8, 16):
            ts = T0 + hours * 3600
            writer.writerow([ts * 1000, iso(ts), "0.0001", "0.0001", "current_period", "withRate"])
    return folder


def arena_eval_tape_header():
    import okx_tape_model
    return okx_tape_model.CANDLE_HEADER


def write_recipe(path):
    item = json.loads((ROOT / "tests" / "fixtures" / "strategy_v2_temporal.json").read_text(encoding="utf-8"))
    rules = item["rules"]
    rules["entry"] = {"long": {"setup": None, "invalidate": None, "expiry_bars": None,
                               "confirm": {"feature": "return_1m_bps", "op": "gte", "value": -1000}}}
    rules.update(max_hold_seconds=300, cost_checkpoint=None)
    item["rules"] = strategy_v2.validate_rules(rules)
    path.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
    return path


def run(argv):
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = arena_eval.main(argv)
    return code, json.loads(buffer.getvalue())


class ArenaEvalTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.data = write_data(self.root / "data")
        self.recipe = write_recipe(self.root / "recipe.json")

    def tearDown(self):
        self.folder.cleanup()

    def common(self, *extra):
        return ["--recipe", str(self.recipe), "--author", "test", "--data", str(self.data), "--jobs", "1",
                "--registry", str(self.root / "registry.jsonl"), *extra]

    def test_generated_windows_run_the_production_engine_and_register_every_attempt(self):
        code, short = run(self.common("--start", iso(T0 + 2400), "--admission-hours", "0.5", "--count", "2", "--label", "first",
                                      "--trades", str(self.root / "trades.csv"), "--out", str(self.root / "report.json")))
        self.assertEqual(code, 0)
        report = json.loads((self.root / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["schema"], "arena-eval/v1")
        self.assertEqual([w["id"] for w in report["windows"]], ["w0000", "w0001"])
        self.assertEqual([w["start_utc"] for w in report["windows"]], [iso(T0 + 2400), iso(T0 + 4200)])
        self.assertEqual(report["tail_seconds"], 720)
        self.assertEqual(report["runtime"], "RULES_ONLY (no Jev, no exchange)")
        self.assertGreater(report["summary"]["trades"], 0)
        self.assertEqual(report["summary"]["complete"], 2)
        self.assertEqual(short["trades"], report["summary"]["trades"])
        with (self.root / "trades.csv").open(newline="", encoding="utf-8") as handle:
            trades = list(csv.reader(handle))
        self.assertEqual(trades[0], arena_eval.TRADE_COLUMNS)
        self.assertEqual(len(trades) - 1, report["summary"]["trades"])
        self.assertEqual(report["trades_file"]["sha256"], arena_eval.sha256_file(self.root / "trades.csv"))
        self.assertAlmostEqual(sum(float(t[10]) for t in trades[1:]), sum(c["net_usdt"] for w in report["windows"] for c in w["cycles"]), places=6)
        run(self.common("--start", iso(T0 + 2400), "--admission-hours", "0.5", "--count", "2", "--only", "w0001", "--label", "second",
                        "--out", str(self.root / "report2.json")))
        registry = [json.loads(line) for line in (self.root / "registry.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["label"] for r in registry], ["first", "second"])
        self.assertEqual(registry[1]["matrix_windows"], 1)
        self.assertEqual(registry[0]["report_sha256"], arena_eval.hashlib.sha256((self.root / "report.json").read_bytes()).hexdigest())

    def test_matrix_files_are_pinned_to_their_candle_file(self):
        windows = {"source_csv_sha256": "0" * 64, "matrices": {"tiny": {"windows": [
            {"id": "a", "day": iso(T0)[:10], "start_utc": iso(T0 + 2400), "admission_seconds": 600}]}}}
        path = self.root / "windows.json"
        path.write_text(json.dumps(windows), encoding="utf-8")
        with self.assertRaises(SystemExit):
            run(self.common("--windows", str(path), "--matrix", "tiny"))
        windows["source_csv_sha256"] = arena_eval.sha256_file(next(self.data.glob("okx_*_1m_*.csv")))
        path.write_text(json.dumps(windows), encoding="utf-8")
        code, short = run(self.common("--windows", str(path), "--matrix", "tiny", "--out", str(self.root / "r.json")))
        self.assertEqual((code, short["windows"], short["matrix"]), (0, 1, "tiny"))
        with self.assertRaises(SystemExit):
            run(self.common("--windows", str(path), "--matrix", "missing"))

    def test_window_generator_validates_its_inputs(self):
        rows = arena_eval.generated_windows(iso(T0), 24, 3, None)
        self.assertEqual([r["start_utc"] for r in rows], [iso(T0), iso(T0 + 86400), iso(T0 + 2 * 86400)])
        self.assertEqual(rows[1]["day"], iso(T0 + 86400)[:10])
        for bad in ((iso(T0 + 30), 24, 1, None), (iso(T0), 0.01, 1, None), (iso(T0), 24, 0, None)):
            with self.assertRaises(SystemExit):
                arena_eval.generated_windows(*bad)


def report(nets, recipe="r", complete=None, matrix="m"):
    days = ["2026-01-01", "2026-01-01", "2026-01-02", "2026-01-02"]
    return {"schema": "arena-eval/v1", "matrix": matrix, "recipe_sha256": recipe,
            "windows": [{"id": f"w{i}", "pnl_usdt": net, "day": days[i], "status": "COMPLETE" if (complete or [True] * 4)[i] else "INCOMPLETE"}
                        for i, net in enumerate(nets)]}


class FieldTests(unittest.TestCase):
    def test_first_place_share_counts_cash_and_splits_ties(self):
        field = {"a": report([1, -1, 0, 2]), "b": report([1, -2, 0, -1])}
        result = field_ff.compare(report([0.5, -0.5, 0, 3], "c"), field)
        stats = result["field_with_candidate"]
        # w0: a/b tie at 1 -> 1/2 each; w1: cash alone; w2: candidate, a, b and cash all at 0 -> 1/4 each; w3: candidate alone.
        self.assertEqual(stats["a"]["ff_pct"], round(100 * (0.5 + 0.25) / 4, 3))
        self.assertEqual(stats["cash"]["ff_pct"], round(100 * (1 + 0.25) / 4, 3))
        self.assertEqual(stats["candidate"]["ff_pct"], round(100 * (0.25 + 1) / 4, 3))
        self.assertEqual(stats["candidate"]["sole_first_pct"], 25.0)
        self.assertEqual(stats["b"]["below_cash_pct"], 50.0)
        self.assertEqual(result["windows_ranked"], 4)

    def test_paired_difference_has_a_deterministic_day_bootstrap_and_drops_incomplete_windows(self):
        field = {"a": report([0, 0, 0, 0])}
        candidate, baseline = report([1, 1, 1, -1], "c"), report([0, 0, 0, 0], "v1", complete=[True, True, True, False])
        first = field_ff.compare(candidate, field, baseline)
        self.assertEqual(first, field_ff.compare(candidate, field, baseline))
        self.assertEqual(first["windows_excluded_incomplete"], ["w3"])
        paired = first["paired_vs_baseline"]
        self.assertEqual((paired["better"], paired["worse"], paired["equal"], paired["net_diff_total_usdt"]), (3, 0, 0, 3.0))
        self.assertEqual(paired["bootstrap"]["days"], 2)
        low, high = paired["bootstrap"]["net_diff_total_usdt_90ci"]
        self.assertLessEqual(low, high)

    def test_mismatched_matrices_or_window_ids_are_refused(self):
        with self.assertRaises(SystemExit):
            field_ff.compare(report([1, 1, 1, 1]), {"a": report([0, 0, 0, 0], matrix="other")})
        broken = report([0, 0, 0, 0])
        broken["windows"] = broken["windows"][:3]
        with self.assertRaises(SystemExit):
            field_ff.compare(report([1, 1, 1, 1]), {"a": broken})
        with self.assertRaises(SystemExit):
            field_ff.compare(report([1, 1, 1, 1]), {"cash": report([0, 0, 0, 0])}, name="cash")


if __name__ == "__main__":
    unittest.main()
