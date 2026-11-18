"""Versioned strategy workshop: free replay plus reports from owner-started OKX Demo."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import threading
import hashlib
import json
import uuid

from .arena import IDENTITY, read, write
from .arena_strategies import ArenaError, validate_strategy, fingerprint
from .arena_market import PATHS
from .strategy_contract import author_contract, payload_of
from .strategy_training import SYNTHETIC, run_trial, read_recorded, code_hash
from .cycle_probe import is_probe, lifecycle


class Workshop:
    # The enclosing Arena owns the workspace's cross-process manager lock.
    def __init__(self, root, arena):
        self.root, self.arena = Path(root), arena
        self.path = self.root / "runtime" / "workshop_v1"
        self.manifest = self.path / "manifest.json"
        self.lock = threading.RLock()
        self.recorder = None
        self.data = read(self.manifest, {"generation": 0, "versions": [], "trials": []})

    def check_generation(self, generation):
        if type(generation) is not int or generation != self.data["generation"]:
            raise ArenaError("Стан майстерні змінився. Онови його та повтори дію.")

    def save(self):
        self.data["generation"] += 1
        write(self.manifest, self.data)

    def version(self, identity):
        if not isinstance(identity, str) or not IDENTITY.fullmatch(identity):
            raise ArenaError("Версію не знайдено.")
        result = next((v for v in self.data["versions"] if v["id"] == identity), None)
        if not result:
            raise ArenaError("Версію не знайдено.")
        if fingerprint(payload_of(result["strategy"])) != result["recipe_sha256"]:
            raise ArenaError("Рецепт змінено поза майстернею: перевірка хешу не пройшла.")
        return result

    def datasets(self):
        items = [deepcopy(SYNTHETIC)]
        with self.arena.lock:
            for identity in reversed(self.arena.data["seasons"]):
                season = self.arena.load(identity)
                path = self.arena.path / (identity + ".snapshots.jsonl")
                if season["status"] != "DONE" or not path.is_file():
                    continue
                # A label is not verification: read_recorded revalidates every raw response at training.
                try:
                    with path.open(encoding="utf-8") as file:
                        import json
                        first = json.loads(file.readline(262145))
                    if not isinstance(first.get("raw"), dict) or set(first["raw"]) != set(PATHS):
                        continue  # Never relabel a UI fixture as real recorded market data.
                except (OSError, ValueError, AttributeError):
                    continue
                items.append({"id": "recorded-" + identity, "name": "Запис арени · " + season["created_at"],
                              "admission_seconds": season["duration_seconds"],
                              "kind": "RECORDED_PUBLIC", "description": "Історичні публічні котирування; віртуальне виконання, без нових запитів."})
        if self.recorder:
            exports = self.recorder.root / "exports"
            for receipt_path in sorted(exports.glob("*.snapshots.jsonl.receipt.json")):
                try:
                    if receipt_path.is_symlink() or receipt_path.stat().st_size > 131072: continue
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                    identity = receipt["dataset_id"]
                    if not IDENTITY.fullmatch(identity) or receipt["schema"] != "recorded-training-tape-v1": continue
                    path = exports / (identity + ".snapshots.jsonl")
                    if path.is_symlink() or not path.is_file() or path.stat().st_size > 512*1024*1024: continue
                    elapsed = receipt["elapsed_recorded_seconds"]
                    items.append({"id": "capture-" + identity, "name": "Запис потоку · " + identity[:8],
                        "kind": "RECORDED_FIXTURE" if receipt.get("fixture_only") else "RECORDED_PUBLIC",
                        "admission_seconds": max(60, min(86400, elapsed - 15)), "source": receipt["source"],
                        "data_sha256": receipt["sha256"], "gaps": receipt["gaps_omitted_but_audited"],
                        "description": "Перевірений запис; пропуски окремо. Виконання модельне, без Jev чи ордерів."})
                except (OSError, ValueError, KeyError, TypeError): continue
        return items

    def status(self):
        with self.lock:
            versions = [{k: deepcopy(v) for k, v in row.items() if k not in {"strategy", "trial_ids"}}
                        for row in reversed(self.data["versions"])]
            return {"generation": self.data["generation"], "versions": versions,
                    "qualification_import": deepcopy(getattr(self, "qualification_import", {"status": "UNCHANGED", "imported": 0})),
                    "trials": deepcopy(list(reversed(self.data["trials"]))),
                    "datasets": self.datasets(), "contract": author_contract()}

    def detail(self, identity):
        with self.lock:
            result = deepcopy(self.version(identity))
            result["trials"] = [deepcopy(t) for t in self.data["trials"] if t["version_id"] == identity]
            return result

    def trial(self, identity):
        with self.lock:
            if (not isinstance(identity, str) or not IDENTITY.fullmatch(identity)
                    or not any(t["id"] == identity for t in self.data["trials"])):
                raise ArenaError("Тренування не знайдено.")
            return read(self.path / "trials" / (identity + ".json"))

    def add_version(self, generation, strategy, parent_id=None):
        with self.lock:
            self.check_generation(generation)
            parent = self.version(parent_id) if parent_id is not None else None
            # Each import is immutable, including failed ideas. Revisions preserve lineage.
            author = strategy.get("author", {}) if isinstance(strategy, dict) else {}
            prior = [v for v in self.data["versions"] if isinstance(strategy, dict) and isinstance(author, dict)
                     and v["name"] == strategy.get("name")
                     and all(v["author"].get(k) == author.get(k) for k in ("name", "kind", "model"))]
            number = max([v["version"] for v in prior] + ([parent["version"]] if parent else []), default=0) + 1
            clean = validate_strategy(strategy, number)
            record = {**deepcopy(clean), "status": "draft", "strategy": clean, "parent_id": parent_id,
                      "lineage_id": parent.get("lineage_id", parent_id) if parent else clean["id"],
                      "trial_count": 0, "trial_ids": [], "promoted_id": None}
            self.data["versions"].append(record)
            self.save()
            return {"id": clean["id"], "version": number}

    def train(self, generation, version_id, dataset_id, budget_usdt):
        with self.lock:
            self.check_generation(generation)
            version = self.version(version_id)
            if is_probe(version):
                raise ArenaError("Технічний рецепт перевіряється лише в OKX Demo; RULES_ONLY не виконує його сценарій.")
            identity = uuid.uuid4().hex
            pending = {"id": identity, "version_id": version_id, "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "dataset_id": dataset_id if isinstance(dataset_id, str) else None, "dataset_name": "Перевірка запиту",
                "dataset_kind": "UNKNOWN", "status": "PENDING", "runtime": "RULES_ONLY", "ai_cost_usd": 0,
                "strategy_sha256": version["recipe_sha256"], "code_sha256": None, "data_end_at_ms": None,
                "attempt_number": version["trial_count"] + 1, "lineage_parent_id": version["parent_id"],
                "summary": {}, "feedback": ["Спробу зарезервовано до виконання; перерваний процес не видає COMPLETE."], "runs": []}
            write(self.path / "trials" / (identity + ".json"), pending)
            self.data["trials"].append(deepcopy(pending))
            version["trial_ids"].append(identity)
            version["trial_count"] += 1
            self.save()
            try:
                dataset = next((d for d in self.datasets() if d["id"] == dataset_id), None)
                if dataset is None: raise ArenaError("dataset_unavailable")
                tapes = None
                if dataset["kind"] in {"RECORDED_PUBLIC", "RECORDED_FIXTURE"}:
                    if dataset_id.startswith("capture-"):
                        path = self.recorder.root / "exports" / (dataset_id.removeprefix("capture-") + ".snapshots.jsonl")
                        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != dataset["data_sha256"]:
                            raise ArenaError("recording_hash_mismatch")
                    else:
                        path = self.arena.path / (dataset_id.removeprefix("recorded-") + ".snapshots.jsonl")
                    tape = read_recorded(path)
                    duration = dataset.get("admission_seconds", min(86400, tape[-1]["at"] - tape[0]["at"] - 15))
                    tapes = [(dataset["name"], tape, duration)]
                result = run_trial(version["strategy"], dataset, budget_usdt, tapes)
                result.update(id=identity, attempt_number=pending["attempt_number"], lineage_parent_id=version["parent_id"])
            except (OSError, ValueError, KeyError, TypeError) as error:
                result = {**pending, "status": "FAILED", "error_code": "training_validation_or_io",
                    "feedback": ["Спроба не завершилася. Перевір рецепт/дані/бюджет; ця помилка збережена в історії."]}
                write(self.path / "trials" / (identity + ".json"), result)
                self.data["trials"][-1] = deepcopy(result)
                self.save()
                raise ArenaError("Проба не виконана; FAILED записано в історію.") from None
            write(self.path / "trials" / (result["id"] + ".json"), result)
            summary = {k: deepcopy(v) for k, v in result.items() if k not in {"runs", "feedback", "strategy"}}
            self.data["trials"][-1] = summary
            self.save()
            return {"id": result["id"], "status": result["status"]}

    def freeze(self, generation, version_id):
        with self.lock:
            self.check_generation(generation)
            version = self.version(version_id)
            if is_probe(version):
                raise ArenaError("Технічна перевірка не є стратегією для змагання й не допускається на арену.")
            from .exchange_lab import source_hash as exchange_hash
            eligible = [t for t in self.data["trials"] if t["version_id"] == version_id and t["status"] == "COMPLETE"
                        and t["strategy_sha256"] == version["recipe_sha256"] and t["code_sha256"] == (
                            exchange_hash() if t.get("dataset_kind") == "OKX_DEMO_FORWARD" else code_hash())]
            if not eligible:
                raise ArenaError("Спершу заверши хоча б один тест саме цієї версії на поточному рушії. Прибуток не є умовою допуску.")
            if version["status"] == "frozen" and version.get("qualification_code_sha256") == eligible[-1]["code_sha256"]:
                return {"id": version_id, "status": "frozen"}
            if version["status"] == "frozen":
                version.setdefault("qualification_history", []).append({k: version.get(k) for k in
                    ("qualification_trial_id", "qualification_code_sha256", "qualification_runtime", "frozen_at")})
            version.update(status="frozen", frozen_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                           qualification_trial_id=eligible[-1]["id"], qualification_code_sha256=eligible[-1]["code_sha256"],
                           qualification_runtime=eligible[-1]["runtime"])
            self.save()
            return {"id": version_id, "status": "frozen"}

    def promote(self, generation, version_id):
        with self.lock:
            self.check_generation(generation)
            version = self.version(version_id)
            if is_probe(version):
                raise ArenaError("Технічна перевірка не допускається на арену.")
            if version["status"] != "frozen":
                raise ArenaError("Перед ареною зафіксуй рецепт.")
            from .exchange_lab import source_hash as exchange_hash
            demo_runtimes = {"OKX_DEMO_SHARED_JEV", "OKX_DEMO_RULES_ONLY", "OKX_DEMO_MECHANICS"}
            expected_hash = exchange_hash() if version.get("qualification_runtime") in demo_runtimes else code_hash()
            if version["qualification_code_sha256"] != expected_hash:
                raise ArenaError("Рушій оновлено. Створи нову версію та перевір її перед допуском.")
            result = self.arena.import_frozen_strategy(version["strategy"], {
                "version_id": version_id, "frozen_at": version["frozen_at"],
                "qualification_trial_id": version["qualification_trial_id"],
                "training_attempts": version["trial_count"], "parent_id": version["parent_id"],
                "training_runtime": version.get("qualification_runtime", "RULES_ONLY")})
            version["promoted_id"] = result["id"]
            self.save()
            return result

    def record_exchange_trial(self, state):
        """Idempotent actual Demo report; never substitutes simulated order metrics."""
        if state["context"] != "workshop_demo" or state["mode"] != "OKX_DEMO" or state["status"] != "DONE":
            raise ArenaError("Звіт потребує завершеного OKX Demo запуску.")
        with self.lock:
            if any(t["id"] == state["id"] for t in self.data["trials"]):
                return
            strategy, bot = state["strategies"][0], state["bots"][0]
            version = self.version(strategy["id"])
            if strategy["recipe_sha256"] != version["recipe_sha256"]:
                raise ArenaError("Звіт стосується іншого рецепта.")
            complete = (not state["incomplete"] and not bot["position_btc"] and bot["accounting_reconciled"]
                        and bot["funding_reconciled"] and bot["ai_cost_complete"])
            metrics = {k: deepcopy(bot[k]) for k in ("pnl_usdt", "return_pct", "max_drawdown_pct", "trades", "fees_usdt", "funding_usdt")}
            summary = {**metrics, "complete_runs": int(complete), "total_runs": 1}
            tape = self.root / "runtime" / "exchange_v1" / (state["id"] + ".snapshots.jsonl")
            import hashlib
            data_hash = hashlib.sha256(tape.read_bytes() if tape.is_file() else b"").hexdigest()
            execution_mode = state.get("execution_mode", "shared_jev")
            runtime = {"shared_jev": "OKX_DEMO_SHARED_JEV", "rules_only": "OKX_DEMO_RULES_ONLY",
                       "demo_mechanics": "OKX_DEMO_MECHANICS"}[execution_mode]
            report = {"id": state["id"], "version_id": version["id"], "created_at": state["created_at"],
                      "dataset_id": "okx-demo-forward-" + state["id"], "dataset_name": "OKX Demo · актуальний ринок",
                      "dataset_kind": "OKX_DEMO_FORWARD", "status": "COMPLETE" if complete else "INCOMPLETE",
                      "budget_usdt": state["budget_usdt"], "runtime": runtime, "execution_mode": execution_mode,
                      "ai_cost_usd": bot["ai_cost_usd"] if bot["ai_cost_complete"] else None,
                      "summary": summary, "feedback": ["Demo: фактичні демоордери, комісії/funding звірено з OKX." if complete else
                          "Неповна звірка або перерваний запуск: допуск не надається.",
                          "Demo ліквідність і результати не доводять якість виконання чи прибутковість на Live."],
                      "runs": [{"name": "OKX Demo", "status": "DONE", "incomplete": not complete,
                                "metrics": {**metrics, "remaining_btc": bot["position_btc"], "cash_pnl_usdt": 0},
                                "curve": deepcopy(bot["curve"]), "events": deepcopy(state["events"])}],
                      "strategy": payload_of(strategy), "strategy_sha256": strategy["recipe_sha256"],
                      "data_sha256": data_hash, "code_sha256": state["code_sha256"],
                      "attempt_number": version["trial_count"] + 1, "lineage_parent_id": version["parent_id"]}
            report["lifecycle"] = lifecycle(state)
            if is_probe(strategy):
                report["feedback"].insert(0, "Технічний сценарій Demo: покриття етапів відокремлено від фінансової звірки; не кандидат на арену.")
            write(self.path / "trials" / (report["id"] + ".json"), report)
            self.data["trials"].append({k: deepcopy(v) for k, v in report.items() if k not in {"runs", "feedback", "strategy"}})
            version["trial_ids"].append(report["id"])
            version["trial_count"] += 1
            self.save()
