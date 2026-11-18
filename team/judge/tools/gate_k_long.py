"""Judge: gate K for long-horizon strategies with few trades (track K-T, D198 and D199) on a DEV trade ledger.

The regular gate K asks for at least 100 closed trades and a result that survives without its 10 best trades. A strategy
that holds for days or weeks makes 4-15 trades a year per instrument, so these two checks fail by construction. This
tool applies the adapted track fixed in team/judge/GATE_K_LONG_UA.md: the unit of evidence is the day in the market,
not the trade, and the statistical guard is a timing test that buy-and-hold cannot pass.

  py -3.12 -X utf8 -B team/judge/tools/gate_k_long.py --ledger <team_eval DEV ledger.json> --configs <m> [--family <files or folder> --instruments <q>] [--out <dir>]
  py -3.12 -X utf8 -B team/judge/tools/gate_k_long.py --trades <trades.json|csv> --instrument ETH-USDT-SWAP --configs <m> [--family ...] [--out <dir>]

--ledger: the official evaluator's DEV ledger (nets recomputed in the conservative funding scenario of D094).
--trades: a plain trade list from an author's own engine (D199, indicative only): entry_at, exit_at (ISO UTC), side
  (long|short), notional_usdt (entry notional on the 100 USDT account), net_usdt (after all costs and funding),
  executed_notional_usdt (optional, default 2 x notional). The result is then labelled INDICATIVE.
--configs: m, the number of configurations the author's PREREG declared for the cycle (every configuration the candidate
  could have been chosen from, all instruments together). Default 24, the cycle cap.
--family: the trade lists (or DEV ledgers) of EVERY configuration the PREREG declared on this instrument, as files or
  one folder. With it the DEV timing check uses the family test below; without it, the single test with 0.05 / m.
--instruments: q, the number of instruments the declared configurations cover (default 3).

Timing test (drift control). Daily exposure e[d] = signed entry notional / 100 on every UTC day whose noon the trade is
open at; x[d] = 100 x (close-to-close return of day d - funding of day d). S(0) = sum e[d] x[d] is the strategy's
day-level gross result at constant notional; S(k) is the same exposure pattern moved k days later (circular inside the
period, every k from 1 to T - 1): the same number, length and side of trades at other dates. p = (1 + number of shifts
with S(k) >= S(0)) / T. Random timing with the same exposure - buy-and-hold included - gets p around 0.5; costs cancel
because every shift pays the same. Every shift is counted, the nearest ones too, so that under random placement
P(p <= a) <= a holds exactly, also for small thresholds.

Choosing the best of m tries (DEV only). Single test: p <= 0.05 / m (Bonferroni; valid for any dependence between the
configurations). Family test: every declared configuration on the instrument is moved by the same k; z_c(k) is S_c(k)
standardised over the shifts; p_family = (1 + number of shifts where the best z_c(k) of the family >= z of the submitted
configuration at k = 0) / T, and the threshold is 0.05 / q. It is exact for any rule of choosing the configuration and
does not punish a family of similar configurations for their number.

Reads only DEV: the store data/okx/<instrument>, cut at the last full DEV day 2026-10-01.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import numpy as np

HERE = Path(__file__).resolve()
ROOT = HERE.parents[3]
for _path in (str(ROOT), str(ROOT / "tools")):
    if _path not in sys.path:
        sys.path.insert(0, _path)
import team_data  # noqa: E402
import team_eval  # noqa: E402
import team_eval_v4  # noqa: E402

RULE = "K-T v1 (2026-11-17)"
CAPITAL = 100.0
DAY_MS, DAY_S = 86_400_000, 86_400
DEV_END_MS = team_eval.DEV_LAST_OPEN_MS + 60_000
STRESS = 0.0005  # +5 bps of every executed notional
MIN_PERIOD_DAYS = 28  # no timing test on a shorter period
THRESHOLDS = {"max_trades_for_track": 99, "min_median_hold_hours": 72,
              "min_trades": {"DEV": 15, "E2": 6, "E2_tail": 2},
              "family_alpha_dev": 0.05,  # divided by m (single test) or by q (family test)
              "p_shift_max": {"E2": 0.10, "E2_tail": 0.50},
              "worst_year_floor_usdt": -10.0, "e1_stress_floor_usdt": -5.0,
              "drop_best_trades": {"DEV": 2, "E2": 1}}
PERIODS = {"E1": ("2020-01-01", "2024-01-01"), "E2": ("2024-01-01", "2026-10-02"), "E2_tail": ("2025-07-01", "2026-10-02")}
E2_PARTS = (("2024H1", "2024-01-01", "2024-07-01"), ("2024H2", "2024-07-01", "2025-01-01"), ("2025H1", "2025-01-01", "2025-07-01"),
            ("2025H2", "2025-07-01", "2026-01-01"), ("2026H1", "2026-01-01", "2026-07-01"), ("2026H2 (to 10-02)", "2026-07-01", "2026-10-02"))


def seconds(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def daily_series(instrument, store_root=None, conservative_rate=0.0001):
    """Per UTC day of the DEV store: date, close at 24:00, return of the day, known funding sum, conservative gap charge."""
    store = Path(store_root) if store_root else Path("data/okx") / instrument
    catalog = team_data.read_catalog(store)
    if catalog.get("instrument") != instrument or catalog.get("problems"):
        raise SystemExit(f"store {store} is not a clean {instrument} store")
    stamps, values = team_eval_v4.read_arrays(store, catalog["segments"][0]["first_ms"], DEV_END_MS, columns=(5,))
    last = stamps % DAY_MS == DAY_MS - 60_000  # the 23:59 bar: its close is the price at 24:00 UTC
    day = (stamps[last] // DAY_MS).astype(np.int64)
    if np.any(np.diff(day) != 1):
        raise SystemExit("the DEV store has a missing day close")
    close = values[last, 0]
    ret = np.zeros(len(close))
    ret[1:] = close[1:] / close[:-1] - 1
    known, count = np.zeros(len(day)), np.zeros(len(day))
    index = {int(d): i for i, d in enumerate(day)}
    for stamp, rate in team_data.load_funding(store):
        if rate in (None, "") or stamp > DEV_END_MS:
            continue
        i = index.get((stamp - 1) // DAY_MS)  # a settlement at 24:00 belongs to the day it ends
        if i is not None:
            known[i] += float(rate)
            count[i] += 1
    gap = np.maximum(0, 3 - count) * conservative_rate  # D094: every missing 8 h settlement charged against the position
    return {"day": day, "date": day.astype("datetime64[D]"), "close": close, "ret": ret, "funding": known, "gap": gap,
            "store": str(store).replace("\\", "/"), "store_catalog_sha256": sha256_file(store / team_data.CATALOG)}


def ledger_rows(ledger, path):
    """Trades of a team_eval DEV ledger on one account, nets in the conservative funding scenario."""
    if ledger.get("data_set") != "DEV":
        raise SystemExit(f"{path}: gate K is judged on DEV ledgers only")
    policy = ledger.get("funding_policy") or {}
    rate = policy.get("conservative_rate", 0.0001)
    kept, dropped = team_eval.single_account(ledger["trades"])
    rows = [{"entry_s": seconds(t["entry_at"]), "exit_s": seconds(t["exit_at"]), "sign": 1 if t["side"] == "long" else -1,
             "notional": t["first_notional_usdt"], "executed": t["executed_notional_usdt"],
             "net": t["net_usdt"] - team_eval.funding_charge(t, policy, rate), "forced": bool(t.get("forced_close"))} for t in kept]
    return rows, rate, len(dropped)


def plain_rows(raw, path):
    """Trades of a plain list from an author's own engine; one account with one position."""
    rows = []
    for t in raw:
        notional = float(t["notional_usdt"])
        rows.append({"entry_s": seconds(t["entry_at"]), "exit_s": seconds(t["exit_at"]), "sign": 1 if str(t["side"]).lower() == "long" else -1,
                     "notional": notional, "executed": float(t.get("executed_notional_usdt") or 2 * notional),
                     "net": float(t.get("net_usdt") or 0.0), "forced": False})
    rows.sort(key=lambda r: r["entry_s"])
    for a, b in zip(rows, rows[1:]):
        if b["entry_s"] < a["exit_s"]:
            raise SystemExit(f"{path}: trades overlap, the gate is for one account with one position")
    return rows


