import asyncio
import concurrent.futures
import json
import os
from pathlib import Path
import queue
import random
import re
import sys
import threading
import time
import traceback
import uuid
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
from urllib.parse import urlsplit
from user_parser import ParserEngine
from parser_ui import ParserUI
from bot_likes import LikesEngine, LikesUI
from ui_theme import COLORS, MatrixTheme, DecorPanel, GifSphereCanvas, ScrollablePage
from runtime_support import EventBuffer, RunLogWriter, RuntimeDiagnostics
from update_control import UpdateControl
from app_paths import data_directory, resource_directory
from app_updates import start_update_helper
from updates_ui import UpdatesUI

BASE = data_directory()
RESOURCE_BASE = resource_directory()
BASE.mkdir(parents=True, exist_ok=True)
for name in ('accounts',):
    (BASE / name).mkdir(exist_ok=True)


def discover(root):
    return sorted(root.rglob('*.session'))


def session_settings(path, fallback_id='', fallback_hash=''):
    metadata = path.with_suffix('.json')
    data = {}
    if metadata.exists():
        try:
            data = json.loads(metadata.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            raise ValueError('Соседний JSON повреждён или недоступен') from None
        if not isinstance(data, dict):
            raise ValueError('JSON должен содержать объект с настройками')
    aid = data.get('app_id', data.get('api_id', fallback_id))
    ahash = data.get('app_hash', data.get('api_hash', fallback_hash))
    try:
        if isinstance(aid, bool) or int(aid) <= 0:
            raise ValueError()
        aid = int(aid)
    except (ValueError, TypeError):
        raise ValueError('Нет корректного app_id/api_id в JSON или общих настройках') from None
    if not isinstance(ahash, str) or len(ahash.strip()) != 32 or any(c not in '0123456789abcdefABCDEF' for c in ahash.strip()):
        raise ValueError('Нет корректного app_hash/api_hash в JSON или общих настройках')
    options = {'flood_sleep_threshold': 0, 'receive_updates': False, 'entity_cache_limit': 2000}
    for source, target in [('device', 'device_model'), ('sdk', 'system_version'),
                           ('app_version', 'app_version'), ('lang_pack', 'lang_code'),
                           ('system_lang_pack', 'system_lang_code')]:
        value = data.get(source)
        if isinstance(value, str) and value.strip():
            options[target] = value.strip()
    for field in ('lang_code', 'system_lang_code'):
        if isinstance(data.get(field), str) and data[field].strip():
            options[field] = data[field].strip()
    if isinstance(data.get('ipv6'), bool):
        options['use_ipv6'] = data['ipv6']
    return aid, ahash.strip(), options


def validate_session(path):
    import sqlite3
    try:
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            columns = {row[1] for row in db.execute('PRAGMA table_info(sessions)')}
            if not {'dc_id', 'server_address', 'port', 'auth_key'}.issubset(columns):
                raise ValueError('Нужна SQLite-сессия Telethon. Формат этой сессии не поддерживается.')
    except sqlite3.Error:
        raise ValueError('Файл .session повреждён или не является SQLite-сессией Telethon') from None


def shortcut_action(keycode, keysym, windows=False):
    if windows:
        physical = {86: 'paste', 67: 'copy', 88: 'cut', 65: 'all', 90: 'undo'}
        if keycode in physical:
            return physical[keycode]
    return {'v': 'paste', 'c': 'copy', 'x': 'cut', 'a': 'all', 'z': 'undo',
            'Cyrillic_em': 'paste', 'Cyrillic_es': 'copy', 'Cyrillic_che': 'cut',
            'Cyrillic_ef': 'all', 'Cyrillic_ya': 'undo'}.get(keysym)


def addlist_slugs(text):
    slugs = []
    for line_number, line in enumerate(text.splitlines(), 1):
        link = line.strip()
        if not link:
            continue
        url = urlsplit(link if '://' in link else 'https://' + link)
        parts = url.path.strip('/').split('/')
        if (url.scheme not in ('http', 'https') or url.hostname not in
                ('t.me', 'telegram.me') or len(parts) != 2 or
                parts[0] != 'addlist' or not re.fullmatch(r'[A-Za-z0-9_-]+', parts[1])):
            raise ValueError(f'Неверная ссылка на папку: строка {line_number}')
        if parts[1] not in slugs:
            slugs.append(parts[1])
    if not slugs:
        raise ValueError('Вставьте хотя бы одну ссылку t.me/addlist/...')
    return slugs


def source_message_reference(value):
    """Parse a Telegram post link into a Telethon entity reference and message id."""
    link = value.strip()
    if not link:
        raise ValueError('Укажите ссылку на исходное сообщение')
    if '://' not in link:
        link = 'https://' + link
    url = urlsplit(link)
    if url.scheme not in ('http', 'https') or (url.hostname or '').lower() not in ('t.me', 'telegram.me'):
        raise ValueError('Нужна ссылка вида https://t.me/c/1234567890/123')
    parts = [part for part in url.path.split('/') if part]
    try:
        if len(parts) >= 3 and parts[0].lower() == 'c':
            if not parts[1].isdigit() or not parts[-1].isdigit():
                raise ValueError()
            entity = int('-100' + parts[1])
            message_id = int(parts[-1])
        else:
            if parts and parts[0].lower() == 's':
                parts = parts[1:]
            if (len(parts) < 2 or not re.fullmatch(r'[A-Za-z0-9_]{5,}', parts[0])
                    or not parts[-1].isdigit()):
                raise ValueError()
            entity = parts[0]
            message_id = int(parts[-1])
        if message_id <= 0:
            raise ValueError()
    except (ValueError, IndexError):
        raise ValueError('Неверная ссылка на сообщение Telegram') from None
    return entity, message_id


def public_channel_username(value):
    """Return a username from a public t.me channel link."""
    link = value.strip()
    if not link:
        raise ValueError('Укажите ссылку на открытый канал')
    if link.startswith('@'):
        username = link[1:]
    else:
        if '://' not in link:
            link = 'https://' + link
        url = urlsplit(link)
        if url.scheme not in ('http', 'https') or (url.hostname or '').lower() not in ('t.me', 'telegram.me'):
            raise ValueError('Нужна публичная ссылка вида https://t.me/channel_name')
        parts = [part for part in url.path.split('/') if part]
        if len(parts) != 1 or parts[0].startswith('+') or parts[0].lower() in ('joinchat', 'c', 's'):
            raise ValueError('Нужна публичная ссылка вида https://t.me/channel_name')
        username = parts[0]
    if not re.fullmatch(r'[A-Za-z0-9_]{5,32}', username):
        raise ValueError('Некорректное публичное имя канала')
    return username


def delivery_failure(exc):
    """Classify permanent Telegram delivery restrictions separately from failures."""
    name = type(exc).__name__
    text = str(exc)
    upper = text.upper()
    permanent = {
        'ChatWriteForbiddenError': 'нет права писать в чат',
        'ChatRestrictedError': 'чат ограничен для отправки',
        'ChannelPrivateError': 'чат закрыт или аккаунт больше не имеет доступа',
        'UserBannedInChannelError': 'аккаунт заблокирован в чате',
        'TopicClosedError': 'топик закрыт',
    }
    if name in permanent:
        return 'skipped', permanent[name]
    if 'TOPIC_CLOSED' in upper:
        return 'skipped', 'основной топик форума закрыт'
    if 'ALLOW_PAYMENT_REQUIRED' in upper:
        return 'skipped', 'чат требует платную отправку; оплата Stars отключена'
    if 'CHAT_WRITE_FORBIDDEN' in upper or "CAN'T WRITE IN THIS CHAT" in upper:
        return 'skipped', 'нет права писать в чат'
    return 'error', f'{name}: {text}'


def dialog_skip_reason(dialog):
    entity = getattr(dialog, 'entity', None)
    if getattr(entity, 'deactivated', False) or getattr(entity, 'left', False):
        return 'чат удалён или аккаунт вышел из него'
    rights = getattr(entity, 'default_banned_rights', None)
    if (rights and getattr(rights, 'send_messages', False)
            and not getattr(entity, 'creator', False)
            and not getattr(entity, 'admin_rights', None)):
        return 'в чате по умолчанию запрещена отправка сообщений'
    return None


def dialog_title(dialog):
    return (getattr(dialog, 'name', None)
            or getattr(getattr(dialog, 'entity', None), 'title', None)
            or str(dialog.id))


def is_high_volume_delivery_log(value):
    """Delivery successes stay in the run log, but not in the dashboard event table."""
    text = str(value).casefold()
    return ': переслано в ' in text or ': отправлено в ' in text


class TextRotation:
    def __init__(self, texts, mode):
        self.texts = tuple(texts)
        if not self.texts or any(not t.strip() for t in self.texts):
            raise ValueError('Добавьте непустые тексты')
        self.mode = mode
        self.order = []

    def next(self):
        if not self.order:
            self.order = list(range(len(self.texts)))
            if self.mode == 'random':
                random.shuffle(self.order)
        return self.texts[self.order.pop(0)]


class Engine(UpdateControl, LikesEngine, ParserEngine):
    def __init__(self, emit):
        self.emit = emit
        self.clients = {}
        self.jobs = {}
        self.job_directions = {}
        self.connection_monitors = {}
        self.connection_locks = {}
        self.reconnect_gate = asyncio.Semaphore(4)
        self.closing = False
        self.search_task = None
        self.parser_base = BASE
        self.init_update_control()
        self.init_parser()
        self.init_likes()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def submit(self, coroutine):
        f = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        def done(result):
            try:
                result.result()
            except (asyncio.CancelledError, concurrent.futures.CancelledError):
                pass
            except Exception as exc:
                self.emit('log', f'{type(exc).__name__}: {exc}')
        f.add_done_callback(done)
        return f

    async def register(self, key, client):
        if key in self.clients:
            await client.disconnect()
            return
        try:
            await asyncio.wait_for(client.connect(), 30)
            if not await client.is_user_authorized():
                raise ValueError('Сессия не авторизована. Войдите заново в Telegram; файл не заменяет вход.')
            me = await client.get_me()
            if any(uid == me.id for _, uid in self.clients.values()):
                self.emit('log', f'{key}: этот аккаунт уже подключён')
                await client.disconnect()
                return
            self.clients[key] = (client, me.id)
            self.connection_locks[key] = asyncio.Lock()
            self.start_connection_monitor(key, client)
            self.emit('account', (key, f'{me.first_name or ""} @{me.username or "—"}', 'Подключён'))
        except BaseException:
            await client.disconnect()
            raise

    def start_connection_monitor(self, key, client):
        monitor = self.connection_monitors.get(key)
        if not self.closing and (monitor is None or monitor.done()):
            self.connection_monitors[key] = asyncio.create_task(self.monitor_connection(key, client))

    async def ensure_connected(self, key):
        if self.closing or key not in self.clients:
            raise ValueError('Аккаунт не подключён')
        client = self.clients[key][0]
        if client.is_connected():
            self.start_connection_monitor(key, client)
            return client
        async with self.connection_locks[key]:
            if self.closing or self.clients.get(key, (None,))[0] is not client:
                raise ValueError('Аккаунт отключён')
            if not client.is_connected():
                self.emit('status', (key, 'Переподключение'))
                async with self.reconnect_gate:
                    await asyncio.wait_for(client.connect(), 30)
                me = await client.get_me()
                if me is None or me.id != self.clients[key][1]:
                    await client.disconnect()
                    raise ValueError('Сессия больше не авторизована или принадлежит другому аккаунту. Войдите в Telegram заново.')
                self.emit('status', (key, 'Подключён'))
        self.start_connection_monitor(key, client)
        return client

    async def monitor_connection(self, key, client):
        while not self.closing and self.clients.get(key, (None,))[0] is client:
            try:
                await client.disconnected
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.emit('log', f'{key}: соединение потеряно ({type(exc).__name__})')
            if self.closing or self.clients.get(key, (None,))[0] is not client:
                return
            if client.is_connected():
                await asyncio.sleep(.5)
                continue
            self.emit('status', (key, 'Нет связи'))
            delay = 5
            while not self.closing and self.clients.get(key, (None,))[0] is client:
                try:
                    await self.ensure_connected(key)
                except asyncio.CancelledError:
                    raise
                except ValueError as exc:
                    self.emit('status', (key, 'Нужна авторизация'))
                    self.emit('log', f'{key}: {exc}')
                    return
                except Exception as exc:
                    self.emit('status', (key, f'Нет связи; повтор через {delay} сек.'))
                    self.emit('log', f'{key}: переподключение: {type(exc).__name__}')
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60)
                else:
                    self.emit('log', f'{key}: соединение восстановлено')
                    break

    async def scan(self, api_id, api_hash):
        sessions = discover(BASE / 'accounts')
        self.emit('log', f'Найдено .session: {len(sessions)}')
        from telegram_runtime import AccountClient as TelegramClient
        for path in sessions:
            key = str(path.relative_to(BASE / 'accounts'))
            try:
                if key in self.clients:
                    existing = self.clients[key][0]
                    if existing.is_connected():
                        continue
                    monitor = self.connection_monitors.pop(key, None)
                    if monitor:
                        monitor.cancel()
                        await asyncio.gather(monitor, return_exceptions=True)
                    if self.parser_account_key == key:
                        await self.stop_parser()
                    await self.stop_likes([key])
                    await self.stop([key])
                    await existing.disconnect()
                    self.clients.pop(key, None)
                    self.connection_locks.pop(key, None)
                    self.parser_dialogs.pop(key, None)
                    getattr(self, 'update_users', {}).pop(key, None)
                    getattr(self, 'update_locks', {}).pop(key, None)
                aid, ahash, options = session_settings(path, api_id, api_hash)
                validate_session(path)
                await self.register(key, TelegramClient(str(path), aid, ahash, **options))
            except Exception as exc:
                self.emit('account', (key, '', f'Ошибка: {type(exc).__name__}'))
                # Do not print arbitrary exception contents: metadata can contain secrets.
                detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                self.emit('log', f'{key}: {detail}')

    async def convert_tdata(self, source, passcode):
        try:
            from converter import run_converter
            await asyncio.to_thread(run_converter, source, BASE / 'accounts', passcode,
                                    lambda line: self.emit('convert_log', line))
        except Exception as exc:
            detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            self.emit('convert_log', f'Ошибка: {detail}')
        finally:
            self.emit('convert_done', None)

    async def start(self, key, texts, mode, gap, interval, launch_delay=0, direction_id=None):
        if key not in self.clients:
            self.emit('log', f'{key}: аккаунт не подключён')
            return False
        if key in self.jobs and not self.jobs[key].done():
            self.emit('log', f'{key}: уже запущен')
            return False
        self.jobs[key] = asyncio.create_task(self.send(key, tuple(texts), mode, gap, interval, launch_delay))
        self.job_directions[key] = direction_id
        self.emit('direction_job', (key, direction_id))
        def finished(task):
            if self.jobs.get(key) is task:
                self.job_directions.pop(key, None)
                self.emit('direction_job', (key, None))
            if not task.cancelled() and task.exception():
                self.emit('log', f'{key}: {type(task.exception()).__name__}')
        self.jobs[key].add_done_callback(finished)
        return True

    async def start_many(self, keys, texts, mode, gap, interval, account_gap,
                         direction_id=None, direction_name=''):
        # Check the complete batch before scheduling any account.
        busy = [key for key in keys if key in self.jobs and not self.jobs[key].done()]
        missing = [key for key in keys if key not in self.clients]
        if busy or missing:
            detail = ('Уже работают: ' + ', '.join(busy)) if busy else ('Не подключены: ' + ', '.join(missing))
            self.emit('direction_error', f'{direction_name}: {detail}. Запуск направления отменён.')
            return
        scheduled = 0
        for key in keys:
            if await self.start(key, texts, mode, gap, interval, scheduled * account_gap, direction_id):
                scheduled += 1
        self.emit('log', f'{direction_name or "Рассылка"}: запущено аккаунтов: {scheduled}, текстов: {len(texts)}')

    async def start_forward(self, key, source_link, gap, interval, launch_delay=0, direction_id=None):
        if key not in self.clients:
            self.emit('log', f'{key}: аккаунт не подключён')
            return False
        if key in self.jobs and not self.jobs[key].done():
            self.emit('log', f'{key}: уже запущен')
            return False
        self.jobs[key] = asyncio.create_task(
            self.send_forward(key, source_link, gap, interval, launch_delay))
        self.job_directions[key] = direction_id
        self.emit('direction_job', (key, direction_id))

        def finished(task):
            if self.jobs.get(key) is task:
                self.job_directions.pop(key, None)
                self.emit('direction_job', (key, None))
            if not task.cancelled() and task.exception():
                self.emit('log', f'{key}: {type(task.exception()).__name__}')

        self.jobs[key].add_done_callback(finished)
        return True

    async def start_many_forward(self, keys, source_link, gap, interval, account_gap,
                                 direction_id=None, direction_name=''):
        busy = [key for key in keys if key in self.jobs and not self.jobs[key].done()]
        missing = [key for key in keys if key not in self.clients]
        if busy or missing:
            detail = ('Уже работают: ' + ', '.join(busy)) if busy else ('Не подключены: ' + ', '.join(missing))
            self.emit('direction_error', f'{direction_name}: {detail}. Запуск направления отменён.')
            return
        try:
            source_message_reference(source_link)
        except ValueError as exc:
            self.emit('direction_error', f'{direction_name}: {exc}. Запуск направления отменён.')
            return
        scheduled = 0
        for key in keys:
            if await self.start_forward(key, source_link, gap, interval,
                                        scheduled * account_gap, direction_id):
                scheduled += 1
        self.emit('log', f'{direction_name or "Рассылка"}: запущено аккаунтов: {scheduled}; '
                         'режим: пересылка сообщения')

    async def stop_direction(self, direction_id):
        await self.stop([key for key, owner in self.job_directions.items() if owner == direction_id])

    async def send(self, key, texts, mode, gap, interval, launch_delay=0):
        from telethon.errors import FloodWaitError
        rotation = TextRotation(texts, mode)
        blocked = set()
        try:
            if launch_delay:
                self.emit('status', (key, f'Запуск через {launch_delay:g} сек.'))
                await asyncio.sleep(launch_delay)
            client = await self.ensure_connected(key)
            self.emit('status', (key, 'Загрузка групп'))
            while True:
                while True:
                    try:
                        dialogs = await asyncio.wait_for(client.get_dialogs(), 60)
                        break
                    except FloodWaitError as exc:
                        self.emit('status', (key, f'Ожидание {exc.seconds} сек.'))
                        await asyncio.sleep(exc.seconds + 1)
                ids = []
                titles = {}
                for dialog in dialogs:
                    if not dialog.is_group or dialog.id in blocked:
                        continue
                    titles[dialog.id] = dialog_title(dialog)
                    reason = dialog_skip_reason(dialog)
                    if reason:
                        blocked.add(dialog.id)
                        self.emit('log', f'{key}: пропущен {dialog.id}: {reason}')
                        self.emit('failed_chat', dict(account=key, chat_id=dialog.id,
                                                      title=dialog_title(dialog), reason=reason))
                        self.emit('dashboard_activity', ('mailing', 'skipped', key))
                    else:
                        ids.append(dialog.id)
                # Dialog.message may carry media and large web-page previews.
                # Do not retain every chat's last message for an hours-long round.
                dialogs = dialog = None
                self.emit('log', f'{key}: групп в круге: {len(ids)}; постоянных пропусков: {len(blocked)}')
                if not ids:
                    return
                self.emit('status', (key, 'Отправка'))
                for number, ident in enumerate(ids):
                    text = rotation.next()
                    while True:
                        try:
                            await asyncio.wait_for(client.send_message(ident, text, parse_mode=None), 60)
                            self.emit('log', f'{key}: отправлено в {ident}')
                            self.emit('dashboard_activity', ('mailing', 'sent', key))
                            break
                        except FloodWaitError as exc:
                            self.emit('status', (key, f'Ожидание {exc.seconds} сек.'))
                            await asyncio.sleep(exc.seconds + 1)
                            self.emit('status', (key, 'Отправка'))
                        except asyncio.TimeoutError:
                            self.emit('log', f'{key}: таймаут {ident}; доставка неизвестна, повтор пропущен')
                            self.emit('dashboard_activity', ('mailing', 'error', key))
                            break
                        except Exception as exc:
                            outcome, detail = delivery_failure(exc)
                            if outcome == 'skipped':
                                blocked.add(ident)
                                self.emit('log', f'{key}: пропущен {ident}: {detail}')
                                title = titles.get(ident, str(ident))
                                self.emit('failed_chat', dict(account=key, chat_id=ident,
                                                              title=title, reason=detail))
                            else:
                                self.emit('log', f'{key}: ошибка {ident}: {detail}')
                            self.emit('dashboard_activity', ('mailing', outcome, key))
                            break
                    if number < len(ids) - 1:
                        await asyncio.sleep(gap)
                if not interval:
                    break
                self.emit('status', (key, 'Ожидание следующего круга'))
                await asyncio.sleep(interval)
                self.emit('status', (key, 'Отправка'))
        finally:
            self.emit('status', (key, 'Остановлен'))

    async def send_forward(self, key, source_link, gap, interval, launch_delay=0):
        from telethon.errors import FloodWaitError, TypeNotFoundError
        blocked = set()
        try:
            if launch_delay:
                self.emit('status', (key, f'Запуск через {launch_delay:g} сек.'))
                await asyncio.sleep(launch_delay)
            client = await self.ensure_connected(key)
            source, message_id = source_message_reference(source_link)
            self.emit('status', (key, 'Загрузка канала'))
            while True:
                try:
                    dialogs = await asyncio.wait_for(client.get_dialogs(), 60)
                    break
                except FloodWaitError as exc:
                    self.emit('status', (key, f'Ожидание {exc.seconds} сек.'))
                    await asyncio.sleep(exc.seconds + 1)
            source_entity = next((getattr(dialog, 'entity', source)
                                  for dialog in dialogs if dialog.id == source), source)
            source_chat_id = next((dialog.id for dialog in dialogs
                                   if getattr(dialog, 'entity', None) is source_entity),
                                  source if isinstance(source, int) else None)
            if getattr(source_entity, 'access_hash', None) is not None:
                from telethon import utils
                source_entity = utils.get_input_peer(source_entity)
            self.emit('status', (key, 'Загрузка групп'))
            while True:
                ids = []
                titles = {}
                for dialog in dialogs:
                    if not dialog.is_group or dialog.id == source_chat_id or dialog.id in blocked:
                        continue
                    titles[dialog.id] = dialog_title(dialog)
                    reason = dialog_skip_reason(dialog)
                    if reason:
                        blocked.add(dialog.id)
                        self.emit('log', f'{key}: пропущен {dialog.id}: {reason}')
                        self.emit('failed_chat', dict(account=key, chat_id=dialog.id,
                                                      title=dialog_title(dialog), reason=reason))
                        self.emit('dashboard_activity', ('mailing', 'skipped', key))
                    else:
                        ids.append(dialog.id)
                dialogs = dialog = None
                self.emit('log', f'{key}: групп в круге: {len(ids)}; постоянных пропусков: {len(blocked)}')
                if not ids:
                    return
                self.emit('status', (key, 'Пересылка'))
                for number, ident in enumerate(ids):
                    while True:
                        try:
                            await asyncio.wait_for(
                                client.forward_messages(ident, message_id, from_peer=source_entity), 60)
                            self.emit('log', f'{key}: переслано в {ident}')
                            self.emit('dashboard_activity', ('mailing', 'sent', key))
                            break
                        except FloodWaitError as exc:
                            self.emit('status', (key, f'Ожидание {exc.seconds} сек.'))
                            await asyncio.sleep(exc.seconds + 1)
                            self.emit('status', (key, 'Пересылка'))
                        except TypeNotFoundError as exc:
                            # The forward request was processed; only Telegram's response
                            # contains a newer object unknown to this Telethon build.
                            constructor = getattr(exc, 'invalid_constructor_id', 0)
                            self.emit('log', f'{key}: переслано в {ident}; ответ Telegram содержит '
                                             f'новый тип 0x{constructor:08x}')
                            self.emit('dashboard_activity', ('mailing', 'sent', key))
                            break
                        except asyncio.TimeoutError:
                            self.emit('log', f'{key}: таймаут {ident}; доставка неизвестна, повтор пропущен')
                            self.emit('dashboard_activity', ('mailing', 'error', key))
                            break
                        except Exception as exc:
                            if type(exc).__name__ == 'ChatForwardsRestrictedError':
                                self.emit('log', f'{key}: в исходном канале запрещена пересылка сообщений')
                                self.emit('dashboard_activity', ('mailing', 'error', key))
                                return
                            outcome, detail = delivery_failure(exc)
                            if outcome == 'skipped':
                                blocked.add(ident)
                                self.emit('log', f'{key}: пропущен {ident}: {detail}')
                                title = titles.get(ident, str(ident))
                                self.emit('failed_chat', dict(account=key, chat_id=ident,
                                                              title=title, reason=detail))
                            else:
                                self.emit('log', f'{key}: ошибка {ident}: {detail}')
                            self.emit('dashboard_activity', ('mailing', outcome, key))
                            break
                    if number < len(ids) - 1:
                        await asyncio.sleep(gap)
                if not interval:
                    break
                self.emit('status', (key, 'Ожидание следующего круга'))
                await asyncio.sleep(interval)
                while True:
                    try:
                        dialogs = await asyncio.wait_for(client.get_dialogs(), 60)
                        break
                    except FloodWaitError as exc:
                        self.emit('status', (key, f'Ожидание {exc.seconds} сек.'))
                        await asyncio.sleep(exc.seconds + 1)
        except Exception as exc:
            if isinstance(exc, TypeNotFoundError):
                constructor = getattr(exc, 'invalid_constructor_id', 0)
                detail = f'неизвестный тип Telegram 0x{constructor:08x}'
            else:
                detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            self.emit('log', f'{key}: исходное сообщение недоступно: {detail}')
            self.emit('dashboard_activity', ('mailing', 'error', key))
        finally:
            self.emit('status', (key, 'Остановлен'))

    async def stop(self, keys):
        tasks = [self.jobs[k] for k in keys if k in self.jobs and not self.jobs[k].done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def join_public_channel(self, key, username):
        from telethon.errors import FloodWaitError, UserAlreadyParticipantError
        from telethon.tl.functions.channels import JoinChannelRequest
        try:
            client = await self.ensure_connected(key)
            entity = await asyncio.wait_for(client.get_entity(username), 60)
            if not getattr(entity, 'broadcast', False) or getattr(entity, 'megagroup', False):
                raise ValueError('ссылка ведёт не на открытый канал')
            while True:
                try:
                    await asyncio.wait_for(client(JoinChannelRequest(entity)), 60)
                    self.emit('log', f'{key}: вступил в @{username}')
                    self.emit('dashboard_activity', ('joins', 'joined', key))
                    return True
                except UserAlreadyParticipantError:
                    self.emit('log', f'{key}: уже состоит в @{username}')
                    return True
                except FloodWaitError as exc:
                    self.emit('log', f'{key}: вступление в @{username}: ожидание {exc.seconds} сек.')
                    await asyncio.sleep(exc.seconds + 1)
        except asyncio.TimeoutError:
            self.emit('log', f'{key}: вступление в @{username}: таймаут')
        except ValueError as exc:
            self.emit('log', f'{key}: вступление в @{username}: {exc}')
        except Exception as exc:
            self.emit('log', f'{key}: вступление в @{username}: {type(exc).__name__}')
        return False

    async def join_public_channel_many(self, keys, username):
        missing = [key for key in keys if key not in self.clients]
        available = [key for key in keys if key in self.clients]
        for key in missing:
            self.emit('log', f'{key}: аккаунт не подключён; вступление пропущено')
        results = await asyncio.gather(
            *(self.join_public_channel(key, username) for key in available),
            return_exceptions=False)
        success = sum(bool(result) for result in results)
        self.emit('log', f'@{username}: обработано аккаунтов: {len(keys)}; успешно: {success}')

    async def leave_failed_chat(self, record):
        from telethon.errors import FloodWaitError
        key, chat_id = record['account'], int(record['chat_id'])
        try:
            client = await self.ensure_connected(key)
            while True:
                try:
                    dialogs = await asyncio.wait_for(client.get_dialogs(), 60)
                    break
                except FloodWaitError as exc:
                    self.emit('log', f'{key}: выход из {chat_id}: ожидание {exc.seconds} сек.')
                    await asyncio.sleep(exc.seconds + 1)
            dialog = next((item for item in dialogs if item.id == chat_id), None)
            if dialog is None:
                self.emit('log', f'{key}: чат {chat_id} уже отсутствует в диалогах')
                self.emit('failed_chat_left', record)
                return True
            while True:
                try:
                    await asyncio.wait_for(client.delete_dialog(dialog.entity), 60)
                    self.emit('log', f'{key}: вышел из «{dialog_title(dialog)}» ({chat_id})')
                    self.emit('failed_chat_left', record)
                    return True
                except FloodWaitError as exc:
                    self.emit('log', f'{key}: выход из {chat_id}: ожидание {exc.seconds} сек.')
                    await asyncio.sleep(exc.seconds + 1)
        except asyncio.TimeoutError:
            detail = 'таймаут; результат неизвестен'
        except Exception as exc:
            detail = f'{type(exc).__name__}: {exc}'
        self.emit('log', f'{key}: не удалось выйти из {chat_id}: {detail}')
        self.emit('failed_chat_leave_error', dict(record, error=detail))
        return False

    async def leave_failed_chats(self, records):
        groups = {}
        for record in records:
            groups.setdefault(record['account'], []).append(record)

        async def worker(items):
            for item in items:
                await self.leave_failed_chat(item)

        await asyncio.gather(*(worker(items) for items in groups.values()))

    async def account_action(self, key, action, value=None):
        from telethon.tl.functions.account import (
            UpdateUsernameRequest, UpdateProfileRequest, SetPrivacyRequest,
            UpdateNotifySettingsRequest)
        from telethon.tl.types import (
            InputPrivacyKeyStatusTimestamp, InputPrivacyKeyPhoneCall,
            InputPrivacyValueDisallowAll, InputNotifyUsers, InputNotifyChats,
            InputPeerNotifySettings)

        try:
            client = await self.ensure_connected(key)
            if action == 'username':
                await client(UpdateUsernameRequest(value))
            elif action == 'bio':
                await client(UpdateProfileRequest(about=value))
            elif action == 'name':
                await client(UpdateProfileRequest(first_name=value))
            elif action == 'notifications':
                settings = InputPeerNotifySettings(mute_until=2147483647)
                for peer in (InputNotifyUsers(), InputNotifyChats()):
                    await client(UpdateNotifySettingsRequest(peer, settings))
            elif action == 'last_seen':
                await client(SetPrivacyRequest(InputPrivacyKeyStatusTimestamp(),
                                               [InputPrivacyValueDisallowAll()]))
            elif action == 'calls':
                await client(SetPrivacyRequest(InputPrivacyKeyPhoneCall(),
                                               [InputPrivacyValueDisallowAll()]))
            else:
                raise ValueError('Неизвестное действие')
            if action in ('username', 'name'):
                try:
                    me = await client.get_me()
                    self.emit('account_name', (key, f'{me.first_name or ""} @{me.username or "—"}'))
                except Exception:
                    pass
            self.emit('account_action', (key, action, True, 'Готово'))
        except (ConnectionError, asyncio.TimeoutError):
            self.emit('account_action', (key, action, False,
                                         'Связь с Telegram потеряна. Дождитесь переподключения и проверьте настройку перед повтором.'))
        except Exception as exc:
            self.emit('account_action', (key, action, False,
                                         f'{type(exc).__name__}: {exc}'))

    async def add_chatlists(self, key, slugs):
        from telethon import utils
        from telethon.errors import FloodWaitError
        from telethon.tl.functions.chatlists import CheckChatlistInviteRequest, JoinChatlistInviteRequest
        from telethon.tl.types import Channel, Chat, PeerChannel, PeerChat
        from telethon.tl.types.chatlists import ChatlistInviteAlready

        added = 0
        failed = 0
        try:
            for number, slug in enumerate(slugs, 1):
                try:
                    client = await self.ensure_connected(key)
                    invite = await client(CheckChatlistInviteRequest(slug))
                    already_added = isinstance(invite, ChatlistInviteAlready)
                    candidates = invite.missing_peers if already_added else invite.peers
                    if already_added and not candidates:
                        self.emit('account_progress', (key, f'{number}: папка уже добавлена'))
                        continue
                    chats = {(type(chat), chat.id): chat for chat in invite.chats}
                    peers = []
                    for peer in candidates:
                        kind = Channel if isinstance(peer, PeerChannel) else Chat if isinstance(peer, PeerChat) else None
                        entity = chats.get((kind, peer.channel_id if kind is Channel else peer.chat_id)) if kind else None
                        if entity is not None:
                            try:
                                peers.append(utils.get_input_peer(entity))
                            except (TypeError, ValueError):
                                continue
                    if not peers:
                        raise ValueError('В папке нет доступных чатов для вступления')
                    await client(JoinChatlistInviteRequest(slug, peers))
                    added += 1
                    verb = 'обновлена' if already_added else 'добавлена'
                    self.emit('account_progress', (key, f'{number}: папка {verb}, чатов: {len(peers)}'))
                except Exception as exc:
                    failed += 1
                    self.emit('account_progress', (key, f'{number}: ошибка {type(exc).__name__}: {exc}'))
                    if isinstance(exc, FloodWaitError):
                        self.emit('account_progress', (key, f'Лимит Telegram: подождите {exc.seconds} сек.'))
                        break
                    if isinstance(exc, (ConnectionError, asyncio.TimeoutError)):
                        self.emit('account_progress', (key, 'Связь потеряна; оставшиеся ссылки не обработаны.'))
                        break
            self.emit('account_action', (key, 'addlist', failed == 0,
                                         f'Добавлено папок: {added}; ошибок: {failed}'))
        except asyncio.CancelledError:
            self.emit('account_action', (key, 'addlist', False,
                                         f'Остановлено. Добавлено папок: {added}'))
            raise
        except Exception as exc:
            self.emit('account_action', (key, 'addlist', False,
                                         f'{type(exc).__name__}: {exc}'))

    async def search_groups(self, keys, queries):
        from group_search import search_groups
        if self.search_task and not self.search_task.done():
            return
        self.search_task = asyncio.current_task()
        async def worker(key):
            def emit(kind, value):
                if kind == 'search_row':
                    ident, row = value
                    value = (ident, dict(row, accounts=key))
                elif kind == 'search_status':
                    value = f'{key}: {value}'
                self.emit(kind, value)
            try:
                await search_groups(self.clients[key][0], queries, emit)
            except Exception as exc:
                emit('search_status', f'Ошибка: {type(exc).__name__}')
        tasks = [asyncio.create_task(worker(key)) for key in keys if key in self.clients]
        try:
            await asyncio.gather(*tasks)
            self.emit('search_status', 'Поиск завершён на всех выбранных аккаунтах.')
        except asyncio.CancelledError:
            self.emit('search_status', 'Остановлено. Найденные группы сохранены в таблице.')
            raise
        except Exception as exc:
            self.emit('search_status', f'Ошибка: {type(exc).__name__}')
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.emit('search_done', None)

    async def stop_search(self):
        if self.search_task and not self.search_task.done():
            self.search_task.cancel()
            await asyncio.gather(self.search_task, return_exceptions=True)

    async def close(self):
        self.closing = True
        for monitor in self.connection_monitors.values():
            monitor.cancel()
        await asyncio.gather(*self.connection_monitors.values(), return_exceptions=True)
        self.connection_monitors.clear()
        await self.stop_parser()
        await self.stop_search()
        await self.stop_likes()
        await self.stop(list(self.jobs))
        await asyncio.gather(*(c.disconnect() for c, _ in self.clients.values()), return_exceptions=True)


from group_search import SearchUI


class App(UpdatesUI, LikesUI, ParserUI, SearchUI, tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('Telegram Manager')
        width = min(1280, self.winfo_screenwidth() - 60)
        height = min(920, self.winfo_screenheight() - 80)
        self.geometry(f'{width}x{height}')
        self.minsize(min(1060, width), min(800, height))
        self.ui_preferences_blocked = False
        try:
            preferences = json.loads((BASE / 'ui_preferences.json').read_text('utf-8'))
            if not isinstance(preferences, dict):
                raise ValueError()
        except FileNotFoundError:
            preferences = {}
        except (OSError, ValueError):
            preferences = {}
            self.ui_preferences_blocked = True
        self.ui_preferences = preferences
        self.matrix_theme = MatrixTheme(self, preferences.get('decorations', True) is not False)
        self.init_app_updates(BASE)
        self.log_path = None
        self.log_buffer = []
        self.log_flush_due = time.monotonic() + 0.5
        self.setup_run_log()
        self.run_log_writer = RunLogWriter(self.log_path) if self.log_path else None
        self.failed_chats_save_blocked = False
        self.failed_chats_dirty = False
        self.failed_chats_save_due = 0.0
        self.failed_chats_ui_pending = False
        self.failed_chats_ui_dirty_keys = set()
        self.failed_chats = self.load_failed_chats()
        self.failed_chats_window = None
        self.failed_leave_future = None
        self.events = EventBuffer(on_event=self.persist_log_event)
        self.engine = Engine(lambda kind, value: self.events.put((kind, value)))
        self.rows = {}
        self.account_windows = {}
        self.chatlist_futures = {}
        self.channel_join_future = None
        self.busy = False
        self.converting = False
        self.dashboard_counts = {'sent': 0, 'errors': 0, 'skipped': 0, 'joins': 0}
        self.dashboard_started_at = None
        self.dashboard_account_total = len(discover(BASE / 'accounts'))
        self.build_app_header()
        self.pages = ttk.Notebook(self)
        self.pages.pack(fill='both', expand=True)
        dashboard = tk.Frame(self.pages, bg=COLORS['bg'])
        self.dashboard_page = dashboard
        manager_page = ScrollablePage(self.pages)
        self.manager_page = manager_page
        manager = manager_page.body
        converter = ttk.Frame(self.pages, padding=15)
        self.pages.add(dashboard, text='Главная')
        self.pages.add(manager_page, text='Аккаунты и отправка')
        self.pages.add(converter, text='Конвертер tdata')
        self.build_converter(converter)
        parser = ttk.Frame(self.pages, padding=10)
        self.pages.add(parser, text="Поиск групп")
        self.build_search(parser)
        user_parser = ttk.Frame(self.pages, padding=10)
        self.pages.add(user_parser, text='База пользователей')
        self.build_parser(user_parser)
        likes = ttk.Frame(self.pages, padding=10)
        self.pages.add(likes, text='Лайки @leomatchbot')
        self.build_likes(likes)
        self.build_dashboard(dashboard, manager_page, parser, user_parser, likes)
        config = {}
        try:
            config = json.loads((BASE / 'config.json').read_text('utf-8'))
            if not isinstance(config, dict):
                config = {}
        except (OSError, ValueError):
            pass
        top = ttk.Frame(manager, padding=10)
        top.pack(fill='x')
        ttk.Label(top, text='Общий API ID (необязательно)').pack(side='left')
        self.api_id = ttk.Entry(top, width=12)
        self.api_id.insert(0, str(config.get('api_id', '')))
        self.api_id.pack(side='left', padx=5)
        ttk.Label(top, text='API hash').pack(side='left')
        self.api_hash = ttk.Entry(top, width=36, show='*')
        self.api_hash.insert(0, config.get('api_hash', ''))
        self.api_hash.pack(side='left', padx=5)
        self.scan_button = ttk.Button(top, text='Загрузить аккаунты', command=self.scan)
        self.scan_button.pack(side='left', padx=5)
        ttk.Button(top, text='Папка accounts', command=self.open_folder).pack(side='left')
        direction_controls = ttk.Frame(manager, padding=(10, 0, 10, 5))
        direction_controls.pack(fill='x')
        ttk.Label(direction_controls, text='Направление:').pack(side='left')
        self.direction_choice = ttk.Combobox(direction_controls, state='readonly', width=28)
        self.direction_choice.pack(side='left', padx=5)
        self.direction_choice.bind('<<ComboboxSelected>>', self.change_direction)
        ttk.Button(direction_controls, text='+ Направление', command=self.add_direction).pack(side='left')
        ttk.Button(direction_controls, text='Переименовать', command=self.rename_direction).pack(side='left', padx=5)
        ttk.Button(direction_controls, text='Удалить', command=self.delete_direction).pack(side='left')
        self.accounts = ttk.Treeview(manager, columns=('name', 'direction', 'status'), height=7)
        self.accounts.heading('#0', text='Файл / папка')
        self.accounts.heading('name', text='Аккаунт')
        self.accounts.heading('direction', text='Направление / работает')
        self.accounts.heading('status', text='Статус')
        self.accounts.pack(fill='x', padx=10)
        self.accounts.bind('<Double-1>', self.open_account_folder)
        self.accounts.bind('<Button-3>', self.open_account_window)
        self.accounts.bind('<<TreeviewSelect>>', self.update_account_count)
        selection_controls = ttk.Frame(manager, padding=(10, 5))
        selection_controls.pack(fill='x')
        ttk.Label(selection_controls, text='Аккаунтов:').pack(side='left')
        self.account_count = ttk.Spinbox(selection_controls, from_=1, to=10000, width=6)
        self.account_count.set('1')
        self.account_count.pack(side='left', padx=5)
        ttk.Button(selection_controls, text='Выбрать свободные', command=self.select_direction_accounts).pack(side='left')
        self.selection_count = ttk.Label(selection_controls, text='Выбрано: 0')
        self.selection_count.pack(side='left', padx=10)
        ttk.Label(selection_controls, text='Ручной выбор: Ctrl / Shift. Отправка во все группы аккаунта.').pack(side='left')
        channel_controls = ttk.Frame(manager, padding=(10, 5))
        channel_controls.pack(fill='x')
        ttk.Label(channel_controls, text='Открытый канал:').pack(side='left')
        self.public_channel_link = ttk.Entry(channel_controls)
        self.public_channel_link.pack(side='left', fill='x', expand=True, padx=5)
        self.public_channel_link.bind('<Control-KeyPress>', self.handle_shortcut)
        self.public_channel_link.bind('<Button-3>', self.edit_menu)
        ttk.Button(channel_controls, text='Вставить',
                   command=lambda: self.edit_action(self.public_channel_link, 'paste')).pack(side='left')
        self.channel_join_button = ttk.Button(
            channel_controls, text='Вступить выбранными', command=self.join_selected_channel)
        self.channel_join_button.pack(side='left', padx=5)
        delivery_controls = ttk.Frame(manager, padding=(10, 5))
        delivery_controls.pack(fill='x')
        ttk.Label(delivery_controls, text='Режим рассылки:').pack(side='left')
        self.delivery_mode = ttk.Combobox(
            delivery_controls, values=['Готовые тексты', 'Пересылка сообщения'],
            state='readonly', width=22)
        self.delivery_mode.current(0)
        self.delivery_mode.pack(side='left', padx=5)
        ttk.Label(delivery_controls, text='Ссылка на сообщение:').pack(side='left', padx=(10, 0))
        self.source_message = ttk.Entry(delivery_controls)
        self.source_message.pack(side='left', fill='x', expand=True, padx=5)
        self.source_message.bind('<Control-KeyPress>', self.handle_shortcut)
        self.source_message.bind('<Button-3>', self.edit_menu)
        ttk.Button(delivery_controls, text='Вставить ссылку',
                   command=lambda: self.edit_action(self.source_message, 'paste')).pack(side='left')
        text_controls = ttk.Frame(manager, padding=5)
        text_controls.pack(fill='x', padx=5)
        ttk.Button(text_controls, text='+ Добавить текст', command=self.add_text).pack(side='left')
        ttk.Button(text_controls, text='Удалить текст', command=self.remove_text).pack(side='left', padx=5)
        ttk.Button(text_controls, text='Сохранить направления', command=self.save_texts).pack(side='left')
        ttk.Button(text_controls, text='Вставить', command=self.paste_current_text).pack(side='left', padx=5)
        ttk.Label(text_controls, text='Порядок:').pack(side='left', padx=10)
        self.mode = ttk.Combobox(text_controls, values=['По очереди', 'Случайный порядок'], state='readonly', width=22)
        self.mode.current(0)
        self.mode.pack(side='left')
        self.notebook = ttk.Notebook(manager)
        self.notebook.pack(fill='both', expand=True, padx=10)
        self.editors = []
        saved = {}
        try:
            saved = json.loads((BASE / 'texts.json').read_text('utf-8'))
            if not isinstance(saved, dict):
                saved = {}
        except (OSError, ValueError):
            pass
        initial = saved.get('texts')
        if not isinstance(initial, list) or not initial or not all(isinstance(t, str) for t in initial):
            initial = ['']
        for value in initial:
            self.add_text(value)
        if saved.get('mode') == 'random':
            self.mode.current(1)
        ttk.Label(manager, text='Готовые тексты работают как раньше. Для пересылки выберите второй режим и вставьте ссылку на один пост из канала.').pack(anchor='w', padx=10)
        ttk.Label(manager, text='Все выбранные аккаунты должны состоять в приватном канале; защита канала от копирования должна быть выключена.').pack(anchor='w', padx=10)
        controls = ttk.Frame(manager, padding=10)
        controls.pack(fill='x')
        ttk.Label(controls, text='Пауза между группами, сек.').pack(side='left')
        self.gap = ttk.Entry(controls, width=7)
        self.gap.insert(0, '25')
        self.gap.pack(side='left', padx=5)
        ttk.Label(controls, text='Пауза между кругами (0 = один раз)').pack(side='left')
        self.interval = ttk.Entry(controls, width=7)
        self.interval.insert(0, '0')
        self.interval.pack(side='left', padx=5)
        launch_controls = ttk.Frame(manager, padding=(10, 0, 10, 10))
        launch_controls.pack(fill='x')
        ttk.Label(launch_controls, text='Пауза между стартами аккаунтов, сек. (0 = одновременно)').pack(side='left')
        self.account_gap = ttk.Entry(launch_controls, width=7)
        self.account_gap.insert(0, '0')
        self.account_gap.pack(side='left', padx=5)
        launch_controls = ttk.Frame(manager_page.toolbar, padding=(10, 6, 10, 6))
        launch_controls.pack(fill='x')
        ttk.Button(launch_controls, text='Запустить направление', style='Primary.TButton',
                   command=self.start).pack(side='left', padx=5)
        ttk.Button(launch_controls, text='Стоп направления', command=lambda: self.engine.submit(self.engine.stop_direction(self.current_direction))).pack(side='left', padx=5)
        ttk.Button(launch_controls, text='Стоп выбранных', command=lambda: self.engine.submit(self.engine.stop(self.selected()))).pack(side='left')
        ttk.Button(launch_controls, text='Стоп всех', command=lambda: self.engine.submit(self.engine.stop(list(self.engine.jobs)))).pack(side='left', padx=5)
        self.log = tk.Text(manager, height=8, state='disabled')
        self.log.pack(fill='both', padx=10, pady=5)
        self.init_directions(saved)
        self.install_shortcuts(self)
        self.matrix_theme.style_widgets(self)
        self.after(150, lambda: self.matrix_theme.dark_titlebar(self))
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.runtime_diagnostics = (RuntimeDiagnostics(self.run_log_writer, self.runtime_snapshot)
                                    if self.run_log_writer else None)
        self.after(100, self.poll)

    def runtime_snapshot(self):
        clients = [pair[0] for pair in list(self.engine.clients.values())]
        def cache_size(client):
            cache = getattr(client, '_mb_entity_cache', None)
            return len(cache) if cache is not None else 0
        return dict(
            self.events.statistics(),
            connected_accounts=len(clients),
            live_update_accounts=sum(not getattr(client, '_no_updates', True) for client in clients),
            telegram_updates_pending=sum(
                getattr(client, '_updates_queue').qsize()
                for client in clients if getattr(client, '_updates_queue', None) is not None),
            telegram_entities=sum(cache_size(client) for client in clients),
            telegram_handler_tasks=sum(len(getattr(client, '_event_handler_tasks', ()))
                                       for client in clients),
            background_updates_discarded=sum(
                getattr(client, 'background_updates_discarded', 0) for client in clients),
            active_mailing_tasks=sum(not task.done() for task in list(self.engine.jobs.values())),
        )

    def report_callback_exception(self, exc_type, value, tb):
        details = ''.join(traceback.format_exception(exc_type, value, tb))
        self.persist_log_event('ui_error', details)

    def destroy(self):
        if not getattr(self, '_runtime_closed', False):
            self._runtime_closed = True
            diagnostics = getattr(self, 'runtime_diagnostics', None)
            if diagnostics:
                diagnostics.close()
            writer = getattr(self, 'run_log_writer', None)
            if writer:
                writer.close()
        super().destroy()

    def build_app_header(self):
        bar = tk.Frame(self, bg=COLORS['panel'], padx=20, pady=12)
        bar._matrix_custom = True
        bar.pack(fill='x')
        logo = tk.Label(bar, text='[ TM ]', bg=COLORS['panel'], fg=COLORS['accent'],
                        font=('Consolas', 16))
        logo._matrix_custom = True
        logo.pack(side='left', padx=(0, 12))
        brand = tk.Label(bar, text='TELEGRAM MANAGER', bg=COLORS['panel'],
                         fg=COLORS['text'], font=('Consolas', 12))
        brand._matrix_custom = True
        brand.pack(side='left')
        ttk.Button(bar, text='Обновления', command=self.check_app_updates).pack(side='right', padx=(8, 0))
        self.decorations_enabled = tk.BooleanVar(value=self.matrix_theme.decorations)
        ttk.Checkbutton(bar, text='Декор', style='Header.TCheckbutton', variable=self.decorations_enabled,
                        command=self.change_decorations).pack(side='right')
        self.sphere_animation_enabled = tk.BooleanVar(
            value=self.ui_preferences.get('sphere_animation', True) is not False)
        ttk.Checkbutton(bar, text='Анимация', style='Header.TCheckbutton',
                        variable=self.sphere_animation_enabled,
                        command=self.change_sphere_animation).pack(side='right', padx=10)
        edition = tk.Label(bar, text=f'v{self.release_config["version"]}',
                           bg=COLORS['panel'], fg=COLORS['muted'], font=('Consolas', 9))
        edition._matrix_custom = True
        edition.pack(side='right', padx=22)

    def change_decorations(self):
        enabled = self.decorations_enabled.get()
        self.matrix_theme.set_decorations(enabled)
        self.save_ui_preferences()

    def change_sphere_animation(self):
        self.dashboard_hologram.set_animation(self.sphere_animation_enabled.get())
        self.save_ui_preferences()

    def save_ui_preferences(self):
        if self.ui_preferences_blocked:
            return
        target = BASE / 'ui_preferences.json'
        try:
            preferences = dict(self.ui_preferences,
                               decorations=self.decorations_enabled.get(),
                               sphere_animation=self.sphere_animation_enabled.get())
            temporary = target.with_suffix('.tmp')
            temporary.write_text(json.dumps(preferences, indent=2), 'utf-8')
            temporary.replace(target)
            self.ui_preferences = preferences
        except OSError:
            self.events.put(('log', 'Не удалось сохранить оформление интерфейса'))

    def build_dashboard(self, page, manager, search, parser, likes):
        background = COLORS['bg']
        ink = COLORS['text']
        muted = COLORS['muted']
        page.configure(bg=background)
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(2, weight=1)
        def label(parent, text='', **kwargs):
            widget = tk.Label(parent, text=text, bg=parent.cget('bg'),
                              fg=kwargs.pop('fg', ink), **kwargs)
            widget._matrix_custom = True
            return widget
        header = tk.Frame(page, bg=background)
        header.grid(row=0, column=0, sticky='ew', padx=22, pady=(12, 8))
        titles = tk.Frame(header, bg=background)
        titles.pack(side='left', fill='x', expand=True)
        label(titles, '// ОБЗОР СИСТЕМЫ', fg=muted, font=('Consolas', 8)).pack(anchor='w')
        label(titles, 'Главная_', font=('Segoe UI', 21)).pack(anchor='w')
        self.dashboard_summary = label(
            titles, 'Рассылка ещё не запускалась', fg=muted, font=('Segoe UI', 9))
        self.dashboard_summary.pack(anchor='w', pady=(4, 0))
        ttk.Button(header, text='Журнал рассылки  ›', style='Primary.TButton',
                   command=lambda: self.pages.select(manager)).pack(side='right')
        ttk.Button(header, text='Папка логов',
                   command=self.open_logs_folder).pack(side='right', padx=(0, 8))
        self.failed_chats_button = ttk.Button(
            header, text=f'Проблемные чаты: {len(self.failed_chats)}',
            command=self.open_failed_chats_window)
        self.failed_chats_button.pack(side='right', padx=(0, 8))
        overview = tk.Frame(page, bg=background)
        overview.grid(row=1, column=0, sticky='ew', padx=20, pady=(0, 10))
        overview.grid_columnconfigure(0, weight=2, minsize=250)
        overview.grid_columnconfigure(1, weight=5)
        core = DecorPanel(overview, self.matrix_theme, code='CORE / TM', minheight=204)
        core.grid(row=0, column=0, sticky='nsew', padx=(0, 12))
        self.dashboard_hologram = GifSphereCanvas(
            core.body, self.matrix_theme, RESOURCE_BASE / 'assets' / 'sphere-3d-360.gif',
            animation=self.sphere_animation_enabled.get())
        self.dashboard_hologram.pack(fill='both', expand=True)
        cards = tk.Frame(overview, bg=background)
        cards.grid(row=0, column=1, sticky='nsew')
        specs = [
            ('total', 'Всего аккаунтов', 'Файлы сессий'),
            ('working', 'В работе', 'Аккаунты в рассылке'),
            ('sent', 'Отправлено', 'После запуска'),
            ('errors', 'Ошибки / пропуски', 'Сбой / ограничения'),
            ('unavailable', 'Недоступны', 'Нет связи / авторизации'),
            ('joins', 'Вступлений', 'За запуск'),
        ]
        self.dashboard_card_values = {}
        for number, (key, title, subtitle) in enumerate(specs):
            row, column = divmod(number, 3)
            cards.grid_columnconfigure(column, weight=1, uniform='cards')
            cards.grid_rowconfigure(row, weight=1)
            card = DecorPanel(cards, self.matrix_theme, code=f'NODE / {number + 1:02d}', minheight=100)
            card.grid(row=row, column=column, sticky='nsew', padx=4, pady=(0, 4))
            label(card.body, title, fg=muted, font=('Segoe UI', 9)).pack(anchor='w')
            value = label(card.body, '0', font=('Consolas', 21),
                          fg=COLORS['warning'] if key == 'errors' else ink)
            value.pack(anchor='w')
            self.dashboard_card_values[key] = value
            label(card.body, subtitle, fg=muted, font=('Segoe UI', 8)).pack(anchor='w')
        events_shell = DecorPanel(page, self.matrix_theme, code='EVENT STREAM / 02', minheight=120)
        events_shell.grid(row=2, column=0, sticky='nsew', padx=20, pady=(0, 10))
        events_frame = events_shell.body
        event_title = tk.Label(events_frame, text='Последние события', bg=COLORS['panel'], fg=ink,
                               font=('Segoe UI', 10))
        event_title._matrix_custom = True
        event_title.pack(anchor='w')
        self.dashboard_events = ttk.Treeview(
            events_frame, columns=('time', 'type', 'event'), show='headings', height=6)
        self.dashboard_events.heading('time', text='Время')
        self.dashboard_events.heading('type', text='Тип')
        self.dashboard_events.heading('event', text='Событие')
        self.dashboard_events.column('time', width=70, stretch=False)
        self.dashboard_events.column('type', width=105, stretch=False)
        self.dashboard_events.column('event', width=700)
        self.dashboard_events.pack(fill='x', pady=(4, 0))
        quick = tk.Frame(page, bg=background)
        quick.grid(row=3, column=0, sticky='ew', padx=22, pady=(0, 10))
        quick_title = tk.Label(quick, text='БЫСТРЫЙ ДОСТУП  //', bg=background, fg=muted,
                               font=('Consolas', 9))
        quick_title._matrix_custom = True
        quick_title.pack(side='left', padx=(0, 12))
        for label, target in [('Аккаунты и отправка', manager), ('База пользователей', parser),
                              ('Поиск групп', search), ('Лайки', likes)]:
            ttk.Button(quick, text=label,
                       command=lambda frame=target: self.pages.select(frame)).pack(side='left', padx=4)
        self.after(250, self.refresh_dashboard)

    def setup_run_log(self):
        try:
            folder = BASE / 'logs'
            folder.mkdir(exist_ok=True)
            stamp = time.strftime('%Y-%m-%d_%H-%M-%S')
            self.log_path = folder / f'telegram_manager_{stamp}.log'
            self.log_path.write_text(
                f'{time.strftime("%Y-%m-%d %H:%M:%S")} [system] Запуск Telegram Manager\n',
                encoding='utf-8')
        except OSError:
            self.log_path = None

    def persist_log_event(self, kind, value):
        if self.log_path is None or kind in {
                'dashboard_activity', 'parser_row', 'parser_snapshot', 'parser_groups',
                'search_row', 'likes_accounts'}:
            return
        important = (kind in {'log', 'direction_error', 'account', 'status', 'direction_job',
                              'account_action', 'account_progress', 'convert_log', 'failed_chat',
                              'failed_chat_left', 'failed_chat_leave_error'}
                     or kind.endswith('_log') or kind.endswith('_status')
                     or kind.endswith('_error') or kind.endswith('_attention')
                     or kind.endswith('_running'))
        if not important:
            return
        if isinstance(value, str):
            message = value
        else:
            try:
                message = json.dumps(value, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                message = str(value)
        message = message.replace('\r', '').replace('\n', '\\n')[:8000]
        line = f'{time.strftime("%Y-%m-%d %H:%M:%S")} [{kind}] {message}\n'
        writer = getattr(self, 'run_log_writer', None)
        if writer:
            writer.append(line)
        else:
            self.log_buffer.append(line)

    def flush_run_log(self):
        writer = getattr(self, 'run_log_writer', None)
        if writer:
            for line in self.log_buffer:
                writer.append(line)
            self.log_buffer.clear()
            return writer.flush()
        if self.log_path is None or not self.log_buffer:
            return
        lines, self.log_buffer = self.log_buffer, []
        try:
            with self.log_path.open('a', encoding='utf-8') as stream:
                stream.writelines(lines)
        except OSError:
            self.log_path = None

    def open_logs_folder(self):
        folder = BASE / 'logs'
        try:
            folder.mkdir(exist_ok=True)
            if sys.platform == 'win32':
                os.startfile(folder)
            else:
                messagebox.showinfo('Логи', str(folder))
        except OSError as exc:
            messagebox.showerror('Логи', f'Не удалось открыть папку логов: {exc}')

    @staticmethod
    def failed_chat_key(record):
        return f'{record["account"]}\x1f{int(record["chat_id"])}'

    def load_failed_chats(self):
        target = BASE / 'failed_chats.json'
        if not target.exists():
            return {}
        try:
            data = json.loads(target.read_text('utf-8'))
            items = data.get('items', [])
            if not isinstance(items, list):
                raise ValueError()
            result = {}
            for item in items:
                if (not isinstance(item, dict) or not isinstance(item.get('account'), str)
                        or not isinstance(item.get('chat_id'), int)
                        or not isinstance(item.get('title', ''), str)
                        or not isinstance(item.get('reason', ''), str)):
                    raise ValueError()
                result[self.failed_chat_key(item)] = item
            return result
        except (OSError, ValueError, TypeError):
            self.failed_chats_save_blocked = True
            self.log_buffer.append(
                f'{time.strftime("%Y-%m-%d %H:%M:%S")} [system] '
                'failed_chats.json повреждён; файл не будет перезаписан\n')
            return {}

    def save_failed_chats(self, background=False):
        if self.failed_chats_save_blocked or not self.failed_chats_dirty:
            return not self.failed_chats_save_blocked
        target = BASE / 'failed_chats.json'
        temporary = target.with_suffix('.tmp')
        try:
            data = {'version': 1, 'items': list(self.failed_chats.values())}
            writer = getattr(self, 'run_log_writer', None)
            if writer:
                if not writer.save_json(target, data, wait=not background):
                    raise OSError('Дисковая запись не завершена')
            else:
                temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), 'utf-8')
                temporary.replace(target)
            self.failed_chats_dirty = False
            return True
        except OSError as exc:
            if background:
                self.events.put(('log', f'Не удалось сохранить список проблемных чатов: {exc}'))
            else:
                messagebox.showerror('Проблемные чаты', f'Не удалось сохранить список: {exc}')
            return False

    def update_failed_chats_button(self):
        if hasattr(self, 'failed_chats_button'):
            self.failed_chats_button.configure(
                text=f'Проблемные чаты: {len(self.failed_chats)}')

    def record_failed_chat(self, record):
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        key = self.failed_chat_key(record)
        previous = self.failed_chats.get(key, {})
        item = dict(account=record['account'], chat_id=int(record['chat_id']),
                    title=str(record.get('title') or record['chat_id']),
                    reason=str(record.get('reason') or 'ограничение Telegram'),
                    first_seen=previous.get('first_seen', now), last_seen=now)
        self.failed_chats[key] = item
        self.failed_chats_dirty = True
        self.failed_chats_ui_dirty_keys.add(key)
        self.schedule_failed_chats_ui_refresh()

    def remove_failed_chat(self, record):
        key = self.failed_chat_key(record)
        if self.failed_chats.pop(key, None) is not None:
            self.failed_chats_dirty = True
            self.failed_chats_ui_dirty_keys.add(key)
            self.schedule_failed_chats_ui_refresh()

    def schedule_failed_chats_ui_refresh(self):
        if self.failed_chats_ui_pending:
            return
        self.failed_chats_ui_pending = True
        self.after(250, self.finish_failed_chats_ui_refresh)

    def finish_failed_chats_ui_refresh(self):
        self.failed_chats_ui_pending = False
        dirty_keys = self.failed_chats_ui_dirty_keys
        self.failed_chats_ui_dirty_keys = set()
        self.update_failed_chats_button()
        self.update_failed_chats_window_rows(dirty_keys)

    def open_failed_chats_window(self):
        if self.failed_chats_save_blocked:
            messagebox.showerror(
                'Проблемные чаты',
                'failed_chats.json повреждён. Переименуйте или исправьте его и перезапустите программу.')
            return
        if self.failed_chats_window and self.failed_chats_window.winfo_exists():
            self.failed_chats_window.lift()
            self.failed_chats_window.focus_force()
            return
        window = tk.Toplevel(self)
        self.failed_chats_window = window
        window.title('Проблемные чаты')
        window.geometry('1050x560')
        window.transient(self)
        ttk.Label(
            window,
            text='Здесь собраны чаты с постоянными ограничениями. Выход необратим: аккаунт потеряет доступ к чату.',
            padding=10).pack(anchor='w')
        frame = ttk.Frame(window, padding=(10, 0, 10, 5))
        frame.pack(fill='both', expand=True)
        self.failed_chats_table = ttk.Treeview(
            frame, columns=('account', 'title', 'chat_id', 'reason', 'last_seen'),
            show='headings', selectmode='extended')
        headings = [('account', 'Аккаунт', 210), ('title', 'Чат', 190),
                    ('chat_id', 'ID', 130), ('reason', 'Причина', 330),
                    ('last_seen', 'Последняя ошибка', 140)]
        for name, title, width in headings:
            self.failed_chats_table.heading(name, text=title)
            self.failed_chats_table.column(name, width=width, stretch=name in ('title', 'reason'))
        scrollbar = ttk.Scrollbar(frame, command=self.failed_chats_table.yview)
        self.failed_chats_table.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        self.failed_chats_table.pack(fill='both', expand=True)
        buttons = ttk.Frame(window, padding=10)
        buttons.pack(fill='x')
        self.failed_leave_selected_button = ttk.Button(
            buttons, text='Выйти из выбранных', command=lambda: self.leave_failed_from_ui(False))
        self.failed_leave_selected_button.pack(side='left')
        self.failed_leave_all_button = ttk.Button(
            buttons, text='Выйти из всех', command=lambda: self.leave_failed_from_ui(True))
        self.failed_leave_all_button.pack(side='left', padx=6)
        ttk.Button(buttons, text='Убрать выбранные из списка',
                   command=self.dismiss_failed_selected).pack(side='left', padx=6)
        ttk.Button(buttons, text='Закрыть', command=window.destroy).pack(side='right')
        self.failed_chats_status = ttk.Label(buttons, text='')
        self.failed_chats_status.pack(side='right', padx=12)
        window.protocol('WM_DELETE_WINDOW', window.destroy)
        window.bind('<Destroy>', lambda event: self.failed_window_closed(event, window))
        self.refresh_failed_chats_window()

    def failed_window_closed(self, event, window):
        if event.widget is window:
            self.failed_chats_window = None

    def refresh_failed_chats_window(self):
        window = getattr(self, 'failed_chats_window', None)
        table = getattr(self, 'failed_chats_table', None)
        if not window or not window.winfo_exists() or table is None:
            return
        for row in table.get_children():
            table.delete(row)
        self.failed_chat_rows = {}
        self.failed_chat_row_by_key = {}
        items = sorted(self.failed_chats.items(), key=lambda pair: pair[1].get('last_seen', ''), reverse=True)
        for number, (key, item) in enumerate(items):
            row = f'failed_{number}'
            self.failed_chat_rows[row] = key
            self.failed_chat_row_by_key[key] = row
            table.insert('', 'end', iid=row, values=(
                item['account'], item.get('title', ''), item['chat_id'],
                item.get('reason', ''), item.get('last_seen', '')))
        self.failed_chat_row_sequence = 0
        if hasattr(self, 'failed_chats_status'):
            self.failed_chats_status.configure(text=f'Всего: {len(items)}')

    def update_failed_chats_window_rows(self, keys):
        """Apply changed rows without rebuilding a table that may contain thousands."""
        window = getattr(self, 'failed_chats_window', None)
        table = getattr(self, 'failed_chats_table', None)
        if not window or not window.winfo_exists() or table is None:
            return
        rows = getattr(self, 'failed_chat_rows', {})
        by_key = getattr(self, 'failed_chat_row_by_key', {})
        for key in keys:
            item = self.failed_chats.get(key)
            row = by_key.get(key)
            if item is None:
                if row is not None and table.exists(row):
                    table.delete(row)
                by_key.pop(key, None)
                if row is not None:
                    rows.pop(row, None)
                continue
            values = (item['account'], item.get('title', ''), item['chat_id'],
                      item.get('reason', ''), item.get('last_seen', ''))
            if row is None or not table.exists(row):
                self.failed_chat_row_sequence = getattr(self, 'failed_chat_row_sequence', 0) + 1
                row = f'failed_live_{self.failed_chat_row_sequence}'
                rows[row] = key
                by_key[key] = row
                table.insert('', 0, iid=row, values=values)
            else:
                table.item(row, values=values)
                table.move(row, '', 0)
        self.failed_chat_rows = rows
        self.failed_chat_row_by_key = by_key
        if hasattr(self, 'failed_chats_status'):
            self.failed_chats_status.configure(text=f'Всего: {len(self.failed_chats)}')

    def selected_failed_keys(self, all_items=False):
        if all_items:
            return list(self.failed_chats)
        table = getattr(self, 'failed_chats_table', None)
        if table is None:
            return []
        return [self.failed_chat_rows[row] for row in table.selection()
                if row in self.failed_chat_rows]

    def leave_failed_from_ui(self, all_items=False):
        if self.failed_leave_future and not self.failed_leave_future.done():
            return
        keys = self.selected_failed_keys(all_items)
        if not keys:
            messagebox.showinfo('Проблемные чаты', 'Выберите хотя бы одну строку')
            return
        records = [self.failed_chats[key] for key in keys]
        accounts = sorted({item['account'] for item in records})
        missing = [key for key in accounts if key not in self.engine.clients]
        if missing:
            messagebox.showinfo(
                'Проблемные чаты',
                f'Сначала загрузите все нужные аккаунты. Не подключены: {len(missing)}')
            return
        busy = [key for key in accounts
                if key in self.engine.jobs and not self.engine.jobs[key].done()]
        if busy:
            messagebox.showinfo(
                'Проблемные чаты',
                f'Сначала остановите рассылку для задействованных аккаунтов: {len(busy)}')
            return
        if not messagebox.askyesno(
                'Выйти из чатов',
                f'Выйти из {len(records)} чатов на {len(accounts)} аккаунтах?\n\n'
                'Это действие нельзя отменить: для возврата понадобятся ссылки-приглашения.',
                parent=self.failed_chats_window):
            return
        for button in (self.failed_leave_selected_button, self.failed_leave_all_button):
            button.configure(state='disabled')
        self.failed_chats_status.configure(text=f'Обработка: {len(records)}')
        self.failed_leave_future = self.engine.submit(self.engine.leave_failed_chats(records))
        self.failed_leave_future.add_done_callback(
            lambda _: self.events.put(('failed_chat_leave_done', None)))

    def dismiss_failed_selected(self):
        keys = self.selected_failed_keys(False)
        if not keys:
            messagebox.showinfo('Проблемные чаты', 'Выберите хотя бы одну строку')
            return
        if not messagebox.askyesno(
                'Убрать из списка',
                f'Убрать выбранные записи из списка без выхода из чатов: {len(keys)}?',
                parent=self.failed_chats_window):
            return
        for key in keys:
            self.failed_chats.pop(key, None)
        self.failed_chats_dirty = True
        self.save_failed_chats()
        self.update_failed_chats_button()
        self.refresh_failed_chats_window()

    def add_dashboard_event(self, event_type, message):
        if not hasattr(self, 'dashboard_events'):
            return
        text = str(message).replace('\n', ' ').strip()
        if len(text) > 180:
            text = text[:177] + '...'
        self.dashboard_events.insert('', 0, values=(time.strftime('%H:%M:%S'), event_type, text))
        children = self.dashboard_events.get_children()
        for item in children[100:]:
            self.dashboard_events.delete(item)

    def add_dashboard_activity(self, category, action, count=1):
        if category == 'mailing' and action == 'sent':
            self.dashboard_counts['sent'] += count
        elif category == 'mailing' and action == 'error':
            self.dashboard_counts['errors'] += count
        elif category == 'mailing' and action == 'skipped':
            self.dashboard_counts['skipped'] += count
        elif category == 'joins' and action == 'joined':
            self.dashboard_counts['joins'] += count

    def observe_dashboard_event(self, kind, value):
        if kind == 'dashboard_activity':
            category, action, _key, *extra = value
            self.add_dashboard_activity(category, action, extra[0] if extra else 1)
        elif kind == 'direction_job':
            key, owner = value
            if owner is not None:
                if self.dashboard_started_at is None:
                    self.dashboard_started_at = time.time()
                self.add_dashboard_event('Рассылка', f'{key}: задача запущена')
            else:
                self.add_dashboard_event('Рассылка', f'{key}: задача завершена')
        elif kind == 'parser_running':
            self.add_dashboard_event('Парсинг', 'Парсер включён' if value else 'Парсер остановлен')
        elif kind in ('log', 'parser_log', 'direction_error'):
            if kind == 'log' and is_high_volume_delivery_log(value):
                return
            event_type = {'log': 'Журнал', 'parser_log': 'Парсинг',
                          'direction_error': 'Ошибка'}.get(kind, 'Событие')
            self.add_dashboard_event(event_type, value)
        elif kind == 'failed_chat_leave_error':
            self.add_dashboard_event('Ошибка выхода', value.get('error', value))

    def refresh_dashboard(self):
        if not hasattr(self, 'dashboard_card_values') or not self.winfo_exists():
            return
        if not self.dashboard_page.winfo_viewable():
            self.after(1000, self.refresh_dashboard)
            return
        total = self.dashboard_account_total
        working = len(getattr(self, 'running_directions', {}))
        unavailable = 0
        for row in self.rows.values():
            status = str(self.accounts.set(row, 'status')).casefold()
            if any(marker in status for marker in ('ошибка', 'нет связи', 'авторизац')):
                unavailable += 1
        values = dict(total=total, working=working, sent=self.dashboard_counts['sent'],
                      errors=f'{self.dashboard_counts["errors"]} / {self.dashboard_counts["skipped"]}',
                      unavailable=unavailable,
                      joins=self.dashboard_counts['joins'])
        for key, value in values.items():
            if str(self.dashboard_card_values[key].cget('text')) != str(value):
                self.dashboard_card_values[key].configure(text=str(value))
        if working:
            summary = f'Рассылка работает: аккаунтов {working}  /  Сводка обновляется в реальном времени'
        elif self.dashboard_started_at:
            summary = f'Последний запуск: {time.strftime("%H:%M:%S", time.localtime(self.dashboard_started_at))}  /  Сейчас рассылка остановлена'
        else:
            summary = 'Рассылка ещё не запускалась  /  Сводка обновляется в реальном времени'
        if self.dashboard_summary.cget('text') != summary:
            self.dashboard_summary.configure(text=summary)
        self.after(1000, self.refresh_dashboard)

    def build_converter(self, page):
        ttk.Label(page, text='tdata → .session + .json', font=('', 14, 'bold')).pack(anchor='w', pady=(0, 10))
        ttk.Label(page, text='Выберите целую папку tdata. Закройте Telegram Desktop, использующий её, перед импортом.').pack(anchor='w')
        row = ttk.Frame(page)
        row.pack(fill='x', pady=12)
        self.tdata_path = ttk.Entry(row)
        self.tdata_path.pack(side='left', fill='x', expand=True)
        ttk.Button(row, text='Выбрать папку', command=self.choose_tdata).pack(side='left', padx=8)
        ttk.Label(page, text='Локальный пароль Telegram Desktop (если установлен; иначе пусто):').pack(anchor='w')
        self.tdata_passcode = ttk.Entry(page, show='*', width=40)
        self.tdata_passcode.pack(anchor='w', pady=5)
        ttk.Label(page, text='Это локальное преобразование существующей сессии. Облачный пароль 2FA не нужен.').pack(anchor='w', pady=5)
        ttk.Label(page, text='Результат: новая папка в accounts. Исходная tdata сохраняется; существующие пары не перезаписываются.').pack(anchor='w', pady=5)
        self.convert_button = ttk.Button(page, text='Конвертировать', command=self.convert_tdata)
        self.convert_button.pack(anchor='w', pady=10)
        self.convert_status = ttk.Label(page, text='Готов к работе')
        self.convert_status.pack(anchor='w')
        self.convert_log = tk.Text(page, state='disabled', height=12, wrap='word')
        self.convert_log.pack(fill='both', expand=True, pady=10)
        ttk.Button(page, text='Открыть accounts', command=self.open_folder).pack(anchor='w')

    def choose_tdata(self):
        path = filedialog.askdirectory(title='Выберите папку tdata')
        if path:
            self.tdata_path.delete(0, 'end')
            self.tdata_path.insert(0, path)

    def convert_tdata(self):
        if self.converting or self.busy:
            messagebox.showinfo('Загрузка', 'Дождитесь завершения текущей операции')
            return
        source = self.tdata_path.get().strip()
        if not source or not Path(source).is_dir():
            messagebox.showerror('Папка', 'Выберите существующую папку tdata')
            return
        passcode = self.tdata_passcode.get()
        self.tdata_passcode.delete(0, 'end')
        self.converting = True
        self.convert_button.config(state='disabled')
        self.scan_button.config(state='disabled')
        self.convert_status.config(text='Конвертация…')
        self.engine.submit(self.engine.convert_tdata(source, passcode))

    def install_shortcuts(self, parent):
        for widget in parent.winfo_children():
            if widget.winfo_class() in ('Text', 'Entry', 'TEntry'):
                widget.bind('<Control-KeyPress>', self.handle_shortcut)
                widget.bind('<Button-3>', self.edit_menu)
            self.install_shortcuts(widget)

    def handle_shortcut(self, event):
        action = shortcut_action(event.keycode, event.keysym, sys.platform == 'win32')
        if action is None:
            return None
        self.edit_action(event.widget, action)
        return 'break'

    def edit_action(self, widget, action):
        try:
            if str(widget.cget('state')) == 'disabled':
                return
            if action == 'all':
                if widget.winfo_class() == 'Text':
                    widget.tag_add('sel', '1.0', 'end-1c')
                    widget.mark_set('insert', 'end-1c')
                else:
                    widget.selection_range(0, 'end')
                    widget.icursor('end')
            elif action == 'paste':
                # Read Unicode directly; do not encode to a Windows code page.
                value = self.clipboard_get()
                if widget.winfo_class() == 'Text':
                    if widget.tag_ranges('sel'):
                        widget.delete('sel.first', 'sel.last')
                    widget.insert('insert', value)
                    widget.see('insert')
                else:
                    if widget.selection_present():
                        widget.delete('sel.first', 'sel.last')
                    widget.insert('insert', value)
            else:
                widget.event_generate({'copy': '<<Copy>>', 'cut': '<<Cut>>', 'undo': '<<Undo>>'}[action])
        except tk.TclError:
            if action == 'paste':
                messagebox.showinfo('Буфер обмена', 'В буфере нет доступного текста. Скопируйте текст заново.')

    def paste_current_text(self):
        if self.editors:
            editor = self.editors[self.notebook.index(self.notebook.select())]
            editor.focus_set()
            self.edit_action(editor, 'paste')

    def edit_menu(self, event):
        event.widget.focus_set()
        menu = tk.Menu(self, tearoff=False)
        for label, action in [('Вставить', 'paste'), ('Копировать', 'copy'), ('Вырезать', 'cut'), ('Выделить всё', 'all')]:
            menu.add_command(label=label, command=lambda a=action: self.edit_action(event.widget, a))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return 'break'

    def selected(self):
        return [self.accounts.item(row, 'text') for row in self.accounts.selection()]

    def join_selected_channel(self):
        if self.channel_join_future and not self.channel_join_future.done():
            return
        try:
            username = public_channel_username(self.public_channel_link.get())
        except ValueError as exc:
            messagebox.showinfo('Открытый канал', str(exc))
            return
        keys = self.selected()
        if not keys:
            messagebox.showinfo('Открытый канал', 'Выберите хотя бы один аккаунт')
            return
        if not any(key in self.engine.clients for key in keys):
            messagebox.showinfo('Открытый канал', 'Среди выбранных нет подключённых аккаунтов')
            return
        self.channel_join_button.config(state='disabled')
        self.channel_join_future = self.engine.submit(
            self.engine.join_public_channel_many(keys, username))
        self.channel_join_future.add_done_callback(
            lambda _: self.events.put(('channel_join_done', None)))

    def open_folder(self):
        if sys.platform == 'win32':
            os.startfile(BASE / 'accounts')
        else:
            messagebox.showinfo('Папка', str(BASE / 'accounts'))

    def open_account_folder(self, event):
        row = self.accounts.identify_row(event.y)
        if not row:
            return
        key = self.accounts.item(row, 'text')
        accounts_root = (BASE / 'accounts').resolve()
        folder = (accounts_root / key).resolve().parent
        if not folder.is_relative_to(accounts_root) or not folder.is_dir():
            messagebox.showerror('Папка аккаунта', 'Папка аккаунта не найдена')
            return
        if sys.platform == 'win32':
            try:
                os.startfile(folder)
            except OSError as exc:
                messagebox.showerror('Папка аккаунта', f'Не удалось открыть папку: {exc}')
        else:
            messagebox.showinfo('Папка аккаунта', str(folder))

    def open_account_window(self, event):
        row = self.accounts.identify_row(event.y)
        if not row:
            return
        key = self.accounts.item(row, 'text')
        self.accounts.selection_set(row)
        self.accounts.focus(row)
        if key not in self.engine.clients:
            messagebox.showinfo('Аккаунт', 'Сначала подключите аккаунт кнопкой «Загрузить аккаунты»')
            return
        if key in self.account_windows:
            self.account_windows[key]['window'].lift()
            return

        window = tk.Toplevel(self)
        window.title(f'Настройки аккаунта — {key}')
        window.geometry('690x650')
        window.minsize(580, 540)
        body = ttk.Frame(window, padding=12)
        body.pack(fill='both', expand=True)
        body.columnconfigure(0, weight=1)
        profile = ttk.LabelFrame(body, text='Профиль', padding=10)
        profile.grid(row=0, column=0, sticky='ew')
        profile.columnconfigure(1, weight=1)
        buttons = []

        ttk.Label(profile, text='Юзернейм').grid(row=0, column=0, sticky='w', pady=4)
        username = ttk.Entry(profile)
        username.grid(row=0, column=1, sticky='ew', padx=8)
        button = ttk.Button(profile, text='Сменить',
                            command=lambda: self.run_account_action(key, 'username', username.get()))
        button.grid(row=0, column=2)
        buttons.append(button)

        ttk.Label(profile, text='Био').grid(row=1, column=0, sticky='nw', pady=4)
        bio = tk.Text(profile, height=3, wrap='word', undo=True)
        bio.grid(row=1, column=1, sticky='ew', padx=8, pady=4)
        button = ttk.Button(profile, text='Сменить',
                            command=lambda: self.run_account_action(key, 'bio', bio.get('1.0', 'end-1c')))
        button.grid(row=1, column=2)
        buttons.append(button)

        ttk.Label(profile, text='Имя профиля').grid(row=2, column=0, sticky='w', pady=4)
        first_name = ttk.Entry(profile)
        first_name.grid(row=2, column=1, sticky='ew', padx=8)
        button = ttk.Button(profile, text='Сменить',
                            command=lambda: self.run_account_action(key, 'name', first_name.get()))
        button.grid(row=2, column=2)
        buttons.append(button)

        privacy = ttk.LabelFrame(body, text='Уведомления и приватность', padding=10)
        privacy.grid(row=1, column=0, sticky='ew', pady=10)
        for row_number, (label, action) in enumerate((
                ('Отключить общие уведомления от личных чатов и групп', 'notifications'),
                ('Скрыть время последнего захода для всех', 'last_seen'),
                ('Запретить звонки для всех', 'calls'))):
            button = ttk.Button(privacy, text=label,
                                command=lambda a=action: self.run_account_action(key, a))
            button.grid(row=row_number, column=0, sticky='w', pady=3)
            buttons.append(button)

        addlist = ttk.LabelFrame(body, text='Добавить папки чатов по ссылкам', padding=10)
        addlist.grid(row=2, column=0, sticky='nsew')
        body.rowconfigure(2, weight=1)
        addlist.columnconfigure(0, weight=1)
        ttk.Label(addlist, text='Ссылки t.me/addlist/... — по одной на строке').grid(row=0, column=0, sticky='w')
        links = tk.Text(addlist, height=5, wrap='none', undo=True)
        links.grid(row=1, column=0, columnspan=2, sticky='nsew', pady=5)
        addlist.rowconfigure(1, weight=1)
        add_button = ttk.Button(addlist, text='Добавить папки',
                                command=lambda: self.run_account_action(key, 'addlist', links.get('1.0', 'end-1c')))
        add_button.grid(row=2, column=0, sticky='w')
        buttons.append(add_button)
        stop_button = ttk.Button(addlist, text='Остановить', state='disabled',
                                 command=lambda: self.stop_chatlists(key))
        stop_button.grid(row=2, column=1, sticky='e')
        progress = tk.Text(addlist, height=4, wrap='word', state='disabled')
        progress.grid(row=3, column=0, columnspan=2, sticky='ew', pady=(6, 0))

        status = ttk.Label(body, text='Готово к работе')
        status.grid(row=3, column=0, sticky='w', pady=(8, 0))
        self.account_windows[key] = {'window': window, 'buttons': buttons,
                                     'stop': stop_button, 'status': status,
                                     'progress': progress, 'busy': False}
        window.protocol('WM_DELETE_WINDOW', lambda: self.close_account_window(key))
        self.install_shortcuts(window)
        username.focus_set()

    def close_account_window(self, key):
        future = self.chatlist_futures.pop(key, None)
        if future and not future.done():
            future.cancel()
        info = self.account_windows.pop(key, None)
        if info:
            info['window'].destroy()

    def stop_chatlists(self, key):
        future = self.chatlist_futures.get(key)
        if future and not future.done():
            future.cancel()
            info = self.account_windows.get(key)
            if info:
                info['status'].config(text='Останавливаем добавление папок…')

    def run_account_action(self, key, action, value=None):
        info = self.account_windows.get(key)
        if not info or info['busy']:
            return
        if key not in self.engine.clients:
            messagebox.showerror('Аккаунт', 'Аккаунт отключён. Загрузите его заново.')
            return
        if action == 'username':
            value = value.strip().removeprefix('@')
            if value and not re.fullmatch(r'[A-Za-z0-9_]{5,32}', value):
                messagebox.showerror('Юзернейм', 'Нужно 5–32 латинские буквы, цифры или подчёркивания')
                return
            if not value and not messagebox.askyesno('Юзернейм', 'Удалить текущий юзернейм?'):
                return
        elif action == 'bio':
            if not value and not messagebox.askyesno('Био', 'Очистить текущее био?'):
                return
        elif action == 'name':
            value = value.strip()
            if not value:
                messagebox.showerror('Имя', 'Введите новое имя профиля')
                return
        elif action == 'addlist':
            try:
                value = addlist_slugs(value)
            except ValueError as exc:
                messagebox.showerror('Папки чатов', str(exc))
                return

        info['busy'] = True
        for button in info['buttons']:
            button.config(state='disabled')
        info['stop'].config(state='normal' if action == 'addlist' else 'disabled')
        info['status'].config(text='Выполняется…')
        if action == 'addlist':
            self.chatlist_futures[key] = self.engine.submit(self.engine.add_chatlists(key, value))
        else:
            self.engine.submit(self.engine.account_action(key, action, value))

    def account_window_event(self, kind, value):
        key = value[0]
        info = self.account_windows.get(key)
        if kind == 'account_progress':
            if info:
                progress = info['progress']
                progress.config(state='normal')
                progress.insert('end', value[1] + '\n')
                progress.see('end')
                progress.config(state='disabled')
            return
        _, action, success, message = value
        self.events.put(('log', f'{key}: {action}: {message}'))
        if info:
            info['busy'] = False
            for button in info['buttons']:
                button.config(state='normal')
            info['stop'].config(state='disabled')
            info['status'].config(text=message if success else f'Ошибка: {message}')
        if action == 'addlist':
            self.chatlist_futures.pop(key, None)

    def scan(self):
        if self.busy or self.converting:
            return
        self.capture_direction()
        aid, ahash = self.api_id.get().strip(), self.api_hash.get().strip()
        if aid and (not aid.isdecimal() or int(aid) <= 0):
            messagebox.showerror('Ошибка', 'API ID должен быть положительным числом')
            return
        (BASE / 'config.json').write_text(json.dumps({'api_id': aid, 'api_hash': ahash}), 'utf-8')
        self.busy = True
        self.scan_button.config(state='disabled')
        f = self.engine.submit(self.engine.scan(aid, ahash))
        f.add_done_callback(lambda _: self.events.put(('scan_done', None)))

    def add_text(self, value=''):
        frame = ttk.Frame(self.notebook)
        editor = tk.Text(frame, height=8, wrap='word', undo=True)
        self.matrix_theme.style_widgets(editor)
        editor.insert('1.0', value)
        scrollbar = ttk.Scrollbar(frame, command=editor.yview)
        editor.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        editor.pack(fill='both', expand=True)
        editor.bind('<Control-KeyPress>', self.handle_shortcut)
        editor.bind('<Button-3>', self.edit_menu)
        self.editors.append(editor)
        self.notebook.add(frame, text=f'Текст {len(self.editors)}')
        self.notebook.select(frame)

    def remove_text(self):
        if len(self.editors) <= 1:
            self.editors[0].delete('1.0', 'end')
            return
        index = self.notebook.index(self.notebook.select())
        frame = self.editors[index].master
        self.notebook.forget(frame)
        frame.destroy()
        self.editors.pop(index)
        for i in range(len(self.editors)):
            self.notebook.tab(i, text=f'Текст {i + 1}')

    def text_values(self):
        return [e.get('1.0', 'end').strip() for e in self.editors if e.get('1.0', 'end').strip()]

    def init_directions(self, legacy):
        self.running_directions = {}
        self.direction_save_blocked = False
        initial = {'id': uuid.uuid4().hex, 'name': 'Основное', 'accounts': [],
                   'texts': self.text_values(), 'mode': legacy.get('mode', 'sequence'),
                   'send_type': 'texts', 'source_message': '',
                   'gap': '25', 'interval': '0', 'account_gap': '0'}
        self.directions = [initial]
        self.current_direction = initial['id']
        target = BASE / 'directions.json'
        if target.exists():
            try:
                data = json.loads(target.read_text('utf-8'))
                directions = data['directions']
                if not isinstance(directions, list) or not directions:
                    raise ValueError()
                ids, names = set(), set()
                for direction in directions:
                    if not isinstance(direction, dict):
                        raise ValueError()
                    for field in ('id', 'name'):
                        if not isinstance(direction.get(field), str) or not direction[field].strip():
                            raise ValueError()
                    if direction['id'] in ids or direction['name'].casefold() in names:
                        raise ValueError()
                    ids.add(direction['id'])
                    names.add(direction['name'].casefold())
                    for field in ('texts', 'accounts'):
                        if not isinstance(direction.get(field), list) or not all(isinstance(v, str) for v in direction[field]):
                            raise ValueError()
                    if direction.get('mode') not in ('sequence', 'random'):
                        raise ValueError()
                    if direction.get('send_type', 'texts') not in ('texts', 'forward'):
                        raise ValueError()
                    if not isinstance(direction.get('source_message', ''), str):
                        raise ValueError()
                    direction['send_type'] = direction.get('send_type', 'texts')
                    direction['source_message'] = direction.get('source_message', '')
                    for field in ('gap', 'interval', 'account_gap'):
                        if not isinstance(direction.get(field), str):
                            raise ValueError()
                self.directions = directions
                self.current_direction = data.get('active') if data.get('active') in ids else directions[0]['id']
            except (OSError, ValueError, KeyError, TypeError):
                self.direction_save_blocked = True
                messagebox.showerror('Направления', 'Не удалось прочитать directions.json. Файл сохранён без изменений. '
                                     'Исправьте или переименуйте его и перезапустите программу для сохранения направлений.')
        self.show_direction()

    def direction(self):
        return next(d for d in self.directions if d['id'] == self.current_direction)

    def capture_direction(self):
        direction = self.direction()
        # Retain saved accounts that have not appeared in the current scan yet.
        accounts = [k for k in direction['accounts'] if k not in self.rows]
        direction.update(accounts=accounts + self.selected(),
                         texts=[e.get('1.0', 'end-1c') for e in self.editors],
                         mode='random' if self.mode.current() == 1 else 'sequence',
                         send_type='forward' if self.delivery_mode.current() == 1 else 'texts',
                         source_message=self.source_message.get().strip(),
                         gap=self.gap.get(), interval=self.interval.get(), account_gap=self.account_gap.get())

    def restore_direction_accounts(self):
        selected = set(self.direction()['accounts'])
        self.accounts.selection_set([row for key, row in self.rows.items() if key in selected])
        self.update_account_count()
        self.refresh_direction_labels()

    def show_direction(self):
        direction = self.direction()
        self.direction_choice.configure(values=[d['name'] for d in self.directions])
        self.direction_choice.current(self.directions.index(direction))
        for editor in self.editors:
            frame = editor.master
            self.notebook.forget(frame)
            frame.destroy()
        self.editors = []
        for value in direction['texts'] or ['']:
            self.add_text(value)
        self.mode.current(1 if direction['mode'] == 'random' else 0)
        self.delivery_mode.current(1 if direction.get('send_type') == 'forward' else 0)
        self.source_message.delete(0, 'end')
        self.source_message.insert(0, direction.get('source_message', ''))
        for field in ('gap', 'interval', 'account_gap'):
            entry = getattr(self, field)
            entry.delete(0, 'end')
            entry.insert(0, direction[field])
        self.restore_direction_accounts()

    def change_direction(self, event=None):
        index = self.direction_choice.current()
        self.capture_direction()
        self.current_direction = self.directions[index]['id']
        self.show_direction()
        self.save_directions()

    def direction_name(self, title, initial=''):
        name = simpledialog.askstring(title, 'Название направления:', initialvalue=initial, parent=self)
        if name is None:
            return None
        name = name.strip()
        if not name or any(d['name'].casefold() == name.casefold() and d['id'] != self.current_direction
                           for d in self.directions) or (title == 'Новое направление' and name.casefold() == self.direction()['name'].casefold()):
            messagebox.showerror('Направления', 'Укажите непустое уникальное название')
            return None
        return name

    def add_direction(self):
        name = self.direction_name('Новое направление')
        if name is None:
            return
        self.capture_direction()
        direction = {'id': uuid.uuid4().hex, 'name': name, 'accounts': [], 'texts': [''],
                     'mode': 'sequence', 'send_type': 'texts', 'source_message': '',
                     'gap': '25', 'interval': '0', 'account_gap': '0'}
        self.directions.append(direction)
        self.current_direction = direction['id']
        self.show_direction()
        self.save_directions()

    def rename_direction(self):
        name = self.direction_name('Переименовать направление', self.direction()['name'])
        if name is not None:
            self.capture_direction()
            self.direction()['name'] = name
            self.show_direction()
            self.save_directions()

    def delete_direction(self):
        if len(self.directions) == 1:
            messagebox.showinfo('Направления', 'Должно остаться хотя бы одно направление')
            return
        if (self.current_direction in self.running_directions.values() or
                self.current_direction in list(self.engine.job_directions.values())):
            messagebox.showinfo('Направления', 'Перед удалением остановите рассылку этого направления')
            return
        if not messagebox.askyesno('Удалить направление', f'Удалить «{self.direction()["name"]}» и его тексты?', parent=self):
            return
        self.directions.remove(self.direction())
        self.current_direction = self.directions[0]['id']
        self.show_direction()
        self.save_directions()

    def update_account_count(self, event=None):
        self.selection_count.configure(text=f'Выбрано: {len(self.selected())}')
        if hasattr(self, 'current_direction'):
            self.capture_direction()
            self.refresh_direction_labels()

    def refresh_direction_labels(self):
        for key, row in self.rows.items():
            owner = self.running_directions.get(key)
            if owner:
                name = next((d['name'] for d in self.directions if d['id'] == owner), 'Рассылка')
                label = f'▶ {name}'
            else:
                label = ', '.join(d['name'] for d in self.directions if key in d['accounts'])
            self.accounts.set(row, 'direction', label)

    def select_direction_accounts(self):
        try:
            count = int(self.account_count.get())
            if count <= 0:
                raise ValueError()
        except ValueError:
            messagebox.showerror('Аккаунты', 'Количество должно быть целым числом больше нуля')
            return
        assigned_elsewhere = {k for d in self.directions if d['id'] != self.current_direction for k in d['accounts']}
        available = [row for key, row in self.rows.items() if key in self.engine.clients
                     and key not in assigned_elsewhere and key not in self.running_directions]
        if len(available) < count:
            messagebox.showinfo('Аккаунты', f'Доступно свободных аккаунтов: {len(available)}. Запрошено: {count}.')
            return
        self.accounts.selection_set(available[:count])
        self.capture_direction()
        self.update_account_count()
        self.refresh_direction_labels()

    def save_directions(self):
        if self.direction_save_blocked:
            return False
        try:
            target = BASE / 'directions.json'
            temp = target.with_suffix('.tmp')
            data = {'version': 2, 'active': self.current_direction, 'directions': self.directions}
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), 'utf-8')
            temp.replace(target)
            self.refresh_direction_labels()
            return True
        except OSError as exc:
            messagebox.showerror('Ошибка', f'Не удалось сохранить настройки: {target}\n{exc}')
            return False

    def save_texts(self):
        self.capture_direction()
        if self.save_directions():
            self.events.put(('log', 'Все направления, аккаунты, тексты, ссылки и паузы сохранены'))

    def start(self):
        self.capture_direction()
        direction = self.direction()
        keys = direction['accounts']
        texts = self.text_values()
        try:
            gap, interval, account_gap = (float(field.get()) for field in
                                          (self.gap, self.interval, self.account_gap))
            import math
            if not all(math.isfinite(v) and v >= 0 for v in (gap, interval, account_gap)):
                raise ValueError()
        except ValueError:
            messagebox.showerror('Ошибка', 'Паузы должны быть конечными числами от 0')
            return
        if not keys or any(k not in self.engine.clients for k in keys):
            messagebox.showinfo('Отправка', 'Выберите подключённые аккаунты')
            return
        if direction.get('send_type') == 'forward':
            try:
                source_message_reference(direction.get('source_message', ''))
            except ValueError as exc:
                messagebox.showinfo('Отправка', str(exc))
                return
        elif not texts:
            messagebox.showinfo('Отправка', 'Добавьте хотя бы один текст')
            return
        self.capture_direction()
        self.save_directions()
        if direction.get('send_type') == 'forward':
            self.engine.submit(self.engine.start_many_forward(
                keys, direction['source_message'], gap, interval, account_gap,
                self.current_direction, direction['name']))
        else:
            mode = 'random' if self.mode.current() == 1 else 'sequence'
            self.engine.submit(self.engine.start_many(
                keys, texts, mode, gap, interval, account_gap,
                self.current_direction, direction['name']))

    def poll(self):
        keep_polling = True
        try:
            keep_polling = App._poll_events(self) is not False
        except Exception:
            reporter = getattr(self, 'report_callback_exception', None)
            if reporter:
                reporter(*sys.exc_info())
        finally:
            diagnostics = getattr(self, 'runtime_diagnostics', None)
            if diagnostics:
                diagnostics.heartbeat()
            if keep_polling and not getattr(self, '_runtime_closed', False):
                self.after(10 if not self.events.empty() else 100, self.poll)

    def _poll_events(self):
        # Keep controls responsive even when history produces many matching authors.
        deadline = time.perf_counter() + 0.015
        for _ in range(200):
            if time.perf_counter() >= deadline:
                break
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            logger = getattr(self, 'persist_log_event', None)
            if logger and not isinstance(self.events, EventBuffer):
                logger(kind, value)
            observer = getattr(self, 'observe_dashboard_event', None)
            if observer:
                observer(kind, value)
            if kind == 'dashboard_activity':
                pass
            elif kind.startswith('search_'):
                self.search_event(kind, value)
            elif kind.startswith('likes_'):
                self.likes_event(kind, value)
            elif kind.startswith('parser_'):
                self.parser_event(kind, value)
            elif kind == 'convert_log':
                self.convert_log.config(state='normal')
                self.convert_log.insert('end', str(value) + '\n')
                self.convert_log.see('end')
                self.convert_log.config(state='disabled')
            elif kind == 'convert_done':
                self.converting = False
                self.dashboard_account_total = len(discover(BASE / 'accounts'))
                self.convert_button.config(state='normal')
                self.scan_button.config(state='normal')
                self.convert_status.config(text='Операция завершена. Результат указан ниже; затем загрузите аккаунты на первой вкладке.')
            elif kind == 'scan_done':
                self.dashboard_account_total = len(discover(BASE / 'accounts'))
                self.refresh_search_accounts()
                self.refresh_parser_accounts()
                self.refresh_likes_accounts()
                self.busy = False
                self.scan_button.config(state='normal')
                self.restore_direction_accounts()
            elif kind == 'channel_join_done':
                self.channel_join_future = None
                self.channel_join_button.config(state='normal')
            elif kind == 'failed_chat':
                self.record_failed_chat(value)
            elif kind == 'failed_chat_left':
                self.remove_failed_chat(value)
            elif kind == 'failed_chat_leave_error':
                self.schedule_failed_chats_ui_refresh()
            elif kind == 'failed_chat_leave_done':
                self.failed_leave_future = None
                window = self.failed_chats_window
                if window and window.winfo_exists():
                    self.failed_leave_selected_button.configure(state='normal')
                    self.failed_leave_all_button.configure(state='normal')
                    self.failed_chats_status.configure(text='Готово')
                    self.refresh_failed_chats_window()
            elif kind == 'direction_job':
                key, owner = value
                if owner is None:
                    self.running_directions.pop(key, None)
                else:
                    self.running_directions[key] = owner
                self.refresh_direction_labels()
            elif kind == 'direction_error':
                messagebox.showinfo('Запуск направления', value)
            elif kind == 'account':
                key, name, status = value
                if key in self.rows:
                    self.accounts.set(self.rows[key], 'name', name)
                    self.accounts.set(self.rows[key], 'status', status)
                else:
                    self.rows[key] = self.accounts.insert('', 'end', text=key, values=(name, '', status))
            elif kind == 'account_name':
                key, name = value
                if key in self.rows:
                    self.accounts.set(self.rows[key], 'name', name)
                if key in self.likes_rows:
                    self.likes_accounts.set(self.likes_rows[key], 'name', name)
            elif kind == 'status':
                key, status = value
                if key in self.rows:
                    row = self.rows[key]
                    self.accounts.set(row, 'status', status)
            elif kind in ('account_action', 'account_progress'):
                self.account_window_event(kind, value)
            elif kind == 'closed':
                if hasattr(self, 'log_buffer'):
                    self.log_buffer.append(
                        f'{time.strftime("%Y-%m-%d %H:%M:%S")} [system] Завершение Telegram Manager\n')
                flusher = getattr(self, 'flush_run_log', None)
                if flusher:
                    flusher()
                self.save_failed_chats()
                self.engine.loop.call_soon_threadsafe(self.engine.loop.stop)
                self.destroy()
                return False
            else:
                self.log.config(state='normal')
                self.log.insert('end', str(value) + '\n')
                if int(self.log.index('end-1c').split('.')[0]) > 1000:
                    self.log.delete('1.0', '100.0')
                self.log.see('end')
                self.log.config(state='disabled')
        now = time.monotonic()
        flusher = getattr(self, 'flush_run_log', None)
        if (not getattr(self, 'run_log_writer', None) and flusher
                and getattr(self, 'log_buffer', None) and now >= getattr(self, 'log_flush_due', 0)):
            flusher()
            self.log_flush_due = now + 0.5
        if (getattr(self, 'failed_chats_dirty', False)
                and now >= getattr(self, 'failed_chats_save_due', 0)):
            saved = self.save_failed_chats(background=True)
            self.failed_chats_save_due = now + (5.0 if saved else 60.0)
        writer = getattr(self, 'run_log_writer', None)
        if writer:
            error = writer.take_state_error()
            if error:
                self.failed_chats_dirty = True
                self.failed_chats_save_due = now + 60
                self.persist_log_event('failed_chats_error', f'Ошибка фонового сохранения: {error}')

    def close(self):
        if (self.busy or self.converting or
                (self.failed_leave_future and not self.failed_leave_future.done())):
            messagebox.showinfo('Загрузка', 'Дождитесь завершения текущей операции перед выходом')
            return False
        self.capture_direction()
        if not self.direction_save_blocked and not self.save_directions():
            return False
        if not self.save_failed_chats():
            return False
        installer = getattr(self, '_update_install_ready', None)
        if installer:
            try:
                start_update_helper(installer, BASE)
            except Exception as exc:
                messagebox.showerror('Установка обновления', str(exc), parent=self)
                return False
            self._update_install_ready = None
        for future in self.chatlist_futures.values():
            if not future.done():
                future.cancel()
        if self.channel_join_future and not self.channel_join_future.done():
            self.channel_join_future.cancel()
        f = self.engine.submit(self.engine.close())
        f.add_done_callback(lambda _: self.events.put(('closed', None)))
        return True


if __name__ == '__main__':
    App().mainloop()
