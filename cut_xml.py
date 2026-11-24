"""Hard-cut FCP7 timelines with one or more full PGM recordings."""
from pathlib import Path
import copy
import csv, hashlib, json
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict
from core import AnalysisError, Segment, probe, validate_rate, roi_pixels, tc_frames, read_frame, frames_tc
from proxy_workflow import file_path
from media_cache import frame_read_session


def read_cut_xml(path, pgm_override='', pgm_overrides=None):
    path=Path(path).expanduser().resolve()
    try:
        raw=path.read_bytes();root=ET.fromstring(raw)
    except (OSError,ET.ParseError) as exc: raise AnalysisError(f'无法读取切点XML：{exc}') from exc
    if root.tag!='xmeml': raise AnalysisError('请选择达芬奇导出的 Final Cut Pro 7 XML，不支持FCPXML或DRT。')
    sequences=root.findall('./sequence')
    if len(sequences)!=1: raise AnalysisError('XML必须只包含一条时间线。')
    seq=sequences[0]
    def rate(node):
        r=node.find('rate')
        if r is None or r.findtext('ntsc','FALSE').upper()!='FALSE': raise AnalysisError('仅支持24/25/30fps非丢帧时间线。')
        try: value=int(r.findtext('timebase','0'))
        except ValueError as exc: raise AnalysisError('XML帧率字段无效。') from exc
        if value not in (24,25,30): raise AnalysisError('仅支持24/25/30fps时间线。')
        return value
    fps=rate(seq)
    tracks=[t for t in seq.findall('./media/video/track') if t.findall('clipitem')]
    if len(tracks)!=1: raise AnalysisError('切点XML必须只有一条视频轨。')
    track=tracks[0]
    if track.findall('transitionitem'): raise AnalysisError('仅支持硬切，XML中不能有转场。')
    definitions={f.get('id'):f for f in root.iter('file') if f.find('pathurl') is not None}
    shots=[];sources={};blocks=[];cursor=0
    overrides=pgm_overrides or {}
    try:
        clips=sorted(track.findall('clipitem'),key=lambda c:int(c.findtext('start')))
        for clip in clips:
            start,end,source_in,source_out=[int(clip.findtext(k)) for k in ('start','end','in','out')]
            if clip.findtext('enabled','TRUE').upper()=='FALSE': raise AnalysisError('切点时间线不能包含禁用镜头。')
            if rate(clip)!=fps: raise AnalysisError('镜头与时间线帧率不一致。')
            effects=[(e.findtext('effectid','')+' '+e.findtext('name','')).lower() for e in clip.findall('./filter/effect')]
            if any(any(k in e for k in ('timeremap','time remap','speed','retime')) for e in effects): raise AnalysisError('切点时间线不能包含变速。')
            if start!=cursor or end<=start or source_in<0 or source_out-source_in!=end-start:
                raise AnalysisError('PGM必须从首帧开始，连续完整且保持原顺序；不能删片、留空隙、重排或变速。')
            f=clip.find('file')
            if f is None: raise AnalysisError('镜头缺少源文件。')
            definition=f if f.find('pathurl') is not None else definitions.get(f.get('id'))
            if definition is None: raise AnalysisError('XML源文件引用无法解析。')
            if rate(definition)!=fps: raise AnalysisError('XML源文件帧率与时间线不一致。')
            original=file_path(definition.findtext('pathurl'))
            if not original: raise AnalysisError('XML源文件必须是本地file路径。')
            xml_tc=definition.findtext('./timecode/string','')
            if xml_tc: tc_frames(xml_tc,fps)
            if original not in sources:
                # The old single-file override remains valid only for the first source.
                replacement=overrides.get(original) or (pgm_override if not sources else '') or original
                info=probe(replacement);validate_rate(info,fps)
                if xml_tc and info['start_tc'] and tc_frames(xml_tc,fps)!=tc_frames(info['start_tc'],fps):
                    raise AnalysisError('XML中的PGM时码与视频元数据不一致，请在达芬奇核对。')
                sources[original]=dict(info,xml_path=original,start_tc=info['start_tc'] or xml_tc)
            elif xml_tc and sources[original]['start_tc'] and xml_tc!=sources[original]['start_tc']:
                raise AnalysisError('XML中PGM源文件时码定义不一致。')
            info=sources[original]
            if not blocks or blocks[-1]['xml_path']!=original:
                if any(b['xml_path']==original for b in blocks): raise AnalysisError('PGM片段不能重排或重复使用，请按演出顺序放置。')
                if source_in!=0: raise AnalysisError('每条PGM必须从首帧开始并保持原顺序。')
                blocks.append(dict(info,start=start,end=end))
            else:
                if source_in!=blocks[-1]['end']-blocks[-1]['start']: raise AnalysisError('PGM镜头必须保持原顺序，不能删片或重复。')
                blocks[-1]['end']=end
            if source_out>info['frames']: raise AnalysisError('PGM镜头入出点超出文件长度。')
            shots.append(dict(start=start,end=end,pgm_path=info['path'],source_in=source_in,source_out=source_out));cursor=end
        if not shots: raise AnalysisError('XML没有PGM镜头。')
        declared_duration=int(seq.findtext('duration','0'))
        # Some Resolve exports put the last frame index in sequence/duration
        # (N-1), while clip end/out and file lengths correctly contain N frames.
        # Full source coverage is still verified below; never trim a real frame.
        if declared_duration not in (cursor,cursor-1):
            raise AnalysisError(f'XML时间线长度与镜头覆盖不一致：总长度字段为{declared_duration}帧，镜头覆盖{cursor}帧。请核对导出的PGM时间线。')
    except (TypeError,ValueError) as exc:
        if isinstance(exc,AnalysisError): raise
        raise AnalysisError('XML镜头帧数或帧率字段无效。') from exc
    for block in blocks:
        if block['end']-block['start']!=block['frames']: raise AnalysisError('切点XML没有覆盖完整PGM，或重新定位的PGM长度不匹配。')
    return dict(xml_path=str(path),xml_digest=hashlib.sha256(raw).hexdigest(),fps=fps,duration=cursor,
                xml_declared_duration=declared_duration,
                pgm_info=blocks[0],pgm_start_tc=blocks[0]['start_tc'],pgm_sources=blocks,shots=shots,
                timeline_start_tc=seq.findtext('./timecode/string','01:00:00:00'))


