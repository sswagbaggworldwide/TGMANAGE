"""Local tdata import; no Telegram connections or messages."""
import asyncio
from datetime import datetime
import os
from pathlib import Path
import shutil
import tempfile
import uuid


def error_details(exc, secret=''):
    details, seen = [], set()
    while exc is not None and id(exc) not in seen and len(details) < 5:
        seen.add(id(exc))
        message = getattr(exc, 'message', None)
        if message is None:
            message = str(exc)
        message = str(message).replace('\n', ' ')[:1000]
        if secret:
            message = message.replace(secret, '[пароль скрыт]')
        details.append(f'{type(exc).__name__}: {message}')
        exc = exc.__cause__ or exc.__context__
    return ' → '.join(details)


def find_tdata(source):
    """Accept the tdata folder itself or the Telegram folder containing it."""
    source = Path(source).expanduser()
    candidates = (source, source / 'tdata')
    for candidate in candidates:
        if (candidate / 'key_datas').is_file():
            return candidate.resolve()
    raise ValueError('Выберите папку tdata с файлом key_datas или папку Telegram, в которой находится tdata')


async def convert(source, destination, passcode, emit):
    # opentele2 must be imported before Telethon. The conversion is completely
    # local, so do not let the library fetch a current client version on import.
    os.environ.setdefault('OPENTELE_NO_FETCH', '1')
    import opentele2  # noqa: F401
    from importlib.metadata import version
    from opentele2.api import API, UseCurrentSession
    from opentele2.exception import OpenTeleException
    from opentele2.td import TDesktop

    source = find_tdata(source)
    destination = Path(destination).expanduser().resolve()
    emit(f'Python {os.sys.version.split()[0]}; opentele2 {version("opentele2")}')
    emit(f'Папка tdata найдена: key_datas {(source / "key_datas").stat().st_size} байт; '
         f'элементов: {len(list(source.iterdir()))}')

    api = API.TelegramDesktop()
    try:
        desktop = TDesktop(str(source), api=api, passcode=passcode or None)
    except (Exception, OpenTeleException) as exc:
        name = type(exc).__name__
        if name in {'TDataBadDecryptKey', 'TDataWrongPasscode', 'TDataReadMapDataIncorrectPasscode'}:
            raise ValueError('Не удалось открыть tdata: неверный локальный пароль Telegram Desktop') from None
        raise ValueError('tdata не прочитана: ' + error_details(exc, passcode or '')) from None

    if not desktop.isLoaded() or not desktop.accounts:
        raise ValueError('В tdata нет доступных аккаунтов')

    destination.mkdir(parents=True, exist_ok=True)
    emit(f'Найдено аккаунтов: {len(desktop.accounts)}')
    output_name = 'import_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8]
    staging = Path(tempfile.mkdtemp(prefix='.tdata_work_', dir=destination.parent))
    completed = 0
    try:
        for number, account in enumerate(desktop.accounts, 1):
            client = None
            path = staging / f'account_{number}.session'
            try:
                client = await account.ToTelethon(
                    session=None,
                    flag=UseCurrentSession,
                    api=api,
                )
                # This writes a Telethon SQLite session and the matching JSON
                # profile without connecting to Telegram.
                await client.SaveSessionJson(
                    str(path),
                    api=api,
                    fetch_user_info=False,
                )
                if not path.is_file() or not path.with_suffix('.json').is_file():
                    raise ValueError('Библиотека не создала полную пару файлов')
                completed += 1
                emit(f'Аккаунт {number}: пара .session + .json создана')
            except (Exception, OpenTeleException) as exc:
                for suffix in ('', '-journal', '-wal', '-shm'):
                    Path(str(path) + suffix).unlink(missing_ok=True)
                path.with_suffix('.json').unlink(missing_ok=True)
                emit(f'Аккаунт {number}: ошибка {error_details(exc, passcode or "")}')
            finally:
                if client is not None:
                    try:
                        await client.disconnect()
                    except (Exception, OpenTeleException):
                        pass

        if not completed:
            raise ValueError('Ни один аккаунт не удалось конвертировать')

        final = destination / output_name
        if final.exists():
            raise ValueError('Папка результата уже существует; повторите импорт')
        staging.rename(final)
        emit(f'Готово: {completed} из {len(desktop.accounts)}. Папка: {final}')
        return str(final), completed
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def run_converter(source, destination, passcode, emit):
    return asyncio.run(convert(source, destination, passcode, emit))
