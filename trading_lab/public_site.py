"""Owner-gated public static site: receipted battles, one league table, standalone replays.

Local journals in, a new folder out. No credentials, transports, owner routes or publishing: the owner
copies the folder to static hosting after each battle's release receipt (the same gate as the spectator
export). Every link is relative, so the folder works under a sub-path such as https://example.org/arena/.
The public review DTO is a strict allowlist of its own: no recipe thresholds, request payloads,
snapshots, account or order identifiers, local paths or keys.
"""
import hashlib
import html
import json
import re
from decimal import Decimal
from pathlib import Path
import shutil

from .arena import IDENTITY
from .arena_strategies import ArenaError, fingerprint
from .battle_review import SAFE_VALUES, build_review, digest, leverage_cohort, number, verify_bundle
from .event_replay import FINANCIAL, build_packet, verify_packet
from .league import label_battles, public_identity, public_standings, score
from .match_journal import restore_events
from .match_protocol import score_policy
from .spectator_export import ASSETS, WEB_FILES, gate, receipt_template

SCHEMA = "arena-public-site/v1"
REVIEW_SCHEMA = "arena-public-review/v1"
JOURNALS = {"paper": ("arena_v4", {"PAPER"}), "exchange": ("exchange_v1", {"OKX_DEMO", "OKX_LIVE"})}
MODES = {"OKX_LIVE": "LIVE · реальні кошти на біржі OKX", "OKX_DEMO": "DEMO · тестові кошти біржі OKX",
         "PAPER": "PAPER · модельне виконання без біржі"}
SITE_FILES = WEB_FILES + ("battle-facts.js", "league-stats.js", "public-site.js", "public-site.css")
PAGES = ("spectator.html", "public-site.html")
# Native PNG originals are 1–3 MB each; the public copy serves derived WebP when Pillow is available. The
# originals in web/assets are never modified, and the page files are rewritten to the served names only.
DERIVABLE = ("coliseum-floor.png", "cash.png", "human-poses.png", "robot-poses.png", "human-portrait.png", "robot-portrait.png")
WEBP_QUALITY, WEBP_METHOD = 82, 4
JOURNAL_LIMIT, RECEIPT_LIMIT = 64 * 1024 * 1024, 8192
ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
DECIMAL = re.compile(r"-?\d{1,40}(\.\d{1,40})?")
TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,60}")
TEXT_SUFFIXES = {".html", ".js", ".css", ".json", ".txt", ".sha256", ".md"}
# Case-sensitive markers that must never appear in a public folder: owner routes and tokens, private
# files, exchange identifiers, model prompts, any transport and local paths.
LEAKS = {"panel-token": r"panel-token|X-Panel", "owner-route": r"/api/", "owner-controls": r"Керування",
         "private-file": r"LocalSettings|\.env\b", "credential": r"(?i)okx_api|api[_-]?key|secret[_-]?key|passphrase",
         "authorization": r"Authorization", "order-id": r"\bordId\b|\bclOrdId\b|\balgoId\b",
         "account-id": r"\buid\b|\bacct\b|\bsubAcct\b", "prompt": r"\bprompt\b|original_text",
         "transport": r"fetch\(|XMLHttpRequest|WebSocket|localhost|127\.0\.0\.1",
         "local-path": r"[A-Za-z]:\\{1,2}[A-Za-z0-9_-]+\\|/Users/\w|/home/\w"}
LEAK_PATTERNS = {name: re.compile(pattern) for name, pattern in LEAKS.items()}
HEX64 = re.compile(r"[a-f0-9]{64}")

FUNNEL_STAGE = ("checks", "signals", "signals_unknown", "failed_checks", "unknown_checks", "jev_decisions", "jev_calls",
                "jev_allows", "jev_holds", "jev_vetoes", "jev_outcomes_unknown", "rechecks", "recheck_vetoes")
FUNNEL_TOTALS = ("entry_filled_orders", "add_filled_orders", "close_filled_orders", "round_trips", "order_intents",
                 "flat_waits", "position_holds", "no_signal_unknown", "cumulative_fill_updates")
COST_FIELDS = ("gross_pnl_usdt", "signed_fees_usdt", "signed_funding_usdt", "net_usdt")
EVENT_NUMBERS = {"quantity_btc", "price_usdt", "fee_usdt", "gross_pnl_usdt", "remaining_btc", "position_btc", "signed_amount_usdt",
                 "signed_funding_usdt", "confidence_threshold", "bar_time_ms", "position_age_seconds", "reduce_pct", "elapsed_ms"}
