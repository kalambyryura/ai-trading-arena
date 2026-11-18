"""Jev research harness: allow-list anonymiser, coord approval, hard cap under concurrency, unknown-billing block,
worst-case reconcile, answer validation, replay. Fake transport only, no key, no paid call."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import jev_research as harness

QUESTIONS = {"trend_strength": {"type": "choice", "instructions": "Judge only the supplied normalised features. If unsure, choose weak.",
                                "criteria": {"strong": "Features agree on a persistent move.", "weak": "Mixed or flat features."}}}
FEATURES = {"return_60m_z": "z", "sma_gap_1d_bps": "bps", "realized_vol_pct": "pct"}
SPEC = harness.validate_spec({"features": FEATURES, "questions": QUESTIONS})
CASE = {"return_60m_z": 3.2, "sma_gap_1d_bps": 140.0, "realized_vol_pct": 81.0}


class FakeJev:
    def __init__(self, cost=.003, fail=False, answers=None, model="typesafe/jev-1.13"):
        self.calls, self.cost, self.fail, self.model = [], cost, fail, model
        self.answers = answers if answers is not None else {"trend_strength": {"choice": "strong", "confidence": .8}}

    def __call__(self, body, key):
        self.calls.append((body, key))
        if self.fail:
            raise TimeoutError("lost response")
        return {"model": self.model, "answers": self.answers, "usage": {"cost": self.cost}}


def approve(root, spec=SPEC):
    flag = root.joinpath(*harness.PAID_FLAG)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text(json.dumps({"approved": {harness.spec_sha256(spec): "test"}}), encoding="utf-8")


def ask(ledger, transport, case=CASE, mode="paid"):
    return harness.ask(ledger, SPEC, case, mode=mode, transport=transport, key_reader=lambda _root: "test-key")


class AnonymisationTests(unittest.TestCase):
    def spec(self, text=None, features=None):
        questions = {"q": {**QUESTIONS["trend_strength"], "instructions": text}} if text else QUESTIONS
        return harness.validate_spec({"features": features or FEATURES, "questions": questions})

    def test_review_bypasses_in_question_text_are_refused(self):
        bypasses = ["About 65000 dollars.", "Trade BTCUSDT only.", "The XBT perpetual.", "Like DOGE did.", "As on Kraken.",
                    "Feels btcish today.", "Before xmas.", "Every Monday.",
                    "Deribit options flow.", "Treat it as біткоїн.", "Treat it as Вitcoin.", "Context of ２０２４.",
                    "In 20​24.", "In 2O24.", "Late October.", "After the ETF approval.", "Opens at 14h30.", "Around 2pm.",
                    "On 3/10.", "Context: the 2024 halving.", "Price on 2026-03-01.", "At 14:30 the session opens.", "Bitcoin only.",
                    "See https://example.org", "The ethusd swap.", "Solana season."]
        for text in bypasses:
            with self.assertRaises(harness.Refused, msg=text):
                self.spec(text)
        clean = "Judge only the supplied normalised features; a mechanical system may wait. If unsure, choose weak."
        self.assertEqual(self.spec(clean)["questions"]["q"]["instructions"], clean)

    def test_review_bypasses_in_features_are_refused(self):
        bad_specs = [{"close": "bps"}, {"day_of_year": "count"}, {"price_hi_k": "count", "price_lo": "bps"}, {"halving_days": "count"},
                     {"btc_return": "bps"}, {"ret2024": "bps"}, {"x": "usd"}, {"Ret": "z"}, {"hour_of_day": "count"},
                     {"day_of_week": "count"}, {"y20_24": "z"}, {"t14_30": "z"}, {"ath_gap_bps": "bps"}, {"mcap_rank": "count"},
                     {"hours_to_open": "count"}, {"months_since": "count"}, {"years_ago": "count"}, {"hod": "count"}, {"wday": "count"},
                     {"ohlc_close": "bps"}, {"close_count": "count"}, {"spot_ref": "bps"}, {"last_trade": "bps"}, {"c": "z"},
                     {"sui_z": "z"}, {"ton_z": "z"}, {"monday_z": "z"},
                     {"x": {"unit": "z"}}, {"x": ["z"]}]
        for features in bad_specs:
            with self.assertRaises(harness.Refused, msg=str(features)):
                self.spec(features=features)
        for case in ({"close": 150.25}, {"undeclared": 1.0}, {"return_60m_z": "up"}, {"return_60m_z": True},
                     {"return_60m_z": float("nan")}, {"return_60m_z": 101.0}, {"return_60m_z": 10 ** 400}, {"realized_vol_pct": 300.0}, {}):
            with self.assertRaises(harness.Refused, msg=str(case)):
                harness.validate_case(case, SPEC["features"])
        self.assertEqual(harness.validate_case(CASE, SPEC["features"]), CASE)
        coarse = harness.validate_case({"return_60m_z": 12.345678901, "sma_gap_1d_bps": 3456.78}, SPEC["features"])
        self.assertEqual(coarse, {"return_60m_z": 12.35, "sma_gap_1d_bps": 3456.8})  # no room for hidden digits
        counts = {"n_touches": "count"}
        self.assertEqual(harness.validate_case({"n_touches": 7.0}, counts), {"n_touches": 7})
        for bad in (3.5, 501):
            with self.assertRaises(harness.Refused):
                harness.validate_case({"n_touches": bad}, counts)

    def test_question_ids_and_choices_pass_the_word_check(self):
        for questions in ({"btc": QUESTIONS["trend_strength"]},
                          {"q": {**QUESTIONS["trend_strength"], "criteria": {"october": "a", "weak": "b"}}},
                          {"q": {**QUESTIONS["trend_strength"], "criteria": {"okxbtcusdt": "a", "weak": "b"}}}):
            with self.assertRaises(harness.Refused, msg=str(questions)):
                harness.validate_spec({"features": FEATURES, "questions": questions})
        opus_names = {f"h{i:02d}": "z" for i in range(1, 25)} | {f"d{i}": "z" for i in range(1, 8)} | {
            "r4z": "z", "r24z": "z", "r72z": "z", "rng72": "pct", "dd30z": "z", "vr24": "ratio", "vlast": "ratio"}
        self.assertEqual(len(self.spec(features=opus_names)["features"]), 38)  # the J0 vocabulary fits the allow-list

    def test_oversized_bodies_are_refused(self):
        wide = harness.validate_spec({"features": {f"f{i:02d}_z": "z" for i in range(40)},
                                      "questions": {k: {"type": "choice", "instructions": "w" * 590, "criteria": {"a": "x" * 590, "b": "y" * 590}}
                                                    for k in ("one", "two", "three", "four")}})
        with self.assertRaises(harness.Refused):
            harness.request_body(wide, {f"f{i:02d}_z": -99.123456 for i in range(40)})

    def test_dry_run_touches_no_ledger_and_no_network(self):
        fake = FakeJev()
        record = harness.ask(None, SPEC, CASE, mode="dry_run", transport=fake)
        self.assertEqual((record["mode"], fake.calls, record["spec_sha256"]), ("dry_run", [], harness.spec_sha256(SPEC)))
        self.assertEqual(record["body"]["state"]["research_case"], CASE)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        approve(self.root)
        (self.root / ".team-role").write_text("opus\n", encoding="utf-8")  # paid calls run only from the author's worktree
        self.patch = mock.patch.object(harness, "ROOT", self.root)  # the flag always comes from the harness's own checkout
        self.patch.start()
        self.ledger = harness.Ledger(self.root / "opus" / "l.sqlite3")

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def paid(self, transport, case=CASE, ledger=None):
        return ask(ledger or self.ledger, transport, case)

    def test_paid_calls_need_the_coord_approval_of_the_exact_spec(self):
        flag = self.root.joinpath(*harness.PAID_FLAG)
        for content in ("", json.dumps({"approved": {harness.spec_sha256(SPEC): False}}), json.dumps({"approved": {harness.spec_sha256(SPEC): ""}}),
                        json.dumps({"approved": {"0" * 64: "other"}})):
            flag.write_text(content, encoding="utf-8")  # bare flag, revoked label, other spec: no approval
            with self.assertRaises(harness.Refused, msg=content):
                self.paid(FakeJev())
        approve(self.root)
        self.assertEqual(self.paid(FakeJev())["billing"], "accounted")

    def test_paid_call_is_accounted_and_a_repeat_is_replayed_free(self):
        fake = FakeJev(cost=.003)
        first = self.paid(fake)
        self.assertEqual((first["mode"], first["billing"], first["cost_usd"], first["decision"]), ("paid", "accounted", .003, "ANSWERED"))
        again = self.paid(fake)
        self.assertEqual((again["mode"], again["cost_usd"], len(fake.calls)), ("replay", 0.0, 1))
        self.assertEqual(again["answers"], first["answers"])
        with self.assertRaises(harness.Refused):
            ask(self.ledger, fake, case={**CASE, "return_60m_z": 1.0}, mode="replay")
        self.assertEqual(self.ledger.status()["spent_usd"], .003)

    def test_invalid_answers_are_spent_hold_and_never_replayed(self):
        for k, bad in enumerate((FakeJev(answers={"trend_strength": {"choice": "maybe", "confidence": .9}}),
                                 FakeJev(answers={"trend_strength": {"choice": "strong", "confidence": 1.5}}),
                                 FakeJev(answers={}), FakeJev(model="someone/else"))):
            case = {**CASE, "return_60m_z": float(k)}
            record = self.paid(bad, case=case)
            self.assertEqual((record["billing"], record["answers"], record["decision"]), ("invalid", None, "HOLD"))
            with self.assertRaises(harness.Refused):
                ask(self.ledger, FakeJev(), case=case, mode="replay")
        self.assertAlmostEqual(self.ledger.status()["spent_usd"], .012)
        self.assertEqual(self.ledger.status()["invalid_answers"], 4)

    def test_unknown_billing_blocks_every_paid_call_until_coord_books_the_reserve(self):
        lost = self.paid(FakeJev(fail=True))
        self.assertEqual((lost["billing"], lost["error"], lost["decision"]), ("billing_unknown", "TimeoutError", "HOLD"))
        with self.assertRaises(harness.Refused):
            self.paid(FakeJev(), case={**CASE, "return_60m_z": 2.0})
        with self.assertRaises(harness.Refused):
            self.ledger.reconcile(lost["call"], 0.0)  # below the reserve
        self.assertEqual(self.ledger.reconcile(lost["call"]), .01)  # D107 Q2: the worst case is booked
        self.assertEqual(self.ledger.status()["spent_usd"], .01)
        self.assertEqual(self.paid(FakeJev(), case={**CASE, "return_60m_z": 2.0})["billing"], "accounted")

    def test_hard_cap_counts_the_reserve_before_the_call(self):
        ledger = harness.Ledger(self.root / "opus" / "cap.sqlite3", cap=.03, reserve=.01)
        fake = FakeJev(cost=.009)
        for z in (1.0, 2.0, 3.0):
            self.assertEqual(self.paid(fake, case={**CASE, "return_60m_z": z}, ledger=ledger)["billing"], "accounted")
        with self.assertRaises(harness.Refused):
            self.paid(fake, case={**CASE, "return_60m_z": 4.0}, ledger=ledger)
        self.assertEqual(len(fake.calls), 3)
        self.assertAlmostEqual(ledger.status()["spent_usd"], .027)
        with self.assertRaises(harness.Refused):
            harness.Ledger(self.root / "opus" / "cap.sqlite3", cap=1.0)  # the cap lives in the ledger, not in the caller

    def test_parallel_reservations_cannot_pass_the_cap(self):
        path = self.root / "race.sqlite3"
        harness.Ledger(path, cap=.05, reserve=.01)
        gate = threading.Barrier(8)

        def one(i):
            ledger = harness.Ledger(path, cap=.05, reserve=.01)
            gate.wait()
            try:
                return ledger.reserve(f"body{i}")
            except harness.Refused:
                return None
        with ThreadPoolExecutor(8) as pool:
            tokens = [t for t in pool.map(one, range(8)) if t]
        self.assertEqual(len(tokens), 1)  # an in-flight reservation blocks the others (B1)
        rows = harness.Ledger(path, cap=.05, reserve=.01).rows()
        self.assertEqual([r[2] for r in rows], ["reserved"])

    def test_a_call_above_its_reserve_stops_further_calls_and_books_its_reported_cost(self):
        over = self.paid(FakeJev(cost=.05))
        self.assertEqual(over["billing"], "over_reserve")
        self.assertEqual(self.ledger.status()["spent_usd"], .05)  # the reported cost counts at once
        with self.assertRaises(harness.Refused):
            self.paid(FakeJev(), case={**CASE, "return_60m_z": 2.0})
        with self.assertRaises(harness.Refused):
            self.ledger.reconcile(over["call"], .02)  # below the reported cost
        self.ledger.reconcile(over["call"])
        self.assertEqual(self.ledger.status()["spent_usd"], .05)

    def test_costs_finer_than_a_millionth_do_not_block_the_next_call(self):
        # coord 2026-10-28: Opus's first paid call cost 4.41e-05; the rounded ledger sum looked below the journal
        for k, cost in enumerate((4.41e-05, 1.2345678e-05, 3.3e-07, 9.87654321e-06)):
            self.assertEqual(self.paid(FakeJev(cost=cost), case={**CASE, "return_60m_z": float(k)})["billing"], "accounted")
        self.assertAlmostEqual(self.ledger.spent(self.ledger.rows()), 4.41e-05 + 1.2345678e-05 + 3.3e-07 + 9.87654321e-06, places=15)
        self.assertEqual(self.ledger.status()["spent_usd"], round(4.41e-05 + 1.2345678e-05 + 3.3e-07 + 9.87654321e-06, 6))

    def test_the_same_fine_cost_twice_and_a_repeating_decimal_cost_pass(self):
        costs = (4.41e-05, 4.41e-05, 3.3333337e-05)  # coord's probe: the second 4.41e-05 call was refused before ff0f681
        for k, cost in enumerate(costs):
            self.assertEqual(self.paid(FakeJev(cost=cost), case={**CASE, "return_60m_z": 10.0 + k})["billing"], "accounted")
        self.assertAlmostEqual(self.ledger.journal_spent(), sum(costs), places=15)

    def test_paid_calls_need_the_author_worktree_and_its_own_ledger(self):
        (self.root / ".team-role").unlink()
        with self.assertRaises(harness.Refused):
            self.paid(FakeJev())  # coord, platform or the main checkout: no paid call on an author's behalf
        (self.root / ".team-role").write_text("opus\n", encoding="utf-8")
        with self.assertRaises(harness.Refused):
            self.paid(FakeJev(), ledger=harness.Ledger(self.root / "sol" / "l.sqlite3"))

    def test_reconcile_books_the_journal_cost_when_the_ledger_lost_it(self):
        token = self.ledger.reserve("body")
        self.ledger.write_journal({"call": token, "event": "over_reserve", "reported_cost_usd": .07})  # the UPDATE never ran
        self.assertEqual(self.ledger.reconcile(token), .07)
        with self.assertRaises(harness.Refused):
            self.ledger.reconcile(token, .01)
        self.assertAlmostEqual(self.ledger.journal_spent(), .07)

    def test_a_ledger_edited_below_its_journal_refuses_new_calls(self):
        self.paid(FakeJev(cost=.008))
        with closing(sqlite3.connect(self.ledger.path)) as db, db:
            db.execute("UPDATE calls SET cost_usd=0, status='reconciled'")
        with self.assertRaises(harness.Refused):
            self.paid(FakeJev(), case={**CASE, "return_60m_z": 2.0})

    def test_a_deleted_ledger_does_not_reset_the_cap(self):
        self.paid(FakeJev())
        self.ledger.path.unlink()
        with self.assertRaises(harness.Refused):
            harness.Ledger(self.ledger.path)


class RoleTests(unittest.TestCase):
    def test_the_working_directory_names_the_author_even_for_the_main_checkout_copy(self):
        with tempfile.TemporaryDirectory() as main, tempfile.TemporaryDirectory() as worktree:
            (Path(worktree) / ".team-role").write_text("sol\n", encoding="utf-8")
            here = Path.cwd()
            try:
                os.chdir(worktree)
                with mock.patch.object(harness, "ROOT", Path(main)):
                    self.assertEqual(harness.invoker_role(), "sol")
                    with self.assertRaises(harness.Refused):
                        harness.ledger_path("opus")
                    harness.ledger_path("sol")
                    with redirect_stdout(io.StringIO()) as out:
                        self.assertEqual(harness.main(["reconcile", "--author", "sol", "--call", "x"]), 2)
                    self.assertIn("coord-only", out.getvalue())
            finally:
                os.chdir(here)

    def test_author_worktrees_spend_only_their_own_ledger_and_never_reconcile(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".team-role").write_text("sol\n", encoding="utf-8")
            with mock.patch.object(harness, "ROOT", root):
                self.assertEqual(harness.ledger_path("sol").parts[-4:], ("runtime", "jev_research", "sol", "ledger.sqlite3"))
                with self.assertRaises(harness.Refused):
                    harness.ledger_path("opus")
                with self.assertRaises(harness.Refused):
                    harness.ledger_path("someone")
                with redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(harness.main(["reconcile", "--author", "sol", "--call", "x"]), 2)
            self.assertIn("coord-only", out.getvalue())

    def test_the_cli_requires_an_explicit_mode_and_refuses_bad_inputs_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            spec, cases = root / "spec.json", root / "cases.jsonl"
            spec.write_text(json.dumps({"features": FEATURES, "questions": QUESTIONS}), encoding="utf-8")
            cases.write_text(json.dumps(CASE) + "\n", encoding="utf-8")
            base = ["ask", "--author", "opus", "--spec", str(spec), "--cases", str(cases), "--out", str(root / "a.jsonl")]
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                harness.main(base)  # no mode: argparse refuses, nothing is paid by default
            with mock.patch.object(harness, "ROOT", root), redirect_stdout(io.StringIO()) as out:
                self.assertEqual(harness.main(base + ["--dry-run"]), 0)
            self.assertEqual(json.loads(out.getvalue())["spec_sha256"], harness.spec_sha256(SPEC))
            self.assertFalse((root / "runtime" / "jev_research").exists())
            for bad_cases in ("{broken\n", "[" * 100000 + "\n", '{"return_60m_z": 1, "return_60m_z": 2}\n'):
                cases.write_text(bad_cases, encoding="utf-8")
                with mock.patch.object(harness, "ROOT", root), redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(harness.main(base + ["--dry-run"]), 2)
                self.assertIn("refused", out.getvalue())
            cases.write_text(json.dumps(CASE) + "\n", encoding="utf-8")
            with mock.patch.object(harness, "ROOT", root), redirect_stdout(io.StringIO()):
                self.assertEqual(harness.main(["status", "--author", "opus"]), 0)
                self.assertEqual(harness.main(base + ["--replay"]), 2)
            self.assertFalse((root / "runtime" / "jev_research").exists())  # status and replay create no ledger

    def test_tools_come_first_so_nothing_in_the_worktree_root_shadows_the_lease_module(self):
        self.assertEqual(Path(harness.research_limits.__file__).resolve(), (ROOT / "tools" / "research_limits.py").resolve())

    def test_an_invalid_answer_stops_the_paid_run(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            approve(root)
            (root / ".team-role").write_text("opus\n", encoding="utf-8")
            spec, cases = root / "spec.json", root / "cases.jsonl"
            spec.write_text(json.dumps({"features": FEATURES, "questions": QUESTIONS}), encoding="utf-8")
            cases.write_text("".join(json.dumps({**CASE, "return_60m_z": float(z)}) + "\n" for z in range(3)), encoding="utf-8")
            fake = FakeJev(answers={"trend_strength": {"choice": "maybe", "confidence": .9}})
            from trading_lab import jev
            with mock.patch.object(harness, "ROOT", root), mock.patch.object(jev, "send_once", fake), \
                    mock.patch.object(jev, "read_key", lambda _root: "test-key"), redirect_stdout(io.StringIO()):
                self.assertEqual(harness.main(["ask", "--author", "opus", "--spec", str(spec), "--cases", str(cases),
                                               "--out", str(root / "a.jsonl"), "--paid"]), 0)
            self.assertEqual(len(fake.calls), 1)


if __name__ == "__main__":
    unittest.main()
