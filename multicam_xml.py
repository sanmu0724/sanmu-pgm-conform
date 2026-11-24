"""FCPXML multi-angle timeline using each recording's configured camera IDs."""
from fractions import Fraction
from bisect import bisect_right
from pathlib import Path
import xml.etree.ElementTree as ET
from camera_schema import cameras_for


def _unique_angle_spans(sources,cameras,offsets,base):
    """Sweep each camera once, retaining only unambiguous source intervals.

    An ambiguity is an unavailable angle at that clock, never a reason to pick
    whichever file happens to sort first. Asset definitions retain full media;
    only the multicam's component in/out points are trimmed.
    """
    from core import AnalysisError
    by_camera={camera:[] for camera in cameras}
    for source in sources:by_camera[source['camera']].append(source)
    result={}
    for camera,clips in by_camera.items():
        edges={}
        for identity,source in enumerate(clips):
            first=source['start']-offsets[camera]-base
            end=source['end']-offsets[camera]-base
            if source['frames']<=0 or end-first!=source['frames']:
                raise AnalysisError(f'{camera}机代理时长与源时码区间不一致，无法建立多机位角度。')
            edges.setdefault(first,[]).append((True,identity))
            edges.setdefault(end,[]).append((False,identity))
        positions=sorted(edges);active=set();spans=[]
        for number,first in enumerate(positions[:-1]):
            for opening,identity in edges[first]:
                if opening:active.add(identity)
                else:active.discard(identity)
            end=positions[number+1]
            if len(active)!=1 or end<=first:continue
            identity=next(iter(active));source=clips[identity]
            source_first=first+offsets[camera]+base
            # Adjacent sweep edges may belong to other sources; do not fragment
            # one continuously unique recording unnecessarily.
            if spans and spans[-1][0] is source and spans[-1][1]+spans[-1][3]==first:
                old=spans[-1];spans[-1]=(source,old[1],old[2],old[3]+end-first)
            else:spans.append((source,first,source_first,end-first))
        result[camera]=spans
    return result


def _pgm_angle_windows(cut, sources, cameras, offsets):
    """One compact, synchronized source domain per physical PGM recording."""
    from core import tc_frames
    fps=int(cut['fps'])
    spans=_unique_angle_spans(sources,cameras,offsets,0)
    starts={camera:[span[1] for span in rows] for camera,rows in spans.items()}
    windows=[]
    for block in cut['pgm_sources']:
        first=tc_frames(block['start_tc'],fps);length=block['end']-block['start'];last=first+length
        clipped={}
        for camera in cameras:
            rows=spans[camera];position=max(0,bisect_right(starts[camera],first)-1);parts=[]
            while position<len(rows):
                source,start,source_first,count=rows[position]
                if start>=last:break
                a=max(start,first);b=min(start+count,last)
                if b>a:parts.append((source,a-first,source_first+a-start,b-a))
                position+=1
            if parts:clipped[camera]=parts
        windows.append(dict(origin=first,duration=length,
                            cameras=tuple(camera for camera in cameras if camera in clipped),clips=clipped))
    return windows


