"""Judge: market outcomes for Opus's J2 labels (news shock), computed only on FORWARD BTC data (after 2026-10-28 00:00 UTC).

Input: the answers file written by tools/jev_research.py (one JSON record per line: decision_at, mode, answers.{shock,
scope,direction,veto}.{choice,confidence}, anchor fields). The Judge never reads the digests themselves. Output: one JSON
with per-group aggregates and the J2 / J2a tests; per-hour volatilities stay in the Judge's vault (--detail), never in Git.

  py -3.12 -X utf8 -B team/judge/tools/j2_outcomes.py --answers <answers.jsonl> --store vault/gate_d/BTC-USDT-SWAP
      --out <summary.json> [--detail <per_hour.json>] [--baselines <baselines.jsonl>] [--boundary 2026-10-28T00:00:00Z]

Outcome for a decision at T (HH:05:00Z grid, J2b): realized volatility sigma_h = sqrt(sum of squared 1m log returns of the
close) over (T, T+h] for h = 1, 4, 24 hours, in bps; r4h = log(close(T+4h) / close(T)) in bps. Every minute of the window must
exist, otherwise the outcome is UNKNOWN and the case is skipped. Normalisations: n_grid = sigma_4h / median over all scored
grid hours (J2 criterion 1); n_hod = sigma_4h / median of sigma_4h at the same hour of day over the previous 30 FORWARD days
(J2a section 2; needs at least 7 earlier days after the boundary, else UNKNOWN). Only cases with a committed anchor count
(J2c); no_news hours form their own group. No scipy: Mann-Whitney uses the normal approximation with tie correction,
Spearman's p-value a permutation test.
"""
from __future__ import annotations

import argparse
from bisect import bisect_left
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys

import numpy as np

HERE = Path(__file__).resolve()
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT / "tools"))
import team_data  # noqa: E402

MINUTE = 60_000
HOUR = 3_600_000
DAY = 86_400_000
HORIZONS = {"1h": 60, "4h": 240, "24h": 1440}


def parse_utc(text):
    return round(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)


def utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_closes(store, start_ms, end_ms):
    rows = team_data.load_candles(store, start_ms, end_ms)
    stamps = np.array(sorted(rows), dtype=np.int64)
    closes = np.array([float(rows[int(s)][4]) for s in stamps])
    return stamps, closes


def outcome(stamps, closes, t_ms):
    """sigma per horizon (bps) and r4h (bps) over the minutes opening in [t, t + h); None when any minute is missing."""
    index = stamps.tolist()
    k = bisect_left(index, t_ms)
    out = {}
    for name, minutes in HORIZONS.items():
        j = k + minutes
        if k == 0 or j > len(stamps) or stamps[k] != t_ms or stamps[j - 1] != t_ms + (minutes - 1) * MINUTE:
            out["sigma_" + name] = None
            continue
        window = closes[k - 1:j]  # the close before T, then every close inside the horizon
        logs = np.diff(np.log(window)) * 10000
        out["sigma_" + name] = float(math.sqrt(float(np.sum(logs * logs))))
    j = k + 240
    ok = k > 0 and j <= len(stamps) and stamps[k] == t_ms and stamps[j - 1] == t_ms + 239 * MINUTE
    out["r4h"] = float(math.log(closes[j - 1] / closes[k - 1]) * 10000) if ok else None
    return out


