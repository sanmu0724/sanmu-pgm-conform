import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import wave

import numpy as np
from PyQt6.QtCore import QUrl, QByteArray
from PyQt6.QtMultimedia import QAudioFormat, QAudioBuffer, QMediaPlayer
from PyQt6.QtWidgets import QApplication
from preview_audio import PreviewAudio, gain_pcm


class Device:
    def __init__(self, name): self.name = name
    def id(self): return QByteArray(self.name.encode())
    def description(self): return self.name
    def isNull(self): return False
    def isFormatSupported(self, fmt): return True


class PreviewAudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.player = QMediaPlayer()
        self.audio = PreviewAudio(self.player)
        self.addCleanup(self.audio.close)

    def test_gain_doubles_quiet_pcm_without_modifying_input(self):
        raw = np.array([1000, -1000, 0, 32767], dtype='<i2').tobytes()
        before = bytes(raw)
        out = gain_pcm(raw, QAudioFormat.SampleFormat.Int16, 6.0206)
        self.assertEqual(np.frombuffer(out, '<i2').tolist(), [2000, -2000, 0, 32767])
        self.assertEqual(raw, before)
        self.assertEqual(np.frombuffer(gain_pcm(raw, QAudioFormat.SampleFormat.Int16, 18, True), '<i2').tolist(), [0]*4)

    def test_float_int32_unsigned_and_mute_are_bounded(self):
        for fmt, dtype, values, silence in (
                (QAudioFormat.SampleFormat.Float, '<f4', [.1, -.1], 0),
                (QAudioFormat.SampleFormat.Int32, '<i4', [100000, -100000], 0),
                (QAudioFormat.SampleFormat.UInt8, 'u1', [140, 116], 128)):
            raw = np.array(values, dtype=dtype).tobytes()
            out = np.frombuffer(gain_pcm(raw, fmt, 6.0206), dtype)
            center = silence
            self.assertAlmostEqual(float(out[0]-center), (values[0]-center)*2, delta=.001)
            self.assertTrue(all(np.frombuffer(gain_pcm(raw, fmt, 18, True), dtype) == silence))

    def test_default_output_change_follows_system_and_explicit_selection_overrides(self):
        mac, bluetooth = Device('Mac'), Device('Bluetooth')
        with patch('preview_audio.QMediaDevices.audioOutputs', return_value=[mac, bluetooth]), \
             patch('preview_audio.QMediaDevices.defaultAudioOutput', return_value=mac), \
             patch.object(self.audio.native, 'setDevice') as route:
            self.audio.refresh_devices()
            route.assert_called_with(mac)
            self.audio.output.setCurrentIndex(self.audio.output.findData(b'Bluetooth'.hex()))
            route.assert_called_with(bluetooth)
            with patch('preview_audio.QMediaDevices.defaultAudioOutput', return_value=bluetooth):
                self.audio.output.setCurrentIndex(0)
                self.audio.refresh_devices()
                self.assertEqual(self.audio.device_id, b'Bluetooth')
            self.audio.refresh_devices()
            self.assertEqual(self.audio.device_id, b'Mac')

    def test_disconnected_explicit_device_falls_back_to_current_system_output(self):
        mac, bluetooth = Device('Mac'), Device('Bluetooth')
        self.audio.selected_id = b'Bluetooth'
        with patch('preview_audio.QMediaDevices.audioOutputs', return_value=[mac]), \
             patch('preview_audio.QMediaDevices.defaultAudioOutput', return_value=mac), \
             patch.object(self.audio.native, 'setDevice'):
            self.audio.refresh_devices()
            self.assertEqual(self.audio.selected_id, b'')
            self.assertEqual(self.audio.device_id, b'Mac')
            self.assertEqual(self.audio.output.currentIndex(), 0)

    def test_reset_discards_pending_sound_pause_seek_and_source_changes(self):
        sink = Mock()
        self.audio.sink = sink
        self.audio.pending.extend(b'old audio')
        self.audio.state_changed(QMediaPlayer.PlaybackState.PausedState)
        sink.reset.assert_called_once()
        self.assertEqual(self.audio.pending, b'')
        self.assertIsNone(self.audio.sink)
        with patch.object(self.player, 'setPosition') as seek:
            self.audio.seek(1234)
            seek.assert_called_once_with(1234)
        self.assertTrue(self.audio.native.isMuted())

    def test_partial_device_writes_keep_unwritten_samples(self):
        self.audio.sink = Mock()
        self.audio.io = Mock()
        self.audio.sink.bytesFree.return_value = 4
        self.audio.io.write.return_value = 2
        self.audio.pending.extend(b'abcdef')
        self.audio.flush()
        self.audio.io.write.assert_called_with(b'abcd')
        self.assertEqual(self.audio.pending, b'cdef')

    def test_stale_decode_after_seek_and_paused_buffers_are_ignored(self):
        fmt = QAudioFormat();fmt.setSampleRate(48000);fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        buffer = QAudioBuffer(QByteArray(b'\0'*2048), fmt, 0)
        with patch.object(self.player, 'playbackState', return_value=QMediaPlayer.PlaybackState.PausedState):
            self.audio.receive(buffer)
        self.assertIsNone(self.audio.sink)
        with patch.object(self.player, 'playbackState', return_value=QMediaPlayer.PlaybackState.PlayingState), \
             patch.object(self.player, 'position', return_value=10000):
            self.audio.receive(buffer)
        self.assertIsNone(self.audio.sink)

    def test_muted_preview_skips_pcm_and_sink_work_without_detaching_capture(self):
        capture = self.player.audioBufferOutput()
        old_sink = Mock()
        self.audio.sink = old_sink
        self.audio.pending.extend(b'queued sound')
        self.audio.set_muted(True)
        old_sink.reset.assert_called_once()
        self.assertEqual(self.audio.pending, b'')
        self.assertIsNone(self.audio.sink)
        buffer = Mock()
        with patch('preview_audio.QAudioSink', side_effect=AssertionError('muted opened sink')), \
             patch.object(self.audio, 'refresh_devices', side_effect=AssertionError('muted queried device')):
            for _ in range(100):
                self.audio.receive(buffer)
        buffer.isValid.assert_not_called()
        self.assertIs(self.player.audioBufferOutput(), capture)
        self.audio.set_muted(False)
        self.assertIs(self.player.audioBufferOutput(), capture)
        self.assertEqual(self.audio.gain_db, 6)

    def test_native_decoder_delivers_gain_adjusted_audio_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'quiet.wav'
            samples = (np.sin(np.arange(48000)*2*np.pi*440/48000)*1000).astype('<i2')
            with wave.open(str(path), 'wb') as wav:
                wav.setparams((1, 2, 48000, 48000, 'NONE', 'not compressed'))
                wav.writeframes(samples.tobytes())
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            received = []
            class Sink:
                def __init__(self, device, fmt, parent): self.fmt = fmt
                def setBufferSize(self, size): pass
                def start(self): return self
                def write(self, data): received.append(bytes(data));return len(data)
                def bytesFree(self): return 1000000
                def format(self): return self.fmt
                def reset(self): pass
                def deleteLater(self): pass
            with patch('preview_audio.QAudioSink', Sink), \
                 patch.object(self.audio, '_selected_device', Device('test')):
                self.player.setSource(QUrl.fromLocalFile(str(path)))
                self.player.play()
                deadline = time.monotonic()+5
                while not received and time.monotonic()<deadline:
                    self.app.processEvents();time.sleep(.005)
                self.player.stop()
            self.assertTrue(received, self.player.errorString())
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
            self.assertNotIn('preview_gain', path.name)

    def test_decoded_buffers_reuse_device_and_format_without_repeated_coreaudio_inventory(self):
        fmt = QAudioFormat();fmt.setSampleRate(48000);fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        buffer = QAudioBuffer(QByteArray(b'\0'*2048), fmt, 0)
        device = Device('test')
        self.audio._selected_device = device
        self.audio.sink = Mock()
        self.audio.sink.format.return_value = fmt
        self.audio.sink.bytesFree.return_value = 1000000
        self.audio.io = Mock();self.audio.io.write.side_effect = len
        with patch.object(self.player, 'playbackState', return_value=QMediaPlayer.PlaybackState.PlayingState), \
             patch.object(self.player, 'position', return_value=0), \
             patch.object(self.audio, 'refresh_devices', side_effect=AssertionError('buffer queried devices')), \
             patch.object(device, 'isFormatSupported', wraps=device.isFormatSupported) as support:
            for _ in range(100):self.audio.receive(buffer)
        self.assertEqual(support.call_count, 1)
        self.assertEqual(self.audio.pending, b'')


if __name__ == '__main__': unittest.main()