def parse_file(path):
    """("ledger", dict) for a team_eval ledger, ("plain", list of trade dicts) otherwise."""
    path = Path(path)
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as handle:
            return "plain", list(csv.DictReader(handle))
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "data_set" in raw:
        return "ledger", raw
    return "plain", raw["trades"] if isinstance(raw, dict) else raw


def read_trades(args):
    """Trades as dicts with entry_s, exit_s, sign, notional, executed, net (verdict scenario) and the source label."""
    if args.ledger:
        kind, ledger = parse_file(args.ledger)
        if kind != "ledger":
            raise SystemExit("--ledger needs a team_eval ledger.json")
        rows, rate, dropped = ledger_rows(ledger, args.ledger)
        source = {"kind": "team_eval DEV ledger", "official": True, "file": str(args.ledger).replace("\\", "/"),
                  "ledger_sha256": sha256_file(args.ledger), "recipe_sha256": ledger.get("recipe_sha256"),
                  "execution_model_id": ledger.get("execution_model_id"), "evaluator_sha256": ledger.get("evaluator_sha256"),
                  "code_sha256": ledger.get("code_sha256"), "overlap_dropped_trades": dropped, "funding_scenario": "conservative (D094)"}
        return ledger["instrument"], rows, rate, source
    kind, raw = parse_file(args.trades)
    if kind != "plain":
        raise SystemExit("--trades needs a plain trade list; a team_eval ledger goes to --ledger")
    source = {"kind": "plain trade list from the author's own engine", "official": False, "file": str(args.trades).replace("\\", "/"),
              "file_sha256": sha256_file(args.trades), "funding_scenario": "as provided by the author"}
    return args.instrument, plain_rows(raw, args.trades), 0.0001, source


