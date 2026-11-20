"""Receive Telegram updates only while a feature needs live events."""
import asyncio


class UpdateControl:
    def init_update_control(self):
        self.update_users = {}
        self.update_locks = {}

    async def acquire_updates(self, key, consumer):
        lock = self.update_locks.setdefault(key, asyncio.Lock())
        async with lock:
            users = self.update_users.setdefault(key, set())
            if consumer in users:
                return
            client = self.clients[key][0]
            if not users:
                try:
                    await asyncio.wait_for(client.set_receive_updates(True), 30)
                except BaseException:
                    # Roll back Telethon's flag if enabling was cancelled or failed.
                    await client.set_receive_updates(False)
                    self.update_users.pop(key, None)
                    raise
            users.add(consumer)

    async def release_updates(self, key, consumer):
        lock = self.update_locks.setdefault(key, asyncio.Lock())
        async with lock:
            users = self.update_users.get(key)
            if not users or consumer not in users:
                return
            users.remove(consumer)
            if not users:
                self.update_users.pop(key, None)
                client = self.clients.get(key)
                if client:
                    await client[0].set_receive_updates(False)


class LiveUpdateLease:
    def __init__(self, engine, key, consumer):
        self.engine, self.key, self.consumer = engine, key, consumer
        self.acquired = False

    async def start(self):
        acquire = getattr(self.engine, 'acquire_updates', None)
        if acquire and not self.acquired:
            await acquire(self.key, self.consumer)
            self.acquired = True

    async def close(self):
        if self.acquired:
            await self.engine.release_updates(self.key, self.consumer)
            self.acquired = False


async def run_with_updates(engine, key, consumer, factory):
    lease = LiveUpdateLease(engine, key, consumer)
    try:
        await lease.start()
        return await factory()
    finally:
        await lease.close()
