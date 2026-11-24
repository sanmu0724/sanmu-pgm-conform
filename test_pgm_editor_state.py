"""Explicit PGM review/remove decisions remain file-level and transactional."""
import concurrent.futures
import copy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from library_store import LibraryError, Store
from library_workflow import BatchWorkflow
from pgm_editor_state import coverage_fingerprint, read_editor_state
from pgm_takes import PgmTakeIndex


class LocalClient:
    actor = '场记甲'
    def __init__(self, store): self.store = store
    def call(self, method, **params): return self.store.call(method, params, self.actor)


class EditorFingerprintTests(unittest.TestCase):
    def setUp(self):
        self.first = dict(script_line_id='line1', transcript_id='speech1', start_seconds=7.1, end_seconds=9.2)
        self.summary = dict(covered_line_ids=['line1', 'line2'], first_candidate=self.first,
                            coverage_candidates=[self.first, dict(script_line_id='line2',
                            transcript_id='speech2', start_seconds=10, end_seconds=12)])
        self.path = '/nas/PGM.mov'

    def reviewed_config(self):
        return {'library_pgm_editor': {self.path: dict(pool='timeline', reviewed=True,
            coverage_fingerprint=coverage_fingerprint(self.summary))}}

    def test_legacy_confirmed_candidates_do_not_imply_human_review(self):
        self.first.update(status='confirmed', tags=['auto_dialogue'], score=.99)
        self.assertEqual(read_editor_state({}, self.path, summary=self.summary)['pool'], 'timeline')
        self.assertFalse(read_editor_state({}, self.path, summary=self.summary)['reviewed'])

    def test_valid_fingerprint_is_green_but_changed_coverage_or_anchor_is_blue(self):
        config = self.reviewed_config()
        self.assertTrue(read_editor_state(config, self.path, summary=self.summary)['reviewed'])
        for field, value in [('covered_line_ids', ['line1']), ('first_candidate', None)]:
            current = dict(self.summary, **{field: value})
            self.assertFalse(read_editor_state(config, self.path, summary=current)['reviewed'])
        current = copy.deepcopy(self.summary)
        current['first_candidate']['start_seconds'] = 8.2
        self.assertFalse(read_editor_state(config, self.path, summary=current)['reviewed'])

    def test_note_score_revision_and_coverage_order_do_not_change_semantic_fingerprint(self):
        before = coverage_fingerprint(self.summary)
        self.first.update(note='导演备注', score=.65, revision=99)
        self.summary['coverage_candidates'].reverse()
        self.assertEqual(coverage_fingerprint(self.summary), before)

    def test_explicit_source_override_and_numeric_types_use_same_fingerprint(self):
        source = dict(start_seconds='7.1000000000', end_seconds='9.2')
        self.assertEqual(coverage_fingerprint(self.summary, source), coverage_fingerprint(self.summary))

    def test_removed_has_priority_over_new_automatic_coverage_and_never_green(self):
        config = self.reviewed_config()
        config['library_pgm_editor'][self.path]['pool'] = 'removed'
        self.assertEqual(read_editor_state(config, self.path, summary=self.summary)['pool'], 'removed')
        self.assertFalse(read_editor_state(config, self.path, summary=self.summary)['reviewed'])

    def test_missing_coverage_is_unmatched_and_malformed_state_fails_closed(self):
        for config in ({}, {'library_pgm_editor': []}, {'library_pgm_editor': {self.path: []}}):
            result = read_editor_state(config, self.path, summary={})
            self.assertEqual(result['pool'], 'unmatched')
            self.assertFalse(result['reviewed'])
        self.assertEqual(coverage_fingerprint({}), '')


class PgmEditorStateTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = Path(self.folder.name)/'catalogue.db'
        self.store = Store(self.db)
        self.production = self.store.create_production('人工核实PGM')['id']
        self.store.put_script(self.production, '完整台本', [dict(text=f'第{number}句台词', order=number)
                              for number in range(1, 9)])
        self.lines = self.store.list_script(self.production)
        self.path = '/NAS/day01/PGM001.mov'
        self.config = dict(pgm=self.path, pgm_timecodes={self.path: '21:00:00:00'},
            library_media=[dict(kind='pgm', path=self.path, fps=25, frames=2500)],
            library_sync={self.path: dict(status='matched', placements=[dict(wav_path='/NAS/audio.wav',
                sound_roll='261009_TIoBE', pgm_start_seconds=0, pgm_end_seconds=100)])})
        self.session = self.store.save_session(self.production, 'day01_分镜头', self.config)
        self.speech = self.store.add_transcript(self.session['id'], self.path, [
            dict(start_seconds=7.1, end_seconds=9.2, text='第一句台词'),
            dict(start_seconds=10, end_seconds=12, text='第二句台词')],
            metadata=dict(reel='CAMERA_ORIGINAL', sound_roll='261009_TIoBE', date='2026-10-09'))
        self.store.replace_candidates(self.production, [dict(transcript_id=identity,
            script_line_id=self.lines[number]['id'], score=.85)
            for number, identity in enumerate(self.speech['transcript_ids'])])
        for row in self.store.list_candidates(self.production):
            self.store.update_candidate(row['id'], 'confirmed', tags=['auto_dialogue'],
                                        expected_revision=row['revision'])
        self.workflow = BatchWorkflow(LocalClient(self.store), self.production, '场记甲')

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def data(self): return self.workflow.read_all()

    def version(self):
        return next(row for row in self.data()['pgm_match_versions']
                    if row['session_id'] == self.session['id'] and row['media_path'] == self.path)

    def state(self, data=None):
        data = data or self.data()
        session = next(row for row in data['sessions'] if row['id'] == self.session['id'])
        index = PgmTakeIndex(data, prepared=True)
        summary = index.file_summary(dict(session_id=self.session['id'], media_path=self.path))
        source = index.source(summary['first_candidate']) if summary.get('first_candidate') else None
        return read_editor_state(session['config'], self.path, summary=summary, source=source)

    def review(self, version=None, **kwargs):
        return self.workflow.mark_pgm_reviewed(self.session['id'], self.path,
            expected_match_version=version or self.version(), **kwargs)

    def remove(self, version=None):
        version = version or self.version()
        return self.workflow.clear_pgm_matches(self.session['id'], self.path,
            expected_session_revision=version['session_revision'],
            expected_candidate_revisions=version['candidate_revisions'],
            expected_reference_revision=version['reference_revision'])

    def locate(self, first=2, last=4):
        return self.workflow.associate_pending_interval(self.session['id'], self.path, 7.1, 100,
            self.lines[first-1]['id'], script_line_ids=[row['id'] for row in self.lines[first-1:last]],
            expected_match_version=self.version())

    def test_initial_editor_pools_have_no_removed_files_and_auto_confirmed_stays_blue(self):
        self.assertNotIn('library_pgm_editor', self.store.get_session(self.session['id'])['config'])
        self.assertEqual(self.state()['pool'], 'timeline')
        self.assertFalse(self.state()['reviewed'])

    def test_review_persists_without_changing_source_candidates_selection_or_sound(self):
        before = self.store.list_transcripts(self.production), self.store.list_candidates(self.production)
        reference = self.store.get_reference_settings(self.production)
        result = self.review()
        self.assertTrue(self.state(result['data'])['reviewed'])
        self.assertEqual(result['editor_state']['updated_by'], '场记甲')
        self.assertEqual((self.store.list_transcripts(self.production), self.store.list_candidates(self.production)), before)
        self.assertEqual(self.store.get_reference_settings(self.production), reference)
        config = copy.deepcopy(result['session']['config'])
        config.pop('library_pgm_editor')
        self.assertEqual(config, self.config)
        self.assertEqual(result['session']['revision'], self.session['revision']+1)
        reopened = Store(self.db)
        try:
            self.assertEqual(reopened.get_session(self.session['id'])['config']['library_pgm_editor'],
                             result['session']['config']['library_pgm_editor'])
        finally:
            reopened.close()

    def test_review_can_be_explicitly_cleared_without_removing_coverage(self):
        self.review()
        result = self.review(reviewed=False)
        self.assertEqual(self.state(result['data'])['pool'], 'timeline')
        self.assertFalse(self.state(result['data'])['reviewed'])

    def test_delete_persists_removed_pool_and_keeps_all_media_and_asr(self):
        original = self.store.list_transcripts(self.production)
        result = self.remove()
        self.assertEqual(result['editor_state']['pool'], 'removed')
        self.assertEqual(self.state(result['data'])['pool'], 'removed')
        self.assertFalse(self.state(result['data'])['reviewed'])
        self.assertEqual(self.store.list_transcripts(self.production), original)
        config = copy.deepcopy(result['session']['config'])
        config.pop('library_pgm_editor')
        self.assertEqual(config, self.config)

    def test_manual_range_readds_removed_file_and_reviews_complete_source(self):
        self.remove()
        result = self.locate()
        self.assertEqual(self.state(result['data'])['pool'], 'timeline')
        self.assertTrue(self.state(result['data'])['reviewed'])
        source = result['transcript']['segments'][0]
        self.assertEqual((source['start_seconds'], source['end_seconds']), (7.1, 100))
        self.assertEqual(result['session']['config']['pgm_timecodes'], self.config['pgm_timecodes'])
        self.assertEqual(result['session']['config']['library_sync'], self.config['library_sync'])

    def test_single_line_legacy_manual_entry_uses_same_atomic_editor_state(self):
        self.remove()
        result = self.workflow.associate_pending_interval(self.session['id'], self.path,
            7.1, 100, self.lines[2]['id'])
        self.assertTrue(self.state(result['data'])['reviewed'])
        self.assertEqual(self.state(result['data'])['pool'], 'timeline')

    def test_range_correction_marks_new_correspondence_reviewed_and_preserves_local_anchor(self):
        self.locate()
        result = self.workflow.update_pgm_script_coverage(self.session['id'], self.path,
            [row['id'] for row in self.lines[3:6]], expected_match_version=self.version())
        self.assertTrue(self.state(result['data'])['reviewed'])
        self.assertEqual(result['transcript']['segments'][0]['start_seconds'], 7.1)
        self.assertEqual(result['candidate']['script_line_id'], self.lines[3]['id'])

    def test_automatic_changed_correspondence_does_not_inherit_previous_review(self):
        self.review()
        for row in self.store.list_candidates(self.production):
            self.store.update_candidate(row['id'], 'rejected', expected_revision=row['revision'])
        self.store.replace_candidates(self.production, [dict(transcript_id=self.speech['transcript_ids'][0],
            script_line_id=self.lines[4]['id'], score=.95)])
        self.assertEqual(self.state()['pool'], 'timeline')
        self.assertFalse(self.state()['reviewed'])

    def test_automatic_new_asr_cannot_revive_explicitly_removed_file(self):
        self.remove()
        speech = self.store.add_transcript(self.session['id'], self.path,
            [dict(start_seconds=2, end_seconds=3, text='新识别台词')])
        self.store.replace_candidates(self.production, [dict(transcript_id=speech['transcript_ids'][0],
            script_line_id=self.lines[4]['id'], score=.99)])
        self.assertTrue(PgmTakeIndex(self.data(), prepared=True).file_summary(
            dict(session_id=self.session['id'], media_path=self.path))['covered_line_ids'])
        self.assertEqual(self.state()['pool'], 'removed')
        self.assertFalse(self.state()['reviewed'])

    def test_note_or_auto_score_refresh_does_not_revoke_same_correspondence_review(self):
        self.review()
        row = self.store.list_candidates(self.production)[0]
        self.store.update_candidate(row['id'], 'confirmed', note='导演的新备注', tags=row['tags'],
                                    expected_revision=row['revision'])
        self.assertTrue(self.state()['reviewed'])

    def test_normal_scene_config_save_cannot_erase_or_override_editor_decisions(self):
        reviewed = self.review()
        original = copy.deepcopy(self.config)
        original['library_pgm_editor'] = {self.path: dict(pool='removed', reviewed=False)}
        updated = self.store.save_session(self.production, self.session['name'], original,
            session_id=self.session['id'], expected_revision=reviewed['session']['revision'])
        self.assertEqual(updated['config']['library_pgm_editor'], reviewed['session']['config']['library_pgm_editor'])
        self.assertTrue(self.state()['reviewed'])

    def test_same_basename_in_other_session_does_not_share_removed_or_reviewed_state(self):
        path = '/NAS/day02/PGM001.mov'
        other = self.store.save_session(self.production, 'day02_通条', dict(pgm=path,
            library_media=[dict(kind='pgm', path=path, fps=25, frames=2500)]))
        self.remove()
        self.assertNotIn('library_pgm_editor', self.store.get_session(other['id'])['config'])
        self.assertEqual(self.state()['pool'], 'removed')

    def test_review_rejects_removed_file_instead_of_silently_readding(self):
        self.remove()
        before = self.store.get_session(self.session['id'])
        with self.assertRaisesRegex(LibraryError, '重新添加'):
            self.review()
        self.assertEqual(self.store.get_session(self.session['id']), before)

    def test_review_without_current_coverage_does_not_write(self):
        for row in self.store.list_candidates(self.production):
            self.store.update_candidate(row['id'], 'rejected', expected_revision=row['revision'])
        before = self.store.get_session(self.session['id'])
        with self.assertRaisesRegex(LibraryError, '有效台本覆盖'):
            self.review()
        self.assertEqual(self.store.get_session(self.session['id']), before)

    def test_review_cas_guards_session_candidate_and_reference_versions(self):
        before = self.store.get_session(self.session['id'])
        for field in ('session_revision', 'candidate_revisions', 'reference_revision'):
            version = copy.deepcopy(self.version())
            if field == 'candidate_revisions': version[field] = {}
            else: version[field] += 1
            with self.subTest(field=field), self.assertRaises(LibraryError) as caught:
                self.review(version=version)
            self.assertEqual(caught.exception.code, 'conflict')
            self.assertEqual(self.store.get_session(self.session['id']), before)

    def test_concurrent_review_and_delete_have_one_atomic_winner(self):
        version = self.version()
        barrier = threading.Barrier(2)
        def action(name):
            barrier.wait()
            try:
                result = self.review(version=version) if name == 'review' else self.remove(version=version)
                return result['editor_state']['pool']
            except LibraryError as error:
                return error.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(action, ('review', 'delete')))
        self.assertEqual(results.count('conflict'), 1)
        self.assertEqual(len([row for row in results if row in ('timeline', 'removed')]), 1)

    def test_new_hidden_candidate_after_snapshot_blocks_review(self):
        version = self.version()
        self.store.replace_candidates(self.production, [dict(transcript_id=self.speech['transcript_ids'][0],
            script_line_id=self.lines[5]['id'], score=.7)])
        before = self.store.get_session(self.session['id'])
        with self.assertRaises(LibraryError) as caught:
            self.review(version=version)
        self.assertEqual(caught.exception.code, 'conflict')
        self.assertEqual(self.store.get_session(self.session['id']), before)

    def test_late_cancellation_returns_committed_review_snapshot(self):
        stopped = []
        result = self.review(progress=lambda number, message: stopped.append(True) if number == 80 else None,
                             cancelled=lambda: bool(stopped))
        self.assertTrue(self.state(result['data'])['reviewed'])

    def test_initial_cancellation_does_not_write_editor_state(self):
        before = self.store.get_session(self.session['id'])
        with self.assertRaisesRegex(ValueError, '已取消'):
            self.review(cancelled=lambda: True)
        self.assertEqual(self.store.get_session(self.session['id']), before)

    def low_confidence(self):
        rows = self.store.list_candidates(self.production)
        for row in rows:
            self.store.update_candidate(row['id'], 'candidate', tags=['auto_dialogue'],
                                        expected_revision=row['revision'])
        self.store.replace_candidates(self.production, [dict(transcript_id=row['transcript_id'],
            script_line_id=row['script_line_id'], score=.31) for row in rows])
        return next(row for row in self.store.list_candidates(self.production)
                    if row['session_id'] == self.session['id'] and row['script_line_id'] == self.lines[0]['id'])

    def baseline(self, *, repeated=False):
        path = '/NAS/day02/PGM002.mov'
        session = self.store.save_session(self.production, 'day02_通条', dict(pgm=path,
            recording_kind='continuous', library_media=[dict(kind='pgm', path=path, fps=25, frames=2500)]))
        segments = [dict(start_seconds=1, end_seconds=3, text='第一句台词')]
        if repeated:
            segments.append(dict(start_seconds=20, end_seconds=22, text='第一句台词重复'))
        speech = self.store.add_transcript(session['id'], path, segments)
        self.store.replace_candidates(self.production, [dict(transcript_id=identity,
            script_line_id=self.lines[0]['id'], score=.31) for identity in speech['transcript_ids']])
        version = next(row for row in self.data()['pgm_match_versions'] if row['session_id'] == session['id'])
        self.workflow.mark_pgm_reviewed(session['id'], path, expected_match_version=version)
        rows = sorted((row for row in self.store.list_candidates(self.production)
                       if row['session_id'] == session['id']), key=lambda row: row['start_seconds'])
        return session, rows

    def test_reference_store_accepts_fingerprinted_review_without_mutating_low_score_candidates(self):
        from reference_workflow import ReferenceWorkflow
        chosen = self.low_confidence()
        with self.assertRaisesRegex(LibraryError, '唯一首句'):
            self.store.update_reference_settings(self.production, None, {chosen['id']: None})
        self.review()
        session, rows = self.baseline()
        before = self.store.list_candidates(self.production)
        reference = ReferenceWorkflow(LocalClient(self.store), self.production, '导演')
        reference.set_reference(session['id'])
        result = reference.select_clip(chosen['id'], True, rows[0]['id'])
        self.assertEqual(result['state']['selected_clip_pairs'], {chosen['id']: rows[0]['id']})
        self.assertEqual(self.store.list_candidates(self.production), before)
        self.assertTrue(all(row['score'] == .31 and row['status'] == 'candidate' for row in before))

    def test_reviewed_baseline_can_use_explicit_later_same_line_utterance(self):
        chosen = self.low_confidence()
        self.review()
        session, rows = self.baseline(repeated=True)
        state = self.store.update_reference_settings(self.production, session['id'],
            {chosen['id']: rows[1]['id']})
        self.assertEqual(state['selected_clip_pairs'], {chosen['id']: rows[1]['id']})
        self.assertEqual(rows[1]['start_seconds'], 20)

    def test_reference_store_rejects_removed_even_after_new_high_confidence_auto_match(self):
        self.remove()
        speech = self.store.add_transcript(self.session['id'], self.path,
            [dict(start_seconds=.5, end_seconds=1, text='新识别首句')])
        self.store.replace_candidates(self.production, [dict(transcript_id=speech['transcript_ids'][0],
            script_line_id=self.lines[0]['id'], score=.99)])
        chosen = next(row for row in self.store.list_candidates(self.production)
                      if row['transcript_id'] == speech['transcript_ids'][0])
        before = self.store.get_reference_settings(self.production)
        with self.assertRaisesRegex(LibraryError, '轨道移出'):
            self.store.update_reference_settings(self.production, None, {chosen['id']: None},
                                                  expected_revision=before['revision'])
        self.assertEqual(self.store.get_reference_settings(self.production), before)

    def test_reference_store_review_does_not_invent_ambiguous_or_missing_first_anchor(self):
        chosen = self.low_confidence()
        self.store.replace_candidates(self.production, [dict(transcript_id=chosen['transcript_id'],
            script_line_id=self.lines[5]['id'], score=.31)])
        self.review()
        with self.assertRaisesRegex(LibraryError, '唯一首句'):
            self.store.update_reference_settings(self.production, None, {chosen['id']: None})

    def test_reference_store_rejects_stale_review_after_new_first_utterance(self):
        chosen = self.low_confidence()
        self.review()
        self.store.add_transcript(self.session['id'], self.path,
                                 [dict(start_seconds=.5, end_seconds=1, text='尚未匹配的开头')])
        with self.assertRaisesRegex(LibraryError, '唯一首句'):
            self.store.update_reference_settings(self.production, None, {chosen['id']: None})

    def test_mark_review_over_real_http_preserves_source_metadata(self):
        from library_service import Client, LibraryServer
        server = LibraryServer(('127.0.0.1', 0), self.store, 'editor-state-token-with-at-least-24-chars')
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        try:
            workflow = BatchWorkflow(Client('http://127.0.0.1:'+str(server.server_port),
                server.token, '导演'), self.production, '导演')
            result = workflow.mark_pgm_reviewed(self.session['id'], self.path,
                                               expected_match_version=self.version())
            self.assertTrue(self.state(result['data'])['reviewed'])
            self.assertEqual(result['editor_state']['updated_by'], '导演')
            self.assertEqual(result['session']['config']['library_sync'], self.config['library_sync'])
        finally:
            server.shutdown(); server.server_close(); thread.join(3)


if __name__ == '__main__': unittest.main()
