"""Ongoing local research league. Locks/feedback never start a worker or move funds."""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from collections import Counter
import time
import threading
import uuid
import hashlib
from datetime import datetime
from .arena import read, write
from .arena_strategies import ArenaError, fingerprint
from .battle_review import build_review, leverage_cohort, number, verify_bundle
from .match_journal import journal_copy, running_status
from .match_protocol import SCORE_POLICY, score_policy, entry_participation


def examples_of(state):
    """R2.1.11 (E, D135 p. 7, D141 p. 2): the participants written by an example author (a Codex sample, the technical
    probe): read from the journal's recipes, since the bundle carries no author."""
    return frozenset(s["id"] for s in state.get("strategies", []) if isinstance(s, dict) and s.get("id") != "cash"
                     and (s.get("author") or {}).get("kind") == "example")


def instruments_mark(state):
    """R2.1.12 (MIX, D149 p. 2): the cohort mark of a mixed battle, e.g. "змішаний: BTC ×3 + ETH ×1"; None for a
    one-contract battle. The cohort itself already differs by the recipes; league.py and public_site use this one."""
    rules = state.get("rules") or {}
    if rules.get("instrument") != "MIXED":
        return None
    coins = Counter(str(c).split("-")[0] for c in (rules.get("instruments") or {}).values())
    return "змішаний: " + " + ".join(f"{coin} ×{n}" for coin, n in sorted(coins.items()))


def example_only(state):
    """D141 p. 2: every participant of the battle is an example author ('технічна проба, не рахується')."""
    ids = [s["id"] for s in state.get("strategies", []) if isinstance(s, dict) and s.get("id") != "cash"]
    return bool(ids) and set(ids) <= examples_of(state)


def score(bundle, tolerance="0.00000001", examples=frozenset()):
    verify_bundle(bundle)
    match, rows = bundle["match"], bundle["participants"]
    if examples and rows and all(r["id"] in examples for r in rows):
        return {"status": "INELIGIBLE", "reason": "example_only", "sole_winner": None, "shared_top": [], "known_results": [],
                "score_policy": score_policy(match), "eligible_ids": [], "excluded": [{"id": r["id"], "reason": "example_author"} for r in rows]}
    rows = [r for r in rows if r["id"] not in examples]  # an example never takes part in a win
    policy = score_policy(match)
    result = {"status": "INCOMPLETE", "sole_winner": None, "shared_top": [], "known_results": [],
              "score_policy": policy, "eligible_ids": [], "excluded": []}
    for row in rows:
        value = number(row["costs"].get("net_usdt"))
        if value is not None: result["known_results"].append({"id": row["id"], "net_usdt": value})
    if policy is None:
        return {**result, "status": "INELIGIBLE", "reason": "unknown_score_policy"}
    if match["status"] != "DONE" or not rows or any(number(r["position_btc"]) is None or Decimal(number(r["position_btc"])) != 0 or not r["costs"].get("complete") for r in rows):
        return result
    if match.get("incomplete") or not bundle.get("history_complete") or any(r["quality"].get("data_status") != "COMPLETE" for r in rows):
        return {**result, "status": "INELIGIBLE"}
    capital = {Decimal(r["allocation_usdt"]) if number(r["allocation_usdt"]) is not None else None for r in rows}
    if len(capital) != 1 or None in capital: return {**result, "status": "INELIGIBLE"}
    if len(result["known_results"]) != len(rows): return result
    if policy["include_cash"]:
        outcomes = result["known_results"] + [{"id": "cash", "net_usdt": "0"}]
    else:
        for row in rows:
            participation = entry_participation(row.get("funnel"))
            if participation["status"] == "UNKNOWN":
                return {**result, "status": "INELIGIBLE", "reason": "entry_evidence_unknown"}
            if participation["eligible_to_win"]:
                result["eligible_ids"].append(row["id"])
            else:
                result["excluded"].append({"id": row["id"], "reason": "no_filled_entry"})
        outcomes = [r for r in result["known_results"] if r["id"] in result["eligible_ids"]]
        if not outcomes:
            return {**result, "status": "NO_CONTEST", "ordered": [], "reason": "no_filled_entries"}
    result["eligible_ids"] = [r["id"] for r in outcomes]
    ordered = sorted(outcomes, key=lambda r: (-Decimal(r["net_usdt"]), r["id"]))
    best = Decimal(ordered[0]["net_usdt"])
    top = [r["id"] for r in ordered if best - Decimal(r["net_usdt"]) <= Decimal(policy["tie_tolerance_usdt"])]
    return {**result, "status": "SOLE_WIN" if len(top) == 1 else "TIE_AT_TOP", "shared_top": top,
            "sole_winner": top[0] if len(top) == 1 else None, "ordered": [r["id"] for r in ordered]}


