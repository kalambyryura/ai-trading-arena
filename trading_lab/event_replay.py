"""Causal, privacy-projected replay packets. Never copy final metrics into past frames."""
from datetime import datetime, timezone
from decimal import Decimal
import re
from .battle_review import build_review, digest, number, verify_bundle
from .arena_strategies import ArenaError

SCHEMA = "arena-display-replay/v1"
FINANCIAL = {"position_btc", "equity_usdt", "pnl_usdt", "gross_usdt", "signed_fees_usdt", "signed_funding_usdt", "known_jev_usd", "trades"}


# Event codes whose replay type is fixed regardless of the words in them ("fill_protected" is no fill, a stop's life cycle
# is no position observation).
ORDER_LIFECYCLE = {"exchange_order_posted": "INTENT", "exchange_cancel_posted": "INTENT", "entry_order_placed": "INTENT",
                   "entry_order_rejected": "CHECK", "entry_order_expired": "CHECK", "entry_order_cancelled": "CHECK",
                   "entry_order_cancel_failed": "CHECK", "entry_order_unwatched_cancel": "CHECK", "fill_protected": "CHECK",
                   "fill_watch_failed": "CHECK", "fill_read_lag": "CHECK", "protective_placed": "CHECK",
                   "protective_amended": "CHECK", "protective_terminal": "CHECK", "accounting_evidence": "CHECK",
                   # R2.1.14: our own stop fired during an account check
                   "protective_fired_during_check": "CHECK", "protective_fire_pending": "CHECK", "protective_fire_resolved": "CHECK",
                   "protective_fire_expired": "CHECK",
                   # R2.1.14 (R2113-RESUME-SETTLED): a bot settled before a pause, read again on the Resume
                   "settled_account_verified": "CHECK", "settled_account_unverified": "CHECK",
                   "protective_cancel_posted": "INTENT",
                   # R2.1.10: the run's execution profile, the confirmed leverage, a renewed Stop, step timing
                   "execution_profile": "CHECK", "leverage_confirmed": "CHECK", "stop_renewed": "CHECK", "step_timing": "CHECK",
                   "exchange_order_read": "CHECK", "degraded_window_started": "CHECK", "degraded_window_ended": "CHECK",
                   "degraded_extension_ended": "CHECK", "protective_close_pending": "CHECK", "entry_hold": "CHECK",
                   # R2.1.5: an order the venue refused or never showed, and the schema-4 book's state
                   "exchange_order_rejected": "CHECK", "exchange_order_absent": "CHECK", "exchange_order_not_visible": "CHECK",
                   "feature_book": "CHECK", "exchange_order_reopened": "CHECK", "exchange_order_fill_not_visible": "CHECK",
                   "exchange_order_found": "CHECK",
                   # R2.1.15: a leveraged bot's position facts and its liquidation guards
                   "entry_position_facts": "CHECK", "liquidation_unknown": "CHECK", "liquidation_too_close": "CHECK",
                   "liquidation_closer_than_stop": "CHECK", "liquidation_detected": "CHECK",
                   # R2.1.15 (QR-01): a stop OKX never showed
                   "protective_absent": "CHECK", "protective_not_created": "CHECK"}

def ms(value, fallback=0):
    if isinstance(value, (float, int)) and not isinstance(value, bool): return int(value * 1000)
    try: return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, TypeError, AttributeError): return fallback


def decimals(values):
    return {k: int(v) if k == "trades" and type(v) is int else number(v) for k, v in values.items() if k in FINANCIAL}


