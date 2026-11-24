"""Exercise the actual QThread load path used by the installed app header."""
import copy
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PyQt6.QtCore import QThread
from PyQt6.QtTest import QTest

from core import AnalysisError
import test_top_session_switch as header_tests


class AsyncSceneSwitchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        header_tests.TopSessionSwitchTests.setUpClass()

    def setUp(self):
        self.fixture = header_tests.TopSessionSwitchTests('test_sound_selector_reloads_scene_and_keeps_sound_page')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.window, self.panel = self.fixture.window, self.fixture.panel
        self.rows = self.fixture.rows
        self.prepared_by_id = {row['id']: row.pop('_prepared_editor') for row in self.rows}
        self.window.prompt_project = True
        self.panel.client = Mock()
        self.panel.client.call.side_effect = lambda method, **params: copy.deepcopy(
            next(row for row in self.rows if row['id'] == params['session_id']))
        self.panel.production.blockSignals(True)
        self.panel.production.addItem('QA', 'project')
        self.panel.production.blockSignals(False)
        self.window.workflow_stage_requested('sound')
        self.entered, self.release = threading.Event(), threading.Event()
        self.addCleanup(self.release.set)
        self.prepared_threads = []
        self.warning = patch('library_ui.QMessageBox.warning')
        self.warning.start()
        self.addCleanup(self.warning.stop)
        self.prepare_error = None
        def prepare(payload):
            self.prepared_threads.append(QThread.currentThread() is self.window.thread())
            self.entered.set()
            if not self.release.wait(2):
                raise AssertionError('QA did not release media preparation')
            if self.prepare_error:
                raise self.prepare_error
            return self.prepared_by_id[payload['id']]
        self.panel.prepare_session_callback = prepare

    def wait_for(self, predicate):
        until = time.monotonic() + 4
        while time.monotonic() < until:
            if predicate():
                return
            QTest.qWait(5)
        self.fail('Scene switch did not reach the expected state')

    def start_second(self):
        self.panel.working_session.setCurrentIndex(self.panel.working_session.findData(self.rows[1]['id']))
        self.wait_for(self.entered.is_set)
        self.assertEqual(self.window.library_context['session_id'], self.rows[0]['id'])
        self.assertEqual(self.panel.working_session.currentData(), self.rows[0]['id'])
        self.assertFalse(self.panel.working_session.isEnabled())
        self.assertEqual(self.prepared_threads, [False])

    def test_real_worker_applies_prepared_scene_then_restores_sound_page(self):
        with patch.object(self.window, 'sync_library_recording') as sync, \
             patch('pgm_app.probe', side_effect=AssertionError('UI media probe')), \
             patch('sound_sync.probe_wav', side_effect=AssertionError('UI WAV probe')):
            self.start_second()
            self.release.set()
            self.wait_for(lambda: not self.panel.is_busy() and
                self.window.library_context['session_id'] == self.rows[1]['id'])
            self.fixture.assert_current_scene(self.rows[1], 'sound')
        sync.assert_not_called()

    def test_real_worker_failure_keeps_previous_selection_and_sound_results(self):
        self.prepare_error = AnalysisError('目标场次的声音素材尚未挂载')
        self.start_second()
        self.release.set()
        self.wait_for(lambda: not self.panel.is_busy())
        self.fixture.assert_current_scene(self.rows[0], 'sound')

    def test_real_worker_cancellation_keeps_previous_scene_and_all_selectors(self):
        self.start_second()
        self.panel.cancel_task()
        self.release.set()
        self.wait_for(lambda: not self.panel.is_busy())
        self.fixture.assert_current_scene(self.rows[0], 'sound')

    def test_queued_selection_while_loading_does_not_start_duplicate_worker(self):
        self.start_second()
        self.panel.working_session.setCurrentIndex(self.panel.working_session.findData(self.rows[1]['id']))
        self.assertEqual(self.panel.working_session.currentData(), self.rows[0]['id'])
        self.release.set()
        self.wait_for(lambda: not self.panel.is_busy() and
            self.window.library_context['session_id'] == self.rows[1]['id'])
        self.assertEqual(self.prepared_threads, [False])
        self.fixture.assert_current_scene(self.rows[1], 'sound')


if __name__ == '__main__':
    unittest.main()
