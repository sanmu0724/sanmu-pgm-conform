"""Range edits must not mistake the preview playhead for a source anchor."""
import copy
import unittest
from unittest.mock import Mock, patch

from PyQt6.QtWidgets import QApplication, QDialog

from manual_script_range import ManualPgmRangeDialog


class PlaybackEndDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_finished_or_overshot_preview_opens_with_usable_anchor(self):
        nodes = [dict(id='line', text='完整台词', script_id='script')]
        for position in (10, 10.04, 200):
            with self.subTest(position=position):
                dialog = ManualPgmRangeDialog(nodes, 'finished.mov', 10, position)
                dialog.line_range.setText('1')
                dialog.save_range()
                self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
                self.assertLess(dialog.result_data['start_seconds'], 10)
                self.assertGreaterEqual(dialog.result_data['start_seconds'], 0)
                dialog.close()

    def test_valid_initial_anchor_remains_unchanged(self):
        nodes = [dict(id='line', text='完整台词', script_id='script')]
        dialog = ManualPgmRangeDialog(nodes, 'paused.mov', 10, 2.375)
        dialog.line_range.setText('1')
        dialog.save_range()
        self.assertEqual(dialog.result_data['start_seconds'], 2.375)
        dialog.close()

    def test_range_only_change_keeps_full_precision_source_anchor(self):
        nodes = [dict(id=f'line{i}', text='完整台词', script_id='script') for i in (1, 2)]
        source_position = 7.123456789123
        dialog = ManualPgmRangeDialog(nodes, 'precise.mov', 10, source_position)
        dialog.line_range.setText('1')
        dialog.line_range.setText('1-2')
        dialog.save_range()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.result_data['start_seconds'], source_position)
        self.assertEqual(dialog.result_data['script_line_ids'], ['line1', 'line2'])
        dialog.close()

    def test_valid_near_end_source_anchor_is_not_rejected_after_display_rounding(self):
        nodes = [dict(id='line', text='完整台词', script_id='script')]
        source_position = 9.9999999999
        dialog = ManualPgmRangeDialog(nodes, 'near-end.mov', 10, source_position)
        self.assertEqual(dialog.position.value(), 10)
        dialog.line_range.setText('1')
        dialog.save_range()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.result_data['start_seconds'], source_position)
        self.assertEqual(dialog.error.text(), '')
        dialog.close()

    def test_explicitly_entered_end_still_requires_a_real_source_position(self):
        nodes = [dict(id='line', text='完整台词', script_id='script')]
        dialog = ManualPgmRangeDialog(nodes, 'paused.mov', 10, 2.375)
        dialog.line_range.setText('1')
        dialog.position.setValue(10)
        dialog.save_range()
        self.assertIsNone(dialog.result_data)
        self.assertIn('结束', dialog.error.text())
        dialog.close()

    def test_automatic_position_normalization_prefers_valid_saved_anchor(self):
        from manual_script_range import normalize_source_position
        for position in (-1, 10, 10.04, 200, float('inf'), float('nan'), None, 'invalid'):
            with self.subTest(position=position):
                self.assertEqual(normalize_source_position(10, position, 3.5), 3.5)
                self.assertEqual(normalize_source_position(10, position, 10), 0)
        self.assertEqual(normalize_source_position(10, 0, 3.5), 0)
        self.assertEqual(normalize_source_position(10, 2.375, 3.5), 2.375)

    def test_automatic_position_normalization_rejects_unknown_duration(self):
        from manual_script_range import normalize_source_position
        for duration in (0, -1, float('inf'), float('nan'), None, 'invalid'):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                normalize_source_position(duration, 0)


class PlaybackRangeUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import test_library_ui as fixtures
        self.fixture = fixtures.LibraryPanelTests()
        self.fixture.setUp()
        self.panel = self.fixture.panel
        self.session = self.fixture.seed_angle_match()
        self.panel.ensure_workflow_context = lambda *_args: (self.fixture.client, self.fixture.production['id'])
        self.panel._load_inline = lambda *_args, **_kwargs: None

    def tearDown(self):
        self.panel.inline_preview = None
        self.fixture.tearDown()

    def preview(self, seconds, path=None):
        from library_bridge import resolve_local_path
        self.panel.inline_preview = Mock()
        self.panel.inline_preview.path = path or resolve_local_path(self.session['config']['pgm'])
        self.panel.inline_preview.player.position.return_value = round(seconds * 1000)

    def anchor(self):
        candidate = self.panel._candidate()
        index = self.panel._take_index()
        first = index.file_summary(candidate)['first_candidate']
        return index.source(first)['start_seconds']

    def test_existing_manual_locator_does_not_move_source_anchor_with_playback(self):
        clip = copy.deepcopy(self.panel._candidate())
        self.panel.main_pages.setCurrentIndex(0)
        initial = self.anchor()
        self.assertEqual(initial, 1)
        for seconds in (0, 4.5, 10, 10.04):
            with self.subTest(seconds=seconds):
                self.preview(seconds)
                with patch('manual_script_range.ManualPgmRangeDialog') as dialog:
                    dialog.return_value.exec.return_value = QDialog.DialogCode.Rejected
                    self.panel.manual_locate_pgm(clip)
                    self.assertEqual(dialog.call_args.args[3], initial)

    def test_track_coverage_edit_after_playback_end_preserves_media_sound_and_anchor(self):
        self.panel.set_workflow_stage('script')
        original = copy.deepcopy(self.fixture.client.call('get_session', session_id=self.session['id'])['config'])
        self.panel._story_take_selected(self.panel._candidate()['id'], replay=False)
        self.preview(10)
        self.panel.coverage_range_input.setText('1-2')
        self.panel.apply_coverage_input()
        self.assertEqual(self.anchor(), 1)
        current = copy.deepcopy(self.fixture.client.call('get_session', session_id=self.session['id'])['config'])
        current.pop('library_pgm_editor', None)
        original.pop('library_pgm_editor', None)
        self.assertEqual(current, original)
        selected = self.panel._candidate()
        self.assertEqual((selected['start_seconds'], selected['end_seconds']), (0, 10))
        self.assertEqual(selected['covered_line_ids'], [row['id'] for row in self.panel._scripts])

    def test_track_edit_without_unique_first_candidate_does_not_prefill_eof(self):
        clip = copy.deepcopy(self.panel._candidate())
        index = self.panel._take_index()
        summary = dict(index.file_summary(clip), first_candidate=None)
        identities = [row['id'] for row in self.panel._scripts]
        self.panel.main_pages.setCurrentIndex(0)
        self.preview(10)
        with patch.object(index, 'file_summary', return_value=summary), \
                patch('manual_script_range.ManualPgmRangeDialog') as dialog:
            dialog.return_value.exec.return_value = QDialog.DialogCode.Rejected
            self.panel._save_story_coverage(clip, identities)
            self.assertGreaterEqual(dialog.call_args.args[3], 0)
            self.assertLess(dialog.call_args.args[3], 10)

    def test_track_manual_without_unique_first_candidate_uses_track_preview_camera(self):
        clip = copy.deepcopy(self.panel._candidate())
        expected = float(clip.get('dialogue_start_seconds', clip.get('start_seconds', 0)))
        index = self.panel._take_index()
        summary = dict(index.file_summary(clip), first_candidate=None)
        self.panel.main_pages.setCurrentIndex(0)
        choice = self.panel.angle_choice
        number = choice.currentIndex()
        original = copy.deepcopy(choice.currentData())
        blocked = choice.blockSignals(True)
        try:
            choice.setItemData(number, dict(original, camera='A'))
        finally:
            choice.blockSignals(blocked)
        self.preview(7)
        with patch.object(index, 'file_summary', return_value=summary), \
                patch('manual_script_range.ManualPgmRangeDialog') as dialog:
            dialog.return_value.exec.return_value = QDialog.DialogCode.Rejected
            self.panel.manual_locate_pgm(clip)
            self.assertEqual(dialog.call_args.args[3], expected)

    def remove_to_pool(self):
        self.panel.remove_pgm_match()
        self.panel._workspace_selection = 'library'
        return self.panel._pending()

    def test_library_range_apply_after_end_uses_valid_fallback_instead_of_playhead_end(self):
        clip = self.remove_to_pool()
        before = copy.deepcopy(self.fixture.client.call('get_session', session_id=self.session['id'])['config'])
        self.preview(10)
        self.panel.coverage_range_input.setText('1-2')
        self.panel.apply_coverage_input()
        self.assertEqual(self.panel.pending_list.count(), 0)
        self.assertGreaterEqual(self.anchor(), 0)
        self.assertLess(self.anchor(), 10)
        current = copy.deepcopy(self.fixture.client.call('get_session', session_id=self.session['id'])['config'])
        for config in (before, current):
            config.pop('library_pgm_editor', None)
        self.assertEqual(current, before)
        self.assertEqual(self.panel._candidate()['media_path'], clip['media_path'])

    def test_library_paused_valid_preview_still_can_define_new_first_line(self):
        clip = self.remove_to_pool()
        self.preview(2.375)
        self.assertEqual(self.panel._pending_source_position(clip), 2.375)
        self.panel.coverage_range_input.setText('1')
        self.panel.apply_coverage_input()
        self.assertEqual(self.anchor(), 2.375)

    def test_other_media_preview_never_overwrites_pgm_source_anchor(self):
        clip = self.remove_to_pool()
        expected = float(clip.get('dialogue_start_seconds', clip.get('start_seconds', 0)))
        self.preview(7, '/Volumes/Edit/unrelated_proxy.mov')
        self.assertEqual(self.panel._pending_source_position(clip), expected)

    def test_wrong_camera_preview_even_at_same_path_never_becomes_pgm_anchor(self):
        clip = self.remove_to_pool()
        expected = float(clip.get('dialogue_start_seconds', clip.get('start_seconds', 0)))
        self.preview(7)
        choice = self.panel.pending_angle_choice
        number, original = choice.currentIndex(), copy.deepcopy(choice.currentData())
        blocked = choice.blockSignals(True)
        try:
            choice.setItemData(number, dict(original, camera='A'))
        finally:
            choice.blockSignals(blocked)
        self.assertEqual(self.panel._pending_source_position(clip), expected)

    def test_cancel_after_preview_end_does_not_change_source_or_match_metadata(self):
        clip = copy.deepcopy(self.panel._candidate())
        production_id = self.fixture.production['id']
        before = dict(
            config=self.fixture.client.call('get_session', session_id=self.session['id'])['config'],
            candidates=self.fixture.client.call('list_candidates', production_id=production_id),
            transcripts=self.fixture.client.call('list_transcripts', production_id=production_id))
        self.preview(10)
        with patch('manual_script_range.ManualPgmRangeDialog') as dialog:
            dialog.return_value.exec.return_value = QDialog.DialogCode.Rejected
            self.panel.manual_locate_pgm(clip)
        after = dict(
            config=self.fixture.client.call('get_session', session_id=self.session['id'])['config'],
            candidates=self.fixture.client.call('list_candidates', production_id=production_id),
            transcripts=self.fixture.client.call('list_transcripts', production_id=production_id))
        self.assertEqual(after, before)


if __name__ == '__main__':
    unittest.main()
