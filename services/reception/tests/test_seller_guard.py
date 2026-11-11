import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

from reception_service.errors import ServiceError
from reception_service.seller_guard import SellerGuard, SellerBusyError
from reception_service.service import ReceptionService


class SellerGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.seller = str(int(uuid.uuid4().hex[:14], 16) + 1)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cross_root_same_seller_and_distinct_seller(self):
        first = SellerGuard(self.seller, self.root / 'a').acquire()
        try:
            with self.assertRaises(SellerBusyError):
                SellerGuard(self.seller, self.root / 'b').acquire()
            other = SellerGuard(str(int(self.seller) + 1), self.root / 'b').acquire()
            other.release()
        finally:
            first.release()
        SellerGuard(self.seller, self.root / 'b').acquire().release()

    def test_busy_service_does_not_open_seller_database(self):
        guard = SellerGuard(self.seller, self.root / 'first' / 'live').acquire()
        service = ReceptionService(self.root / 'second', 'live', seller_id=self.seller)
        try:
            with self.assertRaises(ServiceError) as result:
                service._activate_seller(self.seller)
            self.assertEqual(result.exception.code, 'seller_busy')
            self.assertFalse((self.root / 'second' / 'live' / 'sellers').exists())
            self.assertFalse(service.account_verified)
            self.assertFalse(service._running)
        finally:
            service.close()
            guard.release()

    def test_close_keeps_guard_until_tracked_child_exits(self):
        from reception_service.seller_guard import native_inheritance
        from reception_service.workspace_lock import track_native_child, release_native_child
        service = ReceptionService(self.root / 'a', 'live', seller_id=self.seller)
        service._activate_seller(self.seller)
        with native_inheritance(self.root / 'a' / 'live') as inheritance:
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], **inheritance)
        track_native_child(child)
        errors = []
        def close():
            try:
                service.close()
            except Exception as error:
                errors.append(error)
        closer = threading.Thread(target=close, daemon=True)
        try:
            closer.start()
            deadline = time.monotonic() + 5
            while not service._closing and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(service._closing)
            self.assertTrue(closer.is_alive())
            self.assertIsNone(child.poll())
            with self.assertRaises(SellerBusyError):
                SellerGuard(self.seller, self.root / 'b').acquire()
            self.assertFalse((self.root / 'a' / '.reception-clean-shutdown').exists())
            child.terminate()
            child.wait(timeout=5)
            release_native_child(child)
            closer.join(10)
            self.assertFalse(closer.is_alive())
            self.assertEqual(errors, [])
            SellerGuard(self.seller, self.root / 'b').acquire().release()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            release_native_child(child)
            closer.join(10)
            service.close()

    def test_native_runner_child_retains_guard_after_parent_death(self):
        root = self.root
        child = root / 'child.py'
        child.write_text("import pathlib,sys,time\np=pathlib.Path(sys.argv[1]); p.write_text('ready')\nwhile not pathlib.Path(sys.argv[2]).exists(): time.sleep(.05)\nprint('{}')\n", encoding='utf8')
        parent = root / 'parent.py'
        parent.write_text("import sys\nfrom reception_service.seller_guard import SellerGuard\nfrom reception_service.integrations.accio_native.vendor.adapters.common import Process\ng=SellerGuard(sys.argv[1],sys.argv[2]).acquire()\nProcess(sys.argv[2]).run([sys.executable,sys.argv[3],sys.argv[4],sys.argv[5]],timeout=45)\n", encoding='utf8')
        data = root / 'live'
        ready, stop = root / 'ready', root / 'stop'
        env = dict(os.environ)
        env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
        proc = subprocess.Popen([sys.executable, str(parent), self.seller, str(data), str(child), str(ready), str(stop)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 15
            while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(ready.exists(), 'native child failed to start')
            proc.kill()
            proc.wait(timeout=5)
            with self.assertRaises(SellerBusyError):
                SellerGuard(self.seller, root / 'other').acquire()
            stop.write_text('stop')
            deadline = time.monotonic() + 15
            while True:
                try:
                    acquired = SellerGuard(self.seller, root / 'other').acquire()
                    break
                except SellerBusyError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.05)
            acquired.release()
        finally:
            stop.write_text('stop')
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
