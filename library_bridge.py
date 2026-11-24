"""Local settings and compatibility for existing media path aliases.

The single-workstation application uses LocalProjectClient and .pgmproj files.
Project data is stored in complete .pgmproj files without a service.
"""
from __future__ import annotations

import copy
import json
import os
import posixpath
import sys
import uuid
from pathlib import Path


def data_dir():
    value = os.environ.get('PGM_LIBRARY_HOME')
    if value:
        return Path(value).expanduser().resolve()
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support/PGM机位识别'
    return Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local/share')) / 'PGM机位识别'


def read_settings():
    try:
        result = json.loads((data_dir() / 'workstation.json').read_text(encoding='utf-8'))
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def write_settings(settings):
    settings = copy.deepcopy(settings)
    if 'path_mappings' in settings:
        settings['path_mappings'] = normalize_path_mappings(settings['path_mappings'])
    folder = data_dir(); folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'workstation.json'
    temporary = path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.chmod(0o600)
    temporary.replace(path)


def normalize_path_mappings(mappings):
    """Require a reversible directory mapping; never guess a media identity."""
    result = []
    for row in mappings:
        if not isinstance(row, dict):
            raise ValueError('路径映射需要共享路径和本机路径。')
        pair = {}
        for key in ('shared', 'local'):
            value = row.get(key, '')
            if not isinstance(value, str) or not value.strip().startswith('/'):
                raise ValueError('路径映射必须填写完整目录路径。')
            value = posixpath.normpath(value.strip())
            if value == '/' or '\x00' in value:
                raise ValueError('路径映射不能使用根目录或无效路径。')
            pair[key] = value
        if pair in result:
            continue
        for previous in result:
            for first, last in (('shared', 'local'), ('local', 'shared')):
                a, b = previous[first], pair[first]
                if a == b and previous[last] != pair[last]:
                    raise ValueError('同一目录不能映射到两个不同目录。')
                if b.startswith(a + '/') and pair[last] != previous[last] + b[len(a):]:
                    raise ValueError('嵌套路径映射不一致，无法安全还原素材路径。')
                if a.startswith(b + '/') and previous[last] != pair[last] + a[len(b):]:
                    raise ValueError('嵌套路径映射不一致，无法安全还原素材路径。')
        result.append(pair)
    return result


def remap_path(path, mappings, reverse=False):
    if not isinstance(path, str) or not path:
        return path
    pairs = []
    for item in normalize_path_mappings(mappings):
        first, last = item.get('shared', '').rstrip('/'), item.get('local', '').rstrip('/')
        if reverse:
            first, last = last, first
        if first and last:
            pairs.append((first, last))
    for first, last in sorted(pairs, key=lambda p: len(p[0]), reverse=True):
        if path == first or path.startswith(first + '/'):
            return last + path[len(first):]
    return path


def resolve_local_path(path):
    return remap_path(path, read_settings().get('path_mappings', []))


def canonical_path(path):
    return remap_path(path, read_settings().get('path_mappings', []), reverse=True)


def translate_config(config, mappings=None, reverse=False):
    """Translate path fields, preserving XML-embedded original PGM lookup keys."""
    mappings = read_settings().get('path_mappings', []) if mappings is None else mappings
    result = copy.deepcopy(config)
    translate = lambda value: remap_path(value, mappings, reverse)
    for key in ('cut_xml', 'pgm', 'output_dir'):
        if key in result:
            result[key] = translate(result[key])
    if 'cut_xml_paths' in result:
        result['cut_xml_paths'] = [translate(path) for path in result['cut_xml_paths']]
    for source in result.get('pgm_sources', []):
        if isinstance(source, dict) and source.get('path'):
            source['path'] = translate(source['path'])
    for source in result.get('sources', []):
        for key in ('path', 'proxy_path', 'original_path'):
            if key in source:
                source[key] = translate(source[key])
    for source in result.get('sound_sources', []):
        source['path'] = translate(source.get('path', ''))
    for source in result.get('library_media', []):
        source['path'] = translate(source.get('path', ''))
    result['pgm_timecodes'] = {translate(k): v for k, v in result.get('pgm_timecodes', {}).items()}
    # XML contains the original keys; only override destinations change.
    result['pgm_overrides'] = {k: translate(v) for k, v in result.get('pgm_overrides', {}).items()}
    for field in ('sound_reference_overrides', 'library_legacy_sound_references'):
        if field not in result:
            continue
        refs = {}
        for path, reference in result[field].items():
            reference = dict(reference); reference['path'] = translate(reference.get('path', ''))
            refs[translate(path)] = reference
        result[field] = refs
    for marker in result.get('dialogue_markers', []):
        marker['media_path'] = translate(marker.get('media_path', ''))
    xml_paths = result.get('cut_xml_paths') or ([result['cut_xml']] if result.get('cut_xml') else [])
    if not reverse:
        # Populate overrides from the XML without changing the XML or its digest.
        from proxy_workflow import file_path
        import xml.etree.ElementTree as ET
        for xml_path in xml_paths:
            if not Path(xml_path).is_file():
                continue
            root = ET.parse(xml_path).getroot()
            for url in root.iter('pathurl'):
                old = file_path(url.text)
                if old:
                    result['pgm_overrides'].setdefault(old, translate(old))
    return result


def canonicalize_payload(payload):
    result = copy.deepcopy(payload)
    if 'config' in result:
        result['config'] = translate_config(result['config'], reverse=True)
    return result


def ensure_source_ids(config):
    """Persist UUIDs in project JSON so a workstation path change keeps identity."""
    result = copy.deepcopy(config)
    for row in result.get('sources', []):
        row.setdefault('source_id', str(uuid.uuid4()))
    return result
