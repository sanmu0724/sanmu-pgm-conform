"""Restore historical organization without changing actual or source clocks."""
import copy
import unittest
from unittest.mock import patch

from manual_timeline import (attach_script_organizer, inventory, place_items,
    reconcile_layout, set_script_range, take_id, validate_layout)
from pgm_editor_state import coverage_fingerprint
from pgm_takes import PgmTakeIndex


def legacy_data():
    data=dict(sessions=[],scripts=[dict(id=f'line-{n}',script_id='script',order=n,
        text=f'演员第{n}段完整台词',active=True) for n in range(1,9)],transcripts=[],candidates=[])
    for sid,names in [('base',['base']),('alt',['green','blue','removed','hidden'])]:
        blocks=[dict(path=f'/{name}.mov',start=i*250,end=(i+1)*250,frames=250,fps=25)
                for i,name in enumerate(names)]
        config=dict(fps=25,pgm=blocks[0]['path'],pgm_sources=blocks,library_pgm_editor={},
            sources=[dict(camera='A',source_id=sid+'-A',proxy_path=f'/{sid}-proxy.mov',
                start_tc='19:00:00:00',frames=1000)],
            sound_sources=[dict(path=f'/{sid}.wav',start_tc='06:00:00:00',reel='SOUND_'+sid)])
        data['sessions'].append(dict(id=sid,name=sid,config=config))
        for name in names:
            number=['base','green','blue','removed','hidden'].index(name)+1
            transcript=dict(id=name+'-speech',session_id=sid,media_path=f'/{name}.mov',
                kind='pgm',start_seconds=.28,end_seconds=2.28,text=f'演员第{number}段完整台词')
            data['transcripts'].append(transcript)
            data['candidates'].append(dict(id=name+'-candidate',transcript_id=transcript['id'],
                script_line_id=f'line-{number}',status='confirmed',score=1.,note='人工确认',tags=[]))
    index=PgmTakeIndex(data)
    config=data['sessions'][1]['config']
    for name in ['green','blue','removed','hidden']:
        candidate=index.candidates[name+'-candidate'];summary=index.file_summary(candidate)
        first=summary.get('first_candidate')
        config['library_pgm_editor'][f'/{name}.mov']=dict(
            pool='removed' if name=='removed' else 'timeline',reviewed=name=='green',
            hidden_in_pool=name=='hidden',coverage_fingerprint=coverage_fingerprint(
                summary,index.source(first) if first else None))
    return data


