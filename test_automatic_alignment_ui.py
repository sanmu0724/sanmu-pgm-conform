"""Auto alignment runs in a worker; review states remain independent."""
import copy
import time
import unittest
from unittest.mock import patch

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDialog

from manual_timeline_ui import ManualTimelineDialog
from library_ui import LibraryPanel
from test_manual_timeline_ui import fixture
from test_baseline_map_ui import FakePreview
import test_library_ui as fixtures


def setup_data():
    rows,layout,cut=fixture()
    ids=[row['id'] for row in rows if row['session_id']=='alt']
    from manual_timeline import confirm_alignment
    layout=confirm_alignment(layout,[ids[1]],rows,'base')
    layout['items'][ids[0]].update(alignment_reviewed=False,alignment_origin='automatic')
    data=dict(scripts=[dict(id='script',lines=[dict(id=f'l{i}',order=i,text=f'台词{i}') for i in range(1,11)])])
    return data,rows,layout,cut,ids


class AutomaticDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.data,self.rows,self.layout,self.cut,self.ids=setup_data()
        self.patch=patch('library_ui.InlinePreview',FakePreview);self.patch.start()
        self.dialog=ManualTimelineDialog(self.data,self.rows,self.layout,self.cut)
        self.dialog.mode.setCurrentIndex(1)

    def tearDown(self):
        self.dialog.set_automatic_busy(False);self.dialog._allow_close=True;self.dialog.close();self.patch.stop()

    def test_blue_actual_review_is_independent_of_green_organizer(self):
        self.assertTrue(self.dialog.layout_data['items'][self.ids[0]]['organize_reviewed'])
        self.assertFalse(self.dialog.canvas._entries[self.ids[0]]['reviewed'])
        self.dialog.mode.setCurrentIndex(0)
        self.assertTrue(self.dialog.canvas._entries[self.ids[0]]['reviewed'])

    def test_confirm_selected_turns_green_without_moving_or_changing_organizer(self):
        before=copy.deepcopy(self.dialog.layout_data['items'][self.ids[0]])
        self.dialog.track_selection([self.ids[0]]);self.dialog.confirm_selected_alignment()
        item=self.dialog.layout_data['items'][self.ids[0]]
        self.assertTrue(item['alignment_reviewed']);self.assertTrue(self.dialog.canvas._entries[self.ids[0]]['reviewed'])
        for key in ('start_seconds','lane','organize_first_line','organize_last_line','organize_reviewed'):
            self.assertEqual(item[key],before[key])

    def test_manual_apply_requires_explicit_confirmation_and_preserves_source(self):
        source=copy.deepcopy(self.rows)
        self.dialog.track_selection([self.ids[0]]);self.dialog.position.setValue(42);self.dialog.apply_position()
        item=self.dialog.layout_data['items'][self.ids[0]]
        self.assertEqual(item['start_seconds'],42.)
        self.assertEqual(item['alignment_origin'],'manual');self.assertFalse(item['alignment_reviewed'])
        self.dialog.confirm_selected_alignment()
        self.assertTrue(self.dialog.layout_data['items'][self.ids[0]]['alignment_reviewed'])
        self.assertEqual(source,self.rows)

    def test_auto_request_carries_dirty_copy_model_and_default_safe_replace(self):
        requests=[];self.dialog.automatic_alignment_requested.connect(requests.append)
        self.dialog.track_selection([self.ids[0]]);self.dialog.position.setValue(44);self.dialog.apply_position()
        with patch('baseline_script_map.build_baseline_map',side_effect=AssertionError('GUI ASR mapping')):
            self.dialog.request_automatic()
        self.assertEqual(len(requests),1)
        payload=requests[0]
        self.assertEqual(payload['operation'],'prepare_automatic_alignment')
        self.assertEqual(payload['model'],'small');self.assertFalse(payload['replace_automatic'])
        self.assertEqual(payload['layout']['items'][self.ids[0]]['start_seconds'],44.)
        payload['layout']['items'].clear()
        self.assertTrue(self.dialog.layout_data['items'])
        self.assertTrue(self.dialog._auto_busy);self.assertFalse(self.dialog.canvas.isEnabled())
        self.dialog.preview.pause.assert_called()

    def test_medium_and_replacement_are_explicit_and_repeated_busy_click_is_ignored(self):
        requests=[];self.dialog.automatic_alignment_requested.connect(requests.append)
        self.dialog.alignment_model.setCurrentIndex(1);self.dialog.replace_automatic.setChecked(True)
        self.dialog.request_automatic();self.dialog.request_automatic()
        self.assertEqual(len(requests),1)
        self.assertEqual(requests[0]['model'],'medium');self.assertTrue(requests[0]['replace_automatic'])

    def test_progress_cancel_and_failure_are_visible_and_preserve_edits(self):
        before=copy.deepcopy(self.dialog.layout_data);cancel=[]
        self.dialog.automatic_alignment_cancelled.connect(lambda:cancel.append(True))
        self.dialog.set_automatic_busy(True);self.dialog.automatic_progress(57,'正在识别基准台词')
        self.assertEqual(self.dialog.auto_progress.value(),57)
        self.assertIn('正在识别',self.dialog.alignment_note.text())
        self.dialog.cancel_automatic();self.assertEqual(cancel,[True]);self.assertFalse(self.dialog.cancel_auto_button.isEnabled())
        self.dialog.automatic_failed('已取消')
        self.assertFalse(self.dialog._auto_busy);self.assertTrue(self.dialog.canvas.isEnabled())
        self.assertEqual(before,self.dialog.layout_data);self.assertEqual(self.dialog.error.text(),'已取消')

    def test_busy_close_requests_cancel_without_destroying_editor(self):
        cancelled=[];self.dialog.automatic_alignment_cancelled.connect(lambda:cancelled.append(True))
        self.dialog.set_automatic_busy(True);self.dialog.reject()
        self.assertEqual(cancelled,[True]);self.assertFalse(self.dialog._allow_close)

    def test_worker_result_preserves_selection_preview_and_summary_without_gui_recognition(self):
        self.dialog.track_selection([self.ids[0]])
        loads=list(self.dialog.preview.loads)
        self.dialog.preview.toggle_playback.reset_mock()
        changed=copy.deepcopy(self.dialog.layout_data);changed['items'][self.ids[0]]['start_seconds']=8.
        report=dict(summary=dict(proposed_count=1,preserved_count=1),unresolved=[dict(take_id=self.ids[2],filename='alt2.mov',reason='首句未定位')])
        with patch('baseline_script_map.build_baseline_map',side_effect=AssertionError('GUI ASR')):
            self.dialog.apply_automatic_alignment(dict(manual_timeline=changed,state=dict(revision=8),automatic_alignment=report))
        self.assertEqual(self.dialog._selected,[self.ids[0]])
        self.assertEqual(self.dialog.preview.loads,loads)
        self.assertEqual(self.dialog.position.value(),8.)
        self.assertIn('无法定位 1',self.dialog.alignment_note.text())
        self.assertIn('首句未定位',self.dialog.alignment_note.toolTip())
        model=self.dialog.pool_model;index=model.index(model.by_id[self.ids[2]],0)
        self.assertIn('首句未定位',model.data(index,Qt.ItemDataRole.ToolTipRole))
        self.dialog.preview.toggle_playback.assert_not_called()

    def test_blue_alternatives_can_export_with_visible_unreviewed_reminder(self):
        self.assertIn('待核实 1',self.dialog.alignment_note.text())
        self.dialog.save_and_export('combined')
        self.assertEqual(self.dialog.result(),QDialog.DialogCode.Accepted)
        self.assertEqual(self.dialog.export_mode,'combined')
        self.assertFalse(self.dialog.layout_result()['items'][self.ids[0]]['alignment_reviewed'])

    def test_mapping_review_entry_is_removed_and_only_timeline_adjustment_remains(self):
        requests=[];self.dialog.automatic_alignment_requested.connect(requests.append)
        self.dialog.request_automatic('prepare_automatic_map')
        self.assertFalse(requests)
        self.assertFalse(self.dialog._auto_busy)
        self.assertFalse(hasattr(self.dialog,'review_map_button'))
        self.assertIn('时间轴',self.dialog.error.text())

    def test_mapping_only_result_keeps_blue_and_reminds_explicit_rearrangement(self):
        self.dialog.apply_automatic_alignment(dict(manual_timeline=self.layout,
            automatic_alignment=dict(summary=dict(proposed_count=3),unresolved=[]),
            alignment_report=dict(placed_count=0,mapping_only=True,unresolved=[])))
        self.assertIn('原位置保留',self.dialog.alignment_note.text())
        self.assertNotIn('自动初排 3',self.dialog.alignment_note.text())
        self.assertFalse(self.dialog.layout_data['items'][self.ids[0]]['alignment_reviewed'])
        self.assertNotIn('evidence_data',self.dialog._alignment_report)

    def test_baseline_speech_failures_are_visible_separate_from_alternative_unresolved(self):
        self.dialog.apply_automatic_alignment(dict(manual_timeline=self.layout,alignment_report=dict(
            placed_count=0,preserved_count=1,unresolved=[dict(filename='alt2.mov',reason='台词位置不明确')],
            speech=dict(recognized=2,reused=3,data={'large_asr':'must not retain'},failures=[
                dict(filename='base0.mov',pgm_path='/qa/base0.mov',reason='离线模型尚未安装'),
                dict(filename='base1.mov',pgm_path='/qa/base1.mov',reason='没有检测到台词')]))))
        self.assertIn('无法定位 1 条',self.dialog.alignment_note.text())
        self.assertIn('基准转写待处理 2 条',self.dialog.alignment_note.text())
        self.assertIn('离线模型尚未安装',self.dialog.alignment_note.toolTip())
        self.assertIn('没有检测到台词',self.dialog.alignment_note.toolTip())
        self.assertEqual(self.dialog._alignment_report['speech_counts'],dict(recognized=2,reused=3))
        self.assertNotIn('data',self.dialog._alignment_report)
        self.assertNotIn('speech',self.dialog._alignment_report['summary'])


class AutomaticPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.fixture=fixtures.LibraryPanelTests();self.fixture.setUp();self.panel=self.fixture.panel
        self.data,self.rows,self.layout,self.cut,self.ids=setup_data()
        self.patch=patch('library_ui.InlinePreview',FakePreview);self.patch.start()
        self.dialog=ManualTimelineDialog(self.data,self.rows,self.layout,self.cut,self.panel)
        self.dialog.mode.setCurrentIndex(1);self.panel._manual_dialog=self.dialog
        self.panel.apply_reference_state(dict(reference_session_id='base',revision=7,manual_timeline=self.layout),refresh_matches=False)
        self.requests=[];self.panel.manual_timeline_requested.connect(self.requests.append)

    def tearDown(self):
        self.panel._manual_dialog=None;self.dialog.set_automatic_busy(False);self.dialog._allow_close=True;self.dialog.close();self.patch.stop();self.fixture.tearDown()

    def result(self):
        return dict(data=self.data,state=dict(reference_session_id='base',revision=8,manual_timeline=self.layout),
                    manual_inventory=self.rows,manual_timeline=self.layout,manual_cut=self.cut,
                    automatic_alignment=dict(summary=dict(proposed_count=0,preserved_count=2),unresolved=[]))

    def test_request_routes_dirty_layout_with_expected_revision(self):
        self.panel._manual_automatic_requested(dict(operation='prepare_automatic_alignment',layout=self.layout,model='medium',replace_automatic=True))
        self.assertEqual(self.requests[0]['expected_revision'],7)
        self.assertEqual(self.requests[0]['model'],'medium');self.assertTrue(self.requests[0]['replace_automatic'])
        self.assertEqual(self.requests[0]['layout'],self.layout)

    def test_host_progress_and_failure_reach_active_editor_without_old_modal(self):
        self.dialog.set_automatic_busy(True)
        self.panel._progress(70,'对齐处理中')
        self.assertEqual(self.dialog.auto_progress.value(),70)
        with patch('library_ui.QMessageBox.warning',side_effect=AssertionError('old modal')):self.panel._failed('已取消')
        self.assertFalse(self.dialog._auto_busy);self.assertEqual(self.dialog.error.text(),'已取消')

    def test_worker_result_applies_new_revision_without_rebuilding_catalogue(self):
        with patch.object(self.panel,'_set_data',side_effect=AssertionError('full catalogue rebuild')):
            self.panel.present_automatic_alignment(self.result())
        self.assertEqual(self.panel.reference_state['revision'],8)
        self.assertIn('蓝色对齐待核实 1',self.panel.reference_label.text())
        self.assertIs(self.panel._manual_prepared['manual_inventory'],self.rows)

    def test_mapping_editor_save_preserves_layout_and_emits_atomic_mapping_request(self):
        result=dict(self.result(),baseline_script_map=dict(version=1),baseline_cut=self.cut)
        with patch('baseline_map_ui.BaselineMapDialog') as dialog:
            dialog.return_value.exec.return_value=QDialog.DialogCode.Accepted
            dialog.return_value.rebuild_requested=False;dialog.return_value.mapping_result.return_value=dict(version=1,segments=[])
            self.panel.present_automatic_map(result)
        self.assertEqual(self.requests[0]['operation'],'save_automatic_map')
        self.assertEqual(self.requests[0]['expected_revision'],8)
        self.assertEqual(self.requests[0]['layout'],self.dialog.layout_data)
        self.assertFalse(self.requests[0]['layout']['items'][self.ids[0]]['alignment_reviewed'])

    def test_mapping_cancel_keeps_editor_available_and_does_not_emit_save(self):
        result=dict(self.result(),baseline_script_map=dict(version=1),baseline_cut=self.cut)
        with patch('baseline_map_ui.BaselineMapDialog') as dialog:
            dialog.return_value.exec.return_value=QDialog.DialogCode.Rejected
            self.panel.present_automatic_map(result)
        self.assertEqual(self.requests,[]);self.assertFalse(self.dialog._auto_busy)

    def test_standalone_mapping_save_uses_mapping_first_layout_keyword_signature(self):
        self.panel.manual_timeline_requested.disconnect()
        mapping=dict(version=1,segments=[])
        def run(_title,task,_success):return task(lambda *_:None,lambda:False)
        with patch('reference_workflow.ReferenceWorkflow',autospec=True) as workflow,patch.object(self.panel,'_run',side_effect=run):
            self.panel._reference_request('save_automatic_map',mapping=mapping,layout=self.layout)
        call=workflow.return_value.save_automatic_map.call_args
        self.assertEqual(call.args,(mapping,))
        self.assertEqual(call.kwargs['layout'],self.layout)
        self.assertEqual(call.kwargs['expected_revision'],7)

    def _enable_real_worker(self):
        self.panel.manual_timeline_requested.disconnect()
        self.panel._run=LibraryPanel._run.__get__(self.panel,LibraryPanel)
        self.dialog.automatic_alignment_requested.connect(self.panel._manual_automatic_requested)
        self.dialog.automatic_alignment_cancelled.connect(self.panel.cancel_task)

    def _wait_worker(self):
        deadline=time.monotonic()+3
        while time.monotonic()<deadline and (self.panel.worker is not None or self.dialog._auto_busy):
            self.app.processEvents();QTest.qWait(5)
        if self.panel.worker is not None:
            self.panel.worker.requestInterruption();self.panel.worker.wait(1000);self.app.processEvents()
        self.assertIsNone(self.panel.worker);self.assertFalse(self.dialog._auto_busy)

    def test_real_qthread_result_returns_to_same_editor_and_updates_revision(self):
        self._enable_real_worker()
        def align(_layout,**options):
            options['progress'](45,'测试后台映射');time.sleep(.025)
            return self.result()
        with patch('reference_workflow.ReferenceWorkflow',autospec=True) as workflow:
            workflow.return_value.prepare_automatic_alignment.side_effect=align
            self.dialog.request_automatic();self.assertTrue(self.dialog._auto_busy)
            self._wait_worker()
        self.assertIs(self.panel._manual_dialog,self.dialog)
        self.assertEqual(self.panel.reference_state['revision'],8)

    def test_real_qthread_cancel_unblocks_editor_without_losing_layout(self):
        self._enable_real_worker();before=copy.deepcopy(self.dialog.layout_data)
        def align(_layout,**options):
            while not options['cancelled']():time.sleep(.005)
            raise ValueError('已取消测试')
        with patch('reference_workflow.ReferenceWorkflow',autospec=True) as workflow:
            workflow.return_value.prepare_automatic_alignment.side_effect=align
            self.dialog.request_automatic();self.dialog.cancel_automatic();self._wait_worker()
        self.assertEqual(before,self.dialog.layout_data)
        self.assertIn('已取消',self.dialog.error.text())


if __name__=='__main__':unittest.main()
