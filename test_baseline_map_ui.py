"""Shared-boundary edits and a paused, non-destructive baseline map editor."""
import copy
import unittest
from unittest.mock import Mock, patch
from PyQt6.QtWidgets import QApplication, QDialog, QWidget
from baseline_map_ui import (BaselineMapDialog, edit_partition, merge_partition,
                             script_nodes, split_partition, validate_partition)


def fixture():
    data = dict(scripts=[dict(id=f'l{n}', script_id='s', line_order=n, speaker='人物',
                              text=f'完整台词{n}\n相关动作' if n == 2 else f'完整台词{n}')
                         for n in range(1, 7)])
    mapping = dict(version=1, reference_session_id='base', fps=25, duration_frames=300,
                   evidence_fingerprint='evidence', segments=[
                       dict(id='first', start_frame=0, end_frame=100, line_ids=['l1', 'l2'],
                            anchor_frame=20, confidence=.95, needs_review=False, reason='自动分区', evidence_ids=['t1']),
                       dict(id='second', start_frame=100, end_frame=200, line_ids=['l3', 'l4'],
                            anchor_frame=120, confidence=.7, needs_review=True, reason='需核实', evidence_ids=['t2']),
                       dict(id='third', start_frame=200, end_frame=300, line_ids=['l5', 'l6'],
                            anchor_frame=220, confidence=.95, needs_review=False, reason='自动分区', evidence_ids=['t3'])])
    cut = dict(fps=25, duration=300, pgm_sources=[
        dict(path='/qa/part1.mov', start=0, end=150, frames=150),
        dict(path='/qa/part2.mov', start=150, end=300, frames=150)])
    return data, mapping, cut


class PartitionEditsTests(unittest.TestCase):
    def setUp(self):
        self.data, self.mapping, self.cut = fixture()
        self.nodes = script_nodes(self.data)

    def test_input_order_families_and_inactive_scripts(self):
        data = dict(scripts=[dict(id='b', script_id='s', line_order=2),
                             dict(id='inactive', script_id='s', active=False),
                             dict(id='a', script_id='s', line_order=1),
                             dict(id='other', script_id='t', line_order=1)])
        self.assertEqual([row['id'] for row in script_nodes(data)], ['a', 'b', 'other'])

    def test_moving_both_shared_edges_keeps_continuous_complete_coverage(self):
        result = edit_partition(self.mapping, 1, 80, 240, ['l3', 'l4'], 120, nodes=self.nodes, confirmed=True)
        self.assertEqual([(row['start_frame'], row['end_frame']) for row in result['segments']],
                         [(0, 80), (80, 240), (240, 300)])
        self.assertFalse(result['segments'][1]['needs_review'])
        self.assertTrue(result['segments'][0]['needs_review'])
        self.assertTrue(result['segments'][2]['needs_review'])
        self.assertIsNone(result['segments'][2]['anchor_frame'])
        self.assertEqual(self.mapping['segments'][2]['anchor_frame'], 220)
        self.assertEqual(self.mapping['segments'][0]['end_frame'], 100)

    def test_cannot_shrink_neighbor_to_zero_or_drop_timeline_end(self):
        for index, start, end in ((1, 0, 200), (1, 100, 300), (0, 1, 100), (2, 200, 299)):
            with self.subTest(index=index, start=start, end=end), self.assertRaisesRegex(ValueError, '边界'):
                row = self.mapping['segments'][index]
                edit_partition(self.mapping, index, start, end, row['line_ids'], row['anchor_frame'], nodes=self.nodes)

    def test_duplicate_or_reversed_script_coverage_rejected_atomically(self):
        before = copy.deepcopy(self.mapping)
        for ids in (['l2', 'l3'], ['l4', 'l3'], ['l6'], ['unknown']):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                edit_partition(self.mapping, 1, 100, 200, ids, 120, nodes=self.nodes)
        self.assertEqual(self.mapping, before)

    def test_split_only_one_child_gets_script_ids_and_true_anchor(self):
        for point, owner in ((10, 1), (20, 1), (60, 0)):
            result = split_partition(self.mapping, 0, point, nodes=self.nodes)
            self.assertEqual(result['segments'][owner]['line_ids'], ['l1', 'l2'])
            self.assertEqual(result['segments'][owner]['anchor_frame'], 20)
            self.assertEqual(result['segments'][1-owner]['line_ids'], [])
            self.assertIsNone(result['segments'][1-owner]['anchor_frame'])
            self.assertTrue(all(row['needs_review'] for row in result['segments'][:2]))
            self.assertEqual(len({row['id'] for row in result['segments']}), 4)

    def test_split_rejects_exclusive_edges(self):
        for point in (0, 100):
            with self.assertRaisesRegex(ValueError, '内部'):
                split_partition(self.mapping, 0, point, nodes=self.nodes)

    def test_merge_preserves_first_onset_and_evidence_without_duplicate_ids(self):
        result = merge_partition(self.mapping, 0, nodes=self.nodes)
        row = result['segments'][0]
        self.assertEqual((row['start_frame'], row['end_frame'], row['anchor_frame']), (0, 200, 20))
        self.assertEqual(row['line_ids'], ['l1', 'l2', 'l3', 'l4'])
        self.assertEqual(row['evidence_ids'], ['t1', 't2'])
        self.assertTrue(row['needs_review'])
        self.assertEqual(len(self.mapping['segments']), 3)

    def test_merge_empty_left_uses_real_right_anchor(self):
        mapping = copy.deepcopy(self.mapping)
        mapping['segments'][0].update(line_ids=[], anchor_frame=None)
        row = merge_partition(mapping, 0, nodes=self.nodes)['segments'][0]
        self.assertEqual(row['anchor_frame'], 120)
        self.assertEqual(row['line_ids'], ['l3', 'l4'])

    def test_action_silence_can_be_reviewed_without_synthetic_anchor(self):
        row = edit_partition(self.mapping, 1, 100, 200, [], None, confirmed=True, nodes=self.nodes)['segments'][1]
        self.assertEqual(row['line_ids'], [])
        self.assertIsNone(row['anchor_frame'])
        self.assertFalse(row['needs_review'])

    def test_validator_rejects_gap_overlap_duplicate_id_and_end_anchor(self):
        for change in ('gap', 'overlap', 'id', 'anchor'):
            mapping = copy.deepcopy(self.mapping)
            if change == 'gap': mapping['segments'][1]['start_frame'] += 1
            if change == 'overlap': mapping['segments'][1]['start_frame'] -= 1
            if change == 'id': mapping['segments'][1]['id'] = 'first'
            if change == 'anchor': mapping['segments'][1]['anchor_frame'] = 200
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_partition(mapping, self.nodes)


