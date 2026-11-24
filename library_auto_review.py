"""Read-only automatic dialogue review, independent of ASR and media I/O.

This policy classifies existing candidates. It neither changes matcher scores
nor declares audio synchronized: registered WAV or decoded PGM audio is required.
"""
from __future__ import annotations

from collections import defaultdict
import copy
from fractions import Fraction as F
import math

from dialogue_engine import _DialogueSimilarity, normalize_dialogue
from library_correspondence import filter_active_transcripts, pgm_reference_ranges
from core import AnalysisError
from sound_pairs import group_sound_placements


_MULTI_TAKE_REASON = '同场存在相近候选，须人工确认'
_EPSILON = F(1, 10**9)  # Serialization noise only; a missing audio sample fails.


def _number(value):
    if isinstance(value, bool):
        raise ValueError('布尔值不是数值')
    result = F(str(value))
    if not math.isfinite(float(result)):
        raise ValueError('非有限数值')
    return result


def _manual_visual(row):
    annotation = row.get('data') or {}
    return (row.get('source_kind') == 'manual_visual' or
            isinstance(annotation, dict) and (annotation.get('source_kind') == 'manual_visual' or
                                              annotation.get('classification_only') is True))


def _source(candidate, transcripts):
    return transcripts.get(candidate.get('transcript_id')) or candidate


def _scope(candidate, source):
    return (source.get('session_id') or candidate.get('session_id'),
            source.get('media_path') or candidate.get('media_path'), candidate.get('transcript_id'))


def _wav_coverage(source, config, sound_index=None):
    path = source.get('media_path')
    processing = (config.get('library_processing') or {}).get(path) or {}
    reference = pgm_reference_ranges(config, path, sound_index=sound_index)
    reference_only = bool(reference) and (config.get('library_asr', {}).get(path) or {}).get('asr_audio_source') == 'pgm_reference'
    if processing.get('status') == 'failed' and processing.get('stage') == 'sync' and not reference:
        return '当前声音合板失败，不能沿用历史合板结果自动通过。'
    sync = (config.get('library_sync') or {}).get(path) or {}
    if sync.get('status') != 'matched' and not reference:
        return '尚无成功的原始WAV合板结果。'
    try:
        first, last = _number(source['start_seconds']), _number(source['end_seconds'])
        if not 0 <= first < last:
            raise ValueError()
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        return '缺少有效的PGM本地区间。'
    intervals = [(max(first, a), min(last, b)) for a,b in reference if min(last,b)>max(first,a)]
    placements = [row for row in ([] if reference_only else sync.get('placements') or [])
                  if isinstance(row, dict) and row.get('status') not in ('unmatched', 'failed', 'outside_pgm')]
    try:
        groups = group_sound_placements(placements)
    except AnalysisError:
        return '左右声道配对缺失、重复或区间不一致，需重新核对声音合板。'
    for group in groups:
        valid = True
        for placement in group:
            try:
                if not placement.get('wav_path'):
                    raise ValueError()
                begin, end = _number(placement['pgm_start_seconds']), _number(placement['pgm_end_seconds'])
                wav_begin, wav_end = _number(placement['wav_start_seconds']), _number(placement['wav_end_seconds'])
                info = placement.get('wav_info') or {}
                rate, samples = _number(info['sample_rate']), _number(info['samples'])
                if not (rate > 0 and rate.denominator == 1 and samples > 0 and samples.denominator == 1 and
                        0 <= begin < end and 0 <= wav_begin < wav_end and
                        wav_end <= samples / rate + _EPSILON and
                        abs((end - begin) - (wav_end - wav_begin)) <= _EPSILON):
                    raise ValueError()
                if 'offset_seconds' in placement and abs(begin - wav_begin - _number(placement['offset_seconds'])) > _EPSILON:
                    raise ValueError()
            except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
                valid = False
                break
        if not valid:
            continue
        a, b = max(first, begin), min(last, end)
        if b > a:
            intervals.append((a, b))
    if not intervals:
        return '没有可核对的真实WAV覆盖该PGM区间。'
    cursor = first
    for begin, end in sorted(intervals):
        if begin > cursor + _EPSILON:
            return '台词区间存在原始WAV未覆盖部分。'
        if begin < cursor - _EPSILON:
            return '台词区间存在重叠WAV，需人工核对。'
        cursor = max(cursor, end)
    if cursor < last - _EPSILON:
        return '台词区间存在原始WAV未覆盖部分。'
    for gap in ([] if reference else sync.get('uncovered') or []):
        try:
            begin, end = _number(gap['start_seconds']), _number(gap['end_seconds'])
        except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
            continue
        if min(last, end) > max(first, begin) + _EPSILON:
            return '声音报告仍标记该台词区间有未覆盖部分。'
    return ''


