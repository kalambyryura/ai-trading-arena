"""Small synthetic/offline tests; never opens any active PAPER/runtime input."""
from copy import deepcopy
from decimal import Decimal, localcontext
from io import BytesIO
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from trading_lab import execution_sensitivity as module
from trading_lab.battle_review import digest, verify_bundle


def seal(review):
    review.pop("integrity", None)
    review["integrity"] = {"bundle_sha256": digest(review), "source_event_count": len(review["events"])}
    return review


def review_for(mode="PAPER", fills=None, *, gross="10", fees="-.105", funding="0", complete=True,
               paired_units=True, no_entry=None):
    paper = mode == "PAPER"
    fills = fills if fills is not None else [
        ("entry", "order-1", "1", "100", "0", "1"),
        ("close", "order-2", "1", "110", "1", "0")]
    events, rows = [], []
    for seq, (role, alias, quantity, price, before, after) in enumerate(fills, 1):
        code = ("paper_" if paper else "exchange_") + ("close_fill" if role == "close" else "entry_fill")
        data = {}
        if not paper and paired_units and after is not None:
            data["position_btc"] = str(Decimal(after) * Decimal(".01"))
        events.append({"seq": seq, "at": "2026-10-26T00:00:00Z", "bot_id": "synthetic-author", "code": code, "data": data})
        rows.append({"seq": seq, "role": role, "order": alias, "quantity": quantity, "price_usdt": price,
                     "position_before": before, "position_after": after,
                     "source": "paper_incremental_btc" if paper else "exchange_incremental_contracts"})
    remaining = fills[-1][-1] if fills else "0"
    btc = Decimal(remaining or "0") * (Decimal(1) if paper else Decimal(".01"))
    net = str(Decimal(gross) + Decimal(fees) + Decimal(funding)) if None not in (gross, fees, funding) else None
    bot = {"id": "synthetic-author", "position_btc": str(btc),
           "funnel": {"entry_filled_orders": no_entry if no_entry is not None else int(bool(fills)),
                      "order_intents": len({fill[1] for fill in fills})},
           "costs": {"gross_pnl_usdt": gross, "signed_fees_usdt": fees, "signed_funding_usdt": funding,
                     "net_usdt": net, "complete": complete, "source": "paper_model_events" if paper else "actual_bills_and_cash_reconciled",
                     "funding_source": "estimated_paper_funding" if paper else "actual_bills_reconciled",
                     "jev": {"known_cost_usd": "0.001", "complete": True, "source": "reported_batch_cost_equal_share_estimate"}},
           "trade_ledger": {"rows": rows, "inventory_known": True, "remaining": remaining, "limitations": [],
                            "missing_order_results": False, "unresolved": False},
           "quality": {"data_status": "COMPLETE" if complete else "INCOMPLETE"}}
    return seal({"schema_version": 1, "match": {"id": "SYNTHETIC-ONLY", "mode": mode, "status": "DONE" if complete else "RUNNING", "incomplete": not complete},
                 "participants": [bot], "events": events, "frames": [], "history_complete": True,
                 "cash": {"net_usdt": "0"}, "projection": "author_equal_v1_allowlist"})


def bot_of(report):
    return report["participants"][0]


def scenario(bot, bps):
    return next(row for row in bot["scenarios"] if row["extra_bps_per_fill"] == str(bps))


