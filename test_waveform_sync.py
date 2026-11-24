"""Actual decoded audio alignment fixtures; no alignment implementation is mocked."""
import copy
import hashlib
import shutil
import struct
import tempfile
import unittest
from fractions import Fraction as F
from pathlib import Path
from collections import OrderedDict
from unittest.mock import MagicMock, patch

import av
import numpy as np

from core import AnalysisError
from sound_sync import probe_wav, sound_plan
from waveform_sync import align_audio

RATE = 48000


def speech_like(seconds, seed):
    """Deterministic changing harmonics, syllable envelopes and broadband consonants."""
    rng = np.random.default_rng(seed)
    count = round(seconds * RATE)
    t = np.arange(count, dtype=np.float64) / RATE
    controls = np.arange(0, seconds + .3, .12)
    envelope = np.interp(t, controls, rng.uniform(.06, 1, len(controls)))
    frequency = np.interp(t, controls, rng.uniform(110, 340, len(controls)))
    phase = np.cumsum(frequency) * (2 * np.pi / RATE)
    signal = .46 * np.sin(phase) + .23 * np.sin(phase * 2.13) + .14 * np.sin(phase * 5.61)
    noise = rng.standard_normal(count)
    noise = np.convolve(noise, np.ones(5) / 5, mode='same')
    signal = envelope * (signal + .20 * noise)
    return np.clip(signal * 20000, -32767, 32767).astype(np.int16)


def write_audio(path, samples, start_sample=None):
    """PyAV PCM encode plus optional genuine BWF TimeReference header."""
    samples = np.asarray(samples, dtype=np.int16)
    layout = 'mono' if samples.ndim == 1 else 'stereo'
    with av.open(str(path), 'w', format='wav') as out:
        audio = out.add_stream('pcm_s16le', rate=RATE); audio.layout = layout
        for start in range(0, len(samples), 4096):
            frame = av.AudioFrame.from_ndarray(samples[start:start + 4096].reshape(1, -1), format='s16', layout=layout)
            frame.sample_rate = RATE; frame.pts = start; frame.time_base = F(1, RATE)
            for packet in audio.encode(frame): out.mux(packet)
        for packet in audio.encode(): out.mux(packet)
    if start_sample is not None:
        raw = Path(path).read_bytes(); bext = bytearray(602)
        struct.pack_into('<Q', bext, 338, start_sample)
        bext[320:330] = b'2026-10-11'; bext[330:338] = b'19:00:00'
        chunk = b'bext' + struct.pack('<I', len(bext)) + bext
        body = b'WAVE' + chunk + raw[12:]
        Path(path).write_bytes(b'RIFF' + struct.pack('<I', len(body)) + body)


def write_pgm(path, samples=None, duration=None, timecode='05:00:00:00'):
    duration = len(samples) / RATE if samples is not None else duration
    with av.open(str(path), 'w', format='mov') as out:
        out.metadata['timecode'] = timecode
        video = out.add_stream('libx264', rate=25); video.width = 32; video.height = 24; video.pix_fmt = 'yuv420p'
        audio = None
        layout = 'mono' if samples is None or np.asarray(samples).ndim == 1 else 'stereo'
        if samples is not None:
            audio = out.add_stream('pcm_s16le', rate=RATE); audio.layout = layout
        for index in range(round(duration * 25)):
            frame = av.VideoFrame.from_ndarray(np.full((24, 32, 3), index % 255, np.uint8), format='rgb24')
            frame.pts = index
            for packet in video.encode(frame): out.mux(packet)
        for packet in video.encode(): out.mux(packet)
        if audio is not None:
            for start in range(0, len(samples), 4096):
                frame = av.AudioFrame.from_ndarray(np.asarray(samples[start:start + 4096], dtype=np.int16).reshape(1, -1), format='s16', layout=layout)
                frame.sample_rate = RATE; frame.pts = start; frame.time_base = F(1, RATE)
                for packet in audio.encode(frame): out.mux(packet)
            for packet in audio.encode(): out.mux(packet)


