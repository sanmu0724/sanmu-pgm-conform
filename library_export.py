"""Carry confirmed dialogue locations into existing XML without retiming clips."""
from __future__ import annotations

import json
import hashlib
import math
from fractions import Fraction
from pathlib import Path
import xml.etree.ElementTree as ET

from library_bridge import resolve_local_path


def _path(value):
    return str(Path(resolve_local_path(value)).expanduser().resolve()) if value else ''


def sync_signature(config, cut):
    from library_bridge import canonical_path,translate_config
    from sound_fallback import reference_provenance_config
    canonical=reference_provenance_config(translate_config(config,reverse=True))
    data={key:canonical.get(key) for key in ('sound_sources','sound_sync_mode','sound_reference_overrides', 'library_sound_fallback')}
    policy=config.get('sound_unmatched_policy','pgm')
    # PGM remains the same analysis reference under WAV-only export silence;
    # the new output policy must not invalidate existing transcript provenance.
    data['sound_unmatched_policy']='pgm' if policy=='silence' else policy
    data.update(fps=config['fps'],cut_xml_digest=cut.get('xml_digest'),
                pgm=[dict(path=canonical_path(block['path']),frames=block['frames'],start=block['start'],
                          start_tc=block.get('start_tc')) for block in cut['pgm_sources']])
    return hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def dialogue_ranges(config, cut, sound=None):
    """Return timeline seconds from *this recording's* actual source positions.

    Semantic links between different performances never determine sync offsets.
    A WAV transcription is mapped through the already verified sound plan.
    """
    from sound_fallback import historical_sound_references, sound_entry
    references = historical_sound_references(config)
    result = []; fps = int(config['fps'])
    for marker in config.get('dialogue_markers', []):
        if marker.get('status') != 'confirmed':
            continue
        if marker.get('session_id') != config.get('library_context', {}).get('session_id'):
            continue
        first = Fraction(str(marker['start_seconds']))
        last = Fraction(str(marker['end_seconds']))
        if first < 0 or last <= first:
            continue
        path = _path(marker.get('media_path', ''))
        ranges = []
        for block in cut['pgm_sources']:
            if path == _path(block['path']):
                a, b = max(first, 0), min(last, Fraction(block['frames'], fps))
                if b > a:
                    ranges.append((Fraction(block['start'], fps) + a, Fraction(block['start'], fps) + b))
            reference=sound_entry(references, block['path'])
            if reference and path==_path(reference.get('path')):
                origin=Fraction(str(reference.get('start_seconds',0)))
                a,b=max(first,origin),min(last,origin+Fraction(block['frames'],fps))
                if b>a:ranges.append((Fraction(block['start'],fps)+a-origin,Fraction(block['start'],fps)+b-origin))
        from core import tc_frames
        for source in config.get('sources',[]):
            if path not in (_path(source.get('proxy_path') or source.get('path')),_path(source.get('original_path'))):
                continue
            if not source.get('start_tc'):continue
            origin=Fraction(tc_frames(source['start_tc'],fps)-int(config.get('camera_offsets',{}).get(source['camera'],0)),fps)
            a,b=max(first,0),min(last,Fraction(int(source['frames']),fps))
            if b<=a:continue
            for block in cut['pgm_sources']:
                if not block.get('start_tc'):continue
                pgm_origin=Fraction(tc_frames(block['start_tc'],fps),fps)
                begin,end=max(origin+a,pgm_origin),min(origin+b,pgm_origin+Fraction(block['frames'],fps))
                if end>begin:ranges.append((Fraction(block['start'],fps)+begin-pgm_origin,Fraction(block['start'],fps)+end-pgm_origin))
        if sound is not None:
            from sound_fallback import source_start
            wavs, events = sound
            starts = {_path(w['path']): source_start(w) for w in wavs}
            for event in events:
                if event.get('source_kind') == 'pgm':
                    continue  # PGM file-local dialogue was mapped above.
                if path != _path(event['path']):
                    continue
                source = Fraction(event['source_start']) - starts[path]
                a, b = max(first, source), min(last, source + event['duration'])
                if b > a:
                    ranges.append((event['record_start'] + a - source, event['record_start'] + b - source))
        for a, b in sorted(set(ranges)):
            result.append(dict(marker, timeline_start_seconds=float(a), timeline_end_seconds=float(b),
                               timeline_start_frame=math.floor(a * fps), timeline_end_frame=math.ceil(b * fps)))
    return result


def add_dialogue_markers(spine, config, cut, sound, record):
    fps = int(config['fps'])
    for row in dialogue_ranges(config, cut, sound):
        first, last = Fraction(row['timeline_start_frame'], fps), Fraction(row['timeline_end_frame'], fps)
        for clip in spine:
            if clip.tag not in ('clip', 'mc-clip', 'gap'):
                continue
            position = Fraction(clip.get('offset')[:-1]) - Fraction(record, fps)
            length = Fraction(clip.get('duration')[:-1])
            a, b = max(first, position), min(last, position + length)
            if b <= a:
                continue
            source = Fraction(clip.get('start')[:-1]) + a - position
            def seconds(value):
                return f'{value.numerator}/{value.denominator}s'
            label = row.get('script_text') or row.get('transcript_text', '')
            note = ' | '.join(str(x) for x in (row.get('script_scene'), row.get('script_speaker'),
                    row.get('session_name'), '台词ID:' + str(row.get('script_line_id', '')),
                    row.get('note'), ', '.join(row.get('tags', []))) if x)
            ET.SubElement(clip, 'marker', start=seconds(source), duration=seconds(b-a),
                          value=label[:120], note=note)


def export_library_manifest(config, events, sources, xml_path, sound=None):
    from cut_xml import timeline_info
    cut = timeline_info(config)
    payload = dict(version=1, context=config.get('library_context', {}),
                   xml_file=Path(xml_path).name, fps=config['fps'],
                   timeline_start_tc=config.get('timeline_start_tc'),
                   duration_frames=cut['duration'], sources=sources,
                   sync_signature=sync_signature(config,cut),
                   dialogue=dialogue_ranges(config, cut, sound),
                   events=events,
                   note='台词仅作分类标记；剪辑及同步仍使用本次拍摄的源入出点。')
    located={row.get('id') or row.get('transcript_id') for row in payload['dialogue']}
    payload['unlocated_dialogue']=[dict(row,reason='尚未建立此媒体与本场PGM的同步关系。') for row in config.get('dialogue_markers',[])
        if row.get('status')=='confirmed' and (row.get('id') or row.get('transcript_id')) not in located]
    target = Path(xml_path).parent / 'library_manifest.json'
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    return target
