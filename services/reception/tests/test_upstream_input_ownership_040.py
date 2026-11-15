import threading
import unittest
from unittest.mock import patch

from reception_service.errors import ServiceError
from reception_service.message_photos import MessagePhotosMixin
from reception_service.media_resources import PROCESS_MEDIA_BUDGET
from reception_service.model_media import OwnedInput, OwnedPhotoInputs
from reception_service.integrations.live import LiveIntegration
import test_model_inputs_040 as fixture
import test_model_generation_040 as generation_fixture


class PhotoSource(MessagePhotosMixin):
    def __init__(self, context, inputs):
        self._lock = threading.RLock()
        self.context, self.inputs = context, inputs
        self.repo = self
        self.version = 1
        self.reads = 0
        self.before = PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes']

    def image_transfer_metadata(self, kind, key):
        asset = self.inputs[0]['asset']
        return {'sha256': asset['sha256'], 'size': asset['size'], 'content_type': 'text',
                'content_length': len(asset['original_b64']), 'version': self.version}

    def get(self, kind, key, default=None):
        if kind == 'image_asset':
            self.reads += 1
            assert PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes'] > self.before
            return self.inputs[0]['asset']
        return default

    def _photo_target(self, *args):
        return self.inputs[0]['message']

    def _photo_state(self, message):
        return {'sha256': self.inputs[0]['asset']['sha256'], 'status': 'ready'}


