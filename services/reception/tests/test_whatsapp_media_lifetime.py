import base64
from contextlib import nullcontext
import hashlib
import io
import threading
import unittest
from unittest.mock import patch

from PIL import Image

from reception_service.whatsapp_media import WhatsAppMediaMixin
from reception_service.errors import ServiceError


class Repo:
    def __init__(self):
        self.rows = {}

    def get(self, kind, key):
        return self.rows.get((kind, key))

    def put(self, kind, key, value):
        self.rows[kind, key] = value

    def transaction(self):
        return nullcontext()


class Service(WhatsAppMediaMixin):
    def __init__(self):
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._closing = False
        self._wa_transfers = {}
        self.repo = Repo()
        self.seller_id = 'seller'
        self.changed = 0

    def _wa_check_connection(self, account, epoch):
        if (account, epoch) != ('account', 'epoch'):
            raise ServiceError('source_changed', 'changed')

    def _changed(self):
        self.changed += 1

    def _schedule_whatsapp(self):
        pass


class MediaLifetimeTests(unittest.TestCase):
    def pending(self):
        from reception_service.whatsapp import _key
        service = Service()
        version = _key('account', 'epoch', 'chat', 'message', '100')
        identity = ('account', 'epoch', 'chat', 'message', version)
        key = _key('seller', 'account', 'message')
        service.repo.put('wa_message', key, dict(seller_id='seller', account_id='account', chat_id='chat',
                         message_id='message', timestamp_ms=100, media=dict(status='pending', source_version=version)))
        self.addCleanup(lambda: [service._wa_drop_transfer(tid) for tid in list(service._wa_transfers)])
        return service, identity, key

    def test_static_gif_finishes_without_reencoding_original(self):
        service, identity, key = self.pending()
        with io.BytesIO() as out, Image.new('RGB', (2, 3), 'red') as image:
            image.save(out, 'GIF')
            data = out.getvalue()
        tid = service._wa_media_begin(*identity, 'image/gif')['transfer_id']
        service._wa_media_chunk(tid, *identity, 0, base64.b64encode(data).decode())
        result = service._wa_media_finish(tid, *identity, len(data), hashlib.sha256(data).hexdigest())
        self.assertEqual(service.repo.get('wa_message', key)['media']['status'], 'ready')
        asset = service.repo.get('wa_media_asset', result['media_id'])
        self.assertEqual(asset['mime'], 'image/gif')
        self.assertEqual(base64.b64decode(asset['original_b64']), data)

    def test_resource_failure_records_failed_and_old_error_cannot_touch_retry(self):
        service, identity, key = self.pending()
        with io.BytesIO() as out, Image.new('RGB', (2, 3), 'red') as image:
            image.save(out, 'PNG')
            data = out.getvalue()
        tid = service._wa_media_begin(*identity, 'image/png')['transfer_id']
        service._wa_media_chunk(tid, *identity, 0, base64.b64encode(data).decode())
        with patch('reception_service.whatsapp_media.validate_original', side_effect=ServiceError('model_resource_busy', 'busy')):
            with self.assertRaises(ServiceError) as error:
                service._wa_media_finish(tid, *identity, len(data), hashlib.sha256(data).hexdigest())
        self.assertEqual(error.exception.code, 'model_resource_busy')
        self.assertEqual(service.repo.get('wa_message', key)['media']['status'], 'failed')
        self.assertFalse(service._wa_transfers)
        service.repo.get('wa_message', key)['media']['status'] = 'pending'
        current = service._wa_media_begin(*identity, 'image/png')['transfer_id']
        with self.assertRaises(ServiceError):
            service._wa_media_fail(tid, *identity, 'late')
        self.assertIn(current, service._wa_transfers)
        self.assertEqual(service.repo.get('wa_message', key)['media']['status'], 'pending')
        service._wa_expire_transfer(tid, identity)
        self.assertEqual(service.repo.get('wa_message', key)['media']['status'], 'pending')

    def test_chunk_admission_grows_with_data_and_releases_on_pressure(self):
        from reception_service.media_resources import PROCESS_MEDIA_BUDGET
        baseline = PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes']
        service, identity, key = self.pending()
        tid = service._wa_media_begin(*identity, 'image/png')['transfer_id']
        self.assertLess(service._wa_transfers[tid]['lease'].amount, 2 * 1024 * 1024)
        block = base64.b64encode(b'x' * (256 * 1024)).decode()
        service._wa_media_chunk(tid, *identity, 0, block)
        used = PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes']
        with PROCESS_MEDIA_BUDGET.acquire(PROCESS_MEDIA_BUDGET.limit - used - 256 * 1024):
            with self.assertRaises(ServiceError) as error:
                service._wa_media_chunk(tid, *identity, 256 * 1024, block)
            self.assertEqual(error.exception.code, 'transfer_unavailable')
            self.assertEqual(service.repo.get('wa_message', key)['media']['status'], 'failed')
            self.assertFalse(service._wa_transfers)
        self.assertEqual(PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes'], baseline)

    def test_expiry_marks_pending_source_failed_without_another_rpc(self):
        from reception_service.whatsapp import _key
        service = Service()
        version = _key('account', 'epoch', 'chat', 'message', '100')
        identity = ('account', 'epoch', 'chat', 'message', version)
        key = _key('seller', 'account', 'message')
        service.repo.put('wa_message', key, dict(seller_id='seller', account_id='account', chat_id='chat', message_id='message', timestamp_ms=100, media=dict(status='pending', source_version=version)))
        transfer_id = service._wa_media_begin(*identity, 'image/png')['transfer_id']
        service._wa_expire_transfer(transfer_id, identity)
        self.assertNotIn(transfer_id, service._wa_transfers)
        self.assertEqual(service.repo.get('wa_message', key)['media']['status'], 'failed')
        self.assertEqual(service.changed, 1)

    def test_twenty_mib_limit_rejects_the_next_byte(self):
        import base64
        from reception_service.whatsapp import _key
        service = Service()
        version = _key('account', 'epoch', 'chat', 'message', '100')
        identity = ('account', 'epoch', 'chat', 'message', version)
        service.repo.put('wa_message', _key('seller', 'account', 'message'), dict(
            seller_id='seller', account_id='account', chat_id='chat', message_id='message', timestamp_ms=100,
            media=dict(status='pending', source_version=version)))
        transfer = service._wa_media_begin(*identity, 'image/png')['transfer_id']
        chunk = base64.b64encode(b'x' * (256 * 1024)).decode()
        for index in range(80):
            service._wa_media_chunk(transfer, *identity, index * 256 * 1024, chunk)
        with self.assertRaises(ServiceError):
            service._wa_media_chunk(transfer, *identity, 20 * 1024 * 1024, 'eA==')
        self.assertNotIn(transfer, service._wa_transfers)

    def test_wrong_identity_cannot_remove_another_transfer(self):
        service = Service()
        service._wa_transfers['id'] = dict(identity=('a', 'b', 'c', 'd', 'e'), deadline=10**20)
        with self.assertRaises(ServiceError):
            service._wa_media_transfer('id', ('other', 'b', 'c', 'd', 'e'))
        self.assertIn('id', service._wa_transfers)
