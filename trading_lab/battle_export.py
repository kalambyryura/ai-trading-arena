"""One private, credential-free battle archive for owner/author analysis; no execution."""
from datetime import datetime, timezone
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
from pathlib import Path
import tempfile
import zipfile

from .arena_strategies import ArenaError, fingerprint, validate_strategy
from .battle_review import build_review, verify_bundle
from .event_replay import build_packet, verify_packet
from .strategy_contract import payload_of

SCHEMA = "jev-arena-battle-file/v1"
MAX_BYTES = 512 * 1024**2
PILOT_MAX_BYTES = 2 * 1024**3
MEMBERS = {"manifest.json", "battle-review.json", "battle-replay.json", "strategies.json",
           "match-settings.json", "market.jsonl", "README_UA.txt"}
# R2.1.10 (H): the manifest's own SHA-256 and size. ZIPs exported before R2.1.10 have none and still verify.
RECEIPT = "manifest-receipt.json"
RECEIPT_SCHEMA = "jev-arena-manifest-receipt/v1"
LAYOUTS = (MEMBERS, MEMBERS | {RECEIPT})
EXPORTER_FILES = ("battle_export.py", "battle_review.py", "event_replay.py", "cycle_diagnostics.py",
                  "g1_public_evidence.py", "battle_debrief.py", "review_diagnostics.py")  # R2.1.11 (R2110-HASH-COVER)
HEX64 = re.compile(r"[0-9a-f]{64}")
MARKET_MEMBER = re.compile(r"market\.([A-Z0-9]{2,12}-USDT-SWAP)\.jsonl")  # R2.1.12 (MIX): one per contract of a mixed season


def exporter_hash(folder=None):
    """R2.1.10 (H): sha256 over the exporter's sources, only CRLF -> LF normalised like the core hashes (D098)."""
    folder = Path(folder or Path(__file__).parent)
    return hashlib.sha256(b"".join((folder / name).read_bytes().replace(b"\r\n", b"\n") for name in EXPORTER_FILES)).hexdigest()


def code_hashes(state):
    """The code that wrote the journal and the code of this process that exported it."""
    from .arena import LOADED_SOURCE_HASH
    from .exchange_lab import LOADED_HASH
    from .strategy_training import LOADED_CODE_HASH
    journal = state.get("code_sha256")
    return {"journal": journal if isinstance(journal, str) and HEX64.fullmatch(journal) else None,
            "exporter": exporter_hash(), "arena": LOADED_SOURCE_HASH, "exchange": LOADED_HASH, "training": LOADED_CODE_HASH}

MARKET_FIELDS = frozenset('''at server_time quote_ts bar_close_ms bid ask mid bid_btc ask_btc
    lot_btc min_btc features funding_rate funding_at funding_time_ms funding_status
    funding_feature_provenance funding_events funding_accounting sha256 candles
    features_rebuilt_from_recorded_public source_snapshot_sha256 capture_at source
    htf htf_context m1_snapshot_sha256 rejected_htf_source_sha256 instrument'''.split())


