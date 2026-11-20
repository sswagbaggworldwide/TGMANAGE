import tempfile
import unittest
from pathlib import Path

from converter import error_details, find_tdata


class ConverterTests(unittest.TestCase):
    def test_accepts_tdata_or_its_parent_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tdata = root / 'tdata'
            tdata.mkdir()
            (tdata / 'key_datas').write_bytes(b'test')

            self.assertEqual(find_tdata(tdata), tdata.resolve())
            self.assertEqual(find_tdata(root), tdata.resolve())

    def test_rejects_folder_without_key_data(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, 'key_datas'):
                find_tdata(temp)

    def test_error_details_masks_passcode(self):
        error = ValueError('password secret-value is wrong')
        result = error_details(error, 'secret-value')
        self.assertNotIn('secret-value', result)
        self.assertIn('[пароль скрыт]', result)


if __name__ == '__main__':
    unittest.main()
