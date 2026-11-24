"""A script-ordered visual axis drawn by one delegate, without per-row widgets."""
from __future__ import annotations

from collections import OrderedDict
import copy

from PyQt6.QtCore import Qt, QSize, QPointF, QRectF, QEvent, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen, QTextLayout, QTextOption
from PyQt6.QtWidgets import QAbstractItemView, QHeaderView, QStyle, QStyledItemDelegate, QTreeWidget, QTreeWidgetItem


NODE_ROLE = int(Qt.ItemDataRole.UserRole) + 1
HEADING_ROLE = int(Qt.ItemDataRole.UserRole) + 2
COVERED_ROLE = int(Qt.ItemDataRole.UserRole) + 3


def _number(node, field, alternate=None):
    try:
        return max(0, int(node.get(field, node.get(alternate, 0) if alternate else 0) or 0))
    except (TypeError, ValueError):
        return 0


def _summary(node):
    return (f"已核对 {_number(node, 'confirmed_count', 'confirmed')} · "
            f"待核对 {_number(node, 'candidate_count', 'candidate')} · "
            f"{_number(node, 'session_count', 'sessions')} 场"
            + (f" · {_number(node, 'camera_count')} 机位" if _number(node, 'angle_count') else ''))


def _badge_text(node):
    if 'pgm_count' in node:
        return f"{_number(node, 'pgm_count')} 条PGM"
    count, angles = _number(node, 'count'), _number(node, 'angle_count')
    return f'{count}段 · {angles}机位片段' if angles else f'{count} 条素材'


def _show_materials(node):
    """Dialogue stays a readable script; material statistics belong to PGM rows."""
    return 'pgm_count' in node


def _speaker_label(node):
    speaker = str(node.get('speaker') or '')
    number = node.get('script_row_number')
    return f'{number}  {speaker}'.rstrip() if number is not None and not _show_materials(node) else speaker


def _display_text(node):
    text = (str(node.get('speaker')) + '：' if node.get('speaker') else '') + str(node.get('text') or '')
    number = node.get('script_row_number')
    return f'{number}  {text}' if number is not None and not _show_materials(node) else text


class _WrappedText:
    def __init__(self, text, font, width):
        self.layout = QTextLayout(str(text), font)
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.layout.setTextOption(option)
        self.layout.beginLayout()
        self.height = 0.0
        while True:
            line = self.layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(max(1, width))
            line.setPosition(QPointF(0, self.height))
            self.height += line.height() + 2
        self.layout.endLayout()
        self.height = max(self.height, QFontMetricsF(font).height())

    def draw(self, painter, point):
        self.layout.draw(painter, point)


