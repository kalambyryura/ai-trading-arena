"""R2.1.12 (MIX, plan check p. 19): the golden run. Two one-contract fake seasons, with a fixed clock, fixed uuids and
the code hashes held constant, must give the digests the R2.1.12 tree (8fa65ad; R2.1.11 d3be30e until R2.1.13 froze the training and arena hashes too) gave: the state, the event and snapshot
journals, the review bundle and every ZIP member. A BTC season of two bots on a 2 h window (sparse frames, a market
fault) and an ETH schema-4 season (feature_book). Digests are canonical (sorted keys) where Python's hash seed would
reorder bytes. Temp roots and the fake exchange only. Pins: py -3.12 -X utf8 -B tests/test_r2112_golden.py --pins"""
from collections import namedtuple
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime as _datetime, timezone as _timezone
import hashlib
import itertools
import json
import sys
import unittest
from unittest.mock import Mock, patch
import uuid
import zipfile

import test_exchange_lab as fixture
import test_order_outcomes as outcomes
from trading_lab import exchange_execution as policy

PINS = {
 "btc_two_bots": {
  "bundle": "6204fc7c388efe5e8604154d9ebf096a914c6e274f0c5ed0ce3be804b72cdf11",
  "events": 77,
  "frame_keys": [
   "at",
   "bots",
   "event_seq",
   "market"
  ],
  "frame_markets": [
   "mid",
   "none"
  ],
  "frames": 12,
  "journals": {
   "ID.snapshots.jsonl": "83cc1485597bf44eef5eb154e6a1f6fb77c723e5f692669429eb56c32409fdfe"
  },
  "state": "603f8cc95fb227dfc3fee61dd3a40bb3541e651bbe9c5776f6f7744a3ca8fcd4",
  "zip": {
   "README_UA.txt": "8d960a6a087e47bceaee29f6df2c449673bcea9907d4a5521a19cccef4c92f6e",
   "battle-replay.json": "f2b149ebfa723e0ad9ab9639678c901ebcdd17f0755867648ffb5b1271cf28ad",
   "battle-review.json": "8eabd1ee8aa86ad87f658d67adc90d90a4f06cd6f6f40be70b6904d0fda32b8a",
   "manifest-receipt.json": [
    "manifest_bytes",
    "manifest_sha256",
    "schema"
   ],
   "manifest.json": "46ac72c79f49a31e203c270c1a21335811877f6d527c73877640f25b02f52eae",
   "market.jsonl": "80db4d96a2eab53c3e2bab70d66b2402cbeea07c309a04c77f779f6f01bbc98f",
   "match-settings.json": "9899167d07494323bb51a09774ac7148582417700fedfbb6f0889392ca4afbf7",
   "strategies.json": "1b0211ca0a278ea95213e21970990f72b40fc1d8592f7579bd1c4c713e35ab98"
  }
 },
 "eth_schema4": {
  "bundle": "1968aa10cb3a1916bae6e0560ed0f9dc753708c98e248a765f304bf63f0dc661",
  "events": 123,
  "frame_keys": [
   "at",
   "bots",
   "event_seq",
   "market"
  ],
  "frame_markets": [
   "mid",
   "none"
  ],
  "frames": 49,
  "journals": {
   "ID.snapshots.jsonl": "21a1cd04b987f4f20669d16cf308ed6980f1c274cb7251dc2e9051e5cff6447e"
  },
  "state": "463d01c6456d682c4ffc54e53c464ee6c84d7722872e40470606b7dae39eb672",
  "zip": {
   "README_UA.txt": "8d960a6a087e47bceaee29f6df2c449673bcea9907d4a5521a19cccef4c92f6e",
   "battle-replay.json": "b7d64556b57195a793f3dcfe22189d653d7918aeabe4c9db022eee19bb2aa591",
   "battle-review.json": "d0910e99513021e45f786bf4810ec977934d3a0089146dec10d0aec0a19e2d9d",
   "manifest-receipt.json": [
    "manifest_bytes",
    "manifest_sha256",
    "schema"
   ],
   "manifest.json": "91a59111f38011ab2c09705d7f40870f67f5e8b6fe96e84eca5f5a64f0c6a161",
   "market.jsonl": "4a188f302a1037f7bc931307d415e6117355670b2d61254adb6cf1b006a086f8",
   "match-settings.json": "f37d09e914b7674bcdc68c9f8249f66e035b33641fa08bad6c3d73e82f9aaeec",
   "strategies.json": "11bda05d644979c9577ea2fee3007f4f9555593535564108dcd897b0fae3ab94"
  }
 }
}  # computed by the R2.1.12 tree (git archive 8fa65ad) with --pins and the hashes frozen below; the same on this tree


