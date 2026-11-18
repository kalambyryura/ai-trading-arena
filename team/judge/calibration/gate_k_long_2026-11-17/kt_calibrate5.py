"""Calibration of the family timing test of the long-horizon gate K on SYNTHETIC families over real DEV daily returns.

An "author" declares 24 configurations and submits the best of the first m by stress net on the selection window
(entries up to 2025-06-30). Two kinds of family: "indep" - 24 independent draws (the worst case for selection);
"corr" - one base list and 23 variants of it (80 % of the trades kept, entry and exit moved by up to 3 days), the way
neighbouring parameters of one rule behave. For the selected configuration the raw values are kept: base checks,
single-test p on DEV, family-test p on DEV (over the first m), p in E2, p in the tail, tail net sign.

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

kc.shift_p.__defaults__ = (1,)  # every non-zero circular shift
M = (4, 8, 24)
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


def variant(rng, base, t):
    out, prev_end = [], 0
    for a, b, side in base:
        if rng.random() > 0.8:
            continue
        a2 = max(a + int(rng.integers(-3, 4)), prev_end + 1, 1)
        b2 = max(b + int(rng.integers(-3, 4)), a2 + 3)
        if b2 >= t:
            continue
        out.append((a2, b2, side))
        prev_end = b2
    return out


def main():
    rng = np.random.default_rng(2026111705)
    cells = {}
    for inst in ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"):
        days, close, ret, fund, known = kc.series(inst)
        t = len(days)
        fx_lin = np.fft.rfft(kc.CAP * (ret - fund * known))
        fx_abs = np.fft.rfft(-kc.CAP * kc.GAP_RATE * (~known))
        for mode in ("long", "both"):
            for m_in, m_out in ((12, 20), (40, 50)):
                for kind in ("null", "vol", 0.5, 1.0, 2.5):
                    for fam in ("indep", "corr"):
                        sims = SIMS_NULL if kind in ("null", "vol") else SIMS_SKILL
                        raw = np.zeros((sims, len(M), 7))  # base, p_single, p_family, p_e2, p_tail, tail_net, trades
                        corr = []
                        for i in range(sims):
                            def draw():
                                if kind == "vol":
                                    return generate_vol(rng, days, ret, m_in, m_out, mode)
                                return kc.generate(rng, days, close, ret, m_in, m_out, mode, 0.0 if kind == "null" else kind)
                            if fam == "indep":
                                pool = [draw() for _ in range(24)]
                            else:
                                base = draw()
                                pool = [base] + [variant(rng, base, t) for _ in range(23)]
                            expo = np.zeros((24, t))
                            for j, trades in enumerate(pool):
                                for a, b, side in trades:
                                    expo[j, a + 1:b + 1] = side * kc.FRAC
                            sums = (np.fft.irfft(np.conj(np.fft.rfft(expo, axis=1)) * fx_lin, n=t, axis=1)
                                    + np.fft.irfft(np.conj(np.fft.rfft(np.abs(expo), axis=1)) * fx_abs, n=t, axis=1))
                            sd = sums.std(axis=1, keepdims=True)
                            z = np.where(sd > 0, (sums - sums.mean(axis=1, keepdims=True)) / np.where(sd > 0, sd, 1), -np.inf)
                            score = [kc.select_net(tr, days, close, fund, known) for tr in pool]
                            if i < 40:
                                c = np.corrcoef(expo[:6])
                                corr.append(float(np.nanmean(c[np.triu_indices(6, 1)])))
                            done = {}
                            for j, m in enumerate(M):
                                best = int(np.argmax(score[:m]))
                                if best not in done:
                                    checks, info = kc.evaluate(pool[best], days, close, ret, fund, known)
                                    done[best] = (float(all(checks[k] for k in BASE)), info["p_e2"] if info["p_e2"] is not None else 1.0,
                                                  info["p_tail"] if info["p_tail"] is not None else 1.0, float(checks["tail_net"]), float(info["trades"]))
                                base_ok, p_e2, p_tail, tail_net, n = done[best]
                                if np.isfinite(z[best, 0]):
                                    p_single = (1 + int(np.sum(sums[best, 1:] >= sums[best, 0] - 1e-12))) / t
                                    top = z[:m].max(axis=0)
                                    p_family = (1 + int(np.sum(top[1:] >= z[best, 0] - 1e-12))) / t
                                else:
                                    p_single = p_family = 1.0
                                raw[i, j] = (base_ok, p_single, p_family, p_e2, p_tail, tail_net, n)
                        key = f"{inst[:3]}|{mode}|{m_in}/{m_out}|{kind}|{fam}"
                        cells[key] = raw
                        rest = (raw[:, :, 0] > 0) & (raw[:, :, 3] <= 0.25) & (raw[:, :, 4] <= 0.5) & (raw[:, :, 5] > 0)
                        single = {m: round(float(np.mean(rest[:, j] & (raw[:, j, 1] <= 0.05 / m))), 3) for j, m in enumerate(M)}
                        family = {m: round(float(np.mean(rest[:, j] & (raw[:, j, 2] <= 0.05))), 3) for j, m in enumerate(M)}
                        t4s = {m: round(float(np.mean(raw[:, j, 1] <= 0.05 / m)), 3) for j, m in enumerate(M)}
                        t4f = {m: round(float(np.mean(raw[:, j, 2] <= 0.05)), 3) for j, m in enumerate(M)}
                        print(key, "| trades", round(float(raw[:, -1, 6].mean()), 1), "| exposure corr", round(float(np.mean(corr)), 2),
                              "| T4 alone single", t4s, "family", t4f, "| whole gate single", single, "family", family, flush=True)
    np.savez_compressed(OUT, **cells)
    print("written", OUT)


if __name__ == "__main__":
    main()
