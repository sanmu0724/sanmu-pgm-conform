import copy
from fractions import Fraction as F
import unittest
from unittest import mock

from library_angles import AngleMapper, enrich_related_angles
from library_timeline import build_timeline_data


def session_config(session='s1', cameras=('A', 'B', 'C', 'D')):
    pgm = f'/qa/{session}/PGM.mov'
    sources = [dict(camera=camera, source_id=session + camera, path=f'/qa/{session}/{camera}.mov',
                    fps='25', frames=1000, start_tc='12:00:00:00') for camera in cameras]
    return dict(fps=25, calibration_confirmed=True, pgm=pgm,
                pgm_sources=[dict(path=pgm, fps='25', frames=1000, start_tc='12:00:00:00')], sources=sources)


def catalogue(config=None, begin=10, end=15):
    config = config if config is not None else session_config()
    return dict(sessions=[dict(id='s1', name='第一场', config=config)],
                scripts=[dict(id='line', script_id='script', text='同一句台词', order=0)],
                transcripts=[dict(id='t1', session_id='s1', media_path='/qa/s1/PGM.mov', kind='pgm',
                                  start_seconds=begin, end_seconds=end, text='同一句台词')],
                candidates=[dict(id='match1', script_line_id='line', transcript_id='t1', status='confirmed')])


