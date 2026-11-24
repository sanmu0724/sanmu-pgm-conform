"""Configured cameras flow through dialogue relations and correspondence reports."""
import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest

from library_angles import AngleMapper
from library_correspondence import export_correspondence
from library_timeline import build_timeline_data
from test_library_angles import catalogue, session_config


class DynamicCameraRelationsTests(unittest.TestCase):
    def config(self, session='s1', cameras='ABCDEF'):
        return dict(session_config(session, tuple(cameras)), camera_ids=list(cameras))

    def test_all_six_angles_share_one_dialogue_match_and_preserve_identity(self):
        data = catalogue(self.config())
        before = copy.deepcopy(data)
        node = build_timeline_data(data)[0]
        self.assertEqual((node['count'], node['angle_count'], node['camera_count']), (1, 6, 6))
        angles = node['clips'][0]['related_angles']
        self.assertEqual([row['camera'] for row in angles], list('ABCDEF'))
        for row in angles:
            self.assertEqual((row['parent_candidate_id'], row['parent_transcript_id']), ('match1', 't1'))
            self.assertEqual((row['source_tc_in'], row['source_tc_out']), ('12:00:10:00', '12:00:15:00'))
        self.assertEqual(data, before)

    def test_registry_only_extra_angles_use_their_registered_headers(self):
        config = self.config(cameras='EF')
        config['library_media'] = [dict(row, kind='proxy', id=row['source_id'],
            probe={key: row[key] for key in ('fps', 'frames', 'start_tc')}) for row in config.pop('sources')]
        clip = build_timeline_data(catalogue(config))[0]['clips'][0]
        self.assertEqual([(row['camera'], row['source_id']) for row in clip['related_angles']], [('E', 's1E'), ('F', 's1F')])
        self.assertEqual(clip['camera_count'], 2)

    def test_four_and_six_camera_sessions_never_cross_associate(self):
        data = catalogue(self.config(cameras='ABCD'))
        data['sessions'].append(dict(id='s2', name='第二场', config=self.config('s2')))
        data['transcripts'].append(dict(data['transcripts'][0], id='t2', session_id='s2', media_path='/qa/s2/PGM.mov'))
        data['candidates'].append(dict(data['candidates'][0], id='match2', transcript_id='t2'))
        node = build_timeline_data(data)[0]
        self.assertEqual((node['count'], node['angle_count'], node['camera_count']), (2, 10, 6))
        for clip in node['clips']:
            self.assertEqual(clip['related_angle_count'], 4 if clip['session_id'] == 's1' else 6)
            for angle in clip['related_angles']:
                self.assertIn('/' + clip['session_id'] + '/', angle['media_path'])

    def test_incremental_extra_camera_material_uses_existing_transcript(self):
        config = self.config()
        extra = config['sources'][4:]
        config['sources'] = config['sources'][:4]
        data = catalogue(config)
        before = build_timeline_data(data)[0]
        config['sources'].extend(extra)
        after = build_timeline_data(data)[0]
        self.assertEqual((before['angle_count'], after['angle_count']), (4, 6))
        self.assertEqual((before['count'], after['count'], len(data['transcripts'])), (1, 1, 1))

    def test_extra_camera_offsets_and_partial_coverage_are_reported(self):
        config = self.config(cameras='EF')
        config['camera_offsets'] = {'E': 3, 'F': -2}
        config['sources'][1]['frames'] = 300
        data = catalogue(config)
        clip = AngleMapper(data['sessions']).fields(data['candidates'][0], data['transcripts'][0])
        by_camera = {row['camera']: row for row in clip['related_angles']}
        self.assertEqual((by_camera['E']['frame_in'], by_camera['E']['frame_out_exclusive']), (253, 378))
        self.assertEqual((by_camera['F']['frame_in'], by_camera['F']['frame_out_exclusive']), (248, 300))
        self.assertTrue(any(warning.startswith('F 机位代理未覆盖') for warning in clip['related_angle_warnings']))

    def test_report_union_columns_support_mixed_camera_counts(self):
        data = catalogue(self.config(cameras='AB'))
        data['sessions'].append(dict(id='s2', name='第二场', config=self.config('s2')))
        data['transcripts'].append(dict(data['transcripts'][0], id='t2', session_id='s2', media_path='/qa/s2/PGM.mov'))
        data['candidates'].append(dict(data['candidates'][0], id='match2', transcript_id='t2'))
        with tempfile.TemporaryDirectory() as folder:
            result = export_correspondence(data, Path(folder) / 'lookup.csv')
            document = json.loads(Path(result['json_path']).read_text())
            with Path(result['csv_path']).open(encoding='utf-8-sig', newline='') as stream:
                table = list(csv.DictReader(stream))
        self.assertEqual(set(document['records'][0]['proxies']), set('AB'))
        self.assertEqual(set(document['records'][1]['proxies']), set('ABCDEF'))
        self.assertEqual(table[0]['E机代理'], '')
        self.assertEqual(table[0]['F机代理'], '')
        self.assertIn('/qa/s2/E.mov', table[1]['E机代理'])
        self.assertIn('/qa/s2/F.mov', table[1]['F机代理'])


if __name__ == '__main__':
    unittest.main()