MODE_LABELS = {"OKX_LIVE": "Live-бій", "OKX_DEMO": "Demo-бій", "PAPER": "PAPER-сезон"}
CASH = {"id": "cash", "recipe_sha256": "cash", "version": 0, "name": "Cash", "author_name": None, "author_kind": "baseline", "model": None}


def public_identity(strategy):
    """Display-only passport fields of a frozen recipe; prompts and private files are never projected."""
    if not isinstance(strategy, dict):
        return {"name": None, "author_name": None, "author_kind": None, "model": None}
    author = strategy.get("author") if isinstance(strategy.get("author"), dict) else {}
    def text(value, limit):
        return value[:limit] if isinstance(value, str) and value.strip() else None
    return {"name": text(strategy.get("name"), 60), "author_name": text(author.get("name"), 60),
            "author_kind": author.get("kind") if author.get("kind") in {"human", "model", "example"} else None,
            "model": text(author.get("model"), 80)}


def duration_label(seconds):
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    if seconds % 3600 == 0:
        return f"{int(seconds // 3600)} год"
    if seconds % 60 == 0:
        return f"{int(seconds // 60)} хв"
    return f"{int(seconds)} с"


def label_battles(battles):
    """Copies with a human name numbered chronologically within each mode, e.g. «Live-бій №3 · 24 год».

    Ids, cohort and bundle hashes stay in every row for verification; the cached rows are not mutated.
    """
    out = [dict(row) for row in battles]
    counters = {}
    for row in sorted((r for r in out if r.get("mode") in MODE_LABELS),
                      key=lambda r: (str(r.get("created_at") or r.get("label") or ""), str(r.get("id")))):
        counters[row["mode"]] = counters.get(row["mode"], 0) + 1
        parts = [f"{MODE_LABELS[row['mode']]} №{counters[row['mode']]}", duration_label(row.get("duration_seconds"))]
        row["display_label"] = " · ".join(p for p in parts if p)
    return out


def public_standings(battles, tolerance=Decimal("0.00000001")):
    """Per cohort and recipe over ELIGIBLE battles only: wins, ties, Σnet, mean net and battles vs Cash.

    Rows keep the legacy frequency fields (comparable_battles, sole_wins, shared_top). Net values are the
    scored reconciled net USDT of each battle; Cash is exactly zero. Everything here is descriptive:
    adjacent battles share market regimes, so no naive standard error is projected.
    """
    accumulators, cohorts = {}, {}
    for row in battles:
        if row.get("competitive_validity") != "ELIGIBLE":
            continue
        result = row["score"]
        nets = {r["id"]: Decimal(r["net_usdt"]) for r in result.get("known_results", [])}
        info = cohorts.setdefault(row["cohort"], {"cohort": row["cohort"], **({"cohort_mark": row["cohort_mark"]} if row.get("cohort_mark") else {}),  # R2.1.13 (X3-02)
            "mode": row.get("mode"),
            "mode_label": MODE_LABELS.get(row.get("mode")), "allocation_usdt": row.get("allocation_usdt"),
            "duration_seconds": row.get("duration_seconds"), "duration_label": duration_label(row.get("duration_seconds")),
            "execution_mode": row.get("execution_mode"), "score_policy": result.get("score_policy"),
            "participants": len(row.get("participants", [])), "no_contest_battles": 0,
            "battles": 0, "first_at": None, "last_at": None})
        info["battles"] += 1
        info["no_contest_battles"] += int(result.get("status") == "NO_CONTEST")
        stamp = row.get("created_at") or row.get("label")
        if stamp:
            info["first_at"] = min(filter(None, [info["first_at"], stamp]))
            info["last_at"] = max(filter(None, [info["last_at"], stamp]))
        for participant in row["participants"] + [CASH]:
            net = Decimal(0) if participant["id"] == "cash" else nets.get(participant["id"])
            if net is None:
                continue
            epoch = row["cohort"] + ":" + participant["recipe_sha256"]
            entry = accumulators.setdefault(epoch, {"row": {**participant, "cohort": row["cohort"], "comparable_battles": 0,
                "sole_wins": 0, "shared_top": 0, "above_cash": 0, "below_cash": 0, "at_cash": 0,
                "participated_battles": 0, "no_entry_battles": 0,
                "score_policy": result.get("score_policy")},
                "n": 0, "sum": Decimal(0)})
            tally = entry["row"]
            tally["comparable_battles"] += 1
            if (result.get("score_policy") or {}).get("id") == SCORE_POLICY["id"] and participant["id"] != "cash":
                tally["participated_battles"] += int(participant["id"] in result.get("eligible_ids", []))
                tally["no_entry_battles"] += int(any(r["id"] == participant["id"] and r["reason"] == "no_filled_entry" for r in result.get("excluded", [])))
            tally["sole_wins"] += int(result.get("sole_winner") == participant["id"])
            tally["shared_top"] += int(len(result.get("shared_top", [])) > 1 and participant["id"] in result["shared_top"])
            tally["above_cash"] += int(net > tolerance)
            tally["below_cash"] += int(net < -tolerance)
            tally["at_cash"] += int(abs(net) <= tolerance)
            entry["n"] += 1
            entry["sum"] += net
    rows = []
    for entry in accumulators.values():
        total = entry["sum"]
        rows.append({**entry["row"], "net_sum_usdt": format(total, "f"), "net_mean_usdt": format((total / entry["n"]).normalize(), "f")})
    rows.sort(key=lambda r: (r["cohort"], -Decimal(r["net_sum_usdt"]), r["id"]))
    return rows, sorted(cohorts.values(), key=lambda c: str(c.get("last_at") or ""), reverse=True)


