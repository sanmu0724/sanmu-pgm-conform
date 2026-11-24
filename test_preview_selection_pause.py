"""Selecting media positions a paused preview; only explicit input starts it."""
import unittest
from unittest.mock import Mock, patch

from PyQt6.QtCore import Qt
from PyQt6.QtMultimedia import QMediaPlayer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QWidget

from library_ui import InlinePreview


class InlineSelectionPauseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.preview = InlinePreview()
        self.real_player = self.preview.player
        self.preview.player = Mock()
        self.preview.player.playbackState.return_value = QMediaPlayer.PlaybackState.PausedState
        self.preview.player.position.return_value = 1000

    def tearDown(self):
        self.preview.player = self.real_player
        self.preview.close()

    def ready(self):
        self.preview.ready(QMediaPlayer.MediaStatus.LoadedMedia)

    def test_selection_loads_first_frame_without_playing(self):
        self.preview.load_media('/first.mov', 1, 8)
        self.ready()
        self.preview.player.setPosition.assert_called_once_with(1000)
        self.preview.player.play.assert_not_called()
        self.assertFalse(self.preview._play_requested)

    def test_play_before_ready_is_honoured_once_on_ready(self):
        self.preview.load_media('/first.mov', 1, 8)
        self.preview.toggle_playback()
        self.preview.player.play.assert_not_called()
        self.ready()
        self.preview.ready(QMediaPlayer.MediaStatus.BufferedMedia)
        self.preview.player.play.assert_called_once_with()

    def test_second_space_before_ready_cancels_play_intent(self):
        self.preview.load_media('/first.mov', 1, 8)
        self.preview.toggle_playback()
        self.preview.toggle_playback()
        self.ready()
        self.preview.player.play.assert_not_called()
        self.assertFalse(self.preview._play_requested)

    def test_explicit_load_play_intent_survives_same_key_refresh(self):
        self.preview.load_media('/first.mov', 1, 8, play=True)
        self.preview.load_media('/first.mov', 1, 8)
        self.ready()
        self.preview.player.play.assert_called_once_with()
        self.preview.player.setSource.assert_called_once()

    def test_same_ready_key_refresh_keeps_current_position_and_playback(self):
        self.preview.load_media('/first.mov', 1, 8)
        self.ready()
        self.preview.toggle_playback()
        self.preview.player.reset_mock()
        self.preview.load_media('/first.mov', 1, 8)
        self.preview.player.pause.assert_not_called()
        self.preview.player.stop.assert_not_called()
        self.preview.player.setPosition.assert_not_called()
        self.preview.player.setSource.assert_not_called()

    def test_new_selection_cancels_previous_unready_play_request(self):
        self.preview.load_media('/old.mov', 1, 8, play=True)
        self.preview.load_media('/new.mov', 2, 9)
        self.ready()
        self.preview.player.play.assert_not_called()
        self.preview.player.setPosition.assert_called_once_with(2000)

    def test_same_source_new_interval_reuses_decoder_and_stays_paused(self):
        self.preview.load_media('/first.mov', 1, 8, play=True)
        self.ready()
        self.preview.player.reset_mock()
        self.preview.load_media('/first.mov', 3, 7)
        self.preview.player.pause.assert_called_once_with()
        self.preview.player.setSource.assert_not_called()
        self.preview.player.setPosition.assert_called_once_with(3000)
        self.preview.player.play.assert_not_called()

    def test_pause_before_ready_clears_pending_playback(self):
        self.preview.load_media('/first.mov', 1, 8, play=True)
        self.preview.pause()
        self.ready()
        self.preview.player.play.assert_not_called()

    def test_suspended_ready_and_reopen_do_not_autoplay(self):
        self.preview.load_media('/first.mov', 1, 8, play=True)
        self.preview._suspended = True
        self.ready()
        self.preview._suspended = False
        self.preview.ready(QMediaPlayer.MediaStatus.BufferedMedia)
        self.preview.player.play.assert_not_called()

    def test_clear_cancels_unready_playback(self):
        self.preview.load_media('/first.mov', 1, 8, play=True)
        self.preview.clear()
        self.ready()
        self.preview.player.play.assert_not_called()
        self.assertEqual(self.preview.path, '')
        self.assertFalse(self.preview._play_requested)


