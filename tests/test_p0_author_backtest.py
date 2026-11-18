"""P0-B desired-behaviour regressions. Only bounded in-memory synthetic arrays.

Run via run_tests.py so the same tests load either baseline or proposed source.
No CSV, transport, credentials, subprocess or production process access.
"""
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import numpy as np
from trading_lab import strategy_v2 as v2
from trading_lab.arena_strategies import evaluate as v1_evaluate

from tools import author_backtest as ab  # runner overrides; ordinary unittest uses integrated tools module
T0 = 1_800_000_000_000


def flat(n=24, price=100.):
    a = np.full(n, price)
    return ab.Series(T0+np.arange(n)*60000, a.copy(), a.copy(), a.copy(), a.copy(), np.ones(n))


def plan(S, side=1, sl=50., tp=1000., hold=120., exit_at=None):
    mask = np.zeros(S.n, dtype=bool)
    mask[5] = True
    exit_mask = None
    if exit_at is not None:
        exit_mask = np.zeros(S.n, dtype=bool)
        exit_mask[exit_at] = True
    return ab.Plan("small P0 fixture", 1, mask if side==1 else None, mask if side==-1 else None,
                   exit_mask if side==1 else None, exit_mask if side==-1 else None,
                   sl, tp, hold, 0., 10., [])


def leaf(name="rsi_14", value=0.):
    return {"feature": name, "op": "gt", "value": value}


def direct_recipe(hold):
    return {"name": "already validated fixture", "strategy_schema_version": 2,
            "rules": {"max_entries": 1, "max_spread_bps": 5., "max_hold_seconds": hold,
                      "side": "long", "stop_loss_bps": 50., "take_profit_bps": 100.,
                      "cooldown_seconds": 30., "size_pct": 10., "add_when": None, "reduce_when": None,
                      "cost_checkpoint": None, "exit": None,
                      "entry": {"long": {"setup": None, "confirm": leaf(), "invalidate": None, "expiry_bars": None}}}}


class UnknownTests(unittest.TestCase):
    def test_v2_unknown_all_any_not_matches_shared_kernel(self):
        values = [None, -1., 1.]
        nodes = [leaf(), {"not": leaf()}, {"all": [leaf(), leaf("return_1m_bps")]},
                 {"any": [leaf(), leaf("return_1m_bps")]},
                 {"not": {"any": [leaf(), leaf("return_1m_bps")]}},
                 {"any": [{"all": [leaf(), leaf("return_1m_bps")]}, {"not": leaf("volume_ratio_5")}]},
                 {"feature": "rsi_14", "op": "gte", "ref": "return_1m_bps"}]
        for x in values:
            for y in values:
                F = {"rsi_14": np.array([x if x is not None else np.nan]),
                     "return_1m_bps": np.array([y if y is not None else np.nan]),
                     "volume_ratio_5": np.array([np.nan])}
                scalars = {"rsi_14": x, "return_1m_bps": y, "volume_ratio_5": None}
                for node in nodes:
                    expected = v2.evaluate_predicate(node, scalars)[0]
                    with self.subTest(x=x, y=y, node=node):
                        self.assertEqual(bool(ab.predicate_mask(F, node)[0]), expected is True)

    def test_unknown_verdict_is_retained_separately_from_the_permission_mask(self):
        F = {"rsi_14": np.array([np.nan, -1., 1.])}
        self.assertEqual(ab.predicate_verdicts(F, {"not": leaf()}).tolist(), [None, True, False])

    def test_infinity_and_boolean_are_unknown_in_shared_kernel(self):
        F = {"rsi_14": np.array([np.inf, -np.inf, True, 1.], dtype=object)}
        self.assertEqual(ab.predicate_verdicts(F, {"not": leaf()}).tolist(), [None, None, None, False])

    def test_v1_any_with_known_true_and_unknown_never_passes(self):
        group = {"mode": "any", "conditions": [leaf(), leaf("return_1m_bps")]}
        F = {"rsi_14": np.array([1., np.nan, -1., 1.]), "return_1m_bps": np.array([np.nan, 1., 1., -1.])}
        expected = [v1_evaluate(group, {k: float(v[i]) for k, v in F.items()})[0] for i in range(4)]
        self.assertEqual(ab.group_mask(F, group).tolist(), expected)

    def test_compile_reports_unknown_and_never_arms_it(self):
        recipe = direct_recipe(120)
        recipe["rules"]["entry"]["long"]["confirm"] = {"not": leaf()}
        p = ab.compile_plan(recipe, {"spread_bps": np.array([1., 1.]), "rsi_14": np.array([np.nan, -1.])})
        self.assertEqual(p.long_entry.tolist(), [False, True])
        self.assertEqual(p.unknown_counts["long_entry"], 1)

    def test_unused_missing_provider_does_not_block_but_referenced_missing_is_unsupported(self):
        F = {"rsi_14": np.array([1.]), "spread_bps": np.array([1.])}
        self.assertEqual(ab.predicate_mask(F, leaf()).tolist(), [True])
        with self.assertRaises(ab.Unsupported):
            ab.predicate_mask(F, leaf("funding_rate_bps"))


