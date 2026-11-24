"""Automatic proposals and human actual-position reviews remain independent."""
import copy
import hashlib
import json
import unittest

from local_project import LocalProjectClient
from library_store import LibraryError
from manual_timeline import (apply_alignment_proposals, build_manual_plan, confirm_alignment,
    inventory, invalidate_alignment, move_items, place_items, reconcile_layout,
    set_script_range, take_id, validate_layout)


def sources():
    data=dict(sessions=[]);cuts={}
    for sid in ('base','alt','other'):
        blocks=[dict(path=f'/{sid}{n}.mov',start=n*250,end=(n+1)*250,frames=250,fps=25) for n in range(2)]
        data['sessions'].append(dict(id=sid,name=sid,config=dict(fps=25,pgm_sources=blocks,
            sources=[dict(camera='A',source_id=sid+'A',start_tc='19:00:00:00',proxy_path='/daily.mov')],
            sound_sources=[dict(path='/original.wav',start_tc='06:00:00:00',reel='SOUND')])) )
        cuts[sid]=dict(fps=25,duration=500,pgm_sources=copy.deepcopy(blocks))
    return data,cuts


def decorated(data):
    rows=inventory(data)
    for row in rows:row.update(organize_first_line=1,organize_last_line=2,
        organize_pool='track',organize_reviewed=True,script_line_count=20)
    return rows


