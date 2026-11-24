"""Changing the permanent scene selector must switch the technical editor too."""
import copy
import unittest
from unittest.mock import Mock, patch

from PyQt6.QtWidgets import QApplication, QMessageBox

from core import AnalysisError
from pgm_app import PreparedSession, Window


class TopSessionSwitchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.settings = patch('library_bridge.read_settings', return_value={})
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.window = Window()
        self.addCleanup(self.window.close)
        self.panel = self.window.library_panel
        self.rows = [self.make_session(1, ['A', 'B'], 'timecode'),
                     self.make_session(2, ['A', 'B', 'C', 'D', 'E', 'F'], 'waveform')]
        self.panel._sessions = self.rows
        self.panel._populate(self.panel.sessions, [(row['name'], '', row['revision']) for row in self.rows])
        self.panel._session = self.rows[0]
        self.panel._fill_session_combos()
        self.window.load_library_session(self.rows[0], _confirmed=True)
        self.assertFalse(self.window.recording_dirty())

    @staticmethod
    def make_session(number, cameras, sound_mode):
        scene_id = f'scene-{number}'
        pgm = f'/test/day{number}/PGM_{number}.mov'
        proxy = f'/test/day{number}/A_proxy.mov'
        wav = f'/test/day{number}/MASTER_{number}.wav'
        config = dict(show_id=f'day0{number}_通条', fps=25, camera_ids=cameras,
            camera_count=len(cameras), label_match_mode='prefix',
            infer_a_when_blank=number == 1,
            labels={camera: ('440' if number == 1 and camera == 'C' else camera)
                    for camera in cameras},
            sound_sync_mode=sound_mode, sound_allow_gaps=number == 1,
            library_context=dict(production_id='project', session_id=scene_id,
                                 revision=1, session_name=f'day0{number}_通条'),
            library_notes=f'第{number}场备注',
            library_media=[dict(id=f'pgm-{number}', kind='pgm', path=pgm,
                                metadata=dict(start_tc='12:00:00:00', frames=25))],
            sources=[dict(camera='A', path=proxy, proxy_path=proxy,
                          start_tc='12:00:00:00', frames=25,
                          original_path='', original_start_tc='', source_id=f'proxy-{number}')],
            sound_sources=[dict(path=wav, channel='main')],
            library_sync={pgm: dict(status='matched', placements=[dict(
                wav_path=wav, offset_seconds=number / 10, score=.9, method=sound_mode)])})
        info = dict(path=wav, samples=48000, sample_rate=48000, channels=2, depth=24,
                    start=0, has_timecode=True, sound_roll=f'ROLL{number}', recording_date='2026-10-01')
        row = dict(id=scene_id, name=config['show_id'], production_id='project', revision=1,
                   config=config, segments=[])
        row['_prepared_editor'] = PreparedSession(('project', scene_id, 1), copy.deepcopy(config),
                                                  None, {wav: info})
        return row

    def choose(self, scene):
        self.panel.working_session.setCurrentIndex(self.panel.working_session.findData(scene['id']))
        QApplication.processEvents()

    def assert_current_scene(self, scene, stage, technical_index=None):
        config = self.window.config()
        self.assertEqual(self.window.library_context['session_id'], scene['id'])
        self.assertEqual(self.panel.selected_session()['id'], scene['id'])
        self.assertEqual(self.panel.working_session.currentData(), scene['id'])
        self.assertEqual(self.panel.sessions.currentRow(), self.rows.index(scene))
        self.assertEqual(self.panel.session_name.text(), scene['name'])
        self.assertEqual(self.panel.notes.toPlainText(), scene['config']['library_notes'])
        self.assertEqual(self.window.camera_ids, tuple(scene['config']['camera_ids']))
        self.assertEqual(self.window.sources.item(0, 1).text(), scene['config']['sources'][0]['path'])
        self.assertEqual(self.window.pgm_table.item(0, 0).text(), scene['config']['library_media'][0]['path'])
        self.assertEqual(self.window.sound_table.item(0, 2).text(),
                         'ROLL' + scene['id'].split('-')[-1])
        self.assertEqual(config['sound_sources'], scene['config']['sound_sources'])
        self.assertEqual(config['sound_sync_mode'], scene['config']['sound_sync_mode'])
        # Legacy scene-level False is upgraded only in the editor's export
        # settings; sources and previously computed sound placements stay intact.
        self.assertTrue(config['sound_allow_gaps'])
        self.assertEqual(config['sound_unmatched_policy'], 'silence')
        self.assertFalse(self.window.sound_allow_gaps.isEnabled())
        self.assertEqual(self.window.sound_matches.rowCount(), 1)
        number = scene['id'].split('-')[-1]
        self.assertEqual(self.window.sound_matches.item(0, 0).text(), f'PGM_{number}.mov')
        self.assertEqual(self.window.sound_matches.item(0, 1).text(), f'MASTER_{number}.wav')
        self.assertEqual(self.window.sound_matches.item(0, 2).text(), f'{int(number) / 10:.6f}')
        self.assertEqual(self.panel.workflow_stage, stage)
        self.assertEqual(self.window.tabs.currentIndex(),
                         technical_index if technical_index is not None else 1 if stage == 'sound' else 0)

    def test_camera_selector_reloads_scene_without_reentering_materials(self):
        self.panel.set_workflow_stage('camera')
        with patch.object(self.window, 'sync_library_recording') as sync:
            self.choose(self.rows[1])
        sync.assert_not_called()
        self.assert_current_scene(self.rows[1], 'camera')
        self.assertFalse(self.window.recording_dirty())

    def test_sound_selector_reloads_scene_and_keeps_sound_page(self):
        self.window.workflow_stage_requested('sound')
        with patch.object(self.window, 'sync_library_recording') as sync:
            self.choose(self.rows[1])
        sync.assert_not_called()
        self.assert_current_scene(self.rows[1], 'sound')
        self.assertFalse(self.window.recording_dirty())

    def test_round_trip_does_not_mix_camera_or_sound_settings(self):
        self.window.workflow_stage_requested('sound')
        with patch.object(self.window, 'sync_library_recording') as sync:
            for scene in (self.rows[1], self.rows[0], self.rows[1], self.rows[0]):
                self.choose(scene)
                self.assert_current_scene(scene, 'sound')
                self.assertFalse(self.window.recording_dirty())
        sync.assert_not_called()

    def test_camera_marker_and_review_subpages_survive_scene_change(self):
        self.window.workflow_stage_requested('camera')
        for index, scene in ((2, self.rows[1]), (3, self.rows[0])):
            with self.subTest(index=index):
                self.window.tabs.setCurrentIndex(index)
                self.choose(scene)
                self.assert_current_scene(scene, 'camera', technical_index=index)

    def test_sound_source_subpage_survives_scene_change(self):
        self.window.workflow_stage_requested('sound')
        self.window.sound_data_tabs.setCurrentWidget(self.window.sound_sources_page)
        self.choose(self.rows[1])
        self.assert_current_scene(self.rows[1], 'sound')
        self.assertIs(self.window.sound_data_tabs.currentWidget(), self.window.sound_sources_page)

    def test_materials_selector_stays_lightweight_until_technical_page_is_requested(self):
        self.panel.set_workflow_stage('materials')
        with patch.object(self.window, 'load_library_session', wraps=self.window.load_library_session) as load:
            self.choose(self.rows[1])
            load.assert_not_called()
            self.assertEqual(self.panel.selected_session()['id'], self.rows[1]['id'])
            self.assertEqual(self.window.library_context['session_id'], self.rows[0]['id'])
            self.window.workflow_stage_requested('sound')
        self.assert_current_scene(self.rows[1], 'sound')

    def test_header_change_emits_one_navigation_request(self):
        self.window.workflow_stage_requested('sound')
        changes = []
        self.panel.working_session_selected.connect(lambda row: changes.append(row['id']))
        with patch.object(self.window, 'load_library_session', wraps=self.window.load_library_session) as load:
            self.choose(self.rows[1])
        self.assertEqual(changes, [self.rows[1]['id']])
        load.assert_called_once()
        self.assert_current_scene(self.rows[1], 'sound')

    def test_readiness_navigation_selects_target_without_header_request(self):
        self.window.workflow_stage_requested('camera')
        self.panel._session_readiness[self.rows[1]['id']] = dict(
            camera_ready=True, calibration_ready=True, cut_xml_ready=True)
        changes = []
        self.panel.working_session_selected.connect(changes.append)
        with patch.object(self.window, 'load_library_session', wraps=self.window.load_library_session) as load:
            self.panel._navigate_readiness(self.rows[1]['id'])
        self.assertEqual(changes, [])
        load.assert_called_once()
        self.assert_current_scene(self.rows[1], 'sound')

    def test_directed_preparation_issue_does_not_start_header_load_first(self):
        self.window.workflow_stage_requested('camera')
        changes = []
        self.panel.working_session_selected.connect(changes.append)
        with patch.object(self.window, 'locate_preparation_issue') as locate, \
             patch.object(self.window, 'load_library_session') as load:
            self.panel._locate_preparation_issue(self.rows[1]['id'], 'sound', {'pgm': 'test.mov'})
        self.assertEqual(changes, [])
        load.assert_not_called()
        locate.assert_called_once_with(dict(session_id=self.rows[1]['id'], stage='sound',
                                            issue={'pgm': 'test.mov'}))
        self.assertEqual(self.panel.selected_session()['id'], self.rows[1]['id'])

    def test_programmatic_selection_does_not_depend_on_table_population(self):
        changes = []
        self.panel.working_session_selected.connect(changes.append)
        self.panel._populate(self.panel.sessions, [])
        self.assertTrue(self.panel.select_working_session(self.rows[1]['id']))
        self.assertEqual(self.panel.selected_session()['id'], self.rows[1]['id'])
        self.assertEqual(self.panel.working_session.currentData(), self.rows[1]['id'])
        self.assertEqual(self.panel.session_name.text(), self.rows[1]['name'])
        self.assertEqual(self.panel.notes.toPlainText(), self.rows[1]['config']['library_notes'])
        self.assertEqual(self.panel.dialogue_session.currentData(), self.rows[1]['id'])
        self.assertEqual(self.panel.materials.item(0, 2).text(),
                         self.rows[1]['config']['library_media'][0]['path'])
        self.assertEqual(changes, [])

    def test_selection_helper_preserves_existing_signal_blocks(self):
        for widget in (self.panel.sessions, self.panel.working_session, self.panel.dialogue_session):
            widget.blockSignals(True)
        try:
            self.panel.select_working_session(self.rows[1]['id'])
            for widget in (self.panel.sessions, self.panel.working_session, self.panel.dialogue_session):
                self.assertTrue(widget.signalsBlocked())
        finally:
            for widget in (self.panel.sessions, self.panel.working_session, self.panel.dialogue_session):
                widget.blockSignals(False)

    def test_same_scene_signal_does_not_reload_or_discard_editor_state(self):
        self.window.workflow_stage_requested('camera')
        with patch.object(self.window, 'load_library_session') as load:
            self.panel.working_session_changed()
        load.assert_not_called()
        self.assert_current_scene(self.rows[0], 'camera')

    def test_stale_current_scene_is_refreshed_when_requested_again(self):
        self.window.workflow_stage_requested('camera')
        self.window.library_materials_stale = True
        with patch.object(self.window, 'load_library_session', wraps=self.window.load_library_session) as load:
            self.panel.working_session_changed()
        load.assert_called_once()
        self.assertFalse(self.window.library_materials_stale)
        self.assert_current_scene(self.rows[0], 'camera')

    def test_failed_scene_load_preserves_original_selection_and_data(self):
        self.window.workflow_stage_requested('sound')
        with patch.object(self.window, 'load_library_session', side_effect=AnalysisError('媒体尚未挂载')), \
             patch('pgm_app.QMessageBox.warning') as warning:
            self.choose(self.rows[1])
        warning.assert_called_once()
        self.assert_current_scene(self.rows[0], 'sound')

    def test_busy_or_queued_scene_change_restores_loaded_scene(self):
        self.window.workflow_stage_requested('sound')
        for internal in (False, True):
            with self.subTest(internal=internal):
                self.panel.set_external_busy(not internal)
                self.panel.worker = Mock() if internal else None
                try:
                    with patch.object(self.window, 'load_library_session') as load, \
                         patch('pgm_app.QMessageBox.warning'):
                        self.choose(self.rows[1])
                    load.assert_not_called()
                    self.assert_current_scene(self.rows[0], 'sound')
                finally:
                    self.panel.worker = None
                    self.panel.set_external_busy(False)

    def test_cancel_unsaved_scene_change_restores_all_scene_selectors(self):
        self.window.workflow_stage_requested('sound')
        self.window.sound_sync_mode.setCurrentIndex(self.window.sound_sync_mode.findData('waveform'))
        self.assertTrue(self.window.recording_dirty())
        with patch('pgm_app.QMessageBox.exec', return_value=QMessageBox.StandardButton.Cancel) as prompt:
            self.choose(self.rows[1])
        prompt.assert_called_once()
        self.assertEqual(self.window.library_context['session_id'], self.rows[0]['id'])
        self.assertEqual(self.panel.selected_session()['id'], self.rows[0]['id'])
        self.assertEqual(self.panel.working_session.currentData(), self.rows[0]['id'])
        self.assertEqual(self.panel.sessions.currentRow(), 0)
        self.assertEqual(self.panel.session_name.text(), self.rows[0]['name'])
        self.assertEqual(self.panel.notes.toPlainText(), self.rows[0]['config']['library_notes'])
        self.assertEqual(self.window.sound_sync_mode.currentData(), 'waveform')
        self.assertTrue(self.window.sound_allow_gaps.isChecked())
        self.assertEqual(self.panel.workflow_stage, 'sound')

    def test_unsaved_scene_save_waits_before_loading_target(self):
        self.window.workflow_stage_requested('sound')
        self.window.sound_sync_mode.setCurrentIndex(self.window.sound_sync_mode.findData('waveform'))
        self.assertTrue(self.window.recording_dirty())
        continuations = []
        with patch('pgm_app.QMessageBox.exec', return_value=QMessageBox.StandardButton.Save), \
             patch.object(self.window, 'save_recording_results',
                          side_effect=lambda after_save=None: continuations.append(after_save)) as save, \
             patch.object(self.window, 'sync_library_recording') as sync:
            self.choose(self.rows[1])
            save.assert_called_once()
            self.assertEqual(self.window.library_context['session_id'], self.rows[0]['id'])
            self.assertEqual(len(continuations), 1)
            continuations[0]()
        sync.assert_not_called()
        self.assert_current_scene(self.rows[1], 'sound')

    def test_discard_unsaved_scene_change_reloads_target(self):
        self.window.workflow_stage_requested('camera')
        self.window.sound_sync_mode.setCurrentIndex(self.window.sound_sync_mode.findData('waveform'))
        self.assertTrue(self.window.recording_dirty())
        with patch('pgm_app.QMessageBox.exec', return_value=QMessageBox.StandardButton.Discard) as prompt:
            self.choose(self.rows[1])
        prompt.assert_called_once()
        self.assert_current_scene(self.rows[1], 'camera')
        self.assertFalse(self.window.recording_dirty())


if __name__ == '__main__':
    unittest.main()
