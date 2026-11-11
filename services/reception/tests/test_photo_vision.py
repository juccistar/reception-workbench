"""Input staging and strict observation parsing; all Accio calls are mocked."""
import base64
import copy
import hashlib
from io import BytesIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from reception_service.errors import ServiceError
from reception_service.image_assets import decode_image
from reception_service.integrations import photo_vision as v
from reception_service.integrations.live import LiveIntegration
from reception_service.integrations.demo import natural_demo_analysis
from reception_service.synthetic_check import synthetic_context


class PhotoVisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        output = BytesIO()
        Image.new('RGB', (64, 64), 'red').save(output, 'PNG')
        self.asset = decode_image(output.getvalue(), 'image/png')
        self.message = dict(seller_id='1', buyer_id='2', conversation_id='1-2#c',
                            message_id='m1', role='buyer', own=False, text='[image]')
        self.native = SimpleNamespace(cli='synthetic-cli', data_dir=self.root / 'state')
        self.identity = {'agent_id': 'agent', 'conversation_name': 'own-synthetic-check'}
        self.binding = dict(rootPath=str(self.root), version=1, state='ready', source='default-generated', rootRevision=0)
        self.answer = json.dumps(dict(status='ok', description='红色方块。', visible_text=[], uncertainties=['合成图。']))

    def call(self, **changes):
        values = dict(native=self.native, agent_id='agent', workspace_identity=self.identity,
                      message=self.message, asset=self.asset, request_id='job_1', on_start=Mock())
        values.update(changes)
        return v.observe_photo(**values)

    def test_exact_bytes_callback_provenance_and_cleanup(self):
        events = []
        def run(cli, agent, image, digest, prompt, timeout):
            events.append('ai')
            self.assertEqual(events, ['start', 'ai'])
            self.assertTrue(image.is_relative_to(self.root / '.bgcheck_inputs'))
            self.assertEqual(image.read_bytes(), base64.b64decode(self.asset['original_b64']))
            self.assertEqual(hashlib.sha256(image.read_bytes()).hexdigest(), digest)
            self.assertIn(str(image), prompt)
            self.assertNotIn(self.message['conversation_id'], prompt)
            return dict(answer=self.answer, request_id='native-request', image_sha256=digest)
        with patch.object(v, 'request_workspace', return_value=self.binding) as workspace, patch.object(v, 'VisualQueryRunner') as runner:
            runner.return_value.run.side_effect = run
            result = self.call(on_start=lambda: events.append('start'))
        workspace.assert_called_once_with('synthetic-cli', 'agent', 'own-synthetic-check')
        self.assertEqual(result['request_id'], 'native-request')
        self.assertEqual(result['image_sha256'], self.asset['sha256'])
        self.assertEqual(list((self.root / '.bgcheck_inputs').iterdir()), [])

    def test_revoked_start_never_calls_runner_and_cleans_import(self):
        with patch.object(v, 'request_workspace', return_value=self.binding), patch.object(v, 'VisualQueryRunner') as runner:
            with self.assertRaises(ServiceError):
                self.call(on_start=Mock(side_effect=ServiceError('cancelled', 'paused')))
            runner.return_value.run.assert_not_called()
        self.assertEqual(list((self.root / '.bgcheck_inputs').iterdir()), [])

    def test_changed_image_or_missing_workspace_never_starts(self):
        for changes in ({'workspace_identity': None}, {'agent_id': 'other'},
                        {'asset': dict(self.asset, sha256='0' * 64)},
                        {'message': dict(self.message, own=True)},
                        {'asset': dict(self.asset, original_b64='not-base64')}):
            with self.subTest(changes=list(changes)), patch.object(v, 'request_workspace') as workspace:
                start = Mock()
                with self.assertRaises(ServiceError):
                    self.call(on_start=start, **changes)
                start.assert_not_called()
                workspace.assert_not_called()

    def test_strict_schema_failures_and_duplicate_keys(self):
        valid = json.loads(self.answer)
        bad = [dict(valid, status='unavailable'), dict(valid, command='exec'),
               dict(valid, description=''), dict(valid, description='a' * 2001),
               dict(valid, visible_text=['x'] * 21), dict(valid, uncertainties=['x' * 201]),
               dict(valid, visible_text='text'), dict(valid, description='x\x00')]
        for obj in bad:
            with self.subTest(obj=list(obj)), self.assertRaises(ServiceError):
                v.parse_observation(json.dumps(obj))
        with self.assertRaises(ServiceError):
            v.parse_observation(self.answer[:-1] + ',"status":"ok"}')
        self.assertEqual(v.parse_observation('```json\n' + self.answer + '\n```')['description'], '红色方块。')

    def test_failure_does_not_retry(self):
        start = Mock()
        with patch.object(v, 'request_workspace', return_value=self.binding), patch.object(v, 'VisualQueryRunner') as runner:
            runner.return_value.run.side_effect = ServiceError('failed', 'synthetic')
            with self.assertRaises(ServiceError):
                self.call(on_start=start)
            self.assertEqual(runner.return_value.run.call_count, 1)
        start.assert_called_once()
        self.assertEqual(list((self.root / '.bgcheck_inputs').iterdir()), [])

    def test_facade_remembers_only_successful_synthetic_identity(self):
        facade = object.__new__(LiveIntegration)
        facade.native = self.native
        facade.native.process = Mock()
        facade._agent_args = Mock(return_value=['--agent-id', 'agent'])
        facade.resolve_identity = Mock()
        facade._visual_workspace_identity = None
        context = synthetic_context('1')
        fake = Mock()
        fake.last_diagnostics = {}
        fake.last_request = self.identity
        fake.run.return_value = {'answer': natural_demo_analysis(context)}
        with patch('reception_service.integrations.live.DesktopQueryRunner', return_value=fake):
            facade.generate(context, 'check_job')
        self.assertEqual(facade._visual_workspace_identity, self.identity)
        facade._visual_workspace_identity = None
        customer = copy.deepcopy(context)
        customer['case']['case_id'] = 'ordinary-case'
        fake.run.return_value = {'answer': natural_demo_analysis(customer)}
        with patch('reception_service.integrations.live.DesktopQueryRunner', return_value=fake):
            facade.generate(customer, 'ordinary_job')
        self.assertIsNone(facade._visual_workspace_identity)


if __name__ == '__main__':
    unittest.main()
