"""What the synthetic skill levels mean in plain units: edge over random placement per trade, timing z over DEV, win rate.

One configuration per draw (no selection). Edge = (S(0) - mean over shifts of S(k)) / (trades x notional), in bps.
"""
import pathlib
import sys

import numpy as np

S = pathlib.Path(sys.argv[1])
N = int(sys.argv[2])
sys.argv = ["kt", str(S / "dev_daily.json"), "1"]
sys.path.insert(0, str(S))
import kt_calibrate2 as kc  # noqa: E402

rng = np.random.default_rng(2026111706)
print("kappa freq | trades/yr | edge bps per trade (median) | timing z (median) | win rate | hold days | single p<=0.05 share")
for kappa in (0.0, 0.5, 1.0, 1.5, 2.5):
    for m_in, m_out in ((12, 20), (20, 30), (40, 50)):
        edge, zs, wins, holds, per_year, p05 = [], [], [], [], [], []
        for inst in ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"):
            days, close, ret, fund, known = kc.series(inst)
            t = len(days)
            fx_lin = np.fft.rfft(kc.CAP * (ret - fund * known))
            fx_abs = np.fft.rfft(-kc.CAP * kc.GAP_RATE * (~known))
            for mode in ("long", "both"):
                for _ in range(N):
                    trades = kc.generate(rng, days, close, ret, m_in, m_out, mode, kappa)
                    if len(trades) < 5:
                        continue
                    expo = np.zeros(t)
                    gross = []
                    for a, b, side in trades:
                        expo[a + 1:b + 1] = side * kc.FRAC
                        gross.append(side * (close[b] / close[a] - 1))
                    s = np.fft.irfft(np.conj(np.fft.rfft(expo)) * fx_lin, n=t) + np.fft.irfft(np.conj(np.fft.rfft(np.abs(expo))) * fx_abs, n=t)
                    edge.append((s[0] - s[1:].mean()) / (len(trades) * kc.FRAC * kc.CAP) * 1e4)
                    zs.append((s[0] - s.mean()) / s.std())
                    wins.append(np.mean(np.array(gross) > 0))
                    holds.append(np.mean([b - a for a, b, _ in trades]))
                    per_year.append(len(trades) / (t / 365.25))
                    p05.append((1 + np.sum(s[1:] >= s[0] - 1e-12)) / t <= 0.05)
        print(kappa, f"{m_in}/{m_out}", "|", round(float(np.mean(per_year)), 1), "|", round(float(np.median(edge))), "|", round(float(np.median(zs)), 2),
              "|", round(float(np.mean(wins)), 3), "|", round(float(np.mean(holds)), 1), "|", round(float(np.mean(p05)), 3), flush=True)
