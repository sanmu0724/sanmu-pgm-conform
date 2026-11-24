"""Complete split mono pairs are one coverage interval with two source records."""
import copy
from fractions import Fraction as F
import unittest

from library_auto_review import classify_auto_review
from library_correspondence import _audio


PATH = '/tmp/pair_catalogue/PGM.mov'
TEXT = '今天我们一起到剧场排练这一场戏'


def placement(role, first=0, end=10, source=0, pair='take01'):
    return dict(wav_path=f'/tmp/pair_catalogue/{pair}_{role}.wav', channel=role, pair_id=pair,
                pgm_start_seconds=first, pgm_end_seconds=end, wav_start_seconds=source,
                wav_end_seconds=source + end - first, offset_seconds=first - source,
                method='timecode', wav_info=dict(channels=1, sample_rate=48000,
                    samples=48000 * (source + end - first), has_timecode=True,
                    start_samples=19 * 3600 * 48000, start_tc='19:00:00:00',
                    filename=f'{pair}_{role}.wav', sound_roll='SOUND_' + role,
                    recording_date='2026-10-08' if role == 'L' else '2026-10-09'))


def pair(first=0, end=10, source=0, name='take01'):
    return [placement(role, first, end, source, name) for role in ('L', 'R')]


def data_for(placements, begin=1, end=3):
    return dict(sessions=[dict(id='session', config=dict(library_sync={PATH: dict(status='matched', placements=placements)}))],
                scripts=[dict(id='line', script_id='script', active=1, text=TEXT)],
                transcripts=[dict(id='transcript', session_id='session', media_path=PATH, kind='pgm',
                                  start_seconds=begin, end_seconds=end, text=TEXT, data={})],
                candidates=[dict(id='candidate', transcript_id='transcript', script_line_id='line',
                                 score=.95, status='candidate', reason='原文相同', tags=[])])


def correspondence(placements, begin=1, end=3):
    warnings = []
    result = _audio(dict(status='matched', placements=placements), F(begin), F(end), F(25), warnings)
    return result, warnings


class SplitMonoAutoReviewTests(unittest.TestCase):
    def test_complete_pair_auto_passes_and_preserves_input_metadata(self):
        data = data_for(pair())
        before = copy.deepcopy(data)
        result = classify_auto_review(data)
        self.assertEqual([row['id'] for row in result['eligible']], ['candidate'])
        self.assertFalse(result['decisions']['candidate']['review_required'])
        self.assertEqual(data, before)

    def test_adjacent_pairs_cover_boundary_without_double_counting(self):
        data = data_for(pair(0, 2) + pair(2, 10, name='take02'))
        self.assertEqual(len(classify_auto_review(data)['eligible']), 1)

    def test_pair_plus_unrelated_overlapping_main_still_requires_review(self):
        rows = pair()
        unrelated = placement('main', pair='')
        unrelated['wav_path'] = '/tmp/pair_catalogue/unrelated.wav'
        data = data_for(rows + [unrelated])
        result = classify_auto_review(data)
        self.assertFalse(result['eligible'])
        self.assertIn('重叠', result['review_reasons']['candidate'])

    def test_incomplete_duplicate_or_interval_mismatched_pair_never_auto_passes(self):
        variants = [pair()[:1], [placement('L'), placement('L')], pair()]
        variants[-1][-1]['wav_start_seconds'] = .001
        for rows in variants:
            with self.subTest(rows=rows):
                result = classify_auto_review(data_for(rows))
                self.assertFalse(result['eligible'])
                self.assertIn('配对', result['review_reasons']['candidate'])

    def test_one_member_with_invalid_source_coverage_cannot_use_the_other_to_pass(self):
        rows = pair(); rows[1]['wav_info']['samples'] = 48000
        self.assertFalse(classify_auto_review(data_for(rows))['eligible'])

    def test_failed_pair_member_cannot_pass_using_remaining_channel(self):
        rows = pair(); rows[1]['status'] = 'failed'
        self.assertFalse(classify_auto_review(data_for(rows))['eligible'])


class SplitMonoCorrespondenceTests(unittest.TestCase):
    def test_complete_pair_lists_each_original_source_without_ambiguity(self):
        rows = pair(); before = copy.deepcopy(rows)
        report, warnings = correspondence(rows)
        self.assertEqual(report['uncovered'], [])
        self.assertEqual(len(report['segments']), 2)
        self.assertFalse(any(row['ambiguous'] for row in report['segments']))
        self.assertEqual({row['channel'] for row in report['segments']}, {'L', 'R'})
        self.assertEqual({row['pair_id'] for row in report['segments']}, {'take01'})
        self.assertEqual({row['sound_roll'] for row in report['segments']}, {'SOUND_L', 'SOUND_R'})
        self.assertEqual({row['recording_date'] for row in report['segments']}, {'2026-10-08', '2026-10-09'})
        self.assertEqual({row['path'] for row in report['segments']}, {row['wav_path'] for row in rows})
        self.assertFalse(warnings)
        self.assertEqual(rows, before)

    def test_adjacent_pairs_list_four_sources_with_complete_coverage(self):
        report, warnings = correspondence(pair(0, 2) + pair(2, 10, name='take02'))
        self.assertEqual(len(report['segments']), 4)
        self.assertEqual(report['uncovered'], [])
        self.assertFalse(any(row['ambiguous'] for row in report['segments']))
        self.assertFalse(warnings)

    def test_unrelated_overlap_flags_both_pair_members_and_other_source(self):
        unrelated = placement('main', pair='')
        unrelated['wav_path'] = '/tmp/pair_catalogue/unrelated.wav'
        report, warnings = correspondence(pair() + [unrelated])
        self.assertEqual(sum(row['ambiguous'] for row in report['segments']), 3)
        self.assertTrue(any('重叠' in warning for warning in warnings))

    def test_incomplete_pair_is_listed_but_never_claims_verified_coverage(self):
        report, warnings = correspondence(pair()[:1])
        self.assertEqual(len(report['segments']), 1)
        self.assertTrue(report['segments'][0]['ambiguous'])
        self.assertEqual(report['uncovered'], [dict(start_seconds=1., end_seconds=3.)])
        self.assertTrue(any('配对' in warning for warning in warnings))

    def test_pair_with_invalid_member_metadata_does_not_count_surviving_member(self):
        rows = pair(); rows[1]['wav_info']['samples'] = 48000
        report, warnings = correspondence(rows)
        self.assertEqual(len(report['segments']), 1)
        self.assertTrue(report['segments'][0]['ambiguous'])
        self.assertEqual(report['uncovered'], [dict(start_seconds=1., end_seconds=3.)])

    def test_invalid_other_pair_does_not_discard_a_complete_valid_pair(self):
        rows = pair()[:1] + pair(10, 20, name='take02')
        report, warnings = correspondence(rows, 11, 13)
        self.assertEqual(len(report['segments']), 2)
        self.assertFalse(any(row['ambiguous'] for row in report['segments']))
        self.assertEqual(report['uncovered'], [])


if __name__ == '__main__':
    unittest.main()
