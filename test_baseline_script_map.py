import copy
import time
import unittest
from unittest.mock import patch

from baseline_script_map import (baseline_map_fingerprint, build_baseline_map, build_line_index,
                                 refine_baseline_map, resolve_line, script_units, units_signature, validate_map)


def fixture(count=5):
    data = dict(sessions=[dict(id='base', config={}), dict(id='alt', config={})],
                scripts=[dict(id='L'+str(i), script_id='script', order=i,
                              text='演员台词 ' + str(i)) for i in range(1, count+1)],
                transcripts=[], candidates=[])
    cut = dict(fps=25, duration=1000, pgm_sources=[dict(path='/base.mov', start=0, end=1000, frames=1000)])
    return data, cut


def utterance(data, identity, start, end, lines, score=.95, session='base', path=None, **changes):
    from dialogue_spans import encode_span_tags
    data['transcripts'].append(dict(id='T'+identity, session_id=session, media_path=path or '/'+session+'.mov',
                                    kind='pgm', start_seconds=start, end_seconds=end, text='台词'+identity))
    row = dict(id=identity, transcript_id='T'+identity, script_line_id=lines[0], score=score,
               status='candidate', tags=['auto_dialogue']+encode_span_tags(lines))
    row.update(changes)
    data['candidates'].append(row)
    return row


def individual_units(data, _session):
    return [dict(id='u'+row['id'], line_ids=[row['id']]) for row in data['scripts'] if row.get('active', True)]


def green_range(data, identity, first, last, session='alt'):
    from pgm_editor_state import coverage_fingerprint
    from pgm_takes import PgmTakeIndex
    row = utterance(data, identity, 0, 10, [first], session=session, path='/'+identity+'.mov',
                    status='confirmed', tags=['manual_pgm_confirmed'])
    ids = [line['id'] for line in data['scripts']]
    source = data['transcripts'][-1]
    source['data'] = dict(source_kind='manual_visual', manual_range_explicit=True,
                          manual_script_line_ids=ids[ids.index(first):ids.index(last)+1])
    index = PgmTakeIndex(data)
    summary = index.file_summary(row)
    config = next(item['config'] for item in data['sessions'] if item['id'] == session)
    config.setdefault('library_pgm_editor', {})[source['media_path']] = dict(
        pool='timeline', reviewed=True, coverage_fingerprint=coverage_fingerprint(summary, source))
    return row


