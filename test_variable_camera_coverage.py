"""Authorized fallback preserves real clocks and exposes available angles."""
import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import core


BASE=90000


def source(camera,first=0,last=100,name='',offset=0):
    return dict(camera=camera,path='/'+(name or camera)+'.mov',start=BASE+first+offset,
        end=BASE+last+offset,frames=last-first,reel=camera,width=1920,height=1080)


def cut(frames=100):
    return dict(fps=25,duration=frames,pgm_sources=[dict(path='/pgm.mov',start=0,end=frames,
        frames=frames,start_tc='01:00:00:00')])


class VariableCameraCoverageTests(unittest.TestCase):
    def setUp(self):
        self.config=dict(fps=25,camera_ids=list('ABCD'),missing_camera_policy='available',
            missing_camera_preference='A',calibration_confirmed=True,multicam=True)
        self.cut=cut()
        self.sources=[source(camera) for camera in 'ACD']+[
            source('B',0,30,'B1'),source('B',60,100,'B2')]

    def conform(self,segments=None,sources=None,config=None,geometry=None):
        with patch('cut_xml.timeline_info',return_value=geometry or self.cut):
            return core.conform_events(config or self.config,segments or [core.Segment(0,100,'B','label_read')],
                self.sources if sources is None else sources)

    def test_four_three_four_masks_fallback_and_source_positions(self):
        events=self.conform()
        self.assertEqual([(row['start'],row['end']) for row in events],[(0,30),(30,60),(60,100)])
        self.assertEqual([row['available_cameras'] for row in events],[list('ABCD'),list('ACD'),list('ABCD')])
        self.assertEqual([row['camera'] for row in events],['B','A','B'])
        middle=events[1]
        self.assertEqual((middle['requested_camera'],middle['fallback_reason'],middle['reason']),
            ('B','source_gap','camera_unavailable_fallback'))
        self.assertEqual((middle['path'],middle['source_in'],middle['source_out']),('/A.mov',BASE+30,BASE+60))
        self.assertNotIn('requested_camera',events[0]);self.assertNotIn('fallback_reason',events[2])

    def test_even_available_selected_camera_splits_when_other_angles_change(self):
        events=self.conform([core.Segment(0,100,'C','label_read')])
        self.assertEqual([row['camera'] for row in events],['C']*3)
        self.assertEqual([len(row['available_cameras']) for row in events],[4,3,4])
        self.assertTrue(all('fallback_reason' not in row for row in events))

    def test_strict_mode_keeps_original_missing_proxy_behavior_and_event_shape(self):
        for policy in (None,'strict','unrecognized'):
            config=dict(self.config)
            if policy is None:config.pop('missing_camera_policy')
            else:config['missing_camera_policy']=policy
            events=self.conform(config=config)
            self.assertEqual([row['camera'] for row in events],['B']*3)
            self.assertEqual(events[1]['reel'],'BL');self.assertEqual(events[1]['reason'],'source_gap')
            self.assertTrue(all('available_cameras' not in row and 'empty_camera_gap' not in row for row in events))

    def test_missing_preferred_camera_uses_scene_order_not_alphabetical_order(self):
        config=dict(self.config,camera_ids=['D','C','A','B'])
        events=self.conform(config=config,sources=[source('C'),source('D')])
        self.assertEqual(events[0]['camera'],'D');self.assertEqual(events[0]['available_cameras'],['D','C'])
        self.assertEqual(events[0]['requested_camera'],'B')

    def test_custom_preference_is_used_only_when_uniquely_available(self):
        events=self.conform(config=dict(self.config,missing_camera_preference='D'))
        self.assertEqual(events[1]['camera'],'D')
        sources=[source('A'),source('A',0,100,'Aduplicate'),source('C')]
        events=self.conform(sources=sources)
        self.assertEqual(events[0]['available_cameras'],['C']);self.assertEqual(events[0]['camera'],'C')

    def test_camera_offsets_apply_to_coverage_and_actual_fallback_source_clock(self):
        sources=[source('A',offset=3),source('C'),source('D'),
                 source('B',0,30,'B1',offset=-2),source('B',60,100,'B2',offset=-2)]
        events=self.conform(config=dict(self.config,camera_offsets={'A':3,'B':-2}),sources=sources)
        self.assertEqual([(row['start'],row['end']) for row in events],[(0,30),(30,60),(60,100)])
        self.assertEqual([row['source_in'] for row in events],[BASE-2,BASE+33,BASE+58])
        self.assertEqual([row['record_in'] for row in events],[BASE,BASE+30,BASE+60])
        self.assertEqual([row['pgm_in'] for row in events],[0,30,60])

    def test_selected_overlap_never_falls_back_or_becomes_intentional_gap(self):
        sources=[source('A'),source('B',0,100,'B1'),source('B',0,100,'B2')]
        config=dict(self.config,empty_camera_policy='gap')
        events=self.conform(config=config,sources=sources)
        self.assertEqual(events[0]['available_cameras'],['A'])
        self.assertEqual(events[0]['reason'],'source_overlap')
        self.assertEqual(events[0]['camera'],'B');self.assertEqual(events[0]['reel'],'BL')
        self.assertNotIn('fallback_reason',events[0]);self.assertNotIn('empty_camera_gap',events[0])

    def test_unknown_and_unconfirmed_camera_never_fall_back(self):
        for camera,reason in (('?','sample_conflict'),('B','identity_unconfirmed')):
            with self.subTest(camera=camera),patch('cut_xml.timeline_info',return_value=self.cut):
                events=core.conform_events(dict(self.config,empty_camera_policy='gap'),
                    [core.Segment(0,100,camera,reason)],self.sources)
            self.assertTrue(all(row['reel']=='BL' and not row['path'] for row in events))
            self.assertTrue(all(row['camera']==camera and row['reason']==reason for row in events))
            self.assertTrue(all('fallback_reason' not in row and 'empty_camera_gap' not in row for row in events))

    def test_no_camera_coverage_can_be_explicitly_blocked(self):
        events=self.conform(sources=[],config=dict(self.config,empty_camera_policy='block'))
        self.assertEqual(len(events),1)
        self.assertEqual((events[0]['start'],events[0]['end']),(0,100))
        self.assertEqual(events[0]['available_cameras'],[])
        self.assertEqual(events[0]['reel'],'BL');self.assertEqual(events[0]['reason'],'source_gap')
        self.assertNotIn('empty_camera_gap',events[0])

    def test_explicit_zero_camera_policy_marks_only_known_gap_preserving_duration(self):
        config=dict(self.config,empty_camera_policy='gap')
        events=self.conform(config=config,sources=[])
        row=events[0]
        self.assertTrue(row['empty_camera_gap']);self.assertEqual(row['available_cameras'],[])
        self.assertEqual((row['start'],row['end'],row['pgm_in']),(0,100,0))
        self.assertEqual((row['camera'],row['path'],row['reel'],row['reason']),('B','','BL','source_gap'))
        config['missing_camera_policy']='strict'
        self.assertNotIn('empty_camera_gap',self.conform(config=config,sources=[])[0])

    def test_adjacent_files_use_half_open_edges_without_false_gap_or_overlap(self):
        sources=[source('B',0,50,'B1'),source('B',50,100,'B2')]
        events=self.conform(sources=sources)
        self.assertEqual([(row['start'],row['end']) for row in events],[(0,50),(50,100)])
        self.assertEqual([row['source_in'] for row in events],[BASE,BASE+50])
        self.assertEqual([row['path'] for row in events],['/B1.mov','/B2.mov'])
        self.assertTrue(all(row['available_cameras']==['B'] for row in events))

    def test_multiple_pgm_files_keep_local_clocks_and_contiguous_record_positions(self):
        geometry=dict(fps=25,duration=100,pgm_sources=[
            dict(path='/part1.mov',start=0,end=50,frames=50,start_tc='01:00:00:00'),
            dict(path='/part2.mov',start=50,end=100,frames=50,start_tc='02:00:00:00')])
        sources=[source('A',0,50,'A1'),source('A',BASE,BASE+50,'A2')]
        events=self.conform(sources=sources,geometry=geometry)
        self.assertEqual([row['pgm_in'] for row in events],[0,0])
        self.assertEqual([row['source_in'] for row in events],[BASE,2*BASE])
        self.assertEqual([row['record_in'] for row in events],[BASE,BASE+50])
        self.assertEqual([row['available_cameras'] for row in events],[['A'],['A']])
        self.assertEqual([row['requested_camera'] for row in events],['B','B'])

    def test_input_objects_and_original_segments_are_not_rewritten(self):
        segments=[core.Segment(0,100,'B','label_read')]
        before=copy.deepcopy((self.config,segments,self.sources,self.cut))
        self.conform(segments)
        self.assertEqual((self.config,segments,self.sources,self.cut),before)

    def test_large_registered_coverage_is_indexed_not_rescanned_per_frame_or_event(self):
        class CountedSource(dict):
            reads=0
            def __getitem__(self,key):type(self).reads+=1;return super().__getitem__(key)
            def get(self,key,default=None):type(self).reads+=1;return super().get(key,default)
        frames=500000
        sources=[CountedSource(source(camera,0,frames)) for camera in 'ACD']
        sources += [CountedSource(source('B',first,first+50,str(first))) for first in range(0,frames,100)]
        events=self.conform([core.Segment(0,frames,'B','label_read')],sources=sources,geometry=cut(frames))
        self.assertEqual(len(events),10000)
        self.assertEqual(sum(row['end']-row['start'] for row in events),frames)
        self.assertEqual(sum(row.get('fallback_reason')=='source_gap' for row in events),5000)
        self.assertLess(CountedSource.reads,1000000)