EVENT_FLAGS = {"local_checks_passed", "jev_allowed", "ai_used", "simulated", "sent", "rules_allowed"}
EVENT_TOKENS = {"kind", "side", "reason", "blocked_reason", "choice", "exit_reason", "execution_mode"}
EVENT_KEYS = EVENT_NUMBERS | EVENT_FLAGS | EVENT_TOKENS | {"checks", "exit_checks", "financial", "settled", "answer", "instrument"}
MATCH_KEYS = ("id", "mode", "execution_mode", "status", "phase", "started_at", "admission_ends_at", "ended_at",
              "duration_seconds", "incomplete", "time_policy")
STATUS_WORD = re.compile(r"[A-Z_]{1,24}")
INSTRUMENT = re.compile(r"[A-Z0-9]{2,12}-USDT-SWAP")  # R2.1.12 (MIX)


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _word(value, pattern=TOKEN):
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _label(value, limit=80):
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        return None
    return value.strip()[:limit]


def _checks(values):
    """Which conditions passed, never their thresholds: the recipe stays the author's."""
    return [{"feature": _label(v.get("feature")) or "unknown", "passed": v.get("passed") if type(v.get("passed")) is bool else None}
            for v in values if isinstance(v, dict)] if isinstance(values, list) else []


def public_event_data(data):
    if not isinstance(data, dict):
        return {}
    out = {k: number(data[k]) for k in EVENT_NUMBERS if k in data}
    out.update({k: data[k] for k in EVENT_FLAGS if type(data.get(k)) is bool})
    out.update({k: data[k] for k in EVENT_TOKENS if isinstance(data.get(k), str) and data[k] in SAFE_VALUES})
    if isinstance(data.get("instrument"), str) and INSTRUMENT.fullmatch(data["instrument"]):  # R2.1.13 (X2-07): whose market, in a mixed season
        out["instrument"] = data["instrument"]
    for key in ("checks", "exit_checks"):
        if key in data:
            out[key] = _checks(data[key])
    if isinstance(data.get("financial"), dict):
        out["financial"] = {k: (_count(v) if k == "trades" else number(v)) for k, v in data["financial"].items() if k in FINANCIAL}
    if type(data.get("settled")) is bool:
        out["settled"] = data["settled"]
    if isinstance(data.get("answer"), dict):
        answer = data["answer"]
        out["answer"] = {"choice": answer.get("choice") if answer.get("choice") in SAFE_VALUES else None,
                         "confidence": number(answer.get("confidence"))}
    return out


