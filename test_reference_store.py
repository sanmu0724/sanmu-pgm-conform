"""Shared baseline settings must not lose concurrent editors' decisions."""
import concurrent.futures
from pathlib import Path
import tempfile
import threading
import unittest
from library_store import Store, LibraryError


class ReferenceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/'library.sqlite3')
        self.production=self.store.create_production('试验')
        self.pid=self.production['id']
        self.base=self.store.save_session(self.pid,'day03_通条',{'recording_kind':'continuous'})
        self.alt=self.store.save_session(self.pid,'day01_分镜头',{'recording_kind':'coverage'})
        self.store.put_script(self.pid,'台本',[{'text':'Good morning 早上好','order':1},{'text':'第二句','order':2}])
        self.lines=self.store.list_script(self.pid)
        items=[]
        for session in (self.base,self.alt):
            transcript=self.store.add_transcript(session['id'],'/NAS/'+session['id']+'.mov',
                [{'start_seconds':1,'end_seconds':3,'text':'Good morning 早上好'}])['segments'][0]
            items.append({'script_line_id':self.lines[0]['id'],'transcript_id':transcript['id'],'score':1})
        self.store.replace_candidates(self.pid,items)
        candidates=self.store.list_candidates(self.pid)
        self.candidates={r['session_id']:self.store.update_candidate(r['id'],'confirmed',expected_revision=1) for r in candidates}
        self.pairs={self.candidates[self.alt['id']]['id']:self.candidates[self.base['id']]['id']}

    def tearDown(self):
        self.store.close();self.temp.cleanup()

    def test_initial_state_and_roundtrip_independent_of_session_revision(self):
        self.assertEqual(self.store.get_reference_settings(self.pid)['revision'],0)
        state=self.store.update_reference_settings(self.pid,self.base['id'],self.pairs,{'valid':True,'xml_path':'/NAS/base.fcpxml'})
        self.assertEqual(state['revision'],1)
        self.store.save_session(self.pid,self.base['name'],{'recording_kind':'continuous','library_note':'注'},
            session_id=self.base['id'],expected_revision=1)
        self.assertEqual(self.store.get_reference_settings(self.pid),state)

    def test_simultaneous_first_choice_has_one_winner(self):
        barrier=threading.Barrier(2)
        def set_choice(_):
            barrier.wait()
            try:return self.store.update_reference_settings(self.pid,self.base['id'],self.pairs)['revision']
            except LibraryError as exc:return exc.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            result=list(pool.map(set_choice,range(2)))
        self.assertCountEqual(result,[1,'conflict'])
        self.assertEqual(len([r for r in self.store.audit(self.pid) if r['action']=='update_reference_settings']),1)

    def test_requires_confirmed_active_cross_session_same_script(self):
        invalid=[{self.candidates[self.base['id']]['id']:self.candidates[self.base['id']]['id']},
                 {self.candidates[self.alt['id']]['id']:self.candidates[self.alt['id']]['id']}]
        for pairs in invalid:
            with self.assertRaises(LibraryError):self.store.update_reference_settings(self.pid,self.base['id'],pairs)
        alt=self.candidates[self.alt['id']]
        self.store.update_candidate(alt['id'],'rejected',expected_revision=alt['revision'])
        with self.assertRaises(LibraryError):self.store.update_reference_settings(self.pid,self.base['id'],self.pairs)
        self.store.update_candidate(alt['id'],'confirmed',expected_revision=alt['revision']+1)
        self.store.put_script(self.pid,'台本',[{'text':'更新的台本','order':1}])
        with self.assertRaises(LibraryError):self.store.update_reference_settings(self.pid,self.base['id'],self.pairs)

    def test_other_production_rejected_and_state_unchanged(self):
        other=self.store.create_production('其他')
        foreign=self.store.save_session(other['id'],'day01_通条',{})
        with self.assertRaises(LibraryError):self.store.update_reference_settings(self.pid,foreign['id'])
        self.assertEqual(self.store.get_reference_settings(self.pid)['revision'],0)

    def test_reject_invalid_settings_and_boolean_revision(self):
        for kwargs in ({'selected_clip_pairs':[]},{'baseline_export':[]},{'expected_revision':True},
                       {'expected_revision':None},{'selected_clip_pairs':{'a':3}}):
            with self.assertRaises(LibraryError):self.store.update_reference_settings(self.pid,self.base['id'],**kwargs)

    def test_stale_update_cannot_clear_other_person_selection(self):
        self.store.update_reference_settings(self.pid,self.base['id'],self.pairs)
        with self.assertRaises(LibraryError):self.store.update_reference_settings(self.pid,self.base['id'],{},expected_revision=0)
        self.assertEqual(self.store.get_reference_settings(self.pid)['selected_clip_pairs'],self.pairs)

    def test_pending_choices_roundtrip_without_a_reference_and_still_check_cas(self):
        identity = self.candidates[self.alt['id']]['id']
        state = self.store.update_reference_settings(self.pid, None, {identity: None})
        self.assertIsNone(state['reference_session_id'])
        self.assertEqual(state['selected_clip_pairs'], {identity: None})
        with self.assertRaises(LibraryError):
            self.store.update_reference_settings(self.pid, None, {}, expected_revision=0)
        state = self.store.update_reference_settings(self.pid, self.base['id'], self.pairs,
            expected_revision=state['revision'])
        self.assertEqual(state['selected_clip_pairs'], self.pairs)

    def test_pending_choices_validate_ownership_status_and_cannot_anchor_without_base(self):
        identity = self.candidates[self.alt['id']]['id']
        with self.assertRaises(LibraryError):
            self.store.update_reference_settings(self.pid, None, self.pairs)
        self.store.update_candidate(identity, 'rejected', expected_revision=self.candidates[self.alt['id']]['revision'])
        with self.assertRaises(LibraryError):
            self.store.update_reference_settings(self.pid, None, {identity: None})

    def test_migration_preserves_legacy_reference_choices_revision_and_foreign_keys(self):
        state = self.store.update_reference_settings(self.pid, self.base['id'], self.pairs)
        with self.store._connection(True) as con:
            con.execute('ALTER TABLE reference_settings RENAME TO newer')
            con.execute('CREATE TABLE reference_settings (production_id TEXT PRIMARY KEY REFERENCES productions(id), '
                'reference_session_id TEXT NOT NULL REFERENCES sessions(id), settings TEXT NOT NULL, '
                'updated_at TEXT NOT NULL, revision INTEGER NOT NULL)')
            con.execute('INSERT INTO reference_settings SELECT * FROM newer')
            con.execute('DROP TABLE newer')
            con.execute('UPDATE schema_info SET version=1')
        migrated = Store(self.store.path)
        try:
            self.assertEqual(migrated.get_reference_settings(self.pid), state)
            with migrated._connection() as con:
                self.assertEqual(con.execute('PRAGMA foreign_key_check').fetchall(), [])
                self.assertEqual(con.execute('SELECT version FROM schema_info').fetchone()[0], 2)
            chosen = next(iter(self.pairs))
            migrated.update_reference_settings(self.pid, None, {chosen: None}, expected_revision=state['revision'])
        finally:
            migrated.close()


if __name__=='__main__':unittest.main()
