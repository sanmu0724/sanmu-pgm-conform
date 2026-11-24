import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core import AnalysisError
from cut_xml import read_cut_xmls
from library_bridge import translate_config
from test_cut_xml_batch import make_cut_xml
from ui_common import Window as CommonWindow


class RecordingPortabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.media = [self.root / 'PGM1.mov', self.root / 'PGM2.mov']
        self.xmls = [self.root / '1.xml', self.root / '2.xml']
        self.infos = {}
        for index, (media, xml) in enumerate(zip(self.media, self.xmls)):
            media.write_bytes(b'media')
            tc = f'{12 + index:02d}:00:00:00'
            make_cut_xml(xml, media, tc=tc, cuts=[40])
            self.infos[str(media)] = dict(path=str(media), fps='25', frames=100, start_tc=tc, width=1920, height=1080)
        self.probe = patch('cut_xml.probe', side_effect=lambda path: copy.deepcopy(self.infos[str(path)]))
        self.probe.start()

    def tearDown(self):
        self.probe.stop()
        self.temp.cleanup()

    def test_all_xml_lookup_keys_populated_and_original_xml_files_unchanged(self):
        shared = '/NAS/Show'
        mappings = [{'shared': shared, 'local': str(self.root)}]
        for index, xml in enumerate(self.xmls):
            make_cut_xml(xml, Path(shared) / f'PGM{index+1}.mov', tc=f'{12+index:02d}:00:00:00')
        before = [hashlib.sha256(path.read_bytes()).hexdigest() for path in self.xmls]
        config = dict(cut_xml=shared + '/1.xml', cut_xml_paths=[shared + '/1.xml', shared + '/2.xml'],
                      pgm=shared + '/PGM1.mov', pgm_sources=[dict(path=shared + '/PGM1.mov', start=0),
                                                          dict(path=shared + '/PGM2.mov', start=100)])
        local = translate_config(config, mappings)
        self.assertEqual(local['cut_xml_paths'], list(map(str, self.xmls)))
        self.assertEqual(local['pgm_overrides'], {shared + '/PGM1.mov': str(self.media[0]), shared + '/PGM2.mov': str(self.media[1])})
        self.assertEqual([row['path'] for row in local['pgm_sources']], list(map(str, self.media)))
        self.assertEqual([hashlib.sha256(path.read_bytes()).hexdigest() for path in self.xmls], before)
        restored = translate_config(local, mappings, reverse=True)
        for key in config:
            self.assertEqual(restored[key], config[key])
        self.assertEqual(set(restored['pgm_overrides']), {shared + '/PGM1.mov', shared + '/PGM2.mov'})

    def test_explicit_overrides_win_over_automatic_mapping_in_every_xml(self):
        shared = '/NAS/Show'
        mappings = [{'shared': shared, 'local': str(self.root)}]
        for index, xml in enumerate(self.xmls):
            make_cut_xml(xml, Path(shared) / f'PGM{index+1}.mov', tc=f'{12+index:02d}:00:00:00')
        config = dict(cut_xml_paths=[shared + '/1.xml', shared + '/2.xml'],
                      pgm_overrides={shared + '/PGM2.mov': shared + '/Relocated2.mov'})
        local = translate_config(config, mappings)
        self.assertEqual(local['pgm_overrides'][shared + '/PGM2.mov'], str(self.root / 'Relocated2.mov'))
        self.assertEqual(local['pgm_overrides'][shared + '/PGM1.mov'], str(self.media[0]))

    def test_recording_metadata_source_ids_and_registered_sync_are_not_remapped(self):
        shared = '/NAS/Show'
        config = dict(cut_xml_paths=[shared + '/missing1.xml', shared + '/missing2.xml'],
            recording_day=2, recording_kind='coverage', recording_name='day02_分镜头',
            sources=[dict(path=shared + '/A.mov', source_id='stable-source', camera='A')],
            library_sync={shared + '/PGM1.mov': dict(signature='immutable', dependencies=[dict(path=shared + '/main.wav')])})
        local = translate_config(config, [{'shared': shared, 'local': str(self.root)}])
        self.assertEqual(local['recording_name'], 'day02_分镜头')
        self.assertEqual(local['sources'][0]['source_id'], 'stable-source')
        self.assertEqual(local['library_sync'], config['library_sync'])

    def stub(self):
        state = {'applied': None, 'segments': None, 'enabled': False, 'page': None}
        stub = SimpleNamespace(
            apply_config=lambda config: state.update(applied=config),
            populate_results=lambda rows: state.update(segments=rows),
            export_button=SimpleNamespace(setEnabled=lambda enabled: state.update(enabled=enabled)),
            tabs=SimpleNamespace(setCurrentWidget=lambda page: state.update(page=page)), results_page='results')
        return stub, state

    def saved_project(self):
        cut = read_cut_xmls(self.xmls)
        config = dict(cut_xml=str(self.xmls[0]), cut_xml_paths=list(map(str, self.xmls)), fps=25,
                      cut_xml_digest=cut['xml_digest'])
        data = dict(config=config, analysis_config=copy.deepcopy(config), segments=[
            dict(start=row['start'], end=row['end'], camera='B', reason='manual') for row in cut['shots']])
        path = self.root / 'saved.json'
        path.write_text(json.dumps(data))
        return path, data

    def test_restore_validates_and_recovers_all_xml_shots(self):
        path, data = self.saved_project()
        stub, state = self.stub()
        CommonWindow.restore_project(stub, path)
        self.assertEqual(len(state['segments']), 4)
        self.assertEqual(state['segments'][-1].end, 200)
        self.assertEqual(stub.analysis_config, data['analysis_config'])
        self.assertTrue(state['enabled'])
        self.assertEqual(state['page'], 'results')

    def test_restore_detects_changes_in_second_xml_not_only_first(self):
        path, _ = self.saved_project()
        self.xmls[1].write_bytes(self.xmls[1].read_bytes() + b'\n')
        stub, state = self.stub()
        with self.assertRaisesRegex(AnalysisError, 'XML已改变'):
            CommonWindow.restore_project(stub, path)
        self.assertIsNone(state['segments'])
        self.assertFalse(state['enabled'])

    def test_restore_rejects_first_xml_only_segment_geometry(self):
        path, data = self.saved_project()
        data['segments'] = data['segments'][:2]
        path.write_text(json.dumps(data))
        stub, state = self.stub()
        with self.assertRaisesRegex(AnalysisError, '切点与XML不一致'):
            CommonWindow.restore_project(stub, path)
        self.assertIsNone(state['segments'])


if __name__ == '__main__':
    unittest.main()
