"""Public group discovery; never joins groups or sends messages."""
import asyncio
import csv
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

COLUMNS = ('title', 'link', 'members', 'about', 'query', 'accounts')
HEADERS = ('Название', 'Ссылка', 'Участников', 'Описание', 'Запрос', 'Аккаунты')


def public_group(chat):
    return bool(getattr(chat, 'megagroup', False) and
                not getattr(chat, 'broadcast', False) and
                getattr(chat, 'username', None))


async def search_groups(client, queries, emit):
    from telethon.tl.functions.contacts import SearchRequest
    from telethon.tl.functions.channels import GetFullChannelRequest
    from telethon.errors import FloodWaitError
    seen = set()

    async def request(factory):
        while True:
            try:
                return await asyncio.wait_for(client(factory()), 45)
            except FloodWaitError as exc:
                emit('search_status', f'Ограничение Telegram. Пауза {exc.seconds} сек.; можно нажать Стоп.')
                await asyncio.sleep(exc.seconds + 1)

    for index, query in enumerate(queries, 1):
        emit('search_status', f'Запрос {index}/{len(queries)}: {query}')
        try:
            found = await request(lambda: SearchRequest(q=query, limit=100))
        except Exception as exc:
            emit('search_status', f'{query}: {type(exc).__name__}; запрос пропущен')
            await asyncio.sleep(3)
            continue
        for chat in found.chats:
            if not public_group(chat) or chat.id in seen:
                continue
            seen.add(chat.id)
            row = dict(title=chat.title, link=f'https://t.me/{chat.username}',
                       members=getattr(chat, 'participants_count', None), about='', query=query)
            # Publish immediately so stopping during metadata loading preserves the result.
            emit('search_row', (chat.id, dict(row)))
            try:
                full = await request(lambda: GetFullChannelRequest(channel=chat))
                row['about'] = getattr(full.full_chat, 'about', '')
                row['members'] = getattr(full.full_chat, 'participants_count', row['members'])
                emit('search_row', (chat.id, row))
            except Exception as exc:
                emit('search_status', f'Описание @{chat.username} недоступно: {type(exc).__name__}')
            await asyncio.sleep(2)
        await asyncio.sleep(3)
    emit('search_status', f'Поиск завершён. Уникальных публичных групп: {len(seen)}')


