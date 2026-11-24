"""PGM camera-label OCR and Resolve 21 XML timeline UI."""
from __future__ import annotations
import json
import sys
import copy
import uuid
from bisect import bisect_right
from dataclasses import asdict,dataclass
from pathlib import Path
from datetime import datetime
import numpy as np
from PIL import Image
from PyQt6.QtCore import Qt,QThread,pyqtSignal,QTimer,QSignalBlocker
from PyQt6.QtWidgets import (QApplication,QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,
    QSlider,QLabel,QPushButton,QLineEdit,QComboBox,QCheckBox,QGroupBox,QTableWidget,
    QTableWidgetItem,QDoubleSpinBox,QHeaderView,QFileDialog,QMessageBox,QProgressBar,QInputDialog,QTabWidget,QToolButton,QScrollArea,QStyledItemDelegate,QSplitter,QSizePolicy)
from ui_common import Window as BaseWindow,Canvas,Worker,spin,pixmap
from core import CAMERAS,AnalysisError,Segment,probe,read_frame,validate_rate,roi_pixels,tc_frames,export,validate_segments
from ocr import LabelReader
from camera_schema import cameras_for

@dataclass
class PreparedSession:
    identity:tuple
    config:dict
    cut:dict|None
    sound_infos:dict
    focus_frame:int|None=None
REASONS = {'identity_unconfirmed':'机位修改未确认','sample_conflict':'采样结果不一致，需核查','label_read':'读到机位号','A_inferred_blank':'无标号区域，按本场设置判为A',
    'sample_incomplete':'部分采样漏读，证据不足，需核查','label_read_consensus':'补采样一致，已自动确认',
    'unreadable_label':'文字无法可靠识别','multiple_labels':'同时读到多个机位号',
    'no_label':'未见标号','manual':'已人工确认','source_gap':'代理时码缺口','source_overlap':'代理时码冲突'}


class FilenameDelegate(QStyledItemDelegate):
    """Readable names while the underlying cells retain their full source paths."""
    def __init__(self,columns,parent=None):
        super().__init__(parent);self.columns=set(columns)

    def initStyleOption(self,option,index):
        super().initStyleOption(option,index)
        if index.column() in self.columns and option.text:
            option.text=Path(option.text).name


