"""Build a script-ordered story browser from existing catalogue decisions.

Nodes are narrative order, never a generated edit or a cross-performance clock.
Only stored dialogue associations and true source-local clip positions are used.
"""
from __future__ import annotations

import copy
import math

from library_correspondence import filter_active_transcripts
from library_angles import AngleMapper


def _order(value, fallback):
    try:
        number = float(value)
        return number if math.isfinite(number) else fallback
    except (ValueError, TypeError, OverflowError):
        return fallback


def _clip(candidate, transcript, sessions, memo=None):
    source = transcript if transcript is not None else candidate
    identity = candidate.get('transcript_id') or source.get('id')
    session_id = source.get('session_id') or candidate.get('session_id')
    path = source.get('media_path') or candidate.get('media_path')
    try:
        first, last = source['start_seconds'], source['end_seconds']
        if not (identity and session_id and isinstance(path, str) and path and
                math.isfinite(float(first)) and math.isfinite(float(last)) and 0 <= float(first) < float(last)):
            return None
    except (KeyError, ValueError, TypeError, OverflowError):
        # An orphan/invalid association cannot become a fabricated playable clip.
        return None
    row = copy.deepcopy(candidate, memo)
    row.update(candidate_id=candidate.get('id'), transcript_id=identity,
               session_id=session_id, session_name=sessions.get(session_id, {}).get('name') or
               source.get('session_name') or candidate.get('session_name', ''),
               media_path=path, start_seconds=first, end_seconds=last,
               transcript_text=source.get('text', source.get('transcript_text', candidate.get('transcript_text', ''))),
               transcript_speaker=source.get('speaker', source.get('transcript_speaker', candidate.get('transcript_speaker', ''))))
    if 'metadata' in source:
        row['metadata'] = copy.deepcopy(source['metadata'], memo)
    # Preserve import/sync details such as original_audio_path without replacing
    # candidate ID/status or inventing proxy and audio coverage for this clip.
    for key in ('kind', 'original_audio_path', 'original_audio_start_seconds', 'original_audio_end_seconds'):
        if key in source:
            row[key] = copy.deepcopy(source[key], memo)
    return row


