"""Historical camera sound cannot revive during WAV-only raw conform."""
from copy import deepcopy
from fractions import Fraction as F
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
from fcpxml_conform import strip_camera_reference_audio,conform_fcpxml
from proxy_workflow import (normalize_guide_audio,original_wav_guide_audio,
    _strip_camera_reference_audio_xmeml,conform_edited_xml)


def stamp(v):
    v=F(v);return f'{v.numerator}/{v.denominator}s'


def md(node,key,value):
    parent=node.find('metadata')
    if parent is None:parent=ET.SubElement(node,'metadata')
    ET.SubElement(parent,'md',key=key,value=value)


class CameraReferenceAudioTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name).resolve();self.proxy=self.folder/'proxy.mov';self.raw=self.folder/'raw.mov'
        self.wav=self.folder/'original.wav';self.pgm=self.folder/'pgm.mov';self.music=self.folder/'added_music.aif'
        self.root=ET.Element('fcpxml',version='1.8');self.resources=ET.SubElement(self.root,'resources')
        ET.SubElement(self.resources,'format',id='fmt',frameDuration='1/25s',width='320',height='180')
        for identity,path,start,video in [('proxy',self.proxy,1000,True),('wav',self.wav,2000,False),('pgm',self.pgm,3000,False),('music',self.music,4000,False)]:
            asset=ET.SubElement(self.resources,'asset',id=identity,src=path.as_uri(),name=path.name,start=stamp(start),duration='10s',hasAudio='1',audioChannels='1',audioRate='48000')
            if video:asset.set('hasVideo','1');asset.set('format','fmt')
            if identity=='pgm':md(asset,'com.pgm.audioSourceKind','pgm')
            if identity in ('wav','music'):md(asset,'com.pgm.audioBitDepth','24')
            if identity=='wav':md(asset,'com.apple.proapps.studio.reel','ORIGINAL_SOUND_ROLL')
        resource=ET.SubElement(self.resources,'media',id='mc',name='MC')
        mc=ET.SubElement(resource,'multicam',format='fmt',duration='10s',tcStart='0s')
        angle=ET.SubElement(mc,'mc-angle',name='A',angleID='A')
        ET.SubElement(angle,'video',ref='proxy',offset='0s',start='1000s',duration='10s')
        project=ET.SubElement(ET.SubElement(ET.SubElement(self.root,'library'),'event'),'project',name='Roughcut')
        md(project,'com.pgm.requestedTimelineStart','3600s')
        self.seq=ET.SubElement(project,'sequence',format='fmt',duration='4s',tcStart='0s')
        self.spine=ET.SubElement(self.seq,'spine')
        picture=ET.SubElement(self.spine,'mc-clip',ref='mc',name='MC',offset='0s',start='0s',duration='4s',srcEnable='all')
        ET.SubElement(picture,'mc-source',angleID='A',srcEnable='all');ET.SubElement(picture,'mc-source',angleID='A',srcEnable='audio')
        md(picture,'com.pgm.linkGroupID','group')
        self.leaves={}
        for n,(identity,source) in enumerate([('wav',F(2001)+F(7,48000)),('pgm',3001),('proxy',1001),('music',4001)],1):
            leaf=ET.SubElement(self.spine,'asset-clip',ref=identity,name=identity,offset='0s',start=stamp(source),duration='4s',lane=str(-n),srcEnable='audio',audioRole=f'dialogue.pgm-track-{n}')
            md(leaf,'com.pgm.linkGroupID','group');self.leaves[identity]=leaf
        self.guide=[dict(session_id='session',path=str(self.wav),source_kind='wav'),dict(session_id='session',path=str(self.pgm),source_kind='pgm')]

    def test_known_camera_and_pgm_only_are_removed_wav_and_new_music_unchanged(self):
        wav=ET.tostring(self.leaves['wav']);music=ET.tostring(self.leaves['music']);timeline=dict(self.seq.attrib)
        report=strip_camera_reference_audio(self.root,[str(self.proxy)],self.guide)
        self.assertEqual(report['removed_audio_components'],2)
        self.assertEqual([n.get('ref') for n in self.spine.findall('asset-clip')],['wav','music'])
        self.assertEqual(ET.tostring(self.leaves['wav']),wav);self.assertEqual(ET.tostring(self.leaves['music']),music)
        self.assertEqual(self.seq.attrib,timeline)
        picture=self.spine.find('mc-clip');self.assertEqual(picture.get('srcEnable'),'video')
        self.assertEqual([(s.get('angleID'),s.get('srcEnable')) for s in picture.findall('mc-source')],[('A','video')])
        self.assertIsNone(self.root.find('./resources/asset[@id="pgm"]'))
        self.assertIsNone(self.root.find('./resources/asset[@id="proxy"]').get('hasAudio'))
        self.assertEqual(self.root.find('./resources/media/multicam/mc-angle/video').get('ref'),'proxy')

    def test_native_audio_wrapper_removed_and_primary_audio_span_becomes_gap(self):
        for node in list(self.spine.findall('asset-clip')):self.spine.remove(node)
        host=ET.SubElement(self.spine,'clip',offset='1s',start='3000s',duration='2s',lane='-1')
        ET.SubElement(host,'audio',ref='pgm',offset='3000s',start='3000s',duration='10s')
        leaf=ET.SubElement(self.spine,'asset-clip',ref='proxy',offset='2s',start='1001s',duration='1s',srcEnable='audio')
        strip_camera_reference_audio(self.root,[str(self.proxy)],self.guide)
        self.assertNotIn(host,list(self.spine));self.assertEqual(leaf.tag,'gap')
        self.assertEqual((leaf.get('offset'),leaf.get('start'),leaf.get('duration')),('2s','1001s','1s'))
        self.assertIsNotNone(self.spine.find('mc-clip'))

    def test_pgm_asset_still_used_for_picture_is_video_only_not_deleted(self):
        asset=self.root.find('./resources/asset[@id="pgm"]');asset.set('hasVideo','1')
        video=ET.SubElement(self.spine,'asset-clip',ref='pgm',offset='0s',start='3000s',duration='1s',lane='2')
        strip_camera_reference_audio(self.root,[str(self.proxy)],self.guide)
        self.assertIn(asset,list(self.resources));self.assertEqual(video.get('srcEnable'),'video');self.assertIsNone(asset.get('hasAudio'))

    def test_audio_angle_override_does_not_delete_default_video_picture(self):
        picture=self.spine.find('mc-clip')
        picture.remove(picture.findall('mc-source')[0])
        strip_camera_reference_audio(self.root,[str(self.proxy)],self.guide)
        self.assertIn(picture,list(self.spine));self.assertEqual(picture.get('srcEnable'),'video')
        self.assertEqual(picture.findall('mc-source'),[])

    def test_legacy_guide_is_readable_but_new_guide_is_wav_only(self):
        before=deepcopy(self.guide)
        self.assertEqual(len(normalize_guide_audio(self.guide)),2)
        self.assertEqual(original_wav_guide_audio(self.guide),self.guide[:1]);self.assertEqual(self.guide,before)
        asset=self.root.find('./resources/asset[@id="pgm"]');asset.remove(asset.find('metadata'))
        strip_camera_reference_audio(self.root,[str(self.proxy)],self.guide)
        self.assertNotIn(self.leaves['pgm'],list(self.spine))

    def test_raw_conform_does_not_restore_original_camera_scratch(self):
        source=self.folder/'edited.fcpxml';ET.ElementTree(self.root).write(source,encoding='utf-8');sha=hashlib.sha256(source.read_bytes()).hexdigest()
        index={str(self.proxy):dict(proxy_path=str(self.proxy),original_path=str(self.raw),proxy_start=25000,original_start=25000,frames=250,session_id='session')}
        expected=deepcopy(self.leaves['wav'].attrib)
        info=dict(fps=25,frames=250,start_tc=None,width=3840,height=2160,audio_channels=[dict(sample_rate=48000),dict(sample_rate=48000)])
        output=self.folder/'raw.fcpxml'
        with patch('core.probe',return_value=info):report=conform_fcpxml(ET.parse(source),index,25,source,output,guide_audio=self.guide)
        result=ET.parse(output).getroot();wave=result.find('.//asset-clip[@ref="wav"]')
        self.assertEqual(wave.attrib,expected)
        self.assertIsNone(result.find('.//asset-clip[@ref="pgm"]'));self.assertIsNone(result.find('.//asset-clip[@ref="proxy"]'))
        raw_asset=next(a for a in result.findall('./resources/asset') if a.get('src')==self.raw.as_uri())
        self.assertIsNone(raw_asset.get('hasAudio'));self.assertIsNone(raw_asset.get('audioChannels'))
        self.assertIsNotNone(result.find('.//asset-clip[@ref="music"]'))
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),sha)
        self.assertGreaterEqual(report['camera_audio_cleanup']['removed_audio_components'],2)

    def test_source_id_identifies_relocated_embedded_camera_audio(self):
        asset=self.root.find('./resources/asset[@id="proxy"]')
        moved=self.folder/'relocated.mov';asset.set('src',moved.as_uri());md(asset,'com.pgm.sourceID','immutable-camera')
        ET.SubElement(self.spine,'asset-clip',ref='proxy',offset='0s',start='1000s',duration='4s',lane='2')
        row=dict(proxy_path=str(self.proxy),original_path=str(self.raw),proxy_start=25000,original_start=25000,frames=250,session_id='session')
        info=dict(fps=25,frames=250,start_tc=None,width=3840,height=2160,audio_channels=[dict(sample_rate=48000)])
        output=self.folder/'raw.fcpxml'
        with patch('core.probe',return_value=info):
            conform_fcpxml(ET.ElementTree(self.root),{str(self.proxy):row},25,self.folder/'edited.fcpxml',output,identity_index={'immutable-camera':row},guide_audio=self.guide)
        result=ET.parse(output).getroot()
        self.assertFalse(result.findall('.//asset-clip[@ref="proxy"]'))
        self.assertIsNotNone(result.find('.//asset-clip[@ref="wav"]'))

    def xmeml(self):
        root=ET.Element('xmeml');seq=ET.SubElement(root,'sequence');rate=ET.SubElement(seq,'rate');ET.SubElement(rate,'timebase').text='25';ET.SubElement(rate,'ntsc').text='FALSE'
        media=ET.SubElement(seq,'media');vtrack=ET.SubElement(ET.SubElement(media,'video'),'track');atrack=ET.SubElement(ET.SubElement(media,'audio'),'track')
        video=ET.SubElement(vtrack,'clipitem',id='video');ET.SubElement(video,'name').text='proxy.mov';ET.SubElement(video,'file',id='proxy-file')
        for field,value in [('start','0'),('end','25'),('in','1'),('out','26')]:ET.SubElement(video,field).text=value
        for identity,path in [('scratch',self.proxy),('pgm',self.pgm),('wav',self.wav),('music',self.music)]:
            clip=ET.SubElement(atrack,'clipitem',id=identity);ET.SubElement(clip,'name').text=path.name
            file=ET.SubElement(clip,'file',id='proxy-file' if identity=='scratch' else identity+'-file');ET.SubElement(file,'pathurl').text=path.as_uri();ET.SubElement(file,'name').text=path.name
            ET.SubElement(file,'duration').text='250';ET.SubElement(ET.SubElement(file,'media'),'audio')
            source=ET.SubElement(clip,'sourcetrack');ET.SubElement(source,'mediatype').text='audio';ET.SubElement(source,'trackindex').text='1'
            for owner in (video,clip):link=ET.SubElement(owner,'link');ET.SubElement(link,'linkclipref').text=identity
        return root,video,atrack

    def test_fcp7_removes_known_sound_links_and_restores_video_file_definition(self):
        root,video,track=self.xmeml();before=deepcopy(video.attrib)
        report=_strip_camera_reference_audio_xmeml(root,[str(self.proxy)],self.guide)
        self.assertEqual(report['removed_audio_components'],2)
        self.assertEqual([n.get('id') for n in track.findall('clipitem')],['wav','music'])
        self.assertEqual(video.attrib,before);self.assertEqual(video.findtext('file/pathurl'),self.proxy.as_uri())
        self.assertEqual([n.findtext('linkclipref') for n in video.findall('link')],['wav','music'])
        self.assertIsNone(video.find('file/media/audio'))

    def test_fcp7_conform_preserves_wav_music_and_original_edit_positions(self):
        root,video,_=self.xmeml();source=self.folder/'edited.xml';ET.ElementTree(root).write(source,encoding='utf-8')
        mapping=self.folder/'map.json';mapping.write_text(json.dumps(dict(version=1,fps=25,records=[dict(proxy_path=str(self.proxy),original_path=str(self.raw),proxy_start=25000,original_start=25000,frames=250)],guide_audio=self.guide)))
        before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (source,mapping)}
        output=self.folder/'raw.xml';info=dict(fps=25,frames=250,start_tc=None,width=3840,height=2160,audio_channels=[dict(sample_rate=48000)])
        with patch('core.probe',return_value=info):conform_edited_xml(source,[mapping],output,path_mappings=[])
        result=ET.parse(output);clip=result.find('./sequence/media/video/track/clipitem')
        self.assertEqual([clip.findtext(k) for k in ('start','end','in','out')],['0','25','1','26'])
        self.assertEqual(clip.findtext('file/pathurl'),self.raw.as_uri());self.assertIsNone(clip.find('file/media/audio'))
        self.assertEqual([n.get('id') for n in result.findall('./sequence/media/audio/track/clipitem')],['wav','music'])
        for p,digest in before.items():self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(),digest)


if __name__=='__main__':unittest.main()
