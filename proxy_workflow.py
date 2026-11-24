"""Explicit full-clip proxy/original mapping and edited FCP7 XML conform."""
from pathlib import Path
from urllib.parse import urlparse,unquote
import json
import xml.etree.ElementTree as ET


def file_path(url):
    parsed=urlparse(url or '')
    if parsed.scheme != 'file' or parsed.netloc not in ('','localhost'): return None
    return str(Path(unquote(parsed.path)).expanduser().resolve())


def normalize_guide_audio(rows, path_mappings=()):
    """Keep scene-scoped audio identities; a shared path never picks one scene."""
    from library_bridge import remap_path
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ValueError('声音身份对应表必须为列表。')
    result, seen = [], {}
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get('session_id'), str)
                or not row['session_id'] or not isinstance(row.get('path'), str)
                or not row['path'] or not Path(row['path']).expanduser().is_absolute()):
            raise ValueError('声音身份对应表缺少明确的场次编号或完整素材路径。')
        value = dict(row)
        value['path'] = str(Path(remap_path(value['path'], path_mappings)).expanduser().resolve())
        identity = (value['session_id'], value['path'])
        if identity not in seen:
            result.append(value)
            seen[identity] = value
        else:
            if (seen[identity].get('audio_bit_depth') and value.get('audio_bit_depth')
                    and seen[identity]['audio_bit_depth'] != value['audio_bit_depth']):
                raise ValueError('同一原始 WAV 的位深信息不一致：'+value['path'])
            for key, field in value.items():
                if not seen[identity].get(key):
                    seen[identity][key] = field
    return result


def original_wav_guide_audio(rows, path_mappings=()):
    """New exports retain original WAV identities; old rows remain readable."""
    from sound_fallback import is_original_wav_source
    return [row for row in normalize_guide_audio(rows or [],path_mappings)
            if is_original_wav_source(row)]


def normalize_video_fallbacks(rows, path_mappings=()):
    """Scene-scoped PGM picture identities, separate from proxy and WAV maps."""
    return [dict(row,source_kind='pgm_fallback') for row in normalize_guide_audio(rows or [],path_mappings)
            if row.get('source_kind')=='pgm_fallback']


def guide_audio_identities(config, sources=(), sound=None):
    """Describe the audio actually used by this scene without reading its bytes."""
    config = config or {}
    context = config.get('library_context') or {}
    session_id = context.get('session_id')
    if not session_id:
        identities = {row.get('session_id') for row in sources if row.get('session_id')}
        if len(identities) == 1:
            session_id = next(iter(identities))
    if not session_id:
        return []  # Legacy maps have no trustworthy cross-scene ownership.
    production_id = context.get('production_id', '')
    rows = []
    def add(path, kind, detail=None, header=None):
        if path:
            value = dict(session_id=session_id, production_id=production_id,
                         path=str(Path(path).expanduser().resolve()), source_kind=kind)
            if detail and detail.get('pair_id'):
                value.update(pair_id=detail['pair_id'], channel=detail.get('channel'),
                             out_channel=detail.get('out_channel') or detail.get('channel'))
            # Only an actual probed WAV header supplies bit depth. Event
            # annotations and a default encoding are not source evidence.
            depth=(header or {}).get('depth')
            if type(depth) is int and 0 < depth <= 64:
                value['audio_bit_depth']=depth
            rows.append(value)
    if sound is not None:
        wavs, events = sound
        by_path = {row['path']: row for row in wavs if row.get('path')}
        for event in events:
            path = event.get('path')
            if path:
                header=by_path.get(path,{})
                add(path, event.get('source_kind') or header.get('source_kind', 'wav'), event, header)
    else:
        for source in config.get('sound_sources', []):
            add(source.get('path'), source.get('source_kind', 'wav'), source, source)
    return original_wav_guide_audio(rows)


