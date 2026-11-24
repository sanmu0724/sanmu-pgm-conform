"""Old PGM-audio caches preserve provenance only for unchanged ordinary WAVs.

These tests use real temporary file identities and saved video headers. They do
not decode media, load a speech model, or attempt waveform alignment.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from library_workflow import BatchWorkflow
from sound_fallback import (fallback_signature, sound_path_key_factory,
                            valid_pgm_fallback_signature)


def old_fallback_signature(config, path):
    """The original on-disk schema predates sound_layout and allow_gaps."""
    key = sound_path_key_factory()
    dependencies = []
    for value in [path] + [row['path'] for row in config['sound_sources']]:
        stat = Path(value).stat()
        dependencies.append(dict(path=key(str(Path(value).resolve())),
                                 size=stat.st_size, mtime_ns=stat.st_mtime_ns))
    references = {key(candidate): copy.deepcopy(row) for candidate, row in
                  config.get('sound_reference_overrides', {}).items()}
    for row in references.values():
        if row.get('path'):
            row['path'] = key(row['path'])
    payload = dict(version=1, dependencies=sorted(dependencies, key=lambda row: row['path']),
                   settings=dict(mode=config.get('sound_sync_mode', 'waveform'),
                                 reference=references.get(key(path)),
                                 pgm_timecode=config.get('pgm_timecodes', {}).get(path, '')))
    return hashlib.sha256(json.dumps(payload, sort_keys=True,
                                    ensure_ascii=False).encode()).hexdigest()


class LegacyFallbackCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.pgm = self.root / 'picture.mov'
        self.wav = self.root / 'recorder.wav'
        self.pgm.write_bytes(b'saved pgm identity')
        self.wav.write_bytes(b'saved wav identity')
        self.settings = patch('library_bridge.read_settings', return_value={})
        self.settings.start()
        self.addCleanup(self.settings.stop)
        # Explicitly known empty iXML is required for the conservative legacy
        # guard. Missing metadata cannot establish an ordinary unpaired WAV.
        self.config = dict(pgm=str(self.pgm), sound_sync_mode='waveform',
                           sound_unmatched_policy='pgm', library_sound_cache_only=True,
                           sound_sources=[dict(path=str(self.wav), channel='main',
                                               metadata={'ixml': {}})])
        self.info = dict(path=str(self.pgm), fps=25, frames=250, start_tc='12:00:00:00',
                         audio_channels=[{'source_channel': 1}])
        self.saved = self.saved_fallback(self.config)

    def saved_fallback(self, config, current=False):
        signature = (fallback_signature(config, str(self.pgm)) if current else
                     old_fallback_signature(config, str(self.pgm)))
        return dict(version=1, status='fallback', stage='sync', audio_source='pgm',
                    signature=signature, pgm_path=str(self.pgm),
                    error='original WAV did not match', pgm_info=copy.deepcopy(self.info))

    def configured(self, config=None, saved=None):
        config = copy.deepcopy(config if config is not None else self.config)
        saved = copy.deepcopy(saved if saved is not None else self.saved)
        config['library_sound_fallback'] = {str(self.pgm): saved}
        config['library_processing'] = {str(self.pgm): copy.deepcopy(saved)}
        config['library_asr'] = {str(self.pgm): dict(complete=True,
            sync_signature=saved['signature'], active_transcript_ids=['existing-dialogue'])}
        return config

    def assert_rejected(self, config, saved=None):
        saved = saved if saved is not None else self.saved
        self.assertFalse(valid_pgm_fallback_signature(config, str(self.pgm), saved))
        self.assertFalse(BatchWorkflow._reusable_pgm_fallback(
            self.configured(config, saved), str(self.pgm)))

    def test_old_schema_reuses_without_rewriting_asr_or_fallback_identity(self):
        config = self.configured()
        before = copy.deepcopy(config)
        self.assertNotEqual(fallback_signature(config, str(self.pgm)), self.saved['signature'])
        self.assertTrue(valid_pgm_fallback_signature(config, str(self.pgm), self.saved))
        self.assertTrue(BatchWorkflow._reusable_pgm_fallback(config, str(self.pgm)))
        session = dict(id='scene', name='scene', revision=1, config=config, segments=[])
        workflow = BatchWorkflow(Mock(), 'show')
        with patch('library_workflow._video_info') as probe, \
                patch('library_sync.ensure_synced_pgm') as align, \
                patch.object(workflow, '_save_state') as write:
            returned, state = workflow._save_pgm_fallback(
                session, str(self.pgm), 'retry must retain original evidence', lambda: False)
        self.assertIs(returned, session)
        self.assertEqual(state, self.saved)
        self.assertEqual(config, before)
        probe.assert_not_called()
        align.assert_not_called()
        write.assert_not_called()

    def test_sync_button_uses_signed_legacy_fallback_without_probe_or_realign(self):
        config = self.configured()
        before = copy.deepcopy(config)
        session = dict(id='scene', name='scene', revision=1, config=config, segments=[])
        client = Mock()
        client.call.return_value = session
        workflow = BatchWorkflow(client, 'show')
        with patch.object(workflow, '_recording_sessions', return_value=[session]), \
                patch.object(workflow, 'read_all', return_value={'sessions': [session]}), \
                patch.object(workflow, '_save_state') as write, \
                patch('library_workflow._video_info') as probe, \
                patch('library_sync.valid_sync', return_value=False), \
                patch('library_sync.ensure_synced_pgm') as align:
            result = workflow.sync_recording(session_id='scene')
        self.assertEqual(result['fallback'], 1)
        self.assertEqual(result['failures'], [])
        self.assertTrue(result['fallback_rows'][0]['reused'])
        self.assertEqual(config, before)
        self.assertEqual(config['library_asr'][str(self.pgm)]['sync_signature'],
                         self.saved['signature'])
        self.assertEqual(client.call.call_args_list[0].args, ('get_session',))
        self.assertEqual(client.call.call_count, 1)
        probe.assert_not_called()
        align.assert_not_called()
        write.assert_not_called()

    def test_existing_asr_keeps_old_provenance_and_skips_recognition(self):
        config = self.configured()
        stat = self.pgm.stat()
        config['library_asr'][str(self.pgm)].update(
            asr_model='small', asr_language='auto', asr_audio_source='pgm_reference',
            source_stat=dict(size=stat.st_size, mtime_ns=stat.st_mtime_ns))
        before = copy.deepcopy(config)
        session = dict(id='scene', name='scene', revision=1, config=config, segments=[])
        client = Mock()
        client.call.side_effect = lambda method, **params: [] if method == 'list_transcripts' else session
        workflow = BatchWorkflow(client, 'show')
        with patch.object(workflow, '_recording_sessions', return_value=[session]), \
                patch.object(workflow, 'read_all', return_value={'sessions': [session],
                    'scripts': [], 'transcripts': []}), \
                patch.object(workflow, '_save_script_run', return_value=session), \
                patch.object(workflow, '_apply_auto_review', return_value=0), \
                patch.object(workflow, '_save_state', return_value=session) as write, \
                patch('library_workflow._video_info') as probe, \
                patch('library_sync.ensure_synced_pgm') as align, \
                patch('library_sync.transcribe_synced_pgm') as wav_asr, \
                patch('dialogue_engine.transcribe_media') as pgm_asr:
            result = workflow.recognize_and_match(require_synced=True)
        self.assertEqual(result['recognized'], 0)
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(result['failures'], [])
        self.assertEqual(config, before)
        write.assert_called_once_with('scene', 'library_processing', str(self.pgm), None)
        probe.assert_not_called()
        align.assert_not_called()
        wav_asr.assert_not_called()
        pgm_asr.assert_not_called()

    def test_pgm_or_wav_size_change_invalidates_even_with_original_mtime(self):
        for path in (self.pgm, self.wav):
            with self.subTest(file=path.name):
                before = path.stat()
                original = path.read_bytes()
                try:
                    path.write_bytes(original + b' appended data')
                    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
                    self.assert_rejected(self.config)
                finally:
                    path.write_bytes(original)
                    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    def test_pgm_or_wav_mtime_change_invalidates_without_size_change(self):
        for path in (self.pgm, self.wav):
            with self.subTest(file=path.name):
                before = path.stat()
                try:
                    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
                    self.assert_rejected(self.config)
                finally:
                    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    def test_missing_dependency_invalidates_old_cache(self):
        for path in (self.pgm, self.wav):
            with self.subTest(file=path.name):
                original = path.read_bytes()
                before = path.stat()
                try:
                    path.unlink()
                    self.assert_rejected(self.config)
                finally:
                    path.write_bytes(original)
                    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    def test_mode_timecode_and_wav_membership_changes_invalidate(self):
        extra = self.root / 'another.wav'
        extra.write_bytes(b'another recording')
        mutations = [
            ('mode', {'sound_sync_mode': 'timecode'}),
            ('timecode', {'pgm_timecodes': {str(self.pgm): '13:00:00:00'}}),
            ('add_wav', {'sound_sources': self.config['sound_sources'] +
                [dict(path=str(extra), metadata={'ixml': {}})]}),
            ('remove_wav', {'sound_sources': []}),
            ('duplicate_wav', {'sound_sources': self.config['sound_sources'] * 2}),
        ]
        for label, changes in mutations:
            with self.subTest(change=label):
                changed = copy.deepcopy(self.config)
                changed.update(copy.deepcopy(changes))
                self.assert_rejected(changed)

    def test_retired_reference_does_not_change_a_pgm_only_fallback_hash(self):
        for field in ('sound_reference_overrides', 'library_legacy_sound_references'):
            changed = copy.deepcopy(self.config)
            changed[field] = {str(self.pgm):
                dict(path='/unmounted/retired-reference.mov', start_seconds=-1)}
            before = copy.deepcopy(changed)
            self.assertEqual(fallback_signature(changed, str(self.pgm)),
                             fallback_signature(self.config, str(self.pgm)))
            for saved in (self.saved, self.saved_fallback(changed, current=True)):
                self.assertTrue(valid_pgm_fallback_signature(changed, str(self.pgm), saved))
                self.assertTrue(BatchWorkflow._reusable_pgm_fallback(
                    self.configured(changed, saved), str(self.pgm)))
            self.assertEqual(changed, before)

    def test_true_old_manual_reference_failure_is_not_reused(self):
        changed = copy.deepcopy(self.config)
        changed['sound_reference_overrides'] = {str(self.pgm):
            dict(path=str(self.wav), start_seconds=1)}
        old_manual_failure = self.saved_fallback(changed)
        self.assertNotEqual(old_manual_failure['signature'], self.saved['signature'])
        self.assert_rejected(changed, old_manual_failure)

    def test_manual_roles_pairs_and_potential_recorder_file_sets_are_rejected(self):
        mutations = [
            ('sound_role_main', {'sound_role': 'main'}),
            ('sound_role_L', {'sound_role': 'L'}),
            ('sound_role_R', {'sound_role': 'R'}),
            ('channel_L', {'channel': 'L'}),
            ('channel_R', {'channel': 'R'}),
            ('sound_pair', {'sound_pair': 'manual-pair'}),
            ('pair_id', {'pair_id': 'paired'}),
            ('out_channel', {'out_channel': 1}),
            ('alignment_source_path', {'alignment_source_path': str(self.wav)}),
            ('family_uid', {'family_uid': 'recorder-family'}),
            ('file_set_index', {'file_set_index': 1}),
        ]
        for field in ('FILE_SET/FAMILY_UID', 'FAMILY_UID',
                      'FILE_SET/FILE_SET_INDEX', 'FILE_SET_INDEX'):
            mutations.append((field, {'metadata': {'ixml': {field: '1'}}}))
        for label, changes in mutations:
            with self.subTest(change=label):
                changed = copy.deepcopy(self.config)
                changed['sound_sources'][0].update(copy.deepcopy(changes))
                # The original hash knows none of these layout inputs. A hash
                # match alone must not bypass the explicit conservative guard.
                self.assertEqual(old_fallback_signature(changed, str(self.pgm)),
                                 self.saved['signature'])
                self.assert_rejected(changed)

    def test_filename_channel_hints_are_rejected_even_when_old_hash_matches(self):
        for name in ('take_L.wav', 'take_R.wav', 'take_ch1.wav', 'take_track2.wav', 'take_1.wav'):
            with self.subTest(filename=name):
                hinted = self.root / name
                hinted.write_bytes(b'hinted recording')
                changed = copy.deepcopy(self.config)
                changed['sound_sources'][0]['path'] = str(hinted)
                self.assert_rejected(changed, self.saved_fallback(changed))

    def test_missing_or_unknown_metadata_is_rejected_for_legacy_schema(self):
        for metadata in (None, {}, {'ixml': None}, {'ixml': []}):
            with self.subTest(metadata=metadata):
                changed = copy.deepcopy(self.config)
                changed['sound_sources'][0]['metadata'] = metadata
                self.assert_rejected(changed)
        changed = copy.deepcopy(self.config)
        changed['sound_sources'][0].pop('metadata')
        self.assert_rejected(changed)

    def test_current_schema_remains_valid_without_legacy_metadata_guard(self):
        changed = copy.deepcopy(self.config)
        changed['sound_sources'][0].pop('metadata')
        saved = self.saved_fallback(changed, current=True)
        config = self.configured(changed, saved)
        self.assertTrue(valid_pgm_fallback_signature(config, str(self.pgm), saved))
        self.assertTrue(BatchWorkflow._reusable_pgm_fallback(config, str(self.pgm)))
        before = copy.deepcopy(config)
        workflow = BatchWorkflow(Mock(), 'show')
        session = dict(id='scene', config=config)
        with patch('library_workflow._video_info') as probe, \
                patch.object(workflow, '_save_state') as write:
            returned, state = workflow._save_pgm_fallback(session, str(self.pgm), '', lambda: False)
        self.assertIs(returned, session)
        self.assertEqual(state['signature'], saved['signature'])
        self.assertEqual(config, before)
        probe.assert_not_called()
        write.assert_not_called()

    def test_disabled_pgm_policy_blocks_reuse_of_otherwise_valid_signature(self):
        config = self.configured()
        config['sound_unmatched_policy'] = 'error'
        # Source signature and policy eligibility are deliberately separate.
        self.assertTrue(valid_pgm_fallback_signature(config, str(self.pgm), self.saved))
        self.assertFalse(BatchWorkflow._reusable_pgm_fallback(config, str(self.pgm)))
        self.assertFalse(BatchWorkflow._reusable_pgm_fallback(config, str(self.pgm), allow_legacy=True))


if __name__ == '__main__':
    unittest.main()
