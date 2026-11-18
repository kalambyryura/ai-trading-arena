"""Field calculator (B-15): "who finishes first" for a candidate inside a field of other recipes.

Ported from the Claude coordinator's V8 tool (v8tools/field_ff.py) so every author has the same one.
Takes tools/arena_eval.py reports computed on the SAME window matrix and reports, per participant:

  FF            first-place share; Cash (0 USDT) takes part; k participants tied within 1e-9 USDT
                for the best net each get 1/k of that window
  sole_first    share of windows won alone (strictly best, Cash included)
  below_cash    share of windows with net < -1e-9 USDT
  net_total / net_mean / net_worst in USDT (100 USDT per participant and window)

With --baseline (usually your own frozen version) it adds the paired difference candidate - baseline:
windows better/worse/equal, sum of net, FF change, and a UTC-day block bootstrap 90 % interval
(2000 resamples, fixed seed). Windows where any participant is INCOMPLETE are left out of the
ranking and listed. Secondary DEVELOPMENT evidence only: not a holdout, not a Jev test. FF rewards
size and tie-breaking; read it next to net and drawdown.

  py -3.12 -X utf8 -B tools/field_ff.py --candidate c01.json --baseline v1.json --field sol=sol_v1.json --field astra=astra_v1.json
"""
import argparse
import json
from pathlib import Path
import random
import sys

TOL = 1e-9
B = 2000
SEED = 20261002
SCHEMAS = {"arena-eval/v1", "claude-v8-eval/v1"}


def load(path):
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    if report.get("schema") not in SCHEMAS:
        raise SystemExit(f"{path}: not a tools/arena_eval.py report")
    return report


def per_window(report):
    return {w["id"]: (w["pnl_usdt"], w["status"] == "COMPLETE", w["day"]) for w in report["windows"]}


def ranking(windows, field):
    """field: name -> {id: (net, complete, day)}; returns per-name stats over the given window ids."""
    names = list(field) + ["cash"]
    ff = {n: 0.0 for n in names}
    sole = {n: 0 for n in names}
    below = {n: 0 for n in names}
    total = {n: 0.0 for n in names}
    worst = {n: 0.0 for n in names}
    for wid in windows:
        nets = {n: field[n][wid][0] for n in field}
        nets["cash"] = 0.0
        best = max(nets.values())
        top = [n for n, v in nets.items() if v >= best - TOL]
        for n in top:
            ff[n] += 1 / len(top)
        if len(top) == 1:
            sole[top[0]] += 1
        for n, v in nets.items():
            total[n] += v
            worst[n] = min(worst[n], v)
            if v < -TOL:
                below[n] += 1
    count = len(windows) or 1
    return {n: {"ff_pct": round(100 * ff[n] / count, 3), "sole_first_pct": round(100 * sole[n] / count, 3),
                "below_cash_pct": round(100 * below[n] / count, 3), "net_total_usdt": round(total[n], 6),
                "net_mean_usdt": round(total[n] / count, 6), "net_worst_usdt": round(worst[n], 6)} for n in names}


