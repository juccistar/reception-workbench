import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from reception_service.integrations.accio_native.vendor.adapters.common import AdapterError
from reception_service.integrations.accio_native.vendor.adapters.desktop_query import DesktopQueryRunner
from reception_service.workspace_lock import native_children_stopped, track_native_child

MODULE = 'reception_service.integrations.accio_native.vendor.adapters.desktop_query'


class OwnedProcessTests(unittest.TestCase):
    def invoke(self, args, root, *, cancel=False, recovery=False, event_present=True, event=None):
        controlled_event = event is not None
        event = event if controlled_event else (threading.Event() if event_present else None)
        timer = threading.Timer(.2, event.set) if cancel and not controlled_event else None
        if timer:
            timer.start()
        runner = DesktopQueryRunner(Path(root))
        class Records:
            folder = True
            status = 'not_found'
            def __init__(self, *args):
                pass
            def find(self):
                return None
        try:
            with patch(MODULE + '._prepare', return_value=(args, 'name', 'agent')), \
                 patch(MODULE + '._OwnRecords', Records):
                with self.assertRaises(AdapterError) as caught:
                    runner.run(['x', 'desktop', 'query'], input='x' * 100000, timeout=.6,
                               recover_own_results=recovery, cancel_event=event)
            self.assertEqual(caught.exception.code, 'cancelled' if cancel else 'timeout')
        finally:
            if timer:
                timer.cancel()
                timer.join()

    def test_large_stdin_does_not_block_cancel_or_timeout(self):
        with tempfile.TemporaryDirectory() as root:
            for recovery in (False, True):
                for cancel, present in ((True, True), (False, True), (False, False)):
                    with self.subTest(recovery=recovery, cancel=cancel, present=present):
                        start = time.monotonic()
                        self.invoke([sys.executable, '-c', 'import time; time.sleep(3)'], root,
                                    cancel=cancel, recovery=recovery, event_present=present)
                        self.assertLess(time.monotonic() - start, 2)
                        self.assertTrue(native_children_stopped())

    def test_exited_parent_descendant_remains_tracked_and_is_killed(self):
        with tempfile.TemporaryDirectory() as root:
            for recovery in (False, True):
                for cancel, present in ((True, True), (False, True), (False, False)):
                    with self.subTest(recovery=recovery, cancel=cancel, present=present):
                        marker = Path(root) / 'marker'
                        ready = Path(root) / 'ready'
                        gate = Path(root) / 'gate'
                        for path in (marker, ready, gate):
                            path.unlink(missing_ok=True)
                        child = (
                            'import time,pathlib\n'
                            'ready=pathlib.Path(%r); gate=pathlib.Path(%r)\n'
                            'ready.write_text("ready")\n'
                            'while not gate.exists(): time.sleep(.01)\n'
                            'time.sleep(1.4)\n'
                            'pathlib.Path(%r).write_text("alive")\n'
                        ) % (str(ready), str(gate), str(marker))
                        parent = 'import subprocess,sys; subprocess.Popen([sys.executable,"-c",%r])' % child
                        args = [sys.executable, '-c', parent]
                        errors = []
                        registered = threading.Event()
                        proceed = threading.Event()
                        event = threading.Event() if present else None
                        owned = []
                        def register(process):
                            track_native_child(process)
                            owned.append(process)
                            registered.set()
                            proceed.wait(10)
                        def run():
                            try:
                                self.invoke(args, root, cancel=cancel, recovery=recovery,
                                            event_present=present, event=event)
                            except BaseException as exc:
                                errors.append(exc)
                        thread = threading.Thread(target=run)
                        with patch('reception_service.workspace_lock.track_native_child', side_effect=register):
                            thread.start()
                            try:
                                self.assertTrue(registered.wait(5), 'process was not registered')
                                deadline = time.monotonic() + 5
                                while time.monotonic() < deadline and (not ready.exists() or owned[0].proc.poll() is None):
                                    time.sleep(.01)
                                self.assertTrue(ready.exists(), 'real descendant did not start')
                                self.assertIsNotNone(owned[0].proc.poll(), 'parent did not exit')
                                self.assertFalse(native_children_stopped(), 'live descendant was not tracked')
                                gate.write_text('go')
                                if cancel:
                                    event.set()
                                proceed.set()
                                thread.join(5)
                                self.assertFalse(thread.is_alive(), 'runner did not finish')
                                if errors:
                                    raise errors[0]
                                self.assertTrue(native_children_stopped())
                                time.sleep(1.6)
                                self.assertFalse(marker.exists(), 'descendant wrote after cleanup')
                            finally:
                                if event is not None:
                                    event.set()
                                proceed.set()
                                thread.join(10)
                                if thread.is_alive():
                                    self.fail('runner thread did not stop during fixture cleanup')


if __name__ == '__main__':
    unittest.main()
