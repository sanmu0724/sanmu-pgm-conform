"""Private original-WAV AAF import bridge; no rendered media or public AAF export."""
from pathlib import Path
from fractions import Fraction as F
import json,os,xml.etree.ElementTree as ET
from urllib.parse import urlparse,unquote

_PCM_SUFFIXES={'.wav','.wave','.aif','.aiff','.aifc'}

def _actual_pcm_depth(path):
 """Read the declared PCM depth; never infer it from a filename or defaults."""
 if Path(path).suffix.lower() in ('.wav','.wave'):
  from sound_sync import probe_wav
  return probe_wav(path)['depth']
 # Rough-cut XMLs may contain editor-added AIFF music. Preserve that source,
 # including its endian-aware native decoder, instead of silently omitting it.
 import struct
 with Path(path).open('rb') as source:
  header=source.read(12)
  if len(header)!=12 or header[:4]!=b'FORM' or header[8:] not in (b'AIFF',b'AIFC'):
   raise ValueError('Not a supported original PCM AIFF: '+str(path))
  limit=8+struct.unpack('>I',header[4:8])[0]
  while source.tell()+8<=limit:
   chunk=source.read(8)
   if len(chunk)!=8:break
   tag,size=chunk[:4],struct.unpack('>I',chunk[4:])[0]
   if source.tell()+size>limit:break
   if tag==b'COMM':
    body=source.read(min(size,22))
    if len(body)<18:break
    if header[8:]==b'AIFC' and (len(body)<22 or body[18:22] not in (b'NONE',b'twos',b'sowt')):
     raise ValueError('Compressed AIFF audio requires a supported PCM source: '+str(path))
    return struct.unpack('>H',body[6:8])[0]
   source.seek(size+(size%2),1)
 raise ValueError('AIFF has no valid PCM COMM header: '+str(path))

def seconds(value):return F(value[:-1])if isinstance(value,str)and value.endswith('s')else F(str(value))
def integer(value,label):
 value=F(value)
 if value.denominator!=1:raise ValueError(label+' is not an exact integer')
 return int(value)
def file_path(url):
 p=urlparse(url or'')
 if p.scheme!='file'or p.netloc not in('','localhost'):raise ValueError('Audio must reference a local/mounted WAV URI')
 return str(Path(unquote(p.path)).expanduser().resolve())
