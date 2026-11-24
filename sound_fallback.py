"""Legacy PGM analysis provenance and original-WAV-only export audio.

PGM reference sound can support analysis; it is never exported as fallback.
Path identity uses only explicit mounted-directory mappings and lexical macOS aliases.
"""
from __future__ import annotations

import copy
import hashlib
import json
import posixpath
import sys
from functools import lru_cache
from pathlib import Path


def historical_sound_references(config):
    """Read-only evidence for validating old successful WAV mappings only."""
    history = config.get('library_legacy_sound_references') or {}
    active = config.get('sound_reference_overrides') or {}
    merged = dict(history) if isinstance(history, dict) else {}
    if isinstance(active, dict):
        merged.update(active)
    return copy.deepcopy(merged)


def retire_sound_references(config):
    """Remove the setting, keeping only the small historical evidence table."""
    result = dict(config)
    if 'sound_reference_overrides' in result or 'library_legacy_sound_references' in result:
        result['library_legacy_sound_references'] = historical_sound_references(config)
    result.pop('sound_reference_overrides', None)
    return result


def reference_provenance_config(config):
    """Canonical old shape for unchanged project/export fingerprints."""
    result = dict(config)
    if 'library_legacy_sound_references' in result:
        result['sound_reference_overrides'] = historical_sound_references(config)
        result.pop('library_legacy_sound_references', None)
    return result


def sound_path_key_factory():
    from library_bridge import read_settings, remap_path
    def lexical(value):
        if not isinstance(value, str) or not value:
            return ''
        value = posixpath.normpath(value)
        if sys.platform == 'darwin':
            for alias in ('/tmp', '/var', '/etc'):
                if value == alias or value.startswith(alias + '/'):
                    return '/private' + value
        return value
    mappings = [dict(shared=lexical(row.get('shared')), local=lexical(row.get('local')))
                for row in read_settings().get('path_mappings', [])]
    @lru_cache(maxsize=None)
    def key(value):
        value = lexical(value)
        return lexical(remap_path(value, mappings, reverse=True)) if value else ''
    return key


def sound_entry(mapping, path, key=None):
    key = key or sound_path_key_factory()
    found = [value for candidate, value in (mapping or {}).items()
             if key(candidate) == key(path) and isinstance(value, dict)]
    # Conflicting aliases must be reviewed, not resolved by dictionary order.
    return copy.deepcopy(found[0]) if found and all(row == found[0] for row in found) else {}


class SoundStateIndex:
    """Read-only path index for one catalogue/export snapshot.

    Never cache this object across config edits. Values are borrowed from the
    caller's snapshot and must not be mutated; public ``sound_entry`` retains
    its copy-on-read contract. Alias conflicts still return no sound decision.
    """
    FIELDS = ('library_sync', 'library_processing', 'library_sound_fallback', 'library_asr')

    def __init__(self, config, key=None):
        self.config = config
        self.key = key or sound_path_key_factory()
        self.rows, self.values = {}, {}
        for field in self.FIELDS:
            groups = {}
            for path, row in (config.get(field) or {}).items():
                if isinstance(row, dict):
                    groups.setdefault(self.key(path), []).append(row)
            self.rows[field] = groups
            self.values[field] = {path: rows[0] for path, rows in groups.items()
                                  if all(row == rows[0] for row in rows[1:])}

    def entry(self, field, path):
        return self.values.get(field, {}).get(self.key(path), {})

    def aliases(self, field, path):
        return self.rows.get(field, {}).get(self.key(path), ())

    def eligible(self, path):
        if not fallback_enabled(self.config):
            return False
        processing = self.entry('library_processing', path)
        if processing.get('stage') == 'sync' and processing.get('status') in ('failed', 'fallback'):
            return True
        saved = self.entry('library_sound_fallback', path)
        if saved.get('stage') == 'sync' and saved.get('status') == 'fallback' and saved.get('audio_source', 'pgm') == 'pgm':
            return True
        state = self.entry('library_sync', path)
        return state.get('status') == 'fallback' and state.get('audio_source', state.get('source', 'pgm')) == 'pgm'