def export_mapping(sources,output,fps,config=None,sound=None,video_fallbacks=()):
    import csv
    records=[dict(camera=s['camera'],reel=s['reel'],proxy_path=s['path'],proxy_start=s['start'],
                  frames=s['frames'],original_path=s.get('original_path',''),
                  original_start=s.get('original_start'),original_width=s.get('original_width'),
                  original_height=s.get('original_height'),source_id=s.get('source_id',''),
                  session_id=s.get('session_id',''),production_id=s.get('production_id','')) for s in sources]
    data=dict(version=1,fps=fps,records=records,
              guide_audio=guide_audio_identities(config, sources, sound),
              video_fallbacks=normalize_video_fallbacks([row for row in video_fallbacks if row.get('session_id')]))
    # These stereo files are newly generated delivery media, not replacements
    # for the two original mono recordings. Keep both original identities in
    # the correspondence table and call out the stable folder to copy.
    context=(config or {}).get('library_context') or {}
    merged=[]
    if sound is not None:
        for row in sound[0]:
            if row.get('stereo_merge_sidecar') and row.get('merged_from'):
                merged.append(dict(path=str(row['path']),stereo_merge_sidecar=str(row['stereo_merge_sidecar']),
                    merged_from=row['merged_from'],session_id=context.get('session_id',''),
                    production_id=context.get('production_id','')))
    data['merged_audio_sources']=merged
    directory=(config or {}).get('stereo_audio_cache_dir')
    data['required_media_directories']=list(dict.fromkeys(
        [str(Path(directory).expanduser().resolve())] if merged and directory else
        [str(Path(row['path']).expanduser().resolve().parent) for row in merged]))
    p=Path(output)/'proxy_original_map.json'
    p.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    with (Path(output)/'proxy_original_map.csv').open('w',newline='',encoding='utf-8-sig') as f:
        writer=csv.DictWriter(f,fieldnames=list(records[0]) if records else []);writer.writeheader();writer.writerows(records)
    return p


def _strip_camera_reference_audio_xmeml(root, camera_paths, guide_audio=(), path_mappings=()):
    """FCP7 equivalent: remove known camera/PGM audio, never alter video edits."""
    import copy
    from library_bridge import remap_path
    def key(value):
        return str(Path(remap_path(value,path_mappings)).expanduser().resolve()) if value else ''
    blocked_paths={key(value) for value in camera_paths if value}
    blocked_paths.update(key(row['path']) for row in guide_audio if row.get('path') and row.get('source_kind')=='pgm')
    definitions={node.get('id'):copy.deepcopy(node) for node in root.iter('file') if node.find('pathurl') is not None}
    blocked_ids={identity for identity,node in definitions.items() if key(file_path(node.findtext('pathurl'))) in blocked_paths}
    removed=set();count=0
    for track in root.findall('.//media/audio/track'):
        for clip in list(track.findall('clipitem')):
            file=clip.find('file')
            if file is not None and (file.get('id') in blocked_ids or key(file_path(file.findtext('pathurl'))) in blocked_paths):
                if clip.get('id'):removed.add(clip.get('id'))
                track.remove(clip);count+=1
    links=0
    for clip in root.iter('clipitem'):
        for link in list(clip.findall('link')):
            if link.findtext('linkclipref') in removed:clip.remove(link);links+=1
    # Preserve definitions if Resolve happened to place the full file node on
    # an audio item that was removed while the video uses a short id reference.
    present={node.get('id') for node in root.iter('file') if node.find('pathurl') is not None}
    for node in root.iter('file'):
        identity=node.get('id')
        if identity not in present and identity in definitions:
            node.attrib.clear();node.attrib.update(definitions[identity].attrib)
            node.extend(copy.deepcopy(list(definitions[identity])));present.add(identity)
        if identity in blocked_ids:
            media=node.find('media')
            if media is not None:
                for audio in list(media.findall('audio')):media.remove(audio)
    return dict(removed_audio_components=count,removed_links=links)


