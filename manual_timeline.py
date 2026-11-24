"""Whole-PGM script organization and independent manual time placement.

The organizer uses saved script line ranges; only the alignment view uses
seconds. Neither operation changes source IN/OUT, TC, paths or media metadata.
"""
from __future__ import annotations
from collections import defaultdict
from bisect import bisect_left
import copy
from fractions import Fraction
import hashlib
import json
import math
import heapq
from pathlib import Path

VERSION = 1
ORGANIZER_POOLS = frozenset(('track', 'unmatched', 'removed', 'hidden'))
ALIGNMENT_ORIGINS = frozenset(('legacy', 'manual', 'automatic'))


def _path(value):
    return str(Path(value).expanduser()) if isinstance(value, str) and value else ''


def take_id(session_id, pgm_path):
    value = json.dumps([session_id, _path(pgm_path)], ensure_ascii=False, separators=(',', ':'))
    return 'pgm-'+hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]


def _positive(value, integer=False):
    try:
        number = Fraction(str(value))
        if number <= 0 or integer and number.denominator != 1:
            return None
        return int(number) if integer else float(number)
    except (ValueError, TypeError, ZeroDivisionError, OverflowError):
        return None


def _header(row):
    result = {}
    for field in ('probe', 'meta', 'metadata', 'info'):
        if isinstance(row.get(field), dict): result.update(row[field])
    result.update({key:value for key,value in row.items() if key not in ('probe','meta','metadata','info')})
    return result


def inventory(data, cuts=None):
    """Return one take per registered scene/path without probing any media.

    Missing duration/record coordinates remain explicit ``None``. Such media
    stays in the organizer and cannot be placed/exported until metadata exists.
    """
    output = []
    for session in data.get('sessions', []):
        sid = session.get('id')
        if not sid: continue
        config = session.get('config') or {}
        rows, order = {}, []
        def add(value, geometry=False):
            if isinstance(value, str): value = dict(path=value)
            if not isinstance(value, dict): return
            value = _header(value)
            path = _path(value.get('path') or value.get('pgm_path'))
            if not path: return
            if path not in rows:
                rows[path] = {};order.append(path)
            rows[path].update(value)
            if geometry: rows[path]['_geometry'] = True
        add(config.get('pgm'))
        for row in config.get('library_media') or []:
            if row.get('kind') == 'pgm': add(row)
        for row in config.get('pgm_sources') or []: add(row, isinstance(row,dict) and 'start' in row and 'end' in row)
        cut = (cuts or {}).get(sid) or {}
        for row in cut.get('pgm_sources') or []: add(row, True)
        for path in order:
            row = rows[path]
            fps = _positive(row.get('fps') or cut.get('fps') or config.get('fps'))
            frames = _positive(row.get('frames'), True)
            first = last = None
            if row.get('_geometry'):
                try:
                    begin,end=Fraction(str(row['start'])),Fraction(str(row['end']))
                    if begin.denominator == end.denominator == 1 and 0 <= begin < end:
                        first,last=int(begin),int(end)
                        if frames is None:frames=last-first
                        if frames != last-first:first=last=None
                except (KeyError,ValueError,TypeError,ZeroDivisionError): pass
            duration = frames/fps if frames is not None and fps else None
            identity = take_id(sid,path)
            output.append(dict(id=identity,take_id=identity,session_id=sid,session_name=session.get('name',''),
                pgm_path=path,filename=Path(path).name,name=Path(path).name,fps=fps,frames=frames,
                duration_seconds=duration,record_frame_in=first,record_frame_out_exclusive=last,
                record_frame_out=last,baseline_start_seconds=first/fps if first is not None and fps else None,
                ready=duration is not None,cut_ready=duration is not None and first is not None,
                metadata_warning=('' if duration is not None and first is not None else
                    '切点位置将在导出时核对。' if duration is not None else '缺少PGM帧率或时长，请重新导入素材。')))
    return output


def _inventory(rows):
    if isinstance(rows, dict): rows=list(rows.values())
    return {row.get('id') or row.get('take_id'):row for row in rows or []}


