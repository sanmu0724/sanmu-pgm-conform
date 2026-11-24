import copy
import json
import unittest
from unittest.mock import patch

from library_store import Store,LibraryError
from manual_timeline import (build_manual_plan,confirm_alignment,inventory,move_items,place_items,reconcile_layout,
                             set_script_range,take_id,validate_layout)


def fixture():
    data=dict(sessions=[dict(id='base',name='基准',config={}),dict(id='alt',name='第一场分镜',config={}),
                        dict(id='second',name='另一场',config={})],scripts=[],transcripts=[],candidates=[])
    cuts={}
    for session in data['sessions']:
        sid=session['id'];blocks=[dict(path='/'+sid+'1.mov',start=0,end=250,frames=250,fps=25),
                                dict(path='/'+sid+'2.mov',start=250,end=500,frames=250,fps=25)]
        cuts[sid]=dict(fps=25,duration=500,pgm_sources=copy.deepcopy(blocks))
        session['config']=dict(fps=25,pgm=blocks[0]['path'],pgm_sources=copy.deepcopy(blocks))
    return data,cuts


class ManualLayoutTests(unittest.TestCase):
    def setUp(self):
        self.data,self.cuts=fixture();self.inv=inventory(self.data,self.cuts)
        self.layout=reconcile_layout({},self.inv,'base')
        self.one=take_id('alt','/alt1.mov');self.two=take_id('alt','/alt2.mov')
        self.other=take_id('second','/second1.mov')

    def place(self,identity=None,start=5.,lane=1,layout=None,**kwargs):
        return place_items(layout or self.layout,[identity or self.one],start,lane,self.inv,'base',**kwargs)

    def test_inventory_ignores_script_and_asr_and_uses_scene_path_identity(self):
        self.data.update(scripts=[dict(id='nonsense')],transcripts=[dict(id='bad')],candidates=[dict(id='bad')])
        with patch('builtins.open',side_effect=AssertionError('media read')):
            rows=inventory(self.data,self.cuts)
        self.assertEqual(len(rows),6)
        self.assertEqual(rows[0]['duration_seconds'],10.)
        self.assertEqual(rows[1]['record_frame_in'],250)
        self.assertEqual(rows[1]['baseline_start_seconds'],10.)
        self.assertNotEqual(take_id('base','/same.mov'),take_id('alt','/same.mov'))
        self.assertNotEqual(take_id('alt','/first/same.mov'),take_id('alt','/second/same.mov'))

    def test_inventory_merges_cached_headers_without_duplicate_pgms(self):
        config=self.data['sessions'][1]['config']
        config['library_media']=[dict(kind='pgm',path='/alt1.mov',probe=dict(frames=250,fps='25/1')),
                                 dict(kind='proxy',path='/daily.mov',frames=250,fps=25)]
        self.assertEqual(len(inventory(self.data)),6)
        self.assertTrue(next(row for row in inventory(self.data) if row['id']==self.one)['ready'])

    def test_missing_metadata_stays_staged_and_cannot_be_placed(self):
        self.data['sessions'][1]['config']=dict(pgm='/alt1.mov')
        rows=inventory(self.data);layout=reconcile_layout({},rows,'base')
        row=next(row for row in rows if row['id']==self.one)
        self.assertFalse(row['ready']);self.assertIsNone(row['duration_seconds'])
        self.assertIsNone(layout['items'][self.one]['start_seconds'])
        with self.assertRaisesRegex(ValueError,'完整时长'):
            place_items(layout,[self.one],0,1,rows,'base')

    def test_legacy_probe_only_metadata_allows_manual_placement_without_inventing_record_clock(self):
        self.data['sessions'][1]['config']=dict(fps=25,pgm='/alt1.mov',library_media=[
            dict(kind='pgm',path='/alt1.mov',probe=dict(frames=250,fps='25/1'))])
        rows=inventory(self.data);source=next(row for row in rows if row['id']==self.one)
        self.assertTrue(source['ready'])
        self.assertFalse(source['cut_ready'])
        self.assertIsNone(source['record_frame_in'])
        self.assertIsNone(source['baseline_start_seconds'])
        self.assertIn('导出',source['metadata_warning'])
        layout=reconcile_layout({},rows,'base')
        layout=place_items(layout,[self.one],5,1,rows,'base')
        plan=build_manual_plan(self.data,'base',layout,self.cuts)
        self.assertEqual(plan['selections'][0]['record_frame_in'],0)
        self.assertEqual(plan['selections'][0]['target_frame_in'],125)

    def test_new_inventory_keeps_existing_positions_and_defaults_new_to_staged(self):
        layout=self.place();layout['items'][self.one]['organize_seconds']=21.1
        new_session=dict(id='new',name='后续',config=dict(pgm='/new.mov'))
        self.data['sessions'].append(new_session)
        result=reconcile_layout(layout,inventory(self.data),'base')
        self.assertEqual(result['items'][self.one]['start_seconds'],5.)
        self.assertEqual(result['items'][self.one]['organize_seconds'],21.1)
        self.assertIsNone(result['items'][take_id('new','/new.mov')]['start_seconds'])
        self.assertFalse(result['items'][take_id('new','/new.mov')]['hidden'])

    def test_changing_baseline_clears_only_target_placements(self):
        layout=self.place();layout['items'][self.one].update(organize_seconds=13,organize_lane=7,hidden=True)
        changed=reconcile_layout(layout,self.inv,'second')
        self.assertTrue(all(row['start_seconds'] is None for row in changed['items'].values()))
        self.assertEqual(changed['items'][self.one]['organize_seconds'],13)
        self.assertEqual(changed['items'][self.one]['organize_lane'],7)
        self.assertTrue(changed['items'][self.one]['hidden'])

    def test_added_inventory_uses_free_organizer_lane_without_moving_existing(self):
        layout=self.place()
        layout['items'][self.one]['organize_lane']=7
        self.data['sessions'].append(dict(id='new',config=dict(pgm='/new.mov')))
        rows=inventory(self.data)
        changed=reconcile_layout(layout,rows,'base')
        self.assertEqual(changed['items'][self.one]['organize_lane'],7)
        self.assertNotEqual(changed['items'][take_id('new','/new.mov')]['organize_lane'],7)
        validate_layout(changed,rows,'base')

    def test_script_coordinates_are_not_migrated(self):
        old=dict(version=99,items={self.one:dict(start_seconds=99)},line_start=55)
        result=reconcile_layout(old,self.inv,'base')
        self.assertIsNone(result['items'][self.one]['start_seconds'])
        self.assertNotIn('line_start',result)

    def test_baseline_take_cannot_be_duplicated_as_alternative(self):
        with self.assertRaisesRegex(ValueError,'非基准'):
            self.place(take_id('base','/base1.mov'))

    def test_collision_moves_whole_group_to_upper_lanes(self):
        layout=self.place(start=5,lane=1)
        moved=self.place(self.two,start=7,lane=1,layout=layout)
        self.assertEqual(moved['items'][self.one]['lane'],1)
        self.assertEqual(moved['items'][self.two]['lane'],2)
        with self.assertRaisesRegex(ValueError,'目标轨道'):
            self.place(self.two,start=7,lane=1,layout=layout,auto_lane=False)

    def test_touching_intervals_share_lane_and_group_preserves_offsets(self):
        layout=self.place(start=0,lane=1)
        layout=self.place(self.two,start=10,lane=1,layout=layout)
        moved=move_items(layout,[self.one,self.two],3,2,self.inv,'base')
        self.assertEqual([moved['items'][identity]['start_seconds'] for identity in (self.one,self.two)],[3,13])
        self.assertEqual([moved['items'][identity]['lane'] for identity in (self.one,self.two)],[3,3])

    def test_group_raise_is_uniform_not_individual_reordering(self):
        layout=self.place(start=5,lane=2)
        layout=self.place(self.two,start=6,lane=3,layout=layout)
        layout=self.place(self.other,start=30,lane=2,layout=layout)
        moved=move_items(layout,[self.one,self.two],25,0,self.inv,'base')
        self.assertEqual(moved['items'][self.two]['lane']-moved['items'][self.one]['lane'],1)
        self.assertEqual(moved['items'][self.two]['start_seconds']-moved['items'][self.one]['start_seconds'],1)
        self.assertEqual(moved['items'][self.one]['lane'],3)

    def test_staged_group_placement_does_not_use_script_or_legacy_organizer_offsets_as_seconds(self):
        self.layout['items'][self.one].update(organize_seconds=3,organize_lane=7)
        self.layout['items'][self.two].update(organize_seconds=6,organize_lane=8)
        placed=place_items(self.layout,[self.one,self.two],20,1,self.inv,'base')
        self.assertEqual(placed['items'][self.one]['start_seconds'],20)
        self.assertEqual(placed['items'][self.two]['start_seconds'],20)
        self.assertEqual(placed['items'][self.two]['lane'],2)
        for identity in (self.one,self.two):
            self.assertFalse(placed['items'][identity]['alignment_reviewed'])
            self.assertEqual(placed['items'][identity]['alignment_origin'],'manual')

    def test_editing_actual_positions_requires_new_confirmation_without_source_changes(self):
        source_before=copy.deepcopy((self.data,self.cuts,self.inv))
        placed=self.place(start=20,lane=1)
        self.assertFalse(placed['items'][self.one]['alignment_reviewed'])
        confirmed=confirm_alignment(placed,[self.one],self.inv,'base')
        self.assertTrue(confirmed['items'][self.one]['alignment_reviewed'])
        moved=move_items(confirmed,[self.one],-3,1,self.inv,'base')
        self.assertFalse(moved['items'][self.one]['alignment_reviewed'])
        plan=build_manual_plan(self.data,'base',moved,self.cuts)
        self.assertEqual((plan['selections'][0]['target_frame_in'],plan['selections'][0]['lane']),(425,2))
        self.assertEqual((plan['selections'][0]['pgm_frame_in'],plan['selections'][0]['duration_frames']),(0,250))
        self.assertEqual((self.data,self.cuts,self.inv),source_before)

    def test_organizer_move_does_not_change_actual_alignment(self):
        layout=self.place(start=20,lane=1)
        moved=move_items(layout,[self.one],0,2,self.inv,'base',mode='organize')
        self.assertEqual(moved['items'][self.one]['start_seconds'],20)
        self.assertEqual(moved['items'][self.one]['lane'],1)
        self.assertEqual(moved['items'][self.one]['organize_seconds'],0)
        self.assertEqual(moved['items'][self.one]['organize_lane'],3)
        with self.assertRaisesRegex(ValueError,'台本编号'):
            move_items(layout,[self.one],8,0,self.inv,'base',mode='organize')

    def test_hidden_and_staged_items_are_not_exported(self):
        layout=self.place();layout['items'][self.one]['hidden']=True
        plan=build_manual_plan(self.data,'base',layout,self.cuts)
        self.assertEqual(plan['selections'],[])
        self.assertEqual(plan['duration_frames'],500)

    def test_manual_plan_ignores_whole_play_clocks_and_preserves_full_source(self):
        self.cuts['base']['pgm_sources'][0]['start_tc']='01:00:00:00'
        self.cuts['alt']['pgm_sources'][0]['start_tc']='23:00:00:00'
        layout=self.place(start=1.019,lane=3);before=copy.deepcopy((self.data,self.cuts,layout))
        with patch('dialogue_engine.transcribe_media',side_effect=AssertionError('ASR')):
            plan=build_manual_plan(self.data,'base',layout,self.cuts)
        row=plan['selections'][0]
        self.assertTrue(plan['manual_layout'])
        self.assertEqual(row['candidate_id'],self.one)
        self.assertEqual((row['target_frame_in'],row['target_frame_out_exclusive']),(25,275))
        self.assertEqual((row['pgm_frame_in'],row['pgm_frame_out_exclusive']),(0,250))
        self.assertEqual((row['record_frame_in'],row['record_frame_out_exclusive']),(0,250))
        self.assertEqual(row['lane'],3)
        self.assertIsNone(row['script_line_id'])
        self.assertEqual((self.data,self.cuts,layout),before)

    def test_negative_whole_pgm_head_extends_plan_without_trimming_source(self):
        layout=self.place(start=-2.04)
        plan=build_manual_plan(self.data,'base',layout,self.cuts)
        self.assertEqual(plan['timeline_start_frame'],-51)
        self.assertEqual(plan['selections'][0]['duration_frames'],250)
        self.assertEqual(plan['duration_frames'],551)

    def test_invalid_layout_cannot_rewrite_source_identity_or_overlap(self):
        layout=self.place(start=5)
        for change in ('path','scene','nan','lane','overlap','extra'):
            altered=copy.deepcopy(layout)
            row=altered['items'][self.one]
            if change=='path':row['pgm_path']='/other.mov'
            if change=='scene':row['session_id']='other'
            if change=='nan':row['start_seconds']=float('nan')
            if change=='lane':row['lane']=True
            if change=='overlap':altered['items'][self.two].update(start_seconds=6,lane=1)
            if change=='extra':row['source_start']=99
            with self.subTest(change=change):
                if change=='extra':self.assertNotIn('source_start',validate_layout(altered,self.inv,'base')['items'][self.one])
                else:
                    with self.assertRaises(ValueError):validate_layout(altered,self.inv,'base')

    def test_frame_rate_mismatch_rejected_without_retiming(self):
        layout=self.place()
        self.cuts['alt']['fps']=30
        for block in self.cuts['alt']['pgm_sources']:block['fps']=30
        with self.assertRaisesRegex(ValueError,'帧率'):
            build_manual_plan(self.data,'base',layout,self.cuts)

    def test_manual_plan_interoperates_with_multicam_xml_and_original_wave_resources(self):
        import tempfile
        from pathlib import Path
        import xml.etree.ElementTree as ET
        from test_reference_timeline import dataset,write_fixture,time
        from reference_timeline import assemble_reference_xml
        from fractions import Fraction as F
        data,cuts=dataset();rows=inventory(data,cuts);layout=reconcile_layout({},rows,'base')
        identity=take_id('alt','/alt.mov')
        layout=place_items(layout,[identity],2,2,rows,'base')
        plan=build_manual_plan(data,'base',layout,cuts)
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory);base=write_fixture(folder,'base');alt=write_fixture(folder,'alt',start_tc='22:00:00:00')
            output=folder/'manual.fcpxml'
            report=assemble_reference_xml(base,{'alt':alt},plan,output)
            root=ET.parse(output).getroot()
            sequence=root.find('./library/event/project/sequence')
            self.assertEqual(time(sequence,'tcStart'),0)
            self.assertFalse(sequence.findall('.//clip'))
            requested=root.find('./library/event/project/metadata/md[@key="com.pgm.requestedTimelineStart"]')
            self.assertEqual(F(requested.get('value')[:-1]),3600)
            pieces=sequence.findall('spine/mc-clip[@lane="2"]')
            self.assertEqual(len(pieces),3)
            self.assertEqual([piece.find('mc-source').get('angleID') for piece in pieces],
                             ['alt1-angle-A','alt1-angle-B','alt1-angle-C'])
            self.assertEqual([time(piece,'offset') for piece in pieces],[F(2),F(5),F(8)])
            self.assertEqual(sum(time(piece,'duration') for piece in pieces),F(10))
            audio=[row for row in sequence.findall('spine/asset-clip')
                   if row.get('srcEnable')=='audio' and row.get('ref','').startswith('alt1-')]
            self.assertEqual(len(audio),4)
            self.assertEqual([time(row,'start') for row in audio],
                             [F(100)+F(3,48000),F(103)+F(3,48000),F(200)+F(7,48000),F(202)+F(7,48000)])
            self.assertEqual([time(row,'offset') for row in audio],[F(2),F(5),F(6),F(8)])
            self.assertEqual(sum(time(row,'duration') for row in audio),F(10))
            # The alternative occupies V3, so its sound is A3 with an empty A2.
            self.assertTrue(all(row.get('audioRole')=='dialogue.pgm-track-3' and row.get('lane')=='-3' for row in audio))
            group=lambda row:row.find('./metadata/md[@key="com.pgm.linkGroupID"]').get('value')
            self.assertEqual(len({group(row) for row in pieces}),3)
            self.assertTrue(all(group(row) in {group(piece) for piece in pieces} for row in audio))
            from resolve_import_helper import build_resolve_manifest
            helper=build_resolve_manifest(output)
            self.assertEqual((helper['import_start'],helper['timeline_start'],helper['set_start_timecode']),
                             (0,90000,'01:00:00:00'))
            alternate_groups=[row for row in helper['groups'] if row['video'] and row['video']['track_index']==3]
            self.assertEqual(len(alternate_groups),3)
            self.assertEqual([row['video']['record_start'] for row in alternate_groups],[90050,90125,90200])
            self.assertTrue(all(row['audio'] and all(item['track_index']==3 for item in row['audio']) for row in alternate_groups))
            sounds=[item for item in root.findall('./resources/asset') if item.get('hasAudio')=='1']
            self.assertTrue(sounds)
            for sound in sounds:
                md={row.get('key'):row.get('value') for row in sound.findall('./metadata/md')}
                self.assertIn('SOUND_',md['com.apple.proapps.studio.reel'])
                self.assertTrue(md['com.pgm.originalAudioFilename'].endswith('.wav'))
            self.assertEqual(report['selected_count'],1)



class ManualPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(':memory:');self.production=self.store.create_production('人工排列')['id']
        self.data,self.cuts=fixture();self.sessions={}
        for row in self.data['sessions']:
            self.sessions[row['id']]=self.store.save_session(self.production,row['name'],row['config'])
        self.base=self.sessions['base']['id'];self.alt=self.sessions['alt']['id']
        self.state=self.store.update_reference_settings(self.production,self.base)
        self.inv=self.inventory()
        self.layout=reconcile_layout({},self.inv,self.base)
        self.one=take_id(self.alt,'/alt1.mov')
        self.layout=place_items(self.layout,[self.one],5,2,self.inv,self.base)

    def tearDown(self):self.store.close()
    def inventory(self):return inventory(dict(sessions=self.store.list_sessions(self.production)))
    def save(self,layout=None,revision=None):
        return self.store.call('update_manual_timeline',dict(production_id=self.production,
            manual_timeline=layout or self.layout,expected_revision=self.state['revision'] if revision is None else revision))

    def test_cas_and_roundtrip_preserve_layout(self):
        saved=self.save()
        self.assertEqual(saved['manual_timeline'],self.layout)
        self.assertEqual(self.store.get_reference_settings(self.production)['manual_timeline'],self.layout)
        with self.assertRaises(LibraryError) as error:self.save()
        self.assertEqual(error.exception.code,'conflict')

    def test_baseline_switch_clears_targets_but_preserves_organizer(self):
        self.layout['items'][self.one].update(organize_seconds=12,organize_lane=8)
        saved=self.save()
        changed=self.store.update_reference_settings(self.production,self.sessions['second']['id'],expected_revision=saved['revision'])
        item=changed['manual_timeline']['items'][self.one]
        self.assertIsNone(item['start_seconds'])
        self.assertEqual((item['organize_seconds'],item['organize_lane']),(12,8))

    def test_other_reference_settings_writer_preserves_manual_data(self):
        saved=self.save()
        newer=self.store.update_reference_settings(self.production,self.base,baseline_export=dict(valid=True),expected_revision=saved['revision'])
        self.assertEqual(newer['manual_timeline'],saved['manual_timeline'])
        self.assertTrue(newer['baseline_export']['valid'])

    def test_new_inventory_is_added_without_moving_old_targets(self):
        self.state=self.save()
        self.store.save_session(self.production,'后续',dict(pgm='/later.mov'))
        saved=self.save(revision=self.state['revision'])
        self.assertEqual(saved['manual_timeline']['items'][self.one]['start_seconds'],5.)
        self.assertEqual(len(saved['manual_timeline']['items']),7)

    def test_incorrect_baseline_cannot_replace_saved_layout(self):
        changed=copy.deepcopy(self.layout);changed['baseline_session_id']='unrelated'
        with self.assertRaises(LibraryError):self.save(changed)
        self.assertEqual(self.store.get_reference_settings(self.production)['manual_timeline'],{})

    def _matched_source(self):
        self.store.put_script(self.production,'台本',[dict(text='测试台词第一句'),dict(text='第二句')])
        line=self.store.list_script(self.production)[0]
        transcript=self.store.add_transcript(self.alt,'/alt1.mov',[dict(start_seconds=1,end_seconds=3,text='测试台词第一句')])['segments'][0]
        self.store.replace_candidates(self.production,[dict(transcript_id=transcript['id'],script_line_id=line['id'],score=.95)])
        return line,transcript

    def test_clear_old_script_matches_preserves_manual_placement(self):
        self._matched_source();saved=self.save()
        rows=[row for row in self.store.list_candidates(self.production) if row['media_path']=='/alt1.mov']
        session=self.store.get_session(self.alt)
        result=self.store.clear_pgm_matches(self.production,self.alt,'/alt1.mov',
            expected_session_revision=session['revision'],expected_candidate_revisions={row['id']:row['revision'] for row in rows},
            expected_reference_revision=saved['revision'])
        self.assertEqual(result['reference_state']['manual_timeline'],saved['manual_timeline'])

    def test_old_manual_script_range_writer_preserves_manual_placement(self):
        line,_transcript=self._matched_source();saved=self.save()
        rows=[row for row in self.store.list_candidates(self.production) if row['media_path']=='/alt1.mov']
        session=self.store.get_session(self.alt)
        result=self.store.associate_pgm_script_range(self.production,self.alt,'/alt1.mov',1,3,[line['id']],
            expected_session_revision=session['revision'],expected_candidate_revisions={row['id']:row['revision'] for row in rows},
            expected_reference_revision=saved['revision'])
        self.assertEqual(result['reference_state']['manual_timeline'],saved['manual_timeline'])

    def test_refreshing_old_machine_matches_preserves_manual_placement(self):
        line,transcript=self._matched_source();saved=self.save()
        rows=[row for row in self.store.list_candidates(self.production) if row['media_path']=='/alt1.mov']
        session=self.store.get_session(self.alt)
        self.store.replace_candidates(self.production,[],refreshed_transcript_ids=[transcript['id']],
            expected_candidate_revisions={row['id']:row['revision'] for row in rows},
            expected_reference_revision=saved['revision'],expected_session_revisions={self.alt:session['revision']})
        self.assertEqual(self.store.get_reference_settings(self.production)['manual_timeline'],saved['manual_timeline'])

    def test_saved_layout_before_baseline_can_organize_but_not_align(self):
        state=self.store.update_reference_settings(self.production,None,expected_revision=self.state['revision'])
        layout=reconcile_layout({},self.inv,None)
        layout=move_items(layout,[self.one],0,3,self.inv,None,mode='organize')
        saved=self.store.update_manual_timeline(self.production,layout,expected_revision=state['revision'])
        self.assertIsNone(saved['manual_timeline']['baseline_session_id'])
        self.assertEqual(saved['manual_timeline']['items'][self.one]['organize_seconds'],0)
        self.assertEqual(saved['manual_timeline']['items'][self.one]['organize_lane'],4)
        self.assertIsNone(saved['manual_timeline']['items'][self.one]['start_seconds'])

    def test_complete_project_serialization_contains_manual_layout(self):
        from local_project import LocalProjectClient
        saved=self.save();client=LocalProjectClient();client.store.close();client.store=self.store
        payload=client.export_project(self.production)
        recovered=LocalProjectClient.from_project(json.loads(json.dumps(payload)))
        self.addCleanup(recovered.close)
        self.assertEqual(recovered.call('get_reference_settings',production_id=self.production)['manual_timeline'],saved['manual_timeline'])


if __name__=='__main__':unittest.main()
