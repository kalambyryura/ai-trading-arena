"""Owner-local operations CLI. Fixture QA requires explicit --fixture/temp roots.

Record/stop-recording are public-data lifecycle operations and never trading
Start/Stop/Resume. Owner paid SHADOW is separately explicit and never executes
an order. No operation runs merely by importing this module.
"""
from copy import deepcopy
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from trading_lab.ops_service import PublicRecorderRepository, BackupRepository, FixturePublicSequence, read_json
from trading_lab.public_recorder import OwnerPublicGet
from trading_lab.owner_shadow import OwnerShadowEpisodeRunner


def repository_imports():
    repository = next((p for p in Path(__file__).resolve().parents if (p / "trading_lab" / "arena_market.py").is_file()), None)
    if repository is None:
        raise ValueError("repository_modules_missing")
    if str(repository) not in sys.path:
        sys.path.insert(0, str(repository))
    from trading_lab.arena_market import build_snapshot
    from trading_lab.arena_jev import SharedJevGate
    return build_snapshot, SharedJevGate


def spawn_recorder_child(argv):
    """Exactly this CLI, no shell interpolation; Windows helper window hidden."""
    args = [sys.executable, "-B", str(Path(__file__).resolve()), *[a for a in argv if a != "--background"]]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, shell=False, creationflags=flags,
                               start_new_session=os.name != "nt")
    return {"status": "PUBLIC_RECORDER_DISPATCHED", "worker_pid": process.pid,
            "affects_trading": False, "paid_calls": False}


def parser_for_cli():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("record", "status", "stop-recording", "datasets", "export-tape"):
        command = commands.add_parser(name)
        command.add_argument("--service-root", type=Path, required=True)
        if name == "record":
            modes = command.add_mutually_exclusive_group(required=True)
            modes.add_argument("--fixture", action="store_true")
            modes.add_argument("--owner-public-read", action="store_true")
            command.add_argument("--background", action="store_true")
            command.add_argument("--interval", type=float, default=5)
            command.add_argument("--max-observations", type=int, default=17280)
            command.add_argument("--epoch-ticks", type=int, default=720)
        if name == "datasets":
            command.add_argument("--offset", type=int, default=0)
            command.add_argument("--limit", type=int, default=50)
        if name == "export-tape":
            command.add_argument("--dataset", required=True)
            command.add_argument("--output", type=Path, required=True)
    for name in ("backup-create", "backup-verify", "backup-restore-isolated"):
        command = commands.add_parser(name)
        command.add_argument("--current-runtime", type=Path, required=True)
        command.add_argument("--backup-root", type=Path, required=True)
        command.add_argument("--restore-root", type=Path, required=True)
        command.add_argument("--backup-id")
        if name == "backup-create":
            command.add_argument("--files-json", type=Path, required=True,
                                 help="Explicit {backup_filename: source_relative_path} manifest; no credential files")
        if name == "backup-restore-isolated":
            command.add_argument("--restore-id")
    shadow = commands.add_parser("shadow-episode")
    shadow.add_argument("--root", type=Path, required=True, help="Existing project ledger root; fixture QA uses temp root")
    shadow.add_argument("--season", required=True)
    shadow.add_argument("--episode", required=True)
    shadow.add_argument("--context", choices=("paper", "workshop_demo", "arena_live"), default="arena_live",
                        help="Existing E037/E038/E039 series ledger; never creates a replacement budget")
    shadow.add_argument("--input-json", type=Path, required=True, help="Strict public {context,setups} only")
    modes = shadow.add_mutually_exclusive_group(required=True)
    modes.add_argument("--fixture", action="store_true")
    modes.add_argument("--owner-paid-shadow", action="store_true")
    return parser


