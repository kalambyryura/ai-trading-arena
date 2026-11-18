"""Bounded, offline, evidence-linked trade debrief. No model/exchange transport."""
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html import escape
import json
import re
from pathlib import Path
import zipfile

from .arena_strategies import ArenaError
from .battle_review import digest, verify_bundle
from .execution_sensitivity import analyze_review, SensitivityError

MAX_JSON_BYTES = 64 * 1024**2
MAX_ARCHIVE_BYTES = 2 * 1024**3
MAX_EVENTS = 100000
MAX_QUOTES = 25000
MAX_CHART_POINTS = 500
SCHEMA = "jev-battle-debrief/v1"
REASONS = {
    "g1_stop_loss": "G1: досягнуто абсолютного стопа", "g1_take_profit": "G1: досягнуто геометричної цілі",
    "g1_failed_breakout": "G1: дві завершені H1 підтвердили невдалий пробій",
    "g1_max_hold": "G1: 24 години утримання за власним правилом",
    "g1_owned_paper_state_mismatch": "G1: неузгоджений стан модельної позиції",
    "g1_owned_paper_state_unreadable": "G1: стан модельної позиції не читається",
    "stop_loss": "Спрацював stop-loss", "take_profit": "Досягнуто take-profit",
    "rules_exit": "Збіглися правила виходу", "formal_exit": "Збіглися правила виходу",
    "max_hold": "Вичерпано час утримання за рецептом", "owner_stop": "Зупинка власником",
    "risk_limit": "Обмеження ризику", "recovery": "Відновлення й закриття",
    "formal_reduce": "Часткове скорочення", "cost_checkpoint": "Перевірка витрат",
    "deadline": "Дедлайн за старим протоколом", "data_unavailable": "Дані недоступні",
    "stop_requested": "Зафіксовано запит зупинки; ініціатор не встановлений",
}


