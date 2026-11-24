"""Director lookup report. Dialogue correspondence is never cross-take sync."""
from __future__ import annotations

import csv
from fractions import Fraction as F
import json
import math
import os
from pathlib import Path

from camera_schema import cameras_for

class CorrespondenceError(ValueError):
    pass


def current_asr_signature(config, path, state, sound_index=None):
    """Select the saved sound source used by this ASR run, without media I/O."""
    from sound_fallback import fallback_eligible, sound_entry
    entry = (lambda field: sound_index.entry(field, path)) if sound_index is not None else (lambda field: sound_entry(config.get(field), path))
    if state.get('asr_audio_source') == 'pgm_reference':
        saved = entry('library_sound_fallback')
        eligible = sound_index.eligible(path) if sound_index is not None else fallback_eligible(config, path)
        if eligible and saved.get('status') == 'fallback':
            return saved.get('signature')
        return None
    return entry('library_sync').get('signature')


def pgm_reference_ranges(config, path, duration=None, sound_index=None):
    """Actual reference-audio decoding coverage recorded by the current ASR."""
    from sound_fallback import fallback_enabled, sound_entry
    if not fallback_enabled(config):
        return []
    entry = (lambda field: sound_index.entry(field, path)) if sound_index is not None else (lambda field: sound_entry(config.get(field), path))
    state = entry('library_asr')
    if not state.get('complete') or state.get('asr_audio_source') not in ('pgm_reference', 'mixed'):
        return []
    signature = current_asr_signature(config, path, state, sound_index)
    if not signature or state.get('sync_signature') != signature:
        return []
    if state.get('asr_audio_source') == 'pgm_reference':
        saved = entry('library_sound_fallback')
        recorded, original = state.get('source_stat') or {}, saved.get('source_stat') or {}
        if any(recorded.get(key) is None or recorded.get(key) != original.get(key) for key in ('size', 'mtime_ns')):
            return []
    intervals = []
    for row in state.get('pgm_reference_ranges') or []:
        try:
            first, last = F(str(row['start_seconds'])), F(str(row['end_seconds']))
            if not (math.isfinite(float(first)) and math.isfinite(float(last)) and 0 <= first < last):
                continue
            if duration is not None:
                last = min(last, duration)
            if first < last:
                intervals.append((first, last))
        except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
            continue
    return intervals


