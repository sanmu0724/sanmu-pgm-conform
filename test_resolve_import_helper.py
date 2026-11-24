"""Exact final-XML identities for Resolve's split-mono linking import step."""
from fractions import Fraction as F
import hashlib
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
import xml.etree.ElementTree as ET
from unittest.mock import patch

from core import AnalysisError
from resolve_import_helper import (HEADER, MANAGED_MARKER, MENU_FILENAME,
    RESOLVE_MENU_SCRIPT, build_resolve_manifest, companion_path,
    write_resolve_import_helper, write_resolve_menu_script, _project_opening)


def stamp(value):
    value=F(value);return f'{value.numerator}/{value.denominator}s'


class ResolveImportHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.folder=Path(self.temp.name)
        self.path=self.folder/'多机位.fcpxml'
        self.root=ET.Element('fcpxml',version='1.8')
        self.resources=ET.SubElement(self.root,'resources')
        ET.SubElement(self.resources,'format',id='fmt',frameDuration='1/25s')
        library=ET.SubElement(self.root,'library');event=ET.SubElement(library,'event')
        self.project=ET.SubElement(event,'project',name='多机位测试')
        self.sequence=ET.SubElement(self.project,'sequence',format='fmt',tcStart='3600s',duration='10s')
        self.spine=ET.SubElement(self.sequence,'spine')

    def tearDown(self):self.temp.cleanup()

    def asset(self,identity,filename,start=68400,pair='',role='',video=False):
        path=self.folder/filename;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'unchanged original essence')
        attrs=dict(id=identity,name=Path(filename).name,src=path.as_uri(),start=stamp(start),duration='60s')
        attrs.update(hasVideo='1' if video else '0',hasAudio='0' if video else '1')
        asset=ET.SubElement(self.resources,'asset',attrs)
        if pair:
            metadata=ET.SubElement(asset,'metadata')
            ET.SubElement(metadata,'md',key='com.pgm.audioPairID',value=pair)
            ET.SubElement(metadata,'md',key='com.pgm.audioPairChannel',value=role)
        return asset,path

    def mc(self,identity='mc',name='DAY01_MULTICAM'):
        self.asset(identity+'video',identity+'.mov',video=True)
        resource=ET.SubElement(self.resources,'media',id=identity,name=name)
        multicam=ET.SubElement(resource,'multicam',tcStart='0s',format='fmt')
        angle=ET.SubElement(multicam,'mc-angle',name='A',angleID=identity+'A')
        ET.SubElement(angle,'video',ref=identity+'video',offset='0s',start='68400s',duration='60s')

    def clip(self,record=3600,source=0,duration=2,parent=None,ref='mc',lane=None):
        parent=self.spine if parent is None else parent
        attrs=dict(offset=stamp(record),start=stamp(source),duration=stamp(duration),name=ref)
        if lane is not None:attrs['lane']=str(lane)
        clip=ET.SubElement(parent,'clip',attrs)
        ET.SubElement(clip,'mc-clip',ref=ref,offset=stamp(source),start=stamp(source),duration=stamp(duration))
        return clip

    def pair(self,host,prefix='p',source=68400,offset=0,duration=2,asset_start=None):
        for role,lane in (('L',-1),('R',-2)):
            self.asset(prefix+role,prefix+'_'+role+'.wav',source if asset_start is None else asset_start,prefix,role)
            clip=ET.SubElement(host,'clip',offset=stamp(offset),start=stamp(source),duration=stamp(duration),lane=str(lane))
            ET.SubElement(clip,'audio',ref=prefix+role,offset=stamp(source),start=stamp(source),duration=stamp(duration),srcCh='1')

    def save(self):ET.ElementTree(self.root).write(self.path,encoding='utf-8',xml_declaration=True);return self.path

    def test_exact_source_clocks_and_original_essence_are_untouched(self):
        self.mc();host=self.clip(source=F(7,25));self.pair(host,source=F(68400003,1000),offset=F(7,25),asset_start=68400)
        self.save();before={path:hashlib.sha256(path.read_bytes()).hexdigest() for path in self.folder.iterdir()}
        manifest=build_resolve_manifest(self.path)
        group=manifest['groups'][0]
        self.assertEqual((group['video']['record_start'],group['video']['record_end'],group['video']['source_start']),(90000,90050,7))
        self.assertEqual([row['source_start'] for row in group['audio']],[F(3,40),F(3,40)])
        helper=write_resolve_import_helper(self.path)
        self.assertEqual(helper,companion_path(self.path))
        for path,digest in before.items():self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),digest)
        content=helper.read_text()
        self.assertTrue(content.startswith(HEADER+'\n'))
        self.assertIn('GetLeftOffset(true)',content)
        self.assertLess(content.index('for _,expected in ipairs(manifest.groups)'),content.index('timeline:SetClipsLinked'))
        self.assertIn('DeleteTimelines({timeline})',content)

    def test_import_identity_copy_rewrites_only_real_project_opening(self):
        raw='''<?xml version="1.0" encoding="utf-8"?>
<!-- <project name="this is not the project"> -->
<fcpxml version="1.8"><resources><asset id="source" uid="keep-source-uid" src="file:///声音.wav"/></resources>
<library><event name="场次 &gt; 一"><project name="旧名" uid="old-project-uid" modDate="2026-10-20 00:00:00 +0800"><sequence/></project></event></library></fcpxml>'''.encode('utf-8')
        old=ET.fromstring(raw);project=old.find('.//project')
        first,last,prefix=_project_opening(raw,project)
        staged=raw[:first]+(prefix+' name="新名 &amp; 副本" uid="new-project-uid">').encode('utf-8')+raw[last:]
        new=ET.fromstring(staged)
        self.assertEqual(new.find('.//project').attrib,dict(name='新名 & 副本',uid='new-project-uid',modDate=project.get('modDate')))
        self.assertEqual(ET.tostring(old.find('resources')),ET.tostring(new.find('resources')))
        self.assertEqual(new.find('.//event').get('name'),'场次 > 一')
        self.assertIn(b'<!-- <project name="this is not the project"> -->',staged)

    def test_import_identity_copy_rejects_multiple_projects(self):
        raw=b'<fcpxml><project name="one"/><project name="two"/></fcpxml>'
        with self.assertRaisesRegex(AnalysisError,'一个项目'):
            _project_opening(raw,ET.fromstring(raw).find('project'))

    def test_base_and_connected_alternative_keep_separate_full_intervals(self):
        self.mc();base=self.clip(duration=2);self.pair(base,prefix='base',duration=2)
        self.mc('alt','DAY02_MULTICAM')
        alt=self.clip(record=1,source=5,duration=3,parent=base,ref='alt',lane=1)
        self.pair(alt,prefix='alt',source=30600,offset=5,duration=3)
        manifest=build_resolve_manifest(self.save())
        self.assertEqual(len(manifest['groups']),2)
        a,b=manifest['groups']
        self.assertEqual((a['video']['record_start'],a['video']['record_end']),(90000,90050))
        self.assertEqual((b['video']['record_start'],b['video']['record_end'],b['video']['source_start']),(90025,90100,125))
        self.assertNotEqual(a['audio'][0]['path'],b['audio'][0]['path'])

    def test_multi_wav_boundary_is_one_picture_link_group(self):
        self.mc();host=self.clip(duration=4)
        self.pair(host,prefix='first',source=68400,offset=0,duration=2)
        self.pair(host,prefix='second',source=70001,asset_start=70000,offset=2,duration=2)
        groups=build_resolve_manifest(self.save())['groups']
        self.assertEqual(len(groups),1);self.assertEqual(len(groups[0]['audio']),4)
        self.assertEqual([(row['record_start'],row['record_end'],row['source_start']) for row in groups[0]['audio']],
            [(90000,90050,0),(90000,90050,0),(90050,90100,25),(90050,90100,25)])

    def test_native_long_audio_leaf_is_trimmed_by_connected_clip(self):
        self.mc();host=self.clip();self.pair(host)
        for audio in host.findall('./clip/audio'):audio.set('duration','60s')
        groups=build_resolve_manifest(self.save())['groups']
        self.assertEqual([row['record_end']-row['record_start'] for row in groups[0]['audio']],[50,50])

    def test_native_metadata_loss_is_recovered_from_original_audio_guide(self):
        self.mc();host=self.clip();self.pair(host)
        guide=[]
        for asset in self.resources.findall('asset'):
            metadata=asset.find('metadata')
            if metadata is None:continue
            role=metadata.findall('md')[1].get('value');asset.remove(metadata)
            guide.append(dict(path=str(self.folder/asset.get('name')),pair_id='p',channel=role))
        self.save();self.assertIsNone(build_resolve_manifest(self.path))
        groups=build_resolve_manifest(self.path,guide)['groups']
        self.assertEqual([row['role'] for row in groups[0]['audio']],['L','R'])

    def test_missing_or_misaligned_pair_member_is_rejected(self):
        self.mc();host=self.clip();self.pair(host)
        host.findall('clip')[1].set('offset','1s')
        with self.assertRaisesRegex(AnalysisError,'配对缺失'):
            build_resolve_manifest(self.save())

    def test_same_path_same_positions_across_overlapping_groups_is_ambiguous(self):
        self.mc();host=self.clip();self.pair(host)
        duplicate=ET.fromstring(ET.tostring(host));duplicate.set('lane','1');self.spine.append(duplicate)
        with self.assertRaisesRegex(AnalysisError,'重复片段'):
            build_resolve_manifest(self.save())

    def test_same_multicam_name_for_different_scene_resources_is_rejected(self):
        self.mc();host=self.clip();self.pair(host)
        self.mc('other','DAY01_MULTICAM');self.clip(record=3602,ref='other')
        with self.assertRaisesRegex(AnalysisError,'资源名称重复'):
            build_resolve_manifest(self.save())

    def test_gap_links_its_pair_without_inventing_a_picture(self):
        gap=ET.SubElement(self.spine,'gap',offset='3600s',start='0s',duration='2s')
        self.pair(gap)
        group=build_resolve_manifest(self.save())['groups'][0]
        self.assertIsNone(group['video']);self.assertEqual(len(group['audio']),2)

    def test_no_pair_removes_only_previously_managed_companion(self):
        self.mc();host=self.clip();self.pair(host);self.save()
        helper=write_resolve_import_helper(self.path);self.assertTrue(helper.exists())
        for asset in self.resources.findall('asset'):
            metadata=asset.find('metadata')
            if metadata is not None:asset.remove(metadata)
        self.save();self.assertIsNone(write_resolve_import_helper(self.path));self.assertFalse(helper.exists())
        helper.write_text('foreign user script\n')
        with self.assertRaisesRegex(AnalysisError,'未覆盖'):write_resolve_import_helper(self.path)
        self.assertEqual(helper.read_text(),'foreign user script\n')

    def test_menu_is_standalone_and_requires_managed_companion_marker(self):
        target=self.folder/'Scripts'/'Edit'/MENU_FILENAME
        self.assertEqual(write_resolve_menu_script(target),target)
        text=target.read_text();self.assertEqual(text,RESOLVE_MENU_SCRIPT)
        self.assertTrue(text.startswith(MANAGED_MARKER+'\n'))
        self.assertIn('RequestFile',text);self.assertIn(HEADER,text)
        self.assertIn('ImportTimelineFromFile',text)

    def test_menu_failed_atomic_replace_preserves_previous_version(self):
        target=self.folder/MENU_FILENAME;target.write_text(MANAGED_MARKER+'\nold valid menu\n')
        before=target.read_bytes()
        with patch.object(Path,'replace',side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError,'disk full'):write_resolve_menu_script(target)
        self.assertEqual(target.read_bytes(),before)
        self.assertEqual(list(self.folder.glob('*.writing')),[])

    def test_atomic_cleanup_failure_cannot_hide_original_replace_error(self):
        target=self.folder/MENU_FILENAME;target.write_text(MANAGED_MARKER+'\nold valid menu\n')
        before=target.read_bytes();failure=OSError('original write failure')
        with patch.object(Path,'replace',side_effect=failure), \
                patch.object(Path,'unlink',side_effect=PermissionError('shared volume busy')), \
                self.assertLogs('resolve_import_helper',level='WARNING') as logs:
            with self.assertRaises(OSError) as caught:write_resolve_menu_script(target)
        self.assertIs(caught.exception,failure)
        self.assertEqual(target.read_bytes(),before)
        self.assertEqual(len(list(self.folder.glob('*.writing'))),1)
        self.assertIn('原始写入错误',logs.output[0])

    def test_atomic_success_never_unlinks_a_stale_temporary_name(self):
        target=self.folder/MENU_FILENAME
        with patch.object(Path,'unlink',side_effect=AssertionError('published file must not be cleaned')):
            self.assertEqual(write_resolve_menu_script(target),target)
        self.assertEqual(target.read_text(),RESOLVE_MENU_SCRIPT)
        self.assertEqual(list(self.folder.glob('*.writing')),[])

    def test_menu_and_companion_refuse_symlinks(self):
        target=self.folder/'user.lua';target.write_text('user content')
        menu=self.folder/MENU_FILENAME;menu.symlink_to(target)
        with self.assertRaisesRegex(AnalysisError,'符号链接'):write_resolve_menu_script(menu)
        self.mc();host=self.clip();self.pair(host);self.save()
        companion_path(self.path).symlink_to(target)
        with self.assertRaisesRegex(AnalysisError,'符号链接'):write_resolve_import_helper(self.path)
        self.assertEqual(target.read_text(),'user content')

    def test_plain_non_library_fcpxml_needs_no_helper(self):
        self.path.write_text('<fcpxml version="1.8"><project><sequence/></project></fcpxml>')
        self.assertIsNone(write_resolve_import_helper(self.path))

    def test_unrelated_paired_guide_does_not_validate_plain_legacy_xml(self):
        self.path.write_text('<fcpxml version="1.8"><resources><asset id="missing-path"/></resources><event><project/><project/></event></fcpxml>')
        guide=[dict(path=str(self.folder/'different.wav'),pair_id='unused',channel='L')]
        self.assertIsNone(write_resolve_import_helper(self.path,guide))

    def test_unused_missing_path_asset_does_not_break_paired_xml(self):
        self.mc();host=self.clip();self.pair(host)
        ET.SubElement(self.resources,'asset',id='unused-title',name='Unrelated generator')
        self.assertEqual(len(build_resolve_manifest(self.save())['groups']),1)

    def test_direct_event_project_can_generate_companion(self):
        self.mc();host=self.clip();self.pair(host)
        library=self.root.find('library');event=library.find('event');library.remove(event);self.root.remove(library);self.root.append(event)
        self.assertTrue(write_resolve_import_helper(self.save()).exists())

    def test_offline_bootstrap_import_and_menu_write_need_only_standard_library(self):
        target=self.folder/'offline_menu.lua'
        code='''
import sys
from pathlib import Path
import resolve_import_helper as helper
target=Path(sys.argv[1])
assert helper.write_resolve_menu_script(target)==target
assert target.read_text(encoding='utf-8').startswith(helper.MANAGED_MARKER+'\\n')
# Even a validation error must not pull in video/Qt dependencies at bootstrap.
target.write_text('foreign script',encoding='utf-8')
try:
    helper.write_resolve_menu_script(target)
except ValueError as exc:
    assert '未覆盖' in str(exc)
else:
    raise AssertionError('foreign menu overwritten')
assert not any(name in sys.modules for name in ('core','av','PyQt6','numpy'))
print('stdlib-only bootstrap passed')
'''
        result=subprocess.run([sys.executable,'-S','-B','-c',code,str(target)],
            cwd=Path(__file__).parent,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('stdlib-only bootstrap passed',result.stdout)


if __name__=='__main__':unittest.main()