def _silence_pgm_fallback_wavs_xmeml(root,fallback_paths,guide_audio,path_mappings=()):
    """Trim only explicitly linked original WAVs out of PGM replacement edits."""
    import copy
    from library_bridge import remap_path
    def key(value):return str(Path(remap_path(value,path_mappings)).expanduser().resolve()) if value else ''
    fallback_paths={key(path) for path in fallback_paths}
    wav_paths={key(row['path']) for row in guide_audio if row.get('source_kind','wav')=='wav' and row.get('path')}
    definitions={node.get('id'):copy.deepcopy(node) for node in root.iter('file') if node.find('pathurl') is not None}
    files={identity:key(file_path(node.findtext('pathurl'))) for identity,node in definitions.items()}
    total=0;active=set()
    for sequence in root.findall('.//sequence'):
        video=[node for node in sequence.findall('./media/video/track/clipitem') if node.find('file') is not None and files.get(node.find('file').get('id')) in fallback_paths]
        active.update(files[node.find('file').get('id')] for node in video)
        changed=set()
        for track in sequence.findall('./media/audio/track'):
            for audio in list(track.findall('clipitem')):
                file=audio.find('file')
                if file is None or files.get(file.get('id')) not in wav_paths:continue
                links={node.findtext('linkclipref') for node in audio.findall('link')}
                holes=[(int(node.findtext('start')),int(node.findtext('end'))) for node in video if node.get('id') in links or
                       audio.get('id') in {link.findtext('linkclipref') for link in node.findall('link')}]
                if not holes:continue
                first,last=int(audio.findtext('start')),int(audio.findtext('end'));parts=[(first,last)]
                for a,b in holes:
                    parts=[piece for start,end in parts for piece in ((start,min(end,a)),(max(start,b),end)) if piece[1]>piece[0]]
                if parts==[(first,last)]:continue
                total+=1;changed.add(audio.get('id'));position=list(track).index(audio);track.remove(audio)
                for number,(a,b) in enumerate(parts):
                    item=copy.deepcopy(audio);item.set('id',str(audio.get('id'))+'-pgm-silence-'+str(number))
                    for field,value in (('start',a),('end',b),('in',int(audio.findtext('in'))+a-first),('out',int(audio.findtext('in'))+b-first)):
                        item.find(field).text=str(value)
                    for link in list(item.findall('link')):item.remove(link)
                    track.insert(position+number,item)
        for node in sequence.iter('clipitem'):
            for link in list(node.findall('link')):
                if link.findtext('linkclipref') in changed:node.remove(link)
    present={node.get('id') for node in root.iter('file') if node.find('pathurl') is not None}
    for node in root.iter('file'):
        identity=node.get('id')
        if identity not in present and identity in definitions:
            node.attrib.clear();node.attrib.update(definitions[identity].attrib)
            node.extend(copy.deepcopy(list(definitions[identity])));present.add(identity)
    return total,active


