import asyncio
import unittest

from update_control import UpdateControl, run_with_updates


class Client:
    def __init__(self):
        self.enabled = False
        self.changes = []

    async def set_receive_updates(self, enabled):
        self.enabled = enabled
        self.changes.append(enabled)


class Engine(UpdateControl):
    def __init__(self):
        self.client = Client()
        self.clients = {'account': (self.client, 1)}
        self.init_update_control()


class UpdateControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_mailing_sessions_start_without_background_updates(self):
        from pathlib import Path
        import app
        _api_id, _api_hash, options = app.session_settings(
            Path('nonexistent.session'), 12345, 'a' * 32)
        self.assertIs(options['receive_updates'], False)
        self.assertEqual(options['entity_cache_limit'], 2000)

    async def test_parser_and_likes_share_one_subscription(self):
        engine = Engine()
        await engine.acquire_updates('account', 'parser')
        await engine.acquire_updates('account', 'likes')
        await engine.release_updates('account', 'likes')
        self.assertTrue(engine.client.enabled)
        await engine.release_updates('account', 'parser')
        self.assertFalse(engine.client.enabled)
        self.assertEqual(engine.client.changes, [True, False])
        self.assertFalse(engine.update_users)

    async def test_exception_in_feature_releases_the_subscription(self):
        engine = Engine()
        async def feature():
            self.assertTrue(engine.client.enabled)
            raise ValueError('feature failed')
        with self.assertRaises(ValueError):
            await run_with_updates(engine, 'account', 'parser', feature)
        self.assertFalse(engine.client.enabled)

    async def test_cancelling_live_feature_releases_the_subscription(self):
        engine = Engine()
        ready = asyncio.Event()
        async def feature():
            ready.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(run_with_updates(engine, 'account', 'likes', feature))
        await ready.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(engine.client.enabled)
        self.assertFalse(engine.update_users)

    async def test_failed_enable_rolls_back_and_can_be_retried(self):
        engine = Engine()
        original = engine.client.set_receive_updates
        async def fail(enabled):
            await original(enabled)
            if enabled:
                raise OSError('network unavailable')
        engine.client.set_receive_updates = fail
        with self.assertRaises(OSError):
            await engine.acquire_updates('account', 'parser')
        self.assertFalse(engine.client.enabled)
        self.assertFalse(engine.update_users)
        engine.client.set_receive_updates = original
        await engine.acquire_updates('account', 'parser')
        self.assertTrue(engine.client.enabled)
        await engine.release_updates('account', 'parser')


if __name__ == '__main__':
    unittest.main()