def classify_auto_review(data, threshold=.60, margin=0):
    """Return eligible existing candidates and non-mutating per-ID decisions.

    Scores are not probabilities. Scores strictly above the threshold pass;
    a transcript can only be assigned to its highest-scoring script position.
    Ties and existing human choices remain explicit. Real audio coverage is
    checked independently of text length. The matcher's multi-take-only .84 cap may be reviewed using its
    unchanged raw text similarity; original candidate scores remain untouched.
    Human decisions are never promoted, rejected, reset or otherwise rewritten.
    """
    from pgm_takes import _manual_choice
    threshold, margin = _number(threshold), _number(margin)
    if not 0 <= threshold <= 1 or not 0 <= margin <= 1:
        raise ValueError('自动通过阈值及竞争分差须在0到1之间。')
    current = filter_active_transcripts(data)
    active_ids = {row.get('id') for row in current.get('candidates', [])}
    transcripts = {row['id']: row for row in current.get('transcripts', []) if row.get('id')}
    sessions = {row['id']: row for row in current.get('sessions', []) if row.get('id')}
    from sound_fallback import SoundStateIndex
    sound_indexes = {identity: SoundStateIndex(row.get('config') or {}) for identity, row in sessions.items()}
    scripts = {row['id']: row for row in current.get('scripts', [])
               if row.get('id') and row.get('active', row.get('script_active', True))}
    normalized = {identity: normalize_dialogue(row.get('text', '')) for identity, row in scripts.items()}
    comparison = _DialogueSimilarity()
    effective, raw_scores, scopes = {}, {}, defaultdict(list)
    for candidate in current.get('candidates', []):
        identity = candidate.get('id')
        source = _source(candidate, transcripts)
        if (identity is None or candidate.get('status', 'candidate') == 'rejected' or
                not candidate.get('active', True) or not candidate.get('script_active', True) or
                candidate.get('script_line_id') not in scripts or _manual_visual(source)):
            continue
        try:
            score = _number(candidate.get('score'))
            if not 0 <= score <= 1:
                raise ValueError()
        except (ValueError, TypeError, ZeroDivisionError, OverflowError):
            score = None
        if _MULTI_TAKE_REASON in str(candidate.get('reason') or ''):
            spoken = normalize_dialogue(source.get('text', source.get('transcript_text', '')))
            raw = comparison.similarity(normalized[candidate['script_line_id']], spoken)
            raw_scores[identity] = raw
            if score is not None:
                score = max(score, _number(raw))
        effective[identity] = score
        scopes[_scope(candidate, source)].append(candidate)
    eligible, reasons, decisions = [], {}, {}
    for candidate in data.get('candidates', []):
        identity = candidate.get('id')
        if identity is None:
            continue
        source = _source(candidate, transcripts)
        status = candidate.get('status', 'candidate')
        visual = _manual_visual(source)
        automatic = status == 'confirmed' and not _manual_choice(candidate)
        method = 'manual_visual' if visual else 'auto_dialogue' if automatic else 'manual_dialogue' if status in ('confirmed', 'rejected') else 'unreviewed_dialogue'
        decision = dict(review_required=status not in ('confirmed', 'rejected'), review_reason='',
                        classification_method=method, auto_eligible=False)
        if identity in effective and effective[identity] is not None:
            decision['auto_review_score'] = float(effective[identity])
        if identity in raw_scores:
            decision['raw_similarity'] = raw_scores[identity]
        decisions[identity] = decision
        if status in ('confirmed', 'rejected'):
            continue
        reason = ''
        line = scripts.get(candidate.get('script_line_id'))
        if visual:
            reason = '人工画面归类不是台词识别结果，请保留人工核对。'
        elif status != 'candidate' or not candidate.get('active', True) or not source.get('active', True):
            reason = '候选或转录未启用。'
        elif not line or not candidate.get('script_active', True):
            reason = '缺少已启用的目标台本位置。'
        elif identity not in active_ids:
            reason = '不是当前有效台词识别结果。'
        elif not normalized[candidate['script_line_id']] or not normalize_dialogue(source.get('text', source.get('transcript_text', ''))):
            reason = '台词没有可辨识的归一化词元，需人工核对。'
        elif effective.get(identity) is None:
            reason = '候选分数无效。'
        elif effective[identity] <= threshold:
            reason = f'台词匹配度须高于{float(threshold):.0%}，当前未超过阈值。'
        else:
            competitors = [row for row in scopes[_scope(candidate, source)]
                           if row.get('script_line_id') != candidate.get('script_line_id')]
            if any(row.get('status') == 'confirmed' for row in competitors):
                reason = '同一画面区间已有其他台本位置的确认决定。'
            elif any(effective.get(row.get('id')) is None for row in competitors):
                reason = '同一片段的竞争候选分数无效，需人工核对。'
            elif competitors and (effective[identity] <= max(effective[row['id']] for row in competitors) or
                                  effective[identity] - max(effective[row['id']] for row in competitors) < margin):
                reason = '同一片段有其他更高或同分的台本位置；仅最高分且唯一的位置自动通过。'
            else:
                scope = _scope(candidate, source)
                session = sessions.get(scope[0])
                if not session or not scope[1] or not scope[2]:
                    reason = '缺少明确的拍摄场次或PGM来源。'
                else:
                    resolved = dict(source, media_path=scope[1])
                    reason = _wav_coverage(resolved, session.get('config') or {}, sound_indexes.get(scope[0]))
        if reason:
            decision['review_reason'] = reason
            reasons[identity] = reason
        else:
            decision.update(review_required=False, classification_method='auto_dialogue', auto_eligible=True)
            eligible.append(candidate)
    return dict(eligible=copy.deepcopy(eligible), review_reasons=reasons, decisions=decisions)


def enrich_review_state(data, threshold=.60, margin=0):
    """Return a snapshot with candidate review fields, preserving source data."""
    policy = classify_auto_review(data, threshold, margin)
    result = dict(data)
    # One memo retains shared per-session proxy/audio inventories. Copying each
    # candidate separately expands N takes × all session paths in memory.
    candidates = copy.deepcopy(data.get('candidates', []))
    result['candidates'] = [dict(row, **policy['decisions'].get(row.get('id'), {})) for row in candidates]
    return result


def select_auto_confirmations(data, threshold=.60, margin=0):
    return classify_auto_review(data, threshold, margin)['eligible']
