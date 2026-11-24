"""Rebuilding native linked audio never collapses flat multicam video tracks."""
import copy
from fractions import Fraction as F
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from fcpxml_conform import conform_fcpxml,normalize_native_audio_links
import test_fcpxml_links as native
from test_fcpxml_multitrack_links import placements


def flat_fixture():
    root,spine,first=native.fixture()
    for wav in root.findall('./resources/asset[@hasAudio="1"]'):
        wav.set('audioRate','48000')
        ET.SubElement(ET.SubElement(wav,'metadata'),'md',key='com.pgm.audioBitDepth',value='24')
    first.set('lane','1');first.set('format','fmt')
    child,audio=native.add_sound(first,source=F(600)+F(11,48000))
    root.find('./resources/asset[@id="wav-1"]').set('audioChannels','2')
    audio.set('srcCh','1, 2');audio.set('outCh','L,R');audio.set('role','dialogue.pgm-track-2')
    for number,lane in enumerate(('3','7'),1):
        duplicate=copy.deepcopy(first)
        duplicate.set('name','alternate-'+lane)
        duplicate.set('lane',lane)
        duplicate.set('offset',native.ts(F(3605)+F(number,25)))
        duplicate.find('clip/audio').set('role','dialogue.pgm-track-'+str(int(lane)+1))
        spine.append(duplicate)
    return root,spine


class NativeVideoLaneTests(unittest.TestCase):
    def test_flat_multitrack_native_components_transfer_lane_to_wrapper_once(self):
        root,spine=flat_fixture();before=placements(root)
        resources=ET.tostring(root.find('resources'))
        report=normalize_native_audio_links(root)
        wrappers=spine.findall('clip')
        self.assertEqual(report['normalized_multicam_clips'],3)
        self.assertEqual([row.get('lane') for row in wrappers],['1','3','7'])
        self.assertTrue(all(row.get('format')=='fmt' for row in wrappers))
        self.assertTrue(all(row.find('mc-clip').get('lane') is None for row in wrappers))
        self.assertEqual([row.find('mc-clip').get('offset') for row in wrappers],['100/1s']*3)
        self.assertEqual(before,placements(root))
        self.assertEqual(ET.tostring(root.find('resources')),resources)
        self.assertEqual([F(row.find('audio').get('start')[:-1]) for row in wrappers],[F(600)+F(11,48000)]*3)
        self.assertEqual([row.find('audio').get('role') for row in wrappers],
                         ['dialogue.pgm-track-2','dialogue.pgm-track-4','dialogue.pgm-track-8'])

    def test_primary_without_lane_stays_primary_and_second_pass_is_idempotent(self):
        root,spine,mc=native.fixture();native.add_sound(mc)
        report=normalize_native_audio_links(root)
        self.assertEqual(report['normalized_multicam_clips'],1)
        self.assertIsNone(spine.find('clip').get('lane'))
        self.assertIsNone(spine.find('clip/mc-clip').get('lane'))
        before=ET.tostring(root)
        self.assertEqual(normalize_native_audio_links(root)['normalized_multicam_clips'],0)
        self.assertEqual(ET.tostring(root),before)

    def test_complex_flat_take_is_preserved_without_partial_lane_transfer(self):
        root,spine=flat_fixture();bad=spine.findall('mc-clip')[1]
        bad.insert(0,ET.Element('timeMap'))
        before=ET.tostring(bad)
        report=normalize_native_audio_links(root)
        self.assertEqual(report['normalized_multicam_clips'],2)
        self.assertEqual(ET.tostring(spine.find('mc-clip')),before)
        self.assertEqual(spine.find('mc-clip').get('lane'),'3')
        self.assertEqual({row.get('lane') for row in spine.findall('clip')},{'1','7'})

    def test_raw_reconform_keeps_distinct_video_lanes_and_exact_original_wave_samples(self):
        root,_spine=flat_fixture();before=placements(root)
        sound=ET.tostring(root.find('./resources/asset[@id="wav-1"]'))
        index={str(Path('/tmp/proxy.mov').resolve()):dict(original_path='/tmp/original.mov',
            original_start=2500,proxy_start=2500,frames=500)}
        info=dict(path='/tmp/original.mov',fps=25,frames=500,start_tc='00:01:40:00',
                  width=3840,height=2160,audio_channels=[])
        with tempfile.TemporaryDirectory() as directory,patch('core.probe',return_value=info):
            folder=Path(directory);output=folder/'original.fcpxml'
            report=conform_fcpxml(ET.ElementTree(root),index,25,folder/'edited.fcpxml',output)
            saved=ET.parse(output).getroot();spine=saved.find('./library/event/project/sequence/spine')
            self.assertEqual([row.get('lane') for row in spine.findall('clip')],['1','3','7'])
            self.assertEqual(report['audio_link_normalization']['normalized_multicam_clips'],3)
            self.assertTrue(all(row.find('mc-clip').get('lane') is None for row in spine.findall('clip')))
            self.assertEqual(before,placements(saved))
            self.assertEqual(F(spine.find('clip/audio').get('start')[:-1]),F(600)+F(11,48000))
            self.assertEqual(ET.canonicalize(ET.tostring(saved.find('./resources/asset[@id="wav-1"]')),strip_text=True),
                             ET.canonicalize(sound,strip_text=True))
            self.assertTrue(all('original' in node.get('ref') for node in saved.findall('./resources/media/multicam/mc-angle/clip/video')))


if __name__=='__main__':unittest.main()
