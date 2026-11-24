"""PGM-defined story coverage, independent of clocks in different performances.

A file is an indivisible take. Script ranges describe browsing only and never
trim media. Contained ranges may overlay across sessions; partial intersections
and repeated takes within one session stay distinct.
"""
from hashlib import sha256
import copy
import json
from pathlib import Path

from library_correspondence import filter_active_transcripts
from library_pending import _registered_pgms, is_manual_visual
from library_timeline import _clip, build_timeline_data
from library_angles import AngleMapper
from pgm_takes import PgmTakeIndex


def build_pgm_intervals(data, *, prepared=False, script_nodes=None):
    current = data if prepared else filter_active_transcripts(data)
    index = PgmTakeIndex(current, prepared=True)
    if script_nodes is None:
        script_nodes = build_timeline_data(dict(current, candidates=[]), prepared=True)
    positions = {row['id']: n for n, row in enumerate(script_nodes)}
    lines = {row['id']: row for row in script_nodes}
    mapper = AngleMapper(index.sessions.values())
    files, clip_memo = [], {}
    for session in current.get('sessions', []):
        paths = list(dict.fromkeys(_registered_pgms(session.get('config') or {}) +
                                  [key[1] for key in index.rows if key[0] == session['id']]))
        for path in paths:
            key = (session['id'], str(Path(path).expanduser()))
            summary = index.file_summary(dict(session_id=key[0], media_path=key[1]))
            representative = summary['display_candidate']
            # Coverage uses one current best association per utterance. A rejected
            # or superseded association cannot stretch a file's script interval.
            coverage = summary['coverage_candidates']
            covered_line_ids = summary['covered_line_ids']
            if representative:
                family = lines[representative['script_line_id']].get('script_id', '')
                coverage = [row for row in coverage if lines[row['script_line_id']].get('script_id', '') == family]
                covered_line_ids = [identity for identity in covered_line_ids if identity in lines
                                    and lines[identity].get('script_id', '') == family]
            else:
                family = ''
                coverage = []
                covered_line_ids = []
            span = sorted(positions[identity] for identity in covered_line_ids)
            clip = None
            if representative:
                source = index.source(representative)
                clip = _clip(representative, source, index.sessions, memo=clip_memo)
                duration = index.duration(representative)
                if clip and duration:
                    clip.update(whole_pgm=True, dialogue_start_seconds=clip['start_seconds'],
                                dialogue_end_seconds=clip['end_seconds'], start_seconds=0., end_seconds=float(duration),
                                manual_anchor=is_manual_visual(source))
                    clip['transcript_text'] = '\n'.join(row['text'] for row in index.rows[key])
                    clip.update(mapper.fields(clip))
                if clip:
                    # Coverage is known independently of media duration. Even
                    # pending metadata must not give a short take the grouped
                    # baseline's broader script range.
                    clip['covered_line_ids'] = list(covered_line_ids)
            state, reason = summary['match_status'], summary['match_reason']
            if clip:
                clip.update(pgm_match_status=state, pgm_match_score=summary['match_score'],
                            alignment_ready=summary['alignment_ready'],
                            alignment_reason=summary['alignment_reason'])
            files.append(dict(key=key, match_status=state, match_reason=reason,
                              match_score=summary['match_score'], alignment_ready=summary['alignment_ready'],
                              alignment_reason=summary['alignment_reason'], first=span[0] if span else None, last=span[-1] if span else None,
                              family=family, clip=clip, path=path, session_name=session.get('name', '')))
    from pgm_range_index import CoverageGroupIndex
    groups, group_index = [], CoverageGroupIndex()
    for take in files:
        if take['first'] is None:
            groups.append(dict(first=None, last=None, family='', takes=[take]))
            continue
        compatible = group_index.compatible(take)
        if len(compatible) == 1:
            group = compatible[0]
            group['first'], group['last'] = min(group['first'], take['first']), max(group['last'], take['last'])
            group['takes'].append(take)
            group_index.changed(group)
        else:
            # Repeated lines/overlapping coverage may fit several existing takes.
            # Keep a separate node rather than silently choosing an occurrence.
            group = dict(first=take['first'], last=take['last'], family=take['family'], takes=[take])
            groups.append(group)
            group_index.add(group)
    nodes, output_memo = [], {}
    for number, group in enumerate(groups):
        takes, first, last = group['takes'], group['first'], group['last']
        keys = [list(take['key']) for take in takes]
        # The original file identity survives incremental overlays and range growth.
        identity = 'pgm-axis-' + sha256(json.dumps(keys[0], ensure_ascii=False).encode()).hexdigest()[:24]
        clips = [copy.deepcopy(take['clip'], output_memo) for take in takes if take['clip']]
        if first is None:
            text, scene, family = '待人工定位 · '+Path(takes[0]['path']).name, '未定位 PGM', ''
            line_ids = []
        else:
            line_ids = [row['id'] for row in script_nodes[first:last+1] if row.get('script_id', '') == group['family']]
            start, end = script_nodes[first], script_nodes[last]
            text = f'台本 {first+1}—{last+1} · '+start['text']
            if last != first:
                text += '\n至：'+end['text']
            text += '\n'+' / '.join(Path(take['path']).name for take in takes)
            scene, family = start.get('scene', ''), group['family']
        session_ids = list(dict.fromkeys(take['key'][0] for take in takes))
        nodes.append(dict(id=identity, script_line_id=identity, script_id=family, scene=scene,
            order=first if first is not None else len(script_nodes)+number, text=text, speaker='',
            anchor_line_id=clips[0].get('script_line_id') if clips else None,
            covered_line_ids=line_ids, coverage_start=first, coverage_end=last,
            file_keys=keys, file_statuses=[dict(session_id=t['key'][0], session_name=t['session_name'], media_path=t['path'],
                status=t['match_status'], reason=t['match_reason'], match_score=t['match_score'],
                alignment_ready=t['alignment_ready'], alignment_reason=t['alignment_reason']) for t in takes],
            pgm_count=len(takes), clips=clips, count=len(clips),
            confirmed_count=sum(row['status'] == 'confirmed' for row in clips),
            candidate_count=sum(row['status'] == 'candidate' for row in clips) + sum(take['clip'] is None for take in takes),
            session_ids=session_ids, session_names=list(dict.fromkeys(take['session_name'] for take in takes)),
            session_count=len(session_ids), angle_count=sum(row.get('related_angle_count', 0) for row in clips),
            camera_count=len({angle['camera'] for row in clips for angle in row.get('related_angles', [])})))
    return sorted(nodes, key=lambda row: row['order'])


STATUS_LABELS = {'matched': '已匹配', 'review': '待核实', 'unmatched': '未匹配'}


def file_status_rows(nodes):
    return [dict(row) for node in nodes for row in node.get('file_statuses', [])]


def status_text(rows, session_id=None):
    current = [row for row in rows if not session_id or row['session_id'] == session_id]
    return 'PGM：' + ' · '.join(f"{label} {sum(row['status'] == state for row in current)}" for state,label in STATUS_LABELS.items())