def attach_script_organizer(data, inventory_rows):
    """Decorate technical inventory with existing whole-PGM editing decisions.

    This is an off-UI-thread catalogue operation, not a matching operation. It
    reads persisted ASR associations and fingerprinted reviews once per PGM;
    no transcription, media probing or automatic reclassification is run.
    Explicit removed/hidden pools win over a later machine match.
    """
    from pgm_takes import PgmTakeIndex
    from pgm_editor_state import read_editor_state
    rows = list((_inventory(inventory_rows)).values())
    if not any(row.get('active', row.get('script_active', True)) for row in data.get('scripts', [])):
        configs={session.get('id'):session.get('config') or {} for session in data.get('sessions', [])}
        result=[]
        for source in rows:
            row=dict(source);state=read_editor_state(configs.get(row['session_id'],{}),row['pgm_path'])
            pool='hidden' if state['hidden_in_pool'] else 'removed' if state['pool']=='removed' else 'unmatched'
            row.update(organize_first_line=None,organize_last_line=None,organize_reviewed=False,
                organize_pool=pool,script_first=None,script_last=None,line_ids=[],script_reviewed=False,script_line_count=0)
            result.append(row)
        return result
    index = PgmTakeIndex(data)
    candidates = defaultdict(list)
    for candidate in index.candidates.values():
        candidates[index.key(candidate)].append(candidate)
    result = []
    for source in rows:
        row = dict(source)
        key = (row['session_id'], _path(row['pgm_path']))
        choices = candidates.get(key, [])
        representative = next((item for item in choices if item.get('status') in ('candidate', 'confirmed')), None)
        if representative is None:
            representative = next(iter(index.rows.get(key, [])), None)
        config = index.sessions.get(key[0], {}).get('config') or {}
        summary = index.file_summary(representative) if representative else {}
        # Organizer color describes the existing reviewed range. A missing
        # speech anchor must not erase that review now that alignment itself
        # is performed manually on the separate actual-time page.
        first_candidate=summary.get('first_candidate')
        state=read_editor_state(config,key[1],summary=summary,
            source=index.source(first_candidate) if first_candidate else None)
        if key in index._manual_anchor_conflicts:state['reviewed']=False
        identities = list(summary.get('covered_line_ids') or [])
        # Removed entries retain their former displayed range when rejection
        # deactivated the association. It is evidence for the removed pool,
        # never a new permission to reinsert it onto the organizer tracks.
        if not identities and state['pool'] == 'removed':
            identities = list({identity for item in choices for identity in
                (index._manual_coverage(item, strict=True) or index.coverage_ids(item))})
        positions = sorted({index.script_positions[identity] for identity in identities
                            if identity in index.script_positions})
        first = positions[0]+1 if positions else None
        last = positions[-1]+1 if positions else None
        covered = index.script_order[first-1:last] if first is not None else []
        pool = ('removed' if state['pool'] == 'removed' else
                'track' if covered and state['pool'] == 'timeline' else 'unmatched')
        if state.get('hidden_in_pool'):
            pool = 'hidden'
        reviewed = bool(pool == 'track' and state.get('reviewed'))
        row.update(organize_first_line=first, organize_last_line=last,
            organize_reviewed=reviewed, organize_pool=pool,
            script_first=first, script_last=last, line_ids=list(covered),
            script_reviewed=reviewed, script_line_count=len(index.script_order))
        result.append(row)
    return result


def _organizer_fields(old, source):
    """Seed only absent fields: an explicit saved edit always wins."""
    initialized=old.get('organize_initialized',any(field in old for field in
        ('organize_first_line','organize_last_line','organize_pool','organize_reviewed')))
    if not initialized and (old.get('organize_first_line') is not None or old.get('organize_last_line') is not None
            or old.get('organize_reviewed') is True or old.get('organize_pool') not in
            (None,'unmatched','hidden' if old.get('hidden') else 'unmatched')):
        initialized=True
    selected=old if initialized else {}
    return dict(organize_first_line=selected.get('organize_first_line', source.get('organize_first_line')),
        organize_last_line=selected.get('organize_last_line', source.get('organize_last_line')),
        organize_reviewed=selected.get('organize_reviewed', source.get('organize_reviewed', False)),
        organize_pool=selected.get('organize_pool', 'hidden' if old.get('hidden') else source.get('organize_pool', 'unmatched')),
        organize_initialized=bool(initialized or 'organize_pool' in source))


def _alignment_fields(old):
    """Legacy coordinates remain visible, but never inherit a human review."""
    return dict(alignment_reviewed=old.get('alignment_reviewed', False),
        alignment_origin=old.get('alignment_origin', 'legacy'),
        alignment_basis=copy.deepcopy(old.get('alignment_basis', {})),
        alignment_invalid_reason=old.get('alignment_invalid_reason', ''))


