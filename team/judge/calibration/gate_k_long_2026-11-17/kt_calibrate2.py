"""Calibration of the long-horizon gate K draft on SYNTHETIC strategies over real DEV daily returns.

No real trading rule is evaluated here: positions are random renewal processes (null) or the same thinned by an oracle
with noise (controlled skill kappa). Output: pass rate of every check and of their conjunction, by instrument, trade
frequency, side mode and skill.
"""
import json
import pathlib
import sys

import numpy as np

DATA = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
SIMS = int(sys.argv[2]) if len(sys.argv) > 2 else 1000
CAP, FRAC = 100.0, 0.20
COST, STRESS = 2 * 0.0007, 2 * 0.0005  # taker in and out: 5 + 2 bps each; stress +5 bps each
GAP_RATE = 0.0003  # conservative funding per day where the history has none (1 bps per 8 h, against the position)
L_MIN = 7


def series(inst):
    d = DATA[inst]
    days = np.array(d["day"], dtype="datetime64[D]")
    close = np.array(d["close"])
    ret = np.zeros(len(close))
    ret[1:] = close[1:] / close[:-1] - 1
    fund = np.array(d["funding_sum"])
    known = np.array(d["funding_known"])
    return days, close, ret, fund, known


def shift_p(e, x_lin, x_abs, lo=L_MIN):
    """One-sided p-value of S(0) against circular shifts of the exposure series."""
    t = len(e)
    if t < 4 * lo or not np.any(e):
        return None
    fe, fa = np.fft.rfft(e), np.fft.rfft(np.abs(e))
    s = np.fft.irfft(np.conj(fe) * np.fft.rfft(x_lin), n=t) + np.fft.irfft(np.conj(fa) * np.fft.rfft(x_abs), n=t)
    ks = np.arange(lo, t - lo + 1)
    return (1 + int(np.sum(s[ks] >= s[0] - 1e-12))) / (1 + len(ks))


def evaluate(trades, days, close, ret, fund, known, alpha_dev=0.10, alpha_e2=0.25):
    """trades: list of (a, b, side) = enter at the close of day a, exit at the close of day b."""
    t = len(days)
    e = np.zeros(t)
    rows = []
    for a, b, side in trades:
        e[a + 1:b + 1] = side * FRAC
        gross = side * FRAC * CAP * (close[b] / close[a] - 1)
        held = slice(a + 1, b + 1)
        funding = -side * FRAC * CAP * float(np.sum(fund[held] * known[held])) - FRAC * CAP * GAP_RATE * float(np.sum(~known[held]))
        net = gross - FRAC * CAP * COST + funding
        rows.append((a, b, net, net - FRAC * CAP * STRESS))
    rows = np.array(rows) if rows else np.zeros((0, 4))
    entry_day = days[rows[:, 0].astype(int)] if len(rows) else np.array([], dtype="datetime64[D]")
    net, stress = rows[:, 2], rows[:, 3]
    x_lin = CAP * (ret - fund * known)
    x_abs = -CAP * GAP_RATE * (~known)
    e2 = days >= np.datetime64("2024-01-01")
    tail = days >= np.datetime64("2025-07-01")
    in_e2, in_tail, in_e1 = entry_day >= np.datetime64("2024-01-01"), entry_day >= np.datetime64("2025-07-01"), entry_day < np.datetime64("2024-01-01")
    years = entry_day.astype("datetime64[Y]")
    worst_year = min((float(net[years == y].sum()) for y in np.unique(years)), default=0.0)
    halves = []
    for start, end in (("2024-01-01", "2024-07-01"), ("2024-07-01", "2025-01-01"), ("2025-01-01", "2025-07-01"),
                       ("2025-07-01", "2026-01-01"), ("2026-01-01", "2026-07-01")):
        m = (entry_day >= np.datetime64(start)) & (entry_day < np.datetime64(end))
        halves.append(float(net[m].sum()))
    holds = rows[:, 1] - rows[:, 0]
    p_dev = shift_p(e, x_lin, x_abs)
    p_e2 = shift_p(e[e2], x_lin[e2], x_abs[e2])
    p_tail = shift_p(e[tail], x_lin[tail], x_abs[tail])
    srt = np.sort(net)[::-1]
    srt_e2 = np.sort(net[in_e2])[::-1]
    checks = {
        "dev_net_stress": net.sum() > 0 and stress.sum() > 0,
        "worst_year": worst_year >= -10.0,
        "e1_floor": stress[in_e1].sum() >= -5.0,
        "counts": len(rows) >= 15 and in_e2.sum() >= 6 and in_tail.sum() >= 2 and len(rows) < 100 and (np.median(holds) >= 3 if len(rows) else False),
        "shift_dev": p_dev is not None and p_dev <= alpha_dev,
        "e2_net_stress": net[in_e2].sum() > 0 and stress[in_e2].sum() > 0,
        "shift_e2": p_e2 is not None and p_e2 <= alpha_e2,
        "e2_without_best_half": (net[in_e2].sum() - max(halves)) >= 0,
        "shift_dev_05": p_dev is not None and p_dev <= 0.05,
        "shift_tail_40": p_tail is not None and p_tail <= 0.40,
        "tail_net": net[in_tail].sum() > 0,
        "shift_tail_median": p_tail is not None and p_tail <= 0.5,
        "drop_top2_dev": (net.sum() - srt[:2].sum()) >= 0 if len(srt) >= 2 else False,
        "drop_top1_e2": (net[in_e2].sum() - srt_e2[:1].sum()) >= 0 if len(srt_e2) >= 1 else False,
    }
    info = {"trades": len(rows), "net": float(net.sum()), "p_dev": p_dev, "p_e2": p_e2, "p_tail": p_tail,
            "exposure": float(np.mean(np.abs(e) > 0))}
    return checks, info


