"""Dispersed PGM evidence, sentence rescue and exact window-index regressions."""
from collections import Counter
import copy
import random
import unittest
from unittest.mock import patch

import dialogue_engine as engine


class DistributedDialogueTests(unittest.TestCase):
    def fixture(self, size=90, duration=23):
        lines = [dict(id=f'l{i}', text=f'alpha{i} bravo{i} charlie{i} delta{i} echo{i} foxtrot{i}')
                 for i in range(size)]
        items = [dict(id=f't{i}', text=f'拍摄准备口令第{i}次', session_id='day01_分镜头',
                      media_path='/qa/take.mov', start_seconds=i * 3.125,
                      end_seconds=i * 3.125 + 2, metadata=dict(timecode='10:00:00:00', sound_roll='SR003'))
                 for i in range(duration)]
        return lines, items

    @staticmethod
    def assign(lines, items, item, line, text):
        lines[line]['text'] = text
        items[item]['text'] = text

    @staticmethod
    def positions(rows, item):
        return {row['script_line_id'] for row in rows if row['transcript_id'] == f't{item}'}

    def match(self, lines, items):
        return engine.match_dialogue(lines, items, top_k=len(items))

    def test_first_middle_last_evidence_resolves_distant_repeats_across_asr_gaps(self):
        lines, items = self.fixture()
        self.assign(lines, items, 1, 10, 'The midnight train has arrived beside the harbour')
        self.assign(lines, items, 11, 20, 'The gardener forgot his shovel beside the wooden fence')
        self.assign(lines, items, 21, 30, 'Tomorrow the captain will sail across the northern ocean')
        for item, line, other, words in [(6, 15, 70, '我根本不知道这件事应该怎么办'),
                                         (16, 25, 80, '你为什么一直站在那个门口等着')]:
            self.assign(lines, items, item, line, words)
            lines[other]['text'] = words
        with patch.object(engine, '_sequence_disambiguate', side_effect=lambda rows, *args: rows):
            before = self.match(lines, items)
        rows = self.match(lines, items)
        self.assertEqual(self.positions(before, 6), {'l15', 'l70'})
        self.assertEqual(self.positions(rows, 6), {'l15'})
        self.assertEqual(self.positions(rows, 16), {'l25'})
        for row in rows:
            previous = next(value for value in before if (value['script_line_id'], value['transcript_id']) ==
                            (row['script_line_id'], row['transcript_id']))
            self.assertEqual(row['score'], previous['score'])
        self.assertFalse(self.positions(rows, 0))  # Opening production cue is not invented as dialogue.

    def test_recorded_restart_uses_independent_supported_runs(self):
        lines, items = self.fixture()
        for item, line, words in [
                (0, 30, 'The midnight train has arrived beside the harbour'),
                (4, 40, 'The gardener forgot his shovel beside the wooden fence'),
                (8, 50, 'Tomorrow the captain will sail across the northern ocean'),
                (12, 10, '父亲把那封沉重的来信放在了书桌上'),
                (16, 20, '今晚的宴会将在花园里的凉亭中举行'),
                (20, 25, '楼顶上的积雪终于在今天清晨完全融化')]:
            self.assign(lines, items, item, line, words)
        for item, line, other, words in [(6, 45, 75, '我根本不知道这件事应该怎么办'),
                                         (14, 15, 80, '你为什么一直站在那个门口等着')]:
            self.assign(lines, items, item, line, words)
            lines[other]['text'] = words
        rows = self.match(lines, items)
        self.assertEqual(self.positions(rows, 6), {'l45'})
        self.assertEqual(self.positions(rows, 14), {'l15'})
        self.assertIn('l10', self.positions(rows, 12))

    def test_two_dispersed_anchors_do_not_force_a_whole_file_range(self):
        lines, items = self.fixture()
        self.assign(lines, items, 1, 10, 'The midnight train has arrived beside the harbour')
        self.assign(lines, items, 21, 30, 'Tomorrow the captain will sail across the northern ocean')
        self.assign(lines, items, 11, 20, '我根本不知道这件事应该怎么办')
        lines[80]['text'] = lines[20]['text']
        self.assertEqual(self.positions(self.match(lines, items), 11), {'l20', 'l80'})

    def test_three_portions_reject_far_edge_hits_without_crop_or_clock_changes(self):
        lines, items = self.fixture()
        self.assign(lines, items, 1, 30, 'The midnight train has arrived beside the harbour')
        self.assign(lines, items, 11, 40, 'The gardener forgot his shovel beside the wooden fence')
        self.assign(lines, items, 21, 50, 'Tomorrow the captain will sail across the northern ocean')
        lines[5]['text'] = '别这样，我亲爱的妹妹，我们永远都会在这里等着你。'
        items[0]['text'] = '别这样我亲爱的妹妹'
        lines[85]['text'] = '谢谢你，亲爱的朋友，今天我们已经走得很远了。'
        items[22]['text'] = '谢谢你亲爱的朋友'
        original = copy.deepcopy((lines, items))
        with patch.object(engine, '_sequence_disambiguate', side_effect=lambda rows, *args: rows):
            before = self.match(lines, items)
        self.assertIn('l5', self.positions(before, 0))
        self.assertIn('l85', self.positions(before, 22))
        rows = self.match(lines, items)
        self.assertFalse(self.positions(rows, 0))
        self.assertFalse(self.positions(rows, 22))
        self.assertEqual((lines, items), original)

    def test_three_anchors_confined_to_front_do_not_guess_end_of_file(self):
        lines, items = self.fixture()
        self.assign(lines, items, 0, 30, 'The midnight train has arrived beside the harbour')
        self.assign(lines, items, 2, 40, 'The gardener forgot his shovel beside the wooden fence')
        self.assign(lines, items, 4, 50, 'Tomorrow the captain will sail across the northern ocean')
        lines[85]['text'] = '谢谢你，亲爱的朋友，今天我们已经走得很远了。'
        items[22]['text'] = '谢谢你亲爱的朋友'
        self.assertIn('l85', self.positions(self.match(lines, items), 22))

    def test_no_context_is_borrowed_from_other_file_or_recording(self):
        for key in ('media_path', 'session_id'):
            lines, items = self.fixture()
            self.assign(lines, items, 1, 10, 'The midnight train has arrived beside the harbour')
            self.assign(lines, items, 11, 20, 'The gardener forgot his shovel beside the wooden fence')
            self.assign(lines, items, 21, 30, 'Tomorrow the captain will sail across the northern ocean')
            self.assign(lines, items, 6, 15, '我根本不知道这件事应该怎么办')
            lines[70]['text'] = lines[15]['text']
            for index in (1, 11, 21):
                items[index][key] = 'another-' + str(index)
            with self.subTest(key=key):
                self.assertEqual(self.positions(self.match(lines, items), 6), {'l15', 'l70'})

    def test_equal_order_paths_do_not_arbitrarily_choose_an_interior_scene(self):
        lines, items = self.fixture(duration=12)
        for item, line, words in [
                (0, 10, 'The midnight train has arrived beside the harbour'),
                (2, 40, 'The gardener forgot his shovel beside the wooden fence'),
                (4, 20, 'Tomorrow the captain will sail across the northern ocean'),
                (6, 50, '父亲把那封沉重的来信放在了书桌上'),
                (8, 30, '今晚的宴会将在花园里的凉亭中举行'),
                (10, 60, '楼顶上的积雪终于在今天清晨完全融化')]:
            self.assign(lines, items, item, line, words)
        self.assign(lines, items, 5, 25, '我根本不知道这件事应该怎么办')
        lines[45]['text'] = lines[25]['text']
        self.assertEqual(self.positions(self.match(lines, items), 5), {'l25', 'l45'})

    def test_sentence_inside_another_paragraph_is_not_a_unique_anchor(self):
        lines, items = self.fixture()
        self.assign(lines, items, 0, 10, 'The midnight train has arrived beside the harbour')
        self.assign(lines, items, 11, 20, '年轻人不作死就不会死')
        lines[30]['text'] = '我不知道他的哥哥为什么这样说。年轻人不作死就不会死。我们还是回家吧。'
        self.assign(lines, items, 21, 40, 'Tomorrow the captain will sail across the northern ocean')
        self.assign(lines, items, 6, 15, '我根本不知道这件事应该怎么办')
        lines[80]['text'] = lines[15]['text']
        self.assertEqual(self.positions(self.match(lines, items), 6), {'l15', 'l80'})

    def test_long_paragraph_rescues_noisy_sentence_without_creating_script_or_media_ids(self):
        line = dict(id='long', text='年轻的时候我总觉得世界大得不可思议。石桥另一头的旅店昨晚忽然失火了。'
                    '我们得在黎明之前赶到那里去帮助那些无家可归的人。')
        item = dict(id='actual', text='石桥那头的旅店昨晚突然失火了', session_id='day02',
                    media_path='/qa/scene.mov', start_seconds=7.123, end_seconds=11.567,
                    data=dict(sound_roll='SR004', source_timecode='11:20:30:00'))
        before = copy.deepcopy((line, item))
        rows = engine.match_dialogue([line], [item])
        self.assertEqual([(row['script_line_id'], row['transcript_id']) for row in rows], [('long', 'actual')])
        self.assertGreater(rows[0]['score'], .6)
        self.assertIn('段内句子', rows[0]['reason'])
        self.assertEqual((line, item), before)

    def test_sentence_lookup_rejects_short_common_words_and_unrelated_noise(self):
        lines = [dict(id='long', text='年轻的时候我总觉得世界大得不可思议。石桥另一头的旅店昨晚忽然失火了。'
                      '我们得在黎明之前赶到那里去帮助那些无家可归的人。')]
        for text in ('是的', 'good', '摄影机准备开始倒数三二一', '雪山上的乌鸦成群飞走了'):
            with self.subTest(text=text):
                self.assertEqual(engine.match_dialogue(lines, [dict(id='noise', text=text, session_id='s')]), [])

    def test_asr_sentence_can_begin_inside_a_printed_sentence(self):
        lines = [dict(id='one-paragraph', text='年轻的时候我总觉得这个世界大得不可思议'
                      '石桥另一头的旅店昨晚忽然失火了我们得在黎明之前赶到那里去帮助那些无家可归的人')]
        item = dict(id='speech', text='石桥那头的旅店昨晚突然失火了', session_id='s',
                    media_path='/qa/one.mov', start_seconds=10.123, end_seconds=15.456)
        rows = engine.match_dialogue(lines, [item])
        self.assertEqual([(row['script_line_id'], row['transcript_id']) for row in rows], [('one-paragraph', 'speech')])
        self.assertGreater(rows[0]['score'], .6)
        self.assertIn('连续字词', rows[0]['reason'])

    def test_window_postings_preserve_every_old_admissible_window_score(self):
        rng = random.Random(101215)
        vocabulary = list('甲乙丙丁戊己庚辛壬癸') + ['go', 'stay', 'home']
        for _ in range(150):
            script = ' '.join(rng.choices(vocabulary, k=rng.randrange(3, 50)))
            windows = []
            for number in range(15):
                tokens = rng.choices(vocabulary, k=rng.randrange(2, 65))
                windows.append((' '.join(tokens), tokens, Counter(tokens), [(number, 0, len(tokens))]))
            bases = [rng.random() for _ in windows]
            expected = engine._joined_scores(script, windows, bases, lambda: False)
            actual = engine._joined_scores(script, engine._WindowIndex(windows), bases, lambda: False)
            self.assertEqual(actual, expected)


if __name__ == '__main__':
    unittest.main()
