"""One preparation gate before dialogue recognition and multicam export.

The overview uses only registered headers and saved decisions. Strict checks
read the actual cut XML and validate the existing sound cache; neither mode
decodes frames, recognizes dialogue, or performs another sound alignment.
"""
from __future__ import annotations

from fractions import Fraction
from bisect import bisect_right
from functools import lru_cache
import copy
import hashlib
import json
import posixpath
import sys


class WorkflowReadinessError(ValueError):
    pass


_VIDEO_SUFFIXES = frozenset({'.mov', '.mp4', '.mxf', '.m4v'})


def _integer(value):
    try:
        number = Fraction(str(value))
        return int(number) if number.denominator == 1 and not isinstance(value, bool) else None
    except (ValueError, TypeError, ZeroDivisionError, OverflowError):
        return None


def _path_key_factory():
    """Use explicit mount mappings and lexical OS aliases, without stat calls."""
    from library_bridge import read_settings, remap_path
    def lexical(value):
        if not isinstance(value, str) or not value:
            return ''
        value = posixpath.normpath(value)
        if sys.platform == 'darwin':
            for alias in ('/tmp', '/var', '/etc'):
                if value == alias or value.startswith(alias+'/'):
                    return '/private'+value
        return value
    mappings = [dict(shared=lexical(row.get('shared')), local=lexical(row.get('local')))
                for row in read_settings().get('path_mappings', [])]
    @lru_cache(maxsize=None)
    def key(value):
        value = lexical(value)
        return lexical(remap_path(value, mappings, reverse=True)) if value else ''
    return key


def _paths(config, key=None):
    key = key or _path_key_factory()
    found, seen = [], set()
    def add(value):
        if isinstance(value, str) and value and posixpath.splitext(value)[1].lower() in _VIDEO_SUFFIXES:
            identity = key(value)
            if identity not in seen:
                found.append(identity)
                seen.add(identity)
        elif isinstance(value, dict):
            add(value.get('path'))
        elif isinstance(value, (list, tuple)):
            for child in value:
                add(child)
    add(config.get('pgm'))
    add(config.get('pgm_sources', []))
    for path in config.get('pgm_timecodes', {}):
        add(path)
    for path in config.get('pgm_overrides', {}).values():
        add(path)
    for row in config.get('library_media', []):
        if isinstance(row, dict) and row.get('kind') == 'pgm':
            add(row)
    return found


def _registered_duration(config, paths, key=None):
    """Sum each PGM once, including headers saved by the unified sound sync.

    Older scene snapshots have no probe in ``library_media`` and no global
    ``pgm_sources``. Their completed sync stores the actual PGM frame count in
    ``library_sync[path].pgm_info``. Block start/end belong to an XML's record
    coordinates; they must not be mistaken for source duration or source TC.
    """
    key = key or _path_key_factory()
    headers = {}
    def add(path, row):
        if not isinstance(row, dict):
            return
        frames = _integer(row.get('frames'))
        identity = key(path)
        if identity and frames is not None and frames > 0:
            headers.setdefault(identity, set()).add(frames)
    for row in config.get('pgm_sources') or []:
        if isinstance(row, dict):
            add(row.get('path'), row)
    for row in config.get('library_media', []):
        if isinstance(row, dict) and row.get('kind') == 'pgm':
            for field in ('probe', 'metadata', 'info'):
                if isinstance(row.get(field), dict):
                    add(row.get('path'), row[field])
            add(row.get('path'), row)
    if isinstance(config.get('pgm_info'), dict):
        row = config['pgm_info']
        add(row.get('path') or config.get('pgm'), row)
    # Do not learn a different PGM from a stale or foreign sound-cache header.
    # The dictionary key and the embedded header must identify the same file.
    for path, state in (config.get('library_sync') or {}).items():
        if isinstance(state, dict) and state.get('status') == 'matched':
            info = state.get('pgm_info')
            if isinstance(info, dict) and (not info.get('path') or key(info['path']) == key(path)):
                add(path, info)
    counts = [headers.get(path, set()) for path in paths]
    return sum(next(iter(values)) for values in counts) if counts and all(len(values) == 1 for values in counts) else None


