"""Read original WAV identity and synchronize one main sound track to the PGM."""
from fractions import Fraction as F
from functools import lru_cache
from pathlib import Path
import copy
import math
import struct
import xml.etree.ElementTree as ET

from core import AnalysisError, tc_frames

SAMPLE_RATE = 48000
MAX_METADATA_SIZE = 4 * 1024 * 1024
PCM_GUID = bytes.fromhex('0100000000001000800000aa00389b71')


def _text(data):
    """Read a fixed-width RIFF text field without inventing missing values."""
    return data.split(b'\0', 1)[0].decode('utf-8', errors='replace').strip()


def _info_fields(data, name):
    if data[:4] != b'INFO':
        return {}
    result = {}
    cursor = 4
    while cursor + 8 <= len(data):
        key = data[cursor:cursor + 4].decode('ascii', errors='replace')
        size = struct.unpack_from('<I', data, cursor + 4)[0]
        cursor += 8
        if cursor + size > len(data):
            raise AnalysisError('WAV的LIST/INFO元数据不完整：' + name)
        value = _text(data[cursor:cursor + size])
        if key in result:
            previous = result[key]
            result[key] = previous + [value] if isinstance(previous, list) else [previous, value]
        else:
            result[key] = value
        cursor += size + size % 2
    return result


def _xml_fields(raw, name):
    if not raw:
        return {}
    try:
        root = ET.fromstring(raw.rstrip(b'\0'))
    except ET.ParseError as exc:
        raise AnalysisError('原WAV的iXML损坏：' + name) from exc
    fields = {}

    def visit(node, prefix):
        tag = node.tag.rsplit('}', 1)[-1]
        key = prefix + tag
        if len(node):
            for child in node:
                visit(child, key + '/')
        else:
            value = (node.text or '').strip()
            if key in fields:
                previous = fields[key]
                fields[key] = previous + [value] if isinstance(previous, list) else [previous, value]
            else:
                fields[key] = value

    for child in root:
        visit(child, '')
    return fields


def _bext_fields(raw):
    if not raw:
        return {}
    fields = dict(description=_text(raw[:256]), originator=_text(raw[256:288]),
                  originator_reference=_text(raw[288:320]),
                  origination_date=_text(raw[320:330]), origination_time=_text(raw[330:338]))
    if len(raw) >= 346:
        fields['time_reference'] = struct.unpack_from('<Q', raw, 338)[0]
    if len(raw) >= 348:
        fields['version'] = struct.unpack_from('<H', raw, 346)[0]
    if len(raw) >= 412 and fields.get('version', 0) >= 1:
        umid = raw[348:412]
        fields['umid'] = umid.hex() if any(umid) else ''
    if len(raw) >= 422 and fields.get('version', 0) >= 2:
        for index, key in enumerate(('loudness_value', 'loudness_range', 'max_true_peak_level',
                                     'max_momentary_loudness', 'max_short_term_loudness')):
            fields[key] = struct.unpack_from('<h', raw, 412 + index * 2)[0]
    fields['coding_history'] = _text(raw[602:]) if len(raw) > 602 else ''
    return fields


