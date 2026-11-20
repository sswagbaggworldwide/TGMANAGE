import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from telethon import TelegramClient
from telegram_runtime import AccountClient


class AccountClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = AccountClient(None, 12345, 'a' * 32)

    async def test_idle_incoming_and_outgoing_updates_do_not_accumulate(self):
        for _ in range(10000):
            self.client._sender._updates_queue.put_nowait(object())
        self.assertEqual(self.client._updates_queue.qsize(), 0)
        self.assertEqual(self.client.background_updates_discarded, 10000)

    async def test_idle_update_worker_does_not_enter_difference_synchronization(self):
        with patch.object(TelegramClient, '_update_loop', new_callable=AsyncMock) as worker:
            await self.client._update_loop()
            worker.assert_not_awaited()

    async def test_live_worker_starts_and_stops_without_losing_the_connection(self):
        ready = asyncio.Event()
        async def live_worker():
            ready.set()
            await asyncio.Event().wait()
        with patch.object(TelegramClient, '_update_loop', side_effect=live_worker), \
                patch.object(AccountClient, '__call__', new_callable=AsyncMock), \
                patch.object(self.client, 'disconnect', new_callable=AsyncMock) as disconnect:
            await self.client.set_receive_updates(True)
            await ready.wait()
            self.client._updates_queue.put_nowait('live event')
            self.assertEqual(self.client._updates_queue.qsize(), 1)
            task = self.client._updates_handle
            await self.client.set_receive_updates(False)
            self.assertTrue(task.done())
            self.assertIsNone(self.client._updates_handle)
            self.assertEqual(self.client._updates_queue.qsize(), 0)
            disconnect.assert_not_awaited()

    async def test_failed_enable_stops_the_new_worker(self):
        async def live_worker():
            await asyncio.Event().wait()
        with patch.object(TelegramClient, '_update_loop', side_effect=live_worker), \
                patch.object(AccountClient, '__call__', new_callable=AsyncMock,
                             side_effect=OSError('network error')):
            with self.assertRaises(OSError):
                await self.client.set_receive_updates(True)
        self.assertTrue(self.client._no_updates)
        self.assertIsNone(self.client._updates_handle)

    async def test_stopping_updates_releases_pending_dispatch_tasks(self):
        handler = asyncio.create_task(asyncio.Event().wait())
        self.client._event_handler_tasks.add(handler)
        await self.client.set_receive_updates(False)
        self.assertTrue(handler.done())
        self.assertFalse(self.client._event_handler_tasks)

    async def test_idle_cache_is_trimmed_but_live_cache_is_kept(self):
        cache = self.client._mb_entity_cache
        cache.set_self_user(1, False, 123)
        cache.hash_map[2] = cache.hash_map[1]
        with patch.object(TelegramClient, '_save_states_and_entities', new_callable=AsyncMock):
            self.client._no_updates = False
            await self.client._save_states_and_entities()
            self.assertEqual(len(cache), 2)
            self.client._no_updates = True
            await self.client._save_states_and_entities()
            self.assertEqual(len(cache), 1)
            self.assertIsNotNone(cache.get(1))


if __name__ == '__main__':
    unittest.main()
