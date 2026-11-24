"""Two original mono WAVs retain source identity, stereo routing and A/V links."""
import copy
from fractions import Fraction as F
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from fcpxml_conform import conform_fcpxml, normalize_identified_audio_links, normalize_native_audio_links
from multicam_xml import write_multicam_xml
from proxy_workflow import file_path
from test_fcpxml_links import fixture as native_fixture, add_sound
from test_fcpxml_multitrack_links import fixture as identified_fixture, placements


def number(node, attribute):
    return F(node.get(attribute)[:-1])


def pair_metadata(asset, pair, channel):
    metadata = asset.find('metadata')
    if metadata is None:
        metadata = ET.SubElement(asset, 'metadata')
    for key, value in (('com.pgm.audioPairID', pair), ('com.pgm.audioPairChannel', channel)):
        ET.SubElement(metadata, 'md', key=key, value=value)


def paired_native(container=True, boundary=False):
    root, spine, mc = native_fixture()
    resources = root.find('resources')
    for suffix, start in (('1', 600), ('2', 700)):
        left = resources.find(f'asset[@id="wav-{suffix}"]')
        pair_metadata(left, 'take-' + suffix, 'L')
        right = copy.deepcopy(left)
        right.set('id', 'right-' + suffix)
        right.set('src', 'file:///tmp/right-' + suffix + '.wav')
        right.find('./metadata/md[@key="com.pgm.audioPairChannel"]').set('value', 'R')
        resources.append(right)
    for suffix, position, source, duration in (('1',100,600,2 if boundary else 4),) + ((('2',102,700,2),) if boundary else ()):
        child, left = add_sound(mc, ref='wav-' + suffix, record=position, source=source,
                               duration=duration, asset_start=source)
        left.set('outCh', 'L')
        if container:
            right = copy.deepcopy(left); right.set('ref','right-' + suffix); right.set('outCh','R')
            child.append(right)
        else:
            _, right = add_sound(mc, ref='right-' + suffix, record=position, source=source,
                                 duration=duration, asset_start=source, lane='2')
            right.set('outCh', 'R')
        ET.SubElement(left, 'adjust-volume', amount='-3dB')
        ET.SubElement(right, 'adjust-volume', amount='-6dB')
    return root, spine, mc


def paired_identified(container=False):
    root, rows, guide = identified_fixture()
    resources = root.find('resources')
    for row in list(guide):
        session = row['session_id']; row.update(pair_id='pair-' + session, channel='L')
        original = resources.find(f'asset[@id="{session}a"]')
        right = copy.deepcopy(original); right.set('id',session+'R')
        right.set('src','file:///'+session+'/MIX_R.wav'); resources.append(right)
        guide.append(dict(session_id=session,path='/'+session+'/MIX_R.wav',pair_id='pair-'+session,channel='R'))
    for mc in root.findall('./library/event/project/sequence/spine/mc-clip'):
        for child in list(mc.findall('clip')):
            left = child.find('audio'); session = left.get('ref').removesuffix('a')
            left.set('outCh','L'); ET.SubElement(left,'adjust-volume',amount='-3dB')
            right = copy.deepcopy(left); right.set('ref',session+'R'); right.set('outCh','R')
            right.find('adjust-volume').set('amount','-6dB')
            if container:
                child.append(right)
            else:
                right_clip = copy.deepcopy(child); right_clip.remove(right_clip.find('audio')); right_clip.append(right)
                right_clip.set('lane',str(int(child.get('lane'))+3)); mc.append(right_clip)
    return root, rows, guide


