"""Build a credential-free, pinned Windows PAPER package from an explicit roster."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trading_lab.arena_strategies import fingerprint, validate_strategy
from trading_lab.strategy_contract import payload_of
from tools.build_server_bundle import WEB_SUFFIXES

EXTRA = ('tools/run_paper_package.py', 'tools/run_isolated_paper.py',
         'tools/export_paper_observation.py', 'tools/analyze_battle_file.py', 'tools/battle_debrief.py')


def command(script, args=''):
    return ('@echo off\r\nsetlocal\r\ncd /d "%~dp0"\r\n'
            'py -3.12 --version >nul 2>&1\r\nif errorlevel 1 (\r\n'
            '  echo Python 3.12 with Python Launcher is required. Read START_HERE_UA.md.\r\n'
            '  pause\r\n  exit /b 1\r\n)\r\n'
            f'py -3.12 -B -X utf8 "%~dp0tools\\{script}" --root "%~dp0." {args}\r\n'
            'if errorlevel 1 echo Keep this window and all files if an error occurred.\r\npause\r\n').encode('ascii')


def build(output, roster, *, root=ROOT):
    root, output = Path(root).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError('destination_exists')
    if not isinstance(roster, list) or not 1 <= len(roster) <= 6:
        raise ValueError('invalid_roster')
    if len({s['id'] for s in roster}) != len(roster):
        raise ValueError('duplicate_participants')
    for strategy in roster:
        payload = payload_of(strategy)
        validate_strategy(payload, strategy['version'])
        if strategy.get('status') != 'ready' or fingerprint(payload) != strategy.get('recipe_sha256'):
            raise ValueError('unvalidated_or_unpinned_strategy')
    # Public recipe fields only; never copy runtime accounts/checks or credentials.
    roster = [{**payload_of(s), **{k: s[k] for k in ('id', 'version', 'recipe_sha256', 'status')}} for s in roster]
    files = {p.relative_to(root).as_posix(): p.read_bytes() for p in (root / 'trading_lab').glob('*.py')}
    for path in (root / 'web').rglob('*'):
        if path.is_file() and (path.suffix.lower() in WEB_SUFFIXES or path.name.upper().startswith('LICENSE')):
            if path.is_symlink():
                raise ValueError('symlink_source')
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    for name in EXTRA:
        files[name] = (root / name).read_bytes()
    files['START_HERE_UA.md'] = (root / '03_For_Client_Deliverables/NEXT_PAPER_WINDOWS_UA.md').read_bytes()
    files['START_PAPER_5H.cmd'] = command('run_paper_package.py', '--duration 18000')
    files['OPEN_RESULTS.cmd'] = command('run_paper_package.py', '--view-only')
    files['CHECK_PACKAGE.cmd'] = command('run_paper_package.py', '--check-only')
    files['EXPORT_BATTLE.cmd'] = command('export_paper_observation.py')
    plan = {'schema': 'forward-paper-pilot-plan/v1', 'mode': 'PAPER', 'execution_mode': 'rules',
            'purpose': 'Forward engineering observation; four unchanged V1 controls and first human G1 pilot, not evidence of profitability.',
            'admission_options_seconds': [18000], 'selection': 'Owner chooses duration before first market observation.',
            'budget_usdt_per_bot': 100, 'time_policy': 'admission_then_drain',
            'cost_model': 'Recorded bid/ask + 2bps adverse slippage and 5bps fee per fill; modeled funding.',
            'analysis': ['All entrants/non-entrants and all cycles retained',
                         'Net fees funding, phase failures and data gaps; no hindsight parameter tuning',
                         'Compare V1 controls with prior observation descriptively; different days are not a causal treatment comparison',
                         'Any revised strategy requires a separately pinned future paired window'],
            'human_strategy': 'Geometric IKIGAI adaptation by AI for Yurii; initial history warmup does not generate historical entries.',
            'participants': [{'id': s['id'], 'name': s['name'], 'version': s['version'],
                              'recipe_sha256': s['recipe_sha256']} for s in roster],
            'paid_calls': False, 'financial_actions': False, 'automatic_repeat': False}
    files['NEXT_RUN_PLAN.json'] = (json.dumps(plan, ensure_ascii=False, indent=2) + '\n').encode()
    pins = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}
    try:
        commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = None
    package = {'schema': 'jev-portable-paper/v2', 'created_at': datetime.now(timezone.utc).isoformat(),
               'source_commit': commit, 'mode': 'PAPER', 'execution_mode': 'rules',
               'admission_options_seconds': [18000], 'budget_usdt_per_bot': 100,
               'recipes': {s['id']: s['recipe_sha256'] for s in roster},
               'catalogue_sha256': fingerprint(roster), 'files': pins,
               'contains_credentials': False, 'contains_prior_runtime': False,
               'source_content_sha256': fingerprint(pins), 'python': '3.12'}
    files['paper-package.json'] = (json.dumps(package, ensure_ascii=False, indent=2) + '\n').encode()
    files['runtime/arena_v4/manifest.json'] = json.dumps(
        {'generation': 0, 'strategies': roster, 'seasons': [], 'active_id': None}, ensure_ascii=False).encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, data in sorted(files.items()):
            archive.writestr('JevArena-PAPER/' + name, data)
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise ValueError('zip_crc_failed')
    with output.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'archive': str(output), 'bytes': output.stat().st_size, 'sha256': digest,
            'files': len(files), 'participants': len(roster), 'source_sha256': package['source_content_sha256'],
            'contains_credentials': False, 'process_started': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--roster', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.output, json.loads(args.roster.read_text(encoding='utf-8'))), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
