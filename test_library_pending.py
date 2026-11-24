import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from library_pending import enrich_pending_pgm, is_manual_visual
from library_store import Store
from library_workflow import BatchWorkflow
from test_library_angles import catalogue, session_config


def full_sync(path='/qa/s1/PGM.mov'):
    return dict(status='matched', signature='valid-sync',
                placements=[dict(wav_path='/qa/main.wav', pgm_start_seconds=0, pgm_end_seconds=40,
                                 wav_start_seconds=0, wav_end_seconds=40, offset_seconds=0,
                                 wav_info=dict(sample_rate=48000, samples=1920000))], uncovered=[])


class PendingRangesTests(unittest.TestCase):
    def test_missing_audio_full_pgm_not_lost_and_all_angles_follow(self):
        data = catalogue()
        data['transcripts'] = []
        data['candidates'] = []
        first = enrich_pending_pgm(data)['pending_pgm_intervals'][0]
        second = enrich_pending_pgm(data)['pending_pgm_intervals'][0]
        self.assertEqual(first['id'], second['id'])
        self.assertEqual((first['start_seconds'], first['end_seconds']), (0, 40))
        self.assertEqual(first['reason_code'], 'audio_unmatched')
        self.assertEqual(first['related_angle_count'], 4)
        self.assertTrue(all(row['parent_candidate_id'] is None for row in first['related_angles']))
        self.assertFalse(first['has_audio_sync'])

    def test_real_transcript_queue_takes_precedence_over_pending_range(self):
        data = catalogue()
        data['candidates'][0]['status'] = 'candidate'
        ranges = enrich_pending_pgm(data)['pending_pgm_intervals']
        self.assertEqual([(row['start_seconds'], row['end_seconds']) for row in ranges], [(0, 10), (15, 40)])

    def test_confirmed_manual_interval_subtracted_but_audio_still_unmatched(self):
        data = catalogue()
        data['transcripts'][0].update(text='[人工画面归类]', data=dict(source_kind='manual_visual', classification_only=True))
        ranges = enrich_pending_pgm(data)['pending_pgm_intervals']
        self.assertEqual([(row['start_seconds'], row['end_seconds']) for row in ranges], [(0, 10), (15, 40)])
        self.assertTrue(all(not row['has_audio_sync'] for row in ranges))

    def test_rejected_visual_interval_returns_to_pending_once(self):
        data = catalogue()
        data['transcripts'][0]['data'] = dict(source_kind='manual_visual')
        data['candidates'][0]['status'] = 'rejected'
        ranges = enrich_pending_pgm(data)['pending_pgm_intervals']
        self.assertEqual([(row['start_seconds'], row['end_seconds']) for row in ranges], [(0, 40)])

    def test_synced_without_asr_is_pending_no_invented_words(self):
        data = catalogue()
        data['sessions'][0]['config']['library_sync'] = {'/qa/s1/PGM.mov': full_sync()}
        data['transcripts'] = []
        data['candidates'] = []
        row = enrich_pending_pgm(data)['pending_pgm_intervals'][0]
        self.assertEqual(row['reason_code'], 'asr_unavailable')
        self.assertTrue(row['has_audio_sync'])
        self.assertNotIn('text', row)

    def test_partial_audio_coverage_only_missing_range_pending(self):
        data = catalogue()
        sync = full_sync()
        sync['placements'][0].update(pgm_end_seconds=20, wav_end_seconds=20)
        data['sessions'][0]['config']['library_sync'] = {'/qa/s1/PGM.mov': sync}
        ranges = enrich_pending_pgm(data)['pending_pgm_intervals']
        self.assertEqual([(row['start_seconds'], row['end_seconds']) for row in ranges], [(20, 40)])
        self.assertEqual(ranges[0]['reason_code'], 'audio_gap')

    def test_failed_sync_does_not_trust_old_placements_or_old_asr(self):
        data = catalogue()
        config = data['sessions'][0]['config']
        config.update(library_sync={'/qa/s1/PGM.mov': full_sync()}, library_processing={
            '/qa/s1/PGM.mov': dict(status='failed', stage='sync', error='new matching failed')})
        ranges = enrich_pending_pgm(data)['pending_pgm_intervals']
        self.assertEqual([(row['start_seconds'], row['end_seconds']) for row in ranges], [(0, 40)])
        self.assertFalse(ranges[0]['has_audio_sync'])
        self.assertEqual(ranges[0]['error'], 'new matching failed')

    def test_failed_sync_still_retains_confirmed_manual_picture_decision(self):
        data = catalogue()
        config = data['sessions'][0]['config']
        config.update(library_sync={'/qa/s1/PGM.mov': full_sync()}, library_processing={
            '/qa/s1/PGM.mov': dict(status='failed', stage='sync', error='failed')})
        data['transcripts'][0]['data'] = dict(source_kind='manual_visual')
        ranges = enrich_pending_pgm(data)['pending_pgm_intervals']
        self.assertEqual([(row['start_seconds'], row['end_seconds']) for row in ranges], [(0, 10), (15, 40)])

    def test_failed_sync_keeps_explicit_manual_subtitle_review_anchor(self):
        data = catalogue()
        config = data['sessions'][0]['config']
        config.update(library_sync={'/qa/s1/PGM.mov': full_sync()}, library_processing={
            '/qa/s1/PGM.mov': dict(status='failed', stage='sync', error='failed')},
            library_asr={'/qa/s1/PGM.mov': dict(source_kind='manual')})
        data['candidates'][0]['status'] = 'candidate'
        ranges = enrich_pending_pgm(data)['pending_pgm_intervals']
        self.assertEqual([(row['start_seconds'], row['end_seconds']) for row in ranges], [(0, 10), (15, 40)])
        self.assertTrue(all(not row['has_audio_sync'] for row in ranges))

    def test_missing_headers_never_fabricates_duration(self):
        data = catalogue()
        data['sessions'][0]['config']['pgm_sources'][0]['frames'] = None
        result = enrich_pending_pgm(data)
        self.assertEqual(result['pending_pgm_intervals'], [])
        self.assertTrue(result['pending_pgm_warnings'])

    def test_other_session_confirmed_same_timecode_does_not_remove_pending(self):
        data = catalogue()
        data['sessions'].append(dict(id='s2', config=session_config('s2')))
        ranges = enrich_pending_pgm(data)['pending_pgm_intervals']
        second = [row for row in ranges if row['session_id'] == 's2']
        self.assertEqual([(row['start_seconds'], row['end_seconds']) for row in second], [(0, 40)])


