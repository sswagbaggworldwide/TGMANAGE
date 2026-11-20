"""Selected-account bot worker. Unknown bot screens require manual attention."""
import asyncio
import json
import hashlib
import math
import time
from datetime import datetime, timezone
from tkinter import ttk, messagebox
import tkinter as tk
from update_control import LiveUpdateLease

BOT = 'leomatchbot'
DAY = 24 * 60 * 60


def normalized(text):
    return ' '.join((text or '').casefold().replace('ё', 'е').split())


def button_key(text):
    return ''.join(normalized(text).replace('\ufe0f', '').replace('\ufe0e', '').split())


def classify(text, settings):
    value = normalized(text)
    if any(normalized(p) in value for p in settings['verification'] if normalized(p)):
        return 'verification'
    if any(normalized(p) in value for p in settings['quota'] if normalized(p)):
        return 'quota'
    return 'other'


def validate_settings(settings):
    result = dict(settings)
    result.setdefault('ad', '👎Не интересно')
    if not isinstance(result['ad'], str) or not result['ad'].strip():
        raise ValueError('Укажите точный текст кнопки пропуска рекламы')
    if button_key(result['ad']) == button_key(result.get('like', '')):
        raise ValueError('Кнопки лайка и пропуска рекламы должны отличаться')
    for field in ('like', 'resume'):
        if not isinstance(result.get(field), str) or not result[field].strip():
            raise ValueError('Укажите точный текст кнопки лайка и команду нового круга')
        result[field] = result[field].strip()
    for field in ('quota', 'verification'):
        phrases = result.get(field)
        if not isinstance(phrases, list) or not phrases or not all(isinstance(p, str) and p.strip() for p in phrases):
            raise ValueError('Укажите фразы окончания лайков и запроса верификации')
        result[field] = list(phrases)
    result['delay'] = float(result.get('delay', 5))
    if not math.isfinite(result['delay']) or result['delay'] < 1:
        raise ValueError('Пауза между лайками должна быть не меньше 1 секунды')
    return result


def token(message):
    # Edits of the same bot message can contain the next profile.
    value = f'{message.id}:{getattr(message, "edit_date", None)}:{getattr(message, "raw_text", "")}'
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def normal_button(button):
    raw = getattr(button, 'button', None)
    kind = type(getattr(raw, 'type', raw)).__name__
    return kind in ('KeyboardButton', 'KeyboardButtonCallback',
                    'ButtonTypeDefault', 'InlineButtonTypeCallback')


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), 'utf-8')
    temporary.replace(path)


class Attention(Exception):
    pass


class Verification(Attention):
    pass


