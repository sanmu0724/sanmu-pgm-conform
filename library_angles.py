"""Relate one PGM dialogue range to its own session's registered camera angles.

Only in-memory source headers and existing source-clock mapping are used. These
relations neither create dialogue matches nor identify another performance.
"""
from __future__ import annotations

from fractions import Fraction as F
from bisect import bisect_left, bisect_right
from collections import OrderedDict
import math

from camera_schema import cameras_for
from library_correspondence import PGMInfoIndex, _info, _pgm_info, _positive_rate, _proxies, _video


def _proxy_config(config):
    """Flatten registered headers once, avoiding a registry scan per angle."""
    cameras = set(cameras_for(config))
    registry = {}
    for row in config.get('library_media', []):
        if row.get('kind') == 'proxy' and row.get('path'):
            registry.setdefault(row['path'], _info(row))
    sources = list(config.get('sources', []))
    present = {(str(row.get('camera', '')).upper(), row.get('proxy_path') or row.get('path', '')) for row in sources}
    for path, row in registry.items():
        camera = str(row.get('camera', '')).upper()
        if camera in cameras and (camera, path) not in present:
            sources.append(dict(row, path=path, camera=camera))
            present.add((camera, path))
    flattened = []
    for source in sources:
        info = _info(source)
        path = info.get('proxy_path') or info.get('path', '')
        registered = registry.get(path, {})
        own_rate = _positive_rate(registered.get('fps')) or _positive_rate(info.get('fps'))
        row = dict(info)
        row.update(path=path, start_tc=info.get('start_tc') or registered.get('start_tc') or '',
                   frames=registered.get('frames', info.get('frames')),
                   source_id=info.get('source_id') or registered.get('source_id') or registered.get('id') or info.get('id') or '')
        if own_rate is not None:
            row['fps'] = str(own_rate)
        flattened.append(row)
    result = dict(config)
    result.update(sources=flattened, library_media=[])
    return result


def _is_pgm(source, path, config):
    if source.get('kind'):
        return source['kind'] == 'pgm'
    if config.get('pgm') == path or path in config.get('pgm_timecodes', {}) or path in config.get('library_sync', {}):
        return True
    return any((row == path if isinstance(row, str) else row.get('path') == path)
               for row in config.get('pgm_sources', [])) or any(
        row.get('kind') == 'pgm' and row.get('path') == path for row in config.get('library_media', []))


def _empty(warnings):
    return dict(related_angles=[], related_angle_count=0, angle_count=0, camera_count=0,
                related_angle_warnings=list(dict.fromkeys(warnings)))


def _proxy_headers(config):
    """Parse immutable snapshot headers once; invalid rows remain reviewable."""
    from core import tc_frames
    result = []
    for row in config.get('sources', []):
        rate = _positive_rate(row.get('fps')) or _positive_rate(config.get('fps'))
        try:
            if rate is None or rate.denominator != 1 or not row.get('path'):
                raise ValueError()
            first = tc_frames(str(row.get('start_tc') or ''), int(rate))
            frames = int(row.get('frames'))
            if frames <= 0:
                raise ValueError()
            interval = (first, first + frames)
        except (TypeError, ValueError, ZeroDivisionError, OverflowError):
            interval = None
        result.append((str(row.get('camera', '')).upper(), rate, interval, row))
    return result


def _intersecting_proxy_config(config, headers, pgm, rate, clock):
    """Skip provably distant valid sources without suppressing report warnings.

    The original mapper still owns clock/range/error semantics. Keep invalid
    headers and rate mismatches for its explicit unavailable-source reports.
    Keep at least one imported source per camera even when none intersects so
    its uncovered-range report remains identical to an unfiltered scan.
    """
    if isinstance(headers, _ProxyIntervalIndex):
        return headers.select(config, pgm, rate, clock)
    targets = {}
    for camera in cameras_for(config):
        try:
            offset = config.get('camera_offsets', {}).get(camera, 0)
            parsed = F(str(offset))
            if isinstance(offset, bool) or parsed.denominator != 1:
                raise ValueError()
            targets[camera] = (clock + pgm['frame_in'] + int(offset),
                               clock + pgm['frame_out_exclusive'] + int(offset))
        except (TypeError, ValueError, ZeroDivisionError, OverflowError):
            targets[camera] = None
    selected, seen, fallback = [], set(), {}
    for camera, own_rate, interval, row in headers:
        fallback.setdefault(camera, row)
        target = targets.get(camera)
        intersects = (target is None or interval is None or own_rate != rate or
                      min(target[1], interval[1]) > max(target[0], interval[0]))
        if intersects:
            selected.append(row)
            seen.add(camera)
    selected.extend(row for camera, row in fallback.items() if camera not in seen)
    return dict(config, sources=selected)


