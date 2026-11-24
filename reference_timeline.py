"""Place selected performances above a preserved continuous reference take.

Dialogue IDs locate alternatives in the reference edit. Source timecodes only
locate material within its own session; clocks are never compared across takes.
All arithmetic is rational/frame based and this module never probes media.
"""
from __future__ import annotations

import copy
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET


class ReferenceTimelineError(ValueError):
    pass


def _number(value, label):
    try:
        result = Fraction(str(value))
    except (ValueError, TypeError, ZeroDivisionError) as exc:
        raise ReferenceTimelineError(label + '无效。') from exc
    return result


def _frames(value, label):
    result = _number(value, label)
    if result.denominator != 1:
        raise ReferenceTimelineError(label + '必须是整数帧。')
    return int(result)


def _path(value):
    return str(Path(value).expanduser()) if value else ''


def _cut(cuts, session_id):
    cut = (cuts or {}).get(session_id)
    if not isinstance(cut, dict):
        raise ReferenceTimelineError('请先导入并核对该场次的切点 XML：' + str(session_id))
    fps = _frames(cut.get('fps'), '切点帧率')
    duration = _frames(cut.get('duration'), '切点时长')
    if fps not in (24, 25, 30) or duration <= 0:
        raise ReferenceTimelineError('切点 XML 的帧率或时长无效。')
    blocks, paths, cursor = [], set(), 0
    for row in cut.get('pgm_sources', []):
        path = _path(row.get('path'))
        first = _frames(row.get('start'), 'PGM 记录入点')
        last = _frames(row.get('end'), 'PGM 记录出点')
        frames = _frames(row.get('frames'), 'PGM 源时长')
        if not path or path in paths or first != cursor or last <= first or frames != last-first:
            raise ReferenceTimelineError('PGM 切点覆盖不完整、重复或不连续。')
        paths.add(path)
        blocks.append(dict(row, path=path, start=first, end=last, frames=frames))
        cursor = last
    if cursor != duration:
        raise ReferenceTimelineError('切点 XML 时长与 PGM 覆盖不一致。')
    return dict(cut, fps=fps, duration=duration, pgm_sources=blocks)


def _position(candidate, transcripts, cuts):
    source = transcripts.get(candidate.get('transcript_id')) or candidate
    session_id = source.get('session_id') or candidate.get('session_id')
    cut = _cut(cuts, session_id)
    path = _path(source.get('media_path') or candidate.get('media_path'))
    begin = _number(source.get('start_seconds'), '对白源入点')
    end = _number(source.get('end_seconds'), '对白源出点')
    if begin < 0 or end <= begin:
        raise ReferenceTimelineError('对白源范围无效。')
    block = next((row for row in cut['pgm_sources'] if row['path'] == path), None)
    if block is None:
        raise ReferenceTimelineError('对白对应的 PGM 不在该场次切点 XML 中：' + path)
    first, last = math.floor(begin*cut['fps']), math.ceil(end*cut['fps'])
    if last > block['frames']:
        raise ReferenceTimelineError('对白范围超出 PGM 素材末端：' + path)
    return dict(session_id=session_id, pgm_path=path, fps=cut['fps'],
                pgm_frame_in=first, pgm_frame_out_exclusive=last,
                record_frame_in=block['start']+first,
                record_frame_out_exclusive=block['start']+last,
                transcript_id=candidate.get('transcript_id'),
                candidate_id=candidate.get('id'), script_line_id=candidate.get('script_line_id'))