def public_review(bundle):
    """Strict public projection of an author-equal review bundle (battle_review.build_review)."""
    verify_bundle(bundle)
    match = bundle["match"]
    participants = []
    for row in bundle["participants"]:
        funnel = row.get("funnel") if isinstance(row.get("funnel"), dict) else {}
        public_funnel = {stage: {k: _count((funnel.get(stage) or {}).get(k)) for k in FUNNEL_STAGE} for stage in ("entry", "add", "hold")}
        public_funnel.update({k: _count(funnel.get(k)) for k in FUNNEL_TOTALS})
        failed = funnel.get("failed_features") if isinstance(funnel.get("failed_features"), dict) else {}
        public_funnel["failed_features"] = {_label(k) or "unknown": v for k, v in failed.items() if type(v) is int and v >= 0}
        costs = row.get("costs") if isinstance(row.get("costs"), dict) else {}
        jev = costs.get("jev") if isinstance(costs.get("jev"), dict) else {}
        public_costs = {k: number(costs.get(k)) for k in COST_FIELDS}
        public_costs.update(complete=costs.get("complete") is True, source=_word(costs.get("source")),
                            jev={"known_cost_usd": number(jev.get("known_cost_usd")), "unknown_billing_requests": _count(jev.get("unknown_billing_requests")),
                                 "requests": _count(jev.get("requests")), "complete": jev.get("complete") is True})
        quality = row.get("quality") if isinstance(row.get("quality"), dict) else {}
        public_quality = {"data_status": _word(quality.get("data_status"), STATUS_WORD), "complete_history": quality.get("complete_history") is True,
                          "distinct_closed_bars": _count(quality.get("distinct_closed_bars"))}
        for key in ("settlement", "availability", "competitive_validity"):
            public_quality[key] = {"status": _word((quality.get(key) or {}).get("status"), STATUS_WORD)}
        version = row.get("version")
        participants.append({"id": row["id"], "name": _label(row.get("name")), "version": version if type(version) is int else _label(version, 40),
                             "recipe_sha256": _word(row.get("recipe_sha256"), HEX64),
                             "status": _word(row.get("status"), STATUS_WORD), "position_btc": number(row.get("position_btc")),
                             "allocation_usdt": number(row.get("allocation_usdt")), "funnel": public_funnel, "costs": public_costs, "quality": public_quality,
                             **({"instrument": row["instrument"]} if isinstance(row.get("instrument"), str) and INSTRUMENT.fullmatch(row["instrument"]) else {})})
    events = [{"seq": e["seq"], "at": e.get("at") if isinstance(e.get("at"), str) else None, "bot_id": e.get("bot_id") if isinstance(e.get("bot_id"), str) else None,
               "step": _count(e.get("step")), "code": _word(e.get("code")) or "unknown", "data": public_event_data(e.get("data"))}
              for e in bundle["events"]]
    dto = {"schema": REVIEW_SCHEMA, "projection": "public_v1_allowlist", "source_bundle_sha256": bundle["integrity"]["bundle_sha256"],
           "match": {k: match.get(k) for k in MATCH_KEYS}, "participants": participants, "events": events,
           "cash": {"name": "Cash", "net_usdt": "0"}, "history_complete": bundle.get("history_complete") is True,
           "limitations": ["Jev USD is separate from trading USDT. PAPER is a model. A missing result isn't zero.",
                           "Public projection: recipe thresholds, request payloads, snapshots, account and order identifiers are not exported."]}
    if "score_policy" in match:
        policy = score_policy(match)
        if policy is None:
            raise ArenaError("Невідоме правило перемоги; публічний підсумок не сформовано.")
        dto["match"]["score_policy"] = policy
    if isinstance(match.get("instruments"), list) and all(isinstance(c, str) and INSTRUMENT.fullmatch(c) for c in match["instruments"]):
        dto["match"]["instruments"] = list(match["instruments"])  # R2.1.12 (MIX): a mixed battle's contracts
    dto["integrity"] = {"review_sha256": digest(dto), "source_event_count": len(events)}
    return dto


