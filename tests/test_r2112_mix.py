"""R2.1.12 (MIX, D148/D149; team/platform/MIX_PLAN_UA.md and its check team/reviews/R2112_PLAN_CHECK_UA.md): one Live
RULES_ONLY season of a BTC V1 bot and an ETH schema-4 bot, each on its own fake subaccount. The fakes are strict: a
request naming a contract the account does not serve is recorded and refused as OKX would. Temp roots only."""
from decimal import Decimal
import json
from unittest.mock import Mock, patch
from urllib.parse import urlsplit
import unittest

import test_exchange_lab as fixture
import test_post_only
from test_exchange_eth import contracts_named, eth_payload
from trading_lab import exchange_diagnostics, exchange_engine as engine, exchange_execution as policy, strategy_training
from trading_lab.exchange_api import ExchangeFeed
from trading_lab.exchange_lab import ExchangeLab, own_snapshot, tag_snapshot
from trading_lab.okx_readonly import PreflightError

BTC, ETH = "BTC-USDT-SWAP", "ETH-USDT-SWAP"
PUBLIC = ("/api/v5/market/", "/api/v5/public/")
FOREIGN_POSITION = lambda inst: (lambda fake: {"instId": inst, "mgnMode": "isolated", "posSide": "net", "lever": "1", "pos": "1"})
FOREIGN_ORDER = lambda inst: (lambda fake: {"instId": inst, "clOrdId": "manual-order", "ordId": "9", "state": "live"})


def FOREIGN_BILL(inst, sub, kind):
    return lambda fake: {"billId": "9999", "ts": str(int(fake.clock() * 1000)), "type": kind, "subType": sub, "ccy": "USDT", "instId": inst,
                         "ordId": "", "pnl": "-0.5", "fee": "0", "balChg": "-0.5", "posBalChg": "0"}


def private(requests):
    return [r for r in requests if "/api/v5/market/" not in r.full_url and "/api/v5/public/" not in r.full_url]