def compare(candidate, field, baseline=None, name="candidate"):
    """candidate/baseline: reports; field: {name: report}. Returns the result dict (no I/O)."""
    if name in field or name == "cash":
        raise SystemExit(f"candidate name {name!r} clashes with the field")
    matrix = candidate["matrix"]
    tables = {n: per_window(r) for n, r in field.items()}
    cand = per_window(candidate)
    base = per_window(baseline) if baseline else None
    ids = list(cand)
    for label, table in list(tables.items()) + ([("baseline", base)] if base else []):
        if list(table) != ids:
            raise SystemExit(f"window ids of {label} differ from the candidate (same full matrix needed, no --only)")
    others = [r for r in field.values()] + ([baseline] if baseline else [])
    if any(r["matrix"] != matrix for r in others):
        raise SystemExit("all reports must use the same window matrix")
    complete = [w for w in ids if cand[w][1] and all(t[w][1] for t in tables.values()) and (not base or base[w][1])]
    excluded = [w for w in ids if w not in complete]
    with_candidate = {**tables, name: cand}
    after = ranking(complete, with_candidate)
    result = {"schema": "arena-field/v1", "matrix": matrix, "candidate": name, "candidate_recipe_sha256": candidate["recipe_sha256"],
              "field_recipes": {n: r["recipe_sha256"] for n, r in field.items()},
              "windows": len(ids), "windows_ranked": len(complete), "windows_excluded_incomplete": excluded,
              "tie_tolerance_usdt": TOL, "field_with_candidate": after,
              "caveat": "Secondary development evidence on modelled candle tapes; Cash and ties included; not a holdout or Jev test."}
    if base:
        with_baseline = {**tables, name: base}
        before = ranking(complete, with_baseline)
        diffs = [cand[w][0] - base[w][0] for w in complete]
        days = sorted({cand[w][2] for w in complete})
        by_day = {d: [w for w in complete if cand[w][2] == d] for d in days}
        rng = random.Random(SEED)
        ff_d, net_d = [], []
        for _ in range(B if days else 0):
            sample = [w for d in (rng.choice(days) for _ in days) for w in by_day[d]]
            r0, r1 = ranking(sample, with_baseline)[name], ranking(sample, with_candidate)[name]
            ff_d.append(r1["ff_pct"] - r0["ff_pct"])
            net_d.append(r1["net_total_usdt"] - r0["net_total_usdt"])
        ff_d.sort()
        net_d.sort()
        q = lambda xs, p: round(xs[min(len(xs) - 1, max(0, int(p * len(xs))))], 4) if xs else None
        result.update(baseline_recipe_sha256=baseline["recipe_sha256"], field_with_baseline=before, paired_vs_baseline={
            "better": sum(d > TOL for d in diffs), "worse": sum(d < -TOL for d in diffs), "equal": sum(abs(d) <= TOL for d in diffs),
            "net_diff_total_usdt": round(sum(diffs), 6),
            "ff_diff_pp": round(after[name]["ff_pct"] - before[name]["ff_pct"], 3),
            "bootstrap": {"method": f"UTC-day blocks, {B} resamples, seed {SEED}", "days": len(days),
                          "ff_diff_pp_90ci": [q(ff_d, .05), q(ff_d, .95)],
                          "net_diff_total_usdt_90ci": [q(net_d, .05), q(net_d, .95)]}})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Who finishes first: a candidate inside a field of arena_eval reports (secondary stand).")
    parser.add_argument("--candidate", type=Path, required=True, help="tools/arena_eval.py report of the candidate")
    parser.add_argument("--name", default="candidate", help="the candidate's name in the tables")
    parser.add_argument("--field", action="append", default=[], help="name=report.json, repeat for every other participant")
    parser.add_argument("--baseline", type=Path, default=None, help="report of the version the candidate would replace (paired difference)")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    field = {}
    for item in args.field:
        name, _, path = item.partition("=")
        if not name or not path or name in field:
            raise SystemExit("--field needs unique name=report.json")
        field[name] = load(path)
    if not field:
        raise SystemExit("give at least one --field name=report.json")
    result = compare(load(args.candidate), field, load(args.baseline) if args.baseline else None, args.name)
    text = json.dumps(result, ensure_ascii=False, indent=1)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    short = {"matrix": result["matrix"], "ranked": result["windows_ranked"], "excluded": len(result["windows_excluded_incomplete"]),
             "FF_with_candidate": {n: v["ff_pct"] for n, v in result["field_with_candidate"].items()},
             "candidate": result["field_with_candidate"][args.name]}
    if "paired_vs_baseline" in result:
        short["paired_vs_baseline"] = result["paired_vs_baseline"]
    print(json.dumps(short, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