def read_family(paths, instrument):
    """Every declared configuration on the instrument: [{"file", "sha256", "trades", "rows"}]."""
    files = []
    for item in paths:
        item = Path(item)
        files += sorted(p for p in item.iterdir() if p.suffix.lower() in (".json", ".csv")) if item.is_dir() else [item]
    members = []
    for path in files:
        kind, raw = parse_file(path)
        if kind == "ledger":
            if raw.get("instrument") != instrument:
                raise SystemExit(f"{path}: the family test takes the configurations of {instrument} only")
            rows = ledger_rows(raw, path)[0]
        else:
            rows = plain_rows(raw, path)
        members.append({"file": str(path).replace("\\", "/"), "sha256": sha256_file(path), "trades": len(rows), "rows": rows})
    return members


def exposure_of(rows, series):
    """Signed entry notional / CAPITAL on every UTC day whose noon the trade is open at."""
    t_days = len(series["day"])
    noon0 = int(series["day"][0]) * DAY_S + DAY_S // 2
    exposure = np.zeros(t_days)
    for tr in rows:
        if not (tr["entry_s"] * 1000 < DEV_END_MS and tr["exit_s"] * 1000 <= DEV_END_MS + DAY_MS):
            raise SystemExit("a trade lies outside DEV: gate K reads DEV only")
        first = max(0, int(np.ceil((tr["entry_s"] - noon0) / DAY_S)))
        last = min(t_days - 1, int(np.ceil((tr["exit_s"] - noon0) / DAY_S)) - 1)
        if last >= first:
            exposure[first:last + 1] += tr["sign"] * tr["notional"] / CAPITAL
    return exposure


def shift_sums(exposure, x_lin, x_abs):
    """S(k) for every k: sum over days of exposure[d] x x_lin[d + k] + |exposure[d]| x x_abs[d + k], days wrapping around.

    x_lin: CAPITAL x (return - known funding) per day; x_abs: -CAPITAL x the conservative charge for funding the history
    does not have (paid whatever the side). A circular cross-correlation, computed by FFT. The notional is held constant,
    so that one giant trend (SOL 2021) decides neither for nor against a strategy through compounding."""
    t = len(exposure)
    return (np.fft.irfft(np.conj(np.fft.rfft(exposure)) * np.fft.rfft(x_lin), n=t)
            + np.fft.irfft(np.conj(np.fft.rfft(np.abs(exposure))) * np.fft.rfft(x_abs), n=t))


def shift_test(exposure, x_lin, x_abs):
    """S(0) and its one-sided p-value against the same exposure pattern at shifted dates."""
    t = len(exposure)
    if t < MIN_PERIOD_DAYS or not np.any(exposure):
        return {"days": t, "p": None}
    s = shift_sums(exposure, x_lin, x_abs)
    better = int(np.sum(s[1:] >= s[0] - 1e-9))  # every non-zero shift: the p-value is exact under random placement
    return {"days": t, "shifts": t - 1, "s0_usdt": round(float(s[0]), 6), "shifts_at_least_as_good": better,
            "p": round((1 + better) / t, 8), "shifted_median_usdt": round(float(np.median(s[1:])), 6),
            "shifted_mean_usdt": round(float(np.mean(s[1:])), 6), "days_in_market_share": round(float(np.mean(exposure != 0)), 4),
            "mean_exposure": round(float(exposure.mean()), 4)}


