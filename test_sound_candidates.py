"""Candidate filtering must save work without replacing waveform evidence."""
import copy
import unittest
from fractions import Fraction as F
from pathlib import Path
from unittest.mock import patch

from core import AnalysisError
from sound_sync import SAMPLE_RATE, sound_plan


FPS = 25
CLOCK = 19 * 3600


def media_path(name):
    return str((Path('/private/tmp') / 'pgm_sound_candidate_test' / name).resolve())


def source_clock(seconds):
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}:00'


def pgm(name, clock=CLOCK, duration=10, record=0):
    return dict(path=media_path(name), frames=duration * FPS, start=record * FPS,
                start_tc='' if clock is None else source_clock(clock))


def wav(name, clock=CLOCK, duration=10, has_timecode=True):
    path = media_path(name)
    return dict(path=path, filename=Path(path).name,
                original_audio_filename=Path(path).name,
                samples=round(duration * SAMPLE_RATE),
                start=round(clock * SAMPLE_RATE),
                end=round((clock + duration) * SAMPLE_RATE),
                has_timecode=has_timecode, channels=1, sample_rate=SAMPLE_RATE,
                recording_date='', recording_time='', sound_roll='',
                source_stat=dict(size_bytes=100, modified_time_ns=1), metadata={})


def alignment(offset=0, score=.95):
    return dict(method='waveform', offset_seconds=offset, score=score)


