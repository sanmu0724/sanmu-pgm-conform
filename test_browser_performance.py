"""Responsiveness, stale results and source identity survive browser caching."""
import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch

from PyQt6.QtCore import QThreadPool,QTimer,Qt,QEvent
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QApplication
from library_ui import LibraryPanel,InlinePreview
from library_angles import AngleMapper,enrich_related_angles
from library_timeline import build_timeline_data
from library_thumbnails import SourceFrameLoader
from test_library_angles import catalogue


class BrowserPerformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        with patch('library_bridge.read_settings',return_value={}):
            self.panel=LibraryPanel(lambda:{},lambda _:None,auto_restore=False)

    def tearDown(self):
        self.panel.close();QThreadPool.globalInstance().waitForDone(2000);self.app.processEvents()

    def pump(self,predicate):
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            self.app.processEvents()
            if predicate():return
            time.sleep(.003)
        self.fail('background operation did not complete')

    def test_prepared_timeline_matches_uncached_result_without_remapping(self):
        data=catalogue();expected=build_timeline_data(data)
        enriched=enrich_related_angles(data)
        with patch('library_timeline.AngleMapper',side_effect=AssertionError('repeated angle mapping')):
            self.assertEqual(build_timeline_data(enriched,prepared=True),expected)

    def test_gui_uses_worker_nodes_without_rebuilding_timeline(self):
        data=catalogue();data['timeline_nodes']=build_timeline_data(data)
        with patch('library_timeline.build_timeline_data',side_effect=AssertionError('GUI rebuilt timeline')):
            self.panel._set_data(data)
        self.assertEqual(self.panel.match_list.count(),1)
        self.assertEqual(self.panel._visible_candidates[0]['related_angle_count'],4)

    def test_switching_script_does_not_rebuild_all_pending_rows(self):
        data=catalogue();data['scripts'].append(dict(data['scripts'][0],id='second',order=2))
        self.panel._set_data(data)
        with patch.object(self.panel,'render_pending') as rebuild:
            self.panel.script_timeline.setCurrentItem(self.panel.script_timeline._items['second'])
        rebuild.assert_not_called();self.assertEqual(self.panel.match_list.count(),0)

    def test_pending_mapper_is_reused_and_invalidated_on_catalogue_refresh(self):
        data=catalogue();row=dict(data['transcripts'][0],manual_pending=True)
        self.panel._set_data(data)
        with patch('library_angles.AngleMapper',wraps=AngleMapper) as factory:
            self.panel._set_pending_angles(row);first=self.panel._pending_angle_mapper
            self.panel._set_pending_angles(row)
            self.assertEqual(factory.call_count,1)
        self.panel._set_data(data);self.assertIsNone(self.panel._pending_angle_mapper)
        self.panel._set_pending_angles(row);self.assertIsNot(first,self.panel._pending_angle_mapper)

    def test_slow_nas_check_does_not_block_gui_and_cleared_selection_stays_empty(self):
        started,release=threading.Event(),threading.Event();worker_threads=[];heartbeat=[]
        def slow(path,index):
            worker_threads.append(threading.get_ident());started.set();release.wait(2);return True
        self.panel._inline_io.decoder=slow
        try:
            self.panel._load_inline('/NAS/slow.mov',0,2)
            self.pump(started.is_set)
            QTimer.singleShot(0,lambda:heartbeat.append(True))
            self.pump(lambda:bool(heartbeat))
            self.assertNotEqual(worker_threads,[threading.get_ident()])
            self.panel._clear_inline();release.set()
            QThreadPool.globalInstance().waitForDone(2000);self.app.processEvents()
            self.assertIsNone(self.panel.inline_preview)
        finally:release.set()

    def test_rapid_preview_selection_checks_only_final_path(self):
        paths=[]
        self.panel._inline_io.decoder=lambda path,index:paths.append(path) or False
        for number in range(20):self.panel._load_inline('/NAS/%d.mov'%number,0,2)
        self.pump(lambda:bool(paths))
        self.assertEqual(paths,['/NAS/19.mov'])

    def test_returning_to_current_clip_cancels_pending_different_source(self):
        self.panel.inline_preview=Mock()
        self.panel._inline_key=('/current.mov',0.,2.)
        self.panel._load_inline('/other.mov',0,2)
        self.assertTrue(self.panel._inline_timer.isActive())
        self.panel._load_inline('/current.mov',0,2)
        self.assertFalse(self.panel._inline_timer.isActive())
        self.assertIsNone(self.panel._inline_request)
        self.panel.inline_preview.show.assert_called_once()

    def test_same_file_new_clip_reuses_decoder_and_seeks_to_new_in_point(self):
        preview=InlinePreview()
        original=preview.player;preview.player=Mock()
        try:
            preview.load_media('/same.mov',1,2);preview.positioned=True
            preview.load_media('/same.mov',3,4)
            self.assertEqual(preview.player.setSource.call_count,1)
            preview.player.setPosition.assert_called_with(3000)
            self.assertEqual((preview.start_ms,preview.end_ms),(3000,4000))
            preview.load_media('/different.mov',5,6)
            self.assertEqual(preview.player.setSource.call_count,2)
        finally:preview.player=original;preview.close()

    def test_preview_has_no_height_cap_and_splitters_can_expand_picture(self):
        self.assertGreater(self.panel.inline_host.maximumHeight(),10000)
        self.assertEqual(self.panel.browser_split.orientation(),Qt.Orientation.Horizontal)
        self.assertEqual(self.panel.browser_split.count(),3)
        self.assertEqual(self.panel.workspace_right.orientation(),Qt.Orientation.Vertical)
        self.assertEqual(self.panel.workspace_right.count(),2)
        self.assertTrue(self.panel.workspace_right.widget(0).isAncestorOf(self.panel.inline_host))

    def test_closed_browser_cannot_restart_audio_on_late_media_ready(self):
        from PyQt6.QtMultimedia import QMediaPlayer
        preview=InlinePreview()
        original=preview.player;preview.player=Mock()
        try:
            preview.path='/same.mov';preview._suspended=True
            preview.ready(QMediaPlayer.MediaStatus.LoadedMedia)
            preview.player.play.assert_not_called()
            preview._suspended=False
            preview.replay()
            preview.player.play.assert_called_once_with()
        finally:preview.player=original;preview.close()

    def test_fullscreen_escape_restores_review_picture(self):
        preview=InlinePreview()
        try:
            preview.video.setFullScreen(True)
            self.assertTrue(preview.video.isFullScreen())
            event=QKeyEvent(QEvent.Type.KeyPress,Qt.Key.Key_Escape,Qt.KeyboardModifier.NoModifier)
            preview.video.keyPressEvent(event)
            self.assertFalse(preview.video.isFullScreen())
        finally:preview.close()

    def test_old_frame_completion_cannot_replace_new_scene_frame(self):
        started,release=threading.Event(),threading.Event();received=[]
        def decode(path,index):
            if path=='old':started.set();release.wait(2)
            return path
        loader=SourceFrameLoader(decoder=decode)
        try:
            loader.request('old',0,lambda result,error:received.append(result));self.pump(started.is_set)
            loader.request('new',0,lambda result,error:received.append(result))
            self.assertEqual(received,[])
            release.set();self.pump(lambda:received==['new'])
            QThreadPool.globalInstance().waitForDone(2000);self.app.processEvents()
            self.assertEqual(received,['new'])
        finally:release.set();loader.close()
