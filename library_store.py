"""Transactional project index: in-memory in the single-workstation app.

Disk mode is retained for legacy read/import compatibility and developer QA.
Source media remains at its original mounted filesystem path.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timezone
import uuid


class LibraryError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _id():
    return str(uuid.uuid4())


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise LibraryError("invalid", "数据必须是有效 JSON") from exc


def _invalidate_automatic_alignment(layout, mapping):
    """A changed map invalidates review, never saved coordinates or media."""
    normalized=json.loads(_json(mapping))
    digest=hashlib.sha256(json.dumps(normalized,ensure_ascii=False,sort_keys=True,
        separators=(',',':')).encode('utf-8')).hexdigest() if normalized else ''
    for item in (layout.get('items') or {}).values() if isinstance(layout,dict) else ():
        basis=item.get('alignment_basis') if isinstance(item,dict) else None
        basis=basis if isinstance(basis,dict) else {}
        if (isinstance(item,dict) and item.get('start_seconds') is not None
                and item.get('alignment_origin')=='automatic'
                and basis.get('mapping_fingerprint')!=digest):
            item.update(alignment_reviewed=False,alignment_invalid_reason='基准台本映射已改变，请核对实际位置。')


def _required(value, label):
    if not isinstance(value, str) or not value.strip():
        raise LibraryError("invalid", f"{label}不能为空")
    return value.strip()


def _local_db_path(path):
    """Reject known network mounts before SQLite creates any file."""
    raw = os.fspath(path)
    if raw.startswith(("smb:", "nfs:", "afp:", "//", "\\\\")):
        raise LibraryError("invalid", "数据库必须保存在服务电脑本地，不能直接放在共享存储工作区上")
    full = os.path.realpath(os.path.expanduser(raw))
    mounts = []
    network = {"nfs", "nfs4", "smbfs", "cifs", "afpfs", "fuse.sshfs", "sshfs", "webdav", "davfs"}
    if sys.platform == "darwin":
        try:
            result = subprocess.run(["/sbin/mount"], capture_output=True, text=True, timeout=3, check=False)
            for line in result.stdout.splitlines():
                if " on " in line and " (" in line:
                    mounted = line.split(" on ", 1)[1]
                    location, options = mounted.rsplit(" (", 1)
                    mounts.append((location, options.split(",", 1)[0].rstrip(")")))
        except (OSError, subprocess.SubprocessError):
            # A failed mount inventory must not cause an unverified NAS write.
            if full.startswith("/Volumes/"):
                raise LibraryError("invalid", "无法确认磁盘类型，请把数据库放在用户本地目录")
    elif os.path.exists("/proc/mounts"):
        try:
            for line in Path("/proc/mounts").read_text().splitlines():
                parts = line.split()
                if len(parts) >= 3:
                    mounts.append((parts[1].replace("\\040", " "), parts[2]))
        except OSError:
            pass
    matches = [(p, kind) for p, kind in mounts if full == p or full.startswith(p.rstrip("/") + "/")]
    if matches:
        kind = max(matches, key=lambda item: len(item[0]))[1].lower()
        if kind in network or kind.startswith(('avid', 'nexis', 'fuse.avid', 'fuse.nexis')):
            raise LibraryError("invalid", "数据库必须保存在服务电脑本地，不能直接放在NEXIS等共享存储工作区上")
    return full


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (version INTEGER NOT NULL);
INSERT INTO schema_info SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_info);
CREATE TABLE IF NOT EXISTS productions (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
 id TEXT PRIMARY KEY, production_id TEXT NOT NULL REFERENCES productions(id), name TEXT NOT NULL,
 config TEXT NOT NULL, segments TEXT NOT NULL, export_dir TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS sessions_production ON sessions(production_id);
CREATE TABLE IF NOT EXISTS reference_settings (
 production_id TEXT PRIMARY KEY REFERENCES productions(id),
 reference_session_id TEXT REFERENCES sessions(id),
 settings TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS scripts (
 id TEXT PRIMARY KEY, production_id TEXT NOT NULL REFERENCES productions(id), title TEXT NOT NULL,
 source_path TEXT NOT NULL, content_hash TEXT NOT NULL, version INTEGER NOT NULL, active INTEGER NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS scripts_production ON scripts(production_id, active);
CREATE TABLE IF NOT EXISTS script_lines (
 id TEXT PRIMARY KEY, script_id TEXT NOT NULL REFERENCES scripts(id), production_id TEXT NOT NULL REFERENCES productions(id),
 scene TEXT NOT NULL, speaker TEXT NOT NULL, text TEXT NOT NULL, line_order INTEGER NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS lines_script ON script_lines(script_id, line_order);
CREATE TABLE IF NOT EXISTS media (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), production_id TEXT NOT NULL REFERENCES productions(id),
 kind TEXT NOT NULL, path TEXT NOT NULL, identity_key TEXT NOT NULL UNIQUE, metadata TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS media_session_kind_path ON media(session_id,kind,path);
CREATE TABLE IF NOT EXISTS transcripts (
 id TEXT PRIMARY KEY, media_id TEXT NOT NULL REFERENCES media(id), session_id TEXT NOT NULL REFERENCES sessions(id),
 production_id TEXT NOT NULL REFERENCES productions(id), start_seconds REAL NOT NULL, end_seconds REAL NOT NULL,
 text TEXT NOT NULL, speaker TEXT NOT NULL, segment_key TEXT NOT NULL UNIQUE, data TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS transcripts_production ON transcripts(production_id, session_id);
CREATE INDEX IF NOT EXISTS transcripts_session_media ON transcripts(session_id,media_id);
CREATE TABLE IF NOT EXISTS candidates (
 id TEXT PRIMARY KEY, production_id TEXT NOT NULL REFERENCES productions(id),
 script_line_id TEXT NOT NULL REFERENCES script_lines(id), transcript_id TEXT NOT NULL REFERENCES transcripts(id),
 score REAL NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL, note TEXT NOT NULL, tags TEXT NOT NULL,
 updated_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL,
 UNIQUE(script_line_id, transcript_id));
CREATE INDEX IF NOT EXISTS candidates_production ON candidates(production_id, status);
CREATE INDEX IF NOT EXISTS candidates_transcript ON candidates(transcript_id);
CREATE INDEX IF NOT EXISTS lines_production ON script_lines(production_id,script_id,line_order);
CREATE TABLE IF NOT EXISTS artifacts (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), production_id TEXT NOT NULL REFERENCES productions(id),
 kind TEXT NOT NULL, path TEXT NOT NULL, manifest TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS audit_log (
 id TEXT PRIMARY KEY, production_id TEXT, actor TEXT NOT NULL, action TEXT NOT NULL,
 entity_id TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS audit_production ON audit_log(production_id, created_at);
"""


