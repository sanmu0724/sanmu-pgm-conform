"""FCP7 XML (xmeml v5) timeline interchange for DaVinci Resolve 21.

Timeline frame positions are relative to sequence zero. Source in/out are file-relative,
not wall-clock timecode. Timecode metadata is stored separately in each file element.
"""
from pathlib import Path
import xml.etree.ElementTree as ET
from camera_schema import cameras_for


def node(parent,tag,value=None,**attributes):
    el = ET.SubElement(parent,tag,{k:str(v) for k,v in attributes.items()})
    if value is not None:
        el.text = str(value)
    return el


def rate(parent,fps):
    r = node(parent,'rate')
    node(r,'timebase',fps)
    node(r,'ntsc','FALSE')


def timecode(parent,frame,fps,reel=None):
    from core import frames_tc
    tc = node(parent,'timecode')
    rate(tc,fps)
    node(tc,'string',frames_tc(frame,fps))
    node(tc,'frame',frame)
    node(tc,'displayformat','NDF')
    if reel:
        node(node(tc,'reel'),'name',reel)


def characteristics(parent,width,height,fps):
    rate(parent,fps)
    node(parent,'width',width)
    node(parent,'height',height)
    node(parent,'anamorphic','FALSE')
    node(parent,'pixelaspectratio','square')
    node(parent,'fielddominance','none')


def audio_characteristics(parent,channels):
    audio = node(parent,'audio')
    node(audio,'channelcount',len(channels))
    sc = node(audio,'samplecharacteristics')
    node(sc,'depth',channels[0]['depth'])
    node(sc,'samplerate',channels[0]['sample_rate'])


