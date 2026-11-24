"""Review the one-to-one baseline edit/script partition without decoding ASR.

All edits are made on a deep copy. Shared boundaries always move together, and
preview positions are baseline edit frames, never camera or recording clocks.
"""
from __future__ import annotations

import copy
from pathlib import Path
import uuid

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QKeySequence, QShortcut, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (
    QApplication, QAbstractItemView, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)


def script_nodes(data):
    """Keep canonical line identities and the catalogue's script/line order."""
    raw = data.get('script_nodes', data.get('scripts', []))
    groups = {}
    for offset, row in enumerate(raw):
        if not isinstance(row, dict) or not row.get('active', row.get('script_active', True)):
            continue
        if row.get('lines') is not None:
            for index, line in enumerate(row['lines']):
                value = dict(line, script_id=line.get('script_id', row.get('id', '')))
                groups.setdefault(value['script_id'], []).append((index, value))
        else:
            groups.setdefault(row.get('script_id', ''), []).append((offset, row))
    result, seen = [], set()
    for rows in groups.values():
        def rank(entry):
            try:
                return float(entry[1].get('order', entry[1].get('line_order', entry[0])))
            except (ValueError, TypeError, OverflowError):
                return entry[0]
        for _, row in sorted(rows, key=rank):
            identity = str(row.get('id') or row.get('script_line_id') or '')
            if identity and identity not in seen:
                seen.add(identity)
                result.append(dict(row, id=identity))
    return result


def validate_partition(mapping, nodes):
    """Fast local edit guard; durable fingerprint validation runs on save."""
    try:
        duration = int(mapping['duration_frames'])
        if duration <= 0 or float(mapping['fps']) <= 0:
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError('基准时间线的时长或帧率无效。') from exc
    order = {row['id']: i for i, row in enumerate(nodes)}
    families = {row['id']: row.get('script_id', '') for row in nodes}
    cursor, previous_line, seen_ids = 0, -1, set()
    segments = mapping.get('segments', [])
    for row in segments:
        try:
            first, last = row['start_frame'], row['end_frame']
            if (isinstance(first, bool) or isinstance(last, bool) or
                    not isinstance(first, int) or not isinstance(last, int) or
                    first != cursor or last <= first):
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError('区间必须连续、无重叠，并完整覆盖基准时间线。') from exc
        identity = row.get('id')
        if not identity or identity in seen_ids:
            raise ValueError('区间编号缺失或重复。')
        seen_ids.add(identity)
        lines = row.get('line_ids', [])
        ranks = [order.get(value, -1) for value in lines]
        if (any(value < 0 for value in ranks) or
                ranks and (ranks[0] <= previous_line or ranks != list(range(ranks[0], ranks[-1]+1))) or
                len({families.get(value) for value in lines}) > 1):
            raise ValueError('台本区间必须按顺序排列，不能与其他区间重复或交叉。')
        if ranks:
            previous_line = ranks[-1]
        anchor = row.get('anchor_frame')
        if anchor is not None and (isinstance(anchor, bool) or not isinstance(anchor, int) or not first <= anchor < last):
            raise ValueError('首行台词起点必须位于本区间内。')
        cursor = last
    if not segments or cursor != duration:
        raise ValueError('区间必须连续、无重叠，并完整覆盖基准时间线。')
    return mapping


def _review(row, reason):
    row['needs_review'] = True
    row['reason'] = reason
    row['manual'] = True
    first, last = row['start_frame'], row['end_frame']
    if row.get('anchor_frame') is not None and not first <= row['anchor_frame'] < last:
        row['anchor_frame'] = None


def edit_partition(mapping, index, start, end, line_ids, anchor, *, confirmed=False, nodes):
    """Atomic copy-on-write shared-edge edit; no gaps can be introduced."""
    result = copy.deepcopy(mapping)
    segments = result['segments']
    row = segments[index]
    first_limit = segments[index-1]['start_frame']+1 if index else 0
    last_limit = segments[index+1]['end_frame']-1 if index+1 < len(segments) else result['duration_frames']
    if (not isinstance(start, int) or not isinstance(end, int) or
            not first_limit <= start < end <= last_limit or
            index == 0 and start != 0 or
            index+1 == len(segments) and end != result['duration_frames']):
        raise ValueError('边界调整后每个相邻区间至少需要保留一帧，首尾保持完整覆盖。')
    if index and row['start_frame'] != start:
        previous = segments[index-1]
        previous['end_frame'] = start
        _review(previous, '相邻边界已调整，请核对。')
    if index+1 < len(segments) and row['end_frame'] != end:
        following = segments[index+1]
        following['start_frame'] = end
        _review(following, '相邻边界已调整，请核对。')
    row.update(start_frame=start, end_frame=end, line_ids=list(line_ids), anchor_frame=anchor,
               needs_review=not confirmed, manual=True,
               reason='人工核实' if confirmed else '人工调整后待核实')
    validate_partition(result, nodes)
    return result