class SearchUI:
    def build_search(self, page):
        self.search_running = False
        self.search_results = {}
        ttk.Label(page, text='Публичные группы по ключевым словам', font=('', 14, 'bold')).pack(anchor='w')
        ttk.Label(page, text='Сначала загрузите аккаунты на первой вкладке. Telegram выдаёт ограниченную подборку, не полный каталог.').pack(anchor='w', pady=5)
        row = ttk.Frame(page)
        row.pack(fill='x')
        ttk.Label(row, text='Аккаунты для поиска:').pack(side='left')
        ttk.Button(row, text='Выбрать все', command=lambda: self.select_search_accounts(True)).pack(side='left', padx=5)
        ttk.Button(row, text='Снять выбор', command=lambda: self.select_search_accounts(False)).pack(side='left')
        self.search_account_frame = ttk.Frame(page)
        self.search_account_frame.pack(fill='x', pady=5)
        self.search_account_vars = {}
        self.search_account_buttons = []
        ttk.Label(self.search_account_frame, text='Загрузите аккаунты на первой вкладке.').pack(anchor='w')
        ttk.Label(page, text='Запросы, каждый с новой строки (от 3 символов):').pack(anchor='w', pady=5)
        self.search_queries = tk.Text(page, height=6, undo=True)
        self.search_queries.pack(fill='x')
        buttons = ttk.Frame(page)
        buttons.pack(fill='x', pady=8)
        self.search_start_button = ttk.Button(buttons, text='Начать поиск', command=self.begin_search)
        self.search_start_button.pack(side='left')
        self.search_stop_button = ttk.Button(buttons, text='Стоп', state='disabled', command=self.cancel_search)
        self.search_stop_button.pack(side='left', padx=5)
        for label, ext in [('Excel', '.xlsx'), ('CSV', '.csv'), ('TXT со ссылками', '.txt')]:
            ttk.Button(buttons, text=label, command=lambda e=ext: self.export_search(e)).pack(side='left', padx=4)
        self.search_count = ttk.Label(page, text='Найдено: 0')
        self.search_count.pack(anchor='w')
        frame = ttk.Frame(page)
        frame.pack(fill='both', expand=True)
        self.search_table = ttk.Treeview(frame, columns=COLUMNS, show='headings')
        for col, label in zip(COLUMNS, HEADERS):
            self.search_table.heading(col, text=label)
            self.search_table.column(col, width=180 if col != 'members' else 85)
        scroll = ttk.Scrollbar(frame, command=self.search_table.yview)
        self.search_table.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.search_table.pack(fill='both', expand=True)
        self.search_status = ttk.Label(page, text='Готов к поиску', wraplength=950)
        self.search_status.pack(anchor='w', pady=8)

    def refresh_search_accounts(self):
        previous = {key: var.get() for key, var in self.search_account_vars.items()}
        for widget in self.search_account_frame.winfo_children():
            widget.destroy()
        self.search_account_vars = {}
        self.search_account_buttons = []
        for index, key in enumerate(self.engine.clients):
            var = tk.BooleanVar(value=previous.get(key, False))
            self.search_account_vars[key] = var
            button = ttk.Checkbutton(self.search_account_frame, text=f'{key} — В работе', variable=var)
            button.grid(row=index // 3, column=index % 3, sticky='w', padx=5, pady=2)
            self.search_account_buttons.append(button)
        self.lock_search_accounts(self.search_running)

    def select_search_accounts(self, enabled):
        if not self.search_running:
            for var in self.search_account_vars.values():
                var.set(enabled)

    def lock_search_accounts(self, locked):
        for button in self.search_account_buttons:
            button.configure(state='disabled' if locked else 'normal')

    def begin_search(self):
        if self.search_running:
            return
        keys = [key for key, var in self.search_account_vars.items() if var.get()]
        queries = list(dict.fromkeys(q.strip() for q in self.search_queries.get('1.0', 'end').splitlines() if q.strip()))
        if not keys or any(key not in self.engine.clients for key in keys) or not queries or any(len(q) < 3 for q in queries):
            messagebox.showinfo('Поиск', 'Выберите подключённые аккаунты и введите запросы от 3 символов.')
            return
        self.search_results.clear()
        for item in self.search_table.get_children():
            self.search_table.delete(item)
        self.search_count.configure(text='Найдено: 0')
        self.search_running = True
        self.lock_search_accounts(True)
        self.search_start_button.configure(state='disabled')
        self.search_stop_button.configure(state='normal')
        self.engine.submit(self.engine.search_groups(keys, queries))

    def cancel_search(self):
        self.search_stop_button.configure(state='disabled')
        self.engine.submit(self.engine.stop_search())

    def search_event(self, kind, value):
        if kind == 'search_row':
            ident, row = value
            previous = self.search_results.get(ident, {})
            accounts = set(previous.get('accounts', '').split('; ')) | {row.get('accounts', '')}
            row['accounts'] = '; '.join(sorted(accounts - {''}))
            for field in ('about', 'members'):
                if row.get(field) in ('', None) and previous.get(field) not in ('', None):
                    row[field] = previous[field]
            queries = set(previous.get('query', '').split('; ')) | {row['query']}
            row['query'] = '; '.join(sorted(queries - {''}))
            self.search_results[ident] = row
            values = tuple('' if row[c] is None else row[c] for c in COLUMNS)
            if self.search_table.exists(str(ident)):
                self.search_table.item(str(ident), values=values)
            else:
                self.search_table.insert('', 'end', iid=str(ident), values=values)
            self.search_count.configure(text=f'Найдено: {len(self.search_results)}')
        elif kind == 'search_status':
            self.search_status.configure(text=value)
        elif kind == 'search_done':
            self.search_running = False
            self.lock_search_accounts(False)
            self.search_start_button.configure(state='normal')
            self.search_stop_button.configure(state='disabled')

    def export_search(self, ext):
        if not self.search_results:
            messagebox.showinfo('Экспорт', 'Сначала найдите группы.')
            return
        target = filedialog.asksaveasfilename(defaultextension=ext, initialfile='telegram_groups' + ext,
                                            filetypes=[('Результаты поиска', '*' + ext)])
        if not target:
            return
        rows = list(self.search_results.values())
        try:
            if ext == '.txt':
                Path(target).write_text('\n'.join(r['link'] for r in rows) + '\n', encoding='utf-8')
            elif ext == '.csv':
                with open(target, 'w', encoding='utf-8-sig', newline='') as stream:
                    writer = csv.writer(stream, delimiter=';')
                    writer.writerow(HEADERS)
                    for row in rows:
                        writer.writerow(safe_cell(row[c]) for c in COLUMNS)
            else:
                from openpyxl import Workbook
                from openpyxl.styles import Font
                book = Workbook()
                sheet = book.active
                sheet.title = 'Группы'
                sheet.append(HEADERS)
                for row in rows:
                    sheet.append([safe_cell(row[c]) for c in COLUMNS])
                for cell in sheet[1]:
                    cell.font = Font(bold=True)
                sheet.freeze_panes = 'A2'
                sheet.auto_filter.ref = sheet.dimensions
                for letter, width in zip('ABCDEF', (35, 40, 15, 70, 30, 45)):
                    sheet.column_dimensions[letter].width = width
                book.save(target)
            self.search_status.configure(text=f'Экспортировано групп: {len(rows)}')
        except Exception as exc:
            messagebox.showerror('Экспорт', f'Не удалось сохранить файл: {type(exc).__name__}')


def safe_cell(value):
    # Treat third-party text as text, not spreadsheet formulas.
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
        return "'" + value
    return value
