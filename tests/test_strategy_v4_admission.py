"""Schema 4 admission (R1 full F3, R2): BTC and ETH taker or post-only recipes enter the workshop and the engines; SOL,
unclean passports and Jev execution are refused; the frozen payload re-validates to the same hashes. Temp folders,
synthetic tapes and fake feeds only: no network, key, Jev or order."""
from copy import deepcopy
import math
from pathlib import Path
import random
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trading_lab import arena, strategy_training, strategy_v4 as v4, strategy_v4_book as fb
from trading_lab.arena_strategies import ArenaError, fingerprint, validate_strategy
from trading_lab.strategy_contract import payload_of

PASSPORT = {"name": "Sonnet", "kind": "model", "model": "claude-opus-5-5", "submitted_at": "2026-10-29",
            "prompt": "Автор стратегії (тест допуску схеми 4).", "attempts": 1, "edited_by": ""}


def payload(**changes):
    base = {"strategy_schema_version": 4, "name": "v4 admission", "author": dict(PASSPORT), "description": "Тест допуску.",
            "original_text": "Купівля після падіння за 5 хв на 12 bps.", "confirmed": True, "instrument": "BTC-USDT-SWAP",
            "rules": {"side": "long", "entry": {"when": {"feature": {"name": "return_bps", "tf": "1m", "n": 5}, "op": "lte", "value": -12}},
                      "order": {"type": "taker"}, "stop_loss_bps": 20, "take_profit_bps": 40, "max_hold_seconds": 300,
                      "cooldown_seconds": 60, "size_pct": 11}}
    for key, value in changes.items():
        if key in base["rules"]:
            base["rules"][key] = value
        else:
            base[key] = value
    return base


def no_feed(*_args, **_kwargs):
    raise AssertionError("no market feed in this test")


class AdmissionTests(unittest.TestCase):
    def test_a_btc_taker_recipe_becomes_an_engine_recipe_with_both_hashes(self):
        record = validate_strategy(payload(), 3)
        clean = v4.validate_payload(payload())
        self.assertEqual((record["strategy_schema_version"], record["version"], record["status"]), (4, 3, "ready"))
        self.assertEqual(record["v4_recipe_sha256"], clean["recipe_sha256"])  # the team evaluator's ledger hash
        self.assertEqual(record["recipe_sha256"], fingerprint(payload_of(record)))  # the engines' invariant
        self.assertEqual((record["rules"]["max_entries"], record["rules"]["max_spread_bps"]), (1, 2))
        again = validate_strategy(payload_of(record), 3)
        self.assertEqual((again["recipe_sha256"], again["v4_recipe_sha256"], again["rules"]), (record["recipe_sha256"], record["v4_recipe_sha256"], record["rules"]))

    def test_unclean_and_cheap_targets_are_refused_and_post_only_and_sol_enter(self):
        maker = {"type": "post_only", "offset": {"bps": 5}, "timeout_seconds": 600}
        self.assertEqual(validate_strategy(payload(order=maker))["rules"]["order"], maker)  # R2 (b): D096 post-only
        self.assertEqual(validate_strategy(payload(instrument="SOL-USDT-SWAP"))["instrument"], "SOL-USDT-SWAP")  # R2.1.13 (D152)
        for bad in (payload(take_profit_bps=16),
                    payload(name=" v4 admission"), payload(author={**PASSPORT, "name": "Sonnet "}),
                    payload(author={k: v for k, v in PASSPORT.items() if k != "prompt"}), payload(confirmed=False),
                    payload(size_pct=99)):
            with self.assertRaises(ArenaError):
                validate_strategy(bad)

    def test_workshop_train_freeze_promote_and_arena_import(self):
        from trading_lab.workshop import Workshop
        with tempfile.TemporaryDirectory() as folder:
            board = arena.Arena(folder, feed_factory=no_feed, background=False)
            try:
                shop = Workshop(folder, board)
                added = shop.add_version(shop.data["generation"], payload())
                trial = shop.train(shop.data["generation"], added["id"], strategy_training.SYNTHETIC["id"], 100)
                self.assertEqual(trial["status"], "COMPLETE")
                detail = shop.trial(trial["id"])
                self.assertTrue(any("Схема 4" in line for line in detail["feedback"]))
                self.assertEqual(detail["strategy"], payload_of(shop.version(added["id"])["strategy"]))
                shop.freeze(shop.data["generation"], added["id"])
                promoted = shop.promote(shop.data["generation"], added["id"])
                entry = next(s for s in board.catalogue() if s["id"] == promoted["id"])
                self.assertEqual((entry["strategy_schema_version"], entry["v4_recipe_sha256"]),
                                 (4, v4.validate_payload(payload())["recipe_sha256"]))
                with self.assertRaises(ArenaError):  # Jev never gates schema-4 entries
                    board.start(board.data["generation"], [entry["id"]], 100, 300, "shared_jev")
            finally:
                board.close()

    def test_synthetic_training_gives_every_snapshot_a_book(self):
        record = validate_strategy(payload())
        tape = strategy_training.synthetic_tape("up", 420)
        booked = strategy_training.attach_tape_book(record, tape)
        self.assertNotIn("v4_features", tape[0])  # the market tape itself is never changed
        values = [s["v4_features"]["values"]["return_bps@1m:5"] for s in booked]
        self.assertTrue(all(v is not None for v in values))  # 31 minutes of synthetic history cover a 5-minute window
        self.assertTrue(all(5 <= s["at"] - s["v4_features"]["bar_close_ms"] / 1000 < 65 for s in booked))


