"""Keyword-based collection of message authors. This module never sends messages."""
import asyncio
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import tempfile
import unicodedata
from update_control import LiveUpdateLease


def normalize_text(value):
    """Case-insensitive phrases, consistent spaces and Russian е/ё."""
    return ' '.join(unicodedata.normalize('NFKC', value or '').casefold().replace('ё', 'е').split())


class KeywordFilter:
    def __init__(self, keywords):
        self.keywords = tuple(dict.fromkeys(normalize_text(word) for word in keywords if normalize_text(word)))
        if not self.keywords:
            raise ValueError('Укажите хотя бы одно ключевое слово или фразу')

    def match(self, text):
        text = normalize_text(text)
        return next((word for word in self.keywords if word in text), None)


def utc_text(value=None):
    if value is None:
        value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec='seconds')


def message_link(dialog, message_id):
    username = getattr(dialog.entity, 'username', None)
    if username:
        return f'https://t.me/{username}/{message_id}'
    if getattr(dialog.entity, 'megagroup', False):
        return f'https://t.me/c/{dialog.entity.id}/{message_id}'
    return ''


def export_cell(value):
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
        return "'" + value
    return value


class UserStore:
    """One persistent record per Telegram ID, across chats, accounts and runs."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL,
                    first_name TEXT NOT NULL,
                    last_name TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    message_date TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    chat_title TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    message_link TEXT NOT NULL,
                    keyword TEXT NOT NULL,
                    message_text TEXT NOT NULL,
                    matches_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS matches (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL REFERENCES users(user_id),
                    PRIMARY KEY (chat_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS users_seen ON users(last_seen);
            ''')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys = ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def save_match(self, record):
        """Atomic merge; overlapping history/live collection does not inflate counts."""
        now = utc_text()
        with self.connection() as db:
            previous = db.execute('SELECT * FROM users WHERE user_id = ?', (record['user_id'],)).fetchone()
            inserted = previous is None
            if inserted:
                fields = ('user_id', 'username', 'first_name', 'last_name', 'message_date',
                          'chat_id', 'chat_title', 'message_id', 'message_link', 'keyword', 'message_text')
                values = [record[field] for field in fields]
                db.execute('INSERT INTO users (' + ','.join(fields) + ',first_seen,last_seen) VALUES (' +
                           ','.join('?' for _ in values) + ',?,?)', values + [now, now])
            else:
                # The sender profile is current even when the matching post is old.
                db.execute('UPDATE users SET username=?, first_name=?, last_name=?, last_seen=? WHERE user_id=?',
                           (record['username'], record['first_name'], record['last_name'], now, record['user_id']))
                if record['message_date'] >= previous['message_date']:
                    fields = ('message_date', 'chat_id', 'chat_title', 'message_id',
                              'message_link', 'keyword', 'message_text')
                    db.execute('UPDATE users SET ' + ','.join(field + '=?' for field in fields) + ' WHERE user_id=?',
                               [record[field] for field in fields] + [record['user_id']])
            added = db.execute('INSERT OR IGNORE INTO matches VALUES (?, ?, ?)',
                               (record['chat_id'], record['message_id'], record['user_id'])).rowcount
            if added:
                db.execute('UPDATE users SET matches_count=matches_count+1 WHERE user_id=?', (record['user_id'],))
            row = dict(db.execute('SELECT * FROM users WHERE user_id=?', (record['user_id'],)).fetchone())
            count = db.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        return inserted, bool(added), row, count

    def snapshot(self, limit=1000):
        with self.connection() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM users ORDER BY last_seen DESC, user_id DESC LIMIT ?', (limit,))]
            count, usernames = db.execute("SELECT COUNT(*), COUNT(NULLIF(username, '')) FROM users").fetchone()
        return dict(rows=rows, count=count, usernames=usernames)

    def export(self, target, kind):
        target = Path(target)
        if target.resolve() == self.path.resolve():
            raise ValueError('Нельзя экспортировать поверх файла базы')
        if kind not in {'txt', 'csv'}:
            raise ValueError('Неизвестный формат экспорта')
        fd, temporary_name = tempfile.mkstemp(prefix='.' + target.name + '.', suffix='.tmp', dir=target.parent)
        os.close(fd)
        temporary = Path(temporary_name)
        count = 0
        try:
            with self.connection() as db:
                if kind == 'txt':
                    # Deduplicate handles too, including a reassigned username.
                    rows = db.execute("SELECT DISTINCT lower(username) FROM users WHERE username<>'' ORDER BY lower(username)")
                    with temporary.open('w', encoding='utf-8') as stream:
                        for row in rows:
                            stream.write('@' + row[0] + '\n')
                            count += 1
                else:
                    fields = ('user_id', 'username', 'first_name', 'last_name', 'chat_title', 'keyword',
                              'matches_count', 'message_date', 'message_link', 'message_text')
                    with temporary.open('w', encoding='utf-8-sig', newline='') as stream:
                        writer = csv.writer(stream, delimiter=';')
                        writer.writerow(('Telegram ID', 'Username', 'Имя', 'Фамилия', 'Последний чат', 'Ключевое слово',
                                         'Совпавших сообщений', 'Дата объявления UTC', 'Ссылка на сообщение', 'Текст объявления'))
                        for row in db.execute('SELECT * FROM users ORDER BY user_id'):
                            writer.writerow(export_cell(row[field]) for field in fields)
                            count += 1
            # A failed export must not truncate the user's previous export.
            temporary.replace(target)
            return count
        finally:
            temporary.unlink(missing_ok=True)


class UserParser:
    def __init__(self, client, own_id, dialogs, keywords, store, emit, *,
                 history=True, history_limit=1000, update_lease=None):
        if not dialogs:
            raise ValueError('Выберите чаты для парсинга')
        if history_limit < 0:
            raise ValueError('Глубина истории не может быть отрицательной')
        self.client, self.own_id = client, own_id
        self.dialogs = {dialog.id: dialog for dialog in dialogs}
        self.filter = KeywordFilter(keywords)
        self.store, self.emit = store, emit
        self.history, self.history_limit = history, history_limit
        self.update_lease = update_lease
        self.active = False
        self.lock = asyncio.Lock()
        self.live_tasks = set()
        self.abort = asyncio.Event()
        self.abort_reason = ''
        self.scanned = 0
        self.added = 0

    async def request(self, factory):
        from telethon.errors import FloodWaitError
        attempt = 0
        while self.active:
            try:
                return await asyncio.wait_for(factory(), 60)
            except FloodWaitError as exc:
                self.emit('parser_log', f'Пауза Telegram: {exc.seconds} сек. Стоп прерывает ожидание.')
                await asyncio.sleep(exc.seconds + 1)
            except (OSError, asyncio.TimeoutError):
                attempt += 1
                if attempt >= 3:
                    raise
                self.emit('parser_log', f'Временная ошибка связи при чтении автора. Повтор {attempt}/2.')
                await asyncio.sleep(attempt)
        return None

    async def process(self, message, chat_id):
        from telethon.tl.types import User
        if not self.active or chat_id not in self.dialogs or getattr(message, 'out', False):
            return
        keyword = self.filter.match(getattr(message, 'raw_text', '') or '')
        if not keyword:
            return
        # Use the actual posting account, not usernames or forward origins mentioned in the text.
        async with self.lock:
            if not self.active:
                return
            sender = await self.request(message.get_sender)
            if (not isinstance(sender, User) or sender.bot or sender.deleted or
                    sender.id == self.own_id or not self.active):
                return
            dialog = self.dialogs[chat_id]
            record = dict(user_id=sender.id, username=sender.username or '',
                          first_name=sender.first_name or '', last_name=sender.last_name or '',
                          message_date=utc_text(message.date), chat_id=chat_id, chat_title=dialog.name,
                          message_id=message.id, message_link=message_link(dialog, message.id),
                          keyword=keyword, message_text=message.raw_text)
            try:
                inserted, _, row, count = self.store.save_match(record)
            except (sqlite3.Error, OSError) as exc:
                self.abort_reason = f'База недоступна: {type(exc).__name__}. Сбор остановлен. Проверьте файл базы и права записи.'
                self.active = False
                self.abort.set()
                self.emit('parser_log', self.abort_reason)
                return
            self.added += int(inserted)
            self.emit('parser_row', dict(row=row, count=count, added=self.added))

    async def on_new(self, event):
        task = asyncio.current_task()
        self.live_tasks.add(task)
        try:
            await self.process(event.message, event.chat_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.emit('parser_log', f'Новое сообщение пропущено: {type(exc).__name__}')
        finally:
            self.live_tasks.discard(task)

    async def collect_history(self, dialog):
        from telethon.errors import FloodWaitError
        offset = 0
        remaining = self.history_limit
        while self.active:
            try:
                iterator = self.client.iter_messages(dialog.entity, limit=remaining or None,
                                                     offset_id=offset, wait_time=1)
                async for message in iterator:
                    if not self.active:
                        return
                    try:
                        await self.process(message, dialog.id)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.emit('parser_log', f'{dialog.name}: сообщение {message.id} пропущено: {type(exc).__name__}')
                    offset = message.id
                    self.scanned += 1
                    if self.scanned % 100 == 0:
                        self.emit('parser_status', f'История: проверено {self.scanned} сообщений; новых авторов: {self.added}. Новые сообщения тоже собираются.')
                    if self.history_limit:
                        remaining -= 1
                        if remaining <= 0:
                            return
                return
            except FloodWaitError as exc:
                self.emit('parser_log', f'{dialog.name}: пауза Telegram {exc.seconds} сек.; история продолжится с последнего сообщения.')
                await asyncio.sleep(exc.seconds + 1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.emit('parser_log', f'История {dialog.name} недоступна: {type(exc).__name__}. Мониторинг остаётся включён.')
                return

    async def collect_and_monitor(self):
        if self.history:
            for dialog in self.dialogs.values():
                if not self.active:
                    return
                self.emit('parser_status', f'Читаю историю: {dialog.name}. Сбор новых сообщений уже включён.')
                await self.collect_history(dialog)
            self.emit('parser_log', f'История завершена. Проверено: {self.scanned}; новых авторов за запуск: {self.added}.')
        if self.active:
            self.emit('parser_status', 'Сбор новых сообщений включён. В базу попадают только авторы сообщений с ключевыми словами.')
            await self.abort.wait()

    async def run(self):
        from telethon import events
        builder = events.NewMessage(chats=[dialog.entity for dialog in self.dialogs.values()], incoming=True)
        self.active = True
        reader = disconnected = None
        attached = False
        stop_reason = 'Парсер остановлен. Собранная база сохранена.'
        try:
            self.client.add_event_handler(self.on_new, builder)
            attached = True
            if self.update_lease:
                await self.update_lease.start()
            self.emit('parser_running', True)
            self.emit('parser_log', f'Парсер включён. Чатов: {len(self.dialogs)}. Ключи: {", ".join(self.filter.keywords)}')
            reader = asyncio.create_task(self.collect_and_monitor())
            # Shield the client's shared future: stopping the parser must not cancel it.
            disconnected = asyncio.ensure_future(asyncio.shield(self.client.disconnected))
            done, _ = await asyncio.wait((reader, disconnected), return_when=asyncio.FIRST_COMPLETED)
            if disconnected in done:
                stop_reason = 'Соединение с Telegram завершилось. Парсер остановлен; загрузите аккаунт и включите парсер снова.'
                self.emit('parser_log', stop_reason)
                try:
                    disconnected.result()
                except Exception as exc:
                    self.emit('parser_log', f'Причина отключения: {type(exc).__name__}')
            else:
                await reader
                stop_reason = self.abort_reason or stop_reason
        finally:
            self.active = False
            if attached:
                self.client.remove_event_handler(self.on_new, builder)
            tasks = list(self.live_tasks) + [task for task in (reader, disconnected) if task is not None]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if self.update_lease:
                await self.update_lease.close()
            self.emit('parser_running', False)
            self.emit('parser_status', stop_reason)
            self.emit('parser_log', 'Сбор остановлен; новые сообщения больше не обрабатываются.')


class ParserEngine:
    """Mixin: all database and Telegram operations run in the existing engine loop."""
    def init_parser(self):
        self.parser_task = None
        self.parser_groups_task = None
        self.parser_dialogs = {}
        self.parser_store = None
        self.parser_account_key = None

    def get_parser_store(self):
        if self.parser_store is None:
            self.parser_store = UserStore(self.parser_base / 'parser_users.sqlite3')
        return self.parser_store

    async def parser_snapshot(self):
        try:
            self.emit('parser_snapshot', self.get_parser_store().snapshot())
        except Exception as exc:
            self.emit('parser_log', f'База недоступна: {type(exc).__name__}')

    async def load_parser_groups(self, key):
        from telethon.errors import FloodWaitError
        if self.parser_groups_task and not self.parser_groups_task.done():
            return
        self.parser_groups_task = asyncio.current_task()
        try:
            if key not in self.clients:
                raise ValueError('Сначала подключите аккаунт')
            while True:
                try:
                    dialogs = await asyncio.wait_for(self.clients[key][0].get_dialogs(), 60)
                    break
                except FloodWaitError as exc:
                    self.emit('parser_status', f'Загрузка чатов: пауза Telegram {exc.seconds} сек. Можно нажать Стоп.')
                    await asyncio.sleep(exc.seconds + 1)
            groups = {dialog.id: dialog for dialog in dialogs if dialog.is_group}
            self.parser_dialogs[key] = groups
            rows = [dict(id=dialog.id, title=dialog.name,
                         username=getattr(dialog.entity, 'username', None) or '') for dialog in groups.values()]
            self.emit('parser_groups', dict(key=key, rows=rows))
            self.emit('parser_status', f'Загружено чатов: {len(rows)}. Выберите нужные чаты и нажмите «Включить парсер».')
        except asyncio.CancelledError:
            self.emit('parser_status', 'Загрузка чатов остановлена.')
            raise
        except Exception as exc:
            detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            self.emit('parser_log', f'Чаты не загружены: {detail}')
        finally:
            self.emit('parser_groups_done', None)

    async def start_parser(self, key, chat_ids, keywords, history, limit):
        if self.parser_task and not self.parser_task.done():
            self.emit('parser_log', 'Парсер уже работает. Для изменения настроек остановите его.')
            return
        try:
            if key not in self.clients:
                raise ValueError('Аккаунт не подключён')
            groups = self.parser_dialogs.get(key, {})
            if not chat_ids or any(chat_id not in groups for chat_id in chat_ids):
                raise ValueError('Загрузите чаты выбранного аккаунта и выберите нужные')
            client, own_id = self.clients[key]
            if not client.is_connected():
                raise ValueError('Аккаунт отключён. Нажмите «Загрузить аккаунты» на первой вкладке.')
            parser = UserParser(client, own_id, [groups[chat_id] for chat_id in chat_ids], keywords,
                                self.get_parser_store(), self.emit, history=history, history_limit=limit,
                                update_lease=LiveUpdateLease(self, key, 'parser'))
            self.parser_task = asyncio.create_task(parser.run())
            self.parser_account_key = key
            def finished(task):
                if self.parser_task is task:
                    self.parser_account_key = None
                    # Also restore the UI if Stop cancelled the task before run() began.
                    self.emit('parser_running', False)
                if not task.cancelled() and task.exception():
                    self.emit('parser_log', f'Парсер завершился с ошибкой: {type(task.exception()).__name__}')
                    self.emit('parser_status', 'Ошибка запуска парсера. Проверьте журнал.')
            self.parser_task.add_done_callback(finished)
        except Exception as exc:
            detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            self.emit('parser_log', detail)
            self.emit('parser_running', False)

    async def stop_parser(self):
        tasks = [task for task in (self.parser_task, self.parser_groups_task) if task and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def export_parser(self, target, kind):
        try:
            count = self.get_parser_store().export(target, kind)
            description = 'username' if kind == 'txt' else 'пользователей'
            self.emit('parser_log', f'Экспортировано {description}: {count}. Файл: {target}')
            self.emit('parser_status', f'Экспорт завершён: {count} записей.')
        except Exception as exc:
            self.emit('parser_log', f'Экспорт не удался: {type(exc).__name__}')

