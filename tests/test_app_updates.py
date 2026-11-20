"""Offline regression checks for the release feed and installer integrity."""
from contextlib import contextmanager
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

import app_paths
import app_updates as updates


class UpdateTests(unittest.TestCase):
    repo = 'sswagbaggworldwide/TGMANAGE'
    payload = b'MZ-offline-test-installer'

    def release_data(self, tag='v1.1.0'):
        return {'tag_name': tag, 'body': 'Changes', 'draft': False, 'prerelease': False,
                'assets': [{'name': 'setup.exe', 'state': 'uploaded', 'size': len(self.payload),
                            'browser_download_url': f'https://github.com/{self.repo}/releases/download/{tag}/setup.exe',
                            'digest': 'sha256:' + hashlib.sha256(self.payload).hexdigest()}]}

    def release(self):
        with patch.object(updates, 'small_download', return_value=json.dumps(self.release_data()).encode()):
            return updates.check_release(self.repo, '1.0.0')

    def test_versions_are_numeric_and_strict(self):
        self.assertGreater(updates.version_tuple('v1.10.0'), updates.version_tuple('1.9.9'))
        for bad in ['1.0', '1.0.0-beta', '../../1.0.0', '01.0.0', '1.0.0\n']:
            with self.subTest(bad=bad), self.assertRaises(updates.UpdateError):
                updates.version_tuple(bad)

    def test_old_and_equal_releases_do_not_offer_update(self):
        for tag in ['v0.9.0', 'v1.0.0']:
            with patch.object(updates, 'small_download', return_value=json.dumps(self.release_data(tag)).encode()):
                self.assertIsNone(updates.check_release(self.repo, '1.0.0'))

    def test_prereleases_and_drafts_are_ignored(self):
        for key in ['draft', 'prerelease']:
            data = self.release_data()
            data[key] = True
            with patch.object(updates, 'small_download', return_value=json.dumps(data).encode()):
                self.assertIsNone(updates.check_release(self.repo, '1.0.0'))

    def test_new_release_has_version_and_checksum(self):
        release = self.release()
        self.assertEqual(release.version, '1.1.0')
        self.assertEqual(release.sha256, hashlib.sha256(self.payload).hexdigest())

    def test_rejects_untrusted_redirects(self):
        for url in ['http://github.com/a', 'https://github.com.evil.example/a',
                    'https://user:pass@github.com/a', 'https://github.com:1234/a']:
            with self.subTest(url=url), self.assertRaises(updates.UpdateError):
                updates.trusted_url(url)

    def test_rejects_installer_from_other_repository(self):
        data = self.release_data()
        data['assets'][0]['browser_download_url'] = 'https://github.com/other/repo/releases/download/v1.1.0/setup.exe'
        with patch.object(updates, 'small_download', return_value=json.dumps(data).encode()), self.assertRaises(updates.UpdateError):
            updates.check_release(self.repo, '1.0.0')

    def test_missing_installer_and_missing_hash_are_reported(self):
        for assets in [[], [dict(self.release_data()['assets'][0], digest=None)]]:
            data = dict(self.release_data(), assets=assets)
            with patch.object(updates, 'small_download', return_value=json.dumps(data).encode()), self.assertRaises(updates.UpdateError):
                updates.check_release(self.repo, '1.0.0')

    def test_checksum_asset_fallback(self):
        data = self.release_data()
        del data['assets'][0]['digest']
        data['assets'].append({'name': 'setup.exe.sha256', 'browser_download_url':
            f'https://github.com/{self.repo}/releases/download/v1.1.0/setup.exe.sha256'})
        digest = hashlib.sha256(self.payload).hexdigest()
        with patch.object(updates, 'small_download', side_effect=[json.dumps(data).encode(),
                                                                (digest + '  setup.exe\n').encode()]):
            self.assertEqual(updates.check_release(self.repo, '1.0.0').sha256, digest)

    def test_streamed_download_and_cached_reuse(self):
        release = self.release()
        with tempfile.TemporaryDirectory() as temp:
            progress = []
            with patch.object(updates, 'open_url', return_value=io.BytesIO(self.payload)):
                path = updates.download_release(release, temp, lambda done, total: progress.append((done, total)))
            self.assertEqual(path.read_bytes(), self.payload)
            self.assertEqual(progress[-1], (release.size, release.size))
            with patch.object(updates, 'open_url', side_effect=AssertionError('cached download requested')):
                self.assertEqual(updates.download_release(release, temp), path)
            self.assertFalse(list(Path(temp).rglob('*.part')))

    def test_corrupt_and_truncated_downloads_never_become_executable(self):
        release = self.release()
        for payload in [b'x' * release.size, self.payload[:-1], self.payload + b'x']:
            with tempfile.TemporaryDirectory() as temp:
                with patch.object(updates, 'open_url', return_value=io.BytesIO(payload)), self.assertRaises(updates.UpdateError):
                    updates.download_release(release, temp)
                self.assertFalse(list(Path(temp).rglob('setup.exe')))
                self.assertFalse(list(Path(temp).rglob('*.part')))

    def test_interrupted_download_cleans_up(self):
        class Interrupted(io.BytesIO):
            def read(self, *args):
                raise TimeoutError('interrupted')
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(updates, 'open_url', return_value=Interrupted()), self.assertRaises(TimeoutError):
                updates.download_release(self.release(), temp)
            self.assertFalse(list(Path(temp).rglob('*.part')))

    def test_response_size_is_bounded(self):
        with patch.object(updates, 'open_url', return_value=io.BytesIO(b'x' * 11)), self.assertRaises(updates.UpdateError):
            updates.small_download('https://api.github.com/test', 10)

    def test_installed_data_is_outside_application_directory(self):
        with patch.object(app_paths.sys, 'frozen', True, create=True), patch.dict(os.environ, {'LOCALAPPDATA': r'C:\Users\Example\AppData\Local'}):
            with patch.dict(os.environ):
                os.environ.pop('TELEGRAM_MANAGER_DATA_DIR', None)
                self.assertEqual(app_paths.data_directory(), Path(r'C:\Users\Example\AppData\Local\TelegramManager'))

    def test_source_staging_has_no_personal_files(self):
        script = Path(__file__).resolve().parents[1] / 'packaging' / 'build_release.py'
        spec = importlib.util.spec_from_file_location('build_release_for_tests', script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            stage = module.stage_source(Path(temp) / 'source')
            names = {path.relative_to(stage).as_posix() for path in stage.rglob('*') if path.is_file()}
            self.assertEqual(names, set(module.source_files()))
            self.assertTrue((stage / 'assets' / 'sphere-3d-360.gif').is_file())
            for name in names:
                self.assertFalse(name.startswith(('accounts/', 'logs/', 'backup')))
                self.assertFalse(name.endswith(('.session', '.sqlite3')))
            self.assertFalse((stage / 'config.json').exists())


if __name__ == '__main__':
    unittest.main()
