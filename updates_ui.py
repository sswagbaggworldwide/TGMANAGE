"""Tk update controls; all network and checksum work stays off the GUI thread."""
import json
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox
import webbrowser

from app_updates import check_release, download_release, load_release_config, UpdateError


class UpdatesUI:
    def init_app_updates(self, base):
        self.update_base = Path(base)
        self.release_config = load_release_config()
        self.update_messages = queue.Queue(maxsize=4)
        self.update_busy = False
        self.update_release = None
        self.update_window = None
        self.update_download_progress = None
        self.update_downloaded = None
        self.update_notified = set()
        self.update_status_text = 'Нажмите «Проверить обновления».'
        try:
            settings = json.loads((self.update_base / 'update_settings.json').read_text('utf-8'))
            enabled = settings.get('automatic', True) is not False
        except (OSError, ValueError, AttributeError):
            enabled = True
        self.automatic_updates = tk.BooleanVar(value=enabled)
        self.after(150, self.poll_app_updates)
        self.after(8000, self.automatic_update_check)

    def automatic_update_check(self):
        if getattr(self, '_runtime_closed', False):
            return
        if self.automatic_updates.get():
            self.check_app_updates(manual=False)
        self.after(6 * 60 * 60 * 1000, self.automatic_update_check)

    def save_update_preference(self):
        try:
            path = self.update_base / 'update_settings.json'
            temp = path.with_suffix('.tmp')
            temp.write_text(json.dumps({'automatic': self.automatic_updates.get()}), 'utf-8')
            temp.replace(path)
        except OSError as exc:
            messagebox.showerror('Обновления', f'Не удалось сохранить настройку: {exc}', parent=self)

    def show_updates(self):
        if self.update_window is not None and self.update_window.winfo_exists():
            self.update_window.deiconify()
            self.update_window.lift()
            return
        window = tk.Toplevel(self)
        self.update_window = window
        window.title('Обновления Telegram Manager')
        window.geometry('590x460')
        window.minsize(480, 340)
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill='both', expand=True)
        self.update_heading = ttk.Label(frame, text=self.update_title(), font=('Segoe UI', 14, 'bold'))
        self.update_heading.pack(anchor='w')
        ttk.Label(frame, text=f'Установлена версия {self.release_config["version"]}').pack(anchor='w', pady=(6, 12))
        self.update_status = ttk.Label(frame, text=self.update_status_text, wraplength=540)
        self.update_status.pack(anchor='w')
        self.update_progress = ttk.Progressbar(frame, maximum=100)
        self.update_progress.pack(fill='x', pady=10)
        self.update_notes = tk.Text(frame, height=8, wrap='word', state='disabled')
        self.update_notes.pack(fill='both', expand=True)
        self.update_notes.configure(state='normal')
        self.update_notes.insert('1.0', self.update_release.notes if self.update_release else
                                 'Здесь появится описание новой версии.')
        self.update_notes.configure(state='disabled')
        ttk.Checkbutton(frame, text='Проверять автоматически при запуске и каждые 6 часов',
                        variable=self.automatic_updates, command=self.save_update_preference).pack(anchor='w', pady=12)
        actions = ttk.Frame(frame)
        actions.pack(fill='x')
        self.update_check_button = ttk.Button(actions, text='Проверить обновления', command=self.check_app_updates)
        self.update_check_button.pack(side='left')
        self.update_install_button = ttk.Button(actions, text='Установить обновление',
                                                command=self.download_app_update, style='Primary.TButton')
        self.update_install_button.pack(side='left', padx=8)
        ttk.Button(actions, text='Все версии', command=lambda: webbrowser.open(
            f'https://github.com/{self.release_config["repository"]}/releases')).pack(side='right')
        self.matrix_theme.style_widgets(window)
        self.after(100, lambda: self.matrix_theme.dark_titlebar(window) if window.winfo_exists() else None)
        self.refresh_update_controls()

    def update_title(self):
        return (f'Доступно новое обновление {self.update_release.version}' if self.update_release
                else 'Обновления программы')

    def refresh_update_controls(self):
        window = self.update_window
        if window is None or not window.winfo_exists():
            return
        self.update_heading.configure(text=self.update_title())
        self.update_status.configure(text=self.update_status_text)
        self.update_check_button.configure(state='disabled' if self.update_busy else 'normal')
        self.update_install_button.configure(state='normal' if self.update_release and not self.update_busy else 'disabled')
        self.update_notes.configure(state='normal')
        self.update_notes.delete('1.0', 'end')
        self.update_notes.insert('1.0', self.update_release.notes if self.update_release else
                                 'Здесь появится описание новой версии.')
        self.update_notes.configure(state='disabled')

    def check_app_updates(self, manual=True):
        if manual:
            self.show_updates()
        if self.update_busy:
            return
        self.update_busy = True
        self.update_status_text = 'Проверяем наличие новой версии…'
        self.refresh_update_controls()
        def worker():
            try:
                release = check_release(self.release_config['repository'], self.release_config['version'])
                self.update_messages.put(('checked', release, manual))
            except Exception as exc:
                self.update_messages.put(('error', str(exc), manual))
        threading.Thread(target=worker, name='app-update-check', daemon=True).start()

    def download_app_update(self):
        if self.update_busy or self.update_release is None:
            return
        self.update_busy = True
        self.update_status_text = 'Загружаем установщик… Можно продолжать работу.'
        self.refresh_update_controls()
        release = self.update_release
        def worker():
            try:
                path = download_release(release, self.update_base / 'updates',
                    lambda received, total: setattr(self, 'update_download_progress', (received, total)))
                self.update_messages.put(('downloaded', path, True))
            except Exception as exc:
                self.update_messages.put(('error', str(exc), True))
        threading.Thread(target=worker, name='app-update-download', daemon=True).start()

    def install_downloaded_update(self, path):
        if not messagebox.askyesno('Установить обновление',
                'Обновление загружено и проверено. Завершить текущую работу, сохранить данные '
                'и установить новую версию? После установки программа откроется снова.', parent=self):
            self.update_status_text = 'Обновление загружено. Нажмите «Установить обновление», когда будете готовы.'
            self.refresh_update_controls()
            return
        self._update_install_ready = path
        if not self.close():
            self._update_install_ready = None

    def poll_app_updates(self):
        if getattr(self, '_runtime_closed', False):
            return
        try:
            progress = self.update_download_progress
            if progress is not None and self.update_window is not None and self.update_window.winfo_exists():
                received, total = progress
                self.update_progress['value'] = received * 100 / total
                self.update_status.configure(text=f'Загрузка: {received / 1048576:.1f} / {total / 1048576:.1f} МБ')
            while True:
                try:
                    kind, value, manual = self.update_messages.get_nowait()
                except queue.Empty:
                    break
                self.update_busy = False
                self.update_download_progress = None
                if kind == 'checked':
                    self.update_release = value
                    self.update_status_text = ('Новая версия готова к установке.' if value else
                                               'У вас установлена последняя версия.')
                    if value and (manual or value.version not in self.update_notified):
                        self.update_notified.add(value.version)
                        self.show_updates()
                elif kind == 'downloaded':
                    self.update_downloaded = value
                    self.update_status_text = 'Загрузка завершена. Контрольная сумма проверена.'
                else:
                    self.update_status_text = value
                    self.persist_log_event('app_update_error', value)
                self.refresh_update_controls()
                if kind == 'downloaded':
                    self.install_downloaded_update(value)
        finally:
            if not getattr(self, '_runtime_closed', False):
                self.after(150, self.poll_app_updates)
