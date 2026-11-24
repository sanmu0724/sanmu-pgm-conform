"""Persistent whole-PGM editing decisions, separate from automatic matching.

The caller supplies an existing PgmTakeIndex file summary. Reading a color or
pool does not rebuild an index, inspect media, or change source clocks.
"""
import hashlib
import json
import math


EDITOR_POLICY = 'pgm_editor_v1'


def _seconds(value):
    try:
        number = float(value)
        return round(number, 9) if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def coverage_fingerprint(summary, source=None):
    """Identify current script coverage and real source-local alignment evidence.

    Notes, confidence scores and candidate revisions are not placement changes.
    Script IDs, covered lines, utterance identities, source-local positions and
    the actual first-line anchor are. The RPC's separate revision inventories
    protect concurrent note/status/selection edits while this hash determines
    whether a previous explicit review still applies to the current placement.
    """
    summary = summary if isinstance(summary, dict) else {}
    lines = list(summary.get('covered_line_ids') or [])
    if not lines:
        return ''

    def evidence(candidate, override=None):
        if not isinstance(candidate, dict):
            return None
        actual = override if isinstance(override, dict) else candidate
        return [candidate.get('script_line_id'), candidate.get('transcript_id'),
                _seconds(actual.get('start_seconds', candidate.get('start_seconds'))),
                _seconds(actual.get('end_seconds', candidate.get('end_seconds')))]

    first = summary.get('first_candidate')
    payload = dict(lines=lines, first=evidence(first, source),
        coverage=sorted([evidence(row) for row in summary.get('coverage_candidates', [])],
                        key=lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True)))
    raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def read_editor_state(config, path, *, summary=None, fingerprint=None, source=None):
    """Return effective ``pool``/``reviewed`` for an immutable catalogue snapshot.

    ``summary`` is an already computed file_summary; optionally supply its
    first candidate's source when candidate rows omit source-local positions.
    Explicit removed state wins even if an automatic rematch finds a new line.
    Only a deliberate manual placement clears it. Missing/changed evidence
    never inherits a green review from an older correspondence or auto-confirm.
    """
    table = config.get('library_pgm_editor', {}) if isinstance(config, dict) else {}
    table = table if isinstance(table, dict) else {}
    stored = table.get(str(path), {})
    stored = stored if isinstance(stored, dict) else {}
    current = coverage_fingerprint(summary, source) if fingerprint is None else str(fingerprint or '')
    removed = stored.get('pool') == 'removed'
    return dict(pool='removed' if removed else 'timeline' if current else 'unmatched',
        hidden_in_pool=stored.get('hidden_in_pool') is True,
        reviewed=bool(not removed and current and stored.get('reviewed') is True
                      and stored.get('coverage_fingerprint') == current),
        coverage_fingerprint=current, stored_fingerprint=stored.get('coverage_fingerprint', ''),
        updated_by=stored.get('updated_by', ''), updated_at=stored.get('updated_at', ''))