def verify_public_review(dto):
    """Typed, closed-world check of the public DTO; unknown fields are not a supported public projection."""
    def reject():
        raise ArenaError("Нечинний публічний review DTO.")
    def keys(value, allowed, required=None):
        if not isinstance(value, dict) or set(value) - set(allowed) or set(allowed if required is None else required) - set(value):
            reject()
    def optional(value, check):
        if value is not None and not check(value):
            reject()
    decimal = lambda v: isinstance(v, str) and bool(DECIMAL.fullmatch(v))
    count = lambda v: type(v) is int and 0 <= v <= 9007199254740991
    word = lambda v: isinstance(v, str) and bool(STATUS_WORD.fullmatch(v))
    text = lambda limit: (lambda v: isinstance(v, str) and 1 <= len(v) <= limit and not any(ord(c) < 32 for c in v))
    keys(dto, {"schema", "projection", "source_bundle_sha256", "match", "participants", "events", "cash", "history_complete", "limitations", "integrity"})
    if dto["schema"] != REVIEW_SCHEMA or dto["projection"] != "public_v1_allowlist" or type(dto["history_complete"]) is not bool:
        reject()
    if not isinstance(dto["source_bundle_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", dto["source_bundle_sha256"]):
        reject()
    keys(dto["integrity"], {"review_sha256", "source_event_count"})
    original = {k: v for k, v in dto.items() if k != "integrity"}
    try:
        if digest(original) != dto["integrity"]["review_sha256"]:
            raise ArenaError("Хеш публічного review не збігається.")
    except (TypeError, ValueError):
        reject()
    keys(dto["match"], set(MATCH_KEYS) | {"score_policy", "instruments"}, MATCH_KEYS)
    optional(dto["match"].get("instruments"), lambda v: isinstance(v, list) and len(v) >= 2 and v == sorted(set(v))
             and all(isinstance(c, str) and INSTRUMENT.fullmatch(c) for c in v))
    if "score_policy" in dto["match"] and score_policy(dto["match"]) is None:
        reject()
    if not isinstance(dto["match"]["id"], str) or not ALIAS.fullmatch(dto["match"]["id"]) or dto["match"]["mode"] not in MODES:
        reject()
    for key in ("execution_mode", "status", "phase", "time_policy"):
        optional(dto["match"][key], lambda v: isinstance(v, str) and bool(TOKEN.fullmatch(v)))
    for key in ("started_at", "admission_ends_at", "ended_at"):
        optional(dto["match"][key], text(40))
    optional(dto["match"]["duration_seconds"], lambda v: type(v) in {int, float} and not isinstance(v, bool) and v >= 0)
    optional(dto["match"]["incomplete"], lambda v: type(v) is bool)
    keys(dto["cash"], {"name", "net_usdt"})
    if dto["cash"] != {"name": "Cash", "net_usdt": "0"} or not isinstance(dto["limitations"], list) or not all(map(text(240), dto["limitations"])):
        reject()
    if not isinstance(dto["participants"], list) or not isinstance(dto["events"], list):
        reject()
    ids = set()
    for row in dto["participants"]:
        keys(row, {"id", "name", "version", "recipe_sha256", "status", "position_btc", "allocation_usdt", "funnel", "costs", "quality", "instrument"},
             {"id", "name", "version", "recipe_sha256", "status", "position_btc", "allocation_usdt", "funnel", "costs", "quality"})
        optional(row.get("instrument"), lambda v: isinstance(v, str) and bool(INSTRUMENT.fullmatch(v))
                 and v in (dto["match"].get("instruments") or []))
        if not isinstance(row["id"], str) or not ALIAS.fullmatch(row["id"]) or row["id"] in ids:
            reject()
        ids.add(row["id"])
        optional(row["name"], text(80))
        optional(row["version"], lambda v: type(v) is int or text(40)(v))
        optional(row["recipe_sha256"], lambda v: isinstance(v, str) and bool(re.fullmatch(r"[a-f0-9]{64}", v)))
        optional(row["status"], word)
        optional(row["position_btc"], decimal); optional(row["allocation_usdt"], decimal)
        keys(row["funnel"], set(FUNNEL_TOTALS) | {"entry", "add", "hold", "failed_features"})
        for stage in ("entry", "add", "hold"):
            keys(row["funnel"][stage], FUNNEL_STAGE)
            for value in row["funnel"][stage].values():
                optional(value, count)
        for key in FUNNEL_TOTALS:
            optional(row["funnel"][key], count)
        if not isinstance(row["funnel"]["failed_features"], dict) or not all(text(80)(k) and count(v) for k, v in row["funnel"]["failed_features"].items()):
            reject()
        keys(row["costs"], set(COST_FIELDS) | {"complete", "source", "jev"})
        for key in COST_FIELDS:
            optional(row["costs"][key], decimal)
        if type(row["costs"]["complete"]) is not bool:
            reject()
        optional(row["costs"]["source"], lambda v: isinstance(v, str) and bool(TOKEN.fullmatch(v)))
        keys(row["costs"]["jev"], {"known_cost_usd", "unknown_billing_requests", "requests", "complete"})
        optional(row["costs"]["jev"]["known_cost_usd"], decimal)
        optional(row["costs"]["jev"]["unknown_billing_requests"], count); optional(row["costs"]["jev"]["requests"], count)
        if type(row["costs"]["jev"]["complete"]) is not bool:
            reject()
        keys(row["quality"], {"data_status", "complete_history", "distinct_closed_bars", "settlement", "availability", "competitive_validity"})
        optional(row["quality"]["data_status"], word); optional(row["quality"]["distinct_closed_bars"], count)
        if type(row["quality"]["complete_history"]) is not bool:
            reject()
        for key in ("settlement", "availability", "competitive_validity"):
            keys(row["quality"][key], {"status"})
            optional(row["quality"][key]["status"], word)
    for seq, event in enumerate(dto["events"], 1):
        keys(event, {"seq", "at", "bot_id", "step", "code", "data"})
        if type(event["seq"]) is not int or event["seq"] != seq:
            reject()
        optional(event["at"], text(40))
        optional(event["bot_id"], lambda v: isinstance(v, str) and (v in ids or v == "cash"))  # Cash has events but no participant row
        optional(event["step"], count)
        if not isinstance(event["code"], str) or not TOKEN.fullmatch(event["code"]):
            reject()
        data = event["data"]
        keys(data, EVENT_KEYS, ())
        for key, value in data.items():
            if key in EVENT_NUMBERS:
                optional(value, decimal)
            elif key in EVENT_FLAGS or key == "settled":
                if type(value) is not bool:
                    reject()
            elif key in EVENT_TOKENS:
                if value not in SAFE_VALUES:
                    reject()
            elif key == "instrument":
                if not isinstance(value, str) or not INSTRUMENT.fullmatch(value) or value not in dto["match"].get("instruments", ()):
                    reject()
            elif key in {"checks", "exit_checks"}:
                if not isinstance(value, list):
                    reject()
                for check in value:
                    keys(check, {"feature", "passed"})
                    if not text(80)(check["feature"]):
                        reject()
                    optional(check["passed"], lambda v: type(v) is bool)
            elif key == "financial":
                keys(value, FINANCIAL, ())
                for name, amount in value.items():
                    optional(amount, count if name == "trades" else decimal)
            elif key == "answer":
                keys(value, {"choice", "confidence"})
                optional(value["choice"], lambda v: v in SAFE_VALUES); optional(value["confidence"], decimal)
    if dto["integrity"]["source_event_count"] != len(dto["events"]):
        reject()
    return {"verified": True, "review_sha256": dto["integrity"]["review_sha256"], "events": len(dto["events"]), "participants": len(ids)}


def load_journal(root, kind, identity):
    """Read one finished-or-not journal file as the panel does; never the catalogue, lock or private profiles."""
    if kind not in JOURNALS or not isinstance(identity, str) or not IDENTITY.fullmatch(identity):
        raise ArenaError("Бій не знайдено.")
    folder, modes = JOURNALS[kind]
    base = Path(root).resolve() / "runtime" / folder
    path = base / (identity + ".json")
    if path.is_symlink() or not path.is_file() or path.stat().st_size > JOURNAL_LIMIT:
        raise ArenaError("Журнал бою відсутній або не придатний для читання.")
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or state.get("id") != identity or state.get("mode") not in modes:
        raise ArenaError("Журнал не відповідає запитаному бою.")
    restore_events(base / (identity + ".events.jsonl"), state, full=True)
    return state


def receipt_for(state):
    return receipt_template(build_packet(state))


def cohort_of(state):
    # The same grouping key as League.status() in trading_lab.league: battles compare only inside one regime.
    return fingerprint({"mode": state["mode"], "allocation": state["budget_usdt"], "duration": state["duration_seconds"], "code": state["code_sha256"],
                        "recipes": sorted(r["recipe_sha256"] for r in state["strategies"]), "execution": state.get("execution_mode"),
                        "fixture_only": state.get("fixture_only", False),
                        "source_kind": state.get("source_kind", (state.get("match_manifest") or {}).get("source_kind")),
                        "evaluation": (state.get("match_manifest") or {}).get("evaluation"), "score_policy": score_policy(state),
                        "evidence_kind": state.get("evidence_kind", "MODEL" if state["mode"] == "PAPER" else "ACTUAL"),
                        **leverage_cohort(state)})


def battle_row(kind, state, bundle, packet, receipt):
    """One public league row: the League.status() projection plus the replay hash and release time."""
    from .league import examples_of, instruments_mark
    scored = score(bundle, examples=examples_of(state))  # R2.1.11 (E): example participants never count
    passports = {s["id"]: s for s in state["strategies"]}
    identity = state["id"]
    return {"id": identity, "kind": kind, "label": state["created_at"], "mode": state["mode"], "status": state["status"], "cohort": cohort_of(state),
            **({"cohort_mark": instruments_mark(state)} if instruments_mark(state) else {}),  # R2.1.12 (MIX)
            "allocation_usdt": number(state["budget_usdt"]), "score": scored, "created_at": state["created_at"],
            "started_at": state.get("started_at", state["created_at"]), "ended_at": state.get("finished_at"),
            "duration_seconds": state["duration_seconds"], "execution_mode": state.get("execution_mode", "rules"),
            "score_policy": score_policy(state),
            "competitive_validity": "ELIGIBLE" if scored["status"] in {"SOLE_WIN", "TIE_AT_TOP", "NO_CONTEST"} else "INELIGIBLE",
            "settlement": "SETTLED" if state["status"] == "DONE" and all(p["costs"].get("complete") for p in bundle["participants"]) else "PENDING",
            "incomplete": state.get("incomplete") is True, "history_complete": bundle["history_complete"],
            "bundle_hash": bundle["integrity"]["bundle_sha256"], "package_hash": packet["package_hash"], "evidence_kind": packet["evidence_kind"],
            "participants": [{"id": p["id"], "recipe_sha256": p.get("recipe_sha256") or "unknown", "version": p["version"],
                              **public_identity(passports.get(p["id"]))} for p in bundle["participants"]],
            "release_after_ms": receipt["release_after_ms"], "page": f"battle-{identity}.html",
            "replay": f"battles/{identity}/replay.json", "review": f"battles/{identity}/review.json"}


def battle_item(kind, state, receipt, now_ms=None):
    """Gate one battle (integrity, privacy, rights, owner release, time) and project everything the site needs."""
    bundle = build_review(state)
    packet = build_packet(state)
    proof = gate(packet, receipt, now_ms)
    return {"id": state["id"], "kind": kind, "mode": state["mode"], "packet": packet, "receipt": receipt,
            "review": public_review(bundle), "row": battle_row(kind, state, bundle, packet, receipt), "proof": proof}


def read_receipt(receipts, identity):
    path = Path(receipts) / (identity + ".json")
    if path.is_symlink() or not path.is_file() or path.stat().st_size > RECEIPT_LIMIT:
        raise ArenaError("Немає receipt для цього бою.")
    receipt = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(receipt, dict):
        raise ArenaError("Receipt має бути JSON-об'єктом.")
    return receipt


def collect(root, battles, receipts, now_ms=None):
    """Battles with an approved receipt become items; every other one is reported, never silently dropped."""
    items, skipped, seen = [], [], set()
    for kind, identity in battles:
        if (kind, identity) in seen:
            continue
        seen.add((kind, identity))
        try:
            state = load_journal(root, kind, identity)
            items.append(battle_item(kind, state, read_receipt(receipts, identity), now_ms))
        except ArenaError as error:
            skipped.append({"kind": kind, "id": identity, "reason": str(error)})
        except (OSError, ValueError, KeyError, TypeError):
            skipped.append({"kind": kind, "id": identity, "reason": "Журнал або receipt не вдалося прочитати."})
    return {"items": items, "skipped": skipped}


def league_projection(items):
    """Public standings of the receipted battles only; newest battle first."""
    rows = label_battles([item["row"] for item in items])
    rows.sort(key=lambda r: (str(r.get("created_at") or ""), r["id"]), reverse=True)
    frequencies, cohorts = public_standings(rows)
    return {"schema": SCHEMA, "battles": rows, "frequencies": frequencies, "cohorts": cohorts,
            "policy_note": "Окремі групи за кодом, режимом, складом, ставкою, вікном входів і правилом перемоги. Нові бої: найбільший net USDT "
                           "серед стратегій із виконаним входом; Cash — еталон, без входів немає переможця. Історичний регламент з Cash збережено. "
                           "Нічия ділить перше місце. Суми включають бої без входів і є описовими: сусідні бої не є незалежними доказами. "
                           "Неповні результати збережено, але не дають переможця."}


def facts_projection(review):
    """What battle-facts.js reads for the headline: participants, match and the Jev decisions; not the whole event log."""
    return {"match": review["match"], "participants": review["participants"], "cash": review["cash"], "history_complete": review["history_complete"],
            "events": [e for e in review["events"] if e["code"] == "jev_decision"], "source": "battles/" + review["match"]["id"] + "/review.json"}


def _json(value, indent=None):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=indent, separators=(",", ":") if indent is None else None)


