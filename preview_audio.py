"""Preview-only PCM gain and live output routing; never writes media or XML."""
from __future__ import annotations

import numpy as np
from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtMultimedia import (QAudioBufferOutput, QAudioFormat, QAudioOutput,
                              QAudioSink, QMediaDevices, QMediaPlayer)
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLabel, QComboBox, QSpinBox, QCheckBox


def gain_pcm(raw, sample_format, gain_db, muted=False):
    """Return interleaved PCM bytes, with clipping bounded to the PCM range."""
    formats = {QAudioFormat.SampleFormat.Float: ('<f4', 1.),
               QAudioFormat.SampleFormat.Int16: ('<i2', 32768.),
               QAudioFormat.SampleFormat.Int32: ('<i4', 2147483648.),
               QAudioFormat.SampleFormat.UInt8: ('u1', 128.)}
    dtype, scale = formats[sample_format]
    values = np.frombuffer(raw, dtype=dtype).astype(np.float64)
    if sample_format == QAudioFormat.SampleFormat.UInt8:
        values -= 128
    factor = 0. if muted else 10. ** (max(0., min(18., float(gain_db))) / 20.)
    values *= factor
    if sample_format == QAudioFormat.SampleFormat.Float:
        values = np.nan_to_num(values, nan=0., posinf=1., neginf=-1.)
        values = np.clip(values, -1., 1.)
    else:
        values = np.clip(np.rint(values), -scale, scale-1)
    if sample_format == QAudioFormat.SampleFormat.UInt8:
        values += 128
    return values.astype(dtype).tobytes()


