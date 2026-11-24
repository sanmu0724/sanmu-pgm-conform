"""Shared reference selection and cached-sync-only multicam export workflow."""
from __future__ import annotations

import copy
from datetime import datetime
import errno
import hashlib
import json
import logging
from pathlib import Path
import re
import shutil
import stat
import sys
import time
import uuid

from library_bridge import canonical_path, resolve_local_path, translate_config
from reference_timeline import (ReferenceTimelineError, assemble_reference_xml,
                                build_reference_plan)
from export_diagnostics import blocking_proxy_events, camera_coverage_summary


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), default=str).encode()).hexdigest()


def _file_digest(path):
    return hashlib.sha256(Path(resolve_local_path(path)).read_bytes()).hexdigest()


def _check(cancelled):
    if cancelled():
        raise ReferenceTimelineError('已取消导出。')


_LOG = logging.getLogger(__name__)
_CLEANUP_RETRY_ERRNOS = frozenset((errno.ENOTEMPTY, errno.EBUSY, errno.EEXIST))


def _cleanup_export_stage(stage, parent, owner, original_error=None):
    """Best-effort removal of this export's unpublished, owned directory only.

    Shared volumes can briefly retain directory entries after files close.
    Cleanup must not hide the actual export/cancellation error, nor follow a
    replaced stage path. A published stage is never passed to this function.
    """
    stage, parent = Path(stage), Path(parent)
    issue = None
    for delay in (0., .05, .15):
        if delay:time.sleep(delay)
        try:
            current = stage.lstat()
            if (stage.parent != parent or not re.fullmatch(r'\.reference-[0-9a-f]{32}', stage.name)
                    or not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != owner):
                issue = '临时路径的身份已改变，未执行递归删除：'+str(stage)
                break
        except FileNotFoundError:
            return
        except OSError as exc:
            issue = '导出临时目录暂未清理：'+str(stage)+'；'+str(exc)
            break
        try:
            shutil.rmtree(stage)
            return
        except FileNotFoundError as exc:
            # A disappearing child does not mean the stage itself is gone.
            issue = '导出临时目录暂未清理：'+str(stage)+'；'+str(exc)
        except OSError as exc:
            issue = '导出临时目录暂未清理：'+str(stage)+'；'+str(exc)
            if exc.errno not in _CLEANUP_RETRY_ERRNOS:break
    if issue:
        _LOG.warning(issue, exc_info=True)
        if original_error is not None and hasattr(original_error, 'add_note'):
            original_error.add_note(issue)


def _publish_export_stage(stage, final):
    """Keep publication on the destination volume without replacing output."""
    try:
        final.lstat()
    except FileNotFoundError:
        pass
    else:
        raise ReferenceTimelineError('导出目标已存在，未覆盖原文件，请重新导出：'+str(final))
    stage.rename(final)


def _proxy_coverage_error(session_name, events, config):
    """Keep a readable coverage report outside the rolled-back export stage."""
    fallback = '本场代理缺失、重叠或未覆盖镜头，请先核对：'+session_name
    try:
        from export_diagnostics import build_proxy_coverage_report, proxy_coverage_message
        report = build_proxy_coverage_report(session_name, events, config)
        message = proxy_coverage_message(session_name, events, config)
    except Exception:
        _LOG.exception('未能建立代理覆盖诊断：%s', session_name)
        return ReferenceTimelineError(fallback)
    try:
        folder = Path.home()/'Library'/'Logs'/'三木的PGM回套软件'/'导出诊断'
        folder.mkdir(parents=True, exist_ok=True)
        target = folder/f'proxy_coverage_{datetime.now():%Y%m%d_%H%M%S_%f}_{uuid.uuid4().hex}.json'
        payload = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
        with target.open('x', encoding='utf-8') as handle:
            handle.write(payload)
    except (OSError, TypeError, ValueError):
        _LOG.warning('未能保存代理覆盖诊断文件；完整报告：%r', report, exc_info=True)
        message += '\n完整诊断未能写入本机日志目录，请查看运行日志。'
    else:
        message += '\n完整诊断：'+str(target)
    return ReferenceTimelineError(message)


def _paths(config):
    paths = set()
    def visit(value, key=''):
        if isinstance(value, dict):
            for field, child in value.items():
                visit(child, field)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child, key)
        elif isinstance(value, str) and value and key in (
                'path', 'proxy_path', 'original_path', 'pgm', 'cut_xml', 'cut_xml_paths'):
            paths.add(value)
    visit(config)
    return sorted(paths)


def session_fingerprint(data, session_id):
    """Hash this recording's saved decisions and lightweight media signatures.

    No media decoding or large media hashing occurs. Adding another recording
    or selecting alternatives never invalidates a previously exported base.
    """
    from library_correspondence import filter_active_transcripts
    from sound_fallback import reference_provenance_config
    current = filter_active_transcripts(data)
    session = next((row for row in current.get('sessions', []) if row.get('id') == session_id), None)
    if not session:
        return ''
    config = {key: value for key, value in reference_provenance_config(session.get('config', {})).items()
              if key not in ('library_context', 'dialogue_markers', 'output_dir', 'library_asr','library_script_run')}
    files = []
    for path in _paths(config):
        try:
            stat = Path(resolve_local_path(path)).stat()
            signature = [stat.st_size, stat.st_mtime_ns]
        except OSError:
            signature = None
        files.append([path, signature])
    transcripts = {row.get('id'): row for row in current.get('transcripts', [])}
    anchors = []
    for row in current.get('candidates', []):
        source = transcripts.get(row.get('transcript_id')) or row
        if (source.get('session_id') or row.get('session_id')) != session_id:
            continue
        anchors.append({key: row.get(key) for key in (
            'id', 'script_line_id', 'transcript_id', 'status', 'script_text', 'script_speaker',
            'script_scene', 'note', 'tags', 'score', 'auto_review_score')} | {key: source.get(key) for key in (
                'media_path', 'start_seconds', 'end_seconds', 'text')})
    anchors.sort(key=lambda row: str(row['id']))
    from pgm_takes import PgmTakeIndex
    index = PgmTakeIndex(current, prepared=True)
    spoken_evidence = []
    for key, rows in sorted(index.rows.items()):
        if key[0] != session_id:
            continue
        spoken_evidence.extend({field: row.get(field) for field in (
            'id', 'media_path', 'start_seconds', 'end_seconds', 'text', 'data')}
            for row in rows)
    return _digest(dict(camera_coverage_version=1, session_id=session_id, name=session.get('name'), config=config,
                        segments=session.get('segments', []), files=files, anchors=anchors,
                        spoken_evidence=spoken_evidence))


