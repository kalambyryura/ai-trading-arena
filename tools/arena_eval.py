"""Official secondary stand for authors (B-15): an exact recipe through the production RULES_ONLY engine.

Ported from the Claude coordinator's V8 stand (v8tools/arena_eval_v8.py) so every author has the same
tool. It runs an exact recipe JSON (V1 or declarative V2) through the unchanged production engine
(trading_lab.strategy_training.run_trial) on MODEL tapes rebuilt from public 1-minute candles
(tools/okx_tape_model.py) and reports net after modelled fees/spread/slippage/funding next to Cash,
drawdown, trades, side, partial reductions, exposure, incomplete exits and wait reasons.

Not primary evidence: the round's primary evidence stays tools/strategy_round.py on the common packet.
Candle tapes are a labelled model, the data is previously seen DEVELOPMENT history, nothing here is a
holdout or a Jev test. No network, no Jev/model calls, no exchange code, no keys. 100 USDT per window.

Tail policy: after the admission window the exit tail is max_hold_seconds + 420 s for a finite hold and
--tail-seconds (default 86400) for max_hold_seconds=null. A position still open at the end of the tail
makes the window INCOMPLETE; nothing is closed artificially.

Every invocation appends one line to the author's registry (default runtime/arena_eval/<author>/registry.jsonl)
and writes the full report next to it, so no attempt disappears. --trades writes every closed trade to a
CSV, for reconciling a fast model (e.g. tools/author_backtest.py) trade by trade with the engine.

  py -3.12 -X utf8 -B tools/arena_eval.py --author opus --recipe my.json --data <folder with tools/public_history.py CSVs>
      --start 2026-07-18T01:00:00Z --admission-hours 24 --count 59 --label "C01" --trades c01_trades.csv
  py -3.12 -X utf8 -B tools/arena_eval.py --author opus --recipe my.json --data <folder> --windows windows.json --matrix bouts5h
"""
import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCHEMA = "arena-eval/v1"
BUDGET = 100
FINITE_TAIL_EXTRA_SECONDS = 420  # recovery 120 s + 300 s margin after the recipe's own max_hold
FIRST_TAIL_SECONDS = 7200
DEFAULT_INSTRUMENT = HERE / "okx_instrument_btc_usdt_swap.json"
CLOSE_REASONS = {
    "Досягнуто stop стратегії.": "stop_loss", "Досягнуто ціль стратегії.": "take_profit",
    "Вичерпано час утримання.": "max_hold", "Умова виходу стратегії.": "formal_exit",
    "Умова виходу має пріоритет над докупівлею.": "formal_exit_on_add_recheck",
    "Зупинка учасника.": "participant_stop", "Формальне часткове закриття.": "partial_reduce",
    "Повний вихід має пріоритет над частковим.": "full_exit_over_reduce",
}
V2_WAIT_CODES = {"setup_waiting", "setup_waiting_confirmation"}
COUNTED = ("entry_intent", "entry_cancelled", "entry_no_fill", "risk_veto", "reduction_veto", "close_no_fill",
           "paper_entry_fill", "paper_close_fill", "risk_limit", "data_gap", "market_unavailable", "model_funding",
           "setup_armed", "setup_confirmed", "setup_expired", "setup_invalidated", "setup_gap_reset",
           "cost_checkpoint_checked", "cost_checkpoint_unknown", "exit_trigger")
TRADE_COLUMNS = ["window_id", "entry_at", "side", "seconds", "adds", "partial_reduces", "first_notional_usdt", "gross_usdt",
                 "fees_usdt", "funding_usdt", "net_usdt", "net_bps_of_first_notional", "exposure_usdt_hours", "exit_reason"]


def utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(text):
    return int(datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stamp_of(event):
    return datetime.fromisoformat(event["at"]).timestamp()


def leaf_key(check):
    rhs = check.get("rhs_feature")
    return f'{check.get("path", "")} {check["feature"]} {check["op"]} {rhs if rhs else check["expected"]}'.strip()


def analyse_run(run, admission_seconds):
    events = run["events"]
    codes = Counter(e["code"] for e in events)
    failed, unknown = Counter(), Counter()
    decisions = decisions_passed = 0
    for event in events:
        data = event["data"]
        if event["code"] == "entry_checks":
            decisions += 1
            if data.get("local_checks_passed"):
                decisions_passed += 1
            for check in data.get("checks", []):
                if check["passed"] is None:
                    unknown[leaf_key(check)] += 1
                elif check["passed"] is False:
                    failed[leaf_key(check)] += 1
        elif event["code"] in V2_WAIT_CODES:
            for check in data.get("checks", []):
                key = data.get("side", "?") + ":" + event["code"].replace("setup_", "") + ":" + leaf_key(check)
                if check["passed"] is None:
                    unknown[key] += 1
                elif check["passed"] is False:
                    failed[key] += 1
    cycles, current = [], None
    for event in events:
        data, stamp = event["data"], stamp_of(event)
        if event["code"] == "paper_entry_fill":
            notional = data["quantity_btc"] * data["price_usdt"]
            if current is None:
                current = {"entry_at": stamp, "side": data.get("side", "long"), "fees": 0.0, "gross": 0.0, "funding": 0.0,
                           "adds": 0, "reduces": 0, "max_qty": 0.0, "qty": 0.0, "first_notional": notional,
                           "exposure_usdt_s": 0.0, "last_t": stamp, "last_notional": 0.0}
            else:
                current["adds"] += 1
            current["exposure_usdt_s"] += current["last_notional"] * (stamp - current["last_t"])
            current["qty"] += data["quantity_btc"]
            current["max_qty"] = max(current["max_qty"], current["qty"])
            current["last_t"], current["last_notional"] = stamp, current["qty"] * data["price_usdt"]
            current["fees"] += data["fee_usdt"]
        elif event["code"] == "model_funding" and current is not None:
            current["funding"] += data.get("signed_amount_usdt", 0.0)
        elif event["code"] == "paper_close_fill" and current is not None:
            current["exposure_usdt_s"] += current["last_notional"] * (stamp - current["last_t"])
            current["fees"] += data["fee_usdt"]
            current["gross"] += data["gross_pnl_usdt"]
            current["qty"] = abs(data["remaining_btc"])
            current["last_t"], current["last_notional"] = stamp, current["qty"] * data["price_usdt"]
            reason = CLOSE_REASONS.get(data.get("reason"), data.get("reason"))
            if data["remaining_btc"]:
                if reason == "partial_reduce":
                    current["reduces"] += 1
                continue
            net = current["gross"] - current["fees"] + current["funding"]
            cycles.append({"entry_at": utc(current["entry_at"]), "side": current["side"],
                           "seconds": round(stamp - current["entry_at"], 1), "adds": current["adds"],
                           "partial_reduces": current["reduces"], "first_notional_usdt": round(current["first_notional"], 4),
                           "gross_usdt": round(current["gross"], 6), "fees_usdt": round(current["fees"], 6),
                           "funding_usdt": round(current["funding"], 6), "net_usdt": round(net, 6),
                           "net_bps_of_first_notional": round(net / current["first_notional"] * 10000, 3) if current["first_notional"] else None,
                           "exposure_usdt_hours": round(current["exposure_usdt_s"] / 3600, 6), "exit_reason": reason})
            current = None
    open_cycle = None
    if current is not None:
        open_cycle = {"entry_at": utc(current["entry_at"]), "side": current["side"], "adds": current["adds"],
                      "partial_reduces": current["reduces"], "remaining_btc": run["metrics"]["remaining_btc"]}
    m = run["metrics"]
    return {"status": run["status"], "pnl_usdt": m["pnl_usdt"], "return_pct": m["return_pct"],
            "max_drawdown_pct": m["max_drawdown_pct"], "trades": m["trades"], "fees_usdt": m["fees_usdt"],
            "funding_usdt": m["funding_usdt"], "remaining_btc": m["remaining_btc"],
            "entry_decisions": decisions, "entry_decisions_passed": decisions_passed,
            "event_counts": {k: codes[k] for k in COUNTED if codes[k]},
            "wait_failed_conditions": dict(failed.most_common(10)), "wait_unknown_conditions": dict(unknown.most_common(10)),
            "cycles": cycles, "open_cycle_at_end": open_cycle,
            "time_in_market_seconds": round(sum(c["seconds"] for c in cycles), 1),
            "exposure_usdt_hours": round(sum(c["exposure_usdt_hours"] for c in cycles), 6),
            "admission_seconds": admission_seconds}


def summarise(rows):
    if not rows:
        return {}
    pnls = [r["pnl_usdt"] for r in rows]
    cycles = [c for r in rows for c in r["cycles"]]
    nets = [c["net_usdt"] for c in cycles]
    by_side = {}
    for side in ("long", "short"):
        part = [c["net_usdt"] for c in cycles if c["side"] == side]
        if part:
            by_side[side] = {"cycles": len(part), "net_usdt": round(sum(part), 6),
                             "win_rate": round(sum(n > 0 for n in part) / len(part), 4)}
    return {"windows": len(rows), "complete": sum(r["status"] == "COMPLETE" for r in rows),
            "incomplete_windows": [r["id"] for r in rows if r["status"] != "COMPLETE"],
            "net_total_usdt": round(sum(pnls), 6), "net_mean_usdt": round(statistics.fmean(pnls), 6),
            "net_median_usdt": round(statistics.median(pnls), 6),
            "net_stdev_usdt": round(statistics.pstdev(pnls), 6) if len(pnls) > 1 else 0.0,
            "net_worst_usdt": round(min(pnls), 6), "net_best_usdt": round(max(pnls), 6),
            "windows_above_cash": sum(p > 1e-9 for p in pnls), "windows_below_cash": sum(p < -1e-9 for p in pnls),
            "windows_zero_trades": sum(r["trades"] == 0 for r in rows),
            "max_drawdown_pct_worst": round(max(r["max_drawdown_pct"] for r in rows), 6),
            "trades": len(cycles), "trade_win_rate": round(sum(n > 0 for n in nets) / len(nets), 4) if nets else None,
            "trade_net_mean_usdt": round(statistics.fmean(nets), 6) if nets else None,
            "trade_net_bps_mean": round(statistics.fmean(c["net_bps_of_first_notional"] for c in cycles), 3) if cycles else None,
            "by_side": by_side,
            "partial_reduces": sum(c["partial_reduces"] for c in cycles), "adds": sum(c["adds"] for c in cycles),
            "fees_usdt": round(sum(r["fees_usdt"] for r in rows), 6), "funding_usdt": round(sum(r["funding_usdt"] for r in rows), 6),
            "exit_reasons": dict(Counter(c["exit_reason"] for c in cycles)),
            "exposure_usdt_hours": round(sum(r["exposure_usdt_hours"] for r in rows), 4),
            "time_in_market_hours": round(sum(r["time_in_market_seconds"] for r in rows) / 3600, 3),
            "entry_decisions": sum(r["entry_decisions"] for r in rows),
            "risk_limit_windows": sum(1 for r in rows if r["event_counts"].get("risk_limit"))}


def one_window(job):
    """Runs in a worker process; imports happen there so every worker uses the same sources.

    Speed-up without changing results: the engine stops reading the tape as soon as the season is DONE
    (flat after the admission window). So a window is first run with a tail of at most 2 h; only if a
    position is still open at the end of that tape is the window run again with the full policy tail.
    """
    recipe_bytes, item, tail_seconds, candles, funding, instrument = job
    for path in (str(ROOT), str(HERE)):
        if path not in sys.path:
            sys.path.insert(0, path)
    from trading_lab.arena_strategies import validate_strategy
    from trading_lab.strategy_training import run_trial
    import okx_tape_model
    cache = getattr(okx_tape_model, "CANDLE_CACHE", {})
    key = (candles, funding, instrument)
    store = cache.get(key)
    if store is None:
        store = okx_tape_model.CandleStore(candles, funding, instrument)
        okx_tape_model.CANDLE_CACHE = {key: store}
    strategy = validate_strategy(json.loads(recipe_bytes.decode("utf-8-sig")))
    begin = parse_utc(item["start_utc"])
    admission = item["admission_seconds"]
    used = min(tail_seconds, FIRST_TAIL_SECONDS)
    while True:
        tape = store.tape(begin, begin + admission + used)
        result = run_trial(strategy, {"id": item["id"], "name": item["id"], "kind": "MODEL_FROM_PUBLIC_CANDLES"},
                           BUDGET, [(item["id"], tape, admission)])
        run = result["runs"][0]
        if run["status"] == "COMPLETE" or used >= tail_seconds:
            break
        used = tail_seconds
    row = analyse_run(run, admission)
    row.update(id=item["id"], day=item.get("day") or item["start_utc"][:10], start_utc=item["start_utc"],
               tail_policy_seconds=tail_seconds, tail_used_seconds=used, tape_end_utc=utc(tape[-1]["at"]),
               data_sha256=result["data_sha256"])
    return row


def generated_windows(start, admission_hours, count, step_hours):
    """Consecutive admission windows with a stable id; the day is the UTC date of the window start."""
    begin, admission, step = parse_utc(start), int(admission_hours * 3600), int((step_hours or admission_hours) * 3600)
    if begin % 60 or admission < 300 or step < 60 or not 1 <= count <= 2000:
        raise SystemExit("windows: start on a minute, admission >= 5 min, 1..2000 windows")
    return [{"id": f"w{i:04d}", "day": utc(begin + i * step)[:10], "start_utc": utc(begin + i * step), "admission_seconds": admission}
            for i in range(count)]


def single(folder, pattern):
    found = sorted(Path(folder).glob(pattern))
    if len(found) != 1:
        raise SystemExit(f"{folder}: expected exactly one {pattern}, found {len(found)}; pass the file explicitly")
    return found[0]


def write_trades(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(TRADE_COLUMNS)
        for row in rows:
            for cycle in row["cycles"]:
                writer.writerow([row["id"]] + [cycle[k] for k in TRADE_COLUMNS[1:]])
    return sha256_file(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Secondary DEVELOPMENT stand: exact recipe, production RULES_ONLY engine, model tapes from public candles, no network.")
    parser.add_argument("--recipe", type=Path, required=True)
    parser.add_argument("--author", default="unknown", help="your scope name; the registry lives under runtime/arena_eval/<author>/")
    parser.add_argument("--label", default="", help="your attempt id / short description (goes to the registry)")
    parser.add_argument("--data", type=Path, help="folder with one okx_*_1m_*.csv and one okx_*_funding_*.csv (tools/public_history.py format)")
    parser.add_argument("--candles", type=Path)
    parser.add_argument("--funding", type=Path)
    parser.add_argument("--instrument", type=Path, default=DEFAULT_INSTRUMENT)
    parser.add_argument("--windows", type=Path, help="matrix file {matrices: {name: {windows: [...]}}} (V8 format)")
    parser.add_argument("--matrix", help="matrix name inside --windows")
    parser.add_argument("--start", help="first window start, UTC ISO with Z (generated windows)")
    parser.add_argument("--admission-hours", type=float, default=24.0)
    parser.add_argument("--step-hours", type=float, default=None, help="distance between window starts (default = admission)")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--only", default=None, help="comma-separated window ids (exploration; still registered)")
    parser.add_argument("--tail-seconds", type=int, default=86400, help="exit tail for max_hold_seconds=null (default 86400)")
    parser.add_argument("--jobs", type=int, choices=(1,), default=1,
                        help="serial only: one history cache; parallel workers are disabled to protect RAM")
    parser.add_argument("--trades", type=Path, default=None, help="also write every closed trade to this CSV")
    parser.add_argument("--registry", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    sys.path.insert(0, str(HERE))
    from research_limits import ResearchBusy, exclusive_research_slot
    try:
        with exclusive_research_slot(ROOT, "arena_eval"):
            return evaluate(args)
    except ResearchBusy as error:
        raise SystemExit(str(error)) from None


def evaluate(args):
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(HERE))
    from trading_lab.arena_strategies import validate_strategy
    from trading_lab.strategy_training import code_hash
    from okx_tape_model import MODEL_ASSUMPTIONS

    raw = args.recipe.read_bytes()
    if len(raw) > 131072:
        raise SystemExit("recipe too large (workshop limit 131072 bytes)")
    strategy = validate_strategy(json.loads(raw.decode("utf-8-sig")))
    rules = strategy["rules"]
    hold = rules["max_hold_seconds"]
    tail = (int(hold) + FINITE_TAIL_EXTRA_SECONDS) if hold is not None else int(args.tail_seconds)
    if not 600 <= tail <= 86400 + FINITE_TAIL_EXTRA_SECONDS:
        raise SystemExit("tail must be within 600..86820 s")
    candles = args.candles or (single(args.data, "okx_*_1m_*.csv") if args.data else None)
    funding = args.funding or (single(args.data, "okx_*_funding_*.csv") if args.data else None)
    if not candles or not funding:
        raise SystemExit("pass --data or both --candles and --funding")
    if args.windows:
        spec = json.loads(args.windows.read_text(encoding="utf-8"))
        if args.matrix not in spec.get("matrices", {}):
            raise SystemExit(f"unknown matrix {args.matrix}; available: {sorted(spec.get('matrices', {}))}")
        if spec.get("source_csv_sha256") and spec["source_csv_sha256"] != sha256_file(candles):
            raise SystemExit("the candle file differs from the one this matrix was built for")
        windows, matrix = spec["matrices"][args.matrix]["windows"], args.matrix
    elif args.start:
        windows = generated_windows(args.start, args.admission_hours, args.count, args.step_hours)
        matrix = f"generated:{args.start}/{args.admission_hours:g}h/x{args.count}/step{(args.step_hours or args.admission_hours):g}h"
    else:
        raise SystemExit("pass --windows with --matrix, or --start with --admission-hours/--count")
    if args.only:
        wanted = set(args.only.split(","))
        windows = [w for w in windows if w["id"] in wanted]
    started = time.time()
    jobs = [(raw, item, tail, str(candles), str(funding), str(args.instrument)) for item in windows]
    rows = [one_window(job) for job in jobs]
    report = {
        "schema": SCHEMA, "created_at": utc(time.time()), "author_scope": args.author, "label": args.label,
        "matrix": matrix, "matrix_windows": len(rows), "only": args.only,
        "recipe_file": str(args.recipe), "recipe_file_sha256": hashlib.sha256(raw).hexdigest(),
        "recipe_sha256": strategy["recipe_sha256"], "recipe_name": strategy["name"],
        "strategy_schema_version": strategy.get("strategy_schema_version", 1), "side": rules["side"],
        "rules": rules, "budget_usdt_per_window": BUDGET, "tail_seconds": tail,
        "runtime": "RULES_ONLY (no Jev, no exchange)", "role": "DEVELOPMENT_SECONDARY (model tapes from previously seen public candles)",
        "code_sha256": code_hash(), "data_files_sha256": {Path(p).name: sha256_file(p) for p in (candles, funding, args.instrument)},
        "quote_model": MODEL_ASSUMPTIONS, "evaluator_sha256": sha256_file(__file__),
        "decision_kernel_version": "production-v1-v2-strict-unknown-v1",
        "market_feature_version": "completed-close-wicks-v1;asof-funding-v2",
        "execution_model_version": "paper-quote-ioc-v8:" + MODEL_ASSUMPTIONS["model_id"],
        "accounting_version": "paper-fill-fees-v1;realized-funding-events-v2",
        "execution_evidence": "MODEL: minute OHLC does not establish actual intra-minute path or exchange fills",
        "tape_model_sha256": sha256_file(HERE / "okx_tape_model.py"),
        "windows_file_sha256": sha256_file(args.windows) if args.windows else None,
        "elapsed_seconds": round(time.time() - started, 1),
        "summary": summarise(rows), "windows": rows, "cash_net_usdt": 0.0,
        "caveat": "Secondary development evidence on modelled quotes. Not a market recording, not a Jev test, not a holdout, not proof of edge.",
    }
    scope = ROOT / "runtime" / "arena_eval" / args.author
    registry = args.registry or scope / "registry.jsonl"
    out = args.out or scope / "runs" / f'{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}_{strategy["recipe_sha256"][:8]}.json'
    if args.trades:
        report["trades_file"] = {"path": str(args.trades), "sha256": write_trades(args.trades, rows), "columns": TRADE_COLUMNS}
    out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, ensure_ascii=False, indent=1, allow_nan=False)
    out.write_bytes((text + "\n").encode("utf-8"))  # exact bytes: the registry pins their sha256 on every OS
    compact = {k: report[k] for k in ("created_at", "author_scope", "label", "matrix", "matrix_windows", "only", "recipe_file",
                                      "recipe_file_sha256", "recipe_sha256", "side", "tail_seconds", "code_sha256", "evaluator_sha256")}
    compact["summary"] = {k: v for k, v in report["summary"].items() if k != "incomplete_windows"}
    compact["incomplete_windows"] = len(report["summary"].get("incomplete_windows", []))
    compact["report_file"] = str(out)
    compact["report_sha256"] = hashlib.sha256((text + "\n").encode("utf-8")).hexdigest()
    registry.parent.mkdir(parents=True, exist_ok=True)
    with registry.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(compact, ensure_ascii=False, sort_keys=True) + "\n")
    s = report["summary"]
    print(json.dumps({"label": args.label, "matrix": matrix, "recipe_sha256": strategy["recipe_sha256"],
                      "windows": s.get("windows"), "complete": s.get("complete"), "net_total_usdt": s.get("net_total_usdt"),
                      "net_mean_usdt": s.get("net_mean_usdt"), "above/below_cash/zero_trades":
                      [s.get("windows_above_cash"), s.get("windows_below_cash"), s.get("windows_zero_trades")],
                      "trades": s.get("trades"), "by_side": s.get("by_side"), "win_rate": s.get("trade_win_rate"),
                      "fees_usdt": s.get("fees_usdt"), "max_dd_pct": s.get("max_drawdown_pct_worst"),
                      "exit_reasons": s.get("exit_reasons"), "elapsed_s": report["elapsed_seconds"], "report": str(out)},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
