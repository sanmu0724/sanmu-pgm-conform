"""Saved baseline partitions drive complete multicam/wave exports unchanged."""
import copy
from fractions import Fraction as F
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from baseline_script_map import build_baseline_map, resolve_line
from reference_timeline import ReferenceTimelineError, assemble_reference_xml, build_reference_plan
import test_reference_timeline as fixture
from test_reference_timeline import time


class BaselineMappedPlanTests(unittest.TestCase):
    setUp = fixture.ReferenceXMLTests.setUp
    tearDown = fixture.ReferenceXMLTests.tearDown

    def mapping(self):
        return build_baseline_map(self.data, 'base', self.cuts['base'])

    def plan(self, mapping, selected=('alt1',)):
        return build_reference_plan(self.data, 'base', selected, cuts=self.cuts,
                                    baseline_script_map=mapping)

    def test_manual_source_onset_moves_only_whole_alternative_target(self):
        mapping = self.mapping()
        resolve_line(mapping, 'line1').update(anchor_frame=50, manual=True)
        original_data = copy.deepcopy(self.data)
        plan = self.plan(mapping)
        row = plan['selections'][0]
        self.assertEqual((row['target_frame_in'], row['target_frame_out_exclusive']), (0, 250))
        self.assertEqual((row['pgm_frame_in'], row['pgm_frame_out_exclusive']), (0, 250))
        self.assertEqual((row['dialogue_pgm_frame_in'], row['baseline_dialogue_frame_in']), (50, 50))
        self.assertEqual(row['baseline_segment_id'], resolve_line(mapping, 'line1')['id'])
        self.assertIsNone(row['baseline_candidate_id'])
        self.assertEqual(self.data, original_data)

    def test_mapping_bypasses_baseline_occurrence_picker_even_with_many_asr_chunks(self):
        first = self.data['transcripts'][0]
        candidate = self.data['candidates'][0]
        first['end_seconds'] = 2
        for number in range(2, 7):
            source = dict(first, id='chunk'+str(number), start_seconds=number, end_seconds=number+1)
            self.data['transcripts'].append(source)
            self.data['candidates'].append(dict(candidate, id='candidate'+str(number), transcript_id=source['id']))
        mapping = self.mapping()
        with patch('pgm_takes.PgmTakeIndex.association_ready', side_effect=AssertionError('old occurrence picker used')):
            plan = self.plan(mapping)
        self.assertEqual(plan['selections'][0]['baseline_dialogue_frame_in'], 25)
        self.assertEqual(len(resolve_line(mapping, 'line1')['evidence_ids']), 6)

    def test_inner_line_of_unsplit_script_unit_is_not_given_first_line_timestamp(self):
        utterance = dict(id='inner', session_id='second', kind='pgm', media_path='/second.mov',
                         start_seconds=2, end_seconds=4, text='The end.')
        self.data['transcripts'] = [row for row in self.data['transcripts'] if row['session_id'] != 'second']+[utterance]
        self.data['candidates'] = [row for row in self.data['candidates'] if row['id'] != 'second1']+[
            dict(id='inner-choice', transcript_id='inner', script_line_id='line2', status='confirmed', score=.99)]
        units = [dict(id='whole', line_ids=['line1','line2'])]
        with patch('baseline_script_map.script_units', return_value=units):
            mapping = self.mapping()
        self.assertEqual(resolve_line(mapping, 'line1')['line_ids'], ['line1','line2'])
        with self.assertRaisesRegex(ReferenceTimelineError, '基准起点'):
            self.plan(mapping, ('inner-choice',))

    def test_uncertain_segment_and_missing_onset_cannot_export_by_source_clock(self):
        for change in ('review', 'missing'):
            mapping = self.mapping()
            row = resolve_line(mapping, 'line1')
            if change == 'review':row['needs_review'] = True
            else:row['anchor_frame'] = None
            with self.subTest(change=change), self.assertRaisesRegex(ReferenceTimelineError, '基准起点'):
                self.plan(mapping)

    def test_mapped_export_preserves_proxy_multicam_angles_and_original_wav_samples(self):
        mapping = self.mapping()
        resolve_line(mapping, 'line1').update(anchor_frame=50, manual=True)
        plan = self.plan(mapping)
        report = assemble_reference_xml(self.base, {'alt': self.alt}, plan, self.output)
        root = ET.parse(self.output).getroot()
        pieces = root.findall('./library/event/project/sequence/spine/*/clip[@lane="1"]')
        self.assertEqual([row.find('mc-clip/mc-source').get('angleID') for row in pieces],
                         ['alt1-angle-A', 'alt1-angle-B', 'alt1-angle-C'])
        self.assertEqual([time(row,'duration') for row in pieces], [F(3),F(3),F(4)])
        audio = [row for piece in pieces for row in piece.findall('audio')]
        self.assertEqual([time(row,'start') for row in audio],
                         [F(100)+F(3,48000),F(103)+F(3,48000),F(200)+F(7,48000),F(202)+F(7,48000)])
        self.assertEqual(sum(time(row,'duration') for row in audio), F(10))
        self.assertTrue(all(row.get('srcCh') == '1, 2' and row.get('outCh') == 'L,R' for row in audio))
        self.assertEqual(report['selected_count'], 1)
        resources = root.findall('./resources/asset')
        sound = [row for row in resources if row.get('hasAudio') == '1']
        for row in sound:
            metadata = {item.get('key'):item.get('value') for item in row.findall('./metadata/md')}
            self.assertIn('SOUND_',metadata['com.apple.proapps.studio.reel'])
            self.assertTrue(metadata['com.pgm.originalAudioFilename'].endswith('.wav'))
        self.assertFalse(any('pgm' in row.get('src','').lower() for row in resources))

    def test_overlapping_complete_takes_get_distinct_lanes_and_keep_baseline_v1(self):
        mapping = self.mapping()
        plan = self.plan(mapping, ('alt1','second1'))
        self.assertEqual(len(plan['selections']), 2)
        self.assertEqual({row['lane'] for row in plan['selections']}, {1,2})
        report = assemble_reference_xml(self.base, {'alt':self.alt,'second':self.second},plan,self.output)
        root=ET.parse(self.output).getroot()
        original=ET.parse(self.base).find('./library/event/project/sequence/spine')
        spine=root.find('./library/event/project/sequence/spine')
        self.assertEqual(sum(time(row,'duration') for row in spine if row.tag=='clip'),
                         sum(time(row,'duration') for row in original))
        self.assertTrue(report['manifest']['include_baseline'])

    def test_baseline_only_plan_does_not_need_a_script_mapping(self):
        data=copy.deepcopy(self.data)
        data['transcripts']=[]
        data['candidates']=[]
        plan=build_reference_plan(data,'base',(),cuts=self.cuts)
        self.assertEqual(plan['selections'],[])
        self.assertEqual(plan['baseline_duration_frames'],250)


if __name__ == '__main__':
    unittest.main()