def dec(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        if len(str(value)) > 128:
            return None
        result = Decimal(str(value))
        return result if (result.is_finite() and abs(result.as_tuple().exponent) <= 40
                          and result.copy_abs() <= Decimal("1e18")) else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def numeric(value):
    value = dec(value)
    return format(value, "f") if value is not None else None


def seconds(value):
    if isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return stamp.timestamp() if stamp.tzinfo else None
        except (ValueError, OverflowError):
            return None
    value = dec(value)
    return float(value) if value is not None and abs(value) < Decimal("1e12") else None


def _sum(rows, key):
    values = [dec(row.get(key)) for row in rows]
    return sum(values, Decimal(0)) if all(v is not None for v in values) else None


def _sample(points):
    """Decimate only the visual points; retain gaps from the original observations."""
    segments, segment = [], 0
    for i, point in enumerate(points):
        if i and point[0] < points[i-1][0]:
            raise ArenaError("Кадри PnL не впорядковано за часом.")
        if i and point[0] - points[i-1][0] > 90:
            segment += 1
        segments.append(segment)
    if len(points) <= MAX_CHART_POINTS:
        indices = range(len(points))
    else:
        chosen = {0, len(points) - 1, min(range(len(points)), key=lambda i: dec(points[i][1])),
                  max(range(len(points)), key=lambda i: dec(points[i][1]))}
        chosen.update(round(i * (len(points) - 1) / (MAX_CHART_POINTS - 5))
                      for i in range(MAX_CHART_POINTS - 4))
        indices = sorted(chosen)
    result, previous_segment = [], None
    for i in indices:
        result.append([*points[i][:2], segments[i] != previous_segment])
        previous_segment = segments[i]
    return result


def _quote_path(quotes, start, end, initial_price, side):
    path = []
    if start is not None and end is not None and initial_price and initial_price > 0 and side in {"long", "short"}:
        sign = Decimal(1 if side == "long" else -1)
        path = [[at, numeric(sign * (mid / initial_price - 1) * 10000)]
                for at, mid in quotes if start <= at <= end]
    values = [dec(p[1]) for p in path]
    return {
        "kind": "observed_mid_return_from_first_fill_bps",
        "points": _sample(path), "samples": len(path), "visual_downsampled": len(path) > MAX_CHART_POINTS,
        "best_observed_bps": numeric(max(values)) if values else None,
        "worst_observed_bps": numeric(min(values)) if values else None,
        "max_observed_gap_seconds": max((b[0] - a[0] for a, b in zip(path, path[1:])), default=None),
        "limitation": "Лише записані mid-котирування від першого fill. Це рух ціни, не PnL позиції: обсяг, докупівлі, часткові виходи й витрати тут не враховані. Між спостереженнями екстремуми невідомі.",
    }


def _evidence(event):
    if event is None:
        return None
    data = event.get("data") or {}
    return {"seq": event["seq"], "at": event.get("at"), "code": event.get("code"),
            "checks": [{k: c.get(k) for k in ("feature", "op", "actual", "expected", "passed")}
                       for c in data.get("checks", data.get("exit_checks", [])) if isinstance(c, dict)],
            "reason": data.get("exit_reason", data.get("reason")),
            "local_checks_passed": data.get("local_checks_passed")}


def _bounds(bundle):
    if not isinstance(bundle, dict):
        raise ArenaError("Очікується перевірений BattleReview.")
    if (len(bundle.get("events", [])) > MAX_EVENTS or len(bundle.get("frames", [])) > MAX_QUOTES
            or len(bundle.get("participants", [])) > 24):
        raise ArenaError("Розбір перевищує обмежений обсяг; вихідні дані не обрізано.")
    # Guard before the upstream verifier renders Decimal monetary values.
    for bot in bundle.get("participants", []):
        for key in ("gross_pnl_usdt", "signed_fees_usdt", "signed_funding_usdt", "net_usdt"):
            value = bot.get("costs", {}).get(key)
            if value is not None and dec(value) is None:
                raise ArenaError("Некоректне або завелике числове значення обліку.")


def build_debrief(bundle, market_quotes=None):
    """Consume verified review, not mutable engine state; never reconstruct missing facts."""
    _bounds(bundle)
    verified = verify_bundle(bundle)
    try:
        sensitivity = {b["id"]: b for b in analyze_review(bundle)["participants"]}
        sensitivity_error = None
    except SensitivityError as error:
        sensitivity, sensitivity_error = {}, str(error)
    # R2.1.12 (MIX, plan check p. 14): a mixed season's quotes come per contract, {contract: [...]}; never merged
    series = market_quotes if isinstance(market_quotes, dict) else {None: market_quotes or []}
    by_contract, diagnostic_by_contract = {}, {}
    for contract, rows in series.items():
        quotes, diagnostic_quotes = [], []
        for raw in rows or []:
            if len(quotes) >= MAX_QUOTES:
                raise ArenaError("Забагато котирувань для одного розбору.")
            at, mid = seconds(raw.get("at")), dec(raw.get("mid"))
            if at is None or mid is None or mid <= 0:
                raise ArenaError("Нечинне котирування; відсутній рядок не замінено нулем.")
            if quotes and at < quotes[-1][0]:
                raise ArenaError("Ринкові записи не впорядковано за часом.")
            if quotes and at == quotes[-1][0]:
                if mid != quotes[-1][1]:
                    raise ArenaError("Суперечливі котирування на один момент.")
                continue
            quotes.append((at, mid))
            diagnostic_quotes.append({k: raw.get(k) for k in ("at", "bar_close_ms", "snapshot_sha256")})
        by_contract[contract], diagnostic_by_contract[contract] = quotes, diagnostic_quotes
    mixed = isinstance(market_quotes, dict)
    quotes, diagnostic_quotes = (by_contract[None], diagnostic_by_contract[None]) if not mixed else ([], [])
    events = bundle.get("events", [])
    by_seq = {e["seq"]: e for e in events}
    frames = bundle.get("frames", [])
    from .review_diagnostics import describe, cycle_timing
    diagnostics = describe(bundle, diagnostic_by_contract if mixed else diagnostic_quotes)
    every = [q for rows in diagnostic_by_contract.values() for q in rows]
    has_bar_context = any(q.get('snapshot_sha256') and q.get('bar_close_ms') is not None for q in every)
    result = {"schema": SCHEMA, "source_bundle_sha256": verified["bundle_sha256"],
              "match": {k: bundle["match"].get(k) for k in
                        ("id", "mode", "status", "started_at", "ended_at", "admission_ends_at", "execution_mode")},
              "history_complete": bundle.get("history_complete") is True,
              "market_samples": sum(len(rows) for rows in by_contract.values()), "participants": [],
              "market_gaps": diagnostics["market_gaps"] if every else bundle.get("market_gaps", diagnostics["market_gaps"]),
              "limitations": [
                  "PAPER — модельоване виконання; поточний результат відкритої позиції не є остаточним.",
                  "Частковий або відкритий бій не використовується для оголошення переможця.",
                  "Зв'язок умов із fill показано послідовністю подій; відсутні причини не вигадуються.",
                  "Цей розбір не змінює рецептів і не запускає Jev або ордери.",
              ]}
    for bot in bundle.get("participants", []):
        identity = bot["id"]
        own = [e for e in events if e.get("bot_id") == identity]
        ledger = bot.get("trade_ledger") or {}
        rows = ledger.get("rows", [])
        row_by_seq = {r["seq"]: r for r in rows}
        quality = bot.get("quality") or {}
        costs = bot.get("costs") or {}
        final = (bundle["match"].get("status") == "DONE" and not bundle["match"].get("incomplete")
                 and result["history_complete"] and costs.get("complete") is True
                 and quality.get("settlement", {}).get("status") == "SETTLED"
                 and quality.get("availability", {}).get("status") == "COMPLETE")
        curve = []
        for frame in frames:
            at = seconds(frame.get("at"))
            observation = next((b for b in frame.get("bots", []) if b.get("id") == identity), {})
            value = numeric(observation.get("pnl_usdt"))
            if at is not None and value is not None:
                curve.append([at, value])
        row = {"id": identity, "name": bot.get("name", identity), "version": bot.get("version"),
               "cost_sensitivity": sensitivity.get(identity, {"status": "UNAVAILABLE", "reason_code": sensitivity_error}),
               "recipe_sha256": bot.get("recipe_sha256"), "status": bot.get("status"),
               "phase_diagnostics": diagnostics["participants"][identity]["phase_diagnostics"] if has_bar_context else
                                    bot.get("phase_diagnostics") or diagnostics["participants"][identity]["phase_diagnostics"],
               "result_state": "FINAL" if final else "PROVISIONAL_OR_UNKNOWN",
               "costs": {k: numeric(costs.get(k)) for k in
                         ("gross_pnl_usdt", "signed_fees_usdt", "signed_funding_usdt", "net_usdt", "mark_pnl_usdt")},
               "jev_cost_usd": numeric((costs.get("jev") or {}).get("known_cost_usd")),
               "cost_source": costs.get("source"), "funding_source": costs.get("funding_source"),
               "funnel": {k: bot.get("funnel", {}).get(k) for k in
                          ("entry", "entry_filled_orders", "add_filled_orders", "close_filled_orders", "round_trips", "failed_features", "failed_features_scope")},
               "quality": {k: quality.get(k) for k in ("data_status", "gap_event_seqs", "settlement", "availability")},
               "pnl_curve": {"kind": "recorded_total_bot_mark_pnl_usdt", "points": _sample(curve),
                             "samples": len(curve), "visual_downsampled": len(curve) > MAX_CHART_POINTS},
               "trades": []}
        previous_close = 0
        assigned = set()
        for trip in ledger.get("round_trips", []):
            if type(trip.get("number")) is not int or trip["number"] < 1:
                raise ArenaError("Нечинний номер циклу.")
            entry_seq = trip["entry_seq"]
            fill_seqs = trip.get("fill_seqs", [])
            if any(seq not in row_by_seq or by_seq.get(seq, {}).get("bot_id") != identity for seq in fill_seqs):
                raise ArenaError("Відсутній власний fill для циклу.")
            fill_rows = [row_by_seq[seq] for seq in fill_seqs]
            if not fill_rows or fill_rows[0]["seq"] != entry_seq:
                raise ArenaError("Нечинна межа першого fill.")
            closed = trip.get("closed") is True
            close_seq = trip.get("close_seq") if closed else None
            last_seq = close_seq if close_seq is not None else (events[-1]["seq"] if events else entry_seq)
            if last_seq < entry_seq or closed and close_seq not in fill_seqs:
                raise ArenaError("Нечинна межа закриття циклу.")
            funding = [r for r in rows if r.get("role") == "funding" and entry_seq <= r["seq"] <= last_seq]
            assigned.update(fill_seqs)
            assigned.update(r["seq"] for r in funding)
            gross, fees, funding_sum = (_sum(fill_rows, "gross_pnl_usdt"),
                                        _sum(fill_rows, "signed_fees_usdt"), _sum(funding, "signed_funding_usdt"))
            net = gross + fees + funding_sum if all(v is not None for v in (gross, fees, funding_sum)) else None
            preceding = [e for e in own if previous_close < e["seq"] < entry_seq
                         and e.get("code") in {"entry_checks", "entry_rechecked", "entry_recheck"}]
            entry_check = preceding[-1] if preceding else None
            close_event = by_seq.get(close_seq) if close_seq is not None else None
            exits = [e for e in own if entry_seq <= e["seq"] <= last_seq
                     and e.get("code") in {"exit_trigger", "position_exit_trigger", "stop_requested"}]
            exit_check = exits[-1] if exits else None
            reason = (close_event or {}).get("data", {}).get("reason")
            if reason not in REASONS:
                reason = (exit_check or {}).get("data", {}).get("exit_reason")
            if reason not in REASONS:
                reason = "stop_requested" if exit_check and exit_check.get("code") == "stop_requested" else None
            side = by_seq.get(entry_seq, {}).get("data", {}).get("side")
            side = side if side in {"long", "short"} else "unknown"
            start, end = seconds(trip.get("entry_at")), seconds(trip.get("close_at")) if closed else None
            if end is None and not closed:
                end = max((seconds(e.get("at")) or 0 for e in own if e["seq"] <= last_seq), default=0) or None
            holds = Counter(e.get("code") for e in own if entry_seq < e["seq"] <= last_seq
                            and e.get("code") in {"position_checks", "position_hold", "hold", "hold_checks", "jev_decision", "jev_gate"})
            row["trades"].append({
                "number": trip["number"], "side": side, "closed": closed,
                "entry_at": trip.get("entry_at"), "close_at": trip.get("close_at"),
                "entry_seq": entry_seq, "close_seq": close_seq,
                "duration_seconds": end - start if start is not None and end is not None and end >= start else None,
                "duration_is_final": closed, "timing": cycle_timing(own, trip, previous_close), "entry_evidence": _evidence(entry_check),
                "exit_evidence": _evidence(exit_check), "exit_reason": reason,
                "hold_observations": dict(holds),
                "fills": [{k: r.get(k) for k in ("seq", "at", "role", "quantity", "source", "price_usdt", "position_after")} for r in fill_rows],
                "observed_gross_usdt": numeric(gross), "observed_fees_usdt": numeric(fees),
                "observed_funding_usdt": numeric(funding_sum), "observed_realized_net_usdt": numeric(net),
                "cost_allocation": "Спостережені дельти в межах циклу; пізні нарахування можуть змінити підсумок учасника. Для відкритої позиції тут немає unrealized PnL.",
                "price_path": _quote_path(by_contract.get(bot.get("instrument")) or [] if mixed else quotes, start, end,
                                          dec(fill_rows[0].get("price_usdt")), side),
            })
            previous_close = close_seq or last_seq
        row["unassigned_ledger_rows"] = len([r for r in rows if r["seq"] not in assigned])
        row["reconciliation_deltas"] = {}
        for total_key, trade_key in (("gross_pnl_usdt", "observed_gross_usdt"),
                                     ("signed_fees_usdt", "observed_fees_usdt"),
                                     ("signed_funding_usdt", "observed_funding_usdt"),
                                     ("net_usdt", "observed_realized_net_usdt")):
            total, observed = dec(costs.get(total_key)), _sum(row["trades"], trade_key)
            row["reconciliation_deltas"][total_key] = numeric(total - observed) if (
                final and all(t["closed"] for t in row["trades"])
                and total is not None and observed is not None) else None
        intents = bot.get("funnel", {}).get("order_intents")
        known_no_orders = type(intents) is int and intents == 0
        resolved = (ledger.get("missing_order_results") is False and ledger.get("unresolved") is False)
        uncertain = ledger.get("missing_order_results") is True or ledger.get("unresolved") is True
        no_entry = (ledger.get("inventory_known") is True and result["history_complete"]
                    and not ledger.get("limitations") and not uncertain and (resolved or known_no_orders)
                    and dec(ledger.get("remaining")) == 0 and dec(bot.get("position_btc")) == 0
                    and bot.get("funnel", {}).get("entry_filled_orders") == 0)
        row["entry_state"] = "ENTERED" if row["trades"] else "NO_ENTRY" if no_entry else "UNKNOWN"
        row["order_evidence"] = {k: ledger.get(k) for k in ("missing_order_results", "unresolved")}
        result["participants"].append(row)
    result["integrity"] = {"report_sha256": digest(result)}
    return result


def load_debrief(path):
    """Accept an existing battle ZIP or exact review JSON. Never extract files."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ArenaError("Файл розбору відсутній або перевищує 2 GiB.")
    if path.suffix.lower() != ".zip":
        if path.stat().st_size > MAX_JSON_BYTES:
            raise ArenaError("Review JSON перевищує 64 MiB.")
        return build_debrief(json.loads(path.read_text(encoding="utf-8-sig")))
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if (sum(i.file_size for i in infos) > MAX_ARCHIVE_BYTES
                or any(i.file_size > MAX_JSON_BYTES for i in infos if i.filename.endswith(".json"))):
            raise ArenaError("Розпакований бій перевищує межу offline розбору.")
    from .battle_export import verify_archive
    verify_archive(path)
    with zipfile.ZipFile(path) as archive:
        bundle = json.loads(archive.read("battle-review.json"))
        contracts = bundle["match"].get("instruments")  # R2.1.12 (MIX, plan check p. 18): one member per contract

        def read(member):
            quotes = []
            with archive.open(member) as source:
                for line in iter(lambda: source.readline(2 * 1024**2 + 1), b""):
                    if len(line) > 2 * 1024**2 or len(quotes) >= MAX_QUOTES:
                        raise ArenaError("Ринковий запис перевищує межу offline розбору.")
                    value = json.loads(line)
                    quotes.append({"at": value.get("capture_at", value.get("at")), "mid": value.get("mid"),
                                   "bar_close_ms": value.get("bar_close_ms"),
                                   "snapshot_sha256": value.get("source_snapshot_sha256")})
            return quotes
        report = build_debrief(bundle, {c: read("market." + c + ".jsonl") for c in contracts} if contracts else read("market.jsonl"))
    return report


def recorded_quotes(path, boundary):
    """Stream local bid/ask captures only up to the journal's observed watermark."""
    path = Path(path)
    if not path.exists():
        return []
    cutoff = seconds(boundary)
    millisecond_bucket = isinstance(boundary, str) and re.search(r"\.\d{3}(?:Z|[+-]\d{2}:\d{2})$", boundary) is not None
    if path.is_symlink() or cutoff is None or path.stat().st_size > 2 * 1024**3:
        raise ArenaError("Нечинний запис котирувань або відсутня часова межа.")
    quotes = []
    with path.open(encoding="utf-8") as source:
        for line in iter(lambda: source.readline(2 * 1024**2 + 1), ""):
            if len(line) > 2 * 1024**2 or len(quotes) >= MAX_QUOTES:
                raise ArenaError("Запис котирувань перевищує межу розбору.")
            row = json.loads(line)
            at = seconds(row.get("at"))
            if at is None:
                raise ArenaError("Час котирування невідомий.")
            if (int(Decimal(str(at))*1000) > int(Decimal(str(cutoff))*1000) if millisecond_bucket else at > cutoff):
                break
            ticker = row.get("raw", {}).get("ticker", [])
            if not isinstance(ticker, list) or len(ticker) != 1 or not isinstance(ticker[0], dict):
                raise ArenaError("Неоднозначне котирування.")
            bid, ask = dec(ticker[0].get("bidPx")), dec(ticker[0].get("askPx"))
            if bid is None or ask is None or not 0 < bid <= ask:
                raise ArenaError("Некоректний bid/ask у записі.")
            from .review_diagnostics import bar
            pin = row.get("sha256")
            quotes.append({"at": at, "mid": numeric((bid + ask) / 2), "bar_close_ms": bar(row.get("bar_close_ms")),
                           "snapshot_sha256": pin if isinstance(pin, str) and len(pin)==64 and all(c in "0123456789abcdef" for c in pin) else None,
                           "boundary_precision": "ISO_MILLISECOND_BUCKET" if millisecond_bucket else "EXACT"})
    return quotes


def check_journal_capacity(path, state):
    """Refuse an oversized UI export before restoring the full event history."""
    count = state.get("_journal_checkpoint_seq", state.get("_event_seq", len(state.get("events", []))))
    if type(count) is not int or not 0 <= count <= MAX_EVENTS:
        raise ArenaError("Забагато подій для одного HTML-розбору.")
    path, total = Path(path), 0
    segments = path.with_suffix(".segments")
    if segments.is_symlink() or path.is_symlink():
        raise ArenaError("Непідтримуване посилання на журнал.")
    paths = segments.glob("segment-????????.jsonl") if segments.is_dir() else [path] if path.is_file() else []
    for index, item in enumerate(paths):
        if index >= 256 or item.is_symlink():
            raise ArenaError("Непідтримувана структура журналу.")
        total += item.stat().st_size
        if total > MAX_JSON_BYTES:
            raise ArenaError("Журнал завеликий для одного HTML-розбору; вихідні записи збережено.")


def _fmt(value):
    value = dec(value)
    return format(value.quantize(Decimal("0.000001")), "f").rstrip("0").rstrip(".") if value else ("0" if value == 0 else "Невідомо")


def _chart(points, label):
    if len(points) < 2:
        return '<p class="muted">Для графіка недостатньо записаних спостережень.</p>'
    values = [float(p[1]) for p in points]
    left, right = min(p[0] for p in points), max(p[0] for p in points)
    low, high = min(values), max(values)
    width, height, pad = 800, 190, 24
    coords = [(pad + (p[0] - left) / max(right - left, 1) * (width - 2 * pad),
               height - pad - (float(p[1]) - low) / max(high - low, 1e-12) * (height - 2 * pad)) for p in points]
    segments = []
    for i, (x, y) in enumerate(coords):
        # A line must not imply continuous evidence across a long recording gap.
        command = "M" if i == 0 or (points[i][2] if len(points[i]) > 2 else points[i][0] - points[i-1][0] > 90) else "L"
        segments.append(f"{command}{x:.2f},{y:.2f}")
    return (f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(label, quote=True)}">'
            f'<title>{escape(label)}</title><path d="{" ".join(segments)}" fill="none" stroke="#2b6559" stroke-width="2.5"/>'
            f'<text x="24" y="16">{escape(_fmt(high))}</text><text x="24" y="187">{escape(_fmt(low))}</text></svg>')


