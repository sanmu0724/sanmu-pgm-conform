"""Workstation path portability and export session lifecycle regressions."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication
from core import AnalysisError
from library_bridge import normalize_path_mappings, remap_path, translate_config, write_settings, read_settings
from library_store import Store
from library_ui import media_paths


class BridgeTests(unittest.TestCase):
    def test_roundtrip_paths_do_not_alias_another_day_or_prefix(self):
        mappings = [{'shared': ' /NAS/Show/ ', 'local': '/Volumes/Edit/'}]
        normalized = normalize_path_mappings(mappings)
        source = '/NAS/Show/Day_02/A.mov'
        self.assertEqual(remap_path(remap_path(source, normalized), normalized, reverse=True), source)
        self.assertEqual(remap_path('/NAS/ShowBackup/A.mov', normalized), '/NAS/ShowBackup/A.mov')
        for rows in ([{'shared': '/NAS/One', 'local': '/Volumes/Edit'},
                      {'shared': '/NAS/Two', 'local': '/Volumes/Edit'}],
                     [{'shared': '/NAS/Show', 'local': '/Volumes/Edit'},
                      {'shared': '/NAS/Show/Day2', 'local': '/Volumes/Other'}]):
            with self.assertRaises(ValueError): normalize_path_mappings(rows)
        with self.assertRaises(ValueError): normalize_path_mappings([{'shared': '/', 'local': '/Volumes/Edit'}])

    def test_settings_normalize_and_never_replace_valid_file_with_ambiguous_map(self):
        with tempfile.TemporaryDirectory() as folder, patch('library_bridge.data_dir', return_value=Path(folder)):
            write_settings({'path_mappings': [{'shared': ' /NAS/Show/ ', 'local': '/Volumes/Edit/'}]})
            original = (Path(folder)/'workstation.json').read_bytes()
            self.assertEqual(read_settings()['path_mappings'][0]['shared'], '/NAS/Show')
            with self.assertRaises(ValueError):
                write_settings({'path_mappings': [{'shared': '/NAS/One', 'local': '/Volumes/Edit'},
                                                 {'shared': '/NAS/Two', 'local': '/Volumes/Edit'}]})
            self.assertEqual((Path(folder)/'workstation.json').read_bytes(), original)

    def test_moved_session_retains_xml_lookup_keys_and_all_references(self):
        c = {'pgm': '/NAS/Show/PGM1.mov',
             'pgm_overrides': {'/Old/PGM1.mov': '/NAS/Show/PGM1.mov', '/Old/PGM2.mov': '/NAS/Show/PGM2.mov'},
             'sources': [{'camera': 'A', 'path': '/NAS/Show/A.mov', 'source_id': 'stable'}],
             'sound_reference_overrides': {'/NAS/Show/PGM2.mov': {'path': '/NAS/Show/Ref.mov', 'start_seconds': 5}},
             'sound_sources': [{'path': '/NAS/Show/MAIN.wav'}]}
        mappings = [{'shared': '/NAS/Show', 'local': '/Volumes/Edit'}]
        translated = translate_config(c, mappings)
        self.assertEqual(set(translated['pgm_overrides']), set(c['pgm_overrides']))
        self.assertEqual(translated['sources'][0]['source_id'], 'stable')
        self.assertIn('/Volumes/Edit/A.mov', media_paths(translated))
        self.assertIn('/Volumes/Edit/PGM2.mov', media_paths(translated))
        # Retired references remain portable provenance, never new ASR inputs.
        self.assertNotIn('/Volumes/Edit/Ref.mov', media_paths(translated))
        self.assertEqual(translated['sound_reference_overrides']['/Volumes/Edit/PGM2.mov']['path'],
                         '/Volumes/Edit/Ref.mov')
        restored = translate_config(translated, mappings, reverse=True)
        for key in c: self.assertEqual(restored[key], c[key])


class SessionLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def test_loading_modified_cut_xml_does_not_replace_current_project(self):
        from pgm_app import Window
        w = Window()
        try:
            w.show_id.setCurrentText('KEEP')
            payload = {'id': 'session', 'production_id': 'production', 'revision': 1, 'name': '测试',
                'config': {'cut_xml': '/not-used.xml', 'cut_xml_digest': 'original'}, 'segments': []}
            with patch('library_bridge.translate_config', side_effect=copy.deepcopy), \
                 patch('cut_xml.timeline_info', return_value={'xml_digest': 'changed'}), \
                 patch.object(w, 'apply_config') as apply:
                with self.assertRaisesRegex(AnalysisError, '已改变'): w.load_library_session(payload)
                apply.assert_not_called()
            self.assertEqual(w.show_id.currentText(), 'KEEP')
        finally: w.close()

    def test_completed_export_saves_ocr_and_reports_conflict_without_overwrite(self):
        from pgm_app import Window
        class Client:
            def __init__(self, store): self.store = store
            def call(self, method, **params): return self.store.call(method, params, 'DIT')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); store = Store(root/'library.sqlite3'); client = Client(store); w = Window()
            try:
                production = client.call('create_production', name='导出测试')
                session = client.call('save_session', production_id=production['id'], name='第三场', config={}, segments=[])
                c = {'fps': 25, 'timeline_start_tc': '01:00:00:00', 'library_context': {
                    'production_id': production['id'], 'session_id': session['id'], 'revision': 1, 'session_name': '第三场'}}
                snapshots = {'config': c, 'segments': [{'start': 0, 'end': 50, 'camera': 'B'}]}
                (root/'reviewed_analysis.json').write_text(json.dumps(snapshots))
                target = root/'test.fcpxml'; target.write_text('<fcpxml/>')
                panel = w.library_panel; panel.client = client
                panel._run = lambda title, operation, done: done(operation(lambda *a: None, lambda: False))
                with patch('library_bridge.read_settings', return_value={}):
                    w.register_library_export(target, c)
                saved = client.call('get_session', session_id=session['id'])
                self.assertEqual(saved['revision'], 2)
                self.assertEqual(saved['segments'], snapshots['segments'])
                self.assertEqual(w.library_context['revision'], 2)
                client.call('save_session', production_id=production['id'], name='第三场', config={'note': '导演修改'},
                            segments=[], session_id=session['id'], expected_revision=2)
                with patch('library_bridge.read_settings', return_value={}), patch('pgm_app.QMessageBox.warning') as warning:
                    w.register_library_export(target, c)
                    warning.assert_called_once()
                self.assertEqual(client.call('get_session', session_id=session['id'])['config'], {'note': '导演修改'})
                self.assertTrue(client.call('list_artifacts', session_id=session['id']))
            finally:
                w.close(); store.close()


if __name__ == '__main__': unittest.main()