class WaveformSyncTests(unittest.TestCase):
    def test_band_correlation_matches_original_normalization(self):
        import waveform_sync as engine
        rng = np.random.default_rng(20261012)
        for channels in (1, 5):
            a = rng.normal(size=(813, channels)).astype(np.float32)
            b = rng.normal(size=(619, channels)).astype(np.float32)
            lags, actual, lengths = engine._correlations(a, b, 20)
            numerator = sum(np.correlate(a[:, col].astype(np.float64), b[:, col].astype(np.float64), 'full')
                            for col in range(channels))
            a0, b0 = np.maximum(lags, 0), np.maximum(-lags, 0)
            ae = np.r_[0., np.cumsum(np.sum(a * a, axis=1), dtype=np.float64)]
            be = np.r_[0., np.cumsum(np.sum(b * b, axis=1), dtype=np.float64)]
            denominator = np.sqrt(np.maximum((ae[a0 + lengths] - ae[a0]) * (be[b0 + lengths] - be[b0]), 1e-15))
            expected = np.clip(numerator / denominator, -1, 1)
            expected[lengths < 20] = -1
            np.testing.assert_allclose(actual, expected, atol=2e-7, rtol=2e-6)

    def test_many_clip_feature_reuse_is_bounded_and_invalidates_modified_audio(self):
        import waveform_sync as engine
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / f'take_{i}.wav' for i in range(16)]
            engine._features.clear()
            for path in paths:
                shutil.copyfile(self.wav, path)
                engine._feature_data(path, 0, lambda *args: None, lambda: False)
            with patch.object(engine, '_samples', side_effect=AssertionError('duplicate decode')):
                for path in paths:
                    engine._feature_data(path, 0, lambda *args: None, lambda: False)
            write_audio(paths[0], self.second)
            updated = engine._feature_data(paths[0], 0, lambda *args: None, lambda: False)
            self.assertEqual(updated['duration'], 20)
            self.assertFalse(updated['features'].flags.writeable)
            with patch.object(engine, 'FEATURE_CACHE_BYTES', updated['features'].nbytes):
                engine._feature_data(self.wav, 0, lambda *args: None, lambda: False)
                self.assertLessEqual(sum(row['features'].nbytes for row in engine._features.values()), engine.FEATURE_CACHE_BYTES)
            engine._features.clear()

    def test_spectrum_reuse_does_not_cache_mutable_refinement_samples(self):
        import waveform_sync as engine
        a = np.arange(400, dtype=np.float32).reshape(80, 5)
        engine._spectra.clear()
        engine._correlations(a, a, 20, cache=True)
        self.assertFalse(engine._spectra)
        a.flags.writeable = False
        first = engine._correlations(a, a, 20, cache=True)
        with patch.object(np.fft, 'rfft', side_effect=AssertionError('duplicate forward FFT')):
            second = engine._correlations(a, a, 20, cache=True)
        np.testing.assert_array_equal(first[1], second[1])
        self.assertLessEqual(sum(x.nbytes + y.nbytes for x, y in engine._spectra.values()), engine.SPECTRUM_CACHE_BYTES)

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(); cls.directory = Path(cls.temp.name)
        cls.original = speech_like(22, 552)
        cls.wav = cls.directory / 'ORIGINAL_UNTIMED_MAIN.wav'; write_audio(cls.wav, cls.original)
        cls.bwf = cls.directory / 'ORIGINAL_TC_19h.wav'; write_audio(cls.bwf, cls.original, start_sample=19 * 3600 * RATE)
        cls.pgm = cls.directory / 'PGM_CLOCK_05h.mov'; write_pgm(cls.pgm, cls.original[3 * RATE:21 * RATE])
        cls.second = speech_like(20, 773)
        cls.wav2 = cls.directory / 'ORIGINAL_MAIN_2.wav'; write_audio(cls.wav2, cls.second)
        cls.pgm2 = cls.directory / 'PGM_2.mov'; write_pgm(cls.pgm2, cls.second[2 * RATE:18 * RATE])

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def test_negative_offset_is_precise_and_uses_multiple_anchors(self):
        result = align_audio(self.pgm, self.wav)
        self.assertAlmostEqual(result['offset_seconds'], -3, delta=.0025)
        self.assertGreater(result['score'], .6); self.assertGreaterEqual(len(result['anchors']), 2)
        self.assertEqual(result['method'], 'waveform')

    def test_positive_offset_is_precise(self):
        with tempfile.TemporaryDirectory() as temp:
            sound = Path(temp) / 'LATE_STARTED_SOUND.wav'
            write_audio(sound, self.original[4 * RATE:20 * RATE])
            reference = Path(temp) / 'PGM_STARTS_FIRST.mov'; write_pgm(reference, self.original)
            result = align_audio(reference, sound)
            self.assertAlmostEqual(result['offset_seconds'], 4, delta=.0025)

    def test_conflicting_timecodes_do_not_override_waveform_match(self):
        result = align_audio(self.pgm, self.bwf)
        self.assertAlmostEqual(result['offset_seconds'], -3, delta=.0025)
        self.assertEqual(probe_wav(self.bwf)['start'], 19 * 3600 * RATE)
        self.assertEqual(probe_wav(self.bwf)['recording_date'], '2026-10-11')

    def test_nonzero_reference_region_returns_full_file_coordinate(self):
        result = align_audio(self.pgm, self.wav, reference_start=4, reference_duration=10)
        self.assertAlmostEqual(result['offset_seconds'], -3, delta=.0025)

    def test_gain_and_small_noise_preserve_real_sync(self):
        with tempfile.TemporaryDirectory() as temp:
            rng = np.random.default_rng(839)
            samples = self.original[3 * RATE:21 * RATE].astype(np.float64)
            samples = np.clip(samples * .45 + rng.normal(0, 30, len(samples)), -32767, 32767).astype(np.int16)
            reference = Path(temp) / 'GAIN_AND_NOISE.mov'; write_pgm(reference, samples)
            result = align_audio(reference, self.wav)
            self.assertAlmostEqual(result['offset_seconds'], -3, delta=.0025)

    def test_opposite_polarity_channels_are_not_cancelled_by_a_downmix(self):
        with tempfile.TemporaryDirectory() as temp:
            reference = Path(temp) / 'OPPOSITE_CHANNELS.mov'
            excerpt = self.original[3 * RATE:21 * RATE]
            write_pgm(reference, np.column_stack((excerpt, -excerpt)))
            sound = Path(temp) / 'OPPOSITE_CHANNELS.wav'
            write_audio(sound, np.column_stack((self.original, -self.original)))
            hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (reference, sound)}
            result = align_audio(reference, sound)
            self.assertAlmostEqual(result['offset_seconds'], -3, delta=.0025)
            self.assertEqual((result['reference_channel'], result['sound_channel']), (1, 1))
            self.assertEqual(hashes, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in hashes})

    def test_independent_channels_fall_back_from_unrelated_and_silent_first_channels(self):
        with tempfile.TemporaryDirectory() as temp:
            reference = Path(temp) / 'INDEPENDENT_MICROPHONES.mov'
            excerpt = self.original[3 * RATE:21 * RATE]
            write_pgm(reference, np.column_stack((speech_like(18, 28173), excerpt)))
            sound = Path(temp) / 'SILENT_CH1_MAIN_CH2.wav'
            write_audio(sound, np.column_stack((np.zeros_like(self.original), self.original)))
            result = align_audio(reference, sound)
            self.assertAlmostEqual(result['offset_seconds'], -3, delta=.0025)
            self.assertEqual((result['reference_channel'], result['sound_channel']), (2, 2))

    def test_two_original_mono_files_match_without_assuming_left_and_right_mix(self):
        with tempfile.TemporaryDirectory() as temp:
            left = Path(temp) / 'TAKE_001_L.wav'
            right = Path(temp) / 'TAKE_001_R.wav'
            write_audio(left, self.original)
            write_audio(right, -self.original)
            left_match = align_audio(self.pgm, left)
            right_match = align_audio(self.pgm, right)
            self.assertAlmostEqual(left_match['offset_seconds'], -3, delta=.0025)
            self.assertAlmostEqual(right_match['offset_seconds'], -3, delta=.0025)
            self.assertEqual(left_match['sound_channel'], 1)
            self.assertEqual(right_match['sound_channel'], 1)

    def test_sound_with_two_mono_streams_uses_the_second_when_the_first_is_silent(self):
        with tempfile.TemporaryDirectory() as temp:
            sound = Path(temp) / 'TWO_MONO_STREAMS.mov'
            with av.open(str(sound), 'w', format='mov') as out:
                streams = [out.add_stream('pcm_s16le', rate=RATE) for _ in range(2)]
                for stream in streams:
                    stream.layout = 'mono'
                for stream, samples in zip(streams, (np.zeros_like(self.original), self.original)):
                    for start in range(0, len(samples), 4096):
                        frame = av.AudioFrame.from_ndarray(samples[start:start + 4096][None, :], format='s16', layout='mono')
                        frame.sample_rate = RATE; frame.pts = start; frame.time_base = F(1, RATE)
                        for packet in stream.encode(frame):
                            out.mux(packet)
                    for packet in stream.encode():
                        out.mux(packet)
            result = align_audio(self.pgm, sound)
            self.assertAlmostEqual(result['offset_seconds'], -3, delta=.0025)
            self.assertEqual(result['sound_stream'], 1)
            self.assertEqual(result['sound_channel'], 1)

    def test_successful_stereo_uses_one_channel_pair_and_separate_feature_cache_keys(self):
        import waveform_sync as engine
        with tempfile.TemporaryDirectory() as temp:
            reference = Path(temp) / 'DUAL_IDENTICAL.mov'
            excerpt = self.original[3 * RATE:21 * RATE]
            write_pgm(reference, np.column_stack((excerpt, excerpt)))
            sound = Path(temp) / 'DUAL_IDENTICAL.wav'
            write_audio(sound, np.column_stack((self.original, self.original)))
            with patch.object(engine, '_align_stream', wraps=engine._align_stream) as matcher:
                result = align_audio(reference, sound)
            self.assertEqual(matcher.call_count, 1)
            self.assertAlmostEqual(result['offset_seconds'], -3, delta=.0025)
            first = engine._feature_data(sound, (0, 0), lambda *args: None, lambda: False)
            second = engine._feature_data(sound, (0, 1), lambda *args: None, lambda: False)
            self.assertIsNot(first, second)
            np.testing.assert_array_equal(first['features'], second['features'])

    def test_missing_reference_and_silence_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            no_audio = Path(temp) / 'NO_AUDIO.mov'; write_pgm(no_audio, duration=10)
            silence = Path(temp) / 'SILENT.mov'; write_pgm(silence, np.zeros(10 * RATE, np.int16))
            for reference in (no_audio, silence):
                with self.subTest(reference=reference.name), self.assertRaisesRegex(AnalysisError, '没有声音|静音|参考声音'):
                    align_audio(reference, self.wav)

    def test_repeated_identical_passages_are_ambiguous(self):
        with tempfile.TemporaryDirectory() as temp:
            phrase = speech_like(8, 994)
            sound = Path(temp) / 'REPEATED.wav'; write_audio(sound, np.tile(phrase, 3))
            reference = Path(temp) / 'WHICH_REPEAT.mov'; write_pgm(reference, phrase[RATE:7 * RATE])
            with self.assertRaisesRegex(AnalysisError, '重复|唯一'):
                align_audio(reference, sound)

    def test_drift_and_unrelated_audio_are_not_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            positions = (3 * RATE + np.arange(18 * RATE) * 1.02)
            stretched = np.interp(positions, np.arange(len(self.original)), self.original).astype(np.int16)
            drifting = Path(temp) / 'CLOCK_DRIFT.mov'; write_pgm(drifting, stretched)
            unrelated = Path(temp) / 'OTHER_SHOW.mov'; write_pgm(unrelated, speech_like(18, 8834))
            for reference in (drifting, unrelated):
                with self.subTest(reference=reference.name), self.assertRaises(AnalysisError):
                    align_audio(reference, self.wav)

    def test_default_waveform_matches_two_pgm_and_two_originals_without_timecode(self):
        config = dict(fps=25, sound_sources=[dict(path=str(self.wav), channel='main'), dict(path=str(self.wav2), channel='main')])
        cut = dict(pgm_sources=[dict(path=str(self.pgm), frames=450, start=0, start_tc='05:00:00:00'),
                                dict(path=str(self.pgm2), frames=400, start=450, start_tc='06:00:00:00')])
        paths = (self.wav, self.wav2, self.pgm, self.pgm2); hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        wavs, events = sound_plan(config, cut)
        self.assertEqual([e['path'] for e in events], [str(self.wav.resolve()), str(self.wav2.resolve())])
        self.assertEqual([e['source_start'] for e in events], [3, 2])
        self.assertEqual([e['record_start'] for e in events], [0, 18])
        self.assertEqual([e['duration'] for e in events], [18, 16])
        self.assertTrue(all(not w['has_timecode'] and w['start'] == 0 for w in wavs))
        self.assertEqual(hashes, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})

    def test_retired_reference_cannot_replace_missing_pgm_audio(self):
        with tempfile.TemporaryDirectory() as temp:
            no_audio = Path(temp) / 'NO_AUDIO_PGM.mov'; write_pgm(no_audio, duration=18)
            reference = Path(temp) / 'FULL_REFERENCE.mov'; write_pgm(reference, self.original)
            config = dict(fps=25, sound_sources=[dict(path=str(self.wav), channel='main')],
                          sound_reference_overrides={str(no_audio): dict(path=str(reference), start_seconds=3)})
            before = copy.deepcopy(config)
            with self.assertRaises(AnalysisError):
                sound_plan(config, dict(pgm_sources=[dict(path=str(no_audio), frames=450, start=0, start_tc='')]))
            self.assertEqual(config, before)

    def test_cancellation_leaves_all_originals_unchanged(self):
        before = hashlib.sha256(self.wav.read_bytes()).hexdigest()
        with self.assertRaisesRegex(AnalysisError, '取消'):
            align_audio(self.pgm, self.wav, cancelled=lambda: True)
        self.assertEqual(before, hashlib.sha256(self.wav.read_bytes()).hexdigest())


