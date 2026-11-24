"""Manual editing is metadata-only, virtualised, and independent of scripts."""
import copy
import json
import time
import unittest
from unittest.mock import Mock,patch
from PyQt6.QtCore import QPoint,Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication,QDialog,QMessageBox,QListView
from manual_timeline import take_id,reconcile_layout,place_items
from manual_timeline_ui import ManualTimelineDialog,ManualTimelineCanvas,TakePoolModel,MIME
from test_baseline_map_ui import FakePreview
import test_library_ui as fixtures


def fixture(count=3):
    rows=[]
    for sid,total in (('base',2),('alt',count)):
        for n in range(total):
            path=f'/qa/{sid}{n}.mov';identity=take_id(sid,path)
            rows.append(dict(id=identity,take_id=identity,session_id=sid,session_name=sid,
                pgm_path=path,filename=f'{sid}{n}.mov',duration_seconds=10.,fps=25.,frames=250,
                organize_first_line=n*2+1 if sid=='alt' else None,
                organize_last_line=n*2+2 if sid=='alt' else None,
                organize_pool='track' if sid=='alt' else 'unmatched',organize_reviewed=True,
                baseline_start_seconds=n*10 if sid=='base' else 0,ready=True,
                record_frame_in=n*250 if sid=='base' else 0,record_frame_out_exclusive=(n+1)*250 if sid=='base' else 250))
    layout=reconcile_layout({},rows,'base')
    ids=[row['id'] for row in rows if row['session_id']=='alt']
    layout=place_items(layout,[ids[0]],5,1,rows,'base')
    if count>1:layout=place_items(layout,[ids[1]],20,1,rows,'base')
    return rows,layout,dict(fps=25,duration=500)


class ManualCanvasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.rows,self.layout,self.cut=fixture()
        self.ids=[row['id'] for row in self.rows if row['session_id']=='alt']
        self.canvas=ManualTimelineCanvas(self.rows,self.layout,'base',self.cut)
        self.canvas.resize(1000,390);self.canvas.set_mode('timeline');self.canvas.show();self.app.processEvents()
    def tearDown(self):self.canvas.close()
    def centre(self,identity):return self.canvas.clip_rect(self.canvas._entries[identity]).center().toPoint()
    def drag(self,first,last):
        QTest.mousePress(self.canvas.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,first)
        QTest.mouseMove(self.canvas.viewport(),last)
        QTest.mouseRelease(self.canvas.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,last)
        self.app.processEvents()

    def test_timeline_contains_locked_complete_baseline_and_only_placed_alternatives(self):
        base=[row for row in self.canvas._entries.values() if row['baseline']]
        self.assertEqual([(row['start'],row['end'],row['lane']) for row in base],[(0.,10.,0),(10.,20.,0)])
        self.assertIn(self.ids[0],self.canvas._entries)
        self.assertNotIn(self.ids[2],self.canvas._entries)

    def test_click_selection_emits_once_on_release_and_never_decodes_while_dragging(self):
        selected=[];self.canvas.selection_changed.connect(selected.append)
        first=self.centre(self.ids[0])
        QTest.mousePress(self.canvas.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,first)
        self.assertEqual(selected,[])
        QTest.mouseRelease(self.canvas.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,first)
        self.assertEqual(selected,[[self.ids[0]]])

    def test_group_drag_preserves_relative_positions_and_source_metadata(self):
        before=copy.deepcopy(self.rows)
        self.canvas.selection={self.ids[0],self.ids[1]}
        first=self.centre(self.ids[0]);last=first+QPoint(80,0)
        changed=[];self.canvas.layout_changed.connect(changed.append)
        with patch('subprocess.run',side_effect=AssertionError('No decoder')),patch('builtins.open',side_effect=AssertionError('No file IO')):
            self.drag(first,last)
        self.assertEqual(len(changed),1)
        items=changed[0]['items']
        self.assertEqual(items[self.ids[1]]['start_seconds']-items[self.ids[0]]['start_seconds'],15.)
        self.assertEqual(items[self.ids[0]]['start_seconds'],15.)
        self.assertEqual(self.rows,before)

    def test_locked_baseline_cannot_be_dragged_or_reordered(self):
        identity=next(row['id'] for row in self.rows if row['session_id']=='base')
        changed=[];self.canvas.layout_changed.connect(changed.append)
        first=self.centre(identity);self.drag(first,first+QPoint(100,-60))
        self.assertEqual(changed,[])
        point=QPoint(20,first.y());self.drag(point,point+QPoint(0,-60))
        self.assertEqual(changed,[])

    def test_rubberband_selection_covers_multiple_clips(self):
        a=self.canvas.clip_rect(self.canvas._entries[self.ids[0]])
        b=self.canvas.clip_rect(self.canvas._entries[self.ids[1]])
        first=QPoint(round(a.left()-5),round(a.top()-2))
        last=QPoint(round(b.right()+5),round(b.bottom()+2))
        self.drag(first,last)
        self.assertEqual(self.canvas.selection,set(self.ids[:2]))

    def test_command_multi_selection(self):
        QTest.mouseClick(self.canvas.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,self.centre(self.ids[0]))
        QTest.mouseClick(self.canvas.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.MetaModifier,self.centre(self.ids[1]))
        self.assertEqual(self.canvas.selection,set(self.ids[:2]))

    def test_lane_header_reorder_preserves_clip_target_times(self):
        self.canvas.set_layout(place_items(self.layout,[self.ids[2]],2,2,self.rows,'base'))
        before=copy.deepcopy(self.canvas.layout_data)
        first=QPoint(20,round(self.canvas.lane_y(1)+20));last=QPoint(20,round(self.canvas.lane_y(2)+20))
        self.drag(first,last)
        self.assertEqual(self.canvas.layout_data['items'][self.ids[0]]['lane'],2)
        self.assertEqual(self.canvas.layout_data['items'][self.ids[2]]['lane'],1)
        for identity in self.ids:self.assertEqual(self.canvas.layout_data['items'][identity]['start_seconds'],before['items'][identity]['start_seconds'])

    def test_virtual_large_inventory_paints_visible_rows_only(self):
        rows,layout,cut=fixture(5000)
        # Deliberately stress thousands of explicit vertical lanes; normal
        # non-overlapping script ranges are now compacted onto free lanes.
        for lane,item in enumerate(layout['items'].values(),1):item['organize_lane']=lane
        canvas=ManualTimelineCanvas(rows,layout,'base',cut)
        canvas.resize(1000,390);canvas.show();self.app.processEvents()
        try:
            self.assertEqual(len(canvas._entries),5000)
            self.assertLess(len(list(canvas.visible_entries())),10)
            self.assertGreater(canvas.verticalScrollBar().maximum(),100000)
            self.assertEqual(len(canvas.findChildren(QListView)),0)
        finally:canvas.close()


class ManualDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.rows,self.layout,self.cut=fixture()
        self.ids=[row['id'] for row in self.rows if row['session_id']=='alt']
        self.patch=patch('library_ui.InlinePreview',FakePreview);self.patch.start()
        self.dialog=ManualTimelineDialog(dict(scripts=[],transcripts=[]),self.rows,self.layout,self.cut)
    def tearDown(self):self.dialog._allow_close=True;self.dialog.close();self.patch.stop()

    def test_selecting_track_or_pool_positions_paused_preview(self):
        self.dialog.track_selection([self.ids[0]])
        self.assertEqual(self.dialog.preview.loads[-1],('/qa/alt0.mov',0,10.,False))
        self.dialog.preview.pause.assert_called()
        self.dialog.preview.toggle_playback.assert_not_called()
        self.dialog.toggle_playback();self.dialog.preview.player.play.assert_called_once()

    def test_no_script_or_asr_is_required_to_place_and_save(self):
        self.dialog.mode.setCurrentIndex(1)
        with patch('baseline_script_map.build_baseline_map',side_effect=AssertionError('no ASR mapping')):
            self.dialog.place_ids([self.ids[2]],42,2)
            self.dialog.save_layout()
        self.assertEqual(self.dialog.result(),QDialog.DialogCode.Accepted)
        self.assertEqual(self.dialog.layout_result()['items'][self.ids[2]]['start_seconds'],42.)
        self.assertIsNone(self.layout['items'][self.ids[2]]['start_seconds'])

    def test_remove_alignment_returns_take_to_pool_without_deleting_data(self):
        self.dialog.mode.setCurrentIndex(1)
        self.dialog.track_selection([self.ids[0]])
        self.dialog.remove_selected()
        self.assertIsNone(self.dialog.layout_data['items'][self.ids[0]]['start_seconds'])
        self.assertIn(self.ids[0],self.dialog.pool_model.by_id)
        self.assertIn(self.ids[0],self.dialog.layout_data['items'])
        self.assertEqual(self.dialog.layout_data['items'][self.ids[0]]['pgm_path'],'/qa/alt0.mov')

    def test_numeric_group_position_preserves_offsets_and_moves_conflicts_to_other_lanes(self):
        self.dialog.mode.setCurrentIndex(1)
        self.dialog.track_selection(self.ids[:2])
        self.dialog.position.setValue(30);self.dialog.lane.setValue(2);self.dialog.apply_position()
        items=self.dialog.layout_data['items']
        self.assertEqual(items[self.ids[0]]['start_seconds'],30.)
        self.assertEqual(items[self.ids[1]]['start_seconds'],45.)

    def test_dirty_close_cancel_then_discard_keeps_original_unchanged(self):
        self.dialog.mode.setCurrentIndex(1);self.dialog.place_ids([self.ids[2]],40,1)
        with patch('manual_timeline_ui.QMessageBox.question',return_value=QMessageBox.StandardButton.Cancel):self.dialog.reject()
        self.assertFalse(self.dialog._allow_close)
        with patch('manual_timeline_ui.QMessageBox.question',return_value=QMessageBox.StandardButton.Discard):self.dialog.reject()
        self.assertEqual(self.dialog.result(),QDialog.DialogCode.Rejected)
        self.assertIsNone(self.layout['items'][self.ids[2]]['start_seconds'])

    def test_dirty_close_save_and_three_export_modes(self):
        for mode in ('baseline','alternatives','combined'):
            self.dialog.save_and_export(mode)
            self.assertEqual(self.dialog.export_mode,mode)
            self.assertEqual(self.dialog.result(),QDialog.DialogCode.Accepted)
        result=self.dialog.layout_result();result['items'].clear()
        self.assertTrue(self.dialog.layout_result()['items'])

    def test_space_types_in_search_but_toggles_playback_once_on_canvas(self):
        self.dialog.show();self.app.processEvents()
        self.dialog.track_selection([self.ids[0]])
        self.dialog.search.setFocus();QTest.keyClicks(self.dialog.search,'day 01')
        self.assertEqual(self.dialog.search.text(),'day 01')
        self.dialog.preview.player.play.assert_not_called()
        self.dialog.canvas.setFocus();QTest.keyClick(self.dialog.canvas,Qt.Key.Key_Space)
        self.dialog.preview.player.play.assert_called_once()

    def test_ruler_seek_uses_actual_baseline_record_geometry_and_pauses(self):
        self.dialog.mode.setCurrentIndex(1)
        self.dialog.preview_take(self.ids[0])
        alternative_loads=list(self.dialog.preview.loads)
        baseline=next(row['id'] for row in self.rows if row['session_id']=='base')
        self.dialog.track_selection([baseline],autoplay=False)
        self.dialog.seek_timeline(14.5)
        self.assertEqual(self.dialog.baseline_preview.loads[-1],('/qa/base1.mov',0.,10.,False))
        self.dialog.baseline_preview.player.setPosition.assert_called_with(4500)
        self.assertEqual(self.dialog.preview.loads,alternative_loads)
        self.assertEqual(self.dialog.canvas.playhead_seconds,14.5)
        self.assertEqual(self.dialog.position.value(),14.5)
        self.dialog.preview.toggle_playback.assert_not_called()

    def test_preview_head_updates_target_cursor_without_changing_layout_or_source(self):
        self.dialog.mode.setCurrentIndex(1)
        before=copy.deepcopy(self.dialog.layout_data)
        self.dialog.preview_take(self.ids[0])
        self.dialog.preview_position_changed(2500)
        self.assertEqual(self.dialog.canvas.playhead_seconds,7.5)
        self.assertEqual(self.dialog.layout_data,before)

    def test_legacy_cached_metadata_warning_is_visible_without_probing(self):
        row=self.dialog._inventory[self.ids[2]]
        row['metadata_warning']='切点位置将在导出时核对'
        self.dialog.mode.setCurrentIndex(1)
        self.dialog.preview_take(self.ids[2])
        self.assertIn('切点位置',self.dialog.error.text())
        model=self.dialog.pool_model
        index=model.index(model.by_id[self.ids[2]],0)
        self.assertIn('切点位置',model.data(index,Qt.ItemDataRole.ToolTipRole))

    def test_pool_model_drag_payload_and_search_are_metadata_only(self):
        self.dialog.mode.setCurrentIndex(1)
        model=self.dialog.pool_model
        self.assertIn(self.ids[2],model.by_id)
        index=model.index(model.by_id[self.ids[2]],0)
        self.assertEqual(json.loads(bytes(model.mimeData([index]).data(MIME))),[self.ids[2]])
        self.dialog.search.setText('missing-file')
        self.assertEqual(model.rowCount(),0)


class ManualPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.fixture=fixtures.LibraryPanelTests();self.fixture.setUp();self.panel=self.fixture.panel
        self.rows,self.layout,self.cut=fixture()
        self.panel.apply_reference_state(dict(reference_session_id='base',revision=7,manual_timeline=self.layout))
        self.requests=[];self.panel.manual_timeline_requested.connect(self.requests.append)
        self.exports=[];self.panel.reference_export_requested.connect(self.exports.append)
    def tearDown(self):self.fixture.tearDown()
    def result(self):return dict(data={},state=dict(self.panel.reference_state,revision=8),manual_inventory=self.rows,manual_timeline=self.layout,manual_cut=self.cut)

    def test_open_manual_editor_dispatch_has_no_script_or_mapping_prerequisites(self):
        self.panel.open_manual_timeline()
        self.assertEqual(self.requests[0]['operation'],'prepare_manual_timeline')
        self.assertEqual(self.requests[0]['expected_revision'],7)

    def test_present_editor_saves_before_export_with_old_revision_and_layout(self):
        with patch('manual_timeline_ui.ManualTimelineDialog') as dialog:
            dialog.return_value.exec.return_value=QDialog.DialogCode.Accepted
            dialog.return_value.layout_result.return_value=self.layout
            dialog.return_value.export_mode='combined'
            self.panel.present_manual_timeline(self.result())
        self.assertEqual(self.exports,[])
        self.assertEqual(self.requests[0]['operation'],'save_manual_timeline')
        self.assertEqual(self.requests[0]['expected_revision'],8)
        self.assertEqual(self.requests[0]['after_export'],'combined')

    def test_save_success_uses_new_revision_and_exports_manual_positions_without_anchor_picker(self):
        with patch('library_ui.QFileDialog.getExistingDirectory',return_value='/qa/exports'), \
             patch.object(self.panel,'_choose_baseline_candidate',side_effect=AssertionError('old picker')), \
             patch.object(self.panel,'_baseline_mapping_ready',side_effect=AssertionError('old mapping')):
            self.panel.manual_timeline_saved(self.result(),after_export='combined')
        self.assertEqual(len(self.exports),1)
        self.assertTrue(self.exports[0]['manual'])
        self.assertEqual(self.exports[0]['expected_revision'],8)
        self.assertTrue(self.exports[0]['include_baseline'])
        self.assertTrue(self.exports[0]['include_alternatives'])

    def test_three_export_flags_have_no_asr_or_mapping_requirements(self):
        with patch('library_ui.QFileDialog.getExistingDirectory',return_value='/qa/exports'):
            for alt,base in ((False,True),(True,False),(True,True)):
                self.panel.export_reference(alt,base,manual=True)
        self.assertEqual([(row['include_alternatives'],row['include_baseline']) for row in self.exports],[(False,True),(True,False),(True,True)])
        self.assertTrue(all(row['manual'] for row in self.exports))

    def test_state_only_save_keeps_cached_inventory_and_skips_legacy_script_axis(self):
        self.panel._manual_prepared=dict(manual_inventory=self.rows,manual_cut=self.cut,manual_timeline=self.layout)
        with patch.object(self.panel,'_refresh_coverage_axis',side_effect=AssertionError('legacy script work')):
            self.panel.manual_timeline_saved(dict(state=dict(self.panel.reference_state,revision=9)))
        self.assertIs(self.panel._manual_prepared['manual_inventory'],self.rows)
        self.assertIs(self.panel._manual_prepared['manual_cut'],self.cut)
        self.assertEqual(self.panel.reference_state['revision'],9)

    def test_main_open_button_uses_new_manual_route(self):
        self.panel.browser_open_button.click()
        self.assertEqual(self.requests[0]['operation'],'prepare_manual_timeline')
        self.panel.set_workflow_stage('reference')
        self.assertTrue(self.panel.baseline_map_button.isHidden())
        self.assertTrue(self.panel.baseline_map_status.isHidden())


if __name__=='__main__':unittest.main()
