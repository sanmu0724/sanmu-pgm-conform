"""A light, read-only overview of alternative PGM coverage over the full script.

Positions are derived from script order and text length, never recording clocks.
The widget paints merged bands in one pass and does not create a widget per line
or take. Media identity and source metadata stay in the caller's original data.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import Qt, QRectF, QSize, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QWidget


@dataclass(frozen=True)
class CoverageSpan:
    interval_id: str
    start: float
    end: float
    first_line_id: str
    last_line_id: str
    filenames: tuple[str, ...]


class ScriptCoverageIndex:
    """Compact interval lookup with O(log n + overlap) hit testing."""
    def __init__(self, script_nodes=(), pgm_nodes=(), reference_session_id=''):
        # ScriptTimeline keeps script families in insertion order and orders
        # within each family. Mirror that contract, even for unsorted input.
        groups = {}
        for number, row in enumerate(script_nodes):
            line_id = str(row.get('script_line_id') or row.get('id') or row.get('line_id') or '')
            if not line_id:
                continue
            try:
                order = float(row.get('order', number))
            except (TypeError, ValueError):
                order = number
            groups.setdefault(row.get('script_id', ''), []).append((order, number, line_id, row))
        ordered = [entry for rows in groups.values() for entry in sorted(rows, key=lambda entry: entry[:2])]
        self.line_ids = [entry[2] for entry in ordered]
        self.positions = {line_id: number for number, line_id in enumerate(self.line_ids)}
        weights = [max(1, len(str(entry[3].get('text') or ''))) for entry in ordered]
        total = sum(weights) or 1
        self.edges = [0.0]
        accumulated = 0
        self.scene_edges = []
        previous_scene = None
        for entry, weight in zip(ordered, weights):
            scene = entry[3].get('script_id', ''), entry[3].get('scene', '')
            if scene != previous_scene and accumulated:
                self.scene_edges.append(accumulated / total)
            previous_scene = scene
            accumulated += weight
            self.edges.append(accumulated / total)
        spans = []
        for node in pgm_nodes:
            interval_id = str(node.get('id') or '')
            if not interval_id:
                continue
            statuses = {(str(row.get('session_id') or ''), str(row.get('media_path') or '')): row
                        for row in node.get('file_statuses', [])}
            by_range = {}
            for clip in node.get('clips', []):
                session_id = str(clip.get('session_id') or '')
                if reference_session_id and session_id == str(reference_session_id):
                    continue
                if clip.get('status') == 'rejected':
                    continue
                status = statuses.get((session_id, str(clip.get('media_path') or '')), {})
                match_status = clip.get('pgm_match_status') or status.get('status')
                # Current file statuses are authoritative. Legacy callers that
                # omit them can still show explicitly confirmed associations.
                if match_status != 'matched' and not (match_status is None and clip.get('status') == 'confirmed'):
                    continue
                ids = clip.get('covered_line_ids')
                if ids is None:
                    ids = node.get('covered_line_ids', [])
                numbers = [self.positions[str(line_id)] for line_id in ids if str(line_id) in self.positions]
                if not numbers:
                    continue
                first, last = min(numbers), max(numbers)
                filename = Path(str(clip.get('media_path') or '')).name
                by_range.setdefault((first, last), []).append(filename)
            for (first, last), names in by_range.items():
                spans.append(CoverageSpan(interval_id, self.edges[first], self.edges[last + 1],
                                          self.line_ids[first], self.line_ids[last], tuple(dict.fromkeys(names))))
        self.spans = sorted(spans, key=lambda row: (row.start, row.end, row.interval_id))
        self.starts = [row.start for row in self.spans]
        self.prefix_end = []
        largest = 0.0
        self.by_interval = {}
        for row in self.spans:
            largest = max(largest, row.end)
            self.prefix_end.append(largest)
            self.by_interval.setdefault(row.interval_id, []).append(row)
        self.bands = []
        for row in self.spans:
            if self.bands and row.start <= self.bands[-1][1]:
                self.bands[-1] = self.bands[-1][0], max(self.bands[-1][1], row.end)
            else:
                self.bands.append((row.start, row.end))

    def line_at(self, position):
        if not self.line_ids:
            return ''
        number = min(len(self.line_ids) - 1, max(0, bisect_right(self.edges, position) - 1))
        return self.line_ids[number]

    def line_range(self, first_line_id, last_line_id=None):
        first = self.positions.get(str(first_line_id))
        last = self.positions.get(str(last_line_id or first_line_id))
        if first is None or last is None:
            return None
        first, last = min(first, last), max(first, last)
        return self.edges[first], self.edges[last + 1]

    def hits(self, position, tolerance=0.0):
        number = bisect_right(self.starts, position + tolerance) - 1
        hits = []
        while number >= 0 and self.prefix_end[number] >= position - tolerance:
            row = self.spans[number]
            if row.start <= position + tolerance and row.end >= position - tolerance:
                hits.append(row)
            number -= 1
        return sorted(hits, key=lambda row: (row.end - row.start, row.interval_id, row.start))


class ScriptCoverageAxis(QWidget):
    """White full-script rail, blue valid alternatives, and current-view outline."""
    interval_clicked = pyqtSignal(str)
    line_requested = pyqtSignal(str)
    scroll_requested = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.index = ScriptCoverageIndex()
        self._selected_interval = ''
        self._viewport_range = None
        self._cycle_key = None
        self._cycle_number = 0
        self.setMinimumWidth(30)
        self.setMaximumWidth(52)
        self.setMinimumHeight(80)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName('PGM备用素材台本预览轴')
        self.setAccessibleDescription('蓝色为有备用PGM的台本区间；单击定位，滚轮滚动台本。')

    def sizeHint(self):
        return QSize(40, 600)

    def set_nodes(self, script_nodes, pgm_nodes, reference_session_id=''):
        self.index = ScriptCoverageIndex(script_nodes, pgm_nodes, reference_session_id)
        self._cycle_key = None
        self._cycle_number = 0
        if self._selected_interval not in self.index.by_interval:
            self._selected_interval = ''
        self.update()

    def set_selected_interval(self, interval_id):
        interval_id = str(interval_id or '')
        if interval_id != self._selected_interval:
            self._selected_interval = interval_id
            self.update()

    def set_viewport_range(self, first_line_id, last_line_id=None):
        value = self.index.line_range(first_line_id, last_line_id)
        if value != self._viewport_range:
            self._viewport_range = value
            self.update()

    def _rail(self):
        return QRectF(7, 5, max(1, self.width() - 14), max(1, self.height() - 10))

    def _band_rect(self, start, end):
        rail = self._rail()
        top = rail.top() + start * rail.height()
        height = max(3.0, (end - start) * rail.height())
        return QRectF(rail.left() + 1, min(top, rail.bottom() - height), rail.width() - 2, height)

    def _position(self, y):
        rail = self._rail()
        return min(1.0, max(0.0, (float(y) - rail.top()) / rail.height()))

    def _hits(self, y):
        return self.index.hits(self._position(y), tolerance=1.5 / self._rail().height())

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rail = self._rail()
        painter.setBrush(QColor('#ffffff'))
        painter.setPen(QPen(QColor('#cbd8e6'), 1))
        painter.drawRoundedRect(rail, 4, 4)
        painter.setClipRect(rail.adjusted(1, 1, -1, -1))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#458bda'))
        for start, end in self.index.bands:
            painter.drawRect(self._band_rect(start, end))
        painter.setPen(QPen(QColor('#cbd8e6'), 1))
        for edge in self.index.scene_edges:
            y = rail.top() + edge * rail.height()
            painter.drawLine(int(rail.left()), int(y), int(rail.right()), int(y))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor('#173e70'), 2))
        for span in self.index.by_interval.get(self._selected_interval, []):
            painter.drawRect(self._band_rect(span.start, span.end).adjusted(1, 0, -1, 0))
        if self._viewport_range:
            painter.setBrush(QColor(35, 65, 100, 20))
            painter.setPen(QPen(QColor('#5d7188'), 1))
            painter.drawRect(self._band_rect(*self._viewport_range).adjusted(0, 0, 0, -1))

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        hits = self._hits(event.position().y())
        line_id = self.index.line_at(self._position(event.position().y()))
        if line_id:
            self.line_requested.emit(line_id)
        if hits:
            unique = list(dict.fromkeys(row.interval_id for row in hits))
            cycle_key = (tuple(unique), line_id)
            self._cycle_number = (self._cycle_number + 1) % len(unique) if cycle_key == self._cycle_key else 0
            self._cycle_key = cycle_key
            interval_id = unique[self._cycle_number]
            self.set_selected_interval(interval_id)
            self.interval_clicked.emit(interval_id)
        else:
            self._cycle_key = None
        event.accept()

    def mouseMoveEvent(self, event):
        hits = self._hits(event.position().y())
        if hits:
            filenames = list(dict.fromkeys(name for row in hits for name in row.filenames))
            self.setToolTip('备用PGM：' + str(len(filenames)) + ' 条\n' + '\n'.join(filenames[:8]) +
                            ('\n单击可轮换重叠区间。' if len({row.interval_id for row in hits}) > 1 else ''))
        else:
            self.setToolTip('单击定位台本；滚轮滚动浏览。')
        super().mouseMoveEvent(event)

    def wheelEvent(self, event):
        delta = -event.pixelDelta().y() if event.pixelDelta().y() else -event.angleDelta().y()
        if delta:
            self.scroll_requested.emit(int(delta))
        event.accept()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            direction = -1 if event.key() == Qt.Key.Key_PageUp else 1
            self.scroll_requested.emit(direction * max(80, int(self.height() * .8)))
            event.accept()
        elif event.key() in (Qt.Key.Key_Home, Qt.Key.Key_End) and self.index.line_ids:
            self.line_requested.emit(self.index.line_ids[0 if event.key() == Qt.Key.Key_Home else -1])
            event.accept()
        else:
            super().keyPressEvent(event)