def _checks_html(evidence):
    if not evidence:
        return '<p class="muted">Причина не записана. Не відновлюємо її заднім числом.</p>'
    rows = "".join("<tr>" + "".join(f"<td>{escape(str(c.get(k) if c.get(k) is not None else 'Невідомо'))}</td>"
                                  for k in ("feature", "actual", "op", "expected", "passed")) + "</tr>"
                   for c in evidence.get("checks", []))
    return (f'<p>Останнє записане свідчення: #{evidence["seq"]} · {escape(str(evidence.get("at")))}</p>'
            '<div class="scroll"><table><thead><tr><th>Умова</th><th>Значення</th><th>Оператор</th><th>Поріг</th><th>Виконано</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>')


def _sensitivity_html(result):
    if not result or result.get("status") == "UNAVAILABLE":
        return '<p class="muted">Для перевірки додаткових витрат недостатньо узгоджених даних про виконання.</p>'
    rows = "".join(f'<tr><td>+{escape(str(s["extra_bps_per_fill"]))} bps</td>'
                   f'<td>{escape(_fmt(s["additional_cost_usdt"]))}</td>'
                   f'<td>{escape(_fmt(s["recorded_net_after_overlay_usdt"]))}</td></tr>'
                   for s in result["scenarios"])
    state = ("Повний звірений запис" if result["status"] == "COMPLETE_FIXED_LEDGER"
             else "Лише діагностика: відкритий, неповний або невизначений облік")
    return ('<h3>Чутливість до додаткових витрат</h3>'
            f'<p>{state}. Обсяг і ціни виконань залишено незмінними.</p>'
            '<table><thead><tr><th>Додатково на кожний fill</th><th>Витрати USDT</th><th>Net за записом після витрат, USDT</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
            '<p class="muted">Комісії й funding уже враховано в базі; повторно їх не віднімаємо. Це перевірка чутливості тих самих угод, а не повторний прогін із затримкою чи іншими fills. Jev USD окремо.</p>')