def filter_active_transcripts(data):
    """Hide superseded ASR runs without deleting transcripts/manual decisions.

    Only an explicit current-run ID list restricts a media file. Legacy media
    has no such list and remains visible. An empty list is meaningful: the
    current successful run found no utterances. Scope is immutable session ID
    and full source path, never a filename, camera, or dialogue similarity.
    Auto ASR must use the current sound-sync signature; explicitly manual
    source-local subtitles retain their positions when sound sync changes.
    """
    active = {}
    from sound_fallback import SoundStateIndex
    for session in data.get('sessions', []):
        config = session.get('config') or {}
        sound_index = SoundStateIndex(config)
        processing = config.get('library_processing') or {}
        for path, state in (config.get('library_asr') or {}).items():
            if isinstance(state, dict) and isinstance(state.get('active_transcript_ids'), (list, tuple, set)):
                identifiers = set(state['active_transcript_ids'])
                signature = current_asr_signature(config, path, state, sound_index)
                reference_current = bool(pgm_reference_ranges(config, path, sound_index=sound_index))
                if state.get('asr_audio_source') in ('pgm_reference', 'mixed') and not reference_current:
                    identifiers = set()
                if state.get('source_kind') != 'manual' and signature and state.get('sync_signature') and signature != state['sync_signature']:
                    # A newer sync may have succeeded while retranscription
                    # failed/cancelled. Its old ASR positions remain historical.
                    identifiers = set()
                failure = processing.get(path) or {}
                if (state.get('source_kind') != 'manual' and failure.get('status') == 'failed'
                        and failure.get('stage') == 'sync' and not reference_current):
                    identifiers = set()
                active[(session['id'], path)] = identifiers
        for path, failure in processing.items():
            state = (config.get('library_asr') or {}).get(path) or {}
            if (isinstance(failure, dict) and failure.get('status') == 'failed'
                    and failure.get('stage') == 'sync' and state.get('source_kind') != 'manual'
                    and not pgm_reference_ranges(config, path, sound_index=sound_index)):
                # A failed resync invalidates automatic source positions even
                # when the old database predates explicit ASR run identifiers.
                active[(session['id'], path)] = set()
    by_id = {row.get('id'): row for row in data.get('transcripts', [])}
    inactive_lines = {row.get('id') for row in data.get('scripts', []) if not row.get('active', True)}
    manually_classified = {}
    manual_confirmed = set()
    explicit_ranges = {}
    current_lines = {row.get('id'): row for row in data.get('scripts', []) if row.get('active', True)}
    for candidate in data.get('candidates', []):
        if (candidate.get('status') != 'confirmed' or not candidate.get('script_active', True)
                or candidate.get('script_line_id') in inactive_lines):
            continue
        source = by_id.get(candidate.get('transcript_id')) or candidate
        annotation = source.get('data') or {}
        if not isinstance(annotation, dict) or annotation.get('source_kind') != 'manual_visual':
            continue
        try:
            begin, end = float(source['start_seconds']), float(source['end_seconds'])
            if not (math.isfinite(begin) and math.isfinite(end) and 0 <= begin < end):
                continue
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        scope = (source.get('session_id'), source.get('media_path'))
        ids = annotation.get('manual_script_line_ids')
        if (annotation.get('manual_range_explicit') and isinstance(ids, list) and ids
                and all(isinstance(identity, str) and identity in current_lines for identity in ids)
                and ids[0] == candidate.get('script_line_id')
                and len({current_lines[identity].get('script_id', '') for identity in ids}) == 1):
            explicit_ranges.setdefault(scope, set()).add(tuple(ids))
        else:
            manually_classified.setdefault(scope, []).append((begin, end))
        manual_confirmed.add(candidate.get('transcript_id'))
    automatic_ranges = {}
    for row in data.get('transcripts', []) or data.get('candidates', []):
        annotation = row.get('data') or {}
        if isinstance(annotation, dict) and annotation.get('source_kind') == 'manual_visual':
            continue
        scope = (row.get('session_id'), row.get('media_path'))
        identity = row.get('transcript_id') or row.get('id')
        if scope in active and identity not in active[scope]:
            continue
        try:
            first, last = float(row['start_seconds']), float(row['end_seconds'])
            if math.isfinite(first) and math.isfinite(last) and 0 <= first < last:
                automatic_ranges.setdefault(scope, []).append((first, last))
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
    def visible(row, identity, source=None):
        source = source or row
        scope = (source.get('session_id') or row.get('session_id'),
                 source.get('media_path') or row.get('media_path'))
        # A manually classified visual interval has no ASR run. Its per-segment
        # annotation remains current when automatic transcription is retried,
        # empty, superseded or moved by a newly verified sound relationship.
        annotation = source.get('data') or {}
        if isinstance(annotation, dict) and annotation.get('source_kind') == 'manual_visual':
            if identity not in manual_confirmed:
                try:
                    first, last = float(source['start_seconds']), float(source['end_seconds'])
                    if 0 <= first < last and not _gaps(first, last, automatic_ranges.get(scope, [])):
                        # After undo, current speech becomes the review anchor.
                        # Do not count the superseded visual marker as a take.
                        return False
                except (KeyError, TypeError, ValueError, OverflowError):
                    pass
            return True
        # A later successful ASR run may subdivide an already classified visual
        # interval. Keep its original subtitles in the database, but do not add
        # another performance/review item for that wholly covered source range.
        try:
            begin, end = float(source['start_seconds']), float(source['end_seconds'])
            if 0 <= begin < end and any(first <= begin and end <= last
                                        for first, last in manually_classified.get(scope, [])):
                return False
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
        return scope not in active or identity in active[scope]
    result = dict(data)
    result['transcripts'] = [row for row in data.get('transcripts', []) if visible(row, row.get('id'))]
    confirmed_places = {}
    for row in data.get('candidates', []):
        if (row.get('status') == 'confirmed' and row.get('script_active', True)
                and row.get('script_line_id') not in inactive_lines):
            confirmed_places.setdefault(row.get('transcript_id'), set()).add(row.get('script_line_id'))
    result['candidates'] = [row for row in data.get('candidates', [])
                            if visible(row, row.get('transcript_id'), by_id.get(row.get('transcript_id')))
                            and not (row.get('status') == 'candidate' and
                                     any(place != row.get('script_line_id') for place in
                                         confirmed_places.get(row.get('transcript_id'), set())))]
    # A full-file manual range overrides its first anchor and narrative bounds,
    # but does not erase accurate ASR timing for the other baseline lines. Keep
    # those real source positions so alternatives can still align to them.
    def within_explicit_range(row):
        source = by_id.get(row.get('transcript_id')) or row
        annotation = source.get('data') or {}
        if isinstance(annotation, dict) and annotation.get('source_kind') == 'manual_visual':
            return True
        choices = explicit_ranges.get((source.get('session_id'), source.get('media_path')), set())
        if len(choices) != 1:
            return True
        ids = next(iter(choices))
        return row.get('script_line_id') in ids[1:]
    result['candidates'] = [row for row in result['candidates'] if within_explicit_range(row)]
    return result