def _mark_manual_alignment(item, baseline_id):
    """An edited position is a human proposal, not an explicit confirmation."""
    item.update(alignment_reviewed=False, alignment_origin='manual',
        alignment_invalid_reason='实际位置或轨道已改变，请明确确认对齐。', alignment_basis=dict(baseline_session_id=baseline_id,
            first_line=item.get('organize_first_line'), last_line=item.get('organize_last_line')))


def _finite(value,label,nonnegative=False):
    try:value=float(value)
    except (TypeError,ValueError,OverflowError) as exc:raise ValueError(label+'无效。') from exc
    if not math.isfinite(value) or nonnegative and value < 0:raise ValueError(label+'无效。')
    return value


def _lane(value,label):
    if type(value) is not int or value < 1:raise ValueError(label+'必须是大于零的整数。')
    return value


def reconcile_layout(layout, rows, baseline_id=None):
    """Restore script organization without converting it into target seconds.

    Actual placements from earlier manual versions remain byte-for-byte
    numeric values. A baseline change clears only those actual placements.
    """
    if not isinstance(layout,dict):layout={}
    items = copy.deepcopy(layout.get('items') or {}) if layout.get('version') == VERSION else {}
    changed = bool(layout and layout.get('baseline_session_id') != baseline_id)
    inv = _inventory(rows)
    items = {identity:row for identity,row in items.items() if identity in inv}
    new_organizer=[];migrating=layout.get('organizer_axis')!='script'
    for number,(identity,source) in enumerate(inv.items()):
        old=items.get(identity) or {}
        if migrating or not old or 'organize_lane' not in old or not old.get('organize_initialized',True):
            new_organizer.append(identity)
        items[identity]=dict(session_id=source['session_id'],pgm_path=source['pgm_path'],
            start_seconds=None if changed or source['session_id']==baseline_id else old.get('start_seconds'),
            lane=old.get('lane',1),organize_seconds=old.get('organize_seconds',0.),
            organize_lane=old.get('organize_lane',1),hidden=bool(old.get('hidden',False)),
            **_organizer_fields(old,source), **_alignment_fields(old))
        item=items[identity];limit=source.get('script_line_count')
        if changed or source['session_id']==baseline_id or item['start_seconds'] is None:
            item['alignment_reviewed']=False
        if changed:
            item.update(alignment_origin='legacy',alignment_basis={},
                        alignment_invalid_reason='基准场次已改变，请重新定位。')
        if (type(limit) is int and item['organize_pool']=='track' and
                type(item['organize_last_line']) is int and item['organize_last_line']>limit):
            # A replacement/shorter script must not make the whole editor
            # unsaveable. Keep the old numeric range as a manual repair hint.
            item.update(organize_pool='unmatched',organize_reviewed=False)
            item.update(alignment_reviewed=False,alignment_invalid_reason='台本区间已超出当前台本，请重新核对。')
    result=dict(version=VERSION,baseline_session_id=baseline_id,organizer_axis='script',items=items)
    _pack_new_script_items(result,new_organizer,inv)
    return result


def _pack_new_script_items(layout, ids, inv):
    """Pack newly restored ranges; never reflow an explicitly saved lane."""
    ids=set(ids)
    new=sorted((row['organize_first_line'],row['organize_last_line']+1,identity)
        for identity,row in layout['items'].items() if identity in ids
        and row.get('organize_pool')=='track' and row.get('organize_first_line') is not None)
    if not new:return
    fixed=_intervals(layout,inv,'organize',skip=ids)
    if not fixed:
        active=[];free=[];next_lane=1
        for first,last,identity in new:
            while active and active[0][0] <= first:
                _,lane=heapq.heappop(active);heapq.heappush(free,lane)
            lane=heapq.heappop(free) if free else next_lane
            if lane==next_lane:next_lane+=1
            layout['items'][identity]['organize_lane']=lane
            heapq.heappush(active,(last,lane))
        return
    # Only incremental imports need to respect arbitrarily fixed old lanes.
    # Per-lane sorted intervals avoid rescanning every PGM for each collision.
    occupied={lane:sorted((first,last) for first,last,_ in rows) for lane,rows in fixed.items()}
    for first,last,identity in new:
        lane=1
        while True:
            ranges=occupied.setdefault(lane,[]);position=bisect_left(ranges,(first,-math.inf))
            if ((position==0 or ranges[position-1][1] <= first) and
                    (position==len(ranges) or last <= ranges[position][0])):
                ranges.insert(position,(first,last));break
            lane+=1
        layout['items'][identity]['organize_lane']=lane


