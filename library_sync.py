"""Synchronize registered PGM files before transcribing original main WAVs.

All positions in this module are source-local. Semantic dialogue associations
never change this mapping or either media file's original timecode.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from fractions import Fraction
from pathlib import Path

from core import AnalysisError, probe, validate_rate, frames_tc
from library_bridge import canonical_path, resolve_local_path, translate_config
from sound_sync import SAMPLE_RATE, sound_plan


def _identity(path):
    path = Path(resolve_local_path(path)).expanduser().resolve()
    stat = path.stat()
    return {'path': canonical_path(str(path)), 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}


def _settings(config, path, used_paths=None):
    canonical = canonical_path(str(Path(resolve_local_path(path)).expanduser().resolve()))
    from sound_fallback import sound_layout_settings
    return {'mode': config.get('sound_sync_mode', 'waveform'),
            'sound_layout': sound_layout_settings(config, used_paths, key=lambda value:
                canonical_path(str(Path(resolve_local_path(value)).expanduser().resolve()))),
            'allow_gaps': config.get('sound_allow_gaps', True),
            'pgm_timecode': next((value for key, value in config.get('pgm_timecodes', {}).items()
                                  if canonical_path(str(Path(resolve_local_path(key)).resolve())) == canonical), ''),
            'reference': None}


def _signature(dependencies, settings):
    value = {'dependencies': sorted(dependencies, key=lambda row: row['path']), 'settings': settings}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def legacy_mono_signature_compatible(config, placements=()):
    """Allow old layout-free signatures only for ordinary standalone sound.

    Split-channel support added layout inputs to signatures. An old cache
    cannot prove those inputs, so any manual role, filename hint or recorder
    file-set metadata must take the normal resync path. This guard performs no
    I/O; callers must still verify their usual file identities and settings.
    """
    from sound_pairs import sound_channel_hint

    def ordinary(row, path_field, require_metadata=False):
        if not isinstance(row, dict) or not isinstance(row.get(path_field), str) or not row[path_field]:
            return False
        if row.get('sound_role') not in (None, '', 'auto') or row.get('channel') not in (None, '', 'main'):
            return False
        if any(row.get(field) for field in ('sound_pair', 'pair_id', 'out_channel', 'alignment_source_path',
                                             'family_uid', 'file_set_index')):
            return False
        if sound_channel_hint(row[path_field])[0]:
            return False
        metadata = row.get('metadata') or {}
        if not isinstance(metadata, dict):
            return False
        # Older source lists sometimes omitted recorder metadata altogether.
        # Their bytes may contain file-set pairing cues unknown to this cache.
        if require_metadata and not isinstance(metadata.get('ixml'), dict):
            return False
        ixml = metadata.get('ixml') or {}
        if not isinstance(ixml, dict):
            return False
        return not any(ixml.get(field) for field in ('FILE_SET/FAMILY_UID', 'FAMILY_UID',
                         'FILE_SET/FILE_SET_INDEX', 'FILE_SET_INDEX'))

    sources = config.get('sound_sources')
    if not isinstance(sources, list) or not sources or not all(ordinary(row, 'path', True) for row in sources):
        return False
    paths = [os.path.normpath(os.path.abspath(os.path.expanduser(row['path']))) for row in sources]
    if len(set(paths)) != len(paths) or not isinstance(placements, (list, tuple)):
        return False
    for row in placements:
        if not ordinary(row, 'wav_path'):
            return False
        info = row.get('wav_info', {})
        if not isinstance(info, dict) or not ordinary(dict(info, path=row['wav_path']), 'path'):
            return False
    return True


class SyncValidationIndex:
    """Filesystem identities and signature settings for one export/preflight.

    Construct a fresh object for every operation. A source replacement between
    operations is always restatted; this cache is never persisted in a project.
    """
    def __init__(self, config):
        self.config = config
        self._canonical, self._identities = {}, {}
        self.selected = {self.canonical(row['path']) for row in config.get('sound_sources', [])}
        from sound_fallback import sound_layout_settings
        self.sound_layout = sound_layout_settings(config, key=self.canonical)
        self._sound_layout_by_used = {}
        self.timecodes = {}
        for path, value in config.get('pgm_timecodes', {}).items():
            self.timecodes.setdefault(self.canonical(path), value)

    def canonical(self, path):
        if path not in self._canonical:
            self._canonical[path] = canonical_path(str(Path(resolve_local_path(path)).expanduser().resolve()))
        return self._canonical[path]

    def identity(self, path):
        canonical = self.canonical(path)
        if canonical not in self._identities:
            self._identities[canonical] = _identity(path)
        return self._identities[canonical]

    def settings(self, path, used_paths=None):
        canonical = self.canonical(path)
        used = None if used_paths is None else frozenset(self.canonical(value) for value in used_paths)
        if used is None:
            layout = self.sound_layout
        else:
            if used not in self._sound_layout_by_used:
                from sound_fallback import sound_layout_settings
                self._sound_layout_by_used[used] = sound_layout_settings(self.config, used, key=self.canonical)
            layout = self._sound_layout_by_used[used]
        return {'mode': self.config.get('sound_sync_mode', 'waveform'),
                'sound_layout': layout,
                'allow_gaps': self.config.get('sound_allow_gaps', True),
                'pgm_timecode': self.timecodes.get(canonical, ''),
                'reference': None}


def _historical_reference_settings(config, path, settings, index=None):
    """Reconstruct retired reference provenance only for an old cache miss.

    These settings never reach sound_plan or a new signature. Reading the
    actual historical file identity prevents an old successful mapping from
    surviving replacement or disappearance of its original reference.
    """
    from sound_fallback import historical_sound_references
    canonical = (index.canonical if index is not None else lambda value:
        canonical_path(str(Path(resolve_local_path(value)).expanduser().resolve())))
    target = canonical(path)
    references = [dict(path=canonical(value['path']), start_seconds=value.get('start_seconds', 0))
                  for candidate, value in historical_sound_references(config).items()
                  if canonical(candidate) == target]
    if not references or any(value != references[0] for value in references[1:]):
        return None
    reference = references[0]
    identity = index.identity if index is not None else _identity
    return dict(settings, reference=dict(path=canonical(reference['path']),
        start_seconds=reference.get('start_seconds', 0), identity=identity(reference['path'])))


def valid_sync(config, path, state, index=None):
    """New unrelated files do not invalidate a previously established mapping."""
    if not state or state.get('version') != 1 or state.get('status') != 'matched':
        return False
    try:
        selected = (index.selected if index is not None else
                    {canonical_path(str(Path(resolve_local_path(row['path'])).resolve()))
                     for row in config.get('sound_sources', [])})
        used = {row['wav_path'] for row in state['placements']}
        if not used or not used.issubset(selected):
            return False
        if state.get('uncovered'):
            # New sound can fill an earlier gap. Full coverage only depends on
            # the WAVs actually used, while partial coverage must retry new WAVs.
            candidates = sorted(((index.identity(wav) if index is not None else _identity(wav)) for wav in selected), key=lambda row: row['path'])
            if candidates != state.get('candidate_dependencies'):
                return False
        identity = index.identity if index is not None else _identity
        dependencies = [identity(path)] + [identity(wav) for wav in sorted(used)]
        settings = index.settings(path, used) if index is not None else _settings(config, path, used)
        if state['signature'] == _signature(dependencies, settings):
            return True
        # Output silence is now fixed policy. Changing the retired gap checkbox
        # does not alter an already measured WAV offset or its source identity.
        for allow_gaps in (True,False):
            if state['signature']==_signature(dependencies,dict(settings,allow_gaps=allow_gaps)):
                return True
        # Preserve the provenance signature used by existing ASR. Try the old
        # PGM-only schema before reading a retired reference file's identity.
        ordinary = legacy_mono_signature_compatible(config, state['placements'])
        if ordinary and state['signature'] == _signature(dependencies,
                {key: value for key, value in settings.items() if key != 'sound_layout'}):
            return True
        historical = _historical_reference_settings(config, path, settings, index)
        if historical is None:
            return False
        if state['signature'] == _signature(dependencies, historical):
            return True
        return ordinary and state['signature'] == _signature(dependencies,
            {key: value for key, value in historical.items() if key != 'sound_layout'})
    except (OSError, KeyError, ValueError, TypeError):
        return False


def ensure_synced_pgm(config, pgm_path, progress=lambda n, message: None, cancelled=lambda: False):
    path = str(Path(resolve_local_path(pgm_path)).expanduser().resolve())
    shared = canonical_path(path)
    if cancelled():
        raise AnalysisError('已取消合板。')
    state = config.get('library_sync', {}).get(shared)
    if valid_sync(config, path, state):
        progress(100, '沿用已完成的声音合板：' + Path(path).name)
        return copy.deepcopy(state)
    if not config.get('sound_sources'):
        raise AnalysisError('请在统一素材入口补充本拍摄场次的原始主WAV，完成合板后再识别台词。')
    info = probe(path)
    rate = Fraction(str(info['fps']))
    if rate.denominator != 1 or rate.numerator not in (24,25,30):
        raise AnalysisError('目前仅支持24/25/30fps逐行素材。')
    fps = rate.numerator
    validate_rate(info,fps)
    if Fraction(str(config.get('fps', fps))) != rate:
        raise AnalysisError('登记的帧率与PGM实际帧率不同，请重新核对本场素材。')
    local = translate_config(config)
    local.update(fps=fps, sound_allow_gaps=config.get('sound_allow_gaps', True))
    tc = local.get('pgm_timecodes', {}).get(path) or info.get('start_tc') or ''
    block = dict(path=path, start=0, frames=int(info['frames']), start_tc=tc)
    wavs, events = sound_plan(local, {'pgm_sources': [block], 'duration': block['frames']}, progress, cancelled)
    by_path = {row['path']: row for row in wavs}
    placements = []
    for event in events:
        wav = by_path[event['path']]
        first = Fraction(event['record_start'])
        length = Fraction(event['duration'])
        source = Fraction(event['source_start']) - Fraction(wav['start'], SAMPLE_RATE)
        metadata = {key: copy.deepcopy(wav.get(key)) for key in (
            'filename', 'original_audio_filename', 'sample_rate', 'channels', 'depth', 'samples',
            'has_timecode', 'sound_roll', 'recording_date', 'recording_time', 'file_uid',
            'originator_reference', 'umid')}
        metadata['start_samples'] = wav['start']
        start_seconds = Fraction(wav['start'], SAMPLE_RATE)
        metadata['start_tc'] = frames_tc(int(start_seconds * fps), fps) if wav['has_timecode'] and 0 <= start_seconds < 86400 else ''
        placements.append(dict(wav_path=canonical_path(wav['path']), pgm_start_seconds=float(first),
            pgm_end_seconds=float(first + length), wav_start_seconds=float(source),
            wav_end_seconds=float(source + length), offset_seconds=float(first - source),
            method=event['method'], score=event.get('score'), wav_info=metadata))
        if event.get('pair_id'):
            placements[-1].update(pair_id=event['pair_id'], channel=event['channel'],
                out_channel=event['out_channel'], alignment_source_path=canonical_path(event['alignment_source_path'])
                if event.get('alignment_source_path') else '')
    gaps, seen = [], set()
    for wav in wavs:
        for row in wav.get('sync_report', []):
            key = (row.get('pgm_start_seconds'), row.get('pgm_end_seconds'))
            if row.get('status') == 'uncovered' and key not in seen:
                gaps.append(dict(start_seconds=key[0], end_seconds=key[1]))
                seen.add(key)
    if not placements:
        raise AnalysisError('原始声音没有覆盖此PGM，无法进行合板台词识别。')
    dependencies = [_identity(path)] + [_identity(value) for value in sorted({row['wav_path'] for row in placements})]
    used = {row['wav_path'] for row in placements}
    return dict(version=1, status='matched', signature=_signature(dependencies, _settings(config, path, used)),
        dependencies=dependencies, pgm_info=dict(path=shared, fps=fps, frames=block['frames'], start_tc=tc),
        candidate_dependencies=sorted((_identity(row['path']) for row in wavs), key=lambda row: row['path']),
        placements=placements, uncovered=gaps)


def transcribe_synced_pgm(config, pgm_path, model='small', language='auto',
                         progress=lambda n, message: None, cancelled=lambda: False, sync=None):
    from dialogue_engine import transcribe_media
    state = sync or ensure_synced_pgm(config, pgm_path, progress, cancelled)
    if not valid_sync(config, pgm_path, state):
        raise AnalysisError('声音合板结果已失效，请重新合板后再识别。')
    result = []
    from sound_pairs import group_sound_placements
    placements = group_sound_placements(state['placements'])
    total_duration = sum(group[0]['pgm_end_seconds'] - group[0]['pgm_start_seconds'] for group in placements)
    if total_duration <= 0:
        raise AnalysisError('合板声音没有有效覆盖区间，请重新合板。')
    completed = 0.0
    for index, members in enumerate(placements):
        if cancelled():
            raise AnalysisError('已取消台词识别。')
        preferred = next((row.get('alignment_source_path') for row in members if row.get('alignment_source_path')), '')
        ordered = sorted(members, key=lambda row: row['wav_path'] != preferred) if preferred else members
        row = ordered[0]
        duration = row['pgm_end_seconds'] - row['pgm_start_seconds']
        def report(n, message):
            progress(round((completed + duration * max(0, min(100, n)) / 100) / total_duration * 100),
                     f'原始声音 {index+1}/{len(placements)}：{message}')
        words = []
        for member in ordered:
            row = member
            words = transcribe_media(resolve_local_path(row['wav_path']), model=model, language=language,
                start_seconds=row['wav_start_seconds'], duration=duration, progress=report, cancelled=cancelled)
            if words:
                break  # A split pair covers one dialogue interval, never two.
        for word in words:
            first = max(row['pgm_start_seconds'], float(word['start_seconds']) + row['offset_seconds'])
            last = min(row['pgm_end_seconds'], float(word['end_seconds']) + row['offset_seconds'])
            if last <= first:
                continue
            result.append(dict(word, start_seconds=first, end_seconds=last,
                original_audio_path=row['wav_path'], original_audio_start_seconds=first-row['offset_seconds'],
                original_audio_end_seconds=last-row['offset_seconds']))
        completed += duration
    result.sort(key=lambda row: (row['start_seconds'], row['end_seconds']))
    progress(100, '合板声音台词识别完成；未覆盖的声音区间保持空白。')
    return {'segments': result, 'sync': state}