POLICY = ('台词对应仅表示剧情／对白内容相关，不代表不同拍摄场次逐帧同步。'
          '同场其他机位按已登记源时码及机位补偿关联实际相交区间，缺失不猜配。'
          '秒数和帧号均为各源文件本地位置，OUT 为不含末帧／末样本的边界。'
          '视频帧 IN 向下、OUT 向上取整以覆盖对白；原时码只由实际源时码计算。'
          '代理映射按同场 PGM 源时码加机位补偿，声音映射仅用实际合板 placement。')


def _check(cancelled):
    if cancelled():
        raise CorrespondenceError('已取消导出查镜头对应表。')


def _fraction(value):
    result = F(str(value))
    if not math.isfinite(float(result)):
        raise ValueError('非有限数值')
    return result


def _positive_rate(value):
    try:
        rate = _fraction(value)
        return rate if rate > 0 else None
    except (ValueError, TypeError, ZeroDivisionError, OverflowError):
        return None


def _info(row):
    result = {}
    for field in ('meta', 'metadata', 'info', 'probe'):
        if isinstance(row.get(field), dict):
            result.update(row[field])
    result.update({key: value for key, value in row.items() if key not in ('meta', 'metadata', 'info', 'probe')})
    return result


class PGMInfoIndex:
    """Headers and sound decisions scoped to one immutable scene snapshot."""
    def __init__(self, config, sound_index=None):
        from sound_fallback import SoundStateIndex
        self.headers = {}
        self.sound = sound_index if sound_index is not None else SoundStateIndex(config)
        for row in config.get('library_media', []):
            if row.get('kind') == 'pgm' and row.get('path'):
                self.headers.setdefault(row['path'], {}).update(_info(row))
        for row in config.get('pgm_sources', []):
            if isinstance(row, dict) and row.get('path'):
                self.headers.setdefault(row['path'], {}).update(_info(row))


def _pgm_info(config, path, transcript, index=None):
    from sound_fallback import sound_entry, fallback_eligible
    sync = config.get('library_sync', {}).get(path, {})
    if index is not None:
        result = dict(index.headers.get(path, {}))
    else:
        result = {}
        for row in config.get('library_media', []):
            if row.get('kind') == 'pgm' and row.get('path') == path:
                result.update(_info(row))
        for row in config.get('pgm_sources', []):
            if isinstance(row, dict) and row.get('path') == path:
                result.update(_info(row))
    metadata = transcript.get('metadata') or {}
    for field in ('pgm_info', 'source_info'):
        if isinstance(metadata.get(field), dict):
            result.update(metadata[field])
    if isinstance(sync.get('pgm_info'), dict):
        result.update(sync['pgm_info'])
    fallback = (index.sound.entry('library_sound_fallback', path) if index is not None else
                sound_entry(config.get('library_sound_fallback'), path))
    if isinstance(fallback.get('pgm_info'), dict):
        result.update(fallback['pgm_info'])
    if config.get('pgm_timecodes', {}).get(path):
        result['start_tc'] = config['pgm_timecodes'][path]
    result['path'] = path
    processing = (config.get('library_processing') or {}).get(path) or {}
    if (index.sound.eligible(path) if index is not None else fallback_eligible(config,path)):
        sync = dict(sync,status='fallback',audio_source='pgm',pgm_path=path,pgm_info=result,
                    error=fallback.get('error') or processing.get('error',''),placements=[])
    elif processing.get('status') == 'failed' and processing.get('stage') == 'sync':
        # Preserve historical sync in the catalogue, but never export it as a
        # currently successful original-sound relationship after failed resync.
        sync = dict(sync, status='failed', error=processing.get('error', ''), placements=[])
    return result, sync


def _clock_start(tc, fps, warnings, label):
    if not tc:
        return None
    if fps is None or fps.denominator != 1:
        warnings.append(label + '：无法按当前帧率解释源时码，未伪造时码映射。')
        return None
    from core import tc_frames
    try:
        return tc_frames(str(tc), int(fps))
    except ValueError:
        warnings.append(label + '：源时码格式无效或不是整数非丢帧，未计算源时码。')
        return None


def _clock(frame, fps, warnings, label):
    if frame is None or fps is None or fps.denominator != 1:
        return ''
    from core import frames_tc
    try:
        return frames_tc(int(frame), int(fps))
    except ValueError:
        text = label + '：位置跨午夜或超出支持范围，源时码留空，未按零点循环。'
        if text not in warnings:
            warnings.append(text)
        return ''


def _gaps(begin, end, intervals):
    cursor, result = begin, []
    for first, last in sorted(intervals):
        first, last = max(begin, first), min(end, last)
        if last <= first:
            continue
        if first > cursor:
            result.append((cursor, first))
        cursor = max(cursor, last)
    if cursor < end:
        result.append((cursor, end))
    return result


