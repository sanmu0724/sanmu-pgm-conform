"""Deterministic script partitions of an existing continuous reference edit.

Only saved ASR/manual associations are read. No media is opened, re-transcribed,
retimed or rewritten. Coverage edges and an observed dialogue onset are separate:
covering silent material must never move an onset to an invented timestamp.
"""
from __future__ import annotations

from collections import defaultdict
import copy
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path


VERSION = 1
_BEAM = 32
_CHOICES = 8


def _path(value):
    return str(Path(value).expanduser()) if value else ''


def _integer(value, label):
    try:
        number = Fraction(str(value))
        if number.denominator != 1:
            raise ValueError()
        return int(number)
    except (TypeError, ValueError, ZeroDivisionError, OverflowError) as exc:
        raise ValueError(label + '必须是整数帧。') from exc


def _cut(cut):
    if not isinstance(cut, dict):
        raise ValueError('请先载入基准场次的切点 XML。')
    fps = _integer(cut.get('fps'), '基准帧率')
    duration = _integer(cut.get('duration'), '基准时长')
    if fps not in (24, 25, 30) or duration <= 0:
        raise ValueError('基准帧率或时长无效。')
    blocks, cursor, paths = [], 0, set()
    for row in cut.get('pgm_sources') or []:
        path = _path(row.get('path'))
        first = _integer(row.get('start'), 'PGM 入点')
        last = _integer(row.get('end'), 'PGM 出点')
        frames = _integer(row.get('frames'), 'PGM 时长')
        if not path or path in paths or first != cursor or first >= last or last-first != frames:
            raise ValueError('基准 PGM 覆盖不连续、重复或时长不一致。')
        paths.add(path)
        blocks.append(dict(row, path=path, start=first, end=last, frames=frames))
        cursor = last
    if cursor != duration:
        raise ValueError('基准 PGM 覆盖与切点时间线时长不一致。')
    return dict(cut, fps=fps, duration=duration, pgm_sources=blocks)


def _catalogue(data):
    from library_correspondence import filter_active_transcripts
    from pgm_takes import PgmTakeIndex
    active = filter_active_transcripts(data)
    return active, PgmTakeIndex(active, prepared=True)


def _annotation(source):
    result = {}
    for row in (source.get('data'), source.get('metadata'),
                (source.get('data') or {}).get('metadata') if isinstance(source.get('data'), dict) else None):
        if isinstance(row, dict):
            for key in ('source_kind', 'manual_script_line_ids', 'manual_range_explicit'):
                if key in row:
                    result[key] = row[key]
    return result


def _source_records(data, reference_session_id, cut, index):
    """Position the current source-local rows through the actual cut blocks."""
    blocks = {row['path']: row for row in cut['pgm_sources']}
    sources = {row.get('id'): row for row in data.get('transcripts', []) if row.get('id')}
    # Legacy snapshots may carry source fields directly on a candidate.
    for candidate in data.get('candidates', []):
        if candidate.get('transcript_id') not in sources and candidate.get('media_path'):
            identity = candidate.get('transcript_id') or candidate.get('id')
            sources.setdefault(identity, dict(candidate, id=identity))
    result = []
    for identity, source in sources.items():
        if source.get('session_id') != reference_session_id or not source.get('active', True):
            continue
        path = _path(source.get('media_path'))
        block = blocks.get(path)
        if block is None:
            continue
        try:
            start = Fraction(str(source.get('start_seconds')))
            end = Fraction(str(source.get('end_seconds')))
            if start < 0 or end <= start:
                continue
            local_first = math.floor(start*cut['fps'])
            local_last = math.ceil(end*cut['fps'])
            if local_first >= block['frames'] or local_last > block['frames']:
                continue
        except (TypeError, ValueError, ZeroDivisionError, OverflowError):
            continue
        result.append(dict(id=identity, source=source, path=path,
                           start=block['start']+local_first,
                           end=block['start']+local_last,
                           exact_start=Fraction(block['start'])+start*cut['fps'],
                           exact_end=Fraction(block['start'])+end*cut['fps']))
    result.sort(key=lambda row: (row['start'], row['end'], str(row['id'])))
    return result


