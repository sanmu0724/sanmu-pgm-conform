"""PGM camera-label OCR and source timecode reconform."""
from __future__ import annotations

import csv
import hashlib
import json
import re
from string import ascii_uppercase
from dataclasses import dataclass, asdict
from fractions import Fraction
from pathlib import Path
from bisect import bisect_right
from collections import defaultdict

import av
import numpy as np

from camera_schema import DEFAULT_CAMERAS, cameras_for

CAMERAS = DEFAULT_CAMERAS  # Compatibility for older importers, not session validation.


class AnalysisError(ValueError):
    pass


def tc_frames(tc, fps):
    if not isinstance(tc, str) or not re.fullmatch(r'\d{2}:\d{2}:\d{2}:\d{2}', tc):
        raise AnalysisError('时码必须为 HH:MM:SS:FF（整数帧率、非丢帧）。')
    h, m, s, f = map(int, tc.split(':'))
    if h > 23 or m > 59 or s > 59 or f >= fps:
        raise AnalysisError(f'无效时码：{tc} / {fps} fps')
    return ((h * 60 + m) * 60 + s) * fps + f


def frames_tc(n, fps):
    if not 0 <= n < 24 * 3600 * fps:
        raise AnalysisError('当前版本不支持负时码或跨午夜，请分场次处理。')
    sec, f = divmod(int(n), fps)
    h, sec = divmod(sec, 3600)
    m, s = divmod(sec, 60)
    return f'{h:02}:{m:02}:{s:02}:{f:02}'


def probe(path):
    path = str(Path(path).expanduser().resolve())
    from media_cache import file_identity, video_metadata_cache
    try:
        identity = file_identity(path)
    except OSError as exc:
        raise AnalysisError(f'找不到文件或无法读取文件信息：{path}') from exc
    def load():
        result = _probe_uncached(path)
        try:
            unchanged = file_identity(path) == identity
        except OSError:
            unchanged = False
        if not unchanged:
            raise AnalysisError(f'素材文件在读取过程中发生变化，请待复制完成后重试：{path}')
        return result
    return video_metadata_cache.get(identity, load)


def _probe_uncached(path):
    try:
        with av.open(path) as container:
            if not container.streams.video:
                raise AnalysisError(f'没有视频轨：{path}')
            stream = container.streams.video[0]
            rate = stream.average_rate or stream.base_rate
            if not rate:
                raise AnalysisError(f'无法读取帧率：{path}')
            frames = stream.frames
            if not frames and stream.duration is not None:
                frames = round(stream.duration * stream.time_base * rate)
            if not frames:
                raise AnalysisError(f'无法确定视频长度：{path}；请使用有完整长度信息的文件。')
            values = [stream.metadata.get('timecode'), container.metadata.get('timecode')]
            values += [s.metadata.get('timecode') for s in container.streams]
            tc = next((v for v in values if v), '')
            audio_channels = []
            for audio in container.streams.audio:
                codec = audio.codec_context
                count = codec.channels
                depth_match = re.match(r'pcm_[suf](\d+)',codec.name or '')
                depth = int(depth_match.group(1)) if depth_match else 16
                group = len(audio_channels)+1 if count==2 else 0
                for channel in range(count):
                    audio_channels.append(dict(source_channel=len(audio_channels)+1,
                        sample_rate=codec.sample_rate or 48000,depth=depth,
                        pan=(-1 if channel==0 else 1) if count==2 else 0,stereo_group=group))
            return dict(path=path, fps=str(rate), frames=int(frames), start_tc=tc,
                        width=stream.width, height=stream.height,audio_channels=audio_channels)
    except av.FFmpegError as exc:
        raise AnalysisError(f'视频读取失败：{path}\n{exc}') from exc


def read_frame(path, index=0):
    from media_cache import current_frame_session, FrameReadSession
    session = current_frame_session()
    if session is not None:
        return session.read(path, index)
    # Standalone calls retain their old resource lifetime. Workers/calibration
    # batches use frame_read_session to reuse GOP decoding across exact samples.
    with FrameReadSession(max_open=1, max_cache_bytes=0) as session:
        return session.read(path, index)


def validate_rate(info, fps):
    if Fraction(info['fps']) != fps:
        raise AnalysisError(f"帧率不一致：{Path(info['path']).name} 是 {info['fps']}，项目是 {fps}。本版不支持变速或隔行。")