def fallback_enabled(config):
    # This setting now governs analysis provenance only. Export always uses
    # wav_only_sound_plan, including when an old project still says "pgm".
    return config.get('sound_unmatched_policy', 'pgm') in ('pgm','silence')


def fallback_eligible(config, path, key=None):
    if not fallback_enabled(config):
        return False
    key = key or sound_path_key_factory()
    processing = sound_entry(config.get('library_processing'), path, key)
    if processing.get('stage') == 'sync' and processing.get('status') in ('failed', 'fallback'):
        return True
    saved = sound_entry(config.get('library_sound_fallback'), path, key)
    if saved.get('stage') == 'sync' and saved.get('status') == 'fallback' and saved.get('audio_source', 'pgm') == 'pgm':
        return True
    state = sound_entry(config.get('library_sync'), path, key)
    return state.get('status') == 'fallback' and state.get('audio_source', state.get('source', 'pgm')) == 'pgm'


def _identity(path, key):
    from library_bridge import resolve_local_path
    local = Path(resolve_local_path(path)).expanduser().resolve()
    try:
        value = local.stat()
        return dict(path=key(str(local)), size=value.st_size, mtime_ns=value.st_mtime_ns)
    except OSError:
        return dict(path=key(str(local)), missing=True)


def _fallback_payload(config, path, index=None):
    key = index.canonical if index is not None else sound_path_key_factory()
    def identity(value):
        if index is None:
            return _identity(value, key)
        try:
            return index.identity(value)
        except OSError:
            return dict(path=key(value), missing=True)
    dependencies = [identity(path)]
    dependencies.extend(identity(row['path']) for row in config.get('sound_sources', []) if row.get('path'))
    settings = dict(mode=config.get('sound_sync_mode', 'waveform'),
                    allow_gaps=config.get('sound_allow_gaps', True),
                    sound_layout=sound_layout_settings(config, key=key),
                    reference=None,
                    pgm_timecode=sound_entry({candidate: {'value': value} for candidate, value in
                        (config.get('pgm_timecodes') or {}).items()}, path, key).get('value', ''))
    return dict(version=1, dependencies=sorted(dependencies, key=lambda row: row['path']), settings=settings)


def _fallback_digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def fallback_signature(config, path, pgm_info=None):
    return _fallback_digest(_fallback_payload(config, path))


def valid_pgm_fallback_signature(config, path, saved, index=None):
    """Validate current inputs, with conservative pre-channel-layout support.

    The old fallback schema omitted layout and allow_gaps. It describes using
    this PGM's own audio, not a newly inferred WAV placement. Only unchanged,
    ordinary unpaired sources are eligible; paths and live file identities are
    still included in the exact old digest. Never mutate the saved signature,
    which is also the provenance identity of an existing transcription.
    """
    if not isinstance(saved, dict) or not saved.get('signature'):
        return False
    try:
        payload = _fallback_payload(config, path, index=index)
    except (OSError, KeyError, TypeError, ValueError):
        return False
    if saved['signature'] == _fallback_digest(payload):
        return True
    # The new output policy always leaves missing WAV audio empty. That UI
    # change does not alter a completed analysis attempt's media or sync basis.
    for allow_gaps in (True,False):
        alternate=dict(payload,settings=dict(payload['settings'],allow_gaps=allow_gaps))
        if saved['signature']==_fallback_digest(alternate):return True
    from library_sync import legacy_mono_signature_compatible
    if (any(row.get('missing') for row in payload['dependencies']) or
            not legacy_mono_signature_compatible(config)):
        return False
    legacy = dict(payload, settings={key: value for key, value in payload['settings'].items()
                                    if key not in ('sound_layout', 'allow_gaps')})
    return saved['signature'] == _fallback_digest(legacy)