def read_cut_xmls(paths, pgm_override='', pgm_overrides=None):
    """Concatenate complete cut timelines in the explicitly supplied order.

    Record positions are rebuilt; source positions and source clocks are never
    sorted or shifted. A recording cannot occur in two input XMLs.
    """
    paths = list(paths or [])
    if not paths:
        raise AnalysisError('请导入本场 PGM 分割后的切点 XML。')
    resolved = [str(Path(path).expanduser().resolve()) for path in paths]
    if len(set(resolved)) != len(resolved):
        raise AnalysisError('同一切点 XML 不能重复导入。')
    result = None
    cursor, seen, digests, declared = 0, set(), [], []
    for index, path in enumerate(resolved):
        cut = read_cut_xml(path, pgm_override if index == 0 else '', pgm_overrides)
        if result is not None and cut['fps'] != result['fps']:
            raise AnalysisError('同一拍摄场次的切点 XML 帧率必须一致。')
        if result is None:
            result = copy.deepcopy(cut)
            result['pgm_sources'], result['shots'] = [], []
        for source in cut['pgm_sources']:
            identity = str(Path(source['path']).expanduser().resolve())
            if identity in seen:
                raise AnalysisError('同一 PGM 不能在多个切点 XML 中重复使用：' + Path(identity).name)
            seen.add(identity)
            result['pgm_sources'].append(dict(source, start=source['start'] + cursor, end=source['end'] + cursor))
        result['shots'].extend(dict(shot, start=shot['start'] + cursor, end=shot['end'] + cursor)
                               for shot in cut['shots'])
        digests.append(cut['xml_digest'])
        declared.append(cut['xml_declared_duration'])
        cursor += cut['duration']
    result.update(duration=cursor, xml_paths=resolved, xml_digests=digests,
                  xml_declared_durations=declared, pgm_info=result['pgm_sources'][0])
    if len(paths) > 1:
        result['xml_digest'] = hashlib.sha256(json.dumps(digests, separators=(',', ':')).encode()).hexdigest()
        result['xml_declared_duration'] = sum(declared)
    return result