class BaselineMapTests(unittest.TestCase):
    def setUp(self):
        self.data, self.cut = fixture()

    def build(self):
        with patch('baseline_script_map.script_units', individual_units):
            return build_baseline_map(self.data, 'base', self.cut)

    def assert_coverage(self, mapping):
        cursor = 0
        ids = []
        for row in mapping['segments']:
            self.assertEqual(row['start_frame'], cursor)
            self.assertGreater(row['end_frame'], row['start_frame'])
            cursor = row['end_frame']
            ids.extend(row['line_ids'])
        self.assertEqual(cursor, self.cut['duration'])
        self.assertEqual(len(ids), len(set(ids)))

    def test_empty_speech_still_covers_full_baseline(self):
        mapping = self.build()
        self.assert_coverage(mapping)
        self.assertEqual(mapping['segments'][0]['line_ids'], [])
        self.assertTrue(mapping['segments'][0]['needs_review'])

    def test_same_line_adjacent_chunks_coalesce_to_one_real_anchor(self):
        for number, (first, last) in enumerate(((3, 6), (6, 9), (9, 11), (11, 13))):
            utterance(self.data, 'a'+str(number), first, last, ['L1'])
        mapping = self.build()
        rows = [row for row in mapping['segments'] if row['line_ids']]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['start_frame'], rows[0]['end_frame'], rows[0]['anchor_frame']), (75, 325, 75))
        self.assertEqual(len(rows[0]['evidence_ids']), 4)
        self.assertFalse(rows[0]['needs_review'])
        self.assert_coverage(mapping)

    def test_silent_head_tail_and_between_lines_never_shift_speech_anchors(self):
        utterance(self.data, 'a', 2, 5, ['L1'])
        utterance(self.data, 'b', 8, 10, ['L2'])
        mapping = self.build()
        index = build_line_index(mapping)
        self.assertEqual(index['L1']['anchor_frame'], 50)
        self.assertEqual(index['L2']['anchor_frame'], 200)
        self.assertEqual([row['line_ids'] for row in mapping['segments']], [[], ['L1'], [], ['L2'], []])
        self.assert_coverage(mapping)

    def test_equal_fractional_speech_edges_do_not_erase_next_real_anchor(self):
        utterance(self.data, 'a', 1.1542708333334, 3.1542708333334, ['L1'])
        utterance(self.data, 'b', 3.1542708333334, 5.1542708333334, ['L2'])
        mapping = self.build()
        first, second = resolve_line(mapping, 'L1'), resolve_line(mapping, 'L2')
        self.assertEqual(first['end_frame'], 78)
        self.assertEqual(second['start_frame'], 78)
        self.assertEqual(second['anchor_frame'], 78)
        self.assertFalse(second['needs_review'])
        self.assert_coverage(mapping)

    def test_tiny_real_overlap_remains_reviewed_despite_same_integer_geometry(self):
        utterance(self.data, 'a', 1.154, 3.169, ['L1'])
        utterance(self.data, 'b', 3.154, 5.154, ['L2'])
        mapping = self.build()
        second = resolve_line(mapping, 'L2')
        self.assertIsNone(second['anchor_frame'])
        self.assertTrue(second['needs_review'])
        self.assertEqual(second['start_frame'], 80)
        self.assert_coverage(mapping)

    def test_actual_subframe_overlap_is_not_mistaken_for_rounding_only(self):
        utterance(self.data, 'a', '1.154', '3.154', ['L1'])
        utterance(self.data, 'b', '3.153', '5.154', ['L2'])
        mapping = self.build()
        second = resolve_line(mapping, 'L2')
        self.assertTrue(second['needs_review'])
        self.assertIsNone(second['anchor_frame'])
        self.assertEqual(second['start_frame'], 79)
        self.assert_coverage(mapping)

    def test_nonoverlapping_subframe_gap_preserves_actual_floor_anchor(self):
        utterance(self.data, 'a', 1.1, 3.154, ['L1'])
        utterance(self.data, 'b', 3.159, 5.1, ['L2'])
        mapping = self.build()
        self.assertEqual(resolve_line(mapping, 'L2')['anchor_frame'], 78)
        self.assertFalse(resolve_line(mapping, 'L2')['needs_review'])
        self.assert_coverage(mapping)

    def test_subframe_utterances_in_same_video_frame_cannot_get_fake_distinct_anchors(self):
        utterance(self.data, 'a', 1.001, 1.009, ['L1'])
        utterance(self.data, 'b', 1.010, 1.019, ['L2'])
        mapping = self.build()
        self.assertTrue(resolve_line(mapping, 'L1')['needs_review'])
        self.assertIsNone(resolve_line(mapping, 'L2'))
        self.assert_coverage(mapping)

    def test_whole_rational_frame_rate_keeps_fractional_edge_arithmetic_exact(self):
        self.cut['fps'] = '25/1'
        utterance(self.data, 'a', '1.154', '3.154', ['L1'])
        utterance(self.data, 'b', '3.154', '5.154', ['L2'])
        mapping = self.build()
        self.assertEqual(mapping['fps'], 25)
        self.assertEqual(resolve_line(mapping, 'L2')['anchor_frame'], 78)
        self.assertFalse(resolve_line(mapping, 'L2')['needs_review'])

    def test_fractional_in_frame_rate_is_rejected_consistently_with_cut_workflow(self):
        self.cut['fps'] = '30000/1001'
        with self.assertRaisesRegex(ValueError, '整数帧'):
            self.build()

    def test_actual_adjacent_pgm_file_edges_keep_separate_real_speech_onsets(self):
        self.cut['pgm_sources'] = [dict(path='/part1.mov', start=0, end=500, frames=500),
                                   dict(path='/part2.mov', start=500, end=1000, frames=500)]
        utterance(self.data, 'a', 19.154, 20, ['L1'], path='/part1.mov')
        utterance(self.data, 'b', 0, .854, ['L2'], path='/part2.mov')
        mapping = self.build()
        self.assertEqual(resolve_line(mapping, 'L1')['end_frame'], 500)
        self.assertEqual(resolve_line(mapping, 'L2')['anchor_frame'], 500)
        self.assertFalse(resolve_line(mapping, 'L2')['needs_review'])
        self.assert_coverage(mapping)

    def test_unknown_coalescing_keeps_review_state_and_manual_boundary(self):
        from baseline_script_map import _coalesce_unknown
        rows = [dict(start_frame=0, end_frame=10, line_ids=[], anchor_frame=None,
                     confidence=0., needs_review=True, reason='未定位', evidence_ids=['one']),
                dict(start_frame=10, end_frame=20, line_ids=[], anchor_frame=None,
                     confidence=0., needs_review=False, reason='人工无台词', evidence_ids=['two'],manual=True),
                dict(start_frame=20, end_frame=30, line_ids=[], anchor_frame=None,
                     confidence=0., needs_review=True, reason='未定位', evidence_ids=['three'])]
        cut=dict(pgm_sources=[dict(start=0,end=30)])
        result=_coalesce_unknown(rows,cut)
        self.assertEqual(len(result),3)
        self.assertFalse(result[1]['needs_review'])
        self.assertTrue(result[1]['manual'])

    def test_thousand_unmatched_chunks_merge_per_pgm_without_touching_source_rows(self):
        self.cut = dict(fps=25, duration=50000, pgm_sources=[
            dict(path='/part1.mov', start=0, end=25000, frames=25000),
            dict(path='/part2.mov', start=25000, end=50000, frames=25000)])
        for number in range(1000):
            path = '/part1.mov' if number < 500 else '/part2.mov'
            local = number%500
            self.data['transcripts'].append(dict(id='unmatched'+str(number), session_id='base', kind='pgm',
                media_path=path, start_seconds=local*2+.1, end_seconds=local*2+1.1, text='未找到对应台词'))
        original = copy.deepcopy(self.data)
        mapping = self.build()
        self.assertEqual([(row['start_frame'],row['end_frame']) for row in mapping['segments']],[(0,25000),(25000,50000)])
        self.assertTrue(all(not row['line_ids'] and row['needs_review'] for row in mapping['segments']))
        self.assertEqual(self.data,original)
        self.assert_coverage(mapping)

    def test_cut_parts_map_source_local_positions_to_continuous_frames(self):
        self.cut['pgm_sources'] = [dict(path='/part1.mov', start=0, end=500, frames=500),
                                   dict(path='/part2.mov', start=500, end=1000, frames=500)]
        utterance(self.data, 'a', 1, 2, ['L1'], path='/part1.mov')
        utterance(self.data, 'b', 2, 3, ['L2'], path='/part2.mov')
        mapping = self.build()
        self.assertEqual(resolve_line(mapping, 'L2')['anchor_frame'], 550)
        self.assert_coverage(mapping)

    def test_same_basename_from_other_session_cannot_supply_anchor(self):
        utterance(self.data, 'a', 2, 5, ['L1'], session='alt', path='/base.mov')
        self.assertIsNone(resolve_line(self.build(), 'L1'))

    def test_backward_repeat_not_exposed_as_second_same_line_anchor(self):
        utterance(self.data, 'a', 1, 2, ['L1'])
        utterance(self.data, 'b', 3, 4, ['L2'])
        utterance(self.data, 'c', 5, 6, ['L1'])
        mapping = self.build()
        self.assertEqual(resolve_line(mapping, 'L1')['anchor_frame'], 25)
        self.assertEqual(sum('L1' in row['line_ids'] for row in mapping['segments']), 1)
        self.assertTrue(any('冲突' in row['reason'] or '重复' in row['reason'] for row in mapping['segments']))
        self.assert_coverage(mapping)

    def test_context_prefers_monotone_candidate_over_isolated_higher_score(self):
        utterance(self.data, 'a', 1, 2, ['L1'])
        first = utterance(self.data, 'b', 3, 4, ['L2'], score=.94)
        self.data['candidates'].append(dict(first, id='wrong', script_line_id='L5', score=.97))
        utterance(self.data, 'c', 5, 6, ['L3'])
        mapping = self.build()
        self.assertIsNotNone(resolve_line(mapping, 'L2'))
        self.assertIsNone(resolve_line(mapping, 'L5'))

    def test_equal_context_paths_are_reviewed_not_arbitrarily_accepted(self):
        first = utterance(self.data, 'a', 1, 2, ['L2'])
        self.data['candidates'].append(dict(first, id='tie', script_line_id='L3'))
        mapping = self.build()
        mapped = [row for row in mapping['segments'] if row['line_ids']]
        self.assertEqual(len(mapped), 1)
        self.assertTrue(mapped[0]['needs_review'])
        self.assertIn('路径', mapped[0]['reason'])

    def test_low_confidence_is_visible_but_requires_review(self):
        utterance(self.data, 'a', 1, 2, ['L1'], score=.6)
        self.assertTrue(resolve_line(self.build(), 'L1')['needs_review'])

    def test_manual_visual_range_has_no_invented_per_line_times(self):
        row = utterance(self.data, 'a', 4, 8, ['L1'], status='confirmed', tags=['manual_pgm_confirmed'])
        self.data['transcripts'][0]['data'] = dict(source_kind='manual_visual', manual_range_explicit=True,
                                                  manual_script_line_ids=['L1', 'L2'])
        mapping = self.build()
        span = next(row for row in mapping['segments'] if row['evidence_ids'])
        self.assertIsNone(resolve_line(mapping, 'L1'))
        self.assertIsNone(resolve_line(mapping, 'L2'))
        self.assertTrue(span['needs_review'])
        self.assertIsNone(span['anchor_frame'])
        self.assertIn('边界', span['reason'])

    def test_joined_asr_crossing_required_unit_boundary_requires_review(self):
        utterance(self.data, 'a', 4, 8, ['L1', 'L2'])
        mapping = self.build()
        span = next(row for row in mapping['segments'] if row['evidence_ids'])
        self.assertIsNone(resolve_line(mapping, 'L1'))
        self.assertTrue(span['needs_review'])
        self.assertIsNone(span['anchor_frame'])
        self.assertIn('边界', span['reason'])

    def test_overlapping_asr_does_not_claim_clean_second_onset(self):
        utterance(self.data, 'a', 1, 4, ['L1'])
        utterance(self.data, 'b', 3, 5, ['L2'])
        mapping = self.build()
        second = resolve_line(mapping, 'L2')
        self.assertIsNone(second['anchor_frame'])
        self.assertTrue(second['needs_review'])
        self.assert_coverage(mapping)

    def test_deactivated_script_and_asr_are_not_reused(self):
        utterance(self.data, 'old', 1, 2, ['L1'])
        utterance(self.data, 'new', 3, 4, ['L2'])
        self.data['scripts'][0]['active'] = False
        self.data['transcripts'][1]['active'] = False
        self.assertIsNone(resolve_line(self.build(), 'L1'))
        self.assertIsNone(resolve_line(self.build(), 'L2'))

    def test_baseline_removed_from_alt_tracks_does_not_remove_baseline_evidence(self):
        utterance(self.data, 'a', 1, 2, ['L1'])
        self.data['sessions'][0]['config']['library_pgm_editor'] = {'/base.mov': dict(pool='removed')}
        self.assertEqual(resolve_line(self.build(), 'L1')['anchor_frame'], 25)

    def test_no_media_or_asr_is_opened(self):
        utterance(self.data, 'a', 1, 2, ['L1'])
        with patch('builtins.open', side_effect=AssertionError('media opened')):
            mapping = self.build()
        self.assertEqual(resolve_line(mapping, 'L1')['anchor_frame'], 25)

    def test_fingerprint_ignores_notes_green_state_and_alternative_asr(self):
        utterance(self.data, 'a', 1, 2, ['L1'])
        before = baseline_map_fingerprint(self.data, 'base', self.cut)
        self.data['candidates'][0]['note'] = '仅备注'
        self.data['sessions'][0]['config']['library_pgm_editor'] = {'/base.mov': dict(pool='removed')}
        utterance(self.data, 'other', 2, 4, ['L1'], session='alt')
        self.assertEqual(before, baseline_map_fingerprint(self.data, 'base', self.cut))

    def test_fingerprint_changes_with_actual_alignment_evidence(self):
        utterance(self.data, 'a', 1, 2, ['L1'])
        before = baseline_map_fingerprint(self.data, 'base', self.cut)
        for change in ('text', 'time', 'candidate', 'script', 'cut'):
            data, cut = copy.deepcopy(self.data), copy.deepcopy(self.cut)
            if change == 'text': data['transcripts'][0]['text'] += '更改'
            if change == 'time': data['transcripts'][0]['start_seconds'] = 1.04
            if change == 'candidate': data['candidates'][0]['score'] = .75
            if change == 'script': data['scripts'][0]['text'] += '更改'
            if change == 'cut': cut['pgm_sources'][0]['start_tc'] = '20:00:00:00'
            with self.subTest(change=change):
                self.assertNotEqual(before, baseline_map_fingerprint(data, 'base', cut))

    def test_registered_media_header_changes_invalidate_without_filesystem_probe(self):
        before = baseline_map_fingerprint(self.data, 'base', self.cut)
        self.data['sessions'][0]['config']['library_media'] = [dict(kind='pgm', path='/base.mov',
            probe=dict(frames=1000, fps=25, start_tc='10:00:00:00', size=1234))]
        self.assertNotEqual(before, baseline_map_fingerprint(self.data, 'base', self.cut))

    def test_validator_rejects_overlap_gap_duplicate_and_nonmonotone_lines(self):
        utterance(self.data, 'a', 1, 2, ['L1'])
        utterance(self.data, 'b', 3, 4, ['L2'])
        mapping = self.build()
        for change in ('gap', 'overlap', 'duplicate', 'reverse', 'outside_anchor', 'end'):
            altered = copy.deepcopy(mapping)
            if change == 'gap': altered['segments'][1]['start_frame'] += 1
            if change == 'overlap': altered['segments'][1]['start_frame'] -= 1
            if change == 'duplicate': altered['segments'][3]['line_ids'] = ['L1']
            if change == 'reverse':
                altered['segments'][1]['line_ids'] = ['L2']
                altered['segments'][3]['line_ids'] = ['L1']
            if change == 'outside_anchor': altered['segments'][1]['anchor_frame'] = 0
            if change == 'end': altered['segments'][-1]['end_frame'] -= 1
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_map(altered, self.data, self.cut, 'base')

    def test_validator_accepts_human_reviewed_empty_action_range(self):
        mapping = self.build()
        mapping['segments'][0].update(needs_review=False, confidence=1., reason='人工确认无台词')
        self.assertFalse(validate_map(mapping, self.data, self.cut, 'base')['segments'][0]['needs_review'])

    def test_validator_rejects_stale_map_and_wrong_session(self):
        mapping = self.build()
        with self.assertRaisesRegex(ValueError, '场次'):
            validate_map(mapping, self.data, self.cut, 'alt')
        self.data['scripts'][0]['text'] += '改版'
        with self.assertRaisesRegex(ValueError, '变化'):
            validate_map(mapping, self.data, self.cut, 'base')

    def test_build_is_deterministic_and_does_not_modify_source_data(self):
        utterance(self.data, 'a', 1, 2, ['L1'])
        original = copy.deepcopy(self.data)
        first, second = self.build(), self.build()
        self.assertEqual(first, second)
        self.assertEqual(self.data, original)

    def test_long_baseline_uses_bounded_context_beam(self):
        count = 1200
        self.data, self.cut = fixture(count)
        self.cut['duration'] = count*50
        self.cut['pgm_sources'][0].update(end=count*50, frames=count*50)
        for number in range(count):
            utterance(self.data, 'a'+str(number), number*2, number*2+1, ['L'+str(number+1)])
        start = time.monotonic()
        mapping = self.build()
        self.assertLess(time.monotonic()-start, 6.)
        self.assert_coverage(mapping)

    def test_relative_position_disambiguates_equal_short_lines_as_a_soft_prior(self):
        first = utterance(self.data, 'a', 34, 35, ['L1'])
        self.data['candidates'].append(dict(first, id='late', script_line_id='L5'))
        mapping = self.build()
        self.assertIsNone(resolve_line(mapping, 'L1'))
        late = resolve_line(mapping, 'L5')
        self.assertFalse(late['needs_review'])
        self.assertLess(late['position_delta'], .1)
        self.assertIn('辅助排序', late['reason'])

    def test_position_shortlist_keeps_late_identical_line_among_many_candidates(self):
        self.data, self.cut = fixture(30)
        first = utterance(self.data, 'a', 38, 39, ['L1'])
        for number in range(2, 31):
            self.data['candidates'].append(dict(first, id='option'+str(number), script_line_id='L'+str(number)))
        mapping = self.build()
        self.assertIsNotNone(resolve_line(mapping, 'L29'))
        self.assertIsNone(resolve_line(mapping, 'L1'))

    def test_position_prior_does_not_override_strong_unique_text_after_long_head_silence(self):
        first = utterance(self.data, 'a', 34, 35, ['L1'], score=.98)
        self.data['candidates'].append(dict(first, id='weak-end', script_line_id='L5', score=.65))
        mapping = self.build()
        line = resolve_line(mapping, 'L1')
        self.assertEqual(line['anchor_frame'], 850)
        self.assertFalse(line['needs_review'])
        self.assertIsNone(resolve_line(mapping, 'L5'))

    def test_position_prior_cannot_raise_low_lexical_confidence(self):
        utterance(self.data, 'a', 32, 33, ['L5'], score=.4)
        row = resolve_line(self.build(), 'L5')
        self.assertTrue(row['needs_review'])
        self.assertEqual(row['confidence'], .4)

    def test_manual_line_does_not_move_to_better_position_prior(self):
        first = utterance(self.data, 'a', 34, 35, ['L1'], score=.1, status='confirmed', tags=['human'])
        self.data['candidates'].append(dict(first, id='machine-end', script_line_id='L5', score=.99,
                                           status='candidate', tags=['auto_dialogue']))
        self.assertIsNotNone(resolve_line(self.build(), 'L1'))
        self.assertIsNone(resolve_line(self.build(), 'L5'))

    def test_unequal_script_lengths_use_text_weight_not_row_number(self):
        self.data['scripts'][0]['text'] = '很长的台词' * 100
        first = utterance(self.data, 'a', 36, 37, ['L2'])
        self.data['candidates'].append(dict(first, id='end', script_line_id='L5'))
        mapping = self.build()
        chosen = next(row for row in mapping['segments'] if row['line_ids'])
        self.assertGreater(chosen['position_score'], .8)