def fingerprint(mapping):
    return hashlib.sha256(json.dumps(mapping,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


class AlignmentStateTests(unittest.TestCase):
    def setUp(self):
        self.data,self.cuts=sources();self.rows=decorated(self.data)
        self.layout=reconcile_layout({},self.rows,'base')
        self.one=take_id('alt','/alt0.mov');self.two=take_id('alt','/alt1.mov')

    def proposal(self,identity=None,start=5,lane=1,**extra):
        identity=identity or self.one;row=next(row for row in self.rows if row['id']==identity)
        return dict(take_id=identity,session_id=row['session_id'],pgm_path=row['pgm_path'],
            start_seconds=start,lane=lane,first_line=1,last_line=2,needs_review=True,
            approximate=True,confidence=.35,reason='按可信区间粗排',mapping_fingerprint='map',**extra)

    def apply(self,layout=None,proposals=None,**kwargs):
        return apply_alignment_proposals(layout or self.layout,proposals or [self.proposal()],self.rows,'base',**kwargs)

    def test_legacy_coordinates_stay_put_and_start_blue(self):
        old=copy.deepcopy(self.layout)
        for item in old['items'].values():
            for key in list(item):
                if key.startswith('alignment_'):item.pop(key)
        old['items'][self.one].update(start_seconds=7.24,lane=3)
        result=reconcile_layout(old,self.rows,'base')['items'][self.one]
        self.assertEqual((result['start_seconds'],result['lane']),(7.24,3))
        self.assertFalse(result['alignment_reviewed']);self.assertEqual(result['alignment_origin'],'legacy')

    def test_automatic_proposal_is_blue_and_preserves_media_and_layout_input(self):
        before=copy.deepcopy((self.layout,self.data,self.rows))
        result=self.apply()['items'][self.one]
        self.assertEqual((result['start_seconds'],result['lane']),(5,1))
        self.assertEqual(result['alignment_origin'],'automatic');self.assertFalse(result['alignment_reviewed'])
        self.assertTrue(result['alignment_basis']['approximate'])
        self.assertEqual((self.layout,self.data,self.rows),before)

    def test_reapply_defaults_to_preserving_automatic_position(self):
        placed=self.apply()
        self.assertEqual(self.apply(placed,[self.proposal(start=50)]),placed)
        replaced=self.apply(placed,[self.proposal(start=50)],replace_automatic=True)
        self.assertEqual(replaced['items'][self.one]['start_seconds'],50)
        self.assertFalse(replaced['items'][self.one]['alignment_reviewed'])

    def test_human_and_legacy_positions_never_overwritten_even_in_replacement_mode(self):
        for kind in ('manual','legacy','confirmed_auto'):
            with self.subTest(kind=kind):
                if kind=='manual':placed=place_items(self.layout,[self.one],7.24,3,self.rows,'base')
                elif kind=='legacy':
                    placed=copy.deepcopy(self.layout);placed['items'][self.one].update(start_seconds=7.24,lane=3)
                else:placed=confirm_alignment(self.apply(),[self.one],self.rows,'base')
                self.assertEqual(self.apply(placed,[self.proposal(start=50)],replace_automatic=True),placed)

    def test_automatic_skips_baseline_unreviewed_removed_and_hidden(self):
        for kind in ('base','unreviewed','removed','hidden'):
            with self.subTest(kind=kind):
                layout=copy.deepcopy(self.layout);identity=self.one
                if kind=='base':identity=take_id('base','/base0.mov')
                if kind=='unreviewed':layout['items'][identity]['organize_reviewed']=False
                if kind=='removed':layout['items'][identity]['organize_pool']='removed'
                if kind=='hidden':layout['items'][identity]['hidden']=True
                result=self.apply(layout,[self.proposal(identity)])
                self.assertIsNone(result['items'][identity]['start_seconds'])

    def test_collision_packs_new_takes_without_moving_human_take(self):
        human=place_items(self.layout,[self.one],5,1,self.rows,'base')
        three=take_id('other','/other0.mov')
        result=self.apply(human,[self.proposal(self.two,6,1),self.proposal(three,7,1)])
        self.assertEqual((result['items'][self.one]['start_seconds'],result['items'][self.one]['lane']),(5,1))
        self.assertEqual({result['items'][identity]['lane'] for identity in (self.two,three)},{2,3})

    def test_stale_range_and_wrong_scene_fail_without_partial_changes(self):
        for field,value in [('first_line',2),('session_id','wrong'),('pgm_path','/wrong.mov')]:
            proposal=self.proposal();proposal[field]=value;before=copy.deepcopy(self.layout)
            with self.subTest(field=field),self.assertRaises(ValueError):self.apply(proposals=[proposal])
            self.assertEqual(self.layout,before)

    def test_actual_drag_and_lane_change_are_manual_blue(self):
        placed=self.apply();moved=move_items(placed,[self.one],0,1,self.rows,'base')
        item=moved['items'][self.one]
        self.assertFalse(item['alignment_reviewed']);self.assertEqual(item['alignment_origin'],'manual')
        self.assertEqual(item['start_seconds'],5)
        self.assertTrue(item['organize_reviewed'])

    def test_new_placement_is_not_confirmation_and_validation_never_promotes_it(self):
        placed=place_items(self.layout,[self.one],7.24,3,self.rows,'base')
        for restored in (placed,validate_layout(placed,self.rows,'base'),
                reconcile_layout(placed,self.rows,'base')):
            item=restored['items'][self.one]
            self.assertFalse(item['alignment_reviewed'])
            self.assertEqual(item['alignment_origin'],'manual')
            self.assertTrue(item['organize_reviewed'])

    def test_changing_confirmed_start_or_lane_revokes_confirmation(self):
        confirmed=confirm_alignment(self.apply(),[self.one],self.rows,'base')
        for kind in ('drag_time','drag_lane','enter_time','enter_lane'):
            with self.subTest(kind=kind):
                if kind=='drag_time':changed=move_items(confirmed,[self.one],2,0,self.rows,'base')
                elif kind=='drag_lane':changed=move_items(confirmed,[self.one],0,1,self.rows,'base')
                elif kind=='enter_time':changed=place_items(confirmed,[self.one],7,1,self.rows,'base')
                else:changed=place_items(confirmed,[self.one],5,2,self.rows,'base')
                item=changed['items'][self.one]
                self.assertFalse(item['alignment_reviewed'])
                self.assertEqual(item['alignment_origin'],'manual')
                self.assertTrue(item['alignment_invalid_reason'])
        self.assertTrue(confirmed['items'][self.one]['alignment_reviewed'])

    def test_no_geometry_change_preserves_confirmed_and_unconfirmed_provenance(self):
        for confirmed in (False,True):
            for origin in ('automatic','manual','legacy'):
                with self.subTest(confirmed=confirmed,origin=origin):
                    placed=self.apply();placed['items'][self.one]['alignment_origin']=origin
                    if confirmed:placed=confirm_alignment(placed,[self.one],self.rows,'base')
                    self.assertEqual(move_items(placed,[self.one],0,0,self.rows,'base'),placed)
                    self.assertEqual(place_items(placed,[self.one],5,1,self.rows,'base'),placed)

    def test_group_move_revokes_every_changed_take_but_keeps_unselected_green(self):
        layout=self.apply(proposals=[self.proposal(self.one,5,1),self.proposal(self.two,15,1)])
        other=take_id('other','/other0.mov')
        layout=self.apply(layout,[self.proposal(other,50,2)])
        layout=confirm_alignment(layout,[self.one,self.two,other],self.rows,'base')
        changed=move_items(layout,[self.one,self.two],2,1,self.rows,'base')
        for identity,start in ((self.one,7),(self.two,17)):
            item=changed['items'][identity]
            self.assertEqual((item['start_seconds'],item['lane']),(start,2))
            self.assertFalse(item['alignment_reviewed'])
            self.assertEqual(item['alignment_origin'],'manual')
        self.assertEqual(changed['items'][other],layout['items'][other])

    def test_group_noop_only_invalidates_members_whose_final_geometry_changes(self):
        # Group placement retains relative positions/lanes; an unchanged group
        # must not turn already confirmed takes blue merely because Apply ran.
        layout=self.apply(proposals=[self.proposal(self.one,5,1),self.proposal(self.two,15,1)])
        layout=confirm_alignment(layout,[self.one,self.two],self.rows,'base')
        self.assertEqual(place_items(layout,[self.one,self.two],5,1,self.rows,'base'),layout)

    def test_mixed_group_placement_preserves_unchanged_green_member_only(self):
        layout=confirm_alignment(self.apply(),[self.one],self.rows,'base')
        layout['items'][self.two]['lane']=2
        changed=place_items(layout,[self.one,self.two],5,1,self.rows,'base')
        self.assertEqual(changed['items'][self.one],layout['items'][self.one])
        item=changed['items'][self.two]
        self.assertEqual((item['start_seconds'],item['lane']),(5,2))
        self.assertFalse(item['alignment_reviewed']);self.assertEqual(item['alignment_origin'],'manual')

    def test_collision_forced_lane_change_remains_unconfirmed(self):
        layout=self.apply(proposals=[self.proposal(self.one,5,2),self.proposal(self.two,5,1)])
        layout=confirm_alignment(layout,[self.one,self.two],self.rows,'base')
        changed=place_items(layout,[self.two],5,2,self.rows,'base')
        self.assertEqual(changed['items'][self.two]['lane'],3)
        self.assertFalse(changed['items'][self.two]['alignment_reviewed'])
        self.assertTrue(changed['items'][self.one]['alignment_reviewed'])

    def test_organizer_lane_edit_keeps_actual_confirmation_independent(self):
        layout=confirm_alignment(self.apply(),[self.one],self.rows,'base')
        changed=move_items(layout,[self.one],0,2,self.rows,'base',mode='organize')
        self.assertTrue(changed['items'][self.one]['alignment_reviewed'])
        self.assertEqual(changed['items'][self.one]['alignment_origin'],'automatic')
        self.assertTrue(changed['items'][self.one]['organize_reviewed'])

    def test_explicit_confirm_changes_no_geometry_and_retains_automatic_provenance(self):
        placed=self.apply();result=confirm_alignment(placed,[self.one],self.rows,'base')
        item=result['items'][self.one]
        self.assertTrue(item['alignment_reviewed']);self.assertEqual(item['alignment_origin'],'automatic')
        self.assertEqual((item['start_seconds'],item['lane']),(5,1))
        with self.assertRaises(ValueError):confirm_alignment(self.layout,[self.one],self.rows,'base')

    def test_explicit_confirmation_rejects_baseline_and_hidden_takes(self):
        base=take_id('base','/base0.mov')
        with self.assertRaises(ValueError):confirm_alignment(self.layout,[base],self.rows,'base')
        hidden=self.apply();hidden['items'][self.one]['hidden']=True
        with self.assertRaises(ValueError):confirm_alignment(hidden,[self.one],self.rows,'base')

    def test_script_change_invalidates_review_without_moving_human_position(self):
        placed=place_items(self.layout,[self.one],7.24,3,self.rows,'base')
        result=set_script_range(placed,[self.one],4,5,self.rows,'base')['items'][self.one]
        self.assertEqual((result['start_seconds'],result['lane']),(7.24,3))
        self.assertFalse(result['alignment_reviewed']);self.assertTrue(result['alignment_invalid_reason'])

    def test_removed_take_clears_actual_review_and_confirmation_does_not_revive_it(self):
        placed=confirm_alignment(self.apply(),[self.one],self.rows,'base')
        result=set_script_range(placed,[self.one],1,2,self.rows,'base',pool='removed')
        self.assertIsNone(result['items'][self.one]['start_seconds']);self.assertFalse(result['items'][self.one]['alignment_reviewed'])
        with self.assertRaises(ValueError):confirm_alignment(result,[self.one],self.rows,'base')

    def test_baseline_change_and_explicit_invalidation_keep_source_clock(self):
        before=copy.deepcopy(self.data);placed=confirm_alignment(self.apply(),[self.one],self.rows,'base')
        invalid=invalidate_alignment(placed,[self.one],self.rows,'base',reason='新映射')
        self.assertFalse(invalid['items'][self.one]['alignment_reviewed'])
        self.assertEqual(invalid['items'][self.one]['start_seconds'],5)
        changed=reconcile_layout(placed,self.rows,'other')
        self.assertIsNone(changed['items'][self.one]['start_seconds']);self.assertFalse(changed['items'][self.one]['alignment_reviewed'])
        self.assertEqual(self.data,before)

    def test_invalid_review_provenance_and_unbounded_basis_are_rejected(self):
        for field,value in [('alignment_reviewed',1),('alignment_origin',[]),('alignment_basis',{'offset':float('nan')}),
                ('alignment_basis',{'cache':'x'*70000})]:
            layout=copy.deepcopy(self.layout);layout['items'][self.one][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):validate_layout(layout,self.rows,'base')

    def test_blue_automatic_positions_remain_available_for_rough_cut_export(self):
        result=build_manual_plan(self.data,'base',self.apply(),self.cuts)
        self.assertEqual(len(result['selections']),1)
        self.assertEqual(result['selections'][0]['target_frame_in'],125)
        self.assertEqual(result['selections'][0]['pgm_frame_in'],0)


class AlignmentPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.client=LocalProjectClient();self.production=self.client.call('create_production',name='对齐测试')['id']
        data,_=sources();self.ids={}
        for source in data['sessions']:
            self.ids[source['id']]=self.client.call('save_session',production_id=self.production,
                name=source['name'],config=source['config'])['id']
        self.base=self.ids['base'];self.alt=self.ids['alt']
        self.state=self.client.call('update_reference_settings',production_id=self.production,reference_session_id=self.base)
        self.rows=decorated(dict(sessions=self.client.call('list_sessions',production_id=self.production)))
        self.one=take_id(self.alt,'/alt0.mov');self.layout=reconcile_layout({},self.rows,self.base)
        self.mapping=dict(reference_session_id=self.base,version=1,segments=[])
        proposal=dict(take_id=self.one,start_seconds=5,lane=1,first_line=1,last_line=2,
            mapping_fingerprint=fingerprint(self.mapping))
        self.layout=apply_alignment_proposals(self.layout,[proposal],self.rows,self.base)

    def tearDown(self):self.client.close()
    def state_now(self):return self.client.call('get_reference_settings',production_id=self.production)
    def save(self,layout=None,mapping=None,revision=None):
        return self.client.call('update_manual_timeline',production_id=self.production,
            manual_timeline=self.layout if layout is None else layout,
            baseline_script_map=mapping,expected_revision=self.state_now()['revision'] if revision is None else revision)

    def test_mapping_and_layout_commit_in_one_cas_revision(self):
        old=self.state_now();saved=self.save(mapping=self.mapping)
        self.assertEqual(saved['revision'],old['revision']+1)
        self.assertEqual(saved['baseline_script_map'],self.mapping)
        self.assertEqual(saved['manual_timeline'],self.layout)
        recovered=LocalProjectClient.from_project(self.client.export_project(self.production))
        try:self.assertEqual(recovered.call('get_reference_settings',production_id=self.production),saved)
        finally:recovered.close()

    def test_manual_position_stays_blue_through_save_and_project_reopen(self):
        confirmed=confirm_alignment(self.layout,[self.one],self.rows,self.base)
        changed=place_items(confirmed,[self.one],9.25,2,self.rows,self.base)
        saved=self.save(layout=changed)
        item=saved['manual_timeline']['items'][self.one]
        self.assertEqual((item['start_seconds'],item['lane']),(9.25,2))
        self.assertFalse(item['alignment_reviewed']);self.assertEqual(item['alignment_origin'],'manual')
        recovered=LocalProjectClient.from_project(self.client.export_project(self.production))
        try:
            restored=recovered.call('get_reference_settings',production_id=self.production)['manual_timeline']
            restored=reconcile_layout(restored,self.rows,self.base)
            self.assertEqual(restored['items'][self.one],item)
            self.assertFalse(validate_layout(restored,self.rows,self.base)['items'][self.one]['alignment_reviewed'])
        finally:recovered.close()

    def test_previously_confirmed_position_stays_green_when_opened_without_edits(self):
        confirmed=confirm_alignment(self.layout,[self.one],self.rows,self.base)
        saved=self.save(layout=confirmed)
        recovered=LocalProjectClient.from_project(self.client.export_project(self.production))
        try:
            restored=recovered.call('get_reference_settings',production_id=self.production)['manual_timeline']
            restored=reconcile_layout(restored,self.rows,self.base)
            self.assertTrue(restored['items'][self.one]['alignment_reviewed'])
            self.assertEqual(restored,saved['manual_timeline'])
        finally:recovered.close()

    def test_none_preserves_map_and_explicit_empty_clears_it_without_moving_clip(self):
        self.save(mapping=self.mapping)
        confirmed=confirm_alignment(self.layout,[self.one],self.rows,self.base)
        saved=self.save(layout=confirmed)
        self.assertEqual(saved['baseline_script_map'],self.mapping)
        cleared=self.save(layout=confirmed,mapping={})
        self.assertEqual(cleared['baseline_script_map'],{})
        item=cleared['manual_timeline']['items'][self.one]
        self.assertFalse(item['alignment_reviewed']);self.assertEqual(item['start_seconds'],5)

    def test_bad_layout_bad_map_and_stale_cas_roll_back_both(self):
        self.save(mapping=self.mapping);before=self.client.export_project(self.production)['tables']
        invalid=copy.deepcopy(self.layout);invalid['items'][self.one]['start_seconds']=float('nan')
        attempts=[dict(layout=invalid,mapping={}),dict(mapping={'bad':float('nan')}),
                  dict(mapping={},revision=self.state_now()['revision']-1),dict(mapping=[])]
        for args in attempts:
            with self.subTest(args=list(args)),self.assertRaises(LibraryError):self.save(**args)
            self.assertEqual(self.client.export_project(self.production)['tables'],before)

    def test_map_replacement_invalidates_auto_review_and_preserves_actual_coordinates(self):
        confirmed=confirm_alignment(self.layout,[self.one],self.rows,self.base)
        self.save(layout=confirmed,mapping=self.mapping)
        old=self.state_now();newmap=dict(self.mapping,version=2)
        saved=self.client.call('update_reference_settings',production_id=self.production,
            reference_session_id=self.base,expected_revision=old['revision'],baseline_script_map=newmap)
        item=saved['manual_timeline']['items'][self.one]
        self.assertFalse(item['alignment_reviewed']);self.assertEqual(item['start_seconds'],5)


if __name__=='__main__':unittest.main()
