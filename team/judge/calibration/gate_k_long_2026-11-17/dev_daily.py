"""Daily DEV series for BTC, ETH, SOL: close at 00:00 UTC (the 23:59 minute's close) and the day's settled funding sum.

DEV only (data/okx/<INST>). Output: one JSON with per-instrument arrays day (YYYY-MM-DD of the day that
ends at the close), close, funding_sum (sum of realized rates settled in (00:00, 24:00] of that day), funding_known.
"""
import json
import pathlib
import sys

import numpy as np

REPO = pathlib.Path(sys.argv[1])
OUT = pathlib.Path(sys.argv[2])
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO))
import team_data  # noqa: E402
import team_eval_v4  # noqa: E402

DAY = 86_400_000
DEV_END = 1790965920000  # 2026-10-02T18:32:00Z
out = {}
for inst in ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"):
    store = pathlib.Path("data/okx") / inst
    catalog = team_data.read_catalog(store)
    first = catalog["segments"][0]["first_ms"]
    stamps, values = team_eval_v4.read_arrays(store, first, DEV_END, columns=(5,))
    last_minute = stamps % DAY == DAY - 60_000  # the 23:59 bar: its close is the price at 00:00 UTC
    days = (stamps[last_minute] // DAY).astype(np.int64)
    closes = values[last_minute, 0]
    funding = team_data.load_funding(store)
    known_first = funding[0][0] if funding else None
    per_day, have = {}, set()
    for t, rate in funding:
        if rate in (None, ""):
            continue
        d = (t - 1) // DAY  # a settlement at 00:00 of day d+1 belongs to day d
        per_day[d] = per_day.get(d, 0.0) + float(rate)
        have.add(d)
    out[inst] = {"day": [str(np.datetime64(int(d), "D")) for d in days], "close": [float(c) for c in closes],
                 "funding_sum": [per_day.get(int(d), 0.0) for d in days], "funding_known": [int(d) in have for d in days],
                 "first_minute_utc": team_data.iso(int(stamps[0])), "last_minute_utc": team_data.iso(int(stamps[-1])),
                 "store_catalog_sha256": team_data.sha256_file(store / team_data.CATALOG)}
    print(inst, "days", len(days), out[inst]["day"][0], "->", out[inst]["day"][-1], "| funding known days", sum(out[inst]["funding_known"]),
          "| minutes", len(stamps), flush=True)
OUT.write_text(json.dumps(out), encoding="utf-8")
print("written", OUT)
