"""No live Telegram connections or bot actions."""
import asyncio
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import patch

from bot_likes import LikesEngine, button_key, classify, normal_button, token, validate_settings

SETTINGS = {'like': 'LIKE', 'resume': 'BROWSE', 'quota': ['лайки закончились'],
            'verification': ['нужна верификация'], 'delay': 1}


class Message:
    def __init__(self, ident, text='', button=True, label='LIKE'):
        self.id, self.raw_text, self.out = ident, text, False
        self.edit_date = None
        self.button = button
        self.label = label
        self.clicks = 0
        self.after_click = None

    async def get_buttons(self):
        if not self.button:
            return None
        async def click():
            self.clicks += 1
            if self.after_click:
                self.after_click()
        return [[types.SimpleNamespace(text=self.label, button=type('KeyboardButtonCallback', (), {})(), click=click)]]


class Client:
    def __init__(self, message):
        self.message = message
        self.handlers = []
        self.commands = []
        self.after_command = None

    async def get_entity(self, username):
        from telethon.tl.types import User
        return User(id=123, bot=True, first_name='Bot')

    async def get_messages(self, bot, limit):
        return [self.message]

    def add_event_handler(self, callback, builder):
        self.handlers.append((callback, builder))

    def remove_event_handler(self, callback, builder):
        self.handlers.remove((callback, builder))

    async def update(self, message):
        self.message = message
        for callback, _ in list(self.handlers):
            await callback(types.SimpleNamespace(message=message))

    async def send_message(self, bot, text, **kwargs):
        self.commands.append(text)
        if self.after_command:
            await self.after_command()


