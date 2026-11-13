"""One business query with original images; all files and traces are synthetic."""
import base64
import copy
import hashlib
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from reception_service.errors import ServiceError
from reception_service.image_assets import decode_image
from reception_service.photos import (direct_photo_context, with_direct_photo_reads,
                                      photo_version, photo_fingerprint)
from reception_service.repository import Repository, encoded
from reception_service.integrations.business_images import query_business_images
from reception_service.integrations.accio_native.business_image_trace import validate_business_trace


class BusinessImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = Repository(None, '123')
        self.addCleanup(self.repo.close)
        self.messages, self.inputs = [], []
        for number, color in enumerate(['blue', 'green']):
            output = BytesIO()
            Image.new('RGB', (8, 8), color).save(output, 'PNG')
            asset = decode_image(output.getvalue(), 'image/png')
            message = {'seller_id': '123', 'buyer_id': 'buyer', 'conversation_id': 'cid',
                       'message_id': 'image-' + str(number), 'role': 'buyer' if number == 0 else 'seller', 'own': number == 1,
                       'text': '[image]', 'attachments': [], 'timestamp_ms': number + 1}
            self.repo.put('image_asset', asset['sha256'], asset)
            self.repo.put('message_image', encoded(['cid', message['message_id']]), {
                **{k: message[k] for k in ('seller_id', 'buyer_id', 'conversation_id', 'message_id')},
                'source_version': photo_version(message), 'sha256': asset['sha256'], 'status': 'ready'})
            self.messages.append(message)
            self.inputs.append({'message': message, 'asset': asset})
        self.photo = direct_photo_context(self.repo, self.messages)
        self.context = {'case': {'seller_id': '123', 'buyer_id': 'buyer', 'conversation_id': 'cid'},
                        'recent_messages': self.messages, 'photo_context': self.photo,
                        'supplement': {'body': '14.5 per piece; shipping not included'}}
        self.native = SimpleNamespace(cli=self.root / 'workctl.exe', data_dir=self.root)
        self.identity = {'agent_id': 'agent', 'conversation_name': 'own-synthetic-check'}
        self.paths = [self.root / ('image-' + str(i) + '.png') for i in range(2)]
        self.prompt = 'Read the exact original images'

    def trace(self, sequential=False):
        common = {'conversationId': 'cid', 'agentId': 'agent', 'status': 'finished'}
        rows = [dict(common, role='req', content=self.prompt, requestId='request', messageId='A')]
        parent = 'A'
        groups = [[0], [1]] if sequential else [[0, 1]]
        for group_number, group in enumerate(groups):
            mid = 'M' + str(group_number)
            calls = [{'id': 'T' + str(i), 'name': 'read', 'arguments': {
                'file_path': str(self.paths[i]), 'image_prompt': 'Read the customer image'}} for i in group]
            progress = {entry['id']: [{'toolCallId': entry['id'], 'toolName': 'read', 'status': 'completed'}] for entry in calls}
            rows.append(dict(common, role='res', messageId=mid, branchParentId=parent,
                             clientRequestId='request', tool_calls=calls, tool_progress=progress))
            for i in group:
                rows.append(dict(common, role='tool', messageId=mid, parentMessageId=mid, branchParentId=mid,
                    tool_call_id='T' + str(i), name='read', content='Loaded 1 image:\n  ' + str(self.paths[i])
                    + ' → https://sc02.alicdn.com/kf/synthetic.png'))
            parent = mid
        rows.append(dict(common, role='res', messageId=parent, branchParentId=parent,
                         clientRequestId='request', content='{"reply":"Synthetic answer"}'))
        for sequence, row in enumerate(rows):
            row['seq'] = sequence + 1
        return rows

    def test_parallel_and_sequential_reads_belong_to_one_exact_request(self):
        for sequential in (False, True):
            answer, request = validate_business_trace(self.trace(sequential), 'cid', 'agent', self.prompt, self.paths)
            self.assertEqual(request, 'request')
            self.assertEqual(json.loads(answer)['reply'], 'Synthetic answer')

    def test_five_eight_twenty_images_with_grouped_and_sequential_reads(self):
        for count in (5, 8, 20):
            self.paths = [self.root / ('image-' + str(i) + '.png') for i in range(count)]
            for sequential in (False, True):
                common = {'conversationId': 'cid', 'agentId': 'agent', 'status': 'finished'}
                rows = [dict(common, role='req', content=self.prompt, requestId='request', messageId='A')]
                parent = 'A'
                groups = [[i] for i in range(count)] if sequential else [list(range(count))]
                for group_number, group in enumerate(groups):
                    mid = 'M' + str(group_number)
                    calls = [{'id': 'T' + str(i), 'name': 'read', 'arguments': {
                        'file_path': str(self.paths[i]), 'image_prompt': 'Read'}} for i in group]
                    progress = {c['id']: [{'toolCallId': c['id'], 'toolName': 'read', 'status': 'completed'}] for c in calls}
                    rows.append(dict(common, role='res', messageId=mid, branchParentId=parent,
                                     clientRequestId='request', tool_calls=calls, tool_progress=progress))
                    for i in group:
                        rows.append(dict(common, role='tool', messageId=mid, parentMessageId=mid,
                            branchParentId=mid, tool_call_id='T' + str(i), name='read',
                            content='Loaded 1 image:\n  ' + str(self.paths[i]) + ' → https://sc02.alicdn.com/synthetic'))
                    parent = mid
                rows.append(dict(common, role='res', messageId=parent, branchParentId=parent,
                                 clientRequestId='request', content='answer'))
                with self.subTest(count=count, sequential=sequential):
                    self.assertEqual(validate_business_trace(rows, 'cid', 'agent', self.prompt, self.paths), ('answer', 'request'))
                    altered = copy.deepcopy(rows)
                    altered[1]['tool_calls'][0]['arguments']['file_path'] = 'foreign'
                    with self.assertRaises(ValueError):
                        validate_business_trace(altered, 'cid', 'agent', self.prompt, self.paths)

    def test_extra_tools_foreign_paths_progress_and_incomplete_reads_are_rejected(self):
        changes = [lambda rows: rows[1]['tool_calls'][0].update(name='exec'),
                   lambda rows: rows[1]['tool_calls'][0]['arguments'].update(file_path='foreign'),
                   lambda rows: rows[1]['tool_progress']['T0'][0].update(status='error'),
                   lambda rows: rows[-1].update(clientRequestId='foreign'),
                   lambda rows: rows[2].update(agentId='other'),
                   lambda rows: rows[1]['tool_calls'][1].update(id='T0'),
                   lambda rows: rows.append(copy.deepcopy(rows[-1]))]
        for change in changes:
            with self.subTest(change=change):
                rows = self.trace()
                change(rows)
                with self.assertRaises(ValueError):
                    validate_business_trace(rows, 'cid', 'agent', self.prompt, self.paths)
        with self.assertRaises(ValueError):
            validate_business_trace(self.trace(), 'cid', 'agent', self.prompt, self.paths[:1])

    def test_context_contains_references_without_claiming_new_request_has_read_pixels(self):
        self.assertFalse(self.photo['pixel_access'])
        self.assertEqual(len(self.photo['direct_images']), 2)
        self.assertEqual([row['role'] for row in self.photo['direct_images']], ['buyer', 'seller'])
        self.assertNotIn('original_b64', json.dumps(self.context))
        seen = with_direct_photo_reads(self.context, self.photo['direct_images'])
        self.assertTrue(seen['photo_context']['pixel_access'])
        self.assertEqual(photo_fingerprint(seen), photo_fingerprint(self.context))
        foreign = copy.deepcopy(self.photo['direct_images'])
        foreign[0]['image_sha256'] = '0' * 64
        self.assertEqual(len(with_direct_photo_reads(self.context, foreign)['photo_context']['read_images']), 1)

    def test_one_business_call_reads_originals_and_removes_only_owned_temporary_inputs(self):
        started, calls = [], []
        def run(cli, agent, images, prompt, **kwargs):
            calls.append(prompt)
            self.assertEqual(len(images), 2)
            self.assertIn('14.5 per piece', prompt)
            self.assertIn('image_inputs', prompt)
            manifest = json.loads(prompt.split('image_inputs = ', 1)[1])
            self.assertEqual([row['role'] for row in manifest], ['buyer', 'seller'])
            self.assertEqual([row['sender_id'] for row in manifest], ['buyer', '123'])
            for (path, sha), item in zip(images, self.inputs):
                self.assertEqual(path.read_bytes(), base64.b64decode(item['asset']['original_b64']))
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), sha)
                self.assertNotIn(item['asset']['original_b64'], prompt)
            return {'answer': '{"reply":"I can help with this design."}', 'request_id': 'own-request'}
        with patch('reception_service.integrations.business_images.request_workspace', return_value={'rootPath': str(self.root)}), \
             patch('reception_service.integrations.business_images.VisualQueryRunner') as runner:
            runner.return_value.run_many.side_effect = run
            result = query_business_images(self.native, 'agent', self.identity, self.context, self.inputs,
                'Business context = ', on_call_start=lambda: started.append(True))
        self.assertEqual(len(calls), 1)
        self.assertEqual(started, [True])
        self.assertEqual(result['verified_image_reads'], self.photo['direct_images'])
        self.assertEqual(list((self.root / '.bgcheck_inputs').iterdir()), [])
        self.assertEqual(self.repo.get('image_asset', self.inputs[0]['asset']['sha256']), self.inputs[0]['asset'])

    def test_builder_to_real_runner_with_json_escaped_windows_paths(self):
        import reception_service.workspace_lock as lock
        from reception_service.integrations.accio_native import visual_query as visual
        folder = self.root / 'accounts' / 'test' / 'conversations' / 'dm'
        folder.mkdir(parents=True)
        self.native.cli = self.root / 'accounts' / 'test' / 'bin' / 'accio.exe'
        binding = {'version': 1, 'source': 'default-generated', 'rootPath': str(self.root),
                   'rootRevision': 0, 'state': 'ready'}
        fixture = self

        class FakeProcess:
            returncode = None
            def __init__(self, args, **kwargs):
                self.name = args[args.index('--conversation-name') + 1]
            def communicate(self, input=None, timeout=None):
                manifest = json.loads(input.rsplit('\nimage_inputs = ', 1)[1])
                paths = [Path(row['file_path']) for row in manifest]
                if __import__('os').name == 'nt':
                    fixture.assertNotIn(str(paths[0]), input)
                common = {'conversationId': 'cid', 'agentId': 'agent', 'status': 'finished'}
                calls = [{'id': 'T' + str(i), 'name': 'read', 'arguments': {'file_path': str(path),
                          'image_prompt': 'Read'}} for i, path in enumerate(paths)]
                progress = {c['id']: [{'toolCallId': c['id'], 'toolName': 'read', 'status': 'completed'}] for c in calls}
                rows = [dict(common, role='req', content=input, requestId='req', messageId='A'),
                        dict(common, role='res', messageId='B', branchParentId='A', clientRequestId='req',
                             tool_calls=calls, tool_progress=progress)]
                for i, path in enumerate(paths):
                    rows.append(dict(common, role='tool', messageId='B', parentMessageId='B', branchParentId='B',
                        tool_call_id='T' + str(i), name='read', content='Loaded 1 image:\n  ' + str(path)
                        + ' → https://sc02.alicdn.com/synthetic'))
                rows.append(dict(common, role='res', messageId='B', branchParentId='B', clientRequestId='req', content='answer'))
                meta = {'id': 'cid', 'name': self.name, 'agentId': 'agent', 'metadata': {'workspaceBinding': binding}}
                (folder / 'cid.json').write_text(json.dumps(meta), encoding='utf8')
                (folder / 'cid.message_1.jsonl').write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf8')
                self.returncode = 0
                return '', ''
            def poll(self):
                return self.returncode

        with patch('reception_service.integrations.business_images.request_workspace', return_value=binding), \
             patch.object(visual.subprocess, 'Popen', FakeProcess), patch.object(lock, 'track_native_child'), \
             patch.object(lock, 'release_native_child'):
            result = query_business_images(self.native, 'agent', self.identity, self.context, self.inputs, 'Context = ')
        self.assertEqual(result['request_id'], 'req')
        self.assertEqual(len(result['verified_image_reads']), 2)

    def test_preparation_budget_and_timeout_are_explicit(self):
        self.native.settings = {'image_batch_mib': 1, 'image_prepare_seconds': 1}
        for mode in ('bytes', 'time'):
            with self.subTest(mode=mode), \
                 patch('reception_service.integrations.business_images.request_workspace', return_value={'rootPath': str(self.root)}), \
                 patch('reception_service.integrations.business_images.VisualQueryRunner') as runner:
                if mode == 'bytes':
                    target = patch('reception_service.integrations.business_images.base64.b64decode', return_value=b'x' * (1024 * 1024 + 1))
                else:
                    target = patch('reception_service.integrations.business_images.time.monotonic', side_effect=[0, 2])
                with target, self.assertRaises(ServiceError) as caught:
                    query_business_images(self.native, 'agent', self.identity, self.context, self.inputs, 'Context = ')
                self.assertEqual(caught.exception.code, 'photo_preparation_budget' if mode == 'bytes' else 'photo_preparation_timeout')
                runner.return_value.run_many.assert_not_called()
                self.assertEqual(list((self.root / '.bgcheck_inputs').iterdir()), [])

    def test_shared_preparation_deadline_expires_before_model_call(self):
        with patch('reception_service.integrations.business_images.request_workspace', return_value={'rootPath': str(self.root)}), \
             patch('reception_service.integrations.business_images.VisualQueryRunner') as runner:
            with self.assertRaises(ServiceError) as caught:
                query_business_images(self.native, 'agent', self.identity, self.context, self.inputs,
                                      'Context = ', photo_deadline=0)
            self.assertEqual(caught.exception.code, 'photo_preparation_timeout')
            runner.return_value.run_many.assert_not_called()
            self.assertEqual(list((self.root / '.bgcheck_inputs').iterdir()), [])

    def test_preflight_rejects_foreign_path_role_and_hash_without_starting_process(self):
        from reception_service.integrations.accio_native.visual_query import VisualQueryRunner
        from reception_service.integrations.accio_native.vendor.adapters.common import AdapterError
        original = VisualQueryRunner.run_many
        for field, value in (('file_path', 'foreign'), ('role', 'seller'), ('image_sha256', '0' * 64)):
            def tamper(runner, cli, agent, images, prompt, **kwargs):
                prefix, text = prompt.rsplit('\nimage_inputs = ', 1)
                manifest = json.loads(text)
                manifest[0][field] = value
                return original(runner, cli, agent, images, prefix + '\nimage_inputs = ' + json.dumps(manifest), **kwargs)
            with self.subTest(field=field), \
                 patch('reception_service.integrations.business_images.request_workspace', return_value={'rootPath': str(self.root)}), \
                 patch.object(VisualQueryRunner, 'run_many', tamper), \
                 patch('reception_service.integrations.accio_native.visual_query.subprocess.Popen') as process:
                with self.assertRaises(AdapterError) as caught:
                    query_business_images(self.native, 'agent', self.identity, self.context, self.inputs, 'Context = ')
                self.assertEqual(caught.exception.code, 'args')
                self.assertEqual(caught.exception.visual_stage, 'preflight')
                self.assertEqual(caught.exception.visual_status, 'invalid')
                process.assert_not_called()

    def test_role_tampering_cannot_relabel_a_seller_image_as_buyer_evidence(self):
        from reception_service.context import validated_facts
        from reception_service.photos import direct_photo_sources
        altered = copy.deepcopy(self.photo)
        altered['direct_images'][1]['role'] = 'buyer'
        self.assertEqual(len(with_direct_photo_reads(dict(self.context, photo_context=altered),
            altered['direct_images'])['photo_context']['read_images']), 1)
        seen = with_direct_photo_reads(self.context, self.photo['direct_images'])
        sources = direct_photo_sources(seen['photo_context'])
        self.assertIn('商家图片', sources[self.photo['direct_images'][1]['image_id']])
        self.assertEqual(validated_facts({'facts': {'product': {'value': '[image]',
            'evidence_ids': [self.messages[1]['message_id']]}}}, dict(self.context, historical_evidence=[])), {})

    def test_invalid_seller_ownership_does_not_start_a_business_call(self):
        inputs = copy.deepcopy(self.inputs)
        inputs[1]['message']['own'] = False
        started = []
        with patch('reception_service.integrations.business_images.request_workspace', return_value={'rootPath': str(self.root)}):
            with self.assertRaises(ServiceError):
                query_business_images(self.native, 'agent', self.identity, self.context, inputs,
                    'Business context = ', on_call_start=lambda: started.append(True))
        self.assertEqual(started, [])

    def test_mismatched_originals_do_not_start_a_business_call(self):
        inputs = copy.deepcopy(self.inputs)
        inputs[0]['message']['buyer_id'] = 'foreign'
        started = []
        with patch('reception_service.integrations.business_images.request_workspace', return_value={'rootPath': str(self.root)}):
            with self.assertRaises(ServiceError):
                query_business_images(self.native, 'agent', self.identity, self.context, inputs,
                    'Business context = ', on_call_start=lambda: started.append(True))
        self.assertEqual(started, [])


if __name__ == '__main__':
    unittest.main()
