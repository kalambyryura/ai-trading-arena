"""Run one pinned public-market PAPER round in a separate credential-free copy.

The owner authorizes the observation; this launcher has no exchange order path.
Only an existing, explicitly pinned isolation package is accepted. No retries,
automatic new seasons, recipe changes, or recovery after a process restart.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from urllib.parse import urlsplit
import urllib.request


PUBLIC_PATHS = frozenset({"/api/v5/public/time", "/api/v5/public/instruments",
                          "/api/v5/market/ticker", "/api/v5/market/candles",
                          "/api/v5/market/history-candles", "/api/v5/public/funding-rate"})


def public_request_allowed(request, data=None):
    if not isinstance(request, urllib.request.Request):
        return False
    url = urlsplit(request.full_url)
    return (request.get_method() == "GET" and request.data is None and data is None
            and url.scheme == "https" and url.netloc == "openapi.okx.com"
            and not url.fragment and not url.username and not url.password
            and url.path in PUBLIC_PATHS
            and not any(k.lower().startswith(("ok-access-", "authorization", "proxy-authorization"))
                        for k, _ in request.header_items()))


def verify_package(root):
    root = Path(root).resolve()
    marker = root / "paper-isolation.json"
    package = json.loads(marker.read_text(encoding="utf-8"))
    if package.get("mode") != "PAPER" or package.get("execution_mode") != "rules":
        raise ValueError("paper_only_package_required")
    if package.get("admission_seconds") != 18000 or package.get("budget_usdt_per_bot") != 100:
        raise ValueError("unexpected_five_hour_protocol")
    if any(root.glob(".env*.local")) or (root / "LocalSettings/exchange_accounts.json").exists():
        raise ValueError("isolated_package_must_not_contain_credentials")
    pins = package.get("files")
    if not isinstance(pins, dict) or not pins:
        raise ValueError("source_pins_required")
    for relative, expected in pins.items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("source_pin_path")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("isolated_source_changed")
    roster = json.loads((root / "runtime/arena_v4/manifest.json").read_text(encoding="utf-8"))
    if roster.get("seasons") or roster.get("active_id"):
        raise ValueError("already_started_no_automatic_resume")
    actual = {s["id"]: s["recipe_sha256"] for s in roster["strategies"]}
    if actual != package.get("recipes") or len(actual) != 4:
        raise ValueError("exact_four_recipes_required")
    return package


def run(root, port):
    root = Path(root).resolve()
    package = verify_package(root)
    # Credentials are neither needed nor inherited by this public observation.
    for name in list(os.environ):
        if name.upper().startswith(("OKX_", "JEV_", "TYPESAFE_")):
            del os.environ[name]
    sys.path.insert(0, str(root))
    import trading_lab.arena as arena_module
    from trading_lab.web_panel import Panel, Server, Handler
    from trading_lab.arena_strategies import fingerprint
    from trading_lab.strategy_contract import payload_of
    if Path(arena_module.__file__).resolve().parent.parent != root:
        raise ValueError("wrong_source_copy")

    outbound = {"public_gets": 0, "blocked_requests": 0}
    original_open = urllib.request.OpenerDirector.open

    def guarded_open(opener, request, data=None, *args, **kwargs):
        if not public_request_allowed(request, data):
            outbound["blocked_requests"] += 1
            raise ValueError("isolated_paper_public_get_only")
        outbound["public_gets"] += 1
        return original_open(opener, request, data, *args, **kwargs)

    urllib.request.OpenerDirector.open = guarded_open

    class PaperViewer(Handler):
        def do_POST(self):
            # The existing owner Stop applies only to virtual positions here.
            if self.path == "/api/arena/stop":
                return super().do_POST()
            return self.send(403, {"error": "Окрема PAPER-проба: нові запуски й біржові дії вимкнені."})

    receipt_path = root / "runtime/forward_observation.json"
    panel = Panel(root)
    with Server(panel, port) as server:
        server.RequestHandlerClass = PaperViewer
        for strategy in server.arena.data["strategies"]:
            if fingerprint(payload_of(strategy)) != strategy["recipe_sha256"]:
                raise ValueError("recipe_payload_hash_mismatch")
        result = server.arena.start(server.arena.data["generation"], list(package["recipes"]),
                                    100, 18000, execution_mode="rules")
        server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .5},
                                         name="paper-viewer", daemon=True)
        server_thread.start()
        final = False
        try:
            while True:
                with server.arena.lock:
                    state = server.arena.session
                    summary = {"schema": "isolated-forward-paper/v1", "mode": "PAPER",
                               "execution_mode": "rules", "pid": os.getpid(),
                               "root": str(root), "url": server.origin + "/arena",
                               "season_id": result["id"], "status": state["status"],
                               "started_at": state["started_at"], "admission_end_at": state["end_at"],
                               "updated_at": datetime.now(timezone.utc).isoformat(),
                               "code_sha256": state["code_sha256"], "recipe_sha256": package["recipes"],
                               "last_market_observed": (state.get("_last") or {}).get("at"),
                               "events": state.get("_event_seq", 0), "incomplete": state["incomplete"],
                               "public_requests": dict(outbound), "paid_calls": 0,
                               "financial_actions": 0, "production_workspace_used": False}
                pending = receipt_path.with_suffix(".tmp")
                pending.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                os.replace(pending, receipt_path)
                if not final and state["status"] in {"DONE", "PAUSED"}:
                    print(json.dumps(summary, ensure_ascii=False), flush=True)
                    final = True
                # Keep the viewer available after completion; no second season starts.
                time.sleep(15)
        finally:
            server.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or args.port == 8765:
        parser.error("Use a separate local preview port, not production 8765.")
    if args.check_only:
        verify_package(args.root)
        print("PINNED_PAPER_PACKAGE_OK; no worker or network started")
    else:
        run(args.root, args.port)


if __name__ == "__main__":
    main()
