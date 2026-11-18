"""Consistency check of the family test in team/judge/tools/gate_k_long.py against a direct computation.

Builds a synthetic family (6 configurations per case), writes every member as a plain trade list into a folder, runs
the tool with --family, and compares its family p with the formula computed here from the same exposures.
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

rng = np.random.default_rng(11)
worst = 0.0
for inst, mode, kappa in (("BTC-USDT-SWAP", "long", 0.0), ("ETH-USDT-SWAP", "both", 1.0), ("SOL-USDT-SWAP", "long", 2.5)):
    days, close, ret, fund, known = kc.series(inst)
    t = len(days)
    pool = [kc.generate(rng, days, close, ret, 20, 30, mode, kappa) for _ in range(6)]
    folder = S / f"kt_family_{inst[:3]}"
    folder.mkdir(exist_ok=True)
    for old in folder.glob("*.json"):
        old.unlink()
    expo = np.zeros((6, t))
    for j, trades in enumerate(pool):
        rows = []
        for a, b, side in trades:
            expo[j, a + 1:b + 1] = side * kc.FRAC
            entry = (days[a] + np.timedelta64(1, "D")).astype("datetime64[s]") + np.timedelta64(10, "s")
            exit_ = (days[b] + np.timedelta64(1, "D")).astype("datetime64[s]") + np.timedelta64(10, "s")
            notional = kc.FRAC * kc.CAP
            held = slice(a + 1, b + 1)
            net = (side * notional * (close[b] / close[a] - 1) - notional * kc.COST
                   - side * notional * float(np.sum(fund[held] * known[held])) - notional * kc.GAP_RATE * float(np.sum(~known[held])))
            rows.append({"entry_at": str(entry) + "Z", "exit_at": str(exit_) + "Z", "side": "long" if side > 0 else "short",
                         "notional_usdt": notional, "net_usdt": net})
        (folder / f"config_{j}.json").write_text(json.dumps(rows), encoding="utf-8")
    x_lin = kc.CAP * (ret - fund * known)
    x_abs = -kc.CAP * kc.GAP_RATE * (~known)
    sums = (np.fft.irfft(np.conj(np.fft.rfft(expo, axis=1)) * np.fft.rfft(x_lin), n=t, axis=1)
            + np.fft.irfft(np.conj(np.fft.rfft(np.abs(expo), axis=1)) * np.fft.rfft(x_abs), n=t, axis=1))
    z = (sums - sums.mean(axis=1, keepdims=True)) / sums.std(axis=1, keepdims=True)
    top = z.max(axis=0)
    for own in (int(np.argmax(z[:, 0])), 0):
        direct = (1 + int(np.sum(top[1:] >= z[own, 0] - 1e-9))) / t
        out = S / f"kt_out_family_{inst[:3]}_{own}"
        done = subprocess.run([sys.executable, "-X", "utf8", "-B", str(REPO / "team/judge/tools/gate_k_long.py"), "--trades", str(folder / f"config_{own}.json"),
                               "--instrument", inst, "--family", str(folder), "--instruments", "1", "--out", str(out)],
                              capture_output=True, text=True, encoding="utf-8")
        if done.returncode:
            print("TOOL FAILED", done.stderr[-800:])
            raise SystemExit(1)
        res = json.loads((out / "gate_k_long.json").read_text(encoding="utf-8"))
        timing = res["dev_timing"]
        worst = max(worst, abs(timing["p"] - direct))
        print(inst[:3], mode, kappa, "| own config", own, "| z own tool", timing["z_own"], "direct", round(float(z[own, 0]), 4),
              "| family p tool", timing["p"], "direct", round(direct, 8), "| single p", res["periods"]["DEV"]["timing"]["p"],
              "| members", timing["members"], "| verdict", res["verdict"], "| failed", [r["check"] for r in res["checks"] if r["status"] == "FAIL"])
print("largest family p difference tool vs direct:", round(worst, 8))
