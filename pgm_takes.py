"""Whole PGM take identities; source-local speech remains the alignment evidence.

This index reads the current catalogue only. It never probes media, changes
transcripts, rebases clocks, or equates filenames from different sessions.
"""
from collections import defaultdict, OrderedDict
from fractions import Fraction
from pathlib import Path
from functools import lru_cache
import math


MATCH_THRESHOLD = .60
MANUAL_PGM_TAG = 'manual_pgm_confirmed'


@lru_cache(maxsize=20000)
def _text_weight(text):
    from dialogue_engine import normalize_dialogue
    return len(normalize_dialogue(text).split())


def _manual_choice(candidate):
    if candidate.get('status') != 'confirmed':
        return False
    tags = set(candidate.get('tags') or [])
    automatic = tags.intersection(('auto_dialogue', 'automatic_dialogue'))
    machine_tags = {'auto_dialogue', 'automatic_dialogue', 'auto_match_superseded'}
    machine_notes = {'', '台词匹配度高于60%自动通过；同场同区间机位一并归类。',
        '高台词吻合自动通过：原始声音完整覆盖、无近似剧情竞争；同场同区间机位一并归类。'}
    return (not automatic or candidate.get('note','') not in machine_notes or
            any(tag not in machine_tags and not str(tag).startswith('auto_script_span:') for tag in tags))


def _score(candidate):
    if _manual_choice(candidate):
        return 1.
    try:
        value = float(candidate.get('auto_review_score', candidate.get('score', 0)))
        return value if math.isfinite(value) and 0 <= value <= 1 else 0.
    except (TypeError, ValueError, OverflowError):
        return 0.


