"""Resolve split mono recorder pairs without changing original WAV metadata."""
from collections import defaultdict
from fractions import Fraction
from hashlib import sha256
import os
from pathlib import Path
import re

from core import AnalysisError


_SUFFIXES = (
    re.compile(r'^(?P<family>.+?)[_.-](?P<role>L|R)$', re.IGNORECASE),
    re.compile(r'^(?P<family>.+?)_(?:ch|track)(?P<role>1|2)$', re.IGNORECASE),
    re.compile(r'^(?P<family>.+?)_(?P<role>1|2)$', re.IGNORECASE),
)


def sound_channel_hint(path):
    """Return a strict filename-only (role, family) hint; never read the file."""
    stem = Path(str(path)).stem
    for pattern in _SUFFIXES:
        match = pattern.fullmatch(stem)
        if match:
            role = match['role'].upper()
            return {'1': 'L', '2': 'R'}.get(role, role), match['family']
    return '', ''


def _path(value):
    return str(Path(str(value)).expanduser().resolve())


def _value(wav, field, *ixml_keys):
    value = wav.get(field)
    if not value:
        ixml = wav.get('metadata', {}).get('ixml', {})
        value = next((ixml[key] for key in ixml_keys if ixml.get(key)), '')
    if isinstance(value, (list, tuple)):
        values = {str(item).strip() for item in value if str(item).strip()}
        return next(iter(values)) if len(values) == 1 else ''
    return str(value or '').strip()


def _family_uid(wav):
    return _value(wav, 'family_uid', 'FILE_SET/FAMILY_UID', 'FAMILY_UID')


def _file_index(wav):
    value = _value(wav, 'file_set_index', 'FILE_SET/FILE_SET_INDEX', 'FILE_SET_INDEX')
    if not value.isdecimal():
        return None
    return int(value)


def _names(members):
    return '、'.join(Path(wav['path']).name for wav in members)


def _pair_id(members):
    paths = '\0'.join(sorted(_path(wav['path']) for wav in members))
    return 'split_' + sha256(paths.encode('utf-8')).hexdigest()[:20]


def _same_clock(members):
    return (len(members) == 2 and all(wav.get('channels') == 1 for wav in members) and
            all(members[0].get(key) == members[1].get(key)
                for key in ('start', 'samples', 'sample_rate', 'has_timecode')))


def _validate_pair(members, roles, manual=False):
    names = _names(members)
    if len(members) != 2 or sorted(roles) != ['L', 'R']:
        raise AnalysisError('左右声道配对不完整或有重复：' + names + '；请为每组指定一条L和一条R。')
    if any(wav.get('channels') != 1 for wav in members):
        raise AnalysisError('左右配对只支持两条独立单声道WAV：' + names + '；立体声文件请选择主声音。')
    for key, label in (('start', '源时码'), ('samples', '声音长度'),
                       ('sample_rate', '采样率'), ('has_timecode', '时码状态')):
        if members[0].get(key) != members[1].get(key):
            raise AnalysisError('左右声道的' + label + '不一致：' + names + '；请核对原始录音，不能自动配对。')
    families = [_family_uid(wav) for wav in members]
    if not manual and all(families) and families[0] != families[1]:
        raise AnalysisError('左右声道的录音系列元数据不一致：' + names + '；请核对后手动指定配对。')


