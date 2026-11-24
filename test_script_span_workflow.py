import copy
import json
import unittest

from pgm_takes import PgmTakeIndex
from pgm_editor_state import coverage_fingerprint


class ScriptSpanWorkflowTests(unittest.TestCase):
    def data(self):
        candidate=dict(id='c0',transcript_id='t0',script_line_id='l0',status='candidate',score=.99,
                       tags=['auto_script_span:'+json.dumps(['l0','l1'])])
        source=dict(id='t0',session_id='s0',media_id='m0',media_path='/source/PGM.mov',kind='pgm',
                    start_seconds=1.25,end_seconds=8.5,text='蓝色火车驶过寂静山谷红色风筝飘向遥远海岸')
        return dict(sessions=[dict(id='s0',config=dict(pgm='/source/PGM.mov'))],
                    scripts=[dict(id='l0',script_id='script0',order=0,text='蓝色火车驶过寂静山谷'),
                             dict(id='l1',script_id='script0',order=1,text='红色风筝飘向遥远海岸'),
                             dict(id='l2',script_id='script0',order=2,text='再见')],
                    transcripts=[source],candidates=[candidate])

    def test_one_utterance_covers_two_lines_and_keeps_original_first_anchor(self):
        data=self.data();before=copy.deepcopy(data)
        index=PgmTakeIndex(data,prepared=True)
        summary=index.file_summary(data['candidates'][0])
        self.assertEqual(summary['covered_line_ids'],['l0','l1'])
        self.assertEqual(summary['first_candidate']['script_line_id'],'l0')
        self.assertTrue(summary['alignment_ready'])
        self.assertEqual(summary['match_status'],'matched')
        self.assertEqual(index.source(summary['first_candidate'])['start_seconds'],1.25)
        self.assertEqual(len(index.rows[('s0','/source/PGM.mov')]),1)
        self.assertEqual(data,before)

    def test_bad_inactive_noncontiguous_or_cross_script_span_is_not_expanded(self):
        for ids,edit in [(['l1','l2'],None),(['l0','l2'],None),(['l0','missing'],None),
                         (['l0','l1'],('active',False)),(['l0','l1'],('script_id','other')),
                         (['l0','l1'],('scene','另一幕'))]:
            data=self.data();data['candidates'][0]['tags']=['auto_script_span:'+json.dumps(ids)]
            if edit:data['scripts'][1][edit[0]]=edit[1]
            with self.subTest(ids=ids,edit=edit):
                self.assertEqual(PgmTakeIndex(data,prepared=True).file_summary(data['candidates'][0])['covered_line_ids'],['l0'])

    def test_span_survives_project_json_round_trip_without_new_schema(self):
        data=json.loads(json.dumps(self.data()))
        self.assertEqual(PgmTakeIndex(data,prepared=True).file_summary(data['candidates'][0])['covered_line_ids'],['l0','l1'])

    def test_changed_span_invalidates_green_review_fingerprint(self):
        data=self.data();index=PgmTakeIndex(data,prepared=True)
        before=index.file_summary(data['candidates'][0])
        changed=copy.deepcopy(data);changed['candidates'][0]['tags']=['auto_script_span:'+json.dumps(['l0','l1','l2'])]
        after=PgmTakeIndex(changed,prepared=True).file_summary(changed['candidates'][0])
        self.assertNotEqual(coverage_fingerprint(before,index.source(before['first_candidate'])),
                            coverage_fingerprint(after,changed['transcripts'][0]))

    def test_span_does_not_expose_later_sentence_as_a_real_source_time_anchor(self):
        data=self.data();index=PgmTakeIndex(data,prepared=True)
        summary=index.file_summary(data['candidates'][0])
        self.assertEqual([row['script_line_id'] for row in summary['coverage_candidates']],['l0'])
        self.assertEqual(summary['first_candidate']['transcript_id'],'t0')
        self.assertNotIn('l1',index._by_transcript)

    def test_human_note_or_tag_retaining_old_auto_tag_wins_over_new_span(self):
        for metadata in (dict(note='导演已核实'),dict(tags=['auto_dialogue','director_keep'])):
            data=self.data();old=data['candidates'][0]
            old.update(status='confirmed',score=.8,tags=['auto_dialogue'],note='')
            old.update(metadata)
            data['candidates'].append(dict(id='c1',transcript_id='t0',script_line_id='l1',status='candidate',
                score=.99,tags=['auto_script_span:'+json.dumps(['l1','l2'])]))
            with self.subTest(metadata=metadata):
                summary=PgmTakeIndex(data,prepared=True).file_summary(old)
                self.assertEqual(summary['first_candidate']['id'],'c0')
                self.assertEqual(summary['covered_line_ids'],['l0'])


if __name__=='__main__':unittest.main()