class ReviewPicture(QLabel):
    """Keep the decoded frame while a splitter resizes its fitted preview."""
    def __init__(self,text,parent=None):
        super().__init__(text,parent)
        self._review_pixmap=None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Ignored)
        self.setStyleSheet('background:#111827;color:white;border-radius:5px;')

    def setPixmap(self,picture):
        self._review_pixmap=picture
        self._fit_picture()

    def _fit_picture(self):
        if self._review_pixmap is not None and not self._review_pixmap.isNull():
            super().setPixmap(self._review_pixmap.scaled(self.contentsRect().size(),
                Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self,event):
        super().resizeEvent(event)
        self._fit_picture()

    def clear(self):
        self._review_pixmap=None
        super().clear()


def camera_preview_pixels(frame, maximum=(1920, 1080)):
    """Bound GUI image conversion without reducing the full-resolution OCR input."""
    height, width = frame.shape[:2]
    if width <= maximum[0] and height <= maximum[1]:
        return frame
    picture = Image.fromarray(frame[..., ::-1])
    picture.thumbnail(maximum, Image.Resampling.BILINEAR)
    return np.asarray(picture)[..., ::-1].copy()


def decode_camera_preview(path, index):
    frame = read_frame(path, index)
    return dict(frame=frame, display=camera_preview_pixels(frame))


class SoundWorker(QThread):
    progress=pyqtSignal(int,str)
    done=pyqtSignal(object)
    failed=pyqtSignal(str)

    def __init__(self,config,parent=None):
        super().__init__(parent);self.config=config

    def run(self):
        try:
            from cut_xml import timeline_info
            from sound_sync import sound_plan
            cut=timeline_info(self.config,require_tc=self.config.get('sound_sync_mode')=='timecode')
            result=sound_plan(self.config,cut,self.progress.emit,self.isInterruptionRequested)
            self.done.emit(result)
        except Exception as exc:self.failed.emit(str(exc))


class CameraAnalysisWorker(Worker):
    """Validate media headers on the worker, including large shared-storage scenes."""
    def run(self):
        try:
            from cut_xml import timeline_info
            self.progress.emit(0,'正在核对本场机位与素材…')
            cut=timeline_info(self.config)
            if cut['xml_digest']!=self.config['cut_xml_digest']:raise AnalysisError('切点XML已改变，请重新导入。')
            info=cut['pgm_info']
            validate_rate(info,self.config['fps'])
            tc_frames(self.config['pgm_start_tc'] or info['start_tc'],self.config['fps'])
            validate_segments([Segment(shot['start'],shot['end'],'?','manual') for shot in cut['shots']],cut['duration'])
            if self.isInterruptionRequested():raise AnalysisError('已取消。')
        except Exception as exc:
            self.failed.emit(str(exc));return
        super().run()


class ExportWorker(QThread):
    progress=pyqtSignal(int,str)
    done=pyqtSignal(object)
    failed=pyqtSignal(str)

    def __init__(self,config,segments,output,parent=None,library_client=None):
        super().__init__(parent);self.config,self.segments,self.output=config,segments,output
        self.library_client=library_client

    def run(self):
        try:
            context=self.config.get('library_context',{})
            if self.library_client and context.get('session_id'):
                from library_workflow import BatchWorkflow
                candidates=[row for row in BatchWorkflow(self.library_client,context['production_id']).read_all()['candidates'] if row['status']=='confirmed']
                from library_bridge import resolve_local_path
                self.config['dialogue_markers']=[dict(row,media_path=resolve_local_path(row['media_path'])) for row in candidates if row['session_id']==context['session_id']]
            events,xml=export(self.config,self.segments,self.output,self.progress.emit,self.isInterruptionRequested)
            if self.isInterruptionRequested():raise AnalysisError('已取消导出。')
            (self.output/'reviewed_analysis.json').write_text(json.dumps(dict(config=self.config,segments=[asdict(s) for s in self.segments]),ensure_ascii=False,indent=2),encoding='utf-8')
            self.done.emit((events,xml,self.config))
        except Exception as exc:self.failed.emit(str(exc))


class Window(BaseWindow):
    def __init__(self,prompt_project=False):
        self.prompt_project=prompt_project;self.project_identity=None;self.project_path='';self._closing_after_save=False
        self._saved_project_checkpoint=None
        self._close_prompt_active=False;self._recording_prompt_active=False;self._project_save_in_progress=False
        self.loaded_pgm = ''; self.finder = None; self.example_frames = {}; self.preview_shots = []
        self.sound_worker=None
        self.library_context={};self.library_media=[];self.library_notes='';self.source_id_map={};self.dialogue_markers=[];self.library_extra={};self.config_extra={}
        self.library_materials_stale=False
        self.registered_pgm_timecodes={}
        self.cut_xml_paths=[];self.loaded_library_config={};self.loaded_library_store_config={};self.loaded_library_segments=[]
        self.label_counter_suffix=False
        self._preview_cut=None;self._shot_starts=[];self._pgm_starts=[]
        self._camera_epoch=0;self._preview_epoch=0;self._last_frame_key=None
        self.camera_ids=CAMERAS
        super().__init__()
        from ui_theme import apply_theme
        apply_theme(QApplication.instance());self.setStyleSheet('')
        self.setWindowTitle('三木的PGM回套软件')
        self.confirmed.setToolTip('核对PGM与代理时码及实际画面同步后勾选。')
        self.results.itemChanged.connect(self.camera_edited)
        self.sources.horizontalHeaderItem(4).setToolTip('原片对应可稍后补充。')
        from library_ui import LibraryPanel
        self.library_panel=LibraryPanel(self.capture_library_project,self.load_library_session,self,auto_restore=not prompt_project)
        self.library_panel.prepare_session_callback=self.prepare_library_session
        from library_thumbnails import SourceFrameLoader, LatestTaskLoader
        self.scene_frame_loader=SourceFrameLoader(self,decoder=decode_camera_preview)
        self.camera_task_loader=LatestTaskLoader(self)
        self.preview_prepare_loader=LatestTaskLoader(self)
        self.review_task_loader=LatestTaskLoader(self)
        self.library_panel.session_saved.connect(self.library_session_saved)
        self.library_panel.catalogue_refreshed.connect(self.library_catalogue_refreshed)
        self.library_panel.busy_changed.connect(self.library_busy_changed)
        self.library_panel.save_project_requested.connect(lambda:self.guarded(self.save_named_project))
        if hasattr(self.library_panel,'material_session_changed'):
            self.library_panel.material_session_changed.connect(self.library_materials_changed)
        self.library_panel.reference_changed.connect(lambda value:self.guarded(lambda:self.reference_action('set_reference',value)))
        self.library_panel.clip_selection_changed.connect(lambda value:self.guarded(lambda:self.reference_action('select_clip',value)))
        self.library_panel.reference_export_requested.connect(lambda value:self.guarded(lambda:self.reference_action('export_reference',value)))
        self.library_panel.baseline_map_requested.connect(lambda value:self.guarded(
            lambda:self.reference_action(value.get('operation','prepare_baseline_map'),value)))
        self.library_panel.manual_timeline_requested.connect(lambda value:self.guarded(
            lambda:self.reference_action(value.get('operation','prepare_manual_timeline'),value)))
        # One ordered workflow owns both preparation and the script browser.
        # Technical page indices remain stable for saved settings/internal APIs.
        layout=self.centralWidget().layout()
        position=layout.indexOf(self.tabs);layout.removeWidget(self.tabs)
        self.library_panel.attach_technical_widget(self.tabs)
        self.library_panel.workflow_stage_requested.connect(lambda stage:self.guarded(lambda:self.workflow_stage_requested(stage)))
        self.library_panel.working_session_selected.connect(lambda payload:self.guarded(lambda:self.working_recording_changed(payload)))
        self.library_panel.preparation_issue_requested.connect(lambda value:self.guarded(lambda:self.locate_preparation_issue(value)))
        layout.insertWidget(position,self.library_panel)
        self.library_panel.set_workflow_stage('materials')
        self.save_as_button=QPushButton('项目另存为…')
        self.save_as_button.clicked.connect(lambda:self.guarded(lambda:self.save_named_project(choose_path=True)))
        layout.itemAt(1).layout().insertWidget(2,self.save_as_button)
        self.project_buttons.append(self.save_as_button)
        if prompt_project:
            self.project_buttons[0].setText('打开 / 新建项目')
            QTimer.singleShot(0,self.open_project)

    def select_loaded_recording(self):
        """Keep every scene selector on the editor's accepted recording."""
        panel=self.library_panel
        identity=self.library_context.get('session_id')
        return panel.select_working_session(identity,notify=False)

    def working_recording_changed(self,payload):
        """Header navigation refreshes the visible technical page atomically."""
        panel=self.library_panel;stage=panel.workflow_stage
        if stage not in ('camera','sound'):return
        if (self.library_context.get('session_id')==payload['id'] and
                not self.library_materials_stale):return
        target=payload
        page=self.tabs.currentWidget()
        # Until preparation and any save prompt succeed, the displayed data
        # still belongs to the old scene. Failure/cancellation leaves it intact.
        self.select_loaded_recording()
        if panel.is_busy() or panel.external_busy or any(w and w.isRunning() for w in (self.worker,self.sound_worker,self.finder)):
            self.status.setText('当前任务尚未结束，请完成后再切换场次。')
            return False
        def opened():
            self.workflow_stage_requested(stage)
            if stage=='camera' and page is not None:
                index=self.tabs.indexOf(page)
                if index>=0 and self.tabs.isTabVisible(index):self.tabs.setCurrentIndex(index)
        return self.load_library_session(target,after_load=opened)

    def workflow_stage_requested(self,stage):
        panel=self.library_panel
        if stage=='match':
            session=panel.selected_session()
            if not session:raise AnalysisError('请先选择拍摄场次。')
            if self.library_context.get('session_id')!=session['id'] or self.library_materials_stale:
                return self.load_library_session(session,after_load=lambda:self.workflow_stage_requested(stage))
            panel.set_workflow_stage('script')
            return self.sync_library_recording(match_only=True)
        if stage in ('camera','sound','sync'):
            session=panel.selected_session()
            if not session:raise AnalysisError('请先选择拍摄场次并导入素材。')
            if self.library_context.get('session_id')!=session['id'] or self.library_materials_stale:
                return self.load_library_session(session,after_load=lambda:self.workflow_stage_requested(stage))
            target='sound' if stage in ('sound','sync') else 'camera'
            panel.set_workflow_stage(target)
            for index in range(self.tabs.count()):self.tabs.setTabVisible(index,(index==1) if target=='sound' else (index!=1))
            self.tabs.setCurrentIndex(1 if target=='sound' else 0)
            if stage=='sync':self.sync_library_recording()

    def sync_library_recording(self,match_only=False):
        """Save current settings, then run the sole shared sound-sync operation."""
        panel=self.library_panel;session=panel.selected_session()
        if not session:raise AnalysisError('请先选择拍摄场次。')
        save=self.recording_save_operation()
        if not match_only:self.sound_data_tabs.setCurrentWidget(self.sound_results_page)
        expected=self.library_context['production_id'];session_id=session['id']
        def operation(progress,cancelled):
            client,production=panel.ensure_workflow_context(progress,cancelled)
            if production!=expected:raise AnalysisError('当前项目已改变，请重新载入场次。')
            if cancelled():raise AnalysisError('已取消。')
            saved=save(client)
            return client,production,saved
        def success(value):
            client,production,saved=value
            self.saved_recording(saved)
            from project_ui import _after_idle
            if match_only:
                action=lambda:panel._workflow().recognize_and_match(panel,session_id=session_id,
                    require_synced=True,require_prepared=True)
            else:action=lambda:panel._workflow().sync_recording(panel,session_id=session_id)
            _after_idle(panel,action)
        panel._run('保存本场同步设置…',operation,success)

    def library_busy_changed(self,busy):
        if not busy:self._project_save_in_progress=False
        for button in self.project_buttons:button.setEnabled(not busy)
        self.save_as_button.setEnabled(not busy)
        for index in range(self.tabs.count()):
            if self.tabs.widget(index) is not self.library_panel:self.tabs.setTabEnabled(index,not busy)

    def closeEvent(self,event):
        if not self.library_panel.is_busy():self._project_save_in_progress=False
        if self._close_prompt_active or self._recording_prompt_active or self._project_save_in_progress:
            # Native close events can arrive again while a modal prompt or an
            # asynchronous save is open. Never cancel that save or nest prompts.
            event.ignore();return
        if self.library_panel.is_busy():
            self.library_panel.cancel_task();self.status.setText('正在结束素材库任务，请完成后再关闭。');event.ignore();return
        active=[w for w in (self.worker,self.sound_worker,self.finder) if w and w.isRunning()]
        if active:
            for worker in active:worker.requestInterruption()
            self.status.setText('正在结束当前任务，请完成后再关闭。');event.ignore();return
        if self.prompt_project and self.project_identity and self.project_dirty():
            dialog=QMessageBox(QMessageBox.Icon.Question,'保存项目',
                '关闭前是否保存项目“'+self.project_identity['name']+'”？',parent=self)
            dialog.setStandardButtons(QMessageBox.StandardButton.Save|QMessageBox.StandardButton.Discard|QMessageBox.StandardButton.Cancel)
            dialog.button(QMessageBox.StandardButton.Save).setText('保存后关闭')
            dialog.button(QMessageBox.StandardButton.Discard).setText('不保存')
            dialog.button(QMessageBox.StandardButton.Cancel).setText('取消')
            dialog.setDefaultButton(QMessageBox.StandardButton.Save)
            self._close_prompt_active=True
            try:choice=dialog.exec()
            finally:self._close_prompt_active=False
            if choice not in (QMessageBox.StandardButton.Save,QMessageBox.StandardButton.Discard):
                event.ignore();return
            if choice==QMessageBox.StandardButton.Save:
                event.ignore();self.guarded(lambda:self.save_named_project(close_after=True));return
        self.library_panel.close()
        self.scene_frame_loader.close()
        self.camera_task_loader.close();self.review_task_loader.close();self.preview_prepare_loader.close()
        self.preview_timer.stop()
        super().closeEvent(event)

    def open_project(self):
        if not self.prompt_project:return super().open_project()
        from project_ui import start_project
        continuation=lambda:start_project(self.library_panel,self,on_open=self.project_opened)
        if self._recording_prompt_active or self._close_prompt_active or self._project_save_in_progress:return False
        if self.project_identity and self.project_dirty():
            dialog=QMessageBox(QMessageBox.Icon.Question,'保存项目',
                '打开其他项目前是否保存“'+self.project_identity['name']+'”？',parent=self)
            dialog.setStandardButtons(QMessageBox.StandardButton.Save|QMessageBox.StandardButton.Discard|QMessageBox.StandardButton.Cancel)
            dialog.setDefaultButton(QMessageBox.StandardButton.Save)
            self._recording_prompt_active=True
            try:choice=dialog.exec()
            finally:self._recording_prompt_active=False
            if choice==QMessageBox.StandardButton.Save:return self.save_named_project(after_save=continuation)
            if choice==QMessageBox.StandardButton.Discard:return continuation()
            return False
        return self.leave_recording(continuation)

    def recording_dirty(self):
        if not self.library_context.get('session_id') or not self.loaded_library_config:return False
        ignored={'library_context','library_media','library_notes','dialogue_markers'}
        def editable(config):
            return {key:value for key,value in config.items() if key not in ignored and not key.startswith('library_')}
        if editable(self.config())!=editable(self.loaded_library_config):return True
        if self.analysis_config is not None:
            return [asdict(segment) for segment in self.segments()]!=self.loaded_library_segments
        return False

    def project_checkpoint(self):
        """Track editor changes and durable project data, excluding preview state."""
        ignored={'library_context','library_media','library_notes','dialogue_markers'}
        config={key:value for key,value in self.config().items()
                if key not in ignored and not key.startswith('library_')}
        value=dict(production_id=(self.project_identity or {}).get('id'),
                   session_id=self.library_context.get('session_id',''),config=config,
                   segments=[asdict(segment) for segment in self.segments()] if self.analysis_config is not None else [])
        client=getattr(self.library_panel,'client',None)
        if getattr(client,'is_local_project',False) is True:
            value['catalogue_revision']=client.revision
        # Match the JSON file's representation (e.g. tuples become lists).
        return json.loads(json.dumps(value,ensure_ascii=False))

    def project_dirty(self):
        if not self.project_path or self._saved_project_checkpoint is None:return True
        try:return self.project_checkpoint()!=self._saved_project_checkpoint
        except (ValueError,TypeError):return True

    def leave_recording(self,continuation):
        """A scene/project switch must not silently discard unsaved OCR/calibration."""
        if self._recording_prompt_active or self._close_prompt_active or self._project_save_in_progress:return False
        if not self.recording_dirty():return continuation()
        panel=self.library_panel;identity=self.library_context.get('session_id')
        dialog=QMessageBox(QMessageBox.Icon.Question,'保存本场修改',
            '“'+self.library_context.get('session_name','当前场次')+'”有未保存的机位、同步或声音修改。',parent=self)
        buttons=QMessageBox.StandardButton.Discard|QMessageBox.StandardButton.Cancel
        if not self.library_materials_stale:buttons|=QMessageBox.StandardButton.Save
        dialog.setStandardButtons(buttons)
        if dialog.button(QMessageBox.StandardButton.Save):dialog.button(QMessageBox.StandardButton.Save).setText('保存后继续')
        dialog.button(QMessageBox.StandardButton.Discard).setText('放弃本场修改')
        dialog.button(QMessageBox.StandardButton.Cancel).setText('取消切换')
        self._recording_prompt_active=True
        try:choice=dialog.exec()
        finally:self._recording_prompt_active=False
        if choice==QMessageBox.StandardButton.Discard:return continuation()
        # Keep the current editor's scene selected until its save has succeeded.
        self.select_loaded_recording()
        if choice==QMessageBox.StandardButton.Save:
            return self.save_recording_results(after_save=continuation)
        return False

    def project_opened(self,production):
        state=production.get('project_state')
        self.project_identity={key:copy.deepcopy(value) for key,value in production.items() if key!='project_state'};self._closing_after_save=False
        self._saved_project_checkpoint=None
        # An old technical workspace cannot accidentally save into another project.
        if self.library_context.get('production_id')!=production['id']:
            self.load_library_draft(dict(show_id='day01_通条',label_match_mode='prefix',library_context={},library_media=[],sources=[],sound_sources=[]))
            self.loaded_library_config={};self.loaded_library_store_config={};self.loaded_library_segments=[]
        from project_state import read_project_state
        state=state or read_project_state(production['id'])
        self.project_path=state.get('project_path','')
        session=next((row for row in self.library_panel._sessions if row['id']==state.get('session_id')),None)
        def opened_clean():
            if self.project_identity and self.project_identity['id']==production['id']:
                if self.project_path and state.get('version')==2:self._saved_project_checkpoint=self.project_checkpoint()
                self.library_panel.set_workflow_stage('materials')
        if session:self.guarded(lambda:self.load_library_session(session,after_load=opened_clean))
        else:opened_clean()
        self.library_panel.set_workflow_stage('materials')
        self.status.setText('项目：'+production['name'])

    def save_named_project(self,close_after=False,choose_path=False,after_save=None):
        if self._project_save_in_progress:return
        panel=self.library_panel;production=copy.deepcopy(self.project_identity)
        if not production:raise AnalysisError('请先命名或打开项目。')
        destination=self.project_path
        if choose_path or not destination:
            safe_name=''.join('_' if char in '/\\\0:' else char for char in production['name']).strip() or '项目'
            destination,_=QFileDialog.getSaveFileName(self,'保存项目到…',
                destination or str(Path.home()/'Desktop'/(safe_name+'.pgmproj')),'PGM 项目 (*.pgmproj)')
            if not destination:return
            if not destination.lower().endswith('.pgmproj'):destination+='.pgmproj'
        save=None;snapshot=None
        if self.library_context.get('production_id')==production['id']:
            if self.library_materials_stale:raise AnalysisError('素材已追加，请重新载入本场次后保存项目。')
            save=self.recording_save_operation()
            snapshot=self.capture_library_project()
        checkpoint=self.project_checkpoint()
        def operation(progress,cancelled):
            client,identity=panel.ensure_workflow_context(progress,cancelled)
            if identity!=production['id']:raise AnalysisError('当前项目已改变，请重新选择要保存的项目。')
            if cancelled():raise AnalysisError('已取消保存。')
            session=save(client) if save else None
            saved=copy.deepcopy(snapshot)
            if session:
                saved=dict(config=copy.deepcopy(session['config']),segments=copy.deepcopy(session.get('segments',[])))
                saved['config']['library_context']=dict(production_id=identity,session_id=session['id'],
                    session_name=session['name'],revision=session['revision'])
            from project_state import write_project_state
            catalogue=client.export_project(identity) if getattr(client,'is_local_project',False) is True else None
            path=write_project_state(production,session_id=session['id'] if session else '',snapshot=saved,
                                     save_path=destination,catalogue=catalogue)
            if catalogue is not None:client.last_saved_revision=catalogue['revision']
            from library_workflow import BatchWorkflow
            return client,identity,session,BatchWorkflow(client,identity).read_all(),path
        def success(value):
            client,identity,session,data,path=value
            panel.apply_workflow_context(client,identity,production['name']);panel._set_data(data)
            if session:self.saved_recording(session)
            self.project_path=str(path)
            self._saved_project_checkpoint=checkpoint
            if getattr(client,'is_local_project',False) is True:
                self._saved_project_checkpoint['catalogue_revision']=client.last_saved_revision
            self.status.setText('项目已保存：'+str(path))
            if close_after:
                if self.project_dirty():
                    self.status.setText('项目已保存：'+str(path)+'；保存期间有新修改，请再次保存后关闭。')
                else:
                    self._closing_after_save=True
                    from project_ui import _after_idle
                    _after_idle(panel,self.close)
            elif after_save:
                from project_ui import _after_idle
                _after_idle(panel,after_save)
        self._project_save_in_progress=True
        try:panel._run('保存项目…',operation,success)
        except Exception:
            self._project_save_in_progress=False
            raise

    def capture_library_project(self):
        self.require_current_library_materials()
        c=self.config()
        return dict(config=c,segments=[asdict(s) for s in self.segments()] if self.analysis_config is not None else [])

    def recording_save_operation(self):
        """Capture Qt state now; merge only changed settings against shared CAS."""
        self.require_current_library_materials()
        from library_bridge import translate_config
        captured=self.config()
        current=translate_config(captured,reverse=True)
        baseline=translate_config(copy.deepcopy(self.loaded_library_config),reverse=True)
        store_baseline=translate_config(copy.deepcopy(getattr(self,'loaded_library_store_config',baseline)),reverse=True)
        context=dict(self.library_context)
        if not context.get('session_id'):raise AnalysisError('请先载入一个已登记的拍摄场次。')
        if current.get('show_id')!=baseline.get('show_id'):
            raise AnalysisError('拍摄场次名称由统一素材入口确定，不能在机位校准时更换场次。')
        ignored={'library_context','library_media','library_notes','dialogue_markers'}
        changes={k:v for k,v in current.items() if k not in ignored and not k.startswith('library_') and v!=baseline.get(k)}
        if current.get('sound_unmatched_policy')!=store_baseline.get('sound_unmatched_policy'):
            changes['sound_unmatched_policy']=current['sound_unmatched_policy']
        segments=[asdict(s) for s in self.segments()] if self.analysis_config is not None else None
        old_segments=copy.deepcopy(self.loaded_library_segments)
        if segments is not None:
            defaults={'cut_xml_paths':[self.analysis_config.get('cut_xml')], 'pgm_overrides':{},
                      'label_rois':{}, 'labels':{camera:camera for camera in self.camera_ids}, 'ocr_confidence':.65,
                      'cut_xml_digest':baseline.get('cut_xml_digest'),'label_match_mode':'legacy',
                      'infer_a_when_blank':False,'counter_suffix':False,
                      'camera_ids':list(cameras_for(self.analysis_config)), 'camera_count':len(cameras_for(self.analysis_config))}
            for key in ('cut_xml','cut_xml_paths','cut_xml_digest','pgm','pgm_overrides','fps','label_rois','labels','ocr_confidence','label_match_mode','infer_a_when_blank','counter_suffix','camera_ids','camera_count'):
                if captured.get(key)!=self.analysis_config.get(key,defaults.get(key)):
                    # Older saved projects did not store the one-element XML list.
                    if key=='cut_xml_paths' and self.cut_xml_paths==[self.analysis_config.get('cut_xml')]:continue
                    raise AnalysisError('修改了'+key+'，请重新识别后保存机位结果。')
            if any(row['reason']=='identity_unconfirmed' for row in segments):
                raise AnalysisError('修改机位后请先点击“确认机位”。')
        def save(client):
            if segments is not None:
                from cut_xml import timeline_info
                cut=timeline_info(captured,False)
                if current.get('cut_xml_digest') and cut['xml_digest']!=current['cut_xml_digest']:
                    raise AnalysisError('切点XML已改变，请重新导入并识别后保存。')
                validate_segments([Segment(**row) for row in segments],cut['duration'])
                if [(row['start'],row['end']) for row in segments]!=[(row['start'],row['end']) for row in cut['shots']]:
                    raise AnalysisError('机位结果与当前切点XML不一致，请重新识别。')
            latest=client.call('get_session',session_id=context['session_id'])
            if latest['production_id']!=context['production_id']:raise AnalysisError('拍摄场次所属剧目已变化。')
            from sound_fallback import retire_sound_references
            config=retire_sound_references(latest['config'])
            for key,value in changes.items():
                if config.get(key)!=store_baseline.get(key) and config.get(key)!=value:
                    raise AnalysisError('其他人已修改本场设置“'+key+'”，请重新载入场次后核对。')
                if key=='sources':
                    prior={row.get('proxy_path') or row.get('path'):row for row in config.get('sources',[])}
                    config[key]=[dict(copy.deepcopy(prior.get(row.get('proxy_path') or row.get('path'),{})),**row)
                                 for row in value]
                else:config[key]=value
            values=latest.get('segments',[])
            if segments is not None:
                if values!=old_segments and values!=segments:
                    raise AnalysisError('其他人已修改本场机位结果，请重新载入场次后核对。')
                values=segments
            elif any(key in changes for key in ('cut_xml','cut_xml_paths','cut_xml_digest','labels','label_rois','sources','pgm_overrides','label_match_mode','infer_a_when_blank','counter_suffix','ocr_confidence','camera_ids','camera_count')):
                values=[]
            config['library_sound_cache_only']=True
            return client.call('save_session',production_id=latest['production_id'],name=latest['name'],
                config=config,segments=values,session_id=latest['id'],expected_revision=latest['revision'],
                export_dir=latest.get('export_dir',''))
        return save

    def saved_recording(self,session):
        self.library_session_saved(session)
        self.loaded_library_config=copy.deepcopy(self.config())
        from library_bridge import translate_config
        self.loaded_library_store_config=translate_config(copy.deepcopy(session.get('config',{})),reverse=True)
        self.loaded_library_segments=copy.deepcopy(session.get('segments',[]))
        if self.library_panel.selected_session() and self.library_panel.selected_session()['id']==session['id']:
            self.library_panel._session=session

    def locate_preparation_issue(self,payload):
        """Navigate to one saved problem without rerunning completed work."""
        panel=self.library_panel
        session=next((row for row in panel._sessions if row['id']==payload.get('session_id')),None)
        if not session:raise AnalysisError('该场次已变化，请刷新后重新选择问题。')
        stage=payload.get('stage')
        if stage not in ('camera','sound'):raise AnalysisError('无法定位此准备问题。')
        if self.library_context.get('session_id')!=session['id'] or self.library_materials_stale:
            return self.load_library_session(session,after_load=lambda:self.locate_preparation_issue(payload))
        panel.set_workflow_stage(stage)
        for index in range(self.tabs.count()):self.tabs.setTabVisible(index,(index==1 if stage=='sound' else index!=1))
        issue=payload.get('issue') or {}
        if stage=='camera':
            self.tabs.setCurrentIndex(self.tabs.indexOf(self.results_page))
            first=issue.get('start')
            row=next((row for row in range(self.results.rowCount())
                      if self.results.item(row,0) and self.results.item(row,0).text()==str(first)),None)
            if row is None:raise AnalysisError('该镜头已变化，请刷新准备状态后重新定位。')
            self.results.selectRow(row);self.results.scrollToItem(self.results.item(row,0))
            self.review_frame.setValue(int(first));self.show_review()
        else:
            from library_bridge import canonical_path,resolve_local_path
            self.tabs.setCurrentIndex(1)
            self.sound_data_tabs.setCurrentWidget(self.sound_results_page)
            path=issue.get('pgm') or issue.get('path')
            identity=canonical_path(path) if path else ''
            for row in range(self.sound_matches.rowCount()):
                item=self.sound_matches.item(row,0)
                if item and canonical_path(item.data(Qt.ItemDataRole.UserRole) or '')==identity:
                    self.sound_matches.selectRow(row);self.sound_matches.scrollToItem(item);break
            self.sound_status.setText(Path(resolve_local_path(path)).name+'：'+issue.get('message','未完成声音合板') if path else issue.get('message','未完成声音合板'))

    def save_recording_results(self,after_save=None):
        if not self.library_context.get('session_id'):
            return self.save_project()
        save=self.recording_save_operation();panel=self.library_panel
        expected_production=self.library_context.get('production_id')
        def operation(progress,cancelled):
            client,production=panel.ensure_workflow_context(progress,cancelled)
            if production!=expected_production:raise AnalysisError('当前剧目已改变，请重新载入要保存的拍摄场次。')
            if cancelled():raise AnalysisError('已取消。')
            session=save(client)
            from library_workflow import BatchWorkflow
            return client,production,session,BatchWorkflow(client,production).read_all()
        def success(result):
            client,production,session,data=result
            panel.apply_workflow_context(client,production);panel._set_data(data)
            self.saved_recording(session)
            state=data.get('session_readiness',{}).get(session['id'],{})
            summary='本场机位结果与设置已保存。'
            if state.get('ready'):summary+='机位、视频同步和声音均已准备完成。'
            elif state.get('message'):summary+=state['message']
            self.status.setText(summary)
            if after_save:
                from project_ui import _after_idle
                _after_idle(panel,after_save)
        panel._run('保存本场设置与机位结果…',operation,success)

    def reference_action(self,method,payload):
        panel=self.library_panel;request=copy.deepcopy(payload)
        if method not in ('set_reference','select_clip','export_reference','prepare_baseline_map','save_baseline_map',
                          'prepare_manual_timeline','save_manual_timeline','prepare_automatic_alignment',
                          'prepare_automatic_map','save_automatic_map'):
            raise AnalysisError('不支持的基准或备选操作。')
        save=None
        if method in ('export_reference','prepare_baseline_map','save_baseline_map','prepare_automatic_alignment',
                      'prepare_automatic_map','save_automatic_map') and self.library_context.get('production_id')==request['production_id']:
            relevant={request.get('reference_session_id') or getattr(panel,'reference_session_id',None)}
            if request.get('include_alternatives'):
                if request.get('manual'):
                    relevant.update(row.get('session_id') for row in
                        (panel.reference_state.get('manual_timeline',{}).get('items') or {}).values()
                        if row.get('start_seconds') is not None and not row.get('hidden'))
                else:
                    pairs=request.get('exported_take_pairs')
                    if pairs is None:
                        pairs=panel._take_index().export_pairs(getattr(panel,'reference_session_id',None))
                    relevant.update(row.get('session_id') for row in getattr(panel,'_candidates',[])
                                    if row.get('id') in pairs)
            if self.library_context.get('session_id') in relevant:
                save=self.recording_save_operation()
        def operation(progress,cancelled):
            client,production=panel.ensure_workflow_context(progress,cancelled)
            if production!=request['production_id']:raise AnalysisError('当前剧目已改变，请重新选择。')
            from reference_workflow import ReferenceWorkflow
            workflow=ReferenceWorkflow(client,production)
            if cancelled():raise AnalysisError('已取消。')
            if method in ('select_clip','export_reference','prepare_baseline_map','save_baseline_map',
                          'prepare_manual_timeline','save_manual_timeline','prepare_automatic_alignment',
                          'prepare_automatic_map','save_automatic_map'):
                state=client.call('get_reference_settings',production_id=production)
                if 'reference_session_id' in request and request['reference_session_id']!=state.get('reference_session_id'):
                    raise AnalysisError('基准通条已被其他人更换，请刷新后重新选择。')
                if request.get('expected_revision', state['revision']) != state['revision']:
                    raise AnalysisError('基准备选设置已被其他人更新，请刷新后重新选择。')
            session=save(client) if save else None
            if method=='set_reference':
                result=workflow.set_reference(request['reference_session_id'],
                    legacy_continuous_confirmed=request.get('legacy_continuous_confirmed',False),manual=True)
            elif method=='select_clip':
                if request.get('confirm'):
                    result=workflow.confirm_and_select_clip(request['candidate_id'],request.get('baseline_candidate_id'),
                        expected_revision=state['revision'])
                else:
                    result=workflow.select_clip(request['candidate_id'],request['checked'],request.get('baseline_candidate_id'),
                        expected_revision=state['revision'])
            elif method=='prepare_baseline_map':
                result=workflow.prepare_baseline_map(rebuild=request.get('rebuild',False))
            elif method=='save_baseline_map':
                result=workflow.save_baseline_map(request['mapping'],expected_revision=state['revision'])
            elif method=='prepare_manual_timeline':
                result=workflow.prepare_manual_timeline()
            elif method=='save_manual_timeline':
                result=workflow.save_manual_timeline(request['layout'],expected_revision=state['revision'])
            elif method in ('prepare_automatic_alignment','prepare_automatic_map','save_automatic_map'):
                options=dict(layout=request['layout'],expected_revision=state['revision'],model=request.get('model','small'),
                    replace_automatic=request.get('replace_automatic',False),progress=progress,cancelled=cancelled)
                if method=='save_automatic_map':options['mapping']=request['mapping']
                elif request.get('rebuild'):options['rebuild']=True
                result=getattr(workflow,method)(**options)
            else:
                result=workflow.export_reference(request['output_dir'],
                    include_alternatives=request.get('include_alternatives',False),progress=progress,cancelled=cancelled,
                    expected_revision=state['revision'], include_baseline=request.get('include_baseline',True),
                    resolved_pairs=request.get('resolved_pairs'), export_snapshot=request.get('export_snapshot'),
                    manual=request.get('manual',False))
            if session is not None and method in ('prepare_automatic_alignment','prepare_automatic_map','save_automatic_map'):
                # Baseline-only transcription may have saved private caches
                # after the initial scene snapshot; retain its latest revision.
                session=client.call('get_session',session_id=session['id'])
            return client,production,session,result
        def success(value):
            client,production,session,result=value
            panel.apply_workflow_context(client,production)
            if (method in ('prepare_baseline_map','save_baseline_map','prepare_manual_timeline','save_manual_timeline','set_reference',
                          'prepare_automatic_alignment','prepare_automatic_map','save_automatic_map')
                    or method=='export_reference' and request.get('manual')) and result.get('state'):
                panel.apply_reference_state(result['state'], refresh_matches=method in ('prepare_baseline_map','save_baseline_map'))
            elif result.get('data'):panel._set_data(result['data'])
            elif result.get('state'):panel.apply_reference_state(result['state'])
            if session:self.saved_recording(session)
            if result.get('xml_path'):
                from resolve_import_helper import companion_path
                note='；请通过达芬奇“工作区 → 脚本 → 三木_导入多机位XML”导入，恢复时间线时码与音视频链接。' if companion_path(result['xml_path']).is_file() else ''
                panel.status.setText('多机位 XML 已导出：'+str(result['xml_path'])+note)
                message='XML：'+str(result['xml_path'])+'\n原片对应表：'+str(result.get('map_path',''))
                if note:message+='\n'+note.lstrip('；')
                QMessageBox.information(self,'时间线已导出',message)
            elif method=='prepare_baseline_map':
                from project_ui import _after_idle
                _after_idle(panel,lambda:panel.present_baseline_mapping(result))
            elif method=='save_baseline_map':
                panel.status.setText('基准台本映射已保存，绿色备选将按这份映射对齐。')
            elif method=='prepare_manual_timeline':
                from project_ui import _after_idle
                _after_idle(panel,lambda:panel.present_manual_timeline(result))
            elif method=='save_manual_timeline':
                panel.manual_timeline_saved(result,after_export=request.get('after_export'))
            elif method in ('prepare_automatic_alignment','prepare_automatic_map','save_automatic_map'):
                from project_ui import _after_idle
                callback=panel.present_automatic_map if method=='prepare_automatic_map' else panel.present_automatic_alignment
                _after_idle(panel,lambda:callback(result))
            else:
                panel.status.setText('基准与备选选择已保存。')
        title=('导出多机位时间线…' if method=='export_reference' else
               '读取已登记的PGM与编排…' if method=='prepare_manual_timeline' else
               '保存人工时间轴编排…' if method=='save_manual_timeline' else
               '基准台词映射与自动初排…' if method in ('prepare_automatic_alignment','prepare_automatic_map') else
               '保存基准台本映射…' if method=='save_automatic_map' else
               '建立基准台本映射（复用已有转录）…' if method=='prepare_baseline_map' else
               '保存基准台本映射…' if method=='save_baseline_map' else '保存基准备选设置…')
        panel._run(title,operation,success)

    def save_project(self):
        if self.prompt_project:
            return self.guarded(self.save_named_project)
        self.require_current_library_materials()
        if self.analysis_config is not None:
            from project_state import _without_credentials
            from sound_fallback import retire_sound_references
            self.analysis_config=_without_credentials(retire_sound_references(self.analysis_config))
        super().save_project()

    def library_session_saved(self,session):
        self.library_context=dict(production_id=session['production_id'],session_id=session['id'],
                                  revision=session.get('revision'),session_name=session['name'])
        self.library_media=copy.deepcopy(session.get('config',{}).get('library_media',self.library_media))
        self.library_notes=session.get('config',{}).get('library_notes',self.library_notes)
        self.library_extra=self.library_extras(session.get('config',{}))
        self.show_registered_sound(session.get('config',{}))

    def library_catalogue_refreshed(self,data):
        identity=self.library_context.get('session_id')
        row=next((row for row in data.get('sessions',[]) if row['id']==identity),None)
        if row:
            self.library_extra=self.library_extras(row.get('config',{}))
            self.show_registered_sound(row.get('config',{}))

    def show_registered_sound(self,config=None):
        if not hasattr(self,'sound_matches'):return
        self.sound_matches.setRowCount(0)
        config=config if config is not None else self.config()
        cache=config.get('library_sync') or {}
        for pgm,state in cache.items():
            for placement in state.get('placements',[]):
                row=self.sound_matches.rowCount();self.sound_matches.insertRow(row)
                offset=placement.get('offset_seconds');score=placement.get('score')
                values=(Path(pgm).name,Path(placement.get('wav_path','')).name,
                    f'{offset:.6f}' if isinstance(offset,(int,float)) else '—',
                    f'{score:.3f}' if isinstance(score,(int,float)) else ('时码' if placement.get('method')=='timecode' else '—'))
                for column,value in enumerate(values):self.sound_matches.setItem(row,column,QTableWidgetItem(value))
                self.sound_matches.item(row,0).setData(Qt.ItemDataRole.UserRole,pgm)
                self.sound_matches.item(row,0).setToolTip(pgm)
        from workflow_readiness import session_readiness
        state=session_readiness(dict(config=config,segments=[]))
        for pending in state.get('sound_pending_rows',[]):
            row=self.sound_matches.rowCount();self.sound_matches.insertRow(row)
            pgm=pending.get('pgm') or pending.get('path') or ''
            for column,value in enumerate((Path(pgm).name,'未匹配','—','—')):
                item=QTableWidgetItem(value);item.setToolTip(pending.get('message','未完成声音合板'))
                self.sound_matches.setItem(row,column,item)
            self.sound_matches.item(row,0).setData(Qt.ItemDataRole.UserRole,pgm)
        for fallback in state.get('sound_fallback_rows',[]):
            row=self.sound_matches.rowCount();self.sound_matches.insertRow(row)
            pgm=fallback.get('pgm') or fallback.get('pgm_path') or ''
            for column,value in enumerate((Path(pgm).name,'音频留空','—','未匹配原始 WAV')):
                item=QTableWidgetItem(value);item.setToolTip('未匹配原始 WAV 的区间不输出音频。')
                self.sound_matches.setItem(row,column,item)
            self.sound_matches.item(row,0).setData(Qt.ItemDataRole.UserRole,pgm)
        for gap in state.get('sound_gap_rows',[]):
            row=self.sound_matches.rowCount();self.sound_matches.insertRow(row)
            pgm=gap.get('pgm') or gap.get('pgm_path') or ''
            for column,value in enumerate((Path(pgm).name,'音频留空','—','原始 WAV 未覆盖')):
                item=QTableWidgetItem(value);item.setToolTip(gap.get('message','未覆盖原始 WAV 的区间不输出音频。'))
                self.sound_matches.setItem(row,column,item)
            self.sound_matches.item(row,0).setData(Qt.ItemDataRole.UserRole,pgm)
        total=state.get('sound_total',state.get('pgm_count',0));matched=state.get('sound_matched',0)
        fallback=state.get('sound_fallback',0);gaps=state.get('sound_gap',0);pending=state.get('sound_pending',total-matched-fallback-gaps)
        if total:
            self.sound_status.setText(f'原始 WAV 合板 {matched} 条'+(f' · 音频留空 {gaps+fallback} 条' if gaps+fallback else '')+f' · 共 {total} 条 PGM'+
                (f'；另有 {pending} 条待处理，可在“准备状态”定位。' if pending else '；导出仅使用已合板的原始 WAV。'))
        else:self.sound_status.setText('尚未登记 PGM。')

    @staticmethod
    def library_extras(config):
        from sound_fallback import retire_sound_references
        config=retire_sound_references(config)
        return {key:copy.deepcopy(value) for key,value in config.items() if (key.startswith('library_') or key.startswith('recording_'))
                and key not in ('library_context','library_media','library_notes')}

    def preserve_config_fields(self,config):
        """Keep unrepresented legacy fields local to this loaded scene."""
        from project_state import _without_credentials
        from sound_fallback import retire_sound_references
        config=retire_sound_references(config)
        represented={'mode','cut_xml','cut_xml_paths','cut_xml_digest','media_workflow','show_id','fps',
            'pgm','pgm_start_tc','pgm_overrides','pgm_timecodes','timeline_start_tc','camera_ids','camera_count',
            'camera_offsets','calibration_confirmed','label_rois','labels','label_match_mode','infer_a_when_blank',
            'counter_suffix','ocr_confidence','multicam','external_sound','sound_sources','sound_sync_mode',
            'sound_allow_gaps','sound_unmatched_policy','sound_reference_overrides','sources','include_pgm_reference',
            'timeline_width','timeline_height','output_dir','dialogue_markers'}
        self.config_extra=_without_credentials({key:value for key,value in config.items()
            if key not in represented and not key.startswith(('library_','recording_'))})

    def set_resolution(self,width,height):
        """Select a valid saved size even when it is absent from the shortcuts."""
        values=[]
        for value in (width,height):
            if isinstance(value,bool) or not isinstance(value,(int,str)):
                raise AnalysisError('时间线分辨率必须是正整数。')
            try:parsed=int(value)
            except ValueError as exc:raise AnalysisError('时间线分辨率必须是正整数。') from exc
            if parsed<=0:raise AnalysisError('时间线分辨率必须是正整数。')
            values.append(parsed)
        label=f'{values[0]}×{values[1]}'
        index=self.resolution.findText(label)
        if index<0:self.resolution.addItem(label);index=self.resolution.count()-1
        self.resolution.setCurrentIndex(index)

    def library_materials_changed(self,session):
        if session['id']==self.library_context.get('session_id'):
            self.library_materials_stale=True
            self.invalidate_analysis()
            self.status.setText('本拍摄场次已增补素材。请从“场次素材”重新载入该场次，关联覆盖所需PGM的切点XML；原有归类已保留。')

    def require_current_library_materials(self):
        if self.library_materials_stale:
            raise AnalysisError('本拍摄场次已增补素材，请从“场次素材”重新载入该场次后继续。')

    def request_unified_import(self):
        self.library_panel.set_workflow_stage('materials')
        identity=self.library_context.get('session_id')
        if identity:self.library_panel.open_material_import(identity)
        else:self.library_panel.open_material_import()

    @staticmethod
    def prepare_library_session(payload):
        """Pure media/XML validation; called by the catalogue load worker."""
        from library_bridge import translate_config
        c=translate_config(payload['config'])
        c['library_context']=dict(production_id=payload['production_id'],session_id=payload['id'],
                                  revision=payload['revision'],session_name=payload['name'])
        sounds={}
        c=Window.prepare_library_media(c,sound_infos=sounds)
        cut=None
        if c.get('cut_xml'):
            from cut_xml import timeline_info
            cut=timeline_info(c,False)
            if c.get('cut_xml_digest') and cut['xml_digest']!=c['cut_xml_digest']:
                raise AnalysisError('项目场次的切点XML已改变，请重新导入并识别。')
            if payload.get('segments'):
                values=[Segment(**s) for s in payload['segments']]
                if [(s.start,s.end) for s in values]!=[(s['start'],s['end']) for s in cut['shots']]:
                    raise AnalysisError('项目识别结果与切点XML不一致，请重新识别。')
                validate_segments(values,cut['duration'])
        focus=payload.get('library_focus')
        focus_frame=Window.library_focus_frame(focus,c,cut,payload.get('export_dir','')) if focus and cut else None
        return PreparedSession((payload['production_id'],payload['id'],payload['revision']),c,cut,sounds,focus_frame)

    def load_library_session(self,payload,_confirmed=False,after_load=None):
        if not _confirmed and self.recording_dirty():
            def proceed():
                fresh=next((row for row in self.library_panel._sessions if row['id']==payload['id']),payload)
                return self.load_library_session(fresh,_confirmed=True,after_load=after_load)
            return self.leave_recording(proceed)
        prepared=payload.get('_prepared_editor')
        if self.prompt_project and self.library_panel.client and not isinstance(prepared,PreparedSession):
            # Real app navigation, including startup and workflow buttons,
            # shares the same background prepare path as "载入剪辑场次".
            from project_ui import _after_idle
            _after_idle(self.library_panel,lambda:self.library_panel._load(
                payload['id'],payload.get('library_focus'),after_load=after_load))
            return True
        from library_bridge import translate_config
        async_preview=isinstance(prepared,PreparedSession) and prepared.identity==(payload['production_id'],payload['id'],payload['revision'])
        if not async_preview:prepared=self.prepare_library_session(payload)
        c,cut=prepared.config,prepared.cut
        self.cancel_camera_previews()
        if cut is not None:
            self.apply_config(c,prepared_cut=cut,defer_preview=async_preview,sound_infos=prepared.sound_infos)
            if payload.get('segments'):
                values=[Segment(**s) for s in payload['segments']]
                self.analysis_config=copy.deepcopy(self.config());self.populate_results(values);self.export_button.setEnabled(True)
        else:
            self.load_library_draft(c,media_prepared=True,sound_infos=prepared.sound_infos)
        self.library_session_saved(payload)
        self.loaded_library_config=copy.deepcopy(self.config())
        self.loaded_library_store_config=translate_config(copy.deepcopy(payload['config']),reverse=True)
        self.loaded_library_segments=copy.deepcopy(payload.get('segments',[]))
        self.library_materials_stale=False
        panel=self.library_panel
        if any(row['id']==payload['id'] for row in panel._sessions):
            for index,row in enumerate(panel._sessions):
                if row['id']==payload['id'] and row.get('revision')!=payload.get('revision'):
                    # A worker may have read a newer saved revision than the
                    # catalogue header snapshot. Keep its accepted selection
                    # on that revision without retaining transient media data.
                    panel._sessions[index]={key:value for key,value in payload.items()
                        if key not in ('_prepared_editor','library_focus')}
            panel.working_session.blockSignals(True)
            panel.working_session.setCurrentIndex(panel.working_session.findData(payload['id']))
            panel.working_session.blockSignals(False);panel._session=payload
            self.select_loaded_recording()
        self.library_panel.set_workflow_stage('camera')
        for index in range(self.tabs.count()):self.tabs.setTabVisible(index,index!=1)
        focus=payload.get('library_focus')
        located=False
        if focus and c.get('cut_xml'):
            frame=prepared.focus_frame
            if frame is not None:
                self.preview_index.setValue(max(0,min(frame,cut['duration']-1)))
                if async_preview:self.request_scene_preview(cut,self.preview_index.value())
                else:self.refresh_pgm()
                self.tabs.setCurrentIndex(2);located=True
        else:self.tabs.setCurrentIndex(0)
        self.status.setText('已载入项目场次：'+payload['name'])
        if focus and not located:
            self.status.setText('场次已载入；此媒体尚未定位到PGM。可预览原文件，完成切点及声音合板后再定位。')
        if after_load:return after_load()
        return True

    @staticmethod
    def library_focus_frame(focus,config,cut,export_dir=''):
        from library_bridge import resolve_local_path
        from library_export import dialogue_ranges
        marker=dict(focus,session_id=config['library_context']['session_id'],status='confirmed')
        marker['end_seconds']=max(float(marker.get('end_seconds',0)),float(marker['start_seconds'])+.04)
        ranges=dialogue_ranges(dict(config,dialogue_markers=[marker]),cut)
        if ranges:return ranges[0]['timeline_start_frame']
        # A WAV location requires the verified sound map from a completed export.
        # Reading that saved map avoids a long waveform match on the GUI thread.
        if export_dir and focus.get('candidate_id'):
            path=Path(resolve_local_path(export_dir))/'library_manifest.json'
            if path.is_file():
                try:
                    saved=json.loads(path.read_text(encoding='utf-8'))
                    from library_export import sync_signature
                    if (saved.get('context',{}).get('session_id')==config['library_context']['session_id']
                        and saved.get('sync_signature')==sync_signature(config,cut)):
                        rows=[r for r in saved.get('dialogue',[]) if r.get('id')==focus['candidate_id']]
                        if rows:return min(r['timeline_start_frame'] for r in rows)
                except (OSError,ValueError,KeyError):pass
        return None

    def load_library_draft(self,c,media_prepared=False,sound_infos=None):
        if any(w and w.isRunning() for w in (self.worker,self.sound_worker,self.finder)):
            raise AnalysisError('请先结束当前任务。')
        from sound_fallback import retire_sound_references
        c=retire_sound_references(c)
        if not media_prepared:c=self.prepare_library_media(c)
        self.set_resolution(c.get('timeline_width',3840),c.get('timeline_height',2160))
        self.preserve_config_fields(c)
        self.cancel_camera_previews();self._preview_cut=None;self._shot_starts=[];self._pgm_starts=[]
        self.library_materials_stale=False
        self.cut_xml.clear();self.cut_xml_paths=[];self.cut_digest='';self.pgm_overrides={};self.pgm_table.setRowCount(0);self.pgm.setText(c.get('pgm',''))
        self.sources.setRowCount(0)
        self.library_context=copy.deepcopy(c.get('library_context',{}));self.library_media=copy.deepcopy(c.get('library_media',[]))
        self.library_extra=self.library_extras(c)
        self.registered_pgm_timecodes=copy.deepcopy(c.get('pgm_timecodes',{}))
        self.library_notes=c.get('library_notes','');self.dialogue_markers=[]
        self.source_id_map={row.get('proxy_path') or row.get('path'):row.get('source_id') or str(uuid.uuid4()) for row in c.get('sources',[])}
        self.show_id.setCurrentText(c.get('show_id','day01_通条'));self.fps.setCurrentText(str(c.get('fps',25)))
        self.timeline_tc.setText(c.get('timeline_start_tc','01:00:00:00'))
        for camera,field in self.camera_offsets.items():field.setValue(c.get('camera_offsets',{}).get(camera,0))
        self.restore_label_settings(c)
        self.output.setText(c.get('output_dir',str(Path.home()/'Desktop')))
        self.populate_sources(c.get('sources',[]))
        self.sound_sync_mode.setCurrentIndex(max(0,self.sound_sync_mode.findData(c.get('sound_sync_mode','waveform'))))
        self.sound_allow_gaps.setChecked(True)
        self.populate_sound(c.get('sound_sources',[]),prepared_infos=sound_infos);self.invalidate_analysis();self.loaded_pgm=''
        self.confirmed.setChecked(False);self.canvas.rois=copy.deepcopy(c.get('label_rois',{}));self.canvas.update()
        # Registry is the source of the media list before an edit XML exists.
        from library_workflow import _pgms
        metadata={row['path']:row.get('probe') or row.get('metadata',{}) for row in c.get('library_media',[]) if row.get('kind')=='pgm'}
        for path in _pgms(c):
            row=self.pgm_table.rowCount();self.pgm_table.insertRow(row);info=metadata.get(path,{})
            values=(path,c.get('pgm_timecodes',{}).get(path) or info.get('start_tc') or '',info.get('frames','—'),path)
            for col,value in enumerate(values):
                item=QTableWidgetItem(str(value))
                if col==0:item.setToolTip(str(value))
                if col!=1:item.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsSelectable)
                self.pgm_table.setItem(row,col,item)
        self.relocate_button.setEnabled(False)
        self.update_registry_controls()
        self.refresh_media_summary()

    @staticmethod
    def prepare_library_media(config,sound_infos=None):
        """Use explicitly classified proxies/WAVs; original pairs require review."""
        c=copy.deepcopy(config)
        video_infos={}
        def video_info(path):
            if path not in video_infos:
                info=probe(path);video_infos[path]=info;video_infos[info['path']]=info
            return video_infos[path]
        if sound_infos is None:sound_infos={}
        from sound_sync import probe_wav
        def sound_info(path):
            if path not in sound_infos:
                info=probe_wav(path,fps=int(c.get('fps',25)))
                sound_infos[path]=info;sound_infos[info['path']]=info
            return sound_infos[path]
        if not c.get('sources'):
            sources=[];seen={}
            for row in c.get('library_media',[]):
                if row.get('kind')!='proxy':continue
                camera=row.get('camera','')
                if camera not in cameras_for(c):raise AnalysisError('登记的代理机位不在本场机位设置中。')
                info=video_info(row['path'])
                if info['path'] in seen:
                    if seen[info['path']]!=camera:raise AnalysisError('同一代理被登记到多个不同机位，请先核对归类。')
                    continue
                seen[info['path']]=camera
                sources.append(dict(camera=camera,path=info['path'],proxy_path=info['path'],
                    start_tc=info.get('start_tc') or '',frames=info['frames'],original_path='',original_start_tc='',
                    source_id=row.get('id') or str(uuid.uuid4())))
            if sources:c['sources']=sources
        if not c.get('sound_sources'):
            from sound_sync import probe_wav
            sounds=[];seen=set()
            for row in c.get('library_media',[]):
                if row.get('kind')!='main_wav':continue
                info=sound_info(row['path'])
                if info['path'] not in seen:sounds.append(dict(path=info['path'],channel='main'));seen.add(info['path'])
            c['sound_sources']=sounds
        # Resolve all explicitly configured inputs before mutating the live UI.
        for source in c.get('sources',[]):
            info=video_info(source.get('proxy_path') or source['path'])
            validate_rate(info,int(c.get('fps',25)))
        from sound_sync import probe_wav
        for sound in c.get('sound_sources',[]):
            sound_info(sound['path'])
        return c

    def populate_sources(self,rows):
        """Populate a large camera registry in one layout pass."""
        blocked=self.sources.blockSignals(True);sorting=self.sources.isSortingEnabled()
        self.sources.setUpdatesEnabled(False);self.sources.setSortingEnabled(False)
        try:
            self.sources.setRowCount(len(rows))
            for row,source in enumerate(rows):
                values=(source['camera'],source.get('proxy_path') or source['path'],source.get('start_tc',''),
                    source.get('frames',''),source.get('original_path',''),source.get('original_start_tc',''))
                for column,value in enumerate(values):
                    item=QTableWidgetItem(str(value))
                    if column==1:item.setData(Qt.ItemDataRole.UserRole,copy.deepcopy(source))
                    if column in (0,1,3,4):item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    if column in (1,4):item.setToolTip(str(value))
                    self.sources.setItem(row,column,item)
        finally:
            self.sources.setSortingEnabled(sorting);self.sources.blockSignals(blocked);self.sources.setUpdatesEnabled(True)

    def update_registry_controls(self):
        for row in range(self.sources.rowCount()):
            for column in (0,1,3,4):
                item=self.sources.item(row,column)
                if item:
                    flags=item.flags()
                    item.setFlags(flags & ~Qt.ItemFlag.ItemIsEditable)
                    if column in (1,4):item.setToolTip(item.text())

    def remove_sources(self):
        if self.library_context.get('session_id'):
            self.status.setText('已登记的代理由统一素材入口管理。')
            return
        super().remove_sources()
        self.refresh_media_summary()

    def build_files(self):
        page=QWidget();layout=QVBoxLayout(page)
        self.media_summary=QLabel('请先在“场次素材”导入素材并载入拍摄场次。')
        self.media_summary.setWordWrap(True);layout.addWidget(self.media_summary)
        form=QFormLayout();form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.pgm=QLineEdit()
        self.output=QLineEdit(str(Path.home()/'Desktop'))
        # XML/PGM are registered once at stage 1. Hidden fields retain legacy config compatibility.
        self.cut_xml=QLineEdit(self);self.cut_xml.setReadOnly(True);self.cut_xml.hide()
        self.choose_cut_button=QPushButton('管理本场素材…',self);self.choose_cut_button.hide()
        self.choose_cut_button.clicked.connect(lambda:self.guarded(self.request_unified_import))
        from recording_schema import recording_options
        self.show_id=QComboBox();self.show_id.setEditable(True);self.show_id.addItems([row['name'] for row in recording_options()])
        self.show_id.lineEdit().setReadOnly(True)
        self.show_id.setEnabled(False)
        form.addRow('拍摄场次',self.show_id)
        self.timeline_tc=QLineEdit('01:00:00:00');form.addRow('时间线起始时码',self.timeline_tc)
        self.resolution=QComboBox();self.resolution.addItems(['3840×2160','4096×2160','1920×1080'])
        form.addRow('时间线分辨率',self.resolution)
        self.fps=QComboBox();self.fps.addItems(['25','24','30']);self.fps.setEnabled(False)
        form.addRow('帧率',self.fps)
        self.file_row(form,'输出目录',self.output,directory=True);layout.addLayout(form)
        self.calibration_toggle=QToolButton();self.calibration_toggle.setText('时码核对与原片配对')
        self.calibration_toggle.setCheckable(True);self.calibration_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.calibration_toggle.setArrowType(Qt.ArrowType.RightArrow);layout.addWidget(self.calibration_toggle)
        self.calibration_panel=QWidget();calibration=QVBoxLayout(self.calibration_panel);calibration.setContentsMargins(0,0,0,0)
        # Legacy project values must round-trip without shifting existing cuts.
        # These fields are internal storage only; no compensation controls remain.
        self.camera_offsets={}
        for cam in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
            field=spin(-1000000,1000000);field.setParent(self);field.hide()
            self.camera_offsets[cam]=field
        self.confirmed=QCheckBox('视频同步已核对');calibration.addWidget(self.confirmed)
        self.pgm_tc=QLineEdit();self.pgm_tc.setPlaceholderText('自动读取或填写首帧时码')
        pgm_group=QGroupBox('PGM首帧时码核查');pgm_layout=QVBoxLayout(pgm_group)
        pgm_tools=QHBoxLayout();self.relocate_button=QPushButton('重新定位所选片段…');self.relocate_button.setEnabled(False)
        self.relocate_button.clicked.connect(lambda:self.guarded(self.relocate_pgm));pgm_tools.addWidget(self.relocate_button);pgm_tools.addStretch();pgm_layout.addLayout(pgm_tools)
        self.pgm_table=QTableWidget(0,4);self.pgm_table.setHorizontalHeaderLabels(['PGM文件','首帧时码','帧数','XML路径'])
        self.pgm_table.setItemDelegate(FilenameDelegate((0,),self.pgm_table))
        self.pgm_table.setColumnHidden(3,True);self.pgm_table.setMinimumHeight(100);self.pgm_table.setMaximumHeight(125)
        self.pgm_table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.pgm_table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch)
        pgm_layout.addWidget(self.pgm_table);calibration.addWidget(pgm_group)
        group=QGroupBox('代理时码及原片一对一配对');gl=QVBoxLayout(group);tools=QHBoxLayout()
        tools.addWidget(QLabel('选中一条代理，指定其完整原片。'))
        tools.addStretch();self.attach_original_button=QPushButton('指定原片…')
        self.attach_original_button.clicked.connect(lambda:self.guarded(self.attach_original));tools.addWidget(self.attach_original_button);gl.addLayout(tools)
        self.sources=QTableWidget(0,6);self.sources.setHorizontalHeaderLabels(['机位','代理文件','代理时码','帧数','原片文件','原片时码'])
        self.sources.setItemDelegate(FilenameDelegate((1,4),self.sources))
        self.sources.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.sources.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeMode.Stretch)
        self.sources.horizontalHeader().setSectionResizeMode(4,QHeaderView.ResizeMode.Stretch)
        self.sources.setMinimumHeight(130);gl.addWidget(self.sources);calibration.addWidget(group)
        layout.addWidget(self.calibration_panel,1)
        self.calibration_toggle.toggled.connect(self.calibration_panel.setVisible)
        self.calibration_toggle.toggled.connect(lambda checked:self.calibration_toggle.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow))
        self.calibration_toggle.setChecked(True)
        save_video=QPushButton('保存视频同步设置');save_video.clicked.connect(lambda:self.guarded(self.save_recording_results));layout.addWidget(save_video)
        layout.addStretch()
        # Kept only for loading/using old JSON projects through the internal API.
        self.add_cam=QComboBox(self);self.add_cam.addItems(list(CAMERAS));self.add_cam.hide()
        self.timeline_settings_scroll=QScrollArea();self.timeline_settings_scroll.setWidgetResizable(True)
        self.timeline_settings_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.timeline_settings_scroll.setWidget(page)
        self.tabs.addTab(self.timeline_settings_scroll,'视频同步');self.build_sound()

    def refresh_media_summary(self):
        if not hasattr(self,'media_summary'):return
        name=self.library_context.get('session_name')
        if not name and not self.pgm_table.rowCount() and not self.sources.rowCount():
            self.media_summary.setText('请先在“场次素材”导入素材并载入拍摄场次。')
            return
        title='拍摄场次：'+name if name else '已载入旧项目'
        self.media_summary.setText(f'{title}  ·  {len(self.camera_ids)} 个机位  ·  PGM {self.pgm_table.rowCount()} 条  ·  代理 {self.sources.rowCount()} 条  ·  主WAV {self.sound_table.rowCount()} 条')

    def build_sound(self):
        page=QWidget();self.sound_page=page;layout=QVBoxLayout(page)
        self.sound_split=QSplitter(Qt.Orientation.Horizontal)
        self.sound_split.setChildrenCollapsible(False);self.sound_split.setHandleWidth(7)
        layout.addWidget(self.sound_split)

        self.sound_settings_panel=QWidget();settings_layout=QVBoxLayout(self.sound_settings_panel)
        settings_layout.setContentsMargins(0,0,8,0);settings_layout.setSpacing(12)
        self.sound_settings_panel.setMinimumWidth(285)
        self.sound_settings_scroll=QScrollArea();self.sound_settings_scroll.setWidgetResizable(True)
        self.sound_settings_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.sound_settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        settings_host=QWidget();settings=QVBoxLayout(settings_host);settings.setContentsMargins(0,0,0,0);settings.setSpacing(12)
        sync_group=QGroupBox('合板设置');sync_layout=QVBoxLayout(sync_group)
        sync_layout.addWidget(QLabel('同步方式'))
        self.sound_sync_mode=QComboBox();self.sound_sync_mode.addItem('波形匹配（优先）','waveform');self.sound_sync_mode.addItem('仅时码','timecode')
        self.sound_sync_mode.currentIndexChanged.connect(self.sound_settings_changed);sync_layout.addWidget(self.sound_sync_mode)
        self.sound_allow_gaps=QCheckBox('未匹配原始 WAV 的区间留空');self.sound_allow_gaps.setChecked(True)
        self.sound_allow_gaps.setEnabled(False)
        self.sound_allow_gaps.setToolTip('导出仅引用已合板的原始 WAV；PGM 参考声音仍可用于分析。')
        self.sound_allow_gaps.toggled.connect(self.sound_settings_changed);sync_layout.addWidget(self.sound_allow_gaps)
        settings.addWidget(sync_group)

        settings.addStretch(1)
        self.sound_settings_scroll.setWidget(settings_host);settings_layout.addWidget(self.sound_settings_scroll,1)
        self.save_sound_button=QPushButton('保存本场声音设置')
        self.save_sound_button.clicked.connect(lambda:self.guarded(self.save_recording_results));settings_layout.addWidget(self.save_sound_button)
        self.sound_split.addWidget(self.sound_settings_panel)

        self.sound_data_panel=QWidget();data_layout=QVBoxLayout(self.sound_data_panel);data_layout.setContentsMargins(4,0,0,0)
        self.sound_data_panel.setMinimumWidth(445)
        self.sound_data_tabs=QTabWidget();data_layout.addWidget(self.sound_data_tabs,1)
        self.sound_results_page=QWidget();results_layout=QVBoxLayout(self.sound_results_page)
        self.sound_matches=QTableWidget(0,4);self.sound_matches.setHorizontalHeaderLabels(['PGM','原始 WAV','偏移（秒）','匹配结果'])
        self.sound_matches.horizontalHeaderItem(3).setToolTip('波形匹配显示相关度；仅时码匹配显示“时码”。')
        self.sound_matches.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.sound_matches.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch);self.sound_matches.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeMode.Stretch)
        self.sound_matches.setColumnWidth(2,116);self.sound_matches.setColumnWidth(3,124)
        self.sound_matches.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.sound_matches.setAlternatingRowColors(True);self.sound_matches.setWordWrap(False);self.sound_matches.verticalHeader().hide()
        results_layout.addWidget(self.sound_matches,1);self.sound_data_tabs.addTab(self.sound_results_page,'合板结果')

        self.sound_sources_page=QWidget();sources_layout=QVBoxLayout(self.sound_sources_page)
        self.sound_table=QTableWidget(0,5);self.sound_table.setHorizontalHeaderLabels(['原始声音文件','源时码','声音卷号','录音日期','长度／格式'])
        self.sound_table.setColumnWidth(0,240)
        for column,width in ((1,112),(2,110),(3,112),(4,210)):self.sound_table.setColumnWidth(column,width)
        self.sound_table.horizontalHeader().setMinimumSectionSize(100)
        self.sound_table.horizontalHeader().setStretchLastSection(True)
        self.sound_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows);self.sound_table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection);self.sound_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.sound_table.setAlternatingRowColors(True);self.sound_table.setWordWrap(False);self.sound_table.verticalHeader().hide()
        sources_layout.addWidget(self.sound_table,1)
        self.sound_pair_actions=QWidget();pair_actions=QHBoxLayout(self.sound_pair_actions);pair_actions.setContentsMargins(0,0,0,0)
        self.pair_sound_button=QPushButton('设为／交换左右声道对');self.pair_sound_button.setToolTip('选择两条单声道 WAV；首次设置上方为左、下方为右，再次点击交换左右。')
        self.pair_sound_button.clicked.connect(lambda:self.guarded(self.pair_selected_sound));pair_actions.addWidget(self.pair_sound_button)
        self.auto_sound_button=QPushButton('恢复自动声道');self.auto_sound_button.clicked.connect(lambda:self.guarded(self.reset_selected_sound_pair));pair_actions.addWidget(self.auto_sound_button)
        pair_actions.addStretch();sources_layout.addWidget(self.sound_pair_actions)
        self.sound_data_tabs.addTab(self.sound_sources_page,'原始 WAV／声道配对')
        self.sound_status=QLabel('设置确认后，点击上方“开始声音合板”。');self.sound_status.setWordWrap(True)
        self.sound_status.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred)
        data_layout.addWidget(self.sound_status)
        self.sound_split.addWidget(self.sound_data_panel)
        self.sound_split.setStretchFactor(0,0);self.sound_split.setStretchFactor(1,1);self.sound_split.setSizes([310,790])
        self.match_sound_button=QPushButton('匹配声音',self);self.match_sound_button.hide()
        self.cancel_sound_button=QPushButton('取消',self);self.cancel_sound_button.hide();self.cancel_sound_button.setEnabled(False)
        self.sound_progress=QProgressBar(self);self.sound_progress.hide()
        self.tabs.addTab(page,'声音设置')

    def sound_settings_changed(self,*_):
        if not hasattr(self,'sound_matches'):return
        self.sound_matches.setRowCount(0);self.sound_progress.setValue(0)
        self.sound_status.setText('设置已更新；点击“开始声音合板”保存并匹配。')

    def add_sound(self):
        if self.library_context.get('session_id'):
            return self.request_unified_import()
        paths,_=QFileDialog.getOpenFileNames(self,'选择本场主声音WAV','','WAV (*.wav *.WAV)')
        if paths:self.populate_sound(self.sound_rows()+[dict(path=p,channel='main') for p in paths])

    def sound_rows(self):
        return [copy.deepcopy(self.sound_table.item(r,0).data(Qt.ItemDataRole.UserRole)) for r in range(self.sound_table.rowCount())]

    def pair_selected_sound(self):
        selected=sorted({index.row() for index in self.sound_table.selectedIndexes()})
        if len(selected)!=2:raise AnalysisError('请选择同一条录音的两个单声道 WAV；上方为左声道，下方为右声道。')
        rows=self.sound_rows();infos=getattr(self,'_sound_infos',{})
        for index in selected:
            if infos[rows[index]['path']]['channels']!=1:raise AnalysisError('左右声道对必须由两个单声道 WAV 组成。')
        selected_rows=[rows[index] for index in selected]
        existing_pair=selected_rows[0].get('sound_pair')
        swapping=bool(existing_pair and existing_pair==selected_rows[1].get('sound_pair')
            and {row.get('sound_role') for row in selected_rows}=={'L','R'})
        roles=[('R' if row['sound_role']=='L' else 'L') for row in selected_rows] if swapping else ('L','R')
        # Reassigning either member also releases its previous manual partner.
        previous={rows[index].get('sound_pair') for index in selected}-{None,''}
        for row in rows:
            if row.get('sound_pair') in previous:
                row.pop('sound_pair',None);row.pop('sound_role',None)
        identity=existing_pair if swapping else str(uuid.uuid4())
        for index,role in zip(selected,roles):
            rows[index].update(channel='main',sound_role=role,sound_pair=identity)
        from sound_pairs import resolve_sound_pairs
        resolve_sound_pairs([rows[index] for index in selected],
            [copy.deepcopy(infos[rows[index]['path']]) for index in selected])
        self.populate_sound(rows,prepared_infos=infos)
        self.sound_status.setText(('已交换' if swapping else '已设置')+'左右声道对；点击“开始声音合板”保存并重新匹配。')

    def reset_selected_sound_pair(self):
        selected={index.row() for index in self.sound_table.selectedIndexes()}
        if not selected:raise AnalysisError('请先选择需要恢复自动声道的 WAV。')
        rows=self.sound_rows();pairs={rows[index].get('sound_pair') for index in selected}-{None,''}
        for index,row in enumerate(rows):
            if index in selected or row.get('sound_pair') in pairs:
                row.pop('sound_role',None);row.pop('sound_pair',None);row['channel']='main'
        self.populate_sound(rows,prepared_infos=getattr(self,'_sound_infos',{}))
        self.sound_status.setText('已恢复自动声道；点击“开始声音合板”保存并重新匹配。')

    def populate_sound(self,rows,prepared_infos=None):
        from sound_sync import probe_wav
        from sound_pairs import sound_channel_hint
        from core import frames_tc
        infos=[];sources=[];seen=set();fps=int(self.fps.currentText())
        for row in rows:
            info=(prepared_infos or {}).get(row['path']) or probe_wav(row['path'],fps=fps)
            if info['path'] in seen:continue
            seen.add(info['path']);infos.append(info)
            source=copy.deepcopy(row);source['path']=info['path'];source.setdefault('channel','main')
            if source.get('channel') in ('L','R'):source.setdefault('sound_role',source['channel'])
            sources.append(source)
        self._sound_infos={info['path']:info for info in infos}
        self.sound_table.setRowCount(0)
        for source,info in zip(sources,infos):
            r=self.sound_table.rowCount();self.sound_table.insertRow(r)
            name=QTableWidgetItem(Path(info['path']).name);name.setData(Qt.ItemDataRole.UserRole,source);name.setToolTip(info['path']);self.sound_table.setItem(r,0,name)
            start=info.get('start',0)
            if not info.get('has_timecode',False):timecode='未写入'
            elif 0<=start<24*3600*48000:timecode=frames_tc(start*fps//48000,fps)
            else:timecode=f'跨日：{start}样本'
            duration=info['samples']/info.get('sample_rate',48000)
            role=source.get('sound_role') or (source.get('channel') if source.get('channel') in ('L','R') else '')
            role_text={'L':'左声道','R':'右声道','main':'主声音'}.get(role,'立体声' if info['channels']==2 else '自动')
            if role in ('','auto') and info['channels']==1:
                hint,_=sound_channel_hint(info['path'])
                if hint:role_text='自动'+{'L':'左声道','R':'右声道'}[hint]
            detail=f"{duration:.3f}s／{info.get('sample_rate',48000)//1000}kHz／{info['depth']}bit／{info['channels']}声道／{role_text}"
            for col,value in ((1,timecode),(2,info.get('sound_roll') or '未写入'),(3,info.get('recording_date') or '未写入'),(4,detail)):
                item=QTableWidgetItem(value);item.setToolTip(value);self.sound_table.setItem(r,col,item)
        self.sound_settings_changed();self.refresh_media_summary()

    def remove_sound(self):
        if self.library_context.get('session_id'):
            self.status.setText('已登记的声音由统一素材入口管理。')
            return
        for row in sorted({i.row() for i in self.sound_table.selectedIndexes()},reverse=True):self.sound_table.removeRow(row)
        self.sound_settings_changed()

    def set_sound_busy(self,busy):
        if hasattr(self,'library_panel'):self.library_panel.set_external_busy(busy)
        for widget in (self.sound_table,self.pair_sound_button,self.auto_sound_button,self.sound_sync_mode,self.match_sound_button,self.save_sound_button):widget.setEnabled(not busy)
        self.cancel_sound_button.setEnabled(busy)
        for button in self.project_buttons:button.setEnabled(not busy)
        for i in range(self.tabs.count()):self.tabs.setTabEnabled(i,not busy or self.tabs.widget(i) is self.sound_page)

    def start_sound_match(self):
        self.require_current_library_materials()
        if (self.sound_worker and self.sound_worker.isRunning()) or (self.worker and self.worker.isRunning()) or (self.finder and self.finder.isRunning()):return
        if not self.cut_xml.text():raise AnalysisError('请先导入PGM切点XML。')
        c=self.config()
        if not c['sound_sources']:raise AnalysisError('本场尚未登记主WAV，请在“场次素材”的“导入场次素材”中追加并重新载入场次。')
        self.sound_data_tabs.setCurrentWidget(self.sound_results_page)
        self.sound_matches.setRowCount(0);self.sound_progress.setValue(0);self.set_sound_busy(True)
        self.sound_worker=SoundWorker(c,self);self.sound_worker.progress.connect(self.sound_match_progress);self.sound_worker.done.connect(self.sound_match_done);self.sound_worker.failed.connect(self.sound_match_failed);self.sound_worker.start()

    def sound_match_progress(self,n,text):
        self.sound_progress.setValue(n);self.sound_status.setText(text)

    def sound_match_done(self,result):
        self.set_sound_busy(False);self.sound_progress.setValue(100);self.sound_matches.setRowCount(0)
        self.sound_data_tabs.setCurrentWidget(self.sound_results_page)
        wavs,events=result
        for event in events:
            row=self.sound_matches.rowCount();self.sound_matches.insertRow(row)
            offset=event.get('offset_seconds');score=event.get('score')
            values=(Path(event.get('pgm_path','')).name,Path(event['path']).name,f'{float(offset):.6f}' if offset is not None else '—',f'{float(score):.3f}' if score is not None else ('时码' if event.get('method')=='timecode' else '—'))
            for col,value in enumerate(values):self.sound_matches.setItem(row,col,QTableWidgetItem(value))
            self.sound_matches.item(row,0).setToolTip(event.get('pgm_path',''));self.sound_matches.item(row,1).setToolTip(event['path'])
        gaps=[];seen=set()
        for w in wavs:
            for report in w.get('sync_report',[]):
                key=(report.get('pgm_path'),report.get('record_start_seconds'),report.get('duration_seconds'))
                if report.get('status')!='uncovered' or key in seen:continue
                seen.add(key);gaps.append(report)
                row=self.sound_matches.rowCount();self.sound_matches.insertRow(row)
                first=report.get('pgm_start_seconds',0);last=report.get('pgm_end_seconds',first+report.get('duration_seconds',0))
                values=(Path(report.get('pgm_path','')).name,'未覆盖（留空）',f'{first:.3f}–{last:.3f}','缺口')
                for col,value in enumerate(values):self.sound_matches.setItem(row,col,QTableWidgetItem(value))
                self.sound_matches.item(row,2).setToolTip('缺口在该PGM文件内的起止秒数')
        if gaps:
            duration=sum(gap.get('duration_seconds',0) for gap in gaps)
            self.sound_status.setText(f'已匹配{len(events)}段声音；另有{len(gaps)}处声音缺口（共{duration:.3f}秒）将留空并标记。')
        else:self.sound_status.setText(f'已匹配{len(events)}段声音；导出XML时会按当前设置重新核对。')

    def sound_match_failed(self,message):
        self.set_sound_busy(False);self.sound_status.setText('声音匹配未完成：'+message)

    def build_rois(self):
        page = self.roi_page = QWidget()
        layout = QHBoxLayout(page)
        self.roi_split = QSplitter(Qt.Orientation.Horizontal)
        self.roi_split.setChildrenCollapsible(False)
        self.roi_split.setHandleWidth(6)
        layout.addWidget(self.roi_split)

        # Settings own a scrollable column, never share the preview geometry.
        self.roi_settings = QWidget()
        settings = QVBoxLayout(self.roi_settings)
        settings.setContentsMargins(8, 8, 8, 8)
        form = QFormLayout()
        self.roi_cam = QComboBox(); self.roi_cam.addItems(list(self.camera_ids))
        form.addRow('当前机位', self.roi_cam)
        self.preview_index = spin(0, 10000000)
        form.addRow('预览帧', self.preview_index)
        settings.addLayout(form)
        row = QHBoxLayout()
        for title, action in (('读取画面', self.refresh_pgm), ('测试识别', self.test_frame)):
            button = QPushButton(title)
            button.clicked.connect(lambda checked=False, fn=action: self.guarded(fn))
            row.addWidget(button)
        settings.addLayout(row)
        settings.addWidget(QLabel('浏览镜头'))
        self.shot_choice = QComboBox()
        self.shot_choice.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.shot_choice.setMinimumContentsLength(12)
        self.shot_choice.activated.connect(lambda *_: self.guarded(self.load_selected_shot))
        settings.addWidget(self.shot_choice)
        browse = QHBoxLayout()
        for title, delta in [('上一镜头', -1), ('下一镜头', 1)]:
            button = QPushButton(title)
            button.clicked.connect(lambda checked=False, d=delta: self.guarded(lambda: self.browse_shot(d)))
            browse.addWidget(button)
        settings.addLayout(browse)
        self.find_example = QPushButton('查找此机位画面')
        self.find_example.clicked.connect(lambda: self.guarded(self.find_camera_example))
        settings.addWidget(self.find_example)
        self.cancel_example = QPushButton('取消查找'); self.cancel_example.setEnabled(False)
        self.cancel_example.clicked.connect(lambda: self.finder.requestInterruption() if self.finder else None)
        settings.addWidget(self.cancel_example)
        labels = QGroupBox('机位标号')
        fields = QFormLayout(labels)
        fields.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        fields.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.camera_count_label=QLabel('本场 4 个机位：A / B / C / D')
        self.camera_count_label.setWordWrap(True)
        fields.addRow(self.camera_count_label)
        self.label_preset = QComboBox()
        self.label_preset.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.label_preset.setMinimumContentsLength(9)
        for text,value in [('自定义','custom'),('第一天旧标号（A无号）','day1'),('后续标号（机位字母）','later')]:
            self.label_preset.addItem(text,value)
        fields.addRow('快捷设置',self.label_preset)
        self.label_match_mode = QComboBox()
        self.label_match_mode.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.label_match_mode.setMinimumContentsLength(8)
        for text,value in [('开头匹配','prefix'),('完整匹配','exact'),('旧项目兼容','legacy')]:
            self.label_match_mode.addItem(text,value)
        self.label_match_mode.setToolTip('开头匹配：B可识别B0001、B0002、B003C001；440可识别440_0001、440_0002。')
        fields.addRow('匹配方式',self.label_match_mode)
        self.infer_a_blank = QCheckBox('A无标号：空白判为A')
        self.infer_a_blank.setToolTip('只框选其他机位的标号区域。该区域均未读到文字时判为A；模糊、低置信度或冲突仍标为未知。')
        fields.addRow(self.infer_a_blank)
        self.label_values_host=QWidget()
        self.label_values_form=QFormLayout(self.label_values_host)
        self.label_values_form.setContentsMargins(0,0,0,0)
        fields.addRow(self.label_values_host)
        self.label_fields = {}
        for cam in self.camera_ids:
            edit = QLineEdit(cam)
            edit.setToolTip('填写不会变化的开头，例如B或440，再框选画面中的完整机位号。无需填写后面的卷号、条号。')
            self.label_fields[cam] = edit
            self.label_values_form.addRow(cam + ' 标号', edit)
        self.roi_copy_button = QPushButton('共用此框')
        self.roi_copy_button.clicked.connect(lambda: self.guarded(self.copy_roi))
        fields.addRow(self.roi_copy_button)
        self.confidence = QDoubleSpinBox()
        self.confidence.setRange(.1, 1); self.confidence.setSingleStep(.05); self.confidence.setValue(.65)
        fields.addRow('识别阈值', self.confidence)
        self.label_preset.activated.connect(self.apply_label_preset)
        self.label_match_mode.currentIndexChanged.connect(self.label_settings_changed)
        self.infer_a_blank.toggled.connect(self.label_settings_changed)
        for edit in self.label_fields.values():edit.textChanged.connect(self.label_settings_changed)
        self.confidence.valueChanged.connect(self.label_settings_changed)
        settings.addWidget(labels)
        settings.addStretch(1)
        self.roi_settings_scroll = QScrollArea()
        self.roi_settings_scroll.setWidgetResizable(True)
        self.roi_settings_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.roi_settings_scroll.setWidget(self.roi_settings)
        self.roi_settings_scroll.setMinimumWidth(245)
        self.roi_split.addWidget(self.roi_settings_scroll)

        self.roi_preview_host = QWidget()
        preview = QVBoxLayout(self.roi_preview_host)
        preview.setContentsMargins(0, 0, 0, 0)
        self.canvas = Canvas(); self.canvas.camera = 'A'
        self.canvas.setMinimumSize(320, 180)
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.roi_cam.currentTextChanged.connect(self.canvas.set_camera)
        self.canvas.selected.connect(lambda *_: self.invalidate_analysis())
        preview.addWidget(self.canvas, 1)
        self.preview_slider = QSlider(Qt.Orientation.Horizontal); self.preview_slider.setRange(0, 0)
        self.preview_timer=QTimer(self);self.preview_timer.setSingleShot(True);self.preview_timer.setInterval(45)
        self.preview_timer.timeout.connect(lambda:self.guarded(self.scrub_preview))
        self.preview_slider.valueChanged.connect(lambda *_:self.preview_timer.start() if self.preview_slider.isSliderDown() else None)
        self.preview_slider.sliderReleased.connect(lambda: self.guarded(self.scrub_preview))
        preview.addWidget(self.preview_slider)
        self.frame_status = QLabel(''); self.frame_status.setWordWrap(True)
        preview.addWidget(self.frame_status)
        self.roi_split.addWidget(self.roi_preview_host)
        self.roi_split.setStretchFactor(0, 0); self.roi_split.setStretchFactor(1, 1)
        self.roi_split.setSizes([280, 800])
        self.roi_split.handle(1).setToolTip('左右拖动调整设置区与预览画面宽度')
        self.tabs.addTab(page, '机位标号')

    def active_label_cameras(self):
        return tuple(cam for cam in self.camera_ids if cam!='A' or not self.infer_a_blank.isChecked())

    def set_camera_ids(self,identities):
        identities=tuple(identities)
        if self.camera_ids==identities:return
        self.camera_ids=identities
        while self.label_values_form.rowCount():self.label_values_form.removeRow(0)
        self.label_fields={}
        for cam in identities:
            field=QLineEdit(cam)
            field.setToolTip('填写不会变化的开头，例如B、440或F，再框选完整机位号。')
            self.label_fields[cam]=field;self.label_values_form.addRow(cam+' 标号',field)
            field.textChanged.connect(self.label_settings_changed)
        self.camera_count_label.setText(f'本场 {len(identities)} 个机位：'+ ' / '.join(identities))
        self.add_cam.clear();self.add_cam.addItems(list(identities))

    def sync_label_controls(self):
        active=self.active_label_cameras()
        if 'A' in self.label_fields:self.label_fields['A'].setEnabled('A' in active)
        current=self.roi_cam.currentText()
        if [self.roi_cam.itemText(i) for i in range(self.roi_cam.count())]!=list(active):
            blocker=QSignalBlocker(self.roi_cam)
            self.roi_cam.clear();self.roi_cam.addItems(list(active))
            self.roi_cam.setCurrentText(current if current in active else (active[0] if active else ''))
            del blocker
        self.canvas.set_camera(self.roi_cam.currentText())
        self.roi_cam.setEnabled(bool(active));self.roi_copy_button.setEnabled(bool(active))
        self.find_example.setEnabled(bool(active))

    def restore_label_settings(self,c):
        self.set_camera_ids(cameras_for(c))
        controls=[self.label_preset,self.label_match_mode,self.infer_a_blank,self.confidence,*self.label_fields.values()]
        blockers=[QSignalBlocker(widget) for widget in controls]
        self.infer_a_blank.setChecked(bool(c.get('infer_a_when_blank',False)))
        mode=c.get('label_match_mode','legacy')
        self.label_match_mode.setCurrentIndex(max(0,self.label_match_mode.findData(mode)))
        self.label_counter_suffix=bool(c.get('counter_suffix',False))
        for cam,field in self.label_fields.items():field.setText(c.get('labels',{}).get(cam,cam))
        self.confidence.setValue(c.get('ocr_confidence',.65))
        values={cam:self.label_fields[cam].text().strip().upper() for cam in self.active_label_cameras()}
        preset='custom'
        if mode=='prefix':
            if self.infer_a_blank.isChecked() and values=={cam:('440' if cam=='C' else cam) for cam in self.active_label_cameras()}:preset='day1'
            elif not self.infer_a_blank.isChecked() and values=={cam:cam for cam in self.camera_ids}:preset='later'
        self.label_preset.setCurrentIndex(self.label_preset.findData(preset))
        self.sync_label_controls();self.example_frames={};self.example_key=None
        del blockers

    def apply_label_preset(self,index):
        preset=self.label_preset.itemData(index)
        if preset=='custom':return
        self.restore_label_settings(dict(camera_ids=list(self.camera_ids),label_match_mode='prefix',infer_a_when_blank=preset=='day1',
            labels={cam:('440' if cam=='C' and preset=='day1' else cam) for cam in self.camera_ids},ocr_confidence=self.confidence.value()))
        self.invalidate_analysis()
        self.frame_status.setText('规则已更新；确认标号框后点击“识别镜头”，可批量识别本场全部切点。')

    def label_settings_changed(self,*args):
        self.label_preset.setCurrentIndex(self.label_preset.findData('custom'))
        self.sync_label_controls();self.example_frames={};self.example_key=None
        self.invalidate_analysis()

    def build_results(self):
        self.results_page=QWidget()
        layout=QVBoxLayout(self.results_page)
        self.result_tools=QHBoxLayout()
        self.run_button=QPushButton('识别镜头')
        self.run_button.clicked.connect(lambda:self.guarded(self.start_analysis))
        self.cancel_button=QPushButton('取消');self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(lambda:self.worker.requestInterruption() if self.worker else None)
        self.export_button=QPushButton('保存机位结果');self.export_button.setEnabled(False)
        self.export_button.clicked.connect(lambda:self.guarded(self.export_xml))
        for button in (self.run_button,self.cancel_button,self.export_button):self.result_tools.addWidget(button)
        layout.addLayout(self.result_tools)
        self.progress=QProgressBar();self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.review_split=QSplitter(Qt.Orientation.Horizontal)
        self.review_split.setChildrenCollapsible(False);self.review_split.setHandleWidth(6)
        layout.addWidget(self.review_split,1)
        self.review_list_host=QWidget()
        left=QVBoxLayout(self.review_list_host);left.setContentsMargins(0,0,0,0);left.setSpacing(6)
        self.edit_tools=QHBoxLayout();self.edit_tools.addWidget(QLabel('镜头机位'));self.edit_tools.addStretch()
        left.addLayout(self.edit_tools)
        self.results=QTableWidget(0,4)
        self.results.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.results.setHorizontalHeaderLabels(['入点帧','出点帧','机位','状态'])
        header=self.results.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column,width in enumerate((76,76,52,148)):self.results.setColumnWidth(column,width)
        header.setStretchLastSection(True)
        self.results.verticalHeader().hide()
        self.results.setMinimumSize(295,120)
        self.results.currentCellChanged.connect(lambda row,col,oldrow,oldcol:self.result_selected())
        left.addWidget(self.results,1)
        review=QHBoxLayout();self.review_frame=spin(0,10000000)
        self.review_frame.setMaximumWidth(110)
        review.addWidget(QLabel('核对帧'));review.addWidget(self.review_frame)
        show=QPushButton('预览');show.clicked.connect(lambda:self.guarded(self.show_review));review.addWidget(show)
        left.addLayout(review)
        steps=QHBoxLayout()
        for title,delta in (('前一帧',-1),('后一帧',1)):
            button=QPushButton(title)
            button.clicked.connect(lambda checked=False,d=delta:self.step_review(d))
            steps.addWidget(button)
        left.addLayout(steps)
        left.addWidget(QLabel('机位标号区域'))
        self.label_picture=ReviewPicture('标号区域')
        self.label_picture.setMinimumWidth(240);self.label_picture.setFixedHeight(64)
        self.label_picture.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Fixed)
        left.addWidget(self.label_picture)
        # Keep the picture fully visible in short laptop windows. Only the
        # list and frame controls need to scroll when vertical room runs out.
        self.review_list_scroll=QScrollArea()
        self.review_list_scroll.setWidgetResizable(True)
        self.review_list_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.review_list_scroll.setWidget(self.review_list_host)
        self.review_split.addWidget(self.review_list_scroll)
        self.review_preview_host=QWidget()
        right=QVBoxLayout(self.review_preview_host);right.setContentsMargins(0,0,0,0)
        self.pgm_picture=ReviewPicture('选择镜头后预览画面')
        self.pgm_picture.setMinimumSize(320,160)
        right.addWidget(self.pgm_picture,1)
        self.review_split.addWidget(self.review_preview_host)
        self.review_split.setStretchFactor(0,0);self.review_split.setStretchFactor(1,1)
        self.review_split.setSizes([350,750])
        self.review_split.handle(1).setToolTip('左右拖动调整镜头列表与核对画面宽度')
        reconform = QPushButton('原片回套…'); reconform.clicked.connect(lambda:self.guarded(self.reconform_xml)); self.result_tools.addWidget(reconform)
        verify = QPushButton('确认机位'); verify.clicked.connect(lambda:self.guarded(self.confirm_segment))
        self.edit_tools.addWidget(verify)
        self.tabs.addTab(self.results_page,'机位识别与核对')

    def choose_cut_xml(self):
        # Legacy API routes to the same complete scene-material importer.
        return self.request_unified_import()

    def set_pgm_sources(self,cut,timecodes=None):
        self.cancel_camera_previews()
        self._preview_cut=cut
        self._shot_starts=[shot['start'] for shot in cut.get('shots',[])]
        self._pgm_starts=[block['start'] for block in cut['pgm_sources']]
        self.preview_shots = cut['shots']; self.example_frames = {}
        blocked=self.shot_choice.blockSignals(True)
        self.shot_choice.clear()
        self.shot_choice.addItems([f'镜头 {number} / 帧 {shot["start"]}' for number,shot in enumerate(self.preview_shots,1)])
        self.shot_choice.blockSignals(blocked)
        self.preview_slider.setRange(0,cut['duration']-1)
        self.pgm_table.setRowCount(0)
        for block in cut['pgm_sources']:
            row = self.pgm_table.rowCount(); self.pgm_table.insertRow(row)
            values = (block['path'],(timecodes or {}).get(block['path']) or block['start_tc'],block['frames'],block['xml_path'])
            for col,value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if col==0:item.setToolTip(str(value))
                if col!=1: item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.pgm_table.setItem(row,col,item)
        self.relocate_button.setEnabled(self.pgm_table.rowCount()>0)
        self.pgm.setText(cut['pgm_info']['path']); self.pgm_tc.setText(self.pgm_table.item(0,1).text());self.refresh_media_summary()

    def import_cut_xml(self,path,override=None,registered_only=False):
        self.require_current_library_materials()
        from cut_xml import read_cut_xml
        restricted=registered_only or bool(self.library_context.get('session_id'))
        registered_timecodes=self.config().get('pgm_timecodes',{}) if restricted else {}
        overrides = override if isinstance(override,dict) else {}
        cut = read_cut_xml(path,override if isinstance(override,str) else '',overrides)
        if restricted:
            from library_bridge import canonical_path
            from library_workflow import _pgms
            registered={canonical_path(str(Path(p).resolve())) for p in _pgms(self.config())}
            unknown=[block['path'] for block in cut['pgm_sources'] if canonical_path(str(Path(block['path']).resolve())) not in registered]
            if unknown:raise AnalysisError('切点XML包含尚未登记到本拍摄场次的PGM，请先通过统一素材入口增补：'+unknown[0])
            if int(self.fps.currentText())!=cut['fps']:
                raise AnalysisError('切点XML帧率与已登记的本场素材不同，请核对后重新关联。')
        self.sound_settings_changed()
        self.registered_pgm_timecodes=registered_timecodes
        self.pgm_overrides = overrides; self.cut_xml_paths=[cut['xml_path']];self.cut_xml.setText(cut['xml_path']); self.cut_digest = cut['xml_digest']
        self.set_pgm_sources(cut,registered_timecodes); self.fps.setCurrentText(str(cut['fps']))
        self.preview_index.setMaximum(cut['duration']-1); self.preview_index.setValue(0); self.loaded_pgm = ''
        self.timeline_tc.setText(cut['timeline_start_tc']); self.confirmed.setChecked(False); self.refresh_pgm()
        self.invalidate_analysis()
        status = f'已导入{len(cut["pgm_sources"])}条PGM、{len(cut["shots"])}个镜头，共{cut["duration"]}帧。'
        if cut.get('xml_declared_duration',cut['duration']) != cut['duration']:
            status += 'XML总长度少1帧，已按完整PGM核对。'
        self.status.setText(status)

    def relocate_pgm(self):
        if not self.cut_xml.text() or not self.pgm_table.rowCount():
            raise AnalysisError('请先导入PGM切点XML，载入PGM文件后再重新定位。')
        row = self.pgm_table.currentRow()
        if row<0: row=0
        item = self.pgm_table.item(row,3)
        if item is None or not item.text(): raise AnalysisError('所选PGM记录不完整，请重新导入PGM切点XML。')
        original = item.text()
        path,_ = QFileDialog.getOpenFileName(self,f'重新定位 {Path(original).name}','','视频 (*.mov *.mp4 *.mxf)')
        if path:
            overrides = dict(getattr(self,'pgm_overrides',{})); overrides[original] = path
            self.guarded(lambda:self.import_cut_xml(self.cut_xml.text(),overrides))

    def invalidate_analysis(self):
        self._camera_epoch+=1
        if hasattr(self,'camera_task_loader'):self.camera_task_loader.invalidate()
        if hasattr(self,'review_task_loader'):self.review_task_loader.invalidate()
        if self.finder and self.finder.isRunning():self.finder.requestInterruption()
        self.analysis_config = None
        if self.results.rowCount():self.results.setRowCount(0)
        self.export_button.setEnabled(False)

    def cancel_camera_previews(self):
        self._camera_epoch+=1;self._preview_epoch+=1
        for name in ('scene_frame_loader','camera_task_loader','review_task_loader','preview_prepare_loader'):
            loader=getattr(self,name,None)
            if loader:loader.invalidate()
        if hasattr(self,'preview_timer'):self.preview_timer.stop()
        self.last_frame=None;self._last_frame_key=None;self.pgm_info=None
        if hasattr(self,'canvas'):self.canvas.clear()

    def preview_cut_ready(self,callback):
        """The loaded cut index is immutable for previews; validation stays in workers."""
        if self._preview_cut is not None:
            callback(self._preview_cut);return
        from cut_xml import timeline_info
        config=self.preview_file_config();epoch=self._preview_epoch
        def ready(cut,error):
            if epoch!=self._preview_epoch:return
            if error:self.status.setText('预览未能读取：'+error);return
            self._preview_cut=cut
            self._shot_starts=[shot['start'] for shot in cut.get('shots',[])]
            self._pgm_starts=[block['start'] for block in cut['pgm_sources']]
            callback(cut)
        self.preview_prepare_loader.request(lambda cancelled:timeline_info(config,False),ready)

    def preview_source(self,cut,frame):
        blocks=cut['pgm_sources']
        number=bisect_right(self._pgm_starts,frame)-1
        if 0<=number<len(blocks):
            block=blocks[number]
            if block['start']<=frame<block['end']:return block,frame-block['start']
        raise AnalysisError('预览帧超出整场时间线范围。')

    def refresh_pgm(self):
        self.preview_timer.stop()
        self.preview_cut_ready(lambda cut:self.request_scene_preview(cut,self.preview_index.value()))

    def load_selected_shot(self):
        number=self.shot_choice.currentIndex()
        if not 0<=number<len(self.preview_shots):raise AnalysisError('请先导入PGM切点XML。')
        self.preview_index.setValue(self.preview_shots[number]['start']);self.refresh_pgm()

    def browse_shot(self,delta):
        if not self.preview_shots:raise AnalysisError('请先导入PGM切点XML。')
        self.shot_choice.setCurrentIndex(max(0,min(len(self.preview_shots)-1,self.shot_choice.currentIndex()+delta)))
        self.load_selected_shot()

    def scrub_preview(self):
        self.preview_timer.stop()
        self.preview_index.setValue(self.preview_slider.value());self.refresh_pgm()

    def find_camera_example(self):
        from cut_xml import timeline_info
        from preview_search import ExampleWorker
        if (self.finder and self.finder.isRunning()) or (self.sound_worker and self.sound_worker.isRunning()) or (self.worker and self.worker.isRunning()):return
        if not self.active_label_cameras():
            self.frame_status.setText('本场只有A机且不带标号，可直接识别镜头。');return
        if self._preview_cut is None:
            self.preview_cut_ready(lambda cut:self.find_camera_example());return
        config=self.label_config();config.update(self.preview_file_config());cut=self._preview_cut
        key=(cut['xml_digest'],tuple(sorted(config['labels'].items())),tuple(sorted(config['pgm_overrides'].items())),config['ocr_confidence'],
             config['label_match_mode'],config['infer_a_when_blank'],config['counter_suffix'])
        if getattr(self,'example_key',None)!=key:self.example_frames={};self.example_key=key
        target=self.roi_cam.currentText()
        if target in self.example_frames:
            self.preview_index.setValue(self.example_frames[target]['frame']);self.refresh_pgm()
            self.frame_status.setText(f'已定位{target}示例；请确认画面并框选标号。');return
        self.find_target=target;self._example_epoch=self._camera_epoch;self.find_example.setEnabled(False);self.cancel_example.setEnabled(True)
        for button in self.project_buttons:button.setEnabled(False)
        self.tabs.setTabEnabled(0,False);self.tabs.setTabEnabled(self.tabs.indexOf(self.results_page),False)
        self.finder=ExampleWorker(config,cut,target);self.finder.progress.connect(lambda n,text:self.frame_status.setText(text))
        self.finder.done.connect(self.examples_done);self.finder.failed.connect(self.examples_failed);self.finder.start()

    def finish_examples(self):
        self.find_example.setEnabled(True);self.cancel_example.setEnabled(False)
        for button in self.project_buttons:button.setEnabled(True)
        self.tabs.setTabEnabled(0,True);self.tabs.setTabEnabled(self.tabs.indexOf(self.results_page),True)

    def examples_done(self,found):
        self.finish_examples()
        if getattr(self,'_example_epoch',None)!=self._camera_epoch:return
        self.example_frames.update(found)
        target=self.find_target
        if target in self.example_frames:
            self.preview_index.setValue(self.example_frames[target]['frame']);self.guarded(self.refresh_pgm)
            self.frame_status.setText(f'已定位{target}示例；请确认画面并框选标号。')
        else:self.frame_status.setText(f'未找到{target}示例；可用上一/下一镜头或拖动进度条浏览。')

    def examples_failed(self,message):
        self.finish_examples()
        if getattr(self,'_example_epoch',None)==self._camera_epoch:self.frame_status.setText('示例查找未完成：'+message+'；可逐镜头浏览。')

    def copy_roi(self):
        roi = self.canvas.rois.get(self.roi_cam.currentText())
        if roi is None: raise AnalysisError('请先框选当前机位标号区域。')
        for cam in self.active_label_cameras(): self.canvas.rois[cam] = list(roi)
        self.canvas.update(); self.invalidate_analysis()

    def label_config(self):
        return dict(camera_ids=list(self.camera_ids),camera_count=len(self.camera_ids),
            labels={c:self.label_fields[c].text().strip() for c in self.active_label_cameras()},
            label_rois={c:list(r) for c,r in self.canvas.rois.items() if c in self.active_label_cameras()},
            label_match_mode=self.label_match_mode.currentData(),infer_a_when_blank=self.infer_a_blank.isChecked(),
            counter_suffix=self.label_counter_suffix,ocr_confidence=self.confidence.value())

    def preview_file_config(self):
        return dict(cut_xml=self.cut_xml.text(),cut_xml_paths=list(self.cut_xml_paths),
            pgm=self.pgm.text().strip(),pgm_overrides=dict(getattr(self,'pgm_overrides',{})),
            fps=int(self.fps.currentText()))

    def test_frame(self):
        if not self.canvas.rois and self.active_label_cameras():raise AnalysisError('请先框选标号区域。')
        self.preview_cut_ready(self._test_frame_ready)

    def _test_frame_ready(self,cut):
        config=self.label_config();rois=config['label_rois'];frame_number=self.preview_index.value()
        info,index=self.preview_source(cut,frame_number)
        key=(self.library_context.get('session_id'),info['path'],index)
        frame=self.last_frame if self._last_frame_key==key else None
        epoch=self._camera_epoch
        self.frame_status.setText('正在测试当前画面的机位标号…')
        def operation(cancelled):
            pixels=frame if frame is not None else read_frame(info['path'],index)
            if cancelled():return None
            return LabelReader(config).inspect(pixels,rois)
        def ready(result,error):
            if epoch!=self._camera_epoch or self.preview_index.value()!=frame_number:return
            if error:self.frame_status.setText('测试识别未完成：'+error);return
            if result is None:return
            cam,reason,confidence,texts=result
            self.frame_status.setText(f"判断：{cam}；{REASONS.get(reason,reason)}；OCR置信度：{confidence:.2f}\n读到：{' | '.join(texts) or '无文字'}")
        self.camera_task_loader.request(operation,ready)

    def add_sources(self):
        if self.library_context.get('session_id'):
            return self.request_unified_import()
        files,_ = QFileDialog.getOpenFileNames(self,'选择本场代理文件','','视频文件 (*.mov *.mp4 *.mxf);;全部文件 (*)')
        for path in files:
            def add(path=path):
                info=probe(path); row=self.sources.rowCount(); self.sources.insertRow(row)
                for col,value in enumerate((self.add_cam.currentText(),info['path'],info['start_tc'],info['frames'],'','')):
                    self.sources.setItem(row,col,QTableWidgetItem(str(value)))
            self.guarded(add)
        self.update_registry_controls();self.refresh_media_summary()

    def attach_original(self):
        row=self.sources.currentRow()
        if row<0: raise AnalysisError('请先选中代理所在行。')
        path,_=QFileDialog.getOpenFileName(self,'选择这一条代理对应的完整原片','','视频文件 (*.mxf *.mov *.mp4);;全部文件 (*)')
        if path:
            info=probe(path)
            self.sources.setItem(row,4,QTableWidgetItem(info['path']))
            self.sources.setItem(row,5,QTableWidgetItem(info['start_tc']))
            current=self.sources.item(row,2)
            if (current is None or not current.text().strip()) and info['start_tc']:
                self.sources.setItem(row,2,QTableWidgetItem(info['start_tc']))
            self.update_registry_controls()
            self.invalidate_analysis()

    def reconform_xml(self):
        from reconform_ui import ReconformDialog
        dialog=ReconformDialog(self);dialog.exec()
        if dialog.generated_path:self.status.setText(f'原片XML已生成：{dialog.generated_path}')

    def config(self):
        sources = []
        for r in range(self.sources.rowCount()):
            f = [self.sources.item(r,c).text().strip() if self.sources.item(r,c) else '' for c in range(6)]
            uid=self.source_id_map.setdefault(f[1],str(uuid.uuid4()))
            saved=self.sources.item(r,1).data(Qt.ItemDataRole.UserRole) if self.sources.item(r,1) else None
            sources.append(dict(copy.deepcopy(saved) if isinstance(saved,dict) else {},camera=f[0],path=f[1],proxy_path=f[1],start_tc=f[2],frames=int(f[3]) if f[3] else 0,original_path=f[4],original_start_tc=f[5],source_id=uid))
        width,height = map(int,self.resolution.currentText().split('×'))
        result=copy.deepcopy(self.config_extra)
        result.update(copy.deepcopy(self.library_extra))
        result.update(mode='cut_xml',cut_xml=self.cut_xml.text(),cut_xml_paths=list(self.cut_xml_paths),cut_xml_digest=getattr(self,'cut_digest',''),media_workflow='proxy_edit',show_id=self.show_id.currentText().strip(),fps=int(self.fps.currentText()),
            pgm=self.pgm.text().strip(),pgm_start_tc=self.pgm_table.item(0,1).text().strip() if self.pgm_table.rowCount() else '',
            pgm_overrides=dict(getattr(self,'pgm_overrides',{})),pgm_timecodes={**self.registered_pgm_timecodes,**{self.pgm_table.item(r,0).text():self.pgm_table.item(r,1).text().strip() for r in range(self.pgm_table.rowCount())}},timeline_start_tc=self.timeline_tc.text().strip(),
            camera_ids=list(self.camera_ids),camera_count=len(self.camera_ids),camera_offsets={c:self.camera_offsets[c].value() for c in self.camera_ids},calibration_confirmed=self.confirmed.isChecked(),
            label_rois={c:list(r) for c,r in self.canvas.rois.items() if c in self.active_label_cameras()},labels={c:self.label_fields[c].text().strip() for c in self.active_label_cameras()},
            label_match_mode=self.label_match_mode.currentData(),infer_a_when_blank=self.infer_a_blank.isChecked(),counter_suffix=self.label_counter_suffix,ocr_confidence=self.confidence.value(),
            multicam=True,missing_camera_policy='available',missing_camera_preference='A',empty_camera_policy='pgm',external_sound=True,sound_sources=self.sound_rows(),sound_sync_mode=self.sound_sync_mode.currentData(),sound_allow_gaps=True,sound_unmatched_policy='silence',sources=sources,include_pgm_reference=False,timeline_width=width,timeline_height=height,output_dir=self.output.text().strip(),
            library_context=copy.deepcopy(self.library_context),library_media=copy.deepcopy(self.library_media),library_notes=self.library_notes,dialogue_markers=copy.deepcopy(self.dialogue_markers))
        from project_state import _without_credentials
        from sound_fallback import retire_sound_references
        return _without_credentials(retire_sound_references(result))

    def apply_config(self,c,prepared_cut=None,defer_preview=False,sound_infos=None):
        if (self.worker and self.worker.isRunning()) or (self.sound_worker and self.sound_worker.isRunning()) or (self.finder and self.finder.isRunning()): raise AnalysisError('请先结束当前任务。')
        if c.get('mode') != 'cut_xml': raise AnalysisError('旧版全片分析项目不再支持，请导入达芬奇切点XML。')
        self.cancel_camera_previews()
        from sound_fallback import retire_sound_references
        c=retire_sound_references(c)
        from cut_xml import timeline_info
        cut = prepared_cut if prepared_cut is not None else timeline_info(c,False)
        self.set_resolution(c.get('timeline_width',3840),c.get('timeline_height',2160))
        self.preserve_config_fields(c)
        self.library_materials_stale=False
        self.library_context=copy.deepcopy(c.get('library_context',{}));self.library_media=copy.deepcopy(c.get('library_media',[]));self.library_notes=c.get('library_notes','');self.dialogue_markers=copy.deepcopy(c.get('dialogue_markers',[]))
        self.library_extra=self.library_extras(c)
        self.registered_pgm_timecodes=copy.deepcopy(c.get('pgm_timecodes',{}))
        self.source_id_map={row.get('proxy_path') or row.get('path'):row.get('source_id') or str(uuid.uuid4()) for row in c.get('sources',[])}
        self.cut_xml_paths=list(cut.get('xml_paths') or [cut['xml_path']])
        self.cut_xml.setText(cut['xml_path']);self.cut_xml.setToolTip('\n'.join(self.cut_xml_paths)); self.cut_digest = cut['xml_digest']; self.pgm_overrides = dict(c.get('pgm_overrides',{}))
        self.set_pgm_sources(cut,c.get('pgm_timecodes',{}))
        self.pgm.setText(cut['pgm_info']['path']); self.output.setText(c.get('output_dir',str(Path.home()/'Desktop')))
        self.show_id.setCurrentText(c.get('show_id','day01_通条')); self.fps.setCurrentText(str(cut['fps']))
        if c.get('pgm_start_tc'): self.pgm_table.item(0,1).setText(c['pgm_start_tc'])
        self.timeline_tc.setText(c.get('timeline_start_tc','01:00:00:00'))
        for cam in self.camera_offsets: self.camera_offsets[cam].setValue(c.get('camera_offsets',{}).get(cam,0))
        self.confirmed.setChecked(c.get('calibration_confirmed',False))
        self.restore_label_settings(c)
        self.canvas.rois = {}
        for cam,roi in c.get('label_rois',{}).items(): self.canvas.rois[cam] = list(roi)
        self.canvas.update();self.populate_sources(c.get('sources',[]))
        self.sound_sync_mode.setCurrentIndex(max(0,self.sound_sync_mode.findData(c.get('sound_sync_mode','waveform'))))
        self.sound_allow_gaps.setChecked(True)
        self.populate_sound(c.get('sound_sources',[]),prepared_infos=sound_infos)
        self.preview_index.setValue(0); self.loaded_pgm = ''; self.analysis_config = None; self.results.setRowCount(0); self.export_button.setEnabled(False)
        self.update_registry_controls();self.refresh_media_summary()
        if self.pgm.text():
            if defer_preview:self.request_scene_preview(cut,self.preview_index.value())
            else:self.refresh_pgm()
        if c.get('sources') and not all(item.get('proxy_path') for item in c['sources']) and c.get('media_workflow')!='proxy_edit':
            self.status.setText('旧项目路径不会自动变成代理，请在“场次素材”的“导入场次素材”中登记实际代理文件并重新载入场次。')

    def request_scene_preview(self,cut,frame):
        self._preview_cut=cut
        if not self._pgm_starts:self._pgm_starts=[block['start'] for block in cut['pgm_sources']]
        info,index=self.preview_source(cut,frame)
        self.preview_index.setMaximum(cut['duration']-1);self.review_frame.setMaximum(cut['duration']-1)
        identity=self.library_context.get('session_id');epoch=self._preview_epoch
        key=(identity,info['path'],index)
        self.preview_slider.setValue(frame)
        number=bisect_right(self._shot_starts,frame)-1
        if 0<=number<len(self.preview_shots) and self.preview_shots[number]['start']<=frame<self.preview_shots[number]['end']:
            self.shot_choice.setCurrentIndex(number)
        # A previous picture remains visible while the next frame decodes.
        # Re-reading an already displayed frame does not touch the source disk.
        if self._last_frame_key==key and self.last_frame is not None:return
        def ready(value,error):
            if self.library_context.get('session_id')!=identity or self.preview_index.value()!=frame or epoch!=self._preview_epoch:return
            if error:self.status.setText('预览未能读取：'+error);return
            if self.pgm_info and (self.pgm_info['width'],self.pgm_info['height'])!=(info['width'],info['height']):
                self.canvas.rois={};self.canvas.update();self.invalidate_analysis()
            pixels=value['frame'];display=value['display']
            self.pgm_info=info;self.last_frame=pixels;self._last_frame_key=key;self.canvas.set_frame(display)
            self.status.setText(f'{Path(info["path"]).name} / 文件帧{index} / 时间线帧{frame}；共{len(cut["pgm_sources"])}条PGM。')
        self.scene_frame_loader.request(info['path'],index,ready)

    def start_analysis(self):
        self.require_current_library_materials()
        if (self.worker and self.worker.isRunning()) or (self.sound_worker and self.sound_worker.isRunning()) or (self.finder and self.finder.isRunning()): return
        c = self.config()
        if not c['output_dir']: raise AnalysisError('请选择输出目录。')
        if not c['show_id'] or any(char in c['show_id'] for char in '/\\\0'):raise AnalysisError('请通过统一素材入口选择拍摄场次。')
        if set(c['label_rois']) != set(self.active_label_cameras()):
            raise AnalysisError('请设置'+ '/'.join(self.active_label_cameras()) +'全部标号区域；位置相同可共用一个框。')
        out = Path(c['output_dir'])/datetime.now().strftime('run_%Y%m%d_%H%M%S_%f')
        self.analysis_config,self.analysis_output = c,out; self.results.setRowCount(0); self.export_button.setEnabled(False)
        for button in self.project_buttons: button.setEnabled(False)
        self.results.setEnabled(False); self.review_frame.setEnabled(False)
        self.run_button.setEnabled(False); self.cancel_button.setEnabled(True)
        for i in range(self.tabs.count()):self.tabs.setTabEnabled(i,self.tabs.widget(i) is self.results_page)
        self.progress.setValue(0)
        self.library_panel.set_external_busy(True)
        self.worker = CameraAnalysisWorker(c,out); self.worker.progress.connect(self.update_progress); self.worker.done.connect(self.analysis_done); self.worker.failed.connect(self.analysis_failed); self.worker.start()

    def analysis_done(self,segments):
        self.finish_worker(); self.populate_results(segments); self.export_button.setEnabled(True)
        u = sum(s.end-s.start for s in segments if s.camera=='?')
        self.status.setText(f'识别完成：{len(segments)}个镜头；待确认{sum(s.camera=="?" for s in segments)}个镜头（{u}帧）。')
        recovered=sum(s.reason=='label_read_consensus' for s in segments)
        if recovered:self.status.setText(self.status.text()+f' 自动补采样确认{recovered}个镜头。')
        if self.library_context.get('session_id'):
            identity=self.library_context['session_id']
            snapshot=copy.deepcopy(self.segments())
            def save_when_finished():
                if self.library_panel._closed or self.library_context.get('session_id')!=identity or self.segments()!=snapshot:return
                if self.worker and self.worker.isRunning():
                    QTimer.singleShot(10,save_when_finished);return
                self.save_recording_results()
            from project_ui import _after_idle
            QTimer.singleShot(0,lambda:_after_idle(self.library_panel,save_when_finished))

    def populate_results(self,segments):
        blocked=self.results.blockSignals(True);self.results.setUpdatesEnabled(False)
        try:
            super().populate_results(segments)
            for row,segment in enumerate(segments):
                for column in (0,1,3):
                    self.results.item(row,column).setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.results.item(row,2).setData(Qt.ItemDataRole.UserRole,segment.camera)
                item=self.results.item(row,3);item.setData(Qt.ItemDataRole.UserRole,segment.reason)
                item.setText(REASONS.get(segment.reason,segment.reason))
        finally:
            self.results.blockSignals(blocked);self.results.setUpdatesEnabled(True)

    def segments(self):
        result = []
        for r in range(self.results.rowCount()):
            item = self.results.item(r,3); original = item.data(Qt.ItemDataRole.UserRole)
            reason = original or 'unreadable_label'
            if self.results.item(r,2).text().strip().upper() != self.results.item(r,2).data(Qt.ItemDataRole.UserRole): reason = 'identity_unconfirmed'
            result.append(Segment(int(self.results.item(r,0).text()),int(self.results.item(r,1).text()),self.results.item(r,2).text().strip().upper(),reason or 'manual'))
        return result

    def camera_edited(self,item):
        if item.column()!=2: return
        status = self.results.item(item.row(),3)
        if status is None: return
        reason = status.data(Qt.ItemDataRole.UserRole)
        if reason is None: return
        pending = item.text().strip().upper()!=item.data(Qt.ItemDataRole.UserRole)
        status.setText(REASONS['identity_unconfirmed'] if pending else REASONS.get(reason,reason))

    def confirm_segment(self):
        r = self.results.currentRow()
        if r<0: raise AnalysisError('请选择一个已核查区间。')
        if self.results.item(r,2).text().strip().upper() not in self.camera_ids: raise AnalysisError('请先填写本场已设置的机位：'+ ' / '.join(self.camera_ids))
        item = self.results.item(r,3); item.setData(Qt.ItemDataRole.UserRole,'manual'); item.setText(REASONS['manual'])
        self.results.item(r,2).setData(Qt.ItemDataRole.UserRole,self.results.item(r,2).text().strip().upper())
        self.status.setText('机位已确认；点击“保存机位结果”更新本场准备状态。')

    def show_review(self):
        self.preview_cut_ready(self._show_review_ready)

    def _show_review_ready(self,cut):
        frame_number=self.review_frame.value();block,index=self.preview_source(cut,frame_number)
        rois=[list(value) for value in self.canvas.rois.values()]
        identity=self.library_context.get('session_id');epoch=self._camera_epoch
        self.status.setText(f'正在读取核对帧{frame_number} / {Path(block["path"]).name}…')
        def operation(cancelled):
            frame=read_frame(block['path'],index)
            if cancelled():return None
            crops=[]
            for x0,y0,x1,y1 in set(roi_pixels(r,frame.shape[1],frame.shape[0]) for r in rois):
                crop=frame[y0:y1,x0:x1]
                picture=Image.fromarray(crop[...,::-1])
                picture.thumbnail((480,160),Image.Resampling.BILINEAR)
                crops.append(picture)
            label=None
            if crops:
                picture=Image.new('RGB',(480,sum(c.height for c in crops)),(20,20,20));top=0
                for crop in crops:picture.paste(crop,((480-crop.width)//2,top));top+=crop.height
                label=np.asarray(picture)[...,::-1].copy()
            return camera_preview_pixels(frame),label
        def ready(value,error):
            if self.library_context.get('session_id')!=identity or self.review_frame.value()!=frame_number or epoch!=self._camera_epoch:return
            if error:self.status.setText('核对画面未能读取：'+error);return
            if value is None:return
            display,label=value;self.pgm_picture.setPixmap(pixmap(display))
            if label is not None:self.label_picture.setPixmap(pixmap(label))
            else:self.label_picture.clear();self.label_picture.setText('尚未框选机位标号')
            self.status.setText(f'核对时间线第{frame_number}帧 / {Path(block["path"]).name} 第{index}帧。')
        self.review_task_loader.request(operation,ready)

    def export_xml(self):
        self.require_current_library_materials()
        if self.library_context.get('session_id'):
            return self.save_recording_results()
        if (self.worker and self.worker.isRunning()) or (self.sound_worker and self.sound_worker.isRunning()) or (self.finder and self.finder.isRunning()):raise AnalysisError('请等待当前任务结束。')
        if self.analysis_config is None: raise AnalysisError('请先完成识别；设置修改后请重新分析。')
        c = self.config()
        for key in ('mode','cut_xml','cut_xml_digest','pgm','pgm_overrides','fps','label_rois','labels','infer_a_when_blank','counter_suffix','ocr_confidence','label_match_mode'):
            default={'pgm_overrides':{},'label_match_mode':'legacy','infer_a_when_blank':False,'counter_suffix':False}.get(key)
            if c[key]!=self.analysis_config.get(key,default): raise AnalysisError(f'修改了{key}，请重新分析。')
        if any(s.reason=='identity_unconfirmed' for s in self.segments()): raise AnalysisError('修改机位后请点击确认机位，再导出。')
        from cut_xml import timeline_info
        if timeline_info(c,False)['xml_digest'] != c['cut_xml_digest']: raise AnalysisError('切点XML已改变，请重新导入并分析。')
        segments = self.segments()
        cut = timeline_info(c,False)
        validate_segments(segments,cut['duration'])
        if [(s.start,s.end) for s in segments] != [(s['start'],s['end']) for s in cut['shots']]: raise AnalysisError('分析结果切点与XML不一致，请重新分析。')
        destination = QFileDialog.getExistingDirectory(self,'选择代理XML导出位置',c['output_dir'] or str(Path.home()/'Desktop'))
        if not destination:return
        c['output_dir'] = destination
        out = Path(destination)/datetime.now().strftime('PGM_export_%Y%m%d_%H%M%S_%f')
        self.output.setText(destination)
        for button in self.project_buttons:button.setEnabled(False)
        self.results.setEnabled(False);self.review_frame.setEnabled(False);self.run_button.setEnabled(False);self.export_button.setEnabled(False);self.cancel_button.setEnabled(True)
        for i in range(self.tabs.count()):self.tabs.setTabEnabled(i,self.tabs.widget(i) is self.results_page)
        self.progress.setValue(0);self.status.setText('正在匹配声音并生成XML…')
        self.library_panel.set_external_busy(True)
        library_client=getattr(self.library_panel,'client',None) if c.get('library_context') else None
        self.worker=ExportWorker(c,segments,out,self,library_client=library_client);self.worker.progress.connect(self.update_progress);self.worker.done.connect(self.export_done);self.worker.failed.connect(self.export_failed);self.worker.start()

    def export_done(self,result):
        events,xml_path,c=result;out=xml_path.parent
        self.finish_worker();self.export_button.setEnabled(True);self.progress.setValue(100)
        from export_diagnostics import intentional_empty_camera_gap
        empty_gaps=[e for e in events if intentional_empty_camera_gap(e,c)]
        gaps=[e for e in events if not e['path'] and not intentional_empty_camera_gap(e,c)]
        fallbacks=[e for e in events if e.get('reason')=='camera_unavailable_fallback']
        pgm_references=[e for e in events if e.get('pgm_video_fallback')]
        gap_note=f'\n{len(empty_gaps)}段没有任何机位覆盖，已保留原时长、留空并标记。' if empty_gaps else ''
        if pgm_references:gap_note+=f'\n{len(pgm_references)}段没有任何机位代理覆盖，已用PGM_REF保留PGM画面；这些区间不输出声音。'
        if gaps:gap_note+=f'\n{len(gaps)}段机位或代理对应关系待核对，已标记。'
        if fallbacks:gap_note+=f'\n{len(fallbacks)}段PGM所选机位未录制，已优先选用A或其他可用机位并标记。'
        report_path=out/'sound_sync_report.json';sound_gaps=[]
        if report_path.is_file():
            try:sound_gaps=json.loads(report_path.read_text(encoding='utf-8')).get('gaps',[])
            except (OSError,ValueError):pass
        sound_gap_note=f'\n{len(sound_gaps)}处录音未覆盖，声音已留空并标记；请在达芬奇中核查。' if sound_gaps else ''
        from resolve_import_helper import companion_path
        import_note=('分离声道：请在达芬奇“工作区 → 脚本 → 三木_导入多机位XML”选择此XML，自动链接画面和左右WAV。\nXML和同名.resolve.lua须放在同一文件夹。'
            if companion_path(xml_path).is_file() else '原始WAV已按镜头分段并链接视频；切换机位不切换声音。\n导入新时间线后，请开启达芬奇“链接所选”。')
        QMessageBox.information(self,'达芬奇时间线已导出',f'主文件：{xml_path.name}\n{import_note}\n输出：{out}{gap_note}{sound_gap_note}')
        self.status.setText(f'已导出达芬奇XML：{out}'+(f'；{len(sound_gaps)}处声音缺口已标记。' if sound_gaps else ''))
        if c.get('library_context') and self.library_panel.client:
            self.register_library_export(xml_path,c)

    def finish_worker(self):
        super().finish_worker()
        if hasattr(self,'library_panel'):self.library_panel.set_external_busy(False)

    def register_library_export(self,xml_path,c):
        panel=self.library_panel;client=panel.client;context=c['library_context']
        from library_bridge import canonical_path
        manifest=dict(mapping_path=canonical_path(str(xml_path.parent/'proxy_original_map.json')),
                      library_manifest_path=canonical_path(str(xml_path.parent/'library_manifest.json')),
                      fps=c['fps'],timeline_start_tc=c['timeline_start_tc'])
        def operation(progress,cancelled):
            from library_bridge import canonicalize_payload
            from library_store import LibraryError
            artifact=client.call('register_artifact',session_id=context['session_id'],kind='multicam_xml',
                                 path=canonical_path(str(xml_path)),manifest=manifest)
            snapshot=canonicalize_payload(json.loads((xml_path.parent/'reviewed_analysis.json').read_text(encoding='utf-8')))
            result=dict(artifact=artifact)
            try:
                result['session']=client.call('save_session',production_id=context['production_id'],
                    name=context['session_name'],config=snapshot['config'],segments=snapshot['segments'],
                    session_id=context['session_id'],expected_revision=context['revision'],
                    export_dir=canonical_path(str(xml_path.parent)))
            except LibraryError as exc:
                if exc.code!='conflict':raise
                result['conflict']=str(exc)
            return result
        def completed(result):
            if result.get('session'):
                self.library_session_saved(result['session'])
                if panel.selected_session() and panel.selected_session()['id']==context['session_id']:
                    panel._session=result['session']
                panel.status.setText('多机位XML、原片映射及当前场次已保存。')
            else:
                panel.status.setText('XML已登记；共享场次已有他人修改，当前项目未覆盖。')
                QMessageBox.warning(self,'共享场次版本冲突','XML文件已完整导出并登记。共享场次已有他人修改，未覆盖其内容；请刷新后核对，或另存当前项目。')
        panel._run('登记时间线输出…',operation,completed)

    def export_failed(self,message):
        self.finish_worker();self.export_button.setEnabled(self.analysis_config is not None);self.status.setText('导出未完成：'+message)
        if '取消' not in message:QMessageBox.warning(self,'导出未完成',message)


def main():
    app = QApplication(sys.argv); app.setApplicationName('三木的PGM回套软件'); window = Window(prompt_project=True); window.show(); sys.exit(app.exec())


if __name__ == '__main__': main()
