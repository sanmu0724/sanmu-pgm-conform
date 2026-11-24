"""Batch-first catalogue workflow; semantic matches never alter source timing."""
from __future__ import annotations

import copy
from datetime import datetime
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import uuid

from library_bridge import canonical_path, canonicalize_payload, data_dir, resolve_local_path
from library_store import LibraryError

VIDEO_SUFFIXES = {'.mov', '.mp4', '.mxf', '.m4v'}


def _video_info(path):
    from core import probe, validate_rate
    info = probe(path)
    rate = Fraction(str(info['fps']))
    if rate.denominator != 1 or int(rate) not in (24, 25, 30):
        raise ValueError('当前时间线仅支持整数帧率 24/25/30：' + str(path))
    validate_rate(info, int(rate))
    return canonicalize_payload(dict(info, fps=str(rate)))


def _wav_info(path, fps):
    from sound_sync import probe_wav
    info = dict(probe_wav(path, fps=fps))
    fmt = info.pop('fmt', None)
    if isinstance(fmt, bytes):
        info['fmt_hex'] = fmt.hex()
    return canonicalize_payload(info)


def _local_paths(paths, suffixes):
    result, seen = [], set()
    for value in paths:
        path = str(Path(resolve_local_path(str(value))).expanduser().resolve())
        if not Path(path).is_file() or Path(path).suffix.lower() not in suffixes:
            raise ValueError('素材文件不存在或类型不支持：' + path)
        if path not in seen:
            result.append(path)
            seen.add(path)
    return result


def _source_path(row):
    return row.get('proxy_path') or row.get('path')


def _xml_overrides(xml_paths, pgm_paths, old=None):
    """Relocate XML file references using the PGM files explicitly supplied.

    Basenames are used only to reconnect a missing XML path to one unique file,
    never to infer cameras or assign recordings.
    """
    import xml.etree.ElementTree as ET
    from proxy_workflow import file_path
    result = copy.deepcopy(old or {})
    supplied = [str(Path(resolve_local_path(path)).expanduser().resolve()) for path in pgm_paths]
    supplied_set = set(supplied)
    by_name = {}
    for value in supplied:
        by_name.setdefault(Path(value).name, []).append(value)
    for path in xml_paths:
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError) as exc:
            raise ValueError('无法读取切点 XML：' + str(exc)) from exc
        for node in root.iter('pathurl'):
            original = file_path(node.text)
            if not original or Path(original).suffix.lower() not in VIDEO_SUFFIXES:
                continue
            local = str(Path(resolve_local_path(original)).expanduser().resolve())
            if local in supplied_set:
                result[original] = local
                continue
            candidates = by_name.get(Path(original).name, [])
            if len(candidates) > 1:
                raise ValueError('存在多个同名 PGM，无法安全重新定位 XML 源文件：' + Path(original).name)
            if len(candidates) == 1:
                result[original] = candidates[0]
            elif original not in result and Path(local).is_file():
                result[original] = local
    return result


def _check(cancelled):
    if cancelled():
        raise ValueError('已取消；已经保存的字幕、转录和人工决定仍保留。')


def _pgms(config):
    result, seen = [], set()
    def add(value):
        if isinstance(value, str) and value and Path(value).suffix.lower() in VIDEO_SUFFIXES:
            if value not in seen:
                result.append(value)
                seen.add(value)
        elif isinstance(value, dict):
            add(value.get('path'))
        elif isinstance(value, list):
            for item in value:
                add(item)
    add(config.get('pgm'))
    add(config.get('pgm_sources', []))
    for value in config.get('pgm_timecodes', {}):
        add(value)
    for value in config.get('pgm_overrides', {}).values():
        add(value)
    for row in config.get('library_media', []):
        if row.get('kind') == 'pgm':
            add(row.get('path'))
    return result


def _association(config):
    proxies, audio = [], []
    proxy_set, audio_set = set(), set()
    for row in config.get('sources', []):
        path = row.get('proxy_path') or row.get('path')
        if path and path not in proxy_set:
            proxies.append(path)
            proxy_set.add(path)
    for row in config.get('sound_sources', []):
        path = row.get('path')
        if path and path not in audio_set:
            audio.append(path)
            audio_set.add(path)
    return proxies, audio


def enrich_data(data):
    """Attach only registered relationships, never filename-inferred associations."""
    from library_correspondence import filter_active_transcripts
    from sound_fallback import SoundStateIndex, sound_path_key_factory
    sound_key = sound_path_key_factory()
    data = filter_active_transcripts(data)
    result = copy.deepcopy(data)
    sessions = {row['id']: row for row in result.get('sessions', [])}
    subtitles = {}
    for row in result.get('transcripts', []):
        metadata = row.get('metadata') or {}
        if metadata.get('subtitle_path'):
            subtitles[(row.get('session_id'), row.get('media_path'))] = metadata['subtitle_path']
    counts = {}
    viable_ids = set()
    associations = {}
    sound_indexes = {}
    pgm_sets = {}
    for identity, session in sessions.items():
        config = session.get('config', {})
        associations[identity] = (_pgms(config), _association(config))
        pgm_sets[identity] = set(associations[identity][0])
        sound_indexes[identity] = SoundStateIndex(config, sound_key)
    for row in result.get('candidates', []):
        if row.get('status') != 'rejected' and row.get('script_active', 1):
            counts[row['script_line_id']] = counts.get(row['script_line_id'], 0) + 1
            viable_ids.add(row['transcript_id'])
    def attach(row):
        session = sessions.get(row.get('session_id'), {})
        config = session.get('config', {})
        pgms, (proxies, audio) = associations.get(row.get('session_id'), ([], ([], [])))
        sid = row.get('session_id')
        row.update(pgm_paths=pgms, pgm_path=row.get('media_path') if row.get('media_path') in pgm_sets.get(sid, ()) else '',
                   proxy_paths=proxies, audio_paths=audio, proxies=proxies, audio=audio,
                   proxy=proxies[0] if len(proxies) == 1 else '',
                   subtitle_path=subtitles.get((row.get('session_id'), row.get('media_path')), ''),
                   session_name=session.get('name', row.get('session_name', '')),
                   clip_count=counts.get(row.get('script_line_id'), 0))
        row['srt_path'] = row['subtitle_path']
        row['pgm'] = row['pgm_path']
        path = row.get('media_path')
        sound_index = sound_indexes.get(sid)
        sync = sound_index.entry('library_sync', path) if sound_index else {}
        processing = sound_index.entry('library_processing', path) if sound_index else {}
        fallback = sound_index.eligible(path) if sound_index else False
        fallback_state = sound_index.entry('library_sound_fallback', path) if sound_index else {}
        failed_sync = processing.get('status') == 'failed' and processing.get('stage') == 'sync'
        row['sync_status'] = 'fallback' if fallback else 'failed' if failed_sync else sync.get('status', 'pending')
        row['sync_error'] = (fallback_state.get('error') or processing.get('error', '')) if fallback \
            else processing.get('error', '') if failed_sync else sync.get('error', '')
        row['audio_source'] = 'pgm' if fallback else 'original_wav' if sync.get('status') == 'matched' else ''
        row['sync_uncovered'] = copy.deepcopy(sync.get('uncovered', []))
        row['synced_audio_paths'] = [] if fallback or failed_sync else list(dict.fromkeys(
            item['wav_path'] for item in sync.get('placements', []) if item.get('wav_path')))
        return row
    for row in result.get('candidates', []):
        attach(row)
    result['unmatched_transcripts'] = [attach(copy.deepcopy(row)) for row in result.get('transcripts', [])
                                      if row['id'] not in viable_ids]
    result['unmatched'] = result['unmatched_transcripts']
    result['script_counts'] = counts
    for row in result.get('scripts', []):
        row['clip_count'] = counts.get(row['id'], 0)
    result['notes'] = [copy.deepcopy(note) for session in sessions.values()
                       for note in session.get('config', {}).get('library_notes_import', {}).get('rows', [])]
    from library_angles import AngleMapper, enrich_related_angles
    from library_pending import enrich_pending_pgm
    from library_auto_review import enrich_review_state
    mapper = AngleMapper(result.get('sessions', []))
    return enrich_review_state(enrich_pending_pgm(enrich_related_angles(result, mapper), mapper=mapper))