def build_reference_plan(data, baseline_session_id, selection_ids=(),
                         baseline_candidate_ids=None, cuts=None, baseline_script_map=None):
    """Return a serializable plan; explicit occurrence IDs resolve repeated lines.

    ``cuts`` maps immutable session IDs to already validated cut dictionaries.
    Their PGM paths must use the same shared/canonical spelling as ``data``.
    ``selection_ids`` contains candidate IDs or dictionaries with candidate_id
    and optional baseline_candidate_id. A complete take must pass the whole-file
    text policy (or human review), with a separate accepted first-line anchor.
    No unique baseline anchor is inferred from recording timecodes.
    """
    from library_correspondence import filter_active_transcripts
    data = filter_active_transcripts(data)
    sessions = {row.get('id'): row for row in data.get('sessions', [])}
    if baseline_session_id not in sessions:
        raise ReferenceTimelineError('请选择已登记的基准通条场次。')
    baseline_cut = _cut(cuts, baseline_session_id)
    transcripts = {row.get('id'): row for row in data.get('transcripts', [])}
    lines = {row.get('id') or row.get('script_line_id'): row for row in data.get('scripts', [])
             if row.get('active', row.get('script_active', True))}
    candidates = {row.get('id'): row for row in data.get('candidates', [])
                  if row.get('status') in ('candidate', 'confirmed') and row.get('script_active', True)
                  and row.get('active', True) and row.get('script_line_id') in lines}
    from pgm_takes import PgmTakeIndex
    take_index = PgmTakeIndex(data, prepared=True)
    mapping, mapped_lines = None, {}
    if baseline_script_map is not None:
        from baseline_script_map import validate_map, build_line_index
        try:
            mapping = validate_map(baseline_script_map, data, baseline_cut, baseline_session_id)
        except ValueError as exc:
            raise ReferenceTimelineError(str(exc)) from exc
        mapped_lines = build_line_index(mapping)
    anchors = {}
    for candidate in (() if mapping is not None else candidates.values()):
        source = transcripts.get(candidate.get('transcript_id')) or candidate
        if ((source.get('session_id') or candidate.get('session_id')) == baseline_session_id and
                take_index.association_ready(candidate)):
            anchors.setdefault(candidate['script_line_id'], []).append(candidate)
    explicit = baseline_candidate_ids or {}
    if not isinstance(explicit, dict):
        raise ReferenceTimelineError('基准台词选择必须使用台词 ID 与候选 ID 的对应表。')
    selected, seen = [], set()
    line_order = {identity: index for index, identity in enumerate(lines)}
    for selection in selection_ids:
        if isinstance(selection, dict):
            identity = selection.get('candidate_id') or selection.get('id')
            anchor_id = selection.get('baseline_candidate_id')
        else:
            identity, anchor_id = selection, None
        candidate = candidates.get(identity)
        if candidate is None:
            raise ReferenceTimelineError('所选片段尚未完成台本核对，或对应关系已失效：' + str(identity))
        key = take_index.key(candidate)
        try:
            first_candidate = take_index.selection_candidate(candidate)
        except ValueError as exc:
            raise ReferenceTimelineError(str(exc)) from exc
        if (candidate['script_line_id'] != first_candidate['script_line_id'] and
                candidates.get(anchor_id, {}).get('script_line_id') != first_candidate['script_line_id']):
            # Legacy choices may point at later sentences in the same file.
            # They select the take, but cannot determine its first-line anchor.
            anchor_id = None
        candidate = first_candidate
        if key in seen:
            previous = next(row for row in selected if (row['session_id'], row['pgm_path']) == key)
            if anchor_id and anchor_id != previous['baseline_candidate_id']:
                raise ReferenceTimelineError('同一 PGM 文件指定了不同的基准位置，请核对后只选一次。')
            continue
        seen.add(key)
        located = _position(candidate, transcripts, cuts)
        if located['session_id'] == baseline_session_id:
            raise ReferenceTimelineError('备选片段请选择其他场次，基准通条已保留在 V1。')
        if located['fps'] != baseline_cut['fps']:
            raise ReferenceTimelineError('基准与备选场次的帧率必须一致，不进行变速。')
        line_id = candidate['script_line_id']
        mapped_segment = mapped_lines.get(line_id) if mapping is not None else None
        if mapping is not None:
            line_number = take_index.script_positions.get(line_id, 0)+1
            if (not mapped_segment or mapped_segment.get('needs_review')
                    or mapped_segment.get('anchor_frame') is None
                    or mapped_segment['line_ids'][0] != line_id):
                raise ReferenceTimelineError(f'台本第{line_number}行尚无已核实的基准起点，请在“基准台本映射”中调整区间或首句定位。')
            baseline_position = mapped_segment['anchor_frame']
            baseline_candidate_id = None
            baseline_transcript_id = None
        else:
            options = anchors.get(line_id, [])
            anchor_id = anchor_id or explicit.get(line_id)
            if anchor_id:
                anchor = next((row for row in options if row['id'] == anchor_id), None)
                if anchor is None:
                    raise ReferenceTimelineError('指定的基准片段与所选台词不对应，或尚未核对：' + str(anchor_id))
            elif len(options) == 1:
                anchor = options[0]
            elif not options:
                raise ReferenceTimelineError('基准通条中没有已核对的对应台词：' + str(line_id))
            else:
                raise ReferenceTimelineError('基准通条中该台词出现多次，请明确选择对应的基准片段：' + str(line_id))
            baseline_position = _position(anchor, transcripts, cuts)['record_frame_in']
            baseline_candidate_id, baseline_transcript_id = anchor['id'], anchor.get('transcript_id')
        block = next(row for row in _cut(cuts, located['session_id'])['pgm_sources']
                     if row['path'] == located['pgm_path'])
        dialogue_in = located['pgm_frame_in']
        duration = block['frames']
        destination = baseline_position-dialogue_in
        row = dict(located, baseline_candidate_id=baseline_candidate_id,
                   baseline_transcript_id=baseline_transcript_id,
                   baseline_segment_id=mapped_segment['id'] if mapped_segment else None,
                   pgm_frame_in=0, pgm_frame_out_exclusive=duration,
                   record_frame_in=block['start'], record_frame_out_exclusive=block['end'],
                   dialogue_pgm_frame_in=dialogue_in, baseline_dialogue_frame_in=baseline_position,
                   whole_pgm=True, target_frame_in=destination,
                   target_frame_out_exclusive=destination+duration,
                   duration_frames=duration, session_name=sessions.get(located['session_id'], {}).get('name', ''),
                   script_text=lines[line_id].get('text', ''),
                   script_order=lines[line_id].get('order', lines[line_id].get('line_order', line_order[line_id])))
        selected.append(row)
    def order(row):
        try:
            value = float(row['script_order'])
            if not math.isfinite(value):
                raise ValueError()
        except (ValueError, TypeError):
            value = line_order[row['script_line_id']]
        return value, line_order[row['script_line_id']], row['target_frame_in'], row['session_name'], row['candidate_id']
    selected.sort(key=order)
    warnings = []
    if any(right['target_frame_in'] < left['target_frame_in']
           for left, right in zip(selected, selected[1:])):
        warnings.append('基准通条中的已选台词位置与台本顺序不一致；已保留所选基准片段的实际位置，未重排基准或按时码跨场移动。')
    lanes = []
    for row in selected:
        first, last = row['target_frame_in'], row['target_frame_out_exclusive']
        lane = next((index for index, intervals in enumerate(lanes)
                     if all(last <= begin or first >= end for begin, end in intervals)), len(lanes))
        if lane == len(lanes):
            lanes.append([])
        lanes[lane].append((first, last))
        row['lane'] = lane+1
    start_frame = min([0]+[row['target_frame_in'] for row in selected])
    end_frame = max([baseline_cut['duration']]+[row['target_frame_out_exclusive'] for row in selected])
    return dict(version=2, baseline_session_id=baseline_session_id,
                baseline_session_name=sessions[baseline_session_id].get('name', ''),
                fps=baseline_cut['fps'], baseline_duration_frames=baseline_cut['duration'],
                timeline_start_frame=start_frame, timeline_end_frame=end_frame,
                duration_frames=end_frame-start_frame,
                lane_count=len(lanes), selections=selected, warnings=warnings,
                baseline_mapping_fingerprint=mapping.get('evidence_fingerprint') if mapping else None,
                policy='V1 保留基准通条；整条 PGM 的台词覆盖匹配度须超过60%或人工确认，以第一句匹配度超过60%或人工指定的台词起点对齐基准同句；保留片头片尾、原速、多机位切换和原始声音。')


