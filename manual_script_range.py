"""One manual PGM-to-script range input; positions never alter media clocks."""
import re
import math

from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QLabel, QLineEdit,
    QVBoxLayout,
)


def parse_script_range(value, nodes):
    text = re.sub(r'\s+', '', str(value)).removeprefix('台本')
    match = re.fullmatch(r'(\d+)(?:[-—–－~～至](\d+))?', text)
    if not match:
        raise ValueError('请输入台本行号或区间，例如 55 或 55-76。')
    first, last = int(match[1]), int(match[2] or match[1])
    if not 1 <= first <= last <= len(nodes):
        raise ValueError(f'台本区间应在 1-{len(nodes)} 内，且起点不能大于终点。')
    selected = nodes[first-1:last]
    if len({row.get('script_id', '') for row in selected}) != 1:
        raise ValueError('一个区间不能跨越两份不同台本，请分别定位。')
    return [str(row['id']) for row in selected]


def normalize_source_position(duration, position=0, fallback=0):
    """A preview's exclusive end is never a usable first-dialogue anchor."""
    try:
        duration = float(duration)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('PGM时长信息无效，请先核对素材。') from exc
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('PGM时长信息无效，请先核对素材。')
    for candidate in (position, fallback, 0):
        try:
            value = float(candidate)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value) and 0 <= value < duration:
            return value


class ManualPgmRangeDialog(QDialog):
    def __init__(self, nodes, filename, duration, position=0, parent=None):
        super().__init__(parent)
        self.nodes = nodes
        self._initial_source_position = normalize_source_position(duration, position)
        self.duration = float(duration)
        self.result_data = None
        self.setWindowTitle('人工定位')
        self.resize(490, 260)
        layout = QVBoxLayout(self)
        filename_label = QLabel(filename)
        filename_label.setWordWrap(True)
        layout.addWidget(filename_label)
        form = QFormLayout()
        self.line_range = QLineEdit()
        self.line_range.setPlaceholderText('55-76，或单独一行 55')
        form.addRow('台本行号／区间', self.line_range)
        self.position = QDoubleSpinBox()
        self.position.setDecimals(6)
        self.position.setRange(0, max(0, self.duration))
        self.position.setValue(self._initial_source_position)
        self._initial_display_position = self.position.value()
        self.position.setSingleStep(.04)
        self.position.setSuffix(' 秒')
        self.position.setToolTip('台本首行在PGM中的起点。可先暂停预览，再打开本窗口，或直接填写。')
        form.addRow('首行对应的PGM位置', self.position)
        layout.addLayout(form)
        self.summary = QLabel('')
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.error = QLabel('')
        self.error.setWordWrap(True)
        self.error.setStyleSheet('color: #b43d35;')
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('保存定位')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self.save_range)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.line_range.textChanged.connect(self.update_summary)
        self.line_range.setFocus()

    def update_summary(self):
        try:
            ids = parse_script_range(self.line_range.text(), self.nodes)
        except ValueError:
            self.summary.clear()
            return
        by_id = {row['id']: row for row in self.nodes}
        first, last = by_id[ids[0]], by_id[ids[-1]]
        self.summary.setText(str(first.get('text', ''))[:100] + (
            '\n至：'+str(last.get('text', ''))[:100] if len(ids) > 1 else ''))
        self.error.clear()

    def save_range(self):
        try:
            identities = parse_script_range(self.line_range.text(), self.nodes)
            position = self.position.value()
            if position == self._initial_display_position:
                # Editing coverage alone must not quantize an existing ASR or
                # waveform anchor to the spin box's display precision.
                position = self._initial_source_position
            if not math.isfinite(position) or not 0 <= position < self.duration:
                raise ValueError('定位点必须早于PGM结束位置。')
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        self.result_data = dict(script_line_ids=identities, start_seconds=position)
        self.accept()
