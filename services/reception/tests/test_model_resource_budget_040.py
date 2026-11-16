import asyncio
import threading
import time
import unittest
from unittest.mock import patch

from reception_service.image_assets import decode_image
from reception_service.media_resources import ResourceBudget
from reception_service.model_providers.encoding import validate_request_budget
from reception_service.model_providers.transport import ProviderTransport


class ResourceBudgetTest(unittest.IsolatedAsyncioTestCase):
    async def test_image_decode_and_direct_transport_share_budget(self):
        budget = ResourceBudget(400_000_000)
        entered, release = threading.Event(), threading.Event()

        def decode(*_args):
            entered.set()
            release.wait(3)
            return {}

        class Transport(ProviderTransport):
            async def _generate_leased(self, **_kwargs):
                self.asserted = budget.snapshot()['reserved_bytes'] > 0
                return {}

        with patch('reception_service.image_assets.PROCESS_MEDIA_BUDGET', budget), \
             patch('reception_service.image_assets.inspect_image', return_value={'width': 1, 'height': 1, 'mime': 'image/png'}), \
             patch('reception_service.image_assets._decode_image_leased', decode), \
             patch('reception_service.model_providers.transport.PROCESS_MEDIA_BUDGET', budget):
            thread = threading.Thread(target=decode_image, args=(b'x', 'image/png'))
            thread.start()
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            self.assertEqual(budget.snapshot()['pixel_workers'], 1)
            # Fill the remaining space so the transport must wait asynchronously.
            remaining = budget.limit - budget.snapshot()['reserved_bytes']
            filler = budget.acquire(remaining)
            transport = Transport()
            task = asyncio.create_task(transport.generate(profile={}, api_key='x', system_prompt='x', user_payload='x'))
            await asyncio.sleep(.06)
            self.assertFalse(task.done())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            filler.close()
            release.set()
            await asyncio.to_thread(thread.join, 2)
            self.assertEqual(budget.snapshot()['reserved_bytes'], 0)
            self.assertEqual(budget.snapshot()['pixel_workers'], 0)

    async def test_direct_transport_retains_lease_until_actual_cleanup(self):
        budget = ResourceBudget(1_000_000)
        entered, finish = asyncio.Event(), asyncio.Event()

        class Transport(ProviderTransport):
            async def _generate_leased(self, **_kwargs):
                entered.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    await finish.wait()
                    raise

        with patch('reception_service.model_providers.transport.PROCESS_MEDIA_BUDGET', budget):
            task = asyncio.create_task(Transport().generate(profile={}, api_key='x', system_prompt='abc', user_payload='x'))
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            self.assertGreater(budget.snapshot()['reserved_bytes'], 0)
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(budget.snapshot()['reserved_bytes'], 0)

    def test_resize_updates_peak_and_close_releases(self):
        budget = ResourceBudget(100)
        lease = budget.acquire(10)
        lease.resize(70)
        lease.resize(20)
        self.assertEqual(budget.snapshot()['peak_reserved_bytes'], 70)
        lease.close()
        self.assertEqual(budget.snapshot()['reserved_bytes'], 0)

    def test_estimate_rejects_input_over_request_limit(self):
        self.assertEqual(validate_request_budget('ab', 'cd', ()), 128)
        from reception_service.errors import ServiceError
        with self.assertRaises(ServiceError):
            validate_request_budget('', 'x' * (4 * 1024 * 1024 + 1), ())
        # A 60 MiB batch has a worst-case encoded/serialized footprint above
        # the 512 MiB request budget, even though it fits the raw batch cap.
        data = b'x' * (20 * 1024 * 1024)
        with self.assertRaises(ServiceError):
            validate_request_budget('', '', [{'data': data}] * 3)
