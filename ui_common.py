"""Shared widgets and review controls for the PGM application."""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

from PyQt6.QtCore import Qt, QRectF, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QFormLayout, QLabel, QPushButton, QFileDialog, QLineEdit, QComboBox, QSpinBox,
    QDoubleSpinBox, QCheckBox, QTabWidget, QTableWidget, QTableWidgetItem, QMessageBox,
    QProgressBar, QHeaderView, QGroupBox, QScrollArea)

from core import (CAMERAS, AnalysisError, Segment, analyze, export,
                  probe, read_frame, frames_tc, validate_segments)


def spin(minimum, maximum, value=0):
    control = QSpinBox()
    control.setRange(minimum, maximum)
    control.setValue(value)
    return control


def pixmap(frame):
    rgb = frame[..., ::-1].copy()
    h, w = rgb.shape[:2]
    image = QImage(rgb.data, w, h, rgb.strides[0], QImage.Format.Format_RGB888).copy()
    return QPixmap.fromImage(image)


class Canvas(QWidget):
    selected = pyqtSignal(str, list)
    colors = {'A': '#38bdf8', 'B': '#fbbf24', 'C': '#a78bfa', 'D': '#34d399'}

    def __init__(self):
        super().__init__()
        self.setMinimumSize(640, 360)
        self.image = None
        self._fitted_image = None
        self._fitted_key = None
        self.rois = {}
        self.camera = 'A'
        self.drag_start = None
        self.drag_end = None
        self.setMouseTracking(True)

    def image_rect(self):
        if self.image is None:
            return QRectF()
        size = self.image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        return QRectF((self.width()-size.width())/2, (self.height()-size.height())/2,
                      size.width(), size.height())

    def set_frame(self, frame):
        self.image = pixmap(frame)
        self._fitted_image = None
        self._fitted_key = None
        self.update()

    def set_camera(self, camera):
        if self.camera != camera:
            self.camera = camera
            self.update()

    def clear(self):
        self.image = None
        self._fitted_image = None
        self._fitted_key = None
        self.drag_start = self.drag_end = None
        self.update()

    def fitted_image(self, size):
        # ROI dragging only repaints overlays. Scale a 4K/8K source once for
        # each actual viewport size, instead of on every mouse movement.
        key = (self.image.cacheKey(), size.width(), size.height())
        if key != self._fitted_key:
            self._fitted_image = self.image.scaled(size, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            self._fitted_key = key
        return self._fitted_image

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#111827'))
        if self.image is None:
            painter.setPen(QColor('#cbd5e1'))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, '导入切点 XML 后显示画面')
            return
        rect = self.image_rect()
        painter.drawPixmap(rect.topLeft().toPoint(), self.fitted_image(rect.size().toSize()))
        groups = {}
        for cam, roi in self.rois.items():
            groups.setdefault(tuple(roi), []).append(cam)
        for (x, y, w, h), cameras in groups.items():
            cam = self.camera if self.camera in cameras else cameras[0]
            color=QColor(self.colors[cam]) if cam in self.colors else QColor.fromHsv((ord(cam[0])-65)*47%360,170,235)
            painter.setPen(QPen(color, 3 if self.camera in cameras else 2))
            box = QRectF(rect.x()+x*rect.width(), rect.y()+y*rect.height(), w*rect.width(), h*rect.height())
            painter.drawRect(box)
            title = ' / '.join(cameras) if len(cameras) <= 4 else f'{cam} +{len(cameras)-1}'
            painter.drawText(box.topLeft()+__import__('PyQt6.QtCore', fromlist=['QPointF']).QPointF(5, 18), title)
        if self.drag_start is not None:
            painter.setPen(QPen(QColor('#ffffff'), 2, Qt.PenStyle.DashLine))
            painter.drawRect(QRectF(self.drag_start, self.drag_end).normalized())

    def mousePressEvent(self, event):
        if self.image is not None and self.camera and self.image_rect().contains(event.position()):
            self.drag_start = event.position()
            self.drag_end = event.position()
            self.update()

    def mouseMoveEvent(self, event):
        if self.drag_start is not None:
            rect = self.image_rect()
            from PyQt6.QtCore import QPointF
            self.drag_end = QPointF(max(rect.left(), min(rect.right(), event.position().x())),
                                   max(rect.top(), min(rect.bottom(), event.position().y())))
            self.update()

    def mouseReleaseEvent(self, event):
        if self.drag_start is None:
            return
        rect = self.image_rect()
        box = QRectF(self.drag_start, self.drag_end).normalized().intersected(rect)
        if box.width() >= 12 and box.height() >= 12:
            roi = [(box.x()-rect.x())/rect.width(), (box.y()-rect.y())/rect.height(),
                   box.width()/rect.width(), box.height()/rect.height()]
            self.rois[self.camera] = roi
            self.selected.emit(self.camera, roi)
        self.drag_start = None
        self.update()


