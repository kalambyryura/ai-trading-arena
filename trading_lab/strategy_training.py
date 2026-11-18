"""Reproducible offline practice using the same execution engine as the arena."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import uuid

from .arena_engine import new_season, tick, public_season
from .arena_market import candle_features, context_features, richer_features, build_snapshot, PATHS
from .arena_strategies import ArenaError, fingerprint, number
from .strategy_contract import payload_of
from .match_protocol import JOURNAL_BYTES, REPLAY_QUOTES, TIME_POLICY

SYNTHETIC = {"id": "synthetic-suite-v2", "name": "Три режими · тренажер v2", "kind": "SYNTHETIC",
             "description": "Зростання, падіння та боковик: 3 × 15 хвилин для входів і хвіст для виходу за рецептом. Вигадані ціни, механічна перевірка."}


SOURCE_FILES = ("ikigai_g1.py", "ikigai_g1_reference.py", "causal_htf.py", "arena_engine.py", "arena_market.py", "arena_strategies.py",
                "strategy_v2.py", "strategy_v4.py", "strategy_v4_book.py", "strategy_v4_feed.py", "strategy_v4_runtime.py", "strategy_runtime.py", "strategy_contract.py", "cycle_probe.py", "cycle_diagnostics.py", "match_protocol.py",
                "strategy_training.py")


def source_hash(folder=None):
    """sha256 over the engine sources with only CRLF -> LF normalised (D098): a CRLF working copy and an LF clone of
    the same code hash alike; any other byte change still changes the hash."""
    folder = Path(folder or Path(__file__).parent)
    return hashlib.sha256(b"".join((folder / name).read_bytes().replace(b"\r\n", b"\n") for name in SOURCE_FILES)).hexdigest()


LOADED_CODE_HASH = source_hash()


def code_hash():
    if source_hash() != LOADED_CODE_HASH:
        raise ArenaError("Код оновлено. Перезапусти панель через Update Jev Trading Lab перед новим тестом.")
    return LOADED_CODE_HASH


SYNTHETIC_START = 1800000000  # Minute boundary, arbitrary synthetic clock, not a market observation.


def synthetic_price(regime):
    direction = {"up": 1, "down": -1, "range": 0}[regime]
    return lambda seconds: 84000 * (1 + direction * seconds / 100000 + .0015 * math.sin(seconds / 95))


def synthetic_rows(regime, tail_seconds=120):
    """The 1m candles of one synthetic regime, in the OKX row layout (all confirmed)."""
    start, price = SYNTHETIC_START, synthetic_price(regime)
    rows = []
    end_offset = 900 + tail_seconds
    for minute in range(-31, math.ceil(end_offset / 60) + 2):
        opening, close = price(minute * 60), price((minute + 1) * 60)
        observed = [price(minute * 60 + offset) for offset in range(0, 61, 5)]
        rows.append([str((start + minute * 60) * 1000), str(opening), str(max(observed)),
                     str(min(observed)), str(close), str(100 + 25 * math.sin(minute)), "0", "0", "1"])
    return rows


def synthetic_tape(regime, tail_seconds=120):
    """Features see only closed past candles, even though the fixture is reproducible."""
    start, price = SYNTHETIC_START, synthetic_price(regime)
    rows = synthetic_rows(regime, tail_seconds)
    end_offset = 900 + tail_seconds
    tape = []
    for offset in range(0, end_offset + 5, 5):
        now = start + offset
        past = [row for row in rows if float(row[0]) / 1000 + 60 <= now][-30:]
        features = candle_features(past, now)
        features.update(richer_features(past))
        mid, spread = price(offset), 1.0
        features["spread_bps"] = spread
        funding_at = start + 600 if offset < 600 else start + (offset // 3600 + 1) * 3600
        features.update(context_features(int(float(past[-1][0])) + 60000, now, .0001, funding_at))
        raw = {"synthetic": True, "generator": "three-regimes-v1", "regime": regime, "offset": offset}
        tape.append({"at": now, "server_time": now, "quote_ts": now * 1000, "bar_close_ms": int(float(past[-1][0])) + 60000,
                     "bid": mid * (1 - spread / 20000), "ask": mid * (1 + spread / 20000), "mid": mid,
                     "bid_btc": 1, "ask_btc": 1, "lot_btc": .0001, "min_btc": .0001,
                     "features": features, "funding_rate": .0001, "funding_at": funding_at,
                     "funding_time_ms": int(funding_at * 1000),
                     "raw": raw, "sha256": fingerprint(raw)})
    return tape


def read_recorded(path):
    """Rebuild features from recorded public raw responses; never trust submitted derived facts."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size > JOURNAL_BYTES:
        raise ArenaError("Запис котирувань відсутній або завеликий (512 MB).")
    tape = []
    try:
        with path.open(encoding="utf-8") as file:
            for line in file:
                if len(tape) >= REPLAY_QUOTES or len(line) > 262144:
                    raise ArenaError("Не більше 36000 котирувань у записі.")
                row = json.loads(line)
                if not isinstance(row.get("raw"), dict) or set(row["raw"]) != set(PATHS):
                    raise ArenaError("Потрібен запис публічної стрічки; синтетичні чи сторонні поля не дозволені.")
                now = number(row["at"], 1, 1e12)
                model = (row.get("source") == "MODEL_RECONSTRUCTED_FROM_OKX_PUBLIC_1M"
                         and row.get("model") == "okx-1m-candle-path-v2")
                snapshot = build_snapshot(row["raw"], now, allow_unknown_funding_schedule=row.get("mode") == "OKX_DEMO",
                                          allow_model_funding=model)
                if snapshot["sha256"] != row["sha256"]:
                    raise ArenaError("Хеш вихідних котирувань не збігається.")
                if tape and (now <= tape[-1]["at"] or snapshot["quote_ts"] < tape[-1]["quote_ts"]):
                    raise ArenaError("Запис має зберігати порядок часу.")
                tape.append(snapshot)
    except (KeyError, TypeError, ValueError, AttributeError, IndexError) as error:
        if isinstance(error, ArenaError):
            raise
        raise ArenaError("Запис не містить перевірюваних публічних котирувань OKX.") from None
    if len(tape) < 20 or tape[-1]["at"] - tape[0]["at"] < 90:
        raise ArenaError("Потрібен запис щонайменше 90 секунд та 20 котирувань.")
    return tape