class ScriptTimelineDelegate(QStyledItemDelegate):
    """Native item rendering keeps even large scripts cheap to scroll."""
    AXIS_X = 15
    TEXT_X = 35
    RIGHT = 13

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cache = OrderedDict()

    def clear_cache(self):
        self._cache.clear()

    def _text(self, text, font, width):
        key = (str(text), font.toString(), round(width))
        value = self._cache.pop(key, None)
        if value is None:
            value = _WrappedText(text, font, width)
        self._cache[key] = value
        # This cache holds text layouts, never QWidget instances.
        while len(self._cache) > 1024:
            self._cache.popitem(last=False)
        return value

    def _width(self, option):
        parent = self.parent()
        available = parent.viewport().width() if isinstance(parent, QTreeWidget) else option.rect.width()
        return max(120, available - self.TEXT_X - self.RIGHT)

    @staticmethod
    def _fonts(option):
        body = QFont(option.font)
        body.setPointSizeF(max(11.0, body.pointSizeF()))
        speaker = QFont(body)
        speaker.setPointSizeF(max(10.0, body.pointSizeF() - 1))
        speaker.setBold(True)
        small = QFont(body)
        small.setPointSizeF(max(9.0, body.pointSizeF() - 2))
        badge = QFont(small)
        badge.setBold(True)
        heading = QFont(body)
        heading.setBold(True)
        return body, speaker, small, badge, heading

    def _geometry(self, option, index):
        width = self._width(option)
        body, speaker, small, badge, heading = self._fonts(option)
        if index.data(HEADING_ROLE):
            text = self._text(index.data(Qt.ItemDataRole.DisplayRole) or '', heading, width)
            return {'height': int(text.height + 20), 'heading': text, 'fonts': (body, speaker, small, badge, heading)}
        node = index.data(NODE_ROLE) or {}
        words = self._text(node.get('text', ''), body, width)
        speaker_label = _speaker_label(node)
        name = self._text(speaker_label, speaker, width) if speaker_label else None
        if not _show_materials(node):
            return dict(height=int(12 + (name.height + 5 if name else 0) + words.height + 16),
                        words=words, name=name, show_materials=False,
                        fonts=(body, speaker, small, badge, heading))
        badge_text = _badge_text(node)
        badge_width = QFontMetricsF(badge).horizontalAdvance(badge_text) + 18
        summary_width = QFontMetricsF(small).horizontalAdvance(_summary(node))
        stats_below = badge_width + summary_width + 12 > width
        label_height = max(23, QFontMetricsF(badge).height() + 8)
        height = 12 + (name.height + 5 if name else 0) + words.height + 12 + label_height + 14
        if stats_below:
            height += QFontMetricsF(small).height() + 6
        return dict(height=int(height), words=words, name=name, badge_text=badge_text,
                    badge_width=badge_width, stats_below=stats_below, label_height=label_height,
                    show_materials=True,
                    fonts=(body, speaker, small, badge, heading))

    def sizeHint(self, option, index):
        return QSize(max(120, option.rect.width()), self._geometry(option, index)['height'])

    def paint(self, painter, option, index):
        painter.save()
        painter.setClipRect(option.rect)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(option.rect)
        geometry = self._geometry(option, index)
        body, speaker, small, badge, heading = geometry['fonts']
        x = rect.left()
        axis_x = x + self.AXIS_X
        text_x = x + self.TEXT_X
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        show_axis = _show_materials(index.data(NODE_ROLE) or {})
        if show_axis:
            painter.setPen(QPen(QColor('#dce3ea'), 2))
            painter.drawLine(QPointF(axis_x, rect.top()), QPointF(axis_x, rect.bottom()))
        if index.data(HEADING_ROLE):
            painter.fillRect(QRectF(text_x - 7, rect.top() + 3, rect.width() - self.TEXT_X + 2, rect.height() - 6), QColor('#eef2f6'))
            painter.setPen(QColor('#33465b'))
            geometry['heading'].draw(painter, QPointF(text_x, rect.top() + 10))
            painter.restore()
            return
        node = index.data(NODE_ROLE) or {}
        count = _number(node, 'count')
        show_materials = geometry['show_materials']
        empty = show_materials and count == 0
        color = '#a9b3bf' if empty else '#329873' if not _number(node, 'candidate_count', 'candidate') else '#4985bd'
        if not show_materials:
            color = '#a3b2c2'
        if selected or index.data(COVERED_ROLE):
            painter.setBrush(QColor('#cce3fb' if index.data(COVERED_ROLE) else '#e9f2fc'))
            painter.setPen(QPen(QColor('#a7c5e7'), 1))
            painter.drawRoundedRect(QRectF(text_x - 8, rect.top() + 3, rect.width() - self.TEXT_X - 2, rect.height() - 6), 7, 7)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.setBrush(QColor('#f5f8fb'))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(QRectF(text_x - 8, rect.top() + 3, rect.width() - self.TEXT_X - 2, rect.height() - 6), 7, 7)
        center = QPointF(axis_x, rect.top() + 22)
        if show_materials:
            painter.setBrush(QColor('#ffffff'))
            painter.setPen(QPen(QColor(color), 1.5))
            if count > 1 or _number(node, 'angle_count') > 1:
                painter.drawEllipse(center, 8, 8)
            painter.setBrush(QColor(color))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(center, 4.5, 4.5)
        y = rect.top() + 12
        if geometry['name']:
            painter.setPen(QColor('#8995a2' if empty else '#536a80'))
            geometry['name'].draw(painter, QPointF(text_x, y))
            y += geometry['name'].height + 5
        painter.setPen(QColor('#8e99a6' if empty else '#24384c'))
        geometry['words'].draw(painter, QPointF(text_x, y))
        y += geometry['words'].height + 12
        if show_materials:
            badge_rect = QRectF(text_x, y, geometry['badge_width'], geometry['label_height'])
            painter.setBrush(QColor('#edf0f3' if empty else '#dfeef9'))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(badge_rect, 5, 5)
            painter.setFont(badge)
            painter.setPen(QColor('#8c97a3' if empty else '#27618f'))
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, geometry['badge_text'])
            if geometry['stats_below']:
                stats = QRectF(text_x, y + geometry['label_height'] + 5, self._width(option), QFontMetricsF(small).height() + 2)
            else:
                stats = QRectF(badge_rect.right() + 12, y, self._width(option) - geometry['badge_width'] - 12, geometry['label_height'])
            painter.setFont(small)
            painter.setPen(QColor('#97a1ac' if empty else '#667b8e'))
            painter.drawText(stats, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, _summary(node))
        if option.state & QStyle.StateFlag.State_HasFocus:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor('#7da5ce'), 1, Qt.PenStyle.DotLine))
            painter.drawRoundedRect(QRectF(text_x - 8, rect.top() + 3, rect.width() - self.TEXT_X - 2, rect.height() - 6), 7, 7)
        painter.restore()