async def bounded(awaitable, seconds):
    # asyncio.wait_for on Python 3.10 can swallow a cancellation when its
    # awaited operation finishes simultaneously. Stop must also interrupt a day-long wait.
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait((task,), timeout=seconds)
        if not done:
            raise asyncio.TimeoutError()
        return task.result()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class LikesEngine:
    def init_likes(self):
        self.likes_jobs = {}
        self.likes_state = {}
        self.likes_storage_error = None
        path = self.parser_base / 'bot_likes_state.json'
        if path.exists():
            try:
                data = json.loads(path.read_text('utf-8'))
                if not isinstance(data, dict):
                    raise ValueError()
                for key, state in data.items():
                    if not isinstance(key, str) or not isinstance(state, dict):
                        raise ValueError()
                    due = state.get('next_run', 0)
                    if not isinstance(due, (int, float)) or not math.isfinite(due) or due < 0:
                        raise ValueError()
                    if not isinstance(state.get('count', 0), int) or state.get('count', 0) < 0:
                        raise ValueError()
                self.likes_state = data
            except (OSError, ValueError, TypeError):
                self.likes_storage_error = 'Не удалось прочитать bot_likes_state.json. Исправьте или переименуйте файл и перезапустите программу.'

    def likes_report(self, key, status):
        state = self.likes_state.get(key, {})
        self.emit('likes_status', (key, status, state.get('count', 0), state.get('next_run', 0)))
        self.emit('likes_log', f'{key}: {status}')

    def likes_save(self, key, **changes):
        state = dict(self.likes_state.get(key, {}))
        state.update(changes)
        updated = dict(self.likes_state)
        updated[key] = state
        atomic_json(self.parser_base / 'bot_likes_state.json', updated)
        self.likes_state = updated

    async def start_likes(self, keys, settings):
        try:
            settings = validate_settings(settings)
            if self.likes_storage_error:
                raise ValueError(self.likes_storage_error)
            if not keys or any(key not in self.clients for key in keys):
                raise ValueError('Выберите подключённые аккаунты')
        except (ValueError, TypeError) as exc:
            self.emit('likes_error', str(exc))
            return
        for key in dict.fromkeys(keys):
            if key in self.likes_jobs and not self.likes_jobs[key].done():
                self.emit('likes_log', f'{key}: уже работает')
                continue
            self.likes_jobs[key] = asyncio.create_task(self.run_likes(key, settings))
            def finished(task, key=key):
                if not task.cancelled() and task.exception():
                    self.likes_report(key, f'Остановлен: {type(task.exception()).__name__}. '
                                           'Не удалось включить приём событий Telegram.')
            self.likes_jobs[key].add_done_callback(finished)

    async def stop_likes(self, keys=None):
        keys = list(self.likes_jobs) if keys is None else list(keys)
        tasks = [self.likes_jobs[key] for key in keys if key in self.likes_jobs and not self.likes_jobs[key].done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for key in keys:
            status = ('Нужна верификация. Пройдите её вручную в Telegram.'
                      if self.likes_state.get(key, {}).get('needs_verification') else
                      'Остановлен вручную; срок ожидания сохранён')
            self.likes_report(key, status)

    async def run_likes(self, key, settings):
        from telethon import events
        from telethon.errors import FloodWaitError
        wake = asyncio.Event()
        client, builders = None, []
        update_lease = LiveUpdateLease(self, key, 'likes')
        async def changed(event):
            wake.set()
        async def latest():
            messages = await bounded(client.get_messages(bot, limit=20), 30)
            incoming = next((m for m in messages if not m.out), None)
            outgoing = next((m for m in messages if m.out), None)
            return incoming, outgoing
        async def wait_response():
            try:
                await bounded(wake.wait(), 60)
            except asyncio.TimeoutError:
                raise Attention('Нет нового ответа бота. Проверьте диалог и запустите аккаунт снова.')
        def check_verification(message):
            if message and classify(message.raw_text, settings) == 'verification':
                raise Verification('Нужна верификация. Пройдите её вручную в Telegram и запустите аккаунт снова.')
        try:
            self.likes_report(key, 'Подключение к @' + BOT)
            client = await self.ensure_connected(key)
            bot = await bounded(client.get_entity(BOT), 30)
            if not getattr(bot, 'bot', False):
                raise Attention('Адрес @leomatchbot не определён как бот')
            builders = [events.NewMessage(chats=bot, incoming=True), events.MessageEdited(chats=bot, incoming=True)]
            for builder in builders:
                client.add_event_handler(changed, builder)
            await update_lease.start()
            while True:
                due = self.likes_state.get(key, {}).get('next_run', 0)
                if due:
                    self.likes_report(key, 'лайки закончились, начну круг снова когда они возобновятся (через сутки)')
                    while time.time() < due:
                        wake.clear()
                        message, _ = await latest()
                        check_verification(message)
                        try:
                            await bounded(wake.wait(), max(0, due - time.time()))
                        except asyncio.TimeoutError:
                            break
                    # Save before sending: a timeout must never cause an automatic duplicate command.
                    self.likes_save(key, next_run=0)
                    await self.ensure_connected(key)
                    wake.clear()
                    previous, _ = await latest()
                    if previous:
                        check_verification(previous)
                        self.likes_save(key, last_token=token(previous))
                    self.likes_report(key, 'Начинаю новый круг после ожидания')
                    buttons = await bounded(previous.get_buttons(), 30) if previous else None
                    resume = [b for row in (buttons or []) for b in row
                              if button_key(b.text) == button_key(settings['resume']) and normal_button(b)]
                    if len(resume) > 1:
                        raise Attention('Несколько кнопок нового круга. Проверьте диалог вручную.')
                    if resume:
                        await bounded(resume[0].click(), 30)
                    else:
                        await bounded(client.send_message(bot, settings['resume'], parse_mode=None), 30)
                    await wait_response()
                wake.clear()
                await self.ensure_connected(key)
                message, outgoing = await latest()
                if not message:
                    raise Attention('Откройте в боте просмотр анкет вручную, затем запустите аккаунт снова')
                check_verification(message)
                previous_token = self.likes_state.get(key, {}).get('last_token')
                current_token = token(message)
                if current_token == previous_token:
                    await wait_response()
                    continue
                if classify(message.raw_text, settings) == 'quota':
                    self.likes_save(key, next_run=time.time() + DAY, last_token=current_token, needs_verification=False)
                    continue
                if (outgoing and outgoing.id > message.id and button_key(outgoing.raw_text) in
                        {button_key(settings['like']), button_key(settings['ad']), button_key(settings['resume'])}):
                    # An earlier run (or the user) may already have liked this profile.
                    await wait_response()
                    continue
                buttons = await bounded(message.get_buttons(), 30)
                likes = [button for row in (buttons or []) for button in row
                         if button_key(button.text) == button_key(settings['like']) and normal_button(button)]
                ads = [button for row in (buttons or []) for button in row
                       if button_key(button.text) == button_key(settings['ad']) and normal_button(button)]
                if len(likes) + len(ads) != 1:
                    self.emit('likes_log', f'{key}: нераспознанный ответ бота: {message.raw_text[:1500]}')
                    raise Attention('Не распознана анкета с кнопкой лайка. Откройте просмотр анкет вручную и проверьте настройки.')
                action = (likes or ads)[0]
                await asyncio.sleep(settings['delay'])
                wake.clear()
                fresh, _ = await latest()
                check_verification(fresh)
                if not fresh or token(fresh) != current_token:
                    continue
                count = self.likes_state.get(key, {}).get('count', 0)
                self.likes_save(key, last_token=current_token, needs_verification=False)
                try:
                    await bounded(action.click(), 30)
                except FloodWaitError as exc:
                    self.likes_report(key, f'Telegram просит подождать {exc.seconds} сек.; затем проверьте диалог и запустите снова')
                    return
                if likes:
                    self.likes_save(key, count=count + 1)
                self.likes_report(key, 'Лайк отправлен; ожидаю следующую анкету' if likes else
                                  'Реклама пропущена; ожидаю следующую анкету')
                await wait_response()
        except asyncio.CancelledError:
            raise
        except Attention as exc:
            if isinstance(exc, Verification):
                try:
                    self.likes_save(key, needs_verification=True)
                except OSError:
                    self.emit('likes_log', f'{key}: не удалось сохранить пометку верификации на диск')
            self.likes_report(key, str(exc))
            if isinstance(exc, Verification):
                self.emit('likes_attention', (key, str(exc)))
        except asyncio.TimeoutError:
            self.likes_report(key, 'Таймаут: результат действия неизвестен. Проверьте диалог и запустите снова.')
        except OSError:
            self.likes_report(key, 'Не удалось сохранить состояние или выполнить сетевой запрос. Аккаунт остановлен для проверки.')
        except Exception as exc:
            self.likes_report(key, f'Остановлен: {type(exc).__name__}. Проверьте соединение и диалог бота.')
        finally:
            if client:
                for builder in builders:
                    client.remove_event_handler(changed, builder)
            await update_lease.close()


class LikesUI:
    def build_likes(self, page):
        self.likes_rows = {}
        self.likes_saved_accounts = []
        ttk.Label(page, text='@leomatchbot — выбранные аккаунты', font=('', 13, 'bold')).pack(anchor='w')
        ttk.Label(page, text='Сначала откройте просмотр анкет в Telegram. После запроса верификации аккаунт приостанавливается.').pack(anchor='w', pady=5)
        self.likes_accounts = ttk.Treeview(page, columns=('name', 'status', 'count', 'next'), show='tree headings', height=9, selectmode='extended')
        for col, title, width in (('#0', 'Аккаунт / сессия', 210), ('name', 'Имя', 160),
                                  ('status', 'Статус', 440), ('count', 'Лайков отправлено', 115), ('next', 'Следующий круг (UTC)', 165)):
            self.likes_accounts.heading(col, text=title)
            self.likes_accounts.column(col, width=width)
        self.likes_accounts.pack(fill='x')
        self.likes_accounts.tag_configure('attention', foreground='#f39d96')
        self.likes_accounts.bind('<<TreeviewSelect>>', self.likes_selection_changed)
        row = ttk.Frame(page)
        row.pack(fill='x', pady=6)
        ttk.Button(row, text='Выбрать все', command=lambda: self.likes_accounts.selection_set(list(self.likes_rows.values()))).pack(side='left')
        ttk.Button(row, text='Снять выбор', command=lambda: self.likes_accounts.selection_set([])).pack(side='left', padx=5)
        self.likes_count = ttk.Label(row, text='Выбрано: 0 (Ctrl / Shift)')
        self.likes_count.pack(side='left', padx=10)
        self.likes_fields = {}
        labels = [('like', 'Точный текст кнопки лайка'), ('ad', 'Кнопка пропуска рекламы'),
                  ('resume', 'Текст команды / кнопки для нового круга'),
                  ('quota', 'Фразы: лайки закончились (разделитель |)'),
                  ('verification', 'Фразы: нужна верификация (разделитель |)'), ('delay', 'Пауза между лайками, сек.')]
        defaults = {'like': '❤️', 'ad': '👎Не интересно', 'resume': '1🚀', 'quota': ['лайки закончились'],
                    'verification': ['Для верификации анкеты отправь видео-кружок'], 'delay': 5}
        path = self.engine.parser_base / 'bot_likes_settings.json'
        self.likes_settings_blocked = False
        try:
            if path.exists():
                saved = json.loads(path.read_text('utf-8'))
                if not isinstance(saved, dict):
                    raise ValueError()
                defaults.update(saved)
                accounts = saved.get('accounts', [])
                if not isinstance(accounts, list) or not all(isinstance(k, str) for k in accounts):
                    raise ValueError()
                self.likes_saved_accounts = accounts
        except (OSError, ValueError, TypeError):
            self.likes_settings_blocked = True
            self.events.put(('likes_error', 'Не удалось прочитать bot_likes_settings.json. Файл не будет перезаписан.'))
        for field, label in labels:
            row = ttk.Frame(page)
            row.pack(fill='x', pady=3)
            ttk.Label(row, text=label, width=49).pack(side='left')
            entry = ttk.Entry(row)
            value = defaults.get(field, '')
            if isinstance(value, list):
                value = ' | '.join(str(v) for v in value)
            entry.insert(0, str(value))
            entry.pack(side='left', fill='x', expand=True)
            self.likes_fields[field] = entry
        row = ttk.Frame(page)
        row.pack(fill='x', pady=8)
        ttk.Button(row, text='Запустить выбранные', command=self.likes_begin).pack(side='left')
        ttk.Button(row, text='Стоп выбранных', command=lambda: self.engine.submit(self.engine.stop_likes(self.likes_selected()))).pack(side='left', padx=5)
        ttk.Button(row, text='Стоп всех лайков', command=lambda: self.engine.submit(self.engine.stop_likes())).pack(side='left')
        ttk.Button(row, text='Сохранить настройки', command=self.likes_save_settings).pack(side='left', padx=5)
        ttk.Label(page, text='После лимита: ожидание 24 часа. Срок сохраняется при закрытии. После перезапуска загрузите аккаунты и нажмите запуск.').pack(anchor='w')
        self.likes_log = tk.Text(page, height=9, state='disabled', wrap='word')
        self.likes_log.pack(fill='both', expand=True, pady=6)

    def likes_selected(self):
        return [self.likes_accounts.item(row, 'text') for row in self.likes_accounts.selection()]

    def likes_selection_changed(self, event=None):
        self.likes_saved_accounts = [key for key in self.likes_saved_accounts if key not in self.likes_rows] + self.likes_selected()
        self.likes_count.configure(text=f'Выбрано: {len(self.likes_selected())} (Ctrl / Shift)')

    def refresh_likes_accounts(self):
        for key, row in self.rows.items():
            name = self.accounts.set(row, 'name')
            if key not in self.likes_rows:
                self.likes_rows[key] = self.likes_accounts.insert('', 'end', text=key, values=(name, 'Не запущен', 0, ''))
                state = self.engine.likes_state.get(key, {})
                status = 'Нужна верификация. Пройдите её вручную в Telegram.' if state.get('needs_verification') else 'Не запущен'
                self.likes_event('likes_status', (key, status, state.get('count', 0), state.get('next_run', 0)))
            else:
                self.likes_accounts.set(self.likes_rows[key], 'name', name)
        self.likes_accounts.selection_set([row for key, row in self.likes_rows.items() if key in self.likes_saved_accounts])

    def likes_settings(self):
        settings = {key: field.get().strip() for key, field in self.likes_fields.items()}
        for key in ('quota', 'verification'):
            settings[key] = [p.strip() for p in settings[key].split('|') if p.strip()]
        return validate_settings(settings)

    def likes_save_settings(self):
        if self.likes_settings_blocked:
            messagebox.showerror('Лайки', 'Исправьте или переименуйте повреждённый bot_likes_settings.json и перезапустите программу')
            return False
        try:
            self.likes_selection_changed()
            settings = self.likes_settings()
            settings['accounts'] = self.likes_saved_accounts
            atomic_json(self.engine.parser_base / 'bot_likes_settings.json', settings)
            return True
        except (OSError, ValueError, TypeError) as exc:
            messagebox.showerror('Лайки', str(exc))
            return False

    def likes_begin(self):
        keys = self.likes_selected()
        if not keys:
            messagebox.showinfo('Лайки', 'Выберите аккаунты на этой вкладке')
            return
        if self.likes_save_settings():
            self.engine.submit(self.engine.start_likes(keys, self.likes_settings()))

    def likes_event(self, kind, value):
        if kind == 'likes_status':
            key, status, count, due = value
            row = self.likes_rows.get(key)
            if row:
                self.likes_accounts.set(row, 'status', status)
                self.likes_accounts.set(row, 'count', count)
                self.likes_accounts.set(row, 'next', datetime.fromtimestamp(due, timezone.utc).strftime('%d.%m %H:%M') if due else '')
                self.likes_accounts.item(row, tags=('attention',) if 'верификация' in status else ())
        elif kind == 'likes_attention':
            key, status = value
            # Status and journal remain visible without blocking other accounts.
            self.bell()
            self.likes_event('likes_log', f'ВНИМАНИЕ — {key}: {status}')
        elif kind == 'likes_error':
            messagebox.showerror('Лайки', value)
        elif kind == 'likes_log':
            self.likes_log.configure(state='normal')
            self.likes_log.insert('end', str(value) + '\n')
            self.likes_log.see('end')
            if int(self.likes_log.index('end-1c').split('.')[0]) > 1000:
                self.likes_log.delete('1.0', '100.0')
            self.likes_log.configure(state='disabled')
