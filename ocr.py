"""Local OCR through Apple's Vision. No cloud service or video upload."""
from __future__ import annotations

from io import BytesIO
from collections import OrderedDict
from functools import lru_cache
import re
import unicodedata

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps
from camera_schema import active_label_cameras, cameras_for


@lru_cache(maxsize=16)
def _context_font(size):
    return ImageFont.truetype('/System/Library/Fonts/Supplemental/Arial.ttf', size)


class VisionOCR:
    def __init__(self, accurate=True, custom_words=None):
        import Vision
        self.Vision = Vision
        self.accurate = accurate
        self.custom_words = list(custom_words or [])
        self._request = None
        self._request_words = None

    def recognize(self, rgb, *, upscale=True):
        import objc
        from Foundation import NSData
        with objc.autorelease_pool():
            im = Image.fromarray(np.asarray(rgb, dtype=np.uint8), 'RGB')
            # Vision can omit an isolated character. Add known adjacent OCR context
            # for narrow character crops, then remove only that synthetic suffix.
            context = im.width / im.height < 1.5 and any(len(word)==1 for word in self.custom_words)
            if context:
                background = np.median(np.concatenate([rgb[0],rgb[-1],rgb[:,0],rgb[:,-1]]),axis=0)
                glyph = np.max(np.abs(rgb.astype(float)-background),axis=2)>60
                ys = np.nonzero(glyph)[0]
                height = int(ys.max()-ys.min()+1) if len(ys) else max(10,round(im.height*.6))
                font = _context_font(max(12,round(height*1.35)))
                bbox = font.getbbox('OCR');padding=max(4,round(height*.3))
                canvas = Image.new('RGB',(im.width+padding+round(font.getlength('OCR'))+padding,im.height),tuple(int(v) for v in background))
                canvas.paste(im,(0,0))
                color = 'white' if background.mean()<128 else 'black'
                ImageDraw.Draw(canvas).text((im.width+padding,(int(ys.min()) if len(ys) else padding)-bbox[1]),'OCR',font=font,fill=color)
                im=canvas
            # Upscale small on-screen labels without resizing the entire PGM.
            pixel_scale = (512*1024/(im.width*im.height))**.5
            scale = max(1, min(4, 96 / im.height, pixel_scale)) if upscale else min(1., pixel_scale)
            if scale != 1:
                im = im.resize((round(im.width*scale), round(im.height*scale)), Image.Resampling.LANCZOS)
            if self._request is None:
                self._request = self.Vision.VNRecognizeTextRequest.alloc().init()
                self._request.setRecognitionLevel_(self.Vision.VNRequestTextRecognitionLevelAccurate if self.accurate
                                                  else self.Vision.VNRequestTextRecognitionLevelFast)
                self._request.setRecognitionLanguages_(['en-US'])
                self._request.setUsesLanguageCorrection_(False)
                self._request.setMinimumTextHeight_(.015)
                # Prefer CPU recognition; launch the native app for macOS Vision/XPC access.
                self._request.setUsesCPUOnly_(True)
            request = self._request
            words = self.custom_words+[word+'OCR' for word in self.custom_words] if context else self.custom_words
            if words != self._request_words:
                request.setCustomWords_(words)
                self._request_words = list(words)
            data = BytesIO();im.save(data, format='PNG');raw = data.getvalue()
            nsdata = NSData.dataWithBytes_length_(raw, len(raw))
            handler = self.Vision.VNImageRequestHandler.alloc().initWithData_options_(nsdata, {})
            ok, error = handler.performRequests_error_([request], None)
            if not ok:
                raise RuntimeError(f'macOS 文字识别失败：{error}')
            result = []
            for observation in request.results() or []:
                candidates = observation.topCandidates_(1)
                if candidates:
                    candidate = candidates[0]
                    text = str(candidate.string())
                    if context and normalize(text).endswith('OCR'):
                        text = normalize(text)[:-3]
                    if text:
                        result.append(dict(text=text, confidence=float(candidate.confidence())))
            return result

    def recognize_enhanced(self, rgb):
        return self.recognize(enhance_label_crop(rgb), upscale=False)


