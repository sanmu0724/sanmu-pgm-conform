"""Scoped rematching retires stale machine guesses while preserving humans."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from library_store import LibraryError, Store
from library_workflow import BatchWorkflow
from pgm_editor_state import coverage_fingerprint
from pgm_takes import PgmTakeIndex
from reference_workflow import session_fingerprint
from dialogue_spans import encode_span_tags, parse_span_tags


AUTO_NOTE = '台词匹配度高于60%自动通过；同场同区间机位一并归类。'
LEGACY_AUTO_NOTE = '高台词吻合自动通过：原始声音完整覆盖、无近似剧情竞争；同场同区间机位一并归类。'


class Client:
    actor = '自动匹配'
    def __init__(self, store): self.store = store
    def call(self, method, **params): return self.store.call(method, params, self.actor)


class MatchRefreshTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.folder.name)/'catalogue.db')
        self.production = self.store.create_production('自动对应更新')['id']
        self.store.put_script(self.production, '台本', [dict(text=text) for text in
                              ('今天你为何来这里？', '明天一起去公园。', '这些钱还给你。')])
        self.lines = self.store.list_script(self.production)
        self.sessions, self.sources = {}, {}
        for name in ('base', 'alt'):
            path = '/NAS/'+name+'/PGM001.mov'
            self.sessions[name] = self.store.save_session(self.production, name,
                dict(pgm=path, library_media=[dict(path=path, kind='pgm', fps=25, frames=250)]))
            self.sources[name] = self.store.add_transcript(self.sessions[name]['id'], path,
                [dict(start_seconds=1, end_seconds=3, text='今天你为何来这里？'),
                 dict(start_seconds=4, end_seconds=7, text='明天一起去公园。')])['segments']
        self.workflow = BatchWorkflow(Client(self.store), self.production)

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def make(self, session='base', source=0, line=0, status='candidate', tags=None, note=''):
        tid, lid = self.sources[session][source]['id'], self.lines[line]['id']
        self.store.replace_candidates(self.production, [dict(transcript_id=tid, script_line_id=lid, score=.95)])
        row = next(row for row in self.store.list_candidates(self.production)
                   if row['transcript_id'] == tid and row['script_line_id'] == lid)
        if status != 'candidate' or tags or note:
            row = self.store.update_candidate(row['id'], status, tags=tags, note=note, expected_revision=row['revision'])
        return row

    def snapshot(self): return self.workflow.read_all()

    def save(self, data=None, source_rows=None, pairs=None, score=.8, tags=None):
        sources = self.sources['base'] if source_rows is None else source_rows
        pairs = [(sources[0]['id'], self.lines[0]['id'])] if pairs is None else pairs
        return self.workflow.save_match_results(data or self.snapshot(), sources,
            [dict(transcript_id=tid, script_line_id=lid, score=score, reason='上下文重新匹配',
                  **({'tags': tags} if tags is not None else {})) for tid, lid in pairs])

    def row(self, identity): return next(row for row in self.store.list_candidates(self.production) if row['id'] == identity)

    def enable_audio(self, name='base'):
        session = self.store.get_session(self.sessions[name]['id'])
        config = copy.deepcopy(session['config'])
        config['library_sync'] = {config['pgm']: dict(status='matched', placements=[dict(
            wav_path='/NAS/'+name+'/main.wav', pgm_start_seconds=0, pgm_end_seconds=10,
            wav_start_seconds=0, wav_end_seconds=10, offset_seconds=0,
            wav_info=dict(sample_rate=48000, samples=480000))], uncovered=[])}
        self.sessions[name] = self.store.save_session(self.production, session['name'], config,
            session_id=session['id'], expected_revision=session['revision'])

    def test_same_automatic_pair_uses_lower_score_and_requires_review(self):
        self.enable_audio()
        for tag, note in [('auto_dialogue', AUTO_NOTE), ('automatic_dialogue', LEGACY_AUTO_NOTE),
                          ('auto_dialogue', '')]:
            with self.subTest(tag=tag, note=note):
                old = self.make(status='confirmed', tags=[tag], note=note)
                sessions = self.store.list_sessions(self.production)
                transcripts = self.store.list_transcripts(self.production)
                fingerprint = session_fingerprint(self.snapshot(), self.sessions['base']['id'])
                result = self.save(score=.30)
                current = self.row(old['id'])
                self.assertEqual((current['status'], current['score'], current['note'], current['tags']),
                                 ('candidate', .30, '', []))
                self.assertEqual(current['reason'], '上下文重新匹配')
                self.assertEqual(current['revision'], old['revision']+1)
                self.assertEqual((result['updated'], result['preserved_decisions']), (1, 0))
                self.assertEqual(self.workflow._apply_auto_review(self.snapshot()), 0)
                decision = next(row for row in self.snapshot()['candidates'] if row['id'] == old['id'])
                self.assertTrue(decision['review_required'])
                self.assertEqual(self.store.list_sessions(self.production), sessions)
                self.assertEqual(self.store.list_transcripts(self.production), transcripts)
                self.assertNotEqual(session_fingerprint(self.snapshot(), self.sessions['base']['id']), fingerprint)

    def test_same_automatic_pair_uses_higher_score_and_can_be_confirmed_again(self):
        self.enable_audio()
        old = self.make(status='confirmed', tags=['automatic_dialogue'], note=LEGACY_AUTO_NOTE)
        self.save(score=.99)
        refreshed = self.row(old['id'])
        self.assertEqual((refreshed['status'], refreshed['score'], refreshed['note'], refreshed['tags']),
                         ('candidate', .99, '', []))
        self.assertEqual(self.workflow._apply_auto_review(self.snapshot()), 1)
        current = self.row(old['id'])
        self.assertEqual((current['status'], current['score'], current['note'], current['tags']),
                         ('confirmed', .99, AUTO_NOTE, ['auto_dialogue']))

    def test_human_decisions_survive_incoming_and_missing_pairs_with_old_auto_tags(self):
        cases = [('confirmed', [], ''), ('confirmed', ['auto_dialogue', '人工核对'], AUTO_NOTE),
                 ('confirmed', ['auto_dialogue'], '导演：保留此条'),
                 ('confirmed', ['automatic_dialogue'], AUTO_NOTE+' 导演已核实'),
                 ('rejected', ['auto_dialogue'], AUTO_NOTE), ('rejected', [], '不使用此条')]
        for status, tags, note in cases:
            with self.subTest(status=status, tags=tags, note=note):
                old = self.make(status=status, tags=tags, note=note)
                result = self.save(score=.30)
                self.assertEqual(result['preserved_decisions'], 1)
                self.assertEqual(self.row(old['id']), old)
                self.save(pairs=[])
                self.assertEqual(self.row(old['id']), old)

    def test_same_pair_refresh_preserves_review_fingerprint_and_alternative_choice(self):
        for name in ('base', 'alt'):
            self.enable_audio(name)
            for number in range(2):
                self.make(name, source=number, line=number, status='confirmed', tags=['auto_dialogue'], note=AUTO_NOTE)
        base = next(row for row in self.store.list_candidates(self.production)
                    if row['session_id'] == self.sessions['base']['id'] and row['line_order'] == self.lines[0]['line_order'])
        alt = next(row for row in self.store.list_candidates(self.production)
                   if row['session_id'] == self.sessions['alt']['id'] and row['line_order'] == self.lines[0]['line_order'])
        before = self.snapshot()
        version = next(row for row in before['pgm_match_versions'] if row['session_id'] == self.sessions['alt']['id'])
        self.workflow.mark_pgm_reviewed(version['session_id'], version['media_path'], expected_match_version=version)
        self.store.update_reference_settings(self.production, self.sessions['base']['id'], {alt['id']: base['id']},
                                             expected_revision=0)
        before = self.snapshot()
        index = PgmTakeIndex(before, prepared=True)
        fingerprint = coverage_fingerprint(index.file_summary(alt))
        reference = self.store.get_reference_settings(self.production)
        sessions = self.store.list_sessions(self.production)
        self.save(source_rows=self.sources['alt'], pairs=[(source['id'], self.lines[number]['id'])
                  for number, source in enumerate(self.sources['alt'])], score=.99)
        self.assertEqual(self.workflow._apply_auto_review(self.snapshot()), 2)
        after = self.snapshot()
        index = PgmTakeIndex(after, prepared=True)
        self.assertTrue(index.editor_state(alt)['reviewed'])
        self.assertEqual(coverage_fingerprint(index.file_summary(alt)), fingerprint)
        self.assertEqual(self.store.get_reference_settings(self.production), reference)
        self.assertEqual(self.store.list_sessions(self.production), sessions)
        self.assertEqual(session_fingerprint(after, self.sessions['base']['id']),
                         session_fingerprint(before, self.sessions['base']['id']))
        self.assertNotEqual(session_fingerprint(after, self.sessions['alt']['id']),
                            session_fingerprint(before, self.sessions['alt']['id']))

    def test_refresh_does_not_revive_explicitly_removed_pgm(self):
        old = self.make(status='confirmed', tags=['auto_dialogue'], note=AUTO_NOTE)
        version = next(row for row in self.snapshot()['pgm_match_versions']
                       if row['session_id'] == self.sessions['base']['id'])
        self.workflow.clear_pgm_matches(version['session_id'], version['media_path'],
            expected_session_revision=version['session_revision'],
            expected_candidate_revisions=version['candidate_revisions'],
            expected_reference_revision=version['reference_revision'])
        removed = self.row(old['id'])
        sessions = self.store.list_sessions(self.production)
        self.save(score=.99)
        self.assertEqual(self.workflow._apply_auto_review(self.snapshot()), 0)
        self.assertEqual(self.row(old['id']), removed)
        self.assertEqual(PgmTakeIndex(self.snapshot(), prepared=True).editor_state(removed)['pool'], 'removed')
        self.assertEqual(self.store.list_sessions(self.production), sessions)

    def test_span_tags_are_persisted_replaced_and_cleared_without_losing_human_metadata(self):
        ids = [line['id'] for line in self.lines]
        self.save(tags=encode_span_tags(ids[:2]))
        row = self.store.list_candidates(self.production)[0]
        self.assertEqual(parse_span_tags(row), ids[:2])
        current = self.store.update_candidate(row['id'], 'candidate', note='导演：等待复核',
            tags=row['tags']+['人工备注'], expected_revision=row['revision'])
        transcripts = self.store.list_transcripts(self.production)
        self.save(tags=encode_span_tags(ids))
        current = self.row(row['id'])
        self.assertEqual(parse_span_tags(current), ids)
        self.assertIn('人工备注', current['tags'])
        self.assertEqual(current['note'], '导演：等待复核')
        self.save()
        current = self.row(row['id'])
        self.assertEqual(current['tags'], ['人工备注'])
        self.assertEqual(current['note'], '导演：等待复核')
        self.assertEqual(self.store.list_transcripts(self.production), transcripts)

    def test_six_consecutive_span_lines_survive_reopening_without_schema_changes(self):
        script = self.store.put_script(self.production, '六行台本',
            [dict(text=f'第{number}句', order=number*10) for number in range(6)])
        ids = [line['id'] for line in self.store.list_script(self.production) if line['script_id'] == script['id']]
        self.save(pairs=[(self.sources['base'][0]['id'], ids[0])], tags=encode_span_tags(ids))
        reopened = Store(Path(self.folder.name)/'catalogue.db')
        try:
            self.assertEqual(parse_span_tags(reopened.list_candidates(self.production)[0]), ids)
        finally:
            reopened.close()

    def test_span_metadata_is_machine_owned_for_refresh_and_retirement(self):
        ids = [line['id'] for line in self.lines]
        for status in ('candidate', 'confirmed'):
            with self.subTest(status=status):
                self.save(tags=encode_span_tags(ids[:2]))
                row = self.store.list_candidates(self.production)[0]
                if status == 'confirmed':
                    row = self.store.update_candidate(row['id'], 'confirmed', note=AUTO_NOTE,
                        tags=row['tags']+['auto_dialogue'], expected_revision=row['revision'])
                    self.save(score=.30, tags=encode_span_tags(ids))
                    refreshed = self.row(row['id'])
                    self.assertEqual((refreshed['status'], refreshed['score'], refreshed['note']), ('candidate', .30, ''))
                    self.assertEqual(refreshed['tags'], encode_span_tags(ids))
                self.save(pairs=[])
                retired = self.row(row['id'])
                self.assertEqual(retired['status'], 'rejected')
                self.assertIn('auto_match_superseded', retired['tags'])
                self.save(tags=encode_span_tags(ids[:2]))
                recovered = self.row(row['id'])
                self.assertEqual(recovered['status'], 'candidate')
                self.assertEqual(recovered['tags'], encode_span_tags(ids[:2]))

    def test_span_refresh_does_not_change_human_confirmations_or_rejections(self):
        ids = [line['id'] for line in self.lines]
        for status, tags, note in [('confirmed', [], ''), ('confirmed', ['auto_dialogue', '人工核对'], AUTO_NOTE),
                                   ('confirmed', ['auto_dialogue'], '导演已核实'), ('rejected', ['auto_dialogue'], AUTO_NOTE)]:
            with self.subTest(status=status, tags=tags, note=note):
                old = self.make(status=status, tags=tags+encode_span_tags(ids[:2]), note=note)
                self.save(score=.30, tags=encode_span_tags(ids))
                self.assertEqual(self.row(old['id']), old)
                self.save(pairs=[])
                self.assertEqual(self.row(old['id']), old)

    def test_invalid_span_tags_roll_back_matching(self):
        ids = [line['id'] for line in self.lines]
        other_script = self.store.put_script(self.production, '其他台本', [dict(text='其他台词')])
        other_id = next(line['id'] for line in self.store.list_script(self.production)
                        if line['script_id'] == other_script['id'])
        foreign = self.store.create_production('另一剧目')['id']
        self.store.put_script(foreign, '外部台本', [dict(text='其他剧目台词')])
        foreign_id = self.store.list_script(foreign)[0]['id']
        def raw(values): return 'auto_script_span:'+json.dumps(values, separators=(',', ':'))
        invalid = [['人工备注'], [raw([])], [raw(ids[:1])], [raw([ids[0]]*2)], [raw(ids*3)],
                   [raw(ids[1:])], [raw([ids[0], ids[2]])], [raw([ids[0], other_id])],
                   [raw([ids[0], foreign_id])], [raw([ids[0], 3])], ['auto_script_span:not-json'],
                   'auto_script_span:[]', encode_span_tags(ids[:2])+encode_span_tags(ids)]
        before = self.store.list_candidates(self.production)
        for tags in invalid:
            with self.subTest(tags=tags), self.assertRaises(LibraryError) as caught:
                self.save(tags=tags)
            self.assertEqual(caught.exception.code, 'invalid')
            self.assertEqual(self.store.list_candidates(self.production), before)
        self.store.put_script(self.production, '台本', [dict(text='新版台词')])
        with self.assertRaises(LibraryError) as caught:
            self.save(tags=encode_span_tags(ids[:2]))
        self.assertEqual(caught.exception.code, 'invalid')
        self.assertEqual(self.store.list_candidates(self.production), before)

    def test_cross_scene_span_rolls_back_prior_incoming_candidate(self):
        script = self.store.put_script(self.production, '跨幕台本', [
            dict(text='第一幕第一句', scene='第一幕'), dict(text='第一幕第二句', scene='第一幕'),
            dict(text='第二幕第一句', scene='第二幕'), dict(text='第二幕第二句', scene='第二幕')])
        ids = [line['id'] for line in self.store.list_script(self.production) if line['script_id'] == script['id']]
        self.make(status='confirmed', tags=['auto_dialogue'], note=AUTO_NOTE)
        before = self.store.list_candidates(self.production)
        candidates = [dict(transcript_id=self.sources['base'][number]['id'], script_line_id=span[0],
                           score=.95, tags=encode_span_tags(span))
                      for number, span in enumerate((ids[:2], ids[1:3]))]
        with self.assertRaises(LibraryError) as caught:
            self.workflow.save_match_results(self.snapshot(), self.sources['base'], candidates)
        self.assertEqual(caught.exception.code, 'invalid')
        self.assertEqual(self.store.list_candidates(self.production), before)
        self.save(source_rows=[self.sources['base'][0]],
                  pairs=[(self.sources['base'][0]['id'], ids[0])], tags=encode_span_tags(ids[:2]))
        accepted = next(row for row in self.store.list_candidates(self.production) if row['script_line_id'] == ids[0])
        self.assertEqual(parse_span_tags(accepted), ids[:2])

    def test_stale_automatic_and_unreviewed_guesses_retired_but_humans_and_other_session_kept(self):
        automatic = self.make(line=1, status='confirmed', tags=['auto_dialogue'])
        unreviewed = self.make(line=2)
        human = self.make(source=1, line=1, status='confirmed', tags=['人工核对'])
        rejected = self.make(source=1, line=0, status='rejected', tags=['auto_dialogue'])
        human_pending = self.make(source=1, line=2, note='待导演确认')
        other = self.make('alt', line=1, status='confirmed', tags=['automatic_dialogue'])
        result = self.save()
        self.assertEqual(result['superseded_automatic'], 2)
        for row in (automatic, unreviewed):
            self.assertEqual(self.row(row['id'])['status'], 'rejected')
            self.assertIn('auto_match_superseded', self.row(row['id'])['tags'])
        for row in (human, rejected, human_pending, other):
            self.assertEqual(self.row(row['id']), row)
        data = self.snapshot()
        self.assertNotIn(automatic['id'], {row['id'] for row in __import__('library_auto_review').select_auto_confirmations(data)})

    def test_only_machine_retired_suggestions_may_be_recovered(self):
        old = self.make(line=1, status='confirmed', tags=['auto_dialogue'], note=AUTO_NOTE)
        self.save()
        self.save(pairs=[(old['transcript_id'], old['script_line_id'])])
        recovered = self.row(old['id'])
        self.assertEqual(recovered['status'], 'candidate')
        self.assertNotIn('auto_match_superseded', recovered['tags'])
        self.assertEqual(recovered['score'], .8)
        self.assertEqual(recovered['note'], '')
        self.save(pairs=[])
        retired = self.row(old['id'])
        human = self.store.update_candidate(old['id'], 'rejected', tags=retired['tags'],
                                           expected_revision=retired['revision'], actor='导演')
        self.assertNotIn('auto_match_superseded', human['tags'])
        self.save(pairs=[(old['transcript_id'], old['script_line_id'])])
        self.assertEqual(self.row(old['id']), human)

    def test_old_baseline_anchor_retired_atomically_and_alt_choice_retained_unanchored(self):
        base = self.make(status='confirmed', tags=['auto_dialogue'])
        alt = self.make('alt', status='confirmed', tags=['auto_dialogue'])
        # Whole-PGM selection needs accepted coverage beyond its first sentence.
        self.make(source=1, line=1, status='confirmed', tags=['auto_dialogue'])
        self.make('alt', source=1, line=1, status='confirmed', tags=['auto_dialogue'])
        self.store.update_reference_settings(self.production, self.sessions['base']['id'], {alt['id']: base['id']},
                                             dict(valid=True), expected_revision=0)
        self.save(pairs=[(base['transcript_id'], self.lines[1]['id'])])
        state = self.store.get_reference_settings(self.production)
        self.assertEqual(state['selected_clip_pairs'], {alt['id']: None})
        self.assertFalse(state['baseline_export']['valid'])
        self.assertEqual(self.row(alt['id']), alt)

    def test_stale_snapshot_rolls_back_inserts_and_retirement(self):
        old = self.make(line=1, status='confirmed', tags=['auto_dialogue'])
        data = self.snapshot()
        self.store.update_candidate(old['id'], 'confirmed', tags=['人工核对'], expected_revision=old['revision'])
        before = self.store.list_candidates(self.production)
        with self.assertRaises(LibraryError) as caught: self.save(data=data)
        self.assertEqual(caught.exception.code, 'conflict')
        self.assertEqual(self.store.list_candidates(self.production), before)

    def test_reference_or_recording_edit_since_matching_started_conflicts(self):
        old = self.make(line=1, status='confirmed', tags=['auto_dialogue'])
        data = self.snapshot()
        self.store.update_reference_settings(self.production, None, {}, expected_revision=0)
        with self.assertRaises(LibraryError) as caught: self.save(data=data)
        self.assertEqual(caught.exception.code, 'conflict')
        data = self.snapshot()
        session = self.sessions['base']
        self.store.save_session(self.production, session['name'], session['config'], session_id=session['id'],
                                expected_revision=session['revision'])
        with self.assertRaises(LibraryError) as caught: self.save(data=data)
        self.assertEqual(caught.exception.code, 'conflict')
        self.assertEqual(self.row(old['id']), old)

    def test_candidate_outside_explicit_scope_rejected_without_retiring_inside(self):
        old = self.make(line=1, status='confirmed', tags=['auto_dialogue'])
        with self.assertRaises(LibraryError) as caught:
            self.save(source_rows=[self.sources['base'][0]],
                      pairs=[(self.sources['alt'][0]['id'], self.lines[0]['id'])])
        self.assertEqual(caught.exception.code, 'invalid')
        self.assertEqual(self.row(old['id']), old)

    def test_inactive_historical_asr_cannot_be_refreshed(self):
        old = self.make(line=1, status='confirmed', tags=['auto_dialogue'])
        session = self.sessions['base']
        config = copy.deepcopy(session['config'])
        config['library_asr'] = {config['pgm']: dict(source_kind='manual', active_transcript_ids=[self.sources['base'][1]['id']])}
        self.store.save_session(self.production, session['name'], config, session_id=session['id'], expected_revision=session['revision'])
        with self.assertRaises(LibraryError) as caught: self.save()
        self.assertEqual(caught.exception.code, 'conflict')
        self.assertEqual(self.row(old['id']), old)

    def test_manual_visual_cannot_enter_automatic_scope(self):
        session = self.sessions['base']
        visual = self.store.add_transcript(session['id'], session['config']['pgm'], [dict(start_seconds=0,
            end_seconds=10, text='[人工画面归类]', source_kind='manual_visual')])['segments'][0]
        with self.assertRaises(LibraryError) as caught:
            self.store.replace_candidates(self.production, [], refreshed_transcript_ids=[visual['id']],
                expected_candidate_revisions={}, expected_session_revisions={session['id']: session['revision']},
                expected_reference_revision=0)
        self.assertEqual(caught.exception.code, 'invalid')


if __name__ == '__main__': unittest.main()