def _embed(value):
    # Non-executable JSON inside <script type="application/json">: escape closing tags and all markup.
    return _json(value).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _replace_once(page, marker, replacement):
    if page.count(marker) != 1:
        raise ArenaError("Шаблон сторінки змінився; збірку зупинено.")
    return page.replace(marker, replacement)


def battle_page(template, item, label, indexable):
    row = item["row"]
    page = _replace_once(template, "__SPECTATOR_DATA__", _embed({"packet": item["packet"], "receipt": item["receipt"]}))
    page = _replace_once(page, "<title>Колізей стратегій · повтор бою</title>", f"<title>{html.escape(label)} · Арена стратегій</title>")
    page = _replace_once(page, '<link rel="stylesheet" href="review.css">', '<link rel="stylesheet" href="review.css"><link rel="stylesheet" href="public-site.css">')
    page = _replace_once(page, "<h1>Колізей стратегій</h1>",
                         '<h1>Колізей стратегій</h1><p class="public-nav"><a href="index.html">← Ліга та всі бої</a></p>'
                         f'<p class="public-mode" data-mode="{html.escape(row["mode"])}">{html.escape(MODES[row["mode"]])} · {html.escape(label)}</p>')
    if not indexable:
        page = _replace_once(page, '<meta charset="utf-8">', '<meta charset="utf-8"><meta name="robots" content="noindex, nofollow">')
    return page


