"""Dialogue decisions and same-recording angle lookup share one source anchor."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from dialogue_engine import match_dialogue
from library_correspondence import export_correspondence
from library_store import Store
from library_timeline import build_timeline_data
from library_workflow import enrich_data


class AngleWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'qa.sqlite3')
        self.addCleanup(self.store.close)
        self.production = self.store.create_production('同步角度测试')['id']
        self.pgm = '/nas/day3/PGM.mov'
        self.config = dict(fps=25, calibration_confirmed=True,
            library_media=[dict(kind='pgm', path=self.pgm,
                probe=dict(fps='25',frames=250,start_tc='19:00:00:00'))],
            sources=[
                dict(camera='A',path='/nas/day3/A.mov',source_id='srcA',
                     fps='25',frames=250,start_tc='19:00:00:00'),
                dict(camera='B',path='/nas/day3/B.mov',source_id='srcB',
                     fps='25',frames=375,start_tc='18:59:55:00')])
        self.session = self.store.save_session(self.production, 'Day3', self.config)
        self.store.put_script(self.production, '台本', [
            dict(order=0,speaker='甲',text='请你明天带着雨伞到这里等我。')])
        self.store.add_transcript(self.session['id'], self.pgm, [
            dict(start_seconds=1,end_seconds=3,text='请你明天带着雨伞到这里等我。')])
        rows = self.read()
        matches = match_dialogue(rows['scripts'], rows['transcripts'])
        self.assertEqual(len(matches), 1)
        self.store.replace_candidates(self.production, matches)

    def read(self):
        return dict(sessions=self.store.list_sessions(self.production),
                    scripts=self.store.list_script(self.production),
                    transcripts=self.store.list_transcripts(self.production),
                    candidates=self.store.list_candidates(self.production))

    def test_semantic_match_enriches_all_actual_angles_without_extra_decisions(self):
        before = self.read()
        data = enrich_data(before)
        self.assertEqual(before, self.read())
        candidate = data['candidates'][0]
        angles = candidate['related_angles']
        self.assertEqual([(a['camera'],a['start_seconds'],a['end_seconds']) for a in angles],
                         [('A',1.,3.),('B',6.,8.)])
        self.assertEqual({a['parent_candidate_id'] for a in angles}, {candidate['id']})
        self.assertEqual(len(data['transcripts']), 1)
        self.assertEqual(len(data['candidates']), 1)
        node = build_timeline_data(data)[0]
        self.assertEqual((node['count'],node['angle_count'],node['camera_count']), (1,2,2))

    def test_new_camera_appends_to_confirmed_anchor_without_new_asr(self):
        row = self.read()['candidates'][0]
        self.store.update_candidate(row['id'],'confirmed',note='导演已核对',
                                    expected_revision=row['revision'])
        config = copy.deepcopy(self.config)
        config['sources'].append(dict(camera='C',path='/nas/day3/C.mov',source_id='srcC',
            fps='25',frames=250,start_tc='19:00:02:00'))
        self.store.save_session(self.production,'Day3',config,session_id=self.session['id'],
                                expected_revision=self.session['revision'])
        data = enrich_data(self.read())
        row = data['candidates'][0]
        self.assertEqual(row['status'], 'confirmed')
        self.assertEqual(row['note'], '导演已核对')
        self.assertEqual(len(data['transcripts']), 1)
        self.assertEqual([(a['camera'],a['start_seconds'],a['end_seconds']) for a in row['related_angles']],
                         [('A',1.,3.),('B',6.,8.),('C',0.,1.)])
        self.assertEqual(build_timeline_data(data)[0]['count'], 1)
        result = export_correspondence(data,self.root/'angles.csv')
        records = json.loads(Path(result['json_path']).read_text())['records']
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['proxies']['C']['segments'][0]['start_seconds'], 0.)
        self.assertEqual(records[0]['proxies']['C']['segments'][0]['end_seconds'], 1.)

    def test_other_performance_same_clock_is_not_added(self):
        config = copy.deepcopy(self.config)
        config['sources'] = [dict(camera='D',path='/nas/day1/D.mov',
            fps='25',frames=250,start_tc='19:00:00:00')]
        self.store.save_session(self.production,'Day1',config)
        data = enrich_data(self.read())
        angles = data['candidates'][0]['related_angles']
        self.assertEqual([a['camera'] for a in angles], ['A','B'])
        self.assertNotIn('/nas/day1/D.mov', [a['media_path'] for a in angles])


if __name__ == '__main__':
    unittest.main()
