"""Single-workstation project entry; media is referenced without copying."""
from __future__ import annotations

import copy
from pathlib import Path

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                            QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget)


class ProjectEntryDialog(QDialog):
    def __init__(self, productions, selected_id=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle('打开项目')
        self.resize(500, 230)
        self.productions = copy.deepcopy(productions)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(14)
        heading = QLabel('新建项目或继续已有项目')
        heading.setObjectName('pageTitle')
        layout.addWidget(heading)
        self.form = QFormLayout()
        self.form.setSpacing(12)
        self.mode = QComboBox()
        self.mode.addItem('新建项目', 'new')
        self.mode.addItem('打开已有项目', 'existing')
        self.mode.addItem('打开项目文件', 'file')
        self.form.addRow('打开方式', self.mode)
        self.name = QLineEdit()
        self.name.setPlaceholderText('例如：示例剧目')
        self.form.addRow('新项目名称', self.name)
        self.existing = QComboBox()
        for index, production in enumerate(self.productions):
            label = production['name']
            if sum(row['name'] == label for row in self.productions) > 1:
                label += f' · 项目 {index + 1}'
            self.existing.addItem(label, production['id'])
        index = self.existing.findData(selected_id)
        if index >= 0:
            self.existing.setCurrentIndex(index)
        self.form.addRow('已有项目', self.existing)
        self.file = QLineEdit()
        self.file.setPlaceholderText('选择已保存的 .pgmproj 文件')
        file_row = QWidget(); row = QHBoxLayout(file_row); row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.file)
        browse = QPushButton('选择…'); browse.clicked.connect(self.choose_file); row.addWidget(browse)
        self.file_row = file_row
        self.form.addRow('项目文件', file_row)
        layout.addLayout(self.form)
        self.validation = QLabel('')
        self.validation.setWordWrap(True)
        layout.addWidget(self.validation)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('打开项目')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.mode.currentIndexChanged.connect(self.update_mode)
        if self.productions:
            self.mode.setCurrentIndex(1)
        else:
            self.mode.model().item(1).setEnabled(False)
        self.update_mode()

    def update_mode(self, *_args):
        new = self.mode.currentData() == 'new'
        self.form.setRowVisible(self.name, new)
        self.form.setRowVisible(self.existing, self.mode.currentData() == 'existing')
        self.form.setRowVisible(self.file_row, self.mode.currentData() == 'file')
        self.validation.clear()
        if new:
            self.name.setFocus()

    def choose_file(self):
        path, _ = QFileDialog.getOpenFileName(self, '打开项目文件', str(Path.home()/'Desktop'),
                                            'PGM 项目 (*.pgmproj)')
        if path:
            self.file.setText(path)

    def selection(self):
        if self.mode.currentData() == 'new':
            return {'mode': 'new', 'name': self.name.text().strip()}
        if self.mode.currentData() == 'file':
            return {'mode': 'file', 'path': self.file.text().strip()}
        return {'mode': 'existing', 'production_id': self.existing.currentData()}

    def accept(self):
        selected = self.selection()
        if selected['mode'] == 'new' and not selected['name']:
            self.validation.setText('请填写新项目名称。')
        elif selected['mode'] == 'existing' and not selected['production_id']:
            self.validation.setText('请选择已有项目。')
        elif selected['mode'] == 'file' and not selected['path']:
            self.validation.setText('请选择项目文件。')
        else:
            # Names are display labels. Project UUIDs and file paths determine
            # identity; a recent-file index must never reserve a deleted name.
            super().accept()


def _connect_library(current_client, actor, progress, cancelled):
    from local_project import LocalProjectClient, recent_projects
    from library_bridge import read_settings
    if cancelled():
        raise ValueError('已取消。')
    client = current_client if getattr(current_client, 'is_local_project', False) is True else LocalProjectClient()
    progress(20, '读取本机项目列表…')
    productions = client.call('list_productions')
    recent = recent_projects()
    indexed = {row['id']: row for row in recent}
    productions = [dict(indexed.get(row['id'], {}), **row) for row in productions]
    known = {row['id'] for row in productions}
    productions.extend(row for row in recent if row['id'] not in known)
    return client, productions, read_settings().get('last_local_project')


