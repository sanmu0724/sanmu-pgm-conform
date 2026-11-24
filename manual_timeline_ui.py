"""Virtual manual PGM organiser; every drag changes placement only.

Painting and pointer motion use cached metadata. No file probing, recognition,
media decoding, or source-clock transformation occurs on an editing gesture.
"""
from __future__ import annotations
import bisect
import copy
import json
import math
import re
from pathlib import Path

from PyQt6 import sip
from PyQt6.QtCore import QAbstractListModel, QMimeData, QModelIndex, QPoint, QRect, QRectF, Qt, pyqtSignal, QEvent, QTimer
from PyQt6.QtGui import QColor, QKeySequence, QPainter, QPen, QShortcut
from PyQt6.QtWidgets import (QAbstractItemView, QAbstractScrollArea, QApplication, QComboBox,
    QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListView, QMessageBox, QPushButton, QSlider, QSpinBox, QSplitter, QVBoxLayout, QWidget,
    QTextEdit, QPlainTextEdit, QAbstractSpinBox, QToolTip, QProgressBar, QCheckBox, QSizePolicy)

MIME = 'application/x-sanmu-pgm-takes'


def _identity(row):
    return str(row.get('take_id') or row.get('id'))


def _duration(row):
    try:
        value = float(row.get('duration_seconds') or 0)
        return value if math.isfinite(value) and value > 0 else 0.
    except (ValueError, TypeError, OverflowError):
        return 0.


def _script_bounds(item):
    first, last = item.get('organize_first_line'), item.get('organize_last_line')
    if isinstance(first, int) and not isinstance(first, bool) and isinstance(last, int) and not isinstance(last, bool) and 1 <= first <= last:
        return first, last
    return None


def parse_script_range(text, count=0):
    match = re.fullmatch(r'\s*(?:台本\s*)?(\d+)\s*(?:[-—–~～至]\s*(\d+))?\s*', text)
    if not match:
        raise ValueError('请输入台本编号或区间，例如 55 或 55-76。')
    first, last = int(match[1]), int(match[2] or match[1])
    if first < 1 or last < first or count and last > count:
        raise ValueError('台本区间超出范围，或结束编号早于开始编号。')
    return first, last


