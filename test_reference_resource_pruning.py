"""Only resource dependencies reachable from selected edits are imported."""
import copy
from fractions import Fraction as F
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from reference_timeline import assemble_reference_xml,_prune_unused_resources


def fixture(folder,name):
    root=ET.Element('fcpxml',version='1.8');resources=ET.SubElement(root,'resources')
    for identity in ('timeline','picture-0','picture-1','wave-0','wave-1','unused'):
        ET.SubElement(resources,'format',id=identity,frameDuration='1/25s',width='1920',height='1080')
    project=ET.SubElement(ET.SubElement(ET.SubElement(root,'library'),'event'),'project',name=name)
    seq=ET.SubElement(project,'sequence',format='timeline',tcStart='3600s',duration='10s',audioLayout='stereo',audioRate='48k')
    spine=ET.SubElement(seq,'spine')
    for index in range(2):
        video=ET.SubElement(resources,'asset',id=f'video-{index}',name=f'{name}_DAILY_{index}.mov',src=(folder/f'{name}_DAILY_{index}.mov').as_uri(),start=f'{7200+index*60}s',duration='5s',hasVideo='1',format=f'picture-{index}')
        metadata=ET.SubElement(video,'metadata');ET.SubElement(metadata,'md',key='com.pgm.sourceID',value=f'{name}-{index}')
        wave=ET.SubElement(resources,'asset',id=f'audio-{index}',name=f'{name}_MAIN_{index}.wav',src=(folder/f'{name}_MAIN_{index}.wav').as_uri(),start=f'{300+index*10}s',duration='10s',hasAudio='1',audioChannels='2',audioRate='48000',format=f'wave-{index}')
        metadata=ET.SubElement(wave,'metadata')
        ET.SubElement(metadata,'md',key='com.pgm.audioSourceKind',value='wav')
        # The metadata-only WAV fixture represents known 48 kHz / 24-bit PCM.
        ET.SubElement(metadata,'md',key='com.pgm.audioBitDepth',value='24')
        media=ET.SubElement(resources,'media',id=f'mc-{index}',name=f'{name}_PGM_{index}')
        mc=ET.SubElement(media,'multicam',format='timeline',tcStart='0s',duration='5s');angle=ET.SubElement(mc,'mc-angle',name='A',angleID=f'angle-{index}')
        host=ET.SubElement(angle,'clip',offset='0s',start=f'{7200+index*60}s',duration='5s',format='timeline')
        ET.SubElement(host,'video',ref=f'video-{index}',offset=f'{7200+index*60}s',start=f'{7200+index*60}s',duration='5s')
        wrapper=ET.SubElement(spine,'clip',name=f'{name}_{index}',offset=f'{3600+index*5}s',start='0s',duration='5s',format='timeline')
        clip=ET.SubElement(wrapper,'mc-clip',ref=f'mc-{index}',name=f'{name}_PGM_{index}',offset='0s',start='0s',duration='5s',srcEnable='video')
        ET.SubElement(clip,'mc-source',angleID=f'angle-{index}',srcEnable='video')
        ET.SubElement(wrapper,'audio',ref=f'audio-{index}',offset='0s',start=f'{(300+index*10)*48000+7}/48000s',duration='5s',srcCh='1, 2',outCh='L,R',role='dialogue')
    ET.SubElement(resources,'asset',id='dead-video',name='UNSELECTED.mov',src=(folder/'UNSELECTED.mov').as_uri(),format='unused',hasVideo='1',start='0s',duration='5s')
    path=folder/(name+'.fcpxml');ET.ElementTree(root).write(path,encoding='utf-8',xml_declaration=True)
    return path


class ReferenceResourcePruningTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.folder=Path(self.temp.name)
        self.base=fixture(self.folder,'base');self.alt=fixture(self.folder,'alt')
        self.plan=dict(manual_layout=True,resolve_zero_origin=True,fps=25,baseline_session_id='base',baseline_duration_frames=250,timeline_start_frame=0,timeline_end_frame=250,duration_frames=250,lane_count=1,selections=[dict(candidate_id='alt-first',session_id='alt',record_frame_in=0,record_frame_out_exclusive=125,target_frame_in=25,target_frame_out_exclusive=150,duration_frames=125,lane=1)])

    def assemble(self,include=True,selections=True):
        plan=copy.deepcopy(self.plan)
        if not selections:plan['selections']=[]
        output=self.folder/'OUTPUT.fcpxml'
        result=assemble_reference_xml(self.base,{'alt':self.alt},plan,output,include_baseline=include)
        return ET.parse(output).getroot(),result

    def assert_references_valid(self,root,result):
        resources={node.get('id'):node for node in root.find('resources')}
        self.assertTrue(all(node.get(key) in resources for node in root.iter() for key in ('ref','format') if node.get(key)))
        stored=json.loads(Path(result['manifest_path']).read_text())
        self.assertEqual({row['resource_id'] for row in stored['assets']},{identity for identity,node in resources.items() if node.tag=='asset'})
        self.assertTrue(all(ref in resources for row in stored['pieces'] for ref in row['source_refs']))
        from resolve_import_helper import build_resolve_manifest
        data=build_resolve_manifest(result['xml_path'],audio_layout=stored['audio_tracks'])
        self.assertIsNotNone(data)
        self.assertFalse(any('alt_DAILY_1' in path for path in data['media_paths']))
        self.assertTrue(Path(result['xml_path']).with_suffix('.resolve.lua').is_file())

    def test_combined_imports_only_selected_pgm_and_its_transitive_asset_formats(self):
        before=self.base.read_bytes(),self.alt.read_bytes();root,result=self.assemble()
        ids={node.get('id') for node in root.find('resources')}
        self.assertTrue({'base-mc-0','base-mc-1','alt1-mc-0','alt1-video-0','alt1-audio-0','alt1-picture-0','alt1-wave-0'}<=ids)
        self.assertFalse({'alt1-mc-1','alt1-video-1','alt1-audio-1','alt1-picture-1','alt1-wave-1','base-unused','alt1-unused','base-dead-video','alt1-dead-video'} & ids)
        self.assert_references_valid(root,result);self.assertEqual((self.base.read_bytes(),self.alt.read_bytes()),before)
        asset=root.find('./resources/asset[@id="alt1-video-0"]');self.assertEqual((asset.get('start'),asset.get('duration')),('7200s','5s'))
        audio=root.find('./library/event/project/sequence/spine/asset-clip[@ref="alt1-audio-0"]');self.assertEqual(F(audio.get('start')[:-1]),F(300)+F(7,48000))
        self.assertEqual(F(audio.get('offset')[:-1]),F(1));self.assertEqual(audio.get('duration'),'5/1s')

    def test_baseline_only_prunes_its_own_dead_resources(self):
        root,result=self.assemble(selections=False)
        self.assertFalse(any(node.get('id','').startswith('alt') for node in root.find('resources')))
        self.assertIsNone(root.find('./resources/asset[@id="base-dead-video"]'))
        self.assertEqual(len(root.findall('./resources/media')),2);self.assert_references_valid(root,result)

    def test_alternatives_only_keeps_sequence_format_and_wave_asset_format(self):
        root,result=self.assemble(include=False);ids={node.get('id') for node in root.find('resources')}
        self.assertIn('base-timeline',ids);self.assertIn('alt1-wave-0',ids);self.assertIn('alt1-picture-0',ids)
        self.assertFalse(any(identity.startswith('base-') and identity!='base-timeline' for identity in ids))
        self.assertEqual(len(root.findall('./resources/media')),1);self.assert_references_valid(root,result)

    def test_removed_legacy_pgm_audio_does_not_leave_dangling_piece_sidecar_refs(self):
        tree=ET.parse(self.alt);root=tree.getroot()
        asset=ET.SubElement(root.find('resources'),'asset',id='old-pgm',src=(self.folder/'PGM.mov').as_uri(),start='0s',duration='5s',hasAudio='1',audioChannels='2')
        ET.SubElement(ET.SubElement(asset,'metadata'),'md',key='com.pgm.audioSourceKind',value='pgm')
        ET.SubElement(root.find('./library/event/project/sequence/spine/clip'),'audio',ref='old-pgm',offset='0s',start='0s',duration='5s')
        tree.write(self.alt,encoding='utf-8',xml_declaration=True)
        output,result=self.assemble();self.assert_references_valid(output,result)
        self.assertFalse(any(ref.endswith('old-pgm') for row in result['manifest']['pieces'] for ref in row['source_refs']))

    def test_recursive_nested_media_reference_and_format_dependency_are_retained(self):
        root=ET.fromstring('<fcpxml><resources><format id="f"/><format id="a"/><format id="dead"/><asset id="asset" format="a"/><media id="inner"><sequence format="f"><spine><asset-clip ref="asset"/></spine></sequence></media><media id="outer"><multicam format="f"><mc-angle><ref-clip ref="inner"/></mc-angle></multicam></media><media id="orphan"/></resources><project><sequence format="f"><spine><mc-clip ref="outer"/></spine></sequence></project></fcpxml>')
        before={node.get('id'):ET.tostring(node) for node in root.find('resources')}
        retained,report=_prune_unused_resources(root,root.find('project'))
        self.assertEqual(retained,{'f','a','asset','inner','outer'});self.assertEqual(report['removed'],{'format':1,'media':1})
        for node in root.find('resources'):self.assertEqual(ET.tostring(node),before[node.get('id')])


if __name__=='__main__':unittest.main()