def _video(config, path, transcript, begin, end, warnings, info_index=None):
    info, sync = _pgm_info(config, path, transcript, info_index)
    rate = _positive_rate(info.get('fps'))
    result = dict(path=path, filename=Path(path).name, fps=str(rate) if rate else '',
                  start_tc=str(info.get('start_tc') or ''), start_seconds=float(begin), end_seconds=float(end),
                  frame_in=None, frame_out_exclusive=None, source_tc_in='', source_tc_out_exclusive='',
                  total_frames=info.get('frames'), end_exclusive=True)
    if rate is None:
        warnings.append('PGM 缺少实际帧率，保留秒位置，未推算帧号和代理映射。')
        result['status'] = 'missing_fps'
        return result, None, None, sync
    first, last = math.floor(begin * rate), math.ceil(end * rate)
    result.update(frame_in=first, frame_out_exclusive=last,
                  frame_coverage_start_seconds=float(F(first, 1) / rate), frame_coverage_end_seconds=float(F(last, 1) / rate), status='located')
    try:
        total = int(info['frames'])
        if total <= 0:
            raise ValueError()
        result['total_frames'] = total
        if first >= total or last > total:
            result['status'] = 'outside_media'
            warnings.append('对白范围超出已登记 PGM 帧数，保留请求范围，请核对转录。')
    except (KeyError, TypeError, ValueError):
        result['total_frames'] = None
        warnings.append('PGM 缺少实际总帧数，无法核对文件末端。')
    clock = _clock_start(result['start_tc'], rate, warnings, 'PGM')
    result['source_clock_start_frame'] = clock
    if clock is None:
        if not result['start_tc']:
            warnings.append('PGM 缺少首帧源时码；本地秒位置不作为原时码。')
    else:
        result['source_tc_in'] = _clock(clock + first, rate, warnings, 'PGM IN')
        result['source_tc_out_exclusive'] = _clock(clock + last, rate, warnings, 'PGM OUT')
    return result, rate, clock, sync


def _proxies(config, pgm, rate, clock, warnings):
    # The unified registry is sufficient for catalogue lookup before an edit
    # timeline is loaded. Keep explicitly calibrated source rows authoritative.
    cameras = cameras_for(config)
    all_sources = list(config.get('sources', []))
    registered_proxies = {}
    for row in config.get('library_media', []):
        if row.get('kind') == 'proxy':
            registered_proxies.setdefault(row.get('path'), _info(row))
    known_paths = {row.get('proxy_path') or row.get('path') for row in all_sources}
    for row in config.get('library_media', []):
        if row.get('kind') != 'proxy' or row.get('path') in known_paths:
            continue
        source = _info(row)
        if str(source.get('camera', '')).upper() not in cameras:
            continue
        source['source_id'] = source.get('source_id') or source.get('id', '')
        all_sources.append(source)
        known_paths.add(source.get('path'))
    result = {}
    by_camera = {}
    for source in all_sources:
        by_camera.setdefault(str(source.get('camera', '')).upper(), []).append(source)
    for camera in cameras:
        sources = by_camera.get(camera, [])
        report = dict(camera=camera, offset_frames=0, calibration_confirmed=bool(config.get('calibration_confirmed')),
                      status='not_imported' if not sources else 'unmapped', segments=[], uncovered=[], unavailable_sources=[])
        result[camera] = report
        try:
            offset = config.get('camera_offsets', {}).get(camera, 0)
            if isinstance(offset, bool) or _fraction(offset).denominator != 1:
                raise ValueError()
            report['offset_frames'] = int(offset)
        except (ValueError, TypeError, ZeroDivisionError):
            report['status'] = 'invalid_offset'
            warnings.append(camera + ' 机位补偿不是整数帧，未猜测代理位置。')
            continue
        if not sources:
            continue
        if clock is None or rate is None or rate.denominator != 1 or pgm['frame_in'] is None:
            report['status'] = 'missing_pgm_clock'
            report['unavailable_sources'] = [dict(path=row.get('proxy_path') or row.get('path', ''), reason='PGM缺少可用源时码／帧率') for row in sources]
            continue
        target_in = clock + pgm['frame_in'] + report['offset_frames']
        target_out = clock + pgm['frame_out_exclusive'] + report['offset_frames']
        coverage = []
        for source in sources:
            info = _info(source)
            path = info.get('proxy_path') or info.get('path', '')
            registered = registered_proxies.get(path, {})
            # The unified importer stores actual probe data in the registry;
            # legacy source rows contain TC/frames but may have no fps at all.
            # Keep any explicitly checked source TC while using actual file fps.
            own_rate = _positive_rate(registered.get('fps')) or _positive_rate(info.get('fps'))
            rate_origin = 'registered'
            if own_rate is None:
                own_rate = _positive_rate(config.get('fps'))
                rate_origin = 'session_config'
            first_tc = str(info.get('start_tc') or registered.get('start_tc') or '')
            start = _clock_start(first_tc, own_rate, warnings, camera + '代理 ' + Path(path).name)
            try:
                frames = int(registered.get('frames', info.get('frames')))
                if frames <= 0:
                    raise ValueError()
            except (ValueError, TypeError):
                frames = 0
            if not path or start is None or own_rate != rate or not frames:
                report['unavailable_sources'].append(dict(path=path, reason='缺少代理时码／总帧数或帧率不一致'))
                continue
            a, b = max(target_in, start), min(target_out, start + frames)
            if b <= a:
                continue
            coverage.append((a, b))
            segment = dict(path=path, filename=Path(path).name, source_id=info.get('source_id', ''),
                           original_path=info.get('original_path', ''), fps=str(own_rate), fps_origin=rate_origin,
                           start_tc=first_tc, frame_in=a - start, frame_out_exclusive=b - start,
                           start_seconds=float(F(a - start, 1) / rate), end_seconds=float(F(b - start, 1) / rate),
                           source_tc_in=_clock(a, rate, warnings, camera + '代理 IN'),
                           source_tc_out_exclusive=_clock(b, rate, warnings, camera + '代理 OUT'),
                           pgm_frame_in=a - clock - report['offset_frames'],
                           pgm_frame_out_exclusive=b - clock - report['offset_frames'], end_exclusive=True,
                           ambiguous=False, calibration_confirmed=report['calibration_confirmed'])
            report['segments'].append(segment)
        report['segments'].sort(key=lambda value: (value['pgm_frame_in'], value['path']))
        segments = report['segments']
        furthest_end = -math.inf
        for i, segment in enumerate(segments):
            segment['ambiguous'] = (furthest_end > segment['pgm_frame_in'] or
                i + 1 < len(segments) and segments[i + 1]['pgm_frame_in'] < segment['pgm_frame_out_exclusive'])
            furthest_end = max(furthest_end, segment['pgm_frame_out_exclusive'])
        report['uncovered'] = [dict(pgm_frame_in=a - clock - report['offset_frames'],
                                     pgm_frame_out_exclusive=b - clock - report['offset_frames'])
                               for a, b in _gaps(target_in, target_out, coverage)]
        report['status'] = 'ambiguous' if any(row['ambiguous'] for row in segments) else (
            'mapped' if segments and not report['uncovered'] else 'partial' if segments else 'no_coverage')
        if not report['calibration_confirmed']:
            report['status'] += '_unverified'
        if report['unavailable_sources']:
            warnings.append(camera + ' 机部分代理缺少映射依据，详见 JSON。')
    return result


