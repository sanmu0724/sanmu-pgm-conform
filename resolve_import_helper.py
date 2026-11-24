"""Generate a validated Resolve import step for original-media multicam edits.

The XML retains original media, source clocks and exact audio sample positions.
The companion restores the requested record clock, validates imported picture
and audio placement, and restores links and split-mono pans without rebuilding
or rewriting any media.
"""
from __future__ import annotations

from fractions import Fraction as F
from pathlib import Path
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
import zlib
import tempfile
import re
import logging
import copy
import bisect
from xml.parsers import expat

HEADER = '-- 三木的PGM回套软件 Resolve 导入助手 v1'
MENU_NAME = '三木_导入多机位XML.lua'
MENU_FILENAME = MENU_NAME
MANAGED_MARKER = '-- 三木的PGM回套软件 Resolve XML 导入菜单 v1'
_TIMED = {'clip', 'mc-clip', 'asset-clip', 'ref-clip', 'gap', 'video', 'audio', 'spine'}
_AUDIO_ROUTE = re.compile(r'^dialogue\.pgm-track-([1-9][0-9]*)$')


def companion_path(xml_path):
    return Path(xml_path).with_suffix('.resolve.lua')


def _project_opening(raw, project):
    """Locate the real project opening tag, excluding text inside XML comments."""
    parser = expat.ParserCreate()
    positions = []
    def start(name, _attrs):
        if name == 'project':
            positions.append(parser.CurrentByteIndex)
    parser.StartElementHandler = start
    parser.Parse(raw, True)
    if len(positions) != 1:
        raise _analysis_error('导入助手需要仅包含一个项目的 XML。')
    first = positions[0]
    quote = None
    last = first
    while last < len(raw):
        value = raw[last]
        if quote is not None:
            if value == quote:
                quote = None
        elif value in (34, 39):
            quote = value
        elif value == 62:
            break
        last += 1
    if last == len(raw):
        raise _analysis_error('无法读取 XML 的项目标识。')
    attrs = {key: value for key, value in project.attrib.items() if key not in ('name', 'uid')}
    prefix = ET.tostring(ET.Element('project', attrs), encoding='unicode').removesuffix(' />')
    return first, last + 1, prefix


def _opening_attribute_spans(raw, first):
    """Return quoted value byte spans and an insertion point for one tag."""
    spaces=b' \t\r\n';position=first+1;size=len(raw)
    while position<size and raw[position] not in spaces+b'/>':position+=1
    spans={}
    while position<size:
        while position<size and raw[position] in spaces:position+=1
        if position>=size:break
        if raw[position] in b'/>':return spans,position
        begin=position
        while position<size and raw[position] not in spaces+b'=':position+=1
        name=raw[begin:position].decode('utf-8')
        while position<size and raw[position] in spaces:position+=1
        if position>=size or raw[position]!=61:break
        position+=1
        while position<size and raw[position] in spaces:position+=1
        if position>=size or raw[position] not in (34,39):break
        quote=raw[position];begin=position+1;position=begin
        while position<size and raw[position]!=quote:position+=1
        if position>=size:break
        spans[name]=(begin,position);position+=1
    raise _analysis_error('无法定位 XML 声音时码属性，未生成导入脚本。')