def _intervals(layout,inv,mode='timeline',skip=()):
    field='organize_seconds' if mode=='organize' else 'start_seconds'
    lane_field='organize_lane' if mode=='organize' else 'lane'
    groups=defaultdict(list)
    for identity,row in layout['items'].items():
        if identity in skip:continue
        if mode=='organize' and layout.get('organizer_axis') == 'script':
            if row.get('organize_pool') == 'track' and row.get('organize_first_line') is not None:
                groups[row['organize_lane']].append((row['organize_first_line'],row['organize_last_line']+1,identity))
            continue
        if row.get('hidden') or mode=='timeline' and row['session_id']==layout.get('baseline_session_id'):continue
        start=row.get(field);duration=inv[identity].get('duration_seconds')
        if start is not None and duration is not None:
            groups[row[lane_field]].append((start,start+duration,identity))
    return groups


def _check_collisions(groups):
    for lane,rows in groups.items():
        rows.sort()
        for left,right in zip(rows,rows[1:]):
            if right[0] < left[1]-1e-9:
                raise ValueError(f'轨道{lane}上的PGM片段相互重叠，请移动到其他轨道。')


def validate_layout(layout, rows, baseline_id=None):
    if not isinstance(layout,dict) or layout.get('version') != VERSION or not isinstance(layout.get('items'),dict):
        raise ValueError('人工时间线格式无效。')
    if layout.get('baseline_session_id') != baseline_id:raise ValueError('人工时间线对应的基准场次已变化。')
    result=dict(version=VERSION,baseline_session_id=baseline_id,organizer_axis='script',items=copy.deepcopy(layout['items']));inv=_inventory(rows)
    for identity,row in result['items'].items():
        source=inv.get(identity)
        if source is None or not isinstance(row,dict):raise ValueError('人工时间线引用了未登记的PGM素材。')
        if row.get('session_id')!=source['session_id'] or _path(row.get('pgm_path'))!=source['pgm_path']:
            raise ValueError('PGM的场次或路径与登记素材不一致。')
        row['lane']=_lane(row.get('lane',1),'备选轨道')
        row['organize_lane']=_lane(row.get('organize_lane',1),'整理轨道')
        row['organize_seconds']=_finite(row.get('organize_seconds',0.),'整理位置',True)
        row.update(_organizer_fields(row,source))
        row.update(_alignment_fields(row))
        if type(row['alignment_reviewed']) is not bool:raise ValueError('实际时间线核实状态无效。')
        if not isinstance(row['alignment_origin'],str) or row['alignment_origin'] not in ALIGNMENT_ORIGINS:
            raise ValueError('实际时间线定位来源无效。')
        if not isinstance(row['alignment_basis'],dict):raise ValueError('实际时间线定位依据必须是对象。')
        if not isinstance(row['alignment_invalid_reason'],str):raise ValueError('实际时间线失效原因无效。')
        try:
            encoded=json.dumps(row['alignment_basis'],ensure_ascii=False,allow_nan=False)
            if len(encoded)>65536:raise ValueError('定位依据过大')
        except (ValueError,TypeError,OverflowError) as exc:raise ValueError('实际时间线定位依据包含无效数据。') from exc
        if type(row['organize_initialized']) is not bool:raise ValueError('台本整理初始化状态无效。')
        first,last=row['organize_first_line'],row['organize_last_line']
        if (first is None) != (last is None):raise ValueError('台本区间的起止编号必须同时填写。')
        if first is not None:
            if type(first) is not int or type(last) is not int or not 1 <= first <= last:
                raise ValueError('台本区间必须是有效的正整数编号。')
            if (row['organize_pool']=='track' and type(source.get('script_line_count')) is int
                    and last > source['script_line_count']):
                raise ValueError('台本区间超出了当前台本的编号范围。')
        if type(row['organize_reviewed']) is not bool:raise ValueError('台本区间核实状态无效。')
        if row['organize_pool'] not in ORGANIZER_POOLS:raise ValueError('台本整理媒体池状态无效。')
        if row['organize_pool']=='track' and first is None:raise ValueError('轨道上的PGM必须指定台本区间。')
        if row['organize_pool']!='track':row['organize_reviewed']=False
        if type(row.get('hidden',False)) is not bool:raise ValueError('素材隐藏状态无效。')
        row['hidden']=row.get('hidden',False)
        if row.get('start_seconds') is not None:
            row['start_seconds']=_finite(row['start_seconds'],'备选位置')
            if source['session_id']==baseline_id:raise ValueError('基准场次已保留，不能再次作为备选放置。')
            if baseline_id is None:raise ValueError('请先确定基准时间线再放置备选。')
            if not source.get('ready'):raise ValueError('PGM时长或帧率信息不完整，请补充素材信息后再放置。')
        else:
            row['alignment_reviewed']=False
        if row['hidden']:row['alignment_reviewed']=False
    for identity,row in list(result['items'].items()):
        result['items'][identity]={key:row.get(key) for key in ('session_id','pgm_path','start_seconds','lane',
            'organize_seconds','organize_lane','hidden','organize_first_line','organize_last_line',
            'organize_reviewed','organize_pool','organize_initialized','alignment_reviewed',
            'alignment_origin','alignment_basis','alignment_invalid_reason')}
    _check_collisions(_intervals(result,inv))
    _check_collisions(_intervals(result,inv,'organize'))
    return result