class FakePreview(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.loads = []
        self.player = Mock()
        self.player.position.return_value = 0
        self.pause, self.clear, self.toggle_playback = Mock(), Mock(), Mock()

    def load_media(self, path, start, end, *, play=False):
        self.loads.append((path, start, end, play))


class BaselineMapDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.data, self.mapping, self.cut = fixture()
        self.patch = patch('library_ui.InlinePreview', FakePreview)
        self.patch.start()
        self.dialog = BaselineMapDialog(self.data, self.mapping, self.cut)

    def tearDown(self):
        self.dialog.close()
        self.patch.stop()

    def select(self, row):
        self.dialog.table.selectRow(row)
        self.app.processEvents()

    def test_initial_selection_pauses_preview_and_does_not_mutate_input(self):
        self.assertEqual(self.dialog.preview.loads[-1], ('/qa/part1.mov', 0., 4., False))
        self.dialog.preview.pause.assert_called()
        self.assertEqual(self.mapping['segments'][0]['end_frame'], 100)
        self.assertIn('300 帧', self.dialog.summary.text())
        self.assertIn('1 个待核实', self.dialog.summary.text())

    def test_multiline_script_highlight_covers_entire_selected_rows(self):
        selections = self.dialog.script.extraSelections()
        self.assertEqual(len(selections), 2)
        self.assertIn('相关动作', selections[1].cursor.selectedText())
        self.select(1)
        self.assertEqual(len(self.dialog.script.extraSelections()), 2)
        self.assertIn('完整台词3', self.dialog.script.extraSelections()[0].cursor.selectedText())

    def test_cross_pgm_interval_lists_both_sources_in_file_local_positions(self):
        self.select(1)
        self.assertEqual(self.dialog.preview_sources.count(), 2)
        self.assertEqual(self.dialog.preview.loads[-1], ('/qa/part1.mov', 4., 6., False))
        self.dialog.preview_sources.setCurrentIndex(1)
        self.assertEqual(self.dialog.preview.loads[-1], ('/qa/part2.mov', 0., 2., False))

    def test_preview_head_to_anchor_uses_baseline_record_offset_and_clamps_eof(self):
        self.select(1)
        self.dialog.preview_sources.setCurrentIndex(1)
        self.dialog.preview.player.position.return_value = 9000
        self.dialog.use_preview_anchor()
        self.assertEqual(self.dialog.anchor.value(), 199)
        self.assertTrue(self.dialog.anchor_known.isChecked())

    def test_confirm_unknown_dialogue_onset_remains_unknown(self):
        self.select(1)
        self.dialog.anchor_known.setChecked(False)
        self.assertTrue(self.dialog.confirm_selected())
        row = self.dialog.mapping['segments'][1]
        self.assertFalse(row['needs_review'])
        self.assertIsNone(row['anchor_frame'])
        self.assertEqual(row['line_ids'], ['l3', 'l4'])

    def test_confirm_action_interval_leaves_explicit_gap_but_complete_partition(self):
        self.select(1)
        self.dialog.no_dialogue.setChecked(True)
        self.assertTrue(self.dialog.confirm_selected())
        row = self.dialog.mapping['segments'][1]
        self.assertEqual(row['line_ids'], [])
        self.assertIsNone(row['anchor_frame'])
        self.assertFalse(row['needs_review'])

    def test_bad_duplicate_range_shows_error_and_preserves_original_working_copy(self):
        self.select(1)
        self.dialog.line_range.setText('2-4')
        self.assertFalse(self.dialog.confirm_selected())
        self.assertIn('不能', self.dialog.error.text())
        self.assertEqual(self.dialog.mapping, self.mapping)

    def test_manual_shared_boundaries_apply_and_then_can_be_cancelled(self):
        self.select(1)
        self.dialog.start.setValue(90)
        self.dialog.end.setValue(210)
        self.assertTrue(self.dialog.apply_selected())
        self.assertEqual(self.dialog.mapping['segments'][0]['end_frame'], 90)
        self.dialog.reject()
        self.assertEqual(self.mapping['segments'][0]['end_frame'], 100)
        self.assertIsNone(self.dialog.mapping_result())

    def test_split_and_merge_ui_preserve_complete_duration(self):
        self.dialog.split_frame.setValue(50)
        self.dialog.split_selected()
        self.assertEqual(self.dialog.table.rowCount(), 4)
        self.dialog.merge_selected()
        self.assertEqual(self.dialog.table.rowCount(), 3)
        self.assertEqual(self.dialog.mapping['segments'][-1]['end_frame'], 300)

    def test_save_is_local_only_and_returns_defensive_copy(self):
        with patch('baseline_script_map.validate_map', side_effect=AssertionError('must run in worker')) as validator, \
             patch('baseline_script_map.baseline_map_fingerprint', side_effect=AssertionError('must run in worker')) as fingerprint:
            self.dialog.save_mapping()
        self.assertEqual(self.dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(self.dialog.mapping_result(), self.mapping)
        validator.assert_not_called()
        fingerprint.assert_not_called()
        result = self.dialog.mapping_result()
        result['segments'][0]['line_ids'].clear()
        self.assertEqual(self.dialog.result_mapping()['segments'][0]['line_ids'], ['l1', 'l2'])

    def test_save_applies_pending_form_edits_without_silently_losing_them(self):
        self.select(1)
        self.dialog.start.setValue(90)
        self.dialog.save_mapping()
        result = self.dialog.mapping_result()
        self.assertEqual(result['segments'][0]['end_frame'], 90)
        self.assertEqual(result['segments'][1]['start_frame'], 90)
        self.assertTrue(result['segments'][1]['needs_review'])

    def test_local_structure_error_keeps_dialog_open_before_worker_persistence(self):
        self.dialog.mapping['segments'][1]['start_frame'] = 101
        self.dialog.save_mapping()
        self.assertIsNone(self.dialog.mapping_result())
        self.assertIn('完整覆盖', self.dialog.error.text())

    def test_real_algorithm_map_roundtrip_passes_durable_validator(self):
        from baseline_script_map import build_baseline_map, validate_map
        from test_reference_timeline import dataset
        data, cuts = dataset()
        mapping = build_baseline_map(data, 'base', cuts['base'])
        dialog = BaselineMapDialog(data, mapping, cuts['base'])
        try:
            dialog.save_mapping()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted, dialog.error.text())
            self.assertEqual(dialog.mapping_result()['evidence_fingerprint'], mapping['evidence_fingerprint'])
            self.assertEqual(dialog.mapping_result()['segments'][-1]['end_frame'], 250)
            # The same strict validator is called by background save/export.
            validated = validate_map(dialog.mapping_result(), data, cuts['base'], 'base')
            self.assertEqual(validated['segments'], dialog.mapping_result()['segments'])
        finally:
            dialog.close()

    def test_unicode_supplementary_characters_preserve_script_highlight_offsets(self):
        data = copy.deepcopy(self.data)
        data['scripts'][0]['text'] = '表情😀，台词开始'
        dialog = BaselineMapDialog(data, self.mapping, self.cut)
        try:
            selection = dialog.script.extraSelections()[1]
            self.assertIn('完整台词2', selection.cursor.selectedText())
            self.assertIn('相关动作', selection.cursor.selectedText())
            self.assertNotIn('台词开始', selection.cursor.selectedText())
        finally:
            dialog.close()

    def test_pending_filter_hides_rows_without_renumbering_partition_indices(self):
        self.dialog.only_pending.setChecked(True)
        self.assertEqual(self.dialog.table.rowCount(), 3)
        self.assertTrue(self.dialog.table.isRowHidden(0))
        self.assertFalse(self.dialog.table.isRowHidden(1))
        self.assertTrue(self.dialog.table.isRowHidden(2))
        self.assertEqual(self.dialog.table.currentRow(), 1)
        self.assertEqual(self.dialog.start.value(), 100)
        self.assertEqual(self.dialog.line_range.text(), '3-4')

    def test_pending_filter_after_last_confirmation_can_save_full_partition(self):
        self.dialog.only_pending.setChecked(True)
        self.dialog.confirm_selected()
        self.assertFalse(self.dialog.next_pending_button.isEnabled())
        self.assertFalse(self.dialog.editor.isEnabled())
        self.assertEqual(self.dialog.table.currentRow(), -1)
        self.assertTrue(all(self.dialog.table.isRowHidden(index) for index in range(3)))
        self.assertIn('所有区间', self.dialog.reason.text())
        self.dialog.next_pending()
        self.assertEqual(self.dialog.mapping['segments'][-1]['end_frame'], 300)
        self.dialog.only_pending.setChecked(False)
        self.assertTrue(self.dialog.editor.isEnabled())
        self.assertEqual(self.dialog.table.currentRow(), 0)
        self.assertTrue(all(not self.dialog.table.isRowHidden(index) for index in range(3)))

    def test_next_pending_skips_reviewed_rows_and_wraps(self):
        self.dialog.mapping['segments'][0]['needs_review'] = True
        self.dialog._render(1)
        self.dialog.next_pending()
        self.assertEqual(self.dialog.table.currentRow(), 0)
        self.dialog.next_pending()
        self.assertEqual(self.dialog.table.currentRow(), 1)
        self.assertEqual(self.dialog.line_range.text(), '3-4')

    def test_split_in_filtered_view_preserves_logical_indices_and_complete_coverage(self):
        self.dialog.only_pending.setChecked(True)
        self.dialog.split_frame.setValue(150)
        self.dialog.split_selected()
        self.assertEqual(len(self.dialog.mapping['segments']), 4)
        self.assertEqual(self.dialog.mapping['segments'][0]['end_frame'], 100)
        self.assertEqual(self.dialog.mapping['segments'][1]['end_frame'], 150)
        self.assertEqual(self.dialog.mapping['segments'][2]['start_frame'], 150)
        self.assertTrue(self.dialog.table.isRowHidden(0))
        self.assertFalse(self.dialog.table.isRowHidden(1))
        self.assertFalse(self.dialog.table.isRowHidden(2))
        self.assertEqual(self.dialog.mapping['segments'][-1]['end_frame'], 300)

    def test_rebuild_is_explicit_and_requires_confirmation(self):
        from PyQt6.QtWidgets import QMessageBox
        with patch('baseline_map_ui.QMessageBox.question', return_value=QMessageBox.StandardButton.No):
            self.dialog.request_rebuild()
        self.assertFalse(self.dialog.rebuild_requested)
        with patch('baseline_map_ui.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes):
            self.dialog.request_rebuild()
        self.assertTrue(self.dialog.rebuild_requested)
        self.assertIsNone(self.dialog.mapping_result())


if __name__ == '__main__':
    unittest.main()
