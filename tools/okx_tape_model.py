"""Workshop-compatible MODEL tapes built from public OKX 1m candles (Claude challenge 2026-10-01).

Everything below that is not in a public candle is a labelled MODEL assumption, identical for all authors:
  * Quote path: inside each minute the price moves piecewise-linearly O->L->H->C when close >= open,
    otherwise O->H->L->C; time is split in proportion to the price distance of each leg. The path is
    sampled every POLL_SECONDS (5 s = poll interval of the arena contract).
  * Spread: bid = path rounded down to tickSz, ask = bid + 1 tick (0.1 USDT). Median spread in the five
    genuine arena recordings of 2026-09-29/30 was exactly 1 tick (rare maxima 0.2-3.7 USDT are not modelled).
  * Last: the path price on the tick decimals, like a venue's last. With precise_last (tools/v4_conformance.py only) it
    is the unrounded path price (repr of the float), the price tools/team_eval_v4.py tests a resting post-only limit
    against (D096). Bid, ask and candles stay on ticks either way.
  * Instrument: ticker and funding rows carry the instId of the instrument JSON (BTC-USDT-SWAP or ETH-USDT-SWAP),
    and build_snapshot checks every row against it.
  * Visible size: 500 contracts on bid and ask (genuine recordings: median ~530-890, p10 ~27-227 contracts).
  * Latency: ticker ts = server time - 0.4 s, funding ts = server time - 1 s, snapshot time == server time.
  * Funding: no historical as-of forecast is available. Decision rate/calendar stay UNKNOWN. Realized
    settlements become accounting events only at their timestamp; valuation still uses a model quote.
  * Candles: the 30 newest confirmed public candles plus one in-progress candle derived from the model path
    (confirm="0"; ignored by feature code exactly like live data).
No bid/ask history, order book, trades or liquidity are known. Results on these tapes are training/research
evidence, not a market recording and not a qualification on real quotes.
Features are recomputed by trading_lab.arena_market.build_snapshot, the same function used for recordings.
"""
from bisect import bisect_right
import csv
import hashlib
import json
import math
from pathlib import Path

MODEL_ID = "okx-1m-candle-path-v2"  # unchanged in R2: default tapes are byte for byte as before (precise_last is opt-in)
POLL_SECONDS = 5
TOP_SIZE_CONTRACTS = "500"
QUOTE_LAG_MS = 400
FUNDING_LAG_MS = 1000
CANDLE_HEADER = ["ts_ms", "utc", "open", "high", "low", "close", "vol_contracts", "vol_btc", "vol_quote_usdt", "confirm"]
MODEL_ASSUMPTIONS = {
    "model_id": MODEL_ID,
    "intra_minute_path": "piecewise-linear O->L->H->C if close>=open else O->H->L->C; leg time proportional to price distance",
    "poll_seconds": POLL_SECONDS,
    "spread": "1 tick (0.1 USDT): bid=floor(path/tick)*tick, ask=bid+tick",
    "last": "path price on the tick decimals; unrounded (repr) only with precise_last, for the post-only conformance (D096)",
    "visible_size_contracts_each_side": int(TOP_SIZE_CONTRACTS),
    "quote_lag_ms": QUOTE_LAG_MS, "funding_ts_lag_ms": FUNDING_LAG_MS,
    "funding_rate": "UNKNOWN without an as-of forecast; realized rates are accounting events at settlement only",
    "in_progress_candle": "derived from model path, confirm=0, ignored by features",
    "calibration": "spread/size from 5 genuine arena_v4 public recordings (2026-09-29/30), coordinator-only",
}


class GapError(ValueError):
    """Required public data is missing; the window cannot be modelled honestly."""


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def minute_path(o, h, l, c):
    points = [o, l, h, c] if c >= o else [o, h, l, c]
    legs = [abs(b - a) for a, b in zip(points, points[1:])]
    total = sum(legs)
    knots = [0.0]
    for leg in legs:
        knots.append(knots[-1] + (60.0 * leg / total if total else 0.0))

    def price(second):
        if not total or second <= 0:
            return o
        if second >= 60:
            return c
        for i in range(3):
            if second <= knots[i + 1] and knots[i + 1] > knots[i]:
                return points[i] + (points[i + 1] - points[i]) * (second - knots[i]) / (knots[i + 1] - knots[i])
        return c
    return price, points, knots


