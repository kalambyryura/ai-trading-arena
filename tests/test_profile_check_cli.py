import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).parent))
from test_exchange_lab import FakeExchange
from tools.check_exchange_profile import check
from trading_lab.exchange_accounts import account_tag


class ReadOnlyProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'LocalSettings').mkdir()
        (self.root / 'LocalSettings/exchange_accounts.json').write_text(json.dumps({'profiles': [{
            'id': 'arena-yurii', 'label': 'Fictional human', 'environment': 'live',
            'settings_file': '.env.okx.arena-yurii.local'}]}), encoding='utf-8')
        (self.root / '.env.okx.arena-yurii.local').write_text(
            'OKX_SITE=global\nOKX_ENV=live\nOKX_API_KEY=fictional-api-key\n'
            'OKX_API_SECRET=fictional-secret\nOKX_API_PASSPHRASE=fictional-passphrase\n', encoding='utf-8')
        self.fake = FakeExchange(time.time)

    def test_get_only_check_and_sanitized_report(self):
        result = check(self.root, 'arena-yurii', transport=self.fake.transport)
        self.assertTrue(result['ready'], result)
        self.assertTrue(result['credentials_unchanged'])
        self.assertFalse(result['writes_armed'])
        self.assertEqual(result['request_counts']['writes'], 0)
        self.assertTrue(all(r.get_method() == 'GET' for r in self.fake.requests))
        text = json.dumps(result)
        for secret in ('fictional-api-key', 'fictional-secret', 'fictional-passphrase', 'mainUid', 'account_tag'):
            self.assertNotIn(secret, text)
        self.assertEqual(result['unused_permissions'], ['trade'])

    def test_profile_tool_a_market_failure_never_hides_the_withdraw_refusal(self):  # R2.1.11 (R2110-PROFILE-TOOL)
        self.fake.perm = 'read_only,trade,withdraw'
        def transport(request):
            if urlsplit(request.full_url).path == '/api/v5/market/ticker':
                return {'code': '50001', 'msg': 'x', 'data': []}
            return self.fake.transport(request)
        result = check(self.root, 'arena-yurii', transport=transport)
        self.assertFalse(result['ready'])
        self.assertTrue(result['live_key_refused'])
        self.assertIn('withdraw', result['unused_permissions'])

    def test_profile_tool_a_demo_key_with_withdraw_gets_the_panels_warning(self):
        from unittest import mock
        import tools.check_exchange_profile as tool
        from trading_lab.okx_readonly import Credentials
        live = tool.profiles(self.root)[0]  # the fictional settings file of setUp, read only for its hash
        demo = {**live, 'id': 'demo', 'environment': 'demo'}
        self.fake.perm = 'read_only,trade,withdraw'
        with mock.patch.object(tool, 'profiles', return_value=[demo]), \
                mock.patch.object(tool, 'load_profile', return_value=('global', 'demo', Credentials('k', 's', 'p'))):
            result = check(self.root, 'demo', transport=self.fake.transport)
        self.assertEqual(result.get('permission_warning'), 'demo_key_has_withdraw')
        self.assertFalse(result['live_key_refused'])

    def test_d1_a_live_key_with_withdraw_is_not_ready_as_on_the_panel(self):
        self.fake.perm = 'read_only,trade,withdraw'
        result = check(self.root, 'arena-yurii', transport=self.fake.transport)
        self.assertFalse(result['ready'])
        self.assertEqual(result['error_code'], 'live_key_has_unused_permissions')
        self.assertIn('withdraw', result['unused_permissions'])
        self.assertEqual(result['request_counts']['writes'], 0)

    def test_empty_trading_balance_is_an_explicit_funding_gap(self):
        def transport(request):
            if urlsplit(request.full_url).path == '/api/v5/account/balance':
                return {'code': '0', 'data': [{'totalEq': '0', 'details': []}]}
            return self.fake.transport(request)
        result = check(self.root, 'arena-yurii', transport=transport)
        self.assertFalse(result['ready'])
        self.assertEqual(result['error_code'], 'trading_usdt_balance_missing')
        self.assertFalse(result['diagnostic']['trading_usdt_record_present'])

    def test_duplicate_existing_account_is_not_ready(self):
        folder = self.root / 'runtime/exchange_v1'
        folder.mkdir(parents=True)
        (folder / 'manifest.json').write_text(json.dumps({'checks': {
            'arena-a': {'_account_tag': account_tag('global', 'live', self.fake.uid)}}}), encoding='utf-8')
        result = check(self.root, 'arena-yurii', transport=self.fake.transport)
        self.assertFalse(result['ready'])
        self.assertEqual(result['error_code'], 'duplicate_subaccount_identity')

    def test_missing_read_scope_stops_at_configuration(self):
        def transport(request):
            result = self.fake.transport(request)
            if urlsplit(request.full_url).path == '/api/v5/account/config':
                result['data'][0]['perm'] = 'trade'
            return result
        result = check(self.root, 'arena-yurii', transport=transport)
        self.assertFalse(result['ready'])
        self.assertEqual(result['error_code'], 'read_permission_missing_or_unknown_scopes')
        self.assertEqual(len(self.fake.requests), 2)


if __name__ == '__main__':
    unittest.main()
