"""Read-only diagnostics for the existing proxy-coverage export guard.

Only cached event/config fields are used. No media is probed, no path is
accessed, and no event is repaired or removed by this module. All out points
are exclusive, just like core.conform_events.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from fractions import Fraction
import math


MAX_EXAMPLES = 5
CAUSES = {
    'camera_unconfirmed': '机位未确认',
    'camera_not_configured': '机位未列入本场设置',
    'proxy_not_imported': '未导入该机位代理',
    'source_gap': '代理时码覆盖缺口',
    'source_overlap': '代理时码重叠',
    'source_unresolved': '代理对应关系未确定',
}
EVENT_FIELDS = ('start', 'end', 'camera', 'reason', 'pgm_path', 'pgm_in',
                'reel', 'path', 'source_in', 'source_out', 'record_in', 'record_out')


def _text(value):
    if value is None:
        return ''
    try:
        return str(value)
    except Exception:
        return '<无法显示>'


def _frame(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Fraction(_text(value))
        return int(number) if number.denominator == 1 else None
    except (ValueError, TypeError, ZeroDivisionError, OverflowError):
        return None


def _rate(value):
    rate = _frame(value)
    return rate if rate is not None and 0 < rate <= 1000 else None


def _tc(frame, fps):
    if frame is None or fps is None or not 0 <= frame < 24 * 3600 * fps:
        return None
    seconds, number = divmod(frame, fps)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f'{hours:02}:{minutes:02}:{seconds:02}:{number:02}'


def _basename(value):
    # This also handles a project copied from another OS; never resolve/stat.
    path = _text(value).replace('\\', '/').rstrip('/')
    return path.rsplit('/', 1)[-1] if path else 'PGM文件未知'


def _json_value(value, seen=None, depth=0):
    """Keep malformed diagnostic fields JSON-safe without hiding the failure."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else _text(value)
    if seen is None:
        seen = set()
    if depth >= 10 or id(value) in seen:
        return '<循环或过深字段>'
    if isinstance(value, Mapping):
        seen.add(id(value))
        result = {_text(key): _json_value(item, seen, depth + 1) for key, item in value.items()}
        seen.remove(id(value))
        return result
    if isinstance(value, (list, tuple)):
        seen.add(id(value))
        result = [_json_value(item, seen, depth + 1) for item in value]
        seen.remove(id(value))
        return result
    return _text(value)


def _event(value):
    if isinstance(value, Mapping):
        return dict(value)
    result = {}
    for field in EVENT_FIELDS:
        try:
            result[field] = getattr(value, field)
        except Exception:
            pass
    if not result:
        result['invalid_event'] = _text(value)
    return result


def _camera_ids(config):
    ids = config.get('camera_ids')
    if isinstance(ids, (list, tuple)) and ids:
        return {_text(camera) for camera in ids}
    count = _frame(config.get('camera_count'))
    if count is not None and 1 <= count <= 26:
        return set('ABCDEFGHIJKLMNOPQRSTUVWXYZ'[:count])
    # Malformed custom settings must not turn a diagnostic into another error.
    return None if 'camera_ids' in config or 'camera_count' in config else set('ABCD')


def _position(first, last, first_tc, last_tc, *, prefix):
    if first_tc is not None and last_tc is not None:
        return f'{prefix} {first_tc}–{last_tc}（出点不含）'
    if first is not None and last is not None:
        return f'{prefix}帧 {first}–{last}（出点不含）'
    if first is not None:
        return f'{prefix}帧 {first}，出点未知'
    return f'{prefix}位置未知'


def intentional_empty_camera_gap(event, config):
    """Only an explicit, known-camera, zero-coverage decision is permitted.

    An unknown identity or ambiguous source is never converted into a normal
    recording head/tail gap. The caller must opt in to both coverage policies.
    """
    if not isinstance(event, Mapping) or not isinstance(config, Mapping):
        return False
    cameras = _camera_ids(config)
    return (config.get('missing_camera_policy') == 'available'
            and config.get('empty_camera_policy') == 'gap'
            and event.get('empty_camera_gap') is True
            and event.get('available_cameras') == []
            and event.get('reason') == 'source_gap'
            and cameras is not None and event.get('camera') in cameras | {'?'}
            and not event.get('path') and event.get('reel') == 'BL')


