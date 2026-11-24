import copy
import unittest

from export_diagnostics import (blocking_proxy_events, build_proxy_coverage_report,
                                camera_coverage_summary, intentional_empty_camera_gap)


def config():
    return dict(fps=25, camera_ids=['A','B','C','D'], missing_camera_policy='available',
                missing_camera_preference='A', empty_camera_policy='gap')


def empty(**changes):
    return dict(dict(start=100,end=114,camera='C',requested_camera='C',reason='source_gap',
                     path='',reel='BL',pgm_path='/PGM/118B.mov',pgm_in=200,
                     available_cameras=[],empty_camera_gap=True),**changes)


class VariableCoverageGuardTests(unittest.TestCase):
    def test_explicit_known_zero_camera_range_keeps_duration_without_blocking(self):
        event=empty();before=copy.deepcopy(event)
        self.assertTrue(intentional_empty_camera_gap(event,config()))
        self.assertEqual(blocking_proxy_events([event],config()),[])
        self.assertEqual(build_proxy_coverage_report('通条',[event],config())['problem_count'],0)
        summary=camera_coverage_summary('通条',[event],config())
        self.assertEqual((summary['empty_count'],summary['empty_frames']),(1,14))
        self.assertEqual(summary['empty_ranges'][0]['pgm_out_frame_exclusive'],214)
        self.assertEqual(event,before)

    def test_unknown_unconfirmed_overlap_and_unconfigured_camera_never_bypass_guard(self):
        for changes in (dict(camera='Z'),dict(reason='identity_unconfirmed'),
                        dict(reason='source_overlap'),dict(empty_camera_gap=False),
                        dict(available_cameras=['A']),dict(available_cameras=None)):
            with self.subTest(changes=changes):
                event=empty(**changes)
                self.assertFalse(intentional_empty_camera_gap(event,config()))
                self.assertEqual(blocking_proxy_events([event],config()),[event])

    def test_both_policies_must_allow_explicit_empty_range(self):
        for changed in (dict(missing_camera_policy='strict'),dict(empty_camera_policy='error'),
                        dict(missing_camera_policy=None),dict(empty_camera_policy=None)):
            value={**config(),**changed}
            self.assertEqual(blocking_proxy_events([empty()],value),[empty()])

    def test_legacy_unresolved_without_explicit_coverage_evidence_remains_blocked(self):
        event=empty();event.pop('empty_camera_gap');event.pop('available_cameras')
        self.assertEqual(blocking_proxy_events([event],config()),[event])

    def test_mixed_report_lists_only_actual_blockers(self):
        known=empty();unknown=empty(camera='?',reason='unreadable_label')
        report=build_proxy_coverage_report('通条',[known,unknown],config())
        self.assertEqual(report['problem_count'],1)
        self.assertEqual(report['counts'],dict(camera_unconfirmed=1))
        self.assertEqual(report['details'][0]['camera'],'?')

    def test_fallback_summary_preserves_requested_and_selected_camera_and_local_position(self):
        event=dict(start=100,end=130,camera='A',requested_camera='B',
                   reason='camera_unavailable_fallback',fallback_reason='source_gap',
                   path='/A.mov',reel='A',pgm_path='/PGM/118A.mov',pgm_in=39526,
                   available_cameras=['A','C','D'])
        summary=camera_coverage_summary('通条',[event,empty()],config())
        self.assertEqual(blocking_proxy_events([event,empty()],config()),[])
        self.assertEqual((summary['fallback_count'],summary['fallback_frames']),(1,30))
        self.assertEqual(summary['frames_by_available_camera_count'],{0:14,3:30})
        row=summary['fallbacks'][0]
        self.assertEqual((row['requested_camera'],row['selected_camera']),('B','A'))
        self.assertEqual(row['pgm_in_timecode'],'00:26:21:01')

    def test_empty_range_does_not_claim_an_initial_video_angle(self):
        summary=camera_coverage_summary('通条',[empty()],config())
        self.assertIsNone(summary['empty_ranges'][0]['selected_camera'])
        self.assertEqual(summary['empty_ranges'][0]['available_cameras'],[])

    def test_explicit_zero_camera_unknown_is_not_confused_with_an_unknown_active_shot(self):
        event=empty(camera='?')
        self.assertEqual(blocking_proxy_events([event],config()),[])
        self.assertEqual(blocking_proxy_events([empty(camera='?',available_cameras=['A'])],config()),
                         [empty(camera='?',available_cameras=['A'])])

    def test_readiness_only_exempts_unknown_ranges_when_no_registered_camera_records(self):
        from workflow_readiness import _no_recorded_camera
        value={**config(), 'sources':[dict(camera='A',start_tc='01:00:01:00',frames=50)],
               'pgm_sources':[dict(start_tc='01:00:00:00',frames=100)]}
        predicate=_no_recorded_camera(value,None,list('ABCD'))
        self.assertTrue(predicate(0,25));self.assertTrue(predicate(75,100))
        self.assertFalse(predicate(24,26));self.assertFalse(predicate(0,100))
        value['sources'].append(dict(value['sources'][0]))  # Overlap also counts as recorded.
        self.assertFalse(_no_recorded_camera(value,None,list('ABCD'))(25,75))
        value['sources'][0].pop('start_tc')
        self.assertFalse(_no_recorded_camera(value,None,list('ABCD'))(0,25))

    def test_all_ambiguous_angles_do_not_masquerade_as_zero_recorded_cameras(self):
        from core import conform_events,Segment
        from unittest.mock import patch
        sources=[dict(camera='A',start=90000,end=90100,reel='A',path='/A1.mov'),
                 dict(camera='A',start=90000,end=90100,reel='A',path='/A2.mov')]
        cut=dict(duration=100,pgm_sources=[dict(path='/PGM.mov',start=0,end=100,
                                             frames=100,start_tc='01:00:00:00')])
        for camera in ('B','?'):
            with self.subTest(camera=camera),patch('cut_xml.timeline_info',return_value=cut):
                events=conform_events(config(),[Segment(0,100,camera,'label_read')],sources)
                self.assertNotIn('empty_camera_gap',events[0])
                self.assertEqual(blocking_proxy_events(events,config()),events)


if __name__=='__main__':unittest.main()
