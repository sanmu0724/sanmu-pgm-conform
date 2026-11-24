import unittest
from fractions import Fraction

from sync_candidates import plan_waveform_candidates


RATE = 48000


def pgm(clock='12:00:00:00', seconds=30, **extra):
    return dict(path='pgm.mov', start_tc=clock, frames=round(seconds * 25), **extra)


def wav(name, seconds, length=30, **extra):
    return dict(path=name, start=round(seconds * RATE), samples=round(length * RATE),
                sample_rate=RATE, has_timecode=True, **extra)


class SourceClockCandidatesTests(unittest.TestCase):
    def test_simultaneous_start_orders_near_before_hour_distant(self):
        far = wav('far', 13 * 3600)
        near = wav('near', 12 * 3600 + 2)
        result = plan_waveform_candidates(pgm(), [far, near], 25)
        self.assertEqual(result['primary'], [near])
        self.assertEqual(result['deferred'], [far])
        self.assertIs(result['primary'][0], near)
        self.assertTrue(result['used_timecode'])

    def test_each_sequential_pgm_gets_own_recording(self):
        sources = [wav('one', 12 * 3600, 120), wav('two', 13 * 3600, 120),
                   wav('three', 14 * 3600, 120)]
        for index, hour in enumerate((12, 13, 14)):
            result = plan_waveform_candidates(pgm(f'{hour}:00:02:00', 110), sources, 25)
            self.assertEqual(result['primary'], [sources[index]])
            self.assertEqual(len(result['deferred']), 2)

    def test_long_wav_interval_prioritized_even_when_start_far_away(self):
        continuing = wav('long', 11 * 3600, 7200)
        separate = wav('later', 15 * 3600)
        result = plan_waveform_candidates(pgm(), [separate, continuing], 25)
        self.assertEqual(result['primary'], [continuing])

    def test_known_dates_cross_midnight(self):
        next_day = wav('next_day', 3, 15, recording_date='2026-10-10')
        wrong_day = wav('wrong_day', 3, 15, recording_date='2026-10-09')
        result = plan_waveform_candidates(
            pgm('23:59:55:00', 25, recording_date='2026-10-09'), [wrong_day, next_day], 25)
        self.assertEqual(result['primary'], [next_day])
        self.assertEqual(result['deferred'], [wrong_day])
        self.assertTrue(all(detail['clock_mode'] == 'dated' for detail in result['details']))

    def test_known_wrong_date_not_wrapped_into_match(self):
        yesterday = wav('yesterday', 12 * 3600, recording_date='2026-10-08')
        result = plan_waveform_candidates(pgm(recording_date='2026-10-09'), [yesterday], 25)
        self.assertFalse(result['primary'])
        self.assertEqual(result['deferred'], [yesterday])

    def test_unknown_dates_allow_adjacent_midnight_as_candidate_only(self):
        source = wav('after_midnight', 3, 15)
        result = plan_waveform_candidates(pgm('23:59:55:00', 25), [source], 25)
        self.assertEqual(result['primary'], [source])
        self.assertEqual(result['details'][0]['clock_mode'], 'adjacent_midnight_date_unknown')

    def test_unwrapped_bwf_reference_never_modulo_day(self):
        source = wav('unwrapped', 36 * 3600)
        result = plan_waveform_candidates(pgm(), [source], 25)
        self.assertEqual(result['deferred'], [source])
        self.assertEqual(result['details'][0]['gap_seconds'], 86370)

    def test_unwrapped_reference_with_date_can_cover_next_day(self):
        source = wav('unwrapped', 36 * 3600, recording_date='2026-10-09')
        result = plan_waveform_candidates(pgm(recording_date='2026-10-10'), [source], 25)
        self.assertEqual(result['primary'], [source])

    def test_missing_pgm_tc_preserves_full_original_order(self):
        sources = [wav('first', 13 * 3600), wav('second', 12 * 3600)]
        for block in (pgm(''), pgm('garbage'), pgm(has_timecode=False)):
            result = plan_waveform_candidates(block, sources, 25)
            self.assertEqual(result['primary'], sources)
            self.assertFalse(result['deferred'])
            self.assertFalse(result['used_timecode'])

    def test_missing_wav_tc_remains_primary_and_no_source_lost(self):
        far = wav('far', 15 * 3600)
        unknown = dict(path='unknown', start=0, samples=RATE * 30, has_timecode=False)
        near = wav('near', 12 * 3600 + 1)
        result = plan_waveform_candidates(pgm(), [far, unknown, near], 25)
        self.assertEqual(result['primary'], [near, unknown])
        self.assertEqual(result['deferred'], [far])
        self.assertEqual(len(result['primary']) + len(result['deferred']), 3)

    def test_no_near_candidate_retains_fallback_and_reason(self):
        source = wav('hour_far', 14 * 3600)
        result = plan_waveform_candidates(pgm(), [source], 25)
        self.assertEqual(result['reason'], 'no_near_source_clock_candidates')
        self.assertEqual(result['deferred'], [source])

    def test_overlapping_files_all_remain_primary(self):
        sources = [wav('left', 12 * 3600 - 10, 100), wav('right', 12 * 3600, 100)]
        result = plan_waveform_candidates(pgm(), sources, 25)
        self.assertEqual(result['primary'], list(reversed(sources)))
        self.assertFalse(result['deferred'])

    def test_tolerance_boundary_is_inclusive(self):
        source = wav('edge', 12 * 3600 + 90)
        result = plan_waveform_candidates(pgm(), [source], 25)
        self.assertEqual(result['primary'], [source])
        result = plan_waveform_candidates(pgm(), [source], 25, tolerance_seconds=59)
        self.assertEqual(result['deferred'], [source])

    def test_invalid_tc_metadata_is_unknown_not_excluded(self):
        bad = wav('bad', 0)
        bad['start'] = float('nan')
        result = plan_waveform_candidates(pgm(), [bad], Fraction(25))
        self.assertEqual(result['primary'], [bad])
        self.assertFalse(result['used_timecode'])

    def test_input_dictionaries_not_mutated(self):
        block, source = pgm(recording_date='2026/10/09'), wav('source', 12 * 3600, recording_date='20261009')
        before_block, before_source = dict(block), dict(source)
        plan_waveform_candidates(block, [source], 25)
        self.assertEqual(block, before_block)
        self.assertEqual(source, before_source)

    def test_invalid_tolerance_rejected(self):
        for tolerance in (-1, float('inf'), None):
            with self.assertRaises(ValueError):
                plan_waveform_candidates(pgm(), [], 25, tolerance_seconds=tolerance)

    def test_expected_offset_signed_before_and_after_pgm(self):
        before = wav('before', 12 * 3600 - 2)
        after = wav('after', 12 * 3600 + 3)
        result = plan_waveform_candidates(pgm(), [before, after], 25)
        self.assertEqual([row['expected_offset_seconds'] for row in result['details']], [-2, 3])

    def test_expected_offset_uses_midnight_translation(self):
        result = plan_waveform_candidates(pgm('23:59:55:00', 25), [wav('next', 3)], 25)
        self.assertEqual(result['details'][0]['expected_offset_seconds'], 8)
        result = plan_waveform_candidates(pgm('00:00:03:00', 25), [wav('previous', 86395)], 25)
        self.assertEqual(result['details'][0]['expected_offset_seconds'], -8)

    def test_expected_offset_uses_actual_recording_date(self):
        result = plan_waveform_candidates(pgm('23:59:55:00', 25, recording_date='2026-10-09'),
            [wav('next', 3, recording_date='2026-10-10'),
             wav('same_day', 3, recording_date='2026-10-09')], 25)
        self.assertEqual([row['expected_offset_seconds'] for row in result['details']], [8, -86392])

    def test_expected_offset_missing_for_unavailable_clock(self):
        source = wav('source', 12 * 3600)
        result = plan_waveform_candidates(pgm(''), [source], 25)
        self.assertIsNone(result['details'][0]['expected_offset_seconds'])
        source['has_timecode'] = False
        result = plan_waveform_candidates(pgm(), [source], 25)
        self.assertIsNone(result['details'][0]['expected_offset_seconds'])

    def test_expected_offset_does_not_wrap_multiday_reference(self):
        result = plan_waveform_candidates(pgm(), [wav('unwrapped', 36 * 3600)], 25)
        self.assertEqual(result['details'][0]['expected_offset_seconds'], 86400)


if __name__ == '__main__':
    unittest.main()
