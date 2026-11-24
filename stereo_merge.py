"""Lossless split-mono BWF → derived stereo WAV, with original provenance.

Only explicitly paired, sample-identical L/R timelines are interleaved. Source
files stay read-only. Cache entries publish as a directory, never half a pair.
"""
from __future__ import annotations
import copy,errno,hashlib,json,os,shutil,struct,tempfile,uuid
from pathlib import Path
from fractions import Fraction as F
import xml.etree.ElementTree as ET
from core import AnalysisError

VERSION=1
CHUNK_SAMPLES=131072
MAX_METADATA=4*1024*1024
IDENTITY_TAGS={'FILE_UID','ORIGINAL_FILENAME','PARENT_FILENAME','PARENT_UID','FILE_SET_INDEX','CHANNEL_INDEX','INTERLEAVE_INDEX','TRACK_LIST','TRACK_COUNT'}

def read_wave_layout(source,file_size,name='WAV'):
    """Read RIFF/RF64 chunk geometry without reading PCM or allocating its size."""
    source.seek(0);header=source.read(12)
    if len(header)!=12 or header[:4]not in(b'RIFF',b'RF64')or header[8:]!=b'WAVE':raise AnalysisError('不是支持的RIFF/RF64 WAV：'+name)
    rf64=header[:4]==b'RF64';data64=None;riff64=None;tables={};rows=[]
    while source.tell()+8<=file_size:
        key=source.read(4);size=struct.unpack('<I',source.read(4))[0];position=source.tell()
        if key==b'ds64':
            if not rf64 or size<28 or size>MAX_METADATA:raise AnalysisError('RF64的ds64块无效：'+name)
            if position+size>file_size:raise AnalysisError('RF64长度表不完整：'+name)
            raw=source.read(size);riff64,data64,_,count=struct.unpack_from('<QQQI',raw)
            if 28+12*count>len(raw):raise AnalysisError('RF64长度表损坏：'+name)
            for n in range(count):
                identity,value=struct.unpack_from('<4sQ',raw,28+n*12);tables.setdefault(identity,[]).append(value)
        else:
            if size==0xffffffff:
                if not rf64 or data64 is None:raise AnalysisError('RF64缺少长度表：'+name)
                if key==b'data':size=data64
                elif tables.get(key):size=tables[key].pop(0)
                else:raise AnalysisError('RF64块缺少64位长度：'+name)
            if position+size>file_size:raise AnalysisError('WAV数据不完整：'+name)
            rows.append((key,position,size));source.seek(position+size)
        if size%2:source.seek(1,1)
    if rf64 and (riff64 is None or riff64+8>file_size):raise AnalysisError('RF64文件长度无效：'+name)
    return rows

def _cancel(cancelled):
    if cancelled():raise AnalysisError('已取消左右声道合并；原始文件未修改。')

def _signature(path):
    s=path.stat();return [str(path),s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns]

def _sha(path,cancelled=lambda:False):
    h=hashlib.sha256()
    with path.open('rb')as source:
        while block:=source.read(2*1024*1024):_cancel(cancelled);h.update(block)
    return h.hexdigest()

def _read_chunks(path):
    with path.open('rb')as source:
        rows=read_wave_layout(source,path.stat().st_size,path.name);chunks={};data=[]
        for key,offset,size in rows:
            if key==b'data':data.append((offset,size));continue
            if key in(b'fmt ',b'bext',b'iXML',b'LIST'):
                if size>MAX_METADATA:raise AnalysisError('WAV元数据块过大：'+path.name)
                source.seek(offset);raw=source.read(size)
                if key==b'LIST':chunks.setdefault(key,[]).append(raw)
                else:
                    if key in chunks:raise AnalysisError('WAV元数据块重复：'+path.name)
                    chunks[key]=raw
    if len(data)!=1:raise AnalysisError('左右声道WAV必须只有一个data块：'+path.name)
    return chunks,data[0]

def _common_ixml(left,right,uid,name):
    def parse(raw):
        if not raw:return None
        try:return ET.fromstring(raw.rstrip(b'\0'))
        except ET.ParseError as exc:raise AnalysisError('左右声道iXML损坏，未合并。')from exc
    a,b=parse(left),parse(right)
    def shared(a,b):
        if a.tag!=b.tag or a.tag.rsplit('}',1)[-1]in IDENTITY_TAGS:return None
        out=ET.Element(a.tag,{k:v for k,v in a.attrib.items()if b.attrib.get(k)==v})
        if not len(a)and not len(b):
            text=(a.text or '').strip()
            if text!=(b.text or '').strip()or not text:return None
            out.text=text;return out
        used=set()
        for child in a:
            for n,other in enumerate(b):
                if n in used or child.tag!=other.tag:continue
                value=shared(child,other);used.add(n)
                if value is not None:out.append(value)
                break
        return out if len(out)else None
    root=shared(a,b)if a is not None and b is not None else None
    if root is None:root=ET.Element('BWFXML')
    ET.SubElement(root,'FILE_UID').text=uid;ET.SubElement(root,'ORIGINAL_FILENAME').text=name
    return ET.tostring(root,encoding='utf-8',xml_declaration=True)

