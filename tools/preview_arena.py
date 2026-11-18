"""Disposable UI fixture. Synthetic market only; live launcher/balance access prohibited."""
import argparse
import math
import json
import re
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trading_lab.arena import Arena
from trading_lab.arena_jev import SharedJevGate
from trading_lab.web_panel import Panel, Server
from trading_lab.exchange_lab import ExchangeLab


def synthetic(stamp, origin):
    elapsed = stamp - origin
    price = 84000 * (1 + .006 * math.sin(elapsed / 60))
    trend = math.cos(elapsed / 60)
    return {"at": stamp, "server_time": stamp, "quote_ts": stamp * 1000, "bid": price, "ask": price + .1,
            "mid": price + .05, "bid_btc": 2, "ask_btc": 2, "lot_btc": .0001, "min_btc": .0001,
            "features": {"sma_gap_bps": 30 * trend, "return_5m_bps": 40 * trend,
                         "return_1m_bps": 5 * trend, "rsi_14": 50 + 30 * math.sin(elapsed / 60),
                         "range_position_20": 50, "volume_ratio_5": 1.2, "spread_bps": .012},
            "funding_rate": .0001, "funding_at": origin + 3600, "funding_time_ms": int((origin + 3600) * 1000),
            "sha256": "SYNTHETIC-UI-FIXTURE", "raw": {"synthetic": True}}


