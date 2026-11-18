"""Ordinary RULES_ONLY acceptance on temp roots/fake exchange; no credentials/network."""
from copy import deepcopy
import contextlib
from decimal import Decimal
import hashlib
import io
import json
from unittest.mock import Mock
import unittest

import test_exchange_lab as fixture
from tools import jev_gate_probe as probe
from trading_lab import exchange_execution as policy
from trading_lab.arena_jev import make_payload
from trading_lab.arena_strategies import ArenaError, fingerprint
from trading_lab.battle_review import build_review, verify_bundle
from trading_lab.cycle_probe import template
from trading_lab.gate_shadow import cases, comparison
from trading_lab.match_journal import restore_events


class RulesOnlyTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.ExchangeTests(methodName="runTest")
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.fx.lab.jev_factory = Mock(side_effect=AssertionError("No Jev construction for RULES_ONLY"))

    def start(self, live=False, count=1):
        fx, lab = self.fx, self.fx.lab
        ids = [fx.version]
        accounts = ["demo"]
        if live:
            # Free qualification/freeze/promotion in a disposable root only.
            fx.workshop.train(fx.workshop.data["generation"], fx.version, "synthetic-suite-v2", 100)
            fx.workshop.freeze(fx.workshop.data["generation"], fx.version)
            fx.workshop.promote(fx.workshop.data["generation"], fx.version)
            if count == 2:
                from trading_lab.arena_strategies import catalogue_defaults
                from trading_lab.strategy_contract import payload_of
                second = fx.workshop.add_version(fx.workshop.data["generation"], payload_of(catalogue_defaults()[1]))["id"]
                fx.workshop.train(fx.workshop.data["generation"], second, "synthetic-suite-v2", 100)
                fx.workshop.freeze(fx.workshop.data["generation"], second)
                fx.workshop.promote(fx.workshop.data["generation"], second)
                ids.append(second)
            accounts = ["arena-a", "arena-b"][:count]
        result = lab.start(lab.data["generation"], "arena_live" if live else "workshop_demo", ids, accounts,
                           100, 300, execution_mode=policy.RULES_ONLY)
        state = lab.load(result["id"])
        state.update(fixture_only=True, evidence_kind="SYNTHETIC")
        lab.prepare(state, lab.chosen_profiles(accounts), False)
        self.state, self.bot, self.fake = state, state["bots"][0], fx.fakes[accounts[0]]
        return state

    def full(self):
        state = deepcopy(self.fx.lab.load(self.state["id"]))
        restore_events(self.fx.lab.path / (state["id"]+".events.jsonl"), state, full=True)
        return state

    def test_live_two_recipes_no_model_no_paid_ledger_and_lifecycle(self):
        before = {k: v.summary("") for k, v in self.fx.lab.ledgers.items()}
        state = self.start(live=True, count=2)
        self.fx.lab.step(state)
        self.fx.lab.jev_factory.assert_not_called()
        self.assertIsNone(self.fx.lab.resources[state["id"]]["gate"])
        self.assertEqual(before, {k: v.summary("") for k, v in self.fx.lab.ledgers.items()})
        self.assertTrue(all(b["ai_requests"] == 0 and b["ai_cost_usd"] == 0 for b in state["bots"]))
        self.assertEqual(self.fx.lab.status()["rules_only_check"], policy.capability())
        self.assertEqual(self.fx.lab.status()["history"][0]["execution_mode"], "rules_only")
        for bot in self.fx.lab.history(state["id"])["lifecycle"]["bots"]:
            jev = next(r for r in bot["rows"] if r["id"] == "jev_entry")
            self.assertFalse(jev["required"])
            self.assertEqual(jev["status"], "not_applicable")

    def test_signal_records_exact_unsent_gate_bytes_and_owned_fill(self):
        state = self.start()
        resource = self.fx.lab.resources[state["id"]]
        captured = []
        original = policy.decision
        def capture(s, candidates):
            captured.extend(deepcopy(candidates))
            return original(s, candidates)
        from unittest.mock import patch
        with patch.object(policy, "decision", capture):
            self.fx.lab.step(state)
        self.assertGreater(self.bot["position_btc"], 0)
        self.assertTrue(self.fake.orders)
        self.assertTrue(any(a["state"] == "live" for a in self.fake.algos.values()))
        full = self.full()
        event = next(e for e in full["events"] if e["code"] == "jev_shadow_payload")
        data = event["data"]
        wire = json.dumps(make_payload(captured, "OKX_DEMO"), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        self.assertEqual(data["payload_wire_json"], wire)
        self.assertEqual(data["payload_sha256"], hashlib.sha256(wire.encode()).hexdigest())
        self.assertFalse(data["sent"])
        self.assertFalse(data["ai_used"])
        self.assertTrue(any(e["code"] == "rules_only_review" for e in full["events"]))
        self.assertFalse(any(e["code"] in {"jev_request", "jev_decision"} for e in full["events"]))
        self.assertIsNone(resource["gate"])

    def test_final_fresh_check_stop_and_cutoff_still_veto(self):
        self.start()
        feed = self.fx.lab.resources[self.state["id"]]["feed"]
        original = feed.snapshot
        calls = 0
        def stale_conditions():
            nonlocal calls
            calls += 1
            snap = original()
            if calls > 1: snap["features"]["spread_bps"] = 100
            return snap
        feed.snapshot = stale_conditions
        self.fx.lab.step(self.state)
        self.assertFalse(self.fake.orders)
        self.fx.now += 305
        self.fx.lab.step(self.state)
        self.assertFalse(self.fake.orders)

    def test_owner_stop_during_final_check_and_recovery_remain_close_only(self):
        self.start()
        self.fx.lab.step(self.state)
        self.assertGreater(self.bot["position_btc"], 0)
        from trading_lab import exchange_engine as engine
        engine.stop(self.state, self.fx.now, reason="Відновлення після перерви: лише закриття.")
        self.state.update(_recovery=True, incomplete=True)
        self.fx.lab.resources[self.state["id"]]["apis"][0].close_only = True
        self.fx.now += 1
        self.fx.lab.step(self.state)
        self.fx.now += 1
        self.fx.lab.step(self.state)
        self.assertEqual(self.bot["position_btc"], 0)
        self.assertEqual(self.bot["ai_requests"], 0)
        self.assertEqual(sum(not r["reduceOnly"] for r in self.fake.orders.values()), 1)
        self.assertEqual(self.state["status"], "DONE")

    def test_stop_arrives_before_submission_no_first_order(self):
        self.start()
        feed = self.fx.lab.resources[self.state["id"]]["feed"]
        original = feed.snapshot
        count = 0
        def stopped():
            nonlocal count
            count += 1
            if count > 1: self.fx.lab.stop(self.fx.lab.data["generation"], self.state["id"])
            return original()
        feed.snapshot = stopped
        self.fx.lab.step(self.state)
        self.assertFalse(self.fake.orders)

    def test_unknown_billing_blocks_paid_mode_only_without_reset(self):
        ledger = self.fx.lab.ledgers["workshop_demo"]
        reservation = ledger.reserve("old-run", "unknown", self.fx.now)
        ledger.finish(reservation, None)
        before = ledger.summary("")
        with self.assertRaisesRegex(ArenaError, "бюджет"):
            self.fx.start()
        self.start()
        self.fx.lab.step(self.state)
        self.assertEqual(before, ledger.summary(""))

    def test_scope_and_resealed_tampering_rejected(self):
        lab = self.fx.lab
        technical = self.fx.workshop.add_version(self.fx.workshop.data["generation"], template())["id"]
        for identity, mode in ((technical, "rules_only"), (self.fx.version, "unknown")):
            with self.assertRaises(ArenaError):
                lab.start(lab.data["generation"], "workshop_demo", [identity], ["demo"], 100, 300, execution_mode=mode)
        self.start()
        for field in ("ai_role", "decision_policy", "jev_budget", "execution_mode"):
            altered = deepcopy(self.state)
            if field == "ai_role":
                altered["rules"][field] = "changed"
                altered["match_manifest"]["risk_and_execution"][field] = "changed"
            elif field == "execution_mode":
                altered[field] = "demo_mechanics"
                altered["match_manifest"][field] = "demo_mechanics"
            else: altered["match_manifest"][field] = {"changed": True}
            altered["match_manifest_sha256"] = fingerprint(altered["match_manifest"])
            with self.subTest(field=field), self.assertRaises(ArenaError): policy.validate_state(altered)

    def test_private_bundle_shadow_links_no_public_payload_and_dry_run(self):
        self.start()
        self.fx.lab.step(self.state)
        self.fx.lab.stop(self.fx.lab.data["generation"], self.state["id"])
        for _ in range(3):
            self.fx.now += 1
            self.fx.lab.step(self.state)
        full = self.full()
        public = build_review(full)
        self.assertNotIn("payload_wire_json", json.dumps(public))
        self.assertNotIn("request_payloads", public)
        bundle = build_review(full, include_request_payloads=True)
        self.assertTrue(verify_bundle(bundle)["verified"])
        batch = cases(bundle, repeat=2)
        self.assertEqual(batch[0]["body"], bundle["request_payloads"][0]["payload_wire_json"].encode())
        self.assertEqual(len(batch), 2)
        self.assertEqual(len(batch[0]["meta"]["slots"]["c0"]["linked_trips"]), 1)
        examples = frozenset(p["id"] for p in bundle["participants"])  # R2.1.11 (E): an example author is no gate evidence
        with self.assertRaisesRegex(ArenaError, "навчальними прикладами"):
            cases(bundle, repeat=1, examples=examples)  # R2111-GATE: no slot left -> no case, nothing reaches Jev
        no_calls = Mock(side_effect=AssertionError("DRY RUN must not access key/network"))
        # the fixture's recipe is a Codex sample: with its own strategies.json the CLI refuses it, nothing to Jev
        self.assertEqual(self.shadow_run(full, bundle, no_calls), 2)
        from trading_lab.battle_export import create_archive
        import zipfile
        with create_archive(full) as stream, zipfile.ZipFile(stream) as archive:
            forged = json.loads(archive.read("strategies.json"))
        for row in forged:  # R2111-GATE: the file edited so the samples count, recipe_sha256 kept: its recipe hashes elsewhere
            row["recipe"]["author"] = {**row["recipe"]["author"], "kind": "human"}
        self.assertEqual(self.shadow_run(full, bundle, no_calls, forged), 2)
        no_calls.assert_not_called()
        request = bundle["request_payloads"][0]
        request["payload_wire_json"] += " "
        bundle["integrity"]["bundle_sha256"] = fingerprint({k: v for k, v in bundle.items() if k != "integrity"})
        with self.assertRaises(ArenaError): cases(bundle)

    def shadow_run(self, full, bundle, transport, strategies=None, required=True):
        """jev_gate_probe shadow-run on the bundle with strategies.json of the battle's own ZIP (or the rows given)."""
        from trading_lab.battle_export import create_archive
        import zipfile
        if strategies is None:
            with create_archive(full) as stream, zipfile.ZipFile(stream) as archive:
                strategies = json.loads(archive.read("strategies.json"))
        path, recipes = self.fx.root / "bundle.json", self.fx.root / "strategies.json"
        path.write_text(json.dumps(bundle), encoding="utf-8")
        recipes.write_text(json.dumps(strategies), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return probe.main(["shadow-run", "--bundle", str(path)] + (["--strategies", str(recipes)] if required else []),
                              transport=transport, key_reader=transport)

    def test_gate_shadow_run_needs_the_strategies_of_its_own_battle(self):
        # R2111-GATE: --strategies required and checked against the bundle's recipe_sha256; a human author's battle runs
        from trading_lab.arena_strategies import catalogue_defaults
        from trading_lab.battle_export import create_archive
        from trading_lab.gate_shadow import examples_in
        from trading_lab.strategy_contract import payload_of
        import zipfile
        recipe = payload_of(catalogue_defaults()[0])
        recipe["author"] = {**recipe["author"], "name": "Тестовий автор", "kind": "human"}
        self.fx.version = self.fx.workshop.add_version(self.fx.workshop.data["generation"], recipe)["id"]
        self.start()
        self.fx.lab.step(self.state)
        self.fx.lab.stop(self.fx.lab.data["generation"], self.state["id"])
        for _ in range(3):
            self.fx.now += 1
            self.fx.lab.step(self.state)
        full = self.full()
        bundle = build_review(full, include_request_payloads=True)
        with create_archive(full) as stream, zipfile.ZipFile(stream) as archive:
            strategies = json.loads(archive.read("strategies.json"))
        self.assertEqual(examples_in(bundle, strategies), frozenset())
        no_calls = Mock(side_effect=AssertionError("DRY RUN must not access key/network"))
        self.assertEqual(self.shadow_run(full, bundle, no_calls, strategies), 0)
        with self.assertRaises(SystemExit):  # argparse: the option is required
            self.shadow_run(full, bundle, no_calls, strategies, required=False)
        other = deepcopy(strategies)
        other[0]["recipe"]["rules"]["cooldown_seconds"] += 1  # another battle's file: its recipe hashes elsewhere
        other[0]["recipe_sha256"] = fingerprint(other[0]["recipe"])
        self.assertEqual(self.shadow_run(full, bundle, no_calls, other), 2)
        forged = deepcopy(strategies)
        forged[0]["recipe"]["author"]["kind"] = "example"  # the hash kept, the recipe changed
        self.assertEqual(self.shadow_run(full, bundle, no_calls, forged), 2)
        self.assertEqual(self.shadow_run(full, bundle, no_calls, strategies + deepcopy(strategies)), 2)  # duplicated rows
        self.assertEqual(self.shadow_run(full, bundle, no_calls, []), 2)  # a participant missing
        no_calls.assert_not_called()

    def test_shadow_comparison_all_slots_unknowns_and_placebo(self):
        slots = {"c0": {"bot_id": "a", "linked_trips": [{"entry_seq": 5, "close_seq": 8, "complete": True, "observed_net_usdt": "1"}]},
                 "c1": {"bot_id": "b", "linked_trips": [{"entry_seq": 6, "close_seq": 9, "complete": True, "observed_net_usdt": "-2"}]}}
        record = {"status": "ok", "meta": {"seq": 1, "repeat": 0, "slots": slots}, "answers": {
            "c0": {"type": "choice", "choice": "proceed", "confidence": .8},
            "c1": {"type": "choice", "choice": "proceed", "confidence": .5}}}
        result = comparison([record])
        self.assertEqual(result["pass_ge_0_70"], .5)
        self.assertEqual(result["accepted_net_usdt"], 1)
        self.assertEqual(result["blocked_net_usdt"], -2)
        self.assertIsNotNone(result["placebo"])
        self.assertIsNone(result["welch_t_descriptive"])
        record["answers"]["c1"]["confidence"] = float("nan")
        self.assertEqual(comparison([record])["valid_answers"], 1)

    def test_demo_report_qualifies_with_exchange_hash_and_explicit_mode(self):
        self.start()
        self.fx.lab.step(self.state)
        # Keep heartbeats running through cutoff and the authored hold rule.
        for _ in range(105):
            if self.state["status"] == "DONE": break
            self.fx.now += 10
            self.fx.lab.step(self.state)
        self.assertEqual(self.state["status"], "DONE")
        self.assertFalse(self.state["incomplete"])
        workshop = self.fx.workshop
        workshop.record_exchange_trial(self.full())
        report = workshop.trial(self.state["id"])
        self.assertEqual(report["status"], "COMPLETE")
        self.assertEqual(report["runtime"], "OKX_DEMO_RULES_ONLY")
        self.assertEqual(report["execution_mode"], "rules_only")
        self.assertEqual(report["ai_cost_usd"], 0)
        workshop.freeze(workshop.data["generation"], self.fx.version)
        workshop.promote(workshop.data["generation"], self.fx.version)
        self.assertEqual(workshop.version(self.fx.version)["qualification_code_sha256"], self.state["code_sha256"])
        from trading_lab.strategy_contract import author_contract
        self.assertEqual(author_contract()["rules_only_check"], policy.capability())

    def test_shadow_fake_paid_replays_wire_and_oversize_never_spends(self):
        self.start()
        self.fx.lab.step(self.state)
        batch = cases(build_review(self.full(), include_request_payloads=True))
        ledger = probe.SpendLedger(self.fx.root / "runtime/shadow-ledger.sqlite3", .005, root=self.fx.root)
        exact = batch[0]["body"]
        calls = []
        def transport(body, key):
            self.assertEqual(body, exact)
            calls.append(body)
            return {"model": "FAKE_MODEL", "usage": {"cost": .00001}, "answers": {"c0": {"type": "choice", "choice": "proceed", "confidence": .8}}}
        records = probe.run_cases(batch + [{"case_id": "oversize", "body": b"x"*8001, "meta": {}}], ledger,
            transport=transport, key="FAKE_KEY", run_id="fake", workers=1, rate=1000,
            results_path=self.fx.root / "results.jsonl", payloads_path=self.fx.root / "payloads.jsonl")
        self.assertEqual(len(calls), 1)
        self.assertEqual(records[-1]["status"], "payload_too_large")
        self.assertEqual(ledger.totals()["calls"], 1)
        self.assertEqual(comparison(records)["valid_answers"], 1)

    def test_reevaluating_after_a_filled_entry_never_sends_a_second_entry(self):
        """V1 decides on the engine's 30 s grid (as PAPER and the exact evaluator), so one bar can be checked twice;
        after a fill the entry limits and the position itself stop any second entry order on that bar or later."""
        self.fx.now -= 25  # second 5 of the minute: the next two 30 s decisions see the same closed bar, then the next one
        self.start()
        self.fx.lab.step(self.state)
        self.assertGreater(self.bot["position_btc"], 0)
        for _ in range(3):
            self.fx.now += 30
            self.fx.lab.step(self.state)
        entries = [o for o in self.fake.orders.values() if not o["reduceOnly"]]
        self.assertEqual(len(entries), 1)
        checks = [e for e in self.full()["events"] if e["code"] in ("entry_checks", "position_checks") and e["bot_id"] == self.bot["id"]]
        self.assertTrue(all(not e["data"].get("local_checks_passed") or e["data"].get("kind") != "entry" for e in checks[1:]),
                        "after the fill no further check passes as a new entry")

    def test_order_attempt_and_losing_add_limits_remain(self):
        self.start()
        self.fx.lab.step(self.state)
        self.fx.now += 30
        self.fake.bid, self.fake.ask = Decimal("83960"), Decimal("83960.1")
        self.fx.lab.step(self.state)
        self.assertEqual(len(self.fake.orders), 1)
        self.assertEqual(self.bot["ai_requests"], 0)
        from trading_lab import exchange_engine as engine
        self.bot["_entry_attempts"] = 20
        candidate = engine.candidate(self.state, self.bot, self.state["strategies"][0], self.state["_last"], self.fx.now)
        self.assertFalse(candidate["local_checks_passed"])


class SlowAccountChecksTests(unittest.TestCase):
    """Live 2026-10-30: the tick's account checks took ~6 s and the recheck ~5 s more, so a 10 s limit on the decision
    snapshot vetoed every entry. Private GETs of the fake exchange advance the fake clock like that round."""
    setUp, start, full = RulesOnlyTests.setUp, RulesOnlyTests.start, RulesOnlyTests.full

    def slow(self, seconds_per_private_get, seconds_per_public_get=0.0):
        resource = self.fx.lab.resources[self.state["id"]]
        apis = list(resource["apis"]) + [getattr(resource["feed"], "api", None)]
        for api in {id(a): a for a in apis if a is not None}.values():
            original = api.transport
            def transport(request, original=original):
                path = request.full_url.split("://", 1)[-1].split("/", 1)[-1].split("?")[0]
                if request.get_method() == "GET":
                    self.fx.now += seconds_per_private_get if path.startswith(("api/v5/account/", "api/v5/trade/")) else seconds_per_public_get
                return original(request)
            api.transport = transport

    def events(self, code):
        return [e for e in self.full()["events"] if e["code"] == code]

    def test_slow_account_checks_enter_on_a_fresh_recheck_of_the_same_signal(self):
        self.start()
        self.slow(0.75)
        self.fx.lab.step(self.state)
        self.assertGreater(self.bot["position_btc"], 0)
        self.assertEqual(sum(not r["reduceOnly"] for r in self.fake.orders.values()), 1)
        rechecked = self.events("entry_rechecked")[-1]["data"]
        self.assertLess(rechecked["decision_age_seconds"], 5, "the account checks come before the decision snapshot")
        self.assertLessEqual(rechecked["fresh_age_seconds"], 10)
        self.assertLessEqual(rechecked["account_check_age_seconds"], 10)
        self.assertFalse(self.events("entry_cancelled"))

    def test_a_demo_slow_account_api_still_enters_on_a_fresh_quote(self):
        """OKX Demo, 2026-10-31: ~2-3 s a private request, so each account check takes ~25 s and the decision snapshot
        was ~50 s old at the order. The account checks now come before the fresh quote, so the quote stays fresh."""
        self.start()
        self.slow(3.0)
        self.fx.lab.step(self.state)
        self.assertGreater(self.bot["position_btc"], 0)
        rechecked = self.events("entry_rechecked")[-1]["data"]
        self.assertLess(rechecked["decision_age_seconds"], 5, "R1 full.1 decided on a snapshot taken before ~27 s of checks")
        self.assertLessEqual(rechecked["fresh_age_seconds"], 10)
        self.assertLessEqual(rechecked["account_check_age_seconds"], 10)
        self.assertFalse(self.events("entry_cancelled"))

    def test_the_measured_demo_profile_of_round_a76a80d5_enters(self):
        """The owner's Demo a76a80d5 (R1 full.1): account check 15.6 s at start, RTT 469 ms; every entry cancelled with
        decision ages 21.8-49.3 s and fresh-quote ages 11.8-14.0 s. Same profile on the fake transport: ~1.7 s a private
        GET, ~0.5 s a public GET."""
        self.start()
        self.slow(1.7, 0.5)
        self.fx.lab.step(self.state)
        self.assertGreater(self.bot["position_btc"], 0)
        rechecked = self.events("entry_rechecked")[-1]["data"]
        self.assertLessEqual(rechecked["decision_age_seconds"], 10)
        self.assertLessEqual(rechecked["fresh_age_seconds"], 10)
        self.assertLessEqual(rechecked["account_check_age_seconds"], 10)
        self.assertFalse(self.events("entry_cancelled"))

    def test_a_slow_public_api_does_not_age_the_account_check_out(self):
        """Review 2026-10-31: with slow public reads too, the quote's own fetch time must not push the account check past
        its limit; the check is judged at the moment the fresh quote is requested."""
        self.start()
        self.slow(3.0, 2.6)
        self.fx.lab.step(self.state)
        self.assertGreater(self.bot["position_btc"], 0)
        rechecked = self.events("entry_rechecked")[-1]["data"]
        self.assertLessEqual(rechecked["account_check_before_quote_seconds"], 10)
        self.assertLessEqual(rechecked["fresh_age_seconds"], 10)

    def test_an_account_check_older_than_the_quote_request_allows_is_cancelled(self):
        self.start()
        lab, original = self.fx.lab, self.fx.lab.timed_account_check
        def early(state, bot, api):
            finished = original(state, bot, api)
            lab.resources[state["id"]]["account_checked_at"][bot["id"]] = finished - 20  # as if it had ended 20 s ago
            return finished - 20
        lab.timed_account_check = early
        self.fx.lab.step(self.state)
        self.assertFalse(self.fake.orders)
        cancelled = self.events("entry_cancelled")[-1]["data"]
        self.assertIn("account_check_too_old", cancelled["reasons"])

    def test_a_stale_fresh_quote_still_vetoes_with_its_reason(self):
        self.start()
        feed = self.fx.lab.resources[self.state["id"]]["feed"]
        original, calls = feed.snapshot, []
        def late_quote():
            calls.append(1)
            snap = original()
            if len(calls) > 1:
                snap["at"] -= 11  # the fresh quote itself arrived too late
            return snap
        feed.snapshot = late_quote
        self.fx.lab.step(self.state)
        self.assertFalse(self.fake.orders)
        cancelled = self.events("entry_cancelled")[-1]["data"]
        self.assertEqual(cancelled["reason"], "fresh_quote_too_old")
        self.assertGreater(cancelled["fresh_age_seconds"], 10)
        self.assertIn("execution_snapshot_sha256", cancelled)

    def test_a_decision_older_than_its_budget_is_cancelled_not_dropped(self):
        self.start()
        lab, original_decide = self.fx.lab, self.fx.lab.decide
        def stalled(*args):
            self.fx.now += 125  # the loop stalled between the decision snapshot and the decision
            return original_decide(*args)
        lab.decide = stalled
        self.fx.lab.step(self.state)
        self.assertFalse(self.fake.orders)
        cancelled = self.events("entry_cancelled")
        self.assertTrue(cancelled, "a signal never vanishes without an event")
        self.assertEqual(cancelled[-1]["data"]["reason"], "decision_snapshot_too_old")
        self.assertGreater(cancelled[-1]["data"]["decision_age_seconds"], 120)
        self.assertFalse(self.events("rules_only_review"), "nothing is decided on a snapshot past its budget")

    def test_rechecks_of_two_accounts_run_in_parallel(self):
        import threading
        state, lab = self.twins(), self.fx.lab
        barrier, original = threading.Barrier(2, timeout=5), lab.checked_account
        def together(state, bot, api):
            barrier.wait()  # passes only when both accounts are checked at the same time
            return original(state, bot, api)
        lab.checked_account = together
        lab.step(state)
        self.assertEqual(len(self.events("entry_rechecked")), 2)
        self.assertTrue(all(b["position_btc"] > 0 for b in state["bots"] if b["id"] != "cash"))

    def twins(self):
        from trading_lab.arena_strategies import catalogue_defaults
        from trading_lab.strategy_contract import payload_of
        fx, lab = self.fx, self.fx.lab
        twin = fx.workshop.add_version(fx.workshop.data["generation"], payload_of(catalogue_defaults()[0]))["id"]
        for version in (fx.version, twin):  # the same recipe twice, so both bots signal on the same snapshot
            fx.workshop.train(fx.workshop.data["generation"], version, "synthetic-suite-v2", 100)
            fx.workshop.freeze(fx.workshop.data["generation"], version)
            fx.workshop.promote(fx.workshop.data["generation"], version)
        result = lab.start(lab.data["generation"], "arena_live", [fx.version, twin], ["arena-a", "arena-b"], 100, 300,
                           execution_mode=policy.RULES_ONLY)
        self.state = state = lab.load(result["id"])
        state.update(fixture_only=True, evidence_kind="SYNTHETIC")
        lab.prepare(state, lab.chosen_profiles(["arena-a", "arena-b"]), False)
        return state

    def test_a_slow_first_order_gives_the_next_candidate_a_new_quote_and_account_check(self):
        state, lab = self.twins(), self.fx.lab
        for api in lab.resources[state["id"]]["apis"]:
            original = api.transport
            def transport(request, original=original):
                if request.get_method() == "POST":
                    self.fx.now += 6  # each order write as slow as a congested exchange
                return original(request)
            api.transport = transport
        taken, original_fresh = [], lab.fresh_snapshot
        lab.fresh_snapshot = lambda *a: taken.append(1) or original_fresh(*a)
        accounts, original_check = [], lab.checked_account
        lab.checked_account = lambda s, b, a: accounts.append(b["id"]) or original_check(s, b, a)
        lab.step(state)
        rechecked = self.events("entry_rechecked")
        self.assertEqual(len(rechecked), 2)
        self.assertTrue(all(e["data"]["fresh_age_seconds"] <= 10 for e in rechecked))
        self.assertEqual(len(taken), 2, "one shared quote, then a new one after the first order")
        self.assertTrue(all(b["position_btc"] > 0 for b in state["bots"] if b["id"] != "cash"))
        second = rechecked[1]["bot_id"]
        self.assertEqual(accounts.count(second), 2, "the tick's check, then again because the first order took > 10 s")

    def test_a_decision_that_ages_during_the_recheck_is_cancelled(self):
        self.start()
        lab = self.fx.lab
        feed = lab.resources[self.state["id"]]["feed"]
        original, calls = feed.snapshot, []
        def slow_fresh_quote():
            calls.append(1)
            if len(calls) > 1:
                self.fx.now += 125  # the fresh quote arrives fresh, but after the decision's stall guard
            return original()
        feed.snapshot = slow_fresh_quote
        lab.step(self.state)
        self.assertFalse(self.fake.orders)
        cancelled = self.events("entry_cancelled")[-1]["data"]
        self.assertEqual(cancelled["reason"], "decision_snapshot_too_old")
        self.assertLessEqual(cancelled["fresh_age_seconds"], 10)
        self.assertTrue(self.events("rules_only_review"), "decided within the budget, too old at the order")

    def test_stop_between_decision_and_recheck_and_blocked_admission_are_named(self):
        self.start()
        lab, original_decide = self.fx.lab, self.fx.lab.decide
        def decide(state, resource, candidates, snapshot):
            self.bot["stop_requested"] = True
            return original_decide(state, resource, candidates, snapshot)
        lab.decide = decide
        lab.step(self.state)
        self.assertFalse(self.fake.orders)
        self.assertEqual(self.events("entry_cancelled")[-1]["data"]["reason"], "owner_stop")

    def test_blocked_admission_during_the_recheck_is_named(self):
        self.start()
        feed = self.fx.lab.resources[self.state["id"]]["feed"]
        original, calls = feed.snapshot, []
        def blocked():
            calls.append(1)
            if len(calls) > 1:
                self.state["admission_blocked"] = "clock_gap"
            return original()
        feed.snapshot = blocked
        self.fx.lab.step(self.state)
        self.assertFalse(self.fake.orders)
        cancelled = self.events("entry_cancelled")[-1]["data"]
        self.assertEqual(cancelled["reasons"], ["admission_blocked"])
        self.assertTrue(all(c["passed"] is True for c in cancelled["checks"]), "the recipe passed; the season blocked it")

    def test_review_bundles_keep_the_cancel_reasons_and_ages(self):
        from trading_lab.battle_review import safe_data
        data = safe_data({"reason": "fresh_quote_too_old", "reasons": ["fresh_quote_too_old", "made_up", "account_identity_changed"],
                          "decision_age_seconds": 12.5, "fresh_age_seconds": 11.2, "decision_snapshot_sha256": "a" * 64, "kind": "entry",
                          "diagnostic": "account_identity_changed"})
        self.assertEqual((data["reason"], data["reasons"], data["kind"], data["diagnostic"]),
                         ("fresh_quote_too_old", ["fresh_quote_too_old", "account_identity_changed"], "entry", "account_identity_changed"))
        self.assertEqual((str(data["decision_age_seconds"]), data["decision_snapshot_sha256"]), ("12.5", "a" * 64))

    def test_changed_conditions_name_the_failed_check(self):
        self.start()
        feed = self.fx.lab.resources[self.state["id"]]["feed"]
        original, calls = feed.snapshot, []
        def widened():
            calls.append(1)
            snap = original()
            if len(calls) > 1:
                snap["features"]["spread_bps"] = 100
            return snap
        feed.snapshot = widened
        self.fx.lab.step(self.state)
        self.assertFalse(self.fake.orders)
        cancelled = self.events("entry_cancelled")[-1]["data"]
        self.assertEqual(cancelled["reasons"][0], "local_checks_failed")
        self.assertTrue(any(not c.get("passed", True) for c in cancelled["checks"]))


class PaidDecisionBudgetTests(unittest.TestCase):
    """A paid shared Jev call keeps the 10 s limit on its decision snapshot, so no answer is bought that cannot reach
    an order (the RULES_ONLY budget of 30 s does not apply to it)."""

    def setUp(self):
        self.fx = fixture.ExchangeTests(methodName="runTest")
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)

    def test_a_paid_jev_call_keeps_the_ten_second_snapshot_limit(self):
        fx = self.fx
        state = fx.start()  # the fixture's default mode is shared Jev on a fake transport
        original_decide = fx.lab.decide
        def stalled(*args):
            fx.now += 11  # the decision snapshot is 11 s old when the model would be asked
            return original_decide(*args)
        fx.lab.decide = stalled
        fx.lab.step(state)
        self.assertEqual(state["bots"][0]["ai_requests"], 0, "no model answer bought for a snapshot past 10 s")
        self.assertFalse(fx.fakes["demo"].orders)
        full = deepcopy(fx.lab.load(state["id"]))
        restore_events(fx.lab.path / (state["id"] + ".events.jsonl"), full, full=True)
        cancelled = [e for e in full["events"] if e["code"] == "entry_cancelled"]
        self.assertEqual(cancelled[-1]["data"]["reason"], "decision_snapshot_too_old")
