"""Strict split-mono grouping; source clocks and metadata remain original."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from core import AnalysisError
from sound_pairs import group_sound_placements, resolve_sound_pairs, sound_channel_hint


def wav(name, **changes):
    result = dict(path='/tmp/sound_pair_tests/' + name, channels=1, start=912345678,
                  samples=1234567, sample_rate=48000, has_timecode=True,
                  metadata=dict(bext=dict(time_reference=912345678), ixml=dict(TAPE='ROLL01')))
    result.update(changes)
    return result


def rows_for(wavs, **fields):
    return [dict(path=source['path'], **fields) for source in wavs]


class SoundPairTests(unittest.TestCase):
    def test_channel_hints_are_strict_case_insensitive_and_read_no_files(self):
        with patch.object(Path, 'open', side_effect=AssertionError('unexpected I/O')), \
                patch.object(Path, 'stat', side_effect=AssertionError('unexpected I/O')):
            for suffix, role in (('_L', 'L'), ('_R', 'R'), ('.l', 'L'), ('.r', 'R'),
                                 ('-L', 'L'), ('-R', 'R'), ('_ch1', 'L'), ('_CH2', 'R'),
                                 ('_track1', 'L'), ('_track2', 'R'), ('_1', 'L'), ('_2', 'R')):
                with self.subTest(suffix=suffix):
                    self.assertEqual(sound_channel_hint('/missing/ROLL_' + '001' + suffix + '.wav'), (role, 'ROLL_001'))
            for name in ('MAIN.wav', 'CH1.wav', 'L.wav', 'TAKE_12.wav', 'TAKE_LATER.wav', 'TAKE_CH20.wav'):
                self.assertEqual(sound_channel_hint(name), ('', ''))

    def test_ordinary_mono_and_stereo_remain_separate_main_sources(self):
        sources = [wav('main.wav'), wav('next.wav', channels=2)]
        groups = resolve_sound_pairs(rows_for(sources), sources)
        self.assertEqual([group['members'] for group in groups], [[sources[0]], [sources[1]]])
        self.assertTrue(all(source['channel'] == 'main' and source['pair_id'] == '' for source in sources))

    def test_filename_pair_reversed_import_order_keeps_left_right_source_roles(self):
        sources = [wav('TAKE_R.wav'), wav('TAKE_L.wav')]
        original = copy.deepcopy(sources)
        # Row lookup follows canonical paths rather than the wav list position.
        groups = resolve_sound_pairs(rows_for(list(reversed(sources))), sources)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['members'], [sources[1], sources[0]])
        self.assertEqual([source['channel'] for source in sources], ['R', 'L'])
        self.assertTrue(groups[0]['pair_id'])
        for current, old in zip(sources, original):
            for key, value in old.items():
                self.assertEqual(current[key], value)
        pair_id = groups[0]['pair_id']
        self.assertEqual(resolve_sound_pairs(rows_for(sources), sources)[0]['pair_id'], pair_id)

    def test_multiple_pairs_are_ordered_by_first_selected_member(self):
        sources = [wav('take2_R.wav'), wav('mono.wav'), wav('take1_L.wav'), wav('take2_L.wav'), wav('take1_R.wav')]
        groups = resolve_sound_pairs(rows_for(sources), sources)
        self.assertEqual([[Path(source['path']).name for source in group['members']] for group in groups],
                         [['take2_L.wav', 'take2_R.wav'], ['mono.wav'], ['take1_L.wav', 'take1_R.wav']])
        self.assertNotEqual(groups[0]['pair_id'], groups[2]['pair_id'])

    def test_explicit_main_disables_filename_pairing(self):
        sources = [wav('TAKE_L.wav'), wav('TAKE_R.wav')]
        groups = resolve_sound_pairs(rows_for(sources, sound_role='main'), sources)
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(source['channel'] == 'main' for source in sources))

    def test_single_filename_hint_is_valid_independent_mono(self):
        source = wav('TAKE_ch1.wav')
        groups = resolve_sound_pairs(rows_for([source]), [source])
        self.assertEqual(groups, [dict(members=[source], pair_id='')])
        self.assertEqual(source['channel'], 'main')

    def test_explicit_pair_accepts_unrelated_names_and_selected_order(self):
        sources = [wav('RECORDER_OTHER.wav', family_uid='right-family'), wav('MIC_MAIN.wav', family_uid='left-family')]
        rows = [dict(path=sources[0]['path'], sound_role='R', sound_pair='manual-001'),
                dict(path=sources[1]['path'], sound_role='L', sound_pair='manual-001')]
        groups = resolve_sound_pairs(rows, sources)
        self.assertEqual(groups, [dict(members=[sources[1], sources[0]], pair_id='manual-001')])

    def test_legacy_complete_pair_without_id_accepts_unrelated_names(self):
        sources = [wav('foo.wav'), wav('bar.wav')]
        rows = [dict(path=sources[0]['path'], channel='R'), dict(path=sources[1]['path'], channel='L')]
        groups = resolve_sound_pairs(rows, sources)
        self.assertEqual(groups[0]['members'], [sources[1], sources[0]])
        self.assertTrue(groups[0]['pair_id'])

    def test_legacy_single_role_requires_complete_pair(self):
        source = wav('single.wav')
        before = copy.deepcopy(source)
        with self.assertRaisesRegex(AnalysisError, '完整|配对'):
            resolve_sound_pairs([dict(path=source['path'], channel='L')], [source])
        self.assertEqual(source, before)

    def test_multiple_legacy_pairs_use_unambiguous_filename_families(self):
        sources = [wav('take1_L.wav'), wav('take2_R.wav'), wav('take1_R.wav'), wav('take2_L.wav')]
        rows = [dict(path=source['path'], channel=sound_channel_hint(source['path'])[0]) for source in sources]
        groups = resolve_sound_pairs(rows, sources)
        self.assertEqual(groups[0]['members'], [sources[0], sources[2]])
        self.assertEqual(groups[1]['members'], [sources[3], sources[1]])

    def test_multiple_legacy_pairs_without_family_require_manual_pair_ids(self):
        sources = [wav('alpha.wav'), wav('beta.wav'), wav('gamma.wav'), wav('delta.wav')]
        rows = [dict(path=source['path'], channel=role) for source, role in zip(sources, ('L', 'R', 'L', 'R'))]
        with self.assertRaisesRegex(AnalysisError, '手动|配对'):
            resolve_sound_pairs(rows, sources)

    def test_pair_requires_two_mono_files(self):
        for channel_count in (2, 20):
            with self.subTest(channels=channel_count):
                sources = [wav('TAKE_L.wav'), wav('TAKE_R.wav', channels=channel_count)]
                with self.assertRaisesRegex(AnalysisError, '单声道'):
                    resolve_sound_pairs(rows_for(sources), sources)

    def test_pair_requires_identical_sample_clock_length_and_timecode_status(self):
        for key, value in (('start', 912345679), ('samples', 1234568), ('sample_rate', 44100), ('has_timecode', False)):
            with self.subTest(field=key):
                sources = [wav('TAKE_L.wav'), wav('TAKE_R.wav', **{key: value})]
                before = copy.deepcopy(sources)
                with self.assertRaisesRegex(AnalysisError, '不一致'):
                    resolve_sound_pairs(rows_for(sources), sources)
                self.assertEqual(sources, before)

    def test_untimed_exact_pair_is_valid_for_waveform_sync(self):
        sources = [wav('TAKE_L.wav', start=0, has_timecode=False), wav('TAKE_R.wav', start=0, has_timecode=False)]
        self.assertEqual(len(resolve_sound_pairs(rows_for(sources), sources)), 1)
        self.assertFalse(sources[0]['has_timecode'])

    def test_auto_family_metadata_conflict_requires_manual_pairing(self):
        sources = [wav('TAKE_L.wav', family_uid='one'), wav('TAKE_R.wav', family_uid='two')]
        with self.assertRaisesRegex(AnalysisError, '系列|手动'):
            resolve_sound_pairs(rows_for(sources), sources)

    def test_duplicate_canonical_file_is_rejected(self):
        sources = [wav('TAKE.wav')]
        rows = [dict(path=sources[0]['path']), dict(path='/tmp/sound_pair_tests/../sound_pair_tests/TAKE.wav')]
        with self.assertRaisesRegex(AnalysisError, '重复'):
            resolve_sound_pairs(rows, sources)
        with self.assertRaisesRegex(AnalysisError, '重复'):
            resolve_sound_pairs(rows_for(sources), [sources[0], copy.deepcopy(sources[0])])

    def test_ambiguous_three_filename_members_and_repeated_explicit_roles_are_rejected(self):
        sources = [wav('TAKE_L.wav'), wav('TAKE_R.wav'), wav('TAKE_ch1.wav')]
        with self.assertRaisesRegex(AnalysisError, '重复|完整'):
            resolve_sound_pairs(rows_for(sources), sources)
        sources = sources[:2]
        rows = rows_for(sources, sound_role='L', sound_pair='same-pair')
        with self.assertRaisesRegex(AnalysisError, '重复|完整'):
            resolve_sound_pairs(rows, sources)

    def test_different_parent_directories_do_not_autopair(self):
        sources = [wav('TAKE_L.wav'), wav('TAKE_R.wav', path='/tmp/another_recording/TAKE_R.wav')]
        groups = resolve_sound_pairs(rows_for(sources), sources)
        self.assertEqual(len(groups), 2)

    def test_unmarked_overlapping_mono_remains_independent(self):
        sources = [wav('MIC_FOO.wav'), wav('MIC_BAR.wav')]
        self.assertEqual(len(resolve_sound_pairs(rows_for(sources), sources)), 2)

    def test_numeric_suffixes_with_different_clocks_are_sequential_main_files(self):
        for key, value in (('start', 912445678), ('samples', 1234568), ('sample_rate', 44100), ('has_timecode', False)):
            with self.subTest(field=key):
                sources = [wav('MAIN_1.wav'), wav('MAIN_2.wav', **{key: value})]
                groups = resolve_sound_pairs(rows_for(sources), sources)
                self.assertEqual(len(groups), 2)
                self.assertTrue(all(source['channel'] == 'main' and not source['pair_id'] for source in sources))

    def test_numeric_suffixes_with_same_clocks_can_pair(self):
        sources = [wav('MAIN_2.wav'), wav('MAIN_1.wav')]
        groups = resolve_sound_pairs(rows_for(sources), sources)
        self.assertEqual(groups[0]['members'], [sources[1], sources[0]])

    def test_ixml_indexes_with_different_clocks_are_not_guessed_as_split_mono(self):
        sources = [wav('one.wav', family_uid='REC-FAMILY', file_set_index='1'),
                   wav('two.wav', family_uid='REC-FAMILY', file_set_index='2', start=912445678)]
        self.assertEqual(len(resolve_sound_pairs(rows_for(sources), sources)), 2)

    def test_ixml_family_and_file_index_can_pair_unambiguous_split_mono(self):
        sources = [wav('RIGHT_UNNAMED.wav', family_uid='REC-FAMILY', file_set_index='02'),
                   wav('LEFT_UNNAMED.wav', metadata=dict(ixml={'FILE_SET/FAMILY_UID': 'REC-FAMILY', 'FILE_SET/FILE_SET_INDEX': '1'}))]
        groups = resolve_sound_pairs(rows_for(sources), sources)
        self.assertEqual(groups[0]['members'], [sources[1], sources[0]])
        self.assertEqual([source['channel'] for source in sources], ['R', 'L'])

    def test_ixml_polyphonic_family_or_missing_indexes_are_not_guessed(self):
        for count in (2, 3):
            sources = [wav(f'unnamed_{number + 10}.wav', family_uid='MULTICHANNEL', file_set_index=str(number + 1) if count == 3 else '')
                       for number in range(count)]
            self.assertEqual(len(resolve_sound_pairs(rows_for(sources), sources)), count)

    def test_duplicate_ixml_channel_indexes_are_rejected(self):
        sources = [wav('one.wav', family_uid='REC-FAMILY', file_set_index='1'),
                   wav('two.wav', family_uid='REC-FAMILY', file_set_index='1')]
        with self.assertRaisesRegex(AnalysisError, '重复|唯一'):
            resolve_sound_pairs(rows_for(sources), sources)


def placement(role='L', pair='pair01', first=0, end=10, source=3, **changes):
    result = dict(wav_path='/tmp/sound_pair_tests/TAKE_' + role + '.wav',
                  pair_id=pair, channel=role, pgm_start_seconds=first, pgm_end_seconds=end,
                  wav_start_seconds=source, wav_end_seconds=source + end - first,
                  wav_info=dict(channels=1))
    result.update(changes)
    return result


class SoundPlacementGroupsTests(unittest.TestCase):
    def test_groups_pair_once_without_mutation_and_keeps_original_row_references(self):
        right, left = placement('R'), placement('L')
        rows = [right, left]
        before = copy.deepcopy(rows)
        result = group_sound_placements(rows)
        self.assertEqual(result, [[left, right]])
        self.assertIs(result[0][0], left)
        self.assertIs(result[0][1], right)
        self.assertEqual(rows, before)

    def test_sequential_intervals_of_same_pair_are_separate_groups(self):
        left1, right1 = placement('L'), placement('R')
        left2, right2 = placement('L', first=10, end=20, source=13), placement('R', first=10, end=20, source=13)
        main = dict(wav_path='/tmp/main.wav', channel='main', pair_id='')
        groups = group_sound_placements([right1, main, right2, left1, left2])
        self.assertEqual(groups, [[left1, right1], [main], [left2, right2]])
        self.assertEqual(sum(group[0]['pgm_end_seconds'] - group[0]['pgm_start_seconds'] for group in groups if len(group) == 2), 20)

    def test_two_pgm_files_with_same_interval_do_not_mix_pairs(self):
        rows = [placement(role, pgm_path=pgm) for pgm in ('pgm1.mov', 'pgm2.mov') for role in ('L', 'R')]
        groups = group_sound_placements(rows)
        self.assertEqual(groups, [rows[:2], rows[2:]])

    def test_unpaired_main_rows_are_singletons(self):
        rows = [dict(channel='main'), dict(channel='L', pair_id=''), dict(channel='main', pair_id='')]
        self.assertEqual(group_sound_placements(rows), [[row] for row in rows])

    def test_incomplete_pair_is_rejected(self):
        with self.assertRaisesRegex(AnalysisError, '缺失|不完整'):
            group_sound_placements([placement()])

    def test_source_and_record_intervals_must_match_exactly(self):
        for field in ('pgm_start_seconds', 'pgm_end_seconds', 'wav_start_seconds', 'wav_end_seconds'):
            with self.subTest(field=field):
                right = placement('R')
                right[field] += .001
                with self.assertRaisesRegex(AnalysisError, '区间不一致|缺失'):
                    group_sound_placements([placement('L'), right])

    def test_same_pair_with_duplicate_channel_or_source_is_rejected(self):
        for right in (placement('L'), placement('R', wav_path=placement('L')['wav_path'])):
            with self.assertRaisesRegex(AnalysisError, '重复|缺失'):
                group_sound_placements([placement('L'), right])

    def test_malformed_paired_rows_are_rejected(self):
        variants = [dict(channel='main'), dict(wav_path=''), dict(pgm_start_seconds=float('nan')),
                    dict(wav_end_seconds='infinity'), dict(pgm_end_seconds=0), dict(wav_end_seconds=2),
                    dict(wav_info=dict(channels=2))]
        for changes in variants:
            with self.subTest(changes=changes), self.assertRaises(AnalysisError):
                group_sound_placements([placement('L'), placement('R', **changes)])
        row = placement('R'); row.pop('wav_start_seconds')
        with self.assertRaises(AnalysisError):
            group_sound_placements([placement('L'), row])

    def test_legacy_placement_without_wav_info_can_still_be_grouped(self):
        rows = [placement('L'), placement('R')]
        for row in rows:
            row.pop('wav_info')
        self.assertEqual(group_sound_placements(rows), [rows])

    def test_registered_placement_grouping_never_touches_storage(self):
        rows = [placement('L'), placement('R')]
        with patch.object(Path, 'resolve', side_effect=AssertionError('catalogue path read')), \
                patch.object(Path, 'stat', side_effect=AssertionError('catalogue stat')):
            self.assertEqual(group_sound_placements(rows), [rows])


if __name__ == '__main__':
    unittest.main()