def technical_fingerprint(data, session_id):
    """Guard a manual export without coupling it to script or ASR decisions."""
    from sound_fallback import reference_provenance_config
    session = next((row for row in data.get('sessions', []) if row.get('id') == session_id), None)
    if session is None:
        return ''
    config = {key: value for key, value in reference_provenance_config(session.get('config', {})).items()
              if key not in ('library_context', 'dialogue_markers', 'output_dir', 'library_asr',
                             'library_script_run', 'library_pgm_editor', 'library_baseline_alignment_asr')}
    files = []
    for path in _paths(config):
        try:
            stat = Path(resolve_local_path(path)).stat()
            signature = [stat.st_size, stat.st_mtime_ns]
        except OSError:
            signature = None
        files.append([path, signature])
    return _digest(dict(camera_coverage_version=1, session_id=session_id, name=session.get('name'), config=config,
                        segments=session.get('segments', []), files=files))



def decorate_reference_state(data, state=None):
    """Return current shared settings with stale base exports visibly invalid."""
    state = copy.deepcopy(state if state is not None else data.get('reference_state') or {})
    baseline = state.get('baseline_export') or {}
    if not baseline:
        state['baseline_export'] = {}
        return state
    baseline = copy.deepcopy(baseline)
    reason = ''
    if not baseline.get('valid'):
        reason = baseline.get('invalid_reason') or '请先导出基准多机位 XML。'
    elif baseline.get('fingerprint') != (technical_fingerprint if baseline.get('fingerprint_kind') == 'technical'
                                       else session_fingerprint)(data, state.get('reference_session_id')):
        reason = '基准场次素材或同步设置已改变，请重新导出基准多机位 XML。'
    else:
        for path_key, hash_key in (('xml_path', 'xml_sha256'), ('map_path', 'map_sha256')):
            try:
                if not baseline.get(path_key) or _file_digest(baseline[path_key]) != baseline.get(hash_key):
                    reason = '基准导出文件已移动、缺失或被修改，请重新导出。'
                    break
            except OSError:
                reason = '基准导出文件已移动或缺失，请重新导出。'
                break
    baseline['valid'] = not bool(reason)
    if reason:
        baseline['invalid_reason'] = reason
    else:
        baseline.pop('invalid_reason', None)
    state['baseline_export'] = baseline
    return state


