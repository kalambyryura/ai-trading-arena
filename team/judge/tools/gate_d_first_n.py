"""Judge: the exact manual "first N" recipe of gate D (GATE_D_PROCEDURE_UA.md v1.1, D127 p. 4) on a team_eval ledger.

Until tools/team_eval.py has --declared-n, the Judge computes the gate-D verdict of a submission like this:
  1. take the ledger of the official run on the gate-D sample store (SEALED-1 + FORWARD);
  2. drop trades whose exit is forced_close_end_of_period or data_gap_exit (not closed trades);
  3. sort the rest by entry_at (one account, one position: entry order is exit order) and take the first N;
  4. D2 on the prefix only: net (conservative funding scenario of the ledger, D094) > 0 and stress (net minus 5 bps of every
     executed notional) > 0; D4 on the prefix only: largest trade <= 30 % of gross profit and the account drawdown with the
     open position, marked by the engine at every quote (5 s, liquidation price, exit fee included: stricter than the
     charter's minute close), up to the exit of the N-th trade <= 10 USDT;
  5. fewer than N closed trades -> INSUFFICIENT; otherwise FAIL if any check fails, else PASS_PENDING_LIVE (D5 outside).
The output carries the sha256 of the ordered prefix (canonical JSON), the sha256 of the ledger file and of its registry
line, and the exact command of the run (passed in), so that every VERDICTS.jsonl row is reproducible byte for byte.

--window K (D203, default 1): the K-th window of N closed trades of the same ledger - trades (K-1)N+1 .. KN in the same
order, no overlap with the earlier windows. D2 and D4 are computed on these N trades only; the account drawdown is
measured from the equity before the window's first entry, as for an account that starts there. The window exists only
when every earlier window is complete; until its N-th trade is closed the result is INSUFFICIENT.

  py -3.12 -X utf8 -B team/judge/tools/gate_d_first_n.py --ledger <out>/ledger.json --n 30 --registry <registry.jsonl>
      --command "<exact team_eval command>" [--window 2] [--out <json>]
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve()
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT / "tools"))
import team_eval  # noqa: E402

NOT_CLOSED = ("forced_close_end_of_period", "data_gap_exit")


def canonical_sha256(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def registry_line(registry, ledger_sha256):
    """The registry line (exact bytes) whose ledger_sha256 is the ledger's, or None."""
    if not registry or not Path(registry).is_file():
        return None
    for raw in Path(registry).read_bytes().splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw.decode("utf-8"))
        except ValueError:
            continue
        if row.get("ledger_sha256") == ledger_sha256:
            return {"sha256": hashlib.sha256(raw).hexdigest(), "created_at": row.get("created_at"), "label": row.get("label"),
                    "verdict": row.get("verdict"), "out": row.get("out")}
    return None


