"""Separate installed resources from persistent, per-user application data."""
import os
from pathlib import Path
import sys


def resource_directory():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))


def data_directory():
    override = os.environ.get('TELEGRAM_MANAGER_DATA_DIR')
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, 'frozen', False):
        local = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
        return local / 'TelegramManager'
    return Path(__file__).resolve().parent
