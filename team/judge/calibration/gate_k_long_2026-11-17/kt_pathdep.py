"""Level of the shift test for PATH-DEPENDENT rules on price paths WITHOUT predictability.

The gate's null is "the exposure pattern is placed at random". A trend rule's exposure depends on past prices even when
it predicts nothing, so the test is checked here on synthetic paths: DEV daily returns of an instrument in random order
(iid permutation: no serial dependence at all) and in random order of 20-day blocks (volatility clustering kept inside
blocks). On such paths no rule has an edge; the share of p <= a should be about a.

Rules (daily closes only): channel breakout (enter on an N-day high, exit on an N/2-day low; with the mirror short side
in the "both" mode) and time-series momentum (sign of the L-day return, reviewed every 7 days). These rules are run on
permuted paths only, never on the real DEV path.
"""
import pathlib
import sys

import numpy as np

S = pathlib.Path(sys.argv[1])
SIMS = int(sys.argv[2])
sys.argv = ["kt", str(S / "dev_daily.json"), "1"]
sys.path.insert(0, str(S))
import kt_calibrate2 as kc  # noqa: E402


def breakout(close, n, both):
    t = len(close)
    e = np.zeros(t)
    pos = 0
    for d in range(n, t - 1):
        hi, lo = close[d - n:d].max(), close[d - n:d].min()
        half_hi, half_lo = close[d - n // 2:d].max(), close[d - n // 2:d].min()
        if pos == 1 and close[d] < half_lo:
            pos = 0
        elif pos == -1 and close[d] > half_hi:
            pos = 0
        if pos == 0:
            if close[d] > hi:
                pos = 1
            elif both and close[d] < lo:
                pos = -1
        e[d + 1] = pos
    return e


def momentum(close, lookback, both):
    t = len(close)
    e = np.zeros(t)
    pos = 0
    for d in range(lookback, t - 1):
        if (d - lookback) % 7 == 0:
            up = close[d] > close[d - lookback]
            pos = 1 if up else (-1 if both else 0)
        e[d + 1] = pos
    return e


RULES = [("breakout 20", lambda c, b: breakout(c, 20, b)), ("breakout 55", lambda c, b: breakout(c, 55, b)),
         ("momentum 20", lambda c, b: momentum(c, 20, b)), ("momentum 50", lambda c, b: momentum(c, 50, b)),
         ("momentum 100", lambda c, b: momentum(c, 100, b))]
ALPHAS = (0.25, 0.05, 0.0125, 0.00625, 0.0021)


def main():
    rng = np.random.default_rng(2026111707)
    print("path rule mode | days in market | P(p<=a) for a in", ALPHAS)
    total = {}
    for kind in ("iid", "block20"):
        for name, rule in RULES:
            for both in (False, True):
                ps, share = [], []
                for inst in ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"):
                    days, close, ret, fund, known = kc.series(inst)
                    r = ret[1:]
                    for _ in range(SIMS):
                        if kind == "iid":
                            perm = rng.permutation(r)
                        else:
                            cut = np.array_split(r, max(1, len(r) // 20))
                            perm = np.concatenate([cut[i] for i in rng.permutation(len(cut))])
                        path = np.concatenate([[1.0], np.cumprod(1 + perm)])
                        x = np.concatenate([[0.0], perm]) * 100
                        e = rule(path, both) * 0.2
                        if not np.any(e):
                            continue
                        t = len(e)
                        s = np.fft.irfft(np.conj(np.fft.rfft(e)) * np.fft.rfft(x), n=t)
                        ps.append((1 + int(np.sum(s[1:] >= s[0] - 1e-12))) / t)
                        share.append(np.mean(e != 0))
                ps = np.array(ps)
                rates = [float(np.mean(ps <= a)) for a in ALPHAS]
                total.setdefault(kind, []).append(rates)
                print(kind, name, "both" if both else "long", "|", round(float(np.mean(share)), 2), "|", " ".join(f"{v:.4f}" for v in rates), "| n", len(ps), flush=True)
    for kind, rows in total.items():
        print("mean over rules,", kind, ":", " ".join(f"{v:.4f}" for v in np.mean(rows, axis=0)), "| max:", " ".join(f"{v:.4f}" for v in np.max(rows, axis=0)))


if __name__ == "__main__":
    main()
