"""Judge: facts of one finished Live round for one candidate, read from the owner's Battle ZIP (gate D5 register).

Reads only the ZIP (manifest.json, match-settings.json, battle-review.json): no network, keys or account. Prints one JSON
object with what GATE_D_PROCEDURE_UA.md section 5 (D124, D162, D166, D168) needs for team/judge/LIVE_ROUNDS.jsonl:
the round window, code hashes, the candidate's decision funnel, orders and fills, settlement, the start-up gap before
the first decision, and its degradation windows (count, seconds, skipped decisions). It decides nothing: whether the
round counts for D5 is the Judge's entry in the register.

  py -3.12 -X utf8 -B team/judge/tools/live_round_facts.py --zip <battle.zip> --fingerprint d744a6f5...
      [--candidate H10-ETH --recipe-sha256 dcf48666...]

--fingerprint is the workshop fingerprint of the recipe as the round lists it (participants[].recipe_sha256), or any
unique part of the participant's name.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import zipfile

ORDER_CODES = ("exchange_order_intent", "exchange_order_posted", "exchange_order_read", "exchange_entry_fill", "exchange_close_fill",
               "entry_cancelled", "protective_intent", "protective_placed", "fill_protected", "protective_terminal",
               "protective_child_adopted", "exit_trigger", "rules_close", "exchange_check_failed", "stop_requested",
               "manual_review", "closing_paused", "recovery_required", "liquidation")


def seconds(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() if text else None


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def sums_match(path, digest):
    """True / False against the SHA256SUMS beside the ZIP, None when there is none."""
    sums = Path(path).with_name("SHA256SUMS")
    if not sums.is_file():
        return None
    for line in sums.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == Path(path).name:
            return parts[0].lower() == digest
    return False


def availability(events, started, admission_end, cooldown_seconds, windows):
    """Where the admission bars of one bot went (procedure section 5, D166 and D196).

    A decision bar b (bar_time_ms of entry_checks / position_checks: the close of the last closed minute) is decided
    during the wall-clock minute [b, b + 60 s). A bar without a check is not lost when the recipe itself prescribes no
    decision in that minute: an entry order is working, the cooldown after an exit runs, or an order or exit was in
    flight. Lost bars are the start-up gap before the first check, bars inside a degradation window, minutes of an open
    position without a position check, and flat free minutes without an entry check."""
    decided = {int(e["data"]["bar_time_ms"]) // 1000 for e in events
               if e.get("code") in ("entry_checks", "position_checks") and (e.get("data") or {}).get("bar_time_ms") is not None}
    order, position, cooldown, intent, opened = [], [], [], None, None
    for e in sorted(events, key=lambda item: item["seq"]):
        code, at, data = e.get("code"), seconds(e["at"]), e.get("data") or {}
        if code == "exchange_order_intent":
            body = data.get("order_body") or {}
            if not body.get("reduceOnly") and opened is None:
                intent = at
            else:  # a close in flight: the minute is busy, not lost
                order.append((at, at + 5))
        elif code == "exchange_entry_fill":
            if intent is not None:
                order.append((intent, at))
            intent, opened = None, at if opened is None else opened
        elif code in ("entry_cancelled", "exchange_order_read") and intent is not None and str(data.get("order_state", "canceled")) != "filled":
            order.append((intent, at))
            intent = None
        elif code == "exchange_close_fill" and opened is not None:
            if float(data.get("position_btc") or 0) == 0:
                position.append((opened, at))
                cooldown.append((at, at + cooldown_seconds))
                opened = None
    if intent is not None:
        order.append((intent, admission_end))
    if opened is not None:
        position.append((opened, admission_end))
    degraded = [(int(w["start_ms"]) / 1000, int(w["start_ms"]) / 1000 + float(w.get("seconds") or 0)) for w in (windows or [])]

    def overlaps(intervals, a, b):
        return any(x < b and y > a for x, y in intervals)

    first_check = min(decided) if decided else None
    counts = {"bars": 0, "decided": 0, "startup": 0, "order_or_cooldown": 0, "degraded": 0, "position_without_check": 0, "flat_without_check": 0}
    run = longest = 0
    bar = int(started // 60) * 60
    while bar < admission_end:
        counts["bars"] += 1
        if bar in decided:
            kind = "decided"
        elif first_check is None or bar < first_check:
            kind = "startup"
        elif overlaps(order, bar, bar + 60) or overlaps(cooldown, bar, bar + 60):
            kind = "order_or_cooldown"
        elif overlaps(degraded, bar, bar + 60):
            kind = "degraded"
        elif overlaps(position, bar, bar + 60):
            kind = "position_without_check"
        else:
            kind = "flat_without_check"
        counts[kind] += 1
        if kind in ("degraded", "position_without_check", "flat_without_check"):
            run += 1
            longest = max(longest, run)
        else:
            run = 0
        bar += 60
    lost = counts["startup"] + counts["degraded"] + counts["position_without_check"] + counts["flat_without_check"]
    return {**counts, "lost": lost, "lost_share": round(lost / counts["bars"], 4) if counts["bars"] else None,
            "lost_after_startup": lost - counts["startup"], "longest_lost_run_after_startup": longest,
            "entry_orders": len(order), "positions": len(position), "cooldown_seconds": cooldown_seconds}


def facts(path, fingerprint, candidate=None, recipe_sha256=None):
    digest = sha256_file(path)
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        settings = json.loads(archive.read("match-settings.json"))
        review = json.loads(archive.read("battle-review.json"))
        strategies = json.loads(archive.read("strategies.json")) if "strategies.json" in archive.namelist() else []
    found = [p for p in review["participants"]
             if fingerprint.lower() in (p.get("recipe_sha256") or "").lower() or fingerprint.lower() in (p.get("name") or "").lower()]
    if len(found) != 1:
        raise SystemExit(f"{len(found)} participants match {fingerprint!r}: " + ", ".join(p.get("name", "?") for p in review["participants"]))
    bot = found[0]
    events = [e for e in review["events"] if e.get("bot_id") == bot["id"]]
    codes = Counter(e.get("code") for e in events)
    decisions = [e["at"] for e in events if e.get("code") == "entry_checks"]
    started, admission_end = seconds(settings.get("started_at")), seconds(settings.get("admission_end_at"))
    entry = (bot.get("funnel") or {}).get("entry") or {}
    bars = round((settings.get("duration_seconds") or 0) / 60)
    first, last = (min(decisions), max(decisions)) if decisions else (None, None)
    windows = [w for w in (review.get("degraded_windows") or []) if w.get("bot_id") == bot["id"]] if "degraded_windows" in review else None
    leverage = [e.get("data") for e in events if e.get("code") == "leverage_confirmed"]
    quality, ledger, costs = bot.get("quality") or {}, bot.get("trade_ledger") or {}, bot.get("costs") or {}
    out = {
        "candidate": candidate, "recipe_sha256": recipe_sha256, "workshop_fingerprint_sha256": bot.get("recipe_sha256"),
        "battle_id": manifest.get("battle_id"), "mode": manifest.get("mode"), "execution_mode": manifest.get("execution_mode"),
        "status": manifest.get("status"), "final": manifest.get("final"), "manifest_incomplete_flag": manifest.get("incomplete"),
        "history_complete": manifest.get("history_complete"), "event_count": manifest.get("event_count"), "exported_at": manifest.get("exported_at"),
        "zip": Path(path).name, "zip_sha256": digest, "sha256sums_match": sums_match(path, digest),
        "bundle_sha256": (review.get("integrity") or {}).get("bundle_sha256"),
        "code_hashes": manifest.get("code_hashes") or None, "round_code_sha256": settings.get("code_sha256"),
        "instruments": (review.get("match") or {}).get("instruments") or (review.get("match") or {}).get("instrument"),
        "bots_in_season": len(review["participants"]),
        "started_at": settings.get("started_at"), "admission_end_at": settings.get("admission_end_at"),
        "admission_closed_at": settings.get("admission_closed_at"), "finished_at": settings.get("finished_at"),
        "duration_seconds": settings.get("duration_seconds"), "budget_usdt_per_bot": settings.get("budget_usdt_per_bot"),
        "bot": {"name": bot.get("name"), "instrument": bot.get("instrument"), "status": bot.get("status"), "allocation_usdt": bot.get("allocation_usdt"),
                "leverage_confirmed_events": leverage or None},
        "decisions": {"admission_bars": bars, "entry_checks": entry.get("checks"), "signals": entry.get("signals"),
                      "unknown_checks": entry.get("unknown_checks"), "signals_unknown": entry.get("signals_unknown"),
                      "first_decision_at": first, "last_decision_at": last,
                      "startup_gap_seconds": round(seconds(first) - started, 1) if first and started else None,
                      "last_decision_before_admission_end_seconds": round(admission_end - seconds(last), 1) if last and admission_end else None,
                      # distinct closed bars with a decision (entry or position checks); V1 bots decide several times per bar
                      "bars_with_decision": quality.get("distinct_closed_bars") if quality.get("distinct_closed_bars") is not None else entry.get("checks"),
                      "decision_coverage_of_admission_bars": round((quality.get("distinct_closed_bars") if quality.get("distinct_closed_bars") is not None
                                                                    else entry.get("checks", 0)) / bars, 4) if bars else None},
        "orders": {code: codes.get(code, 0) for code in ORDER_CODES if codes.get(code)},
        "fills": codes.get("exchange_entry_fill", 0),
        "participation": bot.get("participation"),
        "trade_ledger": {k: ledger.get(k) for k in ("missing_order_results", "unresolved", "inventory_known", "remaining")}
        | {"rows": len(ledger.get("rows") or []), "round_trips": len(ledger.get("round_trips") or [])},
        "costs": {k: costs.get(k) for k in ("gross_pnl_usdt", "signed_fees_usdt", "signed_funding_usdt", "net_usdt", "cash_delta_usdt", "complete", "source")},
        "quality": {"data_status": quality.get("data_status"), "settlement": (quality.get("settlement") or {}).get("status"),
                    "availability": (quality.get("availability") or {}).get("status"),
                    "competitive_validity": (quality.get("competitive_validity") or {}).get("status"),
                    "distinct_closed_bars": quality.get("distinct_closed_bars")},
        "decision_availability": availability(
            events, started, admission_end,
            int(next((((s.get("recipe") or {}).get("rules") or {}).get("cooldown_seconds") or 0) for s in strategies if s.get("id") == bot["id"]) or 0)
            if any(s.get("id") == bot["id"] for s in strategies) else 0, windows) if started and admission_end else None,
        "market_gaps_in_season": len(review.get("market_gaps") or []),
        "degraded_windows": None if windows is None else {
            "count": len(windows), "seconds_total": round(sum(float(w.get("seconds") or 0) for w in windows), 3),
            "seconds_max": max((float(w.get("seconds") or 0) for w in windows), default=0.0),
            "skipped_decisions_total": sum(int(w.get("skipped_decisions") or 0) for w in windows),
            "skipped_decisions_max": max((int(w.get("skipped_decisions") or 0) for w in windows), default=0),
            "outcomes": sorted({str(w.get("outcome")) for w in windows}), "extended": sum(bool(w.get("extended")) for w in windows),
            "codes": sorted({c for w in windows for c in (w.get("codes") or [])}),
            "position_or_order_fields_present": sorted({k for w in windows for k in w if "position" in k or "order" in k or "stop" in k})},
    }
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--zip", type=Path, required=True)
    parser.add_argument("--fingerprint", required=True)
    parser.add_argument("--candidate")
    parser.add_argument("--recipe-sha256")
    args = parser.parse_args(argv)
    print(json.dumps(facts(args.zip, args.fingerprint, args.candidate, args.recipe_sha256), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
