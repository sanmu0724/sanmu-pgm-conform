"""Read-only whole-script coverage and native interaction regression tests."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import copy
import time
import unittest

from PyQt6.QtCore import Qt, QPoint, QPointF
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QWidget

from script_coverage import ScriptCoverageAxis, ScriptCoverageIndex


def scripts(count=10):
    return [dict(id=f'line-{n}', script_line_id=f'line-{n}', script_id='s',
                 order=n, text='完整台本', scene='第一幕' if n < 5 else '第二幕') for n in range(count)]


def interval(name, first=2, last=4, session='backup', status='matched', clip_status='confirmed', **extra):
    clip = dict(id='candidate-' + name, session_id=session, media_path='/' + name + '.mov',
                status=clip_status, covered_line_ids=[f'line-{n}' for n in range(first, last + 1)],
                pgm_match_status=status)
    result = dict(id=name, clips=[clip], covered_line_ids=list(clip['covered_line_ids']),
                  file_statuses=[dict(session_id=session, media_path=clip['media_path'], status=status)])
    result.update(extra)
    return result


class CoverageIndexTests(unittest.TestCase):
    def test_complete_script_positions_include_regions_without_media(self):
        index = ScriptCoverageIndex(scripts(), [interval('take')])
        self.assertEqual(len(index.line_ids), 10)
        self.assertEqual(index.bands, [(0.2, 0.5)])
        self.assertEqual(index.line_at(.01), 'line-0')
        self.assertEqual(index.line_at(1), 'line-9')
        self.assertEqual(index.hits(.1), [])
        self.assertEqual([row.interval_id for row in index.hits(.3)], ['take'])

    def test_baseline_unmatched_review_and_rejected_are_not_blue(self):
        nodes = [interval('base', session='baseline'), interval('unmatched', status='unmatched'),
                 interval('review', status='review'), interval('rejected', clip_status='rejected'), interval('valid')]
        index = ScriptCoverageIndex(scripts(), nodes, 'baseline')
        self.assertEqual([row.interval_id for row in index.spans], ['valid'])

    def test_grouped_short_alternative_does_not_inherit_baseline_long_range(self):
        base = interval('combined', 0, 9, session='baseline')
        backup = interval('short', 3, 4)
        base['clips'].extend(backup['clips'])
        base['file_statuses'].extend(backup['file_statuses'])
        index = ScriptCoverageIndex(scripts(), [base], 'baseline')
        self.assertEqual(index.bands, [(0.3, 0.5)])

    def test_real_grouped_interval_retains_each_files_own_coverage(self):
        from test_pgm_intervals import catalogue, add_take
        from pgm_intervals import build_pgm_intervals
        from library_timeline import build_timeline_data
        data = catalogue()
        add_take(data, 'baseline', 'long', (10, 12, 14))
        add_take(data, 'backup', 'short', (12, 13))
        nodes = build_pgm_intervals(data)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]['covered_line_ids'], ['l10', 'l11', 'l12', 'l13', 'l14'])
        self.assertEqual(nodes[0]['clips'][1]['covered_line_ids'], ['l12', 'l13'])
        script = build_timeline_data(dict(data, candidates=[]))
        index = ScriptCoverageIndex(script, nodes, 'baseline')
        self.assertEqual(len(index.spans), 1)
        self.assertEqual((index.spans[0].first_line_id, index.spans[0].last_line_id), ('l12', 'l13'))

    def test_no_clock_fields_influence_script_order_or_coverage(self):
        first = scripts()
        first.reverse()
        nodes = [interval('take')]
        original = copy.deepcopy((first, nodes))
        before = ScriptCoverageIndex(first, nodes)
        nodes[0]['clips'][0].update(start_timecode='22:34:10:00', start_seconds=500000)
        after = ScriptCoverageIndex(first, nodes)
        self.assertEqual(before.bands, after.bands)
        self.assertEqual(after.line_ids, [f'line-{n}' for n in range(10)])
        self.assertEqual(first, original[0])
        self.assertNotIn('start_seconds', original[1][0]['clips'][0])

    def test_legacy_confirmed_nodes_without_status_use_group_coverage(self):
        node = interval('legacy')
        del node['clips'][0]['covered_line_ids']
        del node['clips'][0]['pgm_match_status']
        del node['file_statuses']
        index = ScriptCoverageIndex(scripts(), [node])
        self.assertEqual(index.bands, [(0.2, .5)])

    def test_overlapping_intervals_merge_paint_bands_keep_independent_hits(self):
        index = ScriptCoverageIndex(scripts(), [interval('long', 1, 6), interval('short', 2, 3), interval('other', 8, 8)])
        self.assertEqual(index.bands, [(.1, .7), (.8, .9)])
        self.assertEqual([row.interval_id for row in index.hits(.25)], ['short', 'long'])
        self.assertEqual(index.line_range('line-8', 'line-2'), (.2, .9))

    def test_text_weighted_positions_use_whole_script_and_empty_text_is_safe(self):
        rows = [dict(id='a', text='', order=0), dict(id='b', text='abc', order=1)]
        index = ScriptCoverageIndex(rows, [])
        self.assertEqual(index.edges, [0., .25, 1.])
        self.assertEqual(index.line_at(.25), 'b')
        self.assertIsNone(index.line_range('missing'))
        self.assertEqual(ScriptCoverageIndex().line_at(0), '')

    def test_ten_thousand_nodes_and_takes_build_once_and_hit_test_without_expansion(self):
        rows = scripts(10000)
        nodes = [interval(f'take-{n}', n, n) for n in range(10000)]
        start = time.perf_counter()
        index = ScriptCoverageIndex(rows, nodes)
        elapsed = time.perf_counter() - start
        self.assertEqual(len(index.spans), 10000)
        self.assertLess(elapsed, 3.0)
        start = time.perf_counter()
        for n in range(1000):
            self.assertTrue(index.hits((n + .5) / 10000))
        self.assertLess(time.perf_counter() - start, .5)


class CoverageAxisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.axis = ScriptCoverageAxis()
        self.axis.resize(40, 600)
        self.axis.set_nodes(scripts(), [interval('take')])
        self.axis.show()
        self.app.processEvents()

    def tearDown(self):
        self.axis.close()
        self.axis.deleteLater()
        self.app.processEvents()

    def click_position(self, position):
        rail = self.axis._rail()
        QTest.mouseClick(self.axis, Qt.MouseButton.LeftButton,
                         pos=QPoint(int(rail.center().x()), int(rail.top() + rail.height() * position)))

    def test_blue_click_requests_script_then_whole_pgm_interval(self):
        events = []
        self.axis.line_requested.connect(lambda value: events.append(('line', value)))
        self.axis.interval_clicked.connect(lambda value: events.append(('pgm', value)))
        self.click_position(.35)
        self.assertEqual(events, [('line', 'line-3'), ('pgm', 'take')])
        self.assertEqual(self.axis._selected_interval, 'take')

    def test_white_click_only_navigates_script(self):
        ids, lines = [], []
        self.axis.interval_clicked.connect(ids.append)
        self.axis.line_requested.connect(lines.append)
        self.click_position(.85)
        self.assertEqual(ids, [])
        self.assertEqual(lines, ['line-8'])

    def test_repeated_overlap_click_cycles_independent_intervals(self):
        self.axis.set_nodes(scripts(), [interval('long', 1, 6), interval('short', 2, 3)])
        ids = []
        self.axis.interval_clicked.connect(ids.append)
        for _ in range(3):
            self.click_position(.25)
        self.assertEqual(ids, ['short', 'long', 'short'])

    def test_wheel_and_page_navigation_emit_pixels_in_scroll_direction(self):
        values = []
        self.axis.scroll_requested.connect(values.append)
        event = QWheelEvent(QPointF(20, 200), QPointF(20, 200), QPoint(), QPoint(0, -120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        self.app.sendEvent(self.axis, event)
        QTest.keyClick(self.axis, Qt.Key.Key_PageUp)
        self.assertEqual(values[0], 120)
        self.assertLess(values[1], 0)

    def test_selected_interval_and_viewport_indicator_survive_resize(self):
        self.axis.set_selected_interval('take')
        self.axis.set_viewport_range('line-2', 'line-4')
        self.assertEqual(self.axis._viewport_range, (.2, .5))
        for width, height in [(30, 80), (40, 900), (52, 260)]:
            self.axis.resize(width, height)
            self.app.processEvents()
            image = self.axis.grab().toImage()
            self.assertFalse(image.isNull())
            self.assertEqual(self.axis._selected_interval, 'take')
        self.assertLess(len(self.axis.findChildren(QWidget)), 1)

    def test_blue_and_white_regions_are_visible_in_rendered_axis(self):
        image = self.axis.grab().toImage()
        x = self.axis.width() // 2
        blue = image.pixelColor(x, int(self.axis._rail().top() + self.axis._rail().height() * .35))
        white = image.pixelColor(x, int(self.axis._rail().top() + self.axis._rail().height() * .85))
        self.assertGreater(blue.blue(), blue.red())
        self.assertEqual((white.red(), white.green(), white.blue()), (255, 255, 255))


if __name__ == '__main__':
    unittest.main()
