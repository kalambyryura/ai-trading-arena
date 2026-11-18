"""What the E2 and tail checks add: SYNTHETIC strategies whose skill ends on 2023-12-31 (strong before, none after).

An "author" declares 24 such configurations and submits the best by stress net on the selection window. Pass rate of the
whole gate with the single test (0.05 / m) is reported for the rule and for the rule without the E2 and tail timing
checks, and with no E2/tail checks at all (T1-T4 and T7 only).
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
CUT = np.datetime64("2024-01-01")


def generate_decay(rng, days, close, ret, m_in, m_out, mode, kappa):
    """As kc.generate, but the oracle thins the trades only before the cut; after it the coin is fair."""
    t, sigma = len(days), float(np.std(ret[1:]))
    trades, d = [], int(rng.integers(1, m_out + 1))
    while True:
        a = d
        b = a + 3 + int(rng.geometric(1.0 / max(m_in - 3, 1)))
        if b >= t:
            break
        side = 1 if mode == "long" else (1 if rng.random() < 0.5 else -1)
        k = kappa if days[a] < CUT else 0.0
        z = side * (close[b] / close[a] - 1) / (sigma * np.sqrt(b - a))
        if rng.random() < 1.0 / (1.0 + np.exp(-k * z)):
            trades.append((a, b, side))
            d = b + int(rng.geometric(1.0 / max(m_out // 2, 1)))
        else:
            d = b + 1 if k else a + int(rng.geometric(1.0 / max(m_out // 2, 1)))
            d = max(d, a + 1)
    return trades


BASE_ALL = ("dev_net_stress", "worst_year", "e1_floor", "counts", "e2_net_stress", "e2_without_best_half", "drop_top2_dev", "drop_top1_e2")
BASE_OLD = ("dev_net_stress", "worst_year", "e1_floor", "counts", "drop_top2_dev")


def main():
    rng = np.random.default_rng(2026111708)
    totals = {}
    for kappa in (1.0, 2.5):
        for m in (1, 24):
            rows = {"rule": [], "no E2 and tail timing": [], "no E2 and tail checks at all": []}
            for inst in ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"):
                days, close, ret, fund, known = kc.series(inst)
                for mode in ("long", "both"):
                    for m_in, m_out in ((12, 20), (20, 30)):
                        hit = {k: 0 for k in rows}
                        for _ in range(SIMS):
                            pool = [generate_decay(rng, days, close, ret, m_in, m_out, mode, kappa) for _ in range(m)]
                            trades = max(pool, key=lambda tr: kc.select_net(tr, days, close, fund, known))
                            checks, info = kc.evaluate(trades, days, close, ret, fund, known)
                            t4 = info["p_dev"] is not None and info["p_dev"] <= 0.05 / m
                            full = all(checks[k] for k in BASE_ALL) and checks["tail_net"]
                            hit["rule"] += t4 and full and checks["shift_e2"] and checks["shift_tail_median"]
                            hit["no E2 and tail timing"] += t4 and full
                            hit["no E2 and tail checks at all"] += t4 and all(checks[k] for k in BASE_OLD)
                        for k in rows:
                            rows[k].append(hit[k] / SIMS)
            print(f"skill until 2023 kappa {kappa}, best of {m}:", " | ".join(f"{k}: {np.mean(v) * 100:.1f} % ({np.min(v) * 100:.1f}–{np.max(v) * 100:.1f})" for k, v in rows.items()), flush=True)


if __name__ == "__main__":
    main()
