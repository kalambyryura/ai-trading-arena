"""OKX's answer to a cancel of a client ID that never became an order, on a Demo account only (OWNER_ACTIONS 21).

R2.1.8 reads a cancel answer as "nothing to cancel" in several shapes (exchange_api.NOTHING_TO_CANCEL_CODES): one row with
our client ID or an empty one, or a top-level code with no rows. Which shape OKX really sends has only been modelled.
This tool sends ONE cancel POST of a fresh random client ID that no order ever had, on an OKX Demo profile, and prints
OKX's raw answer (code, msg, data) and how R2.1.8 classifies it. No order can be created or cancelled by it.

  py -3.12 -X utf8 -B tools/check_cancel_answer.py --profile <Demo profile id> [--inst ETH-USDT-SWAP]

Run it only as the owner (or with the owner's permission): it signs with the Demo profile's key. It refuses a Live
profile. Requests: the server time GET and the one cancel POST; anything else is blocked. The printed answer carries no
key, signature or header; send it to the platform, which fixes the shape in a test.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trading_lab.exchange_accounts import load_profile, profiles  # noqa: E402
from trading_lab.exchange_api import CancelFailed, CancelRefused, ExchangeAPI, send  # noqa: E402
from trading_lab.okx_readonly import PreflightError  # noqa: E402

ALLOWED = {("GET", "/api/v5/public/time"), ("POST", "/api/v5/trade/cancel-order")}


def check(root, profile_id, instrument="ETH-USDT-SWAP", *, transport=send, client_id=None):
    root = Path(root)
    profile = next((p for p in profiles(root) if p["id"] == profile_id), None)
    if profile is None:  # R2.1.9: a clear message, not a StopIteration
        raise SystemExit(f"Немає профілю «{profile_id}». Є: " + ", ".join(p["id"] for p in profiles(root)) + ".")
    if profile.get("environment") != "demo":
        raise SystemExit("Лише Demo-профіль: цей інструмент не надсилає нічого на Live-рахунок.")
    seen = {"requests": [], "answer": None}

    def one_cancel(request):
        route = (request.get_method(), urlsplit(request.full_url).path)
        if route not in ALLOWED or (route[1] == "/api/v5/trade/cancel-order" and seen["answer"] is not None):
            raise PreflightError("cancel_check_request_blocked")
        seen["requests"].append(route[0] + " " + route[1])
        payload = transport(request)
        if route[1] == "/api/v5/trade/cancel-order":
            seen["answer"] = payload  # OKX's response body only: no header, key or signature
        return payload

    api = ExchangeAPI(*load_profile(root, profile), transport=one_cancel)
    if api.environment != "demo":
        raise SystemExit("Лише Demo-профіль.")
    api.inst_id = instrument
    api.writes = True  # for this one cancel only: no order body can pass validate_write here
    # the platform's own client ID format (exchange_api.CLIENT_ID) with a random run part no season ever had
    client_id = client_id or "J5" + secrets.token_hex(8) + "5E9999"
    classified = "acknowledged (unexpected: a live order with this client ID?)"
    try:
        api.cancel(client_id)
    except CancelRefused as error:
        classified = "nothing to cancel (" + error.code + ")"
    except CancelFailed as error:
        classified = "another code: " + error.code
    except PreflightError as error:
        classified = "unknown: " + str(error)
    return {"schema": "okx-cancel-answer-check/v1", "profile": profile_id, "environment": "OKX_DEMO", "instrument": instrument,
            "client_order_id": client_id, "checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "raw_answer": seen["answer"], "r218_reads_it_as": classified, "requests": seen["requests"], "orders_sent": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--inst", default="ETH-USDT-SWAP")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    print(json.dumps(check(args.root, args.profile, args.inst), ensure_ascii=False, indent=1))
