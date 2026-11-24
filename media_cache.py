"""Bounded, worker-owned exact frame readers and file-identity metadata cache.

No decoder crosses a thread. Sessions release all containers on exit, including
on cancellation, and only reuse a frame when both its file identity and frame
number are unchanged. They never approximate frame positions or image content.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
import stat
import threading

import av


def file_identity(path):
    path = str(Path(path).expanduser().resolve())
    value = Path(path).stat()
    if not stat.S_ISREG(value.st_mode):
        raise FileNotFoundError(path)
    return (path, value.st_dev, value.st_ino, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


class MetadataCache:
    """Return detached values; callers cannot mutate the cached metadata."""
    def __init__(self, limit=4096):
        self.limit = max(1, int(limit))
        self.values = OrderedDict()
        self.paths = {}
        self.lock = threading.RLock()
        self.hits = self.misses = 0

    def get(self, identity, loader):
        with self.lock:
            if identity in self.values:
                self.hits += 1
                self.values.move_to_end(identity)
                return deepcopy(self.values[identity])
        result = loader()
        with self.lock:
            # Discard old versions of the same path as well as old LRU entries.
            previous = self.paths.get(identity[0])
            if previous is not None and previous != identity:
                self.values.pop(previous, None)
            self.values[identity] = deepcopy(result)
            self.paths[identity[0]] = identity
            self.values.move_to_end(identity)
            self.misses += 1
            while len(self.values) > self.limit:
                key, _ = self.values.popitem(last=False)
                if self.paths.get(key[0]) == key:
                    del self.paths[key[0]]
        return result

    def clear(self):
        with self.lock:
            self.values.clear()
            self.paths.clear()
            self.hits = self.misses = 0


video_metadata_cache = MetadataCache()
_local = threading.local()


class _Decoder:
    def __init__(self, path):
        self.container = av.open(path)
        try:
            if not self.container.streams.video:
                from core import AnalysisError
                raise AnalysisError(f'没有视频轨：{path}')
            self.stream = self.container.streams.video[0]
            self.rate = self.stream.average_rate or self.stream.base_rate
            if not self.rate:
                from core import AnalysisError
                raise AnalysisError(f'无法读取帧率：{path}')
            self.origin = self.stream.start_time or 0
            self.iterator = iter(self.container.decode(self.stream))
            self.last_index = -1
        except BaseException:
            self.container.close()
            raise

    def close(self):
        self.iterator = None
        self.container.close()


class FrameReadSession:
    """Read exact CFR frames with bounded LRU containers and decoded frame bytes.

Use one instance in one worker thread, preferably as a context manager. Nearby
forward requests continue decoding; distant/backward requests seek to a keyframe.
Returned arrays are independent and writable, so preview code cannot poison the
cached copy. ``cancelled`` is checked between every decoded frame.
    """
    def __init__(self, max_open=2, max_cache_bytes=24*1024*1024, cancelled=None):
        self.max_open = max(1, int(max_open))
        self.max_cache_bytes = max(0, int(max_cache_bytes))
        self.cancelled = cancelled or (lambda: False)
        self.owner = threading.get_ident()
        self.decoders = OrderedDict()
        self.frames = OrderedDict()
        self.cached_bytes = 0
        self.closed = False
        self.stats = dict(opens=0, seeks=0, decoded_frames=0, cache_hits=0,
                          max_open=0, peak_cached_bytes=0)

    def _check(self):
        from core import AnalysisError
        if self.closed:
            raise AnalysisError('视频读取任务已关闭。')
        if threading.get_ident() != self.owner:
            raise RuntimeError('视频解码会话不能跨线程使用。')
        if self.cancelled():
            raise AnalysisError('已取消，未导出时间线。')

    def _decoder(self, identity):
        # If a file was replaced while the app is open, neither old decoded
        # pixels nor an old container may survive the identity change.
        for key in tuple(self.decoders):
            if key[0] == identity[0] and key != identity:
                self.decoders.pop(key).close()
        for key in tuple(self.frames):
            if key[0][0] == identity[0] and key[0] != identity:
                self.cached_bytes -= self.frames.pop(key).nbytes
        if identity not in self.decoders:
            while len(self.decoders) >= self.max_open:
                self.decoders.popitem(last=False)[1].close()
            self.decoders[identity] = _Decoder(identity[0])
            self.stats['opens'] += 1
            self.stats['max_open'] = max(self.stats['max_open'], len(self.decoders))
        self.decoders.move_to_end(identity)
        return self.decoders[identity]

    def read(self, path, index=0):
        from core import AnalysisError
        self._check()
        if not isinstance(index, int) or index < 0:
            raise AnalysisError('采样帧号必须为非负整数。')
        try:
            identity = file_identity(path)
        except OSError as exc:
            raise AnalysisError(f'找不到或无法读取文件：{path}') from exc
        key = (identity, index)
        if key in self.frames:
            self.frames.move_to_end(key)
            self.stats['cache_hits'] += 1
            return self.frames[key].copy()
        try:
            state = self._decoder(identity)
            self._check()
            target = Fraction(index, 1) / state.rate
            # A two-second bound avoids decoding a long shot just to obtain the
            # next cut. Short 0/2/5 sampling groups share their current GOP.
            if index <= state.last_index or index - state.last_index > max(8, round(state.rate*2)):
                state.container.seek(state.origin + int(target / state.stream.time_base),
                                     stream=state.stream, backward=True, any_frame=False)
                state.iterator = iter(state.container.decode(state.stream))
                self.stats['seeks'] += 1
            for frame in state.iterator:
                self._check()
                self.stats['decoded_frames'] += 1
                if frame.pts is None:
                    raise AnalysisError('PGM缺少视频时间戳，不能准确读取切点帧。')
                elapsed = (frame.pts - state.origin) * state.stream.time_base
                state.last_index = round(elapsed * state.rate)
                if elapsed < target - Fraction(1, 2) / state.rate:
                    continue
                if frame.interlaced_frame or abs(float((elapsed-target)*state.rate)) > .1:
                    raise AnalysisError('目标采样帧不是连续逐行恒定帧率视频，请检查PGM。')
                pixels = frame.to_ndarray(format='bgr24')
                self._check()
                if pixels.nbytes <= self.max_cache_bytes:
                    while self.frames and self.cached_bytes + pixels.nbytes > self.max_cache_bytes:
                        self.cached_bytes -= self.frames.popitem(last=False)[1].nbytes
                    self.frames[key] = pixels.copy()
                    self.cached_bytes += pixels.nbytes
                    self.stats['peak_cached_bytes'] = max(self.stats['peak_cached_bytes'], self.cached_bytes)
                return pixels
        except av.FFmpegError as exc:
            raise AnalysisError(f'视频读取失败：{identity[0]}\n{exc}') from exc
        raise AnalysisError(f'无法读取第 {index} 帧：{path}')

    def close(self):
        if not self.closed:
            self.closed = True
            for decoder in self.decoders.values():
                decoder.close()
            self.decoders.clear()
            self.frames.clear()
            self.cached_bytes = 0

    def __enter__(self):
        self._check()
        return self

    def __exit__(self, *args):
        self.close()


@contextmanager
def frame_read_session(**kwargs):
    """Make unchanged ``core.read_frame`` calls reuse this worker's session."""
    previous = getattr(_local, 'frame_session', None)
    with FrameReadSession(**kwargs) as session:
        _local.frame_session = session
        try:
            yield session
        finally:
            _local.frame_session = previous


def current_frame_session():
    return getattr(_local, 'frame_session', None)
