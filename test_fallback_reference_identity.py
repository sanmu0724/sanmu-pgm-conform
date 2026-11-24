"""Retired manual references cannot affect or revive PGM-only failure caches."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from library_workflow import BatchWorkflow
from sound_fallback import (fallback_signature, historical_sound_references,
                            retire_sound_references, sound_layout_settings,
                            sound_path_key_factory, valid_pgm_fallback_signature)


def historical_signature(config, path, with_layout=False, with_reference_dependency=False):
    """Recreate actual removed manual-reference schemas, never the new digest."""
    key = sound_path_key_factory()
    dependencies = []

    def identity(value):
        stat = Path(value).stat()
        return dict(path=key(str(Path(value).resolve())), size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns)

    for value in [path] + [row['path'] for row in config['sound_sources']]:
        dependencies.append(identity(value))
    references = {key(candidate): copy.deepcopy(row) for candidate, row in
                  config.get('sound_reference_overrides', {}).items()}
    for row in references.values():
        if row.get('path'):
            row['path'] = key(row['path'])
    reference = references.get(key(path))
    if with_reference_dependency and reference:
        reference_identity = identity(reference['path'])
        if reference_identity['path'] not in {row['path'] for row in dependencies}:
            dependencies.append(reference_identity)
    settings = dict(mode=config.get('sound_sync_mode', 'waveform'), reference=reference,
                    pgm_timecode=config.get('pgm_timecodes', {}).get(path, ''))
    if with_layout:
        settings.update(allow_gaps=config.get('sound_allow_gaps', True),
                        sound_layout=sound_layout_settings(config, key=key))
    payload = dict(version=1, dependencies=sorted(dependencies, key=lambda row: row['path']),
                   settings=settings)
    return hashlib.sha256(json.dumps(payload, sort_keys=True,
                                    ensure_ascii=False).encode()).hexdigest()


class FallbackReferenceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.pgm, self.wav, self.reference = [self.root / name for name in
                                            ('picture.mov', 'recorder.wav', 'reference.mov')]
        for path in (self.pgm, self.wav, self.reference):
            path.write_bytes(b'original file identity')
        settings = patch('library_bridge.read_settings', return_value={})
        settings.start()
        self.addCleanup(settings.stop)
        self.config = dict(pgm=str(self.pgm), sound_sync_mode='waveform',
            sound_sources=[dict(path=str(self.wav), channel='main', metadata={'ixml': {}})],
            sound_reference_overrides={str(self.pgm):
                dict(path=str(self.reference), start_seconds=1.25)})

    def saved(self, config=None, signature=None):
        config = config if config is not None else self.config
        return dict(status='fallback', stage='sync', audio_source='pgm', error='WAV did not match',
            signature=signature or fallback_signature(config, str(self.pgm)),
            pgm_info=dict(path=str(self.pgm), fps=25, frames=250,
                          audio_channels=[{'source_channel': 1}]))

    def assert_rejected(self, config, saved):
        self.assertFalse(valid_pgm_fallback_signature(config, str(self.pgm), saved))
        configured = copy.deepcopy(config)
        configured['library_sound_fallback'] = {str(self.pgm): saved}
        self.assertFalse(BatchWorkflow._reusable_pgm_fallback(configured, str(self.pgm)))

    def assert_unchanged_current_signature(self, saved):
        self.assertEqual(fallback_signature(self.config, str(self.pgm)), saved['signature'])
        self.assertTrue(valid_pgm_fallback_signature(self.config, str(self.pgm), saved))

    def test_retired_independent_reference_size_does_not_change_new_failure_hash(self):
        saved = self.saved()
        before = self.reference.stat()
        self.reference.write_bytes(b'replaced reference with additional audio')
        os.utime(self.reference, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assert_unchanged_current_signature(saved)

    def test_retired_independent_reference_mtime_does_not_change_new_failure_hash(self):
        saved = self.saved()
        before = self.reference.stat()
        os.utime(self.reference, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
        self.assert_unchanged_current_signature(saved)

    def test_missing_retired_reference_does_not_change_new_failure_hash(self):
        saved = self.saved()
        self.reference.unlink()
        self.assert_unchanged_current_signature(saved)
        self.assertEqual(self.saved()['signature'], saved['signature'])

    def test_all_true_old_manual_failure_schemas_are_rejected_after_retirement(self):
        # A failed match against another reference does not establish that this
        # PGM's own audio cannot match the WAV. It must not suppress a new try.
        for reference in (self.reference, self.pgm, self.wav):
            for layout in (False, True):
                for with_identity in (False, True):
                    with self.subTest(reference=reference.name, layout=layout,
                                      independent_identity=with_identity):
                        config = copy.deepcopy(self.config)
                        config['sound_reference_overrides'][str(self.pgm)]['path'] = str(reference)
                        signature = historical_signature(config, str(self.pgm), layout, with_identity)
                        saved = self.saved(config, signature)
                        self.assertNotEqual(signature, fallback_signature(config, str(self.pgm)))
                        self.assert_rejected(config, saved)
                        self.assert_rejected(retire_sound_references(config), saved)

    def test_current_pgm_failure_retains_asr_provenance_without_probing_old_reference(self):
        saved = self.saved()
        config = retire_sound_references(self.config)
        config['library_sound_fallback'] = {str(self.pgm): saved}
        config['library_asr'] = {str(self.pgm): dict(complete=True,
                                                   sync_signature=saved['signature'])}
        before = copy.deepcopy(config)
        session = dict(id='scene', config=config)
        workflow = BatchWorkflow(Mock(), 'show')
        with patch('library_workflow._video_info') as probe, \
                patch.object(workflow, '_save_state') as write, \
                patch('sound_fallback.historical_sound_references',
                      side_effect=AssertionError('current signature read old references')):
            returned, state = workflow._save_pgm_fallback(session, str(self.pgm), 'retry', lambda: False)
        self.assertIs(returned, session)
        self.assertEqual(state['signature'], saved['signature'])
        self.assertEqual(config, before)
        probe.assert_not_called()
        write.assert_not_called()

    def test_primary_pgm_and_registered_wav_still_invalidate_an_existing_hash(self):
        for source in (self.pgm, self.wav):
            with self.subTest(source=source.name):
                saved = self.saved()
                original = source.read_bytes()
                before = source.stat()
                try:
                    source.write_bytes(original + b' changed')
                    self.assert_rejected(self.config, saved)
                    source.unlink()
                    self.assert_rejected(self.config, saved)
                finally:
                    source.write_bytes(original)
                    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))

    def test_no_retired_reference_is_a_new_failure_dependency(self):
        other = self.root / 'other_reference.mov'
        other.write_bytes(b'another recording')
        config = copy.deepcopy(self.config)
        config['sound_reference_overrides']['/other/PGM.mov'] = dict(path=str(other), start_seconds=0)
        saved = self.saved(config)
        other.unlink()
        self.reference.unlink()
        self.assertEqual(fallback_signature(config, str(self.pgm)), saved['signature'])
        self.assertTrue(valid_pgm_fallback_signature(config, str(self.pgm), saved))

    def test_nas_aliases_share_pgm_dependencies_and_ignore_removed_old_reference(self):
        mappings = [dict(shared='/NAS/Show', local=str(self.root))]
        canonical = copy.deepcopy(self.config)
        canonical['pgm'] = '/NAS/Show/' + self.pgm.name
        canonical['sound_sources'][0]['path'] = '/NAS/Show/' + self.wav.name
        canonical['sound_reference_overrides'] = {canonical['pgm']:
            dict(path='/NAS/Show/' + self.reference.name, start_seconds=1.25)}
        with patch('library_bridge.read_settings', return_value={'path_mappings': mappings}):
            saved = self.saved()
            self.assertEqual(fallback_signature(canonical, canonical['pgm']), saved['signature'])
            self.reference.unlink()
            self.assertTrue(valid_pgm_fallback_signature(canonical, canonical['pgm'], saved))

    def test_unavailable_wav_does_not_change_current_pgm_fallback_policy(self):
        self.wav.unlink()
        saved = self.saved()
        self.assertTrue(valid_pgm_fallback_signature(self.config, str(self.pgm), saved))

    def test_retirement_merges_a_small_detached_history_without_mutating_config(self):
        config = copy.deepcopy(self.config)
        config['library_legacy_sound_references'] = {
            str(self.pgm): dict(path='/history/replaced.mov', start_seconds=0),
            '/other/PGM.mov': dict(path='/history/retained.mov', start_seconds=4)}
        config['library_asr'] = {'large-table': {'active_transcript_ids': ['t'] * 1000}}
        before = copy.deepcopy(config)
        history = historical_sound_references(config)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[str(self.pgm)], config['sound_reference_overrides'][str(self.pgm)])
        retired = retire_sound_references(config)
        self.assertNotIn('sound_reference_overrides', retired)
        self.assertEqual(retired['library_legacy_sound_references'], history)
        self.assertIs(retired['library_asr'], config['library_asr'])
        history[str(self.pgm)]['start_seconds'] = 99
        retired['library_legacy_sound_references'][str(self.pgm)]['path'] = '/changed/copy.mov'
        self.assertEqual(config, before)

    def test_retirement_without_old_references_preserves_the_plain_config(self):
        config = dict(pgm=str(self.pgm), library_asr={'one': {'complete': True}})
        before = copy.deepcopy(config)
        retired = retire_sound_references(config)
        self.assertEqual(retired, before)
        self.assertNotIn('library_legacy_sound_references', retired)
        self.assertIs(retired['library_asr'], config['library_asr'])


if __name__ == '__main__':
    unittest.main()
