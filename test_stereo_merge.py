"""Exact PCM/channel, BWF identity, RF64 and atomic split-mono merge tests."""
from pathlib import Path
from fractions import Fraction as F
import copy,hashlib,json,os,struct,tempfile,unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
from core import AnalysisError
from sound_sync import probe_wav
from sound_fallback import AudioSources
from stereo_merge import merge_stereo_pair,merge_sound_plan,read_wave_layout,_wave_header

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def make(path,values,depth=24,start=3173509420,tape='ROLL17',scene='4',date='2026-10-18',file_uid=None,rf64=False,ixml_extra=''):
    width=depth//8;raw=b''.join(int(v).to_bytes(width,'little',signed=True)for v in values)
    bext=bytearray(602);bext[256:262]=b'REAPER';bext[288:320]=b'X'*32;bext[320:330]=date.encode();bext[330:338]=b'18:57:39';bext[338:346]=struct.pack('<Q',start);bext[346:348]=struct.pack('<H',1);bext[348:412]=b'\x01'*64
    xml=f'<BWFXML><TAPE>{tape}</TAPE><SCENE>{scene}</SCENE><TAKE>2</TAKE><FILE_UID>{file_uid or path.stem}</FILE_UID><FILE_SET><FAMILY_UID>FAMILY</FAMILY_UID><FILE_SET_INDEX>1</FILE_SET_INDEX></FILE_SET>{ixml_extra}</BWFXML>'.encode()
    chunks=[(b'fmt ',struct.pack('<HHIIHH',1,1,48000,48000*width,width,depth)),(b'bext',bytes(bext)),(b'iXML',xml)]
    path.write_bytes(_wave_header(chunks,len(raw),len(values),rf64)+raw+(b'\0'if len(raw)%2 else b''));return path

def pcm(path):
    with path.open('rb')as source:
        data=next((o,n)for k,o,n in read_wave_layout(source,path.stat().st_size)if k==b'data');source.seek(data[0]);return source.read(data[1])

class StereoMergeTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name);self.left=make(self.path/'LEFT.wav',[-8388608,-1,0,1,8388607]*53,file_uid='LEFT_UID');self.right=make(self.path/'RIGHT.wav',[5,4,3,2,1]*53,file_uid='RIGHT_UID');self.cache=self.path/'cache'
    def tearDown(self):self.temp.cleanup()
    def merge(self,**kw):return merge_stereo_pair(self.left,self.right,self.cache,**kw)
    def test_pcm24_exact_left_right_no_gain_and_metadata_original_sha(self):
        before=[sha(p)for p in(self.left,self.right)];info=self.merge();out=Path(info['path']);raw=pcm(out);a,b=pcm(self.left),pcm(self.right)
        self.assertEqual(raw,b''.join(a[i:i+3]+b[i:i+3]for i in range(0,len(a),3)))
        self.assertEqual((info['channels'],info['sample_rate'],info['depth'],info['samples'],info['start']),(2,48000,24,265,3173509420))
        self.assertEqual((info['sound_roll'],info['scene'],info['take'],info['recording_date']),('ROLL17','4','2','2026-10-18'))
        self.assertNotIn(info['file_uid'],('LEFT_UID','RIGHT_UID'));self.assertFalse(info['umid']);self.assertFalse(info['file_set_index'])
        self.assertEqual(before,[sha(p)for p in(self.left,self.right)]);side=json.loads(Path(info['stereo_merge_sidecar']).read_text());self.assertEqual([r['file_uid']for r in side['originals']],['LEFT_UID','RIGHT_UID']);self.assertEqual([r['sha256']for r in side['originals']],before)
    def test_pcm16_and_forced_rf64_round_trip(self):
        make(self.left,[-32768,-1,0,1,32767],depth=16,rf64=True);make(self.right,[20,15,10,5,0],depth=16,rf64=True)
        info=self.merge(force_rf64=True);self.assertEqual(Path(info['path']).read_bytes()[:4],b'RF64');self.assertEqual(info['samples'],5);self.assertEqual(len(pcm(Path(info['path']))),20)
    def test_rf64_beyond_four_gb_header_sparse_probe_no_pcm_allocation(self):
        frames=2**30+17;size=frames*6;path=self.path/'large.wav';fmt=struct.pack('<HHIIHH',1,2,48000,288000,6,24);header=_wave_header([(b'fmt ',fmt)],size,frames)
        self.assertEqual(header[:4],b'RF64')
        with path.open('wb')as out:out.write(header);out.seek(len(header)+size-1);out.write(b'\0')
        info=probe_wav(path);self.assertEqual(info['samples'],frames);self.assertEqual(info['channels'],2)
    def test_reuses_cache_without_pcm_reread(self):
        first=self.merge()
        with patch('stereo_merge._sha',side_effect=AssertionError('cache reread')):second=self.merge()
        self.assertEqual(first['path'],second['path']);self.assertEqual(len(list(self.cache.glob('*/stereo_merge.json'))),1)
    def test_conflicting_timecode_rate_depth_length_and_roll_reject(self):
        for kwargs in(dict(start=3173509421),dict(depth=16),dict(tape='OTHER'),dict(scene='OTHER'),dict(date='2026-10-19')):
            make(self.right,[5,4,3,2,1]*53,**kwargs)
            with self.assertRaises(AnalysisError):self.merge()
        make(self.right,[1,2,3])
        with self.assertRaises(AnalysisError):self.merge()
    def test_missing_shared_values_not_invented_and_different_description_not_copied(self):
        make(self.right,[5,4,3,2,1]*53,tape='')
        info=self.merge();self.assertEqual(info['sound_roll'],'');self.assertEqual(info['scene'],'4')
    def test_cancel_during_pcm_has_no_published_partial_and_originals_unchanged(self):
        before=[sha(p)for p in(self.left,self.right)];state={'cancel':False}
        def progress(n,m):state['cancel']=True
        with self.assertRaises(AnalysisError):self.merge(cancelled=lambda:state['cancel'],progress=progress)
        self.assertEqual(before,[sha(p)for p in(self.left,self.right)]);self.assertFalse(list(self.cache.glob('*/stereo_merge.json')));self.assertFalse(list(self.cache.glob('.stereo-*')))
    def test_sound_plan_one_stereo_event_exact_sample_start_and_no_mutation(self):
        heads=[probe_wav(p)for p in(self.left,self.right)];events=[]
        for role,head in zip(('L','R'),heads):head.update(pair_id='pair',channel=role);events.append(dict(path=head['path'],pair_id='pair',channel=role,out_channel=role,source_start=F(3173509427,48000),duration=F(200,48000),record_start=F(7,48000),pgm_path='/pgm.mov',src_channel=1))
        sound=AudioSources(heads),events;before=copy.deepcopy(sound);wavs,rows=merge_sound_plan(sound,self.cache)
        self.assertEqual(sound,before);self.assertEqual(len(wavs),1);self.assertEqual(len(rows),1);self.assertEqual(rows[0]['source_start'],F(3173509427,48000));self.assertEqual(rows[0]['record_start'],F(7,48000));self.assertEqual(rows[0]['duration'],F(200,48000));self.assertEqual(rows[0]['src_channel'],'1,2');self.assertNotIn('pair_id',rows[0]);self.assertNotIn('pair_id',wavs[0])
    def test_mismatched_pair_placement_not_silently_panned_or_dropped(self):
        heads=[probe_wav(p)for p in(self.left,self.right)];events=[dict(path=h['path'],pair_id='pair',channel=c,source_start=F(3173509420+i,48000),duration=F(100,48000),record_start=F(0))for i,(h,c)in enumerate(zip(heads,('L','R')))]
        with self.assertRaises(AnalysisError):merge_sound_plan((heads,events),self.cache)
    def test_nonpaired_stereo_or_mono_passthrough_no_file_read(self):
        source=dict(path='/unmounted/main.wav',channels=1,source_kind='wav');event=dict(path=source['path'],record_start=F(0),source_start=F(0),duration=F(1))
        with patch('stereo_merge.merge_stereo_pair',side_effect=AssertionError('not a pair')):out=merge_sound_plan(([source],[event]),self.cache)
        self.assertEqual(out,([source],[event]))
    def test_write_failure_and_cache_tamper_never_overwrite_originals(self):
        before=[sha(p)for p in(self.left,self.right)]
        with patch('stereo_merge.os.fsync',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.merge()
        self.assertFalse(list(self.cache.glob('.stereo-*')));self.assertFalse(list(self.cache.glob('*/stereo_merge.json')))
        info=self.merge();path=Path(info['path'])
        with path.open('r+b')as wav:wav.seek(-1,2);wav.write(b'\xff')
        with self.assertRaises(AnalysisError):self.merge()
        self.assertEqual(before,[sha(p)for p in(self.left,self.right)])
    def test_core_export_emits_one_stereo_wav_with_exact_sample_source_in(self):
        from core import export,Segment
        headers=[probe_wav(p)for p in(self.left,self.right)];events=[]
        for role,h in zip(('L','R'),headers):
            h.update(pair_id='pair',channel=role)
            events.append(dict(path=h['path'],pair_id='pair',channel=role,out_channel=role,source_kind='wav',record_start=F(0),source_start=F(h['start']+7,48000),duration=F(200,48000)))
        sound=(AudioSources(headers),events);config=dict(fps=25,camera_ids=['A'],multicam=True,external_sound=True,library_sound_cache_only=True,calibration_confirmed=True,stereo_audio_cache_dir=str(self.cache),library_context=dict(session_id='SESSION',production_id='PRODUCTION'))
        cut=dict(fps=25,duration=1,pgm_sources=[dict(path='/pgm.mov',start=0,end=1,frames=1,start_tc='00:00:40:00')]);sources=[dict(camera='A',path='/proxy.mov',start=1000,end=1001,frames=1,reel='A',width=1920,height=1080,audio_channels=[])]
        with patch('core.build_sources',return_value=sources),patch('cut_xml.timeline_info',return_value=cut),patch('registered_sound.registered_sound_plan',return_value=sound):_,xml=export(config,[Segment(0,1,'A','manual')],self.path/'export')
        root=ET.parse(xml).getroot();audio=root.findall('.//audio');self.assertEqual(len(audio),1);self.assertEqual(F(audio[0].get('start')[:-1]),F(3173509427,48000));self.assertEqual(audio[0].get('srcCh').replace(' ',''),'1,2')
        wave=next(a for a in root.findall('./resources/asset')if a.get('hasAudio')=='1');self.assertEqual(wave.get('audioChannels'),'2');self.assertEqual(F(wave.get('start')[:-1]),F(3173509420,48000))
        mapping=json.loads((xml.parent/'proxy_original_map.json').read_text());self.assertEqual(len(mapping['guide_audio']),1);self.assertNotIn('pair_id',mapping['guide_audio'][0]);self.assertEqual(config.get('timeline_start_tc'),None)
        self.assertEqual(mapping['required_media_directories'],[str(self.cache.resolve())])
        self.assertEqual([row['file_uid'] for row in mapping['merged_audio_sources'][0]['merged_from']],['LEFT_UID','RIGHT_UID'])

if __name__=='__main__':unittest.main()
