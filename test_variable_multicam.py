"""Available-angle resources preserve source clocks, sound and review markers."""
import copy
from fractions import Fraction as F
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from core import AnalysisError
from multicam_xml import write_multicam_xml
from reference_timeline import _prefix
from resolve_import_helper import build_resolve_manifest


def seconds(node, key):
    return F(node.get(key)[:-1])


def fixture(directory, *, empty=False):
    config=dict(fps=25,camera_ids=list('ABCD'),timeline_start_tc='01:00:00:00',multicam_resource_layout='legacy_masks',
                missing_camera_policy='available',empty_camera_policy='gap',
                camera_offsets=dict(A=3))
    sources=[]
    for camera in 'ABCD':
        ranges=[(1000,1100),(1200,1300)] if camera=='B' else [(1003,1303)] if camera=='A' else [(1000,1300)]
        for index,(first,last) in enumerate(ranges):
            sources.append(dict(camera=camera,path=str(directory/f'{camera}_{index}.mov'),
                start=first,end=last,frames=last-first,width=1920,height=1080,reel=camera+'003',
                source_id=f'id-{camera}-{index}',session_id='day04',production_id='show',
                audio_channels=[dict(source_channel=1),dict(source_channel=2)]))
    def event(first,last,camera,mask,**extra):
        source=next((row for row in sources if row['camera']==camera and
            row['start']-config['camera_offsets'].get(camera,0)<=1000+first and
            row['end']-config['camera_offsets'].get(camera,0)>=1000+last),None)
        return dict(start=first,end=last,camera=camera,path=source['path'] if source else '',
                    reel=source['reel'] if source else 'BL',reason='manual',available_cameras=list(mask),**extra)
    events=[event(0,100,'B','ABCD'),event(100,150,'A','ACD',requested_camera='B',fallback_reason='source_gap'),
            event(150,200,'C','ACD'),event(200,250,'D','ABCD'),event(250,300,'B','ABCD')]
    events[1]['reason']='camera_unavailable_fallback'
    if empty:
        events.append(dict(start=300,end=325,path='',reel='BL',camera='B',reason='source_gap',
                           available_cameras=[],empty_camera_gap=True))
    block=dict(path=str(directory/'PGM.mov'),start_tc='00:00:40:00',start=0,
               end=325 if empty else 300,frames=325 if empty else 300)
    cut=dict(duration=block['frames'],fps=25,pgm_sources=[block])
    return config,events,sources,cut


def sound_plan(directory, *, paired=False, pgm=False):
    wavs=[];events=[]
    for channel in ('L','R') if paired else ('LR',):
        path=str(directory/('PGM.mov' if pgm else f'MIX_{channel}.wav'))
        row=dict(path=path,start=600*48000,samples=13*48000,sample_rate=48000,
                 channels=1 if paired else 2,sound_roll='SOUND04',file_uid='uid-'+channel)
        event=dict(path=path,record_start=F(0),source_start=F(600),duration=F(13),
                   src_channel=1 if paired else '1,2',out_channel=channel if paired else 'L,R')
        if paired:
            row.update(pair_id='mix-pair',channel=channel)
            event.update(pair_id='mix-pair',channel=channel)
        if pgm:
            row.update(source_kind='pgm',start_frames=15000,fps=25,frames=325)
        wavs.append(row);events.append(event)
    return wavs,events


class VariableMulticamTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)

    def write(self, *, empty=False, sound=None, mutate=None):
        config,events,sources,cut=fixture(self.folder,empty=empty)
        if mutate:mutate(config,events,sources,cut)
        path=self.folder/'variable.fcpxml'
        with patch('cut_xml.timeline_info',return_value=cut):
            write_multicam_xml(config,events,sources,path,'VARIABLE',sound=sound)
        return ET.parse(path).getroot(),path,config,events,sources,cut

    def test_each_unique_mask_has_one_resource_with_only_its_available_angles(self):
        root,_,_,events,sources,_=self.write()
        resources={row.get('id'):row for row in root.findall('./resources/media')}
        self.assertEqual(set(resources),{'multicam-ABCD','multicam-ACD'})
        self.assertEqual([row.get('name') for row in resources['multicam-ACD'].findall('multicam/mc-angle')],list('ACD'))
        clips=root.findall('./library/event/project/sequence/spine/mc-clip')
        self.assertEqual([row.get('ref') for row in clips],['multicam-ABCD','multicam-ACD','multicam-ACD','multicam-ABCD','multicam-ABCD'])
        self.assertEqual(len(clips),len(events));self.assertEqual(len(root.findall('./resources/asset')),len(sources))
        self.assertEqual(len({row.get('name') for row in resources.values()}),2)
        # A/C/D assets are shared across both masks rather than cloned per edit.
        refs=[row.get('ref') for row in root.findall('./resources/media/multicam/mc-angle/clip/video')]
        self.assertEqual(len(set(refs)),len(sources));self.assertGreater(len(refs),len(sources))

    def test_global_angle_ids_are_unique_for_cross_session_merge_and_selected_ref_is_local(self):
        root,path,_,_,_,_=self.write()
        angles=[row.get('angleID') for row in root.findall('./resources/media/multicam/mc-angle')]
        self.assertEqual(len(angles),len(set(angles)))
        self.assertIsNone(build_resolve_manifest(path))  # No original paired sound requires the helper.
        _prefix(root,'day04-')
        resources={row.get('id'):row for row in root.findall('./resources/media')}
        for clip in root.iter('mc-clip'):
            angle=clip.find('mc-source').get('angleID')
            self.assertIn(angle,[row.get('angleID') for row in resources[clip.get('ref')].findall('multicam/mc-angle')])

    def test_global_multicam_clock_and_original_source_metadata_do_not_rebase(self):
        root,_,_,_,sources,_=self.write()
        for media in root.findall('./resources/media'):
            mc=media.find('multicam')
            self.assertEqual(seconds(mc,'duration'),F(12));self.assertEqual(mc.get('tcStart'),'0s')
            for row in mc.findall('mc-angle[@name="A"]/clip'):
                self.assertEqual(seconds(row,'offset'),F(0))
                self.assertEqual(seconds(row,'start'),F(1003,25))
        clips=root.findall('./library/event/project/sequence/spine/mc-clip')
        self.assertEqual([seconds(row,'start') for row in clips],[0,4,6,8,10])
        self.assertEqual([seconds(row,'offset') for row in clips],[3600,3604,3606,3608,3610])
        assets=root.findall('./resources/asset')
        for asset,source in zip(assets,sources):
            self.assertEqual(seconds(asset,'start'),F(source['start'],25))
            self.assertEqual(asset.find('./metadata/md[@key="com.pgm.sourceID"]').get('value'),source['source_id'])
            self.assertEqual(asset.find('./metadata/md[@key="com.apple.proapps.studio.reel"]').get('value'),source['reel'])

    def test_fallback_marker_uses_mc_clip_source_coordinates(self):
        root,_,_,_,_,_=self.write()
        item=root.findall('./library/event/project/sequence/spine/mc-clip')[1]
        marker=item.find('marker')
        self.assertEqual(marker.get('value'),'REVIEW_CAMERA_FALLBACK')
        self.assertIn('原机位 B',marker.get('note'));self.assertIn('改为 A',marker.get('note'))
        self.assertEqual(marker.get('start'),item.get('start'))
        self.assertEqual(marker.get('duration'),item.get('duration'))
        self.assertEqual(item.find('mc-source').get('angleID'),'angle-ACD-A')

    def test_fallback_sound_wrapper_retains_marker_stereo_and_exact_link_coordinates(self):
        root,path,_,_,_,_=self.write(sound=sound_plan(self.folder))
        wrapper=root.findall('./library/event/project/sequence/spine/clip')[1]
        item=wrapper.find('mc-clip');audio=wrapper.find('audio');marker=item.find('marker')
        self.assertEqual(seconds(wrapper,'start'),4);self.assertEqual(seconds(item,'offset'),4)
        self.assertEqual(seconds(marker,'start'),4);self.assertEqual(seconds(audio,'offset'),4)
        self.assertEqual(seconds(audio,'start'),604);self.assertEqual(audio.get('srcCh'),'1, 2')
        self.assertEqual(audio.get('outCh'),'L,R');self.assertIsNone(audio.get('lane'))
        self.assertEqual(item.get('srcEnable'),'video')
        self.assertIsNone(build_resolve_manifest(path))  # Direct stereo stays linked natively.
        self.assertEqual(len([row for row in root.findall('./resources/asset') if row.get('hasAudio')=='1']),1)

    def test_paired_original_mono_sound_and_markers_remain_linkable(self):
        root,path,_,_,_,_=self.write(sound=sound_plan(self.folder,paired=True))
        wrapper=root.findall('./library/event/project/sequence/spine/clip')[1]
        connected=wrapper.findall('clip');self.assertEqual([row.get('lane') for row in connected],['-1','-2'])
        self.assertEqual([row.find('audio').get('outCh') for row in connected],['L','R'])
        self.assertEqual([seconds(row.find('audio'),'start') for row in connected],[604,604])
        self.assertIsNotNone(wrapper.find('mc-clip/marker'))
        self.assertEqual(len(build_resolve_manifest(path)['groups']),5)

    def test_explicit_no_camera_gap_keeps_duration_marker_and_original_sound(self):
        root,_,_,_,_,_=self.write(empty=True,sound=sound_plan(self.folder))
        seq=root.find('./library/event/project/sequence');gap=seq.find('spine/gap')
        self.assertEqual(seconds(seq,'duration'),13)
        self.assertEqual((gap.get('name'),seconds(gap,'offset'),seconds(gap,'duration')),('NO_CAMERA_SOURCE',3612,1))
        marker=gap.find('marker');self.assertEqual(marker.get('value'),'NO_CAMERA_SOURCE')
        self.assertEqual(marker.get('start'),gap.get('start'));self.assertIn('全部机位',marker.get('note'))
        audio=gap.find('asset-clip');self.assertEqual(seconds(audio,'start'),612)
        self.assertEqual(seconds(audio,'duration'),1);self.assertEqual(list(gap)[-1].tag,'marker')
        self.assertEqual(len(root.findall('./resources/media')),2)

    def test_empty_gap_removes_legacy_pgm_sound_without_fabricating_picture(self):
        root,_,_,_,_,_=self.write(empty=True,sound=sound_plan(self.folder,pgm=True))
        gap=root.find('./library/event/project/sequence/spine/gap')
        self.assertIsNone(gap.find('asset-clip'));self.assertFalse(root.findall('.//audio'))
        self.assertFalse(root.findall('./resources/asset[@hasAudio="1"]'))
        self.assertEqual(seconds(gap,'duration'),1)

    def test_empty_gap_preserves_both_original_mono_components(self):
        root,_,_,_,_,_=self.write(empty=True,sound=sound_plan(self.folder,paired=True))
        gap=root.find('./library/event/project/sequence/spine/gap')
        self.assertEqual([row.get('lane') for row in gap.findall('clip')],['-1','-2'])
        self.assertEqual([row.get('outCh') for row in gap.findall('clip/audio')],['L','R'])
        self.assertTrue(all(seconds(row,'start')==612 for row in gap.findall('clip/audio')))

    def test_unknown_remains_review_unknown_and_is_not_an_intentional_gap(self):
        def mutate(config,events,sources,cut):
            events[1].update(path='',reel='BL',camera='?',available_cameras=[],reason='sample_conflict')
        root,_,_,_,_,_=self.write(mutate=mutate)
        gap=root.find('./library/event/project/sequence/spine/gap')
        self.assertEqual(gap.get('name'),'REVIEW_UNKNOWN')
        self.assertEqual(gap.find('marker').get('value'),'REVIEW_UNKNOWN')
        self.assertEqual(seconds(gap,'duration'),2)

    def test_one_two_and_more_than_four_available_angles_are_not_hardcoded(self):
        def mutate(config,events,sources,cut):
            config['camera_ids']=list('ABCDEF')
            for camera in 'EF':
                sources.append(dict(sources[2],camera=camera,path=str(self.folder/(camera+'.mov')),reel=camera+'003'))
            events[0]['available_cameras']=['B']
            events[2]['available_cameras']=['C','D']
            events[3]['available_cameras']=list('ABCDEF')
        root,_,_,_,_,_=self.write(mutate=mutate)
        masks={tuple(row.get('name') for row in media.findall('multicam/mc-angle')) for media in root.findall('./resources/media')}
        self.assertTrue({('B',),('C','D'),tuple('ABCDEF')}<=masks)

    def test_resource_count_does_not_grow_with_repeated_events(self):
        def mutate(config,events,sources,cut):
            events[:]=[dict(events[1],start=index,end=index+1) for index in range(100,200)]
        root,_,_,_,_,_=self.write(mutate=mutate)
        self.assertEqual(len(root.findall('./resources/media')),1)
        self.assertEqual(len(list(root.iter('mc-clip'))),100)
        self.assertEqual(len(root.findall('./resources/asset')),5)

    def test_legacy_without_availability_keeps_original_resource_and_angle_ids(self):
        def mutate(config,events,sources,cut):
            for event in events:
                event.pop('available_cameras');event.pop('requested_camera',None);event.pop('fallback_reason',None)
                event['reason']='manual'
        root,_,_,_,_,_=self.write(mutate=mutate)
        media=root.findall('./resources/media');self.assertEqual(len(media),1)
        self.assertEqual(media[0].get('id'),'multicam-main')
        self.assertEqual([row.get('angleID') for row in media[0].findall('multicam/mc-angle')],['angle-'+camera for camera in 'ABCD'])
        self.assertTrue(all(row.get('value')=='REVIEW_AUDIO_GAP' for row in root.iter('marker')))

    def test_invalid_selected_angle_or_unknown_mask_never_fabricates_picture(self):
        for values in ([],['C','D'],['A','A'],['A','Z'],'ACD'):
            def mutate(config,events,sources,cut):events[1]['available_cameras']=values
            with self.subTest(values=values),self.assertRaises(AnalysisError):self.write(mutate=mutate)

    def test_nonselected_overlap_is_unavailable_only_during_its_ambiguous_clock(self):
        def mutate(config,events,sources,cut):
            config['camera_offsets']['C']=-7
            original=next(row for row in sources if row['camera']=='C')
            original.update(start=993,end=1293)
            sources.append(dict(original,path=str(self.folder/'C_overlap.mov'),start=1093,end=1193,frames=100))
            events[1]['available_cameras']=list('AD');events[2]['available_cameras']=list('AD')
            events[2].update(camera='D',path=sources[-2]['path'])
        root,_,_,_,_,_=self.write(mutate=mutate)
        angle=root.find('./resources/media[@id="multicam-ABCD"]/multicam/mc-angle[@name="C"]')
        self.assertEqual([row.tag for row in angle],['clip','gap','clip'])
        self.assertEqual([(seconds(row,'offset'),seconds(row,'duration')) for row in angle],[(0,4),(4,4),(8,4)])
        for clip,source_start in zip(angle.findall('clip'),(F(993,25),F(1193,25))):
            self.assertEqual(seconds(clip,'start'),source_start)
            self.assertEqual(seconds(clip.find('video'),'start'),source_start)
            self.assertIsNone(clip.find('audio'))
            self.assertEqual(clip.find('video').get('duration'),clip.get('duration'))
        # The conflicted recording remains in source metadata, not silently
        # discarded or substituted for the only uniquely covered original.
        self.assertEqual(len(root.findall('./resources/asset')),6)
        original_ref=angle.find('clip/video').get('ref')
        self.assertEqual(angle.findall('clip')[1].find('video').get('ref'),original_ref)

    def test_legacy_overlap_still_blocks_instead_of_choosing_one_source(self):
        def mutate(config,events,sources,cut):
            for event in events:event.pop('available_cameras')
            sources.append(copy.deepcopy(sources[-1]))
        with self.assertRaisesRegex(AnalysisError,'重叠'):self.write(mutate=mutate)

    def test_invalid_source_frame_extent_never_writes_out_of_asset_bounds(self):
        def mutate(config,events,sources,cut):sources[-1]['frames']-=1
        with self.assertRaisesRegex(AnalysisError,'时长与源时码'):self.write(mutate=mutate)
        self.assertFalse((self.folder/'variable.fcpxml').exists())

    def test_all_duplicate_camera_coverage_becomes_gap_not_arbitrary_first_file(self):
        def mutate(config,events,sources,cut):
            duplicate=copy.deepcopy(sources[-1]);duplicate['path']=str(self.folder/'D_duplicate.mov')
            sources.append(duplicate)
            for event in events:
                event['available_cameras'].remove('D')
                if event['camera']=='D':event.update(camera='C',path=sources[3]['path'])
        root,_,_,_,_,_=self.write(mutate=mutate)
        self.assertFalse(root.findall('./resources/media/multicam/mc-angle[@name="D"]'))
        self.assertEqual(len(root.findall('./resources/asset')),6)

    def test_raw_reconform_keeps_mask_resources_selections_markers_and_original_audio(self):
        from fcpxml_conform import conform_fcpxml
        root,xml,_,_,sources,_=self.write(sound=sound_plan(self.folder,paired=True),empty=True)
        before_library=ET.tostring(root.find('library'))
        before_media=[ET.tostring(row) for row in root.findall('./resources/media')]
        before_audio=[ET.tostring(row) for row in root.iter('audio')]
        index={row['path']:dict(proxy_start=row['start'],original_start=row['start'],
            frames=row['frames'],original_path=str(self.folder/('raw_'+Path(row['path']).name))) for row in sources}
        identities={row['source_id']:index[row['path']] for row in sources}
        def probe(path):
            row=next(row for row in index.values() if row['original_path']==str(path))
            return dict(fps=25,frames=row['frames'],start_tc='',width=3840,height=2160,audio_channels=[])
        with patch('core.probe',side_effect=probe):
            report=conform_fcpxml(ET.ElementTree(root),index,25,xml,self.folder/'raw.fcpxml',identity_index=identities)
        self.assertEqual(report['replaced_file_definitions'],len(sources))
        self.assertEqual(ET.tostring(root.find('library')),before_library)
        self.assertEqual([ET.tostring(row) for row in root.iter('audio')],before_audio)
        # Resource timing/angle identities stay fixed; only video asset refs
        # change to original files, also for shared assets across masks.
        for before,after in zip(before_media,root.findall('./resources/media')):
            previous=ET.fromstring(before);current=copy.deepcopy(after)
            current.tail=None
            for row in current.iter('video'):row.set('ref',row.get('ref').split('-original-')[0])
            self.assertEqual(ET.tostring(current),ET.tostring(previous))


if __name__=='__main__':unittest.main()
