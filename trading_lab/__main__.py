import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
import time

from .jev import BudgetLedger, JevClient
from .risk import RiskPolicy, Snapshot
from .public_market import fetch_snapshot
from .okx_readonly import OkxReader, PreflightError, credentials_from, load_settings, preflight
from .jev_review import review_public_snapshot

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description="Trading research; no exchange execution commands.")
    parser.add_argument("command", choices=["offline-demo", "jev-smoke", "budget-status", "public-snapshot", "okx-preflight", "jev-okx-review"])
    parser.add_argument("--paid-once", action="store_true", help="Enable the selected registered one-attempt Jev experiment.")
    parser.add_argument("--product", choices=["swap", "spot"], default="swap", help="BTC/USDT product for read-only OKX checks.")
    parser.add_argument("--account", action="store_true", help="Read OKX account; Read-only key recommended, other permissions never used.")
    args = parser.parse_args()
    if args.command == "jev-okx-review":
        if not args.paid_once or args.account or args.product != "swap":
            parser.error("jev-okx-review requires --paid-once, public-only data and product swap")
        try:
            site, environment, _ = load_settings(ROOT)
            result = review_public_snapshot(ROOT, OkxReader(site, environment))
        except (PreflightError, OSError, ValueError):
            result = {"status": "blocked", "reason": "local_review_error_check_persistent_ledger",
                      "live_execution_enabled": False, "orders_submitted": 0}
        runtime = ROOT / "runtime"
        runtime.mkdir(exist_ok=True)
        receipt = runtime / ("jev_okx_review_" + str(time.time_ns()) + ".json")
        receipt.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps({**result, "receipt_path": str(receipt)}, indent=2))
        return 0 if result.get("status") == "review_completed" else 1
    if args.command == "okx-preflight":
        try:
            site, environment, values = load_settings(ROOT)
            credentials = credentials_from(values) if args.account else None
            result = preflight(OkxReader(site, environment, credentials), args.product, args.account)
        except (PreflightError, OSError, UnicodeError):
            result = {"status": "blocked", "reason": "missing_or_invalid_local_okx_settings",
                      "orders_submitted": 0, "live_execution_enabled": False, "requests_attempted": 0}
        runtime = ROOT / "runtime"
        runtime.mkdir(exist_ok=True)
        receipt = runtime / ("okx_preflight_" + str(time.time_ns()) + ".json")
        receipt.write_text(json.dumps(result, indent=2), encoding="utf-8")
        # Private account balances stay in the ignored local receipt, not terminal output.
        public_output = {key: value for key, value in result.items() if key != "account"}
        if "account" in result:
            public_output["unused_key_permissions"] = result["account"]["unused_permissions"]
            public_output["account_mode_setup_needed_for_swap"] = result["account"]["account_mode_setup_needed_for_swap"]
            public_output["account_checks"] = result["account"]["checks"]
        public_output["receipt_path"] = str(receipt)
        print(json.dumps(public_output, indent=2))
        return 0 if result.get("status") in {"public_checks_passed", "read_only_checks_passed"} else 1
    if args.command == "public-snapshot":
        result = fetch_snapshot()
        runtime = ROOT / "runtime"
        runtime.mkdir(exist_ok=True)
        (runtime / ("okx_public_" + str(time.time_ns()) + ".json")).write_text(
            json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
        return 0 if result.get("status") == "received" else 1
    if args.command == "budget-status":
        print(json.dumps(BudgetLedger(ROOT / "runtime" / "jev_ledger.sqlite3").summary(), indent=2))
        return
    now = time.time()
    sample = Snapshot(observed_at=now, revision="synthetic-smoke-v1")
    if args.command == "offline-demo":
        policy = RiskPolicy()
        cases = {"eligible_paper_candidate": sample,
                 "stale_data": replace(sample, observed_at=now - 60),
                 "live_mode": replace(sample, mode="live"),
                 "oversized_candidate": replace(sample, notional_usd=1000),
                 "daily_loss_cap": replace(sample, daily_loss_usd=5)}
        print(json.dumps({"synthetic": True, "network_requests": 0,
                          "live_execution_enabled": False, "cases": {
                              name: policy.check(state, now) or "eligible_for_paper_review"
                              for name, state in cases.items()}}, indent=2))
        return
    if not args.paid_once:
        parser.error("jev-smoke requires --paid-once; it can consume the existing OpenRouter balance")
    result = JevClient(ROOT).decide(sample, lambda: sample)
    output = {"synthetic_input": True, "connectivity_test_only": True, **result.as_dict()}
    runtime = ROOT / "runtime"
    runtime.mkdir(exist_ok=True)
    # Unique result path preserves the first receipt if someone repeats the blocked command.
    (runtime / ("jev_smoke_" + str(time.time_ns()) + ".json")).write_text(
        json.dumps(output, indent=2), encoding="utf-8")
    print(json.dumps(output, indent=2))
    return 0 if result.source == "Jev" else 1


if __name__ == "__main__":
    sys.exit(main())
