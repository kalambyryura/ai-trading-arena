"""How often a strategy WITHOUT any timing passes a block bootstrap of its days in the market (Sol's proposal, D199).

Synthetic strategies only: random renewal positions (kc.generate with kappa = 0) on real DEV daily returns. For each one
the daily marked-to-market result on the days in the market is resampled in blocks of 20 in-market days (10 000 samples
in the proposal; 2 000 here), and the share of samples with a positive total is compared with the proposed thresholds:
P(net > 0) >= 0.90 on DEV and >= 0.80 in E2. Costs per trade are charged on every resample in proportion to the days
drawn. The shift test of the K-T track is reported for the same strategies.
"""
import pathlib
import sys

import numpy as np

S = pathlib.Path(sys.argv[1])
SIMS = int(sys.argv[2])
sys.argv = ["kt", str(S / "dev_daily.json"), "1"]
sys.path.insert(0, str(S))
import kt_calibrate2 as kc  # noqa: E402

kc.shift_p.__defaults__ = (1,)
BLOCK, DRAWS = 20, 2000


def boot_share(pnl, rng):
    """Share of block-bootstrap samples of the in-market daily results with a positive total."""
    n = len(pnl)
    if n < 2 * BLOCK:
        return float("nan")
    blocks = int(np.ceil(n / BLOCK))
    starts = rng.integers(0, n - BLOCK + 1, size=(DRAWS, blocks))
    index = (starts[:, :, None] + np.arange(BLOCK)[None, None, :]).reshape(DRAWS, -1)[:, :n]
    return float(np.mean(pnl[index].sum(axis=1) > 0))


def main():
    rng = np.random.default_rng(2026111710)
    print("inst mode freq | trades | boot DEV>=0.90 | boot E2>=0.80 | both | both and DEV, E2, tail net > 0 | shift p_DEV<=0.05 | K-T single test whole gate (m=1)")
    rows = []
    for inst in ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"):
        days, close, ret, fund, known = kc.series(inst)
        t = len(days)
        x = kc.CAP * (ret - fund * known) - 0.0  # day result per 100 USDT of long notional; gap funding charged below
        gap = kc.CAP * kc.GAP_RATE * (~known)
        e2 = days >= np.datetime64("2024-01-01")
        for mode in ("long", "both"):
            for m_in, m_out in ((12, 20), (20, 30), (40, 50)):
                hit = np.zeros(6)
                ntr = []
                for _ in range(SIMS):
                    trades = kc.generate(rng, days, close, ret, m_in, m_out, mode, 0.0)
                    expo = np.zeros(t)
                    for a, b, side in trades:
                        expo[a + 1:b + 1] = side * kc.FRAC
                    in_market = expo != 0
                    cost_per_day = len(trades) * kc.FRAC * kc.CAP * kc.COST / max(int(in_market.sum()), 1)
                    daily = expo * x - np.abs(expo) * gap - cost_per_day * in_market
                    b_dev = boot_share(daily[in_market], rng)
                    b_e2 = boot_share(daily[in_market & e2], rng)
                    checks, info = kc.evaluate(trades, days, close, ret, fund, known)
                    nets = checks["dev_net_stress"] and checks["e2_net_stress"] and checks["tail_net"]
                    kt = (all(checks[k] for k in ("dev_net_stress", "worst_year", "e1_floor", "counts", "e2_net_stress", "e2_without_best_half", "drop_top2_dev", "drop_top1_e2"))
                          and checks["tail_net"] and info["p_dev"] is not None and info["p_dev"] <= 0.05
                          and info["p_e2"] is not None and info["p_e2"] <= 0.10 and info["p_tail"] is not None and info["p_tail"] <= 0.50)
                    ok_dev, ok_e2 = b_dev >= 0.90, b_e2 >= 0.80
                    hit += (ok_dev, ok_e2, ok_dev and ok_e2, ok_dev and ok_e2 and nets, info["p_dev"] is not None and info["p_dev"] <= 0.05, kt)
                    ntr.append(len(trades))
                rate = hit / SIMS
                rows.append((mode, rate))
                print(inst[:3], mode, f"{m_in}/{m_out}", "|", round(float(np.mean(ntr))), "|", " | ".join(f"{v * 100:.1f} %" for v in rate), flush=True)
    for mode in ("long", "both"):
        sel = np.array([r for m, r in rows if m == mode])
        print(f"mean over cells, {mode}:", " | ".join(f"{v * 100:.1f} %" for v in sel.mean(axis=0)), "| range of the 4th column:",
              f"{sel[:, 3].min() * 100:.1f}–{sel[:, 3].max() * 100:.1f} %")


if __name__ == "__main__":
    main()