def _move(layout, ids, changes, rows, baseline_id, auto_lane, mode):
    inv=_inventory(rows);result=validate_layout(layout,rows,baseline_id)
    ids=list(dict.fromkeys(ids));field='organize_seconds' if mode=='organize' else 'start_seconds'
    lane_field='organize_lane' if mode=='organize' else 'lane'
    if not ids or any(identity not in result['items'] for identity in ids):raise ValueError('请先选择有效PGM素材。')
    original={identity:(result['items'][identity].get('start_seconds'),result['items'][identity]['lane'])
              for identity in ids}
    proposed=[]
    for identity in ids:
        row=result['items'][identity]
        start,lane=changes(identity,row)
        row[field]=_finite(start,'整理位置' if mode=='organize' else '备选位置',mode=='organize')
        row[lane_field]=_lane(lane,'轨道')
        row['hidden']=False
        if mode=='timeline' and (row['session_id']==baseline_id or baseline_id is None or not inv[identity].get('ready')):
            raise ValueError('请先确定基准，并选择具有完整时长信息的非基准PGM。')
        proposed.append((identity,row[field],row[lane_field],inv[identity].get('duration_seconds')))
    # Detect internal collisions first; a uniform group shift cannot fix these.
    own=defaultdict(list)
    for identity,start,lane,duration in proposed:
        if duration is not None:own[lane].append((start,start+duration,identity))
    _check_collisions(own)
    occupied=_intervals(result,inv,mode,skip=set(ids));shift=0
    while True:
        collision=any(any(start<end-1e-9 and begin<start+duration-1e-9 for begin,end,_ in occupied.get(lane+shift,[]))
                      for _identity,start,lane,duration in proposed if duration is not None)
        if not collision:break
        if not auto_lane:raise ValueError('目标轨道存在PGM片段，请使用其他轨道。')
        shift+=1
    for identity,_start,_lane_number,_duration in proposed:
        result['items'][identity][lane_field]+=shift
        item=result['items'][identity]
        if mode=='timeline' and (item.get('start_seconds'),item['lane']) != original[identity]:
            _mark_manual_alignment(item,baseline_id)
    return validate_layout(result,rows,baseline_id)


def _move_organizer(layout, ids, delta_lane, rows, baseline_id, auto_lane):
    """Change vertical organization only; horizontal edits name script lines."""
    inv=_inventory(rows);result=validate_layout(layout,rows,baseline_id)
    ids=list(dict.fromkeys(ids))
    if not ids or any(identity not in result['items'] for identity in ids):raise ValueError('请先选择有效PGM素材。')
    for identity in ids:
        item=result['items'][identity]
        item['organize_lane']=_lane(item['organize_lane']+delta_lane,'整理轨道')
    _raise_script_group(result,ids,inv,auto_lane)
    return validate_layout(result,rows,baseline_id)


def _raise_script_group(layout, ids, inv, auto_lane=True):
    """Uniformly raise an already internally valid script-range group."""
    ids=set(ids)
    own=_intervals(layout,inv,'organize',skip=set(layout['items'])-ids)
    _check_collisions(own)
    occupied=_intervals(layout,inv,'organize',skip=ids)
    shift=0
    while any(any(start<other_end and other_start<end
                  for other_start,other_end,_ in occupied.get(lane+shift,[]))
              for lane,group in own.items() for start,end,_ in group):
        if not auto_lane:raise ValueError('目标台本轨道存在PGM片段，请使用其他轨道。')
        shift+=1
    for identity in ids:layout['items'][identity]['organize_lane']+=shift


