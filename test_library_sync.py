"""Verify sync-first transcription with decoded signals and original WAV clocks."""
import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import AnalysisError
from library_sync import ensure_synced_pgm, valid_sync, transcribe_synced_pgm
from test_waveform_sync import RATE, speech_like, write_audio, write_pgm


class LibrarySyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name).resolve()
        cls.original = speech_like(22, 552)
        cls.wav = cls.root / 'MAIN_19h.wav'
        write_audio(cls.wav, cls.original, start_sample=19*3600*RATE)
        cls.pgm = cls.root / 'PGM_05h.mov'
        write_pgm(cls.pgm, cls.original[3*RATE:21*RATE])
        cls.config = dict(fps=25, sound_sync_mode='waveform', sound_allow_gaps=True,
                          sound_sources=[{'path': str(cls.wav), 'channel': 'main'}])
        with patch('library_bridge.read_settings', return_value={}):
            cls.sync = ensure_synced_pgm(cls.config, str(cls.pgm))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.settings = patch('library_bridge.read_settings', return_value={})
        self.settings.start()

    def tearDown(self):
        self.settings.stop()

    def test_real_waveform_overrides_conflicting_clocks_preserving_identity(self):
        sync = self.sync
        self.assertEqual(sync['pgm_info']['fps'], 25)
        self.assertEqual(sync['pgm_info']['start_tc'], '05:00:00:00')
        row = sync['placements'][0]
        self.assertEqual(row['method'], 'waveform')
        self.assertAlmostEqual(row['offset_seconds'], -3, delta=.0025)
        self.assertAlmostEqual(row['wav_start_seconds'], 3, delta=.0025)
        self.assertEqual(row['wav_info']['start_tc'], '19:00:00:00')
        self.assertEqual(row['wav_info']['start_samples'], 19*3600*RATE)
        self.assertEqual(row['wav_info']['recording_date'], '2026-10-11')
        self.assertEqual(row['wav_path'], str(self.wav))

    def test_transcription_uses_original_wav_and_maps_only_actual_coverage(self):
        words = [{'start_seconds': 5, 'end_seconds': 7, 'text': 'Hello，你好'},
                 {'start_seconds': 21.5, 'end_seconds': 22, 'text': 'outside'}]
        with patch('dialogue_engine.transcribe_media', return_value=words) as asr:
            result = transcribe_synced_pgm(self.config, str(self.pgm), sync=self.sync)
        self.assertEqual(asr.call_args.args[0], str(self.wav))
        self.assertAlmostEqual(asr.call_args.kwargs['start_seconds'], 3, delta=.0025)
        self.assertAlmostEqual(asr.call_args.kwargs['duration'], 18, delta=.0025)
        self.assertEqual(len(result['segments']), 1)
        segment = result['segments'][0]
        self.assertEqual(segment['text'], 'Hello，你好')
        self.assertAlmostEqual(segment['start_seconds'], 2, delta=.0025)
        self.assertAlmostEqual(segment['end_seconds'], 4, delta=.0025)
        self.assertAlmostEqual(segment['original_audio_start_seconds'], 5, delta=.0025)

    def test_incremental_unrelated_media_keeps_valid_sync_without_recomputation(self):
        config = copy.deepcopy(self.config)
        config['library_sync'] = {str(self.pgm): self.sync}
        config['sound_sources'].append({'path': str(self.root/'NEW_UNRELATED.wav'), 'channel': 'main'})
        config['sources'] = [{'path': '/NAS/new_B.mov', 'camera': 'B'}]
        config['library_notes'] = '场记新备注'
        self.assertTrue(valid_sync(config, str(self.pgm), self.sync))
        with patch('library_sync.sound_plan', side_effect=AssertionError('cache not reused')):
            self.assertEqual(ensure_synced_pgm(config, str(self.pgm)), self.sync)

    def test_removed_used_wav_and_changed_settings_invalidate_sync(self):
        for change in ({'sound_sources': []}, {'sound_sync_mode': 'timecode'}):
            with self.subTest(change=change):
                config = dict(self.config, **change)
                self.assertFalse(valid_sync(config, str(self.pgm), self.sync))
        previous = self.wav.stat()
        try:
            os.utime(self.wav, ns=(previous.st_atime_ns, previous.st_mtime_ns+1000000))
            self.assertFalse(valid_sync(self.config, str(self.pgm), self.sync))
        finally:
            os.utime(self.wav, ns=(previous.st_atime_ns, previous.st_mtime_ns))

    def test_export_silence_policy_upgrade_reuses_existing_waveform_sync(self):
        config = dict(self.config, sound_allow_gaps=False, sound_unmatched_policy='silence')
        before = copy.deepcopy(self.sync)
        self.assertTrue(valid_sync(config, str(self.pgm), self.sync))
        config['library_sync'] = {str(self.pgm): self.sync}
        with patch('library_sync.sound_plan', side_effect=AssertionError('unnecessary resync')):
            self.assertEqual(ensure_synced_pgm(config, str(self.pgm)), self.sync)
        self.assertEqual(self.sync, before)

    def test_positive_offset_clamps_gap_and_keeps_original_source_positions(self):
        wav, pgm = self.root/'LATE_MAIN.wav', self.root/'EARLY_PGM.mov'
        write_audio(wav, self.original[4*RATE:20*RATE])
        write_pgm(pgm, self.original)
        config = dict(self.config, sound_sources=[{'path':str(wav),'channel':'main'}])
        sync = ensure_synced_pgm(config, str(pgm))
        row = sync['placements'][0]
        self.assertAlmostEqual(row['offset_seconds'], 4, delta=.0025)
        self.assertTrue(sync['uncovered'])
        self.assertTrue(valid_sync(config, str(pgm), sync))
        supplemented = copy.deepcopy(config)
        supplemented['sound_sources'].append({'path': str(self.wav), 'channel': 'main'})
        self.assertFalse(valid_sync(supplemented, str(pgm), sync))
        self.assertEqual(row['wav_info']['start_tc'], '')
        with patch('dialogue_engine.transcribe_media', return_value=[
                {'start_seconds':0,'end_seconds':1,'text':'first'}]):
            result = transcribe_synced_pgm(config, str(pgm), sync=sync)
        self.assertAlmostEqual(result['segments'][0]['start_seconds'], 4, delta=.0025)

    def test_no_wav_unsupported_rate_and_cancel_fail_before_asr(self):
        with self.assertRaisesRegex(AnalysisError, '主WAV'):
            ensure_synced_pgm({'fps':25}, str(self.pgm))
        with patch('library_sync.probe', return_value={'fps':'30000/1001'}):
            with self.assertRaisesRegex(AnalysisError, '24/25/30'):
                ensure_synced_pgm(self.config, str(self.pgm))
        with self.assertRaisesRegex(AnalysisError, '取消'):
            ensure_synced_pgm(self.config, str(self.pgm), cancelled=lambda:True)


if __name__ == '__main__':
    unittest.main()
