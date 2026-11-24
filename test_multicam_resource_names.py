"""Default scene labels must not merge unrelated multicam media identities."""
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from manual_timeline import inventory,reconcile_layout,place_items,take_id,build_manual_plan
from reference_timeline import assemble_reference_xml
from resolve_import_helper import build_resolve_manifest
from test_reference_timeline import dataset,write_fixture


DEFAULT='PGM_S01_PROXY_CONFORM_ABCD'


class MulticamResourceNameTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.folder=Path(self.tmp.name);self.data,self.cuts=dataset()
        self.files={sid:write_fixture(self.folder,sid) for sid in ('base','alt','second')}
        rows=inventory(self.data,self.cuts);layout=reconcile_layout({},rows,'base')
        for sid,position,lane in [('alt',2,1),('second',3,2)]:
            layout=place_items(layout,[take_id(sid,'/'+sid+'.mov')],position,lane,rows,'base')
        self.plan=build_manual_plan(self.data,'base',layout,self.cuts)

    def names(self,value):
        for sid,path in self.files.items():
            tree=ET.parse(path);name=value[sid] if isinstance(value,dict) else value
            for row in tree.findall('./resources/media'):
                if row.find('multicam') is not None:row.set('name',name)
            for row in tree.iter('mc-clip'):row.set('name',name)
            tree.write(path)

    def merge(self,label='merged',include_baseline=True,plan=None):
        output=self.folder/(label+'.fcpxml')
        report=assemble_reference_xml(self.files['base'],self.files,plan or self.plan,output,
            title='Merged',include_baseline=include_baseline)
        return ET.parse(output).getroot(),build_resolve_manifest(output),report

    def test_cross_scene_default_s01_collisions_have_unique_names_and_consistent_clip_refs(self):
        self.names(DEFAULT);root,helper,_=self.merge()
        media={row.get('id'):row for row in root.findall('./resources/media') if row.find('multicam') is not None}
        names={row.get('name') for row in media.values()}
        self.assertEqual(len(names),3)
        self.assertEqual(media['base-multicam-main'].get('name'),DEFAULT)
        for sid,prefix in [('alt','alt1-'),('second','alt2-')]:
            token=hashlib.sha256(sid.encode()).hexdigest()[:8]
            expected=DEFAULT+' [s-'+token+']'
            self.assertEqual(media[prefix+'multicam-main'].get('name'),expected)
            clips=[row for row in root.iter('mc-clip') if row.get('ref')==prefix+'multicam-main']
            self.assertEqual(len(clips),3)
            self.assertTrue(all(row.get('name')==expected for row in clips))
        self.assertEqual({row['video']['name'] for row in helper['groups']},names)

    def test_only_resource_and_mc_clip_names_change_not_any_source_timing_audio_id_or_angle(self):
        self.names(dict(base='BASE_UNIQUE',alt='ALT_UNIQUE',second='SECOND_UNIQUE'))
        control,_,_=self.merge('control')
        self.names(DEFAULT);colliding,_,_=self.merge('colliding')
        for tree in (control,colliding):
            for row in tree.findall('./resources/media'):
                if row.find('multicam') is not None:row.attrib.pop('name',None)
            for row in tree.iter('mc-clip'):row.attrib.pop('name',None)
        self.assertEqual(ET.tostring(control),ET.tostring(colliding))

    def test_no_collision_shared_resource_is_not_renamed_per_clip(self):
        self.names(DEFAULT)
        plan=dict(self.plan,selections=[],duration_frames=250,timeline_start_frame=0,
            timeline_end_frame=250,lane_count=0)
        root,helper,_=self.merge(plan=plan)
        media=root.find('./resources/media')
        self.assertEqual(media.get('name'),DEFAULT)
        clips=list(root.iter('mc-clip'))
        self.assertEqual(len(clips),3)
        self.assertTrue(all(row.get('name')==DEFAULT for row in clips))
        self.assertEqual({row['video']['name'] for row in helper['groups']},{DEFAULT})

    def test_alternatives_only_prunes_baseline_before_disambiguating_and_preserves_new_names(self):
        self.names(DEFAULT);root,helper,_=self.merge(include_baseline=False)
        resources=root.findall('./resources/media')
        self.assertEqual(len(resources),2)
        self.assertTrue(all(not row.get('id').startswith('base-') for row in resources))
        self.assertEqual(len({row.get('name') for row in resources}),2)
        self.assertEqual(len({row['video']['name'] for row in helper['groups']}),2)


if __name__=='__main__':unittest.main()