def family_test(own, members, x_lin, x_abs):
    """p of the submitted configuration against the best member of its family at the same shifted dates.

    own: exposure of the submitted configuration; members: exposures of every declared configuration on the instrument.
    z_c(k) = (S_c(k) - mean over k) / sd over k; best(k) = max over the family, the submitted configuration included.
    p = (1 + number of k >= 1 with best(k) >= z_own(0)) / T. Because best(0) >= z_own(0) whatever the rule of choice,
    the p-value is valid for any selection among the members."""
    def standardised(exposure):
        if not np.any(exposure):
            return None
        s = shift_sums(exposure, x_lin, x_abs)
        sd = float(s.std())
        return (s - s.mean()) / sd if sd > 0 else None

    z_own = standardised(own)
    if z_own is None or len(own) < MIN_PERIOD_DAYS:
        return {"members": len(members), "p": None}
    rows = [z for z in (standardised(e) for e in members) if z is not None]
    best = np.max(np.vstack(rows + [z_own]), axis=0)
    better = int(np.sum(best[1:] >= z_own[0] - 1e-9))
    return {"members": len(members), "members_with_trades": len(rows), "z_own": round(float(z_own[0]), 4),
            "best_member_z_at_true_dates": round(float(best[0]), 4), "best_member_z_shifted_q95": round(float(np.quantile(best[1:], 0.95)), 4),
            "shifts_where_best_member_at_least_as_good": better, "p": round((1 + better) / len(own), 8)}