class ScriptOrganizerPreservationTests(unittest.TestCase):
    def setUp(self):
        self.data=legacy_data()
        self.rows=attach_script_organizer(self.data,inventory(self.data))
        self.by_name={row['filename']:row for row in self.rows}
        self.layout=reconcile_layout({},self.rows,'base')
        self.green=take_id('alt','/green.mov');self.blue=take_id('alt','/blue.mov')

    def test_restore_existing_green_blue_removed_without_new_recognition(self):
        before=copy.deepcopy(self.data)
        with patch('core.probe',side_effect=AssertionError('must not probe')):
            rows=attach_script_organizer(self.data,inventory(self.data))
        rows={row['filename']:row for row in rows}
        self.assertEqual(len(rows),5)
        self.assertEqual((rows['green.mov']['organize_pool'],rows['green.mov']['organize_reviewed']),('track',True))
        self.assertEqual((rows['blue.mov']['organize_pool'],rows['blue.mov']['organize_reviewed']),('track',False))
        self.assertEqual(rows['removed.mov']['organize_pool'],'removed')
        self.assertEqual(self.data,before)

    def test_hidden_discard_remains_hidden_after_later_automatic_match(self):
        self.assertEqual(self.by_name['hidden.mov']['organize_pool'],'hidden')
        self.assertFalse(self.by_name['hidden.mov']['organize_reviewed'])

    def test_stale_review_does_not_inherit_green(self):
        self.data['sessions'][1]['config']['library_pgm_editor']['/green.mov']['coverage_fingerprint']='obsolete'
        rows={row['filename']:row for row in attach_script_organizer(self.data,inventory(self.data))}
        self.assertEqual(rows['green.mov']['organize_pool'],'track')
        self.assertFalse(rows['green.mov']['organize_reviewed'])

    def test_build38_actual_positions_survive_organizer_attachment(self):
        old=copy.deepcopy(self.layout)
        for item in old['items'].values():
            for key in list(item):
                if key.startswith('organize_') and key not in ('organize_seconds','organize_lane'):
                    item.pop(key)
        old.pop('organizer_axis',None)
        old['items'][self.green].update(start_seconds=3.28,lane=2,organize_seconds=55,organize_lane=7)
        changed=reconcile_layout(old,self.rows,'base')
        item=changed['items'][self.green]
        self.assertEqual((item['start_seconds'],item['lane']),(3.28,2))
        self.assertEqual((item['organize_first_line'],item['organize_last_line']),(2,2))
        self.assertTrue(item['organize_reviewed'])
        self.assertTrue(all(row['start_seconds'] is None for identity,row in changed['items'].items() if identity!=self.green))
        validate_layout(changed,self.rows,'base')

    def test_script_range_edit_never_changes_actual_or_source_time(self):
        before=copy.deepcopy(self.data)
        placed=place_items(self.layout,[self.green],21.12,3,self.rows,'base')
        changed=set_script_range(placed,[self.green],5,8,self.rows,'base')
        item=changed['items'][self.green]
        self.assertEqual((item['start_seconds'],item['lane']),(21.12,3))
        self.assertEqual((item['organize_first_line'],item['organize_last_line']),(5,8))
        self.assertEqual(self.data,before)

    def test_script_group_transfer_uses_one_actual_time_not_line_differences(self):
        self.layout['items'][self.green].update(organize_seconds=55,organize_lane=7)
        self.layout['items'][self.blue].update(organize_seconds=76,organize_lane=9)
        changed=place_items(self.layout,[self.green,self.blue],20.28,1,self.rows,'base')
        self.assertEqual([changed['items'][identity]['start_seconds'] for identity in (self.green,self.blue)],[20.28,20.28])
        self.assertEqual([changed['items'][identity]['lane'] for identity in (self.green,self.blue)],[1,3])

    def test_actual_group_transfer_keeps_existing_actual_offsets(self):
        placed=place_items(self.layout,[self.green],5,1,self.rows,'base')
        placed=place_items(placed,[self.blue],8,2,self.rows,'base')
        changed=place_items(placed,[self.green,self.blue],30,4,self.rows,'base')
        self.assertEqual([changed['items'][identity]['start_seconds'] for identity in (self.green,self.blue)],[30,33])

    def test_changed_baseline_resets_actual_only_and_keeps_reviewed_script_range(self):
        placed=place_items(self.layout,[self.green],21.12,3,self.rows,'base')
        changed=reconcile_layout(placed,self.rows,'alt')
        self.assertTrue(all(row['start_seconds'] is None for row in changed['items'].values()))
        item=changed['items'][self.green]
        self.assertEqual((item['organize_first_line'],item['organize_last_line']),(2,2))
        self.assertTrue(item['organize_reviewed'])

    def test_explicit_discard_removes_actual_take_without_losing_media_or_range(self):
        before=copy.deepcopy(self.data)
        placed=place_items(self.layout,[self.green],21.12,3,self.rows,'base')
        removed=set_script_range(placed,[self.green],2,2,self.rows,'base',pool='removed')
        item=removed['items'][self.green]
        self.assertIsNone(item['start_seconds'])
        self.assertEqual(item['lane'],3)
        self.assertEqual((item['organize_pool'],item['organize_reviewed']),('removed',False))
        self.assertEqual((item['organize_first_line'],item['organize_last_line']),(2,2))
        # Opening again must not replace the deliberate removal with the old
        # catalogue's green review, nor restore its former actual position.
        reopened=reconcile_layout(removed,self.rows,'base')
        self.assertEqual(reopened['items'][self.green],item)
        restored=set_script_range(reopened,[self.green],2,2,self.rows,'base')
        self.assertIsNone(restored['items'][self.green]['start_seconds'])
        self.assertEqual(self.data,before)

    def test_no_active_script_keeps_discarded_pools_and_never_uses_cached_line_as_time(self):
        self.data['scripts']=[]
        rows=attach_script_organizer(self.data,inventory(self.data))
        by_name={row['filename']:row for row in rows}
        self.assertEqual(by_name['removed.mov']['organize_pool'],'removed')
        self.assertEqual(by_name['hidden.mov']['organize_pool'],'hidden')
        self.assertEqual(by_name['green.mov']['organize_pool'],'unmatched')
        layout=reconcile_layout({},rows,'base')
        self.assertTrue(all(row['start_seconds'] is None for row in layout['items'].values()))


if __name__=='__main__':unittest.main()