def set_script_range(layout, ids, first, last, inventory, baseline_id=None, *, reviewed=True, pool='track'):
    """Explicitly edit organizer coverage, never source or target positions."""
    result=validate_layout(layout,inventory,baseline_id);inv=_inventory(inventory)
    ids=list(dict.fromkeys(ids))
    if not ids or any(identity not in result['items'] for identity in ids):raise ValueError('请先选择有效PGM素材。')
    if type(first) is not int or type(last) is not int or not 1 <= first <= last:
        raise ValueError('台本区间必须是有效的正整数编号。')
    if type(reviewed) is not bool or pool not in ORGANIZER_POOLS:raise ValueError('台本核实或媒体池状态无效。')
    # A group explicitly assigned the same range needs distinct script lanes.
    # Retain every already distinct relative vertical lane, including gaps.
    reserved={result['items'][identity]['organize_lane'] for identity in ids};used=set()
    extra=max(reserved,default=0)+1
    for identity in ids:
        source=inv[identity];item=result['items'][identity]
        if source.get('script_line_count') and last > source['script_line_count']:
            raise ValueError('台本区间超出了当前台本的编号范围。')
        previous_pool=item['organize_pool']
        previous_range=(item.get('organize_first_line'),item.get('organize_last_line'))
        item.update(organize_first_line=first,organize_last_line=last,
            organize_reviewed=reviewed if pool=='track' else False,organize_pool=pool,organize_initialized=True)
        if pool=='track':
            if previous_pool in ('removed','hidden'):item['start_seconds']=None
            item['hidden']=False
            if item['organize_lane'] in used:
                while extra in used or extra in reserved:extra+=1
                item['organize_lane']=extra;extra+=1
            used.add(item['organize_lane'])
        elif pool in ('removed','hidden'):
            item['start_seconds']=None
        if previous_range!=(first,last) and item.get('start_seconds') is not None:
            item.update(alignment_reviewed=False,alignment_invalid_reason='台本区间已改变，请核对实际位置。')
        if item.get('start_seconds') is None:
            item.update(alignment_reviewed=False,alignment_origin='legacy',alignment_basis={},alignment_invalid_reason='')
    _raise_script_group(result,ids,inv)
    return validate_layout(result,inventory,baseline_id)


def move_items(layout, ids, delta_seconds=0., delta_lane=0, inventory=None, baseline_id=None, *, auto_lane=True, mode='timeline'):
    if mode not in ('timeline','organize'):raise ValueError('时间线模式无效。')
    delta=_finite(delta_seconds,'移动时长')
    if type(delta_lane) is not int:raise ValueError('移动轨道数必须是整数。')
    if mode=='organize':
        if delta != 0:raise ValueError('整理横轴使用台本编号，请通过修改台本区间调整水平位置。')
        return _move_organizer(layout,ids,delta_lane,inventory,baseline_id,auto_lane)
    field='organize_seconds' if mode=='organize' else 'start_seconds';lane='organize_lane' if mode=='organize' else 'lane'
    def change(_identity,row):
        if row.get(field) is None:raise ValueError('未放置的片段请先指定位置。')
        return row[field]+delta,row[lane]+delta_lane
    return _move(layout,ids,change,inventory,baseline_id,auto_lane,mode)


def place_items(layout, ids, start_seconds, lane, inventory, baseline_id=None, *, auto_lane=True, mode='timeline'):
    if mode not in ('timeline','organize'):raise ValueError('时间线模式无效。')
    if mode=='organize':raise ValueError('整理横轴使用台本编号，请通过修改台本区间放置素材。')
    ids=list(dict.fromkeys(ids));items=layout.get('items') or {}
    if not ids or any(identity not in items for identity in ids):raise ValueError('请先选择有效PGM素材。')
    field='organize_seconds' if mode=='organize' else 'start_seconds';lane_field='organize_lane' if mode=='organize' else 'lane'
    origins=[items[identity].get(field) for identity in ids]
    staged_group = mode=='timeline' and all(value is None for value in origins)
    if staged_group:
        # Script line differences are not elapsed time. A staged group starts
        # at one explicitly requested approximate target in separate lanes.
        origin_field,lane_origin=None,'organize_lane'
        origins=[]
    else:
        origin_field,lane_origin=field,lane_field
    anchor=min(value for value in origins if value is not None) if any(value is not None for value in origins) else 0.
    first_lane=min(items[identity].get(lane_origin,1) for identity in ids)
    start_seconds=_finite(start_seconds,'放置位置',mode=='organize');lane=_lane(lane,'轨道')
    targets={identity:lane+items[identity].get(lane_origin,1)-first_lane for identity in ids}
    if staged_group:
        reserved=set(targets.values());used=set();extra=max(reserved)+1
        for identity in ids:
            if targets[identity] in used:
                while extra in used or extra in reserved:extra+=1
                targets[identity]=extra;extra+=1
            used.add(targets[identity])
    return _move(layout,ids,lambda identity,row:(start_seconds+(row.get(origin_field)-anchor
        if origin_field is not None and row.get(origin_field) is not None else 0.),targets[identity]),
        inventory,baseline_id,auto_lane,mode)