def split_partition(mapping, index, frame, *, nodes):
    result = copy.deepcopy(mapping)
    row = result['segments'][index]
    if not isinstance(frame, int) or not row['start_frame'] < frame < row['end_frame']:
        raise ValueError('分割位置必须在所选区间内部。')
    left, right = copy.deepcopy(row), copy.deepcopy(row)
    left['end_frame'], right['start_frame'] = frame, frame
    right['id'] = 'manual-' + uuid.uuid4().hex
    # A real first-line anchor belongs to exactly one child. Never copy line IDs
    # into both halves or invent a second onset for an ASR fragment.
    retained = right if row.get('anchor_frame') is not None and row['anchor_frame'] >= frame else left
    empty = left if retained is right else right
    empty.update(line_ids=[], anchor_frame=None, evidence_ids=[])
    for child in (left, right):
        _review(child, '人工分割后待核实')
    result['segments'][index:index+1] = [left, right]
    validate_partition(result, nodes)
    return result


def merge_partition(mapping, index, *, nodes):
    result = copy.deepcopy(mapping)
    segments = result['segments']
    if not 0 <= index < len(segments)-1:
        raise ValueError('请选择存在后续区间的行。')
    left, right = segments[index:index+2]
    ranks = {row['id']: offset for offset, row in enumerate(nodes)}
    identities = list(dict.fromkeys(left.get('line_ids', [])+right.get('line_ids', [])))
    if identities and [ranks[value] for value in identities] != list(range(ranks[identities[0]], ranks[identities[-1]]+1)):
        raise ValueError('这两个区间的台本编号不连续，不能直接合并。')
    left.update(end_frame=right['end_frame'], line_ids=identities,
                anchor_frame=left.get('anchor_frame') if left.get('line_ids') else right.get('anchor_frame'),
                evidence_ids=list(dict.fromkeys(left.get('evidence_ids', [])+right.get('evidence_ids', []))))
    _review(left, '人工合并后待核实；首行起点保留，其他台词起点需另行核实。')
    segments.pop(index+1)
    validate_partition(result, nodes)
    return result


