"""R1 full F1, R2 (b): schema-4 recipes (taker or D096 post-only, BTC or ETH) in the exact PAPER engine against
tools/team_eval_v4.py mode="official".

Both sides read the same DEV candles of the recipe's instrument (the instrument JSON next to this tool, unless
--instrument names another; its instId and the store's catalogue must be the recipe's instrument). The engine runs its
own 24 h seasons (20 entries, 10 % drawdown stop, exit tail) on the 5 s model tapes of tools/okx_tape_model.py, the
code path of tools/team_eval.py; every snapshot carries the feature book of its D106-visible bar: by default the F2 book (trading_lab/strategy_v4_book.py) fed minute by
minute from the season's warm-up on, or with --features arrays the evaluator's vectorised arrays (F1 alone). The
evaluator simulates the same windows in mode="official" with the same entry cap and risk stop. For a post-only
recipe the exit tail also covers the order timeout, because an order placed before the cutoff may still fill after it.

A pair matches when the entry time, exit time, side and exit reason agree and the entry and exit prices differ by at
most 0.5 bps. The one expected difference is a trade where the evaluator applied its ambiguous-minute rule (both the
stop and the target reachable inside one minute of candles): the engine sees the order of prices on its quote path
and exits where the path goes first. Such trades are listed apart and are not counted as mismatches.

Post-only orders are compared one by one as well: per season by the time the order reached the book, with the same
outcome (rejected because it would cross, unfilled until the timeout, filled at the same quote) and limits within
0.5 bps; an order on one side only or with another outcome is an order mismatch. The maker counters of both sides are
reported next to each other. Trades and orders are paired within their own season (an order placed before the cutoff
may fill inside the next season's admission).

  py -3.12 -X utf8 -B tools/v4_conformance.py --recipe R.json --start 2025-03-01 --days 7 --out report.json
  py -3.12 -X utf8 -B tools/v4_conformance.py --recipe E.json --store data/okx/ETH-USDT-SWAP --start ...
"""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _path in (str(ROOT), str(HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)
import numpy as np  # noqa: E402

import team_data  # noqa: E402
import team_eval  # noqa: E402
import team_eval_v4  # noqa: E402
from trading_lab import arena_engine, strategy_v4, strategy_v4_book, strategy_v4_runtime as v4rt  # noqa: E402

PRICE_BPS = 0.5
TAIL_EXTRA = 420  # seconds after the longest hold, as simulate_exact_windows
FIRST_TAIL = 7200  # a season DONE within this tail gives the same events as with the full tail (team_eval.evaluate_window)
REJECTED = {"post_only_would_cross": "rejected_crossing", "minimum_lot": "too_small", "post_only_limit_unknown": "limit_unknown",
            "risk_veto": "risk_veto"}
NO_ORDER = ("too_small", "limit_unknown")  # HOLD before the book on both sides; counted, not paired


def utc(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def feature_books(strategy, series):
    """Reference arrays of every feature the recipe reads, indexed by swap minute (value at that minute's close)."""
    cache = {}
    return {key: team_eval_v4.feature_array(spec, series, cache) for key, spec in strategy_v4.required_features(strategy["rules"]).items()}


def attach_books(tape, series, arrays):
    """snapshot["v4_features"] of the bar visible under D106 (close + 5 s); a hole leaves the book behind the clock."""
    closes = series["ts"] // 1000 + 60
    for snapshot in tape:
        b = int(np.searchsorted(closes + v4rt.VISIBILITY_SECONDS, snapshot["at"], side="right")) - 1
        if b < 0:
            continue
        snapshot["v4_features"] = {"bar_close_ms": int(closes[b]) * 1000,
                                   "values": {k: (float(a[b]) if math.isfinite(a[b]) else None) for k, a in arrays.items()}}
    return tape


def feature_book(strategy, series, funding, start_s):
    """A fresh F2 book per season, as Live builds one at the season start; its inputs are the evaluator's minutes."""
    book = strategy_v4_book.FeatureBook(strategy_v4.required_features(strategy["rules"]).values())
    first = int(np.searchsorted(series["ts"], start_s * 1000 - book.warmup_ms))
    filled = series.get("filled")
    rows = [(int(series["ts"][k]), float(series["o"][k]), float(series["h"][k]), float(series["l"][k]), float(series["c"][k]),
             float(series["v"][k])) for k in range(first, len(series["ts"])) if filled is None or not filled[k]]
    secondary = {kind: {int(t): float(c) for t, c in zip(series["ts"][first:], series[kind + "_c"][first:]) if math.isfinite(c)}
                 for kind in ("index", "mark")}
    return book, rows, secondary, [(t, r) for t, r in funding]


def engine_orders(events):
    """The season's post-only orders from the engine's events, in the evaluator's terms (team_eval_v4 Run.orders)."""
    orders, resting = [], None
    for event in events:
        code, data = event["code"], event["data"]
        if code not in ("entry_order_rejected", "entry_order_placed", "entry_order_expired", "entry_order_cancelled", "paper_entry_fill"):
            continue
        at = utc(team_eval.stamp_of(event))
        if code == "entry_order_rejected":
            orders.append({"placed_at": at, "limit_px": data.get("limit_usdt"), "outcome": REJECTED.get(data.get("reason"), data.get("reason"))})
        elif code == "entry_order_placed":
            resting = {"placed_at": at, "limit_px": data["limit_usdt"], "outcome": "resting_at_end"}
            orders.append(resting)
        elif resting is not None and (code != "paper_entry_fill" or data.get("order_type") == "post_only"):
            resting.update({"paper_entry_fill": {"outcome": "filled", "filled_at": at}, "entry_order_expired": {"outcome": "unfilled"},
                            "entry_order_cancelled": {"outcome": "cancelled"}}[code])
            resting = None
    return orders


def maker_counts(orders):
    """Orders that reached the book (incl. those it refused as crossing) and their outcomes; HOLDs before the book apart."""
    counts = {"orders": 0, "rejected_crossing": 0, "unfilled": 0, "filled": 0, "cancelled": 0, "resting_at_end": 0, "other_rejections": 0,
              "too_small": 0, "limit_unknown": 0}
    for order in orders:
        outcome = order["outcome"]
        if outcome in NO_ORDER:
            counts[outcome] += 1
            continue
        counts["orders"] += 1
        counts[outcome if outcome in counts else "other_rejections"] += 1
    return counts


def engine_window(recipe, rows, funding, instrument_path, series, arrays, start_s, admission, tail, strategy=None):
    store = team_eval.row_store(rows, funding, instrument_path)
    store.precise_last = True  # the engine's post-only fill test reads the evaluator's exact path price (D096)
    tape = store.tape(start_s, start_s + admission + tail)
    if arrays is None:
        book, minutes, secondary, settlements = feature_book(strategy, series, funding, start_s)
        tape = strategy_v4_book.attach(tape, book, minutes, secondary, settlements)
    else:
        tape = attach_books(tape, series, arrays)
    state = arena_engine.new_season([recipe], team_eval.CAPITAL, admission, tape[0]["at"], "v4-conformance")
    for snapshot in tape:
        if state["status"] not in {"RUNNING", "DRAINING", "CLOSING"}:
            break
        arena_engine.tick(state, snapshot)
    public = arena_engine.public_season(state)
    trades, open_cycle = team_eval.trades_from_events(public["events"], utc(start_s)[:10])
    errors = sorted({e["code"] for e in public["events"] if e["code"] in team_eval.ERROR_EVENTS})
    return trades, engine_orders(public["events"]), {"status": state["status"], "open_at_end": open_cycle is not None, "errors": errors}


def engine_season(recipe, rows, funding, instrument_path, series, arrays, start_s, admission, tail, strategy):
    """engine_window on a short tail first; only a season still running at its end is replayed with the full tail."""
    used = min(tail, FIRST_TAIL)
    while True:
        trades, orders, info = engine_window(recipe, rows, funding, instrument_path, series, arrays, start_s, admission, used, strategy)
        if info["status"] not in {"RUNNING", "DRAINING", "CLOSING"} or used >= tail:
            return trades, orders, {**info, "tail_used_seconds": used}
        used = tail


def bps(a, b):
    return abs(a / b - 1) * 10000


def compare(engine, evaluator):
    """Pairs by season and entry time; a pair matches on times, side, reason and prices within PRICE_BPS."""
    by_entry = {(t.get("season"), t["entry_at"]): t for t in evaluator}
    matched, mismatched, ambiguous, worst = [], [], [], 0.0
    for trade in engine:
        other = by_entry.pop((trade.get("season"), trade["entry_at"]), None)
        if other is None:
            mismatched.append({"engine_only": trade["entry_at"], "season": trade.get("season")})
            continue
        prices = [bps(trade["executions"][0]["price_usdt"], other["executions"][0]["price_usdt"]),
                  bps(trade["executions"][-1]["price_usdt"], other["executions"][-1]["price_usdt"])]
        same = (trade["exit_at"] == other["exit_at"] and trade["side"] == other["side"]
                and trade["exit_reason"] == other["exit_reason"] and max(prices) <= PRICE_BPS)
        row = {"season": trade.get("season"), "entry_at": trade["entry_at"], "side": trade["side"], "engine": [trade["exit_at"], trade["exit_reason"], round(trade["net_usdt"], 8)],
               "evaluator": [other["exit_at"], other["exit_reason"], round(other["net_usdt"], 8)],
               "price_bps": [round(p, 4) for p in prices]}
        if same:
            worst = max(worst, *prices)
            matched.append(row)
        elif other.get("ambiguous_minute"):
            ambiguous.append(row)
        else:
            mismatched.append(row)
    mismatched += [{"evaluator_only": entry, "season": season} for season, entry in by_entry]
    return matched, mismatched, ambiguous, worst


def compare_orders(engine, evaluator):
    """Post-only orders paired by season and placement time: same outcome, same fill quote, limits within PRICE_BPS."""
    by_placement = {(o["season"], o["placed_at"]): o for o in evaluator}
    matched, mismatched, worst = 0, [], 0.0
    for order in engine:
        if order["outcome"] in NO_ORDER:
            continue
        other = by_placement.pop((order["season"], order["placed_at"]), None)
        if other is None:
            mismatched.append({"engine_only": order})
            continue
        limit = bps(order["limit_px"], other["limit_px"]) if order["limit_px"] and other["limit_px"] else math.inf
        if order["outcome"] == other["outcome"] and order.get("filled_at") == other.get("filled_at") and limit <= PRICE_BPS:
            matched += 1
            worst = max(worst, limit)
        else:
            mismatched.append({"engine": order, "evaluator": other, "limit_bps": round(limit, 4) if math.isfinite(limit) else None})
    mismatched += [{"evaluator_only": order} for order in by_placement.values()]
    return matched, mismatched, worst


def evaluator_counts(counters, orders):
    """The evaluator's maker counters in maker_counts terms (its orders include the ones refused as crossing)."""
    counts = maker_counts(orders)
    if (counts["orders"], counts["rejected_crossing"], counts["unfilled"]) != (
            counters["maker_orders"], counters["maker_rejected_crossing"], counters["maker_unfilled"]):
        raise SystemExit(f"evaluator orders and counters disagree: {counts} {counters}")
    return {**counts, "too_small": counters["too_small"]}


def add_counts(total, counts):
    for key, value in counts.items():
        total[key] = total.get(key, 0) + value
    return total


def instrument_for(strategy, instrument_path=None):
    """The instrument JSON of the recipe's instrument (tools/okx_instrument_<inst>.json) unless one is named."""
    path = Path(instrument_path) if instrument_path else team_eval.instrument_file(strategy["instrument"])
    spec = json.loads(path.read_text(encoding="utf-8"))[0]
    if spec.get("instId") != strategy["instrument"]:
        raise SystemExit(f"instrument JSON {path.name} is {spec.get('instId')}, the recipe trades {strategy['instrument']}")
    return path, spec


def run(recipe_path, start, days, store, instrument_path=None, admission=86400, features="book"):
    payload = json.loads(Path(recipe_path).read_text(encoding="utf-8-sig"))
    strategy = strategy_v4.validate_payload(payload)
    recipe = v4rt.engine_recipe(strategy)
    instrument_path, spec = instrument_for(strategy, instrument_path)
    if team_data.read_catalog(store).get("instrument") != strategy["instrument"]:
        raise SystemExit(f"the store {store} is not catalogued as {strategy['instrument']}")
    rules = strategy["rules"]
    post_only = rules["order"]["type"] == "post_only"
    tail = rules["max_hold_seconds"] + (rules["order"]["timeout_seconds"] if post_only else 0) + TAIL_EXTRA
    first = team_eval.parse_utc(start)
    starts = [first + admission * k for k in range(days)]  # consecutive seasons, the last one with its exit tail
    end_ms = (starts[-1] + admission + tail + 120) * 1000
    if end_ms > team_eval.DEV_LAST_OPEN_MS:
        raise SystemExit("conformance reads DEV only: the last window would pass the DEV cutoff")
    catalog_first = team_data.read_catalog(store)["segments"][0]["first_ms"]
    series = team_eval_v4.load_minutes(store, max(catalog_first, first * 1000 - strategy_v4.MAX_LOOKBACK_MS), end_ms)
    funding = team_data.load_funding(store)
    team_eval_v4.attach_positioning(series, store, strategy["instrument"], funding)
    arrays = feature_books(strategy, series) if features == "arrays" else None
    feed = team_eval.RowFeed(store)
    started, windows, engine_trades, evaluator_trades, engine_book, evaluator_book = time.time(), [], [], [], [], []
    maker = {"engine": {}, "evaluator": {}}
    try:
        for start_s in starts:
            rows = feed.slice((start_s - team_eval.WARMUP_SECONDS) * 1000, (start_s + admission + tail + 60) * 1000)
            trades, orders, info = engine_season(recipe, rows, funding, instrument_path, series, arrays, start_s, admission, tail, strategy)
            expected, counters, placed = team_eval_v4.simulate(strategy, series, spec, funding, start_s * 1000, (start_s + admission + tail) * 1000,
                                                               mode="official", admission_end_ms=(start_s + admission) * 1000,
                                                               entry_cap=arena_engine.RULES["max_total_entries"],
                                                               risk_stop_pct=arena_engine.RULES["max_drawdown_pct"])
            season = utc(start_s)
            for item in (*trades, *expected, *orders, *placed):
                item["season"] = season
            engine_trades += trades
            evaluator_trades += expected
            engine_book += orders
            evaluator_book += placed
            row = {"start": season, "engine_trades": len(trades), "evaluator_trades": len(expected),
                   "ambiguous_minute_stops": counters["ambiguous_minute_stops"], **info}
            if post_only:
                row["maker"] = {"engine": maker_counts(orders), "evaluator": evaluator_counts(counters, placed)}
                add_counts(maker["engine"], row["maker"]["engine"])
                add_counts(maker["evaluator"], row["maker"]["evaluator"])
            windows.append(row)
    finally:
        feed.close()
    matched, mismatched, ambiguous, worst = compare(engine_trades, evaluator_trades)
    orders_matched, order_mismatches, worst_limit = compare_orders(engine_book, evaluator_book)
    net_engine = sum(t["net_usdt"] for t in engine_trades)
    net_evaluator = sum(t["net_usdt"] for t in evaluator_trades)
    return {"schema": "v4-conformance/1", "features": features, "recipe_sha256": strategy["recipe_sha256"], "semantic_sha256": strategy["semantic_sha256"],
            "execution_model_id": strategy["execution_model_id"], "instrument": strategy["instrument"],
            "order_type": rules["order"]["type"], "tail_seconds": tail, "store": str(store).replace("\\", "/"),
            "store_catalog_sha256": team_data.sha256_file(Path(store) / team_data.CATALOG),
            "engine_sha256": team_eval.sha256_file(ROOT / "trading_lab" / "arena_engine.py"),
            "adapter_sha256": team_eval.sha256_file(ROOT / "trading_lab" / "strategy_v4_runtime.py"),
            "book_sha256": team_eval.sha256_file(ROOT / "trading_lab" / "strategy_v4_book.py"),
            "evaluator_sha256": team_eval.sha256_file(HERE / "team_eval_v4.py"),
            "tape_model_sha256": team_eval.sha256_file(HERE / "okx_tape_model.py"),
            "instrument_sha256": team_eval.sha256_file(instrument_path),
            "windows": windows, "trades": {"engine": len(engine_trades), "evaluator": len(evaluator_trades),
            "matched": len(matched), "mismatched": len(mismatched), "ambiguous_minute": len(ambiguous)},
            "worst_matched_price_bps": round(worst, 4), "net_usdt": {"engine": round(net_engine, 8), "evaluator": round(net_evaluator, 8)},
            "orders": {"engine": sum(1 for o in engine_book if o["outcome"] not in NO_ORDER), "evaluator": len(evaluator_book),
                       "matched": orders_matched, "mismatched": len(order_mismatches)},
            "worst_matched_limit_bps": round(worst_limit, 4), "maker": maker if post_only else None,
            "mismatches": mismatched, "order_mismatches": order_mismatches, "ambiguous": ambiguous, "matched": matched,
            "elapsed_seconds": round(time.time() - started, 1)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--recipe", required=True)
    parser.add_argument("--start", required=True, help="first season, UTC date or time on a minute")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--store", default=str(team_eval.DEFAULT_STORE))
    parser.add_argument("--instrument", default=None, help="instrument JSON; default: tools/okx_instrument_<recipe instrument>.json")
    parser.add_argument("--features", choices=("book", "arrays"), default="book")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    report = run(args.recipe, args.start, args.days, Path(args.store), args.instrument, features=args.features)
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("trades", "worst_matched_price_bps", "net_usdt", "orders", "maker", "elapsed_seconds")},
                     ensure_ascii=False))
    return 0 if not report["mismatches"] and not report["order_mismatches"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