def build_timeline_data(data, *, prepared=False, whole_pgm=False):
    """Return one stable node for every active script line, including empty ones.

    Each node contains id/script_line_id/line_id, script_id, scene, speaker,
    text, order, count, confirmed_count, candidate_count, session_count,
    session_ids, session_names and clips. Clips retain stored candidate fields
    and source-local positions, with related_angles describing only registered
    same-session source-clock intersections. Node angle_count/camera_count are
    separate from count: extra camera angles never become extra performances.
    Duplicate associations to the same transcript in one line count once;
    confirmed takes precedence over an unverified row.

    Script groups keep their input order. Within each script, numeric line order
    is stable, so a repeated scene heading never merges or relocates its lines.
    This read-only function does not infer dialogue, waveform synchronization,
    cross-performance alignment, or cuts.
    """
    current = data if prepared else filter_active_transcripts(data)
    sessions = {row['id']: row for row in current.get('sessions', []) if row.get('id')}
    angle_mapper = None if prepared else AngleMapper(sessions.values())
    session_rank = {identity: index for index, identity in enumerate(sessions)}
    transcripts = {row['id']: row for row in current.get('transcripts', []) if row.get('id')}
    groups, seen_lines = {}, set()
    for index, line in enumerate(current.get('scripts', [])):
        identity = line.get('id') or line.get('script_line_id')
        if not identity or identity in seen_lines or not line.get('active', line.get('script_active', True)):
            continue
        seen_lines.add(identity)
        script_id = line.get('script_id') or ''
        groups.setdefault(script_id, []).append((index, identity, line))
    nodes = []
    for lines in groups.values():
        for index, identity, line in sorted(lines, key=lambda entry: (
                _order(entry[2].get('order', entry[2].get('line_order')), entry[0]), entry[0])):
            node = copy.deepcopy(line)
            node.update(id=identity, script_line_id=identity, line_id=identity,
                        script_id=line.get('script_id', ''), order=line.get('order', line.get('line_order', index)),
                        scene=line.get('scene', ''), speaker=line.get('speaker', ''), text=line.get('text', ''), clips=[])
            nodes.append(node)
    by_line = {row['id']: row for row in nodes}
    selected = {row['id']: {} for row in nodes}
    from pgm_takes import PgmTakeIndex
    take_index = PgmTakeIndex(current, prepared=True) if whole_pgm else None
    reference = (data.get('reference_state') or {}).get('reference_session_id')
    whole_mapper = None
    seen_files = set()
    # Candidate records are independent, but their immutable full-scene path
    # inventories can be shared. A separate memo per refresh prevents stale
    # state while keeping one copied list instead of N × all imported paths.
    clip_memo = {}
    for candidate in current.get('candidates', []):
        line_id = candidate.get('script_line_id')
        if line_id not in by_line or candidate.get('status') not in ('candidate', 'confirmed') or not candidate.get('active', True) or not candidate.get('script_active', True):
            continue
        transcript = transcripts.get(candidate.get('transcript_id'))
        if transcript is not None and not transcript.get('active', True):
            continue
        summary, duration = None, None
        source = transcript if transcript is not None else candidate
        session_id = source.get('session_id') or candidate.get('session_id')
        if take_index and session_id != reference:
            duration = take_index.duration(candidate)
            if duration is not None:
                summary = take_index.file_summary(candidate)
                representative = summary['display_candidate']
                if representative is None or representative['id'] != candidate['id']:
                    # Most utterance candidates are not displayed individually.
                    # Avoid deep-copying their potentially large angle metadata.
                    continue
        clip = _clip(candidate, transcript, sessions, clip_memo)
        if clip is None:
            continue
        if summary is not None:
            key = take_index.key(candidate)
            if key in seen_files:
                continue
            seen_files.add(key)
            clip.update(whole_pgm=True, dialogue_start_seconds=clip['start_seconds'],
                        dialogue_end_seconds=clip['end_seconds'], start_seconds=0.,
                        end_seconds=float(duration),
                        pgm_match_status=summary['match_status'], pgm_match_score=summary['match_score'],
                        alignment_ready=summary['alignment_ready'], alignment_reason=summary['alignment_reason'])
            clip['transcript_text'] = '\n'.join(row['text'] for row in take_index.rows[key])
            if whole_mapper is None:
                whole_mapper = AngleMapper(sessions.values())
            clip.update(whole_mapper.fields(clip))
        elif angle_mapper is not None:
            clip.update(angle_mapper.fields(clip, transcript))
        else:
            # The workflow has already enriched these exact catalogue rows.
            # Reuse its relations instead of mapping every proxy a second time.
            clip.setdefault('related_angles', [])
            clip.setdefault('related_angle_count', len(clip['related_angles']))
        clips = selected[line_id]
        previous = clips.get(clip['transcript_id'])
        if previous is None or previous['status'] != 'confirmed' and clip['status'] == 'confirmed':
            clips[clip['transcript_id']] = clip
    for node in nodes:
        clips = list(selected[node['id']].values())
        # Session grouping is presentation only. Source file/local IN are kept
        # separate; no clock conversion or cross-performance alignment occurs.
        clips.sort(key=lambda row: (session_rank.get(row['session_id'], len(session_rank)),
                                    row['media_path'], float(row['start_seconds']), float(row['end_seconds'])))
        node['clips'] = clips
        node['count'] = len(clips)
        node['confirmed_count'] = sum(row['status'] == 'confirmed' for row in clips)
        node['candidate_count'] = sum(row['status'] == 'candidate' for row in clips)
        node['session_ids'] = list(dict.fromkeys(row['session_id'] for row in clips))
        names = {row['session_id']: row['session_name'] for row in clips}
        node['session_names'] = [names[identity] for identity in node['session_ids']]
        node['session_count'] = len(node['session_ids'])
        node['angle_count'] = sum(row['related_angle_count'] for row in clips)
        node['camera_count'] = len({angle['camera'] for row in clips for angle in row['related_angles']})
    return nodes