def confirm_alignment(layout, ids, inventory, baseline_id=None, *, reviewed=True):
    """Confirm existing actual positions without moving or trimming any take."""
    if type(reviewed) is not bool:raise ValueError('实际时间线核实状态无效。')
    result=validate_layout(layout,inventory,baseline_id);inv=_inventory(inventory)
    ids=list(dict.fromkeys(ids))
    if not ids or any(identity not in result['items'] for identity in ids):raise ValueError('请先选择有效PGM素材。')
    for identity in ids:
        item=result['items'][identity]
        if (item.get('start_seconds') is None or item['session_id']==baseline_id or item.get('hidden')
                or not inv[identity].get('ready')):
            raise ValueError('请选择已放置的有效备选片段进行核实。')
        item['alignment_reviewed']=reviewed
        item['alignment_invalid_reason']='' if reviewed else '已取消实际位置核实。'
    return validate_layout(result,inventory,baseline_id)


def invalidate_alignment(layout, ids, inventory, baseline_id=None, *, reason='定位依据已改变，请重新核对。', automatic_only=False):
    """Keep actual coordinates while invalidating their independent review."""
    if not isinstance(reason,str) or type(automatic_only) is not bool:raise ValueError('定位失效设置无效。')
    result=validate_layout(layout,inventory,baseline_id)
    ids=list(result['items']) if ids is None else list(dict.fromkeys(ids))
    if any(identity not in result['items'] for identity in ids):raise ValueError('定位失效引用了未登记PGM。')
    for identity in ids:
        item=result['items'][identity]
        if item.get('start_seconds') is None or automatic_only and item['alignment_origin']!='automatic':continue
        item.update(alignment_reviewed=False,alignment_invalid_reason=reason)
    return result


def apply_alignment_proposals(layout, proposals, inventory, baseline_id=None, *, replace_automatic=False):
    """Place blue proposals, retaining human and legacy actual coordinates.

    Explicit replacement only repositions unreviewed automatic takes. Record
    seconds never rewrite source IN/OUT, TC or original media metadata.
    """
    if type(replace_automatic) is not bool:raise ValueError('自动重新定位设置无效。')
    if not isinstance(proposals,(list,tuple)):raise ValueError('自动定位提案必须是列表。')
    result=validate_layout(layout,inventory,baseline_id);inv=_inventory(inventory)
    if baseline_id is None:raise ValueError('请先确定基准时间线。')
    pending=[];seen=set()
    for proposal in proposals:
        if not isinstance(proposal,dict):raise ValueError('自动定位提案格式无效。')
        identity=proposal.get('take_id') or proposal.get('id')
        if not isinstance(identity,str) or identity not in result['items']:raise ValueError('自动定位提案引用了未登记PGM。')
        if identity in seen:raise ValueError('同一PGM存在重复自动定位提案。')
        seen.add(identity);item=result['items'][identity];source=inv[identity]
        if ((proposal.get('session_id') is not None and proposal['session_id']!=source['session_id']) or
                (proposal.get('pgm_path') is not None and _path(proposal['pgm_path'])!=source['pgm_path'])):
            raise ValueError('自动定位提案的场次或路径与素材不一致。')
        if (item['session_id']==baseline_id or item.get('hidden') or item['organize_pool']!='track'
                or not item['organize_reviewed'] or not source.get('ready')):continue
        if item.get('start_seconds') is not None and not (replace_automatic and
                item['alignment_origin']=='automatic' and not item['alignment_reviewed']):continue
        basis=copy.deepcopy(proposal.get('alignment_basis') or {})
        if not isinstance(basis,dict):raise ValueError('自动定位依据必须是对象。')
        for field in ('mapping_fingerprint','first_line','last_line','baseline_segment_id','baseline_anchor_frame',
                'source_anchor_seconds','alignment_source','alignment_reason','alignment_confidence','confidence','approximate','reason'):
            if field in proposal:basis[field]=copy.deepcopy(proposal[field])
        for field,current in (('first_line',item['organize_first_line']),('last_line',item['organize_last_line'])):
            if field in basis and basis[field]!=current:raise ValueError('自动定位所用台本区间已改变，请重新生成提案。')
            basis[field]=current
        if basis.get('baseline_session_id',baseline_id)!=baseline_id:
            raise ValueError('自动定位依据不属于当前基准场次。')
        basis['baseline_session_id']=baseline_id
        start=_finite(proposal.get('start_seconds'),'自动定位位置')
        lane=_lane(proposal.get('lane',item.get('lane',1)),'自动定位轨道')
        pending.append((start,lane,identity,basis))
    occupied={lane:sorted((first,last) for first,last,_ in ranges) for lane,ranges in
              _intervals(result,inv,skip={row[2] for row in pending}).items()}
    for start,lane,identity,basis in sorted(pending,key=lambda row:(row[0],row[1],row[2])):
        end=start+inv[identity]['duration_seconds']
        while True:
            ranges=occupied.setdefault(lane,[]);position=bisect_left(ranges,(start,-math.inf))
            if ((position==0 or ranges[position-1][1]<=start+1e-9) and
                    (position==len(ranges) or end<=ranges[position][0]+1e-9)):
                ranges.insert(position,(start,end));break
            lane+=1
        result['items'][identity].update(start_seconds=start,lane=lane,alignment_reviewed=False,
            alignment_origin='automatic',alignment_basis=basis,alignment_invalid_reason='')
    return validate_layout(result,inventory,baseline_id)


