"""Team evaluator: exact-engine conformance with arena_eval, determinism, gate arithmetic, clone check, data guards.

Synthetic candles in temp folders only; no network, keys, Jev or exchange code. The real research lease is
patched out where a whole run is exercised, so the test never blocks a running author job.
"""
from collections import Counter
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
import arena_eval
import research_limits
import team_data
import team_eval
from test_arena_eval import T0, write_data, write_recipe
sys.path.insert(0, str(ROOT / "team" / "judge" / "tools"))
import gate_d_first_n  # the Judge's manual first-N recipe (gate D procedure v1.1 section 4)

DAY = 86400


def make_store(folder, minutes=720):
    data = write_data(folder, minutes)
    candles = next(data.glob("okx_*_1m_*.csv"))
    funding = next(data.glob("okx_*_funding_*.csv"))
    scan = team_data.scan_candles(candles)
    manifest = {"schema": "public-history/v1",
                "candles": {"file": candles.name, "sha256": team_data.sha256_file(candles), "rows": scan["rows"],
                            "first_open_utc": scan["first_open_utc"], "last_open_utc": scan["last_open_utc"], "gap_count": 0},
                "funding": {"file": funding.name, "sha256": team_data.sha256_file(funding)}}
    (data / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert team_data.catalog(data, "BTC-USDT-SWAP")["problems"] == []
    return data, candles, funding


def trade(window, entry, exit_, net, notional=20.0, side="long"):
    return {"window_id": window, "entry_at": entry, "exit_at": exit_, "seconds": 60.0, "side": side, "adds": 0,
            "partial_reduces": 0, "first_notional_usdt": notional / 2, "executed_notional_usdt": notional,
            "gross_usdt": net, "fees_usdt": 0.0, "funding_usdt": 0.0, "net_usdt": net, "exit_reason": "take_profit",
            "executions": []}


def daily_windows(first, last):
    out, day = [], first
    while day <= last:
        out.append({"id": f"w{len(out):05d}", "start_utc": day.strftime("%Y-%m-%dT%H:%M:%SZ"), "admission_seconds": DAY,
                    "status": "COMPLETE", "pnl_usdt": 0.0, "trades": 0, "max_drawdown_pct": 0.0, "event_counts": {}})
        day += timedelta(days=1)
    return out


def ledger(trades, windows, data_set="DEV"):
    return {"data_set": data_set, "recipe_name": "fixture", "recipe_sha256": "r" * 64, "code_sha256": "c" * 64,
            "evaluator_sha256": "e" * 64, "windows": windows, "trades": trades}


def setUpModule():
    """Free RAM is the machine's, not the test's: every RAM floor reached here sees a fake 64 GB, so the suite passes
    beside other jobs; the tests of a floor set their own value on top (2 GB, 1 GB). Real runs keep their floors."""
    for target in (team_eval, research_limits):
        patcher = mock.patch.object(target, "free_ram_gb", return_value=64.0)
        patcher.start()
        unittest.addModuleCleanup(patcher.stop)


@contextmanager
def no_lease(*_args, **_kwargs):
    yield {"slots": 1, "capacity": 1}


class ConformanceTests(unittest.TestCase):
    """The parallel team evaluator must reproduce arena_eval window by window on the same data."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store, self.candles, self.funding = make_store(self.root / "store")
        self.recipe = write_recipe(self.root / "recipe.json")
        self.raw = self.recipe.read_bytes()

    def tearDown(self):
        self.tmp.cleanup()

    def test_windows_equal_arena_eval_exactly(self):
        _, windows, _ = team_eval.plan_windows(self.store, "DEV", "2026-01-01T01:00:00Z", None, 3600, 3600, 720)
        self.assertGreaterEqual(len(windows), 8)
        rows = team_eval.run_windows(self.raw, windows, 720, self.store, team_eval.DEFAULT_INSTRUMENT, 1)
        funded = 0
        for item, row in zip(windows, rows):
            reference = arena_eval.one_window((self.raw, {"id": item["id"], "start_utc": team_eval.utc(item["start_s"]),
                                                          "admission_seconds": item["admission_seconds"]},
                                               720, str(self.candles), str(self.funding), str(team_eval.DEFAULT_INSTRUMENT)))
            for key in ("status", "pnl_usdt", "trades", "fees_usdt", "funding_usdt", "max_drawdown_pct", "data_sha256",
                        "tail_used_seconds"):
                self.assertEqual(row[key], reference[key], (item["id"], key))
            self.assertEqual([(t["entry_at"], t["side"], round(t["net_usdt"], 6)) for t in row["closed"]],
                             [(c["entry_at"], c["side"], c["net_usdt"]) for c in reference["cycles"]])
            self.assertAlmostEqual(sum(t["net_usdt"] for t in row["closed"]), row["pnl_usdt"], places=6)
            funded += any(t["funding_usdt"] for t in row["closed"])
        self.assertGreater(sum(r["trades"] for r in rows), 0)
        self.assertGreater(funded, 0, "fixture must exercise a realized funding event")

    def test_parallel_workers_give_identical_ledgers(self):
        _, windows, _ = team_eval.plan_windows(self.store, "DEV", "2026-01-01T01:00:00Z", "2026-01-01T07:00:00Z", 3600, 3600, 720)
        serial = team_eval.run_windows(self.raw, windows, 720, self.store, team_eval.DEFAULT_INSTRUMENT, 1)
        parallel = team_eval.run_windows(self.raw, windows, 720, self.store, team_eval.DEFAULT_INSTRUMENT, 2)
        strip = lambda rows: [{k: v for k, v in r.items() if k != "worker_peak_rss_mb"} for r in rows]
        self.assertEqual(strip(serial), strip(parallel))

    def test_run_command_writes_ledger_gates_and_registry(self):
        out, registry = self.root / "out", self.root / "registry.jsonl"
        buffer = io.StringIO()
        with mock.patch.object(research_limits, "research_slot", no_lease), redirect_stdout(buffer):
            code = team_eval.main(["run", "--recipe", str(self.recipe), "--store", str(self.store), "--start", "2026-01-01T01:00:00Z",
                                   "--admission-hours", "1", "--workers", "1", "--out", str(out), "--registry", str(registry),
                                   "--author", "fixture", "--label", "t1", "--quiet"])
        self.assertEqual(code, 0)
        summary = json.loads(buffer.getvalue())
        saved = json.loads((out / "ledger.json").read_text(encoding="utf-8"))
        gates = json.loads((out / "gates.json").read_text(encoding="utf-8"))
        line = json.loads(registry.read_text(encoding="utf-8").splitlines()[-1])
        m = gates["metrics"]
        self.assertEqual(saved["data_set"], "DEV")
        self.assertEqual(m["closed_trades"] + m["overlap_dropped_trades"], len(saved["trades"]))
        self.assertGreater(m["overlap_dropped_trades"], 0, "1-hour windows with a 12-minute tail overlap their neighbours")
        self.assertEqual(gates["verdict"]["verdict"], "NEEDS_SEQUENTIAL_RUN")
        self.assertEqual(line["verdict"], gates["verdict"]["verdict"])
        self.assertEqual(summary["net_usdt"], m["net_usdt"])
        self.assertIsNotNone(m["account_drawdown_pct_of_capital"])
        self.assertGreaterEqual(m["account_drawdown_pct_of_capital"] + 1e-9, m["closed_trade_drawdown_pct_of_capital"])
        self.assertIn("verdict", (out / "gates.md").read_text(encoding="utf-8"))
        self.assertEqual(team_eval.build_gates(saved)["metrics"], m)

    def test_open_position_at_the_end_of_the_data_is_force_closed_at_the_last_mark(self):
        _, windows, clipped = team_eval.plan_windows(self.store, "DEV", "2026-01-01T10:58:00Z", None, 3600, 3600, 86400)
        self.assertEqual((len(windows), clipped, windows[0]["tail_seconds"]), (1, 1, 60))
        row = team_eval.run_windows(self.raw, windows, 86400, self.store, team_eval.DEFAULT_INSTRUMENT, 1)[0]
        self.assertEqual((row["status"], row["forced_close"], row["errors"]), ("INCOMPLETE", True, {}))
        forced = [t for t in row["closed"] if t["forced_close"]]
        self.assertEqual(len(forced), 1)
        self.assertEqual(forced[0]["exit_reason"], "forced_close_end_of_period")
        self.assertTrue(forced[0]["executions"][-1]["forced"])
        self.assertAlmostEqual(sum(t["net_usdt"] for t in row["closed"]), row["pnl_usdt"], places=6)
        for trade in row["closed"]:
            self.assertLessEqual(trade["mtm_worst_usdt"], 0)
            self.assertGreaterEqual(trade["mtm_best_usdt"], 0)


class GuardTests(unittest.TestCase):
    def test_dev_never_reads_a_vault_or_past_the_dev_cutoff(self):
        with tempfile.TemporaryDirectory() as folder:
            vault, _, _ = make_store(Path(folder) / "Trading-vault" / "s1")
            with self.assertRaises(SystemExit):
                team_eval.plan_windows(vault, "DEV", "2026-01-01T01:00:00Z", None, 3600, 3600, 720)
            team_eval.plan_windows(vault, "SEALED", "2026-01-01T01:00:00Z", None, 3600, 3600, 720)
        first = team_eval.DEV_LAST_OPEN_MS - 2 * DAY * 1000
        windows, _ = team_eval.make_windows(first, team_eval.DEV_LAST_OPEN_MS + 5 * DAY * 1000, None, None, 3600, 3600, 720)
        self.assertTrue(windows)
        clipped, count = team_eval.make_windows(first, team_eval.DEV_LAST_OPEN_MS, None, None, 3600, 3600, 7200)
        self.assertGreater(count, 0)
        for item in clipped:
            self.assertLessEqual(item["start_s"] + 3600 + item.get("tail_seconds", 7200), team_eval.DEV_LAST_OPEN_MS // 1000)

    def test_windows_need_warmup_and_end_with_the_data(self):
        first = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp()) * 1000
        last = first + 3 * DAY * 1000 - 60_000
        windows, clipped = team_eval.make_windows(first, last, None, None, DAY, DAY, 7620)
        self.assertEqual(windows[0]["start_s"], first // 1000 + DAY, "first full day after the 31-minute warm-up")
        self.assertEqual((len(windows), clipped), (1, 0), "a window needs its whole admission inside the data")
        late, clipped = team_eval.make_windows(first, last, None, None, DAY - 7200, DAY, 7620)
        self.assertEqual((len(late), clipped, late[-1]["tail_seconds"]), (2, 1, 7200 - 60))
        with self.assertRaises(SystemExit):
            team_eval.make_windows(first, last, "2025-01-01T00:10:00Z", None, 3600, 3600, 720)


DEV_FIRST, DEV_LAST = datetime(2020, 1, 2, tzinfo=timezone.utc), datetime(2026, 10, 1, tzinfo=timezone.utc)


def spread(first, last, every_days, net, hour=10):
    """Equal trades every few days in [first, last], one hour long."""
    out, day = [], first
    while day <= last:
        stamp = day.strftime(f"%Y-%m-%dT{hour:02d}:00:00Z")
        out.append(trade("w", stamp, stamp.replace(f"T{hour:02d}", f"T{hour + 1:02d}"), net))
        day += timedelta(days=every_days)
    return out


class GateTests(unittest.TestCase):
    """Charter 1.5: whole DEV + epochs E2, E2 tail and E1 (D100/D102), one account (D097)."""

    def setUp(self):
        self.windows = daily_windows(DEV_FIRST, DEV_LAST)

    def status(self, trades, paperwork=None):
        m = team_eval.gate_metrics(ledger(trades, self.windows))
        verdict = team_eval.gate_verdicts(m, "DEV", paperwork)
        return m, verdict["verdict"], {r["check"]: r["status"] for r in verdict["checks"]}

    def test_a_modern_steady_strategy_passes_with_paperwork(self):
        e1 = spread(datetime(2020, 1, 5, tzinfo=timezone.utc), datetime(2023, 12, 20, tzinfo=timezone.utc), 20, -0.01)
        e2 = spread(datetime(2024, 1, 3, tzinfo=timezone.utc), datetime(2026, 9, 30, tzinfo=timezone.utc), 6, 0.1)
        m, verdict, status = self.status(e1 + e2, {"valid": True, "prereg_and_trials": True})
        self.assertEqual(verdict, "PASS", status)
        e = m["epochs"]
        self.assertEqual((e["E2"]["quarters_full"], e["E2"]["quarters_active"], e["E2"]["positive_active_quarter_share"]), (11, 11, 1.0))
        self.assertTrue(e["E1"]["covered"] and e["E2"]["covered"] and e["E2_tail"]["covered"])
        self.assertGreaterEqual(e["E2_tail"]["closed_trades"], 15)
        self.assertEqual(self.status(e1 + e2, {"valid": True})[1], "NOT_ALL_CHECKS_EVALUATED")

    def test_old_epoch_effects_do_not_carry_a_dead_modern_market(self):
        e1 = spread(datetime(2020, 1, 5, tzinfo=timezone.utc), datetime(2023, 12, 20, tzinfo=timezone.utc), 3, 0.2)
        e2 = spread(datetime(2024, 1, 3, tzinfo=timezone.utc), datetime(2026, 9, 30, tzinfo=timezone.utc), 6, -0.02)
        m, verdict, status = self.status(e1 + e2, {"valid": True, "prereg_and_trials": True})
        self.assertGreater(m["net_usdt"], 0)
        self.assertEqual(verdict, "FAIL")
        self.assertEqual((status["net_positive"], status["E2_net_and_stress"], status["E2_positive_active_quarter_share"], status["E2_tail"]),
                         ("PASS", "FAIL", "FAIL", "FAIL"))

    def test_a_fading_tail_and_a_heavy_old_loss_each_fail(self):
        good = spread(datetime(2024, 1, 3, tzinfo=timezone.utc), datetime(2025, 6, 28, tzinfo=timezone.utc), 4, 0.2)
        fading = spread(datetime(2025, 7, 2, tzinfo=timezone.utc), datetime(2026, 9, 30, tzinfo=timezone.utc), 8, -0.05)
        old = spread(datetime(2020, 1, 5, tzinfo=timezone.utc), datetime(2023, 12, 20, tzinfo=timezone.utc), 5, -0.025)
        m, verdict, status = self.status(good + fading + old)
        self.assertEqual(status["E2_net_and_stress"], "PASS")
        self.assertEqual(status["E2_tail"], "FAIL", m["epochs"]["E2_tail"])
        self.assertEqual(status["E1_stress_floor"], "FAIL", m["epochs"]["E1"])
        self.assertLess(m["epochs"]["E1"]["stress_net_pct_of_capital"], -5)

    def test_whole_dev_arithmetic_best_trades_years_and_drawdowns(self):
        trades = spread(datetime(2024, 1, 3, tzinfo=timezone.utc), datetime(2024, 1, 30, tzinfo=timezone.utc), 3, -1.0)
        trades.append(trade("w", "2024-02-10T10:00:00Z", "2024-02-10T11:00:00Z", 12.0))
        m, verdict, status = self.status(trades)
        self.assertEqual(m["closed_trades"], 11)
        self.assertAlmostEqual(m["net_usdt"], 2.0)
        self.assertAlmostEqual(m["stress_net_usdt"], 2.0 - 11 * 20 * 5 / 10000)
        self.assertAlmostEqual(m["net_without_best_trades_usdt"], 2.0 - (12 - 9))
        self.assertAlmostEqual(m["best_trade_share_of_gross_profit"], 1.0)
        self.assertAlmostEqual(m["closed_trade_drawdown_pct_of_capital"], 10.0)
        self.assertEqual(next(y for y in m["years"] if y["year"] == "2024")["net_usdt"], 2.0)
        self.assertEqual((status["closed_trades"], status["net_without_best_trades"], status["E2_closed_trades"]), ("FAIL", "FAIL", "FAIL"))
        self.assertIsNone(m["account_drawdown_pct_of_capital"], "hand-made trades carry no minute marks")
        sealed = team_eval.build_gates(ledger(trades, self.windows, "SEALED"))
        self.assertEqual(sealed["verdict"]["verdict"], "INSUFFICIENT")
        self.assertNotIn("epochs", sealed["metrics"], "Judge output carries aggregates only")

    def test_years_and_quarters_follow_the_entry_time(self):
        windows = daily_windows(datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 12, 31, tzinfo=timezone.utc))
        trades = [trade("w", "2024-12-31T23:00:00Z", "2025-01-01T02:00:00Z", -11.0)]
        m = team_eval.gate_metrics(ledger(trades, windows))
        self.assertEqual([(y["year"], y["net_usdt"]) for y in m["years"]], [("2024", -11.0)])
        self.assertEqual(next(q for q in m["quarters"] if q["quarter"] == "2024Q4")["trades"], 1)
        self.assertIn("2025-01-01", m["daily_net_usdt"], "daily PnL stays on the exit day")
        self.assertFalse(m["epochs"]["E1"]["covered"])
        verdict = team_eval.gate_verdicts(m, "DEV", {"valid": True, "prereg_and_trials": True})
        self.assertEqual({r["check"]: r["status"] for r in verdict["checks"]}["E1_stress_floor"], "NOT_EVALUATED")

    def test_one_account_drops_overlaps_and_asks_for_a_sequential_run_above_five_percent(self):
        base = spread(datetime(2024, 1, 3, tzinfo=timezone.utc), datetime(2024, 3, 30, tzinfo=timezone.utc), 1, 0.1)
        overlapping = [trade("w2", t["entry_at"].replace("T10:00", "T10:30"), t["exit_at"].replace("T11:00", "T12:00"), 5.0) for t in base[:3]]
        m = team_eval.gate_metrics(ledger(base + overlapping, self.windows))
        self.assertEqual((m["overlap_dropped_trades"], m["closed_trades"]), (3, len(base)))
        self.assertAlmostEqual(m["overlap_dropped_net_usdt"], 15.0)
        self.assertLessEqual(m["overlap_dropped_share"], 0.05)
        many = base + [trade("w2", t["entry_at"].replace("T10:00", "T10:30"), t["exit_at"], 5.0) for t in base[:10]]
        m = team_eval.gate_metrics(ledger(many, self.windows))
        self.assertGreater(m["overlap_dropped_share"], 0.05)
        self.assertEqual(team_eval.gate_verdicts(m, "DEV")["verdict"], "NEEDS_SEQUENTIAL_RUN")

    def test_account_drawdown_includes_the_open_position_at_minute_closes(self):
        a = trade("w", "2024-01-03T10:00:00Z", "2024-01-03T11:00:00Z", 2.0)
        a.update(mtm_best_usdt=6.0, mtm_worst_usdt=-1.0, mtm_drawdown_usdt=4.0)
        b = trade("w", "2024-01-04T10:00:00Z", "2024-01-04T11:00:00Z", -1.0)
        b.update(mtm_best_usdt=0.5, mtm_worst_usdt=-9.0, mtm_drawdown_usdt=9.5)
        m = team_eval.gate_metrics(ledger([a, b], self.windows))
        self.assertAlmostEqual(m["closed_trade_drawdown_pct_of_capital"], 1.0)
        self.assertAlmostEqual(m["account_drawdown_pct_of_capital"], 6.0 - (2.0 - 9.0))  # in-trade peak of A to B's trough
        self.assertEqual(m["worst_in_trade_move_usdt"], -9.0)
        d = team_eval.gate_verdicts(m, "SEALED")
        self.assertEqual({r["check"]: r["status"] for r in d["checks"]}["account_drawdown_pct_of_capital"], "FAIL")

    def test_windows_with_data_errors_fail_and_forced_closes_do_not(self):
        windows = [dict(w) for w in self.windows]
        windows[3].update(status="INCOMPLETE", forced_close=True, errors={})
        trades = spread(datetime(2024, 1, 3, tzinfo=timezone.utc), datetime(2024, 1, 30, tzinfo=timezone.utc), 3, 0.1)
        m = team_eval.gate_metrics(ledger(trades, windows))
        self.assertEqual(m["windows_with_errors"], [])
        windows[4].update(status="INCOMPLETE", forced_close=False, errors={"data_gap": 1})
        m = team_eval.gate_metrics(ledger(trades, windows))
        self.assertEqual(m["windows_with_errors"], [windows[4]["id"]])
        self.assertEqual({r["check"]: r["status"] for r in team_eval.gate_verdicts(m, "DEV")["checks"]}["no_engine_or_data_errors"], "FAIL")

    def test_conservative_gap_funding_follows_the_open_position(self):
        policy = {"first_real_funding_ms": int(datetime(2025, 7, 1, tzinfo=timezone.utc).timestamp()) * 1000,
                  "first_real_funding_utc": "2025-07-01T00:00:00Z", "settlement_hours_utc": [0, 8, 16],
                  "conservative_rate": 0.0001, "sensitivity_rate": 0.0003}
        held = trade("w", "2025-06-30T07:00:00Z", "2025-07-01T09:00:00Z", 1.0, notional=55.0)
        held["executions"] = [{"at": "2025-06-30T07:00:00Z", "kind": "entry", "quantity": 2.0, "price_usdt": 10.0, "notional_usdt": 20.0, "fee_usdt": 0.01},
                              {"at": "2025-06-30T12:00:00Z", "kind": "add", "quantity": 1.0, "price_usdt": 10.0, "notional_usdt": 10.0, "fee_usdt": 0.005},
                              {"at": "2025-06-30T20:00:00Z", "kind": "reduce", "quantity": 1.5, "price_usdt": 12.0, "notional_usdt": 18.0, "fee_usdt": 0.009},
                              {"at": "2025-07-01T09:00:00Z", "kind": "close", "quantity": 1.5, "price_usdt": 12.0, "notional_usdt": 18.0, "fee_usdt": 0.009}]
        quick = trade("w", "2025-06-29T09:00:00Z", "2025-06-29T10:00:00Z", 0.5)
        book = ledger([held, quick], self.windows)
        book["funding_policy"] = policy
        gates = team_eval.build_gates(book)
        self.assertAlmostEqual(gates["metrics_base"]["net_usdt"], 1.5)
        self.assertAlmostEqual(gates["metrics"]["gap_funding_charged_usdt"], (20 + 30) * 0.0001, msg="quantities at entry cost")
        self.assertAlmostEqual(gates["metrics"]["net_usdt"], 1.5 - 0.005)
        self.assertEqual(gates["metrics"]["scenario"], "conservative")
        self.assertAlmostEqual(gates["metrics_sensitivity"]["gap_funding_charged_usdt"], 50 * 0.0003)
        self.assertEqual(team_eval.open_notional_at(held["executions"], team_eval.parse_utc("2025-07-01T00:00:00Z")), 15.0,
                         "after the reduce half of the cost basis stays, not entry minus exit notional")
        self.assertEqual(team_eval.gap_settlements(100, 50, None, [0]), [])

    def test_settlements_missing_inside_the_history_are_charged_too(self):
        h = 3_600_000
        start = int(datetime(2021, 9, 1, tzinfo=timezone.utc).timestamp()) * 1000
        times = [start + k * 8 * h for k in range(6)] + [start + 30 * 8 * h + k * 8 * h for k in range(4)]
        times[2] += 2 * h + 54 * 60_000  # a record 2 h 54 min late still covers its slot
        runs = team_eval.missing_settlement_runs(times, [0, 8, 16])
        self.assertEqual(runs, [[start + 6 * 8 * h, start + 29 * 8 * h]])
        found = team_eval.gap_settlements(start // 1000 + 5 * 8 * 3600 + 60, start // 1000 + 8 * 8 * 3600, start // 1000, [0, 8, 16], runs)
        self.assertEqual(found, [(start + k * 8 * h) // 1000 for k in (6, 7, 8)])
        self.assertEqual(team_eval.gap_settlements(start // 1000 + 60, start // 1000 + 5 * 8 * 3600, start // 1000, [0, 8, 16], runs), [])

    def test_missing_funding_policy_fails_instead_of_passing_silently(self):
        trades = spread(datetime(2024, 1, 3, tzinfo=timezone.utc), datetime(2026, 9, 30, tzinfo=timezone.utc), 6, 0.1)
        gates = team_eval.build_gates(ledger(trades, self.windows), {"valid": True, "prereg_and_trials": True})
        status = {r["check"]: r["status"] for r in gates["verdict"]["checks"]}
        self.assertEqual(status["funding_policy_present"], "FAIL")
        self.assertEqual(gates["verdict"]["verdict"], "FAIL")


class CloneTests(unittest.TestCase):
    def setUp(self):
        self.windows = daily_windows(datetime(2025, 1, 1, tzinfo=timezone.utc), datetime(2025, 3, 31, tzinfo=timezone.utc))
        self.base = []
        for i in range(40):
            day = (datetime(2025, 1, 2, tzinfo=timezone.utc) + timedelta(days=2 * i)).strftime("%Y-%m-%d")
            self.base.append(trade("w", f"{day}T10:00:00Z", f"{day}T12:00:00Z", (-1) ** (i // 3) * (1 + i % 5)))

    def test_identical_candidates_are_clones(self):
        result = team_eval.clone_check(ledger(self.base, self.windows), ledger(self.base, self.windows))
        self.assertEqual((result["entry_overlap_a_in_b"], result["daily_pnl_correlation_active_days"], result["verdict"]), (1.0, 1.0, "CLONE"))

    def test_shifted_uncorrelated_candidate_is_distinct_and_edges_count(self):
        other = []
        for i, t in enumerate(self.base):
            day = t["entry_at"][:10]
            other.append(trade("w", f"{day}T13:00:00Z", f"{day}T14:00:00Z", (1 if i % 2 else -1) * 2))
        result = team_eval.clone_check(ledger(self.base, self.windows), ledger(other, self.windows))
        self.assertEqual(result["entry_overlap_a_in_b"], 0.0)
        self.assertLess(result["daily_pnl_correlation_active_days"], 0.8)
        self.assertEqual(result["verdict"], "DISTINCT")
        near = [trade("w", t["entry_at"].replace("T10:00", "T10:30"), t["exit_at"], t["net_usdt"] * -1) for t in self.base]
        edge = team_eval.clone_check(ledger(self.base, self.windows), ledger(near, self.windows))
        self.assertEqual(edge["entry_overlap_a_in_b"], 1.0, "+-30 min is inclusive")
        self.assertEqual(edge["verdict"], "CLONE")

    def test_a_candidate_inside_a_busier_one_is_a_clone_and_e2_only_narrows_the_period(self):
        sparse = [t for i, t in enumerate(self.base) if i % 4 == 0]  # every B entry sits on an A entry
        unrelated = [{**t, "net_usdt": (1 if i % 2 else -3) * 1.0} for i, t in enumerate(sparse)]
        result = team_eval.clone_check(ledger(self.base, self.windows), ledger(unrelated, self.windows))
        self.assertEqual((result["entry_overlap_a_in_b"], result["entry_overlap_b_in_a"], result["entry_overlap_max"]), (0.25, 1.0, 1.0))
        self.assertEqual(result["verdict"], "CLONE")
        span = daily_windows(datetime(2023, 10, 1, tzinfo=timezone.utc), datetime(2024, 3, 31, tzinfo=timezone.utc))
        old = [trade("w", f"2023-11-{d:02d}T10:00:00Z", f"2023-11-{d:02d}T12:00:00Z", 1.0) for d in range(1, 29)]
        new_a = [trade("w", f"2024-02-{d:02d}T10:00:00Z", f"2024-02-{d:02d}T12:00:00Z", 1.0) for d in range(1, 28)]
        new_b = [trade("w", f"2024-02-{d:02d}T16:00:00Z", f"2024-02-{d:02d}T17:00:00Z", (-1) ** d * 1.0) for d in range(1, 28)]
        both = team_eval.clone_check(ledger(old + new_a, span), ledger(old + new_b, span))
        e2 = team_eval.clone_check(ledger(old + new_a, span), ledger(old + new_b, span), epoch="E2")
        self.assertEqual((both["verdict"], e2["verdict"], e2["epoch"], e2["common_days"][0]), ("CLONE", "DISTINCT", "E2", "2024-01-01"))

    def test_a_longer_span_cannot_dilute_the_entry_overlap(self):
        longer = daily_windows(datetime(2024, 10, 1, tzinfo=timezone.utc), datetime(2025, 3, 31, tzinfo=timezone.utc))
        early = [trade("w", f"2024-11-{d:02d}T10:00:00Z", f"2024-11-{d:02d}T12:00:00Z", 1.0) for d in range(1, 29)]
        other = [dict(t, net_usdt=(-1) ** i * 3.0) for i, t in enumerate(self.base)]
        result = team_eval.clone_check(ledger(early + self.base, longer), ledger(other, self.windows))
        self.assertEqual(result["entries_a"], len(self.base), "only entries inside the shared period count")
        self.assertEqual((result["entry_overlap_a_in_b"], result["verdict"]), (1.0, "CLONE"))


class LeaseTests(unittest.TestCase):
    def test_capacity_slots_ram_floor_and_legacy_single_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(research_limits.capacity(folder), 1)
            with research_limits.research_slot(folder, slots=2) as held:
                self.assertEqual(held["slots"], 1, "requests clamp to capacity")
            (Path(folder) / "runtime" / "research_resources" / "capacity.json").write_text('{"slots": 3}', encoding="utf-8")
            self.assertEqual(research_limits.capacity(folder), 3)
            with research_limits.research_slot(folder, slots=2):
                with research_limits.research_slot(folder, slots=1):
                    with self.assertRaises(research_limits.ResearchBusy):
                        with research_limits.research_slot(folder, slots=1):
                            pass
            with research_limits.exclusive_research_slot(folder):
                with research_limits.research_slot(folder, slots=2) as held:
                    self.assertEqual(held["slots"], 2)
            with mock.patch.object(research_limits, "free_ram_gb", return_value=2.0):
                with research_limits.research_slot(folder) as held:
                    self.assertEqual(held["held"], ["heavy.lock"], "a lone run keeps the historical behaviour")
                    with self.assertRaises(research_limits.ResearchBusy):
                        with research_limits.research_slot(folder):
                            pass
                with self.assertRaises(research_limits.ResearchBusy):
                    with research_limits.research_slot(folder, slots=2):
                        pass
                with research_limits.research_slot(folder):
                    pass


POLICY = {"first_real_funding_ms": int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()) * 1000,
          "first_real_funding_utc": "2020-01-01T00:00:00Z", "settlement_hours_utc": [0, 8, 16],
          "conservative_rate": 0.0001, "sensitivity_rate": 0.0003}
V4 = {"engine": "candle-v4 (tools/team_eval_v4.py)", "execution_model_id": "v4-path5s-2"}
GAP_POLICY = {**POLICY, "first_real_funding_ms": int(datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp()) * 1000,
              "first_real_funding_utc": "2026-10-20T00:00:00Z", "conservative_rate": 0.002}  # charges every settlement before


def marked(window, entry, exit_, net, best=None, worst=None, drawdown=None, reason="take_profit", notional=20.0):
    out = trade(window, entry, exit_, net, notional)
    best = max(net, 0.0) if best is None else best
    worst = min(net, 0.0) if worst is None else worst
    out.update(exit_reason=reason, forced_close=reason in team_eval.NOT_CLOSED_FOR_GATE_D, mtm_best_usdt=best,
               mtm_worst_usdt=worst, mtm_drawdown_usdt=max(best - worst, 0.0) if drawdown is None else drawdown,
               executions=[{"at": entry, "kind": "entry", "quantity": notional / 20, "price_usdt": 10.0, "notional_usdt": notional / 2},
                           {"at": exit_, "kind": "close", "quantity": notional / 20, "price_usdt": 10.0, "notional_usdt": notional / 2}])
    return out


def hourly(first, count, net, start_hour=0):
    """count one-hour trades, one every two hours from first."""
    return [marked("w", (first + timedelta(hours=start_hour + 2 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   (first + timedelta(hours=start_hour + 2 * i + 1)).strftime("%Y-%m-%dT%H:%M:%SZ"), net) for i in range(count)]


def manual_declared(trades, n):
    """The written rule of D127 p. 4, coded independently of the tool: one account in entry order, data-forced exits
    out, the first N by entry_at; D2 net and stress, D4 best share and the marked account drawdown up to the N-th exit."""
    account, overlapping, busy = [], [], None
    for t in sorted(trades, key=lambda t: (t["entry_at"], t["exit_at"], t["window_id"])):
        if busy is None or t["entry_at"] >= busy:
            account.append(t)
            busy = t["exit_at"]
        else:
            overlapping.append(t)
    sample = [t for t in account if t["exit_reason"] not in ("forced_close_end_of_period", "data_gap_exit")][:n]
    cutoff = sample[-1]["exit_at"] if len(sample) == n else "9999"
    dropped = sum(t["entry_at"] < cutoff for t in overlapping)
    entered = dropped + sum(t["entry_at"] < cutoff for t in account)
    nets = [t["net_usdt"] for t in sample]
    gross = sum(x for x in nets if x > 0)
    equity = peak = drawdown = 0.0
    for t in sorted(sample, key=lambda t: (t["exit_at"], t["entry_at"])):
        drawdown = max(drawdown, peak - (equity + t["mtm_worst_usdt"]), t["mtm_drawdown_usdt"])
        peak = max(peak, equity + t["mtm_best_usdt"], equity + t["net_usdt"])
        equity += t["net_usdt"]
        drawdown = max(drawdown, peak - equity)
    return {"selected": len(sample), "reached": len(sample) == n, "net": sum(nets),
            "stress": sum(x - t["executed_notional_usdt"] * 5 / 10000 for x, t in zip(nets, sample)),
            "best_share": max(nets) / gross if gross > 0 else None, "drawdown_pct": drawdown / 100 * 100,
            "sha256": gate_d_first_n.canonical_sha256(sample),
            "overlap_share": dropped / entered if entered else 0.0}


class DeclaredNTests(unittest.TestCase):
    """D127 p. 4: gate D on the first N closed trades of the declared sample (gate D procedure sections 2-3)."""

    def setUp(self):
        self.windows = daily_windows(datetime(2026, 10, 3, tzinfo=timezone.utc), datetime(2026, 11, 30, tzinfo=timezone.utc))
        self.first = datetime(2026, 10, 3, tzinfo=timezone.utc)

    def gates(self, trades, n=20, data_set="FORWARD", **extra):
        """A candle-v4 ledger unless the test says otherwise: every gate D submission so far is schema 4."""
        return team_eval.build_gates({**ledger(trades, self.windows, data_set), "funding_policy": POLICY, **V4, **extra}, declared_n=n)

    def status(self, gates):
        return {r["check"]: r["status"] for r in gates["verdict"]["checks"]}

    def test_insufficient_until_the_nth_closed_trade_and_forced_exits_do_not_count(self):
        trades = hourly(self.first, 19, 0.2)
        trades.append(marked("w", "2026-10-05T00:00:00Z", "2026-10-05T03:00:00Z", -3.0, reason="data_gap_exit"))
        trades.append(marked("w", "2026-10-06T00:00:00Z", "2026-10-06T05:00:00Z", 4.0, reason="forced_close_end_of_period"))
        g = self.gates(trades)
        self.assertEqual(g["verdict"]["verdict"], "INSUFFICIENT")
        self.assertEqual((g["metrics"]["closed_trades"], g["first_n"]["closed_trades_in_prefix"], g["first_n"]["reached"]), (19, 19, False))
        self.assertEqual(g["first_n"]["excluded_before_nth_exit"], {"forced_close_end_of_period": 1, "data_gap_exit": 1})
        self.assertEqual(g["metrics"]["forced_closes"], 2, "gates.md still shows the exits it left out")
        self.assertAlmostEqual(g["metrics"]["net_usdt"], 19 * 0.2)
        trades.append(marked("w", "2026-10-07T00:00:00Z", "2026-10-07T01:00:00Z", 0.2))
        g = self.gates(trades)
        self.assertEqual((g["verdict"]["verdict"], g["first_n"]["prefix_last_exit_at"]), ("PASS_PENDING_LIVE", "2026-10-07T01:00:00Z"))

    def test_trades_after_the_nth_never_enter_the_verdict(self):
        sample = hourly(self.first, 20, 0.2)
        decided = self.gates(sample)
        later = [marked("w", "2026-10-20T00:00:00Z", "2026-10-20T09:00:00Z", -40.0, worst=-60.0),
                 marked("w", "2026-10-21T00:00:00Z", "2026-10-21T01:00:00Z", 90.0)]
        windows = [dict(w) for w in self.windows]
        windows[30].update(status="INCOMPLETE", errors={"data_gap": 3})  # a hole long after the N-th exit
        again = team_eval.build_gates({**ledger(sample + later, windows, "FORWARD"), "funding_policy": POLICY, **V4}, declared_n=20)
        for key in ("net_usdt", "stress_net_usdt", "best_trade_share_of_gross_profit", "account_drawdown_pct_of_capital"):
            self.assertEqual(again["metrics"][key], decided["metrics"][key], key)
        self.assertEqual(again["verdict"]["verdict"], decided["verdict"]["verdict"])
        self.assertEqual(again["first_n"]["prefix_trades_sha256"], decided["first_n"]["prefix_trades_sha256"])
        self.assertEqual(again["first_n"]["later_closed_trades_ignored"], 2)
        windows[1].update(status="INCOMPLETE", errors={"data_gap": 1})  # a hole before the N-th exit still fails
        failed = team_eval.build_gates({**ledger(sample + later, windows, "FORWARD"), "funding_policy": POLICY, **V4}, declared_n=20)
        self.assertEqual(self.status(failed)["no_engine_or_data_errors"], "FAIL")
        self.assertEqual(failed["verdict"]["verdict"], "NEEDS_DATA_FIX", "a broken run is not judged; no final FAIL from it")
        unpriced = team_eval.build_gates({**ledger(sample, self.windows, "FORWARD"), **V4}, declared_n=20)
        self.assertEqual(unpriced["verdict"]["verdict"], "NEEDS_DATA_FIX", "no funding policy: the conservative scenario is missing")

    def test_a_v4_hole_later_on_the_day_of_the_nth_exit_changes_nothing(self):
        sample = hourly(self.first, 20, 0.2)  # the 20th exits 2026-10-04T15:00:00Z
        windows = [dict(w) for w in self.windows]
        windows[1].update(status="INCOMPLETE", errors={"data_gap": 30})
        later = self.gates(sample, windows=windows, data_gaps=[["2026-10-04T20:00:00Z", 30]])
        self.assertEqual(later["verdict"]["verdict"], "PASS_PENDING_LIVE")
        self.assertEqual(later["metrics"]["windows_with_errors"], [])
        earlier = self.gates(sample, windows=windows, data_gaps=[["2026-10-04T09:00:00Z", 30]])
        self.assertEqual(earlier["verdict"]["verdict"], "NEEDS_DATA_FIX")
        exact = self.gates(sample, windows=windows, engine=None)  # the exact engine knows only the window, so its whole day counts
        self.assertEqual(exact["verdict"]["verdict"], "NEEDS_DATA_FIX")

    def test_exact_engine_window_cut_closes_send_the_verdict_to_the_sequential_run(self):
        """D128: a forced close at the end of a full 24 h window is the cut, not the data end; the data-end one is excluded."""
        windows = [dict(w, tail_policy_seconds=86820, tail_seconds=86820) for w in self.windows]
        cut = marked(windows[1]["id"], "2026-10-04T20:00:00Z", "2026-10-06T00:07:00Z", -2.0, reason="forced_close_end_of_period")
        sample = hourly(self.first, 10, 0.2) + [cut] + [dict(t, window_id=windows[5]["id"]) for t in hourly(datetime(2026, 10, 8, tzinfo=timezone.utc), 10, 0.2)]
        g = self.gates(sample, windows=windows, engine=None)
        self.assertEqual(g["first_n"]["window_cut_closes_before_nth_exit"], 1)
        self.assertEqual((g["verdict"]["verdict"], self.status(g)["window_cut_closes_before_nth_exit"]), ("NEEDS_SEQUENTIAL_RUN", "FAIL"))
        self.assertEqual(self.gates(sample[:12], windows=windows, engine=None)["verdict"]["verdict"], "NEEDS_SEQUENTIAL_RUN",
                         "before N is reached a window cut already makes the windowed count unreliable")
        windows[1].update(tail_seconds=3600)  # the data ended inside this window's tail: a real end-of-data close
        g = self.gates(sample, windows=windows, engine=None)
        self.assertEqual((g["first_n"]["window_cut_closes_before_nth_exit"], g["verdict"]["verdict"]), (0, "PASS_PENDING_LIVE"))
        windows[1].update(tail_seconds=86820)
        later = self.gates(sample[:10] + sample[11:] + [dict(cut, entry_at="2026-10-20T20:00:00Z", exit_at="2026-10-22T00:07:00Z")],
                           windows=windows, engine=None)
        self.assertEqual((later["first_n"]["window_cut_closes_before_nth_exit"], later["verdict"]["verdict"]), (0, "PASS_PENDING_LIVE"),
                         "a cut after the N-th exit never enters the verdict")
        v4 = self.gates(sample, windows=windows)
        self.assertNotIn("window_cut_closes_before_nth_exit", v4["first_n"], "candle v4 has one account and no windows to cut")
        self.assertEqual(v4["verdict"]["verdict"], "PASS_PENDING_LIVE")

    def test_the_tool_equals_the_judges_recipe(self):
        """gate D procedure v1.1 section 4: after the release both ways give the same prefix hash and the same verdict."""
        import random
        rng = random.Random(4)
        verdicts = Counter()
        for case in range(80):
            trades, at = [], self.first
            for _ in range(rng.randint(15, 45)):
                at += timedelta(minutes=rng.randint(30, 900))
                length = timedelta(minutes=rng.randint(5, 1200))
                reason = rng.choice(["take_profit", "stop_loss", "max_hold", "max_hold", "data_gap_exit", "forced_close_end_of_period"])
                net = round(rng.uniform(-1.5, 2.0), 6)
                best, worst = max(net, 0) + rng.uniform(0, 2), min(net, 0) - rng.uniform(0, 4)
                trades.append(marked("w", at.strftime("%Y-%m-%dT%H:%M:%SZ"), (at + length).strftime("%Y-%m-%dT%H:%M:%SZ"), net,
                                     best=best, worst=worst, drawdown=rng.uniform(0, best - worst), reason=reason,
                                     notional=rng.uniform(10, 60)))
                at += length
            n = rng.choice([20, 25, 30])
            book = {**ledger(trades, self.windows, "FORWARD"), "funding_policy": GAP_POLICY, **V4}
            tool, judge = team_eval.build_gates(book, declared_n=n), gate_d_first_n.first_n(book, n)
            m = tool["metrics"]
            self.assertEqual(tool["first_n"]["prefix_trades_sha256"], judge["prefix_trades_sha256"], case)
            self.assertEqual(tool["verdict"]["verdict"], judge["verdict_first_n"], case)
            self.assertEqual((m["closed_trades"], tool["first_n"]["prefix_last_exit_at"]),
                             (judge["closed_trades_in_prefix"], judge["prefix_last_exit_at"] if judge["closed_trades_in_prefix"] == n else None))
            for ours, theirs in (("net_usdt", "net_usdt"), ("stress_net_usdt", "stress_net_usdt"),
                                 ("best_trade_share_of_gross_profit", "best_trade_share_of_gross_profit"),
                                 ("account_drawdown_pct_of_capital", "account_drawdown_usdt"),
                                 ("gap_funding_charged_usdt", "gap_funding_charged_usdt")):
                self.assertAlmostEqual(m[ours] or 0.0, judge[theirs] or 0.0, places=3, msg=(case, ours))
            verdicts[tool["verdict"]["verdict"]] += 1
        self.assertTrue(all(verdicts[v] for v in ("INSUFFICIENT", "FAIL", "PASS_PENDING_LIVE")), verdicts)
        charged = team_eval.build_gates({**ledger(hourly(self.first, 20, 0.2, start_hour=1), self.windows, "FORWARD"), "funding_policy": GAP_POLICY, **V4},
                                        declared_n=20)["metrics"]["gap_funding_charged_usdt"]
        self.assertGreater(charged, 0, "the conservative scenario charges gap funding here")

    def test_d2_and_d4_on_the_selected_trades(self):
        sample = hourly(self.first, 19, 0.5) + [marked("w", "2026-10-06T00:00:00Z", "2026-10-06T01:00:00Z", 8.0)]
        g = self.gates(sample)
        self.assertAlmostEqual(g["metrics"]["best_trade_share_of_gross_profit"], 8.0 / (19 * 0.5 + 8.0), places=6)
        self.assertEqual(self.status(g)["best_trade_share_of_gross_profit"], "FAIL")
        deep = hourly(self.first, 19, 0.3) + [marked("w", "2026-10-06T00:00:00Z", "2026-10-06T08:00:00Z", 0.3, worst=-11.0, drawdown=11.0)]
        g = self.gates(deep)
        self.assertAlmostEqual(g["metrics"]["account_drawdown_pct_of_capital"], 11.0, places=4)  # 11 USDT below the peak of 100 USDT
        self.assertEqual((self.status(g)["account_drawdown_pct_of_capital"], g["verdict"]["verdict"]), ("FAIL", "FAIL"))
        losing = hourly(self.first, 20, -0.05)
        self.assertEqual(self.status(self.gates(losing))["net_positive"], "FAIL")

    def test_the_tool_equals_the_written_rule_on_random_ledgers(self):
        import random
        rng = random.Random(127)
        checked = Counter()
        for case in range(60):
            trades, at, length = [], self.first, timedelta(0)
            overlap_rate = rng.choice([0.0, 0.03, 0.3])
            for _ in range(rng.randint(15, 60)):  # an entry inside the previous trade is an overlap for the one account
                at += timedelta(minutes=rng.randint(1, 400)) + (length if rng.random() >= overlap_rate else -length / 2)
                length = timedelta(minutes=rng.randint(5, 300))
                reason = rng.choice(["take_profit", "stop_loss", "max_hold", "max_hold", "data_gap_exit", "forced_close_end_of_period"])
                net = round(rng.uniform(-2.0, 2.5), 6)
                best, worst = max(net, 0) + rng.uniform(0, 2), min(net, 0) - rng.uniform(0, 3)
                trades.append(marked(f"w{rng.randint(0, 3)}", at.strftime("%Y-%m-%dT%H:%M:%SZ"), (at + length).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                     net, best=best, worst=worst, drawdown=best - worst, reason=reason, notional=rng.uniform(10, 40)))
            n = rng.choice([20, 25, 30])
            expected, g = manual_declared(trades, n), self.gates(trades, n)
            m, info = g["metrics"], g["first_n"]
            self.assertEqual((info["closed_trades_in_prefix"], info["reached"], info["prefix_trades_sha256"]),
                             (expected["selected"], expected["reached"], expected["sha256"]), case)
            self.assertAlmostEqual(m["net_usdt"], expected["net"], places=5)
            self.assertAlmostEqual(m["stress_net_usdt"], expected["stress"], places=5)
            if expected["best_share"] is None:
                self.assertIsNone(m["best_trade_share_of_gross_profit"])
            else:
                self.assertAlmostEqual(m["best_trade_share_of_gross_profit"], expected["best_share"], places=5)
            self.assertAlmostEqual(m["account_drawdown_pct_of_capital"], expected["drawdown_pct"], places=3)
            self.assertAlmostEqual(m["overlap_dropped_share"], expected["overlap_share"], places=5)
            if expected["overlap_share"] > 0.05:
                self.assertEqual(g["verdict"]["verdict"], "NEEDS_SEQUENTIAL_RUN", case)
            else:
                self.assertEqual(g["verdict"]["verdict"] == "INSUFFICIENT", not expected["reached"], case)
            checked[expected["overlap_share"] > 0.05, expected["reached"]] += 1
        self.assertTrue(all(checked[key] for key in [(False, False), (False, True), (True, False), (True, True)]), checked)

    def test_refusals_marking_text_and_unchanged_runs_without_n(self):
        trades = hourly(self.first, 20, 0.2)
        with self.assertRaises(SystemExit):
            self.gates(trades, data_set="DEV")
        with self.assertRaises(SystemExit):
            self.gates(trades, n=19)
        plain = team_eval.build_gates({**ledger(trades, self.windows, "FORWARD"), "funding_policy": POLICY})  # exact engine, no N
        self.assertNotIn("declared_n", plain)
        self.assertEqual(team_eval.declared_registry(plain), {})
        self.assertIn("every minute close", next(r["rule"] for r in plain["verdict"]["checks"] if r["check"] == "account_drawdown_pct_of_capital"))
        v4 = self.gates(trades)
        rule = next(r["rule"] for r in v4["verdict"]["checks"] if r["check"] == "account_drawdown_pct_of_capital")
        self.assertIn("every 5 s", rule)
        self.assertIn("v4-path5s-2", rule)
        self.assertIn("up to the N-th exit", rule)
        self.assertEqual(team_eval.declared_registry(v4)["declared_n"], 20)
        self.assertIn("declared N = 20", team_eval.gates_markdown(v4))

    def test_the_gates_command_recomputes_a_ledger_with_a_declared_n(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ledger.json"
            path.write_text(json.dumps({**ledger(hourly(self.first, 25, 0.2), self.windows, "SEALED"), "funding_policy": POLICY}),
                            encoding="utf-8")
            printed = io.StringIO()
            with redirect_stdout(printed):
                team_eval.main(["gates", "--ledger", str(path), "--declared-n", "20", "--out", str(Path(folder) / "g")])
            line = json.loads(printed.getvalue())
            self.assertEqual((line["verdict"], line["closed_trades"], line["declared_n"]), ("PASS_PENDING_LIVE", 20, 20))
            written = json.loads((Path(folder) / "g" / "gates.json").read_text(encoding="utf-8"))
            self.assertEqual(written["first_n"]["prefix_trades_sha256"], line["prefix_trades_sha256"])
            for argv in (["--set", "dev", "--declared-n", "30"], ["--set", "sealed", "--declared-n", "19"]):
                with self.assertRaises(SystemExit) as refused:
                    team_eval.main(["run", "--recipe", str(path)] + argv)
                self.assertIn("--declared-n is gate D", str(refused.exception))


if __name__ == "__main__":
    unittest.main()
