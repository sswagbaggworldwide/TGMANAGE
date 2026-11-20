"""Frozen application entry point and offline distribution self-test."""
import json
import os
from pathlib import Path
import sys

os.environ.setdefault('OPENTELE_NO_FETCH', '1')


def self_test(report_path):
    from importlib.metadata import version
    import cryptg
    import cryptography
    import opentele2
    from opentele2.td import TDesktop
    import openpyxl
    from PIL import Image, ImageTk
    import tkinter as tk
    import app
    from app_updates import load_release_config
    root = tk.Tk()
    root.withdraw()
    root.update()
    root.destroy()
    with Image.open(app.RESOURCE_BASE / 'assets' / 'sphere-3d-360.gif') as image:
        frames = image.n_frames
    data = dict(ok=True, frozen=bool(getattr(sys, 'frozen', False)),
                **load_release_config(), data_directory=str(app.BASE), gif_frames=frames,
                telethon=version('telethon'), opentele2=version('opentele2'),
                cryptg=version('cryptg'), pillow=version('Pillow'))
    Path(report_path).write_text(json.dumps(data, ensure_ascii=False, indent=2), 'utf-8')


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--self-test':
        self_test(sys.argv[2])
        return
    mutex = None
    if getattr(sys, 'frozen', False) and sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes
        from app_updates import APP_MUTEX
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        mutex = kernel.CreateMutexW(None, False, APP_MUTEX)
        if not mutex:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:
            ctypes.windll.user32.MessageBoxW(None, 'Telegram Manager уже запущен.', 'Telegram Manager', 0x40)
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle(mutex)
            return
    try:
        from app import App
        App().mainloop()
    finally:
        if mutex:
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle(mutex)


if __name__ == '__main__':
    main()