class LocalClient:
    def __init__(self, store):
        self.store = store
    def call(self, method, **params):
        return self.store.call(method, params, '场记')


class PendingWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        env = patch.dict(os.environ, {'PGM_LIBRARY_HOME': str(self.root / 'app')})
        env.start()
        self.addCleanup(env.stop)
        self.store = Store(self.root / 'qa.sqlite3')
        self.addCleanup(self.store.close)
        self.client = LocalClient(self.store)
        self.production = self.client.call('create_production', name='人工核查试验')
        self.workflow = BatchWorkflow(self.client, self.production['id'])
        self.path = str(self.root / 'PGM.mov')
        Path(self.path).write_bytes(b'video fixture')
        self.config = session_config(cameras=('A', 'B'))
        self.config.update(pgm=self.path, library_record_type='recording',sound_unmatched_policy='error')
        self.config['pgm_sources'][0]['path'] = self.path
        self.session = self.client.call('save_session', production_id=self.production['id'], name='第一场', config=self.config)
        self.client.call('put_script', production_id=self.production['id'], title='台本',
                         lines=[dict(text='你为什么今天来到这里？', speaker='甲', order=0)])
        self.line = self.client.call('list_script', production_id=self.production['id'])[0]

    def associate(self, begin=10, end=15):
        return self.workflow.associate_pending_interval(self.session['id'], self.path, begin, end, self.line['id'], note='看A机确认')

    def test_manual_association_once_confirms_all_angles_and_keeps_raw_audio_missing(self):
        result = self.associate()
        self.assertEqual(result['candidate']['status'], 'confirmed')
        row = result['data']['transcripts'][0]
        self.assertTrue(is_manual_visual(row))
        self.assertEqual(row['text'], '[人工画面归类]')
        self.assertNotEqual(row['text'], self.line['text'])
        self.assertEqual(result['data']['candidates'][0]['related_angle_count'], 2)
        self.assertNotIn('library_sync', result['session']['config'])
        self.assertEqual([(row['start_seconds'], row['end_seconds']) for row in result['data']['pending_pgm_intervals']], [(0, 10), (15, 40)])

    def test_manual_association_retry_is_idempotent(self):
        first, second = self.associate(), self.associate()
        self.assertEqual(first['candidate']['id'], second['candidate']['id'])
        self.assertEqual(len(second['data']['transcripts']), 1)
        self.assertEqual(len(second['data']['candidates']), 1)

    def test_invalid_range_or_wrong_pgm_rejected_without_records(self):
        for begin, end in ((-1, 2), (10, 9), (0, 50)):
            with self.assertRaises(ValueError):
                self.associate(begin, end)
        with self.assertRaises(ValueError):
            self.workflow.associate_pending_interval(self.session['id'], '/another/PGM.mov', 0, 1, self.line['id'])
        self.assertEqual(self.client.call('list_transcripts', production_id=self.production['id']), [])

    def test_manual_visual_survives_later_asr_active_id_list(self):
        self.associate()
        self.workflow._save_state(self.session['id'], 'library_asr', self.path,
                                  dict(complete=True, active_transcript_ids=[], sync_signature='new'))
        data = self.workflow.read_all()
        self.assertEqual(len(data['transcripts']), 1)
        self.assertEqual(data['candidates'][0]['status'], 'confirmed')

    def test_sync_failure_stored_without_destroying_old_sync_and_retry_clears(self):
        original = full_sync(self.path)
        self.workflow._save_state(self.session['id'], 'library_sync', self.path, original)
        with patch('library_sync.ensure_synced_pgm', side_effect=ValueError('找不到可靠波形')):
            failed = self.workflow.recognize_and_match()
        saved = self.client.call('get_session', session_id=self.session['id'])
        self.assertEqual(saved['config']['library_sync'][self.path], original)
        self.assertEqual(saved['config']['library_processing'][self.path]['stage'], 'sync')
        self.assertEqual(len(failed['data']['pending_pgm_intervals']), 1)
        with patch('library_sync.ensure_synced_pgm', return_value=original), \
                patch('library_sync.transcribe_synced_pgm', return_value={'segments': [
                    dict(start_seconds=10, end_seconds=15, text=self.line['text'])]}):
            success = self.workflow.recognize_and_match()
        saved = self.client.call('get_session', session_id=self.session['id'])
        self.assertNotIn(self.path, saved['config'].get('library_processing', {}))
        self.assertEqual(success['failures'], [])

    def test_asr_failure_preserves_successful_sound_sync(self):
        sync = full_sync(self.path)
        with patch('library_sync.ensure_synced_pgm', return_value=sync), \
                patch('library_sync.transcribe_synced_pgm', side_effect=ValueError('识别失败')):
            self.workflow.recognize_and_match()
        saved = self.client.call('get_session', session_id=self.session['id'])
        self.assertEqual(saved['config']['library_sync'][self.path], sync)
        self.assertEqual(saved['config']['library_processing'][self.path]['stage'], 'asr')
        self.assertEqual(self.workflow.read_all()['pending_pgm_intervals'][0]['reason_code'], 'asr_unavailable')

    def test_manual_marker_never_enters_automatic_dialogue_match(self):
        self.associate()
        captured = []

        def match(lines, transcripts, **kwargs):
            captured.extend(transcripts)
            return []
        with patch('library_sync.ensure_synced_pgm', return_value=full_sync(self.path)), \
                patch('library_sync.transcribe_synced_pgm', return_value={'segments': []}), \
                patch('dialogue_engine.match_dialogue', side_effect=match):
            self.workflow.recognize_and_match()
        self.assertEqual(captured, [])
        self.assertEqual(self.workflow.read_all()['candidates'][0]['status'], 'confirmed')

    def test_high_dialogue_auto_confirms_once_and_exposes_provenance(self):
        with patch('library_sync.ensure_synced_pgm', return_value=full_sync(self.path)), \
                patch('library_sync.transcribe_synced_pgm', return_value={'segments': [
                    dict(start_seconds=10, end_seconds=15, text=self.line['text'])]}) as asr:
            result = self.workflow.recognize_and_match()
            repeated = self.workflow.recognize_and_match()
        self.assertEqual(result['autoconfirmed'], 1)
        candidate = result['data']['candidates'][0]
        self.assertEqual(candidate['status'], 'confirmed')
        self.assertEqual(candidate['classification_method'], 'auto_dialogue')
        self.assertIn('auto_dialogue', candidate['tags'])
        self.assertEqual(candidate['related_angle_count'], 2)
        self.assertFalse(candidate['review_required'])
        self.assertEqual(repeated['autoconfirmed'], 0)
        self.assertEqual(asr.call_count, 1)

    def test_explicit_refresh_persists_old_eligible_candidate_without_asr(self):
        self.workflow._save_state(self.session['id'], 'library_sync', self.path, full_sync(self.path))
        transcript = self.client.call('add_transcript', session_id=self.session['id'], media_path=self.path,
            segments=[dict(start_seconds=10, end_seconds=15, text=self.line['text'])])['segments'][0]
        self.client.call('replace_candidates', production_id=self.production['id'], candidates=[dict(
            script_line_id=self.line['id'], transcript_id=transcript['id'], score=1,
            reason='台词内容吻合')])
        readonly = self.workflow.read_all()['candidates'][0]
        self.assertEqual(readonly['status'], 'candidate')
        self.assertTrue(readonly['auto_eligible'])
        self.assertEqual(self.client.call('list_candidates', production_id=self.production['id'])[0]['status'], 'candidate')
        with patch('library_sync.transcribe_synced_pgm', side_effect=AssertionError('refresh must not run ASR')):
            result = self.workflow.refresh_review()
            repeated = self.workflow.refresh_review()
        self.assertEqual(result['autoconfirmed'], 1)
        self.assertEqual(result['data']['candidates'][0]['status'], 'confirmed')
        self.assertEqual(result['data']['candidates'][0]['classification_method'], 'auto_dialogue')
        self.assertEqual(repeated['autoconfirmed'], 0)

    def test_explicit_whole_pgm_range_keeps_asr_positions_and_undo_restores_associations(self):
        manual = self.associate()['candidate']
        transcript = self.client.call('add_transcript', session_id=self.session['id'], media_path=self.path,
            segments=[dict(start_seconds=11, end_seconds=14, text=self.line['text'])])['segments'][0]
        self.client.call('replace_candidates', production_id=self.production['id'], candidates=[dict(
            script_line_id=self.line['id'], transcript_id=transcript['id'], score=1, reason='台词内容吻合')])
        self.workflow._save_state(self.session['id'], 'library_asr', self.path,
            dict(complete=True, active_transcript_ids=[transcript['id']]))
        visible = self.workflow.read_all()
        self.assertCountEqual([row['id'] for row in visible['transcripts']],
                              [manual['transcript_id'], transcript['id']])
        self.assertEqual(len(visible['candidates']), 1)
        self.assertEqual(visible['candidates'][0]['classification_method'], 'manual_visual')
        # Full-file manual coverage retains real subtitle times for other
        # baseline lines; deleting the association never rewrites that evidence.
        self.assertEqual(len(self.client.call('list_transcripts', production_id=self.production['id'])), 2)
        version = visible['pgm_match_versions'][0]
        self.workflow.clear_pgm_matches(self.session['id'], self.path,
            expected_session_revision=version['session_revision'],
            expected_candidate_revisions=version['candidate_revisions'],
            expected_reference_revision=version['reference_revision'])
        restored = self.workflow.read_all()
        self.assertIn(transcript['id'], [row['id'] for row in restored['transcripts']])
        self.assertEqual(self.client.call('list_transcripts', production_id=self.production['id'])[1]['text'],
                         transcript['text'])
        self.assertEqual([(row['start_seconds'], row['end_seconds']) for row in restored['pending_pgm_intervals']],
                         [(0, 11), (14, 40)])

    def test_automatic_review_never_overwrites_human_rejection(self):
        with patch('library_sync.ensure_synced_pgm', return_value=full_sync(self.path)), \
                patch('library_sync.transcribe_synced_pgm', return_value={'segments': [
                    dict(start_seconds=10, end_seconds=15, text=self.line['text'])]}):
            first = self.workflow.recognize_and_match()
            candidate = first['data']['candidates'][0]
            self.client.call('update_candidate', candidate_id=candidate['id'], status='rejected',
                             note='导演人工排除', expected_revision=candidate['revision'])
            second = self.workflow.recognize_and_match()
        self.assertEqual(second['autoconfirmed'], 0)
        self.assertEqual(second['data']['candidates'][0]['status'], 'rejected')
        self.assertEqual(second['data']['candidates'][0]['note'], '导演人工排除')

    def test_concurrent_human_decision_wins_automatic_revision_check(self):
        original_call = self.client.call
        intercepted = []

        def call(method, **params):
            if method == 'update_candidate' and 'auto_dialogue' in params.get('tags', []):
                intercepted.append(params['candidate_id'])
                original_call('update_candidate', candidate_id=params['candidate_id'], status='rejected',
                              note='并行人工排除', expected_revision=params['expected_revision'])
            return original_call(method, **params)

        with patch.object(self.client, 'call', side_effect=call), \
                patch('library_sync.ensure_synced_pgm', return_value=full_sync(self.path)), \
                patch('library_sync.transcribe_synced_pgm', return_value={'segments': [
                    dict(start_seconds=10, end_seconds=15, text=self.line['text'])]}):
            result = self.workflow.recognize_and_match()
        self.assertEqual(len(intercepted), 1)
        self.assertEqual(result['autoconfirmed'], 0)
        self.assertEqual(result['data']['candidates'][0]['status'], 'rejected')
        self.assertEqual(result['data']['candidates'][0]['note'], '并行人工排除')


if __name__ == '__main__':
    unittest.main()
