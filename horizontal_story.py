"""Virtualized script-range timeline with independent, editable PGM lanes.

The horizontal coordinate is script order, never source timecode. The caller owns
all association mutations; dragging emits line IDs and does not alter media or
metadata. One canvas paints the visible range rather than creating take widgets.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
import heapq
import math
from pathlib import Path

from PyQt6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen
from PyQt6.QtWidgets import QAbstractScrollArea, QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget


@dataclass(frozen=True)
class StoryTake:
    candidate_id: str
    interval_id: str
    session_id: str
    session_name: str
    filename: str
    first: int
    last: int
    status: str
    reference: bool = False
    reviewed: bool = False


class StoryIndex:
    """Compact O(n log n) model; each file retains its own covered script range."""
    def __init__(self, script_nodes=(), pgm_nodes=(), reference_session_id=''):
        groups = {}
        for number, row in enumerate(script_nodes):
            identity = str(row.get('script_line_id') or row.get('id') or row.get('line_id') or '')
            if not identity:
                continue
            try:
                order = float(row.get('order', number))
                if not math.isfinite(order):
                    order = number
            except (ValueError, TypeError):
                order = number
            groups.setdefault(row.get('script_id', ''), []).append((order, number, identity, row))
        ordered = [entry for values in groups.values() for entry in sorted(values, key=lambda entry: entry[:2])]
        self.lines = [dict(id=row[2], text=str(row[3].get('text') or ''),
                           speaker=str(row[3].get('speaker') or ''), scene=str(row[3].get('scene') or ''))
                      for row in ordered]
        self.line_ids = [row['id'] for row in self.lines]
        self.positions = {identity: number for number, identity in enumerate(self.line_ids)}
        self.takes = []
        seen = set()
        for node in pgm_nodes:
            statuses = {(str(row.get('session_id') or ''), str(row.get('media_path') or '')): row
                        for row in node.get('file_statuses', [])}
            for clip in node.get('clips', []):
                identity = str(clip.get('candidate_id') or clip.get('id') or '')
                key = str(clip.get('session_id') or ''), str(clip.get('media_path') or '')
                if not identity or key in seen or clip.get('status') == 'rejected':
                    continue
                numbers = [self.positions[str(value)] for value in clip.get('covered_line_ids', node.get('covered_line_ids', []))
                           if str(value) in self.positions]
                if not numbers:
                    continue
                seen.add(key)
                status = str(clip.get('pgm_match_status') or statuses.get(key, {}).get('status') or
                             ('matched' if clip.get('status') == 'confirmed' else 'review'))
                self.takes.append(StoryTake(identity, str(node.get('id') or ''), key[0],
                    str(clip.get('session_name') or statuses.get(key, {}).get('session_name') or ''),
                    Path(key[1]).name, min(numbers), max(numbers), status,
                    bool(reference_session_id and key[0] == str(reference_session_id)),
                    clip.get('editor_reviewed') is True))
        self.takes.sort(key=lambda row: (row.first, row.last, not row.reference, row.session_name, row.filename, row.candidate_id))
        self._take_starts = [row.first for row in self.takes]
        self._take_max_ends, maximum = [], -1
        for row in self.takes:
            maximum = max(maximum, row.last)
            self._take_max_ends.append(maximum)
        self.by_candidate = {row.candidate_id: row for row in self.takes}
        self.by_interval = {}
        for row in self.takes:
            self.by_interval.setdefault(row.interval_id, []).append(row)

    def overlapping(self, first, last):
        if first is None or last is None:return []
        begin = bisect_left(self._take_max_ends,first)
        end = bisect_right(self._take_starts,last)
        return [row for row in self.takes[begin:end] if row.last >= first]

    def line_range(self, first, last):
        first, last = max(0, min(first, last)), min(len(self.line_ids) - 1, max(first, last))
        return self.line_ids[first:last + 1] if self.line_ids else []

    def lane_layout(self, line_width, min_width=18.):
        """Assign overlapping drawn rectangles to separate lanes, including tiny takes."""
        line_width = max(.01, float(line_width))
        active, vacant, lanes, by_candidate = [], [], [], {}
        for take in self.takes:
            first = take.first * line_width
            end = max((take.last + 1) * line_width, first + min_width)
            while active and active[0][0] <= first:
                _, lane = heapq.heappop(active)
                heapq.heappush(vacant, lane)
            if vacant:
                lane = heapq.heappop(vacant)
            else:
                lane = len(lanes)
                lanes.append([])
            lanes[lane].append((first, end, take))
            by_candidate[take.candidate_id] = lane, first, end
            heapq.heappush(active, (end, lane))
        return lanes, by_candidate


class StoryTimelineCanvas(QAbstractScrollArea):
    take_selected = pyqtSignal(str)
    take_double_clicked = pyqtSignal(str)
    take_remove_requested = pyqtSignal(str)
    interval_selected = pyqtSignal(str)
    line_selected = pyqtSignal(str)
    coverage_edited = pyqtSignal(str, str, str)
    coverage_highlighted = pyqtSignal(list)
    scale_changed = pyqtSignal(float)
    selection_state_changed = pyqtSignal(bool)
    LEFT = 64
    HEADER = 28
    SCRIPT_HEIGHT = 100
    LANE_HEIGHT = 48
    BLOCK_HEIGHT = 36
    MIN_BLOCK_WIDTH = 18

    def __init__(self, parent=None):
        super().__init__(parent)
        self.index = StoryIndex()
        self.line_width = 96.
        self.compact_ruler = False
        self.lanes, self._layout = [], {}
        self._lane_starts = []
        self.selected_candidate_id = ''
        self.selected_line_id = ''
        self.highlighted_line_ids = set()
        self._highlight_ranges = []
        self._drag = None
        self._ruler_drag = None
        self._range_anchor = None
        self._emitting_ruler_selection = False
        self._range_scroll_timer = QTimer(self)
        self._range_scroll_timer.setInterval(30)
        self._range_scroll_timer.timeout.connect(self._autoscroll_ruler)
        self._press = None
        self._hover = None
        self._remove_key_down = False
        self._wheel_remainder = {'horizontal': 0., 'vertical': 0.}
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setMinimumHeight(120)
        self.setAccessibleName('横向 PGM 区间时间轴')
        self.setAccessibleDescription('上方是独立 PGM 素材轨道，下方是完整台本；滚轮上下浏览轨道，Shift 加滚轮横向浏览台本，Ctrl 或 Command 加滚轮缩放；触控板支持双向滚动，拖动块的边缘调整台本范围；拖选底部行号选择台本范围，Shift 扩展、Ctrl 或 Command 加选；选中 PGM 后可按 Delete 或 Backspace 移除匹配。')
        self.setToolTip('拖选底部行号选择台本范围；Shift 扩展、Ctrl／Command 加选。滚轮上下浏览轨道；Shift＋滚轮横向浏览台本；Ctrl／Command＋滚轮缩放；选中 PGM 后按 Delete／Backspace 移除匹配。')
        self.horizontalScrollBar().setToolTip('横向浏览台本；可在此滚轮滚动，或使用 Shift＋滚轮。')
        self.verticalScrollBar().setToolTip('上下浏览 PGM 素材轨道；滚轮上下滚动。')
        self.horizontalScrollBar().installEventFilter(self)
        self.verticalScrollBar().installEventFilter(self)
        self.horizontalScrollBar().valueChanged.connect(lambda _: self.viewport().update())
        self.verticalScrollBar().valueChanged.connect(lambda _: self.viewport().update())
        self.setStyleSheet('QAbstractScrollArea { background: white; border: 1px solid #d9e2ed; border-radius: 6px; }')

    def sizeHint(self):
        return QSize(1100, 380)

    def set_nodes(self, script_nodes, pgm_nodes, reference_session_id=''):
        self.cancel_drag()
        self._cancel_ruler_drag(restore=False)
        highlighted = self.highlighted_line_ids
        anchor_id = (self.index.line_ids[self._range_anchor]
                     if self._range_anchor is not None and self._range_anchor < len(self.index.line_ids) else '')
        self._wheel_remainder = {'horizontal': 0., 'vertical': 0.}
        self.index = StoryIndex(script_nodes, pgm_nodes, reference_session_id)
        if self.selected_candidate_id and self.selected_candidate_id not in self.index.by_candidate:
            self.selected_candidate_id = ''
            highlighted = []
        elif self.selected_candidate_id:
            selected = self.index.by_candidate[self.selected_candidate_id]
            highlighted = self.index.line_range(selected.first, selected.last)
        # A review flag update or newly imported PGM must not discard a script
        # range the editor just selected. Rebuild ranges using the new order.
        self.highlighted_line_ids = set()
        self._highlight_ranges = []
        self.set_highlighted_line_ids(highlighted)
        self._range_anchor = self.index.positions.get(anchor_id)
        if self.selected_line_id not in self.index.positions:
            self.selected_line_id = ''
        self._relayout()
        self.selection_state_changed.emit(bool(self.selected_candidate_id))

    def _relayout(self):
        self.lanes, self._layout = self.index.lane_layout(self.line_width, self.MIN_BLOCK_WIDTH)
        self._lane_starts = [[entry[0] for entry in lane] for lane in self.lanes]
        self._update_scrollbars()
        if self.selected_candidate_id in self._layout:
            self._ensure_lane(self._layout[self.selected_candidate_id][0])
        self.viewport().update()

    def _script_top(self):
        height = self.viewport().height()
        script_height = 40 if self.compact_ruler else min(self.SCRIPT_HEIGHT, max(28, round(height * .34)))
        return min(height - 24, max(self.HEADER + 42, height - script_height))

    def set_compact_ruler(self, compact=True):
        """The editing workspace already shows the complete script at its left."""
        self.compact_ruler = bool(compact)
        self._update_scrollbars()
        self.viewport().update()

    def _script_height(self):
        return max(24, self.viewport().height() - self._script_top())

    def _vertical_offset(self):
        """Pixels above the lanes nearest the script; scrollbar values stay standard."""
        bar = self.verticalScrollBar()
        return bar.maximum() - bar.value()

    def _update_scrollbars(self):
        vertical_offset = self._vertical_offset()
        available = max(1, self.viewport().width() - self.LEFT)
        extent = max(len(self.index.lines) * self.line_width,
                     max((entry[2] for entry in self._layout.values()), default=0))
        self.horizontalScrollBar().setPageStep(available)
        self.horizontalScrollBar().setSingleStep(max(8, round(self.line_width)))
        self.horizontalScrollBar().setRange(0, max(0, math.ceil(extent - available)))
        track_height = max(1, self._script_top() - self.HEADER)
        bar = self.verticalScrollBar()
        bar.setPageStep(track_height)
        bar.setSingleStep(self.LANE_HEIGHT)
        bar.setRange(0, max(0, len(self.lanes) * self.LANE_HEIGHT - track_height))
        # Keep the same distance from the script when viewport height or lane
        # count changes. A new timeline naturally starts at the bottom (max).
        bar.setValue(bar.maximum() - vertical_offset)
        self._last_script_top = self._script_top()

    def set_line_width(self, value, anchor_x=None):
        value = max(.05, min(360., float(value)))
        if math.isclose(value, self.line_width):
            return
        anchor_x = self.LEFT + max(0, self.viewport().width() - self.LEFT) / 2 if anchor_x is None else anchor_x
        position = (self.horizontalScrollBar().value() + anchor_x - self.LEFT) / self.line_width
        self.line_width = value
        self._relayout()
        self.horizontalScrollBar().setValue(round(position * value - anchor_x + self.LEFT))
        self.scale_changed.emit(value)

    def fit_all(self):
        if self.index.lines:
            self.set_line_width(max(.05, (self.viewport().width() - self.LEFT - 5) / len(self.index.lines)), self.LEFT)
            self.horizontalScrollBar().setValue(0)

    def line_at_x(self, x):
        if not self.index.lines:
            return None
        position = math.floor((x - self.LEFT + self.horizontalScrollBar().value()) / self.line_width)
        return max(0, min(len(self.index.lines) - 1, position))

    def line_rect(self, number):
        return QRectF(self.LEFT + number * self.line_width - self.horizontalScrollBar().value(),
                      self._script_top(), self.line_width, self._script_height())

    def take_rect(self, candidate_id):
        layout = self._layout.get(str(candidate_id))
        if not layout:
            return QRectF()
        lane, first, end = layout
        take = self.index.by_candidate[str(candidate_id)]
        if self._drag and self._drag['candidate_id'] == str(candidate_id):
            first, last = self._drag['first'], self._drag['last']
            first, end = first * self.line_width, max((last + 1) * self.line_width, first * self.line_width + self.MIN_BLOCK_WIDTH)
        y = self._script_top() - (lane + 1) * self.LANE_HEIGHT + 6 + self._vertical_offset()
        return QRectF(self.LEFT + first - self.horizontalScrollBar().value(), y, end - first, self.BLOCK_HEIGHT)

    def visible_takes(self):
        """Yield only viewport lanes and logarithmically indexed horizontal spans."""
        left = self.horizontalScrollBar().value()
        right = left + self.viewport().width() - self.LEFT
        track_top, track_bottom = self.HEADER, self._script_top()
        offset = self._vertical_offset()
        first_lane = max(0, (self._script_top() + offset - track_bottom) // self.LANE_HEIGHT)
        last_lane = min(len(self.lanes), math.ceil((self._script_top() + offset - track_top) / self.LANE_HEIGHT))
        for number in range(first_lane, last_lane):
            lane = self.lanes[number]
            begin = max(0, bisect_right(self._lane_starts[number], left) - 1)
            for item_number in range(begin, len(lane)):
                first, end, take = lane[item_number]
                if first > right:
                    break
                if end >= left:
                    yield take
        # A handle dragged beyond its original range must remain painted.
        if self._drag:
            yield self.index.by_candidate[self._drag['candidate_id']]

    def hit_take(self, point):
        if point.x() < self.LEFT or not self.HEADER <= point.y() < self._script_top():
            return None
        lane_number = math.floor((self._script_top() + self._vertical_offset() - point.y()) / self.LANE_HEIGHT)
        if not 0 <= lane_number < len(self.lanes):
            return None
        x = point.x() - self.LEFT + self.horizontalScrollBar().value()
        number = bisect_right(self._lane_starts[lane_number], x) - 1
        if number < 0:
            return None
        first, end, take = self.lanes[lane_number][number]
        return take if first <= x <= end and self.take_rect(take.candidate_id).contains(point) else None

    def set_highlighted_line_ids(self, identities):
        selected = {str(value) for value in identities if str(value) in self.index.positions}
        if selected != self.highlighted_line_ids:
            self.highlighted_line_ids = selected
            self._highlight_ranges = []
            for number in sorted(self.index.positions[value] for value in selected):
                if self._highlight_ranges and number == self._highlight_ranges[-1][1] + 1:
                    self._highlight_ranges[-1] = self._highlight_ranges[-1][0], number
                else:
                    self._highlight_ranges.append((number, number))
            self.viewport().update()

    def clear_selection(self, emit=False):
        self.cancel_drag()
        if not self._emitting_ruler_selection:
            self._cancel_ruler_drag(restore=False)
        self.selected_candidate_id = ''
        self.selection_state_changed.emit(False)
        self.set_highlighted_line_ids([])
        self.viewport().update()
        if emit:
            self.coverage_highlighted.emit([])

    def select_line(self, line_id, ensure_visible=True, emit=False):
        line_id = str(line_id or '')
        if line_id not in self.index.positions:
            return
        if not self._emitting_ruler_selection:
            self.clear_selection()
            self._range_anchor = self.index.positions[line_id]
        self.selected_line_id = line_id
        if ensure_visible:
            self._ensure_line(self.index.positions[line_id])
        self.viewport().update()
        if emit:
            self.line_selected.emit(line_id)

    def _ensure_line(self, number):
        first, last = number * self.line_width, (number + 1) * self.line_width
        bar = self.horizontalScrollBar()
        available = self.viewport().width() - self.LEFT
        if first < bar.value():
            bar.setValue(math.floor(first))
        elif last > bar.value() + available:
            bar.setValue(math.ceil(first if self.line_width > available else last - available))

    def select_take(self, candidate_id, ensure_visible=True, emit=False):
        take = self.index.by_candidate.get(str(candidate_id))
        if not take:
            return
        self.selected_candidate_id = take.candidate_id
        self.selection_state_changed.emit(True)
        self.selected_line_id = self.index.line_ids[take.first]
        identities = self.index.line_range(take.first, take.last)
        self.set_highlighted_line_ids(identities)
        if ensure_visible:
            self._ensure_line(take.first)
            _, first, last = self._layout[take.candidate_id]
            available = self.viewport().width() - self.LEFT
            bar = self.horizontalScrollBar()
            if last - first <= available and last > bar.value() + available:
                bar.setValue(math.ceil(last - available))
            self._ensure_lane(self._layout[take.candidate_id][0])
        self.viewport().update()
        if emit:
            self.coverage_highlighted.emit(identities)
            self.line_selected.emit(self.selected_line_id)
            self.interval_selected.emit(take.interval_id)
            self.take_selected.emit(take.candidate_id)

    def set_selected_interval(self, interval_id):
        takes = self.index.by_interval.get(str(interval_id or ''), [])
        current = self.index.by_candidate.get(self.selected_candidate_id)
        if current in takes:
            return
        if takes:
            self.select_take(takes[0].candidate_id, ensure_visible=False)
        else:
            self.clear_selection()

    def focus_selected_take(self):
        if self.selected_candidate_id not in self.index.by_candidate:
            return False
        self.select_take(self.selected_candidate_id, ensure_visible=True)
        self.setFocus(Qt.FocusReason.ShortcutFocusReason)
        return True

    def _ensure_lane(self, lane):
        bar = self.verticalScrollBar()
        offset = self._vertical_offset()
        bottom, top = lane * self.LANE_HEIGHT, (lane + 1) * self.LANE_HEIGHT
        if top > offset + bar.pageStep():
            offset = top - bar.pageStep()
        elif bottom < offset:
            offset = bottom
        bar.setValue(bar.maximum() - offset)

    def resizeEvent(self, event):
        # Preserve a manually scrolled view. Only keep the selected block in
        # sight when it was already visible before resizing the viewport.
        lane = self._layout.get(self.selected_candidate_id, (None,))[0]
        old_script_top = getattr(self, '_last_script_top', self._script_top())
        old_y = (old_script_top - (lane + 1) * self.LANE_HEIGHT + 6 + self._vertical_offset()
                 if lane is not None else None)
        selected_visible = old_y is not None and old_y < old_script_top and old_y + self.BLOCK_HEIGHT > self.HEADER
        super().resizeEvent(event)
        self._update_scrollbars()
        if selected_visible:
            self._ensure_lane(lane)

    def scrollContentsBy(self, dx, dy):
        self.viewport().update()

    def paintEvent(self, event):
        painter = QPainter(self.viewport())
        painter.setFont(self.font())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.viewport().rect(), QColor('#f6f8fb'))
        plot = QRectF(self.LEFT, self.HEADER, max(0, self.viewport().width() - self.LEFT), max(0, self._script_top() - self.HEADER))
        painter.fillRect(plot, QColor('#ffffff'))
        painter.save()
        painter.setClipRect(plot)
        offset = self._vertical_offset()
        for lane in range(max(0, offset // self.LANE_HEIGHT), min(len(self.lanes), math.ceil((offset + plot.height()) / self.LANE_HEIGHT))):
            y = self._script_top() - lane * self.LANE_HEIGHT + offset
            painter.setPen(QColor('#edf1f6'))
            painter.drawLine(QPointF(self.LEFT, y), QPointF(self.viewport().width(), y))
        for take in self.visible_takes():
            rect = self.take_rect(take.candidate_id)
            selected = take.candidate_id == self.selected_candidate_id
            fill, border = self.take_colors(take, selected)
            painter.setBrush(QColor(fill))
            painter.setPen(QPen(QColor(border), 2 if selected or take.reference else 1))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 5, 5)
            if rect.width() > 38:
                painter.setPen(QColor('#ffffff'))
                name = f'{take.session_name} · {take.filename}' if take.session_name else take.filename
                text_rect = rect.adjusted(9, 0, -9, 0)
                label = painter.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, max(0, round(text_rect.width())))
                painter.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
            if selected:
                painter.setPen(QPen(QColor('#dcefff'), 2))
                for x in (rect.left() + 4, rect.right() - 4):
                    painter.drawLine(QPointF(x, rect.top() + 10), QPointF(x, rect.bottom() - 10))
        painter.restore()
        painter.fillRect(QRectF(self.LEFT, self._script_top(), self.viewport().width() - self.LEFT, self._script_height()), QColor('#ffffff'))
        first = max(0, math.floor(self.horizontalScrollBar().value() / self.line_width))
        last = min(len(self.index.lines), math.ceil((self.horizontalScrollBar().value() + self.viewport().width() - self.LEFT) / self.line_width))
        painter.save()
        painter.setClipRect(QRectF(self.LEFT, self._script_top(), self.viewport().width() - self.LEFT, self._script_height()))
        step = max(1, math.ceil(52 / self.line_width))
        for highlight_first, highlight_last in self._highlight_ranges:
            if highlight_last < first or highlight_first >= last:
                continue
            highlighted = self.line_rect(highlight_first)
            highlighted.setWidth((highlight_last - highlight_first + 1) * self.line_width)
            painter.fillRect(highlighted, QColor('#d9eafb'))
        draw_step = max(1, math.ceil(4 / self.line_width))
        script_font = QFont(self.font())
        script_font.setPointSizeF(max(9., script_font.pointSizeF() - 1))
        painter.setFont(script_font)
        for number in range((first // draw_step) * draw_step, last, draw_step):
            line = self.index.lines[number]
            rect = self.line_rect(number)
            if line['id'] == self.selected_line_id:
                painter.fillRect(QRectF(rect.left(), rect.top(), rect.width(), 3), QColor('#1e70b3'))
            painter.setPen(QColor('#dce5ef'))
            painter.drawLine(QPointF(rect.left(), rect.top()), QPointF(rect.left(), rect.bottom()))
            if number % step == 0:
                painter.setPen(QColor('#38658e'))
                painter.drawText(QRectF(rect.left() + 6, rect.top() + 3, max(48, rect.width() - 11), 23), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, str(number + 1))
            if not self.compact_ruler and rect.width() >= 42 and rect.height() >= 52:
                text = (line['speaker'] + '：' if line['speaker'] else '') + line['text']
                painter.setPen(QColor('#263f58'))
                painter.drawText(rect.adjusted(6, 29, -5, -5), Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap, text)
        painter.restore()
        painter.fillRect(QRectF(0, 0, self.LEFT, self.viewport().height()), QColor('#f0f4f8'))
        painter.setPen(QColor('#516b83'))
        painter.drawText(QRectF(0, self._script_top(), self.LEFT - 4, self._script_height()), Qt.AlignmentFlag.AlignCenter, '台本')
        painter.drawText(QRectF(0, self.HEADER, self.LEFT - 4, max(1, self._script_top() - self.HEADER)), Qt.AlignmentFlag.AlignCenter, 'PGM\n素材')
        painter.fillRect(QRectF(self.LEFT, 0, self.viewport().width() - self.LEFT, self.HEADER), QColor('#eef3f8'))
        painter.drawText(QRectF(self.LEFT + 9, 0, self.viewport().width() - self.LEFT - 18, self.HEADER), Qt.AlignmentFlag.AlignVCenter,
                         '蓝色：待人工核实 · 绿色：已人工核实 · 灰边：基准' if self.index.lines else '导入台本并完成 PGM 匹配后显示素材区间')

    @staticmethod
    def take_colors(take, selected=False):
        # Automated matching/confirmation is independent of human review.
        fill = ('#26754e' if selected else '#3e9b6a') if take.reviewed else ('#2875ba' if selected else '#4b92d2')
        border = '#687887' if take.reference else ('#145337' if selected else '#2c7b52') if take.reviewed else ('#0a4c88' if selected else '#6c9fca')
        return fill, border

    def _emit_ruler_selection(self, identities, line_number):
        identities = list(identities)
        if not identities or not self.index.line_ids:
            return
        self._emitting_ruler_selection = True
        try:
            self.selected_line_id = self.index.line_ids[line_number]
            self.line_selected.emit(self.selected_line_id)
            # A caller may navigate its full script in the line callback. Apply
            # and emit the complete range last so it cannot degrade to one line.
            self.set_highlighted_line_ids(identities)
            self.coverage_highlighted.emit(identities)
        finally:
            self._emitting_ruler_selection = False

    def _begin_ruler_drag(self, number, point, modifiers):
        snapshot = dict(candidate_id=self.selected_candidate_id, line_id=self.selected_line_id,
                        highlighted=set(self.highlighted_line_ids), anchor=self._range_anchor)
        extend = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        additive = bool(modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier))
        anchor = self._range_anchor if extend and self._range_anchor is not None else number
        base = set(self.highlighted_line_ids) if additive else set()
        self._range_anchor = anchor
        self.selected_candidate_id = ''
        self.selection_state_changed.emit(False)
        self._ruler_drag = dict(anchor=anchor, current=number, base=base, point=point,
                               snapshot=snapshot, identities=[], emitted_line=None)
        self._update_ruler_range(number)

    def _update_ruler_range(self, number):
        drag = self._ruler_drag
        if not drag or number is None:
            return
        drag['current'] = number
        selected = drag['base'].union(self.index.line_range(drag['anchor'], number))
        identities = sorted(selected, key=self.index.positions.__getitem__)
        if identities != drag['identities']:
            drag['identities'] = identities
            self._emit_ruler_selection(identities, number)
            drag['emitted_line'] = number
        self.viewport().update()

    def _autoscroll_ruler(self):
        drag = self._ruler_drag
        if not drag:
            self._range_scroll_timer.stop()
            return
        x = drag['point'].x()
        left, right = self.LEFT + 18, self.viewport().width() - 18
        distance = x - left if x < left else x - right if x > right else 0
        if not distance:
            self._range_scroll_timer.stop()
            return
        amount = math.copysign(min(96, max(8, math.ceil(abs(distance) * .4))), distance)
        bar = self.horizontalScrollBar()
        previous = bar.value()
        bar.setValue(previous + int(amount))
        if bar.value() != previous:
            self._update_ruler_range(self.line_at_x(x))

    def _cancel_ruler_drag(self, restore=True):
        drag = self._ruler_drag
        self._ruler_drag = None
        self._range_scroll_timer.stop()
        if not drag or not restore:
            return
        snapshot = drag['snapshot']
        self._range_anchor = snapshot['anchor']
        self.selected_candidate_id = snapshot['candidate_id'] if snapshot['candidate_id'] in self.index.by_candidate else ''
        self.selection_state_changed.emit(bool(self.selected_candidate_id))
        self.selected_line_id = snapshot['line_id']
        self.set_highlighted_line_ids(snapshot['highlighted'])
        self.coverage_highlighted.emit([identity for identity in self.index.line_ids if identity in snapshot['highlighted']])
        self.viewport().update()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        point = event.position()
        take = self.hit_take(point)
        self._press = (point, take.candidate_id if take else '')
        if take:
            self.select_take(take.candidate_id, ensure_visible=False)
            rect = self.take_rect(take.candidate_id)
            left_distance, right_distance = abs(point.x() - rect.left()), abs(point.x() - rect.right())
            if min(left_distance, right_distance) <= 7:
                self._drag = dict(candidate_id=take.candidate_id, edge='first' if left_distance <= right_distance else 'last',
                                  original_first=take.first, original_last=take.last, first=take.first, last=take.last,
                                  moved=False)
        elif point.x() >= self.LEFT and point.y() >= self._script_top():
            number = self.line_at_x(point.x())
            if number is not None:
                self._begin_ruler_drag(number, point, event.modifiers())
        event.accept()

    def mouseDoubleClickEvent(self, event):
        take = self.hit_take(event.position()) if event.button() == Qt.MouseButton.LeftButton else None
        if take:
            self.cancel_drag()
            self._press = None
            self.select_take(take.candidate_id, ensure_visible=False)
            self.take_double_clicked.emit(take.candidate_id)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):
        point = event.position()
        if self._ruler_drag:
            self._ruler_drag['point'] = point
            self._update_ruler_range(self.line_at_x(point.x()))
            if point.x() < self.LEFT + 18 or point.x() > self.viewport().width() - 18:
                self._range_scroll_timer.start()
            else:
                self._range_scroll_timer.stop()
            event.accept()
            return
        if self._drag:
            number = self.line_at_x(point.x())
            if number is not None:
                edge = self._drag['edge']
                self._drag[edge] = min(number, self._drag['last']) if edge == 'first' else max(number, self._drag['first'])
                self._drag['moved'] |= (point - self._press[0]).manhattanLength() > 3
                ids = self.index.line_range(self._drag['first'], self._drag['last'])
                self.set_highlighted_line_ids(ids)
                self.coverage_highlighted.emit(ids)
                self.viewport().update()
            event.accept()
            return
        take = self.hit_take(point)
        if take:
            rect = self.take_rect(take.candidate_id)
            edge = min(abs(point.x() - rect.left()), abs(point.x() - rect.right())) <= 7
            self.viewport().setCursor(Qt.CursorShape.SizeHorCursor if edge else Qt.CursorShape.PointingHandCursor)
            self.viewport().setToolTip(f'{take.filename}\n{take.session_name}\n台本 {take.first + 1}—{take.last + 1} · '+('已人工核实' if take.reviewed else '待人工核实')+'；拖动两端修改覆盖范围')
        else:
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
            number = self.line_at_x(point.x())
            self.viewport().setToolTip(f"{number + 1}  {self.index.lines[number]['text']}" if number is not None and point.y() >= self._script_top() else '')
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mouseReleaseEvent(event)
        if self._ruler_drag:
            drag = self._ruler_drag
            self._update_ruler_range(self.line_at_x(event.position().x()))
            self._ruler_drag = None
            self._range_scroll_timer.stop()
            self._press = None
            identities = drag['identities']
            if identities and self.index.positions[identities[0]] != drag['emitted_line']:
                self._emit_ruler_selection(identities, self.index.positions[identities[0]])
            event.accept()
            return
        drag, press = self._drag, self._press
        self._drag, self._press = None, None
        if drag and drag['moved']:
            # A drag previews only. Restore the committed range before asking
            # the owner to save, so cancellation, network failures and CAS
            # conflicts cannot leave an uncommitted blue range on either view.
            take = self.index.by_candidate.get(drag['candidate_id'])
            if take:
                self.select_take(take.candidate_id, ensure_visible=False)
                self.coverage_highlighted.emit(self.index.line_range(take.first, take.last))
            if (drag['first'], drag['last']) != (drag['original_first'], drag['original_last']):
                self.coverage_edited.emit(drag['candidate_id'], self.index.line_ids[drag['first']], self.index.line_ids[drag['last']])
            self.viewport().update()
        elif press and press[1]:
            take = self.hit_take(event.position())
            if take and take.candidate_id == press[1]:
                self.select_take(take.candidate_id, ensure_visible=False, emit=True)
        event.accept()

    def cancel_drag(self):
        if self._drag:
            candidate_id = self._drag['candidate_id']
            self._drag, self._press = None, None
            self.select_take(candidate_id, ensure_visible=False)
            self.coverage_highlighted.emit(list(self.highlighted_line_ids))
            self.viewport().update()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self._ruler_drag:
            self._cancel_ruler_drag()
            self._press = None
            event.accept()
            return
        if (event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace)
                and event.modifiers() == Qt.KeyboardModifier.NoModifier
                and self.selected_candidate_id in self.index.by_candidate):
            if not event.isAutoRepeat() and not self._remove_key_down:
                self._remove_key_down = True
                self.take_remove_requested.emit(self.selected_candidate_id)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape and self._drag:
            self.cancel_drag()
            event.accept()
            return
        if event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier) and event.key() in (Qt.Key.Key_Plus, Qt.Key.Key_Equal, Qt.Key.Key_Minus):
            self.set_line_width(self.line_width * (1.25 if event.key() != Qt.Key.Key_Minus else .8))
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if not event.isAutoRepeat():
                self._remove_key_down = False
            event.accept()
            return
        super().keyReleaseEvent(event)

    def focusOutEvent(self, event):
        self._remove_key_down = False
        super().focusOutEvent(event)

    def _scroll_axis_pixels(self, axis, movement):
        """Retain fractional high-resolution wheel movement between events."""
        if not movement:
            return
        bar = self.horizontalScrollBar() if axis == 'horizontal' else self.verticalScrollBar()
        pending = self._wheel_remainder[axis] - float(movement)
        if (pending < 0 and bar.value() <= bar.minimum() or
                pending > 0 and bar.value() >= bar.maximum()):
            self._wheel_remainder[axis] = 0.
            return
        # A small epsilon removes binary floating point drift after, for
        # example, five 0.4px events, without rounding away slow gestures.
        pixels = int(math.copysign(math.floor(abs(pending) + 1e-9), pending))
        self._wheel_remainder[axis] = pending - pixels
        if pixels:
            requested = bar.value() + pixels
            bar.setValue(requested)
            if bar.value() != requested:
                self._wheel_remainder[axis] = 0.

    def _handle_wheel(self, event, forced_axis=None):
        pixel, angle = event.pixelDelta(), event.angleDelta()
        is_pixel = not pixel.isNull()
        dx, dy = (float(pixel.x()), float(pixel.y())) if is_pixel else (float(angle.x()), float(angle.y()))
        if event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier):
            delta = dy if dy else dx
            if delta:
                # Proportional high-resolution zoom avoids a full zoom step
                # for every tiny trackpad packet and ignores phase-only events.
                exponent = max(-20., min(20., delta / 120.))
                anchor = event.position().x() if forced_axis is None else self.viewport().width() / 2
                self.set_line_width(self.line_width * math.exp(math.log(1.2) * exponent), anchor)
        else:
            horizontal_step = 1. if is_pixel else self.horizontalScrollBar().singleStep() / 120.
            vertical_step = 1. if is_pixel else self.LANE_HEIGHT / 120.
            if forced_axis == 'horizontal':
                self._scroll_axis_pixels('horizontal', (dx if dx else dy) * horizontal_step)
            elif forced_axis == 'vertical':
                self._scroll_axis_pixels('vertical', (dy if dy else dx) * vertical_step)
            else:
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    # A conventional mouse reports only Y. Preserve genuine
                    # horizontal motion while routing Shift+Y to the script.
                    dx += dy
                    dy = 0.
                self._scroll_axis_pixels('horizontal', dx * horizontal_step)
                self._scroll_axis_pixels('vertical', dy * vertical_step)
        event.accept()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Wheel:
            if watched is self.horizontalScrollBar():
                self._handle_wheel(event, 'horizontal')
                return True
            if watched is self.verticalScrollBar():
                self._handle_wheel(event, 'vertical')
                return True
        return super().eventFilter(watched, event)

    def wheelEvent(self, event):
        self._handle_wheel(event)


class HorizontalStoryTimeline(QWidget):
    """Self-contained horizontal story browser; persistence is the caller's job."""
    take_selected = pyqtSignal(str)
    take_double_clicked = pyqtSignal(str)
    take_remove_requested = pyqtSignal(str)
    interval_selected = pyqtSignal(str)
    line_selected = pyqtSignal(str)
    coverage_edited = pyqtSignal(str, str, str)
    coverage_highlighted = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel('PGM 区间时间轴'))
        toolbar.addStretch(1)
        self.focus_selection_button = QPushButton('定位所选')
        self.focus_selection_button.setEnabled(False)
        self.focus_selection_button.setToolTip('回到当前选中 PGM 的位置，不改变缩放和播放位置。')
        self.focus_selection_button.setAccessibleName('定位当前选中 PGM')
        toolbar.addWidget(self.focus_selection_button)
        toolbar.addWidget(QLabel('缩放'))
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setRange(0, 100)
        self.zoom_slider.setMaximumWidth(180)
        self.zoom_slider.setAccessibleName('PGM 区间缩放')
        self.zoom_slider.setToolTip('调整台本横向缩放；也可按住 Ctrl／Command 使用滚轮。')
        toolbar.addWidget(self.zoom_slider)
        self.fit_button = QPushButton('全览')
        self.fit_button.setAccessibleName('显示完整台本范围')
        self.fit_button.setToolTip('显示整个台本；重叠和短 PGM 保持独立可选轨道。')
        toolbar.addWidget(self.fit_button)
        layout.addLayout(toolbar)
        self.canvas = StoryTimelineCanvas(self)
        layout.addWidget(self.canvas, 1)
        self.canvas.take_selected.connect(self.take_selected)
        self.canvas.take_double_clicked.connect(self.take_double_clicked)
        self.canvas.take_remove_requested.connect(self.take_remove_requested)
        self.canvas.selection_state_changed.connect(self.focus_selection_button.setEnabled)
        self.focus_selection_button.clicked.connect(self.canvas.focus_selected_take)
        self.canvas.interval_selected.connect(self.interval_selected)
        self.canvas.line_selected.connect(self.line_selected)
        self.canvas.coverage_edited.connect(self.coverage_edited)
        self.canvas.coverage_highlighted.connect(self.coverage_highlighted)
        self.canvas.scale_changed.connect(self._scale_changed)
        self.zoom_slider.valueChanged.connect(lambda value: self.canvas.set_line_width(.05 * (7200 ** (value / 100.))))
        self.fit_button.clicked.connect(self.canvas.fit_all)
        self._scale_changed(self.canvas.line_width)

    def _scale_changed(self, value):
        blocked = self.zoom_slider.blockSignals(True)
        self.zoom_slider.setValue(round(100 * math.log(max(.05, value) / .05) / math.log(7200)))
        self.zoom_slider.blockSignals(blocked)

    @property
    def index(self):
        return self.canvas.index

    def set_nodes(self, script_nodes, pgm_nodes, reference_session_id=''):
        self.canvas.set_nodes(script_nodes, pgm_nodes, reference_session_id)

    def set_compact_ruler(self, compact=True):
        self.canvas.set_compact_ruler(compact)

    def select_take(self, candidate_id, ensure_visible=True, emit=False):
        self.canvas.select_take(candidate_id, ensure_visible, emit)

    def clear_selection(self, emit=False):
        self.canvas.clear_selection(emit)

    def focus_selected_take(self):
        return self.canvas.focus_selected_take()

    def select_line(self, line_id, ensure_visible=True, emit=False):
        self.canvas.select_line(line_id, ensure_visible, emit)

    def set_selected_interval(self, interval_id):
        self.canvas.set_selected_interval(interval_id)

    def set_highlighted_line_ids(self, identities):
        self.canvas.set_highlighted_line_ids(identities)
