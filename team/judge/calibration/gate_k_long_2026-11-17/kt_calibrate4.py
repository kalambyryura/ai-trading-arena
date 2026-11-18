"""Selection-aware calibration of the long-horizon gate K (constant-notional timing statistic), raw values kept.

For every cell (instrument, side mode, trade frequency, skill) an "author" draws 24 SYNTHETIC configurations and submits
the best of the first m by stress net on the selection window (entries up to 2025-06-30), m in M. The selected
configuration's raw check values (base checks, p_DEV, p_E2, p_tail, tail net sign) go to an npz, so that any threshold
variant can be read off without a new simulation. A second null ("vol") starts positions more often after volatile days:
timing by volatility without any directional information.

No real trading rule is evaluated: positions are random renewal processes, thinned by a noisy oracle for skill.
"""
import pathlib
import sys

import numpy as np

S = pathlib.Path(sys.argv[1])
SIMS_NULL = int(sys.argv[2])
SIMS_SKILL = int(sys.argv[3])
OUT = pathlib.Path(sys.argv[4])
sys.argv = ["kt", str(S / "dev_daily.json"), "1"]
sys.path.insert(0, str(S))
import kt_calibrate2 as kc  # noqa: E402

kc.shift_p.__defaults__ = (1,)  # every non-zero circular shift: the p-value is then exact under random placement

M = (1, 4, 8, 12, 24)
BASE = ("dev_net_stress", "worst_year", "e1_floor", "counts", "e2_net_stress", "e2_without_best_half", "drop_top2_dev", "drop_top1_e2")


def generate_vol(rng, days, ret, m_in, m_out, mode):
    """Null with volatility timing: the entry hazard follows the trailing 10-day mean absolute return."""
    t = len(days)
    absr = np.abs(ret)
    rv = np.convolve(absr, np.ones(10) / 10, mode="full")[:t]  # days d-9..d: known at the close of day d
    weight = np.minimum(rv / np.median(rv[10:]), 4.0)
    base = 1.0 / (m_out * float(weight[10:].mean()))  # the same mean time out of the market as the plain null
    trades, d = [], 10
    while d < t:
        if rng.random() < min(1.0, base * weight[d]):
            b = d + 3 + int(rng.geometric(1.0 / max(m_in - 3, 1)))
            if b >= t:
                break
            side = 1 if mode == "long" else (1 if rng.random() < 0.5 else -1)
            trades.append((d, b, side))
            d = b + 1
        else:
            d += 1
    return trades


def main():
    rng = np.random.default_rng(2026111704)
    cells = {}
    for inst in ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"):
        days, close, ret, fund, known = kc.series(inst)
        for mode in ("long", "both"):
            for m_in, m_out in ((12, 20), (20, 30), (40, 50)):
                for kind in ("null", "vol", 0.5, 1.0, 1.5, 2.5):
                    sims = SIMS_NULL if kind in ("null", "vol") else SIMS_SKILL
                    raw = np.zeros((sims, len(M), 6))  # base, p_dev, p_e2, p_tail, tail_net, trades
                    for i in range(sims):
                        if kind == "vol":
                            pool = [generate_vol(rng, days, ret, m_in, m_out, mode) for _ in range(24)]
                        else:
                            pool = [kc.generate(rng, days, close, ret, m_in, m_out, mode, 0.0 if kind == "null" else kind) for _ in range(24)]
                        score = [kc.select_net(tr, days, close, fund, known) for tr in pool]
                        done = {}
                        for j, m in enumerate(M):
                            best = int(np.argmax(score[:m]))
                            if best not in done:
                                checks, info = kc.evaluate(pool[best], days, close, ret, fund, known)
                                done[best] = (float(all(checks[k] for k in BASE)), info["p_dev"] if info["p_dev"] is not None else 1.0,
                                              info["p_e2"] if info["p_e2"] is not None else 1.0, info["p_tail"] if info["p_tail"] is not None else 1.0,
                                              float(checks["tail_net"]), float(info["trades"]))
                            raw[i, j] = done[best]
                    key = f"{inst[:3]}|{mode}|{m_in}/{m_out}|{kind}"
                    cells[key] = raw
                    fp = {m: round(float(np.mean((raw[:, j, 0] > 0) & (raw[:, j, 1] <= 0.05 / m) & (raw[:, j, 2] <= 0.25) & (raw[:, j, 3] <= 0.5) & (raw[:, j, 4] > 0))), 3)
                          for j, m in enumerate(M)}
                    print(key, "| trades", round(float(raw[:, -1, 5].mean()), 1), "| pass with p_dev <= 0.05/m:", fp, flush=True)
    np.savez_compressed(OUT, **cells)
    print("written", OUT)


if __name__ == "__main__":
    main()