class UpstreamOwnershipTest(unittest.TestCase):
    def setUp(self):
        context, inputs = fixture.fixture()
        self.source = PhotoSource(context, inputs)
        self.context = context
        self.before = self.source.before

    def test_budget_precedes_original_read_and_busy_does_not_read(self):
        budget = PROCESS_MEDIA_BUDGET
        blocker = budget.acquire(budget.limit - self.before)
        try:
            with self.assertRaises(ServiceError):
                self.source._direct_photo_inputs(self.context)
            self.assertEqual(self.source.reads, 0)
        finally:
            blocker.close()
        inputs = self.source._direct_photo_inputs(self.context)
        self.assertEqual(self.source.reads, 1)
        inputs.owner.close_if_owned()
        self.assertEqual(budget.snapshot()['reserved_bytes'], self.before)

    def test_failure_during_snapshot_releases(self):
        with patch.object(self.source, '_photo_target', side_effect=ValueError('changed')):
            with self.assertRaises(ValueError):
                self.source._direct_photo_inputs(self.context)
        self.assertEqual(PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes'], self.before)

    def test_live_reuses_owner_and_resolve_failure_releases(self):
        inputs = self.source._direct_photo_inputs(self.context)
        owner = inputs.owner
        live = LiveIntegration.__new__(LiveIntegration)
        live.settings = {}
        from reception_service.model_router import ModelRouter
        live._model_router = ModelRouter()
        self.addCleanup(live._model_router.close)
        live._query_state = threading.local()
        with self.assertRaises(ServiceError):
            live._business_query('rules', self.context, inputs)
        self.assertTrue(owner.lease._closed)
        self.assertEqual(PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes'], self.before)

    def test_live_early_failures_release_owner(self):
        from reception_service.model_media import PhotoDeadline
        for failure in ('expired', 'cancelled', 'unconfigured'):
            with self.subTest(failure=failure):
                inputs = self.source._direct_photo_inputs(self.context)
                live = LiveIntegration.__new__(LiveIntegration)
                live.settings = {}
                live._model_router = None
                live._query_state = threading.local()
                cancelled = threading.Event()
                if failure == 'cancelled':
                    cancelled.set()
                deadline = PhotoDeadline(60, 0) if failure == 'expired' else None
                with patch('reception_service.integrations.live.time.monotonic', return_value=200):
                    with self.assertRaises(ServiceError):
                        live._business_query('rules', self.context, inputs, cancel_event=cancelled,
                                             photo_deadline=deadline)
                self.assertTrue(inputs.owner.lease._closed)
                self.assertEqual(PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes'], self.before)

    def test_handoff_preserves_single_owner_and_cleans_up(self):
        inputs = self.source._direct_photo_inputs(self.context)
        owner = inputs.owner
        generation = generation_fixture.GenerationTest('test_generation_image_evidence_and_before_send')
        generation.setUp()
        self.addCleanup(generation.doCleanups)
        live = LiveIntegration.__new__(LiveIntegration)
        live.settings = {}
        live._model_router = generation.router
        live._query_state = threading.local()
        with patch('reception_service.model_media.OwnedInput', side_effect=AssertionError('second owner')):
            live._business_query('rules', self.context, inputs)
        self.assertTrue(owner.lease._closed)
        self.assertEqual(PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes'], self.before)

    def test_manual_identity_and_configuration_failures_release(self):
        from contextlib import nullcontext
        from types import SimpleNamespace
        from reception_service.conversation_assistance import ConversationAssistanceMixin
        context = {'conversation_id': 'c', 'buyer_id': 'b', 'case_id': None,
                   'unsent_reply': {'body': 'draft', 'revision': 1},
                   'supplement_image_refs': [{'attachment_id': 'a'}]}
        for failure in ('identity', 'configuration'):
            owner = OwnedInput(1024)
            inputs = OwnedPhotoInputs(owner)
            repo = SimpleNamespace(get=lambda *args: None, list=lambda *args, **kwargs: [])
            source = SimpleNamespace(_lock=threading.RLock(), repo=repo, seller_id='s', mode='live',
                _read_token=lambda facade: None, _require=lambda *args: {}, _supplement=lambda cid: {},
                _require_current_read=lambda token: None, _epoch=1, _closing=False,
                _manual_context=lambda *args: (context, 'f'), _direct_photo_inputs=lambda c: inputs,
                _job_phase=lambda *args: nullcontext(), _generation_cancel_event=lambda job: None)
            def identity(facade):
                if failure == 'identity':
                    raise ValueError('identity')
            def configuration():
                raise ValueError('configuration')
            source._ensure_identity = identity
            source._freeze_reception_model = configuration
            with self.assertRaises(ValueError):
                ConversationAssistanceMixin._task_manual_suggestion(source, {'context': context, 'fingerprint': 'f'}, 'j', 1, object())
            self.assertTrue(owner.lease._closed)
            self.assertEqual(PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes'], self.before)

    def test_supplement_deadline_starts_before_snapshot_and_clamps(self):
        from reception_service.model_media import start_photo_deadline
        with patch('reception_service.model_media.time.monotonic', return_value=100):
            deadline = start_photo_deadline({'image_prepare_seconds': 600})
        self.assertEqual(deadline, 160)
        self.assertEqual(deadline.started, 100)

    def test_old_600_second_setting_clamps_from_prepare_entry(self):
        source = self.source
        source.settings = {'image_prepare_seconds': 600}
        source._read_token = lambda facade: 'token'
        source.list = lambda *args, **kwargs: []
        source._require = lambda kind, key: {'case_id': None} if kind == 'job' else {}
        source._supplement = lambda cid: {}
        source._generation_cancel_event = lambda job: None
        source._job_update = lambda *args, **kwargs: None
        with patch('reception_service.message_photos.time.monotonic', return_value=100):
            guard = source._prepare_direct_photos('c', 'j', 1, object())
        self.assertEqual(guard.photo_deadline, 160)

    def test_live_deadline_uses_original_start_after_settings_change(self):
        from unittest.mock import Mock
        from reception_service.model_media import PhotoDeadline
        live = LiveIntegration.__new__(LiveIntegration)
        live.settings = {'image_prepare_seconds': 1}
        live._query_state = threading.local()
        live._model_router = Mock()
        live._model_router.generate.return_value = {'output': {'ok': True}}
        with patch('reception_service.integrations.live.time.monotonic', return_value=150):
            live._business_query('rules', {}, [], photo_deadline=PhotoDeadline(160, 100))
        self.assertEqual(live._model_router.generate.call_args.kwargs['deadline'], 280)

    def test_transferred_owner_waits_for_completion(self):
        owner = OwnedInput(10)
        inputs = OwnedPhotoInputs(owner)
        completed = inputs.owner.transfer()
        inputs.owner.close_if_owned()
        self.assertGreater(PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes'], self.before)
        completed()
        self.assertEqual(PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes'], self.before)
