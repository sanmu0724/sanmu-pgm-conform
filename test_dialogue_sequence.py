"""Actual dialogue order disambiguates repeated lines within one PGM only."""
import copy
import unittest
from unittest.mock import patch

import dialogue_engine as engine


class DialogueSequenceTests(unittest.TestCase):
    def fixture(self):
        words = ['火车已经驶进车站了', '我真的不知道发生了什么', '把门打开我们必须马上离开',
                 '厨房里的面包已经烤熟了', '窗外的风声越来越大', '今天的天气十分晴朗',
                 '楼上的灯光忽然熄灭', '花园里的玫瑰又开了', '我真的不知道发生了什么',
                 '听到了远处传来的钟声']
        lines = [dict(id=f'l{i}', text=text, scene='第一幕') for i, text in enumerate(words)]
        items = [dict(id=f't{i}', text=words[i], session_id='day01', media_path='/qa/pgm.mov',
                      start_seconds=i * 3, end_seconds=i * 3 + 2) for i in range(3)]
        return lines, items

    @staticmethod
    def positions(rows, transcript='t1'):
        return {row['script_line_id'] for row in rows if row['transcript_id'] == transcript}

    def uncontextual(self, lines, items, **kwargs):
        with patch.object(engine, '_sequence_disambiguate', side_effect=lambda values, *args: values):
            return engine.match_dialogue(lines, items, **kwargs)

    def test_two_nearby_anchors_remove_distant_repeated_position_without_changing_scores(self):
        lines, items = self.fixture()
        before = self.uncontextual(lines, items)
        self.assertEqual(self.positions(before), {'l1', 'l8'})
        after = engine.match_dialogue(lines, items)
        self.assertEqual(self.positions(after), {'l1'})
        for row in after:
            original = next(value for value in before if (value['script_line_id'], value['transcript_id']) ==
                            (row['script_line_id'], row['transcript_id']))
            self.assertEqual(row['score'], original['score'])
        self.assertIn('连续台词顺序', next(row for row in after if row['transcript_id'] == 't1')['reason'])

    def test_first_dialogue_uses_two_following_anchors_not_another_file_clock(self):
        lines, items = self.fixture()
        items = items[1:] + [dict(items[-1], id='t3', text=lines[3]['text'], start_seconds=9, end_seconds=11)]
        self.assertEqual(self.positions(engine.match_dialogue(lines, items)), {'l1'})

    def test_last_dialogue_uses_two_preceding_anchors(self):
        lines, items = self.fixture()
        lines[1]['text'], lines[2]['text'] = lines[2]['text'], lines[1]['text']
        items[1]['text'], items[2]['text'] = lines[1]['text'], lines[2]['text']
        self.assertEqual(self.positions(engine.match_dialogue(lines, items), 't2'), {'l2'})

    def test_restart_with_reverse_anchor_positions_keeps_both_positions(self):
        lines, items = self.fixture()
        items[0]['text'], items[2]['text'] = lines[9]['text'], lines[0]['text']
        self.assertEqual(self.positions(engine.match_dialogue(lines, items)), {'l1', 'l8'})

    def test_never_borrows_context_from_another_source_or_performance(self):
        for boundary in ('media_path', 'session_id'):
            lines, items = self.fixture()
            items[0][boundary], items[2][boundary] = 'other-source-0', 'other-source-2'
            with self.subTest(boundary=boundary):
                self.assertEqual(self.positions(engine.match_dialogue(lines, items)), {'l1', 'l8'})

    def test_long_gap_overlapping_or_missing_ranges_provide_no_ordering_evidence(self):
        for mode in ('gap', 'overlap', 'missing'):
            lines, items = self.fixture()
            if mode == 'gap':
                items[0].update(start_seconds=0, end_seconds=1)
                items[1].update(start_seconds=20, end_seconds=21)
                items[2].update(start_seconds=40, end_seconds=41)
            elif mode == 'overlap':
                items[0]['end_seconds'] = 4
                items[1]['end_seconds'] = 7
            else:
                for item in items:
                    item.pop('end_seconds')
            with self.subTest(mode=mode):
                self.assertEqual(self.positions(engine.match_dialogue(lines, items)), {'l1', 'l8'})

    def test_one_side_with_only_one_anchor_is_not_enough(self):
        lines, items = self.fixture()
        self.assertEqual(self.positions(engine.match_dialogue(lines, items[1:])), {'l1', 'l8'})

    def test_nearby_competing_positions_remain_explicit(self):
        lines, items = self.fixture()
        lines[3]['text'] = lines[1]['text']
        items = items[:2] + [dict(items[2], text=lines[4]['text'])]
        self.assertEqual(self.positions(engine.match_dialogue(lines, items)), {'l1', 'l3'})

    def test_single_wrong_position_contradicted_by_two_anchors_returns_to_review(self):
        lines, items = self.fixture()
        lines[1]['text'] = '沉重的行李放在大厅里'
        lines[8]['text'] = '不知道'
        self.assertEqual(self.positions(engine.match_dialogue(lines, items)), set())

    def test_isolated_exact_outlier_cannot_override_two_consistent_neighbouring_anchors(self):
        lines, items = self.fixture()
        lines[1]['text'] = '沉重的行李放在大厅里'
        self.assertEqual(self.positions(self.uncontextual(lines, items)), {'l8'})
        self.assertEqual(self.positions(engine.match_dialogue(lines, items)), set())

    def test_all_positions_outside_anchors_do_not_invent_a_new_match(self):
        lines, items = self.fixture()
        lines[1]['text'] = '沉重的行李放在大厅里'
        lines.append(dict(id='l10', text=lines[8]['text'], scene='第一幕'))
        self.assertEqual(self.positions(engine.match_dialogue(lines, items)), set())

    def test_joint_paragraph_context_defeats_distant_short_containment_hit(self):
        lines = [dict(id=f'l{i}', text=text) for i, text in enumerate([
            'Jack叔叔好认真啊有时候看他那么认真我还只当他不太舒服呢',
            '你的Jack叔叔身体好得很作为监护人他举止庄重在他这个年纪能做到这样是很值得称赞的',
            '怪不得他总是一本正经的样子', '厨房里的面包已经烤熟了',
            '窗外的风声越来越大', '今天的天气十分晴朗', '楼上的灯光忽然熄灭',
            '花园里的玫瑰又开了', '你的监护人?'])]
        words = ['Jack叔说好认真呀', '有时候看他那么认真', '不太舒服呢',
                 '你的监护人呢身体好得很', '举止端庄', '在这个年纪能够做到这个样子',
                 '那是很值得人称赞的', '怪不得他总是一副一分正经的样子']
        items = [dict(id=f't{i}', text=text, session_id='s', media_path='/qa/pgm',
                      start_seconds=i * 3, end_seconds=i * 3 + 2) for i, text in enumerate(words)]
        before = self.uncontextual(lines, items)
        self.assertIn('l8', self.positions(before, 't3'))
        rows = engine.match_dialogue(lines, items)
        self.assertEqual(self.positions(rows, 't3'), {'l1'})
        kept = next(row for row in rows if row['transcript_id'] == 't3')
        original = next(row for row in before if row['transcript_id'] == 't3' and row['script_line_id'] == 'l1')
        self.assertEqual(kept['score'], original['score'])

    def test_unique_short_dialogue_still_uses_user_sixty_percent_rule(self):
        rows = engine.match_dialogue([dict(id='yes', text='好')],
                                    [dict(id='t', text='好', session_id='s', media_path='/qa/x',
                                          start_seconds=0, end_seconds=1)], threshold=.6)
        self.assertEqual(rows[0]['script_line_id'], 'yes')
        self.assertEqual(rows[0]['score'], 1.)

    def test_cross_scene_anchors_do_not_force_a_spanning_order(self):
        lines, items = self.fixture()
        lines[2]['scene'] = '第二幕'
        self.assertEqual(self.positions(engine.match_dialogue(lines, items)), {'l1', 'l8'})

    def test_inputs_remain_unchanged_including_source_times_and_metadata(self):
        lines, items = self.fixture()
        for item in items:
            item['metadata'] = dict(sound_roll='SR001', timecode='10:00:00:00')
        before = copy.deepcopy((lines, items))
        engine.match_dialogue(lines, items)
        self.assertEqual((lines, items), before)


if __name__ == '__main__':
    unittest.main()
