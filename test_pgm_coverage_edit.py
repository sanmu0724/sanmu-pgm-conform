"""Script-lane range corrections preserve source clocks and selected takes."""
import concurrent.futures
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest

from library_store import LibraryError, Store
from library_workflow import BatchWorkflow
from pgm_takes import PgmTakeIndex


class LocalClient:
    actor = '场记'
    def __init__(self, store): self.store = store
    def call(self, method, **params): return self.store.call(method, params, self.actor)


class PgmCoverageEditTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.folder.name) / 'catalogue.db')
        self.production = self.store.create_production('范围修正')['id']
        self.store.put_script(self.production, '全剧', [dict(text='台词第'+str(i)+'句', order=i,
            scene='第一幕' if i < 50 else '第二幕') for i in range(1, 101)])
        self.lines = self.store.list_script(self.production)
        self.path = '/NAS/day01/PIX_001_046.mov'
        self.config = dict(pgm=self.path, pgm_timecodes={self.path: '21:00:00:00'},
            library_media=[dict(kind='pgm', path=self.path, fps=25, frames=2500)],
            sound_sync={'placements': [{'path': '/NAS/sound/original.wav', 'source_timecode': '21:00:03:12'}]},
            associations={'proxy': '/NAS/proxy/A.mov', 'original': '/NAS/original/A.mxf'})
        self.session = self.store.save_session(self.production, 'day01_分镜头', self.config)
        self.workflow = BatchWorkflow(LocalClient(self.store), self.production)
        self.initial = self.workflow.associate_pending_interval(self.session['id'], self.path, 7.1, 100,
            self.lines[54]['id'], script_line_ids=[row['id'] for row in self.lines[54:76]])

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def version(self, session_id=None, path=None):
        return next(row for row in self.workflow.read_all()['pgm_match_versions'] if
                    row['session_id'] == (session_id or self.session['id']) and row['media_path'] == (path or self.path))

    def edit(self, first=55, last=80, version=None, **kwargs):
        return self.workflow.update_pgm_script_coverage(self.session['id'], self.path,
            [row['id'] for row in self.lines[first-1:last]], expected_match_version=version or self.version(), **kwargs)

    def baseline(self, first=55):
        path = '/NAS/day02/continuous.mov'
        session = self.store.save_session(self.production, 'day02_通条', dict(pgm=path, recording_kind='continuous',
            library_media=[dict(kind='pgm', path=path, fps=25, frames=2500)]))
        result = self.workflow.associate_pending_interval(session['id'], path, 10.2, 100,
            self.lines[first-1]['id'], script_line_ids=[self.lines[first-1]['id']])
        return session, path, result['candidate']

    def choose(self, reference, pairs, baseline=None):
        state = self.store.get_reference_settings(self.production)
        return self.store.update_reference_settings(self.production, reference, pairs,
            baseline_export=baseline or {}, expected_revision=state['revision'])

    def test_drag_range_preserves_local_first_position_complete_media_and_sound_metadata(self):
        source_before = self.initial['transcript']['segments'][0]
        config_before = self.store.get_session(self.session['id'])['config']
        result = self.edit(40, 80)
        source = result['transcript']['segments'][0]
        self.assertTrue(result['source_anchor_preserved'])
        self.assertEqual((source['start_seconds'], source['end_seconds']), (7.1, 100))
        self.assertEqual(result['candidate']['script_line_id'], self.lines[39]['id'])
        self.assertEqual(source['data']['manual_script_line_ids'], [row['id'] for row in self.lines[39:80]])
        self.assertEqual(source['id'], source_before['id'])
        current_config = copy.deepcopy(self.store.get_session(self.session['id'])['config'])
        self.assertTrue(current_config.pop('library_pgm_editor')[self.path]['reviewed'])
        config_before.pop('library_pgm_editor', None)
        self.assertEqual(current_config, config_before)
        index = PgmTakeIndex(result['data'], prepared=True)
        self.assertEqual(float(index.duration(result['candidate'])), 100)
        node = next(row for row in result['data']['pgm_interval_nodes'] if [self.session['id'], self.path] in row['file_keys'])
        clip = node['clips'][0]
        self.assertEqual((clip['start_seconds'], clip['end_seconds'], clip['dialogue_start_seconds']), (0, 100, 7.1))
        self.assertEqual((node['coverage_start'], node['coverage_end']), (39, 79))

    def test_same_first_line_retains_selection_and_compatible_baseline_occurrence(self):
        session, _, anchor = self.baseline()
        selected = self.initial['candidate']
        self.choose(session['id'], {selected['id']: anchor['id']})
        result = self.edit(55, 80)
        self.assertTrue(result['selection_preserved'])
        self.assertEqual(result['reset_anchors'], 0)
        self.assertEqual(result['data']['reference_state']['selected_clip_pairs'], {selected['id']: anchor['id']})

    def test_changed_first_line_migrates_selection_but_clears_incompatible_anchor(self):
        session, _, anchor = self.baseline()
        selected = self.initial['candidate']
        self.choose(session['id'], {selected['id']: anchor['id']})
        result = self.edit(40, 80)
        self.assertNotEqual(result['candidate']['id'], selected['id'])
        self.assertTrue(result['selection_preserved'])
        self.assertEqual(result['reset_anchors'], 1)
        self.assertEqual(result['data']['reference_state']['selected_clip_pairs'], {result['candidate']['id']: None})

    def test_unselected_range_does_not_automatically_join_alternatives(self):
        result = self.edit(40, 80)
        self.assertFalse(result['selection_preserved'])
        self.assertEqual(result['data']['reference_state']['selected_clip_pairs'], {})

    def test_baseline_range_change_preserves_related_take_choice_but_invalidates_export(self):
        session, path, anchor = self.baseline()
        selected = self.initial['candidate']
        self.choose(session['id'], {selected['id']: anchor['id']}, baseline=dict(valid=True, path='/NAS/base.fcpxml'))
        result = self.workflow.update_pgm_script_coverage(session['id'], path,
            [row['id'] for row in self.lines[39:80]], expected_match_version=self.version(session['id'], path))
        state = result['data']['reference_state']
        self.assertEqual(state['selected_clip_pairs'], {selected['id']: None})
        self.assertFalse(state['baseline_export']['valid'])
        self.assertEqual(result['transcript']['segments'][0]['start_seconds'], 10.2)

    def test_unchanged_baseline_first_line_preserves_dependent_anchor(self):
        session, path, anchor = self.baseline()
        selected = self.initial['candidate']
        self.choose(session['id'], {selected['id']: anchor['id']})
        result = self.workflow.update_pgm_script_coverage(session['id'], path,
            [row['id'] for row in self.lines[54:80]], expected_match_version=self.version(session['id'], path))
        self.assertEqual(result['data']['reference_state']['selected_clip_pairs'], {selected['id']: anchor['id']})

    def test_retargeting_via_manual_dialog_reuses_same_transaction_and_migrates_selection(self):
        session, _, anchor = self.baseline()
        selected = self.initial['candidate']
        self.choose(session['id'], {selected['id']: anchor['id']})
        result = self.workflow.associate_pending_interval(self.session['id'], self.path, 8.2, 100,
            self.lines[39]['id'], script_line_ids=[row['id'] for row in self.lines[39:80]],
            expected_match_version=self.version())
        self.assertEqual(result['transcript']['segments'][0]['start_seconds'], 8.2)
        self.assertEqual(result['data']['reference_state']['selected_clip_pairs'], {result['candidate']['id']: None})
        self.assertEqual(result['retired_manual_count'], 1)

    def test_cross_scene_in_one_script_is_allowed_and_single_line_range_is_allowed(self):
        self.edit(40, 60)
        result = self.edit(55, 55)
        self.assertEqual(result['transcript']['segments'][0]['data']['manual_script_line_ids'], [self.lines[54]['id']])
        self.assertEqual(result['transcript']['segments'][0]['start_seconds'], 7.1)

    def test_all_version_fields_guard_edit_without_partial_writes(self):
        before = (self.store.list_transcripts(self.production), self.store.list_candidates(self.production),
                  self.store.get_reference_settings(self.production))
        for field in ('session_revision', 'reference_revision', 'candidate_revisions'):
            version = copy.deepcopy(self.version())
            if field == 'candidate_revisions': version[field] = {}
            else: version[field] += 1
            with self.subTest(field=field), self.assertRaises(LibraryError) as caught:
                self.edit(40, 80, version=version)
            self.assertEqual(caught.exception.code, 'conflict')
        self.assertEqual(before, (self.store.list_transcripts(self.production), self.store.list_candidates(self.production),
                                  self.store.get_reference_settings(self.production)))

    def test_noncontiguous_or_cross_script_ranges_cannot_change_selection(self):
        session, _, anchor = self.baseline()
        selected = self.initial['candidate']
        self.choose(session['id'], {selected['id']: anchor['id']})
        self.store.put_script(self.production, '其他台本', [dict(text='其他')])
        other = next(row for row in self.store.list_script(self.production) if row['title'] == '其他台本')
        before = self.store.list_transcripts(self.production), self.store.get_reference_settings(self.production)
        for ids in ([self.lines[39]['id'], self.lines[41]['id']], [self.lines[39]['id'], other['id']]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.workflow.update_pgm_script_coverage(self.session['id'], self.path, ids, expected_match_version=self.version())
        self.assertEqual(before, (self.store.list_transcripts(self.production), self.store.get_reference_settings(self.production)))

    def test_short_clip_does_not_require_a_long_speech_span(self):
        path = '/NAS/day01/short.mov'
        session = self.store.save_session(self.production, '短片', dict(pgm=path,
            library_media=[dict(kind='pgm', path=path, fps=25, frames=1)]))
        self.workflow.associate_pending_interval(session['id'], path, .01, .04,
            self.lines[54]['id'], script_line_ids=[self.lines[54]['id']])
        result = self.workflow.update_pgm_script_coverage(session['id'], path, [self.lines[55]['id']],
            expected_match_version=self.version(session['id'], path))
        self.assertEqual((result['transcript']['segments'][0]['start_seconds'], result['transcript']['segments'][0]['end_seconds']), (.01, .04))
        self.assertEqual(float(PgmTakeIndex(result['data'], prepared=True).duration(result['candidate'])), .04)

    def test_concurrent_drag_edits_have_one_atomic_winner(self):
        version = self.version()
        barrier = threading.Barrier(2)
        def edit(first):
            barrier.wait()
            try: return self.edit(first, first+2, version=version)['candidate']['id']
            except LibraryError as exc: return exc.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(edit, (40, 60)))
        self.assertEqual(results.count('conflict'), 1)
        self.assertEqual(len([row for row in self.store.list_candidates(self.production) if row['status'] == 'confirmed']), 1)

    def test_edit_removed_file_requires_new_manual_anchor(self):
        version = self.version()
        self.workflow.clear_pgm_matches(self.session['id'], self.path,
            expected_session_revision=version['session_revision'],
            expected_candidate_revisions=version['candidate_revisions'], expected_reference_revision=version['reference_revision'])
        before = self.store.list_candidates(self.production)
        with self.assertRaisesRegex(ValueError, '人工定位'):
            self.edit(40, 80)
        self.assertEqual(self.store.list_candidates(self.production), before)

    def test_automatic_first_sentence_keeps_real_asr_span_and_original_asr_metadata(self):
        path = '/NAS/day03/auto.mov'
        session = self.store.save_session(self.production, 'day03_分镜头', dict(pgm=path,
            library_media=[dict(kind='pgm', path=path, fps=25, frames=2500)]))
        speech = self.store.add_transcript(session['id'], path, [dict(start_seconds=7.1, end_seconds=9.2,
            text='台词第55句', sound_filename='original.wav', audio_source_local_seconds=8.7)],
            metadata=dict(original_record_date='2026-10-09', sound_roll='261009_TIoBE'))
        self.store.replace_candidates(self.production, [dict(transcript_id=speech['transcript_ids'][0],
            script_line_id=self.lines[54]['id'], score=.95)])
        before = self.store.list_transcripts(self.production, session_id=session['id'])
        result = self.workflow.update_pgm_script_coverage(session['id'], path,
            [row['id'] for row in self.lines[39:80]], expected_match_version=self.version(session['id'], path))
        self.assertEqual((result['transcript']['segments'][0]['start_seconds'], result['transcript']['segments'][0]['end_seconds']), (7.1, 9.2))
        original = next(row for row in self.store.list_transcripts(self.production, session_id=session['id']) if row['id'] == speech['transcript_ids'][0])
        self.assertEqual(original, before[0])

    def test_api_six_supports_range_correction_over_real_http(self):
        from library_service import Client, LibraryServer
        token = 'range-edit-token-with-at-least-24-characters'
        server = LibraryServer(('127.0.0.1', 0), self.store, token)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        try:
            workflow = BatchWorkflow(Client('http://127.0.0.1:'+str(server.server_port), token, '场记'), self.production)
            result = workflow.update_pgm_script_coverage(self.session['id'], self.path,
                [row['id'] for row in self.lines[39:80]], expected_match_version=self.version())
            self.assertEqual(result['candidate']['script_line_id'], self.lines[39]['id'])
            self.assertEqual(result['transcript']['segments'][0]['start_seconds'], 7.1)
            self.assertEqual(result['reference_state']['revision'], result['data']['reference_state']['revision'])
            with self.assertRaises(LibraryError):
                workflow.update_pgm_script_coverage(self.session['id'], self.path,
                    [row['id'] for row in self.lines[39:80]], expected_match_version=self.version() | {'reference_revision': 0})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_full_candidate_inventory_guards_hidden_second_association(self):
        speech = self.store.add_transcript(self.session['id'], self.path,
            [dict(start_seconds=12, end_seconds=14, text='台词第56句')])
        self.store.replace_candidates(self.production, [dict(transcript_id=speech['transcript_ids'][0],
            script_line_id=self.lines[55]['id'], score=.95)])
        version = self.version()
        second = next(row for row in self.store.list_candidates(self.production) if row['transcript_id'] == speech['transcript_ids'][0])
        self.store.update_candidate(second['id'], 'confirmed', note='另一用户核对', expected_revision=second['revision'])
        before = self.store.list_candidates(self.production), self.store.list_transcripts(self.production)
        with self.assertRaises(LibraryError) as caught:
            self.edit(40, 80, version=version)
        self.assertEqual(caught.exception.code, 'conflict')
        self.assertEqual(before, (self.store.list_candidates(self.production), self.store.list_transcripts(self.production)))

    def test_same_filename_in_another_session_is_not_reassigned_or_unselected(self):
        session, _, anchor = self.baseline()
        path = '/NAS/day03/PIX_001_046.mov'
        other_session = self.store.save_session(self.production, 'day03_分镜头', dict(pgm=path,
            library_media=[dict(kind='pgm', path=path, fps=25, frames=2500)]))
        other = self.workflow.associate_pending_interval(other_session['id'], path, 2.7, 100,
            self.lines[54]['id'], script_line_ids=[self.lines[54]['id']])['candidate']
        self.choose(session['id'], {self.initial['candidate']['id']: anchor['id'], other['id']: anchor['id']})
        other_source = next(row for row in self.store.list_transcripts(self.production) if row['id'] == other['transcript_id'])
        result = self.edit(40, 80)
        self.assertEqual(result['data']['reference_state']['selected_clip_pairs'],
                         {result['candidate']['id']: None, other['id']: anchor['id']})
        self.assertEqual(next(row for row in self.store.list_transcripts(self.production) if row['id'] == other['transcript_id']), other_source)

    def test_duplicate_historical_sentence_choices_collapse_to_one_complete_pgm(self):
        session, _, anchor = self.baseline()
        speech = self.store.add_transcript(self.session['id'], self.path,
            [dict(start_seconds=12, end_seconds=14, text='台词第55句')])
        self.store.replace_candidates(self.production, [dict(transcript_id=speech['transcript_ids'][0],
            script_line_id=self.lines[54]['id'], score=.95)])
        second = next(row for row in self.store.list_candidates(self.production) if row['transcript_id'] == speech['transcript_ids'][0])
        second = self.store.update_candidate(second['id'], 'confirmed', expected_revision=second['revision'])
        state = self.choose(session['id'], {self.initial['candidate']['id']: anchor['id']})
        # Simulate a pre-whole-PGM persisted choice. New writes intentionally
        # reject later-sentence IDs, but range correction must still migrate it.
        with self.store._connection(True) as con:
            legacy = dict(selected_clip_pairs={self.initial['candidate']['id']: anchor['id'],
                second['id']: anchor['id']}, baseline_export=state['baseline_export'])
            con.execute('UPDATE reference_settings SET settings=? WHERE production_id=?',
                        (json.dumps(legacy), self.production))
        result = self.edit(55, 80)
        self.assertEqual(result['data']['reference_state']['selected_clip_pairs'], {result['candidate']['id']: anchor['id']})

    def test_corrected_export_uses_new_script_line_with_unchanged_source_anchor_and_full_duration(self):
        from reference_timeline import build_reference_plan
        session, path, anchor = self.baseline(first=40)
        self.workflow.associate_pending_interval(session['id'], path, 10.2, 100,
            self.lines[39]['id'], script_line_ids=[row['id'] for row in self.lines[39:80]])
        speech = self.store.add_transcript(session['id'], path,
            [dict(start_seconds=30.2, end_seconds=32.2, text='台词第55句')])
        self.store.replace_candidates(self.production, [dict(transcript_id=speech['transcript_ids'][0],
            script_line_id=self.lines[54]['id'], score=.95)])
        spoken = next(row for row in self.store.list_candidates(self.production) if row['transcript_id'] == speech['transcript_ids'][0])
        spoken = self.store.update_candidate(spoken['id'], 'confirmed', expected_revision=spoken['revision'])
        self.choose(session['id'], {self.initial['candidate']['id']: spoken['id']})
        result = self.edit(40, 80)
        cuts = {session_id: dict(fps=25, duration=2500, pgm_sources=[dict(path=media_path,
                    start=0, end=2500, frames=2500, start_tc=tc)]) for session_id, media_path, tc in
                ((self.session['id'], self.path, '21:00:00:00'), (session['id'], path, '23:12:00:00'))}
        selected = [dict(candidate_id=identity, baseline_candidate_id=baseline)
                    for identity, baseline in result['data']['reference_state']['selected_clip_pairs'].items()]
        row = build_reference_plan(result['data'], session['id'], selected, cuts=cuts)['selections'][0]
        self.assertEqual(row['script_line_id'], self.lines[39]['id'])
        self.assertEqual(row['baseline_candidate_id'], anchor['id'])
        self.assertEqual(row['dialogue_pgm_frame_in'], 177)
        self.assertEqual(row['target_frame_in'], 78)
        self.assertEqual((row['pgm_frame_in'], row['pgm_frame_out_exclusive'], row['duration_frames']), (0, 2500, 2500))
        self.assertEqual(cuts[self.session['id']]['pgm_sources'][0]['start_tc'], '21:00:00:00')


if __name__ == '__main__': unittest.main()
