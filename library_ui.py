"""Shared dialogue catalogue UI; media uses local or mounted NEXIS paths.

Only this panel knows the catalogue RPC client.  The existing PGM window gives
it snapshots and receives session payloads, including an optional library_focus.
All disk, recognition and HTTP work runs outside the GUI thread.
"""
from __future__ import annotations

import copy
from collections import defaultdict
import json
import uuid
import time
from datetime import datetime
from pathlib import Path

from PyQt6 import sip
from PyQt6.QtCore import Qt, QThread, QTimer, QUrl, QEvent, QPoint, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox, QLineEdit, QTextEdit, QAbstractSpinBox,
    QPushButton, QLabel, QComboBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QFileDialog, QMessageBox, QInputDialog, QTabWidget,
    QPlainTextEdit, QProgressBar, QDialog, QDialogButtonBox, QAbstractItemView,
    QListWidget, QListWidgetItem, QSplitter,
    QToolButton, QMenu, QSlider, QSpinBox, QDoubleSpinBox, QSizePolicy, QStackedWidget, QScrollArea, QFrame,
)


STATUS_NAMES = {'candidate': '待核实', 'confirmed': '已确认', 'rejected': '已排除'}


def classification_state(row):
    if row.get('status') == 'confirmed':
        return '自动通过' if row.get('classification_method') == 'auto_dialogue' else '已核对'
    return STATUS_NAMES.get(row.get('status'), '')


def classification_method(row):
    manual_visual = row.get('classification_method') == 'manual_visual' or any(
        isinstance(row.get(key), dict) and row[key].get('source_kind') == 'manual_visual'
        for key in ('data', 'metadata')) or row.get('transcript_text') == '[人工画面归类]'
    if manual_visual:
        return '人工画面归类'
    if '人工关联' in row.get('reason', ''):
        return '人工关联'
    score = row.get('auto_review_score', row.get('score', 0)) if row.get('classification_method') == 'auto_dialogue' \
            else row.get('score', 0)
    return f'{float(score):.0%}'


def format_time(seconds):
    milliseconds = max(0, round(float(seconds or 0) * 1000))
    hours, rest = divmod(milliseconds, 3600000)
    minutes, rest = divmod(rest, 60000)
    whole, fraction = divmod(rest, 1000)
    return f'{hours:02}:{minutes:02}:{whole:02}.{fraction:03}'


def media_paths(config):
    """Extract useful recognition references, never an XML as audio input."""
    result = []

    def add(value):
        if isinstance(value, str) and value and Path(value).suffix.lower() in (
                '.mov', '.mp4', '.mxf', '.m4v', '.wav', '.aif', '.aiff', '.flac', '.mp3'):
            if value not in result:
                result.append(value)
        elif isinstance(value, dict):
            for key in ('path', 'media_path', 'file', 'pgm'):
                add(value.get(key))
        elif isinstance(value, (list, tuple)):
            for item in value:
                add(item)

    for key in ('pgm', 'pgm_sources', 'sources', 'sound_sources', 'sound_files', 'audio_files',
                'sound_paths', 'sound_main', 'sound_path', 'library_media'):
        add(config.get(key))
    for path in config.get('pgm_overrides', {}).values():
        add(path)
    return result


class LibraryTask(QThread):
    progress = pyqtSignal(int, str)
    result = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation
        self._last_report = 0.

    def report(self, *args):
        number, message = 0, ''
        for arg in args:
            if isinstance(arg, (int, float)):
                number = max(0, min(100, round(arg)))
            elif arg is not None:
                message = str(arg)
        now = time.monotonic()
        if number in (0, 100) or now - self._last_report >= .1:
            self._last_report = now
            self.progress.emit(number, message)

    def run(self):
        try:
            value = self.operation(self.report, self.isInterruptionRequested)
            self.result.emit(value)
        except Exception as exc:
            self.failed.emit(str(exc))


class MediaPreview(QDialog):
    """Non-destructive preview of the selected candidate's source interval."""
    def __init__(self, path, start, end, parent=None):
        super().__init__(parent)
        from PyQt6.QtMultimedia import QMediaPlayer
        from PyQt6.QtMultimediaWidgets import QVideoWidget
        self.setWindowTitle(Path(path).name)
        self.resize(860, 560)
        self.start_ms = max(0, round(float(start) * 1000))
        self.end_ms = max(self.start_ms, round(float(end) * 1000))
        self.positioned = False
        layout = QVBoxLayout(self)
        video = QVideoWidget(self)
        layout.addWidget(video)
        self.player = QMediaPlayer(self)
        from preview_audio import PreviewAudio
        self.preview_audio = PreviewAudio(self.player, self)
        self.audio_output = self.preview_audio.native
        self.player.setVideoOutput(video)
        row = QHBoxLayout()
        for title, action in (('播放片段', self.replay), ('暂停', self.player.pause)):
            button = QPushButton(title)
            button.clicked.connect(action)
            row.addWidget(button)
        self.label = QLabel(f'{format_time(start)} — {format_time(end)}')
        row.addWidget(self.label, 1)
        layout.addLayout(row)
        layout.addWidget(self.preview_audio.controls)
        self.player.mediaStatusChanged.connect(self.ready)
        self.player.positionChanged.connect(self.position_changed)
        self.player.errorOccurred.connect(lambda *args: self.label.setText(self.player.errorString()))
        self.player.setSource(QUrl.fromLocalFile(path))

    def ready(self, status):
        from PyQt6.QtMultimedia import QMediaPlayer
        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia) and not self.positioned:
            self.positioned = True
            self.replay()

    def replay(self):
        self.preview_audio.seek(self.start_ms)
        self.player.play()

    def position_changed(self, value):
        if self.end_ms > self.start_ms and value >= self.end_ms:
            self.player.pause()

    def closeEvent(self, event):
        self.player.stop()
        self.preview_audio.close()
        super().closeEvent(event)


class InlinePreview(QWidget):
    """One reusable player for both comparison and unmatched dialogue lists."""
    def __init__(self, parent=None):
        super().__init__(parent)
        from PyQt6.QtMultimedia import QMediaPlayer
        from PyQt6.QtMultimediaWidgets import QVideoWidget
        class ReviewVideo(QVideoWidget):
            def keyPressEvent(self,event):
                if event.key()==Qt.Key.Key_Escape and self.isFullScreen():
                    self.setFullScreen(False);event.accept()
                else:super().keyPressEvent(event)
            def mouseDoubleClickEvent(self,event):
                self.setFullScreen(not self.isFullScreen());event.accept()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.video = ReviewVideo(self)
        layout.addWidget(self.video, 1)
        self.player = QMediaPlayer(self)
        from preview_audio import PreviewAudio
        self.preview_audio = PreviewAudio(self.player, self)
        self.audio = self.preview_audio.native
        self.player.setVideoOutput(self.video)
        self.start_ms = self.end_ms = 0
        self.path = ''
        self.positioned = False
        self._play_requested = False
        controls = QHBoxLayout()
        self.fullscreen=QPushButton('全屏')
        self.fullscreen.clicked.connect(lambda:self.video.setFullScreen(not self.video.isFullScreen()))
        self.video.fullScreenChanged.connect(lambda full:self.fullscreen.setText('退出全屏' if full else '全屏'))
        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.sliderMoved.connect(self.preview_audio.seek)
        controls.addWidget(self.seek, 1)
        self.time = QLabel('')
        controls.addWidget(self.time)
        controls.addWidget(self.fullscreen)
        self.audio_settings = QToolButton()
        self.audio_settings.setText('试听设置')
        self.audio_settings.setCheckable(True)
        self.audio_settings.toggled.connect(self.preview_audio.controls.setVisible)
        controls.addWidget(self.audio_settings)
        layout.addLayout(controls)
        layout.addWidget(self.preview_audio.controls)
        self.preview_audio.controls.hide()
        self.player.mediaStatusChanged.connect(self.ready)
        self.player.positionChanged.connect(self.position_changed)
        self.player.errorOccurred.connect(lambda *args: self.time.setText('未能预览：' + self.player.errorString()))

    def load_media(self, path, start, end, *, play=False):
        start_ms = max(0, round(float(start) * 1000))
        end_ms = max(start_ms, round(float(end) * 1000))
        if (self.path, self.start_ms, self.end_ms) == (path, start_ms, end_ms):
            if play:
                self.replay()
            return
        same_source = self.path == path
        self.player.pause() if same_source else self.player.stop()
        self.preview_audio.reset()
        self.path = path
        self.start_ms, self.end_ms = start_ms, end_ms
        self._play_requested = bool(play) and not getattr(self, '_suspended', False)
        self.seek.setRange(self.start_ms, self.end_ms)
        self.seek.setValue(self.start_ms)
        if same_source and self.positioned:
            self.player.setPosition(self.start_ms)
            if self._play_requested:
                self.player.play()
        else:
            self.positioned = False
            if not same_source:self.player.setSource(QUrl.fromLocalFile(path))
        self.time.setText(format_time(start))

    def ready(self, status):
        from PyQt6.QtMultimedia import QMediaPlayer
        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia) and not self.positioned:
            self.positioned = True
            self.preview_audio.reset()
            self.player.setPosition(self.start_ms)
            if self._play_requested and not getattr(self, '_suspended', False):
                self.player.play()
            else:
                self.pause()

    def pause(self):
        self._play_requested = False
        self.player.pause()

    def replay(self):
        if self.path and not getattr(self, '_suspended', False):
            self._play_requested = True
            if self.positioned:
                self.preview_audio.reset()
                self.player.setPosition(self.start_ms)
                self.player.play()

    def toggle_playback(self):
        from PyQt6.QtMultimedia import QMediaPlayer
        if not self.path or getattr(self, '_suspended', False):
            return
        if (self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
                or not self.positioned and self._play_requested):
            self.pause()
        else:
            self._play_requested = True
            if not self.positioned:
                return
            position = self.player.position()
            if position < self.start_ms or (self.end_ms > self.start_ms and position >= self.end_ms):
                self.preview_audio.seek(self.start_ms)
            self.player.play()

    def position_changed(self, milliseconds):
        self.seek.setValue(milliseconds)
        self.time.setText(format_time(milliseconds / 1000))
        if self.end_ms > self.start_ms and milliseconds >= self.end_ms:
            self.pause()

    def clear(self):
        if self.video.isFullScreen():self.video.setFullScreen(False)
        self.player.stop()
        self.preview_audio.reset()
        self.player.setSource(QUrl())
        self.path = ''
        self.positioned = False
        self._play_requested = False

    def closeEvent(self, event):
        self.clear()
        self.preview_audio.close()
        super().closeEvent(event)


class MaterialSelection(QWidget):
    """Existing registrations stay read-only; only additions are submitted."""
    def __init__(self, kind, parent=None):
        super().__init__(parent)
        self.kind, self.records, self.existing_records = kind, [], []
        layout = QVBoxLayout(self)
        buttons = QHBoxLayout()
        if kind == 'proxy':
            self.camera_selector = QComboBox()
            self.camera_selector.setMinimumWidth(110)
            buttons.addWidget(self.camera_selector)
            from camera_schema import DEFAULT_CAMERAS
            self.set_camera_ids(DEFAULT_CAMERAS)
            button = QPushButton('添加所选机位代理…')
            button.clicked.connect(lambda: self.choose(self.camera_selector.currentData()))
            buttons.addWidget(button)
        else:
            button = QPushButton({'pgm': '添加 PGM…', 'cut_xml': '添加切点 XML…'}.get(kind, '添加原始主 WAV…'))
            button.clicked.connect(lambda: self.choose())
            buttons.addWidget(button)
        remove = QPushButton('移除所选新增项')
        remove.clicked.connect(self.remove_selected)
        buttons.addWidget(remove)
        if kind == 'cut_xml':
            for title, step in (('上移', -1), ('下移', 1)):
                move = QPushButton(title)
                move.clicked.connect(lambda checked=False, step=step: self.move_selected(step))
                buttons.addWidget(move)
        buttons.addStretch()
        layout.addLayout(buttons)
        if kind == 'cut_xml':
            hint = QLabel('按演出顺序排列 XML；可上移／下移。本次新增项接在已登记 XML 后。')
            hint.setObjectName('mutedLabel')
            hint.setWordWrap(True)
            layout.addWidget(hint)
        lists = QHBoxLayout()
        for title, name in (('已登记', 'existing'), ('本次新增', 'additions')):
            column = QVBoxLayout()
            column.addWidget(QLabel(title))
            widget = QListWidget()
            widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
            widget.setWordWrap(True)
            setattr(self, name, widget)
            column.addWidget(widget)
            lists.addLayout(column, 1)
        layout.addLayout(lists, 1)
        self.count = QLabel()
        layout.addWidget(self.count)
        self.render()

    def set_camera_ids(self, camera_ids):
        if self.kind != 'proxy':
            return
        current = self.camera_selector.currentData()
        self.camera_selector.clear()
        for camera in camera_ids:
            self.camera_selector.addItem(camera + ' 机', camera)
        index = self.camera_selector.findData(current)
        self.camera_selector.setCurrentIndex(max(0, index))

    @staticmethod
    def key(row):
        return row.get('camera', ''), row['path']

    def choose(self, camera=''):
        title = '选择原始主 WAV（可多选）' if self.kind == 'main_wav' else '选择素材（可多选）'
        file_filter = ('WAV (*.wav *.wave)' if self.kind == 'main_wav' else
                       '剪辑 XML (*.xml *.fcpxml)' if self.kind == 'cut_xml' else
                       '视频 (*.mov *.mp4 *.mxf *.m4v);;所有文件 (*)')
        paths, _ = QFileDialog.getOpenFileNames(self, title, '', file_filter)
        self.add_paths(paths, camera)

    def add_paths(self, paths, camera=''):
        known = {self.key(row) for row in self.existing_records + self.records}
        for path in paths:
            row = {'path': str(path)}
            if self.kind == 'proxy':
                row['camera'] = camera
            if self.key(row) not in known:
                self.records.append(row)
                known.add(self.key(row))
        self.render()

    def set_existing(self, rows):
        self.existing_records = copy.deepcopy(rows)
        known = {self.key(row) for row in rows}
        self.records = [row for row in self.records if self.key(row) not in known]
        self.render()

    def render(self):
        for widget, rows in ((self.existing, self.existing_records), (self.additions, self.records)):
            widget.clear()
            for row in rows:
                prefix = row.get('camera', '') + ' 机  ' if row.get('camera') else ''
                item = QListWidgetItem(prefix + Path(row['path']).name + '\n' + row['path'])
                item.setToolTip(row['path'])
                widget.addItem(item)
        self.count.setText(f'已登记 {len(self.existing_records)} 项 · 本次新增 {len(self.records)} 项')

    def remove_selected(self):
        indices = sorted((self.additions.row(item) for item in self.additions.selectedItems()), reverse=True)
        for index in indices:
            self.records.pop(index)
        self.render()

    def move_selected(self, step):
        indices = sorted({self.additions.row(item) for item in self.additions.selectedItems()})
        if not indices or (step < 0 and indices[0] == 0) or (step > 0 and indices[-1] == len(self.records) - 1):
            return
        for index in (indices if step < 0 else reversed(indices)):
            self.records[index], self.records[index + step] = self.records[index + step], self.records[index]
        self.render()
        for index in indices:
            self.additions.item(index + step).setSelected(True)


class MaterialImportDialog(QDialog):
    def __init__(self, sessions, selected_session_id=None, parent=None, append=False):
        super().__init__(parent)
        self.setWindowTitle('导入场次素材')
        self.resize(920, 650)
        self.session_rows = [copy.deepcopy(row) for row in sessions
                             if row.get('config', {}).get('library_record_type') != 'notes']
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.import_form = form
        self.mode = QComboBox()
        self.mode.addItem('新建拍摄场次', 'new')
        self.mode.addItem('追加到已有拍摄场次', 'append')
        if not self.session_rows:
            self.mode.model().item(1).setEnabled(False)
        form.addRow('导入方式', self.mode)
        self.recording_day = QComboBox()
        self.recording_day.setEditable(True)
        self.recording_day.addItems([f'day{day:02d}' for day in range(1, 5)])
        self.recording_day.lineEdit().setPlaceholderText('例如 2026-10-14 或 day05')
        form.addRow('拍摄日期', self.recording_day)
        self.recording_kind = QComboBox()
        self.recording_kind.setEditable(True)
        self.recording_kind.addItems(['通条', '分镜头'])
        self.recording_kind.lineEdit().setPlaceholderText('通条、分镜头或自定义类型')
        form.addRow('拍摄类型', self.recording_kind)
        self.camera_count = QSpinBox()
        self.camera_count.setRange(1, 26)
        self.camera_count.setValue(4)
        self.camera_count.setSuffix(' 个机位')
        camera_configuration = QWidget()
        camera_configuration_layout = QHBoxLayout(camera_configuration)
        camera_configuration_layout.setContentsMargins(0, 0, 0, 0)
        camera_configuration_layout.addWidget(self.camera_count)
        self.confirm_cameras_button = QPushButton('确定机位')
        camera_configuration_layout.addWidget(self.confirm_cameras_button)
        self._camera_configuration_confirmed = False
        form.addRow('机位数量', camera_configuration)
        self.camera_summary = QLabel()
        self.camera_summary.setWordWrap(True)
        form.addRow('本场机位', self.camera_summary)
        self.name = QLineEdit(self)
        self.name.setReadOnly(True)
        self.name.hide()
        self.session = QComboBox()
        for row in self.session_rows:
            self.session.addItem(row['name'], row['id'])
        if selected_session_id:
            self.session.setCurrentIndex(max(0, self.session.findData(selected_session_id)))
        form.addRow('已有拍摄场次', self.session)
        layout.addLayout(form)
        caption = QLabel('先确定拍摄日期、类型与机位数量，再登记切点 XML、PGM、各机代理和原始主 WAV。追加素材使用已有机位配置。')
        caption.setWordWrap(True)
        layout.addWidget(caption)
        self.tabs = QTabWidget()
        self.pickers = {}
        for kind, title in (('cut_xml', '切点 XML'), ('pgm', 'PGM'), ('proxy', '各机位代理'), ('main_wav', '原始主 WAV')):
            picker = MaterialSelection(kind, self)
            self.pickers[kind] = picker
            self.tabs.addTab(picker, title)
        layout.addWidget(self.tabs, 1)
        self.validation = QLabel('')
        self.validation.setWordWrap(True)
        layout.addWidget(self.validation)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('登记素材')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.mode.currentIndexChanged.connect(self.update_existing)
        self.session.currentIndexChanged.connect(self.update_existing)
        self.recording_day.currentTextChanged.connect(self.update_recording_name)
        self.recording_kind.currentTextChanged.connect(self.update_recording_name)
        self.camera_count.valueChanged.connect(self.camera_count_changed)
        self.confirm_cameras_button.clicked.connect(self.confirm_camera_configuration)
        if append and self.session_rows:
            self.mode.setCurrentIndex(1)
        self.update_existing()

    @staticmethod
    def registered(config):
        result = {'cut_xml': [], 'pgm': [], 'proxy': [], 'main_wav': []}
        def add(kind, path, camera=''):
            if not isinstance(path, str) or not path:
                return
            row = {'path': path}
            if kind == 'proxy':
                row['camera'] = camera
            if row not in result[kind]:
                result[kind].append(row)
        add('cut_xml', config.get('cut_xml'))
        for value in config.get('cut_xml_paths', []):
            add('cut_xml', value.get('path') if isinstance(value, dict) else value)
        add('pgm', config.get('pgm'))
        for value in config.get('pgm_sources', []):
            add('pgm', value.get('path') if isinstance(value, dict) else value)
        for value in config.get('pgm_overrides', {}).values():
            add('pgm', value.get('path') if isinstance(value, dict) else value)
        for row in config.get('sources', []):
            add('proxy', row.get('proxy_path') or row.get('path'), row.get('camera', ''))
        for row in config.get('sound_sources', []):
            add('main_wav', row.get('path'))
        for row in config.get('library_media', []):
            if row.get('kind') in result:
                add(row['kind'], row.get('path'), row.get('camera', ''))
        return result

    def update_existing(self, *_args):
        append = self.mode.currentData() == 'append'
        self.import_form.setRowVisible(self.session, append)
        self.recording_day.setEnabled(not append)
        self.recording_kind.setEnabled(not append)
        self.camera_count.setEnabled(not append)
        self.confirm_cameras_button.setEnabled(not append)
        self.name.setEnabled(not append)
        self.session.setEnabled(append)
        selected = next((row for row in self.session_rows if row['id'] == self.session.currentData()), None)
        if append and selected:
            from camera_schema import cameras_for
            from recording_schema import KINDS
            config = selected.get('config', {})
            day, kind = config.get('recording_day'), config.get('recording_kind')
            self.recording_day.setCurrentText(f'day{day:02d}' if isinstance(day, int) else str(day or '未登记'))
            self.recording_kind.setCurrentText(KINDS.get(kind, str(kind or '未登记')))
            self.camera_count.setValue(len(cameras_for(config)))
        self._camera_configuration_confirmed = append and bool(selected)
        self.tabs.setEnabled(self._camera_configuration_confirmed)
        self.confirm_cameras_button.setText('已确定' if self._camera_configuration_confirmed else '确定机位')
        self.update_recording_name()
        self.update_camera_choices()
        existing = self.registered(selected.get('config', {})) if append and selected else {}
        for kind, picker in self.pickers.items():
            picker.set_existing(existing.get(kind, []))
        self.validation.clear()

    def update_recording_name(self, *_args):
        selected = next((row for row in self.session_rows if row['id'] == self.session.currentData()), None)
        if self.mode.currentData() == 'append' and selected:
            self.name.setText(selected['name'])
            return
        from recording_schema import recording_name
        try:
            self.name.setText(recording_name(self.recording_day.currentText(), self.recording_kind.currentText()))
        except ValueError:
            self.name.clear()

    def update_camera_choices(self, *_args):
        cameras = self.camera_ids()
        self.pickers['proxy'].set_camera_ids(cameras)
        self.camera_summary.setText(' / '.join(cameras) + ('（追加时保持不变）' if self.mode.currentData() == 'append' else ''))

    def camera_count_changed(self, *_args):
        self.update_camera_choices()
        if self.mode.currentData() == 'new':
            self._camera_configuration_confirmed = False
            self.tabs.setEnabled(False)
            self.confirm_cameras_button.setText('确定机位')

    def confirm_camera_configuration(self):
        if any(row.get('camera') not in self.camera_ids() for row in self.pickers['proxy'].records):
            self.validation.setText('已添加代理超出新机位数量；请恢复数量并移除相应新增代理后再调整。')
            return
        self._camera_configuration_confirmed = True
        self.tabs.setEnabled(True)
        self.confirm_cameras_button.setText('已确定')
        self.validation.clear()

    def add_paths(self, kind, paths, camera=''):
        self.pickers[kind].add_paths(paths, camera)

    def materials(self):
        append = self.mode.currentData() == 'append'
        selected = next((row for row in self.session_rows if row['id'] == self.session.currentData()), None)
        config = selected.get('config', {}) if append and selected else {}
        from recording_schema import normalize_recording
        day, kind = ((config.get('recording_day'), config.get('recording_kind')) if append else
            normalize_recording(self.recording_day.currentText(), self.recording_kind.currentText()))
        return {'cut_xml_paths': [row['path'] for row in self.pickers['cut_xml'].records],
                'recording_day': day,
                'recording_kind': kind,
                'camera_count': self.camera_count.value(),
                'pgm_paths': [row['path'] for row in self.pickers['pgm'].records],
                'batch_name': selected['name'] if append and selected else self.name.text().strip(),
                'proxies': copy.deepcopy(self.pickers['proxy'].records),
                'audio_paths': [row['path'] for row in self.pickers['main_wav'].records],
                'session_id': selected['id'] if append and selected else None,
                'expected_revision': selected['revision'] if append and selected else None}

    def accept(self):
        try:
            payload = self.materials()
        except ValueError as exc:
            self.validation.setText(str(exc))
            return
        if not self._camera_configuration_confirmed:
            self.validation.setText('请先选择机位数量并点击“确定机位”。')
        elif not payload['batch_name']:
            self.validation.setText('请填写拍摄场次名称。')
        elif self.mode.currentData() == 'append' and not payload['session_id']:
            self.validation.setText('请选择已有拍摄场次。')
        elif not payload['session_id'] and any(
            row.get('config', {}).get('recording_day') == payload['recording_day'] and
            row.get('config', {}).get('recording_kind') == payload['recording_kind']
            for row in self.session_rows):
            self.validation.setText('该拍摄场次已经登记，请选择“追加到已有拍摄场次”。')
        elif not any(payload[key] for key in ('cut_xml_paths', 'pgm_paths', 'proxies', 'audio_paths')):
            self.validation.setText('请选择本次需要登记的素材。')
        elif not payload['session_id'] and not payload['pgm_paths']:
            self.validation.setText('新拍摄场次请至少添加一条 PGM。')
        elif any(row.get('camera') not in self.camera_ids() for row in payload['proxies']):
            self.validation.setText('代理机位超出本场配置；请调整机位数量或移除该新增代理。')
        else:
            super().accept()

    def camera_ids(self):
        from camera_schema import cameras_for
        if self.mode.currentData() == 'append':
            selected = next((row for row in self.session_rows if row['id'] == self.session.currentData()), None)
            if selected:
                return cameras_for(selected.get('config', {}))
        return cameras_for({'camera_count': self.camera_count.value()})