class FakePreview(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.path = ''
        self.loads = []
        self.player = Mock()
        self.video = QWidget(self)
        self.pause = Mock()
        self.replay = Mock()
        self.toggle_playback = Mock()
        self.clear = Mock()

    def load_media(self, *args, play=False):
        self.path = args[0]
        self.loads.append((args, play))


class PendingSelectionPauseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import test_library_ui as fixtures
        self.fixture = fixtures.LibraryPanelTests()
        self.fixture.setUp()
        self.panel = self.fixture.panel
        self.factory = patch('library_ui.InlinePreview', FakePreview)
        self.factory.start()

    def tearDown(self):
        self.fixture.tearDown()
        self.factory.stop()

    def select(self, path='/selected.mov', start=1., end=8.):
        self.panel._load_inline(path, start, end)
        return self.panel._inline_request[0]

    def test_initial_selection_ready_is_loaded_with_play_false(self):
        key = self.select()
        self.panel._inline_media_ready(key, True, '')
        self.assertEqual(self.panel.inline_preview.loads, [(key, False)])
        self.assertIsNone(self.panel._inline_request)

    def test_space_before_player_exists_starts_current_request_only(self):
        self.panel.open_browser_workspace()
        key = self.select()
        self.assertIsNone(self.panel.inline_preview)
        QTest.keyClick(self.panel.story_timeline.canvas, Qt.Key.Key_Space)
        self.assertEqual(self.panel._inline_request, (key, True))
        self.panel._inline_media_ready(key, True, '')
        self.assertEqual(self.panel.inline_preview.loads, [(key, True)])

    def test_second_space_during_loading_cancels_current_request(self):
        self.panel.open_browser_workspace()
        key = self.select()
        QTest.keyClick(self.panel.story_timeline.canvas, Qt.Key.Key_Space)
        QTest.keyClick(self.panel.story_timeline.canvas, Qt.Key.Key_Space)
        self.panel._inline_media_ready(key, True, '')
        self.assertEqual(self.panel.inline_preview.loads, [(key, False)])

    def test_current_request_refresh_keeps_explicit_play_without_new_io(self):
        key = self.select()
        self.panel._toggle_inline_playback()
        generation = self.panel._inline_io.state['generation']
        with patch.object(self.panel._inline_timer, 'start') as restart:
            self.panel._load_inline(*key)
        self.assertEqual(self.panel._inline_request, (key, True))
        self.assertEqual(self.panel._inline_io.state['generation'], generation)
        restart.assert_not_called()
        self.panel._inline_media_ready(key, True, '')
        self.assertEqual(self.panel.inline_preview.loads, [(key, True)])

    def test_play_button_during_loading_is_forwarded_to_selected_source(self):
        session = self.fixture.seed_angle_match()
        clip = self.panel._candidate()
        self.panel._story_take_selected(clip['id'])
        key = self.panel._inline_request[0]
        self.panel.preview_workspace_selection()
        self.assertEqual(self.panel._inline_request, (key, True))
        self.panel._inline_media_ready(key, True, '')
        self.assertEqual(self.panel.inline_preview.loads, [(key, True)])
        self.assertIn('angle_PGM', session['config']['pgm'])

    def test_new_selection_pauses_old_player_immediately_and_space_never_resumes_it(self):
        old = self.select('/old.mov')
        self.panel._inline_media_ready(old, True, '')
        preview = self.panel.inline_preview
        new = self.select('/new.mov')
        preview.pause.assert_called_once_with()
        self.panel._toggle_inline_playback()
        preview.toggle_playback.assert_not_called()
        self.panel._inline_media_ready(new, True, '')
        self.assertEqual(preview.loads, [(old, False), (new, True)])

    def test_old_loading_callback_cannot_restore_previous_selection(self):
        old = self.select('/old.mov')
        self.panel._toggle_inline_playback()
        new = self.select('/new.mov')
        self.panel._inline_media_ready(old, True, '')
        self.assertIsNone(self.panel.inline_preview)
        self.panel._inline_media_ready(new, True, '')
        self.assertEqual(self.panel.inline_preview.loads, [(new, False)])

    def test_same_loaded_key_refresh_does_not_pause_or_reload(self):
        key = self.select()
        self.panel._inline_media_ready(key, True, '')
        preview = self.panel.inline_preview
        self.panel._load_inline(*key)
        preview.pause.assert_not_called()
        preview.replay.assert_not_called()
        self.assertEqual(preview.loads, [(key, False)])

    def test_close_workspace_cancels_pending_play_intent_and_reopen_stays_paused(self):
        self.panel.open_browser_workspace()
        key = self.select()
        self.panel._toggle_inline_playback()
        self.panel.close_browser_workspace()
        QApplication.processEvents()
        self.assertEqual(self.panel._inline_request, (key, False))
        self.panel.open_browser_workspace()
        self.panel._inline_media_ready(key, True, '')
        self.assertEqual(self.panel.inline_preview.loads, [(key, False)])

    def test_technical_stage_cancels_both_queued_and_decoder_play_intents(self):
        old = self.select('/old.mov')
        self.panel._inline_media_ready(old, True, '')
        preview = self.panel.inline_preview
        new = self.select('/new.mov')
        self.panel._toggle_inline_playback()
        self.panel.set_workflow_stage('sound')
        self.assertEqual(self.panel._inline_request, (new, False))
        self.assertEqual(preview.pause.call_count, 2)

    def test_double_clicking_match_thumbnail_does_not_request_playback(self):
        self.fixture.seed_angle_match()
        with patch.object(self.panel, 'preview_current_inline') as play:
            self.panel.match_list.itemDoubleClicked.emit(self.panel.match_list.currentItem())
        play.assert_not_called()
        self.assertFalse(self.panel._inline_request[1])

    def test_double_clicking_media_pool_item_does_not_request_playback(self):
        self.fixture.seed_angle_match()
        self.panel.remove_pgm_match()
        with patch.object(self.panel, 'preview_pending_inline') as play:
            self.panel.pending_list.itemDoubleClicked.emit(self.panel.pending_list.currentItem())
        play.assert_not_called()
        self.assertFalse(self.panel._inline_request[1])


if __name__ == '__main__':
    unittest.main()
