"""Standalone public recorder reference with injected GET transport and clocks.

No account profile, credential, Jev, exchange worker or order imports. Importing
does no I/O. Recorder epoch/capacity completion ends recording only. CLI has an
explicit owner public-GET mode; assistant QA uses only --fixture/temp roots.
"""
from copy import deepcopy
import argparse
import hashlib
import json
from pathlib import Path
import re
import time
import urllib.request
import uuid

from .ops_policy import ClockGuard
from .ops_storage import SegmentedJournal, StorageBlocked, atomic_write, encode

BASE = "https://openapi.okx.com"
PATHS = {
    "clock": "/api/v5/public/time",
    "ticker": "/api/v5/market/ticker?instId=BTC-USDT-SWAP",
    "candles": "/api/v5/market/candles?instId=BTC-USDT-SWAP&bar=1m&limit=31",
    "funding": "/api/v5/public/funding-rate?instId=BTC-USDT-SWAP",
    "instrument": "/api/v5/public/instruments?instType=SWAP&instId=BTC-USDT-SWAP",
}
FIELDS = {
    "clock": {"ts"},
    "ticker": {"instId", "instType", "bidPx", "bidSz", "askPx", "askSz", "last", "ts"},
    "funding": {"instId", "fundingRate", "fundingTime", "nextFundingTime", "ts"},
    "instrument": {"instId", "instType", "state", "ctType", "ctValCcy", "settleCcy", "ctVal", "ctMult", "lotSz", "minSz", "tickSz", "groupId"},
}
NUMBER_TEXT = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
PUBLIC_ENUMS = {"instId": "BTC-USDT-SWAP", "instType": "SWAP", "state": "live",
                "ctType": "linear", "ctValCcy": "BTC", "settleCcy": "USDT"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


class OwnerPublicGet:
    """No secrets or signed headers are accepted by this fixed GET-only adapter."""
    def __init__(self):
        self.opener = urllib.request.build_opener(NoRedirect())
        self.calls = 0

    def __call__(self, name):
        if name not in PATHS:
            raise ValueError("public_path_not_allowed")
        self.calls += 1
        request = urllib.request.Request(BASE + PATHS[name], method="GET", headers={
            "Accept": "application/json", "Cache-Control": "no-cache", "User-Agent": "ArenaPublicRecorder/1"})
        with self.opener.open(request, timeout=6) as response:
            payload = response.read(524289)
        if len(payload) > 524288:
            raise ValueError("public_response_size")
        envelope = json.loads(payload)
        if not isinstance(envelope, dict) or envelope.get("code") != "0" or not isinstance(envelope.get("data"), list):
            raise ValueError("public_envelope")
        return envelope["data"]


def public_projection(raw):
    if not isinstance(raw, dict) or set(raw) != set(PATHS):
        raise ValueError("public_endpoint_set")
    result = {}
    for kind, fields in FIELDS.items():
        rows = raw[kind]
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise ValueError("public_row_shape")
        if any(not isinstance(rows[0].get(k), str) for k in fields & rows[0].keys()):
            raise ValueError("public_row_value_type")
        for key in fields & rows[0].keys():
            value = rows[0][key]
            if key in PUBLIC_ENUMS:
                if value != PUBLIC_ENUMS[key]:
                    raise ValueError("public_enum_value")
            elif not (value == "" and key in {"ctMult", "nextFundingTime"}):
                if len(value) > 64 or not NUMBER_TEXT.fullmatch(value):
                    raise ValueError("public_numeric_value")
        # Projection scope is fixed; additional provider columns never become an
        # accidental private/free-prose field in future replay/export consumers.
        result[kind] = [{key: rows[0][key] for key in sorted(fields) if key in rows[0]}]
    candles = raw["candles"]
    if (not isinstance(candles, list) or not 25 <= len(candles) <= 31
            or any(not isinstance(row, list) or len(row) != 9
                   or any(not isinstance(v, str) or len(v) > 64 or not NUMBER_TEXT.fullmatch(v) for v in row)
                   or row[8] not in {"0", "1"} for row in candles)):
        raise ValueError("public_candle_shape")
    result["candles"] = deepcopy(candles)
    return result


class PublicRecorder:
    def __init__(self, root, *, get_public, build_snapshot_fn, source="FIXTURE", wall=time.time, mono=time.monotonic,
                 epoch_ticks=720, max_epochs=32, dataset_max_bytes=256 * 1024 * 1024, journal_options=None):
        if source not in {"FIXTURE", "OKX_LIVE_PUBLIC"} or not callable(get_public) or not callable(build_snapshot_fn):
            raise ValueError("public_recorder_source")
        if type(epoch_ticks) is not int or type(max_epochs) is not int or min(epoch_ticks, max_epochs) <= 0:
            raise ValueError("recorder_epoch_policy")
        if max_epochs > 4096 or type(dataset_max_bytes) is not int or dataset_max_bytes <= 0:
            raise ValueError("recorder_capacity_policy")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        if (self.root / "manifest.json").exists():
            raise ValueError("recorder_dataset_exists_no_overwrite")
        self.get_public, self.build_snapshot = get_public, build_snapshot_fn
        self.wall, self.mono, self.source = wall, mono, source
        self.epoch_ticks, self.max_epochs, self.dataset_max_bytes = epoch_ticks, max_epochs, dataset_max_bytes
        self.journal_options = dict(journal_options or {})
        self.guard, self.instrument = ClockGuard(), None
        self.journal, self.epoch, self.epoch_count, self.samples, self.used_bytes = None, 0, 0, 0, 0
        self.epoch_attempts = self.attempts = 0
        self.state = {"schema": "public-recording-v1", "source": source, "instrument": "BTC-USDT-SWAP",
                      "raw_scope": "replay-input-projection-v1", "feature_version": "market-features-v1",
                      "boot_id": uuid.uuid4().hex,
                      "status": "RECORDING", "affects_trading": False, "samples": 0, "epochs": [],
                      "observations": 0, "gaps": 0,
                      "private_fields": False, "actual_elapsed_24h_proven": False}
        self._save()

    def _save(self):
        atomic_write(self.root / "manifest.json", encode(self.state))

    def _open_epoch(self):
        if self.epoch >= self.max_epochs:
            self.stop_recording("recorder_epoch_cap")
            return False
        self.epoch += 1
        self.epoch_count = 0
        self.epoch_attempts = 0
        name = f"epoch-{self.epoch:08}"
        self.journal = SegmentedJournal(self.root / name, **self.journal_options)
        self.state["epochs"].append({"directory": name, "status": "RECORDING", "samples": 0,
                                     "observations": 0,
                                     "first_sample": self.samples + 1, "last_sample": self.samples,
                                     "last_event_sha256": "0" * 64})
        self._save()
        return True

    def stop_recording(self, reason):
        if self.state["status"] != "RECORDING":
            return deepcopy(self.state)
        self.state.update(status="STOPPED_RECORDING", reason=reason, affects_trading=False)
        if self.state["epochs"]:
            self.state["epochs"][-1]["status"] = "SEALED"
        self._save()
        return self.state

    def tick(self):
        if self.state["status"] != "RECORDING":
            return deepcopy(self.state)
        if self.journal is None or self.epoch_attempts >= self.epoch_ticks:
            if self.state["epochs"]:
                self.state["epochs"][-1]["status"] = "SEALED"
            if not self._open_epoch():
                return deepcopy(self.state)
        before_wall, before_mono = self.wall(), self.mono()
        self.epoch_attempts += 1
        self.attempts += 1
        self.state["observations"] = self.attempts
        self.state["epochs"][-1]["observations"] = self.epoch_attempts
        try:
            if self.instrument is None:
                self.instrument = self.get_public("instrument")
            raw = {"instrument": self.instrument, "candles": self.get_public("candles"),
                   "funding": self.get_public("funding"), "ticker": self.get_public("ticker"), "clock": self.get_public("clock")}
            after_wall, after_mono = self.wall(), self.mono()
            projected = public_projection(raw)
            snapshot = self.build_snapshot(projected, after_wall)
            quality = self.guard.observe(wall=after_wall, mono=after_mono, quote_ms=int(snapshot["quote_ts"]))
            record = {"mode": "FIXTURE" if self.source == "FIXTURE" else "OKX_LIVE", "at": snapshot["at"],
                      "sha256": snapshot["sha256"], "raw": projected,
                      "capture": {"boot_id": self.state["boot_id"], "sample_seq": self.samples + 1,
                                  "wall_sent_at": before_wall, "wall_received_at": after_wall,
                                  "mono_sent": before_mono, "mono_received": after_mono, "quality": quality}}
            size = len(encode(record))
            if self.used_bytes + size > self.dataset_max_bytes:
                return deepcopy(self.stop_recording("recorder_dataset_quota"))
            receipt = self.journal.append({"kind": "public_market", "record": record})
            self.used_bytes += size
            self.samples += 1
            self.epoch_count += 1
            current = self.state["epochs"][-1]
            current.update(samples=self.epoch_count, last_sample=self.samples, last_event_sha256=receipt["sha256"])
            self.state.update(samples=self.samples, projected_record_bytes=self.used_bytes)
            self._save()
        except StorageBlocked:
            return deepcopy(self.stop_recording("recorder_storage_capacity"))
        except Exception:
            # No retry inside a quantum, and no private provider error text.
            # Persist the missing public observation; later independent samples
            # remain eligible for validation and continuity re-baselining.
            gap = {"kind": "public_gap", "reason": "public_observation_unavailable_or_invalid",
                   "observation_seq": self.attempts, "wall_sent_at": before_wall,
                   "mono_sent": before_mono, "boot_id": self.state["boot_id"], "affects_trading": False}
            size = len(encode(gap))
            if self.used_bytes + size > self.dataset_max_bytes:
                return deepcopy(self.stop_recording("recorder_dataset_quota"))
            try:
                receipt = self.journal.append(gap)
                self.used_bytes += size
                self.state["gaps"] += 1
                self.state["projected_record_bytes"] = self.used_bytes
                self.state["epochs"][-1]["last_event_sha256"] = receipt["sha256"]
                self._save()
            except (StorageBlocked, OSError):
                return deepcopy(self.stop_recording("recorder_storage_capacity"))
        return deepcopy(self.state)

    def replay_snapshot(self, record):
        result = self.build_snapshot(record["raw"], record["at"])
        if result["sha256"] != record["sha256"]:
            raise ValueError("recorded_public_snapshot_hash_mismatch")
        return result


def fixture_raw():
    end = 60_000_000 + 30 * 60_000
    rows = [[str(end - (30 - i) * 60_000), str(100 + i), str(102 + i), str(99 + i), str(101 + i),
             "1000", "0", "0", "1"] for i in range(30)]
    return {"instrument": [{"instId": "BTC-USDT-SWAP", "instType": "SWAP", "state": "live", "ctType": "linear",
                             "ctValCcy": "BTC", "settleCcy": "USDT", "ctVal": "0.01", "ctMult": "1",
                             "lotSz": "0.01", "minSz": "0.01", "tickSz": "0.1", "groupId": "1"}],
            "ticker": [{"instId": "BTC-USDT-SWAP", "bidPx": "130", "askPx": "130.01", "bidSz": "10", "askSz": "10", "ts": str(end)}],
            "funding": [{"instId": "BTC-USDT-SWAP", "fundingRate": "0.0001", "fundingTime": str(end + 28_800_000), "ts": str(end)}],
            "clock": [{"ts": str(end)}], "candles": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fixture", action="store_true")
    mode.add_argument("--owner-public-record", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ticks", type=int, default=12)
    parser.add_argument("--epoch-ticks", type=int, default=720)
    args = parser.parse_args()
    if not 1 <= args.ticks <= 17280:
        parser.error("ticks must be 1..17280; admission/trading is unaffected")
    # A copied tools/ CLI resolves the repository via its parent; the research
    # copy is runnable from any cwd without requiring PYTHONPATH configuration.
    import sys
    repository = next((p for p in Path(__file__).resolve().parents if (p / "trading_lab" / "arena_market.py").is_file()), None)
    if repository is None:
        parser.error("repository with trading_lab/arena_market.py not found")
    sys.path.insert(0, str(repository))
    from trading_lab.arena_market import build_snapshot
    if args.fixture:
        raw = fixture_raw()
        get_public, wall = lambda name: deepcopy(raw[name]), lambda: int(raw["clock"][0]["ts"]) / 1000
    else:
        get_public, wall = OwnerPublicGet(), time.time
    recorder = PublicRecorder(args.output, get_public=get_public, build_snapshot_fn=build_snapshot,
                              source="FIXTURE" if args.fixture else "OKX_LIVE_PUBLIC", wall=wall, epoch_ticks=args.epoch_ticks)
    for _ in range(args.ticks):
        if recorder.tick()["status"] != "RECORDING":
            break
        if args.owner_public_record:
            time.sleep(5)
    recorder.stop_recording("owner_collection_quantum_completed")
    print(json.dumps(recorder.state, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
