"""Independent PGM editor integration; synthetic data and temporary stores."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PyQt6.QtCore import Qt, QPoint, QPointF
from PyQt6.QtGui import QKeySequence, QWheelEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDialog, QPushButton, QWidget

from library_ui import LibraryPanel
from library_timeline import build_timeline_data
from pgm_intervals import build_pgm_intervals
from test_pgm_intervals import catalogue, add_take


def coverage_data():
    data = catalogue()
    for row in data['scripts']:
        row['speaker'] = '杰克' if row['order'] % 2 else '阿尔吉'
        row['text'] = '这是第%03d段完整台词。This is the complete script paragraph.' % row['order']
    add_take(data, 'baseline', 'continuous', (10, 20, 30))
    add_take(data, 'backup', 'short', (20, 21))
    add_take(data, 'backup', 'later', (60, 61))
    add_take(data, 'other_day', 'overlap', (20, 21))
    add_take(data, 'other_day', 'tiny', (22,))
    data['reference_state'] = dict(reference_session_id='baseline', selected_clip_pairs={})
    data['timeline_nodes'] = build_timeline_data(data, whole_pgm=True)
    data['pgm_interval_nodes'] = build_pgm_intervals(data)
    return data


def source_config(config):
    """Editor review/removal decisions may change; source relations may not."""
    result = copy.deepcopy(config)
    result.pop('library_pgm_editor', None)
    return result


class CoverageIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.settings_patch = patch('library_bridge.data_dir', return_value=Path(self.home.name))
        self.settings_patch.start()
        with patch('library_bridge.read_settings', return_value={}):
            self.panel = LibraryPanel(lambda: {}, lambda _: None, auto_restore=False)
        blocked = self.panel.production.blockSignals(True)
        self.panel.production.addItem('QA测试台本', 'qa-only')
        self.panel.production.blockSignals(blocked)
        self.panel._load_inline = Mock()
        self.data = coverage_data()
        self.panel._set_data(self.data)
        self.panel.set_workflow_stage('script')
        self.panel.resize(1440, 900)
        self.panel.show()
        self.app.processEvents()
        self.panel.open_browser_workspace()
        self.dialog = self.panel._browser_dialog
        self.dialog.showNormal()
        self.dialog.resize(1440, 900)
        self.app.processEvents()
        QTest.qWait(30)
        self.panel.script_timeline.doItemsLayout()
        self.app.processEvents()
        self.canvas = self.panel.story_timeline.canvas

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        self.app.processEvents()
        self.settings_patch.stop()
        self.home.cleanup()

    def identity(self, filename):
        return next(row.candidate_id for row in self.canvas.index.takes if row.filename == filename)

    def click_take(self, filename):
        identity = self.identity(filename)
        self.canvas.select_take(identity)
        self.app.processEvents()
        rect = self.canvas.take_rect(identity)
        QTest.mouseClick(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=rect.center().toPoint())
        self.app.processEvents()
        return identity

    def click_line(self, line_id):
        self.canvas.select_line(line_id)
        self.app.processEvents()
        rect = self.canvas.line_rect(self.canvas.index.positions[line_id])
        QTest.mouseClick(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=rect.center().toPoint())
        self.app.processEvents()

    def test_blue_click_positions_script_and_loads_exact_alternative_paused(self):
        self.panel._load_inline.reset_mock()
        identity = self.click_take('short.mov')
        self.assertEqual(self.panel.script_timeline.current_line_id(), 'l20')
        self.assertEqual(self.panel._script_filter, 'l20')
        self.assertEqual(self.panel._candidate()['id'], identity)
        self.assertEqual(self.panel._candidate()['media_path'], '/nas/backup/short.mov')
        self.assertEqual((self.panel._candidate()['start_seconds'], self.panel._candidate()['end_seconds']), (0, 20))
        self.assertEqual(self.panel._load_inline.call_args.args, ('/nas/backup/short.mov', 0., 20.))
        self.assertFalse(self.panel._load_inline.call_args.kwargs.get('replay', False))
        rect = self.panel.script_timeline.visualItemRect(self.panel.script_timeline.currentItem())
        self.assertGreaterEqual(rect.top(), 0)
        self.assertLess(rect.top(), self.panel.script_timeline.viewport().height())

    def test_overlapping_pgm_blocks_remain_independently_selectable(self):
        for filename, path in (('short.mov', '/nas/backup/short.mov'), ('overlap.mov', '/nas/other_day/overlap.mov')):
            identity = self.click_take(filename)
            self.assertEqual(self.panel._candidate()['id'], identity)
            self.assertEqual(self.panel.angle_choice.currentData()['media_path'], path)
        self.assertNotEqual(self.canvas._layout[self.identity('short.mov')][0], self.canvas._layout[self.identity('overlap.mov')][0])

    def test_single_block_click_loads_only_selected_pgm_once(self):
        self.click_take('short.mov')
        self.panel._load_inline.reset_mock()
        self.click_take('overlap.mov')
        self.panel._load_inline.assert_called_once_with('/nas/other_day/overlap.mov', 0., 20.)
        self.assertFalse(getattr(self.panel, '_selecting_story_take', False))

    def test_selected_block_highlights_every_covered_script_line_and_no_other_line(self):
        self.click_take('continuous.mov')
        expected = {f'l{n}' for n in range(10, 31)}
        self.assertEqual(self.panel.script_timeline._highlighted_line_ids, expected)
        self.assertEqual(self.canvas.highlighted_line_ids, expected)
        self.click_take('short.mov')
        self.assertEqual(self.panel.script_timeline._highlighted_line_ids, {'l20', 'l21'})
        self.assertEqual(self.canvas.highlighted_line_ids, {'l20', 'l21'})

    def test_white_ruler_click_navigates_clears_old_pgm_and_highlights_only_clicked_line(self):
        self.click_take('short.mov')
        self.assertGreater(self.panel.match_list.count(), 0)
        with patch.object(self.panel, '_clear_inline') as clear:
            self.click_line('l90')
            self.assertTrue(clear.called)
        self.assertEqual(self.panel.script_timeline.current_line_id(), 'l90')
        self.assertEqual(self.panel._selected_story_take_id, '')
        self.assertEqual(self.canvas.selected_candidate_id, '')
        self.assertEqual(self.canvas.highlighted_line_ids, {'l90'})
        self.assertEqual(self.panel.script_timeline._highlighted_line_ids, {'l90'})
        self.assertFalse(self.panel.workspace_inspector.isVisible())

    def test_search_and_legacy_filters_keep_full_script_not_fragmented_nodes(self):
        self.panel.script_mode.setCurrentIndex(self.panel.script_mode.findData('multiple'))
        self.panel.script_search.setText('第060段')
        self.app.processEvents()
        tree = self.panel.script_timeline
        self.assertTrue(tree.isVisible())
        self.assertFalse(self.panel.pgm_timeline.isVisible())
        self.assertEqual(self.panel.story_axis.count(), 1)
        self.assertEqual(self.panel.story_axis.currentData(), 'pgm')
        self.assertFalse(self.panel.story_axis.isVisible())
        self.assertEqual(tree.current_line_id(), 'l60')
        self.assertEqual(len(tree._items), len(self.data['scripts']))
        self.assertTrue(all(not item.isHidden() and not item.parent().isHidden() for item in tree._items.values()))
        self.assertTrue(all('机位片段' not in item.text(0) and '条素材' not in item.text(0) for item in tree._items.values()))
        self.panel.script_search.setText('91')
        self.assertEqual(tree.current_line_id(), 'l90')

    def test_plain_wheel_scrolls_stacked_tracks_and_shift_scrolls_script_horizontally(self):
        data = copy.deepcopy(self.data)
        for number in range(24):
            add_take(data, 'extra_day_'+str(number), 'extra_'+str(number), (20, 21))
        data['timeline_nodes'] = build_timeline_data(data, whole_pgm=True)
        data['pgm_interval_nodes'] = build_pgm_intervals(data)
        self.panel._set_data(data)
        self.app.processEvents()
        horizontal, vertical = self.canvas.horizontalScrollBar(), self.canvas.verticalScrollBar()
        self.assertGreater(vertical.maximum(), 0)
        horizontal.setValue(0)
        vertical.setValue(vertical.maximum() // 2)
        previous_vertical = vertical.value()
        event = QWheelEvent(QPointF(200, 80), QPointF(200, 80), QPoint(), QPoint(0, -120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        self.app.sendEvent(self.canvas.viewport(), event)
        self.assertGreater(vertical.value(), previous_vertical)
        self.assertEqual(horizontal.value(), 0)
        previous_vertical = vertical.value()
        event = QWheelEvent(QPointF(200, 80), QPointF(200, 80), QPoint(), QPoint(0, -120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.ShiftModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        self.app.sendEvent(self.canvas.viewport(), event)
        self.assertGreater(horizontal.value(), 0)
        self.assertEqual(vertical.value(), previous_vertical)

    def test_baseline_grey_border_and_backup_independent_coverage_preserves_source_positions(self):
        base = self.canvas.index.by_candidate[self.identity('continuous.mov')]
        short = self.canvas.index.by_candidate[self.identity('short.mov')]
        self.assertTrue(base.reference)
        self.assertFalse(short.reference)
        self.assertFalse(base.reviewed)
        self.assertEqual(self.canvas.take_colors(base), ('#4b92d2', '#687887'))
        self.assertEqual((base.first, base.last), (10, 30))
        self.assertEqual((short.first, short.last), (20, 21))
        before = copy.deepcopy(self.data)
        self.canvas.set_line_width(20)
        self.canvas.fit_all()
        self.assertEqual(self.data, before)

    def test_short_block_is_clickable_in_full_overview(self):
        self.canvas.fit_all()
        self.click_take('tiny.mov')
        self.assertEqual(self.panel._candidate()['media_path'], '/nas/other_day/tiny.mov')
        self.assertEqual(self.panel.script_timeline._highlighted_line_ids, {'l22'})
        self.assertGreaterEqual(self.canvas.take_rect(self.identity('tiny.mov')).width(), self.canvas.MIN_BLOCK_WIDTH)

    def test_coverage_input_single_line_and_range_dispatch_full_ids_for_selected_file(self):
        self.click_take('short.mov')
        self.assertEqual(self.panel.coverage_range_input.text(), '21-22')
        self.assertTrue(self.panel.coverage_range_input.isEnabled())
        for text, expected in [('55-76', [f'l{n}' for n in range(54, 76)]), ('55', ['l54'])]:
            with patch.object(self.panel, '_save_story_coverage') as save:
                self.panel.coverage_range_input.setText(text)
                self.panel.apply_coverage_input()
                self.assertEqual(save.call_args.args[0]['media_path'], '/nas/backup/short.mov')
                self.assertEqual(save.call_args.args[1], expected)

    def test_double_click_focuses_range_editor_for_exact_take(self):
        identity = self.identity('short.mov')
        self.canvas.select_take(identity)
        self.app.processEvents()
        QTest.mouseDClick(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=self.canvas.take_rect(identity).center().toPoint())
        self.app.processEvents()
        self.assertEqual(self.panel._candidate()['id'], identity)
        self.assertTrue(self.panel.coverage_range_input.hasFocus())
        self.assertEqual(self.panel.coverage_range_input.selectedText(), '21-22')

    def test_incremental_import_adds_lanes_and_retains_selected_pgm_identity(self):
        identity = self.click_take('short.mov')
        count = len(self.canvas.lanes)
        data = copy.deepcopy(self.data)
        add_take(data, 'new_day', 'new_overlap', (20, 21))
        data['timeline_nodes'] = build_timeline_data(data, whole_pgm=True)
        data['pgm_interval_nodes'] = build_pgm_intervals(data)
        self.panel._set_data(data)
        self.app.processEvents()
        self.assertGreater(len(self.canvas.lanes), count)
        self.assertEqual(self.panel._selected_story_take_id, identity)
        self.assertEqual(self.canvas.selected_candidate_id, identity)
        self.assertIn('new_overlap.mov', [row.filename for row in self.canvas.index.takes])
        self.assertEqual(self.panel.script_timeline._highlighted_line_ids, {'l20', 'l21'})

    def test_narrow_windows_keep_full_height_columns_and_large_preview_editor(self):
        self.click_take('short.mov')
        for width, height in ((1440, 900), (1024, 768), (1000, 648), (1000, 640)):
            self.dialog.resize(width, height)
            self.app.processEvents()
            QTest.qWait(30)  # Script paragraphs reflow on a 10ms debounce.
            self.assertEqual((self.dialog.width(), self.dialog.height()), (width, height))
            self.assertGreater(self.panel.script_timeline.width(), 100)
            self.assertGreaterEqual(self.panel.inline_host.width(), 340)
            self.assertEqual(self.panel.browser_split.orientation(), Qt.Orientation.Horizontal)
            self.assertEqual(self.panel.workspace_right.orientation(), Qt.Orientation.Vertical)
            self.assertEqual(self.panel.browser_split.count(), 3)
            self.assertGreaterEqual(self.panel.workspace_library.width(), 190)
            self.assertGreaterEqual(self.panel.workspace_right.width(), 400)
            self.assertGreaterEqual(self.panel.story_timeline.height(), 220)
            self.assertGreaterEqual(self.panel.story_timeline.canvas.height(), 175)
            self.assertGreater(self.panel.workspace_library.height(), self.panel.story_timeline.height())
            self.assertFalse(self.panel.compare_scroll.isVisible())
            self.assertFalse(self.panel.comparison_actions.isVisible())
            rect = self.panel.script_timeline.visualItemRect(self.panel.script_timeline.currentItem())
            self.assertTrue(rect.intersects(self.panel.script_timeline.viewport().rect()),
                            '缩小窗口后必须继续看到选中PGM对应的台本区间')

    def test_independent_workspace_returns_to_launch_page_and_reopens_same_state(self):
        identity = self.click_take('short.mov')
        widgets = self.panel.catalogue_page, self.panel.story_timeline, self.panel.inline_host
        dialog = self.panel._browser_dialog
        self.assertTrue(dialog.isVisible())
        self.assertFalse(dialog.isModal())
        self.assertEqual(self.panel.content_stack.indexOf(self.panel.catalogue_page), -1)
        self.assertEqual(self.panel.content_stack.currentWidget(), self.panel.catalogue_launch_page)
        self.assertTrue(dialog.isAncestorOf(self.panel.catalogue_page))
        self.assertTrue(self.panel.coverage_range_input.isVisible())
        self.assertEqual(self.panel._candidate()['id'], identity)
        self.canvas.horizontalScrollBar().setValue(600)
        width = self.canvas.line_width
        self.panel.open_browser_workspace()
        self.assertIs(self.panel._browser_dialog, dialog)
        dialog.close()
        self.app.processEvents()
        self.assertIsNone(self.panel._browser_dialog)
        self.assertEqual(widgets, (self.panel.catalogue_page, self.panel.story_timeline, self.panel.inline_host))
        self.assertEqual(self.panel.content_stack.currentWidget(), self.panel.catalogue_launch_page)
        self.assertFalse(self.panel.catalogue_page.isVisible())
        self.assertEqual(self.panel._candidate()['id'], identity)
        self.assertEqual(self.canvas.horizontalScrollBar().value(), 600)
        self.assertTrue(self.panel.expand_browser_button.isVisible())
        self.panel.open_browser_workspace()
        self.app.processEvents()
        self.assertIsNot(self.panel._browser_dialog, dialog)
        self.assertEqual(widgets, (self.panel.catalogue_page, self.panel.story_timeline, self.panel.inline_host))
        self.assertEqual(self.panel._candidate()['id'], identity)
        self.assertEqual(self.canvas.horizontalScrollBar().value(), 600)
        self.assertEqual(self.canvas.line_width, width)

    def test_space_in_independent_canvas_toggles_once_and_range_input_keeps_typing(self):
        class Preview(QWidget):
            def __init__(self, parent):
                super().__init__(parent)
                self.path = '/fake/preview.mov'
                self.video = self
                self.toggle_playback = Mock()
        preview = Preview(self.panel.inline_host)
        self.panel.inline_preview = preview
        self.panel.inline_layout.addWidget(preview)
        self.panel.open_browser_workspace()
        self.app.processEvents()
        self.canvas.setFocus()
        QTest.keyClick(self.canvas, Qt.Key.Key_Space)
        self.assertEqual(preview.toggle_playback.call_count, 1)
        self.panel.coverage_range_input.setFocus()
        self.panel.coverage_range_input.setText('55')
        QTest.keyClick(self.panel.coverage_range_input, Qt.Key.Key_Space)
        self.assertEqual(preview.toggle_playback.call_count, 1)
        self.assertEqual(self.panel.coverage_range_input.text(), '55 ')
        self.panel._browser_dialog.close()
        self.app.processEvents()
        self.panel.open_browser_workspace()
        self.app.processEvents()
        self.canvas.setFocus()
        QTest.keyClick(self.canvas, Qt.Key.Key_Space)
        self.assertEqual(preview.toggle_playback.call_count, 2)

    def test_main_stage_only_contains_launcher_and_browser_is_an_independent_nonmodal_window(self):
        self.assertEqual(self.panel.content_stack.currentWidget(), self.panel.catalogue_launch_page)
        self.assertEqual(self.panel.content_stack.indexOf(self.panel.catalogue_page), -1)
        self.assertTrue(self.dialog.isWindow())
        self.assertFalse(self.dialog.isModal())
        self.assertGreaterEqual(self.dialog.minimumWidth(), 1000)
        self.assertGreaterEqual(self.dialog.minimumHeight(), 640)
        self.assertTrue(self.dialog.isAncestorOf(self.panel.catalogue_page))
        self.assertTrue(self.dialog.isAncestorOf(self.panel.script_timeline))
        self.assertTrue(self.dialog.isAncestorOf(self.panel.story_timeline))
        self.assertTrue(self.dialog.isAncestorOf(self.panel.inline_host))
        self.assertIs(self.panel.browser_split.widget(1), self.panel.workspace_library)
        self.assertIs(self.panel.browser_split.widget(2), self.panel.workspace_right)
        self.assertIs(self.panel.workspace_right.widget(1), self.panel.story_timeline)
        self.assertTrue(self.panel.workspace_right.isAncestorOf(self.panel.inline_host))
        self.assertTrue(self.panel.workspace_library.isAncestorOf(self.panel.pending_list))
        self.assertGreater(self.panel.browser_split.height(), self.panel.story_timeline.height())
        self.assertFalse(self.panel.main_pages.isVisible())
        self.assertFalse(self.panel.match_list.isVisible())
        self.assertFalse(self.panel.remove_match_button.isVisible())

    def test_stage_switch_keeps_browser_widgets_in_shared_window(self):
        identity = self.click_take('short.mov')
        client = object()
        self.panel.client = client
        for stage in ('reference', 'script', 'materials', 'script'):
            self.panel.set_workflow_stage(stage)
            self.app.processEvents()
            self.assertIs(self.panel._browser_dialog, self.dialog)
            self.assertTrue(self.dialog.isAncestorOf(self.panel.catalogue_page))
            self.assertEqual(self.panel.content_stack.indexOf(self.panel.catalogue_page), -1)
            self.assertEqual(self.panel._candidate()['id'], identity)
            self.assertIs(self.panel.client, client)

    def test_main_launcher_has_one_browser_entry_and_no_separate_pending_entry(self):
        labels = [button.text() for button in self.panel.catalogue_launch_page.findChildren(QPushButton)]
        self.assertEqual(labels, ['打开PGM时间轴'])
        self.assertFalse(hasattr(self.panel, 'browser_pending_button'))
        # The legacy host method remains compatible without adding another
        # user-facing entry or recreating the shared workspace.
        self.panel.open_pending_workspace()
        self.app.processEvents()
        self.assertIs(self.panel._browser_dialog, self.dialog)
        self.assertEqual(self.panel.main_pages.currentIndex(), 1)
        self.panel.close_browser_workspace()
        self.app.processEvents()
        self.assertIsNone(self.panel._browser_dialog)
        with patch.object(self.panel, 'open_manual_timeline') as manual:
            QTest.mouseClick(self.panel.browser_open_button, Qt.MouseButton.LeftButton)
            self.app.processEvents()
        manual.assert_called_once_with()
        self.assertIsNone(self.panel._browser_dialog)

    def test_browser_save_button_and_native_shortcut_emit_once_each_and_respect_busy(self):
        saved = Mock()
        self.panel.save_project_requested.connect(saved)
        QTest.mouseClick(self.panel.browser_save_button, Qt.MouseButton.LeftButton)
        self.assertEqual(saved.call_count, 1)
        stroke = QKeySequence(QKeySequence.StandardKey.Save)[0]
        self.canvas.setFocus()
        QTest.keyClick(self.canvas, stroke.key(), stroke.keyboardModifiers())
        self.app.processEvents()
        self.assertEqual(saved.call_count, 2)
        self.panel.set_external_busy(True)
        QTest.keyClick(self.canvas, stroke.key(), stroke.keyboardModifiers())
        self.app.processEvents()
        self.assertEqual(saved.call_count, 2)
        self.panel.set_external_busy(False)
        self.panel.close_browser_workspace()
        self.app.processEvents()
        self.panel.open_browser_workspace()
        self.app.processEvents()
        self.canvas.setFocus()
        QTest.keyClick(self.canvas, stroke.key(), stroke.keyboardModifiers())
        self.app.processEvents()
        self.assertEqual(saved.call_count, 3, '重开后旧快捷键不能再次触发保存')

    def test_browser_space_works_when_main_panel_hidden_and_does_not_capture_another_window(self):
        class Preview(QWidget):
            def __init__(self, parent):
                super().__init__(parent)
                self.path = '/fake/preview.mov'
                self.video = self
                self.toggle_playback = Mock()
                self.player = Mock()
        preview = Preview(self.panel.inline_host)
        self.panel.inline_layout.addWidget(preview)
        self.panel.inline_preview = preview
        self.panel.hide()
        self.dialog.show()
        self.app.processEvents()
        self.assertFalse(self.panel.isVisible())
        self.assertTrue(self.dialog.isVisible())
        self.canvas.setFocus()
        QTest.keyClick(self.canvas, Qt.Key.Key_Space)
        self.assertEqual(preview.toggle_playback.call_count, 1)
        other = QWidget()
        other.show()
        try:
            QTest.keyClick(other, Qt.Key.Key_Space)
            self.assertEqual(preview.toggle_playback.call_count, 1)
        finally:
            other.close()
        modal = QDialog(self.dialog)
        modal.setModal(True)
        modal.show()
        self.app.processEvents()
        try:
            QTest.keyClick(self.canvas, Qt.Key.Key_Space)
            self.assertEqual(preview.toggle_playback.call_count, 1)
        finally:
            modal.close()

    def test_space_in_main_workflow_does_not_play_visible_browser_preview(self):
        class Preview(QWidget):
            def __init__(self, parent):
                super().__init__(parent)
                self.path = '/fake/preview.mov'
                self.video = self
                self.toggle_playback = Mock()
                self.player = Mock()
        preview = Preview(self.panel.inline_host)
        self.panel.inline_layout.addWidget(preview)
        self.panel.inline_preview = preview
        self.assertTrue(self.dialog.isVisible())
        with patch.object(self.panel, 'open_manual_timeline') as manual:
            QTest.keyClick(self.panel.browser_open_button, Qt.Key.Key_Space)
            self.app.processEvents()
        manual.assert_called_once_with()
        preview.toggle_playback.assert_not_called()
        self.assertIs(self.panel._browser_dialog, self.dialog)

    def test_workspace_fullscreen_button_shortcut_and_escape_preserve_current_take(self):
        identity = self.click_take('short.mov')
        self.assertFalse(self.dialog.isFullScreen())
        QTest.mouseClick(self.panel.browser_fullscreen_button, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertTrue(self.dialog.isFullScreen())
        self.assertEqual(self.panel.browser_fullscreen_button.text(), '退出全屏')
        QTest.keyClick(self.canvas, Qt.Key.Key_Escape)
        self.app.processEvents()
        self.assertIs(self.panel._browser_dialog, self.dialog)
        self.assertFalse(self.dialog.isFullScreen())
        self.assertEqual(self.panel._candidate()['id'], identity)
        stroke = QKeySequence('Ctrl+Meta+F')[0]
        self.canvas.setFocus()
        QTest.keyClick(self.canvas, stroke.key(), stroke.keyboardModifiers())
        self.app.processEvents()
        self.assertTrue(self.dialog.isFullScreen())
        QTest.keyClick(self.canvas, Qt.Key.Key_Escape)
        self.app.processEvents()
        self.assertFalse(self.dialog.isFullScreen())
        self.assertEqual(self.panel._candidate()['id'], identity)

    def test_library_single_click_previews_whole_file_once_and_exposes_direct_range_editor(self):
        data = copy.deepcopy(self.data)
        path = add_take(data, 'day04_分镜头', 'silent', ())
        data['pending_pgm_intervals'] = [dict(id='silent-pending', session_id='day04_分镜头',
            session_name='day04_分镜头', media_path=path, start_seconds=0, end_seconds=20,
            manual_pending=True, reason_code='asr_unavailable', reason='无台词，人工定位', has_audio_sync=False)]
        data['timeline_nodes'] = build_timeline_data(data, whole_pgm=True)
        data['pgm_interval_nodes'] = build_pgm_intervals(data)
        self.panel._set_data(data)
        self.panel.pending_list.setCurrentRow(-1)
        self.app.processEvents()
        item = self.panel.pending_list.item(0)
        self.assertIsNotNone(item)
        self.assertFalse(item.flags() & Qt.ItemFlag.ItemIsUserCheckable,
                         '待匹配库只负责定位，不单独勾选导出')
        self.panel._load_inline.reset_mock()
        rect = self.panel.pending_list.visualItemRect(item)
        QTest.mouseClick(self.panel.pending_list.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        self.app.processEvents()
        self.panel._load_inline.assert_called_once_with(path, 0., 20.)
        self.assertEqual(self.panel._workspace_selection, 'library')
        self.assertEqual(self.panel.workspace_clip_label.text(), 'silent.mov')
        self.assertTrue(self.panel.workspace_inspector.isVisible())
        self.assertEqual(self.panel.coverage_apply_button.text(), '添加至轨道')
        self.assertEqual(self.panel.workspace_angles.currentIndex(), 1)
        self.assertFalse(hasattr(self.panel, 'workspace_export'))
        self.assertEqual(self.canvas.selected_candidate_id, '')
        with patch.object(self.panel, '_save_pending_coverage') as save:
            self.panel.coverage_range_input.setText('55-76')
            self.panel.apply_coverage_input()
            self.assertEqual(save.call_args.args[0]['media_path'], path)
            self.assertEqual(save.call_args.args[1], [f'l{n}' for n in range(54, 76)])
            self.assertEqual(save.call_args.args[2], 0.)

    def test_track_inspector_has_review_without_a_separate_export_choice(self):
        self.click_take('short.mov')
        self.assertEqual(self.panel._workspace_selection, 'track')
        self.assertEqual(self.panel.workspace_angles.currentIndex(), 0)
        self.assertEqual(self.panel.coverage_apply_button.text(), '应用区间')
        self.assertFalse(hasattr(self.panel, 'workspace_export'))
        self.assertTrue(self.panel.workspace_review_button.isEnabled())
        self.assertIn('绿色备选自动导出', self.panel.workspace_hint.text())
        self.click_take('continuous.mov')
        self.assertFalse(hasattr(self.panel, 'workspace_export'))
        self.assertEqual(self.panel._automatic_export_pairs(), {})

    def test_pending_search_reuses_snapshot_file_index_and_does_not_rebuild_identical_cards(self):
        data = copy.deepcopy(self.data)
        # A registered file with no transcript/candidate is genuinely unmatched.
        add_take(data, 'unmatched_day', 'unmatched', ())
        data['timeline_nodes'] = build_timeline_data(data, whole_pgm=True)
        data['pgm_interval_nodes'] = build_pgm_intervals(data)
        self.panel._set_data(data)
        self.assertEqual(self.panel.pending_list.count(), 1)
        card = self.panel.pending_list.item(0)
        self.assertEqual(card.text().splitlines()[0], 'unmatched.mov')
        self.assertEqual((self.panel._pending_rows[0]['start_seconds'],
                          self.panel._pending_rows[0]['end_seconds']), (0., 20.))
        cached = self.panel._pending_catalogue_cache
        with patch('pgm_library_view.prepare_pgm_inventory', side_effect=AssertionError('快照未改变不应重建文件索引')):
            with patch.object(self.panel, 'render_pending', wraps=self.panel.render_pending) as render:
                self.panel.pending_search.setText('un')
                self.panel.pending_search.setText('unmatched')
                self.panel.pending_search.setText('unmatched.mov')
                self.assertEqual(render.call_count, 0, '快速输入应等待防抖')
                QTest.qWait(180)
            self.assertIs(self.panel._pending_catalogue_cache, cached)
            self.assertIs(self.panel.pending_list.item(0), card)
            self.assertEqual(self.panel.pending_list.count(), 1)
            self.panel.pending_search.setText('does-not-exist')
            QTest.qWait(180)
            self.assertEqual(self.panel.pending_list.count(), 0)
            self.panel.pending_search.clear()
            QTest.qWait(180)
            self.assertEqual(self.panel.pending_list.count(), 1)

    def test_two_media_pools_and_tracks_are_disjoint_and_all_counts_share_one_inventory(self):
        data = copy.deepcopy(self.data)
        unmatched = add_take(data, 'new_day', 'unmatched', ())
        removed = add_take(data, 'removed_day', 'withdrawn', (70, 71))
        session = next(row for row in data['sessions'] if row['id'] == 'removed_day')
        session['config']['library_pgm_editor'] = {removed: dict(pool='removed', reviewed=False)}
        data['timeline_nodes'] = build_timeline_data(data, whole_pgm=True)
        data['pgm_interval_nodes'] = build_pgm_intervals(data)
        original = copy.deepcopy(data)
        self.panel._set_data(data)
        self.app.processEvents()
        rows, flags = self.panel._pgm_inventory()
        inventory = {self.panel._take_index().key(row) for row in rows}
        tracks = {(row.session_id, '/nas/'+row.session_id+'/'+row.filename) for row in self.canvas.index.takes}
        self.assertEqual(len(inventory), 7)
        self.assertEqual(len(tracks), 5)
        self.assertTrue(all(flags[key]['pool'] == 'timeline' for key in tracks))
        self.assertTrue(all(not row.reviewed for row in self.canvas.index.takes),
                        '自动 confirmed 状态不能变为已人工核实的绿色')
        pools = {}
        self.assertEqual([self.panel.pending_mode.itemData(n)
                          for n in range(self.panel.pending_mode.count())], ['unmatched', 'removed'])
        for mode, expected in [('unmatched', ('new_day', unmatched)), ('removed', ('removed_day', removed))]:
            self.panel.pending_mode.setCurrentIndex(self.panel.pending_mode.findData(mode))
            self.app.processEvents()
            pools[mode] = {self.panel._take_index().key(row) for row in self.panel._pending_rows}
            self.assertEqual(pools[mode], {expected})
            self.assertEqual(self.panel.pending_list.count(), 1)
            self.assertEqual(self.panel.pending_count.text(), '1 条PGM')
            self.assertIn('（1）', self.panel.pending_mode.currentText())
        self.assertFalse(tracks & pools['unmatched'])
        self.assertFalse(tracks & pools['removed'])
        self.assertFalse(pools['unmatched'] & pools['removed'])
        self.assertEqual(tracks | pools['unmatched'] | pools['removed'], inventory)
        expected = '轨道待核实 5 · 轨道已核实 0 · 未匹配台本 1 · 轨道移出 1'
        self.assertEqual(self.panel.pgm_status_text(), expected)
        self.assertIn(expected, self.panel.browser_summary.text())
        self.assertIn(expected, self.panel.script_count.text())
        self.assertIn('7 条完整PGM', self.panel.browser_summary.text())
        self.assertIn('7 条完整PGM', self.panel.script_count.text())
        self.panel.pending_search.setText('does-not-exist')
        QTest.qWait(180)
        self.assertEqual(self.panel.pending_list.count(), 0)
        self.assertEqual(self.panel.pending_count.text(), '显示 0 / 1 条PGM')
        self.assertEqual(self.panel.pgm_status_text(), expected)
        self.assertEqual(data, original)

    def ruler_drag(self, first, last, modifiers=Qt.KeyboardModifier.NoModifier):
        start, end = (self.canvas.line_rect(number).center().toPoint() for number in (first, last))
        QTest.mousePress(self.canvas.viewport(), Qt.MouseButton.LeftButton, modifiers, start)
        QTest.mouseMove(self.canvas.viewport(), end)
        QTest.mouseRelease(self.canvas.viewport(), Qt.MouseButton.LeftButton, modifiers, end)
        self.app.processEvents()

    def test_ruler_23_to_45_highlights_complete_left_script_without_pgm_autoselection(self):
        self.click_take('short.mov')
        self.canvas.set_line_width(12)
        self.canvas.horizontalScrollBar().setValue(0)
        self.panel._load_inline.reset_mock()
        removed = []
        self.panel.story_timeline.take_remove_requested.connect(removed.append)
        with patch.object(self.panel, '_save_story_coverage') as save:
            self.ruler_drag(22, 44)
            QTest.keyClick(self.canvas, Qt.Key.Key_Delete)
            save.assert_not_called()
        expected = {f'l{n}' for n in range(22, 45)}
        self.assertEqual(self.canvas.highlighted_line_ids, expected)
        self.assertEqual(self.panel.script_timeline._highlighted_line_ids, expected)
        self.assertEqual(self.panel.script_timeline.current_line_id(), 'l22')
        self.assertEqual(self.panel._selected_story_take_id, '')
        self.assertEqual(self.canvas.selected_candidate_id, '')
        self.assertFalse(self.panel.workspace_inspector.isVisible())
        self.panel._load_inline.assert_not_called()
        self.assertEqual(removed, [])

    def test_ruler_additive_range_survives_reference_state_and_coverage_refresh(self):
        self.canvas.select_line('l2')
        self.ruler_drag(2, 4)
        self.ruler_drag(9, 11, Qt.KeyboardModifier.MetaModifier)
        expected = {'l2','l3','l4','l9','l10','l11'}
        self.assertEqual(self.panel.script_timeline._highlighted_line_ids, expected)
        self.panel.apply_reference_state(self.panel.reference_state)
        self.assertEqual(self.canvas.highlighted_line_ids, expected)
        self.assertEqual(self.panel.script_timeline._highlighted_line_ids, expected)
        self.panel._refresh_coverage_axis()
        self.assertEqual(self.panel.script_timeline._highlighted_line_ids, expected)
        self.panel._set_data(copy.deepcopy(self.data))
        self.assertEqual(self.panel.script_timeline._highlighted_line_ids, expected)
        self.assertEqual(self.canvas.highlighted_line_ids, expected)
        self.assertEqual(self.canvas.selected_candidate_id, '')
        self.assertEqual(self.panel._selected_story_take_id, '')

    def test_control_and_command_ruler_add_selection_preserves_unselected_gap_in_left_script(self):
        self.canvas.set_line_width(12)
        for modifier in (Qt.KeyboardModifier.ControlModifier, Qt.KeyboardModifier.MetaModifier):
            with self.subTest(modifier=modifier):
                self.ruler_drag(2, 4)
                self.ruler_drag(9, 11, modifier)
                expected = {f'l{n}' for n in (2, 3, 4, 9, 10, 11)}
                self.assertEqual(self.canvas.highlighted_line_ids, expected)
                self.assertEqual(self.panel.script_timeline._highlighted_line_ids, expected)
                self.assertTrue(all(f'l{n}' not in self.panel.script_timeline._highlighted_line_ids for n in range(5, 9)))
                self.assertEqual(self.panel._selected_story_take_id, '')

    def test_baseline_only_export_is_enabled_without_alternatives_and_requests_only_baseline(self):
        self.assertEqual(self.panel.selected_clip_pairs, {})
        self.panel.set_workflow_stage('reference')
        self.app.processEvents()
        self.assertTrue(self.panel.export_baseline_button.isEnabled())
        self.assertTrue(self.panel.export_reference_button.isEnabled())
        self.assertEqual(self.panel.export_baseline_button.text(), '仅导出基准 XML')
        requests = []
        self.panel.reference_export_requested.connect(requests.append)
        with patch.object(self.panel, '_readiness_rows', return_value=[]):
            with patch('library_ui.QFileDialog.getExistingDirectory', return_value=self.home.name):
                QTest.mouseClick(self.panel.export_baseline_button, Qt.MouseButton.LeftButton)
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0]['include_baseline'])
        self.assertFalse(requests[0]['include_alternatives'])
        self.assertIsNone(requests[0]['resolved_pairs'])
        self.assertEqual(requests[0]['exported_take_pairs'], {})
        self.assertEqual(requests[0]['reference_session_id'], 'baseline')

    def test_combined_export_with_no_green_alternatives_needs_only_baseline_preparation(self):
        requests, required = [], []
        self.panel.reference_export_requested.connect(requests.append)
        def readiness(sessions=None):
            required.append(sessions)
            return []
        with patch.object(self.panel, '_readiness_rows', side_effect=readiness), \
             patch.object(self.panel, '_choose_baseline_candidate', side_effect=AssertionError('蓝色片段不参与对齐')), \
             patch('library_ui.QFileDialog.getExistingDirectory', return_value=self.home.name):
            self.panel.export_reference(include_alternatives=True, include_baseline=True)
        self.assertEqual([value for value in required if value is not None], [{'baseline'}])
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0]['include_alternatives'])
        self.assertEqual(requests[0]['exported_take_pairs'], {})
        self.assertIsNone(requests[0]['resolved_pairs'])
        self.assertEqual(requests[0]['export_snapshot'], self.panel._take_index().export_signature('baseline'))

    def test_baseline_only_export_does_not_consult_selected_alternative_anchors(self):
        self.panel.selected_clip_pairs = {'an-unresolved-alt': None}
        requests = []
        self.panel.reference_export_requested.connect(requests.append)
        with patch.object(self.panel, '_readiness_rows', return_value=[]):
            with patch.object(self.panel, '_choose_baseline_candidate', side_effect=AssertionError('仅基准不应核对备选')):
                with patch('library_ui.QFileDialog.getExistingDirectory', return_value=self.home.name):
                    self.panel.export_reference(include_alternatives=False, include_baseline=True)
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0]['include_baseline'])
        self.assertFalse(requests[0]['include_alternatives'])

    def test_return_cancels_queued_preview_io_and_late_loaded_media_cannot_start_hidden_audio(self):
        from library_ui import InlinePreview
        from PyQt6.QtMultimedia import QMediaPlayer
        LibraryPanel._load_inline(self.panel, '/fake/delayed-preview.mov', 0., 20.)
        self.assertTrue(self.panel._inline_timer.isActive())
        generation = self.panel._inline_io.state['generation']
        preview = InlinePreview(self.panel.inline_host)
        preview.path = '/fake/delayed-preview.mov'
        self.panel.inline_preview = preview
        self.panel.inline_layout.addWidget(preview)
        self.panel.close_browser_workspace()
        self.app.processEvents()
        self.assertFalse(self.panel._inline_timer.isActive())
        self.assertGreater(self.panel._inline_io.state['generation'], generation)
        self.assertTrue(preview._suspended)
        with patch.object(preview.player, 'play') as play:
            preview.ready(QMediaPlayer.MediaStatus.LoadedMedia)
            play.assert_not_called()
        self.panel.open_browser_workspace()
        self.app.processEvents()
        self.assertFalse(preview._suspended)

    def test_audio_settings_are_collapsed_by_default_and_do_not_add_duplicate_play_controls(self):
        from library_ui import InlinePreview
        from PyQt6.QtWidgets import QPushButton
        preview = InlinePreview(self.panel.inline_host)
        self.panel.inline_preview = preview
        self.panel.inline_layout.addWidget(preview)
        preview.show()
        self.app.processEvents()
        self.assertFalse(preview.preview_audio.controls.isVisible())
        self.assertFalse(preview.audio_settings.isChecked())
        labels = [button.text() for button in preview.findChildren(QPushButton)]
        self.assertNotIn('播放', labels)
        self.assertNotIn('暂停', labels)
        QTest.mouseClick(preview.audio_settings, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertTrue(preview.preview_audio.controls.isVisible())
        QTest.mouseClick(preview.audio_settings, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertFalse(preview.preview_audio.controls.isVisible())

    def test_parent_close_stops_both_thumbnail_loaders_and_pending_search_timer(self):
        self.panel.pending_search.setText('queued-search')
        self.assertTrue(self.panel._pending_search_timer.isActive())
        self.panel.close()
        self.app.processEvents()
        self.assertFalse(self.panel._pending_search_timer.isActive())
        self.assertTrue(self.panel.pending_thumbnails.state['closed'])
        self.assertTrue(self.panel.match_thumbnails.state['closed'])

    def test_return_pauses_preview_exits_fullscreen_and_keeps_selection_for_reopening(self):
        from library_ui import InlinePreview
        identity = self.click_take('short.mov')
        preview = InlinePreview(self.panel.inline_host)
        self.panel.inline_layout.addWidget(preview)
        self.panel.inline_preview = preview
        preview.path = '/fake/preview.mov'
        preview.video.setFullScreen(True)
        self.app.processEvents()
        self.assertTrue(preview.video.isFullScreen())
        with patch.object(preview.player, 'pause') as pause:
            self.panel.close_browser_workspace()
            self.app.processEvents()
            pause.assert_called_once_with()
        self.assertFalse(preview.video.isFullScreen())
        self.assertTrue(preview._suspended)
        self.assertEqual(self.panel._candidate()['id'], identity)
        self.assertFalse(self.panel._closed)
        self.panel.open_browser_workspace()
        self.app.processEvents()
        self.assertIs(self.panel.inline_preview, preview)
        self.assertFalse(preview._suspended)
        self.assertEqual(self.panel._candidate()['id'], identity)

    def test_parent_close_closes_browser_without_orphaned_window(self):
        self.panel.close()
        self.app.processEvents()
        self.assertTrue(self.panel._closed)
        self.assertIsNone(self.panel._browser_dialog)
        self.assertFalse(self.panel.catalogue_page.isVisible())

    def test_escape_returns_to_main_launcher_without_clearing_current_take(self):
        identity = self.click_take('short.mov')
        self.canvas.setFocus()
        QTest.keyClick(self.canvas, Qt.Key.Key_Escape)
        self.app.processEvents()
        self.assertIsNone(self.panel._browser_dialog)
        self.assertEqual(self.panel._candidate()['id'], identity)
        self.assertEqual(self.panel.content_stack.currentWidget(), self.panel.catalogue_launch_page)

    def test_native_save_signal_delegates_to_host_project_window(self):
        from pgm_app import Window
        with patch('library_bridge.read_settings', return_value={}):
            host = Window()
        try:
            host.show()
            panel = host.library_panel
            panel.set_workflow_stage('script')
            panel.open_browser_workspace()
            self.app.processEvents()
            with patch.object(host, 'save_named_project') as save:
                QTest.mouseClick(panel.browser_save_button, Qt.MouseButton.LeftButton)
                save.assert_called_once_with()
            self.assertFalse(panel._closed)
        finally:
            host.prompt_project = False
            host.close()
            self.app.processEvents()


class PersistedCoverageWorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from test_pgm_coverage_edit import PgmCoverageEditTests
        self.fixture = PgmCoverageEditTests()
        self.fixture.setUp()
        self.home = tempfile.TemporaryDirectory()
        self.settings_patch = patch('library_bridge.data_dir', return_value=Path(self.home.name))
        self.settings_patch.start()
        with patch('library_bridge.read_settings', return_value={}):
            self.panel = LibraryPanel(lambda: {}, lambda _: None, auto_restore=False)
        self.panel.client = self.fixture.workflow.client
        self.panel.production.blockSignals(True)
        self.panel.production.addItem('范围修正', self.fixture.production)
        self.panel.production.blockSignals(False)
        self.panel.ensure_workflow_context = lambda *_args: (self.fixture.workflow.client, self.fixture.production)
        self.panel._run = lambda title, operation, success: success(operation(lambda *args: None, lambda: False))
        self.panel._load_inline = Mock()
        self.panel._set_data(self.fixture.workflow.read_all())
        self.panel.set_workflow_stage('script')
        self.panel.resize(1000, 648)
        self.panel.show()
        self.app.processEvents()
        self.panel.open_browser_workspace()
        self.panel._browser_dialog.showNormal()
        self.panel._browser_dialog.resize(1000, 648)
        self.app.processEvents()
        QTest.qWait(30)
        self.panel._story_take_selected(self.fixture.initial['candidate']['id'], replay=False)

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        self.app.processEvents()
        self.settings_patch.stop()
        self.home.cleanup()
        self.fixture.tearDown()

    def test_ui_range_edit_persists_refreshes_track_and_retains_original_source_anchor(self):
        original_source = self.fixture.initial['transcript']['segments'][0]
        original_config = self.fixture.store.get_session(self.fixture.session['id'])['config']
        original_candidate = self.panel._candidate()['id']
        self.panel.coverage_range_input.setText('40-80')
        self.panel.apply_coverage_input()
        self.app.processEvents()
        clip = self.panel._candidate()
        self.assertNotEqual(clip['id'], original_candidate)
        self.assertEqual(clip['covered_line_ids'], [row['id'] for row in self.fixture.lines[39:80]])
        self.assertEqual(self.panel.coverage_range_input.text(), '40-80')
        self.assertEqual(self.panel.script_timeline._highlighted_line_ids, set(clip['covered_line_ids']))
        take = self.panel.story_timeline.index.by_candidate[clip['id']]
        self.assertEqual((take.first, take.last), (39, 79))
        self.assertEqual((clip['start_seconds'], clip['end_seconds'], clip['dialogue_start_seconds']), (0, 100, 7.1))
        current_config = self.fixture.store.get_session(self.fixture.session['id'])['config']
        self.assertEqual(source_config(current_config), source_config(original_config))
        self.assertTrue(current_config['library_pgm_editor'][self.fixture.path]['reviewed'])
        source = next(row for row in self.fixture.store.list_transcripts(self.fixture.production)
                      if row['id'] == original_source['id'])
        self.assertEqual((source['start_seconds'], source['end_seconds']), (7.1, 100))
        self.assertEqual(self.panel.pending_list.count(), 0)

    def test_handle_edit_persists_to_same_backend_as_typed_range(self):
        clip = self.panel._candidate()
        self.panel._story_coverage_edited(clip['id'], self.fixture.lines[39]['id'], self.fixture.lines[60]['id'])
        self.assertEqual(self.panel.coverage_range_input.text(), '40-61')
        self.assertEqual(self.panel._candidate()['covered_line_ids'], [row['id'] for row in self.fixture.lines[39:61]])
        self.assertEqual(self.panel._candidate()['dialogue_start_seconds'], 7.1)

    def test_range_enter_saves_inside_browser_and_does_not_activate_dialog_default_button(self):
        dialog = self.panel._browser_dialog
        with patch.object(self.panel, 'refresh') as refresh:
            self.panel.coverage_range_input.setText('40-80')
            self.panel.coverage_range_input.setFocus()
            QTest.keyClick(self.panel.coverage_range_input, Qt.Key.Key_Return)
            self.app.processEvents()
            refresh.assert_not_called()
        self.assertIs(self.panel._browser_dialog, dialog)
        self.assertTrue(dialog.isVisible())
        self.assertEqual(self.panel.coverage_range_input.text(), '40-80')
        self.assertEqual(self.panel._candidate()['dialogue_start_seconds'], 7.1)

    def test_range_save_installs_upgraded_client_for_followup_edit_and_removal(self):
        from test_pgm_coverage_edit import LocalClient
        replacement = LocalClient(self.fixture.store)
        replacement.url = 'http://127.0.0.1:5678'
        replacement.token = 'qa-service-token-with-at-least-24-characters'
        self.panel.ensure_workflow_context = lambda *_args: (replacement, self.fixture.production)
        self.panel.coverage_range_input.setText('40-80')
        self.panel.apply_coverage_input()
        self.assertIs(self.panel.client, replacement)
        self.assertEqual(self.panel.url.text(), '')
        self.assertEqual(self.panel.token.text(), '')
        self.assertEqual(self.panel.coverage_range_input.text(), '40-80')
        self.panel.coverage_range_input.setText('41-80')
        self.panel.apply_coverage_input()
        self.assertIs(self.panel.client, replacement)
        self.assertEqual(self.panel.coverage_range_input.text(), '41-80')
        self.panel.remove_pgm_match()
        self.assertIs(self.panel.client, replacement)
        self.assertEqual(self.panel.story_timeline.index.takes, [])

    def test_pool_delete_button_hides_removed_file_and_refreshes_counts(self):
        self.panel.remove_pgm_match()
        before = self.fixture.store.list_transcripts(self.fixture.production)
        self.assertTrue(self.panel.pool_delete_button.isEnabled())
        QTest.mouseClick(self.panel.pool_delete_button, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertEqual(self.panel.pending_list.count(), 0)
        self.assertEqual(self.panel.pending_count.text(), '0 条PGM')
        self.assertFalse(self.panel.pool_delete_button.isEnabled())
        self.assertIn('轨道移出 0', self.panel.pgm_status_text())
        self.assertEqual(self.fixture.store.list_transcripts(self.fixture.production), before)
        self.panel._set_data(self.fixture.workflow.read_all())
        self.assertEqual(self.panel.pending_list.count(), 0)

    def test_unmatched_pool_backspace_hides_registered_file_before_asr(self):
        path = '/nas/extra/waste.mov'
        session = self.fixture.store.save_session(self.fixture.production, 'day02_分镜头',
            dict(pgm=path, fps=25, library_media=[dict(kind='pgm', path=path, fps=25, frames=250)]))
        self.panel._set_data(self.fixture.workflow.read_all())
        self.panel.pending_mode.setCurrentIndex(self.panel.pending_mode.findData('unmatched'))
        self.panel.pending_list.setCurrentRow(0)
        self.assertEqual(self.panel._pending()['media_path'], path)
        self.panel._browser_dialog.activateWindow()
        self.panel.pending_list.setFocus()
        self.app.processEvents()
        QTest.keyClick(self.panel.pending_list, Qt.Key.Key_Backspace)
        self.app.processEvents()
        self.assertEqual(self.panel.pending_list.count(), 0)
        self.assertTrue(self.fixture.store.get_session(session['id'])['config']
                        ['library_pgm_editor'][path]['hidden_in_pool'])
        self.assertTrue(self.panel.story_timeline.index.takes, '媒体池删除不影响已有轨道')

    def test_pool_delete_shortcuts_hide_file_but_text_backspace_only_edits_search(self):
        self.panel.remove_pgm_match()
        self.panel.pending_search.setText('PIX')
        self.panel.pending_search.setFocus()
        QTest.keyClick(self.panel.pending_search, Qt.Key.Key_Backspace)
        self.app.processEvents()
        self.assertEqual(self.panel.pending_search.text(), 'PI')
        self.assertFalse(self.fixture.store.get_session(self.fixture.session['id'])['config']
                         ['library_pgm_editor'][self.fixture.path].get('hidden_in_pool'))
        self.panel.pending_search.clear()
        self.panel.render_pending()
        self.panel.pending_list.setCurrentRow(0)
        self.panel._browser_dialog.activateWindow()
        self.panel.pending_list.setFocus()
        self.app.processEvents()
        QTest.keyClick(self.panel.pending_list, Qt.Key.Key_Delete)
        self.app.processEvents()
        self.assertEqual(self.panel.pending_list.count(), 0)
        self.assertTrue(self.fixture.store.get_session(self.fixture.session['id'])['config']
                        ['library_pgm_editor'][self.fixture.path]['hidden_in_pool'])

    def test_remove_wrong_match_refreshes_tracks_script_status_and_pending_queue(self):
        path = self.panel._candidate()['media_path']
        self.assertTrue(self.panel.story_timeline.index.takes)
        self.panel.remove_pgm_match()
        self.app.processEvents()
        self.assertEqual(self.panel.story_timeline.index.takes, [])
        self.assertEqual(self.panel.story_timeline.canvas.highlighted_line_ids, set())
        self.assertEqual(self.panel.script_timeline._highlighted_line_ids, set())
        self.assertEqual(self.panel.story_timeline.canvas.selected_candidate_id, '')
        self.assertIn('未匹配台本 0', self.panel.pgm_status_text())
        self.assertIn('轨道移出 1', self.panel.pgm_status_text())
        self.assertEqual(self.panel.pending_mode.currentData(), 'removed')
        self.assertEqual(self.panel.main_pages.currentIndex(), 1)
        self.assertEqual(self.panel._pending()['media_path'], path)
        self.assertEqual(self.panel.pending_list.count(), 1)
        current_config = self.fixture.store.get_session(self.fixture.session['id'])['config']
        self.assertEqual(source_config(current_config), self.fixture.config)
        self.assertEqual(current_config['library_pgm_editor'][self.fixture.path]['pool'], 'removed')

    def test_delete_key_withdraws_exact_track_and_export_choice_to_library_without_deleting_sources(self):
        selected = self.fixture.initial['candidate']['id']
        baseline_session, _, anchor = self.fixture.baseline()
        self.fixture.choose(baseline_session['id'], {selected: anchor['id']})
        self.panel._set_data(self.fixture.workflow.read_all())
        self.panel._story_take_selected(selected, replay=False)
        canvas = self.panel.story_timeline.canvas
        self.assertFalse(hasattr(self.panel, 'workspace_export'))
        original_transcripts = self.fixture.store.list_transcripts(self.fixture.production)
        original_session = self.fixture.store.get_session(self.fixture.session['id'])
        canvas.setFocus()
        QTest.keyClick(canvas, Qt.Key.Key_Delete)
        self.app.processEvents()
        self.assertNotIn(selected, canvas.index.by_candidate)
        self.assertEqual(self.panel.selected_clip_pairs, {})
        self.assertEqual(canvas.selected_candidate_id, '')
        self.assertEqual(self.panel._workspace_selection, 'library')
        self.assertEqual(self.panel._pending()['media_path'], self.fixture.path)
        self.assertEqual(self.panel.pending_list.count(), 1)
        self.assertEqual(self.panel.workspace_clip_label.text(), 'PIX_001_046.mov')
        self.assertFalse(hasattr(self.panel, 'workspace_export'))
        self.assertEqual(self.panel.coverage_apply_button.text(), '添加至轨道')
        self.assertEqual(self.fixture.store.list_transcripts(self.fixture.production), original_transcripts)
        current_session = self.fixture.store.get_session(self.fixture.session['id'])
        self.assertEqual(source_config(current_session['config']), source_config(original_session['config']))
        self.assertEqual(current_session['id'], original_session['id'])
        self.assertEqual(current_session['name'], original_session['name'])
        self.assertEqual(current_session['config']['library_pgm_editor'][self.fixture.path]['pool'], 'removed')
        self.assertFalse(current_session['config']['library_pgm_editor'][self.fixture.path]['reviewed'])
        self.assertTrue(any(row.reference for row in canvas.index.takes), '删除备选不能删除基准轨道')
        self.assertEqual(self.panel.pending_mode.currentData(), 'removed')
        self.panel.pending_mode.setCurrentIndex(self.panel.pending_mode.findData('unmatched'))
        self.app.processEvents()
        self.assertEqual(self.panel.pending_list.count(), 0, '轨道移出的PGM不能重复出现在未匹配池')
        self.panel.pending_mode.setCurrentIndex(self.panel.pending_mode.findData('removed'))
        self.app.processEvents()
        self.assertEqual(self.panel.pending_list.count(), 1)
        self.assertEqual(self.panel._pending_rows[0]['media_path'], self.fixture.path)

    def test_human_review_turns_exact_track_green_preserving_sources_and_alternative_selection(self):
        selected = self.fixture.initial['candidate']['id']
        baseline_session, _, anchor = self.fixture.baseline()
        self.fixture.choose(baseline_session['id'], {selected: anchor['id']})
        self.fixture.workflow.mark_pgm_reviewed(self.fixture.session['id'], self.fixture.path,
            expected_match_version=self.fixture.version(), reviewed=False)
        self.panel._set_data(self.fixture.workflow.read_all())
        self.panel._story_take_selected(selected, replay=False)
        self.app.processEvents()
        canvas = self.panel.story_timeline.canvas
        self.assertFalse(canvas.index.by_candidate[selected].reviewed)
        self.assertEqual(self.panel._automatic_export_pairs(), {})
        self.assertEqual(canvas.take_colors(canvas.index.by_candidate[selected], True)[0], '#2875ba')
        self.assertTrue(self.panel.workspace_review_button.isEnabled())
        self.assertFalse(hasattr(self.panel, 'workspace_export'))
        sources = copy.deepcopy(self.fixture.store.list_transcripts(self.fixture.production))
        candidates = copy.deepcopy(self.fixture.store.list_candidates(self.fixture.production))
        config = copy.deepcopy(self.fixture.store.get_session(self.fixture.session['id'])['config'])
        reference = copy.deepcopy(self.fixture.store.get_reference_settings(self.fixture.production))
        self.panel._load_inline.reset_mock()
        original_index = self.panel._take_index()
        original_script_item = self.panel.script_timeline._items[self.panel.script_timeline.current_line_id()]
        with patch.object(self.panel, '_set_data', side_effect=AssertionError('review-only must reuse snapshot')), \
             patch('pgm_library_view.prepare_pgm_inventory', side_effect=AssertionError('review-only must reuse inventory')):
            QTest.mouseClick(self.panel.workspace_review_button, Qt.MouseButton.LeftButton)
        self.assertIs(self.panel._take_index(), original_index)
        self.assertIs(self.panel.script_timeline._items[self.panel.script_timeline.current_line_id()], original_script_item)
        self.app.processEvents()
        self.assertTrue(canvas.index.by_candidate[selected].reviewed)
        self.assertEqual(canvas.take_colors(canvas.index.by_candidate[selected], True)[0], '#26754e')
        self.assertEqual(canvas.selected_candidate_id, selected)
        self.assertEqual(self.panel.workspace_review_button.text(), '区间已核实')
        self.assertFalse(self.panel.workspace_review_button.isEnabled())
        self.assertFalse(hasattr(self.panel, 'workspace_export'))
        self.assertEqual(self.panel.selected_clip_pairs, {selected: anchor['id']})
        self.assertEqual(self.panel._automatic_export_pairs(), {selected: anchor['id']})
        self.assertIn('尚未保存手动编排', self.panel.reference_label.text())
        self.assertEqual(self.fixture.store.get_reference_settings(self.fixture.production), reference)
        self.assertEqual(self.fixture.store.list_transcripts(self.fixture.production), sources)
        self.assertEqual(self.fixture.store.list_candidates(self.fixture.production), candidates)
        current = self.fixture.store.get_session(self.fixture.session['id'])['config']
        self.assertEqual(source_config(current), source_config(config))
        self.assertTrue(current['library_pgm_editor'][self.fixture.path]['reviewed'])
        self.assertEqual(next(row['status'] for row in self.panel._pgm_status_rows
                              if row['media_path'] == self.fixture.path), 'reviewed')

    def test_library_range_enter_creates_complete_track_directly_without_manual_dialog(self):
        self.panel.remove_pgm_match()
        self.app.processEvents()
        self.assertEqual(self.panel._workspace_selection, 'library')
        self.assertEqual(self.panel.pending_list.count(), 1)

        class Preview(QWidget):
            def __init__(self, parent, path):
                super().__init__(parent)
                self.path = path
                self.video = self
                self.player = Mock()
                self.player.position.return_value = 7100
                self.clear = Mock()
        preview = Preview(self.panel.inline_host, self.fixture.path)
        self.panel.inline_preview = preview
        self.panel.inline_layout.addWidget(preview)
        self.panel.coverage_range_input.setText('40-80')
        self.panel.coverage_range_input.setFocus()
        with patch('manual_script_range.ManualPgmRangeDialog.exec') as dialog:
            QTest.keyClick(self.panel.coverage_range_input, Qt.Key.Key_Return)
            self.app.processEvents()
            dialog.assert_not_called()
        self.assertEqual(self.panel._workspace_selection, 'track')
        self.assertEqual(self.panel.pending_list.count(), 0)
        clip = self.panel._candidate()
        self.assertEqual(clip['media_path'], self.fixture.path)
        self.assertEqual((clip['start_seconds'], clip['end_seconds'], clip['dialogue_start_seconds']), (0., 100., 7.1))
        self.assertEqual(self.panel.coverage_range_input.text(), '40-80')
        self.assertEqual(self.panel.script_timeline._highlighted_line_ids,
                         {row['id'] for row in self.fixture.lines[39:80]})
        self.assertEqual(self.panel.workspace_clip_label.text(), 'PIX_001_046.mov')
        self.assertEqual(self.panel.coverage_apply_button.text(), '应用区间')
        self.assertFalse(hasattr(self.panel, 'workspace_export'))
        current_config = self.fixture.store.get_session(self.fixture.session['id'])['config']
        self.assertEqual(source_config(current_config), self.fixture.config)
        self.assertEqual(current_config['library_pgm_editor'][self.fixture.path]['pool'], 'timeline')
        self.assertTrue(current_config['library_pgm_editor'][self.fixture.path]['reviewed'])

    def test_delete_resolves_requested_id_independently_from_legacy_hidden_list_selection(self):
        selected = self.fixture.initial['candidate']['id']
        other_session, _, other_candidate = self.fixture.baseline()
        self.panel._set_data(self.fixture.workflow.read_all())
        self.panel._story_take_selected(other_candidate['id'], replay=False)
        self.assertEqual(self.panel._candidate()['id'], other_candidate['id'])
        self.panel.remove_pgm_match(candidate_id=selected)
        self.assertNotIn(selected, self.panel.story_timeline.index.by_candidate)
        self.assertIn(other_candidate['id'], self.panel.story_timeline.index.by_candidate)
        self.assertEqual(self.panel._pending()['session_id'], self.fixture.session['id'])
        self.assertNotEqual(self.panel._pending()['session_id'], other_session['id'])

    def test_workspace_range_uses_track_identity_even_when_legacy_candidate_resolver_points_elsewhere(self):
        selected = self.fixture.initial['candidate']['id']
        _, _, other_candidate = self.fixture.baseline()
        self.panel._set_data(self.fixture.workflow.read_all())
        self.panel._story_take_selected(selected, replay=False)
        unrelated = self.panel._story_clip_by_id[other_candidate['id']]
        self.panel.coverage_range_input.setText('40-80')
        with patch.object(self.panel, '_candidate', return_value=unrelated):
            self.panel.apply_coverage_input()
        stored = self.fixture.store.list_transcripts(self.fixture.production)
        original = next(row for row in stored
                        if row['id'] == self.fixture.initial['transcript']['segments'][0]['id'])
        self.assertEqual(original['data']['manual_script_line_ids'],
                         [row['id'] for row in self.fixture.lines[39:80]])
        baseline_source = next(row for row in stored if row['id'] == other_candidate['transcript_id'])
        self.assertEqual(baseline_source['data']['manual_script_line_ids'], [self.fixture.lines[54]['id']])
        self.assertEqual(self.panel.workspace_clip_label.text(), 'PIX_001_046.mov')

    def test_failed_handle_edit_rolls_back_script_and_canvas_coverage(self):
        clip = self.panel._candidate()
        identity = clip['id']
        before = list(clip['covered_line_ids'])
        row = next(row for row in self.fixture.store.list_candidates(self.fixture.production) if row['id'] == identity)
        self.fixture.store.update_candidate(identity, status='confirmed', note='另一个场记刚修改', expected_revision=row['revision'])
        canvas = self.panel.story_timeline.canvas
        canvas.set_line_width(16)
        canvas.horizontalScrollBar().setValue(35 * 16)
        canvas.select_take(identity)
        self.app.processEvents()
        rect = canvas.take_rect(identity)
        first = QPoint(round(rect.left() + 3), round(rect.center().y()))
        last = QPoint(round(canvas.line_rect(39).center().x()), first.y())
        with patch('library_ui.QMessageBox.warning') as warning:
            QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, pos=first)
            QTest.mouseMove(canvas.viewport(), last)
            QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, pos=last)
            self.app.processEvents()
            warning.assert_called_once()
        self.assertEqual(self.panel._candidate()['covered_line_ids'], before)
        self.assertEqual(canvas.highlighted_line_ids, set(before))
        self.assertEqual(self.panel.script_timeline._highlighted_line_ids, set(before))
        self.assertEqual(self.panel.coverage_range_input.text(), '55-76')

    def test_concurrent_edit_rejected_instead_of_overwriting_fresh_server_association(self):
        from library_store import LibraryError
        clip = self.panel._candidate()
        current = self.fixture.store.list_candidates(self.fixture.production)
        row = next(row for row in current if row['id'] == clip['id'])
        self.fixture.store.update_candidate(row['id'], status='confirmed', note='另一个场记刚修改', expected_revision=row['revision'])
        self.panel.coverage_range_input.setText('40-80')
        with self.assertRaises(LibraryError) as caught:
            self.panel.apply_coverage_input()
        self.assertEqual(caught.exception.code, 'conflict')
        self.assertEqual(self.panel._candidate()['covered_line_ids'], [row['id'] for row in self.fixture.lines[54:76]])


if __name__ == '__main__':
    unittest.main()
