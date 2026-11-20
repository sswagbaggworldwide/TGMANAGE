"""Holographic theme with resize-only decoration and a visibility-aware GIF."""
from bisect import bisect_right
from collections import OrderedDict
import math
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
import weakref


COLORS = {
    'bg': '#060b09', 'panel': '#0c1710', 'field': '#09110c',
    'text': '#dcebe0', 'muted': '#91aa99', 'accent': '#75f68b',
    'line': '#294333', 'active': '#193624', 'dim': '#365943',
    'warning': '#e4be83', 'error': '#f39d96',
}


def mix(first, second, amount):
    a = [int(first[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(second[i:i + 2], 16) for i in (1, 3, 5)]
    return '#' + ''.join(f'{round(x + (y - x) * amount):02x}' for x, y in zip(a, b))


class MatrixTheme:
    def __init__(self, root, decorations=True):
        self.root = root
        self.decorations = decorations
        self.surfaces = weakref.WeakSet()
        self.install()

    def install(self):
        root, c = self.root, COLORS
        root.configure(background=c['bg'])
        root.option_add('*Font', ('Segoe UI', 9))
        for key, value in {
            '*Text.background': c['field'], '*Text.foreground': c['text'],
            '*Text.insertBackground': c['accent'], '*Text.selectBackground': c['active'],
            '*Text.selectForeground': c['accent'], '*Text.highlightBackground': c['line'],
            '*Text.highlightColor': c['accent'], '*Text.relief': 'flat',
            '*TCombobox*Listbox.background': c['field'],
            '*TCombobox*Listbox.foreground': c['text'],
            '*TCombobox*Listbox.selectBackground': c['active'],
            '*TCombobox*Listbox.selectForeground': c['accent'],
            '*Menu.background': c['panel'], '*Menu.foreground': c['text'],
            '*Menu.activeBackground': c['active'], '*Menu.activeForeground': c['accent'],
        }.items():
            root.option_add(key, value)
        style = ttk.Style(root)
        if 'matrix' not in style.theme_names():
            style.theme_create('matrix', parent='clam')
        style.theme_use('matrix')
        style.configure('.', background=c['bg'], foreground=c['text'],
                        font=('Segoe UI', 9), bordercolor=c['line'],
                        lightcolor=c['line'], darkcolor=c['line'], focuscolor=c['accent'])
        style.configure('TFrame', background=c['bg'])
        style.configure('TLabel', background=c['bg'], foreground=c['text'])
        style.configure('Muted.TLabel', foreground=c['muted'])
        style.configure('TButton', background=c['panel'], foreground=c['text'],
                        padding=(10, 6), relief='flat', borderwidth=1, anchor='center')
        style.map('TButton', background=[('disabled', c['field']), ('pressed', c['active']),
                                        ('active', c['active'])],
                  foreground=[('disabled', '#63796a'), ('active', c['accent'])],
                  bordercolor=[('focus', c['accent']), ('active', c['dim'])])
        style.configure('Primary.TButton', background=c['accent'], foreground=c['bg'],
                        font=('Segoe UI', 9, 'bold'))
        style.map('Primary.TButton', background=[('disabled', c['dim']), ('active', '#a2ffb1')],
                  foreground=[('disabled', c['muted']), ('active', c['bg'])])
        style.configure('TEntry', fieldbackground=c['field'], foreground=c['text'],
                        insertcolor=c['accent'], padding=5)
        style.map('TEntry', fieldbackground=[('disabled', c['panel'])],
                  foreground=[('disabled', '#6c8374')])
        for name in ('TCombobox', 'TSpinbox'):
            style.configure(name, fieldbackground=c['field'], background=c['panel'],
                            foreground=c['text'], arrowcolor=c['accent'], padding=4)
            style.map(name, fieldbackground=[('readonly', c['field']), ('disabled', c['panel'])],
                      foreground=[('disabled', '#6c8374'), ('readonly', c['text'])],
                      selectbackground=[('readonly', c['active'])],
                      selectforeground=[('readonly', c['accent'])])
        for name in ('TCheckbutton', 'TRadiobutton'):
            style.configure(name, background=c['bg'], foreground=c['text'], padding=3)
            style.map(name, background=[('active', c['bg'])],
                      foreground=[('disabled', '#63796a'), ('active', c['accent'])],
                      indicatorbackground=[('selected', c['accent']), ('!selected', c['field'])],
                      indicatorforeground=[('selected', c['bg'])])
        style.configure('Header.TCheckbutton', background=c['panel'])
        style.map('Header.TCheckbutton', background=[('active', c['panel'])])
        style.configure('TLabelframe', background=c['bg'], bordercolor=c['line'], relief='solid')
        style.configure('TLabelframe.Label', foreground=c['accent'], background=c['bg'])
        style.configure('TNotebook', background=c['bg'], borderwidth=0, tabmargins=(12, 5, 12, 0))
        style.configure('TNotebook.Tab', background=c['bg'], foreground=c['muted'],
                        padding=(14, 9), borderwidth=0)
        style.map('TNotebook.Tab', background=[('selected', c['active']), ('active', c['panel'])],
                  foreground=[('selected', c['accent']), ('active', c['text'])],
                  lightcolor=[('selected', c['accent'])])
        style.configure('Treeview', background=c['field'], fieldbackground=c['field'],
                        foreground=c['text'], rowheight=27, borderwidth=0)
        style.map('Treeview', background=[('selected', c['active'])],
                  foreground=[('selected', c['accent'])])
        style.configure('Treeview.Heading', background=c['panel'], foreground=c['muted'],
                        font=('Segoe UI', 9), padding=(8, 6), relief='flat')
        style.map('Treeview.Heading', background=[('active', c['active'])])
        style.configure('Vertical.TScrollbar', background=c['dim'], troughcolor=c['field'],
                        arrowcolor=c['muted'], borderwidth=0)
        style.configure('Horizontal.TScrollbar', background=c['dim'], troughcolor=c['field'],
                        arrowcolor=c['muted'], borderwidth=0)
        style.configure('TSeparator', background=c['line'])
        style.configure('TProgressbar', background=c['accent'], troughcolor=c['field'])
        root.bind_class('Toplevel', '<Map>', self._map_window, add='+')

    def _map_window(self, event):
        window = event.widget
        if not getattr(window, '_matrix_styled', False):
            window._matrix_styled = True
            self.style_widgets(window)
            self.dark_titlebar(window)

    def style_widgets(self, parent):
        c = COLORS
        if not getattr(parent, '_matrix_custom', False):
            if isinstance(parent, (tk.Tk, tk.Toplevel, tk.Frame)):
                parent.configure(background=c['bg'])
            elif isinstance(parent, tk.Text):
                parent.configure(background=c['field'], foreground=c['text'],
                                 insertbackground=c['accent'], selectbackground=c['active'],
                                 selectforeground=c['accent'], borderwidth=0,
                                 highlightthickness=1, highlightbackground=c['line'],
                                 highlightcolor=c['accent'], padx=8, pady=6,
                                 font=('Consolas', 10))
            elif isinstance(parent, tk.Label):
                parent.configure(background=c['bg'], foreground=c['text'])
            elif isinstance(parent, tk.Button):
                parent.configure(background=c['panel'], foreground=c['text'],
                                 activebackground=c['active'], activeforeground=c['accent'],
                                 relief='flat', borderwidth=1, highlightbackground=c['line'])
        for child in parent.winfo_children():
            self.style_widgets(child)

    @staticmethod
    def dark_titlebar(window):
        if sys.platform != 'win32':
            return
        try:
            import ctypes
            ctypes.windll.user32.GetParent.restype = ctypes.c_void_p
            handle = ctypes.windll.user32.GetParent(window.winfo_id())
            enabled = ctypes.c_int(1)
            setter = ctypes.windll.dwmapi.DwmSetWindowAttribute
            setter.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
            setter(handle, 20, ctypes.byref(enabled), ctypes.sizeof(enabled))
            caption = ctypes.c_uint(0x00090b06)
            text = ctypes.c_uint(0x00e0ebdc)
            setter(handle, 35, ctypes.byref(caption), ctypes.sizeof(caption))
            setter(handle, 36, ctypes.byref(text), ctypes.sizeof(text))
        except (AttributeError, OSError):
            pass

    def set_decorations(self, enabled):
        self.decorations = bool(enabled)
        for surface in list(self.surfaces):
            if surface.winfo_exists():
                surface.schedule_draw()


class DecorPanel(tk.Canvas):
    """A normal Tk frame inside a cheap, resize-only decorative border."""
    def __init__(self, parent, theme, code='', minheight=90, **kwargs):
        super().__init__(parent, background=COLORS['bg'], highlightthickness=0,
                         borderwidth=0, height=minheight, **kwargs)
        self._matrix_custom = True
        self.theme, self.code = theme, code
        self.minheight = minheight
        self.pending = None
        self.body = tk.Frame(self, background=COLORS['panel'])
        self.body._matrix_custom = True
        self.body_window = self.create_window(14, 17, window=self.body, anchor='nw')
        self.bind('<Configure>', lambda event: self.schedule_draw())
        self.body.bind('<Configure>', self._body_size)
        self.bind('<Destroy>', self._cancel, add='+')
        theme.surfaces.add(self)

    def _body_size(self, event):
        desired = max(self.minheight, self.body.winfo_reqheight() + 31)
        if int(float(self.cget('height'))) != desired:
            self.configure(height=desired)
        self.schedule_draw()

    def _cancel(self, event):
        if event.widget is self and self.pending is not None:
            self.theme.root.after_cancel(self.pending)
            self.pending = None

    def schedule_draw(self):
        if self.pending is None:
            self.pending = self.theme.root.after(45, self.draw)

    def draw(self):
        self.pending = None
        w, h = self.winfo_width(), self.winfo_height()
        if w < 3 or h < 3:
            return
        desired_width = max(1, w - 28)
        if int(float(self.itemcget(self.body_window, 'width'))) != desired_width:
            self.itemconfigure(self.body_window, width=desired_width)
        self.delete('decor')
        self.create_rectangle(1, 8, w - 2, h - 2, fill=COLORS['panel'],
                              outline=COLORS['line'], tags='decor')
        if self.theme.decorations:
            for x, y, dx, dy in ((1, 8, 1, 1), (w - 2, 8, -1, 1),
                                  (1, h - 2, 1, -1), (w - 2, h - 2, -1, -1)):
                self.create_line(x, y + dy * 14, x, y, x + dx * 18, y,
                                 fill='#62b97b', width=1, tags='decor')
            if self.code:
                self.create_text(w - 16, 8, text=' ' + self.code + ' ',
                                 fill=COLORS['muted'], font=('Consolas', 8),
                                 anchor='e', tags='decor')
            for i in range(3):
                x = w - 28 + i * 5
                self.create_line(x, h - 7, x + 3, h - 11,
                                 fill=COLORS['dim'], tags='decor')
        self.tag_lower('decor')


class ScrollablePage(ttk.Frame):
    """Long account forms remain accessible below a fixed action toolbar."""
    def __init__(self, parent):
        super().__init__(parent)
        self.toolbar = ttk.Frame(self)
        self.toolbar.pack(fill='x')
        area = ttk.Frame(self)
        area.pack(fill='both', expand=True)
        self.canvas = tk.Canvas(area, bg=COLORS['bg'], highlightthickness=0, borderwidth=0)
        self.canvas._matrix_custom = True
        scrollbar = ttk.Scrollbar(area, command=self.canvas.yview)
        scrollbar.pack(side='right', fill='y')
        self.canvas.configure(yscrollcommand=scrollbar.set, yscrollincrement=25)
        self.canvas.pack(side='left', fill='both', expand=True)
        self.body = ttk.Frame(self.canvas)
        self.body_window = self.canvas.create_window(0, 0, window=self.body, anchor='nw')
        self.canvas.bind('<Configure>', self._resize)
        self.body.bind('<Configure>', self._content_size)
        self.winfo_toplevel().bind_all('<MouseWheel>', self._wheel, add='+')

    def _resize(self, event):
        current = int(float(self.canvas.itemcget(self.body_window, 'width')))
        if current != event.width:
            self.canvas.itemconfigure(self.body_window, width=event.width)

    def _content_size(self, event):
        self.canvas.configure(scrollregion=self.canvas.bbox('all'))

    def _wheel(self, event):
        if (self.winfo_ismapped() and str(event.widget).startswith(str(self.body))
                and event.widget.winfo_class() not in
                ('Text', 'Treeview', 'Listbox', 'TCombobox', 'TSpinbox')):
            steps = -int(event.delta / 120)
            if steps:
                self.canvas.yview_scroll(steps, 'units')


class HologramCanvas(tk.Canvas):
    def __init__(self, parent, theme, **kwargs):
        super().__init__(parent, bg=COLORS['panel'], highlightthickness=0,
                         borderwidth=0, width=250, height=173, **kwargs)
        self._matrix_custom = True
        self.theme, self.pending = theme, None
        self.draw_count = 0
        points = []
        for i in range(700):
            y = 1 - i / 699 * 2
            radius = math.sqrt(max(0, 1 - y * y))
            angle = i * 2.39996322973
            factor = 1 + .12 * math.sin(y * 7 + angle * .4)
            x, z = math.cos(angle) * radius * factor, math.sin(angle) * radius * factor
            points.append((x, y * .974 - z * .227, y * .227 + z * .974))
        self.points = sorted(points, key=lambda item: item[2])
        self.bind('<Configure>', lambda event: self.schedule_draw())
        self.bind('<Map>', lambda event: self.schedule_draw())
        self.bind('<Destroy>', self._cancel, add='+')
        theme.surfaces.add(self)

    def _cancel(self, event):
        if event.widget is self and self.pending is not None:
            self.theme.root.after_cancel(self.pending)
            self.pending = None

    def schedule_draw(self):
        if self.pending is None:
            self.pending = self.theme.root.after(70, self.draw)

    def draw(self):
        self.pending = None
        if not self.winfo_ismapped():
            return
        self.draw_count += 1
        self.delete('all')
        w, h = self.winfo_width(), self.winfo_height()
        cx, cy = w / 2, h * .47
        r = max(12, min(w * .34, h * .34))
        if self.theme.decorations:
            for x in range(12, w, 24):
                self.create_line(x, 15, x, h - 28, fill='#152b1c')
            for y in range(15, h - 27, 24):
                self.create_line(12, y, w - 12, y, fill='#152b1c')
            for index in range(5):
                x = 15 + index * max(1, (w - 30) / 4)
                self.create_text(x, cy, text='01\n10\n11\n00\n10\n01',
                                 fill='#183c24', font=('Consolas', 9))
            for factor in (1.15, 1.28):
                self.create_oval(cx - r * factor, cy - r * factor,
                                 cx + r * factor, cy + r * factor,
                                 outline='#315d3e', dash=(1, 5))
            for i in range(24):
                t = i * math.pi / 12
                self.create_line(cx + math.cos(t) * r * 1.22,
                                 cy + math.sin(t) * r * 1.22,
                                 cx + math.cos(t) * r * 1.3,
                                 cy + math.sin(t) * r * 1.3, fill='#518966')
        for x, y, depth in self.points:
            size = max(.7, (depth + 1.5) * .65)
            color = mix(COLORS['panel'], COLORS['accent'], max(.14, min(1, (depth + 1.5) / 2.7)))
            px, py = cx + x * r, cy + y * r
            self.create_oval(px - size, py - size, px + size, py + size,
                             fill=color, outline='')
        if self.theme.decorations:
            orbit = []
            for i in range(81):
                t = i * math.pi / 40
                x, y = math.cos(t) * r * 1.22, math.sin(t) * r * .29
                orbit.extend((cx + x * .96 - y * .28, cy + x * .28 + y * .96))
            self.create_line(*orbit, fill='#609c72', width=1, smooth=True)
            self.create_text(8, 8, text='X +014', anchor='nw',
                             fill='#78a586', font=('Consolas', 8))
            self.create_text(w - 8, h - 30, text='Y -032', anchor='se',
                             fill='#78a586', font=('Consolas', 8))
        self.create_text(cx, h - 12, text='●  SYSTEM READY', fill=COLORS['accent'],
                         font=('Consolas', 8))


def load_gif_frames(path, stopped=None):
    """Decode composited frames off the UI thread, within a 32 MiB RGB budget."""
    from PIL import Image
    frames, durations = [], []
    with Image.open(path) as image:
        width, height = image.size
        count = image.n_frames
        scale = min(1, 320 / width, 224 / height,
                    math.sqrt(32 * 1024 * 1024 / (count * width * height * 3)))
        size = max(1, int(width * scale)), max(1, int(height * scale))
        for index in range(count):
            if stopped is not None and stopped.is_set():
                return (), ()
            image.seek(index)
            duration = max(20, int(image.info.get('duration', 100) or 100))
            rgba = image.convert('RGBA')
            rgba = rgba.resize(size, Image.Resampling.LANCZOS, reducing_gap=3)
            frame = Image.new('RGB', size, COLORS['panel'])
            frame.paste(rgba, mask=rgba.getchannel('A'))
            frames.append(frame)
            durations.append(duration)
    return tuple(frames), tuple(durations)


def _decode_gif(path, results, stopped):
    try:
        frames, durations = load_gif_frames(path, stopped)
        if not stopped.is_set():
            results.put((frames, durations, None))
    except Exception as exc:
        if not stopped.is_set():
            results.put(((), (), type(exc).__name__))


class GifSphereCanvas(HologramCanvas):
    """Keep one canvas image, six Tk frames, and at most one playback timer."""
    def __init__(self, parent, theme, gif_path, animation=True, **kwargs):
        self.gif_path = Path(gif_path)
        self.animation_enabled = bool(animation)
        self.gif_frames, self.gif_durations = (), ()
        self.gif_error = None
        self.gif_index = 0
        self.displayed_frames = 0
        self._gif_loading = False
        self._gif_results = queue.Queue(maxsize=1)
        self._gif_stopped = threading.Event()
        self._gif_poll_job = None
        self._gif_animation_job = None
        self._gif_image_item = None
        self._gif_current_photo = None
        self._gif_photo_cache = OrderedDict()
        self._gif_render_size = None
        self._gif_timeline = []
        self._gif_started_at = 0
        super().__init__(parent, theme, **kwargs)
        self.bind('<Map>', self._visibility_changed, add='+')
        self.bind('<Unmap>', self._visibility_changed, add='+')
        self._root_visibility_bindings = {
            event: theme.root.bind(event, self._root_visibility_changed, add='+')
            for event in ('<Map>', '<Unmap>')
        }

    def _root_visibility_changed(self, event):
        if event.widget is self.theme.root:
            self._visibility_changed(event)

    def _visibility_changed(self, event=None):
        if self._gif_stopped.is_set():
            return
        if self.winfo_viewable():
            self.schedule_draw()
            self._sync_playback()
        else:
            self._pause_playback()
            self._cancel_gif_job('_gif_poll_job')

    def _cancel_gif_job(self, attribute):
        job = getattr(self, attribute)
        if job is not None:
            self.theme.root.after_cancel(job)
            setattr(self, attribute, None)

    def _cancel(self, event):
        if event.widget is self:
            self._gif_stopped.set()
            self._pause_playback()
            self._cancel_gif_job('_gif_poll_job')
            for name, binding in getattr(self, '_root_visibility_bindings', {}).items():
                self.theme.root.unbind(name, binding)
            self._gif_photo_cache.clear()
            self._gif_current_photo = None
            self.gif_frames = ()
        super()._cancel(event)

    def _start_loading(self):
        if self._gif_loading or self.gif_error or self.gif_frames:
            return
        self._gif_loading = True
        threading.Thread(target=_decode_gif,
                         args=(self.gif_path, self._gif_results, self._gif_stopped),
                         name='dashboard-gif-decoder', daemon=True).start()

    def _poll_loading(self):
        self._gif_poll_job = None
        if self._gif_stopped.is_set() or not self.winfo_viewable():
            return
        try:
            frames, durations, error = self._gif_results.get_nowait()
        except queue.Empty:
            self._gif_poll_job = self.theme.root.after(40, self._poll_loading)
            return
        self.gif_error = error
        if not frames:
            return
        self.gif_frames, self.gif_durations = frames, durations
        total = 0
        for duration in durations:
            total += duration
            self._gif_timeline.append(total)
        self.delete('all')
        self._show_gif_frame()
        self._sync_playback()

    def draw(self):
        self.pending = None
        if self._gif_stopped.is_set() or not self.winfo_viewable():
            return
        if self.gif_frames:
            self.draw_count += 1
            self._show_gif_frame()
        else:
            super().draw()
            self._start_loading()
            if self._gif_loading and not self.gif_error and self._gif_poll_job is None:
                self._poll_loading()
        self._sync_playback()

    def _show_gif_frame(self):
        from PIL import Image, ImageTk
        frame = self.gif_frames[self.gif_index]
        width, height = max(1, self.winfo_width()), max(1, self.winfo_height())
        scale = min(width / frame.width, height / frame.height, 1)
        size = max(1, round(frame.width * scale)), max(1, round(frame.height * scale))
        if size != self._gif_render_size:
            self._gif_photo_cache.clear()
            self._gif_render_size = size
        key = self.gif_index, size
        photo = self._gif_photo_cache.get(key)
        if photo is None:
            fitted = frame if frame.size == size else frame.resize(size, Image.Resampling.BILINEAR)
            photo = ImageTk.PhotoImage(fitted, master=self)
            self._gif_photo_cache[key] = photo
            while len(self._gif_photo_cache) > 6:
                self._gif_photo_cache.popitem(last=False)
        else:
            self._gif_photo_cache.move_to_end(key)
        self._gif_current_photo = photo
        if self._gif_image_item is None:
            self._gif_image_item = self.create_image(width / 2, height / 2, image=photo)
        else:
            self.coords(self._gif_image_item, width / 2, height / 2)
            self.itemconfigure(self._gif_image_item, image=photo)
        self.displayed_frames += 1

    def set_animation(self, enabled):
        self.animation_enabled = bool(enabled)
        self._sync_playback()

    def _pause_playback(self):
        self._cancel_gif_job('_gif_animation_job')

    def _sync_playback(self):
        if (self._gif_stopped.is_set() or not self.animation_enabled
                or len(self.gif_frames) < 2 or not self.winfo_viewable()):
            self._pause_playback()
            return
        if self._gif_animation_job is None:
            offset = self._gif_timeline[self.gif_index - 1] if self.gif_index else 0
            self._gif_started_at = time.monotonic() - offset / 1000
            self._gif_animation_job = self.theme.root.after(
                self.gif_durations[self.gif_index], self._tick_gif)

    def _tick_gif(self):
        self._gif_animation_job = None
        if (self._gif_stopped.is_set() or not self.animation_enabled
                or not self.winfo_viewable()):
            return
        elapsed = int((time.monotonic() - self._gif_started_at) * 1000) % self._gif_timeline[-1]
        self.gif_index = bisect_right(self._gif_timeline, elapsed)
        self._show_gif_frame()
        # Follow the original timeline and skip late frames instead of queuing
        # a burst of work when the UI is briefly busy.
        delay = max(1, self._gif_timeline[self.gif_index] - elapsed)
        self._gif_animation_job = self.theme.root.after(delay, self._tick_gif)
