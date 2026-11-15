import asyncio
import io
import json
import threading
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from reception_service.model_providers.checker import _image, run_check, run_check_from_stdin


def packet(purpose='reception'):
    return {'format': 'model-check-v1', 'purpose': purpose, 'api_key': 'secret-value', 'profile': {
        'profile_id': 'profile1', 'protocol': 'chat-completions', 'base_url': 'https://example.invalid/v1',
        'model': 'test', 'config_revision': 3, 'credential_revision': 4}}


class FakeTransport:
    def __init__(self, fail_at=None, cancel_at=None, event=None):
        self.calls = []
        self.closed = False
        self.fail_at, self.cancel_at, self.event = fail_at, cancel_at, event

    async def generate(self, **kw):
        self.calls.append(kw)
        n = len(self.calls)
        if n == self.cancel_at:
            self.event.set()
            await asyncio.sleep(1)
        if n == self.fail_at:
            return {'unexpected': 'secret-value'}
        images = kw['images']
        if images:
            assert len(kw['encoded_images']) == len(images)
            red = _image('p1', (220, 20, 30))['sha256']
            obs = [{'alias': image['alias'], 'color': 'red' if image['sha256'] == red else 'blue'} for image in images]
            if 'reception JSON schema' in kw['user_payload']:
                return {'action': 'ask', 'reply_messages': [{'body': 'Could you share more details?', 'reply_sources': ['m1']}],
                        'requirement_changes': [], 'conversation_summary': '客户询问图片。', 'observations': obs}
            return {'observations': obs}
        if '2 + 3' in kw['user_payload']:
            return {'answer': 5}
        if 'Translate' in kw['user_payload']:
            return {'items': [{'id': 't1', 'text': '你好'}]}
        return {'items': [{'id': 'x', 'count': 2}]}

    async def aclose(self):
        self.closed = True


class CheckerTests(unittest.TestCase):
    def test_reception_full_probes_and_swapped_images(self):
        fake = FakeTransport()
        result = run_check(packet(), fake)
        self.assertEqual(result['status'], 'passed', result)
        self.assertEqual(len(fake.calls), 7)
        self.assertTrue(fake.closed)
        self.assertEqual([result['capabilities'][k]['status'] for k in ('text', 'structure', 'single_image', 'multi_image', 'combined', 'translation')], ['passed'] * 5 + ['not_checked'])
        self.assertEqual(fake.calls[4]['images'][0]['sha256'], fake.calls[5]['images'][1]['sha256'])
        self.assertEqual(fake.calls[4]['images'][1]['sha256'], fake.calls[5]['images'][0]['sha256'])
        self.assertNotIn('secret-value', json.dumps(result))
        self.assertEqual((result['config_revision'], result['credential_revision']), (3, 4))

    def test_cleanup_finishes_after_checker_timeout(self):
        from reception_service.model_providers.runtime import ModelRuntime
        released, cleaned = threading.Event(), threading.Event()
        runtimes = []
        class SlowCleanup(FakeTransport):
            async def generate(self, **kw):
                try:
                    await asyncio.Event().wait()
                finally:
                    while not released.is_set():
                        await asyncio.sleep(.01)
            async def aclose(self):
                cleaned.set()
        def make_runtime():
            runtime = ModelRuntime(cleanup_timeout=.05)
            runtimes.append(runtime)
            return runtime
        with patch('reception_service.model_providers.checker.ModelRuntime', side_effect=make_runtime):
            result = run_check(packet('translation'), SlowCleanup(), deadline=.05)
        self.assertEqual(result['code'], 'model_timeout')
        self.assertFalse(cleaned.is_set())
        released.set()
        self.assertTrue(cleaned.wait(2))
        runtimes[0]._thread.join(2)
        self.assertFalse(runtimes[0]._thread.is_alive())

    def test_default_transport_http_boundary(self):
        import httpx
        from reception_service.model_providers.transport import ProviderTransport
        calls = []
        def respond(request):
            body = json.loads(request.content)
            calls.append(body)
            prompt = body['messages'][1]['content'][0]['text']
            answer = {'answer': 5} if '2 + 3' in prompt else {'items': [{'id': 't1', 'text': '你好'}]}
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(answer)}}]})
        with patch('reception_service.model_providers.transport.ProviderTransport',
                   side_effect=lambda: ProviderTransport(_transport_factory=lambda: httpx.MockTransport(respond))):
            result = run_check(packet('translation'))
        self.assertEqual(result['status'], 'passed', result)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result['capabilities']['translation']['status'], 'passed')

    def test_invalid_business_result_is_rejected_safely(self):
        from reception_service.model_providers.checker import _business
        self.assertFalse(_business({'action': 'bad', 'conversation_summary': '', 'requirement_changes': []}))

    def test_translation_never_sends_images(self):
        fake = FakeTransport()
        result = run_check(packet('translation'), fake)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(len(fake.calls), 2)
        self.assertTrue(all(not call['images'] and not call['encoded_images'] for call in fake.calls))
        self.assertEqual(result['capabilities']['single_image']['status'], 'not_checked')

    def test_failure_does_not_erase_other_capabilities(self):
        fake = FakeTransport(fail_at=3)
        result = run_check(packet(), fake)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['capabilities']['single_image']['status'], 'failed')
        self.assertEqual(result['capabilities']['multi_image']['status'], 'passed')
        self.assertTrue(fake.closed)

    def test_cancel_mid_pair_does_not_report_pass(self):
        event = threading.Event()
        fake = FakeTransport(cancel_at=4, event=event)
        result = run_check(packet(), fake, cancel=event)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['capabilities']['single_image']['status'], 'not_checked')
        self.assertEqual(result['capabilities']['multi_image']['status'], 'not_checked')
        self.assertTrue(fake.closed)

    def test_timeout_and_invalid_json_are_not_passed(self):
        class Broken(FakeTransport):
            async def generate(self, **kw):
                return '{"answer":5'
        broken = Broken()
        result = run_check(packet('translation'), broken)
        self.assertEqual(result['capabilities']['text']['status'], 'failed')
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(broken.closed)
        fake = FakeTransport()
        result = run_check(packet(), fake, deadline=0)
        self.assertEqual(result['code'], 'model_timeout')
        self.assertTrue(all(row['status'] == 'not_checked' for row in result['capabilities'].values()))
        self.assertTrue(fake.closed)

    def test_invalid_input_and_default_transport(self):
        invalid = packet()
        invalid['profile']['credential_revision'] = True
        self.assertEqual(run_check(invalid)['code'], 'model_invalid_input')
        fake = FakeTransport()
        with patch('reception_service.model_providers.transport.ProviderTransport', return_value=fake):
            self.assertEqual(run_check(packet())['status'], 'passed')
        self.assertTrue(fake.closed)

    def test_cli_one_json_and_bounded_input(self):
        for raw in (json.dumps(packet()).encode() + b'\n', b'x' * (1024 * 1024 + 1)):
            output = io.StringIO()
            with patch('sys.stdin', io.TextIOWrapper(io.BytesIO(raw), encoding='utf-8')), redirect_stdout(output), \
                 patch('reception_service.model_providers.transport.ProviderTransport', side_effect=FakeTransport):
                run_check_from_stdin()
            value = json.loads(output.getvalue())
            self.assertEqual(value['status'], 'failed' if len(raw) > 1024 * 1024 else 'passed')
            self.assertNotIn('secret-value', output.getvalue())


if __name__ == '__main__':
    unittest.main()
