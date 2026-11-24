"""Single-workstation project data; no server, socket or database file.

The existing transaction/index engine is transient in-memory state. All durable
material, script, transcript and review data is serialized into the .pgmproj.
"""
from __future__ import annotations
import copy
import json
import os
from pathlib import Path
import sqlite3
import threading
import urllib.parse

from library_store import Store, LibraryError, _json

TABLES = ('productions', 'sessions', 'scripts', 'script_lines', 'media', 'transcripts',
          'candidates', 'artifacts', 'reference_settings', 'audit_log')
JSON_FIELDS = frozenset(('config', 'segments', 'metadata', 'manifest', 'tags', 'details', 'data', 'settings'))


class LocalProjectClient:
    is_local_project = True
    actor = '本机'
    url = 'local-project://'
    token = ''

    def __init__(self):
        self.store = Store(':memory:')
        self._lock = threading.RLock()
        self.revision = 0
        self.last_saved_revision = 0

    def call(self, method, **params):
        if method == 'backup':
            raise LibraryError('invalid', '单机版请使用“保存项目”或“项目另存为”')
        with self._lock:
            result = self.store.call(method, params, self.actor)
            if not (method.startswith(('list_', 'get_')) or method == 'audit'):
                self.revision += 1
            return result

    def export_project(self, production_id):
        with self._lock, self.store._connection() as con:
            con.execute('BEGIN')
            tables = read_tables(con, production_id)
            return dict(version=1, production_id=production_id, revision=self.revision, tables=tables)

    @classmethod
    def from_project(cls, payload):
        if not isinstance(payload, dict) or payload.get('version') != 1 or not isinstance(payload.get('tables'), dict):
            raise ValueError('项目素材数据格式不受支持')
        identity = payload.get('production_id')
        tables = payload['tables']
        if set(tables) != set(TABLES):
            raise ValueError('项目素材数据不完整')
        if len(tables['productions']) != 1 or tables['productions'][0].get('id') != identity:
            raise ValueError('项目素材数据编号不一致')
        result = cls()
        try:
            with result.store._connection(True) as con:
                for table in TABLES:
                    columns = [r['name'] for r in con.execute('PRAGMA table_info('+table+')')]
                    rows = tables[table]
                    if not isinstance(rows, list):
                        raise ValueError('项目记录格式无效：' + table)
                    for row in rows:
                        if not isinstance(row, dict) or set(row) != set(columns):
                            raise ValueError('项目记录字段不完整：' + table)
                        if table != 'productions' and row.get('production_id') != identity:
                            raise ValueError('项目含其他项目的素材记录')
                        values = [_json(row[k]) if k in JSON_FIELDS else row[k] for k in columns]
                        con.execute('INSERT INTO '+table+' ('+','.join(columns)+') VALUES ('+
                                    ','.join('?' for _ in columns)+')', values)
                if con.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('项目素材关系不完整')
            revision = payload.get('revision', 0)
            if type(revision) is not int or revision < 0:
                raise ValueError('项目版本无效')
            result.revision = revision
            result.last_saved_revision = revision
            return result
        except Exception:
            result.close()
            raise

    def close(self):
        with self._lock:
            self.store.close()


def read_tables(con, production_id):
    tables = {}
    for table in TABLES:
        field = 'id' if table == 'productions' else 'production_id'
        rows = con.execute('SELECT * FROM '+table+' WHERE '+field+'=? ORDER BY '+
                           ('production_id' if table == 'reference_settings' else 'id'), (production_id,))
        values = []
        for raw in rows:
            row = dict(raw)
            for key in JSON_FIELDS.intersection(row):
                row[key] = json.loads(row[key])
            values.append(row)
        tables[table] = values
    if not tables['productions']:
        raise ValueError('项目记录不存在')
    return tables


def import_legacy_project(production_id, paths=None):
    """One-time read-only migration, never start/connect to the old daemon."""
    if paths is None:
        from library_bridge import data_dir
        folder = data_dir()
        paths = [folder/'library.sqlite3', folder/'server/library.sqlite3']
    for path in paths:
        path = Path(path)
        if not path.is_file():
            continue
        uri = 'file:'+urllib.parse.quote(str(path.resolve()))+'?mode=ro'
        con = sqlite3.connect(uri, uri=True)
        con.row_factory = sqlite3.Row
        try:
            con.execute('BEGIN')
            if not con.execute('SELECT 1 FROM productions WHERE id=?', (production_id,)).fetchone():
                continue
            payload = dict(version=1, production_id=production_id, revision=0,
                           tables=read_tables(con, production_id))
            return LocalProjectClient.from_project(payload)
        except sqlite3.Error:
            continue
        finally:
            con.close()
    raise ValueError('旧版项目只保存了索引，未找到原素材数据。请在原电脑打开并另存为新版完整项目，再拷贝.pgmproj。')


def recent_projects():
    from library_bridge import read_settings, data_dir
    rows = []
    seen_paths = set()
    def path_key(path):
        return os.path.abspath(os.path.expanduser(path)) if path else ''
    for item in read_settings().get('recent_project_files', []):
        if isinstance(item, dict) and all(isinstance(item.get(k), str) and item[k] for k in ('id', 'name', 'project_path')):
            key = path_key(item['project_path'])
            if item['id'] not in {r['id'] for r in rows} and key not in seen_paths:
                rows.append(copy.deepcopy(item))
                seen_paths.add(key)
    # Index files are small local checkpoints, not media scans. Keep legacy
    # projects discoverable for their one-time conversion on the original Mac.
    for path in sorted((data_dir()/'projects').glob('*.json')):
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
            identity = raw.get('production_id')
            if not identity or identity in {r['id'] for r in rows}:
                continue
            key = path_key(raw.get('project_path', ''))
            if key and key in seen_paths:
                continue
            rows.append(dict(id=identity, name=raw['name'], project_path=raw.get('project_path', ''), legacy=True))
            if key:
                seen_paths.add(key)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return rows


def remember_project(production, path):
    from library_bridge import read_settings, write_settings
    settings = read_settings()
    previous = settings.get('recent_project_files', [])
    rows = [dict(id=production['id'], name=production['name'], project_path=str(path))]
    rows.extend(row for row in previous if isinstance(row, dict) and row.get('id') != production['id'])
    settings['recent_project_files'] = rows[:20]
    settings['last_local_project'] = production['id']
    write_settings(settings)