def generate(rng, days, close, ret, m_in, m_out, mode, kappa):
    t, sigma = len(days), float(np.std(ret[1:]))
    trades, d = [], int(rng.integers(1, m_out + 1))
    while True:
        a = d
        b = a + 3 + int(rng.geometric(1.0 / max(m_in - 3, 1)))
        if b >= t:
            break
        side = 1 if mode == "long" else (1 if rng.random() < 0.5 else -1)
        z = side * (close[b] / close[a] - 1) / (sigma * np.sqrt(b - a))
        if rng.random() < 1.0 / (1.0 + np.exp(-kappa * z)):  # kappa = 0: a coin, no information
            trades.append((a, b, side))
            d = b + int(rng.geometric(1.0 / max(m_out // 2, 1)))
        else:
            d = b + 1 if kappa else a + int(rng.geometric(1.0 / max(m_out // 2, 1)))
            d = max(d, a + 1)
    return trades


def select_net(trades, days, close, fund, known):
    """Stress net of the trades entered up to 2025-06-30 (the selection window of D199)."""
    total = 0.0
    cut = np.datetime64("2025-07-01")
    for a, b, side in trades:
        if days[a] >= cut:
            continue
        held = slice(a + 1, b + 1)
        total += side * FRAC * CAP * (close[b] / close[a] - 1) - FRAC * CAP * (COST + STRESS)             - side * FRAC * CAP * float(np.sum(fund[held] * known[held])) - FRAC * CAP * GAP_RATE * float(np.sum(~known[held]))
    return total


VARIANTS = {"A a_dev.10 tail.50": ("shift_dev", "shift_tail_median"), "B a_dev.05 tail.50": ("shift_dev_05", "shift_tail_median"),
            "C a_dev.05 tail.40": ("shift_dev_05", "shift_tail_40"), "D a_dev.10 tail.40": ("shift_dev", "shift_tail_40")}
COMMON = ("dev_net_stress", "worst_year", "e1_floor", "counts", "e2_net_stress", "shift_e2", "e2_without_best_half", "tail_net", "drop_top2_dev", "drop_top1_e2")


def verdicts(checks):
    return {name: all(checks[k] for k in COMMON) and checks[a] and checks[b] for name, (a, b) in VARIANTS.items()}


def main():
    rng = np.random.default_rng(20261117)
    mode_run = sys.argv[3] if len(sys.argv) > 3 else "config"
    print("mode", mode_run, "sims", SIMS)
    for inst in ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"):
        days, close, ret, fund, known = series(inst)
        for mode in ("long", "both"):
            for m_in, m_out in ((12, 20), (20, 30), (40, 50)):
                for kappa in (0.0, 0.5, 1.0, 2.5):
                    tally, vt, ntr = {}, {k: 0 for k in VARIANTS}, []
                    for _ in range(SIMS):
                        if mode_run == "author":  # 24 configurations, the best on the selection window is submitted
                            pool = [generate(rng, days, close, ret, m_in, m_out, mode, kappa) for _ in range(24)]
                            trades = max(pool, key=lambda tr: select_net(tr, days, close, fund, known))
                        else:
                            trades = generate(rng, days, close, ret, m_in, m_out, mode, kappa)
                        checks, info = evaluate(trades, days, close, ret, fund, known)
                        ntr.append(info["trades"])
                        for k, v in checks.items():
                            tally[k] = tally.get(k, 0) + bool(v)
                        for k, v in verdicts(checks).items():
                            vt[k] += v
                    keys = [k for k in tally]
                    print(inst[:3], mode, f"{m_in}/{m_out}", kappa, "| n", round(float(np.mean(ntr)), 1), "|",
                          " ".join(f"{k}={tally[k] / SIMS:.2f}" for k in keys), "||", " ".join(f"{k.split()[0]}={v / SIMS:.3f}" for k, v in vt.items()), flush=True)


if __name__ == "__main__":
    main()