def roi_pixels(roi, w, h):
    x, y, rw, rh = map(float, roi)
    if not (0 <= x < 1 and 0 <= y < 1 and rw > 0 and rh > 0
            and x + rw <= 1.000001 and y + rh <= 1.000001):
        raise AnalysisError('窗口框必须在画面内且面积大于零。')
    left, top = round(x*w), round(y*h)
    right, bottom = min(w, round((x+rw)*w)), min(h, round((y+rh)*h))
    if right-left < 12 or bottom-top < 12:
        raise AnalysisError('所选区域太小，请框选完整标号或机位窗口（至少12×12像素）。')
    return left, top, right, bottom






@dataclass
class Segment:
    start: int
    end: int
    camera: str
    reason: str


def analyze(config, output_dir, progress=lambda n,message:None, cancelled=lambda:False):
    if config.get('mode') != 'cut_xml':
        raise AnalysisError('请导入达芬奇切点XML；旧版全片逐帧识别已移除。')
    from cut_xml import analyze_cut_xml
    return analyze_cut_xml(config, output_dir, progress, cancelled)


def build_sources(config):
    fps = int(config['fps'])
    cameras = cameras_for(config)
    sources = []
    for item in config.get('sources', []):
        if item['camera'] not in cameras:
            raise AnalysisError('代理机位必须属于本场机位：'+'/'.join(cameras)+'。')
        info = probe(item.get('proxy_path') or item['path'])
        validate_rate(info, fps)
        tc = item.get('start_tc') or info['start_tc']
        if not tc:
            raise AnalysisError(f"代理没有可读取的时码：{Path(info['path']).name}。请从达芬奇核对后填写起始时码，不能猜测。")
        start = tc_frames(tc, fps)
        count = int(item.get('frames') or info['frames'])
        if count <= 0:
            raise AnalysisError('代理帧数必须大于零。')
        if count != info['frames']: raise AnalysisError('代理帧数填写值与实际文件不一致。')
        frames_tc(start+count, fps)
        original = {}
        if item.get('original_path'):
            raw = probe(item['original_path']); validate_rate(raw,fps)
            raw_tc = item.get('original_start_tc') or raw['start_tc']
            if not raw_tc: raise AnalysisError('原片没有可读取的时码，请核对后填写原片起始时码。')
            raw_start = tc_frames(raw_tc,fps)
            if raw_start != start or raw['frames'] != count:
                raise AnalysisError(f"代理与原片必须逐文件保持相同时码、帧率和总帧数：{Path(info['path']).name}")
            original = dict(original_path=raw['path'],original_start=raw_start,original_width=raw['width'],original_height=raw['height'])
        # Display names include Chinese recording types. The interchange reel
        # remains a short stable identifier, independent of names or NAS paths.
        label = str(config.get('show_id','S01'))
        fallback = label if re.fullmatch(r'[A-Za-z0-9_]{1,7}', label) else (
            'R' + hashlib.sha256(str(config.get('library_context',{}).get('session_id') or label).encode()).hexdigest()[:6])
        reel = config.get('reels', {}).get(item['camera']) or (fallback + item['camera'])
        if not re.fullmatch(r'[A-Za-z0-9_]{1,8}', reel):
            raise AnalysisError('素材卷名须为1～8位英文字母、数字或下划线。')
        sources.append(dict(camera=item['camera'], path=info['path'],
                            start=start, end=start+count, frames=count,
                            reel=reel, width=info['width'], height=info['height'],
                            audio_channels=info.get('audio_channels',[]),media_role='proxy',
                            source_id=item.get('source_id',''),
                            session_id=config.get('library_context',{}).get('session_id',''),
                            production_id=config.get('library_context',{}).get('production_id',''),**original))
    if not sources:
        raise AnalysisError('请添加本场代理文件，建立机位与代理时码对应关系。')
    return sources


def validate_segments(segments, duration):
    cursor = 0
    for s in segments:
        if s.start != cursor or s.end <= s.start or s.end > duration:
            raise AnalysisError('区间必须从第0帧开始、连续且不重叠，出点大于入点，最后覆盖完整 PGM。')
        if not isinstance(s.camera,str) or len(s.camera)!=1 or s.camera not in ascii_uppercase+'?':
            raise AnalysisError('机位只能为 A～Z 或 ?。')
        cursor = s.end
    if cursor != duration:
        raise AnalysisError('区间没有覆盖完整 PGM。')


