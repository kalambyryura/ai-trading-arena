"""Offline evaluator boundary and prefix checks, synthetic/temp data only."""
import json
import socket
import unittest
from unittest.mock import patch

import test_arena_eval as eval_fixture
import test_author_backtest as screen_fixture
import test_arena as tick_fixture
from trading_lab import arena_engine
from trading_lab.exchange_api import ExchangeAPI
from trading_lab.arena_jev import SharedJevGate


class EvaluatorBoundaryTests(unittest.TestCase):
    def test_pre_entry_quote_extreme_cannot_trigger_later_position_stop(self):
        # Observed sequence in a synthetic tick fixture, unlike an OHLC path.
        for side, before_price in (("long", 90), ("short", 110)):
            state = tick_fixture.state(side)
            arena_engine.tick(state, tick_fixture.market(1000, before_price))
            self.assertEqual(state["bots"][0]["position_btc"], 0)
            arena_engine.tick(state, tick_fixture.market(1010, 100))
            self.assertNotEqual(state["bots"][0]["position_btc"], 0)
            arena_engine.tick(state, tick_fixture.market(1015, 100))
            self.assertNotEqual(state["bots"][0]["position_btc"], 0)
            self.assertFalse(any(event["code"] == "exit_trigger" for event in state["events"]))

    def test_exact_rules_route_cannot_instantiate_paid_or_exchange_adapter(self):
        fixture = eval_fixture.ArenaEvalTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        deny = AssertionError("offline regression must not contact a provider")
        with patch.object(ExchangeAPI, "__init__", side_effect=deny) as exchange, \
             patch.object(SharedJevGate, "__init__", side_effect=deny) as jev, \
             patch.object(socket.socket, "connect", side_effect=deny), \
             patch.object(socket, "create_connection", side_effect=deny):
            result, _ = eval_fixture.run(fixture.common(
                "--start", eval_fixture.iso(eval_fixture.T0+2400), "--admission-hours", "0.5", "--count", "1",
                "--out", str(fixture.root / "report.json")))
        self.assertEqual(result, 0)
        exchange.assert_not_called()
        jev.assert_not_called()
        report = json.loads((fixture.root / "report.json").read_text(encoding="utf-8"))
        self.assertIn("MODEL", report["execution_evidence"])
        for name in ("decision_kernel_version", "market_feature_version", "execution_model_version", "accounting_version"):
            self.assertTrue(report[name])

    def test_mutating_future_completed_bars_does_not_change_prefix_features_or_decisions(self):
        ab, np = screen_fixture.ab, screen_fixture.np
        bars = screen_fixture.synthetic(90)
        before, _ = ab.features(bars)
        rule = {"all": [{"feature": "return_5m_bps", "op": "lt", "value": 0},
                        {"not": {"feature": "rsi_14", "op": "gt", "value": 60}}]}
        decisions = ab.predicate_mask(before, rule)
        for values in (bars.o, bars.h, bars.l, bars.c):
            values[60:] *= 2
        bars.v[60:] *= 7
        after, _ = ab.features(bars)
        for name in before:
            np.testing.assert_equal(before[name][:60], after[name][:60])
        np.testing.assert_equal(decisions[:60], ab.predicate_mask(after, rule)[:60])


if __name__ == "__main__":
    unittest.main()
