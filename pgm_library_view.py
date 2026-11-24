"""Prepare one lightweight, current pending-library row per PGM file.

This is an immutable-snapshot view. It performs no media/Qt/database access,
does not map proxy/audio metadata, and never converts a source-local dialogue
anchor into the whole file's display IN of zero.
"""
from collections import defaultdict
from hashlib import sha256
from pathlib import Path
import math

from library_pending import is_manual_visual


def _seconds(row, field, default=0.):
    try:
        value = float(row.get(field, default))
        return value if math.isfinite(value) else default
    except (TypeError, ValueError, OverflowError):
        return default


def prepare_pgm_inventory(sessions, transcripts, candidates, pending_intervals, take_index):
    """One immutable, disjoint inventory of whole files across all sessions.

    Every registered PGM is present, even before ASR. Current script coverage
    places a file on a track independently of the automatic confidence score.
    Explicit track removal overrides a later rematch until manual placement.
    All UI counts, pools and track colors must derive from this same snapshot.
    """
    session_names = {row.get('id'): row.get('name', '') for row in sessions}
    session_order = {row.get('id'): index for index, row in enumerate(sessions)}
    # PgmTakeIndex already owns source validity, current ASR IDs and PGM-kind
    # checking. Inactive historical records must not revive a pending file.
    current_ids = {row.get('id') for values in take_index.rows.values() for row in values
                   if row.get('active', True)}
    speech_by_key, intervals_by_key = defaultdict(list), defaultdict(list)
    for row in transcripts:
        if (row.get('id') not in current_ids or not row.get('active', True)
                or is_manual_visual(row)):
            continue
        key = take_index.key(row)
        if key[0] and key[1]:
            speech_by_key[key].append(row)
    for row in pending_intervals:
        if not row.get('active', True) or is_manual_visual(row):
            continue
        key = take_index.key(row)
        if key[0] and key[1]:
            intervals_by_key[key].append(row)
    proposals, active, confirmed = defaultdict(list), set(), set()
    valid_candidates = take_index.candidates
    for row in candidates:
        identity = row.get('id')
        transcript_id = row.get('transcript_id')
        if (identity not in valid_candidates or not row.get('active', True)
                or not row.get('script_active', True)
                or transcript_id and transcript_id not in current_ids):
            continue
        key = take_index.key(row)
        state = row.get('status')
        if state != 'rejected':
            active.add(key)
        if state == 'candidate':
            proposals[key].append(row)
        elif state == 'confirmed':
            confirmed.add(key)
    from library_pending import _registered_pgms
    from pgm_editor_state import read_editor_state
    session_configs = {row['id']: row.get('config') or {} for row in sessions}
    registered = {(row['id'], str(Path(path).expanduser())) for row in sessions
                  for path in _registered_pgms(row.get('config') or {}) if path}
    keys = registered | set(take_index.rows) | set(intervals_by_key)
    rows, flags = [], {}
    for key in sorted(keys, key=lambda value: (
            session_order.get(value[0], len(session_order)), value[1].casefold(), value[1])):
        speech = sorted(speech_by_key.get(key, ()), key=lambda row: (
            _seconds(row, 'start_seconds'), _seconds(row, 'end_seconds'), str(row.get('id', ''))))
        intervals = intervals_by_key.get(key, ())
        fallback = intervals or take_index.rows.get(key, ())
        source = speech[0] if speech else min(fallback, key=lambda row: (
            _seconds(row, 'start_seconds'), _seconds(row, 'end_seconds'), str(row.get('id', '')))) if fallback else dict(
                id='registered-pgm-'+sha256((str(key[0])+'\0'+key[1]).encode()).hexdigest()[:24],
                session_id=key[0], media_path=key[1], kind='pgm', manual_pending=True,
                start_seconds=0., end_seconds=0., reason='尚未匹配台本')
        summary = take_index.file_summary(source)
        first = summary.get('first_candidate')
        editor = read_editor_state(session_configs.get(key[0], {}), key[1], summary=summary,
                                   source=take_index.source(first) if first else None)
        if editor['hidden_in_pool'] and editor['pool'] != 'timeline':
            continue
        duration = take_index.duration(source)
        row = dict(source)
        row.update(session_name=source.get('session_name') or session_names.get(key[0], ''),
                   pgm_path=source.get('pgm_path') or source.get('media_path', ''),
                   dialogue_start_seconds=source.get('dialogue_start_seconds', source.get('start_seconds')),
                   dialogue_end_seconds=source.get('dialogue_end_seconds', source.get('end_seconds')),
                   pgm_match_status=summary['match_status'], pgm_match_score=summary['match_score'],
                   editor_pool=editor['pool'], editor_reviewed=editor['reviewed'],
                   covered_line_ids=summary['covered_line_ids'],
                   alignment_ready=summary['alignment_ready'],
                   alignment_reason=summary['alignment_reason'])
        if duration is not None:
            row.update(whole_pgm=True, start_seconds=0., end_seconds=float(duration))
        else:
            row['whole_pgm'] = False
        reasons = list(dict.fromkeys(candidate.get('review_reason') for candidate in proposals[key]
                                     if candidate.get('review_reason')))
        brief = ('PGM缺少实际帧率／帧数，请核对素材登记。' if duration is None else
                 summary['alignment_reason'] if summary['match_status'] == 'matched' else
                 '；'.join(reasons[:2]) or source.get('reason') or summary['match_reason'])
        search_parts = [row.get('session_name', ''), key[1], source.get('reason', ''), brief]
        search_parts.extend(str(item.get('text', '')) for item in speech)
        flags[key] = dict(proposed=bool(proposals[key]), active=key in active,
                          confirmed=key in confirmed, proposals=proposals[key],
                          searchable=' '.join(str(part) for part in search_parts if part).casefold(),
                          brief_reason=brief, duration_missing=duration is None,
                          transcript_count=len(speech),match_score=summary['match_score'],
                          alignment_ready=summary['alignment_ready'],alignment_reason=summary['alignment_reason'],
                          processing=session_configs.get(key[0],{}).get('library_processing',{}).get(key[1],{}),
                          script_run=session_configs.get(key[0],{}).get('library_script_run',{}),**editor)
        rows.append(row)
    return rows, flags