def _fingerprint(data, reference_session_id, cut, index, records):
    """Serialize only alignment evidence; no volatile UI/editor settings."""
    script_rows = [dict(id=identity, script_id=index.script_lines[identity].get('script_id', ''),
                        order=index.script_positions[identity],
                        text=index.script_lines[identity].get('text', ''),
                        speaker=index.script_lines[identity].get('speaker', ''),
                        scene=index.script_lines[identity].get('scene', ''))
                   for identity in index.script_order]
    identities = {row['id'] for row in records}
    candidate_rows = []
    for row in index.candidates.values():
        if row.get('transcript_id') in identities or row.get('id') in identities:
            candidate_rows.append({key: row.get(key) for key in
                ('id', 'transcript_id', 'script_line_id', 'status', 'score',
                 'auto_review_score', 'active', 'script_active', 'tags')})
    candidate_rows.sort(key=lambda row: str(row['id']))
    source_rows = [dict(id=row['id'], media_path=row['path'], start=row['start'], end=row['end'],
                        start_seconds=str(row['source'].get('start_seconds')),
                        end_seconds=str(row['source'].get('end_seconds')),
                        text=row['source'].get('text', ''), annotation=_annotation(row['source']))
                   for row in records]
    from library_correspondence import PGMInfoIndex
    config = index.sessions.get(reference_session_id, {}).get('config') or {}
    headers = PGMInfoIndex(config).headers
    source_headers = [{key: dict(headers.get(row['path'], {}), path=row['path']).get(key) for key in
                       ('path', 'frames', 'fps', 'start_tc', 'timecode', 'size', 'mtime_ns', 'source_stat')}
                      for row in cut['pgm_sources']]
    blocks = [{key: row.get(key) for key in
               ('path', 'start', 'end', 'frames', 'start_tc', 'fps', 'size', 'mtime_ns', 'source_stat')}
              for row in cut['pgm_sources']]
    value = dict(version=VERSION, reference_session_id=reference_session_id,
                 fps=cut['fps'], duration=cut['duration'], blocks=blocks, source_headers=source_headers,
                 scripts=script_rows, sources=source_rows, candidates=candidate_rows)
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), default=str).encode('utf-8')).hexdigest()


def baseline_map_fingerprint(data, reference_session_id, cut):
    """Invalidate on active script/ASR/association/cut changes, never green state."""
    cut = _cut(cut)
    active, index = _catalogue(data)
    records = _source_records(active, reference_session_id, cut, index)
    return _fingerprint(active, reference_session_id, cut, index, records)


@dataclass(slots=True)
class _Node:
    score: float
    last: int
    previous: object = None
    choice: object = None
    row: int = -1


