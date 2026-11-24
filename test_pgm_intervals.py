import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import xml.etree.ElementTree as ET
from fractions import Fraction as F

from pgm_intervals import build_pgm_intervals
from pgm_takes import PgmTakeIndex
from library_timeline import build_timeline_data
from reference_timeline import build_reference_plan, assemble_reference_xml, ReferenceTimelineError
from test_reference_timeline import dataset, write_fixture


def catalogue():
    return dict(sessions=[], scripts=[dict(id='l'+str(n), script_id='play', order=n, text='台词'+str(n), scene='第一幕')
                                      for n in range(120)], transcripts=[], candidates=[])


def add_take(data, session, name, covered, manual=False, speech_start=2):
    row = next((row for row in data['sessions'] if row['id'] == session), None)
    if row is None:
        row = dict(id=session, name=session, config={'pgm_sources': []})
        data['sessions'].append(row)
    path = '/nas/'+session+'/'+name+'.mov'
    row['config']['pgm_sources'].append(dict(path=path, fps=25, frames=500, start_tc='20:12:00:00'))
    for number, line in enumerate(covered):
        identity = session+name+str(number)
        transcript = dict(id='t'+identity, session_id=session, media_path=path, kind='pgm',
                          start_seconds=0 if manual else speech_start+number,
                          end_seconds=20 if manual else speech_start+number+.8,
                          text='[人工画面归类]' if manual else '台词'+str(line))
        if manual:
            transcript['data'] = dict(source_kind='manual_visual', classification_only=True)
        data['transcripts'].append(transcript)
        data['candidates'].append(dict(id='c'+identity, transcript_id=transcript['id'],
                                      script_line_id='l'+str(line), score=.98, status='confirmed'))
    return path


