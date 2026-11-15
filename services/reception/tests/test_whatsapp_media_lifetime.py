import threading
import unittest

from reception_service.whatsapp_media import WhatsAppMediaMixin
from reception_service.errors import ServiceError


class Repo:
    def __init__(self):
        self.rows = {}

    def get(self, kind, key):
        return self.rows.get((kind, key))

    def put(self, kind, key, value):
        self.rows[kind, key] = value


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


class MediaLifetimeTests(unittest.TestCase):
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

    def test_six_mib_limit_rejects_the_next_byte(self):
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
        for index in range(24):
            service._wa_media_chunk(transfer, *identity, index * 256 * 1024, chunk)
        with self.assertRaises(ServiceError):
            service._wa_media_chunk(transfer, *identity, 6 * 1024 * 1024, 'eA==')
        self.assertNotIn(transfer, service._wa_transfers)

    def test_wrong_identity_cannot_remove_another_transfer(self):
        service = Service()
        service._wa_transfers['id'] = dict(identity=('a', 'b', 'c', 'd', 'e'), deadline=10**20)
        with self.assertRaises(ServiceError):
            service._wa_media_transfer('id', ('other', 'b', 'c', 'd', 'e'))
        self.assertIn('id', service._wa_transfers)
