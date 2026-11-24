"""Find camera examples at imported cut points, before ROI calibration."""
import re
import time
from PyQt6.QtCore import QThread,pyqtSignal
from ocr import VisionOCR,LabelMatcher,normalize
from core import read_frame
from media_cache import frame_read_session

def find_examples(config,cut,progress=lambda *args:None,cancelled=lambda:False,backend=None,target=None):
    if cancelled():return {}
    labels={camera:value for camera,value in config['labels'].items()
            if camera!='A' or not config.get('infer_a_when_blank',False)}
    ocr=backend or VisionOCR(custom_words=list(labels.values()));found={}
    matcher=LabelMatcher(labels,config.get('counter_suffix',False),config.get('label_match_mode','legacy'))
    last_percent=-1;last_progress=0.
    with frame_read_session(max_open=1, max_cache_bytes=0, cancelled=cancelled):
        for number,shot in enumerate(cut['shots']):
            if cancelled():break
            local=min(shot['source_in']+2,shot['source_out']-1)
            frame=read_frame(shot['pgm_path'],local)
            cameras=set()
            observations=ocr.recognize(frame[:,:,::-1].copy())
            if cancelled():break
            for obs in observations:
                if obs['confidence']<config.get('ocr_confidence',.65):continue
                for token in [obs['text']]+re.findall(r'[A-Za-z0-9]+',obs['text']):
                    camera=matcher.match(token)
                    if camera:cameras.add(camera)
            # Examples guide ROI selection only; never write final camera decisions.
            if len(cameras)==1:
                camera=cameras.pop();found.setdefault(camera,dict(shot=number,frame=shot['start']+local-shot['source_in']))
            percent=round((number+1)/len(cut['shots'])*100);now=time.monotonic()
            if percent!=last_percent or now-last_progress>=.1:
                progress(percent,f'查找示例：{number+1}/{len(cut["shots"])}；已找到 {" / ".join(found) or "暂无"}')
                last_percent,last_progress=percent,now
            if (target and target in found) or set(found)==set(labels):break
    return found

class ExampleWorker(QThread):
    progress=pyqtSignal(int,str);done=pyqtSignal(object);failed=pyqtSignal(str)
    def __init__(self,config,cut,target=None):super().__init__();self.config=config;self.cut=cut;self.target=target
    def run(self):
        try:self.done.emit(find_examples(self.config,self.cut,self.progress.emit,self.isInterruptionRequested,target=self.target))
        except Exception as exc:self.failed.emit(str(exc))