def timeline_info(config,require_tc=True):
    """Record positions are contiguous; source clocks may have intermission gaps."""
    if config.get('cut_xml_paths'):
        cut=read_cut_xmls(config['cut_xml_paths'],config.get('pgm',''),config.get('pgm_overrides',{}))
    elif config.get('cut_xml'):
        cut=read_cut_xml(config['cut_xml'],config.get('pgm',''),config.get('pgm_overrides',{}))
    else:
        # Internal export callers and older single-file results remain compatible.
        info=probe(config['pgm']);validate_rate(info,int(config['fps']))
        block=dict(info,start=0,end=info['frames'],xml_path=info['path'])
        cut=dict(fps=int(config['fps']),duration=info['frames'],pgm_info=block,pgm_sources=[block])
    fps=cut['fps'];previous_end=None
    for number,block in enumerate(cut['pgm_sources']):
        tc=config.get('pgm_timecodes',{}).get(block['path']) or (config.get('pgm_start_tc') if number==0 else '') or block['start_tc']
        block['start_tc']=tc
        if tc:
            start_tc=tc_frames(tc,fps);frames_tc(start_tc+block['frames'],fps)
            if previous_end is not None and start_tc<previous_end:
                raise AnalysisError('PGM时码重叠、倒退或重置，请核对各段首帧时码与拍摄顺序。')
            previous_end=start_tc+block['frames']
        elif require_tc: raise AnalysisError(f'请核对并填写PGM首帧时码：{Path(block["path"]).name}')
    return cut


def locate_frame(cut,index):
    for block in cut['pgm_sources']:
        if block['start']<=index<block['end']: return block,index-block['start']
    raise AnalysisError('预览帧超出整场时间线范围。')


def sample_decision(decisions, recovered=False):
    """Missing OCR is not evidence of another camera; never vote away a rival."""
    cameras = {d[0] for d in decisions if d[0] != '?'}
    if len(cameras) > 1 or any(d[1] == 'multiple_labels' for d in decisions):
        return '?', 'sample_conflict'
    if all(d[0] == decisions[0][0] for d in decisions):
        return decisions[0][:2]
    # Only explicit, threshold-checked reads on distinct frames count. Blank-A
    # inference cannot overrule an unreadable label from another sample.
    if recovered and sum(d[1] == 'label_read' and d[0] != '?' for d in decisions) >= 3:
        return next(iter(cameras)), 'label_read_consensus'
    return '?', 'sample_incomplete'


def retry_sample_indices(shot, sampled):
    """At most two additional frames, within this shot, near the cut only."""
    first, last = shot['source_in'], shot['source_out'] - 1
    candidates = sorted({min(first + offset, last) for offset in (8, 12)} - set(sampled))
    # Very short shots may have sampled their last frame already. Reading their
    # remaining few frames is bounded; never borrow a frame across the cut.
    if len(candidates) < 2 and last - first < 12:
        candidates += [index for index in range(first, last + 1)
                       if index not in sampled and index not in candidates][:2-len(candidates)]
    return candidates[:2]


