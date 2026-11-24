import copy
import unittest
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication, QDialog

from manual_script_range import ManualPgmRangeDialog, parse_script_range


class ManualRangeInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.nodes = [dict(id=f'l{n}', text=f'完整台词{n}', script_id='script') for n in range(1, 101)]

    def test_single_number_and_inclusive_range(self):
        self.assertEqual(parse_script_range('55', self.nodes), ['l55'])
        self.assertEqual(parse_script_range('台本 55—76', self.nodes), [f'l{n}' for n in range(55, 77)])

    def test_invalid_or_reversed_ranges_and_cross_script_rejected(self):
        for text in ('', 'abc', '0', '101', '76-55', '1-2-3', '5.5', '55,76'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_script_range(text, self.nodes)
        self.nodes[55]['script_id'] = 'another'
        with self.assertRaisesRegex(ValueError, '不同台本'):
            parse_script_range('55-56', self.nodes)

    def test_dialog_saves_explicit_range_and_source_local_anchor(self):
        dialog = ManualPgmRangeDialog(self.nodes, 'PIX_001_046.mov', 3270, 7.1)
        dialog.line_range.setText('55-76')
        self.assertIn('完整台词55', dialog.summary.text())
        self.assertIn('完整台词76', dialog.summary.text())
        dialog.save_range()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.result_data['script_line_ids'], [f'l{n}' for n in range(55, 77)])
        self.assertEqual(dialog.result_data['start_seconds'], 7.1)
        dialog.close()

    def test_dialog_validation_keeps_invalid_input_open(self):
        dialog = ManualPgmRangeDialog(self.nodes, 'PGM.mov', 20)
        dialog.line_range.setText('101')
        dialog.save_range()
        self.assertIsNone(dialog.result_data)
        self.assertIn('1-100', dialog.error.text())
        dialog.line_range.setText('55')
        dialog.position.setValue(20)
        dialog.save_range()
        self.assertIsNone(dialog.result_data)
        self.assertIn('结束', dialog.error.text())
        dialog.close()


class ManualRangeUiFlowTests(unittest.TestCase):
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
        self.fixture.tearDown()

    def manual_locate(self, ids, seconds=1.):
        clip = copy.deepcopy(self.panel._candidate())
        with patch('manual_script_range.ManualPgmRangeDialog') as dialog:
            dialog.return_value.exec.return_value = QDialog.DialogCode.Accepted
            dialog.return_value.result_data = dict(script_line_ids=ids, start_seconds=seconds)
            self.panel.manual_locate_pgm(clip)

    def test_manual_range_then_remove_refreshes_axis_status_queue_and_selection(self):
        self.panel.set_workflow_stage('script')
        ids = [row['id'] for row in self.panel._scripts]
        before_asr = {row['id'] for row in self.fixture.client.call('list_transcripts',
            production_id=self.fixture.production['id'])}
        self.manual_locate(ids)
        self.assertEqual(self.panel.story_axis.currentData(), 'pgm')
        self.assertEqual(self.panel._candidate()['covered_line_ids'], ids)
        self.assertTrue(self.panel.story_timeline.index.takes)
        self.assertEqual(self.panel.script_timeline._highlighted_line_ids, set(ids))
        self.assertEqual(self.panel.pending_list.count(), 0)
        path = self.panel._candidate()['media_path']
        self.panel.remove_pgm_match()
        self.assertFalse(self.panel.story_timeline.index.takes)
        self.assertEqual(self.panel.script_timeline._highlighted_line_ids, set())
        self.assertEqual(self.panel.main_pages.currentIndex(), 1)
        self.assertEqual(self.panel.pending_list.currentItem().data(256), self.panel._pending()['id'])
        self.assertEqual(self.panel._pending()['media_path'], path)
        self.assertIn('轨道移出 1', self.panel.pgm_status_text())
        self.assertEqual(self.panel.selected_clip_pairs, {})
        self.assertTrue(before_asr.issubset({row['id'] for row in self.fixture.client.call('list_transcripts',
            production_id=self.fixture.production['id'])}))
        current = self.fixture.client.call('get_session', session_id=self.session['id'])['config']
        self.assertEqual({key:value for key,value in current.items() if key != 'library_pgm_editor'},
                         {key:value for key,value in self.session['config'].items() if key != 'library_pgm_editor'})
        self.assertEqual(current['library_pgm_editor'][path]['pool'], 'removed')

    def test_cancel_does_not_modify_catalogue(self):
        before = self.fixture.client.call('list_candidates', production_id=self.fixture.production['id'])
        with patch('manual_script_range.ManualPgmRangeDialog') as dialog:
            dialog.return_value.exec.return_value = QDialog.DialogCode.Rejected
            self.panel.manual_locate_pgm()
        self.assertEqual(self.fixture.client.call('list_candidates', production_id=self.fixture.production['id']), before)

    def test_baseline_explicit_manual_range_removes_slate_from_pending_files(self):
        state = self.fixture.client.call('get_reference_settings', production_id=self.fixture.production['id'])
        self.fixture.client.call('update_reference_settings', production_id=self.fixture.production['id'],
            reference_session_id=self.session['id'], selected_clip_pairs={}, expected_revision=state['revision'])
        path = self.panel._candidate()['media_path']
        self.fixture.client.call('add_transcript', session_id=self.session['id'], media_path=path,
            segments=[dict(start_seconds=.1, end_seconds=.5, text='三二一开拍')])
        self.fixture.refresh(self.session)
        self.manual_locate([row['id'] for row in self.panel._scripts], seconds=2.)
        self.assertEqual(self.panel.pending_list.count(), 0)
        self.assertIn('轨道已核实 1', self.panel.pgm_status_text())
        self.assertTrue(self.panel._take_index().file_summary(self.panel._candidate())['alignment_ready'])

    def test_remove_uses_ui_snapshot_and_rejects_concurrent_changes(self):
        from library_store import LibraryError
        row = self.panel._candidate()
        self.fixture.client.call('update_candidate', candidate_id=row['id'], status='confirmed',
            note='另一位场记刚核对过', expected_revision=row['revision'])
        with self.assertRaises(LibraryError) as caught:
            self.panel.remove_pgm_match()
        self.assertEqual(caught.exception.code, 'conflict')
        fresh = self.fixture.client.call('list_candidates', production_id=self.fixture.production['id'])
        self.assertEqual(fresh[0]['note'], '另一位场记刚核对过')
        self.assertNotEqual(fresh[0]['status'], 'rejected')


if __name__ == '__main__':
    unittest.main()