class MixedSeason(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.ExchangeTests(methodName="runTest")
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        fx, shop = self.fx, self.fx.workshop
        fx.lab.jev_factory = Mock(side_effect=AssertionError("No Jev construction for RULES_ONLY"))
        self.btc, self.eth = fx.fakes["arena-a"], fx.fakes["arena-b"]
        self.eth.use(fixture.ETH_SPEC, 3000, 1)
        self.btc.serve(fixture.ETH_SPEC, 3000, 1)  # the season's market client is its first bot's account (prepare)
        for fake in (self.btc, self.eth):
            fake.strict = True
        eth_version = shop.add_version(shop.data["generation"], eth_payload())["id"]
        for version, dataset in ((fx.version, "synthetic-suite-v2"), (eth_version, strategy_training.SYNTHETIC["id"])):
            shop.train(shop.data["generation"], version, dataset, 100)
            shop.freeze(shop.data["generation"], version)
            shop.promote(shop.data["generation"], version)
        self.ids = [fx.version, eth_version]

    def start(self):
        lab = self.fx.lab
        result = lab.start(lab.data["generation"], "arena_live", self.ids, ["arena-a", "arena-b"], 100, 300,
                           execution_mode=policy.RULES_ONLY)
        state = lab.load(result["id"])
        state.update(fixture_only=True, evidence_kind="SYNTHETIC")
        lab.prepare(state, lab.chosen_profiles(["arena-a", "arena-b"]), False)
        self.state, (self.b, self.e) = state, state["bots"][:2]
        return state

    def steps(self, n, until=None, dt=5):
        for _ in range(n):
            self.fx.lab.step(self.state)
            if self.state["status"] in {"DONE", "PAUSED"} or (until and until()):
                return
            self.fx.now += dt

    def events(self, code):
        return [e for e in self.state["events"] if e["code"] == code]

    def finish(self):
        lab = self.fx.lab
        lab.stop(lab.data["generation"], self.state["id"])
        self.steps(60, until=lambda: self.state["status"] == "DONE")

    def test_mix_the_rules_name_each_bot_contract_and_its_client_trades_only_it(self):
        state = self.start()
        self.assertEqual(state["rules"]["instrument"], engine.MIXED)
        self.assertEqual(state["rules"]["instruments"], dict(zip(self.ids, (BTC, ETH))))
        self.assertEqual((self.b["_instrument"]["instrument"], self.e["_instrument"]["instrument"]), (BTC, ETH))
        apis = self.fx.lab.resources[state["id"]]["apis"]
        self.assertEqual([a.inst_id for a in apis], [BTC, ETH])
        self.assertEqual(sorted(self.fx.lab.resources[state["id"]]["feeds"]), [BTC, ETH])
        self.assertNotIn("feed", self.fx.lab.resources[state["id"]])  # no reader may take one contract for the season
        self.steps(1)
        public = next(r for r in [self.fx.lab.status()["active"]["arena_live"]] if r)
        self.assertIsNone(public["quantity_units"]["coin"])  # no season coin; the bots' base_ccy name theirs
        self.assertEqual(sorted(b.get("base_ccy") for b in public["bots"] if b["id"] != "cash"), ["BTC", "ETH"])

    def test_mix_isolation_orders_size_position_and_accounting_by_own_contract(self):
        # D149 p. 1: each bot's orders only on its instId, its size, position and accounting by its own contract
        state = self.start()
        self.steps(80, until=lambda: self.b["position_btc"] and self.e["position_btc"])
        self.assertTrue(self.b["position_btc"] and self.e["position_btc"], [e["code"] for e in state["events"][-12:]])
        for bot, mid in ((self.b, 84000), (self.e, 3000)):  # marked at its own contract's bid: no ~28x mis-mark
            self.assertLess(abs(bot["equity_usdt"] - 100), 5, bot["id"])
        eth_entry = next(o for o in self.eth.orders.values() if not o["reduceOnly"])
        self.assertEqual(Decimal(eth_entry["sz"]), Decimal(fixture.ETH_SPEC["minSz"]))  # G12: one lot of ETH
        btc_entry = next(o for o in self.btc.orders.values() if not o["reduceOnly"])
        self.assertLessEqual(Decimal(btc_entry["sz"]) * Decimal(fixture.SPEC["ctVal"]) * Decimal(84000), Decimal(100))
        self.finish()
        self.assertEqual(state["status"], "DONE")
        self.assertTrue(self.b["accounting_reconciled"] and self.e["accounting_reconciled"])
        self.assertEqual((self.b["position_btc"], self.e["position_btc"]), (0, 0))
        for fake, own in ((self.btc, BTC), (self.eth, ETH)):
            with self.subTest(own=own):
                self.assertEqual(fake.violations, [])
                self.assertEqual(contracts_named(private(fake.requests)), {own})
                self.assertTrue(fake.orders and all(o["instId"] == own for o in fake.orders.values()))
                self.assertTrue(all(a["instId"] == own for a in fake.algos.values()))
                self.assertTrue(all(b["instId"] == own for b in fake.bills))
        self.assertEqual(contracts_named([r for r in self.eth.requests if any(p in r.full_url for p in PUBLIC)]), {ETH})  # its preflight
        self.assertEqual(contracts_named([r for r in self.btc.requests if any(p in r.full_url for p in PUBLIC)]), {BTC, ETH})
        frames = [f for f in state["frames"] if f.get("markets")]
        self.assertTrue(frames and all(f["market"] is None for f in frames))
        last = next(f for f in reversed(frames) if all(f["markets"].values()))
        self.assertGreater(last["markets"][BTC]["mid"], 10000)
        self.assertLess(last["markets"][ETH]["mid"], 10000)
        files = engine.snapshot_files(self.fx.lab.path, state)
        for contract, path in files.items():
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(rows and all(r["raw"]["ticker"][0]["instId"] == contract for r in rows), contract)
        self.assertEqual(set(state["latest_markets"]), {BTC, ETH})
        self.assertIsNone(state.get("latest_market"))  # new_season has the key; a mixed season never fills it

    def test_mix_one_decision_and_one_shadow_payload_per_contract(self):
        # plan check p. 1: make_payload refuses candidates of two markets; a decision per contract, no worker failure
        state = self.start()
        groups = []
        original = policy.decision

        def capture(s, candidates):
            groups.append(sorted({engine.instrument_for(s, c["id"]) for c in candidates}))
            return original(s, candidates)
        with patch.object(policy, "decision", capture):
            self.steps(80, until=lambda: self.b["position_btc"] and self.e["position_btc"])
        self.assertTrue(groups and all(len(g) == 1 for g in groups), groups)
        self.assertEqual({g[0] for g in groups}, {BTC, ETH})
        shadows = self.events("jev_shadow_payload")
        self.assertEqual({e["data"]["instrument"] for e in shadows}, {BTC, ETH})
        self.assertFalse(self.events("worker_failed"))

    def test_mix_a_market_fault_of_one_contract_blocks_only_its_bots(self):
        # D149 p. 3: three failures of the ETH feed block ETH entries only; incomplete as before (D138 p. 1)
        state = self.start()
        api = self.fx.lab.resources[state["id"]]["feeds"][ETH]
        api = getattr(api, "base", api).api
        original = api.transport

        def broken(request):
            if "/market/ticker" in request.full_url:
                raise OSError("fictional ETH market outage")
            return original(request)
        api.transport = broken
        self.steps(4)
        self.assertEqual(state.get("admission_blocked_by_instrument"), {ETH: "market_unavailable"})
        self.assertNotIn("admission_blocked", state)  # the season's own block stays free for BTC
        self.assertTrue(state["incomplete"])
        failures = self.events("market_unavailable")
        self.assertTrue(failures and all(e["data"]["instrument"] == ETH for e in failures))
        self.steps(40, until=lambda: self.b["position_btc"])
        self.assertTrue(self.b["position_btc"])  # BTC entered under the ETH block
        self.assertFalse(self.e["position_btc"])
        self.assertFalse(self.eth.orders)
        api.transport = original
        self.steps(2)
        self.assertEqual(state.get("admission_blocked_by_instrument"), {})  # the next clean ETH snapshot lifts it

    def test_mix_a_quote_regression_blocks_its_contract_and_a_wall_clock_jump_the_season(self):
        # plan check p. 6: a gap or a quote regression is the contract's, a wall-clock jump the season's
        state = self.start()
        self.steps(2)
        guards = self.fx.lab.resources[state["id"]]["clock_guards"]
        guards[ETH].last_quote_ms += 10 ** 9
        self.steps(1)
        self.assertEqual(state["admission_blocked_by_instrument"].get(ETH), "clock_gap")
        self.assertNotIn("admission_blocked", state)
        self.assertTrue(all(e["data"]["instrument"] == ETH for e in self.events("clock_gap")))
        for guard in guards.values():
            guard.last_wall -= 600  # the Windows clock set forward 600 s between two observations
        self.steps(1)
        self.assertEqual(state.get("admission_blocked"), "clock_gap")

    def test_mix_a_snapshot_of_another_contract_is_refused(self):
        state = self.start()
        snapshot = {"raw": {"ticker": [{"instId": BTC}]}}
        with self.assertRaisesRegex(PreflightError, "ticker_instrument_mismatch"):
            tag_snapshot(dict(snapshot), ETH)
        with self.assertRaisesRegex(PreflightError, "ticker_instrument_mismatch"):
            own_snapshot(state, self.e, tag_snapshot(dict(snapshot), BTC))
        self.assertIsNotNone(own_snapshot(state, self.b, tag_snapshot(dict(snapshot), BTC)))

    def test_mix_a_shared_contract_never_reaches_the_venue(self):
        # plan check p. 2 and answer 1: with one season contract ("MIXED") the strict fakes refuse every request
        lab = self.fx.lab
        with patch.object(engine, "instrument_for", lambda state, bot_id: state["rules"]["instrument"]):
            with self.assertRaises(PreflightError):
                self.start()
        self.assertTrue(all(v[2] == ["MIXED"] for fake in (self.btc, self.eth) for v in fake.violations))
        self.assertFalse(self.btc.orders or self.eth.orders)

    def test_mix_a_bot_whose_contract_is_not_its_rules_contract_never_trades(self):
        # plan check p. 2: at a real venue an ETH subaccount could preflight BTC; the invariant after preflight refuses it
        real = engine.initialize

        def wrong(bot, check):
            real(bot, check)
            if bot["_instrument"]["instrument"] == ETH:
                bot["_instrument"] = {**bot["_instrument"], "instrument": BTC}
        with patch.object(engine, "initialize", wrong), self.assertRaisesRegex(PreflightError, "instrument_unavailable_or_mismatched"):
            self.start()
        self.assertFalse(self.btc.orders or self.eth.orders)

    def test_mix_the_access_check_reads_each_account_on_its_recipe_contract(self):
        # MIX-1: the server takes each account's contract from its recipe, by the function of the Start
        lab = self.fx.lab
        lab.check(lab.data["generation"], ["arena-a", "arena-b"], self.ids)
        checks = lab.data["checks"]
        self.assertEqual((checks["arena-a"]["instrument"], checks["arena-b"]["instrument"]), (BTC, ETH))
        self.assertTrue(checks["arena-a"]["ready"] and checks["arena-b"]["ready"], checks)
        self.assertEqual((self.btc.violations, self.eth.violations), ([], []))
        lab.check(lab.data["generation"], ["arena-b"])  # without recipes: BTC, as before; the strict ETH account refuses it
        self.assertEqual(lab.data["checks"]["arena-b"]["instrument"], BTC)
        self.assertFalse(lab.data["checks"]["arena-b"]["ready"])

    def test_mix_export_review_debrief_and_author_packets_keep_each_contract_apart(self):
        # plan check pp. 13-18: one market member per contract, no market.jsonl; the review and debrief per participant
        from copy import deepcopy
        import zipfile
        from tools import live_packets
        from trading_lab.battle_debrief import load_debrief
        from trading_lab.battle_export import create_archive, verify_archive
        from trading_lab.battle_review import build_review
        from trading_lab.match_journal import restore_events
        from trading_lab.web_panel import season_quotes
        state = self.start()
        self.steps(80, until=lambda: self.b["position_btc"] and self.e["position_btc"])
        self.steps(6)  # both positions held a while: quotes inside both trades for the debrief's price paths
        feed = self.fx.lab.resources[state["id"]]["feeds"][ETH]
        api = getattr(feed, "base", feed).api
        original = api.transport
        api.transport = lambda request: (_ for _ in ()).throw(OSError("fictional ETH outage")) if "/market/ticker" in request.full_url else original(request)
        self.steps(1)
        api.transport = original
        self.finish()
        lab = self.fx.lab
        full = deepcopy(lab.load(state["id"]))
        restore_events(lab.path / (full["id"] + ".events.jsonl"), full, full=True)
        quotes = season_quotes(lab.path, full, full["id"])
        self.assertEqual(sorted(quotes), [BTC, ETH])
        review = build_review(full, snapshot_context=quotes)
        self.assertEqual(review["match"]["instruments"], [BTC, ETH])
        self.assertEqual(review["match"]["instrument"], engine.MIXED)
        rows = {p["id"]: p for p in review["participants"]}
        self.assertEqual((rows[self.b["id"]]["instrument"], rows[self.e["id"]]["instrument"]), (BTC, ETH))
        eth_gap = [e["seq"] for e in review["events"] if e["code"] == "market_unavailable"]
        self.assertTrue(eth_gap and all(e["data"]["instrument"] == ETH for e in review["events"] if e["code"] == "market_unavailable"))
        self.assertFalse(set(eth_gap) & set(rows[self.b["id"]]["quality"]["gap_event_seqs"]))  # not the BTC bot's gap
        self.assertTrue(set(eth_gap) <= set(rows[self.e["id"]]["quality"]["gap_event_seqs"]))
        self.assertTrue(review["market_gaps"] and all(g["instrument"] == ETH for g in review["market_gaps"]))
        with create_archive(full, snapshots_path=engine.snapshot_files(lab.path, full)) as stream:
            path = lab.path / "mixed.zip"
            path.write_bytes(stream.read())
        self.assertTrue(verify_archive(path)["verified"])
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            self.assertNotIn("market.jsonl", names)
            manifest = json.loads(archive.read("manifest.json"))
            mids = {c: [json.loads(line)["mid"] for line in archive.read("market." + c + ".jsonl").splitlines()] for c in (BTC, ETH)}
            settings = json.loads(archive.read("match-settings.json"))
        self.assertTrue(mids[BTC] and min(mids[BTC]) > 10000 and mids[ETH] and max(mids[ETH]) < 10000)
        self.assertEqual(manifest["market_samples_by_instrument"], {BTC: len(mids[BTC]), ETH: len(mids[ETH])})
        self.assertEqual(manifest["market_samples"], len(mids[BTC]) + len(mids[ETH]))
        self.assertEqual({p["strategy_id"]: p.get("instrument") for p in settings["participants"]}, {self.b["id"]: BTC, self.e["id"]: ETH})
        report = load_debrief(path)  # merged BTC and ETH quotes would raise ArenaError (out of order, conflicting mids)
        self.assertTrue(0 < report["market_samples"] <= manifest["market_samples"])  # equal-time quotes merged, as before
        files, digest, zip_manifest = live_packets.read_zip(path)
        for bot, contract in ((self.b, BTC), (self.e, ETH)):
            out, made = live_packets.packet(files, digest, zip_manifest, "author-" + contract[:3], bot["id"])
            self.assertEqual(out["market.jsonl"], files["market." + contract + ".jsonl"])  # the author's own contract only
            listed = {f["path"] for f in made["source_zip_manifest"]["files"]}  # X2-08: the packet's list and text say so too
            self.assertEqual({n for n in listed if n.startswith("market.")}, {"market." + contract + ".jsonl"})
            self.assertIn("лише твого інструмента (" + contract + ")", out["README_UA.txt"].decode("utf-8"))
            self.assertNotIn("публічні ціни раунду", out["README_UA.txt"].decode("utf-8"))
            self.assertEqual(json.loads(out["summary.json"])["match"]["instruments"], [BTC, ETH])

    def test_mix_league_marks_the_cohort_and_the_public_review_names_the_contracts(self):
        # D149 p. 2: the same league, a cohort mark; the public DTO's closed-world verifier knows the new keys
        from copy import deepcopy
        from trading_lab.arena_strategies import ArenaError
        from trading_lab.battle_review import build_review
        from trading_lab.league import instruments_mark
        from trading_lab.match_journal import restore_events
        from trading_lab.public_site import digest, public_review, verify_public_review
        state = self.start()
        self.steps(4)
        self.finish()
        self.assertEqual(instruments_mark(state), "змішаний: BTC ×1 + ETH ×1")
        self.assertIsNone(instruments_mark({"rules": {"instrument": BTC}}))
        full = deepcopy(self.fx.lab.load(state["id"]))
        restore_events(self.fx.lab.path / (full["id"] + ".events.jsonl"), full, full=True)
        dto = public_review(build_review(full))
        verify_public_review(dto)
        self.assertEqual(dto["match"]["instruments"], [BTC, ETH])
        self.assertEqual({p["id"]: p["instrument"] for p in dto["participants"]}, {self.b["id"]: BTC, self.e["id"]: ETH})
        forged = deepcopy(dto)
        forged["participants"][0]["instrument"] = "SOL-USDT-SWAP"  # not a contract of this battle
        forged["integrity"]["review_sha256"] = digest({k: v for k, v in forged.items() if k != "integrity"})
        with self.assertRaises(ArenaError):
            verify_public_review(forged)
        rows = [r for r in self.fx.lab.status()["history"] if r["id"] == state["id"]]
        self.assertTrue(rows)

    def test_mix_a_contract_block_with_a_fresh_snapshot_refuses_only_its_bots_entries(self):
        # D149 p. 3 / plan check p. 5: the block is the contract's even while its market reads clean. BTC (V1, no feature
        # book) is blocked: only the bot's view of the block stands between its candidate and an order
        state = self.start()
        state["admission_blocked_by_instrument"] = {BTC: "test_block"}  # a reason no clean snapshot lifts
        self.steps(80, until=lambda: self.e["position_btc"])
        self.steps(6)
        self.assertTrue(self.e["position_btc"])  # ETH entered under the BTC block
        self.assertFalse(self.b["position_btc"] or any(not o["reduceOnly"] for o in self.btc.orders.values()))
        self.assertNotIn("admission_blocked", state)  # never left behind by the bot's view

    def test_mix_history_index_candles_keep_okx_10_per_2_s_across_feeds(self):
        # plan check p. 7: one IP; the shared index limiter refuses the ninth non-critical read inside 2 s
        from trading_lab.ops_policy import RollingRequestBudget
        state = self.start()
        feeds = self.fx.lab.resources[state["id"]]["feeds"]
        apis = [getattr(f, "base", f).api for f in feeds.values()]
        self.assertIs(apis[0].index_reads, apis[1].index_reads)
        self.assertIs(apis[0].rolling_reads, apis[1].rolling_reads)
        limiter = apis[0].index_reads
        self.assertIsInstance(limiter, RollingRequestBudget)
        now = self.fx.now
        allowed = [limiter.acquire(now) for _ in range(10)]  # one instant: the API reads the same monotonic clock
        self.assertEqual(allowed, [True] * 8 + [False] * 2)
        with self.assertRaisesRegex(PreflightError, "rolling_read_rate_limit"):
            apis[1].get("/api/v5/market/history-index-candles", {"instId": "ETH-USDT", "limit": "1"}, private=False)

    def test_mix_the_fifth_live_profile_is_offered_and_trades_on_its_own_subaccount(self):
        # D151: the profile ids of the other PC (OTHER_PC_UA.md), through the real profiles() and public_profiles()
        from pathlib import Path
        from trading_lab.arena_strategies import catalogue_defaults
        from trading_lab.strategy_contract import payload_of
        fx, lab, shop = self.fx, self.fx.lab, self.fx.workshop
        live = ["arena-a", "arena-b", "arena-claude-opus", "arena-claude-sonet", "arena-yurii"]
        for name, uid in (("arena-claude-opus", "104"), ("arena-claude-sonet", "105"), ("arena-yurii", "106")):
            fx.fakes[name] = fixture.FakeExchange(fx.clock, uid)
        (Path(fx.root) / "LocalSettings").mkdir()
        (Path(fx.root) / "LocalSettings" / "exchange_accounts.json").write_text(json.dumps({"profiles": [
            {"id": "demo", "label": "Demo", "environment": "demo", "settings_file": ".env.okx.demo.local"},
            *({"id": name, "label": name, "environment": "live", "settings_file": ".env.okx." + name + ".local"} for name in live)]}),
            encoding="utf-8")
        for name in live[2:]:
            (Path(fx.root) / (".env.okx." + name + ".local")).write_text(
                "OKX_SITE=global\nOKX_ENV=live\nOKX_API_KEY=FAKE_KEY\nOKX_API_SECRET=FAKE_SECRET\nOKX_API_PASSPHRASE=FAKE_PASS\n", encoding="utf-8")
        self.eth.use(fixture.SPEC, 84000, 20)  # the roster of D148: only the Sonnet profile on ETH
        sonet, human = fx.fakes["arena-claude-sonet"], fx.fakes["arena-yurii"]
        sonet.use(fixture.ETH_SPEC, 3000, 1)
        fakes = [fx.fakes[name] for name in live]
        for fake in fakes:
            fake.strict = True
        offered = [p for p in lab.status()["profiles"] if p["environment"] == "OKX_LIVE"]
        self.assertEqual([p["id"] for p in offered], live)
        self.assertTrue(all(p["configured"] for p in offered))
        recipes = [payload_of(catalogue_defaults()[i]) for i in (1, 2)] + [{**payload_of(catalogue_defaults()[0]), "name": "Третій BTC"}]
        ids = []
        for recipe in recipes:
            version = shop.add_version(shop.data["generation"], recipe)["id"]
            shop.train(shop.data["generation"], version, "synthetic-suite-v2", 100)
            shop.freeze(shop.data["generation"], version)
            shop.promote(shop.data["generation"], version)
            ids.append(version)
        ids += [self.ids[1], self.ids[0]]  # ETH schema 4 on the Sonnet profile; the signalling BTC recipe on the fifth profile
        result = lab.start(lab.data["generation"], "arena_live", ids, live, 100, 300, execution_mode=policy.RULES_ONLY)
        self.assertEqual(result["id"], lab.load(result["id"])["id"])
        state = lab.load(result["id"])
        state.update(fixture_only=True, evidence_kind="SYNTHETIC")
        lab.prepare(state, lab.chosen_profiles(live), False)
        self.state = state
        fifth = state["bots"][4]
        self.steps(80, until=lambda: fifth["position_btc"])
        self.assertTrue(fifth["position_btc"], [e["code"] for e in state["events"][-12:]])
        self.assertEqual(fifth["_instrument"]["instrument"], BTC)
        self.assertTrue(human.orders and all(o["instId"] == BTC for o in human.orders.values()))
        self.finish()
        self.assertEqual(state["status"], "DONE")
        for name, fake in zip(live, fakes):
            with self.subTest(name):
                self.assertEqual(fake.violations, [])
                self.assertEqual(contracts_named(private(fake.requests)), {ETH if fake is sonet else BTC})

    # ---- R2.1.12 RC2 (X1-01): plan check p. 10, the reviewer's probes as tests ----
    def open_both(self):
        state = self.start()
        self.steps(80, until=lambda: self.b["position_btc"] and self.e["position_btc"])
        self.assertTrue(self.b["position_btc"] and self.e["position_btc"], [e["code"] for e in state["events"][-12:]])
        return state

    def resume_after_restart(self):
        """The process ends with both positions open; a new lab (a new process) loads the checkpoint. Fakes and temp roots only."""
        fx, old = self.fx, self.fx.lab
        old.save(self.state)
        old.close()
        fx.lab = ExchangeLab(fx.root, fx.workshop, fx.arena, clock=fx.clock, monotonic=fx.clock, background=False,
                             api_factory=lambda p: fx.fakes[p["id"]].api(p["environment"]),
                             jev_factory=Mock(side_effect=AssertionError("No Jev in recovery")),
                             feed_factory=lambda api: ExchangeFeed(api, clock=fx.clock))
        self.marks = {id(f): len(f.requests) for f in (self.btc, self.eth)}
        self.known = {id(f): set(f.orders) for f in (self.btc, self.eth)}
        fx.lab.resume(fx.lab.data["generation"], self.state["id"])
        self.state = fx.lab.sessions[self.state["id"]]
        self.b, self.e = self.state["bots"][:2]
        return fx.lab

    def feed_api(self, contract):
        feed = self.fx.lab.resources[self.state["id"]]["feeds"][contract]
        return getattr(feed, "base", feed).api

    def break_ticker(self, contract):
        """The contract's ticker fails from now on; the returned function repairs it."""
        api, original = self.feed_api(contract), self.feed_api(contract).transport

        def broken(request):
            if "/market/ticker" in request.full_url:
                raise OSError("fictional outage")
            return original(request)
        api.transport = broken
        return lambda: setattr(api, "transport", original)

    def test_mix_resume_closes_each_bot_reduce_only_on_its_own_contract(self):
        self.open_both()
        lab = self.resume_after_restart()
        self.assertEqual(self.state["_last_by_instrument"][ETH]["_instrument"], ETH)  # the tag survives the checkpoint
        lab.prepare(self.state, lab.chosen_profiles(["arena-a", "arena-b"]), True)
        self.steps(40, until=lambda: self.state["status"] == "DONE")
        self.assertEqual(self.state["status"], "DONE", [e["code"] for e in self.state["events"][-15:]])
        self.assertTrue(self.b["accounting_reconciled"] and self.e["accounting_reconciled"])
        for fake, own in ((self.btc, BTC), (self.eth, ETH)):
            new = [o for k, o in fake.orders.items() if k not in self.known[id(fake)]]
            self.assertTrue(new and all(o["reduceOnly"] and o["instId"] == own for o in new), (own, new))
            self.assertEqual(fake.violations, [])
            self.assertEqual(contracts_named(private(fake.requests[self.marks[id(fake)]:])), {own})
            self.assertEqual(fake.units, 0)

    def test_mix_a_contract_changed_while_down_pauses_the_whole_season_and_sends_nothing(self):
        self.open_both()
        lab = self.resume_after_restart()
        self.eth.spec["lotSz"] = "0.1"  # the venue changed the ETH contract while the process was down
        selected = lab.chosen_profiles(["arena-a", "arena-b"])
        with self.assertRaisesRegex(PreflightError, "recovery_contract_changed"):
            lab.prepare(self.state, selected, True)
        lab.worker(self.state, selected, True)  # the worker's own handler, synchronously
        self.assertEqual(self.state["status"], "PAUSED")
        self.assertEqual(self.events("exchange_error")[-1]["data"]["reason"], "recovery_contract_changed")
        self.assertIn("recovery_contract_changed", exchange_diagnostics.MANUAL_REVIEW_REASONS)
        for fake in (self.btc, self.eth):
            self.assertEqual(set(fake.orders), self.known[id(fake)])  # no new order, no close of either bot
            self.assertEqual([r for r in fake.requests[self.marks[id(fake)]:] if r.get_method() == "POST"], [])

    def test_mix_resume_closes_both_while_the_eth_feed_is_down(self):
        self.open_both()
        lab = self.resume_after_restart()
        lab.prepare(self.state, lab.chosen_profiles(["arena-a", "arena-b"]), True)
        self.break_ticker(ETH)
        self.steps(40, until=lambda: self.state["status"] == "DONE")
        self.assertEqual(self.state["status"], "DONE", [e["code"] for e in self.state["events"][-15:]])
        self.assertEqual((self.btc.units, self.eth.units), (0, 0))
        self.assertTrue(self.events("market_unavailable") and all(e["data"].get("instrument") == ETH for e in self.events("market_unavailable")))

    def add_row(self, fake, path, make_row):
        """Once armed, the account's answer on `path` also carries one row of the other contract."""
        armed, original = {"on": False}, fake.transport

        def wrapper(request):
            answer = original(request)
            if armed["on"] and request.get_method() == "GET" and urlsplit(request.full_url).path == path:
                answer = {**answer, "data": list(answer["data"]) + [make_row(fake)]}
            return answer
        fake.transport = wrapper
        return armed

    def assert_foreign_row_stops_the_bot(self, fake, bot, path, row, reason):
        armed = self.add_row(fake, path, row)
        self.open_both()
        bot = {"btc": self.b, "eth": self.e}[bot]
        position, armed["on"] = bot["position_btc"], True
        self.steps(14)
        self.assertTrue(bot["_manual_review"])
        self.assertEqual(bot["position_btc"], position)  # never absorbed, never closed by the code
        self.assertIn(reason, {e["data"].get("reason") for e in self.events("exchange_check_failed")})
        self.assertTrue(self.state["incomplete"])
        self.assertEqual((self.btc.violations, self.eth.violations), ([], []))
        self.assertEqual(contracts_named(private(fake.requests)), {BTC if fake is self.btc else ETH})  # none to the foreign contract

    def test_mix_a_foreign_eth_position_on_the_btc_subaccount_stops_for_manual_review(self):
        self.assert_foreign_row_stops_the_bot(self.btc, "btc", "/api/v5/account/positions", FOREIGN_POSITION(ETH), "foreign_or_unsupported_position")

    def test_mix_a_foreign_btc_position_on_the_eth_subaccount_stops_for_manual_review(self):
        self.assert_foreign_row_stops_the_bot(self.eth, "eth", "/api/v5/account/positions", FOREIGN_POSITION(BTC), "foreign_or_unsupported_position")

    def test_mix_a_foreign_pending_order_stops_for_manual_review(self):
        self.assert_foreign_row_stops_the_bot(self.btc, "btc", "/api/v5/trade/orders-pending", FOREIGN_ORDER(ETH), "foreign_or_unbounded_pending_orders")

    def test_mix_a_foreign_funding_bill_is_not_counted_and_blocks_that_bot(self):
        armed = self.add_row(self.eth, "/api/v5/account/bills", FOREIGN_BILL(BTC, "173", "8"))
        self.open_both()
        positions, armed["on"] = (self.b["position_btc"], self.e["position_btc"]), True
        funding = self.e.get("funding_usdt")
        self.steps(14)
        self.assertEqual(self.e["admission_blocked"], "account_unavailable")
        self.assertIsNone(self.b.get("admission_blocked"))
        self.assertEqual(self.e.get("funding_usdt"), funding)  # not absorbed into this bot's accounting
        self.assertEqual((self.b["position_btc"], self.e["position_btc"]), positions)
        self.assertEqual((self.btc.violations, self.eth.violations), ([], []))

    def test_mix_a_foreign_trade_bill_at_the_reconciliation_pauses_for_manual_review(self):
        armed = self.add_row(self.eth, "/api/v5/account/bills", FOREIGN_BILL(BTC, "3", "2"))
        self.open_both()
        armed["on"] = True
        self.finish()
        self.assertEqual(self.state["status"], "PAUSED")
        self.assertTrue(self.state["incomplete"])
        self.assertTrue(self.b["accounting_reconciled"])
        self.assertFalse(self.e["accounting_reconciled"])
        self.assertTrue(self.e["_manual_review"])
        self.assertIn("foreign_account_activity_in_bills", {e["data"].get("reason") for e in self.events("exchange_check_failed")})
        self.assertEqual((self.btc.units, self.eth.units), (0, 0))
        self.assertEqual((self.btc.violations, self.eth.violations), ([], []))

    # ---- D154 p. 2 ----
    def jump_clock(self, *contracts):
        guards = self.fx.lab.resources[self.state["id"]]["clock_guards"]
        for contract in contracts or guards:
            guards[contract].last_wall -= 600  # the Windows clock set forward 600 s between two observations

    def test_mix_the_clock_block_needs_only_the_feeds_that_answer(self):
        # X1-07: the ETH feed is down; the BTC bots wait one clean BTC observation, not ETH's return
        state = self.start()
        self.steps(2)
        repair = self.break_ticker(ETH)
        self.jump_clock()
        self.steps(1)
        self.assertEqual(state.get("admission_blocked"), "clock_gap")
        self.steps(1)
        self.assertNotIn("admission_blocked", state)
        repair()
        self.steps(1)  # the feed is back: its own guard reports the jump it has not seen yet
        self.assertEqual(state.get("admission_blocked"), "clock_gap")
        self.assertEqual({e["data"]["instrument"] for e in self.events("clock_gap")[-1:]}, {ETH})
        self.steps(1)
        self.assertNotIn("admission_blocked", state)

    def test_mix_the_clock_block_stays_while_a_feed_that_answers_still_reports_the_jump(self):
        # X1-04: both feeds healthy; the second feed reports its jump a step later
        state = self.start()
        self.steps(2)
        self.jump_clock(BTC)
        self.steps(1)
        self.assertEqual(state.get("admission_blocked"), "clock_gap")
        self.jump_clock(ETH)
        self.steps(1)
        self.assertEqual(state.get("admission_blocked"), "clock_gap")
        self.steps(1)
        self.assertNotIn("admission_blocked", state)

    def test_mix_the_clock_block_stays_when_no_feed_answers(self):
        state = self.start()
        self.steps(2)
        self.jump_clock()
        self.steps(1)
        self.assertEqual(state.get("admission_blocked"), "clock_gap")
        repairs = [self.break_ticker(BTC), self.break_ticker(ETH)]
        self.steps(1)
        self.assertEqual(state.get("admission_blocked"), "clock_gap")  # nothing observed cleanly after the jump
        for repair in repairs:
            repair()
        self.steps(1)
        self.assertNotIn("admission_blocked", state)

    def test_mix_the_market_mark_needs_every_contract_read(self):
        # X1-04: the ops market mark stays "all fresh"
        state = self.start()
        self.steps(2)
        health = self.fx.lab.resources[state["id"]]["health"]
        marks = []
        original = health.mark
        health.mark = lambda kind, mono, **kw: (marks.append(kind), original(kind, mono, **kw))[1]
        self.steps(1)
        self.assertIn("market", marks)
        marks.clear()
        self.break_ticker(ETH)
        self.steps(1)
        self.assertNotIn("market", marks)

    def test_mix_a_contract_with_no_active_bot_is_not_read_and_its_fault_marks_nothing(self):
        # X1-08: the ETH bot is done; a dead ETH feed must not make the BTC bots' round incomplete
        state = self.start()
        self.steps(80, until=lambda: self.b["position_btc"] and self.e["position_btc"])
        lab = self.fx.lab
        lab.stop(lab.data["generation"], state["id"], bot_id=self.e["id"])
        self.steps(40, until=lambda: self.e["status"] == "STOPPED")
        self.assertEqual(self.e["status"], "STOPPED", [e["code"] for e in state["events"][-12:]])
        self.assertTrue(self.e["accounting_reconciled"])
        self.assertFalse(state["incomplete"])
        self.break_ticker(ETH)
        calls = self.feed_api(ETH).transport
        counted = []
        self.feed_api(ETH).transport = lambda request: (counted.append(request.full_url), calls(request))[1]
        before = len(self.events("market_unavailable"))
        self.steps(5)
        self.assertEqual(state["status"], "RUNNING")
        self.assertEqual(counted, [])  # not one request of that feed
        self.assertEqual(len(self.events("market_unavailable")), before)
        self.assertFalse(state["incomplete"])
        self.assertEqual(sorted(state["frames"][-1]["markets"]), [BTC])
        self.assertTrue(self.b["position_btc"])

    def test_mix_a_closing_bot_keeps_its_contract_read(self):
        # X1-08: only STOPPED frees a contract; a bot still closing needs its feed
        state = self.start()
        self.steps(2)
        self.assertEqual(sorted(state["frames"][-1]["markets"]), [BTC, ETH])
        self.b["status"] = "CLOSING"  # not STOPPED yet: the feed is still needed
        self.steps(1)
        self.assertEqual(sorted(state["frames"][-1]["markets"]), [BTC, ETH])

    # ---- the low findings taken into RC2 ----
    def test_mix_a_failed_account_check_never_goes_on_with_another_contracts_snapshot(self):
        # X1-06: the A3 path after a failed account check checks the snapshot's contract too
        state = self.start()
        self.steps(2)
        lab = self.fx.lab
        real = lab.observe_markets

        def swapped(s, resource, stamp):
            read = real(s, resource, stamp)
            return {BTC: read[ETH], ETH: read[BTC]}
        with patch.object(lab, "observe_markets", swapped), patch.object(lab, "timed_account_check", side_effect=PreflightError("network_error")):
            self.steps(1)
        self.assertIn("ticker_instrument_mismatch", {(e["data"] or {}).get("reason") for e in state["events"][-12:]}, [e["code"] for e in state["events"][-12:]])

    def test_mix_each_contract_group_is_decided_on_its_own_snapshot(self):
        # X1-05: the journal's evidence (snapshot_sha256, maker_evidence) comes from this snapshot
        self.start()
        lab = self.fx.lab
        with patch.object(lab, "decide", wraps=lab.decide) as decide:
            self.steps(80, until=lambda: self.b["position_btc"] and self.e["position_btc"])
        seen = set()
        for call in decide.call_args_list:
            group, snapshot = call.args[2], call.args[3]
            contract = engine.instrument_for(self.state, group[0]["id"])
            self.assertEqual(snapshot["_instrument"], contract)
            seen.add(contract)
        self.assertEqual(seen, {BTC, ETH})

    def test_rolling_read_limit_is_named_in_the_market_diagnostic(self):
        # X2-02
        from trading_lab.exchange_api import market_diagnostic
        self.assertEqual(market_diagnostic(PreflightError("rolling_read_rate_limit"), Mock(stage="ticker"))["reason"], "rolling_read_rate_limit")

    def test_quality_of_a_participant_without_events_takes_only_its_own_contract(self):
        # X2-05
        from trading_lab.battle_review import quality
        state = {"rules": {"instrument": engine.MIXED, "instruments": {"a": BTC, "b": ETH}},
                 "events": [{"seq": 1, "code": "market_unavailable", "data": {"instrument": ETH}},
                            {"seq": 2, "code": "clock_gap", "data": {"instrument": BTC}},
                            {"seq": 3, "code": "clock_gap", "data": {}}]}
        self.assertEqual(quality(state, [], "a")["gap_event_seqs"], [2, 3])
        self.assertEqual(quality(state, [], "b")["gap_event_seqs"], [1, 3])
        self.assertEqual(quality({"rules": {"instrument": BTC}, "events": state["events"]}, [], "a")["gap_event_seqs"], [1, 2, 3])

    def test_mixed_archive_refuses_one_snapshot_path(self):
        # X2-06: never empty market members that pass verify
        from pathlib import Path
        from trading_lab.arena_strategies import ArenaError
        from trading_lab.battle_export import create_archive
        state = {"rules": {"instrument": engine.MIXED, "instruments": {"a": BTC, "b": ETH}}, "mode": "OKX_LIVE"}
        for path in (None, Path("one.jsonl")):
            with self.assertRaisesRegex(ArenaError, "кожним інструментом"):
                create_archive(state, snapshots_path=path)


class MixedRestingEntry(unittest.TestCase):
    """X1-02: a resting post-only ETH entry beside a BTC V1 bot; the fake accounts keep it live."""
    def setUp(self):
        self.fx = fixture.ExchangeTests(methodName="runTest")
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        fx, shop = self.fx, self.fx.workshop
        fx.lab.jev_factory = Mock(side_effect=AssertionError("No Jev construction for RULES_ONLY"))
        self.btc, self.eth = fx.fakes["arena-a"], fx.fakes["arena-b"]
        for fake in (self.btc, self.eth):
            fake.__class__ = test_post_only.RestingFake
        self.eth.use(fixture.ETH_SPEC, 3000, 0)
        self.btc.serve(fixture.ETH_SPEC, 3000, 0)
        for fake in (self.btc, self.eth):
            fake.strict = True
        eth_version = shop.add_version(shop.data["generation"], eth_payload(order=test_post_only.MAKER, size_pct=20, max_hold_seconds=3600))["id"]
        for version, dataset in ((fx.version, "synthetic-suite-v2"), (eth_version, strategy_training.SYNTHETIC["id"])):
            shop.train(shop.data["generation"], version, dataset, 100)
            shop.freeze(shop.data["generation"], version)
            shop.promote(shop.data["generation"], version)
        self.ids = [fx.version, eth_version]

    start = MixedSeason.start
    steps = MixedSeason.steps

    def test_a_contract_block_cancels_its_resting_entry_at_once(self):
        state = self.start()
        self.steps(80, until=lambda: any(not o["reduceOnly"] for o in self.eth.orders.values()))
        order = next(o for o in self.eth.orders.values() if not o["reduceOnly"])
        self.assertEqual((order["ordType"], order["state"]), ("post_only", "live"))
        cancels = lambda: [r for r in self.eth.requests if urlsplit(r.full_url).path == "/api/v5/trade/cancel-order"]
        self.steps(2)
        self.assertEqual((order["state"], cancels()), ("live", []))  # rests while admitted
        state["admission_blocked_by_instrument"] = {ETH: "test_block"}
        self.steps(1)
        self.assertEqual(len(cancels()), 1, "the contract block did not cancel the resting ETH entry")
        self.assertEqual(order["state"], "canceled")
        self.assertNotIn("admission_blocked", state)
        self.assertEqual((self.btc.violations, self.eth.violations), ([], []))


if __name__ == "__main__":
    unittest.main()
