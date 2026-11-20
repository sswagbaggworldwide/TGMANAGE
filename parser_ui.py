"""Parser tab for the existing Tkinter application."""
import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog


class ParserUI:
    def build_parser(self, page):
        self.parser_running = False
        self.parser_loading = False
        self.parser_loaded_key = None
        self.parser_row_data = {}
        self.parser_count_value = 0
        self.parser_settings = {}
        try:
            settings = json.loads((self.engine.parser_base / 'parser_settings.json').read_text('utf-8'))
            if isinstance(settings, dict):
                self.parser_settings = settings
        except (OSError, ValueError):
            pass
        ttk.Label(page, text='Авторы объявлений по ключевым словам', font=('', 14, 'bold')).pack(anchor='w')
        ttk.Label(page, text='Собирает историю и новые сообщения выбранных чатов. Один Telegram ID — одна запись в базе.').pack(anchor='w', pady=4)
        row = ttk.Frame(page)
        row.pack(fill='x')
        ttk.Label(row, text='Аккаунт:').pack(side='left')
        self.parser_account = ttk.Combobox(row, state='readonly', width=44)
        self.parser_account.pack(side='left', padx=5)
        self.parser_account.bind('<<ComboboxSelected>>', self.parser_account_changed)
        self.parser_load_button = ttk.Button(row, text='Загрузить чаты', command=self.parser_load_groups)
        self.parser_load_button.pack(side='left', padx=4)
        self.parser_all_button = ttk.Button(row, text='Выбрать все', command=self.parser_select_all)
        self.parser_all_button.pack(side='left')
        group_frame = ttk.Frame(page)
        group_frame.pack(fill='x', pady=5)
        self.parser_groups = ttk.Treeview(group_frame, columns=('title', 'username'), show='headings', height=4, selectmode='extended')
        self.parser_groups.heading('title', text='Чаты для сбора (Ctrl / Shift для выбора нескольких)')
        self.parser_groups.heading('username', text='Username чата')
        self.parser_groups.column('title', width=650)
        self.parser_groups.column('username', width=200)
        scroll = ttk.Scrollbar(group_frame, command=self.parser_groups.yview)
        self.parser_groups.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.parser_groups.pack(fill='x')
        self.parser_groups.bind('<<TreeviewSelect>>', self.parser_groups_selection)
        self.parser_selection_label = ttk.Label(page, text='Выбрано чатов: 0')
        self.parser_selection_label.pack(anchor='w')
        ttk.Label(page, text='Ключевые слова / фразы, каждое с новой строки. Достаточно любого совпадения; регистр не важен.').pack(anchor='w', pady=(5, 0))
        self.parser_keywords = tk.Text(page, height=2, undo=True, wrap='word')
        keywords = self.parser_settings.get('keywords', [])
        if isinstance(keywords, list) and all(isinstance(word, str) for word in keywords):
            self.parser_keywords.insert('1.0', '\n'.join(keywords))
        self.parser_keywords.pack(fill='x', pady=3)
        row = ttk.Frame(page)
        row.pack(fill='x')
        self.parser_history = tk.BooleanVar(value=bool(self.parser_settings.get('history', True)))
        self.parser_history_check = ttk.Checkbutton(row, text='Собрать историю при старте', variable=self.parser_history)
        self.parser_history_check.pack(side='left')
        ttk.Label(row, text='Последних сообщений на чат (0 = вся история):').pack(side='left', padx=8)
        self.parser_limit = ttk.Entry(row, width=9)
        self.parser_limit.insert(0, str(self.parser_settings.get('history_limit', 1000)))
        self.parser_limit.pack(side='left')
        row = ttk.Frame(page)
        row.pack(fill='x', pady=6)
        self.parser_start_button = ttk.Button(row, text='Включить парсер', command=self.parser_begin)
        self.parser_start_button.pack(side='left')
        self.parser_stop_button = ttk.Button(row, text='Стоп', state='disabled', command=self.parser_cancel)
        self.parser_stop_button.pack(side='left', padx=5)
        ttk.Button(row, text='TXT: @username', command=lambda: self.parser_export('txt')).pack(side='left', padx=5)
        ttk.Button(row, text='CSV: вся база', command=lambda: self.parser_export('csv')).pack(side='left')
        ttk.Button(row, text='Обновить базу', command=lambda: self.engine.submit(self.engine.parser_snapshot())).pack(side='left', padx=5)
        ttk.Button(row, text='Копировать username', command=self.parser_copy_username).pack(side='left')
        self.parser_counter = ttk.Label(page, text='В базе: 0 пользователей')
        self.parser_counter.pack(anchor='w')
        table_frame = ttk.Frame(page)
        table_frame.pack(fill='both', expand=True, pady=4)
        columns = ('username', 'id', 'name', 'chat', 'keyword', 'matches', 'date')
        self.parser_table = ttk.Treeview(table_frame, columns=columns, show='headings', height=5, selectmode='browse')
        for col, title, width in zip(columns, ('Username', 'Telegram ID', 'Имя', 'Чат', 'Ключ', 'Объявлений', 'Дата UTC'),
                                     (125, 110, 130, 190, 100, 80, 145)):
            self.parser_table.heading(col, text=title)
            self.parser_table.column(col, width=width)
        scroll = ttk.Scrollbar(table_frame, command=self.parser_table.yview)
        self.parser_table.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.parser_table.pack(fill='both', expand=True)
        self.parser_table.bind('<<TreeviewSelect>>', self.parser_show_record)
        self.parser_detail = tk.Text(page, height=2, wrap='word', state='disabled')
        self.parser_detail.pack(fill='x')
        self.parser_status_label = ttk.Label(page, text='Загрузите аккаунт на первой вкладке, затем выберите чаты и ключевые слова.', wraplength=950)
        self.parser_status_label.pack(anchor='w', pady=4)
        self.parser_log_widget = tk.Text(page, height=2, state='disabled', wrap='word')
        self.parser_log_widget.pack(fill='x')
        self.engine.submit(self.engine.parser_snapshot())

    def refresh_parser_accounts(self):
        selected = self.parser_account.get()
        keys = list(self.engine.clients)
        self.parser_account.configure(values=keys)
        if selected in keys:
            self.parser_account.set(selected)
        elif self.parser_settings.get('account') in keys:
            self.parser_account.set(self.parser_settings['account'])
        elif keys:
            self.parser_account.current(0)

    def parser_account_changed(self, event=None):
        if not self.parser_running and not self.parser_loading:
            for item in self.parser_groups.get_children():
                self.parser_groups.delete(item)
            self.parser_loaded_key = None
            self.parser_selection_label.configure(text='Выбрано чатов: 0')

    def parser_select_all(self):
        if not self.parser_running and not self.parser_loading:
            self.parser_groups.selection_set(self.parser_groups.get_children())

    def parser_groups_selection(self, event=None):
        if self.parser_running:
            # Freeze selection while the worker uses its startup snapshot.
            if tuple(self.parser_groups.selection()) != self.parser_fixed_selection:
                self.parser_groups.selection_set(self.parser_fixed_selection)
        self.parser_selection_label.configure(text=f'Выбрано чатов: {len(self.parser_groups.selection())}')

    def parser_lock_controls(self):
        locked = self.parser_running or self.parser_loading
        state = 'disabled' if locked else 'normal'
        for widget in (self.parser_load_button, self.parser_all_button, self.parser_history_check, self.parser_limit, self.parser_keywords, self.parser_start_button):
            widget.configure(state=state)
        self.parser_account.configure(state='disabled' if locked else 'readonly')
        self.parser_stop_button.configure(state='normal' if locked else 'disabled')

    def parser_load_groups(self):
        if self.parser_running or self.parser_loading:
            return
        key = self.parser_account.get()
        if key not in self.engine.clients:
            messagebox.showinfo('Аккаунт', 'Сначала загрузите аккаунт на первой вкладке.')
            return
        self.parser_loading = True
        self.parser_lock_controls()
        self.parser_status_label.configure(text='Загружаю чаты…')
        self.engine.submit(self.engine.load_parser_groups(key))

    def parser_begin(self):
        if self.parser_running or self.parser_loading:
            return
        key = self.parser_account.get()
        selected = self.parser_groups.selection()
        keywords = list(dict.fromkeys(word.strip() for word in self.parser_keywords.get('1.0', 'end').splitlines() if word.strip()))
        if key != self.parser_loaded_key or not selected or not keywords:
            messagebox.showinfo('Парсер', 'Загрузите чаты, выберите нужные и укажите ключевые слова.')
            return
        try:
            limit = int(self.parser_limit.get().strip())
            if limit < 0:
                raise ValueError()
        except ValueError:
            messagebox.showerror('История', 'Укажите целое число от 0. Ноль означает всю доступную историю.')
            return
        settings = dict(account=key, chats=[int(item) for item in selected], keywords=keywords,
                        history=self.parser_history.get(), history_limit=limit)
        try:
            target = self.engine.parser_base / 'parser_settings.json'
            temporary = target.with_suffix('.tmp')
            temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), 'utf-8')
            temporary.replace(target)
            self.parser_settings = settings
        except OSError:
            messagebox.showerror('Настройки', 'Не удалось сохранить настройки парсера.')
            return
        self.parser_fixed_selection = tuple(selected)
        self.parser_running = True
        self.parser_lock_controls()
        self.engine.submit(self.engine.start_parser(key, settings['chats'], keywords, settings['history'], limit))

    def parser_cancel(self):
        self.parser_stop_button.configure(state='disabled')
        self.engine.submit(self.engine.stop_parser())

    def parser_export(self, kind):
        target = filedialog.asksaveasfilename(defaultextension='.' + kind, initialfile='telegram_users.' + kind,
                                            filetypes=[('База пользователей', '*.' + kind)])
        if target:
            self.engine.submit(self.engine.export_parser(target, kind))

    def parser_copy_username(self):
        selected = self.parser_table.selection()
        if selected:
            username = self.parser_row_data[selected[0]]['username']
            if username:
                self.clipboard_clear()
                self.clipboard_append('@' + username)
            else:
                messagebox.showinfo('Username', 'У этого пользователя нет публичного username. Его ID есть в CSV.')

    def parser_show_record(self, event=None):
        selected = self.parser_table.selection()
        self.parser_detail.configure(state='normal')
        self.parser_detail.delete('1.0', 'end')
        if selected and selected[0] in self.parser_row_data:
            row = self.parser_row_data[selected[0]]
            self.parser_detail.insert('1.0', (row['message_link'] or 'У сообщения нет публичной ссылки') + '\n' + row['message_text'])
        self.parser_detail.configure(state='disabled')

    def parser_put_row(self, row):
        ident = str(row['user_id'])
        self.parser_row_data[ident] = row
        values = ('@' + row['username'] if row['username'] else 'Без username', row['user_id'],
                  (row['first_name'] + ' ' + row['last_name']).strip(), row['chat_title'], row['keyword'],
                  row['matches_count'], row['message_date'].replace('T', ' ').replace('+00:00', ''))
        if self.parser_table.exists(ident):
            self.parser_table.item(ident, values=values)
        else:
            self.parser_table.insert('', 'end', iid=ident, values=values)
        self.parser_table.move(ident, '', 0)
        children = self.parser_table.get_children()
        if len(children) > 1000:
            for item in children[1000:]:
                self.parser_table.delete(item)
                self.parser_row_data.pop(item, None)

    def parser_event(self, kind, value):
        if kind == 'parser_groups':
            self.parser_loaded_key = value['key']
            for item in self.parser_groups.get_children():
                self.parser_groups.delete(item)
            for row in value['rows']:
                self.parser_groups.insert('', 'end', iid=str(row['id']), values=(row['title'], '@' + row['username'] if row['username'] else ''))
            saved = self.parser_settings.get('chats', []) if self.parser_settings.get('account') == value['key'] else []
            if isinstance(saved, list):
                self.parser_groups.selection_set([str(item) for item in saved if self.parser_groups.exists(str(item))])
        elif kind == 'parser_groups_done':
            self.parser_loading = False
            self.parser_lock_controls()
        elif kind == 'parser_snapshot':
            for item in self.parser_table.get_children():
                self.parser_table.delete(item)
            self.parser_row_data.clear()
            for row in reversed(value['rows']):
                self.parser_put_row(row)
            self.parser_count_value = value['count']
            self.parser_counter.configure(text=f'В базе: {value["count"]} пользователей; с username: {value["usernames"]}. На экране до 1000; экспорт — вся база.')
        elif kind == 'parser_row':
            self.parser_put_row(value['row'])
            self.parser_count_value = value['count']
            self.parser_counter.configure(text=f'В базе: {value["count"]} пользователей; новых за запуск: {value["added"]}. На экране до 1000; экспорт — вся база.')
            if self.parser_table.selection() == (str(value['row']['user_id']),):
                self.parser_show_record()
        elif kind == 'parser_status':
            self.parser_status_label.configure(text=value)
        elif kind == 'parser_running':
            self.parser_running = value
            self.parser_lock_controls()
        elif kind == 'parser_log':
            self.parser_log_widget.configure(state='normal')
            self.parser_log_widget.insert('end', str(value) + '\n')
            if int(self.parser_log_widget.index('end-1c').split('.')[0]) > 500:
                self.parser_log_widget.delete('1.0', '100.0')
            self.parser_log_widget.see('end')
            self.parser_log_widget.configure(state='disabled')

