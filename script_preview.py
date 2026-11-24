"""Read-only script cards: canonical numbering and no media relationships.

The model stores one small record per script line. Native batched list layout
and a bounded text-layout cache keep painting independent of the media pool.
"""
from collections import OrderedDict
import re
from PyQt6 import sip

from PyQt6.QtCore import (QAbstractListModel, QEvent, QModelIndex, QPoint,
                         QPointF, QRectF, QSize, Qt, QTimer)
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen
from PyQt6.QtWidgets import (QAbstractItemView, QHBoxLayout, QLabel, QLineEdit,
                            QListView, QStyle, QStyledItemDelegate, QVBoxLayout,
                            QWidget)

from script_timeline import _WrappedText


NODE_ROLE = int(Qt.ItemDataRole.UserRole)+101
COVERED_ROLE = NODE_ROLE+1
SCENE_ROLE = NODE_ROLE+2


class ScriptPreviewModel(QAbstractListModel):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.nodes=[];self.by_id={};self.highlighted=set();self.haystacks=[]

    def set_nodes(self,nodes):
        values=[];seen=set();previous=None
        for number,raw in enumerate(nodes,1):
            identity=str(raw.get('id') or raw.get('script_line_id') or '')
            if not identity or identity in seen:
                raise ValueError('台本节点缺少唯一编号，或编号重复。')
            seen.add(identity)
            scene=str(raw.get('scene') or '')
            group=(str(raw.get('script_id') or ''),scene)
            values.append(dict(id=identity,number=number,
                speaker=str(raw.get('speaker') or raw.get('character') or ''),
                text=str(raw.get('text') or raw.get('dialogue') or raw.get('content') or ''),
                scene=scene,scene_heading=scene if scene and group!=previous else ''))
            previous=group
        self.beginResetModel()
        self.nodes=values;self.by_id={row['id']:i for i,row in enumerate(values)}
        self.highlighted.intersection_update(self.by_id)
        self.haystacks=[' '.join((row['speaker'],row['text'],row['scene'])).casefold() for row in values]
        self.endResetModel()

    def rowCount(self,parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.nodes)

    def data(self,index,role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0<=index.row()<len(self.nodes):return None
        row=self.nodes[index.row()]
        if role==NODE_ROLE:return row
        if role==COVERED_ROLE:return row['id'] in self.highlighted
        if role==SCENE_ROLE:return row['scene_heading']
        if role in (Qt.ItemDataRole.DisplayRole,Qt.ItemDataRole.AccessibleTextRole,Qt.ItemDataRole.ToolTipRole):
            return f'{row["number"]}  '+(row['speaker']+'：' if row['speaker'] else '')+row['text']
        if role==Qt.ItemDataRole.UserRole:return row['id']


class ScriptPreviewDelegate(QStyledItemDelegate):
    """The old script typography and card colors without statistics/badges."""
    CACHE_LIMIT=512
    INSET=16

    def __init__(self,parent=None):
        super().__init__(parent)
        self._cache=OrderedDict();self._font_cache=OrderedDict()

    def clear_cache(self):
        self._cache.clear();self._font_cache.clear()

    def _fonts(self,font):
        key=font.toString()
        value=self._font_cache.pop(key,None)
        if value is None:
            body=QFont(font);body.setPointSizeF(max(11.,body.pointSizeF()))
            caption=QFont(body);caption.setPointSizeF(max(10.,body.pointSizeF()-1));caption.setBold(True)
            heading=QFont(body);heading.setBold(True)
            value=dict(body=body,caption=caption,heading=heading,
                       body_key=body.toString(),heading_key=heading.toString(),
                       caption_metrics=QFontMetricsF(caption))
        self._font_cache[key]=value
        while len(self._font_cache)>8:self._font_cache.popitem(last=False)
        return value

    def _text(self,text,font,font_key,width):
        # QTextLayout needs a Unicode line separator for explicit paragraphs.
        text=str(text).replace('\r\n','\n').replace('\r','\n').replace('\n','\u2028')
        key=(text,font_key,round(width))
        value=self._cache.pop(key,None)
        if value is None:value=_WrappedText(text,font,width)
        self._cache[key]=value
        while len(self._cache)>self.CACHE_LIMIT:self._cache.popitem(last=False)
        return value

    def _geometry(self,option,index):
        view=self.parent()
        width=max(80,(view.viewport().width() if isinstance(view,QListView) else option.rect.width())-self.INSET*2)
        node=index.data(NODE_ROLE) or {}
        fonts=self._fonts(option.font)
        body=self._text(node.get('text',''),fonts['body'],fonts['body_key'],width)
        caption=f'{node.get("number","")}  {node.get("speaker","")}'.rstrip()
        metrics=fonts['caption_metrics']
        caption_layout=None
        caption_height=metrics.height()+2
        if metrics.horizontalAdvance(caption)>width:
            caption_layout=self._text(caption,fonts['caption'],fonts['caption'].toString(),width)
            caption_height=caption_layout.height
        # This is already present in the plain node snapshot. Do not revisit
        # QModelIndex after text shaping allocations can run Python GC.
        scene=node.get('scene_heading') or ''
        heading=self._text(scene,fonts['heading'],fonts['heading_key'],width) if scene else None
        scene_height=heading.height+18 if heading else 0
        return dict(fonts=fonts,width=width,body=body,caption=caption,
                    caption_layout=caption_layout,caption_height=caption_height,
                    heading=heading,scene_height=scene_height,
                    height=round(scene_height+12+caption_height+5+body.height+16))

    def sizeHint(self,option,index):
        # Qt invokes Python delegates from its pending batched layout without
        # a Python reference to the containing window. Text shaping can run GC
        # and otherwise collect a closed window/model midway through this call.
        view=self.parent()
        lifetime=(view,view.window() if isinstance(view,QWidget) else None,index.model())
        return QSize(max(80,option.rect.width()),self._geometry(option,index)['height'])

    def paint(self,painter,option,index):
        view=self.parent()
        lifetime=(view,view.window() if isinstance(view,QWidget) else None,index.model())
        covered=bool(index.data(COVERED_ROLE))
        geometry=self._geometry(option,index)
        painter.save();painter.setClipRect(option.rect);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect=QRectF(option.rect);x=rect.left()+self.INSET;y=rect.top()
        if geometry['heading']:
            painter.fillRect(QRectF(rect.left()+7,y+3,rect.width()-14,geometry['scene_height']-6),QColor('#eef2f6'))
            painter.setPen(QColor('#33465b'));geometry['heading'].draw(painter,QPointF(x,y+9))
            y+=geometry['scene_height']
        card=QRectF(rect.left()+7,y+3,rect.width()-14,rect.bottom()-y-6)
        selected=bool(option.state & QStyle.StateFlag.State_Selected)
        hover=bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.setBrush(QColor('#cce3fb' if covered else '#e9f2fc' if selected else '#f5f8fb' if hover else '#ffffff'))
        painter.setPen(QPen(QColor('#a7c5e7' if covered or selected else '#edf1f5'),1))
        painter.drawRoundedRect(card,7,7)
        y+=12
        painter.setPen(QColor('#536a80'));painter.setFont(geometry['fonts']['caption'])
        if geometry['caption_layout']:geometry['caption_layout'].draw(painter,QPointF(x,y))
        else:painter.drawText(QRectF(x,y,geometry['width'],geometry['caption_height']),Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter,geometry['caption'])
        y+=geometry['caption_height']+5
        painter.setPen(QColor('#24384c'));geometry['body'].draw(painter,QPointF(x,y))
        if option.state & QStyle.StateFlag.State_HasFocus:
            painter.setBrush(Qt.BrushStyle.NoBrush);painter.setPen(QPen(QColor('#7da5ce'),1,Qt.PenStyle.DotLine));painter.drawRoundedRect(card,7,7)
        painter.restore()


class _ScriptList(QListView):
    def __init__(self,parent=None):
        super().__init__(parent)
        self._resize_anchor=None
        self._anchor_retries=0
        self._last_reading_anchor=None
        self._suspended=False
        self._scroll_timer=QTimer(self);self._scroll_timer.setSingleShot(True)
        self._scroll_timer.timeout.connect(self._remember_anchor)
        self._resize_timer=QTimer(self);self._resize_timer.setSingleShot(True);self._resize_timer.setInterval(40)
        self._resize_timer.timeout.connect(self._restore_anchor)
        self.setLayoutMode(QListView.LayoutMode.Batched);self.setBatchSize(64)
        self.setResizeMode(QListView.ResizeMode.Adjust);self.setUniformItemSizes(False)
        self.setWordWrap(True);self.setSpacing(0)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.setStyleSheet('QListView { background:#ffffff; border:1px solid #dce3ea; border-radius:7px; }')
        self.setAccessibleName('台本预览')
        self.verticalScrollBar().valueChanged.connect(self._schedule_anchor)

    def _alive(self):
        return not sip.isdeleted(self) and not self._suspended

    def _schedule_anchor(self,_value=0):
        if self._alive() and self.isVisible():self._scroll_timer.start()

    def _remember_anchor(self):
        if self._alive() and self.isVisible():self._last_reading_anchor=self.reading_anchor()

    def reading_anchor(self):
        index=self.indexAt(QPoint(max(1,self.viewport().width()//2),1))
        if not index.isValid():index=self.currentIndex()
        return (index.data(Qt.ItemDataRole.UserRole),self.visualRect(index).top()) if index.isValid() else None

    def resizeEvent(self,event):
        # indexAt/visualRect can execute Qt's pending batched layout. Calling
        # them inside resizeEvent would re-enter the list's layout engine.
        if self._resize_anchor is None:
            self._resize_anchor=self._last_reading_anchor;self._anchor_retries=0
        super().resizeEvent(event)
        if self._alive() and self.isVisible():self._resize_timer.start()

    def _restore_anchor(self):
        if not self._alive() or not self.isVisible():return
        anchor=self._resize_anchor
        if not anchor or self.model() is None:return
        row=self.model().by_id.get(anchor[0])
        if row is None:self._resize_anchor=None;return
        # Qt ignores scrollTo for a row not yet in its batched geometry.
        # Retry outside layout/paint callbacks; never force a full sync layout.
        ready=self.indexAt(QPoint(max(1,self.viewport().width()//2),1)).isValid()
        if not ready:
            self._anchor_retries+=1
            if self._anchor_retries<100:self._resize_timer.start()
            return
        self._resize_anchor=None;self._anchor_retries=0
        index=self.model().index(row,0)
        self.scrollTo(index,QAbstractItemView.ScrollHint.PositionAtTop)
        self.verticalScrollBar().setValue(self.verticalScrollBar().value()-anchor[1])

    def hideEvent(self,event):
        self._suspended=True;self._scroll_timer.stop();self._resize_timer.stop()
        super().hideEvent(event)

    def showEvent(self,event):
        self._suspended=False
        super().showEvent(event)
        if self._resize_anchor:self._resize_timer.start()

    def changeEvent(self,event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange,QEvent.Type.ApplicationFontChange,QEvent.Type.StyleChange):
            delegate=self.itemDelegate()
            if isinstance(delegate,ScriptPreviewDelegate):delegate.clear_cache()
            self.scheduleDelayedItemsLayout()


class ScriptPreview(QWidget):
    """Display the entire script; locating text never filters or renumbers it."""
    def __init__(self,parent=None):
        super().__init__(parent)
        box=QVBoxLayout(self);box.setContentsMargins(0,0,0,0);box.setSpacing(5)
        self.search=QLineEdit();self.search.setPlaceholderText('查找行号／人物／台词')
        self.search.setClearButtonEnabled(True);box.addWidget(self.search)
        self.status=QLabel();self.status.setWordWrap(True);self.status.setStyleSheet('color:#667b8e;');box.addWidget(self.status)
        self.model=ScriptPreviewModel(self);self.view=_ScriptList(self);self.view.setModel(self.model)
        self.delegate=ScriptPreviewDelegate(self.view);self.view.setItemDelegate(self.delegate);box.addWidget(self.view,1)
        self._matches=[];self._match_position=0
        self.search.textChanged.connect(self._find);self.search.returnPressed.connect(self.find_next)
        self.setFocusProxy(self.view);self.set_nodes([])

    def set_nodes(self,nodes):
        anchor=self.view.reading_anchor()
        current=self.view.currentIndex().data(Qt.ItemDataRole.UserRole)
        self.model.set_nodes(nodes);self.delegate.clear_cache()
        if current in self.model.by_id:self.view.setCurrentIndex(self.model.index(self.model.by_id[current],0))
        if anchor and anchor[0] in self.model.by_id:
            self.view._resize_anchor=anchor;self.view._resize_timer.start()
        self._find(self.search.text(),locate=False)

    @property
    def highlighted_line_ids(self):
        return set(self.model.highlighted)

    def highlight_ranges(self,ranges):
        numbers=set();first=None;count=self.model.rowCount()
        for bounds in ranges:
            if not bounds or len(bounds)!=2:continue
            start,end=bounds
            if type(start) is not int or type(end) is not int or not 1<=start<=end<=count:continue
            if first is None:first=start
            numbers.update(range(start-1,end))
        self.model.highlighted={self.model.nodes[row]['id'] for row in numbers}
        # Coverage changes only painting; emitting size-related dataChanged
        # would unnecessarily lay out the entire long script again.
        self.view.viewport().update()
        if first is not None:self.scroll_to_line(first)

    def scroll_to_line(self,number):
        if type(number) is not int or not 1<=number<=self.model.rowCount():return False
        index=self.model.index(number-1,0)
        self.view._resize_timer.stop();self.view._resize_anchor=None
        self.view.setCurrentIndex(index)
        self.view.scrollTo(index,QAbstractItemView.ScrollHint.PositionAtTop)
        self.view._resize_timer.stop();self.view._resize_anchor=None
        self.view._last_reading_anchor=(self.model.nodes[number-1]['id'],0)
        self.view._resize_anchor=self.view._last_reading_anchor;self.view._anchor_retries=0
        self.view._resize_timer.start()
        self.view._scroll_timer.start()
        return True

    def _find(self,text,*,locate=True):
        query=text.strip().casefold();self._matches=[];self._match_position=0
        if not query:
            self.status.setText(f'共 {self.model.rowCount()} 行台本' if self.model.rowCount() else '尚未导入台本')
            return
        number=re.fullmatch(r'(?:台本|第)?\s*(\d+)\s*行?',query)
        if number:
            row=int(number[1])-1
            if 0<=row<self.model.rowCount():self._matches=[row]
        else:self._matches=[row for row,words in enumerate(self.model.haystacks) if query in words]
        if not self._matches:
            self.status.setText('未找到；完整台本仍保留显示。');return
        if locate:self.scroll_to_line(self._matches[0]+1)
        self._search_status()

    def _search_status(self):
        number=self._matches[self._match_position]+1
        self.status.setText(f'找到 {len(self._matches)} 处 · 台本 {number}'+(' · 回车下一处' if len(self._matches)>1 else ''))

    def find_next(self):
        if not self._matches:return
        self._match_position=(self._match_position+1)%len(self._matches)
        self.scroll_to_line(self._matches[self._match_position]+1);self._search_status()