def attach_tape_book(strategy, tape):
    """A schema-4 feature book from the tape's own history: the synthetic candles, or the confirmed candles each
    recorded snapshot carries; a feature whose window reaches before the tape stays UNKNOWN."""
    from .strategy_v4 import required_features
    from .strategy_v4_book import FeatureBook, attach, attach_raw
    book = FeatureBook(required_features(strategy["rules"]).values())
    tape = [dict(snapshot) for snapshot in tape]
    first = tape[0].get("raw") or {}
    if first.get("synthetic"):
        minutes = [(int(r[0]), *(float(x) for x in r[1:6])) for r in synthetic_rows(first["regime"], tape[-1]["at"] - SYNTHETIC_START - 900)]
        return attach(tape, book, minutes)
    return attach_raw(tape, book)


def run_trial(strategy, dataset=SYNTHETIC, budget_usdt=100, tapes=None):
    from .ikigai_g1 import enabled
    if enabled(strategy):
        raise ArenaError("G1: existing M1 training unsupported; no fake qualification")
    from .cycle_probe import is_probe
    if is_probe(strategy):
        raise ArenaError("Технічний план потребує OKX Demo, а не PAPER/replay.")
    if strategy.get("strategy_schema_version") == 4 and strategy["instrument"] != "BTC-USDT-SWAP" and dataset["id"] != SYNTHETIC["id"]:
        raise ArenaError("Записані стрічки майстерні — BTC-USDT-SWAP; рецепт іншого інструмента тут перевіряється лише на синтетиці.")
    budget = number(budget_usdt, 25, 1000)
    if tapes is None:
        if dataset["id"] != SYNTHETIC["id"]:
            raise ArenaError("Для записаного ринку потрібна перевірена стрічка.")
        tapes = [(name, synthetic_tape(regime, (strategy["rules"]["max_hold_seconds"] or 3600) + 120), 900) for name, regime in
                 (("Зростання", "up"), ("Падіння", "down"), ("Боковик", "range"))]
    if not 1 <= len(tapes) <= 3:
        raise ArenaError("Один тест містить 1…3 обмежені прогони.")
    source_hash = fingerprint([[name, duration, [{k: v for k, v in s.items() if k != "raw"} for s in tape]]
                               for name, tape, duration in tapes])
    if strategy.get("strategy_schema_version") == 4:  # after the data hash: the book is derived, not market data
        from .strategy_contract import recipe_intact
        if not recipe_intact(strategy):
            raise ArenaError("Схема 4: правила двигуна не відповідають рецепту.")
        tapes = [(name, attach_tape_book(strategy, tape), duration) for name, tape, duration in tapes]
    engine_hash = code_hash()
    runs = []
    for name, tape, duration in tapes:
        duration = number(duration, 60, 86400)
        if not 2 <= len(tape) <= REPLAY_QUOTES:
            raise ArenaError("Непідтримувана довжина стрічки.")
        state = new_season([strategy], budget, duration, tape[0]["at"], engine_hash)
        state["match_manifest"].update(evaluation="offline_training", source_kind=dataset["kind"], data_sha256=source_hash)
        state["match_manifest_sha256"] = fingerprint(state["match_manifest"])
        for snapshot in tape:
            if state["status"] not in {"RUNNING", "DRAINING", "CLOSING"}:
                break
            tick(state, snapshot)
        bot = state["bots"][0]
        complete = state["status"] == "DONE" and not state["incomplete"] and not bot["position_btc"]
        metrics = {k: bot[k] for k in ("pnl_usdt", "return_pct", "max_drawdown_pct", "trades", "fees_usdt", "funding_usdt")}
        metrics.update(remaining_btc=bot["position_btc"], cash_pnl_usdt=0)
        public = public_season(state)
        runs.append({"name": name, "status": "COMPLETE" if complete else "INCOMPLETE", "incomplete": not complete,
                     "time_policy": TIME_POLICY, "admission_seconds": duration,
                     "match_manifest": state["match_manifest"], "match_manifest_sha256": state["match_manifest_sha256"],
                     "metrics": metrics, "curve": deepcopy(bot["curve"]), "events": public["events"],
                     "lifecycle": public["lifecycle"]})
    pnl = sum(r["metrics"]["pnl_usdt"] for r in runs)
    complete_count = sum(r["status"] == "COMPLETE" for r in runs)
    summary = {"pnl_usdt": pnl, "return_pct": pnl / (budget * len(runs)) * 100,
               "max_drawdown_pct": max(r["metrics"]["max_drawdown_pct"] for r in runs),
               "trades": sum(r["metrics"]["trades"] for r in runs),
               "fees_usdt": sum(r["metrics"]["fees_usdt"] for r in runs),
               "funding_usdt": sum(r["metrics"]["funding_usdt"] for r in runs),
               "complete_runs": complete_count, "total_runs": len(runs)}
    feedback = ["Суми — по незалежних прогонах, дохідність — від їхнього сумарного стартового капіталу.",
                "Скриня має PnL 0 USDT. Модельні fees, spread, slippage та funding уже враховані.",
                "Повторні прогони на цих даних — тренування, не незалежна перевірка. Наступний контроль — новий forward PAPER-сезон.",
                "Jev не викликався: цей звіт перевіряє точний формальний рецепт, не рішення Jev."]
    feedback.append("Дедлайн припиняє входи й докупівлі; позиція завершує власний вихід. Недостатній хвіст запису дає INCOMPLETE, без вигаданого закриття.")
    if dataset["kind"] == "SYNTHETIC":
        feedback.insert(0, "СИНТЕТИЧНИЙ ТРЕНАЖЕР: вигадані режими не доводять прибутковості на ринку.")
    if strategy.get("strategy_schema_version") == 4:
        feedback.append("Схема 4: ознаки рахує книга лише з історії цієї стрічки; ознака з довшим вікном лишається UNKNOWN і блокує вхід. "
                        "Офіційна оцінка схеми 4 — tools/team_eval.py на DEV.")
        if strategy["rules"]["order"]["type"] == "post_only" and dataset["kind"] == "SYNTHETIC":
            feedback.append("Post-only на синтетиці не виставляється: у вигаданій стрічці немає свічок і тіку для ліміту. "
                            "Виконання post-only перевіряють записана стрічка, OKX Demo і tools/team_eval.py.")
    if not summary["trades"]:
        feedback.append("Угод не було. Перевір умови входу, мінімальний лот і достатність бюджету; нуль не є перевагою над скринею.")
    if complete_count != len(runs):
        feedback.append("Є неповні прогони/залишок позиції; цим звітом не можна підтвердити готовність версії.")
    return {"id": uuid.uuid4().hex, "version_id": strategy["id"],
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "dataset_id": dataset["id"], "dataset_name": dataset["name"], "dataset_kind": dataset["kind"],
            "status": "COMPLETE" if complete_count == len(runs) else "INCOMPLETE", "budget_usdt": budget,
            "runtime": "RULES_ONLY", "ai_cost_usd": 0, "summary": summary, "feedback": feedback, "runs": runs,
            "strategy": payload_of(strategy),
            "strategy_sha256": strategy["recipe_sha256"], "data_sha256": source_hash, "code_sha256": engine_hash,
            "data_start_at_ms": int(min(tape[0]["at"] for _, tape, _ in tapes)*1000),
            "data_end_at_ms": int(max(tape[-1]["at"] for _, tape, _ in tapes)*1000)}
