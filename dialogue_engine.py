"""Script, transcript and dialogue candidates, independent of camera/timecode sync.

Matches are suggestions for human classification, never synchronization offsets.
ASR is optional: importing this module does not load or download a speech model.
"""
from collections import Counter, defaultdict, OrderedDict
import csv
from decimal import Decimal, ROUND_HALF_UP
from difflib import SequenceMatcher
from functools import lru_cache
from html import unescape
from pathlib import Path
import io
import json
import math
import os
import re
import tempfile
import unicodedata
import wave
import xml.etree.ElementTree as ET
import zipfile


class DialogueError(ValueError):
    pass


def _check(cancelled):
    if cancelled():
        raise DialogueError('已取消台词处理。')


def _read_text(path):
    data = Path(path).read_bytes()
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        encodings = ('utf-16',)
    elif b'\0' in data[:1000]:
        encodings = ('utf-16-le', 'utf-16-be')
    else:
        encodings = ('utf-8-sig', 'gb18030')
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except UnicodeError:
            pass
    raise DialogueError('无法读取文字编码，请另存为 UTF-8：' + Path(path).name)


_SCENE = re.compile(
    r'^\s*(?:《[^》\n]+》\s*)?(?:第\s*[零〇一二两三四五六七八九十百千\d]+\s*[幕场章节]|'
    r'[幕场]\s*[零〇一二两三四五六七八九十百千\d]+(?!\s*号)(?:\s|[：:、.．]|$)|'
    r'场景\s*(?:[：:]|[零〇一二两三四五六七八九十百千\d])|'
    r'(?:act|scene)\s+[\divxlc]+(?:\b|[：:]))', re.IGNORECASE)
_STAGE_LABEL = re.compile(r'^(?:舞台提示|舞台说明|场景说明|动作|音效|音乐|灯光|布景|旁注|场记|备注)\s*[：:]')
_STAGE_ONLY = re.compile(r'^(?:[（(][^）)]*[）)]|[【\[〔][^】\]〕]*[】\]〕])\s*[。.!！]?$', re.S)
_STAGE_NARRATION = re.compile(r'^(?:灯光|灯|音乐|音效|幕布|舞台|布景).*(?:渐暗|亮起|熄灭|响起|暗下|拉开|落下|切换|转暗|渐亮|淡入|淡出)[。.!！]?$')
_SPEAKER = re.compile(r'^([^：:\n]{1,30})\s*[：:]\s*(.*)$', re.S)
_SYMBOL_CUE = re.compile(r'^[◆●■▲★◇○□△]\s*(\S[^：:\n]{0,59})[：:]?\s*$')
_PAGE_NUMBER = re.compile(r'^(?:[-–—]\s*\d+\s*[-–—]|第\s*\d+\s*页(?:\s*/\s*共\s*\d+\s*页)?)$')
_STAGE_PREFIX = re.compile(r'^(?:—{2,}|--+|▪)\s*')


def _character_cue(line):
    marked = _SYMBOL_CUE.fullmatch(line)
    if marked:
        return marked.group(1).strip(), ''
    match = _SPEAKER.match(line)
    return tuple(part.strip() for part in match.groups()) if match else None


def _spoken_text(value):
    """Remove parenthetical acting instructions for scoring, retain original elsewhere."""
    lines, stage = [], False
    for line in str(value).splitlines():
        stripped = line.strip()
        stage = bool(_STAGE_PREFIX.match(stripped)) or (stage and bool(stripped))
        if not stage and not _STAGE_LABEL.match(stripped) and not _STAGE_NARRATION.match(stripped):
            lines.append(line)
    return re.sub(r'[（(][^）)]*[）)]|[【\[〔][^】\]〕]*[】\]〕]', '', '\n'.join(lines)).strip()