def forbidden(*args, **kwargs):
    raise RuntimeError("Live execution and private balance are not available in this fixture.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--seconds", type=int, default=900)
    parser.add_argument("--v6-proof", action="store_true", help="Seed fake Demo coverage and two fake Live positions in DRAINING")
    parser.add_argument("--demo-mechanics-proof", action="store_true", help="Completed pinned short/partial/tail on a fake Demo; HTTP execution forbidden")
    parser.add_argument("--coliseum-proof", type=int, choices=(2, 6, 12, 24), help="Show a fictional display-only coliseum roster; never expands executor participants")
    parser.add_argument("--mixed-form-proof", action="store_true", help="R2.1.12 (D151): the Live form with the five Live profile ids of the other PC and five ready recipes, four BTC and one ETH; fake accounts")
    args = parser.parse_args()
    if args.demo_mechanics_proof and (args.v6_proof or args.coliseum_proof):
        parser.error("Choose one proof fixture.")
    with tempfile.TemporaryDirectory(prefix="arena-ui-fixture-") as directory:
        root = Path(directory)
        shutil.copytree(Path(__file__).resolve().parents[1] / "web", root / "web")
        for name in ("arena.html", "workshop.html"):
            html = root / "web" / name
            html.write_text(re.sub(r"(<body[^>]*>)", r'\1<p class="fixture-banner">UI TEST · СИНТЕТИЧНИЙ РИНОК · API-МОДЕЛІ ЗАМІНЕНО ЗАГЛУШКОЮ</p>', html.read_text(encoding="utf-8"), count=1), encoding="utf-8")
        with (root / "web" / "arena.css").open("a", encoding="utf-8") as file:
            file.write("\n.fixture-banner{margin:0;padding:10px;text-align:center;background:#ffdf8d;color:#513b18;font-weight:700;font-size:14px;position:sticky;top:0;z-index:100}")
        origin = time.time() - 340
        def fake_jev(root):
            def transport(body, key):
                return {"answers": {key: {"type": "choice", "choice": "proceed", "confidence": .99}
                                    for key in json.loads(body)["questions"]}, "usage": {"cost": 0}}
            return SharedJevGate(root, transport=transport, key_reader=lambda root: "UI-FAKE-NO-KEY")
        arena = Arena(root, clock=lambda: origin, background=False, jev_factory=fake_jev)
        arena.start(0, ["sample-trend", "sample-rebound", "sample-short"], 100, 300)
        for i in range(65):
            arena.advance(synthetic(origin + 5 * i, origin))
            if arena.session["status"] == "DONE":
                break
        # UI-created seasons can use only this synthetic provider, never the public feed or keys.
        class Feed:
            def snapshot(self):
                return synthetic(time.time(), origin)
        arena.background = True
        arena.feed_factory = Feed
        arena.clock = time.time
        def exchange_fixture(root, workshop, arena):
            # Stateful exchange simulator from offline tests. Only temp fake tokens;
            # normal production ExchangeAPI transport and actual account files are never used.
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
            from test_exchange_lab import FakeExchange
            from trading_lab.exchange_api import ExchangeFeed
            from trading_lab.strategy_contract import payload_of
            from trading_lab.arena_strategies import catalogue_defaults
            fixture_time = [time.time() - 320]
            fixture_clock = (lambda: fixture_time[0]) if args.v6_proof or args.demo_mechanics_proof else time.time
            fakes = {"demo": FakeExchange(fixture_clock), "arena-a": FakeExchange(fixture_clock, "102"),
                     "arena-b": FakeExchange(fixture_clock, "103")}
            extra = ("arena-claude-opus", "arena-claude-sonet", "arena-yurii") if args.mixed_form_proof else ()
            for offset, name in enumerate(extra):
                fakes[name] = FakeExchange(fixture_clock, str(104 + offset))
            for name, environment in (("demo", "demo"), ("arena-a", "live"), ("arena-b", "live"), *((n, "live") for n in extra)):
                (root / (".env.okx." + name + ".local")).write_text("OKX_SITE=global\nOKX_ENV=" + environment +
                    "\nOKX_API_KEY=FIXTURE\nOKX_API_SECRET=FIXTURE\nOKX_API_PASSPHRASE=FIXTURE\n", encoding="utf-8")
            if args.mixed_form_proof:  # the temp root's own profile list: the ids the other PC has (team/ops/OTHER_PC_UA.md)
                from test_exchange_lab import ETH_SPEC
                fakes["arena-claude-sonet"].use(ETH_SPEC, 3000, 1)
                (root / "LocalSettings").mkdir(exist_ok=True)
                (root / "LocalSettings" / "exchange_accounts.json").write_text(json.dumps({"profiles": [
                    {"id": "demo", "label": "Майстерня · OKX Demo", "environment": "demo", "settings_file": ".env.okx.demo.local"},
                    *({"id": name, "label": "Арена · " + name, "environment": "live", "settings_file": ".env.okx." + name + ".local"}
                      for name in ("arena-a", "arena-b", *extra))]}), encoding="utf-8")
            def gate(context, ledger):
                return SharedJevGate(root, ledger=ledger, mode="OKX_DEMO" if context == "workshop_demo" else "OKX_LIVE",
                    transport=lambda body,key: {"usage":{"cost":0}, "answers":{k:{"type":"choice","choice":"proceed","confidence":.99}
                                               for k in json.loads(body)["questions"]}}, key_reader=lambda root: "UI-FAKE-NO-KEY")
            lab = ExchangeLab(root, workshop, arena, api_factory=lambda p: fakes[p["id"]].api(p["environment"]),
                feed_factory=lambda api: ExchangeFeed(api, clock=fixture_clock), jev_factory=gate,
                clock=fixture_clock, monotonic=fixture_clock if args.demo_mechanics_proof else None,
                background=not (args.v6_proof or args.demo_mechanics_proof))
            try:
                workshop.add_version(workshop.data["generation"], payload_of(catalogue_defaults()[0]))
                if args.mixed_form_proof:  # five ready recipes: free synthetic qualification, this temp root only
                    sys.path.insert(0, str(Path(__file__).resolve().parent))
                    from test_exchange_eth import eth_payload
                    from trading_lab import strategy_training
                    samples = [payload_of(catalogue_defaults()[i]) for i in (0, 1, 2)]
                    for payload in [*samples, {**samples[0], "name": "Четвертий BTC"}, eth_payload()]:
                        version = workshop.add_version(workshop.data["generation"], payload)["id"]
                        workshop.train(workshop.data["generation"], version, strategy_training.SYNTHETIC["id"], 100)
                        workshop.freeze(workshop.data["generation"], version)
                        workshop.promote(workshop.data["generation"], version)
                if args.demo_mechanics_proof:
                    from trading_lab.demo_execution import MODE
                    from trading_lab.demo_v8_review import review
                    recipe = json.loads((Path(__file__).resolve().parents[1] /
                        "03_For_Client_Deliverables/V8_DEMO_SHORT_PARTIAL_TAIL.json").read_text(encoding="utf-8"))
                    version = workshop.add_version(workshop.data["generation"], recipe)["id"]
                    lab.jev_factory = forbidden
                    accepted = lab.start(lab.data["generation"], "workshop_demo", [version], ["demo"], 100, 300,
                                         execution_mode=MODE)
                    state = lab.load(accepted["id"])
                    state.update(fixture_only=True, evidence_kind="SYNTHETIC")
                    lab.prepare(state, lab.chosen_profiles(["demo"]), False)
                    start = fixture_time[0]
                    for offset in range(0, 446, 5):
                        fixture_time[0] = start + offset; lab.step(state)
                    if state["status"] != "DONE" or not review(state)["all_primary_observed"]:
                        raise RuntimeError("Fake mechanics probe did not cover all operating stages.")
                    lab.start = lab.stop = lab.resume = forbidden
                if args.v6_proof:
                    from trading_lab.cycle_probe import template
                    demo_version = workshop.add_version(workshop.data["generation"], template())["id"]
                    accepted = lab.start(lab.data["generation"], "workshop_demo", [demo_version], ["demo"], 100, 300)
                    state = lab.load(accepted["id"])
                    state["fixture_only"] = True
                    lab.prepare(state, lab.chosen_profiles(["demo"]), False)
                    start = fixture_time[0]
                    for offset in (0, 30, 60, 120, 125):
                        fixture_time[0] = start + offset; lab.step(state)
                    if state["status"] != "DONE":
                        raise RuntimeError("Fake Demo fixture failed to close.")
                    workshop.record_exchange_trial(state)
                    fixture_time[0] = time.time() - 320
                    ids = []
                    for index in range(2):
                        payload = payload_of(catalogue_defaults()[index])
                        payload["name"] = "Синтетичний боєць " + str(index + 1)
                        payload["rules"].update(max_hold_seconds=1200, max_entries=1,
                                                stop_loss_bps=500, take_profit_bps=1000)
                        payload["rules"]["entry"] = {"mode": "all", "conditions": [
                            {"feature": "spread_bps", "op": "lte", "value": 5}]}
                        payload["rules"]["exit"] = {"mode": "any", "conditions": []}
                        ids.append(arena.add_strategy(arena.data["generation"], payload)["id"])
                    accounts = ["arena-a", "arena-b"]
                    accepted = lab.start(lab.data["generation"], "arena_live", ids, accounts, 100, 300)
                    state = lab.load(accepted["id"])
                    state["fixture_only"] = True
                    lab.prepare(state, lab.chosen_profiles(accounts), False)
                    start = fixture_time[0]
                    for offset in range(0, 301, 30):
                        fixture_time[0] = start + offset; lab.step(state)
                    if state["status"] != "DRAINING" or not all(b["position_btc"] for b in state["bots"][:-1]):
                        details = str([(b["status"], b["position_btc"], b.get("last_reason")) for b in state["bots"][:-1]])
                        lab.close(); arena.close()
                        raise RuntimeError("Fake Live fixture failed: " + state["status"] + " " + details)
                    def refresh_fake_positions():
                        while not lab.closed and state["status"] not in {"DONE", "PAUSED"}:
                            fixture_time[0] = time.time()
                            lab.step(state)
                            time.sleep(5)
                    threading.Thread(target=refresh_fake_positions, daemon=True, name="fake-v6-display").start()
            except BaseException:
                lab.close(); arena.close()
                raise
            if args.coliseum_proof:
                from tools.coliseum_fixture import ColiseumFixture
                return ColiseumFixture(lab, args.coliseum_proof)
            return lab
        with Server(Panel(root, launcher=forbidden, balance_reader=forbidden), args.port, arena=arena,
                    exchange_factory=exchange_fixture) as server:
            timer = threading.Timer(args.seconds, server.shutdown)
            timer.daemon = True; timer.start()
            print(server.origin + "/arena — disposable synthetic fixture", flush=True)
            try:
                server.serve_forever(poll_interval=.2)
            except KeyboardInterrupt:
                pass
            finally:
                timer.cancel()


if __name__ == "__main__":
    main()
