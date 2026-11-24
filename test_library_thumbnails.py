"""Real source coordinates and bounded background thumbnail decoding."""
import time
import threading
import unittest
from io import BytesIO
from unittest.mock import patch

import numpy as np
from PIL import Image
from PyQt6.QtCore import QSize, QThreadPool
from PyQt6.QtWidgets import QApplication, QListWidget

from library_thumbnails import ThumbnailLoader, decode_thumbnail


def jpeg(color):
    output = BytesIO()
    Image.new('RGB', (144, 81), color).save(output, format='JPEG')
    return output.getvalue()


class DecoderTests(unittest.TestCase):
    def test_source_local_time_floors_actual_frame_rate_and_letterboxes(self):
        pixels = np.zeros((40, 40, 3), dtype=np.uint8)
        pixels[..., 2] = 255  # BGR source becomes RGB red.
        with patch('core.probe', return_value={'fps': '25/1', 'frames': 100}), \
                patch('core.read_frame', return_value=pixels) as reader:
            result = decode_thumbnail('/source.mov', 1.039)
            reader.assert_called_once_with('/source.mov', 25)
        with Image.open(BytesIO(result)) as picture:
            self.assertEqual(picture.size, (144, 81))
            self.assertGreater(picture.getpixel((72, 40))[0], 240)
            self.assertLess(picture.getpixel((0, 40))[0], 40)

    def test_source_frame_is_clamped(self):
        with patch('core.probe', return_value={'fps': '25', 'frames': 3}), \
                patch('core.read_frame', return_value=np.zeros((4, 4, 3), dtype=np.uint8)) as reader:
            decode_thumbnail('/source.mov', 100)
            self.assertEqual(reader.call_args.args[1], 2)
            decode_thumbnail('/source.mov', -1)
            self.assertEqual(reader.call_args.args[1], 0)


class ThumbnailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widget = QListWidget()
        self.widget.resize(700, 350)
        self.widget.show()
        self.loader = None

    def tearDown(self):
        if self.loader:
            self.loader.close()
        QThreadPool.globalInstance().waitForDone(2000)
        self.widget.close()
        self.app.processEvents()

    def pump(self, condition, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return
            time.sleep(.003)
        self.fail('background thumbnail condition did not complete')

    def rows(self, paths, *, row_height=None):
        self.widget.clear()
        self.widget.addItems(paths)
        if row_height is not None:
            for index in range(self.widget.count()):
                self.widget.item(index).setSizeHint(QSize(600, row_height))
        rows = [{'media_path': '/tmp/' + path, 'start_seconds': 2.5} for path in paths]
        self.loader.set_rows(rows)
        return rows

    def test_async_preview_uses_local_path_and_cache(self):
        calls = []
        self.loader = ThumbnailLoader(self.widget, decoder=lambda path, start: (
            calls.append((path, start)) or jpeg('red')))
        self.rows(['one.mov'])
        self.pump(lambda: bool(self.loader.cache))
        self.assertEqual(calls, [('/tmp/one.mov', 2.5)])
        self.assertTrue(self.widget.item(0).icon().pixmap(144, 81).toImage().pixelColor(70, 40).red() > 240)
        self.rows(['one.mov'])
        self.app.processEvents()
        self.assertEqual(len(calls), 1)

    def test_previous_node_late_result_cannot_overwrite_new_node(self):
        started, release = threading.Event(), threading.Event()
        def decoder(path, start):
            if path.endswith('old.mov'):
                started.set()
                release.wait(2)
                return jpeg('red')
            return jpeg('blue')
        self.loader = ThumbnailLoader(self.widget, decoder=decoder)
        try:
            self.rows(['old.mov'])
            self.pump(started.is_set)
            self.rows(['new.mov'])
            self.pump(lambda: any(key[0].endswith('new.mov') for key in self.loader.cache))
            release.set()
            self.pump(lambda: not self.loader.pending)
            color = self.widget.item(0).icon().pixmap(144, 81).toImage().pixelColor(70, 40)
            self.assertGreater(color.blue(), 240)
            self.assertLess(color.red(), 20)
        finally:
            release.set()

    def test_jobs_are_bounded_and_decode_only_actual_viewport_plus_prefetch(self):
        calls, lock = [], threading.Lock()
        def decoder(path, start):
            with lock:
                calls.append(path)
            time.sleep(.005)
            return jpeg('green')
        self.loader = ThumbnailLoader(self.widget, decoder=decoder)
        self.rows([f'{index}.mov' for index in range(100)], row_height=18)
        self.app.processEvents()
        expected = set(self.loader._viewport_indices())
        self.assertGreater(len(expected), 12)  # A compact list shows over twelve rows.
        self.assertLess(len(expected), 50)
        self.pump(lambda: len(self.loader.cache) == len(expected))
        self.assertLessEqual(len(self.loader.pending), 2)
        self.assertEqual(len(calls), len(expected))
        self.assertEqual({path.rsplit('/', 1)[-1] for path in calls}, {f'{i}.mov' for i in expected})

    def test_repeated_status_refresh_does_not_redecode_cached_jpeg_icons(self):
        self.loader = ThumbnailLoader(self.widget, decoder=lambda path, start: jpeg('blue'))
        rows = self.rows([f'{index}.mov' for index in range(1000)], row_height=18)
        self.app.processEvents()
        expected = set(self.loader._viewport_indices())
        self.pump(lambda: len(self.loader.cache) == len(expected) and not self.loader.pending)
        with patch.object(self.loader, '_icon', wraps=self.loader._icon) as convert:
            for _ in range(5):
                self.loader.set_rows(rows)
                self.loader._schedule()
            self.assertEqual(convert.call_count, 0)
        self.assertEqual(self.loader._decorated_indices, expected)

    def test_completed_decode_only_checks_current_viewport(self):
        self.loader = ThumbnailLoader(self.widget, decoder=lambda path, start: jpeg('blue'))
        self.rows([f'{index}.mov' for index in range(1000)])
        self.app.processEvents()
        self.loader.timer.stop()
        self.loader._active_indices = {5, 6}

        class NoWholeListScan(list):
            def __iter__(self):
                raise AssertionError('a completed thumbnail must not scan every source key')

        self.loader.keys = NoWholeListScan(self.loader.keys)
        self.loader._ready((self.loader.generation, 5, self.loader.keys[5], jpeg('blue')))
        self.assertEqual(self.loader._decorated_indices, {5})
        self.loader.timer.stop()

    def test_scrolling_releases_offscreen_icons_and_caps_both_caches(self):
        self.loader = ThumbnailLoader(self.widget, decoder=lambda path, start: jpeg('blue'))
        self.rows([f'{index}.mov' for index in range(400)], row_height=18)
        self.app.processEvents()
        for first in range(0, 400, 25):
            self.widget.scrollToItem(self.widget.item(first), self.widget.ScrollHint.PositionAtTop)
            self.app.processEvents()
            expected = set(self.loader._viewport_indices())
            self.loader._schedule()
            self.pump(lambda: all(self.loader.keys[index] in self.loader.cache for index in expected)
                      and not self.loader.pending)
            self.assertLessEqual(len(self.loader._decorated_indices), len(expected))
            self.assertLessEqual(len(self.loader.cache), self.loader.CACHE_LIMIT)
            self.assertLessEqual(len(self.loader._icons), self.loader.ICON_LIMIT)
        self.assertTrue(self.loader._decorated_indices)
        self.assertNotIn(0, self.loader._decorated_indices)
        color = self.widget.item(0).icon().pixmap(144, 81).toImage().pixelColor(0, 0)
        self.assertEqual(color.name(), '#e4eaf0')

    def test_duplicate_sources_remap_path_once_per_refresh(self):
        self.loader = ThumbnailLoader(self.widget, decoder=lambda path, start: jpeg('blue'))
        self.widget.addItems(['same.mov'] * 1000)
        with patch('library_bridge.remap_path', side_effect=lambda path, mappings: path) as remap:
            self.loader.set_rows([{'media_path': '/tmp/same.mov', 'start_seconds': index}
                                  for index in range(1000)])
        remap.assert_called_once()

    def test_same_unchanged_source_refresh_reuses_inflight_decode(self):
        started, release = threading.Event(), threading.Event()
        calls = []
        def decoder(path, start):
            calls.append(path)
            if len(calls) == 1:
                started.set()
                release.wait(2)
                return jpeg('red')
            return jpeg('blue')
        self.loader = ThumbnailLoader(self.widget, decoder=decoder)
        try:
            self.rows(['same.mov'])
            self.pump(started.is_set)
            for _ in range(5):
                self.rows(['same.mov'])
                self.loader._schedule()
            self.assertEqual(calls, ['/tmp/same.mov'])
            release.set()
            self.pump(lambda: not self.loader.pending and bool(self.loader.cache))
            self.loader._schedule()
            color = self.widget.item(0).icon().pixmap(144, 81).toImage().pixelColor(70, 40)
            self.assertGreater(color.red(), 240)
            self.assertEqual(calls, ['/tmp/same.mov'])
        finally:
            release.set()

    def test_changed_source_stat_rejects_old_picture_for_same_path(self):
        started, release = threading.Event(), threading.Event()
        calls = []
        def decoder(path, start):
            calls.append(path)
            if len(calls) == 1:
                started.set(); release.wait(2)
                return jpeg('red')
            return jpeg('blue')
        self.loader = ThumbnailLoader(self.widget, decoder=decoder)
        def refresh(mtime):
            self.widget.clear();self.widget.addItem('same.mov')
            self.loader.set_rows([dict(media_path='/tmp/same.mov', start_seconds=2.5,
                metadata=dict(source_stat=dict(size=100,mtime_ns=mtime)))])
        try:
            refresh(1);self.pump(started.is_set)
            refresh(2)
            self.pump(lambda:any(key[3]==2 for key in self.loader.cache))
            release.set();self.pump(lambda:not self.loader.pending)
            color=self.widget.item(0).icon().pixmap(144,81).toImage().pixelColor(70,40)
            self.assertGreater(color.blue(),240)
            self.assertLess(color.red(),20)
        finally:release.set()

    def test_old_failure_does_not_suppress_refreshed_same_source_retry(self):
        started, release = threading.Event(), threading.Event()
        calls=[]
        def decoder(path,start):
            calls.append(path)
            if len(calls)==1:
                started.set();release.wait(2)
                raise OSError('NAS remounting')
            return jpeg('blue')
        self.loader=ThumbnailLoader(self.widget,decoder=decoder)
        try:
            self.rows(['same.mov']);self.pump(started.is_set)
            self.rows(['same.mov']);release.set()
            self.pump(lambda:len(calls)==2 and bool(self.loader.cache) and not self.loader.pending)
            self.assertTrue(list(self.loader.cache.values())[0])
        finally:release.set()

    def test_workstation_mapping_is_read_once_per_list_refresh(self):
        self.loader=ThumbnailLoader(self.widget,decoder=lambda path,start:jpeg('red'))
        with patch('library_bridge.read_settings',return_value={'path_mappings':[
                {'shared':'/tmp','local':'/mounted'}]}) as settings:
            self.rows([f'{n}.mov' for n in range(100)])
        settings.assert_called_once()
        self.assertTrue(all(key[0].startswith('/mounted/') for key in self.loader.keys))

    def test_missing_media_retains_placeholder_and_can_retry_after_refresh(self):
        count = []
        def decoder(path, start):
            count.append(path)
            if len(count) == 1:
                raise OSError('NAS not mounted')
            return jpeg('blue')
        self.loader = ThumbnailLoader(self.widget, decoder=decoder)
        self.rows(['one.mov'])
        self.pump(lambda: bool(self.loader.cache))
        self.assertEqual(list(self.loader.cache.values()), [b''])
        self.rows(['one.mov'])
        self.pump(lambda: len(count) == 2 and not self.loader.pending)
        self.assertTrue(list(self.loader.cache.values())[0])

    def test_close_ignores_inflight_result(self):
        started, release = threading.Event(), threading.Event()
        def decoder(path, start):
            started.set()
            release.wait(2)
            return jpeg('red')
        self.loader = ThumbnailLoader(self.widget, decoder=decoder)
        try:
            self.rows(['one.mov'])
            self.pump(started.is_set)
            self.loader.close()
            release.set()
            QThreadPool.globalInstance().waitForDone(2000)
            self.app.processEvents()
            self.assertFalse(self.loader.cache)
            self.assertEqual(self.loader.rows, [])
        finally:
            release.set()


if __name__ == '__main__':
    unittest.main()
