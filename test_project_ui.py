"""Local project entry and asynchronous file loading, without network services."""
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch
from PyQt6.QtCore import QEventLoop,QTimer
from PyQt6.QtWidgets import QApplication,QDialog,QPushButton,QLabel
from local_project import LocalProjectClient
from project_state import write_project_state
from project_ui import ProjectEntryDialog,start_project,_connect_library
from library_ui import LibraryPanel


class ProjectEntryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.env=patch.dict(os.environ,PGM_LIBRARY_HOME=str(self.root/'private'));self.env.start()
        self.panel=LibraryPanel(lambda:{'config':{},'segments':[]},lambda v:None,auto_restore=False)
        self.panel._run=lambda title,op,success:success(op(lambda *_:None,lambda:False))
    def tearDown(self):self.panel.close();self.env.stop();self.tmp.cleanup()
    def result(self,selection,accepted=True):
        dialog=Mock();dialog.exec.return_value=QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected
        dialog.selection.return_value=selection
        return patch('project_ui.ProjectEntryDialog',return_value=dialog)
    def test_dialog_validates_name_and_file(self):
        d=ProjectEntryDialog([dict(id='p',name='示例剧目')]);d.mode.setCurrentIndex(0)
        d.accept();self.assertIn('名称',d.validation.text())
        d.name.setText('示例剧目');d.accept();self.assertEqual(d.result(),QDialog.DialogCode.Accepted)
        self.assertEqual(d.selection(),{'mode':'new','name':'示例剧目'})
        d.deleteLater()
    def test_new_explicit_project_has_no_implicit_default_or_remote_configuration(self):
        previous=self.panel.client;opened=[]
        with patch('socket.socket',side_effect=AssertionError('no network')),self.result({'mode':'new','name':'新戏'}):
            start_project(self.panel,on_open=opened.append)
        self.assertEqual([p['name'] for p in self.panel.client.call('list_productions')],['新戏'])
        self.assertEqual(self.panel.production_id(),opened[0]['id'])
        self.assertIsNone(previous.store._keeper)
        self.assertFalse(list(self.root.rglob('*.sqlite3')))
    def test_open_file_on_empty_workstation_retains_all_records_and_moved_path(self):
        c=LocalProjectClient();p=c.call('create_production',name='导演剪辑')
        session=c.call('save_session',production_id=p['id'],name='day01_通条',config={'pgm':'/Volumes/NEXIS/PGM.mov'},segments=[])
        original=self.root/'old.pgmproj';moved=self.root/'new.pgmproj'
        write_project_state(p,session['id'],save_path=original,catalogue=c.export_project(p['id']));original.rename(moved);c.close()
        opened=[]
        with self.result({'mode':'file','path':str(moved)}):start_project(self.panel,on_open=opened.append)
        self.assertEqual(self.panel._sessions[0]['id'],session['id'])
        self.assertEqual(self.panel._sessions[0]['config']['pgm'],'/Volumes/NEXIS/PGM.mov')
        self.assertEqual(opened[0]['project_state']['project_path'],str(moved))
    def test_cancel_keeps_current_project_unchanged(self):
        p=self.panel.client.call('create_production',name='继续');self.panel.production.addItem(p['name'],p['id'])
        previous=self.panel.client;parent=Mock()
        with self.result({},False):start_project(self.panel,parent=parent)
        parent.close.assert_not_called();self.assertIs(self.panel.client,previous)
        self.assertEqual(self.panel.production_id(),p['id'])
    def test_initial_cancel_closes_parent(self):
        parent=Mock()
        with self.result({},False):start_project(self.panel,parent=parent)
        parent.close.assert_called_once()
    def test_no_visible_service_token_actor_or_mapping_controls(self):
        labels=[w.text() for w in self.panel.advanced.findChildren(QLabel)]
        buttons=[w.text() for w in self.panel.advanced.findChildren(QPushButton)]
        for value in labels+buttons:
            self.assertFalse(any(t in value for t in ('令牌','操作人','连接共享','路径映射','备份数据库')),value)
    def test_async_open_data_runs_off_gui_callback_returns_to_gui(self):
        self.panel._run=LibraryPanel._run.__get__(self.panel,LibraryPanel)
        gui=threading.get_ident();threads=[];opened=[];loop=QEventLoop()
        original=_connect_library
        def connect(*a):threads.append(threading.get_ident());return original(*a)
        def callback(p):opened.append((p,threading.get_ident()));QTimer.singleShot(30,loop.quit)
        with patch('project_ui._connect_library',side_effect=connect),self.result({'mode':'new','name':'异步项目'}):
            start_project(self.panel,on_open=callback);QTimer.singleShot(3000,loop.quit);loop.exec()
        self.assertEqual(len(opened),1);self.assertEqual(opened[0][1],gui)
        self.assertTrue(all(t!=gui for t in threads));self.assertFalse(self.panel.is_busy())

    def test_deleted_file_same_name_can_create_save_and_reopen_fresh_project(self):
        from project_state import read_project_file
        from project_ui import load_project_selection
        old=LocalProjectClient();p=old.call('create_production',name='示例剧目')
        old.call('save_session',production_id=p['id'],name='day01_通条',config={'pgm':'/old/PGM.mov'})
        old.call('put_script',production_id=p['id'],title='旧台本',lines=[{'text':'旧台词'}])
        path=self.root/'示例剧目.pgmproj'
        write_project_state(p,save_path=path,catalogue=old.export_project(p['id']));old.close()
        path.unlink()
        client,rows,_=_connect_library(self.panel.client,'',lambda *_:None,lambda:False)
        self.assertTrue(any(row['id']==p['id'] for row in rows))
        d=ProjectEntryDialog(rows);d.mode.setCurrentIndex(0);d.name.setText(p['name']);d.accept()
        self.assertEqual(d.result(),QDialog.DialogCode.Accepted)
        opened=[]
        with self.result(d.selection()):start_project(self.panel,on_open=opened.append)
        d.deleteLater()
        new=opened[0];self.assertNotEqual(new['id'],p['id'])
        self.assertEqual(self.panel._sessions,[]);self.assertEqual(self.panel._scripts,[])
        self.assertEqual(self.panel.client.call('list_candidates',production_id=new['id']),[])
        write_project_state(new,save_path=path,catalogue=self.panel.client.export_project(new['id']))
        restored,production,state=load_project_selection({'mode':'file','path':str(path)},self.panel.client,rows)
        try:
            self.assertEqual(production['id'],new['id'])
            self.assertEqual(read_project_file(path)['name'],p['name'])
            self.assertEqual(restored.call('list_sessions',production_id=new['id']),[])
        finally:restored.close()

    def test_same_name_in_another_directory_preserves_original_file_and_records(self):
        import hashlib
        from project_ui import load_project_selection
        old=LocalProjectClient();self.addCleanup(old.close)
        p=old.call('create_production',name='示例剧目')
        old.call('save_session',production_id=p['id'],name='旧场次',config={'keep':True})
        first=self.root/'第一版/示例剧目.pgmproj';second=self.root/'第二版/示例剧目.pgmproj'
        write_project_state(p,save_path=first,catalogue=old.export_project(p['id']))
        before=hashlib.sha256(first.read_bytes()).hexdigest()
        _,rows,_=_connect_library(self.panel.client,'',lambda *_:None,lambda:False)
        d=ProjectEntryDialog(rows);d.mode.setCurrentIndex(0);d.name.setText('  示例剧目  ');d.accept()
        self.assertEqual(d.result(),QDialog.DialogCode.Accepted)
        new,prod,_=load_project_selection(d.selection(),self.panel.client,rows);d.deleteLater()
        try:
            write_project_state(prod,save_path=second,catalogue=new.export_project(prod['id']))
            self.assertNotEqual(prod['id'],p['id'])
            self.assertEqual(new.call('list_sessions',production_id=prod['id']),[])
            self.assertEqual(hashlib.sha256(first.read_bytes()).hexdigest(),before)
            self.assertEqual(len(old.call('list_sessions',production_id=p['id'])),1)
        finally:new.close()

    def test_missing_nexis_project_does_not_reserve_name_or_erase_its_history(self):
        from library_bridge import read_settings
        import uuid
        p={'id':str(uuid.uuid4()),'name':'未挂载工程'}
        path=Path('/Volumes/PGM_QA_UNMOUNTED_WORKSPACE/未挂载工程.pgmproj')
        # Simulate an unavailable workspace by remembering a valid file path;
        # no filesystem write is made to that workspace.
        from local_project import remember_project
        remember_project(p,path)
        before=read_settings()
        _,rows,_=_connect_library(self.panel.client,'',lambda *_:None,lambda:False)
        d=ProjectEntryDialog(rows);d.mode.setCurrentIndex(0);d.name.setText(p['name']);d.accept()
        self.assertEqual(d.result(),QDialog.DialogCode.Accepted);d.deleteLater()
        self.assertEqual(read_settings(),before)

    def test_current_unsaved_project_does_not_reserve_its_display_name(self):
        from project_ui import load_project_selection
        p=self.panel.client.call('create_production',name='临时工程')
        self.panel.client.call('save_session',production_id=p['id'],name='未保存场次',config={})
        _,rows,_=_connect_library(self.panel.client,'',lambda *_:None,lambda:False)
        d=ProjectEntryDialog(rows);d.mode.setCurrentIndex(0);d.name.setText(p['name']);d.accept()
        self.assertEqual(d.result(),QDialog.DialogCode.Accepted)
        new,prod,state=load_project_selection(d.selection(),self.panel.client,rows);d.deleteLater()
        try:
            self.assertNotEqual(prod['id'],p['id']);self.assertIsNone(state)
            self.assertEqual(new.call('list_sessions',production_id=prod['id']),[])
            self.assertEqual(len(self.panel.client.call('list_sessions',production_id=p['id'])),1)
        finally:new.close()

    def test_recreated_path_lists_new_project_once_without_erasing_history(self):
        from local_project import recent_projects,remember_project
        from library_bridge import read_settings,data_dir
        old=LocalProjectClient();new=LocalProjectClient()
        self.addCleanup(old.close);self.addCleanup(new.close)
        p=old.call('create_production',name='同名工程')
        replacement=new.call('create_production',name=p['name'])
        path=self.root/'同名工程.pgmproj'
        write_project_state(p,save_path=path,catalogue=old.export_project(p['id']))
        path.unlink()
        # A historical path containing '..' still names the same file.
        remember_project(p,self.root/'folder/../同名工程.pgmproj')
        write_project_state(replacement,save_path=path,catalogue=new.export_project(replacement['id']))
        settings=read_settings()
        checkpoints={str(f):f.read_bytes() for f in (data_dir()/'projects').glob('*.json')}
        rows=recent_projects()
        self.assertEqual([row['id'] for row in rows],[replacement['id']])
        self.assertEqual(read_settings(),settings)
        self.assertEqual({str(f):f.read_bytes() for f in (data_dir()/'projects').glob('*.json')},checkpoints)

    def test_stale_recent_entry_opens_actual_file_identity(self):
        from project_ui import load_project_selection
        old=LocalProjectClient();new=LocalProjectClient()
        self.addCleanup(old.close);self.addCleanup(new.close)
        p=old.call('create_production',name='同名工程')
        replacement=new.call('create_production',name=p['name'])
        path=self.root/'同名工程.pgmproj'
        write_project_state(p,save_path=path,catalogue=old.export_project(p['id']))
        path.unlink()
        expected=new.call('save_session',production_id=replacement['id'],name='新场次',config={})
        write_project_state(replacement,save_path=path,catalogue=new.export_project(replacement['id']))
        client,production,state=load_project_selection(
            {'mode':'existing','production_id':p['id']},old,[dict(p,project_path=str(path))])
        try:
            self.assertEqual(production['id'],replacement['id'])
            self.assertEqual(state['production_id'],replacement['id'])
            self.assertEqual([s['id'] for s in client.call('list_sessions',production_id=production['id'])],[expected['id']])
        finally:client.close()

if __name__=='__main__':unittest.main()