def _probe_wav(path, start_tc=None, fps=25):
    """Read headers only; the WAV remains the audio and metadata source of truth.

    ``start`` and ``end`` remain sample positions for existing XML writers. A WAV
    without BWF TimeReference has a local zero origin and ``has_timecode=False``;
    the absence of source timecode must never be repaired with an inferred value.
    """
    p = Path(path).expanduser().resolve()
    chunks = {}
    info_fields = {}
    data_size = None
    try:
        with p.open('rb') as source:
            file_stat = p.stat()
            file_size = file_stat.st_size
            from stereo_merge import read_wave_layout
            for key, position, size in read_wave_layout(source, file_size, p.name):
                source.seek(position)
                if key == b'data':
                    if data_size is not None:
                        raise AnalysisError('暂不支持包含多个data块的WAV：' + p.name)
                    data_size = size
                    source.seek(size, 1)
                elif key in (b'fmt ', b'bext', b'iXML', b'LIST'):
                    if size > MAX_METADATA_SIZE:
                        raise AnalysisError('WAV元数据块异常：' + p.name)
                    value = source.read(size)
                    if key == b'LIST':
                        info_fields.update(_info_fields(value, p.name))
                    else:
                        chunks[key] = value
                else:
                    source.seek(size, 1)
    except OSError as exc:
        raise AnalysisError('无法读取原始声音：' + str(p) + '；' + str(exc)) from exc
    fmt = chunks.get(b'fmt ', b'')
    if len(fmt) < 16 or data_size is None:
        raise AnalysisError('WAV缺少格式/声音数据：' + p.name)
    encoding, channels, sample_rate, _, align, depth = struct.unpack_from('<HHIIHH', fmt)
    if encoding == 65534 and (len(fmt) < 40 or fmt[24:40] != PCM_GUID):
        raise AnalysisError('原WAV不是支持的单声道/立体声PCM：' + p.name)
    if encoding not in (1, 65534) or channels not in (1, 2) or sample_rate != SAMPLE_RATE or depth not in (16, 24):
        raise AnalysisError('请选择48kHz、16/24bit的原始单声道/立体声PCM WAV：' + p.name)
    if align != channels * (depth // 8) or data_size % align:
        raise AnalysisError('WAV声音长度异常：' + p.name)
    samples = data_size // align
    if not samples:
        raise AnalysisError('原WAV没有声音数据：' + p.name)
    bext = _bext_fields(chunks.get(b'bext', b''))
    ixml = _xml_fields(chunks.get(b'iXML', b''), p.name)
    has_timecode = 'time_reference' in bext
    reference = bext.get('time_reference', 0)
    if start_tc:
        entered = tc_frames(start_tc, fps) * SAMPLE_RATE // fps
        if not has_timecode:
            raise AnalysisError('原WAV没有BWF源时码，不能用填写值替代原始元数据：' + p.name)
        if entered != reference:
            raise AnalysisError('填写的时码与原WAV内嵌时码不同：' + p.name)

    def xml_value(*keys):
        for key in keys:
            value = ixml.get(key, '')
            if isinstance(value, list):
                value = next((entry for entry in value if entry), '')
            if value:
                return value
        return ''

    file_uid = xml_value('FILE_UID')
    original_name = xml_value('ORIGINAL_FILENAME', 'FILE_SET/ORIGINAL_FILENAME')
    date = bext.get('origination_date', '') or info_fields.get('ICRD', '')
    if isinstance(date, list):
        date = date[0] if date else ''
    source_stat = dict(size_bytes=file_stat.st_size, modified_time_ns=file_stat.st_mtime_ns)
    return dict(path=str(p), filename=p.name, original_audio_filename=original_name or p.name,
                sample_rate=SAMPLE_RATE, channels=channels, depth=depth, samples=samples,
                start=reference, end=reference + samples, has_timecode=has_timecode,
                timecode_source='bext/TimeReference' if has_timecode else '',
                sound_roll=xml_value('TAPE'), sound_roll_source='iXML/TAPE' if xml_value('TAPE') else '',
                scene=xml_value('SCENE'), take=xml_value('TAKE'), original_uid=file_uid,
                file_uid=file_uid, family_uid=xml_value('FILE_SET/FAMILY_UID', 'FAMILY_UID'),
                file_set_index=xml_value('FILE_SET/FILE_SET_INDEX'),
                parent_filename=xml_value('PARENT_FILENAME'), parent_uid=xml_value('PARENT_UID'),
                recording_date=date, recording_time=bext.get('origination_time', ''),
                originator=bext.get('originator', ''), originator_reference=bext.get('originator_reference', ''),
                description=bext.get('description', ''), umid=bext.get('umid', ''), source_stat=source_stat,
                metadata=dict(bext=bext, ixml=ixml, list_info=info_fields,
                              source_filename=p.name, source_path=str(p), filesystem_identity=source_stat), fmt=fmt)


@lru_cache(maxsize=512)
def _cached_wav_header(path, signature, start_tc, fps):
    return _probe_wav(path, start_tc, fps)


def probe_wav(path, start_tc=None, fps=25):
    """Reuse unchanged headers across PGM clips; return independent metadata."""
    p = Path(path).expanduser().resolve()
    try:
        stat = p.stat()
    except OSError as exc:
        raise AnalysisError('无法读取原始声音：' + str(p) + '；' + str(exc)) from exc
    signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    return copy.deepcopy(_cached_wav_header(str(p), signature, start_tc, fps))


def _seconds(value, name):
    """Quantize alignment results to the original 48kHz sample clock."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AnalysisError(name + '无效。') from exc
    if not math.isfinite(number):
        raise AnalysisError(name + '无效。')
    return F(round(number * SAMPLE_RATE), SAMPLE_RATE)


def _cancelled(cancelled):
    if cancelled():
        raise AnalysisError('已取消声音合板，未导出时间线。')


def _fully_covered(placements, duration):
    """Only verified waveform coverage can stop the wider candidate search."""
    cursor = F(0)
    for placement in sorted(placements, key=lambda row: row['first']):
        if placement['first'] > cursor:
            return False
        cursor = max(cursor, placement['end'])
        if cursor >= duration:
            return True
    return False


def _short_clock_verified_edges(placements, duration, details, allow_gaps):
    """Accept only small expected start/stop gaps, never an internal sound hole.

    Operators commonly start the recorder a second after the PGM. Searching
    unrelated takes cannot improve such a clock-corroborated edge gap. Keep the
    gap in the output; this optimization never extends or invents source audio.
    """
    if not allow_gaps or not placements:
        return False
    expected = {row['path']: row.get('expected_offset_seconds') for row in details}
    for row in placements:
        value = expected.get(row['wav']['path'])
        if value is None or abs(float(row['offset']) - value) > .25:
            return False
    ordered = sorted(placements, key=lambda row: row['first'])
    limit = min(F(5), duration / 20)
    if ordered[0]['first'] > limit:
        return False
    cursor = ordered[0]['end']
    for row in ordered[1:]:
        if row['first'] > cursor:
            return False
        cursor = max(cursor, row['end'])
    return duration - cursor <= limit


def _main_events(block, placements, fps, allow_gaps=False, uncovered=None):
    """Require unique source coverage and report explicitly permitted gaps."""
    duration = F(block['frames'], fps)
    cursor = F(0)
    events = []
    if not placements:
        raise AnalysisError('缺少主声音覆盖，本段PGM没有任何匹配的主声音：' + Path(block['path']).name)
    while cursor < duration:
        matches = [placement for placement in placements if placement['first'] <= cursor < placement['end']]
        if not matches and allow_gaps:
            stop = min([duration] + [placement['first'] for placement in placements if placement['first'] > cursor])
            if uncovered is not None:
                uncovered.append(dict(status='uncovered', pgm_path=block['path'], sound_path='',
                                      pgm_start_seconds=float(cursor), pgm_end_seconds=float(stop),
                                      record_start_seconds=float(F(block['start'], fps) + cursor),
                                      duration_seconds=float(stop - cursor), reason='no_main_audio'))
            cursor = stop
            continue
        if len(matches) != 1:
            why = '缺少主声音覆盖' if not matches else '主声音范围重叠，请仅添加本场同一主声道的顺序文件'
            raise AnalysisError(f'{why}；PGM {Path(block["path"]).name}，文件内 {float(cursor):.3f} 秒。')
        placement = matches[0]
        stop = min([duration, placement['end']] + [other['first'] for other in placements
                   if cursor < other['first'] < min(duration, placement['end'])])
        local = cursor - placement['offset']
        for w in placement.get('members', [placement['wav']]):
            event = dict(path=w['path'], channel=w.get('channel', 'main'),
                         src_channel=1 if w['channels'] == 1 else '1,2',
                         record_start=F(block['start'], fps) + cursor, duration=stop - cursor,
                         source_start=F(w['start'], SAMPLE_RATE) + local, pgm_path=block['path'],
                         offset_seconds=float(placement['offset']), score=placement['report'].get('score'),
                         method=placement['report']['method'])
            if w.get('pair_id'):
                event.update(pair_id=w['pair_id'], out_channel=w['channel'],
                             alignment_source_path=placement['report'].get('alignment_source_path', ''))
            events.append(event)
        cursor = stop
    return events


def sound_plan(config, cut, progress=lambda n, message: None, cancelled=lambda: False):
    """Synchronize selected mono/stereo main WAV files without altering identity.

    Waveform alignment is the default. Source clocks first narrow its candidate
    WAVs; unverified/incomplete coverage falls back to the remaining recordings.
    Timecode-only synchronization is a separate explicit alternative. Camera
    video offsets never affect either sound synchronization method.
    """
    fps = int(config['fps'])
    rows = config.get('sound_sources', [])
    if not rows:
        from sound_fallback import wav_only_sound_plan
        return wav_only_sound_plan(None,cut,fps)
    mode = config.get('sound_sync_mode', 'waveform')
    if mode not in ('waveform', 'timecode'):
        raise AnalysisError('声音同步方式必须为波形或时码。')
    wavs = []
    seen = set()
    for row in rows:
        _cancelled(cancelled)
        w = probe_wav(row['path'], fps=fps)
        if w['path'] in seen:
            raise AnalysisError('原始主声音文件重复：' + Path(w['path']).name)
        seen.add(w['path'])
        w.update(channel='main', sync_report=[])
        if mode == 'timecode' and not w['has_timecode']:
            raise AnalysisError('原WAV没有BWF源时码，无法按时码同步；请选择波形同步：' + Path(w['path']).name)
        wavs.append(w)
    from sound_pairs import resolve_sound_pairs
    recordings = resolve_sound_pairs(rows, wavs)
    members_by_path = {recording['members'][0]['path']: recording['members'] for recording in recordings}
    anchors = [recording['members'][0] for recording in recordings]
    def save_report(anchor, report):
        for member in members_by_path[anchor['path']]:
            value = copy.deepcopy(report)
            value.update(sound_path=member['path'], sound_stat=member['source_stat'])
            if member.get('pair_id'):
                value.update(pair_id=member['pair_id'], channel=member['channel'])
            member['sync_report'].append(value)
    blocks = cut['pgm_sources']
    if not blocks:
        raise AnalysisError('没有可用于声音同步的PGM素材。')
    events = []
    # Missing original recordings are deliberate output silence. Reference PGM
    # sound is still used for correlation above, never as an exported source.
    allow_gaps = True
    failures = []
    total = len(blocks) * len(anchors)
    done = 0
    for block in blocks:
        _cancelled(cancelled)
        duration = F(block['frames'], fps)
        placements = []
        if mode == 'timecode':
            if not block.get('start_tc'):
                raise AnalysisError('PGM缺少源时码，无法按时码同步：' + Path(block['path']).name)
            block_clock = F(tc_frames(block['start_tc'], fps), fps)
        else:
            from waveform_sync import align_audio
            reference = str(Path(block['path']).expanduser().resolve())
            reference_start = F(0)
        if mode == 'waveform':
            from sync_candidates import plan_waveform_candidates
            search = plan_waveform_candidates(block, anchors, fps)
            groups = [(search['primary'], 'timecode_candidate' if search['used_timecode'] else 'full_search'),
                      (search['deferred'], 'fallback')]
            if search['used_timecode']:
                progress(round(done * 100 / total),
                         f'时码预筛选：{Path(block["path"]).name}，优先核对{len(search["primary"])}条原始录音')
        else:
            groups = [(anchors, 'timecode')]
        for group, stage in groups:
            if not group:
                continue
            if stage == 'fallback':
                full = _fully_covered(placements, duration)
                edges = _short_clock_verified_edges(placements, duration, search['details'], allow_gaps)
                if full or edges:
                    for w in group:
                        _cancelled(cancelled)
                        save_report(w, dict(method=mode, status='not_searched',
                            pgm_path=block['path'], sound_path=w['path'], sound_stat=w['source_stat'],
                            search_stage='timecode_filtered', reason='verified_candidates_cover_pgm'
                            if full else 'verified_clock_and_short_edge_gaps'))
                    done += len(group)
                    progress(round(done * 100 / total), '对应录音已通过波形核查，跳过时码范围外录音。'
                             if full else '开停机短缺口与源时码一致，保留缺口并跳过无关录音。')
                    continue
                progress(round(done * 100 / total), '时码候选未完整覆盖PGM，扩大波形搜索范围。')
            for w in group:
                _cancelled(cancelled)
                label = f'声音合板 {done + 1}/{total}：{Path(w["path"]).name}'
                report = dict(method=mode, pgm_path=block['path'], sound_path=w['path'],
                              sound_stat=w['source_stat'], search_stage=stage)
                if mode == 'timecode':
                    offset = F(w['start'], SAMPLE_RATE) - block_clock
                    report.update(status='matched', offset_seconds=float(offset), has_timecode=True)
                else:
                    def match_progress(number, message):
                        progress(min(99, round((done + max(0, min(100, number)) / 100) * 100 / total)), label + '；' + message)
                    try:
                        attempts = []
                        result = None
                        for member in members_by_path[w['path']]:
                            try:
                                result = align_audio(reference, member['path'], reference_start=float(reference_start),
                                                     reference_duration=float(duration), progress=match_progress, cancelled=cancelled)
                                break
                            except AnalysisError as error:
                                _cancelled(cancelled)
                                attempts.append(Path(member['path']).name + '：' + str(error))
                        if result is None:
                            raise AnalysisError('；'.join(attempts))
                        _cancelled(cancelled)
                        offset = _seconds(result['offset_seconds'], '波形同步偏移') - reference_start
                        report.update(result)
                        report.update(status='matched', reference_path=reference, reference_start_seconds=float(reference_start),
                                      pgm_offset_seconds=float(offset), alignment_source_path=member['path'])
                        if attempts:
                            report['channel_attempts'] = attempts
                    except AnalysisError as exc:
                        _cancelled(cancelled)
                        report.update(status='unmatched', reference_path=reference, reference_start_seconds=float(reference_start), reason=str(exc))
                        save_report(w, report)
                        failures.append(f'{Path(w["path"]).name}：{exc}')
                        done += 1
                        progress(round(done * 100 / total), label + '；未匹配')
                        continue
                first = max(F(0), offset)
                end = min(duration, offset + F(w['samples'], SAMPLE_RATE))
                if end > first:
                    placements.append(dict(wav=w, members=members_by_path[w['path']], offset=offset, first=first, end=end, report=report))
                    report.update(pgm_start_seconds=float(first), pgm_end_seconds=float(end))
                else:
                    report.update(status='outside_pgm')
                save_report(w, report)
                done += 1
                progress(round(done * 100 / total), label)
        try:
            uncovered = []
            events.extend(_main_events(block, placements, fps, allow_gaps=allow_gaps, uncovered=uncovered))
            for report in uncovered:
                report['method'] = mode
            wavs[0]['sync_report'].extend(uncovered)
        except AnalysisError as exc:
            detail = '；波形未匹配：' + '；'.join(failures[-3:]) if mode == 'waveform' and failures else ''
            raise AnalysisError(str(exc) + detail) from exc
    _cancelled(cancelled)
    progress(100, '主声音合板完成。')
    # Keep unused WAV attempt reports for synchronization provenance. Export
    # later removes unused definitions; analysis must retain candidate identity.
    return wavs,events