def _audio(sync, begin, end, rate, warnings):
    result = dict(sync_status=sync.get('status', 'missing'), segments=[], uncovered=[])
    if sync.get('status') == 'fallback':
        result['audio_source']='pgm'
        result['reference_audio']=dict(path=sync.get('pgm_path',''),start_seconds=float(begin),end_seconds=float(end))
        if not (sync.get('pgm_info') or {}).get('audio_channels'):
            result['uncovered']=[dict(start_seconds=float(begin),end_seconds=float(end))]
            warnings.append('PGM原声音轨尚未核对或不存在；没有虚构原始WAV对应关系。')
        return result
    if sync.get('status') != 'matched':
        result['uncovered'] = [dict(start_seconds=float(begin), end_seconds=float(end))]
        warnings.append('本条 PGM 尚无成功的声音合板结果，未推测 WAV 位置。')
        return result
    from core import AnalysisError
    from sound_pairs import group_sound_placements
    placements = [row for row in sync.get('placements', [])
                  if isinstance(row, dict) and row.get('status') not in ('unmatched', 'outside_pgm', 'failed')]
    invalid_pairs = set()
    try:
        groups = group_sound_placements(placements)
    except AnalysisError:
        # Keep listing each source for manual lookup, but never let malformed
        # paired rows claim verified coverage. Independent valid groups survive.
        warnings.append('左右声道配对缺失、重复或区间不一致，未将其视为完整声音覆盖。')
        buckets, groups = {}, []
        for placement in placements:
            pair_id = str(placement.get('pair_id') or '').strip()
            if pair_id:
                buckets.setdefault(pair_id, []).append(placement)
            else:
                groups.append([placement])
        for bucket in buckets.values():
            try:
                groups.extend(group_sound_placements(bucket))
            except AnalysisError:
                invalid_pairs.update(id(row) for row in bucket)
                groups.extend([row] for row in bucket)
    group_for = {id(placement): number for number, group in enumerate(groups) for placement in group}
    grouped_segments, group_coverage = {}, {}
    for placement in placements:
        path = placement.get('wav_path')
        info = placement.get('wav_info') or {}
        try:
            start = _fraction(placement['pgm_start_seconds'])
            finish = _fraction(placement['pgm_end_seconds'])
            sound_start = _fraction(placement['wav_start_seconds'])
            if finish <= start or sound_start < 0 or not path:
                raise ValueError()
            offset = _fraction(placement.get('offset_seconds', start - sound_start))
            sample_rate = int(info.get('sample_rate', 0))
            if sample_rate <= 0 or abs(start - sound_start - offset) > F(1, sample_rate):
                raise ValueError()
            a, b = max(begin, start), min(end, finish)
            if b <= a:
                continue
            sound_in, sound_out = sound_start + a - start, sound_start + b - start
            if placement.get('wav_end_seconds') is not None and sound_out > _fraction(placement['wav_end_seconds']) + F(1, sample_rate):
                raise ValueError()
            first, last = math.floor(sound_in * sample_rate), math.ceil(sound_out * sample_rate)
            if info.get('samples') is not None and last > int(info['samples']):
                # Floats serialized from rational sample times can land a tiny
                # fraction past the true last sample; never invent that sample.
                if sound_out - F(int(info['samples']), sample_rate) > F(1, 10**9):
                    raise ValueError()
                last = int(info['samples'])
        except (KeyError, ValueError, TypeError, ZeroDivisionError, OverflowError):
            warnings.append('某条 WAV placement 信息不完整或相互矛盾，未猜测其位置：' + str(path or '未知文件'))
            continue
        source_start = None
        if info.get('has_timecode'):
            try:
                source_start = int(info.get('start_samples'))
            except (TypeError, ValueError):
                clock = _clock_start(info.get('start_tc'), rate, warnings, 'WAV ' + Path(path).name)
                if clock is not None:
                    source_start = F(clock * sample_rate, int(rate))
        segment = dict(path=path, filename=info.get('filename') or Path(path).name,
                       pgm_start_seconds=float(a), pgm_end_seconds=float(b),
                       start_seconds=float(sound_in), end_seconds=float(sound_out),
                       sample_rate=sample_rate, sample_in=first, sample_out_exclusive=last,
                       start_tc=str(info.get('start_tc') or '') if info.get('has_timecode') else '',
                       source_tc_in='', source_tc_out_exclusive='', source_start_samples=source_start,
                       method=placement.get('method', ''), score=placement.get('score'),
                       sound_roll=info.get('sound_roll', ''), recording_date=info.get('recording_date', ''),
                       end_exclusive=True)
        if placement.get('pair_id'):
            segment.update(pair_id=placement['pair_id'], channel=placement.get('channel', ''))
        if source_start is not None and rate is not None and rate.denominator == 1:
            segment['source_sample_in'] = source_start + first
            segment['source_sample_out_exclusive'] = source_start + last
            segment['source_tc_in'] = _clock(math.floor(F(source_start + first) * rate / sample_rate), rate, warnings, 'WAV IN')
            segment['source_tc_out_exclusive'] = _clock(math.ceil(F(source_start + last) * rate / sample_rate), rate, warnings, 'WAV OUT')
        result['segments'].append(segment)
        grouped_segments.setdefault(group_for[id(placement)], []).append(segment)
        group_coverage.setdefault(group_for[id(placement)], (a, b))
    valid_groups = {number for number, segments in grouped_segments.items()
                    if len(segments) == len(groups[number]) and
                    not any(id(row) in invalid_pairs for row in groups[number])}
    coverage = [group_coverage[number] for number in valid_groups]
    result['segments'].sort(key=lambda value: (value['pgm_start_seconds'], value['path']))
    result['uncovered'] = [dict(start_seconds=float(a), end_seconds=float(b)) for a, b in _gaps(begin, end, coverage)]
    if result['uncovered']:
        warnings.append('对白区间存在原始 WAV 未覆盖部分，详见音频缺口。')
    segment_groups = {id(segment): number for number, segments in grouped_segments.items() for segment in segments}
    for segment in result['segments']:
        number = segment_groups[id(segment)]
        segment['ambiguous'] = number not in valid_groups or any(
            min(segment['pgm_end_seconds'], other['pgm_end_seconds']) >
            max(segment['pgm_start_seconds'], other['pgm_start_seconds'])
            for other in result['segments'] if segment_groups[id(other)] != number)
    if any(row['ambiguous'] for row in result['segments']):
        warnings.append('多个 WAV placement 重叠，全部列出供核对，未自动选择。')
    return result