def _phase_html(bot):
    phases=(bot.get("phase_diagnostics") or {}).get("phases")
    if not isinstance(phases, dict):
        return '<p class="muted">Фази не записано. Старі підсумки об’єднують різні перевірки й не є причинами лише входу.</p>'
    rows=[]
    for key,label in (("entry","Вхід"),("add","Докупівля"),("hold","Утримання"),("exit","Вихід")):
        phase=phases.get(key) or {}
        leaves=phase.get("failed_predicates") or []
        summary=", ".join(f'{c.get("feature")} {c.get("op")} {c.get("expected")}: не виконано {c.get("false_evaluations")}, невідомо {c.get("unknown_evaluations")}' for c in leaves)
        rows.append(f'<tr><td>{label}</td><td>{escape(str(phase.get("evaluations", "Не записано")))}</td>'
                    f'<td>{escape(_fmt(phase.get("unique_closed_bars")))}</td><td>{escape(summary or "Умови не записані або всі записані умови виконано")}</td></tr>')
    unknown=bool(bot.get('phase_diagnostics',{}).get('unclassified_check_event_seqs') or bot.get('phase_diagnostics',{}).get('unclassified_position_checks'))
    return ('<h3>Перевірки за фазами</h3><table><tr><th>Фаза</th><th>Перевірки</th><th>Унікальні завершені свічки</th><th>Умови</th></tr>'
            + "".join(rows) + '</table><p class="muted">Копії рішень не збільшують кількість перевірок. Невиконана умова виходу означає, що вона не спрацювала. Старий підсумок нижче об’єднує різні фази та копії подій.</p>'
            + ('<p class="muted">Для частини записаних перевірок фазу не встановлено.</p>' if unknown else ''))


