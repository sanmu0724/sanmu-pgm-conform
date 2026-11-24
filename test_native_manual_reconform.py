"""Real Resolve connected multicam structure reimports as flat linked A/V."""
import copy
from fractions import Fraction as F
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from fcpxml_conform import standardize_connected_native_interchange,conform_fcpxml
from resolve_import_helper import build_resolve_manifest
import test_fcpxml_multitrack_links as native


def pair_fixture():
    root,rows,guide=native.fixture()
    # Registered PCM24/48k test sources; the private AAF must not guess a
    # format or probe the intentionally unmounted fixture media paths.
    for wav in root.findall('./resources/asset[@hasAudio="1"]'):
        wav.set('audioRate','48000')
        ET.SubElement(ET.SubElement(wav,'metadata'),'md',key='com.pgm.audioBitDepth',value='24')
    asset=root.find('./resources/asset[@id="alta"]')
    asset.set('src','file:///alt/MIX_L.wav');asset.set('name','MIX_L.wav');asset.set('duration','12s')
    right=copy.deepcopy(asset);right.set('id','alt-right');right.set('src','file:///alt/MIX_R.wav');right.set('name','MIX_R.wav')
    root.find('resources').append(right)
    for top in root.findall('./library/event/project/sequence/spine/mc-clip'):
        clip=next(row for row in top.findall('clip') if row.find('audio').get('ref')=='alta')
        clip.set('start',native.sec(F(clip.get('start')[:-1])+F(7,48000)))
        clip.set('name','MIX_L.wav');clip.find('audio').set('duration','12s')
        second=copy.deepcopy(clip);second.set('name','MIX_R.wav');second.set('lane','5');second.find('audio').set('ref','alt-right')
        top.append(second)
    guide=[row for row in guide if row['session_id']!='alt']+[
        dict(session_id='alt',path='/alt/MIX_L.wav',pair_id='original-pair',channel='L'),
        dict(session_id='alt',path='/alt/MIX_R.wav',pair_id='original-pair',channel='R')]
    return root,rows,guide