class Engine(LikesEngine):
    def __init__(self, folder):
        self.parser_base = folder
        self.clients = {}
        self.events = []
        self.init_likes()

    def emit(self, kind, value):
        self.events.append((kind, value))

    async def ensure_connected(self, key):
        return self.clients[key]


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.folder = Path(self.temp.name)
        self.engine = Engine(self.folder)

    async def asyncTearDown(self):
        await self.engine.stop_likes()
        self.temp.cleanup()

    async def until(self, predicate):
        for _ in range(300):
            if predicate():
                return
            await asyncio.sleep(.01)
        self.fail('Worker did not reach expected state')

    async def test_quota_waits_without_clicks_and_survives_restart(self):
        quota = Message(1, 'Лайки закончились', button=False)
        self.engine.clients['account'] = Client(quota)
        await self.engine.start_likes(['account'], SETTINGS)
        await self.until(lambda: self.engine.likes_state.get('account', {}).get('next_run', 0) > 0)
        due = self.engine.likes_state['account']['next_run']
        self.assertGreater(due, time.time() + 86000)
        self.assertEqual(quota.clicks, 0)
        restarted = Engine(self.folder)
        self.assertEqual(restarted.likes_state['account']['next_run'], due)
        await self.engine.stop_likes(['account'])
        self.assertEqual(self.engine.clients['account'].handlers, [])
        self.assertEqual(self.engine.likes_state['account']['next_run'], due)

    async def test_verification_pauses_only_affected_account(self):
        self.engine.clients = {'verify': Client(Message(1, 'Нужна верификация')),
                               'wait': Client(Message(2, 'Лайки закончились', False))}
        await self.engine.start_likes(['verify', 'wait'], SETTINGS)
        await self.until(lambda: self.engine.likes_jobs['verify'].done() and 'wait' in self.engine.likes_state)
        self.assertFalse(self.engine.likes_jobs['wait'].done())
        self.assertTrue(any(kind == 'likes_attention' and value[0] == 'verify' for kind, value in self.engine.events))
        self.assertEqual(self.engine.clients['verify'].handlers, [])
        self.assertTrue(Engine(self.folder).likes_state['verify']['needs_verification'])

    async def test_expired_wait_sends_one_resume_then_checks_verification(self):
        client = Client(Message(1, 'Лайки закончились', False))
        async def new_response():
            await client.update(Message(2, 'Нужна верификация', False))
        client.after_command = new_response
        self.engine.clients['account'] = client
        self.engine.likes_save('account', next_run=time.time() - 1)
        await self.engine.start_likes(['account'], SETTINGS)
        await self.until(lambda: self.engine.likes_jobs['account'].done())
        self.assertEqual(client.commands, ['BROWSE'])
        self.assertEqual(self.engine.likes_state['account']['next_run'], 0)
        self.assertTrue(any(k == 'likes_attention' for k, _ in self.engine.events))

    async def test_one_like_per_profile_then_quota(self):
        card = Message(1, 'Profile')
        client = Client(card)
        card.after_click = lambda: asyncio.create_task(client.update(Message(2, 'Лайки закончились', False)))
        self.engine.clients['account'] = client
        await self.engine.start_likes(['account'], SETTINGS)
        await self.until(lambda: self.engine.likes_state.get('account', {}).get('next_run', 0) > 0)
        self.assertEqual(card.clicks, 1)
        self.assertEqual(self.engine.likes_state['account']['count'], 1)

    async def test_unknown_screen_stops_without_clicking(self):
        message = Message(1, 'Main menu', False)
        self.engine.clients['account'] = Client(message)
        await self.engine.start_likes(['account'], SETTINGS)
        await self.until(lambda: self.engine.likes_jobs['account'].done())
        self.assertEqual(message.clicks, 0)
        self.assertEqual(self.engine.clients['account'].handlers, [])

    async def test_ad_skip_does_not_increment_like_count(self):
        advertisement = Message(1, 'Advertisement', label='👎 Не интересно')
        client = Client(advertisement)
        advertisement.after_click = lambda: asyncio.create_task(client.update(Message(2, 'Лайки закончились', False)))
        self.engine.clients['account'] = client
        await self.engine.start_likes(['account'], SETTINGS)
        await self.until(lambda: self.engine.likes_state.get('account', {}).get('next_run', 0) > 0)
        self.assertEqual(advertisement.clicks, 1)
        self.assertEqual(self.engine.likes_state['account'].get('count', 0), 0)

    async def test_user_verification_phrase_with_changing_emoji(self):
        text = ('Для верификации анкеты отправь видео-кружок - скажи на камеру Дайвинчик '
                'и покажи рукой так - ✌️\n⚠️ Подделка верификации приводит к немедленной блокировке.')
        self.engine.clients['account'] = Client(Message(1, text))
        settings = dict(SETTINGS, verification=['Для верификации анкеты отправь видео-кружок'])
        await self.engine.start_likes(['account'], settings)
        await self.until(lambda: self.engine.likes_jobs['account'].done())
        self.assertTrue(self.engine.likes_state['account']['needs_verification'])

    async def test_verification_during_daily_wait_is_reported_immediately(self):
        client = Client(Message(1, 'Лайки закончились', False))
        self.engine.clients['account'] = client
        await self.engine.start_likes(['account'], SETTINGS)
        await self.until(lambda: 'account' in self.engine.likes_state)
        await client.update(Message(2, 'Нужна верификация', False))
        await self.until(lambda: self.engine.likes_jobs['account'].done())
        self.assertTrue(any(k == 'likes_attention' for k, _ in self.engine.events))

    async def test_failed_state_write_prevents_like(self):
        message = Message(1, 'Profile')
        self.engine.clients['account'] = Client(message)
        with patch('bot_likes.atomic_json', side_effect=OSError('disk failure')):
            await self.engine.start_likes(['account'], SETTINGS)
            await self.until(lambda: self.engine.likes_jobs['account'].done())
        self.assertEqual(message.clicks, 0)


class RecognitionTests(unittest.TestCase):
    def test_verification_precedes_quota(self):
        self.assertEqual(classify('ЛАЙКИ закончились. Нужна верификация', SETTINGS), 'verification')

    def test_button_spacing_and_emoji_presentation(self):
        self.assertEqual(button_key('1 🚀'), button_key('1🚀'))
        self.assertEqual(button_key('❤️'), button_key('❤'))

    def test_empty_button_and_nonfinite_delay_rejected(self):
        for changes in ({'like': ''}, {'resume': ''}, {'delay': float('nan')}):
            with self.assertRaises(ValueError):
                validate_settings(dict(SETTINGS, **changes))

    def test_buttons_requesting_contact_or_url_are_not_actions(self):
        for kind in ('KeyboardButtonRequestPhone', 'KeyboardButtonUrl', 'InlineButtonTypeUrl'):
            self.assertFalse(normal_button(types.SimpleNamespace(button=type(kind, (), {})())))

    def test_edited_profile_changes_token(self):
        card = Message(1, 'First')
        before = token(card)
        card.raw_text = 'Second'
        self.assertNotEqual(before, token(card))


if __name__ == '__main__':
    unittest.main()