class CameraAngleRelationsTests(unittest.TestCase):
    def test_one_pgm_match_relates_all_four_cameras_without_more_performances(self):
        data = catalogue()
        node = build_timeline_data(data)[0]
        self.assertEqual((node['count'], node['session_count'], node['angle_count'], node['camera_count']), (1, 1, 4, 4))
        clip = node['clips'][0]
        self.assertEqual(clip['related_angle_count'], 4)
        self.assertEqual([row['camera'] for row in clip['related_angles']], list('ABCD'))
        for row in clip['related_angles']:
            self.assertEqual((row['start_seconds'], row['end_seconds']), (10, 15))
            self.assertEqual((row['pgm_start_seconds'], row['pgm_end_seconds']), (10, 15))
            self.assertEqual(row['relation'], 'same_session_timecode')
            self.assertEqual((row['session_id'], row['parent_candidate_id'], row['parent_transcript_id']), ('s1', 'match1', 't1'))
            self.assertEqual(row['source_tc_in'], '12:00:10:00')
            self.assertEqual(row['source_tc_out'], '12:00:15:00')
        self.assertEqual(clip['related_angle_warnings'], [])

    def test_same_timecode_different_sessions_never_cross_associate(self):
        data = catalogue()
        data['sessions'].append(dict(id='s2', name='第二场', config=session_config('s2')))
        data['transcripts'].append(dict(data['transcripts'][0], id='t2', session_id='s2', media_path='/qa/s2/PGM.mov'))
        data['candidates'].append(dict(id='match2', script_line_id='line', transcript_id='t2', status='candidate'))
        node = build_timeline_data(data)[0]
        self.assertEqual((node['count'], node['session_count'], node['angle_count'], node['camera_count']), (2, 2, 8, 4))
        for clip in node['clips']:
            for angle in clip['related_angles']:
                self.assertEqual(angle['session_id'], clip['session_id'])
                self.assertIn('/' + clip['session_id'] + '/', angle['media_path'])

    def test_cut_across_same_camera_files_splits_at_actual_boundary(self):
        config = session_config(cameras=('A',))
        config['sources'][0]['frames'] = 300
        config['sources'].append(dict(camera='A', source_id='A2', path='/qa/s1/A2.mov',
                                      fps='25', frames=700, start_tc='12:00:12:00'))
        angles = build_timeline_data(catalogue(config))[0]['clips'][0]['related_angles']
        self.assertEqual(len(angles), 2)
        self.assertEqual([(row['start_seconds'], row['end_seconds']) for row in angles], [(10, 12), (0, 3)])
        self.assertEqual([(row['pgm_start_seconds'], row['pgm_end_seconds']) for row in angles], [(10, 12), (12, 15)])
        self.assertTrue(all(not row['ambiguous'] for row in angles))

    def test_proxy_gap_is_preserved_not_filled(self):
        config = session_config(cameras=('A',))
        config['sources'][0]['frames'] = 300
        config['sources'].append(dict(camera='A', path='/qa/s1/A2.mov', fps='25', frames=700, start_tc='12:00:13:00'))
        clip = build_timeline_data(catalogue(config))[0]['clips'][0]
        self.assertEqual([(row['pgm_start_seconds'], row['pgm_end_seconds']) for row in clip['related_angles']], [(10, 12), (13, 15)])
        self.assertTrue(any('[12.000,13.000)' in warning for warning in clip['related_angle_warnings']))

    def test_overlapping_proxy_files_remain_visible_and_ambiguous(self):
        config = session_config(cameras=('A',))
        config['sources'].append(dict(camera='A', path='/qa/s1/overlap.mov', fps='25', frames=1000, start_tc='12:00:00:00'))
        clip = build_timeline_data(catalogue(config))[0]['clips'][0]
        self.assertEqual(clip['related_angle_count'], 2)
        self.assertTrue(all(row['ambiguous'] for row in clip['related_angles']))
        self.assertTrue(any('重叠' in warning for warning in clip['related_angle_warnings']))

    def test_integer_camera_compensation_and_unverified_status_retained(self):
        config = session_config(cameras=('B',))
        config.update(camera_offsets={'B': 3}, calibration_confirmed=False)
        clip = build_timeline_data(catalogue(config))[0]['clips'][0]
        angle = clip['related_angles'][0]
        self.assertEqual((angle['frame_in'], angle['frame_out_exclusive']), (253, 378))
        self.assertEqual((angle['pgm_start_seconds'], angle['pgm_end_seconds']), (10, 15))
        self.assertEqual(angle['source_tc_in'], '12:00:10:03')
        self.assertFalse(angle['calibration_confirmed'])
        self.assertTrue(any('尚未核对' in warning for warning in clip['related_angle_warnings']))

    def test_registry_only_proxy_with_real_header_and_explicit_camera(self):
        config = session_config(cameras=())
        config['pgm_sources'] = []
        config['library_media'] = [dict(kind='pgm', path='/qa/s1/PGM.mov',
                                       probe=dict(fps='25', frames=1000, start_tc='12:00:00:00')),
                                   dict(kind='proxy', camera='C', id='registeredC', path='/qa/s1/C.mov',
                                        probe=dict(fps='25', frames=1000, start_tc='12:00:00:00'))]
        angle = build_timeline_data(catalogue(config))[0]['clips'][0]['related_angles'][0]
        self.assertEqual((angle['camera'], angle['source_id'], angle['start_seconds']), ('C', 'registeredC', 10))

    def test_registry_duplicate_of_source_does_not_create_second_angle(self):
        config = session_config(cameras=('A',))
        config['library_media'] = [dict(kind='proxy', camera='A', id='registeredA', path='/qa/s1/A.mov',
                                       probe=dict(fps='25', frames=1000, start_tc='12:00:00:00'))]
        node = build_timeline_data(catalogue(config))[0]
        self.assertEqual(node['angle_count'], 1)

    def test_missing_pgm_clock_does_not_guess_from_local_seconds(self):
        config = session_config()
        config['pgm_sources'][0]['start_tc'] = ''
        clip = build_timeline_data(catalogue(config))[0]['clips'][0]
        self.assertEqual(clip['related_angles'], [])
        self.assertTrue(any('时码' in warning for warning in clip['related_angle_warnings']))

    def test_missing_proxy_clock_frames_or_different_fps_not_guessed(self):
        config = session_config()
        config['sources'][0]['start_tc'] = ''
        config['sources'][1]['frames'] = None
        config['sources'][2]['fps'] = '24'
        config['sources'][3]['start_tc'] = '13:00:00:00'
        clip = build_timeline_data(catalogue(config))[0]['clips'][0]
        self.assertEqual(clip['related_angles'], [])
        self.assertTrue(clip['related_angle_warnings'])

    def test_unknown_camera_and_other_media_kind_are_not_related(self):
        config = session_config(cameras=('X',))
        self.assertEqual(build_timeline_data(catalogue(config))[0]['angle_count'], 0)
        data = catalogue()
        data['transcripts'][0]['kind'] = 'proxy'
        self.assertEqual(build_timeline_data(data)[0]['angle_count'], 0)

    def test_pgm_range_clipped_to_actual_file_and_outside_rejected(self):
        config = session_config(cameras=('A',))
        config['pgm_sources'][0]['frames'] = 300
        clip = build_timeline_data(catalogue(config))[0]['clips'][0]
        self.assertEqual(clip['related_angles'][0]['pgm_end_seconds'], 12)
        self.assertEqual(clip['related_angles'][0]['end_seconds'], 12)
        self.assertEqual(build_timeline_data(catalogue(config, 15, 16))[0]['angle_count'], 0)

    def test_frame_coverage_uses_existing_floor_in_ceil_out_rule(self):
        angle = build_timeline_data(catalogue(begin=F(1001, 100), end=F(1202, 100)))[0]['clips'][0]['related_angles'][0]
        self.assertEqual((angle['frame_in'], angle['frame_out_exclusive']), (250, 301))

    def test_empty_script_nodes_keep_zero_counts(self):
        data = catalogue()
        data['candidates'] = []
        node = build_timeline_data(data)[0]
        self.assertEqual((node['count'], node['angle_count'], node['camera_count']), (0, 0, 0))

    def test_enrichment_keeps_parent_records_and_does_not_modify_inputs(self):
        data = catalogue()
        before = copy.deepcopy(data)
        enriched = enrich_related_angles(data)
        self.assertEqual(data, before)
        self.assertIs(enriched['transcripts'], data['transcripts'])
        self.assertEqual(len(enriched['candidates']), 1)
        self.assertEqual(enriched['candidates'][0]['id'], 'match1')
        self.assertEqual(enriched['candidates'][0]['transcript_id'], 't1')
        self.assertEqual(enriched['candidates'][0]['related_angle_count'], 4)

    def test_header_mapping_never_opens_media_or_repeats_range_mapping(self):
        data = catalogue()
        mapper = AngleMapper(data['sessions'])
        with mock.patch('core.probe', side_effect=AssertionError('No media reads')), \
                mock.patch('core.read_frame', side_effect=AssertionError('No media reads')), \
                mock.patch('library_angles._proxies', wraps=__import__('library_angles')._proxies) as proxies:
            first = mapper.fields(data['candidates'][0], data['transcripts'][0])
            second = mapper.fields(dict(data['candidates'][0], id='another'), data['transcripts'][0])
        self.assertEqual(proxies.call_count, 1)
        self.assertEqual(first['related_angle_count'], 4)
        self.assertEqual(second['related_angles'][0]['parent_candidate_id'], 'another')

    def test_incremental_new_angle_appends_without_requiring_new_transcript(self):
        data = catalogue(session_config(cameras=('A',)))
        first = build_timeline_data(data)[0]
        data['sessions'][0]['config']['sources'].append(session_config(cameras=('B',))['sources'][0])
        second = build_timeline_data(data)[0]
        self.assertEqual((first['count'], second['count']), (1, 1))
        self.assertEqual((first['angle_count'], second['angle_count']), (1, 2))
        self.assertEqual(len(data['transcripts']), 1)


if __name__ == '__main__':
    unittest.main()