def resolve_sound_pairs(rows, wavs):
    """Return deterministic main/split groups and annotate the WAV header rows.

    Only ``channel`` and ``pair_id`` are written. Original identity, timecode,
    source paths and all embedded metadata stay unchanged. Validation finishes
    before any row is mutated, so an invalid pair leaves the input intact.
    """
    by_path = {}
    for wav in wavs:
        path = _path(wav['path'])
        if path in by_path:
            raise AnalysisError('原始声音文件重复：' + Path(path).name)
        by_path[path] = wav
    entries = []
    seen = set()
    for index, row in enumerate(rows):
        path = _path(row['path'])
        if path in seen:
            raise AnalysisError('原始声音文件重复：' + Path(path).name)
        seen.add(path)
        if path not in by_path:
            raise AnalysisError('声音配置未找到对应WAV：' + Path(path).name)
        role = str(row.get('sound_role') or 'auto').strip()
        role = role.upper() if role.upper() in ('L', 'R') else role.lower()
        if role == 'auto' and row.get('channel') in ('L', 'R'):
            role = row['channel']
        if role not in ('auto', 'main', 'L', 'R'):
            raise AnalysisError('声音角色无效：' + Path(path).name + '；请选择自动、主声音、L或R。')
        pair = str(row.get('sound_pair') or '').strip()
        if pair and role not in ('L', 'R'):
            raise AnalysisError('请为手动配对的声音指定L或R：' + Path(path).name)
        entries.append(dict(index=index, wav=by_path[path], path=path, role=role, pair=pair))
    if len(seen) != len(by_path):
        raise AnalysisError('声音配置与WAV列表不一致，请重新选择原始声音。')

    groups = []
    consumed = set()

    def add_pair(pair_entries, manual=False, pair_id=''):
        roles = [entry['role'] for entry in pair_entries]
        members = [entry['wav'] for entry in pair_entries]
        _validate_pair(members, roles, manual)
        ordered = sorted(pair_entries, key=lambda entry: entry['role'])
        groups.append(dict(index=min(entry['index'] for entry in pair_entries),
                           members=[entry['wav'] for entry in ordered], pair_id=pair_id or _pair_id(members)))
        consumed.update(entry['index'] for entry in pair_entries)

    explicit = defaultdict(list)
    unassigned = []
    for entry in entries:
        if entry['role'] in ('L', 'R'):
            if entry['pair']:
                explicit[entry['pair']].append(entry)
            else:
                unassigned.append(entry)
    for pair_id, pair_entries in explicit.items():
        add_pair(pair_entries, manual=True, pair_id=pair_id)
    if len(unassigned) == 2:
        # Compatibility with the former L/R selector, even with unrelated names.
        add_pair(unassigned, manual=True)
    elif unassigned:
        families = defaultdict(list)
        for entry in unassigned:
            hint, family = sound_channel_hint(entry['path'])
            family_uid = _family_uid(entry['wav'])
            if family:
                key = ('filename', str(Path(entry['path']).parent), family.casefold())
            elif family_uid:
                key = ('ixml', family_uid)
            else:
                raise AnalysisError('未指定完整的左右声道配对：' + Path(entry['path']).name + '；请手动为每组指定一条L和一条R。')
            families[key].append(entry)
        for pair_entries in families.values():
            add_pair(pair_entries, manual=True)

    automatic = [entry for entry in entries if entry['role'] == 'auto']
    filenames = defaultdict(list)
    for entry in automatic:
        role, family = sound_channel_hint(entry['path'])
        if family:
            hinted = dict(entry, role=role)
            filenames[(str(Path(entry['path']).parent), family.casefold())].append(hinted)
    for pair_entries in filenames.values():
        if len(pair_entries) > 1:
            numeric_only = all(_SUFFIXES[-1].fullmatch(Path(entry['path']).stem) for entry in pair_entries)
            if numeric_only and len(pair_entries) == 2 and not _same_clock([entry['wav'] for entry in pair_entries]):
                # _1/_2 also names sequential recorder takes. Unlike explicit
                # L/R labels, their different clocks are not a broken pair.
                continue
            add_pair(pair_entries)

    # File-set identity can recover a split recorder pair whose filenames do
    # not carry a channel suffix. Larger polyphonic sets must not be guessed.
    families = defaultdict(list)
    for entry in automatic:
        if entry['index'] not in consumed:
            family = _family_uid(entry['wav'])
            if family:
                families[family].append(entry)
    for pair_entries in families.values():
        if len(pair_entries) != 2:
            continue
        indexes = [_file_index(entry['wav']) for entry in pair_entries]
        if indexes == [None, None] or any(index not in (1, 2) for index in indexes):
            continue
        if sorted(indexes) != [1, 2]:
            raise AnalysisError('录音系列声道编号重复，无法唯一配对：' + _names([entry['wav'] for entry in pair_entries]))
        if not _same_clock([entry['wav'] for entry in pair_entries]):
            continue
        hinted = [dict(entry, role='L' if index == 1 else 'R') for entry, index in zip(pair_entries, indexes)]
        add_pair(hinted)

    for entry in entries:
        if entry['index'] not in consumed:
            groups.append(dict(index=entry['index'], members=[entry['wav']], pair_id=''))
    groups.sort(key=lambda group: group['index'])
    for group in groups:
        group.pop('index')
        for index, wav in enumerate(group['members']):
            wav['channel'] = ('L', 'R')[index] if group['pair_id'] else 'main'
            wav['pair_id'] = group['pair_id']
    return groups


def group_sound_placements(rows):
    """Group validated L/R placements as one coverage/transcription interval.

    Rows are returned by reference without alteration. One pair can have many
    sequential placements; each identical PGM/source interval is grouped once.
    Ordinary main placements remain individual groups, preserving their order.
    """
    groups = []
    positions = {}
    fields = ('pgm_start_seconds', 'pgm_end_seconds', 'wav_start_seconds', 'wav_end_seconds')
    for row in rows:
        pair_id = str(row.get('pair_id') or '').strip()
        if not pair_id:
            groups.append([row])
            continue
        if row.get('channel') not in ('L', 'R') or not row.get('wav_path'):
            raise AnalysisError('已登记左右声道配对不完整，请重新进行声音合板。')
        try:
            times = tuple(Fraction(str(row[field])) for field in fields)
        except (KeyError, ValueError, TypeError, ZeroDivisionError, OverflowError) as exc:
            raise AnalysisError('已登记左右声道的同步区间无效，请重新进行声音合板。') from exc
        if times[0] < 0 or times[2] < 0 or times[1] <= times[0] or times[3] <= times[2]:
            raise AnalysisError('已登记左右声道的同步区间为空或倒置，请重新进行声音合板。')
        info = row.get('wav_info') or row.get('wav') or {}
        if not isinstance(info, dict):
            raise AnalysisError('已登记左右声道的WAV信息无效，请重新进行声音合板。')
        if 'channels' in info and info['channels'] != 1:
            raise AnalysisError('已登记左右声道配对不是两条单声道WAV，请重新进行声音合板。')
        key = (pair_id, row.get('pgm_path', ''), *times)
        if key not in positions:
            positions[key] = len(groups)
            groups.append([])
        groups[positions[key]].append(row)
    for group in groups:
        if not str(group[0].get('pair_id') or '').strip():
            continue
        # Registered paths were canonicalized when the actual files were read.
        # Catalogue review must not stat mounted storage again for every line.
        paths = {os.path.abspath(os.path.expanduser(str(row['wav_path']))) for row in group}
        if len(group) != 2 or {row['channel'] for row in group} != {'L', 'R'} or len(paths) != 2:
            raise AnalysisError('已登记左右声道缺失、重复或区间不一致，请重新进行声音合板。')
        group.sort(key=lambda row: row['channel'])
    return groups