def ranks_with_ties(values):
    values = np.asarray(values, float)
    order = values.argsort(kind="mergesort")
    ranks = np.empty(len(values))
    ordered = values[order]
    i = 0
    while i < len(values):
        j = i
        while j + 1 < len(values) and ordered[j + 1] == ordered[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def mann_whitney_one_sided(x, y):
    """One-sided test that x tends to be larger than y: U statistic, normal approximation with tie correction."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    n1, n2 = len(x), len(y)
    if n1 < 1 or n2 < 1:
        return None, None
    pooled = np.concatenate([x, y])
    ranks = ranks_with_ties(pooled)
    u = float(ranks[:n1].sum() - n1 * (n1 + 1) / 2)
    mean = n1 * n2 / 2
    _, counts = np.unique(pooled, return_counts=True)
    n = n1 + n2
    tie = float(((counts ** 3 - counts).sum()) / (n * (n - 1))) if n > 1 else 0.0
    var = n1 * n2 / 12 * ((n + 1) - tie)
    if var <= 0:
        return u, None
    z = (u - mean - 0.5) / math.sqrt(var)
    return u, float(0.5 * math.erfc(z / math.sqrt(2)))


def spearman(x, y, permutations=20000, seed=0):
    """Spearman rho and a one-sided permutation p-value for rho > 0."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    if len(x) < 5 or np.all(x == x[0]) or np.all(y == y[0]):
        return None, None
    rx, ry = ranks_with_ties(x), ranks_with_ties(y)
    rho = float(np.corrcoef(rx, ry)[0, 1])
    rng = np.random.default_rng(seed)
    count = 0
    for _ in range(permutations):
        if np.corrcoef(rx, rng.permutation(ry))[0, 1] >= rho:
            count += 1
    return rho, (count + 1) / (permutations + 1)


def score_of(answers):
    """J2a section 1: confidence for major, half of it for minor, 0 for none."""
    shock = (answers or {}).get("shock") or {}
    confidence = float(shock.get("confidence") or 0.0)
    return {"major": confidence, "minor": 0.5 * confidence, "none": 0.0}.get(shock.get("choice"), 0.0)


def median(values):
    values = [v for v in values if v is not None]
    return float(np.median(values)) if values else None


def share_negative(cases):
    return (sum(c["r4h"] < 0 for c in cases) / len(cases)) if cases else None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--answers", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True, help="BTC-USDT-SWAP store with FORWARD minutes (gate_d or forward)")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--detail", type=Path, help="per-hour outcomes (vault only, never Git)")
    parser.add_argument("--baselines", type=Path, help="JSONL {decision_at, headline_count, keyword_count} from Opus (numbers only)")
    parser.add_argument("--boundary", default="2026-10-28T00:00:00Z")
    parser.add_argument("--allow-unanchored", action="store_true", help="count cases without a committed anchor (never for a verdict)")
    args = parser.parse_args(argv)
    boundary = parse_utc(args.boundary)
    records = [json.loads(line) for line in args.answers.read_text(encoding="utf-8").splitlines() if line.strip()]
    cases, skipped = [], {"no_decision_at": 0, "before_boundary": 0, "refused": 0, "unanchored": 0, "hold_or_invalid": 0, "unknown_outcome": 0}
    for record in records:
        at = record.get("decision_at")
        if not at:
            skipped["no_decision_at"] += 1
            continue
        t = parse_utc(at)
        if t < boundary:
            skipped["before_boundary"] += 1
            continue
        mode = record.get("mode")
        if mode == "refused":
            skipped["refused"] += 1
            continue
        anchored = bool(record.get("committed_anchor_sha256")) and bool(record.get("anchor_heartbeat_sha256"))
        if not args.allow_unanchored and not anchored:
            skipped["unanchored"] += 1
            continue
        if mode != "no_news" and record.get("answers") is None:
            skipped["hold_or_invalid"] += 1
            continue
        cases.append({"t": t, "decision_at": utc(t), "mode": mode, "answers": record.get("answers")})
    if not cases:
        raise SystemExit("no usable cases")
    first, last = min(c["t"] for c in cases), max(c["t"] for c in cases)
    stamps, closes = load_closes(args.store, max(boundary, first - 31 * DAY), last + 25 * HOUR)
    if len(stamps) and stamps[0] < boundary:
        raise SystemExit("store minutes before the boundary must not be used")
    for c in cases:
        c.update(outcome(stamps, closes, c["t"]))
    scored = [c for c in cases if c["sigma_4h"] is not None]
    skipped["unknown_outcome"] = len(cases) - len(scored)
    grid_median = median([c["sigma_4h"] for c in scored])
    by_hod = {}
    for c in sorted(scored, key=lambda item: item["t"]):
        hod = (c["t"] // HOUR) % 24
        history = [v for (tt, v) in by_hod.get(hod, []) if c["t"] - 30 * DAY <= tt < c["t"]]
        c["n_hod"] = (c["sigma_4h"] / float(np.median(history))) if len(history) >= 7 and np.median(history) > 0 else None
        c["n_grid"] = c["sigma_4h"] / grid_median if grid_median else None
        by_hod.setdefault(hod, []).append((c["t"], c["sigma_4h"]))
        c["score"] = score_of(c["answers"]) if c["mode"] != "no_news" else None
        answers = c["answers"] or {}
        for q in ("shock", "scope", "direction", "veto"):
            c[q] = (answers.get(q) or {}).get("choice") if c["mode"] != "no_news" else "no_news"
    news = [c for c in scored if c["mode"] != "no_news"]
    major = [c for c in news if c["shock"] == "major"]
    rest = [c for c in news if c["shock"] != "major"]
    wait = [c for c in news if c["veto"] == "wait"]
    allow = [c for c in news if c["veto"] != "wait"]
    u_major, p_major = mann_whitney_one_sided([c["sigma_4h"] for c in major], [c["sigma_4h"] for c in rest])
    u_wait, p_wait = mann_whitney_one_sided([c["sigma_4h"] for c in wait], [c["sigma_4h"] for c in allow])
    hod_cases = [c for c in news if c["n_hod"] is not None]
    rho, p_rho = spearman([c["score"] for c in hod_cases], [c["n_hod"] for c in hod_cases])
    by_t = {c["t"]: c for c in news}
    placebo = [(by_t[c["t"] - DAY]["score"], c["n_hod"]) for c in hod_cases if c["t"] - DAY in by_t]
    rho_placebo, p_placebo = spearman([s for s, _ in placebo], [n for _, n in placebo])
    risk_off = [c for c in major if c["direction"] == "risk_off" and c["r4h"] is not None]
    others = [c for c in news if not (c["shock"] == "major" and c["direction"] == "risk_off") and c["r4h"] is not None]
    baselines = None
    if args.baselines and args.baselines.is_file():
        rows = [json.loads(line) for line in args.baselines.read_text(encoding="utf-8").splitlines() if line.strip()]
        base = {row["decision_at"]: row for row in rows}
        paired = [(base[c["decision_at"]], c) for c in hod_cases if c["decision_at"] in base]
        baselines = {}
        for key in ("headline_count", "keyword_count"):
            pairs = [(float(b[key]), c["n_hod"]) for b, c in paired if b.get(key) is not None]
            r, p = spearman([x for x, _ in pairs], [y for _, y in pairs])
            baselines[key] = {"n": len(pairs), "spearman": r, "p_one_sided": p}
    shock_counts = {k: sum(c["shock"] == k for c in news) for k in ("none", "minor", "major")}
    degenerate = bool(news) and (any(v / len(news) < 0.03 for v in shock_counts.values()) or max(shock_counts.values()) / len(news) > 0.90)
    major_norm = median([c["n_grid"] for c in major])
    wait_share = (len(wait) / len(news)) if news else None
    if degenerate:
        verdict = "DEGENERATE"
    elif len(major) < 10 or len(news) < 500:
        verdict = "INSUFFICIENT"
    elif p_major is not None and p_major < 0.05 and major_norm is not None and major_norm >= 1.5 and wait_share is not None and wait_share <= 0.10:
        verdict = "PASS"
    else:
        verdict = "FAIL"
    summary = {
        "schema": "judge-j2-outcomes/v1", "boundary_utc": args.boundary, "store": str(args.store).replace("\\", "/"),
        "store_catalog_sha256": team_data.sha256_file(args.store / team_data.CATALOG),
        "records": len(records), "cases_used": len(cases), "cases_scored": len(scored), "skipped": skipped,
        "groups": {"news_hours": len(news), "no_news_hours": len(scored) - len(news), "major": len(major), "minor": shock_counts["minor"],
                   "none": shock_counts["none"], "veto_wait": len(wait), "veto_wait_share": wait_share},
        "sigma_4h_bps_median": {"all_grid": grid_median, "news": median([c["sigma_4h"] for c in news]),
                                "no_news": median([c["sigma_4h"] for c in scored if c["mode"] == "no_news"]),
                                "major": median([c["sigma_4h"] for c in major]), "rest": median([c["sigma_4h"] for c in rest]),
                                "veto_wait": median([c["sigma_4h"] for c in wait]), "veto_allow": median([c["sigma_4h"] for c in allow])},
        "n_grid_median": {"major": major_norm, "veto_wait": median([c["n_grid"] for c in wait])},
        "tests": {
            "J2_1_major_vs_rest": {"criterion": "median n_grid(major) >= 1.5 and one-sided Mann-Whitney p < 0.05", "U": u_major, "p_one_sided": p_major},
            "J2_1_wait_vs_allow": {"U": u_wait, "p_one_sided": p_wait},
            "J2_2_major_count": {"criterion": ">= 10", "value": len(major)},
            "J2_3_wait_share": {"criterion": "<= 0.10", "value": wait_share},
            "J2_4_direction": {"share_negative_r4h_major_risk_off": share_negative(risk_off), "n": len(risk_off),
                               "share_negative_r4h_other": share_negative(others), "n_other": len(others)},
            "J2a_2_spearman_score_vs_n_hod": {"criterion": "rho > 0, one-sided permutation p < 0.05, >= 0.05 above the best code baseline",
                                              "n": len(hod_cases), "rho": rho, "p_one_sided": p_rho},
            "J2a_3_placebo_shift_24h": {"criterion": "p >= 0.05 and at least 0.05 weaker than the real labels", "n": len(placebo),
                                        "rho": rho_placebo, "p_one_sided": p_placebo},
            "J2a_4_sample": {"criterion": ">= 500 news hours and >= 10 major", "news_hours": len(news), "major": len(major)},
            "J2a_1_degenerate": {"shock_counts": shock_counts, "degenerate": degenerate},
            "code_baselines": baselines},
        "verdict_numeric_J2": verdict,
        "note": "Aggregates only. Per-hour volatilities are FORWARD market data and stay in the Judge's vault (--detail). "
                "The numeric verdict covers J2 criteria 1-3; J2a criteria 2-3 also need the code baselines from Opus (numbers per hour, no texts).",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if args.detail:
        args.detail.parent.mkdir(parents=True, exist_ok=True)
        rows = [{k: v for k, v in c.items() if k not in ("t", "answers")} for c in scored]
        args.detail.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"verdict": verdict, "cases_scored": len(scored), "news_hours": len(news), "major": len(major),
                      "p_major": p_major, "rho": rho}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
