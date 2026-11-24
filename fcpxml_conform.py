"""Raw video conform keeps original WAV sound and removes known camera sound."""
from fractions import Fraction
from pathlib import Path
import copy
import json
import xml.etree.ElementTree as ET


def _pgm_video_assets(root,video_fallbacks=(),path_mappings=()):
    """Recover explicit PGM picture identity after an editor drops custom tags."""
    from proxy_workflow import file_path
    from library_bridge import remap_path
    def key(value):return str(Path(remap_path(value,path_mappings)).expanduser().resolve()) if value else ''
    known={key(row.get('path')) for row in video_fallbacks if row.get('path')}
    result={}
    for asset in root.findall('./resources/asset'):
        source=asset.get('src') or next((row.get('src') for row in asset.findall('media-rep') if row.get('kind')=='original-media'),'')
        path=key(file_path(source));marker=asset.find('./metadata/md[@key="com.pgm.videoSourceKind"]')
        if asset.get('hasVideo')=='1' and (path in known or marker is not None and marker.get('value')=='pgm_fallback'):
            if marker is None:
                metadata=asset.find('metadata')
                if metadata is None:metadata=ET.SubElement(asset,'metadata')
                marker=ET.SubElement(metadata,'md',key='com.pgm.videoSourceKind')
            marker.set('value','pgm_fallback');result[asset.get('id')]=path
    return result


def _silence_pgm_fallback_wavs(root,fallback_assets,guide_audio=(),path_mappings=()):
    """Remove original-WAV components owned by a PGM replacement edit.

    Generated wrappers, native connected sound and explicit link-group IDs
    establish ownership. Unrelated added music or another performance's audio
    is never inferred from a coincident record position.
    """
    from proxy_workflow import file_path
    from library_bridge import remap_path
    def key(value):return str(Path(remap_path(value,path_mappings)).expanduser().resolve()) if value else ''
    wav_paths={key(row['path']) for row in guide_audio or () if row.get('source_kind','wav')=='wav' and row.get('path')}
    assets={row.get('id'):row for row in root.findall('./resources/asset')}
    wav_ids={identity for identity,asset in assets.items() if key(file_path(asset.get('src'))) in wav_paths or
             asset.find('./metadata/md[@key="com.pgm.audioSourceKind"][@value="wav"]') is not None}
    angles={media.get('id'):{angle.get('angleID') for angle in media.findall('./multicam/mc-angle')
                            if any(node.get('ref') in fallback_assets for node in angle.iter())}
            for media in root.findall('./resources/media')}
    project=root.find('./library/event/project')
    if project is None:return 0,set()
    parents={child:parent for parent in project.iter() for child in parent};selected=[]
    timed={'clip','mc-clip','asset-clip','audio','video','gap','ref-clip'};positions={}
    def seconds(node,key,default='0s'):return Fraction(node.get(key,default)[:-1])
    def locate(node,absolute):
        positions[node]=absolute
        for child in node:
            if child.tag in timed:locate(child,absolute+seconds(child,'offset')-seconds(node,'start'))
    spine=project.find('./sequence/spine')
    if spine is not None:
        for node in spine:
            if node.tag in timed:locate(node,seconds(node,'offset'))
    for node in project.iter():
        if node.tag=='mc-clip':
            choices=[row for row in node.findall('mc-source') if row.get('srcEnable','all') in ('video','all')]
            fallback=(any(row.get('angleID') in angles.get(node.get('ref'),set()) for row in choices) if choices else
                      node.find('./metadata/md[@key="com.pgm.videoSourceKind"][@value="pgm_fallback"]') is not None)
            if fallback:selected.append(node)
        elif node.tag in ('video','asset-clip') and node.get('ref') in fallback_assets and node.get('srcEnable','all')!='audio':selected.append(node)
    groups={};owned={}
    def mark_sound(container,hole):
        for child in container:
            if int(child.get('lane','0'))>0:continue
            if child.tag in ('audio','asset-clip') and child.get('ref') in wav_ids:owned.setdefault(child,[]).append(hole)
            elif child.tag=='clip':mark_sound(child,hole)
    for node in selected:
        if node not in positions:continue
        hole=(positions[node],positions[node]+seconds(node,'duration'))
        group=node.find('./metadata/md[@key="com.pgm.linkGroupID"]')
        if group is not None:groups.setdefault(group.get('value'),[]).append(hole)
        parent=parents.get(node)
        mark_sound(parent if parent is not None and parent.tag=='clip' else node,hole)
    for node in project.iter():
        group=node.find('./metadata/md[@key="com.pgm.linkGroupID"]')
        if group is not None and group.get('value') in groups and node.get('ref') in wav_ids:owned.setdefault(node,[]).extend(groups[group.get('value')])
    changed=0
    def sec(value):return f'{value.numerator}/{value.denominator}s'
    for node,holes in owned.items():
        if node not in positions:continue
        first=positions[node];last=first+seconds(node,'duration');parts=[(first,last)]
        for a,b in holes:parts=[piece for start,end in parts for piece in ((start,min(end,a)),(max(start,b),end)) if piece[1]>piece[0]]
        if parts==[(first,last)]:continue
        parent=parents.get(node)
        if parent is not None:
            index=list(parent).index(node);changed+=1
            parent.remove(node)
            for number,(a,b) in enumerate(parts):
                item=copy.deepcopy(node);item.set('offset',sec(seconds(node,'offset')+a-first));item.set('start',sec(seconds(node,'start')+a-first));item.set('duration',sec(b-a))
                metadata=item.find('metadata')
                if metadata is not None:
                    for entry in list(metadata):
                        if entry.get('key')=='com.pgm.linkGroupID':metadata.remove(entry)
                parent.insert(index+number,item)
            while parent.tag=='clip' and not any(child.tag in ('audio','asset-clip','video','mc-clip','clip','gap','ref-clip') for child in parent):
                grand=parents.get(parent)
                if grand is None:break
                if parent in list(grand):grand.remove(parent)
                parent=grand
    return changed,{node.get('ref') for node in selected if node.get('ref')}