class BatchWorkflow:
    def __init__(self, client, production_id, actor='local'):
        self.client, self.production_id, self.actor = client, production_id, actor

    def read_all(self):
        data = {
            'sessions': self.client.call('list_sessions', production_id=self.production_id),
            'scripts': self.client.call('list_script', production_id=self.production_id),
            'transcripts': self.client.call('list_transcripts', production_id=self.production_id),
            'candidates': self.client.call('list_candidates', production_id=self.production_id),
            'reference_state': self.client.call('get_reference_settings', production_id=self.production_id),
        }
        # The browser displays current ASR only. Keep a lightweight revision
        # inventory of *all* associations so removing a whole file can detect
        # concurrent edits to hidden historical/manual annotations as well.
        sessions = {row['id']: row for row in data['sessions']}
        versions = {(row['id'], path): dict(session_id=row['id'], media_path=path,
                    session_revision=row['revision'], candidate_revisions={},
                    reference_revision=data['reference_state'].get('revision', 0))
                    for row in data['sessions'] for path in _pgms(row.get('config') or {})}
        for row in data['candidates']:
            if row.get('media_kind', 'pgm') != 'pgm':
                continue
            key = (row.get('session_id'), row.get('media_path'))
            session = sessions.get(key[0])
            if session is None or not key[1]:
                continue
            version = versions.setdefault(key, dict(session_id=key[0], media_path=key[1],
                session_revision=session['revision'], candidate_revisions={},
                reference_revision=data['reference_state'].get('revision', 0)))
            version['candidate_revisions'][row['id']] = row['revision']
        data['pgm_match_versions'] = list(versions.values())
        candidate_versions = {}
        for row in data['candidates']:
            candidate_versions.setdefault(row['transcript_id'], {})[row['id']] = row['revision']
        data['match_candidate_versions'] = candidate_versions
        data = enrich_data(data)
        from reference_workflow import decorate_reference_state
        data['reference_state'] = decorate_reference_state(data)
        from workflow_readiness import session_readiness_map
        data['session_readiness'] = session_readiness_map(data['sessions'])
        from library_timeline import build_timeline_data
        data['timeline_nodes'] = build_timeline_data(data, prepared=True, whole_pgm=True)
        from pgm_intervals import build_pgm_intervals
        data['pgm_interval_nodes'] = build_pgm_intervals(data, prepared=True, script_nodes=data['timeline_nodes'])
        return data

    def hide_pgm_from_pool(self, session_id, pgm_path, *, expected_match_version,
                           progress=lambda n, m: None, cancelled=lambda: False):
        _check(cancelled)
        path = canonical_path(str(pgm_path))
        version = expected_match_version
        if (not isinstance(version, dict) or version.get('session_id') != session_id
                or version.get('media_path') != path):
            raise LibraryError('conflict', '删除缺少该PGM的版本信息，请刷新后重试。')
        progress(20, '从媒体池中隐藏PGM…')
        try:
            result = self.client.call('hide_pgm_from_pool', production_id=self.production_id,
                session_id=session_id, media_path=path,
                expected_session_revision=version.get('session_revision'),
                expected_candidate_revisions=copy.deepcopy(version.get('candidate_revisions')),
                expected_reference_revision=version.get('reference_revision'))
        except LibraryError as exc:
            if exc.code == 'invalid' and '不支持的操作' in str(exc):
                raise LibraryError('incompatible', '当前素材库服务尚不支持媒体池删除。尚未修改数据，请更新并重启服务（API revision 7 或以上）后重试。') from exc
            raise
        result['data'] = self.read_all()
        progress(100, '已从媒体池隐藏，相关数据与磁盘文件保留。')
        return result

    def clear_pgm_matches(self, session_id, media_path, *, expected_session_revision,
                          expected_candidate_revisions, expected_reference_revision,
                          progress=lambda n, m: None, cancelled=lambda: False):
        """Remove script placement/selection for one full-path PGM, not media."""
        _check(cancelled)
        progress(15, '移除该 PGM 的台本匹配与备选选择…')
        try:
            result = self.client.call('clear_pgm_matches', production_id=self.production_id,
                session_id=session_id, media_path=canonical_path(str(media_path)),
                expected_session_revision=expected_session_revision,
                expected_candidate_revisions=copy.deepcopy(expected_candidate_revisions),
                expected_reference_revision=expected_reference_revision, editor_policy='pgm_editor_v1')
        except LibraryError as exc:
            if exc.code == 'invalid' and 'editor_policy' in str(exc):
                raise self._editor_service_upgrade_error() from exc
            raise
        # Once committed, always return the new snapshot even if cancellation
        # arrives during refresh, rather than presenting a false rollback.
        progress(80, '刷新台本位置与待人工匹配素材…')
        result['data'] = self.read_all()
        progress(100, '已移除台本匹配，PGM 和字幕保留，可重新人工归类。')
        return result

    @staticmethod
    def _editor_service_upgrade_error():
        return LibraryError('incompatible',
            '当前素材库服务版本过旧，尚未保存本次修改。请更新并重启素材库服务（需要 API revision 6 或以上），'
            '再刷新项目并重试。')

    def mark_pgm_reviewed(self, session_id, pgm_path, *, expected_match_version, reviewed=True,
                          progress=lambda n, m: None, cancelled=lambda: False):
        """Commit a human file review without changing matching/source media."""
        _check(cancelled)
        path = canonical_path(str(pgm_path))
        version = expected_match_version
        if (not isinstance(version, dict) or version.get('session_id') != session_id
                or version.get('media_path') != path):
            raise LibraryError('conflict', 'PGM核实缺少该文件的版本信息，请刷新后重试。')
        progress(15, '保存整条PGM的人工核实状态…')
        try:
            result = self.client.call('mark_pgm_reviewed', production_id=self.production_id,
                session_id=session_id, media_path=path, reviewed=reviewed,
                expected_session_revision=version.get('session_revision'),
                expected_candidate_revisions=copy.deepcopy(version.get('candidate_revisions')),
                expected_reference_revision=version.get('reference_revision'), editor_policy='pgm_editor_v1')
        except LibraryError as exc:
            if exc.code == 'invalid' and ('editor_policy' in str(exc) or '不支持的操作' in str(exc)):
                raise self._editor_service_upgrade_error() from exc
            raise
        progress(80, '刷新PGM编辑状态…')
        result['data'] = self.read_all()
        progress(100, '已保存PGM人工核实状态。')
        return result

    def save_match_results(self, data_snapshot, spoken_transcripts, new_candidates):
        """Atomically retire old machine guesses only for the analyzed sources."""
        from library_pending import is_manual_visual
        spoken = [row for row in spoken_transcripts if not is_manual_visual(row)]
        identities = {row['id'] for row in spoken}
        sessions = {row['session_id'] for row in spoken}
        versions = data_snapshot.get('match_candidate_versions')
        if not isinstance(versions, dict):
            raise LibraryError('conflict', '台词匹配缺少完整版本快照，请刷新后重试。')
        expected = {candidate_id: revision for identity in identities
                    for candidate_id, revision in versions.get(identity, {}).items()}
        return self.client.call('replace_candidates', production_id=self.production_id,
            candidates=new_candidates, refreshed_transcript_ids=sorted(identities),
            expected_candidate_revisions=expected,
            expected_session_revisions={row['id']: row['revision'] for row in data_snapshot['sessions']
                                        if row['id'] in sessions},
            expected_reference_revision=(data_snapshot.get('reference_state') or {}).get('revision', 0))

    def _apply_auto_review(self, data, cancelled=lambda: False, transcript_ids=None):
        from library_auto_review import select_auto_confirmations
        count = 0
        for candidate in select_auto_confirmations(data):
            if transcript_ids is not None and candidate.get('transcript_id') not in transcript_ids:
                continue
            _check(cancelled)
            # A concurrent human confirmation or rejection wins the CAS check.
            try:
                self.client.call('update_candidate', candidate_id=candidate['id'], status='confirmed',
                    note='台词匹配度高于60%自动通过；同场同区间机位一并归类。',
                    tags=list(dict.fromkeys(candidate.get('tags', []) + ['auto_dialogue'])),
                    expected_revision=candidate['revision'])
                count += 1
            except LibraryError as exc:
                if exc.code != 'conflict':
                    raise
        return count

    def refresh_review(self, progress=lambda n, m: None, cancelled=lambda: False):
        """Explicit refresh persists eligible decisions; read_all stays read-only."""
        _check(cancelled)
        progress(5, '刷新PGM台本匹配状态…')
        count = self._apply_auto_review(self.read_all(), cancelled)
        progress(100, '已刷新PGM台本匹配状态。')
        return {'data': self.read_all(), 'autoconfirmed': count, 'refreshed': True}

    def ingest_pgm(self, paths, batch_name='', current_snapshot=None, progress=lambda n, m: None, cancelled=lambda: False):
        return self.ingest_materials(paths, batch_name, current_snapshot=current_snapshot,
                                     progress=progress, cancelled=cancelled)

    def ingest_materials(self, paths, batch_name='', proxies=None, audio_paths=None, session_id=None,
                         expected_revision=None, current_snapshot=None,
                         progress=lambda n, m: None, cancelled=lambda: False,
                         cut_xml_paths=None, recording_day=None, recording_kind=None, camera_count=None):
        """Register one recording's explicit source groups; updates always use CAS."""
        _check(cancelled)
        paths = _local_paths(paths, VIDEO_SUFFIXES)
        xml_paths = _local_paths(cut_xml_paths or [], {'.xml'})
        audio_paths = _local_paths(audio_paths or [], {'.wav','.wave'})
        all_sessions = self.client.call('list_sessions', production_id=self.production_id)
        existing = self.client.call('get_session', session_id=session_id) if session_id else None
        recording = None
        if recording_day is not None or recording_kind is not None:
            from recording_schema import normalize_recording, recording_name
            stored = existing.get('config', {}) if existing else {}
            if existing and (recording_day, recording_kind) == (stored.get('recording_day'), stored.get('recording_kind')):
                # Appending media does not rename or normalize legacy labels.
                recording = recording_day, recording_kind
                standard_name = stored.get('recording_name') or existing['name']
            else:
                recording = normalize_recording(recording_day, recording_kind)
                standard_name = recording_name(*recording)
        from camera_schema import cameras_for
        if camera_count is not None:
            if isinstance(camera_count, bool) or not isinstance(camera_count, int) or not 1 <= camera_count <= 26:
                raise ValueError('机位数量必须为 1 至 26 的整数。')
        if existing:
            camera_ids = cameras_for(existing['config'])
            if camera_count is not None and camera_count != len(camera_ids):
                raise ValueError('追加素材不能改变已有场次的机位数量，请使用本场原有配置。')
        else:
            camera_ids = cameras_for({'camera_count': camera_count if camera_count is not None else 4})
        proxy_rows = []
        for row in proxies or []:
            if not isinstance(row, dict) or row.get('camera') not in camera_ids:
                raise ValueError('每条代理都必须明确选择本场已配置的机位（' + '/'.join(camera_ids) + '），不能从文件名猜测。')
            copied = copy.deepcopy(row)
            copied['path'] = _local_paths([row.get('proxy_path') or row.get('path', '')], VIDEO_SUFFIXES)[0]
            if copied.get('original_path'):
                copied['original_path'] = canonical_path(str(Path(resolve_local_path(copied['original_path'])).expanduser().resolve()))
            proxy_rows.append(copied)
        if not paths and not proxy_rows and not audio_paths and not xml_paths:
            raise ValueError('请选择切点 XML、PGM、代理或原始主声音 WAV。')
        if existing:
            if existing['production_id'] != self.production_id:
                raise ValueError('追加的拍摄场次不属于当前剧目。')
            if expected_revision is None or existing['revision'] != expected_revision:
                raise LibraryError('conflict', '拍摄场次已更新；请刷新后重新选择追加素材。')
            old_day, old_kind = existing['config'].get('recording_day'), existing['config'].get('recording_kind')
            if recording and old_day is not None and (old_day, old_kind) != recording:
                raise ValueError('已有场次的拍摄日和类型不能在追加素材时改变，请选择正确场次。')
        elif not paths:
            raise ValueError('新拍摄场次至少需要一条 PGM；只补代理或声音请选择追加已有场次。')
        shared = [canonical_path(path) for path in paths]
        owners = [row for row in all_sessions if row['id'] != session_id
                  and set(shared).intersection(_pgms(row.get('config', {})))]
        if owners:
            owner = owners[0]
            old_config = owner['config']
            same_session = len(owners) == 1 and set(shared).issubset(set(_pgms(old_config)))
            registered_proxies = {canonical_path(_source_path(row)) for row in old_config.get('sources', []) if _source_path(row)}
            registered_audio = {canonical_path(row['path']) for row in old_config.get('sound_sources', []) if row.get('path')}
            old_camera = {canonical_path(_source_path(row)): row.get('camera') for row in old_config.get('sources', []) if _source_path(row)}
            same_materials = all(canonical_path(row['path']) in registered_proxies and
                                 old_camera.get(canonical_path(row['path'])) == row['camera'] for row in proxy_rows)
            same_materials = same_materials and all(canonical_path(path) in registered_audio for path in audio_paths)
            same_xmls = not xml_paths or [canonical_path(path) for path in xml_paths] == (
                old_config.get('cut_xml_paths') or [old_config.get('cut_xml')])
            same_recording = not recording or (old_config.get('recording_day'), old_config.get('recording_kind')) == recording
            same_recording = same_recording and (camera_count is None or len(cameras_for(old_config)) == camera_count)
            if not session_id and same_session and same_materials and same_xmls and same_recording:
                return {'session': owner, 'data': self.read_all(), 'count': 0, 'unchanged': True,
                        'existing': True, 'preserved_associations': True}
            raise ValueError('PGM 已登记在“' + '、'.join(row['name'] for row in owners) +
                             '”；请追加对应场次，不能重复归入其他拍摄场次。')
        if recording and not existing and any(row.get('name') == standard_name or
                (row.get('config', {}).get('recording_day'), row.get('config', {}).get('recording_kind')) == recording
                for row in all_sessions):
            raise ValueError('所选场次已存在，请选择追加“' + standard_name + '”。')
        snapshot = canonicalize_payload(current_snapshot or {})
        snapshot_config = snapshot.get('config', {})
        snapshot_paths = {canonical_path(str(Path(resolve_local_path(path)).resolve())) for path in _pgms(snapshot_config)}
        exact = bool(snapshot_paths) and snapshot_paths == set(shared)
        if existing:
            config = copy.deepcopy(existing['config'])
            segments = copy.deepcopy(existing.get('segments', []))
            export_dir = existing.get('export_dir', '')
        elif exact:
            config = copy.deepcopy(snapshot_config)
            segments = copy.deepcopy(snapshot.get('segments', []))
            export_dir = snapshot.get('export_dir', '')
        else:
            config = {'mode': 'cut_xml', 'sources': [], 'sound_sources': [], 'label_match_mode':'prefix'}
            segments, export_dir = [], ''
        if not existing:
            config.pop('library_context', None)
        original_config = copy.deepcopy(config)
        if any(row.get('camera') not in camera_ids for row in config.get('sources', [])):
            raise ValueError('当前代理机位超出所选机位数量，请重新选择正确数量。')
        config.update(camera_ids=list(camera_ids), camera_count=len(camera_ids))
        pgms = _pgms(config)
        for path in shared:
            if path not in pgms:
                pgms.append(path)
        pgm_changed = bool(existing) and set(pgms) != set(_pgms(existing['config']))
        if pgm_changed and not xml_paths:
            for key in ('cut_xml', 'cut_xml_paths', 'cut_xml_digest', 'cut_xml_digests', 'pgm_overrides', 'pgm_sources'):
                config.pop(key, None)
            segments = []
            config['library_cuts_invalidated'] = '追加 PGM 后需重新导入覆盖整场的切点 XML。'
        registry = copy.deepcopy(config.get('library_media', []))
        sources = copy.deepcopy(config.get('sources', []))
        sounds = copy.deepcopy(config.get('sound_sources', []))
        # Upgrade explicitly associated legacy rows without changing their identities.
        for source in sources:
            path = _source_path(source)
            if path:
                media = next((row for row in registry if row.get('kind') == 'proxy' and row.get('path') == path), None)
                if media is None:
                    media = dict(id=source.get('source_id') or str(uuid.uuid4()), kind='proxy',
                                 path=path, camera=source.get('camera', ''))
                    registry.append(media)
                media.setdefault('id', source.get('source_id') or str(uuid.uuid4()))
                source.setdefault('source_id', media['id'])
        for sound in sounds:
            path = sound.get('path')
            if path and not any(row.get('kind') == 'main_wav' and row.get('path') == path for row in registry):
                registry.append(dict(id=str(uuid.uuid4()), kind='main_wav', path=path))
        def registered(kind, path):
            for row in registry:
                if row.get('kind') == kind and row.get('path') == path:
                    row.setdefault('id', str(uuid.uuid4()))
                    return row
            row = dict(id=str(uuid.uuid4()), kind=kind, path=path)
            registry.append(row)
            return row
        fps = None
        # Probe all newly supplied PGM files, never substitute a default frame rate.
        for index, local in enumerate(paths):
            _check(cancelled)
            progress(round(index / max(1, len(paths)) * 35), '读取 PGM：' + Path(local).name)
            info = _video_info(local)
            rate = int(Fraction(info['fps']))
            if fps is not None and rate != fps:
                raise ValueError('同一拍摄场次的 PGM 帧率必须一致。')
            fps = rate
            row = registered('pgm', canonical_path(local))
            row['probe'] = info
        if existing and config.get('fps') and fps is not None and int(config['fps']) != fps:
            raise ValueError('新增 PGM 帧率与现有场次不一致。')
        if fps is None:
            fps = int(config.get('fps', 0))
            if not fps:
                first = _video_info(str(Path(resolve_local_path(pgms[0])).resolve()))
                fps = int(Fraction(first['fps']))
                registered('pgm', pgms[0])['probe'] = first
        config['fps'] = fps
        if xml_paths:
            from cut_xml import read_cut_xmls, timeline_info
            overrides = _xml_overrides(xml_paths, pgms, config.get('pgm_overrides'))
            supplied_cut = read_cut_xmls(xml_paths, pgm_overrides=overrides)
            supplied_sources = {canonical_path(block['path']) for block in supplied_cut['pgm_sources']}
            expected = set(pgms)
            full_xml_paths = list(xml_paths)
            if supplied_sources != expected and existing:
                old_xmls = existing['config'].get('cut_xml_paths') or ([existing['config']['cut_xml']]
                    if existing['config'].get('cut_xml') else [])
                full_xml_paths = [str(Path(resolve_local_path(path)).expanduser().resolve()) for path in old_xmls]
                full_xml_paths += [path for path in xml_paths if path not in full_xml_paths]
                overrides = _xml_overrides(full_xml_paths, pgms, overrides)
            cut_config = dict(config, cut_xml_paths=full_xml_paths, pgm_overrides=overrides)
            cut_config.pop('pgm_start_tc', None)
            cut = timeline_info(cut_config)
            actual = {canonical_path(block['path']) for block in cut['pgm_sources']}
            if actual != expected:
                missing = ', '.join(Path(path).name for path in expected - actual)
                extra = ', '.join(Path(path).name for path in actual - expected)
                raise ValueError('切点 XML 与本场 PGM 不对应。' + ('未覆盖：' + missing + '。' if missing else '') +
                                 ('未登记：' + extra + '。' if extra else ''))
            if cut['fps'] != fps:
                raise ValueError('切点 XML 帧率与本场 PGM 不一致。')
            # Only unchanged complete cut geometry can retain prior OCR review.
            if config.get('cut_xml_digest') != cut['xml_digest']:
                segments = []
            pgms = [canonical_path(block['path']) for block in cut['pgm_sources']]
            config.update(cut_xml=canonical_path(full_xml_paths[0]),
                          cut_xml_paths=[canonical_path(path) for path in full_xml_paths],
                          cut_xml_digest=cut['xml_digest'], cut_xml_digests=cut['xml_digests'],
                          pgm_overrides={key: canonical_path(value) for key, value in overrides.items()},
                          pgm_sources=[dict(block, path=canonical_path(block['path'])) for block in cut['pgm_sources']])
            config.pop('library_cuts_invalidated', None)
        for path in pgms:
            registered('pgm', path)
        for supplied in proxy_rows:
            _check(cancelled)
            local, camera = supplied['path'], supplied['camera']
            path = canonical_path(local)
            info = _video_info(local)
            if int(Fraction(info['fps'])) != fps:
                raise ValueError('代理帧率与 PGM 不同，请先核对转码设置：' + local)
            prior = next((row for row in sources if _source_path(row) == path), None)
            registry_row = registered('proxy', path)
            prior_camera = (prior or {}).get('camera') or registry_row.get('camera')
            if prior_camera and prior_camera != camera:
                raise ValueError('同一代理已分配给其他机位，请核对后再修改归类：' + path)
            registry_row.update(camera=camera, probe=info)
            source = copy.deepcopy(prior or {})
            source.update(camera=camera, path=path, proxy_path=path, start_tc=info.get('start_tc', ''),
                          frames=info['frames'], width=info.get('width'), height=info.get('height'),
                          source_id=source.get('source_id') or registry_row['id'])
            for key in ('original_path', 'original_start_tc', 'original_frames', 'original_width', 'original_height'):
                if key in supplied:
                    source[key] = supplied[key]
            source.setdefault('original_path', '')
            source.setdefault('original_start_tc', '')
            if prior is None:
                sources.append(source)
            else:
                sources[sources.index(prior)] = source
        for local in audio_paths:
            _check(cancelled)
            path = canonical_path(local)
            info = _wav_info(local, fps)
            registered('main_wav', path)['probe'] = info
            prior = next((row for row in sounds if row.get('path') == path), None)
            source = copy.deepcopy(prior or {})
            if source.get('channel') in ('L', 'R'):
                source.setdefault('sound_role', source['channel'])
            source.update(path=path, channel='main', metadata=copy.deepcopy(info.get('metadata', {})),
                          filename=info.get('filename', Path(local).name), sound_roll=info.get('sound_roll', ''),
                          recording_date=info.get('recording_date', ''))
            if prior is None:
                sounds.append(source)
            else:
                sounds[sounds.index(prior)] = source
        config.update(pgm=pgms[0], library_media=registry, sources=sources, sound_sources=sounds,
                      library_record_type='recording', external_sound=True, library_sound_cache_only=True)
        if recording:
            config.update(recording_day=recording[0], recording_kind=recording[1], recording_name=standard_name)
            if not existing:
                config['show_id'] = standard_name
        config.setdefault('sound_sync_mode', 'waveform')
        if not existing:
            config['library_batch_created'] = datetime.now().isoformat(timespec='seconds')
        name = existing['name'] if existing and recording else (standard_name if recording else
            str(batch_name or '').strip() or (existing['name'] if existing else 'PGM批次_' + datetime.now().strftime('%Y%m%d_%H%M%S')))
        unchanged = bool(existing) and config == original_config and name == existing['name'] and segments == existing.get('segments', [])
        _check(cancelled)
        if unchanged:
            saved = existing
        else:
            saved = self.client.call('save_session', production_id=self.production_id, name=name, config=config,
                segments=segments, export_dir=export_dir, session_id=session_id, expected_revision=expected_revision)
        count = len(paths) + len(proxy_rows) + len(audio_paths) + len(xml_paths)
        progress(100, f'已登记 {count} 项素材')
        return {'session': saved, 'data': self.read_all(), 'count': count, 'unchanged': unchanged,
                'preserved_associations': bool(existing) or exact, 'cuts_invalidated': pgm_changed and not bool(xml_paths)}

    def import_script(self, path, progress=lambda n, m: None, cancelled=lambda: False):
        from dialogue_engine import import_script
        _check(cancelled)
        lines = import_script(path)
        _check(cancelled)
        script = self.client.call('put_script', production_id=self.production_id, title=Path(path).stem,
                                  lines=lines, source_path=canonical_path(str(Path(path).expanduser().resolve())))
        progress(100, f'已导入 {len(lines)} 个台本区间')
        return {'script': script, 'data': self.read_all(), 'count': len(lines)}

    def import_notes(self, path, progress=lambda n, m: None, cancelled=lambda: False):
        from dialogue_engine import import_notes
        _check(cancelled)
        path = Path(path).expanduser().resolve()
        rows = import_notes(path)
        for row in rows:
            if row.get('media_path') and Path(row['media_path']).is_absolute():
                row['media_path'] = canonical_path(row['media_path'])
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        source = canonical_path(str(path))
        for session in self.client.call('list_sessions', production_id=self.production_id):
            existing = session.get('config', {}).get('library_notes_import', {})
            if existing.get('sha256') == digest and existing.get('source_path') == source:
                return {'session': session, 'data': self.read_all(), 'count': len(rows), 'unchanged': True}
        _check(cancelled)
        config = {'library_record_type': 'notes', 'library_notes_import': {'source_path': source, 'sha256': digest, 'rows': rows}}
        saved = self.client.call('save_session', production_id=self.production_id, name='场记_' + path.stem, config=config)
        self.client.call('register_artifact', session_id=saved['id'], kind='field_notes', path=source,
                         manifest={'row_count': len(rows), 'sha256': digest})
        progress(100, f'已保留 {len(rows)} 条场记原文')
        return {'session': saved, 'data': self.read_all(), 'count': len(rows), 'unchanged': False}

    def _save_state(self, session_id, field, path, state):
        for attempt in range(2):
            session = self.client.call('get_session', session_id=session_id)
            # Only this state table changes. Copying every camera/media/header
            # for every completed PGM made long ASR/sync batches quadratic in
            # the scene's registered media size. Keep CAS and per-file saves.
            config = dict(session['config'])
            config[field] = dict(config.get(field) or {})
            if state is None:
                if path not in config.get(field, {}):
                    return session
                config.get(field, {}).pop(path, None)
                if not config.get(field):
                    config.pop(field, None)
            else:
                config.setdefault(field, {})[path] = state
            try:
                return self.client.call('save_session', production_id=self.production_id, name=session['name'],
                    session_id=session_id, expected_revision=session['revision'], config=config,
                    segments=session.get('segments', []), export_dir=session.get('export_dir', ''))
            except LibraryError as exc:
                if exc.code != 'conflict' or attempt:
                    raise

    def _recording_sessions(self, session_id=None):
        sessions = self.client.call('list_sessions', production_id=self.production_id)
        if session_id is None:
            return sessions
        selected = [session for session in sessions if session['id'] == session_id]
        if not selected:
            raise ValueError('请选择当前剧目的拍摄场次。')
        return selected

    def _save_script_run(self, session_id, status, **details):
        """Keep preflight/cancellation failures visible without touching sync."""
        for attempt in range(2):
            session=self.client.call('get_session',session_id=session_id)
            config=dict(session['config'])
            state=dict(config.get('library_script_run') or {})
            state.update(details,status=status,updated_at=datetime.now().astimezone().isoformat(timespec='seconds'))
            config['library_script_run']=state
            try:
                return self.client.call('save_session',production_id=self.production_id,name=session['name'],
                    session_id=session_id,expected_revision=session['revision'],config=config,
                    segments=session.get('segments',[]),export_dir=session.get('export_dir',''))
            except LibraryError as exc:
                if exc.code!='conflict' or attempt:raise

    def _save_pgm_fallback(self, session, path, error, cancelled):
        """Keep the failed sync evidence separate from ASR progress/errors."""
        from sound_fallback import valid_pgm_fallback_signature, make_pgm_fallback, sound_entry
        config = session['config']
        saved = sound_entry(config.get('library_sound_fallback'), path)
        if saved.get('status') == 'fallback' and saved.get('pgm_info') and valid_pgm_fallback_signature(config, path, saved):
            return session, saved
        info = _video_info(str(Path(resolve_local_path(path)).expanduser().resolve()))
        state = make_pgm_fallback(config, path, error, pgm_info=info)
        state['updated_at'] = datetime.now().isoformat(timespec='seconds')
        _check(cancelled)
        session = self._save_state(session['id'], 'library_sound_fallback', path, state)
        return session, state

    @staticmethod
    def _fallback_reason(config, path):
        from sound_fallback import sound_entry
        for field in ('library_sound_fallback', 'library_processing', 'library_sync'):
            row = sound_entry(config.get(field), path)
            if row.get('error'):
                return row['error']
        return '原始主声音 WAV 未匹配，沿用 PGM 自身原声。'

    @staticmethod
    def _reusable_pgm_fallback(config, path, allow_legacy=False):
        """Reuse a failed attempt only while its actual inputs are unchanged.

        A saved PGM-audio fallback is a result of one sync attempt, not a
        permanent instruction to stop matching this PGM. In particular,
        changing from waveform to timecode or adding a missing WAV must run
        sync again. Older unsigned failures remain readable by legacy ASR,
        while explicitly clicking sound sync always retries those failures.
        """
        from sound_fallback import valid_pgm_fallback
        return valid_pgm_fallback(config, path, allow_legacy=allow_legacy)

    def sync_recording(self, session_id=None, progress=lambda n, m: None, cancelled=lambda: False):
        """The sole batch sound-sync entry; ASR never recomputes it in the new UI."""
        from library_sync import ensure_synced_pgm, valid_sync
        from sound_fallback import fallback_enabled, sound_entry, retire_sound_references
        sessions = self._recording_sessions(session_id)
        jobs = [(session['id'], path) for session in sessions for path in _pgms(session.get('config', {}))]
        if not jobs:
            raise ValueError('请先导入所选场次的 PGM 和原始主声音 WAV。')
        done, skipped, fallbacks, failures = 0, 0, [], []
        for index, (identity, path) in enumerate(jobs):
            _check(cancelled)
            session = self.client.call('get_session', session_id=identity)
            if (not session['config'].get('library_sound_cache_only') or
                    'sound_reference_overrides' in session['config'] or
                    session['config'].get('sound_unmatched_policy', 'pgm') != 'pgm'):
                config = retire_sound_references(session['config'])
                config['library_sound_cache_only'] = True
                config['sound_unmatched_policy'] = 'pgm'
                session = self.client.call('save_session', production_id=self.production_id, name=session['name'],
                    session_id=identity, expected_revision=session['revision'], config=config,
                    segments=session.get('segments', []), export_dir=session.get('export_dir', ''))
            config = session['config']
            cached = valid_sync(config, path, config.get('library_sync', {}).get(path))
            def per_file(value, message):
                progress(round((index + max(0, min(100, value)) / 100) / len(jobs) * 100),
                         f'合板 {index+1}/{len(jobs)} {Path(path).name}：{message}')
            try:
                if self._reusable_pgm_fallback(config, path):
                    session, state = self._save_pgm_fallback(session, path,
                        self._fallback_reason(config, path), cancelled)
                    fallbacks.append(dict(session_id=identity, media_path=path, error=state['error'], reused=True))
                    per_file(100, '沿用 PGM 原声；本段不再重复匹配原始 WAV。')
                    continue
                sync = ensure_synced_pgm(config, path, progress=per_file, cancelled=cancelled)
                _check(cancelled)
                if sync.get('status') != 'matched' or not sync.get('placements'):
                    raise ValueError('PGM 尚未匹配到原始主声音 WAV。')
                if config.get('library_sync', {}).get(path) != sync:
                    self._save_state(identity, 'library_sync', path, sync)
                if sound_entry(config.get('library_sound_fallback'), path):
                    self._save_state(identity, 'library_sound_fallback', path, None)
                previous = config.get('library_processing', {}).get(path, {})
                if previous.get('stage') == 'sync':
                    self._save_state(identity, 'library_processing', path, None)
                skipped += int(cached)
                done += int(not cached)
            except Exception as exc:
                _check(cancelled)
                if isinstance(exc, LibraryError):
                    raise
                if fallback_enabled(config):
                    try:
                        session, state = self._save_pgm_fallback(session, path, str(exc), cancelled)
                        self._save_state(identity, 'library_processing', path, state)
                        fallbacks.append(dict(session_id=identity, media_path=path, error=str(exc), reused=False))
                        per_file(100, '未匹配原始 WAV，已改用 PGM 原声。')
                        continue
                    except Exception as fallback_exc:
                        _check(cancelled)
                        if isinstance(fallback_exc, LibraryError):
                            raise
                        exc = ValueError(str(exc) + '；PGM 原声不可用：' + str(fallback_exc))
                failures.append({'session_id': identity, 'media_path': path, 'error': str(exc)})
                self._save_state(identity, 'library_processing', path,
                    {'status': 'failed', 'stage': 'sync', 'error': str(exc),
                     'updated_at': datetime.now().isoformat(timespec='seconds')})
        progress(100, f'声音合板完成：新合板 {done}，沿用 {skipped}，PGM 原声 {len(fallbacks)}，待复核 {len(failures)}。')
        return {'data': self.read_all(), 'synced': done, 'skipped': skipped, 'failures': failures,
                'job_count': len(jobs), 'sync_only': True, 'fallback': len(fallbacks), 'fallback_rows': fallbacks}

    def recognize_and_match(self, progress=lambda n, m: None, cancelled=lambda: False, redo=False,
                            model='small', language='auto', require_synced=False, session_id=None,
                            require_prepared=False):
        sessions=self._recording_sessions(session_id)
        run_id=str(uuid.uuid4())
        for session in sessions:
            self._save_script_run(session['id'],'running',run_id=run_id,stage='preflight',error='',
                started_at=datetime.now().astimezone().isoformat(timespec='seconds'),
                job_count=len(_pgms(session.get('config',{}))),failures=0)
        try:
            result=self._recognize_and_match(progress,cancelled,redo,model,language,require_synced,
                                             session_id,require_prepared,_sessions=sessions)
        except Exception as exc:
            for session in sessions:
                try:self._save_script_run(session['id'],'cancelled' if cancelled() else 'failed',error=str(exc))
                except Exception:pass  # Preserve the real processing error if storage also failed.
            raise
        for session in sessions:
            failures=sum(row['session_id']==session['id'] for row in result['failures'])
            self._save_script_run(session['id'],'partial' if failures else 'completed',stage='finished',failures=failures)
        # Run-state bookkeeping alone does not require rebuilding the catalogue.
        # A real edit during completion must refresh all derived readiness and
        # timeline views before exposing its new CAS revision to the editor.
        saved={session['id']:self.client.call('get_session',session_id=session['id']) for session in sessions}
        previous={row['id']:row for row in result['data']['sessions']}
        def semantic_state(row):
            return (row.get('production_id'),row.get('name'),row.get('segments'),row.get('export_dir'),
                    {key:value for key,value in row.get('config',{}).items() if key!='library_script_run'})
        if any(identity not in previous or semantic_state(row)!=semantic_state(previous[identity])
               for identity,row in saved.items()):
            result['data']=self.read_all()
            return result
        result['data']['sessions']=[saved.get(row['id'],row) for row in result['data']['sessions']]
        for version in result['data'].get('pgm_match_versions',[]):
            if version['session_id'] in saved:version['session_revision']=saved[version['session_id']]['revision']
        return result

    def _recognize_and_match(self, progress=lambda n, m: None, cancelled=lambda: False, redo=False,
                            model='small', language='auto', require_synced=False, session_id=None,
                            require_prepared=False, _sessions=None):
        from dialogue_engine import export_subtitles, match_dialogue, transcribe_media
        from library_sync import ensure_synced_pgm, transcribe_synced_pgm, valid_sync
        from library_pending import is_manual_visual
        from sound_fallback import fallback_enabled, sound_entry
        sessions = _sessions if _sessions is not None else self._recording_sessions(session_id)
        prepared = {}
        if require_prepared:
            from workflow_readiness import ensure_session_prepared, preparation_signature
            for session in sessions:
                _check(cancelled)
                ensure_session_prepared(session)
                prepared[session['id']] = preparation_signature(session)
            # The prepared UI path consumes the registered sound plan and never
            # starts a second alignment while recognizing dialogue.
            require_synced = True
        existing = self.client.call('list_transcripts', production_id=self.production_id)
        jobs = [(session, path) for session in sessions for path in _pgms(session.get('config', {}))]
        if not jobs:
            raise ValueError('请先批量导入 PGM。')
        for session in sessions:self._save_script_run(session['id'],'running',stage='asr')
        done, skipped, synced, autoconfirmed, fallbacks, failures = 0, 0, 0, 0, 0, []
        for index, (session, path) in enumerate(jobs):
            _check(cancelled)
            session = self.client.call('get_session', session_id=session['id'])
            if require_prepared and preparation_signature(session) != prepared[session['id']]:
                # Another user may have changed OCR/calibration while the batch
                # was running. Our own ASR bookkeeping does not trigger this.
                ensure_session_prepared(session)
                prepared[session['id']] = preparation_signature(session)
            local = Path(resolve_local_path(path)).expanduser()
            stage = 'source'
            try:
                stat = local.stat()
                fingerprint = {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
                cached = session.get('config', {}).get('library_asr', {}).get(path, {})
                known = next((row.get('metadata', {}) for row in existing
                              if row.get('session_id') == session['id'] and row.get('media_path') == path
                              and not is_manual_visual(row)
                              and row.get('metadata', {}).get('asr_complete')), {})
                completed = cached if cached.get('complete') else known
                if completed and not redo and completed.get('source_stat') and completed['source_stat'] != fingerprint:
                    raise ValueError('已识别 PGM 的文件大小或修改时间变化；请作为新素材登记，或使用字幕／转写导入明确更新。')
                def per_file(value, message):
                    progress(round((index + max(0, min(100, value)) / 100) / len(jobs) * 80),
                             f'PGM {index+1}/{len(jobs)} {local.name}：{message}')
                config = session['config']
                full_fallback = self._reusable_pgm_fallback(config, path, allow_legacy=True)
                if full_fallback:
                    session, sync = self._save_pgm_fallback(session, path,
                        self._fallback_reason(config, path), cancelled)
                    per_file(5, '沿用 PGM 自身原声，开始识别台词。')
                elif require_synced:
                    sync = sound_entry(config.get('library_sync'), path)
                    processing = sound_entry(config.get('library_processing'), path)
                    failed = processing.get('status') == 'failed' and processing.get('stage') == 'sync'
                    if failed or not valid_sync(config, path, sync):
                        stage = 'preparation'
                        raise ValueError('尚未完成有效声音合板，请先点击“声音合板”，再匹配台本。')
                    sync = copy.deepcopy(sync)
                    per_file(5, '沿用已完成的声音合板，开始识别台词。')
                else:
                    # Compatibility for callers of the pre-0.9 batch API.
                    stage = 'sync'
                    try:
                        sync = ensure_synced_pgm(config, path, progress=per_file, cancelled=cancelled)
                        if sync.get('status') != 'matched' or not sync.get('placements'):
                            raise ValueError('PGM 尚未匹配到原始主声音 WAV。')
                    except Exception as exc:
                        _check(cancelled)
                        if isinstance(exc, LibraryError) or not fallback_enabled(config):
                            raise
                        session, sync = self._save_pgm_fallback(session, path, str(exc), cancelled)
                        full_fallback = True
                _check(cancelled)
                if not full_fallback and (sync.get('status') != 'matched' or not sync.get('placements')):
                    raise ValueError('PGM 尚未匹配到原始主声音 WAV，暂不识别台词。')
                if not full_fallback and sound_entry(session['config'].get('library_sync'), path) != sync:
                    session = self._save_state(session['id'], 'library_sync', path, sync)
                fallbacks += int(full_fallback)
                synced += int(not full_fallback)
                stage = 'asr'
                audio_source = 'pgm_reference' if full_fallback else 'mixed' \
                    if fallback_enabled(session['config']) and sync.get('uncovered') else 'original_wav'
                if completed and not redo:
                    if completed.get('source_kind') == 'manual' or (completed.get('sync_signature') == sync['signature']
                            and completed.get('asr_model') == model and completed.get('asr_language') == language
                            and completed.get('asr_audio_source', 'original_wav') == audio_source):
                        skipped += 1
                        if session.get('config', {}).get('library_processing', {}).get(path):
                            self._save_state(session['id'], 'library_processing', path, None)
                        progress(round((index + 1) / len(jobs) * 80), f'沿用字幕 {index+1}/{len(jobs)}：{local.name}')
                        continue
                pgm_ranges = []
                if full_fallback:
                    info = sync['pgm_info']
                    channels = info.get('audio_channels')
                    if channels == [] or channels == 0 or info.get('has_audio') is False:
                        raise ValueError('PGM 没有可解码的参考声音；画面保留，请人工归类台词。')
                    duration = float(Fraction(str(info['frames'])) / Fraction(str(info['fps'])))
                    segments = transcribe_media(local, model=model, language=language,
                        progress=per_file, cancelled=cancelled)
                    if not any(str(row.get('text', '')).strip() for row in segments):
                        raise ValueError('PGM 原声未识别到可用台词，可能静音或无人声；请人工核对画面。')
                    pgm_ranges = [{'start_seconds': 0., 'end_seconds': duration}]
                    for row in segments:
                        row.update(source_kind='pgm', pgm_reference_path=path)
                else:
                    transcription = transcribe_synced_pgm(session['config'], path, model=model, language=language,
                                                          progress=per_file, cancelled=cancelled, sync=sync)
                    segments = transcription['segments']
                    if fallback_enabled(session['config']):
                        # The extractor returns PGM-local times even for a
                        # bounded range; never add an offset a second time.
                        for gap in sync.get('uncovered') or []:
                            _check(cancelled)
                            first, last = float(gap['start_seconds']), float(gap['end_seconds'])
                            if last <= first:
                                continue
                            rows = transcribe_media(local, model=model, language=language,
                                progress=per_file, cancelled=cancelled, start_seconds=first, duration=last-first)
                            for row in rows:
                                a, b = max(first, float(row['start_seconds'])), min(last, float(row['end_seconds']))
                                if b > a and str(row.get('text', '')).strip():
                                    row.update(start_seconds=a, end_seconds=b, source_kind='pgm', pgm_reference_path=path)
                                    segments.append(row)
                            pgm_ranges.append(dict(start_seconds=first, end_seconds=last))
                    segments.sort(key=lambda row: (row['start_seconds'], row['end_seconds']))
                _check(cancelled)
                folder = data_dir() / 'subtitles' / self.production_id / session['id']
                folder.mkdir(parents=True, exist_ok=True)
                subtitle = folder / ('media_' + str(uuid.uuid4()) + '.srt')
                if segments:
                    export_subtitles(segments, subtitle)
                else:
                    with subtitle.open('x', encoding='utf-8') as out:
                        out.write('')
                metadata = {'asr_complete': True, 'asr_model': model, 'asr_language': language,
                            'source_stat': fingerprint, 'subtitle_path': str(subtitle), 'redo': bool(redo),
                            'sync_signature': sync['signature'],
                            'asr_audio_source': audio_source,
                            'pgm_reference_ranges': pgm_ranges}
                transcript = self.client.call('add_transcript', session_id=session['id'], media_path=path,
                                              segments=segments, kind='pgm', metadata=metadata)
                self.client.call('register_artifact', session_id=session['id'], kind='subtitles_srt', path=str(subtitle),
                                 manifest={'media_id': transcript['media_id'], 'media_path': path,
                                           'segment_count': len(segments), 'source_local_seconds': True})
                self._save_state(session['id'], 'library_asr', path, dict(metadata, complete=True,
                    media_id=transcript['media_id'], active_transcript_ids=transcript['transcript_ids']))
                self._save_state(session['id'], 'library_processing', path, None)
                done += 1
            except Exception as exc:
                _check(cancelled)
                if isinstance(exc, LibraryError) and exc.code in ('network', 'unauthorized'):
                    raise
                failures.append({'session_id': session['id'], 'media_path': path, 'error': str(exc)})
                self._save_state(session['id'], 'library_processing', path,
                                 {'status': 'failed', 'stage': stage, 'error': str(exc),
                                  'updated_at': datetime.now().isoformat(timespec='seconds')})
        _check(cancelled)
        for session in sessions:self._save_script_run(session['id'],'running',stage='matching')
        data = self.read_all()
        successful_scopes = {(session['id'], path) for session, path in jobs}.difference(
            (row['session_id'], row['media_path']) for row in failures)
        spoken = [row for row in data['transcripts'] if not is_manual_visual(row)
                  and (row.get('session_id'), row.get('media_path')) in successful_scopes]
        if data['scripts'] and data['transcripts']:
            candidates = match_dialogue(data['scripts'], spoken, notes=data['notes'],
                top_k=max(5, len(spoken)),
                progress=lambda n, message: progress(80 + round(n * .2), '正在按PGM文件定位台本…'), cancelled=cancelled)
            _check(cancelled)
            matching = self.save_match_results(data, spoken, candidates)
        else:
            matching = {'added': 0, 'updated': 0, 'preserved_decisions': 0}
        autoconfirmed = self._apply_auto_review(self.read_all(), cancelled, {row['id'] for row in spoken})
        progress(100, f'PGM处理完成：新识别 {done} 条，沿用 {skipped} 条，识别失败 {len(failures)} 条；正在汇总文件匹配状态。')
        return {'data': self.read_all(), 'recognized': done, 'skipped': skipped, 'synced': synced, 'fallback': fallbacks, 'failures': failures,
                'matching': matching, 'job_count': len(jobs), 'autoconfirmed': autoconfirmed}

    def update_pgm_script_coverage(self, session_id, pgm_path, script_line_ids, *,
                                   expected_match_version, note='', progress=lambda n, m: None,
                                   cancelled=lambda: False):
        """Correct script placement while retaining the real source-local anchor.

        Timeline handles change the inclusive script range, never the media
        duration, recording timecode, WAV placement or first spoken source
        position. The new first script line locates the same source-local
        utterance. Reuse the full-file manual-range transaction so form edits,
        dragging and removal share the same version inventories and history.
        """
        _check(cancelled)
        path = canonical_path(str(pgm_path))
        if not isinstance(script_line_ids, list) or not script_line_ids:
            raise ValueError('请选择该 PGM 连续覆盖的台本行号。')
        snapshot = self.read_all()
        current_version = next((row for row in snapshot['pgm_match_versions'] if
            row['session_id'] == session_id and row['media_path'] == path), None)
        if not isinstance(expected_match_version, dict) or current_version != expected_match_version:
            raise LibraryError('conflict', '该 PGM 的匹配或基准备选设置已更新，请刷新后再调整范围。')
        from pgm_takes import PgmTakeIndex
        index = PgmTakeIndex(snapshot, prepared=True)
        identity = dict(session_id=session_id, media_path=path)
        anchor = index.file_summary(identity).get('first_candidate')
        if anchor is None:
            raise ValueError('这条 PGM 尚未确定唯一的首句位置，请先使用人工定位。')
        source = index.source(anchor)
        # No use of the whole-PGM presentation bounds (always zero/duration):
        # a manually located first line at 7.1 seconds must stay at 7.1 seconds.
        result = self.associate_pending_interval(session_id, path,
            source['start_seconds'], source['end_seconds'], script_line_ids[0],
            script_line_ids=list(script_line_ids), expected_match_version=copy.deepcopy(expected_match_version),
            note=str(note) if note else str(anchor.get('note') or ''),
            progress=progress, cancelled=cancelled)
        result['source_anchor_preserved'] = True
        return result

    def associate_pending_interval(self, session_id, pgm_path, start_seconds, end_seconds,
                                   script_line_id, note='', progress=lambda n, m: None,
                                   cancelled=lambda: False, script_line_ids=None, expected_match_version=None):
        """Confirm one real PGM picture range; its camera angles follow by TC.

        The stored marker explicitly identifies manual visual classification,
        never ASR or invented dialogue. The original WAV remains unmatched until
        sound synchronization actually succeeds.
        """
        from library_correspondence import _pgm_info, _positive_rate
        _check(cancelled)
        session = self.client.call('get_session', session_id=session_id)
        if session['production_id'] != self.production_id:
            raise ValueError('PGM 拍摄场次不属于当前剧目。')
        path = canonical_path(str(pgm_path))
        if path not in _pgms(session.get('config', {})):
            raise ValueError('请先将 PGM 登记到所选拍摄场次，不能按同名文件跨场关联。')
        try:
            begin, end = Fraction(str(start_seconds)), Fraction(str(end_seconds))
            info, _ = _pgm_info(session['config'], path, {})
            rate = _positive_rate(info.get('fps'))
            frames = int(info.get('frames'))
            if rate is None or frames <= 0 or not 0 <= begin < end <= Fraction(frames, 1) / rate:
                raise ValueError()
        except (TypeError, ValueError, ZeroDivisionError, OverflowError):
            raise ValueError('人工归类范围必须位于已登记 PGM 的真实文件范围内。') from None
        lines = self.client.call('list_script', production_id=self.production_id)
        by_id = {row['id']: row for row in lines}
        if script_line_id not in by_id:
            raise ValueError('请选择当前剧目已启用台本中的目标台词。')
        requested = [script_line_id] if script_line_ids is None else script_line_ids
        if (not isinstance(requested, list) or not requested or requested[0] != script_line_id or
                any(not isinstance(identity, str) or identity not in by_id for identity in requested) or
                len(set(requested)) != len(requested)):
            raise ValueError('人工台本范围须从目标首行开始，并包含当前台本中有效且不重复的行号。')
        script_id = by_id[script_line_id]['script_id']
        family = [row['id'] for row in lines if row['script_id'] == script_id]
        first = family.index(script_line_id)
        if requested != family[first:first+len(requested)]:
            raise ValueError('人工台本范围必须按台本顺序连续，且属于同一份启用台本。')
        if expected_match_version is None:
            snapshot = self.read_all()
            version = next((row for row in snapshot['pgm_match_versions'] if
                row['session_id'] == session_id and row['media_path'] == path), None)
        else:
            version = copy.deepcopy(expected_match_version)
        if not isinstance(version, dict) or version.get('session_id') != session_id or version.get('media_path') != path:
            raise LibraryError('conflict', '人工定位缺少该 PGM 的版本信息，请刷新后重试。')
        _check(cancelled)
        progress(15, '保存整条 PGM 的人工台本范围与首句位置…')
        try:
            result = self.client.call('associate_pgm_script_range', production_id=self.production_id,
                session_id=session_id, media_path=path, start_seconds=float(begin), end_seconds=float(end),
                script_line_ids=list(requested), note=str(note),
                selection_policy='preserve_whole_pgm', editor_policy='pgm_editor_v1',
                expected_session_revision=version.get('session_revision'),
                expected_candidate_revisions=version.get('candidate_revisions'),
                expected_reference_revision=version.get('reference_revision'))
        except LibraryError as exc:
            if exc.code == 'invalid' and any(key in str(exc) for key in ('selection_policy', 'editor_policy')):
                raise self._editor_service_upgrade_error() from exc
            raise
        result.update(data=self.read_all(), session=self.client.call('get_session', session_id=session_id))
        progress(100, '已替换该 PGM 的人工台本定位；完整画面、原始声音和源时码保留。')
        return result


def _submit(panel, title, action, select_session=False):
    def operation(progress, cancelled):
        client, production_id = panel.ensure_workflow_context(progress, cancelled)
        workflow = BatchWorkflow(client, production_id, getattr(client, 'actor', 'local'))
        result = action(workflow, progress, cancelled)
        result.update(client=client, production_id=production_id)
        return result
    def success(result):
        panel.apply_workflow_context(result['client'], result['production_id'])
        if select_session and result.get('session'):
            panel._session = copy.deepcopy(result['session'])
            if callable(getattr(panel, 'materials_ingested', None)):
                panel.materials_ingested(copy.deepcopy(result['session']))
        panel._set_data(result['data'])
        if hasattr(panel, 'status'):
            if 'recognized' in result or result.get('sync_only'):
                if result.get('sync_only'):
                    panel.status.setText(f"声音合板完成：新合板 {result['synced']}，沿用 {result['skipped']}，"
                        f"PGM 原声 {result.get('fallback', 0)}，待处理 {len(result['failures'])}。")
                else:
                    selected_session=panel._session or {}
                    panel.status.setText(str(selected_session.get('name') or '本场')+'台本匹配完成。' + panel.pgm_status_text(selected_session.get('id')))
                panel.workflow_failures = result['failures']
                if result['failures']:
                    if callable(getattr(panel, 'show_workflow_failures', None)):
                        panel.show_workflow_failures(result['failures'])
                    else:
                        details = '\n'.join(str(row['media_path']) + '：' + str(row['error']) for row in result['failures'])
                        panel.status.setText(panel.status.text() + '\n' + details)
            elif result.get('refreshed'):
                panel.status.setText('已刷新。' + panel.pgm_status_text())
            else:
                panel.status.setText('素材已登记，已选中原拍摄场次。' if result.get('existing') else f"已导入 {result.get('count', 0)} 项。")
        panel.workflow_last_result = result
    return panel._run(title, operation, success)


def ingest_pgm(panel, paths, batch_name=''):
    return ingest_materials(panel, paths, batch_name, [], [])


def ingest_materials(panel, pgm_paths, batch_name='', proxies=None, audio_paths=None, session_id=None, expected_revision=None,
                     cut_xml_paths=None, recording_day=None, recording_kind=None, camera_count=None):
    snapshot = copy.deepcopy(panel.capture_current()) if callable(getattr(panel, 'capture_current', None)) else {}
    # Plain immutable input snapshots keep all widget access on the GUI thread.
    paths, proxy_rows, sounds = list(pgm_paths), copy.deepcopy(proxies or []), list(audio_paths or [])
    xmls = list(cut_xml_paths or [])
    return _submit(panel, '登记拍摄素材…', lambda workflow, p, c: workflow.ingest_materials(
        paths, batch_name, proxy_rows, sounds, session_id, expected_revision, snapshot, p, c,
        cut_xml_paths=xmls, recording_day=recording_day, recording_kind=recording_kind,
        camera_count=camera_count), select_session=True)


def import_script(panel, path):
    return _submit(panel, '导入台本…', lambda workflow, p, c: workflow.import_script(path, p, c))


def import_notes(panel, path):
    return _submit(panel, '导入场记…', lambda workflow, p, c: workflow.import_notes(path, p, c))


def refresh_catalogue(panel):
    """Restore the previously configured catalogue without loading an edit."""
    return _submit(panel, '载入台本时间轴…', lambda workflow, p, c: workflow.refresh_review(p, c))


def sync_recording(panel, session_id=None):
    return _submit(panel, '按场次合板原始声音…',
                   lambda workflow, p, c: workflow.sync_recording(session_id, p, c))


def recognize_and_match(panel, session_id=None, require_synced=True, require_prepared=False):
    model = panel.model.currentText() if hasattr(panel, 'model') else 'small'
    language = panel.language.currentData() if hasattr(panel, 'language') else 'auto'
    return _submit(panel, '识别全部 PGM 并匹配台词…',
                   lambda workflow, p, c: workflow.recognize_and_match(p, c, model=model or 'small', language=language or 'auto',
                                                                      require_synced=require_synced, session_id=session_id,
                                                                      require_prepared=require_prepared))