def _time(element, key, default=None):
    value = element.get(key, default)
    if not isinstance(value, str) or not value.endswith('s'):
        raise ReferenceTimelineError('FCPXML 时间属性无效：' + key)
    return _number(value[:-1], 'FCPXML '+key)


def _seconds(value):
    value = Fraction(value)
    return f'{value.numerator}/{value.denominator}s'


def _xml(value):
    try:
        if isinstance(value, ET.ElementTree):
            root = copy.deepcopy(value.getroot())
        elif isinstance(value, ET.Element):
            root = copy.deepcopy(value)
        else:
            root = ET.parse(value).getroot()
    except (OSError, ET.ParseError, TypeError) as exc:
        raise ReferenceTimelineError('无法读取多机位 XML：'+str(exc)) from exc
    if root.tag != 'fcpxml' or root.get('version') != '1.8':
        raise ReferenceTimelineError('基准合并只接受本软件生成的 FCPXML 1.8 多机位文件。')
    sequences = root.findall('./library/event/project/sequence')
    if len(sequences) != 1 or root.find('resources') is None or sequences[0].find('spine') is None:
        raise ReferenceTimelineError('多机位 XML 必须包含一条完整时间线。')
    return root, sequences[0]


def _prefix(root, prefix):
    resources = root.find('resources')
    identifiers = [node.get('id') for node in resources.iter() if node.get('id')]
    if len(set(identifiers)) != len(identifiers):
        raise ReferenceTimelineError('多机位 XML 资源 ID 重复。')
    names = {identity: prefix+identity for identity in identifiers}
    # Resolve resolves angleID across the imported XML, not solely inside the
    # referenced multicam. Reusing angle-A in two recordings merges their A
    # angles and can switch an alternative to the baseline's actual media.
    angle_ids = [node.get('angleID') for node in resources.iter('mc-angle') if node.get('angleID')]
    if len(set(angle_ids)) != len(angle_ids):
        raise ReferenceTimelineError('多机位 XML 角度 ID 重复，请重新导出各场多机位文件。')
    angles = {identity: prefix+identity for identity in angle_ids}
    for node in root.iter():
        for key in ('id', 'ref', 'format'):
            value = node.get(key)
            if value in names:
                node.set(key, names[value])
        angle = node.get('angleID')
        if angle in angles:
            node.set('angleID', angles[angle])