def strip_camera_reference_audio(root, proxy_paths=(), guide_audio=(), path_mappings=None):
    """Remove explicitly identified camera/PGM sound, retaining WAV and new media.

    This pure XML edit never opens media, shifts source/record coordinates, or
    guesses ownership from filenames. Saved historical PGM identities remain
    useful even though new mappings contain only original WAV identities.
    """
    from library_bridge import remap_path
    from proxy_workflow import file_path
    mappings=path_mappings or ()
    def path_key(value):
        return str(Path(remap_path(value,mappings)).expanduser().resolve()) if value else ''
    camera_paths={path_key(value) for value in proxy_paths if value}
    pgm_paths={path_key(row['path']) for row in guide_audio or ()
               if isinstance(row,dict) and row.get('path') and row.get('source_kind')=='pgm'}
    resources=root.find('resources')
    report=dict(removed_audio_components=0,video_only_components=0,
                removed_sound_assets=0,removed_audio_multicam_choices=0)
    if resources is None:return report
    assets={asset.get('id'):asset for asset in resources.findall('asset')}
    paths={}
    blocked=set()
    for identity,asset in assets.items():
        source=asset.get('src') or next((node.get('src') for node in asset.findall('media-rep')
                                       if node.get('kind')=='original-media'),'')
        path=path_key(file_path(source));paths[identity]=path
        metadata={node.get('key'):node.get('value') for node in asset.findall('./metadata/md')}
        if path in camera_paths or path in pgm_paths or metadata.get('com.pgm.audioSourceKind')=='pgm' or metadata.get('com.pgm.videoSourceKind')=='pgm_fallback':
            blocked.add(identity)
    camera_multicams={media.get('id') for media in resources.findall('media')
                     if any(node.get('ref') in blocked and assets[node.get('ref')].get('hasVideo')=='1'
                            for node in media.iter())}
    timed={'clip','asset-clip','audio','video','mc-clip','ref-clip','gap','spine'}
    def video_only(node):
        if node.get('srcEnable')!='video':report['video_only_components']+=1
        node.set('srcEnable','video')
        for field in ('audioStart','audioDuration'):node.attrib.pop(field,None)
    def remove(parent,node):
        # Preserve the record span of an audio-only primary-spine component.
        # Connected children need their parent's existing source clock too.
        keep_children=any(child.tag in timed for child in node)
        primary=parent.tag=='spine' and node.get('lane') in (None,'0')
        if primary or keep_children:
            node.tag='gap'
            for field in list(node.attrib):
                if field not in ('name','offset','start','duration','lane'):node.attrib.pop(field)
            node.set('name','REMOVED_CAMERA_REFERENCE_AUDIO')
        else:parent.remove(node)
        report['removed_audio_components']+=1
    def walk(parent):
        for node in list(parent):
            had_camera_audio=(node.tag=='clip' and any(child.tag=='audio' and child.get('ref') in blocked for child in node))
            walk(node)
            identity=node.get('ref')
            if node.tag=='audio' and identity in blocked:
                remove(parent,node)
            elif node.tag=='asset-clip' and identity in blocked:
                asset=assets[identity]
                if asset.get('hasVideo')=='1' and node.get('srcEnable','all')!='audio':video_only(node)
                else:remove(parent,node)
            elif node.tag=='mc-clip' and identity in camera_multicams:
                choices=node.findall('mc-source')
                if node.get('srcEnable')=='audio':
                    remove(parent,node)
                else:
                    video_only(node)
                    for choice in list(node.findall('mc-source')):
                        if choice.get('srcEnable')=='audio':
                            node.remove(choice);report['removed_audio_multicam_choices']+=1
                        elif choice.get('srcEnable','all')!='none':choice.set('srcEnable','video')
            elif node.tag=='clip' and not any(child.tag in timed for child in node):
                # A native Resolve audio wrapper can become empty after its
                # identified PGM leaf is removed. Other clips stay untouched.
                if had_camera_audio:remove(parent,node)
    walk(root)
    # A generic clip wrapper with no surviving source but static audio effects
    # is removable only when it previously contained an identified audio leaf.
    referenced={node.get('ref') for node in root.iter() if node.get('ref')}
    for identity in blocked:
        asset=assets[identity]
        if asset.get('hasVideo')=='1':
            for field in ('hasAudio','audioSources','audioChannels','audioRate'):asset.attrib.pop(field,None)
        elif identity not in referenced:
            resources.remove(asset);report['removed_sound_assets']+=1
    return report