def _conform_available_events(config, segments, sources, cut, cameras, record_tc):
    """Index source coverage once; never search sources for every video frame.

    Coverage coordinates use the PGM source clock. Each camera's offset is
    applied to its boundaries and back to its selected source IN/OUT, without
    changing the original media or the full PGM/record coordinates.
    """
    fps=int(config['fps'])
    offsets={camera:int(config.get('camera_offsets',{}).get(camera,0)) for camera in cameras}
    changes={camera:defaultdict(lambda:[[],[]]) for camera in cameras}
    boundaries=set()
    for number,source in enumerate(sources):
        camera=source['camera'];offset=offsets[camera]
        first,last=source['start']-offset,source['end']-offset
        changes[camera][first][1].append(number)
        changes[camera][last][0].append(number)
        boundaries.update((first,last))
    indices={}
    for camera in cameras:
        active=set();points=[];states=[]
        for position,(ending,starting) in sorted(changes[camera].items()):
            active.difference_update(ending);active.update(starting)
            points.append(position)
            states.append((len(active),next(iter(active)) if len(active)==1 else None))
        indices[camera]=(points,states)
    boundaries=sorted(boundaries)
    preference=config.get('missing_camera_preference','A')
    blocks=cut['pgm_sources'];block_starts=[block['start'] for block in blocks]
    block_clocks=[tc_frames(block['start_tc'],fps) for block in blocks]
    events=[]
    for segment in segments:
        position=segment.start
        while position<segment.end:
            block_number=bisect_right(block_starts,position)-1
            block=blocks[block_number];local=position-block['start']
            clock=block_clocks[block_number]+local
            current={}
            for camera,(points,states) in indices.items():
                index=bisect_right(points,clock)-1
                current[camera]=states[index] if index>=0 else (0,None)
            available=[camera for camera in cameras if current[camera][0]==1]
            length=min(segment.end-position,block['end']-position)
            following=bisect_right(boundaries,clock)
            if following<len(boundaries):length=min(length,boundaries[following]-clock)
            camera=segment.camera;reason=segment.reason;source=None;fallback=False
            if camera!='?' and reason!='identity_unconfirmed':
                count,identity=current[camera]
                if count==1:source=sources[identity]
                elif count>1:reason='source_overlap'
                else:
                    reason='source_gap'
                    if available:
                        camera=preference if preference in available else available[0]
                        source=sources[current[camera][1]]
                        reason='camera_unavailable_fallback';fallback=True
            source_in=clock+offsets[camera] if source is not None else 0
            event=dict(start=position,end=position+length,camera=camera,reason=reason,
                pgm_path=block['path'],pgm_in=local,reel=source['reel'] if source else 'BL',
                path=source['path'] if source else '',source_in=source_in,
                source_out=source_in+length if source else length,
                record_in=record_tc+position,record_out=record_tc+position+length,
                available_cameras=available)
            if fallback:event.update(requested_camera=segment.camera,fallback_reason='source_gap')
            elif (segment.reason!='identity_unconfirmed'
                    and all(state[0]==0 for state in current.values())):
                if config.get('empty_camera_policy','pgm')=='pgm':
                    event.update(reason='pgm_video_fallback',path=block['path'],reel='PGM_REF',
                        source_in=clock,source_out=clock+length,pgm_video_fallback=True,
                        video_source_kind='pgm_fallback',selected_angle='PGM_REF')
                elif config.get('empty_camera_policy')=='gap':
                    event['reason']='source_gap'
                    event['empty_camera_gap']=True
            events.append(event);position+=length
    return events


