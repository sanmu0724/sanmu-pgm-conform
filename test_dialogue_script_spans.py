import copy
import unittest
from unittest.mock import patch

import dialogue_engine as engine
from dialogue_spans import SPAN_TAG_PREFIX, encode_span_tags, parse_span_tags
from pgm_takes import PgmTakeIndex


BLUE = '蓝色火车驶过寂静山谷'
RED = '红色风筝飘向遥远海岸'
WHITE = '白色帆船停在安静湖畔'


class DialogueScriptSpanTests(unittest.TestCase):
    def fixture(self, words, speech=None):
        lines = [dict(id='l' + str(i), script_id='script', scene='第一幕', order=i,
                      speaker='甲' if i % 2 == 0 else '乙', text=text)
                 for i, text in enumerate(words)]
        source = dict(id='t', session_id='session', media_path='/virtual/span.mov',
                      start_seconds=2.125, end_seconds=10.375,
                      text='，'.join(words) if speech is None else speech)
        return lines, [source]

    def match(self, lines, sources):
        return engine.match_dialogue(lines, sources, top_k=100)

    def spans(self, candidates):
        return [row for row in candidates if parse_span_tags(row)]

    def summary(self, lines, sources, candidates):
        data = dict(scripts=lines, transcripts=sources, sessions=[],
                    candidates=[dict(row, id='c' + str(i), status='candidate')
                                for i, row in enumerate(candidates)])
        return PgmTakeIndex(data, prepared=True).file_summary(sources[0])

    def test_equal_two_and_three_line_segments_no_longer_become_zero_score_ties(self):
        for words in ([BLUE, RED], [BLUE, RED, WHITE]):
            with self.subTest(lines=len(words)):
                lines, sources = self.fixture(words)
                rows = self.match(lines, sources)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['script_line_id'], 'l0')
                self.assertEqual(parse_span_tags(rows[0]), [line['id'] for line in lines])
                self.assertEqual(rows[0]['score'], 1.)
                summary = self.summary(lines, sources, rows)
                self.assertEqual(summary['match_status'], 'matched')
                self.assertTrue(summary['alignment_ready'])
                self.assertEqual(summary['covered_line_ids'], [line['id'] for line in lines])

    def test_unequal_lines_anchor_the_actual_first_line_without_retiming(self):
        lines, sources = self.fixture([BLUE, RED + '直到日落时分'])
        original = copy.deepcopy((lines, sources))
        rows = self.match(lines, sources)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['script_line_id'], 'l0')
        self.assertEqual(parse_span_tags(rows[0]), ['l0', 'l1'])
        self.assertEqual((lines, sources), original)
        self.assertNotIn('start_seconds', rows[0])
        self.assertNotIn('end_seconds', rows[0])
        summary = self.summary(lines, sources, rows)
        self.assertEqual(summary['first_candidate']['transcript_id'], 't')
        self.assertEqual(summary['covered_line_ids'], ['l0', 'l1'])

    def test_short_answer_has_to_be_supported_by_the_following_opening_words(self):
        lines, sources = self.fixture(['是的', '不是'])
        self.assertEqual(parse_span_tags(self.match(lines, sources)[0]), ['l0', 'l1'])
        lines, sources = self.fixture(['是的', RED], speech='也许，' + RED)
        self.assertEqual(self.spans(self.match(lines, sources)), [])

    def test_mixed_english_chinese_and_changed_speakers_can_share_one_asr_segment(self):
        lines, sources = self.fixture(['I will meet you at the station.', '我们会一起乘车回家'])
        rows = self.match(lines, sources)
        self.assertEqual(parse_span_tags(rows[0]), ['l0', 'l1'])
        self.assertEqual(rows[0]['script_line_id'], 'l0')

    def test_an_exact_single_line_is_never_replaced_by_a_multi_line_interpretation(self):
        lines, sources = self.fixture([BLUE, RED])
        lines.append(dict(id='whole', script_id='script', scene='第一幕', order=2,
                          text=BLUE + '，' + RED))
        rows = self.match(lines, sources)
        self.assertEqual(self.spans(rows), [])
        self.assertEqual(next(row['score'] for row in rows if row['script_line_id'] == 'whole'), 1.)

    def test_repeated_exact_spans_remain_ambiguous(self):
        lines, sources = self.fixture([BLUE, RED, BLUE, RED], speech=BLUE + '，' + RED)
        rows = self.match(lines, sources)
        self.assertEqual(self.spans(rows), [])
        summary = self.summary(lines, sources, rows)
        self.assertEqual(summary['match_score'], 0.)
        self.assertFalse(summary['alignment_ready'])

    def test_windows_never_cross_scenes_script_families_or_inactive_lines(self):
        for field, value in [('scene', '第二幕'), ('script_id', 'another'), ('active', False)]:
            with self.subTest(field=field):
                lines, sources = self.fixture([BLUE, RED])
                lines[1][field] = value
                self.assertEqual(self.spans(self.match(lines, sources)), [])

    def test_internal_stage_direction_keeps_contiguous_ids_but_cannot_anchor_a_span(self):
        lines, sources = self.fixture([BLUE, '（甲转身）', RED], speech=BLUE + '，' + RED)
        rows = self.match(lines, sources)
        self.assertEqual(parse_span_tags(rows[0]), ['l0', 'l1', 'l2'])
        lines, sources = self.fixture(['（甲转身）', BLUE, RED], speech=BLUE + '，' + RED)
        rows = self.match(lines, sources)
        self.assertEqual(parse_span_tags(rows[0]), ['l1', 'l2'])

    def test_missing_first_line_opening_cannot_manufacture_a_first_line_anchor(self):
        lines, sources = self.fixture([BLUE, RED], speech='寂静山谷，' + RED)
        self.assertEqual(self.spans(self.match(lines, sources)), [])
        lines, sources = self.fixture([BLUE, RED], speech='摄影准备开始，' + BLUE + '，' + RED)
        self.assertEqual(self.spans(self.match(lines, sources)), [])

    def test_unmatched_earlier_asr_is_not_skipped_by_a_later_successful_span(self):
        lines, sources = self.fixture([BLUE, RED])
        sources.insert(0, dict(sources[0], id='slate', start_seconds=.1, end_seconds=1., text='摄影准备开始'))
        rows = self.match(lines, sources)
        self.assertEqual(len(self.spans(rows)), 1)
        summary = self.summary(lines, sources, rows)
        self.assertFalse(summary['alignment_ready'])
        self.assertIsNone(summary['first_candidate'])

    def test_span_candidates_still_pass_through_the_existing_sequence_policy(self):
        lines, sources = self.fixture([BLUE, RED])
        original = engine._sequence_disambiguate
        with patch.object(engine, '_sequence_disambiguate', wraps=original) as sequence:
            self.match(lines, sources)
        self.assertEqual(sequence.call_count, 1)
        self.assertEqual(parse_span_tags(sequence.call_args.args[0][0]), ['l0', 'l1'])

    def test_spans_share_the_existing_per_anchor_and_session_top_k_limit(self):
        lines, sources = self.fixture([BLUE, RED])
        sources = [dict(sources[0], id='t' + str(i), media_path='/virtual/take' + str(i) + '.mov')
                   for i in range(8)]
        rows = engine.match_dialogue(lines, sources, top_k=1)
        self.assertEqual(len(self.spans(rows)), 1)
        self.assertEqual(self.spans(rows)[0]['score'], 1.)
        self.assertEqual(self.spans(rows)[0]['transcript_id'], 't0')
        for session in ('session', 'another'):
            self.assertLessEqual(sum(row['script_line_id'] == 'l0' for row in rows
                                     if next(source for source in sources if source['id'] == row['transcript_id'])['session_id'] == session), 1)
        sources[7]['session_id'] = 'another'
        self.assertEqual(len(self.spans(engine.match_dialogue(lines, sources, top_k=1))), 2)

    def test_window_length_token_count_and_shortlist_have_fixed_bounds(self):
        lines, _ = self.fixture([('token' + str(i) + ' ') * 10 for i in range(500)])
        texts = [engine.normalize_dialogue(row['text']) for row in lines]
        index = engine._ScriptSpanIndex(lines, texts)
        self.assertLessEqual(len(index.windows), 5 * len(lines))
        self.assertTrue(all(2 <= len(window[1]) <= 6 and len(window[3]) <= 400
                            for window in index.windows))
        speech = (texts[250] + ' ' + texts[251]).split()
        candidates, overflow = index.candidates(speech)
        self.assertFalse(overflow)
        self.assertLessEqual(len(candidates), 15)
        self.assertTrue(any(window[1] == ('l250', 'l251') for window in candidates))
        lines, _ = self.fixture([BLUE, RED] * 100)
        index = engine._ScriptSpanIndex(lines, [engine.normalize_dialogue(row['text']) for row in lines])
        candidates, overflow = index.candidates(engine.normalize_dialogue(BLUE + RED).split())
        self.assertTrue(overflow)
        self.assertEqual(len(candidates), index.MAX_CANDIDATES)
        lines, sources = self.fixture([BLUE, RED] * 100, speech=BLUE + RED)
        self.assertEqual(self.spans(self.match(lines, sources)), [])
        lines, sources = self.fixture(['甲' * 210, '乙' * 210])
        self.assertEqual(self.spans(self.match(lines, sources)), [])

    def test_span_tag_round_trip_and_invalid_annotations_fail_closed(self):
        tag = encode_span_tags(('第一行', 'second'))
        self.assertEqual(parse_span_tags(dict(tags=tag)), ['第一行', 'second'])
        self.assertEqual(parse_span_tags(dict(tags=tag + tag)), ['第一行', 'second'])
        for ids in ([], ['one'], ['same', 'same'], ['one', ''], ['one', 2], list('1234567')):
            self.assertEqual(encode_span_tags(ids), [])
        for tags in ('[]', [1], [SPAN_TAG_PREFIX + 'oops'],
                     [SPAN_TAG_PREFIX + '["one"]'], tag + encode_span_tags(['other', 'range']),
                     tag + [SPAN_TAG_PREFIX + 'broken']):
            self.assertEqual(parse_span_tags(dict(tags=tags)), [])
        self.assertEqual(parse_span_tags(dict(script_line_ids=['one', 'two'])), [])


if __name__ == '__main__':
    unittest.main()