def _options(record, index, by_source, script_relative=None, duration=None):
    output, seen = [], set()
    for candidate in by_source.get(record['id'], []):
        if candidate.get('status') not in ('candidate', 'confirmed'):
            continue
        annotation = _annotation(record['source'])
        manual_span = index._manual_coverage(candidate, strict=True)
        ids = manual_span or index.coverage_ids(candidate)
        if not ids:
            continue
        positions = [index.script_positions[identity] for identity in ids]
        family = index.script_lines[ids[0]].get('script_id', '')
        if (positions != list(range(positions[0], positions[-1]+1)) or
                any(index.script_lines[identity].get('script_id', '') != family for identity in ids)):
            continue
        tags = set(candidate.get('tags') or [])
        machine = tags.intersection(('auto_dialogue', 'automatic_dialogue'))
        manual = candidate.get('status') == 'confirmed' and (not machine or bool(
            tags - {'auto_dialogue', 'automatic_dialogue', 'auto_match_superseded'} -
            {value for value in tags if str(value).startswith('auto_script_span:')}))
        try:
            score = float(candidate.get('auto_review_score', candidate.get('score', 0)))
            if not math.isfinite(score) or not 0 <= score <= 1:
                score = 0.
        except (ValueError, TypeError, OverflowError):
            score = 0.
        if manual:
            score = 1.
        option = dict(candidate_id=candidate['id'], line_ids=ids, first=positions[0], last=positions[-1],
                      confidence=score, manual=manual,
                      visual=annotation.get('source_kind') == 'manual_visual',
                      multi=len(ids) > 1)
        if script_relative is not None and duration:
            delta = abs(script_relative[ids[0]]-record['start']/duration)
            option.update(position_delta=delta, position_score=max(0., 1.-delta))
        output.append(option)
    output.sort(key=lambda row: (not row['manual'],
                               -(row['confidence']+.10*row.get('position_score', 0.)),
                               -row['confidence'], row['first'], row['candidate_id']))
    if any(row['manual'] for row in output):
        output = [row for row in output if row['manual']]
    unique = []
    for row in output:
        key = tuple(row['line_ids'])
        if key not in seen:
            unique.append(row)
            seen.add(key)
    return unique[:_CHOICES]


def _monotone_path(records, options):
    """Bounded Viterbi beam with skip states and explicit competing paths.

    Score is per utterance (not per second), so a long incidental ASR chunk
    cannot overwhelm a run of contextual evidence. Progress follows script
    order; repeated chunks of the current line are permitted for coalescing.
    Two paths per last-line state retain ambiguity hidden by a later common cue.
    """
    beam = [_Node(0., -1)]
    for number, record in enumerate(records):
        states = defaultdict(list)
        for previous in beam:
            states[previous.last].append(_Node(previous.score-.30, previous.last, previous, None, number))
            for choice in options[number]:
                # Do not move backwards or cross into an already covered span.
                if choice['first'] < previous.last:
                    continue
                gap = max(0, choice['first']-previous.last-1) if previous.last >= 0 else 0
                jump_penalty = min(.12, gap*.008)
                # Position is a bounded tie-break for already lexical choices.
                # It neither creates a candidate nor raises lexical confidence.
                prior = .10*choice.get('position_score', 0.)
                reward = choice['confidence']-.24 + prior + (.08 if choice['manual'] else 0.)
                node = _Node(previous.score+reward-jump_penalty, choice['last'], previous, choice, number)
                states[node.last].append(node)
        compact = []
        for rows in states.values():
            rows.sort(key=lambda node: (-node.score, node.last,
                                      node.choice['candidate_id'] if node.choice else ''))
            compact.extend(rows[:2])
        compact.sort(key=lambda node: (-node.score, node.last,
                                     node.choice['candidate_id'] if node.choice else ''))
        beam = compact[:_BEAM]
    def decode(node):
        path = [None]*len(records)
        while node.previous is not None:
            path[node.row] = node.choice
            node = node.previous
        return path
    if not records:
        return [], set()
    best = decode(beam[0])
    ambiguous = set()
    for node in beam[1:]:
        if beam[0].score-node.score > .025:
            continue
        other = decode(node)
        for number, (left, right) in enumerate(zip(best, other)):
            a = tuple(left['line_ids']) if left else ()
            b = tuple(right['line_ids']) if right else ()
            if a != b:
                ambiguous.add(number)
    return best, ambiguous


def _segment(first, last, lines=(), anchor=None, confidence=0., review=True, reason='', evidence=()):
    return dict(id='', start_frame=first, end_frame=last, line_ids=list(lines),
                anchor_frame=anchor, confidence=confidence, needs_review=bool(review),
                reason=reason, evidence_ids=list(dict.fromkeys(evidence)))


