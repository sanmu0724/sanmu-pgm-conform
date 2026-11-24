"""Script organization must not change the independent actual-time layout."""
import copy
import time
import unittest

from library_store import LibraryError
from manual_timeline import (inventory,reconcile_layout,validate_layout,move_items,
    place_items,set_script_range,build_manual_plan,take_id)
import test_manual_timeline as fixtures


class ManualScriptRangeTests(unittest.TestCase):
    def setUp(self):
        self.data,self.cuts=fixtures.fixture();self.rows=inventory(self.data,self.cuts)
        for row in self.rows:row['script_line_count']=100
        self.one=take_id('alt','/alt1.mov');self.two=take_id('alt','/alt2.mov')
        self.base=take_id('base','/base1.mov')
        self.layout=reconcile_layout({},self.rows,'base')

    def test_first_migration_preserves_actual_build38_geometry_and_seeds_organizer_only(self):
        legacy=dict(version=1,baseline_session_id='base',items={self.one:dict(
            session_id='alt',pgm_path='/alt1.mov',start_seconds=143.127,lane=8,
            organize_seconds=72.,organize_lane=4,hidden=False)})
        source=next(row for row in self.rows if row['id']==self.one)
        source.update(organize_first_line=55,organize_last_line=76,organize_reviewed=True,organize_pool='track')
        before=copy.deepcopy(legacy)
        result=reconcile_layout(legacy,self.rows,'base');item=result['items'][self.one]
        self.assertEqual((item['start_seconds'],item['lane']),(143.127,8))
        self.assertEqual((item['organize_first_line'],item['organize_last_line'],item['organize_reviewed']),(55,76,True))
        self.assertEqual((item['organize_seconds'],item['organize_lane']),(72.,1))
        self.assertEqual(legacy,before)
        self.assertEqual(result['organizer_axis'],'script')

    def test_build38_legacy_seconds_lanes_are_repacked_for_overlapping_script_ranges_only(self):
        layout=place_items(self.layout,[self.one],3,7,self.rows,'base')
        layout=place_items(layout,[self.two],20,7,self.rows,'base')
        layout.pop('organizer_axis')
        for identity,first,last in ((self.one,10,20),(self.two,15,30)):
            source=next(row for row in self.rows if row['id']==identity)
            source.update(organize_first_line=first,organize_last_line=last,organize_pool='track',organize_reviewed=True)
            item=layout['items'][identity]
            for key in tuple(item):
                if key.startswith('organize_') and key not in ('organize_lane','organize_seconds'):item.pop(key)
            item['organize_lane']=1
        restored=reconcile_layout(layout,self.rows,'base')
        validate_layout(restored,self.rows,'base')
        self.assertEqual([restored['items'][identity]['organize_lane'] for identity in (self.one,self.two)],[1,2])
        self.assertEqual([restored['items'][identity]['lane'] for identity in (self.one,self.two)],[7,7])
        self.assertEqual([restored['items'][identity]['start_seconds'] for identity in (self.one,self.two)],[3,20])

    def test_technical_reconcile_before_script_worker_does_not_consume_legacy_restore(self):
        legacy=dict(version=1,baseline_session_id='base',items={})
        technical=reconcile_layout(legacy,self.rows,'base')
        self.assertFalse(technical['items'][self.one]['organize_initialized'])
        source=next(row for row in self.rows if row['id']==self.one)
        source.update(organize_first_line=10,organize_last_line=20,organize_pool='track',organize_reviewed=True)
        restored=reconcile_layout(technical,self.rows,'base')
        self.assertEqual((restored['items'][self.one]['organize_first_line'],restored['items'][self.one]['organize_pool']),(10,'track'))
        self.assertTrue(restored['items'][self.one]['organize_initialized'])

    def test_existing_explicit_organizer_edits_override_later_machine_decorations(self):
        layout=set_script_range(self.layout,[self.one],12,14,self.rows,'base')
        source=next(row for row in self.rows if row['id']==self.one)
        source.update(organize_first_line=50,organize_last_line=90,organize_pool='unmatched',organize_reviewed=False)
        result=reconcile_layout(layout,self.rows,'base')
        self.assertEqual(result['items'][self.one],layout['items'][self.one])

    def test_shorter_replacement_script_stages_old_range_without_blocking_repairs_or_moving_actual(self):
        layout=place_items(self.layout,[self.one],143.127,8,self.rows,'base')
        layout=set_script_range(layout,[self.one],55,76,self.rows,'base')
        rows=copy.deepcopy(self.rows)
        for source in rows:source['script_line_count']=30
        repaired=reconcile_layout(layout,rows,'base');item=repaired['items'][self.one]
        self.assertEqual((item['organize_first_line'],item['organize_last_line'],item['organize_pool']),(55,76,'unmatched'))
        self.assertEqual((item['start_seconds'],item['lane']),(143.127,8))
        validate_layout(repaired,rows,'base')
        corrected=set_script_range(repaired,[self.one],10,15,rows,'base')
        self.assertEqual(corrected['items'][self.one]['organize_pool'],'track')
        self.assertEqual(corrected['items'][self.one]['start_seconds'],143.127)

    def test_explicit_range_edit_keeps_actual_source_and_target_geometry(self):
        layout=place_items(self.layout,[self.one],52.75,7,self.rows,'base')
        changed=set_script_range(layout,[self.one],55,76,self.rows,'base')
        moved=move_items(changed,[self.one],0,4,self.rows,'base',mode='organize')
        item=moved['items'][self.one]
        self.assertEqual((item['start_seconds'],item['lane']),(52.75,7))
        self.assertEqual((item['organize_first_line'],item['organize_last_line']),(55,76))
        self.assertTrue(item['organize_reviewed'])
        plan=build_manual_plan(self.data,'base',moved,self.cuts)
        piece=plan['selections'][0]
        self.assertEqual((piece['record_frame_in'],piece['record_frame_out_exclusive']),(0,250))
        self.assertEqual(piece['target_frame_in'],1319)

    def test_baseline_script_range_is_editable_without_becoming_an_alternative(self):
        changed=set_script_range(self.layout,[self.base],4,19,self.rows,'base')
        changed=move_items(changed,[self.base],0,2,self.rows,'base',mode='organize')
        self.assertIsNone(changed['items'][self.base]['start_seconds'])
        self.assertEqual(build_manual_plan(self.data,'base',changed,self.cuts)['selections'],[])

    def test_script_interval_collision_uses_lines_not_old_seconds_or_media_length(self):
        layout=set_script_range(self.layout,[self.one],10,20,self.rows,'base')
        layout=set_script_range(layout,[self.two],21,25,self.rows,'base')
        self.assertEqual(layout['items'][self.two]['organize_lane'],1)
        layout=set_script_range(layout,[self.two],20,25,self.rows,'base')
        self.assertEqual(layout['items'][self.two]['organize_lane'],2)
        self.assertEqual(layout['items'][self.one]['organize_seconds'],layout['items'][self.two]['organize_seconds'])
        with self.assertRaisesRegex(ValueError,'目标台本轨道'):
            move_items(layout,[self.two],0,-1,self.rows,'base',mode='organize',auto_lane=False)

    def test_staged_same_organizer_lane_gets_distinct_actual_lanes_and_no_fake_time_delta(self):
        layout=set_script_range(self.layout,[self.one],1,8,self.rows,'base')
        layout=set_script_range(layout,[self.two],55,76,self.rows,'base')
        self.assertEqual(layout['items'][self.one]['organize_lane'],layout['items'][self.two]['organize_lane'])
        moved=place_items(layout,[self.one,self.two],32.5,3,self.rows,'base')
        items=[moved['items'][identity] for identity in (self.one,self.two)]
        self.assertEqual([item['start_seconds'] for item in items],[32.5,32.5])
        self.assertEqual([item['lane'] for item in items],[3,4])
        self.assertEqual([item['organize_first_line'] for item in items],[1,55])

    def test_removed_or_hidden_explicit_restore_is_staged_and_does_not_reveal_old_actual_position(self):
        layout=place_items(self.layout,[self.one],32.5,3,self.rows,'base')
        for pool in ('removed','hidden'):
            old=copy.deepcopy(layout);old['items'][self.one].update(organize_pool=pool,hidden=pool=='hidden')
            restored=set_script_range(old,[self.one],10,12,self.rows,'base')
            item=restored['items'][self.one]
            self.assertIsNone(item['start_seconds'])
            self.assertEqual((item['lane'],item['organize_pool'],item['hidden']),(3,'track',False))

    def test_new_organizer_validation_rejects_bad_range_pool_or_review_without_rewriting_sources(self):
        layout=set_script_range(self.layout,[self.one],55,76,self.rows,'base')
        for mutation in (dict(organize_first_line=0),dict(organize_last_line=101),
                         dict(organize_first_line=None),dict(organize_pool='something'),dict(organize_reviewed=1)):
            altered=copy.deepcopy(layout);altered['items'][self.one].update(mutation)
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):
                validate_layout(altered,self.rows,'base')

    def test_large_first_restore_packs_nonoverlap_and_only_adds_tracks_for_overlap(self):
        rows=[]
        for number in range(5000):
            identity='take'+str(number)
            rows.append(dict(id=identity,session_id='alt',pgm_path='/'+identity+'.mov',
                organize_first_line=(number//5)*3+1,organize_last_line=(number//5)*3+2,
                organize_reviewed=bool(number%2),organize_pool='track',duration_seconds=10,ready=True))
        began=time.monotonic();layout=reconcile_layout({},rows,'base')
        self.assertEqual(max(item['organize_lane'] for item in layout['items'].values()),5)
        self.assertTrue(all(item['start_seconds'] is None for item in layout['items'].values()))
        validate_layout(layout,rows,'base')
        self.assertLess(time.monotonic()-began,3.)


class ScriptRangePersistenceTests(unittest.TestCase):
    setUp=fixtures.ManualPersistenceTests.setUp
    tearDown=fixtures.ManualPersistenceTests.tearDown
    inventory=fixtures.ManualPersistenceTests.inventory
    save=fixtures.ManualPersistenceTests.save
    def test_new_fields_roundtrip_and_baseline_switch_preserve_ranges_and_clear_actual_only(self):
        self.store.put_script(self.production,'完整台本',[dict(text='测试'+str(number)) for number in range(100)])
        layout=set_script_range(self.layout,[self.one],55,76,self.inv,self.base)
        saved=self.save(layout)
        self.assertEqual(saved['manual_timeline']['items'][self.one]['organize_first_line'],55)
        changed=self.store.update_reference_settings(self.production,self.sessions['second']['id'],expected_revision=saved['revision'])
        item=changed['manual_timeline']['items'][self.one]
        self.assertEqual((item['organize_first_line'],item['organize_last_line'],item['organize_reviewed']),(55,76,True))
        self.assertIsNone(item['start_seconds'])

    def test_store_checks_current_script_limit_without_loading_transcripts_or_candidates(self):
        self.store.put_script(self.production,'短台本',[dict(text='第一句'),dict(text='第二句')])
        layout=copy.deepcopy(self.layout)
        layout['items'][self.one].update(organize_first_line=1,organize_last_line=3,organize_pool='track')
        with self.assertRaisesRegex(LibraryError,'超出'):
            self.save(layout)


if __name__=='__main__':unittest.main()