def _common_bext(left,right,uid):
    if not left or not right:return None
    if min(len(left),len(right))<346:raise AnalysisError('左右声道BEXT元数据不完整。')
    out=bytearray(602)
    for first,last in ((0,256),(256,288),(320,330),(330,338)):
        if left[first:last]==right[first:last]:out[first:last]=left[first:last]
    out[288:320]=uid.replace('-','').encode('ascii')[:32]
    out[338:346]=left[338:346];out[346:348]=struct.pack('<H',1)
    # New file identity: never assign an original mono file's UMID to the merge.
    history=left[602:]if left[602:]==right[602:]else b''
    return bytes(out)+history

def _wave_header(chunks,data_size,samples,force_rf64=False):
    body=b''.join(key+struct.pack('<I',len(raw))+raw+(b'\0'if len(raw)%2 else b'')for key,raw in chunks)
    riff_size=4+len(body)+8+data_size+(data_size%2);rf64=force_rf64 or max(riff_size,data_size)>0xffffffff
    if rf64:
        riff_size+=36;ds=struct.pack('<QQQI',riff_size,data_size,samples,0)
        return b'RF64\xff\xff\xff\xffWAVEds64'+struct.pack('<I',28)+ds+body+b'data\xff\xff\xff\xff'
    return b'RIFF'+struct.pack('<I',riff_size)+b'WAVE'+body+b'data'+struct.pack('<I',data_size)

def _load_entry(folder,key,signatures):
    try:
        side=json.loads((folder/'stereo_merge.json').read_text(encoding='utf-8'));path=folder/side['filename']
        if side.get('version')!=VERSION or side.get('key')!=key or (signatures is not None and side.get('source_signatures')!=signatures) or not path.is_file():return None
        if side.get('wav_stat')!=_signature(path)[1:]:return None
        from sound_sync import probe_wav
        info=probe_wav(path);assert info['channels']==2 and info['samples']==side['samples']
        return dict(info,source_kind='wav',channel='main',sync_report=[],stereo_merge_sidecar=str(folder/'stereo_merge.json'),merged_from=copy.deepcopy(side['originals']))
    except (OSError,ValueError,KeyError,AssertionError):return None