def blocking_proxy_events(events, config):
    return [event for event in events
            if (not event.get('path') or event.get('reel') == 'BL')
            and not intentional_empty_camera_gap(event, config)]


def camera_coverage_summary(session_name, events, config):
    """Human-readable export decisions, including deliberately empty ranges."""
    fps = _rate(config.get('fps'))
    fallback, empty, counts = [], [], Counter()
    for event in events:
        available = event.get('available_cameras')
        if not isinstance(available, list):
            continue
        start, end = _frame(event.get('start')), _frame(event.get('end'))
        length = max(0, end-start) if start is not None and end is not None else 0
        counts[len(available)] += length
        if event.get('reason') != 'camera_unavailable_fallback' and not intentional_empty_camera_gap(event, config):
            continue
        local = _frame(event.get('pgm_in'))
        item = dict(pgm_filename=_basename(event.get('pgm_path')), pgm_path=_text(event.get('pgm_path')),
                    pgm_in_frame=local, pgm_out_frame_exclusive=local+length if local is not None else None,
                    pgm_in_timecode=_tc(local, fps),
                    pgm_out_timecode_exclusive=_tc(local+length, fps) if local is not None else None,
                    timeline_in_frame=start, timeline_out_frame_exclusive=end,
                    frames=length, requested_camera=event.get('requested_camera', event.get('camera')),
                    selected_camera=event.get('camera') if event.get('path') else None,
                    available_cameras=list(available))
        (empty if intentional_empty_camera_gap(event, config) else fallback).append(item)
    return dict(session_name=session_name, fps=fps,
                frames_by_available_camera_count=dict(sorted(counts.items())),
                fallback_count=len(fallback), fallback_frames=sum(row['frames'] for row in fallback),
                empty_count=len(empty), empty_frames=sum(row['frames'] for row in empty),
                fallbacks=fallback, empty_ranges=empty)