class PgmTakeIndex:
    fingerprint_cache_limit = 512

    def __init__(self, data, *, prepared=False):
        if not prepared:
            from library_correspondence import filter_active_transcripts
            data = filter_active_transcripts(data)
        self.sessions = {row['id']: row for row in data.get('sessions', [])}
        self.transcripts = {row['id']: row for row in data.get('transcripts', [])}
        self.script_lines = {row.get('id'): row for row in data.get('scripts', [])
                             if row.get('id') and row.get('active', row.get('script_active', True))}
        groups = {}
        for number, row in enumerate(self.script_lines.values()):
            try:
                order = float(row.get('order', row.get('line_order', number)))
                if not math.isfinite(order):
                    order = number
            except (TypeError, ValueError, OverflowError):
                order = number
            groups.setdefault(row.get('script_id', ''), []).append((order, number, row['id']))
        self.script_order = [entry[2] for rows in groups.values() for entry in sorted(rows)]
        self.script_positions = {identity: number for number, identity in enumerate(self.script_order)}
        active_lines = set(self.script_lines)
        self.candidates = {row['id']: row for row in data.get('candidates', [])
                           if row.get('active', True) and row.get('script_active', True)
                           and row.get('script_line_id') in active_lines}
        self.rows = defaultdict(list)
        self.choices = defaultdict(list)
        self._durations = {}
        self._pgm_infos = {}
        self._summaries = {}
        self._coverage_fingerprints = OrderedDict()
        self._first_choices = {}
        self._manual_anchor_conflicts = set()
        self._source_keys = {}
        self._by_transcript = defaultdict(list)
        from library_pending import _registered_pgms, is_manual_visual
        registered = {(sid, str(Path(path).expanduser())) for sid, session in self.sessions.items()
                      for path in _registered_pgms(session.get('config') or {})}
        for row in self.transcripts.values():
            try:
                first, last = Fraction(str(row['start_seconds'])), Fraction(str(row['end_seconds']))
                if first < 0 or last <= first:
                    continue
            except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
                continue
            if (row.get('active', True) and (row.get('kind', 'pgm') == 'pgm' or self.key(row) in registered) and
                    (_text_weight(str(row.get('text', ''))) or is_manual_visual(row))):
                self.rows[self.key(row)].append(row)
        for key, rows in self.rows.items():
            rows.sort(key=lambda row: (Fraction(str(row['start_seconds'])), Fraction(str(row['end_seconds'])), row['id']))
        for row in self.candidates.values():
            if row.get('status') in ('confirmed', 'candidate'):
                self.choices[self.key(row)].append(row)
                self._by_transcript[row.get('transcript_id')].append(row)

    def source(self, candidate):
        return self.transcripts.get(candidate.get('transcript_id')) or candidate

    def key(self, candidate):
        source = self.source(candidate)
        identity = source.get('id')
        if identity is not None and identity in self._source_keys:
            return self._source_keys[identity]
        path = source.get('media_path') or candidate.get('media_path') or ''
        key = (source.get('session_id') or candidate.get('session_id'),
               str(Path(path).expanduser()) if path else '')
        if identity is not None:
            self._source_keys[identity] = key
        return key

    def first_choices(self, candidate):
        key = self.key(candidate)
        if key in self._first_choices:
            return self._first_choices[key]
        rows = self.rows.get(key, [])
        if not rows:
            return []
        from library_pending import is_manual_visual
        sources = {row['id'] for row in rows}
        explicit = [row for row in self.choices.get(key, [])
                    if row.get('transcript_id') in sources and _manual_choice(row)
                    and is_manual_visual(self.source(row)) and self._manual_coverage(row, strict=True)]
        if explicit:
            anchors = {(row.get('transcript_id'), row.get('script_line_id'),
                        tuple(self._manual_coverage(row, strict=True))) for row in explicit}
            if len(anchors) > 1:
                self._manual_anchor_conflicts.add(key)
                self._first_choices[key] = []
            else:
                self._first_choices[key] = sorted(explicit, key=lambda row: row['id'])
            return self._first_choices[key]
        first = Fraction(str(rows[0]['start_seconds']))
        identities = {row['id'] for row in rows if Fraction(str(row['start_seconds'])) == first}
        result = sorted((row for row in self.choices.get(key, []) if row.get('transcript_id') in identities),
                        key=lambda row: (not _manual_choice(row), -_score(row), row['id']))
        self._first_choices[key] = result
        return result

    @staticmethod
    def _winner(choices):
        """A human decision wins; equal best script positions stay ambiguous."""
        if not choices:
            return None
        confirmed = [row for row in choices if _manual_choice(row)]
        pool = confirmed or choices
        maximum = max(_score(row) for row in pool)
        winners = [row for row in pool if abs(_score(row)-maximum) <= 1e-12]
        if len({row.get('script_line_id') for row in winners}) != 1:
            return None
        return sorted(winners, key=lambda row: row['id'])[0]

    def association_ready(self, candidate):
        """Whether this exact utterance has a unique accepted script position."""
        winner = self._winner(self._by_transcript.get(candidate.get('transcript_id'), []))
        editor = self.editor_state(candidate)
        return bool(editor['pool'] != 'removed' and winner
                    and winner['script_line_id'] == candidate.get('script_line_id')
                    and (_manual_choice(winner) or _score(winner) > MATCH_THRESHOLD or editor['reviewed']))

    def editor_state(self, candidate, summary=None):
        """Accept a current file review without inventing a first-line anchor.

        The summary remains machine/human association evidence. A separate
        fingerprinted review can accept its low score, but cannot resolve an
        ambiguous or missing first utterance, or revive a removed file.
        """
        from pgm_editor_state import coverage_fingerprint, read_editor_state
        key = self.key(candidate)
        summary = summary if summary is not None else self.file_summary(candidate)
        first = summary.get('first_candidate')
        config = self.sessions.get(key[0], {}).get('config') or {}
        source = self.source(first) if first else None
        fingerprint = None
        # File summaries and their ASR/script evidence belong to this index's
        # immutable snapshot. Repeated clicks need only read the live editor
        # settings; a fresh index recalculates changed coverage or alignment.
        if summary is self._summaries.get(key):
            if key not in self._coverage_fingerprints:
                self._coverage_fingerprints[key] = coverage_fingerprint(summary, source)
                if len(self._coverage_fingerprints) > self.fingerprint_cache_limit:
                    self._coverage_fingerprints.popitem(last=False)
            else:
                self._coverage_fingerprints.move_to_end(key)
            fingerprint = self._coverage_fingerprints[key]
        state = read_editor_state(config, key[1], summary=summary,
                                  fingerprint=fingerprint, source=source)
        if first is None or key in self._manual_anchor_conflicts:
            state['reviewed'] = False
        return state

    def _manual_coverage(self, candidate, *, strict=False):
        """Read an explicit visual range without inventing dialogue candidates.

        Persisted ranges may outlive a script revision. Invalid/deactivated IDs,
        a different script family, or non-contiguous ranges fail closed to the
        one real alignment candidate instead of extending coverage silently.
        """
        anchor = candidate.get('script_line_id')
        source = self.source(candidate)
        mappings = [source.get('data'), source.get('metadata')]
        if isinstance(source.get('data'), dict):
            mappings.append(source['data'].get('metadata'))
        value = next((row['manual_script_line_ids'] for row in mappings
                      if isinstance(row, dict) and 'manual_script_line_ids' in row), None)
        fallback = [] if strict else [anchor] if anchor in self.script_lines else []
        if not isinstance(value, (tuple, list)) or not value or any(not isinstance(identity, str) for identity in value):
            return fallback
        identities = list(dict.fromkeys(value))
        if any(identity not in self.script_lines for identity in identities):
            return fallback
        identities.sort(key=self.script_positions.__getitem__)
        family = self.script_lines[anchor].get('script_id', '')
        positions = [self.script_positions[identity] for identity in identities]
        if (identities[0] != anchor or any(self.script_lines[identity].get('script_id', '') != family for identity in identities)
                or positions != list(range(positions[0], positions[-1] + 1))):
            return fallback
        return identities

    def file_summary(self, candidate):
        """Cache whole-file classification separately from first-line alignment.

        Every current spoken token contributes to the denominator. Its unique
        best association contributes score × token count; unmatched/ambiguous
        utterances contribute zero. Thus a short excellent match cannot pass a
        mostly unrelated file. Manual per-utterance decisions score one; an
        explicit whole-PGM/manual-picture decision overrides the file score.
        None of these display scores mutate stored ASR or matching evidence.
        """
        key = self.key(candidate)
        if key in self._summaries:
            return self._summaries[key]
        from library_pending import is_manual_visual
        rows = self.rows.get(key, [])
        numerator, denominator = 0., 0
        coverage = []
        for source in rows:
            weight = _text_weight(str(source.get('text', '')))
            winner = self._winner(self._by_transcript.get(source['id'], []))
            if winner and is_manual_visual(source):
                coverage.append(winner)
                continue
            if not weight:
                continue
            denominator += weight
            if winner:
                numerator += weight * _score(winner)
                coverage.append(winner)
        first_choices = self.first_choices(candidate)
        first = self._winner(first_choices)
        alignment_ready = bool(first and (_manual_choice(first) or _score(first) > MATCH_THRESHOLD))
        manual = any(row.get('status') == 'confirmed' and (
            MANUAL_PGM_TAG in (row.get('tags') or []) or is_manual_visual(self.source(row)))
            for row in self.choices.get(key, []))
        score = numerator / denominator if denominator else 0.
        status = 'matched' if manual or score > MATCH_THRESHOLD else 'review' if self.choices.get(key) else 'unmatched'
        if key in self._manual_anchor_conflicts:
            status = 'review'
        reason = ('人工确认整条 PGM' if manual else f'整条 PGM 台词覆盖匹配度 {score:.0%}')
        if key in self._manual_anchor_conflicts:
            reason = '多个人工台本定位相互冲突，需要核实。'
        if not denominator and not manual:
            reason = '未发现可匹配台词；可人工指定台词位置'
        if not alignment_ready:
            alignment_reason = ('PGM 存在多个冲突的人工台本定位，请保留一个有效定位后再继续。'
                                if key in self._manual_anchor_conflicts else
                                'PGM 第一句台词尚未找到唯一对应位置，请人工核对。' if not first else
                                'PGM 第一句台词匹配度未超过 60%，请人工核对。')
        else:
            alignment_reason = '人工指定定位' if is_manual_visual(self.source(first)) else '首句台词已定位'
        display = first or (coverage[0] if coverage else None)
        covered_line_ids = sorted({identity for row in coverage for identity in self.coverage_ids(row)},
                                  key=self.script_positions.__getitem__)
        if first and _manual_choice(first) and is_manual_visual(self.source(first)):
            covered_line_ids = self._manual_coverage(first)
        summary = dict(match_status=status, match_score=score, match_reason=reason,
                       alignment_ready=alignment_ready, alignment_reason=alignment_reason,
                       first_candidate=first, display_candidate=display,
                       covered_line_ids=covered_line_ids,
                       coverage_candidates=coverage, manual_confirmed=manual,
                       spoken_weight=denominator)
        self._summaries[key] = summary
        return summary

    def coverage_ids(self, candidate):
        """Validated multi-line text evidence, with one real source-time anchor.

        A joined ASR utterance can cover consecutive script lines. Its original
        start remains the first line's anchor; no internal sentence timestamps
        are invented from text lengths or copied onto the later script lines.
        """
        from dialogue_spans import parse_span_tags
        anchor = candidate.get('script_line_id')
        fallback = [anchor] if anchor in self.script_lines else []
        identities = parse_span_tags(candidate)
        if (not identities or identities[0] != anchor or
                any(identity not in self.script_lines for identity in identities)):
            return fallback
        family = self.script_lines[anchor].get('script_id', '')
        scene = self.script_lines[anchor].get('scene', '')
        positions = [self.script_positions[identity] for identity in identities]
        if (any(self.script_lines[identity].get('script_id', '') != family for identity in identities)
                or any(self.script_lines[identity].get('scene', '') != scene for identity in identities)
                or positions != list(range(positions[0], positions[-1] + 1))):
            return fallback
        return identities

    def representative(self, candidate, *, confirmed=False):
        choices = self.first_choices(candidate)
        if confirmed:
            summary = self.file_summary(candidate)
            editor = self.editor_state(candidate, summary)
            if editor['pool'] == 'removed':
                raise ValueError('该 PGM 已从轨道移出，请先重新人工定位后再导出。')
            if not summary['alignment_ready'] and not editor['reviewed']:
                raise ValueError(summary['alignment_reason'] + ' ' + Path(self.key(candidate)[1]).name)
            return summary['first_candidate']
        return self._winner(choices) or (choices[0] if choices else None)

    def selection_candidate(self, candidate):
        summary = self.file_summary(candidate)
        first = self.representative(candidate, confirmed=True)
        if summary['match_status'] != 'matched' and not self.editor_state(candidate, summary)['reviewed']:
            raise ValueError('整条 PGM 台词覆盖匹配度未超过 60%，请先人工确认：' + Path(self.key(candidate)[1]).name)
        return first

    def duration(self, candidate):
        key = self.key(candidate)
        if key not in self._durations:
            from library_correspondence import PGMInfoIndex, _pgm_info
            config = self.sessions.get(key[0], {}).get('config') or {}
            if key[0] not in self._pgm_infos:
                self._pgm_infos[key[0]] = PGMInfoIndex(config)
            info, _ = _pgm_info(config, key[1], self.source(candidate), self._pgm_infos[key[0]])
            try:
                frames, rate = Fraction(str(info['frames'])), Fraction(str(info['fps']))
                value = frames/rate if frames.denominator == 1 and frames > 0 and rate > 0 else None
            except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
                value = None
            self._durations[key] = value
        return self._durations[key]

    def selected(self, candidate, pairs):
        if getattr(self, '_selected_pairs', None) is not pairs:
            self._selected_pairs = pairs
            self._selected_keys = {}
            for identity in pairs:
                self._selected_keys.setdefault(self.key(self.candidates.get(identity, {})), identity)
        return self._selected_keys.get(self.key(candidate))

    def _green_export_takes(self, reference_session_id):
        """Yield one current green track placement per non-baseline PGM.

        Inclusion follows the same fingerprinted review that paints a track
        green. Historical checkbox records, automatic confidence and pool
        visibility cannot add a blue, removed or stale placement to an export.
        File summaries are cached by this immutable catalogue index; no media
        probing or per-utterance selection scan is needed here.
        """
        for key in sorted(self.choices, key=lambda value: (str(value[0]), value[1])):
            if key[0] == reference_session_id or key[0] not in self.sessions or not key[1]:
                continue
            representative = self.choices[key][0]
            summary = self.file_summary(representative)
            editor = self.editor_state(representative, summary)
            if editor['pool'] == 'timeline' and editor['reviewed']:
                yield key, summary['first_candidate'], editor['coverage_fingerprint']

    def export_pairs(self, reference_session_id, saved_pairs=None):
        """Green take IDs with optional, still-valid baseline occurrence hints.

        Old ``selected_clip_pairs`` entries are read only as preferences for a
        repeated baseline line. They neither include nor exclude a take. A
        stale/contradictory preference is left unresolved for the export picker.
        """
        hints = defaultdict(set)
        for identity, anchor_id in (saved_pairs or {}).items():
            chosen, anchor = self.candidates.get(identity), self.candidates.get(anchor_id)
            if chosen and anchor and self.key(anchor)[0] == reference_session_id:
                hints[self.key(chosen)].add(anchor_id)
        result = {}
        for key, first, _fingerprint in self._green_export_takes(reference_session_id):
            anchors = [identity for identity in hints.get(key, ())
                       if self.candidates[identity].get('script_line_id') == first.get('script_line_id')
                       and self.association_ready(self.candidates[identity])]
            result[first['id']] = anchors[0] if len(anchors) == 1 else None
        return result

    def export_signature(self, reference_session_id):
        """Detect any eligible track addition/removal/reposition while exporting."""
        import hashlib
        import json
        rows = [[*key, first['id'], fingerprint]
                for key, first, fingerprint in self._green_export_takes(reference_session_id)]
        return hashlib.sha256(json.dumps(rows, ensure_ascii=False,
            separators=(',', ':')).encode('utf-8')).hexdigest()
