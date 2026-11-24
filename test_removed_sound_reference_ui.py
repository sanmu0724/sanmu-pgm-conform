"""Automatic scene sync owns audio selection; removed manual references stay retired."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QGroupBox, QPushButton, QTableWidgetItem

from pgm_app import Window


class RemovedSoundReferenceUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.settings = patch('library_bridge.read_settings', return_value={})
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.window = Window()
        self.addCleanup(self.window.close)

    def old_config(self, name='day01_通条'):
        return dict(mode='cut_xml', show_id=name, fps=25, sources=[], sound_sources=[],
            sound_sync_mode='timecode', sound_allow_gaps=False,
            sound_reference_overrides={'/qa/PGM.mov': {'path': '/qa/old-reference.mov', 'start_seconds': 3}},
            future_setting={'keep': True})

    @staticmethod
    def cut():
        block = dict(path='/qa/PGM.mov', start=0, end=250, frames=250,
            start_tc='12:00:00:00', width=1280, height=720, xml_path='/qa/cuts.xml')
        return dict(xml_path='/qa/cuts.xml', xml_digest='saved-cut', fps=25, duration=250,
            pgm_info=block, pgm_sources=[block], shots=[dict(start=0, end=250)])

    def assert_no_reference(self):
        for name in ('sound_reference_overrides', 'sound_reference_pgm', 'sound_reference_label',
                     'set_sound_reference_button', 'reset_sound_reference_button',
                     'refresh_sound_pgm', 'show_sound_reference', 'choose_sound_reference',
                     'reset_sound_reference'):
            self.assertFalse(hasattr(self.window, name), name)
        self.assertNotIn('sound_reference_overrides', self.window.config())

    def test_sound_page_has_no_reference_group_widgets_or_callbacks(self):
        self.assert_no_reference()
        self.assertEqual(self.window.sound_split.orientation(), Qt.Orientation.Horizontal)
        self.assertNotIn('参考声音', [group.title() for group in self.window.sound_page.findChildren(QGroupBox)])
        labels = [button.text() for button in self.window.sound_page.findChildren(QPushButton)]
        self.assertNotIn('指定参考…', labels)
        self.assertNotIn('使用PGM', labels)
        self.assertIn('保存本场声音设置', labels)
        self.assertIn('设为／交换左右声道对', labels)

    def test_legacy_draft_is_ignored_without_losing_other_settings(self):
        config = self.old_config()
        before = copy.deepcopy(config)
        self.window.load_library_draft(config, media_prepared=True, sound_infos={})
        self.assert_no_reference()
        result = self.window.config()
        self.assertEqual(result['sound_sync_mode'], 'timecode')
        self.assertTrue(result['sound_allow_gaps'])
        self.assertEqual(result['sound_unmatched_policy'], 'silence')
        self.assertFalse(self.window.sound_allow_gaps.isEnabled())
        self.assertEqual(result['future_setting'], {'keep': True})
        self.assertEqual(result['library_legacy_sound_references'], config['sound_reference_overrides'])
        self.assertEqual(config, before)

    def test_complete_config_and_pgm_refresh_do_not_require_removed_widgets(self):
        config = self.old_config()
        with patch.object(self.window, 'refresh_pgm'):
            self.window.apply_config(config, prepared_cut=self.cut(), sound_infos={})
        self.assert_no_reference()
        self.window.set_pgm_sources(self.cut())
        self.assertEqual(self.window.pgm_table.rowCount(), 1)
        self.assertEqual(self.window.pgm.text(), '/qa/PGM.mov')
        self.assert_no_reference()

    def test_wav_only_policy_keeps_old_sync_cache_and_analysis_reference(self):
        config=self.old_config()
        config['empty_camera_policy']='gap'
        config['library_sync']={'/qa/PGM.mov':dict(status='matched',placements=[
            dict(wav_path='/qa/original.wav',wav_start_seconds=7.5,score=0.9,method='waveform')])}
        config['library_sound_fallback']={'/qa/other.mov':dict(status='fallback',stage='sync',audio_source='pgm')}
        before=copy.deepcopy(config)
        self.window.load_library_draft(config,media_prepared=True,sound_infos={})
        self.window.sound_allow_gaps.setChecked(False)  # A stale external caller cannot restore the old export policy.
        result=self.window.config()
        self.assertEqual(result['sound_unmatched_policy'],'silence')
        self.assertTrue(result['sound_allow_gaps'])
        self.assertEqual(result['empty_camera_policy'],'pgm')
        self.assertEqual(result['library_sync'],before['library_sync'])
        self.assertEqual(result['library_sound_fallback'],before['library_sound_fallback'])
        self.assertEqual(config,before)

    def test_scene_switch_never_revives_legacy_reference_or_carries_other_scene_state(self):
        first = self.old_config()
        self.window.load_library_draft(first, media_prepared=True, sound_infos={})
        self.window.load_library_draft(dict(mode='cut_xml', show_id='day02_分镜头', sources=[],
            sound_sources=[], sound_sync_mode='waveform', sound_allow_gaps=True),
            media_prepared=True, sound_infos={})
        self.assert_no_reference()
        result = self.window.config()
        self.assertEqual(result['show_id'], 'day02_分镜头')
        self.assertEqual(result['sound_sync_mode'], 'waveform')
        self.assertTrue(result['sound_allow_gaps'])
        self.assertNotIn('future_setting', result)
        self.assertNotIn('library_legacy_sound_references', result)
        self.window.load_library_draft(first, media_prepared=True, sound_infos={})
        self.assert_no_reference()

    def test_retired_reference_history_merges_without_reactivating_or_aliasing(self):
        config = self.old_config()
        config['library_legacy_sound_references'] = {
            '/qa/PGM.mov': {'path': '/qa/older.mov', 'start_seconds': 1},
            '/qa/other.mov': {'path': '/qa/other-reference.mov', 'start_seconds': 2}}
        before = copy.deepcopy(config)
        self.window.load_library_draft(config, media_prepared=True, sound_infos={})
        result = self.window.config()
        history = result['library_legacy_sound_references']
        self.assertEqual(history['/qa/PGM.mov'], config['sound_reference_overrides']['/qa/PGM.mov'])
        self.assertEqual(history['/qa/other.mov'], config['library_legacy_sound_references']['/qa/other.mov'])
        history['/qa/PGM.mov']['path'] = '/qa/changed.mov'
        self.assertEqual(config, before)
        self.assertEqual(self.window.config()['library_legacy_sound_references']['/qa/PGM.mov'],
                         config['sound_reference_overrides']['/qa/PGM.mov'])
        self.assert_no_reference()

    def test_saving_legacy_analysis_drops_override_from_all_saved_configs(self):
        config = self.old_config()
        self.window.load_library_draft(config, media_prepared=True, sound_infos={})
        self.window.analysis_config = copy.deepcopy(config)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'saved.json'
            with patch('ui_common.QFileDialog.getSaveFileName', return_value=(str(path), '')):
                self.window.save_project()
            saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertNotIn('sound_reference_overrides', saved['config'])
        self.assertNotIn('sound_reference_overrides', saved['analysis_config'])
        self.assertEqual(saved['analysis_config']['future_setting'], config['future_setting'])
        for key in ('config', 'analysis_config'):
            self.assertEqual(saved[key]['library_legacy_sound_references'], config['sound_reference_overrides'])

    def test_busy_state_disables_and_restores_remaining_sound_controls(self):
        controls = (self.window.sound_table, self.window.pair_sound_button,
            self.window.auto_sound_button, self.window.sound_sync_mode,
            self.window.save_sound_button)
        self.window.set_sound_busy(True)
        self.assertTrue(all(not widget.isEnabled() for widget in controls))
        self.assertTrue(self.window.cancel_sound_button.isEnabled())
        self.window.set_sound_busy(False)
        self.assertTrue(all(widget.isEnabled() for widget in controls))
        self.assertFalse(self.window.sound_allow_gaps.isEnabled())
        self.assertFalse(self.window.cancel_sound_button.isEnabled())
        self.assert_no_reference()

    def test_preparation_issue_navigation_selects_sound_result_without_reference_control(self):
        session = dict(id='scene', production_id='production', name='day01_通条', config={}, segments=[])
        self.window.library_context = dict(session_id='scene', production_id='production')
        self.window.library_panel._sessions = [session]
        self.window.sound_matches.setRowCount(1)
        item = QTableWidgetItem('PGM.mov')
        item.setData(Qt.ItemDataRole.UserRole, '/qa/PGM.mov')
        self.window.sound_matches.setItem(0, 0, item)
        self.window.locate_preparation_issue(dict(session_id='scene', stage='sound',
            issue=dict(pgm='/qa/PGM.mov', message='未匹配原始 WAV，音频留空')))
        self.assertEqual(self.window.sound_matches.currentRow(), 0)
        self.assertIs(self.window.sound_data_tabs.currentWidget(), self.window.sound_results_page)
        self.assertEqual(self.window.tabs.currentIndex(), 1)
        self.assertIn('音频留空', self.window.sound_status.text())
        self.assert_no_reference()


if __name__ == '__main__':
    unittest.main()