def valid_pgm_fallback(config, path, allow_legacy=True, index=None):
    """Validate fallback before ASR/export, without reviving manual inputs."""
    states = getattr(index, '_fallback_states', None) if index is not None else None
    if states is None:
        states = SoundStateIndex(config, key=index.canonical if index is not None else None)
        if index is not None:
            index._fallback_states = states
    if not states.eligible(path):
        return False
    signatures = []
    for field in ('library_sound_fallback', 'library_processing', 'library_sync'):
        rows = states.aliases(field, path)
        if rows and any(row != rows[0] for row in rows[1:]):
            return False
        for row in rows[:1]:
            is_failure = (row.get('status') == 'fallback' if field == 'library_sync' else
                row.get('stage') == 'sync' and row.get('status') in ('failed', 'fallback'))
            if is_failure and row.get('signature'):
                signatures.append(row)
    if signatures:
        return all(valid_pgm_fallback_signature(config, path, row, index=index) for row in signatures)
    if any(states.key(candidate) == states.key(path) for candidate in historical_sound_references(config)):
        return False
    return bool(allow_legacy)


def sound_layout_settings(config, used_paths=None, key=None):
    """Channel assignments are synchronization inputs, independent of bytes."""
    from sound_pairs import sound_channel_hint
    key = key or sound_path_key_factory()
    used = None if used_paths is None else {key(path) for path in used_paths}
    records = []
    for row in config.get('sound_sources', []):
        path = key(row.get('path', ''))
        if not path:
            continue
        role = row.get('sound_role') or (row.get('channel') if row.get('channel') in ('L', 'R') else 'auto')
        value = dict(path=path, role=role, pair=row.get('sound_pair', '') if role in ('L', 'R') else '')
        hint, family = sound_channel_hint(path)
        filename = (str(Path(path).parent), family.casefold()) if family and role == 'auto' else None
        ixml = (row.get('metadata') or {}).get('ixml') or {}
        family_uid = row.get('family_uid') or ixml.get('FILE_SET/FAMILY_UID') or ixml.get('FAMILY_UID') or ''
        if not isinstance(family_uid, str):
            family_uid = ''
        records.append((value, filename, family_uid if role == 'auto' else ''))
    relevant = records if used is None else [row for row in records if row[0]['path'] in used]
    filenames = {row[1] for row in relevant if row[1]}
    families = {row[2] for row in relevant if row[2]}
    pairs = {row[0]['pair'] for row in relevant if row[0]['pair']}
    # A formerly standalone L becomes a paired recording when R is appended.
    # Include only related siblings; unrelated additions retain cache hits.
    result = [value for value, filename, family in records if used is None or value['path'] in used
              or filename in filenames or (family and family in families)
              or (value['pair'] and value['pair'] in pairs)]
    return sorted(result, key=lambda row: (row['path'], row['role'], row['pair']))


def make_pgm_fallback(config, path, error, pgm_info=None):
    from core import probe
    from library_bridge import resolve_local_path
    local = str(Path(resolve_local_path(path)).expanduser().resolve())
    key = sound_path_key_factory()
    info = copy.deepcopy(pgm_info if pgm_info is not None else probe(local))
    info['path'] = key(path)
    return dict(version=1, status='fallback', stage='sync', audio_source='pgm',
                error=str(error), pgm_path=key(path), source_stat=_identity(local, key),
                signature=fallback_signature(config, path, info), pgm_info=info)


class AudioSources(list):
    """Retain real gap reports even when there are no actual audio assets."""
    def __init__(self, values=(), unattached_reports=()):
        super().__init__(values)
        self.unattached_reports = list(unattached_reports)


def sound_reports(sources):
    return [row for source in sources for row in source.get('sync_report', [])] + list(
        getattr(sources, 'unattached_reports', ()))


def source_start(source):
    from fractions import Fraction
    if source.get('source_kind') == 'pgm':
        return Fraction(source['start_frames'], source['fps'])
    return Fraction(source['start'], source.get('sample_rate', 48000))


