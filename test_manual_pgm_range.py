"""Explicit PGM script ranges retain full takes and atomically replace anchors."""
import concurrent.futures
import copy
from pathlib import Path
import tempfile
import threading
import unittest

from library_store import LibraryError, Store
from library_workflow import BatchWorkflow
from pgm_takes import PgmTakeIndex


class Client:
    actor = '场记'
    def __init__(self, store): self.store = store
    def call(self, method, **params): return self.store.call(method, params, self.actor)


class ManualPgmRangeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.folder.name)/'catalogue.db')
        self.production = self.store.create_production('人工范围')['id']
        self.store.put_script(self.production, '全台本', [dict(text='台词第'+str(i)+'句', order=i)
                                                         for i in range(1, 101)])
        self.lines = self.store.list_script(self.production)
        self.path = '/NAS/day01/PIX_001_046.mov'
        self.session = self.store.save_session(self.production, 'day01_分镜头', dict(pgm=self.path,
            pgm_timecodes={self.path: '21:00:00:00'}, library_media=[dict(kind='pgm', path=self.path, fps=25, frames=2500)]))
        speech = self.store.add_transcript(self.session['id'], self.path, [dict(start_seconds=1.1,
            end_seconds=2.4, text='前卷杂音'), dict(start_seconds=4.1, end_seconds=6.5, text='摄影机准备开始。'),
            dict(start_seconds=7.1, end_seconds=9, text='台词第55句')])
        self.speech = speech['segments']
        self.workflow = BatchWorkflow(Client(self.store), self.production)

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def version(self): return next(row for row in self.workflow.read_all()['pgm_match_versions']
                                   if row['session_id'] == self.session['id'] and row['media_path'] == self.path)

    def assign(self, first=55, last=76, begin=7.1, end=100, version=None):
        return self.workflow.associate_pending_interval(self.session['id'], self.path, begin, end,
            self.lines[first-1]['id'], script_line_ids=[row['id'] for row in self.lines[first-1:last]],
            expected_match_version=version, note='场记核实')

    def test_one_candidate_covers_full_range_and_explicit_anchor_skips_opening_cues(self):
        result = self.assign()
        row = result['candidate']
        self.assertEqual(row['script_line_id'], self.lines[54]['id'])
        source = result['transcript']['segments'][0]
        self.assertEqual(source['data']['manual_script_line_ids'], [row['id'] for row in self.lines[54:76]])
        self.assertTrue(source['data']['manual_range_explicit'])
        self.assertEqual(source['start_seconds'], 7.1)
        self.assertEqual(len(self.store.list_candidates(self.production)), 1)
        index = PgmTakeIndex(result['data'], prepared=True)
        summary = index.file_summary(row)
        self.assertTrue(summary['alignment_ready'])
        self.assertEqual(summary['first_candidate']['id'], row['id'])
        self.assertEqual(summary['covered_line_ids'], [row['id'] for row in self.lines[54:76]])
        self.assertEqual(float(index.duration(row)), 100)
        actual_speech = [row for row in self.store.list_transcripts(self.production)
                         if row['id'] in {source['id'] for source in self.speech}]
        self.assertEqual(actual_speech, self.speech)

    def test_changing_actual_first_sentence_retires_old_anchor_but_retains_history(self):
        first = self.assign(begin=0)
        second = self.assign(begin=7.1)
        self.assertNotEqual(first['candidate']['id'], second['candidate']['id'])
        self.assertEqual(second['retired_manual_count'], 1)
        rows = self.store.list_candidates(self.production)
        self.assertEqual([row['id'] for row in rows if row['status'] == 'confirmed'], [second['candidate']['id']])
        old = next(row for row in rows if row['id'] == first['candidate']['id'])
        self.assertEqual(old['status'], 'rejected')
        self.assertIn('manual_range_superseded', old['tags'])
        self.assertEqual(len(self.store.list_transcripts(self.production)), 5)
        summary = PgmTakeIndex(second['data'], prepared=True).file_summary(second['candidate'])
        self.assertEqual(summary['first_candidate']['start_seconds'], 7.1)

    def test_changing_range_at_same_seconds_updates_source_and_keeps_unique_anchor(self):
        first = self.assign()
        second = self.assign(first=40, last=80)
        self.assertEqual(first['candidate']['transcript_id'], second['candidate']['transcript_id'])
        self.assertEqual(len(self.store.list_transcripts(self.production)), 4)
        self.assertEqual(len([row for row in self.store.list_candidates(self.production) if row['status'] == 'confirmed']), 1)
        self.assertEqual(second['transcript']['segments'][0]['data']['manual_script_line_ids'],
                         [row['id'] for row in self.lines[39:80]])
        self.assertEqual(PgmTakeIndex(second['data'], prepared=True).file_summary(second['candidate'])['covered_line_ids'],
                         [row['id'] for row in self.lines[39:80]])

    def test_invalid_noncontinuous_cross_script_inactive_or_outside_media_does_not_write(self):
        original = self.store.list_transcripts(self.production)
        invalid = [[self.lines[54]['id'], self.lines[56]['id']], [self.lines[54]['id'], self.lines[54]['id']], []]
        self.store.put_script(self.production, '其他台本', [dict(text='其他台词')])
        other = next(row for row in self.store.list_script(self.production) if row['title'] == '其他台本')
        invalid.append([self.lines[54]['id'], other['id']])
        for ids in invalid:
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.workflow.associate_pending_interval(self.session['id'], self.path, 7.1, 100,
                    self.lines[54]['id'], script_line_ids=ids)
        with self.assertRaises(ValueError): self.assign(end=101)
        self.assertEqual(self.store.list_transcripts(self.production), original)
        self.assertEqual(self.store.list_candidates(self.production), [])

    def test_stale_candidate_or_reference_version_rolls_back_every_record(self):
        initial = self.assign()
        version = self.version()
        row = initial['candidate']
        self.store.update_candidate(row['id'], 'confirmed', note='并发修改', expected_revision=row['revision'])
        before = self.store.list_transcripts(self.production), self.store.list_candidates(self.production)
        with self.assertRaises(LibraryError) as caught: self.assign(first=40, last=60, version=version)
        self.assertEqual(caught.exception.code, 'conflict')
        self.assertEqual(before, (self.store.list_transcripts(self.production), self.store.list_candidates(self.production)))
        version = self.version()
        state = self.store.get_reference_settings(self.production)
        self.store.update_reference_settings(self.production, None, {}, expected_revision=state['revision'])
        with self.assertRaises(LibraryError) as caught: self.assign(first=40, last=60, version=version)
        self.assertEqual(caught.exception.code, 'conflict')
        self.assertEqual(before, (self.store.list_transcripts(self.production), self.store.list_candidates(self.production)))

    def test_concurrent_manual_ranges_have_one_atomic_winner(self):
        version = self.version()
        barrier = threading.Barrier(2)
        def assign(first):
            barrier.wait()
            try: return self.assign(first, first+2, version=version)['candidate']['id']
            except LibraryError as exc: return exc.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool: results = list(pool.map(assign, (55, 70)))
        self.assertEqual(results.count('conflict'), 1)
        self.assertEqual(len(self.store.list_candidates(self.production)), 1)
        self.assertEqual(len(self.store.list_transcripts(self.production)), 4)

    def test_legacy_single_line_call_remains_compatible(self):
        first = self.workflow.associate_pending_interval(self.session['id'], self.path, 0, 2, self.lines[0]['id'])
        second = self.workflow.associate_pending_interval(self.session['id'], self.path, 7.1, 10, self.lines[1]['id'])
        self.assertEqual(first['candidate']['status'], 'confirmed')
        self.assertEqual(second['candidate']['status'], 'confirmed')
        self.assertEqual(second['transcript']['segments'][0]['data']['manual_script_line_ids'], [self.lines[1]['id']])
        self.assertEqual(len([row for row in self.store.list_candidates(self.production) if row['status'] == 'confirmed']), 1)
        old = next(row for row in self.store.list_candidates(self.production) if row['id'] == first['candidate']['id'])
        self.assertEqual(old['status'], 'rejected')
        self.assertEqual(len(self.store.list_transcripts(self.production)), 5)
        self.assertTrue(second['session']['config']['library_pgm_editor'][self.path]['reviewed'])

    def test_direct_rpc_validation_and_cross_session_path_never_alias(self):
        version = self.version()
        params = dict(production_id=self.production, session_id=self.session['id'], media_path=self.path,
            start_seconds=7.1, end_seconds=100, script_line_ids=[self.lines[54]['id'], self.lines[56]['id']],
            expected_session_revision=version['session_revision'], expected_candidate_revisions=version['candidate_revisions'],
            expected_reference_revision=version['reference_revision'])
        with self.assertRaises(LibraryError) as caught: self.store.associate_pgm_script_range(**params)
        self.assertEqual(caught.exception.code, 'invalid')
        params.update(script_line_ids=[self.lines[54]['id']], media_path='/NAS/otherday/PIX_001_046.mov')
        with self.assertRaises(LibraryError): self.store.associate_pgm_script_range(**params)
        self.assertEqual(self.store.list_candidates(self.production), [])
        self.assertEqual(len(self.store.list_transcripts(self.production)), 3)


if __name__ == '__main__': unittest.main()