class SoundCandidateIntegrationTests(unittest.TestCase):
    """Exercise the actual sound planner; substitute only media I/O/alignment."""

    def plan(self, blocks, sounds, matcher, cancelled=lambda: False, allow_gaps=False):
        config = dict(fps=FPS, sound_sync_mode='waveform',
                      sound_allow_gaps=allow_gaps,
                      sound_sources=[dict(path=row['path'], channel='main') for row in sounds])
        by_path = {row['path']: row for row in sounds}

        def probe(path, **kwargs):
            return copy.deepcopy(by_path[str(Path(path).resolve())])

        with patch('sound_sync.probe_wav', side_effect=probe), \
                patch('waveform_sync.align_audio', side_effect=matcher) as match:
            result = sound_plan(config, dict(pgm_sources=blocks), cancelled=cancelled)
            return result, match.call_args_list

    def test_three_recordings_match_only_three_nearby_sound_candidates(self):
        blocks = [pgm(f'PGM_{number}.mov', CLOCK + number * 600, record=number * 10)
                  for number in range(3)]
        sounds = [wav(f'SOUND_{number}.wav', CLOCK + number * 600, duration=10.4)
                  for number in range(3)]
        expected = [(block['path'], sound['path']) for block, sound in zip(blocks, sounds)]

        def match(reference, original, **kwargs):
            self.assertIn((reference, original), expected)
            return alignment(-.2)

        (_, events), calls = self.plan(blocks, sounds, match)
        self.assertEqual([(call.args[0], call.args[1]) for call in calls], expected)
        self.assertEqual(len(events), 3)
        for number, event in enumerate(events):
            self.assertEqual(event['record_start'], F(number * 10))
            self.assertEqual(event['duration'], F(10))
            # Source TC chooses a candidate; the measured waveform still sets sync.
            self.assertEqual(event['source_start'], F(CLOCK + number * 600) + F(1, 5))
            self.assertEqual(event['offset_seconds'], -.2)

    def test_one_pgm_can_cross_two_consecutive_wave_files(self):
        sounds = [wav('FIRST.wav'), wav('SECOND.wav', CLOCK + 10),
                  wav('UNRELATED.wav', CLOCK + 600)]

        def match(reference, original, **kwargs):
            return alignment(0 if original == sounds[0]['path'] else 10)

        (_, events), calls = self.plan([pgm('CONTINUOUS.mov', duration=20)], sounds, match)
        self.assertEqual([call.args[1] for call in calls], [row['path'] for row in sounds[:2]])
        self.assertEqual([(event['path'], event['record_start'], event['duration'])
                          for event in events],
                         [(sounds[0]['path'], F(0), F(10)),
                          (sounds[1]['path'], F(10), F(10))])

    def test_hour_clock_error_falls_back_to_actual_waveform(self):
        sounds = [wav('UNRELATED.wav', CLOCK + 3600),
                  wav('ACTUAL.wav', CLOCK + 4200)]

        def match(reference, original, **kwargs):
            if original != sounds[1]['path']:
                raise AnalysisError('波形不匹配')
            return alignment()

        (_, events), calls = self.plan([pgm('WRONG_CLOCK.mov')], sounds, match)
        self.assertEqual({call.args[1] for call in calls}, {row['path'] for row in sounds})
        self.assertEqual(len(calls), 2)
        self.assertEqual([event['path'] for event in events], [sounds[1]['path']])

    def test_nearest_clock_match_failure_searches_remaining_sounds(self):
        sounds = [wav('DEFERRED_ACTUAL.wav', CLOCK + 600), wav('NEAR_BUT_WRONG.wav')]

        def match(reference, original, **kwargs):
            if original == sounds[1]['path']:
                raise AnalysisError('台词相似但波形不匹配')
            return alignment()

        (_, events), calls = self.plan([pgm('NEAR.mov')], sounds, match)
        self.assertEqual([call.args[1] for call in calls],
                         [sounds[1]['path'], sounds[0]['path']])
        self.assertEqual([event['path'] for event in events], [sounds[0]['path']])

    def test_partial_near_match_triggers_fallback_to_fill_gap(self):
        sounds = [wav('DEFERRED_END.wav', CLOCK + 600, duration=5),
                  wav('NEAR_BEGINNING.wav', duration=5)]

        def match(reference, original, **kwargs):
            return alignment(5 if original == sounds[0]['path'] else 0)

        (_, events), calls = self.plan([pgm('PARTIAL.mov')], sounds, match)
        self.assertEqual([call.args[1] for call in calls],
                         [sounds[1]['path'], sounds[0]['path']])
        self.assertEqual([(event['path'], event['record_start'], event['duration'])
                          for event in events],
                         [(sounds[1]['path'], F(0), F(5)),
                          (sounds[0]['path'], F(5), F(5))])

    def test_clock_verified_short_start_gap_is_kept_without_searching_other_takes(self):
        sounds = [wav('NEAR_LATE_START.wav', CLOCK + 2, duration=100),
                  wav('OTHER_TAKE.wav', CLOCK + 600, duration=100)]
        (_, events), calls = self.plan([pgm('LONG_PGM.mov', duration=100)], sounds,
                                       lambda *a, **kw: alignment(2.034), allow_gaps=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(events[0]['record_start'], F(1017,500))
        self.assertEqual(events[0]['duration'], F(100)-F(1017,500))
        self.assertEqual(events[0]['offset_seconds'], 2.034)

    def test_edge_gap_clock_disagreement_still_searches_remaining_recordings(self):
        sounds = [wav('NEAR.wav', CLOCK, duration=100),
                  wav('OTHER.wav', CLOCK + 600, duration=100)]
        def match(reference, original, **kwargs):
            if original == sounds[1]['path']:
                raise AnalysisError('other take')
            return alignment(2.034)
        (_, events), calls = self.plan([pgm('DISAGREEMENT.mov', duration=100)], sounds,
                                       match, allow_gaps=True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(events[0]['offset_seconds'], 2.034)

    def test_internal_gap_is_never_treated_as_a_start_stop_gap(self):
        sounds = [wav('START.wav', CLOCK, duration=40),
                  wav('END.wav', CLOCK + 42, duration=60),
                  wav('DEFERRED_GAP.wav', CLOCK + 600, duration=2)]
        def match(reference, original, **kwargs):
            return alignment({sounds[0]['path']: 0, sounds[1]['path']: 42,
                              sounds[2]['path']: 40}[original])
        (_, events), calls = self.plan([pgm('INTERNAL.mov', duration=100)], sounds,
                                       match, allow_gaps=True)
        self.assertEqual(len(calls), 3)
        self.assertEqual([event['duration'] for event in events], [40,2,58])

    def test_all_matching_primary_candidates_are_checked_and_overlap_rejected(self):
        sounds = [wav('DUPLICATE_ONE.wav'), wav('DUPLICATE_TWO.wav')]
        calls = []

        def match(reference, original, **kwargs):
            calls.append(original)
            return alignment()

        with self.assertRaisesRegex(AnalysisError, '主声音范围重叠'):
            self.plan([pgm('OVERLAP.mov')], sounds, match)
        self.assertEqual(calls, [row['path'] for row in sounds])

    def test_missing_pgm_clock_searches_all_sounds(self):
        sounds = [wav('NO_PGM_CLOCK_ONE.wav'), wav('NO_PGM_CLOCK_TWO.wav', CLOCK + 600)]

        def match(reference, original, **kwargs):
            if original == sounds[0]['path']:
                raise AnalysisError('不匹配')
            return alignment()

        (_, events), calls = self.plan([pgm('NO_CLOCK.mov', clock=None)], sounds, match)
        self.assertEqual([call.args[1] for call in calls], [row['path'] for row in sounds])
        self.assertEqual(events[0]['path'], sounds[1]['path'])

    def test_wavs_without_clock_are_verified_in_primary_group(self):
        sounds = [wav('NO_SOUND_CLOCK_ONE.wav', clock=0, has_timecode=False),
                  wav('NO_SOUND_CLOCK_TWO.wav', clock=0, has_timecode=False),
                  wav('FAR_CLOCK.wav', CLOCK + 600)]

        def match(reference, original, **kwargs):
            if original == sounds[0]['path']:
                raise AnalysisError('不匹配')
            return alignment()

        (_, events), calls = self.plan([pgm('CLOCKED.mov')], sounds, match)
        self.assertEqual([call.args[1] for call in calls], [row['path'] for row in sounds[:2]])
        self.assertEqual(events[0]['path'], sounds[1]['path'])

    def test_cancelled_waveform_does_not_continue_to_fallback(self):
        sounds = [wav('NEAR_CANCEL.wav'), wav('FAR_NOT_SEARCHED.wav', CLOCK + 600)]
        state = {'cancelled': False}
        calls = []

        def match(reference, original, **kwargs):
            calls.append(original)
            state['cancelled'] = True
            return alignment()

        with self.assertRaisesRegex(AnalysisError, '已取消'):
            self.plan([pgm('CANCEL.mov')], sounds, match,
                      cancelled=lambda: state['cancelled'])
        self.assertEqual(calls, [sounds[0]['path']])

    def test_initial_cancellation_never_reads_or_matches_sound(self):
        with patch('sound_sync.probe_wav') as probe, \
                patch('waveform_sync.align_audio') as match:
            with self.assertRaisesRegex(AnalysisError, '已取消'):
                sound_plan(dict(fps=FPS, sound_sources=[dict(path='unused.wav')]),
                           dict(pgm_sources=[pgm('CANCEL_BEFORE_START.mov')]),
                           cancelled=lambda: True)
            probe.assert_not_called()
            match.assert_not_called()


if __name__ == '__main__':
    unittest.main()