def conform_edited_xml(xml_path,map_paths,output_path,path_mappings=None):
    """Preserve editorial decisions; swap only explicitly mapped file definitions.

    Requires one proxy per whole original with identical timecode and frame count.
    Unknown added media is retained and listed in a report, never filename-guessed.
    """
    from core import AnalysisError,probe,tc_frames,validate_rate
    from library_bridge import read_settings,remap_path
    mappings=read_settings().get('path_mappings',[]) if path_mappings is None else path_mappings
    index={};fps_set=set();identities={};guide_audio=[];video_fallbacks=[]
    for p in map_paths:
        data=json.loads(Path(p).read_text(encoding='utf-8'));fps_set.add(int(data['fps']))
        guide_audio.extend(normalize_guide_audio(data.get('guide_audio', [])))
        video_fallbacks.extend(normalize_video_fallbacks(data.get('video_fallbacks',[]),mappings))
        for row in data['records']:
            row=dict(row)
            for field in ('proxy_path','original_path'):
                if row.get(field):row[field]=remap_path(row[field],mappings)
            key=str(Path(row['proxy_path']).resolve())
            if key in index and index[key] != row: raise AnalysisError(f'代理映射冲突：{key}')
            index[key]=row
            uid=row.get('source_id')
            if uid:
                if uid in identities and identities[uid]!=row:raise AnalysisError(f'素材唯一编号映射冲突：{uid}')
                identities[uid]=row
    if len(fps_set)!=1: raise AnalysisError('映射表帧率不一致。')
    fps=next(iter(fps_set));tree=ET.parse(xml_path);root=tree.getroot()
    if root.tag=='fcpxml':
        from fcpxml_conform import conform_fcpxml
        return conform_fcpxml(tree,index,fps,xml_path,output_path,identity_index=identities,path_mappings=mappings,
                              guide_audio=normalize_guide_audio(guide_audio),video_fallbacks=normalize_video_fallbacks(video_fallbacks))
    if root.tag!='xmeml': raise AnalysisError('请选择达芬奇导出的FCPXML或Final Cut Pro 7 XML，不是DRT。')
    for seq in root.findall('.//sequence'):
        if seq.findtext('rate/timebase') != str(fps) or seq.findtext('rate/ntsc')=='TRUE':
            raise AnalysisError('剪辑时间线帧率与代理映射不一致。')
    camera_paths={value for row in index.values() for value in (row.get('proxy_path'),row.get('original_path')) if value}
    camera_paths.update(index)
    fallback_paths={row['path'] for row in video_fallbacks}
    removed_fallback_wavs,active_pgm=_silence_pgm_fallback_wavs_xmeml(root,fallback_paths,guide_audio,mappings)
    camera_paths.update(fallback_paths)
    audio_cleanup=_strip_camera_reference_audio_xmeml(root,camera_paths,guide_audio,mappings)
    files=list(root.iter('file'));replaced={};unmapped=set();verified={};relocated=0;pgm_retained=set()
    for row in index.values():
        if row.get('original_path') and row.get('original_start')!=row.get('proxy_start'):
            raise AnalysisError('对应表中的原片与代理起始时码不一致。')
    for f in files:
        url=f.find('pathurl')
        if url is None: continue
        old_path=file_path(url.text);path=remap_path(old_path,mappings)
        if old_path and path!=old_path:url.text=Path(path).resolve().as_uri();relocated+=1
        if path in fallback_paths:
            pgm_retained.add(path);continue
        if path not in index:
            unmapped.add(path or url.text);continue
        row=index[path];original=row.get('original_path')
        if not original: raise AnalysisError(f'代理尚未对应原片：{path}。请补充原片对应信息并重新导出映射表。')
        if original not in verified:
            info=probe(original);validate_rate(info,fps)
            start=row.get('original_start')
            if start is None and info['start_tc']: start=tc_frames(info['start_tc'],fps)
            if start!=row['proxy_start'] or info['frames']!=row['frames']:
                raise AnalysisError(f'原片与代理时码/帧数不一致：{original}')
            verified[original]=info
        info=verified[original]
        url.text=Path(original).resolve().as_uri()
        name=f.find('name')
        if name is not None: name.text=Path(original).name
        # Existing file timing/reel metadata already equals the verified original.
        for sc in f.findall('media/video/samplecharacteristics'):
            for tag,value in [('width',info['width']),('height',info['height'])]:
                el=sc.find(tag)
                if el is not None: el.text=str(value)
        # Camera audio is intentionally absent. Adding raw-file audio channel
        # definitions here could revive a removed proxy scratch sound.
        media=f.find('media')
        if media is not None:
            for audio in list(media.findall('audio')):media.remove(audio)
        replaced[f.get('id')]=original
    if not replaced and not active_pgm: raise AnalysisError('没有找到映射表中的代理路径。若代理已移动，请先在达芬奇重新链接，再导出XML并核对映射表。')
    durations = {f.get('id'):int(f.findtext('duration')) for f in files if f.findtext('duration')}
    for clip in root.iter('clipitem'):
        file=clip.find('file')
        if file is not None and file.get('id') in replaced:
            if clip.findtext('sourcetrack/mediatype')=='audio':
                channels=verified[replaced[file.get('id')]].get('audio_channels',[])
                source_channel=int(clip.findtext('sourcetrack/trackindex','1'))
                required_channels=2 if clip.get('premiereChannelType')=='stereo' else 1
                if not 1<=source_channel or source_channel+required_channels-1>len(channels):
                    raise AnalysisError(f'原片缺少代理音频对应的第{source_channel}声道：{replaced[file.get("id")]}。请核对代理制作时的声道映射。')
            name=clip.find('name')
            if name is not None: name.text=Path(replaced[file.get('id')]).name
            # Guard against an edit/retime extending beyond available source handles.
            duration=durations.get(file.get('id'))
            if duration:
                for tag in ('in','out'):
                    value=clip.findtext(tag)
                    if value and (float(value)<0 or float(value)>float(duration)):
                        raise AnalysisError('剪辑入出点超出原片范围，请检查变速/延展镜头。')
    output=Path(output_path)
    if output.resolve()==Path(xml_path).resolve(): raise AnalysisError('回套文件不能覆盖剪辑XML。')
    ET.indent(root,space='  ')
    output.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n'+ET.tostring(root,encoding='unicode')+'\n',encoding='utf-8')
    report=dict(replaced_file_definitions=len(replaced),unmapped_retained=sorted(str(s) for s in unmapped),relocated_file_definitions=relocated,camera_audio_cleanup=audio_cleanup,
                pgm_video_fallbacks_retained=sorted(pgm_retained),pgm_fallback_wavs_removed=removed_fallback_wavs,
                note='视频按映射回套原片；原始WAV及新增未映射声音保留，已明确识别的PGM和摄影机参考声音移除。复杂效果需达芬奇逐项核对。')
    output.with_suffix('.review.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report