def enhance_label_crop(rgb):
    """One bounded grayscale/contrast retry, without thresholding thin glyphs."""
    im = Image.fromarray(np.asarray(rgb, dtype=np.uint8), 'RGB')
    scale = max(1., min(4., 160 / im.height, (512*1024/(im.width*im.height))**.5))
    if scale > 1:
        im = im.resize((round(im.width*scale), round(im.height*scale)), Image.Resampling.BICUBIC)
    gray = ImageOps.grayscale(im)
    lo, hi = np.percentile(np.asarray(gray), (1, 99))
    if hi > lo + 4:
        lut = np.clip((np.arange(256)-lo)*(255./(hi-lo)), 0, 255).astype(np.uint8).tolist()
        gray = gray.point(lut)
    return np.asarray(gray.convert('RGB'))


# Vision sometimes returns a visually identical Cyrillic/Greek letter even in
# en-US mode (e.g. U+0412 in B003C001). Never translate 0/O or 8/B globally:
# those are distinct valid camera IDs, and numeric repair is position-specific.
_LOOKALIKES = str.maketrans(dict(zip(
    'АВСЕНКМОРТХІЈЅΑΒΕΖΗΙΚΜΝΟΡΤΥΧ',
    'ABCEHKMOPTXIJSABEZHIKMNOPTYX')))


def normalize(text):
    text = str(text).upper()
    if text.isascii():
        return re.sub(r'[^A-Z0-9]', '', text)
    text = unicodedata.normalize('NFKC', text).translate(_LOOKALIKES)
    # Keep unknown Unicode letters as a barrier, rather than deleting a bad
    # leading glyph and accidentally matching an internal C / clip counter.
    return ''.join(char for char in text if char.isalnum())


LABEL_MATCH_MODES = frozenset(('legacy', 'prefix', 'exact'))


def _digits_corrected(text, target):
    """Correct OCR substitutions only at positions whose rule expects digits."""
    return ''.join({'O': '0', 'I': '1', 'L': '1'}.get(char, char)
                   if target[index].isdigit() else char
                   for index, char in enumerate(text))


def _match_normalized(text, labels, counter_suffix=False, match_mode='legacy'):
    if match_mode != 'legacy':
        matches = set()
        for camera, target in labels:
            if not target or len(text) < len(target):
                continue
            if match_mode == 'exact' and len(text) != len(target):
                continue
            # Only the beginning is eligible: the C inside B003C001 is not a
            # C-camera label. In these modes every camera obeys the user's rule.
            if _digits_corrected(text[:len(target)], target) == target:
                matches.add(camera)
        return next(iter(matches)) if len(matches) == 1 else None
    # Sony IDs: A003C001 / B003C001 / C003C001 / D003C001. The internal C is
    # the clip-number separator, never another camera. Counters may change.
    # Accept common OCR digit substitutions without changing the camera letter.
    if re.fullmatch(r'[ABCD][0-9OIL]+C[0-9OIL]+', text):
        return text[0]
    matches = set()
    for camera, target in labels:
        if not target:
            continue
        if text == target:
            matches.add(camera)
        # Correct OCR O/I/L only where the custom label expects digits.
        if len(text) == len(target):
            corrected = _digits_corrected(text, target)
            if corrected == target:
                matches.add(camera)
        # Input B accepts a crop of B itself, or B0001/B0002/... .
        # Numeric IDs stay exact: 440 must not match 1440 or 4401.
        if target.isalpha() and re.fullmatch(re.escape(target)+r'[0-9OIL]+',text):
            matches.add(camera)
        if counter_suffix and camera in ('B','D') and re.fullmatch(camera+r'\d{4}', text):
            matches.add(camera)
    return next(iter(matches)) if len(matches)==1 else None


