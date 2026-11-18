"""Tables from the raw calibration files of the long-horizon gate K (kt_raw4.npz: single test; kt_raw5.npz: family test)."""
import pathlib
import sys

import numpy as np

S = pathlib.Path(sys.argv[1])
E2_RULE, TAIL_RULE = 0.10, 0.50  # the timing thresholds of the rule in E2 and in the tail


def agg(values):
    values = np.array(values)
    return f"{values.mean() * 100:5.1f} % ({values.min() * 100:.1f}–{values.max() * 100:.1f})"


def table4():
    data = np.load(S / "kt_raw4.npz")
    m_list = (1, 4, 8, 12, 24)
    kinds = sorted({k.split("|")[3] for k in data.files}, key=lambda x: (x not in ("null", "vol"), x))
    print("== kt_raw4: single test p_DEV <= 0.05/m, E2 p <= 0.10, tail p <= 0.50, tail net > 0, base checks; cells:", len(data.files))
    for e2_max, tail_max, label in ((E2_RULE, TAIL_RULE, "rule (E2 0.10, tail 0.50)"), (0.25, 0.50, "E2 0.25"), (1.0, 0.50, "no E2 timing"), (E2_RULE, 1.0, "no tail timing"),
                                    (E2_RULE, 0.25, "tail 0.25"), (0.05, 0.50, "E2 0.05")):
        print(f"-- variant: {label}")
        for kind in kinds:
            row = []
            for j, m in enumerate(m_list):
                rates = []
                for key in data.files:
                    if key.split("|")[3] != kind:
                        continue
                    raw = data[key]
                    ok = (raw[:, j, 0] > 0) & (raw[:, j, 1] <= 0.05 / m) & (raw[:, j, 2] <= e2_max) & (raw[:, j, 3] <= tail_max) & (raw[:, j, 4] > 0)
                    rates.append(ok.mean())
                row.append(f"m={m}: {agg(rates)}")
            print(f"   {kind:>5}: " + " | ".join(row))
    print("-- T4 alone (level of the single test), null and vol cells")
    for kind in ("null", "vol"):
        row = []
        for j, m in enumerate(m_list):
            rates = [(data[key][:, j, 1] <= 0.05 / m).mean() for key in data.files if key.split("|")[3] == kind]
            row.append(f"m={m}: {agg(rates)}")
        print(f"   {kind:>5}: " + " | ".join(row))
    print("-- by frequency, rule, m = 1, 4, 24")
    for freq in ("12/20", "20/30", "40/50"):
        for kind in kinds:
            row = []
            for j, m in enumerate(m_list):
                if m not in (1, 4, 24):
                    continue
                rates = []
                for key in data.files:
                    parts = key.split("|")
                    if parts[3] != kind or parts[2] != freq:
                        continue
                    raw = data[key]
                    rates.append(((raw[:, j, 0] > 0) & (raw[:, j, 1] <= 0.05 / m) & (raw[:, j, 2] <= E2_RULE) & (raw[:, j, 3] <= TAIL_RULE) & (raw[:, j, 4] > 0)).mean())
                row.append(f"m={m}: {agg(rates)}")
            trades = np.mean([data[key][:, -1, 5].mean() for key in data.files if key.split("|")[3] == kind and key.split("|")[2] == freq])
            print(f"   {freq} {kind:>5} (trades {trades:.0f}): " + " | ".join(row))


def table5():
    data = np.load(S / "kt_raw5.npz")
    m_list = (4, 8, 24)
    kinds = sorted({k.split("|")[3] for k in data.files}, key=lambda x: (x not in ("null", "vol"), x))
    print("== kt_raw5: family test vs single test, rule E2 0.10 and tail 0.50; cells:", len(data.files))
    for fam in ("indep", "corr"):
        print(f"-- family kind: {fam}")
        for kind in kinds:
            for name, col, limit in (("single 0.05/m", 1, None), ("family 0.05", 2, 0.05), ("family 0.05/3", 2, 0.05 / 3)):
                row, alone = [], []
                for j, m in enumerate(m_list):
                    rates, t4 = [], []
                    for key in data.files:
                        parts = key.split("|")
                        if parts[3] != kind or parts[4] != fam:
                            continue
                        raw = data[key]
                        lim = 0.05 / m if limit is None else limit
                        rest = (raw[:, j, 0] > 0) & (raw[:, j, 3] <= E2_RULE) & (raw[:, j, 4] <= TAIL_RULE) & (raw[:, j, 5] > 0)
                        rates.append((rest & (raw[:, j, col] <= lim)).mean())
                        t4.append((raw[:, j, col] <= lim).mean())
                    row.append(f"m={m}: {agg(rates)}")
                    alone.append(f"{np.mean(t4) * 100:.1f}")
                print(f"   {kind:>5} {name:>14}: " + " | ".join(row) + "   || T4 alone, mean %: " + " / ".join(alone))


for name, func in (("kt_raw4.npz", table4), ("kt_raw5.npz", table5)):
    if (S / name).exists():
        func()
    else:
        print(name, "not ready")
