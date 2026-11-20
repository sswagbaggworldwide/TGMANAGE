"""Offline checks for independent mailings and saved direction settings."""
import asyncio
import gc
import json
from pathlib import Path
import tempfile
import types
import unittest
import weakref
from unittest.mock import patch

import app


class DirectionEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = app.Engine.__new__(app.Engine)
        self.engine.jobs = {}
        self.engine.job_directions = {}
        self.engine.clients = {key: object() for key in ('a', 'b', 'c')}
        self.events = []
        self.engine.emit = lambda kind, value: self.events.append((kind, value))
        self.sent = []
        self.forwarded = []
        async def ensure(key):
            async def dialogs():
                return [types.SimpleNamespace(id=1, is_group=True),
                        types.SimpleNamespace(id=2, is_group=True)]
            async def send(ident, text, **kwargs):
                self.sent.append((key, ident, text))
            async def get_messages(source, ids):
                return types.SimpleNamespace(id=ids, chat_id=-100999, source=source)
            async def forward(ident, message_id, **kwargs):
                self.forwarded.append((key, ident, message_id, kwargs.get('from_peer')))
            return types.SimpleNamespace(get_dialogs=dialogs, send_message=send,
                                         get_messages=get_messages, forward_messages=forward)
        self.engine.ensure_connected = ensure

    async def asyncTearDown(self):
        await self.engine.stop(list(self.engine.jobs))

    async def test_parallel_directions_keep_texts_separate_and_snapshot_input(self):
        texts = ['A1', 'A2']
        await self.engine.start_many(['a'], texts, 'sequence', 0, 0, 0, 'one', 'One')
        await self.engine.start_many(['b'], ['B'], 'sequence', 0, 0, 0, 'two', 'Two')
        texts[0] = 'Changed after launch'
        await asyncio.gather(*self.engine.jobs.values())
        self.assertEqual([v[2] for v in self.sent if v[0] == 'a'], ['A1', 'A2'])
        self.assertEqual([v[2] for v in self.sent if v[0] == 'b'], ['B', 'B'])
        self.assertEqual(self.engine.job_directions, {})

    async def test_conflict_rejects_entire_batch_and_stop_is_per_direction(self):
        await self.engine.start_many(['a', 'c'], ['A'], 'sequence', 0, 0, 3600, 'one')
        await self.engine.start_many(['b'], ['B'], 'sequence', 0, 3600, 0, 'two')
        await self.engine.start_many(['c', 'b'], ['WRONG'], 'sequence', 0, 0, 0, 'three')
        self.assertEqual(self.engine.job_directions, {'a': 'one', 'c': 'one', 'b': 'two'})
        self.assertTrue(any(kind == 'direction_error' for kind, _ in self.events))
        await asyncio.sleep(0)
        await self.engine.stop_direction('one')
        self.assertTrue(self.engine.jobs['a'].done())
        self.assertTrue(self.engine.jobs['c'].cancelled())
        self.assertFalse(self.engine.jobs['b'].done())
        self.assertFalse(any(v[2] == 'WRONG' for v in self.sent))

    async def test_missing_account_does_not_start_partial_batch(self):
        await self.engine.start_many(['a', 'missing'], ['A'], 'sequence', 0, 0, 0, 'one')
        self.assertFalse(self.engine.jobs)
        self.assertEqual(self.events[0][0], 'direction_error')

    async def test_forward_one_private_channel_post_to_every_group(self):
        link = 'https://t.me/c/1234567890/77'
        await self.engine.start_many_forward(['a', 'b'], link, 0, 0, 0, 'forward', 'Forward')
        await asyncio.gather(*self.engine.jobs.values())
        self.assertEqual(len(self.forwarded), 4)
        self.assertEqual({row[1] for row in self.forwarded}, {1, 2})
        self.assertEqual({row[2] for row in self.forwarded}, {77})
        self.assertEqual({row[3] for row in self.forwarded}, {-1001234567890})

    async def test_mailing_does_not_retain_last_messages_during_long_pauses(self):
        refs, sent = [], asyncio.Event()
        class Payload:
            pass
        async def ensure(key):
            async def dialogs():
                rows = []
                for ident in (1, 2):
                    payload = Payload()
                    refs.append(weakref.ref(payload))
                    rows.append(types.SimpleNamespace(
                        id=ident, is_group=True, name=str(ident), message=payload))
                return rows
            async def forward(*args, **kwargs):
                sent.set()
            return types.SimpleNamespace(get_dialogs=dialogs, forward_messages=forward)
        self.engine.ensure_connected = ensure
        await self.engine.start_many_forward(
            ['a'], 'https://t.me/c/1234567890/77', 3600, 0, 0, 'forward', 'Forward')
        await asyncio.wait_for(sent.wait(), 1)
        gc.collect()
        self.assertTrue(refs)
        self.assertTrue(all(reference() is None for reference in refs))

    async def test_unknown_article_type_in_forward_response_is_treated_as_delivered(self):
        from telethon.errors import TypeNotFoundError
        attempts = []

        async def ensure(key):
            async def dialogs():
                return [types.SimpleNamespace(id=1, is_group=True),
                        types.SimpleNamespace(id=2, is_group=True)]

            async def forward(ident, message_id, **kwargs):
                attempts.append((ident, message_id, kwargs.get('from_peer')))
                raise TypeNotFoundError(0x12345678, b'article')

            return types.SimpleNamespace(get_dialogs=dialogs, forward_messages=forward)

        self.engine.ensure_connected = ensure
        await self.engine.start_many_forward(
            ['a'], 'https://t.me/c/1234567890/77', 0, 0, 0, 'forward', 'Forward')
        await asyncio.gather(*self.engine.jobs.values())
        self.assertEqual([row[0] for row in attempts], [1, 2])
        self.assertTrue(any('0x12345678' in str(value) for _, value in self.events))

    async def test_selected_accounts_join_public_channel(self):
        joined = []

        class Client:
            async def get_entity(self, username):
                return types.SimpleNamespace(username=username, broadcast=True, megagroup=False)

            async def __call__(self, request):
                joined.append(request.channel.username)

        async def ensure(key):
            return Client()

        self.engine.ensure_connected = ensure
        await self.engine.join_public_channel_many(['a', 'b'], 'example_channel')
        self.assertEqual(joined, ['example_channel', 'example_channel'])
        self.assertTrue(any('успешно: 2' in str(value) for _, value in self.events))

    async def test_leave_failed_chat_removes_the_matching_dialog(self):
        deleted = []
        entity = object()

        class Client:
            async def get_dialogs(self):
                return [types.SimpleNamespace(id=-100123, entity=entity, name='Blocked chat')]

            async def delete_dialog(self, selected):
                deleted.append(selected)

        async def ensure(key):
            return Client()

        self.engine.ensure_connected = ensure
        record = dict(account='a', chat_id=-100123, title='Blocked chat', reason='нет права')
        await self.engine.leave_failed_chats([record])
        self.assertEqual(deleted, [entity])
        self.assertTrue(any(kind == 'failed_chat_left' for kind, _ in self.events))


