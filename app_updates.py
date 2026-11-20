"""Public GitHub Releases updates, streamed downloads and checksum verification.

No GitHub credentials or Telegram account information are sent to the update feed.
"""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request
import uuid

from app_paths import resource_directory

MAX_INSTALLER = 800 * 1024 * 1024
TRUSTED_HOSTS = {'api.github.com', 'github.com', 'release-assets.githubusercontent.com',
                 'objects.githubusercontent.com'}
APP_MUTEX = r'Local\TelegramManager-29012AE9-65A2-46DC-A2E5-8746B874AF92'


class UpdateError(Exception):
    pass


def version_tuple(value):
    match = re.fullmatch(r'v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', value)
    if not match:
        raise UpdateError('Некорректный номер версии. Ожидается формат 1.2.3.')
    return tuple(map(int, match.groups()))


def validate_repository(repository):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*', repository):
        raise UpdateError('Репозиторий обновлений не настроен: нужен адрес owner/repository.')
    return repository


def load_release_config():
    data = json.loads((resource_directory() / 'version.json').read_text('utf-8'))
    version_tuple(data['version'])
    validate_repository(data['repository'])
    return data


def trusted_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in TRUSTED_HOSTS
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise UpdateError('Обновление содержит недопустимый адрес загрузки.')
    return url


class GitHubRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        trusted_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_url(url):
    trusted_url(url)
    request = Request(url, headers={'User-Agent': 'TelegramManager-Updater',
                                   'Accept': 'application/vnd.github+json' if
                                   urlsplit(url).hostname == 'api.github.com' else '*/*'})
    try:
        return build_opener(GitHubRedirects()).open(request, timeout=20)
    except HTTPError as exc:
        messages = {404: 'Релиз не найден. Проверьте, что репозиторий и релиз публичные.',
                    403: 'GitHub временно ограничил запросы. Повторите проверку позже.',
                    429: 'Слишком много запросов к GitHub. Повторите проверку позже.'}
        raise UpdateError(messages.get(exc.code, f'GitHub вернул ошибку {exc.code}.')) from None
    except (URLError, TimeoutError, OSError) as exc:
        raise UpdateError('Не удалось подключиться к GitHub. Проверьте интернет и повторите попытку.') from exc


def small_download(url, limit):
    with open_url(url) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise UpdateError('Слишком большой ответ сервера обновлений.')
    return data


@dataclass(frozen=True)
class Release:
    version: str
    notes: str
    url: str
    installer_url: str
    size: int
    sha256: str


def check_release(repository, current_version):
    repository = validate_repository(repository)
    payload = small_download(f'https://api.github.com/repos/{repository}/releases/latest', 2 * 1024 * 1024)
    try:
        data = json.loads(payload)
        tag = data['tag_name']
        if data.get('draft') or data.get('prerelease'):
            return None
        if version_tuple(tag) <= version_tuple(current_version):
            return None
        prefix = f'https://github.com/{repository}/releases/download/{quote(tag, safe="")}/'
        assets = {asset['name']: asset for asset in data.get('assets', [])}
        asset = assets.get('setup.exe')
        if not asset or asset.get('state') != 'uploaded':
            raise UpdateError('У новой версии пока нет готового установщика setup.exe.')
        if asset['browser_download_url'] != prefix + 'setup.exe':
            raise UpdateError('Адрес установщика не соответствует репозиторию обновлений.')
        size = asset['size']
        if type(size) is not int or not 0 < size <= MAX_INSTALLER:
            raise UpdateError('Недопустимый размер установщика.')
        digest = asset.get('digest') or ''
        if re.fullmatch(r'sha256:[0-9a-fA-F]{64}', digest):
            checksum = digest[7:].lower()
        else:
            checksum_asset = assets.get('setup.exe.sha256', {})
            checksum_url = prefix + 'setup.exe.sha256'
            if checksum_asset.get('browser_download_url') != checksum_url:
                raise UpdateError('У релиза нет контрольной суммы установщика.')
            checksum_text = small_download(checksum_url, 1024).decode('ascii').strip()
            match = re.fullmatch(r'([0-9a-fA-F]{64})(?:\s+\*?setup\.exe)?', checksum_text)
            if not match:
                raise UpdateError('Некорректная контрольная сумма установщика.')
            checksum = match[1].lower()
        return Release(tag.lstrip('v'), str(data.get('body') or '')[:6000],
                       f'https://github.com/{repository}/releases/tag/{quote(tag, safe="")}',
                       asset['browser_download_url'], size, checksum)
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise UpdateError('GitHub вернул некорректные сведения об обновлении.') from exc


def verify_installer(path, release):
    if not path.is_file() or path.stat().st_size != release.size:
        return False
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest() == release.sha256


def download_release(release, directory, progress=lambda received, total: None):
    version_tuple(release.version)
    trusted_url(release.installer_url)
    directory = Path(directory) / release.version
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / 'setup.exe'
    if verify_installer(target, release):
        return target
    temporary = directory / f'{uuid.uuid4().hex}.part'
    try:
        digest = hashlib.sha256()
        received = 0
        with open_url(release.installer_url) as response, temporary.open('xb') as output:
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                received += len(block)
                if received > release.size or received > MAX_INSTALLER:
                    raise UpdateError('Размер загрузки не соответствует сведениям о релизе.')
                digest.update(block)
                output.write(block)
                progress(received, release.size)
        if received != release.size or digest.hexdigest() != release.sha256:
            raise UpdateError('Установщик повреждён: контрольная сумма не совпала. Повторите загрузку.')
        temporary.replace(target)
        return target
    finally:
        temporary.unlink(missing_ok=True)


def start_update_helper(installer, data_directory):
    if not getattr(sys, 'frozen', False):
        raise UpdateError('Автоустановка работает в установленной программе. Исходники обновляются через GitHub.')
    helper = resource_directory() / 'apply_update.ps1'
    if not helper.is_file():
        raise UpdateError('Не найден помощник установки. Запустите скачанный setup.exe вручную.')
    # Copy the helper out of the installation being replaced.
    copied = Path(data_directory) / 'updates' / 'apply_update.ps1'
    copied.parent.mkdir(parents=True, exist_ok=True)
    copied.write_bytes(helper.read_bytes())
    system_root = Path(os.environ.get('SystemRoot', r'C:\Windows'))
    powershell = system_root / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
    subprocess.Popen([str(powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                      '-WindowStyle', 'Hidden', '-File', str(copied), '-WaitPid', str(os.getpid()),
                      '-Installer', str(Path(installer).resolve()),
                      '-InstallDir', str(Path(sys.executable).resolve().parent)],
                     creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
                     close_fds=True)