def build_packet(state):
    bundle = build_review(state)
    verify_bundle(bundle)
    start = ms(state.get("started_at", state.get("created_at")), ms(state.get("_start", 0)))
    admission = ms(state.get("admission_end_at", state.get("end_at")), start + int(state.get("duration_seconds", 300))*1000)
    if admission <= start: raise ArenaError("Нечинний час маніфесту.")
    recipes = {r["id"]: r for r in state.get("strategies", [])}
    roster = []
    for index, bot in enumerate(state["bots"]):
        recipe = recipes.get(bot.get("strategy_id", bot["id"]), {})
        roster.append({"id": bot["id"], "name": bot.get("name", bot["id"])[:80], "strategy_id": bot.get("strategy_id", bot["id"]),
            "version": str(recipe.get("version", "baseline")), "avatar_kind": "baseline" if bot["id"] == "cash" else
                "human" if recipe.get("author", {}).get("kind") == "human" else "model", "slot": index})
    events, availability = [], start
    def add(kind, code, when, bot_id=None, **fields):
        nonlocal availability
        source = ms(when, availability)
        availability = max(availability, source)
        item = {"seq": len(events)+1, "available_at_ms": availability, "source_at_ms": source,
                "type": kind, "code": code, **fields}
        if bot_id: item["bot_id"] = bot_id
        events.append(item)
    if any(r["id"] == "cash" for r in roster):
        add("POSITION", "cash_baseline", start/1000, "cash", financial={"position_btc": "0", "equity_usdt": number(state["budget_usdt"]),
            "pnl_usdt": "0", "gross_usdt": "0", "signed_fees_usdt": "0", "signed_funding_usdt": "0", "known_jev_usd": "0", "trades": 0}, settled=True)
    traces = {p["id"]: {row["seq"]: row for row in p["trade_ledger"]["rows"]} for p in bundle["participants"]}
    quantities = {b["id"]: Decimal(0) for b in state["bots"]}
    totals = {b["id"]: {"gross_usdt": Decimal(0), "signed_fees_usdt": Decimal(0), "signed_funding_usdt": Decimal(0)} for b in state["bots"]}
    frames = {}
    # Legacy frames are only made available at their explicit event boundary.
    if not any(e["code"] == "financial_observation" for e in state.get("events", [])):
        for frame in bundle["frames"]:
            boundary = frame.get("event_seq")
            if type(boundary) is int: frames.setdefault(boundary, []).append(frame)
    for raw, public in zip(state.get("events", []), bundle["events"]):
        code, bot_id, data = raw["code"], raw.get("bot_id"), public["data"]
        kind = "CHECK" if "check" in code else "DECISION" if "jev" in code or "decision" in code else "INTENT" if "intent" in code or "trigger" in code else "FILL" if "fill" in code else "QUALITY" if code in {"data_gap", "clock_gap", "market_unavailable", "exchange_error", "paused"} else "POSITION"
        kind = ORDER_LIFECYCLE.get(code, kind)  # R2/R2.1 order and stop events are neither fills nor position observations
        fields = {"step": raw["step"]} if type(raw.get("step")) is int and 1 <= raw["step"] <= 9 else {}
        if code in {"data_gap", "clock_gap", "market_unavailable", "exchange_error", "paused"}:
            fields["quality"] = {"coverage": "GAP", "billing": "UNKNOWN", "reason": code}
            if isinstance((raw.get("data") or {}).get("instrument"), str):  # R2.1.13 (X2-07): whose market, in a mixed season
                fields["quality"]["instrument"] = raw["data"]["instrument"]
        if code == "admission_closed":
            kind = "ADMISSION_CLOSED" if ms(raw["at"], availability) >= admission else "PHASE"
            fields["phase"] = "DRAINING"
        if code in {"closing_paused", "paused"}: fields["phase"] = "PAUSED"
        if code == "stop_requested": fields["phase"] = "CLOSING" if raw.get("bot_id") is None else None
        if fields.get("phase") is None: fields.pop("phase", None)
        if code == "financial_observation":
            fields["financial"] = decimals((raw.get("data") or {}).get("financial", {}))
            fields["settled"] = data.get("settled", False)
        trace = traces.get(bot_id, {}).get(raw["seq"])
        if trace and trace.get("role") in {"entry", "add", "close"}:
            bot = next(b for b in state["bots"] if b["id"] == bot_id)
            factor = Decimal(1) if state["mode"] == "PAPER" else Decimal(str((bot.get("_instrument") or {}).get("contract_value", 0))) * Decimal(str((bot.get("_instrument") or {}).get("contract_multiplier", 0)))
            sign = -1 if recipes.get(bot_id, {}).get("rules", {}).get("side") == "short" else 1
            if state["mode"] != "PAPER":
                result = (raw.get("data") or {}).get("result") or {}
                order = next((o for o in bot.get("_orders", []) if (o.get("result") or {}).get("ordId") == result.get("ordId")), None)
                if order and trace["role"] != "close": sign = 1 if order["request"]["side"] == "buy" else -1
            else:
                sign = -1 if (raw.get("data") or {}).get("side") == "short" else sign
            quantity = Decimal(trace["quantity"]) * factor
            if trace["role"] == "close": quantities[bot_id] += -quantity if quantities[bot_id] > 0 else quantity
            else: quantities[bot_id] += quantity * sign
            fields["financial"] = {"position_btc": number(quantities[bot_id]) if factor else None}
            for source, target in (("gross_pnl_usdt", "gross_usdt"), ("signed_fees_usdt", "signed_fees_usdt"), ("signed_funding_usdt", "signed_funding_usdt")):
                value = number(trace.get(source))
                if value is not None:
                    totals[bot_id][target] += Decimal(value)
                    fields["financial"][target] = number(totals[bot_id][target])
        if code == "accounting_confirmed":
            actual = raw.get("data") or {}
            bridge = [number(actual.get(k)) for k in ("gross_pnl_usdt", "signed_fees_usdt", "signed_funding_usdt")]
            net = number(sum(map(Decimal, bridge))) if all(v is not None for v in bridge) else None
            fields.update(settled=True, financial={"position_btc": "0", "pnl_usdt": net,
                "equity_usdt": number(Decimal(str(state["budget_usdt"])) + Decimal(net)) if net is not None else None,
                "gross_usdt": number(actual.get("gross_pnl_usdt")), "signed_fees_usdt": number(actual.get("signed_fees_usdt")),
                "signed_funding_usdt": number(actual.get("signed_funding_usdt"))})
            kind = "SETTLEMENT"
        add(kind, code, raw["at"], bot_id, source_event_seq=raw["seq"], **fields)
        for frame in frames.get(raw["seq"], []):
            for bot in frame["bots"]:
                settlement = {"settled": True} if bot["id"] == "cash" or (state["mode"] == "PAPER" and raw["code"] == "season_finished") else {}
                add("POSITION", "recorded_frame", frame["at"], bot["id"], source_event_seq=raw["seq"], financial=decimals({
                    **{k: bot.get(k) for k in ("position_btc", "equity_usdt", "pnl_usdt", "trades")},
                    "signed_fees_usdt": -Decimal(str(bot["fees_usdt"])) if bot.get("fees_usdt") is not None else None,
                    "signed_funding_usdt": -Decimal(str(bot["funding_usdt"])) if state["mode"] != "PAPER" and bot.get("funding_usdt") is not None else bot.get("funding_usdt"),
                    "known_jev_usd": bot.get("ai_cost_usd")}), **settlement)
    complete = state["status"] == "DONE" and all(number(p["position_btc"]) is not None and Decimal(number(p["position_btc"])) == 0 and p["costs"].get("complete") for p in bundle["participants"])
    if complete:
        observed_settlement = {e.get("bot_id") for e in events if e.get("settled") and e.get("financial", {}).get("position_btc") is not None and Decimal(e["financial"]["position_btc"]) == 0}
        if state["mode"] == "PAPER" and any(e["code"] == "season_finished" for e in state.get("events", [])):
            # The final modeled settlement is only available after the recorded completion.
            for p in bundle["participants"]:
                add("SETTLEMENT", "paper_settlement", availability/1000, p["id"], financial={"position_btc": "0", "pnl_usdt": number(p["costs"].get("net_usdt"))}, settled=True)
                observed_settlement.add(p["id"])
        if all(r["id"] in observed_settlement for r in roster):
            add("PHASE", "recording_finished", availability/1000, phase="DONE")
    packet = {"schema": SCHEMA, "evidence_kind": "SYNTHETIC" if state.get("fixture_only") else "MODEL" if state["mode"] == "PAPER" else "ACTUAL",
        "manifest": {"id": state["id"], "mode": state["mode"], "started_at_ms": start, "admission_end_at_ms": admission,
            "recording_end_at_ms": availability, "time_policy": "admission_then_strategy_exit", "participants": roster,
            "renderer_version": "coliseum-v7", "assets_version": "coliseum-v7", "visual_seed": state["id"]}, "events": events}
    packet["package_hash"] = digest(packet)
    return packet


