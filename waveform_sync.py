"""Read-only audio alignment: spectral search followed by sample correlation.

Offsets always mean reference-local seconds = sound-local seconds + offset.
Timecode is deliberately absent from this module. No media is rewritten.
"""
from collections import OrderedDict
from pathlib import Path
import copy
import math
import threading

import av
import numpy as np
from core import AnalysisError

RATE = 8000
FEATURE_RATE = 25
HOP = RATE // FEATURE_RATE
_features = OrderedDict()
_matches = OrderedDict()
_spectra = OrderedDict()
FEATURE_CACHE_BYTES = 32 * 1024 * 1024
SPECTRUM_CACHE_BYTES = 64 * 1024 * 1024
_lock = threading.RLock()


def _check(cancelled):
    if cancelled():
        raise AnalysisError('已取消声音匹配。')


def _key(path):
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise AnalysisError('找不到参考声音文件：' + str(p))
    s = p.stat()
    return str(p), s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_ino


def _remember(cache, key, value, limit):
    with _lock:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > limit:
            cache.popitem(last=False)


def _input_parts(audio_input):
    """Keep the legacy integer mono input, with explicit channels when needed."""
    return audio_input if isinstance(audio_input, tuple) else (audio_input, 0)


def _input_groups(container):
    """Enumerate channels within each stream without decoding or downmixing."""
    groups = []
    for index, stream in enumerate(container.streams.audio):
        codec = getattr(stream, 'codec_context', None)
        layout = getattr(codec, 'layout', None)
        count = len(getattr(layout, 'channels', ())) or 1
        groups.append([index] if count == 1 else [(index, channel) for channel in range(count)])
    return groups


def _samples(path, stream_index, start=0., duration=None, cancelled=lambda: False):
    """Decode one unchanged channel; trim by PTS from the first audio sample.

    Independent recorder channels must not be summed. A mono downmix can erase
    opposite-polarity copies, or mix unrelated microphone feeds into the match.
    """
    stream_number, channel = _input_parts(stream_index)
    try:
        with av.open(str(path)) as c:
            streams = list(c.streams.audio)
            if not streams:
                raise AnalysisError('没有参考声音，不能进行波形匹配：' + Path(path).name)
            s = streams[stream_number]
            origin = float((s.start_time or 0) * s.time_base)
            if start > 0:
                c.seek(int((origin + start) / float(s.time_base)), stream=s,
                       backward=True, any_frame=False)
            resampler = av.AudioResampler(format='fltp', rate=RATE)
            stop = None if duration is None else start + duration
            expected = None
            for frame in c.decode(s):
                _check(cancelled)
                for out in resampler.resample(frame):
                    x = out.to_ndarray()[channel].astype(np.float32, copy=False)
                    time = float(out.pts * out.time_base) - origin if out.pts is not None else (expected or 0.)
                    expected = time + len(x) / RATE
                    a = max(0, round((start - time) * RATE))
                    b = len(x) if stop is None else min(len(x), round((stop - time) * RATE))
                    if b > a:
                        yield x[a:b]
                    if stop is not None and expected >= stop:
                        return
            for out in resampler.resample(None):
                x = out.to_ndarray()[channel].astype(np.float32, copy=False)
                time = float(out.pts * out.time_base) - origin if out.pts is not None else (expected or 0.)
                a = max(0, round((start - time) * RATE))
                b = len(x) if stop is None else min(len(x), round((stop - time) * RATE))
                if b > a:
                    yield x[a:b]
    except (av.FFmpegError, OSError) as exc:
        raise AnalysisError('声音解码失败：' + Path(path).name + '；' + str(exc)) from exc