class _ProxyIntervalIndex:
    """Query valid source intervals in O(log n + actual intersecting sources).

    Different rates/invalid headers still pass through to the existing mapper,
    which owns the warnings and exact source-clock validation. Prefix maximum
    ends retain long and overlapping sources rather than choosing one take.
    """
    def __init__(self, headers):
        self.groups, self.fallback, self.by_camera = {}, {}, {}
        for number, (camera, rate, interval, row) in enumerate(headers):
            item = (number, rate, interval, row)
            self.fallback.setdefault(camera, item)
            self.by_camera.setdefault(camera, []).append(item)
            self.groups.setdefault((camera, rate), []).append(item)
        self.valid, self.invalid = {}, {}
        for identity, rows in self.groups.items():
            valid = sorted((item for item in rows if item[2] is not None), key=lambda item: (item[2][0], item[0]))
            maxima, last = [], -math.inf
            for item in valid:
                last = max(last, item[2][1])
                maxima.append(last)
            self.valid[identity] = (valid, [item[2][0] for item in valid], maxima)
            self.invalid[identity] = [item for item in rows if item[2] is None]

    def select(self, config, pgm, rate, clock):
        selected = []
        for camera, rows in self.by_camera.items():
            try:
                offset = config.get('camera_offsets', {}).get(camera, 0)
                parsed = F(str(offset))
                if isinstance(offset, bool) or parsed.denominator != 1:
                    raise ValueError()
                begin = clock + pgm['frame_in'] + int(offset)
                end = clock + pgm['frame_out_exclusive'] + int(offset)
            except (TypeError, ValueError, ZeroDivisionError, OverflowError):
                selected.extend(rows)
                continue
            own = []
            for (which, own_rate), group in self.groups.items():
                if which != camera:
                    continue
                if own_rate != rate:
                    own.extend(group)
                    continue
                valid, starts, maxima = self.valid[(camera, own_rate)]
                first = bisect_right(maxima, begin)
                stop = bisect_left(starts, end)
                own.extend(item for item in valid[first:stop] if item[2][1] > begin)
                own.extend(self.invalid[(camera, own_rate)])
            selected.extend(own or [self.fallback[camera]])
        selected.sort(key=lambda item: item[0])
        return dict(config, sources=[item[3] for item in selected])


