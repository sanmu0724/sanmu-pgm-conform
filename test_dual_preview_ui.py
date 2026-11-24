"""Comparison previews use edit seconds while keeping source media unchanged."""
import copy
import unittest
from unittest.mock import patch

from PyQt6.QtCore import QObject, QPoint, QTimer, Qt, pyqtSignal
from PyQt6.QtMultimedia import QMediaPlayer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QCheckBox, QLineEdit, QWidget, QSlider

from manual_timeline_ui import ManualTimelineDialog
from test_manual_timeline_ui import fixture


class Player(QObject):
    positionChanged = pyqtSignal(int)
    mediaStatusChanged = pyqtSignal(object)
    playbackStateChanged = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.milliseconds = 0
        self.state = QMediaPlayer.PlaybackState.StoppedState
        self.status = QMediaPlayer.MediaStatus.LoadedMedia
        self.positions = []
        self.capture = None
        self.capture_changes = []

    def position(self): return self.milliseconds
    def playbackState(self): return self.state
    def mediaStatus(self): return self.status
    def setPosition(self, value):
        self.milliseconds = int(value)
        self.positions.append(int(value))
        self.positionChanged.emit(int(value))
    def setAudioBufferOutput(self, value):
        self.capture = value
        self.capture_changes.append(value)
    def set_state(self, state):
        self.state = state
        self.playbackStateChanged.emit(state)
    def play(self): self.set_state(QMediaPlayer.PlaybackState.PlayingState)
    def pause(self):
        if self.state==QMediaPlayer.PlaybackState.StoppedState and self.status==QMediaPlayer.MediaStatus.EndOfMedia:self.setPosition(0)
        self.set_state(QMediaPlayer.PlaybackState.PausedState)
    def stop(self): self.set_state(QMediaPlayer.PlaybackState.StoppedState)


class Audio:
    def __init__(self, player, parent):
        self.player = player
        self.capture = object()
        self.mute = QCheckBox(parent)
        self.closed = False
        self.muted = False
        self.mute.toggled.connect(lambda value:setattr(self, "muted", value))
        self.device_timer = QTimer(parent)
        self.device_timer.start(1500)
    def seek(self, milliseconds): self.player.setPosition(milliseconds)
    def close(self):
        self.closed = True
        self.device_timer.stop()


