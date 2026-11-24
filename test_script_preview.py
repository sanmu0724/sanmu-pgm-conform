"""Readable script cards keep canonical data and use no media services."""
import copy
import gc
import unittest
import weakref
from unittest.mock import patch

from PyQt6.QtCore import Qt,QEventLoop,QTimer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication,QListView,QStyleOptionViewItem,QWidget

from script_preview import ScriptPreview,ScriptPreviewDelegate,NODE_ROLE,COVERED_ROLE,SCENE_ROLE


def nodes(count=80):
    return [dict(id=f'l{i}',order=count-i,script_id='script',scene='第一幕' if i<40 else '第二幕',
        speaker='阿尔吉' if i%2 else '杰克',text=f'这是第{i}段完整台词。\nWe should discuss it, paragraph {i}.',
        pgm_count=12,clips=[dict(path='/never/read.mov')]) for i in range(1,count+1)]


class ScriptPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.widget=ScriptPreview();self.widget.resize(320,760);self.values=nodes();self.widget.set_nodes(self.values)

    def tearDown(self):
        self.widget.close();self.widget.deleteLater();self.app.processEvents()

    def show(self):
        self.widget.show();self.app.processEvents()
        loop=QEventLoop();QTimer.singleShot(200,loop.quit);loop.exec();self.app.processEvents()

    def test_canonical_input_order_is_not_resorted_or_mutated(self):
        before=copy.deepcopy(self.values)
        self.widget.set_nodes(self.values)
        self.assertEqual([row['id'] for row in self.widget.model.nodes],[row['id'] for row in self.values])
        self.assertEqual([row['number'] for row in self.widget.model.nodes],list(range(1,81)))
        self.assertEqual(self.values,before)

    def test_card_model_discards_all_material_relations_and_statistics(self):
        row=self.widget.model.data(self.widget.model.index(0,0),NODE_ROLE)
        self.assertNotIn('clips',row);self.assertNotIn('pgm_count',row)
        displayed=self.widget.model.data(self.widget.model.index(0,0))
        self.assertNotIn('条PGM',displayed);self.assertNotIn('条素材',displayed)

    def test_delegate_keeps_window_and_model_alive_across_text_allocation_gc(self):
        owner=QWidget();widget=ScriptPreview(owner);widget.set_nodes(nodes(4))
        delegate=widget.delegate;index=widget.model.index(0,0)
        option=QStyleOptionViewItem();option.initFrom(widget.view)
        keep=[owner];window_ref=weakref.ref(owner);model_ref=weakref.ref(widget.model)
        del owner,widget
        original=delegate._text;calls=[]
        def collect_during_text(*args):
            keep.clear();gc.collect()
            self.assertIsNotNone(window_ref(),'window must survive the entire delegate callback')
            self.assertIsNotNone(model_ref(),'model must survive text shaping')
            calls.append(True)
            return original(*args)
        with patch.object(delegate,'_text',side_effect=collect_during_text):
            self.assertGreater(delegate.sizeHint(option,index).height(),0)
        self.assertTrue(calls)
        # Strong callback guards must not become a permanent window cache.
        gc.collect();self.assertIsNone(window_ref())

    def test_whole_multirange_highlight_preserves_identifiers_and_full_script(self):
        self.widget.highlight_ranges([(23,45),(55,55)])
        self.assertEqual(self.widget.highlighted_line_ids,{f'l{i}' for i in list(range(23,46))+[55]})
        self.assertEqual(self.widget.model.rowCount(),80)
        self.assertEqual(self.widget.view.currentIndex().row(),22)
        self.assertTrue(self.widget.model.data(self.widget.model.index(44,0),COVERED_ROLE))
        self.assertFalse(self.widget.model.data(self.widget.model.index(45,0),COVERED_ROLE))

    def test_clear_highlight_does_not_change_model_or_source(self):
        self.widget.highlight_ranges([(1,5)]);self.widget.highlight_ranges([])
        self.assertEqual(self.widget.highlighted_line_ids,set());self.assertEqual(self.widget.model.rowCount(),80)
        self.assertNotIn('number',self.values[0])

    def test_invalid_ranges_and_scroll_numbers_are_ignored(self):
        self.widget.highlight_ranges([(0,5),(8,3),(79,81),(True,5),None])
        self.assertEqual(self.widget.highlighted_line_ids,set())
        for value in (0,81,1.5,True):self.assertFalse(self.widget.scroll_to_line(value))

    def test_highlight_property_returns_independent_set(self):
        self.widget.highlight_ranges([(1,1)]);value=self.widget.highlighted_line_ids;value.clear()
        self.assertEqual(self.widget.highlighted_line_ids,{'l1'})

    def test_speaker_multilingual_body_and_action_newlines_are_preserved(self):
        text='（停顿，看向门口）\n第一句中文。\nThe first English line.\n（转身离开）'
        self.widget.set_nodes([dict(id='line',speaker='阿尔吉',text=text)])
        row=self.widget.model.data(self.widget.model.index(0,0),NODE_ROLE)
        self.assertEqual(row['text'],text);self.assertEqual(row['speaker'],'阿尔吉')
        self.show();option=QStyleOptionViewItem();option.initFrom(self.widget.view)
        multiline=self.widget.delegate.sizeHint(option,self.widget.model.index(0,0)).height()
        self.widget.set_nodes([dict(id='line',speaker='阿尔吉',text='一句话')])
        one=self.widget.delegate.sizeHint(option,self.widget.model.index(0,0)).height()
        self.assertGreater(multiline,one*1.5)

    def test_scene_titles_are_decorative_and_do_not_consume_numbered_rows(self):
        self.assertEqual(self.widget.model.data(self.widget.model.index(0,0),SCENE_ROLE),'第一幕')
        self.assertEqual(self.widget.model.data(self.widget.model.index(1,0),SCENE_ROLE),'')
        self.assertEqual(self.widget.model.data(self.widget.model.index(39,0),SCENE_ROLE),'第二幕')
        self.assertEqual(self.widget.model.nodes[39]['number'],40)

    def test_numeric_search_locates_without_filtering_or_renumbering(self):
        self.widget.search.setText('55')
        self.assertEqual(self.widget.view.currentIndex().row(),54)
        self.assertEqual(self.widget.model.rowCount(),80)
        self.assertEqual(self.widget.model.nodes[-1]['number'],80)
        self.assertIn('台本 55',self.widget.status.text())

    def test_search_matches_speaker_dialogue_english_and_cycles(self):
        self.widget.search.setText('阿尔吉');first=self.widget.view.currentIndex().row();self.widget.find_next()
        self.assertGreater(self.widget.view.currentIndex().row(),first)
        self.widget.search.setText('PARAGRAPH 77.')
        self.assertEqual(self.widget.view.currentIndex().row(),76)

    def test_empty_search_result_keeps_full_script_and_existing_pgm_range(self):
        self.widget.highlight_ranges([(4,8)]);self.widget.search.setText('不存在的文字')
        self.assertIn('未找到',self.widget.status.text());self.assertEqual(self.widget.model.rowCount(),80)
        self.assertEqual(self.widget.highlighted_line_ids,{f'l{i}' for i in range(4,9)})

    def test_search_field_keeps_space_and_no_space_shortcut_is_installed(self):
        self.show();self.widget.search.setFocus();QTest.keyClicks(self.widget.search,'English words')
        self.assertEqual(self.widget.search.text(),'English words')
        from PyQt6.QtGui import QShortcut
        self.assertFalse(any(shortcut.key().toString()=='Space' for shortcut in self.widget.findChildren(QShortcut)))

    def test_coverage_change_only_repaints_without_model_reset_or_layout_signals(self):
        resets=[];changed=[];self.widget.model.modelReset.connect(lambda:resets.append(True));self.widget.model.dataChanged.connect(lambda *_:changed.append(True))
        with patch('subprocess.run',side_effect=AssertionError('No media probing')),patch('builtins.open',side_effect=AssertionError('No file access')):
            self.widget.highlight_ranges([(20,30)])
        self.assertEqual(resets,[]);self.assertEqual(changed,[])

    def test_variable_row_height_resizes_and_preserves_reading_anchor(self):
        self.show();self.widget.scroll_to_line(45);self.app.processEvents();QTest.qWait(60)
        before=self.widget.view.reading_anchor();self.assertIsNotNone(before)
        self.widget.resize(240,760);self.app.processEvents();QTest.qWait(90);self.app.processEvents()
        after=self.widget.view.reading_anchor()
        self.assertEqual(after[0],before[0]);self.assertLessEqual(abs(after[1]-before[1]),2)
        self.assertEqual(self.widget.view.horizontalScrollBar().maximum(),0)

    def test_explicit_range_after_resize_wins_over_old_reading_anchor(self):
        self.show();self.widget.scroll_to_line(10);self.app.processEvents()
        self.widget.resize(250,760);self.widget.highlight_ranges([(55,60)]);self.app.processEvents();QTest.qWait(90)
        self.assertEqual(self.widget.view.reading_anchor()[0],'l55')

    def test_large_list_uses_batched_layout_without_per_row_widgets_and_bounded_cache(self):
        class RecordingDelegate(ScriptPreviewDelegate):
            def __init__(self,parent):super().__init__(parent);self.painted=[]
            def paint(self,painter,option,index):self.painted.append(index.row());super().paint(painter,option,index)
        delegate=RecordingDelegate(self.widget.view);self.widget.delegate=delegate;self.widget.view.setItemDelegate(delegate)
        self.widget.set_nodes(nodes(10000));self.show()
        # Batched variable-height layout deliberately yields between chunks.
        # Wait for its first frame rather than requiring a synchronous layout.
        import time
        deadline=time.monotonic()+2
        while not delegate.painted and time.monotonic()<deadline:
            loop=QEventLoop();QTimer.singleShot(20,loop.quit);loop.exec()
        delegate.painted.clear();self.widget.view.viewport().grab()
        self.assertEqual(self.widget.model.rowCount(),10000)
        self.assertEqual(self.widget.view.layoutMode(),QListView.LayoutMode.Batched)
        self.assertGreater(len(delegate.painted),0);self.assertLess(len(delegate.painted),30)
        self.assertLessEqual(len(delegate._cache),delegate.CACHE_LIMIT)
        self.assertLess(len(self.widget.findChildren(QWidget)),20)

    def test_bad_identity_does_not_destroy_existing_script(self):
        for values in ([dict(id='a'),dict(id='a')],[dict(text='missing')]):
            with self.assertRaises(ValueError):self.widget.set_nodes(values)
            self.assertEqual(self.widget.model.rowCount(),80)

    def test_empty_script_has_clear_state(self):
        self.widget.set_nodes([])
        self.assertEqual(self.widget.model.rowCount(),0);self.assertIn('尚未导入',self.widget.status.text())

    def test_hide_stops_timers_and_deleted_view_ignores_late_callbacks(self):
        from PyQt6 import sip
        self.show();view=self.widget.view
        view._resize_timer.start();view._scroll_timer.start();self.widget.hide()
        self.assertFalse(view._resize_timer.isActive());self.assertFalse(view._scroll_timer.isActive())
        sip.delete(view)
        view._remember_anchor();view._restore_anchor();view._schedule_anchor()

    def test_cold_large_script_navigation_waits_for_batched_geometry(self):
        self.widget.set_nodes(nodes(10000));self.widget.show();self.widget.highlight_ranges([(9900,9910)])
        import time
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            loop=QEventLoop();QTimer.singleShot(40,loop.quit);loop.exec()
            anchor=self.widget.view.reading_anchor()
            if anchor and anchor[0]=='l9900':break
        self.assertEqual(self.widget.view.reading_anchor()[0],'l9900')


if __name__=='__main__':unittest.main()