class FrozenDatetime(_datetime):
    @classmethod
    def now(cls, tz=None):
        return _datetime(2026, 11, 5, 9, 0, 0, tzinfo=tz or _timezone.utc)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return sha(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"))


def frozen():
    """The time, ids, free space and code hashes that differ between two runs or two trees by design."""
    import shutil
    import trading_lab.arena as arena
    import trading_lab.arena_strategies as recipes
    import trading_lab.strategy_training as training
    import trading_lab.battle_export as export
    import trading_lab.exchange_lab as lab
    import trading_lab.workshop as workshop
    counter = itertools.count(1)
    stack = ExitStack()
    stack.enter_context(patch.object(uuid, "uuid4", lambda: uuid.UUID(int=next(counter))))
    stack.enter_context(patch.object(shutil, "disk_usage", lambda path: namedtuple("usage", "total used free")(10**12, 0, 10**11)))
    for module in (workshop, recipes, export):
        stack.enter_context(patch.object(module, "datetime", FrozenDatetime))
    stack.enter_context(patch.object(lab, "LOADED_HASH", "c" * 64))
    stack.enter_context(patch.object(lab, "source_hash", lambda *a, **k: "c" * 64))
    stack.enter_context(patch.object(export, "exporter_hash", lambda *a, **k: "e" * 64))
    # R2.1.13 changes the training and arena hashes by design: the state's qualification hash and the manifest's code_hashes
    stack.enter_context(patch.object(training, "LOADED_CODE_HASH", "a" * 64))
    stack.enter_context(patch.object(training, "source_hash", lambda *a, **k: "a" * 64))
    stack.enter_context(patch.object(arena, "LOADED_SOURCE_HASH", "b" * 64))
    stack.enter_context(patch.object(arena, "arena_source_hash", lambda *a, **k: "b" * 64))
    return stack


def digest(lab, state):
    from trading_lab.battle_export import create_archive
    from trading_lab.battle_review import build_review
    from trading_lab.match_journal import journal_copy
    lab.save(state)
    journal = journal_copy(lab, state["id"])
    snapshots = lab.path / (state["id"] + ".snapshots.jsonl")
    out = {"state": canonical(journal), "bundle": build_review(journal)["integrity"]["bundle_sha256"],
           "journals": {p.name.replace(state["id"], "ID"): sha(p.read_bytes()) for p in sorted(lab.path.glob(state["id"] + ".*.jsonl"))},
           "frames": len(journal["frames"]), "frame_markets": sorted({"mid" if f.get("market") else "none" for f in journal["frames"]}),
           "frame_keys": sorted({k for f in journal["frames"] for k in f}), "events": len(journal["events"])}
    with create_archive(journal, snapshots_path=snapshots if snapshots.exists() else None) as stream:
        with zipfile.ZipFile(stream) as archive:
            members = {}
            for name in sorted(archive.namelist()):
                data = archive.read(name)
                if name in {"manifest.json", "manifest-receipt.json"}:
                    value = json.loads(data)
                    if name == "manifest.json":
                        value["files"] = [{k: v for k, v in row.items() if k != "sha256"} for row in value["files"]]
                    members[name] = canonical(value) if name == "manifest.json" else sorted(value)
                elif name.endswith(".json"):
                    members[name] = canonical(json.loads(data))
                else:
                    members[name] = sha(data)
    out["zip"] = members
    return out


def btc_two_bots():
    """RULES_ONLY Live, two BTC V1 recipes on two subaccounts, a 2 h admission window (sparse frames), one market fault."""
    from trading_lab.arena_strategies import catalogue_defaults
    from trading_lab.strategy_contract import payload_of
    fx = fixture.ExchangeTests(methodName="runTest")
    fx.setUp()
    try:
        fx.lab.jev_factory = Mock(side_effect=AssertionError("No Jev for RULES_ONLY"))
        shop = fx.workshop
        second = shop.add_version(shop.data["generation"], payload_of(catalogue_defaults()[1]))["id"]
        for version in (fx.version, second):
            shop.train(shop.data["generation"], version, "synthetic-suite-v2", 100)
            shop.freeze(shop.data["generation"], version)
            shop.promote(shop.data["generation"], version)
        lab = fx.lab
        result = lab.start(lab.data["generation"], "arena_live", [fx.version, second], ["arena-a", "arena-b"], 100, 7200,
                           execution_mode=policy.RULES_ONLY)
        state = lab.load(result["id"])
        state.update(fixture_only=True, evidence_kind="SYNTHETIC")
        lab.prepare(state, lab.chosen_profiles(["arena-a", "arena-b"]), False)
        for _ in range(8):
            lab.step(state); fx.now += 5
        feed = lab.resources[state["id"]]["feed"]
        api = getattr(feed, "base", feed).api
        original = api.transport
        api.transport = lambda request: (_ for _ in ()).throw(OSError("fictional outage")) if "/market/candles" in request.full_url else original(request)
        lab.step(state); fx.now += 5
        api.transport = original
        for _ in range(8):
            lab.step(state); fx.now += 5
        lab.stop(lab.data["generation"], state["id"])
        for _ in range(40):
            lab.step(state); fx.now += 5
            if state["status"] in {"DONE", "PAUSED"}:
                break
        return digest(lab, state)
    finally:
        fx.tearDown()


class EthTaker(outcomes.SeasonCase):
    TAKER = True

    def runTest(self):
        pass


def eth_schema4():
    case = EthTaker()
    case.setUp()
    try:
        case.run_season(until=lambda: case.bot["position_btc"] != 0 and case.bot.get("protection", {}).get("status") == "ARMED", limit=100)
        outcomes.fire_stop(case.fx, case.fake)
        case.run_season(limit=40)
        lab = case.fx.lab
        if case.state["status"] not in {"DONE", "PAUSED"}:
            lab.stop(lab.data["generation"], case.state["id"])
            case.run_season(limit=40)
        return digest(lab, case.state)
    finally:
        case.tearDown()


def scenarios():
    with frozen():
        return {"btc_two_bots": btc_two_bots(), "eth_schema4": eth_schema4()}


class GoldenRun(unittest.TestCase):
    def test_one_contract_seasons_write_what_r2111_wrote(self):
        found = scenarios()
        for name, pin in PINS.items():
            with self.subTest(name):
                self.assertEqual(found[name], pin)

    def test_the_scenarios_cover_what_they_pin(self):
        for name, pin in PINS.items():
            with self.subTest(name):
                self.assertEqual(pin["frame_keys"], ["at", "bots", "event_seq", "market"])  # no "markets" outside a mixed season
                self.assertIn("mid", pin["frame_markets"])  # (3): successful steps keep market == {mid}
        self.assertIn("market.jsonl", PINS["btc_two_bots"]["zip"])
        self.assertIn("ID.snapshots.jsonl", PINS["btc_two_bots"]["journals"])


if __name__ == "__main__" and "--pins" in sys.argv:
    print(json.dumps(scenarios(), indent=1, sort_keys=True))
elif __name__ == "__main__":
    unittest.main()
