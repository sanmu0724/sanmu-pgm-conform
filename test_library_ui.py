"""Catalogue UI tests use the real transactional store and dialogue engine."""
import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from PyQt6.QtCore import QEventLoop, QTimer, Qt, QPoint
from PyQt6.QtWidgets import QApplication, QWidget, QDialog, QMessageBox

from library_ui import LibraryPanel, LibraryTask, MaterialImportDialog, media_paths, format_time
from library_store import Store, LibraryError


class StoreClient:
    def __init__(self, store, actor='场记甲'):
        self.store, self.actor, self.calls = store, actor, []
        self.url, self.token = 'http://127.0.0.1:8765', 'test-library-token-1234567890'

    def call(self, method, **params):
        self.calls.append((method, copy.deepcopy(params)))
        return self.store.call(method, params, self.actor)


class LibraryPanelTests(unittest.TestCase):
    def test_confirmation_updates_badges_and_queue_without_rebuilding_media_or_script(self):
        self.seed_matches()
        selected_id = self.panel.script_timeline.current_line_id()
        item = self.panel.script_timeline._items[selected_id]
        row = copy.deepcopy(self.panel._candidate())
        before_pending = self.panel.pending_list.count()
        with patch.object(self.panel, '_set_data', side_effect=AssertionError('full UI rebuild')), \
             patch.object(self.panel.script_timeline, 'set_nodes', side_effect=AssertionError('script reset')), \
             patch.object(self.panel, '_populate', wraps=self.panel._populate) as populate:
            self.panel.update_candidate('confirmed')
        self.assertIs(self.panel.script_timeline._items[selected_id], item)
        self.assertEqual(self.panel.script_timeline.current_line_id(), selected_id)
        self.assertEqual(self.panel._candidate_by_id[row['id']]['status'], 'confirmed')
        self.assertEqual(before_pending, 0)  # Whole PGM already exceeds the automatic threshold.
        self.assertEqual(self.panel.pending_list.count(), 0)
        self.assertEqual(self.panel._timeline_by_id[selected_id]['confirmed_count'], 1)
        self.assertFalse(any(call.args[0] is self.panel.sessions or call.args[0] is self.panel.script
                             or call.args[0] is self.panel.transcripts for call in populate.call_args_list))

    def test_concurrent_catalogue_edit_uses_full_snapshot_and_keeps_cas_decision(self):
        first, _ = self.seed_matches()
        current = self.client.call('get_session', session_id=first['id'])
        self.client.call('save_session', production_id=self.production['id'], session_id=first['id'],
                         expected_revision=current['revision'], name='其他人更新的场次', config=current['config'])
        with patch.object(self.panel, '_set_data', wraps=self.panel._set_data) as refresh:
            self.panel.update_candidate('confirmed')
        refresh.assert_called_once()
        self.assertTrue(any(row['name'] == '其他人更新的场次' for row in self.panel._sessions))

    def test_confirmation_in_review_only_view_selects_next_take_with_its_own_note(self):
        self.seed_matches()
        self.client.call('replace_candidates', production_id=self.production['id'], candidates=[
            dict(script_line_id=row['script_line_id'], transcript_id=row['transcript_id'],
                 score=.5, reason='需要人工判断的低匹配度') for row in self.panel._candidates])
        self.refresh()
        self.panel.take_mode.setCurrentIndex(self.panel.take_mode.findData('review'))
        original = self.panel._candidate()['id']
        self.panel.candidate_note.setText('已确认素材的备注')
        self.panel.update_candidate('confirmed')
        self.assertEqual(self.panel.match_list.count(), 1)
        self.assertNotEqual(self.panel._candidate()['id'], original)
        self.assertEqual(self.panel._candidate()['status'], 'candidate')
        self.assertEqual(self.panel.candidate_note.text(), self.panel._candidate().get('note', ''))

    def test_pgm_interval_view_lists_whole_files_and_incremental_overlay(self):
        from test_pgm_intervals import catalogue, add_take
        from pgm_intervals import build_pgm_intervals
        from library_timeline import build_timeline_data
        data = catalogue()
        add_take(data, 'day01', 'large', (10,14))
        data['timeline_nodes'] = build_timeline_data(data, whole_pgm=True)
        data['pgm_interval_nodes'] = build_pgm_intervals(data)
        with patch.object(self.panel, '_load_inline'):
            self.panel._set_data(data)
            self.panel.story_axis.setCurrentIndex(1)
            self.assertEqual(len(self.panel.pgm_timeline._nodes),1)
            self.assertEqual(self.panel.match_list.count(),1)
            self.assertEqual(self.panel._candidate()['start_seconds'],0)
            self.assertEqual(self.panel._candidate()['end_seconds'],20)
            identity = self.panel.pgm_timeline.current_line_id()
            add_take(data, 'day02', 'small', (12,13), speech_start=5)
            data['timeline_nodes'] = build_timeline_data(data, whole_pgm=True)
            data['pgm_interval_nodes'] = build_pgm_intervals(data)
            self.panel._set_data(data)
            self.assertEqual(self.panel.pgm_timeline.current_line_id(),identity)
            self.assertEqual(self.panel.match_list.count(),2)
            self.assertIn('2 条完整PGM',self.panel.script_count.text())
            self.panel.story_axis.setCurrentIndex(0)
            self.assertEqual(self.panel._script_filter,self.panel.script_timeline.current_line_id())

    def test_silent_pgm_interval_opens_pending_and_manual_full_file_anchor(self):
        from test_library_pending import session_config
        from library_pending import is_manual_visual
        path='/NAS/Drama/silent.mov'
        config=session_config(cameras=('A',))
        config.update(pgm=path,library_record_type='recording')
        config['pgm_sources'][0]['path']=path
        session=self.add_session('day01_分镜头',config)
        self.client.call('put_script',production_id=self.production['id'],title='台本',
                         lines=[dict(text='你好 Hello',order=0)])
        with patch.object(self.panel,'_load_inline'):
            self.refresh(session)
            self.panel.story_axis.setCurrentIndex(1)
            self.assertEqual(self.panel.main_pages.currentIndex(),1)
            self.assertIsNone(self.panel._script_filter)
            line=self.panel._scripts[0]
            with patch.object(self.panel,'_choose_record',return_value=line):
                self.panel.associate_pending()
            self.assertEqual(self.panel.pending_list.count(),0)
            self.assertEqual(self.panel.match_list.count(),1)
            clip=self.panel._visible_candidates[0]
            self.assertEqual((clip['start_seconds'],clip['end_seconds'],clip['dialogue_start_seconds']),(0,40,0))
            self.assertEqual(clip['script_line_id'],line['id'])
            self.assertTrue(clip['manual_anchor'])
            self.assertTrue(is_manual_visual(self.panel._transcripts[0]))

    def test_pgm_status_dialog_filters_file_names_and_never_counts_sentences(self):
        from test_pgm_intervals import catalogue,add_take
        from PyQt6.QtWidgets import QTableWidget,QComboBox,QLineEdit
        data=catalogue()
        add_take(data,'day01','matched',(1,2,3))
        add_take(data,'day01','review',(4,5))
        add_take(data,'day01','silent',())
        for row in data['candidates']:
            if 'review' in row['id']:
                row.update(status='candidate', score=.5, tags=['auto_dialogue'])
        with patch.object(self.panel,'_load_inline'):
            self.panel._set_data(data)
        def inspect(dialog):
            table=dialog.findChild(QTableWidget)
            choice=dialog.findChild(QComboBox)
            search=dialog.findChild(QLineEdit)
            self.assertEqual(table.rowCount(),3)
            self.assertIn('轨道待核实 2 · 轨道已核实 0 · 未匹配台本 1 · 轨道移出 0',self.panel.script_count.text())
            choice.setCurrentIndex(choice.findData('unreviewed'))
            self.assertEqual(table.rowCount(),2)
            self.assertEqual({table.item(n,2).text() for n in range(table.rowCount())},{'matched.mov','review.mov'})
            choice.setCurrentIndex(0)
            search.setText('silent')
            self.assertEqual(table.rowCount(),1)
            self.assertEqual(table.item(0,0).text(),'未匹配台本')
            self.assertEqual(table.horizontalHeaderItem(3).text(),'转录段数')
            self.assertEqual(table.item(0,3).text(),'0')
            self.assertEqual(table.item(0,4).text(),'0%')
            self.assertEqual(table.item(0,5).text(),'待定位')
            self.assertEqual(table.item(0,6).text(),'/nas/day01/silent.mov')
            return QDialog.DialogCode.Rejected
        with patch('library_ui.QDialog.exec',inspect):
            self.panel.show_pgm_match_status()

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'library.sqlite3')
        self.client = StoreClient(self.store)
        self.production = self.client.call('create_production', name='示例剧目')
        self.config = {'show_id': '第三场', 'fps': 25, 'pgm': '/Volumes/Edit/第三场.mov',
                       'sources': [{'camera': 'A', 'path': '/Volumes/Edit/A_proxy.mov'}]}
        self.snapshot = {'config': self.config, 'segments': [{'start': 0, 'end': 250, 'camera': 'A'}]}
        self.loaded = []
        self.mapping = patch('library_bridge.read_settings', return_value={
            'path_mappings': [{'shared': '/NAS/Drama', 'local': '/Volumes/Edit'}]})
        self.mapping.start()
        self.settings_writer = patch('library_bridge.write_settings')
        self.settings_writer.start()
        self.panel = LibraryPanel(lambda: copy.deepcopy(self.snapshot), self.loaded.append)
        self.panel.client = self.client
        self.panel.production.blockSignals(True)
        self.panel.production.addItem(self.production['name'], self.production['id'])
        self.panel.production.blockSignals(False)
        # Synchronous executor exposes exceptions directly while exercising every
        # RPC and callback against the production store.
        self.panel._run = lambda title, operation, success: success(operation(lambda *args: None, lambda: False))

    def tearDown(self):
        self.panel.close()
        self.store.close()
        self.mapping.stop()
        self.settings_writer.stop()
        self.tmp.cleanup()

    def pump_until(self,condition,timeout=2):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            QApplication.processEvents()
            if condition():return
            time.sleep(.003)
        self.fail('background browser operation did not finish')

    def add_session(self, name, config=None):
        return self.client.call('save_session', production_id=self.production['id'], name=name,
                                config=config or {}, segments=[])

    def refresh(self, selected=None):
        if selected:
            self.panel._session = selected
            self.snapshot['config']['library_context'] = {'session_id': selected['id'],
                'production_id': self.production['id'], 'revision': selected['revision']}
        self.panel._set_data(self.panel._read_all(self.client, self.production['id']))

    def seed_matches(self):
        first = self.add_session('第一场', {'library_media': [{'kind': 'pgm', 'path': '/NAS/Drama/第一场.mov'}]})
        second = self.add_session('第三场', {'library_media': [{'kind': 'pgm', 'path': '/NAS/Drama/第三场.mov'}]})
        self.client.call('put_script', production_id=self.production['id'], title='台本', lines=[
            {'scene': '第一幕', 'speaker': 'Jack', 'text': 'Good morning，今天你好吗？', 'order': 0}])
        for session, text in ((first, 'Good morning，今天你好吗？'), (second, 'Good morning，今天你好吗？')):
            self.client.call('add_transcript', session_id=session['id'],
                media_path='/NAS/Drama/' + session['name'] + '.mov', segments=[
                    {'start_seconds': 12.345, 'end_seconds': 16.6, 'text': text}])
        self.refresh(first)
        self.panel.match()
        return first, second

    def seed_angle_match(self):
        pgm = '/NAS/Drama/angle_PGM.mov'
        config = {'fps': 25, 'pgm': pgm, 'calibration_confirmed': False,
            'pgm_sources': [{'path': pgm, 'fps': '25', 'frames': 250, 'start_tc': '12:00:10:00'}],
            'sources': [
                {'camera': 'A', 'source_id': 'a1', 'path': '/NAS/Drama/A1.mov',
                 'fps': '25', 'frames': 100, 'start_tc': '12:00:08:00'},
                {'camera': 'A', 'source_id': 'a2', 'path': '/NAS/Drama/A2.mov',
                 'fps': '25', 'frames': 100, 'start_tc': '12:00:12:00'},
                {'camera': 'B', 'source_id': 'b1', 'path': '/NAS/Drama/B1.mov',
                 'fps': '25', 'frames': 250, 'start_tc': '12:00:10:00'},
                {'camera': 'B', 'source_id': 'b2', 'path': '/NAS/Drama/B2.mov',
                 'fps': '25', 'frames': 250, 'start_tc': '12:00:10:00'}]}
        session = self.add_session('第三场', config)
        self.client.call('put_script', production_id=self.production['id'], title='台本', lines=[
            {'scene': '第一幕', 'speaker': 'Jack', 'text': 'Good morning，你好吗？', 'order': 0},
            {'scene': '第一幕', 'speaker': 'Algy', 'text': '下一句还没有素材', 'order': 1}])
        transcript = self.client.call('add_transcript', session_id=session['id'], media_path=pgm,
            segments=[{'start_seconds': 1, 'end_seconds': 3, 'text': 'Good morning，你好吗？'}], kind='pgm')['segments'][0]
        script = self.client.call('list_script', production_id=self.production['id'])[0]
        self.client.call('replace_candidates', production_id=self.production['id'], candidates=[{
            'script_line_id': script['id'], 'transcript_id': transcript['id'], 'score': .95, 'reason': '台词相似'}])
        self.refresh(session)
        return session

    def test_angle_views_keep_parent_decision_and_preview_actual_proxy_ranges(self):
        with patch.object(self.panel, '_load_inline') as preview:
            self.seed_angle_match()
            parent = self.panel._candidate()
            self.assertEqual((self.panel.match_list.count(), self.panel.angle_choice.count()), (1, 5))
            self.assertEqual([self.panel.angle_choice.itemData(i)['camera'] for i in range(5)],
                             ['PGM', 'A', 'A', 'B', 'B'])
            self.assertIn('A机 1/2（未校准）', self.panel.angle_choice.itemText(1))
            self.assertIn('B机 1/2（重叠待核对）', self.panel.angle_choice.itemText(3))
            self.panel.angle_choice.setCurrentIndex(1)
            preview.assert_called_with('/NAS/Drama/A1.mov', 2., 4.)
            self.panel.angle_choice.setCurrentIndex(2)
            preview.assert_called_with('/NAS/Drama/A2.mov', 0., 4.)
            self.panel.angle_choice.setCurrentIndex(3)
            preview.assert_called_with('/NAS/Drama/B1.mov', 0., 10.)
            self.assertIn('重叠代理', self.panel.angle_choice.toolTip())
            self.assertEqual(self.panel._candidate()['id'], parent['id'])
            self.assertIn('尚未合板', self.panel.match_sync_status.text())
            self.panel.update_candidate('confirmed')
        calls = [params for method, params in self.client.calls if method == 'update_candidate']
        self.assertEqual(calls[-1]['candidate_id'], parent['id'])
        self.assertEqual(len(self.client.call('list_candidates', production_id=self.production['id'])), 1)
        self.assertEqual(len(self.client.call('list_transcripts', production_id=self.production['id'])), 1)
        self.assertEqual(self.panel._candidate()['status'], 'confirmed')
        self.assertEqual(self.panel.angle_choice.currentData()['source_id'], 'b1')

    def test_proxy_view_and_empty_node_without_obsolete_load_action(self):
        self.seed_angle_match()
        self.panel.angle_choice.setCurrentIndex(2)
        self.assertFalse(hasattr(self.panel, 'locate_candidate'))
        self.assertEqual(self.loaded, [])
        self.panel.script_mode.setCurrentIndex(self.panel.script_mode.findData('multiple'))
        self.assertEqual(self.panel.script_tree.current_line_id(), self.panel._scripts[0]['id'])
        self.assertEqual(self.panel._timeline_nodes[0]['count'], 1)
        self.assertEqual(self.panel._timeline_nodes[0]['angle_count'], 4)
        self.assertNotIn('机位片段', self.panel.script_tree.currentItem().text(0))
        self.assertIn('Good morning，你好吗？', self.panel.script_tree.currentItem().text(0))
        self.assertTrue(self.panel.script_mode.isHidden())
        self.assertEqual(self.panel.story_axis.count(), 1)
        self.assertEqual(self.panel.story_axis.itemData(0), 'pgm')
        self.panel.script_timeline.setCurrentItem(
            self.panel.script_timeline._items[self.panel._scripts[1]['id']])
        self.assertEqual(self.panel.match_list.count(), 0)
        self.assertEqual(self.panel.angle_choice.count(), 0)
        self.assertFalse(self.panel.angle_choice.isEnabled())
        self.assertIsNone(getattr(self.panel, '_inline_key', None))

    def test_refresh_preserves_specific_split_proxy_without_creating_new_candidates(self):
        session = self.seed_angle_match()
        self.panel.angle_choice.setCurrentIndex(2)
        config = copy.deepcopy(session['config'])
        config['sources'].append({'camera': 'C', 'source_id': 'added-c', 'path': '/NAS/Drama/C.mov',
                                 'fps': '25', 'frames': 250, 'start_tc': '12:00:10:00'})
        session = self.client.call('save_session', production_id=self.production['id'], session_id=session['id'],
            expected_revision=session['revision'], name=session['name'], config=config)
        calls_before = len(self.client.calls)
        self.refresh(session)
        self.assertEqual(self.panel.angle_choice.count(), 6)
        self.assertEqual(self.panel.angle_choice.currentData()['source_id'], 'a2')
        self.assertEqual((self.panel.angle_choice.currentData()['start_seconds'],
                          self.panel.angle_choice.currentData()['end_seconds']), (0, 4))
        self.assertFalse(any(method in ('add_transcript', 'replace_candidates', 'update_candidate')
                             for method, _ in self.client.calls[calls_before:]))

    def test_explicit_refresh_persists_eligible_old_candidate_without_retranscription(self):
        session = self.seed_angle_match()
        path = '/NAS/Drama/angle_PGM.mov'
        config = copy.deepcopy(session['config'])
        config['library_sync'] = {path: {'status': 'matched', 'placements': [{
            'wav_path': '/NAS/Drama/main.wav', 'pgm_start_seconds': 0, 'pgm_end_seconds': 10,
            'wav_start_seconds': 0, 'wav_end_seconds': 10, 'offset_seconds': 0,
            'wav_info': {'sample_rate': 48000, 'samples': 480000}}], 'uncovered': []}}
        self.client.call('save_session', production_id=self.production['id'], session_id=session['id'],
            expected_revision=session['revision'], name=session['name'], config=config)
        self.panel._read_all(self.client, self.production['id'])
        self.assertEqual(self.client.call('list_candidates', production_id=self.production['id'])[0]['status'], 'candidate')
        with patch('dialogue_engine.transcribe_media', side_effect=AssertionError('refresh must not rerun ASR')):
            self.panel.refresh()
        stored = self.client.call('list_candidates', production_id=self.production['id'])[0]
        self.assertEqual(stored['status'], 'confirmed')
        self.assertIn('auto_dialogue', stored['tags'])
        self.assertIn('【自动通过】', self.panel.match_list.item(0).text())
        self.assertIn('轨道待核实 1 · 轨道已核实 0', self.panel.status.text())

    def seed_pending_picture(self):
        pgm = '/NAS/Drama/no_audio_PGM.mov'
        session = self.add_session('无音频第三场', {'fps': 25, 'pgm': pgm,
            'pgm_sources': [{'path': pgm, 'fps': '25', 'frames': 250, 'start_tc': '12:00:10:00'}],
            'sources': [
                {'camera': 'A', 'source_id': 'pa', 'path': '/NAS/Drama/PA.mov',
                 'fps': '25', 'frames': 300, 'start_tc': '12:00:08:00'},
                {'camera': 'B', 'source_id': 'pb', 'path': '/NAS/Drama/PB.mov',
                 'fps': '25', 'frames': 250, 'start_tc': '12:00:10:00'}]})
        self.client.call('put_script', production_id=self.production['id'], title='台本', lines=[
            {'scene': '第一幕', 'speaker': 'Jack', 'text': '台本句子', 'order': 0}])
        self.refresh(session)
        self.panel.main_pages.setCurrentIndex(1)
        self.panel.pending_list.setCurrentRow(0)
        return session

    def test_no_audio_pgm_can_preview_angles_and_classify_parent_interval_once(self):
        with patch.object(self.panel, '_load_inline') as preview:
            session = self.seed_pending_picture()
            self.assertEqual(self.panel.pending_list.count(), 1)
            self.assertIn('无匹配音频', self.panel.pending_list.item(0).text())
            self.assertEqual(self.panel.pending_angle_choice.count(), 3)
            self.panel.pending_start.setValue(1)
            self.panel.pending_end.setValue(3)
            self.panel.pending_angle_choice.setCurrentIndex(1)
            preview.assert_called_with('/NAS/Drama/PA.mov', 3., 5.)
            self.refresh(session)
            self.assertEqual((self.panel.pending_start.value(), self.panel.pending_end.value()), (1, 3))
            self.assertEqual(self.panel.pending_angle_choice.currentData()['source_id'], 'pa')
            self.assertIn('声音尚未合板', self.panel.pending_reason.text())
            self.panel.associate_pending()
        transcripts = self.client.call('list_transcripts', production_id=self.production['id'])
        self.assertEqual(len(transcripts), 1)
        transcript = transcripts[0]
        self.assertEqual((transcript['media_path'], transcript['start_seconds'], transcript['end_seconds']),
                         ('/NAS/Drama/no_audio_PGM.mov', 1, 3))
        self.assertEqual(transcript['text'], '[人工画面归类]')
        self.assertEqual(transcript['data']['source_kind'], 'manual_visual')
        self.assertEqual(len(self.panel._candidates), 1)
        self.assertEqual(self.panel._candidates[0]['status'], 'confirmed')
        self.assertEqual(len(self.panel._timeline_nodes[0]['clips'][0]['related_angles']), 2)
        self.assertEqual([(row['start_seconds'], row['end_seconds']) for row in self.panel._pending_intervals],
                         [(0, 1), (3, 10)])
        self.assertEqual(self.client.call('get_session', session_id=session['id'])['config'].get('library_sync'), None)
        self.panel.main_pages.setCurrentIndex(0)
        self.assertIn('人工画面归类', self.panel.match_list.item(0).text())
        self.assertNotIn('100%', self.panel.match_list.item(0).text())
        self.assertIn('尚未合板', self.panel.match_sync_status.text())
        self.panel.angle_choice.setCurrentIndex(1)
        self.panel.update_candidate('candidate')
        self.assertEqual(len(self.panel._pending_intervals), 1)
        self.assertEqual(len(self.panel._pending_transcripts), 0)

    def test_manual_pgm_range_rejects_empty_bounds_and_clear_catalogue_removes_views(self):
        self.seed_pending_picture()
        self.panel.pending_start.setValue(3)
        self.panel.pending_end.setValue(3)
        with self.assertRaisesRegex(ValueError, '有效起止'):
            self.panel.associate_pending()
        self.assertEqual(self.client.call('list_transcripts', production_id=self.production['id']), [])
        self.panel._clear_data()
        self.assertEqual(self.panel.pending_list.count(), 0)
        self.assertEqual(self.panel._pending_intervals, [])

    def test_confirmed_picture_classification_hides_later_duplicate_asr_until_reopened(self):
        session = self.seed_pending_picture()
        self.panel.pending_start.setValue(1)
        self.panel.pending_end.setValue(3)
        self.panel.associate_pending()
        visual_id = self.panel._candidates[0]['transcript_id']
        spoken = self.client.call('add_transcript', session_id=session['id'],
            media_path='/NAS/Drama/no_audio_PGM.mov', kind='pgm', segments=[{
                'start_seconds': 1, 'end_seconds': 3, 'text': '后来重新识别的同一段真实对白'}])
        self.client.call('replace_candidates', production_id=self.production['id'], candidates=[{
            'script_line_id': self.panel._scripts[0]['id'], 'transcript_id': spoken['transcript_ids'][0],
            'score': .5, 'reason': '低吻合需要核对'}])
        self.refresh(session)
        self.assertEqual(len(self.client.call('list_transcripts', production_id=self.production['id'])), 2)
        self.assertEqual({row['id'] for row in self.panel._transcripts}, {visual_id,*spoken['transcript_ids']})
        self.assertEqual((self.panel._timeline_nodes[0]['count'], self.panel._timeline_nodes[0]['angle_count']), (1, 2))
        self.assertEqual(self.panel._pending_transcripts, [])
        self.panel.main_pages.setCurrentIndex(0)
        self.panel.update_candidate('candidate')
        self.assertEqual(self.panel._pending_transcripts, [])
        self.assertEqual(len(self.panel.story_timeline.index.takes),1)
        self.assertEqual((self.panel._timeline_nodes[0]['count'], self.panel._timeline_nodes[0]['angle_count']), (1, 2))
        session = self.client.call('get_session',session_id=session['id'])
        config = copy.deepcopy(session['config'])
        path = '/NAS/Drama/no_audio_PGM.mov'
        config['sound_unmatched_policy'] = 'error'
        config['library_processing'] = {path: {'stage': 'sync', 'status': 'failed', 'error': '没有找到本次原始WAV'}}
        # A previous successful report must not disguise the current failure.
        config['library_sync'] = {path: {'status': 'matched', 'placements': [{
            'wav_path': '/NAS/Drama/old.wav', 'pgm_start_seconds': 0, 'pgm_end_seconds': 10,
            'wav_start_seconds': 0, 'wav_end_seconds': 10}]}}
        session = self.client.call('save_session', production_id=self.production['id'], session_id=session['id'],
            expected_revision=session['revision'], name=session['name'], config=config)
        self.refresh(session)
        self.assertEqual([row['id'] for row in self.panel._transcripts], [visual_id])  # failed sync invalidates automatic ASR
        self.assertIn('本次合板失败', self.panel.match_sync_status.text())
        self.assertIn('没有找到本次原始WAV', self.panel.match_detail.toPlainText())
        self.assertEqual(self.panel.pending_list.count(),0)  # covered file stays on track despite sound failure
        self.assertEqual(len(self.panel.story_timeline.index.takes),1)
        self.assertIn('本次合板失败', self.panel.match_sync_status.text())

    def test_draft_session_can_register_media_without_cut_xml(self):
        with patch('library_ui.QInputDialog.getText', return_value=('Day01_特写', True)):
            self.panel.new_session()
        draft = self.panel.selected_session()
        self.assertNotIn('cut_xml', draft['config'])
        self.assertEqual(draft['config']['empty_camera_policy'],'pgm')
        self.panel._save_materials([{'id': 'media-a', 'kind': 'proxy', 'camera': 'A',
            'path': '/NAS/Drama/A.mov', 'notes': '第二幕特写'}])
        saved = self.panel.selected_session()
        self.assertEqual(saved['revision'], 2)
        self.assertEqual(saved['config']['library_media'][0]['notes'], '第二幕特写')
        self.assertEqual(self.panel.materials.rowCount(), 1)
        self.panel.load_selected()
        self.assertEqual(self.loaded[-1]['id'], saved['id'])

    def test_save_uses_revision_canonical_paths_and_preserves_media(self):
        session = self.add_session('第三场', {'library_media': [{'id': 'stable', 'path': '/NAS/Drama/one.mov'}]})
        self.refresh(session)
        self.panel.notes.setPlainText('开场有改词')
        emitted = []
        self.panel.session_saved.connect(emitted.append)
        self.panel.save_current()
        saved = self.client.call('get_session', session_id=session['id'])
        self.assertEqual(saved['revision'], 2)
        self.assertEqual(saved['config']['pgm'], '/NAS/Drama/第三场.mov')
        self.assertEqual(saved['config']['library_media'][0]['id'], 'stable')
        self.assertEqual(saved['config']['library_notes'], '开场有改词')
        self.assertEqual(saved['segments'], self.snapshot['segments'])
        self.assertEqual(emitted[0]['revision'], 2)
        self.assertEqual(self.config['pgm'], '/Volumes/Edit/第三场.mov')

    def test_stale_session_save_does_not_overwrite_other_editor(self):
        session = self.add_session('第一场', {'library_notes': '原备注'})
        self.refresh(session)
        self.store.call('save_session', {'production_id': self.production['id'], 'name': '第一场',
            'config': {'library_notes': '导演已修改'}, 'session_id': session['id'],
            'expected_revision': 1}, '导演乙')
        self.panel.notes.setPlainText('过期修改')
        with self.assertRaises(LibraryError) as raised:
            self.panel.save_current()
        self.assertEqual(raised.exception.code, 'conflict')
        self.assertEqual(self.client.call('get_session', session_id=session['id'])['config']['library_notes'],
                         '导演已修改')

    def test_selecting_other_session_cannot_overwrite_it_with_current_project(self):
        first = self.add_session('第一场', {'library_notes': '第一场保留'})
        second = self.add_session('第三场', {'library_notes': '第三场保留'})
        self.refresh(first)
        self.panel._session = second
        self.panel.session_name.setText(second['name'])
        with self.assertRaisesRegex(ValueError, '当前编辑项目不属于所选场次'):
            self.panel.save_current()
        self.assertEqual(self.client.call('get_session', session_id=second['id'])['revision'], 1)
        self.panel.save_current(as_new=True, new_name='当前项目另存')
        self.assertEqual(len(self.panel._sessions), 3)

    def test_refresh_does_not_authorize_overwriting_loaded_old_revision(self):
        session = self.add_session('第一场', {'library_notes': '第一版'})
        self.refresh(session)
        self.client.call('save_session', production_id=self.production['id'], name='第一场',
            config={'library_notes': '导演更新'}, session_id=session['id'], expected_revision=1)
        self.panel._set_data(self.panel._read_all(self.client, self.production['id']))
        self.assertEqual(self.panel.selected_session()['revision'], 2)
        with self.assertRaisesRegex(ValueError, '共享场次已有新版本'):
            self.panel.save_current()
        self.assertEqual(self.client.call('get_session', session_id=session['id'])['config']['library_notes'], '导演更新')

    def test_cross_session_match_filter_and_confirm(self):
        first, second = self.seed_matches()
        self.panel.show_advanced(2)
        self.assertEqual(len(self.panel._candidates), 2)
        self.assertEqual(self.panel.transcripts.rowCount(), 2)
        self.panel.keyword.setText('Good morning')
        self.assertEqual(self.panel.candidates.rowCount(), 2)
        self.panel.session_filter.setCurrentIndex(self.panel.session_filter.findData(first['id']))
        self.assertEqual(self.panel.candidates.rowCount(), 1)
        self.panel.candidates.selectRow(0)
        self.panel.candidate_note.setText('表演节奏更好')
        self.panel.candidate_tags.setText('特写，备选')
        self.panel.update_candidate('confirmed')
        chosen = self.panel._candidate()
        self.assertEqual(chosen['status'], 'confirmed')
        self.assertEqual(chosen['tags'], ['特写', '备选'])
        self.assertEqual(self.loaded, [])
        self.panel.match()
        preserved = next(row for row in self.panel._candidates if row['session_id'] == first['id'])
        self.assertEqual(preserved['note'], '表演节奏更好')
        self.assertEqual(preserved['status'], 'confirmed')

    def test_candidate_conflict_keeps_newer_classification(self):
        self.seed_matches()
        self.panel.candidates.selectRow(0)
        stale = copy.deepcopy(self.panel._candidate())
        self.store.call('update_candidate', {'candidate_id': stale['id'], 'expected_revision': stale['revision'],
            'status': 'rejected', 'note': '导演排除', 'tags': ['重拍']}, '导演乙')
        with self.assertRaises(LibraryError) as raised:
            self.panel.update_candidate('confirmed')
        self.assertEqual(raised.exception.code, 'conflict')
        stored = self.client.call('list_candidates', production_id=self.production['id'])
        self.assertEqual(next(r for r in stored if r['id'] == stale['id'])['note'], '导演排除')

    def test_manual_association_handles_changed_dialogue_and_records_confirmed(self):
        session = self.add_session('第二场')
        self.client.call('put_script', production_id=self.production['id'], title='台本', lines=[
            {'scene': '第一幕', 'speaker': 'Jack', 'text': '台本原句', 'order': 0}])
        self.client.call('add_transcript', session_id=session['id'], media_path='/NAS/Drama/two.mov',
            segments=[{'start_seconds': 22, 'end_seconds': 25, 'text': '完全不同的临场发挥'}])
        self.refresh(session)
        self.panel._script_filter = None
        with patch.object(self.panel, '_choose_record', side_effect=[self.panel._scripts[0], self.panel._transcripts[0]]):
            self.panel.manual_match()
        candidate = self.panel._candidates[0]
        self.assertEqual(candidate['status'], 'confirmed')
        self.assertEqual(candidate['reason'], '人工关联')
        self.assertEqual(candidate['score'], 0)
        self.panel.candidates.selectRow(0)
        self.panel.update_candidate('candidate')
        self.assertEqual(self.panel._candidates[0]['status'], 'candidate')

    def test_script_and_srt_import_use_media_relative_positions(self):
        session = self.add_session('第一场', {'pgm': '/NAS/Drama/one.mov'})
        self.client.call('add_transcript', session_id=session['id'], media_path='/NAS/Drama/one.mov',
            segments=[{'start_seconds': 8, 'end_seconds': 11, 'text': '这是上一版错误转录'}], kind='pgm')
        self.refresh(session)
        script = Path(self.tmp.name) / '台本.txt'
        script.write_text('第一幕\nJack：Good morning，今天你好吗？\n', encoding='utf-8')
        with patch('library_ui.QFileDialog.getOpenFileName', return_value=(str(script), '')):
            self.panel.import_script()
        self.assertEqual(self.panel.script.rowCount(), 1)
        subtitle = Path(self.tmp.name) / 'transcript.srt'
        subtitle.write_text('1\n00:00:12,345 --> 00:00:16,600\nGood morning，今天你好吗？\n', encoding='utf-8')
        self.panel.media.setCurrentText('/Volumes/Edit/one.mov')
        with patch('library_ui.QFileDialog.getOpenFileName', return_value=(str(subtitle), '')):
            self.panel.import_transcript()
        row = self.panel._transcripts[0]
        self.assertEqual(row['media_path'], '/NAS/Drama/one.mov')
        self.assertEqual(row['start_seconds'], 12.345)
        self.assertEqual(len(self.panel._transcripts), 1)
        self.assertEqual(len(self.client.call('list_transcripts', production_id=self.production['id'])), 2)
        saved = self.client.call('get_session', session_id=session['id'])
        state = saved['config']['library_asr']['/NAS/Drama/one.mov']
        self.assertEqual(state['active_transcript_ids'], [row['id']])
        self.assertEqual(state['source_kind'], 'manual')
        self.assertTrue(state['complete'])
        self.panel.match()
        self.assertEqual(len(self.panel._candidates), 1)
        self.assertEqual(self.panel._candidates[0]['transcript_id'], row['id'])
        self.assertEqual(self.panel.language.currentData(), 'auto')

    def test_batch_import_retains_existing_ocr_segments(self):
        paths = []
        for index in (1, 2):
            path = Path(self.tmp.name) / f'场次{index}.json'
            value = copy.deepcopy(self.snapshot)
            value['config']['show_id'] = f'S0{index}'
            path.write_text(json.dumps(value), encoding='utf-8')
            paths.append(str(path))
        with patch('library_ui.QFileDialog.getOpenFileNames', return_value=(paths, '')):
            self.panel.import_projects()
        self.assertEqual(len(self.panel._sessions), 2)
        for session in self.panel._sessions:
            self.assertEqual(session['segments'], self.snapshot['segments'])
            self.assertEqual(session['config']['pgm'], '/NAS/Drama/第三场.mov')

    def test_advanced_asr_uses_unified_sync_first_workflow(self):
        workflow = Mock()
        self.panel.set_workflow(workflow)
        with patch('dialogue_engine.transcribe_media') as direct_asr:
            self.panel.transcribe()
        workflow.recognize_and_match.assert_called_once_with(self.panel, session_id=None, require_synced=True, require_prepared=True)
        direct_asr.assert_not_called()

    def test_external_ocr_disables_mutating_ui(self):
        self.panel.set_external_busy(True)
        self.assertFalse(self.panel.save_button.isEnabled())
        self.assertFalse(self.panel.load_button.isEnabled())
        self.assertFalse(hasattr(self.panel, 'locate_button'))
        self.panel.set_external_busy(False)
        self.assertTrue(self.panel.load_button.isEnabled())

    def test_main_view_is_script_centred_and_technical_controls_are_hidden(self):
        self.panel.show()
        QApplication.processEvents()
        self.assertEqual([self.panel.main_pages.tabText(index) for index in range(self.panel.main_pages.count())],
                         ['台本对照', '待人工匹配'])
        self.assertTrue(self.panel.import_pgm_button.isVisible())
        self.assertIs(self.panel.content_stack.currentWidget(), self.panel.materials_page)
        self.assertFalse(self.panel.script_tree.isVisible())
        self.assertFalse(self.panel.import_script_button.isVisible())
        self.assertFalse(self.panel.recognize_button.isVisible())
        self.assertFalse(self.panel.export_reference_button.isVisible())
        self.panel.set_workflow_stage('script')
        self.assertIs(self.panel.content_stack.currentWidget(), self.panel.catalogue_launch_page)
        self.assertTrue(self.panel.browser_open_button.isVisible())
        self.assertFalse(self.panel.story_timeline.isVisible())
        self.assertFalse(self.panel.import_script_button.isVisible())
        self.assertFalse(hasattr(self.panel, 'import_notes_button'))
        self.assertFalse(self.panel.recognize_button.isVisible())
        self.assertEqual(self.panel.import_materials_button.text(), '导入场次素材')
        self.assertEqual(self.panel.recognize_button.text(), '开始本场台本匹配')
        self.assertFalse(self.panel.sync_button.isVisible())
        self.assertEqual(self.panel.sync_button.text(), '开始声音合板')
        self.assertTrue(self.panel.sound_stage_button.isVisible())
        self.assertTrue(self.panel.refresh_button.isVisible())
        self.assertIn('导出跨场对应表', [action.text() for action in self.panel.advanced_button.menu().actions()])
        self.assertNotIn('导出跨场对应表', [button.text() for button in self.panel.findChildren(
            type(self.panel.refresh_button)) if button.isVisible()])
        self.assertFalse(self.panel.advanced.isVisible())
        self.assertFalse(self.panel.url.isVisible())
        self.assertFalse(self.panel.token.isVisible())
        button_texts=[button.text() for button in self.panel.findChildren(type(self.panel.refresh_button))]
        self.assertNotIn('添加素材…',button_texts)
        self.assertNotIn('移除所选登记',button_texts)
        self.panel.show_advanced(0)
        self.assertTrue(self.panel.advanced.isVisible())

    def test_all_five_stage_buttons_only_navigate_and_host_executes_work_once(self):
        workflow = Mock()
        self.panel.set_workflow(workflow)
        stages = []
        self.panel.workflow_stage_requested.connect(stages.append)
        technical = QWidget()
        self.panel.attach_technical_widget(technical)
        with patch.object(self.panel, 'open_material_import') as import_dialog:
            for stage, button in self.panel.stage_buttons.items():
                button.click()
                self.assertEqual(self.panel.workflow_stage, stage)
                self.assertIs(self.panel.content_stack.currentWidget(), technical if stage in ('camera', 'sound')
                    else self.panel.materials_page if stage == 'materials' else self.panel.catalogue_launch_page)
            import_dialog.assert_not_called()
        self.assertEqual(stages, ['materials', 'camera', 'sound', 'script', 'reference'])
        workflow.sync_recording.assert_not_called()
        workflow.recognize_and_match.assert_not_called()
        self.panel.sync_recordings()
        self.assertEqual(stages[-1], 'sync')
        self.assertEqual(self.panel.workflow_stage, 'sound')
        self.panel.recognize_and_match()
        self.assertEqual(stages[-1], 'match')
        self.assertEqual(self.panel.workflow_stage, 'script')
        workflow.sync_recording.assert_not_called()
        workflow.recognize_and_match.assert_not_called()
        # Setter is used by the host: no recursive signal, no work.
        self.panel.set_workflow_stage('camera')
        self.assertEqual(stages[-1], 'match')
        self.panel.attach_technical_widget(technical)
        self.assertEqual(self.panel.content_stack.count(), 4)

    def test_materials_overview_is_read_only_and_scopes_current_recording(self):
        first = self.add_session('day01_通条', {'pgm': '/NAS/one.mov', 'cut_xml_paths': ['/NAS/one.xml'],
            'sources': [{'camera': 'B', 'path': '/NAS/B.mov'}], 'sound_sources': [{'path': '/NAS/main.wav'}]})
        second = self.add_session('day02_分镜头', {'pgm': '/NAS/two.mov'})
        self.refresh(first)
        self.assertEqual(self.panel.materials_overview.rowCount(), 2)
        # Equal millisecond timestamps are ordered by UUID, not insertion.
        indexes = {self.panel.materials_overview.item(row, 0).data(Qt.ItemDataRole.UserRole): row
                   for row in range(self.panel.materials_overview.rowCount())}
        self.assertEqual([self.panel.materials_overview.item(indexes[first['id']], column).text() for column in range(5)],
            ['day01_通条', '1', '1', '1', '1'])
        self.assertEqual(self.panel.materials_overview.editTriggers(),
                         self.panel.materials_overview.EditTrigger.NoEditTriggers)
        self.panel.materials_overview.selectRow(indexes[second['id']])
        self.assertEqual(self.panel.selected_session()['id'], second['id'])
        self.assertIn('day02_分镜头', self.panel.materials_summary.text())

    def test_readiness_table_guides_unready_recording_without_locking_navigation(self):
        session = self.add_session('day01_通条', {'pgm': '/NAS/one.mov'})
        self.refresh(session)
        self.panel.set_workflow_stage('reference')
        self.assertEqual(self.panel.reference_readiness.item(0, 1).text(), '尚未登记')
        self.panel.apply_reference_state({'reference_session_id': session['id']})
        with patch('library_ui.QFileDialog.getExistingDirectory') as output:
            with self.assertRaisesRegex(ValueError, '机位与同步'):
                self.panel.export_reference()
            output.assert_not_called()
        stages = []
        self.panel.workflow_stage_requested.connect(stages.append)
        self.panel._open_readiness_session(0)
        self.assertEqual(stages, ['camera'])
        self.assertTrue(self.panel.camera_button.isEnabled())
        self.panel._session_readiness[session['id']].update(
            camera_ready=True, calibration_ready=True, cut_xml_ready=True, sound_ready=False)
        self.panel._navigate_readiness(session['id'])
        self.assertEqual(stages[-1], 'sound')

    def test_reference_actions_remain_accessible_with_many_recordings_at_small_size(self):
        for number in range(8):
            self.add_session('day0' + str(number // 2 + 1) + ('_通条' if number % 2 == 0 else '_分镜头'))
        self.refresh()
        self.panel.resize(1000, 648)
        self.panel.set_workflow_stage('reference')
        self.panel.show()
        QApplication.processEvents()
        self.assertEqual((self.panel.width(), self.panel.height()), (1000, 648))
        self.assertEqual(self.panel.reference_readiness.rowCount(), 8)
        self.assertLessEqual(self.panel.reference_readiness.height(), 124)
        self.assertTrue(self.panel.browser_open_button.isVisible())
        self.assertFalse(self.panel.story_timeline.isVisible())
        self.panel.open_browser_workspace()
        self.panel._browser_dialog.showNormal()
        self.panel._browser_dialog.resize(1280, 900)
        QApplication.processEvents()
        self.assertEqual(self.panel.browser_split.orientation(), Qt.Orientation.Horizontal)
        choices_right = self.panel.workspace_library.mapTo(self.panel.catalogue_page,
            QPoint(self.panel.workspace_library.width(), 0)).x()
        preview_left = self.panel.workspace_right.mapTo(self.panel.catalogue_page, QPoint()).x()
        self.assertLessEqual(choices_right, preview_left)
        self.assertGreater(self.panel.inline_host.height(), 150)
        self.assertTrue(self.panel.story_timeline.isVisible())
        self.assertGreaterEqual(self.panel.story_timeline.height(), 280)
        self.assertFalse(self.panel.comparison_actions.isVisible())
        self.assertTrue(self.panel.pending_list.isVisible())
        self.panel.main_pages.setCurrentIndex(1)
        self.assertEqual(self.panel.comparison_actions.currentIndex(), 1)
        self.assertTrue(self.panel.pending_list.isVisible())

    def test_host_cancelled_recording_load_preserves_current_selection(self):
        current = self.add_session('当前场次')
        target = self.add_session('目标场次')
        self.refresh(current)
        self.panel.workflow_stage_requested.connect(lambda stage: None)
        self.panel.load_session_callback = lambda payload: False
        previous_status = self.panel.status.text()
        self.panel._load(target['id'])
        self.assertEqual(self.panel.selected_session()['id'], current['id'])
        self.assertEqual(self.panel.working_session.currentData(), current['id'])
        self.assertEqual(self.panel.status.text(), previous_status)

    def test_refresh_reads_saved_camera_calibration_and_sound_readiness_for_selected_recording(self):
        path, wav = '/NAS/full_PGM.mov', '/NAS/original_main.wav'
        config = dict(pgm=path, fps=25, cut_xml='/NAS/full_cuts.xml',
            pgm_sources=[dict(path=path, frames=100, start=0, end=100)],
            sound_sources=[dict(path=wav)], calibration_confirmed=False)
        session = self.add_session('day01_通条', config)
        self.refresh(session)
        self.assertIn('声音文件', self.panel.readiness_button.text())
        config.update(calibration_confirmed=True, library_sync={path: dict(status='matched',
            placements=[dict(wav_path=wav, pgm_start_seconds=0, pgm_end_seconds=4)])})
        saved = self.client.call('save_session', production_id=self.production['id'], name=session['name'],
            session_id=session['id'], expected_revision=session['revision'], config=config,
            segments=[dict(start=0, end=100, camera='A', reason='manual')])
        refreshed = []
        self.panel.catalogue_refreshed.connect(refreshed.append)
        with patch('core.probe', side_effect=AssertionError('overview must not probe NAS')):
            self.refresh()
        self.assertEqual(self.panel.selected_session()['revision'], saved['revision'])
        self.assertEqual(self.panel.working_session.currentData(), session['id'])
        self.assertTrue(self.panel._session_readiness[session['id']]['ready'])
        self.assertIn('已就绪', self.panel.readiness_button.text())
        self.assertEqual([self.panel.reference_readiness.item(0, column).text() for column in (1, 2, 3)],
                         ['1/1 已确认', '已完成', '1/1 已合板'])
        self.assertTrue(refreshed[-1]['session_readiness'][session['id']]['ready'])
        # A later saved decision is read afresh rather than keeping an optimistic
        # state from the previous recognition or alignment operation.
        config['calibration_confirmed'] = False
        self.client.call('save_session', production_id=self.production['id'], name=session['name'],
            session_id=session['id'], expected_revision=saved['revision'], config=config,
            segments=saved['segments'])
        self.refresh()
        self.assertFalse(self.panel._session_readiness[session['id']]['ready'])
        self.assertIn('待校准', self.panel.readiness_button.text())

    def test_readiness_shows_completed_counts_and_locates_only_remaining_issues(self):
        session = self.add_session('day01_通条', {'pgm': '/NAS/PIX_001_019.mov'})
        self.refresh(session)
        camera_issue = dict(shot_index=183, start=33942, end=34061, camera='?',
            reason='sample_conflict', message='采样结果冲突，请人工确认。',
            pgm_path='/NAS/PIX_001_019.mov', pgm_frame=3325)
        sound_issue = dict(pgm_path='/NAS/PIX_001_020.mov', message='波形匹配未达到确认阈值。')
        self.panel._session_readiness[session['id']].update(camera_total=1525,
            camera_confirmed=1524, camera_pending=1, camera_pending_rows=[camera_issue],
            sound_total=59, sound_matched=53, sound_pending=6, sound_pending_rows=[sound_issue])
        self.panel._refresh_preparation_status()
        self.assertEqual(self.panel.reference_readiness.item(0, 1).text(), '1524/1525 已确认')
        self.assertEqual(self.panel.reference_readiness.item(0, 3).text(), '53/59 已合板')
        self.assertIn('1 镜头', self.panel.readiness_button.text())
        self.assertIn('6 声音文件', self.panel.readiness_button.text())
        locations = []
        self.panel.preparation_issue_requested.connect(locations.append)
        with patch('library_ui.QDialog.exec', return_value=0):
            self.panel.show_preparation_status()
        dialog = self.panel._preparation_dialog
        self.assertEqual(dialog.pending_table.rowCount(), 2)
        self.assertIn('183', dialog.pending_table.item(0, 1).text())
        self.assertIn('PIX_001_020.mov', dialog.pending_table.item(1, 1).text())
        self.assertIn(sound_issue['message'], dialog.pending_table.item(1, 2).text())
        dialog.pending_table.selectRow(0)
        dialog.locate_button.click()
        self.assertEqual(locations[-1], dict(session_id=session['id'], stage='camera', issue=camera_issue))
        dialog.pending_table.selectRow(1)
        dialog.locate_button.click()
        self.assertEqual(locations[-1], dict(session_id=session['id'], stage='sound', issue=sound_issue))

    def test_legacy_sound_counts_show_silence_without_hiding_unknown_camera(self):
        session = self.add_session('day01_通条', {'pgm': '/NAS/PIX_001_019.mov'})
        self.refresh(session)
        camera_issue = dict(shot_index=183, start=33942, end=34061, camera='?',
            reason='sample_conflict', message='采样结果冲突，请人工确认。')
        fallback_rows = [dict(pgm_path=f'/NAS/PIX_{index:03d}.mov',
            message='使用 PGM 原声', original_message='没有匹配到原始主 WAV') for index in range(6)]
        state = self.panel._session_readiness[session['id']]
        state.update(camera_total=1525, camera_confirmed=1524, camera_pending=1,
            camera_pending_rows=[camera_issue], sound_total=59, sound_matched=53,
            sound_fallback=6, sound_fallback_rows=fallback_rows, sound_pending=0,
            sound_pending_rows=[], sound_ready=True, ready=False,
            issues=[dict(code='camera_pending', message='还有一个机位需要确认', next_step='camera_sync')])
        self.panel._refresh_preparation_status()
        self.assertEqual(self.panel.reference_readiness.item(0, 3).text(), '53 原WAV + 6 音频留空')
        self.assertIn('1 镜头', self.panel.readiness_button.text())
        self.assertNotIn('声音文件', self.panel.readiness_button.text())
        details = self.panel._preparation_detail(session, state)
        self.assertIn('PIX_000.mov：未匹配原始 WAV，音频留空', details)
        with patch('library_ui.QDialog.exec', return_value=0):
            self.panel.show_preparation_status()
        self.assertEqual(self.panel._preparation_dialog.pending_table.rowCount(), 1)
        self.assertIn('183', self.panel._preparation_dialog.pending_table.item(0, 1).text())

    def test_old_waveform_failure_displays_silence_without_reporting_operation_failure(self):
        path = '/NAS/Drama/PGM_original.mov'
        config = dict(pgm=path, library_processing={path: dict(status='failed', stage='sync', error='波形未匹配')},
            library_sound_fallback={path: dict(status='fallback', stage='sync', audio_source='pgm',
                error='波形未匹配', pgm_info={'audio_channels': [{'source_channel': 1}]})})
        session = self.add_session('day02_分镜头', config)
        self.refresh(session)
        row = dict(session_id=session['id'], session_name=session['name'], media_path=path, pgm_path=path)
        self.assertEqual(self.panel._audio_failure(row), '')
        self.panel._show_match_details(row)
        self.assertEqual(self.panel.match_sync_status.text(), '声音合板：未匹配原始 WAV，音频留空')
        self.assertIn('未使用原始 WAV 的原因：波形未匹配', self.panel.match_detail.toPlainText())
        self.assertNotIn('本次合板失败', self.panel.match_sync_status.text())

    def test_new_wav_match_takes_display_priority_over_stale_fallback_history(self):
        path='/NAS/Drama/newly_matched.mov'
        config=dict(pgm=path,library_sync={path:dict(status='matched',placements=[dict(method='waveform')],uncovered=[dict(start=0,end=1)])},
            library_sound_fallback={path:dict(status='fallback',stage='sync',audio_source='pgm',error='旧失败')})
        session=self.add_session('day03_分镜头',config)
        self.refresh(session)
        row=dict(session_id=session['id'],session_name=session['name'],media_path=path,pgm_path=path)
        self.panel._show_match_details(row)
        self.assertEqual(self.panel.match_sync_status.text(),'声音合板：已合板（波形匹配） · 1 处音频留空')
        self.assertNotIn('旧失败',self.panel.match_detail.toPlainText())

    def test_host_export_defers_old_overview_but_real_backend_still_rejects_unsynced_sound(self):
        from reference_workflow import ReferenceWorkflow
        from reference_timeline import ReferenceTimelineError
        from workflow_readiness import ensure_session_prepared
        path = '/NAS/Drama/full_PGM.mov'
        xml = Path(self.tmp.name) / 'cuts.xml'
        xml.write_text('<test/>')
        config = dict(pgm=path, fps=25, cut_xml=str(xml), recording_kind='continuous',
            pgm_sources=[dict(path=path, frames=100, start=0, end=100)],
            calibration_confirmed=True, sound_sources=[dict(path='/NAS/main.wav')])
        session = self.client.call('save_session', production_id=self.production['id'], name='day01_通条',
            config=config, segments=[dict(start=0, end=100, camera='A', reason='manual')])
        backend = ReferenceWorkflow(self.client, self.production['id'])
        backend.set_reference(session['id'])
        self.refresh(session)
        self.panel.workflow_stage_requested.connect(lambda stage: None)
        requests, failures = [], []
        def export(request):
            requests.append(request)
            try:
                backend.export_reference(request['output_dir'])
            except ReferenceTimelineError as exc:
                failures.append(str(exc))
        self.panel.reference_export_requested.connect(export)
        cut = dict(fps=25, duration=100, pgm_sources=[dict(path=path, frames=100, start=0, end=100)],
                   shots=[dict(start=0, end=100)])
        output = str(Path(self.tmp.name) / 'export')
        with patch('library_ui.QFileDialog.getExistingDirectory', return_value=output), \
             patch('cut_xml.timeline_info', return_value=cut), \
             patch('workflow_readiness.ensure_session_prepared', wraps=ensure_session_prepared) as strict, \
             patch('core.export') as render:
            self.panel.export_reference()
        self.assertEqual(len(requests), 1)
        strict.assert_called_once()
        render.assert_not_called()
        self.assertEqual(len(failures), 1)
        self.assertIn('统一声音合板', failures[0])
        self.assertFalse(Path(output).exists())

    def test_refresh_drops_selection_missing_from_new_catalogue_instead_of_stale_pending_status(self):
        prior = self.add_session('旧场次', {'library_notes': '旧备注'})
        self.refresh(prior)
        production = self.client.call('create_production', name='另一项目')
        next_session = self.client.call('save_session', production_id=production['id'], name='新场次',
                                        config={}, segments=[])
        self.panel.apply_workflow_context(self.client, production['id'], production['name'])
        self.panel._set_data(self.panel._read_all(self.client, production['id']))
        self.assertIsNone(self.panel.selected_session())
        self.assertIsNone(self.panel.working_session.currentData())
        self.assertEqual(self.panel.session_name.text(), '')
        self.assertEqual(self.panel.notes.toPlainText(), '')
        self.assertEqual(self.panel.readiness_button.text(), '准备状态')
        self.assertEqual(self.panel.reference_readiness.item(0, 0).data(Qt.ItemDataRole.UserRole), next_session['id'])

    def test_host_load_waits_for_read_worker_before_requesting_another_save(self):
        session = self.add_session('day01_通条')
        self.refresh(session)
        deferred = []
        self.panel.worker = Mock()
        try:
            with patch('project_ui.QTimer.singleShot', side_effect=lambda delay, callback: deferred.append(callback)):
                self.panel._load(session['id'])
            self.assertEqual(self.loaded, [])
            self.assertEqual(len(deferred), 1)
            self.panel.worker = None
            deferred[0]()
            self.assertEqual(self.loaded[0]['id'], session['id'])
        finally:
            self.panel.worker = None

    def test_batch_toolbar_asks_once_and_delegates_without_prior_connection(self):
        workflow = Mock()
        self.panel.set_workflow(workflow)
        self.panel.client = None
        paths = ['/NAS/PGM_001.mov', '/NAS/PGM_002.mov', '/NAS/PGM_003.mov']
        with patch('library_ui.QFileDialog.getOpenFileNames', return_value=(paths, '')), \
             patch('library_ui.QInputDialog.getText', return_value=('第三场拉通', True)) as batch:
            self.panel.ingest_pgm()
        batch.assert_called_once()
        workflow.ingest_pgm.assert_called_once_with(self.panel, paths, '第三场拉通')
        with patch('library_ui.QFileDialog.getOpenFileName', return_value=('/NAS/台本.docx', '')):
            self.panel.ingest_script()
        workflow.import_script.assert_called_once_with(self.panel, '/NAS/台本.docx')
        self.panel.recognize_and_match()
        workflow.recognize_and_match.assert_called_once_with(self.panel, session_id=None, require_synced=True, require_prepared=True)

    def test_unified_material_dialog_groups_new_media_and_appends_with_revision(self):
        session = self.add_session('第二次拍摄', {'pgm': '/NAS/old_PGM.mov',
            'sources': [{'camera': 'A', 'path': '/NAS/old_A.mov'}],
            'sound_sources': [{'path': '/NAS/old_main.wav'}]})
        notes = self.add_session('场记', {'library_record_type': 'notes'})
        dialog = MaterialImportDialog([session, notes], session['id'], append=True)
        self.assertEqual(dialog.session.count(), 1)
        self.assertEqual(dialog.pickers['pgm'].existing.count(), 1)
        self.assertEqual(dialog.pickers['proxy'].existing.count(), 1)
        self.assertEqual(dialog.pickers['main_wav'].existing.count(), 1)
        dialog.add_paths('pgm', ['/NAS/old_PGM.mov', '/NAS/new_PGM.mov', '/NAS/new_PGM.mov'])
        dialog.add_paths('proxy', ['/NAS/A_001.mov', '/NAS/A_002.mov'], 'A')
        dialog.add_paths('proxy', ['/NAS/B_001.mov'], 'B')
        dialog.add_paths('main_wav', ['/NAS/new_main.wav'])
        payload = dialog.materials()
        self.assertEqual(payload['pgm_paths'], ['/NAS/new_PGM.mov'])
        self.assertEqual(payload['proxies'], [{'camera': 'A', 'path': '/NAS/A_001.mov'},
            {'camera': 'A', 'path': '/NAS/A_002.mov'}, {'camera': 'B', 'path': '/NAS/B_001.mov'}])
        self.assertEqual(payload['audio_paths'], ['/NAS/new_main.wav'])
        self.assertEqual((payload['session_id'], payload['expected_revision']), (session['id'], 1))
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        dialog.close()

    def test_unified_material_dialog_allows_audio_only_append_and_validates_new_batch(self):
        session = self.add_session('第一场', {'pgm': '/NAS/one.mov'})
        dialog = MaterialImportDialog([session], session['id'])
        dialog.confirm_camera_configuration()
        dialog.add_paths('main_wav', ['/NAS/main.wav'])
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertIn('PGM', dialog.validation.text())
        dialog.mode.setCurrentIndex(1)
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.materials()['pgm_paths'], [])
        dialog.close()

    def test_unified_toolbar_delegates_once_and_explicit_append_selects_requested_batch(self):
        session = self.add_session('第二场')
        self.refresh(session)
        workflow = Mock()
        self.panel.set_workflow(workflow)
        payload = dict(pgm_paths=['/NAS/new.mov'], batch_name=session['name'],
            proxies=[{'camera': 'D', 'path': '/NAS/D.mov'}], audio_paths=['/NAS/mix.wav'],
            session_id=session['id'], expected_revision=session['revision'])
        with patch('library_ui.MaterialImportDialog') as constructor:
            dialog = constructor.return_value
            dialog.exec.return_value = QDialog.DialogCode.Accepted
            dialog.materials.return_value = payload
            self.panel.open_material_import(session['id'])
        self.assertTrue(constructor.call_args.kwargs['append'])
        self.assertEqual(constructor.call_args.args[1], session['id'])
        workflow.ingest_materials.assert_called_once_with(self.panel, payload['pgm_paths'], payload['batch_name'],
            payload['proxies'], payload['audio_paths'], session_id=session['id'], expected_revision=1)
        changed = []
        self.panel.material_session_changed.connect(changed.append)
        self.panel.materials_ingested(session)
        self.assertEqual(changed, [session])

    def test_recording_slots_and_cut_xml_order_are_explicit_and_legacy_append_keeps_identity(self):
        legacy = self.add_session('旧名字第三场', {'cut_xml': '/NAS/first.xml', 'pgm': '/NAS/first.mov'})
        dialog = MaterialImportDialog([legacy])
        self.assertTrue(dialog.recording_day.isEditable())
        self.assertTrue(dialog.recording_kind.isEditable())
        self.assertEqual([dialog.recording_day.itemText(index) for index in range(4)],
                         [f'day{day:02d}' for day in range(1, 5)])
        dialog.recording_day.setCurrentText('day04')
        dialog.recording_kind.setCurrentText('分镜头')
        dialog.confirm_camera_configuration()
        dialog.add_paths('pgm', ['/NAS/day4.mov'])
        dialog.add_paths('cut_xml', ['/NAS/part2.xml', '/NAS/part1.xml'])
        dialog.pickers['cut_xml'].additions.setCurrentRow(1)
        dialog.pickers['cut_xml'].move_selected(-1)
        payload = dialog.materials()
        self.assertEqual(payload['cut_xml_paths'], ['/NAS/part1.xml', '/NAS/part2.xml'])
        self.assertEqual((payload['batch_name'], payload['recording_day'], payload['recording_kind']),
                         ('day04_分镜头', 4, 'coverage'))
        dialog.mode.setCurrentIndex(1)
        payload = dialog.materials()
        self.assertEqual(payload['batch_name'], '旧名字第三场')
        self.assertEqual(payload['session_id'], legacy['id'])
        self.assertIsNone(payload['recording_day'])
        self.assertIsNone(payload['recording_kind'])
        self.assertEqual(dialog.pickers['cut_xml'].existing_records, [{'path': '/NAS/first.xml'}])
        dialog.close()

    def test_split_workflow_never_syncs_again_when_matching_and_has_no_duplicate_import_buttons(self):
        workflow = Mock()
        self.panel.set_workflow(workflow)
        self.panel.sync_recordings()
        workflow.sync_recording.assert_called_once_with(self.panel, session_id=None)
        workflow.recognize_and_match.assert_not_called()
        self.panel.recognize_and_match()
        workflow.recognize_and_match.assert_called_once_with(self.panel, session_id=None, require_synced=True, require_prepared=True)
        self.panel.show_advanced(2)
        titles = [button.text() for button in self.panel.advanced.findChildren(type(self.panel.refresh_button))]
        for removed in ('合板并识别全部PGM', '导入字幕／转写', '跨场匹配', '选择声音／视频',
                        '新建拍摄场次', '当前项目另存为场次', '导入台本'):
            self.assertNotIn(removed, titles)

    def test_current_recording_scopes_sync_and_match_and_xml_kwargs_reach_backend_once(self):
        first = self.add_session('day01_通条', {'pgm': '/NAS/one.mov', 'recording_day': 1,
                                            'recording_kind': 'continuous'})
        second = self.add_session('day02_分镜头', {'pgm': '/NAS/two.mov', 'recording_day': 2,
                                                'recording_kind': 'coverage'})
        self.refresh(first)
        workflow = Mock()
        self.panel.set_workflow(workflow)
        self.panel.working_session.setCurrentIndex(self.panel.working_session.findData(second['id']))
        self.assertEqual(self.panel.selected_session()['id'], second['id'])
        self.panel.sync_recordings()
        self.panel.recognize_and_match()
        workflow.sync_recording.assert_called_once_with(self.panel, session_id=second['id'])
        workflow.recognize_and_match.assert_called_once_with(self.panel, session_id=second['id'], require_synced=True, require_prepared=True)
        self.panel._delegate_ingest_materials(['/NAS/new.mov'], 'day03_通条',
            [{'camera': 'C', 'path': '/NAS/C.mov'}], ['/NAS/main.wav'],
            cut_xml_paths=['/NAS/one.xml', '/NAS/two.xml'], recording_day=3, recording_kind='continuous')
        self.assertEqual(workflow.ingest_materials.call_args.kwargs,
            {'session_id': None, 'expected_revision': None, 'cut_xml_paths': ['/NAS/one.xml', '/NAS/two.xml'],
             'recording_day': 3, 'recording_kind': 'continuous'})

    def test_duplicate_standard_recording_slot_requires_explicit_append(self):
        session = self.add_session('day01_通条', {'recording_day': 1, 'recording_kind': 'continuous'})
        dialog = MaterialImportDialog([session])
        dialog.confirm_camera_configuration()
        dialog.add_paths('pgm', ['/NAS/first.mov'])
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertIn('追加', dialog.validation.text())
        dialog.mode.setCurrentIndex(1)
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.materials()['session_id'], session['id'])
        dialog.close()

    def _confirmed_reference_fixture(self):
        first, second = self.seed_matches()
        for row in list(self.panel._candidates):
            if row['status'] != 'confirmed':
                self.client.call('update_candidate', candidate_id=row['id'], status='confirmed',
                                 expected_revision=row['revision'])
        self.refresh(first)
        baseline = next(row for row in self.panel._candidates if row['session_id'] == first['id'])
        alternative = next(row for row in self.panel._candidates if row['session_id'] == second['id'])
        return first, second, baseline, alternative

    def _review_reference_take(self, clip, *, reviewed=True, pool='timeline'):
        """Persist a genuine file review, independently from historical anchor pairs."""
        from pgm_editor_state import coverage_fingerprint
        if pool == 'timeline':
            from library_workflow import BatchWorkflow
            workflow = BatchWorkflow(self.client, self.production['id'], self.client.actor)
            version = next(row for row in workflow.read_all()['pgm_match_versions']
                           if row['session_id'] == clip['session_id'] and row['media_path'] == clip['media_path'])
            workflow.mark_pgm_reviewed(clip['session_id'], clip['media_path'],
                                      expected_match_version=version, reviewed=reviewed)
            self.refresh()
            return
        index = self.panel._take_index()
        summary = index.file_summary(clip)
        first = summary.get('first_candidate')
        session = self.client.call('get_session', session_id=clip['session_id'])
        config = copy.deepcopy(session['config'])
        config.setdefault('library_pgm_editor', {})[clip['media_path']] = dict(
            pool=pool, reviewed=reviewed,
            coverage_fingerprint=coverage_fingerprint(summary, index.source(first) if first else None))
        self.client.call('save_session', production_id=self.production['id'], session_id=session['id'],
            expected_revision=session['revision'], name=session['name'], config=config)
        self.refresh()

    def test_reference_cards_do_not_offer_independent_export_checkboxes(self):
        first, second, baseline, alternative = self._confirmed_reference_fixture()
        self.panel.apply_reference_state({'reference_session_id': first['id']})
        items = {self.panel.match_list.item(i).data(Qt.ItemDataRole.UserRole): self.panel.match_list.item(i)
                 for i in range(self.panel.match_list.count())}
        for item in items.values():
            self.assertFalse(item.flags() & Qt.ItemFlag.ItemIsUserCheckable)
            self.assertIsNone(item.data(Qt.ItemDataRole.CheckStateRole))
        self.assertEqual(self.panel._automatic_export_pairs(), {})
        self.panel.apply_reference_state({'reference_session_id': first['id'],
            'selected_clip_pairs': {alternative['id']: baseline['id']}, 'revision': 2})
        self.assertEqual(self.panel._automatic_export_pairs(), {}, '历史勾选不能把蓝色片段加入导出')

    def test_repeated_baseline_line_requires_explicit_pgm_and_range_choice(self):
        first, second, baseline, alternative = self._confirmed_reference_fixture()
        self.client.call('add_transcript', session_id=first['id'], media_path='/NAS/repeat.mov',
            segments=[{'start_seconds': 100, 'end_seconds': 103, 'text': 'Good morning，今天你好吗？'}])
        self.refresh(first)
        self.panel.match()
        for row in list(self.panel._candidates):
            if row['status'] != 'confirmed':
                self.client.call('update_candidate', candidate_id=row['id'], status='confirmed',
                                 expected_revision=row['revision'])
        self.refresh(first)
        self.panel.apply_reference_state({'reference_session_id': first['id']})
        choices = self.panel._baseline_candidates(alternative)
        self.assertEqual(len(choices), 2)
        def choose(_parent, _title, prompt, labels, *_args):
            self.assertIn('哪一次台词', prompt)
            self.assertTrue(all('—' in label and '.mov' in label for label in labels))
            return next(label for label in labels if 'repeat.mov' in label), True
        with patch('library_ui.QInputDialog.getItem', side_effect=choose):
            selected = self.panel._choose_baseline_candidate(alternative)
        self.assertEqual(next(row for row in choices if row['id'] == selected)['media_path'], '/NAS/repeat.mov')

    def test_take_filters_follow_green_track_membership_without_saved_selection(self):
        first, second, baseline, alternative = self._confirmed_reference_fixture()
        state = {'reference_session_id': first['id'], 'selected_clip_pairs': {},
                 'baseline_export': {'valid': True}}
        self._review_reference_take(alternative)
        self.panel.apply_reference_state(state)
        self.panel.take_mode.setCurrentIndex(self.panel.take_mode.findData('alternatives'))
        self.assertEqual([r['id'] for r in self.panel._visible_candidates], [alternative['id']])
        self.assertTrue(self.panel.select_take_button.isHidden())
        self.assertEqual(self.panel._automatic_export_pairs(), {alternative['id']: None})
        self.panel.take_mode.setCurrentIndex(self.panel.take_mode.findData('selected'))
        self.assertEqual(self.panel.match_list.count(), 1)
        self.panel.take_session_filter.setCurrentIndex(self.panel.take_session_filter.findData(first['id']))
        self.assertEqual(self.panel.match_list.count(), 0)
        self.assertEqual(self.panel.selected_clip_pairs, {})
        self.panel.take_session_filter.setCurrentIndex(0)
        self.assertEqual(self.panel.match_list.count(), 1)
        self._review_reference_take(alternative, reviewed=False)
        self.panel.apply_reference_state(state)
        self.panel.take_mode.setCurrentIndex(self.panel.take_mode.findData('selected'))
        self.assertEqual(self.panel.match_list.count(), 0)

    def test_green_track_can_be_prepared_before_baseline_and_anchor_is_resolved_at_export(self):
        first, second, baseline, alternative = self._confirmed_reference_fixture()
        self._review_reference_take(alternative)
        self.panel.apply_reference_state({})
        self.assertTrue(self.panel.select_take_button.isHidden())
        self.assertFalse(self.panel.export_reference_button.isEnabled())
        with self.assertRaisesRegex(ValueError, '选择基准'):
            self.panel.export_reference(True, False)
        self.panel.apply_reference_state({'reference_session_id': first['id']})
        self.panel.take_mode.setCurrentIndex(self.panel.take_mode.findData('alternatives'))
        self.assertEqual(self.panel._automatic_export_pairs(), {alternative['id']: None})
        with patch.object(self.panel, '_baseline_candidates', return_value=[]):
            with self.assertRaisesRegex(ValueError, '没有'):
                self.panel._choose_baseline_candidate(alternative)

    def test_removed_green_take_is_not_exported_even_with_a_historical_anchor(self):
        first, second, baseline, alternative = self._confirmed_reference_fixture()
        self._review_reference_take(alternative, pool='removed')
        self.panel.apply_reference_state({'reference_session_id': first['id'],
            'selected_clip_pairs': {alternative['id']: baseline['id']}, 'baseline_export': {'valid': False}})
        self.panel.take_mode.setCurrentIndex(self.panel.take_mode.findData('selected'))
        self.assertEqual(self.panel.match_list.count(), 0)
        self.assertEqual(self.panel._automatic_export_pairs(), {})
        self.assertEqual(self.panel.selected_clip_pairs, {alternative['id']: baseline['id']})

    def test_both_export_modes_need_mapped_base_but_do_not_need_a_previous_export(self):
        first, second, baseline, alternative = self._confirmed_reference_fixture()
        pairs = {alternative['id']: None}
        self._review_reference_take(alternative)
        from baseline_script_map import build_baseline_map
        data = dict(sessions=self.panel._sessions, scripts=self.panel._scripts,
                    transcripts=self.panel._transcripts, candidates=self.panel._candidates)
        cut = dict(fps=25, duration=500, pgm_sources=[dict(path=baseline['media_path'], start=0, end=500, frames=500)])
        mapping = build_baseline_map(data, first['id'], cut)
        for segment in mapping['segments']:
            segment['needs_review'] = False
        self.panel.apply_reference_state({'reference_session_id': first['id'],
            'selected_clip_pairs': {}, 'baseline_script_map': mapping})
        self.assertTrue(self.panel.export_reference_button.isEnabled())
        self.assertTrue(self.panel.export_alternatives_button.isEnabled())
        requests = []
        self.panel.reference_export_requested.connect(requests.append)
        self.panel._session_readiness = {session['id']: {'ready': True, 'camera_ready': True,
            'calibration_ready': True, 'sound_ready': True, 'cut_xml_ready': True} for session in (first, second)}
        with patch('library_ui.QFileDialog.getExistingDirectory', return_value='/NAS/Exports'):
            self.panel.export_reference(True, True)
            self.panel.export_reference(True, False)
        self.assertEqual([r['include_baseline'] for r in requests], [True, False])
        self.assertTrue(all(r['include_alternatives'] for r in requests))
        self.assertEqual(requests[1]['exported_take_pairs'], pairs)
        self.assertEqual(requests[1]['export_snapshot'], self.panel._take_index().export_signature(first['id']))
        self.assertIsNone(requests[1]['resolved_pairs'])
        self.assertEqual(requests[1]['reference_session_id'], first['id'])
        self.panel.set_external_busy(True)
        self.assertFalse(self.panel.export_reference_button.isEnabled())
        self.assertFalse(self.panel.export_alternatives_button.isEnabled())
        self.panel.set_external_busy(False)
        self.assertTrue(self.panel.export_alternatives_button.isEnabled())

    def test_legacy_baseline_requires_explicit_continuous_confirmation_without_renaming(self):
        legacy = self.add_session('原场次名称')
        coverage = self.add_session('day01_分镜头', {'recording_day': 1, 'recording_kind': 'coverage'})
        self.refresh(legacy)
        events = []
        self.panel.reference_changed.connect(events.append)
        def choose(_parent, _title, _prompt, labels, *_args):
            self.assertEqual(labels, ['原场次名称（确认通条）'])
            return labels[0], True
        with patch('library_ui.QInputDialog.getItem', side_effect=choose), \
             patch('library_ui.QMessageBox.question', return_value=QMessageBox.StandardButton.No):
            self.panel.choose_reference()
        self.assertEqual(events, [])
        with patch('library_ui.QInputDialog.getItem', side_effect=choose), \
             patch('library_ui.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes):
            self.panel.choose_reference()
        self.assertEqual(events, [{'production_id': self.production['id'],
            'reference_session_id': legacy['id'], 'legacy_continuous_confirmed': True}])
        self.assertEqual(self.client.call('get_session', session_id=legacy['id'])['name'], '原场次名称')

    def test_director_correspondence_export_outputs_real_csv_json_with_explicit_scope(self):
        self.seed_matches()
        self.panel.candidates.selectRow(0)
        self.panel.update_candidate('confirmed')
        for choice, expected in (('仅已确认', 1), ('已确认及待核实', 2)):
            output = Path(self.tmp.name) / f'lookup_{expected}.csv'
            with patch('library_ui.QInputDialog.getItem', return_value=(choice, True)), \
                 patch('library_ui.QFileDialog.getSaveFileName', return_value=(str(output), '')):
                self.panel.export_correspondence()
            document = json.loads(output.with_suffix('.json').read_text(encoding='utf-8'))
            self.assertEqual(document['count'], expected)
            self.assertEqual(document['include_candidates'], expected == 2)
            self.assertTrue(output.read_bytes().startswith(b'\xef\xbb\xbf'))
            self.assertEqual(self.panel.correspondence_last_result['csv_path'], str(output.resolve()))
        self.assertIn('CSV + JSON', self.panel.status.text())

    def test_selected_script_shows_all_performances_and_source_details(self):
        self.seed_matches()
        self.assertEqual(self.panel.script_tree.topLevelItemCount(), 1)
        self.assertEqual(self.panel.script_tree.topLevelItem(0).text(0), '第一幕')
        self.assertEqual(self.panel.match_list.count(), 2)
        self.assertIn('2 条PGM · 已核实 0 · 待核实 2 · 2 场', self.panel.match_count.text())
        self.panel.match_list.setCurrentRow(1)
        chosen = self.panel._candidate()
        self.assertIn(chosen['session_name'], self.panel.match_detail.toPlainText())
        self.assertIn('PGM：', self.panel.match_detail.toPlainText())
        self.assertIn('代理：', self.panel.match_detail.toPlainText())
        self.assertIn('音频：', self.panel.match_detail.toPlainText())
        self.assertEqual(self.panel.match_list.currentItem().data(256), chosen['id'])

    def test_counts_distinguish_utterances_pgm_files_and_recording_sessions(self):
        first, second = self.seed_matches()
        self.add_session('场记导入', {'library_record_type': 'notes'})
        self.client.call('add_transcript', session_id=first['id'], media_path='/NAS/Drama/第一场.mov',
            segments=[{'start_seconds': 24, 'end_seconds': 28, 'text': 'Good morning，今天你好吗？'}])
        self.refresh(first)
        self.panel.match()
        self.assertEqual(self.panel.match_count.text(), '2 条PGM · 已核实 0 · 待核实 2 · 2 场')
        self.assertIn('2 个拍摄场次', self.panel.script_count.text())
        self.assertIn('轨道待核实', self.panel.script_count.text())
        self.assertNotIn('台本区间', self.panel.script_count.text())
        self.panel.show_advanced(2)
        first_index = next(index for index,row in enumerate(self.panel._advanced_candidates) if row['session_id'] == first['id'])
        self.panel.candidates.selectRow(first_index)
        self.panel.update_candidate('rejected')
        self.assertNotIn('条素材', self.panel.script_tree.topLevelItem(0).child(0).text(0))
        self.assertTrue(self.panel.match_count.text().startswith('2 条PGM'))

    def test_registered_proxy_and_sound_do_not_imply_synced_associations(self):
        first, second = self.seed_matches()
        config = copy.deepcopy(first['config'])
        config['library_media'] += [{'kind': 'proxy', 'path': '/NAS/Drama/A_proxy.mov'},
                                    {'kind': 'main_wav', 'path': '/NAS/Drama/mix.wav'}]
        updated = self.client.call('save_session', production_id=self.production['id'],
            session_id=first['id'], expected_revision=first['revision'], name=first['name'], config=config)
        self.refresh(updated)
        candidate = next(row for row in self.panel._candidates if row['session_id'] == first['id'])
        self.panel._show_match_details(candidate)
        details = self.panel.match_detail.toPlainText()
        self.assertIn('代理：未关联', details)
        self.assertIn('音频：未关联／同步', details)
        self.assertIn('本场已登记代理（尚未关联）：A_proxy.mov', details)
        self.assertIn('本场已登记录音（尚未关联／同步）：mix.wav', details)

    def test_retired_script_candidates_do_not_hide_unmatched_dialogue(self):
        first, second = self.seed_matches()
        self.client.call('put_script', production_id=self.production['id'], title='台本', lines=[
            {'scene': '第一幕', 'speaker': 'Jack', 'text': '修订后的另一句台词', 'order': 0}])
        self.refresh(first)
        self.assertEqual(self.panel.pending_list.count(), 2)
        self.assertNotIn('条素材', self.panel.script_tree.topLevelItem(0).child(0).text(0))

    def seed_timeline_cases(self):
        sessions = [self.add_session('第一次特写'), self.add_session('第三次拉通')]
        self.client.call('put_script', production_id=self.production['id'], title='全剧台本', lines=[
            {'scene': '第一幕', 'speaker': 'Jack', 'text': 'Good morning，今天你好吗？', 'order': 0},
            {'scene': '第一幕', 'speaker': 'Algy', 'text': 'Please sit down.', 'order': 1},
            {'scene': '第二幕', 'speaker': 'Cecily', 'text': '暂时没有拍摄的另一句。', 'order': 2}])
        lines = self.client.call('list_script', production_id=self.production['id'])
        for index, session in enumerate(sessions):
            result = self.client.call('add_transcript', session_id=session['id'],
                media_path=f'/NAS/Drama/take_{index}.mov', segments=[
                    {'start_seconds': 1, 'end_seconds': 3, 'text': lines[0]['text']},
                    {'start_seconds': 4, 'end_seconds': 6, 'text': lines[1]['text']}])
            transcript_ids = result['transcript_ids']
            candidates = [{'script_line_id': lines[0]['id'], 'transcript_id': transcript_ids[0],
                           'score': .98, 'reason': '台词相符'}]
            if index == 0:
                candidates.append({'script_line_id': lines[1]['id'], 'transcript_id': transcript_ids[1],
                                   'score': .94, 'reason': '台词相符'})
            self.client.call('replace_candidates', production_id=self.production['id'], candidates=candidates)
        first = self.client.call('list_candidates', production_id=self.production['id'],
                                 script_line_id=lines[0]['id'])[0]
        self.client.call('update_candidate', candidate_id=first['id'], expected_revision=first['revision'],
                         status='confirmed')
        self.refresh(sessions[0])
        return sessions, lines

    def test_full_script_search_locates_lines_without_hiding_other_passages(self):
        from script_timeline import ScriptTimeline
        sessions, lines = self.seed_timeline_cases()
        self.assertIsInstance(self.panel.script_tree, ScriptTimeline)
        self.assertIs(self.panel.script_tree, self.panel.script_timeline)
        self.assertEqual([(row['count'], row['confirmed_count'], row['candidate_count'])
                          for row in self.panel._timeline_nodes], [(2, 1, 1), (1, 0, 1), (0, 0, 0)])
        self.assertEqual({row['status'] for row in self.panel._visible_candidates}, {'confirmed', 'candidate'})
        self.panel.script_mode.setCurrentIndex(self.panel.script_mode.findData('multiple'))
        self.assertEqual(self.panel.script_timeline.current_line_id(), lines[0]['id'])
        self.assertTrue(all(not self.panel.script_timeline._items[line['id']].isHidden()
                            for line in lines))
        self.panel.script_mode.setCurrentIndex(self.panel.script_mode.findData('review'))
        self.assertFalse(self.panel.script_tree.topLevelItem(0).child(1).isHidden())
        with patch.object(self.panel, '_clear_inline', wraps=self.panel._clear_inline) as clear:
            self.panel.script_search.setText('3')
            self.assertEqual(self.panel.script_timeline.current_line_id(), lines[2]['id'])
            self.assertEqual(self.panel.match_list.count(), 0)
            clear.assert_called_with('此处暂无PGM')
            self.panel.script_search.setText('根本没有的词')
            self.assertEqual(self.panel.script_timeline.current_line_id(), lines[2]['id'])
            self.assertEqual(self.panel._visible_candidates, [])
            self.assertTrue(all(not self.panel.script_timeline._items[line['id']].isHidden()
                                for line in lines))
            self.panel.script_search.setText('Please sit down')
            self.assertEqual(self.panel.script_timeline.current_line_id(), lines[1]['id'])
            self.assertEqual(self.panel.match_list.count(), 1)

    def test_incremental_refresh_preserves_script_take_and_ignores_advanced_filters(self):
        sessions, lines = self.seed_timeline_cases()
        self.panel.match_list.setCurrentRow(1)
        selected_id = self.panel._candidate()['id']
        self.panel.status_filter.setCurrentIndex(self.panel.status_filter.findData('confirmed'))
        self.panel.session_filter.setCurrentIndex(self.panel.session_filter.findData(sessions[0]['id']))
        self.panel.keyword.setText('不会匹配的高级筛选')
        self.assertEqual(self.panel.match_list.count(), 2)
        self.assertEqual(self.panel._candidate()['id'], selected_id)
        self.assertEqual(len(self.panel._advanced_candidates), 0)
        result = self.client.call('add_transcript', session_id=sessions[1]['id'],
            media_path='/NAS/Drama/追加_PGM.mov', segments=[
                {'start_seconds': 9, 'end_seconds': 11, 'text': lines[0]['text']}])
        self.client.call('replace_candidates', production_id=self.production['id'], candidates=[
            {'script_line_id': lines[0]['id'], 'transcript_id': result['transcript_ids'][0],
             'score': .99, 'reason': '新增素材'}])
        self.refresh()
        self.assertEqual(self.panel.script_timeline.current_line_id(), lines[0]['id'])
        self.assertEqual(self.panel._candidate()['id'], selected_id)
        self.assertEqual(self.panel._timeline_nodes[0]['count'], 3)
        self.assertEqual(self.panel.match_list.count(), 3)
        self.assertIn('【已核对】', self.panel.match_list.item(0).text())
        self.assertIn('【自动通过】', self.panel.match_list.item(1).text())
        self.assertEqual([row['id'] for row in self.panel.match_thumbnails.rows],
                         [row['id'] for row in self.panel._visible_candidates])

    def test_excluded_take_is_only_restored_from_explicit_advanced_view(self):
        self.seed_matches()
        self.panel.update_candidate('rejected')
        self.assertEqual(self.panel.match_list.count(), 1)
        self.panel.show_advanced(2)
        self.panel.status_filter.setCurrentIndex(self.panel.status_filter.findData('rejected'))
        self.assertEqual(self.panel.candidates.rowCount(), 1)
        self.assertEqual(self.panel.match_list.count(), 1)
        self.panel.candidates.selectRow(0)
        self.panel.update_candidate('candidate')
        self.assertEqual(self.panel.match_list.count(), 2)

    def test_main_refresh_can_initialize_unconfigured_library(self):
        workflow = Mock()
        self.panel.set_workflow(workflow)
        self.panel.client = None
        self.panel.refresh_button.click()
        workflow.refresh_catalogue.assert_called_once_with(self.panel)

    def test_unmatched_dialogue_can_be_manually_associated_with_selected_script(self):
        session = self.add_session('第一场')
        self.client.call('put_script', production_id=self.production['id'], title='台本', lines=[
            {'scene': '第一幕', 'speaker': 'Jack', 'text': '台本原句', 'order': 0},
            {'scene': '第一幕', 'speaker': 'Algy', 'text': '第二句台词', 'order': 1}])
        self.client.call('add_transcript', session_id=session['id'], media_path='/NAS/Drama/one.mov',
            segments=[{'start_seconds': 4, 'end_seconds': 6, 'text': '完全不同的临场发挥'}])
        self.refresh(session)
        self.assertEqual(self.panel.pending_list.count(), 1)
        self.panel.pending_list.setCurrentRow(0)
        self.panel.script_tree.setCurrentItem(self.panel.script_tree.topLevelItem(0).child(1))
        self.assertIsNotNone(self.panel.pending_list.currentItem())
        self.panel.associate_pending()
        self.assertEqual(self.panel.pending_list.count(), 0)
        self.assertEqual(self.panel._candidates[0]['script_text'], '第二句台词')
        self.assertEqual(self.panel._candidates[0]['status'], 'confirmed')

    def test_two_media_pools_exclude_every_placed_file_even_when_low_confidence(self):
        self.seed_matches()
        self.client.call('replace_candidates', production_id=self.production['id'], candidates=[
            dict(script_line_id=row['script_line_id'], transcript_id=row['transcript_id'],
                 score=.5, reason='需要核对的低分匹配') for row in self.panel._candidates])
        self.refresh()
        self.panel.show_advanced(2)
        self.assertEqual(self.panel.pending_mode.count(),2)
        self.assertEqual(self.panel.pending_list.count(),0)
        self.assertEqual(len(self.panel.story_timeline.index.takes),2)
        self.panel.pending_mode.setCurrentIndex(self.panel.pending_mode.findData('removed'))
        self.assertEqual(self.panel.pending_list.count(),0)
        self.panel.candidates.selectRow(0)
        self.panel.update_candidate('confirmed')
        self.assertEqual(self.panel.pending_list.count(),0)
        self.panel.candidates.selectRow(1)
        self.panel.update_candidate('rejected')
        self.panel.pending_mode.setCurrentIndex(self.panel.pending_mode.findData('unmatched'))
        self.assertEqual(self.panel.pending_list.count(),1)
        self.assertEqual(len(self.panel.story_timeline.index.takes),1)

    def test_inline_preview_reuses_one_player_and_releases_previous_source(self):
        class FakePreview(QWidget):
            def __init__(self, parent=None):
                super().__init__(parent)
                self.loads, self.replays, self.clears = [], 0, 0
            def load_media(self, *args, play=False): self.loads.append(args)
            def replay(self): self.replays += 1
            def clear(self): self.clears += 1
        first, second = Path(self.tmp.name) / 'first.mov', Path(self.tmp.name) / 'second.mov'
        first.touch(); second.touch()
        with patch('library_ui.InlinePreview', FakePreview):
            self.panel._load_inline(str(first), 1, 2)
            self.pump_until(lambda:self.panel.inline_preview is not None)
            player = self.panel.inline_preview
            self.panel._load_inline(str(first), 1, 2, replay=True)
            self.assertEqual(len(player.loads), 1)
            self.assertEqual(player.replays, 1)
            self.panel._load_inline(str(second), 3, 5)
            self.pump_until(lambda:player.loads[-1]==(str(second),3,5))
            self.assertIs(self.panel.inline_preview, player)
            self.assertEqual(player.loads[-1], (str(second), 3, 5))
            self.panel._clear_inline('选择片段')
            self.assertEqual(player.clears, 1)

    def test_missing_inline_media_and_empty_selection_show_nonblocking_message(self):
        with patch('library_ui.QMessageBox.warning') as warning:
            self.panel._load_inline('/missing/PGM.mov', 0, 5)
            self.pump_until(lambda:'未能预览' in self.panel.inline_placeholder.text())
            self.assertIn('未能预览', self.panel.inline_placeholder.text())
            self.panel.preview_current_inline()
            self.panel.preview_pending_inline()
            warning.assert_not_called()

    def test_existing_remote_client_without_selected_production_never_falls_back_to_local(self):
        self.client.url = 'http://192.168.1.20:8765'
        self.panel._workflow_production_snapshot = None
        with patch('socket.socket', side_effect=AssertionError('no network')) as local:
            client, production_id = self.panel.ensure_workflow_context()
        self.assertIs(client, self.client)
        self.assertEqual(production_id, self.production['id'])
        local.assert_not_called()

    def test_real_workflow_import_recognize_subtitle_and_match_reaches_main_view(self):
        import shutil
        import library_workflow
        from test_waveform_sync import speech_like, write_pgm, write_audio, RATE
        first, second = Path(self.tmp.name) / 'PGM_001.mov', Path(self.tmp.name) / 'PGM_002.mov'
        wav, proxy = Path(self.tmp.name) / 'ORIGINAL_MAIN.wav', Path(self.tmp.name) / 'A_proxy.mov'
        audio = speech_like(12, 20261011)
        write_audio(wav, audio, start_sample=19 * 3600 * RATE)
        write_pgm(first, audio[:10 * RATE], timecode='05:00:00:00')
        shutil.copy2(first, second)
        shutil.copy2(first, proxy)
        script = Path(self.tmp.name) / 'script.txt'
        script.write_text('第一幕\nJack：Good morning，今天你好吗？\n', encoding='utf-8')
        self.panel.set_workflow(library_workflow)
        self.panel.ensure_workflow_context = lambda *args: (self.client, self.production['id'])
        with patch('library_workflow.data_dir', return_value=Path(self.tmp.name) / 'Support'), \
             patch.object(self.panel, '_load_inline'), \
             patch('workflow_readiness.ensure_session_prepared'), \
             patch('dialogue_engine.transcribe_media', return_value=[{
                 'start_seconds': 1, 'end_seconds': 3, 'text': 'Good morning，今天你好吗？'}]) as asr:
            self.panel._delegate_ingest_materials([str(first), str(second)], '第三场拉通',
                [{'camera': 'A', 'path': str(proxy)}], [str(wav)])
            library_workflow.import_script(self.panel, str(script))
            self.panel.sync_recordings()
            self.panel.recognize_and_match()
            self.assertEqual(asr.call_count, 2)
            self.assertEqual({call.args[0] for call in asr.call_args_list}, {str(wav.resolve())})
            self.panel.candidates.selectRow(0)
            self.panel.update_candidate('confirmed')
            batch = self.panel.selected_session()
            b_proxy = Path(self.tmp.name) / 'B_proxy.mov'
            shutil.copy2(first, b_proxy)
            self.panel._delegate_ingest_materials([], batch['name'],
                [{'camera': 'B', 'path': str(b_proxy)}], [], session_id=batch['id'],
                expected_revision=batch['revision'])
            self.panel.sync_recordings()
            self.panel.recognize_and_match()
            self.assertEqual(asr.call_count, 2)
            self.assertEqual(self.panel.workflow_last_result['skipped'], 2)
            self.assertEqual(sum(row['status'] == 'confirmed' for row in self.panel._candidates), 2)
        self.assertEqual(self.panel.match_list.count(), 2)
        self.assertEqual(self.panel.pending_list.count(), 0)
        self.assertTrue(all('【自动通过】' in self.panel.match_list.item(index).text() for index in range(2)))
        self.assertIn('已核实 0 · 待核实 2', self.panel.match_count.text())
        self.assertIn('100%', self.panel.match_list.item(0).text())
        self.assertNotIn('84%', self.panel.match_list.item(0).text())
        sources = {row['media_path'] for row in self.panel._transcripts}
        self.assertEqual(sources, {str(first.resolve()), str(second.resolve())})
        for row in self.panel._transcripts:
            self.assertEqual(row['metadata']['asr_audio_source'], 'original_wav')
            subtitles = Path(row['metadata']['subtitle_path'])
            self.assertTrue(subtitles.is_file())
            self.assertIn('00:00:01,000 --> 00:00:03,000', subtitles.read_text())
        self.assertIn('已合板（波形匹配）', self.panel.match_detail.toPlainText())
        self.assertEqual(self.panel.match_sync_status.text(), '声音合板：已合板（波形匹配）')

    def test_helpers(self):
        self.assertEqual(format_time(3661.123), '01:01:01.123')
        self.assertEqual(media_paths({'pgm': '/a.mov', 'cut_xml': '/cut.xml',
            'library_media': [{'path': '/voice.wav'}, {'path': '/script.pdf'}],
            'pgm_overrides': {'original-pgm.mov': '/part2.mov'}}), ['/a.mov', '/voice.wav', '/part2.mov'])


class LibraryTaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_worker_is_cancellable_and_reports_on_gui_thread(self):
        loop = QEventLoop()
        captured, errors = [], []
        def operation(progress, cancelled):
            progress(10, '处理中')
            while not cancelled():
                QThread.msleep(5)
            raise ValueError('已取消台词处理。')
        from PyQt6.QtCore import QThread
        worker = LibraryTask(operation)
        worker.progress.connect(lambda *args: captured.append(args))
        worker.failed.connect(errors.append)
        worker.finished.connect(loop.quit)
        worker.start()
        QTimer.singleShot(20, worker.requestInterruption)
        QTimer.singleShot(3000, loop.quit)
        loop.exec()
        worker.requestInterruption()
        self.assertTrue(worker.wait(1000))
        self.assertTrue(captured)
        self.assertEqual(errors, ['已取消台词处理。'])


if __name__ == '__main__':
    unittest.main()