def _coalesce_unknown(segments, cut):
    """Reduce unresolved-ASR row noise without changing any mapped boundary.

    Unknown intervals remain split at PGM file boundaries and at changes in
    manual/review state. Reasons are summarized once, and evidence IDs use sets
    during accumulation, keeping a long unmatched file linear in row count.
    """
    boundaries = [row['end'] for row in cut['pgm_sources'][:-1]]
    boundary_set = set(boundaries)
    split, edge_number = [], 0
    for row in segments:
        while edge_number < len(boundaries) and boundaries[edge_number] <= row['start_frame']:
            edge_number += 1
        if row['line_ids'] or row.get('anchor_frame') is not None:
            split.append(row)
            continue
        first = row['start_frame']
        last = row['end_frame']
        while edge_number < len(boundaries) and boundaries[edge_number] < last:
            edge = boundaries[edge_number]
            split.append(dict(row, start_frame=first, end_frame=edge))
            first = edge
            edge_number += 1
        split.append(dict(row, start_frame=first))
    result, accum = [], None
    for row in split:
        unknown = not row['line_ids'] and row.get('anchor_frame') is None
        previous = result[-1] if result else None
        can_merge = bool(unknown and previous and not previous['line_ids'] and
                         previous.get('anchor_frame') is None and previous['end_frame'] == row['start_frame'] and
                         row['start_frame'] not in boundary_set and
                         previous.get('needs_review') == row.get('needs_review') and
                         previous.get('manual', False) == row.get('manual', False))
        if not can_merge:
            if accum is not None:
                previous['reason'] = '；'.join(accum[0])
                if accum[3]:
                    previous['reason'] += '；另有未定位语音证据。'
                previous['evidence_ids'] = accum[2]
            result.append(row)
            accum = ([row.get('reason', '')], {row.get('reason', '')},
                     list(dict.fromkeys(row.get('evidence_ids', []))), False, set(row.get('evidence_ids', []))) if unknown else None
            continue
        previous['end_frame'] = row['end_frame']
        previous['confidence'] = min(previous.get('confidence', 0.), row.get('confidence', 0.))
        reasons, seen, evidence, truncated, evidence_seen = accum
        reason = row.get('reason', '')
        if reason not in seen:
            seen.add(reason)
            if len(reasons) < 8:
                reasons.append(reason)
            else:
                truncated = True
        for identity in row.get('evidence_ids', []):
            if identity not in evidence_seen:
                evidence.append(identity)
                evidence_seen.add(identity)
        accum = reasons, seen, evidence, truncated, evidence_seen
    if accum is not None:
        result[-1]['reason'] = '；'.join(accum[0])
        if accum[3]:
            result[-1]['reason'] += '；另有未定位语音证据。'
        result[-1]['evidence_ids'] = accum[2]
    return result


def _assign_ids(segments):
    for number, row in enumerate(segments):
        identity = json.dumps([number, row['start_frame'], row['end_frame'], row['line_ids']],
                              ensure_ascii=False, separators=(',', ':'))
        row['id'] = 'base-map-'+hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]


def script_units(data, reference_session_id):
    """Partition active script lines at retained green alternative endpoints.

    A later shorter take contributes new boundaries, without changing existing
    baseline speech evidence or a manually saved map. Baseline track placements
    do not contribute boundaries. No cameras/proxies/media are read.
    """
    _active, index = _catalogue(data)
    boundaries = {0, len(index.script_order)}
    # Script-family boundaries must remain explicit even without alternatives.
    for number, identity in enumerate(index.script_order):
        if number and index.script_lines[identity].get('script_id', '') != index.script_lines[index.script_order[number-1]].get('script_id', ''):
            boundaries.add(number)
    have_ranges = False
    for _key, representative, _fingerprint in index._green_export_takes(reference_session_id):
        summary = index.file_summary(representative)
        ids = summary.get('covered_line_ids') or []
        if ids:
            positions = [index.script_positions[identity] for identity in ids if identity in index.script_positions]
            if positions:
                have_ranges = True
                boundaries.update((min(positions), max(positions)+1))
    if not have_ranges:
        boundaries.update(range(len(index.script_order)+1))
    edges = sorted(boundaries)
    return [dict(id='script-unit-'+str(first)+'-'+str(last),
                 line_ids=index.script_order[first:last]) for first, last in zip(edges, edges[1:]) if first < last]