def build_proxy_coverage_report(session_name, events, config):
    """Return all blocked intervals as a JSON-safe dictionary, without I/O.

    ``counts`` groups mutually exclusive diagnoses, ``reason_counts`` retains
    the original conform/OCR reasons, and ``details`` contains every offending
    event and frame/timecode location. Invalid rates use frame numbers.
    """
    config = config if isinstance(config, Mapping) else {}
    fps = _rate(config.get('fps'))
    cameras = _camera_ids(config)
    imported = Counter()
    sources = config.get('sources') or []
    if isinstance(sources, (list, tuple)):
        for source in sources:
            if isinstance(source, Mapping) and (source.get('proxy_path') or source.get('path')):
                imported[_text(source.get('camera'))] += 1
    counts, reasons, camera_counts = Counter(), Counter(), Counter()
    details = []
    event_count = 0
    for event_count, raw in enumerate(events or [], 1):
        event = _event(raw)
        # Exactly mirror the caller's established guard. Reasons alone are not
        # additional blockers, nor is a diagnostic allowed to release one.
        if event.get('path') and event.get('reel') != 'BL':
            continue
        if intentional_empty_camera_gap(event, config):
            continue
        camera = _text(event.get('camera')).strip() or '?'
        reason = _text(event.get('reason'))
        if camera == '?':
            category = 'camera_unconfirmed'
        elif cameras is not None and camera not in cameras:
            category = 'camera_not_configured'
        elif reason == 'source_overlap':
            category = 'source_overlap'
        elif reason == 'source_gap':
            category = 'source_gap' if imported[camera] else 'proxy_not_imported'
        elif not imported[camera]:
            category = 'proxy_not_imported'
        else:
            category = 'source_unresolved'
        start, end = _frame(event.get('start')), _frame(event.get('end'))
        pgm_in = _frame(event.get('pgm_in'))
        length = end - start if start is not None and end is not None and end > start else None
        pgm_out = pgm_in + length if pgm_in is not None and length is not None else None
        record_in, record_out = _frame(event.get('record_in')), _frame(event.get('record_out'))
        detail = dict(index=len(details) + 1, event_index=event_count - 1,
            camera=camera, category=category, category_label=CAUSES[category], reason=reason,
            imported_proxy_count=imported[camera],
            pgm_path=_text(event.get('pgm_path')), pgm_filename=_basename(event.get('pgm_path')),
            timeline_in_frame=start, timeline_out_frame_exclusive=end,
            pgm_in_frame=pgm_in, pgm_out_frame_exclusive=pgm_out,
            pgm_in_timecode=_tc(pgm_in, fps), pgm_out_timecode_exclusive=_tc(pgm_out, fps),
            record_in_frame=record_in, record_out_frame_exclusive=record_out,
            record_in_timecode=_tc(record_in, fps), record_out_timecode_exclusive=_tc(record_out, fps),
            event=_json_value(event))
        pgm_location = _position(pgm_in, pgm_out, detail['pgm_in_timecode'], detail['pgm_out_timecode_exclusive'], prefix='PGM内')
        if pgm_in is None:
            pgm_location += '；' + _position(start, end, None, None, prefix='时间线')
        record_location = _position(record_in, record_out, detail['record_in_timecode'], detail['record_out_timecode_exclusive'], prefix='记录TC' if detail['record_in_timecode'] else '记录')
        detail['location_text'] = f'{camera}机｜{detail["pgm_filename"]}｜{pgm_location}｜{record_location}'
        details.append(detail)
        counts[category] += 1;reasons[reason or '<无原始原因>'] += 1;camera_counts[camera] += 1
    recommendations = []
    if counts['camera_unconfirmed'] or counts['camera_not_configured']:
        recommendations.append('先在“机位识别与核对”确认列出镜头的机位及本场机位设置。')
    missing = sorted({row['camera'] for row in details if row['category'] == 'proxy_not_imported'})
    if missing:
        recommendations.append('为 ' + '/'.join(missing) + ' 机导入本场对应代理。')
    if counts['source_gap']:
        recommendations.append('核对代理起始时码、帧率及停机后的代理文件是否齐全。')
    if counts['source_overlap']:
        recommendations.append('检查同机位重复导入或重叠时码，去重或核对重叠区间；本报告不指定删除哪条文件。')
    if counts['source_unresolved']:
        recommendations.append('核对列出镜头的机位结果与本场代理登记。')
    return dict(version=1, session_name=_text(session_name) or '未命名场次',
        status='blocked' if details else 'ok', fps=fps, fps_input=_json_value(config.get('fps')),
        frame_rate_note='' if fps else '帧率信息无效或非整数，位置改用帧号。',
        event_count=event_count, problem_count=len(details),
        counts={key:counts[key] for key in CAUSES if counts[key]},
        reason_counts=dict(reasons), camera_counts=dict(camera_counts),
        imported_proxy_counts=dict(imported), details=details, recommendations=recommendations)


def proxy_coverage_message(session_name, events, config):
    """Return a compact explanation of the unchanged export blocker."""
    report = build_proxy_coverage_report(session_name, events, config)
    if not report['problem_count']:
        return report['session_name'] + '：代理覆盖检查未发现阻断区间。'
    lines = [f'{report["session_name"]}：有 {report["problem_count"]} 个问题区间，尚不能导出。',
             '；'.join(f'{CAUSES[key]} {count} 处' for key, count in report['counts'].items()) + '。']
    if report['frame_rate_note']:
        lines.append(report['frame_rate_note'])
    for detail in report['details'][:MAX_EXAMPLES]:
        lines.append(f'{detail["index"]}. {detail["location_text"]}｜{detail["category_label"]}')
    if report['problem_count'] > MAX_EXAMPLES:
        lines.append(f'其余 {report["problem_count"]-MAX_EXAMPLES} 个区间见完整诊断报告。')
    lines.extend(report['recommendations'])
    return '\n'.join(lines)