class MultipleReferenceStreamTests(unittest.TestCase):
    def match_three_streams(self, offsets):
        with tempfile.TemporaryDirectory() as temp:
            reference, sound = Path(temp) / 'three_streams.mov', Path(temp) / 'original.wav'
            reference.write_bytes(b'reference'); sound.write_bytes(b'original')
            ref_container, sound_container = MagicMock(), MagicMock()
            ref_container.__enter__.return_value.streams.audio = [object(), object(), object()]
            sound_container.__enter__.return_value.streams.audio = [object()]
            matches = [dict(score=score, offset_seconds=offset, anchors=[])
                       for score, offset in zip((.99, .98, .90), offsets)]
            with patch('waveform_sync.av.open', side_effect=[ref_container, sound_container]), \
                 patch('waveform_sync._align_stream', side_effect=matches) as align, \
                 patch('waveform_sync._matches', OrderedDict()):
                try:
                    return align_audio(reference, sound)
                finally:
                    self.assertEqual(align.call_count, 3)

    def test_lower_scoring_third_stream_conflict_is_rejected(self):
        with self.assertRaisesRegex(AnalysisError, '不同声音流'):
            self.match_three_streams((0, 0, 5))

    def test_all_three_reference_streams_agree_and_best_match_is_returned(self):
        result = self.match_three_streams((0, 0, 0))
        self.assertEqual(result['offset_seconds'], 0)
        self.assertEqual(result['score'], .99)


if __name__ == '__main__': unittest.main()