def units_signature(units):
    """Separate granularity identity; green changes never invalidate ASR evidence."""
    value = [row.get('line_ids', []) for row in units]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()


def _group_units(chosen, units):
    """Coalesce recognition chunks into requested script units, not ASR units."""
    lookup = {identity: number for number, row in enumerate(units) for identity in row['line_ids']}
    output = []
    for row in chosen:
        ids = row['line_ids']
        if not ids:
            output.append(row)
            continue
        number = lookup.get(ids[0])
        if number is None or any(lookup.get(identity) != number for identity in ids):
            row['needs_review'] = True
            row['reason'] += '；一个语音区间跨越台本单元边界，需人工划分。'
            # Its outer ASR bounds cannot determine either internal unit edge.
            # Keep it explicit instead of claiming duplicate lines already
            # owned by a neighbouring expanded unit.
            row['line_ids'] = []
            row['anchor_frame'] = None
            output.append(row)
            continue
        unit = units[number]['line_ids']
        # Pending unknown chunks inside the same unit belong to its uncertain
        # coverage, but are never used to invent an alignment onset.
        previous = len(output)-1
        while previous >= 0 and not output[previous]['line_ids']:
            previous -= 1
        if previous >= 0 and output[previous].get('_unit') == number:
            target = output[previous]
            intervening = output[previous+1:]
            target['end_frame'] = max(target['end_frame'], row['end_frame'])
            target['_source_end_exact'] = max(target['_source_end_exact'], row['_source_end_exact'])
            target['_observed'].update(ids)
            target['evidence_ids'].extend(identity for identity in row['evidence_ids'] if identity not in target['evidence_ids'])
            target['confidence'] = min(target['confidence'], row['confidence'])
            target['needs_review'] |= row['needs_review'] or bool(intervening)
            if row['anchor_frame'] is not None and ids[0] == unit[0] and target['anchor_frame'] is None:
                target['anchor_frame'] = row['anchor_frame']
            if row['needs_review'] and row['reason'] not in target['reason']:
                target['reason'] += '；'+row['reason']
            if intervening:
                target['reason'] += '；单元中包含未定位语音。'
            del output[previous+1:]
        else:
            row['_unit'] = number
            row['_observed'] = set(ids)
            row['line_ids'] = list(unit)
            if ids[0] != unit[0]:
                row['anchor_frame'] = None
                row['needs_review'] = True
                row['reason'] += '；台本单元首行未找到实际语音起点。'
            output.append(row)
    for row in output:
        if '_unit' not in row:
            continue
        missing = set(row['line_ids'])-row.pop('_observed')
        if missing:
            row['needs_review'] = True
            row['reason'] += '；单元内部分台本行尚无直接语音证据。'
        row.pop('_unit')
    return output