def merge_stereo_pair(left_path,right_path,cache_dir,*,fps=25,cancelled=lambda:False,progress=lambda n,m:None,force_rf64=False):
    """Return a new header row; common BWF metadata survives, originals do not change."""
    from sound_sync import probe_wav
    left,right=Path(left_path).expanduser().resolve(),Path(right_path).expanduser().resolve()
    if left==right:raise AnalysisError('左右声道不能使用同一个WAV文件。')
    _cancel(cancelled);infos=[probe_wav(p,fps=fps)for p in(left,right)]
    for key,label in(('channels','声道'),('sample_rate','采样率'),('depth','位深'),('samples','采样长度'),('has_timecode','时码状态'),('start','源时码')):
        if infos[0].get(key)!=infos[1].get(key)or(key=='channels'and infos[0][key]!=1):raise AnalysisError('左右声道'+label+'不一致，未合并。')
    for field in('sound_roll','scene','take','recording_date','recording_time'):
        a,b=(str(info.get(field)or'').strip()for info in infos)
        if a and b and a!=b:raise AnalysisError('左右声道元数据冲突（'+field+'），未合并。')
    signatures=[_signature(p)for p in(left,right)];cache=Path(cache_dir).expanduser().resolve();cache.mkdir(parents=True,exist_ok=True)
    indexkey=hashlib.sha256(json.dumps([VERSION,signatures,bool(force_rf64)],separators=(',',':')).encode()).hexdigest();index=cache/('pair-'+indexkey+'.json')
    try:
        saved=json.loads(index.read_text());folder=cache/saved['key'];hit=_load_entry(folder,saved['key'],signatures)
        if hit:return hit
    except (OSError,ValueError,KeyError):pass
    hashes=[_sha(p,cancelled)for p in(left,right)];key=hashlib.sha256(json.dumps([VERSION,list(map(str,(left,right))),hashes,bool(force_rf64)],separators=(',',':')).encode()).hexdigest();folder=cache/key
    hit=_load_entry(folder,key,None)
    if hit:return hit
    chunks=[_read_chunks(p)for p in(left,right)];samples=infos[0]['samples'];width=infos[0]['depth']//8;size=samples*width*2
    uid=str(uuid.uuid5(uuid.NAMESPACE_OID,'pgm-stereo-v1:'+key));name='STEREO_'+key[:24]+'.wav'
    fmt=struct.pack('<HHIIHH',1,2,infos[0]['sample_rate'],infos[0]['sample_rate']*width*2,width*2,infos[0]['depth'])
    metadata=[(b'fmt ',fmt)];bext=_common_bext(chunks[0][0].get(b'bext'),chunks[1][0].get(b'bext'),uid)
    if bext:metadata.append((b'bext',bext))
    metadata.append((b'iXML',_common_ixml(chunks[0][0].get(b'iXML'),chunks[1][0].get(b'iXML'),uid,name)))
    for raw in chunks[0][0].get(b'LIST',[]):
        if raw in chunks[1][0].get(b'LIST',[]):metadata.append((b'LIST',raw))
    temp=Path(tempfile.mkdtemp(prefix='.stereo-',dir=cache))
    try:
        with (temp/name).open('xb')as out,left.open('rb')as l,right.open('rb')as r:
            out.write(_wave_header(metadata,size,samples,force_rf64));l.seek(chunks[0][1][0]);r.seek(chunks[1][1][0]);done=0
            while done<samples:
                _cancel(cancelled);count=min(CHUNK_SAMPLES,samples-done);a=l.read(count*width);b=r.read(count*width)
                if len(a)!=count*width or len(b)!=count*width:raise AnalysisError('左右原WAV在合并中发生长度变化，未发布。')
                packed=bytearray(count*width*2)
                for byte in range(width):packed[byte::width*2]=a[byte::width];packed[width+byte::width*2]=b[byte::width]
                out.write(packed);done+=count;progress(round(done/samples*100),'合并左右声道：'+left.name)
            out.flush();os.fsync(out.fileno())
        if signatures!=[_signature(p)for p in(left,right)]:raise AnalysisError('左右原WAV在合并中发生变化，未发布。')
        originals=[dict(role=role,path=str(path),filename=path.name,sha256=digest,file_uid=info.get('file_uid',''),umid=info.get('umid',''),originator_reference=info.get('originator_reference',''),metadata=info.get('metadata',{}))for role,path,digest,info in zip(('L','R'),(left,right),hashes,infos)]
        side=dict(version=VERSION,key=key,filename=name,new_file_uid=uid,samples=samples,sample_rate=infos[0]['sample_rate'],depth=infos[0]['depth'],source_signatures=signatures,originals=originals,wav_sha256=_sha(temp/name,cancelled),wav_stat=_signature(temp/name)[1:],policy='Lossless sample interleave; common BWF metadata only; original files unchanged; sidecar provides original identities, not an AAF metadata guarantee.')
        (temp/'stereo_merge.json').write_text(json.dumps(side,ensure_ascii=False,indent=2),encoding='utf-8');_cancel(cancelled)
        try:temp.rename(folder)
        except OSError as exc:
            if exc.errno not in (errno.EEXIST,errno.ENOTEMPTY):raise
            hit=_load_entry(folder,key,None)
            if not hit:raise AnalysisError('立体声缓存同名内容冲突，未覆盖：'+str(folder))
        pointer=cache/('.pair-'+uuid.uuid4().hex+'.json');pointer.write_text(json.dumps(dict(key=key)));os.replace(pointer,index)
        result=_load_entry(folder,key,None)
        if not result:raise AnalysisError('立体声缓存发布后校验失败。')
        return result
    finally:
        if temp.exists():shutil.rmtree(temp)

def merge_sound_plan(sound,cache_dir,*,fps=25,cancelled=lambda:False,progress=lambda n,m:None):
    """Replace exact L/R event pairs by one stereo source, preserving sample IN."""
    if sound is None:return sound
    from sound_fallback import AudioSources,sound_reports
    wavs,events=sound;by_path={w['path']:w for w in wavs};groups={};singles=[]
    for index,event in enumerate(events):
        if not event.get('pair_id'):singles.append((index,copy.deepcopy(event)));continue
        key=(event['pair_id'],event.get('pgm_path',''),F(event['record_start']),F(event['source_start']),F(event['duration']))
        groups.setdefault(key,[]).append((index,event))
    if not groups:return copy.deepcopy(sound)
    merged={};rows=[];new_sources={}
    for key,members in groups.items():
        roles={str(e.get('out_channel')or e.get('channel')):(i,e)for i,e in members}
        if len(members)!=2 or set(roles)!={'L','R'}:raise AnalysisError('已合板左右声道必须一L一R，且采样入点、时长完全一致；未自动合并。')
        left,right=roles['L'][1],roles['R'][1];pair=(left['path'],right['path'])
        if pair not in merged:merged[pair]=merge_stereo_pair(*pair,cache_dir,fps=fps,cancelled=cancelled,progress=progress)
        header=merged[pair]
        if not source_start_equal(header,by_path[left['path']]):raise AnalysisError('合并后的BWF源时码与原始左右声道不一致，未输出。')
        event=copy.deepcopy(left);event.update(path=header['path'],channel='main',source_kind='wav',src_channel='1,2')
        for field in('pair_id','out_channel','alignment_source_path'):event.pop(field,None)
        new_sources[header['path']]=header;rows.append((min(i for i,_ in members),event))
    for index,event in singles:
        new_sources[event['path']]=dict(copy.deepcopy(by_path[event['path']]),sync_report=[])
        rows.append((index,event))
    rows.sort();reports=copy.deepcopy(sound_reports(wavs));result=AudioSources(new_sources.values(),unattached_reports=reports)
    return result,[event for _,event in rows]

def source_start_equal(a,b):
    from sound_fallback import source_start
    return source_start(a)==source_start(b)