class NativeManualReconformTests(unittest.TestCase):
    def test_identified_native_pairs_flatten_without_losing_source_samples_or_tracks(self):
        root,rows,guide=pair_fixture()
        before=native.placements(root)
        report=standardize_connected_native_interchange(root,rows,guide)
        self.assertTrue(report['changed'])
        self.assertEqual((report['normalized_multicam_clips'],report['normalized_audio_components']),(6,9))
        sequence=root.find('./library/event/project/sequence');spine=sequence.find('spine')
        self.assertEqual(sequence.get('tcStart'),'0s')
        self.assertFalse(sequence.findall('.//clip'))
        self.assertEqual(len(spine.findall('mc-clip')),6)
        self.assertTrue(all(node.get('srcEnable')=='video' for node in spine.findall('mc-clip')))
        self.assertEqual(len(spine.findall('asset-clip')),9)
        requested=root.find('./library/event/project/metadata/md[@key="com.pgm.requestedTimelineStart"]')
        self.assertEqual(F(requested.get('value')[:-1]),3600)
        pair_audio=[node for node in spine.findall('asset-clip') if node.get('ref') in ('alta','alt-right')]
        self.assertEqual({node.get('audioRole') for node in pair_audio},{'dialogue.pgm-track-2','dialogue.pgm-track-3'})
        self.assertEqual({F(node.get('start')[:-1])%1 for node in pair_audio},{F(7,48000)})
        # Every original exact source IN/duration and requested record position
        # is retained, even where the last alternative extends beyond V1.
        after=[]
        for node in spine:
            if node.tag not in ('mc-clip','asset-clip'):continue
            after.append(('video' if node.tag=='mc-clip' else 'audio',node.get('ref'),
                          F(node.get('offset')[:-1])+3600,F(node.get('start')[:-1]),F(node.get('duration')[:-1])))
        self.assertEqual(sorted(before),sorted(after))
        for node in spine.findall('mc-clip'):
            group=node.find('./metadata/md[@key="com.pgm.linkGroupID"]').get('value')
            members=[sound for sound in spine.findall('asset-clip') if sound.find('./metadata/md[@key="com.pgm.linkGroupID"]').get('value')==group]
            self.assertEqual(len(members),2 if node.get('ref')=='alt' else 1)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'manual-raw.fcpxml';ET.ElementTree(root).write(path,encoding='utf-8')
            manifest=build_resolve_manifest(path,guide,report['audio_tracks'])
        self.assertEqual((manifest['import_start'],manifest['timeline_start'],manifest['set_start_timecode']),
                         (0,90000,'01:00:00:00'))
        self.assertEqual(len(manifest['groups']),6)
        alternate=[group for group in manifest['groups'] if group['video']['name']=='alt']
        self.assertEqual([group['video']['record_start'] for group in alternate],[90025,90100,90175])
        self.assertTrue(all(len(group['audio'])==2 for group in alternate))
        self.assertEqual({sound['source_start']%25 for group in alternate for sound in group['audio']},
                         {F(7*25,48000)})

    def test_ambiguous_same_scene_same_record_position_never_guesses_ownership(self):
        root,rows,guide=pair_fixture();top=root.find('./library/event/project/sequence/spine/mc-clip')
        duplicate=copy.deepcopy(top.find('mc-clip'));duplicate.set('lane','7');top.append(duplicate)
        before=ET.tostring(root);report=standardize_connected_native_interchange(root,rows,guide)
        self.assertFalse(report['changed'])
        self.assertIn('唯一',report['skipped'][0]['reason'])
        self.assertEqual(ET.tostring(root),before)

    def test_complex_or_unknown_sound_is_left_byte_identical(self):
        for kind in ('effect','unknown','missing-pair'):
            root,rows,guide=pair_fixture()
            if kind=='effect':root.find('./library/event/project/sequence/spine/mc-clip').insert(0,ET.Element('timeMap'))
            if kind=='unknown':guide=[row for row in guide if row.get('channel')!='R']
            if kind=='missing-pair':
                for top in root.findall('./library/event/project/sequence/spine/mc-clip'):
                    clip=next(row for row in top.findall('clip') if row.find('audio').get('ref')=='alt-right');top.remove(clip)
            before=ET.tostring(root);report=standardize_connected_native_interchange(root,rows,guide)
            with self.subTest(kind=kind):
                self.assertFalse(report['changed']);self.assertEqual(ET.tostring(root),before)

    def test_already_flat_interchange_is_not_rewritten(self):
        root,rows,guide=pair_fixture();standardize_connected_native_interchange(root,rows,guide)
        before=ET.tostring(root);report=standardize_connected_native_interchange(root,rows,guide)
        self.assertFalse(report['changed']);self.assertEqual(ET.tostring(root),before)

    def test_conform_native_file_builds_zero_clock_raw_helper_with_original_pair_sources(self):
        root,rows,guide=pair_fixture()
        index={row['proxy_path']:dict(row,proxy_start=(1000 if row['session_id']=='base' else 2000)*25,
            original_start=(1000 if row['session_id']=='base' else 2000)*25,frames=250) for row in rows}
        def probe(_path):
            return dict(fps=25,frames=250,start_tc=None,width=3840,height=2160,audio_channels=[])
        with tempfile.TemporaryDirectory() as directory,patch('core.probe',side_effect=probe):
            folder=Path(directory);output=folder/'RAW_LINKED.fcpxml'
            report=conform_fcpxml(ET.ElementTree(root),index,25,folder/'roughcut.fcpxml',output,guide_audio=guide)
            saved=ET.parse(output).getroot();manifest=build_resolve_manifest(output,guide)
            self.assertEqual(report['replaced_file_definitions'],2)
            self.assertTrue(report['audio_link_normalization']['changed'])
            self.assertEqual(saved.find('./library/event/project/sequence').get('tcStart'),'0s')
            self.assertTrue(output.with_suffix('.resolve.lua').is_file())
            self.assertEqual(len(manifest['groups']),6)
            assets={node.get('id'):node for node in saved.findall('./resources/asset')}
            for child in saved.findall('./resources/media/multicam/mc-angle/asset-clip'):
                self.assertIn('/raw/',assets[child.get('ref')].get('src'))
            self.assertEqual({sound['path'] for group in manifest['groups'] for sound in group['audio']},
                             {'/base/MIX.wav','/alt/MIX_L.wav','/alt/MIX_R.wav'})


if __name__=='__main__':unittest.main()
