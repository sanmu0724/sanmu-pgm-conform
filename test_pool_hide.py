"""Media-pool deletion hides files persistently, preserving catalogue/media."""
import copy
from pathlib import Path
import threading
import unittest

from library_store import LibraryError, Store
from library_workflow import BatchWorkflow
from pgm_library_view import prepare_pgm_inventory
from pgm_takes import PgmTakeIndex


class PoolHideTests(unittest.TestCase):
    def setUp(self):
        from test_pgm_editor_state import PgmEditorStateTests
        self.f = PgmEditorStateTests()
        self.f.setUp()

    def tearDown(self):
        self.f.tearDown()

    def hide(self, version=None, workflow=None):
        return (workflow or self.f.workflow).hide_pgm_from_pool(self.f.session['id'], self.f.path,
            expected_match_version=version or self.f.version())

    def inventory(self, data):
        index = PgmTakeIndex(data, prepared=True)
        return prepare_pgm_inventory(data['sessions'], data['transcripts'], data['candidates'],
            data.get('unmatched_transcripts', []), index)

    def test_removed_file_hidden_without_deleting_related_data_or_reference(self):
        self.f.remove()
        before = self.f.data()
        media_file = Path(self.f.folder.name)/'physical_file.mov'
        media_file.write_bytes(b'original media unchanged')
        result = self.hide()
        after = result['data']
        for key in ('transcripts', 'candidates', 'scripts', 'reference_state'):
            self.assertEqual(after[key], before[key])
        original, current = copy.deepcopy(before['sessions'][0]), copy.deepcopy(after['sessions'][0])
        original['config'].pop('library_pgm_editor')
        current['config'].pop('library_pgm_editor')
        self.assertEqual(current['config'], original['config'])
        self.assertEqual(media_file.read_bytes(), b'original media unchanged')
        self.assertEqual(self.inventory(after), ([], {}))
        self.assertEqual(self.f.state(after)['pool'], 'removed')
        self.assertTrue(self.f.state(after)['hidden_in_pool'])

    def test_registered_file_before_asr_can_be_hidden_and_other_same_basename_remains(self):
        path = '/NAS/day02/PGM001.mov'
        config = dict(pgm=path, fps=25, library_media=[dict(kind='pgm', path=path, frames=250, fps=25)],
                      sources=[dict(camera='A', proxy_path='/NAS/daily.mov')])
        session = self.f.store.save_session(self.f.production, 'day02_分镜头', config)
        data = self.f.data()
        version = next(row for row in data['pgm_match_versions'] if row['session_id'] == session['id'])
        result = self.f.workflow.hide_pgm_from_pool(session['id'], path, expected_match_version=version)
        rows, _ = self.inventory(result['data'])
        self.assertEqual({row['media_path'] for row in rows}, {self.f.path})
        self.assertEqual(self.f.store.get_session(session['id'])['config']['sources'], config['sources'])

    def test_reopen_and_technical_config_save_keep_hidden_flag(self):
        self.f.remove()
        self.hide()
        reopened = Store(self.f.db)
        try:
            old = reopened.get_session(self.f.session['id'])
            config = copy.deepcopy(old['config'])
            config.pop('library_pgm_editor')
            reopened.save_session(self.f.production, old['name'], config, session_id=old['id'],
                                 expected_revision=old['revision'])
            self.assertTrue(reopened.get_session(old['id'])['config']['library_pgm_editor'][self.f.path]['hidden_in_pool'])
        finally:
            reopened.close()
        self.assertEqual(self.inventory(self.f.data()), ([], {}))

    def test_track_file_cannot_be_hidden_via_pool_action(self):
        before = self.f.data()
        with self.assertRaises(LibraryError) as error:
            self.hide()
        self.assertEqual(error.exception.code, 'invalid')
        self.assertEqual(self.f.data(), before)

    def test_stale_session_candidate_and_reference_leave_no_partial_hide(self):
        self.f.remove()
        for change in ('session', 'candidate', 'reference'):
            with self.subTest(change=change):
                version = self.f.version()
                if change == 'session':
                    session = self.f.store.get_session(self.f.session['id'])
                    self.f.store.save_session(self.f.production, session['name'], session['config'],
                        session_id=session['id'], expected_revision=session['revision'])
                elif change == 'candidate':
                    row = self.f.store.list_candidates(self.f.production)[0]
                    self.f.store.update_candidate(row['id'], 'rejected', note='并发修改', expected_revision=row['revision'])
                else:
                    state = self.f.store.get_reference_settings(self.f.production)
                    self.f.store.update_reference_settings(self.f.production, None, {}, {}, expected_revision=state['revision'])
                before = self.f.data()
                with self.assertRaises(LibraryError) as error:
                    self.hide(version)
                self.assertEqual(error.exception.code, 'conflict')
                self.assertEqual(self.f.data(), before)

    def test_old_service_reports_upgrade_without_changing_any_data(self):
        self.f.remove()
        client = self.f.workflow.client
        class OldClient:
            actor = client.actor
            def call(self, method, **params):
                if method == 'hide_pgm_from_pool':
                    raise LibraryError('invalid', '不支持的操作')
                return client.call(method, **params)
        before = self.f.data()
        with self.assertRaises(LibraryError) as error:
            self.hide(workflow=BatchWorkflow(OldClient(), self.f.production))
        self.assertEqual(error.exception.code, 'incompatible')
        self.assertIn('API revision 7', str(error.exception))
        self.assertEqual(self.f.data(), before)

    def test_actual_http_hide_round_trip(self):
        from library_service import Client, LibraryServer
        self.f.remove()
        token = 'pool-hide-test-token-abcdefghijklmnopqrstuvwxyz'
        server = LibraryServer(('127.0.0.1', 0), self.f.store, token)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = Client(f'http://127.0.0.1:{server.server_port}', token, 'QA')
            result = self.hide(workflow=BatchWorkflow(client, self.f.production))
            self.assertEqual(self.inventory(result['data']), ([], {}))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == '__main__':
    unittest.main()