def build_baseline_map(data, reference_session_id, cut, *, units=None):
    """Partition all baseline frames from saved, ordered dialogue evidence.

    Missing speech and ASR gaps are represented as explicit review intervals.
    Multiple script lines inside one ASR chunk remain a single interval: only
    its first line has an observed onset. Manual visual ranges are not speech
    timestamps and therefore remain marked for baseline-boundary review.
    """
    cut = _cut(cut)
    active, index = _catalogue(data)
    if reference_session_id not in index.sessions:
        raise ValueError('基准场次不存在，请重新选择基准通条。')
    records = _source_records(active, reference_session_id, cut, index)
    by_source = defaultdict(list)
    for candidate in index.candidates.values():
        by_source[candidate.get('transcript_id') or candidate.get('id')].append(candidate)
    from dialogue_engine import normalize_dialogue
    weights = {identity: max(1, len(normalize_dialogue(str(index.script_lines[identity].get('text', ''))).split()))
               for identity in index.script_order}
    total_weight = sum(weights.values()) or 1
    script_relative, cumulative = {}, 0
    for identity in index.script_order:
        script_relative[identity] = cumulative/total_weight
        cumulative += weights[identity]
    options = [_options(row, index, by_source, script_relative, cut['duration']) for row in records]
    path, ambiguous = _monotone_path(records, options)
    chosen, used = [], set()
    for number, (record, choice) in enumerate(zip(records, path)):
        if choice is None:
            reason = '台词顺序冲突或重复出现，需人工划分。' if options[number] else '该语音区间尚未匹配到台本。'
            unknown = _segment(record['start'], record['end'], reason=reason)
            unknown.update(_source_start_exact=record['exact_start'], _source_end_exact=record['exact_end'])
            chosen.append(unknown)
            continue
        ids = choice['line_ids']
        repeated = bool(used.intersection(ids))
        # The immediately previous chunk may continue one speaking block.
        continuation = bool(chosen and chosen[-1]['line_ids'] == ids)
        if repeated and not continuation:
            unknown = _segment(record['start'], record['end'], reason='同一台本位置重复出现，需人工核实。',
                               evidence=[choice['candidate_id']])
            unknown.update(_source_start_exact=record['exact_start'], _source_end_exact=record['exact_end'])
            chosen.append(unknown)
            continue
        low = choice['confidence'] <= .60 and not choice['manual']
        review = low or number in ambiguous or choice['visual']
        reasons = []
        if low:
            reasons.append('台词匹配度未超过60%')
        if number in ambiguous:
            reasons.append('存在近似同分的台本顺序路径')
        if choice['visual']:
            reasons.append('人工覆盖范围不等于逐句语音起点')
        # A visual/manual range explicitly records an anchor chosen by a human.
        # Keep the source-local position but require boundary review on baseline.
        reason = '；'.join(reasons) or '既有台词证据按台本顺序定位。'
        if 'position_delta' in choice:
            reason += f'；全剧相对位置偏差 {choice["position_delta"]:.0%}（仅辅助排序）。'
        row = _segment(record['start'], record['end'], ids, record['start'], choice['confidence'],
                       review, reason, [choice['candidate_id']])
        row.update(_source_start_exact=record['exact_start'], _source_end_exact=record['exact_end'])
        row['position_delta'] = choice.get('position_delta')
        row['position_score'] = choice.get('position_score')
        if continuation:
            previous = chosen[-1]
            gap = row['start_frame']-previous['end_frame']
            if gap > 3*cut['fps']:
                previous['needs_review'] = True
                previous['reason'] = '同一台词的语音证据间隔较长，可能为重复发言，需核实。'
            previous['end_frame'] = max(previous['end_frame'], row['end_frame'])
            previous['_source_end_exact'] = max(previous['_source_end_exact'], row['_source_end_exact'])
            previous['confidence'] = min(previous['confidence'], row['confidence'])
            previous['needs_review'] |= row['needs_review']
            previous['evidence_ids'].extend(identity for identity in row['evidence_ids'] if identity not in previous['evidence_ids'])
            if row['needs_review'] and row['reason'] not in previous['reason']:
                previous['reason'] += '；'+row['reason']
        else:
            chosen.append(row)
        used.update(ids)
    units = script_units(active, reference_session_id) if units is None else copy.deepcopy(units)
    chosen = _group_units(chosen, units)
    # Insert unassigned/silent frames explicitly. Overlapping ASR with different
    # lines cannot define a clean onset: retain exact coverage and flag review.
    segments, cursor = [], 0
    for row in chosen:
        first, last = row['start_frame'], row['end_frame']
        if last <= cursor:
            if segments:
                segments[-1]['needs_review'] = True
                segments[-1]['reason'] += '；重叠语音证据需核实。'
            continue
        if first > cursor:
            segments.append(_segment(cursor, first, reason='无精确台词时间证据（静音、动作或识别缺口），可人工调整区间。'))
        if first < cursor:
            previous = segments[-1] if segments else None
            exact_end = previous.get('_source_end_exact') if previous else None
            exact_start = row.get('_source_start_exact')
            if (cursor-first == 1 and previous is not None and previous['start_frame'] < first
                    and exact_end is not None and exact_start is not None and exact_end <= exact_start):
                # Equal fractional ASR boundaries can outward-round to a one-
                # frame overlap. The next onset owns floor(IN); this is not
                # evidence of overlapping speech and must not erase its anchor.
                previous['end_frame'] = first
                cursor = first
        if first < cursor:
            row['start_frame'] = cursor
            row['anchor_frame'] = None
            row['needs_review'] = True
            row['reason'] += '；语音区间重叠，不能推断逐句起点。'
        if row['anchor_frame'] is not None and row['anchor_frame'] >= last:
            row['anchor_frame'] = None
        segments.append(row)
        cursor = last
    if cursor < cut['duration']:
        segments.append(_segment(cursor, cut['duration'], reason='无精确台词时间证据（静音、动作或识别缺口），可人工调整区间。'))
    if not segments:
        segments = [_segment(0, cut['duration'], reason='基准通条尚无有效台词证据，请先匹配台本或人工划分。')]
    segments = _coalesce_unknown(segments, cut)
    for row in segments:
        row.pop('_source_start_exact', None)
        row.pop('_source_end_exact', None)
    _assign_ids(segments)
    mapping = dict(version=VERSION, reference_session_id=reference_session_id, fps=cut['fps'],
                   duration_frames=cut['duration'], segments=segments,
                   evidence_fingerprint=_fingerprint(active, reference_session_id, cut, index, records),
                   script_units=units, units_signature=units_signature(units))
    return validate_map(mapping, active, cut, reference_session_id)