class BaselineGranularityTests(unittest.TestCase):
    def test_no_green_fallback_keeps_existing_script_line_units(self):
        data, _cut = fixture(5)
        self.assertEqual([row['line_ids'] for row in script_units(data, 'base')],
                         [['L1'], ['L2'], ['L3'], ['L4'], ['L5']])

    def test_green_ranges_define_smaller_boundaries_and_exclude_baseline(self):
        data, _cut = fixture(8)
        green_range(data, 'whole', 'L2', 'L7')
        green_range(data, 'short', 'L4', 'L5')
        green_range(data, 'baseline', 'L3', 'L3', session='base')
        units = script_units(data, 'base')
        self.assertEqual([row['line_ids'] for row in units], [['L1'], ['L2', 'L3'], ['L4', 'L5'], ['L6', 'L7'], ['L8']])

    def test_removed_and_blue_ranges_do_not_refine_units(self):
        data, _cut = fixture(8)
        green_range(data, 'whole', 'L2', 'L7')
        green_range(data, 'short', 'L4', 'L5')
        config = data['sessions'][1]['config']['library_pgm_editor']
        config['/short.mov']['reviewed'] = False
        self.assertEqual([row['line_ids'] for row in script_units(data, 'base')], [['L1'], ['L2','L3','L4','L5','L6','L7'], ['L8']])
        config['/whole.mov']['pool'] = 'removed'
        self.assertEqual(len(script_units(data, 'base')), 8)

    def test_shorter_later_take_changes_units_not_speech_fingerprint(self):
        data, cut = fixture(8)
        green_range(data, 'whole', 'L2', 'L7')
        old = script_units(data, 'base')
        fingerprint = baseline_map_fingerprint(data, 'base', cut)
        green_range(data, 'short', 'L4', 'L5')
        self.assertNotEqual(units_signature(old), units_signature(script_units(data, 'base')))
        self.assertEqual(fingerprint, baseline_map_fingerprint(data, 'base', cut))

    def test_existing_asr_is_grouped_to_units_with_real_unit_start_anchor(self):
        data, cut = fixture(5)
        green_range(data, 'whole', 'L2', 'L4')
        for number in range(1, 6):
            utterance(data, 'b'+str(number), number*2, number*2+1, ['L'+str(number)])
        mapping = build_baseline_map(data, 'base', cut)
        unit = resolve_line(mapping, 'L2')
        self.assertEqual(unit['line_ids'], ['L2', 'L3', 'L4'])
        self.assertEqual(unit['anchor_frame'], 100)
        self.assertEqual(unit['end_frame'], 225)
        self.assertFalse(unit['needs_review'])
        self.assertEqual(mapping['units_signature'], units_signature(script_units(data, 'base')))

    def test_missing_unit_start_does_not_use_later_line_as_precise_anchor(self):
        data, cut = fixture(5)
        green_range(data, 'whole', 'L2', 'L4')
        utterance(data, 'b', 4, 5, ['L3'])
        mapping = build_baseline_map(data, 'base', cut)
        unit = resolve_line(mapping, 'L2')
        self.assertIsNone(unit['anchor_frame'])
        self.assertTrue(unit['needs_review'])

    def test_refine_reuses_saved_asr_for_new_shorter_take(self):
        data, cut = fixture(5)
        green_range(data, 'whole', 'L1', 'L5')
        for number in range(1, 6):
            utterance(data, 'b'+str(number), number*2, number*2+1, ['L'+str(number)])
        original = build_baseline_map(data, 'base', cut)
        self.assertEqual(resolve_line(original, 'L1')['line_ids'], ['L1', 'L2', 'L3', 'L4', 'L5'])
        green_range(data, 'short', 'L3', 'L4')
        refined = refine_baseline_map(original, data, 'base', cut)
        self.assertEqual(resolve_line(refined, 'L3')['line_ids'], ['L3', 'L4'])
        self.assertEqual(resolve_line(refined, 'L3')['anchor_frame'], 150)
        self.assertFalse(refined['refinement_needed'])

    def test_refine_preserves_manual_anchor_and_boundary_when_new_split_is_not_safe(self):
        data, cut = fixture(5)
        green_range(data, 'whole', 'L1', 'L5')
        for number in range(1, 6):
            utterance(data, 'b'+str(number), number*2, number*2+1, ['L'+str(number)])
        original = build_baseline_map(data, 'base', cut)
        row = resolve_line(original, 'L1')
        row.update(manual=True, anchor_frame=52)
        green_range(data, 'short', 'L3', 'L4')
        refined = refine_baseline_map(original, data, 'base', cut)
        kept = resolve_line(refined, 'L1')
        self.assertEqual(kept['anchor_frame'], 52)
        self.assertEqual((kept['start_frame'], kept['end_frame']), (row['start_frame'], row['end_frame']))
        self.assertTrue(refined['refinement_needed'])
        self.assertTrue(kept['refinement_needed'])
        validate_map(refined, data, cut, 'base')

    def test_joined_asr_cross_unit_after_other_evidence_never_duplicates_lines(self):
        data, cut = fixture(5)
        green_range(data, 'short', 'L1', 'L2')
        utterance(data, 'a', 1, 2, ['L1'])
        utterance(data, 'b', 3, 4, ['L2', 'L3'])
        utterance(data, 'c', 5, 6, ['L4'])
        mapping = build_baseline_map(data, 'base', cut)
        ids = [identity for row in mapping['segments'] for identity in row['line_ids']]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(any(row['needs_review'] and '边界' in row['reason'] for row in mapping['segments']))



if __name__ == '__main__':
    unittest.main()