def source_duration(source):
    from fractions import Fraction
    if source.get('source_kind') == 'pgm':
        return Fraction(source['frames'], source['fps'])
    return Fraction(source['samples'], source.get('sample_rate', 48000))


def is_original_wav_source(source):
    """Classify saved source declarations without opening or guessing media."""
    from urllib.parse import urlsplit,unquote
    if not isinstance(source,dict) or source.get('source_kind','wav')!='wav':return False
    path=source.get('path','')
    return bool(path and Path(unquote(urlsplit(str(path)).path)).suffix.lower() in ('.wav','.wave'))


def wav_only_sound_plan(sound=None,cut=None,fps=None):
    """Return a detached export plan containing only actually used main WAVs.

    Old PGM events stay in the project/cache, but become explicit silent ranges
    in the next export. Returning an empty plan rather than None also prevents
    a writer from reviving embedded proxy-camera audio.
    """
    from fractions import Fraction as F
    original_sources,original_events=sound if sound is not None else ((),())
    by_path={row['path']:row for row in original_sources if is_original_wav_source(row)}
    events=[];removed=[]
    for row in original_events:
        if (row.get('path') in by_path and row.get('source_kind','wav')=='wav'):
            events.append(copy.deepcopy(row))
        else:
            removed.append(dict(status='uncovered',source_kind='none',pgm_path=row.get('pgm_path',''),
                record_start_seconds=float(row['record_start']),duration_seconds=float(row['duration']),
                reason='原始 WAV 未覆盖此区间；音频留空。'))
    reports=[]
    for row in sound_reports(original_sources):
        if row.get('source_kind')=='pgm' or row.get('status')=='fallback':continue
        reports.append(copy.deepcopy(row))
    if cut is None or not fps:reports.extend(removed)
    if cut is not None and fps:
        # Merge once, including paired-mono duplicate spans, rather than scan
        # all audio events for every PGM in a large recording catalogue.
        from bisect import bisect_right
        coverage=[]
        for first,last in sorted((F(row['record_start']),F(row['record_start'])+F(row['duration'])) for row in events):
            if coverage and first<=coverage[-1][1]:coverage[-1]=(coverage[-1][0],max(last,coverage[-1][1]))
            else:coverage.append((first,last))
        coverage_ends=[last for _,last in coverage]
        for block in cut.get('pgm_sources',[]):
            first=F(block['start'],fps);end=first+F(block['frames'],fps)
            cursor=first
            position=bisect_right(coverage_ends,first)
            for index in range(position,len(coverage)):
                begin,last=coverage[index]
                if begin>=end:break
                a,b=max(first,begin),min(end,last)
                if a>cursor:reports.append(dict(status='uncovered',source_kind='none',pgm_path=block['path'],
                    record_start_seconds=float(cursor),duration_seconds=float(a-cursor),
                    reason='原始 WAV 未覆盖此区间；音频留空。'))
                cursor=max(cursor,b)
            if cursor<end:reports.append(dict(status='uncovered',source_kind='none',pgm_path=block['path'],
                record_start_seconds=float(cursor),duration_seconds=float(end-cursor),
                reason='原始 WAV 未覆盖此区间；音频留空。'))
    unique=[];seen=set()
    for row in reports:
        identity=(row.get('status'),row.get('pgm_path'),row.get('record_start_seconds'),row.get('duration_seconds'),row.get('path'))
        if identity not in seen:seen.add(identity);unique.append(row)
    used={row['path'] for row in events}
    sources=[]
    for path,row in by_path.items():
        if path not in used:continue
        value=copy.deepcopy(row);value['source_kind']='wav';value['sync_report']=[];sources.append(value)
    if sources:sources[0]['sync_report']=unique
    return AudioSources(sources,unattached_reports=unique if not sources else ()),events


def valid_completed_sync_gap(config,path,index=None):
    """Accept a verified historical failed attempt as deliberate output silence."""
    legacy=dict(config,sound_unmatched_policy='pgm')
    return valid_pgm_fallback(legacy,path,index=index)
