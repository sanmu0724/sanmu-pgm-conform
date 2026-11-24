import copy
import random
import unittest
from unittest.mock import patch
from PyQt6.QtWidgets import QApplication
from library_ui import LibraryPanel
from library_timeline import build_timeline_data
from pgm_intervals import build_pgm_intervals
from pgm_takes import PgmTakeIndex
from test_pgm_intervals import catalogue,add_take


class CatalogueUIPerformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        with patch('library_bridge.read_settings',return_value={}):
            self.panel=LibraryPanel(lambda:{},lambda _:None,auto_restore=False)

    def tearDown(self):
        self.panel.close();self.app.processEvents()

    def data(self,count=20):
        data=catalogue()
        for n in range(count):add_take(data,'s','file%04d'%n,(n%100,n%100+1))
        data['timeline_nodes']=build_timeline_data(data,whole_pgm=True)
        data['pgm_interval_nodes']=build_pgm_intervals(data)
        return data

    def test_same_snapshot_repeated_coverage_refresh_does_not_rebuild_or_lose_highlight(self):
        self.panel._set_data(self.data())
        selected=self.panel.story_timeline.index.takes[0].candidate_id
        self.panel._selected_story_take_id=selected
        with patch.object(self.panel.story_timeline,'set_nodes') as build:
            for _ in range(10):self.panel._refresh_coverage_axis()
        build.assert_not_called()
        self.assertEqual(self.panel.story_timeline.canvas.selected_candidate_id,selected)
        self.assertTrue(self.panel.story_timeline.canvas.highlighted_line_ids)

    def test_new_inventory_or_reference_rebuilds_coverage(self):
        data=self.data();self.panel._set_data(data)
        with patch.object(self.panel.story_timeline,'set_nodes',wraps=self.panel.story_timeline.set_nodes) as build:
            self.panel.reference_session_id='s';self.panel._refresh_coverage_axis()
            self.assertEqual(build.call_count,1)
            self.panel._pending_catalogue_cache=None;self.panel._refresh_coverage_axis()
            self.assertEqual(build.call_count,2)

    def test_sound_status_snapshot_refreshes_even_when_caller_reuses_lists(self):
        data=self.data(1);path=data['transcripts'][0]['media_path']
        config=data['sessions'][0]['config'];config['sound_unmatched_policy']='error'
        config['library_processing']={path:dict(stage='sync',status='failed',error='old failure')}
        self.panel._set_data(data)
        row=self.panel._pgm_inventory()[0][0]
        self.assertEqual(self.panel._audio_failure(row),'old failure')
        config['library_processing'][path]['error']='new failure'
        self.panel._set_data(data)
        self.assertEqual(self.panel._audio_failure(row),'new failure')
        config['sound_unmatched_policy']='pgm';self.panel._set_data(data)
        self.assertEqual(self.panel._audio_failure(row),'')

    def test_durations_build_pgm_header_index_once_per_recording(self):
        data=self.data(100)
        index=PgmTakeIndex(data,prepared=True)
        from library_correspondence import PGMInfoIndex
        with patch('library_correspondence.PGMInfoIndex',wraps=PGMInfoIndex) as factory:
            for row in data['candidates']:self.assertEqual(index.duration(row),20)
        self.assertEqual(factory.call_count,1)

    def test_overlap_index_keeps_long_and_partial_takes_in_exact_original_order(self):
        rng=random.Random(42);data=catalogue()
        for n in range(300):
            a=rng.randrange(100);b=rng.randrange(a,120)
            add_take(data,'s','file%04d'%n,range(a,b+1))
        self.panel._set_data(data)
        index=self.panel.story_timeline.index
        for _ in range(200):
            a=rng.randrange(120);b=rng.randrange(a,120)
            self.assertEqual(index.overlapping(a,b),[r for r in index.takes if r.first<=b and r.last>=a])
        self.assertEqual(index.overlapping(None,None),[])


if __name__=='__main__':unittest.main()
