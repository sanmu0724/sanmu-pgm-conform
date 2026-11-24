"""Snapshot-only performance optimizations retain exact mapping semantics."""
import copy
from fractions import Fraction as F
import random
import unittest
from unittest.mock import patch

from core import frames_tc
import library_correspondence
from library_angles import AngleMapper
from library_workflow import enrich_data
from test_library_angles import catalogue, session_config


class MatchingCacheRegressions(unittest.TestCase):
    def fixture(self, count=40):
        config=session_config()
        config['sources']=[]
        for number in range(count):
            for camera in 'ABCD':
                config['sources'].append(dict(camera=camera,path=f'/qa/{number}_{camera}.mov',
                    fps='25',frames=1000,source_id=f'{number}-{camera}',
                    start_tc=frames_tc(1080000+number*1000,25)))
        result=catalogue(config)
        result['candidates'][0].update(session_id='s1',media_path='/qa/s1/PGM.mov')
        return result

    def unfiltered(self,data,clip=None):
        mapper=AngleMapper(data['sessions'])
        mapper.proxy_headers={sid:None for sid in mapper.proxy_headers}
        mapper.proxy_indexes={sid:None for sid in mapper.proxy_indexes}
        return mapper.fields(clip or data['candidates'][0],data['transcripts'][0])

    def test_distant_valid_headers_are_not_reparsed_per_utterance(self):
        data=self.fixture()
        old=AngleMapper(data['sessions']);old.proxy_headers={'s1':None}
        old.proxy_indexes={'s1':None}
        new=AngleMapper(data['sessions'])
        with patch('library_correspondence._clock_start',wraps=library_correspondence._clock_start) as parser:
            expected=old.fields(data['candidates'][0],data['transcripts'][0])
            previous=parser.call_count
        with patch('library_correspondence._clock_start',wraps=library_correspondence._clock_start) as parser:
            actual=new.fields(data['candidates'][0],data['transcripts'][0])
            current=parser.call_count
        self.assertEqual(actual,expected)
        self.assertEqual(previous,161)
        self.assertEqual(current,5)

    def test_randomized_mapping_matches_full_header_scan_including_warnings(self):
        rng=random.Random(20261012)
        for trial in range(120):
            data=self.fixture(6)
            config=data['sessions'][0]['config']
            config['calibration_confirmed']=bool(trial%2)
            config['camera_offsets']={camera:rng.choice([-20,0,3,1500,'oops',True,F(1,2)]) for camera in 'ABCD'}
            config['pgm_sources'][0]['frames']=6500
            transcript=data['transcripts'][0]
            transcript.update(start_seconds=rng.randrange(0,220),end_seconds=0)
            transcript['end_seconds']=transcript['start_seconds']+rng.choice([.001,3,39,55])
            for row in config['sources']:
                choice=rng.randrange(10)
                if choice==0:row['start_tc']='invalid'
                elif choice==1:row['frames']=0
                elif choice==2:row['fps']='24'
                elif choice==3:row['path']=''
                elif choice==4:row['start_tc']='12:00:00:00'  # Real overlaps survive.
                elif choice==5:row.pop('fps')  # Legacy session-rate fallback.
            before=copy.deepcopy(data)
            actual=AngleMapper(data['sessions']).fields(data['candidates'][0],transcript)
            expected=self.unfiltered(data)
            self.assertEqual(actual,expected,trial)
            self.assertEqual(data,before)

    def test_new_snapshot_calibration_and_source_rate_do_not_reuse_old_headers(self):
        data=self.fixture()
        previous=AngleMapper(data['sessions']).fields(data['candidates'][0],data['transcripts'][0])
        data['sessions'][0]['config'].update(camera_offsets={'A':3},calibration_confirmed=False)
        data['sessions'][0]['config']['sources'][1]['fps']='24'
        current=AngleMapper(data['sessions']).fields(data['candidates'][0],data['transcripts'][0])
        self.assertEqual(current,self.unfiltered(data))
        self.assertNotEqual(current,previous)
        angle=next(row for row in current['related_angles'] if row['camera']=='A')
        self.assertEqual(angle['frame_in'],253)
        self.assertFalse(angle['calibration_confirmed'])
        self.assertFalse(any(row['camera']=='B' for row in current['related_angles']))

    def test_association_lists_are_derived_once_per_session_and_refresh_on_new_snapshot(self):
        import library_workflow as workflow
        data=self.fixture(3)
        for n in range(1,12):
            data['transcripts'].append(dict(data['transcripts'][0],id=f't{n+1}',start_seconds=n,end_seconds=n+1))
            data['candidates'].append(dict(data['candidates'][0],id=f'm{n+1}',transcript_id=f't{n+1}'))
        with patch('library_workflow._pgms',wraps=workflow._pgms) as pgms, \
                patch('library_workflow._association',wraps=workflow._association) as associations:
            first=enrich_data(data)
        self.assertEqual(pgms.call_count,1)
        self.assertEqual(associations.call_count,1)
        data['sessions'][0]['config']['sources'].append(dict(camera='A',path='/new/A.mov',fps='25',frames=1000,start_tc='15:00:00:00'))
        current=enrich_data(data)
        self.assertNotIn('/new/A.mov',first['candidates'][0]['proxy_paths'])
        self.assertIn('/new/A.mov',current['candidates'][0]['proxy_paths'])


if __name__=='__main__':unittest.main()
