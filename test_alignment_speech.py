import copy
import unittest
from unittest.mock import patch
from alignment_speech import ensure_baseline_speech,FIELD

class AlignmentSpeechTests(unittest.TestCase):
    def fixture(self):
        data=dict(sessions=[dict(id='base',name='通条',config={}),dict(id='alt',config={})],scripts=[],candidates=[],
            transcripts=[dict(id='alt-asr',session_id='alt',media_path='/alt.mov',start_seconds=0,end_seconds=2,text='不应重识别')])
        cut=dict(fps=25,duration=250,pgm_sources=[dict(path='/base.mov',start=0,end=250,frames=250)])
        return data,cut

    def run_speech(self,data,cut,**kwargs):
        return ensure_baseline_speech(None,'p',data,'base',cut,**kwargs)

    def test_existing_base_speech_reuses_offline_without_model(self):
        data,cut=self.fixture()
        data['transcripts'].append(dict(id='b',session_id='base',media_path='/base.mov',start_seconds=1,end_seconds=3,text='已经识别'))
        before=copy.deepcopy(data)
        with patch('alignment_speech._signature',return_value=None),patch('dialogue_engine.transcribe_media',side_effect=AssertionError('ASR')):
            result=self.run_speech(data,cut)
        self.assertEqual(result['reused'],1);self.assertEqual(result['recognized'],0)
        self.assertEqual(data,before)
        self.assertEqual({r['id'] for r in result['data']['transcripts']},{'b','alt-asr'})

    def test_missing_source_without_cache_is_reported_not_transcribed(self):
        data,cut=self.fixture()
        with patch('alignment_speech._signature',return_value=None),patch('dialogue_engine.transcribe_media') as transcribe:
            result=self.run_speech(data,cut)
        self.assertEqual(len(result['failures']),1);transcribe.assert_not_called()

    def test_private_cache_reuses_stable_ids_without_source(self):
        data,cut=self.fixture();data['sessions'][0]['config'][FIELD]={'/base.mov':dict(version=1,complete=True,audio_source='pgm',
            source_stat=dict(size=10,mtime_ns=1),segments=[dict(start_seconds=1,end_seconds=3,text='缓存台词')])}
        with patch('alignment_speech._signature',return_value=None),patch('dialogue_engine.transcribe_media',side_effect=AssertionError('ASR')):
            a=self.run_speech(data,cut);b=self.run_speech(data,cut)
        self.assertEqual(a['data']['transcripts'],b['data']['transcripts']);self.assertEqual(a['reused'],1)
        self.assertNotIn('library_asr',data['sessions'][0]['config'])

    def test_new_transcription_only_saves_private_baseline_cache(self):
        data,cut=self.fixture();before=copy.deepcopy(data);segments=[dict(start_seconds=1,end_seconds=3,text='新的台词')]
        with patch('alignment_speech._signature',return_value=dict(size=10,mtime_ns=1)),\
             patch('dialogue_engine.transcribe_media',return_value=segments) as transcribe,\
             patch('library_workflow.BatchWorkflow._save_state') as save:
            result=self.run_speech(data,cut)
        self.assertEqual(result['recognized'],1);self.assertEqual(data,before)
        self.assertEqual(save.call_args.args[:3],('base',FIELD,'/base.mov'))
        self.assertEqual(transcribe.call_args.args[0],'/base.mov')
        self.assertEqual(transcribe.call_args.kwargs['language'],'auto')
        self.assertEqual(result['data']['transcripts'][0]['id'],'alt-asr')

    def test_decode_missing_false_never_opens_model(self):
        data,cut=self.fixture()
        with patch('alignment_speech._signature',return_value=dict(size=1,mtime_ns=1)),patch('dialogue_engine.transcribe_media') as transcribe:
            result=self.run_speech(data,cut,decode_missing=False)
        transcribe.assert_not_called();self.assertEqual(len(result['failures']),1)

    def test_source_replaced_is_not_silently_reused(self):
        data,cut=self.fixture();data['transcripts'].append(dict(id='b',session_id='base',media_path='/base.mov',
            start_seconds=1,end_seconds=3,text='原台词',metadata={'source_stat':dict(size=1,mtime_ns=1)}))
        with patch('alignment_speech._signature',return_value=dict(size=2,mtime_ns=2)),patch('dialogue_engine.transcribe_media') as transcribe:
            result=self.run_speech(data,cut)
        transcribe.assert_not_called();self.assertEqual(len(result['failures']),1)
        self.assertNotIn('b',{r['id'] for r in result['data']['transcripts']})

    def test_cancellation_does_not_save_partial_position_or_speech(self):
        data,cut=self.fixture()
        with self.assertRaisesRegex(ValueError,'取消'):
            self.run_speech(data,cut,cancelled=lambda:True)

    def test_no_speech_cache_is_reused_without_redecode(self):
        data,cut=self.fixture();data['sessions'][0]['config'][FIELD]={'/base.mov':dict(version=1,complete=True,audio_source='pgm',segments=[])}
        with patch('alignment_speech._signature',return_value=None),patch('dialogue_engine.transcribe_media') as transcribe:
            result=self.run_speech(data,cut)
        transcribe.assert_not_called();self.assertEqual(result['reused'],1)

    def test_reference_view_ids_survive_active_filter_without_changing_organizer(self):
        from library_correspondence import filter_active_transcripts
        data,cut=self.fixture();data['sessions'][0]['config'].update(library_asr={'/base.mov':{'active_transcript_ids':['old']}},
            library_baseline_alignment_asr={'/base.mov':dict(version=1,complete=True,audio_source='pgm',segments=[dict(start_seconds=1,end_seconds=2,text='新缓存')])})
        with patch('alignment_speech._signature',return_value=None):result=self.run_speech(data,cut)
        filtered=filter_active_transcripts(result['data'])
        self.assertEqual(len([r for r in filtered['transcripts'] if r['session_id']=='base']),1)
        self.assertEqual(data['sessions'][0]['config']['library_asr']['/base.mov']['active_transcript_ids'],['old'])


    def test_old_visual_range_never_hides_available_baseline_speech_from_reuse(self):
        data,cut=self.fixture()
        data['scripts']=[dict(id='L1',script_id='S',order=0,text='台词')]
        words=dict(id='spoken',session_id='base',media_path='/base.mov',start_seconds=1,end_seconds=2,text='实际台词')
        visual=dict(id='old-visual',session_id='base',media_path='/base.mov',start_seconds=0,end_seconds=10,
            text='人工范围',data=dict(source_kind='manual_visual',manual_script_line_ids=['L1']))
        data['transcripts'].extend([words,visual])
        data['candidates']=[dict(visual,id='old-candidate',transcript_id='old-visual',script_line_id='L1',status='confirmed',score=1)]
        before=copy.deepcopy(data)
        with patch('alignment_speech._signature',return_value=None),patch('dialogue_engine.transcribe_media',side_effect=AssertionError('ASR must reuse')):
            result=self.run_speech(data,cut)
        self.assertEqual(result['reused'],1);self.assertEqual(result['recognized'],0)
        self.assertIn('spoken',{row['id'] for row in result['data']['transcripts']})
        self.assertEqual(data,before)

if __name__=='__main__':unittest.main()