class LibraryPanel(QWidget):
    session_saved = pyqtSignal(object)
    busy_changed = pyqtSignal(bool)
    material_session_changed = pyqtSignal(object)
    reference_changed = pyqtSignal(object)
    clip_selection_changed = pyqtSignal(object)
    reference_export_requested = pyqtSignal(object)
    baseline_map_requested = pyqtSignal(object)
    manual_timeline_requested = pyqtSignal(object)
    workflow_stage_requested = pyqtSignal(str)
    working_session_selected = pyqtSignal(object)
    catalogue_refreshed = pyqtSignal(object)
    preparation_issue_requested = pyqtSignal(object)
    save_project_requested = pyqtSignal()

    def __init__(self, capture_current, load_session, parent=None, auto_restore=True):
        super().__init__(parent)
        self.capture_current = capture_current
        self.load_session_callback = load_session
        from local_project import LocalProjectClient
        self.client = LocalProjectClient()
        self.worker = None
        self.external_busy = False
        self._session = None
        self._sessions = []
        self._scripts = []
        self._field_notes = []
        self._transcripts = []
        self._pending_intervals = []
        self._candidates = []
        self._pgm_match_versions = []
        self._visible_candidates = []
        self._advanced_candidates = []
        self._timeline_nodes = []
        self._prepared_timeline_nodes=None
        self._timeline_by_id = {};self._candidate_by_id = {};self._candidates_by_line = {}
        self._pending_angle_mapper = None
        self._previews = []
        self._controls = []
        self.workflow = None
        self.reference_session_id = None
        self.selected_clip_pairs = {}
        self.reference_state = {}
        self._baseline_map_cut = None
        self._manual_prepared = None
        self._manual_dialog = None
        self._session_readiness = {}
        self._selected_story_take_id = ''
        self._story_clip_by_id = {}
        self._story_intervals_by_candidate = {}
        self._browser_dialog = None
        self._browser_geometry = None
        self.workflow_stage = 'materials'
        self._technical_widget = None
        self._closed = False
        self._build()
        QApplication.instance().installEventFilter(self)
        from library_thumbnails import SourceFrameLoader
        self._inline_io=SourceFrameLoader(self,decoder=lambda path,index:Path(path).is_file())
        self._inline_timer=QTimer(self);self._inline_timer.setSingleShot(True);self._inline_timer.setInterval(100)
        self._inline_timer.timeout.connect(self._begin_inline_load)
        self._inline_request=None
        from pgm_workspace import build_workspace
        build_workspace(self)

    def eventFilter(self, watched, event):
        # Claim Space before button/list shortcuts, but keep normal text editing.
        if event.type() not in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress):
            return False
        if event.key() != Qt.Key.Key_Space or event.modifiers() != Qt.KeyboardModifier.NoModifier:
            return False
        preview = getattr(self, 'inline_preview', None)
        browser_visible = bool(self._browser_dialog and self._browser_dialog.isVisible())
        has_preview = bool(preview and preview.path)
        if (self._closed or not browser_visible
                or not (has_preview or self._inline_request) or (QApplication.activeModalWidget() and
                    QApplication.activeModalWidget() is not self._browser_dialog)):
            return False
        target = watched if isinstance(watched, QWidget) else QApplication.focusWidget()
        if not target or not (target is self._browser_dialog or target is getattr(preview, 'video', None) or
                              self._browser_dialog.isAncestorOf(target)):
            return False
        cursor = target
        while cursor is not None:
            if (isinstance(cursor, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox))
                    or isinstance(cursor, QComboBox) and cursor.isEditable()
                    or isinstance(cursor, QMenu)):
                return False
            cursor = cursor.parentWidget()
        if isinstance(target, QComboBox) and target.view().isVisible():
            return False
        event.accept()
        if event.type() == QEvent.Type.KeyPress and not event.isAutoRepeat():
            self._toggle_inline_playback()
        return True

    def _toggle_inline_playback(self):
        # A just-selected source may still be in the debounce/existence stage.
        # Space then belongs to that source, never to the previous decoder.
        request = self._inline_request
        if request and request[0] != getattr(self, '_inline_key', None):
            self._inline_request = (request[0], not request[1])
        elif self.inline_preview and self.inline_preview.path:
            self.inline_preview.toggle_playback()
        else:
            self.preview_workspace_selection()

    def _button(self, title, action):
        button = QPushButton(title)
        button.clicked.connect(lambda _checked=False: self.guarded(action))
        self._controls.append(button)
        return button

    @staticmethod
    def _table(headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        # Headers only need a representative viewport, not every long source
        # path or subtitle whenever one row changes in a large catalogue.
        table.horizontalHeader().setResizeContentsPrecision(60)
        table.horizontalHeader().setStretchLastSection(True)
        return table

    def _build(self):
        # Detailed connection/session tools stay available, but do not compete
        # with the director's script and picture comparison in the main view.
        self._build_advanced()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(12)
        heading = QHBoxLayout()
        title = QLabel('项目流程')
        title.setObjectName('pageTitle')
        heading.addWidget(title)
        self.catalogue_label = QLabel('')
        self.catalogue_label.setObjectName('mutedLabel')
        self.catalogue_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        heading.addWidget(self.catalogue_label, 1)
        heading.addWidget(QLabel('当前场次'))
        self.working_session = QComboBox()
        self.working_session.setMinimumWidth(165)
        self.working_session.setMaximumWidth(240)
        self.working_session.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.working_session.setPlaceholderText('选择拍摄场次')
        self.working_session.currentIndexChanged.connect(self.working_session_changed)
        heading.addWidget(self.working_session)
        self.readiness_button = self._button('准备状态', self.show_preparation_status)
        heading.addWidget(self.readiness_button)
        self.refresh_button = self._button('刷新项目素材', self.refresh)
        heading.addWidget(self.refresh_button)
        self.advanced_button = QToolButton()
        self.advanced_button.setText('管理与设置')
        self.advanced_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.advanced_button)
        for title, action in (('场次核查与备注', lambda: self.show_advanced(0)),
                              ('可选：台本与识别设置', lambda: self.show_advanced(2)),
                              ('导出跨场对应表', self.export_correspondence)):
            menu.addAction(title, lambda checked=False, action=action: self.guarded(action))
        self.advanced_button.setMenu(menu)
        self._controls.append(self.advanced_button)
        heading.addWidget(self.advanced_button)
        layout.addLayout(heading)

        steps = QHBoxLayout()
        steps.setSpacing(10)
        self.import_materials_button = self._button('导入场次素材', self.ingest_materials)
        self.import_pgm_button = self.import_materials_button  # Compatibility for project integrations.
        self.import_script_button = self._button('导入台本', self.ingest_script)
        self.materials_stage_button = self._button('场次素材', lambda: self.request_workflow_stage('materials'))
        self.sound_stage_button = self._button('声音合板', lambda: self.request_workflow_stage('sound'))
        self.sync_button = self._button('开始声音合板', self.sync_recordings)
        self.camera_button = self._button('机位与同步', lambda: self.request_workflow_stage('camera'))
        self.script_stage_button = self._button('PGM编排', lambda: self.request_workflow_stage('script'))
        self.reference_stage_button = self._button('基准与备选', lambda: self.request_workflow_stage('reference'))
        self.recognize_button = self._button('开始本场台本匹配', self.recognize_and_match)
        self.recognize_button.setToolTip('处理当前所选拍摄场次；已完成的台词转录会复用，并重新计算台本对应关系。')
        self.choose_reference_button = self._button('选择基准通条', self.choose_reference)
        self.baseline_map_button = self._button('基准台本映射', self.open_baseline_mapping)
        self.export_reference_button = self._button('导出基准＋备选 XML', lambda: self.export_reference(True, True, manual=True))
        self.export_baseline_button = self._button('仅导出基准 XML', lambda: self.export_reference(False, True, manual=True))
        self.export_alternatives_button = self._button('仅导出备选 XML', lambda: self.export_reference(True, False, manual=True))
        groups = (('1  场次素材', 'materials', self.materials_stage_button),
                  ('2  机位与同步', 'camera', self.camera_button),
                  ('3  声音合板', 'sound', self.sound_stage_button),
                  ('4  PGM编排', 'script', self.script_stage_button),
                  ('5  基准与备选', 'reference', self.reference_stage_button))
        self.stage_buttons, self.stage_boxes = {}, {}
        for label, stage, button in groups:
            box = QGroupBox(label)
            box.setObjectName('workflowStep')
            box_layout = QHBoxLayout(box)
            box_layout.setContentsMargins(8, 7, 8, 7)
            button.setCheckable(True)
            self.stage_buttons[stage], self.stage_boxes[stage] = button, box
            box_layout.addWidget(button)
            steps.addWidget(box, 1)
        layout.addLayout(steps)
        self.stage_resources = QWidget()
        resources = QHBoxLayout(self.stage_resources)
        resources.setContentsMargins(0, 0, 0, 0)
        resources.addWidget(self.import_materials_button)
        resources.addWidget(self.import_script_button)
        self.reference_label = QLabel('尚未选择基准通条')
        self.reference_label.setObjectName('mutedLabel')
        self.reference_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.stage_hint = QLabel('选择拍摄场次，一次登记切点 XML、PGM、代理和原始主 WAV。')
        self.stage_hint.setObjectName('mutedLabel')
        self.stage_hint.setWordWrap(True)
        resources.addWidget(self.stage_hint, 1)
        resources.addWidget(self.sync_button)
        resources.addWidget(self.recognize_button)
        resources.addWidget(self.choose_reference_button)
        resources.addWidget(self.baseline_map_button)
        resources.addWidget(self.reference_label, 1)
        resources.addWidget(self.export_reference_button)
        resources.addWidget(self.export_baseline_button)
        resources.addWidget(self.export_alternatives_button)
        layout.addWidget(self.stage_resources)
        self.baseline_map_status = QLabel('基准台本映射尚未建立')
        self.baseline_map_status.setObjectName('mutedLabel')
        self.baseline_map_status.setWordWrap(True)
        layout.addWidget(self.baseline_map_status)
        self._refresh_reference_controls()

        self.reference_readiness = self._table(['拍摄场次', '机位', '校准', '声音'])
        self.reference_readiness.setMaximumHeight(124)
        self.reference_readiness.setMinimumHeight(65)
        self.reference_readiness.cellDoubleClicked.connect(self._open_readiness_session)
        self.reference_readiness.hide()
        layout.addWidget(self.reference_readiness)

        split = self.browser_split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False);split.setHandleWidth(6)
        script_side = QWidget()
        script_layout = QVBoxLayout(script_side)
        script_layout.setContentsMargins(0, 0, 8, 0)
        script_top = QHBoxLayout()
        self.story_axis = QComboBox(self)
        self.story_axis.addItem('PGM 区间时间轴', 'pgm')
        self.story_axis.hide()
        script_top.addWidget(QLabel('台本'))
        self.script_search = QLineEdit()
        self.script_search.setPlaceholderText('查找台词／行号')
        self.script_search.textChanged.connect(self.filter_script_tree)
        script_top.addWidget(self.script_search, 1)
        script_layout.addLayout(script_top)
        self.script_mode = QComboBox()
        for title, mode in (('全部', 'all'), ('多素材', 'multiple'),
                            ('待核对', 'review'), ('无素材', 'empty')):
            self.script_mode.addItem(title, mode)
        self.script_mode.currentIndexChanged.connect(lambda _index: self.filter_script_tree())
        self.script_mode.setParent(self)
        self.script_mode.hide()
        self.pgm_status_button = self._button('PGM匹配清单', self.show_pgm_match_status)
        script_layout.addWidget(self.pgm_status_button)
        from script_timeline import ScriptTimeline
        self.script_timeline = ScriptTimeline()
        self.script_tree = self.script_timeline
        self.script_tree.currentItemChanged.connect(self.script_tree_selected)
        script_body = QWidget()
        script_body_layout = QHBoxLayout(script_body)
        script_body_layout.setContentsMargins(0, 0, 0, 0)
        script_body_layout.setSpacing(4)
        script_body_layout.addWidget(self.script_tree, 1)
        script_layout.addWidget(script_body, 1)
        self.pgm_timeline = ScriptTimeline()
        self.pgm_timeline.setAccessibleName('PGM 区间时间轴')
        self.pgm_timeline.hide()
        self.pgm_timeline.currentItemChanged.connect(self.pgm_interval_selected)
        self._pgm_interval_nodes, self._pgm_interval_by_id = [], {}
        self._prepared_pgm_intervals = None
        self.pgm_timeline.setParent(script_side)
        self.script_count = QLabel('导入台本后，点击区间浏览对应画面。')
        self.script_count.setWordWrap(True)
        script_layout.addWidget(self.script_count)
        split.addWidget(script_side)

        comparison_side = QWidget()
        comparison_layout = QVBoxLayout(comparison_side)
        comparison_layout.setContentsMargins(0, 0, 0, 0)
        self.main_pages = QTabWidget()
        self.review_split = QSplitter(Qt.Orientation.Horizontal)
        self.review_split.setChildrenCollapsible(False)
        self.review_split.setHandleWidth(6)
        self.review_split.addWidget(self.main_pages)
        comparison_layout.addWidget(self.review_split, 1)
        compare_page = QWidget()
        compare_layout = QVBoxLayout(compare_page)
        self.selected_line = QLabel('选择左侧台词')
        self.selected_line.setWordWrap(True)
        self.selected_line.setParent(compare_page)
        self.selected_line.hide()  # The selected text is already shown on the script axis.
        self.match_count = QLabel('')
        self.match_count.setWordWrap(True)
        compare_layout.addWidget(self.match_count)
        match_filters = QHBoxLayout()
        self.take_session_filter = QComboBox()
        self.take_session_filter.addItem('全部场次', '')
        self.take_session_filter.setMinimumContentsLength(10)
        self.take_session_filter.currentIndexChanged.connect(self.filter_candidates)
        self.take_mode = QComboBox()
        for title, mode in (('全部PGM', 'all'), ('其他场次已匹配', 'alternatives'),
                            ('绿色已核实备选', 'selected'), ('待核实', 'review')):
            self.take_mode.addItem(title, mode)
        self.take_mode.currentIndexChanged.connect(self.filter_candidates)
        match_filters.addWidget(self.take_session_filter, 1)
        match_filters.addWidget(self.take_mode, 1)
        self.take_filter_bar = QWidget()
        self.take_filter_bar.setLayout(match_filters)
        match_filters.setContentsMargins(0, 0, 0, 0)
        self.selection_hint = QLabel('绿色备选自动导出，按整条PGM首句对齐基准；片头片尾及机位切换全部保留。')
        self.selection_hint.setWordWrap(True)
        self.match_list = QListWidget()
        self.match_list.setMinimumHeight(60)
        self.match_list.setWordWrap(True)
        self.match_list.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.match_list.setResizeMode(self.match_list.ResizeMode.Adjust)
        self.match_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.match_list.setSpacing(7)
        self.match_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.match_list.currentRowChanged.connect(self.match_selected)
        from take_cards import TakeCardDelegate
        self.match_list.setItemDelegate(TakeCardDelegate(self.match_list))
        from library_thumbnails import ThumbnailLoader, decode_thumbnail
        self.match_thumbnails = ThumbnailLoader(self.match_list, self,
            decoder=lambda path, seconds: decode_thumbnail(path, seconds, 320, 180))
        compare_layout.addWidget(self.match_list, 1)
        self.match_sync_status = QLabel('', compare_page)
        self.match_sync_status.hide()
        self.select_take_button = self._button('确认基准位置', self.confirm_current_baseline)
        self.match_details_toggle = QToolButton()
        self.match_details_toggle.setText('素材详情')
        self.match_details_toggle.setCheckable(True)
        match_filters.addWidget(self.match_details_toggle)
        self.match_detail = QPlainTextEdit()
        self.match_detail.setReadOnly(True)
        self.match_detail.setMaximumHeight(82)
        self.match_detail.setPlaceholderText('选中片段查看 PGM、代理与声音来源。')
        self.match_detail.hide()
        self.match_details_toggle.toggled.connect(self.match_detail.setVisible)
        compare_layout.addWidget(self.match_detail)
        compare_actions = QWidget()
        action_layout = QVBoxLayout(compare_actions)
        action_layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(self._button('播放片段', self.preview_current_inline))
        row.addWidget(QLabel('画面'))
        self.angle_choice = QComboBox()
        self.angle_choice.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.angle_choice.setMinimumContentsLength(5)
        self.angle_choice.setMinimumWidth(105)
        self.angle_choice.setMaximumWidth(210)
        self.angle_choice.currentIndexChanged.connect(self.angle_selected)
        row.addWidget(self.angle_choice, 1)
        action_layout.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(self.select_take_button)
        self.remove_match_button = self._button('移除匹配', self.remove_pgm_match)
        self.remove_match_button.setToolTip('移除整条PGM的台本对应关系，之后可重新人工归类；不会删除媒体文件。')
        row.addWidget(self.remove_match_button)
        row.addWidget(self._button('备注', self.edit_candidate_note))
        row.addStretch()
        action_layout.addLayout(row)
        coverage_row = QHBoxLayout()
        coverage_row.addWidget(QLabel('台本区间'))
        self.coverage_range_input = QLineEdit()
        self.coverage_range_input.setPlaceholderText('55-76 或 55')
        self.coverage_range_input.setMinimumWidth(55)
        self.coverage_range_input.returnPressed.connect(lambda: self.guarded(self.apply_coverage_input))
        coverage_row.addWidget(self.coverage_range_input, 1)
        self.coverage_apply_button = self._button('应用', self.apply_coverage_input)
        coverage_row.addWidget(self.coverage_apply_button)
        action_layout.addLayout(coverage_row)
        self.compare_scroll = QScrollArea()
        self.compare_scroll.setWidgetResizable(True)
        self.compare_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.compare_scroll.setWidget(compare_page)
        self.main_pages.addTab(self.compare_scroll, '台本对照')

        pending_page = QWidget()
        pending_layout = QVBoxLayout(pending_page)
        pending_filters = QHBoxLayout()
        self.pending_mode = QComboBox()
        self.pending_mode.addItem('未匹配台本', 'unmatched')
        self.pending_mode.addItem('轨道移出', 'removed')
        self.pending_mode.currentIndexChanged.connect(self.render_pending)
        pending_filters.addWidget(self.pending_mode)
        self.pending_search = QLineEdit()
        self.pending_search.setPlaceholderText('搜索未归类的现场台词、PGM或场次')
        self._pending_search_timer = QTimer(self)
        self._pending_search_timer.setSingleShot(True)
        self._pending_search_timer.setInterval(150)
        self._pending_search_timer.timeout.connect(self.render_pending)
        self.pending_search.textChanged.connect(lambda _text: self._pending_search_timer.start())
        pending_filters.addWidget(self.pending_search, 1)
        pending_layout.addLayout(pending_filters)
        self.pending_count = QLabel('')
        self.pending_count.setWordWrap(True)
        pending_layout.addWidget(self.pending_count)
        self.pending_list = QListWidget()
        self.pending_list.setMinimumHeight(60)
        self.pending_list.setWordWrap(True)
        self.pending_list.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.pending_list.setResizeMode(self.pending_list.ResizeMode.Adjust)
        self.pending_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.pending_list.setSpacing(7)
        self.pending_list.currentRowChanged.connect(self.pending_selected)
        pending_layout.addWidget(self.pending_list, 1)
        pending_actions = QWidget()
        pending_action_layout = QVBoxLayout(pending_actions)
        pending_action_layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(self._button('播放片段', self.preview_pending_inline))
        row.addWidget(QLabel('画面'))
        self.pending_angle_choice = QComboBox()
        self.pending_angle_choice.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.pending_angle_choice.setMinimumContentsLength(5)
        self.pending_angle_choice.setMinimumWidth(105)
        self.pending_angle_choice.currentIndexChanged.connect(self.pending_angle_selected)
        row.addWidget(self.pending_angle_choice, 1)
        pending_action_layout.addLayout(row)
        self.manual_locate_button = self._button('人工定位', self.manual_locate_pgm)
        pending_action_layout.addWidget(self.manual_locate_button)
        self.pending_range = QWidget()
        range_box = QVBoxLayout(self.pending_range)
        range_box.setContentsMargins(0, 0, 0, 0)
        range_box.addWidget(QLabel('PGM秒区间'))
        range_layout = QHBoxLayout()
        self.pending_start, self.pending_end = QDoubleSpinBox(), QDoubleSpinBox()
        for spin in (self.pending_start, self.pending_end):
            spin.setDecimals(3)
            spin.setRange(0, 1000000)
            spin.setSingleStep(.04)
            spin.valueChanged.connect(self.pending_range_changed)
        range_layout.addWidget(self.pending_start)
        range_layout.addWidget(QLabel('至'))
        range_layout.addWidget(self.pending_end)
        self.pending_reason = QLabel('')
        self.pending_reason.setWordWrap(True)
        range_box.addLayout(range_layout)
        range_box.addWidget(self.pending_reason)
        self.pending_range.setParent(pending_actions)
        self.pending_range.hide()
        self.pending_scroll = QScrollArea()
        self.pending_scroll.setWidgetResizable(True)
        self.pending_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.pending_scroll.setWidget(pending_page)
        self.main_pages.addTab(self.pending_scroll, '待人工匹配')
        self.main_pages.currentChanged.connect(self.main_page_changed)
        self.comparison_actions = QStackedWidget()
        self.comparison_actions.addWidget(compare_actions)
        self.comparison_actions.addWidget(pending_actions)
        self.comparison_actions.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.review_choices = QWidget()
        self.review_choices.setMinimumWidth(270)
        choices_layout = QVBoxLayout(self.review_choices)
        choices_layout.setContentsMargins(0,0,0,0)
        self.review_split.insertWidget(0,self.review_choices)
        comparison_layout.insertWidget(0, self.take_filter_bar)
        comparison_layout.insertWidget(1, self.selection_hint)
        choices_layout.addWidget(self.main_pages,1)
        choices_layout.addWidget(self.comparison_actions)
        # Reparenting main_pages removed its temporary splitter entry.
        self.main_pages.currentChanged.connect(self.comparison_actions.setCurrentIndex)
        self.inline_host = QWidget()
        self.inline_host.setMinimumWidth(340)
        self.inline_host.setMinimumHeight(160)
        self.inline_host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.inline_layout = QVBoxLayout(self.inline_host)
        self.inline_layout.setContentsMargins(0, 0, 0, 0)
        self.inline_placeholder = QLabel('选择片段查看画面')
        self.inline_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.inline_layout.addWidget(self.inline_placeholder)
        self.inline_preview = None
        self.review_split.addWidget(self.inline_host)
        self.review_split.setStretchFactor(0,1)
        self.review_split.setStretchFactor(1,2)
        self.review_split.setSizes([320,510])
        self.review_split.handle(1).setToolTip('左右拖动调整片段列表和预览画面宽度')
        split.addWidget(comparison_side)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 3)
        split.setSizes([280, 880])
        split.handle(1).setToolTip('左右拖动调整台本和画面宽度')
        self.catalogue_page = QWidget()
        catalogue_layout = QVBoxLayout(self.catalogue_page)
        catalogue_layout.setContentsMargins(0, 0, 0, 0)
        browser_header = QHBoxLayout()
        self.browser_title = QLabel('PGM时间轴')
        self.browser_title.setObjectName('pageTitle')
        browser_header.addWidget(self.browser_title, 1)
        self.browser_refresh_button = self._button('刷新项目素材', self.refresh)
        browser_header.addWidget(self.browser_refresh_button)
        self.browser_save_button = self._button('保存项目', self.save_project_requested.emit)
        browser_header.addWidget(self.browser_save_button)
        self.browser_return_button = QPushButton('返回项目流程')
        self.browser_return_button.clicked.connect(self.close_browser_workspace)
        browser_header.addWidget(self.browser_return_button)
        catalogue_layout.addLayout(browser_header)
        from horizontal_story import HorizontalStoryTimeline
        self.story_timeline = HorizontalStoryTimeline()
        self.story_timeline.take_selected.connect(lambda identity: self.guarded(
            lambda: self._story_take_selected(identity)))
        self.story_timeline.take_double_clicked.connect(lambda identity: self.guarded(
            lambda: self._edit_story_take(identity)))
        self.story_timeline.line_selected.connect(self._story_line_selected)
        self.story_timeline.coverage_highlighted.connect(self._highlight_script_coverage)
        self.story_timeline.coverage_edited.connect(lambda identity, first, last: self.guarded(
            lambda: self._story_coverage_edited(identity, first, last)))
        self.story_browser_split = QSplitter(Qt.Orientation.Vertical)
        self.story_browser_split.setChildrenCollapsible(False)
        self.story_browser_split.setHandleWidth(6)
        split.setMinimumHeight(200)
        self.story_timeline.setMinimumHeight(240)
        self.story_browser_split.addWidget(split)
        self.story_browser_split.addWidget(self.story_timeline)
        self.story_browser_split.setStretchFactor(0, 2)
        self.story_browser_split.setStretchFactor(1, 3)
        self.story_browser_split.setSizes([320, 480])
        self.story_browser_split.handle(1).setToolTip('上下拖动调整画面与PGM轨道区域')
        catalogue_layout.addWidget(self.story_browser_split, 1)
        # The large browser belongs only to its own window, never to a stage's
        # compact content stack. Reuse its widgets and data on every opening.
        self.catalogue_page.setParent(self)
        self.catalogue_page.hide()
        self.catalogue_launch_page = QWidget()
        launch_layout = QVBoxLayout(self.catalogue_launch_page)
        launch_layout.setContentsMargins(24, 24, 24, 24)
        launch_layout.addStretch(1)
        launch_title = QLabel('PGM时间轴')
        launch_title.setObjectName('pageTitle')
        launch_layout.addWidget(launch_title, 0, Qt.AlignmentFlag.AlignHCenter)
        self.browser_summary = QLabel('尚未导入PGM')
        self.browser_summary.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.browser_summary.setWordWrap(True)
        launch_layout.addWidget(self.browser_summary)
        self.browser_reference_summary = QLabel('')
        self.browser_reference_summary.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.browser_reference_summary.setWordWrap(True)
        self.browser_reference_summary.setObjectName('mutedLabel')
        launch_layout.addWidget(self.browser_reference_summary)
        launch_actions = QHBoxLayout()
        launch_actions.addStretch(1)
        self.browser_open_button = QPushButton('打开PGM时间轴')
        self.browser_open_button.setObjectName('primaryButton')
        self.browser_open_button.setMinimumHeight(44)
        self.browser_open_button.clicked.connect(lambda: self.guarded(self.open_manual_timeline))
        self.expand_browser_button = self.browser_open_button
        launch_actions.addWidget(self.browser_open_button)
        launch_actions.addStretch(1)
        launch_layout.addLayout(launch_actions)
        launch_hint = QLabel('在独立窗口整理PGM、拖动备选对齐基准；无需台本或语音识别。')
        launch_hint.setObjectName('mutedLabel')
        launch_hint.setWordWrap(True)
        launch_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        launch_layout.addWidget(launch_hint)
        launch_layout.addStretch(2)
        self.content_stack = QStackedWidget()
        # A stack otherwise reserves the largest minimum of every hidden
        # technical tab, pushing a 768px display beyond its available height.
        # Each technical page already owns its scrolling/table viewport.
        self.content_stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.content_stack.setMinimumHeight(150)
        self.materials_page = QWidget()
        materials_layout = QVBoxLayout(self.materials_page)
        materials_layout.setContentsMargins(0, 0, 0, 0)
        materials_layout.addWidget(QLabel('已登记拍摄场次'))
        self.materials_overview = self._table(['拍摄场次', 'PGM', '代理', '主 WAV', '切点 XML', '准备状态'])
        self.materials_overview.itemSelectionChanged.connect(self._overview_selected)
        materials_layout.addWidget(self.materials_overview, 1)
        self.materials_summary = QLabel('尚未登记素材。点击“导入场次素材”，选择拍摄日期和通条／分镜头。')
        self.materials_summary.setWordWrap(True)
        self.materials_summary.setObjectName('mutedLabel')
        materials_layout.addWidget(self.materials_summary)
        self.content_stack.addWidget(self.materials_page)
        self.content_stack.addWidget(self.catalogue_launch_page)
        self.technical_placeholder = QLabel('先选择当前场次，再设置机位与同步。')
        self.technical_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.content_stack.addWidget(self.technical_placeholder)
        layout.addWidget(self.content_stack, 1)

        # Move the existing task feedback into the permanent main view.
        bottom = QHBoxLayout()
        for widget, stretch in ((self.status, 1), (self.progress, 0), (self.cancel, 0)):
            bottom.addWidget(widget, stretch)
        layout.addLayout(bottom)
        self.set_workflow_stage('materials')

    def attach_technical_widget(self, widget):
        """Embed the host's existing settings once; importing remains in stage 1."""
        if widget is self._technical_widget:
            return
        if self._technical_widget is not None:
            self.content_stack.removeWidget(self._technical_widget)
            self._technical_widget.setParent(None)
        self._technical_widget = widget
        if widget is not None:
            self.content_stack.addWidget(widget)
        self.set_workflow_stage(self.workflow_stage)

    def set_workflow_stage(self, stage):
        """Navigate without executing work or emitting another navigation request."""
        stage = {'sync': 'sound', 'match': 'script'}.get(stage, stage)
        if stage not in ('materials', 'camera', 'sound', 'script', 'reference'):
            raise ValueError('未知流程阶段：' + str(stage))
        self.workflow_stage = stage
        technical = self._technical_widget or self.technical_placeholder
        self.content_stack.setCurrentWidget(technical if stage in ('camera', 'sound')
            else self.materials_page if stage == 'materials' else self.catalogue_launch_page)
        for key, button in self.stage_buttons.items():
            button.setChecked(key == stage)
            button.setObjectName('primaryButton' if key == stage else '')
            button.style().unpolish(button)
            button.style().polish(button)
        script, reference = stage == 'script', stage == 'reference'
        self.selection_hint.setVisible(reference)
        if reference:
            self.main_pages.setCurrentIndex(0)
        self.inline_host.setMinimumHeight(70 if reference else 100)
        self.import_materials_button.setVisible(stage == 'materials')
        self.sync_button.setVisible(stage == 'sound')
        for widget in (self.import_script_button, self.recognize_button):
            widget.hide()  # Legacy optional script APIs stay compatible; manual editing needs no ASR.
        for widget in (self.choose_reference_button, self.reference_label, self.export_reference_button,
                       self.export_baseline_button, self.export_alternatives_button):
            widget.setVisible(reference)
        self.baseline_map_button.hide()
        self.baseline_map_status.hide()
        # Preparation has a dedicated permanent button/dialog; don't repeat a
        # large status table above the director's take browser.
        self.reference_readiness.hide()
        self.stage_hint.setVisible(not reference)
        hints = {
            'materials': '先确定本场机位数量，再登记切点 XML、PGM、本场各机位代理和原始主 WAV；追加沿用原场次。',
            'camera': '核对所选场次的机位、镜头识别和同步校准；全部代理未覆盖处使用无声PGM_REF画面，保存后进入声音合板。',
            'sound': '匹配原始主 WAV；未覆盖的区间音频留空，然后进入PGM素材编排。',
            'script': '素材整理按台本编号保留筛选结果；基准对齐支持按台词映射自动初排与手工校正，沿用机位和声音结果。',
        }
        if stage in hints:
            self.stage_hint.setText(hints[stage])
        if stage in ('camera', 'sound'):
            if getattr(self, '_inline_request', None):
                self._inline_request = (self._inline_request[0], False)
            if self.inline_preview is not None:
                self.inline_preview.pause()
        self._refresh_preparation_status()

    def request_workflow_stage(self, stage):
        self.set_workflow_stage(stage)
        self.workflow_stage_requested.emit(stage)

    @staticmethod
    def _preparation_label(value):
        return '已完成' if value is True else '待处理' if value is False else '待核查'

    def _preparation_count_label(self, state, kind):
        if kind == 'sound' and (state.get('sound_fallback') or state.get('sound_gap')):
            parts = [f'{state.get("sound_matched", 0)} 原WAV']
            empty=state.get('sound_fallback',0)+state.get('sound_gap',0)
            if empty:
                parts.append(f'{empty} 音频留空')
            if state.get('sound_pending'):
                parts.append(f'{state["sound_pending"]} 待核对')
            return ' + '.join(parts)
        total_key, done_key, verb = ('camera_total', 'camera_confirmed', '已确认') if kind == 'camera' \
            else ('sound_total', 'sound_matched', '已合板')
        total, done = state.get(total_key), state.get(done_key)
        if isinstance(total, int) and isinstance(done, int):
            return f'{done}/{total} {verb}' if total else '尚未登记'
        return self._preparation_label(state.get(kind + '_ready'))

    @staticmethod
    def _preparation_summary(state):
        if state.get('ready') is True:
            return '已就绪'
        remaining = []
        if state.get('camera_pending', 0):
            remaining.append(f'{state["camera_pending"]} 镜头')
        if state.get('sound_pending', 0):
            remaining.append(f'{state["sound_pending"]} 声音文件')
        if remaining:
            return '待核对 ' + ' / '.join(remaining)
        return '待校准' if state.get('calibration_ready') is False and state.get('camera_ready') is True \
            and state.get('sound_ready') is True else '待处理'

    def _readiness_rows(self, session_ids=None):
        selected = set(session_ids) if session_ids is not None else None
        return [(session, self._session_readiness.get(session['id'], {}))
                for session in self._sessions
                if session.get('config', {}).get('library_record_type') != 'notes'
                and (selected is None or session['id'] in selected)]

    def _refresh_preparation_status(self):
        if not hasattr(self, 'reference_readiness'):
            return
        rows = self._readiness_rows()
        self.reference_readiness.setFixedHeight(min(124, max(65, 40 + 32 * len(rows))))
        self._populate(self.reference_readiness, [(session['name'],
            self._preparation_count_label(state, 'camera'),
            self._preparation_label(state.get('calibration_ready')),
            self._preparation_count_label(state, 'sound')) for session, state in rows])
        for row, (session, state) in enumerate(rows):
            self.reference_readiness.item(row, 0).setData(Qt.ItemDataRole.UserRole, session['id'])
            detail = self._preparation_detail(session, state)
            for column in range(self.reference_readiness.columnCount()):
                self.reference_readiness.item(row, column).setToolTip(detail)
        selected = (self._session or {}).get('id')
        current = self._session_readiness.get(selected, {})
        text = '准备状态'
        if selected:
            text += ' · ' + self._preparation_summary(current)
        self.readiness_button.setText(text)
        self.readiness_button.setToolTip(self._preparation_detail(self._session, current) if selected
            else '查看各场次机位、校准和声音状态；双击场次可前往处理。')
        if hasattr(self, 'materials_overview'):
            self._refresh_materials_overview()

    @staticmethod
    def _registration_counts(config):
        # Registration is explicit.  No filename or camera inference here.
        groups = MaterialImportDialog.registered(config)
        return [len(groups.get(kind, [])) for kind in ('pgm', 'proxy', 'main_wav', 'cut_xml')]

    def _refresh_materials_overview(self):
        rows = self._readiness_rows()
        selected = (self._session or {}).get('id')
        self._populate(self.materials_overview, [(session['name'],
            *self._registration_counts(session.get('config', {})),
            self._preparation_summary(state)) for session, state in rows])
        self.materials_overview.blockSignals(True)
        for row, (session, state) in enumerate(rows):
            item = self.materials_overview.item(row, 0)
            item.setData(Qt.ItemDataRole.UserRole, session['id'])
            item.setToolTip(self._preparation_detail(session, state))
            if session['id'] == selected:
                self.materials_overview.selectRow(row)
        self.materials_overview.blockSignals(False)
        session = next((row for row, _state in rows if row['id'] == selected), None)
        if session:
            counts = self._registration_counts(session.get('config', {}))
            self.materials_summary.setText(f'当前：{session["name"]} · {counts[0]} 条 PGM · {counts[1]} 条代理 · '
                f'{counts[2]} 条主 WAV · {counts[3]} 个切点 XML。追加素材仍使用同一导入入口。')
        elif rows:
            self.materials_summary.setText(f'已登记 {len(rows)} 个拍摄场次。选择场次后进入“机位与同步”。')
        else:
            self.materials_summary.setText('尚未登记素材。点击“导入场次素材”，选择拍摄日期和通条／分镜头。')

    def _overview_selected(self):
        item = self.materials_overview.item(self.materials_overview.currentRow(), 0)
        if item is not None:
            index = self.working_session.findData(item.data(Qt.ItemDataRole.UserRole))
            if index >= 0 and index != self.working_session.currentIndex():
                self.working_session.setCurrentIndex(index)

    def _preparation_detail(self, session, state):
        name = (session or {}).get('name', '未选择场次')
        lines = [name, '机位：' + self._preparation_count_label(state, 'camera'),
                 '校准：' + self._preparation_label(state.get('calibration_ready')),
                 '声音：' + self._preparation_count_label(state, 'sound')]
        if state.get('cut_xml_ready') is not True:
            lines.append('切点 XML：' + self._preparation_label(state.get('cut_xml_ready')))
        next_steps = {'import_materials': '在“场次素材”中补齐该场切点 XML 和素材。',
                      'camera_sync': '进入“机位与同步”，完成机位识别与校准并保存。',
                      'sound_sync': '进入“声音合板”，匹配主 WAV；未覆盖的区间音频留空。'}
        for issue in state.get('issues', []):
            if isinstance(issue, dict):
                lines.extend(next_steps.get(str(issue[key]), str(issue[key]))
                             for key in ('message', 'next_step') if issue.get(key))
        if state.get('next_step'):
            lines.append(next_steps.get(str(state['next_step']), str(state['next_step'])))
        if not state:
            lines.append('载入该场次，完成机位设置／识别／校准并保存，再进行声音合板。')
        for row in (state.get('camera_pending_rows') or [])[:20]:
            lines.append(f'第 {row.get("shot_index", "?")} 镜头 · 第 {row.get("start", "?")} 帧：'
                         + str(row.get('message') or row.get('reason') or '待确认机位'))
        for row in (state.get('sound_pending_rows') or [])[:20]:
            lines.append(Path(row.get('pgm_path') or row.get('pgm') or '').name + '：'
                         + str(row.get('message') or row.get('reason') or '声音尚未合板'))
        for row in (state.get('sound_fallback_rows') or [])[:20]:
            lines.append(Path(row.get('pgm_path') or row.get('pgm') or '').name + '：未匹配原始 WAV，音频留空'
                         + ('（' + str(row['original_message']) + '）' if row.get('original_message') else ''))
        for row in (state.get('sound_gap_rows') or [])[:20]:
            lines.append(Path(row.get('pgm_path') or row.get('pgm') or '').name + '：原始 WAV 未覆盖，音频留空')
        return '\n'.join(dict.fromkeys(lines))

    def _locate_preparation_issue(self, session_id, stage, issue):
        if not self.select_working_session(session_id):
            return
        self.set_workflow_stage(stage)
        payload = dict(session_id=session_id, stage=stage, issue=copy.deepcopy(issue))
        if self.receivers(self.preparation_issue_requested):
            self.preparation_issue_requested.emit(payload)
        else:
            self.workflow_stage_requested.emit(stage)

    def _navigate_readiness(self, session_id):
        if not self.select_working_session(session_id):
            return
        state = self._session_readiness.get(session_id, {})
        stage = 'sound' if all(state.get(key) is True for key in (
            'camera_ready', 'calibration_ready', 'cut_xml_ready')) else 'camera'
        self.request_workflow_stage(stage)

    def _open_readiness_session(self, row, _column=0):
        item = self.reference_readiness.item(row, 0)
        if item is not None:
            self._navigate_readiness(item.data(Qt.ItemDataRole.UserRole))

    def show_preparation_status(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('各场次准备状态')
        dialog.resize(820, 570)
        layout = QVBoxLayout(dialog)
        hint = QLabel('完成机位与同步、声音合板后，再打开PGM时间轴手动编排并导出。选择场次可前往处理。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        table = self._table(['拍摄场次', '机位', '校准', '声音'])
        rows = self._readiness_rows()
        self._populate(table, [(session['name'],
            self._preparation_count_label(state, 'camera'),
            self._preparation_label(state.get('calibration_ready')),
            self._preparation_count_label(state, 'sound')) for session, state in rows])
        layout.addWidget(table, 1)
        detail = QPlainTextEdit()
        detail.setReadOnly(True)
        detail.setMaximumHeight(110)
        layout.addWidget(detail)
        layout.addWidget(QLabel('剩余问题（双击定位）'))
        pending = self._table(['类型', '待核对镜头／PGM', '原因'])
        layout.addWidget(pending, 1)
        issue_rows = []
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText('关闭')
        process = buttons.addButton('前往处理', QDialogButtonBox.ButtonRole.ActionRole)
        locate = buttons.addButton('定位所选问题', QDialogButtonBox.ButtonRole.ActionRole)
        process.setEnabled(bool(rows))
        locate.setEnabled(False)
        def show_issues():
            index = table.currentRow()
            issue_rows.clear()
            if 0 <= index < len(rows):
                session, state = rows[index]
                detail.setPlainText(self._preparation_detail(session, state))
                for stage, field in (('camera', 'camera_pending_rows'), ('sound', 'sound_pending_rows')):
                    issue_rows.extend((stage, copy.deepcopy(row)) for row in state.get(field, []))
            else:
                detail.clear()
            display = []
            for stage, issue in issue_rows:
                path = issue.get('pgm_path') or issue.get('pgm') or ''
                location = (f'第 {issue.get("shot_index", "?")} 镜头 · 第 {issue.get("start", "?")} 帧'
                            if stage == 'camera' else Path(path).name)
                display.append(('机位' if stage == 'camera' else '声音', location,
                                issue.get('message') or issue.get('reason') or '待核对'))
            self._populate(pending, display)
            for row, (_stage, issue) in enumerate(issue_rows):
                path = issue.get('pgm_path') or issue.get('pgm') or ''
                pending.item(row, 1).setToolTip(path)
                pending.item(row, 1).setData(Qt.ItemDataRole.UserRole, copy.deepcopy(issue))
            locate.setEnabled(bool(issue_rows))
            if issue_rows:
                pending.selectRow(0)
        table.itemSelectionChanged.connect(show_issues)
        def navigate():
            index = table.currentRow()
            if 0 <= index < len(rows):
                dialog.accept()
                self._navigate_readiness(rows[index][0]['id'])
        def locate_issue():
            selected, index = table.currentRow(), pending.currentRow()
            if 0 <= selected < len(rows) and 0 <= index < len(issue_rows):
                stage, issue = issue_rows[index]
                dialog.accept()
                self._locate_preparation_issue(rows[selected][0]['id'], stage, issue)
        process.clicked.connect(navigate)
        locate.clicked.connect(locate_issue)
        pending.cellDoubleClicked.connect(lambda *_args: locate_issue())
        table.cellDoubleClicked.connect(lambda *_args: navigate())
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if rows:
            table.selectRow(next((index for index, (session, _state) in enumerate(rows)
                                  if session['id'] == (self._session or {}).get('id')), 0))
        # Exposed only for host integration and deterministic widget tests.
        dialog.readiness_table, dialog.pending_table = table, pending
        dialog.locate_button, dialog.detail = locate, detail
        self._preparation_dialog = dialog
        dialog.exec()

    def _build_advanced(self):
        self.advanced = QDialog(self)
        self.advanced.setWindowTitle('素材库高级管理')
        self.advanced.resize(1160, 820)
        layout = QVBoxLayout(self.advanced)
        connection = QGroupBox('项目素材')
        connection_layout = QVBoxLayout(connection)
        self.url = QLineEdit(self); self.url.hide()
        self.token = QLineEdit(self); self.token.hide()
        self.actor = QLineEdit('本机', self); self.actor.hide()
        storage_hint = QLabel('先在Avid NEXIS Client Manager挂载工作区，再直接导入素材。匹配与核实结果随项目保存。')
        storage_hint.setWordWrap(True)
        connection_layout.addWidget(storage_hint)
        row = QHBoxLayout()
        row.addWidget(QLabel('项目'))
        self.production = QComboBox()
        self.production.currentIndexChanged.connect(lambda _index: self.production_changed())
        row.addWidget(self.production, 1)
        row.addWidget(self._button('刷新', self.refresh))
        connection_layout.addLayout(row)
        layout.addWidget(connection)
        self.pages = QTabWidget()
        layout.addWidget(self.pages, 1)

        session_page = QWidget()
        session_layout = QVBoxLayout(session_page)
        self.sessions = self._table(['场次', '更新人／时间', '版本'])
        self.sessions.setMaximumHeight(180)
        self.sessions.itemSelectionChanged.connect(self.session_selected)
        session_layout.addWidget(self.sessions, 1)
        self.material_keyword = QLineEdit()
        self.material_keyword.setPlaceholderText('筛选文件名、机位、素材备注')
        self.material_keyword.textChanged.connect(self._show_materials)
        session_layout.addWidget(self.material_keyword)
        self.materials = self._table(['类型', '机位', '素材位置', '备注'])
        self.materials.setMaximumHeight(160)
        session_layout.addWidget(self.materials, 1)
        form = QFormLayout()
        self.session_name = QLineEdit()
        self.notes = QPlainTextEdit()
        self.notes.setMaximumHeight(90)
        form.addRow('场次名称', self.session_name)
        form.addRow('场记备注', self.notes)
        session_layout.addLayout(form)
        row = QHBoxLayout()
        self.save_button = self._button('保存当前场次', self.save_current)
        row.addWidget(self.save_button)
        self.load_button = self._button('载入所选场次', self.load_selected)
        row.addWidget(self.load_button)
        session_layout.addLayout(row)
        self.pages.addTab(session_page, '场次')

        script_page = QWidget()
        script_layout = QVBoxLayout(script_page)
        row = QHBoxLayout()
        row.addWidget(QLabel('台本内容核查'))
        row.addStretch()
        row.addWidget(self._button('清除台词筛选', self.clear_script_filter))
        script_layout.addLayout(row)
        self.script = self._table(['幕场', '人物', '台词'])
        self.script.itemSelectionChanged.connect(self.script_selected)
        script_layout.addWidget(self.script)
        self.pages.addTab(script_page, '台本')

        dialogue_page = QWidget()
        dialogue_layout = QVBoxLayout(dialogue_page)
        row = QHBoxLayout()
        row.addWidget(QLabel('录制场次'))
        self.dialogue_session = QComboBox()
        self.dialogue_session.currentIndexChanged.connect(self.dialogue_session_changed)
        row.addWidget(self.dialogue_session, 1)
        self.media = QComboBox()
        self.media.setEditable(True)
        self.media.setMinimumWidth(240)
        row.addWidget(self.media, 3)
        dialogue_layout.addLayout(row)
        row = QHBoxLayout()
        self.model = QComboBox()
        self.model.addItems(['small', 'medium'])
        row.addWidget(QLabel('识别模型'))
        row.addWidget(self.model)
        self.language = QComboBox()
        self.language.addItem('自动／中英混合', 'auto')
        self.language.addItem('中文', 'zh')
        self.language.addItem('英文', 'en')
        row.addWidget(self.language)
        row.addStretch()
        dialogue_layout.addLayout(row)
        self.transcripts = self._table(['场次', '来源媒体', '对白数', '更新'])
        self.transcripts.setMaximumHeight(130)
        dialogue_layout.addWidget(self.transcripts)
        filters = QHBoxLayout()
        self.keyword = QLineEdit()
        self.keyword.setPlaceholderText('搜索台词、场次、备注或标签')
        self.keyword.textChanged.connect(self.filter_candidates)
        self.session_filter = QComboBox()
        self.session_filter.currentIndexChanged.connect(self.filter_candidates)
        self.status_filter = QComboBox()
        self.status_filter.addItem('全部状态', '')
        for key, value in STATUS_NAMES.items():
            self.status_filter.addItem(value, key)
        self.status_filter.currentIndexChanged.connect(self.filter_candidates)
        self.script_filter_label = QLabel('全部台词')
        filters.addWidget(self.keyword, 2)
        filters.addWidget(self.session_filter, 1)
        filters.addWidget(self.status_filter)
        filters.addWidget(self.script_filter_label)
        filters.addWidget(self._button('全部台词', self.clear_script_filter))
        dialogue_layout.addLayout(filters)
        self.candidates = self._table(['场次', '台本／现场台词', '媒体位置', '匹配度', '状态', '备注', '标签'])
        self.candidates.itemSelectionChanged.connect(self.candidate_selected)
        dialogue_layout.addWidget(self.candidates, 1)
        row = QHBoxLayout()
        self.candidate_note = QLineEdit()
        self.candidate_note.setPlaceholderText('归类备注')
        self.candidate_tags = QLineEdit()
        self.candidate_tags.setPlaceholderText('标签，逗号分隔')
        row.addWidget(self.candidate_note, 2)
        row.addWidget(self.candidate_tags, 1)
        row.addWidget(self._button('保存备注／标签', lambda: self.update_candidate()))
        dialogue_layout.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(self._button('确认匹配', lambda: self.update_candidate('confirmed')))
        row.addWidget(self._button('排除匹配', lambda: self.update_candidate('rejected')))
        row.addWidget(self._button('恢复待核实', lambda: self.update_candidate('candidate')))
        row.addStretch()
        row.addWidget(self._button('预览画面／声音', self.preview_candidate))
        dialogue_layout.addLayout(row)
        self.pages.addTab(dialogue_page, '对白归类')

        bottom = QHBoxLayout()
        self.status = QLabel('导入场次素材，完成机位与声音准备后打开PGM时间轴。')
        self.status.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(160)
        self.progress.hide()
        self.cancel = QPushButton('取消')
        self.cancel.clicked.connect(self.cancel_task)
        self.cancel.hide()
        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.progress)
        bottom.addWidget(self.cancel)
        layout.addLayout(bottom)
        self._script_filter = None
        self._advanced_script_filter = None
    def show_advanced(self, page=0):
        self.pages.setCurrentIndex(page)
        self.advanced.show()
        if page == 2:
            self.filter_candidates()
        self.advanced.raise_()
        self.advanced.activateWindow()

    def set_workflow(self, workflow):
        self.workflow = workflow

    def ensure_workflow_context(self, progress=lambda *args: None, cancelled=lambda: False):
        if cancelled():
            raise ValueError('已取消。')
        from local_project import LocalProjectClient
        client = self.client or LocalProjectClient()
        production_id = getattr(self, '_workflow_production_snapshot', None)
        productions = client.call('list_productions')
        production = next((row for row in productions if row['id'] == production_id), None)
        production = production or (productions[0] if productions else None)
        if production is None:
            raise ValueError('请先命名新项目或打开已有项目。')
        self._workflow_resolved_production = production['id'], production['name']
        progress(0, '准备当前项目素材…')
        return client, production['id']

    def apply_workflow_context(self, client, production_id, production_name='默认剧目'):
        self.client = client
        self.url.clear()
        self.token.clear()
        self.actor.setText('本机')
        self.production.blockSignals(True)
        resolved = getattr(self, '_workflow_resolved_production', None)
        if resolved and resolved[0] == production_id:
            production_name = resolved[1]
        index = self.production.findData(production_id)
        if index < 0:
            self.production.addItem(production_name, production_id)
            index = self.production.count() - 1
        self.production.setCurrentIndex(index)
        self.production.blockSignals(False)
        self.catalogue_label.setText(self.production.currentText())

    def show_workflow_failures(self, failures):
        previous = getattr(self, '_workflow_failure_dialog', None)
        if previous:
            previous.close()
        dialog = QDialog(self)
        dialog.setWindowTitle('未完成识别的 PGM')
        dialog.resize(760, 420)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        layout = QVBoxLayout(dialog)
        details = QPlainTextEdit()
        details.setReadOnly(True)
        details.setPlainText('\n\n'.join(f'{row.get("media_path", "")}\n{row.get("error", "")}' for row in failures))
        layout.addWidget(details)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(dialog.close)
        layout.addWidget(close)
        self._workflow_failure_dialog = dialog
        dialog.destroyed.connect(lambda: setattr(self, '_workflow_failure_dialog', None)
                                 if getattr(self, '_workflow_failure_dialog', None) is dialog else None)
        dialog.show()

    def _workflow(self):
        if self.workflow is None:
            import library_workflow
            self.workflow = library_workflow
        return self.workflow

    def ingest_materials(self):
        return self.open_material_import()

    def open_material_import(self, session_id=None):
        self.set_workflow_stage('materials')
        selected = session_id or (self._session or {}).get('id')
        if session_id and not any(row['id'] == session_id for row in self._sessions):
            raise ValueError('当前拍摄场次尚未载入素材库，请先刷新项目素材。')
        dialog = MaterialImportDialog(self._sessions, selected, self, append=bool(session_id))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            return self._delegate_ingest_materials(**dialog.materials())

    def materials_ingested(self, session):
        self.material_session_changed.emit(copy.deepcopy(session))

    def _delegate_ingest_materials(self, pgm_paths, batch_name, proxies, audio_paths,
                                   session_id=None, expected_revision=None, cut_xml_paths=None,
                                   recording_day=None, recording_kind=None, camera_count=None):
        kwargs = dict(session_id=session_id, expected_revision=expected_revision)
        if cut_xml_paths is not None:
            kwargs['cut_xml_paths'] = cut_xml_paths
        if recording_day is not None:
            kwargs['recording_day'] = recording_day
        if recording_kind is not None:
            kwargs['recording_kind'] = recording_kind
        if camera_count is not None:
            kwargs['camera_count'] = camera_count
        return self._workflow().ingest_materials(self, pgm_paths, batch_name, proxies, audio_paths, **kwargs)

    def export_correspondence(self):
        choice, ok = QInputDialog.getItem(self, '导出跨场对应表', '匹配范围',
                                        ['仅已确认', '已确认及待核实'], 0, False)
        if not ok:
            return
        path, _ = QFileDialog.getSaveFileName(self, '导出跨场对应表',
            str(Path.home() / 'Desktop' / '跨场台词对应表.csv'), 'CSV (*.csv)')
        if not path:
            return
        include_candidates = choice == '已确认及待核实'
        def operation(progress, cancelled):
            client, production_id = self.ensure_workflow_context(progress, cancelled)
            data = self._read_all(client, production_id)
            if cancelled():
                raise ValueError('已取消。')
            from library_correspondence import export_correspondence
            result = export_correspondence(data, path, include_candidates=include_candidates,
                                           progress=progress, cancelled=cancelled)
            return client, production_id, data, result
        def success(result):
            client, production_id, data, exported = result
            self.apply_workflow_context(client, production_id)
            self._set_data(data)
            self.status.setText('跨场对应表已导出（CSV + JSON）：' + str(Path(path).parent))
            self.correspondence_last_result = exported
        return self._run('导出跨场对应表…', operation, success)

    def ingest_pgm(self):
        paths, _ = QFileDialog.getOpenFileNames(self, '导入全部 PGM（可多选）', '',
                                              '视频 (*.mov *.mp4 *.mxf *.m4v);;所有文件 (*)')
        if not paths:
            return
        batch, ok = QInputDialog.getText(self, '拍摄场次', '批次名称',
                                        text=datetime.now().strftime('Day_%Y%m%d'))
        if ok and batch.strip():
            self._delegate_ingest_pgm(paths, batch.strip())

    def _delegate_ingest_pgm(self, paths, batch_name):
        return self._workflow().ingest_pgm(self, paths, batch_name)

    def ingest_script(self):
        self.set_workflow_stage('script')
        path, _ = QFileDialog.getOpenFileName(self, '导入台本', '', '台本 (*.txt *.docx *.pdf)')
        if path:
            return self._workflow().import_script(self, path)

    def sync_recordings(self):
        self.set_workflow_stage('sound')
        # The host saves the selected recording's live settings before doing
        # work.  A standalone catalogue still has a useful direct fallback.
        if self.receivers(self.workflow_stage_requested):
            self.workflow_stage_requested.emit('sync')
            return
        return self._workflow().sync_recording(self, session_id=(self._session or {}).get('id'))

    def recognize_and_match(self):
        self.set_workflow_stage('script')
        if self.receivers(self.workflow_stage_requested):
            self.workflow_stage_requested.emit('match')
            return
        return self._workflow().recognize_and_match(self, session_id=(self._session or {}).get('id'),
                                                   require_synced=True, require_prepared=True)

    def apply_reference_state(self, state, refresh_matches=True):
        """Apply server-committed selection; UI choices never bypass revision checks."""
        previous_reference = self.reference_session_id
        self.reference_state = copy.deepcopy(state or {})
        self.reference_session_id = self.reference_state.get('reference_session_id') or None
        if hasattr(self, '_pgm_match_versions'):
            self._pgm_match_versions = [dict(row, reference_revision=self.reference_state.get('revision', 0))
                                        for row in self._pgm_match_versions]
        if previous_reference != self.reference_session_id:
            self._baseline_map_cut = None
        pairs = self.reference_state.get('selected_clip_pairs') or {}
        self.selected_clip_pairs = dict(pairs) if isinstance(pairs, dict) else {}
        self._refresh_reference_controls()
        manual_mode = bool(self.reference_state.get('manual_timeline', {}).get('version'))
        if refresh_matches and not manual_mode and hasattr(self, 'story_timeline'):
            self._refresh_coverage_axis()
        if not manual_mode and refresh_matches and hasattr(self, 'match_list') and not getattr(self, '_ruler_selection_active', False):
            self.filter_candidates()

    def _take_index(self):
        signature = (id(self._sessions), id(self._scripts), id(self._transcripts), id(self._candidates))
        if getattr(self, '_take_index_signature', None) != signature:
            from pgm_takes import PgmTakeIndex
            self._pgm_take_index = PgmTakeIndex(dict(sessions=self._sessions, scripts=self._scripts,
                transcripts=self._transcripts, candidates=self._candidates))
            self._take_index_signature = signature
        return self._pgm_take_index

    def _automatic_export_pairs(self):
        """Green track membership is the export selection; saved pairs only name anchors."""
        signature = (id(self._sessions), id(self._scripts), id(self._transcripts), id(self._candidates),
                     self.reference_session_id, id(self.selected_clip_pairs))
        if getattr(self, '_automatic_export_signature', None) != signature:
            index = self._take_index()
            self._automatic_export_candidates = index.export_pairs(
                self.reference_session_id, self.selected_clip_pairs)
            self._automatic_export_keys = {index.key(index.candidates[identity]): identity
                                           for identity in self._automatic_export_candidates}
            self._automatic_export_signature = signature
        return self._automatic_export_candidates

    def _selected_take(self, clip):
        """Return the green whole-file representative, never a historical checkbox choice."""
        self._automatic_export_pairs()
        return self._automatic_export_keys.get(self._take_index().key(clip))

    def _refresh_reference_controls(self):
        if not hasattr(self, 'reference_label'):
            return
        available = not self.is_busy() and not self.external_busy
        self._refresh_pool_delete_button()
        session = next((row for row in self._sessions if row['id'] == self.reference_session_id), None)
        label = '基准：' + session['name'] if session else '尚未选择基准通条'
        layout = self.reference_state.get('manual_timeline') or {}
        count = sum(item.get('start_seconds') is not None and not item.get('hidden')
                    and item.get('session_id') != self.reference_session_id for item in layout.get('items', {}).values())
        label += f' · 已放置备选 {count} 条PGM' if layout.get('version') else ' · 尚未保存手动编排'
        pending = sum(item.get('start_seconds') is not None and not item.get('hidden')
                      and item.get('session_id') != self.reference_session_id and not item.get('alignment_reviewed', False)
                      for item in layout.get('items', {}).values())
        if pending:label += f' · 蓝色对齐待核实 {pending} 条（可导出）'
        self.reference_label.setText(label)
        self.reference_label.setToolTip(label)
        self.choose_reference_button.setEnabled(available and any(
            row.get('config', {}).get('library_record_type') != 'notes' for row in self._sessions))
        self.export_reference_button.setEnabled(available and session is not None)
        self.export_baseline_button.setEnabled(available and session is not None)
        self.export_alternatives_button.setEnabled(available and session is not None)
        self.export_reference_button.setToolTip('按已保存的手动位置导出；保留整条PGM、多机位、源时码及原有声音引用。')
        self.baseline_map_button.setEnabled(available and session is not None)
        self._refresh_baseline_map_status()
        self.export_baseline_button.setToolTip('仅输出基准通条多机位时间线，不加入备选素材。')
        if hasattr(self, 'select_take_button'):
            self._refresh_take_button()
        if not layout.get('version'):
            self._update_browser_summary()
        elif hasattr(self, 'browser_reference_summary'):
            self.browser_reference_summary.setText(label)
        if not layout.get('version') and hasattr(self, 'workspace_inspector') and self._workspace_selection == 'track':
            clip = getattr(self, '_story_clip_by_id', {}).get(self._selected_story_take_id)
            if clip:
                self._show_workspace_clip(clip, 'track')

    def _refresh_take_button(self):
        index = self.match_list.currentRow()
        clip = self._visible_candidates[index] if 0 <= index < len(self._visible_candidates) else None
        baseline = bool(clip and self.reference_session_id and clip.get('session_id') == self.reference_session_id)
        baseline_ready = baseline and self._take_index().association_ready(clip)
        self.select_take_button.setText('基准已定位' if baseline_ready else '确认基准位置')
        self.select_take_button.setVisible(baseline)
        self.select_take_button.setEnabled(not self.is_busy() and not self.external_busy and baseline and not baseline_ready)
        self.select_take_button.setToolTip('仅核对基准台词的位置；绿色备选自动参与导出。')
        if hasattr(self, 'remove_match_button'):
            self.remove_match_button.setEnabled(bool(clip) and not self.is_busy() and not self.external_busy)
        if hasattr(self, 'coverage_range_input'):
            has_source = (self.pending_list.currentItem() is not None
                          if getattr(self, '_workspace_selection', 'track') == 'library' else bool(clip))
            ready = has_source and not self.is_busy() and not self.external_busy
            self.coverage_range_input.setEnabled(ready)
            self.coverage_apply_button.setEnabled(ready)

    def confirm_current_baseline(self):
        if self.is_busy() or self.external_busy:
            raise ValueError('请等待当前任务完成。')
        index = self.match_list.currentRow()
        clip = self._visible_candidates[index] if 0 <= index < len(self._visible_candidates) else None
        if not clip or not self.reference_session_id or clip.get('session_id') != self.reference_session_id:
            return
        if self._take_index().association_ready(clip):
            return
        self.clip_selection_changed.emit({'production_id': self.production_id(),
            'reference_session_id': self.reference_session_id, 'candidate_id': clip['id'],
            'checked': True, 'confirm': True, 'baseline_candidate_id': None,
            'expected_revision': self.reference_state.get('revision', 0)})

    def choose_reference(self):
        self.set_workflow_stage('reference')
        sessions = [row for row in self._sessions
                    if row.get('config', {}).get('library_record_type') != 'notes'
                    and row.get('config', {}).get('recording_kind') not in ('coverage', '分镜头', '分镜')]
        if not sessions:
            raise ValueError('请先登记通条拍摄场次。')
        labels = [row['name'] + ('（确认通条）' if row.get('config', {}).get('recording_kind') not in ('continuous', '通条') else '')
                  + (f' · 场次 {index + 1}' if sum(value['name'] == row['name'] for value in sessions) > 1 else '')
                  for index, row in enumerate(sessions)]
        current = next((index for index, row in enumerate(sessions)
                        if row['id'] == self.reference_session_id), 0)
        choice, ok = QInputDialog.getItem(self, '选择基准通条', '基准时间线来自哪次通条拍摄？',
                                         labels, current, False)
        if not ok or choice not in labels:
            return
        session = sessions[labels.index(choice)]
        kind = session.get('config', {}).get('recording_kind')
        legacy = kind not in ('continuous', '通条')
        if legacy and QMessageBox.question(self, '确认通条',
            f'“{session["name"]}”的拍摄类型为“{kind or "未登记"}”。确认它是完整通条？\n将保留原场次名称、类型和身份。',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self.reference_changed.emit({'production_id': self.production_id(),
            'reference_session_id': session['id'], 'legacy_continuous_confirmed': legacy})

    def _clip_notes(self, clip):
        signature = (id(self._sessions), id(self._field_notes), id(self._candidates))
        if getattr(self, '_notes_signature', None) != signature:
            self._notes_signature, self._notes_cache = signature, {}
        identity = (clip.get('id'), clip.get('note', ''), clip.get('revision'))
        if identity in self._notes_cache:
            return self._notes_cache[identity]
        texts = [clip.get('note', '')]
        session = next((row for row in self._sessions if row['id'] == clip.get('session_id')), {})
        texts.append(session.get('config', {}).get('library_notes', ''))
        from dialogue_engine import _note_scope
        for note in self._field_notes:
            if _note_scope(note, clip):
                texts.append(' · '.join(str(note.get(key) or '') for key in ('scene', 'take', 'notes')
                                        if note.get(key)) or note.get('rawtext', ''))
        result = '\n'.join(dict.fromkeys(str(text).strip() for text in texts if str(text).strip()))
        self._notes_cache[identity] = result
        return result

    def open_manual_timeline(self):
        if self.is_busy() or self.external_busy:
            raise ValueError('请等待当前任务完成。')
        self.require_production()
        return self._reference_request('prepare_manual_timeline')

    def _reference_request(self, operation, **kwargs):
        request = dict(production_id=self.production_id(), reference_session_id=self.reference_session_id,
                       expected_revision=self.reference_state.get('revision', 0), operation=operation, **kwargs)
        if self.receivers(self.manual_timeline_requested):
            self.manual_timeline_requested.emit(copy.deepcopy(request))
            return
        def task(progress, cancelled):
            from reference_workflow import ReferenceWorkflow
            client, production = self.ensure_workflow_context(progress, cancelled)
            if production != request['production_id'] or cancelled():
                raise ValueError('项目已改变或操作已取消。')
            workflow = ReferenceWorkflow(client, production, getattr(client, 'actor', 'local'))
            if operation == 'save_manual_timeline':
                return workflow.save_manual_timeline(request['layout'], expected_revision=request['expected_revision'])
            if operation in ('prepare_automatic_alignment','prepare_automatic_map','save_automatic_map'):
                options=dict(expected_revision=request['expected_revision'],progress=progress,cancelled=cancelled,
                             model=request.get('model','small'),replace_automatic=request.get('replace_automatic',False))
                if operation=='save_automatic_map':return workflow.save_automatic_map(request['mapping'],layout=request['layout'],**options)
                if request.get('rebuild'):options['rebuild']=True
                return getattr(workflow,operation)(request['layout'],**options)
            return workflow.prepare_manual_timeline()
        def success(result):
            from project_ui import _after_idle
            callback = (lambda: self.manual_timeline_saved(result, after_export=request.get('after_export'))) if operation=='save_manual_timeline' else \
                (lambda:self.present_automatic_map(result)) if operation=='prepare_automatic_map' else \
                (lambda:self.present_automatic_alignment(result)) if operation in ('prepare_automatic_alignment','save_automatic_map') else \
                (lambda: self.present_manual_timeline(result))
            _after_idle(self, callback)
        return self._run('建立基准台本映射与自动初排…' if operation.startswith('prepare_automatic') else
                         '保存基准台本映射…' if operation=='save_automatic_map' else
                         '保存手动编排…' if operation == 'save_manual_timeline' else '读取PGM编排素材…', task, success)

    def present_manual_timeline(self, result):
        from manual_timeline_ui import ManualTimelineDialog
        from project_ui import _after_idle
        state = result.get('state') or result.get('data', {}).get('reference_state') or self.reference_state
        self.apply_reference_state(state, refresh_matches=False)
        cached = dict(self._manual_prepared or {})
        cached.update({key: result[key] for key in ('state', 'manual_inventory', 'manual_timeline', 'manual_cut', 'warnings') if key in result})
        self._manual_prepared = cached
        dialog = ManualTimelineDialog(result['data'], result['manual_inventory'], result['manual_timeline'],
                                      result.get('manual_cut'), self, warnings=result.get('warnings'))
        self._manual_dialog=dialog
        dialog.automatic_alignment_requested.connect(self._manual_automatic_requested)
        dialog.automatic_alignment_cancelled.connect(self.cancel_task)
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            layout = dialog.layout_result() if accepted else None
            after_export = dialog.export_mode if accepted else None
        finally:
            self._manual_dialog=None
            dialog.deleteLater()
        if layout is None:
            return
        _after_idle(self, lambda: self._reference_request('save_manual_timeline', layout=layout, after_export=after_export))

    def _active_manual_dialog(self):
        dialog=getattr(self,'_manual_dialog',None)
        return dialog if dialog is not None and not sip.isdeleted(dialog) else None

    def _manual_automatic_requested(self,payload):
        dialog=self._active_manual_dialog()
        if dialog is None:return
        if self.is_busy() or self.external_busy:
            dialog.automatic_failed('请等待当前任务结束。');return
        request=copy.deepcopy(payload);operation=request.pop('operation','prepare_automatic_alignment')
        try:return self._reference_request(operation,**request)
        except Exception as exc:
            dialog.automatic_failed(str(exc));raise

    def present_automatic_alignment(self,result):
        state=result.get('state') or result.get('data',{}).get('reference_state')
        if state:self.apply_reference_state(state,refresh_matches=False)
        cached=dict(self._manual_prepared or {})
        cached.update({key:result[key] for key in ('state','manual_inventory','manual_timeline','manual_cut','warnings','alignment_report') if key in result})
        self._manual_prepared=cached
        dialog=self._active_manual_dialog()
        if dialog is not None:dialog.apply_automatic_alignment(result)
        self.status.setText('映射已保存，现有位置未改变；可勾选“重排未核实自动片段”后再次初排。' if result.get('alignment_report',{}).get('mapping_only') else
                           '基准台本映射与对齐结果已更新；蓝色位置待核实，仍可导出。')

    def present_automatic_map(self,result):
        from baseline_map_ui import BaselineMapDialog
        from project_ui import _after_idle
        self.present_automatic_alignment(result)
        editor=self._active_manual_dialog()
        if editor is None:return
        mapping=result.get('baseline_script_map') or result.get('automatic_alignment',{}).get('mapping')
        cut=result.get('baseline_cut') or result.get('manual_cut')
        if not mapping or not cut:
            editor.show_error('尚无可核对的基准映射，请先建立映射。');return
        editor.preview.pause()
        dialog=BaselineMapDialog(result.get('data') or editor.data,mapping,cut,editor)
        accepted=dialog.exec()==QDialog.DialogCode.Accepted
        rebuild=dialog.rebuild_requested if accepted else False
        revised=dialog.mapping_result() if accepted and not rebuild else None
        dialog.deleteLater()
        if not accepted:return
        options=dict(layout=copy.deepcopy(editor.layout_data),model=editor.alignment_model.currentData(),
                     replace_automatic=editor.replace_automatic.isChecked())
        if rebuild:
            options['rebuild']=True;operation='prepare_automatic_map'
        elif revised is not None:
            options['mapping']=revised;operation='save_automatic_map'
        else:return
        editor.set_automatic_busy(True,'正在保存与核对基准映射…')
        _after_idle(self,lambda:self._reference_request(operation,**options))

    def manual_timeline_saved(self, result, after_export=None):
        from project_ui import _after_idle
        self.apply_reference_state(result.get('state') or result.get('data', {}).get('reference_state'), refresh_matches=False)
        cached = dict(self._manual_prepared or {})
        cached.update({key: result[key] for key in ('state', 'manual_inventory', 'manual_timeline', 'manual_cut', 'warnings') if key in result})
        self._manual_prepared = cached
        self.status.setText('台本区间整理与基准秒数对齐已保存；源素材、时码和声音同步保持原样。')
        if after_export:
            include_base = after_export in ('baseline', 'combined')
            include_alts = after_export in ('alternatives', 'combined')
            _after_idle(self, lambda: self.export_reference(include_alts, include_base, manual=True))

    def _export_manual_reference(self, include_alternatives=True, include_baseline=True):
        self.set_workflow_stage('reference')
        if not self.reference_session_id:
            raise ValueError('请先选择基准通条。')
        layout = self.reference_state.get('manual_timeline') or {}
        placed = {identity: item for identity, item in layout.get('items', {}).items()
                  if item.get('session_id') != self.reference_session_id and item.get('start_seconds') is not None
                  and not item.get('hidden')}
        if include_alternatives and not include_baseline and not placed:
            raise ValueError('请先在手动时间轴放置并保存备选片段。')
        if include_alternatives and not layout.get('version'):
            self.status.setText('请先保存手动编排，再导出备选XML。')
            return self.open_manual_timeline()
        output = QFileDialog.getExistingDirectory(self, '选择 XML 输出文件夹', str(Path.home() / 'Desktop'))
        if output:
            self.reference_export_requested.emit(dict(production_id=self.production_id(),
                reference_session_id=self.reference_session_id, expected_revision=self.reference_state.get('revision', 0),
                output_dir=output, include_alternatives=bool(include_alternatives), include_baseline=bool(include_baseline),
                manual=True, resolved_pairs=None, exported_take_pairs={}))

    def _refresh_baseline_map_status(self):
        if not hasattr(self, 'baseline_map_status'):
            return
        mapping = self.reference_state.get('baseline_script_map') or {}
        if not mapping or mapping.get('reference_session_id') != self.reference_session_id:
            self.baseline_map_status.setText('基准台本映射尚未建立；选择基准后，用已识别台词建立连续映射。')
            return
        segments = mapping.get('segments') or []
        pending = sum(bool(row.get('needs_review', True)) for row in segments)
        units = mapping.get('requested_script_units') or mapping.get('script_units') or []
        sizes = [len(row.get('line_ids') or []) for row in units if row.get('line_ids')]
        label = f'基准台本映射：{len(segments)} 个区间 · {pending} 个待核实'
        if sizes:
            label += f' · 最小备选单元 {min(sizes)} 行台本'
        if mapping.get('refinement_needed') or mapping.get('units_changed'):
            label += ' · 新备选范围需细化，可打开映射核实'
        self.baseline_map_status.setText(label)

    def _baseline_mapping_ready(self, pairs):
        """Fast snapshot guard; source digest/fingerprint are rechecked by export."""
        from baseline_script_map import build_line_index
        mapping = self.reference_state.get('baseline_script_map') or {}
        if (mapping.get('reference_session_id') != self.reference_session_id or
                not mapping.get('segments') or not mapping.get('evidence_fingerprint')):
            return False
        # Full evidence/index hashing runs in the background prepare/save/export
        # workflow. The GUI checks only the saved partition and needed onsets.
        from baseline_map_ui import script_nodes, validate_partition
        try:
            validate_partition(mapping, self._timeline_nodes or script_nodes({'scripts': self._scripts}))
        except (ValueError, TypeError, KeyError):
            return False
        lookup = build_line_index(mapping)
        index = self._take_index()
        for identity in pairs:
            candidate = index.candidates.get(identity)
            if candidate is None:
                return False
            candidate = index.selection_candidate(candidate)
            identity = candidate.get('script_line_id')
            row = lookup.get(identity)
            if (row is None or row.get('needs_review', True) or row.get('anchor_frame') is None
                    or not row.get('line_ids') or row['line_ids'][0] != identity):
                return False
        return True

    def open_baseline_mapping(self, rebuild=False):
        if self.is_busy() or self.external_busy:
            raise ValueError('请等待当前任务完成。')
        if not self.reference_session_id:
            raise ValueError('请先选择基准通条。')
        self.set_workflow_stage('reference')
        request = dict(production_id=self.production_id(), reference_session_id=self.reference_session_id,
                       expected_revision=self.reference_state.get('revision', 0),
                       operation='prepare_baseline_map', rebuild=bool(rebuild))
        return self._request_baseline_map(request)

    def _request_baseline_map(self, request):
        if self.receivers(self.baseline_map_requested):
            self.baseline_map_requested.emit(copy.deepcopy(request))
            return
        # Standalone panels use the same workflow and CAS guards as the host.
        def operation(progress, cancelled):
            from reference_workflow import ReferenceWorkflow
            client, production = self.ensure_workflow_context(progress, cancelled)
            if production != request['production_id'] or cancelled():
                raise ValueError('项目已改变或操作已取消。')
            workflow = ReferenceWorkflow(client, production, getattr(client, 'actor', 'local'))
            if request['operation'] == 'save_baseline_map':
                return workflow.save_baseline_map(request['mapping'], request.get('expected_revision'))
            return workflow.prepare_baseline_map(rebuild=request.get('rebuild', False))
        def success(result):
            self.apply_reference_state(result['state'])
            self._baseline_map_cut = copy.deepcopy(result.get('baseline_cut'))
            if request['operation'] == 'save_baseline_map':
                self.status.setText('基准台本映射已保存。')
            else:
                from project_ui import _after_idle
                _after_idle(self, lambda: self.present_baseline_mapping(result))
        return self._run('保存基准映射…' if request['operation'] == 'save_baseline_map' else
                         '使用已有台词建立基准映射…', operation, success)

    def present_baseline_mapping(self, result):
        from baseline_map_ui import BaselineMapDialog
        from project_ui import _after_idle
        state = result.get('state') or result.get('data', {}).get('reference_state') or self.reference_state
        self.apply_reference_state(state)
        self._baseline_map_cut = copy.deepcopy(result['baseline_cut'])
        dialog = BaselineMapDialog(result['data'], result['baseline_script_map'], result['baseline_cut'], self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if dialog.rebuild_requested:
            _after_idle(self, lambda: self.open_baseline_mapping(rebuild=True))
            return
        mapping = dialog.mapping_result()
        if mapping is None:
            return
        request = dict(production_id=self.production_id(), reference_session_id=self.reference_session_id,
                       expected_revision=state.get('revision', 0), operation='save_baseline_map', mapping=mapping)
        _after_idle(self, lambda: self._request_baseline_map(request))

    def _baseline_candidates(self, clip):
        node = next((value for value in self._timeline_nodes
                     if value['id'] == clip.get('script_line_id')), None)
        return [row for row in (node or {}).get('clips', [])
                if row.get('session_id') == self.reference_session_id and self._take_index().association_ready(row)]

    def _choose_baseline_candidate(self, clip):
        choices = self._baseline_candidates(clip)
        if not choices:
            raise ValueError('基准通条中没有这句台词的已确认片段，请先完成台词核对。')
        if len(choices) == 1:
            return choices[0]['id']
        choices.sort(key=lambda row: (row.get('media_path', ''), float(row.get('start_seconds', 0))))
        labels = [f'{index + 1}. {row.get("session_name", "")} · {Path(row.get("media_path", "")).name} · '
                  f'{format_time(row.get("start_seconds"))} — {format_time(row.get("end_seconds"))}'
                  + (' · ' + self._clip_notes(row).replace('\n', ' ')[:120] if self._clip_notes(row) else '')
                  for index, row in enumerate(choices)]
        selected, ok = QInputDialog.getItem(self, '选择基准片段',
            '将这段备选对齐到基准中的哪一次台词？（按台词起点，保留原速）', labels, 0, False)
        return choices[labels.index(selected)]['id'] if ok and selected in labels else None

    def export_reference(self, include_alternatives=True, include_baseline=True, *, manual=False):
        if manual:
            return self._export_manual_reference(include_alternatives, include_baseline)
        self.set_workflow_stage('reference')
        if not self.reference_session_id:
            raise ValueError('请先选择基准通条。')
        pairs = self._automatic_export_pairs() if include_alternatives else {}
        if not include_baseline and not pairs:
            raise ValueError('轨道上没有可导出的绿色已核实备选片段。')
        if pairs and not self._baseline_mapping_ready(pairs):
            self.status.setText('请先完成基准台本映射并核实相关区间，再导出基准＋备选 XML。')
            self.open_baseline_mapping()
            return
        required = {self.reference_session_id}
        if include_alternatives:
            required.update(row.get('session_id') for row in self._candidates
                            if row.get('id') in pairs)
        blocked = [(session, state) for session, state in self._readiness_rows(required)
                   if state.get('ready') is not True]
        if blocked:
            session, state = blocked[0]
            row = next((index for index, (value, _status) in enumerate(self._readiness_rows())
                        if value['id'] == session['id']), -1)
            if row >= 0:
                self.reference_readiness.selectRow(row)
            if not self.receivers(self.workflow_stage_requested):
                raise ValueError('导出前请完成场次准备：\n' + self._preparation_detail(session, state) +
                                 '\n可双击准备状态表中的场次，或点击“机位与同步”前往处理。')
            # Host export saves the current recording first, then performs the
            # strict preparation gate against fresh shared data.  An old
            # overview must not prevent that save from happening.
            self.status.setText('导出前将保存当前场次，并严格核对所有相关场次的机位、校准和声音。')
        output = QFileDialog.getExistingDirectory(self, '选择 XML 输出文件夹', str(Path.home() / 'Desktop'))
        if output:
            self.reference_export_requested.emit({'production_id': self.production_id(),
                'reference_session_id': self.reference_session_id,
                'exported_take_pairs': copy.deepcopy(pairs),
                'output_dir': output, 'include_alternatives': bool(include_alternatives),
                'export_snapshot': self._take_index().export_signature(self.reference_session_id),
                'include_baseline': bool(include_baseline), 'resolved_pairs': None,
                'expected_revision': self.reference_state.get('revision', 0)})

    def render_script_tree(self):
        if not hasattr(self, 'script_tree'):
            return
        from library_timeline import build_timeline_data
        if self._prepared_timeline_nodes is not None:
            self._timeline_nodes = self._prepared_timeline_nodes
        else:
            self._timeline_nodes = build_timeline_data(dict(sessions=self._sessions,
                scripts=self._scripts, transcripts=self._transcripts, candidates=self._candidates,
                reference_state=self.reference_state), whole_pgm=True)
        self._timeline_by_id = {row['id']:row for row in self._timeline_nodes}
        self.render_pgm_intervals()
        blocked = self.script_timeline.blockSignals(True)
        try:
            self.script_timeline.set_nodes(self._timeline_nodes, selected_id=self._script_filter)
        finally:
            self.script_timeline.blockSignals(blocked)
        self._refresh_coverage_axis()
        # A redraw can retain the same Qt selection without emitting a signal.
        self._script_filter = self.script_timeline.current_line_id() or None
        self.filter_script_tree()
        shoot_count = sum(row.get('config', {}).get('library_record_type') != 'notes'
                          for row in self._sessions)
        self._update_axis_count(shoot_count)
        self.catalogue_label.setText(self.production.currentText())
        if getattr(self, '_ruler_selection_active', False):
            self._highlight_script_coverage(self.story_timeline.canvas.highlighted_line_ids)
        elif not self._selected_story_take_id:
            self.pgm_interval_selected(self.pgm_timeline.currentItem())
        else:
            self.filter_candidates()

    def render_pgm_intervals(self, incremental=False):
        if not hasattr(self, 'pgm_timeline'):
            return
        if self._prepared_pgm_intervals is None:
            from pgm_intervals import build_pgm_intervals
            nodes = build_pgm_intervals(dict(sessions=self._sessions, scripts=self._scripts,
                transcripts=self._transcripts, candidates=self._candidates), script_nodes=self._timeline_nodes)
        else:
            nodes = self._prepared_pgm_intervals
        self._pgm_interval_nodes = nodes
        self._pgm_interval_by_id = {row['id']: row for row in nodes}
        self._pgm_inventory()
        blocked = self.pgm_timeline.blockSignals(True)
        try:
            if incremental:
                self.pgm_timeline.update_nodes(nodes)
            else:
                self.pgm_timeline.set_nodes(nodes)
        finally:
            self.pgm_timeline.blockSignals(blocked)
        self._refresh_coverage_axis()

    def _pgm_inventory(self):
        signature = tuple(id(value) for value in (self._sessions, self._scripts,
            self._transcripts, self._candidates, self._pending_intervals))
        cached = getattr(self, '_pending_catalogue_cache', None)
        if cached is None or cached[0] != signature:
            from pgm_library_view import prepare_pgm_inventory, inventory_status_rows
            index = self._take_index()
            rows, flags = prepare_pgm_inventory(self._sessions, self._transcripts,
                self._candidates, self._pending_intervals, index)
            cached = self._pending_catalogue_cache = signature, rows, flags
            self._pgm_status_rows = inventory_status_rows(rows, flags, index)
        return cached[1], cached[2]

    def pgm_status_text(self, session_id=None):
        from pgm_library_view import inventory_status_text
        self._pgm_inventory()
        return inventory_status_text(self._pgm_status_rows, session_id)

    def show_pgm_match_status(self):
        from pgm_library_view import EDITOR_STATUS_LABELS as STATUS_LABELS
        self._pgm_inventory()
        dialog = QDialog(self)
        dialog.setWindowTitle('PGM 台本匹配清单')
        dialog.resize(940, 560)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(self.pgm_status_text()))
        filters = QHBoxLayout()
        state_filter, search = QComboBox(), QLineEdit()
        state_filter.addItem('全部PGM', '')
        for key, label in STATUS_LABELS.items():
            state_filter.addItem(label, key)
        search.setPlaceholderText('搜索场次／PGM文件名')
        filters.addWidget(state_filter); filters.addWidget(search, 1)
        layout.addLayout(filters)
        table = self._table(['状态', '拍摄场次', 'PGM文件', '转录段数', '覆盖匹配度', '首句定位', '完整路径', '说明'])
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(table, 1)
        rows = getattr(self, '_pgm_status_rows', [])
        def run_reason(row):
            state=row.get('script_run') or {}
            if state.get('status') in ('failed','cancelled') and state.get('error'):
                return '上次台本处理未完成：'+state['error']
            if not row.get('transcript_count'):
                return '尚无台词转录结果；请选择本场并开始台本匹配。'
            return ''
        def refresh():
            chosen = [row for row in rows if (not state_filter.currentData() or row['status'] == state_filter.currentData())
                      and search.text().strip().casefold() in (row['session_name']+' '+row['media_path']).casefold()]
            self._populate(table, [[STATUS_LABELS[row['status']], row['session_name'], Path(row['media_path']).name,
                                  str(row.get('transcript_count',0)),f"{row.get('match_score',0.):.0%}",
                                  '已定位' if row.get('alignment_ready') else '待定位',row['media_path'],
                                  '；'.join(filter(None,[row['reason'],
                                      (row.get('processing') or {}).get('error',''),
                                      run_reason(row),
                                      '' if row.get('alignment_ready') else row.get('alignment_reason','')]))]
                                  for row in chosen])
        state_filter.currentIndexChanged.connect(refresh)
        search.textChanged.connect(refresh)
        refresh()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _update_axis_count(self, shoot_count=None):
        if shoot_count is None:
            shoot_count = sum(row.get('config', {}).get('library_record_type') != 'notes' for row in self._sessions)
        total = len(self._pgm_inventory()[0])
        intervals = ''
        self.script_count.setText(intervals + f'{total} 条完整PGM · {shoot_count} 个拍摄场次\n' + self.pgm_status_text())
        self._update_browser_summary()

    def _update_browser_summary(self):
        if not hasattr(self, 'browser_summary'):
            return
        if not self._browser_dialog:
            if getattr(self, '_manual_summary_sessions', None) is not self._sessions:
                from manual_timeline import inventory
                self._manual_summary_count = len(inventory({'sessions': self._sessions}))
                self._manual_summary_sessions = self._sessions
            total = self._manual_summary_count
            self.browser_summary.setText(f'{total} 条完整PGM · 可手动整理并对齐基准' if total else '尚未导入PGM')
            self.browser_reference_summary.setText(self.reference_label.text())
            return
        total = len(self._pgm_inventory()[0])
        self.browser_summary.setText(f'{total} 条完整PGM\n' + self.pgm_status_text()
                                    if total else '尚未导入PGM')
        self.browser_reference_summary.setText(self.reference_label.text())
        self.browser_title.setText(self.production.currentText()+' · PGM时间轴')
        if self._browser_dialog:
            self._browser_dialog.setWindowTitle(self.production.currentText()+' · PGM时间轴')

    def _refresh_coverage_axis(self):
        if not hasattr(self, 'story_timeline'):
            return
        _rows, flags = self._pgm_inventory()
        pgm_nodes = getattr(self, '_pgm_interval_nodes', [])
        previous = getattr(self, '_coverage_axis_inputs', None)
        reference = self.reference_session_id or ''
        if (previous is None or previous[0] is not self._timeline_nodes or
                previous[1] is not pgm_nodes or previous[2] is not flags or previous[3] != reference):
            index, seen, nodes = self._take_index(), set(), []
            for original in pgm_nodes:
                clips = []
                for row in original.get('clips', []):
                    key = index.key(row)
                    state = flags.get(key, {})
                    if state.get('pool') != 'timeline' or key in seen:
                        continue
                    seen.add(key)
                    clips.append(dict(row, editor_reviewed=state.get('reviewed', False)))
                if clips:
                    nodes.append(dict(original, clips=clips, count=len(clips)))
            self._story_clip_by_id = {row['id']: row for node in nodes for row in node.get('clips', [])}
            self._story_intervals_by_candidate = {row['id']: node['id'] for node in nodes for row in node.get('clips', [])}
            self.story_timeline.set_nodes(self._timeline_nodes, nodes, reference)
            self._coverage_axis_inputs = self._timeline_nodes, pgm_nodes, flags, reference
        if self._selected_story_take_id in self._story_clip_by_id:
            self.story_timeline.select_take(self._selected_story_take_id, ensure_visible=False)
            ids = self._story_clip_by_id[self._selected_story_take_id].get('covered_line_ids', [])
            pos = self.story_timeline.index.positions
            nums = [pos[value] for value in ids if value in pos]
            self._highlight_script_coverage(self.story_timeline.index.line_ids[min(nums):max(nums)+1] if nums else [])
        else:
            self._selected_story_take_id = ''
            self._highlight_script_coverage(self.story_timeline.canvas.highlighted_line_ids)

    def _highlight_script_coverage(self, identities):
        positions = self.story_timeline.index.positions
        complete = [value for value in dict.fromkeys(identities) if value in positions]
        self.script_timeline.set_highlighted_line_ids(complete)

    def _coverage_line_requested(self, line_id, *, activate=True):
        item = self.script_timeline._items.get(line_id)
        if not item:
            return
        blocked = self.script_timeline.blockSignals(True)
        try:
            self.script_timeline.setCurrentItem(item)
            self.script_timeline.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtTop)
        finally:
            self.script_timeline.blockSignals(blocked)
        self._script_filter = self._advanced_script_filter = line_id
        if activate:
            self.script_tree_selected(item)

    def _story_line_selected(self, line_id):
        # Canvas selection emits line before take. Defer list mutation until
        # the exact take identity arrives, so overlapping files stay distinct.
        self._coverage_line_requested(line_id, activate=False)
        if self.story_timeline.canvas.selected_candidate_id:
            return
        self._selected_story_take_id = ''
        self._ruler_selection_active = True
        self._clear_inline()
        if hasattr(self, 'workspace_inspector'):
            self.workspace_inspector.hide()

    def _story_take_selected(self, candidate_id, replay=False):
        clip = self._story_clip_by_id.get(candidate_id)
        if not clip:
            return
        self._workspace_selection = 'track'
        self._ruler_selection_active = False
        self._selecting_story_take = True
        try:
            for combo in (self.take_session_filter, self.take_mode):
                blocked = combo.blockSignals(True)
                combo.setCurrentIndex(0)
                combo.blockSignals(blocked)
            self.main_pages.setCurrentIndex(0)
            self._selected_story_take_id = candidate_id
            identities = clip.get('covered_line_ids') or [clip.get('script_line_id')]
            self._coverage_line_requested(identities[0], activate=False)
            interval_id = self._story_intervals_by_candidate.get(candidate_id)
            blocked = self.pgm_timeline.blockSignals(True)
            self.pgm_timeline.setCurrentItem(self.pgm_timeline._items.get(interval_id))
            self.pgm_timeline.blockSignals(blocked)
            self.filter_candidates()
            for number, row in enumerate(self._visible_candidates):
                if row['id'] == candidate_id:
                    self.match_list.setCurrentRow(number)
                    break
            self.story_timeline.select_take(candidate_id, ensure_visible=False)
            positions = self.story_timeline.index.positions
            numbers = [positions[value] for value in identities if value in positions]
            self._highlight_script_coverage(self.story_timeline.index.line_ids[min(numbers):max(numbers)+1] if numbers else [])
        finally:
            self._selecting_story_take = False
        self._show_workspace_clip(clip, 'track')
        if replay:
            self.preview_current_inline()
        else:
            self.angle_selected()

    def _coverage_interval_clicked(self, interval_id):
        takes = self.story_timeline.index.by_interval.get(interval_id, [])
        chosen = next((take for take in takes if not take.reference), takes[0] if takes else None)
        if chosen:
            self._story_take_selected(chosen.candidate_id)

    def story_axis_changed(self, *_args):
        self.filter_script_tree()

    def filter_script_tree(self, query=None):
        tree = self.script_timeline
        blocked = tree.blockSignals(True)
        tree.filter_nodes('', 'all')
        tree.blockSignals(blocked)
        search = (self.script_search.text() if query is None else query).strip().casefold()
        if not search:
            return
        if search.isdigit():
            number = int(search)-1
            match = self._timeline_nodes[number]['id'] if 0 <= number < len(self._timeline_nodes) else None
        else:
            match = next((row['id'] for row in self._timeline_nodes if search in ' '.join(
                str(row.get(key, '')) for key in ('scene', 'speaker', 'text')).casefold()), None)
        if match:
            self._coverage_line_requested(match)
            self.story_timeline.select_line(match)

    def pgm_interval_selected(self, item, previous=None, *, align_script=True):
        node = self._pgm_interval_by_id.get(self.pgm_timeline.current_line_id(), {})
        clip = next((row for row in node.get('clips', []) if row.get('id') == self._selected_story_take_id),
                    next(iter(node.get('clips', [])), None))
        if clip:
            self._story_take_selected(clip['id'], replay=False)
        elif node:
            self._selected_story_take_id = ''
            self._script_filter = self._advanced_script_filter = None
            self.filter_candidates()
            keys = {tuple(key) for key in node.get('file_keys', [])}
            for number, row in enumerate(getattr(self, '_pending_rows', [])):
                if (row.get('session_id'), row.get('media_path')) in keys:
                    self.main_pages.setCurrentIndex(1)
                    self.pending_list.setCurrentRow(number)
                    break

    def script_tree_selected(self, item, previous=None):
        self._ruler_selection_active = False
        line_id = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        self._script_filter = self._advanced_script_filter = line_id or None
        self._selected_story_take_id = ''
        self.story_timeline.select_line(line_id, ensure_visible=False)
        self._highlight_script_coverage([])
        self.script_filter_label.setText(item.text(0)[:24] if line_id else '全部台词')
        self.filter_candidates()

    def _show_coverage_input(self, clip=None):
        if not hasattr(self, 'coverage_range_input'):
            return
        if clip is None and getattr(self, '_workspace_selection', 'track') == 'library':
            return
        identities = (clip or {}).get('covered_line_ids') or []
        positions = self.story_timeline.index.positions
        numbers = [positions[value]+1 for value in identities if value in positions]
        text = (str(min(numbers)) if min(numbers)==max(numbers) else f'{min(numbers)}-{max(numbers)}') if numbers else ''
        self.coverage_range_input.setText(text)
        ready = bool(clip)
        self.coverage_range_input.setEnabled(ready and not self.is_busy() and not self.external_busy)
        self.coverage_apply_button.setEnabled(ready and not self.is_busy() and not self.external_busy)
        self.coverage_range_input.setToolTip('修改台本覆盖位置，保留实际PGM首句秒数；按回车或点击应用。')

    def apply_coverage_input(self):
        clip = (self._pending() if self._workspace_selection == 'library' else
                self._story_clip_by_id.get(self._selected_story_take_id))
        if not clip:
            raise ValueError('请先在轨道或素材库选择PGM。')
        from manual_script_range import parse_script_range
        identities = parse_script_range(self.coverage_range_input.text(), self.script_timeline._nodes)
        if self._workspace_selection == 'library':
            return self._save_pending_coverage(clip, identities, self._pending_source_position(clip))
        return self._save_story_coverage(clip, identities)

    def _pending_source_position(self, clip, *, view=None):
        from manual_script_range import normalize_source_position
        duration = self._take_index().duration(clip)
        position = clip.get('dialogue_start_seconds', clip.get('start_seconds', 0))
        saved = normalize_source_position(duration, position)
        if self.inline_preview and self.inline_preview.path:
            from library_bridge import resolve_local_path
            if view is None:
                view = self.pending_angle_choice.currentData() or {}
            if view.get('camera', 'PGM') == 'PGM' and self.inline_preview.path == resolve_local_path(clip['media_path']):
                position = self.inline_preview.player.position()/1000
        return normalize_source_position(duration, position, fallback=saved)

    def _workspace_pending_clicked(self):
        if self.pending_list.currentRow() >= 0:
            self._show_workspace_clip(self._pending(), 'library')

    def _show_workspace_clip(self, clip, origin):
        if not hasattr(self, 'workspace_inspector'):
            return
        self._workspace_selection = origin
        self.workspace_angles.setCurrentIndex(1 if origin == 'library' else 0)
        self.workspace_clip_label.setText(Path(clip.get('media_path', '')).name)
        self.workspace_clip_label.setToolTip(clip.get('session_name', '')+'\n'+clip.get('media_path', ''))
        self.workspace_inspector.show()
        self.coverage_apply_button.setText('添加至轨道' if origin == 'library' else '应用区间')
        if origin == 'track':
            self._show_coverage_input(clip)
        elif getattr(self, '_workspace_pending_id', None) != clip['id']:
            self._workspace_pending_id = clip['id']
            self.coverage_range_input.clear()
        available = not self.is_busy() and not self.external_busy
        self.coverage_range_input.setEnabled(available)
        self.coverage_apply_button.setEnabled(available)
        self.workspace_review_button.setEnabled(available and origin == 'track' and not clip.get('editor_reviewed'))
        self.workspace_review_button.setText('区间已核实' if origin == 'track' and clip.get('editor_reviewed') else '确认区间核实')
        self.workspace_review_button.setVisible(origin == 'track')
        self.workspace_hint.setText('预览首句处暂停后输入台本区间（例如55-76或55），添加整条PGM。'
                                    if origin == 'library' else '蓝色待核实，绿色备选自动导出；Delete移至“轨道移出”。')

    def review_workspace_clip(self):
        clip = self._story_clip_by_id.get(self._selected_story_take_id)
        if self._workspace_selection != 'track' or not clip:
            raise ValueError('请先选择轨道上的PGM。')
        session_id, path = self._take_index().key(clip)
        version = next((copy.deepcopy(row) for row in self._pgm_match_versions
            if row['session_id'] == session_id and row['media_path'] == path), None)
        if version is None:
            raise ValueError('请刷新后再核实区间。')
        identity, production_id = clip['id'], self.production_id()
        before = dict(sessions=self._sessions, scripts=self._scripts, transcripts=self._transcripts,
                      candidates=self._candidates, notes=self._field_notes, reference_state=self.reference_state)
        def operation(progress, cancelled):
            client, selected = self.ensure_workflow_context(progress, cancelled)
            if selected != production_id:
                raise ValueError('当前剧目已改变，请刷新后重试。')
            from library_workflow import BatchWorkflow
            result = BatchWorkflow(client, production_id, client.actor).mark_pgm_reviewed(
                session_id, path, expected_match_version=version,
                progress=progress, cancelled=cancelled)
            from pgm_library_view import review_snapshot_compatible
            return dict(result, client=client, review_only=review_snapshot_compatible(before, result['data'], session_id, path))
        def success(result):
            if result['client'] is not self.client:
                self.apply_workflow_context(result['client'], production_id)
            if result.get('review_only'):
                self._set_editor_review_data(result['data'], session_id, path)
            else:
                self._set_data(result['data'])
            if identity in self._story_clip_by_id:
                if result.get('review_only'):
                    self._show_workspace_clip(self._story_clip_by_id[identity], 'track')
                else:
                    self._story_take_selected(identity, replay=False)
            self.status.setText(Path(path).name+' 台本区间已核实。')
        self._run('确认PGM台本区间…', operation, success)

    def _set_editor_review_data(self, data, session_id, path):
        """Apply a worker-verified review-only reply without rescanning media/ASR.

        Any concurrent structural edit uses the ordinary full-snapshot path.
        This path preserves all cached summaries, card identities, source player,
        script widgets and zoom; only inventory state and track colors change.
        """
        rows, flags = self._pgm_inventory()
        index = self._take_index()
        self._sessions = data['sessions']
        index.sessions = {row['id']: row for row in self._sessions}
        self._take_index_signature = tuple(id(value) for value in (
            self._sessions, self._scripts, self._transcripts, self._candidates))
        key = session_id, path
        row = next(row for row in rows if index.key(row) == key)
        from pgm_editor_state import read_editor_state
        summary = index.file_summary(row)
        first = summary.get('first_candidate')
        editor = read_editor_state(index.sessions[session_id].get('config', {}), path, summary=summary,
            source=index.source(first) if first else None)
        state = dict(flags[key], **editor)
        flags = dict(flags)
        flags[key] = state
        rows = [dict(value, editor_pool=editor['pool'], editor_reviewed=editor['reviewed'])
                if index.key(value) == key else value for value in rows]
        signature = tuple(id(value) for value in (self._sessions, self._scripts,
            self._transcripts, self._candidates, self._pending_intervals))
        self._pending_catalogue_cache = signature, rows, flags
        from pgm_library_view import inventory_status_rows
        self._pgm_status_rows = inventory_status_rows(rows, flags, index)
        self._pgm_match_versions = data.get('pgm_match_versions', [])
        if self._session:
            self._session = index.sessions.get(self._session['id'])
        for number, session in enumerate(self._sessions):
            if session['id'] == session_id and number < self.sessions.rowCount():
                self.sessions.item(number, 1).setText(f'{session.get("updated_by", "")} {session.get("updated_at", "")}')
                self.sessions.item(number, 2).setText(str(session.get('revision', '')))
                break
        self._refresh_coverage_axis()
        self.render_pending()
        self._update_axis_count()
        self._refresh_reference_controls()
        self.catalogue_refreshed.emit(data)

    def preview_workspace_selection(self):
        view = (self.pending_angle_choice if self._workspace_selection == 'library' else self.angle_choice).currentData()
        if not view:
            return
        from library_bridge import resolve_local_path
        key = (resolve_local_path(view['media_path']), float(view['start_seconds']), float(view['end_seconds']))
        if (self._inline_request and self._inline_request[0] == key
                and key != getattr(self, '_inline_key', None)):
            self._inline_request = (key, not self._inline_request[1])
            return
        if (self.inline_preview and self.inline_preview.path == resolve_local_path(view['media_path'])
                and getattr(self, '_inline_key', None) == key):
            self.inline_preview.toggle_playback()
        else:
            self._load_inline(view['media_path'], view['start_seconds'], view['end_seconds'], replay=True)

    def _story_coverage_edited(self, candidate_id, first_line_id, last_line_id):
        clip = self._story_clip_by_id.get(candidate_id)
        if not clip:
            raise ValueError('该PGM已改变，请刷新后重试。')
        positions = self.story_timeline.index.positions
        first, last = positions[first_line_id], positions[last_line_id]
        identities = self.story_timeline.index.line_ids[first:last+1]
        return self._save_story_coverage(clip, identities)

    def _edit_story_take(self, candidate_id):
        self._story_take_selected(candidate_id, replay=False)
        self.coverage_range_input.setFocus()
        self.coverage_range_input.selectAll()

    def _save_story_coverage(self, clip, identities):
        if not self._take_index().file_summary(clip).get('first_candidate'):
            return self.manual_locate_pgm(clip, script_line_ids=identities)
        session_id, path = self._take_index().key(clip)
        version = next((copy.deepcopy(row) for row in self._pgm_match_versions
                        if row['session_id']==session_id and row['media_path']==path), None)
        if not version:
            raise ValueError('请刷新项目素材后再调整台本区间。')
        production_id = self.production_id()
        def operation(progress, cancelled):
            client, selected = self.ensure_workflow_context(progress, cancelled)
            if selected != production_id:
                raise ValueError('当前剧目已改变，请刷新后重试。')
            from library_workflow import BatchWorkflow
            result = BatchWorkflow(client, production_id, client.actor).update_pgm_script_coverage(
                session_id, path, identities, expected_match_version=version,
                progress=progress, cancelled=cancelled)
            return dict(result, client=client)
        def success(result):
            if result['client'] is not self.client:
                self.apply_workflow_context(result['client'], production_id)
            self._selected_story_take_id = result['candidate']['id']
            self._set_data(result['data'])
            self._story_take_selected(result['candidate']['id'], replay=False)
            self.status.setText('已调整 '+Path(path).name+' 的台本区间；原始素材、源时码和声音同步位置保持。')
        self._run('保存PGM台本区间…', operation, success)

    def open_browser_workspace(self):
        if self._browser_dialog:
            if self._browser_dialog.isMinimized():
                self._browser_dialog.showNormal()
            self._browser_dialog.raise_()
            self._browser_dialog.activateWindow()
            return
        flags = (Qt.WindowType.Window | Qt.WindowType.WindowMinMaxButtonsHint
                 | Qt.WindowType.WindowCloseButtonHint | Qt.WindowType.WindowFullscreenButtonHint)
        from pgm_workspace import WorkspaceDialog
        dialog = WorkspaceDialog(self, flags)
        dialog.setModal(False)
        dialog.setWindowTitle(self.production.currentText()+' · PGM时间轴')
        dialog.setMinimumSize(1000, 640)
        dialog.resize(1440, 900)
        box = QVBoxLayout(dialog)
        box.setContentsMargins(12, 12, 12, 12)
        box.addWidget(self.catalogue_page, 1)
        self.catalogue_page.show()
        self._browser_dialog = dialog
        save_shortcut = QShortcut(QKeySequence.StandardKey.Save, dialog)
        save_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        save_shortcut.activated.connect(lambda: self.browser_save_button.click())
        dialog.save_shortcut = save_shortcut
        def restore(_result=None):
            if self._browser_dialog is not dialog:
                return
            self._browser_geometry = dialog.saveGeometry()
            self._inline_timer.stop()
            self._inline_io.invalidate()
            if self._inline_request:
                self._inline_request = (self._inline_request[0], False)
            preview = self.inline_preview
            if preview:
                preview._suspended = True
                video = getattr(preview, 'video', None)
                if video is not None and hasattr(video, 'setFullScreen'):
                    video.setFullScreen(False)
                player = getattr(preview, 'player', None)
                if hasattr(preview, 'pause'):
                    preview.pause()
                elif player is not None:
                    player.pause()
            box.removeWidget(self.catalogue_page)
            self.catalogue_page.setParent(self)
            self.catalogue_page.hide()
            self._browser_dialog = None
            dialog.deleteLater()
            if not self._closed and self.window().isVisible():
                self.window().raise_()
                self.window().activateWindow()
        dialog.finished.connect(restore)
        self._update_browser_summary()
        if self._browser_geometry is not None:
            dialog.restoreGeometry(self._browser_geometry)
            dialog.show()
        else:
            dialog.showMaximized()
        self.browser_fullscreen_button.setText('退出全屏' if dialog.isFullScreen() else '全屏')
        if self.inline_preview:
            self.inline_preview._suspended = False
        if self._inline_request and self._inline_request[0] != getattr(self, '_inline_key', None):
            self._inline_timer.start()
        dialog.raise_()
        dialog.activateWindow()

    def close_browser_workspace(self):
        if self._browser_dialog:
            self._browser_dialog.close()

    def open_pending_workspace(self):
        self.open_browser_workspace()
        self.main_pages.setCurrentIndex(1)
        self.pending_list.setFocus()

    def render_matches(self):
        if not hasattr(self, 'match_list'):
            return
        current = self.match_list.currentItem()
        previous = current.data(Qt.ItemDataRole.UserRole) if current else None
        desired = self._selected_story_take_id
        if any(row['id'] == desired for row in self._visible_candidates):
            previous = desired
        self.match_list.blockSignals(True)
        rows = self._visible_candidates
        old_ids = [self.match_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.match_list.count())]
        same_items = old_ids == [row['id'] for row in rows]
        if not same_items:
            self.match_list.clear()
        line = next((row for row in self._scripts if row['id'] == self._script_filter), None)
        self.selected_line.setText(((line.get('speaker', '') + '：') if line.get('speaker') else '') + line['text']
                                  if line else '选择左侧台词')
        restore = -1
        take_index = self._take_index()
        for index, row in enumerate(rows):
            summary = take_index.file_summary(row)
            state = classification_state(row)
            if summary['match_status'] != 'matched':
                state = '待核实'
            elif row.get('status') != 'confirmed':
                state = '自动通过'
            label = '人工画面归类' if row.get('manual_anchor') or classification_method(row) == '人工画面归类' else f"{summary['match_score']:.0%}"
            if not summary['alignment_ready']:
                label += ' · 首句待定位'
            prefix = '基准 · ' if row.get('session_id') == self.reference_session_id else ''
            text = (Path(row.get('media_path', '')).name + '\n' + prefix +
                    f'【{state}】 {row.get("session_name", "")} · {label}')
            item = self.match_list.item(index) if same_items else QListWidgetItem()
            if item.text() != text:
                item.setText(text)
            item.setData(Qt.ItemDataRole.UserRole, row['id'])
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            item.setData(Qt.ItemDataRole.CheckStateRole, None)
            notes = self._clip_notes(row)
            item.setToolTip(row.get('media_path', '') + '\n' + summary['match_reason'] + '\n' +
                            summary['alignment_reason'] + ('\n' + notes if notes else ''))
            if not same_items:
                self.match_list.addItem(item)
            if row['id'] == previous:
                restore = index
        self.match_list.blockSignals(False)
        self._refresh_take_button()
        self.match_thumbnails.set_rows(self._visible_candidates)
        usable = [row for row in self._visible_candidates
                  if row.get('status') != 'rejected' and row.get('script_active', True)]
        scenes = len({row.get('session_id') for row in usable})
        pgm_files = {row.get('pgm_path') or row.get('media_path') for row in usable}
        pgm_files.discard(None)
        pgm_files.discard('')
        angle_count = sum(len(row.get('related_angles', [])) for row in usable)
        auto = sum(row.get('status') == 'confirmed' and row.get('classification_method') == 'auto_dialogue'
                   for row in usable)
        review = sum(row.get('status') == 'candidate' for row in usable)
        statuses = {(row['session_id'], row['media_path']): row['status'] for row in getattr(self, '_pgm_status_rows', [])}
        keys = {(row.get('session_id'), row.get('media_path')) for row in usable}
        matched = sum(statuses.get(key) == 'reviewed' for key in keys)
        review = sum(statuses.get(key) == 'unreviewed' for key in keys)
        self.match_count.setText((f'{len(keys)} 条PGM · 已核实 {matched} · 待核实 {review} · {scenes} 场'
                                 if usable else '此处没有已定位PGM，可在“未匹配台本”媒体池归类。'))
        if self._visible_candidates:
            chosen = restore if restore >= 0 else 0
            self.match_list.setCurrentRow(chosen)
            if same_items:
                self.match_selected(chosen)
        else:
            self.angle_choice.blockSignals(True)
            self.angle_choice.clear()
            self.angle_choice.blockSignals(False)
            self.angle_choice.setEnabled(False)
            self._angle_parent_id = None
            self.match_detail.clear()
            self.match_sync_status.clear()
            if self.main_pages.currentIndex() == 0:
                self._clear_inline('此处暂无PGM')
            self._show_coverage_input()
            self._highlight_script_coverage([])
        self._refresh_take_button()

    def match_selected(self, index):
        if not 0 <= index < len(self._visible_candidates):
            return
        row = self._visible_candidates[index]
        self._refresh_take_button()
        self._selected_story_take_id = row['id']
        interval_id = self._story_intervals_by_candidate.get(row['id'])
        if interval_id:
            blocked = self.pgm_timeline.blockSignals(True)
            self.pgm_timeline.setCurrentItem(self.pgm_timeline._items.get(interval_id))
            self.pgm_timeline.blockSignals(blocked)
        if hasattr(self, 'story_timeline'):
            self.story_timeline.select_take(row['id'], ensure_visible=False)
            self._highlight_script_coverage(row.get('covered_line_ids', []))
        self._show_coverage_input(row)
        advanced_index = next((i for i, value in enumerate(self._advanced_candidates)
                               if value['id'] == row['id']), -1)
        self.candidates.blockSignals(True)
        if advanced_index >= 0:
            self.candidates.selectRow(advanced_index)
        else:
            self.candidates.clearSelection()
            self.candidates.setCurrentCell(-1, -1)
        self.candidates.blockSignals(False)
        self._set_candidate_fields(row)
        self._set_angle_choices(row)
        self._show_match_details(row)
        if self.main_pages.currentIndex() == 0:
            self.angle_selected()

    @staticmethod
    def _angle_key(view):
        if not isinstance(view, dict):
            return None
        return (view.get('camera', 'PGM'), view.get('source_id', ''), view.get('media_path'),
                view.get('start_seconds'), view.get('end_seconds'))

    def _set_angle_choices(self, parent, combo=None, identity_attr='_angle_parent_id'):
        if combo is None:
            combo = self.angle_choice
        previous = self._angle_key(combo.currentData()) if getattr(
            self, identity_attr, None) == parent['id'] else None
        source = dict(camera='PGM', media_path=parent['media_path'],
                      start_seconds=parent['start_seconds'], end_seconds=parent['end_seconds'])
        views = [source] + [dict(angle) for angle in parent.get('related_angles', [])]
        counts = {}
        for view in views[1:]:
            camera = view.get('camera', '')
            counts[camera] = counts.get(camera, 0) + 1
        sequence = {}
        combo.blockSignals(True)
        combo.clear()
        selected = 0
        for index, view in enumerate(views):
            camera = view.get('camera', '')
            sequence[camera] = sequence.get(camera, 0) + 1
            label = ('PGM' if camera == 'PGM' else camera + '机')
            if counts.get(camera, 0) > 1:
                label += f' {sequence[camera]}/{counts[camera]}'
            if view.get('ambiguous'):
                label += '（重叠待核对）'
            elif camera != 'PGM' and not view.get('calibration_confirmed', False):
                label += '（未校准）'
            label += ' · ' + Path(view.get('media_path', '')).name
            combo.addItem(label, view)
            combo.setItemData(index, self._angle_tooltip(view), Qt.ItemDataRole.ToolTipRole)
            if self._angle_key(view) == previous:
                selected = index
        combo.setCurrentIndex(selected)
        combo.blockSignals(False)
        combo.setEnabled(True)
        setattr(self, identity_attr, parent['id'])

    @staticmethod
    def _angle_tooltip(view):
        text = [view.get('media_path', ''),
                f'源文件位置：{format_time(view.get("start_seconds"))} — {format_time(view.get("end_seconds"))}']
        if view.get('camera') != 'PGM':
            text.append(f'对应PGM：{format_time(view.get("pgm_start_seconds"))} — {format_time(view.get("pgm_end_seconds"))}')
            if view.get('ambiguous'):
                text.append('此机位存在重叠代理，请人工核对。')
            if not view.get('calibration_confirmed', False):
                text.append('机位时码与画面同步尚未确认，请先核对同步。')
        return '\n'.join(text)

    def angle_selected(self, *_args):
        view = self.angle_choice.currentData()
        self.angle_choice.setToolTip(self._angle_tooltip(view) if view else '')
        if getattr(self, '_selecting_story_take', False):
            return
        if not view or self.main_pages.currentIndex() != 0:
            return
        index = self.match_list.currentRow()
        if 0 <= index < len(self._visible_candidates):
            self._show_match_details(self._visible_candidates[index])
        self._load_inline(view['media_path'], view['start_seconds'], view['end_seconds'])

    def _clear_inline(self, message='选择片段查看画面'):
        if not hasattr(self, 'inline_host'):
            return
        if hasattr(self,'_inline_timer'):self._inline_timer.stop();self._inline_io.invalidate();self._inline_request=None
        if self.inline_preview:
            self.inline_preview.clear()
            self.inline_preview.hide()
        self._inline_key = None
        self.inline_placeholder.setText(message)
        self.inline_placeholder.show()

    def _load_inline(self, path, start, end, replay=False):
        from library_bridge import resolve_local_path
        try:
            local = resolve_local_path(path)
            key = local, float(start), float(end)
            if key == getattr(self, '_inline_key', None):
                self._inline_timer.stop();self._inline_io.invalidate();self._inline_request=None
                self.inline_placeholder.hide()
                if self.inline_preview:self.inline_preview.show()
                if replay and self.inline_preview:
                    self.inline_preview.replay()
                return
            if self._inline_request and self._inline_request[0] == key:
                # Ordinary refreshes retain explicit Play pressed while loading,
                # and do not postpone the existing decoder request.
                if replay:
                    self._inline_request = (key, True)
                return
            self._inline_request=(key,replay)
            # Fast selection/search should open only the final chosen clip.
            self._inline_io.invalidate();self._inline_timer.start()
            if self.inline_preview:
                if hasattr(self.inline_preview, 'pause'):
                    self.inline_preview.pause()
                elif hasattr(self.inline_preview,'player'):
                    self.inline_preview.player.pause()
                self.inline_preview.hide()
            self.inline_placeholder.setText('正在读取画面…');self.inline_placeholder.show()
        except Exception as exc:
            self._clear_inline('未能预览：' + str(exc))

    def _begin_inline_load(self):
        if self._closed or self._inline_request is None:return
        request=self._inline_request;key,replay=request
        # Reusing an already-open source requires no shared-storage stat or decoder reopen.
        if self.inline_preview and getattr(self.inline_preview,'path',None)==key[0]:
            return self._inline_media_ready(key,True,'')
        self._inline_io.request(key[0],0,lambda exists,error:self._inline_media_ready(key,exists,error))

    def _inline_media_ready(self,key,exists,error):
        if self._closed or self._inline_request is None or self._inline_request[0]!=key:return
        if error or not exists:
            self._clear_inline('未能预览：'+(error or '媒体未找到，请在Avid NEXIS Client Manager中确认工作区已挂载。'));return
        try:
            if self.inline_preview is None:
                self.inline_preview = InlinePreview(self.inline_host)
                self.inline_layout.addWidget(self.inline_preview)
            self.inline_placeholder.hide()
            self.inline_preview.show()
            self.inline_preview._suspended = not bool(self._browser_dialog and self._browser_dialog.isVisible())
            replay = self._inline_request[1]
            self._inline_key = key
            self._inline_request = None
            self.inline_preview.load_media(*key, play=replay)
        except Exception as exc:
            self._clear_inline('未能预览：' + str(exc))

    def preview_current_inline(self):
        if self.match_list.currentRow() < 0:
            self._clear_inline()
            return
        view = self.angle_choice.currentData()
        if view:
            self._load_inline(view['media_path'], view['start_seconds'], view['end_seconds'], replay=True)

    def pending_selected(self, index):
        if index < 0:
            return
        row = self._pending()
        if hasattr(self, 'workspace_inspector'):
            blocked = self.main_pages.blockSignals(True)
            self.main_pages.setCurrentIndex(1)
            self.main_pages.blockSignals(blocked)
            self._workspace_selection = 'library'
            self._ruler_selection_active = False
            self._selected_story_take_id = ''
            self.story_timeline.canvas.clear_selection()
            self._highlight_script_coverage([])
            self._show_workspace_clip(row, 'library')
        manual = bool(row.get('manual_pending'))
        self.pending_range.hide()
        previous = (self.pending_start.value(), self.pending_end.value()) if getattr(
            self, '_pending_range_parent_id', None) == row['id'] else None
        for spin, field in ((self.pending_start, 'start_seconds'), (self.pending_end, 'end_seconds')):
            spin.blockSignals(True)
            spin.setRange(float(row['start_seconds']), float(row['end_seconds']))
            spin.setValue(previous[0 if field == 'start_seconds' else 1] if previous else float(row[field]))
            spin.blockSignals(False)
        self._pending_range_parent_id = row['id']
        failure = self._audio_failure(row)
        self.pending_reason.setText('本次合板失败；仅归类画面' if failure else '仅归类画面，声音尚未合板'
                                   if manual and (not row.get('has_audio_sync') or row.get('reason_code') == 'audio_gap')
                                   else row.get('reason', ''))
        self.pending_reason.setToolTip(failure or row.get('reason', ''))
        self._set_pending_angles(row)

    def _set_pending_angles(self, row):
        from library_angles import AngleMapper
        source = dict(row)
        if source.get('manual_pending'):
            source.update(start_seconds=self.pending_start.value(), end_seconds=self.pending_end.value())
        if self._pending_angle_mapper is None:self._pending_angle_mapper=AngleMapper(self._sessions)
        if len(self._pending_angle_mapper.cache)>256:self._pending_angle_mapper.cache.clear()
        source.update(self._pending_angle_mapper.fields(source, source))
        self._set_angle_choices(source, self.pending_angle_choice, '_pending_angle_parent_id')
        self.pending_angle_selected()

    def pending_range_changed(self, *_args):
        if self.pending_list.currentItem() is not None:
            row = self._pending()
            if row.get('manual_pending') and self.pending_start.value() < self.pending_end.value():
                self._set_pending_angles(row)

    def pending_angle_selected(self, *_args):
        view = self.pending_angle_choice.currentData()
        self.pending_angle_choice.setToolTip(self._angle_tooltip(view) if view else '')
        if view and self.main_pages.currentIndex() == 1:
            self._load_inline(view['media_path'], view['start_seconds'], view['end_seconds'])

    def preview_pending_inline(self):
        if self.pending_list.currentItem() is None:
            self._clear_inline()
            return
        view = self.pending_angle_choice.currentData()
        if view:
            self._load_inline(view['media_path'], view['start_seconds'], view['end_seconds'], replay=True)

    def main_page_changed(self, index):
        if not hasattr(self, 'inline_host'):
            return
        if index == 0:
            self.pending_range.hide()
        if index == 0 and self.match_list.currentRow() >= 0:
            self.match_selected(self.match_list.currentRow())
        elif index == 1 and self.pending_list.currentRow() >= 0:
            self.pending_selected(self.pending_list.currentRow())
        else:
            self._clear_inline()

    def _show_match_details(self, row):
        from sound_fallback import sound_entry
        session = next((item for item in self._sessions if item['id'] == row.get('session_id')), {})
        config = session.get('config', {})
        proxies = [item.get('proxy_path') or item.get('path', '') for item in config.get('sources', [])]
        sounds = [item.get('path', '') for item in config.get('sound_sources', [])]
        pgms = [config['pgm']] if config.get('pgm') else []
        for key, destination in (('pgm', pgms), ('proxy', proxies), ('audio', sounds)):
            value = row.get(key, [])
            if isinstance(value, str):
                value = [value]
            for media in value if isinstance(value, list) else []:
                path = media.get('path', '') if isinstance(media, dict) else str(media)
                if path and path not in destination:
                    destination.append(path)
        registered_proxies, registered_sounds = [], []
        for media in config.get('library_media', []):
            if media.get('kind') == 'proxy' and media.get('path') not in proxies:
                registered_proxies.append(media['path'])
            if media.get('kind') == 'main_wav' and media.get('path') not in sounds:
                registered_sounds.append(media['path'])
            if media.get('kind') == 'pgm' and media.get('path') not in pgms:
                pgms.append(media['path'])
        text = [f'拍摄场次：{row.get("session_name", "")}', f'来源：{row.get("media_path", "")}',
                f'剧情归类：{classification_state(row)} · {classification_method(row)}',
                'PGM：' + ('、'.join(Path(path).name for path in pgms) or '尚未登记'),
                '代理：' + ('、'.join(Path(path).name for path in proxies) or '未关联'),
                '音频：' + ('、'.join(Path(path).name for path in sounds) or '未关联／同步')]
        path = row.get('pgm_path') or row.get('media_path')
        sync = sound_entry(config.get('library_sync'), path)
        failure = self._audio_failure(row)
        legacy = sound_entry(config.get('library_sound_fallback'), path)
        if sync.get('status') == 'matched' and not failure:
            methods = {item.get('method', '') for item in sync.get('placements', [])}
            summary = '已合板' + ('（波形匹配）' if 'waveform' in methods else '')
            if sync.get('uncovered'):
                summary += f' · {len(sync["uncovered"])} 处音频留空'
        elif legacy.get('status')=='fallback':
            state = legacy
            summary = '未匹配原始 WAV，音频留空'
            if state.get('error'):
                text.append('未使用原始 WAV 的原因：' + str(state['error']))
        elif failure:
            summary = '本次合板失败：' + failure
        elif sync.get('status') in ('failed', 'pending', 'unmatched'):
            summary = '待核对' + ('：' + sync['error'] if sync.get('error') else '')
        else:
            summary = '尚未合板'
        self.match_sync_status.setText('声音合板：' + summary)
        text.append('声音合板：' + summary)
        if registered_proxies:
            text.append('本场已登记代理（尚未关联）：' + '、'.join(Path(path).name for path in registered_proxies))
        if registered_sounds:
            text.append('本场已登记录音（尚未关联／同步）：' + '、'.join(Path(path).name for path in registered_sounds))
        if row.get('note'):
            text.append('备注：' + row['note'])
        if row.get('review_required') and row.get('review_reason'):
            text.append('核对原因：' + row['review_reason'])
        if getattr(self, '_angle_parent_id', None) == row.get('id'):
            view = self.angle_choice.currentData()
            if view and view.get('camera') != 'PGM':
                text.extend([f'当前画面：{view.get("camera")}机代理 · {Path(view["media_path"]).name}',
                             self._angle_tooltip(view)])
        for warning in row.get('related_angle_warnings', []):
            text.append('机位关联：' + str(warning))
        text.append('后续素材：在“导入素材”中选择追加到本拍摄场次。')
        self.match_detail.setPlainText('\n'.join(text))

    def _audio_failure(self, row, key=None):
        from sound_fallback import SoundStateIndex
        snapshot = getattr(self, '_sound_status_snapshot', None)
        if snapshot is None or snapshot[0] is not self._sessions:
            snapshot = (self._sessions, {item['id']: (item, SoundStateIndex(item.get('config') or {}))
                for item in self._sessions})
            self._sound_status_snapshot = snapshot
        session, sound_index = snapshot[1].get(row.get('session_id'), ({}, None))
        path = row.get('pgm_path') or row.get('media_path')
        config = session.get('config', {})
        if sound_index is None or sound_index.eligible(path):
            return ''
        state = sound_index.entry('library_processing', path)
        if state.get('status') == 'failed' and state.get('stage') == 'sync':
            return state.get('error') or '请补充原始声音或核对同步。'
        return ''

    def render_pending(self, *_args):
        if not hasattr(self, 'pending_list'):
            return
        previous = self.pending_list.currentItem()
        previous_id = previous.data(Qt.ItemDataRole.UserRole) if previous else None
        prepared, flags = self._pgm_inventory()
        signature = self._pending_catalogue_cache[0]
        rebuilt = getattr(self, '_pending_render_signature', None) != signature
        self._pending_render_signature = signature
        mode = self.pending_mode.currentData()
        query = self.pending_search.text().strip().casefold()
        pool_rows = [row for row in prepared if flags[self._take_index().key(row)]['pool'] == mode]
        self._pending_rows = [row for row in pool_rows
            if not query or query in flags[self._take_index().key(row)]['searchable']]
        blocked_mode = self.pending_mode.blockSignals(True)
        for number in range(self.pending_mode.count()):
            key = self.pending_mode.itemData(number)
            count = sum(state['pool'] == key for state in flags.values())
            label = '未匹配台本' if key == 'unmatched' else '轨道移出'
            self.pending_mode.setItemText(number, f'{label}（{count}）')
        self.pending_mode.blockSignals(blocked_mode)
        self._pending_by_id = {row['id']: row for row in self._pending_rows}
        self._pending_transcripts = [row for row in self._pending_rows if not row.get('manual_pending')]
        same = [self.pending_list.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(self.pending_list.count())] == [row['id'] for row in self._pending_rows]
        from sound_fallback import sound_path_key_factory
        path_key = sound_path_key_factory()
        blocked = self.pending_list.blockSignals(True)
        restore = -1
        try:
            if not same:
                self.pending_list.clear()
            for number, row in enumerate(self._pending_rows):
                state = flags[self._take_index().key(row)]
                item = self.pending_list.item(number) if same else QListWidgetItem()
                failure = self._audio_failure(row, path_key)
                badge = '轨道移出' if state['pool'] == 'removed' else '未匹配台本'
                if failure:
                    badge += ' · 合板失败'
                elif row.get('manual_pending') and not row.get('has_audio_sync'):
                    badge += ' · 无匹配音频'
                label = Path(row['media_path']).name+'\n'+row.get('session_name', '')+' · '+(
                    badge)
                if item.text() != label:
                    item.setText(label)
                item.setToolTip(row['media_path']+'\n'+(failure or state['brief_reason']))
                item.setData(Qt.ItemDataRole.UserRole, row['id'])
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                item.setData(Qt.ItemDataRole.CheckStateRole, None)
                if not same:
                    self.pending_list.addItem(item)
                if row['id'] == previous_id:
                    restore = number
            if restore >= 0:
                self.pending_list.setCurrentRow(restore)
        finally:
            self.pending_list.blockSignals(blocked)
        self.pending_count.setText(f'显示 {len(self._pending_rows)} / {len(pool_rows)} 条PGM' if query else f'{len(pool_rows)} 条PGM')
        if hasattr(self, 'pending_thumbnails'):
            self.pending_thumbnails.set_rows(self._pending_rows)
        self._refresh_pool_delete_button()
        if restore < 0:
            self.pending_angle_choice.clear()
            self.pending_angle_choice.setEnabled(False)
            self.pending_range.hide()
            if self.main_pages.currentIndex() == 1:
                self._clear_inline()
                if hasattr(self, 'workspace_inspector'):
                    self.workspace_inspector.hide()
        elif (not same or rebuilt) and self.main_pages.currentIndex() == 1:
            self.pending_selected(restore)

    def _refresh_pool_delete_button(self):
        if hasattr(self, 'pool_delete_button'):
            self.pool_delete_button.setEnabled(self.pending_list.currentItem() is not None
                and not self.is_busy() and not self.external_busy)

    def delete_pool_pgm(self):
        """A pool-only hide; the track Delete action retains its separate meaning."""
        if self.is_busy() or self.external_busy:
            raise ValueError('请等待当前任务完成。')
        if self.pending_list.currentItem() is None:
            return
        clip = self._pending()
        session_id, path = self._take_index().key(clip)
        version = next((copy.deepcopy(row) for row in self._pgm_match_versions
                        if row['session_id'] == session_id and row['media_path'] == path), None)
        if version is None:
            raise ValueError('请刷新项目素材后再删除这条PGM。')
        production_id = self.production_id()
        def operation(progress, cancelled):
            client, selected = self.ensure_workflow_context(progress, cancelled)
            if selected != production_id:
                raise ValueError('当前剧目已改变，请刷新后重试。')
            from library_workflow import BatchWorkflow
            result = BatchWorkflow(client, production_id, client.actor).hide_pgm_from_pool(
                session_id, path, expected_match_version=version, progress=progress, cancelled=cancelled)
            return dict(result, client=client)
        def success(result):
            if result['client'] is not self.client:
                self.apply_workflow_context(result['client'], production_id)
            self._clear_inline()
            self._set_data(result['data'])
            self.status.setText(Path(path).name+' 已从媒体池删除；相关数据与磁盘文件保留。')
        self._run('从媒体池删除PGM…', operation, success)

    def _pending(self):
        item = self.pending_list.currentItem()
        if item is None:
            raise ValueError('请选择待匹配的现场对白或PGM区间。')
        return self._pending_by_id[item.data(Qt.ItemDataRole.UserRole)]

    def preview_pending(self):
        row = self._pending()
        self._preview_media(row['media_path'], row['start_seconds'], row['end_seconds'])

    def manual_locate_pgm(self, clip=None, *, script_line_ids=None):
        if not self.script_timeline._nodes:
            raise ValueError('请先导入台本，再进行人工定位。')
        if clip is None:
            clip = self._pending() if self.main_pages.currentIndex() == 1 else self._candidate()
        session_id, path = self._take_index().key(clip)
        version = next((copy.deepcopy(row) for row in self._pgm_match_versions
                        if row['session_id'] == session_id and row['media_path'] == path), None)
        if version is None:
            raise ValueError('请先刷新项目素材，再为这条PGM进行人工定位。')
        duration = self._take_index().duration(clip)
        if duration is None:
            raise ValueError('请先补齐这条PGM的帧率和时长信息，再进行人工定位。')
        from manual_script_range import normalize_source_position
        first = self._take_index().file_summary(clip).get('first_candidate')
        if first:
            # A range correction keeps the real first utterance even if the
            # operator has browsed elsewhere or played this PGM to its end.
            source = self._take_index().source(first)
            position = normalize_source_position(duration, source.get('start_seconds'),
                fallback=clip.get('dialogue_start_seconds', clip.get('start_seconds', 0)))
        else:
            view = (self.pending_angle_choice if self.main_pages.currentIndex() == 1 else self.angle_choice).currentData() or {}
            position = self._pending_source_position(clip, view=view)
        from manual_script_range import ManualPgmRangeDialog
        dialog = ManualPgmRangeDialog(self.script_timeline._nodes, Path(path).name, duration, position, self)
        if script_line_ids:
            positions = self.story_timeline.index.positions
            first, last = positions[script_line_ids[0]]+1, positions[script_line_ids[-1]]+1
            dialog.line_range.setText(str(first) if first == last else f'{first}-{last}')
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.result_data
        if not values:
            return
        return self._save_pending_coverage(clip, values['script_line_ids'], values['start_seconds'])

    def _save_pending_coverage(self, clip, line_ids, position):
        if self.is_busy() or self.external_busy:
            raise ValueError('请等待当前任务完成。')
        session_id, path = self._take_index().key(clip)
        version = next((copy.deepcopy(row) for row in self._pgm_match_versions
                        if row['session_id'] == session_id and row['media_path'] == path), None)
        if version is None:
            raise ValueError('请先刷新项目素材，再为这条PGM进行人工定位。')
        duration = self._take_index().duration(clip)
        if duration is None or not 0 <= float(position) < float(duration):
            raise ValueError('请在PGM的首句位置暂停，再输入台本区间。')
        production_id = self.production_id()
        line_ids = list(line_ids)
        def operation(progress, cancelled):
            client, selected = self.ensure_workflow_context(progress, cancelled)
            if selected != production_id:
                raise ValueError('当前剧目已改变，请刷新后重试。')
            from library_workflow import BatchWorkflow
            result = BatchWorkflow(client, production_id, client.actor).associate_pending_interval(
                session_id, path, float(position), float(duration), line_ids[0],
                script_line_ids=line_ids, expected_match_version=version,
                progress=progress, cancelled=cancelled)
            return dict(result, client=client)
        def success(result):
            if result['client'] is not self.client:
                self.apply_workflow_context(result['client'], production_id)
            self._set_data(result['data'])
            self.story_axis.setCurrentIndex(self.story_axis.findData('pgm'))
            self.main_pages.setCurrentIndex(0)
            self._coverage_line_requested(line_ids[0], activate=False)
            node = next((row for row in self._pgm_interval_nodes if [session_id, path] in row.get('file_keys', [])), None)
            if node:
                blocked = self.pgm_timeline.blockSignals(True)
                self.pgm_timeline.setCurrentItem(self.pgm_timeline._items[node['id']])
                self.pgm_timeline.blockSignals(blocked)
                self.pgm_interval_selected(self.pgm_timeline.currentItem(), align_script=False)
                for index, row in enumerate(self._visible_candidates):
                    if self._take_index().key(row) == (session_id, path):
                        self.match_list.setCurrentRow(index)
                        self._story_take_selected(row['id'], replay=False)
                        break
            self.status.setText('已保存 '+Path(path).name+' 的台本区间，预览轴和待匹配清单已刷新。')
        self._run('保存PGM人工定位…', operation, success)

    def associate_pending(self):
        transcript = self._pending()
        selected_line = self._script_filter or self.script_timeline.current_line_id()
        line = next((row for row in self._scripts if row['id'] == selected_line), None)
        if line is None:
            line = self._choose_record('选择台本台词', self._scripts,
                lambda row: f'{row.get("scene", "")} {row.get("speaker", "")}：{row["text"]}')
        if line:
            if transcript.get('manual_pending'):
                self._associate_pending_interval(line, transcript)
            else:
                self._associate_records(line, transcript)

    def _associate_pending_interval(self, line, interval):
        start, end = self.pending_start.value(), self.pending_end.value()
        if not float(interval['start_seconds']) <= start < end <= float(interval['end_seconds']):
            raise ValueError('请填写所选PGM范围内的有效起止秒数。')
        from library_workflow import BatchWorkflow
        client, production_id = self.client, self.require_production()
        def operation(progress, cancelled):
            return BatchWorkflow(client, production_id, getattr(client, 'actor', 'local')).associate_pending_interval(
                session_id=interval['session_id'], pgm_path=interval['media_path'],
                start_seconds=start, end_seconds=end, script_line_id=line['id'],
                progress=progress, cancelled=cancelled)
        def success(result):
            self._set_data(result['data'])
            self.status.setText('画面已归类；同区间机位随此关联显示，声音合板状态保持原状。')
        self._run('保存PGM人工归类…', operation, success)

    def edit_candidate_note(self):
        row = self._candidate()
        note, ok = QInputDialog.getMultiLineText(self, '片段备注', '备注', row.get('note', ''))
        if ok:
            self.candidate_note.setText(note)
            self.update_candidate()

    def _callback_ui_alive(self):
        return not sip.isdeleted(self) and not self._closed

    def guarded(self, action):
        if not self._callback_ui_alive():
            return
        try:
            return action()
        except Exception as exc:
            if self._callback_ui_alive():
                QMessageBox.warning(self, '素材库', str(exc))

    def selected_session(self):
        return copy.deepcopy(self._session)

    def production_id(self):
        return self.production.currentData()

    def require_connection(self):
        if self.client is None:
            raise ValueError('请先新建或打开项目。')

    def require_production(self):
        self.require_connection()
        if not self.production_id():
            raise ValueError('请先新建或选择剧目。')
        return self.production_id()

    def set_external_busy(self, busy):
        self.external_busy = bool(busy)
        self._enable_controls()

    def is_busy(self):
        return self.worker is not None

    def _enable_controls(self):
        available = not self.is_busy() and not self.external_busy
        for control in self._controls:
            control.setEnabled(available)
        for control in (self.production, self.sessions, self.dialogue_session, self.url,
                        self.token, self.actor, self.session_name, self.notes,
                        self.media, self.model, self.language, self.working_session):
            control.setEnabled(available)
        self._refresh_reference_controls()

    def _run(self, title, operation, success):
        if self.is_busy() or self.external_busy:
            raise ValueError('请等待当前识别／导出任务结束。')
        self.status.setText(title)
        self._workflow_production_snapshot = self.production_id()
        self._workflow_actor_snapshot = self.actor.text().strip()
        self.progress.setValue(0)
        self.progress.show()
        self.cancel.show()
        self.cancel.setEnabled(True)
        worker = LibraryTask(operation, self)
        self.worker = worker
        self._enable_controls()
        self.busy_changed.emit(True)
        worker.progress.connect(self._progress)
        worker.result.connect(lambda result: self._completed(success, result))
        worker.failed.connect(self._failed)
        worker.finished.connect(lambda: self._finished(worker))
        worker.start()

    def _progress(self, value, message):
        if not self._callback_ui_alive():
            return
        self.progress.setValue(value)
        dialog=self._active_manual_dialog()
        if dialog is not None and dialog._auto_busy:dialog.automatic_progress(value,message)
        if message:
            self.status.setText(message)

    def _completed(self, callback, result):
        if not self._callback_ui_alive():
            return
        self.guarded(lambda: callback(result))

    def _failed(self, message):
        if not self._callback_ui_alive():
            return
        self.status.setText(message)
        dialog=self._active_manual_dialog()
        if dialog is not None and dialog._auto_busy:
            dialog.automatic_failed(message)
            return
        current = self._story_clip_by_id.get(self._selected_story_take_id)
        if current:
            self.story_timeline.select_take(current['id'], ensure_visible=False)
            self._highlight_script_coverage(current.get('covered_line_ids', []))
        if '取消' not in message and 'cancel' not in message.lower():
            QMessageBox.warning(self, '素材库操作未完成', message +
                ('\n其他人可能已更新此条目；请刷新后重新核对。' if any(
                    key in message.lower() for key in ('conflict', 'revision', '冲突', '版本')) else ''))

    def _finished(self, worker):
        if self.worker is worker:
            self.worker = None
        if not self._callback_ui_alive():
            try:
                if not sip.isdeleted(worker):
                    worker.deleteLater()
            except (TypeError, RuntimeError):
                pass
            return
        self.progress.hide()
        self.cancel.hide()
        self._enable_controls()
        self.busy_changed.emit(False)
        worker.deleteLater()

    def cancel_task(self):
        if self.worker:
            self.worker.requestInterruption()
            self.cancel.setEnabled(False)
            self.status.setText('正在取消；已完成的操作保留在当前项目中，请保存项目。')

    def _fill_productions(self, productions):
        previous = self.production_id()
        self.production.blockSignals(True)
        self.production.clear()
        for row in productions:
            self.production.addItem(row['name'], row['id'])
        found = self.production.findData(previous)
        if found >= 0:
            self.production.setCurrentIndex(found)
        self.production.blockSignals(False)
        self._clear_data()

    def _clear_data(self):
        self._coverage_axis_inputs = self._sound_status_snapshot = None
        self._ruler_selection_active = False
        self._session = None
        self._session_readiness = {}
        self.apply_reference_state({}, refresh_matches=False)
        self._sessions, self._scripts, self._transcripts, self._candidates = [], [], [], []
        self._field_notes = []
        self._pending_intervals, self._pending_rows, self._pending_transcripts = [], [], []
        self._pending_catalogue_cache = None
        self._timeline_nodes, self._visible_candidates, self._advanced_candidates = [], [], []
        self._prepared_timeline_nodes=None
        self._prepared_pgm_intervals=None
        self._pgm_interval_nodes, self._pgm_interval_by_id = [], {}
        self._pgm_status_rows = []
        self._pgm_match_versions = []
        self._timeline_by_id={};self._candidate_by_id={};self._candidates_by_line={};self._pending_angle_mapper=None
        for table in (self.sessions, self.materials, self.script, self.transcripts, self.candidates):
            table.setRowCount(0)
        self.session_name.clear()
        self.notes.clear()
        self._fill_session_combos()
        self._refresh_preparation_status()
        self.clear_script_filter()
        if hasattr(self, 'script_tree'):
            self.script_timeline.set_nodes([])
            self.pgm_timeline.set_nodes([])
            self.story_timeline.set_nodes([], [])
            self._selected_story_take_id = ''
            self._story_clip_by_id = {}
            self._highlight_script_coverage([])
            self.match_list.clear()
            self.match_thumbnails.set_rows([])
            self.pending_list.clear()
            self.match_detail.clear()
            self.match_sync_status.clear()
            self.match_count.clear()
            self.selected_line.setText('选择左侧台词')
            self._clear_inline()
            if hasattr(self, 'workspace_inspector'):
                self.workspace_inspector.hide()
        self._update_browser_summary()

    def production_changed(self):
        if self.is_busy():
            return
        self._clear_data()
        if self.client and self.production_id():
            self.guarded(self.refresh)

    def create_production(self):
        self.require_connection()
        name, ok = QInputDialog.getText(self, '新建剧目', '剧目名称')
        if not ok or not name.strip():
            return
        client = self.client
        def operation(progress, cancelled):
            created = client.call('create_production', name=name.strip())
            return created, client.call('list_productions')
        def success(result):
            created, productions = result
            self._fill_productions(productions)
            self.production.blockSignals(True)
            self.production.setCurrentIndex(self.production.findData(created['id']))
            self.production.blockSignals(False)
            self.status.setText('剧目已创建。可以保存场次、导入台本。')
        self._run('新建剧目…', operation, success)

    def _read_all(self, client, production_id):
        from library_workflow import BatchWorkflow
        return BatchWorkflow(client, production_id, getattr(client, 'actor', 'local')).read_all()

    def refresh(self):
        if self.client is None:
            return self._workflow().refresh_catalogue(self)
        client, production_id = self.client, self.production_id()
        def operation(progress, cancelled):
            from library_workflow import BatchWorkflow
            productions = client.call('list_productions')
            selected = production_id or (productions[0]['id'] if productions else None)
            result = BatchWorkflow(client, selected, getattr(client, 'actor', 'local')).refresh_review(
                progress, cancelled) if selected else None
            return productions, selected, result
        def success(result):
            productions, selected, reviewed = result
            self.production.blockSignals(True)
            self.production.clear()
            for row in productions:
                self.production.addItem(row['name'], row['id'])
            self.production.setCurrentIndex(self.production.findData(selected))
            self.production.blockSignals(False)
            if reviewed:
                self._set_data(reviewed['data'])
            else:
                self._clear_data()
            count = reviewed.get('autoconfirmed', 0) if reviewed else 0
            self.status.setText('已刷新。' + self.pgm_status_text())
        self._run('读取素材库…', operation, success)

    @staticmethod
    def _populate(table, values):
        blocked=table.blockSignals(True);table.setUpdatesEnabled(False)
        try:
            table.setRowCount(len(values))
            for row_index, row in enumerate(values):
                for column, value in enumerate(row):
                    item = QTableWidgetItem(str(value if value is not None else ''))
                    item.setToolTip(item.text())
                    table.setItem(row_index, column, item)
        finally:table.setUpdatesEnabled(True);table.blockSignals(blocked)

    def _set_data(self, data):
        self._sound_status_snapshot = None
        self._take_index_signature = self._notes_signature = None
        self._pending_catalogue_cache = None
        self._pgm_match_versions = copy.deepcopy(data.get('pgm_match_versions', []))
        previous = (self._session or {}).get('id')
        self._sessions = data['sessions']
        # A refresh may change project/service or remove a recording.  Retain
        # its identity only when that recording belongs to this fresh snapshot;
        # otherwise a stale ID would keep the header at an unrelated pending
        # state even while the visible recordings are already prepared.
        self._session = next((row for row in self._sessions if row['id'] == previous), None)
        self._scripts = data['scripts']
        self._field_notes = data.get('notes', [])
        self._transcripts = data['transcripts']
        self._pending_intervals = data.get('pending_pgm_intervals', [])
        self._candidates = data['candidates']
        self._prepared_timeline_nodes=data.get('timeline_nodes')
        self._prepared_pgm_intervals=data.get('pgm_interval_nodes')
        self._candidate_by_id={row['id']:row for row in self._candidates}
        self._candidates_by_line=defaultdict(list)
        for row in self._candidates:self._candidates_by_line[row.get('script_line_id')].append(row)
        self._pending_angle_mapper=None
        self._session_readiness = copy.deepcopy(data.get('session_readiness', {}))
        if 'reference_state' in data:
            self.apply_reference_state(data['reference_state'], refresh_matches=False)
        self._populate(self.sessions, [(r['name'], f'{r.get("updated_by", "")} {r.get("updated_at", "")}',
                                        r.get('revision', '')) for r in self._sessions])
        if self._session is None:
            self.sessions.blockSignals(True)
            self.sessions.clearSelection()
            self.sessions.setCurrentCell(-1, -1)
            self.sessions.blockSignals(False)
            self.session_name.clear()
            self.notes.clear()
            self._show_materials()
        self._populate(self.script, [(r.get('scene', ''), r.get('speaker', ''), r.get('text', ''))
                                    for r in self._scripts])
        self._fill_session_combos()
        names = {r['id']: r['name'] for r in self._sessions}
        media = {}
        for row in self._transcripts:
            key = (row.get('session_id'), row.get('media_path'))
            group = media.setdefault(key, {'count': 0, 'updated_at': ''})
            group['count'] += 1
            group['updated_at'] = max(group['updated_at'], row.get('updated_at', '') or '')
        self._populate(self.transcripts, [(names.get(session_id, ''), path, group['count'], group['updated_at'])
            for (session_id, path), group in media.items()])
        if previous:
            for index, session in enumerate(self._sessions):
                if session['id'] == previous:
                    self._session = session
                    self.sessions.selectRow(index)
                    self.session_name.setText(session['name'])
                    self.notes.setPlainText(session.get('config', {}).get('library_notes', ''))
                    self._show_materials()
                    break
        self.render_script_tree()
        self.render_pending()
        if not getattr(self, '_ruler_selection_active', False) and not self._visible_candidates and getattr(self, '_pending_rows', []):
            self.pgm_interval_selected(self.pgm_timeline.currentItem())
        self._refresh_reference_controls()
        self._refresh_preparation_status()
        self.catalogue_refreshed.emit(data)

    def _set_review_data(self, data):
        """Apply a background-refreshed decision snapshot, keeping media UI live.

        The worker has verified that sessions/scripts/transcripts are unchanged.
        Concurrent catalogue edits still take the normal full-refresh path.
        """
        self._take_index_signature = self._notes_signature = None
        self._pending_catalogue_cache = None
        self._pgm_match_versions = copy.deepcopy(data.get('pgm_match_versions', []))
        self._scripts = data['scripts']
        self._candidates = data['candidates']
        self._candidate_by_id = {row['id']: row for row in self._candidates}
        self._candidates_by_line = defaultdict(list)
        for row in self._candidates:
            self._candidates_by_line[row.get('script_line_id')].append(row)
        self._pending_intervals = data.get('pending_pgm_intervals', [])
        self._prepared_pgm_intervals = data.get('pgm_interval_nodes')
        self.render_pgm_intervals(incremental=True)
        self._prepared_timeline_nodes = data['timeline_nodes']
        self._timeline_nodes = data['timeline_nodes']
        self._timeline_by_id = {row['id']: row for row in self._timeline_nodes}
        self._refresh_coverage_axis()
        blocked = self.script_timeline.blockSignals(True)
        try:
            self.script_timeline.update_nodes(self._timeline_nodes)
        finally:
            self.script_timeline.blockSignals(blocked)
        previous_line = self._script_filter
        self._script_filter = self.script_timeline.current_line_id() or None
        if previous_line != self._script_filter:
            self._advanced_script_filter = self._script_filter
            item = self.script_timeline.currentItem()
            self.script_filter_label.setText(item.text(0)[:24] if item else '全部台词')
        if 'reference_state' in data:
            self.apply_reference_state(data['reference_state'], refresh_matches=False)
        self._update_axis_count()
        if not getattr(self, '_ruler_selection_active', False):
            self.filter_candidates(incremental=True)
        self.render_pending()
        self._refresh_reference_controls()

    def _fill_session_combos(self):
        if hasattr(self, 'working_session'):
            selected = (self._session or {}).get('id')
            self.working_session.blockSignals(True)
            self.working_session.clear()
            for row in self._sessions:
                if row.get('config', {}).get('library_record_type') != 'notes':
                    self.working_session.addItem(row['name'], row['id'])
            self.working_session.setCurrentIndex(self.working_session.findData(selected))
            self.working_session.blockSignals(False)
        for combo, include_all in ((self.dialogue_session, False), (self.session_filter, True),
                                   (self.take_session_filter, True)):
            previous = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            if include_all:
                combo.addItem('全部场次', '')
            for row in self._sessions:
                combo.addItem(row['name'], row['id'])
            found = combo.findData(previous)
            if found >= 0:
                combo.setCurrentIndex(found)
            combo.blockSignals(False)
        self.dialogue_session_changed()

    def working_session_changed(self, _index=None):
        self.select_working_session(self.working_session.currentData(), notify=True)

    def select_working_session(self, session_id, notify=False):
        """Synchronize scene selectors once; only header navigation notifies the host.

        The catalogue may be available before the advanced table is rendered.
        Keep the authoritative selection and the visible header independent of
        that table, including save/cancel restoration and directed navigation.
        """
        index = next((i for i, row in enumerate(self._sessions) if row['id'] == session_id), -1)
        self._session = self._sessions[index] if index >= 0 else None
        blocked = self.sessions.blockSignals(True)
        try:
            if 0 <= index < self.sessions.rowCount():
                self.sessions.selectRow(index)
            else:
                self.sessions.clearSelection()
                self.sessions.setCurrentCell(-1, -1)
        finally:
            self.sessions.blockSignals(blocked)
        blocked = self.working_session.blockSignals(True)
        try:
            self.working_session.setCurrentIndex(self.working_session.findData(session_id)
                                                  if self._session else -1)
        finally:
            self.working_session.blockSignals(blocked)
        self.session_name.setText(self._session['name'] if self._session else '')
        self.notes.setPlainText((self._session or {}).get('config', {}).get('library_notes', ''))
        self._show_materials()
        blocked = self.dialogue_session.blockSignals(True)
        try:
            self.dialogue_session.setCurrentIndex(self.dialogue_session.findData(session_id)
                                                   if self._session else -1)
        finally:
            self.dialogue_session.blockSignals(blocked)
        self.dialogue_session_changed()
        self._refresh_preparation_status()
        if notify and self._session is not None:
            self.working_session_selected.emit(self._session)
        return self._session is not None

    def session_selected(self):
        row = self.sessions.currentRow()
        if row < 0 or row >= len(self._sessions):
            return
        self.select_working_session(self._sessions[row]['id'])

    def new_session(self):
        production_id = self.require_production()
        name, ok = QInputDialog.getText(self, '新建拍摄场次', '场次名称，如 Day03_拉通_01')
        if not ok or not name.strip():
            return
        client = self.client
        def operation(progress, cancelled):
            saved = client.call('save_session', production_id=production_id, name=name.strip(),
                                config={'show_id': name.strip(), 'library_media': [], 'library_notes': '',
                                        'empty_camera_policy': 'pgm'}, segments=[])
            return saved, self._read_all(client, production_id)
        def success(result):
            saved, data = result
            self._session = saved
            self._set_data(data)
            self.status.setText('拍摄场次已建立，可从“导入素材”追加本场素材并归类。')
        self._run('新建拍摄场次…', operation, success)

    def _show_materials(self, *_args):
        items = (self._session or {}).get('config', {}).get('library_media', [])
        keyword = self.material_keyword.text().strip().casefold()
        items = [r for r in items if not keyword or keyword in ' '.join(str(r.get(key, ''))
            for key in ('kind', 'camera', 'path', 'notes')).casefold()]
        self._visible_materials = items
        self._populate(self.materials, [(r.get('kind', ''), r.get('camera', ''),
                                       r.get('path', ''), r.get('notes', '')) for r in items])

    def _save_materials(self, items):
        if not self._session:
            raise ValueError('请先新建或选择拍摄场次。')
        session = copy.deepcopy(self._session)
        config = session.get('config', {})
        config['library_media'] = items
        client, production_id = self.client, self.require_production()
        try:
            current_id = self.capture_current().get('config', {}).get('library_context', {}).get('session_id')
        except Exception:
            current_id = None
        def operation(progress, cancelled):
            saved = client.call('save_session', production_id=production_id, name=session['name'],
                config=config, segments=session.get('segments', []), session_id=session['id'],
                expected_revision=session['revision'], export_dir=session.get('export_dir', ''))
            return saved, self._read_all(client, production_id)
        def success(result):
            saved, data = result
            self._session = saved
            self._set_data(data)
            if current_id == saved['id']:
                self.session_saved.emit(copy.deepcopy(saved))
            self.status.setText('素材登记已保存；媒体文件仍保留在原位置。')
        self._run('保存素材登记…', operation, success)

    def add_media(self):
        self.require_production()
        if not self._session:
            raise ValueError('请先新建或选择拍摄场次。')
        paths, _ = QFileDialog.getOpenFileNames(self, '登记素材（可多选）', '', '所有素材 (*)')
        if not paths:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('素材归类')
        layout = QFormLayout(dialog)
        kind = QComboBox()
        for title, value in [('PGM', 'pgm'), ('代理', 'proxy'), ('原片', 'original'),
                             ('主 WAV', 'main_wav'), ('场记／其他', 'other')]:
            kind.addItem(title, value)
        camera = QComboBox()
        from camera_schema import cameras_for
        camera.addItems([''] + list(cameras_for(self._session.get('config', {}))))
        notes = QLineEdit()
        layout.addRow('类型', kind)
        layout.addRow('机位', camera)
        layout.addRow('备注', notes)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        from library_bridge import canonical_path
        items = copy.deepcopy(self._session.get('config', {}).get('library_media', []))
        existing = {(r.get('path'), r.get('kind'), r.get('camera')) for r in items}
        for path in paths:
            canonical = canonical_path(path)
            key = canonical, kind.currentData(), camera.currentText()
            if key not in existing:
                items.append({'id': str(uuid.uuid4()), 'kind': key[1], 'camera': key[2],
                              'path': canonical, 'notes': notes.text().strip()})
                existing.add(key)
        self._save_materials(items)

    def remove_media(self):
        if not self._session:
            raise ValueError('请选择拍摄场次。')
        index = self.materials.currentRow()
        items = copy.deepcopy(self._session.get('config', {}).get('library_media', []))
        if not 0 <= index < len(self._visible_materials):
            raise ValueError('请选择需要移除的素材登记。')
        selected_id = self._visible_materials[index]['id']
        items = [r for r in items if r.get('id') != selected_id]
        self._save_materials(items)

    def save_as_session(self):
        self.require_production()
        name, ok = QInputDialog.getText(self, '当前项目另存为场次', '新场次名称')
        if ok and name.strip():
            self.save_current(as_new=True, new_name=name.strip())

    def save_current(self, as_new=False, new_name=None):
        production_id = self.require_production()
        name = new_name or self.session_name.text().strip()
        if not name:
            raise ValueError('请填写场次名称；另建场次可先点“新场次”。')
        payload = copy.deepcopy(self.capture_current())
        if not isinstance(payload, dict) or not isinstance(payload.get('config'), dict):
            raise ValueError('当前项目快照不完整。')
        existing = copy.deepcopy(self._session) if not as_new else None
        context = payload['config'].get('library_context', {})
        if existing and context.get('session_id') != existing['id']:
            raise ValueError('当前编辑项目不属于所选场次。请先“载入所选场次”，或用“当前项目另存为场次”建立新记录。')
        if existing and context.get('revision') != existing.get('revision'):
            raise ValueError('共享场次已有新版本；当前编辑项目尚未载入该版本。请重新载入并核对，或另存为新场次。')
        from library_bridge import canonicalize_payload
        payload = canonicalize_payload(payload)
        if existing:
            for key in ('library_media', 'dialogue_markers'):
                payload['config'].setdefault(key, copy.deepcopy(existing.get('config', {}).get(key, [])))
        payload['config']['library_notes'] = (payload['config'].get('library_notes', '')
                                               if as_new else self.notes.toPlainText())
        client = self.client
        def operation(progress, cancelled):
            saved = client.call('save_session', production_id=production_id, name=name,
                config=payload['config'], segments=payload.get('segments', []),
                session_id=existing['id'] if existing else None,
                expected_revision=context.get('revision') if existing else None,
                export_dir=payload.get('export_dir', ''))
            return saved, self._read_all(client, production_id)
        def success(result):
            saved, data = result
            self._session = saved
            self._set_data(data)
            self.session_saved.emit(copy.deepcopy(saved))
            self.status.setText(f'已保存 {saved["name"]}，版本 {saved["revision"]}。')
        self._run('保存当前场次…', operation, success)

    def load_selected(self):
        if not self._session:
            raise ValueError('请选择场次。')
        self._load(self._session['id'])

    def _load(self, session_id, focus=None,after_load=None):
        self.require_connection()
        client = self.client
        prepare=getattr(self,'prepare_session_callback',None)
        def operation(progress,cancelled):
            session=client.call('get_session',session_id=session_id)
            if focus:session['library_focus']=copy.deepcopy(focus)
            if cancelled():raise ValueError('已取消。')
            if prepare:
                progress(35,'读取切点与媒体信息…')
                session['_prepared_editor']=prepare(session)
            if cancelled():raise ValueError('已取消。')
            return session
        def success(session):
            payload = copy.deepcopy(session)
            if focus:
                payload['library_focus'] = copy.deepcopy(focus)
            def loaded():
                accepted=self.load_session_callback(payload)
                if accepted is True and after_load:after_load()
                # The host owns a switch that may prompt to save or cancel.
                if not self.receivers(self.workflow_stage_requested):
                    self._session = session
                    self.status.setText(f'已载入 {session["name"]}。')
            # A save requested by the host is another task; release this read
            # worker before opening its save/discard/cancel prompt.
            from project_ui import _after_idle
            _after_idle(self, loaded)
        self._run('载入场次…', operation, success)

    def import_projects(self):
        production_id = self.require_production()
        paths, _ = QFileDialog.getOpenFileNames(self, '导入项目或识别结果', '', 'JSON (*.json)')
        if not paths:
            return
        client = self.client
        def operation(progress, cancelled):
            from library_bridge import canonicalize_payload, canonical_path
            saved = []
            for index, path in enumerate(paths):
                if cancelled():
                    raise ValueError('已取消导入；已保存的场次仍保留。')
                payload = json.loads(Path(path).read_text(encoding='utf-8-sig'))
                config = payload.get('config', payload)
                if not isinstance(config, dict) or not any(key in config for key in ('cut_xml', 'pgm', 'sources')):
                    raise ValueError(f'{Path(path).name} 不是 PGM 项目／识别结果。')
                payload = canonicalize_payload({'config': config, 'segments': payload.get('segments', [])})
                name = config.get('show_id') or config.get('name') or Path(path).stem
                saved.append(client.call('save_session', production_id=production_id,
                    name=str(name), config=payload['config'], segments=payload['segments'],
                    export_dir=canonical_path(str(Path(path).parent))))
                progress(round((index + 1) * 100 / len(paths)), f'已导入 {Path(path).name}')
            return saved, self._read_all(client, production_id)
        def success(result):
            saved, data = result
            self._set_data(data)
            self.status.setText(f'已导入 {len(saved)} 个场次，已有识别结果已保留。')
        self._run('导入已有项目…', operation, success)

    def import_script(self):
        production_id = self.require_production()
        path, _ = QFileDialog.getOpenFileName(self, '导入台本', '', '台本 (*.txt *.docx *.pdf)')
        if not path:
            return
        client = self.client
        def operation(progress, cancelled):
            from dialogue_engine import import_script
            lines = import_script(path)
            if not lines:
                raise ValueError('台本没有可读取的文字。')
            if cancelled():
                raise ValueError('已取消导入台本。')
            client.call('put_script', production_id=production_id, title=Path(path).stem, lines=lines,
                        source_path=path)
            return self._read_all(client, production_id)
        self._run('读取台本…', operation, lambda data: (
            self._set_data(data), self.status.setText(f'已载入 {len(data["scripts"])} 个台本区间。')))

    def script_selected(self):
        row = self.script.currentRow()
        if 0 <= row < len(self._scripts):
            line = self._scripts[row]
            self._script_filter = line['id']
            self._advanced_script_filter = line['id']
            self.script_timeline.set_nodes(self._timeline_nodes, selected_id=line['id'])
            self.script_filter_label.setText(line.get('text', '')[:24])
            self.filter_candidates()

    def clear_script_filter(self):
        self._advanced_script_filter = None
        self.script_filter_label.setText('全部台词')
        self.script.clearSelection()
        self._script_filter = (self.script_timeline.current_line_id() or None
                               if hasattr(self, 'script_timeline') else None)
        self.filter_candidates()

    def dialogue_session_changed(self, *_args):
        selected = self.dialogue_session.currentData()
        current = self.media.currentText()
        self.media.clear()
        session = next((r for r in self._sessions if r['id'] == selected), None)
        if session:
            self.media.addItems(media_paths(session.get('config', {})))
        if current and self.media.findText(current) >= 0:
            self.media.setCurrentText(current)

    def choose_media(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择连续录音、PGM或带参考声代理', '',
            '声音／视频 (*.wav *.aif *.aiff *.flac *.mp3 *.mov *.mp4 *.mxf *.m4v);;所有文件 (*)')
        if path:
            if self.media.findText(path) < 0:
                self.media.addItem(path)
            self.media.setCurrentText(path)

    def _transcription_input(self):
        production_id = self.require_production()
        session_id = self.dialogue_session.currentData()
        path = self.media.currentText().strip()
        if not session_id:
            raise ValueError('请先保存／选择录制场次。')
        if not path:
            raise ValueError('请选择对应的声音或视频媒体。')
        return production_id, session_id, path

    def transcribe(self):
        return self.recognize_and_match()

    def import_transcript(self):
        production_id, session_id, media_path = self._transcription_input()
        path, _ = QFileDialog.getOpenFileName(self, '导入带时间位置的台词', '',
                                             '台词转写 (*.srt *.vtt *.json)')
        if not path:
            return
        client = self.client
        def operation(progress, cancelled):
            from dialogue_engine import import_transcript
            from library_bridge import canonical_path
            segments = import_transcript(path)
            if cancelled():
                raise ValueError('已取消导入台词。')
            source = canonical_path(str(Path(path).expanduser().resolve()))
            metadata = {'source_path': source, 'source_kind': 'manual', 'asr_complete': True}
            if Path(path).suffix.lower() in ('.srt', '.vtt'):
                metadata['subtitle_path'] = source
            result = client.call('add_transcript', session_id=session_id, media_path=canonical_path(media_path),
                                 segments=segments, kind='imported', metadata=metadata)
            from library_workflow import BatchWorkflow
            workflow = BatchWorkflow(client, production_id, getattr(client, 'actor', 'local'))
            workflow._save_state(session_id, 'library_asr', canonical_path(media_path),
                dict(metadata, complete=True, active_transcript_ids=result['transcript_ids'], media_id=result['media_id']))
            return workflow.read_all()
        self._run('导入台词…', operation, lambda data: (
            self._set_data(data), self.status.setText('台词已导入。')))

    def match(self):
        production_id = self.require_production()
        client = self.client
        def operation(progress, cancelled):
            from dialogue_engine import match_dialogue
            from library_workflow import BatchWorkflow
            current = BatchWorkflow(client, production_id, getattr(client, 'actor', 'local')).read_all()
            script, transcripts = current['scripts'], current['transcripts']
            if not script or not transcripts:
                raise ValueError('请先导入台本，并识别或导入至少一个场次的台词。')
            candidates = match_dialogue(script, transcripts, progress=progress, cancelled=cancelled)
            if cancelled():
                raise ValueError('已取消跨场匹配。')
            from library_pending import is_manual_visual
            spoken = [row for row in transcripts if not is_manual_visual(row)]
            workflow = BatchWorkflow(client, production_id, getattr(client, 'actor', 'local'))
            workflow.save_match_results(current, spoken, candidates)
            return workflow.read_all()
        self._run('跨场匹配台词…', operation, lambda data: (
            self._set_data(data), self.status.setText('PGM匹配已更新。' + self.pgm_status_text())))

    def _choose_record(self, title, rows, label):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(880, 500)
        layout = QVBoxLayout(dialog)
        search = QLineEdit()
        search.setPlaceholderText('输入台词、场次、文件名或时间筛选')
        layout.addWidget(search)
        choices = QListWidget()
        layout.addWidget(choices)
        for row in rows:
            item = QListWidgetItem(label(row))
            item.setData(Qt.ItemDataRole.UserRole, row)
            choices.addItem(item)
        def filter_rows(value):
            for index in range(choices.count()):
                item = choices.item(index)
                item.setHidden(value.casefold() not in item.text().casefold())
        search.textChanged.connect(filter_rows)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(lambda: dialog.accept() if choices.currentItem() and not choices.currentItem().isHidden() else None)
        buttons.rejected.connect(dialog.reject)
        choices.itemDoubleClicked.connect(lambda item: dialog.accept())
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted and choices.currentItem():
            return choices.currentItem().data(Qt.ItemDataRole.UserRole)
        return None

    def manual_match(self):
        production_id = self.require_production()
        if not self._scripts or not self._transcripts:
            raise ValueError('请先导入台本和带时间位置的台词转写。')
        line = next((r for r in self._scripts if r['id'] == self._script_filter), None)
        if line is None:
            line = self._choose_record('选择台本台词', self._scripts,
                lambda r: f'{r.get("scene", "")}  {r.get("speaker", "")}：{r["text"]}')
        if not line:
            return
        transcript = self._choose_record('选择现场对白片段', self._transcripts,
            lambda r: f'{r.get("session_name", "")} | {Path(r["media_path"]).name} | '
                      f'{format_time(r["start_seconds"])} | {r["text"]}')
        if not transcript:
            return
        self._associate_records(line, transcript)

    def _associate_records(self, line, transcript):
        production_id = self.require_production()
        existing = next((r for r in self._candidates if r.get('script_line_id') == line['id']
                         and r.get('transcript_id') == transcript['id']), None)
        if existing and existing['status'] == 'confirmed':
            self.status.setText('此关联已确认。')
            return
        if existing and existing['status'] == 'rejected':
            if QMessageBox.question(self, '恢复已排除关联', '这条关联已被排除，是否确认改为已确认？') != QMessageBox.StandardButton.Yes:
                return
        client = self.client
        def operation(progress, cancelled):
            candidate = copy.deepcopy(existing)
            if candidate is None:
                client.call('replace_candidates', production_id=production_id, candidates=[{
                    'script_line_id': line['id'], 'transcript_id': transcript['id'],
                    'score': 0, 'reason': '人工关联'}])
                rows = client.call('list_candidates', production_id=production_id,
                                   script_line_id=line['id'])
                candidate = next(r for r in rows if r['transcript_id'] == transcript['id'])
                if candidate['status'] != 'candidate':
                    raise ValueError('此关联已由其他人处理；请刷新并核对当前状态。')
            tags = [tag for tag in candidate.get('tags', [])
                    if tag not in ('auto_dialogue', 'automatic_dialogue', 'pgm_match_removed')]
            from pgm_takes import MANUAL_PGM_TAG
            tags = list(dict.fromkeys(tags+[MANUAL_PGM_TAG]))
            client.call('update_candidate', candidate_id=candidate['id'], status='confirmed',
                        note=candidate.get('note', ''), tags=tags,
                        expected_revision=candidate['revision'])
            return self._read_all(client, production_id)
        self._run('保存人工关联…', operation, lambda data: (
            self._set_data(data), self.status.setText('人工关联已确认。')))

    def _filter_advanced_candidates(self):
        keyword = self.keyword.text().strip().casefold()
        session_id, status = self.session_filter.currentData(), self.status_filter.currentData()
        rows = []
        for row in (self._candidates_by_line.get(self._advanced_script_filter,[]) if self._advanced_script_filter
                    else self._candidates):
            if self._advanced_script_filter and row.get('script_line_id') != self._advanced_script_filter:
                continue
            if session_id and row.get('session_id') != session_id:
                continue
            if status and row.get('status') != status:
                continue
            if keyword:
                searchable = ' '.join(str(row.get(key, '')) for key in (
                    'script_text', 'transcript_text', 'script_scene', 'script_speaker',
                    'session_name', 'note', 'tags')).casefold()
                if keyword not in searchable:
                    continue
            rows.append(row)
        old_rows = self._advanced_candidates
        self._advanced_candidates = rows
        def values(row):
            return (
            row.get('session_name', ''),
            '\n'.join(filter(None, (row.get('script_text', ''), row.get('transcript_text', '')))),
            f'{Path(row.get("media_path", "")).name}\n{format_time(row.get("start_seconds"))} — {format_time(row.get("end_seconds"))}',
            classification_method(row), classification_state(row),
            row.get('note', ''), ', '.join(row.get('tags', [])) if isinstance(row.get('tags'), list)
            else row.get('tags', ''))
        if self.candidates.rowCount() == len(rows) and [r['id'] for r in old_rows] == [r['id'] for r in rows]:
            blocked = self.candidates.blockSignals(True)
            try:
                for index, (old, new) in enumerate(zip(old_rows, rows)):
                    if old != new:
                        for column, value in enumerate(values(new)):
                            item = QTableWidgetItem(str(value if value is not None else ''))
                            item.setToolTip(item.text())
                            self.candidates.setItem(index, column, item)
            finally:
                self.candidates.blockSignals(blocked)
        else:
            self._populate(self.candidates, [values(row) for row in rows])

    def filter_candidates(self, *_args, incremental=False):
        if self.advanced.isVisible() and self.pages.currentIndex() == 2:
            self._filter_advanced_candidates()
        # The director's main list always reflects the selected script node.
        # Advanced filters never hide a pending take or revive excluded history.
        position = self.story_timeline.index.positions.get(self._script_filter)
        selected_take = self.story_timeline.index.by_candidate.get(self._selected_story_take_id)
        first, last = (selected_take.first, selected_take.last) if selected_take else (position, position)
        matched_takes = self.story_timeline.index.overlapping(first, last)
        clips = [self._story_clip_by_id[take.candidate_id] for take in matched_takes
                 if take.candidate_id in self._story_clip_by_id]
        if self._selected_story_take_id in self._story_clip_by_id and not any(
                row['id']==self._selected_story_take_id for row in clips):
            clips.append(self._story_clip_by_id[self._selected_story_take_id])
        take_session, mode = self.take_session_filter.currentData(), self.take_mode.currentData()
        export_keys = {self._take_index().key(self._take_index().candidates[identity])
                       for identity in self._automatic_export_pairs()} if mode == 'selected' else set()
        def visible(row):
            if take_session and row.get('session_id') != take_session:
                return False
            if mode == 'selected':
                return self._take_index().key(row) in export_keys
            if mode == 'alternatives':
                return bool((not self.reference_session_id or row.get('session_id') != self.reference_session_id)
                            and self._take_index().file_summary(row)['match_status'] == 'matched')
            if mode == 'review':
                summary = self._take_index().file_summary(row)
                return summary['match_status'] != 'matched' or not summary['alignment_ready']
            return True
        self._visible_candidates = sorted((row for row in clips if visible(row)),
                                          key=lambda row: row.get('status') != 'confirmed')
        self.candidate_note.clear()
        self.candidate_tags.clear()
        self.render_matches()

    def _candidate(self):
        advanced = self.advanced.isVisible() and self.pages.currentIndex() == 2
        row = self.candidates.currentRow() if advanced else self.match_list.currentRow()
        values = self._advanced_candidates if advanced else self._visible_candidates
        if row < 0 or row >= len(values):
            raise ValueError('请选择候选片段。')
        return values[row]

    def _set_candidate_fields(self, value):
        self.candidate_note.setText(value.get('note', ''))
        tags = value.get('tags', [])
        self.candidate_tags.setText(', '.join(tags) if isinstance(tags, list) else str(tags))

    def candidate_selected(self):
        row = self.candidates.currentRow()
        if 0 <= row < len(self._advanced_candidates):
            value = self._advanced_candidates[row]
            self._set_candidate_fields(value)
            index = next((i for i, item in enumerate(self._visible_candidates)
                          if item['id'] == value['id']), -1)
            if index >= 0 and self.match_list.currentRow() != index:
                self.match_list.setCurrentRow(index)

    def remove_pgm_match(self, *, candidate_id=None):
        if self.is_busy() or self.external_busy:
            raise ValueError('请等待当前任务完成。')
        clip = self._story_clip_by_id.get(candidate_id) if candidate_id else self._candidate()
        if not clip:
            return
        session_id, path = self._take_index().key(clip)
        version = next((row for row in self._pgm_match_versions
                        if row['session_id'] == session_id and row['media_path'] == path), None)
        if version is None:
            raise ValueError('请先刷新项目素材，再移除这条PGM的匹配。')
        version = copy.deepcopy(version)
        production_id = self.production_id()
        def operation(progress, cancelled):
            client, selected = self.ensure_workflow_context(progress, cancelled)
            if selected != production_id:
                raise ValueError('当前剧目已改变，请刷新后重试。')
            from library_workflow import BatchWorkflow
            result = BatchWorkflow(client, production_id, client.actor).clear_pgm_matches(
                session_id, path, expected_session_revision=version['session_revision'],
                expected_candidate_revisions=version['candidate_revisions'],
                expected_reference_revision=version['reference_revision'], progress=progress, cancelled=cancelled)
            return dict(result, client=client)
        def success(result):
            if result['client'] is not self.client:
                self.apply_workflow_context(result['client'], production_id)
            self._selected_story_take_id = ''
            self.story_timeline.canvas.clear_selection()
            self._highlight_script_coverage([])
            self._clear_inline()
            blocked = self.pending_mode.blockSignals(True)
            self.pending_mode.setCurrentIndex(self.pending_mode.findData('removed'))
            self.pending_mode.blockSignals(blocked)
            self._set_data(result['data'])
            self.main_pages.setCurrentIndex(1)
            for number, row in enumerate(getattr(self, '_pending_rows', [])):
                if row.get('session_id') == session_id and row.get('media_path') == path:
                    self.pending_list.setCurrentRow(number)
                    break
            self.status.setText(Path(path).name+' 已退回素材库；媒体、声音同步和字幕保留。')
        self._run('将PGM退回素材库…', operation, success)

    def update_candidate(self, status=None):
        self.require_connection()
        row = copy.deepcopy(self._candidate())
        changes = {'status': status or row.get('status', 'candidate'), 'note': self.candidate_note.text().strip(),
                   'tags': [s.strip() for s in self.candidate_tags.text().replace('，', ',').split(',') if s.strip()]}
        client, production_id = self.client, self.production_id()
        # Compare structure in the worker, not on the GUI thread. Derived clip
        # counts are expected to change when one decision is committed.
        sessions, scripts, transcripts, notes = self._sessions, self._scripts, self._transcripts, self._field_notes
        def script_structure(rows):
            return [{key: value for key, value in row.items() if key != 'clip_count'} for row in rows]
        def operation(progress, cancelled):
            updated = client.call('update_candidate', candidate_id=row['id'],
                                  expected_revision=row['revision'], **changes)
            data = self._read_all(client, production_id)
            compatible = (data['sessions'] == sessions and data['transcripts'] == transcripts
                          and data.get('notes', []) == notes
                          and script_structure(data['scripts']) == script_structure(scripts))
            return updated, data, compatible
        def success(result):
            updated, data, compatible = result
            (self._set_review_data if compatible else self._set_data)(data)
            for index, candidate in enumerate(self._advanced_candidates):
                if candidate['id'] == updated['id']:
                    blocked = self.candidates.blockSignals(True)
                    self.candidates.selectRow(index)
                    self.candidates.blockSignals(blocked)
                    if self.advanced.isVisible() and self.pages.currentIndex() == 2:
                        self.candidate_selected()
                    break
            self.status.setText('归类已保存。')
        self._run('保存归类…', operation, success)

    def preview_candidate(self):
        row = self._candidate()
        self._preview_media(row['media_path'], row['start_seconds'], row['end_seconds'])

    def _preview_media(self, path, start, end):
        from library_bridge import resolve_local_path
        path = resolve_local_path(path)
        if not Path(path).is_file():
            raise ValueError('媒体未找到；请在Avid NEXIS Client Manager中确认对应工作区已挂载，并核对素材路径。')
        preview = MediaPreview(path, start, end, self)
        preview.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._previews.append(preview)
        preview.destroyed.connect(lambda: self._previews.remove(preview) if preview in self._previews else None)
        preview.show()

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.requestInterruption()
            self.status.setText('任务正在取消，请稍后关闭。')
            event.ignore()
            return
        self._closed = True
        if self._browser_dialog:
            self._browser_dialog.close()
        QApplication.instance().removeEventFilter(self)
        self._inline_timer.stop();self._inline_io.close()
        self._pending_search_timer.stop()
        self.match_thumbnails.close()
        self.pending_thumbnails.close()
        for preview in list(self._previews):
            preview.close()
        if self.inline_preview:
            self.inline_preview.close()
        failure = getattr(self, '_workflow_failure_dialog', None)
        if failure:
            failure.close()
        self.advanced.close()
        if getattr(self.client, 'is_local_project', False) is True:
            self.client.close()
        super().closeEvent(event)