def _unique_multicam_names(root, resource_sessions):
    """Disambiguate colliding Resolve media names without touching identity.

    Separate scenes may legitimately retain the default S01 export label.
    Resolve's import helper identifies multicams by their media-pool names,
    so resource IDs alone cannot disambiguate those imports. One shared
    resource referenced by many edits remains one resource and is unchanged.
    """
    media=[row for row in root.findall('./resources/media') if row.find('multicam') is not None]
    reserved={row.get('name') for row in media if row.get('name')}
    assigned={};renamed={}
    for resource in media:
        name=resource.get('name');identity=resource.get('id')
        if not name:continue
        if name not in assigned:
            assigned[name]=identity;continue
        if assigned[name]==identity:continue
        session=str(resource_sessions.get(identity) or identity)
        token=hashlib.sha256(session.encode('utf-8')).hexdigest()[:8]
        candidate=f'{name} [s-{token}]';number=0
        while candidate in reserved or candidate in assigned:
            number+=1
            extra=hashlib.sha256((session+'\0'+str(identity)+'\0'+str(number)).encode('utf-8')).hexdigest()[:8]
            candidate=f'{name} [s-{token}-{extra}]'
        resource.set('name',candidate);assigned[candidate]=identity;renamed[identity]=candidate
    if renamed:
        for clip in root.iter('mc-clip'):
            if clip.get('ref') in renamed:clip.set('name',renamed[clip.get('ref')])


def _prune_unused_resources(root, project):
    """Retain the transitive resource closure of the finished project only.

    A selected PGM may reference a multicam which in turn references assets,
    formats or nested media. Resource declarations from unselected PGMs are
    not imports merely because another PGM from that session was selected.
    The surviving declarations and every source/record coordinate stay intact.
    """
    from collections import Counter
    resources=root.find('resources')
    if resources is None:return set(),dict(kept_count=0,removed={})
    by_id={node.get('id'):node for node in resources if node.get('id')}
    def references(node):
        return (value for child in node.iter() for key in ('ref','format') if (value:=child.get(key)))
    pending=list(references(project));used=set()
    while pending:
        identity=pending.pop()
        if identity in used:continue
        used.add(identity)
        node=by_id.get(identity)
        if node is not None:pending.extend(references(node))
    removed=Counter()
    for node in list(resources):
        if node.get('id') not in used:
            removed[node.tag]+=1;resources.remove(node)
    retained={node.get('id') for node in resources if node.get('id')}
    return retained,dict(kept_count=len(retained),removed=dict(removed))


_COMPONENTS = {'clip', 'mc-clip', 'asset-clip', 'audio', 'video', 'gap', 'ref-clip', 'title', 'caption'}
_MARKERS = {'marker', 'chapter-marker', 'rating', 'keyword', 'analysis-marker'}


def _slice(element, first, last):
    """Trim an edit in its parent's coordinate; preserve child source clocks."""
    old_offset = _time(element, 'offset', '0s')
    old_start = _time(element, 'start', '0s')
    old_end = old_offset+_time(element, 'duration')
    begin, end = max(first, old_offset), min(last, old_end)
    if end <= begin:
        return None
    if element.find('.//timeMap') is not None:
        raise ReferenceTimelineError('备选 XML 含变速，无法保持原速合并。')
    result = copy.deepcopy(element)
    new_start = old_start+begin-old_offset
    new_end = new_start+end-begin
    result.set('offset', _seconds(begin))
    result.set('start', _seconds(new_start))
    result.set('duration', _seconds(end-begin))
    for child in list(result):
        if child.tag in _COMPONENTS:
            piece = _slice(child, new_start, new_end)
            index = list(result).index(child)
            result.remove(child)
            if piece is not None:
                result.insert(index, piece)
        elif child.tag in _MARKERS and child.get('start'):
            marker_first = _time(child, 'start')
            marker_duration = _time(child, 'duration', '0s')
            marker_end = marker_first+marker_duration
            if marker_duration == 0:
                if not new_start <= marker_first < new_end:
                    result.remove(child)
            else:
                a, b = max(new_start, marker_first), min(new_end, marker_end)
                if b <= a:
                    result.remove(child)
                else:
                    child.set('start', _seconds(a))
                    child.set('duration', _seconds(b-a))
    return result