def upgrade_eligibility(candidate, window, now_ms):
    required_times = ("opened_at_ms", "closed_at_ms", "feedback_released_at_ms", "training_cutoff_ms", "next_lock_at_ms")
    if any(type(window.get(k)) is not int for k in required_times) or not (
        window["feedback_released_at_ms"] <= window["opened_at_ms"] < window["closed_at_ms"] <= window["next_lock_at_ms"]):
        return {"eligible": False, "reason": "common_window_missing"}
    if not window["opened_at_ms"] <= now_ms <= window["closed_at_ms"] or type(candidate.get("submitted_at_ms")) is not int or not window["opened_at_ms"] <= candidate["submitted_at_ms"] <= window["closed_at_ms"]:
        return {"eligible": False, "reason": "outside_common_window"}
    if not candidate.get("parent_recipe_hash") or candidate.get("recipe_hash") == candidate["parent_recipe_hash"] or not candidate.get("qualified") or not candidate.get("all_trials_logged"):
        return {"eligible": False, "reason": "lineage_qualification_trials_required"}
    for key in ("sdk_hash", "feature_epoch_hash", "execution_policy_hash", "common_bundle_hash"):
        if not window.get(key) or candidate.get(key) != window[key]: return {"eligible": False, "reason": "epoch_mismatch_" + key}
    if type(candidate.get("training_latest_at_ms")) is not int or candidate["training_latest_at_ms"] > window["training_cutoff_ms"]:
        return {"eligible": False, "reason": "training_after_cutoff"}
    return {"eligible": True, "effective_at_lock_ms": window["next_lock_at_ms"]}


def training_calendar_cutoff(trials):
    """Synthetic fixture clocks are not dates of market observations.

    Unknown/unfinished provenance stays ineligible. Only persisted RULES_ONLY
    synthetic reports are exempt; recorded data must still respect the cutoff.
    Zero means that no calendar market observations were used in these trials.
    """
    latest = []
    if not trials:
        return None
    for trial in trials:
        if (trial.get("dataset_kind") == "SYNTHETIC" and trial.get("runtime") == "RULES_ONLY"
                and trial.get("status") in {"COMPLETE", "INCOMPLETE"}):
            continue
        stamp = trial.get("data_end_at_ms")
        if type(stamp) is not int or stamp < 0:
            return None
        latest.append(stamp)
    return max(latest, default=0)


