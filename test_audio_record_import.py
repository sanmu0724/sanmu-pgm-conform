"""Private WAV-only frame-grid import keeps source phase and public metadata."""
from copy import deepcopy
from fractions import Fraction as F
import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
import xml.etree.ElementTree as ET
from resolve_import_helper import (_audio_record_import_patches, _audio_import_patches,
    _merge_time_patches, build_resolve_manifest, write_resolve_import_helper)


def stamp(v):
    v=F(v);return f'{v.numerator}/{v.denominator}s'


def apply(raw,patches):
    for p in reversed(patches):raw=raw[:p['first']]+p['value'].encode()+raw[p['last']:]
    return raw


class AudioRecordImportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.folder=Path(self.temp.name);self.path=self.folder/'交接.fcpxml'
        self.root=ET.Element('fcpxml',version='1.8');self.resources=ET.SubElement(self.root,'resources')
        ET.SubElement(self.resources,'format',id='format',frameDuration='1/25s')
        project=ET.SubElement(ET.SubElement(ET.SubElement(self.root,'library'),'event'),'project',name='交接测试')
        seq=ET.SubElement(project,'sequence',format='format',duration='4s',tcStart='0s')
        md=ET.SubElement(project,'metadata');ET.SubElement(md,'md',key='com.pgm.requestedTimelineStart',value='3600s')
        self.spine=ET.SubElement(seq,'spine');ET.SubElement(self.spine,'gap',name='AUDIO_ONLY',offset='0s',start='0s',duration='4s')
        self.origin=F(3173509420,48000)
        self.asset('wav','原始_MAIN.wav',self.origin,'wav');self.asset('pgm','参考_PGM.mov',68400,'pgm')
        self.first=F(1)+F(975,48000);self.last=F(3)+F(7,48000)

    def tearDown(self):self.temp.cleanup()

    def asset(self,identity,name,origin,kind='',duration=10,pair='',role=''):
        path=self.folder/name
        if path.suffix=='.wav':
            bext=bytearray(602);struct.pack_into('<Q',bext,338,3173509420)
            fmt=struct.pack('<HHIIHH',1,1,48000,96000,2,16)
            body=b'WAVE'+b'fmt '+struct.pack('<I',16)+fmt+b'bext'+struct.pack('<I',602)+bext+b'data'+struct.pack('<I',2)+b'\0\0'
            path.write_bytes(b'RIFF'+struct.pack('<I',len(body))+body)
        else:path.write_bytes(b'original PGM sentinel')
        a=ET.SubElement(self.resources,'asset',id=identity,name=name,src=path.as_uri(),start=stamp(origin),duration=stamp(duration),hasAudio='1',audioChannels='1',audioRate='48000')
        md=ET.SubElement(a,'metadata')
        for k,v in [('com.apple.proapps.studio.reel','ORIGINAL_ROLL'),('com.pgm.originalAudioFilename',name),('com.pgm.recordingDate','2026-10-18'),('com.pgm.audioSourceKind',kind),('com.pgm.audioPairID',pair),('com.pgm.audioPairChannel',role)]:
            if v:ET.SubElement(md,'md',key=k,value=v)
        return a

    def audio(self,ref,first,last,local=0,track=1,group='group'):
        a=next(a for a in self.resources.findall('asset') if a.get('id')==ref)
        n=ET.SubElement(self.spine,'asset-clip',ref=ref,name=a.get('name'),offset=stamp(first),start=stamp(F(a.get('start')[:-1])+local),duration=stamp(F(last)-first),lane=str(-track),srcEnable='audio',audioRole=f'dialogue.pgm-track-{track}')
        md=ET.SubElement(n,'metadata');ET.SubElement(md,'md',key='com.pgm.linkGroupID',value=group)
        return n

    def save(self):
        ET.indent(self.root,space='  ');raw=b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'+ET.tostring(self.root,encoding='utf-8');self.path.write_bytes(raw)
        return raw,build_resolve_manifest(self.path)

    def fixture(self):
        self.audio('pgm',0,self.first);self.audio('wav',self.first,self.last,F(1,4));self.audio('pgm',self.last,4,5)
        return self.save()

    def test_wav_only_trims_inward_keeps_phase_and_leaves_silence(self):
        raw,m=self.fixture();before=deepcopy(m);a,p,r=_audio_record_import_patches(raw,m)
        self.assertEqual(m,before);self.assertEqual(r['dropped_pgm_clip_count'],2)
        self.assertEqual(r['rounded_wav_clip_count'],1);self.assertTrue(p)
        old=m['groups'][0]['audio'][1];new=a['groups'][0]['audio'][0]
        self.assertEqual((new['record_start'],new['record_end']),(90026,90075))
        self.assertEqual(new['source_start']-new['record_start'],old['source_start']-old['record_start'])
        self.assertGreaterEqual(new['source_start'],old['source_start'])
        self.assertLessEqual(new['source_start']+new['record_end']-new['record_start'],old['source_start']+old['record_end']-old['record_start'])
        self.assertEqual(a['timeline_start'],m['timeline_start']);self.assertEqual(a['timeline_end'],m['timeline_end'])
        self.assertNotIn(str((self.folder/'参考_PGM.mov').resolve()),a['media_paths'])

    def test_lr_pair_is_trimmed_together_with_original_metadata(self):
        self.asset('L','原始_L.wav',self.origin,'wav',pair='PAIR',role='L');self.asset('R','原始_R.wav',self.origin,'wav',pair='PAIR',role='R')
        self.audio('pgm',0,self.first);self.audio('L',self.first,self.last,F(1,4),2);self.audio('R',self.first,self.last,F(1,4),3);self.audio('pgm',self.last,4,5)
        raw,m=self.save();a,_,r=_audio_record_import_patches(raw,m);left,right=a['groups'][0]['audio']
        self.assertEqual((r['rounded_wav_clip_count'],r['dropped_pgm_clip_count']),(2,2))
        for key in ('record_start','record_end','source_start','pair_id'):self.assertEqual(left[key],right[key])
        self.assertEqual((left['role'],right['role']),('L','R'));self.assertEqual((left['track_index'],right['track_index']),(2,3))

    def test_unidentified_new_music_is_not_deleted_or_rounded(self):
        self.asset('music','new_music.mov',68400)
        self.audio('music',self.first,self.last,1)
        raw,m=self.save();a,p,r=_audio_record_import_patches(raw,m)
        self.assertTrue(a.pop('strict_audio_inventory'));self.assertEqual(a,m);self.assertEqual(p,[]);self.assertEqual(r['dropped_pgm_clip_count'],0)

    def test_wav_is_frame_safe_without_shared_group_or_pgm(self):
        self.audio('wav',self.first,self.last,1,group='only')
        raw,m=self.save();a,_,r=_audio_record_import_patches(raw,m)
        self.assertEqual(r['rounded_wav_clip_count'],1)
        self.assertEqual((a['groups'][0]['audio'][0]['record_start'],a['groups'][0]['audio'][0]['record_end']),(90026,90075))

    def test_public_xml_wav_header_and_metadata_are_unchanged(self):
        raw,m=self.fixture();files={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.folder.iterdir()}
        self.assertTrue(write_resolve_import_helper(self.path).exists());self.assertEqual(self.path.read_bytes(),raw)
        for p,digest in files.items():self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(),digest)
        clock=_audio_import_patches(raw,m);_,record,_=_audio_record_import_patches(raw,m)
        private=ET.fromstring(apply(raw,_merge_time_patches(raw,clock['patches'],record)))
        original=next(a for a in self.resources.findall('asset') if a.get('id')=='wav')
        kept=private.find('./resources/asset[@id="wav"]');original=deepcopy(original);kept.tail=original.tail=None
        self.assertEqual(ET.tostring(kept),ET.tostring(original))
        self.assertIsNone(private.find('./resources/asset[@id="pgm"]'))

    def test_clock_and_record_start_patches_add_not_overwrite(self):
        raw,m=self.fixture();a,record,_=_audio_record_import_patches(raw,m);clock=_audio_import_patches(raw,m)
        root=ET.fromstring(apply(raw,_merge_time_patches(raw,clock['patches'],record)))
        node=root.find('./library/event/project/sequence/spine/asset-clip');row=a['groups'][0]['audio'][0]
        floor=F((self.origin*25).numerator//(self.origin*25).denominator,25)
        self.assertEqual((F(node.get('start')[:-1])-floor)*25,row['source_start'])
        self.assertEqual(F(node.get('offset')[:-1])*25+90000,row['record_start'])
        self.assertEqual(F(node.get('duration')[:-1])*25,row['record_end']-row['record_start'])

    def test_sub_two_frame_wav_without_a_full_frame_is_private_silence(self):
        for first,last in [(0,F(7,48000)),(self.first,self.first+F(100,48000))]:
            with self.subTest(first=first):
                for n in list(self.spine)[1:]:self.spine.remove(n)
                self.audio('wav',first,last,F(1,4));raw,m=self.save()
                a,record,r=_audio_record_import_patches(raw,m)
                self.assertEqual(a['groups'],[]);self.assertEqual(r['dropped_subframe_wav_count'],1)
                root=ET.fromstring(apply(raw,_merge_time_patches(raw,_audio_import_patches(raw,m)['patches'],record)))
                self.assertEqual(root.findall('.//asset-clip'),[]);self.assertEqual(self.path.read_bytes(),raw)

    def test_trimmed_wav_source_range_is_checked(self):
        next(a for a in self.resources.findall('asset') if a.get('id')=='wav').set('duration','1s')
        self.audio('wav',self.first,self.last,F(1,4));raw,m=self.save()
        with self.assertRaisesRegex(ValueError,'超出原始 WAV'):_audio_record_import_patches(raw,m)

    def test_delete_contained_patch_but_reject_partial_overlap(self):
        raw=b'0123456789';delete=[dict(first=3,last=7,value='')]
        self.assertEqual(_merge_time_patches(raw,[dict(first=4,last=6,value='1s')],delete),delete)
        with self.assertRaisesRegex(ValueError,'补丁重叠'):_merge_time_patches(raw,delete,[dict(first=5,last=9,value='1s')])

    def test_unrouted_manifest_not_adapted(self):
        raw,m=self.fixture();m['routing_enabled']=False;a,p,r=_audio_record_import_patches(raw,m)
        self.assertEqual(a,m);self.assertIsNot(a,m);self.assertEqual(p,[])


if __name__=='__main__':unittest.main()
