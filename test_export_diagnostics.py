"""Coverage diagnostics explain blocked events without touching media/state."""
import copy
import json
import math
from pathlib import Path
import unittest
from unittest.mock import patch

from export_diagnostics import build_proxy_coverage_report, proxy_coverage_message, MAX_EXAMPLES


def blocked(camera='A',reason='source_gap',**values):
    event=dict(start=25,end=75,camera=camera,reason=reason,pgm_path='/recordings/PIX_001_118.mov',pgm_in=125,
               path='',reel='BL',source_in=0,source_out=50,record_in=90025,record_out=90075)
    event.update(values)
    return event


class ExportDiagnosticsTests(unittest.TestCase):
    def test_unconfirmed_camera_is_not_misreported_as_missing_proxy(self):
        event=blocked('?','sample_conflict')
        result=build_proxy_coverage_report('day04_通条',[event],dict(fps=25,sources=[]))
        self.assertEqual(result['counts'],{'camera_unconfirmed':1})
        self.assertEqual(result['reason_counts'],{'sample_conflict':1})
        text=proxy_coverage_message('day04_通条',[event],dict(fps=25,sources=[]))
        self.assertIn('机位未确认',text);self.assertNotIn('未导入该机位代理',text)
        self.assertNotIn('为 ? 机导入',text)

    def test_source_gap_distinguishes_no_import_from_incomplete_coverage(self):
        events=[blocked('A'),blocked('B')]
        config=dict(fps=25,sources=[dict(camera='B',proxy_path='/daily/B003C001.mov')])
        result=build_proxy_coverage_report('场次',events,config)
        self.assertEqual(result['counts'],dict(proxy_not_imported=1,source_gap=1))
        self.assertEqual([row['imported_proxy_count'] for row in result['details']],[0,1])
        self.assertIn('为 A 机导入',proxy_coverage_message('场次',events,config))

    def test_overlap_diagnosis_does_not_guess_candidate_proxy_filenames(self):
        config=dict(fps=25,sources=[dict(camera='C',path='/daily/duplicate_ONE.mov'),dict(camera='C',path='/daily/duplicate_TWO.mov')])
        text=proxy_coverage_message('场次',[blocked('C','source_overlap')],config)
        self.assertIn('代理时码重叠',text);self.assertIn('去重或核对',text)
        self.assertNotIn('duplicate_ONE',text);self.assertNotIn('duplicate_TWO',text)

    def test_locations_include_file_local_timecode_and_record_timecode_with_exclusive_out(self):
        result=build_proxy_coverage_report('day04_通条',[blocked()],dict(fps='25/1'))
        row=result['details'][0]
        self.assertEqual(row['pgm_filename'],'PIX_001_118.mov')
        self.assertEqual((row['pgm_in_frame'],row['pgm_out_frame_exclusive']),(125,175))
        self.assertEqual((row['pgm_in_timecode'],row['pgm_out_timecode_exclusive']),('00:00:05:00','00:00:07:00'))
        self.assertEqual((row['record_in_timecode'],row['record_out_timecode_exclusive']),('01:00:01:00','01:00:03:00'))
        self.assertIn('出点不含',row['location_text'])

    def test_invalid_or_fractional_fps_falls_back_to_frame_numbers(self):
        for fps in (None,0,-25,True,'broken',float('nan'),'30000/1001',29.97):
            with self.subTest(fps=fps):
                result=build_proxy_coverage_report('场次',[blocked()],dict(fps=fps))
                self.assertIsNone(result['fps']);self.assertIsNone(result['details'][0]['pgm_in_timecode'])
                text=proxy_coverage_message('场次',[blocked()],dict(fps=fps))
                self.assertIn('PGM内帧 125–175',text);self.assertIn('记录帧 90025–90075',text)
                self.assertIn('未导入该机位代理',text)
                json.dumps(result,allow_nan=False)

    def test_bad_frame_fields_do_not_mask_main_problem(self):
        event=blocked(pgm_in='not-a-frame',start={},end='end',record_in=None,record_out=math.inf)
        result=build_proxy_coverage_report('场次',[event],dict(fps=25))
        self.assertEqual(result['problem_count'],1)
        self.assertIn('位置未知',result['details'][0]['location_text'])
        self.assertIsNone(result['details'][0]['pgm_out_frame_exclusive'])
        json.dumps(result,allow_nan=False)

    def test_missing_pgm_position_falls_back_to_timeline_frames(self):
        text=proxy_coverage_message('场次',[blocked(pgm_in=None)],dict(fps=25))
        self.assertIn('时间线帧 25–75',text)

    def test_negative_or_out_of_day_frames_are_not_falsely_wrapped_into_timecode(self):
        event=blocked(pgm_in=-3,record_in=25*86400,record_out=25*86400+50)
        row=build_proxy_coverage_report('场次',[event],dict(fps=25))['details'][0]
        self.assertIsNone(row['pgm_in_timecode']);self.assertIsNone(row['record_in_timecode'])
        self.assertIn('PGM内帧 -3–47',row['location_text'])

    def test_custom_dynamic_camera_is_supported_and_unknown_configured_camera_is_distinct(self):
        config=dict(fps=25,camera_ids=['A','F'],sources=[dict(camera='F',path='/F.mov')])
        result=build_proxy_coverage_report('场次',[blocked('F'),blocked('C')],config)
        self.assertEqual(result['counts'],dict(camera_not_configured=1,source_gap=1))
        self.assertEqual([row['category'] for row in result['details']],['source_gap','camera_not_configured'])

    def test_existing_guard_predicate_is_preserved_exactly(self):
        events=[blocked(path='/good.mov',reel='SA'),blocked(path='/valid.mov',reel='SA',reason='source_overlap'),
                blocked(path='/bad.mov',reel='BL'),blocked(path='',reel='SA')]
        result=build_proxy_coverage_report('场次',events,dict(fps=25))
        self.assertEqual(result['problem_count'],2);self.assertEqual(result['event_count'],4)
        self.assertEqual([row['event_index'] for row in result['details']],[2,3])

    def test_full_report_keeps_all_details_but_message_has_bounded_examples(self):
        events=[blocked(start=n*25,end=(n+1)*25,pgm_in=n*25) for n in range(40)]
        result=build_proxy_coverage_report('场次',events,dict(fps=25))
        self.assertEqual(len(result['details']),40);self.assertEqual(sum(result['counts'].values()),40)
        text=proxy_coverage_message('场次',events,dict(fps=25))
        self.assertEqual(sum(line[:1].isdigit() for line in text.splitlines()),MAX_EXAMPLES)
        self.assertIn('其余 35 个区间',text)

    def test_original_event_fields_are_json_safe_and_preserved_without_mutation(self):
        event=blocked(extra=dict(fraction='1/25',path=Path('/offline/file.mov'),not_finite=math.nan))
        config=dict(fps=25,sources=[dict(camera='B',proxy_path='/B.mov')]);before=copy.deepcopy(config)
        original=dict(event)
        result=build_proxy_coverage_report('场次',[event],config)
        self.assertEqual(config,before);self.assertEqual(event,original)
        self.assertEqual(result['details'][0]['event']['extra']['path'],'/offline/file.mov')
        self.assertEqual(result['details'][0]['event']['extra']['not_finite'],'nan')
        json.dumps(result,allow_nan=False)

    def test_diagnostics_never_access_paths_probe_media_or_invoke_commands(self):
        with patch('builtins.open',side_effect=AssertionError('No file IO')),patch.object(Path,'stat',side_effect=AssertionError('No stat')),patch('subprocess.run',side_effect=AssertionError('No probe')):
            report=build_proxy_coverage_report('场次',[blocked()],dict(fps=25))
            message=proxy_coverage_message('场次',[blocked()],dict(fps=25))
        self.assertEqual(report['problem_count'],1);self.assertIn('尚不能导出',message)

    def test_empty_or_malformed_optional_settings_do_not_raise(self):
        for config in (None,dict(fps=[],camera_ids=None,sources='broken'),dict(fps=25,camera_count='bad',sources=[None,42,{}])):
            result=build_proxy_coverage_report(None,[None,blocked()],config)
            self.assertEqual(result['problem_count'],2);json.dumps(result,allow_nan=False)
        result=build_proxy_coverage_report('场次',[],None)
        self.assertEqual(result['status'],'ok');self.assertEqual(result['event_count'],0)
        self.assertIn('未发现阻断',proxy_coverage_message('场次',[],None))

    def test_windows_pgm_path_is_displayed_by_filename_without_resolution(self):
        event=blocked(pgm_path=r'Z:\\NEXIS\\day04\\PIX_001_118.mov')
        self.assertEqual(build_proxy_coverage_report('场次',[event],dict(fps=25))['details'][0]['pgm_filename'],'PIX_001_118.mov')

    def test_real_conform_events_yield_precise_missing_gap_overlap_and_camera_causes(self):
        from core import conform_events,Segment
        from cut_xml import locate_frame  # Import before the strict no-I/O assertion.
        block=dict(path='/PGM/PART_02.mov',start=0,end=300,frames=300,start_tc='10:00:00:00')
        cut=dict(duration=300,pgm_sources=[block])
        config=dict(fps=25,camera_ids=['A','B','C'],sources=[dict(camera='B',path='/B.mov'),dict(camera='C',path='/C1.mov'),dict(camera='C',path='/C2.mov')])
        sources=[dict(camera='B',start=900100,end=900150,path='/B.mov',reel='B'),dict(camera='C',start=900200,end=900250,path='/C1.mov',reel='C1'),dict(camera='C',start=900200,end=900250,path='/C2.mov',reel='C2')]
        segments=[Segment(0,100,'A','label_read'),Segment(100,200,'B','label_read'),Segment(200,250,'C','manual'),Segment(250,300,'?','unreadable_label')]
        with patch('cut_xml.timeline_info',return_value=cut),patch('cut_xml.locate_frame',side_effect=lambda _cut,p:(block,p)):
            events=conform_events(config,segments,sources)
        before=copy.deepcopy(events)
        report=build_proxy_coverage_report('day04_通条',events,config)
        self.assertEqual(report['problem_count'],4)
        self.assertEqual(report['counts'],dict(camera_unconfirmed=1,proxy_not_imported=1,source_gap=1,source_overlap=1))
        self.assertEqual([row['pgm_in_frame'] for row in report['details']],[0,150,200,250])
        self.assertEqual(events,before)


if __name__=='__main__':unittest.main()
