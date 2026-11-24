"""Explicit PGM picture replacements stay silent and survive video reconform."""
import copy
from fractions import Fraction as F
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
from proxy_workflow import conform_edited_xml,export_mapping
from reference_workflow import ReferenceWorkflow
from reference_timeline import assemble_reference_xml
from test_reference_resource_pruning import fixture

class PgmPictureFallbackTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.folder=Path(self.tmp.name).resolve()
  self.pgm=self.folder/'PGM.mov';self.wav=self.folder/'MAIN.wav'
 def mapping(self,records=(),guide=(),fallback=True):
  p=self.folder/'map.json';p.write_text(json.dumps(dict(version=1,fps=25,records=list(records),guide_audio=list(guide),video_fallbacks=[dict(session_id='scene',path=str(self.pgm),source_kind='pgm_fallback')] if fallback else [])))
  return p
 def pure_fcpxml(self,metadata=False,linked_wave=True):
  r=ET.fromstring(f'''<fcpxml version="1.8"><resources><format id="f" frameDuration="1/25s" width="1920" height="1080"/><asset id="pgm" src="{self.pgm.as_uri()}" name="PGM.mov" format="f" start="7200s" duration="5s" hasVideo="1" hasAudio="1" audioChannels="2"/><asset id="wav" src="{self.wav.as_uri()}" start="300s" duration="5s" hasAudio="1" audioChannels="1" audioRate="48000"/><media id="mc" name="PGM_REF"><multicam format="f" tcStart="0s" duration="5s"><mc-angle name="PGM_REF" angleID="pgm-ref"><clip offset="0s" start="7200s" duration="5s" format="f"><video ref="pgm" offset="7200s" start="7200s" duration="5s"/><audio ref="pgm" offset="7200s" start="7200s" duration="5s"/></clip></mc-angle></multicam></media></resources><library><event><project name="PGM-only"><sequence format="f" duration="5s" tcStart="3600s"><spine><clip offset="3600s" start="0s" duration="5s"><mc-clip ref="mc" name="PGM_REF" offset="0s" start="0s" duration="5s" srcEnable="video"><mc-source angleID="pgm-ref" srcEnable="video"/></mc-clip></clip></spine></sequence></project></event></library></fcpxml>''')
  if linked_wave:ET.SubElement(r.find('./library/event/project/sequence/spine/clip'),'audio',ref='wav',offset='0s',start='14400007/48000s',duration='5s',srcCh='1')
  if metadata:ET.SubElement(ET.SubElement(r.find('./resources/asset[@id="pgm"]'),'metadata'),'md',key='com.pgm.videoSourceKind',value='pgm_fallback')
  p=self.folder/'edited.fcpxml';ET.ElementTree(r).write(p,encoding='utf-8',xml_declaration=True);return p
 def test_map_recovers_identity_after_native_metadata_loss_and_only_pgm_edit_needs_no_raw(self):
  edited=self.pure_fcpxml();before=edited.read_bytes();out=self.folder/'raw.fcpxml'
  guide=[dict(session_id='scene',path=str(self.wav),source_kind='wav')]
  with patch('core.probe',side_effect=AssertionError('PGM must not be probed as a raw proxy')):
   report=conform_edited_xml(edited,[self.mapping(guide=guide)],out)
  r=ET.parse(out).getroot();pgm=r.find('./resources/asset[@id="pgm"]')
  self.assertEqual((pgm.get('src'),pgm.get('start'),pgm.get('duration')),(self.pgm.as_uri(),'7200s','5s'))
  self.assertIsNone(pgm.get('hasAudio'));self.assertFalse(list(r.iter('audio')))
  self.assertEqual(report['replaced_file_definitions'],0);self.assertNotIn(str(self.pgm),report['unmapped_retained'])
  self.assertEqual(report['pgm_video_fallbacks_retained'],[str(self.pgm)]);self.assertEqual(report['pgm_fallback_wavs_removed'],1)
  self.assertEqual(edited.read_bytes(),before)
 def test_explicit_asset_metadata_works_with_legacy_map_and_keeps_added_music(self):
  edited=self.pure_fcpxml(metadata=True,linked_wave=False);tree=ET.parse(edited);r=tree.getroot()
  ET.SubElement(r.find('resources'),'asset',id='music',src=(self.folder/'score.aif').as_uri(),hasAudio='1',audioChannels='2',start='0s',duration='5s')
  ET.SubElement(r.find('./library/event/project/sequence/spine'),'asset-clip',ref='music',offset='3600s',start='0s',duration='5s',lane='-3',srcEnable='audio')
  tree.write(edited,encoding='utf-8',xml_declaration=True);out=self.folder/'raw.fcpxml'
  report=conform_edited_xml(edited,[self.mapping(fallback=False)],out);r=ET.parse(out).getroot()
  self.assertIsNotNone(r.find('.//asset-clip[@ref="music"]'));self.assertEqual(report['pgm_video_fallbacks_retained'],[str(self.pgm)])
 def test_partial_owned_wav_is_cut_only_over_pgm_fallback_and_keeps_sample_in(self):
  p=self.pure_fcpxml(metadata=True);tree=ET.parse(p);r=tree.getroot();mc=r.find('./library/event/project/sequence/spine/clip/mc-clip')
  mc.set('offset','1s');mc.set('duration','2s');tree.write(p,encoding='utf-8',xml_declaration=True)
  out=self.folder/'raw.fcpxml';conform_edited_xml(p,[self.mapping(guide=[dict(session_id='scene',path=str(self.wav),source_kind='wav')])],out)
  rows=ET.parse(out).findall('.//audio[@ref="wav"]')
  self.assertEqual([(F(row.get('offset')[:-1]),F(row.get('duration')[:-1]),F(row.get('start')[:-1])) for row in rows],[(F(0),F(1),F(300)+F(7,48000)),(F(3),F(2),F(303)+F(7,48000))])
 def test_map_export_and_cross_scene_merge_keep_video_identity_separate(self):
  rows=[dict(session_id='scene',path=str(self.pgm),production_id='show',source_kind='pgm_fallback')]
  path=export_mapping([],self.folder,25,video_fallbacks=rows+rows);data=json.loads(path.read_text())
  self.assertEqual(data['records'],[]);self.assertEqual(data['guide_audio'],[]);self.assertEqual(data['video_fallbacks'],rows)
  out=self.folder/'merged.json';ReferenceWorkflow._merge_maps([path,path],out)
  self.assertEqual(json.loads(out.read_text())['video_fallbacks'],rows)
 def test_readiness_allows_true_unrecorded_region_under_pgm_picture_policy(self):
  from workflow_readiness import _no_recorded_camera
  config=dict(fps=25,missing_camera_policy='available',empty_camera_policy='pgm',sources=[dict(camera='A',frames=25,start_tc='00:01:00:00')])
  cut=dict(pgm_sources=[dict(path=str(self.pgm),frames=125,start_tc='00:00:00:00')])
  predicate=_no_recorded_camera(config,cut,['A']);self.assertTrue(predicate(0,125))
  config['sources'][0]['start_tc']='00:00:00:00'
  self.assertFalse(_no_recorded_camera(config,cut,['A'])(0,25))
 def test_reference_assembly_defensively_removes_even_covered_wav_for_pgm_picture(self):
  base=fixture(self.folder,'base');alt=fixture(self.folder,'alt');tree=ET.parse(alt);r=tree.getroot()
  mc=r.find('./library/event/project/sequence/spine/clip/mc-clip')
  ET.SubElement(ET.SubElement(mc,'metadata'),'md',key='com.pgm.videoSourceKind',value='pgm_fallback')
  ET.SubElement(mc,'marker',start='0s',duration='5s',value='PGM_VIDEO_FALLBACK')
  tree.write(alt,encoding='utf-8',xml_declaration=True)
  plan=dict(manual_layout=True,resolve_zero_origin=True,fps=25,baseline_session_id='base',baseline_duration_frames=250,timeline_start_frame=0,timeline_end_frame=250,duration_frames=250,lane_count=1,selections=[dict(candidate_id='alt',session_id='alt',record_frame_in=0,record_frame_out_exclusive=125,target_frame_in=0,target_frame_out_exclusive=125,duration_frames=125,lane=1)])
  out=self.folder/'combined.fcpxml';report=assemble_reference_xml(base,{'alt':alt},plan,out);r=ET.parse(out).getroot()
  self.assertFalse(r.findall('./resources/asset[@id="alt1-audio-0"]'));self.assertTrue(r.findall('./resources/asset[@id="base-audio-0"]'))
  self.assertEqual(report['manifest']['audio_policy']['removed_pgm_fallback_wavs'],1)
 def test_fcp7_retains_pgm_video_and_trims_only_its_explicitly_linked_wav(self):
  r=ET.fromstring(f'''<xmeml version="5"><sequence><rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate><media><video><track><clipitem id="v"><start>25</start><end>75</end><in>0</in><out>50</out><file id="pgm"><name>PGM.mov</name><pathurl>{self.pgm.as_uri()}</pathurl><duration>100</duration><media><video/><audio/></media></file><link><linkclipref>a</linkclipref></link><sourcetrack><mediatype>video</mediatype></sourcetrack></clipitem></track></video><audio><track><clipitem id="a"><start>0</start><end>100</end><in>13</in><out>113</out><file id="wav"><pathurl>{self.wav.as_uri()}</pathurl><duration>200</duration><media><audio/></media></file><link><linkclipref>v</linkclipref></link><sourcetrack><mediatype>audio</mediatype></sourcetrack></clipitem></track></audio></media></sequence></xmeml>''')
  p=self.folder/'edited.xml';ET.ElementTree(r).write(p,encoding='utf-8',xml_declaration=True);before=p.read_bytes();out=self.folder/'raw.xml'
  with patch('core.probe',side_effect=AssertionError('no raw proxy required')):
   report=conform_edited_xml(p,[self.mapping(guide=[dict(session_id='scene',path=str(self.wav),source_kind='wav')])],out)
  r=ET.parse(out).getroot();video=r.find('.//media/video/track/clipitem');self.assertEqual(video.findtext('file/pathurl'),self.pgm.as_uri());self.assertIsNone(video.find('file/media/audio'))
  self.assertEqual([tuple(int(row.findtext(k)) for k in ('start','end','in','out')) for row in r.findall('.//media/audio/track/clipitem')],[(0,25,13,38),(75,100,88,113)])
  self.assertEqual(report['pgm_fallback_wavs_removed'],1);self.assertEqual(report['pgm_video_fallbacks_retained'],[str(self.pgm)]);self.assertEqual(p.read_bytes(),before)

if __name__=='__main__':unittest.main()
