"""Script-number organization is restored without converting it to seconds."""
import copy
import unittest
from unittest.mock import patch

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from manual_timeline import reconcile_layout
from manual_timeline_ui import ManualTimelineDialog, parse_script_range
from test_baseline_map_ui import FakePreview
from test_manual_timeline_ui import fixture


class ScriptOrganizerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.rows,self.layout,self.cut=fixture(5)
        self.ids=[row['id'] for row in self.rows if row['session_id']=='alt']
        self.layout['items'][self.ids[0]].update(organize_first_line=55,organize_last_line=76,organize_reviewed=True,organize_pool='track')
        self.layout['items'][self.ids[1]].update(organize_first_line=60,organize_last_line=65,organize_lane=2,organize_reviewed=False,organize_pool='track')
        self.layout['items'][self.ids[2]].update(organize_first_line=None,organize_last_line=None,organize_pool='unmatched')
        self.layout['items'][self.ids[3]].update(organize_pool='removed')
        self.layout['items'][self.ids[4]].update(organize_pool='hidden')
        self.data=dict(scripts=[dict(id='script',lines=[dict(id=f'l{i}',text=f'第{i}行台词',order=i) for i in range(1,101)])])
        self.patch=patch('library_ui.InlinePreview',FakePreview);self.patch.start()
        self.dialog=ManualTimelineDialog(self.data,self.rows,self.layout,self.cut)

    def tearDown(self):
        self.dialog._allow_close=True;self.dialog.close();self.patch.stop()

    def test_organizer_restores_only_track_items_and_script_inclusive_geometry(self):
        entries=self.dialog.canvas._entries
        self.assertEqual(set(entries),set(self.ids[:2]))
        self.assertEqual((entries[self.ids[0]]['start'],entries[self.ids[0]]['end']),(55.,77.))
        self.assertTrue(entries[self.ids[0]]['reviewed'])
        self.assertFalse(entries[self.ids[1]]['reviewed'])
        self.assertEqual(self.dialog.canvas.origin_seconds,1.)
        self.assertGreaterEqual(self.dialog.canvas.end_seconds,101.)
        self.assertEqual(self.dialog.layout_data['items'][self.ids[0]]['start_seconds'],5.)

    def test_media_pools_exclude_track_and_hidden_items(self):
        ids=set(self.dialog.pool_model.by_id)
        self.assertIn(self.ids[2],ids)
        self.assertFalse(ids & set(self.ids[:2]+self.ids[3:]))
        self.dialog.pool_kind.setCurrentIndex(1)
        self.assertEqual(set(self.dialog.pool_model.by_id),{self.ids[3]})

    def test_track_selection_highlights_complete_script_range_and_pauses(self):
        self.dialog.track_selection([self.ids[0]])
        selection=self.dialog.script_preview.highlighted_line_ids
        self.assertEqual(selection,{f'l{i}' for i in range(55,77)})
        self.assertEqual(self.dialog.script_range.text(),'55-76')
        self.dialog.preview.toggle_playback.assert_not_called()

    def test_ruler_click_selects_script_line_without_changing_seconds_or_preview(self):
        before=copy.deepcopy(self.dialog.layout_data)
        self.dialog.seek_timeline(42.5)
        self.assertEqual(self.dialog.script_preview.highlighted_line_ids,{'l42'})
        self.assertFalse(self.dialog.preview.loads)
        self.assertEqual(self.dialog.layout_data,before)

    def test_explicit_range_edit_confirms_green_without_changing_actual_placement(self):
        self.dialog.track_selection([self.ids[1]])
        self.dialog.script_range.setText('70-80');self.dialog.apply_position()
        item=self.dialog.layout_data['items'][self.ids[1]]
        self.assertEqual((item['organize_first_line'],item['organize_last_line']),(70,80))
        self.assertTrue(item['organize_reviewed'])
        self.assertEqual(item['start_seconds'],20.)
        self.assertEqual(item['lane'],1)

    def test_unmatched_can_be_added_by_single_line_and_source_is_untouched(self):
        original=copy.deepcopy(self.rows)
        self.dialog._selected=[self.ids[2]];self.dialog.script_range.setText('35');self.dialog.apply_position()
        item=self.dialog.layout_data['items'][self.ids[2]]
        self.assertEqual((item['organize_first_line'],item['organize_last_line']),(35,35))
        self.assertEqual(item['organize_pool'],'track')
        self.assertIsNone(item['start_seconds'])
        self.assertEqual(self.rows,original)

    def test_group_vertical_drag_preserves_ranges_and_horizontal_drag_does_nothing(self):
        canvas=self.dialog.canvas
        canvas.resize(1000,390);canvas.show();self.app.processEvents()
        canvas.horizontalScrollBar().setValue(370)
        canvas.selection=set(self.ids[:2])
        before=copy.deepcopy(self.dialog.layout_data)
        first=canvas.clip_rect(canvas._entries[self.ids[0]]).center().toPoint()
        last=first+QPoint(90,62)
        QTest.mousePress(canvas.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,first)
        QTest.mouseMove(canvas.viewport(),last)
        QTest.mouseRelease(canvas.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,last)
        for identity in self.ids[:2]:
            old=before['items'][identity];new=self.dialog.layout_data['items'][identity]
            self.assertEqual((new['organize_first_line'],new['organize_last_line']),(old['organize_first_line'],old['organize_last_line']))
            self.assertEqual(new['start_seconds'],old['start_seconds'])
        self.assertNotEqual(self.dialog.layout_data['items'][self.ids[0]]['organize_lane'],before['items'][self.ids[0]]['organize_lane'])

    def test_transfer_retains_group_but_never_converts_line_numbers_to_seconds(self):
        self.dialog.track_selection(self.ids[:2])
        self.dialog.transfer_to_timeline()
        self.assertEqual(self.dialog.mode.currentData(),'timeline')
        self.assertEqual(self.dialog._selected,self.ids[:2])
        self.assertEqual(self.dialog.layout_data['items'][self.ids[0]]['start_seconds'],5.)
        self.assertEqual(self.dialog.layout_data['items'][self.ids[1]]['start_seconds'],20.)
        self.assertTrue(self.dialog.script_panel.isHidden())
        self.assertFalse(self.dialog.position.isHidden())

    def test_new_group_uses_one_requested_second_and_distinct_lanes_not_script_offsets(self):
        for identity in self.ids[:2]:self.dialog.layout_data['items'][identity]['start_seconds']=None
        self.dialog.canvas.set_layout(self.dialog.layout_data)
        self.dialog.track_selection(self.ids[:2]);self.dialog.transfer_to_timeline()
        self.dialog.position.setValue(123.4);self.dialog.lane.setValue(1);self.dialog.apply_position()
        items=[self.dialog.layout_data['items'][identity] for identity in self.ids[:2]]
        self.assertEqual([item['start_seconds'] for item in items],[123.4,123.4])
        self.assertEqual(len({item['lane'] for item in items}),2)
        self.assertEqual([item['organize_first_line'] for item in items],[55,60])

    def test_baseline_can_have_organizer_range_but_actual_v1_geometry_is_unchanged(self):
        baseline=next(row for row in self.rows if row['session_id']=='base')
        item=self.dialog.layout_data['items'][baseline['id']]
        item.update(organize_first_line=2,organize_last_line=8,organize_pool='track',organize_lane=4)
        self.dialog.layout_changed(self.dialog.layout_data)
        self.dialog.track_selection([baseline['id']]);self.dialog.script_range.setText('3-9');self.dialog.apply_position()
        self.assertEqual(self.dialog.layout_data['items'][baseline['id']]['organize_first_line'],3)
        self.dialog.mode.setCurrentIndex(1)
        entry=self.dialog.canvas._entries[baseline['id']]
        self.assertTrue(entry['baseline']);self.assertEqual((entry['start'],entry['end'],entry['lane']),(0.,10.,0))

    def test_organizer_delete_moves_to_removed_and_stops_alternative_export(self):
        self.dialog.track_selection([self.ids[0]]);self.dialog.remove_selected()
        item=self.dialog.layout_data['items'][self.ids[0]]
        self.assertEqual(item['organize_pool'],'removed')
        self.assertIsNone(item['start_seconds'])
        self.assertEqual(item['organize_first_line'],55)
        self.assertNotIn(self.ids[0],self.dialog.canvas._entries)
        self.dialog.pool_kind.setCurrentIndex(1)
        self.assertIn(self.ids[0],self.dialog.pool_model.by_id)
        self.assertIn('台本区间 1 条PGM',self.dialog.summary.text())
        self.assertIn('已核实 0',self.dialog.summary.text())

    def test_deleted_pool_item_can_be_hidden_without_deleting_source_data(self):
        self.dialog.pool_kind.setCurrentIndex(1);self.dialog._selected=[self.ids[3]];self.dialog.remove_selected()
        self.assertNotIn(self.ids[3],self.dialog.pool_model.by_id)
        self.assertEqual(self.dialog.layout_data['items'][self.ids[3]]['organize_pool'],'hidden')
        self.assertIn(self.ids[3],self.dialog.layout_data['items'])

    def test_pool_delete_shortcut_hides_selected_item_and_context_label_is_clear(self):
        self.dialog.show();self.app.processEvents();self.dialog.pool_kind.setCurrentIndex(1)
        index=self.dialog.pool_model.index(self.dialog.pool_model.by_id[self.ids[3]],0)
        self.dialog.pool.setCurrentIndex(index);self.dialog.pool.setFocus();self.app.processEvents()
        self.assertEqual(self.dialog.remove_button.text(),'从素材池隐藏')
        QTest.keyClick(self.dialog.pool,Qt.Key.Key_Delete)
        self.assertEqual(self.dialog.layout_data['items'][self.ids[3]]['organize_pool'],'hidden')
        self.assertNotIn(self.ids[3],self.dialog.pool_model.by_id)

    def test_out_of_bounds_range_leaves_all_data_unchanged(self):
        before=copy.deepcopy(self.dialog.layout_data)
        self.dialog._selected=[self.ids[0]];self.dialog.script_range.setText('55-101');self.dialog.apply_position()
        self.assertEqual(self.dialog.layout_data,before)
        self.assertTrue(self.dialog.error.text())

    def test_opening_never_moves_build38_actual_positions(self):
        for identity,old in self.layout['items'].items():
            current=self.dialog.layout_data['items'][identity]
            self.assertEqual((old['start_seconds'],old['lane']),(current['start_seconds'],current['lane']))

    def test_range_parser_accepts_single_or_full_width_separator(self):
        self.assertEqual(parse_script_range('台本55—76',100),(55,76))
        self.assertEqual(parse_script_range('55',100),(55,55))
        for invalid in ('55.5','76-55','0','55-101',''):
            with self.assertRaises(ValueError):parse_script_range(invalid,100)

    def test_readonly_script_space_and_explicit_play_work_but_edit_fields_keep_space(self):
        self.dialog.show();self.app.processEvents()
        self.dialog.track_selection([self.ids[0]])
        self.dialog.script_preview.setFocus();QTest.keyClick(self.dialog.script_preview,Qt.Key.Key_Space)
        self.dialog.preview.player.play.assert_called_once()
        self.dialog.preview.player.play.reset_mock()
        self.dialog.script_range.setFocus();self.dialog.script_range.clear();QTest.keyClicks(self.dialog.script_range,'55 76')
        self.assertEqual(self.dialog.script_range.text(),'55 76')
        self.dialog.preview.player.play.assert_not_called()
        self.assertTrue(self.dialog._alternative_wants_play)
        self.dialog.play_button.click()
        self.assertFalse(self.dialog._alternative_wants_play)
        self.dialog.preview.pause.assert_called()


if __name__=='__main__':unittest.main()
