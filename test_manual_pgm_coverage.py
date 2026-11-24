"""Manual script coverage is descriptive; only one real source anchor aligns."""
import copy
import unittest

from library_timeline import build_timeline_data
from pgm_intervals import build_pgm_intervals
from pgm_takes import PgmTakeIndex
from script_coverage import ScriptCoverageIndex
from test_pgm_intervals import catalogue, add_take


class ManualPgmCoverageTests(unittest.TestCase):
    def data(self, ids, location='data'):
        data = catalogue()
        add_take(data, 'backup', 'manual', (54,), manual=True)
        source = data['transcripts'][0]
        if location == 'nested':
            source['data']['metadata'] = dict(manual_script_line_ids=ids)
        else:
            source.setdefault(location, {})['manual_script_line_ids'] = ids
        return data

    def summary(self, data):
        return PgmTakeIndex(data).file_summary(data['candidates'][0])

    def test_single_line_manual_location_retains_unique_source_anchor(self):
        data = self.data(['l54'])
        summary = self.summary(data)
        self.assertEqual(summary['covered_line_ids'], ['l54'])
        self.assertTrue(summary['alignment_ready'])
        self.assertEqual(summary['first_candidate']['script_line_id'], 'l54')
        self.assertEqual(len(summary['coverage_candidates']), 1)

    def test_range_55_to_76_expands_display_only_without_creating_candidates(self):
        ids = ['l' + str(n) for n in range(54, 76)]
        data = self.data(ids)
        original = copy.deepcopy(data)
        summary = self.summary(data)
        node = build_pgm_intervals(data)[0]
        self.assertEqual(summary['covered_line_ids'], ids)
        self.assertEqual(node['covered_line_ids'], ids)
        self.assertEqual(node['clips'][0]['covered_line_ids'], ids)
        self.assertEqual((node['coverage_start'], node['coverage_end']), (54, 75))
        self.assertEqual((node['clips'][0]['start_seconds'], node['clips'][0]['end_seconds']), (0, 20))
        self.assertEqual(len(data['candidates']), 1)
        self.assertEqual(summary['first_candidate']['script_line_id'], 'l54')
        self.assertEqual(data, original)

    def test_manual_range_may_cross_scene_within_same_script(self):
        data = self.data(['l54', 'l55', 'l56'])
        data['scripts'][55]['scene'] = '第二幕'
        data['scripts'][56]['scene'] = '第三幕'
        self.assertEqual(self.summary(data)['covered_line_ids'], ['l54', 'l55', 'l56'])

    def test_metadata_and_nested_metadata_are_accepted(self):
        for location in ('metadata', 'nested'):
            with self.subTest(location=location):
                self.assertEqual(self.summary(self.data(['l54', 'l55'], location))['covered_line_ids'], ['l54', 'l55'])

    def test_stale_deleted_inactive_other_script_and_discontinuous_ids_fail_closed(self):
        cases = (['l54', 'missing'], ['l54', 'l56'], ['l55', 'l56'], ['l53', 'l54'], '54-76', [54, 55])
        for ids in cases:
            with self.subTest(ids=ids):
                self.assertEqual(self.summary(self.data(ids))['covered_line_ids'], ['l54'])
        inactive = self.data(['l54', 'l55'])
        inactive['scripts'][55]['active'] = False
        self.assertEqual(self.summary(inactive)['covered_line_ids'], ['l54'])
        other_script = self.data(['l54', 'l55'])
        other_script['scripts'][55]['script_id'] = 'other-script'
        self.assertEqual(self.summary(other_script)['covered_line_ids'], ['l54'])

    def test_rejected_manual_candidate_clears_range_and_axis(self):
        data = self.data(['l54', 'l55', 'l56'])
        script_nodes = build_timeline_data(dict(data, candidates=[]))
        self.assertEqual(len(ScriptCoverageIndex(script_nodes, build_pgm_intervals(data)).spans), 1)
        data['candidates'][0]['status'] = 'rejected'
        self.assertEqual(self.summary(data)['covered_line_ids'], [])
        nodes = build_pgm_intervals(data)
        self.assertEqual(nodes[0]['covered_line_ids'], [])
        self.assertEqual(ScriptCoverageIndex(script_nodes, nodes).spans, [])

    def test_explicit_manual_range_overrides_obsolete_automatic_coverage(self):
        data = self.data(['l54', 'l55'])
        source = copy.deepcopy(data['transcripts'][0])
        source.update(id='old-asr', start_seconds=5, end_seconds=6, text='别的台词', data={})
        data['transcripts'].append(source)
        data['candidates'].append(dict(id='old-candidate', transcript_id='old-asr', script_line_id='l90',
                                       score=.9, status='candidate', tags=['auto_dialogue']))
        self.assertEqual(self.summary(data)['covered_line_ids'], ['l54', 'l55'])

    def test_unique_explicit_manual_anchor_ignores_earlier_asr_noise_and_slate(self):
        data = self.data(['l54', 'l55', 'l56'])
        data['transcripts'][0].update(start_seconds=7.1, end_seconds=20)
        for number, text in ((1.1, '杂音'), (4.1, '开始开拍')):
            data['transcripts'].append(dict(id=str(number), session_id='backup', kind='pgm',
                media_path=data['transcripts'][0]['media_path'], start_seconds=number,
                end_seconds=number + .5, text=text))
        summary = self.summary(data)
        self.assertTrue(summary['alignment_ready'])
        self.assertEqual(summary['first_candidate']['id'], data['candidates'][0]['id'])
        self.assertEqual(summary['covered_line_ids'], ['l54', 'l55', 'l56'])
        clip = build_pgm_intervals(data)[0]['clips'][0]
        self.assertEqual(clip['dialogue_start_seconds'], 7.1)
        self.assertEqual((clip['start_seconds'], clip['end_seconds']), (0, 20))

    def test_multiple_explicit_manual_anchors_require_review_and_never_guess(self):
        data = self.data(['l54', 'l55'])
        source = copy.deepcopy(data['transcripts'][0])
        source.update(id='manual-other', start_seconds=7.1, end_seconds=20)
        source['data']['manual_script_line_ids'] = ['l60', 'l61']
        data['transcripts'].append(source)
        data['candidates'].append(dict(id='manual-other-candidate', transcript_id=source['id'],
            script_line_id='l60', status='confirmed', score=0))
        summary = self.summary(data)
        self.assertEqual(summary['match_status'], 'review')
        self.assertFalse(summary['alignment_ready'])
        self.assertIsNone(summary['first_candidate'])
        self.assertIn('冲突', summary['alignment_reason'])
        with self.assertRaisesRegex(ValueError, '冲突'):
            PgmTakeIndex(data).selection_candidate(data['candidates'][0])

    def test_old_manual_visual_without_explicit_range_does_not_ignore_earlier_asr(self):
        data = self.data(['l54'])
        del data['transcripts'][0]['data']['manual_script_line_ids']
        data['transcripts'][0].update(start_seconds=7.1, end_seconds=20)
        data['transcripts'].append(dict(id='old-slate', session_id='backup', kind='pgm',
            media_path=data['transcripts'][0]['media_path'], start_seconds=4.1, end_seconds=5, text='开拍'))
        self.assertFalse(self.summary(data)['alignment_ready'])

    def test_explicit_range_preserves_real_baseline_speech_positions_inside_bounds(self):
        from library_correspondence import filter_active_transcripts
        data = self.data(['l54', 'l55', 'l56'])
        data['transcripts'][0]['data']['manual_range_explicit'] = True
        for number, line in enumerate((54, 55, 56, 90)):
            source = dict(id=f'speech-{line}', session_id='backup', kind='pgm',
                media_path=data['transcripts'][0]['media_path'], start_seconds=2+number,
                end_seconds=2.5+number, text='真实对白')
            data['transcripts'].append(source)
            data['candidates'].append(dict(id=f'spoken-{line}', transcript_id=source['id'],
                script_line_id=f'l{line}', status='candidate', score=.95))
        current = filter_active_transcripts(data)
        self.assertEqual(len(current['transcripts']), 5)
        self.assertEqual({row['id'] for row in current['candidates']},
                         {data['candidates'][0]['id'], 'spoken-55', 'spoken-56'})
        data['reference_state'] = dict(reference_session_id='backup')
        nodes = build_timeline_data(data, whole_pgm=True)
        self.assertEqual(nodes[55]['clips'][0]['start_seconds'], 3)
        self.assertEqual(nodes[56]['clips'][0]['start_seconds'], 4)
        self.assertEqual(len(nodes[54]['clips']), 1)
        self.assertEqual(nodes[54]['clips'][0]['id'], data['candidates'][0]['id'])


if __name__ == '__main__':
    unittest.main()
