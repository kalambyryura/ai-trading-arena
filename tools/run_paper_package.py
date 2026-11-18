"""Owner-started portable PAPER pilot, with immutable inputs and no private API route."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.run_isolated_paper import public_request_allowed

SCHEMA = 'jev-portable-paper/v2'
ONCE_MARKER = 'runtime/portable-paper-once.json'


def _history_ids(root):
    result = set()
    for path in (root / 'runtime/arena_v4').iterdir():
        match = re.match(r'([a-fA-F0-9]{32})(?:[.]|$)', path.name)
        if match:
            result.add(match.group(1).lower())
    return result


def _reserve_json(root, relative, document):
    """Durable create-only evidence. A failed/partial reservation is never reset."""
    path = root / relative
    if not path.resolve().is_relative_to(root):
        raise ValueError('portable_marker_path')
    for parent in [path, *path.parents]:
        if parent == root:
            break
        if parent.is_symlink() or parent.is_junction():
            raise ValueError('portable_marker_path')
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('x', encoding='utf-8') as marker:
            json.dump(document, marker, ensure_ascii=False, allow_nan=False)
            marker.flush()
            os.fsync(marker.fileno())
    except FileExistsError:
        return False
    return True


def _reserve_start(root, package, duration, existing_id=None):
    return _reserve_json(root, ONCE_MARKER, {
        'schema': 'portable-paper-once/v1', 'reserved_at': datetime.now(timezone.utc).isoformat(),
        'source_content_sha256': package.get('source_content_sha256'),
        'catalogue_sha256': package.get('catalogue_sha256'),
        'admission_seconds': duration, 'existing_run_id': existing_id,
        'automatic_repeat': False})


def _reserve_export(root, summary):
    identity, status = summary['id'], summary['status']
    if not isinstance(identity, str) or not re.fullmatch('[a-f0-9]{32}', identity) or status not in {'DONE', 'PAUSED'}:
        raise ValueError('portable_terminal_state')
    return _reserve_json(root, 'runtime/portable-export-attempts/' + identity + '-' + status + '.json', {
        'schema': 'portable-paper-export-attempt/v1', 'id': identity, 'status': status,
        'last_event_seq': summary['events'], 'attempted_at': datetime.now(timezone.utc).isoformat()})


def verify_package(root):
    root = Path(root).resolve()
    package = json.loads((root / 'paper-package.json').read_text(encoding='utf-8'))
    if (package.get('schema') != SCHEMA or package.get('mode') != 'PAPER'
            or package.get('execution_mode') != 'rules'
            or package.get('budget_usdt_per_bot') != 100
            or package.get('admission_options_seconds') != [18000]):
        raise ValueError('invalid_portable_paper_protocol')
    if any(root.glob('.env*')) or (root / 'LocalSettings/exchange_accounts.json').exists():
        raise ValueError('paper_package_must_not_contain_keys')
    pins = package.get('files')
    if not isinstance(pins, dict) or not pins:
        raise ValueError('source_pins_missing')
    for relative, expected in pins.items():
        path = (root / relative).resolve()
        if (not path.is_relative_to(root) or path.is_symlink() or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected):
            raise ValueError('source_or_plan_changed')
    # Refuse extra imported code; a mutable runtime is the only unpinned subtree.
    for folder in ('trading_lab', 'tools'):
        if any(p.relative_to(root).as_posix() not in pins for p in (root / folder).rglob('*.py')):
            raise ValueError('unexpected_source_file')
    roster = json.loads((root / 'runtime/arena_v4/manifest.json').read_text(encoding='utf-8'))
    seasons = roster.get('seasons')
    if (not isinstance(seasons, list) or any(not isinstance(i, str) or not re.fullmatch('[a-f0-9]{32}', i) for i in seasons)
            or len(set(seasons)) != len(seasons)
            or roster.get('active_id') is not None and roster.get('active_id') not in seasons):
        raise ValueError('invalid_observation_index')
    if _history_ids(root) - set(seasons):
        raise ValueError('unindexed_observation_history')
    actual = {s['id']: s['recipe_sha256'] for s in roster['strategies']}
    if actual != package.get('recipes') or not 1 <= len(actual) <= 6:
        raise ValueError('prepared_roster_changed')
    from trading_lab.strategy_contract import payload_of
    from trading_lab.arena_strategies import fingerprint, validate_strategy
    if fingerprint(roster['strategies']) != package.get('catalogue_sha256'):
        raise ValueError('prepared_catalogue_changed')
    for strategy in roster['strategies']:
        if fingerprint(payload_of(strategy)) != strategy['recipe_sha256']:
            raise ValueError('recipe_payload_changed')
        validate_strategy(payload_of(strategy))
    return package, roster


def run(root, *, duration=18000, port=8766, view_only=False):
    root = Path(root).resolve()
    package, roster = verify_package(root)
    if duration not in package['admission_options_seconds']:
        raise ValueError('duration_not_prepared')
    for name in list(os.environ):
        if name.upper().startswith(('OKX_', 'JEV_', 'TYPESAFE_')):
            del os.environ[name]
    import trading_lab.arena as arena_module
    from trading_lab.web_panel import Panel, Server, Handler
    if Path(arena_module.__file__).resolve().parent.parent != root:
        raise ValueError('wrong_source_copy')
    original_open = urllib.request.OpenerDirector.open
    outbound = {'public_gets': 0, 'blocked_requests': 0}

    def guarded_open(opener, request, data=None, *args, **kwargs):
        if not public_request_allowed(request, data):
            outbound['blocked_requests'] += 1
            raise ValueError('portable_public_get_only')
        outbound['public_gets'] += 1
        return original_open(opener, request, data, *args, **kwargs)

    class PaperViewer(Handler):
        def do_POST(self):
            # These two routes manage only virtual positions, and require an owner click.
            if self.path in {'/api/arena/stop', '/api/arena/resume'}:
                return super().do_POST()
            return self.send(403, {'error': 'PAPER-пакет: склад зафіксований, реальні угоди та платні запити вимкнені.'})

    urllib.request.OpenerDirector.open = guarded_open
    try:
        with Server(Panel(root), port) as server:
            server.RequestHandlerClass = PaperViewer
            # Recheck while the Server owns the manager lock, before reserving one shot.
            package, roster = verify_package(root)
            if not roster.get('seasons') and not view_only:
                if not _reserve_start(root, package, duration):
                    raise ValueError('one_shot_already_reserved_keep_history')
                server.arena.start(server.arena.data['generation'], list(package['recipes']),
                                   100, duration, execution_mode='rules')
            elif roster.get('seasons'):
                # Preserve the boundary for packages started before this marker existed.
                _reserve_start(root, package, duration, roster.get('active_id'))
                print('Existing observation opened. No new round or automatic resume.', flush=True)
            server_thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .5},
                                             name='paper-viewer', daemon=True)
            server_thread.start()
            print(f'PAPER viewer: http://127.0.0.1:{port}/arena?mode=paper', flush=True)
            export_attempted = set()
            try:
                while True:
                    with server.arena.lock:
                        state = server.arena.session
                        summary = None if state is None else {
                            'schema': 'portable-paper-observation/v2', 'mode': 'PAPER', 'execution_mode': 'rules',
                            'pid': os.getpid(), 'id': state['id'], 'status': server.arena.shown_status(state),
                            'started_at': state['started_at'], 'admission_end_at': state['end_at'],
                            'duration_seconds': state['duration_seconds'], 'incomplete': state['incomplete'],
                            'updated_at': datetime.now(timezone.utc).isoformat(),
                            'events': state.get('_event_seq', 0), 'source_sha256': state['code_sha256'],
                            'public_requests': dict(outbound), 'paid_calls': 0, 'financial_actions': 0}
                        idle = state is not None and server.arena.owner is None
                    if summary:
                        marker = root / 'runtime/portable-observation.json'
                        pending = marker.with_suffix('.tmp')
                        pending.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
                        os.replace(pending, marker)
                        export_key = (summary['id'], summary['status'])
                        if idle and summary['status'] in {'DONE', 'PAUSED'} and export_key not in export_attempted:
                            export_attempted.add(export_key)
                            try:
                                from tools.export_paper_observation import export
                                if _reserve_export(root, summary):
                                    print(json.dumps(export(root), ensure_ascii=False), flush=True)
                                    print('Analysis ZIP saved in exports. Keep this window open to view results.', flush=True)
                            except (ValueError, OSError, KeyError, TypeError):
                                print('Automatic export failed. Keep runtime; run EXPORT_BATTLE.cmd after completion.', flush=True)
                    time.sleep(10)
            finally:
                server.shutdown()
    finally:
        urllib.request.OpenerDirector.open = original_open


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--duration', type=int, choices=[18000], default=18000)
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--view-only', action='store_true')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or args.port == 8765:
        parser.error('Use a separate local port, default 8766.')
    try:
        if args.check_only:
            verify_package(args.root)
            print('PORTABLE_PAPER_PACKAGE_OK; no worker, API request or trade started')
        else:
            run(args.root, duration=args.duration, port=args.port, view_only=args.view_only)
    except (ValueError, OSError, KeyError, TypeError):
        print('PAPER package could not open. Run CHECK_PACKAGE.cmd; keep all files and logs unchanged.')
        return 1
    except KeyboardInterrupt:
        print('Viewer stopped. If a round was active it is interrupted; it will not resume automatically.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
