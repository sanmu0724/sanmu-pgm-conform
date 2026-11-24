"""Whole-file match removal retains sources and uses a multi-record CAS."""
import concurrent.futures
import copy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from library_store import LibraryError, Store
from library_workflow import BatchWorkflow
from pgm_takes import PgmTakeIndex


class Client:
    actor = '场记'

    def __init__(self, store):
        self.store = store

    def call(self, method, **params):
        return self.store.call(method, params, self.actor)


class ClearPgmTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.folder.name)/'catalogue.db')
        self.production = self.store.create_production('全片错配移除')['id']
        self.store.put_script(self.production, '台本', [dict(text='今天你为何来这里？'), dict(text='明天一起去公园。')])
        self.lines = self.store.list_script(self.production)
        self.sessions, self.paths, self.candidates, self.transcripts = {}, {}, {}, {}
        for name in ('base', 'wrong', 'other'):
            path = '/NAS/'+name+'/PGM001.mov'
            config = dict(pgm=path, fps=25, pgm_timecodes={path: '01:00:00:00'},
                          library_media=[dict(kind='pgm', path=path, fps=25, frames=250,
                                              start_tc='01:00:00:00')])
            session = self.store.save_session(self.production, name, config)
            self.sessions[name], self.paths[name] = session, path
            speech = self.store.add_transcript(session['id'], path, [
                dict(start_seconds=1, end_seconds=3, text='今天你为何来这里？'),
                dict(start_seconds=4, end_seconds=8, text='明天一起去公园。')],
                metadata=dict(fps=25, reel='ORIGINAL', preserved='media'))
            self.transcripts[name] = speech['segments']
            self.store.replace_candidates(self.production, [dict(script_line_id=line['id'],
                transcript_id=source['id'], score=.9, reason='识别') for line, source in zip(self.lines, speech['segments'])])
            rows = [row for row in self.store.list_candidates(self.production) if row['session_id'] == session['id']]
            self.candidates[name] = [self.store.update_candidate(row['id'], 'confirmed', note='保留备注',
                tags=['auto_dialogue'], expected_revision=row['revision']) for row in rows]
        self.workflow = BatchWorkflow(Client(self.store), self.production)

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def version(self, name):
        return next(row for row in self.workflow.read_all()['pgm_match_versions']
                    if row['session_id'] == self.sessions[name]['id'] and row['media_path'] == self.paths[name])

    def remove(self, name='wrong', version=None):
        version = version or self.version(name)
        return self.workflow.clear_pgm_matches(self.sessions[name]['id'], self.paths[name],
            expected_session_revision=version['session_revision'],
            expected_candidate_revisions=version['candidate_revisions'],
            expected_reference_revision=version['reference_revision'])

    def select(self, pairs, reference='base'):
        state = self.store.get_reference_settings(self.production)
        return self.store.update_reference_settings(self.production, self.sessions[reference]['id'],
            pairs, dict(valid=True, fingerprint='test'), expected_revision=state['revision'])

    def test_whole_file_removed_without_deleting_sources_or_other_day(self):
        wrong, base, other = (self.candidates[key][0]['id'] for key in ('wrong', 'base', 'other'))
        self.select({wrong: base, other: base})
        before = self.store.list_transcripts(self.production)
        result = self.remove()
        self.assertEqual(result['cleared_count'], 2)
        self.assertEqual(result['removed_choices'], 1)
        self.assertEqual(result['data']['reference_state']['selected_clip_pairs'], {other: base})
        self.assertTrue(result['reference_state']['baseline_export']['valid'])
        self.assertEqual(self.store.list_transcripts(self.production), before)
        current_session = self.store.get_session(self.sessions['wrong']['id'])
        current_config = copy.deepcopy(current_session['config'])
        self.assertEqual(current_config.pop('library_pgm_editor')[self.paths['wrong']]['pool'], 'removed')
        self.assertEqual(current_config, self.sessions['wrong']['config'])
        self.assertEqual(current_session['revision'], self.sessions['wrong']['revision']+1)
        rows = self.store.list_candidates(self.production)
        affected = [row for row in rows if row['session_id'] == self.sessions['wrong']['id']]
        self.assertEqual({row['status'] for row in affected}, {'rejected'})
        self.assertTrue(all(row['note'] == '保留备注' and 'pgm_match_removed' in row['tags'] for row in affected))
        self.assertEqual({row['status'] for row in rows if row['session_id'] != self.sessions['wrong']['id']}, {'confirmed'})
        self.assertEqual(PgmTakeIndex(result['data'], prepared=True).file_summary(affected[0])['match_status'], 'unmatched')
        self.assertTrue(any(row['media_path'] == self.paths['wrong'] for row in result['data']['unmatched_transcripts']))

    def test_removing_baseline_hides_alternative_placement_and_preserves_anchor_evidence(self):
        base = self.candidates['base'][0]['id']
        selected = self.candidates['other'][0]['id']
        self.select({selected: base})
        candidates = self.store.list_candidates(self.production)
        transcripts = self.store.list_transcripts(self.production)
        with patch('dialogue_engine.transcribe_media', side_effect=AssertionError('no new ASR')), \
             patch('library_sync.ensure_synced_pgm', side_effect=AssertionError('no new sync')):
            result = self.remove('base')
        state = result['reference_state']
        self.assertEqual(state['selected_clip_pairs'], {selected: base})
        self.assertEqual(result['cleared_count'], 0)
        self.assertEqual(result['reset_anchors'], 0)
        self.assertTrue(state['baseline_export']['valid'])
        self.assertEqual(result['editor_state']['pool'], 'removed')
        self.assertEqual(self.store.list_candidates(self.production), candidates)
        self.assertEqual(self.store.list_transcripts(self.production), transcripts)

    def test_hidden_history_and_manual_visual_are_in_revision_inventory_and_removed(self):
        session, path = self.sessions['wrong'], self.paths['wrong']
        visual = self.store.add_transcript(session['id'], path, [dict(start_seconds=0, end_seconds=10,
            text='[人工画面归类]', source_kind='manual_visual', classification_only=True)])
        self.store.replace_candidates(self.production, [dict(script_line_id=self.lines[0]['id'],
            transcript_id=visual['transcript_ids'][0], score=1, reason='人工')])
        row = next(row for row in self.store.list_candidates(self.production) if row['transcript_id'] == visual['transcript_ids'][0])
        self.store.update_candidate(row['id'], 'confirmed', tags=['人工画面归类'], expected_revision=row['revision'])
        current = self.store.get_session(session['id'])
        config = copy.deepcopy(current['config'])
        config['library_asr'] = {path: dict(source_kind='manual', active_transcript_ids=[self.transcripts['wrong'][1]['id']])}
        self.store.save_session(self.production, current['name'], config, session_id=current['id'], expected_revision=current['revision'])
        data = self.workflow.read_all()
        self.assertLess(len([row for row in data['candidates'] if row['session_id'] == session['id']]), 3)
        version = self.version('wrong')
        self.assertEqual(len(version['candidate_revisions']), 3)
        result = self.remove(version=version)
        self.assertEqual(result['cleared_count'], 3)
        self.assertEqual(len(self.store.list_transcripts(self.production, session['id'])), 3)
        raw = [row for row in self.store.list_candidates(self.production) if row['session_id'] == session['id']]
        self.assertEqual({row['status'] for row in raw}, {'rejected'})
        reassigned = self.workflow.associate_pending_interval(session['id'], path, 0, 10, self.lines[1]['id'])
        self.assertEqual(reassigned['candidate']['status'], 'confirmed')
        self.assertEqual(reassigned['candidate']['script_line_id'], self.lines[1]['id'])
        self.assertEqual(len(self.store.list_transcripts(self.production, session['id'])), 3)

    def test_stale_candidate_session_reference_and_new_association_roll_back(self):
        for change in ('candidate', 'session', 'reference', 'new_candidate'):
            with self.subTest(change=change):
                version = self.version('wrong')
                if change == 'candidate':
                    row = next(row for row in self.store.list_candidates(self.production)
                               if row['id'] in version['candidate_revisions'])
                    self.store.update_candidate(row['id'], 'confirmed', note='并发核查', expected_revision=row['revision'])
                elif change == 'session':
                    session = self.store.get_session(self.sessions['wrong']['id'])
                    self.store.save_session(self.production, session['name'], session['config'], session_id=session['id'],
                                            expected_revision=session['revision'])
                elif change == 'reference':
                    self.select({self.candidates['other'][0]['id']: self.candidates['base'][0]['id']})
                else:
                    self.store.replace_candidates(self.production, [dict(script_line_id=self.lines[1]['id'],
                        transcript_id=self.transcripts['wrong'][0]['id'], score=.8)])
                before = self.store.list_candidates(self.production), self.store.get_reference_settings(self.production)
                with self.assertRaises(LibraryError) as caught:
                    self.remove(version=version)
                self.assertEqual(caught.exception.code, 'conflict')
                self.assertEqual(before, (self.store.list_candidates(self.production), self.store.get_reference_settings(self.production)))
        self.assertFalse(any(row['action'] == 'clear_pgm_matches' for row in self.store.audit(self.production)))

    def test_same_basename_wrong_session_or_foreign_production_never_aliases(self):
        version = self.version('wrong')
        params = dict(production_id=self.production, session_id=self.sessions['wrong']['id'],
            media_path=self.paths['base'], expected_session_revision=version['session_revision'],
            expected_candidate_revisions=version['candidate_revisions'], expected_reference_revision=version['reference_revision'])
        before = self.store.list_candidates(self.production)
        with self.assertRaises(LibraryError) as caught:
            self.store.clear_pgm_matches(**params)
        self.assertEqual(caught.exception.code, 'not_found')
        params['media_path'] = 'PGM001.mov'
        with self.assertRaises(LibraryError):
            self.store.clear_pgm_matches(**params)
        params['production_id'] = self.store.create_production('另一部剧')['id']
        with self.assertRaises(LibraryError) as caught:
            self.store.clear_pgm_matches(**params)
        self.assertEqual(caught.exception.code, 'invalid')
        self.assertEqual(self.store.list_candidates(self.production), before)

    def test_concurrent_removal_has_one_commit(self):
        version = self.version('wrong')
        barrier = threading.Barrier(2)
        def remove(_):
            barrier.wait()
            try:
                return self.remove(version=version)['cleared_count']
            except LibraryError as exc:
                return exc.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(remove, range(2)))
        self.assertCountEqual(result, [2, 'conflict'])
        self.assertEqual(sum(row['action'] == 'clear_pgm_matches' for row in self.store.audit(self.production)), 1)

    def test_rerun_preserves_rejected_relationship_but_manual_reclassification_possible(self):
        result = self.remove()
        rows = [row for row in self.store.list_candidates(self.production) if row['session_id'] == self.sessions['wrong']['id']]
        self.store.replace_candidates(self.production, [dict(script_line_id=row['script_line_id'],
            transcript_id=row['transcript_id'], score=1., reason='再次自动匹配') for row in rows])
        self.assertEqual({row['status'] for row in self.store.list_candidates(self.production)
                          if row['session_id'] == self.sessions['wrong']['id']}, {'rejected'})
        first = rows[0]
        self.store.update_candidate(first['id'], 'confirmed', tags=['manual_pgm_confirmed'], expected_revision=first['revision'])
        data = self.workflow.read_all()
        self.assertEqual(PgmTakeIndex(data, prepared=True).file_summary(first)['match_status'], 'matched')
        self.assertNotEqual(result['data']['pgm_match_versions'], data['pgm_match_versions'])

    def test_missing_or_malformed_versions_do_not_write(self):
        v = self.version('wrong')
        for revisions in (None, [], {next(iter(v['candidate_revisions'])): True}, {}):
            with self.subTest(revisions=revisions), self.assertRaises(LibraryError):
                self.store.clear_pgm_matches(self.production, self.sessions['wrong']['id'], self.paths['wrong'],
                    expected_session_revision=v['session_revision'], expected_candidate_revisions=revisions,
                    expected_reference_revision=v['reference_revision'])
        self.assertEqual({row['status'] for row in self.store.list_candidates(self.production)}, {'confirmed'})

    def test_cancellation_before_commit_preserves_matches(self):
        v = self.version('wrong')
        with self.assertRaisesRegex(ValueError, '已取消'):
            self.workflow.clear_pgm_matches(self.sessions['wrong']['id'], self.paths['wrong'],
                expected_session_revision=v['session_revision'], expected_candidate_revisions=v['candidate_revisions'],
                expected_reference_revision=v['reference_revision'], cancelled=lambda: True)
        self.assertEqual({row['status'] for row in self.store.list_candidates(self.production)}, {'confirmed'})

    def test_late_cancellation_still_returns_committed_fresh_snapshot(self):
        v = self.version('wrong')
        stopped = []
        def progress(percent, _message):
            if percent == 80:
                stopped.append(True)
        result = self.workflow.clear_pgm_matches(self.sessions['wrong']['id'], self.paths['wrong'],
            expected_session_revision=v['session_revision'], expected_candidate_revisions=v['candidate_revisions'],
            expected_reference_revision=v['reference_revision'], cancelled=lambda: bool(stopped), progress=progress)
        self.assertTrue(stopped)
        self.assertEqual(result['cleared_count'], 2)
        self.assertEqual({row['status'] for row in result['data']['candidates']
                          if row['session_id'] == self.sessions['wrong']['id']}, {'rejected'})


if __name__ == '__main__':
    unittest.main()