def _sound_state(config, path, key, index=None):
    """Read one cache through explicit aliases; never choose conflicting maps."""
    found = []
    rows = (index.aliases('library_sync', path) if index is not None else
            [state for candidate, state in (config.get('library_sync') or {}).items() if key(candidate) == path])
    for state in rows:
        if not isinstance(state, dict):
            continue
        normalized = copy.deepcopy(state)
        for row in normalized.get('placements') or []:
            if isinstance(row, dict) and isinstance(row.get('wav_path'), str):
                row['wav_path'] = key(row['wav_path'])
        if isinstance(normalized.get('pgm_info'), dict) and normalized['pgm_info'].get('path'):
            normalized['pgm_info']['path'] = key(normalized['pgm_info']['path'])
        for field in ('dependencies', 'candidate_dependencies'):
            for row in normalized.get(field) or []:
                if isinstance(row, dict) and isinstance(row.get('path'), str):
                    row['path'] = key(row['path'])
        found.append(normalized)
    if found and all(row == found[0] for row in found[1:]):
        return found[0]
    return {}


def _registered_pgm_audio(config, path, key, headers=None):
    """Return an audio-presence decision using saved headers only.

    Missing audio metadata is different from a video with no audio streams.
    Conflicting headers need a strict check, rather than choosing whichever
    catalogue record happened to be encountered first.
    """
    decisions = set()
    def add(candidate, row):
        if key(candidate) != path or not isinstance(row, dict):
            return
        if 'audio_channels' in row:
            channels = row['audio_channels']
            if isinstance(channels, (list, tuple)):
                decisions.add(bool(channels))
            elif _integer(channels) is not None and _integer(channels) >= 0:
                decisions.add(_integer(channels) > 0)
        if isinstance(row.get('has_audio'), bool):
            decisions.add(row['has_audio'])
    if headers is not None:
        for row in headers.get(path, ()):
            add(path, row)
        return next(iter(decisions)) if len(decisions) == 1 else None
    for row in config.get('pgm_sources') or []:
        if isinstance(row, dict):
            add(row.get('path'), row)
    for row in config.get('library_media') or []:
        if isinstance(row, dict) and row.get('kind') == 'pgm':
            add(row.get('path'), row)
            for field in ('probe', 'metadata', 'info'):
                add(row.get('path'), row.get(field))
    info = config.get('pgm_info')
    if isinstance(info, dict):
        add(info.get('path') or config.get('pgm'), info)
    for candidate, state in (config.get('library_sync') or {}).items():
        if isinstance(state, dict):
            info = state.get('pgm_info')
            if isinstance(info, dict) and (not info.get('path') or key(info['path']) == key(candidate)):
                add(candidate, info)
    for field in ('library_processing', 'library_sound_fallback'):
        for candidate, row in (config.get(field) or {}).items():
            if isinstance(row, dict) and row.get('stage') == 'sync':
                add(candidate, row)
                add(candidate, row.get('pgm_info'))
    return next(iter(decisions)) if len(decisions) == 1 else None


def _audio_header_index(config, key):
    """Gather saved audio-presence headers once, preserving every conflict."""
    headers = {}
    def add(path, row):
        if isinstance(row, dict):
            headers.setdefault(key(path), []).append(row)
    for row in config.get('pgm_sources') or []:
        if isinstance(row, dict):
            add(row.get('path'), row)
    for row in config.get('library_media') or []:
        if isinstance(row, dict) and row.get('kind') == 'pgm':
            add(row.get('path'), row)
            for field in ('probe', 'metadata', 'info'):
                add(row.get('path'), row.get(field))
    info = config.get('pgm_info')
    if isinstance(info, dict):
        add(info.get('path') or config.get('pgm'), info)
    for path, state in (config.get('library_sync') or {}).items():
        if isinstance(state, dict):
            info = state.get('pgm_info')
            if isinstance(info, dict) and (not info.get('path') or key(info['path']) == key(path)):
                add(path, info)
    for field in ('library_processing', 'library_sound_fallback'):
        for path, row in (config.get(field) or {}).items():
            if isinstance(row, dict) and row.get('stage') == 'sync':
                add(path, row)
                add(path, row.get('pgm_info'))
    return headers


