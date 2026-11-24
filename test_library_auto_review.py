import copy
import unittest
from unittest.mock import patch

from library_auto_review import classify_auto_review, enrich_review_state, select_auto_confirmations


TEXT = '今天我们一起到剧场排练这一场戏'
PATH = '/NAS/Day01/PGM.mov'


def placement(first=0, last=10, wav_first=0, path='/NAS/Day01/main.wav'):
    return dict(wav_path=path, pgm_start_seconds=first, pgm_end_seconds=last,
                wav_start_seconds=wav_first, wav_end_seconds=wav_first + last - first,
                offset_seconds=first - wav_first,
                wav_info=dict(sample_rate=48000, samples=480000))


def sample(score=.95):
    return dict(
        sessions=[dict(id='s1', config={'library_sync': {PATH: dict(status='matched', placements=[placement()])}})],
        scripts=[dict(id='l1', script_id='script', active=1, text=TEXT)],
        transcripts=[dict(id='t1', session_id='s1', media_path=PATH, kind='pgm',
                          start_seconds=1, end_seconds=3, text=TEXT, data={})],
        candidates=[dict(id='c1', transcript_id='t1', script_line_id='l1',
                         score=score, status='candidate', reason='原文相同', tags=[])])


def sync(data):
    return data['sessions'][0]['config']['library_sync'][PATH]


