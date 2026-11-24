"""Browse-time hashing/layout work stays bounded without stale review colors."""
import copy
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from unittest.mock import patch

from pgm_editor_state import coverage_fingerprint
from pgm_takes import PgmTakeIndex
from test_pgm_intervals import catalogue, add_take
from PyQt6.QtWidgets import QApplication
from script_timeline import ScriptTimeline, COVERED_ROLE


class EditorBrowseCacheTests(unittest.TestCase):
    def fixture(self, manual=False):
        data = catalogue()
        path = add_take(data, 's', 'one', (10,) if manual else (10, 11), manual=manual)
        for candidate, source in zip(data['candidates'], data['transcripts']):
            candidate.update(start_seconds=source['start_seconds'], end_seconds=source['end_seconds'])
        if manual:
            data['transcripts'][0]['data']['manual_script_line_ids'] = ['l10', 'l11']
        return data, path

    def review(self, data, path):
        index = PgmTakeIndex(data, prepared=True)
        candidate = data['candidates'][0]
        state = index.editor_state(candidate)
        data['sessions'][0]['config']['library_pgm_editor'] = {path: dict(
            pool='timeline', reviewed=True, coverage_fingerprint=state['coverage_fingerprint'])}
        self.assertTrue(index.editor_state(candidate)['reviewed'])
        return index, candidate

    def test_repeated_clicks_hash_once_and_read_current_editor_record(self):
        data, path = self.fixture()
        index = PgmTakeIndex(data, prepared=True)
        candidate = data['candidates'][0]
        with patch('pgm_editor_state.coverage_fingerprint', wraps=coverage_fingerprint) as calculate:
            initial = index.editor_state(candidate)
            config = data['sessions'][0]['config']
            config['library_pgm_editor'] = {path: dict(pool='timeline', reviewed=True,
                coverage_fingerprint=initial['coverage_fingerprint'])}
            for _ in range(20):
                self.assertTrue(index.editor_state(candidate)['reviewed'])
            self.assertEqual(calculate.call_count, 1)
            config['library_pgm_editor'][path]['reviewed'] = False
            self.assertFalse(index.editor_state(candidate)['reviewed'])
            changed = copy.deepcopy(config)
            changed['library_pgm_editor'][path].update(pool='removed', reviewed=True)
            index.sessions['s'] = dict(index.sessions['s'], config=changed)
            state = index.editor_state(candidate)
            self.assertEqual(state['pool'], 'removed')
            self.assertFalse(state['reviewed'])
            self.assertEqual(calculate.call_count, 1)

    def test_new_snapshot_rechecks_asr_identity_script_and_anchor(self):
        data, path = self.fixture()
        index, candidate = self.review(data, path)
        original = index.editor_state(candidate)['coverage_fingerprint']
        for kind in ('asr', 'script', 'anchor'):
            with self.subTest(change=kind):
                changed = copy.deepcopy(data)
                if kind == 'asr':
                    changed['transcripts'][0]['id'] = 'new-asr-utterance'
                    changed['candidates'][0]['transcript_id'] = 'new-asr-utterance'
                elif kind == 'script':
                    changed['candidates'][1]['script_line_id'] = 'l12'
                else:
                    changed['transcripts'][0]['start_seconds'] += .25
                    changed['candidates'][0]['start_seconds'] += .25
                fresh = PgmTakeIndex(changed, prepared=True)
                state = fresh.editor_state(changed['candidates'][0])
                self.assertNotEqual(state['coverage_fingerprint'], original)
                self.assertFalse(state['reviewed'])

    def test_new_manual_range_and_explicit_summary_do_not_reuse_old_hash(self):
        data, path = self.fixture(manual=True)
        index, candidate = self.review(data, path)
        summary = copy.deepcopy(index.file_summary(candidate))
        summary['covered_line_ids'] = ['l10']
        self.assertFalse(index.editor_state(candidate, summary)['reviewed'])
        changed = copy.deepcopy(data)
        changed['transcripts'][0]['data']['manual_script_line_ids'] = ['l10', 'l11', 'l12']
        fresh = PgmTakeIndex(changed, prepared=True)
        self.assertFalse(fresh.editor_state(changed['candidates'][0])['reviewed'])

    def test_fingerprint_cache_has_fixed_limit_and_eviction_preserves_results(self):
        data = catalogue()
        for number in range(PgmTakeIndex.fingerprint_cache_limit + 8):
            add_take(data, 's', str(number), (10,))
        index = PgmTakeIndex(data, prepared=True)
        initial = index.editor_state(data['candidates'][0])
        for candidate in data['candidates'][1:]:
            index.editor_state(candidate)
        self.assertEqual(len(index._coverage_fingerprints), index.fingerprint_cache_limit)
        self.assertEqual(initial, index.editor_state(data['candidates'][0]))
        self.assertEqual(len(index._coverage_fingerprints), index.fingerprint_cache_limit)


class HighlightBatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_highlight_repaints_once_without_geometry_queries_or_selection_edits(self):
        view = ScriptTimeline()
        try:
            view.set_nodes(catalogue()['scripts'], selected_id='l3')
            selected = view.currentItem()
            signals = []
            view.currentItemChanged.connect(lambda *args: signals.append(args))
            model_changes = []
            view.model().dataChanged.connect(lambda *args: model_changes.append(args))
            with (patch.object(view, 'visualItemRect', side_effect=AssertionError('synchronous layout')),
                  patch.object(view.viewport(), 'update') as repaint):
                view.set_highlighted_line_ids(['l1', 'l2', 'l3'])
                self.assertEqual(repaint.call_count, 1)
                view.set_highlighted_line_ids(['l1', 'l2', 'l3'])
                self.assertEqual(repaint.call_count, 1)
                view.set_highlighted_line_ids(['l2', 'l4'])
                self.assertEqual(repaint.call_count, 2)
            self.assertIs(view.currentItem(), selected)
            self.assertEqual(signals, [])
            self.assertEqual(model_changes, [])
            self.assertEqual({identity for identity, item in view._items.items()
                              if item.data(0, COVERED_ROLE)}, {'l2', 'l4'})
        finally:
            view.close()
            view.deleteLater()
            self.app.processEvents()


if __name__ == '__main__':
    unittest.main()