class PairedSoundXMLTests(unittest.TestCase):
    def write(self, directory, unknown=False):
        directory = directory.resolve()
        config = dict(fps=25, camera_count=1, timeline_start_tc='01:00:00:00')
        sources = [dict(camera='A',path=str(directory/'daily.mov'),start=1000,end=1250,
                        frames=250,width=1920,height=1080,reel='A003',audio_channels=[])]
        events = [dict(start=0,end=75,path='' if unknown else sources[0]['path'],camera='A',reason='unknown'),
                  dict(start=75,end=250,path=sources[0]['path'],camera='A')]
        wavs = []; sound_events = []
        for take, record, source in ((1,0,600),(2,5,700)):
            for channel in ('L','R'):
                path = str(directory/f'TAKE{take}_{channel}.wav')
                wavs.append(dict(path=path,start=source*48000,samples=5*48000,sample_rate=48000,
                                 channels=1,channel=channel,pair_id=f'take-{take}',sound_roll='DAY01',
                                 original_audio_filename=Path(path).name,file_uid=f'uid-{take}-{channel}'))
                sound_events.append(dict(path=path,record_start=F(record),source_start=F(source),duration=F(5),
                                         src_channel=1,channel=channel,out_channel=channel,pair_id=f'take-{take}'))
        output = directory/'paired.fcpxml'
        with patch('cut_xml.timeline_info',return_value=dict(duration=250)), patch('cut_xml.locate_frame',side_effect=lambda _,frame: (dict(start_tc='00:00:40:00'),frame)):
            write_multicam_xml(config,events,sources,output,'paired',sound=(wavs,sound_events))
        return ET.parse(output).getroot(),output,sources,wavs

    def test_original_mono_pair_assets_and_routes_are_preserved_across_wav_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            root,_,_,wavs = self.write(Path(temp))
            assets = {node.get('id'):node for node in root.findall('./resources/asset')}
            wrappers = root.findall('./library/event/project/sequence/spine/clip')
            self.assertEqual([len(node.findall('clip/audio')) for node in wrappers],[2,4])
            self.assertTrue(all(node.find('mc-clip') is not None for node in wrappers))
            for wrapper in wrappers:
                for audio in wrapper.findall('clip/audio'):
                    self.assertIsNone(audio.get('lane')); self.assertEqual(audio.get('srcCh'),'1')
                    asset = assets[audio.get('ref')]
                    channel = asset.find('./metadata/md[@key="com.pgm.audioPairChannel"]').get('value')
                    self.assertEqual(audio.get('outCh'),channel)
                    self.assertEqual(asset.get('audioChannels'),'1')
                    self.assertEqual(asset.find('./metadata/md[@key="com.apple.proapps.studio.reel"]').get('value'),'DAY01')
            second = wrappers[1]
            self.assertEqual([(number(node,'offset'),number(node,'start'),number(node,'duration'),node.get('outCh'))
                              for node in second.findall('clip/audio')],
                             [(603,603,2,'L'),(603,603,2,'R'),(700,700,5,'L'),(700,700,5,'R')])
            self.assertEqual([number(node,'offset') for node in second.findall('clip')],[3,3,5,5])
            actual = {file_path(node.get('src')) for node in assets.values() if node.get('hasAudio')=='1'}
            self.assertEqual(actual,{row['path'] for row in wavs})
            self.assertEqual(list(Path(temp).glob('*.wav')),[])  # No fabricated/merged WAV.

    def test_unknown_picture_gap_preserves_both_channels_with_explicit_routing(self):
        with tempfile.TemporaryDirectory() as temp:
            root,_,_,_ = self.write(Path(temp),unknown=True)
            gap = root.find('./library/event/project/sequence/spine/gap')
            connected = gap.findall('clip')
            self.assertEqual([node.get('lane') for node in connected],['-1','-2'])
            self.assertEqual([node.find('audio').get('outCh') for node in connected],['L','R'])
            self.assertTrue(all(number(node,'start')==600 for node in connected))
            self.assertEqual([node.tag for node in gap],['clip','clip','marker'])

    def test_raw_reconform_retains_original_pair_paths_timecodes_and_gain(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp);root,xml,sources,wavs = self.write(directory)
            for audio in root.findall('.//project/sequence/spine/clip/clip/audio'):
                ET.SubElement(audio,'adjust-volume',amount='-4dB')
            ET.indent(root,space='  ')
            original_audio = [ET.tostring(node) for node in root.findall('.//project/sequence/spine/clip/clip/audio')]
            audio_assets = [ET.tostring(node) for node in root.findall('./resources/asset') if node.get('hasAudio')=='1']
            index = {sources[0]['path']:dict(proxy_start=1000,original_start=1000,frames=250,original_path=str(directory/'raw.mov'),session_id='day01')}
            guide = [dict(path=row['path'],session_id='day01',pair_id=row['pair_id'],channel=row['channel']) for row in wavs]
            info = dict(path=str(directory/'raw.mov'),fps=25,frames=250,start_tc='00:00:40:00',width=3840,height=2160,audio_channels=[])
            with patch('core.probe',return_value=info):
                result = conform_fcpxml(ET.ElementTree(root),index,25,xml,directory/'raw.fcpxml',guide_audio=guide)
            self.assertEqual(result['replaced_file_definitions'],1)
            self.assertEqual([ET.tostring(node) for node in root.findall('.//project/sequence/spine/clip/clip/audio')],original_audio)
            self.assertEqual([ET.tostring(node) for node in root.findall('./resources/asset') if node.get('hasAudio')=='1'],audio_assets)

    def test_native_export_with_two_audio_children_is_preserved_for_import_helper(self):
        root,_,_ = paired_native(container=True);before=ET.tostring(root)
        report = normalize_native_audio_links(root)
        self.assertEqual((report['normalized_multicam_clips'],report['normalized_audio_components']),(0,0))
        self.assertEqual(ET.tostring(root),before)
        self.assertIn('导入脚本',report['skipped'][0]['reason'])

    def test_native_export_separate_audio_lanes_and_serial_pairs_remain_intact(self):
        root,_,_ = paired_native(container=False,boundary=True);before=ET.tostring(root)
        report = normalize_native_audio_links(root)
        self.assertEqual((report['normalized_multicam_clips'],report['normalized_audio_components']),(0,0))
        self.assertEqual(ET.tostring(root),before)

    def test_native_does_not_guess_two_unrelated_overlapping_mono_tracks(self):
        root,_,_ = paired_native()
        for metadata in root.findall('./resources/asset/metadata'):
            metadata.clear()
        before = ET.tostring(root);report = normalize_native_audio_links(root)
        self.assertEqual(report['normalized_multicam_clips'],0);self.assertEqual(ET.tostring(root),before)

    def test_identified_pairs_preserve_both_base_and_alternate_for_native_link_helper(self):
        root,rows,guide = paired_identified();before = ET.tostring(root)
        report = normalize_identified_audio_links(root,rows,guide)
        self.assertEqual((report['normalized_multicam_clips'],report['normalized_audio_components']),(0,0))
        self.assertEqual(ET.tostring(root),before)
        self.assertEqual(len(report['skipped']),6)

    def test_identified_two_audio_children_in_same_container_are_not_rewritten(self):
        root,rows,guide = paired_identified(container=True);before = ET.tostring(root)
        report = normalize_identified_audio_links(root,rows,guide)
        self.assertEqual(report['normalized_audio_components'],0);self.assertEqual(ET.tostring(root),before)

    def test_native_export_without_pan_attributes_still_preserves_original_pair_by_saved_identity(self):
        root,rows,guide = paired_identified()
        for audio in root.findall('.//project//audio'):audio.attrib.pop('outCh',None)
        before=ET.tostring(root);report=normalize_identified_audio_links(root,rows,guide)
        self.assertEqual(report['normalized_audio_components'],0);self.assertEqual(ET.tostring(root),before)
        self.assertTrue(all('导入脚本' in row['reason'] for row in report['skipped']))

    def test_incomplete_or_conflicting_pair_identity_never_links_by_overlap_alone(self):
        for change in ('missing','conflict','routing'):
            root,rows,guide = paired_identified()
            if change=='missing':
                guide[:] = [row for row in guide if row.get('channel')!='R']
            elif change=='conflict':
                next(row for row in guide if row.get('channel')=='R')['pair_id']='unrelated'
            else:
                for node in root.findall('.//project//audio'):
                    if node.get('ref')=='baseR':node.set('outCh','L')
            before=placements(root);report=normalize_identified_audio_links(root,rows,guide)
            self.assertEqual(placements(root),before)
            self.assertEqual(report['normalized_multicam_clips'],0)

    def test_duplicate_component_in_other_host_blocks_ambiguous_relink(self):
        root,rows,guide = paired_identified()
        spine=root.find('./library/event/project/sequence/spine')
        first=spine.find('mc-clip')
        existing=ET.SubElement(spine,'gap',offset='3600s',start='0s',duration='3s')
        ET.SubElement(existing,'asset-clip',ref='baseR',offset='0s',start='1000s',duration='3s',srcEnable='audio',lane='8')
        before=placements(root);report=normalize_identified_audio_links(root,rows,guide)
        self.assertEqual(report['normalized_multicam_clips'],0);self.assertEqual(placements(root),before)

    def test_pair_normalization_is_idempotent(self):
        root,rows,guide = paired_identified();normalize_identified_audio_links(root,rows,guide)
        before=ET.tostring(root);report=normalize_identified_audio_links(root,rows,guide)
        self.assertEqual(report['normalized_audio_components'],0);self.assertEqual(ET.tostring(root),before)


if __name__=='__main__':unittest.main()