def validate_map(mapping, data, cut, reference_session_id):
    """Validate a complete half-open partition, including human corrections.

    Empty line lists can be accepted by a human for silence/action; they never
    become alignment anchors. Several lines may share one interval, but no
    precise internal timestamp is implied. Every active line occurs at most
    once and mapped ranges follow the current script order.
    """
    cut = _cut(cut)
    if not isinstance(mapping, dict):
        raise ValueError('基准台本映射格式无效。')
    result = copy.deepcopy(mapping)
    if result.get('version') != VERSION or result.get('reference_session_id') != reference_session_id:
        raise ValueError('基准台本映射版本或场次不一致，请重新生成。')
    if (_integer(result.get('fps'), '映射帧率') != cut['fps'] or
            _integer(result.get('duration_frames'), '映射时长') != cut['duration']):
        raise ValueError('基准台本映射帧率或时长已变化，请重新生成。')
    active, index = _catalogue(data)
    if reference_session_id not in index.sessions:
        raise ValueError('基准场次不存在。')
    expected = _fingerprint(active, reference_session_id, cut, index,
                            _source_records(active, reference_session_id, cut, index))
    if result.get('evidence_fingerprint') != expected:
        raise ValueError('基准台本、语音证据或切点已变化，请重新生成并核对基准映射。')
    rows = result.get('segments')
    if not isinstance(rows, list) or not rows:
        raise ValueError('基准映射必须完整覆盖时间线。')
    cursor, used, identities, previous_line = 0, set(), set(), -1
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('基准映射区间格式无效。')
        identity = row.get('id')
        first = _integer(row.get('start_frame'), '区间入点')
        last = _integer(row.get('end_frame'), '区间出点')
        if not isinstance(identity, str) or not identity or identity in identities:
            raise ValueError('基准映射区间 ID 为空或重复。')
        identities.add(identity)
        if first != cursor or not (first < last <= cut['duration']):
            raise ValueError('基准映射存在缺口、重复覆盖或越界。')
        line_ids = row.get('line_ids')
        if (not isinstance(line_ids, list) or any(not isinstance(value, str) or value not in index.script_lines for value in line_ids)
                or len(set(line_ids)) != len(line_ids) or used.intersection(line_ids)):
            raise ValueError('台本行号无效、重复或已被其他区间使用。')
        if line_ids:
            positions = [index.script_positions[value] for value in line_ids]
            family = index.script_lines[line_ids[0]].get('script_id', '')
            if (positions != list(range(positions[0], positions[-1]+1)) or positions[0] <= previous_line or
                    any(index.script_lines[value].get('script_id', '') != family for value in line_ids)):
                raise ValueError('台本区间须连续、同属一份台本，并按台本顺序排列。')
            previous_line = positions[-1]
        anchor = row.get('anchor_frame')
        if anchor is not None:
            anchor = _integer(anchor, '台词定位点')
            if not line_ids or not first <= anchor < last:
                raise ValueError('台词定位点必须位于对应区间内；空台本区间不能有定位点。')
        confidence = row.get('confidence', 0.)
        try:
            confidence = float(confidence)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError('映射置信度无效。') from exc
        if not math.isfinite(confidence) or not 0 <= confidence <= 1 or type(row.get('needs_review')) is not bool:
            raise ValueError('映射置信度或核对状态无效。')
        evidence = row.get('evidence_ids', [])
        if not isinstance(evidence, list) or any(not isinstance(value, str) for value in evidence):
            raise ValueError('映射证据 ID 格式无效。')
        row.update(start_frame=first, end_frame=last, line_ids=list(line_ids), anchor_frame=anchor,
                   confidence=confidence, evidence_ids=list(dict.fromkeys(evidence)),
                   reason=str(row.get('reason', '')))
        used.update(line_ids)
        cursor = last
    if cursor != cut['duration']:
        raise ValueError('基准映射必须完整覆盖时间线首尾。')
    result.update(fps=cut['fps'], duration_frames=cut['duration'])
    return result


