"""Independent overlapping PGM selection, range edits, and large-catalogue QA."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import copy
import time
import unittest

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QColor, QKeyEvent, QWheelEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLineEdit

from horizontal_story import HorizontalStoryTimeline, StoryIndex, StoryTake


def scripts(count=20):
    return [dict(id=f'l{n}', script_line_id=f'l{n}', script_id='s', order=n,
                 speaker='演员', text=f'这是第 {n + 1} 行的完整台词。', scene='第一幕') for n in range(count)]


def take(name, first, last, session='day02_分镜头', **extra):
    clip = dict(id=name, candidate_id=name, session_id=session, session_name=session,
                media_path=f'/source/{name}.mov', covered_line_ids=[f'l{n}' for n in range(first, last + 1)],
                start_seconds=0., end_seconds=120., status='confirmed', pgm_match_status='matched')
    clip.update(extra)
    return dict(id='interval-' + name, clips=[clip], covered_line_ids=[f'l{n}' for n in range(first, last + 1)])


class StoryIndexTests(unittest.TestCase):
    def test_review_flag_is_explicit_and_independent_of_automatic_confirmation(self):
        index = StoryIndex(scripts(), [take('auto', 0, 1), take('manual', 2, 3, editor_reviewed=True),
                                      take('string', 4, 5, editor_reviewed='true')])
        self.assertFalse(index.by_candidate['auto'].reviewed)
        self.assertTrue(index.by_candidate['manual'].reviewed)
        self.assertFalse(index.by_candidate['string'].reviewed)
        self.assertFalse(StoryTake('id', 'interval', 'session', '场次', 'a.mov', 0, 1, 'confirmed').reviewed)

    def test_each_file_uses_own_range_even_in_grouped_intervals(self):
        group = take('long', 1, 15)
        group['clips'].extend(take('short', 4, 6)['clips'])
        index = StoryIndex(scripts(), [group])
        self.assertEqual([(row.candidate_id, row.first, row.last) for row in index.takes], [('long', 1, 15), ('short', 4, 6)])
        lanes, layout = index.lane_layout(96)
        self.assertEqual(len(lanes), 2)
        self.assertNotEqual(layout['long'][0], layout['short'][0])

    def test_identical_ranges_get_distinct_selectable_lanes(self):
        index = StoryIndex(scripts(), [take('a', 3, 5), take('b', 3, 5), take('c', 3, 5)])
        lanes, layout = index.lane_layout(96)
        self.assertEqual(len(lanes), 3)
        self.assertEqual({row[0] for row in layout.values()}, {0, 1, 2})

    def test_nonoverlapping_intervals_reuse_lane(self):
        index = StoryIndex(scripts(), [take('a', 1, 2), take('b', 3, 4), take('c', 7, 8)])
        self.assertEqual(len(index.lane_layout(96)[0]), 1)

    def test_source_objects_and_metadata_never_mutate(self):
        rows, nodes = scripts(), [take('a', 1, 5, timecode='22:04:08:13', metadata=dict(sound_roll='A10'))]
        original = copy.deepcopy((rows, nodes))
        index = StoryIndex(rows, nodes)
        index.lane_layout(12)
        self.assertEqual((rows, nodes), original)

    def test_invalid_or_rejected_files_are_not_rendered_and_files_are_deduplicated(self):
        nodes = [take('a', 1, 3), take('a', 1, 3), take('b', 3, 4, status='rejected'), take('c', 30, 35)]
        self.assertEqual([row.candidate_id for row in StoryIndex(scripts(), nodes).takes], ['a'])

    def test_script_families_and_order_match_existing_script_browser(self):
        rows = [dict(id='s1b', script_id='s1', order=3), dict(id='s2', script_id='s2', order=0),
                dict(id='s1a', script_id='s1', order=0)]
        self.assertEqual(StoryIndex(rows).line_ids, ['s1a', 's1b', 's2'])

    def test_baseline_is_distinct_but_does_not_hide_alternatives(self):
        index = StoryIndex(scripts(), [take('base', 0, 19, session='base'), take('backup', 2, 3)], 'base')
        self.assertTrue(index.by_candidate['base'].reference)
        self.assertFalse(index.by_candidate['backup'].reference)

    def test_minimum_width_allocates_lanes_to_keep_tiny_blocks_individually_clickable(self):
        index = StoryIndex(scripts(), [take(f't{n}', n, n) for n in range(20)])
        lanes, layout = index.lane_layout(.1)
        self.assertGreater(len(lanes), 1)
        self.assertTrue(all(end - first >= 18 for _, first, end in layout.values()))
        for lane in lanes:
            self.assertTrue(all(a[1] <= b[0] for a, b in zip(lane, lane[1:])))

    def test_ten_thousand_lines_and_takes_build_and_layout_without_widget_growth(self):
        rows, nodes = scripts(10000), [take(f't{n}', n, n) for n in range(10000)]
        before = time.perf_counter()
        index = StoryIndex(rows, nodes)
        lanes, layout = index.lane_layout(96)
        self.assertEqual(len(index.takes), 10000)
        self.assertEqual(len(lanes), 1)
        self.assertEqual(len(layout), 10000)
        self.assertLess(time.perf_counter() - before, 2.)


class HorizontalTimelineTests(unittest.TestCase):
    def test_compact_ruler_frees_track_height_and_keeps_range_selection(self):
        self.view.select_take('b')
        before = self.canvas._script_top()
        identities = set(self.canvas.highlighted_line_ids)
        self.view.set_compact_ruler(True)
        self.assertGreater(self.canvas._script_top(), before)
        self.assertLessEqual(self.canvas._script_height(), 40)
        self.assertEqual(self.canvas.highlighted_line_ids, identities)
        self.assertEqual(self.canvas.selected_candidate_id, 'b')
        self.view.grab()  # Exercise the compact paint and selection paths.
        rect = self.canvas.take_rect('b')
        self.assertEqual(self.canvas.hit_take(rect.center()).candidate_id, 'b')

    def test_compact_ruler_line_click_still_positions_full_script(self):
        self.view.set_compact_ruler(True)
        self.view.select_line('l10')
        events=[]
        self.view.line_selected.connect(events.append)
        rect=self.canvas.line_rect(10)
        QTest.mouseClick(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=rect.center().toPoint())
        self.assertEqual(events, ['l10'])
        self.view.set_compact_ruler(False)
        self.assertEqual(self.canvas.selected_line_id, 'l10')

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.view = HorizontalStoryTimeline()
        self.view.resize(1100, 430)
        self.view.set_nodes(scripts(), [take('a', 1, 5), take('b', 3, 4), take('c', 12, 12)])
        self.view.show()
        self.app.processEvents()
        self.canvas = self.view.canvas

    def tearDown(self):
        self.view.close()
        self.view.deleteLater()
        self.app.processEvents()

    def test_overlap_click_selects_exact_file_and_highlights_its_complete_range(self):
        self.canvas.set_line_width(40)
        selections, highlights = [], []
        self.view.take_selected.connect(selections.append)
        self.view.coverage_highlighted.connect(highlights.append)
        for identity, expected in [('a', ['l1', 'l2', 'l3', 'l4', 'l5']), ('b', ['l3', 'l4'])]:
            point = self.canvas.take_rect(identity).center().toPoint()
            QTest.mouseClick(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=point)
            self.assertEqual(selections[-1], identity)
            self.assertEqual(highlights[-1], expected)
            self.assertEqual(self.canvas.highlighted_line_ids, set(expected))

    def test_selected_take_refresh_keeps_identity_and_full_range(self):
        self.view.select_take('b')
        nodes = [take('a', 1, 5), take('b', 3, 6)]
        self.view.set_nodes(scripts(), nodes)
        self.assertEqual(self.canvas.selected_candidate_id, 'b')
        self.assertEqual(self.canvas.highlighted_line_ids, {'l3', 'l4', 'l5', 'l6'})

    def test_line_only_selection_clears_previous_pgm_and_full_range(self):
        self.view.select_take('b')
        self.view.select_line('l10')
        self.assertEqual(self.canvas.selected_candidate_id, '')
        self.assertEqual(self.canvas.highlighted_line_ids, set())
        self.assertEqual(self.canvas.selected_line_id, 'l10')

    def test_clear_selection_does_not_change_dataset_or_horizontal_view(self):
        self.view.select_take('b')
        before = self.canvas.index
        scroll = self.canvas.horizontalScrollBar().value()
        events = []
        self.view.coverage_highlighted.connect(events.append)
        self.view.clear_selection(emit=True)
        self.assertIs(self.canvas.index, before)
        self.assertEqual(self.canvas.horizontalScrollBar().value(), scroll)
        self.assertEqual(self.canvas.selected_candidate_id, '')
        self.assertEqual(self.canvas.highlighted_line_ids, set())
        self.assertEqual(events, [[]])

    def test_deleted_association_clears_selection_and_blue_script_range(self):
        self.view.select_take('b')
        self.view.set_nodes(scripts(), [take('a', 1, 5)])
        self.assertEqual(self.canvas.selected_candidate_id, '')
        self.assertEqual(self.canvas.highlighted_line_ids, set())

    def test_independent_interval_selection_keeps_current_take_in_same_group(self):
        group = take('a', 1, 5)
        group['clips'].extend(take('b', 3, 4)['clips'])
        self.view.set_nodes(scripts(), [group])
        self.view.select_take('b')
        self.view.set_selected_interval('interval-a')
        self.assertEqual(self.canvas.selected_candidate_id, 'b')

    def test_one_line_take_has_large_hit_target_even_in_full_overview(self):
        self.view.set_nodes(scripts(10000), [take('tiny', 5000, 5000)])
        self.canvas.fit_all()
        self.view.select_take('tiny')
        rect = self.canvas.take_rect('tiny')
        self.assertGreaterEqual(rect.width(), 18)
        self.assertEqual(self.canvas.hit_take(rect.center()).candidate_id, 'tiny')

    def test_final_tiny_block_can_be_selected_and_fully_shown_at_overview_scale(self):
        self.view.set_nodes(scripts(10000), [take('last', 9999, 9999)])
        self.canvas.fit_all()
        self.view.select_take('last')
        rect = self.canvas.take_rect('last')
        self.assertLessEqual(rect.right(), self.canvas.viewport().width() + 1)
        self.assertEqual(self.canvas.hit_take(rect.center()).candidate_id, 'last')

    def test_drag_handle_emits_new_line_range_without_modifying_source_or_index(self):
        self.canvas.set_line_width(40)
        edits = []
        self.view.coverage_edited.connect(lambda *args: edits.append(args))
        old = self.canvas.index.by_candidate['a']
        rect = self.canvas.take_rect('a')
        first = QPoint(round(rect.left() + 3), round(rect.center().y()))
        last = QPoint(round(self.canvas.line_rect(2).center().x()), first.y())
        QTest.mousePress(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=first)
        QTest.mouseMove(self.canvas.viewport(), last)
        QTest.mouseRelease(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=last)
        self.assertEqual(edits, [('a', 'l2', 'l5')])
        self.assertEqual(self.canvas.index.by_candidate['a'], old)

    def test_uncommitted_async_edit_restores_stored_highlight_before_emitting_request(self):
        self.canvas.set_line_width(40)
        observed = []
        self.view.coverage_edited.connect(lambda *request: observed.append((request, set(self.canvas.highlighted_line_ids))))
        rect = self.canvas.take_rect('a')
        first = QPoint(round(rect.left() + 3), round(rect.center().y()))
        last = QPoint(round(self.canvas.line_rect(2).center().x()), first.y())
        QTest.mousePress(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=first)
        QTest.mouseMove(self.canvas.viewport(), last)
        self.assertEqual(self.canvas.highlighted_line_ids, {'l2', 'l3', 'l4', 'l5'})
        QTest.mouseRelease(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=last)
        self.assertEqual(observed, [(('a', 'l2', 'l5'), {'l1', 'l2', 'l3', 'l4', 'l5'})])
        self.assertEqual(self.canvas.highlighted_line_ids, {'l1', 'l2', 'l3', 'l4', 'l5'})

    def test_escape_cancels_drag_and_restores_original_coverage(self):
        self.canvas.set_line_width(40)
        edits = []
        self.view.coverage_edited.connect(lambda *args: edits.append(args))
        rect = self.canvas.take_rect('a')
        first = QPoint(round(rect.right() - 3), round(rect.center().y()))
        last = QPoint(round(self.canvas.line_rect(8).center().x()), first.y())
        QTest.mousePress(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=first)
        QTest.mouseMove(self.canvas.viewport(), last)
        QTest.keyClick(self.canvas, Qt.Key.Key_Escape)
        QTest.mouseRelease(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=last)
        self.assertEqual(edits, [])
        self.assertEqual(self.canvas.highlighted_line_ids, {'l1', 'l2', 'l3', 'l4', 'l5'})

    def test_drag_cannot_invert_range(self):
        self.canvas.set_line_width(40)
        edits = []
        self.view.coverage_edited.connect(lambda *args: edits.append(args))
        rect = self.canvas.take_rect('a')
        first = QPoint(round(rect.left() + 3), round(rect.center().y()))
        last = QPoint(round(self.canvas.line_rect(8).center().x()), first.y())
        QTest.mousePress(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=first)
        QTest.mouseMove(self.canvas.viewport(), last)
        QTest.mouseRelease(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=last)
        self.assertEqual(edits[-1], ('a', 'l5', 'l5'))

    def test_script_click_emits_correct_line_after_horizontal_scroll(self):
        self.canvas.horizontalScrollBar().setValue(700)
        selected = []
        self.view.line_selected.connect(selected.append)
        point = self.canvas.line_rect(10).center().toPoint()
        QTest.mouseClick(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=point)
        self.assertEqual(selected, ['l10'])

    def test_zoom_anchors_same_script_position_and_full_overview_shows_whole_script(self):
        self.canvas.horizontalScrollBar().setValue(700)
        x = 450
        before = (700 + x - self.canvas.LEFT) / self.canvas.line_width
        self.canvas.set_line_width(140, x)
        after = (self.canvas.horizontalScrollBar().value() + x - self.canvas.LEFT) / self.canvas.line_width
        self.assertLess(abs(before - after), .01)
        self.canvas.fit_all()
        self.assertEqual(self.canvas.horizontalScrollBar().value(), 0)
        self.assertLessEqual(self.canvas.horizontalScrollBar().maximum(), 1)

    def wheel(self, *, pixel=(0, 0), angle=(0, 0), modifiers=Qt.KeyboardModifier.NoModifier,
              target=None, phase=Qt.ScrollPhase.NoScrollPhase):
        point = QPointF(500, 80)
        event = QWheelEvent(point, point, QPoint(*pixel), QPoint(*angle), Qt.MouseButton.NoButton,
                            modifiers, phase, False)
        QApplication.sendEvent(target or self.canvas.viewport(), event)
        self.assertTrue(event.isAccepted())

    def prepare_wheel_model(self, count=200):
        self.view.set_nodes(scripts(1000), [take(f't{n}', 2, 4) for n in range(count)])
        self.app.processEvents()
        self.canvas.horizontalScrollBar().setValue(100)
        self.canvas.verticalScrollBar().setValue(100)

    def test_plain_mouse_wheel_scrolls_tracks_vertically_without_changing_selection(self):
        self.prepare_wheel_model()
        selected = self.canvas.selected_candidate_id
        self.wheel(angle=(0, -120))
        self.assertEqual(self.canvas.verticalScrollBar().value(), 148)
        self.assertEqual(self.canvas.horizontalScrollBar().value(), 100)
        self.assertEqual(self.canvas.selected_candidate_id, selected)
        self.wheel(angle=(0, 120))
        self.assertEqual(self.canvas.verticalScrollBar().value(), 100)

    def test_shift_mouse_wheel_scrolls_script_horizontally(self):
        self.prepare_wheel_model()
        self.wheel(angle=(0, -120), modifiers=Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(self.canvas.horizontalScrollBar().value(), 100 + self.canvas.horizontalScrollBar().singleStep())
        self.assertEqual(self.canvas.verticalScrollBar().value(), 100)

    def test_horizontal_angle_gesture_keeps_its_axis(self):
        self.prepare_wheel_model()
        self.wheel(angle=(-120, 0))
        self.assertEqual(self.canvas.horizontalScrollBar().value(), 196)
        self.assertEqual(self.canvas.verticalScrollBar().value(), 100)

    def test_trackpad_pixel_deltas_scroll_both_axes_without_double_counting_angle_data(self):
        self.prepare_wheel_model()
        self.wheel(pixel=(-11, -7), angle=(-120, -120))
        self.assertEqual(self.canvas.horizontalScrollBar().value(), 111)
        self.assertEqual(self.canvas.verticalScrollBar().value(), 107)
        self.wheel(pixel=(3, 2))
        self.assertEqual(self.canvas.horizontalScrollBar().value(), 108)
        self.assertEqual(self.canvas.verticalScrollBar().value(), 105)

    def test_shift_trackpad_preserves_x_and_routes_y_to_script_axis(self):
        self.prepare_wheel_model()
        self.wheel(pixel=(-3, -7), modifiers=Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(self.canvas.horizontalScrollBar().value(), 110)
        self.assertEqual(self.canvas.verticalScrollBar().value(), 100)

    def test_initial_scrollbar_is_at_bottom_with_nearest_script_tracks_visible(self):
        self.view.set_nodes(scripts(1000), [take(f't{n}', 2, 4) for n in range(200)])
        self.app.processEvents()
        bar = self.canvas.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        self.assertEqual(bar.value(), bar.maximum())
        self.assertEqual(self.canvas._vertical_offset(), 0)
        self.assertIn('t0', [row.candidate_id for row in self.canvas.visible_takes()])

    def test_wheel_visual_direction_and_hit_testing_follow_standard_scrollbar(self):
        self.view.set_nodes(scripts(1000), [take(f't{n}', 2, 4) for n in range(200)])
        self.app.processEvents()
        bar = self.canvas.verticalScrollBar()
        first = next(identity for identity, layout in self.canvas._layout.items() if layout[0] == 1)
        before = self.canvas.take_rect(first)
        initial = bar.value()
        self.wheel(angle=(0, 120))  # Wheel up reveals higher stacked tracks.
        after = self.canvas.take_rect(first)
        self.assertEqual(bar.value(), initial - self.canvas.LANE_HEIGHT)
        self.assertEqual(after.y(), before.y() + self.canvas.LANE_HEIGHT)
        self.assertEqual(self.canvas.hit_take(after.center()).candidate_id, first)
        self.wheel(angle=(0, -120))
        self.assertEqual(self.canvas.take_rect(first), before)
        self.assertEqual(bar.value(), initial)

    def test_scrollbar_top_shows_highest_tracks_and_bottom_shows_nearest_tracks(self):
        self.prepare_wheel_model()
        bar = self.canvas.verticalScrollBar()
        last = max(self.canvas._layout, key=lambda identity: self.canvas._layout[identity][0])
        bar.setValue(0)
        rect = self.canvas.take_rect(last)
        self.assertGreaterEqual(rect.top(), self.canvas.HEADER)
        self.assertLess(rect.bottom(), self.canvas._script_top())
        self.assertEqual(self.canvas.hit_take(rect.center()).candidate_id, last)
        self.assertNotIn('t0', [row.candidate_id for row in self.canvas.visible_takes()])
        bar.setValue(bar.maximum())
        self.assertIn('t0', [row.candidate_id for row in self.canvas.visible_takes()])

    def test_canvas_and_scrollbar_keyboard_navigation_have_same_visual_direction(self):
        self.prepare_wheel_model()
        bar = self.canvas.verticalScrollBar()
        identity = max(self.canvas._layout, key=lambda value: self.canvas._layout[value][0])
        for target in (self.canvas, bar):
            bar.setValue(100)
            before = self.canvas.take_rect(identity).y()
            QTest.keyClick(target, Qt.Key.Key_Down)
            self.assertEqual(bar.value(), 148)
            self.assertEqual(self.canvas.take_rect(identity).y(), before - 48)
            QTest.keyClick(target, Qt.Key.Key_Up)
            self.assertEqual(bar.value(), 100)

    def test_resize_preserves_distance_from_script_without_jumping_to_selected_offscreen_take(self):
        self.prepare_wheel_model()
        self.view.select_take('t0')
        bar = self.canvas.verticalScrollBar()
        bar.setValue(bar.maximum() - 600)
        for height in (600, 240, 430):
            self.view.resize(1100, height)
            self.app.processEvents()
            self.assertEqual(self.canvas._vertical_offset(), 600)
            self.assertEqual(self.canvas.selected_candidate_id, 't0')

    def test_resize_and_new_tracks_keep_bottom_anchored_view(self):
        self.view.set_nodes(scripts(1000), [take(f't{n}', 2, 4) for n in range(200)])
        self.app.processEvents()
        for height in (600, 240):
            self.view.resize(1100, height)
            self.app.processEvents()
            self.assertEqual(self.canvas._vertical_offset(), 0)
        self.view.set_nodes(scripts(1000), [take(f't{n}', 2, 4) for n in range(400)])
        self.assertEqual(self.canvas._vertical_offset(), 0)
        self.assertEqual(self.canvas.verticalScrollBar().value(), self.canvas.verticalScrollBar().maximum())

    def test_low_speed_angle_packets_accumulate_fractional_pixels(self):
        self.prepare_wheel_model()
        for _ in range(5):
            self.wheel(angle=(0, -1))
        self.assertEqual(self.canvas.verticalScrollBar().value(), 102)
        for _ in range(5):
            self.wheel(angle=(0, 1))
        self.assertEqual(self.canvas.verticalScrollBar().value(), 100)
        for _ in range(5):
            self.wheel(angle=(-1, 0))
        self.assertEqual(self.canvas.horizontalScrollBar().value(), 104)

    def test_single_pixel_trackpad_packets_are_not_dropped(self):
        self.prepare_wheel_model()
        for _ in range(5):
            self.wheel(pixel=(0, -1))
        self.assertEqual(self.canvas.verticalScrollBar().value(), 105)

    def test_ctrl_zoom_is_proportional_for_small_angle_packets(self):
        before = self.canvas.line_width
        self.wheel(angle=(0, 1), modifiers=Qt.KeyboardModifier.ControlModifier)
        self.assertAlmostEqual(self.canvas.line_width, before * 1.2 ** (1 / 120), places=10)
        self.wheel(angle=(0, -1), modifiers=Qt.KeyboardModifier.ControlModifier)
        self.assertAlmostEqual(self.canvas.line_width, before, places=10)

    def test_command_zoom_accepts_pixel_only_trackpad_events(self):
        before = self.canvas.line_width
        self.wheel(pixel=(0, 10), modifiers=Qt.KeyboardModifier.MetaModifier)
        self.assertAlmostEqual(self.canvas.line_width, before * 1.2 ** (10 / 120), places=10)

    def test_zero_delta_phase_events_do_not_zoom_or_scroll(self):
        self.prepare_wheel_model()
        state = self.canvas.line_width, self.canvas.horizontalScrollBar().value(), self.canvas.verticalScrollBar().value()
        for modifier in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ControlModifier,
                         Qt.KeyboardModifier.MetaModifier, Qt.KeyboardModifier.ShiftModifier):
            for phase in (Qt.ScrollPhase.ScrollBegin, Qt.ScrollPhase.ScrollEnd):
                self.wheel(modifiers=modifier, phase=phase)
                self.assertEqual((self.canvas.line_width, self.canvas.horizontalScrollBar().value(),
                                  self.canvas.verticalScrollBar().value()), state)

    def test_scrollbars_wheel_events_follow_the_scrollbars_own_axes(self):
        self.prepare_wheel_model()
        hbar, vbar = self.canvas.horizontalScrollBar(), self.canvas.verticalScrollBar()
        self.wheel(angle=(0, -120), target=hbar)
        self.assertEqual((hbar.value(), vbar.value()), (196, 100))
        self.wheel(angle=(0, -120), target=vbar)
        self.assertEqual((hbar.value(), vbar.value()), (196, 148))
        self.wheel(pixel=(0, -7), target=hbar)
        self.assertEqual((hbar.value(), vbar.value()), (203, 148))
        self.wheel(pixel=(0, -5), modifiers=Qt.KeyboardModifier.ShiftModifier, target=vbar)
        self.assertEqual((hbar.value(), vbar.value()), (203, 153))

    def test_zero_delta_over_scrollbars_does_not_accidentally_zoom(self):
        self.prepare_wheel_model()
        before = self.canvas.line_width
        for target in (self.canvas.horizontalScrollBar(), self.canvas.verticalScrollBar()):
            self.wheel(target=target, modifiers=Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.canvas.line_width, before)

    def test_fractional_wheel_at_boundary_does_not_delay_reversing_direction(self):
        self.prepare_wheel_model()
        bar = self.canvas.verticalScrollBar()
        bar.setValue(0)
        for _ in range(8):
            self.wheel(angle=(0, 1))
        self.assertEqual(bar.value(), 0)
        for _ in range(5):
            self.wheel(angle=(0, -1))
        self.assertEqual(bar.value(), 2)

    def test_many_tracks_can_scroll_up_and_down_without_lane_cap_or_metadata_changes(self):
        rows, nodes = scripts(1000), [take(f't{n}', 2, 4, metadata={'sound_roll': 'R1'}) for n in range(3000)]
        before = copy.deepcopy((rows, nodes))
        self.view.set_nodes(rows, nodes)
        self.app.processEvents()
        bar = self.canvas.verticalScrollBar()
        self.assertEqual(len(self.canvas.lanes), 3000)
        self.assertGreater(bar.maximum(), 140000)
        self.wheel(angle=(0, -120 * 4000))
        self.assertEqual(bar.value(), bar.maximum())
        self.wheel(angle=(0, 120 * 4000))
        self.assertEqual(bar.value(), 0)
        self.assertEqual((rows, nodes), before)

    def test_help_explains_vertical_and_shift_horizontal_wheel_navigation(self):
        self.assertIn('上下浏览轨道', self.canvas.toolTip())
        self.assertIn('Shift', self.canvas.toolTip())
        self.assertIn('Command', self.canvas.accessibleDescription())
        self.assertIn('横向', self.canvas.horizontalScrollBar().toolTip())
        self.assertIn('上下', self.canvas.verticalScrollBar().toolTip())

    def test_double_click_requests_manual_range_editor_for_exact_take(self):
        requested = []
        self.view.take_double_clicked.connect(requested.append)
        self.canvas.set_line_width(40)
        point = self.canvas.take_rect('b').center().toPoint()
        QTest.mouseDClick(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=point)
        self.assertEqual(requested, ['b'])
        self.assertIsNone(self.canvas._drag)

    def test_incremental_import_grows_lanes_without_fixed_track_limit(self):
        self.view.select_take('b')
        count = len(self.canvas.lanes)
        nodes = [take('a', 1, 5), take('b', 3, 4)] + [take(f'extra{n}', 3, 4) for n in range(350)]
        self.view.set_nodes(scripts(), nodes)
        self.assertGreater(len(self.canvas.lanes), count)
        self.assertEqual(len(self.canvas.lanes), 352)
        self.assertEqual(self.canvas.selected_candidate_id, 'b')
        self.assertEqual(self.canvas.highlighted_line_ids, {'l3', 'l4'})

    def test_small_embedded_canvas_does_not_force_large_window_and_keeps_selectable_track(self):
        self.view.select_take('b')
        self.view.resize(1000, 160)
        self.app.processEvents()
        self.assertLessEqual(self.view.height(), 180)
        self.assertLess(self.canvas.minimumHeight(), 141)
        self.assertGreaterEqual(self.canvas._script_height(), 24)
        self.canvas.set_line_width(40)
        rect = self.canvas.take_rect('b')
        self.assertGreaterEqual(rect.top(), self.canvas.HEADER - 1)
        self.assertLessEqual(rect.bottom(), self.canvas._script_top() + 1)
        self.assertEqual(self.canvas.hit_take(rect.center()).candidate_id, 'b')

    def test_delete_and_backspace_emit_selected_pgm_request_and_ignore_key_repeat(self):
        removed = []
        self.view.take_remove_requested.connect(removed.append)
        self.view.select_take('b')
        QTest.keyPress(self.canvas, Qt.Key.Key_Delete)
        for _ in range(5):
            event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Delete,
                              Qt.KeyboardModifier.NoModifier, '', True, 1)
            QApplication.sendEvent(self.canvas, event)
        self.assertEqual(removed, ['b'])
        QTest.keyRelease(self.canvas, Qt.Key.Key_Delete)
        QTest.keyClick(self.canvas, Qt.Key.Key_Backspace)
        self.assertEqual(removed, ['b', 'b'])
        self.assertIn('b', self.canvas.index.by_candidate)  # Owner persists removal.

    def test_remove_key_without_selection_or_with_modifier_does_not_emit(self):
        removed = []
        self.view.take_remove_requested.connect(removed.append)
        QTest.keyClick(self.canvas, Qt.Key.Key_Delete)
        QTest.keyClick(self.canvas, Qt.Key.Key_Backspace)
        self.view.select_take('b')
        QTest.keyClick(self.canvas, Qt.Key.Key_Backspace, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(removed, [])

    def test_text_input_delete_backspace_keeps_normal_editing_and_does_not_remove_pgm(self):
        removed = []
        self.view.take_remove_requested.connect(removed.append)
        self.view.select_take('b')
        editor = QLineEdit(self.view)
        editor.setText('abc')
        editor.show()
        editor.setFocus()
        editor.setCursorPosition(3)
        QTest.keyClick(editor, Qt.Key.Key_Backspace)
        self.assertEqual(editor.text(), 'ab')
        editor.setCursorPosition(0)
        QTest.keyClick(editor, Qt.Key.Key_Delete)
        self.assertEqual(editor.text(), 'b')
        self.assertEqual(removed, [])

    def test_focus_selected_button_returns_to_current_take_without_replay_or_zoom(self):
        self.prepare_wheel_model()
        self.view.select_take('t0')
        selected = []
        self.view.take_selected.connect(selected.append)
        bar = self.canvas.verticalScrollBar()
        bar.setValue(0)
        self.canvas.horizontalScrollBar().setValue(5000)
        width = self.canvas.line_width
        self.assertTrue(self.view.focus_selection_button.isEnabled())
        QTest.mouseClick(self.view.focus_selection_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.canvas.line_width, width)
        rect = self.canvas.take_rect('t0')
        self.assertGreaterEqual(rect.left(), self.canvas.LEFT - 1)
        self.assertLess(rect.top(), self.canvas._script_top())
        self.assertGreater(rect.bottom(), self.canvas.HEADER)
        self.assertEqual(self.canvas.hit_take(rect.center()).candidate_id, 't0')
        self.assertEqual(selected, [])
        self.view.clear_selection()
        self.assertFalse(self.view.focus_selection_button.isEnabled())
        self.assertFalse(self.view.focus_selected_take())

    def test_large_metadata_is_neither_copied_nor_inspected_by_index_or_rendering(self):
        class HeavyMetadata:
            def __deepcopy__(self, memo):
                raise AssertionError('PGM media metadata must stay in the caller catalogue')
            def __repr__(self):
                raise AssertionError('PGM media metadata must not be painted or formatted')
        metadata = HeavyMetadata()
        rows = scripts(10000)
        nodes = [take(f't{n}', n, n, metadata=metadata, related_angles=metadata) for n in range(10000)]
        for row in rows:
            row['metadata'] = metadata
        started = time.perf_counter()
        self.view.set_nodes(rows, nodes)
        for _ in range(20):
            self.canvas.viewport().grab()
        self.assertEqual(len(self.canvas.index.takes), 10000)
        self.assertLess(time.perf_counter() - started, 2.)
        self.assertFalse(any(hasattr(row, 'metadata') for row in self.canvas.index.takes))
        self.assertIs(nodes[0]['clips'][0]['metadata'], metadata)

    def test_many_lanes_are_scrollable_and_selecting_high_lane_makes_it_visible(self):
        self.view.set_nodes(scripts(), [take(f't{n}', 2, 4) for n in range(100)])
        self.assertGreater(self.canvas.verticalScrollBar().maximum(), 0)
        identity = max(self.canvas._layout, key=lambda key: self.canvas._layout[key][0])
        self.view.select_take(identity)
        rect = self.canvas.take_rect(identity)
        self.assertGreaterEqual(rect.top(), self.canvas.HEADER - 1)
        self.assertLess(rect.bottom(), self.canvas._script_top() + 1)
        self.assertEqual(self.canvas.hit_take(rect.center()).candidate_id, identity)

    def ruler_click(self, number, modifiers=Qt.KeyboardModifier.NoModifier):
        point = self.canvas.line_rect(number).center().toPoint()
        QTest.mouseClick(self.canvas.viewport(), Qt.MouseButton.LeftButton, modifiers, point)

    def ruler_drag(self, first, last, modifiers=Qt.KeyboardModifier.NoModifier):
        start, end = (self.canvas.line_rect(number).center().toPoint() for number in (first, last))
        QTest.mousePress(self.canvas.viewport(), Qt.MouseButton.LeftButton, modifiers, start)
        QTest.mouseMove(self.canvas.viewport(), end)
        QTest.mouseRelease(self.canvas.viewport(), Qt.MouseButton.LeftButton, modifiers, end)

    def test_ruler_drag_highlights_complete_range_clears_pgm_and_never_edits_media(self):
        rows, nodes = scripts(100), [take('a', 1, 5), take('b', 3, 4)]
        original = copy.deepcopy((rows, nodes))
        self.view.set_nodes(rows, nodes)
        self.canvas.set_line_width(20)
        self.view.select_take('b', ensure_visible=False)
        highlights, edits, takes, removed, lines = [], [], [], [], []
        self.view.coverage_highlighted.connect(highlights.append)
        self.view.coverage_edited.connect(lambda *args: edits.append(args))
        self.view.take_selected.connect(takes.append)
        self.view.take_remove_requested.connect(removed.append)
        self.view.line_selected.connect(lines.append)
        self.ruler_drag(22, 44)  # Visible row numbers 23–45 are inclusive.
        expected = [f'l{n}' for n in range(22, 45)]
        self.assertEqual(highlights[-1], expected)
        self.assertEqual(self.canvas.highlighted_line_ids, set(expected))
        self.assertEqual(self.canvas.selected_candidate_id, '')
        self.assertEqual(lines[-1], 'l22')
        QTest.keyClick(self.canvas, Qt.Key.Key_Delete)
        QTest.keyClick(self.canvas, Qt.Key.Key_Backspace)
        self.assertEqual((edits, takes, removed), ([], [], []))
        self.assertEqual((rows, nodes), original)

    def test_reverse_ruler_drag_is_sorted_and_keeps_every_line(self):
        self.canvas.set_line_width(40)
        self.ruler_drag(12, 3)
        self.assertEqual(self.canvas.highlighted_line_ids, {f'l{n}' for n in range(3, 13)})
        self.assertEqual(self.canvas._highlight_ranges, [(3, 12)])
        self.assertEqual(self.canvas.selected_line_id, 'l3')

    def test_shift_click_extends_from_stable_range_anchor(self):
        self.canvas.set_line_width(40)
        self.ruler_click(4)
        self.ruler_click(10, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(self.canvas.highlighted_line_ids, {f'l{n}' for n in range(4, 11)})
        self.ruler_click(2, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(self.canvas.highlighted_line_ids, {'l2', 'l3', 'l4'})
        self.assertEqual(self.canvas._range_anchor, 4)

    def test_control_and_command_add_disjoint_ranges_without_losing_existing_selection(self):
        self.canvas.set_line_width(40)
        for modifier in (Qt.KeyboardModifier.ControlModifier, Qt.KeyboardModifier.MetaModifier):
            with self.subTest(modifier=modifier):
                self.ruler_drag(2, 4)
                self.ruler_drag(9, 11, modifier)
                expected = {f'l{n}' for n in (2, 3, 4, 9, 10, 11)}
                self.assertEqual(self.canvas.highlighted_line_ids, expected)
                self.assertEqual(self.canvas._highlight_ranges, [(2, 4), (9, 11)])

    def test_control_shift_adds_anchor_range_to_previous_disjoint_selection(self):
        self.canvas.set_line_width(40)
        self.ruler_drag(1, 3)
        self.ruler_click(10, Qt.KeyboardModifier.ControlModifier)
        self.ruler_click(12, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(self.canvas.highlighted_line_ids, {f'l{n}' for n in (1, 2, 3, 10, 11, 12)})

    def test_ruler_single_click_emits_one_navigation_and_one_full_highlight(self):
        self.canvas.set_line_width(40)
        lines, highlights = [], []
        self.view.line_selected.connect(lines.append)
        self.view.coverage_highlighted.connect(highlights.append)
        self.ruler_click(8)
        self.assertEqual(lines, ['l8'])
        self.assertEqual(highlights, [['l8']])

    def test_range_callback_navigation_cannot_cancel_drag_or_reduce_highlight_to_one_line(self):
        self.canvas.set_line_width(40)
        self.view.line_selected.connect(lambda identity: self.view.select_line(identity, ensure_visible=False))
        self.view.line_selected.connect(lambda _: self.view.clear_selection())
        self.ruler_drag(2, 12)
        self.assertEqual(self.canvas.highlighted_line_ids, {f'l{n}' for n in range(2, 13)})
        self.assertEqual(self.canvas.selected_candidate_id, '')
        self.assertIsNone(self.canvas._ruler_drag)

    def test_escape_restores_script_range_and_selected_pgm_without_requesting_edit_or_delete(self):
        self.canvas.set_line_width(40)
        self.view.select_take('b')
        saved = set(self.canvas.highlighted_line_ids)
        removed, edited = [], []
        self.view.take_remove_requested.connect(removed.append)
        self.view.coverage_edited.connect(lambda *args: edited.append(args))
        start, end = (self.canvas.line_rect(number).center().toPoint() for number in (8, 12))
        QTest.mousePress(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(self.canvas.viewport(), end)
        self.assertEqual(self.canvas.selected_candidate_id, '')
        QTest.keyClick(self.canvas, Qt.Key.Key_Escape)
        QTest.mouseRelease(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=end)
        self.assertEqual(self.canvas.selected_candidate_id, 'b')
        self.assertEqual(self.canvas.highlighted_line_ids, saved)
        self.assertEqual((removed, edited), ([], []))
        self.assertFalse(self.canvas._range_scroll_timer.isActive())

    def test_edge_drag_autoscroll_extends_range_across_viewport_and_stops_on_release(self):
        self.view.set_nodes(scripts(1000), [])
        self.view.set_compact_ruler(True)
        self.canvas.set_line_width(30)
        start = self.canvas.line_rect(2).center().toPoint()
        outside = QPoint(self.canvas.viewport().width() + 70, start.y())
        QTest.mousePress(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(self.canvas.viewport(), outside)
        self.assertTrue(self.canvas._range_scroll_timer.isActive())
        before = self.canvas.horizontalScrollBar().value()
        for _ in range(8):
            self.canvas._autoscroll_ruler()
        self.assertGreater(self.canvas.horizontalScrollBar().value(), before)
        last = self.canvas.line_at_x(outside.x())
        self.assertEqual(self.canvas.highlighted_line_ids, {f'l{n}' for n in range(2, last + 1)})
        QTest.mouseRelease(self.canvas.viewport(), Qt.MouseButton.LeftButton, pos=outside)
        self.assertFalse(self.canvas._range_scroll_timer.isActive())

    def test_refresh_retains_manual_range_and_reindexes_anchor_by_line_identity(self):
        self.canvas.set_line_width(40)
        self.ruler_drag(4, 8)
        rows = scripts()
        rows.insert(0, dict(id='new', script_id='s', order=-1, text='新台词'))
        self.view.set_nodes(rows, [take('a', 1, 5, editor_reviewed=True)])
        self.assertEqual(self.canvas.highlighted_line_ids, {f'l{n}' for n in range(4, 9)})
        self.assertEqual(self.canvas._highlight_ranges, [(5, 9)])
        self.assertEqual(self.canvas._range_anchor, 5)
        self.view.set_nodes([dict(id='replacement', text='另一台本')], [])
        self.assertEqual(self.canvas.highlighted_line_ids, set())
        self.assertIsNone(self.canvas._range_anchor)

    def test_compact_ruler_range_survives_resize_and_full_script_mode_toggle(self):
        self.canvas.set_line_width(40)
        self.view.set_compact_ruler(True)
        self.ruler_drag(4, 10)
        expected = {f'l{n}' for n in range(4, 11)}
        for height in (190, 620, 230):
            self.view.resize(1000, height)
            self.app.processEvents()
            self.assertLessEqual(self.canvas._script_height(), 40)
            self.assertEqual(self.canvas.highlighted_line_ids, expected)
            point = self.canvas.line_rect(5).center()
            self.assertGreaterEqual(point.y(), self.canvas._script_top())
            self.assertIsNone(self.canvas.hit_take(point))
        self.view.set_compact_ruler(False)
        self.assertEqual(self.canvas.highlighted_line_ids, expected)

    def test_manual_review_controls_color_including_reference_and_selected_tracks(self):
        self.view.set_nodes(scripts(), [take('auto', 1, 3), take('reviewed', 4, 6, editor_reviewed=True),
            take('base', 7, 9, session='base'), take('base-reviewed', 10, 12, session='base', editor_reviewed=True)], 'base')
        for identity in ('auto', 'reviewed', 'base', 'base-reviewed'):
            row = self.canvas.index.by_candidate[identity]
            for selected in (False, True):
                color, border = self.canvas.take_colors(row, selected)
                hue = QColor(color).hue()
                self.assertTrue(100 < hue < 170 if row.reviewed else 190 < hue < 220)
                if row.reference:
                    self.assertEqual(border, '#687887')
                self.assertNotEqual(color, '#dce4ec')

    def test_reviewed_selected_take_is_rendered_green_not_selection_blue(self):
        self.canvas.set_line_width(40)
        self.view.set_nodes(scripts(), [take('reviewed', 2, 6, editor_reviewed=True)])
        self.view.select_take('reviewed')
        image = self.canvas.viewport().grab().toImage()
        rect = self.canvas.take_rect('reviewed')
        # Away from text, handles and the rounded border.
        sample = image.pixelColor(round(rect.left() + 22), round(rect.top() + 7))
        self.assertEqual(sample.name(), '#26754e')

    def test_full_ten_thousand_line_range_selection_remains_bounded_and_metadata_unchanged(self):
        rows, nodes = scripts(10000), [take(f't{n}', n, n, editor_reviewed=bool(n % 2)) for n in range(10000)]
        before = time.perf_counter()
        self.view.set_nodes(rows, nodes)
        self.view.set_compact_ruler(True)
        self.canvas.set_line_width(.05)
        self.ruler_drag(0, 9999)
        self.assertEqual(len(self.canvas.highlighted_line_ids), 10000)
        self.assertEqual(self.canvas._highlight_ranges, [(0, 9999)])
        self.assertEqual(len(self.canvas.index.takes), 10000)
        self.assertLess(time.perf_counter() - before, 3.)
        self.assertEqual(nodes[0]['clips'][0]['covered_line_ids'], ['l0'])
        self.assertEqual(nodes[-1]['clips'][0]['covered_line_ids'], ['l9999'])

    def test_large_model_visible_paint_work_is_bounded(self):
        rows, nodes = scripts(10000), [take(f't{n}', n, n) for n in range(10000)]
        before = time.perf_counter()
        self.view.set_nodes(rows, nodes)
        self.canvas.set_line_width(96)
        self.app.processEvents()
        self.assertLess(len(list(self.canvas.visible_takes())), 20)
        for _ in range(50):
            self.canvas.viewport().grab()
        self.assertLess(time.perf_counter() - before, 3.)


if __name__ == '__main__':
    unittest.main()