class VariableCoverageReportTests(unittest.TestCase):
    def test_csv_and_review_separate_fallback_intentional_gap_and_unresolved(self):
        config=dict(fps=25,camera_ids=list('ABCD'),missing_camera_policy='available',
            empty_camera_policy='gap',missing_camera_preference='A',calibration_confirmed=True,multicam=True)
        sources=[source(camera,0,80) for camera in 'ACD']+[
            source('B',0,30,'B1'),source('B',60,80,'B2')]
        def write_xml(config,events,sources,target,title,sound=None):target.write_text('<fcpxml/>')
        with tempfile.TemporaryDirectory() as folder,patch('core.build_sources',return_value=sources), \
                patch('cut_xml.timeline_info',return_value=cut()),patch('proxy_workflow.export_mapping'), \
                patch('multicam_xml.write_multicam_xml',side_effect=write_xml):
            events,target=core.export(config,[core.Segment(0,100,'B','label_read')],folder)
            self.assertIn('_CONFORM_',target.name)
            review=json.loads((Path(folder)/'review.json').read_text())
            self.assertEqual(len(review['fallbacks']),1)
            self.assertEqual((review['fallbacks'][0]['start'],review['fallbacks'][0]['end']),(30,60))
            self.assertEqual(len(review['intentional_gaps']),1)
            self.assertEqual((review['intentional_gaps'][0]['start'],review['intentional_gaps'][0]['end']),(80,100))
            self.assertEqual(review['unresolved'],[])
            self.assertIn('该区间无声音',review['fallback_note'])
            self.assertEqual(review['pgm_video_fallbacks'],[])
            with (Path(folder)/'events.csv').open(encoding='utf-8-sig') as handle:rows=list(csv.DictReader(handle))
            fallback=next(row for row in rows if row['fallback_reason'])
            self.assertEqual((fallback['camera'],fallback['requested_camera']),('A','B'))
            self.assertEqual(json.loads(fallback['available_cameras']),list('ACD'))
            self.assertEqual(rows[-1]['empty_camera_gap'],'True')

    def test_unknowns_still_make_review_draft_and_are_not_classified_as_fallback(self):
        config=dict(fps=25,camera_ids=['A'],missing_camera_policy='available',empty_camera_policy='gap',
            calibration_confirmed=True,multicam=True)
        def write_xml(config,events,sources,target,title,sound=None):target.write_text('<fcpxml/>')
        with tempfile.TemporaryDirectory() as folder,patch('core.build_sources',return_value=[source('A')]), \
                patch('cut_xml.timeline_info',return_value=cut()),patch('proxy_workflow.export_mapping'), \
                patch('multicam_xml.write_multicam_xml',side_effect=write_xml):
            events,target=core.export(config,[core.Segment(0,100,'?','sample_conflict')],folder)
            self.assertIn('_DRAFT_',target.name)
            review=json.loads((Path(folder)/'review.json').read_text())
            self.assertEqual(len(review['unresolved']),1)
            self.assertEqual(review['fallbacks'],[]);self.assertEqual(review['intentional_gaps'],[])


if __name__=='__main__':unittest.main()
