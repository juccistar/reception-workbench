import asyncio
import base64
import ctypes
import hashlib
import io
import os
import threading
import time
import unittest
from ctypes import wintypes
from unittest import mock

from PIL import Image

from reception_service import image_transfer as transfer
from reception_service.cached_media import CachedMediaMixin
from reception_service.media_resources import PROCESS_MEDIA_BUDGET
from reception_service.model_providers.encoding import image_blocks, validate_request_budget
from reception_service.model_providers.transport import ProviderTransport


def windows_process_metrics():
    class Counters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                    *[(name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
                       'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                       'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage', 'PrivateUsage')]]
    class ThreadEntry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                    ('th32ThreadID', wintypes.DWORD), ('th32OwnerProcessID', wintypes.DWORD),
                    ('tpBasePri', wintypes.LONG), ('tpDeltaPri', wintypes.LONG), ('dwFlags', wintypes.DWORD)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
    kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    handle = kernel.CreateToolhelp32Snapshot(4, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        count = 0
        found = kernel.Thread32First(handle, ctypes.byref(entry))
        while found:
            count += entry.th32OwnerProcessID == os.getpid()
            found = kernel.Thread32Next(handle, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(handle)
    return counters.PrivateUsage, counters.WorkingSetSize, count


@unittest.skipUnless(os.name == 'nt', 'Windows process measurements')
class RealImageInterleaveTest(unittest.TestCase):
    def test_copy_20mp_and_model_encoding_share_reservation(self):
        image = Image.new('RGB', (5000, 4000), (32, 140, 200))
        out = io.BytesIO()
        image.save(out, 'JPEG', quality=85)
        image.close()
        data = out.getvalue()
        self.assertLess(len(data), 1024 * 1024)
        sha = hashlib.sha256(data).hexdigest()

        class Host(CachedMediaMixin):
            def __init__(self):
                self._lock = threading.RLock()
                self.snapshot = (base64.b64encode(data).decode(), len(data), 'image/jpeg', sha)
            def _cached_image_snapshot(self, ref, *, metadata=False):
                return self.snapshot

        host = Host()
        converted, release, encoded = threading.Event(), threading.Event(), threading.Event()
        measurements, stop = [], threading.Event()
        original = transfer._convert
        def convert(*args):
            result = original(*args)
            converted.set()
            if not release.wait(15):
                raise TimeoutError()
            return result
        def sample():
            while not stop.is_set():
                measurements.append(windows_process_metrics())
                stop.wait(.01)
        class Model(ProviderTransport):
            async def _generate_leased(self, **kwargs):
                image_blocks(kwargs['images'], 'chat-completions')
                encoded.set()
                return {}
        item = {'data': data, 'mime': 'image/jpeg', 'alias': 'p1', 'sha256': sha,
                'image_id': 'i', 'source_id': 's', 'source_version': '1', 'role': 'buyer',
                'width': 5000, 'height': 4000}
        errors = []
        def model_work():
            try:
                asyncio.run(Model().generate(profile={}, api_key='x', system_prompt='x',
                                             user_payload='x' * 1_000_000, images=[item]))
            except BaseException as exc:
                errors.append(exc)
        sampler = threading.Thread(target=sample)
        sampler.start()
        worker = None
        token = None
        filler = None
        try:
            with mock.patch.object(transfer, '_convert', convert):
                token = host._image_prepare({'id': 'one'}, True)['token']
                self.assertTrue(converted.wait(15))
                snapshot = PROCESS_MEDIA_BUDGET.snapshot()
                self.assertEqual(snapshot['pixel_workers'], 1)
                cost = validate_request_budget('x', 'x' * 1_000_000, [item])
                filler = PROCESS_MEDIA_BUDGET.acquire(snapshot['limit_bytes'] - snapshot['reserved_bytes'] - cost + 1)
                worker = threading.Thread(target=model_work)
                worker.start()
                self.assertFalse(encoded.wait(.15))
                release.set()
                filler.close()
                filler = None
                worker.join(15)
                self.assertFalse(worker.is_alive())
                self.assertFalse(errors, errors)
                self.assertTrue(encoded.is_set())
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    status = host._image_status(token)
                    if status['status'] != 'pending':
                        break
                    time.sleep(.01)
                self.assertEqual(status['status'], 'ready')
                self.assertEqual((status['width'], status['height']), (5000, 4000))
                self.assertEqual(status['media_type'], 'image/png')
                host._image_cancel(token)
                deadline = time.monotonic() + 5
                while PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes'] and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(PROCESS_MEDIA_BUDGET.snapshot()['reserved_bytes'], 0)
        finally:
            release.set()
            if filler is not None:
                filler.close()
            if worker:
                worker.join(15)
            host._image_transfer_close()
            stop.set()
            sampler.join(5)
        self.assertTrue(measurements)
        print('\n20MP scenario measurements (sampled process PrivateBytes, working set, OS threads): '
              f'baseline={measurements[0]}, peak={tuple(max(row[i] for row in measurements) for i in range(3))}', flush=True)