class ReferenceWorkflow:
    def __init__(self, client, production_id, actor='local'):
        self.client, self.production_id, self.actor = client, production_id, actor

    def read_all(self):
        from library_workflow import BatchWorkflow
        return BatchWorkflow(self.client, self.production_id, self.actor).read_all()

    def read_manual_data(self):
        """Only technical scene metadata is needed for manual editing/export."""
        return dict(sessions=self.client.call('list_sessions', production_id=self.production_id),
            reference_state=self.client.call('get_reference_settings', production_id=self.production_id),
            scripts=[], transcripts=[], candidates=[])

    def _save(self, state, reference_session_id=None, pairs=None, baseline=None, mapping=None):
        return self.client.call('update_reference_settings', production_id=self.production_id,
            reference_session_id=reference_session_id or state.get('reference_session_id'),
            selected_clip_pairs=state.get('selected_clip_pairs', {}) if pairs is None else pairs,
            baseline_export=state.get('baseline_export', {}) if baseline is None else baseline,
            expected_revision=state.get('revision', 0), baseline_script_map=mapping)

    def prepare_manual_timeline(self):
        """Read persisted script organisation alongside technical metadata.

        Reuse saved coverage and reviews without recognition or rematching.
        Actual timeline placement/export remains independent of script rows.
        """
        from manual_timeline import inventory, reconcile_layout, attach_script_organizer
        data = self.read_manual_data()
        data.update(scripts=self.client.call('list_script', production_id=self.production_id),
                    transcripts=self.client.call('list_transcripts', production_id=self.production_id),
                    candidates=self.client.call('list_candidates', production_id=self.production_id))
        state = data['reference_state']
        reference = state.get('reference_session_id')
        rows = attach_script_organizer(data, inventory(data))
        layout = reconcile_layout(state.get('manual_timeline') or {}, rows, reference)
        base_rows = [row for row in rows if row['session_id'] == reference]
        cursor, blocks, warnings = 0, [], []
        known = bool(base_rows and all(row.get('cut_ready') for row in base_rows))
        if known:
            for row in sorted(base_rows, key=lambda row: row['record_frame_in']):
                first, last = row['record_frame_in'], row['record_frame_out_exclusive']
                if first != cursor or last-first != row['frames'] or row['fps'] != base_rows[0]['fps']:
                    known = False
                    break
                blocks.append(dict(path=row['pgm_path'], start=first, end=last, frames=row['frames']))
                cursor = last
        cut = dict(fps=base_rows[0]['fps'], duration=cursor, pgm_sources=blocks) if known else None
        if base_rows and not known:
            warnings.append('基准PGM的切点顺序或时长信息不完整，请重新登记本场切点XML；整理位置仍可保存。')
        return dict(data=data, state=state, manual_inventory=rows,
                    manual_cut=cut, manual_timeline=layout, warnings=warnings)

    def save_manual_timeline(self, layout, expected_revision=None):
        state = self.client.call('get_reference_settings', production_id=self.production_id)
        if expected_revision is not None and expected_revision != state.get('revision'):
            raise ReferenceTimelineError('编排已更新，请重新打开时间轴后保存。')
        saved = self.client.call('update_manual_timeline', production_id=self.production_id,
            manual_timeline=layout, expected_revision=state.get('revision', 0))
        return dict(state=saved, manual_timeline=saved.get('manual_timeline') or {})

    def _automatic_context(self, layout=None, expected_revision=None, *, model='small',
                           progress=lambda n,m:None, cancelled=lambda:False, decode_missing=True):
        """Prepare isolated baseline speech without rematching the organizer."""
        from alignment_speech import ensure_baseline_speech
        from manual_timeline import reconcile_layout, validate_layout
        _check(cancelled)
        prepared=self.prepare_manual_timeline();state=prepared['state']
        if expected_revision is not None and state.get('revision')!=expected_revision:
            raise ReferenceTimelineError('编排或基准已改变，请重新打开后操作。')
        reference=state.get('reference_session_id')
        if not reference:raise ReferenceTimelineError('请先选择基准通条。')
        if not prepared['data'].get('scripts'):raise ReferenceTimelineError('请先导入台本并核对备选的台本区间。')
        cut=prepared.get('manual_cut') or self._baseline_cut(prepared['data'],reference)
        original=validate_layout(reconcile_layout(layout if layout is not None else prepared['manual_timeline'],
            prepared['manual_inventory'],reference),prepared['manual_inventory'],reference)
        speech=ensure_baseline_speech(self.client,self.production_id,prepared['data'],reference,cut,model=model,
            progress=lambda n,m:progress(round(n*.60),m),cancelled=cancelled,decode_missing=decode_missing)
        _check(cancelled)
        # Per-PGM speech caches may advance scene revisions, never the layout
        # CAS. Re-read the raw catalogue without expensive browser enrichment.
        fresh=self.prepare_manual_timeline()
        if (fresh['state'].get('revision')!=state.get('revision') or
                fresh['state'].get('reference_session_id')!=reference):
            raise ReferenceTimelineError('识别期间基准或编排已改变；转写缓存保留，未覆盖新编排。')
        view=ensure_baseline_speech(self.client,self.production_id,fresh['data'],reference,cut,model=model,
            cancelled=cancelled,decode_missing=False)
        original=validate_layout(reconcile_layout(original,fresh['manual_inventory'],reference),
            fresh['manual_inventory'],reference)
        fresh.update(manual_cut=cut,baseline_cut=cut,manual_timeline=original)
        return fresh,view['data'],speech

    def prepare_automatic_alignment(self, layout=None, expected_revision=None, *, model='small',
            replace_automatic=False, rebuild=False, apply=True, progress=lambda n,m:None, cancelled=lambda:False):
        """Baseline speech→script mapping→reviewable blue actual placements.

        The saved organizer and its green choices are never rematched. Both
        mapping and layout commit atomically after the cancellable calculation.
        """
        from automatic_alignment import build_alignment_proposals
        from manual_timeline import apply_alignment_proposals
        fresh,evidence,speech=self._automatic_context(layout,expected_revision,model=model,
            progress=progress,cancelled=cancelled)
        state=fresh['state'];reference=state['reference_session_id']
        proposals=build_alignment_proposals(evidence,fresh['manual_inventory'],fresh['manual_timeline'],fresh['manual_cut'],
            mapping=None if rebuild else state.get('baseline_script_map'),replace_automatic=replace_automatic,
            progress=lambda n,m:progress(60+round(n*.35),m),cancelled=cancelled)
        _check(cancelled)
        updated=apply_alignment_proposals(fresh['manual_timeline'],proposals['proposals'],fresh['manual_inventory'],
            reference,replace_automatic=replace_automatic) if apply else fresh['manual_timeline']
        mapping=proposals.get('mapping') or {}
        if not mapping:raise ReferenceTimelineError('未能建立基准台本映射，请核对基准与台本。')
        progress(96,'保存基准映射与初排位置…')
        saved=self.client.call('update_manual_timeline',production_id=self.production_id,manual_timeline=updated,
            expected_revision=state['revision'],baseline_script_map=mapping)
        # Do not report cancellation after a successful atomic commit.
        fresh.update(state=saved,manual_timeline=saved['manual_timeline'],data=proposals['evidence_data'],
            baseline_script_map=mapping,automatic_alignment=proposals)
        fresh['data']['reference_state']=saved
        report=dict(proposals.get('summary') or {})
        report.update(placed_count=len(proposals['proposals']) if apply else 0,
            unresolved=proposals.get('unresolved') or [],speech=speech | {'data':None},mapping_only=not apply)
        fresh['alignment_report']=report
        progress(100,'基准映射已建立。' if not apply else '自动初排已保存；蓝色片段请手动调整或确认。')
        return fresh

    def prepare_automatic_map(self, layout=None, expected_revision=None, **kwargs):
        return self.prepare_automatic_alignment(layout,expected_revision,apply=False,**kwargs)

    def save_automatic_map(self, mapping, layout=None, expected_revision=None, *, model='small',
                           progress=lambda n,m:None,cancelled=lambda:False, **_ignored):
        """Save a human-adjusted partition without moving any existing clip."""
        from automatic_alignment import prepare_alignment_evidence
        from baseline_script_map import validate_map
        fresh,data,speech=self._automatic_context(layout,expected_revision,model=model,
            progress=progress,cancelled=cancelled,decode_missing=False)
        state=fresh['state'];reference=state['reference_session_id']
        prepared=prepare_alignment_evidence(data,reference,mapping=mapping,
            progress=lambda n,m:progress(60+round(n*.30),m),cancelled=cancelled)
        mapping=validate_map(mapping,prepared['evidence_data'],fresh['manual_cut'],reference)
        _check(cancelled)
        saved=self.client.call('update_manual_timeline',production_id=self.production_id,
            manual_timeline=fresh['manual_timeline'],expected_revision=state['revision'],baseline_script_map=mapping)
        fresh.update(state=saved,manual_timeline=saved['manual_timeline'],data=prepared['evidence_data'],
            baseline_script_map=mapping,alignment_report=dict(placed_count=0,preserved_count=0,unresolved=[],
                speech=speech | {'data':None},mapping_only=True))
        fresh['data']['reference_state']=saved
        progress(100,'映射已保存，现有位置保留；可重排未确认的自动片段。')
        return fresh

    def _baseline_cut(self, data, reference):
        """Read cut structure only; mapping never decodes or re-recognizes media."""
        from cut_xml import timeline_info
        from reference_timeline import _cut
        session = next((row for row in data.get('sessions', []) if row['id'] == reference), None)
        if not session:
            raise ReferenceTimelineError('请先选择基准通条。')
        baseline = data.get('reference_state', {}).get('baseline_export') or {}
        self._continuous(session, baseline.get('legacy_continuous_confirmed', False))
        config = copy.deepcopy(session.get('config') or {})
        config['library_context'] = dict(production_id=self.production_id, session_id=reference)
        config = translate_config(config)
        cut = timeline_info(config)
        if config.get('cut_xml_digest') and config['cut_xml_digest'] != cut.get('xml_digest'):
            raise ReferenceTimelineError('基准切点 XML 已改变，请重新导入并核对机位。')
        cut = copy.deepcopy(cut)
        for block in cut.get('pgm_sources', []):
            block['path'] = canonical_path(block['path'])
        return _cut({reference: cut}, reference)

    def prepare_baseline_map(self, rebuild=False):
        from baseline_script_map import (baseline_map_fingerprint, build_baseline_map,
                                         validate_map, script_units, units_signature)
        data = self.read_all()
        state = data['reference_state']
        reference = state.get('reference_session_id')
        cut = self._baseline_cut(data, reference)
        fingerprint = baseline_map_fingerprint(data, reference, cut)
        saved = state.get('baseline_script_map') or {}
        current = bool(saved and saved.get('evidence_fingerprint') == fingerprint)
        requested_units_signature = units_signature(script_units(data, reference))
        if current:
            try:
                saved = validate_map(saved, data, cut, reference)
            except ValueError:
                current = False
        if current and not rebuild and saved.get('units_signature') == requested_units_signature:
            mapping = saved
        else:
            if current:
                from baseline_script_map import refine_baseline_map
                mapping = refine_baseline_map(saved, data, reference, cut)
            else:
                mapping = build_baseline_map(data, reference, cut)
            # Old checkbox pairs no longer drive inclusion or baseline anchors.
            state = self._save(state, pairs={}, mapping=mapping)
            data['reference_state'] = state
            for row in data.get('pgm_match_versions', []):
                row['reference_revision'] = state['revision']
        return dict(data=data, state=state, baseline_script_map=mapping, baseline_cut=cut,
                    units_changed=mapping.get('units_signature') != requested_units_signature)

    def save_baseline_map(self, mapping, expected_revision=None):
        from baseline_script_map import baseline_map_fingerprint, validate_map
        data = self.read_all()
        state = data['reference_state']
        if expected_revision is not None and expected_revision != state.get('revision'):
            raise ReferenceTimelineError('基准映射已更新，请刷新后重新保存。')
        reference = state.get('reference_session_id')
        cut = self._baseline_cut(data, reference)
        mapping = validate_map(mapping, data, cut, reference)
        if mapping.get('evidence_fingerprint') != baseline_map_fingerprint(data, reference, cut):
            raise ReferenceTimelineError('台本、有效转录或基准切点已改变，请重新建立基准映射。')
        previous = {row['id']: row for row in (state.get('baseline_script_map') or {}).get('segments', [])}
        fields = ('start_frame', 'end_frame', 'line_ids', 'anchor_frame', 'needs_review')
        for row in mapping['segments']:
            old = previous.get(row['id'])
            if old is None or any(row.get(key) != old.get(key) for key in fields):
                row['manual'] = True
        state = self._save(state, pairs={}, mapping=mapping)
        data['reference_state'] = state
        for row in data.get('pgm_match_versions', []):
            row['reference_revision'] = state['revision']
        return dict(data=data, state=state, baseline_script_map=mapping, baseline_cut=cut)

    @staticmethod
    def _continuous(session, legacy_continuous_confirmed=False):
        config = session.get('config', {})
        kind = config.get('recording_kind')
        if config.get('library_record_type') == 'notes' or kind in ('coverage', '分镜头', '分镜'):
            raise ReferenceTimelineError('基准必须选择通条拍摄场次，不能选择分镜头或场记记录。')
        if kind not in ('continuous', '通条') and not legacy_continuous_confirmed:
            raise ReferenceTimelineError('旧场次没有拍摄类型，请明确确认它是完整通条。')

    def set_reference(self, session_id, legacy_continuous_confirmed=False, manual=False):
        if manual:
            data = self.read_manual_data()
            session = next((row for row in data['sessions'] if row['id'] == session_id), None)
            if session is None:
                raise ReferenceTimelineError('基准场次不属于当前项目。')
            self._continuous(session, legacy_continuous_confirmed)
            state = data['reference_state']
            legacy = session.get('config', {}).get('recording_kind') not in ('continuous', '通条')
            if state.get('reference_session_id') != session_id:
                state = self._save(state, reference_session_id=session_id, pairs={},
                    baseline=dict(valid=False, legacy_continuous_confirmed=True) if legacy else {})
            elif legacy and legacy_continuous_confirmed and not state.get('baseline_export', {}).get('legacy_continuous_confirmed'):
                state = self._save(state, pairs={}, baseline=dict(state.get('baseline_export') or {},
                                                                legacy_continuous_confirmed=True))
            data['reference_state'] = state
            return dict(data=data, state=state)
        data = self.read_all()
        session = next((row for row in data['sessions'] if row['id'] == session_id), None)
        if session is None:
            raise ReferenceTimelineError('基准场次不属于当前剧目。')
        self._continuous(session, legacy_continuous_confirmed)
        state = data['reference_state']
        if state.get('reference_session_id') != session_id:
            legacy = session.get('config', {}).get('recording_kind') not in ('continuous', '通条')
            initial = dict(valid=False, legacy_continuous_confirmed=True) if legacy else {}
            candidates = {row['id']: row for row in data.get('candidates', [])
                          if row.get('status') in ('candidate', 'confirmed') and row.get('script_active', True)}
            transcripts = {row['id']: row for row in data.get('transcripts', [])}
            def session_of(row):
                return (transcripts.get(row.get('transcript_id')) or row).get('session_id')
            from pgm_takes import PgmTakeIndex
            take_index = PgmTakeIndex(data, prepared=True)
            pairs = {}
            for identity in state.get('selected_clip_pairs', {}):
                chosen = candidates.get(identity)
                if not chosen or session_of(chosen) == session_id:
                    continue
                try:
                    chosen = take_index.selection_candidate(chosen)
                except ValueError:
                    pairs[identity] = None
                    continue
                identity = chosen['id']
                options = [row['id'] for row in candidates.values() if session_of(row) == session_id
                           and take_index.association_ready(row)
                           and row.get('script_line_id') == chosen.get('script_line_id')]
                pairs[identity] = options[0] if len(options) == 1 else None
            state = self._save(state, reference_session_id=session_id, pairs=pairs, baseline=initial)
        elif (legacy_continuous_confirmed and
              session.get('config', {}).get('recording_kind') not in ('continuous', '通条') and
              not state.get('baseline_export', {}).get('legacy_continuous_confirmed')):
            baseline = dict(state.get('baseline_export') or {}, legacy_continuous_confirmed=True)
            state = self._save(state, baseline=baseline)
        return dict(data=self.read_all(), state=state)

    def select_clip(self, candidate_id, checked, baseline_candidate_id=None, expected_revision=None):
        data = self.read_all()
        state = data['reference_state']
        if expected_revision is not None and expected_revision != state.get('revision'):
            raise ReferenceTimelineError('基准备选设置已被其他人更新，请刷新后重新选择。')
        reference = state.get('reference_session_id')
        from pgm_takes import PgmTakeIndex
        index = PgmTakeIndex(data, prepared=True)
        pairs = dict(state.get('selected_clip_pairs') or {})
        requested = index.candidates.get(candidate_id)
        if not requested:
            raise ReferenceTimelineError('备选片段尚未核对或对应关系已失效。')
        key = index.key(requested)
        for identity in list(pairs):
            if index.key(index.candidates.get(identity, {})) == key:
                pairs.pop(identity)
        if checked:
            if requested.get('status') not in ('candidate', 'confirmed'):
                raise ReferenceTimelineError('备选片段尚未核对或对应关系已失效。')
            try:
                chosen = index.selection_candidate(requested)
            except ValueError as exc:
                raise ReferenceTimelineError(str(exc)) from exc
            if reference and key[0] == reference:
                raise ReferenceTimelineError('基准通条已保留在 V1，请选择其他场次的备选。')
            options = [row for row in index.candidates.values() if index.association_ready(row)
                       and index.key(row)[0] == reference and row.get('script_line_id') == chosen.get('script_line_id')]
            if requested['script_line_id'] != chosen['script_line_id'] and baseline_candidate_id not in {row['id'] for row in options}:
                baseline_candidate_id = None
            if baseline_candidate_id:
                if baseline_candidate_id not in {row['id'] for row in options}:
                    raise ReferenceTimelineError('指定基准片段与备选第一句台词不对应，或对应关系已失效。')
            elif len(options) == 1:
                baseline_candidate_id = options[0]['id']
            else:
                baseline_candidate_id = None
            pairs[chosen['id']] = baseline_candidate_id
        state = self._save(state, pairs=pairs)
        return dict(data=self.read_all(), state=state)

    def confirm_and_select_clip(self, candidate_id, baseline_candidate_id=None, expected_revision=None):
        """One explicit review action accepts the whole take and its first anchor.

        Missing first dialogue still requires manual script assignment; this
        operation never substitutes a later sentence. Candidate and reference
        revisions remain checked independently against concurrent editors.
        Baseline requests confirm only their selected utterance and do not
        create a whole-file override or an alternative selection.
        """
        data = self.read_all()
        state = data['reference_state']
        if expected_revision is not None and expected_revision != state.get('revision'):
            raise ReferenceTimelineError('基准备选设置已被其他人更新，请刷新后重新选择。')
        from pgm_takes import PgmTakeIndex, MANUAL_PGM_TAG
        index = PgmTakeIndex(data, prepared=True)
        requested = index.candidates.get(candidate_id)
        if not requested or requested.get('status') not in ('candidate', 'confirmed'):
            raise ReferenceTimelineError('备选片段尚未核对或对应关系已失效。')
        if index.key(requested)[0] == state.get('reference_session_id'):
            # The baseline script browser displays individual dialogue anchors.
            # Confirm exactly the selected utterance, including later sentences;
            # this is not a whole-file alternative or first-dialogue decision.
            competitors = index._by_transcript.get(requested.get('transcript_id'), [])
            if any(row['id'] != requested['id'] and row.get('status') == 'confirmed' and
                   not set(row.get('tags') or []).intersection(('auto_dialogue', 'automatic_dialogue')) and
                   row.get('script_line_id') != requested.get('script_line_id') for row in competitors):
                raise ReferenceTimelineError('该基准台词区间已有不同的人工归属，请先核对唯一位置。')
            tags = [tag for tag in requested.get('tags', []) if tag not in ('auto_dialogue', 'automatic_dialogue')]
            self.client.call('update_candidate', candidate_id=requested['id'], status='confirmed',
                             note=requested.get('note', ''), tags=tags, expected_revision=requested.get('revision'))
            latest = self.read_all()
            return dict(data=latest, state=latest['reference_state'])
        first = index.first_choices(requested)
        if requested['id'] in {row['id'] for row in first}:
            chosen = requested
        else:
            chosen = index._winner(first)
        if chosen is None:
            raise ReferenceTimelineError('PGM 第一句台词尚未找到唯一对应位置，请先人工定位首句。')
        other_human = [row for row in first if row['id'] != chosen['id'] and
                       row.get('status') == 'confirmed' and
                       not set(row.get('tags') or []).intersection(('auto_dialogue', 'automatic_dialogue')) and
                       row.get('script_line_id') != chosen.get('script_line_id')]
        if other_human:
            raise ReferenceTimelineError('PGM 第一句台词已有不同的人工归属，请先核对唯一位置。')
        tags = [tag for tag in chosen.get('tags', []) if tag not in ('auto_dialogue', 'automatic_dialogue')]
        tags = list(dict.fromkeys(tags+[MANUAL_PGM_TAG]))
        self.client.call('update_candidate', candidate_id=chosen['id'], status='confirmed',
                         note=chosen.get('note', ''), tags=tags, expected_revision=chosen.get('revision'))
        return self.select_clip(chosen['id'], True, baseline_candidate_id,
                                expected_revision=state.get('revision'))

    def _prepare(self, session, data):
        from core import Segment
        from camera_schema import cameras_for
        from cut_xml import timeline_info
        config = copy.deepcopy(session.get('config', {}))
        try:
            cameras = cameras_for(config)
        except ValueError as exc:
            raise ReferenceTimelineError(str(exc)+'：'+session['name']) from exc
        if not config.get('cut_xml_paths') and not config.get('cut_xml'):
            raise ReferenceTimelineError('请先导入本场所有 PGM 分割后的 XML：'+session['name'])
        config.update(external_sound=True, multicam=True, library_sound_cache_only=True,
                      missing_camera_policy='available', missing_camera_preference='A',
                      empty_camera_policy='pgm')
        config['library_context'] = dict(production_id=self.production_id, session_id=session['id'],
            session_name=session['name'], revision=session.get('revision'))
        config['dialogue_markers'] = [copy.deepcopy(row) for row in data.get('candidates', [])
                                     if row.get('session_id') == session['id'] and row.get('status') == 'confirmed']
        registered = {row.get('path'): row.get('id') for row in config.get('library_media', [])
                      if row.get('kind') == 'proxy'}
        for source in config.get('sources', []):
            path = source.get('proxy_path') or source.get('path')
            # Legacy projects gain a deterministic identity, never a new random
            # number at every export. New imports already persist source UUIDs.
            if not source.get('source_id'):
                source['source_id'] = registered.get(path) or str(uuid.uuid5(uuid.NAMESPACE_URL,
                    f'pgm-source:{self.production_id}:{session["id"]}:{path}'))
        config = translate_config(config)
        cut = timeline_info(config)
        if config.get('cut_xml_digest') and config['cut_xml_digest'] != cut.get('xml_digest'):
            raise ReferenceTimelineError('切点 XML 已改变，请重新导入并核对机位：'+session['name'])
        segments, cursor = [], 0
        for row in session.get('segments', []):
            try:
                segment = Segment(**{key: row[key] for key in ('start', 'end', 'camera', 'reason')})
            except (KeyError, TypeError) as exc:
                raise ReferenceTimelineError('机位识别结果无效，请重新核对：'+session['name']) from exc
            if (segment.start != cursor or segment.end <= segment.start or
                    not isinstance(segment.camera, str) or len(segment.camera) != 1 or segment.camera not in (*cameras, '?')
                    or segment.reason == 'identity_unconfirmed'):
                raise ReferenceTimelineError('本场机位识别尚未完成或有待核对镜头：'+session['name'])
            segments.append(segment)
            cursor = segment.end
        if cursor != cut['duration']:
            raise ReferenceTimelineError('本场机位识别没有覆盖完整切点时间线，请先完成并保存识别结果：'+session['name'])
        if not config.get('calibration_confirmed'):
            raise ReferenceTimelineError('请先核对本场代理与 PGM 的同步补偿并保存：'+session['name'])
        canonical_cut = copy.deepcopy(cut)
        for block in canonical_cut['pgm_sources']:
            block['path'] = canonical_path(block['path'])
        from workflow_readiness import ensure_session_prepared, WorkflowReadinessError
        try:
            ensure_session_prepared(session, cut=canonical_cut)
        except WorkflowReadinessError as exc:
            raise ReferenceTimelineError(str(exc)) from exc
        return config, segments, canonical_cut

    @staticmethod
    def _merge_maps(paths, target):
        from proxy_workflow import normalize_guide_audio,original_wav_guide_audio,normalize_video_fallbacks
        records, by_path, by_identity, fps, guide_audio,video_fallbacks = [], {}, {}, set(), [],[]
        merged_audio,merged_by_path,required_media_directories=[],{},[]
        for path in paths:
            source = json.loads(Path(resolve_local_path(str(path))).read_text(encoding='utf-8'))
            fps.add(int(source['fps']))
            guide_audio.extend(normalize_guide_audio(source.get('guide_audio', [])))
            video_fallbacks.extend(normalize_video_fallbacks(source.get('video_fallbacks',[])))
            for directory in source.get('required_media_directories',[]):
                if directory not in required_media_directories:required_media_directories.append(directory)
            for row in source.get('merged_audio_sources',[]):
                key=(row.get('path'),row.get('session_id',''),row.get('production_id',''))
                if key in merged_by_path and merged_by_path[key]!=row:
                    raise ReferenceTimelineError('合并立体声 WAV 对应表冲突：'+str(key[0]))
                if key not in merged_by_path:
                    merged_by_path[key]=row;merged_audio.append(copy.deepcopy(row))
            for row in source.get('records', []):
                path_key, identity = row.get('proxy_path'), row.get('source_id')
                if path_key in by_path and by_path[path_key] != row:
                    raise ReferenceTimelineError('跨场代理路径对应表冲突：'+str(path_key))
                if identity and identity in by_identity and by_identity[identity] != row:
                    raise ReferenceTimelineError('跨场素材唯一编号对应表冲突：'+str(identity))
                if path_key not in by_path:
                    records.append(copy.deepcopy(row))
                    by_path[path_key] = row
                if identity:
                    by_identity[identity] = row
        if len(fps) != 1:
            raise ReferenceTimelineError('跨场代理对应表帧率不一致。')
        Path(target).write_text(json.dumps(dict(version=1, fps=next(iter(fps)), records=records,
                                               guide_audio=original_wav_guide_audio(guide_audio),
                                               video_fallbacks=normalize_video_fallbacks(video_fallbacks),
                                               merged_audio_sources=merged_audio,
                                               required_media_directories=required_media_directories),
                                            ensure_ascii=False, indent=2), encoding='utf-8')

    def _export_manual_reference(self, output_dir, include_alternatives=False,
                                 progress=lambda n,m: None, cancelled=lambda: False,
                                 expected_revision=None, include_baseline=True):
        from core import export
        from manual_timeline import inventory, reconcile_layout, validate_layout, build_manual_plan
        data = self.read_manual_data()
        state = data['reference_state']
        if expected_revision is not None and expected_revision != state.get('revision'):
            raise ReferenceTimelineError('时间轴编排已更新，请刷新后重新导出。')
        reference = state.get('reference_session_id')
        sessions = {row['id']: row for row in data['sessions']}
        if not reference or reference not in sessions:
            raise ReferenceTimelineError('导出前请先确定基准通条。')
        if not include_baseline and not include_alternatives:
            raise ReferenceTimelineError('请选择基准、备选或基准＋备选导出模式。')
        baseline = state.get('baseline_export') or {}
        self._continuous(sessions[reference], baseline.get('legacy_continuous_confirmed', False))
        rows = inventory(data)
        layout = reconcile_layout(state.get('manual_timeline') or {}, rows, reference)
        placed = [row for row in layout['items'].values() if row.get('start_seconds') is not None
                  and not row.get('hidden') and row['session_id'] != reference] if include_alternatives else []
        if not include_baseline and not placed:
            raise ReferenceTimelineError('基准时间轴上还没有摆放备选素材。')
        involved = [reference]+list(dict.fromkeys(row['session_id'] for row in placed))
        _check(cancelled)
        prepared = {}
        for index, identity in enumerate(involved):
            progress(round(index/max(1, len(involved))*10), '核对机位与声音缓存：'+sessions[identity]['name'])
            _check(cancelled)
            prepared[identity] = self._prepare(sessions[identity], data)
        fingerprints = {identity: technical_fingerprint(data, identity) for identity in involved}
        # An unavailable, staged or unused alternative cannot block a baseline
        # export. Organizer geometry does not control the actual edit either.
        export_layout = dict(version=1, baseline_session_id=reference, items={
            key:dict(copy.deepcopy(row), organize_lane=number+1, organize_seconds=0.)
            for number,(key,row) in enumerate(layout['items'].items())
            if include_alternatives and row.get('start_seconds') is not None
               and not row.get('hidden') and row['session_id'] != reference})
        plan = build_manual_plan(data, reference, export_layout,
                                 {identity: row[2] for identity, row in prepared.items()})
        output_dir = Path(output_dir).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r'[^\w\-\u4e00-\u9fff]', '_', sessions[reference]['name'])[:60]
        label = ('基准及备选' if include_baseline else '仅备选') if include_alternatives else '基准'
        final = output_dir/f'{safe_name}_{label}_{datetime.now():%Y%m%d_%H%M%S_%f}'
        stage = output_dir/('.reference-'+uuid.uuid4().hex)
        stage.mkdir()
        created = stage.lstat()
        stage_owner = (created.st_dev, created.st_ino)
        published = False
        try:
            xmls, maps, coverage_reports = {}, [], []
            for index, identity in enumerate(involved):
                _check(cancelled)
                config, segments, _ = prepared[identity]
                # This required derived media outlives the staging tree, whose
                # directory name changes when the XML export is published.
                config = dict(config, stereo_audio_cache_dir=str(output_dir/'配对立体声WAV'))
                # Script annotations are optional archival data, not placement
                # evidence. Never run ASR, matching or waveform sync on export.
                config['dialogue_markers'] = []
                def per_file(value, message):
                    progress(10+round((index+max(0,min(100,value))/100)/len(involved)*65),
                             sessions[identity]['name']+'：'+message)
                events, path = export(config, segments, stage/'sessions'/identity, per_file, cancelled)
                if blocking_proxy_events(events, config):
                    raise _proxy_coverage_error(sessions[identity]['name'], events, config)
                coverage_reports.append(camera_coverage_summary(sessions[identity]['name'], events, config))
                xmls[identity] = path
                if include_baseline or identity != reference:
                    maps.append(path.parent/'proxy_original_map.json')
            _check(cancelled)
            xml_path = stage/f'{safe_name}_{label}_MULTICAM.fcpxml'
            progress(80, '按已保存的时间位置组装多机位与声音轨道…')
            report = assemble_reference_xml(xmls[reference], xmls, plan, xml_path,
                title=sessions[reference]['name']+'_'+label, include_baseline=include_baseline)
            map_path = stage/'proxy_original_map.json'
            self._merge_maps(maps, map_path)
            (stage/'camera_coverage_report.json').write_text(json.dumps(
                dict(version=1, sessions=coverage_reports), ensure_ascii=False, indent=2), encoding='utf-8')
            latest = self.read_manual_data()
            if latest['reference_state'].get('revision') != state.get('revision'):
                raise ReferenceTimelineError('导出期间人工编排已更新，请重新导出。')
            if any(technical_fingerprint(latest, identity) != value for identity, value in fingerprints.items()):
                raise ReferenceTimelineError('导出期间机位、代理或声音同步设置已更新，请重新导出。')
            _check(cancelled)
            _publish_export_stage(stage, final)
            published = True
            xml_path, map_path = final/xml_path.name, final/map_path.name
            manifest_path = final/Path(report['manifest_path']).name
            if not include_alternatives:
                new_baseline = dict(valid=True, fingerprint_kind='technical',
                    xml_path=canonical_path(str(xml_path)), map_path=canonical_path(str(map_path)),
                    manifest_path=canonical_path(str(manifest_path)), fingerprint=fingerprints[reference],
                    xml_sha256=_file_digest(str(xml_path)), map_sha256=_file_digest(str(map_path)),
                    duration_frames=plan['baseline_duration_frames'], exported_at=datetime.now().isoformat(timespec='seconds'))
                if baseline.get('legacy_continuous_confirmed'):
                    new_baseline['legacy_continuous_confirmed'] = True
                state = self._save(state, pairs={}, baseline=new_baseline)
            self.client.call('register_artifact', session_id=reference,
                kind=('reference_combined_xml' if include_baseline else 'reference_alternatives_xml')
                     if include_alternatives else 'reference_baseline_xml',
                path=canonical_path(str(xml_path)), manifest=dict(map_path=canonical_path(str(map_path)),
                    manifest_path=canonical_path(str(manifest_path)), selected_count=report['selected_count'],
                    include_baseline=include_baseline, manual_layout=True,
                    reference_fingerprint=fingerprints[reference]))
            progress(100, '已导出'+label+'多机位 XML。')
            result_data = self.read_manual_data()
            return dict(data=result_data, state=result_data['reference_state'], xml_path=str(xml_path),
                map_path=str(map_path), manifest_path=str(manifest_path), output_dir=str(final), plan=plan,
                selected_count=report['selected_count'], piece_count=report['piece_count'])
        except Exception:
            _LOG.exception('多机位导出未完成；%s：%s', '文件已发布' if published else '尚未发布',
                           final if published else stage)
            raise
        finally:
            if not published:
                _cleanup_export_stage(stage, output_dir, stage_owner, sys.exception())

    def export_reference(self, output_dir, include_alternatives=False,
                         progress=lambda n,m: None, cancelled=lambda: False, expected_revision=None,
                         include_baseline=True, resolved_pairs=None, export_snapshot=None, manual=False):
        if manual:
            return self._export_manual_reference(output_dir, include_alternatives, progress, cancelled,
                                                 expected_revision, include_baseline)
        from core import export
        data = self.read_all()
        state = data['reference_state']
        if expected_revision is not None and expected_revision != state.get('revision'):
            raise ReferenceTimelineError('基准备选设置已被其他人更新，请刷新后重新导出。')
        reference = state.get('reference_session_id')
        sessions = {row['id']: row for row in data['sessions']}
        if not reference or reference not in sessions:
            raise ReferenceTimelineError('请先选择基准通条。')
        _check(cancelled)
        baseline = state.get('baseline_export') or {}
        self._continuous(sessions[reference], baseline.get('legacy_continuous_confirmed', False))
        from pgm_takes import PgmTakeIndex
        index = PgmTakeIndex(data, prepared=True)
        pairs = index.export_pairs(reference, state.get('selected_clip_pairs')) if include_alternatives else {}
        inventory_signature = index.export_signature(reference) if include_alternatives else None
        if include_alternatives and export_snapshot is not None and export_snapshot != inventory_signature:
            raise ReferenceTimelineError('轨道上的绿色备选片段已改变，请刷新后重新导出。')
        if not include_baseline and not include_alternatives:
            raise ReferenceTimelineError('请选择需要导出的基准或备选时间线。')
        if not include_baseline and not pairs:
            raise ReferenceTimelineError('轨道上没有已核实的绿色备选片段。')
        if include_alternatives and resolved_pairs is not None:
            if not isinstance(resolved_pairs, dict) or set(resolved_pairs) != set(pairs):
                raise ReferenceTimelineError('轨道上的绿色备选片段已改变，请刷新后重新导出。')
            pairs = resolved_pairs
        selected_sessions = list(dict.fromkeys(index.key(index.candidates[identity])[0] for identity in pairs))
        if any(identity not in sessions for identity in selected_sessions):
            raise ReferenceTimelineError('轨道片段已失效，请刷新后重新导出。')
        involved = [reference]+[identity for identity in selected_sessions if identity != reference]
        prepared = {identity: self._prepare(sessions[identity], data) for identity in involved}
        fingerprints = {identity: session_fingerprint(data, identity) for identity in involved}
        mapping = None
        if include_alternatives and pairs:
            mapped = self.prepare_baseline_map()
            # Preparation can persist a newly built partition. Revisions and
            # inventories must still refer to the same decisions as this job.
            if (PgmTakeIndex(mapped['data'], prepared=True).export_signature(reference) != inventory_signature
                    or any(session_fingerprint(mapped['data'], identity) != value
                           for identity, value in fingerprints.items())):
                raise ReferenceTimelineError('建立基准映射期间素材或台词已更新，请刷新后重试。')
            state = mapped['state']
            data = mapped['data']
            mapping = mapped['baseline_script_map']
        selection = [dict(candidate_id=identity) for identity in pairs] if include_alternatives else []
        plan = build_reference_plan(data, reference, selection,
                                    cuts={identity: row[2] for identity, row in prepared.items()},
                                    baseline_script_map=mapping)
        output_dir = Path(output_dir).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r'[^\w\-\u4e00-\u9fff]', '_', sessions[reference]['name'])[:60]
        label = ('基准及备选' if include_baseline else '仅备选') if include_alternatives else '基准'
        final = output_dir/f'{safe_name}_{label}_{datetime.now():%Y%m%d_%H%M%S_%f}'
        stage = output_dir/('.reference-'+uuid.uuid4().hex)
        stage.mkdir()
        created = stage.lstat()
        stage_owner = (created.st_dev, created.st_ino)
        published = False
        try:
            xmls, maps, coverage_reports = {}, [], []
            base_xml = None
            export_sessions = involved
            for index, identity in enumerate(export_sessions):
                _check(cancelled)
                config, segments, _ = prepared[identity]
                config = dict(config, stereo_audio_cache_dir=str(output_dir/'配对立体声WAV'))
                def per_file(value, message):
                    progress(round((index+max(0,min(100,value))/100)/max(1,len(export_sessions))*75),
                             sessions[identity]['name']+'：'+message)
                # Rebuild interchange files even when the saved baseline is
                # valid: old maps may predate guide-audio identity fields.
                # Preparation reuses verified sound plans; no ASR/sync rerun.
                events, path = export(config, segments, stage/'sessions'/identity, per_file, cancelled)
                if blocking_proxy_events(events, config):
                    raise _proxy_coverage_error(sessions[identity]['name'], events, config)
                coverage_reports.append(camera_coverage_summary(sessions[identity]['name'], events, config))
                xmls[identity] = path
                if include_baseline or identity != reference:
                    maps.append(path.parent/'proxy_original_map.json')
                if identity == reference:
                    base_xml = path
            _check(cancelled)
            xml_path = stage/f'{safe_name}_{label}_MULTICAM.fcpxml'
            report = assemble_reference_xml(base_xml, xmls, plan, xml_path,
                                            title=sessions[reference]['name']+'_'+label, include_baseline=include_baseline)
            map_path = stage/'proxy_original_map.json'
            self._merge_maps(maps, map_path)
            (stage/'camera_coverage_report.json').write_text(json.dumps(
                dict(version=1, sessions=coverage_reports), ensure_ascii=False, indent=2), encoding='utf-8')
            # Snapshot identities are checked again before publishing output.
            latest = self.read_all()
            if latest['reference_state'].get('revision') != state.get('revision'):
                raise ReferenceTimelineError('基准备选设置已被其他人更新，请刷新后重新导出。')
            if (include_alternatives and
                    PgmTakeIndex(latest, prepared=True).export_signature(reference) != inventory_signature):
                raise ReferenceTimelineError('导出期间轨道上的绿色备选片段已更新，请刷新后重新导出。')
            if any(session_fingerprint(latest, identity) != value for identity, value in fingerprints.items()):
                raise ReferenceTimelineError('导出期间素材、合板或台词核对已更新，请刷新后重新导出。')
            _check(cancelled)
            _publish_export_stage(stage, final)
            published = True
            xml_path = final/xml_path.name
            map_path = final/map_path.name
            manifest_path = final/Path(report['manifest_path']).name
            if not include_alternatives:
                new_baseline = dict(valid=True, xml_path=canonical_path(str(xml_path)),
                    map_path=canonical_path(str(map_path)), manifest_path=canonical_path(str(manifest_path)),
                    fingerprint=fingerprints[reference], xml_sha256=_file_digest(str(xml_path)),
                    map_sha256=_file_digest(str(map_path)), duration_frames=plan['baseline_duration_frames'],
                    exported_at=datetime.now().isoformat(timespec='seconds'))
                if baseline.get('legacy_continuous_confirmed'):
                    new_baseline['legacy_continuous_confirmed'] = True
                state = self._save(state, baseline=new_baseline)
            self.client.call('register_artifact', session_id=reference,
                kind=('reference_combined_xml' if include_baseline else 'reference_alternatives_xml')
                     if include_alternatives else 'reference_baseline_xml',
                path=canonical_path(str(xml_path)), manifest=dict(map_path=canonical_path(str(map_path)),
                    manifest_path=canonical_path(str(manifest_path)), selected_count=report['selected_count'],
                    include_baseline=include_baseline,
                    reference_fingerprint=fingerprints[reference]))
            progress(100, '已导出'+label+'多机位 XML。')
            return dict(data=self.read_all(), state=state, xml_path=str(xml_path), map_path=str(map_path),
                        manifest_path=str(manifest_path), output_dir=str(final), plan=plan,
                        selected_count=report['selected_count'], piece_count=report['piece_count'])
        except Exception:
            _LOG.exception('多机位导出未完成；%s：%s', '文件已发布' if published else '尚未发布',
                           final if published else stage)
            raise
        finally:
            if not published:
                _cleanup_export_stage(stage, output_dir, stage_owner, sys.exception())