def write_xml(config,events,sources,path,title,sound=None):
    from core import tc_frames, AnalysisError
    if sound is not None and sound[1]:
        raise AnalysisError('原始 WAV 需要采样级源入点，请使用多机位 FCPXML 导出。')
    cameras = cameras_for(config)
    if any(source.get('camera') not in cameras for source in sources):
        raise AnalysisError('代理素材机位与本场机位设置不符。')
    if any(event.get('path') and event.get('camera') not in cameras for event in events):
        raise AnalysisError('时间线包含本场未设置的机位。')
    fps = int(config['fps'])
    from cut_xml import timeline_info
    cut = timeline_info(config)
    source_map = {s['path']:s for s in sources}
    root = ET.Element('xmeml',version='5')
    sequence = node(root,'sequence',id='sequence-1')
    node(sequence,'name',title)
    node(sequence,'duration',cut['duration'])
    rate(sequence,fps)
    timecode(sequence,tc_frames(config.get('timeline_start_tc','01:00:00:00'),fps),fps)
    media = node(sequence,'media')
    video = node(media,'video')
    fmt = node(node(video,'format'),'samplecharacteristics')
    characteristics(fmt,int(config.get('timeline_width',3840)),int(config.get('timeline_height',2160)),fps)
    # Keep record positions absolute: distributing shots must never concatenate
    # each camera's selections or fill its inactive intervals with extra footage.
    camera_tracks = {camera:node(video,'track') for camera in cameras}
    for camera,track in camera_tracks.items():
        node(track,'name',camera)
    definitions = {}

    def clip(parent,index,start,end,source,source_in,source_out,name,enabled=True,mediatype='video',source_channel=1):
        item = node(parent,'clipitem',id=f'clip-{index}')
        node(item,'name',name)
        node(item,'enabled','TRUE' if enabled else 'FALSE')
        node(item,'duration',source['frames'])
        rate(item,fps)
        node(item,'start',start)
        node(item,'end',end)
        node(item,'in',source_in)
        node(item,'out',source_out)
        file_id = definitions.setdefault(source['path'],f'file-{len(definitions)+1}')
        file_el = node(item,'file',id=file_id)
        if file_id not in defined:
            defined.add(file_id)
            node(file_el,'name',Path(source['path']).name)
            node(file_el,'pathurl',Path(source['path']).resolve().as_uri())
            rate(file_el,fps)
            node(file_el,'duration',source['frames'])
            timecode(file_el,source['start'],fps,source['reel'])
            file_media = node(file_el,'media')
            if source.get('media_role') != 'sound_guide':
                sc = node(node(file_media,'video'),'samplecharacteristics')
                characteristics(sc,source['width'],source['height'],fps)
            # Proxy-camera scratch sound is analysis reference only. This
            # legacy picture-only interchange must never revive embedded sound.
        sourcetrack = node(item,'sourcetrack')
        node(sourcetrack,'mediatype',mediatype)
        node(sourcetrack,'trackindex',source_channel)
        return item

    audio = node(media,'audio')
    audio_format=node(node(audio,'format'),'samplecharacteristics')
    node(audio_format,'depth',24);node(audio_format,'samplerate',48000)
    audio_tracks = {}
    linked_items = []
    # One audio clip per video, on one track per camera. Stereo source media
    # remains stereo inside that clip rather than becoming two mono clips.
    def add_audio(video_item,key,index,start,end,source,source_in,source_out,name,enabled=True):
        channels=source.get('audio_channels',[])
        if not channels:return
        if key not in audio_tracks:
            track=node(audio,'track');node(track,'name',key)
            audio_tracks[key]=track
        track=audio_tracks[key]
        if source.get('sound_identity'):
            identity=source['sound_identity']
            name=identity['sound_roll']+' | '+(identity['original_audio_filename'] or Path(source['path']).name)
        item=clip(track,f'{index}-audio',start,end,source,source_in,source_out,name,enabled,'audio',1)
        item.set('premiereChannelType','stereo' if len(channels)>1 else 'mono')
        node(item,'channelcount',len(channels))
        linked_items.append([video_item,item])

    for camera in cameras:
        track=node(audio,'track');node(track,'name',camera)
        audio_tracks[camera]=track
    defined = set()
    for i,e in enumerate(events,1):
        if e['path']:
            track = camera_tracks[e['camera']]
            s = source_map[e['path']]
            source_in = e['source_in']-s['start']
            source_out = e['source_out']-s['start']
            name=f"{s['reel']} | {Path(s['path']).name}"
            item=clip(track,i,e['start'],e['end'],s,source_in,source_out,name)
        if not e['path']:
            marker = node(sequence,'marker')
            node(marker,'name','REVIEW_UNKNOWN')
            node(marker,'comment',f"{e['camera']} / {e['reason']} / end={e['end']}; 该位置留空，请确认机位并补齐代理")
            node(marker,'in',e['start'])
            node(marker,'out',e['end'])
    for track in camera_tracks.values():
        node(track,'enabled','TRUE');node(track,'locked','FALSE')
    for track in audio_tracks.values():
        node(track,'enabled','TRUE');node(track,'locked','FALSE')
    locations={}
    for kind in ('video','audio'):
        for track_index,track in enumerate(media.findall(f'{kind}/track'),1):
            for clip_index,item in enumerate(track.findall('clipitem'),1):
                locations[item.get('id')]=(kind,track_index,clip_index)
    for items in linked_items:
        for item in items:
            for other in items:
                kind,track_index,clip_index=locations[other.get('id')]
                link=node(item,'link');node(link,'linkclipref',other.get('id'))
                node(link,'mediatype',kind);node(link,'trackindex',track_index);node(link,'clipindex',clip_index)
    silent_sources=sorted({e['path'] for e in events if e['path'] and not source_map[e['path']].get('audio_channels')})
    import json
    Path(path).with_suffix('.audio.json').write_text(json.dumps(dict(
        audio_tracks=[track.findtext('name') for track in audio_tracks.values()],
        silent_proxy_files=silent_sources,
        note='此旧格式只输出画面，不引用代理或PGM内嵌原声。原始WAV声音通过多机位FCPXML输出，未覆盖区间音频留空。'),ensure_ascii=False,indent=2),encoding='utf-8')
    ET.indent(root,space='  ')
    text = ET.tostring(root,encoding='unicode')
    Path(path).write_text('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n'+text+'\n',encoding='utf-8')
    return Path(path)
