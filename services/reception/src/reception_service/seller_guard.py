"""Cross-workspace seller exclusion, retained by native subprocess handles."""
import contextlib
import hashlib
import os
from pathlib import Path
import re
import subprocess
import threading

_registry = {}
_registry_lock = threading.RLock()


def _key(data_dir):
    return os.path.normcase(os.path.abspath(data_dir))


class SellerBusyError(RuntimeError):
    pass


class SellerGuard:
    def __init__(self, seller, data_dir):
        if not isinstance(seller, str) or not re.fullmatch(r'[1-9][0-9]*', seller):
            raise ValueError('Invalid verified seller')
        self.seller = seller
        self.name = 'ReceptionSellerSession-' + hashlib.sha256(seller.encode('ascii')).hexdigest()
        self.key = _key(data_dir)
        self.handle = None

    def acquire(self):
        with _registry_lock:
            if self.key in _registry:
                raise SellerBusyError('Seller session already active')
            if os.name == 'nt':
                import ctypes
                from ctypes import wintypes
                kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                kernel.CreateEventW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
                kernel.CreateEventW.restype = wintypes.HANDLE
                kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
                handle = kernel.CreateEventW(None, True, False, 'Global\\' + self.name)
                error = ctypes.get_last_error()
                if not handle:
                    raise ctypes.WinError(error)
                if error == 183:
                    kernel.CloseHandle(handle)
                    raise SellerBusyError('Seller session already active')
                self.handle = handle
            else:
                import fcntl
                path = Path('/tmp') / (self.name + '.lock')
                fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd)
                    raise SellerBusyError('Seller session already active') from None
                except BaseException:
                    os.close(fd)
                    raise
                self.handle = fd
            _registry[self.key] = self
        return self

    def release(self):
        with _registry_lock:
            if self.handle is None:
                return
            _registry.pop(self.key, None)
            if os.name == 'nt':
                import ctypes
                from ctypes import wintypes
                kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
                kernel.CloseHandle(self.handle)
            else:
                os.close(self.handle)
            self.handle = None


@contextlib.contextmanager
def native_inheritance(data_dir):
    """Give only the selected guard to a child, without broad handle inheritance."""
    duplicate = None
    with _registry_lock:
        guard = _registry.get(_key(data_dir))
        if guard is None:
            yield {}
            return
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            kernel.DuplicateHandle.argtypes = (wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                               ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                                               wintypes.BOOL, wintypes.DWORD)
            kernel.DuplicateHandle.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            current = kernel.GetCurrentProcess()
            duplicate = wintypes.HANDLE()
            if not kernel.DuplicateHandle(current, guard.handle, current, ctypes.byref(duplicate), 0, True, 2):
                raise ctypes.WinError(ctypes.get_last_error())
            startup = subprocess.STARTUPINFO()
            startup.lpAttributeList = {'handle_list': [duplicate.value]}
            kwargs = {'startupinfo': startup, 'close_fds': True}
        else:
            duplicate = os.dup(guard.handle)
            kwargs = {'pass_fds': (duplicate,)}
    try:
        yield kwargs
    finally:
        if os.name == 'nt':
            kernel.CloseHandle(duplicate)
        else:
            os.close(duplicate)
