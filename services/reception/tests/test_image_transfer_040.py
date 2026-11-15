import base64
import hashlib
import io
import os
import threading
import time
import unittest
from unittest import mock

from PIL import Image

from reception_service.cached_media import CachedMediaMixin
from reception_service.errors import ServiceError
from reception_service import image_transfer as transfer


class Service(CachedMediaMixin):
    def __init__(self, data):
        self._lock = threading.RLock()
        self.snapshot = (base64.b64encode(data).decode(), len(data), 'image/png', hashlib.sha256(data).hexdigest())

    def _cached_image_snapshot(self, ref, *, metadata=False):
        if ref != {'id': 'owned'}:
            raise ServiceError('image_unavailable', 'unavailable')
        return self.snapshot


def image_bytes():
    image = Image.frombytes('RGB', (400, 400), os.urandom(400 * 400 * 3))
    out = io.BytesIO()
    image.save(out, format='PNG')
    return out.getvalue()


def ready(service, token):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = service._image_status(token)
        if status['status'] != 'pending':
            return status
        time.sleep(.005)
    raise AssertionError('transfer did not finish')


class ImageTransferTests(unittest.TestCase):
    def setUp(self):
        self.data = image_bytes()
        self.service = Service(self.data)
        self.ref = {'id': 'owned'}

    def tearDown(self):
        self.service._image_transfer_close()

    def test_multichunk_and_single_decode_hash(self):
        with mock.patch.object(transfer.base64, 'b64decode', wraps=base64.b64decode) as decode, mock.patch.object(transfer.hashlib, 'sha256', wraps=hashlib.sha256) as sha:
            token = self.service._image_prepare(self.ref, False)['token']
            info = ready(self.service, token)
            self.assertEqual(info['status'], 'ready')
            chunks = [self.service._image_transfer_chunk(token, offset, info['sha256']) for offset in range(0, info['size'], transfer.CHUNK)]
            # Two full reads do not decode or hash the source again.
            for offset in range(0, info['size'], transfer.CHUNK):
                self.service._image_transfer_chunk(token, offset, info['sha256'])
            self.assertEqual(decode.call_count, 1)
            self.assertEqual(sha.call_count, 1)
        self.assertEqual(b''.join(base64.b64decode(c['data_b64']) for c in chunks), self.data)

    def test_copy_and_source_change(self):
        token = self.service._image_prepare(self.ref, True)['token']
        info = ready(self.service, token)
        self.assertEqual(info['media_type'], 'image/png')
        result = b''.join(base64.b64decode(self.service._image_transfer_chunk(token, offset, info['sha256'])['data_b64']) for offset in range(0, info['size'], transfer.CHUNK))
        self.assertEqual(hashlib.sha256(result).hexdigest(), info['sha256'])
        self.service.snapshot = (*self.service.snapshot[:1], self.service.snapshot[1] + 1, *self.service.snapshot[2:])
        with self.assertRaises(ServiceError):
            self.service._image_status(token)
        with self.assertRaises(ServiceError):
            self.service._image_transfer_chunk(token, 0, info['sha256'])

    def test_cancel_during_work_keeps_slot_and_late_result_is_discarded(self):
        entered, release = threading.Event(), threading.Event()
        original = transfer._convert
        def blocking(*args):
            entered.set()
            release.wait(5)
            return original(*args)
        with mock.patch.object(transfer, '_convert', blocking):
            first = self.service._image_prepare(self.ref, True)['token']
            self.assertTrue(entered.wait(2))
            second = self.service._image_prepare(self.ref, False)['token']
            self.service._image_cancel(first)
            with self.assertRaises(ServiceError):
                self.service._image_status(first)
            with self.assertRaises(ServiceError):
                self.service._image_prepare(self.ref, False)
            self.service._image_cancel(second)
            release.set()
            deadline = time.monotonic() + 5
            manager = self.service._image_transfers
            while time.monotonic() < deadline:
                with manager.lock:
                    if not manager.entries:
                        break
                time.sleep(.005)
            self.assertFalse(manager.entries)

    def test_expiry_and_oversize_failure(self):
        with mock.patch.object(transfer, 'TTL', .05):
            token = self.service._image_prepare(self.ref, False)['token']
            ready(self.service, token)
            time.sleep(.1)
            with self.assertRaises(ServiceError):
                self.service._image_status(token)
            self.assertFalse(self.service._image_transfers.entries)
        with mock.patch.object(transfer, 'PAYLOAD_LIMIT', 1):
            token = self.service._image_prepare(self.ref, False)['token']
            self.assertEqual(ready(self.service, token)['error'], 'image_too_large')
            self.service._image_cancel(token)
            token = self.service._image_prepare(self.ref, False)['token']
            self.assertEqual(ready(self.service, token)['error'], 'image_too_large')

    def test_real_supplement_ownership_rechecked(self):
        class Host(CachedMediaMixin):
            account_verified = True
            seller_id = 'seller'
            _closing = False
            _lock = threading.RLock()
        class Repo:
            def __init__(self, rows):
                self.rows = rows
            def get(self, kind, key):
                return self.rows.get((kind, key))
        content, size, mime, sha = self.service.snapshot
        ref = {'kind': 'supplement', 'conversation_id': 'c', 'attachment_id': 'a', 'sha256': sha}
        attachment = {'id': 'a', 'seller_id': 'seller', 'buyer_id': 'buyer', 'conversation_id': 'c',
                      'status': 'ready', 'origin': 'business_supplement', 'sha256': sha,
                      'size': size, 'media_type': mime, 'original_b64': content}
        host = Host()
        host.repo = Repo({('conversation', 'c'): {'seller_id': 'seller', 'buyer_id': 'buyer'},
                          ('supplement_attachment', 'a'): attachment})
        try:
            token = host._image_prepare(ref, False)['token']
            info = ready(host, token)
            attachment['seller_id'] = 'other'
            with self.assertRaises(ServiceError):
                host._image_transfer_chunk(token, 0, info['sha256'])
        finally:
            host._image_transfer_close()

    def test_budget_fails_explicitly(self):
        from reception_service.media_resources import ResourceBudget
        with mock.patch.object(transfer, 'PROCESS_MEDIA_BUDGET', ResourceBudget(1)):
            token = self.service._image_prepare(self.ref, True)['token']
            self.assertEqual(ready(self.service, token)['error'], 'image_too_large')
            self.service._image_cancel(token)
            token = self.service._image_prepare(self.ref, True)['token']
            self.assertEqual(ready(self.service, token)['error'], 'image_too_large')


if __name__ == '__main__':
    unittest.main()