def load_project_selection(selected, current_client, productions):
    """Load a complete project or read-only legacy migration, without a service."""
    from local_project import LocalProjectClient, import_legacy_project
    from project_state import read_project_file, read_project_state
    state = None
    client = None
    if selected['mode'] == 'new':
        client = LocalProjectClient()
        production = client.call('create_production', name=selected['name'])
        return client, production, None
    if selected['mode'] == 'file':
        state = read_project_file(selected['path'])
        identity = state['production_id']
    else:
        identity = selected['production_id']
        current = next((r for r in current_client.call('list_productions') if r['id'] == identity), None)
        row = next((r for r in productions if r['id'] == identity), None)
        if row is None:
            raise ValueError('所选项目已不可用，请重新选择项目文件。')
        if row.get('project_path'):
            # Reload the saved file even for the active project: Discard must
            # not secretly retain its unsaved in-memory edits.
            state = read_project_file(row['project_path'])
            # The file may have been deleted and recreated with a new UUID.
            # Its contents, rather than a cached recent entry, own identity.
            identity = state['production_id']
        elif current is not None:
            return current_client, current, read_project_state(identity)
        else:
            state = read_project_state(identity)
    if state and state.get('version') == 2:
        client = LocalProjectClient.from_project(state['catalogue'])
    else:
        client = import_legacy_project(identity)
    try:
        production = next(r for r in client.call('list_productions') if r['id'] == identity)
        return client, production, state
    except Exception:
        client.close()
        raise


def _after_idle(panel, callback):
    if getattr(panel, '_closed', False):
        return
    if panel.is_busy() or panel.external_busy:
        QTimer.singleShot(10, lambda: _after_idle(panel, callback))
    else:
        panel.guarded(callback)


def start_project(panel, parent=None, on_open=None):
    """Read local recent files, then ask for a named new or existing project.

    The callback receives the opened production dict on the GUI thread. Cancel
    preserves an already-open project; during initial startup it closes parent.
    This function never creates an implicit/default production.
    """
    parent = parent or panel.window()
    previous_id = panel.production_id()
    actor = panel.actor.text().strip()  # Capture Qt values before entering workers.
    current_client = panel.client

    def connected(result):
        client, productions, last_id = result
        def choose():
            dialog = ProjectEntryDialog(productions, previous_id or last_id, parent)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                dialog.deleteLater()
                if not previous_id:
                    parent.close()
                return
            selected = dialog.selection()
            dialog.deleteLater()
            def operation(progress, cancelled):
                if cancelled():
                    raise ValueError('已取消。')
                opened_client, production, project_state = load_project_selection(selected, client, productions)
                try:
                    from library_workflow import BatchWorkflow
                    progress(50, '载入项目素材与台本…')
                    data = BatchWorkflow(opened_client, production['id'], opened_client.actor).read_all()
                    if cancelled():
                        raise ValueError('已取消。')
                    if project_state and project_state.get('version') == 2 and project_state.get('project_path'):
                        from local_project import remember_project
                        remember_project(production, project_state['project_path'])
                    return opened_client, production, data, project_state
                except Exception:
                    if opened_client is not client:
                        opened_client.close()
                    raise
            def opened(result):
                opened_client, production, data, project_state = result
                old_client = panel.client
                panel.production.blockSignals(True)
                panel.production.clear()
                panel.production.addItem(production['name'], production['id'])
                panel.production.blockSignals(False)
                panel._clear_data()
                panel.apply_workflow_context(opened_client, production['id'], production['name'])
                panel._set_data(data)
                panel.status.setText('项目已打开：' + production['name'])
                if on_open:
                    opened_production = copy.deepcopy(production)
                    if project_state:
                        opened_production['project_state'] = project_state
                    on_open(opened_production)
                if old_client is not opened_client and getattr(old_client, 'is_local_project', False) is True:
                    old_client.close()
            panel._run('打开项目…', operation, opened)
        _after_idle(panel, choose)
    return panel._run('读取项目列表…', lambda progress, cancelled: _connect_library(
        current_client, actor, progress, cancelled), connected)
