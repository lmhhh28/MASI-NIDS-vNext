from __future__ import annotations

import asyncio
import unittest

from masi_analysis.concurrency import gather_cancel_on_error


class StructuredConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_error_cancels_and_joins_sibling(self) -> None:
        sibling_started = asyncio.Event()
        sibling_cancelled = asyncio.Event()

        async def sibling() -> None:
            sibling_started.set()
            try:
                await asyncio.Future()
            finally:
                sibling_cancelled.set()

        async def failing() -> None:
            await sibling_started.wait()
            raise RuntimeError("expected failure")

        with self.assertRaisesRegex(RuntimeError, "expected failure"):
            await gather_cancel_on_error([sibling(), failing()])
        self.assertTrue(sibling_cancelled.is_set())

    async def test_success_preserves_input_order(self) -> None:
        async def value(item: int) -> int:
            await asyncio.sleep(0)
            return item

        self.assertEqual([2, 1], await gather_cancel_on_error([value(2), value(1)]))


if __name__ == "__main__":
    unittest.main()