def parse_script(text):
    """Return speaking blocks, preserving line wraps and their acting directions.

    A named character keeps the floor through document paragraph/page wrapping,
    until the next character cue or scene heading. Unlabelled text instead uses
    blank lines as natural paragraph boundaries, so prose is not one giant cue.
    This only parses newly imported text; it never rewrites stored script IDs.
    """
    raw_lines = str(text).replace('\r\n', '\n').replace('\r', '\n').split('\n')
    # A marked-character edition may begin with a cover/colour legend. Only
    # exclude this explicit legend when an actual act heading follows it.
    first_scene = next((i for i, line in enumerate(raw_lines) if _SCENE.match(line.strip())), None)
    marked_edition = any('人物记号对照表' in line for line in raw_lines[:first_scene or 0])
    if marked_edition and first_scene is not None:
        raw_lines = raw_lines[first_scene:]
    marked_speakers = {match.group(1).strip() for line in raw_lines
                       if (match := _SYMBOL_CUE.fullmatch(line.strip()))}

    def cue_for(line):
        cue = _character_cue(line)
        # In a symbol-labelled edition, punctuation inside a speech (e.g.
        # "原文：...") is not a character change. Explicit known-name colon
        # labels are still accepted if the document occasionally mixes styles.
        if marked_edition and cue and not _SYMBOL_CUE.fullmatch(line) and cue[0] not in marked_speakers:
            return None
        return cue

    speakers = set()
    for raw in raw_lines:
        line = raw.strip()
        cue = cue_for(line)
        if cue and not _SCENE.match(line) and not _STAGE_LABEL.match(line):
            speakers.add(cue[0])
    result, scene, speaker, parts = [], '', '', []

    def flush():
        nonlocal parts
        words = '\n'.join(parts).strip()
        if words:
            result.append(dict(order=len(result), scene=scene, speaker=speaker, text=words))
        parts = []

    for raw in raw_lines:
        line = raw.strip()
        if _PAGE_NUMBER.fullmatch(line):
            continue
        if marked_edition and line == '排演备注' and any('全剧终' in part for part in parts):
            break
        if not line:
            if speaker:
                if parts and parts[-1]:
                    parts.append('')
            else:
                flush()
            continue
        if _SCENE.match(line):
            flush()
            scene, speaker = line, ''
            continue
        if _STAGE_LABEL.match(line) or _STAGE_NARRATION.match(line) or _STAGE_PREFIX.match(line):
            # Global lighting/music/stage instructions do not identify their
            # character. Keep them as unlabelled context rather than assigning
            # them to whoever happened to speak immediately before them.
            direction = _STAGE_PREFIX.sub('', re.split('[：:]', line, maxsplit=1)[-1]).strip()
            if speaker and (_STAGE_LABEL.match(line) or _STAGE_PREFIX.match(line)) and direction.startswith(speaker):
                parts.append(line)
                continue
            if speaker:
                flush()
                speaker = ''
            parts.append(line)
            continue
        if _STAGE_ONLY.match(line):
            direction = line.lstrip('（([【').strip()
            mentioned = {name for name in speakers if direction.startswith(name)
                         or re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', direction)}
            if speaker and mentioned and speaker not in mentioned:
                flush()
                speaker = ''
            # Parenthetical acting instructions inside a named turn retain the
            # turn's context. A direction before any turn stays unlabelled.
            parts.append(line)
            if not speaker and mentioned:
                flush()
            continue
        cue = cue_for(line)
        if cue:
            flush()
            name, words = cue
            speaker = name
            if words:
                parts.append(words)
        else:
            # Unlabelled dialogue remains searchable; no invented character attribution.
            parts.append(line)
    flush()
    if not result:
        raise DialogueError('台本没有可读取的对白。请提供文字台本，扫描件需先转换为文字。')
    return result


def _document_text(path, purpose='台本'):
    suffix = path.suffix.lower()
    if suffix in ('.txt', '.md', '.text'):
        return _read_text(path)
    if suffix == '.docx':
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo('word/document.xml')
            if info.file_size > 50_000_000:
                raise DialogueError(purpose + '文档过大，请拆分后导入。')
            root = ET.fromstring(archive.read(info))
        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        return '\n'.join(''.join((node.text or '') if node.tag.endswith('}t') else
                                 '\t' if node.tag.endswith('}tab') else
                                 '\n' if node.tag.endswith(('}br', '}cr')) else ''
                                 for node in p.iter())
                         for p in root.findall('.//w:p', ns))
    if suffix == '.pdf':
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise DialogueError('读取文字版 PDF 需要安装 pypdf；也可先另存为 TXT 或 DOCX。扫描件不支持直接导入。') from exc
        reader = PdfReader(str(path))
        if reader.is_encrypted and not reader.decrypt(''):
            raise DialogueError('PDF 已加密，请先解锁后导入。')
        text = '\n'.join(page.extract_text() or '' for page in reader.pages)
        if not text.strip():
            raise DialogueError('PDF 未读取到文字，可能是扫描件；请先转换为文字' + purpose + '。')
        return text
    raise DialogueError(purpose + '支持 TXT、DOCX 和文字版 PDF。')


def import_script(path):
    path = Path(path).expanduser()
    try:
        return parse_script(_document_text(path))
    except DialogueError:
        raise
    except Exception as exc:
        raise DialogueError('台本导入失败：' + str(exc)) from exc


_CUE_TIME = re.compile(r'(?:(\d+):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,9}))?')


def _cue_seconds(value):
    match = _CUE_TIME.fullmatch(value.strip())
    if not match:
        raise DialogueError('字幕时间格式错误：' + value)
    hours, minutes, seconds, fraction = match.groups()
    if int(minutes) >= 60 or int(seconds) >= 60:
        raise DialogueError('字幕时间超出范围：' + value)
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + float('0.' + (fraction or '0'))


def validate_transcript(rows):
    """Normalize common JSON fields, preserving exact decimal seconds and source IDs."""
    if not isinstance(rows, list):
        raise DialogueError('转录 JSON 必须包含片段数组。')
    result, previous = [], -1.
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise DialogueError(f'第 {index + 1} 个转录片段不是对象。')
        try:
            start = float(row.get('start_seconds', row.get('start')))
            end = float(row.get('end_seconds', row.get('end')))
        except (ValueError, TypeError) as exc:
            raise DialogueError(f'第 {index + 1} 个转录片段缺少有效起止秒数。') from exc
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise DialogueError(f'第 {index + 1} 个转录片段起止时间无效。')
        if start < previous:
            raise DialogueError('转录片段须按起始时间顺序排列，未自动排序以免掩盖错误。')
        previous = start
        text = str(row.get('text') or '').strip()
        if not text:
            continue
        value = dict(row, start_seconds=start, end_seconds=end, text=text)
        result.append(value)
    return result


def import_transcript(path):
    path = Path(path).expanduser()
    try:
        text = _read_text(path)
        if path.suffix.lower() == '.json':
            rows = json.loads(text)
            if isinstance(rows, dict):
                rows = rows.get('segments', rows.get('transcripts', rows.get('transcript')))
            return validate_transcript(rows)
        if path.suffix.lower() not in ('.srt', '.vtt'):
            raise DialogueError('台词转录支持 SRT、VTT 和 JSON。')
        rows = []
        for block in re.split(r'\n\s*\n', text.replace('\r\n', '\n').replace('\r', '\n').strip()):
            lines = block.splitlines()
            if not lines or lines[0].lstrip().startswith(('NOTE', 'STYLE', 'REGION')):
                continue
            time_index = next((i for i, line in enumerate(lines) if '-->' in line), None)
            if time_index is None:
                continue
            begin, finish = lines[time_index].split('-->', 1)
            begin, finish = begin.strip(), finish.strip().split()[0]
            words = '\n'.join(lines[time_index + 1:]).strip()
            voice = re.search(r'<v(?:\.[^ >]+)?\s+([^>]+)>', words)
            words = unescape(re.sub(r'<[^>]*>', '', words)).strip()
            row = dict(start_seconds=_cue_seconds(begin), end_seconds=_cue_seconds(finish), text=words)
            if voice:
                row['speaker'] = unescape(voice.group(1)).strip()
            rows.append(row)
        if not rows:
            raise DialogueError('字幕中没有找到带有效起止时间的台词片段。')
        return validate_transcript(rows)
    except DialogueError:
        raise
    except (OSError, ValueError, UnicodeError) as exc:
        raise DialogueError('转录导入失败：' + str(exc)) from exc


def _srt_stamp(seconds):
    milliseconds = int((Decimal(str(seconds)) * 1000).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    hours, rest = divmod(milliseconds, 3600000)
    minutes, rest = divmod(rest, 60000)
    seconds, millis = divmod(rest, 1000)
    return f'{hours:02}:{minutes:02}:{seconds:02},{millis:03}', milliseconds


def export_subtitles(segments, path):
    """Write UTF-8 SRT in source-local seconds, refusing to overwrite any file.

    No frame rounding or timeline/timecode offset is added. Existing media and
    subtitle files are preserved; callers should choose a new output filename.
    """
    target = Path(path).expanduser().resolve()
    if target.suffix.lower() != '.srt':
        raise DialogueError('字幕输出文件必须使用 .srt 扩展名，不能覆盖媒体文件。')
    clean = validate_transcript(list(segments))
    if not clean:
        raise DialogueError('没有可导出的台词片段。')
    cues = []
    for index, row in enumerate(clean, 1):
        for name in ('media_path', 'source_path', 'path'):
            if row.get(name) and Path(str(row[name])).expanduser().resolve() == target:
                raise DialogueError('字幕输出位置不能与输入素材相同。')
        begin, a = _srt_stamp(row['start_seconds'])
        finish, b = _srt_stamp(row['end_seconds'])
        if b <= a:
            raise DialogueError(f'第 {index} 段在毫秒四舍五入后没有有效时长，不能输出 SRT。')
        words = '\n'.join(line for line in row['text'].replace('\r\n', '\n').replace('\r', '\n').split('\n') if line.strip())
        cues.append(f'{index}\n{begin} --> {finish}\n{words}\n')
    content = '\n'.join(cues)
    created = False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('x', encoding='utf-8', newline='\n') as handle:
            created = True
            handle.write(content)
    except FileExistsError as exc:
        raise DialogueError('字幕文件已存在，请选择新的输出位置：' + str(target)) from exc
    except OSError as exc:
        if created:
            target.unlink(missing_ok=True)
        raise DialogueError('字幕导出失败：' + str(exc)) from exc
    return str(target)


def _header_key(value):
    return ''.join(char for char in unicodedata.normalize('NFKC', str(value)).casefold() if char.isalnum())


_NOTE_ALIASES = {
    'session_name': ('拍摄场次', '拍摄批次', '拍摄场', '场次', '录制场次', 'session', 'session_name', 'shoot', 'shoot_name', 'performance'),
    'session_id': ('session_id', '拍摄场次id'),
    'scene': ('场号', '场景', '幕场', '剧本场号', '幕场号', 'scene', 'scene_number', 'scene_no', 'slate'),
    'dialogue': ('台词', '对白', '台词内容', '现场台词', 'dialogue', 'line', 'script_line', 'spoken_text'),
    'notes': ('备注', '场记', '说明', '评语', '表演备注', 'notes', 'note', 'remarks', 'comment', 'comments'),
    'media_path': ('文件名', '媒体文件', '视频文件', 'PGM文件', '文件路径', '素材路径', 'media_path', 'file', 'filename', 'file_name', 'media', 'clip', 'clip_name'),
    'speaker': ('人物', '角色', '演员', 'speaker', 'character', 'actor'),
    'take': ('条号', '条次', 'take', 'take_number', 'take_no'),
    'date': ('日期', '拍摄日期', '录制日期', 'date', 'shoot_date', 'shooting_date'),
    'camera': ('机位', '摄影机', 'camera', 'camera_id', 'angle'),
    'start_raw': ('IN', '入点', '开始', '开始时间', '起始时间', '开始时码', '入点时码', 'start', 'start_seconds', '开始秒', '起始秒', '素材内入点', '相对开始'),
    'end_raw': ('OUT', '出点', '结束', '结束时间', '结束时码', '出点时码', 'end', 'end_seconds', '结束秒', '素材内出点', '相对结束'),
    'time_basis': ('时间基准', '时间类型', 'time_basis', 'time_reference'),
}
_NOTE_COLUMNS = {_header_key(alias): field for field, aliases in _NOTE_ALIASES.items() for alias in aliases}
_LOCAL_TIME_COLUMNS = {_header_key(alias) for alias in ('start_seconds', 'end_seconds', '开始秒', '起始秒', '结束秒', '素材内入点', '素材内出点', '相对开始', '相对结束')}


def _note_time(value):
    """Parse only seconds/clock precision; preserve frame timecodes separately."""
    text = str(value).strip()
    if re.fullmatch(r'\d+:\d{2}:\d{2}[:;]\d{2}', text):
        return 'timecode', text
    numeric = re.fullmatch(r'([+-]?\d+(?:[.,]\d+)?)\s*(?:s|sec|seconds?|秒)?', text, re.I)
    if numeric:
        number = float(numeric.group(1).replace(',', '.'))
    elif _CUE_TIME.fullmatch(text):
        number = _cue_seconds(text)
    else:
        return None, None
    if not math.isfinite(number) or number < 0:
        raise DialogueError('场记起止位置必须为非负秒数：' + text)
    return 'seconds', number


def _note_record(fields, rawtext, path, order):
    row = dict(order=order, source_path=str(path), rawtext=rawtext)
    local = False
    for header, value in fields.items():
        name = _NOTE_COLUMNS.get(_header_key(header))
        text = str(value or '').strip()
        if name and text:
            if name in row and row[name] != text:
                raise DialogueError('场记中同义列内容冲突：' + str(header))
            row[name] = text
            local = local or _header_key(header) in _LOCAL_TIME_COLUMNS
    for prefix in ('start', 'end'):
        if row.get(prefix + '_raw'):
            kind, value = _note_time(row[prefix + '_raw'])
            if kind:
                row[prefix + ('_seconds' if kind == 'seconds' else '_timecode')] = value
    if 'start_seconds' in row and 'end_seconds' in row and row['end_seconds'] <= row['start_seconds']:
        raise DialogueError(f'第 {order + 1} 条场记的结束位置必须晚于开始位置。')
    if 'start_seconds' in row or 'end_seconds' in row or 'start_timecode' in row or 'end_timecode' in row:
        requested_basis = _header_key(row.get('time_basis', ''))
        explicit_local = requested_basis in {_header_key(value) for value in ('source_local', '源本地', '素材内', '相对时间', '相对', 'relative')}
        row['time_basis'] = 'source_local' if local or explicit_local else row.get('time_basis', 'unspecified')
    return row


def import_notes(path):
    """Read field notes without inventing links to performances or media.

    Every row has order/source_path/rawtext. Recognized nonempty columns become
    session_name/session_id/scene/dialogue/notes/media_path/speaker/take/date/
    camera, plus actual timing values. CSV raw_fields preserves source columns.
    Frame timecodes remain strings because no frame rate or time origin is known.
    """
    path = Path(path).expanduser().resolve()
    try:
        if path.suffix.lower() == '.csv':
            text = _read_text(path)
            physical = text.splitlines(keepends=True)
            offset = 0
            delimiter = None
            if physical and re.fullmatch(r'sep=(.)\s*', physical[0].strip(), re.I):
                delimiter = physical[0].strip()[4:5]
                offset = 1
            data = ''.join(physical[offset:])
            if not data.strip():
                raise DialogueError('场记 CSV 没有内容。')
            try:
                dialect = csv.Sniffer().sniff(data[:65536], delimiters=',;\t|') if delimiter is None else None
            except csv.Error:
                dialect = csv.excel
            reader = csv.reader(io.StringIO(data, newline=''), dialect=dialect or csv.excel, **({'delimiter': delimiter} if delimiter else {}))
            headers = next(reader)
            meaningful = [header.strip() for header in headers if header.strip()]
            if len(set(meaningful)) != len(meaningful):
                raise DialogueError('场记 CSV 存在重名列，请修改列名后导入。')
            rows, previous_line = [], reader.line_num
            for values in reader:
                rawtext = ''.join(physical[offset + previous_line:offset + reader.line_num]).rstrip('\r\n')
                previous_line = reader.line_num
                if not any(value.strip() for value in values):
                    continue
                fields = {header: values[i] if i < len(values) else '' for i, header in enumerate(headers) if header.strip()}
                row = _note_record(fields, rawtext, path, len(rows))
                row['raw_fields'] = fields
                if len(values) > len(headers):
                    row['extra_values'] = values[len(headers):]
                rows.append(row)
        else:
            text = _document_text(path, '场记').replace('\r\n', '\n').replace('\r', '\n')
            blocks, current, seen = [], [], set()
            for line in text.split('\n'):
                if not line.strip():
                    if current:
                        blocks.append('\n'.join(current)); current = []; seen = set()
                    continue
                labelled = re.match(r'^\s*([^：:]{1,30})\s*[：:]\s*(.*)$', line)
                field = _NOTE_COLUMNS.get(_header_key(labelled[1])) if labelled else None
                bare_scene = bool(_SCENE.match(line)) and field != 'scene'
                if current and ((field in ('session_name', 'session_id', 'scene') and field in seen) or
                                (bare_scene and bool(seen - {'session_name', 'session_id', 'date', 'camera', 'take'}))):
                    blocks.append('\n'.join(current)); current = []; seen = set()
                current.append(line)
                seen.add(field or ('scene' if bare_scene else 'raw'))
            if current:
                blocks.append('\n'.join(current))
            rows = []
            for block in blocks:
                fields = {}
                for line in block.split('\n'):
                    match = re.match(r'^\s*([^：:]{1,30})\s*[：:]\s*(.*)$', line)
                    if match and _header_key(match[1]) in _NOTE_COLUMNS:
                        key, value = match[1].strip(), match[2].strip()
                        if key in fields and fields[key] != value:
                            value = fields[key] + '\n' + value
                        fields[key] = value
                    elif _SCENE.match(line):
                        fields.setdefault('scene', line.strip())
                rows.append(_note_record(fields, block, path, len(rows)))
        if not rows:
            raise DialogueError('场记没有可读取的内容。')
        return rows
    except DialogueError:
        raise
    except Exception as exc:
        raise DialogueError('场记导入失败：' + str(exc)) from exc


_CN_DIGITS = dict(zip('零〇一二两三四五六七八九', (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
_CN_UNITS = {'十': 10, '百': 100, '千': 1000, '万': 10000, '亿': 100000000}


def _number(match):
    word = match.group()
    if not any(char in _CN_UNITS for char in word):
        return ''.join(str(_CN_DIGITS[c]) for c in word)
    total, section, digit = 0, 0, 0
    for char in word:
        if char in _CN_DIGITS:
            digit = _CN_DIGITS[char]
        else:
            unit = _CN_UNITS[char]
            if unit < 10000:
                section += (digit or 1) * unit
            else:
                section += digit
                total = (total + section) * unit if unit == 100000000 else total + section * unit
                section = 0
            digit = 0
    return str(total + section + digit)


@lru_cache(maxsize=1)
def _traditional_converter():
    try:
        from opencc import OpenCC
    except ImportError:
        return None
    return OpenCC('t2s')


def normalize_dialogue(text):
    text = unicodedata.normalize('NFKC', _spoken_text(str(text))).casefold()
    converter = _traditional_converter()
    if converter is not None:
        text = converter.convert(text)
    text = re.sub('[零〇一二两三四五六七八九十百千万亿]+', _number, text)
    # Chinese character tokens plus complete English words. Punctuation separates
    # English words rather than joining "he is" into the unrelated word "heis".
    return ' '.join(re.findall(r'[a-z0-9]+(?:[’\'][a-z]+)?|[\u3400-\u9fff]|[^\W\d_]+', text))


def _ngrams(text):
    tokens = text.split()
    return set(zip(tokens, tokens[1:])) or set(tokens)


def _contains_tokens(shorter, longer):
    size = len(shorter)
    return bool(size) and size <= len(longer) and any(longer[i:i + size] == shorter for i in range(len(longer) - size + 1))


def _similarity(a, b):
    if not a or not b:
        return 0.
    if a == b:
        return 1.
    at, bt = a.split(), b.split()
    if min(len(at), len(bt)) <= 2:
        return 0.  # "是"/"好" and "go" must not match long unrelated dialogue.
    aa, bb = _ngrams(a), _ngrams(b)
    dice = 2 * len(aa & bb) / max(1, len(aa) + len(bb))
    ratio = SequenceMatcher(None, at, bt, autojunk=False).ratio()
    score = .65 * ratio + .35 * dice
    if _contains_tokens(at, bt) or _contains_tokens(bt, at):
        score = max(score, .65 + .3 * min(len(at), len(bt)) / max(len(at), len(bt)))
    return min(1., score)


class _DialogueSimilarity:
    """Bounded, call-local reuse of unchanged token/score calculations."""
    def __init__(self):
        self._features = OrderedDict()
        self._scores = OrderedDict()

    def features(self, text):
        value = self._features.get(text)
        if value is None:
            tokens = text.split()
            value = (tokens, Counter(tokens), set(zip(tokens, tokens[1:])) or set(tokens))
            if len(self._features) >= 8192:
                self._features.popitem(last=False)
            self._features[text] = value
        return value

    def similarity(self, a, b):
        if not a or not b:
            return 0.
        if a == b:
            return 1.
        key = (a, b)
        if key in self._scores:
            return self._scores[key]
        at, ac, aa = self.features(a)
        bt, bc, bb = self.features(b)
        if min(len(at), len(bt)) <= 2 or ac.keys().isdisjoint(bc):
            return 0.
        dice = 2 * len(aa & bb) / max(1, len(aa) + len(bb))
        ratio = SequenceMatcher(None, at, bt, autojunk=False).ratio()
        score = .65 * ratio + .35 * dice
        if _contains_tokens(at, bt) or _contains_tokens(bt, at):
            score = max(score, .65 + .3 * min(len(at), len(bt)) / max(len(at), len(bt)))
        score = min(1., score)
        if len(self._scores) >= 16384:
            self._scores.popitem(last=False)
        self._scores[key] = score
        return score

    def candidate_similarity(self, a, b, minimum):
        """Skip only pairs whose exact score cannot reach the candidate gate.

        Context scoring still uses similarity(), without this rejection bound.
        Rejected scores must never enter the exact-score cache.
        """
        if not a or not b:
            return 0.
        if a == b:
            return 1.
        at, ac, aa = self.features(a)
        bt, bc, bb = self.features(b)
        if min(len(at), len(bt)) <= 2 or ac.keys().isdisjoint(bc):
            return 0.
        common = sum(min(count, bc.get(token, 0)) for token, count in ac.items())
        upper = .65 * (2 * common / (len(at) + len(bt))) + .35 * (2 * len(aa & bb) / max(1, len(aa) + len(bb)))
        if common == min(len(at), len(bt)):
            upper = max(upper, .65 + .3 * min(len(at), len(bt)) / max(len(at), len(bt)))
        return 0. if upper < minimum - 1e-12 else self.similarity(a, b)


def _field(row, name):
    if row.get(name) not in (None, ''):
        return row[name]
    for container in ('data', 'metadata'):
        nested = row.get(container)
        if isinstance(nested, dict) and nested.get(name) not in (None, ''):
            return nested[name]
    return ''


def _full_media_path(value):
    text = str(value or '').strip().replace('\\', '/')
    if text.startswith(('/', 'smb://', 'nfs://')) or re.match(r'^[a-zA-Z]:/', text):
        return os.path.normpath(text)
    return ''


def _note_scope(note, item):
    """A basename or an unscoped scene heading cannot identify a recording."""
    media = _full_media_path(note.get('media_path'))
    matches_media = bool(media) and media == _full_media_path(item.get('media_path'))
    sid = note.get('session_id')
    name = note.get('session_name')
    if sid and str(sid) != str(item.get('session_id', '')):
        return False
    if name and normalize_dialogue(name) != normalize_dialogue(item.get('session_name', '')):
        return False
    matches_session = bool(sid or name)
    timed = any(key in note for key in ('start_seconds', 'end_seconds', 'start_timecode', 'end_timecode'))
    has_range = 'start_seconds' in note and 'end_seconds' in note and note.get('time_basis') == 'source_local'
    if timed and not has_range:
        return False  # No inferred conversion from time-of-day/frame timecode.
    if not matches_media and not (matches_session and has_range):
        return False
    if media and not matches_media:
        return False
    if note.get('media_path') and not media:
        return False  # Do not guess between same-named clips on different cards.
    if has_range:
        try:
            begin, end = float(note['start_seconds']), float(note['end_seconds'])
            item_begin = float(item.get('start_seconds', item.get('start')))
            item_end = float(item.get('end_seconds', item.get('end')))
            if not all(math.isfinite(value) for value in (begin, end, item_begin, item_end)) or begin < 0 or end <= begin:
                return False
            if min(end, item_end) <= max(begin, item_begin):
                return False
        except (TypeError, ValueError):
            return False
    return True


def _continuous_windows(items, spoken, by_media, cancelled):
    """Short neighbouring utterances may together represent one script paragraph."""
    windows = []
    for indexes in by_media.values():
        indexes.sort(key=lambda i: float(items[i].get('start_seconds', items[i].get('start', i))))
        for start in range(len(indexes)):
            _check(cancelled)
            included = [indexes[start]]
            for end in range(start + 1, min(len(indexes), start + 6)):
                previous, following = items[indexes[end - 1]], items[indexes[end]]
                begin = following.get('start_seconds', following.get('start'))
                finish = previous.get('end_seconds', previous.get('end'))
                origin = items[indexes[start]].get('start_seconds', items[indexes[start]].get('start'))
                if begin is not None and finish is not None:
                    gap = float(begin) - float(finish)
                    if gap > 12 or gap < -.25:
                        break  # Overlapping alternatives are not consecutive speech.
                if begin is not None and origin is not None and float(begin) - float(origin) > 90:
                    break
                if _field(previous, 'speaker') and _field(following, 'speaker') and normalize_dialogue(_field(previous, 'speaker')) != normalize_dialogue(_field(following, 'speaker')):
                    break
                included.append(indexes[end])
                parts = [spoken[i].split() for i in included]
                tokens = [token for part in parts for token in part]
                if len(tokens) > 250:
                    break
                bounds, offset = [], 0
                for index, part in zip(included, parts):
                    bounds.append((index, offset, offset + len(part)))
                    offset += len(part)
                windows.append((' '.join(tokens), tokens, Counter(tokens), bounds))
    return _WindowIndex(windows)


class _WindowIndex(list):
    """A call-local token index; omitted windows provably fail the old gate.

    Punctuation and dates never become locations. The original window records
    remain iterable so scoring and tests can use exactly the same evidence.
    """
    def __init__(self, windows):
        super().__init__(windows)
        self.postings = defaultdict(list)
        for index, (_, _, counts, _) in enumerate(windows):
            for token, count in counts.items():
                self.postings[token].append((index, count))

    def candidates(self, tokens, counts):
        shared = defaultdict(int)
        for token, count in counts.items():
            for index, amount in self.postings.get(token, ()):
                shared[index] += min(count, amount)
        size = len(tokens)
        return ((index, self[index]) for index in sorted(shared)
                if len(self[index][1]) <= size * 1.65 + 3 and
                shared[index] / max(1, min(size, len(self[index][1]))) >= .55)


def _script_sentence_units(row, normalizer=normalize_dialogue):
    """Search inside a long speaking block while retaining its one script ID.

    ASR segments can be much shorter than an actor's printed paragraph. Looking
    up actual sentences avoids both losing them and treating a tiny generic
    phrase contained in a very long paragraph as a strong anchor.
    """
    text = _spoken_text(row.get('text', ''))
    whole = normalizer(text)
    if len(whole.split()) < 26:
        return ()
    pieces = [normalizer(value) for value in re.split(r'[。！？!?；;\n]+|(?<=[a-zA-Z])\.(?:\s|$)', text)]
    pieces = [value for value in pieces if value]
    units = []
    for index, value in enumerate(pieces):
        count = len(value.split())
        if 5 <= count <= 120 and value != whole:
            units.append(value)
        if index + 1 < len(pieces):
            pair = value + ' ' + pieces[index + 1]
            if 8 <= len(pair.split()) <= 120 and pair != whole:
                units.append(pair)
    return tuple(dict.fromkeys(units))


def _partial_dialogue(script, speech, comparison, candidate_similarity):
    """Score an actual short excerpt in a long speaking block, never its order.

    ASR boundaries need not coincide with printed punctuation. A few shared
    consecutive words locate possible excerpts; the ordinary full-token score
    must then exceed the gate. The common-token and length gates keep this
    bounded and reject generic one-word matches before sequence comparison.
    """
    tokens, counts, grams = comparison.features(script)
    spoken, spoken_counts, spoken_grams = comparison.features(speech)
    size = len(spoken)
    if size < 6 or len(tokens) < 26 or size > len(tokens) * .75:
        return 0., 0
    if (sum(min(count, counts.get(token, 0)) for token, count in spoken_counts.items()) < size * .6 or
            len(grams & spoken_grams) < 2):
        return 0., 0
    blocks = sorted(SequenceMatcher(None, spoken, tokens, autojunk=False).get_matching_blocks(),
                    key=lambda block: -block.size)
    offsets = list(dict.fromkeys(block.b - block.a for block in blocks if block.size >= 2))[:2]
    best, extent = 0., 0
    for offset in offsets:
        for delta in (-1, 0, 1):
            begin = max(0, offset + delta)
            for length in (size, size + 2):
                excerpt = tokens[begin:begin + length]
                if len(excerpt) < size * .75:
                    continue
                value = candidate_similarity(' '.join(excerpt), speech, .6)
                if value > best:
                    best, extent = min(.92, value), len(excerpt)
    return (best, extent) if best >= .6 else (0., 0)


def _joined_scores(script, windows, base_scores, cancelled, similarity=None, features=None):
    """Promote original segment candidates only when they contribute to a match."""
    context = _DialogueSimilarity() if features is None else None
    features = features or context.features
    similarity = similarity or (context.similarity if context is not None else _similarity)
    tokens, counts, grams = features(script)
    if len(tokens) < 3:
        return {}
    best = {}
    indexed = windows.candidates(tokens, counts) if hasattr(windows, 'candidates') else enumerate(windows)
    for number, (joined, joined_tokens, joined_counts, bounds) in indexed:
        if number % 100 == 0:
            _check(cancelled)
        if len(joined_tokens) > len(tokens) * 1.65 + 3:
            continue
        common = sum(min(count, joined_counts.get(token, 0)) for token, count in counts.items())
        if common / max(1, min(len(tokens), len(joined_tokens))) < .55:
            continue
        _, _, joined_grams = features(joined)
        dice = 2 * len(grams & joined_grams) / max(1, len(grams) + len(joined_grams))
        # SequenceMatcher can match at most the shared multiset token count.
        # This is a rejection bound only: accepted scores still use the exact
        # original algorithm, order and floating-point expressions. A complete
        # containment boost is possible only if all shorter tokens are shared.
        upper = .65 * (2 * common / max(1, len(tokens) + len(joined_tokens))) + .35 * dice
        if common >= min(len(tokens), len(joined_tokens)):
            upper = max(upper, .65 + .3 * min(len(tokens), len(joined_tokens)) / max(len(tokens), len(joined_tokens)))
        if upper < .6 - 1e-12:
            continue
        score = similarity(script, joined)
        if score < .6:
            continue
        matching = SequenceMatcher(None, tokens, joined_tokens, autojunk=False).get_matching_blocks()
        for index, begin, finish in bounds:
            size = finish - begin
            if not size or base_scores[index] >= score:
                continue
            hits = sum(max(0, min(finish, block.b + block.size) - max(begin, block.b)) for block in matching)
            if hits / size < .5 or (size <= 2 and score < .8):
                continue
            # Retain the original segment ID and source IN/OUT. The window is
            # evidence of dialogue content, never an invented synced take.
            value = min(.98, score)
            if value > best.get(index, (0., ''))[0]:
                best[index] = (value, f'相邻 {len(bounds)} 段台词联合匹配，保留本段原始起止位置')
    return best


class _ScriptSpanIndex:
    """At most five consecutive 2–6-line windows per script position.

    Inverted token counts reject unrelated windows before exact comparison.
    An overflowing shortlist remains unresolved, rather than turning a bounded
    search into false evidence that its best result is unique.
    """
    MAX_TOKENS = 400
    MAX_CANDIDATES = 64

    def __init__(self, lines, texts):
        self.windows, self.postings = [], defaultdict(list)
        for start, first in enumerate(lines):
            if not texts[start] or not first.get('active', first.get('script_active', True)):
                continue
            family, scene = first.get('script_id', ''), first.get('scene', '')
            tokens, bounds, identities = [], [], []
            for position in range(start, min(len(lines), start + 6)):
                row = lines[position]
                if (not row.get('active', row.get('script_active', True)) or
                        row.get('script_id', '') != family or row.get('scene', '') != scene):
                    break
                part = texts[position].split()
                begin = len(tokens)
                tokens.extend(part)
                bounds.append((begin, len(tokens)))
                identities.append(row.get('id', row.get('script_line_id', row.get('order', position))))
                if len(tokens) > self.MAX_TOKENS:
                    break
                # Empty stage directions can be internal contiguous coverage,
                # but never the evidence at either end or the second speech.
                if position == start or not part or sum(end > begin for begin, end in bounds) < 2:
                    continue
                if any(not isinstance(identity, str) or not identity for identity in identities):
                    continue
                counts = Counter(tokens)
                window = (start, tuple(identities), ' '.join(tokens), tuple(tokens), counts, tuple(bounds))
                number = len(self.windows)
                self.windows.append(window)
                for token, count in counts.items():
                    self.postings[token].append((number, count))

    def candidates(self, speech_tokens):
        counts, shared = Counter(speech_tokens), defaultdict(int)
        for token, count in counts.items():
            for index, amount in self.postings.get(token, ()):
                shared[index] += min(count, amount)
        size = len(speech_tokens)
        possible = [index for index, common in shared.items()
                    if common >= .60 * size and common >= .60 * len(self.windows[index][3])]
        possible.sort(key=lambda index: (-shared[index] / max(size, len(self.windows[index][3]), 1), index))
        return ([self.windows[index] for index in possible[:self.MAX_CANDIDATES]],
                len(possible) > self.MAX_CANDIDATES)


def _span_alignment_evidence(window, speech_tokens):
    """Require real words from every line and from the first line's opening."""
    _, _, _, tokens, _, bounds = window
    blocks = SequenceMatcher(None, tokens, speech_tokens, autojunk=False).get_matching_blocks()
    blocks = [block for block in blocks if block.size]
    if not blocks or blocks[0].a > min(1, bounds[0][1] - 1) or blocks[0].b > 2:
        return False
    head_end = min(6, bounds[0][1])
    head_hits = sum(max(0, min(head_end, block.a + block.size) - block.a) for block in blocks)
    if head_hits < math.ceil(head_end * .67):
        return False
    # A one-word answer can open a span only when the following words also
    # corroborate that exact opening; its generic word alone is insufficient.
    opening_end = min(6, len(tokens))
    opening_hits = sum(max(0, min(opening_end, block.a + block.size) - block.a) for block in blocks)
    if opening_hits < math.ceil(opening_end * .67):
        return False
    for begin, end in bounds:
        if begin == end:
            continue
        hits = sum(max(0, min(end, block.a + block.size) - max(begin, block.a)) for block in blocks)
        if hits < math.ceil((end - begin) * .60):
            return False
    return sum(block.size for block in blocks) >= math.ceil(len(speech_tokens) * .70)


def _apply_script_span_matches(candidates, lines, items, texts, spoken, raw_scores,
                               anchor_sizes, comparison, cancelled, threshold, top_k):
    """Represent a merged ASR segment once, anchored to its actual first line.

    No ASR segment is split or retimed. Uniqueness is checked against all
    eligible bounded windows; existing exact single-line evidence is retained.
    """
    from dialogue_spans import encode_span_tags
    index = _ScriptSpanIndex(lines, texts)
    if not index.windows:
        return candidates
    exact_lines = set(texts)
    candidate_similarity = getattr(comparison, 'candidate_similarity',
                                   lambda a, b, minimum: comparison.similarity(a, b))
    by_item = defaultdict(list)
    for row in candidates:
        by_item[row['transcript_id']].append(row)
    replacements = {}
    for number, (item, speech) in enumerate(zip(items, spoken)):
        if number % 50 == 0:
            _check(cancelled)
        speech_tokens = comparison.features(speech)[0]
        if speech in exact_lines or len(speech_tokens) < 4 or len(speech_tokens) > index.MAX_TOKENS:
            continue
        windows, overflow = index.candidates(speech_tokens)
        if overflow:
            continue
        ranked = []
        for window in windows:
            score = candidate_similarity(window[2], speech, max(.78, threshold))
            if score < max(.78, threshold) or not _span_alignment_evidence(window, speech_tokens):
                continue
            ranked.append((score, window))
        ranked.sort(key=lambda value: (-value[0], value[1][0], len(value[1][1])))
        if not ranked or len(ranked) > 1 and ranked[0][0] - ranked[1][0] < .08 - 1e-12:
            continue
        score, window = ranked[0]
        position, identities = window[:2]
        transcript_id = item.get('id', item.get('transcript_id', number))
        existing = by_item.get(transcript_id, ())
        if existing and score < max(row['score'] for row in existing) + .025 - 1e-12:
            continue
        row = dict(script_line_id=identities[0], transcript_id=transcript_id,
                   script_line_ids=list(identities), tags=encode_span_tags(identities),
                   score=round(score, 4),
                   reason=f'连续 {len(identities)} 行台本联合匹配；首行开头有实际对白支持，保留整段ASR原始起止位置')
        replacements[transcript_id] = row
        identity = (identities[0], transcript_id)
        raw_scores[identity] = score
        anchor_sizes[identity] = len(window[3])
    if not replacements:
        return candidates
    # The constituent lines are coverage of this one association, not competing
    # interpretations of the same ASR segment. Distant alternatives remain
    # available to the ordinary sequence/ambiguity policy.
    result = [row for row in candidates
              if row['transcript_id'] not in replacements or
              row['script_line_id'] not in replacements[row['transcript_id']]['script_line_ids']]
    result.extend(replacements.values())
    sessions = {item.get('id', item.get('transcript_id', number)):
                item.get('session_id', item.get('session_name', ''))
                for number, item in enumerate(items)}
    groups = defaultdict(list)
    for row in result:
        groups[(row['script_line_id'], sessions[row['transcript_id']])].append(row)
    kept = set()
    for group in groups.values():
        group.sort(key=lambda row: (-row['score'], str(row['transcript_id'])))
        kept.update((row['script_line_id'], row['transcript_id']) for row in group[:int(top_k)])
    return [row for row in result if (row['script_line_id'], row['transcript_id']) in kept]


def _distributed_anchor_ranges(candidates, lines, by_item, by_source, anchors, line_ids, cancelled):
    """Use dispersed reliable dialogue, without assuming constant performance speed.

    First/middle/last neighbourhoods are useful evidence, not three mandatory
    samples: an opening slate, silence, or an incomplete last sentence may be
    unusable. All reliable anchors are retained; reverse-order restarts split
    the evidence into runs. At least three distinct positions are required for
    a wider range. Neither source times nor similarity scores are changed.
    """
    rejected, supported = set(), set()
    for records in by_source.values():
        _check(cancelled)
        records = sorted(records)
        ordered = [(offset, index, anchors[index]) for offset, (_, _, index) in enumerate(records)
                   if index in anchors]
        if len(ordered) < 3 or len(records) < 6:
            continue
        # A single accidentally exact generic sentence is not a restart. Both
        # sides need three progressing anchors before a backwards jump splits
        # one recorded file into separate performances.
        cuts = []
        for pivot in range(3, len(ordered) - 2):
            before, after = ordered[pivot - 3:pivot], ordered[pivot:pivot + 3]
            stable = lambda values: (all(a[2] <= b[2] for a, b in zip(values, values[1:])) and
                                     values[-1][2] - values[0][2] <= 128)
            if stable(before) and stable(after) and before[-1][2] - after[-1][2] >= 3:
                cuts.append(ordered[pivot][0])
        sections = defaultdict(list)
        for row in ordered:
            sections[sum(row[0] >= cut for cut in cuts)].append(row)
        runs, ignored = [], set()
        for section_number, section in sections.items():
            # The longest supported order removes isolated false anchors.
            # If equally supported paths diverge, only their common positions
            # can constrain another utterance; arbitrary tie-breaking must not
            # turn an uncertain scene into an automatic match.
            forward, backward = [1] * len(section), [1] * len(section)
            for index, row in enumerate(section):
                if index % 100 == 0:
                    _check(cancelled)
                forward[index] += max((forward[previous] for previous in range(index)
                                       if section[previous][2] <= row[2]), default=0)
            for index in range(len(section) - 1, -1, -1):
                if index % 100 == 0:
                    _check(cancelled)
                backward[index] += max((backward[following] for following in range(index + 1, len(section))
                                        if section[following][2] >= section[index][2]), default=0)
            length = max(forward, default=0)
            layers = defaultdict(list)
            for index, row in enumerate(section):
                if forward[index] + backward[index] - 1 == length:
                    layers[forward[index]].append(row)
            run = [values[0] for _, values in sorted(layers.items())
                   if len({row[2] for row in values}) == 1]
            if len({row[2] for row in run}) >= 3:
                bounds = ([0] + cuts + [len(records)])[section_number:section_number + 2]
                runs.append((run, bounds))
                valid = {row[1] for row in run}
                ignored.update(row[1] for row in section if row[1] not in valid)
        for run, bounds in runs:
            if len({row[2] for row in run}) < 3:
                continue
            start_offset, end_offset = bounds
            origin, finish = records[start_offset][0], records[end_offset - 1][1]
            duration = finish - origin
            tiers = {0 if (records[row[0]][0] - origin) / duration <= .15 else
                     2 if (records[row[0]][1] - origin) / duration >= .85 else
                     1 if .25 <= (records[row[0]][0] - origin) / duration <= .75 else -1
                     for row in run} if duration > 0 else set()
            if {0, 1, 2}.issubset(tiers):
                # All three portions independently identify this script area.
                # Keep a generous paragraph margin at the edges. This rejects
                # isolated distant short-phrase hits that would otherwise make
                # the whole PGM appear to cover nearly the entire play.
                lower, upper = min(row[2] for row in run) - 16, max(row[2] for row in run) + 16
                for offset in range(start_offset, end_offset):
                    index = records[offset][2]
                    for row in by_item.get(index, ()):
                        if not lower <= line_ids[row['script_line_id']] <= upper:
                            rejected.add((row['script_line_id'], row['transcript_id']))
            for left, right in zip(run, run[1:]):
                if right[2] < left[2] or right[0] <= left[0] + 1:
                    continue
                # Do not bridge overlapping ASR alternatives. Pauses alone do
                # not invalidate three corroborating pieces of dialogue.
                if any(records[offset][0] < records[offset - 1][1] - .25
                       for offset in range(left[0] + 1, right[0] + 1)):
                    continue
                first, last = left[2] - 2, right[2] + 2
                for offset in range(left[0] + 1, right[0]):
                    index = records[offset][2]
                    if index in anchors and index not in ignored:
                        continue
                    rows = by_item.get(index, ())
                    positions = {line_ids[row['script_line_id']] for row in rows}
                    allowed = {position for position in positions if first <= position <= last}
                    if allowed == positions:
                        continue
                    for row in rows:
                        key = (row['script_line_id'], row['transcript_id'])
                        (supported if line_ids[row['script_line_id']] in allowed else rejected).add(key)
        for index in ignored:
            anchors.pop(index, None)
    return rejected, supported


def _sequence_disambiguate(candidates, lines, items, spoken, raw_scores, cancelled, anchor_sizes=None):
    """Resolve competing positions using nearby speech from this PGM only.

    Independent line scores can send repeated dialogue to distant scenes. Two
    reliable local anchors can rule out such positions without manufacturing a
    higher text score. A restart, missing timing, long gap, different source, or
    contradictory anchors provides no ordering evidence. Never infer a position
    that the text matcher did not find; a reliably contradicted utterance can
    remain unmatched for human review.
    """
    line_ids = {row.get('id', row.get('script_line_id', row.get('order', i))): i
                for i, row in enumerate(lines)}
    item_ids = {row.get('id', row.get('transcript_id', i)): i for i, row in enumerate(items)}
    by_item, by_source = defaultdict(list), defaultdict(list)
    for row in candidates:
        index = item_ids.get(row['transcript_id'])
        if index is not None:
            by_item[index].append(row)
    for index, item in enumerate(items):
        source = item.get('media_id') or _full_media_path(item.get('media_path'))
        session = item.get('session_id') or item.get('session_name')
        if not source or not session:
            continue
        try:
            begin = float(item.get('start_seconds', item.get('start')))
            end = float(item.get('end_seconds', item.get('end')))
            if not (math.isfinite(begin) and math.isfinite(end) and 0 <= begin < end):
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        by_source[(session, source)].append((begin, end, index))

    anchors = {}
    line_tokens = [normalize_dialogue(row.get('text', '')).split() for row in lines]
    for index, rows in by_item.items():
        speech_tokens = spoken[index].split()
        if len(speech_tokens) < 6:
            continue
        ranked = sorted(((raw_scores.get((row['script_line_id'], row['transcript_id']), 0.),
                          line_ids[row['script_line_id']]) for row in rows), reverse=True)
        if not ranked or ranked[0][0] < .78:
            continue
        # The anchor must be unique by its actual words, before metadata bonuses
        # and joint-window promotion. Repeated script lines cannot be anchors.
        if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < .08:
            continue
        position = ranked[0][1]
        if len(speech_tokens) <= 35 and any(number != position and _contains_tokens(speech_tokens, tokens)
                                           for number, tokens in enumerate(line_tokens)):
            continue  # A short line printed inside another paragraph is repeated evidence too.
        identity = (lines[position].get('id', lines[position].get('script_line_id', lines[position].get('order', position))),
                    items[index].get('id', items[index].get('transcript_id', index)))
        script_size = (anchor_sizes or {}).get(identity) or len(normalize_dialogue(lines[position].get('text', '')).split())
        speech_size = len(spoken[index].split())
        if min(script_size, speech_size) / max(script_size, speech_size, 1) >= .55:
            anchors[index] = position

    rejected, supported = _distributed_anchor_ranges(candidates, lines, by_item, by_source,
                                                     anchors, line_ids, cancelled)

    def same_scene(first, second):
        a, b = lines[first].get('scene'), lines[second].get('scene')
        return not a or not b or a == b

    for records in by_source.values():
        records.sort()
        chains, current = [], []
        for begin, end, index in records:
            if current and not -.25 <= begin - current[-1][1] <= 12:
                chains.append(current)
                current = []
            current.append((begin, end, index))
        if current:
            chains.append(current)
        for chain in chains:
            local_anchors = {index: anchors[index] for _, _, index in chain if index in anchors}
            # ASR often splits one actor's paragraph into several short pieces.
            # Multiple unique pieces agreeing on that same paragraph provide
            # local context even if no individual piece is a long-text anchor.
            joint_support = defaultdict(list)
            for offset, (_, _, index) in enumerate(chain):
                rows = by_item.get(index, [])
                ranked = sorted(rows, key=lambda row: row['score'], reverse=True)
                if not ranked or ranked[0]['score'] < .72:
                    continue
                first = ranked[0]
                if any(row['script_line_id'] != first['script_line_id'] and
                       first['score'] - row['score'] < .08 for row in ranked[1:]):
                    continue
                joint_support[line_ids[first['script_line_id']]].append((offset, index, first))
            for position, support in joint_support.items():
                for number, (offset, index, row) in enumerate(support):
                    nearby = [value for value in support[max(0, number - 4):number + 5]
                              if abs(value[0] - offset) <= 4]
                    if len(nearby) >= 2 and any('联合匹配' in value[2]['reason'] for value in nearby):
                        local_anchors[index] = position
            anchor_order = [(offset, index, local_anchors[index]) for offset, (_, _, index) in enumerate(chain)
                            if index in local_anchors]
            outliers = set()
            for previous, present, following in zip(anchor_order, anchor_order[1:], anchor_order[2:]):
                first, last = previous[2], following[2]
                if (present[0] - previous[0] <= 4 and following[0] - present[0] <= 4 and
                        0 <= last - first <= 16 and same_scene(first, last) and
                        not first <= present[2] <= last):
                    outliers.add(present[1])
            for index in outliers:
                local_anchors.pop(index)
            for offset, (_, _, index) in enumerate(chain):
                if offset % 100 == 0:
                    _check(cancelled)
                rows = by_item.get(index, [])
                positions = {line_ids[row['script_line_id']] for row in rows}
                if not positions or index in local_anchors:
                    continue
                left = [(distance, local_anchors[chain[offset - distance][2]])
                        for distance in range(1, min(offset, 4) + 1)
                        if chain[offset - distance][2] in local_anchors]
                right = [(distance, local_anchors[chain[offset + distance][2]])
                         for distance in range(1, min(len(chain) - offset - 1, 4) + 1)
                         if chain[offset + distance][2] in local_anchors]
                allowed = None
                if left and right:
                    first, last = left[0][1], right[0][1]
                    if 0 <= last - first <= 16 and same_scene(first, last):
                        allowed = {position for position in positions if first <= position <= last}
                elif len(right) >= 2 and right[0][0] == 1 and right[1][0] == 2:
                    first, second = right[0][1], right[1][1]
                    if second == first + 1 and same_scene(first, second):
                        allowed = positions.intersection((first - 1, first))
                elif len(left) >= 2 and left[0][0] == 1 and left[1][0] == 2:
                    last, previous = left[0][1], left[1][1]
                    if last == previous + 1 and same_scene(previous, last):
                        allowed = positions.intersection((last, last + 1))
                # With only one side, leave nearby competing positions intact:
                # skipped/reordered dialogue cannot be disambiguated by distance.
                if allowed and not (left and right):
                    nearest = (left or right)[0][1]
                    allowed.update(position for position in positions if abs(position - nearest) <= 4)
                if allowed is None or allowed == positions or not allowed and not (left and right):
                    continue
                for row in rows:
                    key = (row['script_line_id'], row['transcript_id'])
                    (supported if line_ids[row['script_line_id']] in allowed else rejected).add(key)
    if not rejected:
        return candidates
    result = []
    for row in candidates:
        key = (row['script_line_id'], row['transcript_id'])
        if key in rejected:
            continue
        if key in supported:
            row = dict(row, reason=row['reason'] + '；同一PGM连续台词顺序及分散锚点支持该剧情位置')
        result.append(row)
    return result


def match_dialogue(script_lines, transcripts, threshold=.45, top_k=5,
                   progress=lambda percent, message: None, cancelled=lambda: False, notes=None):
    """Rank candidates per script paragraph *and session*, without confirming them.

    Repeated lines retain competing positions until reliable local speech
    context disambiguates them; uncertainty keeps an explicit ambiguity reason.
    Only neighbouring transcript items from the same session AND source media
    contribute context. Different performances never become sync offsets.
    """
    if not 0 <= threshold <= 1 or int(top_k) < 1:
        raise DialogueError('匹配阈值应在 0–1 之间，每场候选数量至少为 1。')
    lines, items = list(script_lines), list(transcripts)
    comparison = _DialogueSimilarity()
    similarity = comparison.similarity
    candidate_similarity = getattr(comparison, 'candidate_similarity', lambda a, b, minimum: similarity(a, b))
    normalized = {}

    def normalize(value):
        value = str(value or '')
        if value not in normalized:
            normalized[value] = normalize_dialogue(value)
        return normalized[value]
    field_notes = list(notes or [])
    if any(not isinstance(note, dict) for note in field_notes):
        raise DialogueError('结构化场记必须为对象列表。')
    hints = []
    for index, item in enumerate(items):
        if index % 100 == 0:
            _check(cancelled)
        hints.append([note for note in field_notes if _note_scope(note, item)])
    texts = [normalize(row.get('text', '')) for row in lines]
    spoken = [normalize(row.get('text', '')) for row in items]
    sentence_units = [_script_sentence_units(row, normalize) for row in lines]
    speech_tokens = [comparison.features(text)[0] for text in spoken]
    speech_postings = defaultdict(set)
    for index, tokens in enumerate(speech_tokens):
        for token in set(tokens):
            speech_postings[token].add(index)
    repetitions = Counter(text for text in texts if text)
    by_media = defaultdict(list)
    for i, row in enumerate(items):
        by_media[(row.get('session_id', row.get('session_name', '')), row.get('media_id', row.get('media_path', '')))].append(i)
    neighbours = {}
    for indexes in by_media.values():
        indexes.sort(key=lambda i: float(items[i].get('start_seconds', items[i].get('start', i))))
        for j, i in enumerate(indexes):
            neighbours[i] = (indexes[j - 1] if j else None, indexes[j + 1] if j + 1 < len(indexes) else None)
    windows = _continuous_windows(items, spoken, by_media, cancelled)
    output, raw_scores, anchor_sizes = [], {}, {}
    for li, line in enumerate(lines):
        _check(cancelled)
        groups = defaultdict(list)
        script_tokens = comparison.features(texts[li])[0]
        possible = set()
        for token in set(script_tokens):
            possible.update(speech_postings.get(token, ()))
        bases = [0.] * len(spoken)
        for ti in sorted(possible):
            bases[ti] = candidate_similarity(texts[li], spoken[ti], max(.3, threshold - .1))
        joined_scores = _joined_scores(texts[li], windows, bases, cancelled,
                                       similarity=similarity, features=comparison.features)
        for ti, item in enumerate(items):
            if ti % 100 == 0:
                _check(cancelled)
            base = bases[ti]
            joint = joined_scores.get(ti)
            if joint and joint[0] > base:
                base = joint[0]
            sentence_score, sentence_size, partial_used = 0., 0, False
            # Existing strong full-paragraph/joined evidence keeps its exact
            # score and reason. Rescue only a short ASR sentence that otherwise
            # fails the sixty-percent dialogue gate.
            if (base < .6 and len(script_tokens) >= 26 and
                    len(speech_tokens[ti]) >= 4 and ti in possible):
                for unit in sentence_units[li]:
                    size = len(comparison.features(unit)[0])
                    if min(size, len(speech_tokens[ti])) / max(size, len(speech_tokens[ti]), 1) < .45:
                        continue
                    value = candidate_similarity(unit, spoken[ti], .6)
                    if value >= .6 and value > sentence_score:
                        sentence_score, sentence_size = min(.94, value), size
                if sentence_score > base:
                    base = sentence_score
                if base < .6:
                    value, size = _partial_dialogue(texts[li], spoken[ti], comparison, candidate_similarity)
                    if value > base:
                        base = sentence_score = value
                        sentence_size, partial_used = size, True
            if base < max(.3, threshold - .1):
                continue
            sentence_used = sentence_score > max(bases[ti], joint[0] if joint else 0.)
            reasons = ['台本段内连续字词匹配，保留整段台本行号' if sentence_used and partial_used else
                       '台本段内句子匹配，保留整段台本行号' if sentence_used else
                       joint[1] if joint and joint[0] > bases[ti] else
                       '原文相同' if base == 1 else '字词相似，允许改词或漏词']
            bonus, penalty = 0., 0.
            if _field(line, 'speaker') and _field(item, 'speaker'):
                if normalize(_field(line, 'speaker')) == normalize(_field(item, 'speaker')):
                    bonus += .025; reasons.append('人物一致')
                else:
                    penalty += .08; reasons.append('人物不同，请核对')
            prev, following = neighbours[ti]
            contexts = []
            if li and prev is not None and (not line.get('scene') or not lines[li - 1].get('scene') or line['scene'] == lines[li - 1]['scene']):
                contexts.append(similarity(texts[li - 1], spoken[prev]))
            if li + 1 < len(lines) and following is not None and (not line.get('scene') or not lines[li + 1].get('scene') or line['scene'] == lines[li + 1]['scene']):
                contexts.append(similarity(texts[li + 1], spoken[following]))
            if contexts and max(contexts) >= .5:
                bonus += .045 * sum(contexts) / len(contexts); reasons.append('前后台词支持')
            scene = normalize(line.get('scene', ''))
            related = hints[ti]
            note = normalize(' '.join(str(_field(item, k)) for k in ('scene', 'notes', 'slate', 'scene_note')) + ' ' +
                                      ' '.join(str(value.get(k) or '') for value in related for k in ('scene', 'notes')))
            if scene and note and (_contains_tokens(scene.split(), note.split()) or _contains_tokens(note.split(), scene.split())):
                bonus += .025; reasons.append('场记／幕场支持')
            if any(value.get('dialogue') and similarity(texts[li], normalize(value['dialogue'])) >= .65 for value in related):
                bonus += .025; reasons.append('场记台词线索支持，仍须核对实际对白')
            score = min(1., max(0., base + bonus - penalty))
            if score < threshold:
                continue
            if repetitions[texts[li]] > 1:
                reasons.append('台本有重复台词，须人工确认剧情位置')
                score = min(score, .84)
            groups[item.get('session_id', item.get('session_name', ''))].append(
                dict(script_line_id=line.get('id', line.get('script_line_id', line.get('order', li))),
                     transcript_id=item.get('id', item.get('transcript_id', ti)),
                     score=round(score, 4), reason='；'.join(reasons)))
            identity = (line.get('id', line.get('script_line_id', line.get('order', li))),
                        item.get('id', item.get('transcript_id', ti)))
            raw_scores[identity] = sentence_score if sentence_used else bases[ti]
            if sentence_used:
                anchor_sizes[identity] = sentence_size
        for candidates in groups.values():
            candidates.sort(key=lambda row: (-row['score'], str(row['transcript_id'])))
            repeated = len(candidates) > 1 and candidates[0]['score'] - candidates[1]['score'] < .035
            kept = 0
            for value in candidates:
                if repeated:
                    value['score'] = min(value['score'], .84)
                    value['reason'] += '；同场存在相近候选，须人工确认'
                if value['score'] < threshold:
                    continue
                output.append(value)
                kept += 1
                if kept >= int(top_k):
                    break
        progress(round((li + 1) * 100 / max(1, len(lines))), f'匹配台词 {li + 1}/{len(lines)}')
    _check(cancelled)
    output = _apply_script_span_matches(output, lines, items, texts, spoken, raw_scores,
                                        anchor_sizes, comparison, cancelled, threshold, top_k)
    return _sequence_disambiguate(output, lines, items, spoken, raw_scores, cancelled, anchor_sizes)


def _extract_audio(path, output_path, start, duration, progress, cancelled):
    """Stream media into 16 kHz mono PCM on disk, bounded by one decoded frame."""
    try:
        import av
    except ImportError as exc:
        raise DialogueError('语音识别需要 PyAV；请使用软件的完整运行环境。') from exc
    try:
        with av.open(str(path)) as container, wave.open(str(output_path), 'wb') as target:
            if not container.streams.audio:
                raise DialogueError('素材没有参考声音，无法识别台词：' + Path(path).name)
            stream = container.streams.audio[0]
            origin = float(container.start_time / av.time_base) if container.start_time is not None else float((stream.start_time or 0) * stream.time_base)
            length = duration
            if length is None and container.duration is not None:
                length = max(0., container.duration / av.time_base - start)
            if start:
                container.seek(int((origin + start) / float(stream.time_base)), stream=stream,
                               backward=True, any_frame=False)
            target.setnchannels(1); target.setsampwidth(2); target.setframerate(16000)
            resampler = av.AudioResampler(format='s16', layout='mono', rate=16000)
            written, expected, last_progress = 0, 0., -1
            stop = None if duration is None else start + duration

            def write_frame(frame):
                nonlocal written, expected, last_progress
                _check(cancelled)
                data = frame.to_ndarray().reshape(-1).astype('<i2', copy=False)
                time = float(frame.pts * frame.time_base) - origin if frame.pts is not None else expected
                expected = time + len(data) / 16000
                first = max(0, round((start - time) * 16000))
                last = len(data) if stop is None else min(len(data), round((stop - time) * 16000))
                if last <= first:
                    return
                position = max(0, round((time - start) * 16000) + first)
                # Keep gaps and nonzero audio start aligned with media-local seconds.
                gap = position - written
                while gap > 0:
                    _check(cancelled)
                    count = min(gap, 16000)
                    target.writeframesraw(b'\0\0' * count)
                    gap -= count; written += count
                first += max(0, written - position)
                if last > first:
                    target.writeframesraw(data[first:last].tobytes()); written += last - first
                if length:
                    percent = min(15, round(written / 16000 / length * 15))
                    if percent != last_progress:
                        progress(percent, '提取参考声音')
                        last_progress = percent

            finished = False
            for frame in container.decode(stream):
                _check(cancelled)
                for out in resampler.resample(frame):
                    write_frame(out)
                if stop is not None and expected >= stop:
                    finished = True; break
            if not finished:
                for out in resampler.resample(None):
                    write_frame(out)
            if not written:
                raise DialogueError('所选范围没有可解码的参考声音。')
            return written / 16000
    except DialogueError:
        raise
    except Exception as exc:
        raise DialogueError('参考声音提取失败：' + str(exc)) from exc


def resolve_model_path(model, cache):
    """Prefer complete explicit/manual/Hugging Face caches before downloading.

    tokenizer.json is required here: otherwise faster-whisper may attempt a
    separate implicit tokenizer download even when model.bin is local.
    """
    name = str(model)
    requested = Path(name).expanduser()
    required = ('model.bin', 'config.json', 'tokenizer.json')

    def complete(directory):
        return directory.is_dir() and all((directory / part).is_file() and (directory / part).stat().st_size > 0 for part in required)

    if requested.is_dir():
        if not complete(requested):
            raise DialogueError('本地语音模型不完整，需要 model.bin、config.json 和 tokenizer.json：' + str(requested))
        return str(requested.resolve())
    if '/' in name or '\\' in name:
        raise DialogueError('找不到本地语音模型目录：' + name)
    cache = Path(cache)
    direct = cache / name
    if complete(direct):
        return str(direct.resolve())
    snapshot_root = cache / ('models--Systran--faster-whisper-' + name) / 'snapshots'
    if snapshot_root.is_dir():
        versions = sorted((p for p in snapshot_root.iterdir() if complete(p)), key=lambda p: p.stat().st_mtime_ns, reverse=True)
        if versions:
            return str(versions[0].resolve())
    bundled = Path(__file__).resolve().parent / 'bundled_models' / name
    if complete(bundled):
        return str(bundled)
    if (Path(__file__).resolve().parent / 'offline_installation.json').is_file():
        raise DialogueError('离线模型缺失或不完整，请重新运行完整离线安装包；不会联网下载：' + name)
    return name


def model_cache_dir():
    home = Path(os.environ.get('PGM_LIBRARY_HOME') or
                '~/Library/Application Support/PGM机位识别').expanduser()
    return home.resolve() / 'models'


def configure_hf_cache(cache):
    """Set defaults before importing HF, without overriding user preferences.

    download_root alone does not relocate Hub commit caches or Xet logs. Keep
    those under the same application-owned root rather than ~/.cache. This is
    intentionally invoked only by the explicit transcription entry point.
    """
    cache = Path(cache).expanduser().resolve()
    defaults = {'HF_HOME': str(cache.parent / 'huggingface'),
                'HF_HUB_CACHE': str(cache),
                'HF_XET_CACHE': str(cache.parent / 'huggingface' / 'xet')}
    for name, value in defaults.items():
        os.environ.setdefault(name, value)
    return {name: os.environ[name] for name in defaults}


def transcribe_media(path, model='small', language='auto', progress=lambda percent, message: None,
                     cancelled=lambda: False, start_seconds=0, duration=None):
    """Optional CPU/int8 ASR, in bounded disk chunks; times are source-local.

    Model names can download on this explicit call only. A local model-directory
    path works offline. Whisper inference itself cannot be interrupted inside a
    native operation; cancellation is checked at every yielded segment/chunk.
    """
    _check(cancelled)
    path = Path(path).expanduser()
    if not path.is_file():
        raise DialogueError('找不到台词识别素材：' + str(path))
    try:
        start = float(start_seconds)
        length = None if duration is None else float(duration)
    except (ValueError, TypeError) as exc:
        raise DialogueError('识别范围必须为有效秒数。') from exc
    if not math.isfinite(start) or start < 0 or (length is not None and (not math.isfinite(length) or length <= 0)):
        raise DialogueError('识别范围必须为非负起点及正数时长。')
    language = None if not language or str(language).casefold() in ('auto', 'automatic', '自动') else str(language)
    cache = model_cache_dir()
    configure_hf_cache(cache)
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise DialogueError('语音识别尚未安装可选依赖 faster-whisper；可先导入 SRT、VTT 或转录 JSON。') from exc
    progress(0, '准备语音模型；首次使用模型名称需要下载，下载后可离线使用')
    try:
        cache.mkdir(parents=True, exist_ok=True)
        selected_model = resolve_model_path(model, cache)
        from speech_runtime import get_recognizer
        recognizer = get_recognizer(WhisperModel, selected_model, cache)
    except DialogueError:
        raise
    except Exception as exc:
        raise DialogueError('语音模型加载或下载失败；请检查网络或选择已下载的模型目录：' + str(exc)) from exc
    _check(cancelled)
    from speech_runtime import cached_transcription, remember_transcription
    stat = path.stat()
    source_key = (str(path.resolve()), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns,
                  stat.st_ctime_ns, start, length, language)
    cached = cached_transcription(recognizer, source_key)
    if cached is not None:
        progress(100, '复用已识别台词：' + path.name)
        return cached
    output = []
    with tempfile.TemporaryDirectory(prefix='pgm_dialogue_') as directory:
        audio = Path(directory) / 'reference.wav'
        total = _extract_audio(path, audio, start, length, progress, cancelled)
        with wave.open(str(audio), 'rb') as source:
            rate, frames = source.getframerate(), source.getnframes()
            # Automatic language is re-detected per minute for changing stage
            # dialogue. No translation task is used: preserve spoken language.
            step, overlap = (60 if language is None else 300) * rate, 2 * rate
            position = 0
            while position < frames:
                _check(cancelled)
                begin = max(0, position - (overlap if position else 0))
                count = min(step + position - begin, frames - begin)
                source.setpos(begin)
                progress(15 + round(position / max(1, frames) * 84), '识别台词：' + Path(path).name)
                try:
                    # faster-whisper 1.2.1's path decoder passes metadata_errors
                    # to av.open, removed in PyAV 19. Decode our own already-mono
                    # 16 kHz PCM chunk instead. Memory remains bounded to one
                    # 60/300-second chunk, without changing video dependencies.
                    import numpy as np
                    # Read the bounded PCM window directly. Writing and then
                    # reopening an identical chunk adds disk I/O on every pass.
                    samples = np.frombuffer(source.readframes(count), dtype='<i2').astype(np.float32)
                    samples /= 32768.
                    _check(cancelled)
                    segments, _ = recognizer.transcribe(samples, language=language or None,
                                                       task='transcribe', multilingual=language is None,
                                                       vad_filter=True, beam_size=5,
                                                       condition_on_previous_text=False)
                    for segment in segments:
                        _check(cancelled)
                        a = start + begin / rate + float(segment.start)
                        b = min(start + total, start + begin / rate + float(segment.end))
                        text = str(segment.text).strip()
                        if not text or b <= a:
                            continue
                        # Overlap is for complete boundary words, not duplicate dialogue.
                        if output and b <= output[-1]['end_seconds'] + .05:
                            continue
                        if output and a < output[-1]['end_seconds'] and _similarity(normalize_dialogue(text), normalize_dialogue(output[-1]['text'])) > .8:
                            output[-1]['end_seconds'] = max(output[-1]['end_seconds'], b)
                            continue
                        a = max(a, output[-1]['start_seconds'] if output else start)
                        output.append(dict(start_seconds=a, end_seconds=b, text=text))
                        progress(min(99, 15 + round((b - start) / total * 84)), '识别台词：' + Path(path).name)
                except DialogueError:
                    raise
                except Exception as exc:
                    raise DialogueError('语音识别失败，未保存为成功转录：' + str(exc)) from exc
                position += step
    _check(cancelled)
    progress(100, '台词识别完成' if output else '识别完成，未检测到可辨识台词；请核对声音或导入人工字幕')
    validated = validate_transcript(output)
    # Do not retain a result if the recording changed during the recognition.
    final_stat = path.stat()
    if (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) == (
            final_stat.st_dev, final_stat.st_ino, final_stat.st_size, final_stat.st_mtime_ns, final_stat.st_ctime_ns):
        remember_transcription(recognizer, source_key, validated)
    return validated
