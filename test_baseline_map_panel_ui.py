"""Baseline map UI dispatch never reopens the obsolete ASR-occurrence picker."""
import copy
import unittest
from unittest.mock import Mock, patch

from PyQt6.QtWidgets import QApplication, QDialog
from baseline_script_map import build_baseline_map
import test_library_ui as fixtures


class BaselineMapPanelIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.fixture = fixtures.LibraryPanelTests()
        self.fixture.setUp()
        self.panel = self.fixture.panel
        self.first, self.second, self.base_clip, self.alt_clip = self.fixture._confirmed_reference_fixture()
        self.fixture._review_reference_take(self.alt_clip)
        self.panel.apply_reference_state(dict(reference_session_id=self.first['id'], revision=4))
        self.requests, self.exports = [], []
        self.panel.baseline_map_requested.connect(self.requests.append)
        self.panel.reference_export_requested.connect(self.exports.append)
        self.panel._session_readiness = {session['id']: dict(ready=True) for session in (self.first, self.second)}
        self.cut = dict(fps=25, duration=500, pgm_sources=[
            dict(path=self.base_clip['media_path'], start=0, end=500, frames=500)])

    def tearDown(self):
        self.fixture.tearDown()

    def data(self):
        return dict(sessions=self.panel._sessions, scripts=self.panel._scripts,
                    transcripts=self.panel._transcripts, candidates=self.panel._candidates)

    def ready_mapping(self):
        mapping = build_baseline_map(self.data(), self.first['id'], self.cut)
        for row in mapping['segments']:
            row['needs_review'] = False
        self.panel.apply_reference_state(dict(reference_session_id=self.first['id'],
            revision=5, baseline_script_map=mapping))
        return mapping

    def test_legacy_mapping_controls_stay_hidden_in_manual_active_workflow(self):
        self.panel.set_workflow_stage('script')
        self.assertTrue(self.panel.baseline_map_button.isHidden())
        self.assertTrue(self.panel.baseline_map_status.isHidden())
        self.panel.set_workflow_stage('reference')
        self.assertTrue(self.panel.baseline_map_button.isHidden())
        self.assertTrue(self.panel.baseline_map_status.isHidden())
        self.assertTrue(self.panel.baseline_map_button.isEnabled())
        self.panel.set_external_busy(True)
        self.assertFalse(self.panel.baseline_map_button.isEnabled())

    def test_missing_mapping_routes_to_prepare_without_occurrence_picker_or_file_dialog(self):
        with patch.object(self.panel, '_choose_baseline_candidate', side_effect=AssertionError('old picker')), \
             patch('library_ui.QFileDialog.getExistingDirectory') as path:
            self.panel.export_reference(True, True)
        self.assertEqual(self.exports, [])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0]['operation'], 'prepare_baseline_map')
        self.assertEqual(self.requests[0]['expected_revision'], 4)
        self.assertEqual(self.requests[0]['reference_session_id'], self.first['id'])
        path.assert_not_called()
        self.assertIn('映射', self.panel.status.text())

    def test_ready_mapping_exports_green_whole_pgm_without_saved_occurrence_pairs(self):
        mapping = self.ready_mapping()
        self.panel._baseline_map_cut = copy.deepcopy(self.cut)
        with patch.object(self.panel, '_choose_baseline_candidate', side_effect=AssertionError('old picker')), \
             patch('library_ui.QInputDialog.getItem', side_effect=AssertionError('unneeded ASR choice')), \
             patch('library_ui.QFileDialog.getExistingDirectory', return_value='/qa/exports'):
            self.panel.export_reference(True, True)
        self.assertEqual(self.requests, [])
        self.assertEqual(len(self.exports), 1)
        self.assertIsNone(self.exports[0]['resolved_pairs'])
        self.assertEqual(set(self.exports[0]['exported_take_pairs']), {self.alt_clip['id']})
        self.assertEqual(self.panel.reference_state['baseline_script_map'], mapping)

    def test_baseline_only_does_not_require_mapping_or_open_editor(self):
        with patch('library_ui.QFileDialog.getExistingDirectory', return_value='/qa/base'):
            self.panel.export_reference(False, True)
        self.assertEqual(self.requests, [])
        self.assertFalse(self.exports[0]['include_alternatives'])
        self.assertEqual(self.exports[0]['exported_take_pairs'], {})

    def test_uncertain_or_unknown_onset_returns_to_map_editor(self):
        for field, value in (('needs_review', True), ('anchor_frame', None)):
            mapping = self.ready_mapping()
            mapped = next(row for row in mapping['segments'] if self.alt_clip['script_line_id'] in row['line_ids'])
            mapped[field] = value
            self.panel.apply_reference_state(dict(reference_session_id=self.first['id'], baseline_script_map=mapping))
            with self.subTest(field=field), patch('library_ui.QFileDialog.getExistingDirectory') as path:
                self.panel.export_reference(True, True)
            self.assertEqual(self.requests[-1]['operation'], 'prepare_baseline_map')
            path.assert_not_called()
        self.assertEqual(self.exports, [])

    def test_evidence_validation_is_delegated_to_background_export_without_gui_hashing(self):
        self.ready_mapping()
        self.panel._baseline_map_cut = copy.deepcopy(self.cut)
        source = next(row for row in self.panel._transcripts if row['id'] == self.base_clip['transcript_id'])
        source['text'] += ' 变化'
        with patch('baseline_script_map.baseline_map_fingerprint', side_effect=AssertionError('GUI fingerprint')), \
             patch('baseline_script_map.validate_map', side_effect=AssertionError('GUI evidence validation')), \
             patch('library_ui.QFileDialog.getExistingDirectory', return_value='/qa/exports'):
            self.panel.export_reference(True, True)
        self.assertEqual(self.requests, [])
        self.assertEqual(len(self.exports), 1, 'worker receives request and performs authoritative stale-evidence checks')
        self.assertEqual(self.exports[0]['expected_revision'], self.panel.reference_state['revision'])

    def test_invalid_partition_routes_to_editor_with_only_cheap_structure_check(self):
        mapping = self.ready_mapping()
        mapping['segments'][-1]['end_frame'] -= 1
        self.panel.apply_reference_state(dict(self.panel.reference_state, baseline_script_map=mapping))
        with patch('baseline_script_map.baseline_map_fingerprint', side_effect=AssertionError('GUI fingerprint')), \
             patch('library_ui.QFileDialog.getExistingDirectory') as path:
            self.panel.export_reference(True, True)
        path.assert_not_called()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.exports, [])

    def test_mapping_status_displays_partition_and_adaptive_granularity(self):
        self.ready_mapping()
        self.assertIn('个区间', self.panel.baseline_map_status.text())
        self.assertIn('个待核实', self.panel.baseline_map_status.text())
        self.assertIn('最小备选单元', self.panel.baseline_map_status.text())

    def test_dialog_save_emits_mapping_with_result_revision_without_full_ui_rebuild(self):
        mapping = self.ready_mapping()
        result = dict(data=self.data(), state=dict(self.panel.reference_state, revision=9),
                      baseline_script_map=mapping, baseline_cut=self.cut)
        with patch('baseline_map_ui.BaselineMapDialog') as dialog, \
             patch.object(self.panel, '_set_data', side_effect=AssertionError('heavy full rebuild')):
            dialog.return_value.exec.return_value = QDialog.DialogCode.Accepted
            dialog.return_value.rebuild_requested = False
            dialog.return_value.mapping_result.return_value = mapping
            self.panel.present_baseline_mapping(result)
        self.assertEqual(self.requests[-1]['operation'], 'save_baseline_map')
        self.assertEqual(self.requests[-1]['expected_revision'], 9)
        self.assertEqual(self.requests[-1]['mapping'], mapping)

    def test_dialog_rebuild_uses_cached_prepare_operation(self):
        mapping = self.ready_mapping()
        result = dict(data=self.data(), state=self.panel.reference_state,
                      baseline_script_map=mapping, baseline_cut=self.cut)
        with patch('baseline_map_ui.BaselineMapDialog') as dialog:
            dialog.return_value.exec.return_value = QDialog.DialogCode.Accepted
            dialog.return_value.rebuild_requested = True
            self.panel.present_baseline_mapping(result)
        self.assertTrue(self.requests[-1]['rebuild'])
        self.assertEqual(self.requests[-1]['operation'], 'prepare_baseline_map')

    def test_cancelled_dialog_does_not_emit_save(self):
        mapping = self.ready_mapping()
        result = dict(data=self.data(), state=self.panel.reference_state,
                      baseline_script_map=mapping, baseline_cut=self.cut)
        with patch('baseline_map_ui.BaselineMapDialog') as dialog:
            dialog.return_value.exec.return_value = QDialog.DialogCode.Rejected
            self.panel.present_baseline_mapping(result)
        self.assertEqual(self.requests, [])

    def test_standalone_panel_uses_workflow_and_does_not_run_recognition(self):
        self.panel.baseline_map_requested.disconnect(self.requests.append)
        mapping = self.ready_mapping()
        result = dict(data=self.data(), state=self.panel.reference_state,
                      baseline_script_map=mapping, baseline_cut=self.cut)
        with patch('reference_workflow.ReferenceWorkflow') as workflow, \
             patch.object(self.panel, 'present_baseline_mapping') as present, \
             patch.object(self.panel, 'recognize_and_match', side_effect=AssertionError('ASR must be reused')):
            workflow.return_value.prepare_baseline_map.return_value = result
            self.panel.open_baseline_mapping()
        workflow.return_value.prepare_baseline_map.assert_called_once_with(rebuild=False)
        present.assert_called_once_with(result)
        self.assertEqual(self.panel._baseline_map_cut, self.cut)

    def test_map_only_reference_save_refreshes_next_review_cas_without_reloading_inventory(self):
        before_versions = self.panel._pgm_match_versions
        frozen_versions = copy.deepcopy(before_versions)
        inventory = {name: getattr(self.panel, name) for name in
                     ('_sessions', '_scripts', '_transcripts', '_candidates')}
        state = dict(self.panel.reference_state, revision=23)
        self.panel.apply_reference_state(state)
        self.assertTrue(self.panel._pgm_match_versions)
        self.assertTrue(all(row['reference_revision'] == 23 for row in self.panel._pgm_match_versions))
        self.assertEqual(before_versions, frozen_versions, 'previous worker snapshots remain immutable')
        for previous, current in zip(frozen_versions, self.panel._pgm_match_versions):
            self.assertEqual(current['session_revision'], previous['session_revision'])
            self.assertEqual(current['candidate_revisions'], previous['candidate_revisions'])
        for name, rows in inventory.items():
            self.assertIs(getattr(self.panel, name), rows, 'mapping save must not reload material inventories')
        self.panel._workspace_selection = 'track'
        self.panel._selected_story_take_id = self.alt_clip['id']
        captured = {}
        class CapturedReview(Exception):
            pass
        def capture(session_id, path, **kwargs):
            captured.update(session_id=session_id, path=path, **kwargs)
            raise CapturedReview()
        with patch('library_workflow.BatchWorkflow.mark_pgm_reviewed', side_effect=capture):
            with self.assertRaises(CapturedReview):
                self.panel.review_workspace_clip()
        self.assertEqual(captured['session_id'], self.second['id'])
        self.assertEqual(captured['path'], self.alt_clip['media_path'])
        expected = captured['expected_match_version']
        self.assertEqual(expected['reference_revision'], 23)
        prior = next(row for row in frozen_versions if row['session_id'] == self.second['id']
                     and row['media_path'] == self.alt_clip['media_path'])
        self.assertEqual(expected['session_revision'], prior['session_revision'])
        self.assertEqual(expected['candidate_revisions'], prior['candidate_revisions'])

    def test_prepare_request_is_guarded_while_busy(self):
        self.panel.set_external_busy(True)
        with self.assertRaisesRegex(ValueError, '等待'):
            self.panel.open_baseline_mapping()
        self.assertEqual(self.requests, [])


if __name__ == '__main__':
    unittest.main()
