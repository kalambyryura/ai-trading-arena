"""Consistency check of team/judge/tools/gate_k_long.py against the calibration code on synthetic trade lists.

Builds synthetic trade lists (null and skilled, from kt_calibrate2.generate), writes them in the plain trade-list format with
entries 10 s after the daily close, runs the tool, and compares counts, nets and shift p-values with the calibration.
Also checks the reference cases: buy-and-hold (must fail the timing test) and an always-flat list.
"""
import json
import pathlib
import subprocess
import sys

import numpy as np

S = pathlib.Path(sys.argv[1])
REPO = pathlib.Path(sys.argv[2])
sys.argv = ["kt", str(S / "dev_daily.json"), "1"]
sys.path.insert(0, str(S))
import kt_calibrate2 as kc  # noqa: E402

kc.shift_p.__defaults__ = (1,)  # every non-zero circular shift, as in the tool

rng = np.random.default_rng(7)
worst = 0.0
for inst, mode, kappa in (("BTC-USDT-SWAP", "long", 0.0), ("ETH-USDT-SWAP", "both", 1.0), ("SOL-USDT-SWAP", "long", 2.5), ("ETH-USDT-SWAP", "long", 1.0)):
    days, close, ret, fund, known = kc.series(inst)
    trades = kc.generate(rng, days, close, ret, 20, 30, mode, kappa)
    checks, info = kc.evaluate(trades, days, close, ret, fund, known)
    rows = []
    for a, b, side in trades:
        entry = (days[a] + np.timedelta64(1, "D")).astype("datetime64[s]") + np.timedelta64(10, "s")
        exit_ = (days[b] + np.timedelta64(1, "D")).astype("datetime64[s]") + np.timedelta64(10, "s")
        held = slice(a + 1, b + 1)
        notional = kc.FRAC * kc.CAP
        net = (side * notional * (close[b] / close[a] - 1) - notional * kc.COST
               - side * notional * float(np.sum(fund[held] * known[held])) - notional * kc.GAP_RATE * float(np.sum(~known[held])))
        rows.append({"entry_at": str(entry) + "Z", "exit_at": str(exit_) + "Z", "side": "long" if side > 0 else "short",
                     "notional_usdt": notional, "executed_notional_usdt": 2 * notional, "net_usdt": net})
    path = S / f"kt_synth_{inst[:3]}_{mode}_{kappa}.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    out = S / f"kt_out_{inst[:3]}_{mode}_{kappa}"
    done = subprocess.run([sys.executable, "-X", "utf8", "-B", str(REPO / "team/judge/tools/gate_k_long.py"), "--trades", str(path),
                           "--instrument", inst, "--configs", "1", "--out", str(out)], capture_output=True, text=True, encoding="utf-8")
    if done.returncode:
        print("TOOL FAILED", done.stderr[-800:])
        raise SystemExit(1)
    res = json.loads((out / "gate_k_long.json").read_text(encoding="utf-8"))
    tool_p = {k: v["timing"]["p"] for k, v in res["periods"].items() if "timing" in v}
    diffs = [abs(tool_p["DEV"] - info["p_dev"]), abs(tool_p["E2"] - info["p_e2"]), abs(tool_p["E2_tail"] - info["p_tail"])]
    worst = max(worst, *diffs)
    net_tool, net_cal = res["periods"]["DEV"]["net_usdt"], info["net"]
    print(inst[:3], mode, kappa, "| trades", res["eligibility"]["closed_trades_dev"], "=", info["trades"], "| net", net_tool, "vs", round(net_cal, 6),
          "| p tool", tool_p, "cal", (round(info["p_dev"], 5), round(info["p_e2"], 5), round(info["p_tail"], 5)),
          "| verdict", res["verdict"], "| calibration all:", all(checks[k] for k in kc.COMMON) and checks["shift_dev_05"] and checks["shift_tail_median"],
          "| day-level minus ledger", res["info"]["day_level_gross_minus_ledger_net_usdt"])
print("largest p difference tool vs calibration:", round(worst, 5))

# reference cases on BTC: buy-and-hold in 60-day slices (so that it has trades), and a slow random list
days, close, ret, fund, known = kc.series("BTC-USDT-SWAP")
rows, a = [], 1
while a + 60 < len(days):
    b = a + 60
    notional = 20.0
    entry = (days[a] + np.timedelta64(1, "D")).astype("datetime64[s]") + np.timedelta64(10, "s")
    exit_ = (days[b] + np.timedelta64(1, "D")).astype("datetime64[s]") + np.timedelta64(5, "s")
    rows.append({"entry_at": str(entry) + "Z", "exit_at": str(exit_) + "Z", "side": "long", "notional_usdt": notional,
                 "net_usdt": notional * (close[b] / close[a] - 1) - notional * kc.COST})
    a = b
path = S / "kt_ref_hold.json"
path.write_text(json.dumps(rows), encoding="utf-8")
done = subprocess.run([sys.executable, "-X", "utf8", "-B", str(REPO / "team/judge/tools/gate_k_long.py"), "--trades", str(path),
                       "--instrument", "BTC-USDT-SWAP", "--configs", "1", "--out", str(S / "kt_out_ref_hold")], capture_output=True, text=True, encoding="utf-8")
print("buy-and-hold in 60-day slices:", done.stdout.strip()[-600:], done.stderr[-300:])