def run_cli(argv):
    parser = parser_for_cli()
    args = parser.parse_args(argv)
    build_snapshot, SharedJevGate = repository_imports()
    if args.command in {"record", "status", "stop-recording", "datasets", "export-tape"}:
        if args.command == "record" and args.background:
            if args.owner_public_read and args.interval < 1:
                raise ValueError("public_recording_interval")
            return spawn_recorder_child(argv)
        is_fixture = bool(getattr(args, "fixture", False))
        source = "FIXTURE" if is_fixture else "OKX_LIVE_PUBLIC"
        factory = FixturePublicSequence if is_fixture else OwnerPublicGet
        options = {"epoch_ticks": args.epoch_ticks} if args.command == "record" else {}
        repository = PublicRecorderRepository(args.service_root, build_snapshot_fn=build_snapshot,
                                              get_factory=factory, source=source, recorder_options=options)
        if args.command == "status":
            return repository.status()
        if args.command == "datasets":
            return repository.list_datasets(offset=args.offset, limit=args.limit)
        if args.command == "stop-recording":
            return repository.request_stop_recording()
        if args.command == "export-tape":
            return repository.export_training_tape(args.dataset, args.output)
        repository.start_recording(owner_local=args.owner_public_read, interval_seconds=args.interval,
                                   max_observations=args.max_observations)
        try:
            while repository.worker.is_alive():
                repository.worker.join(.25)
        except KeyboardInterrupt:
            repository.request_stop_recording()
            repository.worker.join(30)
        return repository.status()
    if args.command.startswith("backup-"):
        repository = BackupRepository(args.current_runtime, args.backup_root, args.restore_root)
        if args.command == "backup-create":
            return repository.create(read_json(args.files_json), backup_id=args.backup_id)
        if not args.backup_id:
            parser.error("--backup-id required")
        if args.command == "backup-verify":
            return repository.verify(args.backup_id)
        return repository.restore_isolated(args.backup_id, restore_id=args.restore_id)
    packet = read_json(args.input_json, 16384)
    if not isinstance(packet, dict) or set(packet) != {"context", "setups"}:
        raise ValueError("strict_market_shadow_input")
    from trading_lab.arena_jev import ArenaJevLedger
    from trading_lab.exchange_lab import LEDGERS, POLICIES
    ledger = ArenaJevLedger(args.root) if args.context == "paper" else ArenaJevLedger(args.root,
        relative_path="runtime/exchange_v1/" + LEDGERS[args.context], budget=POLICIES[args.context])
    if args.fixture:
        if packet["context"].get("source") != "FIXTURE":
            raise ValueError("fixture_shadow_public_context_required")
        from trading_lab.shadow_jev import build_pack
        pack = build_pack(packet["context"], packet["setups"])

        class FakeTypedTransport:
            is_fake = True

            def __call__(self, body, key):
                answers = {}
                for name, question in pack.payload["questions"].items():
                    kind, criteria = question["type"], question["criteria"]
                    if kind == "choice":
                        choice = next(iter(criteria))
                        answers[name] = {"type": kind, "choice": choice,
                                         "probabilities": {k: int(k == choice) for k in criteria}, "confidence": .8}
                    elif kind == "score":
                        levels = {str(i): v for i, v in enumerate(criteria)}
                        answers[name] = {"type": kind, "score": 2, "legend": levels,
                                         "probabilities": {k: int(k == "2") for k in levels}, "confidence": .8}
                    else:
                        answers[name] = {"type": "noul", "noul": .5}
                return {"answers": answers, "usage": {"cost": .00001, "input_tokens": 100, "output_tokens": 50}}

        gate = SharedJevGate(args.root, transport=FakeTypedTransport(), key_reader=lambda _: "fixture-key",
                             clock=lambda: 1000, timer=lambda: 0, ledger=ledger)
    else:
        gate = SharedJevGate(args.root, ledger=ledger)  # Only explicit owner-paid-shadow reaches this branch.
    runner = OwnerShadowEpisodeRunner(gate, fixture_only=args.fixture, owner_enabled=args.owner_paid_shadow)
    result = runner.evaluate_episode(args.season, args.episode, packet["context"], packet["setups"])
    return {**result, "usage": gate.ledger.summary(args.season)}


def main():
    try:
        result = run_cli(sys.argv[1:])
    except (ValueError, OSError) as error:
        # No provider text/key/private file contents in output.
        print(json.dumps({"status": "OPERATION_FAILED", "reason": "local_operation_validation_or_io",
                          "execution_permitted": False}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