def plan_audio(xml_path,manifest,guide_audio=(),allow_probe=False):
 xml_path=Path(xml_path);root=ET.parse(xml_path).getroot();fps=F(manifest['fps']);origin=F(manifest['timeline_start']);end=F(manifest['timeline_end']);rate=48000
 headers={};depths={};used_paths={str(Path(a['path']).expanduser().resolve()) for g in manifest['groups'] for a in g.get('audio',[])}
 for row in guide_audio:
  if row.get('audio_bit_depth')is not None:
   key=str(Path(row['path']).expanduser().resolve());depth=integer(row['audio_bit_depth'],'Bit depth')
   if key in depths and depths[key]!=depth:raise ValueError('Conflicting depth for '+key)
   depths[key]=depth
 sides=[xml_path.parent/'sound_sources.json']+list((xml_path.parent/'sessions').glob('*/sound_sources.json'))
 for side in sides:
  if not side.is_file():continue
  for row in json.loads(side.read_text()).get('sources',[]):
   if not row.get('path'):continue
   key=str(Path(row['path']).expanduser().resolve())
   if row.get('depth')is not None:
    depth=integer(row['depth'],'Bit depth')
    if key in depths and depths[key]!=depth:raise ValueError('Conflicting source header depth for '+key)
    depths[key]=depth
 for asset in root.findall('./resources/asset'):
  if asset.get('hasAudio')!='1':continue
  url=asset.get('src')or next((r.get('src')for r in asset.findall('media-rep')if r.get('kind')=='original-media'),None)
  if not url:continue
  path=file_path(url)
  if path not in used_paths:continue
  if Path(path).suffix.lower()not in _PCM_SUFFIXES:continue
  metadata={m.get('key'):m.get('value')for m in asset.findall('./metadata/md')};depth=metadata.get('com.pgm.audioBitDepth')or depths.get(path)
  if metadata.get('com.pgm.audioBitDepth') is not None and path in depths and integer(metadata['com.pgm.audioBitDepth'],'Bit depth')!=depths[path]:raise ValueError('Conflicting XML/source bit depth: '+path)
  if depth is None and allow_probe:
   depth=_actual_pcm_depth(path)
  if depth is None:raise ValueError('Missing actual original WAV bit depth: '+path)
  depth=integer(depth,'Bit depth');channels=integer(asset.get('audioChannels','0'),'Channels');sample_rate=integer(asset.get('audioRate','0'),'Sample rate')
  if depth not in(16,24,32)or channels not in(1,2)or sample_rate!=48000:raise ValueError('Unsupported original WAV PCM format: '+path)
  row=dict(path=path,samples=integer(seconds(asset.get('duration'))*rate,'Source duration samples'),start=integer(seconds(asset.get('start','0s'))*rate,'Source TimeReference samples'),sample_rate=rate,channels=channels,depth=depth,metadata=metadata,file_uid=metadata.get('com.pgm.fileUID',''))
  if path in headers:
   previous=headers[path]
   if any(previous[field]!=row[field]for field in('samples','start','sample_rate','channels','depth')):raise ValueError('Conflicting source asset identity: '+path)
   for field in('com.pgm.fileUID','com.pgm.umid'):
    a,b=previous['metadata'].get(field),metadata.get(field)
    if a and b and a!=b:raise ValueError('Conflicting original WAV UID: '+path)
   # Scene ownership/notes can differ for the same physical WAV. Such text
   # is not a new sound source; retain only mutually identical comments.
   shared={k:v for k,v in previous['metadata'].items()if metadata.get(k)==v}
   row['file_uid']=previous['file_uid']or row['file_uid'];row['metadata']=shared
  headers[path]=row
 rows=[];by_track={};count=max([0]+[int(r['index'])for r in manifest.get('audio_tracks',[])])
 for group in manifest['groups']:
  for audio in group.get('audio',[]):
   path=str(Path(audio['path']).expanduser().resolve())
   if path not in headers:raise ValueError('Original WAV asset was not found: '+path)
   track=integer(audio['track_index'],'Track');count=max(count,track);source=headers[path]
   if integer(audio.get('channels',source['channels']),'Selected channels')!=source['channels']:raise ValueError('原始WAV只选择了部分声道，内部AAF不自动扩展声道；请核对声音声道设置：'+path)
   first=integer((F(audio['record_start'])-origin)/fps*rate,'Record IN samples');last=integer((F(audio['record_end'])-origin)/fps*rate,'Record OUT samples');local=integer(F(audio['source_start'])/fps*rate,'Source IN samples')
   if track<1 or first<0 or last<=first or local<0 or local+last-first>source['samples']:raise ValueError('Audio reference exceeds source or record bounds: '+path)
   row=dict(track=track,path=path,record_samples=first,source_samples=local,duration_samples=last-first,link_group_id=audio.get('link_group_id',''));rows.append(row);by_track.setdefault(track,[]).append(row)
 for track,items in by_track.items():
  cursor=0
  for row in sorted(items,key=lambda r:r['record_samples']):
   if row['record_samples']<cursor:raise ValueError('Overlapping original WAV clips on A'+str(track))
   cursor=row['record_samples']+row['duration_samples']
 duration=integer((end-origin)/fps*rate,'Timeline length samples')
 if any(r['record_samples']+r['duration_samples']>duration for r in rows):raise ValueError('Audio extends outside timeline')
 used={r['path']for r in rows}
 return dict(fps=fps,origin_frame=origin,end_frame=end,duration_samples=duration,track_count=count,sources={k:v for k,v in headers.items()if k in used},rows=rows)