def _feature_data(path, stream_index, progress, cancelled):
    key = (*_key(path), stream_index)
    with _lock:
        cached = _features.get(key)
        if cached is not None:
            _features.move_to_end(key)
            return cached
    progress(0, '读取参考波形：' + Path(path).name)
    bands = [(80, 250), (250, 500), (500, 1000), (1000, 2000), (2000, 3500)]
    freq = np.fft.rfftfreq(512, 1 / RATE)
    masks = [(freq >= lo) & (freq < hi) for lo, hi in bands]
    window = np.hanning(HOP).astype(np.float32)
    carry = np.empty(0, dtype=np.float32)
    parts = []
    energy = 0.
    count = 0
    for x in _samples(path, stream_index, cancelled=cancelled):
        energy += float(np.dot(x, x)); count += len(x)
        carry = np.concatenate((carry, x))
        if len(carry) < 8192:
            continue
        n = len(carry) // HOP
        blocks = carry[:n * HOP].reshape(n, HOP)
        power = abs(np.fft.rfft(blocks * window, n=512, axis=1)) ** 2
        parts.append(np.stack([np.log1p(power[:, m].mean(axis=1) * 1e6) for m in masks], axis=1).astype(np.float32))
        carry = carry[n * HOP:]
    if len(carry) >= HOP:
        n = len(carry) // HOP
        power = abs(np.fft.rfft(carry[:n * HOP].reshape(n, HOP) * window, n=512, axis=1)) ** 2
        parts.append(np.stack([np.log1p(power[:, m].mean(axis=1) * 1e6) for m in masks], axis=1).astype(np.float32))
    if not parts or count < 3 * RATE:
        raise AnalysisError('参考声音少于3秒，无法可靠匹配：' + Path(path).name)
    if math.sqrt(energy / max(count, 1)) < 1e-6:
        raise AnalysisError('参考声音为静音，无法匹配：' + Path(path).name)
    # Temporal contrast rejects constant tones and is insensitive to gain/EQ.
    raw = np.concatenate(parts)
    mean = raw.mean(axis=0)
    std = raw.std(axis=0)
    if float(std.mean()) < .03:
        raise AnalysisError('声音缺少可辨识的变化，无法可靠匹配：' + Path(path).name)
    features = np.clip((raw - mean) / np.maximum(std, .1), -5, 5)
    result = dict(features=features, duration=count / RATE, stream_index=stream_index)
    features.flags.writeable = False
    _remember(_features, key, result, 256)
    with _lock:
        while _features and sum(row['features'].nbytes for row in _features.values()) > FEATURE_CACHE_BYTES:
            _features.popitem(last=False)
    return result


def _retained_bytes(array):
    while isinstance(array.base, np.ndarray):
        array = array.base
    return array.nbytes


def _spectrum(a, nfft, cache):
    # The retained array prevents pointer reuse; immutable features are the
    # only cached input. Refinement samples never use this cache.
    cache = cache and not a.flags.writeable
    key = (a.__array_interface__['data'][0], a.shape, a.strides, a.dtype.str, nfft)
    if cache:
        with _lock:
            saved = _spectra.get(key)
            if saved is not None:
                _spectra.move_to_end(key)
                return saved[1]
    spectrum = np.fft.rfft(a, nfft, axis=0)
    if cache and spectrum.nbytes + _retained_bytes(a) <= SPECTRUM_CACHE_BYTES:
        _remember(_spectra, key, (a, spectrum), 16)
        with _lock:
            while _spectra and sum(_retained_bytes(x) + y.nbytes for x, y in _spectra.values()) > SPECTRUM_CACHE_BYTES:
                _spectra.popitem(last=False)
    return spectrum


