"""Full manual/native guards with synthetic accounts, temporary stores and fake processes.

No AI, workctl, Lark or network process is executed. The real service, facade,
native gateway, vendor protocol and SQLite claims run against test-only data.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

from reception_service.errors import ServiceError
from reception_service.integrations import build_integrations
from reception_service.integrations.accio_native.vendor.adapters.common import AdapterError, Ledger, digest
from reception_service.integrations.accio_native.vendor.models import FACT_LABELS
from reception_service.repository import new_id
from reception_service.service import ReceptionService
from test_integrations import BUYER, CID, NOW, SELLER, MockProcess as SendProcess, intent, message
from test_grounding_regressions import MockProcess as RegistrationProcess, feishu_settings


class NativeProcess(SendProcess):
    """Only synthetic native reads, snapshot/sealing and controlled submissions."""
    def __init__(self, config):
        super().__init__()
        self.lark = RegistrationProcess(config)
        self.after_command = None
        self.uploads = []

    def run(self, args, **kwargs):
        args = [str(value) for value in args]
        if args[1:3] == ['desktop', 'query']:
            raise AssertionError('AI must never run in native manual guard tests')
        if args[1] == 'base':
            command = args[2]
            if command == '+record-upload-attachment':
                self.lark.calls.append(args)
                if '--dry-run' not in args:
                    self.uploads.append(args)
                result = dict(ok=True, identity='user', data={})
            else:
                result = self.lark.run(args, **kwargs)
        else:
            command = args[3] if args[1:3] == ['workflow', 'chat-analysis'] else args[3]
            result = super().run(args, **kwargs)
        if self.after_command:
            self.after_command(command, args)
        return result


class NativeManualGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix='native-manual-guards-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for target in ('subprocess.run', 'subprocess.Popen', 'socket.create_connection', 'socket.socket'):
            guard = patch(target, side_effect=AssertionError('Real external execution forbidden'))
            guard.start()
            self.addCleanup(guard.stop)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        node = self.root / 'synthetic-node.exe'
        account = self.root / 'accounts' / 'synthetic'
        account.mkdir(parents=True)
        script, cli = account / 'synthetic-mcp.js', account / 'workctl.exe'
        for path in (node, script, cli):
            path.touch()
        os.environ.update(ACCIO_GATEWAY_TOKEN='synthetic-token', ACCIO_NODE_BIN=str(node), ACCIO_MCP_SCRIPT=str(script))
        self.config = feishu_settings(self.root)
        self.config['fields'].pop('inquiry_time')
        self.settings = dict(workctl_path=str(cli), seller_id=SELLER, feishu=self.config)
        self.live = build_integrations('live', self.root / 'native', self.settings)
        self.process = NativeProcess(self.config)
        self.live.native.process = self.live.native.feishu.process = self.process

    def service(self):
        factory = patch('reception_service.service.build_integrations', return_value=self.live)
        factory.start()
        self.addCleanup(factory.stop)
        scheduler = patch.object(ReceptionService, '_schedule', return_value=None)
        scheduler.start()
        self.addCleanup(scheduler.stop)
        # Exact native-call assertions must exclude the independent read-only
        # summary monitor; it remains allowed to read while sends are unknown.
        monitor = patch.object(ReceptionService, '_start_message_monitor', return_value=None)
        monitor.start()
        self.addCleanup(monitor.stop)
        service = ReceptionService(self.root / 'service', 'live', seller_id=SELLER)
        self.addCleanup(service.close)
        checked = self.wait_job(service, service.call('check_connection'))
        self.assertEqual(checked['status'], 'succeeded', checked)
        service.settings['merge_seconds'] = 0
        self.assertFalse(service.get_snapshot()['reception']['running'])
        return service

    def wait_job(self, service, ack):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = service.repo.get('job', ack['job_id'])
            if job and job['status'] not in ('queued', 'running'):
                return job
            threading.Event().wait(0.005)
        self.fail('synthetic job did not complete')

    def manual_draft(self, service, body='Thank you. Our team will review your request.'):
        detail = service.get_conversation(CID)
        previous = detail['draft']
        return service.save_draft(detail['case']['case_id'], body,
                                  previous['revision'] if previous else None, new_id('synthetic-mutation'))

    def confirm(self, service, draft):
        confirmation = service.prepare_send(draft['draft_id'], draft['revision'])
        return self.wait_job(service, service.call('confirm_send', {'confirmation_id': confirmation['confirmation_id']}))

    def registration_case(self, revision=1):
        return dict(case_id='synthetic-case', seller_id=SELLER, buyer_id=BUYER, conversation_id=CID,
                    revision=revision, _inquiry_source_id='buyer-1', customer_name='',
                    facts={'product': dict(value='ceramic mugs', evidence_ids=['buyer-1'], status='confirmed')})

    def test_service_pause_after_seal_then_new_manual_confirmation_can_submit_changed_body(self):
        service = self.service()
        service.set_reception(True, manual_only=True)
        draft = self.manual_draft(service)
        self.process.stage_hook = lambda stage: service.set_reception(False, manual_only=True) if stage == 'seal-reply-preview' else None
        first = self.confirm(service, draft)
        self.assertEqual(first['result']['status'], 'rejected', first)
        old_operation = first['result']['operation_id']
        old_record = self.live.native.ledger.get(old_operation)
        self.assertIs(old_record[2]['submission_started'], False)
        self.assertEqual(self.process.send_count, 0)
        self.assertEqual(service.reconcile_operation(old_operation)['status'], 'rejected')
        self.process.stage_hook = None
        service.set_reception(True, manual_only=True)
        changed = self.manual_draft(service, 'Thank you. Could you share your preferred quantity?')
        preview = self.wait_job(service, service.call('preview_send', {
            'draft_id': changed['draft_id'], 'expected_revision': changed['revision']}))
        self.assertEqual(preview['status'], 'succeeded', preview)
        second = self.confirm(service, changed)
        self.assertEqual(second['result']['status'], 'accepted', second)
        new_operation = second['result']['operation_id']
        self.assertNotEqual(old_operation, new_operation)
        self.assertEqual(self.process.send_count, 1)
        self.assertEqual(self.live.native.ledger.get(old_operation), old_record)
        accepted = service.reconcile_operation(new_operation)
        self.assertEqual(accepted['message_id'], 'native-synthetic-sent-1')
        self.assertIs(accepted['submission_started'], True)
        # Replaying a retired operation remains its original rejected result.
        self.assertEqual(self.live.send(old_record[1], lambda: True)['status'], 'rejected')
        self.assertEqual(self.process.send_count, 1)

    def test_service_unknown_submission_blocks_edit_confirmation_and_new_native_operation(self):
        service = self.service()
        service.set_reception(True, manual_only=True)
        self.process.send_status = 'timeout'
        result = self.confirm(service, self.manual_draft(service))
        self.assertEqual(result['result']['status'], 'unknown', result)
        operation = result['result']['operation_id']
        before = len(self.process.calls)
        receipt = service.reconcile_operation(operation)
        self.assertEqual(receipt['status'], 'unknown')
        self.assertIs(receipt['submission_started'], True)
        self.assertEqual(len(self.process.calls), before)
        with self.assertRaises(ServiceError):
            self.manual_draft(service, 'Thank you. Could you share the quantity?')
        self.assertEqual(self.live.send(intent(operation='new-after-unknown'), lambda: True)['status'], 'rejected')
        self.assertEqual(self.process.send_count, 1)

    def test_service_accepted_source_blocks_second_manual_confirmation_and_native_operation(self):
        service = self.service()
        service.set_reception(True, manual_only=True)
        first = self.confirm(service, self.manual_draft(service))
        self.assertEqual(first['result']['status'], 'accepted', first)
        changed = self.manual_draft(service, 'Thank you. Could you share your preferred quantity?')
        with self.assertRaises(ServiceError):
            service.prepare_send(changed['draft_id'], changed['revision'])
        self.assertEqual(self.live.send(intent(operation='new-after-accepted'), lambda: True)['status'], 'rejected')
        self.assertEqual(self.process.send_count, 1)

    def test_last_send_boundary_rejection_is_explicitly_not_submitted_and_retryable_by_new_op(self):
        checks = []
        def authorize():
            checks.append(True)
            return len(checks) < 4
        first = self.live.send(intent(), authorize)
        self.assertEqual(first['status'], 'rejected', first)
        self.assertIs(first['submission_started'], False)
        self.assertEqual(self.process.send_count, 0)
        second = self.live.send(intent(operation='new-boundary-confirmation', reply='Thank you. Could you share the quantity?'), lambda: True)
        self.assertEqual(second['status'], 'accepted', second)
        self.assertEqual(self.process.send_count, 1)

    def test_automatic_operation_reclaims_an_explicitly_rejected_send_source(self):
        self.process.stage_hook = lambda stage: self.live.revoke() if stage == 'seal-reply-preview' else None
        first_intent = intent()
        first = self.live.send(first_intent, lambda: True)
        self.assertEqual(first['status'], 'rejected')
        self.assertIs(first['submission_started'], False)
        old_record = self.live.native.ledger.get(first_intent['operation_id'])
        self.process.stage_hook = None
        self.assertEqual(self.live.send(first_intent, lambda: True)['status'], 'rejected')
        self.assertEqual(self.process.send_count, 0)
        automatic = intent(operation='automatic-retry', send_mode='automatic', budget_reserved=True,
                           source_timestamp_ms=NOW,
                           frozen_scope=dict(mode='selected', conversation_ids=[CID]))
        self.assertEqual(self.live.send(automatic, lambda: True)['status'], 'accepted')
        self.assertEqual(self.process.send_count, 1)
        self.assertEqual(self.live.native.ledger.get(first_intent['operation_id']), old_record)
        self.assertEqual(self.live.send(automatic, lambda: True)['status'], 'accepted')
        self.assertEqual(self.live.send(intent(operation='human-confirmed-retry'), lambda: True)['status'], 'rejected')
        self.assertEqual(self.process.send_count, 1)

    def test_unknown_accepted_or_missing_explicit_false_source_claim_cannot_be_released(self):
        for index, receipt in enumerate((dict(status='unknown', submission_started=True),
                                         dict(status='accepted', submission_started=True),
                                         dict(status='rejected'), dict(status='rejected', submission_started=0),
                                         dict(status='rejected', submission_started=True))):
            with self.subTest(receipt=receipt):
                ledger = Ledger(self.root / f'guard-{index}.sqlite3')
                ledger.claim('old', 'send:synthetic-source', 'send', intent(operation='old'))
                ledger.finish('old', receipt)
                with self.assertRaises(AdapterError):
                    ledger.claim('new', 'send:synthetic-source', 'send', intent(operation='new'), allow_rejected_send=True)
                self.assertEqual(ledger.get('old')[2], receipt)
                self.assertIsNone(ledger.get('new'))

    def test_reclaim_is_atomic_across_independent_sqlite_ledger_instances(self):
        path = self.root / 'concurrent-ledger.sqlite3'
        ledger = Ledger(path)
        ledger.claim('old', 'send:synthetic-source', 'send', intent(operation='old'))
        ledger.finish('old', dict(status='rejected', submission_started=False))
        start = threading.Barrier(3)
        results = []
        def claim(operation):
            contender = Ledger(path)
            start.wait(3)
            try:
                contender.claim(operation, 'send:synthetic-source', 'send', intent(operation=operation), allow_rejected_send=True)
                results.append('claimed')
            except AdapterError:
                results.append('blocked')
        workers = [threading.Thread(target=claim, args=(name,)) for name in ('new-one', 'new-two')]
        for worker in workers:
            worker.start()
        start.wait(3)
        for worker in workers:
            worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertCountEqual(results, ['claimed', 'blocked'])
        self.assertEqual(ledger.get('old')[2], dict(status='rejected', submission_started=False))
        with closing(sqlite3.connect(path)) as db:
            self.assertIsNone(db.execute('SELECT logical_key FROM operations WHERE operation_id="old"').fetchone()[0])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM operations WHERE logical_key=?', ('send:synthetic-source',)).fetchone()[0], 1)
        with self.assertRaises(AdapterError):
            ledger.rearm_send('old', intent(operation='old'))

    def test_source_reclaim_default_and_other_logical_kinds_stay_closed(self):
        ledger = self.live.native.ledger
        ledger.claim('old', 'send:source', 'send', intent(operation='old'))
        ledger.finish('old', dict(status='rejected', submission_started=False))
        with self.assertRaises(AdapterError):
            ledger.claim('new', 'send:source', 'send', intent(operation='new'))
        ledger.claim('old-register', 'register:source', 'register', {})
        ledger.finish('old-register', dict(status='rejected', submission_started=False))
        with self.assertRaises(AdapterError):
            ledger.claim('new-register', 'register:source', 'register', {}, allow_rejected_send=True)
        with self.assertRaises(AdapterError):
            ledger.claim('old', 'send:source', 'send', intent(operation='old', reply='A changed body'), allow_rejected_send=True)

    def test_service_feishu_revocation_blocks_write_and_fresh_authorization_resumes_same_operation(self):
        service = self.service()
        service.settings['feishu'].update(enabled=True, write_enabled=True)
        case = service.get_conversation(CID)['case']
        case['facts'] = self.registration_case()['facts']
        service.repo.put('case', case['case_id'], case)
        service._queue_outbox(case)
        def switch_mode(command, args):
            if command == '+form-submit' and '--dry-run' in args:
                service.set_reception(True, manual_only=True)
        self.process.after_command = switch_mode
        result = self.wait_job(service, service.call('run_outbox'))
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(self.process.lark.submissions, [])
        outbox = service.list_outbox()[0]
        self.assertEqual(outbox['status'], 'failed', outbox)
        record = self.live.native.ledger.get(outbox['operation_id'])
        self.assertEqual(record[2]['status'], 'rejected')
        self.assertIs(record[2]['submission_started'], False)
        self.assertTrue(service.get_snapshot()['reception']['manual_only'])
        self.assertEqual(service.repo.list('registration'), [])
        self.process.after_command = None
        for check in ('ai', 'send_preview'):
            service.checks[check]['status'] = 'passed'
        service.set_reception(True, manual_only=False)
        resumed = self.wait_job(service, service.call('run_outbox'))
        self.assertEqual(resumed['status'], 'succeeded', resumed)
        self.assertEqual(len(self.process.lark.submissions), 1)
        self.assertEqual(service.repo.list('registration')[0]['operation_id'], outbox['operation_id'])
        self.assertEqual(len(service.repo.list('outbox')), 1)

    def test_registration_without_definite_no_submission_never_replays(self):
        operation = 'synthetic-registration-no-replay'
        case = self.registration_case()
        first = self.live.register(case, {}, operation, authorization=lambda: True)
        self.assertIn(first['status'], ('confirmed', 'accepted', 'succeeded'), first)
        submitted = len(self.process.lark.submissions)
        for result in ({'status': 'unknown'}, {'status': 'rejected'},
                       {'status': 'rejected', 'submission_started': True}):
            with self.subTest(result=result):
                self.live.native.ledger.finish(operation, result)
                self.live.register(case, {}, operation, authorization=lambda: True)
                self.assertEqual(len(self.process.lark.submissions), submitted)

    def test_registration_rechecks_after_history_schema_and_dry_run(self):
        for index, stage in enumerate(('query_conversation_msg', '+field-list', '+form-submit')):
            with self.subTest(stage=stage):
                def revoke(command, args):
                    if command == stage and (stage != '+form-submit' or '--dry-run' in args):
                        self.live.revoke()
                self.process.after_command = revoke
                result = self.live.register(self.registration_case(), {}, f'revoked-stage-{index}', authorization=lambda: True)
                self.assertEqual(result['status'], 'rejected', result)
                self.assertIs(result['submission_started'], False)
                self.assertEqual(self.process.lark.submissions, [])

    def test_registration_revocation_inside_callback_cannot_restore_permission(self):
        def authorize():
            self.live.revoke()
            return True
        result = self.live.register(self.registration_case(), {}, 'callback-revoke', authorization=authorize)
        self.assertEqual(result['status'], 'rejected')
        self.assertIs(result['submission_started'], False)
        self.assertEqual(self.process.lark.submissions, [])

    def test_registration_callback_must_return_literal_true_and_failures_never_submit(self):
        def failed():
            raise RuntimeError('synthetic private permission failure')
        for index, callback in enumerate((lambda: False, lambda: 1, failed, False)):
            with self.subTest(callback=index):
                result = self.live.register(self.registration_case(), {}, f'callback-failure-{index}', authorization=callback)
                self.assertEqual(result['status'], 'rejected')
                self.assertNotIn('private', json.dumps(result))
                self.assertEqual(self.process.lark.submissions, [])

    def test_registration_update_dry_run_revocation_blocks_batch_update(self):
        first = self.live.register(self.registration_case(), {}, 'initial-register', authorization=lambda: True)
        self.assertEqual(first['status'], 'confirmed', first)
        case = self.registration_case(2)
        case['facts']['quantity'] = dict(value='500 pieces', evidence_ids=['buyer-1'], status='confirmed')
        def revoke(command, args):
            if command == '+record-batch-update' and '--dry-run' in args:
                self.live.revoke()
        self.process.after_command = revoke
        result = self.live.register(case, dict(first, case_id=case['case_id']), 'revoked-update', authorization=lambda: True)
        self.assertEqual(result['status'], 'rejected', result)
        self.assertIs(result['submission_started'], False)
        self.assertEqual(len(self.process.lark.submissions), 1)

    def attachment_payload(self, *, changed=False):
        mapping = self.config['fields']
        for role in ('main_image', 'sku_image'):
            mapping[role] = dict(name=role, type='attachment')
        # LiveIntegration owns a deep copy of settings; adapter configuration is explicit.
        self.live.settings['feishu']['fields'] = deepcopy(mapping)
        self.live.settings['feishu']['registration'] = dict(allowed_image_hosts=['images.example.com'])
        images = [dict(kind=role, source_message_id='buyer-1', url=f'https://images.example.com/{role}.png')
                  for role in ('main_image', 'sku_image')]
        case = self.registration_case(2)
        case.pop('_inquiry_source_id')
        case.update(version=2, source_message_ids=['buyer-1'], attachments=images)
        if changed:
            case['facts']['quantity'] = dict(value='500 pieces', evidence_ids=['buyer-1'], status='confirmed')
        self.process.lark.rows = [dict(record_id='recSynthetic', number='SYNTHETIC-001', buyer_id=BUYER,
            account=self.config['account_option'], status=self.config['initial_status'],
            product_name=FACT_LABELS['product'] + '：ceramic mugs', main_image=[], sku_image=[])]
        return dict(case=case, previous=dict(record_id='recSynthetic', number='SYNTHETIC-001', case_id=case['case_id']),
                    operation_id='attachment-update', verified_messages=[dict(message(), attachments=images)])

    def test_upload_dry_run_revocation_prevents_first_real_attachment_write(self):
        payload = self.attachment_payload()
        allowed = {'value': True}
        self.process.after_command = lambda command, args: allowed.update(value=False) if command == '+record-upload-attachment' and '--dry-run' in args else None
        with patch('reception_service.integrations.accio_native.vendor.adapters.feishu.download_image', return_value=b'\x89PNG\r\n\x1a\nsynthetic-image'):
            result = self.live.native.feishu.register(payload, authorization=lambda: allowed['value'])
        self.assertEqual(result['status'], 'rejected', result)
        self.assertIs(result['submission_started'], False)
        self.assertEqual(self.process.uploads, [])
        self.assertEqual(self.process.lark.submissions, [])

    def test_revocation_between_update_and_upload_retains_unknown_and_stops_remaining_writes(self):
        payload = self.attachment_payload(changed=True)
        allowed = {'value': True}
        def revoke(command, args):
            if command == '+record-batch-update' and '--dry-run' not in args:
                allowed['value'] = False
        self.process.after_command = revoke
        with patch('reception_service.integrations.accio_native.vendor.adapters.feishu.download_image', return_value=b'\x89PNG\r\n\x1a\nsynthetic-image'):
            result = self.live.native.feishu.register(payload, authorization=lambda: allowed['value'])
        self.assertEqual(result['status'], 'unknown', result)
        self.assertIs(result['submission_started'], True)
        self.assertEqual(len(self.process.lark.submissions), 1)
        self.assertEqual(self.process.uploads, [])
        self.assertEqual(self.live.native.feishu.register(payload, authorization=lambda: True)['status'], 'unknown')
        self.assertEqual(len(self.process.lark.submissions), 1)

    def test_revocation_between_attachment_uploads_stops_second_upload(self):
        payload = self.attachment_payload()
        allowed = {'value': True}
        def revoke(command, args):
            if command == '+record-upload-attachment' and '--dry-run' not in args:
                allowed['value'] = False
        self.process.after_command = revoke
        with patch('reception_service.integrations.accio_native.vendor.adapters.feishu.download_image', return_value=b'\x89PNG\r\n\x1a\nsynthetic-image'):
            result = self.live.native.feishu.register(payload, authorization=lambda: allowed['value'])
        self.assertEqual(result['status'], 'unknown', result)
        self.assertIs(result['submission_started'], True)
        self.assertEqual(len(self.process.uploads), 1)

    def test_successful_registration_and_replay_never_serialize_authorization(self):
        calls = []
        def authorize():
            calls.append(True)
            return True
        case = self.registration_case()
        first = self.live.register(case, {}, 'authorized-register', authorization=authorize)
        self.assertEqual(first['status'], 'confirmed', first)
        self.assertIs(first['submission_started'], True)
        self.assertGreaterEqual(len(calls), 4)
        request = self.live.native.ledger.get('authorized-register')[1]
        serialized = json.dumps(request)
        self.assertNotIn('authorization', serialized)
        self.assertNotIn('<function', serialized)
        self.assertNotIn('authorization', case)
        self.assertIsInstance(digest(request), str)
        replay = self.live.register(case, {}, 'authorized-register', authorization=lambda: False)
        self.assertEqual(replay, first)
        self.assertEqual(len(self.process.lark.submissions), 1)


if __name__ == '__main__':
    unittest.main()