class RecordedBookTests(unittest.TestCase):
    def test_a_book_from_the_candles_of_recorded_snapshots_equals_the_reference(self):
        rng, price, rows = random.Random(8), 80000.0, []
        start = 1_800_000_000_000
        for k in range(120):
            o = price
            price *= math.exp(rng.gauss(0, .001))
            rows.append([str(start + k * 60_000), repr(o), repr(max(o, price) * 1.0002), repr(min(o, price) * .9998), repr(price), "7", "0", "0", "1"])
        tape = []
        for second in range(start // 1000 + 1860, start // 1000 + 7000, 5):
            done = [r for r in rows if int(r[0]) + 60_000 <= second * 1000][-30:]
            live = [str(second // 60 * 60_000), "1", "1", "1", "1", "1", "0", "0", "0"]
            tape.append({"at": second, "raw": {"candles": [live] + list(reversed(done))}})
        spec = {"name": "rms_bar_bps", "tf": "5m", "n": 6}
        booked = fb.attach_raw(deepcopy(tape), fb.FeatureBook([spec]))
        held = min(int(r[0]) for r in tape[0]["raw"]["candles"] if r[8] == "1")  # the tape's history starts here
        minutes = [(int(r[0]), *(float(x) for x in r[1:6])) for r in rows if int(r[0]) >= held]
        checked = 0
        for snapshot in booked[::37]:
            close = snapshot["v4_features"]["bar_close_ms"]
            expected = v4.feature_value(spec, [m for m in minutes if m[0] + 60_000 <= close])
            self.assertEqual(snapshot["v4_features"]["values"]["rms_bar_bps@5m:6"], expected)
            checked += expected is not None
        self.assertGreater(checked, 10)



class ReviewFixTests(unittest.TestCase):
    """Findings of the R1 full review: engine rules covered by the identity check, filled minutes, shadow labels."""

    def test_changed_engine_rules_fail_the_identity_check_everywhere(self):
        from trading_lab.strategy_contract import recipe_intact
        record = validate_strategy(payload())
        self.assertTrue(recipe_intact(record))
        for change in ({"side": "short"}, {"stop_loss_bps": 900}, {"max_spread_bps": 20}):
            tampered = deepcopy(record)
            tampered["rules"].update(change)
            self.assertEqual(fingerprint(payload_of(tampered)), record["recipe_sha256"])  # the payload hash alone misses it
            self.assertFalse(recipe_intact(tampered), change)
            with self.assertRaises(ArenaError):
                strategy_training.run_trial(tampered)
        with tempfile.TemporaryDirectory() as folder:
            board = arena.Arena(folder, feed_factory=no_feed, background=False)
            try:
                tampered = deepcopy(record)
                tampered["rules"]["stop_loss_bps"] = 900
                with self.assertRaises(ArenaError):
                    board.import_frozen_strategy(tampered, {"version_id": record["id"]})
            finally:
                board.close()

    def test_the_evaluator_never_decides_on_a_minute_it_filled(self):
        import numpy as np
        sys.path.insert(0, str(ROOT / "tools"))
        sys.path.insert(0, str(ROOT / "tests"))
        import team_eval_v4
        from test_team_eval_v4 import ETH, JUMP, flat, strategy as v4_strategy
        recipe = v4_strategy(order={"type": "taker"})
        series = flat()
        trades, _, _ = team_eval_v4.simulate(recipe, series, ETH, [], int(series["ts"][0]), int(series["ts"][-1]) + 60_000)
        self.assertEqual(len(trades), 1)
        series["filled"] = np.zeros(len(series["ts"]), dtype=bool)
        series["filled"][10] = True  # the jump bar was a one-minute hole filled flat: Live never sees it
        trades, counters, _ = team_eval_v4.simulate(recipe, series, ETH, [], int(series["ts"][0]), int(series["ts"][-1]) + 60_000)
        self.assertEqual((len(trades), counters["decisions"]), (0, 0))

    def test_schema_4_candidates_are_labelled_in_the_shadow_payload(self):
        from trading_lab import arena_jev
        record = validate_strategy(payload())
        candidate = {"id": "x", "recipe_sha256": record["recipe_sha256"], "features": {}, "rules": record["rules"], "checks": [],
                     "side": "long", "kind": "entry", "position_btc": 0, "local_checks_passed": True}
        packet = arena_jev.make_payload([candidate], "PAPER")
        self.assertIn("v4_rule_format", packet["state"])


if __name__ == "__main__":
    unittest.main()