def _correlations(a, b, min_overlap, *, cache=False):
    """Normalized linear correlations with the energies of each overlap."""
    na, nb = len(a), len(b)
    nfft = 1 << (na + nb - 2).bit_length()
    # Correlation is linear: sum the five band spectra before one inverse FFT.
    # Complex128 accumulation keeps the normalized result stable for float32
    # features while avoiding four full inverse transforms per comparison.
    spectrum = np.sum(_spectrum(a, nfft, cache) * np.conj(_spectrum(b, nfft, cache)), axis=1, dtype=np.complex128)
    result = np.fft.irfft(spectrum, nfft)
    numerator = np.concatenate((result[-(nb - 1):], result[:na]))
    lags = np.arange(-(nb - 1), na)
    a0 = np.maximum(lags, 0); b0 = np.maximum(-lags, 0)
    lengths = np.minimum(na - a0, nb - b0)
    ae = np.r_[0., np.cumsum(np.sum(a * a, axis=1), dtype=np.float64)]
    be = np.r_[0., np.cumsum(np.sum(b * b, axis=1), dtype=np.float64)]
    denom = np.sqrt(np.maximum((ae[a0 + lengths] - ae[a0]) * (be[b0 + lengths] - be[b0]), 1e-15))
    score = np.clip(numerator / denom, -1, 1)
    score[lengths < min_overlap] = -1
    return lags, score, lengths


def _read_segment(path, stream, start, duration, cancelled):
    chunks = list(_samples(path, stream, max(0., start), duration, cancelled))
    return np.concatenate(chunks) if chunks else np.empty(0, dtype=np.float32)


def _refine(reference, sound, ri, si, sound_time, offset, seconds, cancelled):
    """Find a precise offset in a small neighborhood, allowing polarity reversal."""
    radius = .16
    rstart = sound_time + offset - radius
    if rstart < 0:
        sound_time -= rstart; rstart = 0.
    a = _read_segment(reference, ri, rstart, seconds + radius * 2, cancelled)
    b = _read_segment(sound, si, sound_time, seconds, cancelled)
    if len(b) < RATE or len(a) < len(b):
        return None
    a = a.astype(np.float64); b = b.astype(np.float64)
    # Remove DC; pre-emphasis reduces low-frequency room rumble.
    a = np.diff(a); b = np.diff(b); b -= b.mean()
    if np.linalg.norm(b) < 1e-7:
        return None
    lags, scores, lengths = _correlations(a[:, None], b[:, None], len(b))
    valid = (lags >= 0) & (lags <= len(a) - len(b))
    scores = np.where(valid, abs(scores), -1)
    i = int(np.argmax(scores))
    return dict(offset_seconds=rstart + lags[i] / RATE - sound_time,
                reference_seconds=rstart + lags[i] / RATE,
                sound_seconds=sound_time, score=float(scores[i]))


def _align_stream(reference, sound, ri, si, reference_start, reference_duration, progress, cancelled):
    ar = _feature_data(reference, ri, progress, cancelled)
    br = _feature_data(sound, si, progress, cancelled)
    first = round(reference_start * FEATURE_RATE)
    last = len(ar['features']) if reference_duration is None else first + round(reference_duration * FEATURE_RATE)
    a = ar['features'][first:last]; b = br['features']
    if min(len(a), len(b)) < 3 * FEATURE_RATE:
        raise AnalysisError('用于匹配的共同声音区间不足3秒。')
    _check(cancelled); progress(55, '搜索对应录音：' + Path(sound).name)
    minimum = max(3 * FEATURE_RATE, min(12 * FEATURE_RATE, round(min(len(a), len(b)) * .6)))
    lags, scores, lengths = _correlations(a, b, minimum, cache=True)
    # Short edge matches must not outrank a long match merely through chance.
    ranking = scores * np.sqrt(lengths / min(len(a), len(b)))
    best = int(np.argmax(ranking)); coarse = lags[best] / FEATURE_RATE + first / FEATURE_RATE
    if scores[best] < .45:
        raise AnalysisError('波形相似度不足：' + Path(sound).name)
    alternatives = np.abs(lags - lags[best]) > 2 * FEATURE_RATE
    runner = float(np.max(ranking[alternatives])) if alternatives.any() else -1
    if runner > ranking[best] * .95:
        raise AnalysisError('存在重复或相似声音，无法唯一确定同步位置：' + Path(sound).name)
    s0 = max(0., reference_start - coarse)
    s1 = min(br['duration'], (ar['duration'] if reference_duration is None else reference_start + reference_duration) - coarse)
    span = s1 - s0
    length = min(8., span / 3)
    anchors = []
    progress(75, '核对起点、中段和末段波形')
    for pos in np.linspace(s0 + .2, max(s0 + .2, s1 - length - .2), 7):
        _check(cancelled)
        anchor = _refine(reference, sound, ri, si, float(pos), coarse, length, cancelled)
        if anchor is not None and anchor['score'] >= .35:
            anchors.append(anchor)
    if len(anchors) < 2:
        raise AnalysisError('共同波形不足或参考声差异过大，未通过多点同步核查：' + Path(sound).name)
    if span > 20 and anchors[-1]['sound_seconds'] - anchors[0]['sound_seconds'] < (span - length) * .6:
        raise AnalysisError('声音首尾缺少可靠的匹配锚点，请分段核对：' + Path(sound).name)
    offsets = [v['offset_seconds'] for v in anchors]
    drift = max(offsets) - min(offsets)
    if drift > .02:
        raise AnalysisError(f'声音存在漂移或不同步（多点差异{drift:.3f}秒），请分段处理：' + Path(sound).name)
    reference_stream, reference_channel = _input_parts(ri)
    sound_stream, sound_channel = _input_parts(si)
    return dict(offset_seconds=float(np.median(offsets)), score=float(min(v['score'] for v in anchors)),
                anchors=anchors, uncertainty_seconds=max(1 / RATE, drift), drift_seconds=drift,
                method='waveform', reference_stream=reference_stream, sound_stream=sound_stream,
                reference_channel=reference_channel + 1, sound_channel=sound_channel + 1,
                reference_path=str(Path(reference).resolve()), sound_path=str(Path(sound).resolve()),
                feature_score=float(scores[best]))


