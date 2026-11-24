"""One persistent editing workspace; widgets share the catalogue's snapshot.

The library holds whole PGM files. Proxy and WAV records remain relations in
the catalogue and are expanded only when a PGM is selected, never as widgets.
"""
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QSplitter,
    QStackedWidget, QVBoxLayout, QWidget, QSizePolicy,
)


class WorkspaceDialog(QDialog):
    def __init__(self, panel, flags):
        super().__init__(panel, flags)
        self.panel = panel
        self._return_maximized = True
        self.fullscreen_shortcut = QShortcut(QKeySequence('Ctrl+Meta+F'), self)
        self.fullscreen_shortcut.activated.connect(self.toggle_fullscreen)

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showMaximized() if self._return_maximized else self.showNormal()
        else:
            self._return_maximized = self.isMaximized()
            self.showFullScreen()
        self.panel.browser_fullscreen_button.setText('退出全屏' if self.isFullScreen() else '全屏')

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self.isFullScreen():
            self.toggle_fullscreen()
            event.accept()
        else:
            super().keyPressEvent(event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self.panel.browser_fullscreen_button.setText('退出全屏' if self.isFullScreen() else '全屏')


def build_workspace(panel):
    """Move existing single-source widgets to script | library | preview/editor."""
    panel._workspace_selection = 'track'
    panel._pending_catalogue_cache = None
    panel.story_timeline.set_compact_ruler(True)
    panel.browser_title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    script_side = panel.browser_split.widget(0)
    legacy_side = panel.browser_split.widget(1)
    legacy_side.setParent(panel)
    legacy_side.hide()
    # Keep the hidden logical selector for the existing source routing, rather
    # than duplicating the client, project state, source mapping or player.
    panel.main_pages.hide()
    panel.comparison_actions.hide()
    panel.take_filter_bar.hide()
    panel.match_details_toggle.hide()

    library_side = panel.workspace_library = QWidget()
    library_side.setMinimumWidth(190)
    library = QVBoxLayout(library_side)
    library.setContentsMargins(0, 0, 0, 0)
    library.addWidget(QLabel('媒体池'))
    panel.pending_search.setPlaceholderText('搜索文件／场次／台词')
    library.addWidget(panel.pending_search)
    library.addWidget(panel.pending_mode)
    library.addWidget(panel.pending_count)
    panel.pool_delete_button = panel._button('删除所选PGM', panel.delete_pool_pgm)
    panel.pool_delete_button.setToolTip('仅从媒体池隐藏；PGM、代理、音频及相关数据保留。')
    panel.pool_delete_button.setEnabled(False)
    library.addWidget(panel.pool_delete_button)
    library.addWidget(panel.pending_list, 1)
    panel.pool_delete_shortcuts = []
    for key in ('Delete', 'Backspace'):
        shortcut = QShortcut(QKeySequence(key), panel.pending_list)
        shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        shortcut.setAutoRepeat(False)
        shortcut.activated.connect(lambda: panel.guarded(panel.delete_pool_pgm))
        panel.pool_delete_shortcuts.append(shortcut)
    panel.pending_list.currentRowChanged.connect(lambda _row: panel._refresh_pool_delete_button())
    from take_cards import TakeCardDelegate
    from library_thumbnails import ThumbnailLoader, decode_thumbnail
    panel.pending_list.setItemDelegate(TakeCardDelegate(panel.pending_list))
    panel.pending_thumbnails = ThumbnailLoader(panel.pending_list, panel,
        decoder=lambda path, seconds: decode_thumbnail(path, seconds, 320, 180))
    panel.pending_list.itemClicked.connect(lambda _item: panel.guarded(panel._workspace_pending_clicked))

    panel.workspace_inspector = QWidget()
    inspector = QVBoxLayout(panel.workspace_inspector)
    inspector.setContentsMargins(0, 0, 0, 0)
    inspector.setSpacing(6)
    row = QHBoxLayout()
    panel.workspace_clip_label = QLabel('选择轨道或素材库中的PGM')
    panel.workspace_clip_label.setMinimumWidth(0)
    panel.workspace_clip_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    row.addWidget(panel.workspace_clip_label, 1)
    panel.workspace_play = panel._button('播放／暂停', panel.preview_workspace_selection)
    row.addWidget(panel.workspace_play)
    row.addWidget(QLabel('画面'))
    panel.workspace_angles = QStackedWidget()
    panel.workspace_angles.setMaximumWidth(220)
    panel.workspace_angles.addWidget(panel.angle_choice)
    panel.workspace_angles.addWidget(panel.pending_angle_choice)
    row.addWidget(panel.workspace_angles)
    inspector.addLayout(row)
    row = QHBoxLayout()
    row.addWidget(QLabel('台本区间'))
    panel.coverage_range_input.setMaximumWidth(180)
    row.addWidget(panel.coverage_range_input, 1)
    row.addWidget(panel.coverage_apply_button)
    row.addStretch(1)
    inspector.addLayout(row)
    panel.workspace_review_button = panel._button('确认区间核实', panel.review_workspace_clip)
    panel.workspace_review_button.setToolTip('人工确认当前完整PGM的台本区间；绿色备选自动参与XML导出。')
    inspector.addWidget(panel.workspace_review_button, 0, Qt.AlignmentFlag.AlignLeft)
    panel.workspace_hint = QLabel('单击轨道预览；输入区间调整位置；Delete退回素材库。')
    panel.workspace_hint.setObjectName('mutedLabel')
    panel.workspace_hint.setWordWrap(True)
    inspector.addWidget(panel.workspace_hint)
    panel.workspace_inspector.hide()

    right = panel.workspace_right = QSplitter(Qt.Orientation.Vertical)
    right.setChildrenCollapsible(False)
    right.setHandleWidth(6)
    preview_side = QWidget()
    preview_box = QVBoxLayout(preview_side)
    preview_box.setContentsMargins(0, 0, 0, 0)
    preview_box.addWidget(panel.inline_host, 1)
    preview_box.addWidget(panel.workspace_inspector)
    preview_side.setMinimumHeight(250)
    panel.story_timeline.setMinimumHeight(220)
    right.addWidget(preview_side)
    right.addWidget(panel.story_timeline)
    right.setStretchFactor(0, 3)
    right.setStretchFactor(1, 2)
    right.setSizes([510, 340])
    right.handle(1).setToolTip('上下拖动调整预览与PGM轨道的大小')
    panel.browser_split.addWidget(library_side)
    panel.browser_split.addWidget(right)
    script_side.setMinimumWidth(180)
    panel.browser_split.setStretchFactor(0, 1)
    panel.browser_split.setStretchFactor(1, 1)
    panel.browser_split.setStretchFactor(2, 4)
    panel.browser_split.setSizes([230, 230, 940])
    panel.browser_split.setMinimumHeight(480)
    panel.browser_split.handle(1).setToolTip('调整台本和素材库宽度')
    panel.browser_split.handle(2).setToolTip('调整素材库和剪辑区域宽度')
    panel.story_browser_split.hide()
    panel.story_browser_split.setParent(panel)
    panel.catalogue_page.layout().addWidget(panel.browser_split, 1)
    panel.browser_fullscreen_button = QPushButton('全屏')
    panel.browser_fullscreen_button.clicked.connect(lambda: panel._browser_dialog and
                                                    panel._browser_dialog.toggle_fullscreen())
    panel.catalogue_page.layout().itemAt(0).layout().insertWidget(1, panel.browser_fullscreen_button)
    panel.story_timeline.take_remove_requested.connect(lambda identity: panel.guarded(
        lambda: panel.remove_pgm_match(candidate_id=identity)))
