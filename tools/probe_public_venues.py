"""Explicit, one-shot public connectivity checks. No keys, retries or trading."""

import argparse
import json
import math
from pathlib import Path
import time
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


TARGETS = {
    "kraken-demo": ("https://demo-futures.kraken.com/derivatives/api/v3/tickers", None),
    "hyperliquid": ("https://api.hyperliquid.xyz/info", {"type": "l2Book", "coin": "BTC"}),
}


def probe(name):
    url, payload = TARGETS[name]
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    result = {"venue": name, "endpoint": url, "checked_at_unix": time.time(),
              "authenticated": False, "orders_submitted": 0, "calls": 1}
    started = time.perf_counter()
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=12) as response:
            raw = response.read(1048577)
        received_at = time.time()
        if len(raw) > 1048576:
            raise ValueError("Response too large")
        data = json.loads(raw)
        if name == "kraken-demo":
            valid = isinstance(data, dict) and data.get("result") == "success" and isinstance(data.get("tickers"), list)
        else:
            valid = isinstance(data, dict) and data.get("coin") == "BTC" and isinstance(data.get("levels"), list)
        result.update(status="public_response" if valid else "invalid_response",
                      response_bytes=len(raw), received_at_unix=received_at,
                      server_timestamp=data.get("serverTime", data.get("time")) if isinstance(data, dict) else None)
        if name == "hyperliquid" and valid:
            timestamp = data.get("time")
            if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool) and math.isfinite(timestamp):
                age = received_at - timestamp / 1000
                result.update(age_seconds=round(age, 3), freshness="fresh" if -5 <= age <= 30 else "stale_or_clock_skew")
            else:
                result["freshness"] = "invalid_timestamp"
    except urllib.error.HTTPError as error:
        result.update(status="failed", reason="http_" + str(error.code))
    except (urllib.error.URLError, ValueError, OSError):
        result.update(status="failed", reason="network_or_invalid_json")
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("venues", nargs="+", choices=TARGETS)
    args = parser.parse_args()
    results = [probe(name) for name in dict.fromkeys(args.venues)]
    folder = Path(__file__).resolve().parent.parent / "runtime"
    folder.mkdir(exist_ok=True)
    (folder / ("public_probes_" + str(time.time_ns()) + ".json")).write_text(
        json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))
    return 0 if all(row["status"] == "public_response" for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