class Store:
    METHODS = frozenset({"create_production", "list_productions", "save_session", "list_sessions", "get_session",
                         "put_script", "list_script", "add_transcript", "list_transcripts", "replace_candidates",
                         "list_candidates", "update_candidate", "register_artifact", "list_artifacts", "audit", "backup",
                         "get_reference_settings", "update_reference_settings", "update_manual_timeline", "clear_pgm_matches",
                         "associate_pgm_script_range", "mark_pgm_reviewed", "hide_pgm_from_pool"})

    def __init__(self, path):
        self._keeper = None
        self._init_lock = threading.Lock()
        if os.fspath(path) == ":memory:":
            self.path = f"file:pgm_library_{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._uri = True
            self._keeper = self._connect()
        else:
            self.path = _local_db_path(path)
            self._uri = False
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(_SCHEMA)
            version = con.execute("SELECT version FROM schema_info").fetchone()[0]
            if version not in (1, 2):
                raise LibraryError("invalid", f"不支持的数据库版本：{version}")
            # Existing projects retain choices, revisions and foreign keys.
            # SQLite requires a table rebuild to remove the old NOT NULL rule.
            con.execute("BEGIN IMMEDIATE")
            try:
                columns = con.execute("PRAGMA table_info(reference_settings)").fetchall()
                if any(row["name"] == "reference_session_id" and row["notnull"] for row in columns):
                    con.execute("ALTER TABLE reference_settings RENAME TO reference_settings_v1")
                    con.execute("CREATE TABLE reference_settings (production_id TEXT PRIMARY KEY REFERENCES productions(id), "
                                "reference_session_id TEXT REFERENCES sessions(id), settings TEXT NOT NULL, "
                                "updated_at TEXT NOT NULL, revision INTEGER NOT NULL)")
                    con.execute("INSERT INTO reference_settings SELECT * FROM reference_settings_v1")
                    con.execute("DROP TABLE reference_settings_v1")
                con.execute("UPDATE schema_info SET version=2")
                con.commit()
            except Exception:
                con.rollback()
                raise

    def _connect(self):
        con = sqlite3.connect(self.path, timeout=15, isolation_level=None, uri=self._uri,
                              check_same_thread=not self._uri)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=15000")
        return con

    @contextlib.contextmanager
    def _connection(self, write=False):
        con = self._connect()
        try:
            if write:
                con.execute("BEGIN IMMEDIATE")
            yield con
            if write:
                con.commit()
        except Exception:
            if write:
                con.rollback()
            raise
        finally:
            con.close()

    def close(self):
        if self._keeper is not None:
            self._keeper.close()
            self._keeper = None

    def call(self, method, params=None, actor="local"):
        if not isinstance(method, str) or method not in self.METHODS:
            raise LibraryError("invalid", "不支持的操作")
        if params is None:
            params = {}
        if not isinstance(params, dict) or "actor" in params:
            raise LibraryError("invalid", "参数必须是对象，操作者应放在请求的 actor 字段")
        actor = _required(actor, "操作者")
        if len(actor) > 128:
            raise LibraryError("invalid", "操作者名称过长")
        try:
            return getattr(self, method)(actor=actor, **params)
        except (TypeError, ValueError, OverflowError) as exc:
            raise LibraryError("invalid", f"无效参数：{exc}") from exc
        except sqlite3.IntegrityError as exc:
            raise LibraryError("invalid", "数据关联或唯一性校验失败") from exc
        except sqlite3.OperationalError as exc:
            raise LibraryError("storage", "数据库暂时不可用，请稍后重试") from exc

    @staticmethod
    def _row(row):
        if row is None:
            return None
        value = dict(row)
        for field in ("config", "segments", "metadata", "manifest", "tags", "details", "data"):
            if field in value:
                value[field] = json.loads(value[field])
        return value

    @staticmethod
    def _get(con, table, identity):
        row = con.execute(f"SELECT * FROM {table} WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise LibraryError("not_found", "记录不存在")
        return row

    @staticmethod
    def _revision(row, expected):
        if isinstance(expected, bool) or not isinstance(expected, int):
            raise LibraryError("conflict", "更新必须提供当前 revision；请刷新后重试")
        if row["revision"] != expected:
            raise LibraryError("conflict", "记录已被其他人更新，请刷新后重新确认")

    @staticmethod
    def _audit(con, production_id, actor, action, identity, details):
        con.execute("INSERT INTO audit_log VALUES (?,?,?,?,?,?,?)",
                    (_id(), production_id, actor, action, identity, _json(details), _now()))

    def create_production(self, name, actor="local"):
        name = _required(name, "剧目名称")
        identity, now = _id(), _now()
        with self._connection(True) as con:
            con.execute("INSERT INTO productions VALUES (?,?,?,?,1)", (identity, name, now, now))
            self._audit(con, identity, actor, "create_production", identity, {"name": name})
            return self._row(self._get(con, "productions", identity))

    def list_productions(self, actor="local"):
        with self._connection() as con:
            return [self._row(row) for row in con.execute("SELECT * FROM productions ORDER BY created_at,id")]

    def save_session(self, production_id, name, config, segments=None, session_id=None,
                     expected_revision=None, export_dir="", actor="local"):
        name = _required(name, "拍摄场次名称")
        if not isinstance(config, dict) or not isinstance(segments if segments is not None else [], list):
            raise LibraryError("invalid", "config 必须是对象，segments 必须是列表")
        config_json, segments_json = _json(config), _json(segments if segments is not None else [])
        now = _now()
        with self._connection(True) as con:
            self._get(con, "productions", production_id)
            if config.get('library_record_type') == 'recording':
                # The same transaction that creates/updates a recording owns
                # the uniqueness check, including concurrent client imports.
                def pgms(value):
                    found = {value['pgm']} if value.get('pgm') else set()
                    found.update(row['path'] for row in value.get('library_media', [])
                                 if row.get('kind') == 'pgm' and row.get('path'))
                    found.update(value.get('pgm_timecodes', {}))
                    found.update(value.get('pgm_overrides', {}).values())
                    import posixpath
                    return {posixpath.normpath(path) for path in found if isinstance(path, str) and path}
                incoming = pgms(config)
                for previous in con.execute('SELECT id,name,config FROM sessions WHERE production_id=?', (production_id,)):
                    if previous['id'] == session_id:
                        continue
                    duplicate = incoming.intersection(pgms(json.loads(previous['config'])))
                    if duplicate:
                        raise LibraryError('duplicate_material', 'PGM已登记在拍摄场次“' + previous['name']
                            + '”，请刷新后向该场次追加素材：' + sorted(duplicate)[0])
            if session_id:
                row = self._get(con, "sessions", session_id)
                if row["production_id"] != production_id:
                    raise LibraryError("invalid", "场次不属于这个剧目")
                self._revision(row, expected_revision)
                # Technical/synchronization clients may not yet know this
                # file-level editor field. Its explicit review/remove RPCs
                # own it; a normal scene-config save must not erase decisions.
                previous_editor = json.loads(row['config']).get('library_pgm_editor')
                if isinstance(previous_editor, dict):
                    incoming_config = json.loads(config_json)
                    incoming_config['library_pgm_editor'] = previous_editor
                    config_json = _json(incoming_config)
                con.execute("UPDATE sessions SET name=?,config=?,segments=?,export_dir=?,updated_at=?,revision=revision+1 WHERE id=?",
                            (name, config_json, segments_json, str(export_dir), now, session_id))
                action = "update_session"
            else:
                session_id = _id()
                con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,1)",
                            (session_id, production_id, name, config_json, segments_json, str(export_dir), now, now))
                action = "create_session"
            self._audit(con, production_id, actor, action, session_id, {"name": name, "previous_revision": expected_revision})
            return self._row(self._get(con, "sessions", session_id))

    def list_sessions(self, production_id, actor="local"):
        with self._connection() as con:
            self._get(con, "productions", production_id)
            rows = con.execute("SELECT * FROM sessions WHERE production_id=? ORDER BY created_at,id", (production_id,))
            return [self._row(row) for row in rows]

    def get_session(self, session_id, actor="local"):
        with self._connection() as con:
            return self._row(self._get(con, "sessions", session_id))

    @staticmethod
    def _reference_row(row):
        if row is None:
            return dict(reference_session_id=None, selected_clip_pairs={}, baseline_export={},
                        baseline_script_map={}, manual_timeline={}, revision=0)
        result = dict(json.loads(row["settings"]), reference_session_id=row["reference_session_id"],
                      revision=row["revision"], updated_at=row["updated_at"])
        result.setdefault('baseline_script_map', {})
        result.setdefault('manual_timeline', {})
        return result

    def get_reference_settings(self, production_id, actor="local"):
        with self._connection() as con:
            self._get(con, "productions", production_id)
            return self._reference_row(con.execute(
                "SELECT * FROM reference_settings WHERE production_id=?", (production_id,)).fetchone())

    def update_reference_settings(self, production_id, reference_session_id, selected_clip_pairs=None,
                                  baseline_export=None, expected_revision=0, actor="local", baseline_script_map=None):
        """One shared baseline and explicit take choices, protected by CAS."""
        pairs = {} if selected_clip_pairs is None else selected_clip_pairs
        if (not isinstance(pairs, dict) or (baseline_export is not None and not isinstance(baseline_export, dict))
                or (baseline_script_map is not None and not isinstance(baseline_script_map, dict))):
            raise LibraryError("invalid", "基准备选设置必须是对象")
        if type(expected_revision) is not int or expected_revision < 0:
            raise LibraryError("conflict", "基准备选设置缺少有效版本，请刷新后重新选择")
        if any(not isinstance(k, str) or not k or
               (v is not None and (not isinstance(v, str) or not v)) for k,v in pairs.items()):
            raise LibraryError("invalid", "备选片段及基准片段 ID 无效")
        with self._connection(True) as con:
            self._get(con, "productions", production_id)
            if reference_session_id is not None:
                session = self._get(con, "sessions", reference_session_id)
                if session["production_id"] != production_id:
                    raise LibraryError("invalid", "基准场次不属于当前剧目")
            previous = con.execute("SELECT * FROM reference_settings WHERE production_id=?",
                                   (production_id,)).fetchone()
            revision = previous["revision"] if previous is not None else 0
            if expected_revision != revision:
                raise LibraryError("conflict", "基准备选设置已被其他人更新，请刷新后重新选择")
            previous_state = self._reference_row(previous)
            mapping = (previous_state.get('baseline_script_map', {})
                       if previous_state.get('reference_session_id') == reference_session_id else {})
            if baseline_script_map is not None:
                mapping = baseline_script_map
            if mapping and (not reference_session_id or mapping.get('reference_session_id') != reference_session_id):
                raise LibraryError('invalid', '基准台本映射不属于当前基准场次')
            indexes = {}
            def accepted_candidate(candidate, source, *, whole_file):
                key = (source['session_id'], source['media_id'])
                if key not in indexes:
                    owner = self._get(con, 'sessions', source['session_id'])
                    media = self._get(con, 'media', source['media_id'])
                    indexes[key] = self._pgm_editor_index(con, owner, media['path'])
                index = indexes[key]
                current = index.candidates.get(candidate['id'])
                if current is None:
                    return False
                if whole_file:
                    try:
                        first = index.selection_candidate(current)
                    except ValueError:
                        return False
                    return bool(first and first['id'] == candidate['id'])
                return index.association_ready(current)
            for selected_id, baseline_id in pairs.items():
                chosen = self._get(con, "candidates", selected_id)
                if chosen["production_id"] != production_id:
                    raise LibraryError("invalid", "备选片段须属于当前剧目且已通过核对")
                line = self._get(con, "script_lines", chosen["script_line_id"])
                if not self._get(con, "scripts", line["script_id"])["active"]:
                    raise LibraryError("invalid", "所选片段属于旧台本，请刷新后重新选择")
                chosen_source = self._get(con, "transcripts", chosen["transcript_id"])
                if not accepted_candidate(chosen, chosen_source, whole_file=True):
                    raise LibraryError("invalid", "备选PGM须已通过核对、具有唯一首句定位且未从轨道移出")
                if chosen_source["session_id"] == reference_session_id:
                    raise LibraryError("invalid", "备选须来自基准以外的拍摄场次")
                if baseline_id is None:
                    continue  # Resolve missing or repeated dialogue anchors at export.
                if reference_session_id is None:
                    raise LibraryError("invalid", "尚未确定基准，不能指定基准片段")
                anchor = self._get(con, "candidates", baseline_id)
                if anchor["production_id"] != production_id:
                    raise LibraryError("invalid", "基准片段须属于当前剧目且已通过核对")
                if chosen["script_line_id"] != anchor["script_line_id"]:
                    raise LibraryError("invalid", "备选与基准必须对应同一台本区间")
                anchor_source = self._get(con, "transcripts", anchor["transcript_id"])
                if anchor_source["session_id"] != reference_session_id:
                    raise LibraryError("invalid", "基准片段必须来自基准场次")
                if not accepted_candidate(anchor, anchor_source, whole_file=False):
                    raise LibraryError("invalid", "基准片段须具有唯一台词定位、已通过核对且未从轨道移出")
            manual = previous_state.get('manual_timeline', {})
            if manual and previous_state.get('reference_session_id') != reference_session_id:
                from manual_timeline import inventory, reconcile_layout
                sessions = [self._row(row) for row in con.execute(
                    'SELECT * FROM sessions WHERE production_id=? ORDER BY created_at,id', (production_id,))]
                manual = reconcile_layout(manual, inventory(dict(sessions=sessions)), reference_session_id)
            elif manual and mapping!=previous_state.get('baseline_script_map',{}):
                _invalidate_automatic_alignment(manual,mapping)
            settings = _json(dict(selected_clip_pairs=pairs, baseline_export=baseline_export or {},
                                  baseline_script_map=mapping, manual_timeline=manual))
            now = _now()
            con.execute("INSERT INTO reference_settings VALUES (?,?,?,?,?) "
                        "ON CONFLICT(production_id) DO UPDATE SET "
                        "reference_session_id=excluded.reference_session_id,settings=excluded.settings,"
                        "updated_at=excluded.updated_at,revision=excluded.revision",
                        (production_id, reference_session_id, settings, now, revision+1))
            self._audit(con, production_id, actor, "update_reference_settings", production_id,
                        dict(reference_session_id=reference_session_id, selected_count=len(pairs),
                             previous_revision=expected_revision))
            return self._reference_row(con.execute(
                "SELECT * FROM reference_settings WHERE production_id=?", (production_id,)).fetchone())

    def update_manual_timeline(self, production_id, manual_timeline, expected_revision=0, actor="local", baseline_script_map=None):
        """Persist manual target positions with the reference settings CAS."""
        if type(expected_revision) is not int or expected_revision < 0:
            raise LibraryError('conflict', '人工时间线缺少有效版本，请刷新后重试')
        if not isinstance(manual_timeline, dict):
            raise LibraryError('invalid', '人工时间线必须是对象')
        if baseline_script_map is not None and not isinstance(baseline_script_map,dict):
            raise LibraryError('invalid','基准台本映射必须是对象')
        from manual_timeline import inventory, reconcile_layout, validate_layout
        with self._connection(True) as con:
            self._get(con, 'productions', production_id)
            previous = con.execute('SELECT * FROM reference_settings WHERE production_id=?', (production_id,)).fetchone()
            state = self._reference_row(previous)
            if state['revision'] != expected_revision:
                raise LibraryError('conflict', '人工时间线已更新，请刷新后重新保存')
            baseline_id = state.get('reference_session_id')
            sessions = [self._row(row) for row in con.execute(
                'SELECT * FROM sessions WHERE production_id=? ORDER BY created_at,id', (production_id,))]
            rows = inventory(dict(sessions=sessions))
            script_count=con.execute('SELECT COUNT(*) FROM script_lines l JOIN scripts s ON s.id=l.script_id '
                'WHERE l.production_id=? AND s.active=1',(production_id,)).fetchone()[0]
            if script_count:
                for row in rows:row['script_line_count']=script_count
            try:
                layout = validate_layout(manual_timeline, rows, baseline_id)
                layout = reconcile_layout(layout, rows, baseline_id)
                layout = validate_layout(layout, rows, baseline_id)
            except ValueError as exc:
                raise LibraryError('invalid', str(exc)) from exc
            settings = dict(json.loads(previous['settings'])) if previous else dict(selected_clip_pairs={}, baseline_export={}, baseline_script_map={})
            if baseline_script_map is not None:
                _json(baseline_script_map)
                if baseline_script_map!=settings.get('baseline_script_map',{}):
                    _invalidate_automatic_alignment(layout,baseline_script_map)
                settings['baseline_script_map']=baseline_script_map
            settings['manual_timeline'] = layout
            now = _now()
            con.execute('INSERT INTO reference_settings VALUES (?,?,?,?,?) ON CONFLICT(production_id) DO UPDATE SET '
                        'reference_session_id=excluded.reference_session_id,settings=excluded.settings,'
                        'updated_at=excluded.updated_at,revision=excluded.revision',
                        (production_id, baseline_id, _json(settings), now, state['revision']+1))
            self._audit(con, production_id, actor, 'update_manual_timeline', production_id,
                        dict(item_count=len(layout['items']), placed_count=sum(
                            row.get('start_seconds') is not None and not row.get('hidden') for row in layout['items'].values())))
            return self._reference_row(con.execute('SELECT * FROM reference_settings WHERE production_id=?', (production_id,)).fetchone())

    def put_script(self, production_id, title, lines, source_path="", actor="local"):
        title = _required(title, "台本名称")
        if not isinstance(lines, list) or not lines:
            raise LibraryError("invalid", "台本必须包含台词列表")
        clean = []
        for index, line in enumerate(lines):
            if not isinstance(line, dict):
                raise LibraryError("invalid", "台词必须是对象")
            value = dict(line)
            value.update(scene=str(line.get("scene", "")), speaker=str(line.get("speaker", "")),
                         text=_required(line.get("text"), "台词"), order=int(line.get("order", index)))
            clean.append(value)
        digest = hashlib.sha256(_json(clean).encode()).hexdigest()
        now = _now()
        with self._connection(True) as con:
            self._get(con, "productions", production_id)
            previous = con.execute("SELECT * FROM scripts WHERE production_id=? AND title=? AND source_path=? AND active=1",
                                   (production_id, title, str(source_path))).fetchone()
            if previous is not None and previous["content_hash"] == digest:
                value = self._row(previous)
                value.update(line_count=len(clean), unchanged=True)
                return value
            version = (previous["version"] + 1) if previous else 1
            identity = _id()
            con.execute("UPDATE scripts SET active=0 WHERE production_id=? AND title=? AND source_path=?",
                        (production_id, title, str(source_path)))
            con.execute("INSERT INTO scripts VALUES (?,?,?,?,?,?,1,?,?,1)",
                        (identity, production_id, title, str(source_path), digest, version, now, now))
            for line in clean:
                con.execute("INSERT INTO script_lines VALUES (?,?,?,?,?,?,?,?)",
                            (_id(), identity, production_id, line["scene"], line["speaker"], line["text"], line["order"], _json(line)))
            self._audit(con, production_id, actor, "put_script", identity,
                        {"title": title, "version": version, "line_count": len(clean), "previous_script_id": previous["id"] if previous else None})
            value = self._row(self._get(con, "scripts", identity))
            value.update(line_count=len(clean), unchanged=False)
            return value

    def list_script(self, production_id, actor="local"):
        with self._connection() as con:
            self._get(con, "productions", production_id)
            rows = con.execute("""SELECT l.*,s.title,s.source_path,s.version,s.active FROM script_lines l
                                  JOIN scripts s ON s.id=l.script_id WHERE l.production_id=? AND s.active=1
                                  ORDER BY s.created_at,s.id,l.line_order,l.id""", (production_id,))
            values = []
            for row in rows:
                value = self._row(row)
                value["order"] = value["line_order"]
                values.append(value)
            return values

    def add_transcript(self, session_id, media_path, segments, kind="pgm", metadata=None, actor="local"):
        media_path = _required(media_path, "媒体路径")
        kind = _required(kind, "媒体类型")
        if not isinstance(segments, list) or not isinstance(metadata if metadata is not None else {}, dict):
            raise LibraryError("invalid", "转录段必须是列表，metadata 必须是对象")
        metadata = dict(metadata or {})
        # A full path scoped to an immutable session identity, never a basename/camera label.
        media_key = hashlib.sha256(_json([session_id, kind, media_path]).encode()).hexdigest()
        clean = []
        for segment in segments:
            if not isinstance(segment, dict):
                raise LibraryError("invalid", "转录段必须是对象")
            value = dict(segment)
            start, end = float(value.get("start_seconds", -1)), float(value.get("end_seconds", -1))
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
                raise LibraryError("invalid", "转录时间范围必须满足 0 ≤ 开始 < 结束")
            value.update(start_seconds=start, end_seconds=end, text=_required(value.get("text"), "识别台词"),
                         speaker=str(value.get("speaker", "")))
            clean.append(value)
        now, added, ids = _now(), 0, []
        with self._connection(True) as con:
            session = self._get(con, "sessions", session_id)
            production_id = session["production_id"]
            media = con.execute("SELECT * FROM media WHERE identity_key=?", (media_key,)).fetchone()
            if media is None:
                media_id = _id()
                con.execute("INSERT INTO media VALUES (?,?,?,?,?,?,?,?,?,1)",
                            (media_id, session_id, production_id, kind, media_path, media_key, _json(metadata), now, now))
            else:
                media_id = media["id"]
                merged = json.loads(media["metadata"])
                old_fingerprint = merged.get("content_fingerprint", merged.get("sha256"))
                new_fingerprint = metadata.get("content_fingerprint", metadata.get("sha256"))
                if old_fingerprint and new_fingerprint and old_fingerprint != new_fingerprint:
                    raise LibraryError("conflict", "同一路径的媒体内容指纹发生变化，请另建场次或使用新的素材路径")
                merged.update(metadata)
                con.execute("UPDATE media SET metadata=?,updated_at=?,revision=revision+1 WHERE id=?", (_json(merged), now, media_id))
            for value in clean:
                key = hashlib.sha256(_json([media_id, value["start_seconds"], value["end_seconds"], value["text"]]).encode()).hexdigest()
                found = con.execute("SELECT id FROM transcripts WHERE segment_key=?", (key,)).fetchone()
                if found:
                    identity = found["id"]
                else:
                    identity = _id()
                    con.execute("INSERT INTO transcripts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)",
                                (identity, media_id, session_id, production_id, value["start_seconds"], value["end_seconds"],
                                 value["text"], value["speaker"], key, _json(value), now, now))
                    added += 1
                ids.append(identity)
            self._audit(con, production_id, actor, "add_transcript", media_id,
                        {"session_id": session_id, "media_path": media_path, "added": added, "existing": len(ids)-added})
            values = [self._transcript(con, identity) for identity in ids]
            return {"id": media_id, "media_id": media_id, "session_id": session_id, "added": added,
                    "existing": len(ids)-added, "segments": values, "transcript_ids": ids}

    def _transcript(self, con, identity):
        row = con.execute("""SELECT t.*,m.path AS media_path,m.kind,m.metadata,s.name AS session_name
                             FROM transcripts t JOIN media m ON m.id=t.media_id JOIN sessions s ON s.id=t.session_id WHERE t.id=?""",
                          (identity,)).fetchone()
        if row is None:
            raise LibraryError("not_found", "转录段不存在")
        return self._row(row)

    def list_transcripts(self, production_id, session_id=None, actor="local"):
        with self._connection() as con:
            self._get(con, "productions", production_id)
            args = [production_id]
            clause = "t.production_id=?"
            if session_id is not None:
                session = self._get(con, "sessions", session_id)
                if session["production_id"] != production_id:
                    raise LibraryError("invalid", "场次不属于这个剧目")
                clause += " AND t.session_id=?"
                args.append(session_id)
            rows = con.execute(f"""SELECT t.*,m.path AS media_path,m.kind,m.metadata,s.name AS session_name FROM transcripts t
                                   JOIN media m ON m.id=t.media_id JOIN sessions s ON s.id=t.session_id
                                   WHERE {clause} ORDER BY s.created_at,m.created_at,t.start_seconds,t.id""", args)
            return [self._row(row) for row in rows]

    def replace_candidates(self, production_id, candidates, actor="local", refreshed_transcript_ids=None,
                           expected_candidate_revisions=None, expected_reference_revision=None,
                           expected_session_revisions=None):
        if not isinstance(candidates, list):
            raise LibraryError("invalid", "匹配候选必须是列表")
        refreshed = None
        if refreshed_transcript_ids is not None:
            if not isinstance(refreshed_transcript_ids, list) or any(not isinstance(v, str) or not v for v in refreshed_transcript_ids):
                raise LibraryError('invalid', '重新匹配范围必须是转录 ID 列表')
            refreshed = set(refreshed_transcript_ids)
            if (not isinstance(expected_candidate_revisions, dict) or any(not isinstance(k, str) or
                    type(v) is not int or v < 1 for k, v in expected_candidate_revisions.items()) or
                    not isinstance(expected_session_revisions, dict)):
                raise LibraryError('conflict', '重新匹配缺少完整版本信息，请刷新后重试')
            if type(expected_reference_revision) is not int or expected_reference_revision < 0:
                raise LibraryError('conflict', '重新匹配缺少基准备选版本，请刷新后重试')
        added = updated = preserved = superseded = 0
        now = _now()
        automatic_tags = frozenset(('auto_dialogue', 'automatic_dialogue', 'auto_match_superseded'))
        automatic_notes = frozenset(('',
            '台词匹配度高于60%自动通过；同场同区间机位一并归类。',
            '高台词吻合自动通过：原始声音完整覆盖、无近似剧情竞争；同场同区间机位一并归类。'))

        def span_tag(tag):
            return isinstance(tag, str) and tag.startswith('auto_script_span:')

        def machine_owned(row):
            return (all(tag in automatic_tags or span_tag(tag) for tag in json.loads(row['tags'])) and
                    row['note'] in automatic_notes)

        def automatic_confirmation(row):
            tags = set(json.loads(row['tags']))
            # A human-added tag or note owns the decision even if an old
            # automatic tag was retained by that explicit review.
            return (row['status'] == 'confirmed' and
                    bool(tags.intersection(('auto_dialogue', 'automatic_dialogue'))) and
                    machine_owned(row))

        with self._connection(True) as con:
            self._get(con, "productions", production_id)
            old_rows, source_sessions = [], set()
            if refreshed is not None:
                sources = {row['id']: row for row in con.execute('SELECT t.*,m.path AS media_path FROM transcripts t '
                    'JOIN media m ON m.id=t.media_id WHERE t.production_id=?', (production_id,)) if row['id'] in refreshed}
                if set(sources) != refreshed:
                    raise LibraryError('invalid', '重新匹配的转录必须属于当前剧目')
                for source in sources.values():
                    if json.loads(source['data']).get('source_kind') == 'manual_visual':
                        raise LibraryError('invalid', '人工画面归类不能作为自动台词重匹配范围')
                    source_sessions.add(source['session_id'])
                if set(expected_session_revisions) != source_sessions:
                    raise LibraryError('conflict', '重新匹配缺少场次版本，请刷新后重试')
                for identity in source_sessions:
                    session = self._get(con, 'sessions', identity)
                    self._revision(session, expected_session_revisions[identity])
                    config = json.loads(session['config'])
                    for source in sources.values():
                        if source['session_id'] != identity:
                            continue
                        state = (config.get('library_asr') or {}).get(source['media_path']) or {}
                        if isinstance(state.get('active_transcript_ids'), list) and source['id'] not in state['active_transcript_ids']:
                            raise LibraryError('conflict', '台词识别结果已更新，请刷新后重新匹配')
                old_rows = [row for row in con.execute('SELECT * FROM candidates WHERE production_id=?', (production_id,))
                            if row['transcript_id'] in refreshed]
                if {row['id']: row['revision'] for row in old_rows} != expected_candidate_revisions:
                    raise LibraryError('conflict', '台词匹配已被其他人核对或更新，请刷新后重试')
                ref_row = con.execute('SELECT * FROM reference_settings WHERE production_id=?', (production_id,)).fetchone()
                ref_state = self._reference_row(ref_row)
                if ref_state['revision'] != expected_reference_revision:
                    raise LibraryError('conflict', '基准备选设置已更新，请刷新后重新匹配')
            incoming, span_families = set(), {}
            for item in candidates:
                if not isinstance(item, dict):
                    raise LibraryError("invalid", "匹配候选必须是对象")
                line = self._get(con, "script_lines", item.get("script_line_id"))
                transcript = self._get(con, "transcripts", item.get("transcript_id"))
                if line["production_id"] != production_id or transcript["production_id"] != production_id:
                    raise LibraryError("invalid", "台词和转录必须属于同一剧目")
                if refreshed is not None and transcript['id'] not in refreshed:
                    raise LibraryError('invalid', '候选超出本次重新匹配的转录范围')
                incoming.add((line['id'], transcript['id']))
                score = float(item.get("score", 0))
                if not math.isfinite(score) or not 0 <= score <= 1:
                    raise LibraryError("invalid", "匹配分数必须在 0 到 1 之间")
                reason = str(item.get("reason", ""))
                tags = item.get('tags', [])
                if not isinstance(tags, list) or any(not span_tag(tag) for tag in tags):
                    raise LibraryError('invalid', '自动匹配只接受机器生成的连续台本范围标签')
                if tags:
                    from dialogue_spans import encode_span_tags, parse_span_tags
                    span = parse_span_tags(dict(tags=tags))
                    if line['script_id'] not in span_families:
                        script = self._get(con, 'scripts', line['script_id'])
                        rows = con.execute(
                            'SELECT id,scene FROM script_lines WHERE production_id=? AND script_id=? ORDER BY line_order,id',
                            (production_id, line['script_id'])).fetchall()
                        family = [entry['id'] for entry in rows]
                        positions = {identity: index for index, identity in enumerate(family)}
                        scenes = {entry['id']: entry['scene'] for entry in rows}
                        span_families[line['script_id']] = script['active'], family, positions, scenes
                    active, family, positions, scenes = span_families[line['script_id']]
                    first = positions[line['id']]
                    if (not span or not active or span[0] != line['id'] or
                            span != family[first:first+len(span)] or
                            any(scenes[identity] != scenes[line['id']] for identity in span)):
                        raise LibraryError('invalid', '连续台本范围须从定位台词开始，包含当前同一台本同一幕的 2 至 6 条连续台词')
                    tags = encode_span_tags(span)
                row = con.execute("SELECT * FROM candidates WHERE script_line_id=? AND transcript_id=?", (line["id"], transcript["id"])).fetchone()
                if row is None:
                    con.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,'candidate','',?,?,?,?,1)",
                                (_id(), production_id, line["id"], transcript["id"], score, reason, _json(tags), actor, now, now))
                    added += 1
                elif (refreshed is not None and automatic_confirmation(row) and row['score'] == score
                        and row['reason'] == reason
                        and [tag for tag in json.loads(row['tags']) if span_tag(tag)] == tags):
                    # Re-running unchanged matches must retain the automatic
                    # decision, its revision and the PGM's human review hash.
                    preserved += 1
                elif refreshed is not None and (automatic_confirmation(row) or
                        row['status'] == 'rejected' and 'auto_match_superseded' in json.loads(row['tags']) and machine_owned(row)):
                    note = '' if row['note'] in automatic_notes else row['note']
                    con.execute("UPDATE candidates SET score=?,reason=?,status='candidate',note=?,tags=?,updated_at=?,updated_by=?,revision=revision+1 WHERE id=?",
                                (score, reason, note, _json(tags), now, actor, row['id']))
                    updated += 1
                elif row["status"] != "candidate":
                    preserved += 1
                else:
                    tags = [tag for tag in json.loads(row['tags']) if not span_tag(tag)] + tags
                    if row["score"] != score or row["reason"] != reason or json.loads(row['tags']) != tags:
                        con.execute("UPDATE candidates SET score=?,reason=?,tags=?,updated_at=?,updated_by=?,revision=revision+1 WHERE id=?",
                                    (score, reason, _json(tags), now, actor, row["id"]))
                        updated += 1
            stale_ids, stale_sessions = set(), set()
            for row in old_rows:
                if (row['script_line_id'], row['transcript_id']) in incoming:
                    continue
                tags = json.loads(row['tags'])
                untouched_suggestion = (row['status'] == 'candidate' and not row['note'] and
                    machine_owned(row))
                if not (automatic_confirmation(row) or untouched_suggestion):
                    continue
                con.execute("UPDATE candidates SET status='rejected',tags=?,updated_at=?,updated_by=?,revision=revision+1 WHERE id=?",
                    (_json(list(dict.fromkeys(tags + ['auto_match_superseded']))), now, actor, row['id']))
                stale_ids.add(row['id'])
                stale_sessions.add(sources[row['transcript_id']]['session_id'])
                superseded += 1
            if stale_ids:
                pairs = {identity: None if anchor in stale_ids else anchor for identity, anchor in
                         (ref_state.get('selected_clip_pairs') or {}).items() if identity not in stale_ids}
                baseline = dict(ref_state.get('baseline_export') or {})
                if ref_state.get('reference_session_id') in stale_sessions and baseline:
                    baseline.update(valid=False, invalid_reason='台词重新匹配撤销了旧自动对应关系，请重新导出基准时间线。')
                con.execute('INSERT INTO reference_settings VALUES (?,?,?,?,?) ON CONFLICT(production_id) DO UPDATE SET '
                    'settings=excluded.settings,updated_at=excluded.updated_at,revision=excluded.revision',
                    (production_id, ref_state.get('reference_session_id'), _json(dict(selected_clip_pairs=pairs,
                    baseline_export=baseline, baseline_script_map=ref_state.get('baseline_script_map', {}), manual_timeline=ref_state.get('manual_timeline', {}))),
                     now, ref_state['revision']+1))
            # Explicit refresh retires only superseded machine decisions in its
            # current source scope. Human choices and historical ASR stay intact.
            self._audit(con, production_id, actor, "replace_candidates", production_id,
                        {"added": added, "updated": updated, "preserved_decisions": preserved,
                         "superseded_automatic": superseded, 'refreshed_transcript_ids': sorted(refreshed or [])})
        return {"added": added, "updated": updated, "preserved_decisions": preserved,
                "superseded_automatic": superseded}

    _CANDIDATE_JOIN = """SELECT c.*,l.text AS script_text,l.scene AS script_scene,l.speaker AS script_speaker,l.line_order,
        d.title AS script_title,d.version AS script_version,d.active AS script_active,
        t.text AS transcript_text,t.speaker AS transcript_speaker,t.start_seconds,t.end_seconds,t.media_id,
        t.session_id,s.name AS session_name,m.path AS media_path,m.kind AS media_kind,m.metadata,t.data AS data
        FROM candidates c JOIN script_lines l ON l.id=c.script_line_id JOIN scripts d ON d.id=l.script_id
        JOIN transcripts t ON t.id=c.transcript_id JOIN sessions s ON s.id=t.session_id JOIN media m ON m.id=t.media_id"""

    def list_candidates(self, production_id, script_line_id=None, status=None, actor="local"):
        with self._connection() as con:
            self._get(con, "productions", production_id)
            clauses, params = ["c.production_id=?"], [production_id]
            if script_line_id is not None:
                clauses.append("c.script_line_id=?")
                params.append(script_line_id)
            if status is not None:
                if status not in ("candidate", "confirmed", "rejected"):
                    raise LibraryError("invalid", "无效的候选状态")
                clauses.append("c.status=?")
                params.append(status)
            rows = con.execute(self._CANDIDATE_JOIN + " WHERE " + " AND ".join(clauses) + " ORDER BY l.line_order,c.score DESC,c.id", params)
            return [self._row(row) for row in rows]

    def update_candidate(self, candidate_id, status, note="", tags=None, expected_revision=None, actor="local"):
        if status not in ("candidate", "confirmed", "rejected") or not isinstance(tags if tags is not None else [], list):
            raise LibraryError("invalid", "候选状态或标签无效")
        tags = list(tags or [])
        if not all(isinstance(tag, str) for tag in tags):
            raise LibraryError("invalid", "标签必须是文本列表")
        # A later explicit human operation owns the decision. It must never be
        # revived by the automatic-refresh recovery rule for retired guesses.
        tags = [tag for tag in tags if tag != 'auto_match_superseded']
        with self._connection(True) as con:
            row = self._get(con, "candidates", candidate_id)
            self._revision(row, expected_revision)
            con.execute("UPDATE candidates SET status=?,note=?,tags=?,updated_by=?,updated_at=?,revision=revision+1 WHERE id=?",
                        (status, str(note), _json(tags), actor, _now(), candidate_id))
            self._audit(con, row["production_id"], actor, "update_candidate", candidate_id,
                        {"before_status": row["status"], "status": status, "note": str(note), "tags": tags, "previous_revision": row["revision"]})
            return self._row(con.execute(self._CANDIDATE_JOIN + " WHERE c.id=?", (candidate_id,)).fetchone())

    def _pgm_editor_index(self, con, session, media_path):
        """Read one file's current evidence inside the caller's transaction."""
        from pgm_takes import PgmTakeIndex
        production_id, session_id = session['production_id'], session['id']
        sources = [self._row(row) for row in con.execute(
            'SELECT t.*,m.path AS media_path,m.kind,m.metadata FROM transcripts t '
            'JOIN media m ON m.id=t.media_id WHERE t.session_id=? AND m.path=? AND m.kind=\'pgm\'',
            (session_id, media_path))]
        candidates = [self._row(row) for row in con.execute(self._CANDIDATE_JOIN +
            ' WHERE c.production_id=? AND t.session_id=? AND m.path=? AND m.kind=\'pgm\'',
            (production_id, session_id, media_path))]
        scripts = [self._row(row) for row in con.execute(
            'SELECT l.*,s.active FROM script_lines l JOIN scripts s ON s.id=l.script_id '
            'WHERE l.production_id=? AND s.active=1 ORDER BY s.created_at,s.id,l.line_order,l.id',
            (production_id,))]
        return PgmTakeIndex(dict(sessions=[self._row(session)], scripts=scripts,
                                transcripts=sources, candidates=candidates))

    def _pgm_editor_summary(self, con, session, media_path):
        """Compute one file's effective coverage inside its write transaction."""
        index = self._pgm_editor_index(con, session, media_path)
        session_id = session['id']
        summary = index.file_summary(dict(session_id=session_id, media_path=media_path))
        source = index.source(summary['first_candidate']) if summary.get('first_candidate') else None
        return summary, source

    def _save_pgm_editor_state(self, con, session, media_path, *, pool, reviewed, actor, now):
        from pgm_editor_state import coverage_fingerprint
        summary, source = self._pgm_editor_summary(con, session, media_path)
        fingerprint = coverage_fingerprint(summary, source)
        if reviewed and not fingerprint:
            raise LibraryError('invalid', '该PGM尚无有效台本覆盖，请先人工定位后再核实')
        config = json.loads(session['config'])
        table = config.get('library_pgm_editor') or {}
        table = dict(table) if isinstance(table, dict) else {}
        state = dict(pool=pool, reviewed=bool(reviewed), coverage_fingerprint=fingerprint,
                     updated_by=actor, updated_at=now)
        table[media_path] = state
        config['library_pgm_editor'] = table
        con.execute('UPDATE sessions SET config=?,updated_at=?,revision=revision+1 WHERE id=?',
                    (_json(config), now, session['id']))
        return state

    def mark_pgm_reviewed(self, production_id, session_id, media_path,
                          expected_session_revision=None, expected_candidate_revisions=None,
                          expected_reference_revision=None, reviewed=True, actor='local',
                          editor_policy='pgm_editor_v1'):
        """Explicit file-level review, independent of automatic confirmations."""
        from pgm_editor_state import EDITOR_POLICY
        if editor_policy != EDITOR_POLICY or type(reviewed) is not bool:
            raise LibraryError('invalid', 'PGM核实状态或编辑策略无效，请更新素材库服务和客户端')
        media_path = _required(media_path, 'PGM 完整路径')
        expected = expected_candidate_revisions
        if (not isinstance(expected, dict) or any(not isinstance(identity, str) or not identity
                or type(revision) is not int or revision < 1 for identity, revision in expected.items())
                or type(expected_reference_revision) is not int or expected_reference_revision < 0):
            raise LibraryError('conflict', 'PGM核实缺少完整版本信息，请刷新后重试')
        with self._connection(True) as con:
            self._get(con, 'productions', production_id)
            session = self._get(con, 'sessions', session_id)
            if session['production_id'] != production_id:
                raise LibraryError('invalid', 'PGM拍摄场次不属于当前剧目')
            self._revision(session, expected_session_revision)
            from library_pending import _registered_pgms
            config = json.loads(session['config'])
            if media_path not in _registered_pgms(config):
                raise LibraryError('not_found', '未找到该场次和完整路径的PGM')
            actual = {row['id']: row['revision'] for row in con.execute(
                'SELECT c.id,c.revision FROM candidates c JOIN transcripts t ON t.id=c.transcript_id '
                'JOIN media m ON m.id=t.media_id WHERE c.production_id=? AND t.session_id=? '
                'AND m.path=? AND m.kind=\'pgm\'', (production_id, session_id, media_path))}
            if actual != expected:
                raise LibraryError('conflict', '该PGM的台本匹配已被其他人更新，请刷新后重新核实')
            reference = self._reference_row(con.execute(
                'SELECT * FROM reference_settings WHERE production_id=?', (production_id,)).fetchone())
            if reference['revision'] != expected_reference_revision:
                raise LibraryError('conflict', '基准备选设置已更新，请刷新后重新核实')
            previous = (config.get('library_pgm_editor') or {}).get(media_path, {})
            if isinstance(previous, dict) and previous.get('pool') == 'removed':
                raise LibraryError('invalid', '该PGM已从轨道移出，请先输入台本区间重新添加至轨道')
            now = _now()
            state = self._save_pgm_editor_state(con, session, media_path,
                pool='timeline', reviewed=reviewed, actor=actor, now=now)
            self._audit(con, production_id, actor, 'mark_pgm_reviewed', session_id,
                dict(media_path=media_path, reviewed=reviewed,
                     coverage_fingerprint=state['coverage_fingerprint'],
                     previous_session_revision=session['revision'],
                     previous_candidate_revisions=actual,
                     previous_reference_revision=reference['revision']))
            return dict(session_id=session_id, media_path=media_path, editor_state=state,
                        session=self._row(self._get(con, 'sessions', session_id)))

    def hide_pgm_from_pool(self, production_id, session_id, media_path,
                           expected_session_revision=None, expected_candidate_revisions=None,
                           expected_reference_revision=None, actor='local'):
        """Hide a media-pool file without deleting any source or related record."""
        from library_pending import _registered_pgms
        from pgm_editor_state import read_editor_state
        media_path = _required(media_path, 'PGM 完整路径')
        expected = expected_candidate_revisions
        if (not isinstance(expected, dict) or any(not isinstance(identity, str) or not identity
                or type(revision) is not int or revision < 1 for identity, revision in expected.items())
                or type(expected_reference_revision) is not int or expected_reference_revision < 0):
            raise LibraryError('conflict', '删除缺少完整版本信息，请刷新后重试')
        with self._connection(True) as con:
            self._get(con, 'productions', production_id)
            session = self._get(con, 'sessions', session_id)
            if session['production_id'] != production_id:
                raise LibraryError('invalid', 'PGM拍摄场次不属于当前剧目')
            self._revision(session, expected_session_revision)
            config = json.loads(session['config'])
            if media_path not in _registered_pgms(config):
                raise LibraryError('not_found', '未找到该场次和完整路径的PGM')
            actual = {row['id']: row['revision'] for row in con.execute(
                'SELECT c.id,c.revision FROM candidates c JOIN transcripts t ON t.id=c.transcript_id '
                'JOIN media m ON m.id=t.media_id WHERE c.production_id=? AND t.session_id=? '
                'AND m.path=? AND m.kind=\'pgm\'', (production_id, session_id, media_path))}
            if actual != expected:
                raise LibraryError('conflict', '该PGM的匹配已更新，请刷新后重试')
            reference = self._reference_row(con.execute(
                'SELECT * FROM reference_settings WHERE production_id=?', (production_id,)).fetchone())
            if reference['revision'] != expected_reference_revision:
                raise LibraryError('conflict', '基准备选设置已更新，请刷新后重试')
            summary, source = self._pgm_editor_summary(con, session, media_path)
            state = read_editor_state(config, media_path, summary=summary, source=source)
            if state['pool'] == 'timeline':
                raise LibraryError('invalid', '该PGM已在轨道上，请先从轨道移出后再从媒体池删除')
            table = dict(config.get('library_pgm_editor') or {})
            previous = table.get(media_path)
            stored = dict(previous) if isinstance(previous, dict) else {}
            now = _now()
            stored.update(hidden_in_pool=True, updated_by=actor, updated_at=now)
            table[media_path] = stored
            config['library_pgm_editor'] = table
            con.execute('UPDATE sessions SET config=?,updated_at=?,revision=revision+1 WHERE id=?',
                        (_json(config), now, session_id))
            self._audit(con, production_id, actor, 'hide_pgm_from_pool', session_id,
                        dict(media_path=media_path, pool=state['pool'], previous_session_revision=session['revision']))
            return dict(session_id=session_id, media_path=media_path,
                        session=self._row(self._get(con, 'sessions', session_id)))

    def clear_pgm_matches(self, production_id, session_id, media_path,
                          expected_session_revision=None, expected_candidate_revisions=None,
                          expected_reference_revision=None, actor="local", editor_policy='pgm_editor_v1'):
        """Undo a whole file's script associations atomically, retaining its media.

        A complete candidate revision snapshot includes historical ASR and visual
        annotations. This catches new associations as well as edits made since
        the browser's last refresh. All decisions and take selections change in
        one transaction; no filesystem operation or transcript deletion occurs.
        """
        from pgm_editor_state import EDITOR_POLICY
        if editor_policy != EDITOR_POLICY:
            raise LibraryError('invalid', '不支持的 PGM 编辑状态策略，请更新素材库服务和客户端')
        media_path = _required(media_path, "PGM 完整路径")
        expected = expected_candidate_revisions
        if (not isinstance(expected, dict) or any(not isinstance(identity, str) or not identity
                or type(revision) is not int or revision < 1 for identity, revision in expected.items())):
            raise LibraryError("conflict", "PGM 匹配缺少完整版本信息，请刷新后重试")
        if type(expected_reference_revision) is not int or expected_reference_revision < 0:
            raise LibraryError("conflict", "基准备选设置缺少有效版本，请刷新后重试")
        with self._connection(True) as con:
            self._get(con, "productions", production_id)
            session = self._get(con, "sessions", session_id)
            if session["production_id"] != production_id:
                raise LibraryError("invalid", "PGM 拍摄场次不属于当前剧目")
            self._revision(session, expected_session_revision)
            media = con.execute("SELECT id FROM media WHERE production_id=? AND session_id=? AND path=? AND kind='pgm'",
                                (production_id, session_id, media_path)).fetchall()
            if not media:
                raise LibraryError("not_found", "未找到该场次和完整路径的 PGM，不能按同名文件移除匹配")
            rows = con.execute("""SELECT c.* FROM candidates c JOIN transcripts t ON t.id=c.transcript_id
                                  JOIN media m ON m.id=t.media_id WHERE c.production_id=?
                                  AND t.session_id=? AND m.path=? AND m.kind='pgm'""",
                               (production_id, session_id, media_path)).fetchall()
            actual = {row["id"]: row["revision"] for row in rows}
            if actual != expected:
                raise LibraryError("conflict", "该 PGM 的台本匹配已被其他人更新，请刷新后重新移除")
            reference_row = con.execute("SELECT * FROM reference_settings WHERE production_id=?", (production_id,)).fetchone()
            state = self._reference_row(reference_row)
            if state['revision'] != expected_reference_revision:
                raise LibraryError("conflict", "基准备选设置已被其他人更新，请刷新后重新移除")
            now, changed = _now(), 0
            baseline_visibility_only = state.get('reference_session_id') == session_id
            for row in rows:
                # The complete reference edit remains V1. Removing its PGM
                # from the alternatives browser changes visibility only; its
                # baseline speech map must retain the original associations.
                if baseline_visibility_only or row['status'] == 'rejected':
                    continue
                tags = list(dict.fromkeys(json.loads(row['tags']) + ['pgm_match_removed']))
                con.execute("UPDATE candidates SET status='rejected',tags=?,updated_by=?,updated_at=?,revision=revision+1 WHERE id=?",
                            (_json(tags), actor, now, row['id']))
                changed += 1
            affected = set() if baseline_visibility_only else set(actual)
            pairs = dict(state.get('selected_clip_pairs') or {})
            removed_choices = sum(identity in affected for identity in pairs)
            pairs = {identity: None if anchor in affected else anchor for identity, anchor in pairs.items()
                     if identity not in affected}
            reset_anchors = sum(anchor in affected for identity, anchor in (state.get('selected_clip_pairs') or {}).items()
                                if identity not in affected)
            baseline = dict(state.get('baseline_export') or {})
            if state.get('reference_session_id') == session_id and changed and baseline:
                baseline.update(valid=False, invalid_reason='基准场次的 PGM 台本匹配已移除，请重新核对并导出。')
            settings = _json(dict(selected_clip_pairs=pairs, baseline_export=baseline,
                                  baseline_script_map=state.get('baseline_script_map', {}), manual_timeline=state.get('manual_timeline', {})))
            con.execute("INSERT INTO reference_settings VALUES (?,?,?,?,?) "
                        "ON CONFLICT(production_id) DO UPDATE SET settings=excluded.settings,"
                        "updated_at=excluded.updated_at,revision=excluded.revision",
                        (production_id, state.get('reference_session_id'), settings, now, state['revision']+1))
            editor_state = self._save_pgm_editor_state(con, session, media_path,
                pool='removed', reviewed=False, actor=actor, now=now)
            self._audit(con, production_id, actor, 'clear_pgm_matches', session_id,
                        dict(session_id=session_id, media_path=media_path, candidate_ids=sorted(affected),
                             cleared_count=changed, removed_choices=removed_choices, reset_anchors=reset_anchors,
                             previous_session_revision=session['revision'], previous_candidate_revisions=actual,
                             previous_reference_revision=state['revision']))
            return dict(session_id=session_id, media_path=media_path, cleared_count=changed,
                        removed_choices=removed_choices, reset_anchors=reset_anchors,
                        editor_state=editor_state, session=self._row(self._get(con, 'sessions', session_id)),
                        reference_state=self._reference_row(con.execute(
                            "SELECT * FROM reference_settings WHERE production_id=?", (production_id,)).fetchone()))

    def associate_pgm_script_range(self, production_id, session_id, media_path, start_seconds, end_seconds,
                                   script_line_ids, expected_session_revision=None,
                                   expected_candidate_revisions=None, expected_reference_revision=None,
                                   note='', actor='local', selection_policy='preserve_whole_pgm',
                                   editor_policy='pgm_editor_v1'):
        """Replace a file's explicit manual anchor/range in one CAS transaction."""
        # API 5 clients always send this policy. Older services reject the new
        # keyword during argument binding, before their old selection-clearing
        # transaction can run. Also fail closed for unsupported future policies.
        if selection_policy != 'preserve_whole_pgm':
            raise LibraryError('invalid', '不支持的 PGM 选择保留策略，请更新素材库服务和客户端')
        from pgm_editor_state import EDITOR_POLICY
        if editor_policy != EDITOR_POLICY:
            raise LibraryError('invalid', '不支持的 PGM 编辑状态策略，请更新素材库服务和客户端')
        media_path = _required(media_path, 'PGM 完整路径')
        if (not isinstance(script_line_ids, list) or not script_line_ids or
                any(not isinstance(identity, str) or not identity for identity in script_line_ids) or
                len(set(script_line_ids)) != len(script_line_ids)):
            raise LibraryError('invalid', '人工台本范围须是有效且不重复的行号列表')
        begin, end = float(start_seconds), float(end_seconds)
        if not math.isfinite(begin) or not math.isfinite(end) or not 0 <= begin < end:
            raise LibraryError('invalid', '人工定位的实际首句时间范围无效')
        expected = expected_candidate_revisions
        if not isinstance(expected, dict) or any(not isinstance(k, str) or type(v) is not int or v < 1 for k, v in expected.items()):
            raise LibraryError('conflict', '人工定位缺少完整匹配版本，请刷新后重试')
        if type(expected_reference_revision) is not int or expected_reference_revision < 0:
            raise LibraryError('conflict', '人工定位缺少基准备选版本，请刷新后重试')
        with self._connection(True) as con:
            self._get(con, 'productions', production_id)
            session = self._get(con, 'sessions', session_id)
            if session['production_id'] != production_id:
                raise LibraryError('invalid', 'PGM 拍摄场次不属于当前剧目')
            self._revision(session, expected_session_revision)
            config = json.loads(session['config'])
            from library_pending import _registered_pgms
            from library_correspondence import _pgm_info, _positive_rate
            paths = set(_registered_pgms(config)) | set((config.get('pgm_overrides') or {}).values())
            if media_path not in paths:
                raise LibraryError('invalid', 'PGM 未登记在该场次，不能按同名文件人工定位')
            info, _ = _pgm_info(config, media_path, {})
            rate = _positive_rate(info.get('fps'))
            try:
                frames = int(info.get('frames'))
                if rate is None or frames <= 0 or end > frames/float(rate) + 1e-10:
                    raise ValueError()
            except (TypeError, ValueError, ZeroDivisionError, OverflowError):
                raise LibraryError('invalid', '人工定位范围必须位于已登记 PGM 的真实文件范围内') from None
            rows = con.execute("SELECT c.*,t.data AS transcript_data FROM candidates c JOIN transcripts t ON t.id=c.transcript_id "
                "JOIN media m ON m.id=t.media_id WHERE c.production_id=? AND t.session_id=? AND m.path=? AND m.kind='pgm'",
                (production_id, session_id, media_path)).fetchall()
            if {row['id']: row['revision'] for row in rows} != expected:
                raise LibraryError('conflict', '该 PGM 的匹配已被其他人修改，请刷新后重新定位')
            reference_row = con.execute('SELECT * FROM reference_settings WHERE production_id=?', (production_id,)).fetchone()
            state = self._reference_row(reference_row)
            if state['revision'] != expected_reference_revision:
                raise LibraryError('conflict', '基准备选设置已更新，请刷新后重新定位')
            first_line = self._get(con, 'script_lines', script_line_ids[0])
            script = self._get(con, 'scripts', first_line['script_id'])
            if first_line['production_id'] != production_id or not script['active']:
                raise LibraryError('invalid', '人工定位必须使用当前剧目的启用台本')
            family = [row['id'] for row in con.execute('SELECT id FROM script_lines WHERE script_id=? ORDER BY line_order,id',
                                                      (first_line['script_id'],))]
            offset = family.index(script_line_ids[0])
            if script_line_ids != family[offset:offset+len(script_line_ids)]:
                raise LibraryError('invalid', '人工台本范围必须连续有序，并属于同一份启用台本')
            now = _now()
            media_key = hashlib.sha256(_json([session_id, 'pgm', media_path]).encode()).hexdigest()
            media = con.execute('SELECT * FROM media WHERE identity_key=?', (media_key,)).fetchone()
            if media is None:
                media_id = _id()
                con.execute('INSERT INTO media VALUES (?,?,?,?,?,?,?,?,?,1)',
                    (media_id, session_id, production_id, 'pgm', media_path, media_key, '{}', now, now))
            else:
                media_id = media['id']
            source_data = dict(start_seconds=begin, end_seconds=end, text='[人工画面归类]', speaker='',
                source_kind='manual_visual', classification_only=True, manual_range_explicit=True,
                manual_script_line_ids=list(script_line_ids))
            segment_key = hashlib.sha256(_json([media_id, begin, end, source_data['text']]).encode()).hexdigest()
            transcript = con.execute('SELECT * FROM transcripts WHERE segment_key=?', (segment_key,)).fetchone()
            if transcript is None:
                transcript_id = _id()
                con.execute('INSERT INTO transcripts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)',
                    (transcript_id, media_id, session_id, production_id, begin, end, source_data['text'], '',
                     segment_key, _json(source_data), now, now))
            else:
                transcript_id = transcript['id']
                con.execute('UPDATE transcripts SET data=?,updated_at=?,revision=revision+1 WHERE id=?',
                            (_json(source_data), now, transcript_id))
                # The undo/rematch version inventories observe a changed
                # manual range even when the source-local span is unchanged.
                con.execute('UPDATE candidates SET updated_at=?,updated_by=?,revision=revision+1 WHERE transcript_id=?',
                            (now, actor, transcript_id))
            candidate = con.execute('SELECT * FROM candidates WHERE script_line_id=? AND transcript_id=?',
                                    (script_line_ids[0], transcript_id)).fetchone()
            tags = ['人工画面归类', 'manual_pgm_confirmed']
            reason = '人工确认整条 PGM 的台本覆盖范围和实际首句位置；保留完整 PGM 及原始源时码'
            if candidate is None:
                candidate_id = _id()
                con.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,'confirmed',?,?,?,?,?,1)",
                    (candidate_id, production_id, script_line_ids[0], transcript_id, 1., reason, str(note),
                     _json(tags), actor, now, now))
            else:
                candidate_id = candidate['id']
                con.execute("UPDATE candidates SET status='confirmed',score=1,reason=?,note=?,tags=?,updated_by=?,updated_at=?,revision=revision+1 WHERE id=?",
                            (reason, str(note), _json(tags), actor, now, candidate_id))
            retired = []
            for row in rows:
                if (row['id'] == candidate_id or row['status'] == 'rejected' or
                        json.loads(row['transcript_data']).get('source_kind') != 'manual_visual'):
                    continue
                old_tags = list(dict.fromkeys(json.loads(row['tags']) + ['manual_range_superseded']))
                con.execute("UPDATE candidates SET status='rejected',tags=?,updated_at=?,updated_by=?,revision=revision+1 WHERE id=?",
                            (_json(old_tags), now, actor, row['id']))
                retired.append(row['id'])
            old_ids = {row['id'] for row in rows}
            # A placement correction does not revoke the user's choice of this
            # complete PGM. Move its selection to the new first-line candidate;
            # retain an anchor only when it still names that exact script line.
            # Clearing a match is a separate, deliberately destructive action.
            previous_pairs = dict(state.get('selected_clip_pairs') or {})
            selected_before = any(identity in old_ids for identity in previous_pairs)
            pairs, reset_anchors = {}, 0
            for identity, anchor in previous_pairs.items():
                if identity not in old_ids and anchor not in old_ids:
                    pairs[identity] = anchor
                    continue
                selected_id = candidate_id if identity in old_ids else identity
                selected_row = con.execute('SELECT script_line_id,status FROM candidates WHERE id=?',
                                           (selected_id,)).fetchone()
                if anchor in old_ids:
                    # Other takes may have used this PGM as their baseline
                    # occurrence. The corrected manual marker is authoritative.
                    anchor = candidate_id
                anchor_row = (con.execute('SELECT script_line_id,status FROM candidates WHERE id=?',
                                          (anchor,)).fetchone() if anchor else None)
                if (anchor and (not anchor_row or anchor_row['status'] not in ('candidate', 'confirmed')
                                or not selected_row or anchor_row['script_line_id'] != selected_row['script_line_id'])):
                    anchor = None
                    reset_anchors += 1
                # Several historical sentence choices can select the same file.
                # Collapse them once; conflicting explicit occurrences need a
                # new human choice rather than order-dependent reassignment.
                if selected_id in pairs and pairs[selected_id] != anchor:
                    pairs[selected_id] = None
                else:
                    pairs[selected_id] = anchor
            baseline = dict(state.get('baseline_export') or {})
            if state.get('reference_session_id') == session_id and baseline:
                baseline.update(valid=False, invalid_reason='基准场次的 PGM 人工定位已修改，请重新导出。')
            con.execute('INSERT INTO reference_settings VALUES (?,?,?,?,?) ON CONFLICT(production_id) DO UPDATE SET '
                        'settings=excluded.settings,updated_at=excluded.updated_at,revision=excluded.revision',
                (production_id, state.get('reference_session_id'), _json(dict(selected_clip_pairs=pairs,
                 baseline_export=baseline, baseline_script_map=state.get('baseline_script_map', {}), manual_timeline=state.get('manual_timeline', {}))),
                 now, state['revision']+1))
            editor_state = self._save_pgm_editor_state(con, session, media_path,
                pool='timeline', reviewed=True, actor=actor, now=now)
            self._audit(con, production_id, actor, 'associate_pgm_script_range', candidate_id,
                dict(session_id=session_id, media_path=media_path, start_seconds=begin, end_seconds=end,
                     script_line_ids=script_line_ids, retired_manual_candidates=retired,
                     selected_before=selected_before, reset_anchors=reset_anchors,
                     previous_candidate_revisions=expected, previous_session_revision=session['revision'],
                     previous_reference_revision=state['revision']))
            return dict(session_id=session_id, media_path=media_path, retired_manual_count=len(retired),
                selection_preserved=selected_before, reset_anchors=reset_anchors,
                editor_state=editor_state, session=self._row(self._get(con, 'sessions', session_id)),
                reference_state=self._reference_row(con.execute(
                    'SELECT * FROM reference_settings WHERE production_id=?', (production_id,)).fetchone()),
                candidate=self._row(con.execute(self._CANDIDATE_JOIN + ' WHERE c.id=?', (candidate_id,)).fetchone()),
                transcript={'media_id': media_id, 'transcript_ids': [transcript_id],
                            'segments': [self._transcript(con, transcript_id)]})

    def register_artifact(self, session_id, kind, path, manifest=None, actor="local"):
        kind, path = _required(kind, "输出类型"), _required(path, "输出路径")
        if not isinstance(manifest if manifest is not None else {}, dict):
            raise LibraryError("invalid", "输出清单必须是对象")
        now, identity = _now(), _id()
        with self._connection(True) as con:
            session = self._get(con, "sessions", session_id)
            con.execute("INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,1)",
                        (identity, session_id, session["production_id"], kind, path, _json(manifest or {}), now, now))
            self._audit(con, session["production_id"], actor, "register_artifact", identity, {"kind": kind, "path": path})
            return self._row(self._get(con, "artifacts", identity))

    def list_artifacts(self, session_id, actor="local"):
        with self._connection() as con:
            self._get(con, "sessions", session_id)
            return [self._row(row) for row in con.execute("SELECT * FROM artifacts WHERE session_id=? ORDER BY created_at,id", (session_id,))]

    def audit(self, production_id, limit=100, actor="local"):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise LibraryError("invalid", "审计条数必须在 1 到 1000 之间")
        with self._connection() as con:
            self._get(con, "productions", production_id)
            return [self._row(row) for row in con.execute("SELECT * FROM audit_log WHERE production_id=? ORDER BY rowid DESC LIMIT ?", (production_id, limit))]

    def backup(self, path, actor="local"):
        target = _local_db_path(_required(path, "备份路径"))
        if not self._uri and os.path.realpath(target) == os.path.realpath(self.path):
            raise LibraryError("invalid", "备份路径不能覆盖当前数据库")
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        except FileExistsError as exc:
            raise LibraryError("invalid", "备份目标已存在，请选择新文件名") from exc
        try:
            with self._connection() as source:
                dest = sqlite3.connect(target)
                try:
                    source.backup(dest)
                    check = dest.execute("PRAGMA integrity_check").fetchone()[0]
                    if check != "ok":
                        raise LibraryError("storage", "备份完整性校验失败")
                finally:
                    dest.close()
            with self._connection(True) as con:
                self._audit(con, None, actor, "backup", target, {"path": target})
        except Exception:
            Path(target).unlink(missing_ok=True)
            raise
        return {"path": target, "integrity": "ok", "size_bytes": os.path.getsize(target)}