def conform_events(config, segments, sources):
    fps = int(config['fps'])
    from cut_xml import timeline_info,locate_frame
    cut = timeline_info(config)
    validate_segments(segments, cut['duration'])
    cameras = cameras_for(config)
    if any(segment.camera != '?' and segment.camera not in cameras for segment in segments):
        raise AnalysisError('识别结果包含本场未设置的机位，请核对机位数量。')
    if any(source.get('camera') not in cameras for source in sources):
        raise AnalysisError('代理素材包含本场未设置的机位，请核对机位数量。')
    record_tc = tc_frames(config.get('timeline_start_tc', '01:00:00:00'), fps)
    frames_tc(record_tc+cut['duration'], fps)
    if config.get('missing_camera_policy')=='available':
        return _conform_available_events(config,segments,sources,cut,cameras,record_tc)
    events = []
    for s in segments:
        p = s.start
        while p < s.end:
            block,pgm_in = locate_frame(cut,p)
            src = tc_frames(block['start_tc'],fps)+pgm_in+int(config.get('camera_offsets', {}).get(s.camera, 0))
            candidates = [c for c in sources if c['camera'] == s.camera and c['start'] <= src < c['end']]
            # Split at every file coverage change, including overlaps and gaps.
            boundaries = [s.end-p,block['end']-p]
            for c in sources:
                if c['camera'] == s.camera:
                    boundaries += [b-src for b in (c['start'], c['end']) if b > src]
            length = min(boundaries)
            source = candidates[0] if len(candidates) == 1 else None
            reason = s.reason
            if s.camera == '?':
                reason = s.reason
            elif not candidates:
                reason = 'source_gap'
            elif len(candidates) > 1:
                reason = 'source_overlap'
            event = dict(start=p, end=p+length, camera=s.camera, reason=reason,pgm_path=block['path'],pgm_in=pgm_in,
                         reel=source['reel'] if source else 'BL',
                         path=source['path'] if source else '',
                         source_in=src if source else 0,
                         source_out=src+length if source else length,
                         record_in=record_tc+p, record_out=record_tc+p+length)
            events.append(event)
            p += length
    return events


