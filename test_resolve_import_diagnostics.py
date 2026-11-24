"""Execute generated import helpers in LuaJIT against an explicit SDK mock.

These tests prove diagnostics and bridge-call accounting, not Resolve's native
FCPXML importer, decoder, or NEXIS performance.
"""
import ctypes
import copy
from fractions import Fraction as F
import json
import struct
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from resolve_import_helper import (_lua, _audio_track_import_plan, _audio_document_import_patches,
    _audio_record_import_patches, _audio_import_patches, _merge_time_patches,
    build_resolve_manifest, write_resolve_import_helper)


LUAJIT = Path('/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/libluajit-5.1.2.dylib')


def run_lua(text):
    library = ctypes.CDLL(str(LUAJIT))
    library.luaL_newstate.restype = ctypes.c_void_p
    library.luaL_openlibs.argtypes = [ctypes.c_void_p]
    library.luaL_loadstring.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    library.luaL_loadstring.restype = ctypes.c_int
    library.lua_pcall.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    library.lua_pcall.restype = ctypes.c_int
    library.lua_tolstring.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_size_t)]
    library.lua_tolstring.restype = ctypes.c_char_p
    library.lua_close.argtypes = [ctypes.c_void_p]
    state = library.luaL_newstate()
    try:
        library.luaL_openlibs(state)
        status = library.luaL_loadstring(state, text.encode())
        if not status:
            status = library.lua_pcall(state, 0, 0, 0)
        if status:
            error = library.lua_tolstring(state, -1, None)
            raise AssertionError(error.decode(errors='replace') if error else f'Lua status {status}')
    finally:
        library.lua_close(state)