def _validate_sequence(root, seq, fps, duration=None):
    formats = {row.get('id'): row for row in root.findall('./resources/format')}
    fmt = formats.get(seq.get('format'))
    if fmt is None or _time(fmt, 'frameDuration') != Fraction(1, fps):
        raise ReferenceTimelineError('FCPXML 帧率与计划不一致。')
    if duration is not None and _time(seq, 'duration') != Fraction(duration, fps):
        raise ReferenceTimelineError('基准 XML 时长与切点计划不一致。')
    origin = _time(seq, 'tcStart', '0s')
    cursor = origin
    for row in seq.find('spine'):
        if row.tag not in ('clip', 'mc-clip', 'gap') or row.get('lane') not in (None, '0'):
            raise ReferenceTimelineError('请使用本软件生成的单轨多机位 XML 作为合并来源。')
        first, length = _time(row, 'offset'), _time(row, 'duration')
        if first != cursor or length <= 0:
            raise ReferenceTimelineError('多机位 XML 主时间线存在重叠或空隙。')
        cursor += length
    if cursor-origin != _time(seq, 'duration'):
        raise ReferenceTimelineError('FCPXML 时间线时长与镜头覆盖不一致。')
    return origin


def _route_manual_audio(resources, picture_groups, video_track_count=None):
    """Keep each video track Vn and its single-WAV sound on audio track An.

    ``lane`` on a connected audio clip makes Resolve import independent audio;
    keep the known linked component structure and use standard subroles as an
    explicit, source-independent routing plan for the Resolve import helper.
    Mono and stereo source files share a stereo timeline container without
    changing either source's channel selection or clock.  Original split L/R
    recordings must first pass the explicit stereo-WAV preparation step: two
    overlapping source items cannot safely become one timeline item by role.
    """
    assets = {row.get('id'): row for row in resources.findall('asset')}
    kinds, rows = {}, []
    def leaves(node):
        for child in node:
            if child.get('lane') and _number(child.get('lane'), '片段轨道') > 0:
                continue  # Another performance owns its own sound.
            asset = assets.get(child.get('ref'))
            if child.tag in ('audio', 'asset-clip') and asset is not None and asset.get('hasAudio') == '1':
                metadata = {item.get('key'): item.get('value') for item in asset.findall('./metadata/md')}
                pair_role = metadata.get('com.pgm.audioPairChannel')
                paired = bool(metadata.get('com.pgm.audioPairID') and pair_role in ('L', 'R'))
                channels = [part.strip() for part in child.get('srcCh', '').split(',') if part.strip()]
                if not channels:
                    channels = list(range(int(asset.get('audioChannels', '1'))))
                category = 'pair' if paired else 'stereo' if len(channels) > 1 else 'mono'
                yield child, category, pair_role
            elif child.tag in ('clip', 'gap'):
                yield from leaves(child)
    for video_track, host in picture_groups:
        for node, category, pair_role in leaves(host):
            kinds.setdefault(video_track, set()).add(category)
            rows.append((video_track, node, category, pair_role))
    if any(category == 'pair' for _track, _node, category, _role in rows):
        raise ReferenceTimelineError('左右两个原始 WAV 尚未合并为立体声 WAV，不能安全放入一条对应声音轨道。请先完成立体声准备后重新导出。')
    count = max([1, int(video_track_count or 0)] + [value[0] for value in picture_groups])
    tracks = []
    for video_track in range(1, count + 1):
        categories = kinds.get(video_track, set())
        name = ('基准' if video_track == 1 else '备选') + f' V{video_track} / A{video_track}'
        tracks.append(dict(index=video_track, video_track=video_track,
            subtype='stereo', name=name, source_formats=sorted(categories), empty=not bool(categories)))
    for video_track, node, category, pair_role in rows:
        node.set('audioRole' if node.tag == 'asset-clip' else 'role', f'dialogue.pgm-track-{video_track}')
    return tracks


def _silence_pgm_fallback_groups(picture_groups):
    """A PGM picture replacement is deliberately silent, even with a WAV map."""
    removed=0
    def remove_sound(parent):
        nonlocal removed
        for child in list(parent):
            if int(child.get('lane','0'))>0:continue  # A separate performance.
            if child.tag in ('audio','asset-clip') and (child.tag=='audio' or child.get('srcEnable')=='audio'):
                parent.remove(child);removed+=1
            elif child.tag=='clip' and int(child.get('lane','0'))<0:
                remove_sound(child)
                if not any(node.tag in _COMPONENTS for node in child):parent.remove(child)
    for _track,host in picture_groups:
        picture=host if host.tag=='mc-clip' else host.find('mc-clip')
        if picture is not None and (picture.find('./metadata/md[@key="com.pgm.videoSourceKind"][@value="pgm_fallback"]') is not None
                or picture.find('./marker[@value="PGM_VIDEO_FALLBACK"]') is not None):
            remove_sound(host)
    return removed


def _set_metadata(node, key, value):
    metadata = node.find('metadata')
    if metadata is None:
        metadata = ET.SubElement(node, 'metadata')
    ET.SubElement(metadata, 'md', key=key, value=str(value))


