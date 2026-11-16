import base64
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from reception_service.errors import ServiceError
from reception_service.model_inputs import resolve_whatsapp_images
from reception_service.whatsapp_account_service import AccountRepository, _key
from reception_service.whatsapp_account_media import AccountMediaMixin


class Host(AccountMediaMixin):
    def __init__(self, root):
        self.repo = AccountRepository(Path(root), 'test')
        self.account, self.epoch, self.connected = '账号', 'epoch', True
        self.scheduled = 0
        self._init_account_media()

    def _identity(self, account, epoch):
        if not self.connected or (account, epoch) != (self.account, self.epoch):
            raise ServiceError('source_changed', 'changed')

    def _conversation(self, chat):
        return self.repo.get('conversation', _key(self.account, chat))

    def _changed(self):
        pass

    def _schedule_account_replies(self):
        self.scheduled += 1

    def add(self, message='m', history=False, seller=False):
        chat = '聊天'
        self.repo.put('binding', _key(self.account, chat), {'segment_id': 's'})
        self.repo.put('conversation', _key(self.account, chat), {'pending': not history, 'pending_message_id': message})
        self.repo.put('message', _key(self.account, message), {'id': message, 'account_id': self.account, 'chat_id': chat, 'segment_id': 's', 'from_me': seller, 'has_image': True, 'timestamp_ms': 123, 'is_history': history})
        return (self.account, self.epoch, chat, message, _key(self.account, self.epoch, chat, message, '123'))


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.host = Host(self.tmp.name)
        out = io.BytesIO()
        Image.new('RGB', (2, 3), 'red').save(out, 'PNG')
        self.data = out.getvalue()

    def tearDown(self):
        self.host._cancel_account_media()
        self.host.repo.close()
        self.tmp.cleanup()

    def finish(self, identity, mime='image/png'):
        transfer = self.host._wa_media_begin(*identity, mime)['transfer_id']
        self.host._wa_media_chunk(transfer, *identity, 0, base64.b64encode(self.data).decode())
        return self.host._wa_media_finish(transfer, *identity, len(self.data), hashlib.sha256(self.data).hexdigest())

    def test_resolver_and_model_evidence(self):
        identity = self.host.add()
        self.finish(identity)
        refs, rows = self.host._account_image_context('聊天', 's')
        messages = [dict(rows[0], role='buyer')]
        from reception_service.model_media import OwnedInput
        owner = OwnedInput(1024)
        self.addCleanup(owner.close)
        images, manifest = resolve_whatsapp_images({'model_image_refs': refs, 'whatsapp_messages': messages}, lambda ref: self.host._account_resolve_image(ref, *identity[:3]), owner=owner)
        self.assertEqual(images[0]['data'], self.data)
        self.assertEqual((images[0]['width'], images[0]['height']), (2, 3))
        self.assertEqual(manifest[0]['source_id'], rows[0]['source_id'])
        self.assertEqual(self.host.scheduled, 1)
        for args in [('other', *identity[1:3]), (identity[0], 'other', identity[2]), (*identity[:2], 'other')]:
            with self.assertRaises(ServiceError):
                self.host._account_resolve_image(refs[0], *args)
        self.host.repo.put('binding', _key(identity[0], identity[2]), {'segment_id': 'new'})
        with self.assertRaises(ServiceError):
            self.host._account_resolve_image(refs[0], *identity[:3])

    def test_transfer_lease_survives_chunks_and_releases_on_abort(self):
        from reception_service.media_resources import PROCESS_MEDIA_BUDGET
        baseline = PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes']
        identity = self.host.add()
        tid = self.host._wa_media_begin(*identity, 'image/png')['transfer_id']
        reserved = PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes']
        self.assertGreater(reserved, baseline + len(self.data))
        self.host._wa_media_chunk(tid, *identity, 0, base64.b64encode(self.data).decode())
        self.assertEqual(PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes'], reserved)
        self.host._wa_media_fail(tid, *identity, 'aborted')
        self.assertEqual(PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes'], baseline)

    def test_buffers_grow_by_actual_chunks_and_failure_releases_lease(self):
        from reception_service.media_resources import PROCESS_MEDIA_BUDGET
        baseline = PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes']
        identity = self.host.add()
        tid = self.host._wa_media_begin(*identity, 'image/png')['transfer_id']
        transfer = self.host._account_media_transfers[tid]
        self.assertLess(transfer['lease'].amount, 2 * 1024 * 1024)
        block = base64.b64encode(b'x' * (256 * 1024)).decode()
        self.host._wa_media_chunk(tid, *identity, 0, block)
        used = PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes']
        with PROCESS_MEDIA_BUDGET.acquire(PROCESS_MEDIA_BUDGET.limit - used - 256 * 1024):
            with self.assertRaises(ServiceError) as error:
                self.host._wa_media_chunk(tid, *identity, 256 * 1024, block)
            self.assertEqual(error.exception.code, 'transfer_unavailable')
            self.assertFalse(transfer['data'])
            self.assertNotIn(tid, self.host._account_media_transfers)
            self.assertEqual(self.host.repo.db.execute('SELECT status FROM account_media WHERE id=?', (_key(*identity),)).fetchone()[0], 'failed')
        self.assertEqual(PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes'], baseline)

    def test_pixel_resource_failure_retires_pending_and_allows_new_transfer(self):
        from reception_service.media_resources import PROCESS_MEDIA_BUDGET
        baseline = PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes']
        identity = self.host.add()
        tid = self.host._wa_media_begin(*identity, 'image/png')['transfer_id']
        self.host._wa_media_chunk(tid, *identity, 0, base64.b64encode(self.data).decode())
        with patch('reception_service.whatsapp_account_media.validate_original', side_effect=ServiceError('model_resource_busy', 'busy')):
            with self.assertRaises(ServiceError) as error:
                self.host._wa_media_finish(tid, *identity, len(self.data), hashlib.sha256(self.data).hexdigest())
        self.assertEqual(error.exception.code, 'model_resource_busy')
        self.assertEqual(self.host._account_image_context('聊天', 's')[1][0]['media']['status'], 'failed')
        self.assertFalse(self.host._account_media_transfers)
        self.assertEqual(PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes'], baseline)
        self.assertEqual(self.finish(identity)['status'], 'ready')
        self.host._account_media_expire(tid, identity)
        self.assertEqual(self.host._account_image_context('聊天', 's')[1][0]['media']['status'], 'ready')

    def test_static_gif_is_preserved_but_animation_is_failed(self):
        out = io.BytesIO()
        with Image.new('RGB', (2, 3), 'red') as image:
            image.save(out, 'GIF')
        self.data = out.getvalue()
        identity = self.host.add()
        self.assertEqual(self.finish(identity, 'image/gif')['status'], 'ready')
        ref = self.host._account_image_context('聊天', 's')[0][0]
        resolved = self.host._account_resolve_image(ref, *identity[:3])
        self.assertEqual(resolved['mime'], 'image/gif')
        self.assertEqual(base64.b64decode(resolved['original_b64']), self.data)
        out = io.BytesIO()
        with Image.new('RGB', (2, 3), 'red') as first, Image.new('RGB', (2, 3), 'blue') as second:
            first.save(out, 'GIF', save_all=True, append_images=[second], duration=100)
        self.data = out.getvalue()
        animated = self.host.add('animated')
        with self.assertRaises(ServiceError) as error:
            self.finish(animated, 'image/gif')
        self.assertEqual(error.exception.code, 'invalid_media')
        self.assertEqual(self.host.repo.db.execute('SELECT status FROM account_media WHERE id=?', (_key(*animated),)).fetchone()[0], 'failed')
        self.assertFalse(self.host._account_media_transfers)

    def test_resolver_encodes_original_once(self):
        identity = self.host.add()
        self.finish(identity)
        ref = self.host._account_image_context('聊天', 's')[0][0]
        original = base64.b64encode
        with patch('reception_service.whatsapp_account_media.base64.b64encode', wraps=original) as encode:
            result = self.host._account_resolve_image(ref, *identity[:3])
        self.assertEqual(encode.call_count, 1)
        self.assertEqual(base64.b64decode(result['original_b64']), self.data)

    def test_history_and_seller_do_not_schedule_or_expose_seller(self):
        history = self.host.add('history', history=True)
        self.finish(history)
        seller = self.host.add('seller', seller=True)
        self.finish(seller)
        refs, rows = self.host._account_image_context('聊天', 's')
        self.assertEqual(len(refs), 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.host.scheduled, 0)

    def test_source_version_and_transfer_checks(self):
        identity = self.host.add()
        for bad in [('wrong', *identity[1:]), (identity[0], 'wrong', *identity[2:]), (*identity[:2], 'wrong', *identity[3:]), (*identity[:3], 'wrong', identity[4]), (*identity[:4], 'wrong')]:
            with self.assertRaises(ServiceError):
                self.host._wa_media_begin(*bad, 'image/png')
        tid = self.host._wa_media_begin(*identity, 'image/png')['transfer_id']
        with self.assertRaises(ServiceError):
            self.host._wa_media_begin(*identity, 'image/png')
        with self.assertRaises(ServiceError):
            self.host._wa_media_chunk(tid, *identity, 1, 'YQ==')
        tid = self.host._wa_media_begin(*identity, 'image/png')['transfer_id']
        self.host._wa_media_chunk(tid, *identity, 0, base64.b64encode(self.data).decode())
        with self.assertRaises(ServiceError):
            self.host._wa_media_finish(tid, *identity, len(self.data), '0' * 64)
        self.assertFalse(self.host._account_media_transfers)

    def test_chunk_limit_expiry_and_original_pending_source(self):
        identity = self.host.add()
        tid = self.host._wa_media_begin(*identity, 'image/png')['transfer_id']
        with self.assertRaises(ServiceError):
            self.host._wa_media_chunk(tid, *identity, 0, base64.b64encode(b'x' * (256 * 1024 + 1)).decode())
        tid = self.host._wa_media_begin(*identity, 'image/png')['transfer_id']
        self.host._account_media_expire(tid, identity)
        self.assertEqual(self.host._account_image_context('聊天', 's')[1][0]['media']['status'], 'failed')
        self.assertEqual(self.host.scheduled, 0)
        self.host._wa_media_fail('', *identity, 'unavailable')
        self.assertEqual(self.host.scheduled, 0)

    def test_invalid_and_expired_transfers_record_failure_before_retirement(self):
        identity = self.host.add()
        for mode in ('chunk', 'finish', 'expired', 'sweep'):
            tid = self.host._wa_media_begin(*identity, 'image/png')['transfer_id']
            if mode in ('expired', 'sweep'):
                self.host._account_media_transfers[tid]['deadline'] = 0
            if mode == 'sweep':
                other = self.host.add('other')
                other_tid = self.host._wa_media_begin(*other, 'image/png')['transfer_id']
                self.host._wa_media_fail(other_tid, *other, 'unavailable')
            else:
                with self.assertRaises(ServiceError):
                    if mode == 'finish':
                        self.host._wa_media_finish(tid, *identity, 1, 'bad')
                    else:
                        self.host._wa_media_chunk(tid, *identity, 1, 'YQ==')
            status = self.host.repo.db.execute('SELECT status FROM account_media WHERE id=?', (_key(*identity),)).fetchone()[0]
            self.assertEqual(status, 'failed', mode)
        new_tid = self.host._wa_media_begin(*identity, 'image/png')['transfer_id']
        self.host._account_media_expire(tid, identity)
        with self.assertRaises(ServiceError):
            self.host._wa_media_fail(tid, *identity, 'late')
        self.assertEqual(self.host.repo.db.execute('SELECT status FROM account_media WHERE id=?', (_key(*identity),)).fetchone()[0], 'pending')
        self.host._wa_media_chunk(new_tid, *identity, 0, base64.b64encode(self.data).decode())
        self.host._wa_media_finish(new_tid, *identity, len(self.data), hashlib.sha256(self.data).hexdigest())
        with self.assertRaises(ServiceError):
            self.host._wa_media_fail(tid, *identity, 'late')
        self.assertEqual(self.host.repo.db.execute('SELECT status FROM account_media WHERE id=?', (_key(*identity),)).fetchone()[0], 'ready')

    def test_cancel_fail_and_bounds(self):
        identities = [self.host.add(str(i)) for i in range(5)]
        tids = [self.host._wa_media_begin(*identity, 'image/png')['transfer_id'] for identity in identities[:4]]
        with self.assertRaises(ServiceError):
            self.host._wa_media_begin(*identities[4], 'image/png')
        self.host._wa_media_fail(tids[0], *identities[0], 'cancelled')
        with self.assertRaises(ServiceError):
            self.host._wa_media_chunk(tids[0], *identities[0], 0, 'YQ==')
        self.host.epoch = 'new'
        with self.assertRaises(ServiceError):
            self.host._wa_media_chunk(tids[1], *identities[1], 0, 'YQ==')
        self.host._cancel_account_media()
        self.assertFalse(self.host._account_media_transfers)


if __name__ == '__main__':
    unittest.main()
