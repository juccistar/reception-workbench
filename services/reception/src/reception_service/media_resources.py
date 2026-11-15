"""Process-wide reservations for model encoding and heavy image work.

This bounds admitted application workloads, not Python or process RSS. Owners
must retain their lease until work actually finishes, including cancellation.
"""
from __future__ import annotations

import asyncio
import threading
import time


class ResourceLimitError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class ResourceLease:
    def __init__(self, budget, amount, pixels):
        self._budget = budget
        self.amount = amount
        self.pixels = pixels
        self._closed = False

    def resize(self, amount, *, pixels=None):
        budget = self._budget
        with budget._condition:
            budget._validate(amount)
            if self._closed:
                raise ResourceLimitError('resource_closed')
            next_pixels = self.pixels if pixels is None else bool(pixels)
            if (budget._used - self.amount + amount > budget.limit
                    or budget._pixels - int(self.pixels) + int(next_pixels) > 1):
                raise ResourceLimitError('resource_busy')
            budget._used += amount - self.amount
            budget._peak = max(budget._peak, budget._used)
            budget._pixels += int(next_pixels) - int(self.pixels)
            self.amount, self.pixels = amount, next_pixels
            budget._condition.notify_all()

    def close(self):
        budget = self._budget
        with budget._condition:
            if self._closed:
                return
            self._closed = True
            budget._used -= self.amount
            if self.pixels:
                budget._pixels -= 1
            budget._condition.notify_all()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class ResourceBudget:
    def __init__(self, limit=512 * 1024 * 1024):
        if type(limit) is not int or limit <= 0:
            raise ValueError('invalid resource limit')
        self.limit = limit
        self._condition = threading.Condition()
        self._used = 0
        self._pixels = 0
        self._peak = 0

    def _validate(self, amount):
        if type(amount) is not int or amount < 0 or amount > self.limit:
            raise ResourceLimitError('resource_too_large')

    def acquire(self, amount, *, pixels=False, cancel=None, deadline=None, wait=False):
        self._validate(amount)
        if wait and deadline is None:
            raise ValueError('waiting requires an absolute deadline')
        with self._condition:
            while True:
                if cancel is not None and cancel.is_set():
                    raise ResourceLimitError('resource_cancelled')
                if deadline is not None and time.monotonic() >= deadline:
                    raise ResourceLimitError('resource_timeout')
                if self._used + amount <= self.limit and (not pixels or not self._pixels):
                    self._used += amount
                    self._pixels += int(bool(pixels))
                    self._peak = max(self._peak, self._used)
                    return ResourceLease(self, amount, bool(pixels))
                if not wait:
                    raise ResourceLimitError('resource_busy')
                remaining = None if deadline is None else max(0, deadline - time.monotonic())
                self._condition.wait(0.05 if remaining is None else min(0.05, remaining))

    async def acquire_async(self, amount, *, deadline, pixels=False, cancel=None):
        # Never block the event loop on a condition or a pixel worker.
        if deadline is None:
            raise ValueError('waiting requires an absolute deadline')
        while True:
            try:
                return self.acquire(amount, pixels=pixels, cancel=cancel, deadline=deadline)
            except ResourceLimitError as error:
                if error.code != 'resource_busy':
                    raise
            await asyncio.sleep(0.025)

    def snapshot(self):
        with self._condition:
            return {'reserved_bytes': self._used, 'pixel_workers': self._pixels,
                    'peak_reserved_bytes': self._peak, 'limit_bytes': self.limit}


PROCESS_MEDIA_BUDGET = ResourceBudget()