class PreviewAudio(QObject):
    """Player-clock decoded audio routed to a small, resettable output buffer."""
    def __init__(self, player, parent=None):
        super().__init__(parent)
        self.player = player
        self.devices = QMediaDevices(self)
        self.native = QAudioOutput(self)
        self.native.setMuted(True)  # Only the gain-adjusted preview is audible.
        self.player.setAudioOutput(self.native)
        self.capture = QAudioBufferOutput(self)
        self.player.setAudioBufferOutput(self.capture)
        self.capture.audioBufferReceived.connect(self.receive)
        self.player.playbackStateChanged.connect(self.state_changed)
        self.player.sourceChanged.connect(lambda *_: self.reset())
        self.sink = self.io = None
        self.pending = bytearray()
        self.device_id = b''
        self.selected_id = b''
        self.gain_db = 6
        self.muted = False
        self._inventory = None
        self._selected_device = None
        self._format_support = None
        self.controls = QWidget(parent)
        layout = QVBoxLayout(self.controls)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        layout.addLayout(row)
        row.addWidget(QLabel('试听增益'))
        self.gain = QSpinBox()
        self.gain.setRange(0, 18)
        self.gain.setSuffix(' dB')
        self.gain.setValue(self.gain_db)
        self.gain.setToolTip('只放大试听声音，不修改原文件或导出结果；声音失真时降低增益。')
        self.gain.valueChanged.connect(self.set_gain)
        row.addWidget(self.gain)
        self.mute = QCheckBox('静音')
        self.mute.toggled.connect(self.set_muted)
        row.addWidget(self.mute)
        row.addStretch()
        self.notice = QLabel('')
        self.notice.setWordWrap(True)
        row.addWidget(self.notice)
        row = QHBoxLayout()
        layout.addLayout(row)
        row.addWidget(QLabel('输出'))
        self.output = QComboBox()
        self.output.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.output.setMinimumContentsLength(10)
        self.output.currentIndexChanged.connect(self.select_device)
        row.addWidget(self.output, 1)
        self.flush_timer = QTimer(self)
        self.flush_timer.setInterval(5)
        self.flush_timer.timeout.connect(self.flush)
        self.devices.audioOutputsChanged.connect(self.refresh_devices)
        # Some macOS backends don't emit audioOutputsChanged on default-only changes.
        self.device_timer = QTimer(self)
        self.device_timer.setInterval(1500)
        self.device_timer.timeout.connect(self.refresh_devices)
        self.device_timer.start()
        self.refresh_devices()

    def set_gain(self, value):
        self.gain_db = value

    def set_muted(self, value):
        self.muted = value
        self.reset()

    def refresh_devices(self):
        outputs = QMediaDevices.audioOutputs()
        default = QMediaDevices.defaultAudioOutput()
        inventory = (bytes(default.id()), tuple((bytes(d.id()), d.description()) for d in outputs))
        if inventory != self._inventory:
            self._inventory = inventory
            self._format_support = None
            self.output.blockSignals(True)
            self.output.clear()
            self.output.addItem('跟随系统：' + (default.description() or '无输出设备'), '')
            for device in outputs:
                self.output.addItem(device.description(), bytes(device.id()).hex())
            index = self.output.findData(self.selected_id.hex())
            if index < 0:
                self.selected_id = b''  # Disconnected explicit device: follow system.
                index = 0
            self.output.setCurrentIndex(index)
            self.output.blockSignals(False)
        chosen = next((d for d in outputs if bytes(d.id()) == self.selected_id), default)
        self._selected_device = chosen
        identity = bytes(chosen.id())
        if identity != self.device_id:
            self.reset()
            self.device_id = identity
            self.native.setDevice(chosen)
        return chosen

    def select_device(self, *_):
        self.selected_id = bytes.fromhex(self.output.currentData() or '')
        self.refresh_devices()

    def reset(self):
        self.pending.clear()
        self.flush_timer.stop()
        if self.sink is not None:
            self.sink.reset()  # Immediately discard old sound on seek/device change.
            self.sink.deleteLater()
        self.sink = self.io = None

    def seek(self, milliseconds):
        self.reset()
        self.player.setPosition(milliseconds)

    def state_changed(self, state):
        if state != QMediaPlayer.PlaybackState.PlayingState:
            self.reset()

    def receive(self, buffer):
        # Keep QAudioBufferOutput attached for the player's entire lifetime.
        # Repeated detachment can crash Qt during teardown; a muted preview
        # instead skips all PCM work and never opens an output sink.
        if self.muted:
            return
        if not buffer.isValid() or self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            return
        if buffer.startTime() >= 0 and abs(buffer.startTime()/1000 - self.player.position()) > 500:
            return  # A queued buffer from before a seek is never played.
        # Device events/the existing 1.5s timer refresh the routing inventory.
        # Querying CoreAudio for every decoded buffer stalls the GUI at audio
        # buffer frequency, especially with Bluetooth/aggregate devices.
        device = self._selected_device
        if device is None:
            device = self.refresh_devices()
        fmt = buffer.format()
        format_key = (self.device_id, fmt.sampleRate(), fmt.channelCount(), fmt.sampleFormat())
        if self._format_support is None or self._format_support[0] != format_key:
            self._format_support = format_key, (not device.isNull() and device.isFormatSupported(fmt))
        if not self._format_support[1]:
            self.notice.setText('输出格式不支持')
            return
        if self.sink is not None and self.sink.format() != fmt:
            self.reset()
        if self.sink is None:
            self.sink = QAudioSink(device, fmt, self)
            self.sink.setBufferSize(max(fmt.bytesForDuration(60000), 1024))
            self.io = self.sink.start()
            if self.io is None:
                self.notice.setText('音频设备不可用')
                self.reset()
                return
        self.notice.setText('')
        raw = bytes(buffer.constData().asstring(buffer.byteCount()))
        self.pending.extend(gain_pcm(raw, fmt.sampleFormat(), self.gain_db, self.muted))
        # Bound queued sound even if a driver stops accepting data.
        if len(self.pending) > fmt.bytesForDuration(500000):
            self.notice.setText('音频设备延迟，请重新选择输出')
            self.reset()
            return
        self.flush()
        if self.pending:
            self.flush_timer.start()

    def flush(self):
        if not self.sink or not self.io or not self.pending:
            self.flush_timer.stop()
            return
        size = min(len(self.pending), self.sink.bytesFree())
        if size > 0:
            written = self.io.write(bytes(self.pending[:size]))
            if written > 0:
                del self.pending[:written]
        if not self.pending:
            self.flush_timer.stop()

    def close(self):
        self.device_timer.stop()
        self.reset()