def export(config, segments, output_dir, progress=lambda n, message: None, cancelled=lambda: False):
    config = dict(config)
    fps = int(config['fps'])
    sources = build_sources(config)
    events = conform_events(config, segments, sources)
    from cut_xml import timeline_info
    cut=timeline_info(config)
    sound=None
    if config.get('external_sound'):
        if config.get('library_sound_cache_only'):
            from registered_sound import registered_sound_plan
            sound=registered_sound_plan(config,cut,progress,cancelled)
        else:
            from sound_sync import sound_plan
            sound=sound_plan(config,cut,progress,cancelled)
    from sound_fallback import wav_only_sound_plan
    sound=wav_only_sound_plan(sound,cut,fps)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    config['stereo_audio_cache_dir'] = str(Path(config.get('stereo_audio_cache_dir') or out/'配对立体声WAV').expanduser().resolve())
    from stereo_merge import merge_sound_plan
    sound = merge_sound_plan(sound, config['stereo_audio_cache_dir'],
                             fps=fps, cancelled=cancelled, progress=progress)
    if cancelled(): raise AnalysisError('已取消导出。')
    intentional_gaps = [e for e in events if e.get('empty_camera_gap') is True]
    pgm_video_fallbacks=[e for e in events if e.get('pgm_video_fallback') is True]
    unresolved = [e for e in events if e['reel'] == 'BL' and not e.get('empty_camera_gap')]
    review_a = []
    automatic_a = [e for e in events if e['reason'] == 'A_inferred_blank']
    fallbacks = [e for e in events if e.get('fallback_reason')=='source_gap' and e.get('path')]
    is_draft = unresolved or not config.get('calibration_confirmed')
    title = 'PGM_'+config.get('show_id','S01')+'_PROXY'+('_DRAFT' if is_draft else '_CONFORM')
    fields = ['start', 'end', 'camera', 'reason', 'reel', 'path', 'source_in', 'source_out', 'record_in', 'record_out', 'pgm_path', 'pgm_in']
    fields += [key for key in ('available_cameras','requested_camera','fallback_reason','empty_camera_gap','pgm_video_fallback','video_source_kind','selected_angle') if any(key in event for event in events)]
    with (out/'events.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for e in events:
            row = dict(e)
            if 'available_cameras' in row:row['available_cameras']=json.dumps(row['available_cameras'],ensure_ascii=False)
            for k in ('source_in', 'source_out', 'record_in', 'record_out'):
                row[k] = frames_tc(row[k], fps)
            writer.writerow(row)
    (out/'sources.json').write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding='utf-8')
    fallback_note=('已确认机位的代理缺口按可用机位策略回退并标记，fallbacks单列这些已替换区间；'
                   'intentional_gaps单列按明确设置保留的零可用机位空隙，保留时长并标记；'
                   'pgm_video_fallbacks单列全部机位无代理时引用的PGM画面，该区间无声音；unresolved仅列仍需核对的区间。'
                   if config.get('missing_camera_policy')=='available' else '')
    (out/'review.json').write_text(json.dumps(dict(unresolved=unresolved, inferred_a=review_a, automatic_a=automatic_a,
        fallbacks=fallbacks,intentional_gaps=intentional_gaps,pgm_video_fallbacks=pgm_video_fallbacks,
        note=('A无标号模式：其他机位的有效标号区域均无文字时自动判为A，无法识别的文字仍待核对。' if config.get('infer_a_when_blank',False) else '所有机位均按标号识别，无标号为未知。')+'XML按本场机位设置输出代理。'+(
            '可用机位回退、PGM参考画面与明确空隙见下方分类；有代理时的未知机位、未确认识别或重叠仍需核对。'
            if config.get('missing_camera_policy')=='available' else '未知机位、代理缺口或重叠在原时间位置留空并标记，不使用PGM占位。'),
        fallback_note=fallback_note,
        calibration=config.get('camera_offsets', {})),
        ensure_ascii=False, indent=2), encoding='utf-8')
    from proxy_workflow import export_mapping
    context=config.get('library_context') or {}
    video_fallbacks=[dict(path=e.get('pgm_path') or e['path'],session_id=context.get('session_id',''),
        production_id=context.get('production_id',''),source_kind='pgm_fallback')
        for e in events if e.get('pgm_video_fallback')]
    export_mapping(sources,out,fps,config=config,sound=sound,video_fallbacks=video_fallbacks)
    if sound is not None:
        from sound_fallback import sound_reports, source_start
        wavs,sound_events=sound
        # Keep all original identity fields outside application-specific XML,
        # so the sound department can still inspect them after an AAF render.
        manifest=[{k:v for k,v in w.items() if k!='fmt'} for w in wavs]
        reports=sound_reports(wavs)
        gaps=[r for r in reports if r.get('status')=='uncovered']
        (out/'sound_sync_report.json').write_text(json.dumps(dict(version=1,reports=reports,gaps=gaps),ensure_ascii=False,indent=2),encoding='utf-8')
        (out/'sound_sources.json').write_text(json.dumps(dict(version=1,sources=manifest,
            note='仅引用已合板原始 WAV；未覆盖区间音频留空，不引用PGM或代理内嵌原声。缺失卷号不推测。XML自定义字段不保证经达芬奇AAF传递。'),ensure_ascii=False,indent=2),encoding='utf-8')
        sound_fields=['pgm_path','path','source_kind','method','offset_seconds','score','record_start_seconds',
                      'duration_seconds','source_start_seconds','source_local_seconds','original_filename','sound_roll','recording_date','recording_time']
        by_path={w['path']:w for w in wavs}
        with (out/'sound_sync.csv').open('w',newline='',encoding='utf-8-sig') as f:
            writer=csv.DictWriter(f,fieldnames=sound_fields);writer.writeheader()
            for e in sound_events:
                w=by_path[e['path']]
                writer.writerow(dict(pgm_path=e.get('pgm_path',''),path=e['path'],source_kind=e.get('source_kind',w.get('source_kind','wav')),method=e.get('method',''),
                    offset_seconds=e.get('offset_seconds',''),score=e.get('score',''),
                    record_start_seconds=float(e['record_start']),duration_seconds=float(e['duration']),
                    source_start_seconds=float(e['source_start']),source_local_seconds=float(e['source_start']-source_start(w)),
                    original_filename=w.get('original_audio_filename',Path(w['path']).name),sound_roll=w.get('sound_roll',''),
                    recording_date=w.get('recording_date',''),recording_time=w.get('recording_time','')))
    if config.get('multicam') or sound[1] or pgm_video_fallbacks:
        from multicam_xml import write_multicam_xml
        target=out/f'{title}_MULTICAM.fcpxml'
        write_multicam_xml(config,events,sources,target,title,sound=sound)
    else:
        from timeline_xml import write_xml
        target=out/f'{title}.xml'
        write_xml(config,events,sources,target,title,sound=sound)
    if config.get('library_context'):
        from library_export import export_library_manifest
        export_library_manifest(config,events,sources,target,sound)
    return events,target


def main():
    import argparse
    parser = argparse.ArgumentParser(description='PGM机位号分析 → 代理剪辑XML（硬切）')
    parser.add_argument('config', help='项目 JSON')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding='utf-8'))
    config['multicam']=True
    config['external_sound']=True
    try:
        segments = analyze(config, args.output, lambda n, msg: print(f'{n}% {msg}', flush=True))
        events, files = export(config, segments, args.output)
        print('XML:', files)
        print('待核查黑场区间:', sum(e['reel'] == 'BL' for e in events))
    except AnalysisError as exc:
        parser.exit(2, f'错误：{exc}\n')


if __name__ == '__main__':
    main()