class PgmIntervalTests(unittest.TestCase):
    def test_per_file_script_range_survives_missing_duration_metadata(self):
        data = catalogue()
        add_take(data, 'base', 'large', (10, 14))
        add_take(data, 'alt', 'short', (12,))
        for session in data['sessions']:
            for source in session['config']['pgm_sources']:
                source.pop('frames')
        before = copy.deepcopy(data)
        nodes = build_pgm_intervals(data)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]['covered_line_ids'], [f'l{n}' for n in range(10, 15)])
        clips = {row['session_id']: row for row in nodes[0]['clips']}
        self.assertEqual(clips['base']['covered_line_ids'], ['l10', 'l14'])
        self.assertEqual(clips['alt']['covered_line_ids'], ['l12'])
        self.assertEqual(data, before)

    def test_forty_files_are_forty_intervals_not_one_node_per_sentence(self):
        data = catalogue()
        for n in range(40):
            add_take(data, 'day01_分镜头', f'pix_{n:03}', (n*3, n*3+1, n*3+2))
        before = copy.deepcopy(data)
        nodes = build_pgm_intervals(data)
        self.assertEqual(len(nodes), 40)
        self.assertEqual(sum(row['pgm_count'] for row in nodes), 40)
        self.assertEqual(nodes[1]['covered_line_ids'], ['l3', 'l4', 'l5'])
        self.assertTrue(all((row['clips'][0]['start_seconds'], row['clips'][0]['end_seconds']) == (0,20) for row in nodes))
        self.assertEqual(data, before)

    def test_incremental_containment_keeps_largest_script_range_and_individual_first_anchor(self):
        data = catalogue()
        add_take(data, 'day01', 'big', (10, 12, 14))
        before = build_pgm_intervals(data)[0]
        add_take(data, 'day02', 'small', (12,13), speech_start=5)
        after = build_pgm_intervals(data)
        self.assertEqual(len(after), 1)
        node = after[0]
        self.assertEqual(node['id'], before['id'])
        self.assertEqual(node['covered_line_ids'], ['l'+str(n) for n in range(10,15)])
        self.assertEqual(node['pgm_count'], 2)
        self.assertEqual([(r['script_line_id'],r['dialogue_start_seconds']) for r in node['clips']], [('l10',2),('l12',5)])
        add_take(data, 'day03', 'larger', (9,15))
        node = build_pgm_intervals(data)[0]
        self.assertEqual((node['coverage_start'], node['coverage_end'], node['pgm_count']), (9,15,3))
        self.assertEqual(node['id'], before['id'])

    def test_partial_overlap_and_same_recording_repeated_takes_stay_distinct(self):
        data = catalogue()
        add_take(data, 'day01', 'one', (10,14))
        add_take(data, 'day01', 'repeat', (10,14))
        add_take(data, 'day02', 'partial', (13,16))
        add_take(data, 'day03', 'ambiguous', (11,12))
        nodes = build_pgm_intervals(data)
        self.assertEqual(len(nodes),4)
        self.assertEqual([r['pgm_count'] for r in nodes], [1]*4)

    def test_unlocated_silent_file_is_visible_and_manual_association_makes_full_take(self):
        data = catalogue()
        path = add_take(data, 'day02', 'reaction', ())
        node = build_pgm_intervals(data)[0]
        self.assertEqual((node['coverage_start'], node['pgm_count'], node['count']), (None,1,0))
        self.assertEqual(node['file_keys'], [['day02',path]])
        data['sessions'][0]['config']['pgm_sources'].clear()
        add_take(data, 'day02', 'reaction', (24,), manual=True)
        node = build_pgm_intervals(data)[0]
        self.assertEqual(node['covered_line_ids'], ['l24'])
        clip = node['clips'][0]
        self.assertTrue(clip['manual_anchor'])
        self.assertEqual((clip['start_seconds'],clip['end_seconds'],clip['dialogue_start_seconds']), (0,20,0))

    def test_same_filename_different_days_and_unmatched_first_speech(self):
        data = catalogue()
        add_take(data, 'day01', 'same', (1,2))
        add_take(data, 'day02', 'same', (80,81))
        data['transcripts'].append(dict(data['transcripts'][0], id='earlier', start_seconds=.2, end_seconds=.8, text='未匹配'))
        nodes = build_pgm_intervals(data)
        self.assertEqual(len(nodes),2)
        self.assertEqual(sum(row['count'] for row in nodes),2)
        first_clip = next(r for node in nodes for r in node['clips'] if r['session_id']=='day01')
        self.assertEqual(first_clip['pgm_match_status'], 'matched')
        self.assertFalse(first_clip['alignment_ready'])
        self.assertIsNone(PgmTakeIndex(data).representative(data['candidates'][0]))
        with self.assertRaisesRegex(ValueError,'第一句'):
            PgmTakeIndex(data).representative(data['candidates'][0], confirmed=True)

    def test_whole_script_browser_one_file_once_but_baseline_keeps_dialogue_anchors(self):
        data = catalogue()
        add_take(data,'base','continuous',(10,11,12))
        add_take(data,'alt','coverage',(10,11,12))
        data['reference_state'] = dict(reference_session_id='base')
        nodes = build_timeline_data(data, whole_pgm=True)
        self.assertEqual(sum(row['count'] for row in nodes),4)
        self.assertEqual([r['session_id'] for r in nodes[11]['clips']],['base'])
        self.assertTrue(nodes[10]['clips'][1]['whole_pgm'])
        self.assertEqual(nodes[10]['clips'][1]['end_seconds'],20)

    def test_manual_visual_export_uses_selected_sentence_as_anchor_without_inventing_speech(self):
        data, cuts = dataset()
        source = data['transcripts'][2]
        source.update(start_seconds=0,end_seconds=10,text='[人工画面归类]',data=dict(source_kind='manual_visual'))
        data['transcripts'] = [r for r in data['transcripts'] if r['id'] != 't-alt2']
        data['candidates'] = [r for r in data['candidates'] if r['id'] != 'alt2']
        data['candidates'][2]['script_line_id'] = 'line2'
        row = build_reference_plan(data,'base',['alt1'],cuts=cuts)['selections'][0]
        self.assertEqual((row['target_frame_in'],row['duration_frames'],row['dialogue_pgm_frame_in']),(200,250,0))
        self.assertEqual(source['text'],'[人工画面归类]')

    def test_imported_subtitles_on_registered_pgm_keep_first_sentence_anchor(self):
        data = catalogue()
        add_take(data,'day01','subtitles',(1,2))
        for row in data['transcripts']:
            row['kind']='imported'
        index=PgmTakeIndex(data)
        self.assertEqual(index.representative(data['candidates'][1],confirmed=True)['id'],data['candidates'][0]['id'])
        self.assertEqual(build_pgm_intervals(data)[0]['covered_line_ids'],['l1','l2'])

    def test_status_summary_counts_files_not_dialogue_segments(self):
        from pgm_intervals import file_status_rows, status_text
        data=catalogue()
        add_take(data,'day01','matched',(1,2,3))
        add_take(data,'day01','review',(4,5))
        for row in data['candidates']:
            if row['id'].startswith('cday01review'):
                row['status']='candidate'
                row['score']=.55
        add_take(data,'day01','silent',())
        add_take(data,'day02','manual',(10,),manual=True)
        rows=file_status_rows(build_pgm_intervals(data))
        self.assertEqual(len(rows),4)
        self.assertEqual(status_text(rows),'PGM：已匹配 2 · 待核实 1 · 未匹配 1')
        self.assertEqual(status_text(rows,'day01'),'PGM：已匹配 1 · 待核实 1 · 未匹配 1')
        self.assertEqual({Path(r['media_path']).stem:r['status'] for r in rows},
                         dict(matched='matched',review='review',silent='unmatched',manual='matched'))

    def test_negative_preroll_at_zero_clock_fails_without_trimming(self):
        data,cuts = dataset()
        plan=build_reference_plan(data,'base',['alt1'],cuts=cuts)
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            base=write_fixture(folder,'base',start_tc='00:00:00:00')
            alt=write_fixture(folder,'alt')
            with self.assertRaisesRegex(ReferenceTimelineError,'起始时码|00:00'):
                assemble_reference_xml(base,{'alt':alt},plan,folder/'out.fcpxml')

    def test_whole_file_score_counts_unmatched_speech_and_not_only_first_sentence(self):
        data = catalogue()
        path = add_take(data, 'day01', 'sparse', (1,))
        first = data['candidates'][0]
        first.update(status='candidate', score=.99)
        data['transcripts'].append(dict(id='unmatched-tail', session_id='day01', media_path=path,
            kind='pgm', start_seconds=4, end_seconds=18, text=' '.join(['unrelated']*30)))
        summary = PgmTakeIndex(data).file_summary(first)
        self.assertEqual(summary['match_status'], 'review')
        self.assertLess(summary['match_score'], .10)
        self.assertTrue(summary['alignment_ready'])
        self.assertEqual(build_pgm_intervals(data)[0]['file_statuses'][0]['status'], 'review')
        with self.assertRaisesRegex(ValueError, '整条 PGM'):
            PgmTakeIndex(data).selection_candidate(first)

    def test_overall_matching_is_independent_of_low_first_sentence_alignment(self):
        data = catalogue()
        add_take(data, 'day01', 'mixed', (1,2,3))
        for row in data['candidates']:
            row.update(status='candidate', score=.95)
        first=data['candidates'][0]
        first['score']=.5
        summary=PgmTakeIndex(data).file_summary(first)
        self.assertGreater(summary['match_score'], .60)
        self.assertEqual(summary['match_status'], 'matched')
        self.assertFalse(summary['alignment_ready'])
        node=build_pgm_intervals(data)[0]
        self.assertEqual(node['file_statuses'][0]['status'], 'matched')
        self.assertFalse(node['clips'][0]['alignment_ready'])
        with self.assertRaisesRegex(ValueError,'第一句'):
            PgmTakeIndex(data).selection_candidate(first)

    def test_missing_first_sentence_keeps_matched_file_playable_in_both_browsers(self):
        data=catalogue()
        path=add_take(data,'day01','later',(1,2,3,4))
        data['transcripts'].append(dict(id='unmatched-first',session_id='day01',media_path=path,
            kind='pgm',start_seconds=.1,end_seconds=.5,text='extra'))
        interval=build_pgm_intervals(data)[0]
        self.assertEqual(interval['covered_line_ids'],['l1','l2','l3','l4'])
        self.assertEqual(interval['file_statuses'][0]['status'],'matched')
        self.assertFalse(interval['clips'][0]['alignment_ready'])
        clips=[clip for node in build_timeline_data(data,whole_pgm=True) for clip in node['clips']]
        self.assertEqual(len(clips),1)
        self.assertEqual((clips[0]['start_seconds'],clips[0]['end_seconds']),(0,20))
        self.assertFalse(clips[0]['alignment_ready'])

    def test_strict_sixty_percent_threshold_and_manual_first_override(self):
        data=catalogue()
        add_take(data,'day01','boundary',(1,))
        row=data['candidates'][0]
        row.update(status='candidate',score=.60)
        summary=PgmTakeIndex(data).file_summary(row)
        self.assertEqual(summary['match_status'],'review')
        self.assertFalse(summary['alignment_ready'])
        row['score']=.60001
        summary=PgmTakeIndex(data).file_summary(row)
        self.assertEqual(summary['match_status'],'matched')
        self.assertTrue(summary['alignment_ready'])
        row.update(status='confirmed',score=.2)
        self.assertEqual(PgmTakeIndex(data).selection_candidate(row)['id'],row['id'])

    def test_ambiguous_best_positions_do_not_inflate_whole_file_score_or_alignment(self):
        data=catalogue()
        add_take(data,'day01','ambiguous',(1,))
        row=data['candidates'][0]
        row.update(status='candidate',score=.9)
        rival=dict(row,id='rival',script_line_id='l20')
        data['candidates'].append(rival)
        summary=PgmTakeIndex(data).file_summary(row)
        self.assertEqual(summary['match_score'],0)
        self.assertFalse(summary['alignment_ready'])
        self.assertIsNone(summary['first_candidate'])
        row['status']='confirmed'
        summary=PgmTakeIndex(data).file_summary(row)
        self.assertTrue(summary['alignment_ready'])
        self.assertEqual(summary['match_status'],'matched')
        self.assertEqual(summary['first_candidate']['id'],row['id'])

    def test_manual_whole_file_decision_overrides_unmatched_tail_without_changing_evidence(self):
        data=catalogue()
        path=add_take(data,'day01','manual-whole',(1,))
        first=data['candidates'][0]
        data['transcripts'].append(dict(id='unmatched',session_id='day01',media_path=path,
            kind='pgm',start_seconds=4,end_seconds=18,text=' '.join(['different']*40)))
        self.assertEqual(PgmTakeIndex(data).file_summary(first)['match_status'],'review')
        first['tags']=['manual_pgm_confirmed']
        before=copy.deepcopy(data)
        index=PgmTakeIndex(data)
        summary=index.file_summary(first)
        self.assertEqual(summary['match_status'],'matched')
        self.assertTrue(summary['manual_confirmed'])
        self.assertLess(summary['match_score'],.10)
        self.assertEqual(index.selection_candidate(first)['id'],first['id'])
        self.assertIs(summary,index.file_summary(first))
        self.assertEqual(data,before)

    def test_nonspoken_subtitle_annotation_does_not_become_first_dialogue(self):
        data=catalogue()
        path=add_take(data,'day01','note',(1,2))
        data['transcripts'].append(dict(id='opening-note',session_id='day01',media_path=path,
            kind='pgm',start_seconds=.1,end_seconds=.5,text='[掌声]'))
        first=data['candidates'][0]
        summary=PgmTakeIndex(data).file_summary(first)
        self.assertTrue(summary['alignment_ready'])
        self.assertEqual(summary['first_candidate']['id'],first['id'])
        self.assertEqual(summary['match_status'],'matched')

    def test_whole_file_browser_copies_only_displayed_files_at_catalogue_scale(self):
        import library_timeline
        data=catalogue()
        for n in range(50):
            add_take(data,'day01',f'pgm-{n:03}-',range(20),speech_start=0)
        add_take(data,'base','continuous',range(20),speech_start=0)
        data['reference_state']=dict(reference_session_id='base')
        before=copy.deepcopy(data)
        with patch('library_timeline._clip',wraps=library_timeline._clip) as copy_clip:
            nodes=build_timeline_data(data,whole_pgm=True)
        self.assertEqual(sum(node['count'] for node in nodes),70)
        self.assertEqual(copy_clip.call_count,70)
        self.assertEqual(data,before)

    def test_whole_file_prefilter_keeps_invalid_source_rows_out(self):
        data=catalogue()
        add_take(data,'day01','invalid-first',(1,2,3))
        data['transcripts'][0]['start_seconds']=-1
        data['transcripts'][1]['end_seconds']=data['transcripts'][1]['start_seconds']
        clips=[clip for node in build_timeline_data(data,whole_pgm=True) for clip in node['clips']]
        self.assertEqual(len(clips),1)
        self.assertEqual(clips[0]['script_line_id'],'l3')
        self.assertEqual(clips[0]['dialogue_start_seconds'],4)


if __name__ == '__main__':
    unittest.main()
