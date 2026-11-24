"""Atomic, self-contained single-workstation project files and recent-file indexes."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from library_bridge import data_dir


_CREDENTIAL_FIELDS = frozenset({
    'token', 'access_token', 'refresh_token', 'id_token', 'auth_token',
    'service_token', 'bearer_token', 'api_token', 'api_key', 'apikey', 'password',
    'authorization', 'secret', 'client_secret', 'library_connection',
})
_CREDENTIAL_NAMES = frozenset(re.sub(r'[^a-z0-9]', '', key) for key in _CREDENTIAL_FIELDS)


def _identity(value, label):
    if (not isinstance(value, str) or not re.fullmatch(
            r'(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})', value)):
        raise ValueError(label+'必须是项目 UUID。')
    # Accept compact or uppercase spellings, but never paths or UUID URLs.
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError(label+'必须是项目 UUID。') from exc


def _location(production_id):
    identity = _identity(production_id, '项目编号')
    folder = data_dir()/'projects'
    if folder.is_symlink():
        raise ValueError('项目保存目录不能是符号链接。')
    path = folder/(identity+'.json')
    if path.is_symlink():
        raise ValueError('项目保存文件不能是符号链接。')
    return identity, folder, path


def _without_credentials(value):
    if isinstance(value, dict):
        return {key: _without_credentials(child) for key, child in value.items()
                if isinstance(key, str) and re.sub(r'[^a-z0-9]', '', key.casefold()) not in _CREDENTIAL_NAMES}
    if isinstance(value, list):
        return [_without_credentials(child) for child in value]
    if isinstance(value, tuple):
        return [_without_credentials(child) for child in value]
    return value


def _snapshot(value):
    if value is None or value == {}:
        return {}
    if not isinstance(value, dict) or not isinstance(value.get('config'), dict):
        raise ValueError('项目快照需要 config 设置对象。')
    segments = value.get('segments', [])
    if not isinstance(segments, list) or any(not isinstance(row, dict) for row in segments):
        raise ValueError('项目快照的 segments 必须是镜头对象列表。')
    # A snapshot may have arrived from a larger window/service payload. Never
    # copy the client, URL, token, connection preferences or unrelated fields.
    return _without_credentials(dict(config=value['config'], segments=segments))


def _atomic_write(path, payload, prefix):
    folder = path.parent
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix='.'+prefix+'.', suffix='.tmp', dir=folder)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _project_file(value):
    path = Path(value).expanduser().absolute()
    if path.is_symlink():
        raise ValueError('项目保存文件不能是符号链接。')
    if path.suffix.lower() != '.pgmproj':
        raise ValueError('请选择 .pgmproj 项目文件。')
    return path


def write_project_state(production, session_id='', snapshot=None, save_path=None, catalogue=None) -> Path:
    """Atomically write a private checkpoint without changing source media."""
    if not isinstance(production, dict):
        raise ValueError('请选择或命名项目后再保存。')
    identity, folder, path = _location(production.get('id'))
    name = production.get('name')
    if not isinstance(name, str) or not name.strip():
        raise ValueError('项目名称不能为空。')
    session_id = _identity(session_id, '场次编号') if session_id else ''
    destination = _project_file(save_path) if save_path else path
    state = dict(version=1, production_id=identity, name=name.strip(), session_id=session_id,
                 snapshot=_snapshot(snapshot), saved_at=datetime.now(timezone.utc).isoformat(timespec='milliseconds'))
    if catalogue is not None:
        if not isinstance(catalogue, dict) or catalogue.get('production_id') != identity:
            raise ValueError('项目素材数据与项目编号不一致。')
        state.update(version=2, catalogue=_without_credentials(catalogue))
    if save_path:
        state['project_path'] = str(destination)
    # Validate every value before touching an existing checkpoint.
    try:
        payload = json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False)+'\n'
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('项目快照包含无法保存的数据。') from exc
    # Save the chosen file first: a failed external write must never report
    # success or advance the remembered project location.
    _atomic_write(destination, payload, identity)
    if destination != path:
        index = dict(state)
        index.pop('catalogue', None)
        index['version'] = 1
        _atomic_write(path, json.dumps(index, ensure_ascii=False, indent=2, allow_nan=False)+'\n', identity)
    if save_path and catalogue is not None:
        from local_project import remember_project
        remember_project(production, destination)
    return destination


def read_project_state(production_id, path=None) -> dict:
    """Return this project's valid checkpoint, or {} without media guessing.

    A missing/corrupt/foreign file is left untouched. The caller explicitly
    decides whether to restore the registered session or the saved snapshot.
    """
    identity, _, checkpoint = _location(production_id)
    path = _project_file(path) if path else checkpoint
    def invalid_constant(_):
        raise ValueError('项目快照包含非有限数值。')
    try:
        state = json.loads(path.read_text(encoding='utf-8'),
                           parse_constant=invalid_constant)
        if (not isinstance(state, dict) or state.get('version') not in (1, 2)
                or state.get('production_id') != identity
                or not isinstance(state.get('name'), str) or not state['name'].strip()):
            return {}
        session_id = state.get('session_id', '')
        session_id = _identity(session_id, '场次编号') if session_id else ''
        snapshot = _snapshot(state.get('snapshot'))
        result = dict(version=state['version'], production_id=identity, name=state['name'], session_id=session_id,
                      snapshot=snapshot, saved_at=state.get('saved_at', ''))
        if state['version'] == 2:
            catalogue = state.get('catalogue')
            if not isinstance(catalogue, dict) or catalogue.get('production_id') != identity:
                return {}
            result['catalogue'] = _without_credentials(catalogue)
        if state.get('project_path'):
            result['project_path'] = str(_project_file(state['project_path']))
        return result
    except (OSError, ValueError, TypeError, UnicodeError):
        return {}


def read_project_file(path) -> dict:
    """Read an explicit project file including its embedded material records."""
    file = _project_file(path)
    try:
        raw = json.loads(file.read_text(encoding='utf-8'))
        state = read_project_state(raw.get('production_id') if isinstance(raw, dict) else '', path=file)
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        raise ValueError('无法读取项目文件：'+str(exc)) from exc
    if not state:
        raise ValueError('项目文件损坏或格式不受支持。')
    state['project_path'] = str(file)
    return state
