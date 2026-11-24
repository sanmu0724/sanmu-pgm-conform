"""Internal AAF keeps fixed tracks and exact original-WAV sample coordinates."""
from pathlib import Path
from fractions import Fraction as F
import copy,hashlib,json,tempfile,unittest,zlib
from unittest.mock import patch
import xml.etree.ElementTree as ET
import aaf2
from resolve_audio_interchange import plan_audio,write_audio_interchange

def sec(value):value=F(value);return f'{value.numerator}/{value.denominator}s'
def fixture(folder,channels=1,residue=940,depth=24):
    path=folder/'sound.fcpxml';root=ET.Element('fcpxml',version='1.9');resources=ET.SubElement(root,'resources');source='/Volumes/Unavailable/ORIGINAL.wav';origin=3173508480+residue
    asset=ET.SubElement(resources,'asset',id='wav',src=Path(source).as_uri(),name='ORIGINAL.wav',hasAudio='1',audioChannels=str(channels),audioRate='48000',start=sec(F(origin,48000)),duration='8s');metadata=ET.SubElement(asset,'metadata')
    for key,value in {'com.pgm.audioSourceKind':'wav','com.pgm.fileUID':'ORIGINAL_UID','com.apple.proapps.studio.reel':'R017','com.pgm.recordingDate':'2026-10-18'}.items():ET.SubElement(metadata,'md',key=key,value=value)
    if depth is not None:ET.SubElement(metadata,'md',key='com.pgm.audioBitDepth',value=str(depth))
    path.write_text(ET.tostring(root,encoding='unicode'))
    def audio(track,first,last,source_in):return dict(path=source,track_index=track,record_start=F(90000)+F(first,1920),record_end=F(90000)+F(last,1920),source_start=F(source_in,1920),channels=channels,sample_rate=48000,link_group_id='group-'+str(track))
    manifest=dict(fps=F(25),timeline_start=F(90000),timeline_end=F(90200),audio_tracks=[dict(index=i,subtype='stereo',video_tracks=[i])for i in(1,2,3)],groups=[dict(video=dict(track_index=1),audio=[audio(1,7,207,7)]),dict(video=dict(track_index=3),audio=[audio(3,48007,48207,96007)])])
    return path,manifest,source,origin

def comps(file):
    comp=next(file.content.compositionmobs());return comp,{s['PhysicalTrackNumber'].value:s for s in comp.slots if s.media_kind=='Sound'}

class ResolveAudioInterchangeTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.folder=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def test_mono_exact_subframe_record_source_in_and_empty_a2_without_media_probe(self):
        path,m,source,origin=fixture(self.folder);before=copy.deepcopy(m);xml=path.read_bytes()
        with patch('sound_sync.probe_wav',side_effect=AssertionError('must not probe offline WAV')):result=write_audio_interchange(path,m,allow_probe=False)
        self.assertEqual(m,before);self.assertEqual(path.read_bytes(),xml);self.assertEqual((result['clip_count'],result['track_count'],result['source_count']),(2,3,1))
        raw=Path(result['aaf_path']).read_bytes();self.assertEqual((result['bytes'],result['adler32'],result['sha256']),(len(raw),zlib.adler32(raw),hashlib.sha256(raw).hexdigest()))
        with aaf2.open(result['aaf_path'])as file:
            comp,slots=comps(file);self.assertEqual(comp.slot_at(1).segment.start,90000)
            for slot in slots.values():self.assertEqual(slot.edit_rate,48000)
            self.assertEqual([type(c).__name__ for c in slots[2].segment.components],['Filler'])
            for track,expected_record,expected_source in[(1,7,7),(3,48007,96007)]:
                children=list(slots[track].segment.components);self.assertEqual(children[0].length,expected_record);clip=children[1];self.assertEqual((clip.start,clip.length),(expected_source,200));self.assertEqual(clip.mob.slot_at(clip.slot_id).edit_rate,48000)
            source_mob=next(file.content.sourcemobs());self.assertEqual(source_mob.comments['BWFTimeReference'],str(origin));self.assertEqual(source_mob.comments['OriginalFileUID'],'ORIGINAL_UID');self.assertEqual(source_mob.comments['com.apple.proapps.studio.reel'],'R017');self.assertEqual(source_mob.descriptor['QuantizationBits'].value,24);self.assertEqual(source_mob.descriptor['Length'].value,384000);self.assertEqual(source_mob.slot_at(1).segment.mob_id.material.int,0)
    def test_stereo_combiner_is_one_timeline_component_with_two_real_source_channels(self):
        path,m,_,_=fixture(self.folder,channels=2);result=write_audio_interchange(path,m,allow_probe=False)
        with aaf2.open(result['aaf_path'])as file:
            _,slots=comps(file);group=slots[1].segment.components[1];self.assertEqual(type(group).__name__,'OperationGroup');self.assertEqual(group.operation.name,'Audio Channel Combiner');self.assertEqual(len(group.segments),2);self.assertEqual([c.start for c in group.segments],[7,7]);self.assertEqual([c.slot_id for c in group.segments],[1,2]);self.assertEqual([c.length for c in group.segments],[200,200])
            source=next(file.content.sourcemobs());self.assertEqual(source.descriptor['Channels'].value,2);self.assertEqual([s['PhysicalTrackNumber'].value for s in source.slots],[1,2]);self.assertTrue(all(s.segment.mob_id.material.int==0 for s in source.slots))
    def test_bwf_half_frame_boundaries_remain_exact_without_quantized_tape_timecode(self):
        for residue in(940,959,960,961,1500):
            path,m,_,origin=fixture(self.folder,residue=residue);result=write_audio_interchange(path,m,allow_probe=False)
            with aaf2.open(result['aaf_path'])as file:
                source=next(file.content.sourcemobs());self.assertEqual(source.comments['BWFTimeReference'],str(origin));self.assertEqual(len(list(file.content.sourcemobs())),1);_,slots=comps(file);self.assertEqual(slots[3].segment.components[1].start,96007)
    def test_real_bit_depth_fallback_guide_and_registered_header_without_probe(self):
        path,m,source,_=fixture(self.folder,depth=None)
        with self.assertRaisesRegex(ValueError,'Missing actual'):plan_audio(path,m,allow_probe=False)
        self.assertEqual(plan_audio(path,m,[dict(path=source,audio_bit_depth=16)],allow_probe=False)['sources'][source]['depth'],16)
        sub=self.folder/'sessions/s';sub.mkdir(parents=True);(sub/'sound_sources.json').write_text(json.dumps(dict(sources=[dict(path=source,depth=24)])))
        self.assertEqual(plan_audio(path,m,allow_probe=False)['sources'][source]['depth'],24)
        with self.assertRaisesRegex(ValueError,'Conflicting'):plan_audio(path,m,[dict(path=source,audio_bit_depth=16)],allow_probe=False)
    def test_unknown_format_and_source_or_record_overlap_fail_without_output(self):
        for change in('depth','source','overlap','fraction','end'):
            path,m,_,_=fixture(self.folder)
            if change=='depth':root=ET.parse(path);root.find('.//md[@key="com.pgm.audioBitDepth"]').set('value','13');root.write(path)
            elif change=='source':m['groups'][0]['audio'][0]['source_start']=F(999999)
            elif change=='overlap':m['groups'][1]['audio'][0].update(track_index=1,record_start=m['groups'][0]['audio'][0]['record_start'])
            elif change=='fraction':m['groups'][0]['audio'][0]['source_start']=F(1,999999)
            else:m['groups'][0]['audio'][0]['record_end']=F(99999999)
            with self.assertRaises(ValueError):write_audio_interchange(path,m,allow_probe=False)
    def test_atomic_failure_keeps_previous_managed_aaf_and_cleans_temp(self):
        path,m,_,_=fixture(self.folder);result=write_audio_interchange(path,m,allow_probe=False);out=Path(result['aaf_path']);before=out.read_bytes()
        with patch('resolve_audio_interchange.os.replace',side_effect=OSError('publish failed')):
            with self.assertRaisesRegex(OSError,'publish failed'):write_audio_interchange(path,m,allow_probe=False)
        self.assertEqual(before,out.read_bytes());self.assertEqual(list(self.folder.glob('.*.tmp')),[])
    def test_unmanaged_and_symlink_outputs_never_overwrite(self):
        path,m,_,_=fixture(self.folder);out=self.folder/'external.aaf';out.write_bytes(b'user file')
        with self.assertRaisesRegex(ValueError,'同名'):write_audio_interchange(path,m,out,allow_probe=False)
        alias=self.folder/'alias.aaf';alias.symlink_to(out)
        with self.assertRaisesRegex(ValueError,'符号链接'):write_audio_interchange(path,m,alias,allow_probe=False)
        self.assertEqual(out.read_bytes(),b'user file')
    def test_riff64_sized_registered_audio_uses_64bit_length_without_reading_media(self):
        path,m,source,_=fixture(self.folder);root=ET.parse(path);count=2**33+17;root.find('./resources/asset').set('duration',sec(F(count,48000)));root.write(path)
        result=write_audio_interchange(path,m,allow_probe=False)
        with aaf2.open(result['aaf_path'])as file:self.assertEqual(next(file.content.sourcemobs()).descriptor['Length'].value,count)
    def test_partial_channel_selection_never_expands_one_selected_channel_to_stereo(self):
        path,m,_,_=fixture(self.folder,channels=2);m['groups'][0]['audio'][0]['channels']=1
        with self.assertRaisesRegex(ValueError,'部分声道'):write_audio_interchange(path,m,allow_probe=False)
        self.assertFalse(path.with_suffix('.resolve-audio.aaf').exists())
    def test_same_physical_wav_accepts_scene_notes_but_rejects_conflicting_uid_or_clock(self):
        path,m,source,_=fixture(self.folder);tree=ET.parse(path);original=tree.find('./resources/asset');other=copy.deepcopy(original);other.set('id','scene-2')
        ET.SubElement(original.find('metadata'),'md',key='com.pgm.sessionID',value='FIRST')
        ET.SubElement(other.find('metadata'),'md',key='com.pgm.sessionID',value='SECOND')
        tree.find('resources').append(other);tree.write(path)
        result=write_audio_interchange(path,m,allow_probe=False)
        with aaf2.open(result['aaf_path'])as file:
            row=next(file.content.sourcemobs());self.assertEqual(row.comments['OriginalFileUID'],'ORIGINAL_UID')
            with self.assertRaises(IndexError):row.comments['com.pgm.sessionID']
        other.find('./metadata/md[@key="com.pgm.fileUID"]').set('value','DIFFERENT_UID');tree.write(path)
        with self.assertRaisesRegex(ValueError,'UID'):plan_audio(path,m,allow_probe=False)
        other.find('./metadata/md[@key="com.pgm.fileUID"]').set('value','ORIGINAL_UID');other.set('start','10s');tree.write(path)
        with self.assertRaisesRegex(ValueError,'identity'):plan_audio(path,m,allow_probe=False)

if __name__=='__main__':unittest.main()
