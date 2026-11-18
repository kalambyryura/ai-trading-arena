"""Team evaluator for the charter gates K (DEV) and D (SEALED/FORWARD): exact recipe -> trade ledger -> gate metrics.

Engine: the unchanged production RULES_ONLY engine (trading_lab.strategy_training.run_trial) on MODEL tapes built
by tools/okx_tape_model.py, i.e. exactly the code path of tools/arena_eval.py. The only difference is logistics:
candles come from the team data store (tools/team_data.py catalogue, many files) and windows run in parallel
worker processes, each receiving only the minutes its own window needs. A test proves window results equal
arena_eval's on the same data.

  run    recipe + store + span -> ledger.json, gates.json, gates.md in --out, one registry line per attempt
  gates  recompute the gates from a ledger (e.g. after a threshold question), no engine run
  clone  two ledgers -> entry overlap within +-30 min and daily PnL correlation (charter section 5.5)

Costs: the engine charges 5 bps fee and 2 bps slippage per execution plus the modelled 1-tick spread and realized
funding events where the data has them. Stress subtracts another 5 bps of every executed notional afterwards;
decisions and fills are not re-simulated under stress (documented in team/platform/TEAM_EVAL_UA.md).

Rounds: consecutive admission windows (default 24 h, aligned to 00:00 UTC) run in parallel for speed and are then
joined into one 100 USDT account with one net position (charter 1.5, D097): a trade that opens while the previous
kept trade is still open is dropped and counted; above 5 % dropped, the verdict is NEEDS_SEQUENTIAL_RUN.
Exit tail as arena_eval: max_hold + 420 s for a finite hold, --tail-seconds (86400) for max_hold_seconds=null; where
the data ends first, the tail ends with it. A position still open at the end of its tail is closed at the last mark
with taker costs and flagged (forced_close). A data gap or engine failure fails the gates.
Gates K follow the epochs E1 / E2 / E2 tail of D100/D102; gate D uses the account drawdown with the open position
marked at every minute close (exact engine) or every 5 s at the exit price after the exit fee (candle v4, stricter).
With --declared-n N (D124, D127 p. 4) gates D2 and D4 count only the first N closed trades by entry: exits forced by
the data (end of the period, a hole inside it) are not closed trades, the verdict is INSUFFICIENT until the N-th, and
nothing after the N-th exit enters it; gates.json carries the canonical hash of that prefix, computed exactly like the
Judge's recipe (team/judge/tools/gate_d_first_n.py, gate D procedure v1.1 section 4).

No network, keys, Jev, exchange code or account. DEV runs refuse data after the DEV cutoff; SEALED runs need
--set sealed and write only aggregate numbers into gates.md.

  py -3.12 -X utf8 -B tools/team_eval.py run --recipe my.json --author astra --label C01 --out runtime/team/astra/eval/C01
  py -3.12 -X utf8 -B tools/team_eval.py run --recipe my.json --start 2024-01-01 --end 2024-03-31 --workers 4 --out ...
  py -3.12 -X utf8 -B tools/team_eval.py gates --ledger <judge ledger.json> --declared-n 30 --out ...
  py -3.12 -X utf8 -B tools/team_eval.py gates --ledger runtime/team/astra/eval/C01/ledger.json
  py -3.12 -X utf8 -B tools/team_eval.py clone --a new/ledger.json --b other/ledger.json
"""
import argparse
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _path in (str(ROOT), str(HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)
import team_data  # noqa: E402

SCHEMA_LEDGER, SCHEMA_GATES = "team-eval-ledger/v2", "team-eval-gates/v2"
EVALUATOR_VERSION = "team-eval-v2"
CAPITAL = 100
FINITE_TAIL_EXTRA_SECONDS = 420
FIRST_TAIL_SECONDS = 7200
WARMUP_SECONDS = 1860  # the tape needs 30 closed minutes before the first snapshot
DEV_LAST_OPEN_MS = 1790965860000  # 2026-10-02T18:31:00Z, last DEV bar (cutoff_ms 1790965955865)
DEFAULT_STORE = Path("data/okx/BTC-USDT-SWAP")
DEFAULT_INSTRUMENT = HERE / "okx_instrument_btc_usdt_swap.json"
RAM_FLOOR_GB = 6.0
V4_RAM_FLOOR_GB = 3.0  # a whole-DEV v4 run peaks below 1.5 GB committed (H10 ETH, 2026-10-28)
MAX_WORKERS = 12
CHARTER = {
    "costs": {"fee_bps_per_execution": 5, "slippage_bps_per_execution": 2, "stress_extra_bps_per_execution": 5,
              "spread": "model 1 tick", "funding": "realized events where present in the data"},
    "K": {"min_closed_trades": 100, "worst_year_floor_pct_of_capital": -10.0, "drop_best_trades": 10,
          "full_quarter_coverage": 0.95, "active_quarter_min_trades": 5, "max_overlap_dropped_share": 0.05,
          "verdict_scenario": "conservative",
          "epochs": {"E1": {"start": "2020-01-01", "end": "2024-01-01", "min_stress_pct_of_capital": -5.0},
                     "E2": {"start": "2024-01-01", "end": "2026-10-02T18:32:00Z", "min_closed_trades": 40,
                            "min_active_quarters": 6, "min_positive_quarter_share": 0.60},
                     "E2_tail": {"start": "2025-07-01", "end": "2026-10-02T18:32:00Z", "min_closed_trades": 15}}},
    "D": {"min_closed_trades": 20, "max_best_trade_share_of_gross_profit": 0.30, "max_drawdown_pct_of_capital": 10.0},
    "clone": {"entry_window_minutes": 30, "max_entry_overlap": 0.50, "max_daily_pnl_correlation": 0.80},
    "funding": {"settlement_hours_utc": (0, 8, 16), "conservative_floor_rate": 0.0001, "sensitivity_rate": 0.0003,
                "sensitivity_min_mean_hold_hours": 8, "reference_window_utc": ("2026-06-29", "2026-10-02T18:32:00Z")},
    "charter_version": "1.5",
    "decisions": ["D093", "D094", "D097", "D100", "D102"],
}
CLOSE_REASONS = {
    "Досягнуто stop стратегії.": "stop_loss", "Досягнуто ціль стратегії.": "take_profit",
    "Вичерпано час утримання.": "max_hold", "Умова виходу стратегії.": "formal_exit",
    "Умова виходу має пріоритет над докупівлею.": "formal_exit_on_add_recheck",
    "Зупинка учасника.": "participant_stop", "Формальне часткове закриття.": "partial_reduce",
    "Повний вихід має пріоритет над частковим.": "full_exit_over_reduce",
}
ERROR_EVENTS = ("data_gap", "market_unavailable")  # an engine/data failure fails the gates until fixed (D097)
NOT_CLOSED_FOR_GATE_D = ("forced_close_end_of_period", "data_gap_exit")  # forced by the data, not by the recipe (D127 p. 4)
COUNTED = ("entry_intent", "entry_cancelled", "entry_no_fill", "risk_veto", "reduction_veto", "close_no_fill",
           "risk_limit", "data_gap", "market_unavailable", "model_funding", "cost_checkpoint_unknown")


def utc(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(text):
    text = str(text).strip()
    if len(text) == 10:
        text += "T00:00:00Z"
    return int(datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return team_data.sha256_file(path)


def free_ram_gb():
    """Available physical memory in GB, or None where it cannot be read."""
    if os.name == "nt":
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                        ("available", ctypes.c_ulonglong), ("page_total", ctypes.c_ulonglong),
                        ("page_available", ctypes.c_ulonglong), ("virtual_total", ctypes.c_ulonglong),
                        ("virtual_available", ctypes.c_ulonglong), ("extended", ctypes.c_ulonglong)]
        status = Status()
        status.length = ctypes.sizeof(Status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.available / 2**30
        return None
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 2**20
    except OSError:
        return None
    return None


def peak_rss_mb():
    """Peak working set of this process in MB (Windows) or max RSS (POSIX); None if unknown."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD), ("peak", ctypes.c_size_t),
                        ("current", ctypes.c_size_t), ("a", ctypes.c_size_t), ("b", ctypes.c_size_t),
                        ("c", ctypes.c_size_t), ("d", ctypes.c_size_t), ("e", ctypes.c_size_t), ("f", ctypes.c_size_t)]
        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if kernel32.K32GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return round(counters.peak / 2**20, 1)
        return None
    try:
        import resource
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except (ImportError, OSError):
        return None


# ----------------------------------------------------------------------------------------------------------- engine
def row_store(rows, funding, instrument_path):
    """okx_tape_model.CandleStore over in-memory rows: same tape(), raw_at() and paths, no file reading."""
    import okx_tape_model

    class RowStore(okx_tape_model.CandleStore):
        def __init__(self):  # noqa: D401 - deliberately bypasses the CSV reader of the parent
            self.files = {"rows": len(rows)}
            self.rows = rows
            self.first_ms, self.last_ms = min(rows), max(rows)
            self.funding_times = [t for t, _ in funding]
            self.funding_rates = [r for _, r in funding]
            instrument = json.loads(Path(instrument_path).read_text(encoding="utf-8"))
            self.instrument = instrument if isinstance(instrument, list) else [instrument]
            tick = self.instrument[0]["tickSz"]
            self.tick = float(tick)
            self.decimals = len(tick.split(".")[1]) if "." in tick else 0
            self._paths = {}
    return RowStore()


def stamp_of(event):
    return datetime.fromisoformat(event["at"]).timestamp()


def trades_from_events(events, window_id):
    """Closed position cycles with every execution, in the engine's own numbers, plus the cycle still open at the end."""
    trades, current = [], None
    for event in events:
        data, code = event["data"], event["code"]
        if code == "paper_entry_fill":
            stamp, notional = stamp_of(event), data["quantity_btc"] * data["price_usdt"]
            if current is None:
                current = {"window_id": window_id, "entry_at": utc(stamp), "_entry": stamp, "side": data.get("side", "long"),
                           "adds": 0, "partial_reduces": 0, "first_notional_usdt": notional, "gross_usdt": 0.0,
                           "fees_usdt": 0.0, "funding_usdt": 0.0, "executions": []}
            else:
                current["adds"] += 1
            current["fees_usdt"] += data["fee_usdt"]
            if data.get("adverse_selection_usdt"):  # D096 post-only fills only; taker, V1 and V2 trades never get the key
                current["adverse_selection_usdt"] = current.get("adverse_selection_usdt", 0.0) + data["adverse_selection_usdt"]
            current["executions"].append({"at": utc(stamp), "kind": "entry" if not current["executions"] else "add",
                                          "quantity": data["quantity_btc"], "price_usdt": data["price_usdt"],
                                          "notional_usdt": round(notional, 8), "fee_usdt": data["fee_usdt"]})
        elif code == "model_funding" and current is not None:
            current["funding_usdt"] += data.get("signed_amount_usdt", 0.0)
        elif code == "paper_close_fill" and current is not None:
            stamp, notional = stamp_of(event), data["quantity_btc"] * data["price_usdt"]
            current["fees_usdt"] += data["fee_usdt"]
            current["gross_usdt"] += data["gross_pnl_usdt"]
            reason = CLOSE_REASONS.get(data.get("reason"), data.get("reason"))
            current["executions"].append({"at": utc(stamp), "kind": "reduce" if data["remaining_btc"] else "close",
                                          "quantity": data["quantity_btc"], "price_usdt": data["price_usdt"],
                                          "notional_usdt": round(notional, 8), "fee_usdt": data["fee_usdt"]})
            if data["remaining_btc"]:
                if reason == "partial_reduce":
                    current["partial_reduces"] += 1
                continue
            net = current["gross_usdt"] - current["fees_usdt"] + current["funding_usdt"]
            adverse = current.get("adverse_selection_usdt")
            trade = finish_trade(current, stamp, reason, net - adverse if adverse else net)
            if adverse:  # the cost the engine books into the bank at a post-only fill, as tools/team_eval_v4.py
                trade["adverse_selection_usdt"] = round(adverse, 8)
            trades.append(trade)
            current = None
    return trades, current


def finish_trade(current, exit_stamp, reason, net, forced=False):
    executed = sum(x["notional_usdt"] for x in current["executions"])
    return {"window_id": current["window_id"], "entry_at": current["entry_at"], "exit_at": utc(exit_stamp),
            "seconds": round(exit_stamp - current["_entry"], 1), "side": current["side"],
            "adds": current["adds"], "partial_reduces": current["partial_reduces"],
            "first_notional_usdt": round(current["first_notional_usdt"], 6),
            "executed_notional_usdt": round(executed, 6), "gross_usdt": round(current["gross_usdt"], 8),
            "fees_usdt": round(current["fees_usdt"], 8), "funding_usdt": round(current["funding_usdt"], 8),
            "net_usdt": round(net, 8), "exit_reason": reason, "forced_close": forced, "executions": current["executions"]}


def minute_close_excursions(curve, trades):
    """Per trade: best, worst and largest peak-to-trough of the account equity (engine marks, which include the
    exit cost) relative to the equity just before entry, sampled at each minute close and at the exit (D097)."""
    points = [(datetime.fromisoformat(p["at"]).timestamp(), p["equity_usdt"]) for p in curve]
    times = [t for t, _ in points]
    for trade in trades:
        a, b = parse_utc(trade["entry_at"]), parse_utc(trade["exit_at"])
        first, last = bisect_left(times, a), bisect_right(times, b)
        base = points[first - 1][1] if first > 0 else CAPITAL
        closes = {}
        for t, equity in points[first:last]:
            closes[math.ceil(t / 60)] = equity  # the last mark at or before each minute close
        path = [equity - base for _, equity in sorted(closes.items())]
        if last > first:
            path.append(points[last - 1][1] - base)
        peak = best = worst = drawdown = 0.0
        for value in path:
            best, worst = max(best, value), min(worst, value)
            peak = max(peak, value)
            drawdown = max(drawdown, peak - value)
        trade.update(mtm_best_usdt=round(best, 8), mtm_worst_usdt=round(worst, 8), mtm_drawdown_usdt=round(drawdown, 8))


def evaluate_window(job):
    """One admission window through the production engine; runs in a worker process."""
    recipe_bytes, item, tail_seconds, rows, funding, instrument_path = job
    from trading_lab.arena_strategies import validate_strategy
    from trading_lab.strategy_training import run_trial
    strategy = validate_strategy(json.loads(recipe_bytes.decode("utf-8-sig")))
    store = row_store(rows, funding, instrument_path)
    begin, admission = item["start_s"], item["admission_seconds"]
    tail = item.get("tail_seconds", tail_seconds)  # shorter only where the data ends (D097 end of period)
    used = min(tail, FIRST_TAIL_SECONDS)
    while True:  # a DONE season stops reading the tape, so a shorter first tape gives identical results
        tape = store.tape(begin, begin + admission + used)
        result = run_trial(strategy, {"id": item["id"], "name": item["id"], "kind": "MODEL_FROM_PUBLIC_CANDLES"},
                           CAPITAL, [(item["id"], tape, admission)])
        run = result["runs"][0]
        if run["status"] == "COMPLETE" or used >= tail:
            break
        used = tail
    trades, open_cycle = trades_from_events(run["events"], item["id"])
    codes = Counter(e["code"] for e in run["events"])
    m = run["metrics"]
    errors = {k: codes[k] for k in ERROR_EVENTS if codes[k]}
    forced = None
    if run["status"] != "COMPLETE" and open_cycle is not None and not errors:
        # D097: a position still open when the period ends is closed at the last mark, taker costs included
        # (the engine's equity mark already deducts the 2 bps slippage and 5 bps fee of closing).
        last = tape[-1]
        price = last["bid"] * .9998 if open_cycle["side"] == "long" else last["ask"] * 1.0002
        remaining = abs(m["remaining_btc"])
        open_cycle["executions"].append({"at": utc(last["at"]), "kind": "close", "quantity": remaining, "price_usdt": price,
                                         "notional_usdt": round(remaining * price, 8), "fee_usdt": round(remaining * price * .0005, 8),
                                         "forced": True})
        forced = finish_trade(open_cycle, last["at"], "forced_close_end_of_period",
                              m["pnl_usdt"] - sum(t["net_usdt"] for t in trades), forced=True)
        trades.append(forced)
    minute_close_excursions(run["curve"], trades)
    return {"id": item["id"], "start_utc": utc(begin), "admission_seconds": admission, "status": run["status"],
            "pnl_usdt": m["pnl_usdt"], "trades": m["trades"], "fees_usdt": m["fees_usdt"], "funding_usdt": m["funding_usdt"],
            "max_drawdown_pct": m["max_drawdown_pct"], "remaining_btc": m["remaining_btc"],
            "tail_policy_seconds": tail_seconds, "tail_seconds": tail, "tail_used_seconds": used, "data_sha256": result["data_sha256"],
            "event_counts": {k: codes[k] for k in COUNTED if codes[k]}, "errors": errors,
            "events_sha256": events_digest(run["events"]),
            "forced_close": forced is not None, "open_cycle_at_end": None if open_cycle is None else
            {"entry_at": open_cycle["entry_at"], "side": open_cycle["side"], "executions": len(open_cycle["executions"])},
            "closed": trades, "worker_peak_rss_mb": peak_rss_mb()}


def events_digest(events):
    """sha256 of the engine's whole event journal for identity proofs across core releases. Per-run random
    identifiers (bot ids and the config hash that covers them) become stable placeholders, so equal behaviour
    gives equal digests from run to run."""
    order, clean = {}, []
    for event in events:
        item = dict(event)
        if item.get("bot_id") is not None:
            item["bot_id"] = order.setdefault(item["bot_id"], len(order))
        if isinstance(item.get("data"), dict) and "config_sha256" in item["data"]:
            item["data"] = {**item["data"], "config_sha256": None}
        clean.append(item)
    return hashlib.sha256(json.dumps(clean, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def make_windows(first_ms, last_ms, start, end, admission_seconds, step_seconds, tail_seconds):
    """Windows (day-aligned unless start is given) whose warm-up and admission lie inside the data. Where the full
    exit tail does not fit, the tail ends with the data and a still-open position is force-closed there (D097)."""
    data_start, data_end = first_ms // 1000, last_ms // 1000  # the tape may end inside the last minute, not after it
    begin = parse_utc(start) if start else data_start + WARMUP_SECONDS
    if not start:
        begin = -(-begin // 86400) * 86400
    stop = min(parse_utc(end) + (86400 if len(end.strip()) == 10 else 0), data_end) if end else data_end
    if begin % 60 or admission_seconds < 300 or step_seconds < 60:
        raise SystemExit("windows: start on a minute, admission >= 300 s, step >= 60 s")
    if begin - WARMUP_SECONDS < data_start:
        raise SystemExit(f"start {utc(begin)} leaves less than {WARMUP_SECONDS} s of warm-up data")
    windows, clipped, index = [], 0, 0
    t = begin
    while t + admission_seconds <= stop:
        item = {"id": f"w{index:05d}", "start_s": t, "admission_seconds": admission_seconds}
        room = data_end - t - admission_seconds
        if room < tail_seconds:
            item["tail_seconds"] = room
            clipped += 1
        windows.append(item)
        index += 1
        t += step_seconds
    if not windows:
        raise SystemExit("no window fits the span and the data")
    return windows, clipped


def plan_windows(store, data_set, start, end, admission_seconds, step_seconds, tail_seconds):
    """Catalogue + windows for one data set; DEV never reaches past the DEV cutoff or into a sealed vault."""
    store = Path(store)
    if data_set == "DEV" and "trading-vault" in str(store.resolve()).lower():
        raise SystemExit("this store is a sealed vault: only --set sealed (Judge) may read it")
    catalog = team_data.read_catalog(store)
    if catalog.get("problems"):
        raise SystemExit(f"store catalogue has problems: {catalog['problems'][:3]}")
    first_ms, last_ms = catalog["segments"][0]["first_ms"], catalog["segments"][-1]["last_ms"]
    if data_set == "DEV":
        last_ms = min(last_ms, DEV_LAST_OPEN_MS)
    windows, clipped = make_windows(first_ms, last_ms, start, end, admission_seconds, step_seconds, tail_seconds)
    return catalog, windows, clipped


class RowFeed:
    """Sequential reader over catalogue segments that hands out minute slices in increasing time order."""

    def __init__(self, root):
        self.segments = team_data.read_catalog(root)["segments"]
        self.root, self.buffer, self.high, self.done, self.reader = Path(root), {}, None, False, None

    def _rows(self, start_ms):
        import csv
        for segment in self.segments:
            if segment["last_ms"] < start_ms:  # whole files before the first request are never parsed
                continue
            with (self.root / segment["file"]).open(newline="", encoding="utf-8") as handle:
                reader = csv.reader(handle)
                next(reader)
                for row in reader:
                    yield int(row[0]), [row[0], row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9]]

    def slice(self, first_ms, end_ms):
        if self.reader is None:
            self.reader = self._rows(first_ms)
        for key in [k for k in self.buffer if k < first_ms]:
            del self.buffer[key]
        while not self.done and (self.high is None or self.high < end_ms - 60_000):
            try:
                stamp, row = next(self.reader)
            except StopIteration:
                self.done = True
                break
            self.high = stamp
            if stamp >= first_ms:
                self.buffer[stamp] = row
        return {k: v for k, v in self.buffer.items() if first_ms <= k < end_ms}

    def close(self):
        """The suspended reader and this feed reference each other; close the open CSV explicitly."""
        if self.reader is not None:
            self.reader.close()


def run_windows(recipe_bytes, windows, tail_seconds, store_root, instrument_path, workers, log=None):
    feed = RowFeed(store_root)
    try:
        return _run_windows(feed, recipe_bytes, windows, tail_seconds, store_root, instrument_path, workers, log)
    finally:
        feed.close()


def _run_windows(feed, recipe_bytes, windows, tail_seconds, store_root, instrument_path, workers, log):
    funding = team_data.load_funding(store_root)
    results, pending, started = {}, set(), time.time()

    def job(item):
        first = (item["start_s"] - WARMUP_SECONDS) * 1000
        end = (item["start_s"] + item["admission_seconds"] + tail_seconds + 60) * 1000
        return (recipe_bytes, item, tail_seconds, feed.slice(first, end), funding, str(instrument_path))

    if workers == 1:
        for n, item in enumerate(windows, 1):
            row = evaluate_window(job(item))
            results[row["id"]] = row
            if log and n % 50 == 0:
                log(f"{n}/{len(windows)} windows, {time.time() - started:.0f} s")
        return [results[w["id"]] for w in windows]
    queue = iter(windows)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        exhausted = False
        while True:
            while not exhausted and len(pending) < 2 * workers:
                item = next(queue, None)
                if item is None:
                    exhausted = True
                    break
                ram = free_ram_gb()
                if ram is not None and ram < RAM_FLOOR_GB:
                    raise SystemExit(f"free RAM {ram:.1f} GB < {RAM_FLOOR_GB} GB: run stopped, nothing reported as complete")
                pending.add(pool.submit(evaluate_window, job(item)))
            if not pending:
                break
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                row = future.result()
                results[row["id"]] = row
            if log and len(results) % 100 < len(done):
                rate = len(results) / max(time.time() - started, 1e-9)
                log(f"{len(results)}/{len(windows)} windows, {time.time() - started:.0f} s, "
                    f"eta {(len(windows) - len(results)) / rate:.0f} s")
    return [results[w["id"]] for w in windows]


# ----------------------------------------------------------------------------------------------------------- gates
def day_of(text):
    return text[:10]


def quarter_of(text):
    return f"{text[:4]}Q{(int(text[5:7]) - 1) // 3 + 1}"


def period_bounds(key):
    """[start, end) seconds of a calendar year 'YYYY' or quarter 'YYYYQn'."""
    year = int(key[:4])
    if "Q" in key:
        q = int(key[-1])
        start = datetime(year, 3 * q - 2, 1, tzinfo=timezone.utc)
        end = datetime(year + (q == 4), 1 if q == 4 else 3 * q + 1, 1, tzinfo=timezone.utc)
    else:
        start, end = datetime(year, 1, 1, tzinfo=timezone.utc), datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    return int(start.timestamp()), int(end.timestamp())


def span_periods(span_start, span_end, quarterly, extra=()):
    """(period, covered share) for every period the windows touch, plus periods that hold a trade anyway."""
    keys, t = [], span_start
    while t < span_end:
        key = quarter_of(utc(t)) if quarterly else utc(t)[:4]
        keys.append(key)
        t = period_bounds(key)[1]
    out = []
    for key in sorted(set(keys) | set(extra)):
        a, b = period_bounds(key)
        covered = max(0, min(b, span_end) - max(a, span_start))
        out.append((key, round(covered / (b - a), 4)))
    return out


def max_drawdown(values):
    equity = peak = worst = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst


def account_drawdown(trades, nets):
    """Largest drop of one 100 USDT account with the open position marked (D097): at every minute close in the exact
    engine, every 5 s at the exit price after the exit fee in candle v4.

    Trades run one after another; each carries its own marked path relative to the equity before entry
    (best, worst, in-trade peak-to-trough). None when a ledger predates these fields."""
    if any("mtm_worst_usdt" not in t for t in trades):
        return None
    offset = peak = worst = 0.0
    for trade, net in zip(trades, nets):
        shift = net - trade["net_usdt"]  # post-hoc costs of this scenario land at the exit
        worst = max(worst, peak - (offset + trade["mtm_worst_usdt"] + min(shift, 0.0)), trade["mtm_drawdown_usdt"] - min(shift, 0.0))
        peak = max(peak, offset + trade["mtm_best_usdt"])
        offset += net
        peak = max(peak, offset)
        worst = max(worst, peak - offset)
    return worst


def check(name, value, passed, rule, gate):
    return {"gate": gate, "check": name, "value": value, "rule": rule,
            "status": "NOT_EVALUATED" if passed is None else "PASS" if passed else "FAIL"}


def funding_policy(store):
    """D094 funding scenarios for a store: where its funding history starts and the conservative gap rate, the
    mean |rate| over the fixed D094 reference window (2026-06-29 .. DEV end) with a 1 bps floor."""
    funding = team_data.load_funding(store)
    lo, hi = (parse_utc(x) * 1000 for x in CHARTER["funding"]["reference_window_utc"])
    rates = [abs(float(rate)) for t, rate in funding if rate not in (None, "") and lo <= t < hi]
    mean_abs = statistics.fmean(rates) if rates else None
    runs = missing_settlement_runs([t for t, rate in funding if rate not in (None, "")], CHARTER["funding"]["settlement_hours_utc"])
    return {"first_real_funding_ms": funding[0][0] if funding else None,
            "first_real_funding_utc": team_data.iso(funding[0][0]) if funding else None,
            "settlement_hours_utc": list(CHARTER["funding"]["settlement_hours_utc"]),
            "reference_window_utc": list(CHARTER["funding"]["reference_window_utc"]),
            "mean_abs_rate_in_reference_window": mean_abs, "reference_rows": len(rates),
            "conservative_rate": max(mean_abs or 0.0, CHARTER["funding"]["conservative_floor_rate"]),
            "sensitivity_rate": CHARTER["funding"]["sensitivity_rate"], "known_rows": len(funding),
            # D119: settlements missing inside the history (the official archive lacks 2021-10 .. 2022-01-16 and a few
            # slots) are charged like those before the first record
            "missing_settlement_runs_ms": runs,
            "missing_settlement_runs_utc": [[team_data.iso(a), team_data.iso(b), (b - a) // 28_800_000 + 1] for a, b in runs]}


def missing_settlement_runs(times_ms, hours, tolerance_ms=3 * 3_600_000):
    """[first, last] slot instants (ms) of consecutive schedule slots between the first and last real record that have
    no record within 3 h (a few archive records sit seconds or hours off their slot)."""
    times = sorted(times_ms)
    if not times:
        return []
    runs, k = [], 0
    day = times[0] - times[0] % 86_400_000
    while day <= times[-1]:
        for hour in hours:
            slot = day + hour * 3_600_000
            if not times[0] < slot < times[-1]:
                continue
            while k < len(times) and times[k] < slot - tolerance_ms:
                k += 1
            if k < len(times) and abs(times[k] - slot) <= tolerance_ms:
                continue
            if runs and slot - runs[-1][1] <= 8 * 3_600_000:
                runs[-1][1] = slot
            else:
                runs.append([slot, slot])
        day += 86_400_000
    return runs


def gap_settlements(entry_s, exit_s, first_real_s, hours, missing_runs_ms=()):
    """Settlement instants t with entry < t <= exit that precede the first real funding record or fall in a run of
    settlements missing from the history."""
    found, day = [], entry_s - entry_s % 86400
    while day <= exit_s:
        for hour in hours:
            t = day + hour * 3600
            if entry_s < t <= exit_s and (first_real_s is None or t < first_real_s
                                          or any(a // 1000 <= t <= b // 1000 for a, b in missing_runs_ms)):
                found.append(t)
        day += 86400
    return found


def open_notional_at(executions, t):
    """Open position just before t at its average entry price, from the trade's own fills.

    Quantities when the ledger has them (entries and adds raise the cost basis, reductions remove their share);
    older ledgers fall back to entry minus reduction notionals."""
    if all("quantity" in fill for fill in executions):
        quantity = cost = 0.0
        for fill in executions:
            if parse_utc(fill["at"]) >= t:
                break
            if fill["kind"] in ("entry", "add"):
                quantity, cost = quantity + fill["quantity"], cost + fill["quantity"] * fill["price_usdt"]
            elif quantity > 0:
                keep = max(quantity - fill["quantity"], 0.0) / quantity
                quantity, cost = quantity * keep, cost * keep
        return cost
    open_ = 0.0
    for fill in executions:
        if parse_utc(fill["at"]) >= t:
            break
        open_ = 0.0 if fill["kind"] == "close" else open_ + (fill["notional_usdt"] if fill["kind"] in ("entry", "add") else -fill["notional_usdt"])
    return max(open_, 0.0)


def funding_charge(trade, policy, rate):
    """Funding charged against the position for every gap settlement it held through (D094); 0 when rate is 0."""
    if not rate or not policy:
        return 0.0
    first = policy["first_real_funding_ms"] // 1000 if policy.get("first_real_funding_ms") else None
    times = gap_settlements(parse_utc(trade["entry_at"]), parse_utc(trade["exit_at"]), first, policy["settlement_hours_utc"],
                            policy.get("missing_settlement_runs_ms") or ())
    return sum(open_notional_at(trade["executions"], t) * rate for t in times)


def single_account(trades):
    """One account with one net position (D097): in entry order, a trade that opens before the kept previous one
    has exited is dropped and counted."""
    kept, dropped, busy_until = [], [], None
    for trade in sorted(trades, key=lambda t: (t["entry_at"], t["exit_at"], t["window_id"])):
        if busy_until is not None and trade["entry_at"] < busy_until:
            dropped.append(trade)
            continue
        kept.append(trade)
        busy_until = trade["exit_at"]
    return kept, dropped


def canonical_sha256(value):
    """sha256 of compact sorted JSON in UTF-8: the prefix hash of the gate D procedure v1.1 section 4."""
    return sha256_bytes(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def declared_sample(trades, n, clipped=None):
    """The gate D sample of a declared N (D124, D127 p. 4, gate D procedure sections 2-3): the one account in entry
    order, without exits forced by the data, and only its first N trades. Returns the selected trades and what was
    left out; once the N-th trade has exited, nothing that enters later can change the verdict.

    clipped: for an exact-engine ledger, the windows whose tail the data end cut short. A forced close anywhere else is
    the 24 h window cut, not the end of the data (D128): its counting before the N-th exit sends the verdict to the
    sequential run."""
    kept, dropped = single_account(trades)
    closed = [t for t in kept if t["exit_reason"] not in NOT_CLOSED_FOR_GATE_D]
    selected = closed[:n]
    cutoff = selected[-1]["exit_at"] if len(selected) == n else None
    before = (lambda t: True) if cutoff is None else (lambda t: t["entry_at"] < cutoff)
    excluded = Counter(t["exit_reason"] for t in kept if t["exit_reason"] in NOT_CLOSED_FOR_GATE_D and before(t))
    info = {"declared_n": n, "reached": cutoff is not None, "closed_trades_eligible": len(closed),
            "closed_trades_in_prefix": len(selected), "later_closed_trades_ignored": len(closed) - len(selected),
            "excluded_before_nth_exit": {reason: excluded.get(reason, 0) for reason in NOT_CLOSED_FOR_GATE_D},
            "prefix_first_entry_at": selected[0]["entry_at"] if selected else None, "prefix_last_exit_at": cutoff,
            "prefix_trades_sha256": canonical_sha256(selected),
            **({} if clipped is None else {"window_cut_closes_before_nth_exit": sum(
                1 for t in kept if t["exit_reason"] == "forced_close_end_of_period" and t["window_id"] not in clipped and before(t))}),
            "rule": "one account in entry order; exit_reason forced_close_end_of_period / data_gap_exit excluded; "
                    "first N by entry_at; D2 and D4 on these only, account drawdown up to the N-th exit; a data or "
                    "engine error before the N-th exit (or anywhere before N is reached) gives NEEDS_DATA_FIX, not a verdict"}
    overlap = [t for t in dropped if before(t)], [t for t in kept if before(t)]
    return selected, overlap, info


def epoch_summary(name, rule, trades, nets, stressed, quarters, span_start, span_end):
    """Totals and quarter stability inside one epoch [start, end) by trade entry (D100/D102)."""
    start, end = parse_utc(rule["start"]), parse_utc(rule["end"])
    coverage = max(0, min(end, span_end) - max(start, span_start)) / (end - start)
    picked = [(t, n, s) for t, n, s in zip(trades, nets, stressed) if start <= parse_utc(t["entry_at"]) < end]
    inside = [q for q in quarters if period_bounds(q["quarter"])[0] >= start and period_bounds(q["quarter"])[1] <= end]
    full = [q for q in inside if q["full"]]
    active = [q for q in full if q["active"]]
    share = sum(q["net_usdt"] > 0 for q in active) / len(active) if active else None
    return {"epoch": name, "start": rule["start"], "end": rule["end"], "coverage": round(coverage, 4),
            "covered": coverage >= CHARTER["K"]["full_quarter_coverage"], "closed_trades": len(picked),
            "net_usdt": round(sum(n for _, n, _ in picked), 6), "stress_net_usdt": round(sum(s for _, _, s in picked), 6),
            "stress_net_pct_of_capital": round(sum(s for _, _, s in picked) / CAPITAL * 100, 4),
            "quarters_full": len(full), "quarters_active": len(active), "quarters_active_positive": sum(q["net_usdt"] > 0 for q in active),
            "positive_active_quarter_share": round(share, 4) if share is not None else None}


def gate_metrics(ledger, scenario="base", charter=CHARTER):
    """All gate numbers from a ledger for one funding scenario: pure arithmetic over trades and windows.

    One account (D097): overlapping trades of neighbouring windows are dropped and counted; positions still open
    at the end of the period were force-closed at the last mark by the engine run. base: engine numbers (no
    funding where the data has none). conservative / sensitivity: every 8-hour settlement before the first real
    funding record is charged against the open position (D094). Quarters, years and epochs use the entry time;
    drawdowns and daily PnL follow the exits.
    """
    k, stress_bps = charter["K"], charter["costs"]["stress_extra_bps_per_execution"]
    policy = ledger.get("funding_policy")
    rate = {"base": 0.0, "conservative": (policy or {}).get("conservative_rate", 0.0),
            "sensitivity": (policy or {}).get("sensitivity_rate", 0.0)}[scenario]
    windows = ledger["windows"]
    trades, dropped = single_account(ledger["trades"])
    charges = [funding_charge(t, policy, rate) for t in trades]
    nets = [t["net_usdt"] - c for t, c in zip(trades, charges)]
    stressed = [n - t["executed_notional_usdt"] * stress_bps / 10000 for t, n in zip(trades, nets)]
    net, stress = sum(nets), sum(stressed)
    best = sorted(nets, reverse=True)[:k["drop_best_trades"]]
    gross_profit = sum(n for n in nets if n > 0)
    best_share = (max(nets) / gross_profit) if gross_profit > 0 else None
    span_start = min(parse_utc(w["start_utc"]) for w in windows)
    span_end = max(parse_utc(w["start_utc"]) + w["admission_seconds"] for w in windows)
    by_year, by_quarter, by_day = defaultdict(float), defaultdict(float), defaultdict(float)
    stress_year, stress_quarter = defaultdict(float), defaultdict(float)
    count_quarter = Counter()
    for trade, value, stressed_value in zip(trades, nets, stressed):
        year, quarter = trade["entry_at"][:4], quarter_of(trade["entry_at"])
        by_year[year] += value
        stress_year[year] += stressed_value
        by_quarter[quarter] += value
        stress_quarter[quarter] += stressed_value
        count_quarter[quarter] += 1
        by_day[day_of(trade["exit_at"])] += value
    years = [{"year": key, "coverage": cov, "net_usdt": round(by_year.get(key, 0.0), 6),
              "net_pct_of_capital": round(by_year.get(key, 0.0) / CAPITAL * 100, 4),
              "stress_net_usdt": round(stress_year.get(key, 0.0), 6)} for key, cov in span_periods(span_start, span_end, False, by_year)]
    full_share, min_trades = k["full_quarter_coverage"], k["active_quarter_min_trades"]
    quarters = [{"quarter": key, "coverage": cov, "full": cov >= full_share, "trades": count_quarter.get(key, 0),
                 "active": cov >= full_share and count_quarter.get(key, 0) >= min_trades,
                 "net_usdt": round(by_quarter.get(key, 0.0), 6), "stress_net_usdt": round(stress_quarter.get(key, 0.0), 6)}
                for key, cov in span_periods(span_start, span_end, True, by_quarter)]
    by_exit = sorted(zip(trades, nets, stressed), key=lambda x: (x[0]["exit_at"], x[0]["entry_at"]))
    exit_nets, exit_stressed = [n for _, n, _ in by_exit], [s for _, _, s in by_exit]
    account = account_drawdown([t for t, _, _ in by_exit], exit_nets)
    closed_drawdown = max_drawdown(exit_nets)
    worst_year = min(years, key=lambda y: y["net_usdt"]) if years else None
    sides = {}
    for side in ("long", "short"):
        part = [n for t, n in zip(trades, nets) if t["side"] == side]
        if part:
            sides[side] = {"trades": len(part), "net_usdt": round(sum(part), 6), "win_rate": round(sum(n > 0 for n in part) / len(part), 4)}
    holds = [parse_utc(t["exit_at"]) - parse_utc(t["entry_at"]) for t in trades]
    total = len(trades) + len(dropped)
    return {
        "scenario": scenario, "gap_funding_rate": rate, "gap_funding_charged_usdt": round(sum(charges), 6),
        "funding_policy_present": policy is not None,
        "closed_trades": len(trades), "net_usdt": round(net, 6), "stress_net_usdt": round(stress, 6),
        "net_pct_of_capital": round(net / CAPITAL * 100, 4), "fees_usdt": round(sum(t["fees_usdt"] for t in trades), 6),
        "funding_usdt": round(sum(t["funding_usdt"] for t in trades) - sum(charges), 6),
        "executed_notional_usdt": round(sum(t["executed_notional_usdt"] for t in trades), 4),
        "net_bps_per_trade_of_first_notional": round(statistics.fmean(n / t["first_notional_usdt"] * 10000 for t, n in zip(trades, nets)), 3) if trades else None,
        "mean_hold_hours": round(statistics.fmean(holds) / 3600, 3) if holds else None,
        "win_rate": round(sum(n > 0 for n in nets) / len(nets), 4) if nets else None,
        "by_side": sides, "exit_reasons": dict(Counter(t["exit_reason"] for t in trades)),
        "overlap_dropped_trades": len(dropped), "overlap_dropped_share": round(len(dropped) / total, 6) if total else 0.0,
        "overlap_dropped_net_usdt": round(sum(t["net_usdt"] for t in dropped), 6),
        "forced_closes": sum(bool(t.get("forced_close")) for t in trades),
        "net_without_best_trades_usdt": round(net - sum(best), 6), "best_trades_dropped": len(best),
        "gross_profit_usdt": round(gross_profit, 6), "best_trade_usdt": round(max(nets), 6) if nets else None,
        "best_trade_share_of_gross_profit": round(best_share, 6) if best_share is not None else None,
        "account_drawdown_pct_of_capital": None if account is None else round(account / CAPITAL * 100, 4),
        "closed_trade_drawdown_pct_of_capital": round(closed_drawdown / CAPITAL * 100, 4),
        "stress_closed_trade_drawdown_pct_of_capital": round(max_drawdown(exit_stressed) / CAPITAL * 100, 4),
        "worst_in_trade_move_usdt": round(min(t["mtm_worst_usdt"] for t in trades), 6) if trades and account is not None else None,
        "worst_window_intrawindow_drawdown_pct": round(max(w["max_drawdown_pct"] for w in windows), 4),
        "years": years, "worst_year": worst_year, "quarters": quarters,
        "epochs": {name: epoch_summary(name, rule, trades, nets, stressed, quarters, span_start, span_end)
                   for name, rule in k["epochs"].items()},
        "windows": len(windows), "windows_complete": sum(w["status"] == "COMPLETE" for w in windows),
        "windows_with_errors": [w["id"] for w in windows if w.get("errors") or (w["status"] != "COMPLETE" and not w.get("forced_close"))],
        "windows_above_cash": sum(w["pnl_usdt"] > 1e-9 for w in windows), "windows_below_cash": sum(w["pnl_usdt"] < -1e-9 for w in windows),
        "windows_without_trades": sum(w["trades"] == 0 for w in windows),
        "risk_limit_windows": sum(1 for w in windows if w["event_counts"].get("risk_limit")),
        "daily_net_usdt": {day: round(value, 8) for day, value in sorted(by_day.items())},
        "span_start_utc": utc(span_start), "span_end_utc": utc(span_end),
    }


def gate_verdicts(metrics, data_set, paperwork=None, charter=CHARTER, declared=None, marking="every minute close"):
    k, d = charter["K"], charter["D"]
    overlap_ok = metrics["overlap_dropped_share"] <= k["max_overlap_dropped_share"]
    rows = [check("no_engine_or_data_errors", len(metrics["windows_with_errors"]), not metrics["windows_with_errors"],
                  "every window ran without a data gap or engine failure", "K,D"),
            check("funding_policy_present", metrics["funding_policy_present"], metrics["funding_policy_present"] or metrics["scenario"] == "base",
                  "conservative D094 scenario actually applied", "K,D"),
            check("single_account_overlap", metrics["overlap_dropped_share"], overlap_ok,
                  f"<= {k['max_overlap_dropped_share']:.0%} trades dropped as overlapping, else the sequential run decides", "K,D"),
            check("net_positive", metrics["net_usdt"], metrics["net_usdt"] > 0, "net > 0 after modelled costs", "K,D"),
            check("stress_net_positive", metrics["stress_net_usdt"], metrics["stress_net_usdt"] > 0, "net > 0 with +5 bps per execution", "K,D")]
    if data_set == "DEV":
        worst, e = metrics["worst_year"], metrics["epochs"]
        e1, e2, tail = e["E1"], e["E2"], e["E2_tail"]
        r1, r2, rt = k["epochs"]["E1"], k["epochs"]["E2"], k["epochs"]["E2_tail"]

        def epoch(row, passed):
            return passed if row["covered"] else None
        rows += [check("closed_trades", metrics["closed_trades"], metrics["closed_trades"] >= k["min_closed_trades"], f">= {k['min_closed_trades']} on DEV", "K"),
                 check("worst_year_pct_of_capital", worst and worst["net_pct_of_capital"],
                       worst is not None and worst["net_pct_of_capital"] >= k["worst_year_floor_pct_of_capital"],
                       f">= {k['worst_year_floor_pct_of_capital']}% of {CAPITAL} USDT in every calendar year (by entry)", "K"),
                 check("net_without_best_trades", metrics["net_without_best_trades_usdt"], metrics["net_without_best_trades_usdt"] >= 0,
                       f">= 0 without the {k['drop_best_trades']} best trades", "K"),
                 check("E2_net_and_stress", [e2["net_usdt"], e2["stress_net_usdt"]], epoch(e2, e2["net_usdt"] > 0 and e2["stress_net_usdt"] > 0),
                       f"E2 {r2['start']}..{r2['end'][:10]}: net > 0 and stress > 0", "K"),
                 check("E2_closed_trades", e2["closed_trades"], epoch(e2, e2["closed_trades"] >= r2["min_closed_trades"]),
                       f"E2 >= {r2['min_closed_trades']} closed trades", "K"),
                 check("E2_active_quarters", e2["quarters_active"], epoch(e2, e2["quarters_active"] >= r2["min_active_quarters"]),
                       f"E2 >= {r2['min_active_quarters']} active quarters (>= {k['active_quarter_min_trades']} trades by entry)", "K"),
                 check("E2_positive_active_quarter_share", e2["positive_active_quarter_share"],
                       epoch(e2, e2["positive_active_quarter_share"] is not None and e2["positive_active_quarter_share"] >= r2["min_positive_quarter_share"]),
                       f"E2 >= {r2['min_positive_quarter_share']:.0%} of active quarters with net > 0", "K"),
                 check("E2_tail", [tail["closed_trades"], tail["net_usdt"], tail["stress_net_usdt"]],
                       epoch(tail, tail["closed_trades"] >= rt["min_closed_trades"] and tail["net_usdt"] > 0),
                       f"tail {rt['start']}..{rt['end'][:10]}: >= {rt['min_closed_trades']} trades and net > 0 (stress shown)", "K"),
                 check("E1_stress_floor", e1["stress_net_pct_of_capital"], epoch(e1, e1["stress_net_pct_of_capital"] >= r1["min_stress_pct_of_capital"]),
                       f"E1 {r1['start']}..{r1['end']}: stress >= {r1['min_stress_pct_of_capital']}% of capital", "K"),
                 check("validate_lint", (paperwork or {}).get("lint_warnings"), (paperwork or {}).get("valid"), "production validate + lint", "K"),
                 check("prereg_and_trials", (paperwork or {}).get("prereg"), (paperwork or {}).get("prereg_and_trials"),
                       "PREREG file + TRIALS.jsonl given (content reviewed by coord)", "K")]
    else:
        drawdown = metrics["account_drawdown_pct_of_capital"]
        if declared is None:
            count = check("closed_trades", metrics["closed_trades"], metrics["closed_trades"] >= d["min_closed_trades"],
                          f">= {d['min_closed_trades']} else INSUFFICIENT", "D")
        else:
            count = check("closed_trades", metrics["closed_trades"], declared["reached"],
                          f"first N = {declared['declared_n']} closed trades by entry (data-forced exits excluded), else INSUFFICIENT", "D")
        until = "" if declared is None else ", up to the N-th exit"
        if declared is not None and "window_cut_closes_before_nth_exit" in declared:
            cuts = declared["window_cut_closes_before_nth_exit"]
            rows.append(check("window_cut_closes_before_nth_exit", cuts, cuts == 0, "exact engine: no forced close at the end of a "
                              "full 24 h window before the N-th exit, else the sequential run decides (D128)", "D"))
        rows += [count,
                 check("best_trade_share_of_gross_profit", metrics["best_trade_share_of_gross_profit"],
                       metrics["best_trade_share_of_gross_profit"] is not None and metrics["best_trade_share_of_gross_profit"] <= d["max_best_trade_share_of_gross_profit"],
                       f"<= {d['max_best_trade_share_of_gross_profit']:.0%}", "D"),
                 check("account_drawdown_pct_of_capital", drawdown, None if drawdown is None else drawdown <= d["max_drawdown_pct_of_capital"],
                       f"<= {d['max_drawdown_pct_of_capital']}% of {CAPITAL} USDT, account with open position marked {marking}{until}", "D"),
                 check("live_rounds", None, None, ">= 2 owner Live rounds of 24 h+ within 3 bps of the model (Judge, outside this tool)", "D")]
    failed = [r for r in rows if r["status"] == "FAIL" and r["check"] != "single_account_overlap"]
    integrity = ("no_engine_or_data_errors", "funding_policy_present")
    if not overlap_ok:
        verdict = "NEEDS_SEQUENTIAL_RUN"
    elif declared is not None:  # gate D procedure v1.1 section 4; a broken run is not judged, so no final FAIL from it
        verdict = ("NEEDS_SEQUENTIAL_RUN" if declared.get("window_cut_closes_before_nth_exit")
                   else "NEEDS_DATA_FIX" if any(r["check"] in integrity for r in failed) else "INSUFFICIENT" if not declared["reached"]
                   else "FAIL" if failed else "PASS_PENDING_LIVE")
    elif data_set == "DEV":
        verdict = "FAIL" if failed else "PASS" if all(r["status"] == "PASS" for r in rows) else "NOT_ALL_CHECKS_EVALUATED"
    elif any(r["check"] == "closed_trades" and r["status"] == "FAIL" for r in rows):
        verdict = "INSUFFICIENT"
    else:
        verdict = "FAIL" if failed else "PASS_PENDING_LIVE"
    return {"verdict": verdict, "checks": rows}


def gates_markdown(gates):
    m, v, base = gates["metrics"], gates["verdict"], gates["metrics_base"]
    policy = gates.get("funding_policy") or {}
    lines = [f"# Team evaluator · {gates['data_set']} · {gates['recipe_name']}", "",
             f"- recipe_sha256 `{gates['recipe_sha256']}` · evaluator `{gates['evaluator_sha256'][:16]}` · engine code `{gates['code_sha256'][:16]}`",
             f"- span {m['span_start_utc']} → {m['span_end_utc']}, windows {m['windows']} ({m['windows_complete']} complete), "
             f"one account of {CAPITAL} USDT; overlapping trades dropped: {m['overlap_dropped_trades']} ({m['overlap_dropped_share']:.2%}), "
             f"forced closes at the end of the period: {m['forced_closes']}",
             f"- verdict scenario: **{m['scenario']}** — every 8 h settlement before {policy.get('first_real_funding_utc')} "
             f"and the {sum(r[2] for r in policy.get('missing_settlement_runs_utc') or [])} settlements missing inside the history "
             f"charged at {m['gap_funding_rate']} of open notional (D094)",
             f"- **verdict: {v['verdict']}**", "", "| gate | check | value | rule | status |", "|---|---|---|---|---|"]
    if gates.get("first_n"):
        n = gates["first_n"]
        lines[-3:-3] = [f"- declared N = {n['declared_n']}: {n['closed_trades_in_prefix']} of {n['declared_n']} closed trades in the prefix"
                        + (f", N-th exit {n['prefix_last_exit_at']}" if n["reached"] else "")
                        + f"; excluded before it {n['excluded_before_nth_exit']}; later closed trades ignored {n['later_closed_trades_ignored']}; "
                        f"prefix sha256 `{n['prefix_trades_sha256']}`"]
    lines += [f"| {r['gate']} | {r['check']} | {r['value']} | {r['rule']} | {r['status']} |" for r in v["checks"]]
    lines += ["", "| scenario | net USDT | stress USDT | gap funding charged | trades | win rate | account DD % | closed-trade DD % |",
              "|---|---|---|---|---|---|---|---|"]
    for row in [base, m] + ([gates["metrics_sensitivity"]] if gates.get("metrics_sensitivity") else []):
        lines.append(f"| {row['scenario']} | {row['net_usdt']} | {row['stress_net_usdt']} | {row['gap_funding_charged_usdt']} | "
                     f"{row['closed_trades']} | {row['win_rate']} | {row['account_drawdown_pct_of_capital']} | {row['closed_trade_drawdown_pct_of_capital']} |")
    lines += ["", f"Fees {m['fees_usdt']} USDT, mean hold {m['mean_hold_hours']} h, worst in-trade move {m['worst_in_trade_move_usdt']} USDT, "
                  f"by side {m['by_side']}, exits {m['exit_reasons']}."]
    if gates["data_set"] == "DEV":
        lines += ["", "| epoch | span | coverage | trades | net USDT | stress USDT | active quarters + / all |", "|---|---|---|---|---|---|---|"]
        lines += [f"| {e['epoch']} | {e['start']} → {e['end'][:10]} | {e['coverage']} | {e['closed_trades']} | {e['net_usdt']} | "
                  f"{e['stress_net_usdt']} | {e['quarters_active_positive']}/{e['quarters_active']} |" for e in m["epochs"].values()]
        lines += ["", f"Years and quarters by trade entry, scenario {m['scenario']}.", "",
                  "| year | coverage | net USDT | % capital | stress USDT |", "|---|---|---|---|---|"]
        lines += [f"| {y['year']} | {y['coverage']} | {y['net_usdt']} | {y['net_pct_of_capital']} | {y['stress_net_usdt']} |" for y in m["years"]]
        lines += ["", "| quarter | coverage | trades | active | net USDT | stress USDT |", "|---|---|---|---|---|---|"]
        lines += [f"| {q['quarter']} | {q['coverage']} | {q['trades']} | {'yes' if q['active'] else ''} | {q['net_usdt']} | {q['stress_net_usdt']} |"
                  for q in m["quarters"]]
    report = gates.get("v4_report")
    if report:
        lines += ["", f"Schema 4 · execution model `{report['execution_model_id']}` · EVAL_ONLY (Live/Demo/PAPER refuse it in this release).",
                  f"Stop-slippage stress ({report['stop_slippage_stress']['rule']}): {report['stop_slippage_stress']['stop_exits']} stop exits, "
                  f"extra {report['stop_slippage_stress']['extra_cost_usdt']} USDT, net after {report['stop_slippage_stress']['net_after_usdt']} USDT; "
                  f"minutes where the stop beat the target: {report['ambiguous_minute_stops']}; HOLD below the minimum lot: "
                  f"{report['sizing']['hold_too_small']}; funding share of net: {report['funding_share_of_net']}.",
                  ("Quarters where the entry rule is UNKNOWN more than half of the time (their E1/E2 checks are formal): "
                   + (", ".join(report.get("quarters_mostly_unknown") or []) or "none") + "." if "quarters_mostly_unknown" in report else
                   f"Mean share of time with the entry rule UNKNOWN: {report.get('entry_unknown_share_mean')}.")]
        if report["maker_sensitivity"]:
            lines += ["", "| maker pass-through bps | adverse selection bps | trades | net USDT (engine, before gap funding) | unfilled |", "|---|---|---|---|---|"]
            lines += [f"| {r['through_bps']} | {r['adverse_bps'] if r['adverse_bps'] is not None else 'all maker as taker (floor)'} | "
                      f"{r['trades']} | {r['net_usdt']} | {r['maker_unfilled']} |" for r in report["maker_sensitivity"]]
            lines += ["", f"Lowest net of the grid and the floor: {min(r['net_usdt'] for r in report['maker_sensitivity'])} USDT."]
    lines += ["", "MODEL evidence: candle-path quotes, 1-tick spread, fixed visible size; not a market recording, not Jev, "
                  "not proof of edge. Stress and gap funding are post-hoc cost add-ons; fills are not re-simulated."]
    return "\n".join(lines) + "\n"


def declared_metrics(ledger, scenario, sample):
    """gate_metrics over the declared sample only; the one-account overlap and the error check keep looking at the
    whole run up to the N-th exit, so a dropped or failed window before it still counts and nothing after it does."""
    selected, (dropped, kept), info = sample
    m = gate_metrics({**ledger, "trades": selected}, scenario)
    total = len(dropped) + len(kept)
    m.update(overlap_dropped_trades=len(dropped), overlap_dropped_share=round(len(dropped) / total, 6) if total else 0.0,
             overlap_dropped_net_usdt=round(sum(t["net_usdt"] for t in dropped), 6))
    m["forced_closes"] = sum(info["excluded_before_nth_exit"].values())
    if info["reached"]:
        cutoff, starts = info["prefix_last_exit_at"], {w["id"]: w["start_utc"] for w in ledger["windows"]}
        gaps = ledger.get("data_gaps") if str(ledger.get("engine", "")).startswith("candle-v4") else None

        def early(window_id):  # v4 knows when each hole starts; the exact engine only which window had it
            if gaps is not None:
                return any(at[:10] == starts[window_id][:10] and at < cutoff for at, _ in gaps)
            return starts[window_id] < cutoff
        m["windows_with_errors"] = [w for w in m["windows_with_errors"] if early(w)]
    return m


def build_gates(ledger, paperwork=None, store=None, declared_n=None):
    """Gates for a ledger. A ledger without a funding policy (older runs) takes it from its store; if neither has
    one, the conservative scenario cannot be applied and the funding check fails instead of passing silently.
    declared_n: gate D on the first N closed trades only (declared_sample); SEALED/FORWARD ledgers only."""
    if not ledger.get("funding_policy"):
        source = store or ledger.get("store")
        found = funding_policy(source) if source and Path(source, team_data.CATALOG).is_file() else None
        ledger = {**ledger, "funding_policy": found}
    declared = None
    if declared_n is not None:
        if ledger["data_set"] == "DEV":
            raise SystemExit("--declared-n is the gate D sample rule: SEALED/FORWARD ledgers only")
        if int(declared_n) != declared_n or declared_n < CHARTER["D"]["min_closed_trades"]:
            raise SystemExit(f"declared N must be a whole number >= {CHARTER['D']['min_closed_trades']} (charter gate D3)")
        exact = not str(ledger.get("engine", "")).startswith("candle-v4")
        clipped = {w["id"] for w in ledger["windows"]
                   if w.get("tail_policy_seconds") is not None and w.get("tail_seconds", w["tail_policy_seconds"]) < w["tail_policy_seconds"]}
        sample = declared_sample(ledger["trades"], int(declared_n), clipped if exact else None)
        declared = sample[2]
        metrics_of = lambda scenario: declared_metrics(ledger, scenario, sample)
    else:
        metrics_of = lambda scenario: gate_metrics(ledger, scenario)
    base, conservative = metrics_of("base"), metrics_of("conservative")
    sensitivity = None
    if (conservative["mean_hold_hours"] or 0) > CHARTER["funding"]["sensitivity_min_mean_hold_hours"]:
        sensitivity = metrics_of("sensitivity")
    marking = ("every 5 s at the exit price after the exit fee (" + str(ledger.get("execution_model_id")) + "), stricter than the minute close"
               if str(ledger.get("engine", "")).startswith("candle-v4") else "every minute close")
    gates = {"schema": SCHEMA_GATES, "created_at": utc(time.time()), "data_set": ledger["data_set"],
             "recipe_name": ledger["recipe_name"], "recipe_sha256": ledger["recipe_sha256"],
             "code_sha256": ledger["code_sha256"], "evaluator_sha256": sha256_file(__file__),
             "ledger_evaluator_sha256": ledger["evaluator_sha256"],
             "ledger_trades_sha256": sha256_bytes(json.dumps(ledger["trades"], sort_keys=True).encode()),
             "charter": CHARTER, "funding_policy": ledger.get("funding_policy"), "metrics": conservative,
             "metrics_base": base, "metrics_sensitivity": sensitivity, "v4_report": ledger.get("v4_report"),
             "execution_model_id": ledger.get("execution_model_id"), "eval_only": ledger.get("eval_only"),
             "verdict": gate_verdicts(conservative, ledger["data_set"], paperwork or ledger.get("paperwork"),
                                      declared=declared, marking=marking)}
    if declared is not None:
        gates["first_n"] = declared
    if ledger["data_set"] != "DEV":  # Judge reports aggregates only; per-day/per-quarter detail stays in the ledger
        for key in ("metrics", "metrics_base", "metrics_sensitivity"):
            if gates[key]:
                gates[key] = {k: v for k, v in gates[key].items() if k not in ("daily_net_usdt", "quarters", "years", "epochs")}
        if gates["v4_report"]:
            gates["v4_report"] = v4_report_aggregate(gates["v4_report"])
    return gates


def v4_report_aggregate(report):
    """The v4 report without per-quarter rows, for SEALED/FORWARD gates."""
    shares = list((report.get("entry_unknown_share_by_quarter") or {}).values())
    sizing = {k: v for k, v in report["sizing"].items() if k != "actual_mean_notional_by_quarter"}
    clean = {k: v for k, v in report.items() if k not in ("entry_unknown_share_by_quarter", "quarters_mostly_unknown", "sizing")}
    return {**clean, "sizing": sizing, "entry_unknown_share_mean": round(sum(shares) / len(shares), 4) if shares else None}


# ----------------------------------------------------------------------------------------------------------- clone
def clone_check(a, b, charter=CHARTER, epoch=None):
    """Share of A's entries with a B entry within +-30 min, both directions, and daily PnL correlation. The verdict
    takes the larger of the two shares (D124), so a candidate hidden inside a busier one is caught too; epoch="E2"
    limits everything to the E2 epoch."""
    window = charter["clone"]["entry_window_minutes"] * 60

    def entries(ledger):
        return sorted(parse_utc(t["entry_at"]) for t in ledger["trades"])

    def overlap(xs, ys):
        if not xs:
            return None
        hits = 0
        for x in xs:
            i = bisect_right(ys, x + window)
            hits += i > 0 and ys[i - 1] >= x - window
        return hits / len(xs)

    def daily(ledger):
        out = defaultdict(float)
        for t in ledger["trades"]:
            out[day_of(t["exit_at"])] += t["net_usdt"]
        return out

    def pearson(xs, ys):
        if len(xs) < 3:
            return None
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        sy = math.sqrt(sum((y - my) ** 2 for y in ys))
        if not sx or not sy:
            return None
        return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)

    da, db = daily(a), daily(b)
    start = max(min(w["start_utc"] for w in a["windows"]), min(w["start_utc"] for w in b["windows"]))[:10]
    end = min(max(w["start_utc"] for w in a["windows"]), max(w["start_utc"] for w in b["windows"]))[:10]
    if epoch:  # only the epoch's days count (D124 "E2 only")
        rule = charter["K"]["epochs"][epoch]
        start = max(start, rule["start"][:10])
        end = min(end, utc(parse_utc(rule["end"]) - 1)[:10])
    common = (parse_utc(start), parse_utc(end) + 86400)  # entries outside the shared period would dilute the overlap
    ea = [x for x in entries(a) if common[0] <= x < common[1]]
    eb = [x for x in entries(b) if common[0] <= x < common[1]]
    days_active = sorted(day for day in set(da) | set(db) if start <= day <= end)
    first, last = parse_utc(start), parse_utc(end)
    days_all = [utc(t)[:10] for t in range(first, last + 1, 86400)]
    corr_active = pearson([da.get(x, 0.0) for x in days_active], [db.get(x, 0.0) for x in days_active])
    corr_all = pearson([da.get(x, 0.0) for x in days_all], [db.get(x, 0.0) for x in days_all])
    ab, ba = overlap(ea, eb), overlap(eb, ea)
    limit = charter["clone"]
    correlations = [c for c in (corr_active, corr_all) if c is not None]
    shares = [x for x in (ab, ba) if x is not None]
    clone = bool((shares and max(shares) > limit["max_entry_overlap"]) or (correlations and max(correlations) > limit["max_daily_pnl_correlation"]))
    return {"schema": "team-eval-clone/v1", "a": a["recipe_sha256"], "b": b["recipe_sha256"], "common_days": [start, end],
            "entries_a": len(ea), "entries_b": len(eb), "entry_overlap_a_in_b": None if ab is None else round(ab, 4),
            "entry_overlap_b_in_a": None if ba is None else round(ba, 4),
            "entry_overlap_max": round(max(shares), 4) if shares else None, "epoch": epoch,
            "daily_pnl_correlation_active_days": None if corr_active is None else round(corr_active, 4),
            "daily_pnl_correlation_all_days": None if corr_all is None else round(corr_all, 4),
            "active_days": len(days_active), "all_days": len(days_all), "rule": limit,
            "verdict": "CLONE" if clone else "DISTINCT",
            "note": "overlap = share of one ledger's entries with an entry of the other within the window; the larger "
                    "share and the larger correlation are compared with the limits (D124)."}


# ----------------------------------------------------------------------------------------------------------- cli
def paperwork_of(strategy, args):
    from trading_lab.workshop_diagnostics import diagnostic_rules
    from trading_lab.workshop_tools import lint_rules
    lint = lint_rules(diagnostic_rules(strategy))
    out = {"valid": bool(lint.get("valid")), "lint_warnings": [w["code"] for w in lint.get("warnings", [])], "prereg": None,
           "trials": None, "prereg_and_trials": None}
    if args.prereg:
        out["prereg"] = {"file": str(args.prereg), "sha256": sha256_file(args.prereg)}
    if args.trials:
        lines = [line for line in args.trials.read_text(encoding="utf-8").splitlines() if line.strip()]
        out["trials"] = {"file": str(args.trials), "sha256": sha256_file(args.trials), "lines": len(lines)}
    if args.prereg and args.trials:
        out["prereg_and_trials"] = bool(out["trials"]["lines"])
    return out


def command_run(args):
    from trading_lab.arena_strategies import validate_strategy
    from trading_lab.strategy_training import code_hash
    from okx_tape_model import MODEL_ASSUMPTIONS
    raw = args.recipe.read_bytes()
    if len(raw) > 131072:
        raise SystemExit("recipe too large (workshop limit 131072 bytes)")
    payload = json.loads(raw.decode("utf-8-sig"))
    if isinstance(payload, dict) and payload.get("strategy_schema_version") == 4:
        return command_run_v4(args, raw, payload)
    strategy = validate_strategy(payload)
    rules = strategy["rules"]
    hold = rules["max_hold_seconds"]
    tail = int(hold) + FINITE_TAIL_EXTRA_SECONDS if hold is not None else int(args.tail_seconds)
    if not 600 <= tail <= 86400 + FINITE_TAIL_EXTRA_SECONDS:
        raise SystemExit("tail must be within 600..86820 s")
    store = Path(args.store)
    data_set = args.set.upper()
    admission = int(args.admission_hours * 3600)
    catalog, windows, clipped = plan_windows(store, data_set, args.start, args.end, admission,
                                             int((args.step_hours or args.admission_hours) * 3600), tail)
    segments = catalog["segments"]
    workers = max(1, min(args.workers, MAX_WORKERS))
    paperwork = paperwork_of(strategy, args)
    sys.path.insert(0, str(HERE))
    from research_limits import ResearchBusy, research_slot
    started = time.time()
    try:
        with research_slot(ROOT, "team_eval", slots=2 if workers > 4 else 1):
            rows = run_windows(raw, windows, tail, store, args.instrument, workers,
                               log=(lambda text: print(text, file=sys.stderr, flush=True)) if not args.quiet else None)
    except ResearchBusy as error:
        raise SystemExit(str(error)) from None
    trades = [t for row in rows for t in row.pop("closed")]
    ledger = {"schema": SCHEMA_LEDGER, "created_at": utc(time.time()), "evaluator_version": EVALUATOR_VERSION,
              "data_set": data_set, "author_scope": args.author, "label": args.label,
              "recipe_file": str(args.recipe), "recipe_file_sha256": sha256_bytes(raw), "recipe_sha256": strategy["recipe_sha256"],
              "recipe_name": strategy["name"], "strategy_schema_version": strategy.get("strategy_schema_version", 1),
              "side": rules["side"], "tail_seconds": tail, "capital_usdt_per_window": CAPITAL,
              "admission_seconds": admission, "windows_tail_clipped_at_data_end": clipped, "workers": workers,
              "runtime": "RULES_ONLY (no Jev, no exchange)", "code_sha256": code_hash(), "evaluator_sha256": sha256_file(__file__),
              "tape_model_sha256": sha256_file(HERE / "okx_tape_model.py"), "quote_model": MODEL_ASSUMPTIONS,
              "store": str(store).replace("\\", "/"), "store_catalog_sha256": sha256_file(store / team_data.CATALOG),
              "data_files_sha256": {s["file"]: s["sha256"] for s in segments},
              "instrument_sha256": sha256_file(args.instrument), "elapsed_seconds": round(time.time() - started, 1),
              "worker_peak_rss_mb_max": max((r["worker_peak_rss_mb"] or 0) for r in rows),
              "funding_policy": funding_policy(store), "paperwork": paperwork, "windows": rows, "trades": trades}
    gates = build_gates(ledger, paperwork, declared_n=args.declared_n)
    out = args.out or ROOT / "runtime" / "team_eval" / args.author / f'{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{strategy["recipe_sha256"][:8]}'
    out.mkdir(parents=True, exist_ok=True)
    ledger_text = json.dumps(ledger, ensure_ascii=False, indent=1, allow_nan=False) + "\n"
    (out / "ledger.json").write_bytes(ledger_text.encode("utf-8"))
    (out / "gates.json").write_bytes((json.dumps(gates, ensure_ascii=False, indent=1, allow_nan=False) + "\n").encode("utf-8"))
    (out / "gates.md").write_bytes(gates_markdown(gates).encode("utf-8"))
    registry = args.registry or ROOT / "runtime" / "team_eval" / args.author / "registry.jsonl"
    registry.parent.mkdir(parents=True, exist_ok=True)
    line = {"created_at": ledger["created_at"], "author_scope": args.author, "label": args.label, "data_set": data_set,
            "recipe_sha256": ledger["recipe_sha256"], "recipe_file_sha256": ledger["recipe_file_sha256"],
            "span": [gates["metrics"]["span_start_utc"], gates["metrics"]["span_end_utc"]], "windows": len(rows),
            "verdict": gates["verdict"]["verdict"], "net_usdt": gates["metrics"]["net_usdt"],
            "stress_net_usdt": gates["metrics"]["stress_net_usdt"], "closed_trades": gates["metrics"]["closed_trades"],
            "ledger_sha256": sha256_bytes(ledger_text.encode("utf-8")), "out": str(out), "evaluator_sha256": ledger["evaluator_sha256"],
            "code_sha256": ledger["code_sha256"], **declared_registry(gates)}
    with registry.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
    print(json.dumps({"verdict": line["verdict"], "net_usdt": line["net_usdt"], "stress_net_usdt": line["stress_net_usdt"],
                      "closed_trades": line["closed_trades"], "windows": len(rows), "elapsed_s": ledger["elapsed_seconds"],
                      "worker_peak_rss_mb_max": ledger["worker_peak_rss_mb_max"], "out": str(out)}, ensure_ascii=False))
    return 0


def instrument_file(inst):
    return HERE / f"okx_instrument_{inst.lower().replace('-', '_')}.json"


def command_run_v4(args, raw, payload):
    """Schema-v4 recipe: the candle engine of tools/team_eval_v4.py, one continuous account (no windows to join)."""
    import team_eval_v4
    from trading_lab import strategy_v4
    from trading_lab.strategy_training import code_hash
    try:
        strategy = strategy_v4.validate_payload(payload)
    except strategy_v4.ContractError as error:
        raise SystemExit(f"v4 recipe rejected: {error}") from None
    inst = strategy["instrument"]
    store = Path(args.store) if args.store != DEFAULT_STORE else DEFAULT_STORE.parent / inst
    spec_file = args.instrument if args.instrument != DEFAULT_INSTRUMENT else instrument_file(inst)
    spec = json.loads(Path(spec_file).read_text(encoding="utf-8"))[0]
    if spec.get("instId") != inst:
        raise SystemExit(f"instrument file {spec_file} is not {inst}")
    data_set = args.set.upper()
    catalog, windows, _ = plan_windows(store, data_set, args.start, args.end, 86400, 86400, 0)
    if catalog.get("instrument") != inst:
        raise SystemExit(f"store {store} holds {catalog.get('instrument')}, the recipe trades {inst}")
    first_ms = catalog["segments"][0]["first_ms"]
    start_ms, end_ms = windows[0]["start_s"] * 1000, (windows[-1]["start_s"] + 86400) * 1000
    if data_set == "DEV":
        end_ms = min(end_ms, DEV_LAST_OPEN_MS + 60_000)
    warm = start_ms - strategy_v4.MAX_LOOKBACK_MS
    warmup = None
    if data_set != "DEV":  # SEALED/FORWARD stores start at the DEV cutoff: warm the features up on DEV history
        dev_store = DEFAULT_STORE.parent / inst
        warmup = (dev_store, first_ms) if dev_store.resolve() != store.resolve() else None
    if warmup is None:
        warm = max(first_ms, warm)
    sys.path.insert(0, str(HERE))
    from research_limits import free_ram_gb
    # D111 p.4: a v4 run is one process, about 2-3 min and < 1.5 GB for the whole ETH DEV (measured 2026-10-28), so it
    # takes no heavy slot and no BOARD booking; it only refuses to start when the machine is already short of memory.
    free = free_ram_gb()
    if free is not None and free < V4_RAM_FLOOR_GB:
        raise SystemExit(f"free RAM {free:.1f} GB < {V4_RAM_FLOOR_GB} GB: the v4 run was not started")
    started = time.time()
    series = team_eval_v4.load_minutes(store, warm, end_ms, warmup)
    funding_rows = team_data.load_funding(store)
    known_funding = sorted(dict(([row for row in team_data.load_funding(warmup[0]) if row[0] < warmup[1]] if warmup else [])
                                + funding_rows).items())
    positioning = team_eval_v4.attach_positioning(series, store, inst, known_funding, warmup)
    trades, counters, orders = team_eval_v4.simulate(strategy, series, spec, funding_rows, start_ms, end_ms)
    grid = team_eval_v4.sensitivity(strategy, series, spec, funding_rows, start_ms, end_ms)
    unknown = team_eval_v4.unknown_share(strategy, series, start_ms, end_ms)
    days = team_eval_v4.day_windows(start_ms, end_ms)
    for gap_start, missing in series["gaps"]:  # D097: a data hole fails the gates until fixed
        for day in days:
            if day["id"] == gap_start[:10]:
                day.update(status="INCOMPLETE", errors={"data_gap": missing})
    for row in days:
        row["trades"] = sum(t["window_id"] == row["id"] for t in trades)
    paperwork = {"valid": True, "lint_warnings": [], "prereg": None, "trials": None, "prereg_and_trials": None}
    if args.prereg:
        paperwork["prereg"] = {"file": str(args.prereg), "sha256": sha256_file(args.prereg)}
    if args.trials:
        lines = [line for line in args.trials.read_text(encoding="utf-8").splitlines() if line.strip()]
        paperwork["trials"] = {"file": str(args.trials), "sha256": sha256_file(args.trials), "lines": len(lines)}
    if args.prereg and args.trials:
        paperwork["prereg_and_trials"] = bool(paperwork["trials"]["lines"])
    ledger = {"schema": SCHEMA_LEDGER, "created_at": utc(time.time()), "evaluator_version": EVALUATOR_VERSION,
              "engine": "candle-v4 (tools/team_eval_v4.py)", "data_set": data_set, "author_scope": args.author, "label": args.label,
              "recipe_file": str(args.recipe), "recipe_file_sha256": sha256_bytes(raw), "recipe_sha256": strategy["recipe_sha256"],
              "recipe_name": strategy["name"], "strategy_schema_version": 4, "instrument": inst, "side": strategy["rules"]["side"],
              "capital_usdt_per_window": CAPITAL, "runtime": "RULES_ONLY candle engine (no Jev, no exchange)",
              "code_sha256": code_hash(), "evaluator_sha256": sha256_file(__file__),
              "candle_engine_sha256": sha256_file(HERE / "team_eval_v4.py"),
              "store": str(store).replace("\\", "/"), "store_catalog_sha256": sha256_file(store / team_data.CATALOG),
              "data_files_sha256": {seg["file"]: seg["sha256"] for seg in catalog["segments"]},
              "instrument_sha256": sha256_file(spec_file), "elapsed_seconds": round(time.time() - started, 1),
              "filled_minutes": series["filled_minutes"], "data_gaps": series["gaps"], "counters": counters,
              "warmup_store": str(warmup[0]).replace("\\", "/") if warmup else None,
              "execution_model_id": strategy["execution_model_id"], "eval_only": strategy["eval_only"],
              "semantic_sha256": strategy["semantic_sha256"],
              "v4_report": v4_report(strategy, trades, counters, grid, unknown), "orders": orders,
              "positioning_inputs": positioning,
              "funding_policy": funding_policy(store), "paperwork": paperwork, "windows": days, "trades": trades}
    return write_outputs(args, ledger, build_gates(ledger, paperwork, declared_n=args.declared_n), strategy["recipe_sha256"])


def v4_report(strategy, trades, counters, grid, unknown=None):
    """D106 report lines for v4 recipes: maker sensitivity and taker floor, stop-slippage stress, sizing, funding."""
    stop_extra = 0.0
    for t in trades:
        if t["exit_reason"] == "stop_loss":  # max(2 bps, 0.25 x the trigger minute's range) of the exit notional
            bps = max(2.0, .25 * (t.get("trigger_minute_range_bps") or 0.0))
            stop_extra += t["executions"][-1]["notional_usdt"] * bps / 10000
    intended = CAPITAL * strategy["rules"]["size_pct"] / 100
    by_quarter = defaultdict(list)
    for t in trades:
        by_quarter[quarter_of(t["entry_at"])].append(t["first_notional_usdt"])
    net = sum(t["net_usdt"] for t in trades)
    from trading_lab.strategy_v4 import EXECUTION_MODEL_ID
    return {"execution_model_id": EXECUTION_MODEL_ID, "maker_sensitivity": grid,
            "stop_slippage_stress": {"rule": "max(2 bps, 0.25 x trigger-minute range) per stop exit",
                                     "stop_exits": sum(t["exit_reason"] == "stop_loss" for t in trades),
                                     "extra_cost_usdt": round(stop_extra, 6), "net_after_usdt": round(net - stop_extra, 6)},
            "ambiguous_minute_stops": counters.get("ambiguous_minute_stops", 0),
            "sizing": {"intended_notional_usdt": intended, "hold_too_small": counters.get("too_small", 0),
                       "actual_mean_notional_by_quarter": {q: round(statistics.fmean(v), 4) for q, v in sorted(by_quarter.items())}},
            "funding_share_of_net": round(sum(t["funding_usdt"] for t in trades) / net, 6) if net else None,
            "entry_unknown_share_by_quarter": unknown or {},
            "quarters_mostly_unknown": sorted(q for q, share in (unknown or {}).items() if share > .5),
            "counters": counters}


def declared_registry(gates):
    """Registry fields of a declared-N run; none otherwise, so other registry lines keep their exact shape."""
    n = gates.get("first_n")
    return {} if n is None else {"declared_n": n["declared_n"], "declared_n_reached": n["reached"],
                                 "prefix_trades_sha256": n["prefix_trades_sha256"]}


def write_outputs(args, ledger, gates, recipe_sha256):
    out = args.out or ROOT / "runtime" / "team_eval" / args.author / f'{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{recipe_sha256[:8]}'
    out.mkdir(parents=True, exist_ok=True)
    ledger_text = json.dumps(ledger, ensure_ascii=False, indent=1, allow_nan=False) + "\n"
    (out / "ledger.json").write_bytes(ledger_text.encode("utf-8"))
    (out / "gates.json").write_bytes((json.dumps(gates, ensure_ascii=False, indent=1, allow_nan=False) + "\n").encode("utf-8"))
    (out / "gates.md").write_bytes(gates_markdown(gates).encode("utf-8"))
    registry = args.registry or ROOT / "runtime" / "team_eval" / args.author / "registry.jsonl"
    registry.parent.mkdir(parents=True, exist_ok=True)
    line = {"created_at": ledger["created_at"], "author_scope": args.author, "label": args.label, "data_set": ledger["data_set"],
            "recipe_sha256": ledger["recipe_sha256"], "recipe_file_sha256": ledger["recipe_file_sha256"],
            "span": [gates["metrics"]["span_start_utc"], gates["metrics"]["span_end_utc"]], "windows": len(ledger["windows"]),
            "verdict": gates["verdict"]["verdict"], "net_usdt": gates["metrics"]["net_usdt"],
            "stress_net_usdt": gates["metrics"]["stress_net_usdt"], "closed_trades": gates["metrics"]["closed_trades"],
            "ledger_sha256": sha256_bytes(ledger_text.encode("utf-8")), "out": str(out), "evaluator_sha256": ledger["evaluator_sha256"],
            "code_sha256": ledger["code_sha256"], "engine": ledger.get("engine", "exact"),
            "execution_model_id": ledger.get("execution_model_id"), "store_catalog_sha256": ledger.get("store_catalog_sha256"),
            **declared_registry(gates)}
    with registry.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
    print(json.dumps({"verdict": line["verdict"], "net_usdt": line["net_usdt"], "stress_net_usdt": line["stress_net_usdt"],
                      "closed_trades": line["closed_trades"], "windows": len(ledger["windows"]), "elapsed_s": ledger["elapsed_seconds"],
                      "engine": line["engine"], "out": str(out)}, ensure_ascii=False))
    return 0


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="exact engine over consecutive windows -> ledger + gates")
    run.add_argument("--recipe", type=Path, required=True)
    run.add_argument("--author", default="unknown")
    run.add_argument("--label", default="")
    run.add_argument("--store", type=Path, default=DEFAULT_STORE, help="team data store folder with CATALOG.json")
    run.add_argument("--instrument", type=Path, default=DEFAULT_INSTRUMENT)
    run.add_argument("--set", choices=("dev", "sealed", "forward"), default="dev")
    run.add_argument("--start", help="first window start, UTC date or ISO with Z (default: first full day with warm-up)")
    run.add_argument("--end", help="last admission end, UTC date (inclusive day) or ISO with Z (default: data end)")
    run.add_argument("--admission-hours", type=float, default=24.0)
    run.add_argument("--step-hours", type=float, default=None)
    run.add_argument("--tail-seconds", type=int, default=86400, help="exit tail for max_hold_seconds=null")
    run.add_argument("--workers", type=int, default=8, help=f"parallel worker processes, 1..{MAX_WORKERS}")
    run.add_argument("--prereg", type=Path)
    run.add_argument("--trials", type=Path)
    run.add_argument("--out", type=Path)
    run.add_argument("--registry", type=Path)
    run.add_argument("--quiet", action="store_true")
    run.add_argument("--declared-n", type=int, help="gate D: verdict on the first N closed trades only (SEALED/FORWARD, N >= 20)")
    gates = sub.add_parser("gates", help="recompute gates from a ledger")
    gates.add_argument("--ledger", type=Path, required=True)
    gates.add_argument("--out", type=Path)
    gates.add_argument("--store", type=Path, help="store for the funding policy when the ledger has none (older runs)")
    gates.add_argument("--declared-n", type=int, help="gate D: verdict on the first N closed trades only (SEALED/FORWARD, N >= 20)")
    clone = sub.add_parser("clone", help="entry overlap and daily PnL correlation of two ledgers")
    clone.add_argument("--a", type=Path, required=True, help="new candidate ledger")
    clone.add_argument("--b", type=Path, required=True, help="existing candidate ledger")
    clone.add_argument("--epoch", choices=("E2",), help="compare only inside this epoch")
    args = parser.parse_args(argv)
    if args.command == "run":
        if args.declared_n is not None and (args.set == "dev" or args.declared_n < CHARTER["D"]["min_closed_trades"]):
            raise SystemExit(f"--declared-n is gate D: --set sealed|forward and N >= {CHARTER['D']['min_closed_trades']}")
        return command_run(args)
    if args.command == "gates":
        result = build_gates(read_json(args.ledger), store=args.store, declared_n=args.declared_n)
        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out / "gates.json").write_bytes((json.dumps(result, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
            (args.out / "gates.md").write_bytes(gates_markdown(result).encode("utf-8"))
        print(json.dumps({"verdict": result["verdict"]["verdict"], "net_usdt": result["metrics"]["net_usdt"],
                          "stress_net_usdt": result["metrics"]["stress_net_usdt"], "closed_trades": result["metrics"]["closed_trades"],
                          **declared_registry(result)}, ensure_ascii=False))
        return 0
    print(json.dumps(clone_check(read_json(args.a), read_json(args.b), epoch=args.epoch), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
