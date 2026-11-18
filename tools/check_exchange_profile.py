"""Bounded, GET-only account check. No orders, settings, Jev or manifest writes."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trading_lab.exchange_accounts import load_profile, profiles
from trading_lab.exchange_api import ExchangeAPI, ExchangeFeed, GET_PATHS, send
from trading_lab.okx_readonly import PreflightError, config_summary


def check(root, profile_id, budget=100, *, transport=send):
    root = Path(root)
    profile = next(p for p in profiles(root) if p['id'] == profile_id)
    credential_path = root / profile['settings_file']
    original = hashlib.sha256(credential_path.read_bytes()).hexdigest()
    counts = {'get': 0, 'writes': 0, 'blocked': 0}
    configuration = {}
    details = {}

    def get_only(request):
        path = urlsplit(request.full_url).path
        if request.get_method() != 'GET' or request.data is not None or path not in GET_PATHS:
            counts['blocked'] += 1
            raise PreflightError('read_only_transport_rejected')
        counts['get'] += 1
        if counts['get'] > 24:
            raise PreflightError('read_only_request_cap')
        result = transport(request)
        details['last_endpoint'] = path
        if path == '/api/v5/account/config':
            configuration.update(config_summary(result.get('data', [])))
        if path == '/api/v5/account/balance':
            rows = result.get('data', [])
            details['trading_usdt_record_present'] = any(
                d.get('ccy') == 'USDT' for row in rows if isinstance(row, dict)
                for d in row.get('details', []) if isinstance(d, dict))
        return result

    api = ExchangeAPI(*load_profile(root, profile), transport=get_only)
    api.request_limit = 24
    report = {'schema': 'exchange-profile-read-only/v1', 'profile': profile_id,
              'checked_at_utc': datetime.now(timezone.utc).isoformat(),
              'budget_check_usdt': budget, 'ready': False, 'paid_calls': 0}
    try:
        result = api.preflight(budget, require_subaccount=profile['environment'] == 'live')
        # R2.1.11 (R2110-PROFILE-TOOL): the key's Withdraw is reported before the market check, so a market failure
        # never hides it; Demo gets the panel's warning
        withdraw = 'withdraw' in result['unused_permissions']
        refused = profile['environment'] == 'live' and withdraw  # the panel refuses it (D-1)
        report.update(live_key_refused=refused, unused_permissions=sorted(result['unused_permissions']))
        if withdraw and profile['environment'] == 'demo':
            report['permission_warning'] = 'demo_key_has_withdraw'
        snapshot = ExchangeFeed(api).snapshot()
        manifest_path = root / 'runtime/exchange_v1/manifest.json'
        checks = json.loads(manifest_path.read_text(encoding='utf-8')).get('checks', {}) if manifest_path.exists() else {}
        other_tags = {c.get('_account_tag') for name, c in checks.items()
                      if name != profile_id and name.startswith('arena-') and c.get('_account_tag')}
        distinct = result['account_tag'] not in other_tags
        report.update(ready=distinct and not refused, account_preflight=True, market_preflight=True,
                      is_subaccount=result['is_subaccount'], flat=True, no_pending_orders=True,
                      enough_available_for_budget=True, distinct_from_previously_checked_profiles=distinct,
                      other_profile_tags_compared=len(other_tags), configuration=configuration,
                      unused_permissions=configuration.get('unused_permissions', []),
                      funding_status=snapshot.get('funding_status', 'available'),
                      market_instrument=snapshot.get('instrument', 'BTC-USDT-SWAP'))
        if not distinct:
            report['error_code'] = 'duplicate_subaccount_identity'
        if refused:
            report['error_code'] = 'live_key_has_unused_permissions'
    except (PreflightError, ValueError, KeyError, TypeError, OSError) as exc:
        code = str(exc)
        report['error_code'] = code if re.fullmatch(r'[a-z][a-z0-9_]{0,100}', code) else 'account_or_market_check_failed'
        if details.get('trading_usdt_record_present') is False and code == 'unexpected_response_shape':
            report['error_code'] = 'trading_usdt_balance_missing'
        report['configuration'] = configuration
    report.update(request_counts=counts, writes_armed=api.writes, diagnostic=details,
                  credentials_unchanged=original == hashlib.sha256(credential_path.read_bytes()).hexdigest())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--budget', type=int, default=100, choices=range(25, 501))
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        report = check(args.root, args.profile, args.budget)
    except (ValueError, OSError, KeyError, StopIteration):
        report = {'ready': False, 'error_code': 'profile_missing_or_invalid', 'writes': 0, 'paid_calls': 0}
    text = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as out:
            out.write(text)
    print(text)
    return 0 if report['ready'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
