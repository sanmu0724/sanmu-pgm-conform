"""PGM reference speech keeps its provenance through review and ASR filtering."""
import unittest
from unittest.mock import patch

from library_correspondence import filter_active_transcripts
from library_auto_review import classify_auto_review
from library_pending import enrich_pending_pgm
from test_library_auto_review import sample, PATH
from test_library_angles import catalogue


def reference_state(config, path, ids, duration, mixed=False):
    fingerprint=dict(size=1234,mtime_ns=5678)
    config['library_sound_fallback']={path:dict(status='fallback',stage='sync',audio_source='pgm',
        signature='reference-sig',source_stat=dict(path=path,**fingerprint))}
    signature=(config.get('library_sync',{}).get(path) or {}).get('signature') if mixed else 'reference-sig'
    state=dict(complete=True,active_transcript_ids=ids,asr_audio_source='mixed' if mixed else 'pgm_reference',
        sync_signature=signature,source_stat=fingerprint,pgm_reference_ranges=[dict(start_seconds=0,end_seconds=duration)])
    config['library_asr']={path:state}
    return state


class ReferenceAudioReviewTests(unittest.TestCase):
    def test_current_pgm_asr_can_pass_without_external_wav_and_ignores_old_sync_signature(self):
        data=sample();config=data['sessions'][0]['config']
        config.update(library_sync={PATH:dict(status='matched',signature='old-wav',placements=[])},
            library_processing={PATH:dict(status='failed',stage='sync',error='外部录音不存在')})
        reference_state(config,PATH,['t1'],10)
        with patch('core.probe',side_effect=AssertionError('review must not probe media')):
            self.assertEqual([row['id'] for row in filter_active_transcripts(data)['transcripts']],['t1'])
            self.assertEqual([row['id'] for row in classify_auto_review(data)['eligible']],['c1'])

    def test_old_wav_speech_does_not_become_reference_speech_by_enabling_fallback(self):
        data=sample();config=data['sessions'][0]['config']
        config['library_processing']={PATH:dict(status='failed',stage='sync')}
        self.assertFalse(classify_auto_review(data)['eligible'])
        self.assertEqual(filter_active_transcripts(data)['transcripts'],[])

    def test_changed_fallback_signature_or_missing_source_stat_hides_old_reference_asr(self):
        for change in ('signature','stat','policy'):
            with self.subTest(change=change):
                data=sample();config=data['sessions'][0]['config'];state=reference_state(config,PATH,['t1'],10)
                if change=='signature':config['library_sound_fallback'][PATH]['signature']='changed'
                elif change=='stat':state.pop('source_stat')
                else:config['sound_unmatched_policy']='error'
                self.assertEqual(filter_active_transcripts(data)['transcripts'],[])
                self.assertFalse(classify_auto_review(data)['eligible'])

    def test_mixed_wav_and_reference_gap_has_complete_real_audio_coverage(self):
        data=sample();config=data['sessions'][0]['config'];sync=config['library_sync'][PATH]
        sync['signature']='wav-sig'
        sync['placements'][0].update(pgm_end_seconds=2,wav_end_seconds=2)
        sync['uncovered']=[dict(start_seconds=2,end_seconds=10)]
        state=reference_state(config,PATH,['t1'],10,mixed=True)
        state['pgm_reference_ranges']=[dict(start_seconds=2,end_seconds=10)]
        self.assertEqual([row['id'] for row in classify_auto_review(data)['eligible']],['c1'])
        state['pgm_reference_ranges'][0]['start_seconds']=2.1
        self.assertFalse(classify_auto_review(data)['eligible'])

    def test_complete_reference_asr_does_not_create_missing_external_audio_review_range(self):
        data=catalogue();config=data['sessions'][0]['config'];path='/qa/s1/PGM.mov'
        config['library_processing']={path:dict(status='failed',stage='sync',error='未录外部声音')}
        reference_state(config,path,[data['transcripts'][0]['id']],40)
        pending=enrich_pending_pgm(data)['pending_pgm_intervals']
        self.assertEqual(pending,[])

    def test_reference_asr_partial_coverage_still_exposes_real_missing_range(self):
        data=catalogue();config=data['sessions'][0]['config'];path='/qa/s1/PGM.mov'
        reference_state(config,path,[data['transcripts'][0]['id']],20)
        ranges=enrich_pending_pgm(data)['pending_pgm_intervals']
        self.assertEqual([(row['start_seconds'],row['end_seconds']) for row in ranges],[(20,40)])
        self.assertEqual(ranges[0]['reason_code'],'audio_gap')


if __name__=='__main__':unittest.main()
