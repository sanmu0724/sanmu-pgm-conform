"""Baseline-only speech cache; never rematch or replace organizer decisions."""
from __future__ import annotations
import copy
import hashlib
import json
import math
from pathlib import Path

FIELD = 'library_baseline_alignment_asr'
VERSION = 1


def _check(cancelled):
    if cancelled():
        raise ValueError('已取消基准台词识别；已完成的转写缓存保留，编排位置未改变。')


def _signature(path):
    from library_bridge import resolve_local_path
    try:
        stat = Path(resolve_local_path(path)).stat()
        return dict(size=stat.st_size, mtime_ns=stat.st_mtime_ns)
    except OSError:
        return None


def _spoken(rows):
    from library_pending import is_manual_visual
    result=[]
    for row in rows:
        if is_manual_visual(row) or not str(row.get('text') or '').strip():continue
        try:
            first,last=float(row['start_seconds']),float(row['end_seconds'])
            if not math.isfinite(first) or not math.isfinite(last) or first<0 or last<=first:continue
        except (ValueError,TypeError,KeyError,OverflowError):continue
        result.append(row)
    return result


def _cache_rows(session_id,path,entry):
    result=[]
    for row in _spoken(entry.get('segments') or []):
        key=json.dumps([session_id,path,row['start_seconds'],row['end_seconds'],row['text']],ensure_ascii=False,separators=(',',':'))
        identity='alignment-asr-'+hashlib.sha256(key.encode()).hexdigest()[:32]
        result.append(dict(row,id=identity,session_id=session_id,media_path=path,media_kind='pgm',
            metadata=dict(asr_complete=True,source_kind='baseline_alignment',source_stat=entry.get('source_stat'))))
    return result


def ensure_baseline_speech(client,production_id,data,baseline_id,cut,*,model='small',
                           progress=lambda n,m:None,cancelled=lambda:False,decode_missing=True):
    """Reuse active speech; decode only missing baseline PGM into a private cache.

    A returned evidence view may override active IDs in memory. Neither the
    original transcript tables, script assignments, sound plan nor media are
    rewritten. Existing speech is usable offline when the source is unmounted.
    """
    from library_correspondence import filter_active_transcripts
    from library_bridge import resolve_local_path,translate_config
    from library_workflow import BatchWorkflow
    from library_sync import valid_sync,transcribe_synced_pgm
    from dialogue_engine import transcribe_media,validate_transcript
    from sound_fallback import sound_entry
    session=next((row for row in data.get('sessions',[]) if row.get('id')==baseline_id),None)
    if session is None:raise ValueError('请先选择基准通条。')
    # Script/visual categorization must never suppress source-local speech in
    # this baseline-only task. Sound provenance and active run IDs still apply.
    speech_view=dict(data,candidates=[],transcripts=[row for row in data.get('transcripts',[])
        if row.get('session_id')==baseline_id])
    active=filter_active_transcripts(speech_view)
    existing={}
    for row in _spoken(active.get('transcripts',[])):
        if row.get('session_id')==baseline_id:existing.setdefault(row.get('media_path'),[]).append(row)
    config=session.get('config') or {};cached=config.get(FIELD) or {}
    paths=[row['path'] for row in cut.get('pgm_sources') or []]
    chosen={};failures=[];recognized=reused=0
    workflow=BatchWorkflow(client,production_id)
    for number,path in enumerate(paths):
        _check(cancelled)
        current=_signature(path);entry=cached.get(path) or {};rows=existing.get(path) or []
        known=next((row.get('metadata') or {} for row in rows if (row.get('metadata') or {}).get('source_stat')), {})
        if rows and not (current and known.get('source_stat') and current!=known['source_stat']):
            chosen[path]=rows;reused+=1
            progress(round((number+1)/max(1,len(paths))*100),'复用基准转写：'+Path(path).name);continue
        source_sync=sound_entry(config.get('library_sync'),path) or {}
        sync_signature=source_sync.get('signature','')
        cache_current=(entry.get('version')==VERSION and entry.get('complete') and
            (current is None or entry.get('source_stat')==current) and
            (entry.get('audio_source')=='pgm' or entry.get('sync_signature')==sync_signature))
        if cache_current:
            chosen[path]=_cache_rows(baseline_id,path,entry);reused+=1;continue
        if not decode_missing:
            chosen[path]=[];failures.append(dict(pgm_path=path,filename=Path(path).name,reason='基准尚无有效转写。'));continue
        try:
            if current is None:raise ValueError('素材未挂载，且没有可复用的基准转写。')
            if rows or entry.get('complete'):
                # Do not silently overwrite timing evidence for a replaced source.
                if known.get('source_stat') and current!=known['source_stat'] or entry.get('source_stat') and current!=entry['source_stat']:
                    raise ValueError('基准PGM的文件大小或修改时间已变化，请核对并重新登记素材。')
            translated=translate_config(copy.deepcopy(config))
            local_sync=sound_entry(translated.get('library_sync'),resolve_local_path(path)) or source_sync
            def report(n,message):
                progress(round((number+max(0,min(100,n))/100)/max(1,len(paths))*100),
                    f'基准PGM {number+1}/{len(paths)} {Path(path).name}：{message}')
            use_sync=bool(local_sync and valid_sync(translated,resolve_local_path(path),local_sync))
            if use_sync:
                segments=transcribe_synced_pgm(translated,resolve_local_path(path),model=model,language='auto',
                    progress=report,cancelled=cancelled,sync=local_sync)['segments']
                audio_source='original_wav'
            else:
                segments=transcribe_media(resolve_local_path(path),model=model,language='auto',progress=report,cancelled=cancelled)
                audio_source='pgm'
            _check(cancelled)
            segments=validate_transcript(segments)
            if _signature(path)!=current:raise ValueError('识别期间基准素材发生变化，未保存此次转写。')
            entry=dict(version=VERSION,complete=True,model=model,language='auto',source_stat=current,
                audio_source=audio_source,sync_signature=sync_signature,segments=segments)
            workflow._save_state(baseline_id,FIELD,path,entry)
            chosen[path]=_cache_rows(baseline_id,path,entry);recognized+=1
        except Exception as exc:
            _check(cancelled)
            chosen[path]=[];failures.append(dict(pgm_path=path,filename=Path(path).name,reason=str(exc)))
    _check(cancelled)
    result=copy.deepcopy(data)
    # Restrict this view to current evidence, including cached empty results.
    result['transcripts']=[row for row in result.get('transcripts',[])
        if row.get('session_id')!=baseline_id or row.get('media_path') not in chosen]
    for rows in chosen.values():result['transcripts'].extend(copy.deepcopy(rows))
    for row in result.get('sessions',[]):
        if row.get('id')!=baseline_id:continue
        asr=dict((row.get('config') or {}).get('library_asr') or {})
        for path,rows in chosen.items():
            # A private, source-local speech view must not inherit unrelated
            # sound-gate failures that belonged to a previous catalogue run.
            asr[path]=dict(source_kind='manual',active_transcript_ids=[r['id'] for r in rows],complete=True)
        row.setdefault('config',{})['library_asr']=asr
        processing=dict(row['config'].get('library_processing') or {})
        for path in chosen:processing.pop(path,None)
        row['config']['library_processing']=processing
    progress(100,f'基准转写：新识别 {recognized} 条，复用 {reused} 条，待处理 {len(failures)} 条。')
    return dict(data=result,recognized=recognized,reused=reused,failures=failures)