def verify_market_row(row):
    """Public field allowlist and self-contained HTF validation; no transport."""
    from .causal_htf import FEATURES, digest, g1_view, unpack_context
    from .strategy_v2 import MARKET_FEATURES
    if (not isinstance(row, dict) or set(row) - MARKET_FIELDS
            or row.get('source') not in {'RECORDED_PUBLIC', 'SYNTHETIC'}):
        raise ArenaError('public_market_fields')
    features = row.get('features')
    if (not isinstance(features, dict) or set(features) - (set(MARKET_FEATURES) | set(FEATURES))
            or any(v is not None and (type(v) not in (int, float) or not math.isfinite(v))
                   for v in features.values())):
        raise ArenaError('public_market_features')
    extension = set(row) & {'htf', 'htf_context', 'm1_snapshot_sha256', 'rejected_htf_source_sha256'}
    if not extension:
        if set(FEATURES) & set(features):
            raise ArenaError('public_htf_incomplete')
        return True  # Existing M1 archives have no optional HTF extension.
    if not {'htf', 'htf_context', 'm1_snapshot_sha256'} <= extension:
        raise ArenaError('public_htf_incomplete')
    for key in ('at', 'server_time'):
        if type(row.get(key)) not in (int, float) or not math.isfinite(row[key]) or row[key] < 0:
            raise ArenaError('public_htf_asof')
    if ('capture_at' in row and (type(row['capture_at']) not in (int, float)
                                 or row['capture_at'] != row['at'])):
        raise ArenaError('public_htf_asof')
    for key in ('m1_snapshot_sha256', 'rejected_htf_source_sha256'):
        if key == 'rejected_htf_source_sha256' and row.get(key) is None:
            continue
        if not isinstance(row.get(key), str) or not re.fullmatch('[a-f0-9]{64}', row[key]):
            raise ArenaError('public_htf_hash')
    subtree = row['htf_context']
    g1_view({**row, 'raw': {'htf_context': subtree}})  # Bounds/decode/schema/clock/causality.
    view = subtree['view']
    if (json.dumps(row['htf'], sort_keys=True, separators=(',', ':'), allow_nan=False)
            != json.dumps(view, sort_keys=True, separators=(',', ':'), allow_nan=False)
            or any(key not in features or features[key] != view['features'][key] for key in FEATURES)):
        raise ArenaError('public_htf_projection_mismatch')
    # UNKNOWN still must not carry an unrelated opaque context envelope.
    if (view['status'] == 'UNKNOWN' and subtree['context'] is not None
            and digest(unpack_context(subtree['context'])) != view['context_sha256']):
        raise ArenaError('public_htf_context_hash')
    if 'rejected_htf_source_sha256' in row and view['reason'] != 'RECORDED_CONTEXT_INVALID':
        raise ArenaError('public_htf_rejection_provenance')
    return True


def match_settings(state):
    """Only public launch facts. Never copy arbitrary account/transport state."""
    from .arena_engine import RULES as paper_rules
    from .exchange_engine import LIMITS as exchange_rules
    from .match_protocol import capacities, score_policy
    source = state.get("match_manifest") or {}
    risk_keys = set(paper_rules) | set(exchange_rules) | set(capacities(300, []))
    risk_keys.update({"snapshot_line_bytes", "snapshot_rows"})
    risk = {}
    for key, value in (source.get("risk_and_execution") or state.get("rules") or {}).items():
        if key not in risk_keys:
            continue
        if value is None or type(value) in {int, bool} or type(value) is float and math.isfinite(value):
            risk[key] = value
        elif isinstance(value, str) and value == (paper_rules.get(key) or exchange_rules.get(key)):
            risk[key] = value
        elif key == "resource_policy" and value == "rolling_health_blocks_admission_only":
            risk[key] = value
    budget_keys = {"season_attempts", "total_attempts", "season_soft_usd", "total_soft_usd",
                   "maximum_answer_age_seconds", "request_headroom_usd"}
    budget = source.get("jev_budget")
    safe_budget = {k: v for k, v in budget.items() if k in budget_keys and type(v) in {int, float}
                   and math.isfinite(v)} if isinstance(budget, dict) else None
    from .battle_review import leverage_of
    leveraged = isinstance(source.get("leverage"), dict)
    if leveraged:
        risk.pop("leverage", None)  # R2.1.15: the season's 1x default is not what its bots trade; each participant names its own
    return {"schema": "jev-arena-match-settings/v1", "projection": "known_public_fields",
            **({"leverage_table_sha256": source["leverage"].get("table_sha256"),
                "stop_notional_budget_pct": source["leverage"].get("stop_notional_budget_pct")} if leveraged else {}),
            "id": state["id"], "mode": state["mode"], "execution_mode": state.get("execution_mode", "rules"),
            "budget_usdt_per_bot": state.get("budget_usdt"), "protocol_version": state.get("protocol_version"),
            "duration_seconds": state.get("duration_seconds"), "time_policy": state.get("time_policy"),
            "clock_policy": state.get("clock_policy"),
            "started_at": state.get("started_at", state.get("created_at")),
            "admission_end_at": state.get("admission_end_at", state.get("end_at")),
            "admission_closed_at": state.get("admission_closed_at"), "finished_at": state.get("finished_at"),
            "code_sha256": state.get("code_sha256"), "score_policy": score_policy(state),
            "source_manifest_sha256": state.get("match_manifest_sha256"),
            "risk_and_execution": risk, "jev_budget": safe_budget,
            "participants": [{**{k: deepcopy(p[k]) for k in
                ("strategy_id", "version", "recipe_sha256", "max_hold_seconds", "size_pct", "account_id") if k in p},
                **mixed_contract(state, p.get("strategy_id")), **leverage_of(state, p.get("strategy_id"))}
                for p in source.get("participants", [])]}


