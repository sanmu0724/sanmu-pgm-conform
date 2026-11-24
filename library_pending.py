"""Unprocessed PGM ranges for explicit manual picture classification.

Pending ranges and angle relations are derived views, never invented speech.
Confirmed source-local picture annotations remove only their actual interval.
"""
from __future__ import annotations

from fractions import Fraction as F
import hashlib
import json

from library_angles import AngleMapper
from library_correspondence import _gaps, _pgm_info, _positive_rate, filter_active_transcripts, pgm_reference_ranges


def is_manual_visual(row):
    return row.get('source_kind') == 'manual_visual' or (row.get('data') or {}).get('source_kind') == 'manual_visual'


def _registered_pgms(config):
    result, seen = [], set()

    def add(value):
        if isinstance(value, str) and value and value not in seen:
            result.append(value)
            seen.add(value)
        elif isinstance(value, dict):
            add(value.get('path'))
        elif isinstance(value, list):
            for row in value:
                add(row)
    add(config.get('pgm'))
    add(config.get('pgm_sources', []))
    for path in config.get('pgm_timecodes', {}):
        add(path)
    for row in config.get('library_media', []):
        if row.get('kind') == 'pgm':
            add(row)
    return result


def _range(row, duration):
    try:
        first, last = F(str(row['start_seconds'])), F(str(row['end_seconds']))
        first, last = max(F(0), first), min(duration, last)
        return (first, last) if last > first else None
    except (KeyError, ValueError, TypeError, ZeroDivisionError, OverflowError):
        return None


def enrich_pending_pgm(data, mapper=None):
    """Return data plus stable pending_pgm_intervals and unlocated warnings."""
    current = filter_active_transcripts(data)
    sessions = current.get('sessions', [])
    mapper = mapper if mapper is not None else AngleMapper(sessions)
    transcripts = {row['id']: row for row in current.get('transcripts', []) if row.get('id')}
    spoken = {}
    confirmed = {}
    for row in transcripts.values():
        if row.get('kind', 'pgm') == 'pgm' and not is_manual_visual(row):
            spoken.setdefault((row.get('session_id'), row.get('media_path')), []).append(row)
    for candidate in current.get('candidates', []):
        if candidate.get('status') != 'confirmed' or not candidate.get('script_active', True):
            continue
        row = transcripts.get(candidate.get('transcript_id'))
        if row is not None and row.get('kind', 'pgm') == 'pgm':
            confirmed.setdefault((row.get('session_id'), row.get('media_path')), []).append(row)
    pending, warnings = [], []
    for session in sessions:
        config = session.get('config') or {}
        for path in _registered_pgms(config):
            scope = (session['id'], path)
            items = spoken.get(scope, [])
            info_index = mapper.info_indexes[session['id']]
            info, sync = _pgm_info(config, path, items[0] if items else {}, info_index)
            rate = _positive_rate(info.get('fps'))
            try:
                frames = int(info.get('frames'))
                if rate is None or frames <= 0:
                    raise ValueError()
                duration = F(frames, 1) / rate
            except (TypeError, ValueError, ZeroDivisionError, OverflowError):
                warnings.append(dict(session_id=session['id'], media_path=path,
                                     reason='PGM 缺少实际帧率／帧数，无法建立人工核查范围，请重新核对素材登记。'))
                continue
            audio_coverage = []
            state = config.get('library_processing', {}).get(path) or {}
            failed_sync = state.get('status') == 'failed' and state.get('stage') == 'sync'
            reference_coverage = pgm_reference_ranges(config, path, duration, info_index.sound)
            reference_only = bool(reference_coverage) and (config.get('library_asr', {}).get(path) or {}).get('asr_audio_source') == 'pgm_reference'
            if reference_coverage:
                failed_sync = False
            manual_subtitles = (config.get('library_asr', {}).get(path) or {}).get('source_kind') == 'manual'
            if sync.get('status') == 'matched' and not failed_sync and not reference_only:
                for placement in sync.get('placements', []):
                    if placement.get('status') in ('unmatched', 'failed', 'outside_pgm') or not placement.get('wav_path'):
                        continue
                    interval = _range(dict(start_seconds=placement.get('pgm_start_seconds'),
                                           end_seconds=placement.get('pgm_end_seconds')), duration)
                    if interval:
                        audio_coverage.append(interval)
            audio_coverage.extend(reference_coverage)
            has_audio = bool(audio_coverage)
            has_speech = not failed_sync and any(_range(row, duration) for row in items)
            if not has_audio:
                ranges = [(F(0), duration)]
                reason_code, reason = 'audio_unmatched', '未找到对应原始声音，需人工核对画面归类'
            elif not has_speech:
                ranges = [(F(0), duration)]
                reason_code, reason = 'asr_unavailable', '尚无可用台词识别结果，需人工核对画面归类'
            else:
                ranges = _gaps(F(0), duration, audio_coverage)
                reason_code, reason = 'audio_gap', '该区间未被原始声音覆盖，需人工核对画面归类'
            classified = [interval for row in confirmed.get(scope, [])
                          if (not failed_sync or manual_subtitles or is_manual_visual(row)) and (interval := _range(row, duration))]
            # Real utterances already have their own candidate/unmatched queue.
            # A failed synchronization cannot validate an old automatic utterance.
            if not failed_sync or manual_subtitles:
                classified.extend(interval for row in items if (interval := _range(row, duration)))
            for begin, end in ranges:
                for first, last in _gaps(begin, end, classified):
                    raw = json.dumps([session['id'], path, str(first), str(last)], ensure_ascii=False, separators=(',', ':'))
                    identity = 'pending-pgm-' + hashlib.sha256(raw.encode()).hexdigest()[:24]
                    source = dict(session_id=session['id'], media_path=path, kind='pgm',
                                  start_seconds=float(first), end_seconds=float(last))
                    row = dict(source, id=identity, pgm_path=path, session_name=session.get('name', ''),
                               manual_pending=True, reason=reason, reason_code=reason_code,
                               processing_status=state.get('status', 'pending'), error=state.get('error', ''),
                               has_audio_sync=has_audio, session_revision=session.get('revision'))
                    row.update(mapper.fields(source))
                    pending.append(row)
    result = dict(data)
    result.update(pending_pgm_intervals=pending, pending_pgm_warnings=warnings)
    return result
