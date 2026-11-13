"""Company-name reads use synthetic responses and never open a merchant workspace."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from reception_service.errors import ServiceError
from reception_service.integrations.company_profile import read_company_profile, fetch_company_document
from reception_service.service import ReceptionService


class CompanyProfileTests(unittest.TestCase):
    def native(self):
        return SimpleNamespace(_identity=Mock(return_value=('123', 'identity-hash')),
            _read=Mock(return_value={'businessSuccess': True,
                'data': {'companyId': 8, 'ossUrl': 'https://example.oss-cn-shanghai.aliyuncs.com/company.json'}}))

    def test_normal_mcp_request_and_exact_nested_company_name(self):
        native = self.native()
        document = {'companyBasicInfo': {'companyBasicInfo': {'companyName': 'Synthetic Trading Ltd.'}}}
        with patch('reception_service.integrations.company_profile.fetch_company_document', return_value=document):
            result = read_company_profile(native, '123')
        self.assertEqual(result, {'seller_id': '123', 'company_name': 'Synthetic Trading Ltd.'})
        native._read.assert_called_once_with('ai_minisite_get_company_info', {'dataType': 'all', 'language': 'zh_CN'})
        self.assertEqual(native._identity.call_count, 2)
        self.assertNotIn('ossUrl', result)

    def test_invalid_name_or_business_failure_is_not_a_company(self):
        for document in [{}, {'companyBasicInfo': {'companyName': 'Wrong layer'}},
                         {'companyBasicInfo': {'companyBasicInfo': {'companyName': 'secret\nname'}}}]:
            with self.subTest(document=document), patch('reception_service.integrations.company_profile.fetch_company_document', return_value=document):
                with self.assertRaises(ServiceError):
                    read_company_profile(self.native(), '123')
        native = self.native()
        native._read.return_value['businessSuccess'] = False
        with self.assertRaises(ServiceError):
            read_company_profile(native, '123')

    def test_document_download_rejects_non_platform_hosts_and_private_addresses(self):
        for url in ['http://example.alicdn.com/company.json', 'https://127.0.0.1/company.json',
                    'https://example.alicdn.com.evil.test/a', 'https://user:password@example.alicdn.com/a']:
            with self.subTest(url=url), self.assertRaises(ServiceError):
                fetch_company_document(url)
        with patch('reception_service.integrations.company_profile._resolve', return_value=['127.0.0.1']):
            with self.assertRaises(ServiceError):
                fetch_company_document('https://example.alicdn.com/company.json')

    def test_nested_failure_is_checked_even_with_outer_url(self):
        for nested in ({'success': False}, {'businessSuccess': False}):
            native = self.native()
            native._read.return_value = {'ossUrl': 'https://skill.accio.com/a', 'data': nested}
            with patch('reception_service.integrations.company_profile.fetch_company_document') as fetch:
                with self.assertRaises(ServiceError) as caught:
                    read_company_profile(native, '123')
                self.assertEqual((caught.exception.stage, caught.exception.category), ('native', 'business_failure'))
                fetch.assert_not_called()

    def test_company_redirects_validate_every_host_and_resolve_again(self):
        def response(status, body=b'', location=None):
            item = Mock(status=status)
            item.getheader.side_effect = lambda key, default=None: location if key == 'Location' else default
            item.read.side_effect = [body, b'']
            return item
        first = Mock(sock=None)
        first.getresponse.return_value = response(302, location='https://skill.accio.com/company')
        second = Mock(sock=None)
        second.getresponse.return_value = response(200, b'{"ok":true}')
        with patch('reception_service.integrations.company_profile._resolve', return_value=['8.8.8.8']) as resolve, \
             patch('reception_service.integrations.company_profile._PinnedHTTPS', side_effect=[first, second]):
            self.assertEqual(fetch_company_document('https://example.alicdn.com/a'), {'ok': True})
            self.assertEqual([call.args[0] for call in resolve.call_args_list], ['example.alicdn.com', 'skill.accio.com'])
        for url in ['https://x.skill.accio.com/a', 'https://skill.accio.com.evil.test/a',
                    'https://accio.com/a', 'https://skill.accio.com:444/a']:
            with self.subTest(url=url), self.assertRaises(ServiceError) as caught:
                fetch_company_document(url)
            self.assertEqual((caught.exception.stage, caught.exception.category), ('source', 'invalid_url'))
        first.getresponse.return_value = response(302, location='https://evil.test/?secret=token')
        with patch('reception_service.integrations.company_profile._resolve', return_value=['8.8.8.8']), \
             patch('reception_service.integrations.company_profile._PinnedHTTPS', return_value=first):
            with self.assertRaises(ServiceError) as caught:
                fetch_company_document('https://skill.accio.com/a')
            self.assertNotIn('secret', str(caught.exception.to_dict()))

    def test_document_limits_and_json_failures_have_fixed_categories(self):
        for body, length, expected in [(b'not json', None, ('parse', 'invalid_json')),
                                       (b'[]', None, ('parse', 'invalid_document')),
                                       (b'', '524289', ('http', 'size_limit'))]:
            response = Mock(status=200)
            response.getheader.side_effect = lambda key, default=None: length if key == 'Content-Length' else default
            response.read.side_effect = [body, b'']
            connection = Mock(sock=None)
            connection.getresponse.return_value = response
            with self.subTest(expected=expected), \
                 patch('reception_service.integrations.company_profile._resolve', return_value=['8.8.8.8']), \
                 patch('reception_service.integrations.company_profile._PinnedHTTPS', return_value=connection), \
                 self.assertRaises(ServiceError) as caught:
                fetch_company_document('https://skill.accio.com/document')
            self.assertEqual((caught.exception.stage, caught.exception.category), expected)

    def test_failures_expose_only_fixed_diagnostics(self):
        native = self.native()
        native._read.side_effect = RuntimeError('https://secret.example/?token=credential')
        with self.assertRaises(ServiceError) as caught:
            read_company_profile(native, '123')
        error = caught.exception
        self.assertEqual((error.stage, error.category), ('native', 'read_failed'))
        self.assertEqual(error.diagnostics['native_error_category'], 'read_failed')
        self.assertNotIn('credential', str(error.to_dict()))


class CompanyServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = ReceptionService(Path(self.temp.name))
        self.addCleanup(self.service.close)

    def wait(self, job_id):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            job = self.service.repo.get('job', job_id)
            if job['status'] not in ('queued', 'running'):
                return job
            time.sleep(.01)
        self.fail('Company profile task timed out')

    def test_local_snapshots_never_fetch_company_information(self):
        with patch.object(self.service.integrations, 'company_profile') as read:
            for _ in range(5):
                self.assertEqual(self.service.get_snapshot()['app']['company_name'], '演示商家公司')
            read.assert_not_called()

    def test_explicit_read_only_task_updates_current_merchant_without_reception_changes(self):
        before = self.service.get_snapshot()['reception']
        task = self.service.call('refresh_company_profile', {})
        self.assertEqual(self.wait(task['job_id'])['status'], 'succeeded')
        stored = self.service.repo.get('meta', 'company_profile')
        self.assertEqual(stored['seller_id'], 'demo-seller')
        self.assertEqual(self.service.get_snapshot()['reception'], before)

    def test_foreign_company_name_never_enters_the_merchant_cache(self):
        with patch.object(self.service.integrations, 'company_profile', return_value={
                'seller_id': 'foreign', 'company_name': 'Other seller'}):
            task = self.service.call('refresh_company_profile', {})
            self.assertEqual(self.wait(task['job_id'])['status'], 'failed')
        self.assertNotIn('Other seller', str(self.service.repo.get('meta', 'company_profile')))

    def test_unknown_parameters_are_rejected(self):
        with self.assertRaises(ServiceError):
            self.service.call('refresh_company_profile', {'seller_id': 'foreign'})


if __name__ == '__main__':
    unittest.main()