def verify_packet(packet):
    """Verify the typed projection and causal completion, not just a rehash."""
    def reject(): raise ArenaError("Нечинний або неповний causal replay.")
    def keys(value, allowed, required=()):
        if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value): reject()
    def timestamp(value):
        if type(value) is not int or not 0 <= value <= 9007199254740991: reject()
    def alias(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value): reject()
    def text(value):
        if not isinstance(value, str) or not 1 <= len(value) <= 80 or any(ord(c) < 32 for c in value): reject()
    keys(packet, {"schema", "evidence_kind", "package_hash", "manifest", "events"},
         {"schema", "evidence_kind", "package_hash", "manifest", "events"})
    if packet["schema"] != SCHEMA or packet["evidence_kind"] not in {"ACTUAL", "MODEL", "SYNTHETIC"}: reject()
    if not isinstance(packet["package_hash"], str) or not re.fullmatch(r"[a-f0-9]{64}", packet["package_hash"]): reject()
    original = {k: v for k, v in packet.items() if k != "package_hash"}
    try: valid_hash = digest(original) == packet["package_hash"]
    except (TypeError, ValueError): reject()
    if not valid_hash: raise ArenaError("Хеш replay не збігається.")
    manifest = packet["manifest"]
    manifest_keys = {"id", "mode", "started_at_ms", "admission_end_at_ms", "recording_end_at_ms", "time_policy", "participants", "renderer_version", "assets_version", "visual_seed"}
    keys(manifest, manifest_keys, manifest_keys)
    alias(manifest["id"])
    if manifest["mode"] not in {"OKX_LIVE", "OKX_DEMO", "PAPER"} or manifest["time_policy"] != "admission_then_strategy_exit": reject()
    for key in ("started_at_ms", "admission_end_at_ms", "recording_end_at_ms"): timestamp(manifest[key])
    if manifest["admission_end_at_ms"] <= manifest["started_at_ms"] or manifest["recording_end_at_ms"] < manifest["started_at_ms"]: reject()
    for key in ("renderer_version", "assets_version", "visual_seed"): text(manifest[key])
    roster = manifest["participants"]
    if not isinstance(roster, list) or not 1 <= len(roster) <= 25: reject()
    ids, slots, observed = set(), set(), {}
    for participant in roster:
        fields = {"id", "name", "strategy_id", "version", "avatar_kind", "slot"}
        keys(participant, fields, fields)
        alias(participant["id"]); alias(participant["strategy_id"])
        text(participant["name"]); text(participant["version"])
        if participant["avatar_kind"] not in {"human", "model", "baseline"} or participant["id"] in ids: reject()
        if type(participant["slot"]) is not int or not 0 <= participant["slot"] < len(roster) or participant["slot"] in slots: reject()
        ids.add(participant["id"]); slots.add(participant["slot"])
        observed[participant["id"]] = {"position": None, "settled": False}
    if not isinstance(packet["events"], list): reject()
    availability, phase = manifest["started_at_ms"], "RUNNING"
    for seq, event in enumerate(packet["events"], 1):
        keys(event, {"seq", "source_event_seq", "available_at_ms", "source_at_ms", "type", "bot_id", "code", "step", "financial", "phase", "settled", "quality"}, {"seq", "available_at_ms", "type", "code"})
        if type(event["seq"]) is not int or event["seq"] != seq: reject()
        if "source_event_seq" in event:
            timestamp(event["source_event_seq"])
            if event["source_event_seq"] < 1: reject()
        timestamp(event["available_at_ms"])
        if not availability <= event["available_at_ms"] <= manifest["recording_end_at_ms"]: reject()
        availability = event["available_at_ms"]
        if "source_at_ms" in event:
            timestamp(event["source_at_ms"])
            if event["source_at_ms"] > availability: reject()
        if event["type"] not in {"CHECK", "DECISION", "INTENT", "FILL", "POSITION", "ACCOUNTING", "ADMISSION_CLOSED", "QUALITY", "SETTLEMENT", "PHASE"}: reject()
        alias(event["code"])
        bot_id = event.get("bot_id")
        if "bot_id" in event and (not isinstance(bot_id, str) or bot_id not in ids): reject()
        if "step" in event and (type(event["step"]) is not int or not 1 <= event["step"] <= 9): reject()
        if "phase" in event:
            if event["phase"] not in {"RUNNING", "DRAINING", "CLOSING", "PAUSED", "DONE"}: reject()
            phase = event["phase"]
        if event["type"] == "ADMISSION_CLOSED" and (availability < manifest["admission_end_at_ms"] or phase != "DRAINING"): reject()
        if "quality" in event:
            q = event["quality"]
            keys(q, {"coverage", "billing", "reason", "instrument"}, {"coverage", "billing"})
            if q["coverage"] not in {"COMPLETE", "GAP", "UNKNOWN"} or q["billing"] not in {"KNOWN", "UNKNOWN"}: reject()
            if "reason" in q: alias(q["reason"])
        if "financial" in event:
            if bot_id is None: reject()
            values = event["financial"]
            keys(values, FINANCIAL)
            for key, value in values.items():
                if value is None: continue
                if key == "trades":
                    if type(value) is not int or not 0 <= value <= 9007199254740991: reject()
                # Match the browser DTO: preserve tiny recorded amounts exactly.
                # Rounding a small nonzero position would falsely make it flat.
                elif not isinstance(value, str) or not re.fullmatch(r"-?(0|[1-9]\d{0,39})(\.\d{1,64})?", value): reject()
            if "position_btc" in values:
                observed[bot_id]["position"] = values["position_btc"]
                if values["position_btc"] is not None and Decimal(values["position_btc"]) != 0:
                    observed[bot_id]["settled"] = False
        if "settled" in event:
            if type(event["settled"]) is not bool or bot_id is None: reject()
            observed[bot_id]["settled"] = event["settled"]
        if phase == "DONE" and any(row["position"] is None or Decimal(row["position"]) != 0 or not row["settled"] for row in observed.values()): reject()
    return {"verified": True, "package_hash": packet["package_hash"], "events": len(packet["events"])}
