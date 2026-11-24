"""Compact per-PGM multicams keep original clocks and real recording gaps."""
from fractions import Fraction as F
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
from core import Segment,conform_events,frames_tc
from multicam_xml import write_multicam_xml


BASE=90000

def source(camera,first,last,name=None,offset=0):
    return dict(camera=camera,path='/'+(name or camera)+'.mov',start=BASE+first+offset,end=BASE+last+offset,
        frames=last-first,reel=camera,width=320,height=180,audio_channels=[])


def time(node,field):return F(node.get(field)[:-1])*25


class CompactMulticamTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.folder=Path(self.temp.name)
        self.config=dict(fps=25,camera_ids=list('ABCD'),missing_camera_policy='available',empty_camera_policy='pgm',
            multicam_resource_layout='pgm',timeline_width=320,timeline_height=180,timeline_start_tc='01:00:00:00')
        self.cut=dict(fps=25,duration=250,pgm_sources=[dict(path='/PGM1.mov',start=0,end=200,frames=200,start_tc=frames_tc(BASE,25)),
            dict(path='/PGM2.mov',start=200,end=250,frames=50,start_tc=frames_tc(BASE+325,25))])
        self.sources=[source(c,0,400) for c in 'ACD']+[source('B',-2500,-2475,'B_TEST'),source('B',0,60,'B1'),source('B',100,200,'B2')]

    def write(self,sources=None,cut=None,segments=None,config=None,sound=None):
        sources=self.sources if sources is None else sources;cut=cut or self.cut;config=config or self.config
        with patch('cut_xml.timeline_info',return_value=cut):
            events=conform_events(config,segments or [Segment(0,cut['duration'],'B','manual')],sources)
            path=self.folder/'compact.fcpxml';write_multicam_xml(config,events,sources,path,'COMPACT',sound=sound)
        return ET.parse(path).getroot(),events,sources,cut

    def assert_selected_sources(self,root,events):
        assets={a.get('id'):a for a in root.findall('./resources/asset')}
        media={a.get('id'):a for a in root.findall('./resources/media')}
        items=root.findall('./library/event/project/sequence/spine//mc-clip')
        self.assertEqual(len(items),len(events))
        for item,event in zip(items,events):
            angle_id=item.find('mc-source').get('angleID')
            angle=next(a for a in media[item.get('ref')].findall('multicam/mc-angle') if a.get('angleID')==angle_id)
            first=time(item,'start');last=first+time(item,'duration')
            parts=[c for c in angle.findall('clip') if time(c,'offset')<=first and time(c,'offset')+time(c,'duration')>=last]
            self.assertEqual(len(parts),1,(event,ET.tostring(angle)))
            part=parts[0];leaf=part.find('video');asset=assets[leaf.get('ref')]
            self.assertEqual(time(leaf,'start')+first-time(part,'offset'),event['source_in'])
            self.assertEqual(Path(asset.get('src').removeprefix('file://')).name,Path(event['path']).name)

    def test_one_resource_per_pgm_with_camera_union_and_local_source_clock(self):
        root,events,_,_=self.write()
        resources=root.findall('./resources/media');self.assertEqual(len(resources),2)
        self.assertEqual([[a.get('name') for a in r.findall('multicam/mc-angle')] for r in resources],[list('ABCD'),list('ACD')])
        self.assertEqual([time(r.find('multicam'),'duration') for r in resources],[200,50])
        self.assertTrue(all(r.find('multicam').get('tcStart')=='0s' for r in resources))
        self.assert_selected_sources(root,events)
        selected=root.findall('./library/event/project/sequence/spine//mc-clip')
        self.assertEqual([time(n,'start') for n in selected],[0,60,100,0])
        self.assertTrue(all(Path(c.get('name')).name!='B_TEST.mov' for r in resources for c in r.findall('.//clip')))
        self.assertEqual(time(root.find('./library/event/project/sequence'),'duration'),250)

    def test_partial_b_recording_is_real_angle_gap_not_missing_whole_pgm(self):
        root,events,_,_=self.write();angle=root.find('./resources/media/multicam/mc-angle[@name="B"]')
        self.assertEqual([(time(c,'offset'),time(c,'duration')) for c in angle.findall('clip')],[(0,60),(100,100)])
        self.assertEqual([(time(g,'offset'),time(g,'duration')) for g in angle.findall('gap')],[(60,40)])
        self.assertEqual(events[1]['camera'],'A');self.assertEqual(events[1]['fallback_reason'],'source_gap')

    def test_camera_offset_and_trim_preserve_full_asset_source_metadata(self):
        sources=[source('A',0,400,offset=3)]+[s for s in self.sources if s['camera']!='A']
        root,events,_,_=self.write(sources=sources,config=dict(self.config,camera_offsets={'A':3}))
        self.assert_selected_sources(root,events)
        asset=next(a for a in root.findall('./resources/asset') if a.get('name')=='A.mov')
        self.assertEqual((time(asset,'start'),time(asset,'duration')),(BASE+3,400))
        second=root.findall('./resources/media')[1].find('multicam/mc-angle[@name="A"]/clip')
        self.assertEqual((time(second,'offset'),time(second,'start'),time(second,'duration')),(0,BASE+328,50))

    def test_nonselected_overlap_is_an_angle_gap_and_never_arbitrary_file(self):
        sources=self.sources+[source('B',30,50,'B_DUP')]
        root,events,_,_=self.write(sources=sources,segments=[Segment(0,250,'A','manual')])
        angle=root.find('./resources/media/multicam/mc-angle[@name="B"]')
        self.assertEqual([(time(g,'offset'),time(g,'duration')) for g in angle.findall('gap')],[(30,20),(60,40)])
        self.assertFalse(any(c.get('name')=='B_DUP.mov' for c in angle.findall('clip')))
        self.assert_selected_sources(root,events)

    def test_zero_proxy_head_tail_use_silent_marked_pgm_angle_only_there(self):
        cut=dict(fps=25,duration=200,pgm_sources=[dict(path='/PGM.mov',start=0,end=200,frames=200,start_tc=frames_tc(BASE,25))])
        wav=dict(path='/original.wav',start=0,samples=480000,sample_rate=48000,channels=1)
        sound=([wav],[dict(path='/original.wav',record_start=F(0),source_start=F(0),duration=F(8),src_channel=1)])
        root,events,_,_=self.write(sources=[source('A',50,150)],cut=cut,segments=[Segment(0,200,'A','manual')],sound=sound)
        self.assertEqual([bool(e.get('pgm_video_fallback')) for e in events],[True,False,True])
        self.assertEqual([(e['source_in'],e['source_out']) for e in events],[(BASE,BASE+50),(BASE+50,BASE+150),(BASE+150,BASE+200)])
        resource=root.find('./resources/media');self.assertEqual([a.get('name') for a in resource.findall('multicam/mc-angle')],['A','PGM_REF'])
        angle=resource.find('multicam/mc-angle[@name="PGM_REF"]')
        self.assertEqual([(time(c,'offset'),time(c,'duration')) for c in angle.findall('clip')],[(0,50),(150,50)])
        self.assertEqual([(time(g,'offset'),time(g,'duration')) for g in angle.findall('gap')],[(50,100)])
        asset=root.find('./resources/asset[@id="pgm-video-1"]');self.assertIsNone(asset.get('hasAudio'))
        self.assertEqual((time(asset,'start'),time(asset,'duration')),(BASE,200))
        self.assertEqual(asset.find('./metadata/md[@key="com.pgm.videoSourceKind"]').get('value'),'pgm_fallback')
        fallback=[c for c in root.findall('./library/event/project/sequence/spine/mc-clip') if c.find('./marker[@value="PGM_VIDEO_FALLBACK"]') is not None]
        self.assertEqual(len(fallback),2);self.assertTrue(all(not c.findall('.//audio') for c in fallback))
        self.assertEqual(sum(time(n,'duration') for n in root.findall('./library/event/project/sequence/spine/*')),200)
        # The only WAV component belongs to the actual proxy portion.
        self.assertEqual(len(root.findall('./library/event/project/sequence/spine//audio')),1)
        self.assert_selected_sources(root,events)

    def test_entire_pgm_without_proxy_has_single_pgm_ref_not_fake_camera(self):
        cut=dict(fps=25,duration=100,pgm_sources=[dict(path='/PGM.mov',start=0,end=100,frames=100,start_tc=frames_tc(BASE,25))])
        root,events,_,_=self.write(sources=[source('A',500,600)],cut=cut,segments=[Segment(0,100,'?','sample_conflict')])
        self.assertEqual([a.get('name') for a in root.findall('./resources/media/multicam/mc-angle')],['PGM_REF'])
        self.assertTrue(events[0]['pgm_video_fallback']);self.assertEqual(events[0]['camera'],'?')
        self.assert_selected_sources(root,events)

    def test_identity_unconfirmed_and_overlap_are_never_pgm_substituted(self):
        cut=dict(fps=25,duration=100,pgm_sources=[dict(path='/PGM.mov',start=0,end=100,frames=100,start_tc=frames_tc(BASE,25))])
        for sources,reason in [([source('A',500,600)],'identity_unconfirmed'),([source('A',0,100),source('A',0,100,'DUP')],'manual')]:
            with self.subTest(reason=reason),patch('cut_xml.timeline_info',return_value=cut):
                events=conform_events(self.config,[Segment(0,100,'A',reason)],sources)
            self.assertFalse(events[0].get('pgm_video_fallback'));self.assertFalse(events[0]['path'])

    def test_default_available_policy_is_pgm_local_with_pgm_video_for_no_proxy(self):
        config=dict(self.config);config.pop('multicam_resource_layout');config.pop('empty_camera_policy')
        cut=dict(fps=25,duration=100,pgm_sources=[dict(path='/PGM.mov',start=0,end=100,frames=100,start_tc=frames_tc(BASE,25))])
        root,events,_,_=self.write(sources=[source('A',500,600)],cut=cut,config=config,segments=[Segment(0,100,'?','manual')])
        self.assertTrue(events[0]['pgm_video_fallback'])
        self.assertEqual(root.find('./resources/media').get('id'),'multicam-pgm-1')
        self.assertEqual(root.find('./resources/media/multicam/mc-angle').get('name'),'PGM_REF')

    def test_many_hard_cuts_reuse_same_pgm_resource(self):
        cut=dict(fps=25,duration=1000,pgm_sources=[dict(path='/PGM.mov',start=0,end=1000,frames=1000,start_tc=frames_tc(BASE,25))])
        sources=[source(c,0,1000) for c in 'ABCD'];segments=[Segment(i,i+1,'A','manual') for i in range(1000)]
        root,_,_,_=self.write(sources=sources,cut=cut,segments=segments)
        self.assertEqual(len(root.findall('./resources/media')),1)
        self.assertEqual(len(root.findall('./resources/media/multicam/mc-angle/clip')),4)
        self.assertEqual(len(root.findall('./library/event/project/sequence/spine/mc-clip')),1000)


if __name__=='__main__':unittest.main()