def _gaps_html(report):
    def utc(q):
        at=seconds((q or {}).get('at'))
        try: return datetime.fromtimestamp(at,timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z') if at is not None else 'Не записано'
        except (ValueError, OverflowError, OSError): return 'Не записано'
    rows=[]
    names={b["id"]:b.get("name",b["id"]) for b in report.get("participants",[])}
    for g in report.get("market_gaps",[]):
        positions=", ".join(f'{names.get(p["id"],p["id"])}: '+{'OPEN':'відкрита позиція','FLAT':'без позиції'}.get(p['status'],'невідомо') for p in g.get("positions_at_gap",[]))
        kind={'market_unavailable':'Ринкові дані недоступні','data_gap':'Прогалина даних','clock_gap':'Прогалина часу','clock_skew':'Розбіжність часу'}.get(g['kind'],'Прогалина')
        whose=f' · {escape(str(g["instrument"]))}' if g.get("instrument") else ''  # R2.1.13 (X2-07)
        rows.append(f'<tr><td>#{escape(str(g["event_seq"]))} {kind}{whose}</td><td>{escape(utc(g.get("before")))}</td>'
                    f'<td>{escape(utc(g.get("recovery")))}</td><td>{escape(_fmt(g.get("known_snapshot_interval_seconds")))}</td><td>{escape(positions)}</td></tr>')
    if not rows: return ''
    return ('<section><h2>Ринкові прогалини</h2><p>Перебіг усередині невідомий. Інтервал між спостереженнями не встановлює тривалість збою. Повний журнал подій не доводить безперервність ринкових даних.</p>'
            '<table><tr><th>Подія</th><th>Останній запис до · UTC</th><th>Перший запис після · UTC</th><th>Інтервал між записами, с</th><th>Позиції на момент прогалини</th></tr>' + "".join(rows) + '</table></section>')


def render_html(report):
    """Standalone HTML with escaped text, no script, remote URLs or hidden payload."""
    if report.get("schema") != SCHEMA:
        raise ArenaError("Непідтримуваний розбір.")
    clean = {k: v for k, v in report.items() if k != "integrity"}
    if digest(clean) != report.get("integrity", {}).get("report_sha256"):
        raise ArenaError("Хеш розбору не збігається.")
    match = report["match"]
    sections = []
    for bot in report["participants"]:
        costs = bot["costs"]
        final = "Підсумок підтверджено" if bot["result_state"] == "FINAL" else "Проміжний або неповний результат"
        cost_cells = "".join(f'<div><span>{label}</span><strong>{escape(_fmt(costs.get(key)))} USDT</strong></div>'
                             for label, key in (("До комісій і funding", "gross_pnl_usdt"), ("Комісії (зі знаком)", "signed_fees_usdt"),
                                                ("Funding (зі знаком)", "signed_funding_usdt"), ("Net за обліком", "net_usdt")))
        failed = (bot.get("funnel") or {}).get("failed_features") or {}
        why_wait = ", ".join(f"{key}: {value}" for key, value in sorted(failed.items(), key=lambda x: str(x[0]))) or "Не записано"
        trades = []
        for trade in bot["trades"]:
            path = trade["price_path"]
            reason = REASONS.get(trade.get("exit_reason"), "Точна причина виходу не записана")
            timing = trade.get("timing") or {}
            fills = "".join(f'<tr><td>#{f["seq"]}</td><td>{escape(str(f.get("role")))}</td>'
                            f'<td>{escape(str(f.get("at")))}</td><td>{escape(_fmt(f.get("quantity")))}</td>'
                            f'<td>{escape(_fmt(f.get("price_usdt")))}</td><td>{escape(str(f.get("source")))}</td></tr>' for f in trade["fills"])
            trades.append(f'<details class="trade" open><summary>Цикл {escape(str(trade["number"]))} · {escape(trade["side"].upper())} · '
                          f'{"Закрито" if trade["closed"] else "Відкрито"}</summary>'
                          f'<p>{escape(str(trade["entry_at"]))} → {escape(str(trade.get("close_at") or "ще триває"))}</p>'
                          f'<p>Намір входу → виконання: {escape(_fmt(timing.get("entry_intent_to_fill_seconds")))} с · намір закриття → виконання: {escape(_fmt(timing.get("close_intent_to_fill_seconds")))} с · умова виходу → виконання: {escape(_fmt(timing.get("exit_trigger_to_fill_seconds")))} с. Показано лише спостережені часові позначки.</p>'
                          f'<h4>Умови перед входом</h4>{_checks_html(trade["entry_evidence"])}'
                          f'<h4>Утримання та вихід</h4><p>{escape(reason if trade["closed"] else "Позиція ще відкрита; остаточного виходу немає.")}</p>'
                          f'<p>Записані перевірки під час позиції: {escape(str(sum(trade["hold_observations"].values())))}.</p>'
                          f'{_checks_html(trade["exit_evidence"])}'
                          f'<h4>Рух ціни від першого входу · bps</h4>{_chart(path["points"], "Спостережений рух mid-котирування")}'
                          f'<p>Найкраще: {escape(_fmt(path["best_observed_bps"]))} bps · найгірше: {escape(_fmt(path["worst_observed_bps"]))} bps · спостережень: {path["samples"]}.</p>'
                          f'<p class="muted">{escape(path["limitation"])}</p>'
                          '<div class="scroll"><table><thead><tr><th>Подія</th><th>Дія</th><th>Час UTC</th><th>Обсяг</th><th>Ціна USDT (на біржі — cumulative average)</th><th>Одиниці / джерело</th></tr></thead>'
                          f'<tbody>{fills}</tbody></table></div>'
                          f'<p>Спостережено: gross {_fmt(trade["observed_gross_usdt"])} · комісії {_fmt(trade["observed_fees_usdt"])} · funding {_fmt(trade["observed_funding_usdt"])} · реалізований net {_fmt(trade["observed_realized_net_usdt"])} USDT.</p>'
                          f'<p class="muted">{escape(trade["cost_allocation"])}</p></details>')
        sections.append(f'<section><h2>{escape(str(bot["name"]))}</h2><p class="badge">{escape(final)}</p>'
                        f'<div class="costs">{cost_cells}</div><p>Jev окремо: {escape(_fmt(bot["jev_cost_usd"]))} USD. Джерело обліку: {escape(str(bot["cost_source"]))}.</p>'
                        f'{_sensitivity_html(bot.get("cost_sensitivity"))}'
                        f'{_phase_html(bot)}'
                        f'<h3>Записаний сукупний PnL учасника</h3>{_chart(bot["pnl_curve"]["points"], "Сукупний записаний PnL учасника в USDT")}'
                        f'<p class="muted">Графік показує наявні кадри, включно з оцінкою відкритих позицій. Проміжки понад 90 секунд розірвано.</p>'
                        f'<details><summary>Змішаний підсумок старого формату</summary><p>{escape(why_wait)}</p><p>Об’єднано різні фази та копії подій. Повторні перевірки не є незалежними сигналами.</p></details>'
                        + ("".join(trades) or f'<p>Входів у записаному журналі: {"немає" if bot["entry_state"] == "NO_ENTRY" else "невідомо"}.</p>')
                        + f'<p class="muted">Рядків обліку поза межами циклів: {bot["unassigned_ledger_rows"]}. Різниця підсумкового net і спостережених циклів: {_fmt(bot["reconciliation_deltas"]["net_usdt"])} USDT. Пізні нарахування залишаються в підсумку бою.</p></section>')
    notes = "".join(f"<li>{escape(note)}</li>" for note in report["limitations"])
    css = """body{margin:0;background:#f8f6ed;color:#183a34;font:16px system-ui,sans-serif;line-height:1.5}main{max-width:1100px;margin:auto;padding:32px 20px}h1,h2{font-family:Georgia,serif}h1{font-size:38px}section{background:#fffef9;border:1px solid #d6daca;border-radius:14px;padding:24px;margin:24px 0}.badge{display:inline-block;padding:5px 12px;background:#edf1e4;border-radius:20px}.muted{color:#646e65;font-size:14px}.costs{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.costs div{background:#f2f3e9;padding:12px;border-radius:8px}.costs span,.costs strong{display:block}.costs span{font-size:13px}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:9px;text-align:left;border-bottom:1px solid #ddd}details{margin:14px 0;padding:12px;border:1px solid #ddd;border-radius:8px}summary{cursor:pointer;font-weight:650}svg{display:block;width:100%;background:#f8faf4;border-radius:8px}svg text{font:12px system-ui;fill:#586459}h4{margin-bottom:5px}footer{overflow-wrap:anywhere}@media(max-width:700px){.costs{grid-template-columns:1fr 1fr}section{padding:14px}h1{font-size:29px}}"""
    return ('<!doctype html><html lang="uk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src data:; base-uri \'none\'; form-action \'none\'">'
            f'<title>Розбір бою · {escape(str(match["id"]))}</title><style>{css}</style></head><body><main>'
            f'<header><p>JEV / TRADING LAB · РОЗБІР</p><h1>Кожна угода має історію</h1>'
            f'<p>{escape(str(match["mode"]))} · {escape(str(match["status"]))} · {escape(str(match["id"]))}</p>'
            f'<p>Початок: {escape(str(match["started_at"]))} · завершення: {escape(str(match.get("ended_at") or "ще не підтверджено"))}. Час у UTC.</p></header>'
            + _gaps_html(report) + "".join(sections) + f'<footer><ul>{notes}</ul><p class="muted">Джерело SHA-256: {escape(report["source_bundle_sha256"])}</p></footer></main></body></html>')
