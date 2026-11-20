"""Offline regression checks; no Telegram credentials or network calls."""
import asyncio
import csv
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import sqlite3
import queue

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from telethon.errors import FloodWaitError
from telethon.tl.types import User, Channel, Chat
from user_parser import KeywordFilter, UserStore, UserParser, ParserEngine, message_link


def record(user_id=1, username='user_one', chat_id=-100123, message_id=1, date='2026-10-14T10:00:00+00:00'):
    return dict(user_id=user_id, username=username, first_name='First', last_name='', message_date=date,
                chat_id=chat_id, chat_title='Test chat', message_id=message_id,
                message_link='https://t.me/test/1', keyword='модел', message_text='Ищу модель')


def dialog(ident=-100123, title='Test chat'):
    return types.SimpleNamespace(id=ident, name=title, is_group=True,
                                 entity=Channel(id=123, title=title, photo=None, date=None, megagroup=True, username='test_chat'))


class FakeMessage:
    def __init__(self, ident=1, text='Ищу модель', user=None, out=False, date=None):
        self.id, self.raw_text, self.out = ident, text, out
        self.date = date or datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)
        self.user = user if user is not None else User(id=1, username='user_one', first_name='First')
        self.sender_calls = 0

    async def get_sender(self):
        self.sender_calls += 1
        return self.user


class FakeClient:
    def __init__(self, messages=()):
        self.messages = list(messages)
        self.handlers = []
        self.offsets = []
        self.fail_history_once = False
        self.history_started = asyncio.Event()
        self.history_release = None
        self.disconnected = asyncio.get_running_loop().create_future()

    def is_connected(self):
        return not self.disconnected.done()

    def add_event_handler(self, callback, builder):
        self.handlers.append((callback, builder))

    def remove_event_handler(self, callback, builder):
        self.handlers.remove((callback, builder))

    async def iter_messages(self, entity, *, limit, offset_id, wait_time):
        self.offsets.append(offset_id)
        self.history_started.set()
        if self.history_release is not None:
            await self.history_release.wait()
        items = [message for message in self.messages if not offset_id or message.id < offset_id]
        if limit is not None:
            items = items[:limit]
        for index, message in enumerate(items):
            yield message
            if index == 0 and self.fail_history_once:
                self.fail_history_once = False
                raise FloodWaitError(request=None, capture=-1)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = UserStore(Path(self.temp.name) / 'users.sqlite3')

    def tearDown(self):
        self.temp.cleanup()

    def test_keywords_case_spaces_variants(self):
        f = KeywordFilter([' ИЩУ   МОДЕЛЬ ', 'съемка', 'ищу модель'])
        self.assertEqual(f.match('Ищу\nмодель для проекта'), 'ищу модель')
        self.assertEqual(f.match('Планируется СЪЁМКА'), 'съемка')
        self.assertIsNone(f.match('Предлагаю рекламу'))
        self.assertEqual(KeywordFilter(['модел']).match('Нужны модели'), 'модел')
        with self.assertRaises(ValueError):
            KeywordFilter(['   '])

    def test_dedup_history_live_and_multiple_chats(self):
        self.assertTrue(self.store.save_match(record())[0])
        self.assertFalse(self.store.save_match(record())[1])
        self.store.save_match(record(chat_id=-100456))
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot['count'], 1)
        self.assertEqual(snapshot['rows'][0]['matches_count'], 2)

    def test_restart_username_change_and_latest_post(self):
        self.store.save_match(record(message_id=20, date='2026-10-14T12:00:00+00:00'))
        self.store = UserStore(self.store.path)
        self.store.save_match(record(username='renamed', message_id=10, date='2026-10-14T10:00:00+00:00'))
        row = self.store.snapshot()['rows'][0]
        self.assertEqual(row['username'], 'renamed')
        self.assertEqual(row['message_id'], 20)
        self.assertEqual(row['matches_count'], 2)
        self.store.save_match(record(username='', message_id=21, date='2026-10-14T13:00:00+00:00'))
        self.assertEqual(self.store.snapshot()['usernames'], 0)

    def test_no_username_csv_and_txt(self):
        self.store.save_match(record(username=''))
        self.store.save_match(record(user_id=2, username='UserTwo', message_id=2))
        self.store.save_match(record(user_id=3, username='usertwo', message_id=3))
        txt, csv_path = Path(self.temp.name) / 'users.txt', Path(self.temp.name) / 'users.csv'
        self.assertEqual(self.store.export(txt, 'txt'), 1)
        self.assertEqual(txt.read_text(), '@usertwo\n')
        self.assertEqual(self.store.export(csv_path, 'csv'), 3)
        with csv_path.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.reader(stream, delimiter=';'))
        self.assertEqual(rows[1][0], '1')
        self.assertEqual(rows[1][1], '')

    def test_csv_third_party_formula_is_text(self):
        item = record()
        item['first_name'] = '=1+1'
        self.store.save_match(item)
        path = Path(self.temp.name) / 'users.csv'
        self.store.export(path, 'csv')
        with path.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.reader(stream, delimiter=';'))
        self.assertEqual(rows[1][2], "'=1+1")

    def test_message_links(self):
        d = dialog()
        self.assertEqual(message_link(d, 5), 'https://t.me/test_chat/5')
        d.entity.username = None
        self.assertEqual(message_link(d, 5), 'https://t.me/c/123/5')
        d.entity = Chat(id=123, title='Private', photo=None, participants_count=1, date=None, version=1)
        self.assertEqual(message_link(d, 5), '')

    def test_export_cannot_overwrite_database(self):
        self.store.save_match(record())
        with self.assertRaises(ValueError):
            self.store.export(self.store.path, 'csv')
        self.assertEqual(self.store.snapshot()['count'], 1)

    def test_failed_export_preserves_previous_file(self):
        self.store.save_match(record())
        path = Path(self.temp.name) / 'users.csv'
        path.write_text('previous export')
        with patch('user_parser.csv.writer', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.store.export(path, 'csv')
        self.assertEqual(path.read_text(), 'previous export')
        self.assertEqual(list(Path(self.temp.name).glob('*.tmp')), [])

    def test_corrupt_database_does_not_get_replaced(self):
        path = Path(self.temp.name) / 'broken.sqlite3'
        path.write_bytes(b'not a sqlite database')
        with self.assertRaises(sqlite3.DatabaseError):
            UserStore(path)
        self.assertEqual(path.read_bytes(), b'not a sqlite database')

    def test_export_includes_rows_beyond_ui_limit(self):
        for ident in range(1, 1006):
            self.store.save_match(record(user_id=ident, username='user' + str(ident), message_id=ident))
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot['count'], 1005)
        self.assertEqual(len(snapshot['rows']), 1000)
        self.assertEqual(self.store.export(Path(self.temp.name) / 'all.csv', 'csv'), 1005)


class ParserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = UserStore(Path(self.temp.name) / 'users.sqlite3')
        self.events = []
        self.client = FakeClient()
        self.parser = UserParser(self.client, 99, [dialog()], ['модел'], self.store,
                                 lambda k,v:self.events.append((k,v)), history=False)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_only_matching_actual_authors(self):
        self.parser.active = True
        irrelevant = FakeMessage(text='Другой текст')
        await self.parser.process(irrelevant, -100123)
        self.assertEqual(irrelevant.sender_calls, 0)
        await self.parser.process(FakeMessage(text='Ищу модель, контакт @someone_else'), -100123)
        await self.parser.process(FakeMessage(2, user=User(id=2, bot=True)), -100123)
        await self.parser.process(FakeMessage(3, user=User(id=3, deleted=True)), -100123)
        await self.parser.process(FakeMessage(4, user=dialog().entity), -100123)
        await self.parser.process(FakeMessage(5, user=User(id=99)), -100123)
        await self.parser.process(FakeMessage(6, out=True), -100123)
        await self.parser.process(FakeMessage(7), -100999)
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot['count'], 1)
        self.assertEqual(snapshot['rows'][0]['username'], 'user_one')

    async def test_live_collection_and_stop(self):
        task = asyncio.create_task(self.parser.run())
        await asyncio.sleep(0)
        self.assertEqual(len(self.client.handlers), 1)
        callback, _ = self.client.handlers[0]
        await callback(types.SimpleNamespace(message=FakeMessage(), chat_id=-100123))
        await callback(types.SimpleNamespace(message=FakeMessage(), chat_id=-100123))
        self.assertEqual(self.store.snapshot()['rows'][0]['matches_count'], 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.client.handlers, [])
        await callback(types.SimpleNamespace(message=FakeMessage(2, user=User(id=2)), chat_id=-100123))
        self.assertEqual(self.store.snapshot()['count'], 1)

    async def test_listener_is_ready_before_history_and_overlap(self):
        self.client.messages = [FakeMessage(3), FakeMessage(2), FakeMessage(1)]
        self.client.history_release = asyncio.Event()
        self.parser.history = True
        self.parser.history_limit = 2
        task = asyncio.create_task(self.parser.run())
        await self.client.history_started.wait()
        callback, _ = self.client.handlers[0]
        await callback(types.SimpleNamespace(message=FakeMessage(3), chat_id=-100123))
        self.client.history_release.set()
        for _ in range(20):
            if any(kind == 'parser_status' and value.startswith('Сбор новых') for kind,value in self.events):
                break
            await asyncio.sleep(0)
        self.assertEqual(self.parser.scanned, 2)
        self.assertEqual(self.store.snapshot()['rows'][0]['matches_count'], 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_all_history_and_resume_after_floodwait(self):
        self.parser.active = True
        self.parser.history_limit = 0
        self.client.messages = [FakeMessage(3), FakeMessage(2), FakeMessage(1)]
        self.client.fail_history_once = True
        await self.parser.collect_history(dialog())
        self.assertEqual(self.client.offsets, [0, 3])
        self.assertEqual(self.parser.scanned, 3)
        self.assertEqual(self.store.snapshot()['rows'][0]['matches_count'], 3)

    async def test_stop_cancels_inflight_event(self):
        task = asyncio.create_task(self.parser.run())
        await asyncio.sleep(0)
        gate = asyncio.Event()
        message = FakeMessage()
        async def waiting_sender():
            await gate.wait()
            return message.user
        message.get_sender = waiting_sender
        callback, _ = self.client.handlers[0]
        live = asyncio.create_task(callback(types.SimpleNamespace(message=message, chat_id=-100123)))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(live.cancelled())
        self.assertEqual(self.store.snapshot()['count'], 0)

    async def test_immediate_engine_stop_restores_running_state(self):
        engine = ParserEngine()
        engine.init_parser()
        engine.parser_base = Path(self.temp.name)
        engine.parser_store = self.store
        engine.clients = {'account': (self.client, 99)}
        engine.parser_dialogs = {'account': {-100123: dialog()}}
        engine.emit = lambda k,v:self.events.append((k,v))
        await engine.start_parser('account', [-100123], ['модел'], False, 1000)
        await engine.stop_parser()
        self.assertIn(('parser_running', False), self.events)
        self.assertEqual(self.client.handlers, [])

    async def test_live_updates_are_enabled_only_after_listener_is_attached(self):
        started, stopped = [], []
        async def enable():
            started.append(len(self.client.handlers))
        async def disable():
            stopped.append(len(self.client.handlers))
        self.parser.update_lease = types.SimpleNamespace(start=enable, close=disable)
        task = asyncio.create_task(self.parser.run())
        await asyncio.sleep(0)
        self.assertEqual(started, [1])
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(stopped, [0])

    async def test_disconnect_stops_parser_and_removes_listener(self):
        task = asyncio.create_task(self.parser.run())
        await asyncio.sleep(0)
        self.client.disconnected.set_result(None)
        await asyncio.wait_for(task, 1)
        self.assertEqual(self.client.handlers, [])
        self.assertFalse(self.parser.active)
        self.assertTrue(any(kind == 'parser_status' and 'Соединение' in value for kind,value in self.events))

    async def test_stop_does_not_cancel_client_disconnect_future(self):
        task = asyncio.create_task(self.parser.run())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.client.disconnected.cancelled())

    async def test_storage_failure_stops_parser(self):
        task = asyncio.create_task(self.parser.run())
        await asyncio.sleep(0)
        callback, _ = self.client.handlers[0]
        with patch.object(self.store, 'save_match', side_effect=sqlite3.OperationalError('locked')):
            await callback(types.SimpleNamespace(message=FakeMessage(), chat_id=-100123))
            await asyncio.wait_for(task, 1)
        self.assertFalse(self.parser.active)
        self.assertEqual(self.client.handlers, [])
        self.assertEqual(self.store.snapshot()['count'], 0)
        self.assertTrue(any(kind == 'parser_status' and 'База недоступна' in value for kind,value in self.events))

    async def test_database_failure_during_history_stops_parser(self):
        self.parser.history = True
        self.client.messages = [FakeMessage()]
        with patch.object(self.store, 'save_match', side_effect=sqlite3.OperationalError('readonly')):
            await asyncio.wait_for(self.parser.run(), 1)
        self.assertEqual(self.client.handlers, [])
        self.assertFalse(self.parser.active)

    async def test_disconnect_interrupts_history(self):
        self.parser.history = True
        self.client.history_release = asyncio.Event()
        task = asyncio.create_task(self.parser.run())
        await self.client.history_started.wait()
        self.client.disconnected.set_result(None)
        await asyncio.wait_for(task, 1)
        self.assertEqual(self.client.handlers, [])

    async def test_double_start_and_restart_do_not_duplicate_listener(self):
        engine = ParserEngine()
        engine.init_parser()
        engine.parser_base = Path(self.temp.name)
        engine.parser_store = self.store
        engine.clients = {'account': (self.client, 99)}
        engine.parser_dialogs = {'account': {-100123: dialog()}}
        engine.emit = lambda k,v:self.events.append((k,v))
        await engine.start_parser('account', [-100123], ['модел'], False, 1000)
        await asyncio.sleep(0)
        await engine.start_parser('account', [-100123], ['модел'], False, 1000)
        self.assertEqual(len(self.client.handlers), 1)
        await engine.stop_parser()
        await engine.start_parser('account', [-100123], ['модел'], False, 1000)
        await asyncio.sleep(0)
        self.assertEqual(len(self.client.handlers), 1)
        await engine.stop_parser()
        self.assertFalse(self.client.disconnected.cancelled())

    async def test_invalid_start_restores_controls(self):
        engine = ParserEngine()
        engine.init_parser()
        engine.parser_base = Path(self.temp.name)
        engine.clients = {'account': (self.client, 99)}
        engine.parser_dialogs = {'account': {-100123: dialog()}}
        engine.emit = lambda k,v:self.events.append((k,v))
        for key,chats,keywords,limit in [('missing',[-100123],['модел'],1),
                                        ('account',[],['модел'],1),
                                        ('account',[-100123],[],1),
                                        ('account',[-100123],['модел'],-1)]:
            self.events.clear()
            await engine.start_parser(key,chats,keywords,False,limit)
            self.assertIn(('parser_running',False), self.events)
            self.assertIsNone(engine.parser_task)

    async def test_scan_reconnects_a_disconnected_account(self):
        import app
        folder = Path(self.temp.name)
        session = folder / 'accounts' / 'account.session'
        session.parent.mkdir()
        session.touch()
        old = FakeClient()
        old.disconnected.set_result(None)
        disconnected_old = []
        async def close_old():
            disconnected_old.append(True)
        old.disconnect = close_old
        new = FakeClient()
        async def connect_new():
            pass
        async def authorized():
            return True
        async def identity():
            return User(id=99, first_name='Owner')
        new.connect, new.is_user_authorized, new.get_me = connect_new, authorized, identity
        engine = app.Engine.__new__(app.Engine)
        engine.init_parser()
        engine.clients = {'account.session': (old, 99)}
        engine.jobs = {}
        engine.likes_jobs = {}
        engine.likes_state = {}
        engine.connection_monitors = {}
        engine.connection_locks = {}
        engine.emit = lambda k,v:self.events.append((k,v))
        with patch('app.BASE', folder), patch('app.discover', return_value=[session]), \
             patch('app.validate_session'), patch('app.session_settings', return_value=(123, 'a'*32, {})), \
             patch('telegram_runtime.AccountClient', return_value=new) as constructor, \
             patch.object(engine, 'start_connection_monitor'):
            await engine.scan('', '')
            constructor.assert_called_once()
        self.assertEqual(disconnected_old, [True])
        self.assertIs(engine.clients['account.session'][0], new)

    async def test_scan_keeps_a_connected_account(self):
        import app
        folder = Path(self.temp.name)
        session = folder / 'accounts' / 'account.session'
        engine = app.Engine.__new__(app.Engine)
        engine.init_parser()
        engine.clients = {'account.session': (self.client, 99)}
        engine.jobs = {}
        engine.emit = lambda k,v:self.events.append((k,v))
        with patch('app.BASE', folder), patch('app.discover', return_value=[session]), \
             patch('telegram_runtime.AccountClient') as constructor:
            await engine.scan('', '')
            constructor.assert_not_called()
        self.assertIs(engine.clients['account.session'][0], self.client)


class EventPumpTests(unittest.TestCase):
    def test_busy_event_queue_yields_and_keeps_all_events(self):
        import app
        processed, scheduled = [], []
        state = types.SimpleNamespace(events=queue.Queue(),
                                      parser_event=lambda kind,value: processed.append(value),
                                      after=lambda delay,callback: scheduled.append((delay,callback)),
                                      poll=lambda:None)
        for number in range(300):
            state.events.put(('parser_test', number))
        app.App.poll(state)
        self.assertGreater(state.events.qsize(), 0)
        self.assertEqual(scheduled[-1][0], 10)
        app.App.poll(state)
        self.assertEqual(processed, list(range(300)))
        self.assertEqual(scheduled[-1][0], 100)


if __name__ == '__main__':
    unittest.main()