class AutoReviewTests(unittest.TestCase):
    def test_high_score_complete_audio_eligible_and_all_inputs_untouched(self):
        data = sample();before = copy.deepcopy(data)
        result = classify_auto_review(data)
        self.assertEqual([row['id'] for row in result['eligible']], ['c1'])
        self.assertFalse(result['decisions']['c1']['review_required'])
        self.assertEqual(result['decisions']['c1']['classification_method'], 'auto_dialogue')
        self.assertEqual(data, before)
        result['eligible'][0]['tags'].append('mutated')
        self.assertEqual(data, before)

    def test_threshold_boundary_and_low_or_invalid_scores(self):
        for score in (.600001, .61, .899999, .9, 1):
            self.assertEqual(len(select_auto_confirmations(sample(score))), 1)
        for score in (.6, .599999, float('nan'), float('inf'), -1, 2, None, True):
            with self.subTest(score=score):
                result = classify_auto_review(sample(score))
                self.assertFalse(result['eligible'])
                self.assertTrue(result['decisions']['c1']['review_required'])

    def test_no_script_inactive_script_and_no_target_do_not_auto_pass(self):
        for alteration in ('none', 'inactive', 'missing'):
            data = sample()
            if alteration == 'none':data['scripts'] = []
            elif alteration == 'inactive':data['scripts'][0]['active'] = 0
            else:data['candidates'][0]['script_line_id'] = 'absent'
            self.assertFalse(classify_auto_review(data)['eligible'])

    def test_missing_audio_or_status_without_real_wav_placements_fails(self):
        cases = [{}, {'status': 'failed'}, {'status': 'matched'},
                 {'status': 'matched', 'placements': [dict(pgm_start_seconds=0, pgm_end_seconds=10)]}]
        for state in cases:
            data = sample();data['sessions'][0]['config']['library_sync'][PATH] = state
            self.assertFalse(classify_auto_review(data)['eligible'])

    def test_partial_gaps_and_a_single_missing_sample_fail(self):
        for placements in ([placement(0, 2)], [placement(2, 10)],
                           [placement(0, 2), placement(2 + 1 / 48000, 10)]):
            data = sample();sync(data)['placements'] = placements
            self.assertFalse(classify_auto_review(data)['eligible'])
        data = sample();sync(data)['uncovered'] = [dict(start_seconds=2, end_seconds=2.1)]
        self.assertFalse(classify_auto_review(data)['eligible'])

    def test_adjacent_wavs_cover_boundary_without_creating_an_overlap(self):
        data = sample();sync(data)['placements'] = [placement(0, 2), placement(2, 10, path='/NAS/Day01/main2.wav')]
        self.assertEqual(len(classify_auto_review(data)['eligible']), 1)
        data['transcripts'][0].update(start_seconds=2, end_seconds=10)
        sync(data)['uncovered'] = [dict(start_seconds=0, end_seconds=2)]
        self.assertEqual(len(classify_auto_review(data)['eligible']), 1)

    def test_overlapping_or_outside_wav_placement_is_not_success(self):
        data = sample();sync(data)['placements'] = [placement(), placement(2, 4)]
        self.assertIn('重叠', classify_auto_review(data)['review_reasons']['c1'])
        data = sample();sync(data)['placements'][0]['wav_info']['samples'] = 48000
        self.assertFalse(classify_auto_review(data)['eligible'])
        data = sample();sync(data)['placements'][0]['offset_seconds'] = 4
        self.assertFalse(classify_auto_review(data)['eligible'])

    def test_short_nonexact_dialogue_above_sixty_passes(self):
        for field in ('scripts', 'transcripts'):
            data = sample(.61);data[field][0]['text'] = '今天来剧场'
            self.assertEqual(len(classify_auto_review(data)['eligible']), 1)
        data = sample();data['scripts'][0]['text'] = data['transcripts'][0]['text'] = '今天来到剧场'
        self.assertEqual(len(classify_auto_review(data)['eligible']), 1)

    def test_unique_exact_short_dialogue_can_pass_with_real_audio(self):
        for text in ('今天来剧场', '好', 'I agree.'):
            data = sample();data['scripts'][0]['text'] = data['transcripts'][0]['text'] = text
            self.assertEqual([row['id'] for row in classify_auto_review(data)['eligible']], ['c1'])
            sync(data)['placements'] = []
            self.assertFalse(classify_auto_review(data)['eligible'])
        data = sample();data['scripts'][0]['text'] = '好！';data['transcripts'][0]['text'] = '好'
        self.assertEqual(len(classify_auto_review(data)['eligible']), 1)

    def test_repeated_or_competing_short_dialogue_does_not_auto_pass(self):
        data = sample();data['scripts'][0]['text'] = data['transcripts'][0]['text'] = '好'
        data['scripts'].append(dict(id='l2', active=1, text='好！'))
        self.assertEqual([r['id'] for r in classify_auto_review(data)['eligible']], ['c1'])
        data['scripts'][1]['text'] = '行'
        data['candidates'].append(dict(data['candidates'][0], id='other', script_line_id='l2', score=.95))
        self.assertFalse(classify_auto_review(data)['eligible'])

    def test_empty_normalized_dialogue_is_not_an_exact_short_match(self):
        data = sample();data['scripts'][0]['text'] = data['transcripts'][0]['text'] = '！。'
        self.assertFalse(classify_auto_review(data)['eligible'])

    def test_existing_match_reason_does_not_override_sixty_percent_policy(self):
        data = sample();data['scripts'].append(dict(id='l2', active=1, text=TEXT + '！'))
        self.assertEqual([r['id'] for r in classify_auto_review(data)['eligible']], ['c1'])
        for fields in ({'ambiguous': True}, {'reason': '存在剧情位置歧义'},
                       {'reason': '人物不同，请核对'}, {'reason': '须人工确认剧情位置'}):
            data = sample(.61);data['candidates'][0].update(fields)
            self.assertEqual([r['id'] for r in classify_auto_review(data)['eligible']], ['c1'])

    def test_same_transcript_other_script_competition_margin_boundary(self):
        data = sample(.98);data['scripts'].append(dict(id='l2', active=1, text='另一段完全不同的有效长台词'))
        data['candidates'].append(dict(data['candidates'][0], id='competitor', script_line_id='l2', score=.9))
        self.assertEqual([row['id'] for row in classify_auto_review(data)['eligible']], ['c1'])
        data['candidates'][1]['score'] = .97999
        self.assertEqual([r['id'] for r in classify_auto_review(data)['eligible']], ['c1'])
        data['candidates'][1]['score'] = .98
        self.assertFalse(classify_auto_review(data)['eligible'])
        data['candidates'][1]['score'] = .99
        self.assertEqual([r['id'] for r in classify_auto_review(data)['eligible']], ['competitor'])

    def test_other_confirmed_line_wins_over_high_auto_score_and_rejected_does_not_compete(self):
        data = sample(.99);data['scripts'].append(dict(id='l2', active=1, text='其他台本位置的长句子'))
        data['candidates'].append(dict(data['candidates'][0], id='other', script_line_id='l2', score=.6, status='confirmed'))
        self.assertFalse(classify_auto_review(data)['eligible'])
        data['candidates'][1]['status'] = 'rejected'
        self.assertEqual([row['id'] for row in classify_auto_review(data)['eligible']], ['c1'])

    def test_multiple_takes_of_unique_line_can_pass_matcher_cap_without_score_changes(self):
        data = sample(.84);data['candidates'][0]['reason'] = '原文相同；同场存在相近候选，须人工确认'
        data['transcripts'].append(dict(data['transcripts'][0], id='t2', start_seconds=5, end_seconds=7))
        data['candidates'].append(dict(data['candidates'][0], id='c2', transcript_id='t2'))
        before = copy.deepcopy(data);result = classify_auto_review(data)
        self.assertEqual([row['id'] for row in result['eligible']], ['c1', 'c2'])
        self.assertEqual([row['score'] for row in result['eligible']], [.84, .84])
        self.assertEqual(result['decisions']['c1']['raw_similarity'], 1)
        self.assertEqual(data, before)

    def test_multi_take_competition_uses_unique_highest_position(self):
        data = sample(.84);data['candidates'][0]['reason'] = '原文相同；同场存在相近候选，须人工确认'
        data['scripts'].append(dict(id='l2', active=1, text=TEXT))
        data['candidates'].append(dict(data['candidates'][0], id='other', script_line_id='l2'))
        self.assertFalse(classify_auto_review(data)['eligible'])
        data['scripts'][1]['text'] = '另一段完全不同的台词'
        self.assertEqual([r['id'] for r in classify_auto_review(data)['eligible']], ['c1'])

    def test_manual_visual_is_never_asr_or_auto_review(self):
        data = sample();data['transcripts'][0]['data'] = dict(source_kind='manual_visual', classification_only=True)
        self.assertFalse(classify_auto_review(data)['eligible'])
        self.assertEqual(classify_auto_review(data)['decisions']['c1']['classification_method'], 'manual_visual')

    def test_human_confirmed_or_rejected_kept_even_without_sound_or_script(self):
        for status in ('confirmed', 'rejected'):
            data = sample();data['candidates'][0].update(status=status, note='人工决定', tags=['导演保留'])
            data['sessions'][0]['config'] = {};data['scripts'] = []
            before = copy.deepcopy(data);result = classify_auto_review(data)
            self.assertFalse(result['eligible']);self.assertFalse(result['decisions']['c1']['review_required'])
            self.assertEqual(result['decisions']['c1']['classification_method'], 'manual_dialogue')
            enriched = enrich_review_state(data)
            self.assertEqual(enriched['candidates'][0]['status'], status)
            self.assertEqual(enriched['candidates'][0]['note'], '人工决定')
            self.assertEqual(data, before)

    def test_automatic_confirmed_tag_is_explanatory_not_a_new_confirmation(self):
        for tag in ('automatic_dialogue', 'auto_dialogue'):
            data = sample();data['candidates'][0].update(status='confirmed', tags=[tag])
            result = classify_auto_review(data)
            self.assertEqual(result['decisions']['c1']['classification_method'], 'auto_dialogue')
            self.assertFalse(result['eligible'])

    def test_sessions_are_isolated_even_with_same_media_filename_and_time(self):
        data = sample();other = copy.deepcopy(data['sessions'][0]);other['id'] = 's2';other['config'] = {}
        data['sessions'].append(other)
        data['transcripts'].append(dict(data['transcripts'][0], id='t2', session_id='s2'))
        data['candidates'].append(dict(data['candidates'][0], id='c2', transcript_id='t2', score=.99))
        self.assertEqual([row['id'] for row in classify_auto_review(data)['eligible']], ['c1'])

    def test_failed_current_sync_cannot_reuse_old_placements_but_asr_failure_can_keep_old_valid_result(self):
        data = sample();data['sessions'][0]['config']['library_processing'] = {PATH: dict(status='failed', stage='sync')}
        self.assertFalse(classify_auto_review(data)['eligible'])
        data['sessions'][0]['config']['library_processing'][PATH]['stage'] = 'asr'
        self.assertEqual(len(classify_auto_review(data)['eligible']), 1)

    def test_superseded_or_changed_sync_asr_is_not_eligible(self):
        data = sample();config = data['sessions'][0]['config']
        config['library_asr'] = {PATH: dict(complete=True, active_transcript_ids=[])}
        self.assertFalse(classify_auto_review(data)['eligible'])
        config['library_asr'][PATH].update(active_transcript_ids=['t1'], sync_signature='old')
        sync(data)['signature'] = 'new'
        self.assertFalse(classify_auto_review(data)['eligible'])

    def test_policy_uses_existing_text_only_without_media_or_whisper(self):
        with patch('builtins.open', side_effect=AssertionError('media I/O is forbidden')):
            self.assertEqual(len(classify_auto_review(sample())['eligible']), 1)
        for threshold, margin in ((-1, .08), (1.1, .08), (.9, -1), (.9, float('nan'))):
            with self.assertRaises((ValueError, ZeroDivisionError)):
                classify_auto_review(sample(), threshold, margin)


if __name__ == '__main__':
    unittest.main()
