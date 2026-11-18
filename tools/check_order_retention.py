"""How long OKX keeps a cancelled order with no fill (R2.1.6, F3 of the R2.1.5 review). GET only, a few requests.

The settlement of R2.1.5 and earlier re-read every own order; OKX keeps a cancelled unfilled order about 2 hours ("The
incomplete orders that have been canceled are only reserved for 2 hours", docs-v5 Order history), so the GET then
answered 51603 and the round never reached DONE. R2.1.6 no longer re-reads them. This tool confirms the retention on
an account: it takes own post-only entries of a finished run that were cancelled unfilled more than --older hours ago
(from the local journal, read only) and GETs each by its client ID.

  py -3.12 -X utf8 -B tools/check_order_retention.py --profile <exchange profile id> --run <run id> [--older 2.5]

Run it only with the lead's permission: it reads the profile's key file to sign the GETs. No POST, order, setting, Jev
or journal write; at most --max orders (3) and 8 requests.
"""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trading_lab.exchange_accounts import load_profile, profiles  # noqa: E402
from trading_lab.exchange_api import ExchangeAPI, GET_PATHS, OrderAbsent, send  # noqa: E402
from trading_lab.match_journal import verified_rows  # noqa: E402
from trading_lab.okx_readonly import PreflightError  # noqa: E402


def candidates(journal, older_seconds, now):
    """(client ID, instrument, cancelled at) of own entries whose last journalled facts are cancelled with no fill."""
    intents, last = {}, {}
    for row in verified_rows(journal):
        event = row["event"]
        data = event.get("data") or {}
        if event.get("code") == "exchange_order_intent" and isinstance(data.get("request"), dict):
            request = data["request"]
            if request.get("reduceOnly") is False:
                intents[request["clOrdId"]] = request["instId"]
        result = data.get("result")
        if (event.get("code") in {"exchange_entry_fill", "exchange_order_read"}  # R2.1.10: a no-fill read has its own code
                and isinstance(result, dict) and result.get("clOrdId") in intents):
            last[result["clOrdId"]] = (result, event.get("at"))
    out = []
    for client, (result, at) in last.items():
        stamp = datetime.fromisoformat(at).timestamp() if at else None
        if (result.get("state") == "canceled" and Decimal(result.get("contracts") or "0") == 0 and stamp is not None
                and now - stamp >= older_seconds):
            out.append((client, intents[client], stamp))
    return sorted(out, key=lambda item: item[2])


def check(root, profile_id, run, older_hours=2.5, maximum=3, *, transport=send, now=None):
    root = Path(root)
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    journal = root / "runtime" / "exchange_v1" / (run + ".events.jsonl")
    found = candidates(journal, older_hours * 3600, now)[:maximum]
    counts = {"get": 0, "blocked": 0}

    def get_only(request):
        if request.get_method() != "GET" or request.data is not None or urlsplit(request.full_url).path not in GET_PATHS:
            counts["blocked"] += 1
            raise PreflightError("read_only_transport_rejected")
        counts["get"] += 1
        if counts["get"] > 8:
            raise PreflightError("read_only_request_cap")
        return transport(request)

    profile = next((p for p in profiles(root) if p["id"] == profile_id), None)
    if profile is None:  # R2.1.9: a clear message, not a StopIteration
        raise SystemExit(f"Немає профілю «{profile_id}». Є: " + ", ".join(p["id"] for p in profiles(root)) + ".")
    api = ExchangeAPI(*load_profile(root, profile), transport=get_only)
    api.request_limit = 8
    rows = []
    for client, instrument, cancelled_at in found:
        age = round((now - cancelled_at) / 3600, 2)
        try:
            data = api.get("/api/v5/trade/order", {"instId": instrument, "clOrdId": client}, private=True)
            row = data[0] if data and isinstance(data[0], dict) else {}
            rows.append({"client_order_id": client, "hours_since_cancel": age, "answer": "present",
                         "state": row.get("state"), "filled": row.get("accFillSz")})
        except OrderAbsent:
            rows.append({"client_order_id": client, "hours_since_cancel": age, "answer": "51603 Order does not exist"})
        except PreflightError as error:  # the request cap (a --max of 8 or more) or another refusal: said, not raised
            rows.append({"client_order_id": client, "hours_since_cancel": age, "answer": "not read: " + str(error)})
            if str(error) in {"read_only_request_cap", "exchange_request_limit"}:
                break
    return {"schema": "okx-order-retention-check/v1", "profile": profile_id, "run": run, "checked_at_utc":
            datetime.fromtimestamp(now, timezone.utc).isoformat(), "candidates": len(found), "orders": rows,
            "requests": counts, "writes": 0, "paid_calls": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--older", type=float, default=2.5, help="hours since the cancel (default 2.5)")
    parser.add_argument("--max", type=int, default=3)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    print(json.dumps(check(args.root, args.profile, args.run, args.older, args.max), ensure_ascii=False, indent=1))
