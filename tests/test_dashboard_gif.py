import tempfile
import tkinter as tk
from pathlib import Path
import unittest
from unittest.mock import patch

from PIL import Image

from ui_theme import GifSphereCanvas, MatrixTheme, load_gif_frames


class GifDecodeTests(unittest.TestCase):
    def test_preserves_frame_timing_and_fits_without_distorting(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'sphere.gif'
            source = [Image.new('RGB', (680, 476), color)
                      for color in ('red', 'green', 'blue')]
            source[0].save(path, save_all=True, append_images=source[1:],
                           duration=[40, 80, 120], loop=0, disposal=2)
            frames, delays = load_gif_frames(path)
            self.assertEqual(delays, (40, 80, 120))
            self.assertEqual(frames[0].size, (320, 224))
            self.assertEqual(frames[0].getpixel((0, 0)), (255, 0, 0))
            self.assertEqual(frames[-1].getpixel((0, 0)), (0, 0, 255))


class GifPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        theme = MatrixTheme(self.root)
        self.sphere = GifSphereCanvas(self.root, theme, 'unused.gif')
        self.sphere.gif_frames = tuple(Image.new('RGB', (160, 112), color)
                                       for color in ('red', 'green', 'blue'))
        self.sphere.gif_durations = (40, 80, 120)
        self.sphere._gif_timeline = [40, 120, 240]

    def tearDown(self):
        self.root.destroy()

    def test_playback_uses_one_timer_and_stops_when_hidden(self):
        with patch.object(self.sphere, 'winfo_viewable', return_value=True):
            self.sphere._sync_playback()
            timer = self.sphere._gif_animation_job
            self.sphere._sync_playback()
            self.assertEqual(timer, self.sphere._gif_animation_job)
        with patch.object(self.sphere, 'winfo_viewable', return_value=False):
            self.sphere._visibility_changed()
        self.assertIsNone(self.sphere._gif_animation_job)
        self.assertNotIn(timer, self.root.tk.splitlist(self.root.tk.call('after', 'info')))

    def test_pause_keeps_current_frame_and_resume_restarts_one_timer(self):
        self.sphere.gif_index = 1
        with patch.object(self.sphere, 'winfo_viewable', return_value=True):
            self.sphere._sync_playback()
            self.sphere.set_animation(False)
            self.assertIsNone(self.sphere._gif_animation_job)
            self.assertEqual(self.sphere.gif_index, 1)
            self.sphere.set_animation(True)
            self.assertIsNotNone(self.sphere._gif_animation_job)

    def test_late_timer_skips_frames_instead_of_replaying_a_burst(self):
        self.sphere._gif_started_at = 100
        with patch.object(self.sphere, 'winfo_viewable', return_value=True), \
                patch('ui_theme.time.monotonic', return_value=100.160), \
                patch.object(self.sphere, '_show_gif_frame') as show:
            self.sphere._tick_gif()
        self.assertEqual(self.sphere.gif_index, 2)
        show.assert_called_once()

    def test_frame_changes_reuse_one_canvas_item_and_bound_the_cache(self):
        self.sphere.gif_frames = tuple(Image.new('RGB', (160, 112), (i * 20, 60, 100))
                                       for i in range(10))
        with patch.object(self.sphere, 'winfo_width', return_value=160), \
                patch.object(self.sphere, 'winfo_height', return_value=112):
            for index in range(15):
                self.sphere.gif_index = index % len(self.sphere.gif_frames)
                self.sphere._show_gif_frame()
        self.assertEqual(len(self.sphere.find_all()), 1)
        self.assertLessEqual(len(self.sphere._gif_photo_cache), 6)

    def test_destroy_cancels_the_timer_and_releases_frames(self):
        with patch.object(self.sphere, 'winfo_viewable', return_value=True):
            self.sphere._sync_playback()
        timer = self.sphere._gif_animation_job
        self.sphere.destroy()
        self.assertTrue(self.sphere._gif_stopped.is_set())
        self.assertFalse(self.sphere.gif_frames)
        self.assertNotIn(timer, self.root.tk.splitlist(self.root.tk.call('after', 'info')))


if __name__ == '__main__':
    unittest.main()