def _proxy_text(report):
    parts = []
    for row in report['segments']:
        flags = '【重叠待核对】' if row['ambiguous'] else ''
        if not row['calibration_confirmed']:
            flags += '【同步未核对】'
        parts.append(f"{flags}{row['filename']} | {row['path']} | 帧[{row['frame_in']},{row['frame_out_exclusive']}) | "
                     f"源TC[{row['source_tc_in'] or '缺失'},{row['source_tc_out_exclusive'] or '缺失'})")
    return '\n'.join(parts) if parts else report['status']


def _audio_text(report):
    return '\n'.join(f"{'【重叠待核对】' if row.get('ambiguous') else ''}{row['filename']} | {row['path']} | 本地秒[{row['start_seconds']},{row['end_seconds']}) | "
                     f"样本[{row['sample_in']},{row['sample_out_exclusive']}) | "
                     f"源TC[{row['source_tc_in'] or '缺失'},{row['source_tc_out_exclusive'] or '缺失'})"
                     for row in report['segments'])


def _cell(value):
    text = '' if value is None else str(value)
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) else text


def _csv_row(row, cameras=None):
    pgm, script, match, session = row['pgm'], row['script'], row['match'], row['session']
    return {
        '台本顺序': script['order'], '台本ID': script['id'], '幕场': script['scene'], '人物': script['speaker'], '台本台词': script['text'],
        '匹配状态': '已确认' if match['status'] == 'confirmed' else '未核实候选', '匹配分数': match.get('score'),
        '匹配依据': match.get('reason', ''), '人工备注': match.get('note', ''),
        '拍摄场次': session['name'], '拍摄场次ID': session['id'], '实际台词': row['transcript']['text'],
        '转录ID': row['transcript']['id'], '候选ID': match['id'], 'PGM文件名': pgm['filename'], 'PGM完整路径': pgm['path'],
        'PGM帧率': pgm['fps'], 'PGM总帧数': pgm['total_frames'], 'PGM首帧源时码': pgm['start_tc'],
        'PGM本地IN秒': pgm['start_seconds'], 'PGM本地OUT秒(不含)': pgm['end_seconds'],
        'PGM帧IN': pgm['frame_in'], 'PGM帧OUT(不含)': pgm['frame_out_exclusive'],
        'PGM源时码IN': pgm['source_tc_in'], 'PGM源时码OUT(不含)': pgm['source_tc_out_exclusive'],
        'PGM范围状态': pgm['status'],
        **{camera + '机代理': _proxy_text(row['proxies'][camera]) if camera in row['proxies'] else ''
           for camera in (cameras if cameras is not None else row['proxies'])},
        '原始WAV对应位置': _audio_text(row['audio']), '声音合板状态': row['audio']['sync_status'],
        '声音未覆盖PGM秒范围': '; '.join(f"[{gap['start_seconds']},{gap['end_seconds']})" for gap in row['audio']['uncovered']),
        '字幕文件': row.get('subtitle_path', ''), '警告': '；'.join(row['warnings']), '位置说明': POLICY,
    }