def _empty_camera_gap(node):
    return (node.tag == 'gap' and node.get('name') == 'NO_CAMERA_SOURCE'
            and any(child.tag == 'marker' and child.get('value') == 'NO_CAMERA_SOURCE'
                    for child in node))


def _manual_zero_clock(spine, sequence, project, origin):
    """Standard zero-based interchange; the companion restores the record clock.

    Resolve treats primary and connected spine offsets differently when tcStart
    is nonzero. A common zero origin avoids that importer ambiguity, while flat
    original-audio asset clips avoid paired-mono source-offset double counting.
    Explicit group IDs retain the original picture/audio edit ownership.
    """
    replacement = []
    for number, host in enumerate(list(spine)):
        picture = host.find('mc-clip') if host.tag == 'clip' else host if host.tag == 'mc-clip' else None
        empty_camera = _empty_camera_gap(host)
        if picture is None and not empty_camera:
            item = copy.deepcopy(host)
            item.set('offset', _seconds(_time(item, 'offset', '0s') - origin))
            replacement.append(item)
            continue
        group = f'manual-av-{number}'
        item = copy.deepcopy(host if empty_camera else picture)
        picture_position = _time(host, 'offset')
        if picture is not None and host is not picture:
            picture_position += _time(picture, 'offset', '0s') - _time(host, 'start', '0s')
        item.set('offset', _seconds(picture_position - origin))
        if host.get('lane'):
            item.set('lane', host.get('lane'))
        _set_metadata(item, 'com.pgm.linkGroupID', group)
        if empty_camera:
            # An intentional empty picture still owns its original sound.
            # Export those audio leaves as flat, routed clips just like the
            # normal linked-WAV path, retaining exact rational source samples.
            for child in list(item):
                if child.tag in ('audio', 'asset-clip') or (child.tag == 'clip' and int(child.get('lane', '0')) < 0):
                    item.remove(child)
        elif host is not picture:
            for marker in host:
                if marker.tag in _MARKERS:
                    item.append(copy.deepcopy(marker))
        replacement.append(item)
        if host is picture:
            continue
        first, last = _time(host, 'offset'), _time(host, 'offset') + _time(host, 'duration')
        def audios(container, absolute):
            for child in container:
                if child.tag in ('audio', 'asset-clip'):
                    position = absolute + _time(child, 'offset', '0s') - _time(container, 'start', '0s')
                    yield child, position
                elif child.tag == 'clip' and int(child.get('lane', '0')) < 0:
                    position = absolute + _time(child, 'offset', '0s') - _time(container, 'start', '0s')
                    yield from audios(child, position)
        for audio, position in audios(host, first):
            begin, end = max(first, position), min(last, position + _time(audio, 'duration'))
            if end <= begin:
                continue
            role = audio.get('role') or audio.get('audioRole', '')
            if not role.startswith('dialogue.pgm-track-'):
                raise ReferenceTimelineError('手动时间线缺少明确的原始声音轨道。')
            track = int(role.rsplit('-', 1)[-1])
            leaf = ET.Element('asset-clip', ref=audio.get('ref'), name=audio.get('name', ''),
                offset=_seconds(begin - origin), start=_seconds(_time(audio, 'start') + begin - position),
                duration=_seconds(end - begin), lane=str(-track), srcEnable='audio', audioRole=role)
            for adjustment in audio:
                if adjustment.tag.startswith('adjust-'):
                    leaf.append(copy.deepcopy(adjustment))
            _set_metadata(leaf, 'com.pgm.linkGroupID', group)
            replacement.append(leaf)
    spine.clear()
    spine.extend(replacement)
    sequence.set('tcStart', '0s')
    _set_metadata(project, 'com.pgm.requestedTimelineStart', _seconds(origin))