class SensitivityTests(unittest.TestCase):
    def setUp(self):
        for attr in ("connect", "connect_ex"):
            self.enterContext(patch.object(socket.socket, attr, side_effect=AssertionError("Network forbidden")))
        self.enterContext(patch.object(socket, "create_connection", side_effect=AssertionError("Network forbidden")))

    def test_model_long_costs_charged_once_and_input_is_immutable(self):
        review = review_for()
        before = deepcopy(review)
        report = module.analyze_review(review)
        bot = bot_of(report)
        self.assertEqual(review, before)
        self.assertFalse(report["dynamic_replay"])
        self.assertEqual(bot["cost_kind"], "MODEL")
        self.assertEqual(Decimal(bot["full_recorded_fill_notional_usdt"]), Decimal(210))
        self.assertEqual(Decimal(scenario(bot, 0)["recorded_net_after_overlay_usdt"]), Decimal("9.895"))
        self.assertEqual(Decimal(scenario(bot, 2)["additional_cost_usdt"]), Decimal(".042"))
        self.assertEqual(Decimal(scenario(bot, 5)["recorded_net_after_overlay_usdt"]), Decimal("9.79"))
        self.assertEqual(bot["jev_usd"]["known_cost_usd"], "0.001")
        self.assertFalse(bot["jev_usd"]["included_in_trading_usdt"])
        self.assertIsNone(bot["spread_slippage_usdt"])
        self.assertEqual(bot["status"], "COMPLETE_FIXED_LEDGER")

    def test_short_add_and_two_partial_closes_charge_all_fill_notional(self):
        fills = [("entry", "order-1", "1", "100", "0", "-1"),
                 ("add", "order-2", ".5", "90", "-1", "-1.5"),
                 ("close", "order-3", ".4", "80", "-1.5", "-1.1"),
                 ("close", "order-4", "1.1", "85", "-1.1", "0")]
        bot = bot_of(module.analyze_review(review_for(fills=fills, gross="19.5", fees="-.13525", funding=".02")))
        self.assertEqual(bot["positive_fill_observations"], 4)
        self.assertEqual(Decimal(bot["full_recorded_fill_notional_usdt"]), Decimal("270.5"))
        self.assertEqual(Decimal(scenario(bot, 5)["additional_cost_usdt"]), Decimal(".13525"))
        self.assertEqual(Decimal(scenario(bot, 5)["recorded_net_after_overlay_usdt"]), Decimal("19.2495"))

    def test_actual_partial_order_vwap_is_not_incremental_fill_price(self):
        fills = [("entry", "order-1", "1", "100", "0", "1"),
                 ("entry", "order-1", "1", "110", "1", "2"),
                 ("close", "order-2", "1", "115", "2", "1"),
                 ("close", "order-2", "1", "117.5", "1", "0")]
        bot = bot_of(module.analyze_review(review_for("OKX_LIVE", fills, gross=".15", fees="-.00455", funding="-.0001")))
        self.assertEqual(bot["cost_kind"], "ACTUAL_OBSERVATIONS")
        self.assertEqual(Decimal(bot["contract_btc"]), Decimal(".01"))
        self.assertEqual(Decimal(bot["full_recorded_fill_notional_usdt"]), Decimal("4.55"))
        self.assertNotEqual(Decimal(bot["full_recorded_fill_notional_usdt"]), Decimal("4.425"))
        self.assertEqual(Decimal(scenario(bot, 2)["additional_cost_usdt"]), Decimal(".00091"))
        self.assertEqual(Decimal(scenario(bot, 5)["recorded_net_after_overlay_usdt"]), Decimal(".143075"))

    def test_zero_quantity_fee_revision_preserves_final_cost_without_another_fill(self):
        fills = [("entry", "order-1", "1", "100", "0", "1"),
                 ("close", "order-2", "1", "110", "1", "0"),
                 ("close", "order-2", "0", "110", "0", "0")]
        bot = bot_of(module.analyze_review(review_for("OKX_DEMO", fills, gross=".1", fees="-.003")))
        self.assertEqual(bot["positive_fill_observations"], 2)
        self.assertEqual(Decimal(bot["full_recorded_fill_notional_usdt"]), Decimal("2.1"))
        self.assertEqual(Decimal(bot["base_trading_usdt"]["net_usdt"]), Decimal(".097"))

    def test_missing_contract_units_are_unknown_never_assumed_point_zero_one(self):
        bot = bot_of(module.analyze_review(review_for("OKX_LIVE", paired_units=False)))
        self.assertIsNone(bot["contract_btc"])
        self.assertIsNone(bot["full_recorded_fill_notional_usdt"])
        self.assertIsNone(scenario(bot, 2)["additional_cost_usdt"])
        self.assertIsNone(scenario(bot, 5)["settled_eligible_net_after_overlay_usdt"])
        self.assertEqual(Decimal(scenario(bot, 0)["recorded_net_after_overlay_usdt"]), Decimal("9.895"))

    def test_conflicting_retained_units_are_unknown(self):
        review = review_for("OKX_LIVE")
        review["events"][-1]["data"]["position_btc"] = ".01"  # ledger is flat here
        bot = bot_of(module.analyze_review(seal(review)))
        self.assertIsNone(bot["contract_btc"])
        self.assertIn("inconsistent_or_rounded_unit_pairs", bot["issues"])

    def test_confirmed_no_entry_has_zero_overlay_but_missing_ledger_is_not_cash(self):
        review = review_for(fills=[], gross="0", fees="0", no_entry=0)
        bot = bot_of(module.analyze_review(review))
        self.assertEqual(bot["participation"], "NO_ENTRY")
        self.assertTrue(all(row["additional_cost_usdt"] == "0" for row in bot["scenarios"]))
        review["participants"][0]["funnel"]["entry_filled_orders"] = 1
        bot = bot_of(module.analyze_review(seal(review)))
        self.assertEqual(bot["participation"], "UNKNOWN")
        self.assertIsNone(bot["full_recorded_fill_notional_usdt"])
        self.assertIsNone(scenario(bot, 5)["additional_cost_usdt"])

    def test_incomplete_or_open_record_retains_diagnostic_net_without_eligible_result(self):
        review = review_for(fills=[("entry", "order-1", "1", "100", "0", "1")], gross="0", fees="-.05", complete=False)
        bot = bot_of(module.analyze_review(review))
        self.assertEqual(Decimal(scenario(bot, 5)["recorded_net_after_overlay_usdt"]), Decimal("-.1"))
        self.assertIsNone(scenario(bot, 5)["settled_eligible_net_after_overlay_usdt"])
        self.assertEqual(bot["status"], "INCOMPLETE_DIAGNOSTIC_ONLY")

    def test_pending_missing_result_from_build_review_is_unknown_not_no_entry(self):
        from trading_lab.battle_review import build_review
        from tests.test_cycle_diagnostics import state_for
        state = state_for()
        state.update(id="SYNTHETIC-D02-PENDING", mode="OKX_DEMO", budget_usdt=100,
                     frames=[], history_complete=True)
        state["bots"][0]["_orders"] = [{"request": {
            "clOrdId": "synthetic-pending", "reduceOnly": False, "side": "buy"}, "result": None}]
        review = build_review(state)
        source = review["participants"][0]
        self.assertEqual(source["funnel"]["entry_filled_orders"], 0)
        self.assertEqual(source["funnel"]["order_intents"], 1)
        self.assertEqual(source["trade_ledger"]["rows"], [])
        bot = bot_of(module.analyze_review(review))
        self.assertEqual(bot["participation"], "UNKNOWN")
        self.assertIsNone(bot["full_recorded_fill_notional_usdt"])
        self.assertIsNone(scenario(bot, 5)["additional_cost_usdt"])
        self.assertEqual(bot["status"], "INCOMPLETE_DIAGNOSTIC_ONLY")

    def test_no_entry_needs_complete_history_inventory_and_order_resolution(self):
        for defect in ("history", "inventory", "limitation", "remaining", "position", "missing", "pending"):
            with self.subTest(defect=defect):
                review = review_for(fills=[], gross="0", fees="0", no_entry=0)
                source = review["participants"][0]
                ledger = source["trade_ledger"]
                if defect == "history": review["history_complete"] = False
                elif defect == "inventory": ledger["inventory_known"] = False
                elif defect == "limitation": ledger["limitations"] = ["synthetic gap"]
                elif defect == "remaining": ledger["remaining"] = None
                elif defect == "position": source["position_btc"] = None
                elif defect == "missing": ledger["missing_order_results"] = True
                else: ledger["unresolved"] = True
                bot = bot_of(module.analyze_review(seal(review)))
                self.assertEqual(bot["participation"], "UNKNOWN")
                self.assertIsNone(scenario(bot, 2)["additional_cost_usdt"])

    def test_legacy_absent_order_flags_require_confirmed_zero_intents(self):
        for intents, expected in ((None, "UNKNOWN"), (1, "UNKNOWN"), (0, "NO_ENTRY")):
            with self.subTest(intents=intents):
                review = review_for(fills=[], gross="0", fees="0", no_entry=0)
                source = review["participants"][0]
                source["trade_ledger"].pop("missing_order_results")
                source["trade_ledger"].pop("unresolved")
                if intents is None: source["funnel"].pop("order_intents")
                else: source["funnel"]["order_intents"] = intents
                bot = bot_of(module.analyze_review(seal(review)))
                self.assertEqual(bot["participation"], expected)
        review = review_for(fills=[], gross="0", fees="0", no_entry=0)
        review["participants"][0]["trade_ledger"].pop("unresolved")
        review["participants"][0]["trade_ledger"]["missing_order_results"] = True
        self.assertEqual(bot_of(module.analyze_review(seal(review)))["participation"], "UNKNOWN")

    def test_resolved_zero_fill_from_build_review_can_be_no_entry_without_funding(self):
        from trading_lab.battle_review import build_review
        from tests.test_cycle_diagnostics import state_for
        state = state_for()
        state.update(id="SYNTHETIC-D02-CANCELED", mode="OKX_DEMO", budget_usdt=100,
                     frames=[], history_complete=True)
        request = {"clOrdId": "synthetic-canceled", "reduceOnly": False, "side": "buy"}
        state["bots"][0]["_orders"] = [{"request": request, "result": {
            "clOrdId": request["clOrdId"], "ordId": "synthetic-order", "contracts": "0",
            "state": "canceled", "signed_fee_usdt": "0", "gross_pnl_usdt": "0"}}]
        review = build_review(state)
        ledger = review["participants"][0]["trade_ledger"]
        self.assertIs(ledger["missing_order_results"], False)
        self.assertIs(ledger["unresolved"], False)
        bot = bot_of(module.analyze_review(review))
        self.assertEqual(bot["participation"], "NO_ENTRY")
        self.assertEqual(scenario(bot, 5)["additional_cost_usdt"], "0")
        self.assertIsNone(bot["base_trading_usdt"]["signed_funding_usdt"])
        self.assertIsNone(scenario(bot, 5)["settled_eligible_net_after_overlay_usdt"])

    def test_filled_with_unresolved_or_missing_order_stays_filled_but_incomplete(self):
        for flag in ("missing_order_results", "unresolved"):
            with self.subTest(flag=flag):
                review = review_for()
                review["participants"][0]["trade_ledger"][flag] = True
                bot = bot_of(module.analyze_review(seal(review)))
                self.assertEqual(bot["participation"], "FILLED")
                self.assertEqual(bot["status"], "INCOMPLETE_DIAGNOSTIC_ONLY")
                self.assertIsNone(scenario(bot, 5)["settled_eligible_net_after_overlay_usdt"])

    def test_no_entry_participation_does_not_require_known_funding(self):
        review = review_for(fills=[], gross="0", fees="0", funding=None, no_entry=0)
        bot = bot_of(module.analyze_review(review))
        self.assertEqual(bot["participation"], "NO_ENTRY")
        self.assertEqual(scenario(bot, 5)["additional_cost_usdt"], "0")
        self.assertIsNone(bot["base_trading_usdt"]["signed_funding_usdt"])
        self.assertIsNone(scenario(bot, 5)["settled_eligible_net_after_overlay_usdt"])

    def test_late_starting_event_prefix_is_not_complete_history(self):
        review = review_for()
        for event in review["events"]:
            event["seq"] += 10
        for row in review["participants"][0]["trade_ledger"]["rows"]:
            row["seq"] += 10
        bot = bot_of(module.analyze_review(seal(review)))
        self.assertIn("history_incomplete", bot["issues"])
        self.assertIsNone(scenario(bot, 0)["settled_eligible_net_after_overlay_usdt"])

    def test_unknown_jev_usd_stays_separate_and_does_not_become_zero(self):
        review = review_for()
        review["participants"][0]["costs"]["jev"].update(known_cost_usd=None, complete=False)
        bot = bot_of(module.analyze_review(seal(review)))
        self.assertIsNone(bot["jev_usd"]["known_cost_usd"])
        self.assertFalse(bot["jev_usd"]["included_in_trading_usdt"])
        self.assertEqual(Decimal(scenario(bot, 0)["recorded_net_after_overlay_usdt"]), Decimal("9.895"))
        self.assertIsNone(scenario(bot, 0)["settled_eligible_net_after_overlay_usdt"])

    def test_nonzero_net_with_empty_ledger_is_diagnostic_not_valid_no_trade_profit(self):
        review = review_for(fills=[], gross="5", fees="0", no_entry=0)
        bot = bot_of(module.analyze_review(review))
        self.assertIn("nonzero_trading_costs_without_fills", bot["issues"])
        self.assertEqual(Decimal(scenario(bot, 0)["recorded_net_after_overlay_usdt"]), Decimal(5))
        self.assertIsNone(scenario(bot, 0)["settled_eligible_net_after_overlay_usdt"])

    def test_missing_funding_and_price_are_not_zero(self):
        fills = [("entry", "order-1", "1", None, "0", "1"), ("close", "order-2", "1", "110", "1", "0")]
        bot = bot_of(module.analyze_review(review_for(fills=fills, funding=None, complete=False)))
        self.assertIsNone(bot["base_trading_usdt"]["signed_funding_usdt"])
        self.assertIsNone(scenario(bot, 0)["recorded_net_after_overlay_usdt"])
        self.assertIsNone(scenario(bot, 2)["additional_cost_usdt"])
        self.assertEqual(Decimal(bot["known_fill_notional_subtotal_usdt"]), Decimal(110))

    def test_funding_rows_and_jev_are_never_double_charged(self):
        review = review_for(funding="-.02")
        review["events"].append({"seq": 3, "bot_id": "synthetic-author", "code": "model_funding", "data": {}})
        review["participants"][0]["trade_ledger"]["rows"].append({"seq": 3, "role": "funding", "signed_funding_usdt": "-.02", "source": "paper_model"})
        bot = bot_of(module.analyze_review(seal(review)))
        self.assertEqual(bot["funding_observations"], 1)
        self.assertEqual(Decimal(bot["full_recorded_fill_notional_usdt"]), Decimal(210))
        self.assertEqual(Decimal(scenario(bot, 0)["recorded_net_after_overlay_usdt"]), Decimal("9.875"))

    def test_decimal_overlay_is_independent_of_callers_context(self):
        quantity, price = "0.12345678901234567890123456789", "10.98765432109876543210987654321"
        with localcontext() as ctx:
            ctx.prec = 160
            expected = Decimal(quantity) * Decimal(price) * 2 * Decimal(5) / 10000
        fills = [("entry", "order-1", quantity, price, "0", quantity),
                 ("close", "order-2", quantity, price, quantity, "0")]
        review = review_for(fills=fills, gross="0", fees="0")
        with localcontext() as ctx:
            ctx.prec = 6
            bot = bot_of(module.analyze_review(review))
            self.assertEqual(ctx.prec, 6)
        self.assertEqual(Decimal(scenario(bot, 5)["additional_cost_usdt"]), expected)

    def test_integrity_tamper_is_rejected(self):
        review = review_for()
        review["participants"][0]["costs"]["net_usdt"] = "999"
        with self.assertRaises(module.SensitivityError): module.analyze_review(review)

    def test_extreme_allowed_exponents_preserve_small_notional_addends(self):
        fills = [("entry", "order-1", "1e40", "1e40", "0", "1e40"),
                 ("close", "order-2", "1e40", "1e40", "1e40", "0"),
                 ("entry", "order-3", "1e-40", "1e-40", "0", "1e-40"),
                 ("close", "order-4", "1e-40", "1e-40", "1e-40", "0")]
        bot = bot_of(module.analyze_review(review_for(fills=fills, gross="0", fees="0")))
        with localcontext() as ctx:
            ctx.prec = 512
            expected = Decimal("2e80") + Decimal("2e-80")
            expected_extra = expected * Decimal(5) / Decimal(10000)
        self.assertEqual(Decimal(bot["full_recorded_fill_notional_usdt"]), expected)
        self.assertEqual(Decimal(scenario(bot, 5)["additional_cost_usdt"]), expected_extra)

    def test_cost_bridge_verification_uses_own_decimal_context(self):
        review = review_for(gross="123456789.123456789", fees="-.123456789")
        with localcontext() as ctx:
            ctx.prec = 6
            bot = bot_of(module.analyze_review(review))
            self.assertEqual(ctx.prec, 6)
        self.assertEqual(Decimal(bot["base_trading_usdt"]["net_usdt"]), Decimal("123456789"))

    def test_untrusted_exponents_and_nested_shapes_fail_before_verifier_expansion(self):
        review = review_for()
        review["participants"][0]["costs"]["gross_pnl_usdt"] = "1e1000000000"
        with patch.object(module, "verify_bundle", side_effect=AssertionError("unsafe verifier call")):
            with self.assertRaisesRegex(module.SensitivityError, "invalid_decimal"):
                module.analyze_review(seal(review))
        review = review_for()
        review["events"][0] = None
        with self.assertRaisesRegex(module.SensitivityError, "review_verification_failed"):
            module.analyze_review(seal(review))

    def test_hash_valid_malformed_financial_rows_are_rejected(self):
        for mutation in ("bool", "nan", "negative", "duplicate", "owner", "reversal", "source", "zero", "entry_bool", "intent_bool", "resolution_string"):
            with self.subTest(mutation=mutation):
                review = review_for()
                row = review["participants"][0]["trade_ledger"]["rows"][0]
                if mutation == "bool": row["quantity"] = True
                elif mutation == "nan": row["price_usdt"] = "NaN"
                elif mutation == "negative": row["quantity"] = "-1"
                elif mutation == "duplicate": review["participants"][0]["trade_ledger"]["rows"].append(deepcopy(row))
                elif mutation == "owner": review["events"][0]["bot_id"] = "other-author"
                elif mutation == "reversal": row["position_after"] = "-2"
                elif mutation == "source": review["participants"][0]["costs"]["source"] = "actual_bills_and_cash_reconciled"
                elif mutation == "zero": row["price_usdt"] = "0"
                elif mutation == "entry_bool": review["participants"][0]["funnel"]["entry_filled_orders"] = False
                elif mutation == "intent_bool": review["participants"][0]["funnel"]["order_intents"] = False
                else: review["participants"][0]["trade_ledger"]["unresolved"] = "false"
                with self.assertRaises(module.SensitivityError): module.analyze_review(seal(review))

    def test_missing_inventory_stays_diagnostic_and_unknown_rows_not_dropped(self):
        review = review_for(complete=False)
        review["participants"][0]["trade_ledger"]["rows"][0]["quantity"] = None
        review["participants"][0]["trade_ledger"]["inventory_known"] = False
        bot = bot_of(module.analyze_review(seal(review)))
        self.assertEqual(bot["unknown_notional_event_seqs"], [1])
        self.assertIsNone(scenario(bot, 5)["recorded_net_after_overlay_usdt"])
        self.assertEqual(bot["status"], "INCOMPLETE_DIAGNOSTIC_ONLY")

    def test_existing_real_projection_contract_is_reused_without_unit_guess(self):
        from tests.test_battle_review import ReviewTests
        from trading_lab.battle_review import build_review
        review = build_review(ReviewTests().fixture())  # existing synthetic fixture, no account/API
        self.assertTrue(verify_bundle(review)["verified"])
        report = module.analyze_review(review)
        self.assertEqual(len(report["participants"]), 2)
        astra = report["participants"][1]
        self.assertEqual(Decimal(astra["base_trading_usdt"]["net_usdt"]), Decimal("-.007"))
        self.assertIsNone(scenario(astra, 5)["additional_cost_usdt"])

    def archive(self):
        from trading_lab.arena_engine import new_season
        from trading_lab.arena_strategies import catalogue_defaults
        from trading_lab.battle_export import create_archive
        state = new_season([catalogue_defaults()[0]], 100, 300, 1792965600, "0"*64)
        state.update(status="DONE", incomplete=False, history_complete=True, execution_mode="rules")
        for bot in state["bots"]:
            bot["status"] = "STOPPED"
        stream = create_archive(state)
        try: return stream.read()
        finally: stream.close()

    def test_archive_verifier_is_used_and_same_bytes_are_analyzed(self):
        payload = self.archive()
        report = module.analyze_archive(payload)
        self.assertTrue(report["archive"]["verification"]["verified"])
        self.assertEqual(bot_of(report)["participation"], "NO_ENTRY")
        self.assertFalse(report["dynamic_replay"])

    def test_archive_member_tamper_and_untrusted_zip_are_rejected(self):
        original = self.archive()
        output = BytesIO()
        with zipfile.ZipFile(BytesIO(original)) as source, zipfile.ZipFile(output, "w") as dest:
            for name in source.namelist():
                dest.writestr(name, b"{}" if name == "battle-review.json" else source.read(name))
        for payload in (output.getvalue(), b"not a zip"):
            with self.assertRaises(module.SensitivityError): module.analyze_archive(payload)

    def test_bounded_archive_expansion_and_review_size_refuse_without_truncating(self):
        payload = self.archive()
        with patch.object(module, "MAX_ARCHIVE_BYTES", 10):
            with self.assertRaisesRegex(module.SensitivityError, "archive_expansion_limit"):
                module.analyze_archive(payload)
        with patch.object(module, "MAX_REVIEW_BYTES", 10):
            with self.assertRaisesRegex(module.SensitivityError, "review_size_limit"):
                module.analyze_review(review_for())


if __name__ == "__main__":
    unittest.main()
