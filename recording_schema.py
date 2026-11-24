"""Human recording labels, separate from immutable session identities."""

import re

KINDS = {'continuous': '通条', 'coverage': '分镜头'}


def _label(value, title):
    if not isinstance(value, str):
        raise ValueError('请填写' + title + '。')
    text = value.strip()
    if (not text or text in ('.', '..') or len(text) > 120 or
            any(ord(char) < 32 or char in '/\\:\x00' for char in text)):
        raise ValueError(title + '不能为空，且不能包含斜杠、冒号或控制字符。')
    return text


def normalize_recording(day, kind):
    """Keep legacy numeric day IDs while accepting actual dates and custom types."""
    if isinstance(day, bool) or not isinstance(day, (str, int)):
        raise ValueError('请填写拍摄日期，例如 day01 或 2026-10-14。')
    label = _label(str(day), '拍摄日期')
    numeric = re.fullmatch(r'(?:day)?(\d+)', label, flags=re.IGNORECASE)
    if numeric:
        day = int(numeric.group(1))
        if day < 1:
            raise ValueError('拍摄日编号应从 1 开始。')
    else:
        day = label
    value = _label(kind, '拍摄类型')
    aliases = {'通条': 'continuous', '分镜头': 'coverage', '分镜': 'coverage',
               'continuous': 'continuous', 'full': 'continuous', 'coverage': 'coverage'}
    return day, aliases.get(value.casefold(), value)


def recording_name(day, kind):
    day, normalized = normalize_recording(day, kind)
    label = f'day{day:02d}' if isinstance(day, int) else day
    return label + '_' + KINDS.get(normalized, normalized)


def recording_options():
    """Common shortcuts only; these are not limits on user supplied labels."""
    return [dict(day=day, kind=kind, name=recording_name(day, kind))
            for day in range(1, 5) for kind in KINDS]