def normalize_native_audio_links(root):
    """Rebuild simple Resolve-exported WAV components as linked A/V clips.

    Resolve exports a linked external WAV as an audio-only connected ``clip``
    inside an ``mc-clip``. Reimporting that spelling loses Link Clips. A primary
    multicam plus lane-less audio components inside a composite ``clip`` keeps
    the original WAV reference and lets Resolve restore the native link group.
    Paired mono WAVs retain connected clips because Resolve imports only the
    first direct audio component; their native links need the import helper.
    Complex edits are left intact, rather than flattening effects or retiming.
    """
    assets = {asset.get('id'): asset for asset in root.findall('./resources/asset')}
    report = dict(normalized_multicam_clips=0, normalized_audio_components=0, skipped=[])

    def time(element, attribute, default=None):
        raw = element.get(attribute, default)
        if raw is None or not raw.endswith('s'):
            raise ValueError('missing or invalid ' + attribute)
        return Fraction(raw[:-1])

    def seconds(value):
        value = Fraction(value)
        return f'{value.numerator}/{value.denominator}s'

    def skip(mc, reason):
        report['skipped'].append(dict(name=mc.get('name', ''), offset=mc.get('offset', '0s'), reason=reason))

    def static_volume(element):
        # Animated volume uses the old component's time coordinate. Moving it
        # would require translating every animation/fade, so leave it untouched.
        return element.tag == 'adjust-volume' and not list(element)

    spines = []
    for path in ('./library/event/project/sequence/spine', './event/project/sequence/spine', './project/sequence/spine'):
        spines.extend(root.findall(path))
    for spine in spines:
        candidates = [(mc, mc.findall('clip')) for mc in list(spine) if mc.tag == 'mc-clip' and mc.findall('clip')]
        lanes = {child.get('lane') for _, children in candidates for child in children}
        for mc, children in candidates:
            try:
                if any(not lane or int(lane) == 0 for lane in lanes):
                    raise ValueError('声音没有明确的主声音lane')
                if mc.get('enabled', '1') != '1':
                    raise ValueError('多机位片段已禁用，保留原结构')
                if mc.get('srcEnable', 'all') == 'audio' or any(
                    source.get('srcEnable', 'all') not in ('video', 'none') for source in mc.findall('mc-source')
                ):
                    raise ValueError('多机位自身包含声音选择，保留原结构')
                if any(child.tag not in ('note', 'mc-source', 'clip', 'marker', 'chapter-marker', 'rating', 'keyword', 'analysis-marker', 'metadata') for child in mc):
                    raise ValueError('多机位含变速、声音效果或其他复杂组件，保留原结构')
                if any(mc.get(key) is not None for key in ('audioStart', 'audioDuration')):
                    raise ValueError('多机位含分离声音编辑，保留原结构')
                mc_start, mc_duration = time(mc, 'start', '0s'), time(mc, 'duration')
                if mc_duration <= 0:
                    raise ValueError('多机位时长无效')
                extracted = []
                intervals = []
                for child in children:
                    if set(child.attrib) - {'name', 'start', 'offset', 'duration', 'lane', 'enabled'}:
                        raise ValueError('声音容器含额外编辑属性，保留原结构')
                    if child.get('enabled', '1') != '1':
                        raise ValueError('声音片段已禁用，保留原结构')
                    audios = child.findall('audio')
                    if not audios or any(item.tag != 'audio' and not static_volume(item) for item in child):
                        raise ValueError('声音容器含标记、效果或嵌套结构，保留原结构')
                    child_start, child_offset = time(child, 'start', '0s'), time(child, 'offset', '0s')
                    child_duration = time(child, 'duration')
                    if child_duration <= 0:
                        raise ValueError('声音组件时长无效')
                    for audio in audios:
                        if audio.get('enabled', '1') != '1' or audio.get('lane') is not None:
                            raise ValueError('声音组件已禁用或带有独立lane，保留原结构')
                        if set(audio.attrib) - {'ref', 'name', 'start', 'offset', 'duration', 'enabled', 'srcID', 'srcCh', 'outCh', 'role'}:
                            raise ValueError('声音组件含额外编辑属性，保留原结构')
                        if any(not static_volume(item) for item in audio):
                            raise ValueError('声音组件含变速、标记、动画或效果，保留原结构')
                        child_volumes, audio_volumes = child.findall('adjust-volume'), audio.findall('adjust-volume')
                        if len(child_volumes) + len(audio_volumes) > 1:
                            raise ValueError('声音包含多层音量调整，保留原结构')
                        asset = assets.get(audio.get('ref'))
                        if asset is None or asset.get('hasAudio') != '1' or asset.get('hasVideo') == '1':
                            raise ValueError('声音引用不是独立音频素材，保留原结构')
                        audio_start, audio_offset = time(audio, 'start', '0s'), time(audio, 'offset', '0s')
                        audio_duration = time(audio, 'duration')
                        if audio_duration <= 0:
                            raise ValueError('声音组件时长无效')
                        # Keep each original WAV's source clock, including any
                        # subframe offset. A mono pair is two distinct sources.
                        audio_position = child_offset + audio_offset - child_start
                        first = max(mc_start, child_offset, audio_position)
                        last = min(mc_start + mc_duration, child_offset + child_duration, audio_position + audio_duration)
                        if last <= first:
                            raise ValueError('声音组件未覆盖多机位可见区间，保留原结构')
                        source_start = audio_start + first - audio_position
                        asset_start, asset_duration = time(asset, 'start', '0s'), time(asset, 'duration')
                        if source_start < asset_start or source_start + last - first > asset_start + asset_duration:
                            raise ValueError('声音源范围超过素材边界，保留原结构')
                        component = copy.deepcopy(audio)
                        component.set('offset', seconds(first))
                        component.set('start', seconds(source_start))
                        component.set('duration', seconds(last - first))
                        if child.get('name') and not component.get('name'):
                            component.set('name', child.get('name'))
                        if child_volumes:
                            component.insert(0, copy.deepcopy(child_volumes[0]))
                        extracted.append(component)
                        intervals.append((first, last, component))
                grouped = {}
                for first, last, component in intervals:
                    grouped.setdefault((first,last),[]).append(component)
                # Coincident components are safe only when their saved asset
                # identity explicitly declares one original left/right pair.
                def is_pair(components):
                    if len(components) != 2:
                        return False
                    identities = []
                    for component in components:
                        asset = assets[component.get('ref')]
                        metadata = {row.get('key'):row.get('value') for row in asset.findall('./metadata/md')}
                        identities.append((metadata.get('com.pgm.audioPairID'),
                                           metadata.get('com.pgm.audioPairChannel'),component.get('outCh')))
                        if asset.get('audioChannels') != '1' or component.get('srcCh','1').strip() != '1':
                            return False
                    return (identities[0][0] and identities[0][0] == identities[1][0]
                            and {(channel,output) for _,channel,output in identities} == {('L','L'),('R','R')}
                            and len({component.get('ref') for component in components}) == 2)
                if any(len(components)>1 and not is_pair(components) for components in grouped.values()):
                    raise ValueError('主声音组件范围重叠且不是明确的左右声道对，保留原结构')
                if len(lanes) != 1 and not all(is_pair(components) for components in grouped.values()):
                    raise ValueError('声音包含多个lane且缺少明确的左右声道对，保留原结构')
                if any(is_pair(components) for components in grouped.values()):
                    # Native Resolve 21 drops the second direct audio child on
                    # reimport. Preserve both original connected WAV clips;
                    # the import helper restores native links and L/R pan.
                    raise ValueError('左右单声道保留独立声音结构；使用配套达芬奇导入脚本恢复链接和声相')
                ordered = sorted(grouped)
                if any(right[0] < left[1] for left, right in zip(ordered, ordered[1:])):
                    raise ValueError('主声音组件范围重叠，保留原结构')
            except (ValueError, TypeError, ZeroDivisionError) as exc:
                skip(mc, str(exc))
                continue
            # Mutate only after every component passes; failed candidates stay
            # byte-for-byte equivalent at the element level.
            wrapper = ET.Element('clip', name=mc.get('name', ''), offset=mc.get('offset', '0s'),
                                 start=seconds(mc_start), duration=seconds(mc_duration))
            # A flat native export stores V2/V3 placement on this outer
            # multicam. Its linked A/V wrapper now occupies that exact track;
            # the primary picture inside must not create another connection.
            if mc.get('lane') is not None:
                wrapper.set('lane', mc.get('lane'))
            if mc.get('format') is not None:
                wrapper.set('format', mc.get('format'))
            index = list(spine).index(mc)
            spine.remove(mc)
            mc.set('offset', seconds(mc_start))
            mc.attrib.pop('lane', None)
            for child in children:
                mc.remove(child)
            wrapper.append(mc)
            for component in extracted:
                wrapper.append(component)
            spine.insert(index, wrapper)
            report['normalized_multicam_clips'] += 1
            report['normalized_audio_components'] += len(extracted)
    return report


