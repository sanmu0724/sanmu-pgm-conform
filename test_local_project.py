"""Self-contained project migration and save/open across independent workstations."""
import concurrent.futures
import copy
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from local_project import LocalProjectClient, import_legacy_project, TABLES
from project_state import write_project_state, read_project_file
from project_ui import load_project_selection, _connect_library
from library_store import Store, LibraryError
from library_workflow import BatchWorkflow


class LocalProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, PGM_LIBRARY_HOME=str(self.root/'local'))
        env.start(); self.addCleanup(env.stop)
        self.client = LocalProjectClient(); self.addCleanup(self.client.close)
        self.prod = self.client.call('create_production', name='示例剧目')
        self.pid = self.prod['id']

    def populate(self, client=None):
        c = client or self.client
        session = c.call('save_session', production_id=self.pid, name='day01_通条',
            config=dict(fps=25, library_record_type='recording', pgm='/Volumes/NEXIS_SHOW/PGM.mov',
                sources=[dict(camera='A', proxy_path='/Volumes/NEXIS_SHOW/A.mov', source_id='fixed-source-id')],
                sound_sources=[dict(path='/Volumes/NEXIS_SHOW/MAIN.wav', reel='SOUND003')],
                library_sync={'PGM.mov': {'status':'matched','offset':3.2}},
                future={'retain':[1,2,3]}), segments=[dict(start=0,end=100,camera='A')])
        c.call('put_script', production_id=self.pid, title='中英台本',
               lines=[dict(text='Hello，今天你好吗？',speaker='甲',order=1)])
        line = c.call('list_script',production_id=self.pid)[0]
        trans = c.call('add_transcript', session_id=session['id'], media_path='/Volumes/NEXIS_SHOW/PGM.mov',
             metadata=dict(fps=25, asr_audio_source='original_wav', unknown={'keep':True}),
             segments=[dict(start_seconds=1,end_seconds=4,text='Hello，今天你好吗？',manual={'keep':True})])['segments'][0]
        c.call('replace_candidates',production_id=self.pid,candidates=[dict(script_line_id=line['id'],
              transcript_id=trans['id'],score=.95,reason='匹配')])
        cand=c.call('list_candidates',production_id=self.pid)[0]
        c.call('update_candidate',candidate_id=cand['id'],status='confirmed',note='人工核实',tags=['保留'],expected_revision=cand['revision'])
        c.call('register_artifact',session_id=session['id'],kind='multicam',path='/Volumes/NEXIS_SHOW/test.fcpxml',manifest={'source_id':'fixed-source-id'})
        c.call('update_reference_settings',production_id=self.pid,reference_session_id=session['id'],selected_clip_pairs={},expected_revision=0)
        return session

    def test_full_roundtrip_independent_of_database_and_connection(self):
        session=self.populate()
        original=self.client.export_project(self.pid)
        with patch('socket.socket',side_effect=AssertionError('network forbidden')):
            path=write_project_state(self.prod,session['id'],dict(config={'fps':25},segments=[]),
                save_path=self.root/'剪辑.pgmproj',catalogue=original)
            state=read_project_file(path)
            reopened,production,loaded=load_project_selection({'mode':'file','path':str(path)},LocalProjectClient(),[])
            self.addCleanup(reopened.close)
            self.assertEqual(reopened.export_project(self.pid),original)
            self.assertEqual(loaded['session_id'],session['id'])
            data=BatchWorkflow(reopened,self.pid).read_all()
        self.assertEqual(data['candidates'][0]['status'],'confirmed')
        self.assertEqual(data['sessions'][0]['config']['sources'][0]['source_id'],'fixed-source-id')
        self.assertFalse(list(self.root.rglob('*.sqlite3')))
        self.assertEqual(state['version'],2)
        self.assertEqual(set(state['catalogue']['tables']),set(TABLES))
        self.assertTrue(all(state['catalogue']['tables'][table] for table in TABLES))
        self.assertNotIn('catalogue',(__import__('json').loads(next((self.root/'local/projects').glob('*.json')).read_text())))

    def test_legacy_migration_is_read_only_and_copies_only_selected_project(self):
        db=self.root/'old.sqlite3'; old=Store(db)
        old.call('create_production',{'name':'Unrelated'})
        # Copy the representative project into a disk store with all relationships intact.
        self.populate(); payload=self.client.export_project(self.pid)
        restored=LocalProjectClient.from_project(payload)
        self.addCleanup(restored.close)
        with old._connection(True) as con, restored.store._connection() as source:
            for table in TABLES:
                for row in source.execute('SELECT * FROM '+table):
                    con.execute('INSERT INTO '+table+' VALUES ('+','.join('?' for _ in row)+')',tuple(row))
        old.close();before=hashlib.sha256(db.read_bytes()).hexdigest()
        migrated=import_legacy_project(self.pid,[db]);self.addCleanup(migrated.close)
        self.assertEqual(migrated.export_project(self.pid)['tables'],payload['tables'])
        self.assertEqual(len(migrated.call('list_productions')),1)
        self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(),before)

    def test_mutation_revision_reads_and_failed_changes(self):
        revision=self.client.revision
        self.client.call('list_productions');self.client.export_project(self.pid)
        self.assertEqual(self.client.revision,revision)
        with self.assertRaises(LibraryError):self.client.call('save_session',production_id='missing',name='x',config={})
        self.assertEqual(self.client.revision,revision)
        self.populate();self.assertGreater(self.client.revision,revision)

    def test_worker_created_project_can_be_read_saved_and_closed_on_gui_thread(self):
        with concurrent.futures.ThreadPoolExecutor() as pool:
            client=pool.submit(LocalProjectClient).result()
            prod=pool.submit(lambda:client.call('create_production',name='Threaded')).result()
        self.assertEqual(client.export_project(prod['id'])['tables']['productions'][0]['name'],'Threaded')
        client.close();client.close()

    def test_invalid_foreign_keys_and_cross_project_payload_rejected(self):
        self.populate();original=self.client.export_project(self.pid)
        for table in ('sessions','transcripts','candidates'):
            invalid=copy.deepcopy(original);invalid['tables'][table][0]['production_id']='other'
            with self.assertRaises(ValueError):LocalProjectClient.from_project(invalid)
        invalid=copy.deepcopy(original);invalid['tables']['transcripts'][0]['media_id']='missing'
        with self.assertRaises((ValueError,__import__('sqlite3').IntegrityError)):LocalProjectClient.from_project(invalid)
        self.assertEqual(self.client.export_project(self.pid),original)

    def test_new_project_never_reuses_old_records_or_saved_remote(self):
        self.populate()
        with patch('socket.socket',side_effect=AssertionError('no service')), \
             patch('library_bridge.read_settings',return_value={'library_connection':{'url':'http://old','token':'old'}}):
            client,rows,_=_connect_library(self.client,'',lambda *_:None,lambda:False)
            new,prod,state=load_project_selection({'mode':'new','name':'Another'},client,rows)
        self.addCleanup(new.close)
        self.assertNotEqual(prod['id'],self.pid)
        self.assertEqual(new.call('list_sessions',production_id=prod['id']),[])
        self.assertIsNone(state)
        self.assertEqual(len(self.client.call('list_sessions',production_id=self.pid)),1)

    def test_missing_complete_project_does_not_silently_restore_old_index(self):
        with self.assertRaises(ValueError):load_project_selection(
            {'mode':'existing','production_id':'missing'},self.client,
            [{'id':'missing','name':'Missing','project_path':str(self.root/'missing.pgmproj')}])


if __name__=='__main__':unittest.main()