class TakePoolModel(QAbstractListModel):
    """One small row per PGM, never a widget per proxy, WAV, or transcript."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows, self.by_id = [], {}

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.by_id = {_identity(row): index for index, row in enumerate(rows)}
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        row = self.rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            duration = _duration(row)
            prefix='基准 · ' if row.get('session_id')==getattr(self,'baseline_id',None) else ''
            bounds = _script_bounds(row)
            label = (f'台本 {bounds[0]}-{bounds[1]}' if bounds else '尚未指定台本区间') if getattr(self, 'mode', '') == 'organize' else f'{duration:.1f} 秒' if duration else '时长信息待补齐'
            return prefix+(row.get('filename') or Path(row.get('pgm_path', '')).name or row.get('name', 'PGM')) + \
                f'\n{row.get("session_name", "")} · {label}'
        if role == Qt.ItemDataRole.UserRole:
            return _identity(row)
        if role == Qt.ItemDataRole.ToolTipRole:
            return row.get('pgm_path', '') + ''.join('\n'+str(row[key]) for key in ('metadata_warning','alignment_reason') if row.get(key))

    def flags(self, index):
        locked=index.isValid() and getattr(self,'mode','timeline')=='timeline' and self.rows[index.row()].get('session_id')==getattr(self,'baseline_id',None)
        return super().flags(index) | (Qt.ItemFlag.ItemIsDragEnabled if index.isValid() and not locked else Qt.ItemFlag.NoItemFlags)

    def mimeTypes(self):
        return [MIME]

    def mimeData(self, indexes):
        value = QMimeData()
        value.setData(MIME, json.dumps([_identity(self.rows[index.row()]) for index in indexes if index.isValid()]).encode())
        return value

    def supportedDragActions(self):
        return Qt.DropAction.CopyAction


class ManualTimelineCanvas(QAbstractScrollArea):
    selection_changed = pyqtSignal(object)
    layout_changed = pyqtSignal(object)
    message = pyqtSignal(str)
    dropped = pyqtSignal(object, float, int)
    time_clicked = pyqtSignal(float)
    HEADER, GUTTER, TRACK = 34, 112, 62

    def __init__(self, inventory, layout, baseline_id=None, baseline_cut=None, parent=None):
        super().__init__(parent)
        self.inventory = {_identity(row): row for row in inventory}
        self.layout_data, self.baseline_id, self.baseline_cut = layout, baseline_id, baseline_cut or {}
        self.mode, self.scale, self.origin_seconds = 'organize', 8., 1.
        self.script_count = 0
        self.selection = set()
        self._lanes, self._starts, self._entries = {}, {}, {}
        self._press = self._rubber = self._ghost = None
        self._drag_ids = []
        self._lane_drag = None
        self.selection_position = None
        self.playhead_seconds = 0.
        self._press_scroll = (0,0)
        self._mode_scrolls = {}
        self.track_count, self.end_seconds = 4, 60.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setMinimumHeight(220)
        self.horizontalScrollBar().valueChanged.connect(self.viewport().update)
        self.verticalScrollBar().valueChanged.connect(self.viewport().update)
        self.rebuild()

    def set_mode(self, mode):
        self._mode_scrolls[self.mode]=(self.horizontalScrollBar().value(),self.verticalScrollBar().value())
        self.mode = mode
        self.rebuild()
        if mode in self._mode_scrolls:
            horizontal,vertical=self._mode_scrolls[mode]
            self.horizontalScrollBar().setValue(horizontal);self.verticalScrollBar().setValue(vertical)
        elif mode == 'timeline':
            self.horizontalScrollBar().setValue(0)
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
        else:
            self.horizontalScrollBar().setValue(0);self.verticalScrollBar().setValue(0)

    def set_script_count(self, count):
        self.script_count = max(0, int(count))
        self.rebuild()

    def set_layout(self, layout):
        self.layout_data = layout
        self.selection.intersection_update(self.inventory)
        self.rebuild()

    def rebuild(self):
        self._lanes, self._entries = {}, {}
        lane_key = 'lane' if self.mode == 'timeline' else 'organize_lane'
        for identity, item in self.layout_data.get('items', {}).items():
            row = self.inventory.get(identity)
            if not row or self.mode == 'timeline' and item.get('hidden'):
                continue
            baseline = row.get('session_id') == self.baseline_id
            if self.mode == 'timeline':
                position = item.get('start_seconds')
                if baseline or position is None or _duration(row) <= 0:
                    continue
                end = float(position) + _duration(row)
            else:
                bounds = _script_bounds(item)
                if item.get('organize_pool') != 'track' or not bounds:
                    continue
                position, last = bounds
                end = last + 1
            lane = max(1, int(item.get(lane_key, 1)))
            entry = dict(id=identity, start=float(position), end=float(end), lane=lane,
                         baseline=baseline, reviewed=bool(item.get('alignment_reviewed') if self.mode=='timeline' else item.get('organize_reviewed')), row=row)
            self._entries[identity] = entry
            self._lanes.setdefault(lane, []).append(entry)
        if self.mode == 'timeline' and self.baseline_id:
            for identity, row in self.inventory.items():
                if row.get('session_id') != self.baseline_id or _duration(row) <= 0:
                    continue
                start = row.get('baseline_start_seconds')
                if start is None:
                    continue
                entry = dict(id=identity, start=float(start), end=float(start)+_duration(row), lane=0, baseline=True, row=row)
                self._entries[identity] = entry
                self._lanes.setdefault(0, []).append(entry)
        for lane, entries in self._lanes.items():
            entries.sort(key=lambda row: (row['start'], row['id']))
        self._starts = {lane: [row['start'] for row in rows] for lane, rows in self._lanes.items()}
        self.track_count = max(4, max(self._lanes, default=0)+1)
        first = min((row['start'] for row in self._entries.values()), default=0.)
        self.origin_seconds = min(0., first)-2 if self.mode == 'timeline' else 1.
        self.end_seconds = max(60., max((row['end'] for row in self._entries.values()), default=0.)+30) if self.mode == 'timeline' else max(20., self.script_count+1, max((row['end'] for row in self._entries.values()), default=1.)+1)
        self._update_scrolls()
        self.viewport().update()

    def _update_scrolls(self):
        pinned_bottom=self.verticalScrollBar().value()==self.verticalScrollBar().maximum()
        self.horizontalScrollBar().setRange(0, max(0, round((self.end_seconds-self.origin_seconds)*self.scale)-self.viewport().width()+self.GUTTER))
        self.horizontalScrollBar().setPageStep(max(1, self.viewport().width()-self.GUTTER))
        self.verticalScrollBar().setRange(0, max(0, (self.track_count+1)*self.TRACK+self.HEADER-self.viewport().height()))
        self.verticalScrollBar().setPageStep(max(1, self.viewport().height()-self.HEADER))
        if self.mode=='timeline' and pinned_bottom:self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def resizeEvent(self, event):
        self._update_scrolls()
        super().resizeEvent(event)

    def set_zoom(self, scale):
        centre = self.time_at(self.viewport().width()/2)
        self.scale = max(.05, min(300., float(scale)))
        self._update_scrolls()
        self.horizontalScrollBar().setValue(round((centre-self.origin_seconds)*self.scale-self.viewport().width()/2+self.GUTTER))
        self.viewport().update()

    def time_at(self, x):
        return self.origin_seconds+(x-self.GUTTER+self.horizontalScrollBar().value())/self.scale

    def lane_at(self, y):
        offset = math.floor((y-self.HEADER+self.verticalScrollBar().value())/self.TRACK)
        lane = self.track_count-offset if self.mode == 'timeline' else offset+1
        return max(1, lane)

    def lane_y(self, lane):
        row = self.track_count-lane if self.mode == 'timeline' else lane-1
        return self.HEADER+row*self.TRACK-self.verticalScrollBar().value()

    def clip_rect(self, entry):
        return QRectF(self.GUTTER+(entry['start']-self.origin_seconds)*self.scale-self.horizontalScrollBar().value(),
                      self.lane_y(entry['lane'])+5,
                      max(3., (entry['end']-entry['start'])*self.scale), self.TRACK-10)

    def visible_entries(self):
        first, last = self.time_at(self.GUTTER)-1, self.time_at(self.viewport().width())+1
        viewport = QRectF(self.GUTTER, self.HEADER, max(0, self.viewport().width()-self.GUTTER), max(0, self.viewport().height()-self.HEADER))
        for lane, entries in self._lanes.items():
            y = self.lane_y(lane)
            if y+self.TRACK < self.HEADER or y > self.viewport().height():
                continue
            start = max(0, bisect.bisect_left(self._starts[lane], first)-1)
            for entry in entries[start:]:
                if entry['start'] > last:
                    break
                if entry['end'] >= first and self.clip_rect(entry).intersects(viewport):
                    yield entry

    def hit(self, point):
        if point.x() < self.GUTTER or point.y() < self.HEADER:
            return None
        return next((row for row in self.visible_entries() if self.clip_rect(row).contains(float(point.x()),float(point.y()))), None)

    def paintEvent(self, event):
        painter = QPainter(self.viewport())
        painter.fillRect(self.viewport().rect(), QColor('#1e2632'))
        width, height = self.viewport().width(), self.viewport().height()
        for lane in range(0 if self.mode=='timeline' else 1,self.track_count+1):
            y = self.lane_y(lane)
            if y+self.TRACK < self.HEADER or y > height:
                continue
            painter.fillRect(QRectF(self.GUTTER, y, max(0,width-self.GUTTER), self.TRACK), QColor('#252f3e' if lane%2 else '#202a38'))
            painter.setPen(QColor('#465263'))
            painter.drawLine(self.GUTTER, round(y+self.TRACK), width, round(y+self.TRACK))
        painter.save()
        painter.setClipRect(QRectF(self.GUTTER,self.HEADER,max(0,width-self.GUTTER),max(0,height-self.HEADER)))
        for entry in self.visible_entries():
            rect = self.clip_rect(entry)
            if self._ghost and entry['id'] in self._drag_ids:
                rect.translate(self._ghost[0]*self.scale, self._ghost[1]*self.TRACK*(-1 if self.mode=='timeline' else 1))
            selected = entry['id'] in self.selection
            painter.setBrush(QColor('#546276' if entry['baseline'] and self.mode=='timeline' else '#25977d' if entry.get('reviewed') else '#357fc4'))
            painter.setPen(QPen(QColor('#ffd77b' if selected else '#53697f'), 2 if selected else 1))
            painter.drawRoundedRect(rect, 4, 4)
            painter.setPen(QColor('#ffffff'))
            text = entry['row'].get('filename') or Path(entry['row'].get('pgm_path','')).name
            painter.drawText(rect.adjusted(7,4,-5,-4), Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter,
                             painter.fontMetrics().elidedText(text,Qt.TextElideMode.ElideRight,max(0,round(rect.width()-12))))
        if self.mode == 'timeline':
            x=self.GUTTER+(self.playhead_seconds-self.origin_seconds)*self.scale-self.horizontalScrollBar().value()
            painter.setPen(QPen(QColor('#ffd77b'),1));painter.drawLine(round(x),self.HEADER,round(x),height)
        if self._rubber:
            painter.setBrush(QColor(70,150,240,45));painter.setPen(QColor('#88bfff'))
            painter.drawRect(self._rubber)
        painter.restore()
        painter.fillRect(QRect(0,0,width,self.HEADER),QColor('#111b28'))
        steps = (1,2,5,10,20,50,100,200,500,1000,2000,5000,10000,100000) if self.mode == 'organize' else (.1,.2,.5,1,2,5,10,20,30,60,120,300,600,1800,3600,7200,21600,100000)
        step = next(value for value in steps if value*self.scale>=80)
        first = max(1, math.floor(self.time_at(self.GUTTER)/step)*step) if self.mode == 'organize' else math.floor(self.time_at(self.GUTTER)/step)*step
        position = first
        painter.setPen(QColor('#b6c7db'))
        while position <= self.time_at(width)+step:
            x=self.GUTTER+(position-self.origin_seconds)*self.scale-self.horizontalScrollBar().value()
            if x>=self.GUTTER:
                sign='−' if position<0 else ''
                seconds=abs(position)
                text = f'台本 {int(position)}' if self.mode == 'organize' else f'{sign}{int(seconds//3600):02}:{int(seconds%3600//60):02}:{seconds%60:04.1f}'
                painter.drawText(QRectF(x+3,0,95,self.HEADER),Qt.AlignmentFlag.AlignVCenter,text)
                painter.drawLine(round(x),self.HEADER-7,round(x),self.HEADER)
            position+=step
        painter.fillRect(QRect(0,self.HEADER,self.GUTTER,max(0,height-self.HEADER)),QColor('#172230'))
        for lane in range(0 if self.mode=='timeline' else 1,self.track_count+1):
            y=self.lane_y(lane)
            if y+self.TRACK<self.HEADER or y>height:continue
            label='V1 基准（锁定）' if lane==0 else f'V{lane+1} 备选' if self.mode=='timeline' else f'整理 {lane}'
            painter.drawText(QRectF(6,y,self.GUTTER-10,self.TRACK),Qt.AlignmentFlag.AlignVCenter,label)
        painter.end()

    def viewportEvent(self,event):
        if event.type()==QEvent.Type.ToolTip:
            entry=self.hit(event.pos())
            if entry:
                row=entry['row']
                text=row.get('pgm_path','')+('\n'+str(row['metadata_warning']) if row.get('metadata_warning') else '')
                QToolTip.showText(event.globalPos(),text,self.viewport())
            else:QToolTip.hideText()
            return True
        return super().viewportEvent(event)

    def wheelEvent(self,event):
        delta=event.angleDelta()
        if event.modifiers() & (Qt.KeyboardModifier.ControlModifier|Qt.KeyboardModifier.MetaModifier):
            self.set_zoom(self.scale*(1.2 if delta.y()>0 else 1/1.2))
        elif event.modifiers() & Qt.KeyboardModifier.ShiftModifier or abs(delta.x())>abs(delta.y()):
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value()-(delta.x() or delta.y()))
        else:
            self.verticalScrollBar().setValue(self.verticalScrollBar().value()-delta.y())
        event.accept()

    def mousePressEvent(self,event):
        if event.button()!=Qt.MouseButton.LeftButton:return
        self.setFocus()
        point=event.position().toPoint()
        self.selection_position=None
        self._press_scroll=(self.horizontalScrollBar().value(),self.verticalScrollBar().value())
        if point.y()<self.HEADER and point.x()>=self.GUTTER:
            self.playhead_seconds=self.time_at(point.x());self.time_clicked.emit(self.playhead_seconds)
            self.viewport().update();return
        if point.x()<self.GUTTER and point.y()>=self.HEADER:
            if self.mode=='timeline' and self.track_count-math.floor((point.y()-self.HEADER+self.verticalScrollBar().value())/self.TRACK)<=0:
                return  # The locked V1 header is never an alternative lane.
            lane=self.lane_at(point.y())
            if lane>0:self._lane_drag=lane;self._press=point
            return
        row=self.hit(point)
        modifiers=event.modifiers()&(Qt.KeyboardModifier.ControlModifier|Qt.KeyboardModifier.MetaModifier|Qt.KeyboardModifier.ShiftModifier)
        if row:
            if self.mode=='timeline':self.selection_position=dict(id=row['id'],seconds=max(row['start'],min(self.time_at(point.x()),row['end']-.001)))
            if modifiers:
                self.selection.symmetric_difference_update({row['id']})
            elif row['id'] not in self.selection:
                self.selection={row['id']}
            self._drag_ids=[identity for identity in self.selection if identity in self._entries and not (self.mode == 'timeline' and self._entries[identity]['baseline'])]
            self._press=point
            self._rubber=None
        else:
            if not modifiers:self.selection.clear()
            self._drag_ids=[];self._press=point;self._rubber=QRect(point,point)
        self.viewport().update()

    def mouseMoveEvent(self,event):
        if self._press is None:return
        point=event.position().toPoint()
        if self._rubber is not None:
            self._rubber=QRect(self._press,point).normalized()
        elif self._drag_ids and (point-self._press).manhattanLength()>3:
            fps=float(self.baseline_cut.get('fps') or 25)
            seconds=round((point.x()-self._press.x()+self.horizontalScrollBar().value()-self._press_scroll[0])/self.scale*fps)/fps
            lane=round((point.y()-self._press.y()+self.verticalScrollBar().value()-self._press_scroll[1])/self.TRACK)*(-1 if self.mode=='timeline' else 1)
            minimum_lane=min(self._entries[identity]['lane'] for identity in self._drag_ids)
            lane=max(1-minimum_lane,lane)
            if self.mode=='organize':seconds=0.  # Script assignment changes only through explicit range editing.
            self._ghost=(seconds,lane)
        if point.x()>self.viewport().width()-20:self.horizontalScrollBar().setValue(self.horizontalScrollBar().value()+15)
        elif point.x()<self.GUTTER+15:self.horizontalScrollBar().setValue(self.horizontalScrollBar().value()-15)
        if point.y()>self.viewport().height()-20:self.verticalScrollBar().setValue(self.verticalScrollBar().value()+15)
        elif point.y()<self.HEADER+20:self.verticalScrollBar().setValue(self.verticalScrollBar().value()-15)
        self.viewport().update()

    def mouseReleaseEvent(self,event):
        if self._press is None:return
        if self._lane_drag is not None or self._ghost is not None or self._rubber is not None:self.selection_position=None
        if self._lane_drag is not None:
            source,target=self._lane_drag,min(self.track_count,self.lane_at(event.position().y()))
            if source!=target:
                result=copy.deepcopy(self.layout_data)
                field='lane' if self.mode=='timeline' else 'organize_lane'
                moved=[]
                for identity,item in result.get('items',{}).items():
                    lane=item.get(field,1)
                    if lane==source:item[field]=target
                    elif source<target and source<lane<=target:item[field]=lane-1
                    elif target<source and target<=lane<source:item[field]=lane+1
                    if item.get(field)!=lane and item.get('start_seconds') is not None and item.get('session_id')!=self.baseline_id:moved.append(identity)
                try:
                    from manual_timeline import validate_layout
                    result=validate_layout(result,list(self.inventory.values()),self.baseline_id)
                    if self.mode=='timeline' and moved:
                        for identity in moved:
                            result['items'][identity]['alignment_origin']='manual'
                            result['items'][identity]['alignment_reviewed']=False
                    self.set_layout(result);self.layout_changed.emit(result)
                except ValueError as exc:self.message.emit(str(exc))
        elif self._rubber is not None:
            self.selection.update(row['id'] for row in self.visible_entries() if self.clip_rect(row).intersects(QRectF(self._rubber)))
        elif self._ghost and self._drag_ids:
            try:
                from manual_timeline import move_items
                changed=move_items(self.layout_data,self._drag_ids,self._ghost[0],self._ghost[1],
                    list(self.inventory.values()),self.baseline_id,mode=self.mode,auto_lane=True)
                self.set_layout(changed);self.layout_changed.emit(changed)
            except ValueError as exc:self.message.emit(str(exc))
        self._press=self._rubber=self._ghost=self._lane_drag=None;self._drag_ids=[]
        ordered=sorted(self.selection,key=lambda identity:(self._entries.get(identity,{}).get('start',0),identity))
        self.selection_changed.emit(ordered)
        self.viewport().update()

    def keyPressEvent(self,event):
        if event.key()==Qt.Key.Key_Escape and self._press is not None:
            self._press=self._rubber=self._ghost=self._lane_drag=None;self._drag_ids=[]
            self.viewport().update();event.accept()
        else:super().keyPressEvent(event)

    def dragEnterEvent(self,event):
        if event.mimeData().hasFormat(MIME):event.acceptProposedAction()

    def dragMoveEvent(self,event):
        if event.mimeData().hasFormat(MIME):event.acceptProposedAction()

    def dropEvent(self,event):
        try:
            ids=json.loads(bytes(event.mimeData().data(MIME)))
            point=event.position().toPoint()
            self.dropped.emit(ids,self.time_at(point.x()),self.lane_at(point.y()))
            event.acceptProposedAction()
        except (ValueError,TypeError):event.ignore()


class ManualTimelineDialog(QDialog):
    """Fullscreen-capable manual browser, organiser and baseline alignment."""
    automatic_alignment_requested = pyqtSignal(object)
    automatic_alignment_cancelled = pyqtSignal()

    def __init__(self,data,inventory,layout,baseline_cut=None,parent=None,*,warnings=None):
        super().__init__(parent)
        from manual_timeline import reconcile_layout
        self.data,self.inventory=data,inventory
        self.baseline_cut=baseline_cut or {}
        self.baseline_id=layout.get('baseline_session_id')
        self.layout_data=reconcile_layout(copy.deepcopy(layout),inventory,self.baseline_id)
        self._initial=copy.deepcopy(self.layout_data)
        self.result_data=None;self.export_mode=None;self._allow_close=False
        self._auto_busy=False
        self._alignment_report={}
        self._alignment_reasons={}
        self._inventory={_identity(row):row for row in inventory}
        self._selected=[]
        self._preview_identity=None
        self._alternative_ms=0
        self._alternative_seek_pending=None
        self._alternative_wants_play=False
        self._preview_closed=False
        self._changing_preview=False
        self._active_preview='alternative'
        self._selection_from_edit=False
        self._baseline_path=''
        self._baseline_block=None
        self._baseline_target=None
        self._baseline_loading=False
        self._baseline_seek_pending=None
        self._baseline_wants_play=False
        self._baseline_independent=False
        self._baseline_blocks=[]
        self._baseline_starts=[]
        self._baseline_advance=QTimer(self);self._baseline_advance.setSingleShot(True)
        self._baseline_advance.timeout.connect(self._advance_baseline)
        self._preview_resume=QTimer(self);self._preview_resume.setSingleShot(True)
        self._preview_resume.timeout.connect(self._resume_after_ready)
        self._resume_retries=0
        self._rebuild_baseline_index()
        from baseline_map_ui import script_nodes
        self.script_nodes = script_nodes(data)
        self.setWindowTitle('PGM编排与对齐')
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint,True)
        self.resize(1500,940);self.setMinimumSize(1050,680)
        root=QVBoxLayout(self)
        toolbar=QHBoxLayout()
        self.mode=QComboBox();self.mode.addItem('素材整理','organize');self.mode.addItem('基准时间线对齐','timeline')
        self.mode.currentIndexChanged.connect(self.mode_changed)
        toolbar.addWidget(self.mode)
        self.summary=QLabel();toolbar.addWidget(self.summary,1)
        self.active_preview_label=QLabel('当前预览：备选（右）');toolbar.addWidget(self.active_preview_label)
        toolbar.addWidget(QLabel('缩放'))
        self.zoom=QSlider(Qt.Orientation.Horizontal);self.zoom.setRange(-30,50);self.zoom.setValue(15);self.zoom.setMaximumWidth(180)
        self.zoom.valueChanged.connect(lambda value:self.canvas.set_zoom(2**(value/5)))
        toolbar.addWidget(self.zoom)
        full=QPushButton('全屏');full.clicked.connect(self.toggle_fullscreen);toolbar.addWidget(full)
        root.addLayout(toolbar)
        self.alignment_actions=QWidget();actions=QHBoxLayout(self.alignment_actions);actions.setContentsMargins(0,0,0,0)
        self.auto_align_button=QPushButton('建立映射并自动对齐');self.auto_align_button.clicked.connect(lambda:self.request_automatic('prepare_automatic_alignment'));actions.addWidget(self.auto_align_button)
        self.confirm_alignment_button=QPushButton('确认所选对齐');self.confirm_alignment_button.clicked.connect(self.confirm_selected_alignment);self.confirm_alignment_button.setToolTip('可多选。确认最终位置后变绿；再次移动、修改起点或换轨后需要重新确认。');actions.addWidget(self.confirm_alignment_button)
        self.replace_automatic=QCheckBox('重排未核实自动片段');self.replace_automatic.setToolTip('仅重排尚未核实的自动位置；已有人工作业的位置不会覆盖。');actions.addWidget(self.replace_automatic)
        self.alignment_model=QComboBox();self.alignment_model.addItem('small','small');self.alignment_model.addItem('medium','medium');self.alignment_model.setToolTip('台词识别模型；中英文自动识别。');actions.addWidget(self.alignment_model)
        self.auto_progress=QProgressBar();self.auto_progress.setRange(0,100);actions.addWidget(self.auto_progress,1)
        self.cancel_auto_button=QPushButton('取消');self.cancel_auto_button.clicked.connect(self.cancel_automatic);actions.addWidget(self.cancel_auto_button)
        self.alignment_note=QLabel();self.alignment_note.setWordWrap(True)
        root.addWidget(self.alignment_actions);root.addWidget(self.alignment_note)
        self.warnings=QLabel('\n'.join(str(value) for value in (warnings or [])))
        self.warnings.setWordWrap(True);self.warnings.setStyleSheet('color:#9c6722;')
        self.warnings.setVisible(bool(warnings));root.addWidget(self.warnings)
        split=QSplitter(Qt.Orientation.Horizontal);self.main_split=split;root.addWidget(split,1)
        self.script_panel=QWidget();script_box=QVBoxLayout(self.script_panel);script_box.setContentsMargins(0,0,0,0)
        script_box.addWidget(QLabel('台本预览'))
        from script_preview import ScriptPreview
        self.script_preview=ScriptPreview();self.script_preview.set_nodes(self.script_nodes)
        script_box.addWidget(self.script_preview,1)
        split.addWidget(self.script_panel)
        left=QWidget();left_box=QVBoxLayout(left);left_box.setContentsMargins(0,0,0,0)
        self.pool_title=QLabel('未匹配台本');left_box.addWidget(self.pool_title)
        self.pool_kind=QComboBox();self.pool_kind.addItem('未匹配台本','unmatched');self.pool_kind.addItem('轨道移出','removed')
        self.pool_kind.currentIndexChanged.connect(self.refresh_pool);left_box.addWidget(self.pool_kind)
        self.search=QLineEdit();self.search.setPlaceholderText('搜索场次或文件名');self.search.textChanged.connect(self.refresh_pool);left_box.addWidget(self.search)
        self.pool_model=TakePoolModel(self);self.pool_model.baseline_id=self.baseline_id
        self.pool=QListView();self.pool.setModel(self.pool_model);self.pool.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.pool.setDragEnabled(True);self.pool.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.pool.setUniformItemSizes(True)
        self.pool.selectionModel().selectionChanged.connect(self.pool_selection)
        left_box.addWidget(self.pool,1)
        self.add_button=QPushButton('按台本区间加入');self.add_button.clicked.connect(self.place_pool);left_box.addWidget(self.add_button)
        self.transfer_button=QPushButton('将所选加入基准对齐');self.transfer_button.clicked.connect(self.transfer_to_timeline);left_box.addWidget(self.transfer_button)
        split.addWidget(left)
        right=QSplitter(Qt.Orientation.Vertical)
        top=QWidget();top_box=QVBoxLayout(top);top_box.setContentsMargins(0,0,0,0)
        from library_ui import InlinePreview
        self.preview_split=QSplitter(Qt.Orientation.Horizontal)
        self.baseline_panel=QWidget();base_box=QVBoxLayout(self.baseline_panel);base_box.setContentsMargins(0,0,0,0)
        self.baseline_label=QLabel('基准预览 · 点选基准片段播放');base_box.addWidget(self.baseline_label)
        self.baseline_preview=InlinePreview(self);self.baseline_preview.setMinimumHeight(170);base_box.addWidget(self.baseline_preview,1)
        self.baseline_play_button=QPushButton('基准播放／暂停');self.baseline_play_button.clicked.connect(self.toggle_baseline_playback);base_box.addWidget(self.baseline_play_button)
        self.preview_split.addWidget(self.baseline_panel)
        self.alternative_panel=QWidget();alt_box=QVBoxLayout(self.alternative_panel);alt_box.setContentsMargins(0,0,0,0)
        self.clip_label=QLabel('选择PGM预览；按空格播放');alt_box.addWidget(self.clip_label)
        self.preview=InlinePreview(self);self.preview.setMinimumHeight(170);alt_box.addWidget(self.preview,1)
        self.play_button=QPushButton('播放／暂停（空格）');self.play_button.clicked.connect(self.toggle_alternative_playback);alt_box.addWidget(self.play_button)
        self.preview_split.addWidget(self.alternative_panel);self.preview_split.setSizes([500,500]);top_box.addWidget(self.preview_split,1)
        for label in (self.baseline_label,self.clip_label):
            label.setMinimumWidth(0);label.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred)
        for preview in (self.preview,self.baseline_preview):
            if hasattr(getattr(preview,'video',None),'setAspectRatioMode'):preview.video.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        inspector=QHBoxLayout()
        self.position_label=QLabel('起点（秒）');inspector.addWidget(self.position_label);self.position=QDoubleSpinBox();self.position.setRange(-864000,864000);self.position.setDecimals(3);inspector.addWidget(self.position)
        self.range_label=QLabel('台本区间');inspector.addWidget(self.range_label)
        self.script_range=QLineEdit();self.script_range.setPlaceholderText('55-76 或 55');self.script_range.setMaximumWidth(180);inspector.addWidget(self.script_range)
        inspector.addWidget(QLabel('轨道'));self.lane=QSpinBox();self.lane.setRange(1,10000);inspector.addWidget(self.lane)
        self.apply_button=QPushButton('应用区间并核实');self.apply_button.clicked.connect(self.apply_position);inspector.addWidget(self.apply_button)
        self.remove_button=QPushButton('移至轨道移出');self.remove_button.clicked.connect(self.remove_selected);inspector.addWidget(self.remove_button)
        top_box.addLayout(inspector)
        right.addWidget(top)
        self.canvas=ManualTimelineCanvas(inventory,self.layout_data,self.baseline_id,self.baseline_cut,self)
        self.canvas.set_script_count(len(self.script_nodes))
        self.canvas.setMinimumHeight(200)
        self.canvas.selection_changed.connect(self._canvas_selection)
        self.canvas.layout_changed.connect(self.layout_changed)
        self.canvas.message.connect(self.show_error)
        self.canvas.dropped.connect(self.place_ids)
        self.canvas.time_clicked.connect(self.seek_timeline)
        self.preview.player.positionChanged.connect(self.preview_position_changed)
        self.preview.player.playbackStateChanged.connect(lambda state:self._playback_changed('alternative',state))
        self.baseline_preview.player.playbackStateChanged.connect(lambda state:self._playback_changed('baseline',state))
        self.preview.player.mediaStatusChanged.connect(lambda status:self._preview_ready('alternative',status))
        self.baseline_preview.player.positionChanged.connect(self._baseline_position_changed)
        self.baseline_preview.player.mediaStatusChanged.connect(self._baseline_ready)
        if getattr(self.baseline_preview,'seek',None) is not None:self.baseline_preview.seek.sliderMoved.connect(self._baseline_user_seek)
        if getattr(self.preview,'seek',None) is not None:self.preview.seek.sliderMoved.connect(self._alternative_user_seek)
        right.addWidget(self.canvas);right.setSizes([480,350]);split.addWidget(right);split.setSizes([300,230,1000])
        self.error=QLabel();self.error.setWordWrap(True);self.error.setStyleSheet('color:#b43d35;');root.addWidget(self.error)
        self.hint=QLabel();self.hint.setWordWrap(True);root.addWidget(self.hint)
        footer=QHBoxLayout()
        self.export_buttons=[]
        for text,mode in [('仅导出基准XML','baseline'),('仅导出备选XML','alternatives'),('导出基准＋备选XML','combined')]:
            button=QPushButton(text);button.clicked.connect(lambda _checked=False,mode=mode:self.save_and_export(mode));button.setEnabled(bool(self.baseline_id));footer.addWidget(button);self.export_buttons.append(button)
        footer.addStretch()
        self.save_button=QPushButton('保存编排');self.save_button.clicked.connect(self.save_layout);footer.addWidget(self.save_button)
        self.close_button=QPushButton('关闭');self.close_button.clicked.connect(self.reject);footer.addWidget(self.close_button);root.addLayout(footer)
        QApplication.instance().installEventFilter(self)
        self.full=QShortcut(QKeySequence('Ctrl+Meta+F'),self);self.full.activated.connect(self.toggle_fullscreen)
        for owner in (self.canvas,self.pool):
            for key in ('Delete','Backspace'):
                shortcut=QShortcut(QKeySequence(key),owner);shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut);shortcut.activated.connect(self.remove_selected)
        for button in self.findChildren(QPushButton):button.setAutoDefault(False)
        self._update_mode_controls();self.refresh_pool();self.update_summary();self.set_automatic_busy(False);self._activate_preview('alternative')

    def _previews_alive(self):
        return not self._preview_closed and not sip.isdeleted(self)

    def _rebuild_baseline_index(self):
        """Index edit-clock intervals once, never scan source metadata per frame."""
        blocks=[]
        try:fps=float(self.baseline_cut.get('fps') or 0)
        except (ValueError,TypeError):fps=0.
        if fps>0:
            for row in self.baseline_cut.get('pgm_sources',[]):
                try:
                    start=float(row['start'])/fps
                    duration=float(row.get('frames') or (float(row['end'])-float(row['start'])))/fps
                    end=min(start+duration,float(row.get('end',row['start']+duration*fps))/fps)
                    if row.get('path') and all(math.isfinite(value) for value in (start,end,duration)) and end>start:
                        blocks.append(dict(path=row['path'],start=start,end=end,duration=duration))
                except (ValueError,TypeError,KeyError):continue
        if not blocks:
            for row in self.inventory:
                if row.get('session_id')!=self.baseline_id:continue
                try:start=float(row['baseline_start_seconds'])
                except (ValueError,TypeError,KeyError):continue
                duration=_duration(row)
                if math.isfinite(start) and duration>0 and row.get('pgm_path'):
                    blocks.append(dict(path=row['pgm_path'],start=start,end=start+duration,duration=duration))
        self._baseline_blocks=sorted(blocks,key=lambda block:(block['start'],block['path']))
        self._baseline_starts=[block['start'] for block in self._baseline_blocks]

    def _baseline_at(self,seconds):
        index=bisect.bisect_right(self._baseline_starts,seconds)-1
        if index>=0:
            block=self._baseline_blocks[index]
            if block['start']<=seconds<block['end']:return block
        return None

    @staticmethod
    def _playing(preview):
        from PyQt6.QtMultimedia import QMediaPlayer
        return preview.player.playbackState()==QMediaPlayer.PlaybackState.PlayingState

    @staticmethod
    def _pause_preview(preview):
        """Preserve a native EOF frame: pause on EndOfMedia can rewind Qt."""
        from PyQt6.QtMultimedia import QMediaPlayer
        preview._play_requested=False
        status=getattr(preview.player,'mediaStatus',None)
        if (preview.player.playbackState()==QMediaPlayer.PlaybackState.StoppedState and callable(status)
                and status()==QMediaPlayer.MediaStatus.EndOfMedia):return
        preview.pause()

    def _pause_previews(self):
        self._preview_resume.stop();self._resume_retries=0
        self._baseline_advance.stop();self._baseline_wants_play=False;self._baseline_independent=False;self._alternative_wants_play=False
        self._pause_preview(self.preview);self._pause_preview(self.baseline_preview)

    def _clear_baseline(self,message='基准预览 · 当前位置没有基准画面'):
        self._baseline_advance.stop();self._baseline_seek_pending=None;self._baseline_wants_play=False;self._baseline_independent=False
        self._baseline_target=None;self._baseline_block=None
        if self._baseline_path:
            self._baseline_path=''
            self._changing_preview=True
            try:self._pause_preview(self.baseline_preview);self.baseline_preview.clear()
            finally:self._changing_preview=False
        self.baseline_label.setText(message)

    def _show_baseline(self,seconds,*,playing=False):
        if not self._previews_alive() or self.mode.currentData()!='timeline':return
        block=self._baseline_at(float(seconds))
        if block is None:
            self._clear_baseline();return
        self._baseline_block=block;self._baseline_target=float(seconds)
        local_ms=round((float(seconds)-block['start'])*1000)
        self._baseline_seek_pending=max(0,min(local_ms,max(0,round(block['duration']*1000)-1)))
        self._baseline_wants_play=bool(playing)
        self.baseline_label.setText(f'基准 · {Path(block["path"]).name}')
        self.baseline_label.setToolTip(f'基准时间线 {float(seconds):.3f} 秒；文件内 {local_ms/1000:.3f} 秒')
        if block['path']!=self._baseline_path:
            self._baseline_path=block['path'];self._baseline_loading=True;self._changing_preview=True
            try:
                self._pause_preview(self.baseline_preview)
                self.baseline_preview.load_media(block['path'],0,block['duration'],play=False)
            finally:self._changing_preview=False;self._baseline_loading=False
        # InlinePreview applies its initial seek when the asynchronous source is
        # ready. Our ready callback then applies this most recent edit position.
        if getattr(self.baseline_preview,'positioned',True):self._apply_baseline_seek()

    def _baseline_ready(self,status):
        if not self._previews_alive() or self._baseline_loading or not self._baseline_path:return
        self._preview_ready('baseline',status)
        from PyQt6.QtMultimedia import QMediaPlayer
        if status in (QMediaPlayer.MediaStatus.LoadedMedia,QMediaPlayer.MediaStatus.BufferedMedia):
            self._apply_baseline_seek();self._schedule_preview_resume('baseline')
        elif status==QMediaPlayer.MediaStatus.EndOfMedia and self._baseline_independent:self._baseline_advance.start(0)

    def _advance_baseline(self):
        """Continue independent V1 playback across adjacent PGM files only."""
        if not self._previews_alive() or self._active_preview!='baseline' or self.mode.currentData()!='timeline' or not self._baseline_independent or not self._baseline_block:return
        end=self._baseline_block['end'];next_block=self._baseline_at(end)
        if next_block is None or next_block is self._baseline_block:
            self._baseline_independent=False;self._baseline_wants_play=False;self._pause_preview(self.baseline_preview);return
        self._show_baseline(end,playing=True)

    def _apply_baseline_seek(self):
        if not self._previews_alive() or self._baseline_seek_pending is None or not self._baseline_path:return
        milliseconds=self._baseline_seek_pending;self._baseline_seek_pending=None
        playing=self._baseline_wants_play and self._active_preview=='baseline' and self.mode.currentData()=='timeline'
        current=self.baseline_preview.player.position()
        current=float(current) if isinstance(current,(int,float)) else -100000.
        needs_seek=abs(current-milliseconds)>15
        self._changing_preview=True
        try:
            if not playing:self._pause_preview(self.baseline_preview)
            if needs_seek:
                audio=getattr(self.baseline_preview,'preview_audio',None)
                if audio is not None:audio.seek(milliseconds)
                else:self.baseline_preview.player.setPosition(milliseconds)
            self.baseline_preview._play_requested=playing
            if playing and not self._playing(self.baseline_preview):self.baseline_preview.player.play()
        finally:self._changing_preview=False
        if self._active_preview=='baseline' and self._baseline_block:
            self.canvas.playhead_seconds=self._baseline_block['start']+milliseconds/1000
            self.canvas.viewport().update()

    def _activate_preview(self,side):
        """The last selected clip, never stale media or focus, owns controls."""
        if not self._previews_alive():return
        if self.mode.currentData()!='timeline':side='alternative'
        self._preview_resume.stop();self._resume_retries=0
        self._active_preview=side
        if side=='baseline':
            self._alternative_wants_play=False;self._pause_preview(self.preview)
        else:
            self._baseline_advance.stop();self._baseline_wants_play=False;self._baseline_independent=False;self._pause_preview(self.baseline_preview)
        self.active_preview_label.setText('当前控制：基准（左）' if side=='baseline' else '当前控制：备选（右）')
        self.baseline_label.setStyleSheet('color:#235d95;font-weight:600;' if side=='baseline' else '')
        self.clip_label.setStyleSheet('color:#235d95;font-weight:600;' if side=='alternative' else '')
        for current,preview,button in (('baseline',self.baseline_preview,self.baseline_play_button),('alternative',self.preview,self.play_button)):
            enabled=current==side and not self._auto_busy
            button.setEnabled(enabled)
            if getattr(preview,'seek',None) is not None:preview.seek.setEnabled(enabled)

    def _playback_changed(self,side,state):
        from PyQt6.QtMultimedia import QMediaPlayer
        preview=self.baseline_preview if side=='baseline' else self.preview
        wanted=self._baseline_wants_play if side=='baseline' else self._alternative_wants_play
        if state!=QMediaPlayer.PlaybackState.PlayingState:
            if state==QMediaPlayer.PlaybackState.PausedState and wanted:self._schedule_preview_resume(side)
            return
        if not self._previews_alive() or side!=self._active_preview or not wanted or side=='baseline' and self.mode.currentData()!='timeline':
            self._pause_preview(preview);return
        self._pause_preview(self.preview if side=='baseline' else self.baseline_preview)

    def _preview_ready(self,side,status):
        if not self._previews_alive():return
        from PyQt6.QtMultimedia import QMediaPlayer
        if side!=self._active_preview:
            self._pause_preview(self.baseline_preview if side=='baseline' else self.preview)
        if side=='alternative' and status in (QMediaPlayer.MediaStatus.LoadedMedia,QMediaPlayer.MediaStatus.BufferedMedia):
            self._apply_alternative_seek();self._schedule_preview_resume(side)
        elif side=='alternative' and status==QMediaPlayer.MediaStatus.EndOfMedia:
            self._alternative_wants_play=False;self.preview._play_requested=False;self._preview_resume.stop()

    def _schedule_preview_resume(self,side):
        if not self._previews_alive() or self._auto_busy or side!=self._active_preview:return
        wanted=self._baseline_wants_play if side=='baseline' else self._alternative_wants_play
        preview=self.baseline_preview if side=='baseline' else self.preview
        if not wanted or not getattr(preview,'positioned',True):return
        duration=self._baseline_block['duration'] if side=='baseline' and self._baseline_block else _duration(self._inventory.get(self._preview_identity,{}))
        position=preview.player.position()
        if isinstance(position,(float,int)) and duration>0 and position>=round(duration*1000)-1:return
        if not self._preview_resume.isActive() and self._resume_retries<3:
            self._resume_retries+=1;self._preview_resume.start(0)

    def _resume_after_ready(self):
        """A queued native pause from InlinePreview.ready must finish first."""
        if not self._previews_alive() or self._auto_busy:return
        side=self._active_preview;preview=self.baseline_preview if side=='baseline' else self.preview
        wanted=self._baseline_wants_play if side=='baseline' else self._alternative_wants_play
        pending=self._baseline_seek_pending if side=='baseline' else self._alternative_seek_pending
        if not wanted or pending is not None or not getattr(preview,'positioned',True):return
        self._pause_preview(self.preview if side=='baseline' else self.baseline_preview)
        preview._play_requested=True
        if not self._playing(preview):preview.player.play()

    def _set_alternative_position(self,milliseconds,*,play=False):
        row=self._inventory.get(self._preview_identity,{})
        duration=_duration(row)
        self._alternative_seek_pending=max(0,min(round(milliseconds),max(0,round(duration*1000)-1)))
        self._alternative_wants_play=bool(play) and self._active_preview=='alternative'
        self._alternative_ms=self._alternative_seek_pending
        if getattr(self.preview,'positioned',True):self._apply_alternative_seek()

    def _apply_alternative_seek(self):
        if not self._previews_alive() or self._alternative_seek_pending is None:return
        milliseconds=self._alternative_seek_pending;self._alternative_seek_pending=None
        self._alternative_ms=milliseconds
        playing=self._alternative_wants_play and self._active_preview=='alternative' and not self._auto_busy
        current=self.preview.player.position()
        current=float(current) if isinstance(current,(int,float)) else -100000.
        self._changing_preview=True
        try:
            if not playing:self._pause_preview(self.preview)
            if abs(current-milliseconds)>1:
                audio=getattr(self.preview,'preview_audio',None)
                if audio is not None:audio.seek(milliseconds)
                else:self.preview.player.setPosition(milliseconds)
            self.preview._play_requested=playing
            if playing:self.preview.player.play()
        finally:self._changing_preview=False
        self._update_alternative_cursor(milliseconds)

    def _update_alternative_cursor(self,milliseconds):
        if self.mode.currentData()!='timeline' or self._active_preview!='alternative':return
        item=self.layout_data.get('items',{}).get(self._preview_identity,{})
        if item.get('start_seconds') is not None:
            self.canvas.playhead_seconds=float(item['start_seconds'])+float(milliseconds)/1000
            self.canvas.viewport().update()

    def _alternative_local_at_head(self):
        row=self._inventory.get(self._preview_identity,{})
        item=self.layout_data.get('items',{}).get(self._preview_identity,{})
        if self.mode.currentData()=='timeline' and item.get('start_seconds') is not None:
            local=self.canvas.playhead_seconds-float(item['start_seconds'])
            duration=_duration(row)
            if 0<=local<duration:return local*1000
            if duration>0 and abs(local-duration)<.001:
                return max(0.,duration-1/float(row.get('fps') or 25))*1000
            return 0
        return self._alternative_ms

    def _baseline_position_changed(self,milliseconds):
        if not self._previews_alive() or self._changing_preview or not self._baseline_block or self.mode.currentData()!='timeline':return
        if self._active_preview=='baseline':
            self.canvas.playhead_seconds=self._baseline_block['start']+float(milliseconds)/1000
            self.canvas.viewport().update()
        if self._active_preview=='baseline' and self._baseline_independent and milliseconds>=round(self._baseline_block['duration']*1000)-5:
            self._baseline_advance.start(0)

    def _baseline_user_seek(self,milliseconds):
        if not self._previews_alive() or self._active_preview!='baseline':return
        self._alternative_wants_play=False;self._pause_preview(self.preview)
        if not getattr(self.baseline_preview,'positioned',True):self._baseline_seek_pending=int(milliseconds)
        if self._baseline_block:
            self._baseline_target=self._baseline_block['start']+int(milliseconds)/1000
            self.canvas.playhead_seconds=self._baseline_target;self.canvas.viewport().update()

    def _alternative_user_seek(self,milliseconds):
        if not self._previews_alive() or self._active_preview!='alternative':return
        self._baseline_wants_play=False;self._baseline_independent=False;self._baseline_advance.stop();self._pause_preview(self.baseline_preview)
        self._alternative_ms=int(milliseconds)
        if not getattr(self.preview,'positioned',True):self._alternative_seek_pending=int(milliseconds)
        self._update_alternative_cursor(milliseconds)

    def _canvas_selection(self,ids):
        autoplay=not self._selection_from_edit
        self._selection_from_edit=False
        position=self.canvas.selection_position
        if position and position['id'] in ids:
            # Group membership may be sorted by timeline start, but playback
            # belongs to the clip the user most recently clicked.
            ids=[position['id']]+[identity for identity in ids if identity!=position['id']]
        self.track_selection(ids,autoplay=autoplay,seconds=position['seconds'] if position and position['id'] in ids else None)

    def eventFilter(self,watched,event):
        if event.type() not in (QEvent.Type.ShortcutOverride,QEvent.Type.KeyPress):return False
        if event.key()!=Qt.Key.Key_Space or event.modifiers()!=Qt.KeyboardModifier.NoModifier or not self.isVisible() or self._auto_busy:return False
        modal=QApplication.activeModalWidget()
        if modal is not None and modal is not self:return False
        target=watched if isinstance(watched,QWidget) else QApplication.focusWidget()
        if target is None or not (target is self or self.isAncestorOf(target)):return False
        cursor=target
        while cursor is not None:
            if (isinstance(cursor,(QLineEdit,QTextEdit,QPlainTextEdit)) and not cursor.isReadOnly() or
                    isinstance(cursor,QAbstractSpinBox) or isinstance(cursor,QComboBox) and cursor.isEditable()):return False
            cursor=cursor.parentWidget()
        event.accept()
        if event.type()==QEvent.Type.KeyPress and not event.isAutoRepeat():self.toggle_playback()
        return True

    def mode_changed(self):
        if self.mode.currentData()=='timeline' and not self.baseline_id:
            self.mode.setCurrentIndex(0);self.show_error('请先在主界面选择基准通条，再进行时间线对齐。');return
        selected=list(self._selected)
        self._pause_previews()
        self.canvas.set_mode(self.mode.currentData());self._update_mode_controls();self.refresh_pool();self.update_summary()
        self._selected=selected
        self.canvas.selection=set(selected)&set(self.canvas._entries)
        if selected:self.track_selection(selected,autoplay=False)
        if self.mode.currentData()=='timeline' and self._inventory.get(self._preview_identity,{}).get('session_id')==self.baseline_id:
            self._changing_preview=True
            try:self.preview.clear()
            finally:self._changing_preview=False
            self._preview_identity=None;self._alternative_ms=0;self.clip_label.setText('备选预览 · 点选轨道片段播放')
        if self.mode.currentData()=='organize':self._clear_baseline('基准预览 · 对齐页启用')
        if self.mode.currentData()=='organize':self._activate_preview('alternative')
        self.canvas.viewport().update()

    def _update_mode_controls(self):
        organize=self.mode.currentData()=='organize'
        self.baseline_panel.setVisible(not organize);self.active_preview_label.setVisible(not organize)
        if not self._baseline_path:self.baseline_label.setText('基准预览 · 点选基准片段播放')
        self.play_button.setText('播放／暂停（空格）' if organize else '备选播放／暂停')
        if not self._preview_identity:self.clip_label.setText('选择PGM预览；按空格播放' if organize else '备选预览 · 点选轨道片段播放')
        self.alignment_actions.setVisible(not organize);self.alignment_note.setVisible(not organize)
        self.script_panel.setVisible(organize);self.pool_kind.setVisible(organize)
        self.range_label.setVisible(organize);self.script_range.setVisible(organize)
        self.position_label.setVisible(not organize);self.position.setVisible(not organize)
        self.transfer_button.setVisible(organize);self.transfer_button.setEnabled(bool(self.baseline_id))
        self.add_button.setText('按台本区间加入' if organize else '放入基准时间线')
        self.apply_button.setText('应用区间并核实' if organize else '应用到所选片段')
        self.remove_button.setText('移至轨道移出' if organize else '移回素材池')
        self.hint.setText('横轴为台本编号；绿色已核实、蓝色待核实。拖动只调整轨道，修改台本位置请明确输入区间。Command点选或框选可多选，滚轮上下、Shift横向滚动。' if organize else '点选轨道片段即播放对应窗口，另一侧停帧；空格控制当前窗口。移动或输入位置后为蓝色，点击“确认所选对齐”后变绿，移动后需重确认。基准V1锁定，源时码保持不变。')
        self.main_split.setSizes([300,230,1000] if organize else [0,260,1250])
        self._update_alignment_note()

    def request_automatic(self,operation='prepare_automatic_alignment'):
        if self._auto_busy:return
        if operation!='prepare_automatic_alignment':return self.show_error('请自动初排后在时间轴上拖动或确认片段。')
        if not self.baseline_id:return self.show_error('请先选择基准通条。')
        if not self.script_nodes:return self.show_error('请先导入台本，并保留已筛选的备选区间。')
        if not self.receivers(self.automatic_alignment_requested):return self.show_error('请从主程序打开PGM时间轴后执行自动对齐。')
        try:
            from manual_timeline import validate_layout
            layout=validate_layout(self.layout_data,self.inventory,self.baseline_id)
        except ValueError as exc:return self.show_error(str(exc))
        self._pause_previews();self.set_automatic_busy(True,'正在读取基准台词与台本映射…')
        self.automatic_alignment_requested.emit(dict(operation=operation,layout=layout,
            replace_automatic=self.replace_automatic.isChecked(),model=self.alignment_model.currentData()))

    def set_automatic_busy(self,busy,message=''):
        self._auto_busy=bool(busy)
        self.main_split.setEnabled(not busy);self.mode.setEnabled(not busy);self.zoom.setEnabled(not busy)
        self.save_button.setEnabled(not busy);self.close_button.setEnabled(not busy)
        for button in self.export_buttons:button.setEnabled(not busy and bool(self.baseline_id))
        self.auto_align_button.setEnabled(not busy and bool(self.baseline_id))
        self.replace_automatic.setEnabled(not busy);self.alignment_model.setEnabled(not busy)
        if busy:self.confirm_alignment_button.setEnabled(False)
        self.auto_progress.setVisible(bool(busy));self.cancel_auto_button.setVisible(bool(busy));self.cancel_auto_button.setEnabled(True)
        if busy:
            self.auto_progress.setValue(0)
            if message:self.alignment_note.setText(message)
        else:self._update_alignment_note()

    def automatic_progress(self,value,message=''):
        if not self._auto_busy:return
        self.auto_progress.setValue(max(0,min(100,int(value))))
        if message:self.alignment_note.setText(str(message))

    def cancel_automatic(self):
        if not self._auto_busy:return
        self.cancel_auto_button.setEnabled(False);self.alignment_note.setText('正在取消，已保存的人工对齐不会被清除…')
        self.automatic_alignment_cancelled.emit()

    def automatic_failed(self,message):
        self.set_automatic_busy(False);self.show_error(message)

    def apply_automatic_alignment(self,result):
        """Apply a prepared worker result without recognition, decoding or replay."""
        self._pause_previews()
        from manual_timeline import reconcile_layout
        selected=list(self._selected)
        if result.get('manual_inventory') is not None:
            self.inventory=result['manual_inventory'];self._inventory={_identity(row):row for row in self.inventory}
            self.canvas.inventory=self._inventory
        if result.get('manual_cut') is not None:
            self.baseline_cut=result['manual_cut'];self.canvas.baseline_cut=self.baseline_cut
        self._rebuild_baseline_index()
        if result.get('data') is not None:self.data=result['data']
        layout=result.get('manual_timeline',result.get('layout',self.layout_data))
        self.layout_data=reconcile_layout(copy.deepcopy(layout),self.inventory,self.baseline_id)
        if 'automatic_alignment' in result or 'alignment_report' in result:
            automatic=result.get('automatic_alignment') or {}
            applied=result.get('alignment_report') or {}
            summary=dict(automatic.get('summary') or {})
            summary.update({key:value for key,value in applied.items() if key not in ('speech','unresolved')})
            speech=applied.get('speech') or {}
            self._alignment_report=dict(summary=summary,unresolved=applied.get('unresolved',automatic.get('unresolved',[])),
                mapping_only=bool(applied.get('mapping_only')),
                speech_counts={key:speech.get(key,0) for key in ('recognized','reused')},
                speech_failures=[{key:row.get(key,'') for key in ('filename','pgm_path','reason')} for row in speech.get('failures',[])])
        self._alignment_reasons={row.get('take_id'):row.get('reason','') for row in self._alignment_report.get('unresolved',[]) if row.get('take_id')}
        self.canvas.set_layout(self.layout_data)
        self._selected=[identity for identity in selected if identity in self._inventory]
        self.canvas.selection=set(self._selected)&set(self.canvas._entries)
        self.refresh_pool();self.update_summary()
        entry=self.canvas._entries.get(self._selected[0]) if self._selected else None
        if entry and self.mode.currentData()=='timeline':self.position.setValue(entry['start']);self.lane.setValue(max(1,entry['lane']))
        if result.get('state'):self._initial=copy.deepcopy(self.layout_data)
        self.set_automatic_busy(False);self.error.clear();self.canvas.viewport().update()

    def _unreviewed_alignment_count(self):
        return sum(item.get('start_seconds') is not None and not item.get('hidden') and item.get('session_id')!=self.baseline_id and not item.get('alignment_reviewed',False)
                   for item in self.layout_data.get('items',{}).values())

    def _update_alignment_note(self):
        if not hasattr(self,'alignment_note') or self._auto_busy:return
        report=self._alignment_report;summary=report.get('summary') or report
        unresolved=report.get('unresolved') or []
        pending=self._unreviewed_alignment_count()
        text=f'蓝色对齐待核实 {pending} 条（仍可导出）'
        if report.get('mapping_only'):
            text='映射已保存，原位置保留；可勾选重排后再初排。 · '+text
        elif report:
            text=f'自动初排 {summary.get("placed_count",summary.get("proposed_count",0))} 条 · 保留已有位置 {summary.get("preserved_count",0)} 条 · 无法定位 {len(unresolved)} 条 · '+text
            if summary.get('placed_count',summary.get('proposed_count',0))==0:
                if 'eligible_count' in summary:text+=f' · 可初排绿色备选 {summary["eligible_count"]} 条'
                if 'baseline_anchor_count' in summary:text+=f' · 基准语音定位 {summary["baseline_anchor_count"]} 处'
        speech_failures=report.get('speech_failures') or []
        if speech_failures:text+=f' · 基准转写待处理 {len(speech_failures)} 条'
        self.alignment_note.setText(text)
        details=[f'备选无法定位 · {row.get("filename") or Path(row.get("pgm_path","")).name}：{row.get("reason","")}' for row in unresolved[:80]]
        details.extend(f'基准转写待处理 · {row.get("filename") or Path(row.get("pgm_path","")).name}：{row.get("reason","")}' for row in speech_failures[:80])
        self.alignment_note.setToolTip('\n'.join(details))
        self.confirm_alignment_button.setEnabled(bool(self.mode.currentData()=='timeline' and any(self.layout_data.get('items',{}).get(identity,{}).get('start_seconds') is not None and self._inventory.get(identity,{}).get('session_id')!=self.baseline_id for identity in self._selected)))

    def confirm_selected_alignment(self):
        if self._auto_busy or self.mode.currentData()!='timeline':return
        ids=[identity for identity in self._selected if self.layout_data.get('items',{}).get(identity,{}).get('start_seconds') is not None and self._inventory.get(identity,{}).get('session_id')!=self.baseline_id]
        if not ids:return
        try:
            from manual_timeline import confirm_alignment
            self.layout_changed(confirm_alignment(self.layout_data,ids,self.inventory,self.baseline_id))
        except ValueError as exc:self.show_error(str(exc))

    def transfer_to_timeline(self):
        selected=list(self._selected)
        if not selected:return self.show_error('请先选择需要对齐的PGM片段。')
        self.mode.setCurrentIndex(1)
        if self.mode.currentData()=='timeline':
            self._selected=selected
            self.show_error(f'已保留 {len(selected)} 条所选素材。请填写相对基准的起点（秒）和轨道，再点击“应用到所选片段”；不会把台本编号转换成秒数。')

    def update_summary(self):
        count=sum(item.get('start_seconds') is not None and not item.get('hidden') and item.get('session_id')!=self.baseline_id for item in self.layout_data.get('items',{}).values())
        missing_base=bool(self.baseline_id and any(row.get('session_id')==self.baseline_id and row.get('baseline_start_seconds') is None for row in self.inventory))
        if self.mode.currentData()=='organize':
            track=[item for item in self.layout_data.get('items',{}).values() if item.get('organize_pool')=='track' and _script_bounds(item)]
            reviewed=sum(bool(item.get('organize_reviewed')) for item in track)
            self.summary.setText(f'台本区间 {len(track)} 条PGM · 已核实 {reviewed} · 待核实 {len(track)-reviewed}')
        else:self.summary.setText(f'{len(self.inventory)} 条PGM · 已放置备选 {count} 条'+(' · 尚未选择基准' if not self.baseline_id else ' · 基准切点信息待补齐' if missing_base else ''))
        self._update_alignment_note()

    def refresh_pool(self):
        search=self.search.text().strip().casefold()
        mode=self.mode.currentData()
        rows=[]
        for row in self.inventory:
            baseline=row.get('session_id')==self.baseline_id
            item=self.layout_data.get('items',{}).get(_identity(row),{})
            if mode=='organize':
                visible=item.get('organize_pool','unmatched')==self.pool_kind.currentData()
            else:
                visible=row.get('baseline_start_seconds') is None if baseline else \
                    not item.get('hidden') and item.get('organize_pool') not in ('removed','hidden') and (item.get('start_seconds') is None or _duration(row)<=0)
            if visible and (not search or search in ' '.join(str(row.get(key,'')) for key in ('filename','session_name','pgm_path')).casefold()):
                rows.append(dict(row,alignment_reason=self._alignment_reasons.get(_identity(row)) or item.get('alignment_invalid_reason',''),**{key:item.get(key) for key in ('organize_first_line','organize_last_line','organize_reviewed','organize_pool')}))
        self.pool_model.mode=mode
        self.pool_model.set_rows(rows);self.pool_title.setText(f'{self.pool_kind.currentText() if mode=="organize" else "待放入基准时间线"} · {len(rows)} 条')

    def pool_selection(self,*_args):
        ids=[self.pool_model.data(index,Qt.ItemDataRole.UserRole) for index in self.pool.selectionModel().selectedIndexes()]
        if ids:
            self._selected=ids;self.canvas.selection.clear();self.canvas.viewport().update()
            if self.mode.currentData()=='organize':self.remove_button.setText('从素材池隐藏')
            self._inspect_selection(ids);self.preview_take(ids[0],as_source=True)
            self._update_alignment_note()

    def track_selection(self,ids,*,autoplay=True,seconds=None):
        self._selected=ids
        if not ids:self._update_alignment_note();return
        self.pool.clearSelection()
        self.remove_button.setText('移至轨道移出' if self.mode.currentData()=='organize' else '移回素材池')
        row=self.canvas._entries.get(ids[0])
        if row:
            if self.mode.currentData()=='timeline':self.position.setValue(row['start'])
            self.lane.setValue(max(1,row['lane']))
        self._inspect_selection(ids)
        self.preview_take(ids[0],autoplay=autoplay and self.mode.currentData()=='timeline',seconds=seconds)
        self._update_alignment_note()

    def _inspect_selection(self,ids):
        if not ids:return
        item=self.layout_data.get('items',{}).get(ids[0],{})
        bounds=_script_bounds(item)
        self.script_range.setText(f'{bounds[0]}-{bounds[1]}' if bounds and bounds[0]!=bounds[1] else str(bounds[0]) if bounds else '')
        if self.mode.currentData()=='organize':
            self.lane.setValue(int(item.get('organize_lane') or 1))
            self.highlight_script([_script_bounds(self.layout_data['items'].get(identity,{})) for identity in ids])

    def highlight_script(self,ranges):
        self.script_preview.highlight_ranges([bounds for bounds in ranges if bounds])

    def preview_take(self,identity,*,autoplay=False,seconds=None,as_source=False):
        row=self._inventory.get(identity)
        if not row:return
        self._pause_previews()
        if not as_source and self.mode.currentData()=='timeline' and row.get('session_id')==self.baseline_id:
            # Baseline selection never replaces the right-hand comparison.
            self._activate_preview('baseline')
            start=row.get('baseline_start_seconds')
            if start is None:self._clear_baseline('基准预览 · 请补齐基准切点信息')
            else:
                self._baseline_independent=bool(autoplay)
                target=float(seconds) if seconds is not None else self.canvas.playhead_seconds
                if not float(start)<=target<float(start)+_duration(row):target=float(start)
                self._show_baseline(target,playing=autoplay)
                self.canvas.playhead_seconds=target;self.canvas.viewport().update()
            return
        self._activate_preview('alternative')
        same=identity==self._preview_identity
        self._preview_identity=identity
        self.clip_label.setText(f'{row.get("session_name", "")} · {row.get("filename") or Path(row.get("pgm_path", "")).name}')
        if _duration(row)<=0:
            self.preview.clear();self.show_error('这条PGM尚无完整时长信息，请先补齐素材信息。');return
        if not same:
            self._alternative_ms=0;self._changing_preview=True
            try:self.preview.load_media(row.get('pgm_path',''),0,_duration(row),play=False)
            finally:self._changing_preview=False
        if seconds is not None:self.canvas.playhead_seconds=float(seconds)
        self._set_alternative_position(self._alternative_local_at_head(),play=autoplay)
        self.show_error(row.get('metadata_warning') or '')

    def preview_position_changed(self,milliseconds):
        if not self._previews_alive() or self._changing_preview:return
        self._alternative_ms=max(0,int(milliseconds))
        duration=_duration(self._inventory.get(self._preview_identity,{}))
        if duration and milliseconds>=round(duration*1000):self._alternative_wants_play=False
        self._update_alternative_cursor(milliseconds)

    def seek_timeline(self,seconds):
        if self.mode.currentData()=='organize':
            line=max(1,min(len(self.script_nodes) or 1,int(math.floor(seconds))))
            self.highlight_script([(line,line)]);return
        self.position.setValue(seconds);self._pause_previews()
        self.canvas.playhead_seconds=float(seconds)
        if self._active_preview=='baseline' or not self._preview_identity:
            self._activate_preview('baseline');self._show_baseline(float(seconds))
        else:
            item=self.layout_data.get('items',{}).get(self._preview_identity,{})
            start=item.get('start_seconds');row=self._inventory.get(self._preview_identity,{})
            if start is not None and 0<=seconds-float(start)<_duration(row):
                self._set_alternative_position((seconds-float(start))*1000)
        self.canvas.playhead_seconds=float(seconds);self.canvas.viewport().update()

    def toggle_playback(self):
        if self._active_preview=='baseline' and self.mode.currentData()=='timeline':self.toggle_baseline_playback()
        else:self.toggle_alternative_playback()

    def toggle_alternative_playback(self):
        if self._auto_busy or not self._previews_alive() or self._active_preview!='alternative':return
        self._activate_preview('alternative')
        if self._playing(self.preview) or self._alternative_wants_play:
            self._alternative_wants_play=False;self._pause_preview(self.preview)
        elif self._preview_identity:self._set_alternative_position(self._alternative_local_at_head(),play=True)

    def toggle_baseline_playback(self):
        if self._auto_busy or not self._previews_alive() or self.mode.currentData()!='timeline' or self._active_preview!='baseline':return
        self._activate_preview('baseline')
        if self._playing(self.baseline_preview) or self._baseline_wants_play:
            self._baseline_wants_play=False;self._baseline_independent=False;self._pause_preview(self.baseline_preview);self._baseline_advance.stop()
        else:
            self._baseline_independent=True
            target=self.canvas.playhead_seconds
            if self._baseline_at(target) is None:
                index=bisect.bisect_right(self._baseline_starts,target)-1
                block=self._baseline_blocks[index] if index>=0 else None
                if block and abs(block['end']-target)<.001:target=max(block['start'],block['end']-1/float(self.baseline_cut.get('fps') or 25))
            self._show_baseline(target,playing=True)

    def layout_changed(self,layout):
        self._pause_previews()
        if self.canvas._press is not None:self._selection_from_edit=True
        self.layout_data=layout
        if self.canvas.layout_data is not layout:self.canvas.set_layout(layout)
        self.refresh_pool();self.update_summary();self.error.clear()

    def place_pool(self):
        ids=[self.pool_model.data(index,Qt.ItemDataRole.UserRole) for index in self.pool.selectionModel().selectedIndexes()]
        self.place_ids(ids or self._selected,self.position.value(),self.lane.value())

    def place_ids(self,ids,seconds,lane):
        if not ids:return
        try:
            if self.mode.currentData()=='organize':
                from manual_timeline import set_script_range,move_items
                first,last=parse_script_range(self.script_range.text(),len(self.script_nodes))
                changed=set_script_range(self.layout_data,ids,first,last,self.inventory,self.baseline_id,reviewed=True,pool='track')
                first_lane=changed['items'][ids[0]].get('organize_lane',1)
                changed=move_items(changed,ids,0,int(lane)-first_lane,self.inventory,self.baseline_id,mode='organize',auto_lane=True)
            else:
                from manual_timeline import place_items
                changed=place_items(self.layout_data,ids,float(seconds),int(lane),self.inventory,self.baseline_id,mode='timeline',auto_lane=True)
            self.layout_changed(changed);self.canvas.selection=set(ids);self.canvas.viewport().update();self.track_selection(ids,autoplay=False)
        except ValueError as exc:self.show_error(str(exc))

    def apply_position(self):
        if self.mode.currentData()=='organize':
            return self.place_ids(list(self._selected),self.position.value(),self.lane.value())
        ids=[identity for identity in self._selected if self._inventory.get(identity,{}).get('session_id')!=self.baseline_id]
        if not ids:return
        key='start_seconds' if self.mode.currentData()=='timeline' else 'organize_seconds'
        lane_key='lane' if self.mode.currentData()=='timeline' else 'organize_lane'
        first=self.layout_data['items'][ids[0]]
        if first.get(key) is None:return self.place_ids(ids,self.position.value(),self.lane.value())
        try:
            from manual_timeline import move_items
            changed=move_items(self.layout_data,ids,self.position.value()-first[key],self.lane.value()-first[lane_key],self.inventory,self.baseline_id,mode=self.mode.currentData(),auto_lane=True)
            self.layout_changed(changed)
        except ValueError as exc:self.show_error(str(exc))

    def remove_selected(self):
        result=copy.deepcopy(self.layout_data)
        for identity in self._selected:
            row=self._inventory.get(identity,{})
            if self.mode.currentData()=='timeline' and row.get('session_id')==self.baseline_id:continue
            item=result.get('items',{}).get(identity)
            if item is not None:
                if self.mode.currentData()=='timeline':item['start_seconds']=None
                else:
                    item['organize_pool']='removed' if item.get('organize_pool')=='track' else 'hidden'
                    item['start_seconds']=None  # Explicitly discarding this take must also stop its alternative export.
        self._selected=[];self.canvas.selection.clear();self.layout_changed(result)

    def show_error(self,message):
        self.error.setText(str(message))

    def toggle_fullscreen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def save_layout(self,*_args,export_mode=None):
        if self._auto_busy:return
        try:
            from manual_timeline import validate_layout
            self.result_data=validate_layout(self.layout_data,self.inventory,self.baseline_id)
        except ValueError as exc:self.show_error(str(exc));return
        self.export_mode=export_mode
        self._allow_close=True;self.accept()

    def save_and_export(self,mode):
        self.save_layout(export_mode=mode)

    def layout_result(self):
        return copy.deepcopy(self.result_data)

    def reject(self):
        if self._auto_busy:self.cancel_automatic();return
        if not self._allow_close and self.layout_data!=self._initial:
            answer=QMessageBox.question(self,'保存PGM编排','编排尚未保存，是否保存？',
                QMessageBox.StandardButton.Save|QMessageBox.StandardButton.Discard|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Save)
            if answer==QMessageBox.StandardButton.Cancel:return
            if answer==QMessageBox.StandardButton.Save:self.save_layout();return
        self._allow_close=True;super().reject()

    def keyPressEvent(self,event):
        if event.key()==Qt.Key.Key_Escape and self.isFullScreen():
            self.showNormal();event.accept()
        else:super().keyPressEvent(event)

    def closeEvent(self,event):
        self.reject()
        event.accept() if self._allow_close else event.ignore()

    def done(self,result):
        if self._preview_closed:
            super().done(result);return
        self._preview_closed=True;self._preview_resume.stop();self._baseline_advance.stop();self._baseline_seek_pending=None;self._alternative_seek_pending=None;self._alternative_wants_play=False;self._baseline_wants_play=False;self._baseline_independent=False
        QApplication.instance().removeEventFilter(self)
        for preview in (self.preview,self.baseline_preview):
            self._pause_preview(preview);preview.clear()
            if hasattr(preview,'preview_audio'):preview.preview_audio.close()
        super().done(result)