def build_line_index(mapping):
    """O(n) reusable lookup; later lines in a multi-line span have no own onset."""
    return {identity: row for row in mapping.get('segments', []) for identity in row.get('line_ids', [])}


def resolve_line(mapping, line_id, *, index=None):
    """Find a line's containing interval; only line_ids[0] owns anchor_frame."""
    return (index if index is not None else build_line_index(mapping)).get(line_id)


def refine_baseline_map(saved, data, reference_session_id, cut):
    """Refresh granularity from cached evidence without overwriting human work.

    Fresh boundaries are safe when no manually edited segment is affected. If
    a manual segment cannot be preserved exactly, return its existing complete
    partition with requested-unit metadata and an explicit refinement flag.
    The editor can then split it using actual footage; no proportional timing
    is invented and no confirmed anchor is silently replaced.
    """
    current = validate_map(saved, data, cut, reference_session_id)
    units = script_units(data, reference_session_id)
    signature = units_signature(units)
    if current.get('units_signature') == signature:
        current['units_changed'] = False
        return current
    fresh = build_baseline_map(data, reference_session_id, cut)
    manual = [row for row in current['segments'] if row.get('manual') or row.get('origin') in ('manual', 'human')]
    replacements = {}
    for row in manual:
        match = next((other for other in fresh['segments']
                      if other['start_frame'] == row['start_frame'] and other['end_frame'] == row['end_frame']
                      and other['line_ids'] == row['line_ids']), None)
        if match is None:
            current.update(units_changed=True, refinement_needed=True,
                           requested_script_units=units, requested_units_signature=signature)
            lookup = {identity: number for number, unit in enumerate(units) for identity in unit['line_ids']}
            for segment in current['segments']:
                required = {lookup.get(identity) for identity in segment['line_ids']}
                if len(required) > 1:
                    segment['refinement_needed'] = True
            return current
        replacements[match['id']] = copy.deepcopy(row)
    fresh['segments'] = [replacements.get(row['id'], row) for row in fresh['segments']]
    fresh.update(units_changed=False, refinement_needed=False)
    return validate_map(fresh, data, cut, reference_session_id)