def normalize_identified_audio_links(root, source_rows, guide_audio, path_mappings=None):
    """Restore native multi-track A/V pairs only from verified session identity.

    A connected alternate can extend beyond its parent picture. Walk source
    coordinates into absolute timeline coordinates without clipping to that
    parent, then rebuild each exact, unique picture/WAV pair in its own clip.
    No filename, lane number, nearest time or overlap is an identity criterion.
    A saved L/R pair stays in its connected structure for the Resolve helper;
    rebuilding it as two direct audio children would discard one channel.
    """
    from proxy_workflow import file_path
    from library_bridge import remap_path
    assets = {row.get('id'): row for row in root.findall('./resources/asset')}
    media = {row.get('id'): row for row in root.findall('./resources/media')}
    report = dict(normalized_multicam_clips=0, normalized_audio_components=0,
                  identity_matched=True, skipped=[])

    def path_key(value):
        if not value:
            return None
        value = remap_path(value, path_mappings or [])
        return str(Path(value).expanduser().resolve())

    video_sessions, sound_sessions, pgm_sound_sessions, sound_pairs = {}, {}, {}, {}
    for row in source_rows:
        if row.get('session_id'):
            for key in ('proxy_path', 'original_path'):
                path = path_key(row.get(key))
                if path:
                    video_sessions.setdefault(path, set()).add(row['session_id'])
    for row in guide_audio:
        if isinstance(row, dict) and row.get('session_id') and row.get('path'):
            path = path_key(row['path'])
            sound_sessions.setdefault(path, set()).add(row['session_id'])
            if row.get('pair_id') and row.get('channel') in ('L','R'):
                sound_pairs.setdefault((row['session_id'],path),set()).add((row['pair_id'],row['channel']))
            if row.get('source_kind') == 'pgm':
                pgm_sound_sessions.setdefault(path, set()).add(row['session_id'])

    def unique(values):
        return next(iter(values)) if len(values) == 1 else None

    multicam_sessions, multicam_candidates = {}, {}
    for identity, resource in media.items():
        sessions, complete = set(), True
        for component in resource.iter():
            asset = assets.get(component.get('ref'))
            if asset is None or asset.get('hasVideo') != '1':
                continue
            known = video_sessions.get(path_key(file_path(asset.get('src'))), set())
            if len(known) != 1:
                complete = False
            sessions.update(known)
        multicam_sessions[identity] = unique(sessions) if complete else None
        multicam_candidates[identity] = sessions

    def time(node, key, default=None):
        value = node.get(key, default)
        if value is None or not value.endswith('s'):
            raise ValueError('无效的时间坐标：'+key)
        return Fraction(value[:-1])

    def seconds(value):
        value = Fraction(value)
        return f'{value.numerator}/{value.denominator}s'

    def static_volume(node):
        return node.tag == 'adjust-volume' and not list(node)

    def picture(node, absolute):
        if set(node.attrib)-{'name','ref','offset','start','duration','lane','enabled','format','tcFormat','srcEnable'}:
            raise ValueError('多机位含额外编辑属性')
        if node.get('enabled','1') != '1' or node.get('srcEnable','all') == 'audio':
            raise ValueError('多机位已禁用或选择了声音')
        sources = node.findall('mc-source')
        if not sources or any(row.get('srcEnable','all') not in ('video','none') for row in sources):
            raise ValueError('多机位自身含声音选择')
        for source in sources:
            if any(child.tag not in ('adjust-conform','adjust-transform') or list(child) for child in source):
                raise ValueError('机位含变速、动画或复杂效果')
        if time(node,'duration') <= 0:
            raise ValueError('视频时长无效')
        return dict(node=node,absolute=absolute,duration=time(node,'duration'),
                    session=multicam_sessions.get(node.get('ref')),start=time(node,'start','0s'))

    def sound(node, absolute):
        if set(node.attrib)-{'name','start','offset','duration','lane','enabled'} or node.get('enabled','1') != '1':
            raise ValueError('声音容器已禁用或含额外编辑属性')
        audios = node.findall('audio')
        if not audios or any(child.tag != 'audio' and not static_volume(child) for child in node):
            raise ValueError('声音容器不含简单的原始声音组件')
        result = []
        duration = time(node,'duration')
        for audio in audios:
            if (set(audio.attrib)-{'ref','name','start','offset','duration','enabled','srcID','srcCh','outCh','role'} or
                    audio.get('enabled','1') != '1' or any(not static_volume(child) for child in audio)):
                raise ValueError('声音组件含变速、动画或复杂效果')
            if len(node.findall('adjust-volume'))+len(audio.findall('adjust-volume')) > 1:
                raise ValueError('声音包含多层音量调整')
            asset = assets.get(audio.get('ref'))
            if asset is None or asset.get('hasAudio') != '1':
                raise ValueError('声音资产缺少原始音频')
            path = path_key(file_path(asset.get('src')))
            session = unique(sound_sessions.get(path,set()))
            # Embedded PGM audio needs the saved fallback declaration.
            if asset.get('hasVideo') == '1' and (
                    not session or pgm_sound_sessions.get(path,set()) != {session}):
                raise ValueError('视频内嵌原声缺少明确PGM后备声音身份')
            position = absolute+time(audio,'offset','0s')-time(node,'start','0s')
            if duration <= 0 or position > absolute or position+time(audio,'duration') < absolute+duration:
                raise ValueError('声音底层组件未覆盖完整容器区间')
            source_start = time(audio,'start','0s')+absolute-position
            asset_start = time(asset,'start','0s')
            if source_start < asset_start or source_start+duration > asset_start+time(asset,'duration'):
                raise ValueError('声音源范围超过素材边界')
            pair = unique(sound_pairs.get((session,path),set()))
            result.append(dict(node=node,component=audio,absolute=absolute,duration=duration,path=path,
                               session=session,source_start=source_start,pair=pair))
        return result

    def skip(node, reason):
        report['skipped'].append(dict(name=node.get('name',''),offset=node.get('offset','0s'),reason=reason))

    timed = {'mc-clip','clip','gap'}
    decorations = {'note','mc-source','marker','chapter-marker','rating','keyword','analysis-marker','metadata'}

    def spine_identity_counts(spine):
        """Count potential owners before mutating any host or linked composite.

        Resolve can attach two coincident alternate takes to different primary
        clips. Local uniqueness inside either parent is therefore insufficient.
        Ambiguous identities count against every possible session, and existing
        linked/unsupported structures also prevent a duplicate from appearing
        uniquely owned elsewhere in the same spine.
        """
        videos, audios = {}, {}
        def add(table, sessions, absolute, duration):
            if duration > 0:
                for session in sessions:
                    key = (session,absolute,duration)
                    table[key] = table.get(key,0)+1
        def asset_sessions(node, table):
            asset = assets.get(node.get('ref'))
            return table.get(path_key(file_path(asset.get('src'))),set()) if asset is not None else set()
        def walk(node, absolute):
            try:
                duration, start = time(node,'duration'), time(node,'start','0s')
            except (ValueError,TypeError,ZeroDivisionError):
                return
            if node.tag == 'mc-clip':
                add(videos,multicam_candidates.get(node.get('ref'),set()),absolute,duration)
            elif node.tag == 'asset-clip':
                asset = assets.get(node.get('ref'))
                if asset is not None:
                    if asset.get('hasVideo') == '1' and node.get('srcEnable','all') != 'audio':
                        add(videos,asset_sessions(node,video_sessions),absolute,duration)
                    if asset.get('hasAudio') == '1' and node.get('srcEnable','all') != 'video':
                        add(audios,asset_sessions(node,sound_sessions),absolute,duration)
            for child in node:
                if child.tag not in timed | {'asset-clip','audio'}:
                    continue
                try:
                    position = absolute+time(child,'offset','0s')-start
                    if child.tag == 'audio':
                        # The whole WAV component is trimmed by its own clip,
                        # not by higher connected-video parents.
                        first = max(position,absolute)
                        end = min(position+time(child,'duration'),absolute+duration)
                        add(audios,asset_sessions(child,sound_sessions),first,end-first)
                    else:
                        walk(child,position)
                except (ValueError,TypeError,ZeroDivisionError):
                    continue
        for child in spine:
            if child.tag in timed | {'asset-clip'}:
                try:walk(child,time(child,'offset'))
                except (ValueError,TypeError,ZeroDivisionError):pass
        return videos,audios

    spines = []
    for location in ('./library/event/project/sequence/spine','./event/project/sequence/spine','./project/sequence/spine'):
        spines.extend(root.findall(location))
    for spine in spines:
        # Transitions need explicit trimming of their contributing overlaps.
        if spine.find('transition') is not None:
            for node in spine:
                if node.tag == 'mc-clip':
                    skip(node,'时间线包含转场，保留音视频原结构')
            continue
        global_pictures,global_sounds = spine_identity_counts(spine)
        for top in list(spine):
            if top.tag not in ('mc-clip','gap'):
                continue  # Existing linked composites are already canonical.
            pictures, sounds = [], []
            try:
                def walk(node, absolute, primary=False):
                    if node.tag == 'mc-clip':
                        pictures.append(picture(node,absolute))
                    elif node.tag == 'clip':
                        sounds.extend(sound(node,absolute))
                        return
                    elif node.tag != 'gap':
                        raise ValueError('包含不支持的嵌套媒体结构')
                    for child in node:
                        if child.tag in timed:
                            if not child.get('lane') or int(child.get('lane')) == 0:
                                raise ValueError('连接片段缺少明确轨道，保留原结构')
                            walk(child,absolute+time(child,'offset')-time(node,'start','0s'))
                        elif child.tag not in decorations:
                            raise ValueError('包含转场、变速或复杂效果')
                walk(top,time(top,'offset'),True)
            except (ValueError,TypeError,ZeroDivisionError) as exc:
                skip(top,str(exc))
                continue
            if not pictures or not sounds:
                continue
            # Both sides must be unique, including repeated same-session takes.
            def key(row):
                return row['session'],row['absolute'],row['duration']
            by_picture, by_sound = {}, {}
            for row in pictures:
                if row['session']:by_picture.setdefault(key(row),[]).append(row)
            for row in sounds:
                if row['session']:by_sound.setdefault(key(row),[]).append(row)
            def sound_group(matches):
                return len(matches) == 1 and not matches[0]['pair']
            pairs = []
            for video in pictures:
                matches = by_sound.get(key(video),[]) if video['session'] else []
                if (len(matches) == 2 and all(row['pair'] for row in matches)
                        and matches[0]['pair'][0] == matches[1]['pair'][0]
                        and {row['pair'][1] for row in matches} == {'L','R'}):
                    # Do not turn two connected original WAVs into two direct
                    # audio children: actual Resolve import silently drops R.
                    # Guide identities survive in the accompanying map, even
                    # when native FCPXML omits outCh, pan and custom metadata.
                    skip(video['node'],'左右单声道保留独立声音结构；使用配套达芬奇导入脚本恢复链接和声相')
                    continue
                if (sound_group(matches) and len(by_picture.get(key(video),[])) == 1
                        and global_pictures.get(key(video),0) == 1
                        and global_sounds.get(key(video),0) == len(matches)):
                    pairs.append((video,matches))
                else:
                    skip(video['node'],'场次或原声音身份不唯一，或音视频入出点不同，未强制链接')
            if not pairs:
                continue
            # Work on a complete clone: failed analysis never partly mutates XML.
            replacement = copy.deepcopy(top)
            copies = dict(zip(top.iter(),replacement.iter()))
            wrappers = {}
            for video,_ in reversed(pairs):
                original = video['node'];mc = copies[original]
                wrapper = ET.Element('clip',{key:value for key,value in mc.attrib.items()
                    if key in ('name','offset','start','duration','lane')})
                fmt = mc.get('format')
                if not fmt:
                    resource = media.get(mc.get('ref'))
                    multi = resource.find('multicam') if resource is not None else None
                    fmt = multi.get('format') if multi is not None else None
                if fmt:wrapper.set('format',fmt)
                if mc is replacement:
                    replacement = wrapper
                else:
                    parent = next(parent for parent in replacement.iter() if mc in list(parent))
                    position = list(parent).index(mc);parent.remove(mc);parent.insert(position,wrapper)
                mc.attrib.pop('lane',None)
                mc.set('offset',seconds(video['start']))
                wrapper.append(mc)
                # Connected clips retain their original source-coordinate offset
                # in the composite. Do not intersect with the parent's duration.
                for child in list(mc):
                    if child.tag in timed:
                        mc.remove(child);wrapper.append(child)
                wrappers[original] = wrapper
            removed = set()
            for video,audios in pairs:
                wrapper = wrappers[video['node']]
                for audio in audios:
                    container = copies[audio['node']]
                    if container not in removed:
                        parent = next(parent for parent in replacement.iter() if container in list(parent))
                        parent.remove(container);removed.add(container)
                    component = copy.deepcopy(audio['component'])
                    component.set('offset',seconds(video['start']))
                    component.set('start',seconds(audio['source_start']))
                    component.set('duration',seconds(audio['duration']))
                    if container.get('name') and not component.get('name'):component.set('name',container.get('name'))
                    for volume in container.findall('adjust-volume'):
                        component.insert(0,copy.deepcopy(volume))
                    wrapper.insert(1+len(wrapper.findall('audio')),component)
                    report['normalized_audio_components'] += 1
                report['normalized_multicam_clips'] += 1
            position=list(spine).index(top);spine.remove(top);spine.insert(position,replacement)
    return report


