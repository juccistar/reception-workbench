"""Offline fixtures follow A41's verified read shape; no external calls."""
import copy
import hashlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from reception_service.integrations.accio_native import visual_query as v


class VisualTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.image = self.root / 'image.png'
        self.image.write_bytes(b'\x89PNG\r\n\x1a\nsynthetic')
        self.digest = hashlib.sha256(self.image.read_bytes()).hexdigest()
        self.prompt = 'Read ' + str(self.image)
        self.folder = self.root / 'accounts' / 'test' / 'conversations' / 'dm'
        self.folder.mkdir(parents=True)
        self.cli = self.root / 'accounts' / 'test' / 'bin' / 'accio'
        common = {'conversationId': 'cid', 'agentId': 'agent'}
        self.rows = [
            dict(common, role='req', status='finished', content=self.prompt, requestId='req', messageId='A', seq=1),
            dict(common, role='res', status='finished', messageId='B', branchParentId='A', clientRequestId='req', seq=2,
                 tool_calls=[{'id': 'C', 'name': 'read', 'arguments': {'file_path': str(self.image), 'image_prompt': 'Describe'}}],
                 tool_progress={'C': [{'toolCallId': 'C', 'toolName': 'read', 'status': 'completed'}]}),
            dict(common, role='tool', status='finished', messageId='B', parentMessageId='B', branchParentId='B',
                 tool_call_id='C', name='read', content='Loaded 1 image:\n  ' + str(self.image) + ' → https://sc02.alicdn.com/kf/synthetic.png', seq=3),
            dict(common, role='res', status='finished', messageId='B', branchParentId='B', clientRequestId='req',
                 content='Synthetic answer', seq=4),
        ]
        self.binding = {'version': 1, 'source': 'default-generated', 'rootPath': str(self.root), 'rootRevision': 0, 'state': 'ready'}

    def check(self, rows):
        return v.validate_trace(rows, 'cid', 'agent', self.prompt, self.image)

    def records(self):
        return v._Records(self.cli, '接待分析草稿-视觉-test', 'agent', self.prompt, self.image, time.time(), time.monotonic() + 5)

    def write_records(self, rows=None, meta=None):
        meta = meta or {'id': 'cid', 'name': '接待分析草稿-视觉-test', 'agentId': 'agent', 'metadata': {'workspaceBinding': self.binding}}
        (self.folder / 'cid.json').write_text(json.dumps(meta), encoding='utf8')
        (self.folder / 'cid.message_1.jsonl').write_text('\n'.join(json.dumps(r) for r in (rows or self.rows)), encoding='utf8')

    def test_synthetic_success(self):
        self.assertEqual(self.check(self.rows), ('Synthetic answer', 'req'))
        self.write_records()
        self.assertEqual(self.records().find()['workspace_binding'], self.binding)

    def test_windows_stdin_newlines(self):
        rows = copy.deepcopy(self.rows)
        rows[0]['content'] = 'Synthetic\r\n' + self.prompt
        self.assertEqual(v.validate_trace(rows, 'cid', 'agent', 'Synthetic\n' + self.prompt,
                                         self.image), ('Synthetic answer', 'req'))

    def test_workspace_requires_actual_nested_metadata(self):
        self.write_records(meta={'id': 'cid', 'name': '接待分析草稿-视觉-test', 'agentId': 'agent',
                                 'workspaceBinding': self.binding})
        with self.assertRaises(v.AdapterError):
            v.request_workspace(self.cli, 'agent', '接待分析草稿-视觉-test')

    def test_a39_error_progress_and_content(self):
        rows = copy.deepcopy(self.rows)
        rows[1]['tool_progress']['C'][0]['status'] = 'error'
        rows[2]['content'] = 'Error: Local image path is outside approved roots'
        rows[3]['content'] = '{"status":"unavailable"}'
        with self.assertRaises(ValueError):
            self.check(rows)
        rows[1]['tool_progress']['C'][0]['status'] = 'completed'
        with self.assertRaises(ValueError):
            self.check(rows)

    def test_wrong_correlation_and_extra_tools(self):
        mutations = [
            lambda r: r[0].update(content='wrong'),
            lambda r: r[2].update(agentId='other'),
            lambda r: r[3].update(conversationId='other'),
            lambda r: r[3].update(clientRequestId='other'),
            lambda r: r[2].update(tool_call_id='other'),
            lambda r: r[1]['tool_calls'][0]['arguments'].update(file_path='other'),
            lambda r: r[1]['tool_calls'].append({'id':'D', 'name':'write', 'arguments':{}}),
            lambda r: r[1]['tool_calls'][0].update(name='exec'),
            lambda r: r[1].update(approvalRequired=True),
            lambda r: r[1].update(pendingToolCalls=['C']),
            lambda r: r.append(copy.deepcopy(r[-1])),
            lambda r: r[3].update(seq=2),
            lambda r: r[3].update(content=''),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                rows = copy.deepcopy(self.rows)
                mutation(rows)
                with self.assertRaises(ValueError):
                    self.check(rows)

    def test_image_validation(self):
        self.assertEqual(v._image(self.image, self.digest), self.image)
        with self.assertRaises(ValueError):
            v._image(self.image, '0' * 64)
        self.image.write_bytes(b'not an image')
        with self.assertRaises(ValueError):
            v._image(self.image, hashlib.sha256(b'not an image').hexdigest())
        self.image.write_bytes(b'\x89PNG\r\n\x1a\n' + b'x' * (6 * 1024 * 1024))
        with self.assertRaises(ValueError):
            v._image(self.image, hashlib.sha256(self.image.read_bytes()).hexdigest())

    def test_partial_native_write_remains_pending_then_completes(self):
        self.write_records()
        path = self.folder / 'cid.message_1.jsonl'
        complete = path.read_text(encoding='utf8')
        path.write_text(complete[:-12], encoding='utf8')
        records = self.records()
        self.assertIsNone(records.find())
        self.assertNotEqual(records.status, 'rejected')
        path.write_text(complete, encoding='utf8')
        self.assertEqual(records.find()['answer'], 'Synthetic answer')

    def test_unchanged_records_do_not_consume_polling_read_budget(self):
        self.write_records(rows=self.rows[:3])
        for index in range(50):
            (self.folder / ('other-' + str(index) + '.json')).write_text(
                json.dumps({'id': 'other-' + str(index), 'name': 'unrelated', 'agentId': 'agent'}), encoding='utf8')
        records = self.records()
        self.assertIsNone(records.find())
        reads, size = records.reads, records.bytes
        for _ in range(12):
            self.assertIsNone(records.find())
        self.assertEqual((records.reads, records.bytes), (reads, size))
        self.write_records()
        self.assertEqual(records.find()['answer'], 'Synthetic answer')

    def test_metadata_and_bounds(self):
        self.write_records()
        (self.folder / 'cid.message_1.jsonl').write_text('{bad', encoding='utf8')
        self.assertIsNone(self.records().find())
        self.write_records()
        meta = {'id': 'cid', 'name': '接待分析草稿-视觉-test', 'agentId': 'other', 'metadata': {'workspaceBinding': self.binding}}
        self.write_records(meta=meta)
        self.assertIsNone(self.records().find())
        meta['agentId'] = 'agent'
        meta['metadata']['workspaceBinding'] = dict(self.binding, rootPath=str(self.root / 'other'))
        self.write_records(meta=meta)
        self.assertIsNone(self.records().find())
        self.write_records()
        with mock.patch.object(v, '_MAX_MESSAGES', 10):
            records = self.records()
            self.assertIsNone(records.find())
            self.assertEqual(records.status, 'budget_exhausted')
        with mock.patch.object(v, '_MAX_ENTRIES', 1):
            records = self.records()
            self.assertIsNone(records.find())
            self.assertEqual(records.status, 'budget_exhausted')

    def test_runner_uses_only_own_record_when_stdout_fails(self):
        import reception_service.workspace_lock as lock
        runner = v.VisualQueryRunner(self.root)
        fixture = self

        class FakeProcess:
            returncode = None
            def __init__(self, args, **kwargs):
                fixture.assertEqual(args[1:3], ['desktop', 'query'])
                fixture.assertNotIn('--approve-tools', args)
                fixture.assertEqual(args[3:8], ['--agent-id', 'agent', '--input', '-', '--json'])
                name = args[args.index('--conversation-name') + 1]
                fixture.assertTrue(name.startswith('接待分析草稿-视觉-'))
                fixture.write_records(meta={'id': 'cid', 'name': name, 'agentId': 'agent', 'metadata': {'workspaceBinding': fixture.binding}})
            def communicate(self, input=None, timeout=None):
                fixture.assertEqual(input, fixture.prompt)
                self.returncode = 1
                return 'broken stdout', ''
            def poll(self):
                return self.returncode

        with mock.patch.object(v.subprocess, 'Popen', FakeProcess), mock.patch.object(lock, 'track_native_child'), mock.patch.object(lock, 'release_native_child'):
            result = runner.run(self.cli, 'agent', self.image, self.digest, self.prompt, timeout=2)
        self.assertEqual(result['answer'], 'Synthetic answer')
        self.assertEqual(result['request_id'], 'req')
        self.assertEqual(runner.last_request['conversation_name'], result['conversation_name'])
        self.assertEqual(runner.last_request['prompt_sha256'], hashlib.sha256(self.prompt.encode()).hexdigest())

    def test_request_workspace_only_reads_metadata(self):
        self.write_records()
        (self.folder / 'cid.message_1.jsonl').write_text('malformed', encoding='utf8')
        self.assertEqual(v.request_workspace(self.cli, 'agent', '接待分析草稿-视觉-test'), self.binding)
        with self.assertRaises(v.AdapterError):
            v.request_workspace(self.cli, 'wrong', '接待分析草稿-视觉-test')
        second = {'id': 'other', 'name': '接待分析草稿-视觉-test', 'agentId': 'agent', 'metadata': {'workspaceBinding': self.binding}}
        (self.folder / 'other.json').write_text(json.dumps(second), encoding='utf8')
        with self.assertRaises(v.AdapterError):
            v.request_workspace(self.cli, 'agent', '接待分析草稿-视觉-test')

    def test_duplicate_metadata_and_final(self):
        self.write_records()
        second = {'id':'other', 'name':'接待分析草稿-视觉-test', 'agentId':'agent', 'metadata': {'workspaceBinding': self.binding}}
        (self.folder / 'other.json').write_text(json.dumps(second), encoding='utf8')
        self.assertIsNone(self.records().find())
        (self.folder / 'other.json').unlink()
        rows = copy.deepcopy(self.rows)
        rows.append(dict(rows[-1], content='conflicting answer', seq=5))
        self.write_records(rows)
        self.assertIsNone(self.records().find())


if __name__ == '__main__':
    unittest.main()