def assemble_reference_xml(base_xml, session_xmls, plan, output, title=None, include_baseline=True):
    """Merge generated session multicams, retaining every asset's source identity.

    Alternatives are connected above the unmodified reference primary storyline.
    Each original multicam/WAV edit is intersected with its whole selected PGM
    file range, including material before and after its first dialogue. Return an accompanying manifest containing exact rational source and
    destination ranges; no sound renders, retimes, or media copies are made.
    """
    fps = _frames(plan.get('fps'), '计划帧率')
    if fps not in (24, 25, 30):
        raise ReferenceTimelineError('基准合并仅支持 24/25/30fps。')
    base, sequence = _xml(base_xml)
    baseline_origin = _validate_sequence(base, sequence, fps, plan['baseline_duration_frames'])
    _prefix(base, 'base-')
    resources = base.find('resources')
    resource_sessions={row.get('id'):plan['baseline_session_id'] for row in resources}
    spine = sequence.find('spine')
    baseline_hosts = list(spine)
    duration = _frames(plan.get('duration_frames'), '计划总时长')
    baseline_duration = _frames(plan.get('baseline_duration_frames'), '基准时长')
    if duration < baseline_duration:
        raise ReferenceTimelineError('合并不能缩短基准通条。')
    start_frame = _frames(plan.get('timeline_start_frame', 0), '计划起始位置')
    end_frame = start_frame+duration
    if start_frame > 0 or end_frame < baseline_duration:
        raise ReferenceTimelineError('合并时间线必须完整包含基准范围。')
    timeline_origin = baseline_origin+Fraction(start_frame, fps)
    if timeline_origin < 0:
        raise ReferenceTimelineError('完整 PGM 的片头将超出 00:00:00:00，请提高基准时间线起始时码后重新导出。')
    sequence.set('tcStart', _seconds(timeline_origin))
    if include_baseline and start_frame < 0:
        spine.insert(0, ET.Element('gap', name='备选片头', offset=_seconds(timeline_origin),
                     start='0s', duration=_seconds(Fraction(-start_frame, fps))))
    if not include_baseline:
        # Keep the reference clock and offsets without referencing its picture or sound.
        # All alternatives connect to this silent primary gap in the same coordinates.
        spine.clear()
        ET.SubElement(spine, 'gap', name='基准位置', offset=_seconds(timeline_origin),
                      start='0s', duration=_seconds(Fraction(duration, fps)))
    # This tail is a gap, never an extension or freeze frame of the last picture.
    if include_baseline and end_frame > baseline_duration:
        ET.SubElement(spine, 'gap', name='备选尾部', offset=_seconds(baseline_origin+Fraction(baseline_duration, fps)),
                      start='0s', duration=_seconds(Fraction(end_frame-baseline_duration, fps)))
    originals = list(spine)
    sessions, source_manifest, pieces_manifest, piece_nodes, lane_intervals = {}, [], [], [], {}
    # Reserve the same baseline audio slots in alternatives-only exports.
    picture_groups = [(1, host) for host in baseline_hosts]
    for selection in plan.get('selections', []):
        session_id = selection['session_id']
        if session_id == plan['baseline_session_id']:
            raise ReferenceTimelineError('备选不能重复引用基准场次。')
        if session_id not in sessions:
            if session_id not in session_xmls:
                raise ReferenceTimelineError('缺少备选场次多机位 XML：'+str(session_id))
            root, seq = _xml(session_xmls[session_id])
            origin = _validate_sequence(root, seq, fps)
            prefix = f'alt{len(sessions)+1}-'
            _prefix(root, prefix)
            for resource in list(root.find('resources')):
                resources.append(resource)
                resource_sessions[resource.get('id')]=session_id
            sessions[session_id] = (root, seq, origin)
        root, seq, source_origin = sessions[session_id]
        first = source_origin+Fraction(selection['record_frame_in'], fps)
        last = source_origin+Fraction(selection['record_frame_out_exclusive'], fps)
        destination = baseline_origin+Fraction(selection['target_frame_in'], fps)
        selected_duration = last-first
        if selected_duration <= 0 or selected_duration != Fraction(selection['duration_frames'], fps):
            raise ReferenceTimelineError('备选计划的源范围与时长不一致。')
        if selection.get('target_frame_out_exclusive') != selection['target_frame_in']+selection['duration_frames']:
            raise ReferenceTimelineError('备选计划的目标范围与时长不一致。')
        if destination < timeline_origin or destination+selected_duration > baseline_origin+Fraction(end_frame, fps):
            raise ReferenceTimelineError('备选目标范围超出合并时间线。')
        lane = _frames(selection.get('lane'), '备选轨道')
        if lane <= 0:
            raise ReferenceTimelineError('备选轨道必须位于基准 V1 上方。')
        intervals = lane_intervals.setdefault(lane, [])
        if any(destination < end and destination+selected_duration > begin for begin, end in intervals):
            raise ReferenceTimelineError('同一备选轨道上的片段重叠，请重新生成选择计划。')
        intervals.append((destination, destination+selected_duration))
        covered = Fraction(0)
        for original in seq.find('spine'):
            piece = _slice(original, first, last)
            if piece is None:
                continue
            if piece.tag == 'gap' and not _empty_camera_gap(piece):
                raise ReferenceTimelineError('所选备选范围包含未识别机位，请先完成机位核对。')
            piece_position = destination+_time(piece, 'offset')-first
            host = next((row for row in originals
                         if _time(row, 'offset') <= piece_position < _time(row, 'offset')+_time(row, 'duration')), None)
            if host is None:
                raise ReferenceTimelineError('备选片段没有可连接的基准位置。')
            position_in_host = _time(host, 'start', '0s')+piece_position-_time(host, 'offset')
            piece.set('lane', str(lane))
            if plan.get('manual_layout'):
                # Resolve imports a baseline wrapper containing connected
                # performances as a compound, hiding its multicam and WAVs.
                # Flat sibling edits retain their own native multicam identity.
                piece.set('offset', _seconds(piece_position))
                spine.append(piece)
            else:
                piece.set('offset', _seconds(position_in_host))
                # Keep components before markers in legacy generated exports.
                index = next((i for i, child in enumerate(host) if child.tag in _MARKERS), len(host))
                host.insert(index, piece)
            picture_groups.append((lane + 1, piece))
            length = _time(piece, 'duration')
            covered += length
            pieces_manifest.append(dict(candidate_id=selection['candidate_id'], session_id=session_id,
                                        script_line_id=selection.get('script_line_id'), lane=lane,
                                        target_start=_seconds(piece_position-baseline_origin),
                                        duration=_seconds(length), source_start=piece.get('start'),
                                        source_refs=sorted({node.get('ref') for node in piece.iter() if node.get('ref')})))
            piece_nodes.append(piece)
        if covered != selected_duration:
            raise ReferenceTimelineError('备选 XML 未完整覆盖所选对白范围，未输出不完整结果。')
    sequence.set('duration', _seconds(Fraction(duration, fps)))
    if title:
        base.find('./library/event/project').set('name', str(title))
    # Old scene exports can still contain explicit PGM sound or an embedded
    # camera audio choice. Strip those before routing the surviving WAVs;
    # source/record coordinates and the project's cached analysis stay intact.
    from fcpxml_conform import strip_camera_reference_audio
    from proxy_workflow import file_path
    assets_by_id={asset.get('id'):asset for asset in resources.findall('asset')}
    camera_refs={node.get('ref') for media in resources.findall('media')
                 for angle in media.findall('./multicam/mc-angle') for node in angle.iter('video')}
    camera_paths=[]
    for identity in camera_refs:
        asset=assets_by_id.get(identity)
        if asset is not None:
            source=asset.get('src') or next((row.get('src') for row in asset.findall('media-rep')
                                            if row.get('kind')=='original-media'),'')
            path=file_path(source)
            if path:camera_paths.append(path)
    audio_policy=strip_camera_reference_audio(base,proxy_paths=camera_paths)
    audio_policy['removed_pgm_fallback_wavs']=_silence_pgm_fallback_groups(picture_groups)
    audio_tracks = _route_manual_audio(resources, picture_groups,
        video_track_count=plan.get('lane_count', 0) + 1) if plan.get('manual_layout') else []
    if plan.get('manual_layout') and plan.get('resolve_zero_origin', True):
        _manual_zero_clock(spine, sequence, base.find('./library/event/project'), timeline_origin)
    retained,resource_cleanup=_prune_unused_resources(base,base.find('./library/event/project'))
    for row,piece in zip(pieces_manifest,piece_nodes):
        # Historical PGM sound may have been removed and manual zero-clock
        # output flattened its wrapper. Do not retain obsolete sidecar refs.
        row['source_refs']=sorted({node.get('ref') for node in piece.iter() if node.get('ref') in retained})
    _unique_multicam_names(base,resource_sessions)
    for asset in resources.findall('asset'):
        metadata = {row.get('key'): row.get('value') for row in asset.findall('./metadata/md')}
        source_manifest.append(dict(resource_id=asset.get('id'), name=asset.get('name'), src=asset.get('src'),
                                    media_reps=[dict(row.attrib) for row in asset.findall('media-rep')],
                                    source_id=metadata.get('com.pgm.sourceID', ''),
                                    session_id=metadata.get('com.pgm.sessionID', ''),
                                    production_id=metadata.get('com.pgm.productionID', ''), metadata=metadata))
    output = Path(output)
    manifest_path = output.with_suffix('.reference.json')
    export_mode = 'combined' if include_baseline else 'alternatives_only'
    if plan.get('manual_layout') and include_baseline and not plan.get('selections'):
        export_mode = 'baseline_only'
    manifest = dict(plan, xml_file=output.name, assets=source_manifest, pieces=pieces_manifest,
                    audio_tracks=audio_tracks, audio_policy=dict(original_wav_only=True,**audio_policy),
                    resource_cleanup=resource_cleanup,
                    include_baseline=include_baseline, export_mode=export_mode)
    ET.indent(base, space='  ')
    payload = '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'+ET.tostring(base, encoding='unicode')+'\n'
    output.write_text(payload, encoding='utf-8')
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    from resolve_import_helper import write_resolve_import_helper
    write_resolve_import_helper(output, audio_layout=audio_tracks)
    return dict(xml_path=str(output), manifest_path=str(manifest_path), manifest=manifest,
                selected_count=len(plan.get('selections', [])), piece_count=len(pieces_manifest),
                lane_count=plan.get('lane_count', 0))
