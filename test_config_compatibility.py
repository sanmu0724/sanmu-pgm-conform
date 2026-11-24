"""Legacy editor settings retain unrepresented metadata without crossing scenes."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication

from core import AnalysisError
from pgm_app import Window
from project_state import _without_credentials


class ConfigCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.settings=patch('library_bridge.read_settings',return_value={})
        self.settings.start();self.addCleanup(self.settings.stop)
        self.window=Window();self.addCleanup(self.window.close)
        self.sound_info=dict(path='/qa/main.wav',samples=480000,sample_rate=48000,
            channels=2,depth=24,start=0,has_timecode=False)
        self.infos={self.sound_info['path']:self.sound_info}

    def legacy_config(self):
        return dict(mode='cut_xml',show_id='day01_通条',fps=25,
            timeline_width=1280,timeline_height=720,reels={'A':'CUSTOMA'},
            future_settings={'version':3,'values':['keep']},
            sources=[dict(camera='A',path='/qa/A.mov',proxy_path='/qa/A.mov',
                source_id='stable-source',start_tc='12:00:00:00',frames=250,
                original_path='/qa/A_original.mov',original_start_tc='12:00:00:00',
                metadata={'future':'keep'},original_width=4096)],
            sound_sources=[dict(path='/qa/main.wav',channel='main',
                metadata={'sound_roll':'S001','future':'keep'},sound_pair='pair',sound_role='L')])

    def load_draft(self,config):
        self.window.load_library_draft(config,media_prepared=True,sound_infos=self.infos)

    def cut(self):
        block=dict(path='/qa/PGM.mov',start=0,end=250,frames=250,start_tc='12:00:00:00',
            width=1280,height=720,xml_path='/qa/cuts.xml')
        return dict(xml_path='/qa/cuts.xml',xml_digest='saved-cut',fps=25,duration=250,
            pgm_info=block,pgm_sources=[block],shots=[dict(start=0,end=250)])

    def assert_preserved(self,config,result):
        for field in ('reels','future_settings','timeline_width','timeline_height'):
            self.assertEqual(result[field],config[field])
        self.assertEqual(result['sources'],config['sources'])
        self.assertEqual(result['sound_sources'],config['sound_sources'])

    def test_draft_retains_custom_size_source_identity_and_audio_metadata(self):
        config=self.legacy_config();before=copy.deepcopy(config)
        self.load_draft(config)
        self.assert_preserved(config,self.window.config())
        self.assertEqual(config,before)
        self.assertEqual(self.window.config()['camera_ids'],list('ABCD'))

    def test_complete_cut_config_retains_custom_size_and_unrepresented_fields(self):
        config=self.legacy_config()
        with patch.object(self.window,'refresh_pgm'):
            self.window.apply_config(config,prepared_cut=self.cut(),sound_infos=self.infos)
        self.assert_preserved(config,self.window.config())

    def test_calibration_edits_replace_visible_fields_only(self):
        config=self.legacy_config();self.load_draft(config)
        self.window.sources.item(0,2).setText('12:00:00:03')
        self.window.timeline_tc.setText('02:00:00:00')
        self.window.resolution.setCurrentText('1920×1080')
        result=self.window.config()
        self.assertEqual(result['sources'][0],dict(config['sources'][0],start_tc='12:00:00:03'))
        self.assertEqual((result['timeline_width'],result['timeline_height']),(1920,1080))
        self.assertEqual(result['timeline_start_tc'],'02:00:00:00')
        self.assertEqual(result['reels'],config['reels'])

    def test_old_json_save_and_restore_preserves_metadata(self):
        config=self.legacy_config();self.load_draft(config)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'saved.json'
            with patch('ui_common.QFileDialog.getSaveFileName',return_value=(str(path),'')):
                self.window.save_project()
            saved=json.loads(path.read_text(encoding='utf-8'))['config']
            self.assert_preserved(config,saved)
            self.load_draft(dict(show_id='another',sources=[],sound_sources=[]))
            with patch('cut_xml.timeline_info',return_value=self.cut()), \
                 patch('sound_sync.probe_wav',return_value=self.sound_info), \
                 patch.object(self.window,'refresh_pgm'):
                self.window.restore_project(path)
            self.assert_preserved(config,self.window.config())

    def test_scene_switch_and_new_project_clear_passthrough_and_library_state(self):
        first=self.legacy_config()
        first.update(library_sync={'/qa/PGM.mov':{'status':'matched'}},
            library_future={'old':'scene'},recording_day='2026-10-14')
        self.load_draft(first)
        second=dict(show_id='second',sources=[dict(camera='B',path='/qa/B.mov',
            source_id='second-source',frames=50)],sound_sources=[])
        self.load_draft(second);result=self.window.config()
        for field in ('reels','future_settings','library_sync','library_future','recording_day'):
            self.assertNotIn(field,result)
        self.assertNotIn('metadata',result['sources'][0])
        self.assertEqual(result['sources'][0]['source_id'],'second-source')
        self.assertEqual((result['timeline_width'],result['timeline_height']),(3840,2160))
        self.load_draft(dict(sources=[],sound_sources=[]))
        self.assertEqual(self.window.config()['sources'],[])

    def test_library_field_removed_from_current_state_is_not_revived(self):
        config=self.legacy_config();config['library_future']={'old':'scene'}
        self.load_draft(config);self.window.library_extra.pop('library_future')
        self.assertNotIn('library_future',self.window.config())

    def test_unknown_fields_follow_project_credential_filter(self):
        config=self.legacy_config()
        config.update(api_key='secret',future_settings={'token':'secret','keep':['value'],
            'nested':{'client-secret':'secret','future':'keep'}},library_connection={'url':'secret'})
        config['sources'][0]['metadata']['password']='secret'
        config['sound_sources'][0]['metadata']['access_token']='secret'
        self.load_draft(config);result=self.window.config()
        expected=_without_credentials(config)
        self.assert_preserved(expected,result)
        self.assertNotIn('api_key',result)
        self.assertNotIn('library_connection',result)
        # An old analysis payload can also carry settings outside config().
        self.window.analysis_config=copy.deepcopy(config)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'saved.json'
            with patch('ui_common.QFileDialog.getSaveFileName',return_value=(str(path),'')):
                self.window.save_project()
            self.assertNotIn('secret',path.read_text(encoding='utf-8'))
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['analysis_config'],expected)

    def test_invalid_sizes_raise_without_silently_using_a_shortcut(self):
        self.window.set_resolution('2048','1080')
        self.assertEqual(self.window.resolution.currentText(),'2048×1080')
        for width,height in ((0,720),(-1,720),(True,720),(1280,1.5),('1280.5',720),(1280,None)):
            with self.subTest(size=(width,height)),self.assertRaises(AnalysisError):
                self.window.set_resolution(width,height)
            self.assertEqual(self.window.resolution.currentText(),'2048×1080')


if __name__=='__main__':unittest.main()