def _pending_picture(index, row, config, key, cut=None, message=''):
    first, last = _integer(row.get('start')), _integer(row.get('end'))
    location = dict(shot_index=index+1, start=first, end=last,
                    camera=row.get('camera', ''), reason=row.get('reason', ''),
                    message=message, pgm_path='', pgm='', pgm_frame=None)
    # A saved source list has explicit file order. Rebuild its record cursor
    # rather than reading media or assuming that source TC is continuous.
    blocks = (cut or {}).get('pgm_sources') or config.get('pgm_sources') or []
    cursor = 0
    for block in blocks:
        if not isinstance(block, dict):
            return location
        frames = _integer(block.get('frames'))
        if frames is None or frames <= 0:
            return location
        if first is not None and cursor <= first < cursor+frames:
            path = key(block.get('path'))
            location.update(pgm_path=path, pgm=path, pgm_frame=first-cursor)
            break
        cursor += frames
    return location


def _no_recorded_camera(config, cut, cameras):
    """A conservative, header-only predicate for silent/black pre/post-roll.

    Any recorded range counts, including ambiguous overlaps. Missing or bad
    headers never authorize an unknown identity. The export probes again and
    makes its final decision from actual source coverage.
    """
    if (config.get('missing_camera_policy', 'available') != 'available'
            or config.get('empty_camera_policy', 'gap') not in ('gap','pgm')):
        return lambda first,last: False
    try:
        from core import tc_frames
        fps=int(config['fps']);sources=config.get('sources') or []
        if not sources:return lambda first,last: False
        intervals=[]
        for row in sources:
            if row.get('camera') not in cameras:raise ValueError('invalid camera')
            count=_integer(row.get('frames'))
            if count is None or count<=0:raise ValueError('missing length')
            start=tc_frames(row['start_tc'],fps)-int(config.get('camera_offsets',{}).get(row['camera'],0))
            intervals.append((start,start+count))
        merged=[]
        for start,end in sorted(intervals):
            if merged and start<=merged[-1][1]:merged[-1]=(merged[-1][0],max(end,merged[-1][1]))
            else:merged.append((start,end))
        begins=[start for start,end in merged]
        blocks=(cut or {}).get('pgm_sources') or config.get('pgm_sources') or []
        clocks=[];cursor=0
        for block in blocks:
            count=_integer(block.get('frames'))
            if count is None or count<=0:raise ValueError('missing PGM length')
            clocks.append((cursor,cursor+count,tc_frames(block['start_tc'],fps)))
            cursor+=count
        if not clocks:raise ValueError('missing PGM clocks')
        starts=[row[0] for row in clocks]
    except (ValueError,TypeError,KeyError,OverflowError):
        return lambda first,last: False
    def uncovered(first,last):
        if first is None or last is None or first<0 or last<=first or last>cursor:return False
        position=first
        while position<last:
            number=bisect_right(starts,position)-1
            base,end,clock=clocks[number]
            duration=min(last,end)-position
            source_start=clock+position-base;source_end=source_start+duration
            index=bisect_right(begins,source_start)-1
            if index>=0 and merged[index][1]>source_start:return False
            if index+1<len(merged) and merged[index+1][0]<source_end:return False
            position+=duration
        return True
    return uncovered