def evaluate(instrument, trades, series, configs=24, instruments=3, family=None):
    date, t_days = series["date"], len(series["day"])
    exposure = exposure_of(trades, series)
    x_lin = CAPITAL * (series["ret"] - series["funding"])
    x_abs = -CAPITAL * series["gap"]
    entry_date = np.array([np.datetime64(int(tr["entry_s"] // DAY_S), "D") for tr in trades], dtype="datetime64[D]")
    net = np.array([tr["net"] for tr in trades])
    stress = net - np.array([tr["executed"] for tr in trades]) * STRESS
    hold_hours = np.array([(tr["exit_s"] - tr["entry_s"]) / 3600 for tr in trades])

    def window(name):
        start, end = (np.datetime64(v) for v in PERIODS[name])
        return (date >= start) & (date < end), (entry_date >= start) & (entry_date < end)

    periods = {"DEV": (np.ones(t_days, bool), np.ones(len(trades), bool))} | {name: window(name) for name in PERIODS}
    stats = {}
    for name, (days_in, trades_in) in periods.items():
        row = {"closed_trades": int(trades_in.sum()), "net_usdt": round(float(net[trades_in].sum()), 6),
               "stress_net_usdt": round(float(stress[trades_in].sum()), 6)}
        if name != "E1":
            row["timing"] = shift_test(exposure[days_in], x_lin[days_in], x_abs[days_in])
        stats[name] = row
    years = entry_date.astype("datetime64[Y]")
    by_year = {str(y): round(float(net[years == y].sum()), 6) for y in np.unique(years)}
    parts = {label: round(float(net[(entry_date >= np.datetime64(a)) & (entry_date < np.datetime64(b))].sum()), 6) for label, a, b in E2_PARTS}
    in_e2 = periods["E2"][1]
    ordered, ordered_e2 = np.sort(net)[::-1], np.sort(net[in_e2])[::-1]
    th = THRESHOLDS
    drop_dev = float(net.sum() - ordered[:th["drop_best_trades"]["DEV"]].sum()) if len(net) else 0.0
    drop_e2 = float(net[in_e2].sum() - ordered_e2[:th["drop_best_trades"]["E2"]].sum()) if in_e2.any() else 0.0
    gross_profit = float(net[net > 0].sum())
    median_hold = float(np.median(hold_hours)) if len(trades) else 0.0
    eligible = len(trades) <= th["max_trades_for_track"] and median_hold >= th["min_median_hold_hours"]

    def p_ok(name):
        p = stats[name]["timing"]["p"]
        return p is not None and p <= th["p_shift_max"][name]

    p_dev = stats["DEV"]["timing"]["p"]
    if family is not None:  # the family test: every declared configuration on the instrument at the same shifted dates
        fam = family_test(exposure, [exposure_of(m["rows"], series) for m in family], x_lin, x_abs)
        limit = th["family_alpha_dev"] / instruments
        dev_timing = {"method": "family", "threshold": round(limit, 8), "instruments": instruments, **fam,
                      "files": [{k: m[k] for k in ("file", "sha256", "trades")} for m in family]}
        t4 = ("T4 DEV timing, family test", fam["p"], fam["p"] is not None and fam["p"] <= limit,
              f"the best of the {fam['members']} supplied configurations at shifted dates is at least as good in <= "
              f"{th['family_alpha_dev']} / {instruments} instruments = {limit:.5f} of the shifts")
    else:  # the single test with Bonferroni over the declared configurations
        limit = th["family_alpha_dev"] / configs
        dev_timing = {"method": "single", "threshold": round(limit, 8), "configs": configs, "p": p_dev}
        t4 = ("T4 DEV timing, single test", p_dev, p_dev is not None and p_dev <= limit,
              f"p <= {th['family_alpha_dev']} / {configs} declared configurations = {limit:.5f} against the same trades at shifted dates")

    checks = [
        ("T1 enough trades", {k: stats[k]["closed_trades"] for k in th["min_trades"]},
         all(stats[k]["closed_trades"] >= v for k, v in th["min_trades"].items()), f"closed trades >= {th['min_trades']}"),
        ("T2 DEV net and stress", [stats["DEV"]["net_usdt"], stats["DEV"]["stress_net_usdt"]],
         stats["DEV"]["net_usdt"] > 0 and stats["DEV"]["stress_net_usdt"] > 0, "net > 0 and net with +5 bps per execution > 0"),
        ("T3 worst calendar year", min(by_year.values()) if by_year else 0.0,
         (min(by_year.values()) if by_year else 0.0) >= th["worst_year_floor_usdt"], f">= {th['worst_year_floor_usdt']} USDT in every calendar year (by entry)"),
        ("T3 E1 stress floor", stats["E1"]["stress_net_usdt"], stats["E1"]["stress_net_usdt"] >= th["e1_stress_floor_usdt"],
         f"E1 stress >= {th['e1_stress_floor_usdt']} USDT"),
        t4,
        ("T5 E2 net and stress", [stats["E2"]["net_usdt"], stats["E2"]["stress_net_usdt"]],
         stats["E2"]["net_usdt"] > 0 and stats["E2"]["stress_net_usdt"] > 0, "E2 net > 0 and stress > 0"),
        ("T5 E2 timing", stats["E2"]["timing"]["p"], p_ok("E2"), f"p <= {th['p_shift_max']['E2']} inside E2"),
        ("T5 E2 without its best half-year", round(stats["E2"]["net_usdt"] - max(parts.values()), 6),
         stats["E2"]["net_usdt"] - max(parts.values()) >= 0, "E2 net minus the best of its six half-year parts >= 0"),
        ("T6 tail net", stats["E2_tail"]["net_usdt"], stats["E2_tail"]["net_usdt"] > 0, "tail 2025-07-01..2026-10-02: net > 0 at base costs"),
        ("T6 tail timing", stats["E2_tail"]["timing"]["p"], p_ok("E2_tail"), f"p <= {th['p_shift_max']['E2_tail']} inside the tail"),
        ("T7 DEV without its 2 best trades", round(drop_dev, 6), drop_dev >= 0, "DEV net minus the 2 best trades >= 0"),
        ("T7 E2 without its best trade", round(drop_e2, 6), drop_e2 >= 0, "E2 net minus the best trade >= 0"),
    ]
    rows = [{"check": name, "value": value, "rule": rule, "status": "PASS" if ok else "FAIL"} for name, value, ok, rule in checks]
    verdict = "NOT_ELIGIBLE" if not eligible else "PASS" if all(r["status"] == "PASS" for r in rows) else "FAIL"
    return {"eligibility": {"closed_trades_dev": len(trades), "median_hold_hours": round(median_hold, 2), "eligible": eligible,
                            "rule": f"fewer than 100 closed trades on DEV and median hold >= {th['min_median_hold_hours']} h; otherwise the regular gate K applies"},
            "declared_configurations": configs, "dev_timing": dev_timing,
            "verdict": verdict, "checks": rows, "periods": stats, "net_by_year_usdt": by_year, "e2_parts_net_usdt": parts,
            "info": {"instrument": instrument, "dev_days": t_days, "first_day": str(date[0]), "last_day": str(date[-1]),
                     "trades_per_year": round(len(trades) / (t_days / 365.25), 2), "forced_closes": sum(tr["forced"] for tr in trades),
                     "largest_trade_share_of_gross_profit": round(float(net.max() / gross_profit), 4) if gross_profit > 0 else None,
                     "day_level_gross_minus_ledger_net_usdt": round(stats["DEV"]["timing"].get("s0_usdt", 0.0) - float(net.sum()), 6)
                     if stats["DEV"]["timing"].get("s0_usdt") is not None else None}}


def markdown(result):
    timing = result["dev_timing"]
    how = (f"family test over {timing['members']} supplied configurations, threshold {timing['threshold']}" if timing["method"] == "family"
           else f"single test, {timing['configs']} declared configurations, threshold {timing['threshold']}")
    lines = [f"# Gate K, long-horizon track ({RULE}) · {result['source']['kind']}",
             "", f"- instrument {result['info']['instrument']}, DEV days {result['info']['dev_days']} ({result['info']['first_day']} .. {result['info']['last_day']})",
             f"- {'OFFICIAL evaluator ledger' if result['source']['official'] else 'INDICATIVE: author engine, not the official evaluator (D199)'}",
             f"- DEV timing: {how}",
             f"- eligibility: {result['eligibility']}", f"- **verdict: {result['verdict']}**", "",
             "| check | value | rule | status |", "|---|---|---|---|"]
    lines += [f"| {r['check']} | {r['value']} | {r['rule']} | {r['status']} |" for r in result["checks"]]
    lines += ["", "| period | trades | net USDT | stress USDT | days | in market | S(0) | median shifted | mean shifted | p |", "|---|---|---|---|---|---|---|---|---|---|"]
    for name, row in result["periods"].items():
        t = row.get("timing") or {}
        lines.append(f"| {name} | {row['closed_trades']} | {row['net_usdt']} | {row['stress_net_usdt']} | {t.get('days', '')} | "
                     f"{t.get('days_in_market_share', '')} | {t.get('s0_usdt', '')} | {t.get('shifted_median_usdt', '')} | "
                     f"{t.get('shifted_mean_usdt', '')} | {t.get('p', '')} |")
    if timing["method"] == "family":
        lines += ["", f"Family test: z of the submitted configuration {timing.get('z_own')}; the best member at the true dates "
                      f"{timing.get('best_member_z_at_true_dates')}; 95 % of the shifted dates give the best member below "
                      f"{timing.get('best_member_z_shifted_q95')}; p = {timing.get('p')}."]
    lines += ["", "Gate K is a necessary filter on DEV, not proof of an edge: the proof is gate D on data after the freeze."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ledger", type=Path, help="team_eval DEV ledger.json")
    group.add_argument("--trades", type=Path, help="plain trade list (json or csv) from an author's own engine: indicative")
    parser.add_argument("--instrument", choices=("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"))
    parser.add_argument("--store", type=Path, help="DEV store of the instrument (default data/okx/<instrument>)")
    parser.add_argument("--configs", type=int, default=24,
                        help="m: configurations declared in the author's PREREG for the cycle (default 24, the cycle cap)")
    parser.add_argument("--family", type=Path, nargs="+",
                        help="trade lists or DEV ledgers of every declared configuration on this instrument (files or one folder)")
    parser.add_argument("--instruments", type=int, default=3, help="q: instruments covered by the declared configurations (default 3)")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if args.trades and not args.instrument:
        parser.error("--trades needs --instrument")
    if args.configs < 1 or args.instruments < 1:
        parser.error("--configs and --instruments must be at least 1")
    instrument, trades, rate, source = read_trades(args)
    series = daily_series(instrument, args.store, rate)
    family = read_family(args.family, instrument) if args.family else None
    result = {"schema": "judge-gate-k-long/v1", "rule": RULE, "thresholds": THRESHOLDS, "source": source,
              "dev_store": series["store"], "dev_store_catalog_sha256": series["store_catalog_sha256"],
              "tool_sha256": sha256_file(HERE), **evaluate(instrument, trades, series, args.configs, args.instruments, family)}
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "gate_k_long.json").write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        (args.out / "gate_k_long.md").write_text(markdown(result), encoding="utf-8")
    timing = result["dev_timing"]
    print(json.dumps({"verdict": result["verdict"], "official": source["official"], "eligible": result["eligibility"]["eligible"],
                      "trades": result["eligibility"]["closed_trades_dev"],
                      "dev_timing": {k: timing.get(k) for k in ("method", "p", "threshold", "members")},
                      "failed": [r["check"] for r in result["checks"] if r["status"] == "FAIL"],
                      "p": {k: v["timing"]["p"] for k, v in result["periods"].items() if "timing" in v}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