def leak_scan(destination):
    destination = Path(destination)
    findings, files = [], 0
    for path in sorted(p for p in destination.rglob("*") if p.is_file()):
        files += 1
        if path.suffix not in TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for name, pattern in LEAK_PATTERNS.items():
            if pattern.search(text):
                findings.append({"file": path.relative_to(destination).as_posix(), "marker": name})
    return {"clean": not findings, "findings": findings[:50], "files": files}


def derive_assets(web, destination, enabled=True):
    """Copy the native assets, deriving WebP for the large PNGs when Pillow is importable; returns the served names and sizes."""
    try:
        from PIL import Image
    except ImportError:  # stdlib-only machine: originals are served as they are
        Image = None
    (destination / "assets").mkdir()
    served, report = {}, []
    for name in ASSETS:
        source = web / "assets" / name
        target_name = name
        if enabled and Image is not None and name in DERIVABLE:
            target_name = name[:-4] + ".webp"
            with Image.open(source) as image:
                image.convert("RGBA").save(destination / "assets" / target_name, "WEBP", quality=WEBP_QUALITY, method=WEBP_METHOD)
        else:
            shutil.copyfile(source, destination / "assets" / target_name)
        served[name] = target_name
        report.append({"source": name, "served": target_name, "bytes_source": source.stat().st_size,
                       "bytes_served": (destination / "assets" / target_name).stat().st_size})
    return {"derived": any(row["served"] != row["source"] for row in report), "files": report,
            "bytes_source": sum(r["bytes_source"] for r in report), "bytes_served": sum(r["bytes_served"] for r in report)}, served