def write_multicam_xml(config, events, sources, path, title,sound=None):
    from core import AnalysisError, tc_frames
    from cut_xml import timeline_info, locate_frame
    fps=int(config['fps']);cut=timeline_info(config)
    from sound_fallback import wav_only_sound_plan
    sound=wav_only_sound_plan(sound,cut,fps)
    def t(frame):
        value=Fraction(frame,fps)
        return f'{value.numerator}/{value.denominator}s'
    def seconds(value):
        value=Fraction(value);return f'{value.numerator}/{value.denominator}s'
    def n(parent,tag,**attrs):return ET.SubElement(parent,tag,{k:str(v) for k,v in attrs.items()})
    cameras=cameras_for(config)
    if not sources or any(source.get('camera') not in cameras for source in sources):
        raise AnalysisError('代理素材机位与本场机位设置不符。')
    if any(event.get('path') and not event.get('pgm_video_fallback') and event.get('camera') not in cameras for event in events):
        raise AnalysisError('时间线包含本场未设置的机位。')
    offsets={c:int(config.get('camera_offsets',{}).get(c,0)) for c in cameras}
    base=min(s['start']-offsets[s['camera']] for s in sources)
    limit=max(s['end']-offsets[s['camera']] for s in sources)
    root=ET.Element('fcpxml',version='1.8');resources=n(root,'resources')
    n(resources,'format',id='timeline-format',name=f'FFVideoFormat{config.get("timeline_height",2160)}p{fps}',
      frameDuration=t(1),width=config.get('timeline_width',3840),height=config.get('timeline_height',2160))
    ids={}
    for i,s in enumerate(sources,1):
        fmt=f'format-{i}';n(resources,'format',id=fmt,frameDuration=t(1),width=s['width'],height=s['height'])
        vid=f'video-{i}';ids[(s['path'],'video')]=vid
        attrs=dict(id=vid,name=Path(s['path']).name,src=Path(s['path']).resolve().as_uri(),
                   start=t(s['start']),duration=t(s['frames']),hasVideo='1',format=fmt)
        if s.get('audio_channels') and sound is None:
            attrs.update(hasAudio='1',audioSources='1',audioChannels=len(s['audio_channels']),audioRate=48000)
        asset=n(resources,'asset',**attrs)
        metadata=n(asset,'metadata')
        n(metadata,'md',key='com.apple.proapps.studio.reel',value=s['reel'])
        for key,value in (('com.pgm.sourceID',s.get('source_id')),('com.pgm.sessionID',s.get('session_id')),('com.pgm.productionID',s.get('production_id'))):
            if value:n(metadata,'md',key=key,value=value)
    if sound is not None:
        from sound_fallback import source_start, source_duration
        wavs,sound_events=sound;sound_ids={}
        for i,w in enumerate(wavs,1):
            ref=f'sound-{i}';sound_ids[w['path']]=ref
            asset=n(resources,'asset',id=ref,name=Path(w['path']).name,src=Path(w['path']).as_uri(),
                    start=seconds(source_start(w)),duration=seconds(source_duration(w)),
                    hasAudio='1',audioSources='1',audioChannels=w['channels'],audioRate=w.get('sample_rate',48000))
            metadata=n(asset,'metadata')
            identity={
                'com.apple.proapps.studio.reel':w.get('sound_roll',''),
                'com.apple.proapps.studio.scene':w.get('scene',''),
                'com.apple.proapps.studio.take':w.get('take',''),
                'com.pgm.originalAudioFilename':w.get('original_audio_filename',Path(w['path']).name),
                'com.pgm.recordingDate':w.get('recording_date',''),
                'com.pgm.recordingTime':w.get('recording_time',''),
                'com.pgm.originator':w.get('originator',''),
                'com.pgm.originatorReference':w.get('originator_reference',''),
                'com.pgm.fileUID':w.get('file_uid',''),
                'com.pgm.familyUID':w.get('family_uid',''),
                'com.pgm.umid':w.get('umid',''),
                'com.pgm.audioPairID':w.get('pair_id',''),
                'com.pgm.audioPairChannel':w.get('channel',''),
                'com.pgm.audioSourceKind':w.get('source_kind','wav')}
            if w.get('depth') is not None:
                identity['com.pgm.audioBitDepth']=w['depth']
            # Standard reel/scene/take plus readable notes and an accompanying
            # source manifest. Custom XML fields are not an AAF transport promise.
            note=' | '.join(f'{label}: {value}' for label,value in (
                ('Original audio',w.get('original_audio_filename',Path(w['path']).name)),
                ('Recorded',(' '.join([w.get('recording_date',''),w.get('recording_time','')])).strip()),
                ('Sound roll',w.get('sound_roll','')),('File UID',w.get('file_uid','')))
                if value)
            identity['com.apple.proapps.studio.logNote']=note
            for key,value in identity.items():
                if value:n(metadata,'md',key=key,value=value)
    # Availability is an explicit conform result, never inferred from the PGM
    # image here. A recording without these fields retains its legacy resource.
    variable=any('available_cameras' in event for event in events)
    event_masks=[]
    for event in events:
        values=event.get('available_cameras',cameras)
        if variable and (not isinstance(values,(list,tuple)) or
                         any(camera not in cameras for camera in values) or
                         len(set(values))!=len(values)):
            raise AnalysisError('多机位可用角度信息无效，请重新建立时间线。')
        mask=tuple(camera for camera in cameras if camera in values)
        if event.get('path') and not event.get('pgm_video_fallback') and (not mask or event['camera'] not in mask):
            raise AnalysisError('时间线所选机位不在该区间的可用角度中。')
        event_masks.append(mask)
    # Each physical PGM owns one compact source domain. A legacy spelling is
    # kept for old interchange fixtures, never inferred from the source TC.
    pgm_local=variable and (config.get('multicam_resource_layout','pgm')!='legacy_masks' or any(event.get('pgm_video_fallback') for event in events))
    windows=_pgm_angle_windows(cut,sources,cameras,offsets) if pgm_local else None
    block_starts=[block['start'] for block in cut['pgm_sources']] if pgm_local else []
    event_keys=[]
    for event,mask in zip(events,event_masks):
        event_keys.append(bisect_right(block_starts,event['start'])-1 if pgm_local else mask)
    pgm_video_ids={}
    fallback_events={}
    for event,key in zip(events,event_keys):
        if not event.get('pgm_video_fallback'):continue
        if not pgm_local or event.get('available_cameras'):
            raise AnalysisError('PGM参考画面只能用于全部机位无代理的区间。')
        block=cut['pgm_sources'][key];expected=windows[key]['origin']+event['start']-block['start']
        if (Path(event['path']).resolve()!=Path(block['path']).resolve()
                or event.get('source_in')!=expected or event.get('source_out')!=expected+event['end']-event['start']):
            raise AnalysisError('PGM参考画面的文件或源入出点与实际录制片段不一致。')
        fallback_events.setdefault(key,[]).append(event)
    if pgm_local:
        for key,window in enumerate(windows):
            spans=[];block=cut['pgm_sources'][key]
            for event in fallback_events.get(key,()):
                position=event['start']-block['start'];length=event['end']-event['start']
                if spans and spans[-1][1]+spans[-1][3]==position:
                    old=spans[-1];spans[-1]=(old[0],old[1],old[2],old[3]+length)
                else:spans.append((dict(path=block['path']),position,window['origin']+position,length))
            if not spans:continue
            window['clips']['PGM_REF']=spans;window['cameras']+=('PGM_REF',)
            identity='pgm-video-'+str(key+1);pgm_video_ids[key]=identity
            fmt='pgm-format-'+str(key+1)
            n(resources,'format',id=fmt,frameDuration=t(1),width=block.get('width',config.get('timeline_width',1920)),height=block.get('height',config.get('timeline_height',1080)))
            asset=n(resources,'asset',id=identity,name=Path(block['path']).name,src=Path(block['path']).resolve().as_uri(),
                start=t(window['origin']),duration=t(window['duration']),hasVideo='1',format=fmt)
            metadata=n(asset,'metadata');n(metadata,'md',key='com.pgm.videoSourceKind',value='pgm_fallback')
            n(metadata,'md',key='com.pgm.originalPGMPath',value=block['path'])
    keys=list(dict.fromkeys(key for event,key in zip(events,event_keys) if event.get('path')))
    if not variable:keys=[cameras]
    unique_spans=_unique_angle_spans(sources,cameras,offsets,base) if variable and not pgm_local else None
    media_refs={};angle_refs={};resource_origins={}
    for key in keys:
        window=windows[key] if pgm_local else None
        mask=window['cameras'] if window else key
        if not mask:raise AnalysisError('PGM 区间没有可建立的真实机位。')
        token=('pgm-'+str(key+1)) if pgm_local else ''.join(mask)
        media_id='multicam-'+token if variable else 'multicam-main'
        suffix=(str(key+1)+'_'+Path(cut['pgm_sources'][key]['path']).stem) if pgm_local else token
        multicam_name=title+'_'+suffix
        media_refs[key]=(media_id,multicam_name)
        resource_origins[key]=window['origin'] if window else base
        media=n(resources,'media',id=media_id,name=multicam_name)
        resource_duration=window['duration'] if window else limit-base
        mc=n(media,'multicam',format='timeline-format',duration=t(resource_duration),tcStart='0s',tcFormat='NDF')
        for camera in mask:
            angle_id='angle-'+token+'-'+camera if variable else 'angle-'+camera
            angle_refs[(key,camera)]=angle_id
            angle=n(mc,'mc-angle',name=camera,angleID=angle_id)
            clips=(window['clips'][camera] if window else unique_spans[camera] if variable else
                [(s,s['start']-offsets[camera]-base,s['start'],s['frames'])
                 for s in sorted((s for s in sources if s['camera']==camera),key=lambda s:s['start'])])
            cursor=0
            for source,position,source_first,length in clips:
                if position<cursor:raise AnalysisError(f'{camera}机代理时码重叠，无法建立唯一的多机位角度。')
                if position>cursor:n(angle,'gap',offset=t(cursor),start='0s',duration=t(position-cursor))
                composite=n(angle,'clip',name=Path(source['path']).name,offset=t(position),start=t(source_first),duration=t(length),format='timeline-format')
                n(composite,'video',ref=pgm_video_ids[key] if camera=='PGM_REF' else ids[(source['path'],'video')],offset=t(source_first),start=t(source_first),duration=t(length))
                channels=source.get('audio_channels',[])
                if channels and sound is None:
                    n(composite,'audio',ref=ids[(source['path'],'video')],offset=t(source_first),start=t(source_first),duration=t(length),
                      srcCh='1, 2' if len(channels)>1 else '1',outCh='L,R' if len(channels)>1 else 'C',role='dialogue')
                cursor=position+length
            if cursor<resource_duration:n(angle,'gap',offset=t(cursor),start='0s',duration=t(resource_duration-cursor))
    project=n(n(n(root,'library'),'event',name=config.get('show_id','S01')),'project',name=title)
    record=tc_frames(config.get('timeline_start_tc','01:00:00:00'),fps)
    seq=n(project,'sequence',format='timeline-format',duration=t(cut['duration']),tcStart=t(record),tcFormat='NDF',audioLayout='stereo',audioRate='48k')
    spine=n(seq,'spine')
    # FCPXML record offsets include the sequence's starting timecode.
    for e,mask,key in zip(events,event_masks,event_keys):
        duration=e['end']-e['start']
        if not e['path']:
            empty=bool(e.get('empty_camera_gap'))
            name='NO_CAMERA_SOURCE' if empty else 'REVIEW_UNKNOWN'
            note='全部机位无对应代理录制；保留原时长，画面留空。' if empty else e.get('reason','unknown')
            gap=n(spine,'gap',name=name,offset=t(record+e['start']),start=t(record),duration=t(duration))
            n(gap,'marker',start=t(record) if empty else '0s',duration=t(duration),value=name,note=note);continue
        block,local=locate_frame(cut,e['start'])
        source=tc_frames(block['start_tc'],fps)+local-resource_origins[key]
        enable='video' if sound is not None else 'all'
        media_id,multicam_name=media_refs[key]
        item=n(spine,'mc-clip',ref=media_id,name=multicam_name,offset=t(record+e['start']),start=t(source),duration=t(duration),srcEnable=enable)
        n(item,'mc-source',angleID=angle_refs[(key,'PGM_REF' if e.get('pgm_video_fallback') else e['camera'])],srcEnable=enable)
        if e.get('pgm_video_fallback'):
            n(item,'marker',start=t(source),duration=t(duration),value='PGM_VIDEO_FALLBACK',
              note='本区间无对应机位代理，画面引用原PGM；本区间不附声音。')
            metadata=n(item,'metadata');n(metadata,'md',key='com.pgm.videoSourceKind',value='pgm_fallback')
        if e.get('reason')=='camera_unavailable_fallback' and e.get('fallback_reason')=='source_gap':
            requested=e.get('requested_camera','?')
            n(item,'marker',start=t(source),duration=t(duration),value='REVIEW_CAMERA_FALLBACK',
              note=f'原机位 {requested} 无对应代理录制，初始角度改为 {e["camera"]}；请核对画面。')
    if sound is not None:
        # Each edited picture owns only its corresponding original-WAV range.
        # A recording boundary may produce several audio parts for one picture;
        # intersect in rational seconds so waveform/subframe offsets stay intact.
        for anchor in list(spine):
            if any(node.get('key')=='com.pgm.videoSourceKind' and node.get('value')=='pgm_fallback'
                   for node in anchor.findall('./metadata/md')):continue
            anchor_start=Fraction(anchor.get('start')[:-1])
            anchor_record=Fraction(anchor.get('offset')[:-1])-Fraction(record,fps)
            anchor_end=anchor_record+Fraction(anchor.get('duration')[:-1])
            parts=[]
            for e in sound_events:
                first=max(anchor_record,e['record_start'])
                end=min(anchor_end,e['record_start']+e['duration'])
                if end<=first:continue
                parts.append((e,first,end))
            if anchor.tag=='mc-clip' and parts:
                # Resolve treats unlaned picture/audio components of a clip as
                # linked timeline items. A lane=-1 connected clip imports as
                # independent audio even when its range matches the picture.
                wrapper=ET.Element('clip',{k:anchor.get(k) for k in ('name','offset','start','duration')})
                wrapper.set('format','timeline-format')
                spine.insert(list(spine).index(anchor),wrapper);spine.remove(anchor)
                anchor.set('offset',seconds(anchor_start));wrapper.append(anchor)
                host=wrapper
            else:
                # Unknown picture gaps have no video item to link. Retain their
                # original audio without attaching it to an adjacent picture.
                host=anchor
            for e,first,end in parts:
                attrs=dict(ref=sound_ids[e['path']],name=Path(e['path']).name,
                           offset=seconds(anchor_start+first-anchor_record),
                           start=seconds(e['source_start']+first-e['record_start']),duration=seconds(end-first))
                channels=', '.join(str(e['src_channel']).replace(' ','').split(','))
                output=e.get('out_channel') or ('L,R' if ',' in channels else 'C')
                if e.get('pair_id'):
                    # Resolve 21 imports only the first direct audio component
                    # of a composite clip. Distinct connected clips preserve
                    # both original mono WAVs, including their source identity.
                    # The accompanying Resolve import script restores the A/V
                    # link group and pan (Resolve also ignores outCh here).
                    connected=n(host,'clip',name=attrs['name'],offset=attrs['offset'],
                                start=attrs['start'],duration=attrs['duration'],
                                lane='-2' if output=='R' else '-1')
                    child_attrs={**attrs,'offset':attrs['start']}
                    n(connected,'audio',**child_attrs,srcCh=channels,outCh=output,role='dialogue')
                elif host is anchor:
                    n(host,'asset-clip',**attrs,lane='-1',srcEnable='audio',audioRole='dialogue')
                else:
                    # Resolve 21 misreads the first stereo channel in "1,2";
                    # the comma-space spelling imports as source channels 1/2.
                    # Paired mono recordings retain their two original WAV
                    # assets. Each component owns its explicit stereo output;
                    # centering both mono components would sum rather than
                    # reproduce the original L/R recording.
                    n(host,'audio',**attrs,srcCh=channels,outCh=output,role='dialogue')
            # Connected clips must precede markers, including an unknown gap.
            markers=[child for child in host if child.tag=='marker']
            for marker in markers:
                host.remove(marker);host.append(marker)
        from sound_fallback import sound_reports
        gaps=[r for r in sound_reports(wavs) if r.get('status')=='uncovered']
        # Put each uncovered audio interval on the corresponding video clip.
        # Marker start uses that clip's source coordinate, not wall-clock time.
        for gap in gaps:
            first=Fraction(str(gap['record_start_seconds']))
            end=first+Fraction(str(gap['duration_seconds']))
            for clip in spine:
                position=Fraction(clip.get('offset')[:-1])-Fraction(record,fps)
                length=Fraction(clip.get('duration')[:-1])
                a=max(first,position);b=min(end,position+length)
                if b>a:
                    n(clip,'marker',start=seconds(Fraction(clip.get('start')[:-1])+a-position),
                      duration=seconds(b-a),value='REVIEW_AUDIO_GAP',note=gap.get('reason') or '原始主声音未覆盖此区间；未生成替代声音。')
    if config.get('dialogue_markers'):
        from library_export import add_dialogue_markers
        add_dialogue_markers(spine,config,cut,sound,record)
    ET.indent(root,space='  ')
    Path(path).write_text('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'+ET.tostring(root,encoding='unicode')+'\n',encoding='utf-8')
    from resolve_import_helper import write_resolve_import_helper
    write_resolve_import_helper(path)
    return Path(path)