def prepare_pending_files(sessions, transcripts, candidates, pending_intervals, take_index):
    """Compatibility view of the two media pools; placed files are excluded."""
    rows, flags = prepare_pgm_inventory(sessions, transcripts, candidates, pending_intervals, take_index)
    rows = [row for row in rows if flags[take_index.key(row)]['pool'] != 'timeline']
    return rows, {take_index.key(row): flags[take_index.key(row)] for row in rows}


EDITOR_STATUS_LABELS = {'unreviewed': '轨道待核实', 'reviewed': '轨道已核实',
                        'unmatched': '未匹配台本', 'removed': '轨道移出'}


def inventory_status_rows(rows, flags, take_index):
    return [dict(session_id=take_index.key(row)[0], session_name=row.get('session_name', ''),
        media_path=take_index.key(row)[1], status=(
            'reviewed' if flags[take_index.key(row)]['reviewed'] else 'unreviewed')
            if flags[take_index.key(row)]['pool'] == 'timeline' else flags[take_index.key(row)]['pool'],
        reason=flags[take_index.key(row)]['brief_reason'],
        transcript_count=flags[take_index.key(row)].get('transcript_count',0),
        match_score=flags[take_index.key(row)].get('match_score',0.),
        alignment_ready=flags[take_index.key(row)].get('alignment_ready',False),
        alignment_reason=flags[take_index.key(row)].get('alignment_reason',''),
        processing=flags[take_index.key(row)].get('processing',{}),
        script_run=flags[take_index.key(row)].get('script_run',{})) for row in rows]


def inventory_status_text(rows, session_id=None):
    current = [row for row in rows if not session_id or row['session_id'] == session_id]
    return ' · '.join(f"{label} {sum(row['status'] == key for row in current)}"
                    for key, label in EDITOR_STATUS_LABELS.items())


def review_snapshot_compatible(before, after, target_session_id, target_path):
    """Worker-side guard for a color-only GUI commit; fail closed on any edit."""
    for name in ('scripts', 'transcripts', 'candidates', 'notes', 'reference_state'):
        if before.get(name, [] if name != 'reference_state' else {}) != after.get(name, [] if name != 'reference_state' else {}):
            return False
    old, new = before.get('sessions', []), after.get('sessions', [])
    if len(old) != len(new) or [row.get('id') for row in old] != [row.get('id') for row in new]:
        return False
    for a, b in zip(old, new):
        if a['id'] != target_session_id:
            if a != b:
                return False
            continue
        for key in set(a) | set(b):
            if key in ('revision', 'updated_at', 'updated_by'):
                continue
            if key == 'config':
                ac, bc = a.get(key) or {}, b.get(key) or {}
                ae, be = ac.get('library_pgm_editor') or {}, bc.get('library_pgm_editor') or {}
                if {k:v for k,v in ae.items() if k != target_path} != {k:v for k,v in be.items() if k != target_path}:
                    return False
                if {k:v for k,v in ac.items() if k != 'library_pgm_editor'} != {k:v for k,v in bc.items() if k != 'library_pgm_editor'}:
                    return False
            elif a.get(key) != b.get(key):
                return False
    return any(row.get('id') == target_session_id for row in new)
