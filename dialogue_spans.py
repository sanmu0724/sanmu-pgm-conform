"""Schema-free machine annotations for one ASR segment spanning script lines.

These tags describe script coverage only. They never describe internal source
times, which cannot be recovered from an ASR segment's outer IN/OUT points.
"""
import json


SPAN_TAG_PREFIX = 'auto_script_span:'
MAX_SPAN_LINES = 6


def _valid_ids(value):
    return (isinstance(value, list) and 2 <= len(value) <= MAX_SPAN_LINES and
            all(isinstance(identity, str) and identity.strip() for identity in value) and
            len(set(value)) == len(value))


def encode_span_tags(ids):
    """Return one machine tag for 2–6 distinct string IDs, or an empty list."""
    values = list(ids) if isinstance(ids, (list, tuple)) else ids
    if not _valid_ids(values):
        return []
    return [SPAN_TAG_PREFIX + json.dumps(values, ensure_ascii=False, separators=(',', ':'))]


def parse_span_tags(candidate):
    """Read a structurally valid span; absent/malformed/conflicting tags fail closed.

    Script activity, family, ordering and the first-line anchor must be checked
    against the current catalogue by the caller. Only string-list tags are read.
    """
    tags = candidate.get('tags') if isinstance(candidate, dict) else None
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        return []
    result = None
    for tag in tags:
        if not tag.startswith(SPAN_TAG_PREFIX):
            continue
        try:
            value = json.loads(tag[len(SPAN_TAG_PREFIX):])
        except (ValueError, TypeError):
            return []
        if not _valid_ids(value) or result is not None and result != value:
            return []
        result = value
    return list(result) if result is not None else []