def mixed_contract(state, strategy_id):
    """R2.1.12 (MIX, plan check p. 17): {"instrument": ...} of a mixed season's participant; {} otherwise."""
    rules = state.get("rules") or {}
    contract = (rules.get("instruments") or {}).get(strategy_id) if rules.get("instrument") == "MIXED" else None
    return {"instrument": contract} if isinstance(contract, str) and MARKET_MEMBER.fullmatch("market." + contract + ".jsonl") else {}


def encode(value):
    return json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":")).iterencode(value)


def public_candles(raw):
    rows = []
    for row in raw.get("candles", []):
        if not isinstance(row, (list, tuple)) or len(row) < 9 or str(row[8]) not in {"0", "1"}:
            raise ValueError("invalid_candle")
        try:
            values = [Decimal(str(value)) for value in row[:6]]
        except InvalidOperation:
            raise ValueError("invalid_candle") from None
        if any(not value.is_finite() for value in values) or values[0] != int(values[0]):
            raise ValueError("invalid_candle")
        rows.append({"open_time_ms": int(values[0]), **{key: format(value, "f") for key, value in
            zip(("open", "high", "low", "close", "volume"), values[1:])}, "closed": str(row[8]) == "1"})
    return rows


def create_archive(state, *, snapshots_path=None, max_bytes=MAX_BYTES):
    """Caller owns a stable journal snapshot. Full events restored before this call. snapshots_path: the season's journal,
    or (R2.1.12, MIX) {contract: journal} of a mixed season, as exchange_engine.snapshot_files gives them."""
    rules = state.get("rules") or {}
    mixed = rules.get("instrument") == "MIXED"
    if mixed and not isinstance(snapshots_path, dict):  # R2.1.12 RC2 (X2-06): never empty market members that pass verify
        raise ArenaError("Записи ринку змішаного сезону потрібні окремо за кожним інструментом.")
    paths = snapshots_path if isinstance(snapshots_path, dict) else {None: snapshots_path}
    if mixed and set(paths) != set((rules.get("instruments") or {}).values()):
        raise ArenaError("Записи ринку не відповідають інструментам змішаного сезону.")
    if max_bytes == MAX_BYTES and state.get("mode") == "PAPER" and any(r.get("strategy_schema_version") == 3 for r in state.get("strategies", [])):
        max_bytes = PILOT_MAX_BYTES
    output = tempfile.SpooledTemporaryFile(max_size=8 * 1024**2, mode="w+b")
    receipt, total = [], 0
    snapshot_count, omitted = 0, 0
    counts, omissions = {}, {}
    try:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            def write_chunks(name, chunks):
                nonlocal total
                digest, length = hashlib.sha256(), 0
                with archive.open(name, "w") as target:
                    for chunk in chunks:
                        data = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
                        total += len(data)
                        if total > max_bytes:
                            raise ArenaError("Пакет бою перевищує межу експорту; дані не обрізано.")
                        target.write(data)
                        digest.update(data)
                        length += len(data)
                receipt.append({"path": name, "bytes": length, "sha256": digest.hexdigest()})

            from .battle_debrief import recorded_quotes
            context_status = None
            try:
                if mixed:  # R2.1.12 (MIX, plan check p. 14): each contract's quotes up to its own watermark
                    context = {c: recorded_quotes(p, ((state.get("latest_markets") or {}).get(c) or {}).get("at"))
                               for c, p in paths.items() if p is not None} or None
                else:
                    context = (recorded_quotes(snapshots_path, (state.get("latest_market") or {}).get("at"))
                               if snapshots_path is not None else None)
            except (ArenaError, ValueError, OSError, TypeError):
                # Optional diagnostic context is unavailable, never fabricated.
                # The bounded exporter below still counts/omits invalid snapshots
                # and enforces its own per-line and aggregate archive limits.
                context = None
                context_status = 'UNAVAILABLE'
            review = build_review(state, include_request_payloads=True, snapshot_context=context,
                                  snapshot_context_status=context_status)
            del context
            verify_bundle(review)
            history_complete = review["history_complete"]
            windows = len(review["degraded_windows"]) if "degraded_windows" in review else None  # R2.1.11 (A4)
            write_chunks("battle-review.json", encode(review))
            del review
            replay = build_packet(state)
            verify_packet(replay)
            write_chunks("battle-replay.json", encode(replay))
            del replay
            recipes = []
            for row in state.get("strategies", []):
                if row.get("id") == "cash":
                    continue
                payload = payload_of(row)
                validate_strategy(payload)
                if fingerprint(payload) != row.get("recipe_sha256"):
                    raise ArenaError("Рецепт бою не збігається з зафіксованим хешем.")
                recipes.append({"id": row["id"], "version": row.get("version"),
                                "recipe_sha256": row["recipe_sha256"], "recipe": payload})
            write_chunks("strategies.json", encode(recipes))
            write_chunks("match-settings.json", encode(match_settings(state)))
            def market_rows(contract=None):
                nonlocal snapshot_count, omitted
                from .arena_market import build_snapshot
                snapshots_path = paths[contract]
                if snapshots_path is None or not Path(snapshots_path).is_file():
                    return
                source_path = Path(snapshots_path)
                if source_path.is_symlink():
                    raise ArenaError("Посилання замість запису ринку.")
                boundary = ((state.get("latest_markets") or {}).get(contract) if contract else state.get("latest_market") or {}) or {}
                boundary = boundary.get("at")
                # The season's contract: an ETH row rebuilt as BTC fails validation and was silently omitted (Demo 9b6289).
                instrument = contract or (state.get("rules") or {}).get("instrument", "BTC-USDT-SWAP")
                with source_path.open(encoding="utf-8") as source:
                    for line in iter(lambda: source.readline(2 * 1024**2 + 1), ""):
                        if len(line) > 2 * 1024**2:
                            raise ArenaError("Нечинний рядок запису ринку.")
                        try:
                            row = json.loads(line)
                            if not isinstance(row, dict):
                                raise ValueError()
                            if isinstance(boundary, (int, float)) and isinstance(row.get("at"), (int, float)) and row["at"] > boundary:
                                break
                            snapshot = build_snapshot(row["raw"], row["at"], instrument=instrument,
                                allow_unknown_funding_schedule=state["mode"] == "OKX_DEMO"
                                and row.get("funding_status") in {"unknown", "unknown_schedule"})
                            candles = public_candles(row["raw"])
                        except (ValueError, KeyError, TypeError):
                            omitted += 1
                            if contract:
                                omissions[contract] = omissions.get(contract, 0) + 1
                            continue
                        # build_snapshot constructs a typed public DTO. Raw provider objects
                        # and arbitrary unknown fields are deliberately not copied.
                        public = {k: value for k, value in snapshot.items() if k != "raw"}
                        if "htf_context" in snapshot["raw"]:
                            public["htf_context"] = snapshot["raw"]["htf_context"]
                        public["candles"] = candles
                        if contract:  # R2.1.13 (X2-04): a mixed season's member names its contract in every row
                            public["instrument"] = contract
                        public["features_rebuilt_from_recorded_public"] = True
                        pin = row.get("sha256")
                        public["source_snapshot_sha256"] = pin if isinstance(pin, str) and len(pin) == 64 and all(c in "0123456789abcdef" for c in pin) else None
                        public["capture_at"] = row.get("at")
                        public["source"] = "SYNTHETIC" if row["raw"].get("synthetic") is True else "RECORDED_PUBLIC"
                        verify_market_row(public)
                        snapshot_count += 1
                        if contract:
                            counts[contract] = counts.get(contract, 0) + 1
                        yield from encode(public)
                        yield "\n"
            if mixed:  # R2.1.12 (MIX, plan check p. 18): no market.jsonl, so no reader takes one contract for the season
                for contract in sorted(paths):
                    counts.setdefault(contract, 0)
                    omissions.setdefault(contract, 0)
                    write_chunks("market." + contract + ".jsonl", market_rows(contract))
            else:
                write_chunks("market.jsonl", market_rows())
            instructions = (
                "Приватний пакет аналізу Jev Trading Arena.\n"
                "Надішли весь цей ZIP координатору/моделі для аналізу.\n"
                "battle-review.json: усі події, funnel, fills, net/fees/funding/Jev, точні числові запити.\n"
                "battle-replay.json: причинний повтор; strategies.json: зафіксовані повні рецепти.\n"
                "match-settings.json: бюджет, час, правила оцінки та обмеження цього бою.\n"
                + ("market.<instId>.jsonl: по файлу на інструмент змішаного сезону; перевірені записані ринкові ознаки та котирування; пропуски не вигадуються.\n"
                   if mixed else "market.jsonl: перевірені записані ринкові ознаки та котирування; пропуски не вигадуються.\n") +
                "manifest.json: статус, межа запису, SHA-256 і розмір кожного файлу, хеші коду журналу й експорту.\n"
                "manifest-receipt.json: SHA-256 і розмір самого manifest.json.\n"
                "API-ключі, Authorization, UID, приватні відповіді рахунку та вільні причини Jev не входять.\n"
                "Це файл аналізу, не архів перенесення і не дозвіл на публікацію.\n"
                "RUNNING/PAUSED/DRAINING або incomplete означають проміжний/непідтверджений результат.\n"
                "Старий журнал може не містити точних wire bytes, повних ринкових записів або нових ознак.\n"
                "Ринкові ознаки перераховані з записаних публічних даних чинним кодом; фактичні рішення лишаються в подіях.\n"
            )
            write_chunks("README_UA.txt", (instructions,))
            manifest = {"schema": SCHEMA, "exported_at": datetime.now(timezone.utc).isoformat(),
                        "battle_id": state["id"], "mode": state["mode"], "status": state["status"],
                        "execution_mode": state.get("execution_mode", "rules"),
                        "history_complete": history_complete, "incomplete": bool(state.get("incomplete")),
                        "final": state["status"] == "DONE", "event_count": len(state.get("events", [])),
                        "last_event_seq": state.get("events", [{}])[-1].get("seq") if state.get("events") else None,
                        "market_samples": snapshot_count, "market_rows_omitted": omitted,
                        "contains_credentials": False, "private_analysis_only": True,
                        "financial_actions": False, "code_hashes": code_hashes(state), "files": receipt}
            if windows is not None:
                manifest["degraded_windows"] = windows
            if mixed:  # R2.1.12 (MIX): per contract; market_samples stays the sum
                manifest.update(market_samples_by_instrument=dict(sorted(counts.items())),
                                market_rows_omitted_by_instrument=dict(sorted(omissions.items())))
            manifest_bytes = "".join(encode(manifest)).encode("utf-8")
            archive.writestr("manifest.json", manifest_bytes)
            archive.writestr(RECEIPT, "".join(encode({"schema": RECEIPT_SCHEMA, "manifest_bytes": len(manifest_bytes),
                                                      "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest()})).encode("utf-8"))
        output.seek(0)
        return output
    except Exception:
        output.close()
        raise


def verify_archive(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        expected = MEMBERS
        markets = sorted(MARKET_MEMBER.fullmatch(n).group(1) for n in names if MARKET_MEMBER.fullmatch(n))
        if markets:  # R2.1.12 (MIX): a mixed season's ZIP: one member per contract instead of market.jsonl
            expected = (MEMBERS - {"market.jsonl"}) | {"market." + c + ".jsonl" for c in markets}
        if len(names) != len(set(names)) or set(names) not in (expected, expected | {RECEIPT}) or (markets and len(markets) < 2):
            raise ArenaError("Нечинний склад файлу бою.")
        if any(i.filename.endswith('.json') and i.file_size > 64 * 1024**2 for i in archive.infolist()):
            raise ArenaError("JSON файлу бою перевищує межу перевірки.")
        if any(i.file_size > PILOT_MAX_BYTES for i in archive.infolist()) or archive.getinfo("manifest.json").file_size > 131072:
            raise ArenaError("Файл бою перевищує межу перевірки.")
        manifest_bytes = archive.read("manifest.json")
        if RECEIPT in names:
            if archive.getinfo(RECEIPT).file_size > 4096:
                raise ArenaError("Квитанція маніфесту перевищує межу.")
            stamp = json.loads(archive.read(RECEIPT))
            if (not isinstance(stamp, dict) or stamp.get("schema") != RECEIPT_SCHEMA
                    or stamp.get("manifest_bytes") != len(manifest_bytes)
                    or stamp.get("manifest_sha256") != hashlib.sha256(manifest_bytes).hexdigest()):
                raise ArenaError("Квитанція маніфесту не збігається з manifest.json.")
        manifest = json.loads(manifest_bytes)
        if (manifest.get("schema") != SCHEMA or manifest.get("contains_credentials") is not False
                or manifest.get("financial_actions") is not False or not isinstance(manifest.get("files"), list)
                or len(manifest["files"]) != len(expected) - 1
                or {r.get("path") for r in manifest["files"]} != expected - {"manifest.json"}):
            raise ArenaError("Нечинний маніфест файлу бою.")
        total = 0
        for row in manifest["files"]:
            info = archive.getinfo(row["path"])
            total += info.file_size
            if total > PILOT_MAX_BYTES or info.file_size != row["bytes"]:
                raise ArenaError("Розмір файлу бою не збігається.")
            digest = hashlib.sha256()
            with archive.open(row["path"]) as source:
                for chunk in iter(lambda: source.read(1024**2), b""):
                    digest.update(chunk)
            if digest.hexdigest() != row["sha256"]:
                raise ArenaError("Хеш файлу бою не збігається.")
        review = json.loads(archive.read("battle-review.json"))
        verify_bundle(review)
        if ("degraded_windows" in manifest) != ("degraded_windows" in review) or (
                "degraded_windows" in manifest and manifest["degraded_windows"] != len(review["degraded_windows"])):
            raise ArenaError("Лічильник вікон збоїв у маніфесті не збігається з розділом бою.")  # R2.1.11 (A4)
        replay = json.loads(archive.read("battle-replay.json"))
        verify_packet(replay)
        recipes = json.loads(archive.read("strategies.json"))
        if not isinstance(recipes, list) or len({r["id"] for r in recipes}) != len(recipes):
            raise ArenaError("Нечинний склад стратегій.")
        for row in recipes:
            validate_strategy(row["recipe"])
            if fingerprint(row["recipe"]) != row["recipe_sha256"]:
                raise ArenaError("Рецепт у файлі бою змінено.")
        if total > MAX_BYTES and not (manifest.get('mode') == 'PAPER' and any(r['recipe'].get('strategy_schema_version') == 3 for r in recipes)):
            raise ArenaError("Розмір файлу бою перевищує межу цього режиму.")
        settings = json.loads(archive.read("match-settings.json"))
        if (manifest["battle_id"] != review["match"]["id"] or manifest["event_count"] != len(review["events"])
                or replay["manifest"]["id"] != manifest["battle_id"] or settings.get("id") != manifest["battle_id"]
                or manifest["mode"] != review["match"]["mode"] or manifest["mode"] != replay["manifest"]["mode"]
                or manifest["mode"] != settings.get("mode") or manifest["status"] != review["match"]["status"]
                or manifest["incomplete"] is not bool(review["match"].get("incomplete"))
                or manifest["final"] is not (review["match"]["status"] == "DONE")
                or manifest["history_complete"] is not review["history_complete"]
                or {r["id"] for r in recipes} != {p["id"] for p in review["participants"]}
                or any(r["recipe_sha256"] != next(p["recipe_sha256"] for p in review["participants"] if p["id"] == r["id"]) for r in recipes)):
            raise ArenaError("Файли належать різним боям.")
        if markets and (markets != review["match"].get("instruments") or "market_samples_by_instrument" not in manifest
                        or sorted(manifest["market_samples_by_instrument"]) != markets):
            raise ArenaError("Записи ринку не відповідають інструментам бою.")  # R2.1.12 (MIX)
        if not markets and ("instruments" in review["match"] or "market_samples_by_instrument" in manifest):
            raise ArenaError("Записи ринку не відповідають інструментам бою.")
        if markets:  # R2.1.13 (X2-04): whole counters, one for each contract, adding up to the season's own
            for field, total in (("market_samples_by_instrument", "market_samples"), ("market_rows_omitted_by_instrument", "market_rows_omitted")):
                counters = manifest.get(field)
                if (not isinstance(counters, dict) or sorted(counters) != markets or any(type(v) is not int or v < 0 for v in counters.values())
                        or type(manifest.get(total)) is not int or sum(counters.values()) != manifest[total]):
                    raise ArenaError("Лічильники ринку за інструментом не збігаються.")
        market_count = 0
        for member in ["market." + c + ".jsonl" for c in markets] or ["market.jsonl"]:
            member_count, named = 0, None
            with archive.open(member) as source:
                for line in iter(lambda: source.readline(2 * 1024**2 + 1), b""):
                    if len(line) > 2 * 1024**2:
                        raise ArenaError("Нечинний рядок ринку.")
                    row = json.loads(line)
                    verify_market_row(row)
                    if markets:  # R2.1.13 (X2-04): every row names its contract, or none does: a ZIP written by R2.1.12 has no such field
                        has = "instrument" in row
                        if (named is not None and has != named) or (has and row["instrument"] != member[7:-6]):
                            raise ArenaError("Запис ринку належить іншому інструменту.")
                        named = has
                    member_count += 1
            if markets and manifest["market_samples_by_instrument"][member[7:-6]] != member_count:
                raise ArenaError("Кількість ринкових записів не збігається.")
            market_count += member_count
        if type(manifest["market_samples"]) is not int or manifest["market_samples"] != market_count:
            raise ArenaError("Кількість ринкових записів не збігається.")
        return {"verified": True, "battle_id": manifest["battle_id"], "status": manifest["status"],
                "final": manifest["final"], "incomplete": manifest["incomplete"],
                "events": manifest["event_count"], "history_complete": manifest["history_complete"],
                "participants": len(recipes), "contains_credentials": False}
