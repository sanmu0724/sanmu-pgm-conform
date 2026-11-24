"""Build edit audio from the one registered sync result, without rematching."""
from __future__ import annotations

import copy
from fractions import Fraction as F
from pathlib import Path

from core import AnalysisError, probe, tc_frames, validate_rate
from library_bridge import resolve_local_path
from sound_fallback import (AudioSources, SoundStateIndex,is_original_wav_source,
                            sound_path_key_factory,source_start,valid_completed_sync_gap,wav_only_sound_plan)


def _pgm_audio_source(block, fps):
    """Reference the real container; never extract or relabel it as a WAV."""
    import av
    path = str(Path(resolve_local_path(block['path'])).expanduser().resolve())
    info = probe(path)
    validate_rate(info, fps)
    if int(info['frames']) != int(block['frames']):
        raise AnalysisError('PGM实际长度与登记声音区间不一致：' + path)
    if not info.get('audio_channels'):
        return None
    with av.open(path) as container:
        video = container.streams.video[0]
        audio = container.streams.audio[0]
        if audio.duration is None or audio.time_base is None:
            raise AnalysisError('无法确定PGM内嵌声音的实际覆盖长度：' + path)
        video_origin = F(video.start_time or 0) * video.time_base
        first = F(audio.start_time or 0) * audio.time_base - video_origin
        last = first + F(audio.duration) * audio.time_base
        channels = audio.codec_context.channels
        rate = audio.codec_context.sample_rate
    tc = block.get('start_tc') or info.get('start_tc')
    return dict(path=path, filename=Path(path).name, source_kind='pgm', channel='main',
                original_audio_filename=Path(path).name, fps=fps, frames=info['frames'],
                start_frames=tc_frames(tc, fps) if tc else 0, has_timecode=bool(tc),
                channels=channels, sample_rate=rate, sync_report=[],
                audio_first_seconds=str(max(F(0), first)),
                audio_end_seconds=str(min(F(info['frames'], fps), last)))


def registered_sound_plan(config, cut, progress=lambda n,m: None, cancelled=lambda: False):
    from library_sync import SyncValidationIndex, valid_sync
    from sound_sync import probe_wav, SAMPLE_RATE
    from sound_pairs import group_sound_placements
    fps = int(config['fps'])
    wavs, events, reports = {}, [], []
    key = sound_path_key_factory()
    sound_index = SoundStateIndex(config, key)
    validation_index = SyncValidationIndex(config)
    for number, block in enumerate(cut['pgm_sources'], 1):
        if cancelled():
            raise AnalysisError('已取消导出。')
        state = sound_index.entry('library_sync', block['path'])
        failed = sound_index.entry('library_processing', block['path'])
        sync_failed = failed.get('status') in ('failed', 'fallback') and failed.get('stage') == 'sync'
        matched = not sync_failed and valid_sync(config, block['path'], state, index=validation_index)
        completed_gap = valid_completed_sync_gap(config,block['path'],index=validation_index) if not matched else False
        if not matched and not completed_gap:
            raise AnalysisError('本场声音合板未完成或已失效，请回到统一“声音合板”入口处理：'
                                + Path(block['path']).name)
        origin = F(block['start'], fps)
        end = F(block['frames'], fps)
        intervals = []
        items = [item for item in state.get('placements',[]) if is_original_wav_source(
            dict(path=item.get('wav_path',''),source_kind=item.get('source_kind','wav')))] if matched else []
        coverage_members = {id(group[0]) for group in group_sound_placements(items)}
        for item in items:
            path = str(Path(resolve_local_path(item['wav_path'])).resolve())
            if path not in wavs:
                info = probe_wav(path, fps=fps)
                if int(info['sample_rate']) != SAMPLE_RATE:
                    raise AnalysisError('原始主声音必须为48kHz：' + path)
                wavs[path] = dict(info, channel='main', source_kind='wav', sync_report=[])
            wav = wavs[path]
            if item.get('pair_id'):
                wav.update(channel=item['channel'], pair_id=item['pair_id'])
            first = F(str(item['pgm_start_seconds'])).limit_denominator(SAMPLE_RATE)
            last = F(str(item['pgm_end_seconds'])).limit_denominator(SAMPLE_RATE)
            source = F(str(item['wav_start_seconds'])).limit_denominator(SAMPLE_RATE)
            a, b = max(F(0), first), min(end, last)
            local = source + a-first
            if (b <= a or local < 0 or local+b-a > F(wav['samples'], SAMPLE_RATE)):
                raise AnalysisError('已登记声音区间超出原始WAV，请重新核对合板：' + path)
            if id(item) in coverage_members:
                intervals.append((a,b))
            start = source_start(wav) + local
            event = dict(path=path, channel='main', source_kind='wav', pgm_path=block['path'], record_start=origin+a,
                         source_start=start, duration=b-a, src_channel='1,2' if wav['channels'] == 2 else 1,
                         method=item.get('method',''), score=item.get('score'),
                         offset_seconds=float(first-source))
            events.append(event)
            if item.get('pair_id'):
                event.update(channel=item['channel'], pair_id=item['pair_id'],
                    out_channel=item.get('out_channel') or item['channel'],
                    alignment_source_path=item.get('alignment_source_path', ''))
            wav['sync_report'].append(dict(status='matched', source_kind='wav', pgm_path=block['path'], path=path,
                record_start_seconds=float(origin+a), duration_seconds=float(b-a),
                pgm_start_seconds=float(a), pgm_end_seconds=float(b),
                source_start_seconds=float(start), source_local_seconds=float(local),
                method=event['method'], score=event['score']))
        gaps, cursor = [], F(0)
        for first,last in sorted(intervals):
            if first < cursor:
                raise AnalysisError('已登记主声音存在重叠，请在统一声音合板中核对。')
            if first > cursor:
                gaps.append((cursor, first))
            cursor = last
        if cursor < end:
            gaps.append((cursor, end))
        reason = failed.get('error') or ('原始主WAV没有覆盖此区间' if matched else '原始WAV匹配失败')
        for first, last in gaps:
            if last>first:
                reports.append(dict(status='uncovered',source_kind='none',pgm_path=block['path'],
                    pgm_start_seconds=float(first),pgm_end_seconds=float(last),
                    record_start_seconds=float(origin+first),duration_seconds=float(last-first),
                    reason=reason+'；音频留空。'))
        progress(round(number/len(cut['pgm_sources'])*100), '沿用声音结果：' + Path(block['path']).name)
    if wavs:
        next(iter(wavs.values()))['sync_report'].extend(copy.deepcopy(reports))
    events.sort(key=lambda row:(row['record_start'],row['path']))
    return wav_only_sound_plan((AudioSources(wavs.values(),unattached_reports=reports if not wavs else ()),events),cut,fps)