class BaselineMapDialog(QDialog):
    """Resizable baseline partition editor. Construction never runs ASR."""
    def __init__(self, data, mapping, cut, parent=None):
        super().__init__(parent)
        self.data, self.cut = data, cut
        self.nodes = script_nodes(data)
        self._line_numbers = {row['id']: offset+1 for offset, row in enumerate(self.nodes)}
        self.mapping = copy.deepcopy(mapping)
        self.result_data = None
        self.rebuild_requested = False
        self._preview_block = None
        self._updating = False
        self.setWindowTitle('基准时间线与台本映射')
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.resize(1380, 900)
        self.setMinimumSize(1000, 680)
        root = QVBoxLayout(self)
        self.summary = QLabel()
        root.addWidget(self.summary)
        hint = QLabel('使用已识别的基准台词建立映射；完整覆盖、不重复。帧数为基准剪辑时间线相对位置，源素材时码保持不变。')
        hint.setWordWrap(True)
        root.addWidget(hint)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter, 1)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        navigation = QHBoxLayout()
        self.only_pending = QCheckBox('仅看待核实')
        self.only_pending.toggled.connect(lambda _checked: self.filter_pending())
        navigation.addWidget(self.only_pending)
        navigation.addStretch()
        self.next_pending_button = QPushButton('下一个待核实')
        self.next_pending_button.clicked.connect(self.next_pending)
        navigation.addWidget(self.next_pending_button)
        left_layout.addLayout(navigation)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['入点（时间／帧）', '出点（不含）', '台本行号', '核实'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._selected)
        left_layout.addWidget(self.table, 1)
        editor = self.editor = QGroupBox('调整所选区间')
        form = QFormLayout(editor)
        self.start, self.end, self.anchor = QSpinBox(), QSpinBox(), QSpinBox()
        duration = max(0, int(mapping.get('duration_frames', 0)))
        for control in (self.start, self.end, self.anchor):
            control.setRange(0, min(2147483647, duration))
            control.setSuffix(' 帧')
        form.addRow('区间入点', self.start)
        form.addRow('区间出点（不含）', self.end)
        self.line_range = QLineEdit()
        self.line_range.setPlaceholderText('例如 55 或 55-76')
        form.addRow('对应台本行号', self.line_range)
        self.no_dialogue = QCheckBox('无台词／动作／尚未确定台本位置')
        self.no_dialogue.toggled.connect(self._no_dialogue_changed)
        form.addRow('', self.no_dialogue)
        self.anchor_known = QCheckBox('已确定首行台词起点')
        self.anchor_known.toggled.connect(lambda _value: self._no_dialogue_changed(self.no_dialogue.isChecked()))
        form.addRow('', self.anchor_known)
        form.addRow('首行台词起点', self.anchor)
        use_head = QPushButton('使用当前预览位置作为台词起点')
        use_head.clicked.connect(self.use_preview_anchor)
        form.addRow('', use_head)
        split_row = QHBoxLayout()
        self.split_frame = QSpinBox()
        self.split_frame.setRange(0, min(2147483647, duration))
        self.split_frame.setSuffix(' 帧')
        split_row.addWidget(self.split_frame, 1)
        split_button = QPushButton('在此分割')
        split_button.clicked.connect(self.split_selected)
        split_row.addWidget(split_button)
        form.addRow('分割位置', split_row)
        actions = QHBoxLayout()
        for text, action in [('应用调整', self.apply_selected), ('确认本区间', self.confirm_selected),
                             ('与下一行合并', self.merge_selected)]:
            button = QPushButton(text)
            button.clicked.connect(action)
            actions.addWidget(button)
        form.addRow(actions)
        left_layout.addWidget(editor)
        splitter.addWidget(left)
        right = QSplitter(Qt.Orientation.Vertical)
        self.script = QTextEdit()
        self.script.setReadOnly(True)
        self.script.setPlaceholderText('台本')
        font = self.script.font()
        font.setPointSize(13)
        self.script.setFont(font)
        pieces, self._script_spans, cursor = [], {}, 0
        for offset, row in enumerate(self.nodes):
            text = f'{offset+1}  {row.get("speaker", "")}  {row.get("text", "")}'
            length = len(text.encode('utf-16-le'))//2
            self._script_spans[row['id']] = cursor, cursor+length
            pieces.append(text)
            cursor += length+1
        self.script.setPlainText('\n'.join(pieces))
        right.addWidget(self.script)
        preview_wrap = QWidget()
        preview_layout = QVBoxLayout(preview_wrap)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        self.preview_sources = QComboBox()
        self.preview_sources.currentIndexChanged.connect(self._load_preview)
        preview_layout.addWidget(self.preview_sources)
        from library_ui import InlinePreview
        self.preview = InlinePreview(self)
        self.preview.setMinimumHeight(230)
        preview_layout.addWidget(self.preview, 1)
        play = QPushButton('播放／暂停（空格）')
        play.clicked.connect(self.toggle_preview)
        preview_layout.addWidget(play)
        right.addWidget(preview_wrap)
        right.setSizes([360, 420])
        splitter.addWidget(right)
        splitter.setSizes([600, 740])
        self.reason = QLabel()
        self.reason.setWordWrap(True)
        root.addWidget(self.reason)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet('color: #b43d35;')
        root.addWidget(self.error)
        footer = QHBoxLayout()
        rebuild = QPushButton('按当前备选细化…')
        rebuild.setToolTip('使用已经识别的基准台词重建；不会再次进行语音识别。')
        rebuild.clicked.connect(self.request_rebuild)
        footer.addWidget(rebuild)
        footer.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText('保存映射')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self.save_mapping)
        buttons.rejected.connect(self.reject)
        footer.addWidget(buttons)
        root.addLayout(footer)
        self.space = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        self.space.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.space.activated.connect(self.toggle_preview)
        self._render(0)

    def _label_frame(self, frame):
        fps = max(1, round(float(self.mapping.get('fps', 25))))
        whole, sub = divmod(frame, fps)
        hours, rest = divmod(whole, 3600)
        minutes, seconds = divmod(rest, 60)
        return f'{hours:02}:{minutes:02}:{seconds:02}:{sub:02} · {frame}'

    def _range_text(self, identities):
        values = [self._line_numbers[value] for value in identities if value in self._line_numbers]
        return str(values[0]) if len(values) == 1 else f'{values[0]}-{values[-1]}' if values else ''

    def _render(self, selected=0):
        rows = self.mapping.get('segments', [])
        self._updating = True
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(rows))
        for number, row in enumerate(rows):
            values = [self._label_frame(row['start_frame']), self._label_frame(row['end_frame']),
                      self._range_text(row.get('line_ids', [])) or '无台词／未定位',
                      '待核实' if row.get('needs_review', True) else '已核实']
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setBackground(QColor('#e8f1fb' if row.get('needs_review', True) else '#e5f3e9'))
                self.table.setItem(number, column, item)
        unresolved = sum(bool(row.get('needs_review', True)) for row in rows)
        self.summary.setText(f'基准完整覆盖 {self.mapping.get("duration_frames", 0)} 帧 · '
                             f'{len(rows)} 个连续区间 · {unresolved} 个待核实')
        self._updating = False
        self.table.setUpdatesEnabled(True)
        self._apply_row_filter(selected)

    def filter_pending(self):
        self._apply_row_filter(self.table.currentRow())

    def _apply_row_filter(self, preferred=-1):
        rows = self.mapping.get('segments', [])
        pending = [index for index, row in enumerate(rows) if row.get('needs_review', True)]
        self.next_pending_button.setEnabled(bool(pending))
        self.table.setUpdatesEnabled(False)
        for index, row in enumerate(rows):
            self.table.setRowHidden(index, self.only_pending.isChecked() and not row.get('needs_review', True))
        visible = pending if self.only_pending.isChecked() else list(range(len(rows)))
        self.editor.setEnabled(bool(visible))
        if visible:
            current = preferred if preferred in visible else next((value for value in visible if value >= preferred), visible[0])
            self.table.selectRow(current)
            self.table.scrollToItem(self.table.item(current, 0))
            self._selected()
        else:
            self.table.clearSelection()
            self.table.setCurrentCell(-1, -1)
            self.preview_sources.clear()
            self._preview_block = None
            self.preview.clear()
            self._highlight([])
            self.reason.setText('所有区间均已核实。取消“仅看待核实”可浏览完整映射。' if rows else '没有可用映射区间。')
        self.table.setUpdatesEnabled(True)

    def next_pending(self, *_args):
        rows = self.mapping.get('segments', [])
        pending = [index for index, row in enumerate(rows) if row.get('needs_review', True)]
        if not pending:
            self._apply_row_filter(self.table.currentRow())
            return
        current = self.table.currentRow()
        next_row = next((index for index in pending if index > current), pending[0])
        self.table.selectRow(next_row)
        self.table.scrollToItem(self.table.item(next_row, 0))
        self._selected()

    def _selected(self):
        if self._updating:
            return
        index = self.table.currentRow()
        if not 0 <= index < len(self.mapping.get('segments', [])) or self.table.isRowHidden(index):
            return
        row = self.mapping['segments'][index]
        self.start.setValue(row['start_frame'])
        self.end.setValue(row['end_frame'])
        self.start.setEnabled(index > 0)
        self.end.setEnabled(index+1 < len(self.mapping['segments']))
        self.line_range.setText(self._range_text(row.get('line_ids', [])))
        self.no_dialogue.setChecked(not row.get('line_ids'))
        self.anchor_known.setChecked(row.get('anchor_frame') is not None)
        self._no_dialogue_changed(self.no_dialogue.isChecked())
        self.anchor.setValue(row.get('anchor_frame') if row.get('anchor_frame') is not None else row['start_frame'])
        self.split_frame.setValue((row['start_frame']+row['end_frame'])//2)
        self.reason.setText(str(row.get('reason') or ''))
        self.error.clear()
        self._highlight(row.get('line_ids', []))
        blocked = self.preview_sources.blockSignals(True)
        self.preview_sources.clear()
        for block in self.cut.get('pgm_sources', []):
            first, last = max(row['start_frame'], int(block['start'])), min(row['end_frame'], int(block['end']))
            if first < last:
                self.preview_sources.addItem(Path(block['path']).name,
                                             dict(block=block, first=first, last=last))
        self.preview_sources.blockSignals(blocked)
        self._load_preview()
        self._editor_state = self._control_state()

    def _control_state(self):
        return (self.start.value(), self.end.value(), self.line_range.text().strip(),
                self.no_dialogue.isChecked(), self.anchor_known.isChecked(), self.anchor.value())

    def _has_pending_edit(self):
        return self._control_state() != getattr(self, '_editor_state', self._control_state())

    def _highlight(self, identities):
        selections = []
        for identity in identities:
            if identity not in self._script_spans:
                continue
            first, last = self._script_spans[identity]
            cursor = QTextCursor(self.script.document())
            cursor.setPosition(first)
            cursor.setPosition(last, QTextCursor.MoveMode.KeepAnchor)
            extra = QTextEdit.ExtraSelection()
            extra.cursor = cursor
            extra.format = QTextCharFormat()
            extra.format.setBackground(QColor('#c7e3ff'))
            selections.append(extra)
        self.script.setExtraSelections(selections)
        if identities and identities[0] in self._script_spans:
            cursor = QTextCursor(self.script.document())
            cursor.setPosition(self._script_spans[identities[0]][0])
            self.script.setTextCursor(cursor)
            self.script.ensureCursorVisible()

    def _load_preview(self, *_args):
        value = self.preview_sources.currentData()
        if not value:
            self._preview_block = None
            self.preview.clear()
            return
        self._preview_block = value['block']
        fps = float(self.mapping['fps'])
        first = (value['first']-self._preview_block['start'])/fps
        last = (value['last']-self._preview_block['start'])/fps
        self.preview.pause()
        self.preview.load_media(self._preview_block['path'], first, last, play=False)

    def toggle_preview(self):
        # A typed space in an editable field remains text input.
        focus = QApplication.focusWidget()
        if any(focus is field or field.isAncestorOf(focus) for field in
               (self.line_range, self.start, self.end, self.anchor, self.split_frame) if focus is not None):
            return
        self.preview.toggle_playback()

    def _no_dialogue_changed(self, checked):
        self.line_range.setEnabled(not checked)
        self.anchor_known.setEnabled(not checked)
        self.anchor.setEnabled(not checked and self.anchor_known.isChecked())

    def use_preview_anchor(self):
        if self._preview_block is None:
            return
        frame = self._preview_block['start']+round(self.preview.player.position()*float(self.mapping['fps'])/1000)
        # A preview at its exclusive end is not a dialogue onset.
        frame = min(max(self.start.value(), frame), max(self.start.value(), self.end.value()-1))
        self.anchor.setValue(frame)
        self.anchor_known.setChecked(True)

    def _apply(self, confirmed=False):
        from manual_script_range import parse_script_range
        try:
            index = self.table.currentRow()
            if index < 0:
                raise ValueError('请先选择一个区间。')
            ids = [] if self.no_dialogue.isChecked() else parse_script_range(self.line_range.text(), self.nodes)
            anchor = self.anchor.value() if ids and self.anchor_known.isChecked() else None
            self.mapping = edit_partition(self.mapping, index, self.start.value(), self.end.value(),
                                          ids, anchor, confirmed=confirmed, nodes=self.nodes)
        except ValueError as exc:
            self.error.setText(str(exc))
            return False
        self._render(index)
        return True

    def apply_selected(self, *_args):
        return self._apply(False)

    def confirm_selected(self, *_args):
        return self._apply(True)

    def split_selected(self, *_args):
        try:
            index, frame = self.table.currentRow(), self.split_frame.value()
            if self._has_pending_edit() and not self._apply(False):
                return
            self.mapping = split_partition(self.mapping, index, frame, nodes=self.nodes)
        except (ValueError, IndexError) as exc:
            self.error.setText(str(exc))
            return
        self._render(index)

    def merge_selected(self, *_args):
        try:
            index = self.table.currentRow()
            if self._has_pending_edit() and not self._apply(False):
                return
            self.mapping = merge_partition(self.mapping, index, nodes=self.nodes)
        except (ValueError, IndexError) as exc:
            self.error.setText(str(exc))
            return
        self._render(index)

    def request_rebuild(self, *_args):
        answer = QMessageBox.question(self, '重建基准映射',
            '将按照当前绿色备选的台本范围，使用已有台词识别结果细化映射。\n已保存的人工区间会保留；本窗口尚未保存的调整不会应用。是否继续？',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.rebuild_requested = True
        self.result_data = None
        self.accept()

    def save_mapping(self):
        if self._has_pending_edit() and not self._apply(False):
            return
        try:
            validate_partition(self.mapping, self.nodes)
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        # Durable evidence/metadata validation is done by the background
        # ReferenceWorkflow.save_baseline_map transaction before persistence.
        self.result_data = copy.deepcopy(self.mapping)
        self.accept()

    def mapping_result(self):
        return copy.deepcopy(self.result_data)

    def result_mapping(self):
        return self.mapping_result()

    def done(self, result):
        self.preview.clear()
        super().done(result)