def build_manual_plan(data, baseline_id, layout, cuts):
    from reference_timeline import _cut,ReferenceTimelineError
    sessions={row['id']:row for row in data.get('sessions',[])}
    if baseline_id not in sessions:raise ReferenceTimelineError('请先确定基准时间线。')
    base=_cut(cuts,baseline_id);rows=inventory(data,cuts);inv=_inventory(rows)
    try:layout=validate_layout(layout,rows,baseline_id)
    except ValueError as exc:raise ReferenceTimelineError(str(exc)) from exc
    selected=[]
    for identity,item in layout['items'].items():
        if item.get('hidden') or item.get('start_seconds') is None or item['session_id']==baseline_id:continue
        source=inv[identity];cut=_cut(cuts,item['session_id'])
        if cut['fps']!=base['fps']:raise ReferenceTimelineError('基准与备选帧率必须一致，不进行变速。')
        block=next((row for row in cut['pgm_sources'] if row['path']==source['pgm_path']),None)
        if block is None:raise ReferenceTimelineError('备选PGM不在该场次切点XML中。')
        # Round only at interchange export; target seconds never modify source.
        value=Fraction(str(item['start_seconds']))*base['fps']
        target=math.floor(value+Fraction(1,2));duration=block['frames']
        selected.append(dict(candidate_id=identity,take_id=identity,session_id=item['session_id'],
            pgm_path=source['pgm_path'],fps=base['fps'],pgm_frame_in=0,pgm_frame_out_exclusive=duration,
            record_frame_in=block['start'],record_frame_out_exclusive=block['end'],
            target_frame_in=target,target_frame_out_exclusive=target+duration,duration_frames=duration,
            lane=item['lane'],whole_pgm=True,session_name=sessions[item['session_id']].get('name',''),
            script_line_id=None,script_text='',script_order=0,baseline_candidate_id=None,
            baseline_transcript_id=None,dialogue_pgm_frame_in=None,baseline_dialogue_frame_in=None))
    selected.sort(key=lambda row:(row['target_frame_in'],row['lane'],row['session_name'],row['candidate_id']))
    occupied=defaultdict(list)
    for row in selected:occupied[row['lane']].append((row['target_frame_in'],row['target_frame_out_exclusive'],row['candidate_id']))
    try:_check_collisions(occupied)
    except ValueError as exc:raise ReferenceTimelineError('导出取整后'+str(exc)) from exc
    first=min([0]+[row['target_frame_in'] for row in selected]);last=max([base['duration']]+[row['target_frame_out_exclusive'] for row in selected])
    return dict(version=2,manual_layout=True,baseline_session_id=baseline_id,baseline_session_name=sessions[baseline_id].get('name',''),
        fps=base['fps'],baseline_duration_frames=base['duration'],timeline_start_frame=first,timeline_end_frame=last,
        duration_frames=last-first,lane_count=max([0]+[row['lane'] for row in selected]),selections=selected,warnings=[],
        policy='基准保留V1；备选位置由人工实际时间线确定，保留整条PGM、原速、源时码和原始音频。')