class NullHoldTests(unittest.TestCase):
    def test_compile_null_hold_explicitly_refuses_instead_of_imposing_24h(self):
        with self.assertRaisesRegex(ab.Unsupported, "null"):
            ab.compile_plan(direct_recipe(None), {"rsi_14": np.array([1.]), "spread_bps": np.array([1.])})

    def test_manual_plan_cannot_bypass_null_hold_refusal(self):
        S = flat()
        with self.assertRaisesRegex(ab.Unsupported, "null"):
            ab.simulate(S, plan(S, hold=None), ab.Cost(0., 0.))


class TimelineTests(unittest.TestCase):
    def test_low_before_long_fill_cannot_stop_later_position(self):
        S = flat(price=110.)
        S.o[6], S.h[6], S.l[6], S.c[6] = 100., 110., 99., 110.
        result = ab.simulate(S, plan(S), ab.Cost(0., 0.), delay_s=20.)
        self.assertEqual(result["reason"].tolist(), ["max_hold"])
        self.assertAlmostEqual(float(result["entry_px"][0]), 102.)
        self.assertAlmostEqual(float(result["exit_px"][0]), 110.)
        self.assertAlmostEqual(float(result["hold_s"][0]), 120.)

    def test_high_before_short_fill_cannot_stop_later_position(self):
        S = flat(price=90.)
        S.o[6], S.h[6], S.l[6], S.c[6] = 100., 101., 90., 90.
        result = ab.simulate(S, plan(S, side=-1), ab.Cost(0., 0.), delay_s=20.)
        self.assertEqual(result["reason"].tolist(), ["max_hold"])
        self.assertAlmostEqual(float(result["entry_px"][0]), 98.)
        self.assertAlmostEqual(float(result["exit_px"][0]), 90.)

    def test_post_fill_adverse_move_in_entry_bar_is_not_discarded(self):
        S = flat(price=98.)
        S.o[6], S.h[6], S.l[6], S.c[6] = 100., 104., 95., 98.
        result = ab.simulate(S, plan(S), ab.Cost(0., 0.), delay_s=20.)
        entry = 102.+2./3.
        stop = entry*(1.-50./10000.)
        crossing = 15. + (104.-stop)/9.*33.75
        self.assertEqual(result["reason"].tolist(), ["stop_loss"])
        self.assertAlmostEqual(float(result["exit_px"][0]), stop, places=8)
        # A float epoch near 1.8e9s has ~0.24us resolution; this is not a
        # declaration of nanosecond execution accuracy from minute OHLC.
        self.assertAlmostEqual(float(result["hold_s"][0]), crossing-20., places=6)
        self.assertGreater(float(result["hold_s"][0]), 0.)

    def test_target_touch_before_fill_is_not_later_profit(self):
        S = flat(price=100.)
        S.o[6], S.h[6], S.l[6], S.c[6] = 100., 110., 100., 100.
        result = ab.simulate(S, plan(S, sl=500., tp=50.), ab.Cost(0., 0.), delay_s=40.)
        self.assertEqual(result["reason"].tolist(), ["stop_loss"])
        self.assertGreater(float(result["hold_s"][0]), 0.)
        self.assertLess(float(result["hold_s"][0]), 20.)

    def test_future_bar_gap_uses_actual_open_and_actual_elapsed_time(self):
        S = flat()
        S.o[7], S.h[7], S.l[7], S.c[7] = 98., 98.5, 97.5, 98.
        result = ab.simulate(S, plan(S), ab.Cost(5., 0.), delay_s=20.)
        self.assertEqual(result["reason"].tolist(), ["stop_loss"])
        self.assertAlmostEqual(float(result["exit_px"][0]), 98.)
        self.assertAlmostEqual(float(result["hold_s"][0]), 40.)
        self.assertAlmostEqual(float(result["net_bps"][0]), -210.)

    def test_max_hold_before_later_bar_extreme_has_time_priority(self):
        S = flat()
        S.o[7], S.h[7], S.l[7], S.c[7] = 100., 102., 90., 101.
        # Authored timeout at 3 seconds into bar7. With SL500bps the
        # post-entry path reaches the stop later than that timeout.
        result = ab.simulate(S, plan(S, sl=500., tp=1000., hold=43.), ab.Cost(0., 0.), delay_s=20.)
        self.assertEqual(result["reason"].tolist(), ["max_hold"])
        self.assertAlmostEqual(float(result["hold_s"][0]), 43.)

    def test_delay_at_or_beyond_a_minute_is_refused_not_misindexed(self):
        S = flat()
        for delay in (-1., 60., 90., float("nan")):
            with self.subTest(delay=delay), self.assertRaises(ab.Unsupported):
                ab.simulate(S, plan(S), ab.Cost(0., 0.), delay_s=delay)

    def test_missing_tail_is_explicitly_refused_no_cash_or_fabricated_close(self):
        S = flat(n=9)
        with self.assertRaisesRegex(ab.Unsupported, "INCOMPLETE"):
            ab.simulate(S, plan(S, sl=500., tp=1000., hold=3600.), ab.Cost(0., 0.))


