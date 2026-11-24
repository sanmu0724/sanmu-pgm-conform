import copy
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
import uuid
from unittest.mock import patch

from project_state import read_project_file, read_project_state, write_project_state


class ProjectStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.env = patch.dict(os.environ, {'PGM_LIBRARY_HOME': str(self.root/'app')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.production = dict(id=str(uuid.uuid4()), name='示例剧目：导演剪辑', token='never-save')
        self.session = str(uuid.uuid4())
        self.snapshot = dict(config=dict(pgm='/NAS/day01/PGM001.mov',
            sources=[dict(camera='A', proxy_path='/NAS/day01/A001.mov')],
            camera_offsets={'A': -3}, calibration_confirmed=True),
            segments=[dict(start=0, end=50, camera='A', reason='manual')])

    def test_named_project_and_unexported_camera_settings_roundtrip(self):
        path = write_project_state(self.production, self.session, self.snapshot)
        self.assertEqual(path, self.root/'app'/'projects'/(self.production['id']+'.json'))
        state = read_project_state(self.production['id'])
        self.assertEqual(state['name'], self.production['name'])
        self.assertEqual(state['session_id'], self.session)
        self.assertEqual(state['snapshot'], self.snapshot)
        self.assertTrue(state['saved_at'].endswith('+00:00'))

    def test_default_save_has_empty_snapshot_and_no_recording_guess(self):
        write_project_state(self.production)
        state = read_project_state(self.production['id'])
        self.assertEqual(state['session_id'], '')
        self.assertEqual(state['snapshot'], {})

    def test_chosen_location_and_checkpoint_remember_same_project(self):
        target = self.root/'剪辑项目'/'示例剧目.pgmproj'
        saved = write_project_state(self.production, self.session, self.snapshot, save_path=target)
        self.assertEqual(saved, target)
        state = read_project_state(self.production['id'])
        self.assertEqual(state['project_path'], str(target))
        self.assertEqual(read_project_file(target)['snapshot'], self.snapshot)
        self.assertEqual(read_project_file(target)['production_id'], self.production['id'])
        self.assertNotIn('never-save', target.read_text())

    def test_failed_external_save_keeps_old_checkpoint_and_file(self):
        target = self.root/'编辑.pgmproj'
        write_project_state(self.production, save_path=target)
        checkpoint = self.root/'app'/'projects'/(self.production['id']+'.json')
        old = (target.read_bytes(), checkpoint.read_bytes())
        with patch('project_state.os.replace', side_effect=OSError('disk unavailable')):
            with self.assertRaisesRegex(OSError, 'unavailable'):
                write_project_state(dict(self.production, name='new'), save_path=target)
        self.assertEqual((target.read_bytes(), checkpoint.read_bytes()), old)

    def test_project_file_moved_remembers_actual_chosen_location(self):
        original = self.root/'旧路径.pgmproj'
        moved = self.root/'新路径.pgmproj'
        write_project_state(self.production, save_path=original)
        original.rename(moved)
        self.assertEqual(read_project_file(moved)['project_path'], str(moved))

    def test_external_file_extension_and_symlink_are_checked(self):
        for target in (self.root/'file.mov', self.root/'file.sqlite3'):
            with self.assertRaisesRegex(ValueError, 'pgmproj'):
                write_project_state(self.production, save_path=target)
            self.assertFalse(target.exists())
        external = self.root/'target.pgmproj'; external.write_text('untouched')
        link = self.root/'link.pgmproj'; link.symlink_to(external)
        with self.assertRaisesRegex(ValueError, '符号链接'):
            write_project_state(self.production, save_path=link)
        self.assertEqual(external.read_text(), 'untouched')

    def test_read_missing_state_does_not_create_files(self):
        self.assertEqual(read_project_state(self.production['id']), {})
        self.assertFalse((self.root/'app').exists())

    def test_existing_checkpoint_is_replaced_by_complete_new_save(self):
        path = write_project_state(self.production, self.session, self.snapshot)
        changed = copy.deepcopy(self.snapshot)
        changed['config']['camera_offsets']['A'] = 2
        write_project_state(dict(self.production, name='新名称'), self.session, changed)
        self.assertEqual(read_project_state(self.production['id'])['snapshot'], changed)
        self.assertEqual(read_project_state(self.production['id'])['name'], '新名称')
        self.assertEqual(list(path.parent.glob('*.tmp')), [])

    def test_credentials_and_connection_payloads_are_not_saved(self):
        snapshot = copy.deepcopy(self.snapshot)
        snapshot.update(token='top-level-token', client='connection', url='http://service')
        snapshot['config'].update(Token='config-token', access_token='access',
            serviceToken='camel-case-secret',
            nested=dict(Authorization='Bearer secret', value='keep'),
            library_connection=dict(url='http://server', token='secret'))
        path = write_project_state(self.production, snapshot=snapshot)
        payload = path.read_text()
        for forbidden in ('never-save', 'top-level-token', 'config-token', 'camel-case-secret', 'Bearer secret', 'http://service', 'http://server'):
            self.assertNotIn(forbidden, payload)
        restored = read_project_state(self.production['id'])['snapshot']['config']
        self.assertEqual(restored['nested'], {'value': 'keep'})

    def test_input_payload_is_not_mutated(self):
        before = copy.deepcopy((self.production, self.snapshot))
        write_project_state(self.production, self.session, self.snapshot)
        self.assertEqual((self.production, self.snapshot), before)

    def test_path_injection_and_non_uuid_ids_are_rejected(self):
        for identity in ('../escape', '/tmp/escape', self.production['id']+'/a', '', 's01', 'urn:uuid:not-a-uuid'):
            with self.subTest(identity=identity):
                with self.assertRaises(ValueError):
                    write_project_state(dict(self.production, id=identity))
                with self.assertRaises(ValueError):
                    read_project_state(identity)
        self.assertFalse((self.root/'app').exists())

    def test_uuid_spelling_is_canonicalized_without_duplicate_files(self):
        compact = uuid.UUID(self.production['id']).hex.upper()
        path = write_project_state(dict(self.production, id=compact))
        self.assertEqual(path.name, self.production['id']+'.json')
        self.assertEqual(read_project_state(compact)['production_id'], self.production['id'])

    def test_invalid_session_and_snapshot_leave_prior_checkpoint_untouched(self):
        path = write_project_state(self.production, self.session, self.snapshot)
        previous = path.read_bytes()
        for arguments in (dict(session_id='../session'), dict(snapshot=[]),
                          dict(snapshot=dict(config=[], segments=[])),
                          dict(snapshot=dict(config={}, segments=['bad'])),
                          dict(snapshot=dict(config={'offset': float('nan')}, segments=[]))):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                write_project_state(self.production, **arguments)
            self.assertEqual(path.read_bytes(), previous)

    def test_atomic_replace_failure_retains_previous_file_and_cleans_temp(self):
        path = write_project_state(self.production, self.session, self.snapshot)
        previous = path.read_bytes()
        with patch('project_state.os.replace', side_effect=OSError('simulated disk failure')):
            with self.assertRaisesRegex(OSError, 'disk failure'):
                write_project_state(dict(self.production, name='未提交'))
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_checkpoint_permissions_are_private(self):
        path = write_project_state(self.production)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_corrupt_or_foreign_state_returns_empty_and_retains_file(self):
        path = write_project_state(self.production)
        valid = json.loads(path.read_text())
        for value in ('{bad json', json.dumps(dict(valid, production_id=str(uuid.uuid4()))),
                      json.dumps(dict(valid, version=99)), json.dumps(dict(valid, session_id='../session')),
                      json.dumps(dict(valid, snapshot=dict(config={'bad': float('nan')}, segments=[])))):
            with self.subTest(value=value):
                path.write_text(value)
                self.assertEqual(read_project_state(self.production['id']), {})
                self.assertEqual(path.read_text(), value)

    def test_file_symlink_cannot_read_or_overwrite_other_file(self):
        folder = self.root/'app'/'projects'
        folder.mkdir(parents=True)
        target = self.root/'outside.json'
        target.write_text('untouched')
        (folder/(self.production['id']+'.json')).symlink_to(target)
        for action in (lambda: write_project_state(self.production), lambda: read_project_state(self.production['id'])):
            with self.assertRaisesRegex(ValueError, '符号链接'):
                action()
        self.assertEqual(target.read_text(), 'untouched')

    def test_project_directory_symlink_is_rejected(self):
        app = self.root/'app'
        app.mkdir()
        outside = self.root/'outside'
        outside.mkdir()
        (app/'projects').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, '符号链接'):
            write_project_state(self.production)
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
