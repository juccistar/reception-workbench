"""A subprocess and only the descendants created by that invocation."""
import os
import signal
import subprocess
import threading
import time


class OwnedProcess:
    def __init__(self, args, inheritance, input_text):
        self.job = None
        self.proc = None
        self._buffers = [bytearray(), bytearray()]
        self._errors = []
        self._lock = threading.RLock()
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
            self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            self.kernel.AssignProcessToJobObject.restype = wintypes.BOOL
            self.kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            self.kernel.TerminateJobObject.restype = wintypes.BOOL
            self.kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
            self.kernel.QueryInformationJobObject.restype = wintypes.BOOL
            self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self.job = self.kernel.CreateJobObjectW(None, None)
            if not self.job:
                raise ctypes.WinError(ctypes.get_last_error())
        try:
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) | (0x4 if os.name == 'nt' else 0)
            self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, creationflags=flags,
                **({'start_new_session': True} if os.name != 'nt' else {}), **inheritance)
            if self.job:
                if not self.kernel.AssignProcessToJobObject(self.job, int(self.proc._handle)):
                    raise ctypes.WinError(ctypes.get_last_error())
                ntdll = ctypes.WinDLL('ntdll')
                ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
                ntdll.NtResumeProcess.restype = ctypes.c_long
                if ntdll.NtResumeProcess(int(self.proc._handle)) != 0:
                    raise OSError('Could not resume owned process')
        except BaseException:
            if self.proc is not None:
                self.proc.kill()
                self.proc.wait()
            if self.job:
                self.kernel.CloseHandle(self.job)
                self.job = None
            raise
        self.threads = [threading.Thread(target=self._read, args=(self.proc.stdout, 0), daemon=True),
                        threading.Thread(target=self._read, args=(self.proc.stderr, 1), daemon=True),
                        threading.Thread(target=self._write, args=(input_text,), daemon=True)]
        for thread in self.threads:
            thread.start()

    def _read(self, stream, index):
        try:
            while True:
                chunk = stream.read1(65536)
                if not chunk:
                    break
                self._buffers[index].extend(chunk)
        except OSError as exc:
            self._errors.append(exc)
        finally:
            stream.close()

    def _write(self, text):
        try:
            self.proc.stdin.write(text.encode('utf-8'))
        except (OSError, ValueError):
            pass
        except UnicodeError as exc:
            self._errors.append(exc)
        finally:
            try:
                self.proc.stdin.close()
            except (OSError, ValueError):
                pass

    def output(self):
        return tuple(bytes(buf).decode('utf-8', errors='replace') for buf in self._buffers)

    def strict_output(self):
        if self._errors:
            raise self._errors[0]
        return tuple(bytes(buf).decode('utf-8', errors='strict') for buf in self._buffers)

    def finished(self):
        return self.proc.poll() is not None and all(not thread.is_alive() for thread in self.threads)

    def poll(self):
        with self._lock:
            return self._poll()

    def _poll(self):
        # The tracker calls poll independently, including after the root exits.
        if self.proc.poll() is None:
            return None
        if self.job:
            import ctypes
            from ctypes import wintypes
            class Accounting(ctypes.Structure):
                _fields_ = [('user', ctypes.c_longlong), ('kernel', ctypes.c_longlong),
                            ('period_user', ctypes.c_longlong), ('period_kernel', ctypes.c_longlong),
                            ('faults', wintypes.DWORD), ('total', wintypes.DWORD),
                            ('active', wintypes.DWORD), ('terminated', wintypes.DWORD)]
            info = Accounting()
            if not self.kernel.QueryInformationJobObject(self.job, 1, ctypes.byref(info), ctypes.sizeof(info), None):
                return None
            if info.active:
                return None
            self.kernel.CloseHandle(self.job)
            self.job = None
        elif os.name != 'nt':
            try:
                os.killpg(self.proc.pid, 0)
                return None
            except ProcessLookupError:
                pass
            except OSError:
                return None
        return self.proc.returncode

    def stop(self):
        with self._lock:
            self._terminate()
        deadline = time.monotonic() + 1.0
        for thread in self.threads:
            thread.join(max(0, deadline - time.monotonic()))
        try:
            self.proc.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass

    def _terminate(self):
        if self.job:
            self.kernel.TerminateJobObject(self.job, 1)
        elif os.name != 'nt':
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif self.proc.poll() is None:
            self.proc.kill()