class ModelReportingTests(unittest.TestCase):
    def test_same_bar_sl_tp_is_model_ambiguous_and_reverse_scenario_is_visible(self):
        S = flat()
        S.o[7], S.h[7], S.l[7], S.c[7] = 100., 101.5, 99., 101.
        p = plan(S, sl=50., tp=100.)
        primary = ab.simulate(S, p, ab.Cost(0., 0.))
        self.assertEqual(primary["reason"].tolist(), ["stop_loss"])
        self.assertEqual(primary["intrabar_path_status"].tolist(), ["INTRABAR_PATH_AMBIGUOUS"])
        reverse = ab.simulate(S, p, ab.Cost(0., 0.), path_scenario="reverse_extremes")
        self.assertEqual(reverse["reason"].tolist(), ["take_profit"])
        self.assertAlmostEqual(float(primary["net_bps"][0]), -50.)
        self.assertAlmostEqual(float(reverse["net_bps"][0]), 100.)

    def test_primary_minute_price_matches_shared_candle_path(self):
        from tools.okx_tape_model import minute_path
        for bar in ((100., 110., 99., 110.), (100., 104., 95., 98.), (100., 100., 100., 100.)):
            reference = minute_path(*bar)[0]
            for second in (0., 1., 3., 20., 59., 60.):
                self.assertAlmostEqual(ab.minute_price(*bar, second), reference(second))

    def test_shared_kernel_is_reused_instead_of_a_second_rule_interpreter(self):
        F = {"rsi_14": np.array([1.])}
        with patch.object(v2, "evaluate_predicate", wraps=v2.evaluate_predicate) as actual_kernel:
            ab.predicate_mask(F, leaf())
            self.assertGreater(actual_kernel.call_count, 0)

    def test_flat_model_cost_is_charged_once_long_and_short(self):
        S = flat()
        cost = ab.Cost(5., 2.)
        for side in (1, -1):
            result = ab.simulate(S, plan(S, side=side, sl=500., tp=1000.), cost)
            entry = 100.*(1.+side*2./10000.)
            exit_px = 100.*(1.-side*2./10000.)
            expected_gross = side*(exit_px/entry-1.)*10000.
            self.assertAlmostEqual(float(result["gross_bps"][0]), expected_gross)
            self.assertAlmostEqual(float(result["net_bps"][0]), expected_gross-10.)
            self.assertAlmostEqual(float(result["hold_s"][0]), 120.)

    def test_report_declares_model_version_unknowns_and_both_sensitivity_scenarios(self):
        S = flat()
        S.o[7], S.h[7], S.l[7], S.c[7] = 100., 101.5, 99., 101.
        p = plan(S, sl=50., tp=100.)
        args = SimpleNamespace(data=[], recipe="unused", start=None, end=None, cost="stress",
                               fee_bps=0., slip_bps=0., delay_s=20., windows=None, split=[], out=None)
        captured = []
        with patch.object(ab, "load_bars", return_value=S), patch.object(ab, "features", return_value=({}, None)), \
             patch.object(ab, "load_recipe", return_value={"recipe_sha256": "fake"}), \
             patch.object(ab, "compile_plan", return_value=p), \
             patch.object(ab, "print_report", side_effect=captured.append):
            self.assertEqual(ab.command_run(args), 0)
        report = captured[0]
        self.assertIn("INTRABAR_PATH_AMBIGUOUS", report["intrabar_evidence"] if "intrabar_evidence" in report else "MISSING")
        self.assertEqual(report["execution_model_version"], "screening-ohlc-causal-path-v2")
        self.assertEqual(report["unknown_rows_by_predicate"], {})
        self.assertEqual(report["periods"][0]["reasons"], {"stop_loss": 1})
        self.assertEqual(report["path_scenarios"]["periods"][0]["reasons"], {"take_profit": 1})
        self.assertEqual(report["periods"][0]["intrabar_path_ambiguous_trades"], 1)

    def test_cli_adapter_refuses_incomplete_simulation_instead_of_reporting_zero_profit(self):
        S = flat(n=9)
        p = plan(S, sl=500., tp=1000., hold=3600.)
        args = SimpleNamespace(data=[], recipe="unused", start=None, end=None, cost="stress",
                               fee_bps=0., slip_bps=0., delay_s=20., windows=None, split=[], out=None)
        with patch.object(ab, "load_bars", return_value=S), patch.object(ab, "features", return_value=({}, None)), \
             patch.object(ab, "load_recipe", return_value={}), patch.object(ab, "compile_plan", return_value=p), \
             patch.object(ab, "print_report") as report:
            self.assertEqual(ab.command_run(args), 2)
            self.assertFalse(report.called)


if __name__ == "__main__":
    unittest.main()
