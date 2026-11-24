"""Reviewable whole-PGM placement proposals from cached dialogue evidence.

No speech model, media decoder or cross-performance source-clock comparison is
used here. The script organizer remains unchanged. All proposed coordinates
are target positions only and every automatic placement requires human review.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
import copy
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path


VERSION = 2
_NEARBY_LINES = 8
_UNCERTAIN_OCCURRENCE = ('重复', '冲突', '近似同分', '重叠')


def _check(cancelled):
    if cancelled():raise ValueError('已取消自动初排。')


def _hash(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,
        separators=(',', ':'),default=str).encode('utf-8')).hexdigest()


def _speech_view(data,baseline_id):
    """Isolate baseline speech from organizer-only visual classifications.

    The ordinary browser deliberately hides ASR under manual visual ranges.
    Those saved decisions still govern the organizer, but cannot suppress a
    separate baseline speech cache. This view never mutates the catalogue.
    """
    from library_pending import is_manual_visual
    baseline=[row for row in data.get('transcripts',[]) if row.get('session_id')==baseline_id]
    all_ids={row.get('id') for row in baseline}
    spoken={row.get('id') for row in baseline if not is_manual_visual(row)}
    transcripts=[row for row in data.get('transcripts',[]) if row.get('session_id')!=baseline_id or row.get('id') in spoken]
    candidates=[row for row in data.get('candidates',[]) if not (
        (row.get('session_id')==baseline_id or row.get('transcript_id') in all_ids)
        and row.get('transcript_id') not in spoken)]
    return dict(data,transcripts=transcripts,candidates=candidates)


def _base_view(data, baseline_id):
    """Keep all session identities, but no alternate speech/green-unit input."""
    data=_speech_view(data,baseline_id)
    transcripts=[row for row in data.get('transcripts',[]) if row.get('session_id')==baseline_id]
    ids={row.get('id') for row in transcripts}
    return dict(data,transcripts=transcripts,candidates=[row for row in data.get('candidates',[])
        if row.get('transcript_id') in ids or not transcripts and row.get('session_id')==baseline_id])


def _evidence_fingerprint(data, baseline_id):
    from library_correspondence import filter_active_transcripts
    active=filter_active_transcripts(_base_view(data,baseline_id))
    lines=[{key:row.get(key) for key in ('id','script_id','order','line_order','text','speaker','scene','active')}
        for row in active.get('scripts',[]) if row.get('active',row.get('script_active',True))]
    sources=[{key:row.get(key) for key in ('id','session_id','media_path','start_seconds','end_seconds','text','kind','data','metadata')}
        for row in active.get('transcripts',[])]
    candidates=[{key:row.get(key) for key in ('id','script_line_id','transcript_id','status','score','auto_review_score','tags','note','active','script_active')}
        for row in active.get('candidates',[]) if not row.get('alignment_generated')]
    return _hash(dict(version=VERSION,baseline_id=baseline_id,scripts=lines,
        sources=sorted(sources,key=lambda row:str(row['id'])),candidates=sorted(candidates,key=lambda row:str(row['id']))))


def prepare_alignment_evidence(data, baseline_id, progress=lambda n,m:None, cancelled=lambda:False, *, mapping=None):
    """Generate missing baseline text candidates in an isolated deterministic view.

    ``evidence_data`` must accompany a proposed mapping through its review/save
    operation. Generated candidates are stored inside the map cache, never in
    the user's associations table. Human choices and rejections have priority.
    """
    from library_correspondence import filter_active_transcripts
    from library_pending import is_manual_visual
    from pgm_takes import _manual_choice
    _check(cancelled)
    data=_speech_view(data,baseline_id)
    fingerprint=_evidence_fingerprint(data,baseline_id)
    base_ids={row.get('id') for row in data.get('transcripts',[]) if row.get('session_id')==baseline_id}
    existing=[row for row in data.get('candidates',[]) if not row.get('alignment_generated') and
        not (base_ids and row.get('session_id')==baseline_id and row.get('transcript_id') not in base_ids)]
    base=_base_view(dict(data,candidates=existing),baseline_id)
    active=filter_active_transcripts(base)
    sources=[row for row in active.get('transcripts',[]) if not is_manual_visual(row)]
    source_ids={row.get('id') for row in sources}
    lines=[row for row in active.get('scripts',[]) if row.get('active',row.get('script_active',True))]
    line_ids={row.get('id') for row in lines}
    cache=(mapping or {}).get('automatic_evidence') or {}
    generated=[];reused=False;diagnostics=[]
    if cache.get('version')==VERSION and cache.get('input_fingerprint')==fingerprint:
        rows=cache.get('generated_candidates')
        if isinstance(rows,list) and all(isinstance(row,dict) and row.get('transcript_id') in source_ids
                and row.get('script_line_id') in line_ids and row.get('alignment_generated') is True for row in rows):
            generated=copy.deepcopy(rows);reused=True
    if not reused and sources and lines:
        from dialogue_engine import match_dialogue
        progress(0,'对照基准已有台词与台本…')
        matches=match_dialogue(lines,sources,threshold=.45,top_k=5,progress=progress,cancelled=cancelled)
        active_candidates=[row for row in active.get('candidates',[]) if row.get('script_line_id') in line_ids]
        manual_sources={row.get('transcript_id') for row in active_candidates if _manual_choice(row)}
        # An old *automatic* low score must not suppress the fresh read-only
        # match for the same pair. Only real human decisions own that pair.
        protected_pairs={(row.get('transcript_id'),row.get('script_line_id')) for row in active_candidates
            if _manual_choice(row) or row.get('status')=='rejected' and 'pgm_match_removed' not in (row.get('tags') or [])}
        for row in matches:
            _check(cancelled)
            pair=(row.get('transcript_id'),row.get('script_line_id'))
            if pair[0] in manual_sources or pair in protected_pairs:continue
            identity='alignment-candidate-'+_hash([pair[1],pair[0]])[:24]
            tags=list(dict.fromkeys(['auto_dialogue']+list(row.get('tags') or [])))
            generated.append(dict(row,id=identity,status='candidate',tags=tags,
                alignment_generated=True,session_id=baseline_id))
        generated.sort(key=lambda row:row['id'])
        diagnostics.append(dict(code='baseline_text_candidates',reason='仅在自动初排的只读证据中补充基准台词候选。',count=len(generated)))
    _check(cancelled)
    evidence=dict(data,candidates=list(existing)+generated)
    metadata=dict(version=VERSION,input_fingerprint=fingerprint,generated_candidates=copy.deepcopy(generated))
    evidence['automatic_alignment_evidence']=metadata
    return dict(evidence_data=evidence,input_fingerprint=fingerprint,generated_candidates=generated,
        reused=reused,diagnostics=diagnostics)


def _inventory(rows):
    return {row.get('id') or row.get('take_id'):row for row in (rows.values() if isinstance(rows,dict) else rows or [])}


def _diagnostic(source,code,reason):
    return dict(take_id=source.get('id') or source.get('take_id'),session_id=source.get('session_id'),
        pgm_path=source.get('pgm_path'),filename=source.get('filename') or Path(source.get('pgm_path') or '').name,
        code=code,reason=reason)


def _natural_units(data,baseline_id,cut):
    """Do not split a real multi-line utterance into invented internal times.

    Reuse the existing monotone beam and weighted position prior to choose
    ASR associations. Only the selected spoken spans suppress internal script
    boundaries; visual whole-file classifications never become speech units.
    """
    from baseline_script_map import _catalogue,_source_records,_options,_monotone_path,_cut
    from dialogue_engine import normalize_dialogue
    cut=_cut(cut);view,index=_catalogue(_base_view(data,baseline_id))
    records=_source_records(view,baseline_id,cut,index);by_source=defaultdict(list)
    for row in index.candidates.values():by_source[row.get('transcript_id') or row.get('id')].append(row)
    weights={identity:max(1,len(normalize_dialogue(str(index.script_lines[identity].get('text',''))).split()))
             for identity in index.script_order}
    total=sum(weights.values()) or 1;relative={};cursor=0
    for identity in index.script_order:relative[identity]=cursor/total;cursor+=weights[identity]
    options=[_options(row,index,by_source,relative,cut['duration']) for row in records]
    path,_ambiguity=_monotone_path(records,options)
    # Difference events keep wide units O(lines + chosen spans), instead of
    # touching every internal line for every overlapping alternative.
    events=defaultdict(int)
    for choice in path:
        if choice and not choice['visual'] and choice['confidence']>=.45 and choice['last']>choice['first']:
            events[choice['first']+1]+=1;events[choice['last']+1]-=1
    boundaries=[0];cover=0
    for number,identity in enumerate(index.script_order):
        if not number:continue
        cover+=events.get(number,0)
        family=index.script_lines[identity].get('script_id','')
        previous=index.script_lines[index.script_order[number-1]].get('script_id','')
        if not cover or family!=previous:boundaries.append(number)
    boundaries.append(len(index.script_order))
    return [dict(id=f'asr-unit-{first}-{last}',line_ids=index.script_order[first:last])
        for first,last in zip(boundaries,boundaries[1:]) if first<last]


def _needs_natural_rebuild(mapping):
    # Explicit human partitions survive UI retirement and algorithm upgrades.
    if any(row.get('manual') and not row.get('needs_review') and row.get('line_ids')
           and row.get('anchor_frame') is not None for row in mapping.get('segments',[])):return False
    return (mapping.get('automatic_alignment_version',0)<VERSION and
        ((mapping.get('automatic_evidence') or {}).get('version',VERSION)<VERSION
         or not any(row.get('anchor_frame') is not None and row.get('line_ids') for row in mapping.get('segments',[]))
         or any('跨越台本单元边界' in row.get('reason','') for row in mapping.get('segments',[]))))


def _contextual_anchors(segments,positions,families,fps):
    """Allow modest lexical matches only with coherent neighbouring evidence.

    Isolated weak phrases, unsupported positions and conflicting occurrences
    stay unresolved. This changes only proposal confidence, never source time.
    """
    rows=copy.deepcopy(segments);run=[]
    def finish():
        strong=any(float(row.get('confidence',0))>.60 for row in run)
        if len(run)>=3 or strong and len(run)>=2:
            for row in run:
                if .45<=float(row.get('confidence',0))<=.60:row['_rough_supported']=True
        run.clear()
    for row in rows:
        if (not row.get('line_ids') or row.get('anchor_frame') is None or float(row.get('confidence',0))<.45
                or row.get('needs_review') and any(word in row.get('reason','') for word in _UNCERTAIN_OCCURRENCE)):
            continue
        if run:
            previous=run[-1];a=positions[previous['line_ids'][-1]];b=positions[row['line_ids'][0]]
            if (families[previous['line_ids'][0]]!=families[row['line_ids'][0]] or b-a>16 or
                    row['anchor_frame']-previous['anchor_frame']>120*fps):finish()
        run.append(row)
    finish()
    return rows


def _reliable_segment(row):
    if not row or row.get('anchor_frame') is None or not row.get('line_ids'):return False
    if row.get('needs_review') and any(word in row.get('reason','') for word in _UNCERTAIN_OCCURRENCE):return False
    return bool(row.get('manual') and not row.get('needs_review') or float(row.get('confidence',0))>.60 or row.get('_rough_supported'))


def _baseline_anchor(first,last,order,mapping):
    """Use observed anchors only; nearby fallback never interpolates time."""
    from baseline_script_map import build_line_index
    # Filled once by the caller; this fallback keeps the helper independent.
    lookup=mapping.get('_line_lookup') or build_line_index(mapping)
    line=order[first-1];segment=lookup.get(line)
    ambiguous=bool(segment and segment.get('needs_review') and any(word in segment.get('reason','') for word in _UNCERTAIN_OCCURRENCE))
    if _reliable_segment(segment) and segment['line_ids'][0]==line:
        return dict(segment=segment,line_id=line,line_number=first,approximate=bool(segment.get('needs_review')),
            reason='基准首行已有实际台词定位点。'),'',''
    anchors=mapping['_ordered_anchors'];positions=mapping['_anchor_positions']
    begin,end=bisect_left(positions,first),bisect_right(positions,last)
    if begin < end:
        number,row=anchors[begin]
        return dict(segment=row,line_id=order[number-1],line_number=number,approximate=True,
            reason=f'首行缺少独立时间定位点，按覆盖区间内第{number}行的已识别起点粗排。'),'',''
    if ambiguous:
        return None,'baseline_ambiguous','基准这段台词存在多个可能位置，可在时间轴手动放置。'
    # A small merged unit can reuse its *actual* observed first onset as an
    # explicitly rough location. Large gaps in evidence never become linear
    # line-number-to-time estimates.
    if (_reliable_segment(segment) and len(segment['line_ids'])<=2*_NEARBY_LINES):
        first_line=segment['line_ids'][0];number=mapping['_script_positions'][first_line]+1
        if abs(number-first)<=_NEARBY_LINES:
            return dict(segment=segment,line_id=first_line,line_number=number,approximate=True,
                reason=f'该行位于合并区间内部，按所属区间第{number}行的实际起点粗排。'),'',''
    neighbours=[]
    for position in (begin-1,begin):
        if 0 <= position < len(anchors):
            number,row=anchors[position]
            if (abs(number-first)<=_NEARBY_LINES and
                    mapping['_script_families'][order[number-1]]==mapping['_script_families'][line]):
                neighbours.append((abs(number-first),number,row))
    if neighbours:
        _,number,row=min(neighbours,key=lambda item:(item[0],item[1]))
        return dict(segment=row,line_id=order[number-1],line_number=number,approximate=True,
            reason=f'该行未找到精确起点，暂按附近第{number}行的实际台词定位点粗排。'),'',''
    return None,'baseline_anchor_missing','基准对应台本区间没有可靠台词定位点，可在时间轴手动放置。'


def _source_anchors(index,eligible):
    """One real onset per continuous same-line run, with repeats explicit."""
    from library_pending import is_manual_visual
    from pgm_takes import _score
    from dialogue_engine import normalize_dialogue
    table=defaultdict(list);weights={}
    for key in eligible:
        previous_greater=[]
        for sequence,source in enumerate(index.rows.get(key,[])):
            if is_manual_visual(source):continue
            winner=index._winner(index._by_transcript.get(source['id'],[]))
            if winner is None or _score(winner)<=.60:continue
            try:
                first,end=Fraction(str(source['start_seconds'])),Fraction(str(source['end_seconds']))
            except (KeyError,TypeError,ValueError,ZeroDivisionError,OverflowError):continue
            if first<0 or end<=first:continue
            line=winner['script_line_id'];text=normalize_dialogue(str(source.get('text','')))
            if line not in weights:weights[line]=len(normalize_dialogue(str(index.script_lines[line].get('text',''))).split())
            size=len(text.split());long=weights[line]>=26
            # A generic three-character phrase in a distant scene cannot
            # create a second occurrence of a long verified paragraph.
            if long and size<=3 and _score(winner)<.8:continue
            position=index.script_positions[line]
            while previous_greater and previous_greater[-1][1]<=position:previous_greater.pop()
            greater=previous_greater[-1][0] if previous_greater else -1
            previous_greater.append((sequence,position))
            partial=long and size < .8*weights[line]
            table[(key,line)].append((first,end,_score(winner),winner['id'],partial,text,sequence,greater))
    result={}
    for key,rows in table.items():
        rows.sort();runs=[]
        for first,end,confidence,identity,partial,text,sequence,greater in rows:
            continuation=bool(runs and partial and runs[-1]['partial'] and
                first<=runs[-1]['end']+60 and greater<=runs[-1]['last_sequence'] and text not in runs[-1]['texts'])
            if runs and (first <= runs[-1]['end']+3 or continuation):
                runs[-1]['end']=max(runs[-1]['end'],end)
                runs[-1]['confidence']=min(runs[-1]['confidence'],confidence)
                runs[-1]['evidence_ids'].append(identity)
                runs[-1]['partial'] &= partial;runs[-1]['texts'].add(text);runs[-1]['last_sequence']=sequence
            else:runs.append(dict(start=first,end=end,confidence=confidence,evidence_ids=[identity],
                partial=partial,texts={text},last_sequence=sequence))
        result[key]=runs
    return result


def _lanes(proposals,layout,inv,baseline_id):
    """Keep existing ownership, fit each full PGM into a collision-free lane."""
    occupied=defaultdict(list)
    proposed_ids={row['take_id'] for row in proposals}
    for identity,item in (layout.get('items') or {}).items():
        source=inv.get(identity)
        if identity in proposed_ids or not source or item.get('hidden') or source.get('session_id')==baseline_id:continue
        duration=source.get('duration_seconds');first=item.get('start_seconds')
        if first is not None and duration:
            occupied[item.get('lane',1)].append((float(first),float(first)+float(duration)))
    for rows in occupied.values():rows.sort()
    for proposal in sorted(proposals,key=lambda row:(row['start_seconds'],row['take_id'])):
        first=proposal['start_seconds'];end=first+proposal['duration_seconds'];lane=1
        while True:
            rows=occupied[lane];position=bisect_left(rows,(first,-math.inf))
            if (position==0 or rows[position-1][1] <= first+1e-9) and (position==len(rows) or end<=rows[position][0]+1e-9):
                rows.insert(position,(first,end));break
            lane+=1
        proposal['lane']=lane


def build_alignment_proposals(data,inventory,layout,baseline_cut,mapping=None,*,replace_automatic=False,
        progress=lambda n,m:None,cancelled=lambda:False):
    """Propose blue actual-time placements for green script-organizer takes.

    Existing positions are protected by default. Rough proposals state exactly
    which real anchor was reused; unknown or ambiguous baseline positions are
    diagnostics, never invented source clocks or proportionally scaled lines.
    """
    from baseline_script_map import build_baseline_map,validate_map,build_line_index
    from pgm_takes import PgmTakeIndex
    inv=_inventory(inventory);baseline_id=layout.get('baseline_session_id')
    result=dict(version=VERSION,baseline_session_id=baseline_id,mapping=None,mapping_reused=False,
        proposals=[],unresolved=[],skipped=[],diagnostics=[],evidence_data=data)
    if not baseline_id:
        result['unresolved'].append(dict(code='baseline_required',reason='请先确定基准时间线。'))
        return _summary(result)
    _check(cancelled)
    data=_speech_view(data,baseline_id)
    valid=None;cache_error=None
    if mapping:
        try:valid=validate_map(mapping,data,baseline_cut,baseline_id)
        except ValueError as exc:cache_error=str(exc)
        if valid is not None and _needs_natural_rebuild(valid):
            valid=None;cache_error='旧自动映射未保留多行语音起点，已按当前基准台词重新建立。'
    if valid is not None:
        evidence=data;result['mapping_reused']=True
    else:
        prepared=prepare_alignment_evidence(data,baseline_id,progress,cancelled,mapping=mapping)
        evidence=prepared['evidence_data'];result['diagnostics'].extend(prepared['diagnostics'])
        if mapping:
            try:valid=validate_map(mapping,evidence,baseline_cut,baseline_id);result['mapping_reused']=True
            except ValueError as exc:cache_error=str(exc)
            if valid is not None and _needs_natural_rebuild(valid):
                valid=None;result['mapping_reused']=False
                cache_error='旧自动映射未保留多行语音起点，已按当前基准台词重新建立。'
        if valid is None:
            if cache_error:result['diagnostics'].append(dict(code='mapping_cache_changed',
                reason='基准台词或旧自动定位缓存已更新，已重新计算初排位置。',detail=cache_error))
            # No alternative green ranges are allowed to merge away needed
            # baseline line anchors. The monotone path and position prior are
            # reused unchanged from the existing mapping implementation.
            units=_natural_units(evidence,baseline_id,baseline_cut)
            valid=build_baseline_map(_base_view(evidence,baseline_id),baseline_id,baseline_cut,units=units)
            valid['automatic_alignment_version']=VERSION
        valid['automatic_evidence']=copy.deepcopy(evidence.get('automatic_alignment_evidence',{}))
    result.update(mapping=valid,evidence_data=evidence)
    mapping_fingerprint=_hash(valid)
    index=PgmTakeIndex(evidence);order=index.script_order
    positions={identity:number for number,identity in enumerate(order)}
    families={identity:index.script_lines[identity].get('script_id','') for identity in order}
    working=dict(valid,segments=_contextual_anchors(valid['segments'],positions,families,valid['fps']),
        _script_positions=positions,_script_families=families)
    working['_line_lookup']=build_line_index(working)
    working['_ordered_anchors']=sorted((positions[row['line_ids'][0]]+1,row) for row in working['segments']
        if _reliable_segment(row) and row['line_ids'][0] in positions)
    working['_anchor_positions']=[number for number,row in working['_ordered_anchors']]
    result['baseline_anchor_count']=len(working['_ordered_anchors'])
    result['baseline_observed_anchor_count']=sum(row.get('anchor_frame') is not None and bool(row.get('line_ids')) for row in valid['segments'])
    result['baseline_asr_segment_count']=sum(row.get('session_id')==baseline_id for row in evidence.get('transcripts',[]))
    eligible=[]
    for identity,source in inv.items():
        _check(cancelled)
        item=(layout.get('items') or {}).get(identity) or {}
        code=reason=None
        if source.get('session_id')==baseline_id:code,reason='baseline','基准场次不重复作为备选。'
        elif item.get('hidden') or item.get('organize_pool')!='track':code,reason='not_on_track','素材不在已保留的台本轨道上。'
        elif item.get('organize_reviewed') is not True:code,reason='script_unreviewed','台本区间尚未人工核实为绿色。'
        elif item.get('start_seconds') is not None and not (replace_automatic and item.get('alignment_origin')=='automatic'
                and not item.get('alignment_reviewed')):code,reason='position_preserved','保留已有的实际时间位置。'
        if code:
            result['skipped'].append(_diagnostic(source,code,reason));continue
        first,last=item.get('organize_first_line'),item.get('organize_last_line')
        if type(first) is not int or type(last) is not int or not 1 <= first <= last <= len(order):
            result['unresolved'].append(_diagnostic(source,'script_range_invalid','台本区间无效或已超出当前台本。'));continue
        try:duration=Fraction(str(source.get('duration_seconds')));fps=Fraction(str(source.get('fps')))
        except (TypeError,ValueError,ZeroDivisionError):duration=fps=0
        if duration<=0 or fps!=valid['fps'] or source.get('ready') is False:
            result['unresolved'].append(_diagnostic(source,'media_metadata_missing','PGM时长或帧率信息不完整，或与基准帧率不一致。'));continue
        if index.script_lines[order[first-1]].get('script_id','') != index.script_lines[order[last-1]].get('script_id',''):
            result['unresolved'].append(_diagnostic(source,'script_family_conflict','覆盖区间跨越了不同台本，需人工确认。'));continue
        eligible.append((identity,source,item,first,last,float(duration)))
    result['eligible_count']=len(eligible)
    source_keys={(row['session_id'],str(Path(row['pgm_path']).expanduser())) for _,row,*_ in eligible}
    source_anchors=_source_anchors(index,source_keys)
    for identity,source,item,first,last,duration in eligible:
        _check(cancelled)
        anchor,code,reason=_baseline_anchor(first,last,order,working)
        if anchor is None:
            result['unresolved'].append(_diagnostic(source,code,reason));continue
        key=(source['session_id'],str(Path(source['pgm_path']).expanduser()))
        # Human organizer coverage overrides a stale outside-range machine
        # association. Nearby baseline anchors can only give a rough head
        # position, not revive rejected alternative dialogue outside the range.
        runs=(source_anchors.get((key,anchor['line_id']),[])
              if first<=anchor['line_number']<=last else [])
        if len(runs)>1:
            result['unresolved'].append(_diagnostic(source,'source_repeated_dialogue','备选中同一句台词多次出现，可在时间轴手动放置。'));continue
        onset=runs[0]['start'] if runs else None
        if onset is not None and onset>=Fraction(str(duration)):
            result['unresolved'].append(_diagnostic(source,'source_anchor_outside','备选台词定位点超出整条PGM，需核查缓存。'));continue
        segment=anchor['segment'];approximate=anchor['approximate'] or onset is None or bool(runs and runs[0].get('partial'))
        baseline_time=Fraction(segment['anchor_frame'],valid['fps'])
        target=baseline_time-(onset if onset is not None else 0)
        reason=anchor['reason']
        if onset is None:reason+='；缺少备选台词起点，按片头粗排'
        elif runs[0].get('partial'):reason+='；备选长台词由多个片句识别，按最早已识别片句起点粗排。'
        else:reason+='；按两条实际台词起点对齐，保留整条PGM原速。'
        baseline_confidence=(1. if segment.get('manual') and not segment.get('needs_review')
                             else float(segment['confidence']))
        confidence=min(baseline_confidence,runs[0]['confidence'] if runs else .35)
        if approximate:confidence=min(confidence,.45)
        basis=dict(method='script_dialogue',baseline_session_id=baseline_id,mapping_fingerprint=mapping_fingerprint,
            evidence_fingerprint=valid['evidence_fingerprint'],
            evidence_input_fingerprint=(valid.get('automatic_evidence') or {}).get('input_fingerprint'),
            first_line=first,last_line=last,baseline_segment_id=segment['id'],
            baseline_anchor_frame=segment['anchor_frame'],baseline_line=anchor['line_number'],
            source_anchor_seconds=float(onset) if onset is not None else None,
            source_anchor_policy=('observed_partial_dialogue' if runs and runs[0].get('partial')
                else 'observed_dialogue') if onset is not None else 'whole_pgm_head',
            source_evidence_ids=runs[0]['evidence_ids'] if runs else [],approximate=approximate,
            confidence=confidence,reason=reason)
        result['proposals'].append(dict(take_id=identity,session_id=source['session_id'],pgm_path=source['pgm_path'],
            filename=source.get('filename') or Path(source['pgm_path']).name,start_seconds=float(target),
            lane=1,duration_seconds=duration,alignment_origin='automatic',alignment_reviewed=False,
            alignment_basis=basis,alignment_reason=reason,alignment_confidence=confidence,
            reason=reason,confidence=confidence,
            approximate=approximate,needs_review=True,mapping_fingerprint=mapping_fingerprint,
            first_line=first,last_line=last,baseline_segment_id=segment['id'],baseline_anchor_frame=segment['anchor_frame'],
            source_anchor_seconds=float(onset) if onset is not None else None))
    _lanes(result['proposals'],layout,inv,baseline_id)
    progress(100,'自动初排提案已生成，仍需人工核实。')
    return _summary(result)


def _summary(result):
    result['summary']=dict(proposed_count=len(result['proposals']),
        preserved_count=sum(row.get('code')=='position_preserved' for row in result['skipped']),
        unresolved_count=len(result['unresolved']),skipped_count=len(result['skipped']),
        approximate_count=sum(row.get('approximate',False) for row in result['proposals']),
        eligible_count=result.get('eligible_count',0),baseline_anchor_count=result.get('baseline_anchor_count',0),
        baseline_observed_anchor_count=result.get('baseline_observed_anchor_count',0),
        baseline_asr_segment_count=result.get('baseline_asr_segment_count',0),
        skipped_reasons={code:sum(row.get('code')==code for row in result['skipped'])
            for code in sorted({row.get('code','unknown') for row in result['skipped']})})
    return result
