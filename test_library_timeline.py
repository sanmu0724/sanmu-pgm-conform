import copy
import unittest

from library_timeline import build_timeline_data


def sample():
    return dict(
        sessions=[dict(id='s1', name='第一场', config={}), dict(id='s3', name='第三场', config={})],
        scripts=[dict(id='l3', script_id='script', order=3, scene='第一幕', speaker='Alice', text='再见。Bye.', active=1),
                 dict(id='l1', script_id='script', order=1, scene='第二幕', speaker='Bob', text='你为什么来？', active=1),
                 dict(id='l2', script_id='script', order=2, scene='第一幕', speaker='Alice', text='因为你在。', active=1),
                 dict(id='old_line', script_id='oldscript', order=0, text='过期台本', active=0)],
        transcripts=[dict(id='t1', session_id='s1', media_path='/nas/day1/PGM.mov', start_seconds=12.125,
                          end_seconds=14.75, text='你为什么到这里来？', speaker='Bob', metadata={'source_tc': '19:00:00:00'}),
                     dict(id='t3', session_id='s3', media_path='/nas/day3/PGM.mov', start_seconds=1200.1,
                          end_seconds=1202.25, text='你怎么来了？', speaker='Bob'),
                     dict(id='bye', session_id='s3', media_path='/nas/day3/PGM.mov', start_seconds=1234,
                          end_seconds=1235.25, text='Bye.')],
        candidates=[dict(id='m1', script_line_id='l1', transcript_id='t1', status='candidate', score=.8,
                         reason='台词相近', proxy_paths=['/nas/day1/A.mov']),
                    dict(id='m3', script_line_id='l1', transcript_id='t3', status='confirmed', score=.7, note='导演已核实'),
                    dict(id='bye_match', script_line_id='l3', transcript_id='bye', status='candidate', score=.95)])