def standardize_connected_native_interchange(root, source_rows, guide_audio, path_mappings=None):
    """Flatten a fully identified native multi-track edit for reliable reimport.

    Resolve re-exports linked performances as connected children of a primary
    multicam, losing custom group IDs. Only simple original-media components
    with unique scene/record-range ownership are reconstructed. Effects,
    ambiguous identities and unsupported edits stay untouched for review.
    """
    from proxy_workflow import file_path
    from library_bridge import remap_path
    from collections import defaultdict
    from bisect import bisect_right
    report = dict(changed=False, normalized_multicam_clips=0, normalized_audio_components=0, skipped=[])
    sequence = root.find('./library/event/project/sequence')
    project = root.find('./library/event/project')
    if sequence is None or project is None:
        return report
    spine = sequence.find('spine')
    if spine is None or not any(node.tag in ('mc-clip','gap') and node.find('.//mc-clip') is not None for node in spine):
        return report
    path_cache={}
    def key(value):
        if value not in path_cache:
            path_cache[value]=str(Path(remap_path(value,path_mappings or [])).expanduser().resolve()) if value else ''
        return path_cache[value]
    def number(node,field,default=None):
        text=node.get(field,default)
        if text is None or not text.endswith('s'):raise ValueError('缺少有效时间坐标：'+field)
        return Fraction(text[:-1])
    def sec(value):
        value=Fraction(value);return f'{value.numerator}/{value.denominator}s'
    def md(node,field,value):
        metadata=node.find('metadata')
        if metadata is None:metadata=ET.SubElement(node,'metadata')
        existing=metadata.find('md[@key="'+field+'"]')
        if existing is None:existing=ET.SubElement(metadata,'md',key=field)
        existing.set('value',str(value))
    assets={row.get('id'):row for row in root.findall('./resources/asset')}
    media={row.get('id'):row for row in root.findall('./resources/media')}
    asset_paths={identity:key(file_path(asset.get('src'))) for identity,asset in assets.items()}
    root_nodes=set(spine)
    video_paths=defaultdict(set);sound_paths=defaultdict(list)
    for row in source_rows:
        if row.get('session_id'):
            for field in ('proxy_path','original_path'):
                if row.get(field):video_paths[key(row[field])].add(row['session_id'])
    for row in guide_audio:
        if row.get('session_id') and row.get('path'):
            sound_paths[key(row['path'])].append(row)
    multicams={}
    for identity,resource in media.items():
        owners=set();complete=True
        for child in resource.iter():
            asset=assets.get(child.get('ref'))
            if asset is not None and asset.get('hasVideo')=='1':
                values=video_paths.get(asset_paths.get(asset.get('id')),set())
                complete &= len(values)==1
                owners.update(values)
        multicams[identity]=next(iter(owners)) if complete and len(owners)==1 else None
    pictures=[];sounds=[];gaps=[]
    timed={'mc-clip','clip','gap'}
    decorations={'note','marker','chapter-marker','rating','keyword','analysis-marker','metadata'}
    try:
        origin=number(sequence,'tcStart','0s');duration=number(sequence,'duration')
        if duration<=0:raise ValueError('时间线时长无效')
        def walk(node,absolute,parent_lane=0):
            start=number(node,'start','0s');length=number(node,'duration')
            if length<=0 or node.get('enabled','1')!='1':raise ValueError('片段禁用或时长无效')
            if node.tag=='mc-clip':
                if set(node.attrib)-{'ref','name','offset','start','duration','lane','enabled','format','tcFormat','srcEnable'}:
                    raise ValueError('多机位包含复杂编辑属性')
                lane=int(node.get('lane',str(parent_lane)))
                if lane<0 or parent_lane and node.get('lane') is not None:raise ValueError('嵌套视频轨道不能安全确定')
                session=multicams.get(node.get('ref'))
                if not session:raise ValueError('多机位缺少唯一原片场次身份')
                if node.get('srcEnable','all')=='audio' or any(row.get('srcEnable','all') not in ('video','none') for row in node.findall('mc-source')):
                    raise ValueError('多机位自身含声音选择')
                for source in node.findall('mc-source'):
                    if any(child.tag not in ('adjust-conform','adjust-transform') or list(child) for child in source):
                        raise ValueError('机位含动画、变速或复杂效果')
                pictures.append(dict(node=node,absolute=absolute,duration=length,start=start,lane=lane,session=session))
                parent_lane=lane
            elif node.tag=='clip':
                if set(node.attrib)-{'name','offset','start','duration','lane','enabled'}:
                    raise ValueError('声音容器含复杂编辑属性')
                if any(child.tag!='audio' and not(child.tag=='adjust-volume' and not list(child)) for child in node):
                    raise ValueError('声音容器含复杂嵌套或效果')
                if not node.findall('audio'):raise ValueError('声音容器没有原始声音')
                for audio in node.findall('audio'):
                    if (set(audio.attrib)-{'ref','name','start','offset','duration','enabled','srcID','srcCh','outCh','role'}
                            or audio.get('enabled','1')!='1' or any(child.tag!='adjust-volume' or list(child) for child in audio)):
                        raise ValueError('声音组件含复杂编辑属性')
                    asset=assets.get(audio.get('ref'));path=asset_paths.get(audio.get('ref'),'')
                    owners=sound_paths.get(path,[])
                    sessions={row['session_id'] for row in owners}
                    if asset is None or asset.get('hasAudio')!='1' or len(sessions)!=1:raise ValueError('原始声音场次身份不唯一')
                    pair_values={(row.get('pair_id'),row.get('channel')) for row in owners if row.get('pair_id')}
                    if len(pair_values)>1:raise ValueError('左右声音身份冲突')
                    pair=next(iter(pair_values)) if pair_values else None
                    channels=int(asset.get('audioChannels','1'))
                    if pair and channels!=1:raise ValueError('左右原声音不是独立单声道素材')
                    selected=[part.strip() for part in audio.get('srcCh',', '.join(str(n) for n in range(1,channels+1))).split(',')]
                    if selected!=[str(n) for n in range(1,channels+1)]:raise ValueError('非完整声音通道选择需要保留原结构')
                    audio_position=absolute+number(audio,'offset','0s')-start
                    first=max(absolute,audio_position);last=min(absolute+length,audio_position+number(audio,'duration'))
                    source_start=number(audio,'start','0s')+first-audio_position
                    if first>=last or source_start<number(asset,'start','0s') or source_start+last-first>number(asset,'start','0s')+number(asset,'duration'):
                        raise ValueError('原声音源范围无效')
                    volumes=node.findall('adjust-volume')+audio.findall('adjust-volume')
                    if len(volumes)>1:raise ValueError('声音多层音量调整需保留原结构')
                    sounds.append(dict(node=audio,asset=asset,absolute=first,duration=last-first,source_start=source_start,
                        session=next(iter(sessions)),pair=pair,channels=channels,name=audio.get('name') or node.get('name',''),volumes=volumes))
                return
            elif node.tag=='gap':
                if any(child.tag not in timed|decorations for child in node):raise ValueError('空隙包含复杂结构')
                if node in root_nodes:gaps.append((node,absolute))
            else:raise ValueError('包含不支持的时间线组件')
            for child in node:
                if child.tag in timed:walk(child,absolute+number(child,'offset','0s')-start,parent_lane)
                elif child.tag not in decorations|{'mc-source'}:raise ValueError('包含转场、变速或复杂效果')
        for node in spine:
            if node.tag not in timed:raise ValueError('包含不支持的主时间线组件')
            walk(node,number(node,'offset','0s'))
        if not pictures or not sounds:return report
        assigned=defaultdict(list)
        scene_pictures=defaultdict(list)
        for picture in pictures:
            if picture['absolute']<origin or picture['absolute']+picture['duration']>origin+duration:
                raise ValueError('画面超出时间线范围')
            scene_pictures[picture['session']].append(picture)
        scene_indexes={}
        for session,rows in scene_pictures.items():
            rows.sort(key=lambda row:row['absolute'])
            maximum=None;ends=[]
            for row in rows:
                maximum=max(maximum if maximum is not None else row['absolute'],row['absolute']+row['duration']);ends.append(maximum)
            scene_indexes[session]=rows,[row['absolute'] for row in rows],ends
        for sound in sounds:
            rows,starts,ends=scene_indexes.get(sound['session'],([],[],[]))
            position=bisect_right(starts,sound['absolute'])-1;choices=[]
            last=sound['absolute']+sound['duration']
            while position>=0 and ends[position]>=last:
                row=rows[position]
                if row['absolute']+row['duration']>=last:choices.append(row)
                if len(choices)>1:break
                position-=1
            if len(choices)!=1:raise ValueError('画面与原声音区间不能唯一对应')
            assigned[id(choices[0])].append(sound)
        by_lane=defaultdict(list)
        for picture in pictures:by_lane[picture['lane']].append((picture['absolute'],picture['absolute']+picture['duration']))
        for intervals in by_lane.values():
            intervals.sort()
            if any(right[0]<left[1] for left,right in zip(intervals,intervals[1:])):raise ValueError('同一画面轨道存在重叠')
        kinds=defaultdict(set)
        for picture in pictures:
            grouped=defaultdict(list)
            for sound in assigned[id(picture)]:grouped[sound['absolute'],sound['duration']].append(sound)
            ordered=sorted(grouped)
            if any(right[0]<left[0]+left[1] for left,right in zip(ordered,ordered[1:])):
                raise ValueError('同一画面内的原声音区间存在重叠')
            for members in grouped.values():
                pairs=[sound['pair'] for sound in members if sound['pair']]
                if pairs and not(len(members)==2 and len(pairs)==2 and pairs[0][0]==pairs[1][0] and {row[1] for row in pairs}=={'L','R'}):
                    raise ValueError('原始左右声道不完整或范围不一致')
                if len(members)>1 and not pairs:raise ValueError('同区间存在多个非配对原声音')
            for sound in assigned[id(picture)]:
                sound['category']='pair' if sound['pair'] else 'stereo' if sound['channels']>1 else 'mono'
                kinds[picture['lane']+1].add(sound['category'])
        allocation={};audio_layout=[]
        legacy_pairs=any(sound['pair'] for sound in sounds)
        if legacy_pairs:
            # A historical edit can still reference two untouched mono WAVs.
            # Retain both independent items rather than sum/drop one channel
            # or render new media during an original-picture reconform.
            cursor=1
            for track in range(1,max(row['lane'] for row in pictures)+2):
                for category in ('stereo','mono','pair'):
                    if category not in (kinds.get(track) or {'stereo'}):continue
                    allocation[track,category]=cursor
                    count=2 if category=='pair' else 1
                    for channel in range(count):audio_layout.append(dict(index=cursor+channel,video_track=track,subtype='stereo' if category=='stereo' else 'mono',name=f'V{track} 原始声音'))
                    cursor+=count
        else:
            for track in range(1,max(row['lane'] for row in pictures)+2):
                categories=kinds.get(track,set())
                for category in categories:allocation[track,category]=track
                audio_layout.append(dict(index=track,video_track=track,subtype='stereo',
                    name=f'V{track} / A{track}',source_formats=sorted(categories),empty=not bool(categories)))
    except (ValueError,TypeError,ZeroDivisionError,KeyError) as exc:
        report['skipped'].append(dict(reason=str(exc)))
        return report
    # Commit only after all source identities, ranges and routes pass validation.
    replacement=[]
    for ordinal,picture in enumerate(pictures):
        group='conform-av-'+str(ordinal);node=copy.deepcopy(picture['node'])
        for child in list(node):
            if child.tag in timed:node.remove(child)
        node.set('offset',sec(picture['absolute']-origin));node.attrib.pop('lane',None)
        # Native exports omit this outer flag even with video-only mc-source.
        # Explicitly suppress camera scratch audio; original audio lives in the
        # independently identified asset clips below.
        node.set('srcEnable','video')
        if picture['lane']:node.set('lane',str(picture['lane']))
        md(node,'com.pgm.linkGroupID',group);replacement.append(node)
        for sound in assigned[id(picture)]:
            track=allocation[picture['lane']+1,sound['category']]+(1 if sound['pair'] and sound['pair'][1]=='R' else 0)
            leaf=ET.Element('asset-clip',ref=sound['node'].get('ref'),name=sound['name'],srcEnable='audio',
                offset=sec(sound['absolute']-origin),start=sec(sound['source_start']),duration=sec(sound['duration']),
                lane=str(-track),audioRole=f'dialogue.pgm-track-{track}')
            for volume in sound['volumes']:leaf.append(copy.deepcopy(volume))
            md(leaf,'com.pgm.linkGroupID',group);replacement.append(leaf)
            if sound['pair']:
                md(sound['asset'],'com.pgm.audioPairID',sound['pair'][0]);md(sound['asset'],'com.pgm.audioPairChannel',sound['pair'][1])
    for original,absolute in gaps:
        node=copy.deepcopy(original)
        for child in list(node):
            if child.tag in timed:node.remove(child)
        node.set('offset',sec(absolute-origin));replacement.append(node)
    ownership={'conform-av-'+str(ordinal):(picture['lane'],picture['absolute']-origin)
               for ordinal,picture in enumerate(pictures)}
    def order(node):
        entry=node.find('./metadata/md[@key="com.pgm.linkGroupID"]')
        owner=ownership.get(entry.get('value')) if entry is not None else None
        lane,position=owner if owner is not None else (0,number(node,'offset'))
        return lane,position,0 if node.tag in ('mc-clip','gap') else 1,number(node,'offset'),abs(int(node.get('lane','0')))
    replacement.sort(key=order)
    spine.clear();spine.extend(replacement);sequence.set('tcStart','0s')
    md(project,'com.pgm.requestedTimelineStart',sec(origin))
    report.update(changed=True,normalized_multicam_clips=len(pictures),normalized_audio_components=len(sounds),audio_tracks=audio_layout,
                  requested_timeline_start=sec(origin),routing_policy='legacy_split_mono' if legacy_pairs else 'video_audio_ordinal')
    return report