@unittest.skipUnless(LUAJIT.exists(), 'The optional Resolve LuaJIT runtime is not installed')
class ResolveImportDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self, count=3, record=F(617, 25), second_sound=False):
        root = ET.Element('fcpxml', version='1.8')
        resources = ET.SubElement(root, 'resources')
        ET.SubElement(resources, 'format', id='fmt', frameDuration='1/25s')
        for identity, name in [('picture', 'camera.mov'), ('sound', 'sound.wav'), ('other', 'folder/sound.wav')]:
            path = self.folder/name
            path.parent.mkdir(exist_ok=True)
            if identity == 'picture':path.write_bytes(b'Unchanged original video')
            else:
                # Valid sparse PCM/BWF test source, with a known 16-bit header.
                data_size=2000*48000*2;fmt=struct.pack('<HHIIHH',1,1,48000,96000,2,16)
                bext=bytearray(602);struct.pack_into('<Q',bext,338,3173509420)
                body=b'WAVEfmt '+struct.pack('<I',16)+fmt+b'bext'+struct.pack('<I',602)+bext+b'data'+struct.pack('<I',data_size)
                with path.open('wb')as stream:
                    stream.write(b'RIFF'+struct.pack('<I',len(body)+data_size)+body)
                    stream.seek(8+len(body)+data_size-1);stream.write(b'\0')
            attrs = dict(id=identity, name=path.name, src=path.as_uri(), start='0s', duration='1000s')
            if identity == 'picture':
                attrs.update(hasVideo='1', hasAudio='0')
            else:
                attrs.update(start='158675471/2400s', duration='2000s',hasAudio='1', audioChannels='1', audioRate='48000')
            node=ET.SubElement(resources, 'asset', attrs)
            if identity!='picture':
                metadata=ET.SubElement(node,'metadata')
                ET.SubElement(metadata,'md',key='com.pgm.audioBitDepth',value='16')
        media = ET.SubElement(resources, 'media', id='mc', name='SCENE_MULTICAM')
        mc = ET.SubElement(media, 'multicam', tcStart='0s', format='fmt')
        angle = ET.SubElement(mc, 'mc-angle', name='A', angleID='A')
        ET.SubElement(angle, 'video', ref='picture', offset='0s', start='0s', duration='1000s')
        project = ET.SubElement(root, 'project', name='Diagnostics QA')
        sequence = ET.SubElement(project, 'sequence', format='fmt', tcStart='0s', duration='900s')
        spine = ET.SubElement(sequence, 'spine')
        for index in range(count):
            group = f'group-{index}'
            offset = record + index*2
            picture = ET.SubElement(spine, 'mc-clip', ref='mc', offset=f'{offset}s', start=f'{index*2}s', duration='2s', srcEnable='video')
            metadata = ET.SubElement(picture, 'metadata')
            ET.SubElement(metadata, 'md', key='com.pgm.linkGroupID', value=group)
            sound = ET.SubElement(spine, 'asset-clip', ref='other' if second_sound and index%2 else 'sound',
                offset=f'{offset}s', start=f'{F(1663603,25)+index*2}s', duration='2s', srcEnable='audio',
                audioRole='dialogue.pgm-track-1', lane='-1')
            metadata = ET.SubElement(sound, 'metadata')
            ET.SubElement(metadata, 'md', key='com.pgm.linkGroupID', value=group)
        metadata = ET.SubElement(project, 'metadata')
        ET.SubElement(metadata, 'md', key='com.pgm.requestedTimelineStart', value='89383/25s')
        path = self.folder/'test.fcpxml'
        ET.ElementTree(root).write(path, encoding='utf-8', xml_declaration=True)
        helper = write_resolve_import_helper(path)
        return path, helper, build_resolve_manifest(path)

    def execute(self, manifest, helper, change='', block_reports=False, initial_fps=None):
        log = self.folder/'local-stage.log'
        stats = self.folder/'stats.txt'
        script = r'''
print=function() end
local manifest=MANIFEST
local stats={props=0,names=0,uids=0,links=0,deleted=0,restored=0,disabled_audio=0}
local items={video={},audio={}}
local routed=manifest.routing_enabled
local function media(expected,kind)
 local uid=kind=='video' and expected.name or expected.path
 return {
  GetUniqueId=function() stats.uids=stats.uids+1;return uid end,
  GetName=function() stats.names=stats.names+1;return expected.name or 'sound.wav' end,
  GetClipProperty=function() stats.props=stats.props+1;return {['File Path']=expected.path,
    ['File Name']=expected.name or 'sound.wav',['Type']=kind=='video' and 'Multicam' or 'Audio'} end}
end
local function add(expected,kind,track,uid)
 local row={first=expected.record_start,last=expected.record_end,left=expected.source_start,
   media=media(expected,kind),uid=uid}
 row.GetStart=function(self) return self.first end
 row.GetEnd=function(self) return self.last end
 row.GetLeftOffset=function(self) return self.left end
 row.GetUniqueId=function(self) return self.uid end
 row.GetMediaPoolItem=function(self) return self.media end
 row.enabled=true
 row.SetClipEnabled=function(self,value) self.enabled=value;if not value then stats.disabled_audio=stats.disabled_audio+1 end;return true end
 row.GetClipEnabled=function(self) return self.enabled end
 for number=1,track do items[kind][number]=items[kind][number] or {} end;table.insert(items[kind][track],row)
 return row
end
local targets,rank={},{}
for _,group in ipairs(manifest.groups) do for _,audio in ipairs(group.audio) do targets[audio.track_index or 1]=true end end
local ordered={};for target in pairs(targets) do ordered[#ordered+1]=target end;table.sort(ordered)
for index,target in ipairs(ordered) do rank[target]=index end
for index,group in ipairs(manifest.groups) do
 if group.video then add(group.video,'video',group.video.track_index or 1,'v'..index) end
 if not routed then for sub,audio in ipairs(group.audio) do add(audio,'audio',rank[audio.track_index or 1],'a'..index..'-'..sub) end end
end
local function fault() CHANGE end
if PREFAULT or not routed then fault()end
if routed then items.audio={{}}end
local timeline={start=0}
local enabled_tracks={}
timeline.GetName=function() return 'Diagnostics QA' end
timeline.GetUniqueId=function() return 'new-timeline' end
timeline.GetStartFrame=function(self) return self.start end
timeline.SetStartTimecode=function(self) self.start=manifest.timeline_start;return true end
timeline.GetTrackCount=function(self,kind) return #items[kind] end
timeline.GetItemListInTrack=function(self,kind,track) return items[kind][track] end
timeline.AddTrack=function(self,kind,options)
 if options.index then table.insert(items[kind],options.index,{}) else items[kind][#items[kind]+1]={} end
 return true end
timeline.DeleteTrack=function(self,kind,index)
 if #items[kind]<=1 or #(items[kind][index]or{})>0 then return false end
 table.remove(items[kind],index);return true end
timeline.ImportIntoTimeline=function(self,path,options)
 assert(path:match('%.aaf$') and options.insertAdditionalTracks==false)
 for index,group in ipairs(manifest.groups)do for sub,audio in ipairs(group.audio)do
  add(audio,'audio',audio.track_index or 1,'a'..index..'-'..sub)
 end end
 if not PREFAULT then fault()end
 return true end
timeline.SetClipsLinked=function() stats.links=stats.links+1;return true end
timeline.SetTrackEnable=function(self,kind,track,enabled) enabled_tracks[kind..track]=enabled;return true end
timeline.GetIsTrackEnabled=function(self,kind,track) return enabled_tracks[kind..track] end
local pool={ImportTimelineFromFile=function() return timeline end,
 DeleteTimelines=function() stats.deleted=stats.deleted+1;return true end}
local previous={}
local project={GetMediaPool=function() return pool end,GetCurrentTimeline=function() return previous end,
 GetSetting=function() return tostring(PROJECT_FPS) end,
 GetTimelineCount=function() return 0 end,SetCurrentTimeline=function(self,value)
  if value==previous then stats.restored=stats.restored+1 end;return true end}
Resolve=function() return {GetProjectManager=function() return {GetCurrentProject=function() return project end} end} end
local temporary_count=0
os.tmpname=function() temporary_count=temporary_count+1
 if temporary_count==1 then return LOG end
 return LOG..'-'..temporary_count end
local original_open=io.open
BLOCK_REPORTS
local ok,why=pcall(dofile,HELPER)
local handle=assert(original_open(STATS,'wb'))
handle:write(tostring(ok),'\n',tostring(why),'\n')
for _,key in ipairs({'props','names','uids','links','deleted','restored','disabled_audio'}) do handle:write(key,'=',stats[key],'\n') end
handle:close()
'''
        script = (script.replace('MANIFEST', _lua(manifest)).replace('CHANGE', change)
            .replace('PREFAULT', 'true' if 'items.video' in change and 'items.audio' not in change else 'false').replace('LOG', _lua(str(log))).replace('HELPER', _lua(str(helper))).replace('STATS', _lua(str(stats))))
        script=script.replace('PROJECT_FPS',_lua(initial_fps if initial_fps is not None else manifest['fps']))
        script = script.replace('BLOCK_REPORTS', "io.open=function(path,mode) if path:match('%.resolve%-report%.json$') then error('report volume offline') end;return original_open(path,mode) end" if block_reports else '')
        run_lua(script)
        lines = stats.read_text().splitlines()
        counts = dict(line.split('=',1) for line in lines if '=' in line and line.split('=',1)[0] in ('props','names','uids','links','deleted','restored','disabled_audio'))
        report_path = helper.with_name(helper.name.removesuffix('.resolve.lua')+'.resolve-report.json')
        report = json.loads(report_path.read_text()) if report_path.exists() else json.loads(Path(str(log)+'.json').read_text())
        return report, {key:int(value) for key,value in counts.items()}, log.read_text(), lines

    def test_media_uid_cache_reduces_calls_and_progress_is_local(self):
        _, helper, manifest = self.fixture(count=300)
        report, counts, log, _ = self.execute(manifest,helper)
        self.assertTrue(report['ok'])
        self.assertEqual((counts['props'],counts['names'],counts['uids']),(2,2,600))
        self.assertEqual(counts['links'],300)
        self.assertEqual(report['diagnostics']['counts']['media_cache_hits'],598)
        self.assertIn('inventory_progress 500',log)
        self.assertIn('native_import_returned',log)
        self.assertIn('native_import',report['diagnostics']['timings'])

    def test_same_filename_distinct_media_uids_never_share_properties(self):
        _, helper, manifest = self.fixture(second_sound=True)
        report, counts, _, _ = self.execute(manifest,helper)
        self.assertTrue(report['ok'])
        self.assertEqual((counts['props'],counts['names']),(3,3))

    def test_unsupported_media_uid_safely_disables_cache(self):
        _, helper, manifest = self.fixture()
        change="for _,kind in pairs(items) do for _,track in ipairs(kind) do for _,item in ipairs(track) do item.media.GetUniqueId=nil end end end"
        report, counts, _, _ = self.execute(manifest,helper,change)
        self.assertTrue(report['ok'])
        self.assertEqual((counts['props'],counts['names']),(6,6))
        self.assertEqual(report['diagnostics']['counts']['media_cache_hits'],0)

    def test_empty_media_uid_cannot_merge_unrelated_media(self):
        _, helper, manifest = self.fixture(second_sound=True)
        change="for _,kind in pairs(items) do for _,track in ipairs(kind) do for _,item in ipairs(track) do item.media.GetUniqueId=function() return '' end end end end"
        report, counts, _, _ = self.execute(manifest,helper,change)
        self.assertTrue(report['ok'])
        self.assertEqual((counts['props'],counts['names']),(6,6))

    def test_940_sample_mismatch_is_not_relaxed_and_reports_actual_delta(self):
        _, helper, manifest = self.fixture()
        report, counts, _, _ = self.execute(manifest,helper,"items.audio[1][1].left=items.audio[1][1].left+940/1920")
        failure=report['diagnostics']['lookup_failure']
        self.assertFalse(report['ok'])
        self.assertEqual((failure['kind'],failure['group_index'],failure['match_count']),('audio',1,0))
        self.assertAlmostEqual(failure['candidates'][0]['left_delta']['samples'],940,places=6)
        self.assertEqual((counts['links'],counts['deleted'],counts['restored']),(0,1,1))

    def test_unregistered_cross_track_duplicate_still_blocks(self):
        path, _, _ = self.fixture()
        layout=[dict(index=n,video_track=n,subtype='stereo')for n in (1,2)]
        helper=write_resolve_import_helper(path,audio_layout=layout);manifest=build_resolve_manifest(path,audio_layout=layout)
        change="add(manifest.groups[1].audio[1],'audio',2,'duplicate')"
        report, counts, _, _ = self.execute(manifest,helper,change)
        self.assertFalse(report['ok'])
        self.assertEqual(report['diagnostics']['unexpected_audio'][0]['uid'],'duplicate')
        self.assertEqual(counts['links'],0)

    def test_unexpected_unregistered_audio_blocks_before_linking(self):
        path, _, _ = self.fixture()
        layout=[dict(index=n,video_track=n,subtype='stereo')for n in (1,2)]
        helper=write_resolve_import_helper(path,audio_layout=layout);manifest=build_resolve_manifest(path,audio_layout=layout)
        change="local extra=add(manifest.groups[1].audio[1],'audio',2,'unexpected');extra.first=extra.first+100;extra.last=extra.last+100"
        report, counts, _, _ = self.execute(manifest,helper,change)
        self.assertFalse(report['ok'])
        self.assertEqual(report['diagnostics']['unexpected_audio'][0]['uid'],'unexpected')
        self.assertEqual((counts['links'],counts['deleted'],counts['restored']),(0,1,1))

    def alternate_fixture(self):
        path,_,_=self.fixture(count=3)
        tree=ET.parse(path);spine=tree.find('.//sequence/spine')
        spine.findall('mc-clip')[1].set('lane','1')
        audio=spine.findall('asset-clip')[1];audio.set('lane','-2');audio.set('audioRole','dialogue.pgm-track-2')
        tree.write(path,encoding='utf-8',xml_declaration=True)
        return write_resolve_import_helper(path),build_resolve_manifest(path)

    def test_only_primary_picture_and_actual_paired_sound_tracks_are_enabled(self):
        helper,manifest=self.alternate_fixture()
        report,counts,_,_=self.execute(manifest,helper)
        self.assertTrue(report['ok'])
        state={(r['kind'],r['track']):r['enabled'] for r in report['diagnostics']['track_visibility']}
        self.assertEqual(state,{('video',1):True,('video',2):False,('audio',1):True,('audio',2):False})
        self.assertEqual(counts['links'],3)

    def test_importer_collapsing_distinct_video_audio_tracks_is_rejected(self):
        helper,manifest=self.alternate_fixture()
        change="items.audio[1][#items.audio[1]+1]=items.audio[2][1];items.audio[2]={}"
        report,counts,_,_=self.execute(manifest,helper,change)
        self.assertFalse(report['ok']);self.assertEqual(counts['links'],0)
        self.assertEqual(report['diagnostics']['lookup_failure']['kind'],'audio')

    def test_sparse_video_numbers_insert_empty_matching_audio_slots(self):
        path,_,_=self.fixture(count=2)
        tree=ET.parse(path);spine=tree.find('.//sequence/spine')
        spine.findall('mc-clip')[1].set('lane','2')
        audio=spine.findall('asset-clip')[1];audio.set('lane','-3');audio.set('audioRole','dialogue.pgm-track-3')
        tree.write(path,encoding='utf-8',xml_declaration=True)
        layout=[dict(index=n,video_track=n,subtype='stereo') for n in range(1,5)]
        helper=write_resolve_import_helper(path,audio_layout=layout)
        manifest=build_resolve_manifest(path,audio_layout=layout)
        report,counts,_,_=self.execute(manifest,helper)
        self.assertTrue(report['ok'],report.get('message'))
        self.assertEqual(report['diagnostics']['audio_lanes'],dict(method='explicit_aaf_physical_tracks',planned_tracks=4))
        self.assertEqual(report['diagnostics']['audio_interchange']['track_count'],4)
        state={(r['kind'],r['track']):r['enabled'] for r in report['diagnostics']['track_visibility']}
        self.assertEqual([state['audio',n] for n in range(1,5)],[True,False,False,False])
        self.assertEqual(counts['links'],2)

    def test_native_compression_of_an_empty_video_lane_is_restored_before_audio_linking(self):
        path,_,_=self.fixture(count=2)
        tree=ET.parse(path);spine=tree.find('.//sequence/spine')
        spine.findall('mc-clip')[1].set('lane','2')
        audio=spine.findall('asset-clip')[1];audio.set('lane','-3');audio.set('audioRole','dialogue.pgm-track-3')
        tree.write(path,encoding='utf-8',xml_declaration=True)
        helper=write_resolve_import_helper(path);manifest=build_resolve_manifest(path)
        change="items.video[2]=items.video[3];items.video[3]=nil"
        report,counts,_,_=self.execute(manifest,helper,change)
        self.assertTrue(report['ok'],report.get('message'))
        self.assertEqual(report['diagnostics']['video_track_numbering']['inserted'],[2])
        self.assertEqual(report['diagnostics']['audio_lanes'],dict(method='explicit_aaf_physical_tracks',planned_tracks=3))
        self.assertEqual(counts['links'],2)

    def test_same_wav_source_and_position_on_two_registered_tracks_is_unambiguous(self):
        path,_,_=self.fixture(count=2)
        tree=ET.parse(path);spine=tree.find('.//sequence/spine')
        picture=spine.findall('mc-clip')[1];sound=spine.findall('asset-clip')[1]
        first_picture=spine.findall('mc-clip')[0];first_sound=spine.findall('asset-clip')[0]
        picture.set('lane','1');sound.set('lane','-2');sound.set('audioRole','dialogue.pgm-track-2')
        for key in ('offset','start'):
            picture.set(key,first_picture.get(key));sound.set(key,first_sound.get(key))
        tree.write(path,encoding='utf-8',xml_declaration=True)
        manifest=build_resolve_manifest(path);helper=write_resolve_import_helper(path)
        report,counts,_,_=self.execute(manifest,helper)
        self.assertTrue(report['ok'],report.get('message'));self.assertEqual(counts['links'],2)

    def test_private_document_order_preserves_role_lane_and_all_source_coordinates(self):
        path,_,_=self.fixture(count=3)
        tree=ET.parse(path);spine=tree.find('.//sequence/spine')
        for number,(picture,sound) in zip((1,3,6),zip(spine.findall('mc-clip'),spine.findall('asset-clip'))):
            picture.set('lane',str(number-1));sound.set('lane',str(-number));sound.set('audioRole',f'dialogue.pgm-track-{number}')
        tree.write(path,encoding='utf-8',xml_declaration=True)
        original=path.read_bytes();manifest=build_resolve_manifest(path)
        patches,info=_audio_track_import_plan(original,manifest)
        patches=_audio_document_import_patches(original,manifest,patches)
        private=self.apply_private(original,patches)
        before=ET.fromstring(original).findall('.//project/sequence/spine/asset-clip')
        after=ET.fromstring(private).findall('.//project/sequence/spine/asset-clip')
        self.assertEqual([n.get('lane') for n in after],['-6','-3','-1'])
        self.assertEqual(info['active_tracks'],[1,3,6])
        for public,node in zip(reversed(before),after):self.assertEqual(ET.tostring(public),ET.tostring(node))
        self.assertEqual(path.read_bytes(),original)

    @staticmethod
    def apply_private(original,patches):
        output=[];cursor=0
        for patch in patches:
            output.append(original[cursor:patch['first']])
            if 'parts' in patch:
                output.extend(part['value'].encode() if 'value' in part else original[part['first']:part['last']]
                    for part in patch['parts'])
            else:output.append(patch['value'].encode())
            cursor=patch['last']
        output.append(original[cursor:]);return b''.join(output)

    def test_reordered_private_audio_composes_fractional_bwf_clock_and_record_grid(self):
        path,_,_=self.fixture(count=2,record=F(617,25)+F(7,48000))
        tree=ET.parse(path);spine=tree.find('.//sequence/spine')
        spine.findall('mc-clip')[1].set('lane','1')
        node=spine.findall('asset-clip')[1];node.set('lane','-2');node.set('audioRole','dialogue.pgm-track-2')
        tree.write(path,encoding='utf-8',xml_declaration=True)
        raw=path.read_bytes();public=build_resolve_manifest(path)
        clock=_audio_import_patches(raw,public);native,records,_=_audio_record_import_patches(raw,public)
        lanes,_=_audio_track_import_plan(raw,native)
        patches=_audio_document_import_patches(raw,native,_merge_time_patches(raw,clock['patches'],records,lanes))
        corrected=ET.fromstring(self.apply_private(raw,patches))
        by_role={node.get('audioRole'):node for node in corrected.findall('.//project/sequence/spine/asset-clip')}
        self.assertEqual(len(by_role),2)
        for group in native['groups']:
            for row in group['audio']:
                node=by_role[f'dialogue.pgm-track-{row["track_index"]}']
                asset=corrected.find('./resources/asset[@id="'+node.get('ref')+'"]')
                media_origin=F(asset.get('start')[:-1])*25
                integral=media_origin.numerator//media_origin.denominator
                self.assertEqual(F(node.get('start')[:-1])*25-integral,row['source_start'])
                self.assertEqual(F(node.get('duration')[:-1])*25,row['record_end']-row['record_start'])

    def test_24_fps_project_is_rejected_before_import_or_linking(self):
        _,helper,manifest=self.fixture()
        report,counts,_,_=self.execute(manifest,helper,initial_fps=24)
        self.assertFalse(report['ok']);self.assertIn('导入前项目帧率为 24',report['message'])
        self.assertEqual((counts['links'],counts['deleted']),(0,0))

    def test_neighbor_bucket_is_diagnostic_only_and_never_selected(self):
        _, helper, manifest = self.fixture(record=F(1235,50))
        # Lookup-only numerical edge. Actual AAF media remains on its exact
        # sample clock; this artificial SDK perturbation isolates bucket logic.
        for group in manifest['groups']:
            for row in [group['video']]+group['audio']:
                row['record_start']-=F(1,10000000);row['record_end']-=F(1,10000000)
        manifest.update(audio_import_method='private-aaf',strict_audio_inventory=True)
        # Exercise lookup at a half-frame bucket edge independently of the
        # private AAF import itself; this mock intentionally changes SDK numbers.
        lines=helper.read_text().splitlines()
        self.assertTrue(lines[1].startswith('local manifest='))
        lines[1]='local manifest='+_lua(manifest)
        helper.write_text('\n'.join(lines)+'\n')
        change="items.audio[1][1].first=items.audio[1][1].first+0.0000002;items.audio[1][1].last=items.audio[1][1].last+0.0000002"
        report, counts, _, _ = self.execute(manifest,helper,change)
        failure=report['diagnostics']['lookup_failure']
        self.assertEqual((failure['match_count'],failure['exact_bucket_count']),(0,0))
        self.assertEqual(failure['candidates'][0]['origin'],'neighbor_bucket')
        self.assertTrue(failure['candidates'][0]['first_matches'])
        self.assertEqual(counts['links'],0)

    def test_network_report_failure_keeps_local_diagnostics_and_original_error(self):
        _, helper, manifest = self.fixture()
        report, counts, _, lines = self.execute(manifest,helper,"items.audio[1][1].left=items.audio[1][1].left+1",block_reports=True)
        self.assertFalse(report['ok'])
        self.assertIn('片段来源或位置缺失／重复',report['message'])
        self.assertNotIn('report volume offline',report['message'])
        self.assertEqual(counts['deleted'],1)
        self.assertEqual(lines[0],'false')

    def test_failure_candidates_are_bounded(self):
        _, helper, manifest = self.fixture(count=40)
        report, _, _, _ = self.execute(manifest,helper,"items.audio[1][1].left=items.audio[1][1].left+1")
        failure=report['diagnostics']['lookup_failure']
        self.assertEqual(failure['same_identity_count'],40)
        self.assertEqual(len(failure['candidates']),10)

    def test_missing_or_damaged_private_aaf_blocks_before_native_import(self):
        for damage in ('missing','changed'):
            with self.subTest(damage=damage):
                path,helper,manifest=self.fixture()
                bridge=path.with_suffix('.resolve-audio.aaf')
                if damage=='missing':bridge.unlink()
                else:
                    with bridge.open('ab')as file:file.write(b'damaged transfer')
                report,counts,log,_=self.execute(manifest,helper)
                self.assertFalse(report['ok'])
                self.assertIn('内部声音导入文件',report['message'])
                self.assertEqual((counts['links'],counts['deleted']),(0,0))
                self.assertNotIn('native_import_returned',log)


if __name__ == '__main__':
    unittest.main()