class LabelMatcher:
    """Compile per-project prefixes once rather than once per OCR observation."""
    def __init__(self, labels, counter_suffix=False, match_mode='legacy'):
        if match_mode not in LABEL_MATCH_MODES:
            raise ValueError('未知的机位标号匹配方式。')
        self.labels = tuple((camera, normalize(value)) for camera, value in labels.items())
        self.counter_suffix, self.match_mode = counter_suffix, match_mode
        self.cache = OrderedDict()

    def match(self, text):
        text = normalize(text)
        if text not in self.cache:
            self.cache[text] = _match_normalized(text, self.labels, self.counter_suffix, self.match_mode)
            while len(self.cache) > 256:
                self.cache.popitem(last=False)
        self.cache.move_to_end(text)
        return self.cache[text]

    def is_unmatched_numeric_fragment(self, text):
        if not re.fullmatch(r'[0-9OIL\s_:.,/-]+', str(text).upper()):
            return False
        text = normalize(text)
        if not re.fullmatch(r'[0-9OIL]+', text):
            return False
        # A failed unique match may mean overlapping camera rules. Check each
        # rule before treating a counter/timecode fragment as unrelated text.
        return not any(_match_normalized(text, (label,), self.counter_suffix, self.match_mode)
                       is not None for label in self.labels)


def match_label(text, labels, counter_suffix=False, match_mode='legacy'):
    """Match explicit prefixes/exact IDs, retaining old projects' legacy rules."""
    return LabelMatcher(labels, counter_suffix, match_mode).match(text)


