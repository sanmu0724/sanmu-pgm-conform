"""Visual script-axis tests use offscreen Qt and no media/network access."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import copy
import unittest

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QWidget

from script_timeline import ScriptTimeline, ScriptTimelineDelegate, NODE_ROLE


def node(line_id, text='台词', order=0, scene='第一幕', count=0, confirmed=0, candidate=0, **extra):
    result = dict(id=line_id, script_line_id=line_id, script_id='script', scene=scene,
                speaker='Jack', text=text, order=order, count=count,
                confirmed_count=confirmed, candidate_count=candidate,
                session_count=1 if count else 0, session_names=['Day01'] if count else [])
    result.update(extra)
    return result


class ScriptTimelineTests(unittest.TestCase):
    def test_summary_update_keeps_items_selection_and_applies_review_filter(self):
        values = [node('one', count=1, candidate=1), node('two', order=1, count=1, candidate=1)]
        self.view.set_nodes(values, selected_id='one')
        original = dict(self.view._items)
        values[0].update(candidate_count=0, confirmed_count=1)
        self.view.update_nodes(values)
        self.assertIs(self.view._items['one'], original['one'])
        self.assertIs(self.view._items['two'], original['two'])
        self.assertEqual(self.view.current_line_id(), 'one')
        self.assertEqual(self.view._items['one'].data(0, NODE_ROLE)['confirmed_count'], 1)
        self.view.filter_nodes(mode='review')
        self.assertEqual(self.view.current_line_id(), 'two')
        values[1].update(candidate_count=0, confirmed_count=1)
        self.view.update_nodes(values)
        self.assertTrue(self.view._items['two'].isHidden())

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.view = ScriptTimeline()
        self.view.resize(430, 700)

    def tearDown(self):
        self.view.close()
        self.view.deleteLater()
        self.app.processEvents()

    def show(self):
        self.view.show()
        self.app.processEvents()
        self.view.doItemsLayout()
        self.app.processEvents()

    def test_script_order_is_preserved_and_scene_names_are_not_sorted(self):
        self.view.set_nodes([node('2', order=2, scene='第一幕'), node('1', order=1, scene='第二幕'),
                             node('3', order=3, scene='第二幕')])
        self.assertEqual([self.view.topLevelItem(i).text(0) for i in range(3)], ['第二幕', '第一幕', '第二幕'])
        ids = [self.view.topLevelItem(i).child(j).data(0, Qt.ItemDataRole.UserRole)
               for i in range(self.view.topLevelItemCount()) for j in range(self.view.topLevelItem(i).childCount())]
        self.assertEqual(ids, ['1', '2', '3'])

    def test_selection_survives_refresh_and_emits_unique_line_id(self):
        values = [node('one'), node('two', order=1)]
        signals = []
        self.view.node_selected.connect(signals.append)
        self.view.set_nodes(values, selected_id='two')
        self.assertEqual(self.view.current_line_id(), 'two')
        changed = copy.deepcopy(values)
        changed[1]['count'] = 4
        self.view.set_nodes(changed)
        self.assertEqual(self.view.current_line_id(), 'two')
        self.assertEqual(self.view.currentItem().data(0, NODE_ROLE)['count'], 4)
        self.assertEqual(signals[-1], 'two')

    def test_filter_modes_move_selection_off_hidden_nodes(self):
        self.view.set_data([node('empty'), node('one', order=1, count=1, confirmed=1),
                            node('many', order=2, count=4, confirmed=2, candidate=2)], selected_line_id='one')
        self.view.set_filter('multiple')
        self.assertEqual(self.view.current_line_id(), 'many')
        self.assertTrue(self.view.topLevelItem(0).child(1).isHidden())
        self.view.filter_nodes(mode='empty')
        self.assertEqual(self.view.current_line_id(), 'empty')
        self.view.filter_nodes(mode='review')
        self.assertEqual(self.view.current_line_id(), 'many')
        self.view.filter_nodes(query='no such word')
        self.assertEqual(self.view.current_line_id(), '')
        self.assertTrue(self.view.topLevelItem(0).isHidden())
        self.view.filter_nodes()
        self.assertEqual(self.view.current_line_id(), 'empty')

    def test_search_includes_scene_speaker_text_and_recording_names(self):
        self.view.set_nodes([node('chinese', '我们等明天再说', scene='第三幕'),
                             node('english', 'What is happening here?', order=1, count=1, candidate=1)])
        for query, expected in [('第三幕', 'chinese'), ('明天', 'chinese'), ('HAPPENING', 'english'), ('Day01', 'english')]:
            self.view.filter_nodes(query)
            self.assertEqual(self.view.current_line_id(), expected)

    def test_long_chinese_english_wrap_and_resize_increase_height_without_elision(self):
        long_text = '这是演员一整段不会被截去的对白。' * 18 + ' This is a long English speech with ordinary words. ' * 12
        self.view.set_nodes([node('long', long_text, count=5, confirmed=3, candidate=2), node('short', '你好', order=1)])
        self.show()
        long_item = self.view.topLevelItem(0).child(0)
        short_item = self.view.topLevelItem(0).child(1)
        wide = self.view.visualItemRect(long_item).height()
        self.assertGreater(wide, self.view.visualItemRect(short_item).height() * 3)
        self.assertIn(long_text, long_item.toolTip(0))
        self.assertIn(long_text, long_item.text(0))
        self.view.resize(300, 700)
        self.app.processEvents()
        self.view.doItemsLayout()
        narrow = self.view.visualItemRect(long_item).height()
        self.assertGreater(narrow, wide)
        self.assertEqual(self.view.horizontalScrollBar().maximum(), 0)

    def test_dialogue_rows_show_script_only_without_material_statistics(self):
        self.view.set_nodes([node('many', '长台词', count=7, confirmed=4, candidate=3, session_count=3)])
        item = self.view.topLevelItem(0).child(0)
        self.assertEqual(item.text(0), '1  Jack：长台词')
        self.assertNotIn('条素材', item.text(0))
        self.assertNotIn('已核对', item.toolTip(0))
        self.assertIsNone(self.view.itemWidget(item, 0))

    def test_visible_global_line_numbers_follow_sorted_script_across_scenes(self):
        values = [node('last', order=3, scene='第二幕'), node('first', order=1), node('middle', order=2)]
        before = copy.deepcopy(values)
        self.view.set_nodes(values)
        for number, identity in enumerate(('first', 'middle', 'last'), 1):
            item = self.view._items[identity]
            self.assertEqual(item.data(0, NODE_ROLE)['script_row_number'], number)
            self.assertTrue(item.text(0).startswith(str(number) + '  '))
        self.view.filter_nodes(query='第二幕')
        self.assertTrue(self.view.currentItem().text(0).startswith('3  '))
        self.assertEqual(values, before)
        self.view.update_nodes(sorted(values, key=lambda row: row['order']))
        self.assertEqual(self.view._items['last'].data(0, NODE_ROLE)['script_row_number'], 3)

    def test_pgm_rows_keep_whole_file_material_statistics(self):
        self.view.set_nodes([node('pgm', 'PGM覆盖区间', count=3, confirmed=2, candidate=1,
                                 pgm_count=3, angle_count=12, camera_count=4)])
        item = self.view.topLevelItem(0).child(0)
        self.assertIn('3 条PGM', item.text(0))
        self.assertIn('已核对 2 · 待核对 1', item.toolTip(0))

    def test_one_performance_with_multiple_angles_keeps_distinct_counts_and_filter(self):
        self.view.set_nodes([node('empty'), node('multicam', order=1, count=1, candidate=1,
                             angle_count=4, camera_count=3), node('single', order=2, count=1,
                             confirmed=1, angle_count=1, camera_count=1)])
        self.view.filter_nodes(mode='multiple')
        self.assertEqual(self.view.current_line_id(), 'multicam')
        item=self.view.currentItem()
        self.assertEqual(item.data(0,NODE_ROLE)['count'],1)
        self.assertNotIn('机位片段',item.text(0))
        self.assertNotIn('机位',item.toolTip(0))
        self.assertTrue(self.view.topLevelItem(0).child(2).isHidden())
        self.show()
        self.assertEqual(self.view.horizontalScrollBar().maximum(),0)

    def test_keyboard_navigation_and_scroll_select_real_nodes(self):
        self.view.set_nodes([node(str(i), f'第{i}句', order=i, count=i % 4) for i in range(40)])
        self.show()
        self.view.setFocus()
        QTest.keyClick(self.view, Qt.Key.Key_Down)
        self.assertEqual(self.view.current_line_id(), '1')
        self.view.setCurrentItem(self.view.topLevelItem(0).child(39))
        self.view.scrollToItem(self.view.currentItem())
        self.app.processEvents()
        self.assertGreater(self.view.verticalScrollBar().value(), 0)

    def test_thousands_of_nodes_create_no_row_widgets_and_paint_only_visible_rows(self):
        class RecordingDelegate(ScriptTimelineDelegate):
            painted = []
            def paint(self, painter, option, index):
                self.painted.append(index.data(Qt.ItemDataRole.UserRole))
                return super().paint(painter, option, index)
        delegate = RecordingDelegate(self.view)
        self.view.setItemDelegate(delegate)
        self.view.set_nodes([node(str(i), '剧情台词', order=i, count=i % 7, candidate=i % 7) for i in range(2500)])
        self.show()
        self.assertEqual(self.view.topLevelItem(0).childCount(), 2500)
        self.assertLess(len(self.view.findChildren(QWidget)), 20)
        delegate.painted.clear()
        self.view.viewport().grab()
        self.assertGreater(len(delegate.painted), 0)
        self.assertLess(len(delegate.painted), 30)
        self.assertLessEqual(len(delegate._cache), 1024)

    def test_invalid_node_identity_does_not_destroy_previous_tree(self):
        self.view.set_nodes([node('stable')])
        with self.assertRaises(ValueError):
            self.view.set_nodes([node('duplicate'), node('duplicate')])
        self.assertEqual(self.view.current_line_id(), 'stable')
        with self.assertRaises(ValueError):
            self.view.set_nodes([{'text': '缺少ID'}])
        self.assertEqual(self.view.current_line_id(), 'stable')


if __name__ == '__main__':
    unittest.main()