class AngleMapper:
    """Per-refresh mapper; camera associations are scoped by immutable session ID."""
    def __init__(self, sessions):
        self.configs = {row['id']: row.get('config') or {} for row in sessions if row.get('id')}
        self.proxy_configs = {identity: _proxy_config(config) for identity, config in self.configs.items()}
        self.proxy_headers = {identity: _proxy_headers(config) for identity, config in self.proxy_configs.items()}
        self.proxy_indexes = {identity: _ProxyIntervalIndex(headers) for identity, headers in self.proxy_headers.items()}
        self.info_indexes = {identity: PGMInfoIndex(config) for identity, config in self.configs.items()}
        self.cache = OrderedDict()
        self.cache_limit = 512

    def fields(self, clip, transcript=None):
        source = transcript if transcript is not None else clip
        sid = source.get('session_id') or clip.get('session_id')
        path = source.get('media_path') or clip.get('media_path')
        config = self.configs.get(sid)
        if config is None:
            return _empty(['缺少本段 PGM 所属拍摄场次，未关联其他场次代理。'])
        if not path or not _is_pgm(source, path, config):
            return _empty(['仅对 PGM 台词片段关联同场机位，未推测其他媒体的对应关系。'])
        try:
            begin, end = F(str(source['start_seconds'])), F(str(source['end_seconds']))
            if not (math.isfinite(float(begin)) and math.isfinite(float(end)) and 0 <= begin < end):
                raise ValueError()
        except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
            return _empty(['PGM 台词片段缺少有效起止范围，未关联代理。'])
        info, _ = _pgm_info(config, path, source, self.info_indexes[sid])
        key = (sid, path, begin, end, str(info.get('fps')), str(info.get('frames')), str(info.get('start_tc')))
        if key not in self.cache:
            self.cache[key] = self._map(config, self.proxy_configs[sid], path, source, begin, end,
                                        self.proxy_indexes[sid], self.info_indexes[sid])
            if len(self.cache) > self.cache_limit:
                self.cache.popitem(last=False)
        else:
            self.cache.move_to_end(key)
        base = self.cache[key]
        parent_candidate = clip.get('candidate_id') or clip.get('id')
        parent_transcript = clip.get('transcript_id') or source.get('id')
        angles = [dict(row, session_id=sid, parent_candidate_id=parent_candidate,
                       parent_transcript_id=parent_transcript) for row in base['related_angles']]
        return dict(base, related_angles=angles, related_angle_warnings=list(base['related_angle_warnings']))

    @staticmethod
    def _map(config, proxy_config, path, source, begin, end, proxy_headers=None, info_index=None):
        warnings = []
        pgm, rate, clock, _ = _video(config, path, source, begin, end, warnings, info_index)
        if rate is None or rate.denominator != 1 or clock is None:
            return _empty(warnings + ['PGM 缺少有效源时码／整数帧率，未关联代理。'])
        if not pgm.get('total_frames'):
            return _empty(warnings + ['PGM 缺少实际总帧数，无法核对有效范围，未关联代理。'])
        if pgm['frame_in'] >= pgm['total_frames']:
            return _empty(warnings + ['台词片段位于 PGM 文件范围之外，未关联代理。'])
        # Transcript seconds cover complete video frames; never map beyond the
        # actual PGM or beyond any proxy's own source interval.
        pgm['frame_out_exclusive'] = min(pgm['frame_out_exclusive'], pgm['total_frames'])
        if proxy_headers is not None:
            proxy_config = _intersecting_proxy_config(proxy_config, proxy_headers, pgm, rate, clock)
        reports = _proxies(proxy_config, pgm, rate, clock, warnings)
        angles = []
        for camera, report in reports.items():
            for segment in report['segments']:
                row = dict(segment)
                row.update(camera=camera, media_path=segment['path'], relation='same_session_timecode',
                           pgm_start_seconds=float(F(segment['pgm_frame_in'], 1) / rate),
                           pgm_end_seconds=float(F(segment['pgm_frame_out_exclusive'], 1) / rate),
                           source_tc_out=segment['source_tc_out_exclusive'])
                angles.append(row)
            if any(row['ambiguous'] for row in report['segments']):
                warnings.append(camera + ' 机位同一区间有重叠代理，已全部保留，请核对。')
            if report['segments'] and not report['calibration_confirmed']:
                warnings.append(camera + ' 机位同步补偿尚未核对。')
            if report['uncovered']:
                gaps = '、'.join(f"[{float(F(row['pgm_frame_in'], 1) / rate):.3f},"
                                f"{float(F(row['pgm_frame_out_exclusive'], 1) / rate):.3f})"
                                for row in report['uncovered'])
                warnings.append(camera + ' 机位代理未覆盖部分 PGM 秒区间：' + gaps)
        angles.sort(key=lambda row: (row['camera'], row['pgm_start_seconds'], row['media_path'], row['start_seconds']))
        if not angles:
            warnings.append('本段 PGM 暂无同场且覆盖该时码区间的已登记代理。')
        return dict(related_angles=angles, related_angle_count=len(angles), angle_count=len(angles),
                    camera_count=len({row['camera'] for row in angles}),
                    related_angle_warnings=list(dict.fromkeys(warnings)))


def enrich_related_angles(data, mapper=None):
    """Attach precise angle relations to existing candidates without new matches.

    Only candidate dictionaries are shallow-copied/replaced. All original
    candidate/transcript identities, statuses and catalogue collections remain.
    """
    mapper = mapper if mapper is not None else AngleMapper(data.get('sessions', []))
    transcripts = {row['id']: row for row in data.get('transcripts', []) if row.get('id')}
    result = dict(data)
    result['candidates'] = [dict(row, **mapper.fields(row, transcripts.get(row.get('transcript_id'))))
                            for row in data.get('candidates', [])]
    return result