def conform_fcpxml(tree,index,fps,xml_path,output_path,identity_index=None,path_mappings=None,guide_audio=None,video_fallbacks=None):
    from core import AnalysisError,probe,validate_rate,tc_frames
    from proxy_workflow import file_path
    from library_bridge import remap_path
    root=tree.getroot()
    if root.get('version')!='1.8':raise AnalysisError('请从达芬奇导出FCPXML 1.8，当前原片回套不支持其他FCPXML版本。')
    resources=root.find('resources')
    if resources is None:raise AnalysisError('FCPXML缺少素材资源。')
    pgm_assets=_pgm_video_assets(root,video_fallbacks or [],path_mappings or [])
    pgm_wavs_removed,selected_pgm=_silence_pgm_fallback_wavs(root,pgm_assets,guide_audio,path_mappings or [])
    formats={x.get('id'):x for x in resources.findall('format')}
    for fmt in formats.values():
        duration=fmt.get('frameDuration')
        if duration and Fraction(duration[:-1]) != Fraction(1,fps):raise AnalysisError('FCPXML帧率与对应表不一致。')
    camera_paths={value for row in index.values() for value in (row.get('proxy_path'),row.get('original_path')) if value}
    camera_paths.update(index)
    # An immutable source ID can identify a relocated proxy before its video
    # reference is swapped; the old embedded sound still belongs to that camera.
    for asset in resources.findall('asset'):
        uid=next((node.get('value') for node in asset.findall('./metadata/md')
                  if node.get('key')=='com.pgm.sourceID'),None)
        if uid in (identity_index or {}):
            current=remap_path(file_path(asset.get('src')),path_mappings or [])
            if current:camera_paths.add(current)
    audio_cleanup=strip_camera_reference_audio(root,camera_paths,guide_audio,path_mappings)
    from proxy_workflow import original_wav_guide_audio
    wav_guide_audio=original_wav_guide_audio(guide_audio or [],path_mappings or ())
    verified={};replaced={};retained=set();counter=0;relocated=0;pgm_retained=set()
    for asset in resources.findall('asset'):
        before=file_path(asset.get('src'))
        after=remap_path(before,path_mappings or [])
        if before and after!=before:
            asset.set('src',Path(after).resolve().as_uri());relocated+=1
    # Video/audio components can share a MOV asset. Clone the asset for video
    # and keep its proxy audio asset intact, so rough sound survives grading.
    refs=[x for x in root.iter('video')]
    for asset in list(resources.findall('asset')):
        path=remap_path(file_path(asset.get('src')),path_mappings or []);row=index.get(path)
        if asset.get('id') in pgm_assets:
            pgm_retained.add(path or asset.get('src',''));continue
        uid=next((m.get('value') for m in asset.findall('./metadata/md') if m.get('key')=='com.pgm.sourceID'),None)
        if uid:
            identity_row=(identity_index or {}).get(uid)
            if identity_row is not None:
                if row and any(row.get(k)!=identity_row.get(k) for k in ('original_path','proxy_start','frames')):
                    raise AnalysisError('XML素材唯一编号与路径对应关系冲突：'+str(path))
                row=identity_row
            elif row:
                raise AnalysisError('XML含素材唯一编号，请选择同批次导出的对应JSON：'+str(path))
        if not row:
            retained.add(path or asset.get('src',''));continue
        original=row.get('original_path')
        if not original:raise AnalysisError('代理尚未对应原片：'+path)
        if original not in verified:
            info=probe(original);validate_rate(info,fps)
            actual=row.get('original_start')
            if actual is None and info['start_tc']:actual=tc_frames(info['start_tc'],fps)
            if actual!=row['proxy_start'] or info['frames']!=row['frames']:raise AnalysisError('原片与代理时码/帧数不一致：'+original)
            verified[original]=info
        if asset.get('start') and Fraction(asset.get('start')[:-1])*fps!=row['proxy_start']:raise AnalysisError('FCPXML素材源时码与映射不一致：'+path)
        counter+=1;info=verified[original];old=asset.get('id');new=copy.deepcopy(asset)
        new_id=f'{old}-original-{counter}';new.set('id',new_id);new.set('src',Path(original).resolve().as_uri());new.set('name',Path(original).name)
        for key in ('hasAudio','audioSources','audioChannels','audioRate'):new.attrib.pop(key,None)
        if info.get('audio_channels'):
            new.set('hasAudio','1');new.set('audioSources','1');new.set('audioChannels',str(len(info['audio_channels'])))
            new.set('audioRate',str(info['audio_channels'][0]['sample_rate']))
        fmt=formats.get(asset.get('format'))
        if fmt is not None:
            new_fmt=copy.deepcopy(fmt);fmt_id=f'{fmt.get("id")}-original-{counter}';new_fmt.set('id',fmt_id)
            new_fmt.set('width',str(info['width']));new_fmt.set('height',str(info['height']));new_fmt.attrib.pop('name',None)
            resources.append(new_fmt);new.set('format',fmt_id)
        resources.append(new);changed=False
        for item in refs:
            if item.get('ref')==old:item.set('ref',new_id);changed=True
        # Resolve may export a video-only asset-clip instead of a video element.
        for item in root.iter('asset-clip'):
            if item.get('ref')==old:
                if item.get('srcEnable')=='video' or not asset.get('hasAudio'):
                    item.set('ref',new_id);changed=True
                else:
                    raise AnalysisError('这份FCPXML把代理音视频合在同一asset-clip中。请先将多机位展开后导出Final Cut Pro 7 XML，或保留独立声音引用后导出FCPXML。')
        if changed:replaced[old]=original
        else:resources.remove(new)
    if not replaced and not selected_pgm:raise AnalysisError('未找到可回套的代理视频引用；请检查素材路径及对应表。')
    output=Path(output_path)
    if output.resolve()==Path(xml_path).resolve():raise AnalysisError('回套文件不能覆盖剪辑XML。')
    # Cloned originals may expose scratch sound in their own media header; the
    # edit retains only the original WAV components, never camera sound.
    strip_camera_reference_audio(root,camera_paths,guide_audio,path_mappings)
    native_interchange = (standardize_connected_native_interchange(root,index.values(),wav_guide_audio,path_mappings)
                          if wav_guide_audio else dict(changed=False))
    links=(native_interchange if native_interchange.get('changed') else
           normalize_identified_audio_links(root,index.values(),wav_guide_audio,path_mappings)
           if wav_guide_audio else normalize_native_audio_links(root))
    ET.indent(root,space='  ')
    output.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'+ET.tostring(root,encoding='unicode')+'\n',encoding='utf-8')
    from resolve_import_helper import write_resolve_import_helper
    from proxy_workflow import normalize_guide_audio
    import_helper = write_resolve_import_helper(output, wav_guide_audio,
        audio_layout=native_interchange.get('audio_tracks') if native_interchange.get('changed') else None)
    report=dict(replaced_file_definitions=len(replaced),unmapped_retained=sorted(retained),relocated_file_definitions=relocated,
                pgm_video_fallbacks_retained=sorted(pgm_retained),pgm_fallback_wavs_removed=pgm_wavs_removed,
                audio_link_normalization=links,camera_audio_cleanup=audio_cleanup,
                note='回套多机位内部的视频组件，保留原始声音引用；简单主声音结构重建为可链接的音视频组件，复杂声音编辑保留原结构并列入报告。')
    if import_helper:
        report['resolve_import_helper'] = str(import_helper)
    output.with_suffix('.review.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report