class LabelReader:
    def __init__(self, config, backend=None):
        self.config = config
        self.match_mode = config.get('label_match_mode', 'legacy')
        if self.match_mode not in LABEL_MATCH_MODES:
            raise ValueError('未知的机位标号匹配方式。')
        self.cameras = cameras_for(config)
        self.active_cameras = set(active_label_cameras(config))
        self.labels = {camera: value for camera, value in config.get(
            'labels', {camera: camera for camera in self.cameras}).items()
            if camera in self.active_cameras}
        self.backend = backend or (VisionOCR(custom_words=[normalize(value) for value in self.labels.values()])
                                   if self.active_cameras else None)
        self.matcher = LabelMatcher(self.labels, config.get('counter_suffix', False), self.match_mode)
        self.cache = OrderedDict()
        self.cache_bytes = 0
        self.cache_limit = max(0, min(64*1024*1024, int(config.get('ocr_cache_bytes', 8*1024*1024))))
        self.box_cache = OrderedDict()
        self.calls = 0
        self.hits = 0
        self.enhanced_calls = 0

    def inspect(self, frame, rois, index=0):
        from core import roi_pixels
        h, w = frame.shape[:2]
        # Overlapping/shared ROIs need only one recognition pass.
        geometry = (w, h, tuple((camera, tuple(roi)) for camera, roi in rois.items()
                               if camera in self.active_cameras))
        if geometry not in self.box_cache:
            boxes = {}
            for camera, roi in rois.items():
                if camera in self.active_cameras:
                    boxes.setdefault(roi_pixels(roi,w,h),set()).add(camera)
            self.box_cache[geometry] = tuple((box, frozenset(boxes[box])) for box in sorted(boxes))
            while len(self.box_cache) > 8:
                self.box_cache.popitem(last=False)
        self.box_cache.move_to_end(geometry)
        boxes = self.box_cache[geometry]
        records = []
        for (x0,y0,x1,y1), expected in boxes:
            # Serialize this small crop once. Construct the RGB ndarray view only
            # on a cache miss; cache hits need neither another array nor Vision.
            raw = frame[y0:y1,x0:x1,::-1].tobytes()
            # Exact RGB caching only: no scene-dependent similarity threshold.
            shape = (y1-y0, x1-x0, 3)
            key = (shape, raw)
            if key in self.cache:
                entry = self.cache[key]
                self.cache.move_to_end(key)
                self.hits += 1
            else:
                rgb = np.frombuffer(raw, dtype=np.uint8).reshape(shape)
                entry = dict(original=self.backend.recognize(rgb), enhanced=None)
                self.calls += 1
                if len(raw) <= self.cache_limit:
                    while self.cache and (len(self.cache) >= 128 or self.cache_bytes + len(raw) > self.cache_limit):
                        old, _ = self.cache.popitem(last=False)
                        self.cache_bytes -= len(old[1])
                    self.cache[key] = entry
                    self.cache_bytes += len(raw)
            # Keep coordinates, not all ROI pixel buffers. Cache owns at most
            # its byte budget even if a project defines many different boxes.
            records.append((expected, entry, shape, (x0, y0, x1, y1)))
        decision = self._classify([(expected, entry['original']) for expected, entry, _, _ in records])
        if decision[0] != '?' or decision[1] == 'multiple_labels' or not hasattr(self.backend, 'recognize_enhanced'):
            return decision
        # Only the unresolved frame takes this branch, and at most ONE crop is
        # enhanced. Normal shots and blank-A inference have zero extra OCR work.
        threshold = self.config.get('ocr_confidence', .65)
        candidates = []
        for number, (expected, entry, shape, raw) in enumerate(records):
            observations = entry['original']
            if any(self.matcher.match(o['text']) in expected and o['confidence'] >= threshold for o in observations):
                continue
            if shape[0]*shape[1] > 512*1024:
                continue  # An accidentally full-frame ROI must not become an expensive retry.
            visible = [o for o in observations if str(o['text']).strip()]
            candidates.append((bool(visible), max((o['confidence'] for o in visible), default=0.), -number))
        if not candidates:
            return decision
        original_ids = {self.matcher.match(o['text']) for _, row, _, _ in records
                        for o in row['original']} - {None}
        if len(original_ids) > 1:
            return decision
        number = -max(candidates)[2]
        expected, entry, shape, box = records[number]
        if entry['enhanced'] is None:
            x0, y0, x1, y1 = box
            raw = frame[y0:y1,x0:x1,::-1].tobytes()
            entry['enhanced'] = self.backend.recognize_enhanced(np.frombuffer(raw, dtype=np.uint8).reshape(shape))
            self.calls += 1; self.enhanced_calls += 1
        enhanced_ids = {self.matcher.match(o['text']) for o in entry['enhanced']} - {None}
        if original_ids and not enhanced_ids.issubset(original_ids):
            return decision  # Enhancement cannot rewrite a conflicting camera identity.
        enhanced = self._classify([(exp, row['enhanced'] if index == number else row['original'])
                                  for index, (exp, row, _, _) in enumerate(records)])
        if enhanced[0] != '?' and enhanced[1] == 'label_read':
            return (*enhanced[:3], decision[3] + ['增强识别: ' + t for t in enhanced[3]])
        # Keep any new explicit contradiction visible to the multi-frame check.
        return enhanced if enhanced[1] == 'multiple_labels' else decision

    def _classify(self, observations_by_roi):
        found, uncertain, texts = set(), set(), []
        numeric_fragments = False
        confidence = 0.
        for expected, observations in observations_by_roi:
            for observation in observations:
                observed_text = str(observation['text'])
                if not observed_text.strip():
                    continue
                texts.append(observed_text)
                cam = self.matcher.match(observed_text)
                score = observation['confidence']
                if cam in expected and score >= self.config.get('ocr_confidence',.65):
                    found.add(cam)
                    confidence = max(confidence,score)
                elif cam is None and self.matcher.is_unmatched_numeric_fragment(observed_text):
                    # These fragments cannot veto a unique reliable label, but
                    # text-only frames must still remain unknown rather than A.
                    numeric_fragments = True
                else:
                    # Text exists but isn't a confident expected label: not A.
                    uncertain.update(expected)
        if len(found) > 1:
            return '?','multiple_labels',confidence,texts
        if len(found) == 1 and not (found & uncertain):
            return next(iter(found)),'label_read',confidence,texts
        if found or uncertain or numeric_fragments:
            return '?','unreadable_label',confidence,texts
        if (observations_by_roi or self.cameras == ('A',)) and 'A' in self.cameras and self.config.get('infer_a_when_blank',False):
            return 'A','A_inferred_blank',0.,texts
        return '?','no_label',0.,texts
