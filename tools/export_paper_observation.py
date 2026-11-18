"""Export a stopped portable PAPER observation, offline; never resume execution."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def export(root, *, output=None):
    from trading_lab.arena import exclusive, read
    from trading_lab.match_journal import restore_events
    from trading_lab.battle_export import create_archive, verify_archive
    from trading_lab.battle_debrief import load_debrief, render_html
    root = Path(root).resolve()
    path = root / 'runtime/arena_v4'
    with exclusive(path / 'season.lock'):
        manifest = read(path / 'manifest.json')
        identity = manifest.get('active_id')
        if not isinstance(identity, str) or not re.fullmatch('[a-f0-9]{32}', identity):
            raise ValueError('no_observation_to_export')
        state = read(path / (identity + '.json'))
        if state['mode'] != 'PAPER' or state.get('execution_mode') != 'rules':
            raise ValueError('portable_rules_paper_only')
        if state['status'] not in {'DONE', 'PAUSED'}:
            # A killed process is evidence of interruption, never of flatness.
            state['status'] = 'PAUSED'
            state['incomplete'] = True
        restore_events(path / (identity + '.events.jsonl'), state, full=True)
        output = Path(output).resolve() if output else root / 'exports'
        output.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        base = f'Battle-{identity[:8]}-PAPER-{stamp}'
        destination = output / (base + '.zip')
        pending = output / (base + '.zip.pending')
        with create_archive(state, snapshots_path=path / (identity + '.snapshots.jsonl')) as archive:
            with pending.open('xb') as target:
                shutil.copyfileobj(archive, target, 1024**2)
        verification = verify_archive(pending)
        pending.rename(destination)
        with destination.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        receipt = {**verification, 'archive': destination.name,
                   'sha256': digest,
                   'bytes': destination.stat().st_size, 'html_created': False,
                   'paid_calls': 0, 'financial_actions': False}
        try:
            report = load_debrief(destination)
            (output / (base + '.html')).write_text(render_html(report), encoding='utf-8')
            receipt['html_created'] = True
        except (ValueError, OSError, KeyError, TypeError):
            # Complete ZIP remains available even if the bounded visualization is too large.
            receipt['html_error'] = 'debrief_unavailable_zip_retained'
        (output / (base + '.receipt.json')).write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        result = export(args.root)
    except (ValueError, OSError, KeyError, TypeError):
        print('Export failed. Keep the entire folder; do not delete runtime or logs. A running round must finish first.')
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print('Send the Battle-...-PAPER-....zip from exports to Codex. It contains no API keys.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