class CandleStore:
    def __init__(self, candle_csv, funding_csv, instrument_json):
        self.files = {str(candle_csv): sha256_file(candle_csv), str(funding_csv): sha256_file(funding_csv),
                      str(instrument_json): sha256_file(instrument_json)}
        with open(candle_csv, encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            if next(reader) != CANDLE_HEADER:
                raise ValueError("unexpected candle CSV header")
            self.rows = {}
            for row in reader:
                # Back to the OKX response order: ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm (original strings).
                self.rows[int(row[0])] = [row[0], row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9]]
        self.first_ms, self.last_ms = min(self.rows), max(self.rows)
        with open(funding_csv, encoding="utf-8", newline="") as handle:
            funding = [(int(r["funding_time_ms"]), r["realized_rate"] or None) for r in csv.DictReader(handle)]
        funding.sort()
        self.funding_times = [t for t, _ in funding]
        self.funding_rates = [r for _, r in funding]
        instrument = json.loads(Path(instrument_json).read_text(encoding="utf-8"))
        self.instrument = instrument if isinstance(instrument, list) else [instrument]
        tick = self.instrument[0]["tickSz"]
        self.tick = float(tick)
        self.decimals = len(tick.split(".")[1]) if "." in tick else 0
        self._paths = {}

    precise_last = False  # True: the ticker's last is the unrounded path price (tools/v4_conformance.py, D096)

    def fmt(self, value):
        return f"{value:.{self.decimals}f}"

    def inst_id(self):
        """The instrument of the instrument JSON; every ticker and funding row of the tape carries it."""
        return self.instrument[0]["instId"]

    def has_range(self, start_s, end_s):
        """True if every minute needed for snapshots in [start_s, end_s] exists (incl. 30-minute history)."""
        first = (start_s // 60) * 60 - 1800
        last = (end_s // 60) * 60
        return all((m * 1000) in self.rows for m in range(first, last + 60, 60))

    def path_for(self, minute_ms):
        cached = self._paths.get(minute_ms)
        if cached is None:
            row = self.rows[minute_ms]
            cached = minute_path(*(float(x) for x in row[1:5]))
            if len(self._paths) > 4096:
                self._paths.clear()
            self._paths[minute_ms] = cached
        return cached

    def raw_at(self, second):
        if second % POLL_SECONDS:
            raise ValueError("snapshot time must be on the poll grid")
        minute = (second // 60) * 60
        offset = second - minute
        needed = [(minute - 60 * i) * 1000 for i in range(0, 31)]
        missing = [m for m in needed if m not in self.rows]
        if missing:
            raise GapError(f"missing public candle(s) near {missing[0]}")
        current = self.rows[minute * 1000]
        price, points, knots = self.path_for(minute * 1000)
        now_price = price(offset)
        seen = [points[0], now_price] + [points[i] for i in range(1, 4) if knots[i] <= offset]
        volume_share = offset / 60
        in_progress = [current[0], current[1], self.fmt(max(seen)), self.fmt(min(seen)), self.fmt(now_price),
                       f"{float(current[5]) * volume_share:.2f}", f"{float(current[6]) * volume_share:.6f}",
                       f"{float(current[7]) * volume_share:.2f}", "0"]
        candles = [in_progress] + [self.rows[m] for m in needed[1:]]
        bid = math.floor(now_price / self.tick + 1e-9) * self.tick
        ask = bid + self.tick
        server_ms = second * 1000
        index = bisect_right(self.funding_times, server_ms) - 1
        inst = self.inst_id()
        ticker = {"instType": "SWAP", "instId": inst, "last": repr(float(now_price)) if self.precise_last else self.fmt(now_price), "lastSz": "0",
                  "askPx": self.fmt(ask), "askSz": TOP_SIZE_CONTRACTS, "bidPx": self.fmt(bid), "bidSz": TOP_SIZE_CONTRACTS,
                  "ts": str(server_ms - QUOTE_LAG_MS), "source": "MODEL:" + MODEL_ID}
        funding = {"instType": "SWAP", "instId": inst, "fundingRate": None,
                   "fundingTime": None, "ts": str(server_ms - FUNDING_LAG_MS),
                   "source": "MODEL:realized-funding-events-v2",
                   "settlements": ([{"fundingTime": str(self.funding_times[index]),
                                     "realizedRate": self.funding_rates[index]}] if index >= 0 else [])}
        return {"instrument": self.instrument, "candles": candles, "funding": [funding],
                "ticker": [ticker], "clock": [{"ts": str(server_ms)}]}

    def tape(self, start_s, end_s):
        """List of engine snapshots for [start_s, end_s], built with the production build_snapshot."""
        from trading_lab.arena_market import build_snapshot
        if start_s % 60 or end_s <= start_s:
            raise ValueError("window must start on a minute boundary")
        if not self.has_range(start_s, end_s):
            raise GapError("window touches a gap in public candles")
        inst = self.inst_id()
        return [build_snapshot(self.raw_at(t), t, allow_model_funding=True, instrument=inst)
                for t in range(start_s, end_s + 1, POLL_SECONDS)]

    def write_recording(self, start_s, end_s, path):
        """Write a .snapshots.jsonl accepted by tools/strategy_workshop.py test --recording."""
        from trading_lab.arena_market import build_snapshot
        if not self.has_range(start_s, end_s):
            raise GapError("window touches a gap in public candles")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = 0
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for t in range(start_s, end_s + 1, POLL_SECONDS):
                raw = self.raw_at(t)
                snapshot = build_snapshot(raw, t, allow_model_funding=True, instrument=self.inst_id())
                handle.write(json.dumps({"at": t, "raw": raw, "sha256": snapshot["sha256"],
                                         "source": "MODEL_RECONSTRUCTED_FROM_OKX_PUBLIC_1M", "model": MODEL_ID},
                                        ensure_ascii=False, separators=(",", ":")) + "\n")
                rows += 1
        return {"file": str(path), "rows": rows, "sha256": sha256_file(path)}


if __name__ == "__main__":
    import argparse
    import sys
    from datetime import datetime, timezone
    parser = argparse.ArgumentParser(description="Write one workshop-compatible MODEL tape (.snapshots.jsonl).")
    parser.add_argument("--data", type=Path, required=True, help="folder with candles/funding/instrument files")
    parser.add_argument("--candles", default="okx_BTC-USDT-SWAP_1m_dev.csv")
    parser.add_argument("--funding", default="okx_BTC-USDT-SWAP_funding_dev.csv")
    parser.add_argument("--start", required=True, help="UTC, e.g. 2026-08-01T00:00:00Z")
    parser.add_argument("--seconds", type=int, required=True, help="tape length incl. exit tail")
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="project root containing trading_lab")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.project.resolve()))
    store = CandleStore(args.data / args.candles, args.data / args.funding, args.data / "instrument.json")
    begin = int(datetime.strptime(args.start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
    print(json.dumps(store.write_recording(begin, begin + args.seconds, args.output), ensure_ascii=False))