class TimelineDataTests(unittest.TestCase):
    def test_script_order_repeated_scene_and_empty_lines_are_preserved(self):
        nodes = build_timeline_data(sample())
        self.assertEqual([row['id'] for row in nodes], ['l1', 'l2', 'l3'])
        self.assertEqual([row['scene'] for row in nodes], ['第二幕', '第一幕', '第一幕'])
        self.assertEqual([row['count'] for row in nodes], [2, 0, 1])
        self.assertEqual(nodes[1]['clips'], [])
        self.assertEqual((nodes[1]['confirmed_count'], nodes[1]['candidate_count'], nodes[1]['session_count']), (0, 0, 0))
        self.assertEqual(nodes[0]['line_id'], nodes[0]['script_line_id'])

    def test_counts_sessions_and_real_clip_fields_are_not_retimed(self):
        node = build_timeline_data(sample())[0]
        self.assertEqual((node['count'], node['confirmed_count'], node['candidate_count'], node['session_count']), (2, 1, 1, 2))
        self.assertEqual(node['session_names'], ['第一场', '第三场'])
        first, third = node['clips']
        self.assertEqual((first['start_seconds'], first['end_seconds']), (12.125, 14.75))
        self.assertEqual((third['start_seconds'], third['end_seconds']), (1200.1, 1202.25))
        self.assertEqual(first['media_path'], '/nas/day1/PGM.mov')
        self.assertEqual(first['proxy_paths'], ['/nas/day1/A.mov'])
        self.assertEqual(first['candidate_id'], 'm1')
        self.assertEqual(first['transcript_text'], '你为什么到这里来？')
        self.assertEqual(first['metadata'], {'source_tc': '19:00:00:00'})
        self.assertNotIn('source_tc_in', first)

    def test_duplicate_transcript_counts_once_and_confirmed_wins_regardless_of_order(self):
        data = sample()
        duplicate = dict(data['candidates'][0], id='manual', status='confirmed', note='人工通过')
        data['candidates'].extend([duplicate, dict(duplicate, id='second_manual'), dict(data['candidates'][0], id='later_candidate')])
        node = build_timeline_data(data)[0]
        self.assertEqual((node['count'], node['confirmed_count'], node['candidate_count']), (2, 2, 0))
        self.assertEqual(node['clips'][0]['candidate_id'], 'manual')
        self.assertEqual(node['clips'][0]['note'], '人工通过')

    def test_rejected_inactive_and_orphan_associations_do_not_count(self):
        data = sample()
        original = data['candidates'][0]
        data['candidates'].extend([dict(original, id='old', script_line_id='old_line'),
                                   dict(original, id='rejected', transcript_id='bye', status='rejected'),
                                   dict(original, id='inactive', transcript_id='bye', active=0),
                                   dict(original, id='orphan', transcript_id='does_not_exist')])
        self.assertEqual([row['count'] for row in build_timeline_data(data)], [2, 0, 1])

    def test_current_asr_run_filters_old_confirmed_and_same_filename_other_session_is_safe(self):
        data = sample()
        data['sessions'][0]['config'] = {'library_asr': {'/nas/day1/PGM.mov': dict(
            active_transcript_ids=['new'], sync_signature='current')},
            'library_sync': {'/nas/day1/PGM.mov': {'signature': 'current'}}}
        data['transcripts'].append(dict(data['transcripts'][0], id='new', start_seconds=20, end_seconds=21))
        data['candidates'][0]['status'] = 'confirmed'
        data['candidates'].append(dict(data['candidates'][0], id='new_match', transcript_id='new', status='candidate'))
        node = build_timeline_data(data)[0]
        self.assertEqual([row['transcript_id'] for row in node['clips']], ['new', 't3'])
        self.assertEqual(node['confirmed_count'], 1)
        data['sessions'][0]['config']['library_sync']['/nas/day1/PGM.mov']['signature'] = 'changed'
        self.assertEqual(build_timeline_data(data)[0]['count'], 1)

    def test_multiple_scripts_do_not_interleave_order_and_identical_text_keeps_line_ids(self):
        data = sample()
        data['scripts'].extend([dict(id='new2', script_id='other_script', order=2, scene='ACT I', speaker='Bob', text='你为什么来？'),
                                dict(id='new1', script_id='other_script', order=1, scene='SCENE II', speaker='Alice', text='Hello.')])
        nodes = build_timeline_data(data)
        self.assertEqual([row['id'] for row in nodes], ['l1', 'l2', 'l3', 'new1', 'new2'])
        self.assertEqual(nodes[-1]['count'], 0)

    def test_incremental_append_keeps_existing_nodes_ids_and_clips(self):
        data = sample()
        before = build_timeline_data(data)
        data['sessions'].append(dict(id='s2', name='第二场', config={}))
        data['transcripts'].append(dict(data['transcripts'][0], id='t2', session_id='s2', media_path='/nas/day2/PGM.mov'))
        data['candidates'].append(dict(data['candidates'][0], id='m2', transcript_id='t2'))
        after = build_timeline_data(data)
        self.assertEqual([row['id'] for row in after], [row['id'] for row in before])
        self.assertEqual(after[0]['count'], 3)
        self.assertEqual(after[0]['clips'][:2], before[0]['clips'])
        self.assertEqual(after[1:], before[1:])

    def test_joined_candidate_snapshot_preserves_true_local_positions(self):
        data = sample()
        data['transcripts'] = []
        data['candidates'] = [dict(data['candidates'][0], session_id='s1', media_path='/nas/day1/PGM.mov',
                                   start_seconds=12.125, end_seconds=14.75, transcript_text='现场原话')]
        node = build_timeline_data(data)[0]
        self.assertEqual(node['count'], 1)
        self.assertEqual(node['clips'][0]['transcript_text'], '现场原话')

    def test_transcript_source_wins_over_stale_joined_fields_and_input_is_not_mutated(self):
        data = sample()
        data['candidates'][0].update(media_path='/wrong.mov', start_seconds=0, end_seconds=1, session_id='wrong')
        before = copy.deepcopy(data)
        node = build_timeline_data(data)[0]
        self.assertEqual(node['clips'][0]['media_path'], '/nas/day1/PGM.mov')
        self.assertEqual(node['clips'][0]['session_id'], 's1')
        self.assertEqual(node['clips'][0]['start_seconds'], 12.125)
        self.assertEqual(data, before)
        node['clips'][0]['proxy_paths'].append('mutated output')
        self.assertEqual(data, before)

    def test_invalid_ranges_are_not_fabricated_and_no_script_is_empty(self):
        data = sample()
        data['transcripts'][0]['end_seconds'] = float('nan')
        self.assertEqual(build_timeline_data(data)[0]['count'], 1)
        self.assertEqual(build_timeline_data(dict(data, scripts=[])), [])


if __name__ == '__main__':
    unittest.main()
