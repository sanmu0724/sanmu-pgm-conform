"""Named-project close prompts must honor cancel and wait for successful saves."""
import copy
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import Mock, patch
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtCore import QCoreApplication, QEvent, QThread
from PyQt6.QtWidgets import QApplication,QDialog,QMessageBox
from library_store import Store
from pgm_app import Window
from project_state import read_project_state


class ProjectWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        with patch('library_bridge.read_settings',return_value={}):self.window=Window()
        self.window.prompt_project=True
        self.window.project_identity={'id':'12b0f1aa4c094f3ba114f795fb594a11','name':'示例剧目'}

    def tearDown(self):
        # A closed window may still be waiting for its real worker's queued
        # finished signal. Drain those signals before destroying the QObject
        # tree, rather than leaving 28 windows for SIP's interpreter cleanup.
        panel=self.window.library_panel
        if not isinstance(panel.worker,QThread):
            panel.worker=None  # Explicit test doubles are released by cleanup.
        elif panel.worker.isRunning():
            panel.worker.requestInterruption()
        deadline=time.monotonic()+3
        while panel.worker is not None and time.monotonic()<deadline:
            self.app.processEvents()
            if isinstance(panel.worker,QThread):panel.worker.wait(10)
        self.assertIsNone(panel.worker,'cooperative library worker did not finish')
        self.window.prompt_project=False;self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def close_with(self,choice):
        event=QCloseEvent()
        with patch('pgm_app.QMessageBox.exec',return_value=choice):self.window.closeEvent(event)
        return event

    def test_cancel_keeps_window_and_library_open(self):
        event=self.close_with(QMessageBox.StandardButton.Cancel)
        self.assertFalse(event.isAccepted());self.assertFalse(self.window.library_panel._closed)

    def test_independent_browser_save_delegates_to_current_project(self):
        panel = self.window.library_panel
        panel.open_browser_workspace()
        with patch.object(self.window, 'save_named_project') as save:
            panel.browser_save_button.click()
            save.assert_called_once_with()
        self.assertIsNotNone(panel._browser_dialog)

    def test_browser_return_does_not_prompt_or_close_project(self):
        panel = self.window.library_panel
        panel.open_browser_workspace()
        with patch('pgm_app.QMessageBox.exec') as prompt:
            panel.close_browser_workspace()
        prompt.assert_not_called()
        self.assertIsNone(panel._browser_dialog)
        self.assertFalse(panel._closed)

    def test_project_cancel_keeps_independent_browser_and_project_open(self):
        panel = self.window.library_panel
        panel.open_browser_workspace()
        dialog = panel._browser_dialog
        event = self.close_with(QMessageBox.StandardButton.Cancel)
        self.assertFalse(event.isAccepted())
        self.assertIs(panel._browser_dialog, dialog)
        self.assertTrue(dialog.isVisible())

    def test_unrecognized_dialog_result_keeps_project_open(self):
        event=self.close_with(0)
        self.assertFalse(event.isAccepted());self.assertFalse(self.window.library_panel._closed)

    def complete_project_save(self,close_after=False,during_save=None):
        panel=self.window.library_panel
        self.window.project_path='/private/tmp/saved-project.pgmproj'
        production=self.window.project_identity['id']
        def run(title,operation,success):
            if during_save:during_save()
            success((Mock(),production,None,{},Path(self.window.project_path)))
            self.window.library_busy_changed(False)
        with patch.object(panel,'_run',side_effect=run), \
             patch.object(panel,'apply_workflow_context'),patch.object(panel,'_set_data'):
            self.window.save_named_project(close_after=close_after)

    def test_saved_unchanged_project_closes_without_another_prompt(self):
        self.complete_project_save()
        self.assertFalse(self.window.project_dirty())
        event=QCloseEvent()
        with patch('pgm_app.QMessageBox.exec') as prompt:self.window.closeEvent(event)
        prompt.assert_not_called()
        self.assertTrue(event.isAccepted())

    def test_real_edit_after_save_still_prompts_and_cancel_preserves_it(self):
        self.complete_project_save()
        self.window.camera_offsets['B'].setValue(7)
        self.assertTrue(self.window.project_dirty())
        event=self.close_with(QMessageBox.StandardButton.Cancel)
        self.assertFalse(event.isAccepted())
        self.assertEqual(self.window.camera_offsets['B'].value(),7)

    def test_refresh_progress_and_preview_do_not_mark_saved_project_dirty(self):
        self.complete_project_save()
        self.window.library_context['revision']=45
        self.window.library_extra['library_processing']={'p.mov':{'stage':'asr','status':'complete'}}
        self.window.review_frame.setValue(8)
        self.assertFalse(self.window.project_dirty())

    def test_close_prompt_cannot_nest_on_another_close_event(self):
        nested=QCloseEvent()
        def answer():
            self.window.closeEvent(nested)
            return QMessageBox.StandardButton.Cancel
        outer=QCloseEvent()
        with patch('pgm_app.QMessageBox.exec',side_effect=answer) as prompt:
            self.window.closeEvent(outer)
        self.assertEqual(prompt.call_count,1)
        self.assertFalse(nested.isAccepted());self.assertFalse(outer.isAccepted())

    def test_repeated_close_while_saving_never_cancels_or_prompts_again(self):
        panel=self.window.library_panel
        panel.worker=Mock()
        self.window._project_save_in_progress=True
        self.addCleanup(lambda:setattr(panel,'worker',None))
        event=QCloseEvent()
        with patch.object(panel,'cancel_task') as cancel,patch('pgm_app.QMessageBox.exec') as prompt:
            self.window.closeEvent(event)
        self.assertFalse(event.isAccepted());cancel.assert_not_called();prompt.assert_not_called()

    def test_edits_during_save_are_not_marked_saved_or_closed(self):
        with patch.object(self.window,'close') as close:
            self.complete_project_save(close_after=True,during_save=lambda:self.window.camera_offsets['A'].setValue(9))
        self.assertTrue(self.window.project_dirty())
        self.assertFalse(self.window._closing_after_save)
        close.assert_not_called()

    def test_failed_save_keeps_project_dirty_and_allows_retry(self):
        self.complete_project_save()
        self.window.camera_offsets['A'].setValue(4)
        with patch.object(self.window.library_panel,'_run',side_effect=OSError('write failed')):
            with self.assertRaises(OSError):self.window.save_named_project()
        self.assertTrue(self.window.project_dirty())
        self.assertFalse(self.window._project_save_in_progress)
        self.complete_project_save()
        self.assertFalse(self.window.project_dirty())

    def test_scene_save_prompt_cannot_nest(self):
        self.recording_pair()
        proceed=Mock()
        def answer():
            self.assertFalse(self.window.leave_recording(proceed))
            return QMessageBox.StandardButton.Cancel
        with patch('pgm_app.QMessageBox.exec',side_effect=answer) as prompt:
            self.window.leave_recording(proceed)
        self.assertEqual(prompt.call_count,1);proceed.assert_not_called()

    def test_save_does_not_close_before_save_success(self):
        with patch.object(self.window,'save_named_project') as save:
            event=self.close_with(QMessageBox.StandardButton.Save)
        self.assertFalse(event.isAccepted());self.assertFalse(self.window.library_panel._closed)
        self.assertFalse(self.window._closing_after_save)
        save.assert_called_once_with(close_after=True)

    def test_discard_closes_without_calling_save(self):
        with patch.object(self.window,'save_named_project') as save:
            event=self.close_with(QMessageBox.StandardButton.Discard)
        self.assertTrue(event.isAccepted());self.assertTrue(self.window.library_panel._closed)
        save.assert_not_called()

    def test_project_entry_routes_to_named_dialog_not_json_picker(self):
        with patch('project_ui.start_project') as start,patch('pgm_app.QFileDialog.getOpenFileName') as picker:
            self.window._saved_project_checkpoint=self.window.project_checkpoint()
            self.window.project_path='/private/tmp/saved.pgmproj'
            self.window.open_project()
        start.assert_called_once_with(self.window.library_panel,self.window,on_open=self.window.project_opened)
        picker.assert_not_called()

    def test_empty_named_project_can_save_and_continue(self):
        class Client:
            url='http://localhost';token='test-token';actor='DIT'
            def __init__(self,store):self.store=store
            def call(self,method,**params):return self.store.call(method,params,self.actor)
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'library.sqlite3')
            try:
                production=store.create_production('Project 名称');client=Client(store)
                self.window.project_identity=production
                self.window.project_path=str(Path(folder)/'导演剪辑.pgmproj')
                panel=self.window.library_panel
                with patch('project_state.data_dir',return_value=Path(folder)), \
                     patch.object(panel,'ensure_workflow_context',return_value=(client,production['id'])), \
                     patch.object(panel,'_run',side_effect=lambda title,op,success:success(op(lambda *args:None,lambda:False))):
                    self.window.save_named_project()
                    saved=read_project_state(production['id'])
                self.assertEqual(saved['name'],'Project 名称');self.assertEqual(saved['session_id'],'')
                self.assertFalse(self.window._closing_after_save)
                self.assertEqual(panel.production_id(),production['id'])
                self.assertTrue(Path(self.window.project_path).is_file())
            finally:store.close()

    def test_first_save_prompts_for_path_and_cancel_keeps_window_open(self):
        with patch('pgm_app.QFileDialog.getSaveFileName',return_value=('','')) as picker, \
             patch.object(self.window.library_panel,'_run') as run:
            self.window.save_named_project(close_after=True)
        picker.assert_called_once();run.assert_not_called()
        self.assertFalse(self.window._closing_after_save)
        self.assertEqual(self.window.project_path,'')

    def test_existing_path_is_reused_without_prompt(self):
        self.window.project_path='/private/tmp/project.pgmproj'
        with patch('pgm_app.QFileDialog.getSaveFileName') as picker, \
             patch.object(self.window.library_panel,'_run') as run:
            self.window.save_named_project()
        picker.assert_not_called();run.assert_called_once()

    def test_save_as_asks_for_new_path_without_changing_old_until_saved(self):
        self.window.project_path='/private/tmp/old.pgmproj'
        with patch('pgm_app.QFileDialog.getSaveFileName',return_value=('/private/tmp/new','')) as picker, \
             patch.object(self.window.library_panel,'_run') as run:
            self.window.save_named_project(choose_path=True)
        picker.assert_called_once();run.assert_called_once()
        self.assertEqual(self.window.project_path,'/private/tmp/old.pgmproj')

    def recording_pair(self):
        panel=self.window.library_panel
        production=self.window.project_identity['id']
        rows=[dict(id=str(uuid.uuid4()),production_id=production,name=f'day0{number}_通条',revision=1,
                   config=dict(show_id=f'day0{number}_通条',fps=25,sources=[],sound_sources=[],
                               library_media=[],library_record_type='recording'),segments=[])
              for number in (1,2)]
        for row in rows:row['_prepared_editor']=self.window.prepare_library_session(row)
        panel._sessions=rows
        panel.working_session.blockSignals(True)
        panel.working_session.clear()
        for row in rows:panel.working_session.addItem(row['name'],row['id'])
        panel.working_session.blockSignals(False)
        panel._session=rows[0]
        self.window.load_library_session(rows[0],_confirmed=True)
        self.window.camera_offsets['A'].setValue(3)
        self.assertTrue(self.window.recording_dirty())
        return rows

    def choose_scene(self,session):
        panel=self.window.library_panel
        panel.working_session.blockSignals(True)
        panel.working_session.setCurrentIndex(panel.working_session.findData(session['id']))
        panel.working_session.blockSignals(False)
        panel._session=session

    def test_cancel_scene_switch_preserves_current_edits_and_stops_requested_work(self):
        for stage in ('sync','match'):
            with self.subTest(stage=stage):
                first,second=self.recording_pair()
                self.choose_scene(second)
                with patch('pgm_app.QMessageBox.exec',return_value=QMessageBox.StandardButton.Cancel), \
                     patch.object(self.window,'sync_library_recording') as work:
                    self.window.workflow_stage_requested(stage)
                work.assert_not_called()
                self.assertEqual(self.window.library_context['session_id'],first['id'])
                self.assertEqual(self.window.library_panel.selected_session()['id'],first['id'])
                self.assertEqual(self.window.library_panel.working_session.currentData(),first['id'])
                self.assertEqual(self.window.camera_offsets['A'].value(),3)

    def test_requested_work_waits_for_scene_save_then_runs_on_target_only(self):
        first,second=self.recording_pair()
        self.choose_scene(second)
        continuations=[]
        def pending_save(after_save=None):continuations.append(after_save)
        with patch('pgm_app.QMessageBox.exec',return_value=QMessageBox.StandardButton.Save), \
             patch.object(self.window,'save_recording_results',side_effect=pending_save) as save, \
             patch.object(self.window,'sync_library_recording') as work:
            self.window.workflow_stage_requested('match')
            save.assert_called_once()
            work.assert_not_called()
            self.assertEqual(self.window.library_context['session_id'],first['id'])
            self.assertEqual(len(continuations),1)
            # Only successful server save completion may resume the request.
            continuations[0]()
            work.assert_called_once_with(match_only=True)
        self.assertEqual(self.window.library_context['session_id'],second['id'])
        self.assertEqual(self.window.library_panel.selected_session()['id'],second['id'])
        self.assertEqual(self.window.library_panel.workflow_stage,'script')

    def test_same_scene_reload_after_save_uses_latest_session_payload(self):
        first,_second=self.recording_pair()
        stale=copy.deepcopy(first)
        continuations=[]
        with patch('pgm_app.QMessageBox.exec',return_value=QMessageBox.StandardButton.Save), \
             patch.object(self.window,'save_recording_results',side_effect=lambda after_save=None:continuations.append(after_save)):
            self.window.load_library_session(stale)
        latest=copy.deepcopy(first)
        latest['revision']=2
        latest['config']['camera_offsets']={'A':3}
        latest['_prepared_editor']=self.window.prepare_library_session(latest)
        self.window.library_panel._sessions=[latest]
        continuations[0]()
        self.assertEqual(self.window.camera_offsets['A'].value(),3)
        self.assertEqual(self.window.library_context['revision'],2)
        self.assertFalse(self.window.recording_dirty())

    def test_cancel_project_switch_does_not_open_dialog_or_clear_recording(self):
        first,_second=self.recording_pair()
        with patch('pgm_app.QMessageBox.exec',return_value=QMessageBox.StandardButton.Cancel), \
             patch('project_ui.start_project') as start:
            self.window.open_project()
        start.assert_not_called()
        self.assertEqual(self.window.library_context['session_id'],first['id'])
        self.assertEqual(self.window.camera_offsets['A'].value(),3)

    def test_save_then_close_waits_until_result_worker_is_released(self):
        panel=self.window.library_panel
        self.window.project_path='/private/tmp/close-project.pgmproj'
        production=self.window.project_identity['id']
        callbacks=[]
        def run(title,operation,success):
            panel.worker=object()  # The result signal arrives before finished.
            success((Mock(),production,None,{},Path(self.window.project_path)))
        self.addCleanup(lambda:setattr(panel,'worker',None))
        with patch.object(panel,'_run',side_effect=run), \
             patch.object(panel,'apply_workflow_context'),patch.object(panel,'_set_data'), \
             patch.object(self.window,'close') as close, \
             patch('project_ui.QTimer.singleShot',side_effect=lambda delay,callback:callbacks.append(callback)):
            self.window.save_named_project(close_after=True)
            close.assert_not_called()
            self.assertTrue(self.window._closing_after_save)
            self.assertEqual(len(callbacks),1)
            panel.worker=None
            callbacks.pop()()
            close.assert_called_once()

    def test_initial_project_cancel_waits_for_entry_worker_and_closes_window(self):
        from project_ui import start_project
        panel=self.window.library_panel
        self.window.project_identity=None
        callbacks=[]
        dialog=Mock()
        dialog.exec.return_value=QDialog.DialogCode.Rejected
        def run(title,operation,success):
            panel.worker=object()
            success((Mock(),[],None))
        self.addCleanup(lambda:setattr(panel,'worker',None))
        with patch.object(panel,'_run',side_effect=run), \
             patch('project_ui.ProjectEntryDialog',return_value=dialog) as entry, \
             patch('project_ui.QTimer.singleShot',side_effect=lambda delay,callback:callbacks.append(callback)):
            start_project(panel,parent=self.window)
            entry.assert_not_called()
            self.assertFalse(panel._closed)
            panel.worker=None
            callbacks.pop()()
            dialog.exec.assert_called_once()
            self.assertTrue(panel._closed)

    def test_failed_save_as_keeps_old_project_path_and_does_not_close(self):
        panel=self.window.library_panel
        self.window.project_path='/private/tmp/old-project.pgmproj'
        production=self.window.project_identity['id']
        def run(title,operation,success):success(operation(lambda *_args:None,lambda:False))
        with patch('pgm_app.QFileDialog.getSaveFileName',return_value=('/private/tmp/new-project.pgmproj','')), \
             patch.object(panel,'_run',side_effect=run), \
             patch.object(panel,'ensure_workflow_context',return_value=(Mock(),production)), \
             patch('project_state.write_project_state',side_effect=OSError('save target unavailable')), \
             patch.object(self.window,'close') as close:
            with self.assertRaisesRegex(OSError,'unavailable'):
                self.window.save_named_project(close_after=True,choose_path=True)
        self.assertEqual(self.window.project_path,'/private/tmp/old-project.pgmproj')
        self.assertFalse(self.window._closing_after_save)
        close.assert_not_called()

    def test_local_project_save_keeps_matches_and_tracks_subsequent_edits(self):
        import os
        from local_project import LocalProjectClient
        from project_state import read_project_file
        panel=self.window.library_panel
        client=panel.client
        production=client.call('create_production',name='完整项目')
        session=client.call('save_session',production_id=production['id'],name='day01_通条',config={'future':'keep'})
        self.window.project_identity=production
        panel.production.addItem(production['name'],production['id'])
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,PGM_LIBRARY_HOME=folder), \
             patch.object(panel,'_run',side_effect=lambda title,op,success:success(op(lambda *_:None,lambda:False))):
            self.window.project_path=str(Path(folder)/'complete.pgmproj')
            self.window.save_named_project()
            self.assertFalse(self.window.project_dirty())
            state=read_project_file(self.window.project_path)
            restored=LocalProjectClient.from_project(state['catalogue'])
            self.assertEqual(restored.call('get_session',session_id=session['id'])['config']['future'],'keep')
            restored.close()
            client.call('put_script',production_id=production['id'],title='后来新增',lines=[{'text':'Hello，台词'}])
            self.assertTrue(self.window.project_dirty())
            with patch('project_ui.start_project') as start,patch('pgm_app.QMessageBox.exec',return_value=QMessageBox.StandardButton.Cancel):
                self.window.open_project()
            start.assert_not_called()
            self.window.library_busy_changed(False)
            self.window.save_named_project()
            self.assertFalse(self.window.project_dirty())


if __name__=='__main__':unittest.main()