def site_text(text, served):
    text = text.replace("/assets/", "assets/")
    for source, target in served.items():
        if source != target:
            text = text.replace("assets/" + source, "assets/" + target)
    return text


def write_manifest(destination):
    lines = []
    for path in sorted(p for p in Path(destination).rglob("*") if p.is_file()):
        relative = path.relative_to(destination).as_posix()
        if relative != "manifest.sha256":
            lines.append(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + relative)
    text = "\n".join(lines) + "\n"
    _write(Path(destination) / "manifest.sha256", text)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build(destination, web, items, *, indexable=False, now_ms=None, derive=True):
    """Write a new folder: shared web files, one standalone page per battle, the league index and a manifest."""
    if not items:
        raise ArenaError("Немає боїв із чинним receipt; тека не створена.")
    destination, web = Path(destination).absolute(), Path(web).resolve()
    if destination.exists() or any(p.is_symlink() for p in (destination, *destination.parents)):
        raise ArenaError("Збірка потребує нової окремої теки без symlink.")
    for name in (*SITE_FILES, *PAGES, *("assets/" + n for n in ASSETS)):
        source = web / name
        if not source.is_file() or source.is_symlink():
            raise ArenaError("Не вистачає ресурсу сайту: " + name)
    ids = [item["id"] for item in items]
    if len(set(ids)) != len(ids):
        raise ArenaError("Бій повторюється у збірці.")
    for item in items:
        gate(item["packet"], item["receipt"], now_ms)
        verify_public_review(item["review"])
    league = league_projection(items)
    labels = {row["id"]: row.get("display_label") or row["id"] for row in league["battles"]}
    built_at = max(item["receipt"]["release_after_ms"] for item in items)
    newest = next((row for row in league["battles"] if row["status"] == "DONE"), league["battles"][0])
    latest_item = next(item for item in items if item["id"] == newest["id"])
    battles = {"schema": SCHEMA, "built_at_ms": built_at, "indexable": indexable, "modes": MODES, "league": league,
               "latest": {"id": newest["id"], "display_label": labels[newest["id"]], "mode": newest["mode"], "status": newest["status"],
                          "created_at": newest["created_at"], "page": newest["page"], "score": {k: newest["score"].get(k) for k in ("status", "sole_winner", "shared_top")}}}
    destination.mkdir(parents=True)
    try:
        assets, served = derive_assets(web, destination, derive)
        for name in SITE_FILES:
            _write(destination / name, site_text((web / name).read_text(encoding="utf-8"), served))
        spectator = (web / "spectator.html").read_text(encoding="utf-8")
        for item in items:
            folder = destination / "battles" / item["id"]
            _write(folder / "replay.json", _json(item["packet"]))
            _write(folder / "review.json", _json(item["review"]))
            _write(folder / "release.json", _json(item["receipt"], indent=2))
            _write(destination / item["row"]["page"], battle_page(spectator, item, labels[item["id"]], indexable))
        _write(destination / "battles.json", _json(battles, indent=1))
        _write(destination / "latest.json", _json({"schema": SCHEMA, "built_at_ms": built_at, "battle": battles["latest"]}, indent=2))
        _write(destination / "release.json", _json({"schema": SCHEMA, "built_at_ms": built_at, "indexable": indexable, "public_hosting": False,
                                                    "assets": assets,
                                                    "battles": [{"id": i["id"], "mode": i["mode"], "package_hash": i["packet"]["package_hash"],
                                                                 "release_after_ms": i["receipt"]["release_after_ms"]} for i in items]}, indent=2))
        index = (web / "public-site.html").read_text(encoding="utf-8")
        index = _replace_once(index, "__PUBLIC_SITE_DATA__", _embed({"battles": battles, "latest": {"id": newest["id"], "facts": facts_projection(latest_item["review"])}}))
        index = _replace_once(index, "__ROBOTS__", "" if indexable else '<meta name="robots" content="noindex, nofollow">')
        _write(destination / "index.html", index)
    except ArenaError:
        raise
    except Exception:
        # Keep the incomplete folder for inspection; it has no manifest, so verify_site refuses it.
        raise ArenaError("Збірку не завершено; не публікуй цю теку.")
    scan = leak_scan(destination)
    if not scan["clean"]:
        raise ArenaError("Знайдено приватні маркери: " + ", ".join(sorted({f["marker"] for f in scan["findings"]})) + "; теку не публікувати.")
    manifest = write_manifest(destination)
    return {"allowed": True, "public_hosting": False, "battles": len(items), "indexable": indexable, "built_at_ms": built_at,
            "index": str(destination / "index.html"), "manifest_sha256": manifest, "files": scan["files"] + 1,
            "assets": {"derived": assets["derived"], "bytes_source": assets["bytes_source"], "bytes_served": assets["bytes_served"]}}


