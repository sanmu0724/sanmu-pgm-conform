"""WAV-only output keeps source samples and never revives scratch sound."""
import copy
from fractions import Fraction as F
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
from sound_fallback import AudioSources,sound_reports,wav_only_sound_plan,make_pgm_fallback,valid_pgm_fallback_signature
from reference_timeline import assemble_reference_xml
from test_manual_audio_routing import write_session,selection

class WavOnlyPlanTests(unittest.TestCase):
    def test_old_fallback_removed_without_mutating_cached_wav_sample_clock(self):
        wav=dict(path='/media/Main.wav',start=3173509420,samples=480000,channels=1,sample_rate=48000,sound_roll='REAL')
        pgm=dict(path='/media/PGM.mov',source_kind='pgm',start_frames=15000,frames=250,fps=25)
        events=[dict(path=pgm['path'],source_kind='pgm',record_start=F(0),duration=F(1),source_start=F(600)),
                dict(path=wav['path'],record_start=F(1),duration=F(8),source_start=F(wav['start']+317,48000)),
                dict(path=pgm['path'],source_kind='pgm',record_start=F(9),duration=F(1),source_start=F(609))]
        sources=AudioSources([wav,pgm,dict(wav,path='/media/unused.wav')]);before=copy.deepcopy((sources,events))
        actual,kept=wav_only_sound_plan((sources,events),dict(pgm_sources=[dict(path=pgm['path'],start=0,frames=250)]),25)
        self.assertEqual([row['path'] for row in actual],[wav['path']]);self.assertEqual(kept,[events[1]])
        self.assertEqual(actual[0]['start'],wav['start']);self.assertEqual(actual[0]['sound_roll'],'REAL')
        self.assertEqual([(row['record_start_seconds'],row['duration_seconds']) for row in sound_reports(actual)],[(0,1),(9,1)])
        self.assertEqual((sources,events),before)

    def test_missing_plan_is_explicit_silence(self):
        sources,events=wav_only_sound_plan(None,dict(pgm_sources=[dict(path='PGM.mov',start=50,frames=100)]),25)
        self.assertFalse(sources);self.assertFalse(events)
        self.assertEqual([(row['record_start_seconds'],row['duration_seconds']) for row in sound_reports(sources)],[(2,4)])

    def test_paired_mono_overlapping_coverage_remains_two_original_channels(self):
        wavs=[dict(path='/media/'+side+'.wav',start=0,samples=480000,channels=1,pair_id='pair',channel=side) for side in ('L','R')]
        events=[dict(path=row['path'],record_start=F(1),duration=F(8),source_start=F(317,48000),pair_id='pair',out_channel=row['channel']) for row in wavs]
        sources,kept=wav_only_sound_plan((wavs,events),dict(pgm_sources=[dict(path='pgm.mov',start=0,frames=250)]),25)
        self.assertEqual(kept,events)
        self.assertEqual([(row['record_start_seconds'],row['duration_seconds']) for row in sound_reports(sources)],[(0,1),(9,1)])

    def test_output_policy_change_preserves_previous_pgm_analysis_signature(self):
        with tempfile.TemporaryDirectory() as folder:
            pgm=Path(folder)/'PGM.mov';pgm.write_bytes(b'unchanged analysis source')
            config=dict(fps=25,sound_sync_mode='timecode',sound_allow_gaps=False,sound_unmatched_policy='pgm',sound_sources=[])
            saved=make_pgm_fallback(config,str(pgm),'No matched WAV',pgm_info=dict(path=str(pgm),frames=250,fps=25,audio_channels=[1,2]))
            changed=dict(config,sound_allow_gaps=True,sound_unmatched_policy='silence')
            self.assertTrue(valid_pgm_fallback_signature(changed,str(pgm),saved))
            from library_export import sync_signature
            cut=dict(pgm_sources=[dict(path=str(pgm),frames=250,start=0)])
            self.assertEqual(sync_signature(dict(config,library_sound_fallback={str(pgm):saved}),cut),
                             sync_signature(dict(changed,library_sound_fallback={str(pgm):saved}),cut))

class HistoricalAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.folder=Path(self.temp.name)
        self.paths={sid:write_session(self.folder,sid,'stereo') for sid in ('base','alt')};self.before={}
        for sid,path in self.paths.items():
            tree=ET.parse(path);root=tree.getroot();resources=root.find('resources')
            resource=ET.SubElement(resources,'asset',id='legacy-pgm',name='OldPGM.mov',src=(self.folder/(sid+'_PGM.mov')).as_uri(),start='7200s',duration='10s',hasAudio='1',audioChannels='2',audioRate='48000')
            metadata=ET.SubElement(resource,'metadata');ET.SubElement(metadata,'md',key='com.pgm.audioSourceKind',value='pgm')
            for host in root.findall('./library/event/project/sequence/spine/clip'):
                ET.SubElement(host,'audio',ref='legacy-pgm',start='7200s',offset=host.get('start'),duration=host.get('duration'),srcCh='1, 2')
            assets={row.get('id'):row for row in resources.findall('asset')}
            for angle in root.findall('./resources/media/multicam/mc-angle'):
                for host in angle.findall('clip'):
                    video=host.find('video');asset=assets[video.get('ref')];asset.set('hasAudio','1');asset.set('audioChannels','2')
                    ET.SubElement(host,'audio',ref=video.get('ref'),start=video.get('start'),offset=video.get('offset'),duration=video.get('duration'),srcCh='1, 2')
            tree.write(path,encoding='utf-8',xml_declaration=True);self.before[sid]=path.read_bytes()

    def test_all_three_manual_modes_strip_legacy_audio_and_preserve_wav_source_values(self):
        for mode,include,selections in [('base',True,[]),('alternative',False,[selection('alt',1)]),('combined',True,[selection('alt',1)])]:
            with self.subTest(mode=mode):
                output=self.folder/('out_'+mode+'.fcpxml')
                plan=dict(manual_layout=True,resolve_zero_origin=True,fps=25,baseline_session_id='base',baseline_duration_frames=250,timeline_start_frame=0,timeline_end_frame=250,duration_frames=250,lane_count=bool(selections),selections=selections)
                result=assemble_reference_xml(self.paths['base'],{'alt':self.paths['alt']},plan,output,include_baseline=include)
                root=ET.parse(output).getroot();assets={row.get('id'):row for row in root.findall('./resources/asset')}
                audio=[row for row in root.iter() if row.tag in ('audio','asset-clip') and row.get('ref') in assets and assets[row.get('ref')].get('hasAudio')=='1']
                self.assertTrue(audio);self.assertTrue(all(assets[row.get('ref')].get('src').endswith('.wav') for row in audio))
                self.assertFalse(any(row.get('name')=='OldPGM.mov' for row in assets.values()))
                self.assertTrue(all(row.get('hasAudio') is None for row in assets.values() if row.get('hasVideo')=='1'))
                self.assertTrue(result['manifest']['audio_policy']['original_wav_only'])
                self.assertGreater(result['manifest']['audio_policy']['removed_audio_components'],0)
                for row in audio:
                    self.assertIn(F(row.get('start')[:-1]),[F(100)+F(7,48000),F(200)+F(14,48000),F(100)+F(7,48000)+3,F(200)+F(14,48000)+2,F(200)+F(14,48000)+5])
                for sid,path in self.paths.items():self.assertEqual(path.read_bytes(),self.before[sid])

    def test_new_merged_map_discards_pgm_guide_but_keeps_scene_identity(self):
        from reference_workflow import ReferenceWorkflow
        inputs=[]
        for sid in ('base','alt'):
            path=self.folder/(sid+'.json');inputs.append(path)
            path.write_text(json.dumps(dict(fps=25,records=[dict(proxy_path='/media/'+sid+'.mov',source_id=sid)],guide_audio=[dict(session_id=sid,path='/media/'+sid+'.wav',source_kind='wav'),dict(session_id=sid,path='/media/'+sid+'_PGM.mov',source_kind='pgm')])))
        output=self.folder/'merged.json';ReferenceWorkflow._merge_maps(inputs,output);rows=json.loads(output.read_text())
        self.assertEqual(len(rows['records']),2);self.assertEqual({row['source_kind'] for row in rows['guide_audio']},{'wav'})
        self.assertEqual({row['session_id'] for row in rows['guide_audio']},{'base','alt'})

if __name__=='__main__':unittest.main()