def analyze_cut_xml(config, output_dir, progress=lambda n,message:None, cancelled=lambda:False, backend=None):
    from ocr import LabelReader,normalize,LABEL_MATCH_MODES
    from camera_schema import active_label_cameras,cameras_for
    cut=timeline_info(config)
    if cut['fps']!=int(config['fps']): raise AnalysisError('项目帧率与切点XML不一致。')
    if config.get('cut_xml_digest') and config['cut_xml_digest']!=cut['xml_digest']: raise AnalysisError('切点XML已改变，请重新导入。')
    if config.get('label_match_mode','legacy') not in LABEL_MATCH_MODES:
        raise AnalysisError('未知的机位标号匹配方式，请重新选择。')
    configured_rois=config.get('label_rois',{})
    cameras = set(cameras_for(config))
    label_cameras = set(active_label_cameras(config))
    if not label_cameras.issubset(configured_rois) or set(configured_rois)-cameras:
        raise AnalysisError('请设置'+ '/'.join(sorted(label_cameras)) +'全部标号区域。')
    # A's saved ROI can remain when changing to the unlabelled-A preset. It is
    # inactive and must not introduce text or invalid geometry into this run.
    rois={camera: configured_rois[camera] for camera in sorted(label_cameras)}
    for block in cut['pgm_sources']:
        for roi in rois.values(): roi_pixels(roi,block['width'],block['height'])
    configured_labels=config.get('labels',{})
    labels={camera: configured_labels.get(camera,'') for camera in sorted(label_cameras)}
    if set(configured_labels)-cameras or any(not normalize(v) for v in labels.values()) or len(set(map(normalize,labels.values())))!=len(label_cameras):
        raise AnalysisError('/'.join(sorted(label_cameras))+'标号必须为不同的非空值。')
    reader=LabelReader(config,backend);out=Path(output_dir);out.mkdir(parents=True,exist_ok=True)
    segments=[];sampled=0;retry_frames=0;retry_shots=0;recovered_shots=0;last_progress=0.;last_percent=-1
    # XML shots are already in monotonic source order. Retain one decoder and
    # no full-resolution frame cache. Normal shots still use exactly 0/2/5;
    # only incomplete readings get bounded extra samples, never a full scan.
    with frame_read_session(max_open=1, max_cache_bytes=0, cancelled=cancelled) as frame_session, (out/'ocr_samples.csv').open('w',newline='',encoding='utf-8-sig') as f:
        writer=csv.writer(f);writer.writerow(['shot','timeline_frame','pgm_file','pgm_frame','camera','reason','confidence','text'])
        for number,shot in enumerate(cut['shots'],1):
            decisions=[]
            indices=sorted({min(shot['source_in']+d,shot['source_out']-1) for d in (0,2,5)})
            def inspect_sample(index):
                nonlocal sampled
                if cancelled(): raise AnalysisError('已取消，未导出时间线。')
                frame=read_frame(shot['pgm_path'],index);timeline_frame=shot['start']+index-shot['source_in']
                decision=reader.inspect(frame,rois,timeline_frame);decisions.append(decision)
                if cancelled(): raise AnalysisError('已取消，未导出时间线。')
                camera,reason,confidence,texts=decision
                writer.writerow([number,timeline_frame,shot['pgm_path'],index,camera,reason,round(confidence,4),' | '.join(texts)]);sampled+=1
            for index in indices: inspect_sample(index)
            camera,reason=sample_decision(decisions)
            if reason == 'sample_incomplete' and any(d[1] == 'label_read' and d[0] != '?' for d in decisions):
                extra=retry_sample_indices(shot,indices)
                retry_shots+=bool(extra)
                for index in extra:
                    inspect_sample(index);retry_frames+=1
                    camera,reason=sample_decision(decisions,recovered=True)
                    if camera != '?' or reason == 'sample_conflict': break
                recovered_shots+=camera != '?'
            segments.append(Segment(shot['start'],shot['end'],camera,reason))
            percent=round(number/len(cut['shots'])*100);now=time.monotonic()
            if percent!=last_percent or now-last_progress>=.1:
                progress(percent,f'识别镜头：{number}/{len(cut["shots"])}；采样{sampled}帧')
                last_percent,last_progress=percent,now
    if cancelled(): raise AnalysisError('已取消，未导出时间线。')
    (out/'analysis.json').write_text(json.dumps(dict(config=config,pgm_info=cut['pgm_info'],pgm_sources=cut['pgm_sources'],
        duration=cut['duration'],cut_xml_digest=cut['xml_digest'],ocr_stats=dict(calls=reader.calls,cache_hits=reader.hits,sampled_frames=sampled,
            retry_frames=retry_frames,retry_shots=retry_shots,recovered_shots=recovered_shots,enhanced_calls=getattr(reader,'enhanced_calls',0)),
        media_stats=frame_session.stats,
        segments=[asdict(s) for s in segments]),ensure_ascii=False,indent=2),encoding='utf-8')
    return segments