def first_n(ledger, n, window=1):
    """Verdict of the window-th N closed trades of a ledger (window 1: the first N), with every number the row needs."""
    policy = ledger.get("funding_policy")
    rate = (policy or {}).get("conservative_rate", 0.0)
    trades = sorted(ledger["trades"], key=lambda t: (t["entry_at"], t["exit_at"]))
    not_closed = [t for t in trades if t["exit_reason"] in NOT_CLOSED]
    closed = [t for t in trades if t["exit_reason"] not in NOT_CLOSED]
    prefix = closed[(window - 1) * n:window * n]
    charges = [team_eval.funding_charge(t, policy, rate) for t in prefix]
    nets = [t["net_usdt"] - c for t, c in zip(prefix, charges)]
    stress_bps = team_eval.CHARTER["costs"]["stress_extra_bps_per_execution"]
    stressed = [v - t["executed_notional_usdt"] * stress_bps / 10000 for t, v in zip(prefix, nets)]
    d = team_eval.CHARTER["D"]
    net, stress = sum(nets), sum(stressed)
    gross = sum(v for v in nets if v > 0)
    best_share = (max(nets) / gross) if gross > 0 else None
    by_exit = sorted(zip(prefix, nets), key=lambda x: (x[0]["exit_at"], x[0]["entry_at"]))
    drawdown = team_eval.account_drawdown([t for t, _ in by_exit], [v for _, v in by_exit]) if prefix else 0.0
    drawdown_limit = d["max_drawdown_pct_of_capital"] / 100 * team_eval.CAPITAL
    checks = {"closed_trades_reached_n": len(prefix) >= n,
              "net_positive": net > 0, "stress_net_positive": stress > 0,
              "best_trade_share": best_share is not None and best_share <= d["max_best_trade_share_of_gross_profit"],
              "account_drawdown": drawdown is not None and drawdown <= drawdown_limit}
    if len(prefix) < n:
        verdict = "INSUFFICIENT"
    elif all(checks.values()):
        verdict = "PASS_PENDING_LIVE"
    else:
        verdict = "FAIL"
    return {"declared_n": n, "window": window, "window_trades": [(window - 1) * n + 1, window * n],
            "closed_trades_eligible": len(closed), "closed_trades_in_prefix": len(prefix),
            "not_closed_excluded": {"count": len(not_closed), "reasons": sorted({t["exit_reason"] for t in not_closed})},
            "prefix_first_entry_at": prefix[0]["entry_at"] if prefix else None, "prefix_last_exit_at": prefix[-1]["exit_at"] if prefix else None,
            "prefix_trades_sha256": canonical_sha256(prefix),
            "funding_scenario": "conservative", "gap_funding_rate": rate, "gap_funding_charged_usdt": round(sum(charges), 6),
            "net_usdt": round(net, 6), "stress_net_usdt": round(stress, 6), "gross_profit_usdt": round(gross, 6),
            "best_trade_usdt": round(max(nets), 6) if nets else None,
            "best_trade_share_of_gross_profit": round(best_share, 6) if best_share is not None else None,
            "account_drawdown_usdt": round(drawdown, 6) if drawdown is not None else None,
            "account_drawdown_marking": "engine marks at every 5 s quote at the liquidation price with the exit fee (stricter than minute close)",
            "thresholds": {"net": "> 0", "stress": "> 0", "best_trade_share": f"<= {d['max_best_trade_share_of_gross_profit']}",
                           "account_drawdown_usdt": f"<= {drawdown_limit}"},
            "checks": checks, "verdict_first_n": verdict}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--n", type=int, required=True)
    parser.add_argument("--window", type=int, default=1, help="K-th window of N closed trades (D203); default 1, the first N")
    parser.add_argument("--registry", type=Path, help="registry.jsonl of the run (its line for this ledger is hashed)")
    parser.add_argument("--command", default="", help="the exact team_eval command of the run, echoed into the output")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if args.n < team_eval.CHARTER["D"]["min_closed_trades"]:
        raise SystemExit(f"declared N must be >= {team_eval.CHARTER['D']['min_closed_trades']}")
    if args.window < 1:
        raise SystemExit("--window must be at least 1")
    raw = args.ledger.read_bytes()
    ledger = json.loads(raw.decode("utf-8"))
    if ledger.get("data_set") == "DEV":
        raise SystemExit("gate D is judged on SEALED/FORWARD ledgers only")
    ledger_sha256 = hashlib.sha256(raw).hexdigest()
    result = {"schema": "judge-gate-d-first-n/v1", "procedure": "GATE_D_PROCEDURE_UA.md v1.1 (D127 p. 4)",
              "ledger": str(args.ledger).replace("\\", "/"), "ledger_sha256": ledger_sha256,
              "registry_line": registry_line(args.registry, ledger_sha256), "command": args.command,
              "submission_tuple_seen_in_ledger": {"recipe_sha256": ledger["recipe_sha256"], "execution_model_id": ledger.get("execution_model_id"),
                                                  "evaluator_sha256": ledger["evaluator_sha256"], "candle_engine_sha256": ledger.get("candle_engine_sha256"),
                                                  "code_sha256": ledger["code_sha256"]},
              "data_set": ledger["data_set"], "store": ledger["store"], "store_catalog_sha256": ledger["store_catalog_sha256"],
              "created_at_run": ledger["created_at"], **first_n(ledger, args.n, args.window)}
    text = json.dumps(result, ensure_ascii=False, indent=1) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("verdict_first_n", "window", "closed_trades_eligible", "closed_trades_in_prefix", "net_usdt", "stress_net_usdt",
                                              "account_drawdown_usdt", "best_trade_share_of_gross_profit", "prefix_trades_sha256")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
