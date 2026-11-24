"""Reuse ordinary pre-split-channel caches without weakening identity checks."""
import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from library_sync import (SyncValidationIndex, _settings, _signature, ensure_synced_pgm,
                          legacy_mono_signature_compatible, valid_sync)
from sound_sync import probe_wav
from test_waveform_sync import RATE, speech_like, write_audio, write_pgm


class LegacySyncCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name).resolve()
        signal = speech_like(10, 231)
        cls.wav = cls.root / 'RECORDER_MAIN.wav'
        cls.pgm = cls.root / 'PGM.mov'
        write_audio(cls.wav, signal)
        write_pgm(cls.pgm, signal[2 * RATE:8 * RATE])
        cls.config = dict(fps=25, sound_sync_mode='waveform', sound_allow_gaps=True,
                          sound_sources=[dict(path=str(cls.wav), channel='main',
                                              metadata=probe_wav(cls.wav)['metadata'])])
        with patch('library_bridge.read_settings', return_value={}):
            cls.current = ensure_synced_pgm(cls.config, str(cls.pgm))
            cls.legacy = copy.deepcopy(cls.current)
            settings = _settings(cls.config, str(cls.pgm), {str(cls.wav)})
            settings.pop('sound_layout')
            cls.legacy['signature'] = _signature(cls.legacy['dependencies'], settings)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.settings = patch('library_bridge.read_settings', return_value={})
        self.settings.start()
        self.addCleanup(self.settings.stop)

    def assertInvalid(self, config, state=None):
        state = self.legacy if state is None else state
        self.assertFalse(valid_sync(config, str(self.pgm), state))
        self.assertFalse(valid_sync(config, str(self.pgm), state, index=SyncValidationIndex(config)))

    def test_generated_ordinary_cache_reuses_legacy_signature_without_mutation(self):
        before = copy.deepcopy(self.legacy)
        self.assertNotEqual(self.legacy['signature'], self.current['signature'])
        self.assertTrue(valid_sync(self.config, str(self.pgm), self.legacy))
        self.assertTrue(valid_sync(self.config, str(self.pgm), self.legacy,
                                   index=SyncValidationIndex(self.config)))
        config = dict(self.config, library_sync={str(self.pgm): self.legacy})
        with patch('library_sync.sound_plan', side_effect=AssertionError('unexpected realignment')):
            self.assertEqual(ensure_synced_pgm(config, str(self.pgm)), self.legacy)
        self.assertEqual(self.legacy, before)
        self.assertTrue(valid_sync(self.config, str(self.pgm), self.current))

    def test_changed_mode_gaps_or_pgm_timecode_still_invalidate(self):
        for changes in (dict(sound_sync_mode='timecode'), dict(sound_allow_gaps=False),
                        dict(pgm_timecodes={str(self.pgm): '01:00:00:00'})):
            with self.subTest(changes=changes):
                self.assertInvalid(dict(self.config, **changes))

    def test_retired_references_do_not_invalidate_current_or_ordinary_old_pgm_hash(self):
        for field in ('sound_reference_overrides', 'library_legacy_sound_references'):
            config = dict(self.config, **{field: {str(self.pgm):
                dict(path='/unmounted/old-reference.mov', start_seconds=-4)}})
            before = copy.deepcopy(config)
            for state in (self.current, self.legacy):
                with self.subTest(field=field, signature=state['signature']):
                    self.assertTrue(valid_sync(config, str(self.pgm), state))
                    self.assertTrue(valid_sync(config, str(self.pgm), state,
                                               index=SyncValidationIndex(config)))
            self.assertEqual(config, before)

    def test_modified_or_missing_files_and_unregistered_used_wav_still_invalidate(self):
        self.assertInvalid(dict(self.config, sound_sources=[]))
        for path in (self.pgm, self.wav):
            before = path.stat()
            try:
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000))
                self.assertInvalid(self.config)
            finally:
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            hidden = path.with_suffix(path.suffix + '.hidden')
            path.rename(hidden)
            try:
                self.assertInvalid(self.config)
            finally:
                hidden.rename(path)

    def test_partial_coverage_still_checks_all_candidate_dependencies(self):
        state = copy.deepcopy(self.legacy)
        state['uncovered'] = [dict(start_seconds=0, end_seconds=.25)]
        self.assertTrue(valid_sync(self.config, str(self.pgm), state))
        extra = self.root / 'UNRELATED_MAIN.wav'
        extra.write_bytes(b'new candidate')
        self.assertInvalid(dict(self.config, sound_sources=[*self.config['sound_sources'],
            dict(path=str(extra), channel='main')]), state)
        state['candidate_dependencies'][0]['size'] += 1
        self.assertInvalid(self.config, state)

    def test_manual_roles_pairs_and_old_pair_placements_are_not_compatible(self):
        for fields in (dict(sound_role='main'), dict(sound_role='L'), dict(channel='R'),
                       dict(sound_pair='manual'), dict(pair_id='paired'),
                       dict(sound_role='invalid')):
            with self.subTest(fields=fields):
                self.assertInvalid(dict(self.config, sound_sources=[
                    dict(self.config['sound_sources'][0], **fields)]))
        for fields in (dict(pair_id='paired'), dict(channel='L'), dict(out_channel='R'),
                       dict(alignment_source_path=str(self.wav))):
            with self.subTest(placement=fields):
                state = copy.deepcopy(self.legacy)
                state['placements'][0].update(fields)
                self.assertInvalid(self.config, state)

    def test_automatic_filename_and_recorder_metadata_hints_reject_legacy_hash(self):
        for fields in (dict(family_uid='family'), dict(file_set_index='1'),
                       dict(metadata=dict(ixml={'FILE_SET/FAMILY_UID': 'family'})),
                       dict(metadata=dict(ixml={'FAMILY_UID': 'family'})),
                       dict(metadata=dict(ixml={'FILE_SET/FILE_SET_INDEX': '1'})),
                       dict(metadata=dict(ixml={'FILE_SET_INDEX': '1'}))):
            with self.subTest(fields=fields):
                self.assertInvalid(dict(self.config, sound_sources=[
                    dict(self.config['sound_sources'][0], **fields)]))
        for suffix in ('L', 'R', 'ch1', 'track2', '1'):
            source = dict(path=str(self.root / ('TAKE_' + suffix + '.wav')), channel='main',
                          metadata=dict(ixml={}))
            self.assertFalse(legacy_mono_signature_compatible(dict(sound_sources=[source])))
        self.assertTrue(legacy_mono_signature_compatible(dict(sound_sources=[
            dict(self.config['sound_sources'][0], sound_role='auto')])))

    def test_malformed_config_and_wrong_old_hash_are_rejected(self):
        for sources in (None, [], {}, ['bad'], [dict(path='')], [dict(path=str(self.wav))],
                        [dict(path=str(self.wav), metadata='bad')],
                        self.config['sound_sources'] * 2):
            with self.subTest(sources=sources):
                self.assertFalse(legacy_mono_signature_compatible(dict(sound_sources=sources)))
        state = copy.deepcopy(self.legacy)
        state['signature'] = '0' * 64
        self.assertInvalid(self.config, state)


if __name__ == '__main__':
    unittest.main()