class Preview(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.player = Player(self)
        self.preview_audio = Audio(self.player, self)
        self.path = ''
        self.loads = []
        self.pauses = 0
        self.clears = 0
        self.positioned = True
        self.async_load = False
        self._play_requested = False
        self.seek=QSlider(Qt.Orientation.Horizontal,self)
        self.seek.sliderMoved.connect(self.preview_audio.seek)
    def pause(self):
        self.pauses += 1
        self._play_requested = False
        self.player.pause()
    def clear(self):
        self.clears += 1
        self.pause()
        self.player.setPosition(0)
        self.path = ''
        self.positioned = False
    def load_media(self, path, start, end, *, play=False):
        self.loads.append((path, start, end, play))
        self.pause()
        self.path = path
        self.positioned = not self.async_load
        self.player.setPosition(round(start * 1000))
    def ready(self):
        self.positioned = True
        self.player.setPosition(0)  # InlinePreview's initial seek precedes ours.
        self.player.mediaStatusChanged.emit(QMediaPlayer.MediaStatus.LoadedMedia)
    def toggle_playback(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.pause()
        elif self.path:
            self._play_requested = True
            if self.positioned: self.player.play()


class DualPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])
    def setUp(self):
        self.rows, self.layout, self.cut = fixture()
        self.ids = [row['id'] for row in self.rows if row['session_id'] == 'alt']
        self.base_ids = [row['id'] for row in self.rows if row['session_id'] == 'base']
        self.patch = patch('library_ui.InlinePreview', Preview);self.patch.start()
        self.dialog = ManualTimelineDialog(dict(scripts=[], transcripts=[]), self.rows, self.layout, self.cut)
        self.dialog.show();self.dialog.mode.setCurrentIndex(1);self.app.processEvents()
        self.states=[]
        for preview in (self.dialog.preview,self.dialog.baseline_preview):
            preview.player.playbackStateChanged.connect(lambda _state:self.states.append(sum(self.dialog._playing(p) for p in (self.dialog.preview,self.dialog.baseline_preview))))
    def tearDown(self):
        self.dialog._allow_close=True;self.dialog.close();self.app.processEvents();self.patch.stop()
        self.assertLessEqual(max(self.states,default=0),1,'players must never play simultaneously')
    def select(self,identity=None,**kwargs): self.dialog.track_selection([identity or self.ids[0]],**kwargs)
    def position(self,preview,milliseconds): preview.player.setPosition(milliseconds)
    def assert_active(self,side,playing=True):
        self.assertEqual(self.dialog._active_preview,side)
        current=self.dialog.baseline_preview if side=='baseline' else self.dialog.preview
        other=self.dialog.preview if side=='baseline' else self.dialog.baseline_preview
        self.assertEqual(self.dialog._playing(current),playing);self.assertFalse(self.dialog._playing(other))

    def test_track_alternative_autoplays_right_without_loading_or_following_left(self):
        original=copy.deepcopy(self.rows);self.select()
        self.assert_active('alternative');self.assertEqual(self.dialog.preview.loads,[('/qa/alt0.mov',0,10.,False)])
        self.assertFalse(self.dialog.baseline_preview.loads)
        self.position(self.dialog.preview,2500);self.assertFalse(self.dialog.baseline_preview.loads)
        self.assertEqual(self.dialog.canvas.playhead_seconds,7.5);self.assertEqual(self.rows,original)

    def test_select_baseline_autoplays_left_and_keeps_right_position(self):
        self.select();self.position(self.dialog.preview,2300);loads=list(self.dialog.preview.loads)
        self.select(self.base_ids[1]);self.assert_active('baseline')
        self.assertEqual(self.dialog.preview.loads,loads);self.assertEqual(self.dialog.preview.player.position(),2300)
        self.assertEqual(self.dialog._preview_identity,self.ids[0])
        self.assertEqual(self.dialog._baseline_path,'/qa/base1.mov')

    def test_select_alternative_preserves_baseline_frozen_frame(self):
        self.select(self.base_ids[0]);self.position(self.dialog.baseline_preview,3700)
        path=self.dialog._baseline_path;loads=list(self.dialog.baseline_preview.loads)
        self.select();self.assert_active('alternative')
        self.assertEqual(self.dialog.baseline_preview.player.position(),3700)
        self.position(self.dialog.preview,2000)
        self.assertEqual(self.dialog._baseline_path,path);self.assertEqual(self.dialog.baseline_preview.loads,loads)
        self.assertEqual(self.dialog.baseline_preview.player.position(),3700)

    def test_space_controls_last_selected_baseline_even_with_old_alternative_loaded(self):
        self.select();self.position(self.dialog.preview,2600);self.select(self.base_ids[1])
        self.position(self.dialog.baseline_preview,1850)
        for playing in (False,True,False,True):
            self.dialog.canvas.setFocus();QTest.keyClick(self.dialog.canvas,Qt.Key.Key_Space)
            self.assert_active('baseline',playing)
            self.assertEqual(self.dialog.preview.player.position(),2600)
            self.assertEqual(self.dialog.baseline_preview.player.position(),1850)

    def test_inactive_buttons_and_seek_cannot_steal_selection_owner(self):
        self.select(self.base_ids[0]);self.assertFalse(self.dialog.play_button.isEnabled());self.assertFalse(self.dialog.preview.seek.isEnabled())
        self.dialog.toggle_alternative_playback();self.assert_active('baseline')
        self.select();self.assertFalse(self.dialog.baseline_play_button.isEnabled());self.assertFalse(self.dialog.baseline_preview.seek.isEnabled())
        self.dialog.toggle_baseline_playback();self.assert_active('alternative')

    def test_alternative_pause_resume_uses_timeline_head_without_reloading_or_rewinding(self):
        self.select();self.position(self.dialog.preview,2780);loads=list(self.dialog.preview.loads)
        for _ in range(3):
            self.dialog.toggle_playback();self.assert_active('alternative',False)
            self.assertAlmostEqual(self.dialog.canvas.playhead_seconds,7.78)
            self.dialog.toggle_playback();self.assert_active('alternative')
            self.assertEqual(self.dialog.preview.player.position(),2780)
        self.assertEqual(self.dialog.preview.loads,loads)

    def test_alternative_resume_near_end_does_not_jump_to_start(self):
        self.select();self.position(self.dialog.preview,9999);self.dialog.toggle_playback();self.dialog.toggle_playback()
        self.assertEqual(self.dialog.preview.player.position(),9999)
        self.assertEqual(self.dialog.canvas.playhead_seconds,14.999)

    def test_alternative_eof_does_not_auto_restart_through_ready_recovery(self):
        self.select();self.position(self.dialog.preview,10000)
        self.dialog.preview.player.pause();self.dialog.preview.player.mediaStatusChanged.emit(QMediaPlayer.MediaStatus.EndOfMedia)
        self.app.processEvents();self.assert_active('alternative',False)
        self.assertFalse(self.dialog._alternative_wants_play);self.assertFalse(self.dialog._preview_resume.isActive())
        self.assertEqual(self.dialog.preview.player.position(),10000)

    def test_selecting_other_pane_does_not_native_pause_or_rewind_ended_media(self):
        self.select();player=self.dialog.preview.player
        player.setPosition(10000);player.status=QMediaPlayer.MediaStatus.EndOfMedia;player.set_state(QMediaPlayer.PlaybackState.StoppedState)
        pauses=self.dialog.preview.pauses
        self.select(self.base_ids[0])
        self.assertEqual(self.dialog.preview.pauses,pauses);self.assertEqual(player.position(),10000)
        self.assertFalse(self.dialog.preview._play_requested)

    def test_explicit_alternative_resume_at_exact_eof_uses_last_legal_frame(self):
        self.select();self.position(self.dialog.preview,10000)
        self.dialog.preview.player.status=QMediaPlayer.MediaStatus.EndOfMedia
        self.dialog.preview.player.set_state(QMediaPlayer.PlaybackState.StoppedState)
        self.dialog.toggle_playback()
        self.assertEqual(self.dialog.preview.player.position(),9960);self.assert_active('alternative')

    def test_final_baseline_eof_preserves_head_and_explicit_resume_uses_last_frame(self):
        self.select(self.base_ids[1]);player=self.dialog.baseline_preview.player
        player.status=QMediaPlayer.MediaStatus.EndOfMedia;player.set_state(QMediaPlayer.PlaybackState.StoppedState)
        player.setPosition(10000);self.app.processEvents()
        self.assertEqual(player.position(),10000);self.assertEqual(self.dialog.canvas.playhead_seconds,20.)
        self.dialog.toggle_playback();self.assertEqual(player.position(),9960);self.assert_active('baseline')

    def test_baseline_second_file_pause_resume_preserves_local_position(self):
        self.select(self.base_ids[1]);self.position(self.dialog.baseline_preview,2450);loads=list(self.dialog.baseline_preview.loads)
        self.dialog.toggle_playback();self.dialog.toggle_playback()
        self.assert_active('baseline');self.assertEqual(self.dialog.baseline_preview.player.position(),2450)
        self.assertEqual(self.dialog.canvas.playhead_seconds,12.45);self.assertEqual(self.dialog.baseline_preview.loads,loads)

    def test_ruler_scrub_then_resume_uses_current_alternative_and_leaves_left_frozen(self):
        self.select(self.base_ids[0]);self.position(self.dialog.baseline_preview,3300);self.select()
        self.dialog.seek_timeline(8.25);self.assert_active('alternative',False)
        self.assertEqual(self.dialog.preview.player.position(),3250)
        self.dialog.toggle_playback();self.assertEqual(self.dialog.preview.player.position(),3250)
        self.assertEqual(self.dialog.baseline_preview.player.position(),3300)

    def test_ruler_baseline_scrub_and_resume_loads_only_correct_file(self):
        self.select(self.base_ids[0]);self.dialog.seek_timeline(14.5);self.assert_active('baseline',False)
        self.assertEqual(self.dialog._baseline_path,'/qa/base1.mov');self.assertEqual(self.dialog.baseline_preview.player.position(),4500)
        loads=list(self.dialog.baseline_preview.loads);self.dialog.toggle_playback()
        self.assert_active('baseline');self.assertEqual(self.dialog.baseline_preview.player.position(),4500)
        self.assertEqual(self.dialog.baseline_preview.loads,loads)

    def test_outside_alternative_range_resume_uses_selected_clip_start(self):
        self.select();self.dialog.seek_timeline(100);self.dialog.toggle_playback()
        self.assertEqual(self.dialog.preview.player.position(),0);self.assertEqual(self.dialog.canvas.playhead_seconds,5.)

    def test_unplaced_alternative_can_preview_without_invented_baseline_position(self):
        self.select(self.ids[2]);self.assert_active('alternative');self.assertFalse(self.dialog.baseline_preview.loads)
        self.position(self.dialog.preview,1200);self.dialog.toggle_playback();self.dialog.toggle_playback()
        self.assertEqual(self.dialog.preview.player.position(),1200)

    def test_real_track_click_starts_from_clicked_local_position(self):
        canvas=self.dialog.canvas;entry=canvas._entries[self.ids[0]]
        rect=canvas.clip_rect(entry);point=rect.center().toPoint()
        expected=canvas.time_at(point.x())-entry['start']
        QTest.mouseClick(canvas.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point)
        self.assert_active('alternative')
        self.assertAlmostEqual(self.dialog.preview.player.position()/1000,expected,delta=.002)

    def test_actual_baseline_click_uses_clicked_local_position_not_clip_head(self):
        canvas=self.dialog.canvas;entry=canvas._entries[self.base_ids[1]]
        point=canvas.clip_rect(entry).center().toPoint();expected=canvas.time_at(point.x())-entry['start']
        QTest.mouseClick(canvas.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point)
        self.assert_active('baseline');self.assertAlmostEqual(self.dialog.baseline_preview.player.position()/1000,expected,delta=.002)

    def test_command_multiselection_controls_last_clicked_type_in_both_directions(self):
        canvas=self.dialog.canvas;before=copy.deepcopy(self.dialog.layout_data)
        def click(identity,modifier=Qt.KeyboardModifier.NoModifier):
            point=canvas.clip_rect(canvas._entries[identity]).center().toPoint()
            QTest.mouseClick(canvas.viewport(),Qt.MouseButton.LeftButton,modifier,point)
        click(self.ids[0])
        click(self.base_ids[1],Qt.KeyboardModifier.MetaModifier)
        self.assert_active('baseline')
        self.assertEqual(self.dialog._selected[0],self.base_ids[1])
        self.assertEqual(canvas.selection,{self.ids[0],self.base_ids[1]})
        click(self.base_ids[0])
        click(self.ids[0],Qt.KeyboardModifier.MetaModifier)
        self.assert_active('alternative')
        self.assertEqual(self.dialog._selected[0],self.ids[0])
        self.assertEqual(canvas.selection,{self.base_ids[0],self.ids[0]})
        self.assertEqual(self.dialog.layout_data,before)
        self.assertFalse(self.dialog.layout_data['items'][self.ids[0]]['alignment_reviewed'])

    def test_async_alternative_ready_uses_latest_pending_local_before_starting(self):
        self.dialog.preview.async_load=True;self.select(seconds=8.2)
        self.assert_active('alternative',False);self.assertTrue(self.dialog._alternative_wants_play)
        self.dialog.preview.ready();self.assert_active('alternative')
        self.assertEqual(self.dialog.preview.player.position(),3200);self.assertEqual(self.dialog.canvas.playhead_seconds,8.2)

    def test_late_baseline_ready_after_selecting_alternative_cannot_steal_playback(self):
        self.dialog.baseline_preview.async_load=True;self.select(self.base_ids[1],seconds=14.1)
        self.select();self.assert_active('alternative');self.dialog.baseline_preview.ready()
        self.assert_active('alternative');self.assertFalse(self.dialog.baseline_preview._play_requested)

    def test_late_alternative_ready_after_selecting_baseline_cannot_steal_playback(self):
        self.dialog.preview.async_load=True;self.select(seconds=8)
        self.select(self.base_ids[0]);self.dialog.preview.ready();self.assert_active('baseline')
        self.assertFalse(self.dialog.preview._play_requested)

    def test_queued_native_ready_pause_retries_only_current_wanted_pane(self):
        self.dialog.preview.async_load=True;self.select(seconds=8.2);self.dialog.preview.ready()
        loads=list(self.dialog.preview.loads)
        self.dialog.preview.player.pause()  # Native pause from the ready stack arrives late.
        self.assertTrue(self.dialog._preview_resume.isActive());self.app.processEvents()
        self.assert_active('alternative');self.assertEqual(self.dialog.preview.player.position(),3200)
        self.assertEqual(self.dialog.preview.loads,loads)

    def test_user_pause_cancels_pending_ready_resume(self):
        self.select();self.dialog.preview.player.pause();self.assertTrue(self.dialog._preview_resume.isActive())
        self.dialog.toggle_playback();self.app.processEvents();self.assert_active('alternative',False)
        self.assertFalse(self.dialog._alternative_wants_play)

    def test_switch_to_baseline_cancels_pending_alternative_native_resume(self):
        self.select();self.dialog.preview.player.pause();self.select(self.base_ids[0]);self.app.processEvents()
        self.assert_active('baseline');self.assertFalse(self.dialog._alternative_wants_play)

    def test_pause_while_loading_cancels_pending_play_but_keeps_seek(self):
        self.dialog.preview.async_load=True;self.select(seconds=8.4);self.dialog.toggle_playback()
        self.dialog.preview.ready();self.assert_active('alternative',False)
        self.assertEqual(self.dialog.preview.player.position(),3400)
        self.dialog.toggle_playback();self.assert_active('alternative');self.assertEqual(self.dialog.preview.player.position(),3400)

    def test_baseline_ready_after_pause_keeps_global_position_and_does_not_autoplay(self):
        self.dialog.baseline_preview.async_load=True;self.select(self.base_ids[1],seconds=14.4)
        self.dialog.toggle_playback();self.dialog.baseline_preview.ready();self.assert_active('baseline',False)
        self.assertEqual(self.dialog.baseline_preview.player.position(),4400);self.assertEqual(self.dialog.canvas.playhead_seconds,14.4)

    def test_pending_baseline_advance_cannot_restart_after_alternative_selection(self):
        self.select(self.base_ids[0]);self.position(self.dialog.baseline_preview,10000)
        self.assertTrue(self.dialog._baseline_advance.isActive());self.select();self.app.processEvents()
        self.assert_active('alternative');self.assertEqual(self.dialog._baseline_path,'/qa/base0.mov')

    def test_baseline_playback_continues_adjacent_pgm_and_stops_at_final_end(self):
        self.select(self.base_ids[0]);self.position(self.dialog.baseline_preview,10000);self.app.processEvents()
        self.assertEqual(self.dialog._baseline_path,'/qa/base1.mov');self.assert_active('baseline')
        self.position(self.dialog.baseline_preview,10000);self.app.processEvents();self.assert_active('baseline',False)

    def test_baseline_gaps_and_outside_range_clear_only_active_left(self):
        self.select();self.position(self.dialog.preview,2400);self.select(self.base_ids[0])
        for value in (-1.,20.,100.):
            self.dialog.seek_timeline(value);self.assertFalse(self.dialog._baseline_path)
            self.assertEqual(self.dialog.preview.player.position(),2400)
        self.dialog._baseline_blocks[0]['end']=7.;self.dialog.seek_timeline(8)
        self.assertFalse(self.dialog._baseline_path)

    def test_pool_selection_pauses_and_assigns_right_control_after_baseline(self):
        self.select(self.base_ids[0]);index=self.dialog.pool_model.index(self.dialog.pool_model.by_id[self.ids[2]],0)
        self.dialog.pool.setCurrentIndex(index);self.assert_active('alternative',False)
        self.dialog.toggle_playback();self.assert_active('alternative')

    def test_organizer_select_stays_paused_and_space_can_preview_right(self):
        self.select(self.base_ids[0]);self.dialog.mode.setCurrentIndex(0);self.select()
        self.assertFalse(self.dialog.baseline_panel.isVisible());self.assert_active('alternative',False)
        self.dialog.toggle_playback();self.assert_active('alternative')

    def test_programmatic_position_apply_and_worker_result_do_not_restart_previews(self):
        self.select();self.position(self.dialog.preview,2000);loads=list(self.dialog.preview.loads)
        self.dialog.position.setValue(8);self.dialog.apply_position();self.assert_active('alternative',False)
        self.assertEqual(self.dialog.preview.player.position(),2000);self.assertEqual(self.dialog.preview.loads,loads)
        self.dialog.apply_automatic_alignment(dict(manual_timeline=copy.deepcopy(self.dialog.layout_data)))
        self.assert_active('alternative',False);self.assertEqual(self.dialog.preview.loads,loads)

    def test_selected_clips_only_turn_green_after_explicit_confirmation(self):
        self.select();self.assertFalse(self.dialog.layout_data['items'][self.ids[0]]['alignment_reviewed'])
        self.dialog.confirm_selected_alignment();self.assertTrue(self.dialog.layout_data['items'][self.ids[0]]['alignment_reviewed'])
        self.dialog.position.setValue(6);self.dialog.apply_position()
        self.assertFalse(self.dialog.layout_data['items'][self.ids[0]]['alignment_reviewed'])
        self.assertEqual(self.dialog.confirm_alignment_button.text(),'确认所选对齐')

    def test_dragging_confirmed_clip_marks_blue_and_does_not_autoplay(self):
        self.select();self.dialog.confirm_selected_alignment()
        canvas=self.dialog.canvas;point=canvas.clip_rect(canvas._entries[self.ids[0]]).center().toPoint()
        QTest.mousePress(canvas.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point)
        QTest.mouseMove(canvas.viewport(),point+QPoint(48,0))
        QTest.mouseRelease(canvas.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point+QPoint(48,0))
        self.assert_active('alternative',False)
        self.assertFalse(self.dialog.layout_data['items'][self.ids[0]]['alignment_reviewed'])

    def test_no_linked_or_audio_source_controls_and_no_capture_or_mute_routing(self):
        self.assertFalse(hasattr(self.dialog,'linked_preview'));self.assertFalse(hasattr(self.dialog,'audio_source'))
        for _ in range(4):self.select();self.select(self.base_ids[0])
        for preview in (self.dialog.preview,self.dialog.baseline_preview):
            self.assertFalse(preview.preview_audio.muted);self.assertFalse(preview.preview_audio.mute.isChecked())
            self.assertEqual(preview.player.capture_changes,[])

    def test_search_text_accepts_spaces_without_global_playback(self):
        self.select(self.base_ids[0]);self.dialog.toggle_playback();self.dialog.search.setFocus()
        QTest.keyClick(self.dialog.search,Qt.Key.Key_Space)
        self.assertEqual(self.dialog.search.text(),' ');self.assert_active('baseline',False)

    def test_closed_dialog_ignores_ready_events_and_cleans_both_audio_timers(self):
        self.dialog.preview.async_load=True;self.select();self.dialog._allow_close=True;self.dialog.close()
        loads=list(self.dialog.baseline_preview.loads);self.dialog.preview.ready();self.dialog.baseline_preview.ready();self.dialog._advance_baseline()
        self.app.processEvents();self.assertEqual(self.dialog.baseline_preview.loads,loads)
        self.assertFalse(self.dialog._baseline_advance.isActive());self.assertIsNone(self.dialog._alternative_seek_pending)
        self.assertFalse(self.dialog._preview_resume.isActive())
        for preview in (self.dialog.preview,self.dialog.baseline_preview):
            self.assertTrue(preview.preview_audio.closed);self.assertFalse(preview.preview_audio.device_timer.isActive());self.assertFalse(preview._play_requested)


if __name__=='__main__':unittest.main()