def write_audio_interchange(xml_path,manifest,output_path=None,guide_audio=(),*,allow_probe=True):
    """Write PRIVATE sample-rate AAF for ImportIntoTimeline, never audio essence.

    SourceMob slots are file-local (no rounded video-rate tape timecode). Stereo
    uses two source channel slots in the same standard Audio Channel Combiner
    structure produced by Resolve. Composition/source/master edit rates are
    48kHz, retaining subframe record positions and exact original sample INs.
    """
    import hashlib,logging,os,tempfile,zlib
    import aaf2
    from aaf2.ama import create_network_locator
    path=Path(output_path)if output_path else Path(xml_path).with_suffix('.resolve-audio.aaf')
    plan=plan_audio(xml_path,manifest,guide_audio,allow_probe);path.parent.mkdir(parents=True,exist_ok=True)
    if path.is_symlink():raise ValueError('内部声音交换文件为符号链接，未覆盖：'+str(path))
    marker='com.pgm.internalAudioBridge'
    if path.exists():
        with path.open('rb')as handle:signature=handle.read(8)
        if signature!=bytes.fromhex('d0cf11e0a1b11ae1'):raise ValueError('同名文件不是本软件生成的内部声音交换文件，未覆盖：'+str(path))
        try:
            with aaf2.open(str(path),'r')as previous:
                owned=any(m.comments[marker]=='1' for m in previous.content.compositionmobs())
        except Exception:owned=False
        if not owned:raise ValueError('同名文件不是本软件生成的内部声音交换文件，未覆盖：'+str(path))
    temporary=None
    try:
        handle,name=tempfile.mkstemp(prefix='.'+path.name+'-',suffix='.tmp',dir=path.parent);os.close(handle);temporary=Path(name)
        with aaf2.open(str(temporary),'w')as file:
            comp=file.create.CompositionMob(Path(xml_path).stem+' [Internal Original WAV]');comp.usage='Usage_TopLevel';comp.comments[marker]='1';comp.comments['XMLSourceSHA256']=hashlib.sha256(Path(xml_path).read_bytes()).hexdigest();file.content.mobs.append(comp)
            fps=integer(plan['fps'],'Timeline FPS');origin=integer(plan['origin_frame'],'Timeline start frame');frames=integer(plan['end_frame']-plan['origin_frame'],'Timeline duration frames')
            tc=comp.create_timeline_slot(fps,slot_id=1);tc.name='TC';tc.segment=file.create.Timecode(fps=fps,length=frames);tc.segment.start=origin
            masters={};operation=None
            for key,header in plan['sources'].items():
                source=file.create.SourceMob(Path(key).name);file.content.mobs.append(source);descriptor=file.create.PCMDescriptor()
                for name,value in dict(AudioSamplingRate=48000,SampleRate=48000,Channels=header['channels'],QuantizationBits=header['depth'],Length=header['samples'],BlockAlign=header['channels']*(header['depth']//8),AverageBPS=48000*header['channels']*(header['depth']//8)).items():descriptor[name].value=value
                descriptor['Locator'].append(create_network_locator(file,key));source.descriptor=descriptor
                source.comments['OriginalPath']=key;source.comments['OriginalFilename']=Path(key).name;source.comments['BWFTimeReference']=str(header['start']);source.comments['OriginalFileUID']=header.get('file_uid','')
                for name,value in header['metadata'].items():
                    if value is not None:source.comments[name]=str(value)
                master=file.create.MasterMob(Path(key).name);file.content.mobs.append(master)
                for channel in range(1,header['channels']+1):
                    slot=source.create_timeline_slot(48000,slot_id=channel);slot.name='Original channel '+str(channel);slot['PhysicalTrackNumber'].value=channel;slot.segment=file.create.SourceClip(media_kind='sound',length=header['samples'])
                    slot=master.create_timeline_slot(48000,slot_id=channel);slot.name='Original channel '+str(channel);slot['PhysicalTrackNumber'].value=channel;slot.segment=source.create_source_clip(channel,start=0,length=header['samples'],media_kind='sound')
                masters[key]=master
            by_track={}
            for row in plan['rows']:by_track.setdefault(row['track'],[]).append(row)
            for track in range(1,plan['track_count']+1):
                slot=comp.create_empty_sequence_slot(48000,slot_id=track+1,media_kind='sound');slot.name='A'+str(track);slot['PhysicalTrackNumber'].value=track;seq=slot.segment;cursor=0
                for row in sorted(by_track.get(track,[]),key=lambda r:r['record_samples']):
                    first,length=row['record_samples'],row['duration_samples']
                    if first>cursor:seq.components.append(file.create.Filler(media_kind='sound',length=first-cursor))
                    master=masters[row['path']];clip=master.create_source_clip(1,start=row['source_samples'],length=length,media_kind='sound')
                    if plan['sources'][row['path']]['channels']==2:
                        if operation is None:
                            operation=file.create.OperationDef('6b46dd7a-132d-4856-ab21-8b751d8462ec','Audio Channel Combiner','Audio Channel Combiner');operation['DataDefinition'].value=file.dictionary.lookup_datadef('sound');operation['IsTimeWarp'].value=False;operation['NumberInputs'].value=1;file.dictionary.register_def(operation)
                        combined=file.create.OperationGroup(operation,length=length,media_kind='sound');combined.segments.append(clip);combined.segments.append(master.create_source_clip(2,start=row['source_samples'],length=length,media_kind='sound'));clip=combined
                    seq.components.append(clip);cursor=first+length
                if plan['duration_samples']>cursor:seq.components.append(file.create.Filler(media_kind='sound',length=plan['duration_samples']-cursor))
        with temporary.open('rb')as stream:os.fsync(stream.fileno())
        raw=temporary.read_bytes();os.replace(temporary,path);temporary=None
        return dict(version=1,aaf_path=str(path.resolve()),bytes=len(raw),aaf_bytes=len(raw),adler32=zlib.adler32(raw),sha256=hashlib.sha256(raw).hexdigest(),clip_count=len(plan['rows']),track_count=plan['track_count'],source_count=len(plan['sources']),audio_record_origin_frame=origin,audio_rows=plan['rows'],sample_rate=48000,source_clock_policy='original-file-local',source_time_references={p:h['start']for p,h in plan['sources'].items()})
    finally:
        if temporary is not None:
            try:temporary.unlink(missing_ok=True)
            except OSError:logging.getLogger(__name__).warning('内部AAF临时文件清理失败：%s',temporary,exc_info=True)