def _json_default(value):
    if isinstance(value, F):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError('报告中存在不可序列化字段：' + type(value).__name__)


def export_correspondence(data, output_path, include_candidates=False, *,
                          progress=lambda number, message: None, cancelled=lambda: False):
    """Export active confirmed dialogue placements to BOM CSV plus full JSON.

    Source timing comes solely from registered media metadata and actual sound
    sync placements. No media is probed or modified during this read-only export.
    """
    _check(cancelled)
    data = filter_active_transcripts(data)
    output = Path(output_path).expanduser().resolve()
    if output.suffix.lower() not in ('', '.csv', '.json'):
        raise CorrespondenceError('对应表输出位置必须为 CSV 或 JSON，不能覆盖媒体文件。')
    csv_path, json_path = output.with_suffix('.csv'), output.with_suffix('.json')
    if csv_path.exists() or json_path.exists():
        raise CorrespondenceError('对应表文件已存在，请选择新的输出名称。')
    sessions = {row['id']: row for row in data.get('sessions', [])}
    scripts = {row['id']: row for row in data.get('scripts', []) if row.get('active', row.get('script_active', 1))}
    transcripts = {row['id']: row for row in data.get('transcripts', [])}
    statuses = {'confirmed', 'candidate'} if include_candidates else {'confirmed'}
    candidates = [row for row in data.get('candidates', []) if row.get('status') in statuses and
                  row.get('script_active', 1) and row.get('script_line_id') in scripts]
    if not candidates:
        raise CorrespondenceError('暂无已确认的有效台词对应；请先人工确认，或选择包含未核实候选。')
    records = []
    for index, candidate in enumerate(candidates):
        _check(cancelled)
        script = scripts[candidate['script_line_id']]
        transcript = transcripts.get(candidate.get('transcript_id')) or candidate
        session = sessions.get(transcript.get('session_id') or candidate.get('session_id'))
        if session is None:
            raise CorrespondenceError('候选缺少拍摄场次，无法输出可靠对应：' + str(candidate.get('id')))
        path = transcript.get('media_path') or candidate.get('media_path')
        if not path:
            raise CorrespondenceError('候选缺少完整源文件路径：' + str(candidate.get('id')))
        try:
            begin, end = _fraction(transcript['start_seconds']), _fraction(transcript['end_seconds'])
            if begin < 0 or end <= begin:
                raise ValueError()
        except (KeyError, ValueError, TypeError, ZeroDivisionError, OverflowError) as exc:
            raise CorrespondenceError('转录源本地起止位置无效：' + str(candidate.get('transcript_id'))) from exc
        warnings = []
        config = session.get('config') or {}
        pgm, rate, clock, sync = _video(config, str(path), transcript, begin, end, warnings)
        proxies = _proxies(config, pgm, rate, clock, warnings)
        audio = _audio(sync, begin, end, rate, warnings)
        metadata = transcript.get('metadata') or {}
        records.append(dict(script=dict(id=script['id'], order=script.get('order', script.get('line_order', 0)),
            scene=script.get('scene', candidate.get('script_scene', '')), speaker=script.get('speaker', candidate.get('script_speaker', '')),
            text=script.get('text', candidate.get('script_text', '')), title=script.get('title', candidate.get('script_title', '')),
            version=script.get('version', candidate.get('script_version'))),
            match={key: candidate.get(key) for key in ('id', 'status', 'score', 'reason', 'note', 'tags')},
            session=dict(id=session['id'], name=session.get('name', candidate.get('session_name', ''))),
            transcript=dict(id=candidate.get('transcript_id', transcript.get('id')),
                            text=transcript.get('text', candidate.get('transcript_text', ''))),
            pgm=pgm, proxies=proxies, audio=audio,
            subtitle_path=metadata.get('subtitle_path', candidate.get('subtitle_path', '')), warnings=list(dict.fromkeys(warnings))))
        progress(round((index + 1) * 80 / len(candidates)), '建立导演查镜头对应表')
    session_order = {row['id']: index for index, row in enumerate(data.get('sessions', []))}
    def source_order(row):
        pgm = row['pgm']
        rate = _positive_rate(pgm['fps'])
        clock = pgm.get('source_clock_start_frame')
        if clock is not None and rate:
            return (0, F(clock, 1) / rate + _fraction(pgm['start_seconds']), pgm['path'])
        session = sessions[row['session']['id']]
        paths = [value.get('path') for value in session.get('config', {}).get('library_media', []) if value.get('kind') == 'pgm']
        return (1, paths.index(pgm['path']) if pgm['path'] in paths else len(paths), pgm['path'], pgm['start_seconds'])
    records.sort(key=lambda row: (int(row['script']['order']), str(row['script']['id']),
                                 session_order.get(row['session']['id'], 0), source_order(row), str(row['match']['id'])))
    cameras = sorted({camera for row in records for camera in row['proxies']})
    table = [_csv_row(row, cameras) for row in records]
    used_sessions = {row['session']['id'] for row in records}
    used_scripts = {row['script']['id'] for row in records}
    used_transcripts = {row['transcript']['id'] for row in records}
    document = dict(version=1, policy=POLICY, include_candidates=bool(include_candidates), count=len(records), records=records,
                    catalogue_snapshot=dict(sessions=[row for row in data.get('sessions', []) if row['id'] in used_sessions],
                        scripts=[row for row in data.get('scripts', []) if row['id'] in used_scripts],
                        transcripts=[row for row in data.get('transcripts', []) if row['id'] in used_transcripts], candidates=candidates))
    _check(cancelled)
    created = []
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with csv_path.open('x', encoding='utf-8-sig', newline='') as stream:
            created.append(csv_path)
            writer = csv.DictWriter(stream, fieldnames=list(table[0])); writer.writeheader()
            for row in table:
                _check(cancelled)
                writer.writerow({key: _cell(value) for key, value in row.items()})
        _check(cancelled)
        progress(90, '保存完整 JSON 对应关系')
        with json_path.open('x', encoding='utf-8', newline='\n') as stream:
            created.append(json_path)
            for number, chunk in enumerate(json.JSONEncoder(ensure_ascii=False, indent=2, default=_json_default, allow_nan=False).iterencode(document)):
                if number % 100 == 0:
                    _check(cancelled)
                stream.write(chunk)
            stream.write('\n')
        _check(cancelled)
    except Exception as exc:
        for path in created:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        if isinstance(exc, CorrespondenceError):
            raise
        raise CorrespondenceError('对应表导出失败，已清理本次未完成文件：' + str(exc)) from exc
    progress(100, '导演查镜头对应表已导出')
    return dict(paths={'csv': str(csv_path), 'json': str(json_path)}, csv_path=str(csv_path), json_path=str(json_path),
                count=len(records), warnings=sorted({warning for row in records for warning in row['warnings']}))
