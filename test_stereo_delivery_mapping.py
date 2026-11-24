"""Generated stereo media stays explicit, scene scoped and outside staging."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from proxy_workflow import export_mapping
from reference_workflow import ReferenceWorkflow
from reference_timeline import ReferenceTimelineError


class StereoDeliveryMappingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name).resolve()
        self.media_root = self.folder/'配对立体声WAV'
        self.stereo = self.media_root/'content-key'/'new-stereo.wav'
        self.sidecar = self.stereo.with_name('stereo_merge.json')
        self.originals = [dict(role=role, path=str(self.folder/f'original_{role}.wav'),
            filename=f'original_{role}.wav', file_uid=f'original-uid-{role}', sha256=role*64,
            metadata=dict(sound_roll='ROLL_COMMON', scene='SCENE_COMMON')) for role in ('L','R')]

    def export_map(self, session):
        out = self.folder/session
        out.mkdir()
        sources = [dict(camera='A',reel='A001',path=str(self.folder/(session+'.mov')),start=100,
            frames=250,source_id=session,session_id=session,production_id='production')]
        wavs = [dict(path=str(self.stereo), stereo_merge_sidecar=str(self.sidecar),
            merged_from=copy.deepcopy(self.originals), channels=2, source_kind='wav')]
        events = [dict(path=str(self.stereo),source_kind='wav')]
        return export_mapping(sources,out,25,
            config=dict(library_context=dict(session_id=session,production_id='production'),
                        stereo_audio_cache_dir=str(self.media_root)),sound=(wavs,events))

    def test_map_calls_out_stable_required_media_and_preserves_original_identity(self):
        before=copy.deepcopy(self.originals)
        path=self.export_map('scene-one')
        data=json.loads(path.read_text())
        self.assertEqual(data['required_media_directories'],[str(self.media_root)])
        self.assertEqual(data['merged_audio_sources'],[dict(path=str(self.stereo),
            stereo_merge_sidecar=str(self.sidecar),merged_from=self.originals,
            session_id='scene-one',production_id='production')])
        self.assertEqual(data['guide_audio'][0]['path'],str(self.stereo))
        self.assertEqual(data['guide_audio'][0]['source_kind'],'wav')
        self.assertEqual(self.originals,before)
        self.assertNotIn('.reference-',json.dumps(data))

    def test_merge_keeps_same_stereo_file_in_two_scene_contexts_and_deduplicates_directory(self):
        first,second=self.export_map('scene-one'),self.export_map('scene-two')
        result=self.folder/'merged.json'
        ReferenceWorkflow._merge_maps([first,second,first],result)
        data=json.loads(result.read_text())
        self.assertEqual(data['required_media_directories'],[str(self.media_root)])
        self.assertEqual({row['session_id'] for row in data['merged_audio_sources']},{'scene-one','scene-two'})
        self.assertEqual(len(data['merged_audio_sources']),2)
        self.assertTrue(all(row['merged_from']==self.originals for row in data['merged_audio_sources']))

    def test_conflicting_same_scene_merge_identity_is_rejected(self):
        first=self.export_map('scene-one')
        second=self.folder/'conflicting.json'
        data=json.loads(first.read_text())
        data['merged_audio_sources'][0]['merged_from'][0]['file_uid']='wrong-original'
        second.write_text(json.dumps(data))
        with self.assertRaisesRegex(ReferenceTimelineError,'立体声 WAV 对应表冲突'):
            ReferenceWorkflow._merge_maps([first,second],self.folder/'bad.json')

    def test_existing_single_file_audio_does_not_invent_delivery_directory(self):
        out=self.folder/'single'
        out.mkdir()
        path=export_mapping([],out,25,config=dict(stereo_audio_cache_dir=str(self.media_root)),
            sound=([dict(path=str(self.folder/'already-stereo.wav'))],[]))
        data=json.loads(path.read_text())
        self.assertEqual(data['merged_audio_sources'],[])
        self.assertEqual(data['required_media_directories'],[])


if __name__=='__main__':
    unittest.main()