def _audio_import_patches(raw, manifest):
    """Plan private import-only WAV clock corrections in original byte space.

    Resolve measures audio left offsets from the integral-frame media clock.
    The public XML and manifest instead retain the physical BWF sample origin.
    Only the private import copy shifts referenced WAV source coordinates;
    assets, media files and the expected physical sample positions stay exact.
    """
    fps=F(manifest['fps'])
    if fps<=0:raise _analysis_error('声音导入补偿缺少有效帧率。')
    parser=expat.ParserCreate();nodes=[];stack=[]
    def start(tag,attrs):
        node=dict(tag=tag,attrs=attrs,first=parser.CurrentByteIndex,children=[])
        if stack:stack[-1]['children'].append(node)
        nodes.append(node);stack.append(node)
    parser.StartElementHandler=start
    parser.EndElementHandler=lambda _:stack.pop()
    try:parser.Parse(raw,True)
    except expat.ExpatError as exc:raise _analysis_error('无法读取声音导入 XML：'+str(exc)) from exc
    deltas={}
    referenced={node['attrs'].get('ref') for node in nodes if node['tag'] in _TIMED}
    for node in nodes:
        attrs=node['attrs']
        if (node['tag']!='asset' or attrs.get('id') not in referenced or
            attrs.get('hasAudio')!='1' or attrs.get('hasVideo')=='1'):continue
        src=attrs.get('src') or next((child['attrs'].get('src','') for child in node['children']
            if child['tag']=='media-rep' and child['attrs'].get('kind')=='original-media'),'')
        if Path(unquote(urlsplit(src).path)).suffix.lower() not in ('.wav','.wave'):continue
        try:rate=int(attrs.get('audioRate') or 48000)
        except (ValueError,TypeError) as exc:raise _analysis_error('声音素材采样率无效，未生成导入脚本。') from exc
        if rate<=0:raise _analysis_error('声音素材采样率无效，未生成导入脚本。')
        # Native exports can approximate an exact BWF origin by <0.1 sample.
        # Recover the integer sample before modulo, especially just below an
        # integral frame where direct modulo would invent nearly a whole frame.
        exact_samples=_time(attrs.get('start'))*rate
        samples=round(exact_samples)
        if abs(exact_samples-samples)>F(1,10):
            raise _analysis_error('WAV 素材起点不是可确认的整数音频采样，暂不能安全转换导入时基；请保留当前 XML。')
        origin=F(samples,rate)*fps;residual=origin-(origin.numerator//origin.denominator)
        if residual*rate/fps<=F(1,10):continue
        identity=attrs.get('id')
        if not identity:raise _analysis_error('声音素材缺少资源编号，未生成导入脚本。')
        deltas[identity]=residual/fps
    changes={};shifted=set();child_offsets=0;marker_starts=0;used_assets=set()
    markers={'marker','chapter-marker','rating','keyword','analysis-marker'}
    def shift(node,attribute,delta):
        values=changes.setdefault(node['first'],dict(node=node,values={}))['values']
        value=_time(node['attrs'].get(attribute))-delta
        if attribute in values and values[attribute]!=value:
            raise _analysis_error('XML 声音导入时码补偿冲突，未生成导入脚本。')
        values[attribute]=value
    def check_keyframes(host):
        pending=list(host['children'])
        while pending:
            child=pending.pop()
            # A nested timed clip owns a separate source domain. Its offset is
            # adjusted below, but its effects are not in the parent's domain.
            if child['tag'] in _TIMED or child['tag']=='metadata':continue
            if child['tag']=='keyframe' and 'time' in child['attrs']:
                raise _analysis_error('需补偿源时码的 WAV 片段含时间关键帧，暂不能安全生成导入脚本；请保留当前 XML。')
            pending.extend(child['children'])
    for node in nodes:
        delta=deltas.get(node['attrs'].get('ref'))
        if delta is None or node['tag'] not in _TIMED:continue
        check_keyframes(node)
        shift(node,'start',delta);shifted.add(node['first']);used_assets.add(node['attrs']['ref'])
        for child in node['children']:
            if child['tag'] in _TIMED:
                shift(child,'offset',delta);child_offsets+=1
            elif child['tag'] in markers:
                shift(child,'start',delta);marker_starts+=1
    patches=[]
    for first,change in changes.items():
        spans,insertion=_opening_attribute_spans(raw,first);extra=[]
        for attribute,value in sorted(change['values'].items()):
            text=f'{value.numerator}/{value.denominator}s'
            if attribute in spans:
                begin,end=spans[attribute];patches.append(dict(first=begin,last=end,value=text))
            else:extra.append(f' {attribute}="{text}"')
        if extra:patches.append(dict(first=insertion,last=insertion,value=''.join(extra)))
    patches.sort(key=lambda row:(row['first'],row['last']))
    previous=None
    for patch in patches:
        if (patch['first']<0 or patch['last']<patch['first'] or patch['last']>len(raw) or
            (previous is not None and (patch['first']<previous['last'] or patch['first']==previous['first']))):
            raise _analysis_error('XML 声音导入补丁范围重叠，未生成导入脚本。')
        previous=patch
    return dict(patches=patches,assets=len(used_assets),nodes=len(shifted),
                child_offsets=child_offsets,marker_starts=marker_starts)


def _audio_record_import_patches(raw, manifest):
    """Private Resolve adapter: WAV only, sample-safe source clocks, frame INs.

    Resolve floors FCPXML audio record INs. Trim WAV record IN up and OUT down
    to the frame grid and move source IN by the same amount. This retains the
    original synchronization of every sample used; the unused slivers stay
    silent. Historical PGM fallback leaves are omitted, never substituted by
    another sound source. Public XML, WAV headers and saved sync plans stay exact.
    """
    native=copy.deepcopy(manifest)
    report=dict(rounded_wav_clip_count=0,dropped_subframe_wav_count=0,
                dropped_pgm_clip_count=0,adjustments=[])
    fps=F(manifest['fps'])
    if not manifest.get('routing_enabled'):return native,[],report
    native['strict_audio_inventory']=True
    root=ET.fromstring(raw);sequence=root.find('.//project/sequence')
    spine=sequence.find('spine') if sequence is not None else None
    if spine is None:return native,[],report
    shift=F(manifest['timeline_start'])-_time(sequence.get('tcStart'))*fps
    assets={row.get('id'):row for row in root.findall('./resources/asset')}
    bypath={}
    for asset in assets.values():
        if asset.get('hasAudio')!='1':continue
        try:path=_asset_path(asset)
        except ValueError:continue
        bypath.setdefault(path,[]).append(asset)
    def is_pgm(path):return any(_metadata(asset).get('com.pgm.audioSourceKind')=='pgm' for asset in bypath.get(path,()))
    def is_wav(path):return Path(path).suffix.lower() in ('.wav','.wave')
    def key(row):return (row.get('link_group_id'),row['path'],row['record_start'],row['record_end'],row['source_start'],row.get('track_index'))
    changes={}
    for number,(original,target) in enumerate(zip(manifest['groups'],native['groups']),1):
        retained=[]
        for row,updated in zip(original['audio'],target['audio']):
            drop=is_pgm(row['path'])
            first,last=F(row['record_start']),F(row['record_end'])
            if is_wav(row['path']):
                first=F(-(-first.numerator//first.denominator));last=F(last.numerator//last.denominator)
            local=row['source_start']+first-row['record_start']
            adjusted=(first,last)!=(row['record_start'],row['record_end'])
            if drop:
                report['dropped_pgm_clip_count']+=1
            elif adjusted and last<=first:
                if row['record_end']-row['record_start']>=2:
                    raise _analysis_error('声音片段无法安全落在视频帧边界，未转换导入。')
                drop=True;report['dropped_subframe_wav_count']+=1
            elif adjusted:
                durations=[_time(asset.get('duration'))*fps for asset in bypath.get(row['path'],())]
                if local<0 or (durations and local+last-first>max(durations)+F(1,1000000)):
                    raise _analysis_error('声音帧边界会超出原始 WAV 范围，未转换导入。')
                updated.update(record_start=first,record_end=last,source_start=local)
                report['rounded_wav_clip_count']+=1
            if not drop:retained.append(updated)
            if not drop and not adjusted:continue
            identity=key(row)
            if not identity[0] or identity in changes:
                raise _analysis_error('声音片段缺少唯一身份，未转换导入。')
            changes[identity]=None if drop else (first-row['record_start'],last-row['record_end'])
            report['adjustments'].append(dict(group_index=number,path=row['path'],omitted=drop,
                reason='pgm_audio_disabled' if is_pgm(row['path']) else 'resolve_audio_frame_grid',
                original_start=row['record_start'],original_end=row['record_end'],
                import_start=None if drop else first,import_end=None if drop else last))
        target['audio']=retained
    native['groups']=[group for group in native['groups'] if group['video'] is not None or group['audio']]
    if not changes:return native,[],report
    positions=[];stack=[];parser=expat.ParserCreate()
    def opening(tag,attrs):
        row=[tag,parser.CurrentByteIndex,None];positions.append(row);stack.append(row)
    def closing(tag):
        row=stack.pop();_,insert=_opening_attribute_spans(raw,row[1])
        row[2]=insert+2 if raw[insert:insert+2]==b'/>' else raw.index(b'>',parser.CurrentByteIndex)+1
    parser.StartElementHandler=opening;parser.EndElementHandler=closing;parser.Parse(raw,True)
    elements=list(root.iter())
    if len(elements)!=len(positions) or any(node.tag!=tag for node,(tag,_,_) in zip(elements,positions)):
        raise _analysis_error('无法定位声音 XML，未转换导入。')
    locations={id(node):(first,last) for node,(_,first,last) in zip(elements,positions)}
    patches=[];seen=set();deleted_refs=set();deleted_nodes=set()
    for node in spine:
        if node.tag!='asset-clip' or node.get('srcEnable')!='audio':continue
        asset=assets.get(node.get('ref'));route=_AUDIO_ROUTE.fullmatch(node.get('audioRole') or node.get('role') or '')
        if asset is None or route is None:continue
        first=_time(node.get('offset'))*fps+shift;last=first+_time(node.get('duration'))*fps
        local=(_time(node.get('start'))-_time(asset.get('start')))*fps
        identity=(_metadata(node).get('com.pgm.linkGroupID'),_asset_path(asset),first,last,local,int(route.group(1)))
        if identity not in changes:continue
        if identity in seen or any(child.tag in _TIMED or child.tag in ('marker','chapter-marker') or child.find('.//keyframe') is not None for child in node):
            raise _analysis_error('声音包含无法安全转换的嵌套编辑，未转换导入。')
        seen.add(identity)
        if changes[identity] is None:
            begin,end=locations[id(node)];patches.append(dict(first=begin,last=end,value=''))
            deleted_refs.add(node.get('ref'));deleted_nodes.add(id(node));continue
        delta_in,delta_out=changes[identity];spans,_=_opening_attribute_spans(raw,locations[id(node)][0])
        for attr,value in dict(offset=_time(node.get('offset'))+delta_in/fps,start=_time(node.get('start'))+delta_in/fps,
                               duration=_time(node.get('duration'))+(delta_out-delta_in)/fps).items():
            if attr not in spans:raise _analysis_error('声音片段属性不完整，未转换导入。')
            begin,end=spans[attr];patches.append(dict(first=begin,last=end,value=f'{value.numerator}/{value.denominator}s'))
    if seen!=set(changes):raise _analysis_error('声音不是可核对的平面片段，未转换导入。')
    # Remove unused historical PGM sound assets from the private copy too. Their
    # video assets, if any, remain independently referenced by the multicams.
    remaining_refs={node.get('ref') for node in root.iter() if node.get('ref') and id(node) not in deleted_nodes}
    removed_paths=set()
    for ref in deleted_refs-remaining_refs:
        asset=assets[ref]
        if not is_pgm(_asset_path(asset)):continue
        begin,end=locations[id(asset)];patches.append(dict(first=begin,last=end,value=''));removed_paths.add(_asset_path(asset))
    needed_paths={row['path'] for group in native['groups'] for row in group['audio']}
    for asset in assets.values():
        if asset.get('hasVideo')=='1':
            try:needed_paths.add(_asset_path(asset))
            except ValueError:pass
    native['media_paths']=[path for path in native['media_paths'] if path not in removed_paths or path in needed_paths]
    return native,patches,report


def _merge_time_patches(raw,*plans):
    """Compose additive time-coordinate corrections on the same attribute."""
    merged={}
    for patches in plans:
        for patch in patches:
            key=patch['first'],patch['last']
            if key in merged:
                if key[0]==key[1]:
                    merged[key]=dict(first=key[0],last=key[1],value=merged[key]['value']+patch['value'])
                else:
                    value=_time(merged[key]['value'])+_time(patch['value'])-_time(raw[key[0]:key[1]].decode('utf-8'))
                    merged[key]=dict(first=key[0],last=key[1],value=f'{value.numerator}/{value.denominator}s')
            else:merged[key]=dict(patch)
    result=[]
    for patch in sorted(merged.values(),key=lambda row:(row['first'],-row['last'])):
        if result and patch['first']<result[-1]['last']:
            if result[-1]['value']=='' and patch['last']<=result[-1]['last']:continue
            raise _analysis_error('声音导入补丁重叠，未转换导入。')
        result.append(patch)
    return result


def _audio_track_import_plan(raw, manifest):
    """Plan occupied native track numbers; never change public role identities.

    Resolve assigns audio tracks in reverse first-encounter order, ignoring
    the sign and value of the public lane/role labels. A separate private byte
    range plan reorders whole sound elements without changing any source time.
    """
    if not manifest.get('routing_enabled'):
        return [], dict(active_tracks=[], lane_count=0)
    active=sorted({row['track_index'] for group in manifest['groups'] for row in group['audio']})
    ranks={target:index for index,target in enumerate(active,1)}
    for group in manifest['groups']:
        for row in group['audio']:
            row['import_track_index']=ranks[row['track_index']]
    manifest['numbered_audio_tracks']=True
    manifest['import_audio_tracks']=[dict(index=index,target_index=target) for target,index in ranks.items()]
    return [],dict(active_tracks=active,lane_count=len(active),policy='reverse_first_encounter_document_order')


def _audio_document_import_patches(raw,manifest,patches):
    """Move private top-level sound elements by ranges, not duplicated XML text.

    The generated Lua streams unchanged source byte ranges and sparse time
    patches. Only element order changes; record/source attributes, audioRole,
    metadata and the public XML remain intact. Deleted audio stays deleted.
    """
    if not manifest.get('numbered_audio_tracks'):return patches
    parser=expat.ParserCreate();stack=[];sounds=[];spine_close=None
    def opening(tag,attrs):
        parent=stack[-1] if stack else None
        row=dict(tag=tag,attrs=attrs,first=parser.CurrentByteIndex,main_spine=False)
        row['main_spine']=tag=='spine' and parent is not None and parent['tag']=='sequence' and any(p['tag']=='project' for p in stack)
        if tag=='asset-clip' and parent is not None and parent['main_spine'] and attrs.get('srcEnable')=='audio':
            route=_AUDIO_ROUTE.fullmatch(attrs.get('audioRole') or attrs.get('role') or '')
            if route:row['target']=int(route.group(1));sounds.append(row)
        stack.append(row)
    def closing(tag):
        nonlocal spine_close
        row=stack.pop()
        if row['main_spine']:spine_close=parser.CurrentByteIndex
        if 'target' not in row:return
        _,insert=_opening_attribute_spans(raw,row['first'])
        row['last']=insert+2 if raw[insert:insert+2]==b'/>' else raw.index(b'>',parser.CurrentByteIndex)+1
    parser.StartElementHandler=opening;parser.EndElementHandler=closing;parser.Parse(raw,True)
    if not sounds:return patches
    if spine_close is None:raise _analysis_error('无法定位声音所属的主时间线，未转换导入。')
    result=[];parts=[];inner={id(row):[] for row in sounds};omitted=set()
    # Both lists are sorted in the same original byte domain; linear sweep
    # avoids an O(number_of_clips * number_of_patches) export-time scan.
    index=0
    for patch in patches:
        while index<len(sounds) and sounds[index]['last']<=patch['first']:index+=1
        row=sounds[index] if index<len(sounds) else None
        if row is None or patch['last']<=row['first']:
            result.append(patch);continue
        if patch['first']<row['first'] or patch['last']>row['last']:
            raise _analysis_error('声音顺序补丁跨越片段边界，未转换导入。')
        if patch['first']==row['first'] and patch['last']==row['last'] and patch['value']=='':omitted.add(id(row))
        else:inner[id(row)].append(patch)
    for row in sorted(sounds,key=lambda item:(-item['target'],item['first'])):
        result.append(dict(first=row['first'],last=row['last'],value=''))
        if id(row) in omitted:continue
        cursor=row['first']
        for patch in inner[id(row)]:
            if patch['first']>cursor:parts.append(dict(first=cursor,last=patch['first']))
            if patch['value']:parts.append(dict(value=patch['value']))
            cursor=patch['last']
        if cursor<row['last']:parts.append(dict(first=cursor,last=row['last']))
    if parts:result.append(dict(first=spine_close,last=spine_close,parts=parts))
    result.sort(key=lambda row:(row['first'],row['last']))
    return result


def _sample_audio_import_plan(raw, manifest):
    """Split private picture import from explicitly numbered, sample-rate audio.

    Resolve packs FCPXML audio into gaps even when roles and lanes name tracks.
    Removing audio only in the private picture copy lets the AAF adapter supply
    exact physical An slots without moving any source/record sample boundary.
    """
    native=copy.deepcopy(manifest)
    root=ET.fromstring(raw)
    assets={node.get('id'):node for node in root.findall('./resources/asset')}
    pgm_paths=set()
    video_paths=set()
    for asset in assets.values():
        if asset.get('hasVideo')=='1':video_paths.add(_asset_path(asset))
        if _metadata(asset).get('com.pgm.audioSourceKind')=='pgm':pgm_paths.add(_asset_path(asset))
    dropped=0
    for group in native['groups']:
        retained=[]
        for row in group['audio']:
            if row['path'] in pgm_paths:
                dropped+=1
                continue
            if Path(row['path']).suffix.lower() not in ('.wav','.wave','.aif','.aiff','.aifc'):
                raise _analysis_error('编号音轨暂不支持此声音格式，请保留原始 PCM WAV／AIFF：'+row['path'])
            row['import_track_index']=row['track_index']
            retained.append(row)
        group['audio']=retained
    native['groups']=[group for group in native['groups'] if group['video'] is not None or group['audio']]
    needed={row['path'] for group in native['groups'] for row in group['audio']}|video_paths
    native['media_paths']=[path for path in native['media_paths'] if path not in pgm_paths or path in needed]
    total=max([0]+[row['index'] for row in native.get('audio_tracks',[])]+
              [group['video']['track_index'] for group in native['groups'] if group['video']])
    tracks={row['index']:row for row in native.get('audio_tracks',[])}
    native['audio_tracks']=[dict(tracks.get(index,{}),index=index,subtype='stereo') for index in range(1,total+1)]
    native.update(strict_audio_inventory=True,audio_import_method='private-aaf',numbered_audio_tracks=False)
    parser=expat.ParserCreate();nodes=[];stack=[]
    def opening(tag,attrs):
        parent=stack[-1] if stack else None
        node=dict(tag=tag,attrs=attrs,first=parser.CurrentByteIndex,parent=parent)
        nodes.append(node);stack.append(node)
    def closing(tag):
        node=stack.pop();_,insert=_opening_attribute_spans(raw,node['first'])
        node['last']=insert+2 if raw[insert:insert+2]==b'/>' else raw.index(b'>',parser.CurrentByteIndex)+1
    parser.StartElementHandler=opening;parser.EndElementHandler=closing;parser.Parse(raw,True)
    deleted=[]
    picture_positions=[node['first'] for node in nodes if node['tag'] in ('video','mc-clip','ref-clip')]
    for node in nodes:
        asset=assets.get(node['attrs'].get('ref'))
        if node['tag'] not in ('audio','asset-clip') or asset is None:continue
        if asset.get('hasAudio')!='1' or asset.get('hasVideo')=='1':continue
        if deleted and node['first']<deleted[-1][1]:continue
        # Connected picture children cannot be silently removed with a sound host.
        position=bisect.bisect_right(picture_positions,node['first'])
        if position<len(picture_positions) and picture_positions[position]<node['last']:
            raise _analysis_error('声音宿主含连接画面，不能安全拆分内部导入。')
        deleted.append((node['first'],node['last']))
    patches=[dict(first=first,last=last,value='') for first,last in deleted]
    return native,patches,dict(method='explicit_sample_rate_aaf',removed_audio_nodes=len(deleted),
        dropped_pgm_clip_count=dropped,rounded_wav_clip_count=0,dropped_subframe_wav_count=0)


def _analysis_error(message):
    # The offline installer imports this module before installing av/PyQt.
    # Reuse the application's error type when it is already running; otherwise
    # keep bootstrap validation completely independent of optional packages.
    import sys
    core=sys.modules.get('core')
    error=getattr(core,'AnalysisError',ValueError)
    return error(message)


def _time(value, default=F(0)):
    if value is None:
        return default
    try:
        return F(str(value).removesuffix('s'))
    except (ValueError, ZeroDivisionError) as exc:
        raise _analysis_error('XML 包含无效的有理数时间：' + str(value)) from exc


def _path(value):
    parts = urlsplit(str(value))
    if parts.scheme:
        if parts.scheme != 'file' or parts.netloc not in ('', 'localhost'):
            raise _analysis_error('声音链接助手只支持本机挂载的媒体路径：' + str(value))
        value = unquote(parts.path)
    return str(Path(str(value)).expanduser().resolve())


def _metadata(asset):
    return {row.get('key'): row.get('value', '') for row in asset.findall('./metadata/md')}


def _asset_path(asset):
    value = asset.get('src') or next((row.get('src') for row in asset.findall('media-rep')
                                    if row.get('kind') == 'original-media'), '')
    if not value:
        raise _analysis_error('XML 媒体缺少原始路径：' + str(asset.get('name', '')))
    return _path(value)


def build_resolve_manifest(xml_path, guide_audio=(), audio_layout=()):
    """Read one final FCPXML, preserving exact rational source/record positions.

    No media is decoded or rewritten. A group is one picture interval (or one
    explicit gap) and its own connected audio; unrelated overlapping scenes
    are separate groups even if they share original WAV paths.
    """
    path = Path(xml_path)
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError) as exc:
        raise _analysis_error('无法读取多机位 XML：' + str(exc)) from exc
    root = tree.getroot()
    requested_clocks = [node.get('value') for node in root.findall('.//project/metadata/md')
                        if node.get('key') == 'com.pgm.requestedTimelineStart']
    if len(requested_clocks) > 1:
        raise _analysis_error('XML 存在冲突的时间线起始时码。')
    routed = any(_AUDIO_ROUTE.fullmatch(node.get('role') or node.get('audioRole') or '')
                 for node in root.iter() if node.tag in ('audio', 'asset-clip'))
    routed = routed or bool(requested_clocks)
    guide_audio=list(guide_audio or ())
    guide_candidates={_path(row['path']) for row in guide_audio if isinstance(row,dict) and row.get('path')
        and row.get('pair_id') and (row.get('channel') in ('L','R') or row.get('out_channel') in ('L','R'))}
    asset_elements=root.findall('./resources/asset')
    xml_has_pair=any(row.get('key')=='com.pgm.audioPairID' and row.get('value')
        for asset in asset_elements for row in asset.findall('./metadata/md'))
    guide_has_pair=False
    if guide_candidates:
        for asset in asset_elements:
            try:guide_has_pair=_asset_path(asset) in guide_candidates
            except ValueError:continue
            if guide_has_pair:break
    if not xml_has_pair and not guide_has_pair and not routed:
        return None
    sequences = root.findall('.//project/sequence')
    if len(sequences) != 1:
        raise _analysis_error('声音链接助手需要仅包含一条时间线的 FCPXML。')
    sequence = sequences[0]
    resources = {row.get('id'): row for row in root.findall('./resources/*') if row.get('id')}
    format_ = resources.get(sequence.get('format'))
    if format_ is None or not format_.get('frameDuration'):
        raise _analysis_error('XML 缺少时间线帧率。')
    fps = 1 / _time(format_.get('frameDuration'))
    if fps <= 0:
        raise _analysis_error('XML 时间线帧率无效。')
    guide = {}
    for row in guide_audio or ():
        if not isinstance(row, dict) or not row.get('path') or not row.get('pair_id'):
            continue
        role = row.get('channel') if row.get('channel') in ('L','R') else row.get('out_channel')
        if role not in ('L', 'R'):
            continue
        key, value = _path(row['path']), (str(row['pair_id']), role)
        if key in guide and guide[key] != value:
            raise _analysis_error('同一原始 WAV 的左右声道身份不一致：' + key)
        guide[key] = value
    assets, media_paths = {}, set()
    for identity, asset in resources.items():
        if asset.tag != 'asset':
            continue
        try:file_path,path_error=_asset_path(asset),''
        except ValueError as exc:file_path,path_error=None,str(exc)
        metadata = _metadata(asset)
        pair = metadata.get('com.pgm.audioPairID', '')
        role = metadata.get('com.pgm.audioPairChannel', '')
        if file_path in guide:
            supplied = guide[file_path]
            if pair and role in ('L', 'R') and (pair, role) != supplied:
                raise _analysis_error('XML 与原声音对应表的配对身份冲突：' + file_path)
            pair, role = supplied
        assets[identity] = dict(path=file_path, path_error=path_error,start=_time(asset.get('start')),
            pair_id=pair, role=role, name=asset.get('name', ''),
            channels=int(asset.get('audioChannels') or 1), sample_rate=int(asset.get('audioRate') or 48000),
            video=asset.get('hasVideo') == '1', audio=asset.get('hasAudio') == '1')
    groups = []
    used_mc_names = {}
    resource_path_cache={}

    def usable_asset(identity):
        asset=assets[identity]
        if not asset['path']:raise _analysis_error(asset['path_error'])
        return asset

    def resource_paths(identity,stack=()):
        if identity in resource_path_cache:return resource_path_cache[identity]
        if identity in stack:raise _analysis_error('XML 媒体资源存在循环引用。')
        resource=resources.get(identity)
        if identity in assets:
            result={usable_asset(identity)['path']}
        elif resource is not None:
            result=set()
            for element in resource.findall('.//*[@ref]'):
                result.update(resource_paths(element.get('ref'),stack+(identity,)))
        else:
            raise _analysis_error('XML 片段引用了不存在的媒体资源：'+str(identity))
        resource_path_cache[identity]=result
        return result

    def bounds(node, origin, parent_bounds):
        first = origin + _time(node.get('offset'))
        duration = _time(node.get('duration'), parent_bounds[1] - first)
        last = first + duration
        return first, max(first, parent_bounds[0]), min(last, parent_bounds[1])

    def picture(node, origin, parent_bounds, video_track=1):
        if node.tag == 'mc-clip':
            resource = resources.get(node.get('ref'))
            if resource is None or resource.find('multicam') is None:
                raise _analysis_error('XML 多机位片段缺少对应资源。')
            name = resource.get('name') or node.get('name')
            if not name:
                raise _analysis_error('XML 多机位资源缺少名称。')
            prior = used_mc_names.setdefault(name, node.get('ref'))
            if prior != node.get('ref'):
                raise _analysis_error('不同场次的多机位资源名称重复，不能安全恢复声音链接：' + name)
            media_start = _time(resource.find('multicam').get('tcStart'))
            media_paths.update(resource_paths(node.get('ref')))
            identity = dict(kind='multicam', name=name, ref=node.get('ref'))
        elif node.tag in ('video', 'asset-clip') and node.get('ref') in assets and assets[node.get('ref')]['video']:
            asset = usable_asset(node.get('ref'))
            media_start = asset['start']
            media_paths.add(asset['path'])
            identity = dict(kind='video', path=asset['path'], name=asset['name'])
        else:
            return None
        raw, first, last = bounds(node, origin, parent_bounds)
        if last <= first:
            return None
        identity.update(record_start=first*fps, record_end=last*fps,
                        source_start=(_time(node.get('start')) + first-raw-media_start)*fps)
        if routed:
            identity['track_index'] = video_track
            identity['link_group_id'] = _metadata(node).get('com.pgm.linkGroupID', '')
        return identity

    def visit(node, origin, parent_bounds, host=None, picture_node=None,resource_stack=(), video_track=1):
        if node.tag not in _TIMED:
            return
        raw, first, last = bounds(node, origin, parent_bounds)
        if last <= first:
            return
        local_origin = raw - _time(node.get('start'))
        own = None
        if node is not picture_node:
            own = picture(node, origin, parent_bounds, video_track)
        direct = None
        if own is None and node.tag in ('clip', 'gap'):
            direct_pictures = [(child, picture(child, local_origin, (first,last), video_track))
                               for child in node if child.tag in ('mc-clip','video','asset-clip')]
            direct_pictures = [(child,value) for child,value in direct_pictures if value is not None]
            if len(direct_pictures) > 1:
                raise _analysis_error('同一宿主包含多个画面，不能安全恢复左右声音链接。')
            if direct_pictures:
                direct, own = direct_pictures[0]
        if own is not None:
            host = dict(video=own, audio=[])
            groups.append(host)
            picture_node = direct if direct is not None else node
        elif node.tag == 'gap':
            host = dict(video=None, audio=[])
            groups.append(host)
            picture_node = None
        if node.tag in ('audio','asset-clip') and node.get('ref') in assets:
            asset = usable_asset(node.get('ref'))
            if asset['audio'] and not asset['video']:
                if host is None:
                    host = dict(video=None, audio=[])
                    groups.append(host)
                media_paths.add(asset['path'])
                audio_row = dict(path=asset['path'], pair_id=asset['pair_id'], role=asset['role'],
                    record_start=first*fps, record_end=last*fps,
                    source_start=(_time(node.get('start'))+first-raw-asset['start'])*fps)
                if routed:
                    route = _AUDIO_ROUTE.fullmatch(node.get('role') or node.get('audioRole') or '')
                    if route is None:
                        raise _analysis_error('手动时间线的声音缺少明确的轨道分配：' + asset['path'])
                    channels = [part.strip() for part in node.get('srcCh', '').split(',') if part.strip()]
                    audio_row.update(track_index=int(route.group(1)),
                        channels=len(channels) if channels else asset['channels'], sample_rate=asset['sample_rate'],
                        link_group_id=_metadata(node).get('com.pgm.linkGroupID', ''))
                host['audio'].append(audio_row)
        if node.tag=='ref-clip':
            identity=node.get('ref')
            if identity in resource_stack:raise _analysis_error('XML 复合片段存在循环引用。')
            resource=resources.get(identity)
            nested=resource.find('./sequence/spine') if resource is not None else None
            if nested is None:raise _analysis_error('XML 复合片段缺少可核对的内部时间线。')
            media_paths.update(resource_paths(identity))
            for child in nested:
                visit(child,local_origin,(first,last),host,picture_node,resource_stack+(identity,),video_track)
        for child in node:
            if child.tag in _TIMED:
                # A connected alternative can extend past the underlying shot.
                # Audio leaves still inherit their own wrapper's trim bounds.
                connected_picture=child.get('lane') is not None and (
                    child.tag=='mc-clip' or child.find('mc-clip') is not None or
                    (child.tag=='asset-clip' and child.get('ref') in assets and assets[child.get('ref')]['video']))
                child_track = int(child.get('lane')) + 1 if connected_picture and int(child.get('lane')) > 0 else video_track
                visit(child,local_origin,(start,end) if connected_picture else (first,last),host,picture_node,resource_stack,child_track)

    start = _time(sequence.get('tcStart'))
    end = start + _time(sequence.get('duration'))
    spine = sequence.find('spine')
    if spine is None:
        raise _analysis_error('XML 缺少时间线主轨。')
    for child in spine:
        lane = int(child.get('lane', '0'))
        visit(child, F(0), (start,end), video_track=lane + 1 if lane > 0 else 1)
    if routed:
        linked, unlabelled = {}, []
        primary_link_ids={_metadata(node).get('com.pgm.linkGroupID') for node in spine
            if node.tag in ('mc-clip','clip','gap') and int(node.get('lane','0'))<=0}
        primary_link_ids.discard(None);primary_link_ids.discard('')
        for group in groups:
            identifiers = {row.get('link_group_id') for row in group['audio']}
            if group['video'] is not None:
                identifiers.add(group['video'].get('link_group_id'))
            identifiers.discard('')
            identifiers.discard(None)
            if len(identifiers) > 1:
                raise _analysis_error('同一声音组包含冲突的画面身份。')
            if not identifiers:
                unlabelled.append(group)
                continue
            identity = next(iter(identifiers))
            target = linked.setdefault(identity, dict(video=None, audio=[]))
            if group['video'] is not None:
                if target['video'] is not None:
                    raise _analysis_error('同一声音组存在多个画面，不能安全恢复链接。')
                target['video'] = group['video']
            target['audio'].extend(group['audio'])
        groups = list(linked.values()) + unlabelled
    result, all_identities = [], set()
    for group in groups:
        if routed and group['video'] is None and not group['audio']:
            continue
        paired = [row for row in group['audio'] if row['pair_id']]
        if not paired and not routed:
            continue
        sections = {}
        for row in paired:
            if row['role'] not in ('L','R'):
                raise _analysis_error('XML 左右声音配对缺少声道身份：' + row['path'])
            sections.setdefault((row['pair_id'],row['record_start'],row['record_end'],row['source_start']),[]).append(row)
        for members in sections.values():
            if len(members)!=2 or sorted(row['role'] for row in members)!=['L','R'] or members[0]['path']==members[1]['path']:
                raise _analysis_error('XML 中左右声音配对缺失、重复或位置不一致。')
        for kind, row in [('video',group['video'])] + [('audio',row) for row in group['audio']]:
            if row is None:
                continue
            identity=(kind,row.get('path') or row.get('name'),row['record_start'],row['record_end'],row['source_start'])
            if routed:
                identity += (row.get('track_index'),)
            if identity in all_identities:
                raise _analysis_error('XML 含有同一素材、同一位置的重复片段，不能安全恢复链接。')
            all_identities.add(identity)
        result.append(group)
    if not result:
        return None
    manifest = dict(version=1, fps=fps, timeline_start=start*fps, timeline_end=end*fps,
                    groups=result, media_paths=sorted(media_paths))
    if requested_clocks:
        requested = _time(requested_clocks[0])
        frames = requested * fps
        if requested < 0 or frames.denominator != 1 or fps.denominator != 1:
            raise _analysis_error('XML 请求的时间线起始时码不是有效的非丢帧位置。')
        shift = (requested - start) * fps
        for group in result:
            for item in ([group['video']] if group['video'] is not None else []) + group['audio']:
                item['record_start'] += shift
                item['record_end'] += shift
        whole, sub = divmod(int(frames), int(fps))
        hours, rest = divmod(whole, 3600)
        minutes, seconds = divmod(rest, 60)
        manifest.update(import_start=start*fps, timeline_start=frames, timeline_end=end*fps+shift,
                        set_start_timecode=f'{hours:02}:{minutes:02}:{seconds:02}:{sub:02}')
    if routed:
        supplied = {int(row['index']): row for row in audio_layout or () if isinstance(row, dict) and row.get('index')}
        track_types, intervals, track_owners = {}, {}, {}
        for group in result:
            for row in group['audio']:
                track = row['track_index']
                if group['video'] is not None:
                    track_owners.setdefault(track,set()).add(group['video']['track_index'])
                source_type = 'stereo' if row['channels'] > 1 else 'mono'
                subtype = supplied.get(track, {}).get('subtype', source_type)
                if subtype not in ('mono', 'stereo') or (source_type=='stereo' and subtype=='mono'):
                    raise _analysis_error('声音轨道规划与源文件声道不一致。')
                if track in track_types and track_types[track] != subtype:
                    raise _analysis_error('同一声音轨道混用了单声道和立体声。')
                track_types[track] = subtype
                bounds = row['record_start'], row['record_end']
                intervals.setdefault(track, []).append(bounds)
        for occupied in intervals.values():
            occupied.sort()
            if any(first < last for (_, last), (first, _) in zip(occupied, occupied[1:])):
                raise _analysis_error('同一声音轨道存在重叠，不能安全分配备选声音。')
        for number,row in supplied.items():
            if isinstance(row.get('video_track'),int):track_owners.setdefault(number,set()).add(row['video_track'])
        primary_video_track=min((group['video']['track_index'] for group in result if group['video'] is not None),default=1)
        primary_link_ids={_metadata(node).get('com.pgm.linkGroupID') for node in spine
            if node.tag in ('mc-clip','clip','gap') and int(node.get('lane','0'))==primary_video_track-1}
        primary_link_ids.discard(None);primary_link_ids.discard('')
        for group in result:
            if group['video'] is not None:
                group['primary']=group['video']['track_index']==primary_video_track
            else:
                group['primary']=bool(group['audio']) and (any(row.get('link_group_id') in primary_link_ids for row in group['audio']) or
                    all(track_owners.get(row['track_index'])=={primary_video_track} for row in group['audio']))
        tracks = [dict(index=number, subtype=track_types.get(number, supplied.get(number, {}).get('subtype', 'stereo')),
                       name=supplied.get(number, {}).get('name', 'PGM A' + str(number)),
                       video_tracks=sorted(track_owners.get(number,())))
                  for number in range(1, max([*track_types, *supplied], default=0) + 1)]
        manifest.update(routing_enabled=True, audio_tracks=tracks, primary_tracks_only=True,primary_video_track=primary_video_track)
    return manifest


def _lua(value):
    if value is None:
        return 'nil'
    if isinstance(value,bool):
        return 'true' if value else 'false'
    if isinstance(value,F):
        return str(value.numerator) if value.denominator==1 else '('+str(value.numerator)+'/'+str(value.denominator)+')'
    if isinstance(value,(int,float)):
        return repr(value)
    if isinstance(value,str):
        return '"'+''.join('\\%03d'%ord(char) if ord(char)<32 else '\\'+char if char in '\\"' else char for char in value)+'"'
    if isinstance(value,dict):
        return '{'+','.join('['+_lua(key)+']='+_lua(item) for key,item in value.items())+'}'
    return '{'+','.join(_lua(item) for item in value)+'}'


_IMPORT_LUA = r'''
local started=os.time()
local diagnostics={phase='startup',counts={inventory=0,validated=0,linked=0},timings={},
 expected_fps=manifest.fps,expected_start=manifest.timeline_start,import_start=manifest.import_start,
 requested_timecode=manifest.set_start_timecode,source_clock=import_clock_info,audio_record=import_audio_record_info,
 audio_lanes=import_audio_lane_info,audio_interchange=audio_interchange}
local local_log_path
pcall(function() local_log_path=os.tmpname() end)
local function log_event(event,detail)
 if not local_log_path then return end
 pcall(function()
  local handle=io.open(local_log_path,'ab');if not handle then return end
  handle:write(tostring(os.time()),' +',tostring(os.time()-started),'s ',event,
    detail and (' '..tostring(detail)) or '','\n');handle:close()
 end)
end
local phase_started=started
local function phase(name,detail)
 local now=os.time();local prior=diagnostics.phase
 diagnostics.timings[prior]=(diagnostics.timings[prior] or 0)+now-phase_started
 diagnostics.phase=name;phase_started=now
 log_event(name,detail)
end
local function fail(message) log_event('error',message);error(message,0) end
local function need(value,message) if not value then fail(message) end;return value end
local function same(a,b) return type(a)=='number' and math.abs(a-b)<0.001 end
if local_log_path then print('导入诊断日志：'..local_log_path) end
local function optional(object,method,...)
 local arguments={...}
 local ok,value=pcall(function() return object[method](object,(table.unpack or unpack)(arguments)) end)
 if ok and (type(value)=='number' or type(value)=='string' or type(value)=='boolean') then return value end
 return nil
end
local function canonical(path)
 path=tostring(path or ''):gsub('\\','/')
 for _,prefix in ipairs({'/var/','/tmp/','/etc/'}) do
  if path:sub(1,#prefix)==prefix then return '/private'..path end
 end
 return path
end
local function bucket(kind,first,last)
 return kind..':'..tostring(math.floor(first+0.5))..':'..tostring(math.floor(last+0.5))
end
local source=debug.getinfo(1,'S').source
need(source:sub(1,1)=='@','请从导出的 .resolve.lua 文件运行导入。')
local folder=need(source:sub(2):match('^(.*[/\\])'),'无法定位 XML 所在文件夹。')
local xml=folder..xml_name
phase('xml_read',xml_name)
local input=need(io.open(xml,'rb'),'找不到 XML：'..xml)
local a,b,length=1,0,0
local xml_parts={}
while true do local part=input:read(65536);if not part then break end
 length=length+#part
 xml_parts[#xml_parts+1]=part
 for index=1,#part do a=(a+part:byte(index))%65521;b=(b+a)%65521 end
end
input:close()
need(length==xml_length and b*65536+a==xml_adler,'XML 已变更，请通过软件重新导出 XML 和导入助手。')
local xml_bytes=table.concat(xml_parts);xml_parts=nil
phase('resolve_context')
local resolve=(Resolve and Resolve()) or (bmd and bmd.scriptapp and bmd.scriptapp('Resolve'))
resolve=need(resolve,'请在达芬奇内运行此脚本。')
local project=need(resolve:GetProjectManager():GetCurrentProject(),'请先打开剪辑项目。')
local pool=need(project:GetMediaPool(),'无法读取媒体池。')
local previous=project:GetCurrentTimeline()
local existing,names={},{}
for index=1,project:GetTimelineCount() do
 local item=project:GetTimelineByIndex(index);existing[item:GetUniqueId()]=true;names[item:GetName()]=true
end
local requested_name=timeline_name
local duplicate=2
while names[timeline_name] do timeline_name=requested_name..' ('..duplicate..')';duplicate=duplicate+1 end
local timeline,created,import_copy=nil,false,nil
local actual_timeline_name
local report_path=folder..xml_name:gsub('%.[^.]+$','')..'.resolve-report.json'
local function json_quote(value)
 return '"'..tostring(value):gsub('[%z\1-\31\\"]',function(char)
  if char=='"' then return '\\"' elseif char=='\\' then return '\\\\' end
  return string.format('\\u%04x',string.byte(char))
 end)..'"'
end
local function json(value)
 local kind=type(value)
 if kind=='nil' then return 'null' end
 if kind=='boolean' then return tostring(value) end
 if kind=='number' then
  if value~=value or value==math.huge or value==-math.huge then return 'null' end
  return string.format('%.17g',value)
 end
 if kind~='table' then return json_quote(value) end
 local values={}
 if #value>0 then
  for index=1,#value do values[#values+1]=json(value[index]) end
  return '['..table.concat(values,',')..']'
 end
 for key,item in pairs(value) do values[#values+1]=json_quote(key)..':'..json(item) end
 table.sort(values);return '{'..table.concat(values,',')..'}'
end
local function write_report(success,reason)
 diagnostics.elapsed_seconds=os.time()-started
 local report={ok=success,group_count=#manifest.groups,
  timeline_name=actual_timeline_name or timeline_name,
  message=reason or '',diagnostics=diagnostics,local_log_path=local_log_path}
 local body=json(report)..'\n'
 local function write(path)
  if not path then return false end
  local ok,saved=pcall(function()
   local handle=io.open(path,'wb');if not handle then return false end
   local result=handle:write(body);local closed=handle:close();return result~=nil and closed~=nil
  end)
  return ok and saved or false
 end
 -- Local diagnostics survive a disconnected or read-only shared volume.
 write(local_log_path and (local_log_path..'.json'))
 return write(report_path)
end
local ok,why=xpcall(function()
 local initial_rate=optional(project,'GetSetting','timelineFrameRate')
 diagnostics.initial_project_fps=initial_rate
 local initial_fps=tonumber(tostring(initial_rate or ''):match('^%s*([%d%.]+)'))
 need(initial_fps and math.abs(initial_fps-manifest.fps)<0.000001,
  '导入前项目帧率为 '..tostring(initial_rate)..'，XML 需要 '..tostring(manifest.fps)..
  ' fps。请在新建空项目中先设置正确帧率，再导入；已导入的旧多机位不会自动修正。')
 if audio_interchange then
  phase('audio_bridge_preflight',audio_interchange.name)
  local path=folder..audio_interchange.name
  local handle=need(io.open(path,'rb'),'缺少内部声音导入文件，请完整复制整个 XML 导出文件夹：'..path)
  local a,b,length=1,0,0
  while true do local part=handle:read(65536);if not part then break end
   length=length+#part
   for index=1,#part do a=(a+part:byte(index))%65521;b=(b+a)%65521 end
  end
  handle:close()
  need(length==audio_interchange.bytes and b*65536+a==audio_interchange.adler32,
   '内部声音导入文件已改变／不完整，请从软件重新导出整个 XML 包。')
 end
 phase('media_preflight',#manifest.media_paths..' unique paths')
 for index,path in ipairs(manifest.media_paths) do
  local handle=need(io.open(path,'rb'),'媒体未找到，请先挂载存储并核对路径：'..path);handle:close()
  if index%100==0 then log_event('media_preflight_progress',index..'/'..#manifest.media_paths) end
 end
 local function xml_quote(value)
  return value:gsub('&','&amp;'):gsub('<','&lt;'):gsub('>','&gt;'):gsub('"','&quot;')
 end
 local import_uid=string.format('%08x-%04x-4%03x-a%03x-%04x%04x%04x',os.time(),math.random(0,65535),
   math.random(0,4095),math.random(0,4095),math.random(0,65535),math.random(0,65535),math.random(0,65535))
 local temporary=need(os.tmpname(),'无法创建临时 XML。');os.remove(temporary);import_copy=temporary..'.fcpxml'
 local copy=need(io.open(import_copy,'wb'),'无法写入临时导入 XML。')
 -- Every span uses zero-based bytes in the untouched public XML. Merge the
 -- identity rewrite and sparse source-clock attributes before streaming once.
 local patches={}
 for _,patch in ipairs(import_patches or {}) do patches[#patches+1]=patch end
 patches[#patches+1]={first=project_open_start,last=project_open_end,
  value=project_open_prefix..' name="'..xml_quote(timeline_name)..'" uid="'..import_uid..'">'}
 table.sort(patches,function(a,b) if a.first~=b.first then return a.first<b.first end;return a.last<b.last end)
 local cursor=0
 phase('prepare_import_xml',#(import_patches or {})..' WAV source-clock attributes')
 for _,patch in ipairs(patches) do
  need(type(patch.first)=='number' and type(patch.last)=='number' and patch.first>=cursor and
   patch.last>=patch.first and patch.last<=length and patch.first%1==0 and patch.last%1==0,
   '临时 XML 补丁范围无效／重叠，未导入。')
  need(copy:write(xml_bytes:sub(cursor+1,patch.first)),'临时 XML 写入失败。')
  if patch.parts then
   for _,part in ipairs(patch.parts) do
    need(copy:write(part.value or xml_bytes:sub(part.first+1,part.last)),'临时声音 XML 写入失败。')
   end
  else need(copy:write(patch.value),'临时 XML 写入失败。') end
  cursor=patch.last
 end
 local written=copy:write(xml_bytes:sub(cursor+1))
 local closed=copy:close();need(written and closed,'临时 XML 写入失败。');xml_bytes=nil
 phase('native_import')
 local imported=pool:ImportTimelineFromFile(import_copy,{timelineName=timeline_name,importSourceClips=true})
 phase('native_import_returned',imported and 'timeline returned' or 'no timeline')
 os.remove(import_copy);import_copy=nil
 timeline=need(imported,'XML 导入失败。')
 actual_timeline_name=optional(timeline,'GetName') or timeline_name
 need(not existing[timeline:GetUniqueId()],'导入没有生成新时间线，已停止。')
 created=true
 phase('clock_restore')
 diagnostics.actual_import_start=timeline:GetStartFrame()
 if manifest.set_start_timecode then
  need(same(timeline:GetStartFrame(),manifest.import_start),'导入时间线的临时时码与 XML 不一致。')
  need(timeline:SetStartTimecode(manifest.set_start_timecode),'达芬奇未能恢复请求的时间线起始时码。')
 end
 need(same(timeline:GetStartFrame(),manifest.timeline_start),'导入时间线的起始时码与 XML 不一致。')
 diagnostics.actual_start=timeline:GetStartFrame()
 diagnostics.project_fps=optional(project,'GetSetting','timelineFrameRate')
 diagnostics.timeline_fps=optional(timeline,'GetSetting','timelineFrameRate')
 diagnostics.resolve_version=optional(resolve,'GetVersionString')
 if manifest.audio_import_method=='private-aaf' then
  phase('prepare_audio_tracks')
  -- Only the newly created picture-only timeline is touched. Unexpected
  -- native sound is an error, never deleted to force a validation pass.
  local prior_audio_count=timeline:GetTrackCount('audio')
  for track=1,prior_audio_count do
   need(#(timeline:GetItemListInTrack('audio',track) or {})==0,
    '画面导入意外带入声音片段，未执行声音轨道替换。')
  end
  -- Resolve refuses deleting its last audio track. Append the replacement
  -- first, then remove only the verified empty original tracks.
  need(timeline:AddTrack('audio',{audioType='stereo'}),'达芬奇未能建立立体声音轨。')
  for track=prior_audio_count,1,-1 do
   need(timeline:DeleteTrack('audio',track),'达芬奇未能移除新时间线的空音轨。')
  end
  for index,_ in ipairs(manifest.audio_tracks or {}) do
   if index>1 then
    need(timeline:AddTrack('audio',{audioType='stereo'}),'达芬奇未能建立对应视频编号的声音轨道。')
   end
  end
  if audio_interchange then
   phase('native_audio_import',audio_interchange.clip_count..' original WAV clips')
   need(timeline:ImportIntoTimeline(folder..audio_interchange.name,{
    autoImportSourceClipsIntoMediaPool=true,ignoreFileExtensionsWhenMatching=false,
    linkToSourceCameraFiles=false,importMultiChannelAudioTracksAsLinkedGroups=false,
    insertAdditionalTracks=false,insertWithOffset='00:00:00:00'}),
    '达芬奇未能导入原始 WAV 声音交换文件。')
   phase('native_audio_import_returned')
  end
  need(timeline:GetTrackCount('audio')==#(manifest.audio_tracks or {}),
   '声音导入后的轨道数量与 Vn/An 编号规划不一致。')
 end
 phase('inventory')
 local inventory,inventory_rows,media_cache={},{video={},audio={}},{ }
 local cache_hits,cache_misses=0,0
 for _,kind in ipairs({'video','audio'}) do
  for track=1,timeline:GetTrackCount(kind) do
   for _,item in ipairs(timeline:GetItemListInTrack(kind,track) or {}) do
    local first,last=item:GetStart(true),item:GetEnd(true)
    need(type(first)=='number' and type(last)=='number','无法读取导入片段位置。')
    local media=need(item:GetMediaPoolItem(),'导入片段没有媒体池来源。')
    local media_uid=optional(media,'GetUniqueId')
    if type(media_uid)~='string' or media_uid=='' then media_uid=nil end
    local cached=media_uid and media_cache[media_uid]
    if cached then cache_hits=cache_hits+1 else
     cached={props=media:GetClipProperty() or {},name=media:GetName()};cache_misses=cache_misses+1
     if media_uid then media_cache[media_uid]=cached end
    end
    local props=cached.props
    local row={item=item,uid=item:GetUniqueId(),first=first,last=last,left=item:GetLeftOffset(true),
      name=cached.name,filename=props['File Name'],path=canonical(props['File Path']),raw_path=props['File Path'],
      track=track,media_type=props['Type'],media_uid=media_uid,props=props}
    local key=bucket(kind,first,last);inventory[key]=inventory[key] or {};table.insert(inventory[key],row)
    table.insert(inventory_rows[kind],row)
    diagnostics.counts.inventory=diagnostics.counts.inventory+1
    if diagnostics.counts.inventory%500==0 then log_event('inventory_progress',diagnostics.counts.inventory) end
   end
  end
 end
 diagnostics.counts.media_cache_hits=cache_hits;diagnostics.counts.media_cache_misses=cache_misses
 if manifest.routing_enabled then
  local wanted,actual={},{}
  for _,group in ipairs(manifest.groups) do if group.video then wanted[group.video.track_index]=true end end
  for _,row in ipairs(inventory_rows.video) do actual[row.track]=true end
  local logical,native={},{}
  for index in pairs(wanted) do logical[#logical+1]=index end
  for index in pairs(actual) do native[#native+1]=index end
  table.sort(logical);table.sort(native)
  need(#logical==#native,'导入后的非空视频轨数量与排列不一致。')
  local inserted=0
  diagnostics.video_track_numbering={inserted={}}
  for number,target in ipairs(logical) do
   local current=native[number]+inserted
   need(current<=target,'导入后的画面轨道顺序无法安全恢复。')
   while current<target do
    need(timeline:AddTrack('video',{index=current}),'达芬奇未能恢复空的视频轨道。')
    diagnostics.video_track_numbering.inserted[#diagnostics.video_track_numbering.inserted+1]=current
    inserted=inserted+1;current=current+1
   end
  end
  if inserted>0 then
   phase('video_track_numbering')
   local by_uid={}
   for _,row in ipairs(inventory_rows.video) do by_uid[row.uid]=row end
   local seen=0
   for track=1,timeline:GetTrackCount('video') do
    for _,item in ipairs(timeline:GetItemListInTrack('video',track) or {}) do
     local row=need(by_uid[item:GetUniqueId()],'补齐空视频轨后出现未知片段。')
     need(same(item:GetStart(true),row.first) and same(item:GetEnd(true),row.last) and
       same(item:GetLeftOffset(true),row.left),'补齐空视频轨改变了画面源位置。')
     row.track=track;seen=seen+1
    end
   end
   need(seen==#inventory_rows.video,'补齐空视频轨后画面数量不一致。')
  end
 end
 phase('validation')
 local used,validated,audio_intervals={},{},{}
 local function find(kind,expected)
  local matches={};local routed_elsewhere=false
  local tolerance=manifest.routing_enabled and kind=='audio' and manifest.fps/(expected.sample_rate or 48000)/10 or 0.001
  local function exact(a,b) return type(a)=='number' and math.abs(a-b)<tolerance end
  local function identity(row)
   return expected.kind=='multicam' and (row.name==expected.name or row.filename==expected.name)
     or expected.path and row.path==canonical(expected.path)
  end
  local exact_bucket=inventory[bucket(kind,expected.record_start,expected.record_end)] or {}
  for _,row in ipairs(exact_bucket) do
   local wanted_track=kind=='audio' and expected.import_track_index or kind=='video' and expected.track_index
   if identity(row) and exact(row.first,expected.record_start) and exact(row.last,expected.record_end)
      and exact(row.left,expected.source_start) then
    if not wanted_track or row.track==wanted_track then table.insert(matches,row)
    else routed_elsewhere=true end
   end
  end
  if #matches~=1 then
   local diagnostic_ok,diagnostic_error=pcall(function()
   -- Diagnose only: candidate expansion never participates in matching.
   local candidates,seen={},{}
   local function add(row,origin,priority)
    if seen[row] then return end;seen[row]=true
    local distance=math.abs(row.first-expected.record_start)+math.abs(row.last-expected.record_end)
    candidates[#candidates+1]={row=row,origin=origin,priority=priority,distance=distance}
   end
   for _,row in ipairs(exact_bucket) do add(row,'exact_bucket',0) end
   for first_delta=-1,1 do for last_delta=-1,1 do
    for _,row in ipairs(inventory[bucket(kind,expected.record_start+first_delta,expected.record_end+last_delta)] or {}) do
     add(row,'neighbor_bucket',1)
    end
   end end
   local identity_count=0
   for _,row in ipairs(inventory_rows[kind]) do
    if identity(row) then identity_count=identity_count+1;add(row,'same_identity',2) end
   end
   table.sort(candidates,function(a,b)
    if a.priority~=b.priority then return a.priority<b.priority end
    if a.distance~=b.distance then return a.distance<b.distance end
    return tostring(a.row.uid)<tostring(b.row.uid)
   end)
   local details={}
   local scale=kind=='audio' and (expected.sample_rate or 48000)/manifest.fps or nil
   local function delta(actual,wanted)
    if type(actual)~='number' then return nil end
    return {frames=actual-wanted,samples=scale and (actual-wanted)*scale or nil}
   end
   for index=1,math.min(#candidates,10) do
    local item=candidates[index];local row=item.row
    details[#details+1]={origin=item.origin,uid=row.uid,media_uid=row.media_uid,
     track=row.track,name=row.name,filename=row.filename,path=row.raw_path,canonical_path=row.path,
     media_type=row.media_type,first=row.first,last=row.last,left=row.left,
     first_delta=delta(row.first,expected.record_start),last_delta=delta(row.last,expected.record_end),
     left_delta=delta(row.left,expected.source_start),identity_matches=not not identity(row),
     first_matches=exact(row.first,expected.record_start),last_matches=exact(row.last,expected.record_end),
     left_matches=exact(row.left,expected.source_start),already_used=not not used[row.uid],
     media_start_tc=row.props['Start TC'],media_end_tc=row.props['End TC'],
     media_frames=row.props['Frames'],media_fps=row.props['FPS'],audio_channels=row.props['Audio Ch'],
     source_start_frame=optional(row.item,'GetSourceStartFrame'),
     source_start_time=optional(row.item,'GetSourceStartTime'),
     source_end_frame=optional(row.item,'GetSourceEndFrame'),
     source_end_time=optional(row.item,'GetSourceEndTime'),right=optional(row.item,'GetRightOffset',true)}
   end
   diagnostics.lookup_failure={kind=kind,group_index=diagnostics.group_index,match_count=#matches,
    reason=routed_elsewhere and 'track_routing_mismatch' or 'source_or_position_mismatch',
    exact_bucket_count=#exact_bucket,same_identity_count=identity_count,candidate_count=#candidates,
    candidate_limit=10,tolerance_frames=tolerance,tolerance_samples=scale and tolerance*scale or nil,
    expected={path=expected.path,canonical_path=expected.path and canonical(expected.path),
     name=expected.name,kind=expected.kind,record_start=expected.record_start,record_end=expected.record_end,
     source_start=expected.source_start,record_start_17g=string.format('%.17g',expected.record_start),
     record_end_17g=string.format('%.17g',expected.record_end),source_start_17g=string.format('%.17g',expected.source_start),
     track_index=expected.track_index,import_track_index=expected.import_track_index,sample_rate=expected.sample_rate,channels=expected.channels,
     role=expected.role,pair_id=expected.pair_id,link_group_id=expected.link_group_id},candidates=details}
   log_event('lookup_failure',json(diagnostics.lookup_failure))
   end)
   if not diagnostic_ok then log_event('diagnostic_error',diagnostic_error) end
  end
  if #matches==0 and routed_elsewhere then
   fail('声音／画面来源和位置正确，但导入轨道编号不一致；未建立时间线，请保留导入报告：'..tostring(expected.path or expected.name))
  end
  need(#matches==1,'片段来源或位置缺失／重复，未恢复链接：'..tostring(expected.path or expected.name))
  local row=matches[1];need(not used[row.uid],'不同场次匹配到同一个片段，已停止。');used[row.uid]=true
  if manifest.routing_enabled and kind=='video' then
   need(row.track==expected.track_index,'导入后的多机位轨道与手动排列不一致。')
   local media_type=tostring(row.media_type or ''):lower()
   need(media_type:find('multicam',1,true) or media_type:find('多机位',1,true) or media_type:find('多機位',1,true),
    '导入后的画面不是可核实的多机位素材，未修改时间线。')
  end
  return row
 end
 -- Validate every group before any linking or panning operation.
 for _,expected in ipairs(manifest.groups) do
  diagnostics.group_index=diagnostics.counts.validated+1
  local group={items={},audio={},sound_items={},primary=expected.primary}
  if expected.video then table.insert(group.items,find('video',expected.video).item) end
  for _,audio in ipairs(expected.audio) do
   local row=find('audio',audio);table.insert(group.items,row.item)
   table.insert(group.sound_items,{item=row.item,track=row.track,expected_track=audio.track_index,
     uid=row.uid,first=row.first,last=row.last,left=row.left,sample_rate=audio.sample_rate})
   if manifest.routing_enabled then
    audio_intervals[row.track]=audio_intervals[row.track] or {}
    table.insert(audio_intervals[row.track],{row.first,row.last})
   end
   if audio.role=='L' or audio.role=='R' then
    need(type(row.item:GetProperty('AudioPan'))=='number','无法读取声音声相，未恢复链接。')
    table.insert(group.audio,{item=row.item,pan=audio.role=='L' and -100 or 100})
   end
  end
  need(#group.items>=2 or (manifest.routing_enabled and #group.items>=1), '声音配对缺少对应片段。')
  table.insert(validated,group)
  diagnostics.counts.validated=#validated
  if #validated%500==0 then log_event('validation_progress',#validated..'/'..#manifest.groups) end
 end
 if manifest.strict_audio_inventory then
  local unexpected={}
  for _,row in ipairs(inventory_rows.audio) do
   if not used[row.uid] then
    if #unexpected<10 then
     unexpected[#unexpected+1]={uid=row.uid,path=row.raw_path,track=row.track,
       first=row.first,last=row.last,left=row.left}
    end
   end
  end
  if #unexpected>0 then diagnostics.unexpected_audio=unexpected end
  need(#unexpected==0,'导入后出现未登记的声音片段，未恢复链接。')
 end
 for _,intervals in pairs(audio_intervals) do
  table.sort(intervals,function(a,b) return a[1]==b[1] and a[2]<b[2] or a[1]<b[1] end)
  for index=2,#intervals do
   need(intervals[index][1]>=intervals[index-1][2]-0.000001,'同一声音轨道出现重叠，未恢复链接。')
  end
 end
 if manifest.numbered_audio_tracks then
  phase('audio_track_numbering')
  local occupied,total,inserted={},#(manifest.audio_tracks or {}),0
  for _,row in ipairs(manifest.import_audio_tracks or {}) do occupied[row.target_index]=true end
  diagnostics.audio_track_numbering={inserted={},tracks={}}
  local active_count=#(manifest.import_audio_tracks or {})
  for index=1,total do
   if not occupied[index] then
    local options={audioType=(manifest.audio_tracks[index] or {}).subtype or 'stereo'}
    if index<=active_count+inserted then
     options.index=index
     need(timeline:AddTrack('audio',options),'达芬奇未能插入对应视频的空音轨。')
     inserted=inserted+1
     diagnostics.audio_track_numbering.inserted[#diagnostics.audio_track_numbering.inserted+1]=index
    elseif index>timeline:GetTrackCount('audio') then
     need(timeline:AddTrack('audio',options),'达芬奇未能补齐对应视频的空音轨。')
     diagnostics.audio_track_numbering.inserted[#diagnostics.audio_track_numbering.inserted+1]=index
    end
   end
  end
  if inserted>0 then
   local actual={}
   for track=1,timeline:GetTrackCount('audio') do
    for _,item in ipairs(timeline:GetItemListInTrack('audio',track) or {}) do
     local uid=item:GetUniqueId();need(not actual[uid],'音轨整理后片段身份重复。')
     actual[uid]={track=track,item=item}
    end
   end
   for _,group in ipairs(validated) do for _,row in ipairs(group.sound_items) do
    local found=need(actual[row.uid],'音轨整理后片段缺失。')
    need(found.track==row.expected_track,'音轨整理后未能达到 Vn 对应 An，未恢复链接。')
    local tolerance=manifest.fps/(row.sample_rate or 48000)/10
    need(math.abs(found.item:GetStart(true)-row.first)<tolerance and
      math.abs(found.item:GetEnd(true)-row.last)<tolerance and
      math.abs(found.item:GetLeftOffset(true)-row.left)<tolerance,
      '音轨整理改变了声音位置，未恢复链接。')
    row.track=found.track
   end end
  else
   -- The common dense layout needs no extra per-clip SDK inventory calls.
   for _,group in ipairs(validated) do for _,row in ipairs(group.sound_items) do
    need(row.track==row.expected_track,'声音未能导入对应视频编号的音轨。')
   end end
  end
  for _,planned in ipairs(manifest.audio_tracks or {}) do
   diagnostics.audio_track_numbering.tracks[#diagnostics.audio_track_numbering.tracks+1]={index=planned.index,video_tracks=planned.video_tracks}
  end
 end
 phase('linking')
 for _,group in ipairs(validated) do
  diagnostics.group_index=diagnostics.counts.linked+1
  if #group.items>=2 then need(timeline:SetClipsLinked(group.items,true),'达芬奇未能设置音视频链接。') end
  for _,audio in ipairs(group.audio) do
   need(audio.item:SetProperty('AudioPan',audio.pan),'达芬奇未能设置左右声相。')
   need(same(audio.item:GetProperty('AudioPan'),audio.pan),'左右声相设置未生效。')
  end
  diagnostics.counts.linked=diagnostics.counts.linked+1
  if diagnostics.counts.linked%500==0 then log_event('linking_progress',diagnostics.counts.linked..'/'..#validated) end
 end
 if manifest.primary_tracks_only then
  phase('track_visibility')
  local audio_owners={}
  for _,group in ipairs(validated) do
   for _,row in ipairs(group.sound_items) do
    local owners=audio_owners[row.track] or {};audio_owners[row.track]=owners
    owners[group.primary and 'primary' or 'alternative']=true
   end
  end
  diagnostics.track_visibility={}
  for _,kind in ipairs({'video','audio'}) do
   for index=1,timeline:GetTrackCount(kind) do
    local enabled=kind=='video' and index==(manifest.primary_video_track or 1) or kind=='audio' and audio_owners[index] and audio_owners[index].primary or false
    need(timeline:SetTrackEnable(kind,index,enabled),'达芬奇未能设置轨道启用状态。')
    need(timeline:GetIsTrackEnabled(kind,index)==enabled,'轨道启用状态未生效。')
    diagnostics.track_visibility[#diagnostics.track_visibility+1]={kind=kind,track=index,enabled=enabled}
   end
  end
  -- Resolve can reuse an audio track for non-overlapping performances.
  -- Disable only the alternative items on a shared primary audio track.
  for _,group in ipairs(validated) do
   for _,row in ipairs(group.sound_items) do
    local owners=audio_owners[row.track]
    if owners and owners.primary and owners.alternative then
     need(row.item:SetClipEnabled(not not group.primary),'达芬奇未能设置备选声音启用状态。')
     need(row.item:GetClipEnabled()==not not group.primary,'备选声音启用状态未生效。')
    end
   end
  end
 end
 phase('select_timeline')
 need(project:SetCurrentTimeline(timeline),'无法显示导入时间线。')
 phase('complete')
 local report_ok,report_saved=pcall(write_report,true,manifest.routing_enabled and '多机位及原始声音链接已核实。' or '原始左右声音链接及声相已核实。')
 report_saved=report_ok and report_saved
 print('导入完成：'..actual_timeline_name..'；已核实 '..#validated..(manifest.routing_enabled and ' 组音视频。' or ' 组原始左右声音链接。')..
   (report_saved and ('核查报告：'..report_path) or '核查报告未能保存。'))
end,debug.traceback)
if not ok then
 if import_copy then os.remove(import_copy) end
 log_event('failed',why)
 local report_ok,report_error=pcall(write_report,false,why)
 if not report_ok then log_event('diagnostic_error',report_error) end
 if created and timeline then pool:DeleteTimelines({timeline}) end
 if previous then project:SetCurrentTimeline(previous) end
 print('导入未完成：'..tostring(why));error(why,0)
end
return timeline
'''


def write_resolve_import_helper(xml_path, guide_audio=(), audio_layout=()):
    """Write ``name.resolve.lua`` beside the XML; return its Path or None."""
    path=Path(xml_path)
    target=companion_path(path)
    manifest=build_resolve_manifest(path,guide_audio,audio_layout)
    if target.is_symlink():
        raise _analysis_error('导入脚本路径是符号链接，未覆盖：'+str(target))
    if target.exists() and not target.read_text(encoding='utf-8').startswith(HEADER+'\n'):
        raise _analysis_error('同名导入脚本不是本软件生成，未覆盖：'+str(target))
    if manifest is None:
        if target.exists():target.unlink()
        return None
    raw=path.read_bytes()
    project=ET.fromstring(raw).find('.//project')
    title=project.get('name') or path.stem
    opening_start, opening_end, opening_prefix = _project_opening(raw, project)
    audio_interchange=None
    if manifest.get('routing_enabled'):
        from resolve_audio_interchange import write_audio_interchange
        manifest,import_patches,record_info=_sample_audio_import_plan(raw,manifest)
        if any(group['audio'] for group in manifest['groups']):
            bridge=write_audio_interchange(path,manifest,guide_audio=guide_audio)
            audio_interchange={key:bridge[key] for key in ('bytes','adler32','sha256','clip_count','track_count','sample_rate','source_clock_policy')}
            audio_interchange['name']=Path(bridge['aaf_path']).name
        clock=dict(method='original-file-local',adjusted_asset_count=0)
        lane_info=dict(method='explicit_aaf_physical_tracks',planned_tracks=len(manifest['audio_tracks']))
    else:
        clock=_audio_import_patches(raw,manifest)
        manifest,record_patches,record_info=_audio_record_import_patches(raw,manifest)
        lane_patches,lane_info=_audio_track_import_plan(raw,manifest)
        import_patches=_merge_time_patches(raw,clock.pop('patches'),record_patches,lane_patches)
        import_patches=_audio_document_import_patches(raw,manifest,import_patches)
    content=HEADER+'\nlocal manifest='+_lua(manifest)+'\nlocal xml_name='+_lua(path.name)
    content+='\nlocal xml_length='+str(len(raw))+'\nlocal xml_adler='+str(zlib.adler32(raw))
    content+='\nlocal timeline_name='+_lua(title)
    content+='\nlocal project_open_start='+str(opening_start)+'\nlocal project_open_end='+str(opening_end)
    content+='\nlocal project_open_prefix='+_lua(opening_prefix)
    content+='\nlocal import_patches='+_lua(import_patches)
    content+='\nlocal import_clock_info='+_lua(clock)
    content+='\nlocal import_audio_lane_info='+_lua(lane_info)
    content+='\nlocal import_audio_record_info='+_lua(record_info)
    content+='\nlocal audio_interchange='+_lua(audio_interchange)+'\n'+_IMPORT_LUA
    _atomic_write(target,content)
    return target


RESOLVE_MENU_SCRIPT = r'''-- 三木的PGM回套软件 Resolve XML 导入菜单 v1
local resolve=(Resolve and Resolve()) or (bmd and bmd.scriptapp and bmd.scriptapp('Resolve'))
assert(resolve,'请在达芬奇内运行。')
local fusion=resolve:Fusion()
local file=fusion:RequestFile('',{FReqB_Saving=false,FReqS_Title='选择多机位 FCPXML',FReqS_Filter='*.fcpxml'})
if not file or file=='' then return end
assert(file:lower():match('%.fcpxml$'),'请选择 .fcpxml 文件。')
local companion=file:gsub('%.[fF][cC][pP][xX][mM][lL]$','.resolve.lua')
local handle=io.open(companion,'rb')
if handle then
 local first=handle:read('*l');handle:close()
 assert(first=='-- 三木的PGM回套软件 Resolve 导入助手 v1','旁边的导入脚本不是本软件生成，请重新导出。')
 return dofile(companion)
end
local project=assert(resolve:GetProjectManager():GetCurrentProject(),'请先打开剪辑项目。')
local timeline=assert(project:GetMediaPool():ImportTimelineFromFile(file,{importSourceClips=true}),'XML 导入失败。')
project:SetCurrentTimeline(timeline)
print('导入完成：'..timeline:GetName())
return timeline
'''


def write_resolve_menu_script(path):
    target=Path(path)
    target.parent.mkdir(parents=True,exist_ok=True)
    if target.is_symlink():
        raise _analysis_error('达芬奇菜单脚本路径是符号链接，未覆盖：'+str(target))
    if target.exists() and not target.read_text(encoding='utf-8').startswith(MANAGED_MARKER+'\n'):
        raise _analysis_error('同名达芬奇菜单脚本不是本软件生成，未覆盖：'+str(target))
    _atomic_write(target,RESOLVE_MENU_SCRIPT)
    return target


def _atomic_write(target,content):
    temporary=None
    published=False
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=target.parent,
                prefix=target.name+'.',suffix='.writing',delete=False) as handle:
            temporary=Path(handle.name)
            handle.write(content)
        temporary.replace(target)
        published=True
    finally:
        # A successful replace transfers ownership to the final file. Shared
        # volumes can briefly expose a stale temporary name; never touch it
        # after publishing, and never let failed cleanup replace a write error.
        if temporary is not None and not published:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                logging.getLogger(__name__).warning(
                    '未能清理导入助手临时文件，已保留原始写入错误：%s', temporary,
                    exc_info=True)
