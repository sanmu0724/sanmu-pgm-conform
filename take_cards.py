"""Lightweight thumbnail cards: no per-row widgets or transcript paragraphs."""
from PyQt6.QtCore import QEvent, QRect, QSize, Qt
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QStyledItemDelegate, QStyle, QStyleOptionButton


class TakeCardDelegate(QStyledItemDelegate):
    def __init__(self, view):
        super().__init__(view)
        self.view = view
        view.viewport().installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Resize:
            self.view.scheduleDelayedItemsLayout()
        return False

    def sizeHint(self, option, index):
        width = max(100, self.view.viewport().width() - 18)
        return QSize(width, round(width * 9 / 16) + 52)

    @staticmethod
    def check_rect(rect):
        return QRect(rect.right() - 29, rect.bottom() - 33, 22, 22)

    def paint(self, painter, option, index):
        painter.save()
        rect = option.rect.adjusted(3, 3, -3, -3)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.setBrush(QColor('#eaf3fc' if selected else '#ffffff'))
        painter.setPen(QColor('#2076bd' if selected else '#d5dfeb'))
        painter.drawRoundedRect(rect, 7, 7)
        picture = QRect(rect.left()+5, rect.top()+5, rect.width()-10, max(1, rect.height()-55))
        painter.fillRect(picture, QColor('#18232e'))
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        if icon:
            pixmap = icon.pixmap(picture.size())
            target = pixmap.size().scaled(picture.size(), Qt.AspectRatioMode.KeepAspectRatio)
            destination = QRect(0, 0, target.width(), target.height())
            destination.moveCenter(picture.center())
            painter.drawPixmap(destination, pixmap)
        text_rect = QRect(rect.left()+9, picture.bottom()+7, rect.width()-18, 40)
        if index.flags() & Qt.ItemFlag.ItemIsUserCheckable:
            text_rect.adjust(0, 0, -27, 0)
            check = QStyleOptionButton()
            check.rect = self.check_rect(rect)
            check.state = QStyle.StateFlag.State_Enabled | (
                QStyle.StateFlag.State_On if index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked.value
                else QStyle.StateFlag.State_Off)
            self.view.style().drawControl(QStyle.ControlElement.CE_CheckBox, check, painter, self.view)
        painter.setPen(option.palette.color(QPalette.ColorRole.Text))
        lines = str(index.data(Qt.ItemDataRole.DisplayRole) or '').splitlines()
        for number, text in enumerate(lines[:2]):
            shortened = option.fontMetrics.elidedText(text, Qt.TextElideMode.ElideRight, text_rect.width())
            painter.drawText(text_rect.adjusted(0, number*20, 0, 0),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, shortened)
        painter.restore()

    def editorEvent(self, event, model, option, index):
        if (event.type() == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton
                and index.flags() & Qt.ItemFlag.ItemIsUserCheckable
                and self.check_rect(option.rect.adjusted(3, 3, -3, -3)).contains(event.position().toPoint())):
            checked = index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked.value
            return model.setData(index, Qt.CheckState.Unchecked.value if checked else Qt.CheckState.Checked.value,
                                 Qt.ItemDataRole.CheckStateRole)
        return False
