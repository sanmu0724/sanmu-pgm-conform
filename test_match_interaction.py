import copy
import tempfile
import unittest
import time
import wave
from pathlib import Path
from unittest.mock import Mock, patch
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDialog, QLineEdit, QPushButton
from PyQt6.QtMultimedia import QMediaPlayer
from library_ui import LibraryPanel, InlinePreview
from library_timeline import build_timeline_data
from pgm_intervals import build_pgm_intervals
from test_pgm_intervals import catalogue, add_take


class MatchInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        with patch('library_bridge.read_settings', return_value={}):
            self.panel = LibraryPanel(lambda: {}, lambda _: None, auto_restore=False)
        # This fixture supplies immutable snapshots, not a real saved project;
        # adding its placeholder ID must not launch a DB refresh task.
        self.panel.production.blockSignals(True)
        self.panel.production.addItem('QA', 'play')
        self.panel.production.blockSignals(False)
        self.panel.set_workflow_stage('script')
        self.panel.resize(1200, 800)
        self.panel.show()
        QApplication.processEvents()
        self.panel.open_browser_workspace()
        self.panel._browser_dialog.showNormal()
        self.panel._browser_dialog.resize(1440, 900)
        QApplication.processEvents()
        self.panel._load_inline = Mock()

    def tearDown(self):
        self.panel.close()
        QApplication.processEvents()

    def data(self, score=.85, first=.85):
        data = catalogue()
        add_take(data, 'other', 'PIX_001_040', (1, 2, 3))
        for i, row in enumerate(data['candidates']):
            row.update(status='candidate', score=first if i == 0 else score, tags=['auto_dialogue'])
        data['timeline_nodes'] = build_timeline_data(data, whole_pgm=True)
        data['pgm_interval_nodes'] = build_pgm_intervals(data)
        self.panel._set_data(data)
        self.panel.story_axis.setCurrentIndex(1)
        return data

    def preview(self):
        preview = InlinePreview(self.panel.inline_host)
        self.panel.inline_layout.addWidget(preview)
        self.panel.inline_preview = preview
        preview.path = '/test/video.mov'
        return preview

    def test_space_from_list_button_slider_and_video_toggles_once(self):
        self.data()
        preview = self.preview()
        with patch.object(preview, 'toggle_playback') as toggle:
            for widget in (self.panel.story_timeline.canvas, self.panel.workspace_play, preview.seek, preview.video):
                widget.setFocus()
                QTest.keyClick(widget, Qt.Key.Key_Space)
            self.assertEqual(toggle.call_count, 4)
            QApplication.sendEvent(self.panel.story_timeline.canvas, QKeyEvent(
                QEvent.Type.KeyPress, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier, ' ', True))
            self.assertEqual(toggle.call_count, 4)

    def test_space_preserves_text_editing_and_modal_dialogs(self):
        self.data()
        preview = self.preview()
        with patch.object(preview, 'toggle_playback') as toggle:
            self.panel.pending_search.setFocus()
            QTest.keyClick(self.panel.pending_search, Qt.Key.Key_Space)
            self.assertEqual(self.panel.pending_search.text(), ' ')
            dialog = QDialog(self.panel)
            dialog.setModal(True)
            button = QPushButton('QA', dialog)
            dialog.show()
            QApplication.processEvents()
            QTest.keyClick(button, Qt.Key.Key_Space)
            dialog.close()
            toggle.assert_not_called()

    def test_space_does_not_intercept_other_window_or_material_stage(self):
        preview = self.preview()
        other = QPushButton('other')
        other.show()
        with patch.object(preview, 'toggle_playback') as toggle:
            QTest.keyClick(other, Qt.Key.Key_Space)
            self.panel.set_workflow_stage('materials')
            QTest.keyClick(self.panel.refresh_button, Qt.Key.Key_Space)
            toggle.assert_not_called()
        other.close()

    def test_fullscreen_video_space_toggles_once(self):
        preview = self.preview()
        preview.video.setFullScreen(True)
        QApplication.processEvents()
        with patch.object(preview, 'toggle_playback') as toggle:
            QTest.keyClick(preview.video, Qt.Key.Key_Space)
            toggle.assert_called_once()
        preview.video.setFullScreen(False)

    def test_native_player_pause_resume_via_space_keeps_media_unchanged(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'native-preview.wav'
            with wave.open(str(path), 'wb') as sound:
                sound.setparams((1, 2, 48000, 8*48000, 'NONE', 'not compressed'))
                sound.writeframes(b'\0\0' * (8*48000))
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            preview = self.preview()
            preview.load_media(str(path), 1, 7)
            def wait_until(condition):
                deadline = time.monotonic()+4
                while time.monotonic() < deadline:
                    QApplication.processEvents()
                    if condition():
                        return
                    QTest.qWait(10)
                self.fail(preview.player.errorString() or 'Native playback did not reach expected state')
            wait_until(lambda: preview.positioned and preview.player.position() >= 1000)
            QTest.qWait(100)
            self.assertNotEqual(preview.player.playbackState(), QMediaPlayer.PlaybackState.PlayingState)
            self.assertLess(preview.player.position(), 1100)
            QTest.keyClick(preview.video, Qt.Key.Key_Space)
            wait_until(lambda: preview.player.position() > 1100 and
                       preview.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState)
            QTest.keyClick(preview.video, Qt.Key.Key_Space)
            wait_until(lambda: preview.player.playbackState() == QMediaPlayer.PlaybackState.PausedState)
            position = preview.player.position()
            QTest.keyClick(preview.video, Qt.Key.Key_Space)
            wait_until(lambda: preview.player.position() > position+80)
            self.assertGreaterEqual(preview.player.position(), position)
            preview.clear()
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_pause_resumes_position_and_end_restarts_range(self):
        preview = self.preview()
        preview.positioned = True
        preview.start_ms, preview.end_ms = 2000, 6000
        original = preview.player
        preview.player = Mock()
        preview.player.playbackState.return_value = QMediaPlayer.PlaybackState.PlayingState
        preview.toggle_playback()
        preview.player.pause.assert_called_once()
        preview.player.reset_mock()
        preview.player.playbackState.return_value = QMediaPlayer.PlaybackState.PausedState
        preview.player.position.return_value = 3500
        with patch.object(preview.preview_audio, 'seek') as seek:
            preview.toggle_playback()
            seek.assert_not_called()
            preview.player.play.assert_called_once()
            preview.player.position.return_value = 6000
            preview.toggle_playback()
            seek.assert_called_once_with(2000)
        preview.player = original

    def test_whole_file_success_without_first_alignment_exposes_no_export_selection(self):
        self.data(score=.98, first=.2)
        summary = self.panel._take_index().file_summary(self.panel._candidate())
        self.assertEqual(summary['match_status'], 'matched')
        self.assertFalse(summary['alignment_ready'])
        self.assertTrue(self.panel.select_take_button.isHidden())
        self.assertEqual(self.panel._automatic_export_pairs(), {})
        self.assertFalse(hasattr(self.panel, 'workspace_export'))

    def test_eligible_whole_file_still_requires_green_review_not_an_add_action(self):
        self.data()
        self.assertTrue(self.panel.select_take_button.isHidden())
        self.assertEqual(self.panel._automatic_export_pairs(), {})
        self.assertFalse(any('加入备选' in button.text() or '移出备选' in button.text()
                             for button in self.panel.findChildren(QPushButton)))

    def test_baseline_confirmation_targets_current_sentence_not_file_first(self):
        data = self.data(score=.3)
        self.panel.apply_reference_state({'reference_session_id': 'other', 'revision': 2})
        chosen = dict(data['candidates'][2], session_id='other', media_path=data['transcripts'][2]['media_path'],
                      start_seconds=4, end_seconds=4.8)
        self.panel._visible_candidates = [chosen]
        self.panel.render_matches()
        self.assertEqual(self.panel.select_take_button.text(), '确认基准位置')
        events = []
        self.panel.clip_selection_changed.connect(events.append)
        self.panel.confirm_current_baseline()
        self.assertEqual(events[0]['candidate_id'], chosen['id'])
        self.assertTrue(events[0]['confirm'])
        self.assertEqual(events[0]['expected_revision'], 2)

    def test_compact_cards_omit_dialogue_and_preserve_items_on_filter_refresh(self):
        self.data()
        item = self.panel.match_list.item(0)
        self.assertIn('PIX_001_040.mov', item.text())
        self.assertEqual(len(item.text().splitlines()), 2)
        self.assertNotIn('台词1', item.text())
        self.panel.filter_candidates()
        self.assertIs(self.panel.match_list.item(0), item)
        self.assertEqual(self.panel.match_list.currentRow(), 0)

    def test_thumbnail_click_cannot_create_a_separate_export_selection(self):
        self.data()
        QApplication.processEvents()
        item = self.panel.match_list.item(0)
        self.assertFalse(item.flags() & Qt.ItemFlag.ItemIsUserCheckable)
        self.assertIsNone(item.data(Qt.ItemDataRole.CheckStateRole))
        rect = self.panel.match_list.visualItemRect(item)
        events = []
        self.panel.clip_selection_changed.connect(events.append)
        QTest.mouseClick(self.panel.match_list.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        self.assertEqual(events, [])
        self.assertEqual(self.panel._automatic_export_pairs(), {})

    def test_matched_missing_first_keeps_range_editor_without_alternative_selection(self):
        data = self.data()
        data['transcripts'].append(dict(data['transcripts'][0], id='missing', start_seconds=.1,
                                        end_seconds=.5, text='嗯'))
        data['timeline_nodes'] = build_timeline_data(data, whole_pgm=True)
        data['pgm_interval_nodes'] = build_pgm_intervals(data)
        self.panel._set_data(data)
        self.assertTrue(self.panel.select_take_button.isHidden())
        self.assertTrue(self.panel.coverage_apply_button.isEnabled())
        self.assertEqual(self.panel._automatic_export_pairs(), {})


if __name__ == '__main__':
    unittest.main()