class League:
    def __init__(self, root, arena, exchange, clock=time.time, workshop=None, monotonic=time.monotonic):
        self.path = Path(root) / "runtime" / "league_v1" / "manifest.json"
        self.arena, self.exchange, self.clock, self.monotonic = arena, exchange, clock, monotonic
        self.workshop, self.lock = workshop, threading.RLock()
        self.data = read(self.path, {"generation": 0, "plans": [], "feedback": [], "locks": [], "slots": []})
        self.cache = {}
        self.recent = {}  # (kind, id) -> (monotonic time, row) of a running battle: rebuilt at most every LIVE_ROW_SECONDS
        self.status_lock = threading.Lock()  # concurrent polls wait for one rebuild and then read its cached rows

    LIVE_ROW_SECONDS = 60  # a running battle's provisional row; its review is rebuilt at most once a minute

    def status(self):
        with self.status_lock:
            return self._status()

    def _status(self):
        battles = []
        for kind, controller, identities in (("paper", self.arena, self.arena.data["seasons"]), ("exchange", self.exchange, self.exchange.data["runs"])):
            for identity in reversed(identities):
                try:
                    stat = (controller.path / (identity + ".json")).stat()  # before the load: a later write misses the key
                    key = (kind, identity, stat.st_mtime_ns, stat.st_size)
                    row = self.cache.get(key)
                    live = running_status(controller, identity) not in {None, "DONE"}
                    recent = self.recent.get((kind, identity))
                    if row is None and live and recent and self.monotonic() - recent[0] < self.LIVE_ROW_SECONDS:
                        row = recent[1]  # a running battle: its provisional row of the last minute
                    if row is None:
                        state = journal_copy(controller, identity)  # the review itself never holds the engine's lock
                        bundle = build_review(state)
                        scored = score(bundle, examples=examples_of(state))
                        eligible = scored["status"] in {"SOLE_WIN", "TIE_AT_TOP", "NO_CONTEST"}
                        cohort = fingerprint({"mode": state["mode"], "allocation": state["budget_usdt"],
                            "duration": state["duration_seconds"], "code": state["code_sha256"],
                            "recipes": sorted(r["recipe_sha256"] for r in state["strategies"]),
                            "execution": state.get("execution_mode"), "fixture_only": state.get("fixture_only", False),
                            "source_kind": state.get("source_kind", (state.get("match_manifest") or {}).get("source_kind")),
                            "evaluation": (state.get("match_manifest") or {}).get("evaluation"),
                            "evidence_kind": state.get("evidence_kind", "MODEL" if state["mode"] == "PAPER" else "ACTUAL"),
                            "score_policy": score_policy(state), **leverage_cohort(state)})
                        passports = {s["id"]: s for s in state["strategies"]}
                        row = {"id": identity, "label": state["created_at"], "kind": kind, "mode": state["mode"], "status": state["status"],
                            "cohort": cohort, **({"cohort_mark": instruments_mark(state)} if instruments_mark(state) else {}),
                            "allocation_usdt": number(state["budget_usdt"]), "score": scored,
                            "created_at": state["created_at"], "duration_seconds": state["duration_seconds"],
                            "execution_mode": state.get("execution_mode", "rules"),
                            "score_policy": score_policy(state),
                            "competitive_validity": "ELIGIBLE" if eligible else "INELIGIBLE",
                            "settlement": "SETTLED" if state["status"] == "DONE" and all(p["costs"].get("complete") for p in bundle["participants"]) else "PENDING",
                            "bundle_hash": bundle["integrity"]["bundle_sha256"],
                            "participants": [{"id": p["id"], "recipe_sha256": p["recipe_sha256"], "version": p["version"],
                                              **public_identity(passports.get(p["id"]))} for p in bundle["participants"]]}
                        if live and state["status"] != "DONE":
                            self.recent[(kind, identity)] = (self.monotonic(), deepcopy(row))
                        else:  # finished or not driven by this process: its checkpoint's mtime and size key the row
                            if len(self.cache) >= 512: self.cache.pop(next(iter(self.cache)))
                            self.cache[key] = deepcopy(row)
                            self.recent.pop((kind, identity), None)
                    battles.append(deepcopy(row))
                except (OSError, ValueError, KeyError, TypeError):
                    battles.append({"id": identity, "kind": kind, "status": "UNKNOWN", "competitive_validity": "INELIGIBLE", "error": "evidence_unavailable"})
        battles = label_battles(battles)
        frequencies, cohorts = public_standings(battles)
        return {**deepcopy(self.data), "battles": battles, "frequencies": frequencies, "cohorts": cohorts, "target": None,
                "current_score_policy": deepcopy(SCORE_POLICY),
                "policy_note": "Нові бої: перемога — найбільший звірений net USDT після комісій і funding серед стратегій із хоча б одним виконаним входом. Без входу — поза рейтингом переможців; Cash = 0 лише еталон. Якщо входів немає, переможця немає. Перемога зі збитком не означає прибутковість. Старі бої зберігають свій регламент; групи за кодом, score, режимом, складом, allocation та вікном входів не змішуються. Суми й середні описові: сусідні бої не є незалежними доказами. Неповні результати не дають winner. Нові версії — лише з наступного lock після спільного feedback-вікна."}

    def action(self, generation, action, payload):
        with self.lock:
            previous = deepcopy(self.data)
            try: return self._action(generation, action, payload)
            except Exception:
                self.data = previous
                raise

    def _action(self, generation, action, payload):
        if generation != self.data["generation"]: raise ArenaError("Стан ліги змінився.")
        if not isinstance(payload, dict): raise ArenaError("Нечинний план ліги.")
        if action == "plan":
            if set(payload) != {"mode", "allocation_usdt", "duration_seconds", "participants", "tie_tolerance_usdt"}: raise ArenaError("Потрібен повний спільний регламент.")
            from .match_protocol import DURATIONS
            if payload["mode"] not in {"PAPER", "OKX_DEMO", "OKX_LIVE"} or payload["duration_seconds"] not in DURATIONS or not 25 <= Decimal(str(payload["allocation_usdt"])) <= 500:
                raise ArenaError("Непідтримувані умови.")
            ids = payload["participants"]
            if not isinstance(ids, list) or not 1 <= len(ids) <= 6 or len(set(ids)) != len(ids): raise ArenaError("Вибери1..6різних frozen рецептів.")
            if Decimal(str(payload["allocation_usdt"])) * len(ids) > 1000 or (payload["mode"] == "OKX_DEMO" and len(ids) != 1):
                raise ArenaError("Загальний allocation до1000USDT; Demo має один рецепт.")
            catalogue = {s["id"]: s for s in self.arena.catalogue() if s.get("workshop", {}).get("frozen_at")}
            if any(i not in catalogue for i in ids): raise ArenaError("Потрібні зафіксовані рецепти.")
            tolerance = Decimal(str(payload["tie_tolerance_usdt"]))
            if not tolerance.is_finite() or tolerance != Decimal("0.00000001"): raise ArenaError("Спільна точність нічиєї — 0.00000001 USDT.")
            record = {**deepcopy(payload), "id": uuid.uuid4().hex, "created_at_ms": int(self.clock()*1000),
                "recipe_hashes": {i: catalogue[i]["recipe_sha256"] for i in ids}, "automatic_start": False,
                "score_policy": deepcopy(SCORE_POLICY)}
            record["policy_sha256"] = fingerprint(record)
            self.data["plans"].append(record)
        elif action == "feedback":
            if set(payload) != {"plan_id", "bundle_hashes", "window"}: raise ArenaError("Потрібен однаковий feedback-пакет і вікно.")
            plan = next((p for p in self.data["plans"] if p["id"] == payload["plan_id"]), None)
            if not plan: raise ArenaError("План не знайдено.")
            if plan.get("score_policy") != SCORE_POLICY: raise ArenaError("Правило перемоги змінилося; потрібні новий план і спільне вікно.")
            hashes = {b["bundle_hash"] for b in self.status()["battles"] if b.get("settlement") == "SETTLED"}
            if not payload["bundle_hashes"] or any(h not in hashes for h in payload["bundle_hashes"]): raise ArenaError("Немає перевірених спільних bundle.")
            window = deepcopy(payload["window"])
            if not isinstance(window, dict): raise ArenaError("Потрібне спільне вікно покращення.")
            now = int(self.clock()*1000)
            if any(type(window.get(k)) is not int for k in ("opened_at_ms", "closed_at_ms", "training_cutoff_ms", "next_lock_at_ms")) or not (
                    now <= window["opened_at_ms"] < window["closed_at_ms"] <= window["next_lock_at_ms"] and window["training_cutoff_ms"] <= now):
                raise ArenaError("Вікно і cutoff мають бути визначені до покращення.")
            from .strategy_training import code_hash
            from .arena_engine import RULES
            from .exchange_engine import LIMITS
            window.update(feedback_released_at_ms=now, sdk_hash=code_hash(),
                feature_epoch_hash=hashlib.sha256((Path(__file__).parent / "arena_market.py").read_bytes()).hexdigest(),
                execution_policy_hash=fingerprint({"paper": RULES, "exchange": LIMITS}),
                common_bundle_hash=fingerprint(sorted(payload["bundle_hashes"])))
            record = {**deepcopy(payload), "window": window, "id": uuid.uuid4().hex, "released_at_ms": now}
            self.data["feedback"].append(record)
        elif action == "lock":
            if set(payload) != {"feedback_id", "candidates"}: raise ArenaError("Потрібні feedback і всі кандидати.")
            feedback = next((f for f in self.data["feedback"] if f["id"] == payload["feedback_id"]), None)
            if not feedback: raise ArenaError("Спільний feedback ще не видано.")
            from .strategy_training import code_hash
            from .arena_engine import RULES
            from .exchange_engine import LIMITS
            if feedback["window"]["sdk_hash"] != code_hash() or feedback["window"]["feature_epoch_hash"] != hashlib.sha256((Path(__file__).parent / "arena_market.py").read_bytes()).hexdigest() or feedback["window"]["execution_policy_hash"] != fingerprint({"paper": RULES, "exchange": LIMITS}):
                raise ArenaError("Рушій/ознаки/execution змінилися; потрібне нове спільне вікно.")
            if not self.workshop or not isinstance(payload["candidates"], list): raise ArenaError("Потрібні перевірені версії майстерні.")
            plan = next(p for p in self.data["plans"] if p["id"] == feedback["plan_id"])
            if plan.get("score_policy") != SCORE_POLICY: raise ArenaError("Правило перемоги змінилося; потрібні новий план і спільне вікно.")
            if len(payload["candidates"]) != len(plan["participants"]): raise ArenaError("Потрібне подання для кожного автора плану.")
            covered = set()
            candidates = []
            for submission in payload["candidates"]:
                if not isinstance(submission, dict) or set(submission) - {"version_id", "retain"} or "version_id" not in submission or type(submission.get("retain", False)) is not bool: raise ArenaError("Подавай ID версії; допуск обчислює майстерня.")
                version = self.workshop.version(submission["version_id"])
                retain = submission.get("retain", False)
                parent = version if retain else self.workshop.version(version["parent_id"]) if version.get("parent_id") else None
                if not parent or parent["id"] not in plan["participants"] or parent["id"] in covered or parent["recipe_sha256"] != plan["recipe_hashes"][parent["id"]]:
                    raise ArenaError("Lineage кандидата має відповідати одному учаснику зафіксованого плану.")
                covered.add(parent["id"])
                trials = [t for t in self.workshop.data["trials"] if t["version_id"] == version["id"]]
                qualified_trial = next((t for t in trials if t["id"] == version.get("qualification_trial_id")), None)
                qualified = bool(qualified_trial and qualified_trial["status"] == "COMPLETE" and
                    qualified_trial.get("strategy_sha256") == version["recipe_sha256"] and
                    qualified_trial.get("code_sha256") == feedback["window"]["sdk_hash"] and
                    qualified_trial.get("runtime") == "RULES_ONLY")
                candidate = {"version_id": version["id"], "recipe_hash": version["recipe_sha256"],
                    "parent_recipe_hash": parent["recipe_sha256"] if parent else None,
                    "qualified": qualified and version["status"] == "frozen" and version.get("qualification_code_sha256") == feedback["window"]["sdk_hash"],
                    "all_trials_logged": len(trials) == version["trial_count"] and all(t["status"] != "PENDING" for t in trials),
                    "submitted_at_ms": int(self.clock()*1000),
                    "training_latest_at_ms": training_calendar_cutoff(trials),
                    "synthetic_clock_exempt_trials": sum(t.get("dataset_kind") == "SYNTHETIC" and
                        t.get("runtime") == "RULES_ONLY" and t.get("status") in {"COMPLETE", "INCOMPLETE"} for t in trials),
                    **{k: feedback["window"][k] for k in ("sdk_hash", "feature_epoch_hash", "execution_policy_hash", "common_bundle_hash")}}
                candidate["retained"] = retain
                if not retain and datetime.fromisoformat(version["created_at"]).timestamp()*1000 < feedback["released_at_ms"]:
                    raise ArenaError("Нову версію створюй після спільного feedback.")
                candidates.append(candidate)
            checks = [upgrade_eligibility({**c, "parent_recipe_hash": "retained:"+c["parent_recipe_hash"]} if c["retained"] else c, feedback["window"], int(self.clock()*1000)) for c in candidates]
            if not checks or any(not c["eligible"] for c in checks): raise ArenaError("Кандидати не пройшли спільне вікно/epoch/qualification.")
            record = {**deepcopy(payload), "verified_candidates": candidates, "id": uuid.uuid4().hex, "locked_at_ms": int(self.clock()*1000), "automatic_start": False}
            self.data["locks"].append(record)
        else: raise ArenaError("Невідома дія ліги.")
        self.data["generation"] += 1
        write(self.path, self.data)
        return {"id": record["id"], "generation": self.data["generation"], "execution_started": False}
