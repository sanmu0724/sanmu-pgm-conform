"""Per-session 1–26 camera schemas survive OCR, conform and mixed-session XML."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import numpy as np

from camera_schema import cameras_for, active_label_cameras, DEFAULT_CAMERAS, MAX
from core import AnalysisError, Segment, build_sources, conform_events, export, validate_segments
from cut_xml import analyze_cut_xml
from ocr import LabelReader


def fixture(count, session='session', clock='02:00:00:00'):
    cameras = cameras_for({'camera_count': count})
    block = dict(path='/'+session+'.mov', start=0, end=250, frames=250,
                 start_tc=clock, width=100, height=50)
    cuts = dict(fps=25, duration=250, xml_digest='fixture', pgm_info=block, pgm_sources=[block],
                shots=[dict(start=0,end=250,source_in=0,source_out=250,pgm_path=block['path'])])
    config = dict(camera_count=count, fps=25, show_id=session, multicam=True,
                  timeline_start_tc='01:00:00:00', calibration_confirmed=True,
                  label_match_mode='prefix', infer_a_when_blank=False,
                  labels={camera:camera for camera in cameras},
                  label_rois={camera:[0,0,1,1] for camera in cameras},
                  pgm=block['path'], cut_xml='/'+session+'.xml', pgm_sources=[block],
                  sources=[dict(camera=camera,path='/'+session+'_'+camera+'.mov') for camera in cameras])
    def probe(path):
        return dict(path=str(path), fps='25', frames=250, start_tc=clock, width=100, height=50,
                    audio_channels=[dict(source_channel=1,sample_rate=48000,depth=24,pan=0,stereo_group=0)])
    return config,cuts,probe


class CameraSchemaTests(unittest.TestCase):
    def test_legacy_defaults_and_count_sizes(self):
        self.assertEqual(cameras_for({}), DEFAULT_CAMERAS)
        for count in (1,2,6,26):
            self.assertEqual(len(cameras_for({'camera_count': count})), count)
        self.assertEqual(MAX, 26)

    def test_explicit_ids_take_precedence_and_keep_order(self):
        self.assertEqual(cameras_for({'camera_ids':['F','A','Z'], 'camera_count':2}), ('F','A','Z'))
        self.assertEqual(active_label_cameras({'camera_ids':['F','A','Z'], 'infer_a_when_blank':True}), ('F','Z'))

    def test_invalid_schemas_rejected(self):
        for count in (0,27,-1,True,2.5,'6',None):
            with self.subTest(count=count), self.assertRaises(ValueError):
                cameras_for({'camera_count':count})
        for ids in ([],['A','A'],['a'],['AA'],['1'],['?'],'AB',None):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                cameras_for({'camera_ids':ids})


class DynamicCameraRecognitionTests(unittest.TestCase):
    def test_sixth_camera_prefix_and_full_clip_sampling(self):
        config,cut,_ = fixture(6)
        class OCR:
            def recognize(self,rgb):return [dict(text='F003C012',confidence=1)]
        with tempfile.TemporaryDirectory() as output, patch('cut_xml.timeline_info',return_value=cut), \
             patch('cut_xml.read_frame',return_value=np.zeros((50,100,3),np.uint8)):
            segments=analyze_cut_xml(config,output,backend=OCR())
        self.assertEqual(segments,[Segment(0,250,'F','label_read')])

    def test_single_unlabelled_a_needs_neither_roi_nor_ocr(self):
        config,cut,_ = fixture(1)
        config.update(infer_a_when_blank=True,labels={},label_rois={})
        with tempfile.TemporaryDirectory() as output, patch('cut_xml.timeline_info',return_value=cut), \
             patch('cut_xml.read_frame',return_value=np.zeros((50,100,3),np.uint8)), \
             patch('ocr.VisionOCR',side_effect=AssertionError('Single A should not initialize OCR')):
            segments=analyze_cut_xml(config,output)
        self.assertEqual(segments,[Segment(0,250,'A','A_inferred_blank')])

    def test_missing_active_roi_is_not_guessed_as_a(self):
        config,cut,_=fixture(2)
        config.update(infer_a_when_blank=True,labels={},label_rois={})
        with tempfile.TemporaryDirectory() as output, patch('cut_xml.timeline_info',return_value=cut):
            with self.assertRaisesRegex(AnalysisError,'B全部标号区域'):
                analyze_cut_xml(config,output,backend=object())

    def test_blank_non_a_single_camera_cannot_infer_a(self):
        class OCR:
            def recognize(self,rgb):return []
        reader=LabelReader(dict(camera_ids=['F'],infer_a_when_blank=True,label_match_mode='prefix'),OCR())
        self.assertEqual(reader.inspect(np.zeros((50,100,3),np.uint8),{'F':[0,0,1,1]})[0],'?')


class DynamicCameraExportTests(unittest.TestCase):
    def export_fixture(self, folder, count, session='session', clock='02:00:00:00', multicam=True):
        config,cut,probe=fixture(count,session,clock)
        config['multicam']=multicam
        camera=cameras_for(config)[-1]
        with patch('core.probe',side_effect=probe),patch('cut_xml.timeline_info',return_value=cut):
            _,path=export(config,[Segment(0,250,camera,'manual')],folder)
        return path,config,cut

    def test_one_two_six_and_legacy_four_have_exactly_configured_angles(self):
        with tempfile.TemporaryDirectory() as directory:
            for count in (1,2,6):
                path,config,_=self.export_fixture(Path(directory)/str(count),count)
                root=ET.parse(path)
                angles=root.findall('./resources/media/multicam/mc-angle')
                self.assertEqual([a.get('name') for a in angles],list(cameras_for(config)))
                self.assertTrue(all(a.find('clip/video') is not None for a in angles))
                self.assertEqual(len(root.findall('./resources/asset')),count)
                self.assertEqual(root.find('.//spine/mc-clip/mc-source').get('angleID'),
                                 'angle-'+cameras_for(config)[-1])
            config,cut,probe=fixture(4)
            config.pop('camera_count')
            with patch('core.probe',side_effect=probe),patch('cut_xml.timeline_info',return_value=cut):
                _,path=export(config,[Segment(0,250,'D','manual')],Path(directory)/'legacy')
            self.assertEqual([a.get('name') for a in ET.parse(path).findall('.//mc-angle')],list('ABCD'))

    def test_fcp7_video_audio_tracks_follow_camera_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            path,_,_=self.export_fixture(Path(directory),6,multicam=False)
            root=ET.parse(path)
            for kind in ('video','audio'):
                self.assertEqual([row.findtext('name') for row in root.findall(f'./sequence/media/{kind}/track')],list('ABCDEF'))
            self.assertEqual(root.findtext('./sequence/media/video/track[6]/clipitem/in'),'0')

    def test_unknown_or_unconfigured_cameras_are_not_conformed(self):
        config,cut,probe=fixture(2)
        validate_segments([Segment(0,250,'F','manual')],250)
        with patch('core.probe',side_effect=probe),patch('cut_xml.timeline_info',return_value=cut):
            sources=build_sources(config)
            with self.assertRaisesRegex(AnalysisError,'未设置的机位'):
                conform_events(config,[Segment(0,250,'F','manual')],sources)
            config['sources'].append(dict(camera='F',path='/F.mov'))
            with self.assertRaisesRegex(AnalysisError,'本场机位'):
                build_sources(config)

    def test_mixed_baseline_and_alternative_keep_separate_angle_sets_and_source_metadata(self):
        from test_reference_timeline import dataset
        from reference_timeline import build_reference_plan,assemble_reference_xml
        from fractions import Fraction
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            base,base_config,base_cut=self.export_fixture(folder/'base',2,'base','02:00:00:00')
            alt,alt_config,alt_cut=self.export_fixture(folder/'alt',6,'alt','18:00:00:00')
            data,cuts=dataset()
            data['sessions'][0]['config']=base_config
            data['sessions'][1]['config']=alt_config
            cuts.update(base=base_cut,alt=alt_cut)
            plan=build_reference_plan(data,'base',['alt1'],cuts=cuts)
            output=folder/'combined.fcpxml'
            assemble_reference_xml(base,{'alt':alt},plan,output,title='mixed-camera-counts')
            root=ET.parse(output)
            self.assertEqual(sorted(len(row.findall('mc-angle')) for row in root.findall('./resources/media/multicam')),[2,6])
            self.assertEqual({row.get('angleID') for row in root.findall('.//mc-source')},{'base-angle-B','alt1-angle-F'})
            angle_ids=[row.get('angleID') for row in root.findall('.//mc-angle')]
            self.assertEqual(len(angle_ids),len(set(angle_ids)))
            alt_assets=[row for row in root.findall('./resources/asset') if '/alt_' in row.get('src','')]
            self.assertEqual(len(alt_assets),6)
            self.assertTrue(all(Fraction(row.get('start')[:-1])==18*3600 for row in alt_assets))
            selection=plan['selections'][0]
            self.assertEqual(selection['target_frame_in']+selection['dialogue_pgm_frame_in'],selection['baseline_dialogue_frame_in'])

    def test_readiness_and_reference_prepare_use_session_camera_ids(self):
        from workflow_readiness import session_readiness
        from reference_workflow import ReferenceWorkflow
        from reference_timeline import ReferenceTimelineError
        config,cut,_=fixture(6)
        session=dict(id='session',name='day01_通条',config=config,
                     segments=[dict(start=0,end=250,camera='F',reason='manual')])
        with patch('library_bridge.read_settings',return_value={}):
            self.assertTrue(session_readiness(session)['camera_ready'])
            session['config']['camera_count']=2
            self.assertFalse(session_readiness(session)['camera_ready'])
        session['config']['camera_count']=6
        workflow=ReferenceWorkflow(object(),'production')
        with patch('reference_workflow.translate_config',side_effect=lambda c:c), \
             patch('cut_xml.timeline_info',return_value=cut), \
             patch('workflow_readiness.ensure_session_prepared'):
            prepared=workflow._prepare(session,dict(candidates=[]))
            self.assertEqual(cameras_for(prepared[0]),tuple('ABCDEF'))
            self.assertEqual(prepared[1][0].camera,'F')
            session['config']['camera_count']=2
            with self.assertRaises(ReferenceTimelineError):
                workflow._prepare(session,dict(candidates=[]))

    def test_custom_recording_type_confirmation_is_saved_for_reference_export(self):
        from reference_workflow import ReferenceWorkflow
        from reference_timeline import ReferenceTimelineError
        data=dict(sessions=[dict(id='base',name='自定义场次',config=dict(recording_kind='排练'))],
                  scripts=[],transcripts=[],candidates=[],reference_state=dict(revision=0,selected_clip_pairs={}))
        workflow=ReferenceWorkflow(object(),'production')
        def save(state, **changes):
            updated=dict(state,reference_session_id=changes.get('reference_session_id','base'),
                         baseline_export=changes['baseline'])
            data['reference_state']=updated
            return updated
        with patch.object(workflow,'read_all',side_effect=lambda:data),patch.object(workflow,'_save',side_effect=save):
            result=workflow.set_reference('base',legacy_continuous_confirmed=True)
            self.assertTrue(result['state']['baseline_export']['legacy_continuous_confirmed'])
            workflow._continuous(data['sessions'][0],result['state']['baseline_export']['legacy_continuous_confirmed'])
            data['reference_state']['baseline_export']={}
            result=workflow.set_reference('base',legacy_continuous_confirmed=True)
            self.assertTrue(result['state']['baseline_export']['legacy_continuous_confirmed'])
            data['sessions'][0]['config']['recording_kind']='coverage'
            with self.assertRaises(ReferenceTimelineError):
                workflow.set_reference('base',legacy_continuous_confirmed=True)


if __name__=='__main__':
    unittest.main()