class Worker(QThread):
    progress = pyqtSignal(int, str)
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, config, output):
        super().__init__()
        self.config, self.output = config, output

    def run(self):
        try:
            result = analyze(self.config, self.output, self.progress.emit, self.isInterruptionRequested)
            self.done.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('三木的PGM回套软件')
        self.resize(1120, 850)
        self.worker = None
        self.pgm_info = None
        self.last_frame = None
        self.analysis_config = None
        self.analysis_output = None
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        title = QLabel('三木的PGM回套软件')
        title.setStyleSheet('font-size:24px;font-weight:700;')
        layout.addWidget(title)
        toolbar = QHBoxLayout(); self.project_buttons = []
        for label, fn in [('打开项目', self.open_project), ('保存项目', self.save_project)]:
            button = QPushButton(label)
            button.clicked.connect(fn)
            toolbar.addWidget(button); self.project_buttons.append(button)
        toolbar.addStretch()
        layout.addLayout(toolbar)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self.build_files()
        self.build_rois()
        self.build_results()
        self.status = QLabel('')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.setStyleSheet('QPushButton { padding:6px 12px; } QLineEdit { padding:4px; } QGroupBox { font-weight:600; }')

    def guarded(self, fn):
        try:
            return fn()
        except Exception as exc:
            QMessageBox.warning(self, '请检查设置', str(exc))

    def file_row(self, form, label, target, callback=None, directory=False):
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(target)
        button = QPushButton('选择…')
        def choose():
            path = (QFileDialog.getExistingDirectory(self, label) if directory else
                    QFileDialog.getOpenFileName(self, label, '', '视频文件 (*.mp4 *.mov *.mxf *.mkv *.avi);;全部文件 (*)')[0])
            if path:
                target.setText(path)
                if callback:
                    self.guarded(callback)
        button.clicked.connect(choose)
        row.addWidget(button)
        form.addRow(label, box)



    def build_results(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        tools = QHBoxLayout()
        self.result_tools = tools
        self.run_button = QPushButton('识别镜头')
        self.run_button.clicked.connect(lambda: self.guarded(self.start_analysis))
        tools.addWidget(self.run_button)
        self.cancel_button = QPushButton('取消')
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(lambda: self.worker.requestInterruption() if self.worker else None)
        tools.addWidget(self.cancel_button)
        self.export_button = QPushButton('导出代理XML')
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(lambda: self.guarded(self.export_xml))
        tools.addWidget(self.export_button)
        layout.addLayout(tools)
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        self.results = QTableWidget(0, 4)
        self.results.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.results.setHorizontalHeaderLabels(['入点帧', '出点帧', '机位', '状态'])
        self.results.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.results.currentCellChanged.connect(lambda row, col, oldrow, oldcol: self.result_selected())
        layout.addWidget(self.results, 1)
        review = QHBoxLayout()
        self.review_frame = spin(0, 10000000)
        review.addWidget(QLabel('核对帧'))
        review.addWidget(self.review_frame)
        for text, delta in [('前一帧', -1), ('后一帧', 1)]:
            button = QPushButton(text)
            button.clicked.connect(lambda checked=False, d=delta: self.step_review(d))
            review.addWidget(button)
        show = QPushButton('预览')
        show.clicked.connect(lambda: self.guarded(self.show_review))
        review.addWidget(show)
        layout.addLayout(review)
        edit = QHBoxLayout()
        self.edit_tools = edit
        edit.addStretch()
        layout.addLayout(edit)
        pictures = QHBoxLayout()
        self.pgm_picture = QLabel('PGM')
        self.label_picture = QLabel('标号区域')
        for label in (self.pgm_picture, self.label_picture):
            label.setMinimumSize(360, 200)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setStyleSheet('background:#111827;color:white;')
            pictures.addWidget(label)
        layout.addLayout(pictures)
        self.results_page=page
        self.tabs.addTab(page, '识别与导出')






    def remove_sources(self):
        for row in sorted({i.row() for i in self.sources.selectedIndexes()}, reverse=True):
            self.sources.removeRow(row)



    def save_project(self):
        path, _ = QFileDialog.getSaveFileName(self, '保存项目', '', 'JSON (*.json)')
        if path:
            def save():
                c = self.config()
                data = dict(config=c)
                if self.analysis_config is not None:
                    data.update(segments=[asdict(s) for s in self.segments()],
                                analysis_config=self.analysis_config)
                Path(path).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
            self.guarded(save)

    def open_project(self):
        path, _ = QFileDialog.getOpenFileName(self, '打开项目或识别结果', '', 'JSON (*.json)')
        if path:
            self.guarded(lambda:self.restore_project(path))

    def restore_project(self,path):
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        config = data.get('config',data)
        self.apply_config(config)
        if 'segments' in data:
            from cut_xml import timeline_info
            cut = timeline_info(config)
            analysis_config = data.get('analysis_config',config)
            if analysis_config.get('cut_xml_digest') != cut['xml_digest']:
                raise AnalysisError('切点XML已改变，请重新分析。')
            segments = [Segment(**s) for s in data['segments']]
            if [(s.start,s.end) for s in segments] != [(s['start'],s['end']) for s in cut['shots']]:
                raise AnalysisError('识别结果切点与XML不一致，请重新分析。')
            validate_segments(segments,cut['duration'])
            self.analysis_config = analysis_config
            self.analysis_output = Path(path).parent
            self.populate_results(segments)
            self.export_button.setEnabled(True)
            self.tabs.setCurrentWidget(self.results_page)


    def update_progress(self, n, text):
        self.progress.setValue(n)
        self.status.setText(text)

    def finish_worker(self):
        self.results.setEnabled(True); self.review_frame.setEnabled(True)
        self.run_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        for i in range(self.tabs.count()):self.tabs.setTabEnabled(i, True)
        for button in self.project_buttons: button.setEnabled(True)


    def analysis_failed(self, message):
        self.finish_worker()
        self.analysis_config = None
        self.status.setText(message)
        QMessageBox.warning(self, '分析未完成', message)

    def populate_results(self, segments):
        blocked=self.results.blockSignals(True);self.results.setUpdatesEnabled(False)
        try:
            self.results.setRowCount(len(segments))
            for row,s in enumerate(segments):
                for col, value in enumerate((s.start, s.end, s.camera, s.reason)):
                    self.results.setItem(row, col, QTableWidgetItem(str(value)))
                    if s.camera == '?':
                        self.results.item(row, col).setBackground(QColor('#fff0cc'))
                        self.results.item(row, col).setForeground(QColor('#111827'))
        finally:self.results.setUpdatesEnabled(True);self.results.blockSignals(blocked)

    def result_selected(self):
        row = self.results.currentRow()
        if row >= 0 and self.results.item(row, 0):
            try:
                self.review_frame.setValue(int(self.results.item(row, 0).text()))
            except ValueError:
                pass

    def step_review(self, delta):
        self.review_frame.setValue(self.review_frame.value()+delta)
        self.guarded(self.show_review)


    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.requestInterruption()
            self.status.setText('正在取消分析，请等待线程结束后再关闭。')
            event.ignore()
        else:
            event.accept()