def align_audio(reference_path, sound_path, reference_start=0., reference_duration=None,
                progress=lambda n, message: None, cancelled=lambda: False):
    """Match independently of timecode, refusing silence, ambiguity and drift."""
    _check(cancelled)
    if reference_start < 0 or (reference_duration is not None and reference_duration <= 0):
        raise AnalysisError('参考声音起点或长度无效。')
    key = (_key(reference_path), _key(sound_path), float(reference_start), reference_duration)
    with _lock:
        result = _matches.get(key)
        if result is not None:
            _matches.move_to_end(key)
            progress(100, '已读取波形匹配结果')
            return copy.deepcopy(result)
    try:
        with av.open(str(reference_path)) as c:
            reference_inputs = _input_groups(c)
        with av.open(str(sound_path)) as c:
            sound_inputs = _input_groups(c)
    except (av.FFmpegError, OSError) as exc:
        raise AnalysisError('无法读取声音：' + str(exc)) from exc
    if not reference_inputs or not sound_inputs:
        raise AnalysisError('PGM或指定参考文件没有声音，无法波形匹配。')
    results = []; errors = []
    # Keep separate streams independent. For a multichannel stream, first try
    # channel 1 and use the remaining channels only when that pair cannot match.
    # Normal stereo succeeds on the first pair, avoiding redundant full scans.
    for reference_group in reference_inputs:
        for sound_group in sound_inputs:
            matched = False
            for ri in reference_group:
                for si in sound_group:
                    try:
                        results.append(_align_stream(reference_path, sound_path, ri, si, reference_start,
                                                     reference_duration, progress, cancelled))
                        matched = True
                        break
                    except AnalysisError as exc:
                        _check(cancelled); errors.append(str(exc))
                if matched:
                    break
    if not results:
        raise AnalysisError('波形匹配未通过：' + '；'.join(dict.fromkeys(errors)))
    results.sort(key=lambda r: r['score'], reverse=True)
    if any(abs(results[0]['offset_seconds'] - other['offset_seconds']) > .02 for other in results[1:]):
        raise AnalysisError('参考文件不同声音流或声道给出不同同步位置，请使用唯一的参考声。')
    result = results[0]
    _remember(_matches, key, result, 64)
    progress(100, '波形匹配完成')
    return copy.deepcopy(result)