class ScriptTimeline(QTreeWidget):
    """Ordered complete script text; PGM-specific rows may show material counts."""
    node_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._nodes = []
        self._items = {}
        self._highlighted_line_ids = set()
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(10)
        self._resize_timer.timeout.connect(self._restore_reading_position)
        self._query, self._mode = '', 'all'
        self.setColumnCount(1)
        self.setHeaderHidden(True)
        self.setRootIsDecorated(False)
        self.setIndentation(0)
        self.setSortingEnabled(False)
        self.setUniformRowHeights(False)
        self.setWordWrap(True)
        self.setExpandsOnDoubleClick(False)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.header().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.setItemDelegate(ScriptTimelineDelegate(self))
        self.setStyleSheet('QTreeWidget { background: #ffffff; border: 1px solid #dce3ea; border-radius: 7px; }')
        self.currentItemChanged.connect(self._selected)
        self.setAccessibleName('台本剧情轴')

    def sizeHint(self):
        return QSize(430, 700)

    def current_line_id(self):
        item = self.currentItem()
        return str(item.data(0, Qt.ItemDataRole.UserRole) or '') if item else ''

    def _selected(self, item, previous=None):
        if item is None:
            self.node_selected.emit('')
        else:
            line_id = item.data(0, Qt.ItemDataRole.UserRole)
            if line_id:
                self.node_selected.emit(str(line_id))

    def set_nodes(self, nodes, selected_id=None):
        desired = selected_id or self.current_line_id()
        groups = OrderedDict()
        for index, raw in enumerate(nodes):
            # Painting needs summary fields only. Copying all camera ranges
            # into both Python nodes and QVariant items can dwarf the script.
            node = {key: copy.deepcopy(value) for key, value in raw.items() if key != 'clips'}
            line_id = node.get('script_line_id') or node.get('id') or node.get('line_id')
            if not line_id:
                raise ValueError('台本节点缺少唯一 ID。')
            node['id'] = str(line_id)
            try:
                order = float(node.get('order', index))
            except (ValueError, TypeError):
                order = index
            groups.setdefault(node.get('script_id', ''), []).append((order, index, node))
        ordered = [row[2] for group in groups.values() for row in sorted(group, key=lambda row: (row[0], row[1]))]
        if len({row['id'] for row in ordered}) != len(ordered):
            raise ValueError('台本节点 ID 重复。')
        for number, node in enumerate(ordered, 1):
            if not _show_materials(node):
                node['script_row_number'] = number
        previous = self.blockSignals(True)
        self.setUpdatesEnabled(False)
        try:
            self.clear()
            self._nodes, self._items = ordered, {}
            current_group, parent = None, None
            for node in ordered:
                scene = node.get('scene') or '未分幕场'
                key = node.get('script_id', ''), scene
                if key != current_group:
                    parent = QTreeWidgetItem(self, [str(scene)])
                    parent.setData(0, HEADING_ROLE, True)
                    parent.setFlags(parent.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                    parent.setToolTip(0, str(scene))
                    parent.setExpanded(True)
                    current_group = key
                text = _display_text(node)
                summary = f"{_badge_text(node)} · {_summary(node)}" if _show_materials(node) else ''
                item = QTreeWidgetItem(parent, [text + ('\n' + summary if summary else '')])
                item.setData(0, Qt.ItemDataRole.UserRole, node['id'])
                item.setData(0, NODE_ROLE, node)
                item.setData(0, COVERED_ROLE, node['id'] in self._highlighted_line_ids)
                item.setToolTip(0, str(scene) + '\n' + text + ('\n' + summary if summary else ''))
                self._items[node['id']] = item
            self.itemDelegate().clear_cache()
        finally:
            self.setUpdatesEnabled(True)
            self.blockSignals(previous)
        self.filter_nodes(self._query, self._mode, selected_id=desired)
        self.scheduleDelayedItemsLayout()

    def set_data(self, nodes, selected_line_id=None):
        self.set_nodes(nodes, selected_line_id)

    def set_highlighted_line_ids(self, identities):
        current = {str(value) for value in identities if str(value) in self._items}
        changed = current.symmetric_difference(self._highlighted_line_ids)
        self._highlighted_line_ids = current
        if not changed:
            return
        blocked = self.blockSignals(True)
        model = self.model()
        model_blocked = model.blockSignals(True)
        try:
            for identity in changed:
                item = self._items.get(identity)
                if item:
                    item.setData(0, COVERED_ROLE, identity in current)
        finally:
            model.blockSignals(model_blocked)
            self.blockSignals(blocked)
        # COVERED_ROLE affects paint only. Its dataChanged notifications and
        # per-item rectangle queries otherwise force redundant tree layouts.
        self.viewport().update()

    def update_nodes(self, nodes):
        """Refresh model data without recreating the script or selection."""
        summaries = [{key: value for key, value in row.items() if key != 'clips'} for row in nodes]
        for number, node in enumerate(summaries, 1):
            if not _show_materials(node):
                node['script_row_number'] = number
        if [str(row['id']) for row in summaries] != [row['id'] for row in self._nodes]:
            self.set_nodes(nodes)
            return
        if any(any(old.get(key) != new.get(key) for key in ('scene', 'script_id', 'order'))
               for old, new in zip(self._nodes, summaries)):
            self.set_nodes(nodes)
            return
        previous = self.blockSignals(True)
        self.setUpdatesEnabled(False)
        try:
            for old, new in zip(self._nodes, summaries):
                if old == new:
                    continue
                new = copy.deepcopy(new)
                new['id'] = str(new['id'])
                item = self._items[new['id']]
                text = _display_text(new)
                summary = f'{_badge_text(new)} · {_summary(new)}' if _show_materials(new) else ''
                item.setText(0, text + ('\n' + summary if summary else ''))
                item.setData(0, NODE_ROLE, new)
                item.setToolTip(0, str(new.get('scene') or '未分幕场') + '\n' + text + ('\n' + summary if summary else ''))
                old.clear()
                old.update(new)
        finally:
            self.setUpdatesEnabled(True)
            self.blockSignals(previous)
        self.filter_nodes(self._query, self._mode)

    def filter_nodes(self, query='', mode='all', selected_id=None):
        if mode not in ('all', 'multiple', 'review', 'empty'):
            raise ValueError('未知台本筛选方式：' + str(mode))
        self._query, self._mode = str(query or '').strip().casefold(), mode
        first = None
        for index in range(self.topLevelItemCount()):
            heading = self.topLevelItem(index)
            visible = False
            for number in range(heading.childCount()):
                item = heading.child(number)
                node = item.data(0, NODE_ROLE) or {}
                count = _number(node, 'count')
                condition = (mode == 'all' or mode == 'multiple' and (count > 1 or _number(node, 'angle_count') > 1) or
                             mode == 'review' and _number(node, 'candidate_count', 'candidate') > 0 or
                             mode == 'empty' and count == 0)
                haystack = ' '.join(str(node.get(field, '')) for field in ('scene', 'speaker', 'text'))
                haystack += ' ' + ' '.join(str(value) for value in node.get('session_names', []))
                show = condition and (not self._query or self._query in haystack.casefold())
                item.setHidden(not show)
                visible |= show
                if show and first is None:
                    first = item
            heading.setHidden(not visible)
            heading.setExpanded(True)
        desired = self._items.get(str(selected_id or self.current_line_id()))
        selected = desired if desired and not desired.isHidden() and not desired.parent().isHidden() else first
        if self.currentItem() is not selected:
            self.setCurrentItem(selected)
        if selected:
            self.scrollToItem(selected, QAbstractItemView.ScrollHint.EnsureVisible)
        else:
            self.clearSelection()
        self.viewport().update()

    def set_filter(self, status='all', query=''):
        self.filter_nodes(query, status)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.scheduleDelayedItemsLayout()
        if hasattr(self, '_resize_timer'):
            self._resize_timer.start()

    def _restore_reading_position(self):
        item = self.currentItem()
        if self.isVisible() and item and not item.isHidden():
            self.doItemsLayout()
            self.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtTop)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.ApplicationFontChange, QEvent.Type.StyleChange):
            delegate = self.itemDelegate()
            if isinstance(delegate, ScriptTimelineDelegate):
                delegate.clear_cache()
            self.scheduleDelayedItemsLayout()
