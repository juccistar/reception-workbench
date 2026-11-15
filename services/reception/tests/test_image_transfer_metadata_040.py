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
from reception_service.media_resources import ResourceBudget
from reception_service.repository import Repository
from reception_service import image_transfer as transfer


class Host(CachedMediaMixin):
    account_verified = True
    seller_id = 'seller'
    _closing = False

    def __init__(self):
        self._lock = threading.RLock()
        self.repo = Repository(None, 'seller')
        image = Image.frombytes('RGB', (400, 400), os.urandom(480000))
        output = io.BytesIO()
        image.save(output, format='PNG')
        data = output.getvalue()
        sha = hashlib.sha256(data).hexdigest()
        self.ref = {'kind': 'supplement', 'conversation_id': 'c', 'attachment_id': 'a', 'sha256': sha}
        self.asset = {'id': 'a', 'seller_id': 'seller', 'buyer_id': 'b', 'conversation_id': 'c',
                      'status': 'ready', 'origin': 'business_supplement', 'sha256': sha,
                      'size': len(data), 'media_type': 'image/png', 'original_b64': base64.b64encode(data).decode()}
        self.repo.put('conversation', 'c', {'seller_id': 'seller', 'buyer_id': 'b'})
        self.repo.put('supplement_attachment', 'a', self.asset)

    def close(self):
        self._image_transfer_close()
        self.repo.close()


def ready(host, token):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = host._image_status(token)
        if status['status'] != 'pending':
            return status
        time.sleep(.005)
    raise AssertionError('timed out')


class MetadataTransferTests(unittest.TestCase):
    def setUp(self):
        self.host = Host()

    def tearDown(self):
        self.host.close()

    def test_chunks_do_not_reload_asset_and_updates_invalidate(self):
        host = self.host
        get = host.repo.get
        queries = []
        host.repo.db.set_trace_callback(queries.append)
        with mock.patch.object(host.repo, 'get', wraps=get) as reads:
            token = host._image_prepare(host.ref, False)['token']
            info = ready(host, token)
            for _ in range(2):
                for offset in range(0, info['size'], transfer.CHUNK):
                    host._image_transfer_chunk(token, offset, info['sha256'])
            asset_reads = [c for c in reads.call_args_list if c.args[:2] == ('supplement_attachment', 'a')]
            self.assertEqual(len(asset_reads), 1)
            self.assertEqual(sum("json_extract(data,'$.original_b64')" in q for q in queries), 1)
        host.repo.db.set_trace_callback(None)
        changed = dict(host.asset, original_b64='A' + host.asset['original_b64'][1:])
        host.repo.put('supplement_attachment', 'a', changed)
        with self.assertRaises(ServiceError):
            host._image_status(token)

    def test_rolled_back_change_keeps_token_valid(self):
        host = self.host
        token = host._image_prepare(host.ref, False)['token']
        info = ready(host, token)
        try:
            with host.repo.transaction():
                host.repo.put('supplement_attachment', 'a', dict(host.asset, original_b64='changed'))
                raise RuntimeError('rollback')
        except RuntimeError:
            pass
        self.assertEqual(host._image_status(token)['status'], 'ready')
        host._image_transfer_chunk(token, 0, info['sha256'])

    def test_budget_wait_cancel_and_release_after_worker_exit(self):
        host = self.host
        budget = ResourceBudget()
        entered, release = threading.Event(), threading.Event()
        original = transfer._convert
        def block(*args):
            entered.set()
            release.wait(5)
            return original(*args)
        with mock.patch.object(transfer, 'PROCESS_MEDIA_BUDGET', budget), mock.patch.object(transfer, '_convert', block):
            token = host._image_prepare(host.ref, True)['token']
            self.assertTrue(entered.wait(2))
            self.assertEqual(budget.snapshot()['pixel_workers'], 1)
            host._image_cancel(token)
            self.assertGreater(budget.snapshot()['reserved_bytes'], 0)
            with self.assertRaises(Exception):
                budget.acquire(1, pixels=True)
            release.set()
            deadline = time.monotonic() + 5
            while budget.snapshot()['reserved_bytes'] and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertEqual(budget.snapshot()['reserved_bytes'], 0)

    def test_waiting_budget_expires(self):
        budget = ResourceBudget()
        occupied = budget.acquire(1, pixels=True)
        try:
            with mock.patch.object(transfer, 'PROCESS_MEDIA_BUDGET', budget), mock.patch.object(transfer, 'TTL', .08):
                token = self.host._image_prepare(self.host.ref, False)['token']
                time.sleep(.15)
                with self.assertRaises(ServiceError):
                    self.host._image_status(token)
                deadline = time.monotonic() + 2
                while len(self.host._image_transfers.entries) and time.monotonic() < deadline:
                    time.sleep(.005)
                self.assertFalse(self.host._image_transfers.entries)
                self.assertEqual(budget.snapshot()['reserved_bytes'], 1)
        finally:
            occupied.close()


class RepositoryMetadataStartupTests(unittest.TestCase):
    def test_new_wal_repository_can_close_before_first_data_query(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            repo = Repository(Path(directory) / 'workspace.sqlite3', 'seller')
            repo.close()


if __name__ == '__main__':
    unittest.main()
