"""Automatic initial placement uses actual cached speech anchors, not clocks."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from automatic_alignment import build_alignment_proposals,prepare_alignment_evidence
from baseline_script_map import build_baseline_map,resolve_line,validate_map
from manual_timeline import inventory,reconcile_layout,set_script_range,take_id,apply_alignment_proposals


def fixture():
    texts=['The red velvet curtains cover the entrance.',
           'Please bring the blue notebook tomorrow morning.',
           '今晚的月光落在安静的花园里。',
           'This final scene belongs to another generation.']
    data=dict(sessions=[dict(id='base',name='基准',config={}),dict(id='alt',name='分镜',config={})],
        scripts=[dict(id='L'+str(number+1),script_id='script',order=number,text=text)
            for number,text in enumerate(texts)],transcripts=[],candidates=[])
    cut=dict(fps=25,duration=2500,pgm_sources=[dict(path='/base.mov',start=0,end=2500,frames=2500)])
    for sid,duration in (('base',2500),('alt',500)):
        next(row for row in data['sessions'] if row['id']==sid)['config']=dict(fps=25,
            pgm='/'+sid+'.mov',pgm_sources=[dict(path='/'+sid+'.mov',start=0,end=duration,frames=duration,fps=25)],
            pgm_timecodes={'/'+sid+'.mov':'01:00:00:00' if sid=='base' else '23:59:00:00'})
    for number,start in enumerate((8,20,45,70)):
        speech(data,'base'+str(number),'base',start,start+3,'L'+str(number+1))
    rows=inventory(data);layout=reconcile_layout({},rows,'base');identity=take_id('alt','/alt.mov')
    layout=set_script_range(layout,[identity],2,2,rows,'base')
    return data,rows,layout,cut,identity


def speech(data,identity,sid,start,end,line,*,candidate=True):
    text=next(row['text'] for row in data['scripts'] if row['id']==line)
    data['transcripts'].append(dict(id='T'+identity,session_id=sid,kind='pgm',media_path='/'+sid+'.mov',
        start_seconds=start,end_seconds=end,text=text))
    if candidate:data['candidates'].append(dict(id='C'+identity,transcript_id='T'+identity,
        script_line_id=line,status='candidate',score=.98,tags=['auto_dialogue']))


class AlignmentProposalTests(unittest.TestCase):
    def setUp(self):self.data,self.rows,self.layout,self.cut,self.identity=fixture()
    def build(self,**kwargs):return build_alignment_proposals(self.data,self.rows,self.layout,self.cut,**kwargs)

    def test_exact_cached_onsets_are_subtracted_and_never_cross_scene_timecodes(self):
        speech(self.data,'alt','alt',2,5,'L2');before=copy.deepcopy((self.data,self.rows,self.layout,self.cut))
        with patch('dialogue_engine.transcribe_media',side_effect=AssertionError('ASR')),patch('core.probe',side_effect=AssertionError('media probe')):
            result=self.build()
        row=result['proposals'][0]
        self.assertEqual(row['start_seconds'],18.)
        self.assertFalse(row['approximate']);self.assertFalse(row['alignment_reviewed'])
        self.assertEqual(row['alignment_origin'],'automatic')
        self.assertEqual((row['first_line'],row['last_line']),(2,2))
        self.assertEqual(row['source_anchor_seconds'],2.)
        self.assertEqual(row['duration_seconds'],20.)
        self.assertEqual((self.data,self.rows,self.layout,self.cut),before)
        placed=apply_alignment_proposals(self.layout,result['proposals'],self.rows,'base')
        self.assertEqual(placed['items'][self.identity]['start_seconds'],18.)
        self.assertFalse(placed['items'][self.identity]['alignment_reviewed'])

    def test_second_pgm_uses_real_concatenated_cut_coordinate_and_negative_head_keeps_complete_source(self):
        self.cut['pgm_sources']=[dict(path='/base.mov',start=0,end=1000,frames=1000),
            dict(path='/part2.mov',start=1000,end=2500,frames=1500)]
        base_source=next(row for row in self.data['transcripts'] if row['id']=='Tbase1')
        base_source.update(media_path='/part2.mov',start_seconds=2,end_seconds=5)
        self.data['sessions'][0]['config']['pgm_sources']=copy.deepcopy(self.cut['pgm_sources'])
        speech(self.data,'alt','alt',19,20,'L2')
        result=self.build();row=result['proposals'][0]
        self.assertEqual(row['baseline_anchor_frame'],1050)
        self.assertEqual(row['start_seconds'],23.)
        base_source.update(media_path='/base.mov',start_seconds=2,end_seconds=5)
        next(row for row in self.data['transcripts'] if row['id']=='Tbase0').update(start_seconds=0,end_seconds=1)
        result=self.build();row=result['proposals'][0]
        self.assertEqual(row['start_seconds'],-17.)
        self.assertEqual(row['duration_seconds'],20.)

    def test_missing_alternative_speech_is_explicit_low_confidence_whole_head_rough_placement(self):
        result=self.build();row=result['proposals'][0]
        self.assertEqual(row['start_seconds'],20.)
        self.assertTrue(row['approximate']);self.assertLessEqual(row['alignment_confidence'],.35)
        self.assertIsNone(row['source_anchor_seconds'])
        self.assertIn('缺少备选台词起点，按片头粗排',row['alignment_reason'])
        self.assertEqual(row['alignment_basis']['source_anchor_policy'],'whole_pgm_head')

    def test_no_baseline_dialogue_or_cannot_match_never_uses_line_fraction_of_duration(self):
        self.data['transcripts']=[];self.data['candidates']=[]
        result=self.build()
        self.assertEqual(result['proposals'],[])
        self.assertEqual(result['unresolved'][0]['code'],'baseline_anchor_missing')
        self.assertEqual(result['mapping']['segments'][0]['start_frame'],0)
        self.assertEqual(result['mapping']['segments'][-1]['end_frame'],2500)

    def test_long_leading_silence_and_relative_position_prior_do_not_move_unique_strong_dialogue(self):
        self.data['transcripts']=[row for row in self.data['transcripts'] if row['id']=='Tbase0']
        self.data['transcripts'][0].update(start_seconds=80,end_seconds=83)
        self.data['candidates']=[row for row in self.data['candidates'] if row['id']=='Cbase0']
        self.data['candidates'].append(dict(self.data['candidates'][0],id='weak-other-line',script_line_id='L4',score=.65))
        self.layout=set_script_range(self.layout,[self.identity],1,1,self.rows,'base')
        speech(self.data,'alt','alt',2,5,'L1')
        result=self.build();row=result['proposals'][0]
        self.assertEqual(row['start_seconds'],78.)
        self.assertFalse(row['approximate'])

    def test_existing_manual_and_legacy_coordinates_are_protected_even_when_green(self):
        for origin,reviewed in (('manual',False),('legacy',False),('automatic',True)):
            self.layout['items'][self.identity].update(start_seconds=7.123,lane=8,
                alignment_origin=origin,alignment_reviewed=reviewed)
            result=self.build(replace_automatic=True)
            self.assertEqual(result['proposals'],[])
            self.assertEqual(result['summary']['preserved_count'],1)
            self.assertEqual(self.layout['items'][self.identity]['start_seconds'],7.123)

    def test_automatic_blue_replacement_requires_explicit_option(self):
        self.layout['items'][self.identity].update(start_seconds=7,lane=8,alignment_origin='automatic',alignment_reviewed=False)
        self.assertEqual(self.build()['proposals'],[])
        self.assertEqual(self.build(replace_automatic=True)['proposals'][0]['start_seconds'],20.)

    def test_removed_hidden_blue_and_baseline_are_never_proposed(self):
        for change in (dict(organize_pool='removed'),dict(organize_pool='hidden'),dict(hidden=True),dict(organize_reviewed=False)):
            changed=copy.deepcopy(self.layout);changed['items'][self.identity].update(change)
            result=build_alignment_proposals(self.data,self.rows,changed,self.cut)
            self.assertEqual(result['proposals'],[])
            self.assertEqual(result['unresolved'],[])
        self.assertTrue(any(row['code']=='baseline' for row in self.build()['skipped']))

    def test_valid_saved_map_is_reused_and_manual_actual_anchor_is_respected(self):
        mapping=build_baseline_map(self.data,'base',self.cut)
        resolve_line(mapping,'L2').update(anchor_frame=525,manual=True,needs_review=False)
        with patch('automatic_alignment.prepare_alignment_evidence',side_effect=AssertionError('unnecessary text matching')):
            result=self.build(mapping=mapping)
        self.assertTrue(result['mapping_reused'])
        self.assertEqual(result['proposals'][0]['start_seconds'],21.)
        expected=hashlib.sha256(json.dumps(result['mapping'],sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(result['proposals'][0]['mapping_fingerprint'],expected)

    def test_human_confirmed_baseline_anchor_does_not_inherit_old_zero_machine_score(self):
        speech(self.data,'alt','alt',2,5,'L2')
        mapping=build_baseline_map(self.data,'base',self.cut)
        resolve_line(mapping,'L2').update(anchor_frame=525,manual=True,needs_review=False,confidence=0.)
        row=self.build(mapping=mapping)['proposals'][0]
        self.assertEqual(row['start_seconds'],19.)
        self.assertEqual(row['alignment_confidence'],.98)
        self.assertFalse(row['alignment_reviewed'])

    def test_repeated_source_line_with_separate_speaking_runs_stays_unresolved(self):
        speech(self.data,'alt1','alt',1,3,'L2');speech(self.data,'alt2','alt',12,14,'L2')
        result=self.build()
        self.assertEqual(result['proposals'],[])
        self.assertEqual(result['unresolved'][0]['code'],'source_repeated_dialogue')

    def long_paragraph(self):
        text=('Please bring the blue notebook tomorrow morning because we need to finish the rehearsal together. '
              'My brother already told every person in the theatre about the wonderful performance. '
              'After the final scene we shall all meet outside the garden entrance.')
        self.data['scripts'][1]['text']=text
        next(row for row in self.data['transcripts'] if row['id']=='Tbase1')['text']=text

    def test_distinct_partial_sentences_with_pauses_are_one_long_paragraph_not_false_repeated_takes(self):
        self.long_paragraph()
        for identity,first,end,text in [('part1',2,4,'Please bring the blue notebook tomorrow morning'),
                ('part2',11,13,'My brother already told every person in the theatre')]:
            speech(self.data,identity,'alt',first,end,'L2')
            self.data['transcripts'][-1]['text']=text
        result=self.build();self.assertEqual(result['unresolved'],[])
        row=result['proposals'][0]
        self.assertEqual(row['source_anchor_seconds'],2.)
        self.assertEqual(row['start_seconds'],18.)
        self.assertTrue(row['approximate'])
        self.assertEqual(len(row['alignment_basis']['source_evidence_ids']),2)

    def test_identical_partial_sentence_repeated_after_pause_still_requires_manual_placement(self):
        self.long_paragraph()
        for identity,first,end in [('part1',2,4),('part2',11,13)]:
            speech(self.data,identity,'alt',first,end,'L2')
            self.data['transcripts'][-1]['text']='Please bring the blue notebook tomorrow morning'
        result=self.build()
        self.assertEqual(result['proposals'],[])
        self.assertEqual(result['unresolved'][0]['code'],'source_repeated_dialogue')

    def test_other_dialogue_progress_then_returning_to_partial_line_is_not_merged_as_continuation(self):
        self.long_paragraph()
        speech(self.data,'part1','alt',2,4,'L2')
        self.data['transcripts'][-1]['text']='Please bring the blue notebook tomorrow morning'
        speech(self.data,'progress','alt',6,8,'L3')
        speech(self.data,'part2','alt',11,13,'L2')
        self.data['transcripts'][-1]['text']='My brother already told every person in the theatre'
        result=self.build()
        self.assertEqual(result['proposals'],[])
        self.assertEqual(result['unresolved'][0]['code'],'source_repeated_dialogue')

    def test_isolated_generic_short_phrase_is_not_a_second_occurrence_of_long_paragraph(self):
        self.long_paragraph()
        speech(self.data,'real','alt',2,4,'L2')
        speech(self.data,'generic','alt',18,19,'L2');self.data['transcripts'][-1]['text']='你是说'
        self.data['candidates'][-1]['score']=.67
        result=self.build()
        self.assertEqual(result['unresolved'],[])
        self.assertEqual(result['proposals'][0]['source_anchor_seconds'],2.)

    def test_repeated_baseline_occurrence_is_not_silently_chosen(self):
        mapping=build_baseline_map(self.data,'base',self.cut)
        resolve_line(mapping,'L2').update(needs_review=True,reason='同一台词重复出现，需核查')
        result=self.build(mapping=mapping)
        self.assertEqual(result['proposals'],[])
        self.assertEqual(result['unresolved'][0]['code'],'baseline_ambiguous')

    def test_weak_baseline_text_requires_coherent_neighbour_support_for_rough_placement(self):
        mapping=build_baseline_map(self.data,'base',self.cut)
        for row in mapping['segments']:
            if row['line_ids']:row.update(confidence=.55,needs_review=True,reason='台词匹配度未超过60%')
        supported=self.build(mapping=mapping)
        self.assertEqual(len(supported['proposals']),1)
        self.assertTrue(supported['proposals'][0]['approximate'])
        for row in mapping['segments']:
            if row['line_ids'] and row['line_ids']!=['L2']:row['anchor_frame']=None
        isolated=self.build(mapping=mapping)
        self.assertEqual(isolated['proposals'],[])

    def test_interior_merged_script_unit_is_only_roughly_placed_at_observed_unit_anchor(self):
        mapping=build_baseline_map(self.data,'base',self.cut)
        first=resolve_line(mapping,'L1');second=resolve_line(mapping,'L2')
        first.update(end_frame=second['end_frame'],line_ids=['L1','L2'])
        mapping['segments']=[row for row in mapping['segments'] if not first['start_frame']<row['start_frame']<first['end_frame']]
        validate_map(mapping,self.data,self.cut,'base')
        result=self.build(mapping=mapping);proposal=result['proposals'][0]
        self.assertTrue(proposal['approximate']);self.assertEqual(proposal['start_seconds'],8.)
        self.assertIn('合并区间内部',proposal['alignment_reason'])
        self.assertEqual(proposal['alignment_basis']['baseline_line'],1)

    def test_missing_first_line_can_use_actual_later_line_inside_covered_range_without_interpolation(self):
        self.data['candidates']=[row for row in self.data['candidates'] if row['script_line_id']!='L2']
        self.data['transcripts']=[row for row in self.data['transcripts'] if row['id']!='Tbase1']
        self.layout=set_script_range(self.layout,[self.identity],2,3,self.rows,'base')
        result=self.build();proposal=result['proposals'][0]
        self.assertEqual(proposal['start_seconds'],45.)
        self.assertTrue(proposal['approximate'])
        self.assertEqual(proposal['alignment_basis']['baseline_line'],3)

    def test_proposals_preserve_existing_track_ownership_and_do_not_overlap(self):
        other=dict(self.rows[-1],id='other',take_id='other',pgm_path='/other.mov',filename='other.mov')
        rows=self.rows+[other];layout=reconcile_layout(self.layout,rows,'base')
        layout['items']['other'].update(start_seconds=15,lane=1)
        result=build_alignment_proposals(self.data,rows,layout,self.cut)
        self.assertEqual(result['proposals'][0]['lane'],2)
        self.assertEqual(layout['items']['other']['lane'],1)

    def test_fps_mismatch_and_unknown_duration_are_diagnostics(self):
        for change in (dict(fps=30),dict(duration_seconds=None)):
            rows=copy.deepcopy(self.rows);next(row for row in rows if row['id']==self.identity).update(change)
            result=build_alignment_proposals(self.data,rows,self.layout,self.cut)
            self.assertEqual(result['proposals'],[])
            self.assertEqual(result['unresolved'][0]['code'],'media_metadata_missing')


class AlignmentEvidenceTests(unittest.TestCase):
    def setUp(self):self.data,self.rows,self.layout,self.cut,self.identity=fixture()

    def test_missing_baseline_candidates_get_stable_readonly_candidates_and_reusable_cache(self):
        self.data['candidates']=[];before=copy.deepcopy(self.data)
        one=build_alignment_proposals(self.data,self.rows,self.layout,self.cut)
        self.assertTrue(one['proposals'])
        rows=one['mapping']['automatic_evidence']['generated_candidates']
        self.assertTrue(rows);self.assertTrue(all(row['status']=='candidate' for row in rows))
        with patch('dialogue_engine.match_dialogue',side_effect=AssertionError('cached matching repeated')):
            two=build_alignment_proposals(self.data,self.rows,self.layout,self.cut,mapping=one['mapping'])
        self.assertTrue(two['mapping_reused'])
        self.assertFalse(any(row['code']=='mapping_cache_changed' for row in two['diagnostics']))
        self.assertEqual(one['mapping'],two['mapping'])
        self.assertEqual(one['proposals'],two['proposals'])
        self.assertEqual(self.data,before)

    def test_human_baseline_choice_takes_priority_over_new_text_candidate(self):
        self.data['candidates'][1].update(status='confirmed',tags=['manual_pgm_confirmed'],script_line_id='L3')
        prepared=prepare_alignment_evidence(self.data,'base')
        generated=[row for row in prepared['generated_candidates'] if row['transcript_id']=='Tbase1']
        self.assertEqual(generated,[])
        self.assertEqual(next(row for row in prepared['evidence_data']['candidates'] if row['id']=='Cbase1')['script_line_id'],'L3')

    def test_changed_script_or_speech_invalidates_generated_evidence_cache(self):
        self.data['candidates']=[]
        first=build_alignment_proposals(self.data,self.rows,self.layout,self.cut)
        self.data['transcripts'][1]['text']='Something completely new and unrelated.'
        second=prepare_alignment_evidence(self.data,'base',mapping=first['mapping'])
        self.assertFalse(second['reused'])
        self.assertNotEqual(first['mapping']['automatic_evidence']['input_fingerprint'],second['input_fingerprint'])

    def test_cancellation_never_changes_layout_or_catalogue(self):
        before=copy.deepcopy((self.data,self.layout))
        with self.assertRaisesRegex(ValueError,'取消'):
            build_alignment_proposals(self.data,self.rows,self.layout,self.cut,cancelled=lambda:True)
        self.assertEqual((self.data,self.layout),before)

    def test_organizer_visual_ranges_do_not_overlap_or_hide_baseline_speech(self):
        before=copy.deepcopy(self.data)
        visual=dict(id='old-organizer-visual',session_id='base',kind='pgm',media_path='/base.mov',
            start_seconds=0,end_seconds=100,text='[人工画面归类]',
            data=dict(source_kind='manual_visual',manual_script_line_ids=['L1','L2','L3','L4']))
        candidate=dict(id='old-organizer-candidate',session_id='base',media_path='/base.mov',
            transcript_id=visual['id'],script_line_id='L1',status='confirmed',score=1.,tags=['manual_pgm_confirmed'],
            start_seconds=0,end_seconds=100,data=copy.deepcopy(visual['data']))
        for dangling in (False,True):
            data=copy.deepcopy(before)
            if not dangling:data['transcripts'].append(copy.deepcopy(visual))
            data['candidates'].append(copy.deepcopy(candidate));snapshot=copy.deepcopy(data)
            result=build_alignment_proposals(data,self.rows,self.layout,self.cut)
            self.assertEqual(len(result['proposals']),1)
            self.assertEqual(result['proposals'][0]['start_seconds'],20.)
            self.assertEqual(result['summary']['baseline_asr_segment_count'],4)
            self.assertEqual(result['summary']['baseline_anchor_count'],4)
            self.assertNotIn(candidate['id'],{row['id'] for row in result['evidence_data']['candidates']})
            self.assertEqual(data,snapshot)

    def test_old_automatic_pair_does_not_suppress_fresh_high_confidence_text_match(self):
        for row in self.data['candidates']:row['score']=.31
        result=build_alignment_proposals(self.data,self.rows,self.layout,self.cut)
        self.assertEqual(len(result['proposals']),1)
        self.assertEqual(result['summary']['baseline_anchor_count'],4)
        self.assertTrue(any(row['script_line_id']=='L2' and row['score']>.6 for row in
            result['mapping']['automatic_evidence']['generated_candidates']))

    def test_multi_line_asr_spans_keep_observed_onsets_and_upgrade_old_zero_anchor_cache(self):
        from dialogue_spans import encode_span_tags
        self.data['transcripts']=[];self.data['candidates']=[]
        for number,(first,last) in enumerate(((1,2),(3,4))):
            identity='span'+str(number);text=' '.join(row['text'] for row in self.data['scripts'][first-1:last])
            self.data['transcripts'].append(dict(id=identity,session_id='base',kind='pgm',media_path='/base.mov',
                start_seconds=1+number*20,end_seconds=10+number*20,text=text))
            self.data['candidates'].append(dict(id='C'+identity,transcript_id=identity,script_line_id='L'+str(first),
                status='candidate',score=.99,tags=['auto_dialogue']+encode_span_tags(['L'+str(i) for i in range(first,last+1)])))
        broken=build_baseline_map(self.data,'base',self.cut)
        self.assertEqual(sum(row['anchor_frame'] is not None for row in broken['segments']),0)
        broken['automatic_evidence']=dict(version=1,input_fingerprint='old',generated_candidates=[])
        result=build_alignment_proposals(self.data,self.rows,self.layout,self.cut,mapping=broken)
        self.assertFalse(result['mapping_reused'])
        self.assertEqual(result['mapping']['automatic_alignment_version'],2)
        self.assertEqual(result['summary']['baseline_anchor_count'],2)
        self.assertEqual(len(result['proposals']),1)
        row=result['proposals'][0]
        self.assertTrue(row['approximate'])
        self.assertEqual(row['start_seconds'],1.)
        self.assertFalse(row['alignment_reviewed'])
        self.assertEqual(resolve_line(result['mapping'],'L2')['line_ids'],['L1','L2'])

    def test_zero_proposal_summary_distinguishes_preserved_positions_from_missing_baseline_anchors(self):
        self.layout['items'][self.identity].update(start_seconds=5,lane=1,alignment_origin='manual',alignment_reviewed=False)
        preserved=build_alignment_proposals(self.data,self.rows,self.layout,self.cut)
        self.assertEqual(preserved['summary']['eligible_count'],0)
        self.assertEqual(preserved['summary']['preserved_count'],1)
        self.assertEqual(preserved['summary']['skipped_reasons']['position_preserved'],1)
        self.layout['items'][self.identity]['start_seconds']=None
        self.data['transcripts']=[];self.data['candidates']=[]
        missing=build_alignment_proposals(self.data,self.rows,self.layout,self.cut)
        self.assertEqual(missing['summary']['eligible_count'],1)
        self.assertEqual(missing['summary']['baseline_anchor_count'],0)
        self.assertEqual(missing['summary']['unresolved_count'],1)
        self.assertIn('时间轴手动放置',missing['unresolved'][0]['reason'])


if __name__=='__main__':unittest.main()