class SourceMessageReferenceTests(unittest.TestCase):
    def test_private_and_public_post_links(self):
        self.assertEqual(app.source_message_reference('https://t.me/c/1234567890/77?single'),
                         (-1001234567890, 77))
        self.assertEqual(app.source_message_reference('t.me/example_channel/12'),
                         ('example_channel', 12))

    def test_invalid_link_is_rejected(self):
        for value in ('', 'https://example.com/c/123/4', 'https://t.me/c/wrong/4'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                app.source_message_reference(value)

    def test_public_channel_link(self):
        self.assertEqual(app.public_channel_username('https://t.me/example_channel'),
                         'example_channel')
        self.assertEqual(app.public_channel_username('@example_channel'),
                         'example_channel')
        for value in ('', 'https://t.me/+private', 'https://example.com/channel'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                app.public_channel_username(value)


class OfflineEngine:
    def __init__(self, emit):
        self.clients = {}
        self.jobs = {}
        self.job_directions = {}
        self.parser_base = app.BASE
        self.likes_state = {}

    async def parser_snapshot(self):
        pass

    def submit(self, coroutine):
        coroutine.close()


class DirectionUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.folder = Path(self.temp.name)
        self.base_patch = patch('app.BASE', self.folder)
        self.base_patch.start()
        self.engine_patch = patch('app.Engine', OfflineEngine)
        self.engine_patch.start()
        self.dialog_patches = [patch('app.messagebox.' + name,
                                     side_effect=lambda *args, **kwargs: self.fail('Unexpected dialog: ' + str(args)))
                               for name in ('showerror', 'showinfo')]
        for dialog_patch in self.dialog_patches:
            dialog_patch.start()
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            for timer in window.tk.splitlist(window.tk.call('after', 'info')):
                window.after_cancel(timer)
            window.destroy()
        self.engine_patch.stop()
        for dialog_patch in self.dialog_patches:
            dialog_patch.stop()
        self.base_patch.stop()
        self.temp.cleanup()

    def window(self):
        window = app.App()
        window.withdraw()
        self.windows.append(window)
        return window

    def add_accounts(self, window):
        for key in ('a', 'b', 'c'):
            window.rows[key] = window.accounts.insert('', 'end', text=key, values=(key, '', 'OK'))
            window.engine.clients[key] = object()
        window.restore_direction_accounts()

    def test_switch_save_restart_and_select_exact_number_of_free_accounts(self):
        (self.folder / 'texts.json').write_text(json.dumps({'texts': ['Legacy'], 'mode': 'random'}), 'utf-8')
        window = self.window()
        self.add_accounts(window)
        window.account_count.set('2')
        window.select_direction_accounts()
        first_id = window.current_direction
        window.gap.delete(0, 'end')
        window.gap.insert(0, '19')
        with patch('app.simpledialog.askstring', return_value='Second'):
            window.add_direction()
        second_id = window.current_direction
        self.assertEqual(window.text_values(), [])
        self.assertEqual(window.selected(), [])
        window.account_count.set('1')
        window.select_direction_accounts()
        self.assertEqual(window.selected(), ['c'])
        window.editors[0].insert('1.0', 'Second text')
        window.delivery_mode.current(1)
        window.source_message.insert(0, 'https://t.me/c/1234567890/77')
        window.save_texts()
        window.direction_choice.current(0)
        window.change_direction()
        self.assertEqual(window.current_direction, first_id)
        self.assertEqual(window.selected(), ['a', 'b'])
        self.assertEqual(window.text_values(), ['Legacy'])
        self.assertEqual(window.mode.current(), 1)
        self.assertEqual(window.delivery_mode.current(), 0)
        self.assertEqual(window.source_message.get(), '')
        self.assertEqual(window.gap.get(), '19')
        window.direction_choice.current(1)
        window.change_direction()
        restarted = self.window()
        self.add_accounts(restarted)
        self.assertEqual(restarted.current_direction, second_id)
        self.assertEqual(restarted.selected(), ['c'])
        self.assertEqual(restarted.text_values(), ['Second text'])
        self.assertEqual(restarted.delivery_mode.current(), 1)
        self.assertEqual(restarted.source_message.get(), 'https://t.me/c/1234567890/77')
        self.assertEqual(json.loads((self.folder / 'texts.json').read_text('utf-8'))['texts'], ['Legacy'])

    def test_corrupt_saved_directions_are_not_overwritten(self):
        target = self.folder / 'directions.json'
        target.write_text('{broken', 'utf-8')
        with patch('app.messagebox.showerror') as error:
            window = self.window()
        error.assert_called_once()
        self.assertFalse(window.save_directions())
        self.assertEqual(target.read_text('utf-8'), '{broken')

    def test_dashboard_is_first_and_tracks_realtime_activity(self):
        window = self.window()
        self.assertEqual(window.pages.tab(0, 'text'), 'Главная')
        window.observe_dashboard_event('dashboard_activity', ('mailing', 'sent', 'a'))
        window.observe_dashboard_event('dashboard_activity', ('mailing', 'error', 'a'))
        window.observe_dashboard_event('dashboard_activity', ('joins', 'joined', 'a'))
        window.observe_dashboard_event('parser_row', {'row': {}})
        window.observe_dashboard_event('log', 'Проверочное событие')
        self.assertEqual(window.dashboard_counts,
                         {'sent': 1, 'errors': 1, 'skipped': 0, 'joins': 1})
        self.assertFalse(hasattr(window, 'dashboard_canvas'))
        self.assertFalse(hasattr(window, 'dashboard_activity'))
        self.assertFalse(hasattr(window, 'dashboard_mode_buttons'))
        self.assertEqual(len(window.dashboard_events.get_children()), 1)
        window.persist_log_event('log', 'Проверка сохранения')
        window.flush_run_log()
        self.assertIn('Проверка сохранения', window.log_path.read_text('utf-8'))

    def test_delivery_successes_do_not_flood_dashboard_events(self):
        window = self.window()
        window.observe_dashboard_event('log', 'account.session: переслано в -100123')
        window.observe_dashboard_event('log', 'account.session: отправлено в -100456')
        self.assertEqual(len(window.dashboard_events.get_children()), 0)
        window.observe_dashboard_event('log', 'account.session: ошибка -100789')
        self.assertEqual(len(window.dashboard_events.get_children()), 1)

    def test_dashboard_activity_does_not_repaint_the_hologram(self):
        window = self.window()
        with patch.object(window.dashboard_hologram, 'draw') as draw:
            for _ in range(1000):
                window.observe_dashboard_event('dashboard_activity', ('mailing', 'sent', 'a'))
            draw.assert_not_called()
        self.assertEqual(window.dashboard_counts['sent'], 1000)

    def test_decoration_preference_survives_restart_without_changing_other_settings(self):
        window = self.window()
        window.decorations_enabled.set(False)
        window.change_decorations()
        window.sphere_animation_enabled.set(False)
        window.change_sphere_animation()
        self.assertFalse(window.matrix_theme.decorations)
        restored = self.window()
        self.assertFalse(restored.decorations_enabled.get())
        self.assertFalse(restored.sphere_animation_enabled.get())
        self.assertFalse(restored.dashboard_hologram.animation_enabled)
        self.assertEqual(restored.matrix_theme.root.tk.call('ttk::style', 'theme', 'use'), 'matrix')
        self.assertEqual(restored.parser_keywords.cget('background'), app.COLORS['field'])

    def test_failed_chat_ui_refresh_is_batched(self):
        window = self.window()
        with patch.object(window, 'update_failed_chats_window_rows') as refresh:
            for chat_id in range(100):
                window.record_failed_chat(dict(
                    account='a', chat_id=chat_id, title=str(chat_id), reason='нет права'))
            self.assertTrue(window.failed_chats_ui_pending)
            refresh.assert_not_called()
            window.finish_failed_chats_ui_refresh()
            refresh.assert_called_once()
            self.assertEqual(len(refresh.call_args.args[0]), 100)

    def test_permanent_delivery_restrictions_are_classified_as_skips(self):
        cases = [
            type('ChatWriteForbiddenError', (Exception,), {})('forbidden'),
            Exception('RPCError 400: TOPIC_CLOSED'),
            Exception('RPCError 403: ALLOW_PAYMENT_REQUIRED_23'),
        ]
        for exc in cases:
            with self.subTest(error=str(exc)):
                self.assertEqual(app.delivery_failure(exc)[0], 'skipped')
        self.assertEqual(app.delivery_failure(RuntimeError('temporary'))[0], 'error')

    def test_failed_chats_are_saved_and_restored(self):
        window = self.window()
        record = dict(account='a', chat_id=-100123, title='Blocked chat', reason='нет права')
        window.record_failed_chat(record)
        self.assertTrue(window.save_failed_chats())
        restored = self.window()
        key = restored.failed_chat_key(record)
        self.assertIn(key, restored.failed_chats)
        self.assertEqual(restored.failed_chats[key]['reason'], 'нет права')

    def test_likes_settings_selection_and_verification_label(self):
        window = self.window()
        self.add_accounts(window)
        window.refresh_likes_accounts()
        window.likes_accounts.selection_set([window.likes_rows['b']])
        window.likes_selection_changed()
        for field, value in [('like', 'LIKE'), ('resume', 'BROWSE')]:
            window.likes_fields[field].delete(0, 'end')
            window.likes_fields[field].insert(0, value)
        self.assertTrue(window.likes_save_settings())
        window.likes_event('likes_status', ('b', 'Нужна верификация', 7, 0))
        self.assertEqual(window.likes_accounts.item(window.likes_rows['b'], 'tags'), ('attention',))
        restored = self.window()
        self.add_accounts(restored)
        restored.refresh_likes_accounts()
        self.assertEqual(restored.likes_selected(), ['b'])
        self.assertEqual(restored.likes_settings()['like'], 'LIKE')


if __name__ == '__main__':
    unittest.main()