def verify_site(destination, now_ms=None):
    """Re-check a built folder before upload: manifest, receipts, replay and review DTOs, no private markers."""
    destination = Path(destination)
    manifest = destination / "manifest.sha256"
    if not manifest.is_file():
        raise ArenaError("Немає manifest.sha256; збірка не завершена.")
    text = manifest.read_text(encoding="utf-8")
    listed = {}
    for line in text.splitlines():
        sha, separator, relative = line.partition("  ")
        if not separator or not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise ArenaError("Пошкоджений manifest.sha256.")
        listed[relative] = sha
    actual = {p.relative_to(destination).as_posix(): p for p in destination.rglob("*") if p.is_file() and p.name != "manifest.sha256"}
    if set(actual) != set(listed):
        raise ArenaError("Склад теки не збігається з manifest.sha256.")
    for relative, path in actual.items():
        if hashlib.sha256(path.read_bytes()).hexdigest() != listed[relative]:
            raise ArenaError("Хеш файла не збігається: " + relative)
    scan = leak_scan(destination)
    if not scan["clean"]:
        raise ArenaError("Знайдено приватні маркери: " + ", ".join(sorted({f["marker"] for f in scan["findings"]})))
    battles = 0
    folders = sorted(p for p in (destination / "battles").iterdir() if p.is_dir()) if (destination / "battles").is_dir() else []
    for folder in folders:
        packet = json.loads((folder / "replay.json").read_text(encoding="utf-8"))
        verify_packet(packet)
        gate(packet, json.loads((folder / "release.json").read_text(encoding="utf-8")), now_ms)
        review = json.loads((folder / "review.json").read_text(encoding="utf-8"))
        verify_public_review(review)
        if folder.name != packet["manifest"]["id"] or review["match"]["id"] != folder.name or not (destination / f"battle-{folder.name}.html").is_file():
            raise ArenaError("Тека бою не відповідає його запису.")
        battles += 1
    if not battles:
        raise ArenaError("У збірці немає жодного бою.")
    return {"verified": True, "battles": battles, "files": len(actual) + 1, "manifest_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "public_hosting": False}
