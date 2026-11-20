"""Telethon 1.x client that does not synchronize idle mailing-only accounts."""
import asyncio

from telethon import TelegramClient


class _LiveUpdatesQueue(asyncio.Queue):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner

    def put_nowait(self, update):
        if self.owner._no_updates:
            self.owner.background_updates_discarded += 1
            return
        return super().put_nowait(update)


class AccountClient(TelegramClient):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('receive_updates', False)
        kwargs.setdefault('entity_cache_limit', 2000)
        super().__init__(*args, **kwargs)
        self.background_updates_discarded = 0
        self._updates_queue = _LiveUpdatesQueue(self)
        self._sender._updates_queue = self._updates_queue

    async def _update_loop(self):
        # In Telethon 1.x, get_dialogs also creates 15-minute difference timers.
        # receive_updates=False alone does not disable this synchronization loop.
        if not self._no_updates:
            await super()._update_loop()

    def _trim_idle_cache(self):
        if self._no_updates:
            # RPC results are already persisted through session.process_entities.
            # Cache misses can use SQLite; no authorization/session rows are removed.
            own_id = self._mb_entity_cache.self_id
            self._mb_entity_cache.retain(lambda ident: ident == own_id)

    async def _save_states_and_entities(self):
        await super()._save_states_and_entities()
        self._trim_idle_cache()

    async def get_dialogs(self, *args, **kwargs):
        dialogs = await super().get_dialogs(*args, **kwargs)
        if len(self._mb_entity_cache) > self._entity_cache_limit:
            self._trim_idle_cache()
        return dialogs

    async def set_receive_updates(self, receive_updates):
        if receive_updates:
            self._no_updates = False
            if self._updates_handle is None or self._updates_handle.done():
                self._updates_handle = self.loop.create_task(self._update_loop())
            try:
                await super().set_receive_updates(True)
            except BaseException:
                await self.set_receive_updates(False)
                raise
        else:
            await super().set_receive_updates(False)
            task = self._updates_handle
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if self._updates_handle is task:
                    self._updates_handle = None
            handlers = [task for task in self._event_handler_tasks
                        if task is not asyncio.current_task()]
            for handler in handlers:
                handler.cancel()
            if handlers:
                await asyncio.gather(*handlers, return_exceptions=True)
                self._event_handler_tasks.difference_update(handlers)
            # RPC responses can include our outgoing updates too. Do not retain
            # them while the consumer is intentionally stopped.
            while not self._updates_queue.empty():
                self._updates_queue.get_nowait()
            self._trim_idle_cache()