def session_readiness(session, strict=False, cut=None):
    """Return stable status fields for the UI, or perform a strict preflight.

    Light ``ready`` means the saved preparation is complete, rather than a
    claim that mounted files have just been verified. ``verified`` distinguishes
    a strict preflight from the inexpensive catalogue overview.
    """
    config = session.get('config') or {}
    key = _path_key_factory()
    paths = _paths(config, key)
    xml_paths = config.get('cut_xml_paths') or ([config['cut_xml']] if config.get('cut_xml') else [])
    issues = []
    result = dict(session_id=session.get('id'), session_name=session.get('name', ''),
                  cut_xml_ready=bool(paths and xml_paths), camera_ready=False,
                  calibration_ready=config.get('calibration_confirmed') is True,
                  sound_ready=False, pgm_count=len(paths), duration_frames=None,
                  camera_total=0, camera_confirmed=0, camera_pending=0, camera_pending_rows=[],
                  sound_total=len(paths), sound_matched=0, sound_pending=len(paths), sound_pending_rows=[],
                  sound_fallback=0, sound_fallback_rows=[], sound_gap=0, sound_gap_rows=[],
                  verified=bool(strict), ready=False, ready_for_dialogue=False)
    def issue(code, message, next_step):
        if code not in {row['code'] for row in issues}:
            issues.append(dict(code=code, message=message, next_step=next_step))
    if config.get('library_record_type') == 'notes':
        issue('not_recording', '场记记录不包含需要识别的视频场次。', 'import_materials')
    if not paths:
        issue('missing_pgm', '请先导入本场 PGM 素材。', 'import_materials')
    if not xml_paths:
        issue('missing_cut_xml', '请先导入本场所有 PGM 分割后的 XML。', 'import_materials')
    if config.get('library_cuts_invalidated'):
        result['cut_xml_ready'] = False
        issue('stale_cut_xml', '本场素材已追加或切点失效，请重新导入覆盖本场的切点 XML。', 'import_materials')
    duration = _registered_duration(config, paths, key)
    if strict and result['cut_xml_ready'] and not config.get('library_cuts_invalidated'):
        try:
            from cut_xml import timeline_info
            from library_bridge import translate_config
            actual = cut if cut is not None else timeline_info(translate_config(config))
            duration = _integer(actual.get('duration'))
            actual_paths = [key(row['path']) for row in actual['pgm_sources']]
            if set(actual_paths) != set(paths):
                result['cut_xml_ready'] = False
                issue('invalid_cut_xml', '切点 XML 与本场已登记 PGM 不一致，请重新导入核对。', 'import_materials')
            if _integer(actual.get('fps')) != _integer(config.get('fps')):
                result['cut_xml_ready'] = False
                issue('invalid_frame_rate', '切点 XML 与本场保存的帧率不一致，请重新导入核对。', 'camera_sync')
            if config.get('cut_xml_digest') and config['cut_xml_digest'] != actual.get('xml_digest'):
                result['cut_xml_ready'] = False
                issue('stale_cut_xml', '切点 XML 已改变，请到“机位与同步”重新识别并保存。', 'camera_sync')
            cut = actual
        except Exception as exc:
            result['cut_xml_ready'] = False
            issue('invalid_cut_xml', '切点 XML／PGM 无法核对：'+str(exc), 'import_materials')
    if _integer(config.get('fps')) not in (24, 25, 30):
        result['cut_xml_ready'] = False
        issue('invalid_frame_rate', '请核对本场切点 XML 与 PGM 的帧率。', 'camera_sync')
    result['duration_frames'] = duration
    rows = session.get('segments') or []
    cursor, valid = 0, bool(rows)
    from camera_schema import cameras_for
    try:
        cameras = cameras_for(config)
    except ValueError as exc:
        cameras, valid = (), False
        issue('invalid_cameras', str(exc), 'camera_sync')
    pending_camera = []
    unrecorded = (_no_recorded_camera(config,cut,cameras)
                  if any(isinstance(row,dict) and row.get('camera')=='?' for row in rows)
                  else lambda first,last:False)
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            valid = False
            pending_camera.append(_pending_picture(index, {}, config, key, cut,
                                                    '镜头记录格式无效，请重新识别并保存。'))
            continue
        first, last = _integer(row.get('start')), _integer(row.get('end'))
        camera = row.get('camera')
        geometry = first == cursor and last is not None and first is not None and last > first
        identity = (isinstance(camera, str) and len(camera) == 1 and camera in cameras
                    and row.get('reason') != 'identity_unconfirmed')
        if camera=='?' and row.get('reason')!='identity_unconfirmed' and geometry and unrecorded(first,last):
            identity=True
        if not geometry or not identity:
            valid = False
            if not geometry:
                message = '镜头范围不连续或无效，请核对切点结果。'
            elif row.get('reason') == 'sample_conflict':
                message = '取样画面的机位标号不一致，请核对本镜头。'
            elif row.get('reason') == 'sample_incomplete':
                message = '部分取样未读到可靠标号，补采样后仍证据不足，请核对本镜头。'
            elif row.get('reason') == 'identity_unconfirmed':
                message = '机位已更改，尚未确认。'
            else:
                message = '尚未识别到可靠机位，请核对本镜头。'
            pending_camera.append(_pending_picture(index, row, config, key, cut, message))
        if last is not None:
            cursor = last
    result.update(camera_total=len(rows), camera_confirmed=len(rows)-len(pending_camera),
                  camera_pending=len(pending_camera), camera_pending_rows=pending_camera)
    if not valid:
        message = (f'机位已确认 {result["camera_confirmed"]}/{len(rows)}，还有 {len(pending_camera)} 个镜头待核对；'
                   if rows else '尚未保存本场机位识别结果；')
        issue('camera_unconfirmed', message+'请到“机位与同步”核对并保存。', 'camera_sync')
    elif duration is None or duration <= 0 or cursor != duration:
        valid = False
        issue('camera_coverage', '机位结果尚未覆盖完整切点时间线，请到“机位与同步”核对并保存。', 'camera_sync')
    if valid and strict and cut and cut.get('shots'):
        expected = [(row['start'], row['end']) for row in cut['shots']]
        if [(row['start'], row['end']) for row in rows] != expected:
            valid = False
            issue('camera_boundaries', '已保存机位结果与当前 XML 切点不一致，请到“机位与同步”重新识别并保存。', 'camera_sync')
    result['camera_ready'] = valid
    if not result['calibration_ready']:
        issue('calibration_unconfirmed', '请到“机位与同步”核对视频同步补偿，确认同步并保存。', 'camera_sync')
    from sound_fallback import SoundStateIndex
    sound_index = SoundStateIndex(config, key)
    registered = {key(row.get('path')) for row in config.get('sound_sources', []) if isinstance(row, dict)}
    validation_index = None
    pending_sound, fallback_sound, silent_sound, matched_count = [], [], [], 0
    for path in paths:
        state = _sound_state(config, path, key, sound_index)
        failures = sound_index.aliases('library_processing', path)
        placements = state.get('placements') or []
        matched = (state.get('status') == 'matched' and isinstance(placements, list)
                   and bool(placements) and all(isinstance(row, dict) and row.get('wav_path') for row in placements))
        reason = 'not_synced' if state.get('status') != 'matched' else 'invalid_sound_map'
        message = ('尚未完成本段 PGM 的原始主 WAV 合板。' if reason == 'not_synced'
                   else '合板结果缺少有效的原始 WAV 对应范围。')
        if any(row.get('status') in ('failed', 'fallback') and row.get('stage') == 'sync' for row in failures):
            matched = False
            reason = 'sync_failed'
            errors = list(dict.fromkeys(str(row['error']) for row in failures
                if row.get('status') in ('failed', 'fallback') and row.get('stage') == 'sync' and row.get('error')))
            message = '；'.join(errors) or '本段 PGM 的声音合板失败，请检查参考声与原始 WAV。'
        if strict and matched:
            from library_sync import SyncValidationIndex, valid_sync
            if validation_index is None:
                validation_index = SyncValidationIndex(config)
            matched = valid_sync(config, path, state, index=validation_index)
            if not matched:
                reason, message = 'sync_stale', '已保存声音合板失效，源文件或同步设置已变化，请重新合板。'
        elif matched:
            used = {row.get('wav_path') for row in placements}
            matched = bool(used) and used.issubset(registered)
            if not matched:
                reason, message = 'wav_unregistered', '合板引用的原始 WAV 尚未登记在本场次。'
        if matched:
            matched_count += 1
            continue
        # A completed unmatched attempt is ready for deliberate audio silence.
        # It must never require probing/using the PGM's embedded sound here.
        from sound_fallback import valid_completed_sync_gap
        attempted = sound_index.eligible(path) or any(
            row.get('status') in ('failed','fallback') and row.get('stage')=='sync' for row in failures)
        if attempted:
            if strict:
                from library_sync import SyncValidationIndex
                if validation_index is None:
                    validation_index = SyncValidationIndex(config)
                if not valid_completed_sync_gap(config,path,index=validation_index):
                    pending_sound.append(dict(pgm_path=path, pgm=path, reason='pgm_fallback_stale',
                        message='已保存声音尝试失效，请重新运行本场声音合板。'))
                    continue
            row = dict(pgm_path=path, pgm=path, source_kind='none',
                       original_reason=reason, original_message=message)
            row.update(reason='wav_unmatched_silence',message='未匹配原始 WAV，音频留空。')
            silent_sound.append(row)
            continue
        pending_sound.append(dict(pgm_path=path, pgm=path, reason=reason, message=message))
    sound_ready = bool(paths) and not pending_sound
    result.update(sound_ready=bool(sound_ready), sound_matched=matched_count,
                  sound_pending=len(pending_sound), sound_pending_rows=pending_sound,
                  sound_fallback=len(fallback_sound), sound_fallback_rows=fallback_sound,
                  sound_gap=len(silent_sound), sound_gap_rows=silent_sound)
    summary = f'{matched_count} 条原始 WAV 合板'
    if silent_sound:
        summary += f' + {len(silent_sound)} 条声音空白'
    result['sound_message'] = summary
    if not sound_ready:
        issue('sound_unsynced', f'声音已合板 {matched_count}/{len(paths)}（{summary}），还有 {len(pending_sound)} 条 PGM 待处理；'
              '请到统一声音合板核对待处理文件。', 'sound_sync')
    result.update(issues=issues, message='；'.join(row['message'] for row in issues),
                  next_step=issues[0]['next_step'] if issues else 'dialogue',
                  ready=not issues, ready_for_dialogue=not issues)
    return result


def ensure_session_prepared(session, cut=None):
    result = session_readiness(session, strict=True, cut=cut)
    if not result['ready']:
        raise WorkflowReadinessError('场次“'+str(session.get('name') or session.get('id') or '')+'”尚未准备完成：'+result['message'])
    return result


def session_readiness_map(sessions):
    return {row['id']: session_readiness(row) for row in sessions if row.get('id')}


def preparation_signature(session):
    """Detect concurrent preparation edits without invalidating on ASR writes.

    This hashes saved JSON only. It intentionally does not stat media, and does
    not change when this worker saves subtitles or its own ASR failure state.
    """
    from sound_fallback import reference_provenance_config
    config = reference_provenance_config(session.get('config') or {})
    config.pop('library_asr', None)
    config.pop('library_script_run', None)
    processing = config.pop('library_processing', {}) or {}
    config['library_processing'] = {path: row for path, row in processing.items()
        if isinstance(row, dict) and row.get('status') in ('failed', 'fallback') and row.get('stage') == 'sync'}
    value = dict(config=config, segments=session.get('segments') or [])
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()
